"""webapp/backend/security/middleware.py - Security Headers & Rate Limiting Middleware.

Both are pure ASGI middleware: BaseHTTPMiddleware runs every request through an extra task and
memory stream, which every API call paid twice.
"""
from __future__ import annotations
import time
from collections import deque
from starlette.responses import JSONResponse

_CSP = (
    "default-src 'self'; "
    # No frontend module evaluates code from a string (no eval, no new Function), so
    # 'unsafe-eval' would only widen what an injected string could do.
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none';"
).encode()
_FIXED_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)
_FIXED_NAMES = {name for name, _ in _FIXED_HEADERS}


class SecurityHeadersMiddleware:
    """Injects defensive HTTP security headers onto every response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in _FIXED_NAMES]
                headers.extend(_FIXED_HEADERS)
                # Routes may set a stricter policy (e.g. sandboxed raw SVG); only fill in the default
                if not any(k.lower() == b"content-security-policy" for k, _ in headers):
                    headers.append((b"content-security-policy", _CSP))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class SlidingWindowRateLimiter:
    """Sliding-window in-memory rate limiter per client IP address, on /api/ requests.

    The shell, its loader and the build's assets are not limited (they are static and cached by the
    browser); a path ending in ".js" or ".json" under /api/ is an API request like any other.
    """

    def __init__(self, app, max_requests: int = 600, window_seconds: int = 60) -> None:
        self.app = app
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.hits: dict[str, deque[float]] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        now = time.monotonic()
        cutoff = now - self.window_seconds

        timestamps = self.hits.setdefault(client_ip, deque())
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= self.max_requests:
            retry_after = int(timestamps[0] + self.window_seconds - now) + 1
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."},
                headers={"Retry-After": str(max(1, retry_after))},
            )
            await response(scope, receive, send)
            return
        timestamps.append(now)

        # Periodic cleanup of expired IP keys to prevent unbounded memory growth
        if len(self.hits) > 2000:
            for ip in [ip for ip, tss in self.hits.items() if not tss or tss[-1] <= cutoff]:
                self.hits.pop(ip, None)
        await self.app(scope, receive, send)


class RepoContextMiddleware:
    """Pure ASGI middleware selecting the repository for a request (X-KI-Repo header or ?repo= query).

    Implemented without BaseHTTPMiddleware so the ContextVar set here is inherited by the endpoint,
    including sync endpoints executed in the threadpool.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from urllib.parse import parse_qs
        from webapp.backend.config import set_active_repository, reset_active_repository

        name = None
        for key, value in scope.get("headers", []):
            if key == b"x-ki-repo":
                name = value.decode("utf-8", errors="replace").strip()
                break
        if not name:
            qs = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="replace"))
            name = (qs.get("repo") or [None])[0]
        token = set_active_repository(name) if name else None
        try:
            await self.app(scope, receive, send)
        finally:
            reset_active_repository(token)
