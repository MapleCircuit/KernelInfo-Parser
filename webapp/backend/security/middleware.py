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
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
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
