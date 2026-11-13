"""cache_engine/errors.py - Neutral exceptions of the cache engines (cache_engine/CACHE_API.md, Errors).

No driver exception (``sqlite3.Error`` ...) crosses the engine boundary: every implementation translates its own
failures into these classes, so callers only ever catch ``CacheError``.
"""
from __future__ import annotations


class CacheError(Exception):
    """Any failure of a cache engine; callers treat the cache as unavailable and rebuild or skip it."""


class CacheUnavailableError(CacheError):
    """The storage could not be opened or created (unwritable directory, missing permissions, disk full)."""


class CacheBusyError(CacheError):
    """Another process or thread kept the store locked beyond the engine's timeout."""


class CacheCorruptError(CacheError):
    """The stored data is unreadable; discard the space (``CacheSpace.discard()``) and rebuild it."""


class CacheSchemaError(CacheError, ValueError):
    """A store declaration, field name, condition or record does not fit the declared schema (a caller bug), or a
    space opened with a schema that differs from the one it holds."""


class UnknownCacheEngine(CacheError, KeyError):
    """``get_cache_engine(name)`` was given a name no engine is registered under."""

    def __str__(self) -> str:  # KeyError would quote the message
        return Exception.__str__(self)
