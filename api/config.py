"""api/config.py - Configuration, key normalization, and repository context bridge for the AI API."""
from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import (
    _coerce_bool,
    _get_user_env,
    get_api_config,
    get_db_config,
    get_logging_config,
    get_nodemap_canvas_dir,
    get_webapp_config,
    normalize_api_client_ip_header,
)
from webapp.backend.config import (
    get_active_features as _webapp_get_active_features,
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
DEFAULT_API_SCOPES: frozenset[str] = frozenset({"read"})
VALID_API_SCOPES: frozenset[str] = frozenset({"read", "nodemap:write", "write"})

_ALLOW_WRITES_OVERRIDE: ContextVar[bool | None] = ContextVar("allow_writes_override", default=None)
_PROCESS_ALLOW_WRITES_OVERRIDE: bool | None = None


def get_allow_writes_override() -> bool | None:
    """Return any active session/context `allow_writes` override, or None if unset."""
    ctx_val = _ALLOW_WRITES_OVERRIDE.get()
    if ctx_val is not None:
        return ctx_val
    return _PROCESS_ALLOW_WRITES_OVERRIDE


def set_allow_writes_override(value: bool | None) -> tuple[Any, bool | None]:
    """Override `api.allow_writes` for the current context/session (e.g. `mcp_stdio --allow-writes`)."""
    global _PROCESS_ALLOW_WRITES_OVERRIDE
    prev_proc = _PROCESS_ALLOW_WRITES_OVERRIDE
    _PROCESS_ALLOW_WRITES_OVERRIDE = value
    ctx_tok = _ALLOW_WRITES_OVERRIDE.set(value)
    return (ctx_tok, prev_proc)


def reset_allow_writes_override(token: tuple[Any, bool | None] | None) -> None:
    """Restore a token returned by `set_allow_writes_override()`."""
    global _PROCESS_ALLOW_WRITES_OVERRIDE
    if token is not None:
        ctx_tok, prev_proc = token
        _PROCESS_ALLOW_WRITES_OVERRIDE = prev_proc
        _ALLOW_WRITES_OVERRIDE.reset(ctx_tok)


def get_active_features() -> dict[str, bool]:
    """Resolved feature flags for the current request's repository (including `kapi` alias for `api`)."""
    feats = dict(_webapp_get_active_features())
    feats.setdefault("kapi", bool(feats.get("api", False)))
    return feats


@dataclass(frozen=True)
class ApiKeyEntry:
    """Normalized API key entry (`id`, `secret`, `scopes`) with backward-compatible tuple/str access."""

    id: str
    secret: str
    scopes: frozenset[str] = DEFAULT_API_SCOPES

    @property
    def name(self) -> str:
        return self.id

    @property
    def key(self) -> str:
        return self.secret

    def __iter__(self):
        yield self.id
        yield self.secret

    def __getitem__(self, index: int) -> Any:
        return (self.id, self.secret, self.scopes)[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ApiKeyEntry):
            return (self.id, self.secret, self.scopes) == (other.id, other.secret, other.scopes)
        if isinstance(other, str):
            return self.id == other
        if isinstance(other, tuple):
            if len(other) == 2:
                return (self.id, self.secret) == other
            if len(other) == 3:
                return (self.id, self.secret, self.scopes) == other
        return NotImplemented


def normalize_api_scopes(raw_scopes: Any) -> frozenset[str] | None:
    """Validate and normalize a key's scope collection against `VALID_API_SCOPES`.

    Omitted (`None`) defaults to `frozenset({"read"})`; malformed or unknown scopes return `None`.
    """
    if raw_scopes is None:
        return DEFAULT_API_SCOPES
    if isinstance(raw_scopes, str):
        parts = [p.strip() for p in raw_scopes.split(",") if p.strip()]
        if not parts or any(p not in VALID_API_SCOPES for p in parts):
            return None
        return frozenset(parts)
    if isinstance(raw_scopes, (list, tuple, set, frozenset)):
        cleaned: list[str] = []
        for item in raw_scopes:
            if not isinstance(item, str):
                return None
            s = item.strip()
            if not s or s not in VALID_API_SCOPES:
                return None
            cleaned.append(s)
        return frozenset(cleaned) if cleaned else None
    return None


def normalize_api_keys(raw_keys: Any) -> list[ApiKeyEntry]:
    """Normalize configured API keys into `ApiKeyEntry(id, secret, scopes)` entries.

    Accepts:
    - a list of non-empty strings (defaulting to `frozenset({"read"})`),
    - a `{name: "secret"}` mapping (defaulting to `frozenset({"read"})`), or
    - a `{name: {"key": "secret", "scopes": ["read", "nodemap:write"]}}` scoped mapping.
    Empty, whitespace-only, or malformed configurations normalize to `[]` (fail closed).
    """
    out: list[ApiKeyEntry] = []
    if isinstance(raw_keys, list):
        for idx, item in enumerate(raw_keys):
            if isinstance(item, str):
                secret = item.strip()
                if secret:
                    out.append(ApiKeyEntry(id=f"key_{idx}", secret=secret, scopes=DEFAULT_API_SCOPES))
            elif isinstance(item, dict):
                raw_secret = item.get("key") if "key" in item else item.get("secret")
                if isinstance(raw_secret, str) and raw_secret.strip():
                    scopes = normalize_api_scopes(item.get("scopes"))
                    if scopes is not None:
                        label = str(item.get("id") or item.get("name") or "").strip() or f"key_{idx}"
                        out.append(ApiKeyEntry(id=label, secret=raw_secret.strip(), scopes=scopes))
    elif isinstance(raw_keys, dict):
        for idx, (name, item) in enumerate(raw_keys.items()):
            label = str(name).strip() or f"key_{idx}"
            if isinstance(item, str):
                secret = item.strip()
                if secret:
                    out.append(ApiKeyEntry(id=label, secret=secret, scopes=DEFAULT_API_SCOPES))
            elif isinstance(item, dict):
                raw_secret = item.get("key") if "key" in item else item.get("secret")
                if isinstance(raw_secret, str) and raw_secret.strip():
                    scopes = normalize_api_scopes(item.get("scopes"))
                    if scopes is not None:
                        out.append(ApiKeyEntry(id=label, secret=raw_secret.strip(), scopes=scopes))
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

    client_ip_header = normalize_api_client_ip_header(raw.get("client_ip_header"))
    normalized_keys = normalize_api_keys(raw.get("keys"))
    override = get_allow_writes_override()
    if override is not None:
        allow_writes = bool(override)
    else:
        env_allow = _get_user_env("API_ALLOW_WRITES")
        if env_allow is None:
            env_allow = _get_user_env("KIP_API_ALLOW_WRITES")
        if env_allow is not None:
            allow_writes = _coerce_bool(env_allow, _coerce_bool(raw.get("allow_writes"), False))
        else:
            allow_writes = _coerce_bool(raw.get("allow_writes"), False)
    nodemap_canvas_dir = str(get_nodemap_canvas_dir())

    return {
        "host": host,
        "port": port,
        "reload": reload_flag,
        "default_version": default_version,
        "allow_writes": allow_writes,
        "nodemap_canvas_dir": nodemap_canvas_dir,
        "keys": raw.get("keys"),
        "normalized_keys": normalized_keys,
        "rate_limit": rate_limit,
        "auth_lockout": auth_lockout,
        "trusted_proxies": trusted_proxies,
        "client_ip_header": client_ip_header,
    }


def apply_config_file(path: str | os.PathLike[str]) -> Path:
    """Activate a `-c` configuration file: export `CONFIG_FILE` / `KERNELINFO_CONFIG` and `init_config()` it."""
    from core.config import init_config

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Specified configuration file not found: {resolved}")
    os.environ["CONFIG_FILE"] = str(resolved)
    os.environ["KERNELINFO_CONFIG"] = str(resolved)
    init_config(resolved)
    return resolved


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
    "ApiKeyEntry",
    "DEFAULT_API_HOST",
    "DEFAULT_API_PORT",
    "DEFAULT_API_SCOPES",
    "VALID_API_SCOPES",
    "apply_config_file",
    "get_active_features",
    "get_active_repo_name",
    "get_active_repository",
    "get_allow_writes_override",
    "get_api_config",
    "get_api_server_config",
    "get_db_config",
    "get_logging_config",
    "get_nodemap_canvas_dir",
    "get_repo_dir",
    "is_feature_enabled",
    "list_repositories",
    "normalize_api_keys",
    "normalize_api_scopes",
    "paginate_items",
    "refresh_default_repo_dir",
    "repository_project_slug",
    "require_feature",
    "reset_active_repository",
    "reset_allow_writes_override",
    "resolve_repo_path",
    "resolve_version",
    "set_active_repository",
    "set_allow_writes_override",
]
