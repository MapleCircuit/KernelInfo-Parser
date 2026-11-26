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
from dataclasses import dataclass, field
from typing import Any

from starlette.responses import JSONResponse

from api.config import get_api_server_config, normalize_api_keys

MAX_TRACKED_IPS = 4096
MAX_TRACKED_KEYS = 4096

_BEARER_RE = re.compile(r"(Bearer\s+)[^\s,;\"']+", re.IGNORECASE)
_API_KEY_KV_RE = re.compile(r"(X-API-Key\s*[:=]\s*)[^\s,;\"']+", re.IGNORECASE)


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


def extract_presented_key(scope: dict[str, Any]) -> str | None:
    """Extract an API key from `Authorization: Bearer <key>` or `X-API-Key: <key>`."""
    x_api_key = _header_value(scope, b"x-api-key")
    if x_api_key is not None:
        return x_api_key.strip() or None

    auth = _header_value(scope, b"authorization")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
            return token or None
    return None


def verify_api_key_constant_time(candidate: str | None, configured_keys: list[tuple[str, str]]) -> str | None:
    """Compare `candidate` against all configured keys in constant time without short-circuiting.

    Returns the matched key name or None. Always returns None when `configured_keys` is empty.
    """
    if not configured_keys:
        return None
    # Fixed-length SHA-256 digests on both sides: the comparison time depends on neither key's length
    cand_digest = hashlib.sha256((candidate or "").encode("utf-8")).digest()
    cand_non_empty = bool(candidate)
    matched_id: str | None = None
    for key_id, secret in configured_keys:
        ok = hmac.compare_digest(cand_digest, hashlib.sha256(secret.encode("utf-8")).digest())
        if ok and cand_non_empty and matched_id is None:
            matched_id = key_id
    return matched_id


def redact_sensitive_text(text: str, configured_keys: Iterable[tuple[str, str]] = ()) -> str:
    """Scrub `Authorization: Bearer ...`, `X-API-Key`, and raw configured secrets from text."""
    if not text:
        return text
    cleaned = _BEARER_RE.sub(r"\1[REDACTED]", text)
    cleaned = _API_KEY_KV_RE.sub(r"\1[REDACTED]", cleaned)
    for _name, secret in configured_keys:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


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

        keys, rate_limit, auth_lockout, trusted_networks, ip_header = self._runtime_policy()
        client_ip = extract_client_ip(scope, trusted_networks, ip_header)
        now = self._clock()

        # 1. Pre-auth IP lockout check (never reveal key validity while locked out)
        lockout_retry_after: int | None = None
        with self._lock:
            state = self._ip_failures.get(client_ip)
            if state is not None and state.lockout_until > now:
                self._ip_failures.move_to_end(client_ip)
                lockout_retry_after = max(1, int(math.ceil(state.lockout_until - now)))

        if lockout_retry_after is not None:
            resp = JSONResponse(
                status_code=429,
                content={"detail": "Too many failed authentication attempts. IP temporarily locked out."},
                headers={"Retry-After": str(lockout_retry_after)},
            )
            await resp(scope, receive, send)
            return

        # 2. Constant-time API key authentication (fail closed when no keys are configured)
        presented = extract_presented_key(scope)
        key_id = verify_api_key_constant_time(presented, keys)
        if key_id is None:
            with self._lock:
                self._record_failure(client_ip, now, auth_lockout)
            resp = JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await resp(scope, receive, send)
            return

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
                headers={"Retry-After": str(max(1, rate_retry_after))},
            )
            await resp(scope, receive, send)
            return

        scope["api_key_id"] = key_id
        scope["client_ip"] = client_ip
        await self.app(scope, receive, send)


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
