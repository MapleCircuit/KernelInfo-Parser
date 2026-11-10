"""parser/asm_ast/asm_ast.py - Native Assembly Parser Orchestrator.

Provides fast, accurate AST extraction for .S and .s assembly source files.
"""
from __future__ import annotations

import time
from pathlib import Path
import clang.cindex as cc

from core.SymbolRows import finish as finish_symbol_rows
from core.globalstuff import (
    G,
    COLOR,
    REF_ROOT,
    REF_OLD,
    REF_C_AST,
    REF_NO_REF,
    FILE_ERROR,
    ASTT,
    SymbolLinkage,
)
from typing import Any
ChangeSetType = Any
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


def asm_ast_parse(CS: ChangeSetType) -> None:
    """Entry point for parsing assembly (.S, .s) files into ChangeSet operations."""
    if CS.file_operation == "R100":
        return

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
        try:
            self.unsplit_rawfile = Path(self.fullfilename).read_text(encoding="latin-1")
        except Exception as e:
            raise FILE_ERROR(e)

        self.rawfile = tuple(self.unsplit_rawfile.split("\n"))

        global _WORKER_ASM_CLANG_INDEX
        if _WORKER_ASM_CLANG_INDEX is None:
            _WORKER_ASM_CLANG_INDEX = cc.Index.create()
        index = _WORKER_ASM_CLANG_INDEX

        prof = getattr(CS, "profiler", None)
        if prof is not None:
            t_parse_0 = time.perf_counter()

        G.CURRENT_RAWFILE = self.rawfile
        parts = self.filename.split("/")
        arch_args = []
        if len(parts) > 1 and parts[0] == "arch":
            arch = parts[1]
            arch_args.extend([
                f"-I{self.mfdir}/arch/{arch}/include",
                f"-I{self.mfdir}/arch/{arch}/include/uapi",
                f"-I{self.mfdir}/arch/{arch}/include/generated",
                f"-I{self.mfdir}/arch/{arch}/include/generated/uapi",
            ])

        args = [
            "-w",
            "-x",
            "assembler-with-cpp",
            "-D__KERNEL__",
            "-D__ASSEMBLY__",
            f"-I{self.mfdir}/{'/'.join(self.filename.split('/')[:-1])}",
            *arch_args,
            f"-I{self.mfdir}/include",
            f"-I{self.mfdir}/include/uapi",
        ]

        parsed_tu = index.parse(
            self.fullfilename,
            args=args,
            options=(cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD + 32768),
        )

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
        })

        LINKAGE_END_MACROS = frozenset({
            "SYM_FUNC_END", "SYM_FUNC_END_ALIAS", "SYM_CODE_END", "SYM_DATA_END", "SYM_DATA_END_LABEL", "ENDPROC", "END",
        })

        CPP_DIRECTIVES = frozenset({
            "include", "include_next", "define", "undef", "if", "ifdef", "ifndef",
            "else", "elif", "elifdef", "elifndef", "endif", "pragma", "error", "warning", "line", "ident",
        })

        func_stack: list[Ast_ASM_Label] = []

        while i < n:
            token = tokens_array[i]
            cursor = cursors_array[i]
            ast_kind = token.ast_kind
            tspelling = token.spelling_str
            tline = token.line

            # Comments
            if ast_kind == AST_KIND.comment:
                self.children.append(Ast_ASM_Comment(tline, tspelling))
                i += 1
                continue

            # Check if previous child can continue absorbing token
            if self.children and self.children[-1].within_range(token, ast_kind):
                self.children[-1].exec_filter(token, cursor, ast_kind)
                i += 1
                continue

            # Preprocessor directives & # comments
            if ast_kind == AST_KIND.punctuation and tspelling == "#":
                next_tok = tokens_array[i + 1] if i + 1 < n else None
                same_line = next_tok and next_tok.line.line_pos[0] == tline.line_pos[0]
                if same_line and next_tok.spelling_str in CPP_DIRECTIVES:
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
                    # Assembly line comment starting with #
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

            # Macro definitions (.macro name ... .endm)
            if ast_kind == AST_KIND.punctuation and tspelling == "." and i + 1 < n and tokens_array[i + 1].spelling_str == "macro":
                macro_name = tokens_array[i + 2].spelling_str if i + 2 < n else "macro"
                macro_line = Line(tline)
                macro_line.grow(tokens_array[i + 1].line)
                if i + 2 < n:
                    macro_line.grow(tokens_array[i + 2].line)
                self.children.append(Ast_ASM_Macro(macro_line, macro_name))
                i += 3
                continue

            # Assembler directives starting with '.' (e.g. .section, .align, .globl, .text)
            if ast_kind == AST_KIND.punctuation and tspelling == ".":
                self.children.append(Ast_ASM_Directive(tline, "."))
                i += 1
                continue

            # Linkage macros: ENTRY(name), GLOBAL(name), SYM_FUNC_START*(name), SYM_CODE_START*(name)
            if tspelling in LINKAGE_START_MACROS and i + 2 < n and tokens_array[i + 1].spelling_str == "(":
                sym_name = tokens_array[i + 2].spelling_str
                close_idx = i + 3
                while close_idx < n and tokens_array[close_idx].spelling_str != ")" and tokens_array[close_idx].line.line_pos[0] == tline.line_pos[0]:
                    close_idx += 1
                if close_idx < n and tokens_array[close_idx].spelling_str == ")":
                    entry_ext = Line(tline)
                    entry_ext.grow(tokens_array[close_idx].line)
                    if "LOCAL" in tspelling:
                        linkage = SymbolLinkage.InternalStaticDef
                    elif "WEAK" in tspelling or tspelling == "WEAK":
                        linkage = SymbolLinkage.WeakOrAliasDef
                    else:
                        linkage = SymbolLinkage.ExternalDef
                    open_func = Ast_ASM_Label(entry_ext, sym_name, linkage=linkage)
                    self.children.append(open_func)
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
                    self.children.append(Ast_ASM_Instruction(end_ext, tspelling))
                    i = close_idx + 1
                    continue

            # Labels (token followed by ':' on the same line)
            if i + 1 < n and tokens_array[i + 1].spelling_str == ":" and tokens_array[i + 1].line.line_pos[0] == tline.line_pos[0]:
                label_ext = Line(tline)
                label_ext.grow(tokens_array[i + 1].line)
                is_num = tspelling.isdigit()
                self.children.append(Ast_ASM_Label(label_ext, tspelling, is_numeric=is_num))
                i += 2
                continue

            # General assembly statement / instruction
            self.children.append(Ast_ASM_Instruction(tline, tspelling))
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
