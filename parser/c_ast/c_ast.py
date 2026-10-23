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
    _CLANG_GET_CURSOR_LOC,
    _CLANG_GET_FILE_LOC,
    _CLANG_CURSOR_IS_NULL,
    _CLANG_GET_CURSOR_REFERENCED,
    _CLANG_VISIT_CHILDREN,
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
    _VALID_TOP_KINDS,
    filter_enclosing_cursors,
    clear_tu_caches,
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

    clear_tu_caches()
    try:
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
    finally:
        clear_tu_caches()


def process_c_ast(CS: Any) -> None:
    """Parse source file into AST nodes and extract ChangeSet operations."""
    Ast_Manager(CS)


_CURSOR_KIND_ID_MAP: dict[int, ASTT] = {
    cc.CursorKind.FUNCTION_DECL.value: ASTT.C_functionproto,
    cc.CursorKind.CXX_METHOD.value: ASTT.C_functionproto,
    cc.CursorKind.STRUCT_DECL.value: ASTT.C_struct,
    cc.CursorKind.UNION_DECL.value: ASTT.C_union,
    cc.CursorKind.ENUM_DECL.value: ASTT.C_enum,
    cc.CursorKind.ENUM_CONSTANT_DECL.value: ASTT.C_enumequal,
    cc.CursorKind.TYPEDEF_DECL.value: ASTT.C_SCtypedef,
    cc.CursorKind.VAR_DECL.value: ASTT.C_SCextern,
    cc.CursorKind.MACRO_DEFINITION.value: ASTT.CPPro_define,
}

_REFERENCING_KIND_IDS: frozenset[int] = frozenset({
    43,   # TYPE_REF
    47,   # MEMBER_REF
    48,   # LABEL_REF
    49,   # OVERLOADED_DECL_REF
    50,   # VARIABLE_REF
    100,  # UNEXPOSED_EXPR
    101,  # DECL_REF_EXPR
    102,  # MEMBER_REF_EXPR
    103,  # CALL_EXPR
    501,  # MACRO_DEFINITION
    502,  # MACRO_INSTANTIATION
    503,  # INCLUSION_DIRECTIVE
})


def extract_include_symbols(parsed_tu: cc.TranslationUnit, fullfilename: str, CS: Any) -> list[cc.Cursor]:
    """Map direct #includes to the external symbols they import that are directly used in the file,
    and return pre-filtered top-level declaration cursors for TokenList processing."""
    CS.include_symbols = defaultdict(list)
    CS.include_symbol_refs = {}
    CS.resolved_includes_by_line = {}

    target_file = cc.File.from_name(parsed_tu, fullfilename)
    target_ptr = ctypes.cast(target_file._as_parameter_, ctypes.c_void_p).value if target_file else None

    target_real = os.path.realpath(fullfilename)
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
        inclusions = ()

    ptr_to_direct_line: dict[int, int] = {}
    current_direct_line = None

    for inc in inclusions:
        inc_file = inc.include
        if not inc_file:
            continue
        inc_ptr = ctypes.cast(inc_file._as_parameter_, ctypes.c_void_p).value
        if inc.depth == 1:
            src = inc.source
            if not src:
                continue
            src_ptr = ctypes.cast(src._as_parameter_, ctypes.c_void_p).value
            if src_ptr == target_ptr or (target_ptr is None and (src.name == fullfilename or get_real(src.name) == target_real)):
                current_direct_line = inc.location.line if inc.location else None
                if current_direct_line is not None:
                    ptr_to_direct_line[inc_ptr] = current_direct_line
                    inc_name = inc_file.name
                    r_inc = get_real(inc_name)
                    base_dir = getattr(CS, "mfdir", None) or getattr(G, "CURRENT_PARSING_DIR", None)
                    if base_dir:
                        try:
                            rel_inc = os.path.relpath(r_inc, base_dir)
                        except Exception:
                            rel_inc = inc_name
                    else:
                        rel_inc = inc_name
                    if not rel_inc.startswith(".."):
                        CS.resolved_includes_by_line[current_direct_line] = rel_inc
        elif current_direct_line is not None and inc.depth > 1:
            ptr_to_direct_line[inc_ptr] = current_direct_line

    found_symbols_by_line: dict[int, set[tuple[str, int]]] = defaultdict(set)
    f_ptr = ctypes.c_void_p()
    u_val = ctypes.c_uint()
    byref = ctypes.byref
    get_loc = _CLANG_GET_CURSOR_LOC
    get_file_loc = _CLANG_GET_FILE_LOC
    is_null = _CLANG_CURSOR_IS_NULL
    get_ref = _CLANG_GET_CURSOR_REFERENCED
    visit_children = _CLANG_VISIT_CHILDREN

    def visitor(cursor: Any, parent: Any, client_data: Any) -> int:
        k_id = cursor._kind_id
        if k_id in _REFERENCING_KIND_IDS:
            ref = get_ref(cursor)
            if not is_null(ref):
                loc = get_loc(ref)
                get_file_loc(loc, byref(f_ptr), byref(u_val), byref(u_val), byref(u_val))
                ref_ptr = f_ptr.value
                if ref_ptr and ref_ptr != target_ptr:
                    inc_line = ptr_to_direct_line.get(ref_ptr)
                    if inc_line is not None:
                        ast_type = _CURSOR_KIND_ID_MAP.get(ref._kind_id)
                        if ast_type is not None:
                            cursor._tu = parsed_tu
                            ref._tu = parsed_tu
                            sym_name = ref.spelling
                            if sym_name and sym_name not in STANDARD_C_KEYWORDS:
                                if not (sym_name.startswith("__builtin_") or sym_name.startswith("__attribute__")):
                                    found_symbols_by_line[inc_line].add((sym_name, ast_type))
        return 2  # CXChildVisit_Recurse

    cb_func = cc.cursor_visit_callback(visitor)

    top_decl_cursors: list[cc.Cursor] = []
    try:
        for c in parsed_tu.cursor.get_children():
            loc = get_loc(c)
            get_file_loc(loc, byref(f_ptr), byref(u_val), byref(u_val), byref(u_val))
            if f_ptr.value == target_ptr or (target_ptr is None and c.location.file and (c.location.file.name == fullfilename or get_real(c.location.file.name) == target_real)):
                visitor(c, parsed_tu.cursor, None)
                visit_children(c, cb_func, None)
                if c.kind in _VALID_TOP_KINDS:
                    top_decl_cursors.append(c)
    except Exception as e:
        logger.debug(f"Error walking file-bounded cursors for include symbols: {e}")

    for line, syms_set in found_symbols_by_line.items():
        CS.include_symbols[line] = sorted(syms_set, key=lambda s: (s[1], s[0]))

    return filter_enclosing_cursors(top_decl_cursors)


class TokenList(TokenStream):
    """Token list maintaining API compatibility with TokenStream and legacy TokenList."""

    def __init__(
        self,
        parsed_tu: cc.TranslationUnit,
        fullfilename: str,
        rawfile: tuple[str, ...],
        file_size: int | None = None,
        top_cursors: list[cc.Cursor] | None = None,
    ) -> None:
        super().__init__(parsed_tu, fullfilename, rawfile, file_size=file_size)
        self.rawfile = rawfile
        self.main_zone: Zone | None = None
        self.CS: Any = None
        self.top_cursors = top_cursors

    def process_tokens(self, CS: Any) -> None:
        self.CS = CS
        G.CURRENT_RAWFILE = self.rawfile
        top_cursors = self.top_cursors if self.top_cursors is not None else get_top_level_cursors(self.parsed_tu, self.fullfilename)
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
        self.top_cursors = None
        if self.CS and hasattr(self.CS, "parsers"):
            self.CS.parsers.pop("C_AM", None)


_CLANG_KERNEL_MACRO_DEFINES: tuple[str, ...] = (
    "-D__KERNEL__",
    "-D__user=",
    "-D__init=",
    "-D__initdata=",
    "-D__initconst=",
    "-D__exit=",
    "-D__exitdata=",
    "-D__sched=",
    "-D__weak=",
    "-D__read_mostly=",
    "-D__maybe_unused=",
    "-D__always_unused=",
    "-D__percpu=",
    "-D__rcu=",
    "-D__force=",
    "-D__bitwise=",
    "-D__bitwise__=",
    "-D__nocast=",
    "-D__iomem=",
    "-D__must_check=",
    "-D__deprecated=",
    "-D__cpuinit=",
    "-D__cpuinitdata=",
    "-D__meminit=",
    "-D__meminitdata=",
    "-Dnoinline=",
    "-D__always_inline=",
    "-Dnotrace=",
    "-D__kprobes=",
    "-D__ref=",
    "-D__pure=",
    "-D__const=",
    "-D__noreturn=",
    "-D__malloc=",
    "-Dasmlinkage=",
    "-DFASTCALL=",
    "-D__cold=",
    "-D__hot=",
    "-D__visible=",
    "-D__releases(x)=",
    "-D__acquires(x)=",
    "-D__acquire(x)=",
    "-D__release(x)=",
    "-D__cond_lock(x,c)=(c)",
    "-D__cacheline_aligned=",
    "-D__cacheline_aligned_in_smp=",
    "-D____cacheline_aligned=",
    "-D____cacheline_aligned_in_smp=",
    "-D__aligned(x)=",
    "-Daligned(x)=",
    "-D__devinit=",
    "-D__devinitdata=",
    "-D__devinitconst=",
    "-D__devexit=",
    "-D__devexitdata=",
    "-D__devexitconst=",
    "-D__net_init=",
    "-D__net_initdata=",
    "-D__net_exit=",
    "-D__net_exitdata=",
    "-D__paginginit=",
    "-D__init_refok=",
    "-D__initdata_refok=",
    "-D__section(x)=",
    "-D__irq_entry=",
    "-Dmodule_init(x)=",
    "-Dmodule_exit(x)=",
    "-Dcore_initcall(x)=",
    "-Dpostcore_initcall(x)=",
    "-Darch_initcall(x)=",
    "-Dsubsys_initcall(x)=",
    "-Dfs_initcall(x)=",
    "-Ddevice_initcall(x)=",
    "-Dlate_initcall(x)=",
    "-D__initcall(x)=",
    "-Dconsole_initcall(x)=",
    "-Dsecurity_initcall(x)=",
    "-Dlate_initcall_sync(x)=",
    "-Dpure_initcall(x)=",
    "-Drootfs_initcall(x)=",
    "-DRESERVE_BRK(name,sz)=",
)


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
                *_CLANG_KERNEL_MACRO_DEFINES,
                *cppro_cindex_input,
                f"-I{self.mfdir}/{inc_dir}",
                f"-I{self.mfdir}/include",
                f"-I{self.mfdir}/include/uapi",
                f"-I{self.mfdir}/arch/x86/include",
                f"-I{self.mfdir}/arch/x86/include/uapi",
            ],
            options=(cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD + 32768 + 16384),
        )

        top_cursors = extract_include_symbols(translation_unit, self.fullfilename, CS)

        if prof is not None:
            prof.clang_parse_tu_s = time.perf_counter() - t_parse_0
            t_tok_0 = time.perf_counter()

        self.TL = TokenList(translation_unit, self.fullfilename, self.rawfile, file_size=file_byte_size, top_cursors=top_cursors)
        TL = self.TL

        if prof is not None:
            prof.clang_tokenize_s = time.perf_counter() - t_tok_0

        TL.process_tokens(CS)
        self.TL = None
        self.rawfile = None
        self.processing_list.clear()
        self.cppro_parse_result.clear()
        del translation_unit
        del top_cursors
        cc.Cursor._tu = None
        clear_tu_caches()

