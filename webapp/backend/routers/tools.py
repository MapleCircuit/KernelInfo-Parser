"""webapp/backend/routers/tools.py - Pahole Layout, Function Callgraph & Revision Compare."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query
from webapp.backend.services.pahole_service import PaholeService
from webapp.backend.services.callgraph_service import CallgraphService
from webapp.backend.services.diff_service import DiffService, RENAME_SIMILARITY

router = APIRouter(prefix="/api", tags=["tools"])
pahole_service = PaholeService()
callgraph_service = CallgraphService()
diff_service = DiffService()


@router.get("/tools/{version_name}/pahole/{struct_name}")
def get_struct_layout(version_name: str, struct_name: str) -> dict[str, Any]:
    """Pahole-style memory layout of a C struct (estimated from its AST)."""
    return pahole_service.get_struct_layout(version_name, struct_name)


@router.get("/tools/{version_name}/callgraph/{function_name}")
def get_callgraph(version_name: str, function_name: str) -> dict[str, Any]:
    """Bidirectional caller and callee callgraph."""
    return callgraph_service.get_function_callgraph(version_name, function_name)


@router.get("/tools/diff/versions")
def get_version_diff(
    version_a: str = Query(...),
    version_b: str = Query(...),
) -> dict[str, Any]:
    """Files added, deleted, modified and renamed between two revisions."""
    return diff_service.get_versions_diff(version_a, version_b)


@router.get("/tools/diff/unchanged")
def get_unchanged_files(
    version_a: str = Query(...),
    version_b: str = Query(...),
) -> dict[str, Any]:
    """Files identical at both revisions (same path, same content)."""
    return diff_service.get_unchanged_files(version_a, version_b)


@router.get("/tools/diff/file")
def get_file_diff(
    version_a: str = Query(...),
    version_b: str = Query(...),
    path: str = Query(...),
    old_path: str = Query(""),
    similarity: int = Query(RENAME_SIMILARITY, ge=1, le=100),
) -> dict[str, Any]:
    """Unified diff of a single file between two releases or commits."""
    return diff_service.get_file_diff(version_a, version_b, path, old_path, similarity)


@router.get("/tools/diff/blobs")
def get_blob_diff(
    version_a: str = Query(...),
    path_a: str = Query(...),
    version_b: str = Query(...),
    path_b: str = Query(...),
) -> dict[str, Any]:
    """Changed lines between any two files at any two revisions (code view "Diff with…")."""
    return diff_service.get_blob_diff(version_a, path_a, version_b, path_b)


@router.get("/tools/diff/renames")
def search_renames(
    version_a: str = Query(...),
    version_b: str = Query(...),
    path: str = Query(""),
    similarity: int = Query(RENAME_SIMILARITY, ge=1, le=100),
) -> dict[str, Any]:
    """Exhaustive rename search between two revisions, optionally under one directory."""
    return diff_service.search_renames(version_a, version_b, path, similarity)


@router.get("/diff/kconfig/{v1}/{v2}")
def get_kconfig_diff(v1: str, v2: str) -> dict[str, Any]:
    """Kconfig symbol definitions added, removed and changed between two revisions."""
    return diff_service.get_kconfig_diff(v1, v2)
