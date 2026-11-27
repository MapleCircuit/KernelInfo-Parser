"""api/routers/insights.py - Stable backport gaps, subsystem health, release CVE reports, and watch change checks."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from api.config import (
    get_active_repo_name,
    paginate_items,
    require_feature,
    resolve_version,
)
from api.routers.common import _with_repo, cve_service
from webapp.backend.routers.insights import job_answer
from webapp.backend.security.git import safe_rev
from webapp.backend.services import insights_service, stable_service, watch_service

router = APIRouter()


class WatchChangesRequest(BaseModel):
    """Stateless read-only watch-list change check between two revisions (`from` and `to`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_rev: str = Field(..., alias="from", min_length=1, max_length=256, description="Base revision (tag, branch, or commit SHA).")
    to_rev: str = Field(..., alias="to", min_length=1, max_length=256, description="Target revision (tag, branch, or commit SHA).")
    items: list[dict[str, Any]] = Field(..., max_length=200, description="Watched items (`file`, `dir`, `symbol`, `kconfig`, `section`).")


@router.get("/insights/stable", dependencies=[Depends(require_feature("insights", "stable"))])
def get_stable_backport_gaps(
    series: Annotated[str, Query(max_length=256)] = "",
    mode: Annotated[str, Query(pattern="^(auto|series|summary|missing)$")] = "auto",
    from_ref: Annotated[str | None, Query(max_length=256)] = None,
    to_ref: Annotated[str | None, Query(max_length=256)] = None,
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """Stable series list (`mode='series'`), stable series summary metrics (`mode='summary'`), or missing backport candidates (`mode='missing'`)."""
    clean_series = series.strip()
    eff_mode = mode
    if eff_mode == "auto":
        if not clean_series and not from_ref and not to_ref:
            eff_mode = "series"
        elif from_ref or to_ref:
            eff_mode = "summary"
        else:
            eff_mode = "missing"

    if eff_mode == "series":
        rows = stable_service.stable_series()
        page, total, truncated = paginate_items(rows, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "series",
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "series": page,
        }

    target = safe_rev(clean_series or resolve_version(version), "series")
    if eff_mode == "summary":
        if from_ref is not None:
            safe_rev(from_ref, "from_ref")
        if to_ref is not None:
            safe_rev(to_ref, "to_ref")
        res = stable_service.stable_summary(target, from_ref=from_ref, to_ref=to_ref)
        return {"repo": get_active_repo_name(), "mode": "summary", **res}

    def _shape_missing(r: dict[str, Any]) -> dict[str, Any]:
        cands = list(r.get("candidates") or [])
        page, total, truncated = paginate_items(cands, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "missing",
            **{k: v for k, v in r.items() if k != "candidates"},
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "candidates": page,
        }

    return _with_repo(job_answer(stable_service.missing_report(target, wait=2.0), _shape_missing))


@router.get("/insights/subsystems", dependencies=[Depends(require_feature("insights", "maintainers"))])
def get_subsystem_health(
    version: Annotated[str | None, Query(max_length=256)] = None,
    window: Annotated[int, Query(ge=1, le=12)] = 4,
    sec_id: Annotated[int | None, Query(ge=1)] = None,
    q: Annotated[str, Query(max_length=256)] = "",
    flag: Annotated[str, Query(max_length=64)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """MAINTAINERS subsystem health over the last `window` releases: commits, authors, review coverage, bus factor, and risk flags."""
    ver = resolve_version(version)
    needle = q.strip().lower()
    flag_clean = flag.strip()

    def _shape(r: dict[str, Any]) -> dict[str, Any]:
        sections = list(r.get("sections") or [])
        if sec_id is not None:
            sec = next((s for s in sections if s.get("sec_id") == sec_id), None)
            if sec is None:
                raise HTTPException(status_code=404, detail=f"Section {sec_id} is not in the MAINTAINERS of {ver}")
            return {
                "repo": get_active_repo_name(),
                "mode": "section",
                "release": r.get("release", ver),
                "window": r.get("window"),
                "section": sec,
            }
        if needle:
            sections = [s for s in sections if needle in str(s.get("name") or "").lower()]
        if flag_clean:
            sections = [s for s in sections if flag_clean in (s.get("flags") or [])]
        page, total, truncated = paginate_items(sections, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "list",
            **{k: v for k, v in r.items() if k != "sections"},
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "sections": page,
        }

    return _with_repo(job_answer(insights_service.subsystem_health(ver, window, wait=2.0), _shape))


@router.get("/insights/cves", dependencies=[Depends(require_feature("insights", "cve"))])
def get_release_cve_report(
    version: Annotated[str | None, Query(max_length=256)] = None,
    state: Annotated[str, Query(pattern="^(|affected|possibly|fixed)$")] = "",
    cvss_op: Annotated[str, Query(max_length=16)] = "",
    cvss_score: Annotated[float | None, Query(ge=0.0, le=10.0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """CVEs affecting, possibly affecting, or fixed in `version`, with optional `state` and CVSS threshold filter."""
    ver = resolve_version(version)
    norm_op = cve_service.normalize_cvss_op(cvss_op)
    norm_score = cve_service.clamp_cvss_score(cvss_score)

    def _shape(r: dict[str, Any]) -> dict[str, Any]:
        if r.get("ingested") is False:
            return {
                "repo": get_active_repo_name(),
                **{k: v for k, v in r.items() if k != "cves"},
                "configured": r.get("configured", True),
                "total": 0,
                "limit": limit,
                "offset": offset,
                "truncated": False,
                "cves": [],
            }
        cvss_rows, counts, cvss_filter = cve_service.filter_report_by_cvss(r.get("cves", []), norm_op, norm_score)
        rows = [c for c in cvss_rows if not state or c.get("state") == state]
        page, total, truncated = paginate_items(rows, limit, offset)
        out: dict[str, Any] = {
            "repo": get_active_repo_name(),
            **{k: v for k, v in r.items() if k != "cves"},
            "counts": counts,
            "configured": r.get("configured", True),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "cves": page,
        }
        if cvss_filter is not None:
            out["cvss_filter"] = cvss_filter
        return out

    return _with_repo(job_answer(cve_service.report(ver, wait=2.0), _shape))


@router.post("/watch/changes")
def check_watched_changes(payload: WatchChangesRequest) -> dict[str, Any]:
    """Stateless read-only differential check between `from` and `to` revisions for watched files, dirs, symbols, Kconfig, or sections."""
    safe_rev(payload.from_rev, "from")
    safe_rev(payload.to_rev, "to")
    if len(payload.items) > 200:
        raise HTTPException(status_code=400, detail="items must be a list of at most 200 watches")
    res = watch_service.check_changes(
        {"from": payload.from_rev, "to": payload.to_rev, "items": payload.items}
    )
    return {"repo": get_active_repo_name(), **res}
