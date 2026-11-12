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


def _restore_null_cursor_semantics() -> int:
    """Remove the `cursor_null_guard` wrapper that clang >= 22 bindings put on `cc.Cursor` methods.

    The guard raises "Tried calling method on a null-cursor." on any guarded method of the null cursor, and
    costs an extra `clang_equalCursors` call on every guarded call. The parser reads the null cursor's kind
    (`INVALID_FILE`) and children on purpose, which libclang supports, as the bindings up to clang 21 did.
    Returns the number of methods and properties restored (0 for bindings without the guard).
    """
    guard = getattr(cc, "cursor_null_guard", None)
    if guard is None:
        return 0
    guard_code = guard(lambda self: None).__code__

    def _unwrap(fn: Any) -> Any:
        code = getattr(fn, "__code__", None)
        if code is not guard_code or not fn.__closure__ or "func" not in code.co_freevars:
            return None
        return fn.__closure__[code.co_freevars.index("func")].cell_contents

    restored = 0
    for name, attr in list(vars(cc.Cursor).items()):
        if isinstance(attr, property):
            original = _unwrap(attr.fget)
            if original is not None:
                setattr(cc.Cursor, name, property(original, attr.fset, attr.fdel, attr.__doc__))
                restored += 1
        else:
            original = _unwrap(attr)
            if original is not None:
                setattr(cc.Cursor, name, original)
                restored += 1
    return restored


# Applied on import, before any cursor is created (forked parsing workers inherit the patched class)
NULL_GUARDS_REMOVED = _restore_null_cursor_semantics()

_SELF_TEST_SOURCE = (
    "struct s { int a; };\n"
    "typedef struct s s_t;\n"
    "static int f(s_t *p) { return p->a + sizeof(int); }\n"
)


def libclang_version() -> str:
    """The loaded libclang's version string, e.g. "clang version 22.1.0" ("unknown" if it cannot be read)."""
    try:
        fn = cc.conf.lib.clang_getClangVersion
        fn.argtypes = []
        fn.restype = cc._CXString
        return str(cc._CXString.from_result(fn()))
    except Exception:  # noqa: BLE001
        return "unknown"


def libclang_self_test() -> str:
    """Check that libclang and its Python bindings behave the way the C parser relies on.

    Parses a small in-memory translation unit, walks every cursor reading the attributes the parser reads,
    and uses the null cursor the way the parser does. Returns a one-line description of the bindings.

    Raises:
        RuntimeError: naming the bindings file, the libclang version and the failing operation.
    """
    version = libclang_version()
    where = f"bindings {cc.__file__}, {version}"
    try:
        null = cc.conf.lib.clang_getNullCursor()
        if null.kind != cc.CursorKind.INVALID_FILE:
            raise RuntimeError(f"the null cursor reports kind {null.kind}, not INVALID_FILE")
        list(null.get_children())
        tu = cc.Index.create().parse(
            "kip_self_test.c", args=["-x", "c"], unsaved_files=[("kip_self_test.c", _SELF_TEST_SOURCE)]
        )
        walked = 0
        for cursor in tu.cursor.walk_preorder():
            _ = (cursor.kind, cursor.spelling, cursor.extent, cursor.location, cursor.type.kind)
            ref = cursor.referenced
            if ref is not None:
                _ = (ref.kind, ref.spelling, ref.location)
            walked += 1
        if walked < 5:
            raise RuntimeError(f"the self-test translation unit produced only {walked} cursor(s)")
    except Exception as e:
        raise RuntimeError(f"libclang self-test failed ({where}): {type(e).__name__}: {e}") from e
    guards = f"; removed the null-cursor guard from {NULL_GUARDS_REMOVED} Cursor methods" if NULL_GUARDS_REMOVED else ""
    return f"{where}{guards}"


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

_CLANG_GET_CURSOR_LOC = cc.conf.lib.clang_getCursorLocation
_CLANG_GET_CURSOR_LOC.argtypes = [cc.Cursor]
_CLANG_GET_CURSOR_LOC.restype = cc.SourceLocation

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

_CLANG_GET_FILE_LOC = cc.conf.lib.clang_getFileLocation
_CLANG_GET_FILE_LOC.argtypes = [
    cc.SourceLocation,
    ctypes.c_void_p,
    ctypes.POINTER(cc.c_uint),
    ctypes.POINTER(cc.c_uint),
    ctypes.c_void_p,
]
_CLANG_GET_FILE_LOC.restype = None

_CLANG_GET_TOKEN_KIND = cc.conf.lib.clang_getTokenKind
_CLANG_GET_TOKEN_KIND.argtypes = [cc.Token]
_CLANG_GET_TOKEN_KIND.restype = ctypes.c_uint

_CLANG_CURSOR_IS_NULL = cc.conf.lib.clang_Cursor_isNull
_CLANG_CURSOR_IS_NULL.argtypes = [cc.Cursor]
_CLANG_CURSOR_IS_NULL.restype = ctypes.c_int

_CLANG_GET_CURSOR_REFERENCED = cc.conf.lib.clang_getCursorReferenced
_CLANG_GET_CURSOR_REFERENCED.argtypes = [cc.Cursor]
_CLANG_GET_CURSOR_REFERENCED.restype = cc.Cursor

_CLANG_VISIT_CHILDREN = cc.conf.lib.clang_visitChildren
_CLANG_VISIT_CHILDREN.argtypes = [cc.Cursor, cc.cursor_visit_callback, ctypes.py_object]
_CLANG_VISIT_CHILDREN.restype = ctypes.c_uint

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
