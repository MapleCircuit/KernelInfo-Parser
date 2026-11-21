"""parser/struct_source.py - Pure-text C definition spans and struct member layout with preprocessor guards.

No database, Git or file access: every function reads one source text. The webapp uses it as the `"git"`
fallback for revisions that were never parsed, and for the member guards (`#ifdef` / `#if` conditions) that
the database does not store.

- `find_definition_span(text, name, kind)` locates a struct, union, enum, function or macro definition and
  returns `(kind, line_s, line_e)` (1-based, inclusive). A prototype is not a definition.
- `extract_struct_layout(text, line_s, line_e)` parses the body of a struct / union into an item tree
  (fields, nested aggregates, preprocessor directives) with the condition stack active at every item.
- `outer_conditions(text, line_s)` is the condition stack in effect at a line, from conditionals opened
  before it.

Conditions are normalised like `parser/include_graph.py` `include_directives_with_conditions()`:
`#ifdef X` is `defined(X)`, `#ifndef X` is `!defined(X)`, `#if E` is `(E)`, and a branch after `#elif` /
`#else` adds the negation of every earlier branch of its block.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

KINDS = ("struct", "union", "enum", "function", "macro")
_TAGS = ("struct", "union", "enum")
_COND_DIRECTIVES = ("if", "ifdef", "ifndef", "elif", "elifdef", "elifndef", "else", "endif")

_COMMENT_OR_LITERAL_RE = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.DOTALL)
_DIRECTIVE_RE = re.compile(r"^[ \t]*#[ \t]*([A-Za-z_]*)[ \t]*(.*?)[ \t]*$", re.DOTALL)
_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\d[\w.]*|\.\.\.|->|[^\s\w]|\S')
_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")
_DEFINE_NAME_RE = re.compile(r"^([A-Za-z_]\w*)")

_QUALIFIERS = frozenset({"const", "volatile", "restrict", "__restrict", "__restrict__", "static", "extern", "register"})
_TYPE_WORDS = frozenset({
    "int", "char", "short", "long", "unsigned", "signed", "bool", "_Bool", "void", "float", "double",
    "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64", "__u8", "__u16", "__u32", "__u64",
    "__s8", "__s16", "__s32", "__s64",
})
_BARE_ATTRS = frozenset({
    "__packed", "__packed__", "__randomize_layout", "__designated_init", "__cacheline_aligned",
    "__cacheline_aligned_in_smp", "____cacheline_aligned", "____cacheline_aligned_in_smp", "__aligned",
    "__attribute_const__", "__no_randomize_layout", "__private",
})


# --- Text preparation ----------------------------------------------------------------------------

def strip_comments(text: str) -> str:
    """`text` with `/* */` and `//` comments replaced by a space (block comments keep their newlines, so line
    numbers are unchanged); string and character literals are kept."""
    def repl(m: re.Match) -> str:
        s = m.group(0)
        if s[0] in "\"'":
            return s
        return " " + "\n" * s.count("\n") if s.startswith("/*") else " "

    return _COMMENT_OR_LITERAL_RE.sub(repl, (text or "").replace("\r\n", "\n"))


@dataclass
class LogicalLine:
    """A source line with comments removed and backslash continuations joined (`ls`..`le` physical lines)."""
    ls: int
    le: int
    text: str
    directive: tuple[str, str] | None = None   # (name, argument) of a preprocessor line


def logical_lines(text: str) -> list[LogicalLine]:
    """Comment-free logical lines of `text`, each with its first and last physical line number."""
    raw = strip_comments(text).split("\n")
    out: list[LogicalLine] = []
    i = 0
    while i < len(raw):
        ls = i + 1
        buf = raw[i]
        while buf.rstrip().endswith("\\") and i + 1 < len(raw):
            i += 1
            buf = buf.rstrip()[:-1] + " " + raw[i].lstrip()
        directive = None
        m = _DIRECTIVE_RE.match(buf)
        if m:
            directive = (m.group(1), m.group(2).strip())
        out.append(LogicalLine(ls, i + 1, buf, directive))
        i += 1
    return out


class _Conds:
    """The stack of open `#if` blocks: one frame `[earlier branch conditions, active conditions]` each."""

    def __init__(self) -> None:
        self.stack: list[list[list[str]]] = []

    def active(self) -> tuple[str, ...]:
        return tuple(c for frame in self.stack for c in frame[1])

    @staticmethod
    def _positive(name: str, arg: str) -> str:
        if name in ("if", "elif"):
            return f"({arg})"
        if not arg:
            return "(0)"
        return f"defined({arg})" if name in ("ifdef", "elifdef") else f"!defined({arg})"

    def apply(self, name: str, arg: str) -> int | None:
        """Apply a conditional directive; its nesting depth (0 = outermost), None when it has no open block."""
        if name in ("if", "ifdef", "ifndef"):
            depth = len(self.stack)
            positive = self._positive(name, arg)
            self.stack.append([[positive], [positive]])
            return depth
        if not self.stack:
            return None
        depth = len(self.stack) - 1
        frame = self.stack[-1]
        if name in ("elif", "elifdef", "elifndef"):
            positive = self._positive(name, arg)
            frame[1] = [f"!({e})" for e in frame[0]] + [positive]
            frame[0].append(positive)
        elif name == "else":
            frame[1] = [f"!({e})" for e in frame[0]]
        elif name == "endif":
            self.stack.pop()
        return depth


def outer_conditions(text: str, line_s: int) -> list[str]:
    """Normalised conditions of the `#if` blocks opened before line `line_s` and still open there."""
    conds = _Conds()
    for ll in logical_lines(text):
        if ll.ls >= line_s:
            break
        if ll.directive and ll.directive[0] in _COND_DIRECTIVES:
            conds.apply(*ll.directive)
    return list(conds.active())


# --- Tokens --------------------------------------------------------------------------------------

@dataclass
class _Tok:
    text: str
    line: int
    conds: tuple[str, ...] = ()
    directive: dict[str, Any] | None = field(default=None)


def _code_tokens(lls: list[LogicalLine]) -> list[_Tok]:
    """Tokens of every non-preprocessor line."""
    toks: list[_Tok] = []
    for ll in lls:
        if ll.directive is not None:
            continue
        toks.extend(_Tok(t, ll.ls) for t in _TOKEN_RE.findall(ll.text))
    return toks


def _skip_parens(toks: list[_Tok], i: int) -> int:
    """Index after the `)` matching the `(` at `toks[i]` (`len(toks)` when unmatched)."""
    depth = 0
    n = len(toks)
    while i < n:
        t = toks[i].text
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _match_brace(toks: list[_Tok], i: int) -> int | None:
    """Index of the `}` matching the `{` at `toks[i]`."""
    depth = 0
    for j in range(i, len(toks)):
        t = toks[j].text
        if t == "{":
            depth += 1
        elif t == "}":
            depth -= 1
            if depth == 0:
                return j
    return None


def _brace_depths(toks: list[_Tok]) -> list[int]:
    """Brace depth at every token; the body of `extern "C" { }` does not count as nesting."""
    depths: list[int] = []
    depth = 0
    stack: list[bool] = []
    for i, t in enumerate(toks):
        if t.text == "{":
            depths.append(depth)
            transparent = i >= 2 and toks[i - 1].text.startswith('"') and toks[i - 2].text == "extern"
            stack.append(transparent)
            if not transparent:
                depth += 1
        elif t.text == "}":
            if stack and not stack.pop():
                depth -= 1
            depths.append(depth)
        else:
            depths.append(depth)
    return depths


def _is_attr_word(text: str) -> bool:
    return bool(_IDENT_RE.match(text)) and (text.startswith("_") or text.isupper())


# --- Definition spans ----------------------------------------------------------------------------

def _aggregate_open(toks: list[_Tok], i: int, enum: bool) -> tuple[str | None, int] | None:
    """(tag name or None, index of `{`) of `struct|union|enum [NAME] [attributes] {` starting at keyword `toks[i]`."""
    n = len(toks)
    j = i + 1
    tag = None
    if j < n and _IDENT_RE.match(toks[j].text) and not (j + 1 < n and toks[j + 1].text == "("):
        tag = toks[j].text
        j += 1
    colon = False
    while j < n:
        tx = toks[j].text
        if tx == "{":
            return tag, j
        if tx == "(":
            j = _skip_parens(toks, j)
        elif enum and tx == ":":
            colon = True
            j += 1
        elif _IDENT_RE.match(tx) and (_is_attr_word(tx) or colon or tx == "__attribute__"):
            j += 1
        else:
            return None
    return None


def _find_aggregate(toks: list[_Tok], name: str, tag: str) -> tuple[str, int, int] | None:
    for i, t in enumerate(toks):
        if t.text != tag:
            continue
        opened = _aggregate_open(toks, i, tag == "enum")
        if opened is None:
            continue
        tag_name, bi = opened
        end = _match_brace(toks, bi)
        if end is None:
            continue
        typedef = i > 0 and toks[i - 1].text == "typedef"
        matched = tag_name == name
        if not matched and typedef:
            j = end + 1
            names = []
            while j < len(toks) and toks[j].text != ";":
                names.append(toks[j].text)
                j += 1
            matched = name in names
        if matched:
            start = toks[i - 1].line if typedef else t.line
            return tag, start, toks[end].line
    return None


def _find_function(toks: list[_Tok], name: str) -> tuple[str, int, int] | None:
    depths = _brace_depths(toks)
    n = len(toks)
    for i, t in enumerate(toks):
        if t.text != name or depths[i] != 0 or i + 1 >= n or toks[i + 1].text != "(":
            continue
        if i > 0 and toks[i - 1].text in (".", "->"):
            continue
        j = _skip_parens(toks, i + 1)
        while j < n:
            tx = toks[j].text
            if tx == "(":
                j = _skip_parens(toks, j)
            elif tx == "{":
                break
            elif _IDENT_RE.match(tx):
                j += 1
            else:
                j = n
        if j >= n:
            continue
        end = _match_brace(toks, j)
        if end is None:
            continue
        start = i
        while start > 0 and toks[start - 1].text not in (";", "}", "{"):
            start -= 1
        return "function", toks[start].line, toks[end].line
    return None


def _find_macro(lls: list[LogicalLine], name: str) -> tuple[str, int, int] | None:
    for ll in lls:
        if ll.directive and ll.directive[0] == "define":
            m = _DEFINE_NAME_RE.match(ll.directive[1])
            if m and m.group(1) == name:
                return "macro", ll.ls, ll.le
    return None


def find_definition_span(text: str, name: str, kind: str | None = None) -> tuple[str, int, int] | None:
    """(kind, line_s, line_e) of the definition of `name` in `text`, 1-based and inclusive, else None.

    `kind` is `struct`, `union`, `enum`, `function` or `macro` (None: the first that matches, in that order).
    A struct / union / enum definition is `struct NAME {` (the brace may follow on a later line, attributes may
    sit between) or a `typedef struct [NAME] { ... } NAME;` (the span then starts at `typedef`); a function
    definition is a file-scope `NAME(...)` followed by a `{` body, not a prototype ending in `;`; a macro is a
    `#define NAME` with its continuation lines."""
    if not name or (kind is not None and kind not in KINDS):
        return None
    lls = logical_lines(text)
    toks: list[_Tok] | None = None
    for k in ((kind,) if kind else KINDS):
        if k == "macro":
            found = _find_macro(lls, name)
        else:
            if toks is None:
                toks = _code_tokens(lls)
            found = _find_function(toks, name) if k == "function" else _find_aggregate(toks, name, k)
        if found:
            return found
    return None


# --- Struct layout -------------------------------------------------------------------------------

def _layout_tokens(lls: list[LogicalLine]) -> list[_Tok]:
    """Tokens of `lls` with their active conditions; conditional directives become tokens carrying an item."""
    conds = _Conds()
    toks: list[_Tok] = []
    for ll in lls:
        if ll.directive is not None:
            name, arg = ll.directive
            if name in _COND_DIRECTIVES:
                depth = conds.apply(name, arg)
                if depth is not None:
                    item = {
                        "kind": "directive", "directive": name,
                        "expr": "" if name in ("else", "endif") else arg,
                        "text": ll.text.strip(), "line": ll.ls, "depth": depth,
                    }
                    toks.append(_Tok("", ll.ls, conds.active(), item))
            continue
        active = conds.active()
        toks.extend(_Tok(t, ll.ls, active) for t in _TOKEN_RE.findall(ll.text))
    return toks


def _join(texts: list[str]) -> str:
    """Declaration text from tokens: `unsigned long *p`, `void (*fn)(int, char)`, `char buf[16]`."""
    out = ""
    prev = ""
    bracket = 0
    for idx, tx in enumerate(texts):
        nxt = texts[idx + 1] if idx + 1 < len(texts) else ""
        if tx == "[":
            bracket += 1
        elif tx == "]":
            bracket = max(0, bracket - 1)
        if not out or tx in (")", "]", ",", ";") or prev in ("(", "[") or tx == "[":
            space = False
        elif tx == "(":
            space = bool(_IDENT_RE.match(prev)) and nxt == "*"
        elif tx == "*":
            space = bracket > 0 or prev not in ("*", "(")
        elif prev == "*":
            space = bracket > 0
        elif prev == ")" or prev == "]":
            space = _word(tx) or tx in (":",)
        else:
            space = True
        out += (" " if space else "") + tx
        prev = tx
    return out


def _word(tx: str) -> bool:
    return bool(tx) and (tx[0].isalnum() or tx[0] in "_\"'")


def _strip_attrs(toks: list[_Tok]) -> list[_Tok]:
    """`toks` without `__attribute__((...))`, `__aligned(N)`-style calls (not the first token) and bare attribute words."""
    out: list[_Tok] = []
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i].text
        call = i + 1 < n and toks[i + 1].text == "(" and not (i + 2 < n and toks[i + 2].text in ("*", "^"))
        if t in _BARE_ATTRS:
            i = _skip_parens(toks, i + 1) if call else i + 1
        elif i > 0 and call and (t.startswith("_") or t in ("alignas", "_Alignas")):
            i = _skip_parens(toks, i + 1)
        else:
            out.append(toks[i])
            i += 1
    return out


def _split_top(toks: list[_Tok], sep: str) -> list[list[_Tok]]:
    """`toks` split at `sep` outside any parentheses, brackets and braces."""
    parts: list[list[_Tok]] = [[]]
    depth = 0
    for t in toks:
        if t.text in ("(", "[", "{"):
            depth += 1
        elif t.text in (")", "]", "}"):
            depth -= 1
        if t.text == sep and depth == 0:
            parts.append([])
        else:
            parts[-1].append(t)
    return parts


def _trailing_dims(toks: list[_Tok]) -> tuple[list[_Tok], list[str]]:
    """(`toks` without its trailing `[...]` groups, the groups' text in source order)."""
    dims: list[str] = []
    end = len(toks)
    while end > 0 and toks[end - 1].text == "]":
        j = end - 1
        depth = 0
        while j >= 0:
            if toks[j].text == "]":
                depth += 1
            elif toks[j].text == "[":
                depth -= 1
                if depth == 0:
                    break
            j -= 1
        if j < 0:
            break
        dims.insert(0, _join([t.text for t in toks[j:end]]))
        end = j
    return toks[:end], dims


def _bits_of(toks: list[_Tok]) -> int | None:
    text = "".join(t.text for t in toks).rstrip("uUlL")
    try:
        return int(text, 0)
    except ValueError:
        return None


def _declarator_item(toks: list[_Tok], conds: tuple[str, ...], base: list[_Tok] | None) -> tuple[dict[str, Any], list[_Tok]] | None:
    """(field item, base type tokens for the next declarators) of one declarator; `toks` holds the base type
    for the first declarator, `base` is the base type prepended for the others."""
    full = (base or []) + toks
    parts = _split_top_colon(full)
    body, bits_toks = parts
    bits = _bits_of(bits_toks) if bits_toks else None
    decl = _join([t.text for t in full])
    # function pointer: ... ( * [qualifiers] name [dims] ) ( params ) ...
    fp = next(
        (i for i, t in enumerate(body) if t.text == "(" and i + 1 < len(body) and body[i + 1].text in ("*", "^")), None
    )
    if fp is not None:
        close = _skip_parens(body, fp)
        inside = body[fp + 1:close - 1]
        inner, dims = _trailing_dims(inside)
        name_tok = next((t for t in reversed(inner) if _IDENT_RE.match(t.text) and t.text not in _QUALIFIERS), None)
        before = [t.text for t in body[:fp]]
        rest = [t.text for t in body[close:]]
        ftype = f"{_join(before)} (*){_join(rest)}".strip()
        item = {
            "kind": "field", "name": name_tok.text if name_tok else "", "type": ftype, "decl": decl,
            "line": (name_tok or body[fp]).line, "bits": bits, "array": "".join(dims) or None, "conds": list(conds),
        }
        return item, body[:fp]
    core, dims = _trailing_dims(body)
    if not core:
        return None
    last = core[-1]
    anonymous = bool(bits_toks) and (len(core) == 1 or last.text in _TYPE_WORDS)
    if anonymous:
        name_tok, type_toks = None, core
    elif _IDENT_RE.match(last.text) and len(core) >= 2 and core[-2].text not in ("struct", "union", "enum"):
        name_tok, type_toks = last, core[:-1]
    else:
        return None   # a forward declaration or a lone type word declares no member
    star = next((i for i, t in enumerate(type_toks) if t.text == "*"), None)
    next_base = type_toks if star is None else type_toks[:star]
    item = {
        "kind": "field", "name": name_tok.text if name_tok else "", "type": _join([t.text for t in type_toks]),
        "decl": decl, "line": (name_tok or last).line, "bits": bits, "array": "".join(dims) or None,
        "conds": list(conds),
    }
    return item, next_base


def _split_top_colon(toks: list[_Tok]) -> tuple[list[_Tok], list[_Tok]]:
    """(declarator tokens, bit-width tokens) split at the last top-level `:` (bit-width tokens empty without one)."""
    depth = 0
    pos = None
    for i, t in enumerate(toks):
        if t.text in ("(", "[", "{"):
            depth += 1
        elif t.text in (")", "]", "}"):
            depth -= 1
        elif t.text == ":" and depth == 0:
            pos = i
    return (toks[:pos], toks[pos + 1:]) if pos is not None else (toks, [])


def _field_items(toks: list[_Tok], conds: tuple[str, ...]) -> list[dict[str, Any]]:
    """Field items of one `;`-terminated member declaration (one per declarator)."""
    if toks and toks[-1].text == ";":
        toks = toks[:-1]
    if not toks:
        return []
    # a whole-declaration macro call: DECLARE_BITMAP(name, bits), __DECLARE_FLEX_ARRAY(type, name)
    if len(toks) > 3 and _IDENT_RE.match(toks[0].text) and toks[1].text == "(" and toks[2].text not in ("*", "^") \
            and _skip_parens(toks, 1) == len(toks):
        args = _split_top(toks[2:-1], ",")
        arg0 = args[0][0].text if args and len(args[0]) == 1 and _IDENT_RE.match(args[0][0].text) else ""
        return [{
            "kind": "field", "name": arg0 or toks[0].text, "type": toks[0].text,
            "decl": _join([t.text for t in toks]), "line": toks[0].line, "bits": None, "array": None, "conds": list(conds),
        }]
    toks = _strip_attrs(toks)
    items: list[dict[str, Any]] = []
    base: list[_Tok] | None = None
    for seg in _split_top(toks, ","):
        if not seg:
            continue
        made = _declarator_item(seg, conds, base)
        if made is None:
            break
        item, next_base = made
        items.append(item)
        if base is None:
            base = next_base
    return items


def _parse_body(toks: list[_Tok], i: int) -> tuple[list[dict[str, Any]], int, int]:
    """(items, index after the closing `}`, line of that `}`) of the member list starting at `toks[i]`."""
    items: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    acc: list[_Tok] = []
    paren = 0
    n = len(toks)
    end_line = toks[i - 1].line if i else 0

    def flush(extra: list[dict[str, Any]] | None = None) -> None:
        if acc:
            items.extend(extra if extra is not None else _field_items(list(acc), acc[0].conds))
            acc.clear()
        items.extend(deferred)
        deferred.clear()

    while i < n:
        t = toks[i]
        if t.directive is not None:
            (deferred if acc else items).append(t.directive)
            i += 1
            continue
        tx = t.text
        if tx == "}" and paren <= 0:
            flush()
            return items, i + 1, t.line
        if tx in ("(", "["):
            paren += 1
        elif tx in (")", "]"):
            paren -= 1
        if tx == ";" and paren <= 0:
            flush()
            i += 1
            continue
        if tx == "{" and paren <= 0:
            head = [a.text for a in acc]
            core = [h for h in head if h not in _QUALIFIERS]
            if core and core[0] in ("struct", "union") and "=" not in head:
                children, i, close_line = _parse_body(toks, i + 1)
                tail: list[_Tok] = []
                while i < n and not (toks[i].directive is None and toks[i].text == ";" and not tail_open(tail)):
                    if toks[i].directive is not None:
                        deferred.append(toks[i].directive)
                    else:
                        tail.append(toks[i])
                    i += 1
                i += 1
                items.append(_aggregate_item(acc, children, close_line, tail))
                items.extend(_aggregate_extra_fields(acc, tail))
                acc.clear()
                items.extend(deferred)
                deferred.clear()
                continue
            # enum or other braced type: kept as an opaque `{...}` inside the declaration
            close = _match_brace(toks, i)
            if close is None:
                close = n - 1
            acc.extend([_Tok("{", t.line, t.conds), _Tok("...", t.line, t.conds), _Tok("}", toks[close].line, t.conds)])
            i = close + 1
            continue
        acc.append(t)
        i += 1
    flush()
    return items, n, end_line


def tail_open(tail: list[_Tok]) -> bool:
    """True while `tail` has an unclosed parenthesis or bracket (a `;` inside is not the end)."""
    depth = 0
    for t in tail:
        if t.text in ("(", "["):
            depth += 1
        elif t.text in (")", "]"):
            depth -= 1
    return depth > 0


def _aggregate_head(acc: list[_Tok]) -> tuple[str, str]:
    """(tag, tag name) of the tokens before an aggregate's `{`."""
    texts = [t.text for t in acc if t.text not in _QUALIFIERS]
    tag = texts[0]
    name = texts[1] if len(texts) > 1 and _IDENT_RE.match(texts[1]) and not (len(texts) > 2 and texts[2] == "(") else ""
    if name in _BARE_ATTRS or name == "__attribute__":
        name = ""
    return tag, name


def _aggregate_item(acc: list[_Tok], children: list[dict[str, Any]], close_line: int, tail: list[_Tok]) -> dict[str, Any]:
    tag, tag_name = _aggregate_head(acc)
    first = _field_items(acc_base(acc, tail), acc[0].conds) if tail else []
    return {
        "kind": "aggregate", "tag": tag, "name": first[0]["name"] if first else "",
        "type_name": f"{tag} {tag_name}" if tag_name else "", "line": acc[0].line, "line_e": close_line,
        "conds": list(acc[0].conds), "children": children,
    }


def acc_base(acc: list[_Tok], tail: list[_Tok]) -> list[_Tok]:
    """Declaration tokens of an aggregate member: the `struct NAME` head followed by its declarators."""
    tag, tag_name = _aggregate_head(acc)
    head = [_Tok(tag, acc[0].line, acc[0].conds)]
    head.append(_Tok(tag_name or "{...}", acc[0].line, acc[0].conds))
    return head + tail


def _aggregate_extra_fields(acc: list[_Tok], tail: list[_Tok]) -> list[dict[str, Any]]:
    """Declarators after the first one of `struct { ... } a, *b;` as fields (the first names the aggregate)."""
    if not tail:
        return []
    return _field_items(acc_base(acc, tail), acc[0].conds)[1:]


def extract_struct_layout(text: str, line_s: int, line_e: int) -> dict[str, Any]:
    """The members of the struct / union spanning `line_s`..`line_e` as `{"items": [...], "has_guards": bool}`.

    Items, in source order and nested by braces:
    - `{"kind": "field", name, type, decl, line, bits, array, conds}`: one per declarator (`int a, *b;` gives two);
    - `{"kind": "aggregate", tag, name, type_name, line, line_e, conds, children}`: a nested struct / union,
      anonymous (`name` and `type_name` empty) or named;
    - `{"kind": "directive", directive, expr, text, line, depth}`: a conditional directive.
    `conds` is the condition stack opened inside the span and active at the item; `depth` is the conditional
    nesting depth inside the span (an `#endif` carries the depth of its `#if`)."""
    lls = [ll for ll in logical_lines(text) if line_s <= ll.ls <= line_e]
    toks = _layout_tokens(lls)
    start = next((i for i, t in enumerate(toks) if t.directive is None and t.text == "{"), None)
    if start is None:
        return {"items": [], "has_guards": False}
    items, _, _ = _parse_body(toks, start + 1)
    return {"items": items, "has_guards": _has_directive(items)}


def _has_directive(items: list[dict[str, Any]]) -> bool:
    for it in items:
        if it["kind"] == "directive" or (it["kind"] == "aggregate" and _has_directive(it["children"])):
            return True
    return False
