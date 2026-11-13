"""db_engine/foreign.py - Read-only access to foreign (externally produced) SQLite files.

Some inputs are SQLite databases written by other programs (for example public-inbox `over.sqlite3` /
`msgmap.sqlite3`). `open_foreign_sqlite()` opens one read-only and exposes the spec read primitives
(`select_rows`, `select_one`, `select_values`, ...) rendered with `dialect="sqlite"`, so callers never write SQL.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from db_engine.base import BaseDBEngine
from db_engine.errors import ConnectionLostError, DBError, MissingTableError
from db_engine.spec import ColumnSpec, Select, Source, Where
from db_engine.sql_render import render_column_ddl, render_insert, render_select, validate_ident

logger = logging.getLogger(__name__)

DEFAULT_CACHE_KIB = 8_000
_LOCK_RETRIES = 5
_LOCK_BASE_DELAY = 0.2


class ForeignReader:
    """Thread-safe read-only view of one foreign SQLite file (one lazily opened connection per thread).

    Read primitives mirror `BaseDBEngine` (`select_rows`, `iter_rows`, `select_one`, `select_values`, `select_value`,
    `count_rows`, `row_exists`) with tuple rows by default (`as_dict=True` gives dicts). `ProgrammingError` for a
    missing table surfaces as `MissingTableError`; lock contention is retried with exponential backoff.
    """

    dialect: Literal["sqlite"] = "sqlite"

    def __init__(
        self,
        path: str | Path,
        *,
        readonly: bool = True,
        immutable: bool = True,
        timeout: float = 60.0,
        cache_kib: int = DEFAULT_CACHE_KIB,
    ) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self.immutable = immutable and readonly
        self.timeout = timeout
        self.cache_kib = cache_kib
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        if not self.path.exists():
            raise ConnectionLostError(f"Foreign SQLite file not found: {self.path}")

    # ---- connections -------------------------------------------------------------
    def _open(self) -> sqlite3.Connection:
        if self.readonly:
            uri = f"file:{self.path}?mode=ro" + ("&immutable=1" if self.immutable else "")
        else:
            uri = f"file:{self.path}?mode=rw"
        try:
            conn = sqlite3.connect(
                uri, uri=True, check_same_thread=False, timeout=self.timeout, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise ConnectionLostError(f"Cannot open foreign SQLite file {self.path}: {exc}") from exc
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)};")
            if self.readonly:
                conn.execute("PRAGMA query_only = ON;")
            # Handles are per thread: bound the page cache and spill sorters to disk instead of growing the heap.
            conn.execute(f"PRAGMA cache_size = -{int(self.cache_kib)};")
            conn.execute("PRAGMA temp_store = FILE;")
        except sqlite3.Error:
            logger.debug("Could not apply PRAGMAs to %s", self.path, exc_info=True)
        with self._lock:
            self._connections.append(conn)
        return conn

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def close_thread(self) -> None:
        """Close the calling thread's connection (request threads that finish while the reader lives on)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)
        try:
            conn.close()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        """Close every connection opened by any thread."""
        with self._lock:
            conns, self._connections = self._connections, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    def __enter__(self) -> "ForeignReader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- execution -----------------------------------------------------------------
    def _run(self, sql: str, params: Sequence[Any]) -> sqlite3.Cursor:
        conn = self._connection()
        for attempt in range(_LOCK_RETRIES):
            try:
                return conn.execute(sql, tuple(params))
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "no such table" in text:
                    raise MissingTableError(str(exc)) from exc
                if ("locked" in text or "busy" in text) and attempt < _LOCK_RETRIES - 1:
                    delay = _LOCK_BASE_DELAY * (2**attempt)
                    logger.warning("SQLite lock contention (%d/%d), retrying in %.2fs: %s", attempt + 1, _LOCK_RETRIES, delay, exc)
                    time.sleep(delay)
                    continue
                raise DBError(f"Foreign SQLite query failed: {exc}") from exc
            except sqlite3.Error as exc:
                raise DBError(f"Foreign SQLite query failed: {exc}") from exc
        raise DBError("Foreign SQLite query failed: retries exhausted")

    # ---- spec reads ------------------------------------------------------------------
    def select_rows(
        self,
        source: Source,
        columns: Sequence[Any] | Mapping[str, Any],
        where: Where | None = None,
        *,
        joins: Sequence[Any] = (),
        distinct: bool = False,
        order_by: Sequence[Any] = (),
        limit: int | None = None,
        offset: int | None = None,
        as_dict: bool | None = None,
        group_by: Sequence[Any] = (),
        having: Where | None = None,
        hint: str | None = None,
    ) -> list[tuple | dict]:
        """Run a spec select against the foreign file and return all rows."""
        spec = Select(source, columns, where, tuple(joins), distinct, tuple(order_by), limit, offset, tuple(group_by), having, None)
        return self.select_spec(spec, as_dict=as_dict)

    def select_spec(self, select: Select, *, as_dict: bool | None = None) -> list[tuple | dict]:
        """Run a complete `Select` specification."""
        sql, params, col_names = render_select(select, dialect="sqlite")
        rows = self._run(sql, params).fetchall()
        if as_dict:
            return [dict(zip(col_names, r)) for r in rows]
        return [tuple(r) for r in rows]

    def iter_rows(self, source: Source, columns: Sequence[Any] | Mapping[str, Any], where: Where | None = None, *,
                  chunk_size: int = 2000, as_dict: bool | None = None, **kwargs: Any) -> Iterator[tuple | dict]:
        """Stream rows of a spec select in chunks of `chunk_size`."""
        spec = Select(source, columns, where, tuple(kwargs.get("joins", ())), bool(kwargs.get("distinct", False)),
                      tuple(kwargs.get("order_by", ())), kwargs.get("limit"), kwargs.get("offset"),
                      tuple(kwargs.get("group_by", ())), kwargs.get("having"), None)
        sql, params, col_names = render_select(spec, dialect="sqlite")
        cur = self._run(sql, params)
        while True:
            chunk = cur.fetchmany(chunk_size)
            if not chunk:
                return
            for r in chunk:
                yield dict(zip(col_names, r)) if as_dict else tuple(r)

    def table_exists(self, name: str) -> bool:
        """True when the file holds a table or view called `name`."""
        validate_ident(name)
        rows = self.select_rows("sqlite_master", ["name"], {"name": name}, limit=1)
        return bool(rows)

    # Derived primitives are generic over `select_rows`.
    select_one = BaseDBEngine.select_one
    select_values = BaseDBEngine.select_values
    select_value = BaseDBEngine.select_value
    count_rows = BaseDBEngine.count_rows
    row_exists = BaseDBEngine.row_exists
    aggregate = BaseDBEngine.aggregate
    min_value = BaseDBEngine.min_value
    max_value = BaseDBEngine.max_value


def open_foreign_sqlite(
    path: str | Path,
    *,
    readonly: bool = True,
    immutable: bool = True,
    timeout: float = 60.0,
    cache_kib: int = DEFAULT_CACHE_KIB,
) -> ForeignReader:
    """Open the SQLite file `path` written by another program for spec reads.

    Args:
        path: Existing SQLite file (raises `ConnectionLostError` when missing or unopenable).
        readonly: Open with `mode=ro` and `PRAGMA query_only` (the default; the only supported use today).
        immutable: With `readonly`, add `immutable=1` (no locking, valid for files nobody writes concurrently).
        timeout: Busy timeout in seconds.
        cache_kib: Page cache per connection in KiB (one connection per calling thread).
    """
    return ForeignReader(path, readonly=readonly, immutable=immutable, timeout=timeout, cache_kib=cache_kib)


class ForeignWriter:
    """Writable handle on a SQLite file in the layout of a foreign program (fixtures for tools and tests).

    Production code only reads foreign files; this builder produces files `ForeignReader` can then open
    (for example an `over.sqlite3` lookalike). Every `insert_rows()` call commits.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        except sqlite3.Error as exc:
            raise ConnectionLostError(f"Cannot open foreign SQLite file {self.path}: {exc}") from exc

    def create_table(self, name: str, columns: Mapping[str, ColumnSpec] | Sequence[tuple[str, ColumnSpec]],
                     *, primary: Sequence[str] = ()) -> None:
        """Create table `name` (when missing) from `{column: ColumnSpec}`; `primary` names the primary key columns."""
        validate_ident(name)
        items = list(columns.items()) if isinstance(columns, Mapping) else list(columns)
        parts = [render_column_ddl(col, spec, "sqlite") for col, spec in items]
        if primary:
            parts.append(f"PRIMARY KEY ({', '.join(primary)})")
        self._execute(f"CREATE TABLE IF NOT EXISTS {name} ({', '.join(parts)})", ())

    def insert_rows(self, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> int:
        """Insert `rows` (one tuple per row in `columns` order); returns the number of rows written."""
        rows = list(rows)
        if not rows:
            return 0
        sql = render_insert(table, columns, 1, dialect="sqlite")
        for row in rows:
            self._execute(sql, tuple(row))
        return len(rows)

    def _execute(self, sql: str, params: Sequence[Any]) -> None:
        try:
            self._conn.execute(sql, tuple(params))
        except sqlite3.Error as exc:
            raise DBError(f"Foreign SQLite write failed: {exc}") from exc

    def close(self) -> None:
        """Close the file."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "ForeignWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def create_foreign_sqlite(path: str | Path) -> ForeignWriter:
    """Create (or open for writing) the SQLite file `path`; see `ForeignWriter`."""
    return ForeignWriter(path)


__all__ = ["ForeignReader", "ForeignWriter", "create_foreign_sqlite", "open_foreign_sqlite"]
