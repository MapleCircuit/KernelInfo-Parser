"""api/security.py - Pure ASGI authentication, IP lockout, rate limiting, and redacted logging."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import math
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from starlette.responses import JSONResponse, Response

from api.config import (
    DEFAULT_API_SCOPES,
    ApiKeyEntry,
    get_api_server_config,
    normalize_api_keys,
    normalize_api_scopes,
)

MAX_TRACKED_IPS = 4096
MAX_TRACKED_KEYS = 4096

_ACTIVE_API_SCOPES: ContextVar[frozenset[str]] = ContextVar("active_api_scopes", default=DEFAULT_API_SCOPES)
_ACTIVE_API_KEY_ID: ContextVar[str | None] = ContextVar("active_api_key_id", default=None)
_ACTIVE_OAUTH_ISSUER: ContextVar[str | None] = ContextVar("active_oauth_issuer", default=None)
_ACTIVE_AUTH_MIDDLEWARE: ContextVar[Any] = ContextVar("active_auth_middleware", default=None)
_LAST_AUTH_MIDDLEWARE: Any = None

_BEARER_RE = re.compile(r"(Bearer\s+)[^\s,;\"']+", re.IGNORECASE)
_BASIC_RE = re.compile(r"(Basic\s+)[^\s,;\"']+", re.IGNORECASE)
_API_KEY_KV_RE = re.compile(r"(X-API-Key\s*[:=]\s*)[^\s,;\"']+", re.IGNORECASE)


def get_active_api_scopes() -> frozenset[str]:
    """Return the active caller's API scopes for the current request/stdio context."""
    return _ACTIVE_API_SCOPES.get()


def set_active_api_scopes(scopes: Iterable[str]) -> Any:
    """Bind `scopes` into `_ACTIVE_API_SCOPES` and return the reset token."""
    return _ACTIVE_API_SCOPES.set(frozenset(str(s).strip() for s in scopes if str(s).strip()))


def reset_active_api_scopes(token: Any) -> None:
    """Reset `_ACTIVE_API_SCOPES` using a token from `set_active_api_scopes()`."""
    if token is not None:
        _ACTIVE_API_SCOPES.reset(token)


def get_active_api_key_id() -> str | None:
    """Return the authenticated API key identifier for the current request context, if any."""
    return _ACTIVE_API_KEY_ID.get()


def has_write_scope(scope_name: str = "nodemap:write", scopes: Iterable[str] | None = None) -> bool:
    """Return True only when `api.allow_writes` is enabled and `scopes` (or active context) has `scope_name` or `'write'`."""
    if not get_api_server_config().get("allow_writes", False):
        return False
    effective = frozenset(scopes) if scopes is not None else _ACTIVE_API_SCOPES.get()
    return scope_name in effective or "write" in effective


def require_write_scope(scope_name: str = "nodemap:write") -> Callable[[], None]:
    """FastAPI dependency returning HTTP 403 whenever `api.allow_writes` is False or caller lacks `scope_name`."""

    def _check() -> None:
        cfg = get_api_server_config()
        oauth_cfg = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else {}
        oauth_enabled = bool(oauth_cfg.get("enabled", False))
        headers: dict[str, str] | None = None
        if oauth_enabled:
            from api.oauth import build_www_authenticate_header, resolve_issuer

            issuer = _ACTIVE_OAUTH_ISSUER.get() or resolve_issuer(None, oauth_cfg=oauth_cfg)
            headers = {
                "WWW-Authenticate": build_www_authenticate_header(
                    issuer,
                    error="insufficient_scope",
                    scope=scope_name,
                )
            }
        if not cfg.get("allow_writes", False):
            raise HTTPException(
                status_code=403,
                detail="Write operations are disabled on this API server (api.allow_writes is false).",
                headers=headers,
            )
        scopes = _ACTIVE_API_SCOPES.get()
        if scope_name not in scopes and "write" not in scopes:
            raise HTTPException(
                status_code=403,
                detail=f"API key lacks required write scope '{scope_name}'.",
                headers=headers,
            )

    _check.__kip_required_scope__ = scope_name  # type: ignore[attr-defined]
    return _check


def parse_trusted_proxies(entries: Iterable[str] | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse IP addresses or CIDR ranges into `ipaddress` network objects."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in entries or ():
        raw = str(entry or "").strip()
        if not raw:
            continue
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            continue
    return networks


def is_trusted_ip(ip_str: str, trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    """Return True when `ip_str` is a valid IP inside one of `trusted_networks`."""
    if not ip_str or not trusted_networks:
        return False
    try:
        addr = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return False
    return any(addr in net for net in trusted_networks)


def _header_value(scope: dict[str, Any], name_lower: bytes) -> str | None:
    for key, val in scope.get("headers", ()):
        if key.lower() == name_lower:
            return val.decode("latin-1", errors="replace").strip()
    return None


def extract_client_ip(
    scope: dict[str, Any],
    trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    client_ip_header: str = "X-Forwarded-For",
) -> str:
    """Extract the client IP safely from `scope`, honoring `client_ip_header` only for trusted peers.

    For `X-Forwarded-For`, hops are walked right-to-left skipping `trusted_networks`. If the direct
    peer IP is not in `trusted_networks` or any inspected forwarding value is malformed, the direct
    peer IP from `scope['client'][0]` is returned.
    """
    client = scope.get("client")
    peer_ip = str(client[0]).strip() if client and client[0] else "unknown"
    if peer_ip == "unknown" or not is_trusted_ip(peer_ip, trusted_networks):
        return peer_ip

    header_name = (client_ip_header or "X-Forwarded-For").strip().lower()
    raw_val = _header_value(scope, header_name.encode("latin-1", errors="ignore"))
    if not raw_val:
        return peer_ip

    if header_name == "x-forwarded-for":
        hops = [part.strip() for part in raw_val.split(",") if part.strip()]
        if not hops:
            return peer_ip
        parsed_hops: list[str] = []
        for hop in reversed(hops):
            try:
                addr_str = str(ipaddress.ip_address(hop))
            except ValueError:
                return peer_ip
            if not is_trusted_ip(addr_str, trusted_networks):
                return addr_str
            parsed_hops.append(addr_str)
        return parsed_hops[-1] if parsed_hops else peer_ip

    try:
        return str(ipaddress.ip_address(raw_val))
    except ValueError:
        return peer_ip


def extract_presented_credentials(scope: dict[str, Any]) -> tuple[str | None, bool]:
    """Extract `(token, is_bearer)` from `X-API-Key: <key>` or `Authorization: Bearer <token>`.

    An empty or whitespace-only `X-API-Key` header is treated as absent so it does not shadow a valid
    `Authorization: Bearer` header. `is_bearer` is True when the credential came from `Authorization: Bearer`.
    """
    for key, val in scope.get("headers", ()):
        if key.lower() == b"x-api-key":
            cleaned = val.decode("latin-1", errors="replace").strip()
            if cleaned:
                return cleaned, False

    auth = _header_value(scope, b"authorization")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
            return (token or None), True
    return None, False


def extract_presented_key(scope: dict[str, Any]) -> str | None:
    """Extract an API key from `Authorization: Bearer <key>` or `X-API-Key: <key>`."""
    token, _is_bearer = extract_presented_credentials(scope)
    return token


def _coerce_key_entry(entry: ApiKeyEntry | tuple[Any, ...], idx: int = 0) -> ApiKeyEntry:
    if isinstance(entry, ApiKeyEntry):
        return entry
    if isinstance(entry, tuple) and len(entry) >= 2:
        key_id = str(entry[0]).strip() or f"key_{idx}"
        secret = str(entry[1])
        scopes = (
            normalize_api_scopes(entry[2])
            if len(entry) >= 3 and entry[2] is not None
            else DEFAULT_API_SCOPES
        ) or DEFAULT_API_SCOPES
        return ApiKeyEntry(id=key_id, secret=secret, scopes=scopes)
    raise TypeError(f"Unsupported API key entry: {entry!r}")


def verify_api_key_constant_time(
    candidate: str | None,
    configured_keys: Iterable[ApiKeyEntry | tuple[Any, ...]],
) -> ApiKeyEntry | None:
    """Compare `candidate` against all configured keys in constant time without short-circuiting.

    Returns the matched `ApiKeyEntry` (`id`, `secret`, `scopes`) or None. Always returns None when
    `configured_keys` is empty.
    """
    entries = [_coerce_key_entry(e, idx) for idx, e in enumerate(configured_keys or ())]
    if not entries:
        return None
    # Fixed-length SHA-256 digests on both sides: the comparison time depends on neither key's length
    cand_digest = hashlib.sha256((candidate or "").encode("utf-8")).digest()
    cand_non_empty = bool(candidate)
    matched: ApiKeyEntry | None = None
    for entry in entries:
        ok = hmac.compare_digest(cand_digest, hashlib.sha256(entry.secret.encode("utf-8")).digest())
        if ok and cand_non_empty and matched is None:
            matched = entry
    return matched


def redact_sensitive_text(text: str, configured_keys: Iterable[ApiKeyEntry | tuple[Any, ...]] = ()) -> str:
    """Scrub `Authorization: Bearer ...`, `Basic ...`, `X-API-Key`, and raw configured secrets from text."""
    if not text:
        return text
    cleaned = _BEARER_RE.sub(r"\1[REDACTED]", text)
    cleaned = _BASIC_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = _API_KEY_KV_RE.sub(r"\1[REDACTED]", cleaned)
    for entry in configured_keys:
        secret = entry.secret if isinstance(entry, ApiKeyEntry) else (entry[1] if len(entry) >= 2 else "")
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def _active_middleware() -> ApiAuthMiddleware | None:
    mw = _ACTIVE_AUTH_MIDDLEWARE.get()
    return mw if mw is not None else _LAST_AUTH_MIDDLEWARE


def check_ip_lockout(client_ip: str) -> int | None:
    """Return remaining lockout seconds for `client_ip` on the active `ApiAuthMiddleware`, or None."""
    mw = _active_middleware()
    if mw is None:
        return None
    return mw.get_ip_lockout_remaining(client_ip)


def record_ip_auth_failure(client_ip: str, auth_lockout: dict[str, int] | None = None) -> None:
    """Record a failed authentication attempt (e.g. OAuth consent form bad API key) against `client_ip`."""
    mw = _active_middleware()
    if mw is None:
        return
    mw.record_auth_failure(client_ip, auth_lockout)


def clear_ip_auth_failures(client_ip: str) -> None:
    """Clear any accumulated authentication failures for `client_ip` upon valid authentication."""
    mw = _active_middleware()
    if mw is None:
        return
    mw.clear_auth_failures(client_ip)


@dataclass
class _IpAuthState:
    failures: deque[float] = field(default_factory=deque)
    lockout_until: float = 0.0
    lockout_tier: int = 0
    last_seen: float = 0.0


def _prune_window(hits: deque[float], cutoff: float) -> None:
    while hits and hits[0] <= cutoff:
        hits.popleft()


def _evict_lru(mapping: OrderedDict[str, Any], limit: int) -> None:
    while len(mapping) >= limit:
        mapping.popitem(last=False)


class ApiAuthMiddleware:
    """Pure ASGI middleware enforcing IP lockout, constant-time API key auth, and rate limits.

    Every HTTP path served by `api/app.py` (including `/api/openapi.json` and `/api/v1/capabilities`)
    passes through this gate. Runs without `BaseHTTPMiddleware` so inner `ContextVar` state
    (repository context) propagates cleanly to sync and async endpoints.
    """

    def __init__(
        self,
        app: Any,
        *,
        keys: Any = None,
        rate_limit: dict[str, int] | None = None,
        auth_lockout: dict[str, int] | None = None,
        trusted_proxies: Iterable[str] | None = None,
        client_ip_header: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_ips: int = MAX_TRACKED_IPS,
        max_tracked_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self.app = app
        self._keys_override = normalize_api_keys(keys) if keys is not None else None
        self._rate_limit_override = dict(rate_limit) if rate_limit is not None else None
        self._auth_lockout_override = dict(auth_lockout) if auth_lockout is not None else None
        self._trusted_proxies_override = (
            parse_trusted_proxies(trusted_proxies) if trusted_proxies is not None else None
        )
        self._client_ip_header_override = client_ip_header
        self._clock = clock
        self._max_tracked_ips = max(16, int(max_tracked_ips))
        self._max_tracked_keys = max(16, int(max_tracked_keys))

        self._lock = threading.Lock()
        self._ip_failures: OrderedDict[str, _IpAuthState] = OrderedDict()
        self._ip_hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._key_hits: OrderedDict[str, deque[float]] = OrderedDict()
        global _LAST_AUTH_MIDDLEWARE
        _LAST_AUTH_MIDDLEWARE = self

    def get_ip_lockout_remaining(self, client_ip: str) -> int | None:
        now = self._clock()
        with self._lock:
            state = self._ip_failures.get(client_ip)
            if state is not None and state.lockout_until > now:
                self._ip_failures.move_to_end(client_ip)
                return max(1, int(math.ceil(state.lockout_until - now)))
        return None

    def record_auth_failure(self, client_ip: str, auth_lockout: dict[str, int] | None = None) -> None:
        now = self._clock()
        policy = auth_lockout if auth_lockout is not None else self._runtime_policy()[2]
        with self._lock:
            self._record_failure(client_ip, now, policy)

    def clear_auth_failures(self, client_ip: str) -> None:
        with self._lock:
            self._ip_failures.pop(client_ip, None)

    def _runtime_policy(self) -> tuple[
        list[tuple[str, str]],
        dict[str, int],
        dict[str, int],
        list[ipaddress.IPv4Network | ipaddress.IPv6Network],
        str,
    ]:
        cfg = get_api_server_config()
        keys = self._keys_override if self._keys_override is not None else cfg["normalized_keys"]
        rate_limit = self._rate_limit_override if self._rate_limit_override is not None else cfg["rate_limit"]
        auth_lockout = self._auth_lockout_override if self._auth_lockout_override is not None else cfg["auth_lockout"]
        trusted = (
            self._trusted_proxies_override
            if self._trusted_proxies_override is not None
            else parse_trusted_proxies(cfg["trusted_proxies"])
        )
        ip_header = (
            self._client_ip_header_override
            if self._client_ip_header_override is not None
            else cfg["client_ip_header"]
        )
        return keys, rate_limit, auth_lockout, trusted, ip_header

    def _evict_expired_failures(self, now: float, failure_window: int) -> None:
        cutoff = now - failure_window
        expired = [
            ip
            for ip, st in self._ip_failures.items()
            if st.lockout_until <= now and (not st.failures or st.failures[-1] <= cutoff)
        ]
        for ip in expired:
            self._ip_failures.pop(ip, None)
        _evict_lru(self._ip_failures, self._max_tracked_ips)

    def _record_failure(self, client_ip: str, now: float, auth_lockout: dict[str, int]) -> None:
        max_failures = max(1, int(auth_lockout.get("max_failures", 5)))
        failure_window = max(1, int(auth_lockout.get("failure_window_seconds", 60)))
        base_lockout = max(1, int(auth_lockout.get("base_lockout_seconds", 30)))
        max_lockout = max(base_lockout, int(auth_lockout.get("max_lockout_seconds", 3600)))

        state = self._ip_failures.get(client_ip)
        if state is None:
            if len(self._ip_failures) >= self._max_tracked_ips:
                self._evict_expired_failures(now, failure_window)
            state = _IpAuthState()
            self._ip_failures[client_ip] = state
        else:
            self._ip_failures.move_to_end(client_ip)

        state.last_seen = now
        _prune_window(state.failures, now - failure_window)
        state.failures.append(now)

        if len(state.failures) >= max_failures:
            state.lockout_tier += 1
            duration = min(max_lockout, base_lockout * (2 ** (state.lockout_tier - 1)))
            state.lockout_until = now + duration
            state.failures.clear()

    def _check_and_record_rate_limit(
        self,
        bucket: OrderedDict[str, deque[float]],
        key: str,
        now: float,
        max_requests: int,
        window_seconds: int,
        max_entries: int,
    ) -> int | None:
        cutoff = now - window_seconds
        hits = bucket.get(key)
        if hits is None:
            if len(bucket) >= max_entries:
                expired = [k for k, dq in bucket.items() if not dq or dq[-1] <= cutoff]
                for k in expired:
                    bucket.pop(k, None)
                _evict_lru(bucket, max_entries)
            hits = deque()
            bucket[key] = hits
        else:
            bucket.move_to_end(key)

        _prune_window(hits, cutoff)
        if len(hits) >= max_requests:
            return max(1, int(math.ceil(hits[0] + window_seconds - now)))
        return None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from api.oauth import (
            build_www_authenticate_header,
            is_oauth_public_path,
            oauth_manager,
            resolve_issuer,
        )

        cfg = get_api_server_config()
        oauth_cfg = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else {}
        oauth_enabled = bool(oauth_cfg.get("enabled", False))
        keys, rate_limit, auth_lockout, trusted_networks, ip_header = self._runtime_policy()
        client_ip = extract_client_ip(scope, trusted_networks, ip_header)
        scope["client_ip"] = client_ip
        now = self._clock()
        path = scope.get("path", "")
        method = (scope.get("method") or "GET").upper()
        origin = _header_value(scope, b"origin")

        cors_headers: dict[str, str] = {}
        if oauth_enabled and origin:
            cors_headers = {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers": "WWW-Authenticate, Mcp-Session-Id",
            }

        async def send_with_cors(message: dict[str, Any]) -> None:
            if cors_headers and message["type"] == "http.response.start":
                raw_hdrs = list(message.get("headers", []))
                existing_lower = {k.lower() for k, _ in raw_hdrs}
                for hk, hv in cors_headers.items():
                    if hk.lower().encode("latin-1") not in existing_lower:
                        raw_hdrs.append((hk.lower().encode("latin-1"), hv.encode("latin-1")))
                message = {**message, "headers": raw_hdrs}
            await send(message)

        # 0. Browser CORS preflight when OAuth 2.1 is enabled and Origin header is present
        if oauth_enabled and method == "OPTIONS" and origin:
            preflight_resp = Response(
                status_code=204,
                headers={
                    **cors_headers,
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": (
                        "Authorization, Content-Type, Accept, MCP-Protocol-Version, "
                        "Mcp-Session-Id, X-API-Key, X-KI-Repo"
                    ),
                    "Access-Control-Max-Age": "86400",
                },
            )
            await preflight_resp(scope, receive, send)
            return

        # 1. Pre-auth IP lockout check (never reveal key validity while locked out)
        lockout_retry_after = self.get_ip_lockout_remaining(client_ip)
        if lockout_retry_after is not None:
            resp = JSONResponse(
                status_code=429,
                content={"detail": "Too many failed authentication attempts. IP temporarily locked out."},
                headers={"Retry-After": str(lockout_retry_after), **cors_headers},
            )
            await resp(scope, receive, send)
            return

        resolved_issuer = resolve_issuer(scope, oauth_cfg=oauth_cfg) if oauth_enabled else ""

        # 1b. OAuth 2.1 discovery and authorization endpoints (/.well-known/oauth-*, /api/oauth/*)
        if is_oauth_public_path(path):
            if not oauth_enabled:
                resp = JSONResponse(status_code=404, content={"detail": "Not Found"})
                await resp(scope, receive, send)
                return

            max_requests = max(1, int(rate_limit.get("max_requests", 120)))
            window_seconds = max(1, int(rate_limit.get("window_seconds", 60)))
            with self._lock:
                ip_wait = self._check_and_record_rate_limit(
                    self._ip_hits, client_ip, now, max_requests, window_seconds, self._max_tracked_ips
                )
                if ip_wait is None:
                    self._ip_hits[client_ip].append(now)
            if ip_wait is not None:
                resp = JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please slow down."},
                    headers={"Retry-After": str(max(1, ip_wait)), **cors_headers},
                )
                await resp(scope, receive, send)
                return

            scope["api_key_id"] = "oauth:public"
            tok_mw = _ACTIVE_AUTH_MIDDLEWARE.set(self)
            tok_iss = _ACTIVE_OAUTH_ISSUER.set(resolved_issuer)
            try:
                await self.app(scope, receive, send_with_cors)
            finally:
                _ACTIVE_OAUTH_ISSUER.reset(tok_iss)
                _ACTIVE_AUTH_MIDDLEWARE.reset(tok_mw)
            return

        # 2. Constant-time API key authentication + dual OAuth 2.1 Bearer access token verification
        presented, is_bearer = extract_presented_credentials(scope)
        matched_key = verify_api_key_constant_time(presented, keys)
        key_id: str | None = None
        key_scopes: frozenset[str] = DEFAULT_API_SCOPES

        if matched_key is not None:
            key_id = matched_key.id
            key_scopes = matched_key.scopes
        elif oauth_enabled and is_bearer and presented:
            oauth_verified = oauth_manager.verify_access_token(
                presented,
                expected_issuer=resolved_issuer,
                allow_writes=bool(cfg.get("allow_writes", False)),
                configured_keys=keys,
                oauth_cfg=oauth_cfg,
            )
            if oauth_verified is not None:
                key_id, key_scopes, _payload = oauth_verified

        if key_id is None:
            with self._lock:
                self._record_failure(client_ip, now, auth_lockout)
            if oauth_enabled:
                www_auth = build_www_authenticate_header(
                    resolved_issuer,
                    error="invalid_token" if (is_bearer and presented) else None,
                )
            else:
                www_auth = "Bearer"
            resp = JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
                headers={"WWW-Authenticate": www_auth, **cors_headers},
            )
            await resp(scope, receive, send)
            return

        scope["api_key_id"] = key_id
        scope["api_key_scopes"] = key_scopes

        # 3. Reset failure streak on successful auth and enforce per-key + per-IP rate limits
        max_requests = max(1, int(rate_limit.get("max_requests", 120)))
        window_seconds = max(1, int(rate_limit.get("window_seconds", 60)))
        rate_retry_after: int | None = None
        with self._lock:
            self._ip_failures.pop(client_ip, None)
            key_wait = self._check_and_record_rate_limit(
                self._key_hits, key_id, now, max_requests, window_seconds, self._max_tracked_keys
            )
            ip_wait = self._check_and_record_rate_limit(
                self._ip_hits, client_ip, now, max_requests, window_seconds, self._max_tracked_ips
            )
            if key_wait is not None or ip_wait is not None:
                rate_retry_after = max(key_wait or 0, ip_wait or 0)
            else:
                self._key_hits[key_id].append(now)
                self._ip_hits[client_ip].append(now)

        if rate_retry_after is not None:
            resp = JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."},
                headers={"Retry-After": str(max(1, rate_retry_after)), **cors_headers},
            )
            await resp(scope, receive, send)
            return

        tok_scopes = _ACTIVE_API_SCOPES.set(key_scopes)
        tok_key = _ACTIVE_API_KEY_ID.set(key_id)
        tok_iss = _ACTIVE_OAUTH_ISSUER.set(resolved_issuer if oauth_enabled else None)
        tok_mw = _ACTIVE_AUTH_MIDDLEWARE.set(self)
        try:
            await self.app(scope, receive, send_with_cors)
        finally:
            _ACTIVE_AUTH_MIDDLEWARE.reset(tok_mw)
            _ACTIVE_OAUTH_ISSUER.reset(tok_iss)
            _ACTIVE_API_KEY_ID.reset(tok_key)
            _ACTIVE_API_SCOPES.reset(tok_scopes)


def _has_uvicorn_access_handler() -> bool:
    uv_access = logging.getLogger("uvicorn.access")
    return any(getattr(h, "_kip_log_target", None) is None for h in uv_access.handlers)


class ApiRequestLoggingMiddleware:
    """Pure ASGI middleware logging AI API requests via `api.request` without ever logging raw secrets."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "-")
        raw_path = scope.get("path", "-")
        started = time.monotonic()
        status_code = 500
        req_logger = logging.getLogger("api.request")

        async def send_with_status(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        except Exception:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            keys = get_api_server_config().get("normalized_keys", ())
            safe_path = redact_sensitive_text(raw_path, keys)
            req_logger.error(
                "%s %s -> 500 (%.1f ms) [ip=%s key=%s]",
                method,
                safe_path,
                elapsed_ms,
                scope.get("client_ip") or (scope.get("client") or ("unknown",))[0],
                scope.get("api_key_id") or "-",
                exc_info=True,
                extra={"status_code": 500},
            )
            raise

        if not _has_uvicorn_access_handler():
            elapsed_ms = (time.monotonic() - started) * 1000.0
            keys = get_api_server_config().get("normalized_keys", ())
            safe_path = redact_sensitive_text(raw_path, keys)
            if status_code >= 500:
                level = logging.ERROR
            elif status_code >= 400:
                level = logging.WARNING
            else:
                level = logging.INFO
            req_logger.log(
                level,
                "%s %s -> %d (%.1f ms) [ip=%s key=%s]",
                method,
                safe_path,
                status_code,
                elapsed_ms,
                scope.get("client_ip") or (scope.get("client") or ("unknown",))[0],
                scope.get("api_key_id") or "-",
                extra={"status_code": status_code},
            )
