"""webapp/backend/routers/filesystem.py - Tree, File, Include, Treemap & Search Endpoints."""
from __future__ import annotations
import json
import re
from typing import Annotated, Any
from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse, Response
from webapp.backend.security.git import safe_rev
from webapp.backend.services.filesystem_service import FilesystemService
from webapp.backend.services.grep_service import grep_service
from webapp.backend.services.outline_service import outline_service

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


@router.get("/fs/references/{version_name}")
def get_file_references(
    version_name: str,
    path: str = Query(...),
    limit: Annotated[int, Query(ge=1, le=1000, description="Entries per list")] = 200,
) -> dict[str, Any]:
    """What refers to a file at a revision: includers, Kconfig `source`s, documents, Kbuild rules."""
    return fs_service.get_file_references(version_name, path, limit)


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


@router.get("/fs/grep/{version_name}")
def grep_code(
    version_name: str,
    q: Annotated[str, Query(description="Text or POSIX ERE to search for (1-256 characters, one line)")] = "",
    mode: Annotated[str, Query(pattern="^(fixed|regex)$", description="fixed: literal text (-F); regex: POSIX ERE (-E)")] = "fixed",
    case: Annotated[int, Query(ge=0, le=1, description="1: match case")] = 0,
    word: Annotated[int, Query(ge=0, le=1, description="1: whole words only")] = 0,
    path: Annotated[list[str] | None, Query(description="Path filters: 'drivers/net/', '*.c', '!*.h' (at most 8)")] = None,
    limit: Annotated[int, Query(ge=1, le=20000, description="Matches returned overall")] = 2000,
    per_file: Annotated[int, Query(ge=1, le=5000, description="Matches returned per file")] = 50,
) -> Any:
    """Full-text search of a revision's files (git grep over its commit, webapp/API.md section 2.1.3, 4.2)."""
    safe_rev(version_name)
    result = grep_service.grep(version_name, q, mode=mode, ignore_case=not case, word=bool(word),
                               paths=path or [], max_matches=limit, max_per_file=per_file)
    body = {"version": version_name, "query": q, "mode": mode, "case": bool(case), "word": bool(word),
            "paths": path or [], **{k: v for k, v in result.items() if k != "cached"}}
    if result.get("timed_out"):
        # A partial answer must never be stored offline (Service Worker, IndexedDB)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})
    return body



@router.get("/fs/outline/{version_name}")
def get_outline(
    version_name: str,
    path: Annotated[str, Query(description="Repository-relative file path")] = "",
) -> dict[str, Any]:
    """Definitions of one file (functions, types, macros, variables, kernel API entries, Kconfig entries,
    Rust items) sorted by line and nested by containment, for the code view's outline and sticky scroll."""
    return outline_service.get_outline(version_name, path)


@router.post("/version/{version_name}/treemap/build")
def post_build_treemap(version_name: str, payload: dict[str, Any] = Body(...)) -> Any:
    """The treemap of a subtree with what a configuration compiles (K4): per node bytes and files built
    in, as modules, not built and unknown. Body {arch, assignments: {SYM: value}, path, max_depth};
    202 while the Kbuild index builds."""
    from fastapi import HTTPException
    from webapp.backend.routers.insights import job_answer
    from webapp.backend.services.kbuild_index_service import build_treemap, validate_assignments
    safe_rev(version_name)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    path = str(payload.get("path") or "")
    from webapp.backend.security.jail import is_safe_rel_path
    if path and not is_safe_rel_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")
    assignments = validate_assignments(payload.get("assignments") or {})
    return job_answer(build_treemap(version_name, str(payload.get("arch") or "x86_64"), assignments, path,
                                    int(payload.get("max_depth") or 2)))
