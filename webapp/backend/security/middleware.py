"""webapp/backend/security/middleware.py - Security Headers & Rate Limiting Middleware."""
from __future__ import annotations
import time
from collections import defaultdict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Injects defensive HTTP security headers onto every response."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Routes may set a stricter policy (e.g. sandboxed raw SVG); only fill in the default
        response.headers.setdefault("Content-Security-Policy", (
            "default-src 'self'; "
            # No frontend module evaluates code from a string (no eval, no new Function), so
            # 'unsafe-eval' would only widen what an injected string could do.
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        ))
        return response


class SlidingWindowRateLimiter(BaseHTTPMiddleware):
    """Sliding-window in-memory rate limiter per client IP address."""

    def __init__(self, app, max_requests: int = 600, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.hits: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        # Allow static files and asset requests to bypass rate limiter
        path = request.url.path
        if path.startswith("/static") or path.endswith((".js", ".css", ".png", ".svg", ".json")):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        cutoff = now - self.window_seconds

        # Clean old timestamps
        timestamps = [ts for ts in self.hits[client_ip] if ts > cutoff]
        if len(timestamps) >= self.max_requests:
            retry_after = int(timestamps[0] + self.window_seconds - now) + 1
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."},
                headers={"Retry-After": str(max(1, retry_after))},
            )

        timestamps.append(now)
        self.hits[client_ip] = timestamps
        return await call_next(request)


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
