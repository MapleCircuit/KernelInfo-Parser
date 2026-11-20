"""parser/c_ast/c_ast.py - C-AST Parser Orchestrator & Entry Point.

Provides:
- c_ast_parse(CS): Main file lifecycle dispatcher for C parsing.
- process_c_ast(CS): Parse and stage C-AST constructs into ChangeSet.
- Ast_Manager: Translation unit manager coordinating Libclang parsing and tokenization.
- TokenList: High-speed ctypes token stream with in-place Latin-1 line slicing.
- get_prior_tags(CS): Prior version tags (recycling and transition matching).
"""
from __future__ import annotations
import time
import ctypes
import logging
import os
from pathlib import Path
from typing import Any
import clang.cindex as cc

from core.SymbolRows import finish as finish_symbol_rows
from core.DBLayout import m_blob_facts
from core.globalstuff import (
    G,
    REF_C_AST,
    REF_NO_REF,
    REF_POS,
    FILE_ERROR,
)
from parser.c_ast.ctypes_bindings import (
    AST_KIND,
    _CLANG_GET_EXTENT,
    _CLANG_GET_CURSOR_EXTENT,
    _CLANG_GET_CURSOR_LOC,
    _CLANG_GET_FILE_LOC,
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
    match_prior_tag_transition,
    check_exact_match,
)
from parser.c_ast.cs_extractor import CSExtractor
from parser.c_ast.lexical_refs import record_lexical_refs

logger = logging.getLogger(__name__)

_WORKER_CLANG_INDEX: cc.Index | None = None

# CXTranslationUnit flags (clang-c/Index.h) without a cindex constant
_CXTU_IGNORE_NON_ERRORS_FROM_INCLUDED_FILES = 0x4000
_CXTU_RETAIN_EXCLUDED_CONDITIONAL_BLOCKS = 0x8000
_CLANG_PARSE_OPTIONS = (
    cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
    | _CXTU_IGNORE_NON_ERRORS_FROM_INCLUDED_FILES
    | _CXTU_RETAIN_EXCLUDED_CONDITIONAL_BLOCKS
)

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
            elif CS.file_operation == "D":
                pass  # Tags are immutable content records: a deleted file stages nothing
            else:
                logger.error(f"Unsupported file operation '{CS.file_operation}' in c_ast_parse")
        # Emit the symbol definitions/references recorded while parsing (core/SymbolRows.py)
        finish_symbol_rows(CS)
        stage_parse_diagnostics(CS)
    finally:
        CS.compile_env = None
        CS.compile_arch = None
        CS.parse_diagnostics = None
        clear_tu_caches()


def stage_parse_diagnostics(CS: Any) -> None:
    """Stage the blob's parse diagnostics (`m_blob_facts` family 7, parser/compile_env.py `diagnostics_fact()`)."""
    from parser import compile_env

    fact = getattr(CS, "parse_diagnostics", None)
    blob_hash = getattr(CS, "blob_hash", None)
    CS.parse_diagnostics = None
    CS.compile_env = None
    CS.compile_arch = None
    if fact is None or not blob_hash or not any(blob_hash):
        return
    with CS(REF_POS):
        CS.store(m_blob_facts.set(bytes(blob_hash), compile_env.DIAG_FAMILY, compile_env.DIAG_FORMAT,
                                  compile_env.encode_fact(fact)))


def process_c_ast(CS: Any) -> None:
    """Parse source file into AST nodes and extract ChangeSet operations."""
    Ast_Manager(CS)


def map_direct_includes(parsed_tu: cc.TranslationUnit, fullfilename: str, CS: Any) -> list[cc.Cursor]:
    """Record the repository path libclang opened for each direct `#include` line of the file
    (`CS.resolved_includes_by_line`) and return the file's pre-filtered top-level declaration cursors for
    TokenList processing."""
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

    env = getattr(CS, "compile_env", None)
    base_dir = getattr(CS, "mfdir", None) or getattr(G, "CURRENT_PARSING_DIR", None)
    for inc in inclusions:
        if inc.depth != 1:
            continue
        inc_file = inc.include
        src = inc.source
        if not inc_file or not src or not inc.location:
            continue
        src_ptr = ctypes.cast(src._as_parameter_, ctypes.c_void_p).value
        if not (src_ptr == target_ptr or (target_ptr is None and (src.name == fullfilename or get_real(src.name) == target_real))):
            continue
        line = inc.location.line
        inc_name = inc_file.name
        rel_inc = env.repo_path(inc_name) if env is not None else None
        if rel_inc is None and env is None:
            try:
                rel_inc = os.path.relpath(get_real(inc_name), base_dir) if base_dir else inc_name
            except Exception:
                rel_inc = inc_name
        if rel_inc and not rel_inc.startswith(".."):
            CS.resolved_includes_by_line[line] = rel_inc

    # Walk the TU's top-level cursors with clang_visitChildren directly: thousands of them come from headers,
    # and cindex's get_children() spends two extra C calls per child on a null-cursor assertion
    top_decl_cursors: list[cc.Cursor] = []
    walk_errors: list[Exception] = []
    f_ptr = ctypes.c_void_p()
    f_ref = ctypes.byref(f_ptr)
    get_loc = _CLANG_GET_CURSOR_LOC
    get_file_loc = _CLANG_GET_FILE_LOC

    def top_level_visitor(c: Any, parent: Any, client_data: Any) -> int:
        try:
            get_file_loc(get_loc(c), f_ref, None, None, None)
            if f_ptr.value == target_ptr or (target_ptr is None and c.location.file and (c.location.file.name == fullfilename or get_real(c.location.file.name) == target_real)):
                if c.kind in _VALID_TOP_KINDS:
                    c._tu = parsed_tu
                    top_decl_cursors.append(c)
        except Exception as e:
            walk_errors.append(e)
            return 0  # CXChildVisit_Break
        return 1  # CXChildVisit_Continue

    _CLANG_VISIT_CHILDREN(parsed_tu.cursor, cc.cursor_visit_callback(top_level_visitor), None)
    if walk_errors:
        logger.debug(f"Error walking file-bounded top-level cursors: {walk_errors[0]}")

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
        # Identifiers libclang left unresolved (skipped code, missing definitions): parser/c_ast/lexical_refs.py
        # (C only: assembly text has no C names)
        if not self.fullfilename.endswith((".S", ".s")):
            record_lexical_refs(self.CS, self.tokens_array, self.rawfile)

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
    # linux/linkage.h only defines fastcall under #ifndef FASTCALL: without it every
    # "<type> fastcall f(...)" definition (2.6.x) is a syntax error and its body leaks to file scope
    "-Dfastcall=",
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

        global _WORKER_CLANG_INDEX
        if _WORKER_CLANG_INDEX is None:
            _WORKER_CLANG_INDEX = cc.Index.create()
        index = _WORKER_CLANG_INDEX

        prof = getattr(CS, "profiler", None)
        t_parse_0 = time.perf_counter() if prof is not None else 0.0

        # The compilation `make ARCH=<arch>` runs for this file (parser/compile_env.py): layout include
        # directories and build directory, forced headers, defconfig, no host header (-nostdinc)
        from parser import compile_env  # lazy: parser.arch_layout imports the C parser through kconfig_ast

        env = compile_env.env_for(self.mfdir)
        CS.compile_env = env
        env_args, extra_options, arch_env = env.args_for(self.filename, asm=False)
        CS.compile_arch = arch_env
        translation_unit = index.parse(
            self.fullfilename,
            args=[
                "-ferror-limit=0",
                "-w",
                *env_args,
                *_CLANG_KERNEL_MACRO_DEFINES,
            ],
            options=_CLANG_PARSE_OPTIONS | extra_options,
        )
        CS.parse_diagnostics = compile_env.diagnostics_fact(translation_unit, arch_env)
        t_includes_0 = time.perf_counter() if prof is not None else 0.0

        top_cursors = map_direct_includes(translation_unit, self.fullfilename, CS)

        if prof is not None:
            prof.clang_parse_tu_s = t_includes_0 - t_parse_0
            prof.include_map_s = time.perf_counter() - t_includes_0
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

