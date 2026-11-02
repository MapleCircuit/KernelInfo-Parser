"""webapp/backend/routers/commits.py - Commit History & Git Inspection."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query, Body
from webapp.backend.services.git_service import GitService

router = APIRouter(prefix="/api", tags=["commits"])
git_service = GitService()


@router.get("/commits/{version_name}/list")
def get_commits(
    version_name: str,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    q: str = Query(""),
) -> dict[str, Any]:
    """Indexed commits of the repository matching `q`, newest first."""
    return git_service.get_commits(version_name, page, limit, q)


@router.get("/commits/{version_name}/detail/{commit_id_or_hash}")
def get_commit(version_name: str, commit_id_or_hash: str) -> dict[str, Any]:
    """Commit details, files and multi-contributor trailers."""
    return git_service.get_commit_detail(commit_id_or_hash)


@router.post("/commits/{version_name}/resolve_mail/{commit_id_or_hash}")
def resolve_commit_mail(version_name: str, commit_id_or_hash: str) -> dict[str, Any]:
    """On-demand resolution of a commit against the mailing list archive."""
    return git_service.resolve_commit_mail(commit_id_or_hash)


@router.get("/commits/{version_name}/diff/{commit_hash}")
def get_commit_diff(version_name: str, commit_hash: str, context: int = Query(3, ge=0, le=20)) -> dict[str, Any]:
    """Structured diff of one commit against its first parent, with old/new line numbers per row."""
    return git_service.get_commit_diff(commit_hash, context)


@router.get("/commits/{version_name}/blame/{path:path}")
def get_blame(version_name: str, path: str) -> dict[str, Any]:
    """Line-by-line `git blame` annotations."""
    return git_service.get_blame(version_name, path)


@router.get("/commits/{version_name}/history/{path:path}")
def get_file_history(version_name: str, path: str, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    """Commits that changed a file, newest first, and its last change."""
    return git_service.get_file_history(version_name, path, limit)


@router.get("/commits/{version_name}/timeline")
def get_commit_timeline(
    version_name: str,
    limit: int = Query(100, ge=1, le=250),
    direction: str = Query("past"),
    cursor_id: int | None = Query(None),
) -> dict[str, Any]:
    """Commit timeline around the version's commit, with contributor summaries."""
    return git_service.get_commit_timeline(version_name, limit=limit, direction=direction, cursor_id=cursor_id)


@router.get("/commits/{version_name}/search")
def search_commits(version_name: str, q: str = Query(...), limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """Commit search by hash prefix, subject or author (at least two characters)."""
    if len(q.strip()) < 2:
        return {"version": version_name, "page": 1, "limit": limit, "total": 0, "commits": []}
    return git_service.get_commits(version_name, page=1, limit=limit, query=q)


@router.post("/version/{version_name}/patch/format")
def format_patch(version_name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """RFC-2822 email patch from in-browser edits."""
    return git_service.format_patch(version_name, payload)
