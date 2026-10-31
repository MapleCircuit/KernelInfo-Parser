"""webapp/backend/database/pool.py - Resilient Database Connection Pool."""
from __future__ import annotations
import os
import ssl
import time
import logging
from contextlib import contextmanager
from typing import Generator, Any
import mysql.connector
from mysql.connector import pooling
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

_POOL: pooling.MySQLConnectionPool | None = None


def get_db_pool() -> pooling.MySQLConnectionPool:
    """Initialize or return the thread-safe MySQL connection pool."""
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

        pool_kwargs: dict[str, Any] = {
            "pool_name": "kernelinfo_webapp_pool",
            "pool_size": 32,
            "pool_reset_session": True,
            "host": cfg.get("host", "127.0.0.1"),
            "port": int(cfg.get("port", 3306)),
            "user": cfg.get("user", "root"),
            "password": cfg.get("password", "Passe123"),
            "database": cfg.get("database", "test"),
            "connection_timeout": max(int(cfg.get("timeout", 10)), 30),
            "autocommit": True,
        }
        if os.getenv("DB_SSL_DISABLED", "").lower() in ("true", "1", "yes") or cfg.get("ssl_disabled"):
            pool_kwargs["ssl_disabled"] = True

        # Attempt Unix domain socket connection for local non-tunneled configurations
        socket_path = cfg.get("resolved_unix_socket") or os.getenv("DB_SOCKET") or os.getenv("MYSQL_UNIX_PORT")
        if not ssh_tunnel_active and socket_path and os.path.exists(socket_path):
            pool_kwargs["unix_socket"] = socket_path
            try:
                _POOL = pooling.MySQLConnectionPool(**pool_kwargs)
                logger.info("Initialized MySQLConnectionPool with 32 connections via Unix socket (%s).", socket_path)
                return _POOL
            except Exception as e:
                logger.warning(
                    "Failed to initialize MySQLConnectionPool via Unix socket '%s' (%s); falling back to TCP...",
                    socket_path,
                    e,
                )
                pool_kwargs.pop("unix_socket", None)

        _POOL = pooling.MySQLConnectionPool(**pool_kwargs)
        logger.info(
            "Initialized MySQLConnectionPool with 32 connections via TCP (%s:%s).",
            pool_kwargs.get("host"),
            pool_kwargs.get("port"),
        )
    return _POOL


def close_db_pool() -> None:
    """Reset and release the active database connection pool."""
    global _POOL
    _POOL = None


def get_pooled_connection(max_retries: int = 3) -> Any:
    """Acquire a connection from pool with exponential backoff on saturation or connection drops."""
    global _POOL
    pool = get_db_pool()
    backoff = 0.05
    for attempt in range(max_retries):
        try:
            cnx = pool.get_connection()
            if hasattr(cnx, "ping"):
                try:
                    cnx.ping(reconnect=True, attempts=1, delay=0)
                except Exception:
                    pass
            return cnx
        except (
            mysql.connector.errors.PoolError,
            mysql.connector.errors.InterfaceError,
            mysql.connector.errors.OperationalError,
        ) as e:
            if attempt == max_retries - 1:
                logger.error("Database connection acquisition failed after %d retries: %s", max_retries, e)
                raise
            if isinstance(e, (mysql.connector.errors.InterfaceError, mysql.connector.errors.OperationalError)):
                logger.warning("Database connection error (%s); resetting pool...", e)
                _POOL = None
            time.sleep(backoff)
            backoff *= 2
            pool = get_db_pool()


@contextmanager
def get_db_cursor(dictionary: bool = True) -> Generator[Any, None, None]:
    """Provide a managed database cursor, guaranteeing return of connection to pool.

    Enforces the web application's strict read-only database invariant (Rule 35).
    All database mutations and commits are strictly forbidden in webapp backend services.
    """
    cnx = get_pooled_connection()
    cursor = cnx.cursor(dictionary=dictionary)
    try:
        yield cursor
    finally:
        cursor.close()
        cnx.close()


class DatabaseManager:
    """Manages database connection parameters according to core.config (Rule 26)."""

    def __init__(self) -> None:
        self._init_pool()
        cfg = get_backend_db_config()
        self.host = cfg.get("host", "127.0.0.1")
        self.port = int(cfg.get("port", 3306))
        self.user = cfg.get("user", "root")
        self.password = cfg.get("password", "Passe123")
        self.database = cfg.get("database", "test")
        self.timeout = int(cfg.get("timeout", 10))

    def _init_pool(self) -> None:
        try:
            get_db_pool()
        except Exception as exc:
            logger.debug("Database pool initialization deferred on import: %s", exc)

    def get_connection(self) -> Any:
        return get_pooled_connection()


db = DatabaseManager()

