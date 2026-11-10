"""webapp/backend/routers/cve.py - Linux kernel CVE records (H4, webapp/API.md section 4.11)."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from webapp.backend.services import cve_service

router = APIRouter(prefix="/api/cve", tags=["cve"])


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


@router.get("/{cve_id}")
def get_cve(cve_id: str, release: Annotated[str, Query(max_length=64)] = "") -> dict[str, Any]:
    """One CVE: description, severity, introducing/fixing commit ranges with subjects and releases, version
    entries, files; with `release`, its state there."""
    from webapp.backend.security.git import is_safe_rev
    return cve_service.record(cve_id, release if release and is_safe_rev(release) else "")
