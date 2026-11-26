"""webapp/backend/routers/cve.py - Linux kernel CVE records (H4, webapp/API.md section 4.11)."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from webapp.backend.config import require_feature
from webapp.backend.services import cve_service

router = APIRouter(prefix="/webapi/cve", tags=["cve"], dependencies=[Depends(require_feature("cve"))])


@router.get("/status")
def get_cve_status() -> dict[str, Any]:
    """{configured, ingested, name, layout, head, records, built_at, ...}: whether `cve.repo_dir` holds a clone, and the
    list the parser ingested from it (m_cve_source)."""
    return cve_service.status()


@router.get("/file")
def get_cve_file(
    version: str = Query(""),
    file_path: str = Query(...),
) -> dict[str, Any]:
    """CVE security badge lookup for a source file at a kernel version."""
    return cve_service.cves_for_file(version, file_path)


@router.get("/list")
def get_cve_list(
    q: str = "",
    kind: Annotated[str, Query(pattern="^(|cna|commit|text)$")] = "",
    version: Annotated[str, Query(max_length=64)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50000)] = 50000,
) -> dict[str, Any]:
    """Filterable, paginated catalog of all ingested kernel CVE records."""
    return cve_service.catalog(q=q, kind=kind, version=version, offset=offset, limit=limit)


@router.get("/{cve_id}")
def get_cve(cve_id: str, release: Annotated[str, Query(max_length=64)] = "") -> dict[str, Any]:
    """One CVE: description, severity, introducing/fixing commit ranges with subjects and releases, version
    entries, files; with `release`, its state there."""
    from webapp.backend.security.git import is_safe_rev
    return cve_service.record(cve_id, release if release and is_safe_rev(release) else "")
