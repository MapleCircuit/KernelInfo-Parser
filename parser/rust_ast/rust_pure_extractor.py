"""parser/rust_ast/rust_pure_extractor.py - Pure Rust Outline Extractor.

Free of TableEngine/GreatProcessor state. Extracts top-level and nested Rust definitions
from raw source bytes for WebApp outline Git fallback on unparsed revisions.
"""
from __future__ import annotations

import re
from typing import Any

from parser.rust_ast.rust_text import mask_code

# Construct patterns over masked code
_CONSTRUCT_RE = re.compile(
    r"\b(?P<kind>fn|struct|enum|union|trait|type|const|static|mod)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b|"
    r"\bmacro_rules!\s*(?P<macro>[A-Za-z_][A-Za-z0-9_]*)\b|"
    r"\bimpl(?:<[^>{]*>)?\s+(?P<impl>[A-Za-z0-9_:]+(?:<[^>{]*>)?(?:\s+for\s+[A-Za-z0-9_:]+(?:<[^>{]*>)?)?)\s*(?:where[^{]*)?\{|"
    r"(?P<maccall>[A-Za-z_][A-Za-z0-9_:]*!)\s*[\{\(]"
)

_KIND_MAP = {
    "fn": "Rust_Fn",
    "struct": "Rust_Struct",
    "enum": "Rust_Enum",
    "union": "Rust_Union",
    "trait": "Rust_Trait",
    "type": "Rust_Type",
    "const": "Rust_Const",
    "static": "Rust_Static",
    "mod": "Rust_Mod",
}

_OUTLINE_KIND = {
    "Rust_Fn": "function",
    "Rust_Struct": "type",
    "Rust_Enum": "type",
    "Rust_Union": "type",
    "Rust_Trait": "type",
    "Rust_Type": "type",
    "Rust_Impl": "type",
    "Rust_Mod": "type",
    "Rust_MacroDef": "macro",
    "Rust_MacroCall": "macro",
    "Rust_Const": "variable",
    "Rust_Static": "variable",
    "Rust_Variant": "constant",
    "Rust_Field": "constant",
    "Rust_ModuleAttr": "api",
}

_CONTAINER_KINDS = {"function", "type", "macro"}


def extract_rust_outline(source_bytes: bytes) -> list[dict[str, Any]]:
    """Extract symbol definitions from raw Rust file bytes into outline item dicts.

    Args:
        source_bytes: Raw bytes of the .rs file.

    Returns:
        List of nested dictionary items with keys:
            name, type_name, kind, line_s, line_e, linkage, children
    """
    if not source_bytes:
        return []

    try:
        text = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = source_bytes.decode("latin-1", errors="replace")

    masked = mask_code(text)
    newlines = [i for i, c in enumerate(text) if c == "\n"]

    def line_of(offset: int) -> int:
        import bisect
        return bisect.bisect_right(newlines, offset) + 1

    def find_closing_brace(start_brace_idx: int) -> int:
        depth = 1
        j = start_brace_idx + 1
        n = len(masked)
        while j < n and depth > 0:
            c = masked[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        return j - 1 if depth == 0 else n - 1

    flat_items: list[dict[str, Any]] = []

    for m in _CONSTRUCT_RE.finditer(masked):
        offset = m.start()
        line_s = line_of(offset)
        kind = m.group("kind")
        if kind:
            name = m.group("name")
            type_name = _KIND_MAP[kind]
            brace_pos = masked.find("{", m.end())
            semi_pos = masked.find(";", m.end())
            if brace_pos >= 0 and (semi_pos < 0 or brace_pos < semi_pos):
                close_pos = find_closing_brace(brace_pos)
                line_e = line_of(close_pos)
            elif semi_pos >= 0:
                line_e = line_of(semi_pos)
            else:
                line_e = line_s
        elif m.group("macro"):
            name = m.group("macro")
            type_name = "Rust_MacroDef"
            brace_pos = masked.find("{", m.end())
            semi_pos = masked.find(";", m.end())
            if brace_pos >= 0 and (semi_pos < 0 or brace_pos < semi_pos):
                close_pos = find_closing_brace(brace_pos)
                line_e = line_of(close_pos)
            elif semi_pos >= 0:
                line_e = line_of(semi_pos)
            else:
                line_e = line_s
        elif m.group("impl"):
            name = re.sub(r"\s+", " ", m.group("impl")).strip()
            type_name = "Rust_Impl"
            brace_pos = m.end() - 1
            close_pos = find_closing_brace(brace_pos)
            line_e = line_of(close_pos)
        elif m.group("maccall"):
            name = m.group("maccall")
            type_name = "Rust_MacroCall"
            # Only index top-level module/driver registration macros
            if not (name.startswith("module") or "::module" in name):
                continue
            open_char = masked[m.end() - 1]
            if open_char == "{":
                close_pos = find_closing_brace(m.end() - 1)
            else:
                close_pos = masked.find(")", m.end())
                if close_pos < 0:
                    close_pos = m.end()
            line_e = line_of(close_pos)
        else:
            continue

        o_kind = _OUTLINE_KIND.get(type_name, "other")
        flat_items.append({
            "name": name,
            "type_name": type_name,
            "kind": o_kind,
            "line_s": line_s,
            "line_e": max(line_s, line_e),
            "linkage": 0,
            "children": [],
        })

    # Sort strictly by line_s asc, line_e desc
    flat_items.sort(key=lambda it: (it["line_s"], -it["line_e"]))

    # Hierarchical nesting
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for it in flat_items:
        while stack and not (
            stack[-1]["line_s"] <= it["line_s"]
            and it["line_e"] <= stack[-1]["line_e"]
            and (stack[-1]["line_s"], stack[-1]["line_e"]) != (it["line_s"], it["line_e"])
        ):
            stack.pop()
        (stack[-1]["children"] if stack else roots).append(it)
        if it["kind"] in _CONTAINER_KINDS and it["line_e"] > it["line_s"]:
            stack.append(it)

    return roots
