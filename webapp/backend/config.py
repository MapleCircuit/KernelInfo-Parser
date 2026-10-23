"""webapp/backend/config.py - Configuration management wrapper.

Resolves all database connection settings, webapp host/port bindings, and server options
through core.config with strict precedence:
    CLI Flags > Environment Variables > config.json > Built-in Defaults
"""
from __future__ import annotations
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from core.config import (
    get_db_config,
    get_default_repo_path,
    get_repositories_config,
    get_repository,
    get_webapp_config,
    get_parser_config,
    get_ssh_tunnel_config,
    init_config,
    resolve_repo_path,
)

# Default repository root (config "repositories", auto-detected under workspace.repos_dir).
# Request handlers must use get_repo_dir(), which honours the repository selected by the client.
LINUX_REPO_DIR = get_default_repo_path().resolve()

# Repository selected for the current request (set by RepoContextMiddleware from X-KI-Repo / ?repo=)
_ACTIVE_REPO: ContextVar[dict[str, Any] | None] = ContextVar("active_repo", default=None)


def list_repositories() -> list[dict[str, Any]]:
    """Enabled repositories from config (including ones auto-detected under workspace.repos_dir)."""
    return get_repositories_config()


def get_active_repository() -> dict[str, Any]:
    """Repository entry for the current request (the configured default when none was selected)."""
    repo = _ACTIVE_REPO.get() or get_repository()
    return repo or {"name": "linux", "path": str(LINUX_REPO_DIR)}


def get_active_repo_name() -> str:
    return str(get_active_repository().get("name") or "default")


def get_repo_dir() -> Path:
    """Absolute Git work tree of the repository selected for the current request."""
    repo = _ACTIVE_REPO.get()
    return resolve_repo_path(repo).resolve() if repo else LINUX_REPO_DIR


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
    """

    def _k(self, key: Any) -> tuple[str, Any]:
        return (get_active_repo_name(), key)

    def __getitem__(self, key: Any) -> Any:
        return super().__getitem__(self._k(key))

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(self._k(key), value)

    def __contains__(self, key: object) -> bool:
        return super().__contains__(self._k(key))

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(self._k(key))

    def get(self, key: Any, default: Any = None) -> Any:
        return super().get(self._k(key), default)

    def pop(self, key: Any, *default: Any) -> Any:
        return super().pop(self._k(key), *default)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        return super().setdefault(self._k(key), default)


def get_backend_db_config() -> dict[str, Any]:
    """Retrieve verified database configuration."""
    return get_db_config()


def get_backend_server_config() -> dict[str, Any]:
    """Retrieve verified web application server bindings."""
    return get_webapp_config()


def get_backend_ssh_tunnel_config() -> dict[str, Any]:
    """Retrieve verified SSH tunnel configuration."""
    return get_ssh_tunnel_config()
