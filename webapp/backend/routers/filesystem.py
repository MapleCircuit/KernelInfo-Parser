"""webapp/backend/routers/filesystem.py - Tree, File, Blame & Reference Endpoints."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query
from fastapi.responses import Response
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


@router.get("/blame")
def get_blame(version: str = Query("v3.0"), path: str = Query(...)) -> dict[str, Any]:
    """Retrieve Git blame line-by-line annotations."""
    return git_service.get_blame(version, path)


@router.get("/version/{version_name}/file/{fid}/blame")
def get_file_blame_by_fid(version_name: str, fid: int | str) -> dict[str, Any]:
    """Retrieve Git blame and tag-level annotations for file ID."""
    return git_service.get_file_blame(version_name, fid)


@router.get("/version/{version_name}/file/{path:path}/history")
def get_file_history_by_path(version_name: str, path: str, limit: int = Query(50)) -> dict[str, Any]:
    """Retrieve Git commit history and last changed metadata for a file path."""
    return git_service.get_file_history(version_name, path, limit)



@router.get("/fs/resolve_include")
@router.get("/resolve_include")
def resolve_include(
    version: str = Query("v3.0"),
    header: str = Query(...),
    ast_id: int | None = Query(None),
    current_file: str | None = Query(None),
    arch: str | None = Query(None, description="ARCH whose build layout resolves the include (default x86_64)"),
) -> dict[str, Any]:
    """Resolve an #include header directive into an authoritative git repo file path."""
    return fs_service.resolve_include(version, header, ast_id, current_file, arch)


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


@router.get("/fs/compile_commands/{version_name}/export")
@router.get("/version/{version_name}/export/compile_commands.json")
def download_compile_commands(version_name: str = "v3.0", arch: str = Query("x86")) -> Response:
    """Download compile_commands.json as an attachment."""
    import json
    cmds = fs_service.export_compile_commands(version_name, arch)
    return Response(
        content=json.dumps(cmds, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="compile_commands-{version_name}-{arch}.json"'},
    )


@router.get("/fs/search/{version_name}")
def search_files(version_name: str, q: str = Query(""), limit: int = Query(50)) -> dict[str, Any]:
    """Fuzzy search of file paths in the revision's tree."""
    return fs_service.search_files(version_name, q, limit)


@router.get("/fs/raw/{version_name}")
def get_raw_file(version_name: str, path: str = Query(...)) -> Response:
    """Serve an image blob (documentation figures). SVG is sandboxed so embedded scripts never run."""
    data, media_type = fs_service.read_raw(version_name, path)
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.get("/version/{version_name}/export/compile_commands")
def export_compile_commands_envelope(version_name: str = "v3.0", arch: str = Query("x86")) -> dict[str, Any]:
    """Export Clang compile_commands.json compilation database wrapped in envelope for UI export handlers."""
    cmds = fs_service.export_compile_commands(version_name, arch)
    return {"compile_commands": cmds, "commands": cmds}

