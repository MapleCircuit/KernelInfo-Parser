"""parser/asm_ast/asm_ast.py - Native Assembly Parser Orchestrator.

Provides fast, accurate AST extraction for .S and .s assembly source files.
"""
from __future__ import annotations

import time
from pathlib import Path
import clang.cindex as cc

from core.SymbolRows import finish as finish_symbol_rows, record_def as record_symbol_def
from core.globalstuff import (
    G,
    COLOR,
    REF_ROOT,
    REF_OLD,
    REF_POS,
    REF_C_AST,
    REF_NO_REF,
    FILE_ERROR,
    ASTT,
    SymbolLinkage,
    SymbolRole,
)
from collections import namedtuple
from typing import Any

from parser.asm_ast.asm_pure_extractor import asm_comment_chars, looks_like_gas_macro
from parser.c_ast.ast_nodes import _ASM_BRANCH_MNEMONICS, _ASM_REGISTER_NAMES, record_name_refs

ChangeSetType = Any
from parser.c_ast.c_ast import stage_parse_diagnostics
from parser.c_ast.cursor_tree import clear_tu_caches
from parser.c_ast import (
    AST_KIND,
    Line,
    Ast_Comment,
    Ast_ASM_Macro,
    Ast_ASM_Directive,
    Ast_ASM_Instruction,
    Ast_ASM_Label,
    Ast_ASM_Comment,
    CPPro,
    CPPro_include,
    CPPro_if,
    CPPro_elif,
    CPPro_else,
    CPPro_endif,
    CPPro_ifdef,
    CPPro_ifndef,
    CPPro_elifdef,
    CPPro_elifndef,
    CPPro_define,
    CPPro_undef,
    CPPro_line,
    CPPro_error,
    CPPro_warning,
    CPPro_pragma,
    End_Mode,
    get_cursor_line,
    TokenList,
    get_prior_tags,
    resolve_cppro_scopes,
    _CLANG_GET_EXTENT,
    _CLANG_GET_RANGE_START,
    _CLANG_GET_RANGE_END,
    _CLANG_GET_SPELLING_LOC,
    _CTYPES_F_PTR,
    _CTYPES_S_LINE,
    _CTYPES_S_COL,
    _CTYPES_S_OFF,
    _CTYPES_E_LINE,
    _CTYPES_E_COL,
    _CTYPES_E_OFF,
    _CTYPES_BYREF,
    _CLANG_GET_TOKEN_KIND,
    _CLANG_TOKEN_KIND_MAP,
)

_WORKER_ASM_CLANG_INDEX: cc.Index | None = None

# One token of an assembly statement: kind is "id", "p" (punctuation), "lit" or "comment";
# macro is True when libclang reports the token as the use of a C preprocessor macro.
AsmTok = namedtuple("AsmTok", "text kind line col end macro")

_KIND_NAMES = {AST_KIND.identifier: "id", AST_KIND.keyword: "id", AST_KIND.punctuation: "p", AST_KIND.literal: "lit", AST_KIND.comment: "comment"}
_INSTRUCTION_PREFIXES = frozenset({"rep", "repe", "repz", "repne", "repnz", "lock", "notrack", "data16", "addr32", "bnd", "xacquire", "xrelease"})
_DATA_DIRECTIVES = frozenset({"long", "quad", "word", "int", "short", "8byte", "4byte", "2byte", "dword", "xword", "ptr", "pointer", "rva", "uleb128", "sleb128"})
_OPERAND_KEYWORDS = frozenset({
    "lsl", "lsr", "asr", "ror", "rrx", "sxtw", "uxtw", "sxtx", "uxtx", "sxtb", "sxth", "uxtb", "uxth",
    "ptr", "offset", "byte", "word", "dword", "qword", "short", "near", "far", "st", "rn", "rz",
})


def _tok(token: Any, cursor: Any) -> AsmTok:
    """The `AsmTok` of a parser token and its libclang cursor."""
    kind = _KIND_NAMES.get(token.ast_kind, "p")
    macro = False
    if kind == "id":
        try:
            macro = cursor.kind == cc.CursorKind.MACRO_INSTANTIATION and cursor.spelling == token.spelling_str
        except Exception:  # noqa: BLE001 - an unreadable cursor is not a macro use
            macro = False
    ln, cp = token.line.line_pos, token.line.char_pos
    return AsmTok(token.spelling_str, kind, int(ln[0]), int(cp[0]), int(cp[1]), macro)


def _adjacent(a: AsmTok, b: AsmTok) -> bool:
    return a.line == b.line and a.end == b.col


def asm_node_refs(
    toks: list[AsmTok],
    comment_chars: tuple[str, ...],
    params: frozenset[str] = frozenset(),
    gas_macros: set[str] | frozenset[str] = frozenset(),
) -> list[tuple[str, int, int, int, int]]:
    """References made by the tokens of one assembly node: [(name, line, col, SymbolRole, target ASTT)].

    A line comment (`comment_chars`, `//`) ends the statement part of its line. Statements split at `;`.
    Per statement: a C preprocessor macro use is a `MacroExpansion` of a `CPPro_define`; a leading
    identifier naming a GAS `.macro` (defined in the file, or unknown and macro-shaped) is a
    `MacroExpansion` of an `ASM_Macro` whose argument identifiers are `DeclRef`s; the identifier operands
    of a branch or call are `Call` refs; the other operand identifiers are `DeclRef`s of an `ASM_Label`.
    Registers, `%reg`, `\\param` uses of `.macro` parameters, relocation suffixes (`@PLT`), local labels
    (`.L1`, `1f`) and `name=` keyword arguments are not references."""
    kept: list[AsmTok] = []
    cut_line = -1
    for k, t in enumerate(toks):
        if t.kind == "comment" or t.line == cut_line:
            continue
        if t.kind == "p" and (
            t.text in comment_chars
            or (t.text == "/" and k + 1 < len(toks) and toks[k + 1].text == "/" and _adjacent(t, toks[k + 1]))
        ):
            cut_line = t.line
            continue
        kept.append(t)

    statements: list[list[AsmTok]] = [[]]
    for t in kept:
        if t.kind == "p" and t.text == ";":
            statements.append([])
        else:
            statements[-1].append(t)

    out: list[tuple[str, int, int, int, int]] = []
    seen: set[tuple[str, int, int, int]] = set()

    def add(t: AsmTok, role: int, target: int) -> None:
        key = (t.text, t.line, t.col, role)
        if key not in seen:
            seen.add(key)
            out.append((t.text, t.line, t.col, role, target))

    for stmt in statements:
        if not stmt:
            continue
        for t in stmt:
            if t.macro:
                add(t, int(SymbolRole.MacroExpansion), int(ASTT.CPPro_define))
        idx = 0
        mode = "operands"   # how the identifiers after the leading word are read
        directive = ""
        if stmt[0].kind == "p" and stmt[0].text == "." and len(stmt) > 1 and stmt[1].kind == "id":
            directive = stmt[1].text.lower()
            if (
                directive in ("equ", "set", "equiv")
                and len(stmt) > 3
                and stmt[2].kind == "id"
                and stmt[3].kind == "p"
                and stmt[3].text == ","
            ):
                idx = 4
                mode = "data"
            else:
                idx = 2
                mode = "data" if directive in _DATA_DIRECTIVES else "skip"
        elif stmt[0].kind == "id":
            if (
                len(stmt) >= 2
                and stmt[1].kind == "p"
                and stmt[1].text == "="
                and not (len(stmt) >= 3 and stmt[2].kind == "p" and stmt[2].text == "=")
            ):
                idx = 2
                mode = "operands"
            else:
                while stmt[idx].text.lower() in _INSTRUCTION_PREFIXES and idx + 1 < len(stmt) and stmt[idx + 1].kind == "id":
                    idx += 1
                lead = stmt[idx]
                idx += 1
                if lead.macro:
                    mode = "skip"
                elif lead.text in gas_macros or (lead.text not in _ASM_BRANCH_MNEMONICS and looks_like_gas_macro(lead.text)
                                                 and lead.text.lower() not in _ASM_REGISTER_NAMES):
                    add(lead, int(SymbolRole.MacroExpansion), int(ASTT.ASM_Macro))
                    mode = "args"
                elif lead.text.lower() in _ASM_BRANCH_MNEMONICS:
                    mode = "branch"
        if mode == "skip":
            continue
        for j in range(idx, len(stmt)):
            t = stmt[j]
            if t.kind != "id" or t.macro or t.text in params:
                continue
            prev = stmt[j - 1] if j > 0 else None
            nxt = stmt[j + 1] if j + 1 < len(stmt) else None
            if prev is not None and _adjacent(prev, t) and (prev.text in ("%", "\\", ".", "@") or prev.kind == "lit"):
                continue
            if nxt is not None and _adjacent(t, nxt) and nxt.text in ("\\", "="):
                continue
            low = t.text.lower()
            if low in _ASM_REGISTER_NAMES or low in _OPERAND_KEYWORDS or not t.text.isidentifier():
                continue
            if t.text.isupper() and nxt is not None and nxt.text == "(" and _adjacent(t, nxt):
                continue   # an upper-case function-like macro use libclang could not resolve
            if len(t.text) > 1 and t.text[0] == "L" and t.text[1].isdigit():
                continue
            if mode == "branch":
                add(t, int(SymbolRole.Call), int(ASTT.ASM_Instruction))
            else:
                add(t, int(SymbolRole.DeclRef), int(ASTT.ASM_Label))
    return out


class _TokenRefs:
    """Mixin of the assembly nodes that keep their tokens to record references once their tag exists."""

    toks: list[AsmTok]
    comment_chars: tuple[str, ...]
    params: frozenset[str]
    gas_macros: set[str]
    extra_refs: list[tuple[str, int, int, int, int]]

    def _init_refs(self, first: AsmTok | None, comment_chars: tuple[str, ...], params: frozenset[str], gas_macros: set[str]) -> None:
        self.toks = [first] if first is not None else []
        self.comment_chars = comment_chars
        self.params = params
        self.gas_macros = gas_macros
        self.extra_refs = []

    def exec_filter(self, token: Any, cursor: Any, kind: int) -> None:
        self.toks.append(_tok(token, cursor))
        super().exec_filter(token, cursor, kind)  # type: ignore[misc]

    def _record_refs(self, CS: ChangeSetType) -> None:
        tag_ref = getattr(self, "tag_ref", 0)
        if not tag_ref:
            return
        refs = [*self.extra_refs, *asm_node_refs(self.toks, self.comment_chars, self.params, self.gas_macros)]
        by_role: dict[int, list[tuple[str, int, int, int]]] = {}
        for name, line, col, role, target in refs:
            by_role.setdefault(role, []).append((name, line, col, target))
        for role, names in by_role.items():
            record_name_refs(CS, tag_ref, names, role)


class Asm_Instruction(_TokenRefs, Ast_ASM_Instruction):
    """Instruction or statement node recording `Call`, `DeclRef` and `MacroExpansion` references."""

    def extract(self, CS: ChangeSetType, create_tag: bool = True) -> None:
        parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
        if parser_obj and hasattr(parser_obj, "rawfile"):
            self.extent.cc(parser_obj.rawfile)
        self.extract_1arg(CS, ASTT.ASM_Instruction, (self.mnemonic or self.extent.code)[:255], self.extent, create_tag=create_tag)
        self._record_refs(CS)


class Asm_Directive(_TokenRefs, Ast_ASM_Directive):
    """Directive node recording the macro uses and the data operands of `.long` & co."""

    def extract(self, CS: ChangeSetType, create_tag: bool = True) -> None:
        parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
        if parser_obj and hasattr(parser_obj, "rawfile"):
            self.extent.cc(parser_obj.rawfile)
        self.extract_1arg(CS, ASTT.ASM_Directive, (self.payload or self.extent.code)[:255], self.extent, create_tag=create_tag)
        def_sym = getattr(self, "def_sym", None)
        if create_tag and def_sym and not def_sym.isdigit():
            safe_sym = str(def_sym)[:255]
            if hasattr(CS, "symbol_dict") and isinstance(self.ast_ref, tuple) and len(self.ast_ref) == 3:
                CS.symbol_dict[(safe_sym, ASTT.ASM_Label)] = self.ast_ref[2][1]
            with CS(REF_POS):
                record_symbol_def(
                    CS,
                    getattr(self, "tag_ref", 0),
                    self.ast_ref,
                    safe_sym,
                    ASTT.ASM_Label,
                    int(SymbolLinkage.InternalStaticDef),
                    self.extent.line_pos[0],
                    self.extent.line_pos[1],
                )
        self._record_refs(CS)


class Asm_Label(_TokenRefs, Ast_ASM_Label):
    """Label node; a linkage macro (`ENTRY(name)`) adds the macro's own `MacroExpansion` reference."""

    def extract(self, CS: ChangeSetType, create_tag: bool = True) -> None:
        parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
        if parser_obj and hasattr(parser_obj, "rawfile"):
            self.extent.cc(parser_obj.rawfile)
        self.extract_1arg(CS, ASTT.ASM_Label, self.label[:255], self.extent, create_tag=create_tag)
        self._record_refs(CS)


class Asm_Macro(Ast_ASM_Macro):
    """`.macro` definition whose extent ends at its `.endm` (set by `Asm_Manager.process_tokens`); the body
    tokens are parsed as statements of their own."""

    def __init__(self, extent: Line | None = None, name: str = "") -> None:
        super().__init__(extent, name)
        self.need_processing = False

    def within_range(self, token: Any, ast_kind: int) -> bool:
        return False


def asm_ast_parse(CS: ChangeSetType) -> None:
    """Entry point for parsing assembly (.S, .s) files into ChangeSet operations."""
    if CS.file_operation == "R100":
        return

    try:
        with CS(REF_C_AST):
            if CS.file_operation == "A":
                Asm_Manager(CS)
            elif CS.file_operation == "M" or (CS.file_operation and CS.file_operation.startswith("R")):
                get_prior_tags(CS)
                Asm_Manager(CS)
            elif CS.file_operation == "D":
                pass  # Tags are immutable content records: a deleted file stages nothing
        # Emit the symbol definitions/references recorded while parsing (core/SymbolRows.py)
        finish_symbol_rows(CS)
        stage_parse_diagnostics(CS)
    finally:
        CS.compile_env = None
        CS.compile_arch = None
        CS.parse_diagnostics = None


class Asm_Manager:
    def __init__(self, CS: ChangeSetType) -> None:
        self.mfdir = CS.mf.version_dict[CS.gp.Version_Name]
        self.filename = CS.current_path
        self.fullfilename = f"{self.mfdir}/{self.filename}"
        G.CURRENT_PARSING_FILE = self.filename
        self.children = []
        CS.parsers["ASM_AM"] = self
        self.Init_Parse(CS)

    def Init_Parse(self, CS: ChangeSetType) -> None:
        parsed_tu = None
        TL = None
        clear_tu_caches()
        try:
            try:
                self.unsplit_rawfile = Path(self.fullfilename).read_text(encoding="latin-1")
            except Exception as e:
                raise FILE_ERROR(e)

            self.rawfile = tuple(self.unsplit_rawfile.replace("\r\n", "\n").split("\n"))

            global _WORKER_ASM_CLANG_INDEX
            if _WORKER_ASM_CLANG_INDEX is None:
                _WORKER_ASM_CLANG_INDEX = cc.Index.create()
            index = _WORKER_ASM_CLANG_INDEX

            prof = getattr(CS, "profiler", None)
            if prof is not None:
                t_parse_0 = time.perf_counter()

            G.CURRENT_RAWFILE = self.rawfile
            # The assembler-with-cpp compilation `make ARCH=<arch>` runs (parser/compile_env.py)
            from parser import compile_env  # lazy: parser.arch_layout imports the C parser through kconfig_ast

            env = compile_env.env_for(self.mfdir)
            CS.compile_env = env
            env_args, extra_options, arch_env = env.args_for(self.filename, asm=True)
            CS.compile_arch = arch_env
            parsed_tu = index.parse(
                self.fullfilename,
                args=["-w", *env_args],
                options=(cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD + 32768) | extra_options,
            )
            CS.parse_diagnostics = compile_env.diagnostics_fact(parsed_tu, arch_env, asm=True)

            if prof is not None:
                prof.clang_parse_tu_s = time.perf_counter() - t_parse_0
                t_tok_0 = time.perf_counter()

            TL = TokenList(parsed_tu, self.fullfilename, self.rawfile)

            if prof is not None:
                prof.clang_tokenize_s = time.perf_counter() - t_tok_0
                t_proc_0 = time.perf_counter()

            self.process_tokens(TL.tokens_array, TL.cursors_array)

            if prof is not None:
                prof.token_processing_s = time.perf_counter() - t_proc_0
                t_extract_0 = time.perf_counter()

            self.extract(CS)

            if prof is not None:
                prof.ast_extraction_s = time.perf_counter() - t_extract_0
        finally:
            TL = None
            parsed_tu = None
            cc.Cursor._tu = None
            clear_tu_caches()

    def process_tokens(self, tokens_array: list, cursors_array: list) -> None:
        """Process assembly and preprocessor tokens into AST nodes."""
        i = 0
        n = len(tokens_array)
        G.CURRENT_RAWFILE = self.rawfile

        LINKAGE_START_MACROS = frozenset({
            "ENTRY", "GLOBAL", "KENTRY",
            "SYM_FUNC_START", "SYM_FUNC_START_NOALIGN", "SYM_FUNC_START_LOCAL", "SYM_FUNC_START_LOCAL_NOALIGN", "SYM_FUNC_START_WEAK",
            "SYM_FUNC_START_ALIAS", "SYM_FUNC_START_WEAK_ALIAS",
            "SYM_CODE_START", "SYM_CODE_START_NOALIGN", "SYM_CODE_START_LOCAL", "SYM_CODE_START_LOCAL_NOALIGN", "SYM_CODE_START_WEAK",
            "SYM_TYPED_FUNC_START", "SYM_DATA_START", "SYM_DATA_START_LOCAL", "SYM_DATA_START_WEAK", "WEAK",
            "SYM_INNER_LABEL", "SYM_DATA", "SYM_DATA_LOCAL",
        })
        SINGLE_LINE_LINKAGE_MACROS = frozenset({"SYM_INNER_LABEL", "SYM_DATA", "SYM_DATA_LOCAL"})

        LINKAGE_END_MACROS = frozenset({
            "SYM_FUNC_END", "SYM_FUNC_END_ALIAS", "SYM_CODE_END", "SYM_DATA_END", "SYM_DATA_END_LABEL", "ENDPROC", "END",
        })

        CPP_DIRECTIVES = frozenset({
            "include", "include_next", "define", "undef", "if", "ifdef", "ifndef",
            "else", "elif", "elifdef", "elifndef", "endif", "pragma", "error", "warning", "line", "ident",
        })

        func_stack: list[Ast_ASM_Label] = []
        comment_chars = asm_comment_chars(self.filename)
        line_comment_chars = frozenset(c for c in comment_chars if len(c) == 1)
        gas_macros: set[str] = set()
        macro_stack: list[tuple[Asm_Macro, frozenset[str]]] = []
        no_params: frozenset[str] = frozenset()

        def first_on_line(idx: int) -> bool:
            return idx == 0 or tokens_array[idx - 1].line.line_pos[1] < tokens_array[idx].line.line_pos[0]

        def make(cls: type, extent: Line, text: str, idx: int, *args: Any, **kwargs: Any) -> Any:
            """A statement node of class `cls` that keeps its tokens (the first is token `idx`) for reference extraction."""
            node = cls(extent, text, *args, **kwargs)
            node._init_refs(_tok(tokens_array[idx], cursors_array[idx]), comment_chars,
                            macro_stack[-1][1] if macro_stack else no_params, gas_macros)
            return node

        while i < n:
            token = tokens_array[i]
            cursor = cursors_array[i]
            ast_kind = token.ast_kind
            tspelling = token.spelling_str
            tline = token.line

            # Check if previous child can continue absorbing token (before comment handling so /* ... */
            # inside a multiline #define is absorbed by CPPro instead of splitting the #define)
            if self.children and self.children[-1].within_range(token, ast_kind):
                self.children[-1].exec_filter(token, cursor, ast_kind)
                i += 1
                continue

            # Comments
            if ast_kind == AST_KIND.comment:
                self.children.append(Ast_ASM_Comment(tline, tspelling))
                i += 1
                continue

            # Preprocessor directives & line comments
            if ast_kind == AST_KIND.punctuation and (tspelling == "#" or (tspelling in line_comment_chars and tspelling != ";")):
                next_tok = tokens_array[i + 1] if i + 1 < n else None
                same_line = next_tok and next_tok.line.line_pos[0] == tline.line_pos[0]
                if tspelling == "#" and first_on_line(i) and same_line and next_tok.spelling_str in CPP_DIRECTIVES:
                    kind = cursor.kind
                    if kind == cc.CursorKind.INCLUSION_DIRECTIVE:
                        inc_node = CPPro_include(get_cursor_line(cursor))
                        inc_node._check_inc_cursor(cursor)
                        self.children.append(inc_node)
                    else:
                        self.children.append(CPPro(tline))
                    i += 1
                    continue
                else:
                    # Assembly line comment starting with `#` or the architecture's comment character
                    # Consume all remaining tokens on this line into Ast_ASM_Comment
                    comment_line = Line(tline)
                    j = i + 1
                    while j < n and tokens_array[j].line.line_pos[0] == tline.line_pos[0]:
                        comment_line.grow(tokens_array[j].line)
                        j += 1
                    if hasattr(self, "rawfile") and self.rawfile:
                        comment_line.cc(self.rawfile)
                    self.children.append(Ast_ASM_Comment(comment_line, comment_line.code))
                    i = j
                    continue

            # Macro definitions (.macro name params ... .endm); the parameters are not references
            if ast_kind == AST_KIND.punctuation and tspelling == "." and i + 1 < n and tokens_array[i + 1].spelling_str == "macro":
                macro_name = tokens_array[i + 2].spelling_str if i + 2 < n else "macro"
                macro_line = Line(tline)
                macro_line.grow(tokens_array[i + 1].line)
                j = i + 2
                if j < n:
                    macro_line.grow(tokens_array[j].line)
                    j += 1
                params: set[str] = set()
                last_line = macro_line.line_pos[1]
                while j < n:
                    pt = tokens_array[j]
                    pl = pt.line.line_pos[0]
                    if pl != last_line and not (pl == last_line + 1 and self.rawfile[last_line - 1].rstrip().endswith("\\")):
                        break
                    prev_text = tokens_array[j - 1].spelling_str
                    if pt.ast_kind in (AST_KIND.identifier, AST_KIND.keyword) and prev_text not in ("=", ":", "\\"):
                        params.add(pt.spelling_str)
                    macro_line.grow(pt.line)
                    last_line = pt.line.line_pos[1]
                    j += 1
                macro_node = Asm_Macro(macro_line, macro_name)
                self.children.append(macro_node)
                gas_macros.add(macro_name)
                macro_stack.append((macro_node, frozenset(params)))
                i = j
                continue

            # Assembler directives starting with '.' (e.g. .section, .align, .globl, .text)
            if ast_kind == AST_KIND.punctuation and tspelling == ".":
                if macro_stack and i + 1 < n and tokens_array[i + 1].spelling_str == "endm":
                    macro_stack.pop()[0].extent.grow(tokens_array[i + 1].line)
                dir_node = make(Asm_Directive, tline, ".", i)
                if (
                    i + 3 < n
                    and tokens_array[i + 1].spelling_str.lower() in ("equ", "set", "equiv")
                    and tokens_array[i + 2].ast_kind in (AST_KIND.identifier, AST_KIND.keyword)
                    and tokens_array[i + 3].spelling_str == ","
                    and tokens_array[i + 3].line.line_pos[0] == tline.line_pos[0]
                ):
                    dir_node.def_sym = tokens_array[i + 2].spelling_str
                self.children.append(dir_node)
                i += 1
                continue

            # Linkage macros: ENTRY(name), GLOBAL(name), SYM_FUNC_START*(name), SYM_CODE_START*(name),
            # SYM_INNER_LABEL(name, linkage), SYM_DATA(name, ...), SYM_DATA_LOCAL(name, ...)
            if (
                tspelling in LINKAGE_START_MACROS
                and i + 2 < n
                and tokens_array[i + 1].spelling_str == "("
                and tokens_array[i + 2].ast_kind in (AST_KIND.identifier, AST_KIND.keyword)
            ):
                sym_name = tokens_array[i + 2].spelling_str
                close_idx = i + 2
                depth = 1
                while close_idx < n and tokens_array[close_idx].line.line_pos[0] == tline.line_pos[0]:
                    sp = tokens_array[close_idx].spelling_str
                    if sp == "(":
                        depth += 1
                    elif sp == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    close_idx += 1
                if close_idx < n and depth == 0 and tokens_array[close_idx].spelling_str == ")":
                    entry_ext = Line(tline)
                    entry_ext.grow(tokens_array[close_idx].line)
                    if tspelling == "SYM_INNER_LABEL":
                        arg_text = " ".join(tokens_array[k].spelling_str for k in range(i + 3, close_idx))
                        if "LOCAL" in arg_text:
                            linkage = SymbolLinkage.InternalStaticDef
                        elif "WEAK" in arg_text:
                            linkage = SymbolLinkage.WeakOrAliasDef
                        else:
                            linkage = SymbolLinkage.ExternalDef
                    elif "LOCAL" in tspelling:
                        linkage = SymbolLinkage.InternalStaticDef
                    elif "WEAK" in tspelling or tspelling == "WEAK":
                        linkage = SymbolLinkage.WeakOrAliasDef
                    else:
                        linkage = SymbolLinkage.ExternalDef
                    open_func = make(Asm_Label, entry_ext, sym_name, i, linkage=linkage)
                    open_func.extra_refs.append((tspelling, int(tline.line_pos[0]), int(tline.char_pos[0]), int(SymbolRole.MacroExpansion), int(ASTT.CPPro_define)))
                    open_func.toks = []
                    self.children.append(open_func)
                    if tspelling not in SINGLE_LINE_LINKAGE_MACROS:
                        func_stack.append(open_func)
                    i = close_idx + 1
                    continue

            # Linkage end macros: SYM_FUNC_END*(name), SYM_CODE_END*(name), ENDPROC(name), END(name)
            if tspelling in LINKAGE_END_MACROS and i + 2 < n and tokens_array[i + 1].spelling_str == "(":
                sym_name = tokens_array[i + 2].spelling_str
                close_idx = i + 3
                while close_idx < n and tokens_array[close_idx].spelling_str != ")" and tokens_array[close_idx].line.line_pos[0] == tline.line_pos[0]:
                    close_idx += 1
                if close_idx < n and tokens_array[close_idx].spelling_str == ")":
                    end_ext = Line(tline)
                    end_ext.grow(tokens_array[close_idx].line)
                    for s_idx in range(len(func_stack) - 1, -1, -1):
                        if func_stack[s_idx].label == sym_name:
                            matched = func_stack.pop(s_idx)
                            matched.extent.grow(tokens_array[close_idx].line)
                            break
                    end_node = make(Asm_Instruction, end_ext, tspelling, i)
                    end_node.toks[0] = end_node.toks[0]._replace(macro=True)
                    self.children.append(end_node)
                    i = close_idx + 1
                    continue

            # Labels (identifier/keyword or numeric literal followed by ':' on the same line, not preceded by '\')
            if (
                (ast_kind in (AST_KIND.identifier, AST_KIND.keyword) or (ast_kind == AST_KIND.literal and tspelling.isdigit()))
                and not (i > 0 and tokens_array[i - 1].spelling_str == "\\" and tokens_array[i - 1].line.line_pos[0] == tline.line_pos[0])
                and i + 1 < n
                and tokens_array[i + 1].spelling_str == ":"
                and tokens_array[i + 1].line.line_pos[0] == tline.line_pos[0]
            ):
                label_ext = Line(tline)
                label_ext.grow(tokens_array[i + 1].line)
                is_num = tspelling.isdigit()
                self.children.append(make(Asm_Label, label_ext, tspelling, i, is_numeric=is_num))
                self.children[-1].toks = []
                i += 2
                continue

            # Symbol assignment: SYM = expr (not ==)
            if (
                ast_kind in (AST_KIND.identifier, AST_KIND.keyword)
                and (
                    first_on_line(i)
                    or (i > 0 and tokens_array[i - 1].spelling_str == ";" and tokens_array[i - 1].line.line_pos[0] == tline.line_pos[0])
                )
                and i + 1 < n
                and tokens_array[i + 1].spelling_str == "="
                and tokens_array[i + 1].line.line_pos[0] == tline.line_pos[0]
                and not (i + 2 < n and tokens_array[i + 2].spelling_str == "=" and tokens_array[i + 2].line.line_pos[0] == tline.line_pos[0])
            ):
                assign_ext = Line(tline)
                assign_node = make(Asm_Label, assign_ext, tspelling, i, linkage=SymbolLinkage.InternalStaticDef)
                j = i + 1
                last_line = assign_ext.line_pos[1]
                while j < n:
                    nt = tokens_array[j]
                    nl = nt.line.line_pos[0]
                    if nl != last_line and not (
                        hasattr(self, "rawfile") and self.rawfile
                        and all(0 <= l < len(self.rawfile) and self.rawfile[l].rstrip().endswith("\\") for l in range(last_line - 1, nl - 1))
                    ):
                        break
                    if nt.ast_kind == AST_KIND.comment:
                        if nt.spelling_str.startswith("//"):
                            break
                        j += 1
                        continue
                    if nt.ast_kind == AST_KIND.punctuation and nt.spelling_str == ";":
                        j += 1
                        break
                    if nt.ast_kind == AST_KIND.punctuation and nt.spelling_str in line_comment_chars and nt.spelling_str != ";":
                        break
                    assign_ext.grow(nt.line)
                    assign_node.toks.append(_tok(nt, cursors_array[j]))
                    last_line = nt.line.line_pos[1]
                    j += 1
                self.children.append(assign_node)
                i = j
                continue

            # General assembly statement / instruction
            self.children.append(make(Asm_Instruction, tline, tspelling, i))
            i += 1

        self.resolve_cppro_scopes()

    def resolve_cppro_scopes(self) -> None:
        """Resolve ending coordinates for preprocessor conditionals."""
        resolve_cppro_scopes(self.children)

    def extract(self, CS: ChangeSetType) -> None:
        """Extract all AST nodes to ChangeSet operations."""
        for item in self.children:
            with CS(REF_NO_REF):
                item.extract(CS)
