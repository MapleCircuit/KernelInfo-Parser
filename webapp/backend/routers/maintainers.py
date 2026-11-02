"""webapp/backend/routers/maintainers.py - Subsystems Roster, Maintainers & CREDITS."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query, Body, HTTPException
from webapp.backend.services.maintainer_service import MaintainerService

router = APIRouter(prefix="/api", tags=["maintainers"])
maintainer_service = MaintainerService()


@router.get("/maintainers/{version_name}/overview")
def get_maintainers(version_name: str, q: str = Query("")) -> dict[str, Any]:
    """Subsystems of the version's MAINTAINERS file, optionally filtered by name or member."""
    return maintainer_service.get_overview(version_name, q)


@router.get("/maintainers/{version_name}/section/{sec_id}")
def get_section(version_name: str, sec_id: str) -> dict[str, Any]:
    """Subsystem details, pattern rules and members (by section id or name)."""
    return maintainer_service.get_section_detail(version_name, sec_id)


@router.get("/maintainers/{version_name}/person")
@router.get("/maintainers/{version_name}/person/{person_id_or_email}")
def get_person(version_name: str, person_id_or_email: str | None = None, email: str | None = Query(None)) -> dict[str, Any]:
    """Developer profile and maintained subsystems. An email address goes in the `email` query
    parameter (its `@`, `<`, `/` do not survive a path segment)."""
    pid = person_id_or_email or email
    if not pid:
        raise HTTPException(status_code=400, detail="Missing developer ID or email.")
    return maintainer_service.get_person_profile(version_name, pid)


@router.post("/maintainers/{version_name}/match")
def match_maintainers(version_name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """get_maintainer.pl emulation on patch content and/or file paths."""
    return maintainer_service.match_maintainers(
        version_name,
        patch_text=payload.get("patch") or payload.get("patch_text") or "",
        paths=payload.get("touched_files") or payload.get("paths") or [],
    )


@router.get("/maintainers/{version_name}/credits")
def get_credits(version_name: str, q: str = Query("")) -> dict[str, Any]:
    """Entries of the version's CREDITS file, optionally filtered."""
    return maintainer_service.get_credits(version_name, q)


@router.get("/maintainers/{version_name}/developers")
def get_developers(
    version_name: str,
    q: str = Query(""),
    role: str = Query("all"),
    sort: str = Query("activity"),
) -> dict[str, Any]:
    """Every developer of the persona registry (alias personas merged), filtered by role."""
    return maintainer_service.get_developers(version_name, query=q, role=role, sort=sort)
