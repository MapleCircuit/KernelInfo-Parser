"""parser/rust_ast/rust_tree_parser.py - AST-Tree Parser for rustc Output.

Reads `rustc -Z unpretty=ast-tree` output (parser/rust_ast/RUST_API.md section 1) into `Ast_Rust_*` trees:

1. `strip_unused_blocks()` drops the dump's token streams, function bodies and initializer expressions (about
   60% of it, never used) by indentation, and the file's path in every span is shortened to `@`.
2. `RustAstTreeTokenizer` / `RustAstTreeParser` turn the rest into dicts (`_type`, `_items`, fields); a span
   becomes a `Span` of rustc's (line, char column) numbers.
3. `RustTreeBuilder` converts items. Display text (types, patterns, generics, bounds, use trees, impl headers,
   discriminants, attributes) is sliced from the source at the node's span instead of being re-rendered from
   the dump, so it is exact and only depends on the `span` keys, which every rustc version prints. rustc
   counts columns in characters; `RustSource` maps them to the byte columns stored everywhere (the file is
   read as latin-1, like every parser), slicing with an exclusive end.
4. Outer doc comments and attributes of top-level items, crate-level `//!` / `#![...]` and the plain comments
   between items become their own nodes, so every non-blank line of the file is tagged.
5. From the source text (parser/rust_ast/rust_text.py): the entries of module macros (`module! { ... }`) and
   the uses of C symbols through bindgen (`bindings::foo`) in every top-level item.
"""
from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from typing import Any

from core.globalstuff import ASTT, SymbolLinkage, SymbolRole
from parser.rust_ast.rust_text import MacroField, macro_fields, mask_code, scan_binding_refs
from parser.rust_ast.rust_ast_type import (
    Line,
    Ast_Rust,
    Ast_Rust_Attribute,
    Ast_Rust_Comment,
    Ast_Rust_Struct,
    Ast_Rust_Field,
    Ast_Rust_Enum,
    Ast_Rust_Variant,
    Ast_Rust_Union,
    Ast_Rust_Fn,
    Ast_Rust_Param,
    Ast_Rust_Trait,
    Ast_Rust_TraitAlias,
    Ast_Rust_Impl,
    Ast_Rust_Type,
    Ast_Rust_Const,
    Ast_Rust_Static,
    Ast_Rust_Mod,
    Ast_Rust_Use,
    Ast_Rust_MacroDef,
    Ast_Rust_MacroCall,
    Ast_Rust_ModuleAttr,
    Ast_Rust_ForeignMod,
    Ast_Rust_DocComment,
)

# ----------------------------------------------------------------------------------------------------------------
# Dump pre-processing
# ----------------------------------------------------------------------------------------------------------------
# A block opened at the end of its line and closed by `)` at the same indentation (rustc's `{:#?}` layout)
_UNUSED_BLOCK_RE = re.compile(r"\n( *)(tokens|body|expr): (?:Some|TokenStream)\(\n")
_FIRST_SPAN_RE = re.compile(r"span: (\S.*?):\d+:\d+: \d+:\d+ \(#\d+\)")
SPAN_FILE = "@"


def strip_unused_blocks(text: str) -> str:
    """Replace token streams, function bodies and initializers with the marker `Stripped` (a body is known to exist)."""
    out: list[str] = []
    pos = 0
    search = _UNUSED_BLOCK_RE.search
    find = text.find
    while True:
        m = search(text, pos)
        if m is None:
            out.append(text[pos:])
            break
        indent = m.group(1)
        end = find("\n" + indent + ")", m.end())
        if end < 0:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        out.append(f"\n{indent}{m.group(2)}: Stripped,")
        nl = find("\n", end + 1)
        pos = nl if nl >= 0 else len(text)
    return "".join(out)


def shorten_span_paths(text: str, path: str | None = None) -> str:
    """Replace the parsed file's path in every span with `@` (shorter dump, path-independent span tokens)."""
    if path is None:
        m = _FIRST_SPAN_RE.search(text)
        if m is None:
            return text
        path = m.group(1)
    return text.replace(f"{path}:", f"{SPAN_FILE}:")


# ----------------------------------------------------------------------------------------------------------------
# Spans and source text
# ----------------------------------------------------------------------------------------------------------------
class Span(tuple):
    """rustc span (line_s, char_col_s, line_e, char_col_e): 1-based, character columns, exclusive end."""

    __slots__ = ()

    @property
    def start(self) -> tuple[int, int]:
        return (self[0], self[1])

    @property
    def end(self) -> tuple[int, int]:
        return (self[2], self[3])


_SPAN_NUMS_RE = re.compile(r":(\d+):(\d+): (\d+):(\d+)")


def parse_span_coords(span_str: str) -> tuple[int, int, int, int]:
    """Extract (line_s, line_e, char_s, char_e) from a rustc span string."""
    m = re.search(r":(\d+):(\d+):\s*(\d+):(\d+)", span_str)
    if m:
        ls, cs, le, ce = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return ls, le, cs, ce
    m = re.search(r":(\d+):(\d+):\s*(\d+)", span_str)
    if m:
        ls, cs, ce = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return ls, ls, cs, ce
    m = re.search(r":(\d+):(\d+)", span_str)
    if m:
        ls, cs = int(m.group(1)), int(m.group(2))
        return ls, ls, cs, cs
    return 1, 1, 1, 1


def _to_span(span_str: str) -> Span:
    m = _SPAN_NUMS_RE.search(span_str)
    if m:
        return Span((int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))))
    ls, le, cs, ce = parse_span_coords(span_str)
    return Span((ls, cs, le, ce))


def extract_code_snippet(raw_lines: tuple[str, ...], ls: int, le: int, cs: int, ce: int) -> str:
    """Source text between 1-based (line, column) positions, the end column exclusive."""
    if not raw_lines:
        return ""
    total = len(raw_lines)
    idx_s = max(0, min(total - 1, ls - 1))
    idx_e = max(0, min(total - 1, le - 1))

    if idx_s == idx_e:
        line = raw_lines[idx_s]
        c_start = max(0, min(len(line), cs - 1))
        c_end = max(c_start, min(len(line), ce - 1))
        return line[c_start:c_end]
    parts = []
    for i in range(idx_s, idx_e + 1):
        line = raw_lines[i]
        if i == idx_s:
            parts.append(line[max(0, min(len(line), cs - 1)):])
        elif i == idx_e:
            parts.append(line[:max(0, min(len(line), ce - 1))])
        else:
            parts.append(line)
    return "\n".join(parts)


class RustSource:
    """The parsed file's lines (latin-1, one character per byte) and rustc character-to-byte column mapping."""

    def __init__(self, raw_lines: tuple[str, ...]) -> None:
        self.lines = raw_lines
        self._offsets: dict[int, list[int]] = {}
        # Single-pass source masking: mask comments and strings once for the entire source
        self.masked_lines: tuple[str, ...] = tuple(mask_code("\n".join(raw_lines)).split("\n"))

    def slice_masked(self, ls: int, le: int, cs: int, ce: int) -> str:
        """Extract slice from pre-masked lines without repeating full-file or per-node mask operations."""
        return extract_code_snippet(self.masked_lines, ls, le, cs, ce)

    def byte_col(self, line: int, char_col: int) -> int:
        """1-based byte column of rustc's 1-based character column `char_col` on `line`."""
        idx = line - 1
        if not 0 <= idx < len(self.lines):
            return char_col
        raw = self.lines[idx]
        if raw.isascii():
            return char_col
        offsets = self._offsets.get(idx)
        if offsets is None:
            text = raw.encode("latin-1").decode("utf-8", errors="replace")
            offsets = [0]
            total = 0
            for ch in text:
                total += len(ch.encode("utf-8", errors="replace"))
                offsets.append(total)
            self._offsets[idx] = offsets
        ci = char_col - 1
        if ci < len(offsets):
            return offsets[ci] + 1
        return offsets[-1] + (ci - len(offsets) + 1) + 1

    def bytes_of(self, span: Span) -> tuple[int, int, int, int]:
        """(line_s, line_e, byte_col_s, byte_col_e) of a span."""
        return span[0], span[2], self.byte_col(span[0], span[1]), self.byte_col(span[2], span[3])

    def text(self, start: tuple[int, int], end: tuple[int, int]) -> str:
        """Source text from rustc position `start` to `end` (exclusive)."""
        if end < start:
            return ""
        return extract_code_snippet(
            self.lines, start[0], end[0], self.byte_col(start[0], start[1]), self.byte_col(end[0], end[1])
        )

    def span_text(self, span: Any) -> str:
        return self.text(span.start, span.end) if isinstance(span, Span) else ""

    def extent(self, span: Any) -> Line:
        if not isinstance(span, Span):
            return Line(1, 1, 1, 1, "")
        ls, le, cs, ce = self.bytes_of(span)
        return Line(ls, le, cs, ce, extract_code_snippet(self.lines, ls, le, cs, ce))


def norm(text: str) -> str:
    """Whitespace-normalized text (a multi-line type or header on one line)."""
    return " ".join(text.split())


def make_line_extent(span_node: Any, raw_lines: tuple[str, ...]) -> Line:
    """Line extent (byte columns, exclusive end) and source text of a span (a `Span`, its dict or its string)."""
    if isinstance(span_node, dict):
        span_node = span_node.get("span")
    if isinstance(span_node, str):
        span_node = _to_span(span_node)
    return RustSource(raw_lines).extent(span_node)


# ----------------------------------------------------------------------------------------------------------------
# Tokenizer / generic parser
# ----------------------------------------------------------------------------------------------------------------
def unquote_string(token: str) -> str:
    """Decode a quoted STRING token from rustc's AST dump into its text.

    The escapes rustc emits (`\\n`, `\\t`, `\\"`, `\\\\`, `\\x41`, `\\u{1F600}`) are decoded here
    instead of by evaluating the token as a Python literal: the token comes from a subprocess, so it
    is data, and `unicode_escape` would also mangle non-ASCII bytes.
    """
    body = token[1:-1] if len(token) >= 2 and token.startswith('"') else token
    if "\\" not in body:
        return body
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(body):
            out.append("\\")
            break
        esc = body[i]
        i += 1
        if esc in _RUST_SIMPLE_ESCAPES:
            out.append(_RUST_SIMPLE_ESCAPES[esc])
        elif esc == "x" and i + 2 <= len(body):
            out.append(_chr_or_raw(body[i:i + 2], f"\\x{body[i:i + 2]}"))
            i += 2
        elif esc == "u" and body[i:i + 1] == "{" and "}" in body[i:]:
            end = body.index("}", i)
            out.append(_chr_or_raw(body[i + 1:end], f"\\u{{{body[i + 1:end]}}}"))
            i = end + 1
        else:
            # Unknown escape: keep it verbatim rather than dropping characters
            out.append("\\" + esc)
    return "".join(out)


_RUST_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "'": "'", "\\": "\\"}


def _chr_or_raw(hex_digits: str, fallback: str) -> str:
    """Character for a hex escape's digits, or the escape verbatim when they are not valid."""
    try:
        return chr(int(hex_digits, 16))
    except (ValueError, OverflowError):
        return fallback


class RustAstTreeTokenizer:
    """Lazy regular-expression tokenizer for rustc AST debug output."""

    # Leading whitespace is consumed by the match itself: a search failing at every indentation space is 9x slower
    TOKEN_RE = re.compile(
        r'\s*(?:'
        r'(?P<STRING>"(?:\\.|[^"\\])*")|'
        r'(?P<SPAN>(?:@|/[^:\s]+):\d+:\d+: ?\d+:\d+(?: \(#\d+\))?)|'
        r'(?P<LBRACE>\{)|(?P<RBRACE>\})|'
        r'(?P<LBRACKET>\[)|(?P<RBRACKET>\])|'
        r'(?P<LPAREN>\()|(?P<RPAREN>\))|'
        r'(?P<DOUBLE_COLON>::)|(?P<COLON>:)|(?P<COMMA>,)|'
        r'(?P<IDENT>[a-zA-Z_#][a-zA-Z0-9_#$]*)|'
        r'(?P<NUMBER>-?\d+(?:\.\d+)?))'
    )

    __slots__ = ("_it", "_cur")

    def __init__(self, text: str) -> None:
        self._it = self.TOKEN_RE.finditer(text)
        self._cur: tuple[str | None, str | None] = (None, None)
        self.next()

    def peek(self) -> tuple[str | None, str | None]:
        return self._cur

    def next(self) -> tuple[str | None, str | None]:
        tok = self._cur
        m = next(self._it, None)
        if m is None:
            self._cur = (None, None)
        else:
            kind = m.lastgroup
            self._cur = (kind, m.group(kind))
        return tok


class RustAstTreeParser:
    """Recursive-descent parser converting AST debug tokens into Python objects."""

    def __init__(self, tokenizer: RustAstTreeTokenizer) -> None:
        self.tok = tokenizer

    def parse(self) -> Any:
        return self.parse_value()

    def parse_value(self) -> Any:
        tok = self.tok
        kind, val = tok.peek()
        if kind is None:
            return None
        if kind == "IDENT":
            tok.next()
            while tok.peek()[0] == "DOUBLE_COLON":
                tok.next()
                if tok.peek()[0] == "IDENT":
                    val = f"{val}::{tok.next()[1]}"
            next_kind = tok.peek()[0]
            if next_kind == "LBRACE":
                return self.parse_dict(val)
            if next_kind == "LPAREN":
                return self.parse_tuple(val)
            return val
        if kind == "SPAN":
            tok.next()
            return _to_span(val)
        if kind == "STRING":
            tok.next()
            return unquote_string(val)
        if kind == "NUMBER":
            tok.next()
            return int(val) if "." not in val else float(val)
        if kind == "LBRACKET":
            return self.parse_list()
        if kind == "LBRACE":
            return self.parse_dict(None)
        if kind == "LPAREN":
            # Anonymous tuple, e.g. `(UseTree { .. }, NodeId(..))` in a nested use tree
            return self.parse_tuple(None)
        tok.next()
        return val

    def _parse_sequence(self, close: str) -> list[Any]:
        tok = self.tok
        tok.next()  # consume the opening delimiter
        items = []
        while True:
            kind = tok.peek()[0]
            if kind is None:
                break
            if kind == close:
                tok.next()
                break
            if kind == "COMMA":
                tok.next()
                continue
            items.append(self.parse_value())
        return items

    def parse_list(self) -> list[Any]:
        return self._parse_sequence("RBRACKET")

    def parse_tuple(self, type_name: str | None) -> dict[str, Any]:
        return {"_type": type_name, "_items": self._parse_sequence("RPAREN")}

    def parse_dict(self, type_name: str | None) -> dict[str, Any]:
        tok = self.tok
        tok.next()  # consume {
        d: dict[str, Any] = {"_type": type_name} if type_name else {}
        while True:
            kind, val = tok.peek()
            if kind is None:
                break
            if kind == "RBRACE":
                tok.next()
                break
            if kind == "IDENT":
                tok.next()
                if tok.peek()[0] == "COLON":
                    tok.next()
                    d[val] = self.parse_value()
                else:
                    d[val] = True
            else:
                tok.next()
        return d


# ----------------------------------------------------------------------------------------------------------------
# Dump shape helpers (tolerant of rustc layout changes)
# ----------------------------------------------------------------------------------------------------------------
def _type(v: Any) -> str | None:
    if isinstance(v, dict):
        return v.get("_type")
    return v if isinstance(v, str) else None


def _items(v: Any) -> list[Any]:
    if isinstance(v, dict):
        items = v.get("_items")
        return items if isinstance(items, list) else []
    return []


def _some(v: Any) -> Any:
    """`Some(x)` -> x, `None` -> None, anything else unchanged."""
    if isinstance(v, dict) and v.get("_type") == "Some":
        items = v.get("_items") or []
        return items[0] if items else None
    if v == "None":
        return None
    return v


def _payload(kind_obj: Any) -> dict[str, Any]:
    """The struct inside an ItemKind tuple (`Fn(Fn { .. })`, `Impl(Impl { .. })`), or the kind itself."""
    for it in _items(kind_obj):
        if isinstance(it, dict) and it.get("_type") not in ("Some", None) and ("ident" in it or len(it) > 2):
            return it
    return kind_obj if isinstance(kind_obj, dict) else {}


def clean_ident(ident_val: Any) -> str:
    """Identifier without its hygiene suffix (`foo#0` -> `foo`, `r#type#0` -> `r#type`) or quotes."""
    ident_val = _some(ident_val)
    if not isinstance(ident_val, str):
        return ""
    ident_val = ident_val.strip()
    if ident_val.startswith('"') and ident_val.endswith('"'):
        ident_val = ident_val[1:-1]
    head, sep, tail = ident_val.rpartition("#")
    if sep and tail.isdigit():
        ident_val = head
    return "" if ident_val == "None" else ident_val


def _bare(ident: str) -> str:
    """Identifier without a raw prefix (`r#type` -> `type`), the name searched for."""
    return ident[2:] if ident.startswith("r#") else ident


def _find_ident(item: dict[str, Any], kind_obj: Any) -> str:
    """The item's identifier: on the item (older rustc) or in its kind (`Struct(Foo#0, ..)`, `Fn { ident }`)."""
    ident = clean_ident(item.get("ident"))
    if ident:
        return ident
    for it in _items(kind_obj):
        if isinstance(it, str) and "#" in it:
            return clean_ident(it)
        if isinstance(it, dict) and it.get("ident"):
            return clean_ident(it["ident"])
    return ""


def _spans_in(node: Any):
    stack = [node]
    while stack:
        v = stack.pop()
        if isinstance(v, Span):
            yield v
        elif isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)


def _cover(node: Any) -> Span | None:
    """Smallest span covering every non-empty span inside `node`."""
    start = end = None
    for s in _spans_in(node):
        if s.start == s.end:
            continue
        if start is None or s.start < start:
            start = s.start
        if end is None or s.end > end:
            end = s.end
    return Span(start + end) if start is not None else None


def _later(*positions: Any) -> tuple[int, int] | None:
    found = [p for p in positions if p is not None]
    return max(found) if found else None


def extract_visibility(vis_node: Any, src: RustSource | None = None) -> str:
    """Visibility as written (`pub`, `pub(crate)`, `pub(super)`, `pub(in crate::x)`), "" when private."""
    if not isinstance(vis_node, dict):
        return ""
    kind = vis_node.get("kind")
    if kind == "Inherited" or kind is None:
        return ""
    if src is not None:
        text = norm(src.span_text(vis_node.get("span")))
        if text:
            return text
    if kind == "Public":
        return "pub"
    if isinstance(kind, dict) and kind.get("_type") == "Restricted":
        return "pub(crate)"
    return ""


# ----------------------------------------------------------------------------------------------------------------
# Builder
# ----------------------------------------------------------------------------------------------------------------
_PATH_ATTR_RE = re.compile(r'^#\[\s*path\s*=\s*(?:r#*|)"([^"]*)"#*\s*\]$')
_MOD_RS_NAMES = ("mod.rs", "lib.rs", "main.rs")
# Attributes giving an item a C symbol: `#[no_mangle]` / `#[unsafe(no_mangle)]`, the kernel's `#[export]` (a
# `#[no_mangle]` checked against the C prototype) and `#[export_name = "x"]` / `#[unsafe(export_name = "x")]`
_NO_MANGLE_RE = re.compile(r"^#\[\s*(?:unsafe\s*\(\s*)?(?:no_mangle|export)\s*\)?\s*\]$")
_EXPORT_NAME_RE = re.compile(r'^#\[\s*(?:unsafe\s*\(\s*)?export_name\s*=\s*("(?:[^"\\]|\\.)*")\s*\)?\s*\]$')
# Node types whose text holds no code (no bindgen use is looked for in them)
_TEXT_ONLY_TYPES = frozenset({ASTT.Rust_Comment, ASTT.Rust_DocComment, ASTT.Rust_Attribute})


def c_symbol_of(attributes: list[str], ident: str) -> str | None:
    """The symbol an item is linked under for C (from its attribute texts), None when it is mangled."""
    for text in attributes:
        if _NO_MANGLE_RE.match(text):
            return ident
        m = _EXPORT_NAME_RE.match(text)
        if m:
            return unquote_string(m.group(1)) or ident
    return None


def is_module_macro(path: str) -> bool:
    """`module!` and the kernel's module-declaring macros (`kernel::module_platform_driver!`, ...)."""
    last = path.rsplit("::", 1)[-1].strip()
    return last == "module" or last.startswith("module_")


def is_driver_macro(path: str) -> bool:
    """Kernel driver registration macros (`module_pci_driver!`, `module_platform_driver!`, ...)."""
    last = path.rsplit("::", 1)[-1].strip()
    return last.startswith("module_") and "driver" in last


class _Ctx:
    """Where an item sits: its module's directory (for `mod x;`) and whether it is a top-level item."""

    __slots__ = ("mod_dir", "file_dir", "nested", "in_trait", "in_foreign")

    def __init__(self, mod_dir: str, file_dir: str, nested: bool = False, in_trait: bool = False, in_foreign: bool = False) -> None:
        self.mod_dir = mod_dir
        self.file_dir = file_dir
        self.nested = nested
        self.in_trait = in_trait
        self.in_foreign = in_foreign

    def child(self, mod_dir: str | None = None, in_trait: bool = False, in_foreign: bool = False) -> _Ctx:
        return _Ctx(self.mod_dir if mod_dir is None else mod_dir, self.file_dir, True, in_trait, in_foreign)


class RustTreeBuilder:
    """Converts the parsed `Crate` dict into `Ast_Rust` nodes (see the module docstring)."""

    def __init__(
        self,
        raw_lines: tuple[str, ...],
        rel_path: str | None = None,
        file_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self.src = RustSource(raw_lines)
        self.rel_path = (rel_path or "").strip("/")
        self.file_exists = file_exists

    # -- entry -------------------------------------------------------------------------------------------------
    def build(self, crate: dict[str, Any]) -> list[Ast_Rust]:
        nodes: list[Ast_Rust] = []
        nodes.extend(self._attr_nodes(crate.get("attrs", []), standalone=True))

        file_dir = posixpath.dirname(self.rel_path)
        base = posixpath.basename(self.rel_path)
        mod_dir = file_dir if base in _MOD_RS_NAMES else posixpath.join(file_dir, base[:-3] if base.endswith(".rs") else base)
        ctx = _Ctx(mod_dir, file_dir)

        covered: list[Line] = []
        for item in crate.get("items", []):
            if not isinstance(item, dict):
                continue
            attr_nodes = self._attr_nodes(item.get("attrs", []), standalone=True)
            nodes.extend(attr_nodes)
            node = self.convert_item(item, ctx)
            if node is not None:
                nodes.append(node)
            elif isinstance(item.get("span"), Span):
                covered.append(self.src.extent(item["span"]))
        covered.extend(n.extent for n in nodes)
        for node in nodes:
            if node.ast_type not in _TEXT_ONLY_TYPES:
                existing = list(node.binding_refs)
                masked_code = self.src.slice_masked(node.extent.line_pos[0], node.extent.line_pos[1], node.extent.char_pos[0], node.extent.char_pos[1])
                refs = self._binding_refs(node.extent, masked=masked_code)
                if existing:
                    refs.extend(existing)
                    refs.sort(key=lambda r: (r[2], r[3]))
                node.binding_refs = refs
        nodes.extend(scan_comments(self.src.lines, covered))
        nodes.sort(key=lambda n: (n.extent.line_pos[0], n.extent.char_pos[0]))
        return nodes

    # -- attributes --------------------------------------------------------------------------------------------
    def _attr_nodes(self, attrs: Any, standalone: bool) -> list[Ast_Rust]:
        """Attributes as nodes: with `standalone`, doc blocks and attributes (each tagged on its own); else
        only the non-doc attributes, stored as children of a nested item."""
        out: list[Ast_Rust] = []
        block: list[tuple[Line, str, bool]] = []

        def flush() -> None:
            if not block:
                return
            ext = Line(block[0][0].line_pos[0], block[-1][0].line_pos[1], block[0][0].char_pos[0], block[-1][0].char_pos[1])
            ext.code = extract_code_snippet(self.src.lines, ext.line_pos[0], ext.line_pos[1], ext.char_pos[0], ext.char_pos[1])
            out.append(Ast_Rust_DocComment("\n".join(text.strip() for _e, text, _i in block), ext, is_inner=block[0][2]))
            block.clear()

        for attr in attrs if isinstance(attrs, list) else ():
            if not isinstance(attr, dict) or not isinstance(attr.get("span"), Span):
                continue
            kind = attr.get("kind")
            is_inner = attr.get("style") == "Inner"
            ext = self.src.extent(attr["span"])
            if _type(kind) == "DocComment":
                if not standalone:
                    continue
                doc_items = _items(kind)
                text = doc_items[1] if len(doc_items) > 1 and isinstance(doc_items[1], str) else ""
                if block and (block[-1][2] != is_inner or ext.line_pos[0] > block[-1][0].line_pos[1] + 1):
                    flush()
                block.append((ext, text, is_inner))
                continue
            flush()
            out.append(Ast_Rust_Attribute(norm(ext.code), ext, is_inner=is_inner))
        flush()
        return out

    def _attr_texts(self, attrs: Any) -> tuple[list[str], list[str]]:
        docs: list[str] = []
        texts: list[str] = []
        for attr in attrs if isinstance(attrs, list) else ():
            if not isinstance(attr, dict):
                continue
            kind = attr.get("kind")
            if _type(kind) == "DocComment":
                doc_items = _items(kind)
                if len(doc_items) > 1 and isinstance(doc_items[1], str):
                    docs.append(doc_items[1].strip())
            elif isinstance(attr.get("span"), Span):
                texts.append(norm(self.src.span_text(attr["span"])))
        return docs, texts

    # -- text helpers ------------------------------------------------------------------------------------------
    def _text(self, node: Any) -> str:
        """Normalized source text of a node with a `span` (or of a Span)."""
        span = node if isinstance(node, Span) else (node.get("span") if isinstance(node, dict) else None)
        return norm(self.src.span_text(span))

    def _ty(self, ty: Any) -> str:
        ty = _some(ty)
        if isinstance(ty, dict) and not isinstance(ty.get("span"), Span) and _items(ty):
            ty = _items(ty)[0]  # e.g. FnRetTy::Ty(Ty { .. })
        return self._text(ty) if isinstance(ty, dict) else ""

    def _generics(self, generics: Any) -> tuple[str, tuple[int, int] | None]:
        """Generic parameter text (`<'a, T: Clone>`) and the end of the generics / where clause."""
        if not isinstance(generics, dict):
            return "", None
        span = generics.get("span")
        text = self._text(span) if isinstance(span, Span) and span.start != span.end else ""
        end = span.end if isinstance(span, Span) else None
        where = generics.get("where_clause")
        if isinstance(where, dict) and where.get("has_where_token") == "true" and isinstance(where.get("span"), Span):
            end = _later(end, where["span"].end)
        return text, end

    def _header(self, item_span: Any, end: tuple[int, int] | None) -> str:
        if not isinstance(item_span, Span) or end is None:
            return ""
        return norm(self.src.text(item_span.start, min(end, item_span.end))).rstrip(";").rstrip()

    def _linkage(self, vis: str, ctx: _Ctx) -> int:
        if ctx.in_foreign:
            return SymbolLinkage.PrototypeDecl  # declared here, defined elsewhere (C)
        if ctx.in_trait or vis.startswith("pub"):
            return SymbolLinkage.ExternalDef
        return SymbolLinkage.InternalStaticDef

    def _fields(self, fields: Any, ctx: _Ctx) -> list[Ast_Rust_Field]:
        out = []
        for idx, f in enumerate(fields if isinstance(fields, list) else ()):
            if not isinstance(f, dict):
                continue
            out.append(Ast_Rust_Field(
                clean_ident(f.get("ident")),
                self._ty(f.get("ty")),
                self.src.extent(f.get("span")),
                vis=extract_visibility(f.get("vis"), self.src),
                priority=idx,
                attr_nodes=self._attr_nodes(f.get("attrs", []), standalone=False),
            ))
        return out

    def _variant_fields(self, data: Any, ctx: _Ctx) -> list[Ast_Rust_Field]:
        """Fields of a VariantData: `Struct { fields, .. }`, `Tuple([fields], id)` or `Unit(id)`."""
        if not isinstance(data, dict):
            return []
        if isinstance(data.get("fields"), list):
            return self._fields(data["fields"], ctx)
        items = _items(data)
        if data.get("_type") == "Tuple" and items and isinstance(items[0], list):
            return self._fields(items[0], ctx)
        return []

    # -- source-text scans (parser/rust_ast/rust_text.py) -------------------------------------------------------
    @staticmethod
    def _position(ext: Line, code: str, offset: int) -> tuple[int, int]:
        """(line, byte column) of `offset` in `code`, the text of `ext`."""
        nl = code.rfind("\n", 0, offset)
        line = ext.line_pos[0] + code.count("\n", 0, offset)
        return line, (ext.char_pos[0] + offset if nl < 0 else offset - nl)

    def _binding_refs(self, ext: Line, masked: str | None = None) -> list[tuple[str, int, int, int]]:
        code = ext.code
        if masked is None and hasattr(self, "src"):
            masked = self.src.slice_masked(ext.line_pos[0], ext.line_pos[1], ext.char_pos[0], ext.char_pos[1])
        return [(r.name, r.role, *self._position(ext, code, r.offset)) for r in scan_binding_refs(code, masked=masked)]

    def _module_attrs(self, ext: Line, fields: list[MacroField], in_params: bool = False) -> list[Ast_Rust_ModuleAttr]:
        out = []
        for f in fields:
            value = f.value
            if f.children:
                brace = value.find("{")
                value = value[:brace].strip() if brace >= 0 else value
            if not in_params and f.key == "name" and value.startswith('"'):
                def_name = unquote_string(value) or None
            else:
                def_name = f.key if in_params else None
            ls, cs = self._position(ext, ext.code, f.start)
            le, ce = self._position(ext, ext.code, f.end)
            out.append(Ast_Rust_ModuleAttr(
                f.key, value, Line(ls, le, cs, ce, ext.code[f.start:f.end]),
                children=self._module_attrs(ext, f.children, in_params=f.key == "params" and not in_params),
                def_name=def_name,
            ))
        return out

    # -- items -------------------------------------------------------------------------------------------------
    def convert_item(self, item: dict[str, Any], ctx: _Ctx) -> Ast_Rust | None:
        """Convert an Item (top-level, associated or foreign) into an Ast_Rust construct."""
        if not isinstance(item, dict):
            return None
        kind_obj = item.get("kind", {})
        kind_type = _type(kind_obj)
        ident = _find_ident(item, kind_obj)
        item_span = item.get("span")
        extent = self.src.extent(item_span)
        vis = extract_visibility(item.get("vis"), self.src)
        doc_comments, attributes = self._attr_texts(item.get("attrs", []))
        attr_nodes = self._attr_nodes(item.get("attrs", []), standalone=False) if ctx.nested else []
        common = {"doc_comments": doc_comments, "attributes": attributes, "attr_nodes": attr_nodes}
        linkage = self._linkage(vis, ctx)
        data = _payload(kind_obj)
        kind_items = _items(kind_obj)

        if kind_type in ("Struct", "Union"):
            generics = next((it for it in kind_items if isinstance(it, dict) and it.get("_type") == "Generics"), data.get("generics"))
            gen_text, gen_end = self._generics(generics)
            variant_data = next(
                (it for it in kind_items if isinstance(it, dict) and it.get("_type") in ("Struct", "Tuple", "Unit")),
                data,
            )
            cls = Ast_Rust_Struct if kind_type == "Struct" else Ast_Rust_Union
            return cls(
                name=_bare(ident), extent=extent, vis=vis, generics=gen_text,
                fields=self._variant_fields(variant_data, ctx),
                signature=self._header(item_span, gen_end), linkage=linkage, **common,
            )

        if kind_type == "Enum":
            generics = next((it for it in kind_items if isinstance(it, dict) and it.get("_type") == "Generics"), data.get("generics"))
            gen_text, gen_end = self._generics(generics)
            enum_def = next((it for it in kind_items if isinstance(it, dict) and it.get("_type") == "EnumDef"), data)
            variants = []
            for v in enum_def.get("variants", []) if isinstance(enum_def, dict) else ():
                if not isinstance(v, dict):
                    continue
                disr = _some(v.get("disr_expr"))
                disc = self._text(disr.get("value")) if isinstance(disr, dict) and isinstance(disr.get("value"), dict) else None
                variants.append(Ast_Rust_Variant(
                    name=_bare(clean_ident(v.get("ident"))),
                    extent=self.src.extent(v.get("span")),
                    fields=self._variant_fields(v.get("data"), ctx),
                    discriminant=disc or None,
                    attr_nodes=self._attr_nodes(v.get("attrs", []), standalone=False),
                ))
            return Ast_Rust_Enum(
                name=_bare(ident), extent=extent, vis=vis, generics=gen_text, variants=variants,
                signature=self._header(item_span, gen_end), linkage=linkage, **common,
            )

        if kind_type == "Fn":
            return self._fn(ident, item_span, extent, vis, data, ctx, linkage, common)

        if kind_type == "Trait":
            gen_text, gen_end = self._generics(data.get("generics"))
            bounds = data.get("bounds") if isinstance(data.get("bounds"), list) else []
            bounds_span = _cover(bounds)
            sub_ctx = ctx.child(in_trait=True)
            members = [n for n in (self.convert_item(a, sub_ctx) for a in data.get("items", []) or []) if n]
            return Ast_Rust_Trait(
                name=_bare(ident), extent=extent, vis=vis, generics=gen_text,
                bounds=[self._text(b) for b in bounds if isinstance(b, dict)] if bounds_span else [],
                items=members,
                signature=self._header(item_span, _later(gen_end, bounds_span.end if bounds_span else None)),
                linkage=linkage, **common,
            )

        if kind_type == "TraitAlias":
            generics = data.get("generics") or next((it for it in kind_items if isinstance(it, dict) and it.get("_type") == "Generics"), None)
            gen_text, gen_end = self._generics(generics)
            bounds = data.get("bounds") if isinstance(data.get("bounds"), list) else next((it for it in kind_items if isinstance(it, list)), [])
            bounds_span = _cover(bounds)
            return Ast_Rust_TraitAlias(
                name=_bare(ident), extent=extent, vis=vis, generics=gen_text,
                bounds=[self._text(b) for b in bounds if isinstance(b, dict)],
                signature=self._header(item_span, _later(gen_end, bounds_span.end if bounds_span else None)),
                linkage=linkage, **common,
            )

        if kind_type == "Impl":
            gen_text, gen_end = self._generics(data.get("generics"))
            self_ty = data.get("self_ty")
            header_obj = _some(data.get("of_trait"))
            of_trait = None
            if isinstance(header_obj, dict):
                trait_ref = header_obj.get("trait_ref") if isinstance(header_obj.get("trait_ref"), dict) else header_obj
                of_trait = self._text(trait_ref.get("path")) or None
                polarity = header_obj.get("polarity")
                if (polarity == "negative" or _type(polarity) == "Negative") and of_trait:
                    of_trait = f"!{of_trait}"
            self_end = self_ty.get("span").end if isinstance(self_ty, dict) and isinstance(self_ty.get("span"), Span) else None
            sub_ctx = ctx.child(in_trait=of_trait is not None)
            members = [n for n in (self.convert_item(a, sub_ctx) for a in data.get("items", []) or []) if n]
            return Ast_Rust_Impl(
                self_ty=self._ty(self_ty) or ident, of_trait=of_trait, extent=extent, generics=gen_text,
                items=members, header=self._header(item_span, _later(self_end, gen_end)) or None, **common,
            )

        if kind_type == "Const":
            ty_text = self._ty(data.get("ty"))
            return Ast_Rust_Const(name=ident, type_str=ty_text, extent=extent, vis=vis, linkage=linkage, **common)

        if kind_type == "Static":
            c_symbol = None if ctx.in_foreign else c_symbol_of(attributes, _bare(ident))
            node = Ast_Rust_Static(
                name=_bare(ident), type_str=self._ty(data.get("ty")), extent=extent,
                is_mut=data.get("mutability") == "Mut", vis=vis,
                linkage=SymbolLinkage.ExternalDef if c_symbol else linkage, **common,
            )
            node.c_symbol = c_symbol
            return node

        if kind_type in ("TyAlias", "Type"):
            gen_text, _end = self._generics(data.get("generics"))
            bounds = data.get("bounds") if isinstance(data.get("bounds"), list) else []
            bounds_span = _cover(bounds)
            return Ast_Rust_Type(
                name=_bare(ident), target_ty=self._ty(data.get("ty")), extent=extent, vis=vis, generics=gen_text,
                bounds=self._text(bounds_span) if bounds_span else "", linkage=linkage, **common,
            )

        if kind_type == "Mod":
            return self._mod(ident, item, extent, vis, kind_items, ctx, linkage, common)

        if kind_type == "Use":
            tree = kind_items[0] if kind_items and isinstance(kind_items[0], dict) else data
            tree_kind = tree.get("kind") if isinstance(tree, dict) else None
            alias = clean_ident(_items(tree_kind)[0]) if _type(tree_kind) == "Simple" and _items(tree_kind) else ""
            tree_text = self._text(tree) if isinstance(tree, dict) else ""
            return Ast_Rust_Use(
                path=self._text(tree.get("prefix")) if isinstance(tree, dict) else ident,
                extent=extent, vis=vis, alias=alias or None, is_glob=tree_kind == "Glob",
                tree=f"use {tree_text}" if tree_text else None, **common,
            )

        if kind_type == "ExternCrate":
            text = norm(extent.code).removeprefix(vis).strip().rstrip(";").strip()
            return Ast_Rust_Use(path=ident, extent=extent, vis=vis, tree=text or f"extern crate {ident}", **common)

        if kind_type == "MacroDef":
            return Ast_Rust_MacroDef(
                name=_bare(ident), extent=extent, is_macro_rules=data.get("macro_rules", "true") == "true", **common,
            )

        if kind_type == "MacCall":
            mac = kind_items[0] if kind_items and isinstance(kind_items[0], dict) else data
            path = self._text(mac.get("path")) if isinstance(mac, dict) else ""
            fields = macro_fields(extent.code) if is_module_macro(path or ident) else []
            module_attrs = self._module_attrs(extent, fields) if fields else []
            node = Ast_Rust_MacroCall(name=path or ident, extent=extent, module_attrs=module_attrs, **common)
            if is_driver_macro(path or ident):
                for f in fields:
                    if f.key == "type" and f.value:
                        driver_type = f.value.rsplit("::", 1)[-1].strip()
                        if driver_type:
                            val_offset = extent.code.find(driver_type, f.start)
                            if val_offset >= 0:
                                val_line, val_col = self._position(extent, extent.code, val_offset)
                                node.binding_refs.append((driver_type, int(SymbolRole.TypeUsage), val_line, val_col))
            return node

        if kind_type == "GlobalAsm":
            return Ast_Rust_MacroCall(name="global_asm", extent=extent, **common)

        if kind_type == "ForeignMod":
            abi_obj = _some(data.get("abi"))
            if isinstance(abi_obj, dict):
                abi = str(abi_obj.get("symbol_unescaped") or abi_obj.get("symbol") or "C")
            else:
                abi = abi_obj if isinstance(abi_obj, str) and abi_obj else "C"
            sub_ctx = ctx.child(in_foreign=True)
            members = [n for n in (self.convert_item(a, sub_ctx) for a in data.get("items", []) or []) if n]
            return Ast_Rust_ForeignMod(
                abi=abi, extent=extent, items=members, is_unsafe=_type(data.get("safety")) == "Unsafe", **common,
            )

        return None

    def _fn(self, ident: str, item_span: Any, extent: Line, vis: str, fn: dict[str, Any], ctx: _Ctx, linkage: int, common: dict[str, Any]) -> Ast_Rust_Fn:
        sig = fn.get("sig") if isinstance(fn.get("sig"), dict) else {}
        decl = sig.get("decl") if isinstance(sig.get("decl"), dict) else {}
        header = sig.get("header") if isinstance(sig.get("header"), dict) else {}
        params = []
        for idx, p in enumerate(decl.get("inputs", []) or []):
            if not isinstance(p, dict):
                continue
            pat = p.get("pat") if isinstance(p.get("pat"), dict) else {}
            ty = p.get("ty") if isinstance(p.get("ty"), dict) else {}
            pat_text = self._text(pat) if _type(pat.get("kind")) != "Missing" else ""
            ty_kind = _type(ty.get("kind"))
            inner = _items(ty.get("kind"))
            is_self = ty_kind == "ImplicitSelf" or (
                ty_kind in ("Ref", "Ptr", "PinnedRef") and any(
                    isinstance(x, dict) and isinstance(x.get("ty"), dict) and _type(x["ty"].get("kind")) == "ImplicitSelf"
                    for x in inner
                )
            ) or pat_text in ("self", "mut self")
            params.append(Ast_Rust_Param(
                pat_text, self._ty(ty), self.src.extent(p.get("span")),
                is_self=is_self, priority=idx, text=self._text(p) or None,
            ))

        qualifiers = []
        if _type(header.get("constness")) == "Yes":
            qualifiers.append("const")
        coroutine = _some(header.get("coroutine_kind"))
        if isinstance(coroutine, dict):
            qualifiers.append({"Async": "async", "Gen": "gen", "AsyncGen": "async gen"}.get(coroutine.get("_type"), "async"))
        safety = _type(header.get("safety"))
        if safety in ("Unsafe", "Safe"):
            qualifiers.append(safety.lower())
        ext = header.get("ext")
        if _type(ext) == "Explicit":
            lit = next((x for x in _items(ext) if isinstance(x, dict)), {})
            qualifiers.append(f'extern "{lit.get("symbol_unescaped") or lit.get("symbol") or "C"}"')
        elif _type(ext) == "Implicit":
            qualifiers.append("extern")

        gen_text, gen_end = self._generics(fn.get("generics"))
        sig_span = sig.get("span") if isinstance(sig.get("span"), Span) else None
        body = fn.get("body")
        has_body = body is not None and body != "None" and body is not False
        # Callable from C under a fixed symbol: a global definition whatever its Rust visibility
        c_symbol = c_symbol_of(common["attributes"], _bare(ident)) if has_body and not ctx.in_foreign else None
        node = Ast_Rust_Fn(
            name=_bare(ident), extent=extent, vis=vis, generics=gen_text, params=params,
            ret_type=self._ty(decl.get("output")) if _type(decl.get("output")) != "Default" else "",
            qualifiers=qualifiers,
            signature=self._header(item_span, _later(sig_span.end if sig_span else None, gen_end)),
            has_body=has_body, linkage=SymbolLinkage.ExternalDef if c_symbol else linkage, **common,
        )
        node.c_symbol = c_symbol
        return node

    def _mod(self, ident: str, item: dict[str, Any], extent: Line, vis: str, kind_items: list[Any], ctx: _Ctx, linkage: int, common: dict[str, Any]) -> Ast_Rust_Mod:
        loaded = next((it for it in kind_items if isinstance(it, dict) and it.get("_type") == "Loaded"), None)
        _docs, attr_texts = self._attr_texts(item.get("attrs", []))
        path_attr = next((m.group(1) for m in (_PATH_ATTR_RE.match(t) for t in attr_texts) if m), None)
        if loaded is not None:
            parts = _items(loaded)
            sub_items = parts[0] if parts and isinstance(parts[0], list) else []
            inline = len(parts) < 2 or parts[1] != "No"
            child_dir = posixpath.join(ctx.mod_dir, path_attr) if path_attr else posixpath.join(ctx.mod_dir, ident)
            sub_ctx = ctx.child(mod_dir=child_dir)
            members = [n for n in (self.convert_item(sub, sub_ctx) for sub in sub_items) if n]
            return Ast_Rust_Mod(name=_bare(ident), extent=extent, vis=vis, items=members, is_inline=inline, linkage=linkage, **common)
        return Ast_Rust_Mod(
            name=_bare(ident), extent=extent, vis=vis, is_inline=False,
            include_target=self.resolve_module_file(ident, path_attr, ctx), linkage=linkage, **common,
        )

    def resolve_module_file(self, ident: str, path_attr: str | None, ctx: _Ctx) -> str | None:
        """Repository path of the file `mod ident;` loads, when it exists in the version's tree.

        `#[path]` is relative to the file's directory (to the module's inside an inline module). Otherwise
        `ident.rs` / `ident/mod.rs` under the module directory, then next to the file: a crate root (a driver's
        `foo.rs`) keeps its modules beside it, which the name of the file does not tell.
        """
        if self.file_exists is None or not self.rel_path:
            return None
        name = _bare(ident)
        if path_attr:
            base = ctx.mod_dir if ctx.nested else ctx.file_dir
            candidates = [posixpath.join(base, path_attr)]
        else:
            candidates = [
                posixpath.join(ctx.mod_dir, f"{name}.rs"),
                posixpath.join(ctx.mod_dir, name, "mod.rs"),
            ]
            if not ctx.nested:
                candidates += [posixpath.join(ctx.file_dir, f"{name}.rs"), posixpath.join(ctx.file_dir, name, "mod.rs")]
        for cand in candidates:
            path = posixpath.normpath(cand)
            if path.startswith("../") or path == ".." or path.startswith("/"):
                continue
            if self.file_exists(path):
                return path
        return None


# ----------------------------------------------------------------------------------------------------------------
# Comments between items
# ----------------------------------------------------------------------------------------------------------------
def scan_comments(raw_lines: tuple[str, ...], covered: list[Line]) -> list[Ast_Rust_Comment]:
    """Plain comments in the text no node covers: consecutive `//` lines form one block, `/* */` nest."""
    if not raw_lines:
        return []
    starts = [0]
    for line in raw_lines:
        starts.append(starts[-1] + len(line) + 1)
    text = "\n".join(raw_lines)

    def offset(line: int, col: int) -> int:
        idx = max(0, min(len(raw_lines) - 1, line - 1))
        return starts[idx] + max(0, col - 1)

    ranges = sorted((offset(e.line_pos[0], e.char_pos[0]), offset(e.line_pos[1], e.char_pos[1])) for e in covered)
    gaps: list[tuple[int, int]] = []
    pos = 0
    for s, e in ranges:
        if s > pos:
            gaps.append((pos, s))
        pos = max(pos, e)
    if pos < len(text):
        gaps.append((pos, len(text)))

    found: list[tuple[int, int, bool]] = []  # (start, end, is_line_comment)
    for g_s, g_e in gaps:
        i = text.find("/", g_s, g_e)
        while 0 <= i < g_e - 1:
            nxt = text[i + 1]
            if nxt == "/":
                end = text.find("\n", i, g_e)
                end = g_e if end < 0 else end
                found.append((i, end, True))
                i = text.find("/", end, g_e)
            elif nxt == "*":
                depth, j = 1, i + 2
                while j < g_e and depth:
                    if text.startswith("/*", j):
                        depth += 1
                        j += 2
                    elif text.startswith("*/", j):
                        depth -= 1
                        j += 2
                    else:
                        j += 1
                found.append((i, j, False))
                i = text.find("/", j, g_e)
            else:
                i = text.find("/", i + 1, g_e)

    def position(off: int) -> tuple[int, int]:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1, off - starts[lo] + 1

    # Merge consecutive line comments separated only by a newline and indentation
    blocks: list[list[int]] = []
    for s, e, is_line in found:
        if blocks and is_line and blocks[-1][2] and not text[blocks[-1][1]:s].strip() and text.count("\n", blocks[-1][1], s) == 1:
            blocks[-1][1] = e
        else:
            blocks.append([s, e, is_line])

    out = []
    for s, e, _is_line in blocks:
        ls, cs = position(s)
        le, ce = position(e)
        code = text[s:e]
        out.append(Ast_Rust_Comment(code, Line(ls, le, cs, ce, code)))
    return out


# ----------------------------------------------------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------------------------------------------------
def build_rust_ast_nodes(
    parsed_crate: dict[str, Any],
    raw_lines: tuple[str, ...],
    rel_path: str | None = None,
    file_exists: Callable[[str], bool] | None = None,
) -> list[Ast_Rust]:
    """Convert parsed Crate dictionary into strongly typed Ast_Rust objects (in source order)."""
    if not isinstance(parsed_crate, dict):
        return []
    return RustTreeBuilder(raw_lines, rel_path, file_exists).build(parsed_crate)


def parse_rust_ast_tree(
    ast_debug_text: str,
    raw_lines: tuple[str, ...],
    rel_path: str | None = None,
    file_exists: Callable[[str], bool] | None = None,
    source_path: str | None = None,
) -> list[Ast_Rust]:
    """Parse raw rustc AST debug text into a list of Ast_Rust objects.

    `source_path` is the path rustc was given (found from the first span when omitted); `rel_path` the file's
    repository path and `file_exists(repo_path)` the tree lookup that links `mod x;` declarations to their files.
    """
    if not ast_debug_text or not ast_debug_text.strip():
        return []
    text = strip_unused_blocks(shorten_span_paths(ast_debug_text, source_path))
    tree = RustAstTreeParser(RustAstTreeTokenizer(text)).parse()
    if isinstance(tree, dict) and tree.get("_type") == "Crate":
        return build_rust_ast_nodes(tree, raw_lines, rel_path, file_exists)
    return []
