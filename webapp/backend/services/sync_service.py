"""webapp/backend/services/sync_service.py - Tokens telling clients what changed on the server.

Four tokens (short hex digests), each with its own client reaction (webapp/frontend/js/sync.js):
- `db`: the database instance (m_db_instance); a rebuilt database invalidates every client cache.
- `frontend`: the webapp code under webapp/frontend; the page reloads into the new build.
- `parsed`: the active repository's ingested versions (m_git_ref + m_ingest_state); version lists
  are refetched and cached data of the versions that changed is dropped.
- `git`: the active repository's refs (git for-each-ref); tag/branch lists are refetched.

They are cached (frontend 5 s, db 30 s, parsed/git 15 s) so `/api/sync` and the `X-KIP-Sync`
header of every API response cost no database or Git work on most requests.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from webapp.backend.config import get_active_repo_name, get_repo_dir
from webapp.backend.database.helpers import get_active_repo_id, safe_decode
from webapp.backend.database.pool import get_db_cursor

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
SYNC_HEADER = "X-KIP-Sync"
INGEST_PARSING = 1

_FRONTEND_TTL = 5.0
_DB_TTL = 30.0
_REPO_TTL = 15.0


def _digest(parts: list[str]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
    return h.hexdigest()[:12]


class _Ttl:
    """value_fn() cached for `ttl` seconds per key."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._values: dict[Any, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any, value_fn: Callable[[], str]) -> str:
        now = time.monotonic()
        with self._lock:
            hit = self._values.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        value = value_fn()
        with self._lock:
            self._values[key] = (now, value)
        return value


class SyncService:
    def __init__(self, frontend_dir: Path = FRONTEND_DIR) -> None:
        self.frontend_dir = frontend_dir
        self._frontend_checked = 0.0
        self._frontend_signature: tuple = ()
        self._frontend_build = ""
        self._frontend_lock = threading.Lock()
        self._db = _Ttl(_DB_TTL)
        self._parsed = _Ttl(_REPO_TTL)
        self._git = _Ttl(_REPO_TTL)
        self._last_parsed: dict[str, str] = {}

    # ------------------------------------------------------------------ frontend
    def _frontend_files(self) -> list[tuple[str, int, int, int, int]]:
        files = []
        for root, dirs, names in os.walk(self.frontend_dir):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(names):
                if name.startswith("."):
                    continue
                path = Path(root) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                # inode: deployments that replace files atomically (rename) keep sizes and may keep mtimes
                files.append((path.relative_to(self.frontend_dir).as_posix(), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino))
        return files

    def frontend_build(self) -> str:
        """Content digest of the frontend files; recomputed only when a file's size or mtime changed."""
        with self._frontend_lock:
            now = time.monotonic()
            if self._frontend_build and now - self._frontend_checked < _FRONTEND_TTL:
                return self._frontend_build
            self._frontend_checked = now
            files = self._frontend_files()
            signature = tuple(files)
            if signature != self._frontend_signature:
                h = hashlib.sha256()
                for rel, *_ in files:
                    h.update(rel.encode())
                    h.update(b"\0")
                    try:
                        h.update((self.frontend_dir / rel).read_bytes())
                    except OSError:
                        pass
                    h.update(b"\0")
                build = h.hexdigest()[:12]
                if self._frontend_build and build != self._frontend_build:
                    logger.info("Frontend build changed: %s -> %s", self._frontend_build, build)
                self._frontend_signature = signature
                self._frontend_build = build
            return self._frontend_build

    # ------------------------------------------------------------------ database / repository
    def db_token(self) -> str:
        def compute() -> str:
            try:
                with get_db_cursor() as cursor:
                    cursor.execute("SELECT instance_hash FROM m_db_instance LIMIT 1;")
                    row = cursor.fetchone()
                    return safe_decode(row["instance_hash"])[:12] if row else ""
            except Exception as exc:
                logger.debug("db token unavailable: %s", exc)
                return ""
        return self._db.get("db", compute)

    def parsed_token(self) -> str:
        """Digest of the active repository's finished versions (ref, commit, completion time)."""
        repo = get_active_repo_name()

        def compute() -> str:
            try:
                with get_db_cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT r.ref_name, r.commit_id, COALESCE(s.status, 0) AS status, COALESCE(s.updated_at, 0) AS updated_at
                        FROM m_git_ref r
                        LEFT JOIN m_ingest_state s ON s.commit_id = r.commit_id
                        WHERE r.repo_id = %s AND r.commit_id > 0
                        ORDER BY r.ref_id;
                        """,
                        (get_active_repo_id(cursor),),
                    )
                    parts = [
                        f"{safe_decode(r['ref_name'])}:{r['commit_id']}:{r['updated_at']}"
                        for r in cursor.fetchall() if int(r["status"]) != INGEST_PARSING
                    ]
            except Exception as exc:
                logger.debug("parsed token unavailable: %s", exc)
                return ""
            token = _digest(parts)
            if self._last_parsed.get(repo) not in (None, token):
                clear_parse_dependent_caches()
                logger.info("Parsed versions of '%s' changed; version caches cleared.", repo)
            self._last_parsed[repo] = token
            return token
        return self._parsed.get(repo, compute)

    def git_token(self) -> str:
        """Digest of the active repository's refs (tags, branches, remotes)."""
        repo_dir = str(get_repo_dir())

        def compute() -> str:
            try:
                out = subprocess.run(
                    ["git", "for-each-ref", "--format=%(objectname) %(refname)"],
                    cwd=repo_dir, capture_output=True, text=True, timeout=10, check=False,
                ).stdout
            except Exception as exc:
                logger.debug("git token unavailable: %s", exc)
                return ""
            return _digest(out.splitlines())
        return self._git.get(repo_dir, compute)

    # ------------------------------------------------------------------ API
    def snapshot(self) -> dict[str, str]:
        return {
            "db": self.db_token(),
            "frontend": self.frontend_build(),
            "parsed": self.parsed_token(),
            "git": self.git_token(),
            "repo": get_active_repo_name(),
        }

    def header_value(self) -> str:
        """`db.parsed.git` for the X-KIP-Sync response header (the frontend build is only checked on page open)."""
        return f"{self.db_token()}.{self.parsed_token()}.{self.git_token()}"


def clear_parse_dependent_caches() -> None:
    """Drop server caches holding per-version data derived from what was parsed (a version finished or was
    rolled back, so its name now resolves to other rows)."""
    from webapp.backend.database import helpers
    from webapp.backend.services import filesystem_service, history_service, maintainer_service
    from webapp.backend.services.tree_resolver import git_tree_resolver

    for cache in (
        helpers._NAME_TO_VID_CACHE, helpers._VID_TO_NAME_CACHE, helpers._REPO_ID_CACHE,
        filesystem_service._FILE_TOKENS_CACHE, filesystem_service._INCLUDE_CACHE,
        maintainer_service._MAINTAINER_CACHE, maintainer_service._FILE_SUBSYSTEMS_CACHE,
        history_service._FACTS_CACHE, git_tree_resolver._blob_path_cache,
    ):
        cache.clear()


sync_service = SyncService()


class SyncHeaderMiddleware:
    """Pure ASGI middleware adding `X-KIP-Sync: <db>.<parsed>.<git>` to API responses.

    Must run inside RepoContextMiddleware so the tokens are those of the request's repository; the
    (cached) tokens are computed in the threadpool with the request's context.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        import contextvars
        from starlette.concurrency import run_in_threadpool

        ctx = contextvars.copy_context()

        async def send_with_header(message) -> None:
            if message["type"] == "http.response.start":
                try:
                    value = await run_in_threadpool(ctx.run, sync_service.header_value)
                    message.setdefault("headers", [])
                    message["headers"] = [*message["headers"], (SYNC_HEADER.lower().encode(), value.encode())]
                except Exception as exc:
                    logger.debug("sync header skipped: %s", exc)
            await send(message)

        await self.app(scope, receive, send_with_header)
