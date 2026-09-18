"""parser/c_ast/c_ast.py - C-AST Parser Orchestrator & Entry Point.

Provides:
- c_ast_parse(CS): Main file lifecycle dispatcher for C parsing.
- process_c_ast(CS): Parse and stage C-AST constructs into ChangeSet.
- Ast_Manager: Translation unit manager coordinating Libclang parsing and tokenization.
- TokenList: High-speed ctypes token stream with in-place Latin-1 line slicing.
- get_prior_tags(CS), close_prior_tags(CS): Prior tag version lifecycle hooks.
"""
from __future__ import annotations
import re
import time
import ctypes
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any
import clang.cindex as cc

from core.globalstuff import (
    G,
    REF_C_AST,
    REF_NO_REF,
    REF_POS,
    FILE_ERROR,
    configure_logging,
    ASTT,
    STANDARD_C_KEYWORDS,
)
from parser.c_ast.ctypes_bindings import (
    AST_KIND,
    _CLANG_GET_EXTENT,
    _CLANG_GET_CURSOR_EXTENT,
    _CLANG_GET_RANGE_START,
    _CLANG_GET_RANGE_END,
    _CLANG_GET_SPELLING_LOC,
    _CLANG_GET_TOKEN_KIND,
    _CLANG_TOKEN_KIND_MAP,
    _CTYPES_F_PTR,
    _CTYPES_S_LINE,
    _CTYPES_S_COL,
    _CTYPES_S_OFF,
    _CTYPES_E_LINE,
    _CTYPES_E_COL,
    _CTYPES_E_OFF,
    _CTYPES_BYREF,
    _BYREF_F_PTR,
    _BYREF_S_LINE,
    _BYREF_S_COL,
    _BYREF_S_OFF,
    _BYREF_E_LINE,
    _BYREF_E_COL,
    _BYREF_E_OFF,
    safe_spelling,
    safe_cursor_spelling,
)
from parser.c_ast.tokenizer import (
    Line,
    get_cursor_line,
    TokenStream,
)
from parser.c_ast.ast_nodes import (
    Ast,
    End_Mode,
    Zone_Type,
    Ast_Comment,
    Ast_ASM_Directive,
    Ast_ASM_Macro,
    Ast_ASM_Instruction,
    Ast_ASM_Label,
    Ast_ASM_Comment,
    CPPro,
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
    CPPro_include,
    CPPro_embed,
    CPPro_line,
    CPPro_error,
    CPPro_warning,
    CPPro_pragma,
    CPPro_defined,
)
from parser.c_ast.cursor_tree import (
    Zone,
    C_Type,
    SemanticPartitioner,
    resolve_cppro_scopes,
    resolve_cursor_type_ast,
    get_decl_type,
    get_top_level_cursors,
)
from parser.c_ast.tag_tracker import (
    get_prior_tags,
    close_prior_tags,
    match_prior_tag_transition,
    check_exact_match,
)
from parser.c_ast.cs_extractor import CSExtractor

configure_logging(level=logging.INFO, fmt="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_WORKER_CLANG_INDEX: cc.Index | None = None

_COMMENT_PATTERN = re.compile(
    r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
    re.DOTALL | re.MULTILINE,
)


def _comment_replacer(match: Any) -> str:
    s = match.group(0)
    if s.startswith("/"):
        return "\n" * s.count("\n")
    return s


def comment_remover(text: str) -> str:
    """Remove comments from C source text preserving line numbers."""
    return _COMMENT_PATTERN.sub(_comment_replacer, text)


def c_ast_parse(CS: Any) -> None:
    """Entry point dispatching C file parsing into ChangeSet operations."""
    if CS.file_operation == "R100":
        return

    with CS(REF_C_AST):
        if CS.file_operation == "A":
            process_c_ast(CS)
        elif CS.file_operation == "M" or (CS.file_operation and CS.file_operation.startswith("R")):
            get_prior_tags(CS)
            process_c_ast(CS)
            close_prior_tags(CS)
        elif CS.file_operation == "D":
            get_prior_tags(CS)
            close_prior_tags(CS)
        else:
            logger.error(f"Unsupported file operation '{CS.file_operation}' in c_ast_parse")


def process_c_ast(CS: Any) -> None:
    """Parse source file into AST nodes and extract ChangeSet operations."""
    Ast_Manager(CS)


def extract_include_symbols(parsed_tu: cc.TranslationUnit, fullfilename: str, CS: Any) -> None:
    """Map direct #includes to the external symbols they import that are directly used in the file."""
    CS.include_symbols = defaultdict(list)
    CS.include_symbol_refs = {}

    target_real = os.path.realpath(fullfilename)
    direct_includes: dict[int, dict[str, Any]] = {}
    current_direct_line = None

    real_cache: dict[str, str] = {fullfilename: target_real}

    def get_real(fname: str) -> str:
        r = real_cache.get(fname)
        if r is None:
            r = os.path.realpath(fname)
            real_cache[fname] = r
        return r

    try:
        inclusions = parsed_tu.get_includes()
    except Exception:
        return

    for inc in inclusions:
        src = inc.source
        inc_file = inc.include
        if not src or not inc_file:
            continue
        src_name = src.name
        inc_name = inc_file.name
        if inc.depth == 1 and (src_name == fullfilename or get_real(src_name) == target_real):
            current_direct_line = inc.location.line if inc.location else None
            if current_direct_line is not None:
                r_inc = get_real(inc_name)
                direct_includes[current_direct_line] = {
                    "target": r_inc,
                    "target_name": inc_name,
                    "headers": {r_inc},
                }
        elif current_direct_line is not None and inc.depth > 1:
            direct_includes[current_direct_line]["headers"].add(get_real(inc_name))

    if not direct_includes:
        return

    header_to_direct_line: dict[str, int] = {
        data["target"]: line for line, data in direct_includes.items()
    }

    def find_include_line(header_real: str) -> int | None:
        if header_real in header_to_direct_line:
            return header_to_direct_line[header_real]
        for line in sorted(direct_includes.keys()):
            if header_real in direct_includes[line]["headers"]:
                return line
        return None

    # Map cursor kinds to ASTT
    cursor_kind_map = {
        cc.CursorKind.FUNCTION_DECL: ASTT.C_functionproto,
        cc.CursorKind.CXX_METHOD: ASTT.C_functionproto,
        cc.CursorKind.STRUCT_DECL: ASTT.C_struct,
        cc.CursorKind.UNION_DECL: ASTT.C_union,
        cc.CursorKind.ENUM_DECL: ASTT.C_enum,
        cc.CursorKind.ENUM_CONSTANT_DECL: ASTT.C_enumequal,
        cc.CursorKind.TYPEDEF_DECL: ASTT.C_SCtypedef,
        cc.CursorKind.VAR_DECL: ASTT.C_SCextern,
        cc.CursorKind.MACRO_DEFINITION: ASTT.CPPro_define,
    }

    found_symbols_by_line: dict[int, set[tuple[str, int]]] = defaultdict(set)

    def walk_file(node: cc.Cursor) -> None:
        f_obj = node.location.file
        if not f_obj:
            return
        f_name = f_obj.name
        if f_name != fullfilename and get_real(f_name) != target_real:
            return

        ref = node.referenced
        if ref:
            ref_f = ref.location.file
            if ref_f:
                ref_name = ref_f.name
                if ref_name != fullfilename:
                    ref_real = get_real(ref_name)
                    if ref_real != target_real:
                        sym_name = ref.spelling
                        if sym_name and sym_name not in STANDARD_C_KEYWORDS:
                            if not (sym_name.startswith("__builtin_") or sym_name.startswith("__attribute__")):
                                ast_type = cursor_kind_map.get(ref.kind)
                                if ast_type is not None:
                                    inc_line = find_include_line(ref_real)
                                    if inc_line is not None:
                                        found_symbols_by_line[inc_line].add((sym_name, ast_type))

        for ch in node.get_children():
            walk_file(ch)

    try:
        for c in parsed_tu.cursor.get_children():
            walk_file(c)
    except Exception as e:
        logger.debug(f"Error walking file-bounded cursors for include symbols: {e}")

    for line, syms_set in found_symbols_by_line.items():
        CS.include_symbols[line] = sorted(syms_set, key=lambda s: (s[1], s[0]))


class TokenList(TokenStream):
    """Token list maintaining API compatibility with TokenStream and legacy TokenList."""

    def __init__(
        self,
        parsed_tu: cc.TranslationUnit,
        fullfilename: str,
        rawfile: tuple[str, ...],
        file_size: int | None = None,
    ) -> None:
        super().__init__(parsed_tu, fullfilename, rawfile, file_size=file_size)
        self.rawfile = rawfile
        self.main_zone: Zone | None = None
        self.CS: Any = None

    def process_tokens(self, CS: Any) -> None:
        self.CS = CS
        G.CURRENT_RAWFILE = self.rawfile
        top_cursors = get_top_level_cursors(self.parsed_tu, self.fullfilename)
        self.main_zone = Zone(Zone_Type.Full_File, top_cursors, tokens_array=self.tokens_array)

        prof = getattr(CS, "profiler", None)
        t_proc_0 = time.perf_counter() if prof is not None else 0.0

        check_exec = self.main_zone.check_exec
        for token in self.tokens_array:
            check_exec(token, token._cursor, token.ast_kind)

        self.main_zone.gen_lined_dict()
        self.main_zone.resolve_cppro_scopes()

        if prof is not None:
            prof.token_processing_s = time.perf_counter() - t_proc_0

        CSExtractor.extract_zone(self.CS, self.main_zone)

        # Immediate teardown: release Clang AST, token stream, and intermediate structures
        self.main_zone = None
        self.tokens_array.clear()
        self.token_group = None
        self.parsed_tu = None
        self.rawfile = None
        if self.CS and hasattr(self.CS, "parsers"):
            self.CS.parsers.pop("C_AM", None)


class Ast_Manager:
    """Coordinates Clang TU parsing, tokenization, and ChangeSet extraction."""

    def __init__(self, CS: Any) -> None:
        self.mfdir = CS.mf.version_dict[CS.gp.Version_Name]
        CS.mfdir = self.mfdir
        G.CURRENT_PARSING_DIR = self.mfdir
        self.filename = CS.current_path
        self.fullfilename = f"{self.mfdir}/{self.filename}"
        G.CURRENT_PARSING_FILE = self.filename
        self.processing_list: list[Any] = []
        self.cppro_parse_result: list[Any] = []
        CS.parsers["C_AM"] = self
        self.Init_Parse(CS)

    def Init_Parse(self, CS: Any) -> None:
        try:
            unsplit_rawfile = Path(self.fullfilename).read_text(encoding="latin-1")
        except Exception as e:
            raise FILE_ERROR(e)

        # Invariant B: Strict \n split to prevent Form Feed (\x0c) line desynchronization
        self.rawfile = tuple(unsplit_rawfile.replace("\r\n", "\n").split("\n"))
        G.CURRENT_RAWFILE = self.rawfile
        file_byte_size = len(unsplit_rawfile)

        cppro_cindex_input = []
        if getattr(G, "OVERRIDE_CPPRO_CINDEX_INPUT", False):
            cppro_cindex_input = [
                line[6:].lstrip()
                for line in comment_remover(unsplit_rawfile).splitlines()
                if line.startswith("#ifdef")
            ]

        global _WORKER_CLANG_INDEX
        if _WORKER_CLANG_INDEX is None:
            _WORKER_CLANG_INDEX = cc.Index.create()
        index = _WORKER_CLANG_INDEX

        prof = getattr(CS, "profiler", None)
        t_parse_0 = time.perf_counter() if prof is not None else 0.0

        inc_dir = self.filename.rpartition("/")[0]

        translation_unit = index.parse(
            self.fullfilename,
            args=[
                "-ferror-limit=0",
                "-w",
                "-D__KERNEL__",
                *cppro_cindex_input,
                f"-I{self.mfdir}/{inc_dir}",
                f"-I{self.mfdir}/include",
                f"-I{self.mfdir}/include/uapi",
            ],
            options=(cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD + 32768),
        )

        extract_include_symbols(translation_unit, self.fullfilename, CS)

        if prof is not None:
            prof.clang_parse_tu_s = time.perf_counter() - t_parse_0
            t_tok_0 = time.perf_counter()

        self.TL = TokenList(translation_unit, self.fullfilename, self.rawfile, file_size=file_byte_size)
        TL = self.TL

        if prof is not None:
            prof.clang_tokenize_s = time.perf_counter() - t_tok_0

        TL.process_tokens(CS)
        self.TL = None
        self.rawfile = None
        self.processing_list.clear()
        self.cppro_parse_result.clear()
