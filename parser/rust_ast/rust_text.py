"""parser/rust_ast/rust_text.py - Source-text scans of Rust items (parser/rust_ast/RUST_API.md sections 6, 7).

rustc's dump does not expand macros and its function bodies are dropped before parsing, so three details are
read from an item's source text instead:

- `mask_code()`: the text with comments and string / char literals blanked (same length, newlines kept), so
  the scans below never match inside them.
- `macro_fields()`: the `key: value` entries of a `module! { ... }`-style macro call (nested blocks such as
  `params: { p: i64 { default: 1 } }` as children).
- `scan_binding_refs()`: uses of C symbols through the bindgen crates (`bindings::foo`, `kernel::bindings::foo`,
  `uapi::FOO`), with a role told from the text around them (call, type, other); a bindgen enumerator name
  (`phy_state_PHY_RUNNING`) is the C enumerator (`PHY_RUNNING`).

Offsets are positions in the latin-1 text (one character per byte), so columns are byte columns.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from core.globalstuff import SymbolRole

# A char literal: escape, one ASCII character or one UTF-8 sequence read as latin-1 (2-4 bytes); a quote followed
# by anything else starts a lifetime or label (`'a`, `'static`)
_CHAR_LIT_RE = re.compile(r"'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|.)|[^\\'\n\x80-\xff]|[\x80-\xff]{2,4})'")
_RAW_STR_RE = re.compile(r'(?:b|c)?r(#*)"')
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_]")


def mask_code(text: str, fill: str = " ") -> str:
    """`text` with comments replaced by spaces and string / char literals by `fill` (newlines kept)."""
    out = list(text)
    n = len(text)
    i = 0

    def blank(s: int, e: int, c: str = " ") -> None:
        for k in range(s, min(e, n)):
            if out[k] != "\n":
                out[k] = c

    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            e = text.find("\n", i)
            e = n if e < 0 else e
            blank(i, e)
            i = e
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif ch in "rbc" and (i == 0 or not _IDENT_CHAR.match(text[i - 1])) and (m := _RAW_STR_RE.match(text, i)):
            close = '"' + m.group(1)
            e = text.find(close, m.end())
            e = n if e < 0 else e + len(close)
            blank(i, e, fill)
            i = e
        elif ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            blank(i, j + 1, fill)
            i = j + 1
        elif ch == "'" and (m := _CHAR_LIT_RE.match(text, i)):
            blank(i, m.end(), fill)
            i = m.end()
        else:
            i += 1
    return "".join(out)


# ----------------------------------------------------------------------------------------------------------------
# Macro call fields (`module! { type: M, name: "m", authors: ["a"], params: { p: i64 { default: 1 } } }`)
# ----------------------------------------------------------------------------------------------------------------
class MacroField(NamedTuple):
    key: str
    value: str          # the value as written, whitespace-normalized
    start: int          # offset of the key in the text
    end: int            # offset just past the value
    children: list      # entries of a `{ ... }` block value (`name: Ty { ... }` items for `params`)


_KEY_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:(?!:)")
_OPEN = {"{": "}", "(": ")", "[": "]"}
# What may precede the `{` of a block value: nothing, or a type (`i64`, `&'static str`, `ArrayParam<i32, 3>`)
_BLOCK_TYPE_RE = re.compile(r"[\s\w:<>&',]*")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _match_close(masked: str, i: int) -> int:
    """Offset of the bracket closing the one at `i` (len(masked) when it is not closed)."""
    stack = [_OPEN[masked[i]]]
    j = i + 1
    while j < len(masked) and stack:
        c = masked[j]
        if c in _OPEN:
            stack.append(_OPEN[c])
        elif c in ")]}":
            stack.pop()
        j += 1
    return j - 1 if not stack else len(masked)


def _split_top(masked: str, start: int, end: int) -> list[tuple[int, int]]:
    """Ranges of `masked[start:end]` separated by commas outside brackets."""
    parts: list[tuple[int, int]] = []
    depth, s = 0, start
    for j in range(start, end):
        c = masked[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append((s, j))
            s = j + 1
    parts.append((s, end))
    return [(a, b) for a, b in parts if masked[a:b].strip()]


def _fields(text: str, masked: str, start: int, end: int) -> list[MacroField]:
    out: list[MacroField] = []
    for a, b in _split_top(masked, start, end):
        m = _KEY_RE.match(masked, a)
        if not m or m.end() > b:
            continue
        v_s = m.end()
        while v_s < b and masked[v_s].isspace():
            v_s += 1
        v_e = b
        while v_e > v_s and masked[v_e - 1].isspace():
            v_e -= 1
        children: list[MacroField] = []
        brace = masked.find("{", v_s, v_e)
        # A block value (`{ ... }`) or a typed block (`i64 { ... }`, a module parameter): its own entries
        if brace >= 0 and masked[v_e - 1] == "}" and _BLOCK_TYPE_RE.fullmatch(masked, v_s, brace) and _match_close(masked, brace) == v_e - 1:
            children = _fields(text, masked, brace + 1, v_e - 1)
        key_s = m.start(1)
        out.append(MacroField(m.group(1), _norm(text[v_s:v_e]), key_s, v_e, children))
    return out


def macro_fields(code: str) -> list[MacroField]:
    """The `key: value` entries of a macro call's text (`path! { ... }`, `path!( ... )`), [] without any."""
    masked = mask_code(code, fill="\0")  # literals kept as non-blank text, so values are trimmed correctly
    bang = masked.find("!")
    if bang < 0:
        return []
    i = bang + 1
    while i < len(masked) and masked[i].isspace():
        i += 1
    if i >= len(masked) or masked[i] not in _OPEN:
        return []
    return _fields(code, masked, i + 1, _match_close(masked, i))


# ----------------------------------------------------------------------------------------------------------------
# Uses of C symbols through bindgen (`bindings::foo`, `uapi::FOO`)
# ----------------------------------------------------------------------------------------------------------------
class BindingRef(NamedTuple):
    name: str           # the C symbol (`PHY_RUNNING` for bindgen's `phy_state_PHY_RUNNING`)
    crate: str          # "bindings" or "uapi"
    role: int           # SymbolRole: Call, TypeUsage or DeclRef
    offset: int         # offset of the bindgen name in the text


# The crate segment is a whole identifier: `my_bindings::x` is not a use of the bindgen crate
_BINDING_RE = re.compile(r"(?<![A-Za-z0-9_])(bindings|uapi|c_bindings|ffi)\s*::\s*([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*!)")
_PATH_PREFIX_RE = re.compile(r"(?:(?:\$?[A-Za-z_][A-Za-z0-9_]*|)\s*::\s*)+$")
_TYPE_BEFORE_RE = re.compile(
    r"(?:\*\s*const|\*\s*mut|&\s*(?:'\w+\s*)?mut|&\s*'\w+|&|->|\bdyn|\bimpl|\bas|[A-Za-z0-9_:>]<|(?<!:):"
    r"|\btype\s+\w+\s*(?:<[^=;]*>)?\s*="            # type X = bindings::t
    r"|\bstruct\s+\w+\s*(?:<[^();]*>)?\s*\("        # struct X(bindings::t)
    r"|\boffset_of!\s*\("                              # offset_of!(bindings::t, f)
    r"|\bcontainer_of!\s*\([^(),;]*(?:\([^()]*\))?[^(),;]*,)\s*$"  # container_of!(p, bindings::t, f)
)
# bindgen names a C enumerator `<enum>_<ENUMERATOR>` (`phy_state_PHY_RUNNING`, `irqreturn_IRQ_NONE`)
class _EnumeratorRegex:
    """Matches bindgen enum variants `<enum>_<ENUMERATOR>` without truncating C symbols like `bpf_prog_RUN`."""

    pattern = r"([a-z][a-z0-9_]*?)_([A-Z](?:[A-Z0-9_]*[A-Z0-9])?)"
    _BASE = re.compile(rf"^{pattern}$")

    def fullmatch(self, string: str, pos: int = 0, endpos: int | None = None) -> Any:
        target = string if endpos is None else string[pos:endpos]
        m = self._BASE.fullmatch(target)
        if not m:
            return None
        pfx, sfx = m.group(1), m.group(2)
        common_len = 0
        while common_len < min(len(pfx), len(sfx)) and pfx[common_len].upper() == sfx[common_len]:
            common_len += 1
        if pfx.endswith("_enum") or pfx == "enum" or pfx.startswith("enum_") or common_len >= 2:
            class _Match:
                def group(self, idx: int) -> str:
                    return sfx if idx == 1 else target
            return _Match()
        return None

_ENUMERATOR_RE = _EnumeratorRegex()



def _role(masked: str, name: str, start: int, end: int) -> int:
    rest = masked[end:end + 64].lstrip()
    if rest.startswith("("):
        return int(SymbolRole.Call)
    if rest.startswith("::") and not rest.startswith("::<"):
        return int(SymbolRole.TypeUsage)  # an item of a bindgen type or enum module (`bindings::lru_status::LRU_SKIP`)
    if not any(c.islower() for c in name):
        return int(SymbolRole.DeclRef)  # a constant (`bindings::GFP_KERNEL`) is never a type
    if rest.startswith("{"):
        return int(SymbolRole.TypeUsage)  # a struct literal (`bindings::pci_device_id { .. }`)
    before = masked[max(0, start - 96):start]
    prefix = _PATH_PREFIX_RE.search(before)
    if prefix:
        before = before[:prefix.start()]
    return int(SymbolRole.TypeUsage) if _TYPE_BEFORE_RE.search(before) else int(SymbolRole.DeclRef)


def scan_binding_refs(code: str, masked: str | None = None) -> list[BindingRef]:
    """Uses of bindgen symbols in an item's text (comments and literals excluded), in text order."""
    if "bindings" not in code and "uapi" not in code and "ffi" not in code:
        return []
    masked = mask_code(code) if masked is None else masked
    out = []
    for m in _BINDING_RE.finditer(masked):
        name = m.group(2)
        if "__bindgen" in name or name.startswith("__Bindgen"):
            continue  # bindgen's own types (anonymous members, bitfield units): no C symbol
        enumerator = _ENUMERATOR_RE.fullmatch(name)
        if enumerator:
            out.append(BindingRef(enumerator.group(1), m.group(1), int(SymbolRole.DeclRef), m.start(2)))
            continue
        out.append(BindingRef(name, m.group(1), _role(masked, name, m.start(1), m.end(2)), m.start(2)))
    return out
