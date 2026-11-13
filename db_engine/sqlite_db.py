"""db_engine/sqlite_db.py - SQLite implementation of `BaseDBEngine`.

`SQLiteDB` keeps the whole relational schema of `core/DBLayout.py` in one SQLite database (a file, or
`":memory:"`): DDL is rendered from the neutral column specs (`sql_render.render_create_table_statements`,
`dialect="sqlite"`), reads and writes go through the same spec renderers as MariaDB, and every `sqlite3` error
leaves the class as a neutral `db_engine.errors` exception.

Connection model: one connection per instance (`isolation_level=None`, i.e. autocommit unless `transaction()`
holds a `BEGIN`), guarded by an `RLock` so an instance may be shared by threads. File databases run in WAL mode,
so any number of read-only sessions (`reader()`) work while a writer is active. A `":memory:"` database is private
to its instance; `":memory:shared"` opens a named shared-cache in-memory database several instances can see.
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, ContextManager, Literal, Mapping, Self

from core.globalstuff import JoinsType, PointerGetter, PointerType, SafeDataType
from db_engine.base import (
    CAP_PERSISTENT,
    BaseDBEngine,
    ColumnInfo,
    NewPairs,
    ProbeResult,
    TableCommitStat,
    TableStats,
    _hybridmethod,
    estimate_column_bytes,
)
from db_engine.errors import (
    DBError,
    ReadOnlyViolation,
    UnsupportedOperation,
    translate_sqlite_error,
)
from db_engine.read_intents import ReadIntents
from db_engine.spec import (
    Agg,
    Const,
    Join,
    Select,
    Source,
    Where,
    dedup_rows,
    find_oversize_in_term,
    has_empty_in_term,
    replace_in_term,
)
from db_engine.sql_render import (
    render_column_type,
    render_copy,
    render_create_table_statements,
    render_delete,
    render_drop_tables,
    render_insert_with_params,
    render_select,
    render_update,
    validate_ident,
)
from db_engine.sqlite_store import _sort_list

if TYPE_CHECKING:
    from core.TableHandling import Table

MEMORY = ":memory:"
SHARED_MEMORY = ":memory:shared"
_SHARED_MEMORY_URI = "file:kernelinfo_shared_memory?mode=memory&cache=shared"
_IN_CHUNK = 1000
_MAX_VARIABLES = 30000  # SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 32766
_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")

_BINARY_TYPES_FOR_OCTETS = ("BINARY", "VARBINARY")


def sqlite_path_from_config(cfg: Mapping[str, Any] | None = None) -> str:
    """Database path of the `database` config section `cfg` (the environment when None).

    Order: `path` (`DB_PATH`), then `database` (`DB_NAME`) taken as a path when it names a file or a directory
    (a suffix `.db` / `.sqlite` / `.sqlite3`, a path separator, or `:memory:`); `":memory:"` when neither is set.
    A bare database name (the MariaDB schema name) is not a file location: it raises `ValueError`, so no database
    file is ever created implicitly in the working directory.
    """
    if cfg is None:
        path = os.environ.get("DB_PATH", "")
        name = os.environ.get("DB_NAME", "")
    else:
        path = str(cfg.get("path") or "")
        name = str(cfg.get("database") or "")
    if path:
        return path
    if not name:
        return MEMORY
    if name.startswith(":memory:") or name.lower().endswith(_SQLITE_SUFFIXES) or os.sep in name or "/" in name:
        return name
    raise ValueError(f"SQLite needs an explicit database file: set database.path (database name {name!r} is not a path)")


def _to_qmark(sql: str) -> str:
    """Rewrite `%s` placeholders (and `%%`) outside quoted text to SQLite's `?` / `%`."""
    if "%" not in sql:
        return sql
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
        elif ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
        elif ch == "%" and sql.startswith("%s", i):
            out.append("?")
            i += 1
        elif ch == "%" and sql.startswith("%%", i):
            out.append("%")
            i += 1
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _table_name(table: Table | str) -> str:
    return table.table_name if hasattr(table, "table_name") else str(table)


def _version_predicate(table: Table) -> str | None:
    """`WHERE` fragment (one `?`) restricting `table` to the versions from one `min_vid` on, or None."""
    names = [col[0] for col in table.init_columns]
    if "vid_s" in names and "vid_e" in names:
        return "(vid_e = 0 OR vid_e >= ?)"
    if "vid" in names:
        return "vid >= ?"
    if "vid_s" in names:
        return "vid_s >= ?"
    if "commit_id" in names:
        return "commit_id >= ?"
    return None


class _Cursor:
    """Minimal DB-API-style cursor over the owning `SQLiteDB` (`%s` placeholders, serialized by its lock)."""

    def __init__(self, db: SQLiteDB, dictionary: bool = False) -> None:
        self._db = db
        self._dictionary = dictionary
        self._rows: list[Any] = []
        self._pos = 0
        self.rowcount = -1
        self.description: list[tuple[str, ...]] | None = None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        rows, rc, names = self._db._run(_to_qmark(sql), params or ())
        self.rowcount = rc
        self.description = [(n,) for n in names] if names else None
        self._rows = [dict(zip(names, r)) for r in rows] if self._dictionary and names else rows
        self._pos = 0

    def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        _, rc, _ = self._db._run(_to_qmark(sql), params_seq, many=True)
        self.rowcount = rc
        self._rows, self._pos = [], 0

    def fetchone(self) -> Any:
        if self._pos >= len(self._rows):
            return None
        self._pos += 1
        return self._rows[self._pos - 1]

    def fetchall(self) -> list[Any]:
        rows, self._pos = self._rows[self._pos:], len(self._rows)
        return rows

    def fetchmany(self, size: int = 1) -> list[Any]:
        rows = self._rows[self._pos:self._pos + size]
        self._pos += len(rows)
        return rows

    def close(self) -> None:
        self._rows = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class SQLiteDB(BaseDBEngine):
    """`BaseDBEngine` over a SQLite file (or in-memory database); see the module docstring."""

    dialect = "sqlite"
    display_name = "SQLite"
    base_capabilities = frozenset({CAP_PERSISTENT})

    _reader_lock = threading.Lock()
    _reader_db: SQLiteDB | None = None
    _keepalive: dict[str, sqlite3.Connection] = {}

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        read_only: bool = False,
        foreign_keys: bool = True,
        timeout: float = 30.0,
    ) -> None:
        """Open the database at `path` (default: `database.path` / `DB_PATH`, else `DB_NAME`, else `":memory:"`).

        `read_only=True` opens a file with `mode=ro` plus `PRAGMA query_only`, and every write raises
        `ReadOnlyViolation`. `foreign_keys` turns SQLite's FOREIGN KEY enforcement on (it is off by default there).
        """
        self.path = str(path) if path is not None else sqlite_path_from_config()
        self.read_only = read_only
        self.foreign_keys = foreign_keys
        self.timeout = timeout
        self.memory = self.path.startswith(":memory:")
        self._lock = threading.RLock()
        self._txn_depth = 0
        self._in_txn = False
        self._conn: sqlite3.Connection | None = self._connect()

    # ------------------------------------------------------------------ connection
    def _connect(self) -> sqlite3.Connection:
        if self.path == SHARED_MEMORY:
            with SQLiteDB._reader_lock:
                SQLiteDB._keepalive.setdefault(
                    self.path, sqlite3.connect(_SHARED_MEMORY_URI, uri=True, isolation_level=None, check_same_thread=False)
                )
            conn = sqlite3.connect(
                _SHARED_MEMORY_URI, uri=True, timeout=self.timeout, isolation_level=None, check_same_thread=False
            )
        elif self.memory:
            conn = sqlite3.connect(MEMORY, isolation_level=None, check_same_thread=False)
        elif self.read_only:
            uri = f"file:{Path(self.path).resolve().as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=self.timeout, isolation_level=None, check_same_thread=False)
        else:
            parent = Path(self.path).resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None, check_same_thread=False)
        pragmas = [f"busy_timeout = {int(self.timeout * 1000)}", "case_sensitive_like = ON"]
        if not self.read_only and not self.memory:
            pragmas += ["journal_mode = WAL", "synchronous = NORMAL"]
        if self.read_only:
            pragmas.append("query_only = ON")
        pragmas.append(f"foreign_keys = {'ON' if self.foreign_keys else 'OFF'}")
        for pragma in pragmas:
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"PRAGMA {pragma}")
        return conn

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise translate_sqlite_error(sqlite3.ProgrammingError("Cannot operate on a closed database.")) or DBError(
                "database is closed"
            )
        return self._conn

    @property
    def capabilities(self) -> frozenset[str]:
        caps = set(super().capabilities)
        if self.memory:
            caps.discard(CAP_PERSISTENT)
        return frozenset(caps)

    def _require_writable(self) -> None:
        if self.read_only:
            raise ReadOnlyViolation(
                "This database session is read-only; write through the writer engine (SQLiteDB without read_only) "
                "- see DB_API.md 'Webapp Read Session & Query Primitives'"
            )

    def _run(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        many: bool = False,
    ) -> tuple[list[tuple[Any, ...]], int, list[str]]:
        """Execute one statement (or `executemany`); returns `(rows, rowcount, column names)`."""
        with self._lock:
            conn = self._connection()
            try:
                if many:
                    try:
                        cur = conn.executemany(sql, [tuple(p) for p in params])
                    except sqlite3.ProgrammingError as exc:
                        if "only execute DML" not in str(exc):
                            raise
                        total = 0
                        for p in params:  # e.g. a SELECT: run it once per parameter set
                            one = conn.execute(sql, tuple(p))
                            one.fetchall()
                            total += max(0, one.rowcount)
                        return [], total, []
                else:
                    cur = conn.execute(sql, tuple(params))
                names = [d[0] for d in cur.description] if cur.description else []
                rows = [tuple(r) for r in cur.fetchall()] if names else []
                return rows, cur.rowcount, names
            except sqlite3.Error as exc:
                neutral = translate_sqlite_error(exc)
                raise (neutral if neutral is not None else DBError(str(exc))) from exc

    def _write(self, sql: str, params: Sequence[Any] = (), *, many: bool = False) -> int:
        self._require_writable()
        return max(0, self._run(sql, params, many=many)[1])

    def _scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        rows = self._run(sql, params)[0]
        return rows[0][0] if rows else None

    def __enter__(self) -> Self:
        self._connection()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        if exception_type is not None:
            self.rollback()

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()

    # ------------------------------------------------------------------ transactions
    def commit(self) -> None:
        """Commit the open transaction (a no-op in autocommit mode); inside `transaction()` a new one follows."""
        with self._lock:
            conn = self._conn
            if conn is not None and conn.in_transaction:
                conn.execute("COMMIT")
                if self._in_txn:
                    conn.execute("BEGIN IMMEDIATE" if not self.read_only else "BEGIN")

    def rollback(self) -> None:
        """Roll the open transaction back (a no-op in autocommit mode); inside `transaction()` a new one follows."""
        with self._lock:
            conn = self._conn
            if conn is not None and conn.in_transaction:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                if self._in_txn:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("BEGIN IMMEDIATE" if not self.read_only else "BEGIN")

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Self]:
        """Run the block as one transaction (outermost block commits, an error rolls everything back)."""
        with self._lock:
            conn = self._connection()
            outer = self._txn_depth == 0
            if outer:
                try:
                    conn.execute("BEGIN IMMEDIATE" if not self.read_only else "BEGIN")
                except sqlite3.Error as exc:
                    raise (translate_sqlite_error(exc) or DBError(str(exc))) from exc
                self._in_txn = True
            self._txn_depth += 1
            try:
                yield self
                if outer and conn.in_transaction:
                    conn.execute("COMMIT")
            except BaseException:
                if outer and conn.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                raise
            finally:
                self._txn_depth -= 1
                if outer:
                    self._in_txn = False

    @contextlib.contextmanager
    def _foreign_keys_off(self) -> Iterator[None]:
        """Suspend FOREIGN KEY enforcement (no effect inside a transaction) and restore it afterwards."""
        with self._lock:
            conn = self._connection()
            previous = conn.execute("PRAGMA foreign_keys").fetchone()
            was_on = bool(previous and previous[0])
            if was_on and not conn.in_transaction:
                conn.execute("PRAGMA foreign_keys = OFF")
            try:
                yield
            finally:
                if was_on and not conn.in_transaction:
                    conn.execute("PRAGMA foreign_keys = ON")

    # ------------------------------------------------------------------ engine-level hooks
    @classmethod
    def probe_connection(cls, cfg: Mapping[str, Any]) -> ProbeResult:
        """Open the configured database and run `SELECT sqlite_version()`; never raises.

        A file that does not exist yet is not created: it probes as reachable when its directory exists.
        """
        t0 = time.perf_counter()
        path = str(cfg.get("path") or cfg.get("database") or "")
        try:
            path = sqlite_path_from_config(cfg)
            timeout = float(cfg.get("timeout", 3) or 3)
            if path.startswith(":memory:"):
                conn = sqlite3.connect(MEMORY, timeout=timeout)
            elif Path(path).is_file():
                conn = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True, timeout=timeout)
            else:
                parent = Path(path).resolve().parent
                if not parent.is_dir():
                    raise FileNotFoundError(f"directory {parent} does not exist")
                conn = sqlite3.connect(MEMORY, timeout=timeout)
            try:
                version = str(conn.execute("SELECT sqlite_version()").fetchone()[0])
            finally:
                conn.close()
        except Exception as exc:
            return ProbeResult(False, latency_ms=round((time.perf_counter() - t0) * 1000.0, 2), error=str(exc), target=path)
        return ProbeResult(True, version, round((time.perf_counter() - t0) * 1000.0, 2), "", path)

    @_hybridmethod
    def reader(self) -> ContextManager[Any]:
        """Read-only session: on an instance a fresh `mode=ro` connection to the same file (the instance itself
        for an in-memory database); on the class the process-wide reader of the configured path."""
        if isinstance(self, SQLiteDB):
            if self.memory:
                return SQLiteReadSession(self, owned=False)
            return SQLiteReadSession(SQLiteDB(self.path, read_only=True, timeout=self.timeout), owned=True)
        return SQLiteReadSession(SQLiteDB._shared_reader(), owned=False)

    @classmethod
    def _shared_reader(cls) -> SQLiteDB:
        with cls._reader_lock:
            if cls._reader_db is None or cls._reader_db._conn is None:
                path = sqlite_path_from_config()
                if path == MEMORY:
                    raise UnsupportedOperation("A private in-memory SQLite database has no shared reader; use a file")
                cls._reader_db = cls(path, read_only=True)
            return cls._reader_db

    @classmethod
    def warm_up(cls) -> None:
        """Open the shared reader connection of the configured database."""
        cls._shared_reader()

    @classmethod
    def shutdown(cls) -> None:
        """Close the shared reader connection."""
        with cls._reader_lock:
            db, cls._reader_db = cls._reader_db, None
        if db is not None:
            db.close()

    # ------------------------------------------------------------------ private raw SQL
    def _raw_cursor(self, dictionary: bool = False) -> Any:
        self._connection()
        return _Cursor(self, dictionary)

    def _raw_query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> list[dict[str, Any] | tuple[SafeDataType, ...]]:
        rows, _, names = self._run(_to_qmark(sql), params or ())
        return [dict(zip(names, r)) for r in rows] if dictionary else rows

    def _raw_query_iter(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
        chunk_size: int = 2000,
    ) -> Iterator[dict[str, Any] | tuple[SafeDataType, ...]]:
        yield from self._raw_query(sql, params, dictionary=dictionary)

    def _raw_execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        return self._write(_to_qmark(sql), params or ())

    def _raw_executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
        if not params_seq:
            return 0
        return self._write(_to_qmark(sql), params_seq, many=True)

    # ------------------------------------------------------------------ catalog
    def table_exists(self, table_name: str) -> bool:
        return bool(self._scalar("SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (table_name,)))

    def test_tables(self, tables: Sequence[Table] | Table) -> list[str] | None:
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        existing = {r[0] for r in self._run("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")[0]}
        missing = [t.table_name for t in tables if t.table_name not in existing]
        if missing:
            print(f"We are missing these tables: {missing}")
            return missing
        return None

    def existing_tables(self, names: Sequence[str], *, include_views: bool = False) -> set[str]:
        if not names:
            return set()
        kinds = "'table', 'view'" if include_views else "'table'"
        found: set[str] = set()
        for start in range(0, len(names), _IN_CHUNK):
            chunk = list(names[start:start + _IN_CHUNK])
            sql = f"SELECT name FROM sqlite_master WHERE type IN ({kinds}) AND name IN ({','.join('?' * len(chunk))})"
            found.update(str(r[0]) for r in self._run(sql, chunk)[0])
        return found

    def _pragma_columns(self, table_name: str) -> list[tuple[Any, ...]]:
        validate_ident(table_name)
        return self._run(f"PRAGMA table_info({table_name})")[0]

    def column_info(self, table: Table | str, column: str) -> ColumnInfo | None:
        tbl_name = _table_name(table)
        for _cid, name, decl, notnull, _default, _pk in self._pragma_columns(tbl_name):
            if name != column:
                continue
            col_type = str(decl or "").upper()
            octets = None
            schema = _schema_spec(tbl_name, column)
            if schema is not None:
                col_type = render_column_type(schema, "mariadb")
                if schema.type.value in _BINARY_TYPES_FOR_OCTETS:
                    octets = schema.length
            return ColumnInfo(
                data_type=col_type.split("(")[0].split()[0].lower() if col_type else "",
                column_type=col_type,
                octet_length=octets,
                nullable=not notnull,
            )
        return None

    def index_names(self, table: Table | str) -> set[str]:
        tbl_name = _table_name(table)
        validate_ident(tbl_name)
        names = {str(r[1]) for r in self._run(f"PRAGMA index_list({tbl_name})")[0] if not str(r[1]).startswith("sqlite_autoindex_")}
        if any(r[5] for r in self._pragma_columns(tbl_name)):
            names.add("PRIMARY")
        return names

    def _stored_bytes(self, name: str) -> tuple[int, int]:
        """`(data_bytes, index_bytes)` of a table from the `dbstat` virtual table (0, 0 when it is not compiled in)."""
        try:
            data = int(self._scalar("SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name = ?", (name,)) or 0)
            index = 0
            for (idx,) in self._run(f"SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (name,))[0]:
                index += int(self._scalar("SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name = ?", (idx,)) or 0)
            return data, index
        except DBError:
            return 0, 0

    def table_stats(self, names: Sequence[str] | None = None) -> list[TableStats]:
        base = [str(r[0]) for r in self._run("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")[0]]
        wanted = [n for n in base if names is None or not names or n in set(names)]
        stats: list[TableStats] = []
        for name in wanted:
            validate_ident(name)
            rows = int(self._scalar(f"SELECT COUNT(*) FROM {name}") or 0)
            data_bytes, index_bytes = self._stored_bytes(name)
            stats.append(TableStats(name=name, rows=rows, data_bytes=data_bytes, index_bytes=index_bytes))
        stats.sort(key=lambda s: s.data_bytes + s.index_bytes, reverse=True)
        return stats

    def server_variable(self, name: str) -> str | None:
        return None

    def table_size(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> int:
        """Stored bytes (`dbstat`) scaled to the column projection and version window; 0 when unknown."""
        if not self.table_exists(table.table_name):
            return 0
        data, index = self._stored_bytes(table.table_name)
        stored, total_rows = data + index, int(self._scalar(f"SELECT COUNT(*) FROM {table.table_name}") or 0)
        if not stored:
            return 0
        rows = total_rows
        predicate = _version_predicate(table) if (min_vid is not None and min_vid > 0) else None
        if predicate is not None and total_rows > 0:
            rows = int(self._scalar(f"SELECT COUNT(*) FROM {table.table_name} WHERE {predicate}", (min_vid,)) or 0)
            stored = min(stored, int(stored * rows / max(total_rows, 1)))
        if not cached_columns or len(cached_columns) >= table.length:
            return stored
        projected = rows * sum(estimate_column_bytes(table.init_columns[i]) for i in cached_columns)
        return min(stored, projected)

    # ------------------------------------------------------------------ DDL
    def create_table(self, tables: Sequence[Table] | Table) -> None:
        self._require_writable()
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        with self._foreign_keys_off():
            for table in tables:
                for stmt in render_create_table_statements(table, "sqlite"):
                    self._run(stmt)
                if table.initial_insert:
                    self.insert(table, table.initial_insert)

    def drop_table(self, tables: Sequence[Table] | Table) -> None:
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        self.drop_tables([t.table_name for t in tables], ignore_fk=True)

    def drop_tables(self, names: Sequence[str], *, ignore_fk: bool = False, if_exists: bool = True) -> None:
        self._require_writable()
        if not names:
            return
        statements = render_drop_tables(names, if_exists=if_exists, dialect="sqlite")
        ctx = self._foreign_keys_off() if ignore_fk else contextlib.nullcontext()
        with ctx:
            for stmt in statements:
                self._run(stmt)

    def index_exists(self, index_name: str, table: Table) -> bool:
        return bool(
            self._scalar("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ? AND tbl_name = ?", (index_name, table.table_name))
        )

    def create_index(self, index_name: str, table: Table, rows: tuple[PointerType, ...]) -> None:
        self._require_writable()
        validate_ident(index_name)
        cols = ", ".join(validate_ident(table.init_columns[x[1]][0]) for x in rows)
        self._run(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table.table_name} ({cols})")

    def remove_index(self, index_name: str, table: Table) -> None:
        self._require_writable()
        validate_ident(index_name)
        self._run(f"DROP INDEX IF EXISTS {index_name}")

    def create_indexes(
        self,
        indexes: Sequence[tuple[str, Table, tuple[PointerType, ...]]],
        max_workers: int | None = None,
    ) -> None:
        for name, table, rows in indexes:
            self.create_index(name, table, rows)

    def remove_indexes(self, indexes: Sequence[tuple[str, Table]], max_workers: int | None = None) -> None:
        for name, table in indexes:
            self.remove_index(name, table)

    def verify_relational_integrity(self, tables: Sequence[Table]) -> dict[str, int]:
        violations: dict[str, int] = {}
        for tbl in tables:
            for local_col, foreign_tbl, foreign_col in getattr(tbl, "init_foreign", None) or ():
                sql = (
                    f"SELECT COUNT(*) FROM {tbl.table_name} child LEFT JOIN {foreign_tbl} parent "
                    f"ON child.{local_col} = parent.{foreign_col} "
                    f"WHERE child.{local_col} IS NOT NULL AND parent.{foreign_col} IS NULL"
                )
                try:
                    count = int(self._scalar(sql) or 0)
                except DBError:
                    continue
                if count > 0:
                    violations[f"{tbl.table_name}.{local_col} -> {foreign_tbl}.{foreign_col}"] = count
        return violations

    # ------------------------------------------------------------------ Table-API reads / writes (TableEngine)
    def get_next_id(self, table: Table) -> int:
        pk = table.init_columns[0][0]
        return int(self._scalar(f"SELECT COALESCE(MAX({pk}), 0) + 1 FROM {table.table_name}") or 1)

    def reserve_id_block(self, table: Table, block_size: int, commit_id: int = 0) -> tuple[int, int]:
        """Atomically reserve `[start, end]` ids from `m_table_sequence` (seeded from the table's maximum)."""
        table_id = getattr(table, "table_id", None)
        if table_id is None or not self.table_exists("m_table_sequence"):
            return super().reserve_id_block(table, block_size, commit_id)
        pk = table.init_columns[0][0]
        with self.transaction():
            self._run(
                "INSERT INTO m_table_sequence (seq_table_id, next_id) "
                f"SELECT ?, COALESCE(MAX({pk}), 0) + 1 FROM {table.table_name} WHERE true "
                "ON CONFLICT (seq_table_id) DO UPDATE SET next_id = MAX(next_id, excluded.next_id)",
                (table_id,),
            )
            start = int(self._scalar("SELECT next_id FROM m_table_sequence WHERE seq_table_id = ?", (table_id,)) or 1)
            end = start + block_size - 1
            self._run("UPDATE m_table_sequence SET next_id = ? WHERE seq_table_id = ?", (end + 1, table_id))
            if commit_id > 0 and self.table_exists("m_ingest_id_range"):
                self._run(
                    "INSERT INTO m_ingest_id_range (commit_id, marked_table_id, start_id, end_id) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT DO UPDATE SET end_id = MAX(end_id, excluded.end_id)",
                    (commit_id, table_id, start, end),
                )
        return (start, end)

    def select(self, table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None:
        clauses = [f"{table.init_columns[i][0]} = ?" for i, v in enumerate(data) if v is not None]
        sql = f"SELECT * FROM {table.table_name}" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " LIMIT 1"
        rows = self._run(sql, [v for v in data if v is not None])[0]
        return rows[0] if rows else None

    @staticmethod
    def _view_query(
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[str, list[Any]]:
        initial = PointerGetter(joins).get_first_pointer()
        first = tables[initial[0]]
        sql = f"SELECT * FROM {first.table_name} AS A1"
        clauses: list[str] = []
        offset = 0
        for i, col in enumerate(first.init_columns):
            if columns[i] is not None:
                clauses.append(f"A1.{col[0]} = ?")
            offset += 1
        if len(joins[0]) > 1:
            aliases = {initial[0]: 1}
            alias = 1
            for join in joins:
                for rep in range(join[2]):
                    alias += 1
                    if rep == 0:
                        aliases[join[1][0]] = alias
                    src, tgt = tables[join[0][0]], tables[join[1][0]]
                    sql += (
                        f" JOIN {tgt.table_name} A{alias} ON A{aliases[join[0][0]]}.{src.init_columns[join[0][1]][0]}"
                        f" = A{alias}.{tgt.init_columns[join[1][1]][0]}"
                    )
                    for col in tgt.init_columns:
                        if offset < len(columns) and columns[offset] is not None:
                            clauses.append(f"A{alias}.{col[0]} = ?")
                        offset += 1
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return sql, [v for v in columns if v is not None]

    def view_select(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        sql, params = self._view_query(tables, joins, columns)
        rows = self._run(sql + " LIMIT 1", params)[0]
        return rows[0] if rows else None

    def view_select_multiple(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> list[tuple[SafeDataType, ...]]:
        sql, params = self._view_query(tables, joins, columns)
        return self._run(sql, params)[0]

    def select_in(self, table: Table, col_idx: int, values: Sequence[SafeDataType]) -> list[tuple[SafeDataType, ...]]:
        uniq = list(dict.fromkeys(v for v in values if v is not None))
        col = table.init_columns[col_idx][0]
        rows: list[tuple[SafeDataType, ...]] = []
        for start in range(0, len(uniq), _IN_CHUNK):
            chunk = uniq[start:start + _IN_CHUNK]
            rows.extend(self._run(f"SELECT * FROM {table.table_name} WHERE {col} IN ({','.join('?' * len(chunk))})", chunk)[0])
        return rows

    def select_preload(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> list[tuple[SafeDataType, ...]]:
        if cached_columns is not None and len(cached_columns) < table.length:
            cols = ", ".join(table.init_columns[i][0] for i in cached_columns)
        else:
            cols = "*"
        sql = f"SELECT {cols} FROM {table.table_name}"
        params: tuple[Any, ...] = ()
        if min_vid is not None and min_vid > 0:
            predicate = _version_predicate(table)
            if predicate is not None:
                sql += f" WHERE {predicate}"
                params = (min_vid,)
        return self._run(sql, params)[0]

    @staticmethod
    def _batches(data: Any) -> list[tuple[Any, ...]]:
        if not data:
            return []
        if isinstance(data[0], (tuple, list)):
            return [tuple(r) for r in data]
        return [tuple(data)]

    def _chunked_write(self, sql_for: Any, ncols: int, rows: list[tuple[Any, ...]]) -> None:
        per = max(1, min(500, _MAX_VARIABLES // max(ncols, 1)))
        with self.transaction():
            for start in range(0, len(rows), per):
                batch = rows[start:start + per]
                sql, trailing = sql_for(len(batch))
                self._run(sql, [v for r in batch for v in r] + trailing)

    def insert(self, table: Table, data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]) -> None:
        """Insert rows; `idempotent_pk` tables skip rows whose primary key exists, others raise `DuplicateKeyError`."""
        self._require_writable()
        rows = self._batches(data)
        if not rows:
            return
        cols = [c[0] for c in table.init_columns]
        mode: Literal["error", "ignore"] = "ignore" if getattr(table, "idempotent_pk", False) and table.init_primary else "error"
        self._chunked_write(
            lambda n: render_insert_with_params(table, cols, n, on_conflict=mode, dialect="sqlite"),
            len(cols),
            rows,
        )

    def update(self, table: Table, data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]) -> None:
        """Upsert rows by primary key (every non-key column takes the offered value)."""
        self._require_writable()
        rows = self._batches(data)
        if not rows:
            return
        cols = [c[0] for c in table.init_columns]
        updatable = [c for i, c in enumerate(cols) if i not in table.primary]
        mode: Literal["ignore", "update"] = "update" if updatable else "ignore"
        self._chunked_write(
            lambda n: render_insert_with_params(table, cols, n, on_conflict=mode, update_columns=updatable or None, dialect="sqlite"),
            len(cols),
            rows,
        )

    def commit_tables_parallel(
        self,
        tables_data: Sequence[tuple[Table, Sequence[tuple[SafeDataType, ...]], Sequence[tuple[SafeDataType, ...]]]],
        max_workers: int | None = None,
        dedicated: bool = False,
    ) -> list[TableCommitStat]:
        """Write each table's inserts and updates atomically; SQLite serializes writers, so tables go in turn."""
        stats: list[TableCommitStat] = []
        failed: list[tuple[Table, BaseException]] = []
        for table, insert_data, update_data in tables_data:
            if not insert_data and not update_data:
                continue
            t0 = time.perf_counter()
            try:
                with self.transaction():
                    if insert_data:
                        self.insert(table, tuple(insert_data))
                    if update_data:
                        self.update(table, tuple(update_data))
            except Exception as exc:
                failed.append((table, exc))
                continue
            stats.append(TableCommitStat(table.table_name, len(insert_data), len(update_data), time.perf_counter() - t0))
        if failed:
            first = failed[0][1]
            first.failed_table_ids = {t.table_id for t, _ in failed}  # type: ignore[attr-defined]
            raise first
        return stats

    # ------------------------------------------------------------------ spec reads / writes
    def select_rows(
        self,
        source: Source,
        columns: Sequence[Any] | Mapping[str, Any],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[Any] = (),
        limit: int | None = None,
        offset: int | None = None,
        as_dict: bool | None = None,
        group_by: Sequence[Any] = (),
        having: Where | None = None,
        hint: Literal["straight_join"] | None = None,
    ) -> list[tuple | dict]:
        if has_empty_in_term(where):
            return []
        term_idx, target_col, oversize = find_oversize_in_term(where, chunk_size=_IN_CHUNK)
        if oversize is not None and target_col is not None and not group_by and having is None:
            if limit is not None or offset is not None:
                raise ValueError("Auto-chunking cannot be combined with limit/offset")
            merged: list[Any] = []
            for i in range(0, len(oversize), _IN_CHUNK):
                merged.extend(
                    self.select_rows(
                        source, columns, replace_in_term(where, term_idx, target_col, oversize[i:i + _IN_CHUNK]),
                        joins=joins, distinct=distinct, order_by=order_by, as_dict=as_dict, hint=hint,
                    )
                )
            if distinct:
                merged = dedup_rows(merged, bool(as_dict))
            if order_by:
                _, _, names = render_select(Select(source, columns, None, joins, distinct, order_by), dialect="sqlite")
                merged = _sort_list(merged, order_by, names, bool(as_dict))
            return merged
        spec = Select(
            source=source, columns=columns, where=where, joins=joins, distinct=distinct, order_by=order_by,
            limit=limit, offset=offset, group_by=group_by, having=having, hint=hint,
        )
        sql, params, names = render_select(spec, dialect="sqlite")
        rows = self._run(sql, params)[0]
        if as_dict:
            return [dict(zip(names, r)) for r in rows]
        return rows

    def insert_rows(
        self,
        table: Table | str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]] | Any,
        *,
        on_conflict: Literal["error", "ignore", "update", "replace"] = "error",
        update_columns: Sequence[str] | None = None,
        chunk_size: int | None = None,
        update_exprs: Mapping[str, Any] | None = None,
    ) -> int:
        self._require_writable()
        name = _table_name(table)
        cols = [validate_ident(c) for c in columns]
        if not cols:
            raise ValueError("insert_rows requires at least one column")
        per = max(1, min(chunk_size or 5000, _MAX_VARIABLES // len(cols)))
        total = 0
        batch: list[tuple[Any, ...]] = []

        def flush() -> int:
            if not batch:
                return 0
            sql, trailing = render_insert_with_params(
                name, cols, len(batch), on_conflict=on_conflict, update_columns=update_columns,
                dialect="sqlite", update_exprs=update_exprs,
            )
            return self._write(sql, [v for r in batch for v in r] + trailing)

        with self.transaction():
            for r in rows:
                batch.append(tuple(r))
                if len(batch) >= per:
                    total += flush()
                    batch = []
            total += flush()
        return total

    def update_rows(
        self,
        table: Table | str,
        values: Mapping[str, Any],
        where: Where | None,
        *,
        limit: int | None = None,
    ) -> int:
        if has_empty_in_term(where):
            return 0
        term_idx, target_col, oversize = find_oversize_in_term(where, chunk_size=_IN_CHUNK)
        if oversize is not None and target_col is not None:
            if limit is not None:
                raise ValueError("Auto-chunking cannot be combined with limit")
            return sum(
                self.update_rows(table, values, replace_in_term(where, term_idx, target_col, oversize[i:i + _IN_CHUNK]))
                for i in range(0, len(oversize), _IN_CHUNK)
            )
        sql, params = render_update(table, values, where, limit=limit, dialect="sqlite")
        return self._write(sql, params)

    def delete_rows(
        self,
        table: Table | str,
        where: Where | None,
        *,
        limit: int | None = None,
        all_rows: bool = False,
    ) -> int:
        if where is None and not all_rows:
            raise ValueError("delete_rows without where requires all_rows=True")
        if has_empty_in_term(where):
            return 0
        term_idx, target_col, oversize = find_oversize_in_term(where, chunk_size=_IN_CHUNK)
        if oversize is not None and target_col is not None:
            if limit is not None:
                raise ValueError("Auto-chunking cannot be combined with limit")
            return sum(
                self.delete_rows(table, replace_in_term(where, term_idx, target_col, oversize[i:i + _IN_CHUNK]), all_rows=all_rows)
                for i in range(0, len(oversize), _IN_CHUNK)
            )
        sql, params = render_delete(table, where, limit=limit, all_rows=all_rows, dialect="sqlite")
        return self._write(sql, params)

    def copy_rows(
        self,
        table: Table | str,
        columns: Sequence[str],
        select: Select,
        *,
        on_conflict: Literal["error", "ignore"] = "error",
    ) -> int:
        sql, params = render_copy(table, columns, select, on_conflict=on_conflict, dialect="sqlite")
        return self._write(sql, params)

    def add_column_if_missing(self, table_name: str, column_name: str, column_def: str) -> bool:
        if self.column_exists(table_name, column_name):
            return False
        validate_ident(table_name)
        col_spec = column_def if column_def.strip().startswith(column_name) else f"{column_name} {column_def}"
        self._write(f"ALTER TABLE {table_name} ADD COLUMN {col_spec}")
        return True

def _schema_spec(table_name: str, column: str) -> Any:
    """`ColumnSpec` of `table_name.column` in the canonical schema (`core.DBLayout.TABLES`), or None."""
    try:
        import core.DBLayout as layout
    except Exception:
        return None
    for table in getattr(layout, "TABLES", ()):
        if table.table_name == table_name:
            for col in table.init_columns:
                if col[0] == column:
                    return col[1]
    return None


class SQLiteReadSession(ReadIntents):
    """Read-only view of a `SQLiteDB`: select/aggregate/catalog calls only (dict rows by default)."""

    _READ_ONLY_CALLS = frozenset({
        "column_exists", "column_info", "existing_tables", "index_names", "table_stats", "resolve_relation_name",
        "table_exists",
    })

    _WRITE_CALLS = frozenset({
        "execute", "executemany", "insert", "update", "insert_rows", "update_rows", "delete_rows", "copy_rows",
        "delete", "drop_tables", "drop_table", "create_table", "create_view", "drop_view", "add_column",
        "add_column_if_missing", "create_index", "remove_index", "ensure_columns", "ensure_views",
        "enqueue_parse_tasks", "record_task_failure", "get_or_insert_id", "commit", "rollback",
        "apply_server_tuning", "query", "cursor",
    })

    def __init__(self, db: SQLiteDB, *, owned: bool) -> None:
        self._db = db
        self._owned = owned

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._owned:
            self._db.close()

    def close(self) -> None:
        """Close the connection this session opened (a shared reader stays open)."""
        if self._owned:
            self._db.close()

    def select_rows(self, source: Source, columns: Any, where: Where | None = None, *, as_dict: bool | None = None, **kw: Any) -> list[tuple | dict]:
        return self._db.select_rows(source, columns, where, as_dict=True if as_dict is None else as_dict, **kw)

    def select_spec(self, select: Select, *, as_dict: bool | None = None) -> list[tuple | dict]:
        return self._db.select_spec(select, as_dict=True if as_dict is None else as_dict)

    def iter_rows(self, source: Source, columns: Any, where: Where | None = None, *, as_dict: bool | None = None, **kw: Any) -> Iterator[tuple | dict]:
        return self._db.iter_rows(source, columns, where, as_dict=True if as_dict is None else as_dict, **kw)

    def select_one(self, source: Source, columns: Any, where: Where | None = None, *, as_dict: bool | None = None, **kw: Any) -> tuple | dict | None:
        return self._db.select_one(source, columns, where, as_dict=True if as_dict is None else as_dict, **kw)

    def aggregate(self, source: Source, aggregates: Mapping[str, Agg], where: Where | None = None, *, as_dict: bool | None = None, **kw: Any) -> list:
        return self._db.aggregate(source, aggregates, where, as_dict=True if as_dict is None else as_dict, **kw)

    def select_values(self, *args: Any, **kw: Any) -> list:
        return self._db.select_values(*args, **kw)

    def select_value(self, *args: Any, **kw: Any) -> Any:
        return self._db.select_value(*args, **kw)

    def count_rows(self, *args: Any, **kw: Any) -> int:
        return self._db.count_rows(*args, **kw)

    def row_exists(self, *args: Any, **kw: Any) -> bool:
        return self._db.row_exists(*args, **kw)

    def max_value(self, *args: Any, **kw: Any) -> Any:
        return self._db.max_value(*args, **kw)

    def min_value(self, *args: Any, **kw: Any) -> Any:
        return self._db.min_value(*args, **kw)

    def __getattr__(self, name: str) -> Any:
        if name in SQLiteReadSession._READ_ONLY_CALLS:
            return getattr(self._db, name)
        if name not in SQLiteReadSession._WRITE_CALLS:
            raise AttributeError(name)

        def _refuse(*args: Any, **kwargs: Any) -> Any:
            raise ReadOnlyViolation(
                    f"SQLiteReadSession is read-only: {name}() needs the writer engine "
                    "- see DB_API.md 'Webapp Read Session & Query Primitives'"
                )

        return _refuse


__all__ = ["SQLiteDB", "SQLiteReadSession", "SQLiteNewPairs", "sqlite_path_from_config", "MEMORY", "SHARED_MEMORY"]
