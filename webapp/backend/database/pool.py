"""webapp/backend/database/pool.py - Resilient Database Connection Pool."""
from __future__ import annotations
import time
import logging
from contextlib import contextmanager
from typing import Generator, Any
import mysql.connector
from mysql.connector import pooling
from webapp.backend.config import get_backend_db_config

logger = logging.getLogger(__name__)

_POOL: pooling.MySQLConnectionPool | None = None


def get_db_pool() -> pooling.MySQLConnectionPool:
    """Initialize or return the thread-safe MySQL connection pool."""
    global _POOL
    if _POOL is None:
        cfg = get_backend_db_config()
        _POOL = pooling.MySQLConnectionPool(
            pool_name="kernelinfo_webapp_pool",
            pool_size=32,
            pool_reset_session=True,
            host=cfg.get("host", "127.0.0.1"),
            port=int(cfg.get("port", 3306)),
            user=cfg.get("user", "root"),
            password=cfg.get("password", "Passe123"),
            database=cfg.get("database", "test"),
            connection_timeout=int(cfg.get("timeout", 10)),
            autocommit=True,
        )
        logger.info("Initialized MySQLConnectionPool with 32 connections.")
    return _POOL


def get_pooled_connection(max_retries: int = 3) -> Any:
    """Acquire a connection from pool with exponential backoff on saturation."""
    pool = get_db_pool()
    backoff = 0.05
    for attempt in range(max_retries):
        try:
            return pool.get_connection()
        except mysql.connector.errors.PoolError as e:
            if attempt == max_retries - 1:
                logger.error("Database connection pool exhausted after %d retries: %s", max_retries, e)
                raise
            time.sleep(backoff)
            backoff *= 2


@contextmanager
def get_db_cursor(commit: bool = False, dictionary: bool = True) -> Generator[Any, None, None]:
    """Provide a managed database cursor, guaranteeing return of connection to pool."""
    cnx = get_pooled_connection()
    cursor = cnx.cursor(dictionary=dictionary)
    try:
        yield cursor
        if commit:
            cnx.commit()
    finally:
        cursor.close()
        cnx.close()


class DatabaseManager:
    """Manages database connection parameters according to core.config (Rule 26)."""

    def __init__(self) -> None:
        cfg = get_backend_db_config()
        self.host = cfg.get("host", "127.0.0.1")
        self.port = int(cfg.get("port", 3306))
        self.user = cfg.get("user", "root")
        self.password = cfg.get("password", "Passe123")
        self.database = cfg.get("database", "test")
        self.timeout = int(cfg.get("timeout", 10))
        self._init_pool()

    def _init_pool(self) -> None:
        get_db_pool()

    def get_connection(self) -> Any:
        return get_pooled_connection()


db = DatabaseManager()

