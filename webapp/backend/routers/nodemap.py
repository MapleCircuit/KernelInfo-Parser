"""webapp/backend/routers/nodemap.py - NodeMap struct layout and source slice endpoints (webapp/API_ENDPOINTS.md section 4.14)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from webapp.backend.config import require_feature
from webapp.backend.services.nodemap_canvas_service import nodemap_canvas_service
from webapp.backend.services.nodemap_service import nodemap_service

router = APIRouter(prefix="/webapi/nodemap", tags=["nodemap"], dependencies=[Depends(require_feature("nodemap"))])


@router.get("/canvases")
def list_canvases(
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List server-side saved NodeMap canvases for the active repository."""
    return nodemap_canvas_service.list_canvases(q=q, limit=limit, offset=offset)


@router.get("/canvases/{canvas_id}")
def get_canvas(canvas_id: str) -> dict[str, Any]:
    """Load a server-side saved NodeMap canvas document by ID."""
    return nodemap_canvas_service.get_canvas(canvas_id)


@router.get("/{version_name}/struct/{name}")
def get_struct(
    version_name: str,
    name: str,
    path: str | None = Query(None),
    kind: str | None = Query(None),
    line: int | None = Query(None, ge=1),
) -> dict[str, Any]:
    """Members of a struct / union with the preprocessor guards read from its source span."""
    if len(name) > 256:
        raise HTTPException(status_code=400, detail="name must be at most 256 characters")
    return nodemap_service.get_struct(version_name, name, path, kind, line)


@router.get("/{version_name}/source")
def get_source(
    version_name: str,
    path: str | None = Query(None),
    start: int | None = Query(None, ge=1),
    end: int | None = Query(None),
    symbol: str | None = Query(None),
    kind: str | None = Query(None),
    context: int = Query(0),
) -> dict[str, Any]:
    """Lines of a file, or of a symbol's definition span plus `context` lines."""
    if symbol is not None and len(symbol) > 256:
        raise HTTPException(status_code=400, detail="symbol must be at most 256 characters")
    return nodemap_service.get_source(version_name, path, start, end, symbol, kind, context)
