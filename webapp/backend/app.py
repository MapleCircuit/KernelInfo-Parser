"""webapp/backend/app.py - FastAPI Application Factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from core.logging_setup import setup_webapp_logging
from webapp.backend.config import get_backend_logging_config, refresh_default_repo_dir
from webapp.backend.routers import (
    commits_router,
    cve_router,
    docs_router,
    filesystem_router,
    insights_router,
    kapi_router,
    kconfig_router,
    mail_router,
    maintainers_router,
    man_router,
    nodemap_router,
    symbols_router,
    sync_router,
    tools_router,
    versions_router,
    watch_router,
)
from webapp.backend.security.middleware import (
    RepoContextMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
    SlidingWindowRateLimiter,
)
from webapp.backend.services.sync_service import SyncHeaderMiddleware, sync_service

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
# The app shell and its loader are re-checked at most hourly (sync.js asks for a new build after that)
SHELL_CACHE_CONTROL = "private, max-age=3600"
# /b/<build>/... never changes for a given build
BUILD_CACHE_CONTROL = "public, max-age=31536000, immutable"


_INDEX_CACHE: dict[str, str] = {}


def render_index(build: str) -> str:
    """index.html with its assets under /b/<build>/ (relative module imports inherit the prefix).

    The build is a digest of every frontend file, index.html included, so one rendering per build is
    all that is ever needed."""
    html = _INDEX_CACHE.get(build)
    if html is None:
        html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("__KIP_BUILD__", build).replace('"/static/', f'"/b/{build}/')
        _INDEX_CACHE.clear()
        _INDEX_CACHE[build] = html
    return html


logger = logging.getLogger(__name__)


def _db_engine():
    """Engine class of `database.engine`; importing the pool bridge injects the pool configuration."""
    import webapp.backend.database.pool  # noqa: F401  (configure_pool side effect)
    from db_engine import get_db_engine
    from webapp.backend.config import get_backend_db_config

    return get_db_engine(get_backend_db_config().get("engine"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI application lifespan manager for startup pre-warming and graceful shutdown."""
    if get_backend_logging_config().get("enabled") and not any(
        getattr(h, "_kip_log_target", None) == "webapp" for h in logging.getLogger().handlers
    ):
        setup_webapp_logging()
    # Pre-warm DB pool and establish SSH tunnel if configured
    try:
        _db_engine().warm_up()
    except Exception as exc:  # noqa: BLE001 - best-effort startup pre-warm
        logger.debug("Database pool startup pre-warm deferred: %s", exc)
    # Version/branch catalogs and the default branch's tag walk, in the background (API.md section 2.12)
    try:
        from webapp.backend.routers.versions import start_prewarm
        start_prewarm()
    except Exception as exc:  # noqa: BLE001 - best-effort startup pre-warm
        logger.warning("Catalog pre-warm not started: %s", exc)
    yield
    try:
        from webapp.backend.routers.versions import cancel_branch_walks, cancel_prewarm
        cancel_prewarm()
        cancel_branch_walks()
    except Exception as exc:  # noqa: BLE001 - shutdown best-effort cleanup
        logger.debug("Shutdown branch walk cancellation skipped: %s", exc)
    # Background jobs (API.md section 2.15): cancel queued and running ones, stop the workers
    try:
        from webapp.backend.services.jobs import jobs
        jobs.cancel_all()
    except Exception as exc:  # noqa: BLE001 - shutdown best-effort cleanup
        logger.debug("Shutdown job cancellation skipped: %s", exc)
    # Graceful shutdown: close connection pool and stop SSH tunnel
    try:
        _db_engine().shutdown()
    except Exception as exc:  # noqa: BLE001 - shutdown best-effort cleanup
        logger.debug("Shutdown DB engine skipped: %s", exc)
    try:
        from core.ssh_tunnel import stop_ssh_tunnel
        stop_ssh_tunnel("database")
    except Exception as exc:  # noqa: BLE001 - shutdown best-effort cleanup
        logger.debug("Shutdown SSH tunnel skipped: %s", exc)


def create_app() -> FastAPI:
    """Create and configure the FastAPI web application instance."""
    refresh_default_repo_dir()
    setup_webapp_logging()
    app = FastAPI(
        title="KernelInfo-Parser Developer Platform",
        version="2.0.0",
        description="High-performance, zero-trust introspection and analysis platform for Linux kernel ASTs.",
        lifespan=lifespan,
    )

    # 1. Performance & Security Middleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(SlidingWindowRateLimiter, max_requests=600, window_seconds=60)
    # Read-only API with no cookie or header authentication: any origin may read it, but never with
    # credentials. "*" together with allow_credentials makes Starlette reflect the caller's Origin
    # and allow credentials, which would let any page read this API as the visitor.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    # Inside RepoContextMiddleware: the change tokens are those of the request's repository
    app.add_middleware(SyncHeaderMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    # Selection of the request's repository visible to inner middleware and endpoints
    app.add_middleware(RepoContextMiddleware)
    # Outermost: attach defensive security headers to every response (including early 429s)
    app.add_middleware(SecurityHeadersMiddleware)

    # 2. Register API Routers
    app.include_router(versions_router)
    app.include_router(filesystem_router)
    app.include_router(symbols_router)
    app.include_router(kconfig_router)
    app.include_router(maintainers_router)
    app.include_router(commits_router)
    app.include_router(tools_router)
    app.include_router(sync_router)
    app.include_router(docs_router)
    app.include_router(mail_router)
    app.include_router(insights_router)
    app.include_router(cve_router)
    app.include_router(kapi_router)
    app.include_router(watch_router)
    app.include_router(man_router)
    app.include_router(nodemap_router)

    # 3. Frontend files outside a build (the manifest's icon); the shell loads /b/<build>/...
    if FRONTEND_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    @app.get("/app")
    @app.get("/webapp")
    def serve_frontend():
        """Serve the single-page application shell, bound to the current frontend build."""
        if (FRONTEND_DIR / "index.html").is_file():
            return HTMLResponse(render_index(sync_service.frontend_build()), headers={"Cache-Control": SHELL_CACHE_CONTROL})
        return {"status": "KernelInfo-Parser Web API active. Frontend index.html not yet built."}

    @app.get("/boot.js")
    def serve_boot():
        """Loader of the build's modules; recovers when the shell references a build the server no longer has."""
        return FileResponse(str(FRONTEND_DIR / "boot.js"), media_type="text/javascript",
                            headers={"Cache-Control": SHELL_CACHE_CONTROL})

    @app.get("/b/{build}/{asset_path:path}")
    def serve_build_asset(build: str, asset_path: str):
        """A frontend file of the current build. Another build's URL answers 409: the page must reload
        rather than mix modules of two builds."""
        current = sync_service.frontend_build()
        if build != current:
            return JSONResponse({"detail": "stale frontend build", "build": current}, status_code=409,
                                headers={"Cache-Control": "no-store"})
        try:
            target = (FRONTEND_DIR / asset_path).resolve()
            is_inside = target.is_relative_to(FRONTEND_DIR)
        except (OSError, ValueError):
            return Response(status_code=404)
        # The JS unit tests (webapp/frontend/tests) are not part of a build
        if asset_path.startswith("tests/") or not is_inside or not target.is_file():
            return Response(status_code=404)
        return FileResponse(str(target), headers={"Cache-Control": BUILD_CACHE_CONTROL})

    @app.api_route("/api/{unmatched_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    def api_not_found(unmatched_path: str):
        """Consistent JSON 404 for unknown /api/ paths."""
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    @app.get("/manifest.json")
    def serve_manifest():
        manifest_path = FRONTEND_DIR / "manifest.json"
        if manifest_path.is_file():
            return FileResponse(str(manifest_path), media_type="application/manifest+json")
        return {}

    @app.get("/sw.js")
    def serve_sw():
        sw_path = FRONTEND_DIR / "sw.js"
        if sw_path.is_file():
            return FileResponse(str(sw_path), media_type="application/javascript", headers={"Cache-Control": BUILD_CACHE_CONTROL})
        return Response(status_code=404)

    return app
