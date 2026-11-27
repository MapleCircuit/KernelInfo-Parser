"""api/routers/mail.py - Mailing list / LKML search, message detail, thread, and merged commit endpoints."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.config import (
    get_active_repo_name,
    paginate_items,
    require_feature,
)
from webapp.backend.services.mailing_list_service import mailing_list_service
from webapp.backend.services.merged_service import merged_commits

router = APIRouter()


@router.get("/mail/search", dependencies=[Depends(require_feature("mail"))])
def search_mail(
    q: Annotated[str, Query(max_length=256)] = "",
    filter: Annotated[str, Query(pattern="^(all|patch|cover|pull)$")] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    page: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """Search LKML archive messages across subjects, authors, and patch metadata."""
    eff_page = page if page is not None else ((offset // limit) + 1)
    eff_offset = (eff_page - 1) * limit if page is not None else offset
    rel_off = eff_offset - (eff_page - 1) * limit
    res = mailing_list_service.search(query=q, page=eff_page, limit=limit, filter_type=filter)
    raw_results = list(res.get("results") if "results" in res else (res.get("messages") or []))
    sliced = raw_results[rel_off : rel_off + limit] if rel_off > 0 else raw_results[:limit]
    total = int(res["total"]) if res.get("total") is not None else len(raw_results)
    truncated = bool(res.get("has_more") or (eff_offset + len(sliced) < total))
    return {
        "repo": get_active_repo_name(),
        **res,
        "query": q,
        "filter": filter,
        "page": eff_page,
        "total": total,
        "limit": limit,
        "offset": eff_offset,
        "truncated": truncated,
        "results": sliced,
    }


@router.get("/mail/message", dependencies=[Depends(require_feature("mail"))])
def get_mail_message(
    mid: Annotated[str, Query(min_length=1, max_length=512)],
) -> dict[str, Any]:
    """Full LKML email headers, body, patch diffs, and RFC822 payload by `mid` (Message-ID or article number)."""
    res = mailing_list_service.get_message(mid.strip())
    return {"repo": get_active_repo_name(), **res}


@router.get("/mail/thread", dependencies=[Depends(require_feature("mail"))])
def get_mail_thread(
    tid: Annotated[int, Query(ge=0)] = 0,
    mid: Annotated[str, Query(max_length=512)] = "",
    include_versions: bool = False,
    versions_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Messages of an LKML thread in chronological order, with optional patch-series version list (`include_versions`)."""
    clean_mid = mid.strip()
    if not tid and not clean_mid:
        raise HTTPException(status_code=400, detail="Provide `tid` or `mid`.")
    if versions_only:
        res = mailing_list_service.get_thread_versions(tid, mid_hint=clean_mid)
        return {"repo": get_active_repo_name(), "mode": "versions", **res}

    res = mailing_list_service.get_thread(tid, mid_hint=clean_mid)
    msgs = list(res.get("messages") or [])
    page, total, truncated = paginate_items(msgs, limit, offset)
    out: dict[str, Any] = {
        "repo": get_active_repo_name(),
        **res,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "messages": page,
    }
    if include_versions:
        resolved_tid = int(res.get("thread_id") or tid or 0)
        out["versions"] = mailing_list_service.get_thread_versions(resolved_tid, mid_hint=clean_mid)
    return out


@router.get("/mail/merged", dependencies=[Depends(require_feature("mail"))])
def get_mail_merged_commits(
    mid: Annotated[str, Query(max_length=512)] = "",
    tid: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Commits a patch (`mid`) or patch series (`tid`) became in mainline, with their first release."""
    clean_mid = mid.strip()
    if not tid and not clean_mid:
        raise HTTPException(status_code=400, detail="Provide `mid` or `tid`.")
    res = merged_commits(clean_mid, tid)
    return {"repo": get_active_repo_name(), **res}
