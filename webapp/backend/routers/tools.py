"""webapp/backend/routers/tools.py - Pahole Layout, Function Callgraph & Revision Compare."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Body, Query
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


@router.post("/tools/{version_name}/checkpatch")
def post_checkpatch(version_name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Run scripts/checkpatch.pl of the configured release tag on a patch (P4; off by default: {enabled: false})."""
    from webapp.backend.services.checkpatch_service import check_patch
    patch = payload.get("patch") if isinstance(payload, dict) else None
    return check_patch(patch if isinstance(patch, str) else "")


@router.get("/tools/{version_name}/implementations")
def get_implementations(
    version_name: str,
    struct: str = Query(""),
    field: str = Query(""),
    function: str = Query(""),
) -> Any:
    """Ops implementations (N1): `struct` (+ `field`) lists the functions designated initializers assign to its
    fields; `function` lists the ops tables that use a function. From the revision's source index (202 with progress
    while it is assembled)."""
    from fastapi import HTTPException
    from webapp.backend.routers.insights import job_answer
    from webapp.backend.services.ops_service import index_state, uses_state
    if function:
        return job_answer(uses_state(version_name, function, wait=3.0), lambda r: r)
    if not struct:
        raise HTTPException(status_code=400, detail="Give struct (and field) or function")

    def shape(r: dict[str, Any]) -> dict[str, Any]:
        out = {"version": version_name, "struct": r["struct"], "tables": r["tables"], "files": r["files"],
               "positional_skipped": r["positional_skipped"],
               "fields": [{"field": k, "count": len(v)} for k, v in r["fields"].items()]}
        if field:
            out.update(field=field, implementations=r["fields"].get(field, []))
        return out

    return job_answer(index_state(version_name, struct, wait=3.0), shape)


@router.get("/tools/{version_name}/callpath")
def get_callpath(
    version_name: str,
    src: str = Query("", alias="from"),
    dst: str = Query("", alias="to"),
    max_depth: int = Query(6, ge=1, le=10),
    k: int = Query(5, ge=1, le=20),
    exclude: str = Query("", description="Comma-separated functions not to go through (hubs such as printk)"),
    indirect: bool = Query(False, description="Also follow calls through struct members to the functions ops tables assign"),
    arch: str = Query("", max_length=32, description="ARCH whose tree counts (default: the version's default ARCH)"),
) -> Any:
    """Shortest call chains from one function to another (N2), over the revision's source index (202 with progress
    while it is assembled)."""
    from webapp.backend.routers.insights import job_answer
    from webapp.backend.services.callpath_service import callpath_state
    names = tuple(n.strip() for n in exclude.split(",") if n.strip())[:50]
    return job_answer(callpath_state(version_name, src.strip(), dst.strip(), max_depth=max_depth, k=k, exclude=names,
                                     indirect=indirect, arch=arch.strip()))


@router.get("/tools/pahole/compare")
def get_pahole_compare(a: str = Query(...), b: str = Query(...), struct: str = Query(...)) -> dict[str, Any]:
    """A struct's layout at two revisions, member by member (N3)."""
    from webapp.backend.services.pahole_compare import compare
    return compare(a, b, struct)


@router.get("/tools/{version_name}/pahole/{struct_name}/history")
def get_pahole_history(version_name: str, struct_name: str, include_rc: bool = Query(False)) -> Any:
    """Size, holes and cachelines of a struct at each release where its definition changed (N3); 202 while it runs."""
    from webapp.backend.routers.insights import job_answer
    from webapp.backend.services.pahole_compare import history_state
    return job_answer(history_state(version_name, struct_name, include_rc))
