"""webapp/backend/services/imports_service.py - Symbols a C / header / assembly file imports through its
`#include` chain, for the code view's Imports panel (webapp/API_ENDPOINTS.md section 4.2).

The imported symbols of a file at a revision are the names its blob references (`m_symbol_ref` rows of
roles TypeUsage, Call, DeclRef and MacroExpansion) that a header the compilation reaches for ARCH=<arch>
defines (`m_symbol_def` rows of those headers' blobs). Reachability and the include chain of every header
come from `arch_layout_service.reachable()` (`{header: including file}` in breadth-first order, so
following the including files back gives a shortest chain); headers reached through a compiler-forced
`-include` root report `forced: true` and a chain starting at that root. Names the file defines itself are
not imports. Read with SELECTs only (webapp/API.md section 2.5); revisions whose blob is not indexed answer
`indexed: false` without a Git fallback.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from core.globalstuff import SymbolLinkage, SymbolRole
from db_engine.spec import Join
from webapp.backend.config import RepoScopedCache
from webapp.backend.database.helpers import safe_decode
from webapp.backend.database.pool import get_reader
from webapp.backend.security.git import safe_rev
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.security.sql import is_valid_identifier
from webapp.backend.services.arch_layout_service import arch_layout_service
from webapp.backend.services.outline_service import outline_kind
from webapp.backend.services.tree_resolver import DEFAULT_DEFINITION_ARCH, GitTreeResolver, git_tree_resolver

logger = logging.getLogger(__name__)

IMPORT_EXTENSIONS = (".c", ".h", ".S", ".s")
MAX_ITEMS = 2000
MAX_REF_LINES = 20
MAX_REF_ROWS = 50_000
MAX_DEF_ROWS = 50_000
_CHUNK = 1000

# Reference roles that use a name defined elsewhere (Declaration and MemberRef name the file's own
# declarations and struct members)
_IMPORT_ROLES = [int(SymbolRole.TypeUsage), int(SymbolRole.Call), int(SymbolRole.DeclRef), int(SymbolRole.MacroExpansion)]

_STRUCT = {"C_structdecl", "C_structnotbind"}
_TYPE = {"C_uniondecl", "C_unionnotbind", "C_enumdecl", "C_enumnotbind", "C_SCtypedef"}
# Forward declarations (`struct foo;`) rank like prototypes, after the full definition
_FORWARD = {"C_structnotbind", "C_unionnotbind", "C_enumnotbind", "C_functionprotnotbind"}
KIND_ORDER = ("function", "prototype", "struct", "type", "macro", "variable")
_KIND_RANK = {k: i for i, k in enumerate(KIND_ORDER)}
# Kinds a reference role can name; a definition of a fitting kind ranks before one that only shares the name
_ROLE_KINDS = {
    int(SymbolRole.TypeUsage): {"struct", "type", "macro"},
    int(SymbolRole.Call): {"function", "prototype", "macro", "variable"},
    int(SymbolRole.DeclRef): {"function", "prototype", "macro", "variable"},
    int(SymbolRole.MacroExpansion): {"macro"},
}


def import_kind(type_name: str, linkage: int | None = None, line_s: int = 0, line_e: int = 0) -> str | None:
    """function | prototype | struct | type | macro | variable, None for definitions that are not imports
    (kernel API entries, Kconfig entries, labels, instructions, Rust items)."""
    if not type_name.startswith(("C_", "CPPro_", "ASM_")):
        return None
    if type_name in _STRUCT:
        return "struct"
    if type_name in _TYPE:
        return "type"
    if type_name == "C_functionprotnotbind":
        return "prototype"
    kind = outline_kind(type_name, linkage, line_s, line_e)
    if kind == "constant":
        return "variable"
    return kind if kind in _KIND_RANK else None


def is_imports_path(path: str) -> bool:
    return bool(path) and path.endswith(IMPORT_EXTENSIONS) and bool(path.rsplit("/", 1)[-1].rsplit(".", 1)[0])


def include_chain(reached: dict[str, str | None], header: str) -> list[str]:
    """[root, ..., header] following the including files back to a root (the start file or a forced
    `-include` header, whose including file is None)."""
    chain = [header]
    seen = {header}
    cur = reached.get(header)
    while cur is not None and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = reached.get(cur)
    chain.reverse()
    return chain


def _chunks(values: list[Any], size: int = _CHUNK):
    for i in range(0, len(values), size):
        yield values[i:i + size]


class ImportsService:
    """Imported symbols of one file, cached per (commit, ARCH, path)."""

    def __init__(self) -> None:
        self._cache: RepoScopedCache = RepoScopedCache(maxsize=256)

    def get_imports(self, version_name: str, path: str, arch: str = DEFAULT_DEFINITION_ARCH) -> dict[str, Any]:
        rev = safe_rev(version_name)
        clean = str(path or "").strip().strip("/")
        if not clean or not is_safe_rel_path(clean):
            raise HTTPException(status_code=400, detail="Invalid path")
        arch = str(arch or DEFAULT_DEFINITION_ARCH).strip()
        if not is_valid_identifier(arch) or len(arch) > 32:
            raise HTTPException(status_code=400, detail="Invalid arch")
        base: dict[str, Any] = {"version": version_name, "path": clean, "arch": arch}
        empty = {"indexed": False, "layout": False, "reachable": 0, "total": 0, "truncated": False, "items": []}
        if not is_imports_path(clean):
            return {**base, "blob": None, **empty, "reason": "unsupported"}
        tree = git_tree_resolver.list_tree(rev)
        if tree is None:
            raise HTTPException(status_code=404, detail=f"Revision not found: {version_name}")
        blob_sha = tree.get(clean)
        if not blob_sha:
            raise HTTPException(status_code=404, detail=f"File not found at {version_name}: {clean}")
        base["blob"] = blob_sha
        key = (git_tree_resolver.resolve_commit_sha(rev) or rev, arch, clean, blob_sha)
        cached = self._cache.get(key)
        if cached is not None:
            return {**base, **cached}
        try:
            with get_reader() as reader:
                blob_id = GitTreeResolver.map_blob_hashes(reader, [blob_sha]).get(blob_sha, (0, 0))[0]
                if not blob_id:
                    return {**base, **empty, "reason": "unindexed"}
                refs = self._references(reader, blob_id)
                own = self._own_names(reader, blob_id, list(refs)) if refs else set()
                for name in own:
                    refs.pop(name, None)
            # The walk reads include facts through its own reader
            reached = arch_layout_service.reachable(rev, arch, clean)
            headers = [p for p in reached if p != clean]
            blob_paths: dict[int, list[str]] = {}
            defs: list[dict[str, Any]] = []
            if headers and refs:
                with get_reader() as reader:
                    shas = {p: tree[p] for p in headers if p in tree}
                    mapped = GitTreeResolver.map_blob_hashes(reader, list(shas.values()))
                    for p, sha in shas.items():
                        hid = mapped.get(sha, (0, 0))[0]
                        if hid and hid != blob_id:
                            blob_paths.setdefault(hid, []).append(p)
                    if blob_paths:
                        defs = self._definitions(reader, sorted(blob_paths), sorted(refs))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreachable database leaves the panel empty
            logger.debug("imports of %s unavailable: %s", clean, exc)
            return {**base, **empty, "reason": "database unavailable"}
        body = self._build(clean, refs, reached, blob_paths, defs)
        body.update({"indexed": True, "layout": bool(reached), "reachable": len(headers), "reason": None})
        self._cache[key] = body
        return {**base, **body}

    @staticmethod
    def _references(reader: Any, blob_id: int) -> dict[str, dict[str, Any]]:
        """{name: {"lines": set, "roles": set}} of the names the blob references."""
        rows = reader.select_rows(
            ("m_symbol_ref", "r"),
            {"name": "a.name", "line": "r.line", "role": "r.role"},
            {"r.blob_id": blob_id, "r.role": _IMPORT_ROLES},
            joins=[Join(("m_ast", "a"), {"a.ast_id": "r.ast_id"})],
            limit=MAX_REF_ROWS,
        )
        refs: dict[str, dict[str, Any]] = {}
        for r in rows:
            name = safe_decode(r.get("name"))
            if not name:
                continue
            entry = refs.setdefault(name, {"lines": set(), "roles": set()})
            line = int(r.get("line") or 0)
            if line > 0:
                entry["lines"].add(line)
            entry["roles"].add(int(r.get("role") or 0))
        return refs

    @staticmethod
    def _own_names(reader: Any, blob_id: int, names: list[str]) -> set[str]:
        """Referenced names the file itself defines (prototypes and extern declarations do not count)."""
        own: set[str] = set()
        for chunk in _chunks(sorted(names)):
            rows = reader.select_rows(
                ("m_symbol_def", "s"),
                {"name": "s.name", "linkage": "s.linkage", "type_name": "t.name"},
                {"s.blob_id": blob_id, "s.name": chunk},
                joins=[Join(("m_type_descriptor", "t"), {"t.type_id": "s.type_id"})],
            )
            for r in rows:
                linkage = r.get("linkage")
                if linkage is not None and int(linkage) == SymbolLinkage.PrototypeDecl:
                    continue
                if safe_decode(r.get("type_name")) in _FORWARD:
                    continue
                own.add(safe_decode(r.get("name")))
        return own

    @staticmethod
    def _definitions(reader: Any, blob_ids: list[int], names: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for blobs in _chunks(blob_ids):
            for chunk in _chunks(names):
                rows.extend(reader.select_rows(
                    ("m_symbol_def", "s"),
                    {"name": "s.name", "blob_id": "s.blob_id", "type_name": "t.name", "line_s": "s.line_s",
                     "line_e": "s.line_e", "linkage": "s.linkage"},
                    {"s.blob_id": blobs, "s.name": chunk},
                    joins=[Join(("m_type_descriptor", "t"), {"t.type_id": "s.type_id"})],
                    limit=MAX_DEF_ROWS,
                ))
                if len(rows) >= MAX_DEF_ROWS:
                    return rows
        return rows

    @staticmethod
    def _build(start: str, refs: dict[str, dict[str, Any]], reached: dict[str, str | None],
               blob_paths: dict[int, list[str]], defs: list[dict[str, Any]]) -> dict[str, Any]:
        """Items: per referenced name the best definition (fitting kind, shortest chain, full definition
        before prototype / forward declaration, then BFS order, path and line)."""
        order = {p: i for i, p in enumerate(reached)}
        chains: dict[str, list[str]] = {}
        candidates: dict[str, list[tuple[tuple, dict[str, Any]]]] = {}
        seen: set[tuple] = set()
        for r in defs:
            name = safe_decode(r.get("name"))
            ref = refs.get(name)
            if ref is None:
                continue
            type_name = safe_decode(r.get("type_name"))
            line_s = int(r.get("line_s") or 0)
            line_e = max(line_s, int(r.get("line_e") or line_s))
            linkage = r.get("linkage")
            linkage = int(linkage) if linkage is not None else None
            kind = import_kind(type_name, linkage, line_s, line_e)
            if kind is None or line_s <= 0:
                continue
            fits = any(kind in _ROLE_KINDS.get(role, ()) for role in ref["roles"])
            weak = kind == "prototype" or type_name in _FORWARD
            for header in blob_paths.get(int(r.get("blob_id") or 0), ()):
                dedup = (name, header, line_s, type_name)
                if dedup in seen:
                    continue
                seen.add(dedup)
                chain = chains.get(header)
                if chain is None:
                    chain = chains[header] = include_chain(reached, header)
                rank = (0 if fits else 1, len(chain), 1 if weak else 0, _KIND_RANK[kind],
                        order.get(header, len(order)), header, line_s, type_name)
                candidates.setdefault(name, []).append((rank, {
                    "kind": kind, "type_name": type_name, "header_path": header, "line": line_s, "line_e": line_e,
                    "chain": chain,
                }))
        items = []
        for name, cands in candidates.items():
            cands.sort(key=lambda c: c[0])
            best = dict(cands[0][1])
            chain = best["chain"]
            lines = sorted(refs[name]["lines"])
            others = {c[1]["header_path"] for c in cands} - {best["header_path"]}
            items.append({
                "name": name, **best, "chain": list(chain), "forced": chain[0] != start,
                "ref_line": lines[0] if lines else 0, "ref_lines": lines[:MAX_REF_LINES], "ref_count": len(lines),
                "alternatives": len(others),
            })
        items.sort(key=lambda it: (_KIND_RANK[it["kind"]], it["name"], it["header_path"], it["line"]))
        total = len(items)
        return {"total": total, "truncated": total > MAX_ITEMS, "items": items[:MAX_ITEMS]}


imports_service = ImportsService()
