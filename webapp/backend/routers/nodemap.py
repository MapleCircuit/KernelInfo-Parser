"""webapp/backend/routers/nodemap.py - NodeMap struct layout, source slice and saved canvas endpoints (webapp/API_ENDPOINTS.md section 4.14).

Saved canvases are JSON files of `nodemap_canvas_service` (never the database). Writes are open unless
`webapp.nodemap_canvas_writes` is false (403); nested NodeMap references are validated by the service.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from core.config import get_config
from webapp.backend.config import require_feature
from webapp.backend.services.nodemap_canvas_service import nodemap_canvas_service
from webapp.backend.services.nodemap_nesting import (
    HIERARCHY_DEPTH_DEFAULT,
    HIERARCHY_DEPTH_MAX,
)
from webapp.backend.services.nodemap_service import nodemap_service

router = APIRouter(prefix="/webapi/nodemap", tags=["nodemap"], dependencies=[Depends(require_feature("nodemap"))])

JsonBody = Annotated[dict[str, Any], Body()]
_CREATED_BY = "webapp"
# Keys of the write bodies forwarded to the service (anything else is ignored)
_CANVAS_KEYS = ("id", "name", "description", "version", "canvas", "expected_revision")
_OPS_KEYS = ("ops", "expected_revision", "name", "description")
_ATTACH_KEYS = ("target_id", "new_canvas_id", "name", "description", "version", "node", "expected_revision")
_GROUP_KEYS = ("node_ids", "new_canvas_id", "name", "description", "version", "node", "expected_revision")


def require_canvas_writes() -> None:
    """403 unless saved canvas writes are enabled (`webapp.nodemap_canvas_writes`, default true)."""
    if (get_config().get("webapp") or {}).get("nodemap_canvas_writes", True) is False:
        raise HTTPException(status_code=403, detail="Saved NodeMap canvas writes are disabled (webapp.nodemap_canvas_writes).")


def _body(payload: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object.")
    return {k: payload[k] for k in keys if k in payload}


@router.get("/canvases")
def list_canvases(
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    insertable_into: str | None = Query(None, max_length=64),
) -> dict[str, Any]:
    """List server-side saved NodeMap canvases for the active repository (`insertable` per item with `insertable_into`)."""
    return nodemap_canvas_service.list_canvases(q=q, limit=limit, offset=offset, insertable_into=insertable_into)


@router.post("/canvases", dependencies=[Depends(require_canvas_writes)])
def create_canvas(payload: JsonBody) -> dict[str, Any]:
    """Create a saved canvas (`id` optional; 409 when it exists)."""
    body = _body(payload, _CANVAS_KEYS)
    body.pop("expected_revision", None)
    return nodemap_canvas_service.create_canvas({**body, "hydrate": False}, created_by=_CREATED_BY)


@router.get("/canvases/{canvas_id}")
def get_canvas(canvas_id: str) -> dict[str, Any]:
    """Load a server-side saved NodeMap canvas document by ID, with `nested_maps` previews of its nested references."""
    return nodemap_canvas_service.get_canvas(canvas_id)


@router.put("/canvases/{canvas_id}", dependencies=[Depends(require_canvas_writes)])
def replace_canvas(canvas_id: str, payload: JsonBody) -> dict[str, Any]:
    """Replace a saved canvas document (`expected_revision` -> 409 on conflict)."""
    body = _body(payload, _CANVAS_KEYS)
    body.pop("id", None)
    return nodemap_canvas_service.replace_canvas(canvas_id, {**body, "hydrate": False}, updated_by=_CREATED_BY)


@router.delete("/canvases/{canvas_id}", dependencies=[Depends(require_canvas_writes)])
def delete_canvas(canvas_id: str) -> dict[str, Any]:
    """Delete a saved canvas; `referenced_by` lists the canvases whose nested nodes now read as missing."""
    return nodemap_canvas_service.delete_canvas(canvas_id)


@router.post("/canvases/{canvas_id}/ops", dependencies=[Depends(require_canvas_writes)])
def mutate_canvas(canvas_id: str, payload: JsonBody) -> dict[str, Any]:
    """Apply declarative operations (`upsert_nodes`, `remove_nodes`, `upsert_edges`, ...) without hydration."""
    return nodemap_canvas_service.mutate_canvas(canvas_id, {**_body(payload, _OPS_KEYS), "hydrate": False})


@router.get("/canvases/{canvas_id}/hierarchy")
def get_canvas_hierarchy(
    canvas_id: str,
    depth: int = Query(HIERARCHY_DEPTH_DEFAULT, ge=1, le=HIERARCHY_DEPTH_MAX),
) -> dict[str, Any]:
    """Nested reference tree, parents, ancestors and invalid insertion targets of a canvas (no node payloads)."""
    return nodemap_canvas_service.canvas_hierarchy(canvas_id, depth=depth)


@router.post("/canvases/{canvas_id}/nested", dependencies=[Depends(require_canvas_writes)])
def attach_nested_canvas(canvas_id: str, payload: JsonBody) -> dict[str, Any]:
    """Add a nested NodeMap node referencing `target_id`, or a new empty canvas when `target_id` is omitted."""
    return nodemap_canvas_service.attach_nested(canvas_id, _body(payload, _ATTACH_KEYS), created_by=_CREATED_BY)


@router.post("/canvases/{canvas_id}/group", dependencies=[Depends(require_canvas_writes)])
def group_into_nested_canvas(canvas_id: str, payload: JsonBody) -> dict[str, Any]:
    """Move `node_ids` into a new nested canvas, rewiring boundary edges to the nested node."""
    return nodemap_canvas_service.group_into_nested(canvas_id, _body(payload, _GROUP_KEYS), created_by=_CREATED_BY)


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
