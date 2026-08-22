"""parser/asm_ast/asm_ast.py - Native Assembly Parser Orchestrator.

Provides fast, accurate AST extraction for .S and .s assembly source files.
"""
from __future__ import annotations

import time
import ctypes
from pathlib import Path
from typing import TYPE_CHECKING
import clang.cindex as cc

from core.globalstuff import (
    G,
    COLOR,
    REF_ROOT,
    REF_OLD,
    REF_C_AST,
    REF_NO_REF,
    FILE_ERROR,
    ASTT,
)
from typing import Any
ChangeSetType = Any
from parser.c_ast.c_ast_type import (
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
)
from parser.c_ast.c_ast import (
    TokenList,
    get_prior_tags,
    close_prior_tags,
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
    with CS(REF_C_AST):
        if CS.file_operation == "A":
            Asm_Manager(CS)
        elif CS.file_operation == "M" or CS.file_operation[0] == "R":
            get_prior_tags(CS)
            Asm_Manager(CS)
            close_prior_tags(CS)
        elif CS.file_operation == "D":
            get_prior_tags(CS)
            close_prior_tags(CS)


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

        args = [
            "-w",
            "-x",
            "assembler-with-cpp",
            "-D__KERNEL__",
            "-D__ASSEMBLY__",
            f"-I{self.mfdir}/{'/'.join(self.filename.split('/')[:-1])}",
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
        while i < n:
            token = tokens_array[i]
            cursor = cursors_array[i]
            ast_kind = token.ast_kind
            tspelling = token.spelling_str
            tline = token.line

            # Comments
            if ast_kind == AST_KIND.comment:
                self.children.append(Ast_Comment(tline, tspelling))
                i += 1
                continue

            # Check if previous child can continue absorbing token
            if self.children and self.children[-1].within_range(token, ast_kind):
                self.children[-1].exec_filter(token, cursor, ast_kind)
                i += 1
                continue

            # Preprocessor directives & # comments
            if ast_kind == AST_KIND.punctuation and tspelling == "#":
                kind = cursor.kind
                if kind == cc.CursorKind.INCLUSION_DIRECTIVE:
                    self.children.append(CPPro_include(get_cursor_line(cursor)))
                else:
                    self.children.append(CPPro(tline))
                i += 1
                continue

            # Assembler directives starting with '.' (e.g. .macro, .set, .align, .global, .text)
            if ast_kind == AST_KIND.punctuation and tspelling == ".":
                self.children.append(Ast_ASM_Directive(tline, "."))
                i += 1
                continue

            # Labels (token followed by ':' on the same line)
            if i + 1 < n and tokens_array[i + 1].spelling_str == ":" and tokens_array[i + 1].line.line_pos[0] == tline.line_pos[0]:
                label_ext = Line(tline)
                label_ext.grow(tokens_array[i + 1].line)
                self.children.append(Ast_ASM_Label(label_ext, tspelling))
                i += 2
                continue

            # General assembly statement / instruction
            self.children.append(Ast_ASM_Instruction(tline, tspelling))
            i += 1

        self.resolve_cppro_scopes()

    def resolve_cppro_scopes(self) -> None:
        """Resolve ending coordinates for preprocessor conditionals."""
        cpp_stack = []
        for item in self.children:
            if isinstance(item, (CPPro_if, CPPro_ifdef, CPPro_ifndef)) and not isinstance(item, (CPPro_elif, CPPro_elifdef, CPPro_elifndef)):
                cpp_stack.append([item])
            elif isinstance(item, (CPPro_elif, CPPro_elifdef, CPPro_elifndef)):
                if cpp_stack:
                    prev_branch = cpp_stack[-1][-1]
                    end_l = max(prev_branch.extent.line_pos[0], item.extent.line_pos[0] - 1)
                    prev_branch.endif = Line(end_l, end_l)
                    cpp_stack[-1].append(item)
            elif isinstance(item, CPPro_else):
                if cpp_stack:
                    prev_branch = cpp_stack[-1][-1]
                    end_l = max(prev_branch.extent.line_pos[0], item.extent.line_pos[0] - 1)
                    prev_branch.endif = Line(end_l, end_l)
                    cpp_stack[-1].append(item)
            elif isinstance(item, CPPro_endif):
                if cpp_stack:
                    branches = cpp_stack.pop()
                    for branch in branches:
                        if not hasattr(branch, "endif") or branch.endif.line_pos[0] == 0:
                            branch.endif = item.extent

    def extract(self, CS: ChangeSetType) -> None:
        """Extract all AST nodes to ChangeSet operations."""
        for item in self.children:
            with CS(REF_NO_REF):
                item.extract(CS)
