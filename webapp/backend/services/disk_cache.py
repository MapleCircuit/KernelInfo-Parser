"""webapp/backend/services/disk_cache.py - Persistent cache of derived data (webapp/API.md section 2.14).

Expensive results computed from Git (Kconfig models, indexes, per-release passes, reports) survive
server restarts as gzipped JSON files under ``KERNELINFO_CACHE_DIR`` (default
``~/.cache/kernelinfo``), one directory per namespace. This lives outside the database, which the
webapp never writes (section 2.5).

Rules for callers:
- keys include the repository name and the fingerprint of the producing code (``fingerprint()``);
- failures, partial and timed-out results are never stored.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SUFFIX = ".json.gz"
_BIN_SUFFIX = ".bin"
_SUFFIXES = (_SUFFIX, _BIN_SUFFIX)
_NAMESPACE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_KEY_RE = re.compile(r"[0-9a-f]{16,64}")
_DEFAULT_TOTAL_MB = 1024


def cache_root() -> Path:
    """Directory holding every namespace (``cache_engine.cache_root()``: ``KERNELINFO_CACHE_DIR``, config
    ``cache.dir``, else ``~/.cache/kernelinfo``)."""
    from cache_engine import cache_root as _root
    return _root()


def total_cap_bytes() -> int:
    """Global cap over all namespaces, from ``webapp.cache.max_mb`` (default 1024 MB)."""
    try:
        from core.config import get_webapp_config
        mb = (get_webapp_config().get("cache") or {}).get("max_mb", _DEFAULT_TOTAL_MB)
        return max(1, int(mb)) << 20
    except Exception:
        return _DEFAULT_TOTAL_MB << 20


_FINGERPRINTS: dict[tuple[str, ...], str] = {}
_FP_LOCK = threading.Lock()


def fingerprint(*paths: str | Path, salt: str = "") -> str:
    """Hash of the source files that shape a cached result (computed once per process).

    A missing file hashes its name, so the fingerprint stays stable but still differs from the
    one of a present file."""
    key = (salt, *(str(p) for p in paths))
    with _FP_LOCK:
        cached = _FINGERPRINTS.get(key)
        if cached is not None:
            return cached
    h = hashlib.sha1(salt.encode())
    for p in paths:
        try:
            h.update(Path(p).read_bytes())
        except OSError:
            h.update(str(p).encode())
    value = h.hexdigest()
    with _FP_LOCK:
        _FINGERPRINTS[key] = value
    return value


def module_fingerprint(*module_files: str | Path, salt: str = "") -> str:
    """``fingerprint()`` of Python source files given relative to the repository root."""
    root = Path(__file__).resolve().parents[3]
    return fingerprint(*(root / p for p in module_files), salt=salt)


class DiskCache:
    """One namespace of the disk cache: ``get(key)`` / ``put(key, obj)``, LRU eviction by mtime.

    ``key`` may be any string: names are hashed unless already a hex digest (which keeps the file
    names of caches that predate this helper, such as ``kconfig-models``)."""

    suffix = _SUFFIX

    def __init__(self, namespace: str, max_bytes: int = 256 << 20, max_entries: int | None = None) -> None:
        if not _NAMESPACE_RE.fullmatch(namespace):
            raise ValueError(f"invalid disk cache namespace: {namespace!r}")
        self.namespace = namespace
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        return cache_root() / self.namespace

    @staticmethod
    def file_key(key: str) -> str:
        return key if _KEY_RE.fullmatch(key) else hashlib.sha1(key.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        return self.directory / f"{self.file_key(key)}{self.suffix}"

    def get(self, key: str) -> Any | None:
        path = self.path_for(key)
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, EOFError):
            return None
        try:
            os.utime(path)  # least recently used eviction by mtime
        except OSError:
            pass
        return data

    def contains(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def put(self, key: str, obj: Any) -> bool:
        """Store ``obj`` atomically (temporary file, then rename). Never raises; returns success."""
        directory = self.directory
        name = self.file_key(key)
        tmp = directory / f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
                json.dump(obj, fh, separators=(",", ":"))
            os.replace(tmp, directory / f"{name}{self.suffix}")
        except (OSError, TypeError, ValueError) as exc:
            logger.debug("Disk cache %s: %s not stored: %s", self.namespace, name, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        self.evict()
        return True

    def delete(self, key: str) -> None:
        try:
            self.path_for(key).unlink(missing_ok=True)
        except OSError:
            pass

    def entries(self) -> list[tuple[Path, int, float]]:
        """(path, size, mtime) of the namespace's entries, oldest first."""
        out = []
        try:
            for p in self.directory.glob(f"*{self.suffix}"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append((p, st.st_size, st.st_mtime))
        except OSError:
            return []
        out.sort(key=lambda e: e[2])
        return out

    def size_bytes(self) -> int:
        return sum(size for _p, size, _m in self.entries())

    def evict(self) -> int:
        """Drop the least recently used entries beyond this namespace's caps and the global cap."""
        removed = 0
        with self._lock:
            entries = self.entries()
            total = sum(size for _p, size, _m in entries)
            limit = min(self.max_bytes, total_cap_bytes())
            keep_count = self.max_entries if self.max_entries is not None else len(entries)
            while entries and (total > limit or len(entries) > keep_count):
                path, size, _mtime = entries.pop(0)
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
                total -= size
            removed += _evict_global(exclude=None)
        return removed


class BinaryDiskCache(DiskCache):
    """A namespace of raw byte entries (``get_bytes`` / ``put_bytes``), e.g. assembled indexes written in their
    own format; same LRU eviction and global cap as the JSON entries."""

    suffix = _BIN_SUFFIX

    def get_bytes(self, key: str) -> bytes | None:
        path = self.path_for(key)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:
            os.utime(path)
        except OSError:
            pass
        return data

    def put_bytes(self, key: str, data: bytes) -> bool:
        """Store ``data`` atomically. Never raises; returns success."""
        directory = self.directory
        name = self.file_key(key)
        tmp = directory / f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, directory / f"{name}{self.suffix}")
        except OSError as exc:
            logger.debug("Disk cache %s: %s not stored: %s", self.namespace, name, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        self.evict()
        return True

    def get(self, key: str) -> Any | None:  # noqa: D102 - raw bytes only
        raise TypeError("BinaryDiskCache holds bytes: use get_bytes()")

    def put(self, key: str, obj: Any) -> bool:  # noqa: D102 - raw bytes only
        raise TypeError("BinaryDiskCache holds bytes: use put_bytes()")


def _evict_global(exclude: Path | None) -> int:
    """Keep the sum of every namespace under ``webapp.cache.max_mb`` (oldest files go first)."""
    root = cache_root()
    try:
        files = []
        for p in (q for suffix in _SUFFIXES for q in root.glob(f"*/*{suffix}")):
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((p, st.st_size, st.st_mtime))
    except OSError:
        return 0
    total = sum(s for _p, s, _m in files)
    cap = total_cap_bytes()
    if total <= cap:
        return 0
    files.sort(key=lambda e: e[2])
    removed = 0
    for path, size, _mtime in files:
        if total <= cap:
            break
        if exclude is not None and path == exclude:
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
        total -= size
    return removed


def usage() -> list[dict[str, Any]]:
    """Per-namespace size and entry count (used by the status page, G11)."""
    root = cache_root()
    out = []
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    for d in dirs:
        size = count = 0
        for p in (q for suffix in _SUFFIXES for q in d.glob(f"*{suffix}")):
            try:
                size += p.stat().st_size
                count += 1
            except OSError:
                continue
        out.append({"name": d.name, "entries": count, "bytes": size})
    return out
