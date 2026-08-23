"""table_engine - TableEngine Subsystem.

Provides stateful in-memory caching, sequence tracking, and relational view coordination
between ChangeSets and Database storage drivers.
"""
from typing import Any
from table_engine.te_direct_db import TEDirectDB
from table_engine.te_cached_db import TECachedDB

TABLE_ENGINE_MAP: dict[str, type[TEDirectDB | TECachedDB]] = {
    "cached": TECachedDB,
    "tecacheddb": TECachedDB,
    "direct": TEDirectDB,
    "tedirectdb": TEDirectDB,
}


def get_table_engine(name: str | type[TEDirectDB | TECachedDB] | None = None) -> type[TEDirectDB | TECachedDB]:
    """Retrieve TableEngine class by name or alias.

    Args:
        name: Name of the engine ('cached', 'direct', 'tecacheddb', 'tedirectdb'), or engine class.

    Returns:
        TECachedDB or TEDirectDB class.

    Raises:
        ValueError: If the engine name is unrecognized.
        TypeError: If name is not a string, engine class, or None.
    """
    if name is None:
        return TECachedDB
    if isinstance(name, type) and issubclass(name, (TEDirectDB, TECachedDB)):
        return name
    if isinstance(name, str):
        normalized = name.strip().lower()
        if normalized in TABLE_ENGINE_MAP:
            return TABLE_ENGINE_MAP[normalized]
        valid = ", ".join(sorted(set(TABLE_ENGINE_MAP.keys())))
        raise ValueError(f"Unknown table engine '{name}'. Supported engines: {valid}")
    raise TypeError(f"Invalid table engine identifier: {name!r}")


__all__ = ["TEDirectDB", "TECachedDB", "get_table_engine", "TABLE_ENGINE_MAP"]
