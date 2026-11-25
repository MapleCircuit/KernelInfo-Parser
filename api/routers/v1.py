"""api/routers/v1.py - Curated, token-efficient REST endpoints under /api/v1/ for AI & programmatic clients."""
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
from webapp.backend.routers.insights import job_answer
from webapp.backend.routers.system import ingest_status
from webapp.backend.routers.versions import get_branches, get_versions, projects_catalog
from webapp.backend.security.git import (
    safe_rev,
    validate_grep_pattern,
    validate_pathspecs,
)
from webapp.backend.security.jail import is_safe_rel_path, resolve_and_verify_repo_path
from webapp.backend.security.sql import is_valid_identifier
from webapp.backend.services import cve_service, man_service
from webapp.backend.services.callgraph_service import CallgraphService
from webapp.backend.services.callpath_service import callpath_state
from webapp.backend.services.commit_query import query_commits as run_commit_query
from webapp.backend.services.diff_service import DiffService
from webapp.backend.services.docs_service import DocsService
from webapp.backend.services.filesystem_service import FilesystemService
from webapp.backend.services.git_service import GitService
from webapp.backend.services.grep_service import grep_service
from webapp.backend.services.hover_service import hover_service
from webapp.backend.services.imports_service import imports_service
from webapp.backend.services.kconfig_service import DEFAULT_ARCH, KconfigService
from webapp.backend.services.maintainer_service import MaintainerService
from webapp.backend.services.outline_service import outline_service, flatten as flatten_outline
from webapp.backend.services.pahole_service import PaholeService
from webapp.backend.services.symbol_service import SymbolService
from webapp.backend.services.tree_resolver import DEFAULT_DEFINITION_ARCH

router = APIRouter(prefix="/api/v1", tags=["v1"])

_fs_service = FilesystemService()
_symbol_service = SymbolService()
_git_service = GitService()
_diff_service = DiffService()
_pahole_service = PaholeService()
_callgraph_service = CallgraphService()
_kconfig_service = KconfigService()
_maintainer_service = MaintainerService()
_docs_service = DocsService()

_LIMITS = {
    "default_page_limit": 50,
    "max_page_limit": 500,
    "max_file_lines": 500,
    "max_grep_matches": 500,
}

_ENDPOINT_CATALOG = [
    {"method": "GET", "path": "/api/v1/capabilities", "feature": None, "summary": "Service capabilities, repos, limits, and endpoint catalog"},
    {"method": "GET", "path": "/api/v1/repos", "feature": None, "summary": "Configured and auto-detected repositories"},
    {"method": "GET", "path": "/api/v1/branches", "feature": None, "summary": "Git branches of the active repository"},
    {"method": "GET", "path": "/api/v1/versions", "feature": None, "summary": "Tags and indexed versions of the active repository"},
    {"method": "GET", "path": "/api/v1/status", "feature": None, "summary": "Ingestion status of the active repository"},
    {"method": "GET", "path": "/api/v1/tree", "feature": None, "summary": "Directory listing at a revision"},
    {"method": "GET", "path": "/api/v1/files/search", "feature": None, "summary": "Fuzzy file path search at a revision"},
    {"method": "GET", "path": "/api/v1/file", "feature": None, "summary": "Bounded line slice of a file at a revision"},
    {"method": "GET", "path": "/api/v1/file/outline", "feature": None, "summary": "AST/symbol outline of a file"},
    {"method": "GET", "path": "/api/v1/file/imports", "feature": None, "summary": "Include directives and imported symbols for a file"},
    {"method": "GET", "path": "/api/v1/file/references", "feature": None, "summary": "Includers, Kconfig sources, docs, and Kbuild rules referencing a file"},
    {"method": "GET", "path": "/api/v1/symbols/search", "feature": None, "summary": "Symbol definition prefix search"},
    {"method": "GET", "path": "/api/v1/symbols/definition", "feature": None, "summary": "Ranked symbol definition (with optional local scope resolution)"},
    {"method": "GET", "path": "/api/v1/symbols/references", "feature": None, "summary": "Categorized cross-references of a symbol"},
    {"method": "GET", "path": "/api/v1/symbols/hover", "feature": None, "summary": "Hover signature, location, and kernel-doc for a symbol"},
    {"method": "GET", "path": "/api/v1/symbols/ast/{ast_id}", "feature": None, "summary": "Recursive AST container subtree"},
    {"method": "GET", "path": "/api/v1/struct", "feature": "pahole", "summary": "Struct/union memory layout, offsets, holes, and cachelines"},
    {"method": "GET", "path": "/api/v1/callgraph", "feature": "callgraph", "summary": "Bidirectional callers and callees of a function"},
    {"method": "GET", "path": "/api/v1/callpath", "feature": "callgraph", "summary": "Shortest call chains between two functions"},
    {"method": "GET", "path": "/api/v1/grep", "feature": None, "summary": "Bounded git grep search with pathspec filters"},
    {"method": "GET", "path": "/api/v1/commits", "feature": "commits", "summary": "Commit search and structured git log query"},
    {"method": "GET", "path": "/api/v1/commits/{commit_hash}", "feature": "commits", "summary": "Commit metadata, trailers, and touched files"},
    {"method": "GET", "path": "/api/v1/commits/{commit_hash}/diff", "feature": "commits", "summary": "Bounded unified diff of a commit"},
    {"method": "GET", "path": "/api/v1/blame", "feature": "commits", "summary": "Bounded line-by-line git blame slice"},
    {"method": "GET", "path": "/api/v1/history/file", "feature": "commits", "summary": "Chronological commit history of a file"},
    {"method": "GET", "path": "/api/v1/diff", "feature": "compare", "summary": "Bounded diff between two revisions (file or tree summary)"},
    {"method": "GET", "path": "/api/v1/kconfig/search", "feature": "kconfig", "summary": "Kconfig symbol search by name or prompt"},
    {"method": "GET", "path": "/api/v1/kconfig/symbol/{symbol_name}", "feature": "kconfig", "summary": "Kconfig symbol definition, dependencies, help, and usage"},
    {"method": "GET", "path": "/api/v1/kconfig/build", "feature": "kconfig", "summary": "Kbuild compile conditions for source paths"},
    {"method": "GET", "path": "/api/v1/maintainers", "feature": "maintainers", "summary": "Maintainers lookup by file path, section, or query"},
    {"method": "GET", "path": "/api/v1/docs", "feature": "docs", "summary": "Documentation labels or kernel-doc comments"},
    {"method": "GET", "path": "/api/v1/man", "feature": "man", "summary": "Linux man-pages lookup, index, or page content"},
    {"method": "GET", "path": "/api/v1/cve", "feature": "cve", "summary": "CVE catalog or per-file CVE lookup"},
    {"method": "GET", "path": "/api/v1/cve/{cve_id}", "feature": "cve", "summary": "Single CVE record with commit ranges and optional release state"},
]


def _validate_rel_path(path: str, *, allow_empty: bool = False) -> str:
    raw = (path or "").strip()
    if not raw or raw == "/":
        if allow_empty:
            return ""
        raise HTTPException(status_code=400, detail="Path parameter cannot be empty.")
    clean = raw.strip("/")
    if not clean or not is_safe_rel_path(clean):
        raise HTTPException(status_code=400, detail="Invalid relative repository path.")
    resolve_and_verify_repo_path(clean)
    return clean


def _validate_identifier(name: str, label: str = "identifier") -> str:
    cleaned = (name or "").strip()
    if not is_valid_identifier(cleaned):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {cleaned[:64]!r}")
    return cleaned


# ------------------------------------------------------------------ Discovery & Meta


@router.get("/capabilities")
def get_capabilities() -> dict[str, Any]:
    """Compact machine-readable summary of available endpoints, repos, features, and limits."""
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
        for p in projects_catalog()
    ]
    return {
        "service": "KernelInfo-Parser AI API",
        "version": "1.0.0",
        "openapi_url": "/api/openapi.json",
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
    repos = [{**p, "active": p["repo"] == active} for p in projects_catalog()]
    return {"total": len(repos), "active": active, "repositories": repos}


@router.get("/branches")
def list_branches(
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Paginated branches of the active repository."""
    all_branches = get_branches().get("branches") or []
    page, total, truncated = paginate_items(all_branches, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "branches": page,
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
    rows = get_versions(include_rc=include_rc).get("versions") or []
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
    return ingest_status()


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
    ver = resolve_version(version)
    res = _fs_service.search_files(ver, q, limit=min(200, offset + limit))
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
    headers = res.get("headers") or []
    page, total, truncated = paginate_items(headers, limit, offset)
    out: dict[str, Any] = {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "path": clean_path,
        "arch": res.get("arch", arch),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "headers": page,
    }
    if resolve_header:
        out["resolved_header"] = _fs_service.resolve_include(
            ver, resolve_header, current_file=clean_path, arch=arch
        )
    return out


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
        "total": len(page) if truncated else total_fetched,
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
        from webapp.backend.services.scope_service import detail_payload, scope_service

        info, _in_fn = scope_service.locate(ver, clean_from, line, col, ident, arch)
        if info is not None:
            local = detail_payload(ver, ident, arch, clean_from, info)
            if local is not None:
                return {"repo": get_active_repo_name(), **local}
    res = _symbol_service.get_symbol_detail(ver, ident, arch, clean_from, kind=kind)
    return {"repo": get_active_repo_name(), **res}


@router.get("/symbols/references")
def get_symbol_references(
    name: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_DEFINITION_ARCH,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Paginated cross-references and definitions for a symbol."""
    ver = resolve_version(version)
    ident = _validate_identifier(name, "symbol")
    fetch_cap = min(5000, offset + limit + 1)
    res = _symbol_service.get_symbol_xref(ver, ident, limit=fetch_cap, arch=arch)
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
        from webapp.backend.services.scope_service import hover_payload, scope_service

        info, _in_fn = scope_service.locate(ver, clean_from, line, col, ident, arch)
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
    return job_answer(
        callpath_state(
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


# ------------------------------------------------------------------ History & Commits


@router.get("/commits", dependencies=[Depends(require_feature("commits"))])
def query_commits(
    q: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    path: Annotated[list[str] | None, Query()] = None,
    author: Annotated[str, Query(max_length=128)] = "",
    committer: Annotated[str, Query(max_length=128)] = "",
    role: Annotated[list[str] | None, Query()] = None,
    since: Annotated[str, Query(max_length=32)] = "",
    until: Annotated[str, Query(max_length=32)] = "",
    range: Annotated[str, Query(max_length=256)] = "",
    release: Annotated[str, Query(max_length=128)] = "",
    merges: Annotated[str, Query(max_length=16)] = "",
    section: Annotated[str, Query(max_length=128)] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Commit search and structured git log query."""
    ver = resolve_version(version) if version else ""
    structured = any((path, author, committer, role, since, until, range, release, merges, section))
    if structured:
        validate_pathspecs(path)
        page_num = (offset // limit) + 1
        res = run_commit_query(
            ver,
            paths=path,
            author=author,
            committer=committer,
            roles=role,
            since=since,
            until=until,
            range_=range,
            release=release,
            merges=merges,
            section=section,
            text=q,
            page=page_num,
            limit=limit,
        )
        commits = res.get("commits") or []
        return {
            "repo": get_active_repo_name(),
            "version": ver,
            "limit": limit,
            "offset": (page_num - 1) * limit,
            "truncated": bool(res.get("has_more")),
            "commits": commits,
        }

    if offset + limit + 1 <= 500:
        res = _git_service.get_commits(ver, page=1, limit=offset + limit + 1, query=q)
        commits = res.get("commits") or []
        page, _total, truncated = paginate_items(commits, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "version": res.get("version", ver),
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "commits": page,
        }

    page_num = (offset // limit) + 1
    res = _git_service.get_commits(ver, page=page_num, limit=limit, query=q)
    commits = res.get("commits") or []
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "limit": limit,
        "offset": (page_num - 1) * limit,
        "truncated": len(commits) >= limit,
        "commits": commits,
    }


@router.get("/commits/{commit_hash}", dependencies=[Depends(require_feature("commits"))])
def get_commit(commit_hash: str) -> dict[str, Any]:
    """Commit metadata, trailers, and touched files."""
    safe_rev(commit_hash, "commit")
    return {"repo": get_active_repo_name(), **_git_service.get_commit_detail(commit_hash)}


@router.get("/commits/{commit_hash}/diff", dependencies=[Depends(require_feature("commits"))])
def get_commit_diff(
    commit_hash: str,
    context: Annotated[int, Query(ge=0, le=20)] = 3,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Bounded structured diff of a commit against its first parent."""
    safe_rev(commit_hash, "commit")
    res = _git_service.get_commit_diff(commit_hash, context=context)
    files = res.get("files") or []
    page, total, truncated = paginate_items(
        files, limit, offset, already_truncated=bool(res.get("truncated"))
    )
    return {
        "repo": get_active_repo_name(),
        "commit_hash": res.get("commit_hash"),
        "parent_hash": res.get("parent_hash"),
        "is_merge": res.get("is_merge"),
        "context": res.get("context", context),
        "additions": res.get("additions", 0),
        "deletions": res.get("deletions", 0),
        "total_files": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "files": page,
    }


@router.get("/blame", dependencies=[Depends(require_feature("commits"))])
def get_file_blame(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    move: bool = False,
    ignore_ws: bool = False,
) -> dict[str, Any]:
    """Bounded git blame slice for a file at a revision."""
    if end_line is not None and end_line < start_line:
        raise HTTPException(status_code=400, detail="end_line must be >= start_line.")
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=False)
    res = _git_service.get_blame(ver, clean_path, move=move, copy=False, ignore_ws=ignore_ws)
    all_lines = res.get("lines") or []
    sliced_range = [
        ln
        for ln in all_lines
        if ln.get("line", 0) >= start_line and (end_line is None or ln.get("line", 0) <= end_line)
    ]
    page, total, truncated = paginate_items(sliced_range, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "path": res.get("path", clean_path),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "lines": page,
    }


@router.get("/history/file", dependencies=[Depends(require_feature("commits"))])
def get_file_history(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Chronological commit history of a file up to `version`."""
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=False)
    fetch_cap = min(200, offset + limit + 1)
    res = _git_service.get_file_history(ver, clean_path, limit=fetch_cap)
    commits = res.get("commits") or []
    page, total, truncated = paginate_items(
        commits, limit, offset, already_truncated=(len(commits) >= 200)
    )
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "path": res.get("path", clean_path),
        "last_changed": res.get("last_changed"),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "commits": page,
    }


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


# ------------------------------------------------------------------ Subsystem Knowledge


@router.get("/kconfig/search", dependencies=[Depends(require_feature("kconfig"))])
def search_kconfig(
    q: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Search Kconfig symbols by name or prompt."""
    ver = resolve_version(version)
    res = _kconfig_service.search_symbols(ver, q=q, limit=min(500, offset + limit), arch=arch)
    items = res.get("results") or res.get("symbols") or []
    total = int(res.get("total") or len(items))
    sliced = items[offset : offset + limit]
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "arch": res.get("arch", arch),
        "query": q,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": (offset + len(sliced)) < total,
        "symbols": sliced,
    }


@router.get("/kconfig/symbol/{symbol_name}", dependencies=[Depends(require_feature("kconfig"))])
def get_kconfig_symbol(
    symbol_name: str,
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
    include_usage: bool = False,
    usage_limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Kconfig symbol definition, dependencies, help, and optional usage."""
    ver = resolve_version(version)
    ident = _validate_identifier(symbol_name, "kconfig symbol")
    detail = _kconfig_service.get_symbol_detail(ver, ident, arch)
    out: dict[str, Any] = {"repo": get_active_repo_name(), **detail}
    if include_usage:
        out["usage"] = _kconfig_service.get_symbol_usage(ver, ident, arch, limit=usage_limit)
    return out


@router.get("/kconfig/build", dependencies=[Depends(require_feature("kconfig"))])
def get_kconfig_build_conditions(
    path: Annotated[list[str], Query(min_length=1)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
) -> dict[str, Any]:
    """Kbuild compilation condition expressions for up to 64 source paths."""
    if len(path) > 64:
        raise HTTPException(status_code=400, detail="At most 64 paths allowed.")
    ver = resolve_version(version)
    clean_paths = [_validate_rel_path(p, allow_empty=False) for p in path]
    res = _kconfig_service.build_conditions(ver, clean_paths, arch)
    return {"repo": get_active_repo_name(), **res}


@router.get("/maintainers", dependencies=[Depends(require_feature("maintainers"))])
def lookup_maintainers(
    path: Annotated[str | None, Query(max_length=512)] = None,
    section: Annotated[str | None, Query(max_length=256)] = None,
    q: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Lookup maintainers by repository file `path`, `section`, or subsystem overview query `q`."""
    ver = resolve_version(version)
    if path:
        clean_path = _validate_rel_path(path, allow_empty=False)
        res = _maintainer_service.match_maintainers(ver, paths=[clean_path])
        return {"repo": get_active_repo_name(), "mode": "path", "path": clean_path, **res}
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
        clean_path = _validate_rel_path(path, allow_empty=False)
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
