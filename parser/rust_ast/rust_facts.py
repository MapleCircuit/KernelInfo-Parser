"""parser/rust_ast/rust_facts.py - Extract trait implementations and definitions from Rust source text.

Extracts trait implementations (`impl Trait for Type`) into `m_blob_facts` Family 1 ops table format:
`t: [[trait, type, line, 0, [[method, method, line], ...]]]`, enabling driver ops navigation for Rust.
Also records method and function definitions into Family 1 definitions `d: [[name, line_s, line_e, flags], ...]`.
"""
from __future__ import annotations

import json
import re
import zlib
from typing import Any

from parser.rust_ast.rust_text import mask_code

FAMILY = 1
FACTS_FORMAT = 1

EMPTY: dict[str, list[Any]] = {"d": [], "c": [], "m": [], "t": [], "a": []}

_IMPL_TRAIT_RE = re.compile(
    r"\bimpl(?:<[^>{]*>)?\s+([A-Za-z0-9_:]+(?:<[^>{]*>)?)\s+for\s+([A-Za-z0-9_:]+(?:<[^>{]*>)?)\s*(?:where[^{]*)?\{"
)
_FN_RE = re.compile(r"\bfn\s+([A-Za-z0-9_]+)\b")


def encode(facts: dict[str, Any] | None) -> bytes:
    """Stored form of a blob's facts (`m_blob_facts.data`): zlib-compressed JSON."""
    return zlib.compress(json.dumps(facts if facts is not None else EMPTY, separators=(",", ":")).encode(), 3)


def decode(data: bytes | bytearray | memoryview | None) -> dict[str, Any] | None:
    """`encode()` reversed (None when bytes are invalid)."""
    if data is None:
        return None
    try:
        facts = json.loads(zlib.decompress(bytes(data)))
    except (zlib.error, ValueError):
        return None
    return facts if isinstance(facts, dict) else None


def _clean_type_name(name: str) -> str:
    """Strip generic arguments: 'Foo<T>' -> 'Foo', 'pci::Driver' -> 'pci::Driver'."""
    clean = re.sub(r"<[^>]*>", "", name).strip()
    return clean


def extract(text: str, masked: str | None = None) -> dict[str, list[Any]]:
    """Extract Rust trait implementations (Family 1 't') and function definitions (Family 1 'd').

    Returns:
        dict with:
            "d": [[name, line_s, line_e, flags], ...]
            "c": []
            "m": []
            "t": [[trait, type, line, 0, [[method, method, line], ...]], ...]
            "a": []
    """
    if not text or "impl" not in text and "fn " not in text:
        return EMPTY.copy()

    if masked is None:
        masked = mask_code(text)

    # Precompute newline offsets for fast 1-based line number calculation
    newlines = [i for i, c in enumerate(text) if c == "\n"]

    def line_num_of(offset: int) -> int:
        import bisect
        return bisect.bisect_right(newlines, offset) + 1

    d_items: list[list[Any]] = []
    t_items: list[list[Any]] = []

    # Find matching closing brace using masked text
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

    # Extract impl Trait for Type blocks
    for m in _IMPL_TRAIT_RE.finditer(masked):
        trait_raw = m.group(1).strip()
        type_raw = m.group(2).strip()
        trait_name = _clean_type_name(trait_raw)
        type_name = _clean_type_name(type_raw)
        impl_offset = m.start()
        impl_line = line_num_of(impl_offset)

        brace_start = m.end() - 1  # opening '{'
        brace_end = find_closing_brace(brace_start)
        impl_body = masked[brace_start + 1:brace_end]

        methods: list[list[Any]] = []
        # Parse methods directly inside the impl block
        depth = 0
        j = 0
        body_len = len(impl_body)
        while j < body_len:
            c = impl_body[j]
            if c == "{":
                depth += 1
                j += 1
            elif c == "}":
                depth -= 1
                j += 1
            elif depth == 0 and impl_body[j:j + 3] == "fn " and (j == 0 or not impl_body[j - 1].isalnum() and impl_body[j - 1] != "_"):
                fn_match = _FN_RE.match(impl_body, j)
                if fn_match:
                    fn_name = fn_match.group(1)
                    method_offset = brace_start + 1 + j
                    fn_line_s = line_num_of(method_offset)

                    fn_brace = impl_body.find("{", fn_match.end())
                    semicolon = impl_body.find(";", fn_match.end())
                    if fn_brace >= 0 and (semicolon < 0 or fn_brace < semicolon):
                        fn_close = find_closing_brace(brace_start + 1 + fn_brace)
                        fn_line_e = line_num_of(fn_close)
                        j = (fn_close + 1) - (brace_start + 1)
                    else:
                        fn_line_e = fn_line_s
                        j = (semicolon + 1) if semicolon >= 0 else fn_match.end()

                    methods.append([fn_name, fn_name, fn_line_s])
                    d_items.append([fn_name, fn_line_s, fn_line_e, 0])
                else:
                    j += 1
            else:
                j += 1

        if methods:
            t_items.append([trait_name, type_name, impl_line, 0, methods])

    # Extract any free functions outside impl
    for m in _FN_RE.finditer(masked):
        fn_name = m.group(1)
        # Avoid duplicate definition if already captured in methods
        fn_offset = m.start()
        fn_line_s = line_num_of(fn_offset)
        if any(d[0] == fn_name and d[1] == fn_line_s for d in d_items):
            continue
        fn_brace = masked.find("{", m.end())
        fn_semi = masked.find(";", m.end())
        if fn_brace >= 0 and (fn_semi < 0 or fn_brace < fn_semi):
            fn_close = find_closing_brace(fn_brace)
            fn_line_e = line_num_of(fn_close)
        else:
            fn_line_e = fn_line_s
        d_items.append([fn_name, fn_line_s, fn_line_e, 0])

    d_items.sort(key=lambda d: (d[1], d[2]))
    t_items.sort(key=lambda t: t[2])

    return {"d": d_items, "c": [], "m": [], "t": t_items, "a": []}
