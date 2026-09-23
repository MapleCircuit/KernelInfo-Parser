"""webapp/backend/routers/filesystem.py - Tree, File, Blame & Reference Endpoints."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query
from webapp.backend.services.filesystem_service import FilesystemService
from webapp.backend.services.git_service import GitService

router = APIRouter(prefix="/api", tags=["filesystem"])
fs_service = FilesystemService()
git_service = GitService()


@router.get("/tree")
@router.get("/fs/tree/{version_name}")
def get_tree(version: str = Query("v3.0"), path: str = Query(""), version_name: str | None = None) -> dict[str, Any]:
    """Retrieve directory hierarchy for given version and path."""
    v = version_name or version
    return fs_service.get_tree(v, path)


@router.get("/file")
@router.get("/fs/file/{version_name}")
def get_file(version: str = Query("v3.0"), path: str = Query(...), version_name: str | None = None) -> dict[str, Any]:
    """Retrieve file content, AST tokens, and metadata."""
    v = version_name or version
    return fs_service.get_file(v, path)


@router.get("/version/{version_name}/browse/{path:path}")
def browse_path(version_name: str, path: str = "") -> dict[str, Any]:
    """Browse either directory tree or inspect file content with tokens."""
    return fs_service.browse_path(version_name, path)


@router.get("/version/{version_name}/references/{fid}")
def get_references(version_name: str, fid: int, ref_type: str | None = None) -> dict[str, Any]:
    """Retrieve incoming cross-file references for a file ID."""
    return fs_service.get_file_references(version_name, fid, ref_type)


@router.get("/blame")
def get_blame(version: str = Query("v3.0"), path: str = Query(...)) -> dict[str, Any]:
    """Retrieve Git blame line-by-line annotations."""
    return git_service.get_blame(version, path)


@router.get("/fs/resolve_include")
@router.get("/resolve_include")
def resolve_include(
    version: str = Query("v3.0"),
    header: str = Query(...),
    ast_id: int | None = Query(None),
    current_file: str | None = Query(None),
) -> dict[str, Any]:
    """Resolve an #include header directive into an authoritative git repo file path."""
    return fs_service.resolve_include(version, header, ast_id, current_file)


@router.get("/fs/treemap/{version_name}")
@router.get("/version/{version_name}/treemap")
def get_codebase_treemap(version_name: str = "v3.0", max_depth: int = Query(3), path: str = Query("")) -> dict[str, Any]:
    """Generate hierarchical squarified treemap data structure representing file and directory size distribution."""
    return fs_service.get_codebase_treemap(version_name, max_depth, path)


@router.get("/fs/compile_commands/{version_name}")
@router.get("/version/{version_name}/compile_commands")
def export_compile_commands(version_name: str = "v3.0", arch: str = Query("x86")) -> list[dict[str, str]]:
    """Export Clang compile_commands.json compilation database entries for indexed C files."""
    return fs_service.export_compile_commands(version_name, arch)

