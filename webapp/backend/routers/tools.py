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
@router.get("/version/{version_name}/struct/layout/{struct_name}")
@router.get("/version/{version_name}/pahole/{struct_name}")
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
@router.get("/diff/versions/{v1}/{v2}")
def get_version_diff(
    v1: str | None = None,
    v2: str | None = None,
    path: str = Query(""),
    version_a: str | None = Query(None),
    version_b: str | None = Query(None),
) -> dict[str, Any]:
    """Compare file changes across two releases."""
    va = version_a or v1 or "v3.0"
    vb = version_b or v2 or "v3.0"
    return diff_service.get_versions_diff(va, vb, path)


@router.get("/tools/diff/file")
@router.get("/diff/file")
def get_file_diff(
    version_a: str = Query(...),
    version_b: str = Query(...),
    path: str = Query(...),
    old_path: str = Query(""),
) -> dict[str, Any]:
    """Unified diff of a single file between two releases or commits."""
    return diff_service.get_file_diff(version_a, version_b, path, old_path)


@router.get("/version/diff/kconfig")
@router.get("/tools/{version_name}/diff/kconfig")
@router.get("/diff/kconfig/{v1}/{v2}")
def get_kconfig_diff(
    version_name: str = "v3.0",
    v1: str | None = None,
    v2: str | None = None,
) -> dict[str, Any]:
    """Compare KConfig symbol definitions and default values across two kernel releases."""
    ver1 = v1 or version_name or "v3.0"
    ver2 = v2 or "v3.0"
    return diff_service.get_kconfig_diff(ver1, ver2)
