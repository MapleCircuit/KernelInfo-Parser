"""webapp/backend/services/imports_service.py - Symbols a C / header / assembly file imports through its
`#include` chain, for the code view's Imports panel (webapp/API_ENDPOINTS.md section 4.2).

The imported symbols of a file at a revision are the names its blob references (`m_symbol_ref` rows of
roles TypeUsage, Call, DeclRef, MacroExpansion, MacroBodyRef and CondRef) that a header the compilation reaches
for ARCH=<arch> defines (`m_symbol_def` rows of those headers' blobs). Reachability and the include chain of every
header come from `arch_layout_service.reachable()` (`{header: including file}` in breadth-first order, so
following the including files back gives a shortest chain); headers reached through a compiler-forced
`-include` root report `forced: true` and a chain starting at that root. An ARCH the revision does not build
falls back to the revision's definition ARCH (`arch_used`).

A reference names a symbol in one of C's name spaces, read from its role and its target AST's type: a struct,
union or enum tag (`struct file` -> `C_structnotbind`), a macro (MacroExpansion, CondRef), or an ordinary
identifier, which may also be a function-like or object-like macro. A definition matches a reference only in
the reference's name spaces, and names the file defines itself are not imports in the name spaces it defines
them in (prototypes, extern declarations and forward declarations do not count).

A header that relies on what its includer included before it (`include/linux/lockd/bind.h` in 2.6.0 uses
`struct file` without including linux/fs.h) leaves names its own chain does not define. Those resolve through
**context imports**: the files that include this one at the revision (`m_file_reference` Include rows of the
parser's `#include` directives; a row naming the include as written counts when the directive resolves to this
file for the ARCH) are walked up to their `#include` of this file
(`arch_layout_service.reachable_before()`); an item found that way has `via: "context"`, `context` (the
includer) and a chain starting at that includer. Items of the file's own chain have `via: "include"`.

Read with SELECTs only (webapp/API.md section 2.5); revisions whose blob is not indexed answer `indexed: false`
without a Git fallback.
"""
from __future__ import annotations

import contextlib
import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException

from core.globalstuff import ASTT, FileRefType, SymbolLinkage, SymbolRole
from db_engine.spec import Join
from parser.arch_layout import path_arch
from parser.kconfig_ast import kconfig_definitions as kd
from webapp.backend.config import RepoScopedCache
from webapp.backend.database.helpers import safe_decode
from webapp.backend.database.pool import get_reader
from webapp.backend.security.git import safe_rev
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.security.sql import is_valid_identifier
from webapp.backend.services import kconfig_facts
from webapp.backend.services.arch_layout_service import arch_layout_service
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.outline_service import outline_kind
from webapp.backend.services.tree_resolver import (
    DEFAULT_DEFINITION_ARCH,
    GitTreeResolver,
    git_tree_resolver,
)

logger = logging.getLogger(__name__)

IMPORT_EXTENSIONS = (".c", ".h", ".S", ".s")
MAX_ITEMS = 2000
MAX_REF_LINES = 20
MAX_REF_ROWS = 50_000
MAX_DEF_ROWS = 50_000
_CHUNK = 1000

# Reference roles that use a name defined elsewhere (Declaration and MemberRef name the file's own
# declarations and struct members)
_IMPORT_ROLES = [int(SymbolRole.TypeUsage), int(SymbolRole.Call), int(SymbolRole.DeclRef), int(SymbolRole.MacroExpansion),
                 int(SymbolRole.MacroBodyRef), int(SymbolRole.CondRef)]

_STRUCT = {"C_structdecl", "C_structnotbind"}
_TYPE = {"C_uniondecl", "C_unionnotbind", "C_enumdecl", "C_enumnotbind", "C_SCtypedef"}
# Forward declarations (`struct foo;`) rank like prototypes, after the full definition
_FORWARD = {"C_structnotbind", "C_unionnotbind", "C_enumnotbind", "C_functionprotnotbind"}
KIND_ORDER = ("function", "prototype", "struct", "type", "macro", "variable", "config")
_KIND_RANK = {k: i for i, k in enumerate(KIND_ORDER)}
# Kinds a reference role can name; a definition of a fitting kind ranks before one that only shares the name
_ROLE_KINDS = {
    int(SymbolRole.TypeUsage): {"struct", "type", "macro"},
    int(SymbolRole.Call): {"function", "prototype", "macro", "variable"},
    int(SymbolRole.DeclRef): {"function", "prototype", "macro", "variable", "config"},
    int(SymbolRole.MacroExpansion): {"macro", "config"},
    int(SymbolRole.MacroBodyRef): {"function", "prototype", "macro", "variable", "type", "struct", "config"},
    int(SymbolRole.CondRef): {"macro", "config"},
}

# C name spaces of definitions and references
NS_TAG, NS_MACRO, NS_ORDINARY = "tag", "macro", "ordinary"
_ALL_NS = frozenset({NS_TAG, NS_MACRO, NS_ORDINARY})
_TAG_TYPES = frozenset({"C_structdecl", "C_structnotbind", "C_uniondecl", "C_unionnotbind", "C_enumdecl",
                        "C_enumnotbind", "C_struct", "C_union", "C_enum"})
_MACRO_ROLES = frozenset({int(SymbolRole.MacroExpansion), int(SymbolRole.CondRef)})
MAX_CONTEXT_INCLUDERS = 24
MAX_INCLUDER_ROWS = 20_000
_KCONFIG_DEF_TYPES = [int(ASTT.Kconfig_Config), int(ASTT.Kconfig_Menuconfig)]
_IS_ENABLED_HELPERS = frozenset({"IS_ENABLED", "IS_BUILTIN", "IS_MODULE", "IS_REACHABLE"})
_ARCH_INCLUDE_RE = re.compile(r"^arch/[^/]+/include/(?:generated/(?:uapi/)?)?(?:uapi/)?(.+)$")
_ASM_DIR_RE = re.compile(r"^asm-(?!generic/)[^/]+/(.+)$")
_IS_ENABLED_ARG_RE = re.compile(r"\b(?:IS_ENABLED|IS_BUILTIN|IS_MODULE|IS_REACHABLE)\s*\(\s*(CONFIG_[A-Za-z0-9_]+)\s*\)")


def _type_name(type_id: Any) -> str:
    """ASTT name of an m_ast type id ("" for 0 / unknown: a target of unknown type)."""
    try:
        return ASTT(int(type_id)).name if type_id else ""
    except (TypeError, ValueError):
        return ""


def ref_namespaces(role: int, target_type: str) -> frozenset[str]:
    """Name spaces a reference of `role` to a target AST of type `target_type` can name."""
    if target_type in _TAG_TYPES:
        return frozenset({NS_TAG})
    if int(role) in _MACRO_ROLES:
        return frozenset({NS_MACRO})
    if not target_type:
        return _ALL_NS
    return frozenset({NS_ORDINARY, NS_MACRO})


def def_namespace(type_name: str) -> str:
    """Name space a definition of type `type_name` is in."""
    if type_name in _TAG_TYPES:
        return NS_TAG
    if type_name.startswith("CPPro_define") or type_name == "ASM_Macro":
        return NS_MACRO
    return NS_ORDINARY


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


def include_spellings(path: str) -> list[str]:
    """Names an `#include <...>` of `path` is written as, which the parser keeps as the include's target
    when it could not resolve the directive: `include/linux/x.h` -> `linux/x.h`,
    `include/uapi/linux/x.h` -> `linux/x.h`, `arch/<a>/include/asm/x.h` -> `asm/x.h`,
    `include/asm-<a>/x.h` -> `asm/x.h`."""
    out: list[str] = []
    m = _ARCH_INCLUDE_RE.match(path)
    if m:
        out.append(m.group(1))
    elif path.startswith("include/"):
        rest = path[len("include/"):]
        out.append(rest)
        if rest.startswith(("uapi/", "generated/")):
            out.append(rest.split("/", 1)[1].removeprefix("uapi/"))
        m = _ASM_DIR_RE.match(rest)
        if m:
            out.append("asm/" + m.group(1))
    return [p for p in dict.fromkeys(out) if p and p != path]


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
        empty = {"indexed": False, "layout": False, "reachable": 0, "total": 0, "truncated": False, "items": [],
                 "arch_used": None, "context": []}
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
            return {**base, **cached, "items": list(cached["items"]), "context": list(cached["context"])}
        try:
            with get_reader() as reader:
                blob_id = GitTreeResolver.map_blob_hashes(reader, [blob_sha]).get(blob_sha, (0, 0))[0]
                if not blob_id:
                    return {**base, **empty, "reason": "unindexed"}
                refs = self._references(reader, blob_id)
                if refs:
                    self._augment_is_enabled_refs(rev, clean, refs)
                    self._mask_own(refs, self._own_defs(reader, blob_id, list(refs)))
                kconfig_items = self._kconfig_items(reader, rev, arch, clean, tree, refs) if refs else []
            # The walk reads include facts through its own reader
            arch_used, reached = self._reach(rev, arch, clean)
            headers = [p for p in reached if p != clean]
            blob_paths: dict[int, list[str]] = {}
            defs: list[dict[str, Any]] = []
            resolved_kconfig = {it["name"] for it in kconfig_items}
            header_refs = {n: r for n, r in refs.items() if n not in resolved_kconfig}
            if headers and header_refs:
                with get_reader() as reader:
                    blob_paths = self._header_blobs(reader, tree, headers, blob_id)
                    if blob_paths:
                        defs = self._definitions(reader, self._ordered_blobs(blob_paths, reached), sorted(header_refs))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreachable database leaves the panel empty
            logger.debug("imports of %s unavailable: %s", clean, exc)
            return {**base, **empty, "reason": "database unavailable"}
        body = self._build(clean, header_refs, reached, blob_paths, defs)
        found = {it["name"] for it in body["items"]} | resolved_kconfig
        context: list[str] = []
        unresolved = {n: r for n, r in refs.items() if n not in found}
        extra: list[dict[str, Any]] = []
        if unresolved and arch_used:
            try:
                extra, context = self._context_items(rev, arch_used, clean, tree, blob_id, unresolved)
            except Exception as exc:  # noqa: BLE001 - context imports are best effort over the stored rows
                logger.debug("context imports of %s unavailable: %s", clean, exc)
                extra = []
        if extra or kconfig_items:
            items = body["items"] + extra + kconfig_items
            items.sort(key=lambda it: (_KIND_RANK[it["kind"]], it["name"], it["header_path"], it["line"]))
            body = {"total": len(items), "truncated": len(items) > MAX_ITEMS, "items": items[:MAX_ITEMS]}
        body.update({"indexed": True, "layout": bool(reached), "reachable": len(headers), "reason": None,
                     "arch_used": arch_used, "context": context})
        self._cache[key] = body
        return {**base, **body}

    @staticmethod
    def _reach(rev: str, arch: str, path: str) -> tuple[str | None, dict[str, str | None]]:
        """(ARCH walked, walk) of `path`: the requested ARCH, else the revision's definition ARCH when the
        revision does not build the requested one."""
        for candidate in dict.fromkeys((arch, DEFAULT_DEFINITION_ARCH, "x86_64", "i386", "x86")):
            if arch_layout_service.layout(rev, candidate) is None and candidate != arch:
                continue
            reached = arch_layout_service.reachable(rev, candidate, path)
            if reached:
                return candidate, reached
        return None, {}

    @staticmethod
    def _header_blobs(reader: Any, tree: dict[str, str], headers: Iterable[str], own_blob: int) -> dict[int, list[str]]:
        shas = {p: tree[p] for p in headers if p in tree}
        mapped = GitTreeResolver.map_blob_hashes(reader, list(shas.values()))
        out: dict[int, list[str]] = {}
        for p, sha in shas.items():
            hid = mapped.get(sha, (0, 0))[0]
            if hid and hid != own_blob:
                out.setdefault(hid, []).append(p)
        return out

    @staticmethod
    def _ordered_blobs(blob_paths: dict[int, list[str]], reached: dict[str, str | None]) -> list[int]:
        """Header blobs nearest first (breadth-first order), so a capped read keeps the closest headers."""
        order = {p: i for i, p in enumerate(reached)}
        return sorted(blob_paths, key=lambda b: (min(order.get(p, len(order)) for p in blob_paths[b]), b))

    @staticmethod
    def _references(reader: Any, blob_id: int) -> dict[str, dict[str, Any]]:
        """{name: {"lines": set, "roles": set, "spaces": set}} of the names the blob references."""
        rows = reader.select_rows(
            ("m_symbol_ref", "r"),
            {"name": "a.name", "target_type": "a.type_id", "line": "r.line", "role": "r.role"},
            {"r.blob_id": blob_id, "r.role": _IMPORT_ROLES},
            joins=[Join(("m_ast", "a"), {"a.ast_id": "r.ast_id"})],
            limit=MAX_REF_ROWS,
        )
        refs: dict[str, dict[str, Any]] = {}
        for r in rows:
            name = safe_decode(r.get("name"))
            if not name:
                continue
            role = int(r.get("role") or 0)
            entry = refs.setdefault(name, {"lines": set(), "roles": set(), "spaces": set()})
            line = int(r.get("line") or 0)
            if line > 0:
                entry["lines"].add(line)
            entry["roles"].add(role)
            entry["spaces"].update(ref_namespaces(role, _type_name(r.get("target_type"))))
        return refs

    @staticmethod
    def _augment_is_enabled_refs(rev: str, path: str, refs: dict[str, dict[str, Any]]) -> None:
        """Ensure `CONFIG_*` arguments inside `IS_ENABLED(CONFIG_*)` / `IS_BUILTIN` / `IS_MODULE` / `IS_REACHABLE`
        in C statements are present in `refs` even when the blob was indexed without macro-argument lexical refs."""
        if not (_IS_ENABLED_HELPERS & set(refs)):
            return
        try:
            raw, is_dir, _ = git_reader.read_file(rev, path)
        except Exception:  # noqa: BLE001
            return
        if not raw or is_dir:
            return
        for idx, line_text in enumerate(raw.decode("latin-1", errors="replace").replace("\r\n", "\n").split("\n"), 1):
            if "CONFIG_" not in line_text:
                continue
            for m in _IS_ENABLED_ARG_RE.finditer(line_text):
                cname = m.group(1)
                entry = refs.setdefault(cname, {"lines": set(), "roles": set(), "spaces": set()})
                entry["lines"].add(idx)
                entry["roles"].add(int(SymbolRole.MacroExpansion))
                entry["spaces"].add(NS_MACRO)

    @staticmethod
    def _kconfig_rank(kpath: str, file_path: str, arch: str) -> tuple[int, int, int, str]:
        """Rank candidate Kconfig files for `(file_path, arch)`: same subsystem tree first, then generic or
        target-arch Kconfig files ahead of foreign `arch/*/` trees, then shorter paths."""
        karch = path_arch(kpath)
        arch_norm = "x86" if arch in ("x86_64", "i386") else arch
        if karch is None or karch in (arch, arch_norm):
            arch_tier = 0
        else:
            arch_tier = 1
        fparts = file_path.split("/")[:-1]
        kparts = kpath.split("/")[:-1]
        common = 0
        for a, b in zip(fparts, kparts):
            if a != b:
                break
            common += 1
        return (arch_tier, -common, kpath.count("/"), kpath)

    @classmethod
    def _kconfig_items(cls, reader: Any, rev: str, arch: str, path: str, tree: dict[str, str],
                       refs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Resolve `CONFIG_*` references in `refs` against Kconfig definitions in `m_symbol_def` and
        `m_blob_facts` family 4 (`kconfig_facts`), returning items of kind `"config"`."""
        sym_to_refs: dict[str, list[str]] = defaultdict(list)
        for name, entry in refs.items():
            if not name.startswith("CONFIG_") or len(name) <= 7:
                continue
            if NS_MACRO not in entry.get("spaces", ()) and NS_ORDINARY not in entry.get("spaces", ()):
                continue
            bare = name[7:]
            sym_to_refs[bare].append(name)
            if bare.endswith("_MODULE") and len(bare) > 7:
                sym_to_refs[bare[:-7]].append(name)
        if not sym_to_refs:
            return []

        kconfig_paths = [p for p in tree if kd.applies(p)]
        if not kconfig_paths:
            return []

        wanted_syms = sorted(sym_to_refs)
        # 1. Query m_symbol_def for Kconfig_Config / Kconfig_Menuconfig rows
        def_rows: list[dict[str, Any]] = []
        for chunk in _chunks(wanted_syms):
            try:
                def_rows.extend(reader.select_rows(
                    ("m_symbol_def", "s"),
                    {"name": "s.name", "blob_id": "s.blob_id", "type_name": "t.name",
                     "line_s": "s.line_s", "line_e": "s.line_e"},
                    {"s.name": chunk, "s.type_id": _KCONFIG_DEF_TYPES},
                    joins=[Join(("m_type_descriptor", "t"), {"t.type_id": "s.type_id"})],
                ))
            except Exception:  # noqa: BLE001
                break

        candidates: dict[str, list[tuple[tuple, dict[str, Any]]]] = defaultdict(list)
        seen: set[tuple[str, str, int]] = set()

        def add_cand(ref_name: str, kpath: str, line_s: int, line_e: int, type_name: str) -> None:
            if line_s <= 0:
                return
            key = (ref_name, kpath, line_s)
            if key in seen:
                return
            seen.add(key)
            is_mod_alias = ref_name.endswith("_MODULE") and not kpath.endswith("_MODULE") and ref_name[7:] not in sym_exact.get(ref_name, ())
            rank = (1 if is_mod_alias else 0, *cls._kconfig_rank(kpath, path, arch), line_s)
            candidates[ref_name].append((rank, {
                "kind": "config",
                "type_name": type_name or "Kconfig_Config",
                "header_path": kpath,
                "line": line_s,
                "line_e": max(line_s, line_e),
                "chain": [path, kpath],
            }))

        sym_exact: dict[str, set[str]] = defaultdict(set)
        if def_rows:
            kshas = {p: tree[p] for p in kconfig_paths}
            mapped = GitTreeResolver.map_blob_hashes(reader, list(kshas.values()))
            blob_to_kpaths: dict[int, list[str]] = defaultdict(list)
            for kp, sha in kshas.items():
                bid = mapped.get(sha, (0, 0))[0]
                if bid:
                    blob_to_kpaths[bid].append(kp)
            for r in def_rows:
                sym = safe_decode(r.get("name"))
                for rn in sym_to_refs.get(sym, ()):
                    if rn[7:] == sym:
                        sym_exact[rn].add(sym)
            for r in def_rows:
                sym = safe_decode(r.get("name"))
                bid = int(r.get("blob_id") or 0)
                kpaths = blob_to_kpaths.get(bid)
                if not sym or not kpaths:
                    continue
                line_s = int(r.get("line_s") or 0)
                line_e = int(r.get("line_e") or line_s)
                tname = safe_decode(r.get("type_name")) or "Kconfig_Config"
                for rn in sym_to_refs.get(sym, ()):
                    for kp in kpaths:
                        add_cand(rn, kp, line_s, line_e, tname)

        # 2. Check m_blob_facts family 4 (via kconfig_facts.load_stored) for any CONFIG_* still unresolved
        missing_refs = {rn for rn_list in sym_to_refs.values() for rn in rn_list if rn not in candidates}
        if missing_refs:
            sha_to_kpaths: dict[str, list[str]] = defaultdict(list)
            for kp in kconfig_paths:
                sha = tree.get(kp)
                if sha and len(sha) == 40:
                    sha_to_kpaths[sha].append(kp)
            if sha_to_kpaths:
                with contextlib.suppress(Exception):
                    kconfig_facts.load_stored(sha_to_kpaths.keys())
                for sha, kpaths in sha_to_kpaths.items():
                    bdefs = kconfig_facts.cached(sha)
                    if not bdefs:
                        continue
                    for sym, facts_list in bdefs.items():
                        rnames = [rn for rn in sym_to_refs.get(sym, ()) if rn in missing_refs]
                        if not rnames:
                            continue
                        for rn in rnames:
                            if rn[7:] == sym:
                                sym_exact[rn].add(sym)
                        for f in facts_list:
                            ls = int(f.get("line_s") or 0)
                            le = int(f.get("line_e") or ls)
                            tn = "Kconfig_Menuconfig" if f.get("kind") == "menuconfig" else "Kconfig_Config"
                            for rn in rnames:
                                for kp in kpaths:
                                    add_cand(rn, kp, ls, le, tn)

        items: list[dict[str, Any]] = []
        for ref_name, cands in candidates.items():
            cands.sort(key=lambda c: c[0])
            best = dict(cands[0][1])
            lines = sorted(refs[ref_name]["lines"])
            others = {c[1]["header_path"] for c in cands} - {best["header_path"]}
            items.append({
                "name": ref_name,
                **best,
                "forced": False,
                "via": "kconfig",
                "ref_line": lines[0] if lines else 0,
                "ref_lines": lines[:MAX_REF_LINES],
                "ref_count": len(lines),
                "alternatives": len(others),
            })
        return items

    @staticmethod
    def _own_defs(reader: Any, blob_id: int, names: list[str]) -> dict[str, set[str]]:
        """{name: name spaces} the file itself defines among `names` (prototypes, extern declarations and
        forward declarations do not count)."""
        own: dict[str, set[str]] = {}
        for chunk in _chunks(sorted(names)):
            rows = reader.select_rows(
                ("m_symbol_def", "s"),
                {"name": "s.name", "linkage": "s.linkage", "type_name": "t.name"},
                {"s.blob_id": blob_id, "s.name": chunk},
                joins=[Join(("m_type_descriptor", "t"), {"t.type_id": "s.type_id"})],
            )
            for r in rows:
                linkage = r.get("linkage")
                type_name = safe_decode(r.get("type_name"))
                if linkage is not None and int(linkage) == SymbolLinkage.PrototypeDecl:
                    continue
                if type_name in _FORWARD:
                    continue
                own.setdefault(safe_decode(r.get("name")), set()).add(def_namespace(type_name))
        return own

    @staticmethod
    def _mask_own(refs: dict[str, dict[str, Any]], own: dict[str, set[str]]) -> None:
        """Drop the name spaces the file defines a referenced name in; a name left with none is no import."""
        for name, spaces in own.items():
            entry = refs.get(name)
            if entry is None:
                continue
            entry["spaces"] -= spaces
            if not entry["spaces"]:
                refs.pop(name, None)

    @staticmethod
    def _definitions(reader: Any, blob_ids: list[int], names: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for blobs in _chunks(blob_ids):
            for chunk in _chunks(names):
                rem = MAX_DEF_ROWS - len(rows)
                if rem <= 0:
                    return rows
                rows.extend(reader.select_rows(
                    ("m_symbol_def", "s"),
                    {"name": "s.name", "blob_id": "s.blob_id", "type_name": "t.name", "line_s": "s.line_s",
                     "line_e": "s.line_e", "linkage": "s.linkage"},
                    {"s.blob_id": blobs, "s.name": chunk},
                    joins=[Join(("m_type_descriptor", "t"), {"t.type_id": "s.type_id"})],
                    limit=rem,
                ))
                if len(rows) >= MAX_DEF_ROWS:
                    return rows
        return rows

    # ------------------------------------------------------------------ context imports
    def _context_items(self, rev: str, arch: str, path: str, tree: dict[str, str], blob_id: int,
                       unresolved: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """Items for `unresolved` names defined by what the files including `path` include before it, or
        forward-declared tag types completed later in a direct includer header itself."""
        with get_reader() as reader:
            includers = self._includers(reader, path, tree, rev, arch)
        if not includers:
            return [], []
        best: dict[str, tuple[tuple, dict[str, Any]]] = {}
        used_order: list[str] = []
        tag_unresolved = {n: r for n, r in unresolved.items() if NS_TAG in r.get("spaces", ())}
        for includer in includers[:MAX_CONTEXT_INCLUDERS]:
            walk = arch_layout_service.reachable_before(rev, arch, includer, path)
            headers = [p for p in walk if p not in (includer, path)]
            include_self = includer.endswith(".h") and bool(tag_unresolved) and includer in walk
            query_headers = [*headers, includer] if include_self else headers
            if not query_headers:
                continue
            with get_reader() as reader:
                blob_paths = self._header_blobs(reader, tree, query_headers, blob_id)
                if not blob_paths:
                    continue
                defs = self._definitions(reader, self._ordered_blobs(blob_paths, walk), sorted(unresolved))
            if include_self:
                # Direct includer header itself can only complete forward-declared tag types (`struct foo`)
                self_blobs = {b for b, ps in blob_paths.items() if includer in ps}
                filtered_defs = []
                for d in defs:
                    if int(d.get("blob_id") or 0) in self_blobs:
                        tname = safe_decode(d.get("type_name"))
                        if (
                            (def_namespace(tname) != NS_TAG or tname in _FORWARD)
                            and blob_paths.get(int(d.get("blob_id") or 0)) == [includer]
                        ):
                            continue
                    filtered_defs.append(d)
                defs = filtered_defs
            body = self._build(includer, unresolved, walk, blob_paths, defs)
            if body["items"] and includer not in used_order:
                used_order.append(includer)
            for item in body["items"]:
                weak = item["kind"] == "prototype" or item["type_name"] in _FORWARD
                item = {**item, "via": "context", "context": includer, "forced": item["chain"][0] != includer}
                rank = (1 if weak else 0, len(item["chain"]), item["header_path"], item["line"])
                cur = best.get(item["name"])
                if cur is None or rank < cur[0]:
                    best[item["name"]] = (rank, item)
            if all(n in best and best[n][0][0] == 0 for n in unresolved):
                break
        items = [pair[1] for pair in best.values()]
        winning_contexts = {it["context"] for it in items}
        used = [inc for inc in used_order if inc in winning_contexts] or used_order[:1] if items else []
        return items, used

    @staticmethod
    def _diversify_includers(paths: Iterable[str]) -> list[str]:
        """Prioritize `.c` callers first, interleaved round-robin across distinct directories/subsystems,
        followed by `.h` / `.S` includers also interleaved across directories."""
        c_by_dir: dict[str, list[str]] = defaultdict(list)
        h_by_dir: dict[str, list[str]] = defaultdict(list)
        for p in sorted(paths, key=lambda x: (x.count("/"), x)):
            d = p.rsplit("/", 1)[0] if "/" in p else ""
            if p.endswith(".c"):
                c_by_dir[d].append(p)
            else:
                h_by_dir[d].append(p)

        def round_robin(buckets: dict[str, list[str]]) -> list[str]:
            ordered_dirs = sorted(buckets, key=lambda d: (d.count("/"), d))
            out: list[str] = []
            idx = 0
            while True:
                added = False
                for d in ordered_dirs:
                    lst = buckets[d]
                    if idx < len(lst):
                        out.append(lst[idx])
                        added = True
                if not added:
                    break
                idx += 1
            return out

        c_list = round_robin(c_by_dir)
        h_list = round_robin(h_by_dir)
        if not h_list:
            return c_list
        if not c_list:
            return h_list
        # Place the first wave of distinct-directory .c callers and direct .h includers up front
        first_c_wave = min(len(c_by_dir), len(c_list))
        first_h_wave = min(len(h_by_dir), len(h_list))
        return c_list[:first_c_wave] + h_list[:first_h_wave] + c_list[first_c_wave:] + h_list[first_h_wave:]

    @classmethod
    def _includers(cls, reader: Any, path: str, tree: dict[str, str], rev: str | None = None,
                   arch: str | None = None) -> list[str]:
        """Files of the revision that `#include` `path` (the parser's include rows), diversified across
        directories with `.c` files and direct `.h` includers prioritized.

        A row targets `path` itself (an include the parser resolved) or one of its include spellings
        (`include_spellings()`: an include the parser kept as written); a spelled row counts only when the
        includer's directive resolves to `path` for ARCH=<arch> at `rev`."""
        spellings = include_spellings(path) if rev and arch else []
        fn_rows = reader.select_rows("m_file_name", ["fnid", "fname"], {"fname": [path, *spellings]})
        exact = {int(r["fnid"]) for r in fn_rows if safe_decode(r.get("fname")) == path}
        spelled = {int(r["fnid"]) for r in fn_rows} - exact
        if not exact and not spelled:
            return []
        rows = reader.select_rows("m_file_reference", ["source_blob_id", "source_fnid", "target_fnid", "details"],
                                  {"target_fnid": sorted(exact | spelled), "ref_type": int(FileRefType.Include)},
                                  limit=MAX_INCLUDER_ROWS)
        if not rows:
            return []
        fnids = sorted({int(r["source_fnid"]) for r in rows})
        names: dict[int, str] = {}
        for chunk in _chunks(fnids):
            for r in reader.select_rows("m_file_name", ["fnid", "fname"], {"fnid": chunk}):
                names[int(r["fnid"])] = safe_decode(r.get("fname"))
        # Rows of every revision's blob of a path: the includer counts through the row of its current blob
        by_src: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            src = names.get(int(r["source_fnid"]))
            if src and src != path and src in tree:
                by_src.setdefault(src, []).append(r)
        mapped = GitTreeResolver.map_blob_hashes(reader, [tree[p] for p in by_src])
        current: list[str] = []
        for src, src_rows in by_src.items():
            blob = mapped.get(tree[src], (0, 0))[0]
            for r in src_rows:
                if int(r["source_blob_id"]) != blob:
                    continue
                if int(r["target_fnid"]) not in exact:
                    written = safe_decode(r.get("details")).strip()
                    name = written.strip("<>\"")
                    if not name or arch_layout_service.resolve_include(rev, arch, name, src,
                                                                       written.startswith('"')) != path:
                        continue
                current.append(src)
                break
        return cls._diversify_includers(current)

    def closure_definitions(self, version_name: str, arch: str, start: str,
                            wanted: dict[str, str]) -> tuple[str | None, dict[str, dict[str, Any]]]:
        """(ARCH walked, {name: item}) of the names in `wanted` ({name: type name of the declaration, "" when
        unknown}) defined by `start` itself or a header its compilation reaches for ARCH=<arch>: the name
        space of the declaration's type, full definitions before prototypes / forward declarations, then the
        shortest chain. Items have the `_build()` shape (`header_path`, `line`, `chain`, ...)."""
        rev = safe_rev(version_name)
        if not wanted or not is_imports_path(start):
            return None, {}
        tree = git_tree_resolver.list_tree(rev)
        if not tree or start not in tree:
            return None, {}
        arch_used, reached = self._reach(rev, arch, start)
        if not reached:
            return arch_used, {}
        roles = set(_IMPORT_ROLES)
        refs = {name: {"lines": set(), "roles": roles,
                       "spaces": {def_namespace(type_name)} if type_name else set(_ALL_NS)}
                for name, type_name in wanted.items()}
        with get_reader() as reader:
            blob_paths = self._header_blobs(reader, tree, list(reached), 0)
            if not blob_paths:
                return arch_used, {}
            defs = self._definitions(reader, self._ordered_blobs(blob_paths, reached), sorted(refs))
        body = self._build(start, refs, reached, blob_paths, defs, prefer_full=True)
        return arch_used, {it["name"]: it for it in body["items"]}

    @staticmethod
    def _build(start: str, refs: dict[str, dict[str, Any]], reached: dict[str, str | None],
               blob_paths: dict[int, list[str]], defs: list[dict[str, Any]], prefer_full: bool = False) -> dict[str, Any]:
        """Items: per referenced name the best definition (fitting kind, shortest chain, full definition
        before prototype / forward declaration, then BFS order, path and line). `prefer_full` ranks a full
        definition before a prototype / forward declaration of a shorter chain."""
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
            spaces = ref.get("spaces")
            if spaces and def_namespace(type_name) not in spaces:
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
                strength = (1 if weak else 0, len(chain)) if prefer_full else (len(chain), 1 if weak else 0)
                rank = (0 if fits else 1, *strength, _KIND_RANK[kind],
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
                "name": name, **best, "chain": list(chain), "forced": chain[0] != start, "via": "include",
                "ref_line": lines[0] if lines else 0, "ref_lines": lines[:MAX_REF_LINES], "ref_count": len(lines),
                "alternatives": len(others),
            })
        items.sort(key=lambda it: (_KIND_RANK[it["kind"]], it["name"], it["header_path"], it["line"]))
        total = len(items)
        return {"total": total, "truncated": total > MAX_ITEMS, "items": items[:MAX_ITEMS]}


imports_service = ImportsService()
