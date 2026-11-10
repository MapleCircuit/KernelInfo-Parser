"""db_engine/pool.py - Resilient Database Connection Pool & Session Management."""
from __future__ import annotations

import logging
import os
import ssl
import threading
import time
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from types import TracebackType
from typing import TYPE_CHECKING, Any, NamedTuple, Self

import mysql.connector
import mysql.connector.errors

from core.globalstuff import JoinsType, PointerType, SafeDataType
from db_engine.base import BaseDBEngine, ColumnInfo, TableStats
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

if TYPE_CHECKING:
    from core.TableHandling import Table
    from typing import Protocol
else:
    from typing import Protocol

# Python 3.12 compatibility shim: mysql.connector calls ssl.wrap_socket which was removed in 3.12
if not hasattr(ssl, "wrap_socket"):
    def _compat_wrap_socket(
        sock: Any,
        keyfile: str | None = None,
        certfile: str | None = None,
        server_side: bool = False,
        cert_reqs: int = ssl.CERT_NONE,
        ssl_version: Any = None,
        ca_certs: str | None = None,
        do_handshake_on_connect: bool = True,
        suppress_ragged_eofs: bool = True,
        ciphers: str | None = None,
    ) -> Any:
        context = ssl.create_default_context() if not server_side else ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.check_hostname = False
        context.verify_mode = cert_reqs
        if ca_certs:
            context.load_verify_locations(ca_certs)
        if certfile:
            context.load_cert_chain(certfile, keyfile)
        if ciphers:
            context.set_ciphers(ciphers)
        return context.wrap_socket(
            sock,
            server_side=server_side,
            do_handshake_on_connect=do_handshake_on_connect,
            suppress_ragged_eofs=suppress_ragged_eofs,
        )

    ssl.wrap_socket = _compat_wrap_socket

logger = logging.getLogger(__name__)

POOL_WAIT_S = 10.0
POOL_SIZE = 32


class ConnectionPool:
    """Bounded pool of database connections, opened on demand."""

    def __init__(self, connect_kwargs: dict[str, Any], size: int = POOL_SIZE) -> None:
        self._kwargs = connect_kwargs
        self.size = max(1, size)
        self._idle: list[Any] = []
        self._open = 0
        self._closed = False
        self._cond = threading.Condition()

    def acquire(self, timeout: float = POOL_WAIT_S) -> Any:
        """Lend a connection, waiting up to `timeout` s while all `size` are lent."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if self._closed:
                    raise mysql.connector.errors.PoolError("Connection pool is closed")
                if self._idle:
                    return self._idle.pop()
                if self._open < self.size:
                    self._open += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise mysql.connector.errors.PoolError("Failed getting connection; pool exhausted")
                self._cond.wait(remaining)
        try:
            return mysql.connector.connect(**self._kwargs)
        except Exception:
            self._forget(None)
            raise

    def release(self, cnx: Any, broken: bool = False) -> None:
        """Take back a lent connection; a broken one is closed."""
        if not broken:
            with self._cond:
                if not self._closed:
                    self._idle.append(cnx)
                    self._cond.notify()
                    return
        self._forget(cnx)

    def _forget(self, cnx: Any) -> None:
        if cnx is not None:
            try:
                cnx.close()
            except Exception:
                pass
        with self._cond:
            self._open -= 1
            self._cond.notify()

    def close(self) -> None:
        """Close the idle connections; lent ones are closed when given back."""
        with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
        for cnx in idle:
            self._forget(cnx)


_POOL: ConnectionPool | None = None
_POOL_LOCK = threading.Lock()


def get_db_pool() -> ConnectionPool:
    """Initialize or return the connection pool."""
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        return _create_pool()


def _create_pool() -> ConnectionPool:
    """Build the pool (starting the SSH tunnel first when configured) and check it with one connection."""
    global _POOL
    if _POOL is None:
        from webapp.backend.config import get_backend_db_config

        cfg = get_backend_db_config()

        ssh_tunnel_cfg = cfg.get("ssh_tunnel") or {}
        ssh_tunnel_active = bool(ssh_tunnel_cfg.get("enabled", False))
        if ssh_tunnel_active:
            from core.ssh_tunnel import is_ssh_tunnel_active, start_ssh_tunnel

            if not is_ssh_tunnel_active("database"):
                logger.info("Starting database SSH tunnel for web application...")
                try:
                    start_ssh_tunnel(ssh_tunnel_cfg, feature="database")
                    cfg = get_backend_db_config()
                except Exception as exc:
                    logger.error("Failed to start database SSH tunnel: %s", exc)
                    raise

        connect_kwargs: dict[str, Any] = {
            "host": cfg.get("host", "127.0.0.1"),
            "port": int(cfg.get("port", 3306)),
            "user": cfg.get("user", "root"),
            "password": cfg.get("password", "Passe123"),
            "database": cfg.get("database", "test"),
            "connection_timeout": max(int(cfg.get("timeout", 10)), 30),
            "autocommit": True,
        }
        if os.getenv("DB_SSL_DISABLED", "").lower() in ("true", "1", "yes") or cfg.get("ssl_disabled"):
            connect_kwargs["ssl_disabled"] = True

        socket_path = cfg.get("resolved_unix_socket") or os.getenv("DB_SOCKET") or os.getenv("MYSQL_UNIX_PORT")
        if not ssh_tunnel_active and socket_path and os.path.exists(socket_path):
            pool = ConnectionPool({**connect_kwargs, "unix_socket": socket_path})
            try:
                pool.release(pool.acquire())
                _POOL = pool
                logger.info("Initialized connection pool (up to %d connections) via Unix socket (%s).", POOL_SIZE, socket_path)
                return _POOL
            except Exception as e:
                pool.close()
                logger.warning(
                    "Failed to connect via Unix socket '%s' (%s); falling back to TCP...",
                    socket_path,
                    e,
                )

        pool = ConnectionPool(connect_kwargs)
        pool.release(pool.acquire())
        _POOL = pool
        logger.info(
            "Initialized connection pool (up to %d connections) via TCP (%s:%s).",
            POOL_SIZE,
            connect_kwargs.get("host"),
            connect_kwargs.get("port"),
        )
    return _POOL


def close_db_pool() -> None:
    """Close and release the active database connection pool."""
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.close()


def get_pooled_connection(max_retries: int = 3) -> tuple[ConnectionPool, Any]:
    """Acquire a connection: (the pool it goes back to, connection)."""
    global _POOL
    pool = get_db_pool()
    backoff = 0.05
    failures = 0
    while True:
        try:
            return pool, pool.acquire(POOL_WAIT_S)
        except mysql.connector.errors.PoolError as e:
            logger.error("No database connection free after %.0f s: %s", POOL_WAIT_S, e)
            raise
        except (mysql.connector.errors.InterfaceError, mysql.connector.errors.OperationalError) as e:
            failures += 1
            if failures >= max_retries:
                logger.error("Database connection acquisition failed after %d retries: %s", max_retries, e)
                raise
            logger.warning("Database connection error (%s); resetting pool...", e)
            with _POOL_LOCK:
                if _POOL is pool:
                    _POOL = None
            pool.close()
            time.sleep(backoff)
            backoff *= 2
            pool = get_db_pool()


class PooledSession:
    """Leased database engine session providing cursor and legacy query compatibility."""

    def __init__(self, cnx: Any, cursor_dict: bool = True) -> None:
        self.cnx = cnx
        self._cursor = cnx.cursor(dictionary=cursor_dict)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        pass

    def close(self) -> None:
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:
                pass
            self._cursor = None

    # --- Cursor Compatibility Interface ---
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Execute query/statement through session cursor."""
        self._cursor.execute(sql, tuple(params or ()))
        return max(0, getattr(self._cursor, "rowcount", 0))

    def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
        if not params_seq:
            return 0
        self._cursor.executemany(sql, [tuple(p) for p in params_seq])
        return max(0, getattr(self._cursor, "rowcount", 0))

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._cursor.fetchall() or ())

    def fetchmany(self, size: int = 1) -> list[Any]:
        return list(self._cursor.fetchmany(size) or ())

    @property
    def rowcount(self) -> int:
        return getattr(self._cursor, "rowcount", -1)

    @property
    def description(self) -> Any:
        return getattr(self._cursor, "description", None)

    # --- BaseDBEngine Contract Implementation ---
    def query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> list[dict[str, Any] | tuple[SafeDataType, ...]]:
        if getattr(self._cursor, "dictionary", True) == dictionary:
            self._cursor.execute(sql, tuple(params or ()))
            rows = self._cursor.fetchall()
            return list(rows) if rows else []
        with self.cnx.cursor(dictionary=dictionary) as cur:
            cur.execute(sql, tuple(params or ()))
            rows = cur.fetchall()
            return list(rows) if rows else []

    def query_one(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> dict[str, Any] | tuple[SafeDataType, ...] | None:
        if getattr(self._cursor, "dictionary", True) == dictionary:
            self._cursor.execute(sql, tuple(params or ()))
            return self._cursor.fetchone()
        with self.cnx.cursor(dictionary=dictionary) as cur:
            cur.execute(sql, tuple(params or ()))
            return cur.fetchone()

    def query_iter(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
        chunk_size: int = 2000,
    ) -> Iterator[dict[str, Any] | tuple[SafeDataType, ...]]:
        self._cursor.execute(sql, tuple(params or ()))
        while True:
            chunk = self._cursor.fetchmany(chunk_size)
            if not chunk:
                break
            yield from chunk

    def commit(self) -> None:
        self.cnx.commit()

    def rollback(self) -> None:
        self.cnx.rollback()

    def get_next_id(self, table: Table) -> int:
        pk_col = table.init_columns[0][0]
        row = self.query_one(f"SELECT COALESCE(MAX({pk_col}), 0) + 1 AS nxt FROM {table.table_name}", dictionary=False)
        return int(row[0]) if row and row[0] is not None else 1

    def select(self, table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None:
        filters = []
        params = []
        for i, val in enumerate(data):
            if val is not None:
                filters.append(f"{table.init_columns[i][0]} = %s")
                params.append(val)
        where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""
        row = self.query_one(f"SELECT * FROM {table.table_name}{where_clause} LIMIT 1", tuple(params), dictionary=False)
        return tuple(row) if row is not None else None

    def view_select(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        rows = self.view_select_multiple(tables, joins, columns)
        return rows[0] if rows else None

    def view_select_multiple(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> list[tuple[SafeDataType, ...]]:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        return driver.view_select_multiple(tables, joins, columns)

    def select_preload(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> list[tuple[SafeDataType, ...]]:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        return driver.select_preload(table, cached_columns, min_vid)

    def insert(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        driver.insert(table, data)

    def update(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        driver.update(table, data)

    def commit_tables_parallel(
        self,
        tables_data: Sequence[tuple[Table, Sequence[tuple[SafeDataType, ...]], Sequence[tuple[SafeDataType, ...]]]],
        max_workers: int | None = None,
        dedicated: bool = False,
    ) -> list[Any]:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        return driver.commit_tables_parallel(tables_data, max_workers, dedicated)

    def create_table(self, tables: Sequence[Table] | Table) -> None:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        driver.create_table(tables)

    def drop_table(self, tables: Sequence[Table] | Table) -> None:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        driver.drop_table(tables)

    def index_exists(self, index_name: str, table: Table) -> bool:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        return driver.index_exists(index_name, table)

    def create_index(self, index_name: str, table: Table, rows: tuple[PointerType, ...]) -> None:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        driver.create_index(index_name, table, rows)

    def remove_index(self, index_name: str, table: Table) -> None:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        driver.remove_index(index_name, table)

    def create_indexes(
        self,
        indexes: Sequence[tuple[str, Table, tuple[PointerType, ...]]],
        max_workers: int | None = None,
    ) -> None:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        driver.create_indexes(indexes, max_workers)

    def remove_indexes(
        self,
        indexes: Sequence[tuple[str, Table]],
        max_workers: int | None = None,
    ) -> None:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        driver.remove_indexes(indexes, max_workers)

    def test_tables(self, tables: Sequence[Table] | Table) -> list[str] | None:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        return driver.test_tables(tables)

    def verify_relational_integrity(self, tables: Sequence[Table]) -> dict[str, int]:
        from db_engine.DBHandling import MariaDB
        driver = MariaDB.__new__(MariaDB)
        driver.cnx = self.cnx
        driver.cursor = self._cursor
        return driver.verify_relational_integrity(tables)


@contextmanager
def get_db_cursor() -> Generator[PooledSession, None, None]:
    """Provide a managed database session implementing BaseDBEngine and cursor semantics.

    Guarantees return of connection to pool and enforces strict read-only semantics for webapp.
    """
    pool, cnx = get_pooled_connection()
    session: PooledSession | None = None
    broken = False
    try:
        try:
            session = PooledSession(cnx, cursor_dict=True)
        except (mysql.connector.errors.InterfaceError, mysql.connector.errors.OperationalError):
            logger.info("Pooled database connection was lost; reconnecting.")
            cnx.reconnect(attempts=1, delay=0)
            session = PooledSession(cnx, cursor_dict=True)
        yield session
    except (mysql.connector.errors.InterfaceError, mysql.connector.errors.OperationalError):
        broken = True
        raise
    finally:
        try:
            if session is not None:
                session.close()
            if not broken and getattr(cnx, "unread_result", False):
                cnx.consume_results()
        except Exception:
            broken = True
        pool.release(cnx, broken)


get_db_session = get_db_cursor


# -----------------------------------------------------------------------------
# Webapp Read API Protocol & ReadSession (§6 & §7.2)
# -----------------------------------------------------------------------------
class ReadAPI(Protocol):
    """Protocol defining the read interface exposed to webapp and services."""

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
    ) -> list[tuple | dict]: ...

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
    ) -> Iterator[tuple | dict]: ...

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
    ) -> tuple | dict | None: ...

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
    ) -> list: ...

    def select_value(
        self,
        source: Source,
        column: str,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        order_by: Sequence[str] = (),
        default: Any = None,
    ) -> Any: ...

    def count_rows(
        self,
        source: Source,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: str | None = None,
    ) -> int: ...

    def row_exists(
        self,
        source: Source,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
    ) -> bool: ...

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
    ) -> list: ...

    def max_value(self, source: Source, column: str, where: Where | None = None, *, default: Any = 0) -> Any: ...
    def min_value(self, source: Source, column: str, where: Where | None = None, *, default: Any = 0) -> Any: ...
    def column_exists(self, table: Table | str, column: str) -> bool: ...
    def existing_tables(self, names: Sequence[str], *, include_views: bool = False) -> set[str]: ...
    def column_info(self, table: Table | str, column: str) -> ColumnInfo | None: ...
    def index_names(self, table: Table | str) -> set[str]: ...
    def table_stats(self, names: Sequence[str] | None = None) -> list[TableStats]: ...
    def resolve_relation_name(self, name: str) -> str: ...

    # Intent read methods (§7.2)
    def list_network_commits(
        self,
        network_id: int,
        *,
        hash_prefix: str | None = None,
        text: str | None = None,
        authored_before: int | None = None,
        id_min: int | None = None,
        id_min_exclusive: bool = False,
        id_max: int | None = None,
        newest_first: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]: ...

    def maintainer_sections(
        self,
        sec_ids: Sequence[int] | None = None,
        *,
        sec_id: int | None = None,
        name: str | None = None,
        member_person_ids: Sequence[int] | None = None,
        text: str | None = None,
        limit: int | None = None,
    ) -> list[dict]: ...

    def maintainer_developer_directory(
        self,
        sec_ids: Sequence[int] | None = None,
        credit_ids: Sequence[int] | None = None,
    ) -> list[dict]: ...

    def find_maintainer_person(
        self,
        raw: str,
        stripped: str,
        email: str,
        name: str,
    ) -> dict | None: ...

    def symbol_name_page(
        self,
        prefix: str,
        *,
        after: tuple[int, str] | None = None,
        limit: int = 400,
    ) -> list[str]: ...

    def symbol_refs_of_targets(
        self,
        ast_ids: Sequence[int],
        *,
        role: int | None = None,
        with_caller: bool = False,
        limit: int | None = None,
    ) -> list[dict]: ...

    def symbol_def_blobs_of_targets(self, ast_ids: Sequence[int]) -> set[tuple[int, int]]: ...

    def checkpoint_find_file(
        self,
        commit_id: int,
        candidates: Sequence[str],
        suffix: str | None = None,
    ) -> str | None: ...


class ReadSession(ReadAPI):
    """Managed read session bound to a single pooled connection with as_dict=True default."""

    def __init__(self, cnx: Any, pool: ConnectionPool) -> None:
        self.cnx = cnx
        self.pool = pool
        self._cursor = cnx.cursor(dictionary=True)
        from db_engine.DBHandling import MariaDB
        self._driver = MariaDB.__new__(MariaDB)
        self._driver.cnx = self.cnx
        self._driver.cursor = self._cursor
        self._closed = False

    def close(self) -> None:
        """Close session cursor."""
        if not self._closed:
            try:
                self._cursor.close()
            except Exception:
                pass
            self._closed = True

    # ---- §2 Read Primitives (as_dict=True default) ---------------------------
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
        dict_mode = True if as_dict is None else as_dict
        return self._driver.select_rows(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            offset=offset,
            as_dict=dict_mode,
        )

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
        dict_mode = True if as_dict is None else as_dict
        return self._driver.iter_rows(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            offset=offset,
            as_dict=dict_mode,
        )

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
        dict_mode = True if as_dict is None else as_dict
        return self._driver.select_one(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            as_dict=dict_mode,
        )

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
        return self._driver.select_values(
            source,
            column,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
        )

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
        return self._driver.select_value(
            source,
            column,
            where=where,
            joins=joins,
            order_by=order_by,
            default=default,
        )

    def count_rows(
        self,
        source: Source,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: str | None = None,
    ) -> int:
        return self._driver.count_rows(
            source,
            where=where,
            joins=joins,
            distinct=distinct,
        )

    def row_exists(
        self,
        source: Source,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
    ) -> bool:
        return self._driver.row_exists(source, where=where, joins=joins)

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
        dict_mode = True if as_dict is None else as_dict
        return self._driver.aggregate(
            source,
            aggregates,
            where=where,
            joins=joins,
            group_by=group_by,
            order_by=order_by,
            as_dict=dict_mode,
        )

    def max_value(self, source: Source, column: str, where: Where | None = None, *, default: Any = 0) -> Any:
        return self._driver.max_value(source, column, where=where, default=default)

    def min_value(self, source: Source, column: str, where: Where | None = None, *, default: Any = 0) -> Any:
        return self._driver.min_value(source, column, where=where, default=default)

    # ---- §4 Catalog and Logical Relations (§5) -------------------------------
    def column_exists(self, table: Table | str, column: str) -> bool:
        return self._driver.column_exists(table, column)

    def existing_tables(self, names: Sequence[str], *, include_views: bool = False) -> set[str]:
        return self._driver.existing_tables(names, include_views=include_views)

    def column_info(self, table: Table | str, column: str) -> ColumnInfo | None:
        return self._driver.column_info(table, column)

    def index_names(self, table: Table | str) -> set[str]:
        return self._driver.index_names(table)

    def table_stats(self, names: Sequence[str] | None = None) -> list[TableStats]:
        return self._driver.table_stats(names)

    def resolve_relation_name(self, name: str) -> str:
        return self._driver.resolve_relation_name(name)

    # ---- §7.2 Webapp Intent Reads --------------------------------------------
    def list_network_commits(
        self,
        network_id: int,
        *,
        hash_prefix: str | None = None,
        text: str | None = None,
        authored_before: int | None = None,
        id_min: int | None = None,
        id_min_exclusive: bool = False,
        id_max: int | None = None,
        newest_first: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Fetch network commits matching hash prefix, text, date, and range bounds."""
        where_clauses = ["c.commit_id > 0", "c.network_id = %s"]
        params: list[Any] = [network_id]

        if hash_prefix:
            from db_engine.spec import hash_prefix as hp_spec
            hp = hp_spec(hash_prefix)
            lo, hi = hp.value, hp.value2
            if hi is not None:
                where_clauses.append("c.commit_hash >= %s AND c.commit_hash < %s")
                params.extend([lo, hi])
            else:
                where_clauses.append("c.commit_hash >= %s")
                params.append(lo)

        if text:
            esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pat = f"%{esc}%"
            where_clauses.append("(LOWER(c.subject) LIKE LOWER(%s) ESCAPE '\\\\' OR LOWER(p.name) LIKE LOWER(%s) ESCAPE '\\\\')")
            params.extend([pat, pat])

        if authored_before is not None:
            where_clauses.append("c.author_date < %s")
            params.append(authored_before)

        if id_min is not None:
            op = ">" if id_min_exclusive else ">="
            where_clauses.append(f"c.commit_id {op} %s")
            params.append(id_min)

        if id_max is not None:
            where_clauses.append("c.commit_id <= %s")
            params.append(id_max)

        order_dir = "DESC" if newest_first else "ASC"
        order_sql = f"ORDER BY c.commit_id {order_dir}"
        limit_sql = f"LIMIT {int(limit)} OFFSET {int(offset)}"

        sql = f"""
            SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                   p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                   (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
            FROM m_commit c
            JOIN m_maintainer_person p ON c.author_id = p.person_id
            WHERE {' AND '.join(where_clauses)}
            {order_sql} {limit_sql};
        """
        rows = self._driver.query(sql, params, dictionary=True)
        for r in rows:
            if isinstance(r.get("commit_hash"), (bytearray, memoryview)):
                r["commit_hash"] = bytes(r["commit_hash"])
        return rows

    def maintainer_sections(
        self,
        sec_ids: Sequence[int] | None = None,
        *,
        sec_id: int | None = None,
        name: str | None = None,
        member_person_ids: Sequence[int] | None = None,
        text: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Fetch maintainer sections with status, URLs, and optional columns."""
        has_ext = self.column_exists("m_maintainer_section", "patchwork")
        if has_ext:
            cols = "s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list, s.ast_id, s.patchwork, s.bug_url, s.chat_channel, s.profile_doc"
        else:
            cols = "s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list, s.ast_id"

        where_parts = []
        params: list[Any] = []
        if sec_ids is not None:
            if not sec_ids:
                return []
            ph = ", ".join(["%s"] * len(sec_ids))
            where_parts.append(f"s.sec_id IN ({ph})")
            params.extend(sec_ids)

        if sec_id is not None:
            where_parts.append("s.sec_id = %s")
            params.append(sec_id)

        if name is not None:
            where_parts.append("s.name = %s")
            params.append(name)

        if member_person_ids is not None:
            if not member_person_ids:
                return []
            ph = ", ".join(["%s"] * len(member_person_ids))
            where_parts.append(f"EXISTS (SELECT 1 FROM m_maintainer_member mem WHERE mem.sec_id = s.sec_id AND mem.person_id IN ({ph}))")
            params.extend(member_person_ids)

        if text is not None:
            esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pat = f"%{esc}%"
            where_parts.append(
                "(LOWER(s.name) LIKE LOWER(%s) ESCAPE '\\\\' OR EXISTS ("
                "SELECT 1 FROM m_maintainer_member mem JOIN m_maintainer_person per ON mem.person_id = per.person_id "
                "WHERE mem.sec_id = s.sec_id AND (LOWER(per.name) LIKE LOWER(%s) ESCAPE '\\\\' OR LOWER(per.email) LIKE LOWER(%s) ESCAPE '\\\\')))"
            )
            params.extend([pat, pat, pat])

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        limit_sql = f"LIMIT {int(limit)}" if limit is not None else ""
        sql = f"SELECT {cols} FROM m_maintainer_section s {where_sql} ORDER BY s.sec_id DESC {limit_sql};"
        rows = self._driver.query(sql, params, dictionary=True)
        all_keys = ("sec_id", "name", "status", "scm_tree", "web_page", "mailing_list", "ast_id", "patchwork", "bug_url", "chat_channel", "profile_doc")
        for r in rows:
            for k in all_keys:
                if k not in r:
                    r[k] = None
        return rows

    def maintainer_developer_directory(
        self,
        sec_ids: Sequence[int] | None = None,
        credit_ids: Sequence[int] | None = None,
    ) -> list[dict]:
        """Aggregate developer profiles across maintainer and credits memberships."""
        credit_where = ""
        credit_params: list[Any] = []
        if credit_ids is not None:
            if not credit_ids:
                credit_where = " AND 1=0"
            else:
                ph = ", ".join(["%s"] * len(credit_ids))
                credit_where = f" AND c.credit_id IN ({ph})"
                credit_params = list(credit_ids)

        sec_join = ""
        sec_params: list[Any] = []
        if sec_ids is not None:
            if not sec_ids:
                sec_join = " AND 1=0"
            else:
                ph = ", ".join(["%s"] * len(sec_ids))
                sec_join = f" AND s.sec_id IN ({ph})"
                sec_params = list(sec_ids)

        sql = f"""
            SELECT
                p.person_id,
                p.name,
                p.email,
                COUNT(DISTINCT s.name) AS subsystems_count,
                MAX(CASE WHEN s.sec_id IS NOT NULL AND m.role_type = 1 THEN 1 ELSE 0 END) AS is_maintainer,
                MAX(CASE WHEN s.sec_id IS NOT NULL AND m.role_type = 2 THEN 1 ELSE 0 END) AS is_reviewer,
                EXISTS(SELECT 1 FROM m_credits_entry c WHERE c.person_id = p.person_id{credit_where}) AS in_credits,
                EXISTS(SELECT 1 FROM m_commit com WHERE com.author_id = p.person_id) AS has_commits
            FROM m_maintainer_person p
            LEFT JOIN m_maintainer_member m ON p.person_id = m.person_id
            LEFT JOIN m_maintainer_section s ON m.sec_id = s.sec_id{sec_join}
            WHERE p.person_id > 0
            GROUP BY p.person_id, p.name, p.email;
        """
        params = credit_params + sec_params
        return self._driver.query(sql, params, dictionary=True)

    def find_maintainer_person(
        self,
        raw: str,
        stripped: str,
        email: str,
        name: str,
    ) -> dict | None:
        """Resolve maintainer person identity by ID, email, or name."""
        clean_raw = raw.strip("<>'\" \t\r\n")
        clean_stripped = stripped.strip("<>'\" \t\r\n")
        clean_email = email.strip("<>'\" \t\r\n")
        clean_name = name.strip("<>'\" \t\r\n")

        if clean_stripped.isdigit():
            rows = self._driver.query(
                "SELECT person_id, name, email FROM m_maintainer_person WHERE person_id = %s LIMIT 1;",
                (int(clean_stripped),),
                dictionary=True,
            )
        else:
            rows = self._driver.query(
                """
                SELECT person_id, name, email FROM m_maintainer_person
                WHERE LOWER(email) = LOWER(%s)
                   OR LOWER(name) = LOWER(%s)
                   OR LOWER(email) = LOWER(%s)
                   OR (LENGTH(%s) > 0 AND LOWER(email) = LOWER(%s))
                   OR (LENGTH(%s) > 0 AND LOWER(name) = LOWER(%s))
                ORDER BY (name = '') ASC, person_id ASC LIMIT 1;
                """,
                (clean_raw, clean_raw, clean_stripped, clean_email, clean_email, clean_name, clean_name),
                dictionary=True,
            )
        return rows[0] if rows else None

    def symbol_name_page(
        self,
        prefix: str,
        *,
        after: tuple[int, str] | None = None,
        limit: int = 400,
    ) -> list[str]:
        """Fetch distinct symbol names with keyset paging."""
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pat = f"{esc}%"
        cond = ""
        args = [pat]
        if after is not None:
            cond = " AND (LENGTH(name) > %s OR (LENGTH(name) = %s AND name > %s))"
            args.extend([after[0], after[0], after[1]])
        names: set[str] = set()
        for tbl in ("m_tag_def", "m_blob_symbol_def"):
            if self.existing_tables([tbl]):
                sql = f"SELECT name FROM {tbl} WHERE name LIKE %s ESCAPE '\\\\'{cond} GROUP BY name ORDER BY LENGTH(name), name LIMIT %s;"
                rows = self._driver.query(sql, (*args, limit), dictionary=False)
                for r in rows:
                    if r and r[0] is not None:
                        val = r[0]
                        if isinstance(val, (bytes, bytearray)):
                            val = val.decode("utf-8", errors="replace")
                        names.add(str(val))
        sorted_names = sorted(names, key=lambda n: (len(n), n))
        return sorted_names[:limit]

    def symbol_refs_of_targets(
        self,
        ast_ids: Sequence[int],
        *,
        role: int | None = None,
        with_caller: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        """Query symbol references targeting ast_ids."""
        if not ast_ids:
            return []
        from core.SymbolRows import rows_of_targets_sql
        refs_sql, repeat = rows_of_targets_sql("m_symbol_ref", len(ast_ids))
        caller_select = ", a.name AS caller" if with_caller else ""
        caller_join = "LEFT JOIN m_tag t ON r.tag_id = t.tag_id LEFT JOIN m_ast a ON a.ast_id = t.ast_id" if with_caller else ""
        where_role = f"WHERE r.role = {int(role)}" if role is not None else ""
        limit_sql = f"LIMIT {int(limit)}" if limit is not None else ""
        sql = f"""
            SELECT r.role, r.line, r.char_s, r.blob_id AS fid, r.tag_id, r.ast_id{caller_select}
            FROM ({refs_sql}) r
            {caller_join}
            {where_role}
            {limit_sql};
        """
        params = list(ast_ids) * repeat
        return self._driver.query(sql, params, dictionary=True)

    def symbol_def_blobs_of_targets(self, ast_ids: Sequence[int]) -> set[tuple[int, int]]:
        """Query distinct (ast_id, blob_id) definitions for ast_ids."""
        if not ast_ids:
            return set()
        from core.SymbolRows import rows_of_targets_sql
        defs_sql, repeat = rows_of_targets_sql("m_symbol_def", len(ast_ids))
        sql = f"SELECT DISTINCT d.ast_id, d.blob_id FROM ({defs_sql}) d;"
        params = list(ast_ids) * repeat
        rows = self._driver.query(sql, params, dictionary=False)
        return {(int(r[0]), int(r[1])) for r in rows if r and len(r) >= 2}

    def checkpoint_find_file(
        self,
        commit_id: int,
        candidates: Sequence[str],
        suffix: str | None = None,
    ) -> str | None:
        """Find best matching file path in checkpoint tree."""
        tree_src = self.resolve_relation_name("m_checkpoint_tree")
        if not candidates:
            return None
        ph = ", ".join(["%s"] * len(candidates))
        params: list[Any] = [commit_id, *candidates]
        suffix_cond = ""
        if suffix is not None:
            esc = suffix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            suffix_cond = " OR fn.fname LIKE %s ESCAPE '\\\\'"
            params.append(f"%{esc}")

        case_whens = []
        for i, c in enumerate(candidates):
            case_whens.append(f"WHEN fn.fname = %s THEN {i}")
            params.append(c)

        case_sql = f"CASE {' '.join(case_whens)} ELSE {len(candidates)} END"

        sql = f"""
            SELECT fn.fname
            FROM m_file_name fn
            JOIN {tree_src} bc ON fn.fnid = bc.fnid
            JOIN m_file_blob fb ON bc.blob_id = fb.blob_id
            WHERE bc.commit_id = %s AND fb.ftype != 0 AND (fn.fname IN ({ph}){suffix_cond})
            ORDER BY {case_sql}, LENGTH(fn.fname) ASC
            LIMIT 1;
        """
        rows = self._driver.query(sql, params, dictionary=False)
        if rows and rows[0] and rows[0][0] is not None:
            val = rows[0][0]
            if isinstance(val, (bytes, bytearray)):
                val = val.decode("utf-8", errors="replace")
            return str(val)
        return None


@contextmanager
def get_db_reader() -> Generator[ReadSession, None, None]:
    """Lease a pooled connection wrapped in ReadSession with as_dict=True default."""
    pool, cnx = get_pooled_connection()
    session: ReadSession | None = None
    broken = False
    try:
        try:
            session = ReadSession(cnx, pool)
        except (mysql.connector.errors.InterfaceError, mysql.connector.errors.OperationalError):
            logger.info("Pooled database connection lost; reconnecting for reader.")
            cnx.reconnect(attempts=1, delay=0)
            session = ReadSession(cnx, pool)
        yield session
    except (mysql.connector.errors.InterfaceError, mysql.connector.errors.OperationalError):
        broken = True
        raise
    finally:
        try:
            if session is not None:
                session.close()
            if not broken and getattr(cnx, "unread_result", False):
                cnx.consume_results()
        except Exception:
            broken = True
        pool.release(cnx, broken)

