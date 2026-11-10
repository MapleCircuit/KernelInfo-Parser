"""db_engine/sqlite_store.py - High-Performance SQLite Storage Engine.

Implements SQLiteStore with TableSpec DDL generation, connection pooling / sharing,
pragmas, transactions, attachment sessions, and spec-driven reads and writes.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, ContextManager, Iterator, Literal, Mapping, NamedTuple, Sequence, Self

from db_engine.spec import (
    Agg,
    AnyOf,
    Col,
    Cond,
    Const,
    Exists,
    Inc,
    InSelect,
    Join,
    Select,
    Source,
    Value,
    Where,
)
from db_engine.sql_render import (
    render_select,
    render_insert,
    render_update,
    render_delete,
    render_copy,
    validate_ident,
)

if TYPE_CHECKING:
    from core.TableHandling import Table


class TableSpec(NamedTuple):
    """Specification for SQLite table definition and indexing."""

    name: str
    columns: Sequence[tuple[str, str] | Sequence[str]]  # (col_name, col_type, [modifiers...])
    primary_key: Sequence[str] = ()
    unique: Sequence[Sequence[str]] = ()
    indexes: Sequence[tuple[str, Sequence[str]]] = ()
    without_rowid: bool = False


class StoreError(Exception):
    """Base exception for SQLiteStore errors."""


class MissingTableError(StoreError):
    """Raised when an operation targets a table that does not exist."""


class SQLiteStore:
    """Thread-safe SQLite store supporting spec-driven reads, writes, and attach sessions."""

    def __init__(
        self,
        path: Path | str,
        *,
        tables: Sequence[TableSpec] = (),
        pragmas: Mapping[str, Any] | None = None,
        read_only: bool = False,
        immutable: bool = False,
        autocommit: bool = False,
        timeout: float = 60.0,
        connections: Literal["shared", "per_thread"] = "shared",
        busy_retries: int = 0,
    ) -> None:
        self.path = Path(path).resolve()
        self.tables = tables
        self.pragmas = dict(pragmas or {})
        self.read_only = read_only
        self.immutable = immutable
        self.autocommit = autocommit
        self.timeout = timeout
        self.connections_mode = connections
        self.busy_retries = busy_retries

        self._lock = threading.RLock()
        self._shared_conn: sqlite3.Connection | None = None
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._in_txn = False

        # If not read_only, ensure parent directory exists and initialize schema
        if not self.read_only and not self.immutable:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()

    def exists(self) -> bool:
        """Check if SQLite database file exists on disk without creating it."""
        return self.path.exists() and self.path.is_file()

    def _open_connection(self) -> sqlite3.Connection:
        """Open and configure a new SQLite connection."""
        if self.immutable:
            uri = f"file:{self.path.as_posix()}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, timeout=self.timeout, isolation_level=None)
        elif self.read_only:
            uri = f"file:{self.path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=self.timeout, isolation_level=None)
        else:
            conn = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)

        # Set default row_factory to sqlite3.Row for column access by name
        conn.row_factory = sqlite3.Row

        # Apply pragmas
        default_pragmas = {
            "journal_mode": "WAL",
            "synchronous": "NORMAL",
            "foreign_keys": "ON",
        }
        if not self.read_only and not self.immutable:
            for k, v in default_pragmas.items():
                if k not in self.pragmas:
                    try:
                        conn.execute(f"PRAGMA {k} = {v};")
                    except Exception:
                        pass

        for k, v in self.pragmas.items():
            try:
                conn.execute(f"PRAGMA {k} = {v};")
            except Exception:
                pass

        with self._lock:
            self._all_conns.append(conn)
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        """Retrieve connection according to connection mode."""
        if self.connections_mode == "shared":
            with self._lock:
                if self._shared_conn is None:
                    self._shared_conn = self._open_connection()
                return self._shared_conn
        else:
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._open_connection()
                self._local.conn = conn
            return conn

    def _init_schema(self) -> None:
        """Initialize tables, unique constraints, and indexes from TableSpecs."""
        if not self.tables:
            return
        conn = self._get_connection()
        with self._lock:
            for spec in self.tables:
                col_defs = []
                for col in spec.columns:
                    col_defs.append(" ".join(col))
                if spec.primary_key:
                    col_defs.append(f"PRIMARY KEY ({', '.join(spec.primary_key)})")
                for u in spec.unique:
                    col_defs.append(f"UNIQUE ({', '.join(u)})")
                without_rowid = " WITHOUT ROWID" if spec.without_rowid else ""
                sql = f"CREATE TABLE IF NOT EXISTS {spec.name} ({', '.join(col_defs)}){without_rowid};"
                try:
                    conn.execute(sql)
                except sqlite3.Error as e:
                    raise StoreError(f"Failed to create table {spec.name}: {e}") from e

                for idx_name, idx_cols in spec.indexes:
                    idx_sql = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {spec.name} ({', '.join(idx_cols)});"
                    try:
                        conn.execute(idx_sql)
                    except sqlite3.Error as e:
                        raise StoreError(f"Failed to create index {idx_name}: {e}") from e
            conn.commit()

    def has_table(self, name: str) -> bool:
        """Check whether table or view exists in sqlite_master."""
        conn = self._get_connection()
        try:
            cur = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1;",
                (name,),
            )
            return cur.fetchone() is not None
        except sqlite3.Error as e:
            raise StoreError(f"Error checking table {name}: {e}") from e

    def meta_get(self, key: str) -> str | None:
        """Retrieve value string from meta table, or None if key / table absent."""
        if not self.has_table("meta"):
            return None
        conn = self._get_connection()
        try:
            cur = conn.execute("SELECT value FROM meta WHERE key = ? LIMIT 1;", (key,))
            row = cur.fetchone()
            return str(row[0]) if row and row[0] is not None else None
        except sqlite3.Error as e:
            raise StoreError(f"meta_get failed for key '{key}': {e}") from e

    def meta_set(self, **values: Any) -> None:
        """Store key-value pairs into meta table (ensuring table exists)."""
        conn = self._get_connection()
        with self._lock:
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);")
                for k, v in values.items():
                    val_str = str(v) if v is not None else None
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                        (k, val_str),
                    )
                conn.commit()
            except sqlite3.Error as e:
                raise StoreError(f"meta_set failed: {e}") from e

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Self]:
        """Atomic transaction context manager protected by writer lock."""
        conn = self._get_connection()
        with self._lock:
            self._in_txn = True
            try:
                conn.execute("BEGIN IMMEDIATE;")
                yield self
                conn.execute("COMMIT;")
            except BaseException:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK;")
                raise
            finally:
                self._in_txn = False

    def clear(self, tables: Sequence[str]) -> None:
        """Atomically delete all rows from specified tables."""
        conn = self._get_connection()
        with self._lock:
            try:
                conn.execute("BEGIN IMMEDIATE;")
                for tbl in tables:
                    validate_ident(tbl)
                    conn.execute(f"DELETE FROM {tbl};")
                conn.commit()
            except sqlite3.Error as e:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK;")
                raise StoreError(f"clear failed for tables {tables}: {e}") from e

    @contextlib.contextmanager
    def session(self, attach: Mapping[str, Path | str] = {}) -> Iterator[SQLiteSession]:
        """Create a fresh connection, attach specified databases, and yield SQLiteSession."""
        conn = self._open_connection()
        try:
            for alias, db_path in attach.items():
                validate_ident(alias)
                conn.execute(f"ATTACH DATABASE ? AS {alias};", (str(db_path),))
            session = SQLiteSession(conn, self)
            yield session
        finally:
            for alias in attach.keys():
                with contextlib.suppress(Exception):
                    conn.execute(f"DETACH DATABASE {alias};")
            conn.close()
            with self._lock:
                if conn in self._all_conns:
                    self._all_conns.remove(conn)

    def wal_checkpoint(self, mode: str = "TRUNCATE") -> None:
        """Execute PRAGMA wal_checkpoint(mode)."""
        conn = self._get_connection()
        try:
            conn.execute(f"PRAGMA wal_checkpoint({mode});")
        except sqlite3.Error as e:
            raise StoreError(f"wal_checkpoint failed: {e}") from e

    def close_thread(self) -> None:
        """Close connection associated with current thread."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
            with self._lock:
                if conn in self._all_conns:
                    self._all_conns.remove(conn)
            self._local.conn = None

    def close_all(self) -> None:
        """Close all opened connections."""
        with self._lock:
            if self._shared_conn is not None:
                with contextlib.suppress(Exception):
                    self._shared_conn.close()
                self._shared_conn = None
            for conn in list(self._all_conns):
                with contextlib.suppress(Exception):
                    conn.close()
            self._all_conns.clear()
            self._local.conn = None

    # -------------------------------------------------------------------------
    # Spec Read Primitives (§2)
    # -------------------------------------------------------------------------
    def select_rows(
        self,
        source: Source,
        columns: Sequence[str | Agg | Const] | Mapping[str, str | Agg | Const],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        limit: int | None = None,
        offset: int | None = None,
        as_dict: bool | None = None,
    ) -> list[tuple | dict]:
        """Execute spec-driven SELECT with auto-chunking at 500 values."""
        if _has_empty_in(where):
            return []

        # Auto-chunking at 500 values on SQLite
        term_idx, target_col, oversize = _find_oversize_in(where, chunk_size=500)
        if oversize is not None and target_col is not None:
            if limit is not None or offset is not None:
                raise ValueError("Auto-chunking cannot be combined with limit/offset")
            merged = []
            for i in range(0, len(oversize), 500):
                chunk = oversize[i : i + 500]
                chunk_where = _replace_in(where, term_idx, target_col, chunk)
                sub = self.select_rows(
                    source,
                    columns,
                    where=chunk_where,
                    joins=joins,
                    distinct=distinct,
                    order_by=order_by,
                    as_dict=as_dict,
                )
                merged.extend(sub)
            if distinct:
                merged = _dedup_list(merged, bool(as_dict))
            if order_by:
                _, _, col_names = render_select(
                    Select(source, columns, None, joins, distinct, order_by),
                    dialect="sqlite",
                )
                merged = _sort_list(merged, order_by, col_names, bool(as_dict))
            return merged

        select_spec = Select(
            source=source,
            columns=columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )
        sql, params, col_names = render_select(select_spec, dialect="sqlite")
        conn = self._get_connection()
        try:
            cur = conn.execute(sql, tuple(params))
            raw_rows = cur.fetchall()
            return _format_rows(raw_rows, col_names, bool(as_dict))
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                raise MissingTableError(f"Table not found: {e}") from e
            raise StoreError(f"Query error: {e}") from e
        except sqlite3.Error as e:
            raise StoreError(f"Database error: {e}") from e

    def iter_rows(
        self,
        source: Source,
        columns: Sequence[str | Agg | Const] | Mapping[str, str | Agg | Const],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        limit: int | None = None,
        offset: int | None = None,
        as_dict: bool | None = None,
    ) -> Iterator[tuple | dict]:
        """Stream query result rows."""
        rows = self.select_rows(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            offset=offset,
            as_dict=as_dict,
        )
        yield from rows

    def select_one(
        self,
        source: Source,
        columns: Sequence[str | Agg | Const] | Mapping[str, str | Agg | Const],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        as_dict: bool | None = None,
    ) -> tuple | dict | None:
        rows = self.select_rows(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=1,
            as_dict=as_dict,
        )
        return rows[0] if rows else None

    def select_values(
        self,
        source: Source,
        column: str,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        limit: int | None = None,
    ) -> list:
        rows = self.select_rows(
            source,
            [column],
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            as_dict=False,
        )
        return [r[0] for r in rows]

    def select_value(
        self,
        source: Source,
        column: str,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        order_by: Sequence[str] = (),
        default: Any = None,
    ) -> Any:
        vals = self.select_values(
            source,
            column,
            where=where,
            joins=joins,
            order_by=order_by,
            limit=1,
        )
        return vals[0] if vals else default

    def count_rows(
        self,
        source: Source,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: str | None = None,
    ) -> int:
        agg = Agg("COUNT", distinct if distinct else "*", distinct=bool(distinct))
        rows = self.select_rows(source, [agg], where=where, joins=joins, as_dict=False)
        return int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0

    def row_exists(
        self,
        source: Source,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
    ) -> bool:
        rows = self.select_rows(source, [Const(1)], where=where, joins=joins, limit=1, as_dict=False)
        return bool(rows)

    def aggregate(
        self,
        source: Source,
        aggregates: Mapping[str, Agg],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        group_by: Sequence[str] = (),
        order_by: Sequence[str] = (),
        as_dict: bool | None = None,
    ) -> list:
        cols: dict[str, str | Agg | Const] = {col: col for col in group_by}
        cols.update(aggregates)
        return self.select_rows(
            source,
            cols,
            where=where,
            joins=joins,
            order_by=order_by,
            as_dict=as_dict,
        )

    def max_value(self, source: Source, column: str, where: Where | None = None, *, default: Any = 0) -> Any:
        agg = Agg("MAX", column)
        rows = self.select_rows(source, [agg], where=where, as_dict=False)
        return rows[0][0] if rows and rows[0] and rows[0][0] is not None else default

    def min_value(self, source: Source, column: str, where: Where | None = None, *, default: Any = 0) -> Any:
        agg = Agg("MIN", column)
        rows = self.select_rows(source, [agg], where=where, as_dict=False)
        return rows[0][0] if rows and rows[0] and rows[0][0] is not None else default

    # -------------------------------------------------------------------------
    # Spec Write Primitives (§3)
    # -------------------------------------------------------------------------
    def insert_rows(
        self,
        table: Table | str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]] | Any,
        *,
        on_conflict: Literal["error", "ignore", "update", "replace"] = "error",
        update_columns: Sequence[str] | None = None,
        chunk_size: int | None = None,
    ) -> int:
        """Insert row(s) into SQLite table. Returns total affected rows."""
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        clean_cols = [validate_ident(c) for c in columns]
        if not clean_cols:
            raise ValueError("insert_rows requires at least one column")

        max_batch_rows = chunk_size or 500
        total_affected = 0
        current_batch: list[tuple[Any, ...]] = []

        conn = self._get_connection()

        def _exec_sqlite_batch(batch: list[tuple[Any, ...]]) -> int:
            if not batch:
                return 0
            sql = render_insert(
                tbl_name,
                clean_cols,
                len(batch),
                on_conflict=on_conflict,
                update_columns=update_columns,
                dialect="sqlite",
            )
            flat_params = [v for r in batch for v in r]
            with self._lock:
                try:
                    cur = conn.execute(sql, flat_params)
                    rc = cur.rowcount
                    if self.autocommit and not self._in_txn:
                        conn.commit()
                    return max(0, rc)
                except sqlite3.OperationalError as e:
                    if "no such table" in str(e).lower():
                        raise MissingTableError(f"Table '{tbl_name}' does not exist: {e}") from e
                    raise StoreError(f"Insert error: {e}") from e
                except sqlite3.Error as e:
                    raise StoreError(f"Database error: {e}") from e

        for r in rows:
            current_batch.append(tuple(r))
            if len(current_batch) >= max_batch_rows:
                total_affected += _exec_sqlite_batch(current_batch)
                current_batch = []

        if current_batch:
            total_affected += _exec_sqlite_batch(current_batch)

        return total_affected

    def update_rows(
        self,
        table: Table | str,
        values: Mapping[str, Any],
        where: Where | None,
        *,
        limit: int | None = None,
    ) -> int:
        """Update rows in SQLite table."""
        if _has_empty_in(where):
            return 0
        term_idx, target_col, oversize = _find_oversize_in(where, chunk_size=500)
        if oversize is not None and target_col is not None:
            if limit is not None:
                raise ValueError("Auto-chunking cannot be combined with limit")
            total = 0
            for i in range(0, len(oversize), 500):
                chunk = oversize[i : i + 500]
                total += self.update_rows(table, values, _replace_in(where, term_idx, target_col, chunk))
            return total

        sql, params = render_update(table, values, where, limit=limit, dialect="sqlite")
        conn = self._get_connection()
        with self._lock:
            try:
                cur = conn.execute(sql, tuple(params))
                rc = cur.rowcount
                if self.autocommit and not self._in_txn:
                    conn.commit()
                return max(0, rc)
            except sqlite3.OperationalError as e:
                if "no such table" in str(e).lower():
                    raise MissingTableError(f"Table not found: {e}") from e
                raise StoreError(f"Update error: {e}") from e
            except sqlite3.Error as e:
                raise StoreError(f"Database error: {e}") from e

    def delete_rows(
        self,
        table: Table | str,
        where: Where | None,
        *,
        limit: int | None = None,
        all_rows: bool = False,
    ) -> int:
        """Delete rows in SQLite table."""
        if where is None and not all_rows:
            raise ValueError("delete_rows without where requires all_rows=True")
        if _has_empty_in(where):
            return 0
        term_idx, target_col, oversize = _find_oversize_in(where, chunk_size=500)
        if oversize is not None and target_col is not None:
            if limit is not None:
                raise ValueError("Auto-chunking cannot be combined with limit")
            total = 0
            for i in range(0, len(oversize), 500):
                chunk = oversize[i : i + 500]
                total += self.delete_rows(table, _replace_in(where, term_idx, target_col, chunk), all_rows=all_rows)
            return total

        sql, params = render_delete(table, where, limit=limit, all_rows=all_rows, dialect="sqlite")
        conn = self._get_connection()
        with self._lock:
            try:
                cur = conn.execute(sql, tuple(params))
                rc = cur.rowcount
                if self.autocommit and not self._in_txn:
                    conn.commit()
                return max(0, rc)
            except sqlite3.OperationalError as e:
                if "no such table" in str(e).lower():
                    raise MissingTableError(f"Table not found: {e}") from e
                raise StoreError(f"Delete error: {e}") from e
            except sqlite3.Error as e:
                raise StoreError(f"Database error: {e}") from e

    def get_or_insert_id(
        self,
        table: Table | str,
        key: Mapping[str, Any],
        values: Mapping[str, Any] | None = None,
    ) -> int:
        """Fetch primary key ID by unique key; insert if missing."""
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        pk_col = getattr(table, "init_columns", [("id",)])[0][0] if hasattr(table, "init_columns") else "rowid"
        row = self.select_one(tbl_name, [pk_col], where=key)
        if row is not None:
            val = row[0] if isinstance(row, tuple) else row[pk_col]
            return int(val)
        row_dict = dict(key)
        if values:
            row_dict.update(values)
        cols = list(row_dict.keys())
        self.insert_rows(tbl_name, cols, [tuple(row_dict[c] for c in cols)], on_conflict="ignore")
        row = self.select_one(tbl_name, [pk_col], where=key)
        if row is not None:
            val = row[0] if isinstance(row, tuple) else row[pk_col]
            return int(val)
        raise StoreError(f"Failed to get_or_insert_id for {tbl_name} with key {key}")


class SQLiteSession:
    """Wrapper around a temporary SQLite connection with attached schemas."""

    def __init__(self, conn: sqlite3.Connection, store: SQLiteStore) -> None:
        self.conn = conn
        self.store = store

    def select_rows(
        self,
        source: Source,
        columns: Sequence[str | Agg | Const] | Mapping[str, str | Agg | Const],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        limit: int | None = None,
        offset: int | None = None,
        as_dict: bool | None = None,
    ) -> list[tuple | dict]:
        select_spec = Select(
            source=source,
            columns=columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )
        sql, params, col_names = render_select(select_spec, dialect="sqlite")
        try:
            cur = self.conn.execute(sql, tuple(params))
            raw_rows = cur.fetchall()
            return _format_rows(raw_rows, col_names, bool(as_dict))
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                raise MissingTableError(f"Table not found: {e}") from e
            raise StoreError(f"Session query error: {e}") from e
        except sqlite3.Error as e:
            raise StoreError(f"Session database error: {e}") from e

    def select_values(
        self,
        source: Source,
        column: str,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        limit: int | None = None,
    ) -> list:
        rows = self.select_rows(
            source,
            [column],
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            as_dict=False,
        )
        return [r[0] for r in rows]

    def select_one(
        self,
        source: Source,
        columns: Sequence[str | Agg | Const] | Mapping[str, str | Agg | Const],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        as_dict: bool | None = None,
    ) -> tuple | dict | None:
        rows = self.select_rows(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=1,
            as_dict=as_dict,
        )
        return rows[0] if rows else None


# -----------------------------------------------------------------------------
# Internal Helpers
# -----------------------------------------------------------------------------
def _has_empty_in(where: Where | None) -> bool:
    if where is None:
        return False
    if isinstance(where, Mapping):
        for val in where.values():
            if isinstance(val, (list, tuple, set, frozenset)) and len(val) == 0:
                return True
    elif isinstance(where, Sequence):
        for term in where:
            if isinstance(term, Mapping):
                for val in term.values():
                    if isinstance(val, (list, tuple, set, frozenset)) and len(val) == 0:
                        return True
    return False


def _find_oversize_in(
    where: Where | None,
    chunk_size: int = 500,
) -> tuple[int | None, str | None, list[Any] | None]:
    if where is None:
        return None, None, None
    oversize = []
    if isinstance(where, Mapping):
        for col, val in where.items():
            if isinstance(val, (list, tuple, set, frozenset)) and len(val) > chunk_size:
                oversize.append((None, col, list(val)))
    elif isinstance(where, Sequence):
        for idx, term in enumerate(where):
            if isinstance(term, Mapping):
                for col, val in term.items():
                    if isinstance(val, (list, tuple, set, frozenset)) and len(val) > chunk_size:
                        oversize.append((idx, col, list(val)))
    if len(oversize) > 1:
        raise ValueError(f"Auto-chunking supports only one oversize IN list, found {len(oversize)}")
    if len(oversize) == 1:
        return oversize[0]
    return None, None, None


def _replace_in(
    where: Where | None,
    target_term_idx: int | None,
    target_col: str,
    sub_list: list[Any],
) -> Where:
    if where is None:
        return {target_col: sub_list}
    if isinstance(where, Mapping):
        new_w = dict(where)
        new_w[target_col] = sub_list
        return new_w
    new_terms = []
    for idx, term in enumerate(where):
        if idx == target_term_idx and isinstance(term, Mapping):
            new_t = dict(term)
            new_t[target_col] = sub_list
            new_terms.append(new_t)
        else:
            new_terms.append(term)
    return new_terms


def _format_rows(raw_rows: list[Any], col_names: list[str], as_dict: bool) -> list[Any]:
    formatted = []
    for r in raw_rows:
        if as_dict:
            row_dict = {}
            for idx, c in enumerate(col_names):
                val = r[idx]
                if isinstance(val, (bytearray, memoryview)):
                    val = bytes(val)
                row_dict[c] = val
            formatted.append(row_dict)
        else:
            t_vals = []
            for val in r:
                if isinstance(val, (bytearray, memoryview)):
                    val = bytes(val)
                t_vals.append(val)
            formatted.append(tuple(t_vals))
    return formatted


def _dedup_list(rows: list[Any], as_dict: bool) -> list[Any]:
    seen = set()
    deduped = []
    for r in rows:
        if as_dict and isinstance(r, dict):
            k = tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in r.items()))
        else:
            k = r
        if k not in seen:
            seen.add(k)
            deduped.append(r)
    return deduped


def _sort_list(
    rows: list[Any],
    order_by: Sequence[str],
    col_names: list[str],
    as_dict: bool,
) -> list[Any]:
    if not order_by or not rows:
        return rows
    result = list(rows)
    for order_spec in reversed(order_by):
        clean = order_spec.strip()
        desc = clean.startswith("-")
        col = clean[1:] if desc else clean
        base_col = col.rsplit(".", 1)[-1]

        def _get_sort_val(r: Any) -> Any:
            if as_dict and isinstance(r, dict):
                return r.get(col, r.get(base_col))
            else:
                if col in col_names:
                    idx = col_names.index(col)
                elif base_col in col_names:
                    idx = col_names.index(base_col)
                else:
                    return None
                return r[idx] if idx < len(r) else None

        result.sort(key=lambda r: (_get_sort_val(r) is None, _get_sort_val(r)), reverse=desc)
    return result
