"""webapp/backend/routers/man.py - Linux man-pages Endpoints (webapp/API_ENDPOINTS.md section 4.13).

The man-pages repository is the request's repository (`X-KI-Repo` / `?repo=`) or, when that is another
project, the configured man-pages repository; `/resolve` answers for a kernel revision of the request's
repository.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from webapp.backend.services import man_service

router = APIRouter(prefix="/api/man", tags=["man"])


@router.get("/tags")
def get_man_tags() -> dict[str, Any]:
    """The man-pages release tags with their dates, newest first."""
    return man_service.tags()


@router.get("/resolve")
def resolve_man_page(
    name: str = Query(...),
    section: str = Query(""),
    kernel: str = Query(""),
) -> dict[str, Any]:
    """Our copy of name(section) at the man-pages tag released on or before kernel revision `kernel`, else the man7.org URL."""
    return man_service.resolve(name, section or None, kernel or None)


@router.get("/{version_name}/index")
def get_man_index(version_name: str) -> dict[str, Any]:
    """Every page of a man-pages revision with its summary and `.so` alias target."""
    return man_service.index(version_name)


@router.get("/{version_name}/page")
def get_man_page(
    version_name: str,
    path: str = Query(""),
    name: str = Query(""),
    section: str = Query(""),
    kernel: str = Query(""),
    kernel_repo: str = Query(""),
) -> dict[str, Any]:
    """A page (by path, or by name with an optional section) as the document model, with its Linux links."""
    return man_service.page(version_name, path or None, name or None, section or None, kernel or None, kernel_repo or None)
