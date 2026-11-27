"""api/routers/nodemap.py - NodeMap struct/source inspection and programmatic canvas authoring endpoints."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from api.config import (
    get_active_repo_name,
    get_api_server_config,
    require_feature,
    resolve_version,
)
from api.routers.common import (
    DEFAULT_ARCH,
    _with_repo,
    nodemap_canvas_service,
    nodemap_service,
)
from api.security import get_active_api_key_id, require_write_scope

router = APIRouter()


class NodemapPreviewRequest(BaseModel):
    """Stateless read-only NodeMap canvas construction, hydration, expansion, layout, and export request."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Preview Canvas", max_length=256)
    version: str | None = Field(default=None, max_length=256)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    groups: list[dict[str, Any]] = Field(default_factory=list)
    ops: list[dict[str, Any]] = Field(default_factory=list)
    expansions: list[dict[str, Any]] = Field(default_factory=list)
    expand: dict[str, Any] | list[dict[str, Any]] | None = None
    layout: dict[str, Any] | str | None = None
    settings: dict[str, Any] | None = None
    tours: list[dict[str, Any]] = Field(default_factory=list)
    canvas: dict[str, Any] | None = None
    hydrate: bool = True
    auto_layout: str | None = Field(default="dag")
    export_format: str | None = Field(default=None, pattern="^(json|mermaid|dot)$")
    direction: str = Field(default="LR", pattern="^(LR|RL|TB|TD|BT)$")


class NodemapCreateRequest(BaseModel):
    """Create and persist a new NodeMap canvas document."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(default="Untitled Canvas", min_length=1, max_length=256)
    description: str = Field(default="", max_length=2000)
    version: str | None = Field(default=None, max_length=256)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    groups: list[dict[str, Any]] = Field(default_factory=list)
    ops: list[dict[str, Any]] = Field(default_factory=list)
    expansions: list[dict[str, Any]] = Field(default_factory=list)
    expand: dict[str, Any] | list[dict[str, Any]] | None = None
    layout: dict[str, Any] | str | None = None
    settings: dict[str, Any] | None = None
    tours: list[dict[str, Any]] = Field(default_factory=list)
    canvas: dict[str, Any] | None = None
    hydrate: bool = True
    auto_layout: str | None = Field(default="dag")


class NodemapReplaceRequest(BaseModel):
    """Full replacement of a saved NodeMap canvas document with optional optimistic concurrency check."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    version: str | None = Field(default=None, max_length=256)
    expected_revision: int | None = Field(default=None, ge=1)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    groups: list[dict[str, Any]] = Field(default_factory=list)
    ops: list[dict[str, Any]] = Field(default_factory=list)
    layout: dict[str, Any] | str | None = None
    settings: dict[str, Any] | None = None
    tours: list[dict[str, Any]] = Field(default_factory=list)
    canvas: dict[str, Any] | None = None
    hydrate: bool = True
    auto_layout: str | None = None


class NodemapMutateRequest(BaseModel):
    """Atomic batch of declarative operations over a saved NodeMap canvas."""

    model_config = ConfigDict(extra="forbid")

    ops: list[dict[str, Any]] = Field(default_factory=list)
    upsert_nodes: list[dict[str, Any]] | None = None
    remove_nodes: list[str] | None = None
    upsert_edges: list[dict[str, Any]] | None = None
    remove_edges: list[str] | None = None
    upsert_groups: list[dict[str, Any]] | None = None
    annotate: dict[str, Any] | None = None
    upsert_tour: dict[str, Any] | None = None
    upsert_tours: list[dict[str, Any]] | None = None
    layout: dict[str, Any] | str | None = None
    name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    version: str | None = Field(default=None, max_length=256)
    expected_revision: int | None = Field(default=None, ge=1)
    hydrate: bool = True
    auto_layout: str | None = None


class NodemapExpandRequest(BaseModel):
    """Server-side graph expansion over a saved NodeMap canvas."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: str = Field(pattern="^(struct_types|callgraph|callpath|kconfig|commit|impact_map)$")
    node_id: str | None = Field(default=None, max_length=64)
    struct: str | None = Field(default=None, max_length=256)
    function: str | None = Field(default=None, max_length=256)
    direction: str = Field(default="both", pattern="^(both|callers|callees|in|out)$")
    from_func: str | None = Field(default=None, alias="from", max_length=256)
    to_func: str | None = Field(default=None, alias="to", max_length=256)
    max_depth: int = Field(default=6, ge=1, le=10)
    k: int = Field(default=5, ge=1, le=20)
    symbol: str | None = Field(default=None, max_length=256)
    arch: str = Field(default=DEFAULT_ARCH, max_length=64)
    commit_hash: str | None = Field(default=None, max_length=64)
    seed_kind: str | None = Field(default=None, max_length=64)
    target: str | None = Field(default=None, max_length=512)
    path: str | None = Field(default=None, max_length=512)
    files: list[str] = Field(default_factory=list)
    diff_text: str = Field(default="", max_length=200000)
    title: str = Field(default="", max_length=256)
    limit: int = Field(default=16, ge=1, le=100)
    version: str | None = Field(default=None, max_length=256)
    expected_revision: int | None = Field(default=None, ge=1)
    auto_layout: str | bool | None = Field(default="dag")


class NodemapLayoutRequest(BaseModel):
    """Deterministic Sugiyama DAG or force-directed layout over a saved NodeMap canvas."""

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(default="dag", pattern="^(dag|force)$")
    group_id: str | None = Field(default=None, max_length=64)
    node_ids: list[str] | None = None
    fit_groups: bool = True
    options: dict[str, Any] | None = None
    expected_revision: int | None = Field(default=None, ge=1)


@router.get("/nodemap/struct", dependencies=[Depends(require_feature("nodemap"))])
def get_nodemap_struct(
    name: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    path: Annotated[str | None, Query(max_length=512)] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    line: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """Members of a struct/union with preprocessor guards read from its source span."""
    ver = resolve_version(version)
    res = nodemap_service.get_struct(ver, name, path=path, kind=kind, line=line)
    return {"repo": get_active_repo_name(), **res}


@router.get("/nodemap/source", dependencies=[Depends(require_feature("nodemap"))])
def get_nodemap_source(
    path: Annotated[str | None, Query(max_length=512)] = None,
    symbol: Annotated[str | None, Query(max_length=256)] = None,
    version: Annotated[str | None, Query(max_length=256)] = None,
    start: Annotated[int | None, Query(ge=1)] = None,
    end: Annotated[int | None, Query(ge=1)] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    context: Annotated[int, Query(ge=0, le=200)] = 0,
) -> dict[str, Any]:
    """Lines of a file, line range, or symbol definition span plus `context` lines for NodeMap cards."""
    ver = resolve_version(version)
    res = nodemap_service.get_source(ver, path=path, start=start, end=end, symbol=symbol, kind=kind, context=context)
    return {"repo": get_active_repo_name(), **res}


@router.get("/nodemap/canvases", dependencies=[Depends(require_feature("nodemap"))])
def list_nodemap_canvases(
    q: Annotated[str, Query(max_length=256)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Paginated list of saved NodeMap canvases for the active repository."""
    return _with_repo(nodemap_canvas_service.list_canvases(q=q, limit=limit, offset=offset))


@router.get("/nodemap/canvases/{canvas_id}", dependencies=[Depends(require_feature("nodemap"))])
def get_nodemap_canvas(canvas_id: str) -> dict[str, Any]:
    """Retrieve a saved NodeMap canvas document, metadata, and webapp URL."""
    return _with_repo(nodemap_canvas_service.get_canvas(canvas_id))


@router.get("/nodemap/canvases/{canvas_id}/export", dependencies=[Depends(require_feature("nodemap"))])
def export_nodemap_canvas(
    canvas_id: str,
    format: Annotated[str, Query(pattern="^(json|mermaid|dot)$")] = "json",
    direction: Annotated[str, Query(pattern="^(LR|RL|TB|TD|BT)$")] = "LR",
) -> dict[str, Any]:
    """Export a saved NodeMap canvas in `"json"`, `"mermaid"`, or `"dot"` format."""
    return _with_repo(nodemap_canvas_service.export_saved_canvas(canvas_id, fmt=format, direction=direction))


@router.post("/nodemap/preview", dependencies=[Depends(require_feature("nodemap"))])
def preview_nodemap_canvas(payload: NodemapPreviewRequest) -> dict[str, Any]:
    """Stateless read-only NodeMap canvas builder: normalize, hydrate, expand, lay out, and optionally export in memory."""
    ver = resolve_version(payload.version)
    data = payload.model_dump(by_alias=True)
    if str(data.get("auto_layout") or "").strip().lower() in ("none", "false", "0", ""):
        data["auto_layout"] = None
    return _with_repo(
        nodemap_canvas_service.preview_canvas(data, default_version=ver, default_repo=get_active_repo_name())
    )


@router.post(
    "/nodemap/canvases",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def create_nodemap_canvas(payload: NodemapCreateRequest) -> dict[str, Any]:
    """Create and persist a new NodeMap canvas document on disk."""
    ver = resolve_version(payload.version)
    data = payload.model_dump(by_alias=True)
    if str(data.get("auto_layout") or "").strip().lower() in ("none", "false", "0", ""):
        data["auto_layout"] = None
    return _with_repo(
        nodemap_canvas_service.create_canvas(
            data,
            default_version=ver,
            created_by=get_active_api_key_id() or "api",
        )
    )


@router.put(
    "/nodemap/canvases/{canvas_id}",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def replace_nodemap_canvas(canvas_id: str, payload: NodemapReplaceRequest) -> dict[str, Any]:
    """Replace a saved NodeMap canvas with optional `expected_revision` optimistic concurrency check."""
    ver = resolve_version(payload.version) if payload.version else get_api_server_config()["default_version"]
    data = payload.model_dump(by_alias=True, exclude_unset=True)
    if str(data.get("auto_layout") or "").strip().lower() in ("none", "false", "0", ""):
        data["auto_layout"] = None
    return _with_repo(
        nodemap_canvas_service.replace_canvas(
            canvas_id,
            data,
            default_version=ver,
            updated_by=get_active_api_key_id() or "api",
        )
    )


@router.delete(
    "/nodemap/canvases/{canvas_id}",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def delete_nodemap_canvas(canvas_id: str) -> dict[str, Any]:
    """Delete a saved NodeMap canvas JSON file."""
    return _with_repo(nodemap_canvas_service.delete_canvas(canvas_id))


@router.post(
    "/nodemap/canvases/{canvas_id}/ops",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def mutate_nodemap_canvas(canvas_id: str, payload: NodemapMutateRequest) -> dict[str, Any]:
    """Apply an atomic batch of declarative operations to a saved NodeMap canvas."""
    data = payload.model_dump(by_alias=True, exclude_none=True)
    if payload.version:
        data["version"] = resolve_version(payload.version)
    if str(data.get("auto_layout") or "").strip().lower() in ("none", "false", "0", ""):
        data["auto_layout"] = None
    return _with_repo(nodemap_canvas_service.mutate_canvas(canvas_id, data))


@router.post(
    "/nodemap/canvases/{canvas_id}/expand",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def expand_nodemap_graph(canvas_id: str, payload: NodemapExpandRequest) -> dict[str, Any]:
    """Expand a saved NodeMap canvas with server-side graph discovery (`struct_types`, `callgraph`, `callpath`, `kconfig`, `commit`, `impact_map`)."""
    data = payload.model_dump(by_alias=True, exclude_none=True)
    if payload.version:
        data["version"] = resolve_version(payload.version)
    if str(data.get("auto_layout") or "").strip().lower() in ("none", "false", "0", ""):
        data["auto_layout"] = None
    return _with_repo(nodemap_canvas_service.expand_canvas(canvas_id, data))


@router.post(
    "/nodemap/canvases/{canvas_id}/layout",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def layout_nodemap_canvas(canvas_id: str, payload: NodemapLayoutRequest) -> dict[str, Any]:
    """Run deterministic Sugiyama `"dag"` or `"force"` layout over a saved NodeMap canvas."""
    return _with_repo(nodemap_canvas_service.layout_canvas(canvas_id, payload.model_dump(by_alias=True, exclude_none=True)))
