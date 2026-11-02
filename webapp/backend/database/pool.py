"""webapp/backend/database/pool.py - Resilient Database Connection Pool."""
from __future__ import annotations
import os
import ssl
import threading
import time
import logging
from contextlib import contextmanager
from typing import Generator, Any
import mysql.connector
import mysql.connector.errors
from webapp.backend.config import get_backend_db_config

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

# How long a request waits for a connection of a saturated pool before failing
POOL_WAIT_S = 10.0
POOL_SIZE = 32


class ConnectionPool:
    """Bounded pool of database connections, opened on demand.

    Replaces mysql.connector's MySQLConnectionPool, which pinged every connection it lent (holding a
    process-wide lock across the round trip) and reset its session when given back, on top of the ping
    `cursor()` itself makes: three round trips per cursor, costly over the SSH tunnel. The webapp never
    changes session state (read only, autocommit, no session variables, temporary tables or locks), so a
    connection goes back as it is; get_db_cursor reconnects one that `cursor()` finds dropped.
    """

    def __init__(self, connect_kwargs: dict[str, Any], size: int = POOL_SIZE) -> None:
        self._kwargs = connect_kwargs
        self.size = max(1, size)
        self._idle: list[Any] = []  # most recently given back last
        self._open = 0  # connections lent or idle
        self._closed = False
        self._cond = threading.Condition()

    def acquire(self, timeout: float = POOL_WAIT_S) -> Any:
        """Lend a connection, waiting up to `timeout` s while all `size` are lent (PoolError after)."""
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
# Creation of the pool (and of the SSH tunnel before it) happens once, whatever the threads asking
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
        cfg = get_backend_db_config()

        # Automatically start SSH tunnel if enabled in configuration
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

        # Attempt Unix domain socket connection for local non-tunneled configurations
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
    """Acquire a connection: (the pool it goes back to, connection).

    A saturated pool (every connection lent, PoolError) is waited on for up to POOL_WAIT_S: the
    connections come back as other requests finish. A failure to connect (InterfaceError,
    OperationalError: the database or the tunnel dropped) resets the pool, at most `max_retries` times.
    """
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


@contextmanager
def get_db_cursor() -> Generator[Any, None, None]:
    """Provide a managed database cursor, guaranteeing return of connection to pool.

    Enforces the web application's strict read-only database invariant (Rule 35).
    All database mutations and commits are strictly forbidden in webapp backend services.
    A connection that failed (lost, or left with a result it cannot drain) is closed, not reused.
    """
    pool, cnx = get_pooled_connection()
    cursor = None
    broken = False
    try:
        try:
            cursor = cnx.cursor(dictionary=True)
        except (mysql.connector.errors.InterfaceError, mysql.connector.errors.OperationalError):
            # cursor() pings: the connection was dropped while idle (server restart, tunnel drop)
            logger.info("Pooled database connection was lost; reconnecting.")
            cnx.reconnect(attempts=1, delay=0)
            cursor = cnx.cursor(dictionary=True)
        yield cursor
    except (mysql.connector.errors.InterfaceError, mysql.connector.errors.OperationalError):
        broken = True
        raise
    finally:
        try:
            if cursor is not None:
                cursor.close()
            if not broken and getattr(cnx, "unread_result", False):
                cnx.consume_results()
        except Exception:
            broken = True
        pool.release(cnx, broken)
