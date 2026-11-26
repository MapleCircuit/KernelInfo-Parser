"""api/app.py - FastAPI Application Factory for the Standalone AI API Service."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from api.config import get_db_config, get_logging_config, refresh_default_repo_dir
from api.mcp import router as mcp_router
from api.routers import v1_router
from api.security import ApiAuthMiddleware, ApiRequestLoggingMiddleware
from core.logging_setup import setup_api_logging
from webapp.backend.security.middleware import (
    RepoContextMiddleware,
    SecurityHeadersMiddleware,
)

logger = logging.getLogger(__name__)


def _db_engine():
    """Engine class of `database.engine`; importing the pool bridge injects the pool configuration."""
    import webapp.backend.database.pool  # noqa: F401  (configure_pool side effect)
    from db_engine import get_db_engine

    return get_db_engine(get_db_config().get("engine"))


def startup_resources() -> None:
    """Best-effort DB pool warm-up shared by the HTTP lifespan and `api/mcp_stdio.py`."""
    try:
        _db_engine().warm_up()
    except Exception as exc:  # noqa: BLE001 - best-effort startup pre-warm
        logger.debug("Database pool startup pre-warm deferred: %s", exc)


def shutdown_resources() -> None:
    """Cancel jobs, shut the DB engine down, and stop the database SSH tunnel (HTTP lifespan and stdio)."""
    try:
        from webapp.backend.services.jobs import jobs

        jobs.cancel_all()
    except Exception as exc:  # noqa: BLE001 - shutdown best-effort cleanup
        logger.debug("Shutdown job cancellation skipped: %s", exc)
    try:
        _db_engine().shutdown()
    except Exception as exc:  # noqa: BLE001 - shutdown best-effort cleanup
        logger.debug("Shutdown DB engine skipped: %s", exc)
    try:
        from core.ssh_tunnel import stop_ssh_tunnel

        stop_ssh_tunnel("database")
    except Exception as exc:  # noqa: BLE001 - shutdown best-effort cleanup
        logger.debug("Shutdown SSH tunnel skipped: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI application lifespan manager for DB pool warm-up and graceful shutdown."""
    if get_logging_config("api").get("enabled") and not any(
        getattr(h, "_kip_log_target", None) == "api" for h in logging.getLogger().handlers
    ):
        setup_api_logging()
    startup_resources()
    yield
    shutdown_resources()


def create_app() -> FastAPI:
    """Create and configure the standalone authenticated AI API FastAPI application."""
    refresh_default_repo_dir()
    setup_api_logging()
    app = FastAPI(
        title="KernelInfo-Parser AI & Programmatic API",
        version="1.0.0",
        description=(
            "Authenticated, read-only programmatic and AI agent introspection service "
            "for indexed kernel repositories, ASTs, symbols, history, Kconfig, maintainers, docs, man pages, and CVEs."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # No CORSMiddleware: programmatic/AI clients only, zero wildcard browser CORS.
    # Middleware order (added innermost -> outermost):
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(RepoContextMiddleware)
    app.add_middleware(ApiAuthMiddleware)
    app.add_middleware(ApiRequestLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(v1_router)
    # MCP Streamable HTTP (`POST /api/mcp`) sits behind the same middleware stack as /api/v1/*.
    app.include_router(mcp_router)
    return app
