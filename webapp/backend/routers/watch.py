"""webapp/backend/routers/watch.py - Watch list change checks (G10, webapp/API.md section 4.12)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

router = APIRouter(prefix="/api/watch", tags=["watch"])


@router.post("/changes")
def post_watch_changes(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """What changed between `from` and `to` for each watched item (file, dir, symbol, kconfig, section)."""
    from webapp.backend.services.watch_service import check_changes
    return check_changes(payload)
