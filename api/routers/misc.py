"""api/routers/misc.py - Maintainers, docs, man pages, CVEs, and P3 REST-only utilities (`compile_commands`, `treemap`, `checkpatch`)."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from api.config import (
    get_active_repo_name,
    get_api_server_config,
    paginate_items,
    require_feature,
    resolve_version,
)
from api.routers.common import (
    _docs_service,
    _fs_service,
    _maintainer_service,
    _validate_man_rel_path,
    _validate_rel_path,
    cve_service,
    man_service,
)
from webapp.backend.security.git import safe_rev
from webapp.backend.services import checkpatch_service

router = APIRouter()


class MaintainerMatchRequest(BaseModel):
    """Stateless read-only `get_maintainer.pl` emulation over unified diff/patch text and/or repository file paths."""

    model_config = ConfigDict(extra="forbid")

    patch_text: str = Field(default="", max_length=5_000_000, description="Unified diff or patch text.")
    patch: str = Field(default="", max_length=5_000_000, description="Alias for patch_text.")
    paths: list[str] = Field(default_factory=list, description="Repository-relative file paths.")
    touched_files: list[str] = Field(default_factory=list, description="Alias for paths.")
    version: str | None = Field(default=None, max_length=256)


class CheckpatchRequest(BaseModel):
    """REST-only fenced `scripts/checkpatch.pl` run request."""

    model_config = ConfigDict(extra="forbid")

    patch: str = Field(default="", max_length=2_100_000, description="Unified diff / patch text (at most 2 MiB).")


@router.get("/maintainers", dependencies=[Depends(require_feature("maintainers"))])
def lookup_maintainers(
    path: Annotated[list[str] | None, Query()] = None,
    section: Annotated[str | None, Query(max_length=256)] = None,
    q: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Lookup maintainers by one or more repository file `path`s, `section`, or subsystem overview query `q`."""
    ver = resolve_version(version)
    if path:
        if len(path) > 500:
            raise HTTPException(status_code=400, detail="At most 500 paths allowed.")
        clean_paths = [_validate_rel_path(p, allow_empty=False) for p in path]
        res = _maintainer_service.match_maintainers(ver, paths=clean_paths)
        return {
            "repo": get_active_repo_name(),
            "mode": "path",
            "path": clean_paths[0] if len(clean_paths) == 1 else clean_paths,
            "paths": clean_paths,
            **res,
        }
    if section:
        res = _maintainer_service.get_section_detail(ver, section.strip())
        return {"repo": get_active_repo_name(), "mode": "section", **res}
    res = _maintainer_service.get_overview(ver, q)
    sections = res.get("sections") or []
    page, total, truncated = paginate_items(sections, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "mode": "overview",
        "version": res.get("version", ver),
        "indexed": res.get("indexed"),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "sections": page,
    }


@router.post("/maintainers/match", dependencies=[Depends(require_feature("maintainers"))])
def match_patch_maintainers(payload: MaintainerMatchRequest) -> dict[str, Any]:
    """Stateless read-only `get_maintainer.pl` recipient and subsystem matcher for patch text and/or file paths."""
    ver = resolve_version(payload.version)
    eff_patch = payload.patch_text or payload.patch or ""
    if len(eff_patch) > 5_000_000 or len(eff_patch.encode("utf-8")) > 5_000_000:
        raise HTTPException(status_code=413, detail="patch_text exceeds 5,000,000 bytes")
    raw_paths = list(payload.paths or payload.touched_files or [])
    if len(raw_paths) > 500:
        raise HTTPException(status_code=400, detail="At most 500 paths allowed.")
    clean_paths = [_validate_rel_path(p, allow_empty=False) for p in raw_paths]
    if not eff_patch.strip() and not clean_paths:
        raise HTTPException(status_code=400, detail="Provide `patch_text` (or `patch`) or `paths`.")
    res = _maintainer_service.match_maintainers(ver, patch_text=eff_patch, paths=clean_paths)
    return {"repo": get_active_repo_name(), **res}


@router.get("/maintainers/person", dependencies=[Depends(require_feature("maintainers"))])
def get_maintainer_person(
    person_id_or_email: Annotated[str | None, Query(max_length=256)] = None,
    email: Annotated[str | None, Query(max_length=256)] = None,
    mode: Annotated[str, Query(pattern="^(profile|developers|credits)$")] = "profile",
    q: Annotated[str, Query(max_length=256)] = "",
    role: Annotated[str, Query(max_length=64)] = "all",
    sort: Annotated[str, Query(max_length=64)] = "activity",
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Developer profile (`person_id_or_email`/`email`), persona directory (`mode='developers'`), or CREDITS lookup (`mode='credits'`)."""
    ver = resolve_version(version)
    pid = (person_id_or_email or email or "").strip()

    if mode == "credits":
        res = _maintainer_service.get_credits(ver, q)
        entries = list(res.get("entries") if "entries" in res else (res.get("credits") or []))
        page, total, truncated = paginate_items(entries, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "credits",
            "version": res.get("version", ver),
            "indexed": res.get("indexed"),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "entries": page,
        }

    if mode == "developers" or (not pid and q.strip()):
        res = _maintainer_service.get_developers(ver, query=q, role=role, sort=sort)
        devs = list(res.get("developers") or [])
        page, total, truncated = paginate_items(devs, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "developers",
            "version": res.get("version", ver),
            "role": role,
            "sort": sort,
            "counts": res.get("counts"),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "developers": page,
        }

    if not pid:
        raise HTTPException(
            status_code=400,
            detail="Missing developer ID or email (provide `person_id_or_email`, `email`, or `mode='developers'`/`'credits'`).",
        )
    res = _maintainer_service.get_person_profile(ver, pid)
    return {"repo": get_active_repo_name(), "mode": "profile", **res}


@router.get("/docs", dependencies=[Depends(require_feature("docs"))])
def lookup_docs(
    path: Annotated[str, Query(max_length=512)] = "",
    q: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    identifiers: Annotated[str, Query(max_length=256)] = "",
    functions: Annotated[str, Query(max_length=256)] = "",
    export: bool = False,
    internal: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Extract kernel-doc entries for `path` or search `Documentation/` RST labels."""
    ver = resolve_version(version)
    if path:
        clean_path = _validate_rel_path(path, allow_empty=False)
        res = _docs_service.get_kernel_doc(
            ver,
            clean_path,
            export=export,
            internal=internal,
            functions=functions,
            identifiers=identifiers,
        )
        entries = res.get("entries") or []
        page, total, truncated = paginate_items(entries, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "kernel-doc",
            "version": res.get("version", ver),
            "path": clean_path,
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "entries": page,
        }

    res = _docs_service.get_label_index(ver)
    labels_map = res.get("labels") or {}
    needle = q.strip().lower()
    items = [
        {"label": lbl, **meta}
        for lbl, meta in sorted(labels_map.items())
        if not needle or needle in lbl.lower() or needle in str(meta.get("title") or "").lower()
    ]
    page, total, truncated = paginate_items(items, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "mode": "labels",
        "version": res.get("version", ver),
        "source": res.get("source"),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "labels": page,
    }


@router.get("/man", dependencies=[Depends(require_feature("man"))])
def lookup_man(
    name: Annotated[str, Query(max_length=128)] = "",
    section: Annotated[str, Query(max_length=16)] = "",
    path: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    kernel: Annotated[str, Query(max_length=256)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Linux man-pages lookup: specific page by `name`/`path`, or index/tags listing."""
    if path:
        clean_path = _validate_man_rel_path(path)
        ver = resolve_version(version) if version else (man_service.tags().get("tags") or [{}])[0].get("name", "")
        if not ver:
            raise HTTPException(status_code=404, detail="No man-pages version available.")
        res = man_service.page(ver, path=clean_path, kernel=kernel or None)
        return {"repo": get_active_repo_name(), "mode": "page", **res}

    if name:
        if version:
            ver = safe_rev(version, "version")
            res = man_service.page(ver, name=name, section=section or None, kernel=kernel or None)
            return {"repo": get_active_repo_name(), "mode": "page", **res}
        resolved = man_service.resolve(name, section or None, kernel or None)
        return {"repo": get_active_repo_name(), "mode": "resolve", **resolved}

    if version:
        ver = safe_rev(version, "version")
        idx = man_service.index(ver)
        pages = idx.get("pages") or []
        page, total, truncated = paginate_items(pages, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "index",
            "version": idx.get("version", ver),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "pages": page,
        }

    tags_res = man_service.tags()
    tags = tags_res.get("tags") or []
    page, total, truncated = paginate_items(tags, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "mode": "tags",
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "tags": page,
    }


@router.get("/cve", dependencies=[Depends(require_feature("cve"))])
def lookup_cves(
    q: Annotated[str, Query(max_length=256)] = "",
    file_path: Annotated[str, Query(max_length=512)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    kind: Annotated[str, Query(pattern="^(|cna|commit|text)$")] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """CVE catalog search or per-file CVE lookup."""
    ver = resolve_version(version) if version else ""
    if file_path:
        clean_path = _validate_rel_path(file_path, allow_empty=False)
        res = cve_service.cves_for_file(ver or get_api_server_config()["default_version"], clean_path)
        rows = res.get("cves") or []
        page, total, truncated = paginate_items(rows, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "file",
            "version": res.get("version"),
            "file_path": clean_path,
            "affected_count": res.get("affected_count", 0),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "cves": page,
        }

    res = cve_service.catalog(q=q, kind=kind, version=ver, offset=offset, limit=limit)
    total = int(res.get("total") or 0)
    rows = res.get("cves") or []
    return {
        "repo": get_active_repo_name(),
        "mode": "catalog",
        "configured": res.get("configured"),
        "ingested": res.get("ingested"),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": (offset + len(rows)) < total,
        "cves": rows,
    }


@router.get("/cve/{cve_id}", dependencies=[Depends(require_feature("cve"))])
def get_cve_record(
    cve_id: str,
    release: Annotated[str, Query(max_length=64)] = "",
) -> dict[str, Any]:
    """Single CVE record with commit ranges and optional release state."""
    rel = safe_rev(release, "release") if release else ""
    return {"repo": get_active_repo_name(), **cve_service.record(cve_id, rel)}


# ------------------------------------------------------------------ P3 REST-Only Utilities (Excluded from MCP)


@router.get("/compile_commands")
def get_compile_commands(
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = "x86",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """REST-only paginated clang `compile_commands.json` entries for `version` and `arch`."""
    ver = resolve_version(version)
    cmds = list(_fs_service.export_compile_commands(ver, arch) or [])
    page, total, truncated = paginate_items(cmds, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "version": ver,
        "arch": arch,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "commands": page,
    }


@router.get("/treemap")
def get_codebase_treemap(
    version: Annotated[str | None, Query(max_length=256)] = None,
    path: Annotated[str, Query(max_length=512)] = "",
    max_depth: Annotated[int, Query(ge=1, le=6)] = 3,
) -> dict[str, Any]:
    """REST-only directory treemap hierarchy weighted by bytes and file counts."""
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=True)
    res = _fs_service.get_codebase_treemap(ver, max_depth, clean_path)
    return {"repo": get_active_repo_name(), **res}


@router.post("/checkpatch", dependencies=[Depends(require_feature("patch"))])
def run_checkpatch_tool(payload: CheckpatchRequest) -> dict[str, Any]:
    """REST-only fenced `scripts/checkpatch.pl` execution over a patch payload (at most 2 MiB)."""
    res = checkpatch_service.check_patch(payload.patch)
    return {"repo": get_active_repo_name(), **res}
