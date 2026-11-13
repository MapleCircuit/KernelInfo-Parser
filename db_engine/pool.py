"""db_engine/pool.py - Resilient Database Connection Pool & Session Management."""
from __future__ import annotations

import logging
import os
import ssl
import threading
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import mysql.connector
import mysql.connector.errors

from db_engine.base import ColumnInfo, TableStats
from db_engine.errors import ConnectionLostError, ReadOnlyViolation
from db_engine.read_intents import ReadIntents
from db_engine.spec import (
    Agg,
    Const,
    Join,
    Select,
    Source,
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


_CONFIG_PROVIDER: Callable[[], dict[str, Any]] | None = None


class PoolConfigError(RuntimeError):
    """Raised when the pool has no usable connection configuration."""


def configure_pool(config_provider: Callable[[], dict[str, Any]] | dict[str, Any]) -> None:
    """Inject the connection configuration source (a `database` config section or a callable returning one).

    A callable is re-evaluated whenever the pool is built, so values written by a later SSH tunnel start are seen.
    """
    global _CONFIG_PROVIDER
    _CONFIG_PROVIDER = config_provider if callable(config_provider) else (lambda: config_provider)


def _load_config() -> dict[str, Any]:
    if _CONFIG_PROVIDER is None:
        raise PoolConfigError(
            "Connection pool is not configured: call configure_pool(cfg) before the first read "
            "- see DB_API.md 'Webapp Read Session & Query Primitives'"
        )
    return _CONFIG_PROVIDER()


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
        cfg = _load_config()

        ssh_tunnel_cfg = cfg.get("ssh_tunnel") or {}
        ssh_tunnel_active = bool(ssh_tunnel_cfg.get("enabled", False))
        if ssh_tunnel_active:
            from core.ssh_tunnel import is_ssh_tunnel_active, start_ssh_tunnel

            if not is_ssh_tunnel_active("database"):
                logger.info("Starting database SSH tunnel for web application...")
                try:
                    start_ssh_tunnel(ssh_tunnel_cfg, feature="database")
                    cfg = _load_config()
                except Exception as exc:
                    logger.error("Failed to start database SSH tunnel: %s", exc)
                    raise

        missing = [k for k in ("user", "password") if not cfg.get(k)]
        if missing:
            raise PoolConfigError(
                f"Database configuration is missing required credential(s): {', '.join(missing)}; set database.user / "
                "database.password in config.json - see DB_API.md 'Webapp Read Session & Query Primitives'"
            )
        connect_kwargs: dict[str, Any] = {
            "host": cfg.get("host", "127.0.0.1"),
            "port": int(cfg.get("port", 3306)),
            "user": cfg["user"],
            "password": cfg["password"],
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
            raise ConnectionLostError(str(e)) from e
        except (mysql.connector.errors.InterfaceError, mysql.connector.errors.OperationalError) as e:
            failures += 1
            if failures >= max_retries:
                logger.error("Database connection acquisition failed after %d retries: %s", max_retries, e)
                raise ConnectionLostError(str(e)) from e
            logger.warning("Database connection error (%s); resetting pool...", e)
            with _POOL_LOCK:
                if _POOL is pool:
                    _POOL = None
            pool.close()
            time.sleep(backoff)
            backoff *= 2
            pool = get_db_pool()


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
        group_by: Sequence[Any] = (),
        having: Where | None = None,
        hint: str | None = None,
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
        group_by: Sequence[Any] = (),
        having: Where | None = None,
        hint: str | None = None,
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
        order_by: Sequence[Any] = (),
        as_dict: bool | None = None,
        having: Where | None = None,
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


_POOLLESS_STATE: dict[str, Any] = {}


class ReadSession(ReadIntents, ReadAPI):
    """Managed read session bound to a single pooled connection with as_dict=True default."""

    def __init__(self, cnx: Any, pool: ConnectionPool) -> None:
        self._cnx = cnx
        self._pool = pool
        self._cursor = cnx.cursor(dictionary=True)
        from db_engine.DBHandling import MariaDB
        self._driver = MariaDB.__new__(MariaDB)
        self._driver.cnx = self._cnx
        self._driver._cur = self._cursor
        self._driver.read_only = True
        self._closed = False

    #: Write / DDL entry points a read session refuses (`ReadOnlyViolation`).
    _WRITE_METHODS = frozenset({
        "execute", "executemany", "insert", "update", "insert_rows", "update_rows", "delete_rows", "copy_rows",
        "delete", "drop_tables", "drop_table", "create_table", "create_view", "drop_view", "add_column",
        "add_column_if_missing", "create_index", "remove_index", "ensure_columns", "ensure_views",
        "enqueue_parse_tasks", "record_task_failure", "get_or_insert_id", "commit", "rollback",
        "apply_server_tuning",
    })

    def __getattr__(self, name: str) -> Any:
        if name in ReadSession._WRITE_METHODS:
            def _refuse(*args: Any, **kwargs: Any) -> Any:
                raise ReadOnlyViolation(
                    f"ReadSession is read-only: {name}() needs the writer; the webapp never writes, parse in the parser "
                    "pipeline - see DB_API.md 'Webapp Read Session & Query Primitives'"
                )

            return _refuse
        raise AttributeError(name)

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
        group_by: Sequence[Any] = (),
        having: Where | None = None,
        hint: str | None = None,
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
            group_by=group_by,
            having=having,
            hint=hint,
        )

    def select_spec(self, select: Select, *, as_dict: bool | None = None) -> list[tuple | dict]:
        """Run a complete `Select` specification (dict rows by default)."""
        return self.select_rows(
            select.source, select.columns, select.where, joins=select.joins, distinct=select.distinct,
            order_by=select.order_by, limit=select.limit, offset=select.offset, as_dict=as_dict,
            group_by=select.group_by, having=select.having, hint=select.hint,
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
        group_by: Sequence[Any] = (),
        having: Where | None = None,
        hint: str | None = None,
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
            group_by=group_by,
            having=having,
            hint=hint,
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
        order_by: Sequence[Any] = (),
        as_dict: bool | None = None,
        having: Where | None = None,
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
            having=having,
        )

    def max_value(self, source: Source, column: str, where: Where | None = None, *, default: Any = 0) -> Any:
        return self._driver.max_value(source, column, where=where, default=default)

    def min_value(self, source: Source, column: str, where: Where | None = None, *, default: Any = 0) -> Any:
        return self._driver.min_value(source, column, where=where, default=default)

    # ---- §4 Catalog and Logical Relations (§5) -------------------------------
    def _state(self) -> dict[str, Any]:
        """Process-wide state shared by the sessions of one pool (a pool-less session shares the module's)."""
        return self._pool.__dict__ if self._pool is not None else _POOLLESS_STATE

    def _catalog_cached(self, kind: str, key: Any, compute: Callable[[], Any], *, forever: bool) -> Any:
        """Catalog answer remembered on the pool (process-wide, one catalog query instead of one per request):
        a truthy answer for the pool's lifetime when `forever` (columns and indexes only ever get added), anything
        else for 5 minutes (a database migrated while the webapp runs)."""
        cache = self._state().setdefault("_catalog_cache", {})
        now = time.monotonic()
        hit = cache.get((kind, key))
        if hit is not None and ((forever and hit[0]) or now - hit[1] < 300.0):
            return hit[0]
        value = compute()
        cache[(kind, key)] = (value, now)
        return value

    def column_exists(self, table: Table | str, column: str) -> bool:
        name = table if isinstance(table, str) else table.table_name
        return bool(self._catalog_cached("column", (name, column), lambda: self._driver.column_exists(table, column), forever=True))

    def index_names(self, table: Table | str) -> set[str]:
        name = table if isinstance(table, str) else table.table_name
        return set(self._catalog_cached("index", name, lambda: self._driver.index_names(table), forever=False))

    def existing_tables(self, names: Sequence[str], *, include_views: bool = False) -> set[str]:
        return self._driver.existing_tables(names, include_views=include_views)

    def column_info(self, table: Table | str, column: str) -> ColumnInfo | None:
        return self._driver.column_info(table, column)

    def table_stats(self, names: Sequence[str] | None = None) -> list[TableStats]:
        return self._driver.table_stats(names)

    def resolve_relation_name(self, name: str) -> str:
        # The engine's per-instance answer cache lives on the pool: one catalog check per process, not per request
        self._driver._relation_name_cache = self._state().setdefault("_relation_name_cache", {})
        return self._driver.resolve_relation_name(name)

    # ---- §7.2 Webapp Intent Reads --------------------------------------------
    # Every intent read (list_network_commits, maintainer_*, find_maintainer_person, symbol_name_page,
    # symbol_refs_of_targets, symbol_def_blobs_of_targets, checkpoint_tree_*, checkpoint_find_file) is inherited
    # from `ReadIntents` (db_engine/read_intents.py), shared with the SQLite and mock sessions.


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
    except (mysql.connector.errors.InterfaceError, mysql.connector.errors.OperationalError, ConnectionLostError):
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

