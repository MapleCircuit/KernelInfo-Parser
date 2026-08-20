"""db_engine package - Pluggable Database Drivers for KernelInfo-Parser.

Exports:
    - BaseDBEngine: Abstract base class interface for database backends.
    - MariaDB: Production MySQL / MariaDB direct driver.
    - MockDB: Fast in-memory database driver for isolated unit testing.
    - get_db_engine: Factory function to retrieve engine class by name.
"""
from __future__ import annotations

from typing import Any
from db_engine.base import BaseDBEngine
from db_engine.DBHandling import MariaDB
from db_engine.mock_db import MockDB

DB_ENGINES: dict[str, type[BaseDBEngine]] = {
    "mariadb": MariaDB,
    "mysql": MariaDB,
    "mock": MockDB,
    "mockdb": MockDB,
    "inmemory": MockDB,
}


def get_db_engine(name: str | None = None) -> type[BaseDBEngine]:
    """Retrieve database engine driver class by name.
    
    Args:
        name: Backend name (e.g. 'mariadb', 'mock'). Defaults to 'mariadb'.
        
    Returns:
        Database engine driver class.
        
    Raises:
        ValueError: If unsupported engine name is provided.
    """
    if not name:
        return MariaDB
    key = name.strip().lower()
    if key not in DB_ENGINES:
        valid_options = ", ".join(sorted(set(DB_ENGINES.keys())))
        raise ValueError(f"Unknown database engine '{name}'. Supported options: {valid_options}")
    return DB_ENGINES[key]


__all__ = [
    "BaseDBEngine",
    "MariaDB",
    "MockDB",
    "get_db_engine",
    "DB_ENGINES",
]
