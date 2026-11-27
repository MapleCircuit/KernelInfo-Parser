"""api/routers/common.py - Shared services, limits, endpoint catalog, and input validators for `/api/v1/*`."""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from api.config import get_active_repo_name, resolve_default_version, resolve_version
from webapp.backend.security.jail import (
    is_safe_rel_path,
    resolve_and_verify_repo_path,
)
from webapp.backend.security.sql import is_valid_identifier
from webapp.backend.services import cve_service, man_service
from webapp.backend.services.callgraph_service import CallgraphService
from webapp.backend.services.diff_service import DiffService
from webapp.backend.services.docs_service import DocsService
from webapp.backend.services.filesystem_service import FilesystemService
from webapp.backend.services.git_service import GitService
from webapp.backend.services.grep_service import grep_service
from webapp.backend.services.hover_service import hover_service
from webapp.backend.services.imports_service import imports_service
from webapp.backend.services.kconfig_service import DEFAULT_ARCH, KconfigService
from webapp.backend.services.maintainer_service import MaintainerService
from webapp.backend.services.nodemap_canvas_service import nodemap_canvas_service
from webapp.backend.services.nodemap_service import nodemap_service
from webapp.backend.services.outline_service import flatten as flatten_outline, outline_service
from webapp.backend.services.pahole_service import PaholeService
from webapp.backend.services.symbol_service import SymbolService
from webapp.backend.services.tree_resolver import DEFAULT_DEFINITION_ARCH

_fs_service = FilesystemService()
_symbol_service = SymbolService()
_git_service = GitService()
_diff_service = DiffService()
_pahole_service = PaholeService()
_callgraph_service = CallgraphService()
_kconfig_service = KconfigService()
_maintainer_service = MaintainerService()
_docs_service = DocsService()

_MAX_SEARCH_OFFSET = 2000

_LIMITS = {
    "default_page_limit": 50,
    "max_page_limit": 500,
    "max_file_lines": 500,
    "max_grep_matches": 500,
    "max_search_offset": _MAX_SEARCH_OFFSET,
}

_ENDPOINT_CATALOG: list[dict[str, Any]] = [
    # Discovery, Repositories & Versions (api/routers/code.py)
    {"method": "GET", "path": "/api/v1/capabilities", "feature": None, "summary": "Service capabilities, repos, limits, and endpoint catalog"},
    {"method": "GET", "path": "/api/v1/repos", "feature": None, "summary": "Configured and auto-detected repositories"},
    {"method": "GET", "path": "/api/v1/branches", "feature": None, "summary": "Git branches of the active repository"},
    {"method": "GET", "path": "/api/v1/branches/tags", "feature": None, "summary": "Tags reachable from a branch or across a branch source"},
    {"method": "GET", "path": "/api/v1/versions", "feature": None, "summary": "Tags and indexed versions of the active repository"},
    {"method": "GET", "path": "/api/v1/status", "feature": None, "summary": "Ingestion status of the active repository"},
    # Code & Filesystem (api/routers/code.py)
    {"method": "GET", "path": "/api/v1/tree", "feature": None, "summary": "Directory listing at a revision"},
    {"method": "GET", "path": "/api/v1/files/search", "feature": None, "summary": "Fuzzy file path search at a revision"},
    {"method": "GET", "path": "/api/v1/file", "feature": None, "summary": "Bounded line slice of a file at a revision"},
    {"method": "GET", "path": "/api/v1/file/outline", "feature": None, "summary": "AST/symbol outline of a file"},
    {"method": "GET", "path": "/api/v1/file/imports", "feature": None, "summary": "Include directives and imported symbols for a file"},
    {"method": "GET", "path": "/api/v1/file/resolve", "feature": None, "summary": "Resolve #include header directives and .o build object targets"},
    {"method": "GET", "path": "/api/v1/file/references", "feature": None, "summary": "Includers, Kconfig sources, docs, and Kbuild rules referencing a file"},
    # Symbols, Pahole, Callgraph & Search (api/routers/code.py)
    {"method": "GET", "path": "/api/v1/symbols/search", "feature": None, "summary": "Symbol definition prefix search"},
    {"method": "GET", "path": "/api/v1/symbols/definition", "feature": None, "summary": "Ranked symbol definition (with optional local scope resolution)"},
    {"method": "GET", "path": "/api/v1/symbols/scope", "feature": None, "summary": "Local variable, parameter, or struct member scope resolution at path:line:col"},
    {"method": "GET", "path": "/api/v1/symbols/references", "feature": None, "summary": "Categorized cross-references of a symbol or CPPro_include header usage"},
    {"method": "GET", "path": "/api/v1/symbols/hover", "feature": None, "summary": "Hover signature, location, and kernel-doc for a symbol"},
    {"method": "GET", "path": "/api/v1/symbols/ast/{ast_id}", "feature": None, "summary": "Recursive AST container subtree"},
    {"method": "GET", "path": "/api/v1/struct", "feature": "pahole", "summary": "Struct/union memory layout, offsets, holes, and cachelines"},
    {"method": "GET", "path": "/api/v1/struct/compare", "feature": "pahole", "summary": "Member-by-member struct layout comparison between two revisions"},
    {"method": "GET", "path": "/api/v1/struct/history", "feature": "pahole", "summary": "Struct size, holes, and cacheline history across releases"},
    {"method": "GET", "path": "/api/v1/implementations", "feature": "callgraph", "summary": "Function-pointer and ops-table implementations by struct (+ field) or function"},
    {"method": "GET", "path": "/api/v1/callgraph", "feature": "callgraph", "summary": "Bidirectional callers and callees of a function"},
    {"method": "GET", "path": "/api/v1/callpath", "feature": "callgraph", "summary": "Shortest call chains between two functions"},
    {"method": "GET", "path": "/api/v1/grep", "feature": None, "summary": "Bounded git grep search with pathspec filters"},
    # History & Commits (api/routers/history.py)
    {"method": "GET", "path": "/api/v1/commits", "feature": "commits", "summary": "Commit search and structured git log query"},
    {"method": "GET", "path": "/api/v1/commits/{commit_hash}", "feature": "commits", "summary": "Commit metadata, trailers, and touched files"},
    {"method": "GET", "path": "/api/v1/commits/{commit_hash}/diff", "feature": "commits", "summary": "Bounded unified diff of a commit"},
    {"method": "GET", "path": "/api/v1/commits/{commit_hash}/relations", "feature": "commits", "summary": "Fixes, fixed-by, reverts, backports, stable Cc, and CVEs of a commit"},
    {"method": "GET", "path": "/api/v1/commits/{commit_hash}/released", "feature": "commits", "summary": "First tag, mainline release, and stable release containing a commit"},
    {"method": "GET", "path": "/api/v1/blame", "feature": "commits", "summary": "Bounded line-by-line git blame slice"},
    {"method": "GET", "path": "/api/v1/history/file", "feature": "commits", "summary": "Chronological commit history of a file"},
    {"method": "GET", "path": "/api/v1/history/symbol", "feature": "commits", "summary": "Release-by-release definition history of a symbol at path:line"},
    {"method": "GET", "path": "/api/v1/history/lines", "feature": "commits", "summary": "Commits touching a file line range between two revisions (git log -L)"},
    # Compare & Diffs (api/routers/diffs.py)
    {"method": "GET", "path": "/api/v1/diff", "feature": "compare", "summary": "Bounded diff between two revisions (file or tree summary)"},
    {"method": "GET", "path": "/api/v1/diff/blobs", "feature": "compare", "summary": "Line diff between any two repository file paths at any two revisions"},
    {"method": "GET", "path": "/api/v1/diff/kconfig", "feature": "kconfig", "summary": "Kconfig symbol definitions added, removed, and changed between two revisions"},
    {"method": "GET", "path": "/api/v1/diff/kapi", "feature": "kapi", "summary": "Kernel API changelog (syscalls, exports, params, tracepoints, modules) between two revisions"},
    {"method": "GET", "path": "/api/v1/diff/man", "feature": "man", "summary": "Linux man-pages entries added, modified, or deleted between two revisions"},
    # Kconfig & Kbuild (api/routers/kconfig.py)
    {"method": "GET", "path": "/api/v1/kconfig/search", "feature": "kconfig", "summary": "Kconfig symbol search by name or prompt"},
    {"method": "GET", "path": "/api/v1/kconfig/symbol/{symbol_name}", "feature": "kconfig", "summary": "Kconfig symbol definition, dependencies, help, and usage"},
    {"method": "GET", "path": "/api/v1/kconfig/build", "feature": "kconfig", "summary": "Kbuild compile conditions for source paths"},
    {"method": "GET", "path": "/api/v1/kconfig/eval", "feature": "kconfig", "summary": "Evaluate symbols and Kbuild file conditions under a defconfig plus assignments"},
    {"method": "GET", "path": "/api/v1/kconfig/graph", "feature": "kconfig", "summary": "Direct dependency, select, and imply graph of a Kconfig symbol"},
    {"method": "GET", "path": "/api/v1/kconfig/solve", "feature": "kconfig", "summary": "Auto-solve legal symbol assignments to make a target Kconfig symbol reach y/m/n"},
    {"method": "GET", "path": "/api/v1/kconfig/defconfigs", "feature": "kconfig", "summary": "List architecture defconfigs/fragments or read a defconfig file's assignments"},
    {"method": "GET", "path": "/api/v1/kconfig/history", "feature": "kconfig", "summary": "Release-by-release history of a Kconfig symbol definition"},
    {"method": "GET", "path": "/api/v1/kconfig/kbuild", "feature": "kconfig", "summary": "Kbuild objects owning a source file or building named kernel modules"},
    # Kernel API (api/routers/kapi.py)
    {"method": "GET", "path": "/api/v1/kapi/summary", "feature": "kapi", "summary": "Per-release Kernel API summary and catalog counts"},
    {"method": "GET", "path": "/api/v1/kapi/syscalls", "feature": "kapi", "summary": "Paginated architecture system calls, entry points, and SYSCALL_DEFINE signatures"},
    {"method": "GET", "path": "/api/v1/kapi/exports", "feature": "kapi", "summary": "Paginated EXPORT_SYMBOL* catalog or single-symbol export site detail"},
    {"method": "GET", "path": "/api/v1/kapi/params", "feature": "kapi", "summary": "Paginated boot and module parameters or kernel-parameters.txt documentation entry"},
    {"method": "GET", "path": "/api/v1/kapi/tracepoints", "feature": "kapi", "summary": "Paginated tracepoint catalog or single-tracepoint call sites"},
    {"method": "GET", "path": "/api/v1/kapi/modules", "feature": "kapi", "summary": "Paginated loadable kernel modules or single-module parameters, exports, and maintainers"},
    {"method": "GET", "path": "/api/v1/kapi/devices", "feature": "kapi", "summary": "Paginated MODULE_DEVICE_TABLE entries or hardware ID / modalias driver matching"},
    # Mail / LKML (api/routers/mail.py)
    {"method": "GET", "path": "/api/v1/mail/search", "feature": "mail", "summary": "Search LKML archive messages across subjects, authors, and patches"},
    {"method": "GET", "path": "/api/v1/mail/message", "feature": "mail", "summary": "Full LKML email headers, body, and patch diffs by Message-ID"},
    {"method": "GET", "path": "/api/v1/mail/thread", "feature": "mail", "summary": "Chronological messages of an LKML thread with optional patch-series version list"},
    {"method": "GET", "path": "/api/v1/mail/merged", "feature": "mail", "summary": "Mainline commits and first releases a patch or series became"},
    # Insights & Watch (api/routers/insights.py)
    {"method": "GET", "path": "/api/v1/insights/stable", "feature": "stable", "summary": "Stable series catalog, series summary metrics, and missing stable backport gaps"},
    {"method": "GET", "path": "/api/v1/insights/subsystems", "feature": "maintainers", "summary": "MAINTAINERS subsystem health metrics, review coverage, bus factor, and risk flags"},
    {"method": "GET", "path": "/api/v1/insights/cves", "feature": "cve", "summary": "Release CVE security report filtered by state and CVSS threshold"},
    {"method": "POST", "path": "/api/v1/watch/changes", "feature": None, "mcp": True, "read_only": True, "summary": "Stateless read-only change check between two revisions for watched files, dirs, symbols, Kconfig, or sections"},
    # Maintainers, Docs, Man, CVE & P3 REST-Only Utilities (api/routers/misc.py)
    {"method": "GET", "path": "/api/v1/maintainers", "feature": "maintainers", "summary": "Maintainers lookup by file path(s), section, or query"},
    {"method": "POST", "path": "/api/v1/maintainers/match", "feature": "maintainers", "mcp": True, "read_only": True, "summary": "Stateless read-only get_maintainer.pl recipient and subsystem matcher for a patch or file list"},
    {"method": "GET", "path": "/api/v1/maintainers/person", "feature": "maintainers", "summary": "Developer profile, persona directory, or CREDITS lookup"},
    {"method": "GET", "path": "/api/v1/docs", "feature": "docs", "summary": "Documentation labels or kernel-doc comments"},
    {"method": "GET", "path": "/api/v1/man", "feature": "man", "summary": "Linux man-pages lookup, index, or page content"},
    {"method": "GET", "path": "/api/v1/cve", "feature": "cve", "summary": "CVE catalog or per-file CVE lookup"},
    {"method": "GET", "path": "/api/v1/cve/{cve_id}", "feature": "cve", "summary": "Single CVE record with commit ranges and optional release state"},
    {"method": "GET", "path": "/api/v1/compile_commands", "feature": None, "mcp": False, "summary": "REST-only clang compile_commands.json entries for a revision and architecture"},
    {"method": "GET", "path": "/api/v1/treemap", "feature": None, "mcp": False, "summary": "REST-only directory treemap hierarchy weighted by bytes and file counts"},
    {"method": "POST", "path": "/api/v1/checkpatch", "feature": "patch", "mcp": False, "summary": "REST-only fenced scripts/checkpatch.pl execution over a patch payload"},
    # NodeMap & Programmatic Canvases (api/routers/nodemap.py)
    {"method": "GET", "path": "/api/v1/nodemap/struct", "feature": "nodemap", "summary": "NodeMap struct/union members, nested aggregates, and preprocessor guards"},
    {"method": "GET", "path": "/api/v1/nodemap/source", "feature": "nodemap", "summary": "NodeMap source slice for a file, line range, or symbol definition span"},
    {"method": "GET", "path": "/api/v1/nodemap/canvases", "feature": "nodemap", "summary": "Paginated list of saved NodeMap canvases for the active repository"},
    {"method": "GET", "path": "/api/v1/nodemap/canvases/{canvas_id}", "feature": "nodemap", "summary": "Saved NodeMap canvas document, metadata, and webapp deep-link URL"},
    {"method": "GET", "path": "/api/v1/nodemap/canvases/{canvas_id}/export", "feature": "nodemap", "summary": "Export a saved NodeMap canvas as canonical JSON, Mermaid flowchart, or Graphviz DOT"},
    {"method": "POST", "path": "/api/v1/nodemap/preview", "feature": "nodemap", "mcp": True, "read_only": True, "summary": "Stateless read-only NodeMap canvas builder, expander, layout engine, and exporter"},
    {"method": "POST", "path": "/api/v1/nodemap/canvases", "feature": "nodemap", "scope": "nodemap:write", "mcp": True, "summary": "Create and persist a new NodeMap canvas document"},
    {"method": "PUT", "path": "/api/v1/nodemap/canvases/{canvas_id}", "feature": "nodemap", "scope": "nodemap:write", "mcp": True, "summary": "Replace a saved NodeMap canvas with optional optimistic revision check"},
    {"method": "DELETE", "path": "/api/v1/nodemap/canvases/{canvas_id}", "feature": "nodemap", "scope": "nodemap:write", "mcp": True, "summary": "Delete a saved NodeMap canvas JSON file"},
    {"method": "POST", "path": "/api/v1/nodemap/canvases/{canvas_id}/ops", "feature": "nodemap", "scope": "nodemap:write", "mcp": True, "summary": "Apply an atomic batch of declarative node/edge/group/annotation/tour operations to a saved canvas"},
    {"method": "POST", "path": "/api/v1/nodemap/canvases/{canvas_id}/expand", "feature": "nodemap", "scope": "nodemap:write", "mcp": True, "summary": "Expand a saved NodeMap canvas with struct_types, callgraph, callpath, kconfig, commit, or impact_map"},
    {"method": "POST", "path": "/api/v1/nodemap/canvases/{canvas_id}/layout", "feature": "nodemap", "scope": "nodemap:write", "mcp": True, "summary": "Run deterministic Sugiyama DAG or force-directed layout over a saved NodeMap canvas"},
    {"method": "POST", "path": "/api/mcp", "feature": None, "summary": "MCP Streamable HTTP endpoint (JSON-RPC 2.0)"},
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


def _validate_man_rel_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw or raw == "/":
        raise HTTPException(status_code=400, detail="Path parameter cannot be empty.")
    clean = raw.strip("/")
    if not clean or not is_safe_rel_path(clean):
        raise HTTPException(status_code=400, detail="Invalid relative repository path.")
    resolve_and_verify_repo_path(clean, man_service._repo_dir())
    return clean


def _validate_search_offset(offset: int, max_offset: int = _MAX_SEARCH_OFFSET) -> int:
    if offset >= max_offset:
        raise HTTPException(status_code=400, detail=f"offset ({offset}) exceeds maximum supported cap ({max_offset}).")
    return offset


def _with_repo(res: Any) -> Any:
    repo = get_active_repo_name()
    if isinstance(res, dict):
        return {"repo": repo, **res}
    if isinstance(res, JSONResponse):
        try:
            body = json.loads(bytes(res.body or b"{}"))
        except Exception:  # noqa: BLE001
            return res
        if isinstance(body, dict) and "repo" not in body:
            headers = {k: v for k, v in res.headers.items() if k.lower() != "content-length"}
            return JSONResponse({"repo": repo, **body}, status_code=res.status_code, headers=headers)
    return res


def _locate_local(
    ver: str,
    ident: str,
    arch: str,
    clean_from: str | None,
    line: int | None,
    col: int | None,
) -> tuple[dict[str, Any] | None, bool]:
    if not clean_from or line is None or not is_valid_identifier(ident):
        return None, False
    from webapp.backend.services.scope_service import scope_service

    try:
        return scope_service.locate(ver, clean_from, line, col, ident, arch)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - degrade gracefully to global lookup
        return None, False


__all__ = [
    "DEFAULT_ARCH",
    "DEFAULT_DEFINITION_ARCH",
    "_ENDPOINT_CATALOG",
    "_LIMITS",
    "_MAX_SEARCH_OFFSET",
    "_callgraph_service",
    "_diff_service",
    "_docs_service",
    "_fs_service",
    "_git_service",
    "_kconfig_service",
    "_locate_local",
    "_maintainer_service",
    "_pahole_service",
    "_symbol_service",
    "_validate_identifier",
    "_validate_man_rel_path",
    "_validate_rel_path",
    "_validate_search_offset",
    "_with_repo",
    "cve_service",
    "flatten_outline",
    "grep_service",
    "hover_service",
    "imports_service",
    "man_service",
    "nodemap_canvas_service",
    "nodemap_service",
    "outline_service",
    "resolve_default_version",
    "resolve_version",
]
