"""api/routers/diffs.py - Revision, blob, Kconfig, Kernel API, and man-pages diff endpoints."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.config import (
    get_active_repo_name,
    paginate_items,
    require_feature,
)
from api.routers.common import (
    DEFAULT_ARCH,
    _diff_service,
    _validate_man_rel_path,
    _validate_rel_path,
    _with_repo,
    man_service,
)
from webapp.backend.routers.insights import job_answer
from webapp.backend.security.git import safe_rev
from webapp.backend.services import kapi_diff_service

router = APIRouter()


def _resolve_pair(
    version_a: str | None,
    version_b: str | None,
    a: str | None,
    b: str | None,
) -> tuple[str, str]:
    raw_a = (version_a or a or "").strip()
    raw_b = (version_b or b or "").strip()
    if not raw_a or not raw_b:
        raise HTTPException(status_code=400, detail="Provide `version_a` and `version_b` (or `a` and `b`).")
    return safe_rev(raw_a, "version_a"), safe_rev(raw_b, "version_b")


@router.get("/diff", dependencies=[Depends(require_feature("compare"))])
def get_revision_diff(
    version_a: Annotated[str, Query(min_length=1, max_length=256)],
    version_b: Annotated[str, Query(min_length=1, max_length=256)],
    path: Annotated[str, Query(max_length=512)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Bounded diff between `version_a` and `version_b` (single file when `path` is set, else changed files)."""
    va = safe_rev(version_a, "version_a")
    vb = safe_rev(version_b, "version_b")
    clean_path = _validate_rel_path(path, allow_empty=True)
    if clean_path:
        res = _diff_service.get_file_diff(va, vb, clean_path)
        return {"repo": get_active_repo_name(), **res}

    res = _diff_service.get_versions_diff(va, vb)
    changes = res.get("files") or res.get("changes") or []
    if isinstance(changes, dict):
        flat = [
            {"status": status, **(item if isinstance(item, dict) else {"path": item})}
            for status, items in changes.items()
            if isinstance(items, list)
            for item in items
        ]
        page, total, truncated = paginate_items(flat, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "version_a": va,
            "version_b": vb,
            "summary": res.get("summary"),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "changes": page,
        }
    page, total, truncated = paginate_items(list(changes), limit, offset)
    return {
        "repo": get_active_repo_name(),
        "version_a": va,
        "version_b": vb,
        "summary": res.get("summary"),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "changes": page,
    }


@router.get("/diff/blobs", dependencies=[Depends(require_feature("compare"))])
def get_blob_diff(
    version_a: Annotated[str, Query(min_length=1, max_length=256)],
    path_a: Annotated[str, Query(min_length=1, max_length=512)],
    version_b: Annotated[str, Query(min_length=1, max_length=256)],
    path_b: Annotated[str, Query(min_length=1, max_length=512)],
) -> dict[str, Any]:
    """Bounded line diff between any two repository file paths at any two revisions."""
    va = safe_rev(version_a, "version_a")
    vb = safe_rev(version_b, "version_b")
    clean_a = _validate_rel_path(path_a, allow_empty=False)
    clean_b = _validate_rel_path(path_b, allow_empty=False)
    res = _diff_service.get_blob_diff(va, clean_a, vb, clean_b)
    return {"repo": get_active_repo_name(), **res}


@router.get("/diff/kconfig", dependencies=[Depends(require_feature("compare", "kconfig"))])
def get_kconfig_diff(
    version_a: Annotated[str | None, Query(max_length=256)] = None,
    version_b: Annotated[str | None, Query(max_length=256)] = None,
    a: Annotated[str | None, Query(max_length=256)] = None,
    b: Annotated[str | None, Query(max_length=256)] = None,
) -> dict[str, Any]:
    """Kconfig symbol definitions added, removed, and changed between two revisions."""
    va, vb = _resolve_pair(version_a, version_b, a, b)
    res = _diff_service.get_kconfig_diff(va, vb)
    return {"repo": get_active_repo_name(), **res}


@router.get("/diff/kapi", dependencies=[Depends(require_feature("kapi", "compare"))])
def get_kapi_diff(
    version_a: Annotated[str | None, Query(max_length=256)] = None,
    version_b: Annotated[str | None, Query(max_length=256)] = None,
    a: Annotated[str | None, Query(max_length=256)] = None,
    b: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
    kinds: Annotated[str, Query(max_length=256)] = "",
) -> Any:
    """Kernel API changelog (`syscalls`, `exports`, `params`, `tracepoints`, `modules`) between two revisions."""
    va, vb = _resolve_pair(version_a, version_b, a, b)
    kind_list = [x.strip() for x in kinds.split(",") if x.strip()]
    return _with_repo(job_answer(kapi_diff_service.diff(va, vb, arch, kind_list)))


@router.get("/diff/man", dependencies=[Depends(require_feature("man", "compare"))])
def get_man_diff(
    version_a: Annotated[str | None, Query(max_length=256)] = None,
    version_b: Annotated[str | None, Query(max_length=256)] = None,
    a: Annotated[str | None, Query(max_length=256)] = None,
    b: Annotated[str | None, Query(max_length=256)] = None,
    path: Annotated[str | None, Query(max_length=256, description="Man-pages repository-relative path.")] = None,
    path_a: Annotated[str | None, Query(max_length=256)] = None,
    path_b: Annotated[str | None, Query(max_length=256)] = None,
    q: Annotated[str, Query(max_length=256)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Linux man-pages entries added, modified, or deleted between two revisions."""
    va, vb = _resolve_pair(version_a, version_b, a, b)
    clean_p = _validate_man_rel_path(path) if path else None
    clean_pa = _validate_man_rel_path(path_a) if path_a else None
    clean_pb = _validate_man_rel_path(path_b) if path_b else None
    if (clean_pa and not clean_pb) or (clean_pb and not clean_pa):
        raise HTTPException(status_code=400, detail="Provide both `path_a` and `path_b` when comparing two man page blobs.")
    if clean_pa and clean_pb:
        entry = man_service._man_repo_or_404()
        with man_service.using_repo(entry["name"]):
            blob_res = _diff_service.get_blob_diff(va, clean_pa, vb, clean_pb)
        return {"repo": get_active_repo_name(), "man_repo": entry["name"], **blob_res}

    res = man_service.diff(va, vb)
    entries = list(res.get("entries") or [])
    if clean_p:
        entries = [
            e
            for e in entries
            if e.get("path") == clean_p or e.get("path_a") == clean_p or e.get("path_b") == clean_p
        ]
    needle = q.strip().lower()
    if needle:
        entries = [
            e
            for e in entries
            if needle in str(e.get("name") or "").lower()
            or needle in str(e.get("path") or "").lower()
            or needle in str(e.get("summary") or "").lower()
        ]
    page, total, truncated = paginate_items(entries, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "man_repo": res.get("repo"),
        "version_a": va,
        "version_b": vb,
        "a": res.get("a", va),
        "b": res.get("b", vb),
        "commit_a": res.get("commit_a"),
        "commit_b": res.get("commit_b"),
        "source": res.get("source"),
        "summary": res.get("summary"),
        **({"path": clean_p} if clean_p else {}),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "entries": page,
    }
