"""parser/c_ast/c_pure_extractor.py - Pure-Python C Outline Extractor.

Free of TableEngine, GreatProcessor, or Database driver dependencies.
Used by webapp backend (OutlineService) as a fast "git" fallback for unparsed revisions.
"""
from __future__ import annotations

import re
from typing import Any

from core.globalstuff import SymbolLinkage
from parser.source_facts import extract as extract_source_facts, F_STATIC, F_MACRO_FN, F_MACRO_OBJ

_MASK = re.compile(r"//[^\n]*|/\*.*?\*/|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", re.S)
_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(\([^\)]*\))?", re.M)
_TYPE_DECL = re.compile(r"\b(struct|union|enum)\s+([A-Za-z_]\w*)\s*\{", re.M)
_TYPEDEF = re.compile(r"^[ \t]*typedef\s+.*?\s+([A-Za-z_]\w*)\s*;", re.M | re.S)
_PROTO = re.compile(
    r"^[ \t]*(?:(?:static|extern|inline|__always_inline|__maybe_unused|asmlinkage)\s+)*"
    r"(?:struct\s+\w+|union\s+\w+|enum\s+\w+|[A-Za-z_]\w+)"
    r"(?:\s+|\s*\*+\s*|\s+\*+)"
    r"([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*;",
    re.M,
)

_CONTAINERS = {"function", "type"}


def _blank_code(text: str) -> str:
    """Mask comments and string literals with spaces while preserving newlines."""
    return _MASK.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def extract_c_outline(source_bytes: bytes) -> list[dict[str, Any]]:
    """Extract outline items (functions, structs, unions, enums, macros) from raw C source bytes."""
    if not source_bytes:
        return []

    code = source_bytes.decode("latin-1", errors="replace")
    masked = _blank_code(code)
    lines = code.replace("\r\n", "\n").split("\n")
    num_lines = len(lines)

    # Compute line start offsets in masked
    line_starts = [0]
    for idx, line in enumerate(lines[:-1]):
        line_starts.append(line_starts[-1] + len(line) + 1)

    def offset_to_line(offset: int) -> int:
        import bisect
        return bisect.bisect_right(line_starts, offset)

    raw_items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()

    # 1. Functions and complex macros via source_facts
    try:
        facts = extract_source_facts(code)
        for def_row in facts.get("d", []):
            name, line_s, line_e, flags = def_row[0], int(def_row[1]), int(def_row[2]), int(def_row[3])
            if flags & (F_MACRO_FN | F_MACRO_OBJ):
                type_name = "CPPro_define_macro" if (flags & F_MACRO_FN) else "CPPro_define"
                kind = "macro"
                linkage = int(SymbolLinkage.MacroOrKernelDef)
            else:
                type_name = "C_functionprotodecl"
                kind = "function"
                linkage = int(SymbolLinkage.InternalStaticDef) if (flags & F_STATIC) else int(SymbolLinkage.ExternalDef)
            key = (name, type_name, line_s)
            if key not in seen:
                seen.add(key)
                raw_items.append({
                    "name": name,
                    "kind": kind,
                    "type_name": type_name,
                    "line_s": line_s,
                    "line_e": max(line_s, line_e),
                    "linkage": linkage,
                })
    except Exception:
        pass

    # 2. All #define directives (catches simple constants and object-like macros)
    for m in _DEFINE.finditer(masked):
        name = m.group(1)
        line_s = offset_to_line(m.start())
        # Follow line continuations
        curr_line = line_s
        while curr_line <= num_lines and lines[curr_line - 1].rstrip().endswith("\\"):
            curr_line += 1
        line_e = curr_line
        is_macro_fn = bool(m.group(2))
        type_name = "CPPro_define_macro" if is_macro_fn else "CPPro_define"
        key = (name, type_name, line_s)
        if key not in seen:
            seen.add(key)
            raw_items.append({
                "name": name,
                "kind": "macro",
                "type_name": type_name,
                "line_s": line_s,
                "line_e": line_e,
                "linkage": int(SymbolLinkage.MacroOrKernelDef),
            })

    # 3. Struct, union, and enum declarations
    type_map = {
        "struct": "C_structdecl",
        "union": "C_uniondecl",
        "enum": "C_enumdecl",
    }
    for m in _TYPE_DECL.finditer(masked):
        decl_kw = m.group(1)
        name = m.group(2)
        brace_pos = m.end() - 1
        line_s = offset_to_line(m.start())
        # Find matching closing brace
        depth = 1
        pos = brace_pos + 1
        n_chars = len(masked)
        while pos < n_chars and depth > 0:
            ch = masked[pos]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            pos += 1
        end_offset = pos - 1 if depth == 0 else m.end()
        line_e = max(line_s, offset_to_line(end_offset))
        type_name = type_map.get(decl_kw, "C_structdecl")
        key = (name, type_name, line_s)
        if key not in seen:
            seen.add(key)
            raw_items.append({
                "name": name,
                "kind": "type",
                "type_name": type_name,
                "line_s": line_s,
                "line_e": line_e,
                "linkage": int(SymbolLinkage.ExternalDef),
            })

    # 4. Function prototypes (header prototypes)
    for m in _PROTO.finditer(masked):
        name = m.group(1)
        if name in {"if", "while", "for", "switch", "return", "sizeof"}:
            continue
        line_s = offset_to_line(m.start())
        line_e = offset_to_line(m.end())
        key = (name, "C_functionproto", line_s)
        if key not in seen and (name, "C_functionprotodecl", line_s) not in seen:
            seen.add(key)
            raw_items.append({
                "name": name,
                "kind": "prototype",
                "type_name": "C_functionproto",
                "line_s": line_s,
                "line_e": line_e,
                "linkage": int(SymbolLinkage.PrototypeDecl),
            })

    # Sort items: line_s ascending, line_e descending (outer containers first)
    raw_items.sort(key=lambda it: (it["line_s"], -it["line_e"], it["name"]))

    # Nest into hierarchy
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []

    for raw in raw_items:
        node = {
            "name": raw["name"],
            "kind": raw["kind"],
            "type_name": raw["type_name"],
            "line_s": raw["line_s"],
            "line_e": raw["line_e"],
            "linkage": raw.get("linkage"),
            "children": [],
        }
        while stack and not (
            stack[-1]["line_s"] <= node["line_s"]
            and node["line_e"] <= stack[-1]["line_e"]
            and (stack[-1]["line_s"], stack[-1]["line_e"]) != (node["line_s"], node["line_e"])
        ):
            stack.pop()
        (stack[-1]["children"] if stack else roots).append(node)
        if node["kind"] in _CONTAINERS and node["line_e"] > node["line_s"]:
            stack.append(node)

    return roots
