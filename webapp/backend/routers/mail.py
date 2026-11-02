"""webapp/backend/routers/mail.py - Mailing list & LKML thread endpoints."""
from __future__ import annotations

from typing import Annotated, Any
from fastapi import APIRouter, Query

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
