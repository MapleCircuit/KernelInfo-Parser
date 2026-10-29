"""webapp/backend/routers/mail.py - Mailing list & LKML thread endpoints."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Query

from webapp.backend.services.mailing_list_service import mailing_list_service

router = APIRouter(prefix="/api/mail", tags=["mail"])


@router.get("/status")
def get_mail_status() -> dict[str, Any]:
    """Retrieve mailing list daemon status and archive shard metrics."""
    return mailing_list_service.get_status()


@router.get("/message")
def get_mail_message(
    mid: str = Query("", description="RFC822 Message-ID (or article num)"),
    num: int | None = Query(None, description="Optional article sequential number"),
) -> dict[str, Any]:
    """Retrieve full email details including headers, body, patch diffs, and raw RFC822 payload."""
    clean_num = num if isinstance(num, int) else None
    clean_mid = mid if isinstance(mid, str) else ""
    target = clean_num if clean_num is not None and clean_num > 0 else clean_mid
    return mailing_list_service.get_message(target)


@router.get("/thread/{tid}")
@router.get("/thread")
def get_mail_thread(
    tid: int = 0,
    mid: str = Query("", description="Optional starting or reference Message-ID"),
) -> dict[str, Any]:
    """Retrieve all messages in a thread ordered chronologically."""
    clean_tid = tid if isinstance(tid, int) else 0
    clean_mid = mid if isinstance(mid, str) else ""
    return mailing_list_service.get_thread(clean_tid, mid_hint=clean_mid)


@router.get("/versions/{tid}")
@router.get("/versions")
def get_mail_thread_versions(
    tid: int = 0,
    mid: str = Query("", description="Optional Message-ID identifying the thread"),
) -> dict[str, Any]:
    """List every revision (v1, v2, ...) of the patch series a thread belongs to."""
    clean_tid = tid if isinstance(tid, int) else 0
    clean_mid = mid if isinstance(mid, str) else ""
    return mailing_list_service.get_thread_versions(clean_tid, mid_hint=clean_mid)


@router.get("/search")
def search_mailing_list(
    q: str = Query("", description="Search keywords, boolean prefixes (s:, f:, m:), or terms"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(50, ge=1, le=100, description="Results per page"),
    filter_type: str = Query("all", alias="filter", description="Filter: all, patch, cover, pull"),
) -> dict[str, Any]:
    """Search mailing list messages across subjects, authors, and patch metadata."""
    clean_q = q if isinstance(q, str) else ""
    clean_page = page if isinstance(page, int) else 1
    clean_limit = limit if isinstance(limit, int) else 50
    clean_filter = filter_type if isinstance(filter_type, str) else "all"
    return mailing_list_service.search(query=clean_q, page=clean_page, limit=clean_limit, filter_type=clean_filter)
