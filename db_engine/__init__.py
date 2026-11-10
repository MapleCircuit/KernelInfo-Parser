"""db_engine package - Pluggable Database Drivers for KernelInfo-Parser.

Exports:
    - BaseDBEngine: Abstract base class interface for database backends.
    - TableCommitStat: Per-table result of `commit_tables_parallel()`.
    - MariaDB: Production MySQL / MariaDB direct driver.
    - MockDB: Fast in-memory database driver for isolated unit testing.
    - get_db_engine: Factory function to retrieve engine class by name.
"""
from __future__ import annotations

from typing import Any
from db_engine.base import BaseDBEngine, TableCommitStat, ColumnInfo, TableStats, NewPairs
from db_engine.DBHandling import MariaDB
from db_engine.mock_db import MockDB, MockReadSession
from db_engine.pool import ReadAPI, ReadSession, get_db_reader
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
    NotExists,
    Select,
    Source,
    Value,
    Where,
    between,
    finished_ref,
    ge,
    gt,
    hash_prefix,
    ieq,
    is_null,
    le,
    len_lt,
    like,
    lt,
    ne,
    not_in,
    not_null,
    dedup_rows,
    find_oversize_in_term,
    has_empty_in_term,
    replace_in_term,
)
from db_engine.sqlite_store import MissingTableError, SQLiteStore, StoreError, TableSpec
from db_engine.views import ORDERED_VIEW_NAMES, VIEW_DEFINITIONS

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
    "TableCommitStat",
    "ColumnInfo",
    "TableStats",
    "NewPairs",
    "MariaDB",
    "MockDB",
    "MockReadSession",
    "ReadAPI",
    "ReadSession",
    "get_db_reader",
    "get_db_engine",
    "DB_ENGINES",
    "Source",
    "Where",
    "Cond",
    "Col",
    "Value",
    "Inc",
    "Join",
    "Agg",
    "Const",
    "Select",
    "AnyOf",
    "Exists",
    "NotExists",
    "InSelect",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "between",
    "not_in",
    "is_null",
    "not_null",
    "like",
    "ieq",
    "hash_prefix",
    "len_lt",
    "finished_ref",
    "dedup_rows",
    "find_oversize_in_term",
    "has_empty_in_term",
    "replace_in_term",
    "TableSpec",
    "StoreError",
    "MissingTableError",
    "SQLiteStore",
    "VIEW_DEFINITIONS",
    "ORDERED_VIEW_NAMES",
]
