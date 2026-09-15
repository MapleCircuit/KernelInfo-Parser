"""parser/c_ast/cursor_tree.py - Semantic Cursor Tree Partitioner & AST Resolver.

Provides:
- SemanticPartitioner: High-fidelity partitioning of tokens and cursors into AST nodes.
- Zone & Zone_Type: Scope boundary management (functions, structs, enums, statements).
- C_Type: Semantic parsing and extraction of C types, specifiers, qualifiers, and declarations.
- resolve_cursor_type_ast: Relational symbol resolution from Clang cursors.
- resolve_cppro_scopes: Preprocessor conditional branch coordinate linking.
"""
from __future__ import annotations
import re
import logging
from collections import deque
from pathlib import Path
from typing import Any
import clang.cindex as cc

from core.globalstuff import (
    G,
    ASTT,
    REF_POS,
    REF_MULTI,
    REF_NO_REF,
    REF_ROOT,
    REF_FILE,
    SymbolRole,
    OP_REF,
    STANDARD_C_KEYWORDS,
)
from core.DBLayout import (
    m_file,
    m_ast,
    m_ast_container,
    m_ast_include,
    m_file_name,
    m_symbol_def,
    m_symbol_ref,
)
from parser.c_ast.ctypes_bindings import (
    AST_KIND,
    safe_spelling,
    safe_cursor_spelling,
)
from parser.c_ast.tokenizer import (
    Line,
    get_cursor_line,
    encapsulate_trailing_delimiter,
)
from parser.c_ast.ast_nodes import (
    Ast,
    End_Mode,
    Zone_Type,
    CQual,
    TypeToken,
    TypeSegment,
    TSRef,
    get_notbind_type,
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
    Ast_CallExpr,
    Ast_MemberRefExpr,
    Ast_DeclRefExpr,
)

logger = logging.getLogger(__name__)

_DEF_TYPES = frozenset({ASTT.C_struct, ASTT.C_functionproto, ASTT.C_union, ASTT.C_enum})
_PUNCT_IGNORED = frozenset({",", ";"})
_BRACE_ZONE_TYPES = frozenset({Zone_Type.Compound_Stmt, Zone_Type.Declared_Args, Zone_Type.Enum_Content})
_VALID_TOP_KINDS = frozenset({
    cc.CursorKind.STRUCT_DECL,
    cc.CursorKind.UNION_DECL,
    cc.CursorKind.ENUM_DECL,
    cc.CursorKind.FUNCTION_DECL,
    cc.CursorKind.CXX_METHOD,
    cc.CursorKind.VAR_DECL,
    cc.CursorKind.TYPEDEF_DECL,
})
_NON_NAME_TOKENS = frozenset({"struct", "union", "enum", "(", "*", ""})
_SKIP_REF_KINDS = frozenset({
    cc.CursorKind.MACRO_INSTANTIATION,
    cc.CursorKind.INVALID_FILE,
    cc.CursorKind.PREPROCESSING_DIRECTIVE,
    cc.CursorKind.MACRO_DEFINITION,
    cc.CursorKind.INCLUSION_DIRECTIVE,
})




def filter_enclosing_cursors(cursors: list[cc.Cursor]) -> list[cc.Cursor]:
    """Filter out cursors whose source extents are strictly contained within another cursor."""
    if len(cursors) <= 1:
        return cursors
    items = []
    for idx, c in enumerate(cursors):
        ext = get_cursor_line(c)
        items.append((ext.line_pos[0], ext.char_pos[0], ext.line_pos[1], ext.char_pos[1], idx, c))

    # Sort primarily by start pos ascending, then by end pos descending
    items.sort(key=lambda x: (x[0], x[1], -x[2], -x[3]))

    filtered = []
    active_enclosing = []
    for s_l, s_c, e_l, e_c, idx, c in items:
        # Prune candidates that end before current start
        active_enclosing = [
            (ae_l, ae_c, as_l, as_c) for ae_l, ae_c, as_l, as_c in active_enclosing
            if ae_l > s_l or (ae_l == s_l and ae_c > s_c)
        ]
        is_enclosed = False
        for ae_l, ae_c, as_l, as_c in active_enclosing:
            if (as_l < s_l or (as_l == s_l and as_c < s_c)) and (ae_l > e_l or (ae_l == e_l and ae_c > e_c)):
                is_enclosed = True
                break
        if not is_enclosed:
            filtered.append((idx, c))
            active_enclosing.append((e_l, e_c, s_l, s_c))

    filtered.sort(key=lambda x: x[0])
    return [c for _, c in filtered]


def get_top_level_cursors(parsed_tu: cc.TranslationUnit | None, fullfilename: str) -> list[cc.Cursor]:
    """Filter translation unit top-level declarations belonging to target source file."""
    if parsed_tu is None or not fullfilename:
        return []
    resolved_full = Path(fullfilename).resolve()
    resolved_cache: dict[str, bool] = {fullfilename: True}
    top_cursors: list[cc.Cursor] = []
    top_append = top_cursors.append
    for c in parsed_tu.cursor.get_children():
        if c.kind in _VALID_TOP_KINDS:
            loc = c.location
            ext_st = getattr(c.extent, "start", None)
            f = getattr(loc, "file", None) or getattr(ext_st, "file", None)
            if f:
                fname = f.name
                if fname == fullfilename:
                    top_append(c)
                else:
                    is_match = resolved_cache.get(fname)
                    if is_match is None:
                        try:
                            is_match = (Path(fname).resolve() == resolved_full)
                        except Exception:
                            is_match = False
                        resolved_cache[fname] = is_match
                    if is_match:
                        top_append(c)
    return filter_enclosing_cursors(top_cursors)



def get_decl_type(ast_type: int) -> int:
    """Map base AST type to declaration type (e.g. C_struct -> C_structdecl)."""
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

def collect_cursor_used_types(cursor: Any, used_types: set[str] | None = None) -> set[str]:
    """Recursively collect distinct type names used within a Clang cursor subtree."""
    if used_types is None:
        used_types = set()
    if cursor is None:
        return used_types

    def _clean_type_name(t_name: str) -> str:
        if not t_name:
            return ""
        clean = t_name.strip()
        for prefix in ("const ", "volatile ", "restrict ", "struct ", "union ", "enum "):
            if clean.startswith(prefix):
                clean = clean[len(prefix):].strip()
        clean = clean.split()[0].rstrip("*[]() ")
        return clean

    try:
        k = getattr(cursor, "kind", None)
        if k == cc.CursorKind.TYPE_REF:
            sp = getattr(cursor, "spelling", "")
            if sp:
                clean = _clean_type_name(sp)
                if clean:
                    used_types.add(clean)
        elif k in (cc.CursorKind.VAR_DECL, cc.CursorKind.PARM_DECL):
            t = getattr(cursor, "type", None)
            if t is not None:
                sp = getattr(t, "spelling", "")
                clean = _clean_type_name(sp)
                if clean:
                    used_types.add(clean)
                can = getattr(t, "get_canonical", None)
                if callable(can):
                    can_t = can()
                    can_sp = getattr(can_t, "spelling", "")
                    can_clean = _clean_type_name(can_sp)
                    if can_clean:
                        used_types.add(can_clean)
                pt = getattr(t, "get_pointee", None)
                if callable(pt):
                    p = pt()
                    p_sp = getattr(p, "spelling", "")
                    p_clean = _clean_type_name(p_sp)
                    if p_clean:
                        used_types.add(p_clean)
        elif k == cc.CursorKind.MEMBER_REF_EXPR:
            for child in cursor.get_children():
                ct = getattr(child, "type", None)
                if ct is not None:
                    sp = getattr(ct, "spelling", "")
                    clean = _clean_type_name(sp)
                    if clean:
                        used_types.add(clean)
                    can = getattr(ct, "get_canonical", None)
                    if callable(can):
                        can_t = can()
                        can_sp = getattr(can_t, "spelling", "")
                        can_clean = _clean_type_name(can_sp)
                        if can_clean:
                            used_types.add(can_clean)
        elif k == cc.CursorKind.CALL_EXPR:
            ref = getattr(cursor, "referenced", None)
            if ref is not None:
                rt = getattr(ref, "result_type", None)
                if rt is not None:
                    sp = getattr(rt, "spelling", "")
                    clean = _clean_type_name(sp)
                    if clean:
                        used_types.add(clean)

        for child in cursor.get_children():
            collect_cursor_used_types(child, used_types)
    except Exception:
        pass

    return used_types


def get_rel_file_for_cursor(CS: Any, cursor: Any) -> str | None:
    """Extract relative repository file path for a Clang cursor, ignoring host system headers."""
    if cursor is None:
        return None
    try:
        loc = getattr(cursor, "location", None)
        if loc is None or loc.file is None:
            return None
        file_path = loc.file.name
        if not file_path:
            return None
    except Exception:
        return None

    # Exclude host system headers (server include directories)
    if file_path.startswith(("/usr/", "/etc/", "/opt/", "/var/", "/lib/", "/bin/", "/dev/")):
        # Exception: /dev/shm workspace used for repository checkouts
        if not file_path.startswith("/dev/shm/"):
            return None

    # Resolve via MasterFile temp dir
    mf = (getattr(CS, "mf", None) if CS is not None else None) or getattr(G, "MF", None)
    gp = (getattr(CS, "gp", None) if CS is not None else None) or getattr(G, "GP", None)
    v_name = getattr(gp, "Version_Name", None) if gp is not None else getattr(G, "VERSION_NAME", "v3.0")
    if mf is not None and hasattr(mf, "version_dict") and v_name in mf.version_dict:
        mfdir = mf.version_dict.get(v_name, "")
        if mfdir and file_path.startswith(mfdir):
            rel = file_path[len(mfdir):].lstrip("/")
            if rel.startswith(("usr/", "etc/", "opt/", "var/", "lib/")):
                return None
            return rel if rel else None

    cur_p = getattr(G, "CURRENT_PARSING_FILE", None) or (getattr(CS, "current_path", None) if CS is not None else None)
    if cur_p and (file_path == cur_p or file_path.endswith("/" + cur_p)):
        return cur_p

    # If within a local git repository path containing /linux/
    if "/linux/" in file_path and not file_path.startswith(("/usr/", "/etc/", "/opt/", "/var/", "/lib/")):
        rel = file_path.split("/linux/", 1)[1]
        if rel and not rel.startswith(("usr/", "etc/", "opt/", "var/", "lib/")):
            return rel

    # Reject any absolute path outside the repository workspace
    if os.path.isabs(file_path):
        return None

    # Reject relative paths that refer to system-like directories
    if file_path.startswith(("usr/", "etc/", "opt/", "var/", "lib/")):
        return None

    return file_path.lstrip("/")


def resolve_cursor_type_ast(CS: Any, cursor: Any) -> tuple[int, Any]:
    """Resolve a Clang cursor's referenced symbol/type to an AST type ID and relational reference."""
    if cursor is None:
        return (ASTT.Undefined, 0)

    try:
        ref_cursor = getattr(cursor, "referenced", None) or cursor.get_definition()
    except Exception:
        ref_cursor = None

    if ref_cursor is None:
        ref_cursor = cursor

    cur_file = getattr(CS, "current_path", "") or getattr(G, "CURRENT_PARSING_FILE", "")

    k = getattr(ref_cursor, "kind", None)
    if k in (cc.CursorKind.FUNCTION_DECL, cc.CursorKind.CXX_METHOD):
        spelling = safe_cursor_spelling(ref_cursor)
        if spelling:
            safe_name = str(spelling)[:255]
            rel_file = get_rel_file_for_cursor(CS, ref_cursor)
            if rel_file and rel_file != cur_file:
                return (ASTT.C_functionproto, CS.ref(m_ast.ast_id, REF_FILE, rel_file, safe_name, int(ASTT.C_functionproto)))
            if hasattr(CS, "symbol_dict"):
                if (safe_name, ASTT.C_functionproto) in CS.symbol_dict:
                    return (ASTT.C_functionproto, CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(safe_name, ASTT.C_functionproto)]))
                if (safe_name, ASTT.C_functionprotodecl) in CS.symbol_dict:
                    return (ASTT.C_functionproto, CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(safe_name, ASTT.C_functionprotodecl)]))
            op_idx = len(CS.cs)
            with CS(REF_NO_REF):
                CS.store(m_ast.view(((m_ast.ast_id,),), None, safe_name, ASTT.C_functionprotnotbind))
            return (ASTT.C_functionproto, CS.ref(m_ast.ast_id, REF_POS, op_idx))

    if k == cc.CursorKind.FIELD_DECL:
        spelling = safe_cursor_spelling(ref_cursor)
        if spelling:
            safe_name = str(spelling)[:255]
            rel_file = get_rel_file_for_cursor(CS, ref_cursor)
            if rel_file and rel_file != cur_file:
                return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_FILE, rel_file, safe_name, int(ASTT.C_structdecl)))
            if hasattr(CS, "symbol_dict"):
                if (safe_name, ASTT.C_structdecl) in CS.symbol_dict:
                    return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(safe_name, ASTT.C_structdecl)]))
                if (safe_name, ASTT.C_struct) in CS.symbol_dict:
                    return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(safe_name, ASTT.C_struct)]))
            op_idx = len(CS.cs)
            with CS(REF_NO_REF):
                CS.store(m_ast.view(((m_ast.ast_id,),), None, safe_name, ASTT.C_structnotbind))
            return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_POS, op_idx))

    if k in (cc.CursorKind.VAR_DECL, cc.CursorKind.PARM_DECL):
        type_obj = getattr(ref_cursor, "type", None)
        t_spelling = safe_cursor_spelling(ref_cursor)
        if type_obj is not None:
            t_kind = getattr(type_obj, "kind", None)
            if t_kind == cc.TypeKind.INT:
                return (ASTT.C_int, 0)
            elif t_kind in (cc.TypeKind.CHAR_S, cc.TypeKind.CHAR_U):
                return (ASTT.C_char, 0)
            elif t_kind in (cc.TypeKind.LONG, cc.TypeKind.LONGLONG):
                return (ASTT.C_long, 0)
            elif t_kind == cc.TypeKind.SHORT:
                return (ASTT.C_short, 0)
            elif t_kind == cc.TypeKind.FLOAT:
                return (ASTT.C_float, 0)
            elif t_kind == cc.TypeKind.DOUBLE:
                return (ASTT.C_double, 0)
            elif t_kind == cc.TypeKind.BOOL:
                return (ASTT.C_bool, 0)
            elif t_kind == cc.TypeKind.VOID:
                return (ASTT.C_void, 0)
            elif t_kind == cc.TypeKind.POINTER:
                return (ASTT.C_pointer, 0)
            elif t_kind == cc.TypeKind.RECORD:
                try:
                    decl_cursor = type_obj.get_declaration()
                    tag_name = safe_cursor_spelling(decl_cursor) or t_spelling
                except Exception:
                    decl_cursor = None
                    tag_name = t_spelling
                if tag_name:
                    safe_name = str(tag_name)[:255]
                    rel_file = get_rel_file_for_cursor(CS, decl_cursor)
                    if rel_file and rel_file != cur_file:
                        return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_FILE, rel_file, safe_name, int(ASTT.C_structdecl)))
                    if hasattr(CS, "symbol_dict"):
                        if (safe_name, ASTT.C_structdecl) in CS.symbol_dict:
                            return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(safe_name, ASTT.C_structdecl)]))
                        if (safe_name, ASTT.C_struct) in CS.symbol_dict:
                            return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(safe_name, ASTT.C_struct)]))
                    op_idx = len(CS.cs)
                    with CS(REF_NO_REF):
                        CS.store(m_ast.view(((m_ast.ast_id,),), None, safe_name, ASTT.C_structnotbind))
                    return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_POS, op_idx))
        return (ASTT.C_DeclRefExpr, 0)

    type_obj = getattr(ref_cursor, "type", None)
    if type_obj is not None:
        t_kind = getattr(type_obj, "kind", None)
        t_spelling = safe_cursor_spelling(ref_cursor)
        if t_kind == cc.TypeKind.RECORD:
            try:
                decl_cursor = type_obj.get_declaration()
                tag_name = safe_cursor_spelling(decl_cursor) or t_spelling
            except Exception:
                decl_cursor = None
                tag_name = t_spelling
            if tag_name:
                safe_name = str(tag_name)[:255]
                rel_file = get_rel_file_for_cursor(CS, decl_cursor)
                if rel_file and rel_file != cur_file:
                    return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_FILE, rel_file, safe_name, int(ASTT.C_structdecl)))
                if hasattr(CS, "symbol_dict"):
                    if (safe_name, ASTT.C_structdecl) in CS.symbol_dict:
                        return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(safe_name, ASTT.C_structdecl)]))
                    if (safe_name, ASTT.C_struct) in CS.symbol_dict:
                        return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(safe_name, ASTT.C_struct)]))
                op_idx = len(CS.cs)
                with CS(REF_NO_REF):
                    CS.store(m_ast.view(((m_ast.ast_id,),), None, safe_name, ASTT.C_structnotbind))
                return (ASTT.C_struct, CS.ref(m_ast.ast_id, REF_POS, op_idx))
        elif t_kind == cc.TypeKind.ENUM:
            try:
                decl_cursor = type_obj.get_declaration()
                tag_name = safe_cursor_spelling(decl_cursor) or t_spelling
            except Exception:
                decl_cursor = None
                tag_name = t_spelling
            if tag_name:
                safe_name = str(tag_name)[:255]
                rel_file = get_rel_file_for_cursor(CS, decl_cursor)
                if rel_file and rel_file != cur_file:
                    return (ASTT.C_enum, CS.ref(m_ast.ast_id, REF_FILE, rel_file, safe_name, int(ASTT.C_enumdecl)))
                if hasattr(CS, "symbol_dict"):
                    if (safe_name, ASTT.C_enumdecl) in CS.symbol_dict:
                        return (ASTT.C_enum, CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(safe_name, ASTT.C_enumdecl)]))
                    if (safe_name, ASTT.C_enum) in CS.symbol_dict:
                        return (ASTT.C_enum, CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(safe_name, ASTT.C_enum)]))
                op_idx = len(CS.cs)
                with CS(REF_NO_REF):
                    CS.store(m_ast.view(((m_ast.ast_id,),), None, safe_name, ASTT.C_enumnotbind))
                return (ASTT.C_enum, CS.ref(m_ast.ast_id, REF_POS, op_idx))

    return (ASTT.Undefined, 0)


class Ast_Statement(Ast):
    """Base class for C statement AST nodes."""
    type_id: int = ASTT.C_CompoundStmt

    def __init__(self, extent: Line, name: str = "", end_mode: int = End_Mode.Auto, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id: int = getattr(self.__class__, "type_id", ASTT.C_CompoundStmt)
        self.name = name
        self.cursor = cursor
        self.ast_ref: Any = None
        self.operands: list[str] = []
        self.zones: list[Any] = []
        self.call_exprs: list[Any] = []
        self.member_refs: list[Any] = []
        self.decl_refs: list[Any] = []

        if cursor is not None:
            try:
                compound_kids = [k for k in cursor.get_children() if k.kind == cc.CursorKind.COMPOUND_STMT]
                if compound_kids:
                    self.zones.append(Zone(Zone_Type.Compound_Stmt, compound_kids))
            except Exception:
                pass

    def within_range(self, token: Any, ast_kind: int) -> bool:
        if not self.need_processing:
            return False
        tline = token.line
        tspelling = token.spelling_str
        if self.end_mode == End_Mode.Extent:
            if not self.extent.is_inside(tline):
                self.need_processing = False
                return False
            self.extent.grow(tline)
            return True
        elif self.end_mode in (End_Mode.Auto, End_Mode.Semicolon):
            if ast_kind == AST_KIND.punctuation and tspelling == ";":
                self.extent.grow(tline)
                self.need_processing = False
                return True
        self.extent.grow(tline)
        return True

    def exec_comment(self, token: Any, cursor: Any) -> None:
        for zone in reversed(self.zones):
            if not zone.completed and zone.check_exec(token, cursor, AST_KIND.comment):
                return

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        for zone in reversed(self.zones):
            if not zone.completed and zone.check_exec(token, cursor, AST_KIND.punctuation):
                return
        tspelling = token.spelling_str
        if tspelling == "{" and (not self.zones or self.zones[-1].completed):
            self.zones.append(Zone(Zone_Type.Compound_Stmt, (cursor,)))
            return
        self.operands.append(tspelling)

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        for zone in reversed(self.zones):
            if not zone.completed and zone.check_exec(token, cursor, AST_KIND.keyword):
                return
        if not self.name:
            self.name = token.spelling_str
        self.operands.append(token.spelling_str)

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        for zone in reversed(self.zones):
            if not zone.completed and zone.check_exec(token, cursor, AST_KIND.identifier):
                return
        k = getattr(cursor, "kind", None)
        if k == cc.CursorKind.CALL_EXPR:
            self.call_exprs.append(Ast_CallExpr(token.line, token.spelling_str, cursor=cursor))
            self.call_exprs[-1].callee_cursor = cursor
        elif k == cc.CursorKind.MEMBER_REF_EXPR:
            self.member_refs.append(Ast_MemberRefExpr(token.line, token.spelling_str, cursor=cursor))
            self.member_refs[-1].member_cursor = cursor
        elif k is not None and k not in _SKIP_REF_KINDS:
            ref = getattr(cursor, "referenced", None)
            ref_k = getattr(ref, "kind", None) if ref is not None else None
            if ref_k in (cc.CursorKind.FUNCTION_DECL, cc.CursorKind.CXX_METHOD):
                self.call_exprs.append(Ast_CallExpr(token.line, token.spelling_str, cursor=cursor))
                self.call_exprs[-1].callee_cursor = cursor
            elif ref_k == cc.CursorKind.FIELD_DECL:
                self.member_refs.append(Ast_MemberRefExpr(token.line, token.spelling_str, cursor=cursor))
                self.member_refs[-1].member_cursor = cursor
            elif ref_k in (cc.CursorKind.VAR_DECL, cc.CursorKind.PARM_DECL) or k == cc.CursorKind.DECL_REF_EXPR:
                self.decl_refs.append(Ast_DeclRefExpr(token.line, token.spelling_str, cursor=cursor))
                self.decl_refs[-1].decl_cursor = cursor
        self.operands.append(token.spelling_str)

    def exec_literal(self, token: Any, cursor: Any) -> None:
        for zone in reversed(self.zones):
            if not zone.completed and zone.check_exec(token, cursor, AST_KIND.literal):
                return
        self.operands.append(token.spelling_str)

    def exec_filter(self, token: Any, cursor: Any, kind: int) -> None:
        match kind:
            case AST_KIND.comment:
                self.exec_comment(token, cursor)
            case AST_KIND.punctuation:
                self.exec_punctuation(token, cursor)
            case AST_KIND.keyword:
                self.exec_keyword(token, cursor)
            case AST_KIND.identifier:
                self.exec_identifier(token, cursor)
            case AST_KIND.literal:
                self.exec_literal(token, cursor)

    def _extract_nested(self, CS: Any, create_tags: bool = False) -> None:
        if self.zones:
            with CS(REF_MULTI):
                for zone in self.zones:
                    zone.extract(CS, create_tags=create_tags)
        for call_expr in self.call_exprs:
            with CS(REF_NO_REF):
                call_expr.extract(CS, create_tag=create_tags)
        for member_ref in self.member_refs:
            with CS(REF_NO_REF):
                member_ref.extract(CS, create_tag=create_tags)
        for decl_ref in self.decl_refs:
            with CS(REF_NO_REF):
                decl_ref.extract(CS, create_tag=create_tags)

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        stmt_name = str(self.name)[:255] if self.name else ""
        self.extract_1arg(CS, self.type_id, stmt_name, self.extent, create_tag=create_tag)


class Ast_Expression(Ast):
    """Base class for C expression AST nodes."""
    pass


AST_Expression = Ast_Expression


class Ast_CompoundStmt(Ast_Statement):
    """AST node for compound block statements ({ ... })."""
    type_id = ASTT.C_CompoundStmt

    def __init__(self, extent: Line, name: str = "{}", end_mode: int = End_Mode.Auto, cursor: Any = None) -> None:
        super().__init__(extent, name, end_mode, cursor)
        self.type_id = ASTT.C_CompoundStmt
        self.used_types: set[str] = set()

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        with CS(REF_POS):
            CS.store(m_ast.view(((m_ast.ast_id,),), None, "{}", ASTT.C_CompoundStmt))
            ast_id_route = CS.get_route_parse()
        self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
        if create_tag:
            with CS(REF_NO_REF):
                if G.OVERRIDE_FORCE_AST_DEBUG:
                    self.ast_debug(CS, ast_id_route)
                self.tag(CS, ast_id_route, self.extent, ast_name="{}", ast_type=ASTT.C_CompoundStmt)
        if self.cursor is not None:
            collect_cursor_used_types(self.cursor, self.used_types)


class Ast_IfStmt(Ast_Statement):
    """AST node for if statements."""
    type_id = ASTT.C_IfStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_SwitchStmt(Ast_Statement):
    """AST node for switch statements."""
    type_id = ASTT.C_SwitchStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_CaseStmt(Ast_Statement):
    """AST node for case statements."""
    type_id = ASTT.C_CaseStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_DefaultStmt(Ast_Statement):
    """AST node for default statements."""
    type_id = ASTT.C_DefaultStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_WhileStmt(Ast_Statement):
    """AST node for while statements."""
    type_id = ASTT.C_WhileStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_DoStmt(Ast_Statement):
    """AST node for do-while statements."""
    type_id = ASTT.C_DoStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_ForStmt(Ast_Statement):
    """AST node for for loops."""
    type_id = ASTT.C_ForStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_ReturnStmt(Ast_Statement):
    """AST node for return statements."""
    type_id = ASTT.C_ReturnStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_BreakStmt(Ast_Statement):
    """AST node for break statements."""
    type_id = ASTT.C_BreakStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_ContinueStmt(Ast_Statement):
    """AST node for continue statements."""
    type_id = ASTT.C_ContinueStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_GotoStmt(Ast_Statement):
    """AST node for goto statements."""
    type_id = ASTT.C_GotoStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_AsmStmt(Ast_Statement):
    """AST node for inline asm statements."""
    type_id = ASTT.C_AsmStmt

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        self.extract_1arg(CS, self.type_id, "", self.extent, create_tag=create_tag)


class Ast_LabelStmt(Ast_Statement):
    """AST node for goto jump labels."""
    type_id = ASTT.C_LabelStmt

    def __init__(self, extent: Line, name: str = "", end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        if not name and cursor is not None:
            name = safe_cursor_spelling(cursor)
        super().__init__(extent, name=name, end_mode=end_mode, cursor=cursor)

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        for zone in reversed(self.zones):
            if not zone.completed and zone.check_exec(token, cursor, AST_KIND.keyword):
                return
        self.operands.append(token.spelling_str)

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        for zone in reversed(self.zones):
            if not zone.completed and zone.check_exec(token, cursor, AST_KIND.identifier):
                return
        if not self.name:
            self.name = token.spelling_str
        super().exec_identifier(token, cursor)

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        label_name = str(self.name)[:255] if self.name else "label"
        self.extract_1arg(CS, self.type_id, label_name, self.extent, create_tag=create_tag)


class Ast_BinaryOperator(Ast_Statement):
    """AST node for binary operators in statement contexts."""

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        op_name = str(self.name)[:255] if self.name else "op"
        self.extract_1arg(CS, ASTT.C_BinaryOperator, op_name, self.extent, create_tag=create_tag)


class Ast_UnaryOperator(Ast_Statement):
    """AST node for unary operators in statement contexts."""

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self._extract_nested(CS, create_tags=False)
        op_name = str(self.name)[:255] if self.name else "op"
        self.extract_1arg(CS, ASTT.C_UnaryOperator, op_name, self.extent, create_tag=create_tag)


class AST_Array(AST_Expression):
    """AST container for array dimension extents ([...])."""

    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent) -> None:
        super().__init__(extent, end_mode=end_mode)
        self.data: list[str] = []
        self.bracket_depth = 1

    def within_range(self, token: Any, ast_kind: int) -> bool:
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

    def exec_filter(self, token: Any, cursor: Any, ast_kind: int) -> None:
        if ast_kind == AST_KIND.comment:
            return
        self.data.append(token.spelling_str)

    def extract(self, CS: Any) -> None:
        pass


class InitializerEntry:
    """Designated member assignment within an initializer list expression."""

    def __init__(self, member_name: str, member_line: Line, member_cursor: Any = None) -> None:
        self.member_name = member_name
        self.member_line = Line(member_line)
        self.member_cursor = member_cursor
        self.value_tokens: list[tuple[Any, Any, int]] = []
        self.value_extent: Line | None = None


class AST_Initializer(AST_Expression):
    """AST container for variable and struct initializer expressions."""

    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent) -> None:
        super().__init__(extent, end_mode=end_mode)
        self.type_id = ASTT.C_InitListExpr
        self.data: list[str] = []
        self.brace_depth = 0
        self.paren_depth = 0
        self.bracket_depth = 0
        self.call_exprs: list[Ast_CallExpr] = []
        self.member_refs: list[Ast_MemberRefExpr] = []
        self.decl_refs: list[Ast_DeclRefExpr] = []
        self.entries: list[InitializerEntry] = []
        self.cur_entry: InitializerEntry | None = None
        self.entry_state: int = 0
        self.has_braces: bool = False
        self.init_extent: Line | None = None

    def within_range(self, token: Any, ast_kind: int) -> bool:
        if not self.need_processing:
            return False
        if ast_kind == AST_KIND.punctuation:
            spelling = token.spelling_str
            if spelling == "{":
                self.brace_depth += 1
                self.has_braces = True
                if self.init_extent is None:
                    self.init_extent = Line(token.line)
                else:
                    self.init_extent.grow(token.line)
            elif spelling == "}":
                self.brace_depth = max(0, self.brace_depth - 1)
                if self.init_extent is not None:
                    self.init_extent.grow(token.line)
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

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        k = getattr(cursor, "kind", None)
        if k == cc.CursorKind.CALL_EXPR:
            self.call_exprs.append(Ast_CallExpr(token.line, token.spelling_str, cursor=cursor))
            self.call_exprs[-1].callee_cursor = cursor
        elif k == cc.CursorKind.MEMBER_REF_EXPR:
            self.member_refs.append(Ast_MemberRefExpr(token.line, token.spelling_str, cursor=cursor))
            self.member_refs[-1].member_cursor = cursor
        elif k is not None and k not in _SKIP_REF_KINDS:
            ref = getattr(cursor, "referenced", None)
            ref_k = getattr(ref, "kind", None) if ref is not None else None
            if ref_k in (cc.CursorKind.FUNCTION_DECL, cc.CursorKind.CXX_METHOD):
                self.call_exprs.append(Ast_CallExpr(token.line, token.spelling_str, cursor=cursor))
                self.call_exprs[-1].callee_cursor = cursor
            elif ref_k == cc.CursorKind.FIELD_DECL:
                self.member_refs.append(Ast_MemberRefExpr(token.line, token.spelling_str, cursor=cursor))
                self.member_refs[-1].member_cursor = cursor
            elif ref_k in (cc.CursorKind.VAR_DECL, cc.CursorKind.PARM_DECL) or k == cc.CursorKind.DECL_REF_EXPR:
                self.decl_refs.append(Ast_DeclRefExpr(token.line, token.spelling_str, cursor=cursor))
                self.decl_refs[-1].decl_cursor = cursor
        self.data.append(token.spelling_str)

    def exec_filter(self, token: Any, cursor: Any, ast_kind: int) -> None:
        if ast_kind == AST_KIND.comment:
            return

        spelling = token.spelling_str

        if self.brace_depth >= 1:
            if spelling == "." and self.brace_depth == 1 and self.paren_depth == 0 and self.bracket_depth == 0:
                if self.cur_entry is not None:
                    self.entries.append(self.cur_entry)
                    self.cur_entry = None
                self.entry_state = 1
                return

            if self.entry_state == 1:
                if ast_kind == AST_KIND.identifier:
                    self.cur_entry = InitializerEntry(spelling, token.line, cursor)
                    self.entry_state = 2
                    return

            if self.entry_state == 2:
                if spelling == "=":
                    self.entry_state = 3
                    return

            if self.entry_state == 3:
                if self.brace_depth == 1 and self.paren_depth == 0 and self.bracket_depth == 0 and spelling in (",", "}"):
                    if self.cur_entry is not None:
                        self.entries.append(self.cur_entry)
                        self.cur_entry = None
                    self.entry_state = 0
                    return
                if self.cur_entry is not None:
                    self.cur_entry.value_tokens.append((token, cursor, ast_kind))
                    if self.cur_entry.value_extent is None:
                        self.cur_entry.value_extent = Line(token.line)
                    else:
                        self.cur_entry.value_extent.grow(token.line)

        if ast_kind == AST_KIND.identifier:
            self.exec_identifier(token, cursor)
            return
        self.data.append(spelling)

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        if self.cur_entry is not None:
            self.entries.append(self.cur_entry)
            self.cur_entry = None

        if self.entries:
            ext = self.init_extent or self.extent
            container_items = []
            p = 0
            for entry in self.entries:
                member_node = Ast_MemberRefExpr(entry.member_line, entry.member_name, cursor=entry.member_cursor)
                member_node.extract(CS, create_tag=create_tag)
                container_items.append((p, int(ASTT.C_MemberRefExpr), member_node.ast_ref))
                p += 1

                if entry.value_tokens:
                    val_tok = None
                    val_cur = None
                    for vt, vc, vk in entry.value_tokens:
                        if vk == AST_KIND.identifier:
                            val_tok, val_cur = vt, vc
                            break
                    if val_tok is None:
                        for vt, vc, vk in entry.value_tokens:
                            if vk == AST_KIND.literal:
                                val_tok, val_cur = vt, vc
                                break
                    if val_tok is None and entry.value_tokens:
                        val_tok, val_cur, _ = entry.value_tokens[-1]

                    val_name = str(val_tok.spelling_str)[:255] if val_tok is not None else ""
                    val_ext = entry.value_extent or (val_tok.line if val_tok is not None else entry.member_line)
                    val_node = Ast_DeclRefExpr(val_ext, val_name, cursor=val_cur)
                    val_node.extract(CS, create_tag=create_tag)
                    container_items.append((p, int(val_node.type_id), val_node.ast_ref))
                    p += 1

            total_containers = len(container_items)
            if total_containers == 0:
                with CS(REF_POS):
                    CS.store(m_ast.view(((m_ast.ast_id,),), None, "{}", ASTT.C_InitListExpr))
                    init_route = CS.get_route_parse()
            else:
                container_args = []
                for priority, t_code, ref_ast_id in container_items:
                    container_args.extend((None, priority, t_code, ref_ast_id))
                with CS(REF_POS):
                    CS.store(m_ast.view(
                        ((m_ast.ast_id, m_ast_container.ast_id, total_containers),),
                        None,
                        "{}",
                        ASTT.C_InitListExpr,
                        *container_args,
                    ))
                    init_route = CS.get_route_parse()

            self.ast_ref = CS.ref(m_ast.ast_id, *init_route)
            if create_tag:
                with CS(REF_NO_REF):
                    if G.OVERRIDE_FORCE_AST_DEBUG:
                        self.ast_debug(CS, init_route)
                    self.tag(CS, init_route, ext, ast_name="{}", ast_type=ASTT.C_InitListExpr)

        for call_expr in self.call_exprs:
            with CS(REF_NO_REF):
                try:
                    call_expr.extract(CS, create_tag=create_tag)
                except TypeError:
                    call_expr.extract(CS)
        for member_ref in self.member_refs:
            with CS(REF_NO_REF):
                try:
                    member_ref.extract(CS, create_tag=create_tag)
                except TypeError:
                    member_ref.extract(CS)
        for decl_ref in self.decl_refs:
            with CS(REF_NO_REF):
                try:
                    decl_ref.extract(CS, create_tag=create_tag)
                except TypeError:
                    decl_ref.extract(CS)


class AST_Enum_Equal(AST_Expression):
    """AST container for enum constant value assignments (= expr)."""

    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent) -> None:
        super().__init__(extent, end_mode=end_mode)
        self.data: list[str] = []

    def within_range(self, token: Any, ast_kind: int) -> bool:
        if not self.need_processing:
            return False
        if ast_kind == AST_KIND.punctuation:
            if token.spelling_str in ("}", ","):
                self.need_processing = False
                return False
        self.extent.grow(token.line)
        return True

    def exec_filter(self, token: Any, cursor: Any, ast_kind: int) -> None:
        if ast_kind == AST_KIND.comment:
            return
        self.data.append(token.spelling_str)

    def extract(self, CS: Any) -> None:
        pass


class C_Type(Ast):
    """Represent C type declarations, specifiers, qualifiers, and declarators."""

    def __init__(self, extent: Line, end_mode: int = End_Mode.Auto, cursor: Any = None) -> None:
        super().__init__(extent, end_mode=end_mode)
        self.cursor = cursor
        self.zones: list[Zone] = []
        self.content = TypeSegment()
        self.typedata: list[TypeSegment] = []
        self.func_proto = False
        self.paren_depth = 0
        self.storage_class: TypeToken | None = None
        self.has_functionproto: bool = False
        self.struct_union_enum_type: ASTT | None = None
        self.sig_extent: Line | None = None
        self.ast_ref: Any = None

    def within_range(self, token: Any, ast_kind: int) -> bool:
        if not self.need_processing:
            return False

        tline = token.line
        tspelling = token.spelling_str

        if self.zones:
            last_z = self.zones[-1]
            if not last_z.completed:
                if ast_kind == AST_KIND.punctuation:
                    if tspelling in (",", "}") and last_z.zone_type == Zone_Type.Enum_Equal:
                        last_z.completed = True
                        if last_z.children and last_z.children[-1].need_processing:
                            last_z.children[-1].need_processing = False
                    elif tspelling in (";", ",") and last_z.zone_type == Zone_Type.Initializer_Expr:
                        child_brace = getattr(last_z.children[-1], "brace_depth", 0) if last_z.children else 0
                        if (
                            last_z.brace_depth <= 0
                            and child_brace <= 0
                            and last_z.paren_depth <= 0
                            and last_z.bracket_depth <= 0
                        ):
                            last_z.completed = True
                            if last_z.children and last_z.children[-1].need_processing:
                                last_z.children[-1].need_processing = False
                if last_z.children and not last_z.children[-1].need_processing:
                    if last_z.zone_type in (
                        Zone_Type.Array_Content,
                        Zone_Type.Enum_Equal,
                        Zone_Type.Initializer_Expr,
                    ):
                        last_z.completed = True
                if not last_z.completed:
                    self.extent.grow(tline)
                    return True
            else:
                for z in reversed(self.zones[:-1]):
                    if not z.completed:
                        self.extent.grow(tline)
                        return True

        if self.paren_depth > 0:
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
                    self.extent.grow(tline)
                    self.need_processing = False
                    return False
            case End_Mode.Comma:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(tline)
                    return True
                if tspelling in (",", ")", "}"):
                    self.need_processing = False
                    return False
            case End_Mode.Extent:
                if not self.extent.is_inside(tline):
                    self.need_processing = False
                    return False
                if ast_kind == AST_KIND.punctuation and tspelling == ";":
                    self.extent.grow(tline)
                    self.need_processing = False
                    return False

        self.extent.grow(tline)
        return True

    def swap_out(self) -> None:
        """Move accumulated tokens from content into structured typedata."""
        if self.content.content:
            last_type = self.content.content[-1].type
            if last_type in {ASTT.C_struct, ASTT.C_union, ASTT.C_enum}:
                if self.struct_union_enum_type is None:
                    self.struct_union_enum_type = last_type
            elif last_type == ASTT.C_functionproto:
                self.has_functionproto = True
        self.typedata.append(self.content)
        self.content = TypeSegment()

    def keyword_parse(self, token: Any, cursor: Any) -> None:
        tspelling = token.spelling_str
        match tspelling:
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

            # Qualifiers
            case "const" | "__const" | "__const__":
                if CQual.const in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual.const
                self.content.cqual_content.append(TypeToken(token, ASTT.C_Qconst))
            case "volatile" | "__volatile" | "__volatile__":
                if CQual.volatile in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual.volatile
                self.content.cqual_content.append(TypeToken(token, ASTT.C_Qvolatile))
            case "restrict" | "__restrict" | "__restrict__":
                if CQual.restrict in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual.restrict
                self.content.cqual_content.append(TypeToken(token, ASTT.C_Qrestrict))
            case "_Atomic":
                if CQual._Atomic in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual._Atomic
                self.content.cqual_content.append(TypeToken(token, ASTT.C_Q_Atomic))

            # Function Specifiers
            case "inline" | "__inline" | "__inline__" | "__always_inline" | "__gnu_inline":
                self.content.append(TypeToken(token, ASTT.C_FSinline))
            case "_Noreturn":
                self.content.append(TypeToken(token, ASTT.C_FS_Noreturn))

            # Alignment & Size Specifiers
            case "_Alignas" | "alignas":
                self.content.append(TypeToken(token, ASTT.C_AS__Alignas))
            case "sizeof" | "_Alignof" | "alignof" | "__alignof__" | "__alignof":
                return
            case "typeof" | "__typeof__" | "__typeof":
                return
            case "__attribute__" | "__attribute" | "__declspec":
                return
            case "__extension__":
                return
            case "asm" | "__asm__" | "__asm":
                return
            case "_Generic":
                return
            case "_Static_assert" | "static_assert":
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
            case "__func__" | "__FUNCTION__" | "__PRETTY_FUNCTION__":
                return
            case "if" | "else" | "return" | "switch" | "case" | "default" | "break" | "continue" | "for" | "while" | "do" | "goto":
                return
            case _:
                return

    def exec_comment(self, token: Any, cursor: Any) -> None:
        if self.zones:
            last_z = self.zones[-1]
            if not last_z.completed and last_z.check_exec(token, cursor, AST_KIND.comment):
                return
            for zone in reversed(self.zones[:-1]):
                if not zone.completed and zone.check_exec(token, cursor, AST_KIND.comment):
                    return

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        tspelling = token.spelling_str
        if tspelling == "(":
            self.paren_depth += 1
        elif tspelling == ")":
            self.paren_depth = max(0, self.paren_depth - 1)

        if self.zones:
            last_z = self.zones[-1]
            if not last_z.completed and last_z.check_exec(token, cursor, AST_KIND.punctuation):
                if tspelling == "}" and last_z.zone_type == Zone_Type.Compound_Stmt and last_z.completed:
                    self.need_processing = False
                return
            for zone in reversed(self.zones[:-1]):
                if not zone.completed and zone.check_exec(token, cursor, AST_KIND.punctuation):
                    if tspelling == "}" and zone.zone_type == Zone_Type.Compound_Stmt and zone.completed:
                        self.need_processing = False
                    return

        match tspelling:
            case "*":
                self.content.append(TypeToken(token, ASTT.C_pointer))
                if self.func_proto:
                    self.name = ""
                    for ts in self.typedata:
                        for tok in ts.content:
                            if tok.type == 0:
                                tok.type = ASTT.C_SCtypedef
                    self.content.append(TypeToken(token, ASTT.C_functionproto))
                    self.has_functionproto = True
                    self.func_proto = False
                    return
                self.swap_out()
            case "(":
                has_func_proto = self.has_functionproto or (
                    bool(self.content.content) and self.content.content[-1].type == ASTT.C_functionproto
                )
                if not has_func_proto and cursor.kind not in {cc.CursorKind.FUNCTION_DECL, cc.CursorKind.CXX_METHOD}:
                    self.func_proto = True
                    return
                if self.func_proto:
                    self.func_proto = False

                fn_cursor = (
                    self.cursor
                    if (
                        self.cursor is not None
                        and getattr(self.cursor, "kind", None)
                        in {
                            cc.CursorKind.FUNCTION_DECL,
                            cc.CursorKind.CXX_METHOD,
                            cc.CursorKind.FIELD_DECL,
                            cc.CursorKind.VAR_DECL,
                            cc.CursorKind.TYPEDEF_DECL,
                        }
                    )
                    else cursor
                )
                children = tuple(fn_cursor.get_children())
                arg_children = [
                    kids for kids in children
                    if kids.kind == cc.CursorKind.PARM_DECL
                    and (not self.name or safe_cursor_spelling(kids) != self.name)
                ]
                tokens_arr = getattr(self, "tokens_array", None)
                if arg_children:
                    self.zones.append(Zone(Zone_Type.Function_Args, arg_children, tokens_array=tokens_arr))
                elif not self.zones:
                    self.zones.append(Zone(Zone_Type.Function_Args, (), tokens_array=tokens_arr))
            case "{":
                kind = cursor.kind
                if kind == cc.CursorKind.COMPOUND_STMT:
                    if self.sig_extent is None:
                        self.sig_extent = Line(self.extent).new_end_reversed(token.line)
                    self.zones.append(Zone(Zone_Type.Compound_Stmt, (cursor,)))
                    return

                children = tuple(cursor.get_children())
                compound_kids = [k for k in children if k.kind == cc.CursorKind.COMPOUND_STMT]
                if compound_kids:
                    if self.sig_extent is None:
                        self.sig_extent = Line(self.extent).new_end_reversed(token.line)
                    self.zones.append(Zone(Zone_Type.Compound_Stmt, compound_kids))
                    return

                if kind in (cc.CursorKind.FUNCTION_DECL, cc.CursorKind.CXX_METHOD):
                    if self.sig_extent is None:
                        self.sig_extent = Line(self.extent).new_end_reversed(token.line)
                    self.zones.append(Zone(Zone_Type.Compound_Stmt, (cursor,)))
                    return

                if kind == cc.CursorKind.ENUM_DECL:
                    zone_type = Zone_Type.Enum_Content
                elif kind in {cc.CursorKind.STRUCT_DECL, cc.CursorKind.UNION_DECL, cc.CursorKind.CLASS_DECL}:
                    zone_type = Zone_Type.Declared_Args
                else:
                    return

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
                    if kids:
                        self.zones.append(Zone(Zone_Type.Initializer_Expr, kids))
                    else:
                        self.zones.append(Zone(Zone_Type.Initializer_Expr, (cursor,)))

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        if self.zones:
            last_z = self.zones[-1]
            if not last_z.completed and last_z.check_exec(token, cursor, AST_KIND.keyword):
                return
            for zone in reversed(self.zones[:-1]):
                if not zone.completed and zone.check_exec(token, cursor, AST_KIND.keyword):
                    return
        self.keyword_parse(token, cursor)

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        if self.zones:
            last_z = self.zones[-1]
            if not last_z.completed and last_z.check_exec(token, cursor, AST_KIND.identifier):
                return
            for zone in reversed(self.zones[:-1]):
                if not zone.completed and zone.check_exec(token, cursor, AST_KIND.identifier):
                    return

        tspelling = token.spelling_str
        if tspelling in {"__func__", "__FUNCTION__", "__PRETTY_FUNCTION__"}:
            return

        # Function declaration identifier check
        if (
            cursor.kind in {cc.CursorKind.FUNCTION_DECL, cc.CursorKind.CXX_METHOD}
            and tspelling == safe_cursor_spelling(cursor)
        ):
            self.name = tspelling
            tt = TypeToken(token, ASTT.C_functionproto)
            tt.is_definition = True
            self.content.append(tt)
            self.content.content[-1].is_definition = cursor.is_definition()
            self.swap_out()
            self.has_functionproto = True
            return

        if self.content.content:
            if self.content.content[-1].type in {
                ASTT.C_struct,
                ASTT.C_union,
                ASTT.C_enum,
                ASTT.C_functionproto,
            }:
                if cursor.kind not in {cc.CursorKind.FIELD_DECL, cc.CursorKind.VAR_DECL, cc.CursorKind.PARM_DECL}:
                    self.name = tspelling
                tt = TypeToken(token, self.content.content[-1].type, cursor=cursor)
                try:
                    decl = getattr(cursor, "type", None)
                    decl = decl.get_declaration() if decl is not None and hasattr(decl, "get_declaration") else None
                    if decl is None or not getattr(decl, "location", None) or not getattr(decl.location, "file", None):
                        decl = getattr(cursor, "referenced", None) or cursor.get_definition()
                    if decl is not None and getattr(decl, "location", None) and getattr(decl.location, "file", None):
                        rf = get_rel_file_for_cursor(None, decl)
                        cur_f = getattr(G, "CURRENT_PARSING_FILE", "")
                        if rf and rf != cur_f:
                            tt.foreign_file = rf
                            tt.foreign_name = safe_cursor_spelling(decl) or tspelling
                except Exception:
                    pass
                self.content.append(tt)
                self.swap_out()
                return

        # Declarator identifier check (field, variable, or parameter declaration)
        if (
            cursor.kind in {cc.CursorKind.FIELD_DECL, cc.CursorKind.VAR_DECL, cc.CursorKind.PARM_DECL}
            and tspelling == safe_cursor_spelling(cursor)
        ):
            self.name = tspelling
            self.content.append(TypeToken(token, cursor=cursor))
            self.swap_out()
            return

        if self.struct_union_enum_type is not None:
            tt = TypeToken(token, self.struct_union_enum_type, cursor=cursor)
            try:
                decl = getattr(cursor, "type", None)
                decl = decl.get_declaration() if decl is not None and hasattr(decl, "get_declaration") else None
                if decl is None or not getattr(decl, "location", None) or not getattr(decl.location, "file", None):
                    decl = getattr(cursor, "referenced", None) or cursor.get_definition()
                if decl is not None and getattr(decl, "location", None) and getattr(decl.location, "file", None):
                    rf = get_rel_file_for_cursor(None, decl)
                    cur_f = getattr(G, "CURRENT_PARSING_FILE", "")
                    if rf and rf != cur_f:
                        tt.foreign_file = rf
                        tt.foreign_name = safe_cursor_spelling(decl) or tspelling
            except Exception:
                pass
            self.content.append(tt)
            self.swap_out()
            return


        if cursor.type.kind == cc.TypeKind.TYPEDEF:
            self.content.append(TypeToken(token, ASTT.C_SCtypedef))
            self.swap_out()
            return

        if (
            cursor.kind in {cc.CursorKind.FIELD_DECL, cc.CursorKind.VAR_DECL, cc.CursorKind.PARM_DECL}
            and tspelling != safe_cursor_spelling(cursor)
        ):
            self.content.append(TypeToken(token, ASTT.C_SCtypedef))
            self.swap_out()
            return

        if self.content.content:
            if self.content.content[-1].type in {
                ASTT.C_struct,
                ASTT.C_union,
                ASTT.C_enum,
                ASTT.C_functionproto,
            }:
                if cursor.kind not in {cc.CursorKind.FIELD_DECL, cc.CursorKind.VAR_DECL, cc.CursorKind.PARM_DECL}:
                    self.name = tspelling
                self.content.append(TypeToken(token, self.content.content[-1].type))
                self.swap_out()
                return

        self.content.append(TypeToken(token))
        self.swap_out()

    def exec_literal(self, token: Any, cursor: Any) -> bool:
        if self.zones:
            last_z = self.zones[-1]
            if not last_z.completed and last_z.check_exec(token, cursor, AST_KIND.literal):
                return True
            for zone in reversed(self.zones[:-1]):
                if not zone.completed and zone.check_exec(token, cursor, AST_KIND.literal):
                    return True
        self.content.append(TypeToken(token, ASTT.Undefined))
        return True

    def exec_filter(self, token: Any, cursor: Any, kind: int) -> None:
        match kind:
            case AST_KIND.comment:
                self.exec_comment(token, cursor)
            case AST_KIND.punctuation:
                self.exec_punctuation(token, cursor)
            case AST_KIND.keyword:
                self.exec_keyword(token, cursor)
            case AST_KIND.identifier:
                self.exec_identifier(token, cursor)
            case AST_KIND.literal:
                self.exec_literal(token, cursor)

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        if self.content:
            self.swap_out()

        # 1. Process Child Zones by Category
        declared_args_link = ()
        enum_content_link = ()
        function_args_link = ()
        compound_stmt_link = ()
        initializer_ref = None

        for zone in self.zones:
            with CS(REF_MULTI):
                zone.extract(CS, create_tags=(create_tag if zone.zone_type == Zone_Type.Initializer_Expr else False))
                link = tuple(CS.route[-2:])
                if zone.zone_type == Zone_Type.Declared_Args:
                    declared_args_link = link
                elif zone.zone_type == Zone_Type.Enum_Content:
                    enum_content_link = link
                elif zone.zone_type == Zone_Type.Function_Args:
                    function_args_link = link
                elif zone.zone_type == Zone_Type.Compound_Stmt:
                    compound_stmt_link = link
                elif zone.zone_type == Zone_Type.Initializer_Expr:
                    initializer_ref = getattr(zone, "initializer_ref", None)

        # 2. Parse self.typedata into distinct declarations (final_types)
        root_type = TypeSegment()
        type_constructor = []
        final_types = []

        for typesegment in self.typedata:
            if not root_type:
                root_type = typesegment

            type_constructor.append(typesegment)

            if typesegment is not root_type and typesegment.content and typesegment.content[-1].type == 0:
                final_types.append(tuple(type_constructor))
                type_constructor = [root_type]

        if not final_types and type_constructor:
            final_types.append(tuple(type_constructor))

        # 3. Zone Handling for Type Definitions (struct/union/enum/functionproto declarations)
        for final_type in final_types:
            has_var = any(
                token.type == 0 for ts in final_type for token in ts.content
            )
            has_pointer = any(
                token.type == ASTT.C_pointer for ts in final_type for token in ts.content
            )
            is_func_decl = (
                self.cursor is not None
                and getattr(self.cursor, "kind", None) in {cc.CursorKind.FUNCTION_DECL, cc.CursorKind.CXX_METHOD}
            )

            for typesegment in final_type:
                # Check definition and type categories in a single pass
                is_def = False
                has_struct = False
                has_enum = False
                has_func = False
                target_link = ()

                for it in typesegment.content:
                    t = it.type
                    if not is_def and t in _DEF_TYPES and getattr(it, "is_definition", False):
                        is_def = True
                    if t in (ASTT.C_struct, ASTT.C_union):
                        has_struct = True
                    elif t == ASTT.C_enum:
                        has_enum = True
                    elif t == ASTT.C_functionproto:
                        has_func = True

                if has_struct:
                    if declared_args_link:
                        is_def = True
                        target_link = declared_args_link
                    elif not has_var and not has_pointer and self.end_mode != End_Mode.Comma:
                        is_def = True
                        target_link = ()
                elif has_enum:
                    if enum_content_link:
                        is_def = True
                        target_link = enum_content_link
                    elif not has_var and not has_pointer and self.end_mode != End_Mode.Comma:
                        is_def = True
                        target_link = ()
                elif has_func:
                    if is_func_decl or function_args_link or compound_stmt_link or (not has_var and self.end_mode != End_Mode.Comma):
                        is_def = True
                        target_link = function_args_link

                if is_def:
                    def_candidates = [item for item in typesegment.content if item.type in _DEF_TYPES]
                    named_candidates = [
                        item for item in def_candidates 
                        if item.code not in _NON_NAME_TOKENS
                    ]
                    def_items = named_candidates if named_candidates else def_candidates[:1]
                    for item in def_items:
                        if item.type in (ASTT.C_struct, ASTT.C_union) and not declared_args_link:
                            decl_type = item.type
                        elif item.type == ASTT.C_enum and not enum_content_link:
                            decl_type = item.type
                        else:
                            decl_type = get_decl_type(item.type)

                        safe_item_name = str(item.code)[:255]
                        if safe_item_name in _NON_NAME_TOKENS:
                            safe_item_name = str(self.name)[:255]
                        if not safe_item_name:
                            safe_item_name = str(self.name)[:255]
                        if item.type == ASTT.C_functionproto:
                            ret_idx = final_type.index(typesegment)
                            item_idx = (
                                typesegment.content.index(item)
                                if item in typesegment.content
                                else len(typesegment.content)
                            )
                            pre_tokens = [
                                tok
                                for tok in typesegment.content[:item_idx]
                                if tok.type != 0 and tok.type != ASTT.Undefined
                            ]

                            valid_prev_segs = [
                                seg
                                for seg in final_type[:ret_idx]
                                if seg.cqual != CQual.Empty
                                or seg.ref_type != TSRef.No_Ref
                                or any(tok.type != 0 and tok.type != ASTT.Undefined for tok in seg.content)
                            ]

                            if pre_tokens:
                                ret_seg = TypeSegment()
                                for seg in valid_prev_segs:
                                    ret_seg.content.extend(seg.content)
                                ret_seg.content.extend(pre_tokens)
                                ret_seg.generate_ast(CS)
                            elif len(valid_prev_segs) == 1:
                                ret_seg = valid_prev_segs[0]
                                ret_seg.generate_ast(CS)
                            elif len(valid_prev_segs) > 1:
                                ret_seg = TypeSegment()
                                for seg in valid_prev_segs:
                                    ret_seg.content.extend(seg.content)
                                ret_seg.generate_ast(CS)
                            else:
                                ret_seg = TypeSegment()
                                ret_seg.content.append(TypeToken(item, ASTT.C_void))
                                ret_seg.generate_ast(CS)

                            ret_t_id = ret_seg.type_id
                            if ret_t_id is None or ret_t_id == 0 or ret_t_id == ASTT.Undefined:
                                valid_toks = [
                                    t for t in ret_seg.content if t.type != 0 and t.type != ASTT.Undefined
                                ]
                                if ret_seg.ref_type == TSRef.Route_Ref and not valid_toks:
                                    ret_t_id = ASTT.C_Compound
                                elif valid_toks:
                                    ret_t_id = valid_toks[0].type
                                else:
                                    ret_t_id = ASTT.C_void

                            if ret_seg.ref_type == TSRef.Route_Ref:
                                ret_ref_ast_id = CS.ref(m_ast.ast_id, *ret_seg.ref)
                            elif ret_seg.ref_type == TSRef.AST_Ref:
                                ret_ref_ast_id = ret_seg.ref
                            else:
                                ret_ref_ast_id = (
                                    ret_seg.ref_ast_id if ret_seg.ref_ast_id is not None else 0
                                )

                            trailing_items = ()
                            if self.zones:
                                for zone in self.zones:
                                    if zone.zone_type == Zone_Type.Compound_Stmt and getattr(zone, "compound_ref", None) is not None:
                                        trailing_items = (
                                            (
                                                ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                                                (None, ("rank",), int(ASTT.C_CompoundStmt), zone.compound_ref),
                                            ),
                                        )
                                        break

                            if target_link or trailing_items:
                                with CS(REF_POS):
                                    CS.store(m_ast.ref_view(
                                        ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                                        None,
                                        safe_item_name,
                                        decl_type,
                                        None,
                                        0,
                                        ret_t_id,
                                        ret_ref_ast_id,
                                        (
                                            ((m_ast.ast_id, None),),
                                            (
                                                (
                                                    ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                                                    (None, ("rank",), m_ast.type_id, m_ast.ast_id),
                                                ),
                                            ),
                                            tuple(target_link),
                                            1,
                                            trailing_items,
                                        ),
                                    ))
                                    ast_id_route = CS.get_route_parse()
                                    self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
                                    typesegment.ref_type = TSRef.Route_Ref
                                    typesegment.ref = ast_id_route
                            else:
                                with CS(REF_POS):
                                    CS.store(m_ast.view(
                                        ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                                        None,
                                        safe_item_name,
                                        decl_type,
                                        None,
                                        0,
                                        ret_t_id,
                                        ret_ref_ast_id,
                                    ))
                                    ast_id_route = CS.get_route_parse()
                                    self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
                                    typesegment.ref_type = TSRef.Route_Ref
                                    typesegment.ref = ast_id_route
                        else:
                            if target_link:
                                with CS(REF_POS):
                                    CS.store(m_ast.ref_view(
                                        ((m_ast.ast_id,),),
                                        None,
                                        safe_item_name,
                                        decl_type,
                                        (
                                            ((m_ast.ast_id, None),),
                                            (
                                                (
                                                    ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                                                    (None, ("rank",), m_ast.type_id, m_ast.ast_id),
                                                ),
                                            ),
                                            tuple(target_link),
                                        ),
                                    ))
                                    ast_id_route = CS.get_route_parse()
                                    self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
                                    typesegment.ref_type = TSRef.Route_Ref
                                    typesegment.ref = ast_id_route
                            else:
                                with CS(REF_POS):
                                    CS.store(m_ast.view(
                                        ((m_ast.ast_id,),),
                                        None,
                                        safe_item_name,
                                        decl_type,
                                    ))
                                    ast_id_route = CS.get_route_parse()
                                    self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
                                    typesegment.ref_type = TSRef.Route_Ref
                                    typesegment.ref = ast_id_route

                        has_var = any(
                            token.type == 0 for ts in final_type for token in ts.content
                        )
                        if safe_item_name and hasattr(CS, "symbol_dict"):
                            CS.symbol_dict[(safe_item_name, decl_type)] = ast_id_route[1]
                            CS.symbol_dict[(safe_item_name, item.type)] = ast_id_route[1]

                        if create_tag:
                            if has_var and item.type != ASTT.C_functionproto:
                                declared_zone = next((z for z in self.zones if z.zone_type in (Zone_Type.Declared_Args, Zone_Type.Enum_Content)), None)
                                def_end_line = declared_zone.extent.line_pos[1] if declared_zone else self.extent.line_pos[1]
                                def_end_char = declared_zone.extent.char_pos[1] if declared_zone else self.extent.char_pos[1]
                                def_extent = Line(self.extent.line_pos[0], def_end_line, self.extent.char_pos[0], def_end_char)
                                with CS(REF_NO_REF):
                                    if G.OVERRIDE_FORCE_AST_DEBUG:
                                        self.ast_debug(CS, ast_id_route)
                                    def_tag_ref = self.tag(
                                        CS,
                                        ast_id_route,
                                        def_extent,
                                        ast_name=safe_item_name,
                                        ast_type=decl_type,
                                    )
                                with CS(REF_POS):
                                    CS.store(m_symbol_def.set(
                                        None,
                                        CS.gp.VID,
                                        ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                                        def_tag_ref or getattr(self, "tag_ref", 0),
                                        self.ast_ref,
                                        safe_item_name,
                                        decl_type,
                                        def_extent.line_pos[0],
                                        def_extent.line_pos[1],
                                    ))
                            else:
                                with CS(REF_NO_REF):
                                    if G.OVERRIDE_FORCE_AST_DEBUG:
                                        self.ast_debug(CS, ast_id_route)
                                    tag_ref = self.tag(
                                        CS,
                                        ast_id_route,
                                        self.extent,
                                        ast_name=safe_item_name,
                                        ast_type=decl_type,
                                    )
                                if (decl_type in (ASTT.C_structdecl, ASTT.C_uniondecl, ASTT.C_enumdecl) or (item.type == ASTT.C_functionproto and (compound_stmt_link or is_func_decl))):
                                    with CS(REF_POS):
                                        CS.store(m_symbol_def.set(
                                            None,
                                            CS.gp.VID,
                                            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                                            tag_ref or getattr(self, "tag_ref", 0),
                                            self.ast_ref,
                                            safe_item_name,
                                            decl_type,
                                            self.extent.line_pos[0],
                                            self.extent.line_pos[1],
                                        ))
                                elif item.type == ASTT.C_functionproto and not compound_stmt_link:
                                    with CS(REF_POS):
                                        CS.store(m_symbol_ref.set(
                                            None,
                                            CS.gp.VID,
                                            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                                            tag_ref or getattr(self, "tag_ref", 0),
                                            self.ast_ref,
                                            int(SymbolRole.Declaration),
                                            self.extent.line_pos[0],
                                            self.extent.char_pos[0],
                                        ))

        # 4. Insert Variable / Declarator ASTs into ChangeSet
        for final_type in final_types:
            if not final_type:
                continue

            has_var = any(token.type == 0 for ts in final_type for token in ts.content)
            is_type_def = any(
                any(it.type in _DEF_TYPES for it in ts.content) for ts in final_type
            )
            is_func_decl = (
                self.cursor is not None
                and getattr(self.cursor, "kind", None) in {cc.CursorKind.FUNCTION_DECL, cc.CursorKind.CXX_METHOD}
            )
            is_param = self.end_mode == End_Mode.Comma or (
                self.cursor is not None and getattr(self.cursor, "kind", None) == cc.CursorKind.PARM_DECL
            )
            if is_type_def and not is_param and (
                not has_var
                or is_func_decl
                or (any(any(it.type == ASTT.C_functionproto for it in ts.content) for ts in final_type) and (function_args_link or compound_stmt_link))
            ):
                continue

            cs_inserter = []
            name = ""

            for typesegment in final_type:
                typesegment.generate_ast(CS)

            for typesegment in final_type:
                for item in typesegment.content:
                    if item.type == 0 or item.type == ASTT.Undefined:
                        name = item.code

            type_segments = [
                ts
                for ts in final_type
                if ts.cqual != CQual.Empty
                or ts.ref_type != TSRef.No_Ref
                or any(item.type != 0 and item.type != ASTT.Undefined for item in ts.content)
            ]

            for i, typesegment in enumerate(type_segments):
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
                cur_spelling = safe_cursor_spelling(self.cursor) if self.cursor is not None else ""
                name = cur_spelling if (is_param and cur_spelling) else (self.name if not (is_param and not has_var) else "")
            safe_name = str(name)[:255]

            if type_segments:
                if len(type_segments) == 1:
                    main_t_id = type_segments[0].type_id
                    if main_t_id is None:
                        if type_segments[0].content:
                            main_t_id = type_segments[0].content[0].type
                        else:
                            main_t_id = ASTT.C_Compound
                else:
                    main_t_id = ASTT.C_Compound

                # Invariant E: Simple primitive struct members generate NO m_ast_container records
                is_simple_type = (
                    len(type_segments) == 1
                    and type_segments[0].ref_type == TSRef.No_Ref
                    and type_segments[0].cqual == CQual.Empty
                    and main_t_id != ASTT.C_Compound
                    and main_t_id != 0
                )

                if initializer_ref is not None:
                    init_priority = len(type_segments)
                    cs_inserter.extend((None, init_priority, int(ASTT.C_InitListExpr), initializer_ref))
                    total_containers = len(type_segments) + 1
                else:
                    total_containers = len(type_segments)

                if is_simple_type and initializer_ref is None:
                    with CS(REF_POS):
                        CS.store(m_ast.view(
                            ((m_ast.ast_id,),),
                            None,
                            safe_name,
                            main_t_id,
                        ))
                        ast_id_route = CS.get_route_parse()
                        self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
                else:
                    with CS(REF_POS):
                        CS.store(m_ast.view(
                            ((m_ast.ast_id, m_ast_container.ast_id, total_containers),),
                            None,
                            safe_name,
                            main_t_id,
                            *cs_inserter,
                        ))
                        ast_id_route = CS.get_route_parse()
                        self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
            else:
                main_t_id = ASTT.C_Compound
                if initializer_ref is not None:
                    cs_inserter = [None, 0, int(ASTT.C_InitListExpr), initializer_ref]
                    with CS(REF_POS):
                        CS.store(m_ast.view(
                            ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                            None,
                            safe_name,
                            main_t_id,
                            *cs_inserter,
                        ))
                        ast_id_route = CS.get_route_parse()
                        self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
                else:
                    with CS(REF_POS):
                        CS.store(m_ast.view(
                            ((m_ast.ast_id,),),
                            None,
                            safe_name,
                            main_t_id,
                        ))
                        ast_id_route = CS.get_route_parse()
                        self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)

            if safe_name and hasattr(CS, "symbol_dict"):
                CS.symbol_dict[(safe_name, main_t_id)] = ast_id_route[1]
                CS.symbol_dict[(safe_name, ASTT.C_DeclRefExpr)] = ast_id_route[1]

            if create_tag:
                is_co_decl = is_type_def and any(z.zone_type in (Zone_Type.Declared_Args, Zone_Type.Enum_Content) for z in self.zones)
                if is_co_decl:
                    declared_zone = next((z for z in self.zones if z.zone_type in (Zone_Type.Declared_Args, Zone_Type.Enum_Content)), None)
                    v_start_l = declared_zone.extent.line_pos[1] if declared_zone else self.extent.line_pos[0]
                    v_start_c = (declared_zone.extent.char_pos[1] + 1) if declared_zone else self.extent.char_pos[0]
                    var_extent = Line(v_start_l, self.extent.line_pos[1], v_start_c, self.extent.char_pos[1])
                else:
                    var_extent = self.extent

                with CS(REF_NO_REF):
                    if G.OVERRIDE_FORCE_AST_DEBUG:
                        self.ast_debug(CS, ast_id_route)
                    tag_ref = self.tag(
                        CS,
                        ast_id_route,
                        var_extent,
                        ast_name=safe_name,
                        ast_type=main_t_id,
                    )
                if not is_param and safe_name:
                    with CS(REF_POS):
                        CS.store(m_symbol_def.set(
                            None,
                            CS.gp.VID,
                            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                            tag_ref or getattr(self, "tag_ref", 0),
                            self.ast_ref,
                            safe_name,
                            main_t_id,
                            var_extent.line_pos[0],
                            var_extent.line_pos[1],
                        ))
                if is_type_def and hasattr(self, "ast_ref"):
                    for ts in final_type:
                        if ts.ref_type in (TSRef.Route_Ref, TSRef.AST_Ref) and ts.ref is not None:
                            type_ast_ref = CS.ref(m_ast.ast_id, *ts.ref) if ts.ref_type == TSRef.Route_Ref else ts.ref
                            with CS(REF_POS):
                                CS.store(m_symbol_ref.set(
                                    None,
                                    CS.gp.VID,
                                    ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                                    tag_ref or getattr(self, "tag_ref", 0),
                                    type_ast_ref,
                                    int(SymbolRole.TypeUsage),
                                    var_extent.line_pos[0],
                                    var_extent.char_pos[0],
                                ))
                            break
def resolve_cppro_scopes(children: list[Any]) -> None:
    """Resolve preprocessor conditional branch bounds linking to terminating endif."""
    cpp_stack: list[list[Any]] = []
    for item in children:
        if isinstance(item, (CPPro_if, CPPro_ifdef, CPPro_ifndef)) and not isinstance(
            item, (CPPro_elif, CPPro_elifdef, CPPro_elifndef)
        ):
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
                    if getattr(branch, "endif", None) is None or branch.endif.line_pos[0] == 0:
                        branch.endif = item.extent


class Zone:
    """Scope boundary manager for token dispatch, nested AST tracking, and spatial extents."""

    def __init__(
        self,
        zone_type: int = Zone_Type.Full_File,
        cursors_array: Any = None,
        tokens_array: Any = None,
    ) -> None:
        self.children: list[Ast] = []
        self.zone_type = zone_type
        self.extent = Line(0, 0)
        self.preset_extents: deque = deque()
        self.tokens_array = tokens_array
        self.paren_depth = 1 if zone_type == Zone_Type.Function_Args else 0
        self.brace_depth = 1 if zone_type in _BRACE_ZONE_TYPES else 0
        self.bracket_depth = 1 if zone_type == Zone_Type.Array_Content else 0
        self.completed = False
        self.ast_type = C_Type
        self.end_mode = End_Mode.Auto
        self.compound_stmt: Ast_CompoundStmt | None = None
        self.compound_ref: Any = None

        if cursors_array is None:
            cursors_array = ()

        if cursors_array and isinstance(cursors_array, (tuple, list)):
            def _get_cur_file_name(c: Any) -> str:
                f = getattr(c, "_file_name", None)
                if f is None:
                    file_obj = getattr(getattr(c, "extent", None), "start", None)
                    f_obj = getattr(file_obj, "file", None)
                    f = f_obj.name if f_obj else ""
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
            self.compound_stmt = Ast_CompoundStmt(self.extent, end_mode=End_Mode.Extent)
            if cursors_array:
                self.compound_stmt.cursor = cursors_array[0]
            for cursor in cursors_array:
                self.extent.grow(get_cursor_line(cursor))
                try:
                    for child in cursor.get_children():
                        child_ext = Line(get_cursor_line(child))
                        if tokens_array is not None:
                            encapsulate_trailing_delimiter(tokens_array, child_ext)
                        if child_ext.line_pos[0] > 0:
                            self.preset_extents.append((child_ext, child))
                except Exception:
                    pass

        elif zone_type == Zone_Type.Full_File:
            pe_list = []
            for cursor in cursors_array:
                temp_ext = Line(get_cursor_line(cursor))
                if tokens_array is not None:
                    encapsulate_trailing_delimiter(tokens_array, temp_ext)
                if temp_ext.line_pos[0] > 0:
                    self.extent.grow(temp_ext)
                    pe_list.append((temp_ext, cursor))
            pe_list.sort(key=lambda item: (item[0].line_pos[0], item[0].char_pos[0], -item[0].line_pos[1], -item[0].char_pos[1]))
            self.preset_extents = deque(pe_list)

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

        else:
            for cursor in cursors_array:
                temp_ext = Line(get_cursor_line(cursor))
                if tokens_array is not None:
                    encapsulate_trailing_delimiter(tokens_array, temp_ext)
                self.extent.grow(temp_ext)
                self.preset_extents.append((temp_ext, cursor))

        if zone_type in (Zone_Type.Enum_Content, Zone_Type.Function_Args):
            self.end_mode = End_Mode.Comma

    def _create_child_node(self, cursor: Any, extent: Line) -> Ast:
        sp = safe_cursor_spelling(cursor)
        if self.zone_type in (Zone_Type.Function_Args, Zone_Type.Enum_Content):
            node = self.ast_type(extent, End_Mode.Comma, cursor=cursor)
            node.cursor = cursor
            node.tokens_array = self.tokens_array
            if sp:
                node.name = sp
            return node
        k = getattr(cursor, "kind", None) if cursor is not None else None
        if k in (cc.CursorKind.DECL_STMT, cc.CursorKind.VAR_DECL, cc.CursorKind.FIELD_DECL, cc.CursorKind.PARM_DECL):
            return C_Type(extent, End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.IF_STMT:
            return Ast_IfStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.SWITCH_STMT:
            return Ast_SwitchStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.CASE_STMT:
            return Ast_CaseStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.DEFAULT_STMT:
            return Ast_DefaultStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.WHILE_STMT:
            return Ast_WhileStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.DO_STMT:
            return Ast_DoStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.FOR_STMT:
            return Ast_ForStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.RETURN_STMT:
            return Ast_ReturnStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.BREAK_STMT:
            return Ast_BreakStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.CONTINUE_STMT:
            return Ast_ContinueStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.GOTO_STMT:
            return Ast_GotoStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.LABEL_STMT:
            return Ast_LabelStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif (
            k in (getattr(cc.CursorKind, "ASM_STMT", None), getattr(cc.CursorKind, "MS_ASM_STMT", None))
            or (k and getattr(k, "name", "").endswith("ASM_STMT"))
        ):
            return Ast_AsmStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.CALL_EXPR:
            return Ast_CallExpr(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.MEMBER_REF_EXPR:
            return Ast_MemberRefExpr(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.DECL_REF_EXPR:
            return Ast_DeclRefExpr(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.BINARY_OPERATOR:
            return Ast_BinaryOperator(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.UNARY_OPERATOR:
            return Ast_UnaryOperator(extent, end_mode=End_Mode.Extent, cursor=cursor)
        elif k == cc.CursorKind.COMPOUND_STMT:
            return Ast_CompoundStmt(extent, end_mode=End_Mode.Extent, cursor=cursor)
        node = self.ast_type(extent, End_Mode.Extent, cursor=cursor)
        if sp:
            node.name = sp
        return node

    def check_exec(self, token: Any, cursor: Any, ast_kind: int) -> bool:
        """Check if token is part of this Zone and execute/absorb. Return True on exec."""
        if self.completed:
            return False

        tline = token.line
        tspelling = token.spelling_str

        # Macro Optimization: Zone_Type.Full_File fast child dispatch
        if self.zone_type == Zone_Type.Full_File and self.children:
            last_child = self.children[-1]
            if last_child.within_range(token, ast_kind):
                last_child.exec_filter(token, cursor, ast_kind)
                return True
            if ast_kind == AST_KIND.punctuation and tspelling == "*":
                last_child.extent.grow(tline)
                last_child.need_processing = True
                last_child.exec_filter(token, cursor, ast_kind)
                return True

        if ast_kind == AST_KIND.punctuation:
            last_ch = self.children[-1] if self.children else None
            ch_zones = last_ch.zones if last_ch else ()
            has_active_child_zones = bool(ch_zones and not ch_zones[-1].completed)
            child_paren = last_ch.paren_depth if last_ch else 0

            if self.zone_type == Zone_Type.Function_Args:
                if tspelling == "(":
                    if not (last_ch and last_ch.within_range(token, ast_kind)):
                        self.paren_depth += 1
                elif tspelling == ")":
                    if not has_active_child_zones and child_paren <= 0 and self.paren_depth <= 1:
                        self.paren_depth = 0
                        self.extent.grow(tline)
                        self.preset_extents.clear()
                        self.completed = True
                        if last_ch and last_ch.need_processing:
                            last_ch.need_processing = False
                        return True
                    elif not has_active_child_zones and child_paren <= 0:
                        self.paren_depth -= 1
            elif self.zone_type == Zone_Type.Enum_Content:
                if tspelling == "{":
                    if not (last_ch and last_ch.within_range(token, ast_kind)):
                        self.brace_depth += 1
                elif tspelling == "}":
                    if not has_active_child_zones and self.brace_depth <= 1:
                        self.brace_depth = 0
                        self.extent.grow(tline)
                        self.preset_extents.clear()
                        self.completed = True
                        if last_ch and last_ch.need_processing:
                            last_ch.need_processing = False
                        return True
                    elif not has_active_child_zones:
                        self.brace_depth -= 1
            elif self.zone_type in _BRACE_ZONE_TYPES:
                if tspelling == "}":
                    if not has_active_child_zones and self.brace_depth <= 1:
                        self.brace_depth = 0
                        self.extent.grow(tline)
                        self.preset_extents.clear()
                        self.completed = True
                        if last_ch and last_ch.need_processing:
                            last_ch.need_processing = False
                        return True
                    elif not has_active_child_zones:
                        self.brace_depth -= 1
            elif self.zone_type == Zone_Type.Array_Content:
                if tspelling == "[":
                    if not (last_ch and last_ch.within_range(token, ast_kind)):
                        self.bracket_depth += 1
                elif tspelling == "]":
                    if not has_active_child_zones and self.bracket_depth <= 1:
                        self.bracket_depth = 0
                        self.extent.grow(tline)
                        self.preset_extents.clear()
                        self.completed = True
                        if last_ch and last_ch.need_processing:
                            last_ch.need_processing = False
                        return True
                    elif not has_active_child_zones:
                        self.bracket_depth -= 1
            elif self.zone_type == Zone_Type.Initializer_Expr:
                if tspelling == "{":
                    self.brace_depth += 1
                elif tspelling == "}":
                    self.brace_depth = max(0, self.brace_depth - 1)
                elif tspelling == "(":
                    self.paren_depth += 1
                elif tspelling == ")":
                    self.paren_depth = max(0, self.paren_depth - 1)
                elif tspelling == "[":
                    self.bracket_depth += 1
                elif tspelling == "]":
                    self.bracket_depth = max(0, self.bracket_depth - 1)

        if self.children:
            last_child = self.children[-1]
            if last_child.within_range(token, ast_kind):
                last_child.exec_filter(token, cursor, ast_kind)
                return True
            if ast_kind == AST_KIND.punctuation:
                if tspelling == "*":
                    last_child.extent.grow(tline)
                    last_child.need_processing = True
                    last_child.exec_filter(token, cursor, ast_kind)
                    return True

        if ast_kind == AST_KIND.comment:
            self.children.append(Ast_Comment(tline, tspelling))
            return True

        if ast_kind == AST_KIND.punctuation:
            if self.zone_type == Zone_Type.Function_Args:
                if tspelling == "(":
                    self.paren_depth += 1
                elif tspelling == ")":
                    self.paren_depth -= 1
                    if self.paren_depth <= 0:
                        self.extent.grow(tline)
                        self.preset_extents.clear()
                        self.completed = True
                        if self.children and self.children[-1].need_processing:
                            self.children[-1].need_processing = False
                        return True
            elif self.zone_type == Zone_Type.Initializer_Expr:
                if tspelling == ";":
                    if self.brace_depth <= 0:
                        self.completed = True
                        self.preset_extents.clear()
                        return False
                elif tspelling == ",":
                    if self.brace_depth <= 0 and self.paren_depth <= 0 and self.bracket_depth <= 0:
                        self.completed = True
                        self.preset_extents.clear()
                        return False
            elif self.zone_type == Zone_Type.Enum_Equal:
                if tspelling in (",", "}"):
                    self.completed = True
                    self.preset_extents.clear()
            elif tspelling == "{":
                self.brace_depth += 1
            elif tspelling == "}":
                self.brace_depth -= 1
                if self.zone_type in _BRACE_ZONE_TYPES and self.brace_depth <= 0:
                    self.extent.grow(tline)
                    self.preset_extents.clear()
                    self.completed = True
                    if self.children:
                        self.children[-1].extent.grow(tline)
                    return True

        if (
            (not self.extent.is_inside(tline))
            and (self.zone_type != Zone_Type.Full_File)
            and (self.zone_type not in _BRACE_ZONE_TYPES)
            and (self.zone_type != Zone_Type.Function_Args)
        ):
            if not (self.children and self.children[-1].need_processing):
                if self.zone_type == Zone_Type.Initializer_Expr:
                    self.completed = True
                return False

        if self.zone_type in _BRACE_ZONE_TYPES or self.zone_type == Zone_Type.Function_Args:
            self.extent.grow(tline)

        if ast_kind == AST_KIND.punctuation:
            if tspelling in _PUNCT_IGNORED:
                if self.children:
                    self.children[-1].extent.grow(tline)
                return True

            if tspelling == "#":
                node = CPPro(tline)
                if cursor.kind == cc.CursorKind.INCLUSION_DIRECTIVE:
                    try:
                        inc_file = cursor.get_included_file()
                        if inc_file is not None and inc_file.name:
                            node.a_include = inc_file.name
                    except Exception:
                        pass
                self.children.append(node)
                return True
            if tspelling == "." and self.zone_type == Zone_Type.Full_File:
                self.children.append(Ast_ASM_Directive(tline, "."))
                return True

        # Check preset_extents
        if self.preset_extents:
            while (
                self.preset_extents
                and (
                    self.preset_extents[0][0]
                    if isinstance(self.preset_extents[0], tuple)
                    else self.preset_extents[0]
                ).line_pos[1]
                < tline.line_pos[0]
            ):
                self.preset_extents.popleft()

            for i, p_item in enumerate(self.preset_extents):
                p_extent = p_item[0] if isinstance(p_item, tuple) else p_item
                p_cursor = p_item[1] if isinstance(p_item, tuple) else cursor
                if p_extent.line_pos[0] > tline.line_pos[1]:
                    break
                if p_extent.is_inside(tline):
                    node = self._create_child_node(p_cursor, p_extent)
                    self.children.append(node)
                    self.children[-1].exec_filter(token, cursor, ast_kind)
                    del self.preset_extents[i]
                    return True

        # Check statement keywords
        if ast_kind == AST_KIND.keyword:
            match tspelling:
                case "if":
                    self.children.append(Ast_IfStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "return":
                    self.children.append(Ast_ReturnStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "while":
                    self.children.append(Ast_WhileStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "for":
                    self.children.append(Ast_ForStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "do":
                    self.children.append(Ast_DoStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "switch":
                    self.children.append(Ast_SwitchStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "case":
                    self.children.append(Ast_CaseStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "default":
                    self.children.append(Ast_DefaultStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "break":
                    self.children.append(Ast_BreakStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "continue":
                    self.children.append(Ast_ContinueStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "goto":
                    self.children.append(Ast_GotoStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "asm" | "__asm__" | "__asm":
                    self.children.append(Ast_AsmStmt(tline))
                    self.children[-1].exec_keyword(token, cursor)
                    return True
                case "_Static_assert" | "static_assert":
                    return True

        self.children.append(self.ast_type(tline, self.end_mode))
        self.children[-1].exec_filter(token, cursor, ast_kind)
        return True

    def gen_lined_dict(self) -> None:
        """Bypass dead line indexing to conserve memory and execution time."""
        pass

    def resolve_cppro_scopes(self) -> None:
        """Resolve preprocessor conditional branch bounds linking to terminating endif."""
        resolve_cppro_scopes(self.children)

    def extract(self, CS: Any, create_tags: bool = True) -> None:
        """Stage all child AST nodes into ChangeSet operations."""
        if self.zone_type == Zone_Type.Function_Args and len(self.children) == 1:
            ch = self.children[0]
            if isinstance(ch, C_Type):
                if ch.content:
                    ch.swap_out()
                is_void = any(
                    tok.type == ASTT.C_void
                    for ts in ch.typedata
                    for tok in ts.content
                ) and not ch.name
                if is_void:
                    return
        for item in self.children:
            if isinstance(item, (C_Type, AST_Initializer)):
                item.extract(CS, create_tag=create_tags)
            else:
                with CS(REF_NO_REF):
                    try:
                        item.extract(CS, create_tag=create_tags)
                    except TypeError:
                        item.extract(CS)

        if self.zone_type == Zone_Type.Initializer_Expr:
            for item in self.children:
                if isinstance(item, AST_Initializer) and getattr(item, "ast_ref", None) is not None:
                    self.initializer_ref = item.ast_ref
                    break

        if self.zone_type == Zone_Type.Compound_Stmt and self.compound_stmt is not None:
            self.compound_stmt.extent = Line(self.extent)
            if self.compound_stmt.cursor is not None:
                collect_cursor_used_types(self.compound_stmt.cursor, self.compound_stmt.used_types)

            container_items = []
            stmt_priority = 1
            for child in self.children:
                c_ref = getattr(child, "ast_ref", None)
                c_type = getattr(child, "type_id", getattr(child, "ast_type", ASTT.C_CompoundStmt))
                if c_ref is not None:
                    container_items.append((stmt_priority, int(c_type), c_ref))
                    stmt_priority += 1

            for t_name in sorted(self.compound_stmt.used_types):
                safe_t = str(t_name)[:255]
                with CS(REF_POS):
                    CS.store(m_ast.view(((m_ast.ast_id,),), None, safe_t, ASTT.C_structnotbind))
                    type_route = CS.get_route_parse()
                    type_ast_ref = CS.ref(m_ast.ast_id, *type_route)
                container_items.append((stmt_priority, int(ASTT.C_TypeRef), type_ast_ref))
                stmt_priority += 1

            total_containers = len(container_items)
            if total_containers == 0:
                with CS(REF_POS):
                    CS.store(m_ast.view(((m_ast.ast_id,),), None, "{}", ASTT.C_CompoundStmt))
                    ast_id_route = CS.get_route_parse()
            else:
                container_args = []
                for priority, t_code, ref_ast_id in container_items:
                    container_args.extend((None, priority, t_code, ref_ast_id))
                with CS(REF_POS):
                    CS.store(m_ast.view(
                        ((m_ast.ast_id, m_ast_container.ast_id, total_containers),),
                        None,
                        "{}",
                        ASTT.C_CompoundStmt,
                        *container_args,
                    ))
                    ast_id_route = CS.get_route_parse()

            self.compound_ref = CS.ref(m_ast.ast_id, *ast_id_route)
            self.compound_stmt.ast_ref = self.compound_ref
            if create_tags:
                with CS(REF_NO_REF):
                    if G.OVERRIDE_FORCE_AST_DEBUG:
                        self.compound_stmt.ast_debug(CS, ast_id_route)
                    self.compound_stmt.tag(CS, ast_id_route, self.extent, ast_name="{}", ast_type=ASTT.C_CompoundStmt)


class SemanticPartitioner:
    """Partitions tokens and annotated cursors into structured AST trees."""

    def __init__(self, token_stream: Any, rawfile: tuple[str, ...]) -> None:
        self.stream = token_stream
        self.rawfile = rawfile
        parsed_tu = getattr(token_stream, "parsed_tu", None)
        fullfilename = getattr(token_stream, "fullfilename", "")
        top_cursors = get_top_level_cursors(parsed_tu, fullfilename)
        self.main_zone = Zone(Zone_Type.Full_File, top_cursors, tokens_array=getattr(token_stream, "tokens_array", None))

    def process(self, CS: Any) -> Zone:
        check_exec = self.main_zone.check_exec
        tokens_array = self.stream.tokens_array
        cursors_array = self.stream.cursors_array

        for token, cursor in zip(tokens_array, cursors_array):
            check_exec(token, cursor, token.ast_kind)

        self.main_zone.gen_lined_dict()
        self.main_zone.resolve_cppro_scopes()
        return self.main_zone
