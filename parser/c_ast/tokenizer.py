"""parser/c_ast/tokenizer.py - Fast Ctypes Tokenizer & Line Coordinate Carriers.

Provides slot-optimized Line spatial coordinate carrier, fast-path token extraction,
direct Latin-1 line array slicing, and underlying memory retention via cc.TokenGroup.
"""
from __future__ import annotations

import bisect
import ctypes
from pathlib import Path
from typing import Any
import clang.cindex as cc

from parser.c_ast.ctypes_bindings import (
    AST_KIND,
    _CLANG_GET_CURSOR_EXTENT,
    _CLANG_GET_RANGE_START,
    _CLANG_GET_RANGE_END,
    _CLANG_GET_SPELLING_LOC,
    _CLANG_GET_TOKEN_KIND,
    _CLANG_TOKEN_KIND_MAP,
    _CLANG_GET_EXTENT,
    _BYREF_F_PTR,
    _BYREF_S_LINE,
    _BYREF_S_COL,
    _BYREF_S_OFF,
    _BYREF_E_LINE,
    _BYREF_E_COL,
    _BYREF_E_OFF,
    _CTYPES_S_LINE,
    _CTYPES_E_LINE,
    _CTYPES_S_COL,
    _CTYPES_E_COL,
    _CTYPES_REUSABLE_LOC,
    safe_spelling,
)


class Line:
    """Slot-optimized spatial coordinate carrier across source code files."""

    __slots__ = ("line_pos", "char_pos", "code")

    def __init__(
        self,
        arg0: Any = 0,
        arg1: Any = 0,
        arg2: Any = 0,
        arg3: Any = 0,
    ) -> None:
        self.code: str = ""
        if type(arg0) is int:
            self.line_pos = (arg0, int(arg1))
            self.char_pos = (int(arg2), int(arg3))
            return
        if isinstance(arg0, Line):
            self.line_pos = arg0.line_pos
            self.char_pos = arg0.char_pos
            self.code = arg0.code
            return
        elif isinstance(arg0, cc.SourceRange):
            st = _CLANG_GET_RANGE_START(arg0)
            en = _CLANG_GET_RANGE_END(arg0)
            _CLANG_GET_SPELLING_LOC(st, _BYREF_F_PTR, _BYREF_S_LINE, _BYREF_S_COL, _BYREF_S_OFF)
            _CLANG_GET_SPELLING_LOC(en, _BYREF_F_PTR, _BYREF_E_LINE, _BYREF_E_COL, _BYREF_E_OFF)
            self.line_pos = (_CTYPES_S_LINE.value, _CTYPES_E_LINE.value)
            self.char_pos = (_CTYPES_S_COL.value, _CTYPES_E_COL.value)
            return
        elif hasattr(arg0, "line") and isinstance(getattr(arg0, "line"), Line):
            l = getattr(arg0, "line")
            self.line_pos = l.line_pos
            self.char_pos = l.char_pos
            self.code = l.code
            return

        if isinstance(arg0, (tuple, list)):
            self.line_pos = (int(arg0[0]), int(arg0[1]))
            if isinstance(arg1, (tuple, list)):
                self.char_pos = (int(arg1[0]), int(arg1[1]))
            else:
                self.char_pos = (0, 0)
        else:
            self.line_pos = (int(arg0), int(arg1))
            self.char_pos = (int(arg2), int(arg3))

    def cc(self, rawfile: tuple[str, ...]) -> Line:
        """Extract exact raw source string from line array using 1-based coordinates."""
        if not rawfile:
            self.code = ""
            return self

        s_l, e_l = self.line_pos
        s_c, e_c = self.char_pos

        if s_l <= 0 or e_l <= 0:
            self.code = ""
            return self

        raw_len = len(rawfile)
        if s_l > raw_len:
            self.code = ""
            return self

        if s_l == e_l:
            line_str = rawfile[s_l - 1]
            line_len = len(line_str)
            s_idx = max(0, s_c - 1)
            e_idx = min(line_len, e_c - 1) if e_c > 0 else line_len
            self.code = line_str[s_idx:e_idx]
            return self

        slices = []
        for l_num in range(s_l, min(e_l + 1, raw_len + 1)):
            line_str = rawfile[l_num - 1]
            line_len = len(line_str)
            if l_num == s_l:
                s_idx = max(0, s_c - 1)
                slices.append(line_str[s_idx:])
            elif l_num == e_l:
                e_idx = min(line_len, e_c - 1) if e_c > 0 else line_len
                slices.append(line_str[:e_idx])
            else:
                slices.append(line_str)

        self.code = "\n".join(slices)
        return self

    def new_end(self, target: Any) -> Line:
        """Update ending coordinates to match target range."""
        if isinstance(target, Line):
            self.line_pos = (self.line_pos[0], target.line_pos[1])
            self.char_pos = (self.char_pos[0], target.char_pos[1])
        elif isinstance(target, cc.SourceRange):
            en = _CLANG_GET_RANGE_END(target)
            _CLANG_GET_SPELLING_LOC(en, _BYREF_F_PTR, _BYREF_E_LINE, _BYREF_E_COL, _BYREF_E_OFF)
            self.line_pos = (self.line_pos[0], _CTYPES_E_LINE.value)
            self.char_pos = (self.char_pos[0], _CTYPES_E_COL.value)
        elif hasattr(target, "line") and isinstance(getattr(target, "line"), Line):
            l = getattr(target, "line")
            self.line_pos = (self.line_pos[0], l.line_pos[1])
            self.char_pos = (self.char_pos[0], l.char_pos[1])
        return self

    def new_end_reversed(self, target: Any) -> Line:
        """Update ending coordinates using the START position of target range."""
        if isinstance(target, Line):
            self.line_pos = (self.line_pos[0], target.line_pos[0])
            self.char_pos = (self.char_pos[0], target.char_pos[0])
        elif isinstance(target, cc.SourceRange):
            st = _CLANG_GET_RANGE_START(target)
            _CLANG_GET_SPELLING_LOC(st, _BYREF_F_PTR, _BYREF_S_LINE, _BYREF_S_COL, _BYREF_S_OFF)
            self.line_pos = (self.line_pos[0], _CTYPES_S_LINE.value)
            self.char_pos = (self.char_pos[0], _CTYPES_S_COL.value)
        elif hasattr(target, "line") and isinstance(getattr(target, "line"), Line):
            l = getattr(target, "line")
            self.line_pos = (self.line_pos[0], l.line_pos[0])
            self.char_pos = (self.char_pos[0], l.char_pos[0])
        return self

    def grow(self, target: Any) -> Line:
        """Expand bounding box coordinates to enclose target range."""
        if target is None:
            return self

        if isinstance(target, Line):
            ts_l, te_l = target.line_pos
            ts_c, te_c = target.char_pos
        elif hasattr(target, "line") and isinstance(getattr(target, "line"), Line):
            t = getattr(target, "line")
            ts_l, te_l = t.line_pos
            ts_c, te_c = t.char_pos
        elif isinstance(target, cc.SourceRange):
            t = Line(target)
            ts_l, te_l = t.line_pos
            ts_c, te_c = t.char_pos
        else:
            return self

        if ts_l == 0 and te_l == 0:
            return self

        s_l, e_l = self.line_pos
        if s_l == 0 and e_l == 0:
            self.line_pos = (ts_l, te_l)
            self.char_pos = (ts_c, te_c)
            return self

        s_c, e_c = self.char_pos

        # Short-circuit if already enclosed
        if ts_l > s_l and te_l < e_l:
            return self
        if ts_l == s_l and te_l == e_l and s_c <= ts_c and e_c >= te_c:
            return self

        # Min start
        if ts_l < s_l:
            new_sl, new_sc = ts_l, ts_c
        elif ts_l == s_l:
            new_sl = s_l
            new_sc = min(s_c, ts_c) if s_c > 0 and ts_c > 0 else max(s_c, ts_c)
        else:
            new_sl, new_sc = s_l, s_c

        # Max end
        if te_l > e_l:
            new_el, new_ec = te_l, te_c
        elif te_l == e_l:
            new_el = e_l
            new_ec = max(e_c, te_c)
        else:
            new_el, new_ec = e_l, e_c

        if new_sl != s_l or new_el != e_l:
            self.line_pos = (new_sl, new_el)
        if new_sc != s_c or new_ec != e_c:
            self.char_pos = (new_sc, new_ec)
        return self

    def is_inside(self, extent: Line) -> bool:
        """Check if target extent is completely enclosed within self boundaries."""
        if self.line_pos[0] < extent.line_pos[0] and self.line_pos[1] > extent.line_pos[1]:
            return True
        if self.line_pos[0] == extent.line_pos[0] and self.line_pos[1] > extent.line_pos[1]:
            return self.char_pos[0] <= extent.char_pos[0]
        if self.line_pos[0] < extent.line_pos[0] and self.line_pos[1] == extent.line_pos[1]:
            return self.char_pos[1] >= extent.char_pos[1]
        if self.line_pos[0] == extent.line_pos[0] and self.line_pos[1] == extent.line_pos[1]:
            return self.char_pos[0] <= extent.char_pos[0] and self.char_pos[1] >= extent.char_pos[1]
        return False

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Line):
            return self.line_pos == other.line_pos and self.char_pos == other.char_pos
        return False

    def __repr__(self) -> str:
        return f"Line({self.line_pos[0]}:{self.char_pos[0]}..{self.line_pos[1]}:{self.char_pos[1]})"


def get_cursor_line(cursor: cc.Cursor) -> Line:
    """Fast-path ctypes Line extraction from Clang Cursor with in-place caching."""
    cl = getattr(cursor, "_cached_line", None)
    if cl is not None:
        return cl
    ext = _CLANG_GET_CURSOR_EXTENT(cursor)
    st = _CLANG_GET_RANGE_START(ext)
    en = _CLANG_GET_RANGE_END(ext)
    _CLANG_GET_SPELLING_LOC(st, None, _BYREF_S_LINE, _BYREF_S_COL, None)
    _CLANG_GET_SPELLING_LOC(en, None, _BYREF_E_LINE, _BYREF_E_COL, None)
    cl = Line.__new__(Line)
    cl.code = ""
    cl.line_pos = (_CTYPES_S_LINE.value, _CTYPES_E_LINE.value)
    cl.char_pos = (_CTYPES_S_COL.value, _CTYPES_E_COL.value)
    cursor._cached_line = cl
    return cl


def get_tokens_in_extent(tokens: list[Any], extent: Line) -> list[Any]:
    """Retrieve all tokens from tokens_array whose extent falls within or overlaps extent."""
    s_l, e_l = extent.line_pos
    s_c, e_c = extent.char_pos
    if not tokens:
        return []

    # Binary search for start index
    low = 0
    high = len(tokens)
    target = (s_l, s_c)
    while low < high:
        mid = (low + high) // 2
        tok_line = tokens[mid].line
        mid_pos = (tok_line.line_pos[0], tok_line.char_pos[0])
        if mid_pos < target:
            low = mid + 1
        else:
            high = mid

    start_idx = max(0, low - 1)
    res = []
    for idx in range(start_idx, len(tokens)):
        tok = tokens[idx]
        tl = tok.line
        t_sl, t_el = tl.line_pos
        t_sc, t_ec = tl.char_pos
        if t_sl > e_l or (t_sl == e_l and t_sc >= e_c):
            break
        if (t_sl > s_l or (t_sl == s_l and t_sc >= s_c)) and (t_el < e_l or (t_el == e_l and t_ec <= e_c)):
            res.append(tok)
    return res


def _get_tok_start_line(t: Any) -> int:
    return t.line.line_pos[0]


def encapsulate_trailing_delimiter(
    tokens: list[Any], extent: Line, delimiters: tuple[str, ...] = (";", ",")
) -> Line:
    """Expand extent to encapsulate trailing punctuation delimiter on the same line or immediate next line."""
    if not tokens:
        return extent
    e_l, e_c = extent.line_pos[1], extent.char_pos[1]
    idx = bisect.bisect_left(tokens, e_l, key=_get_tok_start_line)
    n = len(tokens)
    for i in range(idx, n):
        tok = tokens[i]
        tl = tok.line
        if tl.line_pos[0] == e_l:
            if tl.char_pos[0] >= e_c:
                if tok.spelling_str in delimiters:
                    extent.grow(tl)
                break
        elif tl.line_pos[0] > e_l:
            if tl.line_pos[0] == e_l + 1 and tl.char_pos[0] <= 2 and tok.spelling_str in delimiters:
                extent.grow(tl)
            break
    return extent


class ParsedToken:
    """Slot-optimized token carrier eliminating ctypes dynamic dict allocations."""

    __slots__ = ("line", "spelling_str", "ast_kind", "_cursor")

    def __init__(
        self,
        line: Line,
        spelling_str: str,
        ast_kind: int,
        cursor: Any,
    ) -> None:
        self.line = line
        self.spelling_str = spelling_str
        self.ast_kind = ast_kind
        self._cursor = cursor

    @property
    def spelling(self) -> str:
        return self.spelling_str

    @property
    def _tu(self) -> Any:
        return getattr(self._cursor, "_tu", None)


class TokenStream:
    """High-speed token and cursor streaming engine backed by direct ctypes calls."""

    def __init__(
        self,
        parsed_tu: cc.TranslationUnit,
        fullfilename: str,
        rawfile: tuple[str, ...] | None = None,
        file_size: int | None = None,
    ) -> None:
        self.parsed_tu = parsed_tu
        self.fullfilename = fullfilename
        parsed_file = cc.File.from_name(parsed_tu, fullfilename)
        start_loc = cc.SourceLocation.from_position(parsed_tu, parsed_file, 1, 1)

        if file_size is None:
            file_size = Path(fullfilename).stat().st_size
        end_loc = cc.conf.lib.clang_getLocationForOffset(parsed_tu, parsed_file, file_size)

        extent = cc.SourceRange.from_locations(start_loc, end_loc)

        tokens_memory = ctypes.POINTER(cc.Token)()
        tokens_count = ctypes.c_uint()

        cc.conf.lib.clang_tokenize(
            parsed_tu,
            extent,
            ctypes.byref(tokens_memory),
            ctypes.byref(tokens_count),
        )

        self.count = int(tokens_count.value)
        self.tokens_array: list[ParsedToken] = []

        if self.count < 1:
            return

        # Pre-allocate contiguous Cursor C-array and annotate tokens in single foreign call
        temp_cursors_array = (cc.Cursor * self.count)()
        cc.conf.lib.clang_annotateTokens(
            parsed_tu,
            tokens_memory.contents,
            self.count,
            temp_cursors_array,
        )

        # Set class-level _tu so cursors don't require per-instance dynamic dict allocation
        cc.Cursor._tu = parsed_tu

        temp_tokens_array = ctypes.cast(
            tokens_memory,
            ctypes.POINTER(cc.Token * self.count),
        ).contents

        tokens_array: list[ParsedToken] = []
        tokens_append = tokens_array.append
        rawfile_len = len(rawfile) if rawfile else 0

        get_extent = _CLANG_GET_EXTENT
        get_spelling_loc = _CLANG_GET_SPELLING_LOC
        token_kind_map = _CLANG_TOKEN_KIND_MAP
        byref_s_l = _BYREF_S_LINE
        byref_s_c = _BYREF_S_COL
        byref_e_l = _BYREF_E_LINE
        byref_e_c = _BYREF_E_COL
        s_l_val = _CTYPES_S_LINE
        e_l_val = _CTYPES_E_LINE
        s_c_val = _CTYPES_S_COL
        e_c_val = _CTYPES_E_COL

        loc = _CTYPES_REUSABLE_LOC
        loc_ptr = loc.ptr_data
        parsed_tu_ptr = parsed_tu._as_parameter_

        prev_s_l = -1
        prev_e_l = -1
        prev_line_pos = (0, 0)
        prev_cursor = None

        for i, token in enumerate(temp_tokens_array):
            raw_c = temp_cursors_array[i]
            if (
                prev_cursor is not None
                and raw_c._kind_id == prev_cursor._kind_id
                and raw_c.xdata == prev_cursor.xdata
                and raw_c.data[0] == prev_cursor.data[0]
                and raw_c.data[1] == prev_cursor.data[1]
                and raw_c.data[2] == prev_cursor.data[2]
            ):
                cursor = prev_cursor
            else:
                cursor = raw_c
                prev_cursor = cursor

            # Direct SourceLocation configuration (bypasses 2 C-calls + 2 struct allocs + from_param)
            ext = get_extent(parsed_tu_ptr, token)
            p0, p1 = ext.ptr_data[0], ext.ptr_data[1]
            loc_ptr[0] = p0
            loc_ptr[1] = p1
            loc.int_data = ext.begin_int_data
            get_spelling_loc(loc, None, byref_s_l, byref_s_c, None)

            loc.int_data = ext.end_int_data
            get_spelling_loc(loc, None, byref_e_l, byref_e_c, None)

            s_line = s_l_val.value
            e_line = e_l_val.value
            s_col = s_c_val.value
            e_col = e_c_val.value

            if s_line == prev_s_l and e_line == prev_e_l:
                line_pos = prev_line_pos
            else:
                line_pos = (s_line, e_line)
                prev_s_l = s_line
                prev_e_l = e_line
                prev_line_pos = line_pos

            l = Line.__new__(Line)
            l.code = ""
            l.line_pos = line_pos
            l.char_pos = (s_col, e_col)

            if s_line == e_line and 0 < s_line <= rawfile_len:
                spelling_str = rawfile[s_line - 1][s_col - 1 : e_col - 1]
            else:
                try:
                    token._tu = parsed_tu
                    spelling_str = token.spelling or ""
                except (UnicodeDecodeError, Exception):
                    spelling_str = ""

            ast_kind = token_kind_map[token.int_data[0]]
            tokens_append(ParsedToken(l, spelling_str, ast_kind, cursor))

        self.tokens_array = tokens_array
        # Retain token memory pointer to prevent premature garbage collection
        self.token_group = cc.TokenGroup(parsed_tu, tokens_memory, tokens_count)

    @property
    def cursors_array(self) -> list[Any]:
        """Backward-compatibility property returning cursors matching tokens_array."""
        return [t._cursor for t in self.tokens_array]
