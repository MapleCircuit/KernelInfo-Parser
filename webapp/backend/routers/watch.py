"""webapp/backend/routers/watch.py - Watch list change checks (G10, webapp/API.md section 4.12)."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from webapp.backend.security.git import safe_rev

router = APIRouter(prefix="/api/watch", tags=["watch"])


@router.post("/changes")
def post_watch_changes(payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """What changed between `from` and `to` for each watched item (file, dir, symbol, kconfig, section)."""
    from webapp.backend.services.watch_service import check_changes

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    for side in ("from", "to"):
        rev = payload.get(side)
        if not isinstance(rev, str):
            raise HTTPException(status_code=400, detail=f"Invalid {side}: must be a tag, branch or commit SHA.")
        safe_rev(rev, side)
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 200:
        raise HTTPException(status_code=400, detail="items must be a list of at most 200 watches")
    return check_changes(payload)
