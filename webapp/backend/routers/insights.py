"""webapp/backend/routers/insights.py - Insights reports (webapp/API.md section 4.10): stable backports,
release statistics, merge windows, subsystem health and security (CVE)."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from webapp.backend.security.git import safe_rev
from webapp.backend.services.jobs import retry_after_ms

router = APIRouter(prefix="/api/insights", tags=["insights"])
NO_STORE = {"Cache-Control": "no-store"}


def job_answer(state: dict[str, Any], shape=None) -> Any:
    """200 with the result, 202 while building (section 2.15), 500 when the job failed."""
    if state.get("state") == "done":
        result = state.get("result")
        return shape(result) if shape else result
    if state.get("state") == "failed":
        raise HTTPException(status_code=500, detail=f"background job failed: {state.get('error') or 'unknown error'}")
    body = {"state": "building", "progress": state.get("progress") or {}, "retry_after_ms": retry_after_ms(state)}
    return JSONResponse(body, status_code=202, headers=NO_STORE)


@router.get("/{version_name}/stable/series")
def get_stable_series(version_name: str) -> dict[str, Any]:
    """Releases that have stable tags (H2): [{series, stable_tags, last}], newest first."""
    from webapp.backend.services.stable_service import stable_series
    return {"series": stable_series()}


@router.get("/{version_name}/stable/missing")
def get_stable_missing(
    version_name: str,
    series: Annotated[str, Query(description="A release with stable tags, e.g. v3.0")],
    limit: Annotated[int, Query(ge=1, le=20000)] = 5000,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """Mainline commits marked for stable (Cc: stable covering the series) or fixing a commit the series
    contains, that no stable tag of the series carries (H2). A background job (202 while building)."""
    from webapp.backend.services.stable_service import missing_report
    safe_rev(series, "series")

    def shape(r: dict[str, Any]) -> dict[str, Any]:
        cands = r.get("candidates", [])
        return {**{k: v for k, v in r.items() if k != "candidates"}, "total": len(cands), "offset": offset,
                "candidates": cands[offset:offset + limit]}

    return job_answer(missing_report(series, wait=2.0), shape)


@router.get("/{version_name}/release-stats")
def get_release_stats(version_name: str) -> Any:
    """Statistics of a release (H6): commits, merges, authors, first-timers, lines, days, -rc count, top
    authors / reviewers / testers / reporters / ackers / sign-offs, employers. The version is the release."""
    from webapp.backend.services.insights_service import release_stats
    safe_rev(version_name)
    return job_answer(release_stats(version_name, wait=2.0))


@router.get("/release-stats/series")
def get_release_stats_series(
    tags: Annotated[list[str] | None, Query(alias="tag", description="Releases (repeatable, at most 40)")] = None,
) -> dict[str, Any]:
    """Trend data of several releases from cached statistics only; missing ones are queued (pending)."""
    from webapp.backend.services.insights_service import release_stats_series
    for t in tags or []:
        safe_rev(t)
    return {"series": release_stats_series(tags or [])}


@router.get("/{version_name}/merge-window")
def get_merge_window(version_name: str) -> Any:
    """The pulls merged for a release (H5): per first-parent merge its source tree, ref, sender, commit
    count, top directories, MAINTAINERS section, [GIT PULL] thread and merge-window day; commits per day."""
    from webapp.backend.services.insights_service import merge_window
    safe_rev(version_name)
    return job_answer(merge_window(version_name, wait=2.0))


@router.get("/{version_name}/subsystems")
def get_subsystem_health(
    version_name: str,
    window: Annotated[int, Query(ge=1, le=12, description="Releases up to this one")] = 4,
) -> Any:
    """Health of every MAINTAINERS section over the last releases (H7): commits, authors, review coverage,
    bus factor, maintainer activity, flags."""
    from webapp.backend.services.insights_service import subsystem_health
    safe_rev(version_name)
    return job_answer(subsystem_health(version_name, window, wait=2.0))


@router.get("/{version_name}/subsystems/{sec_id}")
def get_subsystem_health_one(version_name: str, sec_id: int, window: Annotated[int, Query(ge=1, le=12)] = 4) -> Any:
    """One section of the health report (the Maintainers view's Health panel)."""
    from webapp.backend.services.insights_service import subsystem_health
    safe_rev(version_name)

    def pick(r: dict[str, Any]) -> dict[str, Any]:
        sec = next((s for s in r["sections"] if s["sec_id"] == sec_id), None)
        if sec is None:
            raise HTTPException(status_code=404, detail=f"Section {sec_id} is not in the MAINTAINERS of {version_name}")
        return {"release": r["release"], "window": r["window"], "section": sec}

    return job_answer(subsystem_health(version_name, window, wait=2.0), pick)


@router.get("/{version_name}/cves")
def get_release_cves(
    version_name: str,
    state: Annotated[str, Query(pattern="^(|affected|possibly|fixed)$")] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50000)] = 50000,
) -> Any:
    """CVEs affecting a release, possibly affecting it, or fixed in it (H4). {configured: false} without a CVE clone."""
    from webapp.backend.services.cve_service import report
    safe_rev(version_name)

    def shape(r: dict[str, Any]) -> dict[str, Any]:
        rows = [c for c in r.get("cves", []) if not state or c["state"] == state]
        return {**{k: v for k, v in r.items() if k != "cves"}, "configured": r.get("configured", True), "total": len(rows),
                "cves": rows[offset:offset + limit]}

    return job_answer(report(version_name, wait=2.0), shape)
