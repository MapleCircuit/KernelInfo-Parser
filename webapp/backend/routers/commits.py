"""webapp/backend/routers/commits.py - Commit History & Git Inspection."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query, Body
from webapp.backend.services.git_service import GitService

router = APIRouter(prefix="/api", tags=["commits"])
git_service = GitService()


@router.get("/commits")
@router.get("/commits/{version_name}/list")
def get_commits(
    version: str = Query("v3.0"),
    page: int = Query(1),
    limit: int = Query(50),
    q: str = Query(""),
    version_name: str | None = None,
) -> dict[str, Any]:
    """Retrieve paginated commit history."""
    v = version_name or version
    return git_service.get_commits(v, page, limit, q)


@router.get("/commit")
@router.get("/commits/{version_name}/detail/{commit_id_or_hash}")
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


@router.get("/commits/{version_name}/blame/{path:path}")
def get_blame_scoped(version_name: str, path: str) -> dict[str, Any]:
    """Retrieve Git blame line-by-line annotations."""
    return git_service.get_file_blame(version_name, path)


@router.get("/commits/{version_name}/timeline")
def get_commit_timeline(version_name: str = "v3.0", limit: int = Query(100)) -> dict[str, Any]:
    """Retrieve chronological commit timeline with contributor summaries."""
    return git_service.get_commit_timeline(version_name, limit)


@router.post("/commits/{version_name}/format_patch")
@router.post("/commits/format_patch")
def format_patch(payload: dict[str, Any] = Body(...), version_name: str = "v3.0") -> dict[str, Any]:
    """Generate RFC-2822 standard email formatted patch from in-browser edits."""
    return git_service.format_patch(version_name, payload)
