"""db_engine package - Pluggable Database Drivers for KernelInfo-Parser.

Exports:
    - BaseDBEngine: Abstract base class interface for database backends.
    - TableCommitStat: Per-table result of `commit_tables_parallel()`.
    - MariaDB: Production MySQL / MariaDB direct driver.
    - DBError / UnsupportedOperation: Neutral exceptions raised by drivers (`db_engine/errors.py`).
    - MockDB: Fast in-memory database driver for isolated unit testing.
    - get_db_engine: Factory function to retrieve engine class by name.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from db_engine.base import (
    BaseDBEngine,
    TableCommitStat,
    ColumnInfo,
    TableStats,
    NewPairs,
    ProbeResult,
    CAP_BACKGROUND_COMMIT,
    CAP_CONCURRENT_CONNECTIONS,
    CAP_PERSISTENT,
    CAP_SERVER_TUNING,
    tuning_note_is_warning,
)
from db_engine.DBHandling import MariaDB
from db_engine.errors import (
    ConnectionLostError,
    DBError,
    DuplicateKeyError,
    MissingTableError,
    PacketTooLargeError,
    ConfigurationError,
    ReadOnlyViolation,
    SpecError,
    UnsupportedOperation,
)
from db_engine.mock_db import MockDB, MockReadSession
from db_engine.pool import PoolConfigError, ReadAPI, ReadSession, configure_pool, get_db_reader
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
    all_of,
    any_of,
    between,
    eq,
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
from db_engine.spec import (
    CURRENT_TIMESTAMP,
    NO_DEFAULT,
    Arith,
    Case,
    Coalesce,
    ColumnSpec,
    ColumnType,
    Concat,
    ExistsQuery,
    Expr,
    Hex,
    InQuery,
    Incoming,
    Length,
    NullIf,
    Order,
    Pred,
    Substr,
    add,
    exists_select,
    in_select,
    mul,
    sub,
)
from db_engine.foreign import ForeignReader, ForeignWriter, create_foreign_sqlite, open_foreign_sqlite
from db_engine.sqlite_db import SQLiteDB, SQLiteReadSession
from db_engine.sqlite_store import SQLiteStore, StoreError, TableSpec
from db_engine.views import DEF_ID_FLAG, KIND_DEFS, KIND_REFS, ORDERED_VIEW_NAMES, VIEW_DEFINITIONS

DB_ENGINES: dict[str, type[BaseDBEngine]] = {
    "mariadb": MariaDB,
    "mysql": MariaDB,
    "mock": MockDB,
    "mockdb": MockDB,
    "inmemory": MockDB,
    "sqlite": SQLiteDB,
    "sqlite3": SQLiteDB,
}

# canonical engine name -> aliases; the canonical names are what `available_engines()` offers to UIs and CLIs
_ENGINE_ALIASES: dict[str, tuple[str, ...]] = {
    "mariadb": ("mysql",),
    "mock": ("mockdb", "inmemory"),
    "sqlite": ("sqlite3",),
}


def register_engine(name: str, cls: type[BaseDBEngine], aliases: Sequence[str] = ()) -> None:
    """Register `cls` under the canonical `name` (and `aliases`) for `get_db_engine()` / `available_engines()`."""
    DB_ENGINES[name.lower()] = cls
    _ENGINE_ALIASES[name.lower()] = tuple(a.lower() for a in aliases)
    for alias in aliases:
        DB_ENGINES[alias.lower()] = cls


def available_engines(*, include_aliases: bool = False) -> tuple[str, ...]:
    """Names accepted for `database.engine`: the canonical ones (default) or also their aliases, in stable order."""
    names: list[str] = []
    for canonical, aliases in _ENGINE_ALIASES.items():
        names.append(canonical)
        if include_aliases:
            names.extend(aliases)
    return tuple(names)


def default_socket_paths(name: str | None = None) -> tuple[str, ...]:
    """Candidate local server socket paths of the engine `name` (default engine when None); empty when it has none."""
    return get_db_engine(name).default_socket_paths


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
    "PoolConfigError",
    "configure_pool",
    "DEF_ID_FLAG",
    "KIND_DEFS",
    "KIND_REFS",
    "SQLiteDB",
    "SQLiteReadSession",
    "BaseDBEngine",
    "DBError",
    "UnsupportedOperation",
    "SpecError",
    "DuplicateKeyError",
    "ConnectionLostError",
    "PacketTooLargeError",
    "ReadOnlyViolation",
    "ConfigurationError",
    "ProbeResult",
    "CAP_BACKGROUND_COMMIT",
    "CAP_CONCURRENT_CONNECTIONS",
    "CAP_PERSISTENT",
    "CAP_SERVER_TUNING",
    "tuning_note_is_warning",
    "register_engine",
    "available_engines",
    "default_socket_paths",
    "ForeignReader",
    "open_foreign_sqlite",
    "create_foreign_sqlite",
    "ForeignWriter",
    "ColumnSpec",
    "ColumnType",
    "NO_DEFAULT",
    "CURRENT_TIMESTAMP",
    "Expr",
    "Concat",
    "Length",
    "Substr",
    "Coalesce",
    "NullIf",
    "Arith",
    "Hex",
    "Case",
    "Incoming",
    "Order",
    "Pred",
    "InQuery",
    "ExistsQuery",
    "in_select",
    "exists_select",
    "add",
    "sub",
    "mul",
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
    "eq",
    "all_of",
    "any_of",
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
