"""cache_engine - local, rebuildable caches behind a storage-neutral API (cache_engine/CACHE_API.md).

``get_cache_engine(name=None)`` returns the process-wide engine selected by ``cache.engine`` (config) or
``KERNELINFO_CACHE_ENGINE`` (default ``"sqlite"``); ``register_cache_engine`` adds implementations.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

from .base import (BYTES, FLOAT, INT, TEXT, BaseCacheEngine, CacheSpace, CacheStore, Condition, Eq, Field,
                   FieldType, In, IsNull, Prefix, Range, Record, StoreSpec, Where, as_conditions)
from .errors import (CacheBusyError, CacheCorruptError, CacheError, CacheSchemaError, CacheUnavailableError,
                     UnknownCacheEngine)

DEFAULT_ENGINE = "sqlite"
DEFAULT_CACHE_DIR = "~/.cache/kernelinfo"

_FACTORIES: dict[str, Callable[[], BaseCacheEngine]] = {}
_INSTANCES: dict[str, BaseCacheEngine] = {}   # by canonical (first registered) name
_CANON: dict[str, str] = {}
_LOCK = threading.Lock()


def _config_section() -> dict:
    try:
        from core.config import get_config
        sec = get_config().get("cache")
        return sec if isinstance(sec, dict) else {}
    except Exception:  # noqa: BLE001 - the cache must work without the application config
        return {}


def cache_root() -> Path:
    """Directory holding the caches of directory-based engines: ``KERNELINFO_CACHE_DIR``, else config
    ``cache.dir``, else ``~/.cache/kernelinfo``."""
    raw = os.environ.get("KERNELINFO_CACHE_DIR") or str(_config_section().get("dir") or "") or DEFAULT_CACHE_DIR
    return Path(raw).expanduser()


def register_cache_engine(name: str, factory: Callable[[], BaseCacheEngine], aliases: tuple[str, ...] = ()) -> None:
    """Register an engine class/factory (no-argument callable) under ``name`` (and aliases)."""
    with _LOCK:
        _INSTANCES.pop(name.lower(), None)
        for n in (name, *aliases):
            _FACTORIES[n.lower()] = factory
            _CANON[n.lower()] = name.lower()


def available_cache_engines(*, include_aliases: bool = False) -> tuple[str, ...]:
    with _LOCK:
        names = sorted(_FACTORIES)
        if include_aliases:
            return tuple(names)
        seen: set[int] = set()
        primary = []
        for n in names:
            if id(_FACTORIES[n]) not in seen:
                seen.add(id(_FACTORIES[n]))
                primary.append(n)
        return tuple(primary)


def configured_engine_name() -> str:
    return str(os.environ.get("KERNELINFO_CACHE_ENGINE") or _config_section().get("engine") or DEFAULT_ENGINE).lower()


def get_cache_engine(name: str | None = None) -> BaseCacheEngine:
    """The shared engine instance registered as ``name`` (default: the configured one)."""
    key = (name or configured_engine_name()).lower()
    with _LOCK:
        key = _CANON.get(key, key)
        inst = _INSTANCES.get(key)
        if inst is not None:
            return inst
        factory = _FACTORIES.get(key)
        if factory is None:
            raise UnknownCacheEngine(f"unknown cache engine {key!r} (available: {', '.join(sorted(_FACTORIES))})")
        inst = _INSTANCES[key] = factory()
        return inst


def _register_builtin() -> None:
    from .sqlite_cache import SQLiteCacheEngine
    register_cache_engine("sqlite", SQLiteCacheEngine, ("sqlite3",))


_register_builtin()
