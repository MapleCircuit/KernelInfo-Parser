"""webapp/backend/app.py - FastAPI Application Factory."""
from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response

from webapp.backend.security.middleware import SecurityHeadersMiddleware, SlidingWindowRateLimiter
from webapp.backend.routers import (
    versions_router,
    filesystem_router,
    symbols_router,
    kconfig_router,
    maintainers_router,
    commits_router,
    tools_router,
    system_router,
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


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

    # 2. Register API Routers
    app.include_router(versions_router)
    app.include_router(filesystem_router)
    app.include_router(symbols_router)
    app.include_router(kconfig_router)
    app.include_router(maintainers_router)
    app.include_router(commits_router)
    app.include_router(tools_router)
    app.include_router(system_router)

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
        """Serve the single-page application shell."""
        index_path = FRONTEND_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(str(index_path))
        return {"status": "KernelInfo-Parser Web API active. Frontend index.html not yet built."}

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
            return FileResponse(str(sw_path), media_type="application/javascript")
        return Response(status_code=404)

    return app
