"""webapp/backend/services/outline_service.py - Definitions of one file, for the code view's outline (webapp/API.md section 4.2).

The definitions of a file at a revision are the `m_symbol_def` rows of its blob (content addressed, so
an answer per blob never changes): functions, types, macros, variables, kernel API entries (exports,
module parameters, tracepoints...), Kconfig entries and Rust items, sorted by line, with enum constants
and anything else whose line range lies inside a type, a function or a Kconfig menu nested under it.
Read with SELECTs only (section 2.5).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from core.globalstuff import SymbolLinkage
from webapp.backend.config import RepoScopedCache
from webapp.backend.database.helpers import safe_decode
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.security.git import safe_rev
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.services.tree_resolver import GitTreeResolver, git_tree_resolver

logger = logging.getLogger(__name__)

MAX_ITEMS = 5000

# Outline kind of each AST type name (parser/c_ast/CAST_API.md, parser/rust_ast/RUST_API.md,
# parser/kconfig_ast/KCONFIG_API.md); anything else defined by C is a variable.
_FUNCTION = {"C_functionprotodecl", "C_functionproto", "Rust_Fn", "C_SyscallDef", "C_AsmEntry"}
_TYPE = {"C_structdecl", "C_uniondecl", "C_enumdecl", "C_SCtypedef", "Rust_Struct", "Rust_Enum", "Rust_Union",
         "Rust_Trait", "Rust_TraitAlias", "Rust_Type", "Rust_Impl", "Rust_Mod"}
_MACRO = {"CPPro_define", "CPPro_define_macro", "Rust_MacroDef", "ASM_Macro"}
_ENUM_CONST = {"C_enumequal", "Rust_Variant", "Rust_Field"}
_API = {"C_ExportSymbolDef", "C_ModuleParamDef", "C_ModuleAttr", "C_TracepointDef", "C_PerCpuVarDef",
        "C_InitcallDef", "C_DeviceTableDef", "C_SysfsAttrDef", "C_MacroVarDef", "Rust_ModuleAttr"}
_KCONFIG = {"Kconfig_Config", "Kconfig_Menuconfig", "Kconfig_Choice", "Kconfig_Menu", "Kconfig_Mainmenu",
            "Kconfig_Comment", "Kconfig_If"}
_LABEL = {"ASM_Label"}
# Kinds whose line range can contain other definitions
_CONTAINERS = {"function", "type", "kconfig"}


def outline_kind(type_name: str, linkage: int | None = None, line_s: int = 0, line_e: int = 0) -> str:
    """function | prototype | type | macro | constant | variable | api | kconfig | label | instruction."""
    if type_name in _FUNCTION:
        return "prototype" if linkage == SymbolLinkage.PrototypeDecl else "function"
    if type_name in _TYPE:
        return "type"
    if type_name in _MACRO:
        return "macro"
    if type_name in _ENUM_CONST:
        return "constant"
    if type_name in _API:
        return "api"
    if type_name in _KCONFIG:
        return "kconfig"
    if type_name in _LABEL:
        if line_e > line_s or (linkage is not None and linkage in {SymbolLinkage.ExternalDef, SymbolLinkage.InternalStaticDef, SymbolLinkage.WeakOrAliasDef}):
            return "function"
        return "label"
    if type_name == "ASM_Instruction":
        return "instruction"
    if type_name in {"Rust_Const", "Rust_Static"} or type_name.startswith("C_"):
        return "variable"
    return "other"


def build_outline(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows {name, type_name, line_s, line_e, linkage} -> sorted, nested items.

    An item is nested under the innermost container (function, type, Kconfig menu) whose line range
    strictly contains it; prototypes and extern declarations keep their place in line order."""
    items = []
    seen = set()
    for r in rows:
        name = safe_decode(r.get("name"))
        type_name = safe_decode(r.get("type_name"))
        line_s = int(r.get("line_s") or 0)
        line_e = max(line_s, int(r.get("line_e") or line_s))
        linkage = r.get("linkage")
        linkage = int(linkage) if linkage is not None else None
        key = (name, type_name, line_s, line_e)
        if not name or line_s <= 0 or key in seen:
            continue
        seen.add(key)
        items.append({
            "name": name, "kind": outline_kind(type_name, linkage, line_s, line_e), "type_name": type_name,
            "line_s": line_s, "line_e": line_e, "linkage": linkage, "children": [],
        })
    # Outer ranges first at the same start line, so containers precede what they contain
    items.sort(key=lambda it: (it["line_s"], -it["line_e"], it["name"]))
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for it in items:
        while stack and not (stack[-1]["line_s"] <= it["line_s"] and it["line_e"] <= stack[-1]["line_e"]
                             and (stack[-1]["line_s"], stack[-1]["line_e"]) != (it["line_s"], it["line_e"])):
            stack.pop()
        (stack[-1]["children"] if stack else roots).append(it)
        if it["kind"] in _CONTAINERS and it["line_e"] > it["line_s"]:
            stack.append(it)
    return roots


def flatten(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for it in items:
        out.append(it)
        out.extend(flatten(it["children"]))
    return out


class OutlineService:
    """Definitions of one file at a revision, cached per blob (content addressed, never stale)."""

    def __init__(self) -> None:
        self._cache: RepoScopedCache = RepoScopedCache(maxsize=1024)

    def get_outline(self, version_name: str, path: str) -> dict[str, Any]:
        rev = safe_rev(version_name)
        clean = str(path or "").strip().strip("/")
        if not clean or not is_safe_rel_path(clean):
            raise HTTPException(status_code=400, detail="Invalid path")
        tree = git_tree_resolver.list_tree(rev)
        if tree is None:
            raise HTTPException(status_code=404, detail=f"Revision not found: {version_name}")
        blob_sha = tree.get(clean)
        if not blob_sha:
            raise HTTPException(status_code=404, detail=f"File not found at {version_name}: {clean}")
        base = {"version": version_name, "path": clean, "blob": blob_sha}
        cached = self._cache.get(blob_sha)
        if cached is not None:
            return {**base, **cached}
        try:
            with get_db_cursor() as cursor:
                mapped = GitTreeResolver.map_blob_hashes(cursor, [blob_sha])
                blob_id = mapped.get(blob_sha, (0, 0))[0]
                if not blob_id:
                    from webapp.backend.services.git_reader import git_reader
                    payload = None
                    if clean.endswith((".c", ".h")):
                        _status, payload, _err = git_reader.read_object(blob_sha, "")
                        if payload:
                            from parser.c_ast.c_pure_extractor import extract_c_outline
                            items = extract_c_outline(payload)
                            body = {"indexed": True, "items": items, "count": len(flatten(items)), "truncated": False}
                            self._cache[blob_sha] = body
                            return {**base, **body}
                    elif clean.endswith((".S", ".s")):
                        _status, payload, _err = git_reader.read_object(blob_sha, "")
                        if payload:
                            from parser.asm_ast.asm_pure_extractor import extract_asm_outline
                            items = extract_asm_outline(payload)
                            body = {"indexed": True, "items": items, "count": len(flatten(items)), "truncated": False}
                            self._cache[blob_sha] = body
                            return {**base, **body}
                    elif clean.endswith(".rs"):
                        _status, payload, _err = git_reader.read_object(blob_sha, "")
                        if payload:
                            from parser.rust_ast.rust_pure_extractor import extract_rust_outline
                            items = extract_rust_outline(payload)
                            body = {"indexed": True, "items": items, "count": len(flatten(items)), "truncated": False}
                            self._cache[blob_sha] = body
                            return {**base, **body}
                    body = {"indexed": False, "items": [], "count": 0}
                    return {**base, **body}
                cursor.execute(
                    """
                    SELECT s.name, t.name AS type_name, s.line_s, s.line_e, s.linkage
                    FROM m_symbol_def s
                    JOIN m_type_descriptor t ON s.type_id = t.type_id
                    WHERE s.blob_id = %s
                    LIMIT %s;
                    """,
                    (blob_id, MAX_ITEMS),
                )
                rows = cursor.fetchall()
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreachable database leaves the outline empty
            logger.debug("outline of %s unavailable: %s", clean, exc)
            return {**base, "indexed": False, "items": [], "count": 0, "error": "database unavailable"}
        items = build_outline(rows)
        body = {"indexed": True, "items": items, "count": len(flatten(items)), "truncated": len(rows) >= MAX_ITEMS}
        self._cache[blob_sha] = body
        return {**base, **body}


outline_service = OutlineService()
