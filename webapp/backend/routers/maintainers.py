"""webapp/backend/routers/maintainers.py - Subsystems Roster, Maintainers & CREDITS."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query, Body
from webapp.backend.services.maintainer_service import MaintainerService

router = APIRouter(prefix="/api", tags=["maintainers"])
maintainer_service = MaintainerService()


@router.get("/maintainers")
@router.get("/maintainers/{version_name}/overview")
def get_maintainers(version: str = Query("v3.0"), q: str = Query(""), version_name: str | None = None) -> dict[str, Any]:
    """Search and list kernel subsystems."""
    v = version_name or version
    return maintainer_service.get_overview(v, q)


@router.get("/maintainers/section")
@router.get("/maintainers/{version_name}/section/{sec_id}")
def get_section(version: str = Query("v3.0"), sec_name: str | None = None, sec_id: int | str | None = None, version_name: str | None = None) -> dict[str, Any]:
    """Retrieve subsystem details, pattern rules, and members."""
    v = version_name or version
    name = sec_name or str(sec_id)
    return maintainer_service.get_section_detail(v, name)


@router.get("/maintainers/person")
@router.get("/maintainers/{version_name}/person")
@router.get("/maintainers/{version_name}/person/{person_id_or_email}")
def get_person(
    version: str = Query("v3.0"),
    person_id: int | str | None = Query(None),
    email: str | None = Query(None),
    name: str | None = Query(None),
    id: int | str | None = Query(None),
    version_name: str | None = None,
    person_id_or_email: str | None = None,
) -> dict[str, Any]:
    """Retrieve developer profile and maintained subsystems."""
    from fastapi import HTTPException
    v = version_name or version
    pid = person_id_or_email or person_id or email or name or id
    if not pid:
        raise HTTPException(status_code=400, detail="Missing developer ID, email, or name parameter.")
    return maintainer_service.get_person_profile(v, pid)


@router.post("/maintainers/match")
@router.post("/maintainers/{version_name}/match")
def match_maintainers(payload: dict[str, Any] = Body(...), version_name: str = "v3.0") -> dict[str, Any]:
    """Emulate get_maintainer.pl on file paths or patch content."""
    paths = payload.get("paths") or payload.get("touched_files") or []
    patch = payload.get("patch", None)
    return maintainer_service.match_maintainers(version_name, patch, paths)


@router.get("/credits")
@router.get("/maintainers/{version_name}/credits")
def get_credits(version: str = Query("v3.0"), q: str = Query(""), version_name: str | None = None) -> dict[str, Any]:
    """Search historical CREDITS directory."""
    v = version_name or version
    return maintainer_service.get_credits(v, q)


@router.get("/developers")
@router.get("/developers/{version_name}")
@router.get("/maintainers/{version_name}/developers")
def get_developers(
    version: str = Query("v3.0"),
    q: str = Query(""),
    query: str = Query(""),
    role: str = Query("all"),
    sort: str = Query("activity"),
    version_name: str | None = None,
) -> dict[str, Any]:
    """Search and list all kernel developers, maintainers, reviewers, and contributors."""
    v = version_name or version
    target_q = query or q
    return maintainer_service.get_developers(v, query=target_q, role=role, sort=sort)

