"""webapp/backend/routers/mail.py - Mailing list & LKML thread endpoints."""
from __future__ import annotations

from typing import Annotated, Any
from fastapi import APIRouter, Body, Query

from webapp.backend.services.mailing_list_service import mailing_list_service

router = APIRouter(prefix="/api/mail", tags=["mail"])


@router.get("/status")
def get_mail_status() -> dict[str, Any]:
    """Mailing list daemon status and archive shard metrics."""
    return mailing_list_service.get_status()


@router.get("/message")
def get_mail_message(
    mid: Annotated[str, Query(description="RFC822 Message-ID (or article num)")] = "",
) -> dict[str, Any]:
    """Full email: headers, body, patch diffs and the raw RFC822 payload."""
    return mailing_list_service.get_message(mid)


@router.get("/thread")
def get_mail_thread(
    tid: Annotated[int, Query(description="Thread id (0: resolve it from mid)")] = 0,
    mid: Annotated[str, Query(description="Optional starting or reference Message-ID")] = "",
) -> dict[str, Any]:
    """Messages of a thread in chronological order."""
    return mailing_list_service.get_thread(tid, mid_hint=mid)


@router.get("/versions")
def get_mail_thread_versions(
    tid: Annotated[int, Query(description="Thread id (0: resolve it from mid)")] = 0,
    mid: Annotated[str, Query(description="Optional Message-ID identifying the thread")] = "",
) -> dict[str, Any]:
    """Every revision (v1, v2, ...) of the patch series a thread belongs to."""
    return mailing_list_service.get_thread_versions(tid, mid_hint=mid)


@router.get("/search")
def search_mailing_list(
    q: Annotated[str, Query(description="Search keywords, boolean prefixes (s:, f:, m:), or terms")] = "",
    page: Annotated[int, Query(ge=1, description="Page number (1-indexed)")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page")] = 50,
    filter_type: Annotated[str, Query(alias="filter", description="Filter: all, patch, cover, pull")] = "all",
) -> dict[str, Any]:
    """Search messages across subjects, authors and patch metadata."""
    return mailing_list_service.search(query=q, page=page, limit=limit, filter_type=filter_type)


@router.get("/merged")
def get_merged_commits(
    mid: Annotated[str, Query(description="Message-ID of a patch or cover letter")] = "",
    tid: Annotated[int, Query(description="Thread id: the whole series")] = 0,
) -> dict[str, Any]:
    """Commits a patch or a series became, with their first release (P2): from the parser's commit-mail
    links, else from Link:/Message-Id: trailers ({source: "db" | "trailers"})."""
    from webapp.backend.services.merged_service import merged_commits
    return merged_commits(mid, tid)


@router.post("/apply")
def post_mail_apply(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Apply patches from the archive to a revision in memory (P1): {revision, message_ids: [...]} (in series
    order, at most 50 messages and 5 MB) -> {apply_id, base, base_commit, patches: [{message_id, subject, status,
    files: [{path, status, hunks}]}], summary}. Nothing is written to the repository or the database."""
    from fastapi import HTTPException
    from webapp.backend.services.patch_apply import patch_apply_service
    if not isinstance(payload, dict) or not isinstance(payload.get("message_ids"), list):
        raise HTTPException(status_code=400, detail='Body must be {"revision": "...", "message_ids": ["..."]}')
    return patch_apply_service.apply(str(payload.get("revision") or "HEAD"), payload["message_ids"])


@router.get("/apply/{apply_id}/file")
def get_mail_apply_file(apply_id: str, path: Annotated[str, Query()] = "") -> dict[str, Any]:
    """Changed lines of one patched file against its base (the blob-diff hunk shape); 410 once evicted."""
    from webapp.backend.services.patch_apply import patch_apply_service
    return patch_apply_service.file(apply_id, path.strip("/"))
