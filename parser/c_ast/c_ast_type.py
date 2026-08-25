from __future__ import annotations
from collections import deque
from core.globalstuff import G, COLOR, REF_POS, REF_ROOT, REF_OLD, REF_MULTI, REF_NO_REF, ASTT, IntEnum, Flag, auto, RefType, OP_REF, RouteType
import clang.cindex as cc
import logging
import json
import ctypes
from typing import Self, Any
import random

logger = logging.getLogger(__name__)

# Linter bypass
m_v_main = m_file_name = m_file = m_bridge_file = m_moved_file = m_type_descriptor = m_ast = m_ast_container = m_ast_include = m_ast_debug = m_tag = m_bridge_tag = m_map_ast = m_bridge_map = m_ast_hash = None
ChangeSetType = Any
_DEF_TYPES = frozenset({ASTT.C_struct, ASTT.C_functionproto, ASTT.C_union, ASTT.C_enum})
_PUNCT_IGNORED = frozenset({";", ",", ")", "}"})
_KEYWORD_IGNORED = frozenset({"if", "else", "return", "switch", "case", "default", "break", "continue", "for", "while", "do", "goto", "_Static_assert", "static_assert"})

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

_BYREF_F_PTR = ctypes.byref(_CTYPES_F_PTR)
_BYREF_S_LINE = ctypes.byref(_CTYPES_S_LINE)
_BYREF_S_COL = ctypes.byref(_CTYPES_S_COL)
_BYREF_S_OFF = ctypes.byref(_CTYPES_S_OFF)
_BYREF_E_LINE = ctypes.byref(_CTYPES_E_LINE)
_BYREF_E_COL = ctypes.byref(_CTYPES_E_COL)
_BYREF_E_OFF = ctypes.byref(_CTYPES_E_OFF)


def get_cursor_line(cursor) -> Line:
    """Fast-path ctypes Line extraction from Clang Cursor with caching."""
    cl = getattr(cursor, "_cached_line", None)
    if cl is not None:
        return cl
    ext = _CLANG_GET_CURSOR_EXTENT(cursor)
    st = _CLANG_GET_RANGE_START(ext)
    en = _CLANG_GET_RANGE_END(ext)
    _CLANG_GET_SPELLING_LOC(st, _BYREF_F_PTR, _BYREF_S_LINE, _BYREF_S_COL, _BYREF_S_OFF)
    _CLANG_GET_SPELLING_LOC(en, _BYREF_F_PTR, _BYREF_E_LINE, _BYREF_E_COL, _BYREF_E_OFF)
    cl = Line.__new__(Line)
    cl.code = ""
    cl.line_pos = (_CTYPES_S_LINE.value, _CTYPES_E_LINE.value)
    cl.char_pos = (_CTYPES_S_COL.value, _CTYPES_E_COL.value)
    cursor._cached_line = cl
    return cl

def serializer(obj: object):
    """For ast_debug."""
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    if hasattr(obj, "__slots__"):
        return {s: getattr(obj, s, None) for s in obj.__slots__}
    if isinstance(obj, (deque, tuple, set)):
        return list(obj)
    return str(obj)

def good_looking_printing(object_name: object, pre_result: str="", post_result: str=" ") -> str:
    """Print AST without headache."""
    result = " "
    multi_line_leap = False
    list_wait_arr = []
    if hasattr(object_name, "__dict__"):
        keys = vars(object_name)
    elif hasattr(object_name, "__slots__"):
        keys = object_name.__slots__
    else:
        keys = ()

    for key in keys:
        val = getattr(object_name, key, None)
        if not val:
            continue
        if isinstance(val, (list, tuple, deque)):
            list_wait_arr.append(key)
        else:
            to_be_added = f"{COLOR.magenta(key)}:{val},"
            if len(result.splitlines()[-1]) > G.OVERRIDE_MAX_PRINT_SIZE:
                if not multi_line_leap:
                    pre_result += "\n"
                    result = f"   {result}"
                    multi_line_leap = True
                to_be_added = f"\n{to_be_added}"
            result += to_be_added
    result = result[:-1]  # comma remover

    if multi_line_leap:
        result = result.replace("\n", "\n   ")

    for key in list_wait_arr:
        # if multi_line_leap:
        # result += "   "
        result += f", {COLOR.green(key)}" + COLOR.green(": {")
        for key_key in getattr(object_name, key):
            result += "\n   "
            result += str(key_key).replace("\n", "\n   ")
        result += COLOR.green("\n}")
    return f"{pre_result}{result[1:]}{post_result}"



# This applies to c_ast and c_ast_type only
# ==========================================================
# The goal of the C_AST parser is to parse C code.
# We achieve this by creating an intermediary tree structure
# made of Zones and Asts. 
# This allows us to standardize (and simplify) our handling of C.
# TLDR: We don't have to care about libclang when adding to CS.
#
# ==========================================================
# OVERVIEW
# C_AST works in 2 main stages:
# 1. The parsing through libclang which creates the Zone/Ast Tree.
# 2. The "extract" / push of changes to CS (ChangeSet)


class Line:
    """Represent position of code, can extract the underlying str."""

    __slots__ = ("line_pos", "char_pos", "code")

    def __init__(self, arg0: int | Line | cc.SourceRange | Any = 0, arg1: int = 0, arg2: int = 0, arg3: int = 0) -> None:
        """Init the line pos and optionally the col pos, accept cc.SourceRange."""
        self.code = ""
        t = type(arg0)
        if t is int:
            self.line_pos = (arg0, arg1)
            self.char_pos = (arg2, arg3)
        elif t is Line or isinstance(arg0, Line):
            self.line_pos = arg0.line_pos
            self.char_pos = arg0.char_pos
        elif isinstance(arg0, cc.SourceRange):
            self.line_pos = (arg0.start.line, arg0.end.line)
            self.char_pos = (arg0.start.column, arg0.end.column)
        elif hasattr(arg0, 'line') and isinstance(arg0.line, Line):
            self.line_pos = arg0.line.line_pos
            self.char_pos = arg0.line.char_pos
        else:
            self.line_pos = (0, 0)
            self.char_pos = (0, 0)

    # Code Capture
    def cc(self, rawfile: tuple[str]) -> Self:
        """Extract the str using line/col pos."""
        if self.line_pos[0] == 0:
            self.code = ""
            return self

        # Line Select
        try:
            if self.line_pos[0] == self.line_pos[1]:
                self.code = rawfile[self.line_pos[0] - 1]
                char_start = 0 if self.char_pos[0] == 0 else self.char_pos[0] - 1
                char_end = len(self.code) if (self.char_pos[1] == 0 or self.char_pos[1] - 1 >= len(self.code)) else (self.char_pos[1] - 1)
                self.code = self.code[char_start : char_end]
                return self
            else:
                self.code = "\n".join(rawfile[self.line_pos[0] - 1 : self.line_pos[1]])
        except IndexError:
            self.code = ""
            return self

        char_start = 0 if self.char_pos[0] == 0 else self.char_pos[0] - 1
        last_line_len = len(rawfile[self.line_pos[1] - 1])
        char_end = last_line_len if (self.char_pos[1] == 0 or self.char_pos[1] - 1 >= last_line_len) else (self.char_pos[1] - 1)

        if char_end == last_line_len:
            self.code = self.code[char_start:]
        else:
            trim_end = last_line_len - char_end
            self.code = self.code[char_start : -trim_end]
        return self

    def new_end(self, *args: int | object) -> None:
        """Update the end values of Line, accept cc.SourceRange, cc.Token and Line."""
        if self.line_pos[0] == 0:
            self.__init__(*args)
            return

        l = len(args)
        if l == 1:
            arg = args[0]
            if type(arg) is Line:
                self.line_pos = (self.line_pos[0], arg.line_pos[1])
                self.char_pos = (self.char_pos[0], arg.char_pos[1])
            elif hasattr(arg, 'line'):
                self.line_pos = (self.line_pos[0], arg.line.line_pos[1])
                self.char_pos = (self.char_pos[0], arg.line.char_pos[1])
            elif isinstance(arg, Line):
                self.line_pos = (self.line_pos[0], arg.line_pos[1])
                self.char_pos = (self.char_pos[0], arg.char_pos[1])
            elif isinstance(arg, cc.SourceRange):
                self.line_pos = (self.line_pos[0], arg.end.line)
                self.char_pos = (self.char_pos[0], arg.end.column)
            else:
                self.line_pos = (self.line_pos[0], arg)
        elif l == 2:
            self.line_pos = (self.line_pos[0], args[0])
            self.char_pos = (self.char_pos[0], args[1])

    def new_end_reversed(self, *args: int | object) -> None:
        """Update the end values of Line, will use start vals, accept cc.SourceRange and Line."""
        if self.line_pos[0] == 0:
            self.__init__(*args)
            return

        l = len(args)
        if l == 1:
            arg = args[0]
            if type(arg) is Line:
                self.line_pos = (self.line_pos[0], arg.line_pos[0])
                self.char_pos = (self.char_pos[0], arg.char_pos[0])
            elif hasattr(arg, 'line'):
                self.line_pos = (self.line_pos[0], arg.line.line_pos[0])
                self.char_pos = (self.char_pos[0], arg.line.char_pos[0])
            elif isinstance(arg, cc.SourceRange):
                self.line_pos = (self.line_pos[0], arg.start.line)
                self.char_pos = (self.char_pos[0], arg.start.column)
            elif isinstance(arg, Line):
                self.line_pos = (self.line_pos[0], arg.line_pos[0])
                self.char_pos = (self.char_pos[0], arg.char_pos[0])
            else:
                self.line_pos = (self.line_pos[0], arg)
        elif l == 2:
            self.line_pos = (self.line_pos[0], args[0])
            self.char_pos = (self.char_pos[0], args[1])

    def grow(self, *args: int | object) -> None:
        """Update the start and end values of Line, accept cc.SourceRange and Line."""
        if self.line_pos[0] == 0:
            self.__init__(*args)
            return

        arg = args[0]
        if type(arg) is Line:
            a_s_l, a_e_l = arg.line_pos
            a_s_c, a_e_c = arg.char_pos
        elif hasattr(arg, 'line') and type(arg.line) is Line:
            a_s_l, a_e_l = arg.line.line_pos
            a_s_c, a_e_c = arg.line.char_pos
        elif isinstance(arg, Line):
            a_s_l, a_e_l = arg.line_pos
            a_s_c, a_e_c = arg.char_pos
        elif isinstance(arg, cc.SourceRange):
            a_s_l, a_e_l = arg.start.line, arg.end.line
            a_s_c, a_e_c = arg.start.column, arg.end.column
        else:
            return

        s_l, e_l = self.line_pos
        s_c, e_c = self.char_pos

        if s_l > a_s_l:
            s_l = a_s_l
            s_c = a_s_c
        elif s_l == a_s_l and s_c > a_s_c:
            s_c = a_s_c

        if e_l < a_e_l:
            e_l = a_e_l
            e_c = a_e_c
        elif e_l == a_e_l and e_c < a_e_c:
            e_c = a_e_c

        self.line_pos = (s_l, e_l)
        self.char_pos = (s_c, e_c)

    def is_inside(self, extent) -> bool:
        """Test whether an extent/Line is within current Line."""
        s_s_l, s_e_l = self.line_pos
        if type(extent) is Line:
            e_s_l, e_e_l = extent.line_pos
            if s_s_l > e_s_l or e_e_l > s_e_l:
                return False
            if s_s_l < e_s_l and e_e_l < s_e_l:
                return True
            s_s_c, s_e_c = self.char_pos
            e_s_c, e_e_c = extent.char_pos
            return not ((s_s_l == e_s_l and s_s_c > e_s_c) or (s_e_l == e_e_l and s_e_c < e_e_c))

        if hasattr(extent, 'line') and type(extent.line) is Line:
            t_ext = extent.line
            e_s_l, e_e_l = t_ext.line_pos
            if s_s_l > e_s_l or e_e_l > s_e_l:
                return False
            if s_s_l < e_s_l and e_e_l < s_e_l:
                return True
            s_s_c, s_e_c = self.char_pos
            e_s_c, e_e_c = t_ext.char_pos
            return not ((s_s_l == e_s_l and s_s_c > e_s_c) or (s_e_l == e_e_l and s_e_c < e_e_c))

        if isinstance(extent, cc.SourceRange):
            e_s_l, e_e_l = extent.start.line, extent.end.line
            if s_s_l > e_s_l or e_e_l > s_e_l:
                return False
            if s_s_l < e_s_l and e_e_l < s_e_l:
                return True
            s_s_c, s_e_c = self.char_pos
            e_s_c, e_e_c = extent.start.column, extent.end.column
            return not ((s_s_l == e_s_l and s_s_c > e_s_c) or (s_e_l == e_e_l and s_e_c < e_e_c))

        return False

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Line):
            return self.line_pos == other.line_pos and self.char_pos == other.char_pos
        return False

    def __str__(self) -> str:
        """Line to str with empty detection."""
        if (self.line_pos == (0, 0)) and (self.char_pos == (0, 0)):
            return "None"

        if G.OVERRIDE_C_AST_LINE_PRINT and self.code:
            return f"(S{self.line_pos[0]}[{self.char_pos[0]}], E{self.line_pos[1]}[{self.char_pos[1]}], C\u00ad<{self.code}>)"

        return f"(S{self.line_pos[0]}[{self.char_pos[0]}], E{self.line_pos[1]}[{self.char_pos[1]}])"


def safe_spelling(token) -> str:
    """Safely return token.spelling, checking cached spelling_str and caching in-place."""
    if (cached := getattr(token, "spelling_str", None)) is not None:
        return cached
    try:
        spelling = token.spelling
    except (UnicodeDecodeError, Exception):
        spelling = ""
    try:
        token.spelling_str = spelling
    except Exception:
        pass
    return spelling


def safe_cursor_spelling(cursor) -> str:
    """Safely return cursor.spelling, caching in-place on cursor._spelling_str."""
    if (cached := getattr(cursor, "_spelling_str", None)) is not None:
        return cached
    try:
        spelling = cursor.spelling or ""
    except (UnicodeDecodeError, Exception):
        spelling = ""
    try:
        cursor._spelling_str = spelling
    except Exception:
        pass
    return spelling



class Ast:
    """Building block of parser.

    Allow to push to tables using extract.
    """

    def ast_debug(self, CS: ChangeSetType, ast_id_route: RouteType) -> None:
        """Add CS infos to m_ast_debug."""
        CS.store(m_ast_debug.set(
            CS.ref(m_ast.ast_id, *ast_id_route),
            json.dumps(self.__dict__, default=serializer),
        ))
        return

    def tag(self, CS: ChangeSetType, ast_id_route: RouteType, extent: Line|None=None) -> None:
        """Create or recycle tag for current AST."""
        self.extent = extent
        if self.extent is None:
            self.extent = Line(0, 0)
        else:
            parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
            if parser_obj and hasattr(parser_obj, "rawfile"):
                self.extent.cc(parser_obj.rawfile)

        ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)

        current_tag = (
            None,
            CS.gp.VID,
            0,
            self.extent.code,
            ast_ref,
            0,
            0,
        )

        if CS.prior_tags and (self.extent.code != ""):
            lookup = getattr(CS, "prior_tags_map", None)
            if lookup is not None:
                tag_list = lookup.get(self.extent.code)
                if tag_list is not None:
                    tag_match = None
                    for item in tag_list:
                        if item[0] not in CS.active_tag_list:
                            tag_match = item
                            break
                    if tag_match is not None:
                        x, tag_id = tag_match
                        if isinstance(CS.active_tag_list, set):
                            CS.active_tag_list.add(x)
                        else:
                            CS.active_tag_list.append(x)
                        CS.store(m_bridge_tag.set(
                            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                            tag_id,
                            self.extent.line_pos[0],
                            self.extent.line_pos[1],
                            self.extent.char_pos[0],
                            self.extent.char_pos[1],
                        ))
                        return
            else:
                for x, tag in enumerate(CS.prior_tags):
                    if x in CS.active_tag_list:
                        continue
                    # If tag found in prior_tags, set bridge and return
                    if tag[9] == self.extent.code:
                        if isinstance(CS.active_tag_list, set):
                            CS.active_tag_list.add(x)
                        else:
                            CS.active_tag_list.append(x)
                        CS.store(m_bridge_tag.set(
                            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                            tag[1],
                            self.extent.line_pos[0],
                            self.extent.line_pos[1],
                            self.extent.char_pos[0],
                            self.extent.char_pos[1],
                        ))
                        return

        with CS(REF_POS):
            # Create tag
            CS.store(m_tag.set(*current_tag))
            tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        # Create bridge tag
        CS.store(m_bridge_tag.set(
            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
            tag_ref,
            self.extent.line_pos[0],
            self.extent.line_pos[1],
            self.extent.char_pos[0],
            self.extent.char_pos[1],
        ))

        # Create map and bridge map
        self.map_ast(CS, ast_ref, tag_ref, self.extent)
        return

    def map_ast(
        self,
        CS: ChangeSetType,
        ast_id_route: RouteType | Any,
        tag_route: RouteType | Any,
        extent: Line | None = None,
    ) -> None:
        """Create m_map_ast and m_bridge_map spatial coordinate entries for this AST node."""
        ext = extent if extent is not None else getattr(self, "extent", None)
        if ext is None:
            return

        line_s = 1
        char_s = 1
        if hasattr(self, "endif") and self.endif and self.endif.line_pos[0] > 0:
            end_line = self.endif.line_pos[1]
            line_e = max(1, end_line - ext.line_pos[0] + 1)
            char_e = self.endif.char_pos[1] if self.endif.char_pos[1] > 0 else ext.char_pos[1]
        else:
            line_e = max(1, ext.line_pos[1] - ext.line_pos[0] + 1)
            char_e = ext.char_pos[1]

        ast_target = ast_id_route if (type(ast_id_route) is tuple and len(ast_id_route) == 3 and ast_id_route[1] == OP_REF) or type(ast_id_route) is int else CS.ref(m_ast.ast_id, *ast_id_route)
        tag_target = tag_route if (type(tag_route) is tuple and len(tag_route) == 3 and tag_route[1] == OP_REF) or type(tag_route) is int else CS.ref(m_tag.tag_id, *tag_route)

        CS.store(m_map_ast.set(
            tag_target,
            line_s,
            char_s,
            line_e,
            char_e,
            ast_target,
        ))
        if not hasattr(CS, "register_bridge_map") or CS.register_bridge_map(tag_target, tag_target):
            CS.store(m_bridge_map.set(
                tag_target,
                tag_target,
            ))
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Unknown AST default."""
        # Create ast
        with CS(REF_POS):
            CS.store(m_ast.set(None, f"AST{len(CS.cs)}", 0))
            ast_id_route = CS.get_route_parse()
        
        with CS(REF_NO_REF):
            # Create ast_debug
            self.ast_debug(CS, ast_id_route)

            # Create tag and bridge_tag
            self.tag(CS, ast_id_route)
        return

    def extract_1arg(self, CS: ChangeSetType, type_id: int, name: int|str, extent: Line|None=None) -> None:
        """m_ast only handler."""
        # Create ast
        with CS(REF_POS):
            CS.store(m_ast.view(((m_ast.ast_id,),), None, name, type_id))
            ast_id_route = CS.get_route_parse()

        with CS(REF_NO_REF):
            # Create ast_debug
            if G.OVERRIDE_FORCE_AST_DEBUG:
                self.ast_debug(CS, ast_id_route)

            # Create tag and bridge_tag
            self.tag(CS, ast_id_route, extent)
        return


    def within_range(self, token, ast_kind) -> bool:
        """Check if token is within Type/CPPro. Called from Zone"""
        if not self.need_processing:
            return False
        tline = token.line
        tspelling = token.spelling_str
        match self.end_mode:
            case End_Mode.No_Check:
                self.extent.grow(tline)
                return True
            case End_Mode.Auto:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(tline)
                    return True
                if tspelling == ";":
                    self.need_processing = False
                    return False
            case End_Mode.Semicolon:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(tline)
                    return True
                if tspelling == ";":
                    self.need_processing = False
                    return False
            case End_Mode.Comma:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(tline)
                    return True
                if tspelling == ",":
                    self.need_processing = False
                    return False
            case End_Mode.Extent:
                if not self.extent.is_inside(tline):
                    self.need_processing = False
                    return False
        self.extent.grow(tline)
        return True

    def em_auto_check(self, cursor) -> None:
        """No-op: Extents are managed by parent zones and token delimiters."""
        return

    def exec_filter(self, token, cursor, kind) -> None:
        match kind:
            case AST_KIND.comment:
                return self.exec_comment(token, cursor)
            case AST_KIND.punctuation:
                return self.exec_punctuation(token, cursor)
            case AST_KIND.keyword:
                return self.exec_keyword(token, cursor)
            case AST_KIND.identifier:
                return self.exec_identifier(token, cursor)
            case AST_KIND.literal:
                return self.exec_literal(token, cursor)

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        return

    def exec_keyword(self, token, cursor) -> None:
        return

    def exec_identifier(self, token, cursor) -> None:
        return

    def exec_literal(self, token, cursor) -> None:
        return

    def clean(self):
        return


    def __str__(self) -> str:
        """Passthrough to good_looking_printing."""
        return good_looking_printing(self, COLOR.red(f"\n{type(self).__name__}: "))


class Ast_Comment(Ast):
    """type_id ASTT.C_Comment."""

    def __init__(self, extent: Line, comment: str = "") -> None:
        self.extent = extent
        self.comment = comment

    def within_range(self, token, ast_kind) -> bool:
        return False

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        comment_name = self.comment[:255] if self.comment else ""
        self.extract_1arg(CS, ASTT.C_Comment, comment_name, self.extent)
        return

class Ast_Keyword(Ast_Comment):
    """type_id 2."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent

    # rip this shit
    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, 2, self.comment, self.extent)
        return

class Ast_ASM_Macro(Ast):
    """type_id ASTT.ASM_Macro."""

    def __init__(self, extent: Line, name: str = "") -> None:
        self.extent = extent
        self.name = name
        self.need_processing = True
        self.end_mode = End_Mode.No_Check
        self.saw_dot = False

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        self.extent.grow(token.line)
        tspelling = token.spelling_str
        if tspelling == ".":
            self.saw_dot = True
            return True
        if self.saw_dot and tspelling == "endm":
            self.need_processing = False
            return True
        if tspelling == ".endm":
            self.need_processing = False
            return True
        self.saw_dot = False
        return True

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        return

    def exec_keyword(self, token, cursor) -> None:
        if not self.name:
            self.name = token.spelling_str
        return

    def exec_identifier(self, token, cursor) -> None:
        if not self.name:
            self.name = token.spelling_str
        return

    def exec_literal(self, token, cursor) -> None:
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.ASM_Macro, self.name or "macro", self.extent)
        return


class Ast_ASM_Directive(Ast):
    """type_id ASTT.ASM_Directive."""

    def __init__(self, extent: Line, directive: str = "") -> None:
        self.extent = extent
        self.directive = directive
        self.need_processing = True
        self.end_mode = End_Mode.No_Check

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        if token.spelling_str == "macro" and self.directive == ".":
            self.__class__ = Ast_ASM_Macro
            self.__init__(self.extent)
            return True
        tline = token.line
        if tline.line_pos[0] <= self.extent.line_pos[1]:
            self.extent.grow(tline)
            return True
        self.need_processing = False
        return False

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.directive += (" " if self.directive else "") + token.spelling_str
        return

    def exec_keyword(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.directive += (" " if self.directive else "") + token.spelling_str
        return

    def exec_identifier(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.directive += (" " if self.directive else "") + token.spelling_str
        return

    def exec_literal(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.directive += (" " if self.directive else "") + token.spelling_str
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        dir_name = self.directive[:255] if self.directive else ".directive"
        self.extract_1arg(CS, ASTT.ASM_Directive, dir_name, self.extent)
        return


class Ast_ASM_Comment(Ast):
    """type_id ASTT.ASM_Comment."""

    def __init__(self, extent: Line, comment: str = "") -> None:
        self.extent = extent
        self.comment = comment
        self.need_processing = True
        self.end_mode = End_Mode.No_Check

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        tline = token.line
        if tline.line_pos[0] <= self.extent.line_pos[1]:
            self.extent.grow(tline)
            return True
        self.need_processing = False
        return False

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.comment += (" " if self.comment else "") + token.spelling_str
        return

    def exec_keyword(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.comment += (" " if self.comment else "") + token.spelling_str
        return

    def exec_identifier(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.comment += (" " if self.comment else "") + token.spelling_str
        return

    def exec_literal(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.comment += (" " if self.comment else "") + token.spelling_str
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        c_name = self.comment[:255] if self.comment else ""
        self.extract_1arg(CS, ASTT.ASM_Comment, c_name, self.extent)
        return


class Ast_ASM_Instruction(Ast):
    """type_id ASTT.ASM_Instruction."""

    def __init__(self, extent: Line, instruction: str = "") -> None:
        self.extent = extent
        self.instruction = instruction
        self.need_processing = True
        self.end_mode = End_Mode.No_Check

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        tline = token.line
        if tline.line_pos[0] <= self.extent.line_pos[1]:
            self.extent.grow(tline)
            return True
        self.need_processing = False
        return False

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.instruction += (" " if self.instruction else "") + token.spelling_str
        return

    def exec_keyword(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.instruction += (" " if self.instruction else "") + token.spelling_str
        return

    def exec_identifier(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.instruction += (" " if self.instruction else "") + token.spelling_str
        return

    def exec_literal(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.instruction += (" " if self.instruction else "") + token.spelling_str
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        ins_name = self.instruction[:255] if self.instruction else "instruction"
        self.extract_1arg(CS, ASTT.ASM_Instruction, ins_name, self.extent)
        return


class Ast_ASM_Label(Ast):
    """type_id ASTT.ASM_Label."""

    def __init__(self, extent: Line, name: str = "") -> None:
        self.extent = extent
        self.name = name

    def within_range(self, token, ast_kind) -> bool:
        return False

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.ASM_Label, self.name, self.extent)
        return


class Ast_MACRO_INSTANTIATION(Ast):
    def __init__(self, line: Line, func_name: Line) -> None:
        self.extent = line
        self.is_function = False
        self.function_args = []
        if self.extent != func_name:
            self.func_name = func_name

    def exec_comment(self, token, cursor):
        return

    def exec_punctuation(self, token, cursor):
        if self.is_function:
            if token.spelling_str == ",":
                self.function_args.append(Line())
            else:
                self.function_args[-1].new_end(token.line)
        elif token.spelling_str == "(":
            self.function_args.append(Line())
            self.is_function = True
        
        return

    def exec_keyword(self, token, cursor):
        if self.is_function:
            self.function_args[-1].new_end(token.line)

        return

    def exec_identifier(self, token, cursor):
        if self.is_function:
            self.function_args[-1].new_end(token.line)

        return

    def exec_literal(self, token, cursor):
        if self.is_function:
            self.function_args[-1].new_end(token.line)

        return


class AST_KIND(IntEnum):
    NOT_SET = 0
    comment = auto()
    punctuation = auto()
    keyword = auto()
    identifier = auto()
    literal = auto()

# RANGE 100 is reserved for CPPro
class CPPro(Ast):
    def __init__(self, cppro_ex: Line):
        self.ccpro_start_extent = cppro_ex
        self.extent = Line(cppro_ex)
        self.need_processing = True
        self.end_mode = End_Mode.No_Check

    def ccpro_start_flip(self, cppro_class, *args) -> None:
        start_extent = Line(self.ccpro_start_extent)
        if args and isinstance(args[0], Line):
            if args[0].line_pos[0] != 0:
                start_extent.grow(args[0])
            args = (start_extent, *args[1:])
        else:
            args = (start_extent, *args)
        self.__class__ = cppro_class
        self.__init__(*args)

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        tline = token.line
        # If token is on a subsequent line before CPPro was flipped to a concrete directive,
        # the lone '#' was a null preprocessor directive.
        if tline.line_pos[0] > self.extent.line_pos[1]:
            self.need_processing = False
            return False
        return True

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        return

    def exec_keyword(self, token, cursor) -> None:
        self.exec_identifier(token, cursor)
        return 

    def exec_identifier(self, token, cursor) -> None:
        tspelling = token.spelling_str
        cline = get_cursor_line(cursor)
        match tspelling:
            case 'if':
                self.ccpro_start_flip(CPPro_if, cline)
            case 'elif':
                self.ccpro_start_flip(CPPro_elif, cline)
            case 'else':
                self.ccpro_start_flip(CPPro_else, cline)
            case 'endif':
                self.ccpro_start_flip(CPPro_endif, cline)
            case 'ifdef':
                self.ccpro_start_flip(CPPro_ifdef, cline)
            case 'ifndef':
                self.ccpro_start_flip(CPPro_ifndef, cline)
            case 'elifdef':
                self.ccpro_start_flip(CPPro_elifdef, cline)
            case 'elifndef':
                self.ccpro_start_flip(CPPro_elifndef, cline)
            case 'define':
                self.ccpro_start_flip(CPPro_define, cline)
            case 'undef':
                self.ccpro_start_flip(CPPro_undef, cline)
            case 'include':
                self.ccpro_start_flip(CPPro_include, cline if cursor.kind == cc.CursorKind.INCLUSION_DIRECTIVE else token.line)
                self.exec_identifier(token, cursor)
            case 'embed':
                logger.warn(f"#embed detected {cursor.extent}, NOT IMPLEMENTED!!")
                return
            case 'line':
                self.ccpro_start_flip(CPPro_line, cline)
            case 'error':
                self.ccpro_start_flip(CPPro_error, cline)
            case 'warning' | 'warn':
                self.ccpro_start_flip(CPPro_warning, cline)
            case 'pragma':
                self.ccpro_start_flip(CPPro_pragma, cline)

            case _:
                self.ccpro_start_flip(Ast_ASM_Comment, cline, tspelling)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Do not emit database AST records for unflipped or non-directive CPPro instances."""
        return


    def exec_literal(self, token, cursor) -> None:
        return




class CPPro_if(Ast):
    """type_id 102."""

    def __init__(self, extent: Line, expression: str = "") -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.No_Check
        self.expression = expression
        self.highlight = Line()
        self.endif = Line()

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        tline = token.line
        if tline.line_pos[0] <= self.extent.line_pos[1]:
            self.extent.grow(tline)
            return True
        self.need_processing = False
        return False

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.expression += token.spelling_str
        self.highlight.new_end(token.line)
        return

    def exec_keyword(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.expression += token.spelling_str
        self.highlight.new_end(token.line)
        return

    def exec_identifier(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.expression += token.spelling_str
        self.highlight.new_end(token.line)
        return

    def exec_literal(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.expression += token.spelling_str
        self.highlight.new_end(token.line)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_if, self.expression, self.extent)
        return


class CPPro_elif(CPPro_if):
    """type_id 103."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_elif, self.expression, self.extent)
        return



class CPPro_else(Ast):
    """type_id 104."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = False
        self.end_mode = End_Mode.No_Check
        self.expression = ""
        self.endif = Line()

    def within_range(self, token, ast_kind) -> bool:
        return False

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_else, "", self.extent)
        return


class CPPro_endif(Ast):
    """type_id 105."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = False
        self.end_mode = End_Mode.No_Check

    def within_range(self, token, ast_kind) -> bool:
        return False

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_endif, "", self.extent)
        return

class CPPro_ifdef(CPPro_if):
    """type_id 100."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_ifdef, self.expression, self.extent)
        return


class CPPro_ifndef(CPPro_if):
    """type_id 101."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_ifndef, self.expression, self.extent)
        return

class CPPro_elifdef(CPPro_elif):
    """type_id 999."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_elifdef, self.expression, self.extent)
        return

class CPPro_elifndef(CPPro_elif):
    """type_id 999."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_elifndef, self.expression, self.extent)
        return


class CPPro_define(Ast):
    """type_id 106."""

    def __init__(self, extent: Line, identifier: str = "", replacement: str = "") -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.No_Check
        self.identifier = identifier
        self.func_args = []
        self.func_enabled = False
        self.replacement = replacement
        self.highlight = Line()
        self.highlight_replacement = Line()

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        tline = token.line
        if tline.line_pos[0] <= self.extent.line_pos[1]:
            self.extent.grow(tline)
            return True
        self.need_processing = False
        return False

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.extent.grow(token.line)
        spelling = token.spelling_str
        if self.func_enabled:
            if spelling == ")":
                self.func_enabled = False
                return
            elif spelling == ",":
                self.func_args.append("")
            else:
                self.func_args[-1] += spelling
            return
        
        elif self.replacement == "" and spelling == "(":
            self.func_enabled = True
            self.func_args.append("")
            return

        self.replacement += spelling
        self.highlight_replacement.new_end(token.line)
        return

    def exec_keyword(self, token, cursor) -> None:
        self.extent.grow(token.line)
        spelling = token.spelling_str
        if self.identifier == "":
            self.identifier += spelling
            self.highlight.new_end(token.line)
            return

        if self.func_enabled:
            self.func_args[-1] += spelling
            return

        self.replacement += spelling
        self.highlight_replacement.new_end(token.line)
        return

    def exec_identifier(self, token, cursor) -> None:
        self.extent.grow(token.line)
        spelling = token.spelling_str
        if self.identifier == "":
            self.identifier += spelling
            self.highlight.new_end(token.line)
            return

        if self.func_enabled:
            self.func_args[-1] += spelling
            return

        self.replacement += spelling
        self.highlight_replacement.new_end(token.line)
        return

    def exec_literal(self, token, cursor) -> None:
        self.extent.grow(token.line)
        spelling = token.spelling_str
        if self.func_enabled:
            self.func_args[-1] += spelling
            return

        self.replacement += spelling
        self.highlight_replacement.new_end(token.line)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        if self.replacement is None or self.replacement == "":
            # Empty define
            self.extract_1arg(CS, ASTT.CPPro_define, self.identifier, self.extent)
            return

        # BAD IMPLEMENTATION, NEEDS TO BE FIXED, WE NEED RECURSIVE DETECTION FOR 2ND ARG
        self.extract_1arg(CS, ASTT.CPPro_define_macro, self.identifier, self.extent)

        return


class CPPro_undef(Ast):
    """type_id 107."""

    def __init__(self, extent: Line, identifier: str = "") -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.No_Check
        self.identifier = identifier
        self.highlight = Line()

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        tline = token.line
        if tline.line_pos[0] <= self.extent.line_pos[1]:
            self.extent.grow(tline)
            return True
        self.need_processing = False
        return False

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.identifier += token.spelling_str
        self.highlight.new_end(token.line)
        return

    def exec_keyword(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.identifier += token.spelling_str
        self.highlight.new_end(token.line)
        return

    def exec_identifier(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.identifier += token.spelling_str
        self.highlight.new_end(token.line)
        return

    def exec_literal(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.identifier += token.spelling_str
        self.highlight.new_end(token.line)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_undef, self.identifier, self.extent)
        return


class CPPro_include(Ast):
    """type_id 108."""

    def __init__(self, extent: Line, written_include: str = "", actual_include: str = "") -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.No_Check
        self.a_include = actual_include if actual_include else None
        self.w_include = written_include
        self.highlight = Line()
        self.debug = None

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        tline = token.line
        if tline.line_pos[0] <= self.extent.line_pos[1]:
            self.extent.grow(tline)
            return True
        self.need_processing = False
        return False

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.w_include += token.spelling_str
        if self.debug:
            self.a_include = self.w_include
        self.highlight.new_end(token.line)
        return

    def exec_keyword(self, token, cursor) -> None:
        self.extent.grow(token.line)
        spelling = token.spelling_str
        if self.a_include is None and spelling == "include":
            try:
                inc_file = cursor.get_included_file()
                if G.MF and inc_file:
                    self.a_include = G.MF.resolve_path(inc_file.name)
                else:
                    self.debug = "#FAILED_RESOLVE"
            except (AssertionError, Exception):
                self.debug = "#FAILED_RESOLVE"
            return

        self.w_include += spelling
        if self.debug:
            self.a_include = self.w_include
        self.highlight.new_end(token.line)
        return

    def exec_identifier(self, token, cursor) -> None:
        self.extent.grow(token.line)
        spelling = token.spelling_str
        if self.a_include is None and spelling == "include":
            try:
                inc_file = cursor.get_included_file()
                if G.MF and inc_file:
                    self.a_include = G.MF.resolve_path(inc_file.name)
                else:
                    self.debug = "#FAILED_RESOLVE"
            except (AssertionError, Exception):
                self.debug = "#FAILED_RESOLVE"
            return

        self.w_include += spelling
        if self.debug:
            self.a_include = self.w_include
        self.highlight.new_end(token.line)
        return

    def exec_literal(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.w_include += token.spelling_str
        if self.debug:
            self.a_include = self.w_include
        self.highlight.new_end(token.line)
        return


    #################################REMEMBER TO CLEAN A_INCLUDE IF DEBUG ISN'T NONE
    def extract(self, CS: ChangeSetType) -> None:
        """Extract with m_ast_include."""
        include_target = self.a_include if self.a_include is not None else self.w_include
        with CS(REF_POS):
            CS.store(m_file_name.get_set(None, include_target))
            fnid_route = CS.get_route_parse()
        
        with CS(REF_POS):
            # Create ast
            CS.store(m_ast.view(
                ((m_ast.ast_id, m_ast_include.ast_id, 1),),
                None,
                self.w_include,
                ASTT.CPPro_include,
                None,
                CS.ref(m_file_name.fnid, *fnid_route),
            ))
            ast_id_route = CS.get_route_parse()

        with CS(REF_NO_REF):
            # Create ast_debug
            if G.OVERRIDE_FORCE_AST_DEBUG:
                self.ast_debug(CS, ast_id_route)

            # Create tag and bridge_tag
            self.tag(CS, ast_id_route, self.extent)
        return

##NOT IN USE
class CPPro_embed(Ast):
    """type_id 999."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.Extent
        #self.identifier = identifier

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, 999, self.identifier, self.extent)
        return


class CPPro_line(Ast):
    """type_id 109."""

    def __init__(self, extent: Line, lineno: int | str = "", filename: str | None = None) -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.No_Check
        self.lineno = lineno
        self.hl_lineno = Line()
        self.filename = filename
        self.hl_filename = Line()

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        tline = token.line
        if tline.line_pos[0] <= self.extent.line_pos[1]:
            self.extent.grow(tline)
            return True
        self.need_processing = False
        return False

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.extent.grow(token.line)
        spelling = token.spelling_str
        if self.lineno == "":
            self.lineno = spelling
            self.hl_lineno = Line(token.line)
            return

        self.filename = spelling
        self.hl_filename = Line(token.line)
        return

    def exec_keyword(self, token, cursor) -> None:
        self.extent.grow(token.line)
        spelling = token.spelling_str
        if self.lineno == "":
            self.lineno = spelling
            self.hl_lineno = Line(token.line)
            return

        self.filename = spelling
        self.hl_filename = Line(token.line)
        return

    def exec_identifier(self, token, cursor) -> None:
        self.extent.grow(token.line)
        spelling = token.spelling_str
        if self.lineno == "":
            self.lineno = spelling
            self.hl_lineno = Line(token.line)
            return

        self.filename = spelling
        self.hl_filename = Line(token.line)
        return

    def exec_literal(self, token, cursor) -> None:
        self.extent.grow(token.line)
        spelling = token.spelling_str
        if self.lineno == "":
            self.lineno = spelling
            self.hl_lineno = Line(token.line)
            return

        self.filename = spelling
        self.hl_filename = Line(token.line)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        if self.filename is None:
            self.extract_1arg(CS, ASTT.CPPro_line, f"{self.lineno}", self.extent)
        else:
            self.extract_1arg(CS, ASTT.CPPro_line, f"{self.lineno} {self.filename}", self.extent)
        return


class CPPro_error(Ast):
    """type_id 110."""

    def __init__(self, extent: Line, msg: str = "") -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.No_Check
        self.msg = msg
        self.hl_msg = Line()

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False
        tline = token.line
        if tline.line_pos[0] <= self.extent.line_pos[1]:
            self.extent.grow(tline)
            return True
        self.need_processing = False
        return False

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.msg += token.spelling_str
        self.hl_msg.new_end(token.line)
        return

    def exec_keyword(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.msg += token.spelling_str
        self.hl_msg.new_end(token.line)
        return

    def exec_identifier(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.msg += token.spelling_str
        self.hl_msg.new_end(token.line)
        return

    def exec_literal(self, token, cursor) -> None:
        self.extent.grow(token.line)
        self.msg += token.spelling_str
        self.hl_msg.new_end(token.line)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_error, self.msg, self.extent)
        return

class CPPro_warning(CPPro_error):
    """type_id 999."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_warning, self.msg, self.extent)
        return


class CPPro_pragma(CPPro_error):
    """type_id 111."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_pragma, self.msg, self.extent)
        return

##NOT IN USE
class CPPro_defined(Ast):
    """type_id 999."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.Extent
        #self.defined = defined

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, 999, self.defined, self.extent)
        return

class End_Mode(IntEnum):
    No_Check = 0
    Auto = 1
    Semicolon = 2
    Comma = 3
    Extent = 4


class CQual(Flag):
    Empty = 0
    const = 1
    volatile = 2
    restrict = 4
    _Atomic = 8

    def output_ast(self) -> tuple | None:
        if self.value == 0:
            return None
        
        current_list = []

        self.value

        if CQual.const in self:
            current_list.append(ASTT.C_Qconst)
        if CQual.volatile in self:
            current_list.append(ASTT.C_Qvolatile)
        if CQual.restrict in self:
            current_list.append(ASTT.C_Qrestrict)
        if CQual._Atomic in self:
            current_list.append(ASTT.C_Q_Atomic)

        return tuple(current_list)

def get_decl_type(ast_type: int) -> int:
    """Return declaration AST type (e.g. C_struct -> C_structdecl)."""
    match ast_type:
        case ASTT.C_struct:
            return ASTT.C_structdecl
        case ASTT.C_union:
            return ASTT.C_uniondecl
        case ASTT.C_enum:
            return ASTT.C_enumdecl
        case ASTT.C_functionproto:
            return ASTT.C_functionprotodecl
        case _:
            return ast_type + 1


def get_notbind_type(ast_type: int) -> int:
    """Return unbound forward AST type (e.g. C_struct -> C_structnotbind)."""
    match ast_type:
        case ASTT.C_struct:
            return ASTT.C_structnotbind
        case ASTT.C_union:
            return ASTT.C_unionnotbind
        case ASTT.C_enum:
            return ASTT.C_enumnotbind
        case ASTT.C_functionproto:
            return ASTT.C_functionprotnotbind
        case _:
            return ast_type + 2


class Not_Implemented(Ast):
    def __init__(self, extent: Line, end_mode: int=End_Mode.Extent) -> None:
        self.extent = extent
        self.end_mode = end_mode
        # Flag to be set to False once we are past the appropriate extent of the type.
        self.need_processing = True
        self.data = []
        self.brace_depth = 0

    def within_range(self, token, ast_kind) -> bool:
        """Swallow all tokens within compound statement / unhandled block."""
        if not self.need_processing:
            return False

        if ast_kind == AST_KIND.punctuation:
            tspelling = token.spelling_str
            if tspelling == "{":
                self.brace_depth += 1
            elif tspelling == "}":
                self.brace_depth -= 1
                if self.brace_depth <= 0:
                    self.extent.grow(token.line)
                    self.need_processing = False
                    return True

        if self.end_mode == End_Mode.Extent and not self.extent.is_inside(token.line):
            if self.brace_depth <= 0:
                self.need_processing = False
                return False

        self.extent.grow(token.line)
        return True

    def exec_filter(self, token, cursor, ast_kind):
        if ast_kind == AST_KIND.comment:
            return
        self.data.append(token.spelling_str)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Do not emit AST database records for unextracted code blocks."""
        return


class AST_Expression(Ast):
    pass


class AST_Enum_Equal(AST_Expression):
    def __init__(self, extent: Line, end_mode: int=End_Mode.Extent) -> None:
        self.extent = extent
        self.end_mode = end_mode
        # Flag to be set to False once we are past the appropriate extent of the type.
        self.need_processing = True
        self.data = []

    def exec_filter(self, token, cursor, ast_kind):
        if ast_kind == AST_KIND.comment:
            return
        if ast_kind == AST_KIND.punctuation:
            if token.spelling_str == "}":
                self.extent.new_end_reversed(token.line)
                self.need_processing = False
                return
        self.data.append(token.spelling_str)
        return

class AST_Array(AST_Expression):
    def __init__(self, extent: Line, end_mode: int=End_Mode.Extent) -> None:
        self.extent = extent
        self.end_mode = end_mode
        # Flag to be set to False once we are past the appropriate extent of the type.
        self.need_processing = True
        self.data = []
        self.bracket_depth = 1

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False

        if ast_kind == AST_KIND.punctuation:
            spelling = token.spelling_str
            if spelling == "[":
                self.bracket_depth += 1
            elif spelling == "]":
                self.bracket_depth -= 1
                if self.bracket_depth <= 0:
                    self.extent.new_end_reversed(token.line)
                    self.need_processing = False
                    return True

        self.extent.grow(token.line)
        return True

    def exec_filter(self, token, cursor, ast_kind):
        if ast_kind == AST_KIND.comment:
            return
        self.data.append(token.spelling_str)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Do not emit AST database records for array size expressions."""
        return


class AST_Initializer(AST_Expression):
    def __init__(self, extent: Line, end_mode: int=End_Mode.Extent) -> None:
        self.extent = extent
        self.end_mode = end_mode
        self.need_processing = True
        self.data = []
        self.brace_depth = 0
        self.paren_depth = 0
        self.bracket_depth = 0

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False

        if ast_kind == AST_KIND.punctuation:
            spelling = token.spelling_str
            if spelling == "{":
                self.brace_depth += 1
            elif spelling == "}":
                self.brace_depth = max(0, self.brace_depth - 1)
            elif spelling == "(":
                self.paren_depth += 1
            elif spelling == ")":
                self.paren_depth = max(0, self.paren_depth - 1)
            elif spelling == "[":
                self.bracket_depth += 1
            elif spelling == "]":
                self.bracket_depth = max(0, self.bracket_depth - 1)
            elif spelling in {";", ","}:
                if self.brace_depth == 0 and self.paren_depth == 0 and self.bracket_depth == 0:
                    self.extent.new_end_reversed(token.line)
                    self.need_processing = False
                    return False

        self.extent.grow(token.line)
        return True

    def exec_filter(self, token, cursor, ast_kind):
        if ast_kind == AST_KIND.comment:
            return
        self.data.append(token.spelling_str)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Do not emit AST database records for variable initializer expressions."""
        return


class TypeToken():
    __slots__ = ("extent", "code", "type", "is_definition", "foreign_name", "foreign_file", "foreign_extent")

    def __init__(self, token, asttype: int = 0) -> None:
        if hasattr(token, 'line'):
            self.extent = token.line
            self.code = getattr(token, 'spelling_str', '')
        elif hasattr(token, 'extent'):
            self.extent = token.extent
            self.code = getattr(token, 'code', '') or getattr(token, 'spelling_str', '')
        else:
            self.extent = getattr(token, 'line', None) or Line(0, 0)
            self.code = getattr(token, 'spelling_str', '')
        self.type = asttype
        self.is_definition = False
        self.foreign_name = None
        self.foreign_file = None
        self.foreign_extent = None
        return

    def __repr__(self):
        return self.code

    def get_foreign(self, cursor) -> None:
        if cursor.is_definition():
            self.is_definition = True
            return

        if (foreign_cursor := cursor.get_definition()) is None:
            return
        self.foreign_name = safe_cursor_spelling(foreign_cursor)
        self.foreign_file = foreign_cursor.extent.start.file
        self.foreign_extent = get_cursor_line(foreign_cursor)
        return


class TSRef(IntEnum):
    No_Ref = 0
    AST_Ref = 1
    Route_Ref = 2


class TypeSegment():
    __slots__ = ("content", "cqual", "cqual_content", "ref_type", "ref", "type_id", "ref_ast_id")

    def __init__(self) -> None:
        self.content = []
        self.cqual = CQual.Empty
        self.cqual_content = []
        self.ref_type = TSRef.No_Ref
        self.ref = None

        self.type_id = None
        self.ref_ast_id = None
        return

    def __repr__(self):
        return f"TypeSegment:{self.cqual_content} {self.content}"

    def __bool__(self) -> bool:
        return bool(self.content or self.cqual_content)

    def is_definition(self):
        return any(typetoken.is_definition for typetoken in self.content)

    def extend(self, typesegment) -> None:
        self.content.extend(typesegment.content)
        self.cqual = self.cqual | typesegment.cqual
        self.cqual_content.extend(typesegment.cqual_content)
        return

    def append(self, item) -> None:
        self.content.append(item)
        return

    def append_cqal(self, item) -> None:
        self.cqual_content.append(item)
        return

    def get_foreign(self, cursor) -> None:
        self.content[-1].get_foreign(cursor)
        return

    def generate_ast(self, CS: ChangeSetType) -> None:
        self.type_id = None
        self.ref_ast_id = None

        if self.ref_type == TSRef.No_Ref:
            if len(self.content) == 1 and self.cqual == CQual.Empty:
                self.type_id = self.content[0].type
                return

            if (
                self.content
                and self.content[0].type in {ASTT.C_struct, ASTT.C_functionproto, ASTT.C_union, ASTT.C_enum}
                and len(self.content) == 2
                and self.cqual == CQual.Empty
            ):
                notbind_type = get_notbind_type(self.content[0].type)
                op_idx = len(CS.cs)
                with CS(REF_NO_REF):
                    CS.store(m_ast.get_set(
                        None,
                        self.content[1].code,
                        notbind_type,
                    ))
                route_key = (REF_POS, op_idx)
                self.type_id = self.content[0].type
                self.ref_type = TSRef.Route_Ref
                self.ref = route_key
                return

            compound = []
            if (cqual_out := self.cqual.output_ast()) is not None:
                for item in cqual_out:
                    compound.append((item, 0))

            for i, typetoken in enumerate(self.content):
                if typetoken.type in {ASTT.C_struct, ASTT.C_functionproto, ASTT.C_union, ASTT.C_enum}:
                    if i > 0 and self.content[i - 1].type == typetoken.type:
                        notbind_type = get_notbind_type(typetoken.type)
                        op_idx = len(CS.cs)
                        with CS(REF_NO_REF):
                            CS.store(m_ast.get_set(
                                None,
                                typetoken.code,
                                notbind_type,
                            ))
                        compound.append((typetoken.type, CS.ref(m_ast.ast_id, REF_POS, op_idx)))
                else:
                    compound.append((typetoken.type, 0))

            view = []
            for i, item in enumerate(compound):
                t_code = int(item[0]) if hasattr(item[0], "value") else int(item[0])
                view.extend((None, i, t_code, item[1]))

            view = tuple(view)
            with CS(REF_POS):
                CS.store(m_ast.view(
                    ((m_ast.ast_id, m_ast_container.ast_id, len(compound)),),
                    None,
                    "",
                    ASTT.C_Compound,
                    *view,
                ))
                route_key = CS.get_route_parse()

                self.ref_type = TSRef.Route_Ref
                self.ref = route_key


class Zone_Type(IntEnum):
    Unset = 0
    Function_Args = 1
    Declared_Args = 2
    Compound_Stmt = 3
    Array_Content = 4
    Enum_Content = 5
    Enum_Equal = 6
    Full_File = 7
    Initializer_Expr = 8


_BRACE_ZONE_TYPES = frozenset({Zone_Type.Declared_Args, Zone_Type.Enum_Content, Zone_Type.Compound_Stmt})


class Zone:
    """Represent spatial code scoping boundaries for parsing C code blocks in isolation.
    
    `Zone` instances scope sections of C code (such as function parameters, compound statements,
    or enum bodies). During ChangeSet extraction (`.extract(CS)`), `Zone` instances act as position
    locators, providing the stored operation index in `CS.cs` so parent and child AST nodes can resolve
    relational foreign keys via `CS.ref(...)`.
    """
    def __init__(self, zone_type:int, cursors_array) -> None:
        self.zone_type = zone_type
        self.preset_extents = deque()
        self.children = []
        self.completed = False
        self.extent = Line(0, 0)
        self.ast_type = C_Type
        self.end_mode = End_Mode.Auto
        self.A_Line_Dict = None

        if not cursors_array:
            return

        if zone_type != Zone_Type.Full_File:
            def _get_cur_file_name(c):
                f = getattr(c, "_file_name", None)
                if f is None:
                    file_obj = c.extent.start.file
                    f = file_obj.name if file_obj else ""
                    c._file_name = f
                return f

            cur_file = None
            for c in cursors_array:
                fn = _get_cur_file_name(c)
                if fn:
                    cur_file = fn
                    break
            if cur_file:
                cursors_array = tuple(
                    c for c in cursors_array if not _get_cur_file_name(c) or _get_cur_file_name(c) == cur_file
                )

        if zone_type == Zone_Type.Compound_Stmt:
            for cursor in cursors_array:
                self.extent.grow(get_cursor_line(cursor))
            self.children.append(Not_Implemented(self.extent))

        elif zone_type == Zone_Type.Enum_Equal:
            for cursor in cursors_array:
                self.extent.grow(get_cursor_line(cursor))
            self.children.append(AST_Enum_Equal(self.extent))

        elif zone_type == Zone_Type.Initializer_Expr:
            for cursor in cursors_array:
                self.extent.grow(get_cursor_line(cursor))
            self.children.append(AST_Initializer(self.extent))

        elif zone_type == Zone_Type.Array_Content:
            for cursor in cursors_array:
                self.extent.grow(get_cursor_line(cursor))
            self.children.append(AST_Array(self.extent))

        elif zone_type != Zone_Type.Full_File:
            for cursor in cursors_array:
                temp_ext = get_cursor_line(cursor)
                self.extent.grow(temp_ext)
                self.preset_extents.append(temp_ext)

        if zone_type == Zone_Type.Enum_Content:
            self.end_mode = End_Mode.Comma

    def check_exec(self, token, cursor, ast_kind):
        """Check if extent is part of Zone and exec if needed. Return True on exec."""
        if self.completed:
            return False

        tline = token.line
        tspelling = token.spelling_str

        # Capture comments into Ast_Comment AST nodes
        if ast_kind == AST_KIND.comment:
            self.children.append(Ast_Comment(tline, tspelling))
            return True

        if ast_kind == AST_KIND.punctuation and tspelling == "}":
            if self.zone_type in _BRACE_ZONE_TYPES:
                self.extent.grow(tline)
                self.preset_extents.clear()
                self.completed = True
                return True

        if (not self.extent.is_inside(tline)) and (self.zone_type != Zone_Type.Full_File):
            if not (self.children and self.children[-1].need_processing):
                return False

        # Fast path, the last children added
        if self.children:
            last_child = self.children[-1]
            if last_child.within_range(token, ast_kind):
                last_child.exec_filter(token, cursor, ast_kind)
                return True
            if ast_kind == AST_KIND.punctuation:
                if tspelling == "*":
                    last_child.extent.grow(get_cursor_line(cursor))
                    last_child.need_processing = True
                    last_child.exec_filter(token, cursor, ast_kind)
                    return True

        if ast_kind == AST_KIND.punctuation:
            # Commonly found between extents, Processing not needed.
            if tspelling in _PUNCT_IGNORED:
                return True
            # While were in punctuation, lets handle CPPros
            if tspelling == "#":
                kind = cursor.kind
                if kind == cc.CursorKind.INCLUSION_DIRECTIVE: 
                    self.children.append(CPPro_include(get_cursor_line(cursor)))
                    return True
                else:
                    self.children.append(CPPro(tline))
                    return True
            if tspelling == "." and self.zone_type == Zone_Type.Full_File:
                self.children.append(Ast_ASM_Directive(tline, "."))
                return True

        # Guard against statement keywords outside type declarations
        if ast_kind == AST_KIND.keyword and tspelling in _KEYWORD_IGNORED:
            return True

        # Check for valid preset_extents
        if self.preset_extents:
            while self.preset_extents and self.preset_extents[0].line_pos[1] < tline.line_pos[0]:
                self.preset_extents.popleft()

            for i, p_extent in enumerate(self.preset_extents):
                if p_extent.is_inside(tline):
                    # Quick reminder that libclang won't give us the extent of CPPros here...
                    self.children.append(self.ast_type(p_extent, End_Mode.Extent))
                    self.children[-1].exec_filter(token, cursor, ast_kind)

                    del self.preset_extents[i]
                    return True

        self.children.append(self.ast_type(tline, self.end_mode))
        self.children[-1].exec_filter(token, cursor, ast_kind)

        return True

    def extract(self, CS: ChangeSetType) -> None:
        for item in self.children:
            if isinstance(item, C_Type):
                item.extract(CS)
            else:
                with CS(REF_NO_REF):
                    item.extract(CS)

        return


    def gen_lined_dict(self):
        self.A_Line_Dict = {}
        for item in self.children:
            if self.A_Line_Dict.get(item.extent.line_pos[0]) is None:
                self.A_Line_Dict[item.extent.line_pos[0]] = []
            self.A_Line_Dict[item.extent.line_pos[0]].append(item)

        return

    def resolve_cppro_scopes(self) -> None:
        """Resolve the ending boundaries (endif line coordinates) for all CPPro_if* conditionals."""
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
                    group = cpp_stack.pop()
                    for branch in group:
                        if branch.endif.line_pos[0] == 0:
                            branch.endif = Line(item.extent.line_pos[1], item.extent.line_pos[1])


class C_Type(Ast):
    """Represent C type declarations, specifiers, qualifiers (`const`, `volatile`), and declarators.
    
    `C_Type` parses individual C statement lines or type declarations. It uses three internal mechanisms
    to accurately handle type qualifiers and pointers:
    - `self.content`: Accumulates current tokens until a swap trigger is encountered.
    - `self.swap_out()`: Moves accumulated tokens from `content` into structured `typedata`.
    - `self.typedata`: Stores list of `TypeSegment` tuples `([ASTT, ...], CQUAL)` maintaining type ordering.
    """
    def __init__(self, extent: Line, end_mode: int=End_Mode.Auto) -> None:
        self.extent = extent
        self.end_mode = end_mode
        self.name = ""
        # Flag to be set to False once we are past the appropriate extent of the type.
        self.need_processing = True
        # All Zones within this type.
        self.zones = []
        # Content being process, this is temporary and should end up in root.
        self.content = TypeSegment()
        # Final form of current type (pointer/const might need to be right merged).
        # Contains multiple ​​TypeSegment: ([ASTT, ASTT....],CQUAL)
        self.typedata = []
        # Allow us to capture arg from non func_decl function proto and
        # allow us to keep pointer+func_proto inside the same root index.
        self.func_proto = False

        self.storage_class = None

    def within_range(self, token, ast_kind) -> bool:
        """Check if token is within Type/Declaration scope. Called from Zone."""
        if not self.need_processing:
            return False

        tline = token.line
        tspelling = token.spelling_str

        if self.zones:
            for zone in self.zones:
                if zone.completed:
                    continue
                if zone.extent.is_inside(tline) or (zone.children and zone.children[-1].need_processing) or zone.preset_extents:
                    self.extent.grow(tline)
                    return True

        match self.end_mode:
            case End_Mode.No_Check:
                self.extent.grow(tline)
                return True
            case End_Mode.Auto | End_Mode.Semicolon:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(tline)
                    return True
                if tspelling == ";":
                    self.need_processing = False
                    return False
            case End_Mode.Comma:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(tline)
                    return True
                if tspelling == ",":
                    self.need_processing = False
                    return False
            case End_Mode.Extent:
                if not self.extent.is_inside(tline):
                    self.need_processing = False
                    return False

        self.extent.grow(tline)
        return True

    def extract(self, CS: ChangeSetType) -> None:
        # Makes sure that everything is in self.typedata
        if self.content:
            self.swap_out()

        # 1. Process Child Zones within Route
        zone_link = ()
        if self.zones:
            with CS(REF_MULTI):
                for zone in self.zones:
                    zone.extract(CS)
                zone_link = CS.route[-2:]

        # 2. Parse self.typedata into distinct declarations (final_types)
        # Groups shared base type specifier with individual declarators
        root_type = TypeSegment()
        type_constructor = []
        final_types = []

        for typesegment in self.typedata:
            if not root_type:
                root_type = typesegment

            type_constructor.append(typesegment)

            # Detects variable identifier tokens (type == 0) to split multiple declarators
            if typesegment.content and typesegment.content[-1].type == 0:
                final_types.append(tuple(type_constructor))
                type_constructor = [root_type]

        if not final_types and type_constructor:
            final_types.append(tuple(type_constructor))

        # 3. Zone Handling for Type Definitions (struct/union/enum/functionproto declarations)
        for final_type in final_types:
            for typesegment in final_type:
                if typesegment.is_definition():
                    for item in typesegment.content:
                        if item.is_definition and item.type in _DEF_TYPES:
                            decl_type = get_decl_type(item.type)
                            if item.type == ASTT.C_functionproto:
                                # Determine return type from preceding segments in final_type
                                ret_idx = final_type.index(typesegment)
                                return_segments = final_type[:ret_idx]
                                if not return_segments:
                                    return_segments = [root_type] if root_type != typesegment else []

                                if len(return_segments) == 1:
                                    ret_seg = return_segments[0]
                                    ret_seg.generate_ast(CS)
                                elif len(return_segments) > 1:
                                    ret_seg = TypeSegment()
                                    for seg in return_segments:
                                        ret_seg.extend(seg)
                                    ret_seg.generate_ast(CS)
                                else:
                                    ret_seg = TypeSegment()
                                    ret_seg.content.append(TypeToken(item, ASTT.C_void))
                                    ret_seg.generate_ast(CS)

                                ret_t_id = ret_seg.type_id
                                if ret_t_id is None:
                                    if ret_seg.ref_type == TSRef.Route_Ref and not ret_seg.content:
                                        ret_t_id = ASTT.C_Compound
                                    elif ret_seg.content:
                                        ret_t_id = ret_seg.content[0].type
                                    else:
                                        ret_t_id = ASTT.C_void

                                if ret_seg.ref_type == TSRef.Route_Ref:
                                    ret_ref_ast_id = CS.ref(m_ast.ast_id, *ret_seg.ref)
                                elif ret_seg.ref_type == TSRef.AST_Ref:
                                    ret_ref_ast_id = ret_seg.ref
                                else:
                                    ret_ref_ast_id = ret_seg.ref_ast_id if ret_seg.ref_ast_id is not None else 0

                                if zone_link:
                                    with CS(REF_POS):
                                        CS.store(m_ast.ref_view(
                                            ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                                            None,
                                            item.code,
                                            decl_type,
                                            None,
                                            0,
                                            ret_t_id,
                                            ret_ref_ast_id,
                                            (
                                                (  # If condition: any child m_ast in zone
                                                    (m_ast.ast_id, None),
                                                ),
                                                (  # Then / Else branches
                                                    (
                                                        ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                                                        (None, ("rank",), m_ast.type_id, m_ast.ast_id),
                                                    ),
                                                ),
                                                tuple(zone_link),
                                                1,
                                            ),
                                        ))
                                        ast_id_route = CS.get_route_parse()
                                        typesegment.ref_type = TSRef.Route_Ref
                                        typesegment.ref = ast_id_route
                                else:
                                    with CS(REF_POS):
                                        CS.store(m_ast.view(
                                            ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                                            None,
                                            item.code,
                                            decl_type,
                                            None,
                                            0,
                                            ret_t_id,
                                            ret_ref_ast_id,
                                        ))
                                        ast_id_route = CS.get_route_parse()
                                        typesegment.ref_type = TSRef.Route_Ref
                                        typesegment.ref = ast_id_route
                            else:
                                if zone_link:
                                    # Push the defined struct/union/enum to CS via ref_view
                                    with CS(REF_POS):
                                        CS.store(m_ast.ref_view(
                                            ((m_ast.ast_id,),),
                                            None,
                                            item.code,
                                            decl_type,
                                            (
                                                (  # If condition: any child m_ast in zone
                                                    (m_ast.ast_id, None),
                                                ),
                                                (  # Then / Else branches
                                                    (
                                                        ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                                                        (None, ("rank",), m_ast.type_id, m_ast.ast_id),
                                                    ),
                                                ),
                                                tuple(zone_link),
                                            ),
                                        ))
                                        ast_id_route = CS.get_route_parse()
                                        typesegment.ref_type = TSRef.Route_Ref
                                        typesegment.ref = ast_id_route
                                else:
                                    with CS(REF_POS):
                                        CS.store(m_ast.get_set(
                                            None,
                                            item.code,
                                            decl_type,
                                        ))
                                        ast_id_route = CS.get_route_parse()
                                        typesegment.ref_type = TSRef.Route_Ref
                                        typesegment.ref = ast_id_route

                            # For standalone type definitions (or function declarations),
                            # tag and debug the canonical declaration AST directly here
                            has_var = any(token.type == 0 for ts in final_type for token in ts.content)
                            if not has_var or item.type == ASTT.C_functionproto:
                                with CS(REF_NO_REF):
                                    if G.OVERRIDE_FORCE_AST_DEBUG:
                                        self.ast_debug(CS, ast_id_route)
                                    self.tag(CS, ast_id_route, self.extent)

        # 4. Insert ASTs into ChangeSet
        for final_type in final_types:
            if not final_type:
                continue

            # Skip standalone type definitions and function declarations that were already emitted and tagged in Step 3
            has_var = any(token.type == 0 for ts in final_type for token in ts.content)
            is_type_def = any(ts.is_definition() for ts in final_type)
            if is_type_def and (not has_var or any(tok.type == ASTT.C_functionproto for ts in final_type for tok in ts.content)):
                continue

            cs_inserter = []
            name = ""

            # Fetch / Generate Type Segment ASTs
            for typesegment in final_type:
                typesegment.generate_ast(CS)

            for i, typesegment in enumerate(final_type):
                # Search for identifier token if available
                for item in typesegment.content:
                    if item.type == 0:
                        name = item.code

                t_id = typesegment.type_id
                if t_id is None:
                    if typesegment.ref_type == TSRef.Route_Ref and not typesegment.content:
                        t_id = ASTT.C_Compound
                    elif typesegment.content:
                        t_id = typesegment.content[0].type
                    else:
                        t_id = 0

                if typesegment.ref_type == TSRef.Route_Ref:
                    ref_ast_id = CS.ref(m_ast.ast_id, *typesegment.ref)
                elif typesegment.ref_type == TSRef.AST_Ref:
                    ref_ast_id = typesegment.ref
                else:
                    ref_ast_id = typesegment.ref_ast_id if typesegment.ref_ast_id is not None else 0

                cs_inserter.extend((None, i, t_id, ref_ast_id))

            if not name:
                name = self.name

            if len(final_type) == 1:
                main_t_id = final_type[0].type_id
                if main_t_id is None:
                    if final_type[0].content:
                        main_t_id = final_type[0].content[0].type
                    else:
                        main_t_id = ASTT.C_Compound
            else:
                main_t_id = ASTT.C_Compound

            with CS(REF_POS):
                CS.store(m_ast.view(
                    ((m_ast.ast_id, m_ast_container.ast_id, len(final_type)),),
                    None,
                    name,
                    main_t_id,
                    *cs_inserter,
                ))
                ast_id_route = CS.get_route_parse()

            with CS(REF_NO_REF):
                if G.OVERRIDE_FORCE_AST_DEBUG:
                    self.ast_debug(CS, ast_id_route)

                self.tag(CS, ast_id_route, self.extent)

        CS.debug.append(final_types)
        return

    def swap_out(self):
        """Swap out content to root, allows correct pointer/const values."""
        self.typedata.append(self.content)
        self.content = TypeSegment()
        return

    def keyword_parse(self, token, cursor) -> None:
        """Allow to parse all possible keywords within C Types."""
        tspelling = token.spelling_str
        match tspelling:
            # Storage Class
            case "auto":
                self.storage_class = TypeToken(token, ASTT.C_SCauto)
            case "register":
                self.storage_class = TypeToken(token, ASTT.C_SCregister)
            case "static":
                self.storage_class = TypeToken(token, ASTT.C_SCstatic)
            case "extern":
                self.storage_class = TypeToken(token, ASTT.C_SCextern)
            case "_Thread_local" | "thread_local" | "__thread":
                self.storage_class = TypeToken(token, ASTT.C_SC_Thread_local)
            case "typedef":
                self.storage_class = TypeToken(token, ASTT.C_SCtypedef)
            case "constexpr":
                self.storage_class = TypeToken(token, ASTT.C_SCconstexpr)
                logger.warn("Ast_TYPE>>keyword_parse>>constexpr<<Not Implemented")

            # Qualifiers
            case "const" | "__const" | "__const__":
                if CQual.const in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual.const
                self.content.append_cqal(TypeToken(token, ASTT.C_Qconst))
            case "volatile" | "__volatile" | "__volatile__":
                if CQual.volatile in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual.volatile
                self.content.append_cqal(TypeToken(token, ASTT.C_Qvolatile))
            case "restrict" | "__restrict" | "__restrict__":
                if CQual.restrict in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual.restrict
                self.content.append_cqal(TypeToken(token, ASTT.C_Qrestrict))
            case "_Atomic":
                if CQual._Atomic in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual._Atomic
                self.content.append_cqal(TypeToken(token, ASTT.C__Atomic))

            # Function Specifiers
            case "inline" | "__inline" | "__inline__" | "__always_inline" | "__gnu_inline":
                self.content.append(TypeToken(token, ASTT.C_FSinline))
            case "_Noreturn":
                self.content.append(TypeToken(token, ASTT.C_FS_Noreturn))

            # Alignment & Size Specifiers / Operators
            case "_Alignas" | "alignas":
                self.content.append(TypeToken(token, ASTT.C_AS__Alignas))
            case "sizeof" | "_Alignof" | "alignof" | "__alignof__" | "__alignof":
                logger.debug(f"C_Type>>size/align operator {tspelling}")
                return
            case "typeof" | "__typeof__" | "__typeof":
                logger.debug(f"C_Type>>typeof operator {tspelling}")
                return
            case "__attribute__" | "__attribute" | "__declspec":
                logger.debug(f"C_Type>>attribute {tspelling}")
                return
            case "__extension__":
                return
            case "asm" | "__asm__" | "__asm":
                logger.debug(f"C_Type>>asm keyword {tspelling}")
                return
            case "_Generic":
                return
            case "_Static_assert" | "static_assert":
                logger.debug(f"C_Type>>static assert keyword {tspelling}")
                return

            # Built-in operators and compiler intrinsics
            case (
                "__builtin_types_compatible_p"
                | "__builtin_choose_expr"
                | "__builtin_offsetof"
                | "__builtin_constant_p"
                | "__builtin_expect"
                | "__builtin_unreachable"
                | "__builtin_alloca"
                | "__builtin_prefetch"
                | "__builtin_assume_aligned"
                | "__builtin_convertvector"
                | "__builtin_bit_cast"
                | "__builtin_va_start"
                | "__builtin_va_end"
                | "__builtin_va_arg"
                | "__builtin_va_copy"
            ):
                logger.debug(f"C_Type>>builtin operator {tspelling}")
                return

            # Type specifiers
            case "struct":
                self.content.append(TypeToken(token, ASTT.C_struct))
            case "union":
                self.content.append(TypeToken(token, ASTT.C_union))
            case "enum":
                self.content.append(TypeToken(token, ASTT.C_enum))
            case "void":
                self.content.append(TypeToken(token, ASTT.C_void))

            case "unsigned" | "__unsigned" | "__unsigned__":
                self.content.append(TypeToken(token, ASTT.C_unsigned))
            case "signed" | "__signed" | "__signed__":
                self.content.append(TypeToken(token, ASTT.C_signed))

            case "char":
                self.content.append(TypeToken(token, ASTT.C_char))
                self.swap_out()
            case "short":
                self.content.append(TypeToken(token, ASTT.C_short))
                self.swap_out()
            case "int":
                self.content.append(TypeToken(token, ASTT.C_int))
                self.swap_out()
            case "long":
                self.content.append(TypeToken(token, ASTT.C_long))
                self.swap_out()
            case "Bool" | "_Bool" | "bool":
                self.content.append(TypeToken(token, ASTT.C_bool))
                self.swap_out()
            case "float":
                self.content.append(TypeToken(token, ASTT.C_float))
                self.swap_out()
            case "double":
                self.content.append(TypeToken(token, ASTT.C_double))
                self.swap_out()
            case "_Complex" | "_Imaginary" | "__int128" | "__int128_t" | "__uint128_t" | "__builtin_va_list":
                self.swap_out()

            # Predefined Identifiers in keyword context
            case "__func__" | "__FUNCTION__" | "__PRETTY_FUNCTION__":
                logger.debug(f"C_Type>>predefined identifier {tspelling} skipped")
                return

            # Statement keywords - safely skipped if encountered inside unhandled blocks
            case "if" | "else" | "return" | "switch" | "case" | "default" | "break" | "continue" | "for" | "while" | "do" | "goto":
                logger.debug(f"C_Type>>statement keyword {tspelling} skipped")
                return

            case _:
                logger.warn(f"C_Type>>keyword_parse>>{token.kind}|{tspelling}=> Not implemented")
        return

    def exec_comment(self, token, cursor):
        for zone in self.zones:
            if zone.check_exec(token, cursor, AST_KIND.comment):
                return

        return

    def exec_punctuation(self, token, cursor):
        tspelling = token.spelling_str
        for zone in self.zones:
            if zone.check_exec(token, cursor, AST_KIND.punctuation):
                if tspelling == "}" and zone.zone_type == Zone_Type.Compound_Stmt:
                    if not any(ch.need_processing for ch in zone.children):
                        self.need_processing = False
                return

        match tspelling:
            case "*":
                self.content.append(TypeToken(token, ASTT.C_pointer))
                if self.func_proto:
                    self.content.append(TypeToken(token, ASTT.C_functionproto))
                    return
                self.swap_out()
            case "(":
                if self.func_proto:
                    self.func_proto = False
                elif (cursor.type.get_pointee().kind == cc.TypeKind.FUNCTIONPROTO) and (cursor.kind != cc.CursorKind.FUNCTION_DECL):
                    self.func_proto = True
                    return

                children = tuple(cursor.get_children())
                arg_children = [kids for kids in children if kids.kind == cc.CursorKind.PARM_DECL]
                if arg_children:
                    self.zones.append(Zone(Zone_Type.Function_Args, arg_children))
            case "{":
                kind = cursor.kind
                if kind == cc.CursorKind.COMPOUND_STMT:
                    self.zones.append(Zone(Zone_Type.Compound_Stmt, (cursor,)))
                    return

                children = tuple(cursor.get_children())
                compound_kids = [k for k in children if k.kind == cc.CursorKind.COMPOUND_STMT]
                if compound_kids:
                    self.zones.append(Zone(Zone_Type.Compound_Stmt, compound_kids))
                    return

                if kind == cc.CursorKind.FUNCTION_DECL:
                    self.zones.append(Zone(Zone_Type.Compound_Stmt, (cursor,)))
                    return

                zone_type = Zone_Type.Declared_Args
                if kind == cc.CursorKind.ENUM_DECL:
                    zone_type = Zone_Type.Enum_Content

                cur_file = cursor.extent.start.file.name if cursor.extent.start.file else None
                filtered_children = []
                for kids in children:
                    kid_file = kids.extent.start.file.name if kids.extent.start.file else None
                    if cur_file and kid_file and cur_file != kid_file:
                        continue
                    filtered_children.append(kids)

                if filtered_children:
                    self.zones.append(Zone(zone_type, filtered_children))
                elif zone_type == Zone_Type.Enum_Content:
                    self.zones.append(Zone(zone_type, (cursor,)))
            case "}":
                # When compound statement finishes, close function C_Type processing
                for zone in self.zones:
                    if zone.zone_type == Zone_Type.Compound_Stmt:
                        if not any(ch.need_processing for ch in zone.children):
                            self.need_processing = False
                            return
                    elif zone.zone_type in {Zone_Type.Declared_Args, Zone_Type.Enum_Content}:
                        zone.completed = True
                        zone.preset_extents.clear()
            case "[":
                array_children = tuple(cursor.get_children())
                if array_children:
                    self.content.append(TypeToken(token, ASTT.C_array))
                    self.zones.append(Zone(Zone_Type.Array_Content, array_children))
                else:
                    self.content.append(TypeToken(token, ASTT.C_arrayempty))
                self.swap_out()
            case "=":
                kids = tuple(cursor.get_children())
                if cursor.kind in {cc.CursorKind.ENUM_DECL, cc.CursorKind.ENUM_CONSTANT_DECL}:
                    self.content.append(TypeToken(token, ASTT.C_enumequal))
                    self.swap_out()
                    if kids:
                        self.zones.append(Zone(Zone_Type.Enum_Equal, kids))
                    else:
                        self.zones.append(Zone(Zone_Type.Enum_Equal, (cursor,)))
                else:
                    # Variable / Struct Initializer expression zone
                    if kids:
                        self.zones.append(Zone(Zone_Type.Initializer_Expr, kids))
                    else:
                        self.zones.append(Zone(Zone_Type.Initializer_Expr, (cursor,)))

        return

    def exec_keyword(self, token, cursor):
        for zone in self.zones:
            if zone.check_exec(token, cursor, AST_KIND.keyword):
                return

        self.keyword_parse(token, cursor)
        return

    def exec_identifier(self, token, cursor):
        for zone in self.zones:
            if zone.check_exec(token, cursor, AST_KIND.identifier):
                return

        tspelling = token.spelling_str
        # Ignore predefined identifiers in expression contexts
        if tspelling in {"__func__", "__FUNCTION__", "__PRETTY_FUNCTION__"}:
            return

        if cursor.type.kind == cc.TypeKind.TYPEDEF:
            self.content.append(TypeToken(token, ASTT.C_SCtypedef))
            self.content.get_foreign(cursor)
            self.swap_out()
            return

        if self.content.content:
            if self.content.content[-1].type in {ASTT.C_struct, ASTT.C_union, ASTT.C_enum, ASTT.C_functionproto}:
                self.name = tspelling
                self.content.append(TypeToken(token, self.content.content[-1].type))
                self.content.get_foreign(cursor)
                self.swap_out()
                return

        if cursor.kind == cc.CursorKind.FUNCTION_DECL and tspelling == safe_cursor_spelling(cursor):
            self.name = tspelling
            tt = TypeToken(token, ASTT.C_functionproto)
            tt.is_definition = True
            self.content.append(tt)
            self.content.get_foreign(cursor)
            self.swap_out()
            return

        self.content.append(TypeToken(token))
        self.swap_out()
        return

    def exec_literal(self, token, cursor):
        for zone in self.zones:
            if zone.check_exec(token, cursor, AST_KIND.literal):
                return True

        self.content.append(TypeToken(token, ASTT.Undefined))
        return


