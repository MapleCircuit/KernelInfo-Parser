"""parser/asm_ast/asm_pure_extractor.py - Pure-Python Assembly Outline Extractor.

Free of TableEngine, GreatProcessor, or Database driver dependencies.
Used by webapp backend (OutlineService) as a fast "git" fallback for unparsed assembly blobs.
"""
from __future__ import annotations

import re
from typing import Any

from core.globalstuff import SymbolLinkage

_START_MACRO = re.compile(
    r"^[ \t]*(?:SYM_FUNC_START\w*|SYM_CODE_START\w*|SYM_TYPED_FUNC_START|ENTRY|GLOBAL|KENTRY)\s*\(\s*([A-Za-z0-9_]+)\s*\)"
)
_END_MACRO = re.compile(
    r"^[ \t]*(?:SYM_FUNC_END\w*|SYM_CODE_END\w*|SYM_DATA_END\w*|ENDPROC|END)\s*\(\s*([A-Za-z0-9_]+)\s*\)"
)
_MACRO_START = re.compile(r"^[ \t]*\.macro\s+([A-Za-z0-9_]+)")
_MACRO_END = re.compile(r"^[ \t]*\.endm\b")
_LABEL = re.compile(r"^[ \t]*([A-Za-z_]\w*):")
_STRIP_COMMENT = re.compile(
    r"/\*.*?\*/|//.*$|#[ \t]*(?!include|define|undef|if|ifdef|ifndef|else|elif|endif|pragma).*$",
    re.DOTALL,
)


def extract_asm_outline(source_bytes: bytes) -> list[dict[str, Any]]:
    """Extract outline items (functions, macros, labels) from raw assembly source bytes."""
    if not source_bytes:
        return []

    code = source_bytes.decode("latin-1", errors="replace")
    raw_lines = code.replace("\r\n", "\n").split("\n")

    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []

    def push(item: dict[str, Any]) -> None:
        if stack:
            stack[-1]["children"].append(item)
        else:
            roots.append(item)

    in_multiline_comment = False

    for idx, raw in enumerate(raw_lines):
        line = idx + 1
        line_clean = raw

        # Handle multi-line /* ... */
        if in_multiline_comment:
            end_pos = line_clean.find("*/")
            if end_pos != -1:
                in_multiline_comment = False
                line_clean = line_clean[end_pos + 2:]
            else:
                continue

        while "/*" in line_clean:
            start_pos = line_clean.find("/*")
            end_pos = line_clean.find("*/", start_pos + 2)
            if end_pos != -1:
                line_clean = line_clean[:start_pos] + " " + line_clean[end_pos + 2:]
            else:
                in_multiline_comment = True
                line_clean = line_clean[:start_pos]
                break

        text = _STRIP_COMMENT.sub("", line_clean).strip()
        if not text:
            continue

        # 1. Function start: SYM_FUNC_START*(name) or ENTRY(name)
        m = _START_MACRO.match(text)
        if m:
            sym_name = m.group(1)
            macro_token = text.split("(", 1)[0].strip()
            if "LOCAL" in macro_token:
                linkage = SymbolLinkage.InternalStaticDef
            elif "WEAK" in macro_token or macro_token == "WEAK":
                linkage = SymbolLinkage.WeakOrAliasDef
            else:
                linkage = SymbolLinkage.ExternalDef
            item = {
                "name": sym_name,
                "kind": "function",
                "type_name": "ASM_Label",
                "line_s": line,
                "line_e": line,
                "linkage": int(linkage),
                "children": [],
            }
            push(item)
            stack.append(item)
            continue

        # 2. Function end: SYM_FUNC_END*(name) or ENDPROC(name) or END(name)
        m = _END_MACRO.match(text)
        if m:
            sym_name = m.group(1)
            for i in range(len(stack) - 1, -1, -1):
                if stack[i]["name"] == sym_name:
                    stack[i]["line_e"] = line
                    del stack[i:]
                    break
            continue

        # 3. Macro start: .macro name
        m = _MACRO_START.match(text)
        if m:
            macro_name = m.group(1)
            item = {
                "name": macro_name,
                "kind": "macro",
                "type_name": "ASM_Macro",
                "line_s": line,
                "line_e": line,
                "linkage": int(SymbolLinkage.MacroOrKernelDef),
                "children": [],
            }
            push(item)
            stack.append(item)
            continue

        # 4. Macro end: .endm
        if _MACRO_END.match(text):
            if stack and stack[-1]["type_name"] == "ASM_Macro":
                stack[-1]["line_e"] = line
                stack.pop()
            continue

        # 5. Label: name:
        m = _LABEL.match(text)
        if m:
            lbl_name = m.group(1)
            if not lbl_name.isdigit() and not (lbl_name.startswith("L") or lbl_name.startswith(".L")):
                item = {
                    "name": lbl_name,
                    "kind": "label",
                    "type_name": "ASM_Label",
                    "line_s": line,
                    "line_e": line,
                    "linkage": int(SymbolLinkage.InternalStaticDef),
                    "children": [],
                }
                push(item)

    return roots
