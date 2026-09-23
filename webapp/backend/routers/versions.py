"""webapp/backend/routers/versions.py - Release Versions Catalog."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import safe_decode

router = APIRouter(prefix="/api", tags=["versions"])


@router.get("/versions")
def get_versions() -> dict[str, Any]:
    """List all available kernel versions stored in m_v_main."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT vid, vname FROM m_v_main ORDER BY vid ASC;")
        versions = [{"vid": r["vid"], "vname": safe_decode(r["vname"])} for r in cursor.fetchall()]
        return {"total": len(versions), "versions": versions}
