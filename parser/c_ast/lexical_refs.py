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
CUR_PARAM_DECL = 4    # the name a parameter declaration (PARM_DECL) introduces

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


def _build_token_scopes(
    tokens: list[tuple[str, str, int, int, int]],
    directive_lines: set[int],
    rawfile: Iterable[str] | None = None,
) -> tuple[list[int | None], list[set[str]], list[int], set[int]]:
    """Build a scope tree `(parents, decls_by_scope, token_scope, extra_decl_indices)` over `tokens`.

    Scope 0 is file scope. Function parameters and locals live in their function/block scopes (and prototype
    parameters or struct/union field declarations never enter file or function scopes), so a parameter or local
    in one function never masks an unresolved reference with the same name in another function or at file scope.
    """
    n = len(tokens)

    # Identify which token indices are declaration names via _type_position or (*name)(
    _TYPE_KW = frozenset({"void", "char", "short", "int", "long", "float", "double", "signed", "unsigned", "_Bool", "bool"})
    _QUAL_OR_PTR = frozenset({"*", "const", "volatile", "restrict", "__restrict", "__restrict__", "__user", "__iomem", "__rcu", "__percpu"})
    extra_decl_indices: set[int] = set()
    fn_ptr_indices: set[int] = set()
    for i in range(n):
        if tokens[i][2] in directive_lines:
            continue
        if tokens[i][1] == KIND_IDENTIFIER and tokens[i][4] != CUR_RESOLVED and _type_position(tokens, i):
            j = i + 1
            while j < n and tokens[j][0] in _QUAL_OR_PTR:
                j += 1
            if j < n and tokens[j][1] == KIND_IDENTIFIER:
                extra_decl_indices.add(j)
        if (tokens[i][1] == KIND_IDENTIFIER and 1 < i < n - 2 and tokens[i - 1][0] == "*" and tokens[i - 2][0] == "("
                and tokens[i + 1][0] == ")" and tokens[i + 2][0] in ("(", "[")):
            extra_decl_indices.add(i)
            fn_ptr_indices.add(i)
            if tokens[i + 2][0] == "(":
                depth = 1
                k = i + 3
                while k < n and depth > 0:
                    if tokens[k][0] == "(":
                        depth += 1
                    elif tokens[k][0] == ")":
                        depth -= 1
                    elif depth == 1 and (
                        tokens[k][0] in _TYPE_KW
                        or (tokens[k][0] in ("struct", "union", "enum") and k + 1 < n and tokens[k + 1][1] == KIND_IDENTIFIER)
                    ):
                        m = k + 2 if tokens[k][0] in ("struct", "union", "enum") else k + 1
                        while m < n and (tokens[m][0] in _TYPE_KW or tokens[m][0] in _QUAL_OR_PTR):
                            m += 1
                        if m < n and tokens[m][1] == KIND_IDENTIFIER and tokens[m][4] != CUR_RESOLVED:
                            if m + 1 >= n or tokens[m + 1][0] in _DECL_END:
                                extra_decl_indices.add(m)
                    k += 1

    # When rawfile is available, use c_scope.extract_scopes for #if/#else-balanced function and block spans
    if rawfile is not None:
        try:
            from parser.c_ast.c_scope import extract_scopes
            raw_list = rawfile if isinstance(rawfile, (list, tuple)) else list(rawfile)
            model = extract_scopes("\n".join(raw_list))
        except Exception:  # noqa: BLE001
            model = None
        if model and model.get("scopes"):
            cs_scopes = model["scopes"]
            cs_funcs = model.get("functions") or []
            parents = [s.get("parent") for s in cs_scopes]
            decls: list[set[str]] = [set() for _ in cs_scopes]
            spans: list[tuple[tuple[int, int], tuple[int, int], int, int]] = []
            for sid, s in enumerate(cs_scopes):
                if sid == 0:
                    continue
                sp = s.get("span") or [0, 0, 0, 0]
                ls, cs_col, le, ce_col = int(sp[0] or 0), int(sp[1] or 1), int(sp[2] or 0), int(sp[3] or 1)
                fn_idx = s.get("fn")
                if s.get("kind") == "function" and isinstance(fn_idx, int) and 0 <= fn_idx < len(cs_funcs):
                    fn_ls = int(cs_funcs[fn_idx].get("line_s") or ls)
                    if fn_ls < ls:
                        ls, cs_col = fn_ls, 1
                spans.append(((ls, cs_col), (le, ce_col), le - ls, sid))
            # Sort innermost (smallest line span, deepest sid) first
            spans.sort(key=lambda item: (item[2], -item[3]))

            # Spans covering each line, innermost first (a token tests only the spans of its own line)
            spans_by_line: dict[int, list[tuple[tuple[int, int], tuple[int, int], int, int]]] = {}
            for span in spans:
                for span_line in range(span[0][0], span[1][0] + 1):
                    spans_by_line.setdefault(span_line, []).append(span)

            def scope_at(line: int, col: int) -> int:
                pos = (line, col)
                for start, end, _len, sid in spans_by_line.get(line, ()):
                    if start <= pos <= end:
                        return sid
                return 0

            for v in model.get("vars") or ():
                if v.get("kind") in ("param", "local", "static_local"):
                    sc = v.get("scope")
                    vname = v.get("name")
                    if vname and isinstance(sc, int) and 0 <= sc < len(decls):
                        decls[sc].add(vname)

            # Track struct/union body spans and top-level parentheses so struct fields and prototype params
            # never enter file scope decls[0], while function names declared at paren_depth == 0 enter decls[0]
            tok_scope = [0] * n
            in_struct_depth = 0
            pending_struct = False
            paren_depth = 0
            for i, (text, kind, line, col, state) in enumerate(tokens):
                if line in directive_lines:
                    continue
                sid = scope_at(line, col)
                tok_scope[i] = sid
                if kind == KIND_KEYWORD and text in ("struct", "union") and paren_depth == 0 and sid == 0:
                    j = i + 1
                    if j < n and tokens[j][1] == KIND_IDENTIFIER:
                        j += 1
                    if j < n and tokens[j][0] == "{":
                        pending_struct = True
                elif kind == KIND_PUNCT:
                    if text == "(":
                        paren_depth += 1
                    elif text == ")" and paren_depth > 0:
                        paren_depth -= 1
                    elif text == "{":
                        paren_depth = 0
                        if sid == 0 and (pending_struct or in_struct_depth > 0):
                            in_struct_depth += 1
                            pending_struct = False
                    elif text == "}":
                        paren_depth = 0
                        if sid == 0 and in_struct_depth > 0:
                            in_struct_depth -= 1
                    elif text == ";" and paren_depth == 0:
                        pending_struct = False
                if kind == KIND_IDENTIFIER and (state in (CUR_DECL, CUR_PARAM_DECL) or i in extra_decl_indices):
                    if sid == 0:
                        if in_struct_depth > 0 or state == CUR_PARAM_DECL:
                            continue
                        if paren_depth > 0 and state != CUR_DECL and i not in fn_ptr_indices:
                            continue
                        decls[0].add(text)
                    else:
                        if state != CUR_PARAM_DECL and paren_depth == 0 and cs_scopes[sid].get("kind") == "function":
                            fn_idx = cs_scopes[sid].get("fn")
                            body = cs_funcs[fn_idx].get("body") if isinstance(fn_idx, int) and 0 <= fn_idx < len(cs_funcs) else None
                            if body and (line, col) < (int(body[0] or 0), int(body[1] or 1)):
                                decls[0].add(text)
                                continue
                        decls[sid].add(text)
            return parents, decls, tok_scope, extra_decl_indices

    parents = [None]
    kinds: list[str] = ["file"]
    decls = [set()]
    tok_scope = [0] * n
    cur = 0
    paren_depth = 0
    pending_params: set[str] = set()
    pending_param_indices: list[int] = []
    pending_tag_kind: str = ""

    for i, (text, kind, line, _col, state) in enumerate(tokens):
        if line in directive_lines:
            tok_scope[i] = cur
            continue
        if kind == KIND_KEYWORD and text in ("struct", "union", "enum") and paren_depth == 0:
            j = i + 1
            if j < n and tokens[j][1] == KIND_IDENTIFIER:
                j += 1
            if j < n and tokens[j][0] == "{":
                pending_tag_kind = text
        elif kind == KIND_PUNCT:
            if text == "(":
                paren_depth += 1
            elif text == ")":
                if paren_depth > 0:
                    paren_depth -= 1
            elif text == "{":
                paren_depth = 0
                if pending_tag_kind in ("struct", "union"):
                    new_kind = "struct"
                    pending_tag_kind = ""
                elif pending_tag_kind == "enum":
                    new_kind = "enum"
                    pending_tag_kind = ""
                elif kinds[cur] == "file":
                    new_kind = "func"
                else:
                    new_kind = "block"
                new_id = len(parents)
                parents.append(cur)
                kinds.append(new_kind)
                if new_kind in ("func", "block"):
                    decls.append(set(pending_params))
                    extra_decl_indices.update(pending_param_indices)
                    pending_params.clear()
                    pending_param_indices.clear()
                else:
                    decls.append(set())
                cur = new_id
            elif text == "}":
                paren_depth = 0
                pending_tag_kind = ""
                if parents[cur] is not None:
                    cur = parents[cur]  # type: ignore[assignment]
            elif text == ";" and paren_depth == 0:
                pending_tag_kind = ""
                if kinds[cur] == "file":
                    pending_params.clear()
                    pending_param_indices.clear()

        tok_scope[i] = cur
        if kind == KIND_IDENTIFIER and (state in (CUR_DECL, CUR_PARAM_DECL) or i in extra_decl_indices):
            if kinds[cur] == "struct":
                continue
            if kinds[cur] == "enum":
                decls[0].add(text)
            elif state == CUR_PARAM_DECL or (paren_depth > 0 and kinds[cur] == "file" and state != CUR_DECL and i not in fn_ptr_indices):
                if kinds[cur] in ("func", "block"):
                    decls[cur].add(text)
                else:
                    pending_params.add(text)
                    pending_param_indices.append(i)
            else:
                decls[cur].add(text)

    return parents, decls, tok_scope, extra_decl_indices


def _is_declared_in_scope(name: str, scope_id: int, parents: list[int | None], decls: list[set[str]]) -> bool:
    cur: int | None = scope_id
    while cur is not None:
        if name in decls[cur]:
            return True
        cur = parents[cur]
    return False


def _is_candidate(text: str, kind: str, line: int, col: int, state: int, covered: set[tuple[int, int]],
                  directive_lines: set[int]) -> bool:
    """The token-local conditions of a candidate (the scope and neighbour checks of `lexical_refs()` only drop more)."""
    return (kind == KIND_IDENTIFIER and state in (CUR_NONE, CUR_UNRESOLVED) and line not in directive_lines
            and (line, col) not in covered and text not in _NOT_NAMES and text not in STANDARD_C_KEYWORDS
            and _is_name(text))


def lexical_refs(tokens: list[tuple[str, str, int, int, int]], covered: set[tuple[int, int]],
                 directive_lines: set[int],
                 rawfile: Iterable[str] | None = None) -> list[tuple[str, int, int, int, int]]:
    """(name, line, column, role, target type) of the unresolved identifiers of a token stream.

    `tokens`: (spelling, kind, line, column, cursor state) in file order; `covered`: (line, column) of the
    references the parser recorded; `directive_lines`: lines of preprocessor directives (and their continuations).
    """
    out: list[tuple[str, int, int, int, int]] = []
    n = len(tokens)
    if not any(_is_candidate(text, kind, line, col, state, covered, directive_lines)
               for text, kind, line, col, state in tokens):
        return out  # every identifier is explained: no scope model needed
    parents, decls, tok_scope, extra_decl_indices = _build_token_scopes(tokens, directive_lines, rawfile)
    for i, (text, kind, line, col, state) in enumerate(tokens):
        if kind != KIND_IDENTIFIER or state in (CUR_RESOLVED, CUR_DECL, CUR_PARAM_DECL) or i in extra_decl_indices or line in directive_lines:
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
        elif _is_declared_in_scope(text, tok_scope[i], parents, decls):
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
    if kind_id == 6:  # FIELD_DECL: struct/union field declarations do not bind variable names in scope
        return CUR_RESOLVED
    if kind_id == 10 and spelling == text:  # PARM_DECL: function parameter declaration
        return CUR_PARAM_DECL
    if spelling == text and (cursor.kind.is_declaration() or kind_id in (501, 503, 201, 202)):
        return CUR_DECL  # the name a declaration / macro definition / label introduces
    return CUR_NONE


def record_lexical_refs(CS: Any, tokens_array: list[Any], rawfile: Iterable[str]) -> int:
    """Record the lexical references of a parsed C file (see the module docstring); returns how many."""
    from parser.c_ast.ast_nodes import record_name_refs
    from parser.c_ast.tokenizer import AST_KIND
    from core.SymbolRows import collector

    col = collector(CS)
    covered: set[tuple[int, int]] = set()
    for p in col.placements.values():
        covered.update((item[3], item[4]) for item in p.refs)
    covered.update((item[3], item[4]) for _tag, item in col.loose_refs)
    directive_lines = directive_lines_of(rawfile)

    # Fast path: the cursor state (libclang calls) of only the identifiers no reference covers; when none of them
    # is unexplained, the file has no lexical reference and the full token conversion and scope model are skipped
    identifier = AST_KIND.identifier
    states: dict[int, int] = {}
    for idx, tok in enumerate(tokens_array):
        if tok.ast_kind != identifier:
            continue
        text = tok.spelling_str
        line = int(tok.line.line_pos[0])
        column = int(tok.line.char_pos[0])
        if (line in directive_lines or (line, column) in covered or text in _NOT_NAMES or text in STANDARD_C_KEYWORDS
                or not _is_name(text)):
            continue
        states[idx] = _cursor_state(tok._cursor, text)
    if not any(state in (CUR_NONE, CUR_UNRESOLVED) for state in states.values()):
        return 0

    kinds = {AST_KIND.identifier: KIND_IDENTIFIER, AST_KIND.keyword: KIND_KEYWORD, AST_KIND.punctuation: KIND_PUNCT}
    toks: list[tuple[str, str, int, int, int]] = []
    for idx, tok in enumerate(tokens_array):
        kind = kinds.get(tok.ast_kind, KIND_OTHER)
        if kind == KIND_OTHER and tok.ast_kind == AST_KIND.comment:
            continue
        if kind == KIND_IDENTIFIER:
            state = states.get(idx)
            if state is None:
                state = _cursor_state(tok._cursor, tok.spelling_str)
        else:
            state = CUR_RESOLVED
        toks.append((tok.spelling_str, kind, int(tok.line.line_pos[0]), int(tok.line.char_pos[0]), state))
    found = lexical_refs(toks, covered, directive_lines, rawfile)
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
