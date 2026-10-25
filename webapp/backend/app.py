"""webapp/backend/app.py - FastAPI Application Factory."""
from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from webapp.backend.security.middleware import RepoContextMiddleware, SecurityHeadersMiddleware, SlidingWindowRateLimiter
from webapp.backend.routers import (
    versions_router,
    filesystem_router,
    symbols_router,
    kconfig_router,
    maintainers_router,
    commits_router,
    tools_router,
    system_router,
    sync_router,
    docs_router,
)
from webapp.backend.services.sync_service import SyncHeaderMiddleware, sync_service

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
# The app shell and its loader are re-checked at most hourly (sync.js asks for a new build after that)
SHELL_CACHE_CONTROL = "private, max-age=3600"
# /b/<build>/... never changes for a given build
BUILD_CACHE_CONTROL = "public, max-age=31536000, immutable"


def render_index(build: str) -> str:
    """index.html with its assets under /b/<build>/ (relative module imports inherit the prefix)."""
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("__KIP_BUILD__", build).replace('"/static/', f'"/b/{build}/')


def create_app() -> FastAPI:
    """Create and configure the FastAPI web application instance."""
    app = FastAPI(
        title="KernelInfo-Parser Developer Platform",
        version="2.0.0",
        description="High-performance, zero-trust introspection and analysis platform for Linux kernel ASTs.",
    )

    # 1. Performance & Security Middleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(SlidingWindowRateLimiter, max_requests=600, window_seconds=60)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Inside RepoContextMiddleware: the change tokens are those of the request's repository
    app.add_middleware(SyncHeaderMiddleware)
    # Outermost: the selected repository must be visible to every inner middleware and endpoint
    app.add_middleware(RepoContextMiddleware)

    # 2. Register API Routers
    app.include_router(versions_router)
    app.include_router(filesystem_router)
    app.include_router(symbols_router)
    app.include_router(kconfig_router)
    app.include_router(maintainers_router)
    app.include_router(commits_router)
    app.include_router(tools_router)
    app.include_router(system_router)
    app.include_router(sync_router)
    app.include_router(docs_router)

    # 3. Mount Frontend Static Assets
    if FRONTEND_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
        for subdir in ["css", "js", "assets", "vendor"]:
            subpath = FRONTEND_DIR / subdir
            if subpath.is_dir():
                app.mount(f"/{subdir}", StaticFiles(directory=str(subpath)), name=subdir)

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
        target = (FRONTEND_DIR / asset_path).resolve()
        if not target.is_relative_to(FRONTEND_DIR) or not target.is_file():
            return Response(status_code=404)
        return FileResponse(str(target), headers={"Cache-Control": BUILD_CACHE_CONTROL})

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
            return FileResponse(str(sw_path), media_type="application/javascript", headers={"Cache-Control": "no-cache"})
        return Response(status_code=404)

    return app
