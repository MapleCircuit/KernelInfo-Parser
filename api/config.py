"""api/config.py - Configuration, key normalization, and repository context bridge for the AI API."""
from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import (
    DEFAULT_OAUTH_REDIRECT_URIS,
    _coerce_bool,
    _get_user_env,
    get_api_config,
    get_db_config,
    get_logging_config,
    get_nodemap_canvas_dir,
    get_oauth_data_dir,
    get_webapp_config,
    is_api_default_version_pinned,
    normalize_api_client_ip_header,
)
from webapp.backend.config import (
    get_active_features as _webapp_get_active_features,
)
from webapp.backend.config import (
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

logger = logging.getLogger(__name__)

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


def normalize_oauth_clients(raw_clients: Any) -> list[dict[str, Any]]:
    """Normalize pre-registered static OAuth clients from `api.oauth.clients`."""
    if not isinstance(raw_clients, list):
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_clients:
        if not isinstance(item, dict):
            continue
        client_id = str(item.get("client_id") or item.get("id") or "").strip()
        if not client_id or client_id in seen_ids:
            continue
        raw_redirects = item.get("redirect_uris")
        if isinstance(raw_redirects, str):
            redirects = [u.strip() for u in raw_redirects.split(",") if u.strip()]
        elif isinstance(raw_redirects, (list, tuple)):
            redirects = [str(u).strip() for u in raw_redirects if isinstance(u, str) and str(u).strip()]
        else:
            redirects = []
        if not redirects:
            continue
        raw_secret = item.get("client_secret") if "client_secret" in item else item.get("secret")
        secret = str(raw_secret).strip() if isinstance(raw_secret, str) and raw_secret.strip() else ""
        name = str(item.get("name") or item.get("client_name") or client_id).strip() or client_id
        raw_scopes = item.get("max_scopes") if "max_scopes" in item else item.get("scopes")
        max_scopes = normalize_api_scopes(raw_scopes) if raw_scopes is not None else VALID_API_SCOPES
        if max_scopes is None:
            max_scopes = DEFAULT_API_SCOPES
        auth_method = str(item.get("token_endpoint_auth_method") or "").strip()
        if auth_method not in ("none", "client_secret_post", "client_secret_basic"):
            auth_method = "client_secret_basic" if secret else "none"
        seen_ids.add(client_id)
        out.append(
            {
                "client_id": client_id,
                "client_secret": secret,
                "name": name,
                "redirect_uris": redirects,
                "max_scopes": max_scopes,
                "token_endpoint_auth_method": auth_method,
            }
        )
    return out


def normalize_oauth_config(raw_oauth: Any) -> dict[str, Any]:
    """Normalize the `api.oauth` configuration block with environment variable precedence."""
    sec = dict(raw_oauth) if isinstance(raw_oauth, dict) else {}

    env_en = _get_user_env("API_OAUTH_ENABLED")
    if env_en is None:
        env_en = _get_user_env("KIP_API_OAUTH_ENABLED")
    enabled = (
        _coerce_bool(env_en, _coerce_bool(sec.get("enabled"), False))
        if env_en is not None
        else _coerce_bool(sec.get("enabled"), False)
    )

    env_issuer = _get_user_env("API_OAUTH_ISSUER")
    if env_issuer is None:
        env_issuer = _get_user_env("KIP_API_OAUTH_ISSUER")
    issuer = (
        str(env_issuer).strip().rstrip("/")
        if env_issuer is not None
        else str(sec.get("issuer") or "").strip().rstrip("/")
    )

    env_secret = _get_user_env("API_OAUTH_SIGNING_SECRET")
    if env_secret is None:
        env_secret = _get_user_env("KIP_API_OAUTH_SIGNING_SECRET")
    signing_secret = (
        str(env_secret).strip()
        if env_secret is not None
        else str(sec.get("signing_secret") or "").strip()
    )

    env_dcr = _get_user_env("API_OAUTH_ALLOW_DCR")
    if env_dcr is None:
        env_dcr = _get_user_env("KIP_API_OAUTH_ALLOW_DCR")
    allow_dcr = (
        _coerce_bool(env_dcr, _coerce_bool(sec.get("allow_dcr"), True))
        if env_dcr is not None
        else _coerce_bool(sec.get("allow_dcr"), True)
    )

    env_cimd = _get_user_env("API_OAUTH_ALLOW_CIMD")
    if env_cimd is None:
        env_cimd = _get_user_env("KIP_API_OAUTH_ALLOW_CIMD")
    allow_cimd = (
        _coerce_bool(env_cimd, _coerce_bool(sec.get("allow_cimd"), True))
        if env_cimd is not None
        else _coerce_bool(sec.get("allow_cimd"), True)
    )

    env_at = _get_user_env("API_OAUTH_ACCESS_TOKEN_TTL") or _get_user_env("KIP_API_OAUTH_ACCESS_TOKEN_TTL")
    access_token_ttl = _coerce_positive_int(env_at if env_at is not None else sec.get("access_token_ttl"), 3600)

    env_rt = _get_user_env("API_OAUTH_REFRESH_TOKEN_TTL") or _get_user_env("KIP_API_OAUTH_REFRESH_TOKEN_TTL")
    refresh_token_ttl = _coerce_positive_int(env_rt if env_rt is not None else sec.get("refresh_token_ttl"), 2592000)

    env_ac = _get_user_env("API_OAUTH_AUTH_CODE_TTL") or _get_user_env("KIP_API_OAUTH_AUTH_CODE_TTL")
    auth_code_ttl = _coerce_positive_int(env_ac if env_ac is not None else sec.get("auth_code_ttl"), 120)

    env_redir = _get_user_env("API_OAUTH_ALLOWED_REDIRECT_URIS")
    if env_redir is None:
        env_redir = _get_user_env("KIP_API_OAUTH_ALLOWED_REDIRECT_URIS")
    if env_redir is not None:
        raw_redir = env_redir.strip()
        if raw_redir.startswith("["):
            try:
                parsed = json.loads(raw_redir)
                allowed_redirect_uris = [str(u).strip() for u in parsed if isinstance(u, str) and str(u).strip()]
            except json.JSONDecodeError:
                allowed_redirect_uris = [u.strip() for u in raw_redir.split(",") if u.strip()]
        else:
            allowed_redirect_uris = [u.strip() for u in raw_redir.split(",") if u.strip()]
    elif "allowed_redirect_uris" in sec and isinstance(sec.get("allowed_redirect_uris"), (list, tuple)):
        allowed_redirect_uris = [
            str(u).strip() for u in sec["allowed_redirect_uris"] if isinstance(u, str) and str(u).strip()
        ]
    elif "allowed_redirect_uris" in sec and isinstance(sec.get("allowed_redirect_uris"), str):
        allowed_redirect_uris = [u.strip() for u in sec["allowed_redirect_uris"].split(",") if u.strip()]
    else:
        allowed_redirect_uris = list(DEFAULT_OAUTH_REDIRECT_URIS)

    env_clients = _get_user_env("API_OAUTH_CLIENTS")
    if env_clients is None:
        env_clients = _get_user_env("KIP_API_OAUTH_CLIENTS")
    if env_clients is not None:
        try:
            raw_clients = json.loads(env_clients.strip())
        except json.JSONDecodeError:
            raw_clients = []
    else:
        raw_clients = sec.get("clients") if isinstance(sec.get("clients"), list) else []

    data_dir = str(get_oauth_data_dir())
    normalized_clients = normalize_oauth_clients(raw_clients)
    return {
        "enabled": enabled,
        "issuer": issuer,
        "signing_secret": signing_secret,
        "data_dir": data_dir,
        "access_token_ttl": access_token_ttl,
        "refresh_token_ttl": refresh_token_ttl,
        "auth_code_ttl": auth_code_ttl,
        "allow_dcr": allow_dcr,
        "allow_cimd": allow_cimd,
        "allowed_redirect_uris": allowed_redirect_uris,
        "clients": raw_clients if isinstance(raw_clients, list) else [],
        "normalized_clients": normalized_clients,
    }


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
    oauth = normalize_oauth_config(raw.get("oauth"))

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
        "oauth": oauth,
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


def resolve_default_version() -> str:
    """Resolve the default revision: explicit config/env pin if set, else latest indexed mainline release."""
    env_ver = _get_user_env("API_DEFAULT_VERSION")
    if env_ver is not None:
        cleaned_env = str(env_ver).strip()
        if cleaned_env and cleaned_env.lower() not in ("auto", "latest"):
            return safe_rev(cleaned_env, "version")

    cfg_default = get_api_server_config()["default_version"]
    if is_api_default_version_pinned() or (cfg_default and cfg_default != "v3.0"):
        return cfg_default

    try:
        from parser.git_ast.release_order import parse_tag, release_key
        from webapp.backend.routers.versions import get_versions

        versions = get_versions(include_rc=False).get("versions") or []
        mainline: list[str] = []
        for entry in versions:
            if not entry.get("indexed") or entry.get("ref_type") not in (None, 1):
                continue
            vname = str(entry.get("vname") or "").strip()
            if not vname:
                continue
            parsed = parse_tag(vname)
            if parsed is not None and parsed.get("kind") == "mainline" and parsed.get("final"):
                mainline.append(vname)
        if mainline:
            return max(mainline, key=release_key)
    except Exception as exc:  # noqa: BLE001 - fall back to configured default when DB/Git unavailable
        logger.debug("Default version resolution fell back to %s: %s", cfg_default, exc)
    return cfg_default


def resolve_version(version: str | None = None) -> str:
    """Resolve and validate a caller-supplied version or fall back to the resolved default."""
    candidate = (version or "").strip() or resolve_default_version()
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
    "DEFAULT_OAUTH_REDIRECT_URIS",
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
    "get_oauth_data_dir",
    "get_repo_dir",
    "is_feature_enabled",
    "list_repositories",
    "normalize_api_keys",
    "normalize_api_scopes",
    "normalize_oauth_clients",
    "normalize_oauth_config",
    "paginate_items",
    "refresh_default_repo_dir",
    "repository_project_slug",
    "require_feature",
    "reset_active_repository",
    "reset_allow_writes_override",
    "resolve_default_version",
    "resolve_repo_path",
    "resolve_version",
    "set_active_repository",
    "set_allow_writes_override",
]
