"""webapp/backend/routers/filesystem.py - Tree, File, Include, Treemap & Search Endpoints."""
from __future__ import annotations
import json
import re
from typing import Any
from fastapi import APIRouter, Query
from fastapi.responses import Response
from webapp.backend.services.filesystem_service import FilesystemService

router = APIRouter(prefix="/api", tags=["filesystem"])
fs_service = FilesystemService()


@router.get("/fs/tree/{version_name}")
def get_tree(version_name: str, path: str = Query("")) -> dict[str, Any]:
    """Direct children of a directory at a revision."""
    return fs_service.get_tree(version_name, path)


@router.get("/fs/file/{version_name}")
def get_file(version_name: str, path: str = Query(...)) -> dict[str, Any]:
    """File content and AST tokens (a directory path answers with its listing)."""
    return fs_service.get_file(version_name, path)


@router.get("/fs/resolve_include")
def resolve_include(
    version: str = Query("v3.0"),
    header: str = Query(...),
    ast_id: int | None = Query(None),
    current_file: str | None = Query(None),
    arch: str | None = Query(None, description="ARCH whose build layout resolves the include (default x86_64)"),
) -> dict[str, Any]:
    """Resolve an #include header directive into a repository file path."""
    return fs_service.resolve_include(version, header, ast_id, current_file, arch)


@router.get("/version/{version_name}/treemap")
def get_codebase_treemap(version_name: str, max_depth: int = Query(3), path: str = Query("")) -> dict[str, Any]:
    """Directory hierarchy weighted by bytes and file counts (squarified treemap)."""
    return fs_service.get_codebase_treemap(version_name, max_depth, path)


@router.get("/fs/compile_commands/{version_name}/export")
def download_compile_commands(version_name: str, arch: str = Query("x86")) -> Response:
    """Download a clang compile_commands.json for the revision's C sources."""
    cmds = fs_service.export_compile_commands(version_name, arch)
    name = f"compile_commands-{_filename_part(version_name)}-{_filename_part(arch)}.json"
    return Response(
        content=json.dumps(cmds, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _filename_part(value: str) -> str:
    """A request value placed in a Content-Disposition header, kept to file-name characters."""
    return re.sub(r"[^A-Za-z0-9._+-]", "_", value)[:100]


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
