"""webapp/backend/config.py - Configuration management wrapper.

Resolves all database connection settings, webapp host/port bindings, and server options
through core.config with strict precedence:
    CLI Flags > Environment Variables > config.json > Built-in Defaults
"""
from __future__ import annotations

import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from core.config import (
    LINUX_PROJECT,
    MANPAGES_PROJECT,
    get_db_config,
    get_default_repo_path,
    get_logging_config,
    get_nodemap_canvas_dir,
    get_repositories_config,
    get_repository,
    get_webapp_config,
    repo_feature_enabled,
    resolve_repo_features,
    resolve_repo_insights,
    resolve_repo_path,
)

# Default repository root (config "repositories", auto-detected under workspace.repos_dir).
# Request handlers must use get_repo_dir(), which honours the repository selected by the client.
LINUX_REPO_DIR = get_default_repo_path().resolve()

# Repository selected for the current request (set by RepoContextMiddleware from X-KI-Repo / ?repo=)
_ACTIVE_REPO: ContextVar[dict[str, Any] | None] = ContextVar("active_repo", default=None)


def refresh_default_repo_dir() -> Path:
    """Recompute and return LINUX_REPO_DIR from the currently active configuration."""
    global LINUX_REPO_DIR
    LINUX_REPO_DIR = get_default_repo_path().resolve()
    return LINUX_REPO_DIR


def list_repositories() -> list[dict[str, Any]]:
    """Enabled repositories from config (including ones auto-detected under workspace.repos_dir)."""
    return get_repositories_config()


def get_active_repository() -> dict[str, Any]:
    """Repository entry for the current request (the configured default when none was selected)."""
    repo = _ACTIVE_REPO.get() or get_repository()
    return repo or {"name": "linux", "path": str(get_default_repo_path().resolve())}


def repository_project_slug(repo: dict[str, Any] | None) -> str:
    """Project slug of a repositories[] entry: `man-pages` for the Linux man-pages (its `project`, else its
    name, root commit or URL, `core.config.is_manpages_repository`), else its `project` (default `linux`)."""
    from core.config import is_manpages_repository

    entry = repo or {}
    project = str(entry.get("project") or "").strip()
    if project == MANPAGES_PROJECT or is_manpages_repository(
        str(entry.get("name") or ""), str(entry.get("git_url") or ""), str(entry.get("root_commit") or "")
    ):
        return MANPAGES_PROJECT
    return project or LINUX_PROJECT


def is_manpages_entry(repo: dict[str, Any] | None) -> bool:
    """True for the repositories[] entry of a Linux man-pages clone."""
    return repository_project_slug(repo) == MANPAGES_PROJECT


def get_active_features() -> dict[str, bool]:
    """Resolved feature flags for the current request's repository."""
    return resolve_repo_features(get_active_repository())


def get_active_insights_config(repo_entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolved Insights configuration for repo_entry (or the current request's repository)."""
    return resolve_repo_insights(repo_entry if repo_entry is not None else get_active_repository())


def is_feature_enabled(feature: str, repo: dict[str, Any] | None = None) -> bool:
    """True when `feature` is enabled for `repo` (or the current request's repository)."""
    return repo_feature_enabled(repo if repo is not None else get_active_repository(), feature)


def require_feature(*features: str):
    """FastAPI dependency rejecting requests with 403 when any named feature is disabled for the active repo."""
    def _check() -> None:
        repo = get_active_repository()
        disabled = [f for f in features if not repo_feature_enabled(repo, f)]
        if disabled:
            name = str(repo.get("name") or "default")
            raise HTTPException(
                status_code=403,
                detail=f"Feature(s) disabled for repository '{name}': {', '.join(disabled)}",
            )

    return _check


def get_active_repo_name() -> str:
    return str(get_active_repository().get("name") or "default")


def get_repo_dir() -> Path:
    """Absolute Git work tree of the repository selected for the current request."""
    repo = _ACTIVE_REPO.get() or get_repository()
    return resolve_repo_path(repo).resolve() if repo else get_default_repo_path().resolve()


def set_active_repository(name: str | None):
    """Select a configured repository for the current context; returns a token for reset (None if unknown)."""
    repo = get_repository(name) if name else None
    return _ACTIVE_REPO.set(repo) if repo else None


def reset_active_repository(token) -> None:
    if token is not None:
        _ACTIVE_REPO.reset(token)


class RepoScopedCache(dict):
    """dict whose keys are transparently namespaced by the active repository.

    Module-level caches keyed by version names would otherwise collide between repositories that
    share tag names (e.g. two projects both tagging "v1.0").

    `maxsize` bounds the cache, evicting the least recently used entry. A cache whose key holds a
    request parameter (a header name, a path, a symbol) needs one: without it every distinct value a
    caller sends adds an entry that is never released. Caches keyed only by data the repository
    itself defines (version names) are left unbounded, and callers that evict by their own policy
    (`_MODELS`, `_BUILD_RESOLVERS`) reach through `dict.pop` and are unaffected.
    """

    def __init__(self, maxsize: int | None = None) -> None:
        super().__init__()
        self.maxsize = maxsize
        self._lock = threading.RLock()

    def _k(self, key: Any) -> tuple[str, Any]:
        return (get_active_repo_name(), key)

    def _touch(self, scoped_key: Any) -> None:
        """Re-insert an entry so it becomes the most recently used (dicts keep insertion order)."""
        if self.maxsize is not None:
            super().__setitem__(scoped_key, super().pop(scoped_key))

    def _evict(self) -> None:
        while self.maxsize is not None and len(self) > self.maxsize:
            super().pop(next(iter(self)), None)

    def __getitem__(self, key: Any) -> Any:
        scoped = self._k(key)
        with self._lock:
            value = super().__getitem__(scoped)
            self._touch(scoped)
            return value

    def __setitem__(self, key: Any, value: Any) -> None:
        scoped = self._k(key)
        with self._lock:
            super().__setitem__(scoped, value)
            self._touch(scoped)
            self._evict()

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return super().__contains__(self._k(key))

    def __delitem__(self, key: Any) -> None:
        with self._lock:
            super().__delitem__(self._k(key))

    def __len__(self) -> int:
        with self._lock:
            return super().__len__()

    def clear(self) -> None:
        with self._lock:
            super().clear()

    def get(self, key: Any, default: Any = None) -> Any:
        scoped = self._k(key)
        with self._lock:
            if not super().__contains__(scoped):
                return default
            value = super().__getitem__(scoped)
            self._touch(scoped)
            return value

    def pop(self, key: Any, *default: Any) -> Any:
        with self._lock:
            return super().pop(self._k(key), *default)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        scoped = self._k(key)
        with self._lock:
            value = super().setdefault(scoped, default)
            self._touch(scoped)
            self._evict()
            return value


def get_backend_db_config() -> dict[str, Any]:
    """Retrieve verified database configuration."""
    return get_db_config()


def get_backend_server_config() -> dict[str, Any]:
    """Retrieve verified web application server bindings."""
    cfg = dict(get_webapp_config() or {})
    try:
        pool_size = max(1, int(cfg.get("pool_size") or 10))
    except (TypeError, ValueError):
        pool_size = 10
    try:
        port = int(cfg.get("port") or 8000)
    except (TypeError, ValueError):
        port = 8000
    if not 1 <= port <= 65535:
        port = 8000
    cfg["pool_size"] = pool_size
    cfg["port"] = port
    cfg["default_version"] = str(cfg.get("default_version") or "v3.0")
    return cfg


def get_backend_logging_config() -> dict[str, Any]:
    """Retrieve verified web application logging configuration."""
    return get_logging_config("webapp")
