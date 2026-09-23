"""webapp/backend/routers/tools.py - Pahole Layout, Function Callgraph & Semantic Version Diff."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query
from webapp.backend.services.pahole_service import PaholeService
from webapp.backend.services.callgraph_service import CallgraphService
from webapp.backend.services.diff_service import DiffService

router = APIRouter(prefix="/api", tags=["tools"])
pahole_service = PaholeService()
callgraph_service = CallgraphService()
diff_service = DiffService()


@router.get("/struct/layout")
@router.get("/tools/{version_name}/pahole/{struct_name}")
def get_struct_layout(
    version: str = Query("v3.0", alias="version_name"),
    name: str = Query(None, alias="struct_name"),
    version_name: str | None = None,
    struct_name: str | None = None,
) -> dict[str, Any]:
    """Pahole-style memory alignment and padding analysis for C structs."""
    v = version_name or version
    s = struct_name or name or "task_struct"
    return pahole_service.get_struct_layout(v, s)


@router.get("/version/{version_name}/callgraph/{function_name}")
@router.get("/tools/{version_name}/callgraph/{function_name}")
def get_callgraph(version_name: str, function_name: str) -> dict[str, Any]:
    """Bidirectional caller and callee callgraph."""
    return callgraph_service.get_function_callgraph(version_name, function_name)


@router.get("/version/diff")
@router.get("/tools/diff/versions")
def get_version_diff(
    v1: str = Query(None),
    v2: str = Query(None),
    path: str = Query(""),
    version_a: str = Query(None),
    version_b: str = Query(None),
) -> dict[str, Any]:
    """Compare file changes across two releases."""
    va = version_a or v1 or "v3.0"
    vb = version_b or v2 or "v3.0"
    return diff_service.get_versions_diff(va, vb, path)


@router.get("/version/diff/kconfig")
@router.get("/tools/{version_name}/diff/kconfig")
def get_kconfig_diff(
    version_name: str = "v3.0",
    v1: str = Query("v3.0"),
    v2: str = Query("v3.0"),
) -> dict[str, Any]:
    """Compare KConfig symbol definitions and default values across two kernel releases."""
    return diff_service.get_kconfig_diff(v1, v2)
