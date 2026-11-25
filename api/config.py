"""api/config.py - Configuration, key normalization, and repository context bridge for the AI API."""
from __future__ import annotations

from typing import Any

from core.config import (
    get_api_config,
    get_db_config,
    get_logging_config,
    get_webapp_config,
)
from webapp.backend.config import (
    get_active_features,
    get_active_repo_name,
    get_active_repository,
    get_repo_dir,
    is_feature_enabled,
    list_repositories,
    refresh_default_repo_dir,
    repository_project_slug,
    require_feature,
    reset_active_repository,
    resolve_repo_path,
    set_active_repository,
)
from webapp.backend.security.git import safe_rev

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8001


def normalize_api_keys(raw_keys: Any) -> list[tuple[str, str]]:
    """Normalize configured API keys into `(key_name, secret)` pairs.

    Accepts either a list of non-empty strings or a `{name: key}` mapping.
    Empty, whitespace-only, or malformed configurations normalize to `[]` (fail closed).
    """
    out: list[tuple[str, str]] = []
    if isinstance(raw_keys, list):
        for idx, item in enumerate(raw_keys):
            if isinstance(item, str):
                secret = item.strip()
                if secret:
                    out.append((f"key_{idx}", secret))
    elif isinstance(raw_keys, dict):
        for idx, (name, item) in enumerate(raw_keys.items()):
            if isinstance(item, str):
                secret = item.strip()
                if secret:
                    label = str(name).strip() or f"key_{idx}"
                    out.append((label, secret))
    return out


def _coerce_positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def get_api_server_config() -> dict[str, Any]:
    """Retrieve normalized configuration for the standalone AI API service."""
    raw = dict(get_api_config() or {})
    webapp_cfg = get_webapp_config() or {}

    host = str(raw.get("host") or DEFAULT_API_HOST).strip() or DEFAULT_API_HOST
    try:
        port = int(raw.get("port") or DEFAULT_API_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_API_PORT
    if not (1 <= port <= 65535):
        port = DEFAULT_API_PORT

    reload_flag = bool(raw.get("reload", False))
    default_version = (
        str(raw.get("default_version") or webapp_cfg.get("default_version") or "v3.0").strip() or "v3.0"
    )

    rl_raw = raw.get("rate_limit") if isinstance(raw.get("rate_limit"), dict) else {}
    rate_limit = {
        "max_requests": _coerce_positive_int(rl_raw.get("max_requests"), 120),
        "window_seconds": _coerce_positive_int(rl_raw.get("window_seconds"), 60),
    }

    lo_raw = raw.get("auth_lockout") if isinstance(raw.get("auth_lockout"), dict) else {}
    base_lockout = _coerce_positive_int(lo_raw.get("base_lockout_seconds"), 30)
    max_lockout = max(base_lockout, _coerce_positive_int(lo_raw.get("max_lockout_seconds"), 3600))
    auth_lockout = {
        "max_failures": _coerce_positive_int(lo_raw.get("max_failures"), 5),
        "failure_window_seconds": _coerce_positive_int(lo_raw.get("failure_window_seconds"), 60),
        "base_lockout_seconds": base_lockout,
        "max_lockout_seconds": max_lockout,
    }

    raw_proxies = raw.get("trusted_proxies")
    if isinstance(raw_proxies, str):
        trusted_proxies = [p.strip() for p in raw_proxies.split(",") if p.strip()]
    elif isinstance(raw_proxies, (list, tuple)):
        trusted_proxies = [str(p).strip() for p in raw_proxies if isinstance(p, str) and p.strip()]
    else:
        trusted_proxies = ["127.0.0.1", "::1"]

    client_ip_header = str(raw.get("client_ip_header") or "X-Forwarded-For").strip() or "X-Forwarded-For"
    normalized_keys = normalize_api_keys(raw.get("keys"))

    return {
        "host": host,
        "port": port,
        "reload": reload_flag,
        "default_version": default_version,
        "keys": raw.get("keys"),
        "normalized_keys": normalized_keys,
        "rate_limit": rate_limit,
        "auth_lockout": auth_lockout,
        "trusted_proxies": trusted_proxies,
        "client_ip_header": client_ip_header,
    }


def resolve_version(version: str | None = None) -> str:
    """Resolve and validate a caller-supplied version or fall back to the configured default."""
    candidate = (version or "").strip() or get_api_server_config()["default_version"]
    return safe_rev(candidate, "version")


def paginate_items(
    items: list[Any],
    limit: int,
    offset: int,
    *,
    already_truncated: bool = False,
) -> tuple[list[Any], int, bool]:
    """Slice `items[offset : offset + limit]` and return `(page, total, truncated)`."""
    total = len(items)
    page = items[offset : offset + limit]
    truncated = bool(already_truncated or (offset + limit < total))
    return page, total, truncated


__all__ = [
    "DEFAULT_API_HOST",
    "DEFAULT_API_PORT",
    "get_active_features",
    "get_active_repo_name",
    "get_active_repository",
    "get_api_config",
    "get_api_server_config",
    "get_db_config",
    "get_logging_config",
    "get_repo_dir",
    "is_feature_enabled",
    "list_repositories",
    "normalize_api_keys",
    "paginate_items",
    "refresh_default_repo_dir",
    "repository_project_slug",
    "require_feature",
    "reset_active_repository",
    "resolve_repo_path",
    "resolve_version",
    "set_active_repository",
]
