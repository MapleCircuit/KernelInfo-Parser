"""parser/c_ast/ctypes_bindings.py - Low-Level Libclang Ctypes FFI Bindings.

Bypasses Python-C API boundary overhead in clang.cindex with pre-allocated foreign
buffers, direct ctypes foreign function pointers, and fast-path coordinate extraction.
"""
from __future__ import annotations

import ctypes
from enum import IntEnum
from typing import Any
import clang.cindex as cc

from core.globalstuff import clean_unnamed_spelling


class AST_KIND(IntEnum):
    punctuation = 0
    keyword = 1
    identifier = 2
    literal = 3
    comment = 4


# Direct ctypes foreign function bindings
_CLANG_GET_EXTENT = cc.conf.lib.clang_getTokenExtent
_CLANG_GET_EXTENT.argtypes = [cc.c_object_p, cc.Token]
_CLANG_GET_EXTENT.restype = cc.SourceRange

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
_CLANG_GET_SPELLING_LOC.argtypes = [
    cc.SourceLocation,
    ctypes.c_void_p,
    ctypes.POINTER(cc.c_uint),
    ctypes.POINTER(cc.c_uint),
    ctypes.c_void_p,
]
_CLANG_GET_SPELLING_LOC.restype = None

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

# Pre-allocated reusable ctypes buffers for zero-allocation 2 C-call coordinate retrieval
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
_CTYPES_BYREF = ctypes.byref
_CTYPES_REUSABLE_LOC = cc.SourceLocation()


def safe_spelling(token: Any) -> str:
    """Safely return token spelling, checking cached spelling_str and caching in-place."""
    if (cached := getattr(token, "spelling_str", None)) is not None:
        return cached
    try:
        spelling = token.spelling or ""
    except (UnicodeDecodeError, Exception):
        spelling = ""
    try:
        token.spelling_str = spelling
    except Exception:
        pass
    return spelling


def safe_cursor_spelling(cursor: Any) -> str:
    """Safely return cursor.spelling, catching UnicodeDecodeError, normalizing unnamed paths, and caching in-place."""
    if (cached := getattr(cursor, "_spelling_str", None)) is not None:
        return cached
    try:
        spelling = cursor.spelling or ""
    except (UnicodeDecodeError, Exception):
        spelling = ""
    if "(unnamed at " in spelling or "(anonymous at " in spelling:
        spelling = clean_unnamed_spelling(spelling)
    try:
        cursor._spelling_str = spelling
    except Exception:
        pass
    return spelling
