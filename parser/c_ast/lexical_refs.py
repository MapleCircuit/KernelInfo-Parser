"""parser/c_ast/lexical_refs.py - References libclang leaves unresolved, recovered from the token stream.

libclang emits a reference cursor only for a name it resolved: a macro whose definition is in a header the
compilation did not reach, a function or variable declared nowhere it saw, a type in a branch of `#if` that is
not compiled, or any name in code the preprocessor skipped carry no reference, and the C-AST parser records none
(`parser/c_ast/CAST_API.md` section 7.3). `lexical_refs()` finds those identifiers deterministically from the
tokens alone, so no reference is silently dropped:

- a candidate is an identifier token outside preprocessor directives (their names are recorded by the directive
  nodes: `MacroBodyRef`, `CondRef`) that its cursor does not explain: no cursor (skipped code,
  `RETAIN_EXCLUDED_CONDITIONAL_BLOCKS`), a reference cursor (`DECL_REF_EXPR`, `TYPE_REF`, `CALL_EXPR`,
  `MACRO_INSTANTIATION`, ...) whose referenced declaration is null, or the cursor of an enclosing statement,
  expression or declaration of another name (libclang's error recovery for an undeclared name), at a position no
  parser reference covers;
- skipped: member names (after `.` / `->`), labels (`name:` opening a statement), designators (`.name =`), the
  names a declaration introduces (their cursor is the declaration itself), and every use of a name the file
  declares (a parameter, local or field, also when its declaration failed: the identifier after an unresolved
  type name, `u32 v`, or a function-pointer declarator `(*fopen)(`);
- the role and target type follow from the neighbouring tokens: after `struct` / `union` / `enum` a `TypeUsage`
  of the tag (`C_structnotbind` & co.), before `(` a `Call` (`C_functionprotnotbind`), an all-capitals name a
  `MacroExpansion` (`CPPro_define`), a name followed by an identifier or `*` and an identifier a `TypeUsage` of a
  typedef (`C_SCtypedef`), anything else a `DeclRef` (`C_DeclRefExpr`).

`lexical_refs()` is pure (token tuples in, reference tuples out); `record_lexical_refs()` binds it to a C parse
(`parser/c_ast/c_ast.py` `TokenList.process_tokens()`), attributing each reference to the innermost tag around it.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from core.globalstuff import ASTT, STANDARD_C_KEYWORDS, SymbolRole

# Token kinds (parser/c_ast/tokenizer.py AST_KIND values, passed in by the binding)
KIND_IDENTIFIER = "identifier"
KIND_KEYWORD = "keyword"
KIND_PUNCT = "punctuation"
KIND_OTHER = "other"

# Cursor states of a token
CUR_NONE = 0          # no cursor (skipped code)
CUR_UNRESOLVED = 1    # a reference / macro instantiation whose target is null
CUR_RESOLVED = 2      # anything else (resolved references, expressions)
CUR_DECL = 3          # the name a declaration introduces (its cursor is the declaration)

_TAG_KEYWORDS = {"struct": ASTT.C_structnotbind, "union": ASTT.C_unionnotbind, "enum": ASTT.C_enumnotbind}
_NOT_NAMES = frozenset({
    "defined", "sizeof", "typeof", "__typeof__", "__typeof", "__attribute__", "__attribute", "__extension__",
    "__asm__", "__asm", "asm", "__volatile__", "__inline__", "__inline", "__restrict", "__restrict__",
    "__builtin_va_list", "__builtin_offsetof", "__builtin_expect", "__builtin_constant_p", "__label__",
    "__alignof__", "_Alignof", "_Static_assert", "_Generic", "__auto_type", "__int128", "__signed__",
})


def _is_name(text: str) -> bool:
    return bool(text) and (text[0].isalpha() or text[0] == "_") and text.replace("_", "a").isalnum()


_DECL_PREV = frozenset({"", ";", "{", "}", ",", "const", "volatile", "static", "extern", "inline", "register",
                        "signed", "unsigned", "__inline__", "__inline", "typedef", "__user", "__iomem"})
_DECL_END = frozenset({";", ",", ")", "=", "[", "(", ":"})


def _type_position(tokens: list[tuple[str, str, int, int, int]], i: int) -> bool:
    """True when the identifier at `i` names a type: it starts a declaration (`u32 x;`, `u32 *p,`) or a cast
    (`(u32)x`, `(u32 *)p`)."""
    prev = tokens[i - 1][0] if i > 0 else ""
    nxt = tokens[i + 1] if i + 1 < len(tokens) else None
    nxt2 = tokens[i + 2] if i + 2 < len(tokens) else None
    if nxt is None:
        return False
    if prev == "(" and (nxt[0] == ")" or (nxt[0] == "*" and nxt2 is not None and nxt2[0] in ("*", ")"))):
        # A cast `(u32)x` / `(u32 *)p`: the parenthesis is no call's and an operand follows it
        before = tokens[i - 2] if i > 1 else None
        if before is not None and (before[1] == KIND_IDENTIFIER or before[0] in (")", "]")):
            return False
        close = i + 1 if nxt[0] == ")" else next((j for j in range(i + 2, min(i + 6, len(tokens))) if tokens[j][0] == ")"), None)
        after = tokens[close + 1] if close is not None and close + 1 < len(tokens) else None
        return after is not None and (after[1] == KIND_IDENTIFIER or after[0] in ("(", "*", "&", "-", "~", "!") or
                                      after[0][:1].isdigit() or after[1] == KIND_OTHER)
    if prev not in _DECL_PREV and prev != "(":
        return False
    if nxt[1] == KIND_IDENTIFIER:
        return nxt2 is None or nxt2[0] in _DECL_END
    if nxt[0] == "*":
        return nxt2 is not None and (nxt2[1] == KIND_IDENTIFIER or nxt2[0] in ("*", "const", "__user", "__iomem"))
    return False


def lexical_refs(tokens: list[tuple[str, str, int, int, int]], covered: set[tuple[int, int]],
                 directive_lines: set[int]) -> list[tuple[str, int, int, int, int]]:
    """(name, line, column, role, target type) of the unresolved identifiers of a token stream.

    `tokens`: (spelling, kind, line, column, cursor state) in file order; `covered`: (line, column) of the
    references the parser recorded; `directive_lines`: lines of preprocessor directives (and their continuations).
    """
    out: list[tuple[str, int, int, int, int]] = []
    n = len(tokens)
    # Names the file declares itself (parameters and locals included, even when their type is unresolved):
    # a use of one is no reference to something defined elsewhere
    declared = {t[0] for t in tokens if t[1] == KIND_IDENTIFIER and t[4] == CUR_DECL}
    for i in range(n):
        if tokens[i][1] == KIND_IDENTIFIER and tokens[i][4] != CUR_RESOLVED and _type_position(tokens, i):
            j = i + 1
            while j < n and tokens[j][0] in ("*", "const", "volatile", "__user", "__iomem", "__rcu", "__percpu"):
                j += 1
            if j < n and tokens[j][1] == KIND_IDENTIFIER:
                declared.add(tokens[j][0])
        # A function-pointer declarator `(*name)(` names a field, parameter or variable being declared
        if (tokens[i][1] == KIND_IDENTIFIER and 1 < i < n - 2 and tokens[i - 1][0] == "*" and tokens[i - 2][0] == "("
                and tokens[i + 1][0] == ")" and tokens[i + 2][0] in ("(", "[")):
            declared.add(tokens[i][0])
    for i, (text, kind, line, col, state) in enumerate(tokens):
        if kind != KIND_IDENTIFIER or state in (CUR_RESOLVED, CUR_DECL) or line in directive_lines:
            continue
        if (line, col) in covered or text in _NOT_NAMES or text in STANDARD_C_KEYWORDS or not _is_name(text):
            continue
        prev = tokens[i - 1][0] if i > 0 else ""
        nxt = tokens[i + 1][0] if i + 1 < n else ""
        if prev in (".", "->"):
            continue
        if nxt == ":" and prev in ("", ";", "{", "}", ":") and (i + 2 >= n or tokens[i + 2][0] != ":"):
            continue  # a label opening a statement
        if prev in _TAG_KEYWORDS:
            out.append((text, line, col, int(SymbolRole.TypeUsage), int(_TAG_KEYWORDS[prev])))
        elif text.isupper() and any(c.isalpha() for c in text):
            out.append((text, line, col, int(SymbolRole.MacroExpansion), int(ASTT.CPPro_define)))
        elif text in declared:
            continue
        elif nxt == "(":
            out.append((text, line, col, int(SymbolRole.Call), int(ASTT.C_functionprotnotbind)))
        elif _type_position(tokens, i):
            out.append((text, line, col, int(SymbolRole.TypeUsage), int(ASTT.C_SCtypedef)))
        else:
            out.append((text, line, col, int(SymbolRole.DeclRef), int(ASTT.C_DeclRefExpr)))
    return out


def directive_lines_of(rawfile: Iterable[str]) -> set[int]:
    """1-based lines of preprocessor directives, with their backslash continuations."""
    out: set[int] = set()
    cont = False
    for idx, text in enumerate(rawfile, 1):
        stripped = text.lstrip()
        if cont or stripped.startswith("#"):
            out.add(idx)
            cont = text.rstrip().endswith("\\")
        else:
            cont = False
    return out


# ------------------------------------------------------------------------------------------------- binding
_REF_CURSOR_KINDS: frozenset[int] = frozenset({43, 47, 49, 50, 101, 102, 103, 502})  # TYPE_REF ... MACRO_INSTANTIATION
_NULL_KINDS: frozenset[int] = frozenset({70, 71, 72, 73, 350})  # invalid file / no decl / not implemented / invalid code / TU


def _cursor_state(cursor: Any, text: str) -> int:
    """How the token's cursor explains the identifier `text`: a resolved reference or the declaration or macro
    it names (CUR_RESOLVED), a reference whose target is null (CUR_UNRESOLVED), or nothing - no cursor, or one of
    an enclosing statement / expression / declaration of another name, as libclang gives an undeclared name in
    error recovery (CUR_NONE)."""
    if cursor is None:
        return CUR_NONE
    try:
        kind_id = cursor._kind_id
    except Exception:  # noqa: BLE001
        return CUR_RESOLVED
    if kind_id in _NULL_KINDS:
        return CUR_NONE
    if kind_id in _REF_CURSOR_KINDS:
        try:
            ref = cursor.referenced
        except Exception:  # noqa: BLE001
            ref = None
        if ref is None or ref.kind.is_invalid():
            return CUR_UNRESOLVED
        return CUR_RESOLVED
    try:
        spelling = cursor.spelling
    except Exception:  # noqa: BLE001
        spelling = ""
    if spelling == text and (cursor.kind.is_declaration() or kind_id in (501, 503, 201, 202)):
        return CUR_DECL  # the name a declaration / macro definition / label introduces
    return CUR_NONE


def record_lexical_refs(CS: Any, tokens_array: list[Any], rawfile: Iterable[str]) -> int:
    """Record the lexical references of a parsed C file (see the module docstring); returns how many."""
    from parser.c_ast.ast_nodes import record_name_refs
    from parser.c_ast.tokenizer import AST_KIND
    from core.SymbolRows import collector

    kinds = {AST_KIND.identifier: KIND_IDENTIFIER, AST_KIND.keyword: KIND_KEYWORD, AST_KIND.punctuation: KIND_PUNCT}
    toks: list[tuple[str, str, int, int, int]] = []
    for tok in tokens_array:
        kind = kinds.get(tok.ast_kind, KIND_OTHER)
        if kind == KIND_OTHER and tok.ast_kind == AST_KIND.comment:
            continue
        state = _cursor_state(tok._cursor, tok.spelling_str) if kind == KIND_IDENTIFIER else CUR_RESOLVED
        toks.append((tok.spelling_str, kind, int(tok.line.line_pos[0]), int(tok.line.char_pos[0]), state))
    col = collector(CS)
    covered: set[tuple[int, int]] = set()
    for p in col.placements.values():
        covered.update((item[3], item[4]) for item in p.refs)
    covered.update((item[3], item[4]) for _tag, item in col.loose_refs)
    found = lexical_refs(toks, covered, directive_lines_of(rawfile))
    if not found:
        return 0
    # Innermost tag around each line (the references are stored with that tag's items)
    spans = sorted(col.placements.values(), key=lambda p: p.line_e - p.line_s)
    by_role: dict[tuple[Any, int], list[tuple[str, int, int, int]]] = {}
    for name, line, column, role, target in found:
        owner = next((p.tag_ref for p in spans if p.line_s <= line <= p.line_e), 0)
        by_role.setdefault((owner, role), []).append((name, line, column, target))
    for (owner, role), names in by_role.items():
        record_name_refs(CS, owner, names, role)
    return len(found)
