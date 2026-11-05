"""webapp/backend/routers/system.py - Server change tokens (database, frontend build, parsed versions, Git refs, repositories)."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter

from webapp.backend.services.sync_service import sync_service

sync_router = APIRouter(prefix="/api", tags=["system"])


@sync_router.get("/sync")
def get_sync(fresh: bool = False) -> dict[str, Any]:
    """Change tokens of the server: database instance, frontend build, parsed versions, Git refs and the
    repository list.

    Clients compare them with the ones they saw last (webapp/frontend/js/sync.js); see
    webapp/backend/services/sync_service.py for what each one covers. `fresh=true` recomputes the
    tokens instead of serving the cached ones (not sent by the frontend).
    """
    return sync_service.snapshot(fresh=fresh)
