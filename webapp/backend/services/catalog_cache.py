"""webapp/backend/services/catalog_cache.py - Catalog responses built once per repository state.

The version and branch catalogs only change when the active repository's `parsed` or `git` sync token
changes (sync_service, webapp/API.md section 2.11). `TokenCache` builds such a value once per token pair
(concurrent requests share one build); `JsonBody` holds a response body encoded once, raw and gzip, with
its ETag; `respond()` sends it, or 304 when the request's If-None-Match names it (webapp/API.md section 2.13).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from typing import Any, Callable, Generic, TypeVar

from fastapi import Request, Response

from webapp.backend.services.sync_service import sync_service

T = TypeVar("T")

# Bodies below this size are sent as they are (GZipMiddleware's threshold)
GZIP_MIN_SIZE = 1000
# A token that could not be computed ("") cannot vouch for a cached value: rebuild at most this often then
_UNVERIFIED_TTL_S = 15.0
# The body depends on the repository (X-KI-Repo header) and is sent compressed or not
VARY = "Accept-Encoding, X-KI-Repo"


def digest(data: bytes) -> str:
    """16-hex content digest (ETags, catalog generations)."""
    return hashlib.blake2b(data, digest_size=8).hexdigest()


def dumps(obj: Any) -> bytes:
    """JSON exactly as FastAPI's JSONResponse renders it."""
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")).encode("utf-8")


class JsonBody:
    """A JSON response body encoded once: raw bytes, gzip (level 9, once) and its ETag (content digest
    unless given)."""

    __slots__ = ("raw", "gz", "etag")

    def __init__(self, obj: Any, etag: str | None = None) -> None:
        self.raw = dumps(obj)
        self.gz = gzip.compress(self.raw, compresslevel=9, mtime=0) if len(self.raw) >= GZIP_MIN_SIZE else None
        self.etag = etag or digest(self.raw)


def client_etags(request: Request) -> list[str]:
    """The opaque tags of the request's If-None-Match (weak prefix and quotes removed)."""
    tags = []
    for part in request.headers.get("if-none-match", "").split(","):
        part = part.strip()
        if part.startswith("W/"):
            part = part[2:]
        part = part.strip('"')
        if part:
            tags.append(part)
    return tags


def respond(request: Request, body: JsonBody, *, cache_control: str = "no-cache", etag: bool = True) -> Response:
    """Send `body`: 304 when If-None-Match names its ETag, gzip when the client accepts it.

    `no-cache` lets the browser keep the body and revalidate it (a 304 costs no body); a body without
    `etag` (a delta) goes out as given."""
    headers = {"Cache-Control": cache_control, "Vary": VARY}
    if etag:
        headers["ETag"] = f'W/"{body.etag}"'
        tags = client_etags(request)
        if body.etag in tags or "*" in tags:
            return Response(status_code=304, headers=headers)
    if body.gz is not None and "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"  # GZipMiddleware passes encoded bodies through
        return Response(body.gz, media_type="application/json", headers=headers)
    return Response(body.raw, media_type="application/json", headers=headers)


class TokenCache(Generic[T]):
    """Values built once per key and repository state (the active repository's `parsed` and `git` sync tokens).

    `get(key, build)` calls `build(previous)` only when the tokens changed since the value was built;
    requests arriving during a build wait for it instead of starting their own. The key must name the
    repository (the tokens are those of the request's repository).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[Any, tuple[tuple[str, str], float, T]] = {}
        self._gates: dict[Any, threading.Lock] = {}

    @staticmethod
    def _current(entry: tuple[tuple[str, str], float, T] | None, tokens: tuple[str, str]) -> bool:
        if entry is None or entry[0] != tokens:
            return False
        return all(tokens) or time.monotonic() - entry[1] < _UNVERIFIED_TTL_S

    def get(self, key: Any, build: Callable[[T | None], T]) -> T:
        tokens = (sync_service.parsed_token(), sync_service.git_token())
        with self._lock:
            entry = self._entries.get(key)
            if self._current(entry, tokens):
                return entry[2]
            gate = self._gates.setdefault(key, threading.Lock())
        with gate:
            with self._lock:
                entry = self._entries.get(key)
            if self._current(entry, tokens):
                return entry[2]  # built by the request this one waited for
            value = build(entry[2] if entry else None)
            with self._lock:
                self._entries[key] = (tokens, time.monotonic(), value)
            return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
