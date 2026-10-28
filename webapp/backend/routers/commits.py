"""webapp/backend/routers/commits.py - Commit History & Git Inspection."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query, Body, HTTPException
from webapp.backend.services.git_service import GitService

router = APIRouter(prefix="/api", tags=["commits"])
git_service = GitService()



@router.get("/commits")
@router.get("/commits/{version_name}/list")
def get_commits(
    version: str = Query("v3.0"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    q: str = Query(""),
    version_name: str | None = None,
) -> dict[str, Any]:
    """Retrieve paginated commit history."""
    v = version_name or version
    return git_service.get_commits(v, page, limit, q)


@router.get("/commit")
@router.get("/commits/{version_name}/detail/{commit_id_or_hash}")
@router.get("/version/{version_name}/commit/{commit_id_or_hash}")
def get_commit(
    version: str = Query("v3.0"),
    commit_id: str = Query(None),
    version_name: str | None = None,
    commit_id_or_hash: str | None = None,
) -> dict[str, Any]:
    """Retrieve commit details, files, and multi-contributor trailers."""
    v = version_name or version
    cid = commit_id_or_hash or commit_id
    return git_service.get_commit_detail(v, cid)


@router.post("/commits/{version_name}/resolve_mail/{commit_id_or_hash}")
@router.post("/commits/resolve_mail")
def resolve_commit_mail(
    version_name: str = "v3.0",
    commit_id_or_hash: str = "",
    version: str = Query("v3.0"),
    commit_id: str = Query(""),
) -> dict[str, Any]:
    """On-demand resolution of commit against mailing list archive."""
    v = version_name or version
    cid = commit_id_or_hash or commit_id
    return git_service.resolve_commit_mail(v, cid)


@router.get("/commits/{version_name}/diff/{commit_hash}")
def get_commit_diff(version_name: str, commit_hash: str, context: int = Query(3, ge=0, le=20)) -> dict[str, Any]:
    """Structured diff of one commit against its first parent, with old/new line numbers per row."""
    return git_service.get_commit_diff(commit_hash, context)


@router.get("/commits/{version_name}/blame/{path:path}")
@router.get("/commits/{version_name}/blame")
def get_blame_scoped(
    version_name: str,
    path: str | None = None,
    query_path: str | None = Query(None, alias="path"),
) -> dict[str, Any]:
    """Retrieve Git blame line-by-line annotations."""
    target_path = path or query_path
    if not target_path:
        raise HTTPException(status_code=400, detail="File path must be specified.")
    return git_service.get_blame(version_name, target_path)


@router.get("/commits/{version_name}/history/{path:path}")
@router.get("/commits/{version_name}/history")
def get_file_history_scoped(
    version_name: str,
    path: str | None = None,
    query_path: str | None = Query(None, alias="path"),
    limit: int = Query(50),
) -> dict[str, Any]:
    """Retrieve Git commit history and last changed metadata for a specific file."""
    target_path = path or query_path
    if not target_path:
        raise HTTPException(status_code=400, detail="File path must be specified.")
    return git_service.get_file_history(version_name, target_path, limit)



@router.get("/commits/{version_name}/timeline")
@router.get("/commits/timeline")
def get_commit_timeline(
    version_name: str = "v3.0",
    limit: int = Query(100),
    direction: str = Query("past"),
    cursor_id: int | None = Query(None),
) -> dict[str, Any]:
    """Retrieve chronological commit timeline with contributor summaries starting at version anchor."""
    return git_service.get_commit_timeline(version_name, limit=limit, direction=direction, cursor_id=cursor_id)


@router.get("/commits/{version_name}/search")
@router.get("/commits/search")
def search_commits(
    q: str = Query(...),
    version_name: str = "v3.0",
    limit: int = Query(25, ge=1, le=500),
) -> dict[str, Any]:
    """Fast commit search by hash or message for commit-level exploration."""
    return git_service.get_commits(version_name, page=1, limit=limit, query=q)


@router.post("/commits/{version_name}/format_patch")
@router.post("/commits/format_patch")
@router.post("/version/{version_name}/patch/format")
@router.post("/patch/format")
def format_patch(payload: dict[str, Any] = Body(...), version_name: str = "v3.0") -> dict[str, Any]:
    """Generate RFC-2822 standard email formatted patch from in-browser edits."""
    return git_service.format_patch(version_name, payload)

