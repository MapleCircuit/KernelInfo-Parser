"""parser/c_ast.py - Parse C Source Files into ChangeSet Database Operations.

===============================================================================
C_AST PARSER & DATA ARCHITECTURE GUIDE
===============================================================================
This module implements the C language AST parser. Its responsibility is to parse
C source code files (.c, .h) into an intermediate node tree and extract relational
database operations into a ChangeSet (`CS`).

1. TWO-STAGE PARSING PIPELINE:
-------------------------------------------------------------------------------
  Stage 1: Tree Generation (libclang + TokenList + Zone/Ast Tree)
    - Uses `libclang` (`clang.cindex`) to tokenize and annotate C source files.
    - Parses C preprocessor directives (#include, #define, #ifdef, #ifndef, etc.).
    - Builds an in-memory intermediate tree composed of `Ast` nodes, `Zone` scopes,
      and `C_Type` declarations.
  
  Stage 2: ChangeSet Extraction (`.extract(CS)`)
    - Walks the constructed `Ast` / `Zone` tree.
    - Emits relational database view operations (`m_ast.view`, `m_ast_container.set`,
      `m_ast_include.set`, `m_tag.get_set`, `m_bridge_tag.set`) into `CS.cs[]`.

2. INTERMEDIATE TREE STRUCTURE (Zone vs C_Type):
-------------------------------------------------------------------------------
  `Zone`:
    - Manages spatial scope boundaries for code parsed in isolation (e.g. `Full_File`,
      `Declared_Args`, `Function_Args`, `Compound_Body`).
    - Acts as a position locator: during extraction, requests from child/parent zones
      return the index in `CS.cs` where their `m_ast.view(...)` is stored, allowing
      relational foreign keys (`container_ast_id`, `parent_ast_id`) to be resolved using `CS.ref(...)`.

  `C_Type`:
    - Encapsulates type information, specifiers, qualifiers (`const`, `volatile`, `restrict`),
      and declarators for variable/function/type definitions.
    - Uses 3 internal mechanics:
        1. `self.content = []` (+ `self.current_qual` flag for `CQual`)
        2. `self.swap_out()` (Pops accumulated tokens into `typedata`)
        3. `self.typedata = [([], CQual), ...]` (Stores type-qualifier tuples)

3. C TYPE QUALIFIERS & MULTI-DECLARATIONS:
-------------------------------------------------------------------------------
  In C, position dictates semantics:
    - `const char * text`  -> Pointer to constant char.
    - `char * text` vs `const char ctext` -> Multi-identifier declarations:
        `char * text, const ctext;` creates two declarations where pointer `*`
        applies only to `text` and `const` applies only to `ctext`.

4. TAG MANAGEMENT & VERSION TRACKING (`m_tag` & `m_bridge_tag`):
-------------------------------------------------------------------------------
  Tags track individual AST nodes (functions, structs, typedefs, macros) across project releases:
    - `get_prior_tags(CS)`: Queries previous version (`REF_OLD`) for active file tags.
    - `process_c_ast(CS)`: Matches AST elements against prior tags to recycle tag IDs via `m_bridge_tag`.
    - `close_prior_tags(CS)`: Marks tags as inactive if the underlying AST element was removed or altered.
===============================================================================
"""
from core.globalstuff import G, COLOR, REF_ROOT, REF_OLD, REF_C_AST, FILE_ERROR
from parser.c_ast.c_ast_type import (
    ChangeSetType,
    m_v_main,
    m_file_name,
    m_file,
    m_bridge_file,
    m_moved_file,
    m_ast,
    m_ast_container,
    m_ast_include,
    m_ast_debug,
    m_tag,
    m_bridge_tag,
    Line,
    Ast,
    Ast_Comment,
    Ast_Keyword,
    AST_KIND,
    End_Mode,
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
    Zone,
    Zone_Type,
    C_Type,
    Ast_MACRO_INSTANTIATION,

)  # noqa: F401
from pathlib import Path
import re
import time
import clang.cindex as cc
import ctypes
import logging
from core.globalstuff import configure_logging

configure_logging(level=logging.INFO, fmt="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
_WORKER_CLANG_INDEX: cc.Index | None = None

# This applies to c_ast and c_ast_type only
# ==========================================================
# The goal of the C_AST parser is to parse C code.
# We achive this by creating an intermediary tree structure
# made of Zones and Asts. 
# This allows us to standardize (and simplify) our handling of C.
# TLDR: We don't have to care about libclang when adding to CS.
#
# ==========================================================
# OVERVIEW
# C_AST works in 2 main stages:
# 1. The parsing through libclang which creates the Zone/Ast Tree.
# 2. The "extract" / push of changes to CS (ChangeSet)
# 
# Each class have their documentation in c_ast_type. (will be true...)
#
# ==========================================================
# LET IT BE CLEAR
# The other parsers shouldn't be built using any concept that are created
# within this parser. All parsers should be indepentent and don't have
# to resemble this implementation.
# The only thing we care about is that it plugs into our CS infrastructure.
#
# ==========================================================
# Zones/Asts examples.
# Here is a code snippet and its resulting tree structure which should be created.
# SOURCE: https://elixir.bootlin.com/linux/v3.0/source/include/linux/lockd/bind.h
#
#   struct nlmsvc_binding {
#	    __be32			(*fopen)(struct svc_rqst *,
#	    					struct nfs_fh *,
#	    					struct file **);
#	    void			(*fclose)(struct file *);
#   };
#
# and our structure:
#
# Zone(Zone_Type=Full_File)
# \=>C_Type(nlmsvc_binding)
#    \=>Zone(Zone_Type=Declared_Args)
#       \=>C_Type(fopen)
#       |  \=>Zone(Zone_Type=Function_Args)
#       |     \=>C_Type(struct svc_rqst *)
#       |     |=>C_Type(struct nfs_fh *)
#       |     |=>C_Type(struct file **)
#       |=>C_Type(fclose)
#          \=>Zone(Zone_Type=Function_Args)
#             \=>C_Type(struct file *)
#
# Each C_Type is going to be representing what is usualy 1 line of code.
# This is most likely 1 type but in this case:
#   char *text, const ctext;
# It would capture both type as it is part of the same base type.
# The inverse would be 2 C_type.:
#   char *text; const char ctext;
#
# ==========================================================
# Zone VS C_Type
#
# Zone
# Zone allows us to 'zone' parts of the code to be parsed.
# It is not possible for us to know when something stops or starts without it.
# It can create multiple section of code within itself
# to be parsed in a vacuum (Look Zone(Zone_Type=Function_Args) above).
# Once at the extract stage, we can request from a zone to return the relevant
# pos within CS where its CS.store(m_ast.view(...)) is.
# Zone should NOT have much more than positioning info, for all other info, we use:
#
# C_Type
# C_Type allows the information of a type to be stored.
# The main way it acheives this is by using self.typedata and self.content.
# Before getting into it, here is a quick TLDR on types in C.
# ---
# In C, "const char * text;" and "char * const text;" do not represent the same thing.
# In the 1st, the pointer is const while in the 2nd the text being pointed to is const.
# This fact means that we cannot just set a flag for every instance of const/(any other code)
# as the position is core to understanding the data.
# 
# C also allow multiple identifier per type:
#   "char * text, const ctext;"
# In this example 2 char are created:
#   char * text;
#   const char ctext; 
# Note that * doesn't apply to ctext and const doesn't apply to text.
#
# ---
# Now that we have a quick understanding of C types, how do we capture this correctly?
# We will use 3 tools for our parsing to be done correctly:
#   1. self.content => []  (+ self.current_qual=CQual)
#   2. self.swap_out()
#   3. self.typedata => [([], CQual)]
# When parsing, each token are stored in self.content, 
# there are special token we call CQual, 
#
# CQuals are stored in a flag within self.content/self.typedata.
# Here is the list of CQuals {const, volatile, restrict, _Atomic}
# 
# While we parse, we store each token in self.content,
# if it is a CQual, it is stored in the flag.
# some token will trigger self.swap_out() which pop self.content into self.typedata.
# If we have an already enable CQual, self.swap_out() is triggered.
# If we have an identifier, self.swap_out() is triggered.
# Pointer, char, int? self.swap_out() is triggered.






def c_ast_parse(CS: ChangeSetType) -> None:
    """Parse a C source file's diff changes into ChangeSet database operations.
    
    Dispatches parsing pipeline based on git diff operation type:
    - `"A"` (Added file): Parses AST and extracts operations via `process_c_ast()`.
    - `"M"` / `"R"` (Modified / Renamed file): Queries prior version active tags (`get_prior_tags`),
      parses new AST (`process_c_ast`), and marks obsolete tags closed (`close_prior_tags`).
    - `"D"` (Deleted file): Queries prior version active tags and marks them as closed.
    """
    with CS(REF_C_AST):
        if CS.file_operation == "A":
            process_c_ast(CS)
        elif CS.file_operation == "M" or CS.file_operation[0] == "R":
            get_prior_tags(CS)
            process_c_ast(CS)
            close_prior_tags(CS)
        elif CS.file_operation == "D":
            get_prior_tags(CS)
            close_prior_tags(CS)
        else:
            logger.error(f"this operation[{CS}] is not implemented---c_ast_parse()---")

    return

def process_c_ast(CS: ChangeSetType) -> None:
    """Instantiate `Ast_Manager` to parse source file and extract AST nodes into `CS`."""
    Ast_Manager(CS)
    return


def get_prior_tags(CS: ChangeSetType) -> None:
    """Query Table Engine for existing active AST tags registered in the previous version."""
    from core.DBLayout import m_file_name, m_bridge_file, m_bridge_tag, m_tag
    CS.active_tag_list = set()
    CS.prior_tags = None
    CS.prior_tags_map = {}

    old_vid = getattr(CS.gp, "Old_VID", 0)
    if old_vid <= 0:
        return

    fn_row = G.TE.get(m_file_name.table_id, (None, CS.current_path))
    if not fn_row:
        return
    fnid = fn_row[0]

    bf_row = G.TE.get(m_bridge_file.table_id, (old_vid, fnid, None))
    if not bf_row:
        return
    old_fid = bf_row[2]

    CS.prior_tags = m_bridge_tag.view_get_multiple(
        ((m_bridge_tag.tag_id, m_tag.tag_id, 1),),
        old_fid,
        None,  # 1: m_bridge_tag.tag_id
        None,  # 2: m_bridge_tag.line_s
        None,  # 3: m_bridge_tag.line_e
        None,  # 4: m_bridge_tag.char_s
        None,  # 5: m_bridge_tag.char_e
        None,  # 6: m_tag.tag_id
        None,  # 7: m_tag.vid_s
        None,  # 8: m_tag.vid_e
        None,  # 9: m_tag.code
        None,  # 10: m_tag.ast_id
        None,  # 11: m_tag.hl_s
        None,  # 12: m_tag.hl_l
    )
    if CS.prior_tags:
        CS.prior_tags_map = {tag[9]: (x, tag[1]) for x, tag in enumerate(CS.prior_tags) if tag[9]}
    return


def close_prior_tags(CS: ChangeSetType) -> None:
    """Mark prior version AST tags as closed/inactive if they were not recycled in the current version."""
    from core.DBLayout import m_tag
    if CS.prior_tags:
        with CS(REF_OLD):
            for x, tag in enumerate(CS.prior_tags):
                if x in CS.active_tag_list:
                    continue
                CS.store(m_tag.update(
                    tag[6],          # m_tag.tag_id
                    tag[7],          # m_tag.vid_s
                    CS.gp.Old_VID,   # m_tag.vid_e (closed)
                    tag[9],          # m_tag.code
                    tag[10],         # m_tag.ast_id
                    tag[11],         # m_tag.hl_s
                    tag[12],         # m_tag.hl_l
                ))
    return


###### SPECIAL BINDINGS 


class CXSourceRangeList(ctypes.Structure):
    """Use in clang_getSkippedRanges."""

    _fields_ = [("count", ctypes.c_uint), ("ranges", ctypes.POINTER(cc.SourceRange))]

CXSourceRangeList_P = ctypes.POINTER(CXSourceRangeList)


(
    Stack_CPP, Stack_CPP_Include
) = range(2)

_CLANG_GET_EXTENT = cc.conf.lib.clang_getTokenExtent
_CLANG_GET_CURSOR_EXTENT = cc.conf.lib.clang_getCursorExtent
_CLANG_GET_CURSOR_EXTENT.argtypes = [cc.Cursor]
_CLANG_GET_CURSOR_EXTENT.restype = cc.SourceRange

_CLANG_GET_RANGE_START = cc.conf.lib.clang_getRangeStart
_CLANG_GET_RANGE_START.argtypes = [cc.SourceRange]
_CLANG_GET_RANGE_START.restype = cc.SourceLocation

_CLANG_GET_RANGE_END = cc.conf.lib.clang_getRangeEnd
_CLANG_GET_RANGE_END.argtypes = [cc.SourceRange]
_CLANG_GET_RANGE_END.restype = cc.SourceLocation

_CLANG_GET_SPELLING_LOC = cc.conf.lib.clang_getSpellingLocation

_CTYPES_F_PTR = cc.c_object_p()
_CTYPES_S_LINE = cc.c_uint()
_CTYPES_S_COL = cc.c_uint()
_CTYPES_S_OFF = cc.c_uint()
_CTYPES_E_LINE = cc.c_uint()
_CTYPES_E_COL = cc.c_uint()
_CTYPES_E_OFF = cc.c_uint()
_CTYPES_BYREF = ctypes.byref


_CLANG_GET_TOKEN_KIND = cc.conf.lib.clang_getTokenKind
_CLANG_GET_TOKEN_KIND.argtypes = [cc.Token]
_CLANG_GET_TOKEN_KIND.restype = ctypes.c_uint

_CLANG_TOKEN_KIND_MAP = (
    AST_KIND.punctuation,
    AST_KIND.keyword,
    AST_KIND.identifier,
    AST_KIND.literal,
    AST_KIND.comment,
)


def get_cursor_line(cursor) -> Line:
    """Fast-path ctypes Line extraction from Clang Cursor with caching."""
    cl = getattr(cursor, "_cached_line", None)
    if cl is not None:
        return cl
    ext = _CLANG_GET_CURSOR_EXTENT(cursor)
    st = _CLANG_GET_RANGE_START(ext)
    en = _CLANG_GET_RANGE_END(ext)
    _CLANG_GET_SPELLING_LOC(st, _CTYPES_BYREF(_CTYPES_F_PTR), _CTYPES_BYREF(_CTYPES_S_LINE), _CTYPES_BYREF(_CTYPES_S_COL), _CTYPES_BYREF(_CTYPES_S_OFF))
    _CLANG_GET_SPELLING_LOC(en, _CTYPES_BYREF(_CTYPES_F_PTR), _CTYPES_BYREF(_CTYPES_E_LINE), _CTYPES_BYREF(_CTYPES_E_COL), _CTYPES_BYREF(_CTYPES_E_OFF))
    cl = Line.__new__(Line)
    cl.code = ""
    cl.line_pos = (_CTYPES_S_LINE.value, _CTYPES_E_LINE.value)
    cl.char_pos = (_CTYPES_S_COL.value, _CTYPES_E_COL.value)
    cursor._cached_line = cl
    return cl


class TokenList:
    """Binding for clang_tokenize and clang_annotateTokens."""

    def __init__(self, parsed_tu, fullfilename, rawfile: tuple[str] | None = None):

        parsed_file = cc.File.from_name(parsed_tu, fullfilename)

        start_loc = cc.SourceLocation.from_position(parsed_tu, parsed_file, 1, 1)

        #filesize in bytes
        file_size = Path(fullfilename).stat().st_size
        end_loc = cc.conf.lib.clang_getLocationForOffset(parsed_tu, parsed_file, file_size)
        logger.debug(f"end_loc:{end_loc}")

        extent = cc.SourceRange.from_locations(start_loc, end_loc)

        tokens_memory = ctypes.POINTER(cc.Token)()
        tokens_count = ctypes.c_uint()

        cc.conf.lib.clang_tokenize(parsed_tu, extent, ctypes.byref(tokens_memory), ctypes.byref(tokens_count))

        self.count = int(tokens_count.value)

        self.cursors_array = []
        self.tokens_array = []

        if self.count < 1:
            return

        # Pre Allocate
        temp_cursors_array = (cc.Cursor * self.count)()

        cc.conf.lib.clang_annotateTokens(parsed_tu, tokens_memory.contents, self.count, temp_cursors_array)

        for cursor in temp_cursors_array:
            cursor._tu = parsed_tu
            self.cursors_array.append(cursor)

        temp_tokens_array = ctypes.cast(tokens_memory, ctypes.POINTER(cc.Token * self.count)).contents
        for token in temp_tokens_array:
            token._tu = parsed_tu

            # Direct Ctypes line/char coordinate extraction (2 C calls vs 5 C calls)
            ext = _CLANG_GET_EXTENT(parsed_tu, token)
            st = _CLANG_GET_RANGE_START(ext)
            en = _CLANG_GET_RANGE_END(ext)
            _CLANG_GET_SPELLING_LOC(st, _CTYPES_BYREF(_CTYPES_F_PTR), _CTYPES_BYREF(_CTYPES_S_LINE), _CTYPES_BYREF(_CTYPES_S_COL), _CTYPES_BYREF(_CTYPES_S_OFF))
            _CLANG_GET_SPELLING_LOC(en, _CTYPES_BYREF(_CTYPES_F_PTR), _CTYPES_BYREF(_CTYPES_E_LINE), _CTYPES_BYREF(_CTYPES_E_COL), _CTYPES_BYREF(_CTYPES_E_OFF))

            s_line = _CTYPES_S_LINE.value
            e_line = _CTYPES_E_LINE.value
            s_col = _CTYPES_S_COL.value
            e_col = _CTYPES_E_COL.value

            l = Line.__new__(Line)
            l.code = ""
            l.line_pos = (s_line, e_line)
            l.char_pos = (s_col, e_col)
            token.line = l

            if s_line == e_line and rawfile and s_line <= len(rawfile):
                token.spelling_str = rawfile[s_line - 1][s_col - 1 : e_col - 1]
            else:
                try:
                    token.spelling_str = token.spelling
                except Exception:
                    token.spelling_str = ""

            token.ast_kind = _CLANG_TOKEN_KIND_MAP[_CLANG_GET_TOKEN_KIND(token)]
            self.tokens_array.append(token)

        self.token_group = cc.TokenGroup(parsed_tu, tokens_memory, tokens_count)

        return


    def process_tokens(self, CS):

        self.CS = CS
        self.main_zone = Zone(Zone_Type.Full_File, None)

        prof = getattr(CS, "profiler", None)
        if prof is not None:
            t_proc_0 = time.perf_counter()

        check_exec = self.main_zone.check_exec
        for token, cursor in zip(self.tokens_array, self.cursors_array):
            check_exec(token, cursor, token.ast_kind)

        self.main_zone.gen_lined_dict()
        self.main_zone.resolve_cppro_scopes()

        if prof is not None:
            prof.token_processing_s = time.perf_counter() - t_proc_0
            t_extract_0 = time.perf_counter()

        if G.OVERRIDE_FORCE_AST_DEBUG:
            G.BP()
        
        self.main_zone.extract(self.CS)

        if prof is not None:
            prof.ast_extraction_s = time.perf_counter() - t_extract_0

        if G.OVERRIDE_FORCE_AST_DEBUG:
            G.BP()

        return












class Ast_Manager:
    def __init__(self, CS: ChangeSetType) -> None:
        self.mfdir = CS.mf.version_dict[CS.gp.Version_Name]
        self.filename = CS.current_path
        self.fullfilename = f"{self.mfdir}/{self.filename}"
        G.CURRENT_PARSING_FILE = self.filename
        self.processing_list = []
        self.cppro_parse_result = []
        CS.parsers["C_AM"] = self
        self.Init_Parse(CS)

    def Init_Parse(self, CS) -> None:
        try:
            self.unsplit_rawfile = Path(self.fullfilename).read_text(encoding="latin-1")
        except Exception as e:
            raise FILE_ERROR(e)

        self.rawfile = tuple(self.unsplit_rawfile.split("\n"))

        cppro_cindex_input = []
        if G.OVERRIDE_CPPRO_CINDEX_INPUT:
            cppro_cindex_input = [line[6:].lstrip() for line in comment_remover(self.unsplit_rawfile).splitlines() if line.startswith("#ifdef")]


        # Initialize/Reuse the Clang index
        global _WORKER_CLANG_INDEX
        if _WORKER_CLANG_INDEX is None:
            _WORKER_CLANG_INDEX = cc.Index.create()
        index = _WORKER_CLANG_INDEX

        prof = getattr(CS, "profiler", None)
        if prof is not None:
            t_parse_0 = time.perf_counter()

        # these: "-M","-MG", were probably important, but who gives a shit as they print a bunch of shit on screen, lol
        translation_unit = index.parse(
            self.fullfilename,
            args=[
                "-ferror-limit=0",
                "-w",
                "-D__KERNEL__",
                *cppro_cindex_input,  # "-nostdinc",
                f"-I{self.mfdir}/{'/'.join(self.filename.split('/')[:-1])}",
                f"-I{self.mfdir}/include",
                f"-I{self.mfdir}/include/uapi",
            ],
            options=(cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD + 32768),
        )
        # https://clang.llvm.org/doxygen/group__CINDEX__TRANSLATION__UNIT.html

        if prof is not None:
            prof.clang_parse_tu_s = time.perf_counter() - t_parse_0
            t_tok_0 = time.perf_counter()

        TL = TokenList(translation_unit, self.fullfilename, self.rawfile)

        if prof is not None:
            prof.clang_tokenize_s = time.perf_counter() - t_tok_0

        TL.process_tokens(CS)

        return

    def cppro_parse(self, current_file: str, file_path: str) -> list[Ast]:
        # Cleanup
        current_file = comment_remover(current_file).splitlines()

        result_arr = []
        bypass_num = 0
        for shit in range(len(current_file)):
            if shit < bypass_num:
                continue
            result = self.cppro_line_parse(current_file, shit, file_path)
            if result:
                if (temp := getattr(result, "line")) is not None:
                    bypass_num = temp.line_pos[0]
                result_arr.append(result)
        return result_arr

    def cppro_line_parse(self, current_file: list[str], current_line: int, file_path: str) -> Ast|None:
        # This try: is a check for misformed CPPro tags like "#error"<-Without anything else....
        try:
            working_line = current_file[current_line].lstrip()

            loopval = 0
            if working_line == "":
                return None
            if working_line[0] != "#":
                return None

            # Start Handling possible " " or \t after #
            try:
                working_line = working_line[0] + working_line[1:].lstrip()
            except IndexError:
                return None
            # End Handling possible " " or \t after #

            # Start \newline handling
            while current_file[current_line + loopval][-1] == "\\":
                # Start Confirm that there is a next line
                try:
                    current_file[current_line + loopval + 1]
                except IndexError:
                    break
                # End Confirm that there is a next line

                loopval += 1
                if (current_file[current_line + loopval][0] == " ") or (
                    current_file[current_line + loopval][0] == "\t"
                ):
                    working_line = (
                        working_line[:-1]
                        + " \n"
                        + current_file[current_line + loopval].lstrip()
                    )
                else:
                    working_line = (
                        working_line[:-1] + "\n" + current_file[current_line + loopval]
                    )
            # End \newline handling

            match working_line.split(maxsplit=1)[0]:
                # Start #ifdef
                case "#ifdef":
                    return CPPro_ifdef(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        ),
                        working_line[6:].lstrip(),
                    )
                # End #ifdef

                # Start #ifndef
                case "#ifndef":
                    return CPPro_ifndef(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        ),
                        working_line[7:].lstrip(),
                    )
                # End #ifndef

                # Start #if
                case "#if":
                    return CPPro_if(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        ),
                        working_line[3:].lstrip(),
                    )
                # End #if

                # Start #elifndef AND #elifdef
                case "#elifndef":
                    if G.OVERRIDE_GLOBAL_C_AST:
                        logger.error(
                            f"SOME RETARDED DEVS, ADDED THIS FUCKING BULSHIT TO THEIR CODE: #elifndef , Line:{current_line + 1}"
                        )
                    G.emergency_shutdown(6)
                case "#elifdef":
                    if G.OVERRIDE_GLOBAL_C_AST:
                        logger.error(
                            f"SOME RETARDED DEVS, ADDED THIS FUCKING BULSHIT TO THEIR CODE: #elifdef , Line:{current_line + 1}"
                        )
                    G.emergency_shutdown(7)
                # End #elifndef AND #elifdef

                # Start #elif
                case "#elif":
                    return CPPro_elif(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        ),
                        working_line[5:].lstrip(),
                    )
                # End #elif

                # Start #else
                case "#else":
                    return CPPro_else(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        )
                    )
                # End #else

                # Start #endif
                case "#endif":
                    return CPPro_endif(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        )
                    )
                # End #endif

                # Start #define
                case "#define":
                    working_line = working_line[7:].lstrip()
                    parentheses = 0
                    bypass = False
                    arg_one = ""
                    arg_two = ""
                    for line_char in working_line:
                        if not bypass:
                            if line_char == "\n":
                                continue
                            if line_char == "(":
                                parentheses += 1
                            elif line_char in {" ", "\t"}:
                                if parentheses == 0:
                                    bypass = True
                            elif line_char == ")":
                                parentheses -= 1
                                if parentheses == 0:
                                    bypass = True

                        if bypass:
                            arg_two += line_char
                        else:
                            arg_one += line_char

                    arg_two = arg_two.lstrip()
                    if arg_two == "":
                        arg_two = None

                    return CPPro_define(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        ),
                        arg_one,
                        arg_two,
                    )
                # End #define

                # Start #undef
                case "#undef":
                    return CPPro_undef(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        ),
                        working_line[6:].lstrip(),
                    )
                # End #undef

                # Start #include
                case "#include":
                    working_line = working_line[8:].lstrip()
                    if working_line == "":
                        return CPPro_include(
                            Line(current_line + 1, current_line + 1 + loopval).cc(
                                self.rawfile
                            ),
                            "",
                            "",
                        )

                    if working_line[0] == '"':
                        written_include = '"'
                        actual_path = f"{Path(file_path).parent}/"
                        for line_char in working_line[1:]:
                            written_include += line_char
                            if line_char == '"':
                                break
                    elif working_line[0] == "<":
                        written_include = "<"
                        actual_path = "include/"
                        for line_char in working_line[1:]:
                            written_include += line_char
                            if line_char == ">":
                                break
                    else:
                        return CPPro_include(
                            Line(current_line + 1, current_line + 1 + loopval).cc(
                                self.rawfile
                            ),
                            "",
                            "",
                        )

                    # PARSE THE ACTUAL INCLUDE
                    written_include[1:-2]

                    path_arr = []
                    dotdot = 0
                    # IT WILL FUCKING BREAK IF SOME RETARD PUT SOME ROOT PATH IN THERE LIBS, WHY WOULD SOMEONE DO SOMETHING SO WRONG???? WHO THE FUCJK KNOWS!!!! BEWARE
                    for chunk in str(actual_path + written_include[1:-1]).split("/")[
                        ::-1
                    ]:
                        if chunk == "..":
                            dotdot += 1
                        elif dotdot > 0:
                            dotdot -= 1
                        else:
                            path_arr.append(chunk)

                    return CPPro_include(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        ),
                        written_include,
                        "/".join(path_arr[::-1]),
                    )
                # End #include

                # Start #line 453 /path
                case "#line":
                    line_in_work = working_line[5:].lstrip()
                    lineno = re.match(r"^\d+", line_in_work).group(1)

                    try:
                        filename = line_in_work[len(lineno) :].lstrip()
                    except IndexError:
                        filename = None

                    return CPPro_line(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        ),
                        int(lineno),
                        filename,
                    )
                # End #line

                # Start #error
                case "#error":
                    return CPPro_error(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        ),
                        working_line[6:].lstrip().rstrip(),
                    )
                # End #error

                # Start #pragma
                case "#pragma":
                    return CPPro_pragma(
                        Line(current_line + 1, current_line + 1 + loopval).cc(
                            self.rawfile
                        ),
                        working_line[7:].lstrip(),
                    )
                # End #pragma

        except IndexError:
            return None

        return None



###### END SPECIAL BINDINGS 


def comment_remover(text: str) -> str:
    """Remove C comments.

    Stolen from: https://gist.github.com/ChunMinChang/88bfa5842396c1fbbc5b
    """
    def replacer(match: object) -> str:
        s = match.group(0)
        if s.startswith("/"):
            return "\n" * s.count("\n")

        return s

    pattern = re.compile(
        r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
        re.DOTALL | re.MULTILINE,
    )
    return re.sub(pattern, replacer, text)
