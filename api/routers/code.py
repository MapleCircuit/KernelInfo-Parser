"""api/routers/code.py - Discovery, repositories/versions, filesystem, symbols, pahole, callgraph, and grep endpoints."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.config import (
    get_active_features,
    get_active_repo_name,
    get_api_server_config,
    paginate_items,
    require_feature,
    resolve_version,
)
from api.routers.common import (
    DEFAULT_DEFINITION_ARCH,
    _ENDPOINT_CATALOG,
    _LIMITS,
    _MAX_SEARCH_OFFSET,
    _callgraph_service,
    _fs_service,
    _locate_local,
    _pahole_service,
    _symbol_service,
    _validate_identifier,
    _validate_rel_path,
    _validate_search_offset,
    _with_repo,
    flatten_outline,
    grep_service,
    hover_service,
    imports_service,
    outline_service,
)
from webapp.backend.routers.insights import job_answer
from webapp.backend.routers.versions import get_branch_tags as _webapp_get_branch_tags
from webapp.backend.security.git import (
    safe_rev,
    validate_grep_pattern,
    validate_pathspecs,
)
from webapp.backend.services import pahole_compare

router = APIRouter()


def _v1():
    from api.routers import v1

    return v1


# ------------------------------------------------------------------ Discovery & Meta


@router.get("/capabilities")
def get_capabilities() -> dict[str, Any]:
    """Compact machine-readable summary of available endpoints, repos, features, and limits."""
    from api.mcp import MCP_PATH, SUPPORTED_PROTOCOL_VERSIONS, TOOLS  # lazy: api.mcp imports v1

    cfg = get_api_server_config()
    active_repo = get_active_repo_name()
    repos = [
        {
            "repo": p["repo"],
            "name": p["name"],
            "project": p["project"],
            "default_branch": p["default_branch"],
            "indexed": p["indexed"],
            "available": p["available"],
            "features": p["features"],
        }
        for p in _v1().projects_catalog()
    ]
    return {
        "repo": active_repo,
        "service": "KernelInfo-Parser AI API",
        "version": "1.0.0",
        "openapi_url": "/api/openapi.json",
        "mcp": {
            "endpoint": MCP_PATH,
            "transport": "streamable-http",
            "stateless": True,
            "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "stdio": "python3 api/mcp_stdio.py [-c config.json] [--repo NAME] [--allow-writes]",
            "tools": len(TOOLS),
        },
        "active_repo": active_repo,
        "default_version": cfg["default_version"],
        "repo_selection": {"header": "X-KI-Repo", "query": "repo"},
        "features": get_active_features(),
        "limits": _LIMITS,
        "repositories": repos,
        "endpoints": _ENDPOINT_CATALOG,
    }


# ------------------------------------------------------------------ Repositories & Versions


@router.get("/repos")
def list_repos() -> dict[str, Any]:
    """List configured and auto-detected repositories."""
    active = get_active_repo_name()
    repos = [{**p, "active": p["repo"] == active} for p in _v1().projects_catalog()]
    return {"repo": active, "total": len(repos), "active": active, "repositories": repos}


@router.get("/branches")
def list_branches(
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Paginated branches of the active repository."""
    all_branches = _v1().get_branches().get("branches") or []
    page, total, truncated = paginate_items(all_branches, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "branches": page,
    }


@router.get("/branches/tags")
def get_branch_tags(
    branch: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    source: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Paginated tag names reachable from `branch` or across a branch `source` (e.g. `history`, `stable`)."""
    res = _webapp_get_branch_tags(branch=branch, source=source)
    tags = res.get("tags") or []
    page, total, truncated = paginate_items(tags, limit, offset)
    return {
        "repo": get_active_repo_name(),
        **res,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "tags": page,
    }


@router.get("/versions")
def list_versions(
    q: Annotated[str, Query(max_length=128)] = "",
    include_rc: bool = True,
    indexed_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Paginated tags and indexed versions of the active repository."""
    rows = _v1().get_versions(include_rc=include_rc).get("versions") or []
    if indexed_only:
        rows = [r for r in rows if r.get("indexed")]
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows if needle in str(r.get("vname") or "").lower()]
    page, total, truncated = paginate_items(rows, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "versions": page,
    }


@router.get("/status")
def get_repo_status() -> dict[str, Any]:
    """Ingestion status of the active repository."""
    return _with_repo(_v1().ingest_status())


# ------------------------------------------------------------------ Code & Filesystem


@router.get("/tree")
def get_tree(
    version: Annotated[str | None, Query(max_length=256)] = None,
    path: Annotated[str, Query(max_length=512)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Paginated directory entries at a revision."""
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=True)
    res = _fs_service.get_tree(ver, clean_path)
    entries = res.get("entries") or res.get("tree") or []
    page, total, truncated = paginate_items(entries, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "path": clean_path,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "entries": page,
    }


@router.get("/files/search")
def search_files(
    q: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Fuzzy file path search at a revision."""
    _validate_search_offset(offset, _MAX_SEARCH_OFFSET)
    ver = resolve_version(version)
    fetch_cap = min(_MAX_SEARCH_OFFSET, offset + limit)
    res = _fs_service.search_files(ver, q, limit=fetch_cap)
    files = res.get("files") or []
    total = int(res.get("total") or len(files))
    sliced = files[offset : offset + limit]
    truncated = (offset + len(sliced)) < total
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "query": q,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "files": sliced,
    }


@router.get("/file")
def get_file_slice(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """Token-efficient line slice of a source file at a revision."""
    if end_line is not None and end_line < start_line:
        raise HTTPException(status_code=400, detail="end_line must be >= start_line.")
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=False)
    data = _fs_service.get_file(ver, clean_path)
    if data.get("is_directory"):
        raise HTTPException(status_code=400, detail="Path is a directory; use /api/v1/tree.")
    if data.get("is_binary") or data.get("is_oversized"):
        return {
            "repo": get_active_repo_name(),
            "version": data.get("version", ver),
            "path": clean_path,
            "is_binary": bool(data.get("is_binary")),
            "is_oversized": bool(data.get("is_oversized")),
            "truncated": False,
            "message": data.get("message", ""),
        }

    lines = (data.get("content") or "").splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return {
            "repo": get_active_repo_name(),
            "version": data.get("version", ver),
            "path": clean_path,
            "total_lines": 0,
            "start_line": 1,
            "end_line": 0,
            "truncated": False,
            "content": "",
        }

    effective_start = min(start_line, total_lines)
    requested_end = end_line if end_line is not None else (effective_start + limit - 1)
    capped_end = min(requested_end, effective_start + limit - 1, total_lines)
    slice_lines = lines[effective_start - 1 : capped_end]
    truncated = capped_end < total_lines and (end_line is None or requested_end > capped_end)

    return {
        "repo": get_active_repo_name(),
        "version": data.get("version", ver),
        "path": clean_path,
        "ftype": data.get("ftype"),
        "last_changed": (data.get("file_info") or {}).get("last_changed"),
        "total_lines": total_lines,
        "start_line": effective_start,
        "end_line": capped_end,
        "truncated": truncated,
        "content": "\n".join(slice_lines),
    }


@router.get("/file/outline")
def get_file_outline(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """AST/symbol outline of a source file."""
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=False)
    res = outline_service.get_outline(ver, clean_path)
    flat = flatten_outline(res.get("items") or [])
    page, total, truncated = paginate_items(
        flat, limit, offset, already_truncated=bool(res.get("truncated"))
    )
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "path": clean_path,
        "indexed": bool(res.get("indexed")),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "items": page,
    }


@router.get("/file/imports")
def get_file_imports(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_DEFINITION_ARCH,
    resolve_header: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Includes and imported symbols of a file, with optional single-header resolution."""
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=False)
    res = imports_service.get_imports(ver, clean_path, arch)
    raw_items = res.get("items") if "items" in res else (res.get("headers") or [])
    page, total_sliced, page_truncated = paginate_items(
        raw_items or [], limit, offset, already_truncated=bool(res.get("truncated"))
    )
    total = int(res["total"]) if res.get("total") is not None else total_sliced
    truncated = bool(page_truncated or (offset + len(page) < total))
    out: dict[str, Any] = {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "path": clean_path,
        "arch": res.get("arch", arch),
        "arch_used": res.get("arch_used"),
        "indexed": bool(res.get("indexed", False)),
        "layout": res.get("layout", False),
        "reachable": res.get("reachable", 0),
        "context": res.get("context") or [],
        "reason": res.get("reason"),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "items": page,
    }
    if resolve_header:
        out["resolved_header"] = _fs_service.resolve_include(
            ver, resolve_header, current_file=clean_path, arch=arch
        )
    return out


@router.get("/file/resolve")
def resolve_file_target(
    header: Annotated[str | None, Query(max_length=256)] = None,
    obj: Annotated[str | None, Query(alias="object", max_length=256)] = None,
    target: Annotated[str | None, Query(max_length=256)] = None,
    current_file: Annotated[str | None, Query(max_length=512)] = None,
    from_file: Annotated[str | None, Query(max_length=512)] = None,
    ast_id: Annotated[int | None, Query(ge=1)] = None,
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_DEFINITION_ARCH,
) -> dict[str, Any]:
    """Resolve an `#include` header directive or a `.o` Kbuild object target into a repository file path."""
    ver = resolve_version(version)
    eff_from = current_file or from_file
    clean_current = _validate_rel_path(eff_from, allow_empty=False) if eff_from else None
    eff_header = (header or "").strip()
    eff_obj = (obj or "").strip()
    if not eff_header and not eff_obj and target:
        t_clean = target.strip()
        if t_clean.endswith(".o"):
            eff_obj = t_clean
        else:
            eff_header = t_clean
    if eff_header and eff_obj:
        raise HTTPException(status_code=400, detail="Specify either `header` or `object`, not both.")
    if eff_header:
        res = _fs_service.resolve_include(ver, eff_header, ast_id=ast_id, current_file=clean_current, arch=arch)
        return {"repo": get_active_repo_name(), "version": ver, "mode": "include", "kind": "include", **res}
    if eff_obj:
        resolver = getattr(_fs_service, "resolve_object_target", None) or _fs_service.resolve_object
        res = resolver(ver, eff_obj, current_file=clean_current) if resolver is _fs_service.resolve_object else resolver(ver, eff_obj)
        return {"repo": get_active_repo_name(), "version": ver, "mode": "object", **res}
    raise HTTPException(status_code=400, detail="Provide `header` or `object` (or `target`) to resolve.")


@router.get("/file/references")
def get_file_references(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Files, Kconfig sources, docs, and Kbuild rules referencing `path`."""
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=False)
    res = _fs_service.get_file_references(ver, clean_path, limit=limit)
    return {"repo": get_active_repo_name(), **res}


# ------------------------------------------------------------------ Symbols & Navigation


@router.get("/symbols/search")
def search_symbols(
    q: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Search symbol definitions by prefix."""
    ver = resolve_version(version)
    fetch_cap = min(1000, offset + limit + 1)
    rows = _symbol_service.search_symbols(ver, q, limit=fetch_cap)
    page, total_fetched, truncated = paginate_items(
        rows, limit, offset, already_truncated=(len(rows) >= fetch_cap)
    )
    return {
        "repo": get_active_repo_name(),
        "version": ver,
        "query": q,
        "total": total_fetched,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "symbols": page,
    }


@router.get("/symbols/definition")
def get_symbol_definition(
    name: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_DEFINITION_ARCH,
    from_file: Annotated[str | None, Query(max_length=512)] = None,
    line: Annotated[int | None, Query(ge=1)] = None,
    col: Annotated[int | None, Query(ge=0)] = None,
    kind: Annotated[str | None, Query(pattern="^type$")] = None,
) -> dict[str, Any]:
    """Ranked symbol definition (with optional local-scope resolution at `from_file:line`)."""
    ver = resolve_version(version)
    ident = _validate_identifier(name, "symbol")
    clean_from = _validate_rel_path(from_file, allow_empty=False) if from_file else None
    if clean_from and line is not None and kind != "type":
        from webapp.backend.services.scope_service import detail_payload

        info, _in_fn = _locate_local(ver, ident, arch, clean_from, line, col)
        if info is not None:
            local = detail_payload(ver, ident, arch, clean_from, info)
            if local is not None:
                return {"repo": get_active_repo_name(), **local}
    res = _symbol_service.get_symbol_detail(ver, ident, arch, clean_from, kind=kind)
    return {"repo": get_active_repo_name(), **res}


@router.get("/symbols/scope")
def get_symbol_scope(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    line: Annotated[int, Query(ge=1)],
    col: Annotated[int, Query(ge=0)] = 0,
    name: Annotated[str | None, Query(max_length=256)] = None,
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_DEFINITION_ARCH,
) -> dict[str, Any]:
    """Local variable, parameter, static, or struct-member scope resolution at `path:line:col`."""
    from webapp.backend.services.scope_service import scope_service

    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=False)
    ident = _validate_identifier(name, "symbol") if name else None
    res = scope_service.resolve(ver, clean_path, line, col, ident, arch)
    return {
        "repo": get_active_repo_name(),
        "version": ver,
        "path": clean_path,
        "line": line,
        "col": col,
        "arch": arch,
        **res,
    }


@router.get("/symbols/references")
def get_symbol_references(
    name: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_DEFINITION_ARCH,
    from_file: Annotated[str | None, Query(max_length=512)] = None,
    line: Annotated[int | None, Query(ge=1)] = None,
    col: Annotated[int | None, Query(ge=0)] = None,
    ast_id: Annotated[int | None, Query(ge=0)] = None,
    tag_id: Annotated[int | None, Query(ge=1)] = None,
    header: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Paginated cross-references and definitions for a symbol (with local scope and `CPPro_include` lookup)."""
    ver = resolve_version(version)
    clean_from = _validate_rel_path(from_file, allow_empty=False) if from_file else None
    raw_name = (name or "").strip()

    if ast_id is not None or tag_id is not None or header is not None or raw_name == "CPPro_include":
        inc = _symbol_service.get_include_symbols(
            ver,
            ast_id or 0,
            tag_id=tag_id,
            file_path=clean_from,
            line=line,
            header=header,
            arch=arch,
        )
        symbols = inc.get("symbols") or []
        page, total, truncated = paginate_items(symbols, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "version": ver,
            "arch": inc.get("arch", arch),
            "symbol": "CPPro_include",
            "ast_id": inc.get("ast_id"),
            "include_text": inc.get("include_text"),
            "header_file": inc.get("header_file"),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "symbols": page,
            "references": page,
        }

    if not raw_name:
        raise HTTPException(status_code=400, detail="Symbol `name` (or `ast_id`/`header` for CPPro_include) is required.")
    ident = _validate_identifier(raw_name, "symbol")
    fetch_cap = min(5000, offset + limit + 1)

    info, in_fn = _locate_local(ver, ident, arch, clean_from, line, col)
    if info is not None and clean_from:
        from webapp.backend.services.scope_service import scope_service, xref_payload

        if "references" in info:
            local_res = info
        else:
            model = scope_service.model(ver, clean_from)
            local_res = xref_payload(ver, ident, arch, fetch_cap, model, info)
        refs = local_res.get("references") or []
        page, total, truncated = paginate_items(
            refs, limit, offset, already_truncated=(len(refs) >= fetch_cap)
        )
        return {
            "repo": get_active_repo_name(),
            "version": local_res.get("version", ver),
            "arch": local_res.get("arch", arch),
            "symbol": ident,
            "definition": local_res.get("definition"),
            "definitions": local_res.get("definitions") or [],
            "scope": local_res.get("scope"),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "references": page,
        }

    extra_kwargs = {"name_fallback": False} if in_fn else {}
    res = _symbol_service.get_symbol_xref(ver, ident, limit=fetch_cap, arch=arch, **extra_kwargs)
    refs = res.get("references") or []
    page, total, truncated = paginate_items(
        refs, limit, offset, already_truncated=(len(refs) >= fetch_cap)
    )
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "arch": res.get("arch", arch),
        "symbol": ident,
        "definition": res.get("definition"),
        "definitions": res.get("definitions") or [],
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "references": page,
    }


@router.get("/symbols/hover")
def get_symbol_hover(
    name: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_DEFINITION_ARCH,
    from_file: Annotated[str | None, Query(max_length=512)] = None,
    line: Annotated[int | None, Query(ge=1)] = None,
    col: Annotated[int | None, Query(ge=0)] = None,
) -> dict[str, Any]:
    """Hover signature, location, and kernel-doc for a symbol."""
    ver = resolve_version(version)
    ident = _validate_identifier(name, "symbol")
    clean_from = _validate_rel_path(from_file, allow_empty=False) if from_file else None
    if clean_from and line is not None:
        from webapp.backend.services.scope_service import hover_payload

        info, _in_fn = _locate_local(ver, ident, arch, clean_from, line, col)
        if info is not None:
            return {"repo": get_active_repo_name(), **hover_payload(ver, ident, arch, info)}
    res = hover_service.hover(ver, ident, arch, clean_from, _symbol_service.get_symbol_detail)
    return {"repo": get_active_repo_name(), **res}


@router.get("/symbols/ast/{ast_id}")
def get_symbol_ast(
    ast_id: int,
    depth: Annotated[int, Query(ge=0, le=12)] = 3,
) -> dict[str, Any]:
    """Recursive AST container subtree."""
    return {"repo": get_active_repo_name(), **_symbol_service.get_ast_tree(ast_id, depth)}


@router.get("/struct", dependencies=[Depends(require_feature("pahole"))])
def get_struct_layout(
    name: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
) -> dict[str, Any]:
    """Pahole-style struct/union memory layout, member offsets, holes, and cachelines."""
    ver = resolve_version(version)
    ident = _validate_identifier(name, "struct")
    return {"repo": get_active_repo_name(), **_pahole_service.get_struct_layout(ver, ident)}


@router.get("/struct/compare", dependencies=[Depends(require_feature("pahole"))])
def compare_struct_layout(
    name: Annotated[str | None, Query(max_length=256)] = None,
    struct: Annotated[str | None, Query(max_length=256)] = None,
    version_a: Annotated[str | None, Query(max_length=256)] = None,
    version_b: Annotated[str | None, Query(max_length=256)] = None,
    a: Annotated[str | None, Query(max_length=256)] = None,
    b: Annotated[str | None, Query(max_length=256)] = None,
) -> dict[str, Any]:
    """Member-by-member struct memory layout comparison between two revisions (`version_a`/`a` and `version_b`/`b`)."""
    raw_struct = (name or struct or "").strip().removeprefix("struct ").strip()
    if not raw_struct:
        raise HTTPException(status_code=400, detail="Provide struct `name` (or `struct`).")
    ident = _validate_identifier(raw_struct, "struct")
    raw_a = (version_a or a or "").strip()
    raw_b = (version_b or b or "").strip()
    if not raw_a or not raw_b:
        raise HTTPException(status_code=400, detail="Provide `version_a` and `version_b` (or `a` and `b`).")
    va = safe_rev(raw_a, "version_a")
    vb = safe_rev(raw_b, "version_b")
    res = pahole_compare.compare(va, vb, ident)
    return {"repo": get_active_repo_name(), **res}


@router.get("/struct/history", dependencies=[Depends(require_feature("pahole"))])
def get_struct_history(
    name: Annotated[str | None, Query(max_length=256)] = None,
    struct: Annotated[str | None, Query(max_length=256)] = None,
    version: Annotated[str | None, Query(max_length=256)] = None,
    include_rc: bool = False,
) -> Any:
    """Size, holes, and cacheline history of a struct across releases where its definition changed."""
    raw_struct = (name or struct or "").strip().removeprefix("struct ").strip()
    if not raw_struct:
        raise HTTPException(status_code=400, detail="Provide struct `name` (or `struct`).")
    ident = _validate_identifier(raw_struct, "struct")
    ver = resolve_version(version)
    return _with_repo(job_answer(pahole_compare.history_state(ver, ident, include_rc)))


@router.get("/implementations", dependencies=[Depends(require_feature("callgraph"))])
def get_implementations(
    struct: Annotated[str, Query(max_length=256)] = "",
    field: Annotated[str, Query(max_length=256)] = "",
    function: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
) -> Any:
    """Function-pointer and ops-table implementations by `struct` (+ optional `field`) or `function`."""
    ver = resolve_version(version)
    fn_clean = function.strip()
    st_clean = struct.strip().removeprefix("struct ").strip()
    fld_clean = field.strip()
    if fn_clean and st_clean:
        raise HTTPException(status_code=400, detail="Specify either `struct` or `function`, not both.")
    if fn_clean:
        _validate_identifier(fn_clean, "function")
        return _with_repo(
            job_answer(
                _v1().ops_uses_state(ver, fn_clean, wait=3.0),
                lambda r: {
                    "repo": get_active_repo_name(),
                    "version": ver,
                    "mode": "function",
                    **(r if isinstance(r, dict) else {}),
                },
            )
        )
    if not st_clean:
        raise HTTPException(status_code=400, detail="Give struct (and field) or function.")
    _validate_identifier(st_clean, "struct")
    if fld_clean:
        _validate_identifier(fld_clean, "field")

    def _shape(r: dict[str, Any]) -> dict[str, Any]:
        fields_raw = r.get("fields")
        fields_map = (
            fields_raw
            if isinstance(fields_raw, dict)
            else (r.get("by_field") if isinstance(r.get("by_field"), dict) else {})
        )
        fields_out = (
            [{"field": k, "count": len(v)} for k, v in fields_map.items()]
            if isinstance(fields_raw, dict)
            else (fields_raw if isinstance(fields_raw, list) else [])
        )
        out: dict[str, Any] = {
            **r,
            "repo": get_active_repo_name(),
            "version": ver,
            "mode": "struct",
            "struct": r.get("struct", st_clean),
            "tables": r.get("tables", 0),
            "files": r.get("files", 0),
            "positional_skipped": r.get("positional_skipped", 0),
            "fields": fields_out,
        }
        if fld_clean:
            out["field"] = fld_clean
            out["implementations"] = fields_map.get(fld_clean, [])
        return out

    return _with_repo(job_answer(_v1().ops_index_state(ver, st_clean, wait=3.0), _shape))


@router.get("/callgraph", dependencies=[Depends(require_feature("callgraph"))])
def get_callgraph(
    function: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Bidirectional callers and callees of a function."""
    ver = resolve_version(version)
    ident = _validate_identifier(function, "function")
    res = _callgraph_service.get_function_callgraph(ver, ident)
    callers, callers_total, callers_trunc = paginate_items(res.get("callers") or [], limit, offset)
    callees, callees_total, callees_trunc = paginate_items(res.get("callees") or [], limit, offset)
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "function": ident,
        "file_path": res.get("file_path"),
        "line_s": res.get("line_s"),
        "line_e": res.get("line_e"),
        "callers_total": callers_total,
        "callees_total": callees_total,
        "limit": limit,
        "offset": offset,
        "truncated": bool(res.get("truncated") or callers_trunc or callees_trunc),
        "callers": callers,
        "callees": callees,
    }


@router.get("/callpath", dependencies=[Depends(require_feature("callgraph"))])
def get_callpath(
    src: Annotated[str, Query(alias="from", min_length=1, max_length=256)],
    dst: Annotated[str, Query(alias="to", min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    max_depth: Annotated[int, Query(ge=1, le=10)] = 6,
    k: Annotated[int, Query(ge=1, le=20)] = 5,
    exclude: Annotated[str, Query(max_length=512)] = "",
    indirect: bool = False,
    arch: Annotated[str, Query(max_length=32)] = "",
) -> Any:
    """Shortest call chains between `from` and `to` functions."""
    ver = resolve_version(version)
    src_name = _validate_identifier(src, "from")
    dst_name = _validate_identifier(dst, "to")
    names = tuple(_validate_identifier(n, "exclude") for n in exclude.split(",") if n.strip())[:50]
    return _with_repo(
        job_answer(
            _v1().callpath_state(
                ver,
                src_name,
                dst_name,
                max_depth=max_depth,
                k=k,
                exclude=names,
                indirect=indirect,
                arch=arch.strip(),
            )
        )
    )


# ------------------------------------------------------------------ Search


@router.get("/grep")
def grep_code(
    q: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    mode: Annotated[str, Query(pattern="^(fixed|regex)$")] = "fixed",
    case: Annotated[int, Query(ge=0, le=1)] = 0,
    word: Annotated[int, Query(ge=0, le=1)] = 0,
    path: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    per_file: Annotated[int, Query(ge=1, le=200)] = 25,
) -> dict[str, Any]:
    """Bounded git grep search over a revision's tree."""
    ver = resolve_version(version)
    pattern = validate_grep_pattern(q)
    clean_paths = validate_pathspecs(path)
    fetch_cap = min(2000, offset + limit)
    res = grep_service.grep(
        ver,
        pattern,
        mode=mode,
        ignore_case=not case,
        word=bool(word),
        paths=clean_paths,
        max_matches=fetch_cap,
        max_per_file=per_file,
    )
    matches = res.get("matches") or []
    page, _len_matches, page_truncated = paginate_items(
        matches, limit, offset, already_truncated=bool(res.get("truncated"))
    )
    return {
        "repo": get_active_repo_name(),
        "version": ver,
        "query": pattern,
        "mode": mode,
        "case": bool(case),
        "word": bool(word),
        "paths": clean_paths,
        "total": int(res.get("total_matches") or len(matches)),
        "limit": limit,
        "offset": offset,
        "truncated": page_truncated,
        "timed_out": bool(res.get("timed_out")),
        "matches": page,
    }
