"""parser/c_ast/ast_nodes.py - Intermediate AST Representation & Language Nodes.

Defines lightweight AST classes for C constructs, preprocessor directives, assembly
nodes, and type qualifiers. Implements coordinate math and schema bounds.
"""
from __future__ import annotations

import json
from enum import Flag, IntEnum
from typing import Any
from pathlib import Path

from core.globalstuff import G, ASTT, OP_REF, REF_ROOT, REF_POS, REF_NO_REF, REF_FILE, SymbolRole, compute_code_hash, normalize_repo_path
from core.DBLayout import (
    m_ast,
    m_ast_container,
    m_ast_include,
    m_ast_debug,
    m_file,
    m_file_name,
    m_tag,
    m_tag_code,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
    m_moved_tag,
    m_symbol_def,
    m_symbol_ref,
)
from parser.c_ast.tokenizer import AST_KIND, Line, get_cursor_line
from parser.c_ast.tag_tracker import match_prior_tag_transition
import clang.cindex as cc


class CQual(Flag):
    """Bit-flag tracking C type qualifiers."""
    Empty = 0
    const = 1       # ASTT.C_Qconst (15)
    volatile = 2    # ASTT.C_Qvolatile (16)
    restrict = 4    # ASTT.C_Qrestrict (17)
    _Atomic = 8     # ASTT.C_Q_Atomic (18)

    def output_ast(self) -> tuple[int, ...] | None:
        """Return tuple of ASTT enum values for enabled qualifier bits."""
        res = []
        if self.value & 1:
            res.append(ASTT.C_Qconst)
        if self.value & 2:
            res.append(ASTT.C_Qvolatile)
        if self.value & 4:
            res.append(ASTT.C_Qrestrict)
        if self.value & 8:
            res.append(ASTT.C_Q_Atomic)
        return tuple(res) if res else None


class End_Mode(IntEnum):
    No_Check = 0
    Auto = 1
    Semicolon = 2
    Comma = 3
    Extent = 4


def get_notbind_type(ast_type: int) -> int:
    """Map bound AST declaration type to unbound forward reference type."""
    match ast_type:
        case ASTT.C_struct | ASTT.C_structdecl:
            return ASTT.C_structnotbind
        case ASTT.C_union | ASTT.C_uniondecl:
            return ASTT.C_unionnotbind
        case ASTT.C_enum | ASTT.C_enumdecl:
            return ASTT.C_enumnotbind
        case ASTT.C_functionproto | ASTT.C_functionprotodecl:
            return ASTT.C_functionprotnotbind
        case _:
            return ast_type


class Ast:
    """Base intermediate AST node offering extraction and tagging."""
    zones: tuple[Any, ...] = ()
    paren_depth: int = 0
    brace_depth: int = 0
    bracket_depth: int = 0
    need_processing: bool = True

    def __init__(self, extent: Line | None = None, end_mode: int = End_Mode.Auto) -> None:
        self.extent = extent if extent is not None else Line(0, 0)
        self.end_mode = end_mode
        self.need_processing = True
        self.name: str = ""
        self.type_id: int = 0
        self.endif: Line | None = None

    def within_range(self, token: Any, ast_kind: int) -> bool:
        """Evaluate boundary enclosure against End_Mode."""
        if not self.need_processing:
            return False
        tline = token.line
        tspelling = token.spelling_str
        match self.end_mode:
            case End_Mode.No_Check:
                self.extent.grow(tline)
                return True
            case End_Mode.Auto | End_Mode.Semicolon:
                if ast_kind != 0:  # AST_KIND.punctuation
                    self.extent.grow(tline)
                    return True
                if tspelling == ";":
                    self.extent.grow(tline)
                    self.need_processing = False
                    return False
            case End_Mode.Comma:
                if ast_kind != 0:
                    self.extent.grow(tline)
                    return True
                if tspelling == ",":
                    self.extent.grow(tline)
                    self.need_processing = False
                    return False
            case End_Mode.Extent:
                if not self.extent.is_inside(tline):
                    self.need_processing = False
                    return False
        self.extent.grow(tline)
        return True

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        pass

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        pass

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        pass

    def exec_literal(self, token: Any, cursor: Any) -> None:
        pass

    def exec_comment(self, token: Any, cursor: Any) -> None:
        pass

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

    def extract_1arg(self, CS: Any, type_id: int, name: int | str, extent: Line | None = None, create_tag: bool = True) -> None:
        """Stage single-argument AST symbol into CS with strict schema length bounding."""
        safe_name = str(name)[:255]
        with CS(REF_POS):
            CS.store(m_ast.view(((m_ast.ast_id,),), None, safe_name, type_id))
            ast_id_route = CS.get_route_parse()
        self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)

        if create_tag:
            with CS(REF_NO_REF):
                if G.OVERRIDE_FORCE_AST_DEBUG:
                    self.ast_debug(CS, ast_id_route)
                tag_ref = self.tag(CS, ast_id_route, extent or self.extent, ast_name=safe_name, ast_type=type_id)
                if type_id in {ASTT.ASM_Label, ASTT.ASM_Macro, ASTT.C_LabelStmt, ASTT.CPPro_define, ASTT.CPPro_define_macro} and safe_name:
                    if hasattr(CS, "symbol_dict"):
                        CS.symbol_dict[(safe_name, type_id)] = ast_id_route[1]
                        if type_id in (ASTT.CPPro_define, ASTT.CPPro_define_macro):
                            CS.symbol_dict[(safe_name, ASTT.CPPro_define)] = ast_id_route[1]
                            CS.symbol_dict[(safe_name, ASTT.CPPro_define_macro)] = ast_id_route[1]
                    ext = extent or self.extent
                    vid = getattr(CS.gp, "VID", 1) if getattr(CS, "gp", None) else getattr(CS, "VID", 1)
                    with CS(REF_POS):
                        CS.store(m_symbol_def.set(
                            None,
                            vid,
                            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                            tag_ref or getattr(self, "tag_ref", 0),
                            self.ast_ref,
                            safe_name,
                            type_id,
                            ext.line_pos[0],
                            ext.line_pos[1],
                        ))

    def ast_debug(self, CS: Any, ast_id_route: Any) -> None:
        """Stage JSON debug serialization in m_ast_debug."""
        CS.store(m_ast_debug.set(
            CS.ref(m_ast.ast_id, *ast_id_route),
            json.dumps({"name": getattr(self, "name", ""), "extent": str(self.extent)}),
        ))

    def tag(
        self,
        CS: Any,
        ast_id_route: Any,
        extent: Line | None = None,
        ast_name: str | None = None,
        ast_type: Any = None,
    ) -> None:
        """Stage or recycle code tag and spatial coordinate bridges."""
        ext = extent if extent is not None else self.extent
        if not ext.code:
            parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
            if parser_obj and hasattr(parser_obj, "rawfile"):
                ext.cc(parser_obj.rawfile)

        code_hash = compute_code_hash(ext.code)

        # 1. Tier 1: Check exact code hash match against prior version tags
        if getattr(CS, "prior_tags", None) and ext.code != "":
            lookup = getattr(CS, "prior_tags_map", None)
            if lookup is not None:
                tag_list = lookup.get(code_hash)
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
                            ext.line_pos[0], ext.line_pos[1],
                            ext.char_pos[0], ext.char_pos[1],
                        ))
                        self.tag_ref = tag_id
                        if hasattr(CS, "last_tag_ref"):
                            CS.last_tag_ref = tag_id
                        self._flush_pending_symbol_refs(CS, tag_id)
                        return tag_id

        # 2. Check evolved prior tag match via tag_tracker
        cur_ast_name = ast_name or getattr(self, "name", None)
        cur_ast_type = ast_type if ast_type is not None else getattr(self, "type_id", None)
        s_tag_id = match_prior_tag_transition(CS, ext, cur_ast_name, cur_ast_type)

        ast_ref = CS.ref(m_ast.ast_id, *ast_id_route) if not (isinstance(ast_id_route, tuple) and len(ast_id_route) == 3 and ast_id_route[1] == OP_REF) else ast_id_route

        # 3. Rule 12 Invariant: m_tag.set MUST be first inside with CS(REF_POS)
        with CS(REF_POS):
            CS.store(m_tag.set(None, CS.gp.VID, 0, code_hash, ast_ref, 0, 0))
            tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
            CS.store(m_tag_code.get_set(code_hash, ext.code))
            if s_tag_id is not None:
                CS.store(m_moved_tag.set(s_tag_id, tag_ref))

        # 4. Stage File-to-Tag Bridge (source coordinates: 1-based, inclusive start, exclusive end)
        CS.store(m_bridge_tag.set(
            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
            tag_ref,
            ext.line_pos[0], ext.line_pos[1],
            ext.char_pos[0], ext.char_pos[1],
        ))

        # 5. Stage Intra-Tag Spatial Mapping & Bridge Map (with deduplication)
        self.map_ast(CS, ast_ref, tag_ref, ext)
        self.tag_ref = tag_ref
        if hasattr(CS, "last_tag_ref"):
            CS.last_tag_ref = tag_ref
        self._flush_pending_symbol_refs(CS, tag_ref)
        return tag_ref

    def _flush_pending_symbol_refs(self, CS: Any, tag_ref: Any) -> None:
        """Flush pending symbol references with the active tag reference."""
        if getattr(CS, "pending_symbol_refs", None):
            seen: set[tuple[Any, int, int, int]] = set()
            for ref_ast_id, role, line, col in CS.pending_symbol_refs:
                key = (ref_ast_id, role, line, col)
                if key in seen:
                    continue
                seen.add(key)
                with CS(REF_POS):
                    CS.store(m_symbol_ref.set(
                        None,
                        CS.gp.VID,
                        ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                        tag_ref,
                        ref_ast_id,
                        int(role),
                        line,
                        col,
                    ))
            CS.pending_symbol_refs.clear()

    def map_ast(
        self,
        CS: Any,
        ast_id_route: Any,
        tag_route: Any,
        extent: Line | None = None,
    ) -> None:
        """Stage intra-tag spatial coordinate map and bridge map."""
        ext = extent if extent is not None else self.extent
        line_s = 1
        char_s = 1

        if self.endif and self.endif.line_pos[0] > 0:
            end_line = self.endif.line_pos[1]
            line_e = max(1, end_line - ext.line_pos[0] + 1)
            char_e = self.endif.char_pos[1] if self.endif.char_pos[1] > 0 else ext.char_pos[1]
        else:
            line_e = max(1, ext.line_pos[1] - ext.line_pos[0] + 1)
            char_e = ext.char_pos[1]

        ast_target = (
            ast_id_route
            if (type(ast_id_route) is tuple and len(ast_id_route) == 3 and ast_id_route[1] == OP_REF) or type(ast_id_route) is int
            else CS.ref(m_ast.ast_id, *ast_id_route)
        )
        tag_target = (
            tag_route
            if (type(tag_route) is tuple and len(tag_route) == 3 and tag_route[1] == OP_REF) or type(tag_route) is int
            else CS.ref(m_tag.tag_id, *tag_route)
        )

        CS.store(m_map_ast.set(
            tag_target,
            line_s, char_s,
            line_e, char_e,
            ast_target,
        ))

        # Invariant J: Deduplicate bridge map entries via CS.register_bridge_map
        if not hasattr(CS, "register_bridge_map") or CS.register_bridge_map(tag_target, tag_target):
            CS.store(m_bridge_map.set(tag_target, tag_target))

    def extract(self, CS: Any) -> None:
        """Default fallback extraction."""
        with CS(REF_POS):
            CS.store(m_ast.view(
                ((m_ast.ast_id,),),
                None,
                f"AST{len(CS.cs)}"[:255],
                0,
            ))
            ast_id_route = CS.get_route_parse()
        with CS(REF_NO_REF):
            self.tag(CS, ast_id_route, self.extent)


# --- Comments ---

class Ast_Comment(Ast):
    """C Source Comment (// ... or /* ... */). Truncates to 255 chars for m_ast.name."""

    def __init__(self, extent: Line | None = None, comment_text: str = "") -> None:
        super().__init__(extent, End_Mode.Extent)
        self.type_id = ASTT.C_Comment
        self.comment_text = comment_text

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        name = self.comment_text
        if not name:
            parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
            if parser_obj and hasattr(parser_obj, "rawfile"):
                self.extent.cc(parser_obj.rawfile)
            name = self.extent.code
        self.extract_1arg(CS, ASTT.C_Comment, name[:255], self.extent, create_tag=create_tag)


class Ast_ASM_Comment(Ast):
    """Assembly Source Comment."""

    def __init__(self, extent: Line | None = None) -> None:
        super().__init__(extent, End_Mode.Extent)
        self.type_id = ASTT.ASM_Comment

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
        if parser_obj and hasattr(parser_obj, "rawfile"):
            self.extent.cc(parser_obj.rawfile)
        self.extract_1arg(CS, ASTT.ASM_Comment, self.extent.code[:255], self.extent, create_tag=create_tag)


class Ast_Keyword(Ast):
    """Preserved statement keyword placeholder."""
    pass


# --- Assembly Directives and Instructions ---

class Ast_ASM_Directive(Ast):
    """Assembly Directive (.section, .align, .globl)."""

    def __init__(self, extent: Line | None = None, payload: str = "") -> None:
        super().__init__(extent, End_Mode.No_Check)
        self.type_id = ASTT.ASM_Directive
        self.payload = payload

    def extract(self, CS: Any) -> None:
        parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
        if parser_obj and hasattr(parser_obj, "rawfile"):
            self.extent.cc(parser_obj.rawfile)
        name = self.payload or self.extent.code
        self.extract_1arg(CS, ASTT.ASM_Directive, name[:255], self.extent)


class Ast_ASM_Macro(Ast):
    """Assembly Macro Definition (.macro ... .endm)."""

    def __init__(self, extent: Line | None = None, name: str = "") -> None:
        super().__init__(extent, End_Mode.No_Check)
        self.type_id = ASTT.ASM_Macro
        self.name = name

    def extract(self, CS: Any) -> None:
        parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
        if parser_obj and hasattr(parser_obj, "rawfile"):
            self.extent.cc(parser_obj.rawfile)
        self.extract_1arg(CS, ASTT.ASM_Macro, self.name[:255], self.extent)


class Ast_ASM_Instruction(Ast):
    """Assembly Mnemonic Instruction with operands."""

    def __init__(self, extent: Line | None = None, mnemonic: str = "") -> None:
        super().__init__(extent, End_Mode.No_Check)
        self.type_id = ASTT.ASM_Instruction
        self.mnemonic = mnemonic

    def extract(self, CS: Any) -> None:
        parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
        if parser_obj and hasattr(parser_obj, "rawfile"):
            self.extent.cc(parser_obj.rawfile)
        name = self.mnemonic or self.extent.code
        self.extract_1arg(CS, ASTT.ASM_Instruction, name[:255], self.extent)


class Ast_ASM_Label(Ast):
    """Assembly Jump / Function Label."""

    def __init__(self, extent: Line | None = None, label: str = "") -> None:
        super().__init__(extent, End_Mode.No_Check)
        self.type_id = ASTT.ASM_Label
        self.label = label

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.ASM_Label, self.label[:255], self.extent)


# --- C Preprocessor Subsystem (CPPro) ---

class CPPro(Ast):
    """Root C Preprocessor Directive."""

    def __init__(self, extent: Line | None = None) -> None:
        super().__init__(extent, End_Mode.No_Check)
        self.highlight = Line()
        self.flipped = False
        self.type_id = 0

    def ccpro_start_flip(self, target_class: type, cline: Line) -> None:
        self.__class__ = target_class
        self.extent.grow(cline)
        self.type_id = getattr(target_class, "type_id", 0)
        self.flipped = True
        self.init_subclass()

    def init_subclass(self) -> None:
        pass

    def within_range(self, token: Any, ast_kind: int) -> bool:
        if not self.need_processing:
            return False
        if ast_kind == AST_KIND.comment:
            return False
        t_line = token.line.line_pos[0]
        cur_line = self.extent.line_pos[1]
        if t_line <= cur_line:
            self.extent.grow(token.line)
            return True
        # Check backslash continuation from rawfile
        rawfile = getattr(G, "CURRENT_RAWFILE", None)
        if rawfile:
            all_continued = True
            for l in range(cur_line - 1, t_line - 1):
                if not (0 <= l < len(rawfile) and rawfile[l].rstrip().endswith("\\")):
                    all_continued = False
                    break
            if all_continued:
                self.extent.grow(token.line)
                return True
        self.need_processing = False
        return False

    def exec_filter(self, token: Any, cursor: Any, kind: int) -> None:
        if not self.flipped:
            if kind in (AST_KIND.identifier, AST_KIND.keyword):
                self._flip_directive(token, cursor)
                return
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

    def _flip_directive(self, token: Any, cursor: Any) -> None:
        spelling = token.spelling_str
        match spelling:
            case "if":
                self.ccpro_start_flip(CPPro_if, token.line)
            case "elif":
                self.ccpro_start_flip(CPPro_elif, token.line)
            case "else":
                self.ccpro_start_flip(CPPro_else, token.line)
                self.need_processing = False
            case "endif":
                self.ccpro_start_flip(CPPro_endif, token.line)
                self.need_processing = False
            case "ifdef":
                self.ccpro_start_flip(CPPro_ifdef, token.line)
            case "ifndef":
                self.ccpro_start_flip(CPPro_ifndef, token.line)
            case "elifdef":
                self.ccpro_start_flip(CPPro_elifdef, token.line)
            case "elifndef":
                self.ccpro_start_flip(CPPro_elifndef, token.line)
            case "define":
                self.ccpro_start_flip(CPPro_define, token.line)
            case "undef":
                self.ccpro_start_flip(CPPro_undef, token.line)
            case "include":
                self.ccpro_start_flip(CPPro_include, token.line)
                self._check_inc_cursor(cursor)
            case "line":
                self.ccpro_start_flip(CPPro_line, token.line)
            case "error":
                self.ccpro_start_flip(CPPro_error, token.line)
            case "warning":
                self.ccpro_start_flip(CPPro_warning, token.line)
            case "pragma":
                self.ccpro_start_flip(CPPro_pragma, token.line)
            case "embed":
                self.ccpro_start_flip(CPPro_embed, token.line)
            case "defined":
                self.ccpro_start_flip(CPPro_defined, token.line)

    def _check_inc_cursor(self, cursor: Any) -> None:
        pass

    def exec_comment(self, token: Any, cursor: Any) -> None:
        pass

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        pass

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        pass

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        pass

    def exec_literal(self, token: Any, cursor: Any) -> None:
        pass

    def extract(self, CS: Any) -> None:
        pass


class CPPro_if(CPPro):
    type_id = ASTT.CPPro_if

    def __init__(self, extent: Line, expression: str = "") -> None:
        super().__init__(extent)
        self.expression = expression
        self.type_id = ASTT.CPPro_if

    def init_subclass(self) -> None:
        self.expression = ""
        self.type_id = ASTT.CPPro_if

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        self.expression += token.spelling_str

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        self.expression += token.spelling_str

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        self.expression += token.spelling_str

    def exec_literal(self, token: Any, cursor: Any) -> None:
        self.expression += token.spelling_str

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_if, self.expression[:255], self.extent)


class CPPro_elif(CPPro):
    type_id = ASTT.CPPro_elif

    def __init__(self, extent: Line, expression: str = "") -> None:
        super().__init__(extent)
        self.expression = expression
        self.type_id = ASTT.CPPro_elif

    def init_subclass(self) -> None:
        self.expression = ""
        self.type_id = ASTT.CPPro_elif

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        self.expression += token.spelling_str

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        self.expression += token.spelling_str

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        self.expression += token.spelling_str

    def exec_literal(self, token: Any, cursor: Any) -> None:
        self.expression += token.spelling_str

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_elif, self.expression[:255], self.extent)


class CPPro_else(CPPro):
    type_id = ASTT.CPPro_else

    def __init__(self, extent: Line) -> None:
        super().__init__(extent)
        self.type_id = ASTT.CPPro_else

    def init_subclass(self) -> None:
        self.type_id = ASTT.CPPro_else

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_else, "", self.extent)


class CPPro_endif(CPPro):
    type_id = ASTT.CPPro_endif

    def __init__(self, extent: Line) -> None:
        super().__init__(extent)
        self.type_id = ASTT.CPPro_endif

    def init_subclass(self) -> None:
        self.type_id = ASTT.CPPro_endif

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_endif, "", self.extent)


class CPPro_ifdef(CPPro):
    type_id = ASTT.CPPro_ifdef

    def __init__(self, extent: Line, identifier: str = "") -> None:
        super().__init__(extent)
        self.identifier = identifier
        self.type_id = ASTT.CPPro_ifdef

    def init_subclass(self) -> None:
        self.identifier = ""
        self.type_id = ASTT.CPPro_ifdef

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        if not self.identifier:
            self.identifier = token.spelling_str

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        if not self.identifier:
            self.identifier = token.spelling_str

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_ifdef, self.identifier[:255], self.extent)


class CPPro_ifndef(CPPro):
    type_id = ASTT.CPPro_ifndef

    def __init__(self, extent: Line, identifier: str = "") -> None:
        super().__init__(extent)
        self.identifier = identifier
        self.type_id = ASTT.CPPro_ifndef

    def init_subclass(self) -> None:
        self.identifier = ""
        self.type_id = ASTT.CPPro_ifndef

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        if not self.identifier:
            self.identifier = token.spelling_str

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        if not self.identifier:
            self.identifier = token.spelling_str

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_ifndef, self.identifier[:255], self.extent)


class CPPro_elifdef(CPPro):
    type_id = ASTT.CPPro_elifdef

    def __init__(self, extent: Line, identifier: str = "") -> None:
        super().__init__(extent)
        self.identifier = identifier
        self.type_id = ASTT.CPPro_elifdef

    def init_subclass(self) -> None:
        self.identifier = ""
        self.type_id = ASTT.CPPro_elifdef

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        if not self.identifier:
            self.identifier = token.spelling_str

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_elifdef, self.identifier[:255], self.extent)


class CPPro_elifndef(CPPro):
    type_id = ASTT.CPPro_elifndef

    def __init__(self, extent: Line, identifier: str = "") -> None:
        super().__init__(extent)
        self.identifier = identifier
        self.type_id = ASTT.CPPro_elifndef

    def init_subclass(self) -> None:
        self.identifier = ""
        self.type_id = ASTT.CPPro_elifndef

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        if not self.identifier:
            self.identifier = token.spelling_str

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_elifndef, self.identifier[:255], self.extent)


class CPPro_define(CPPro):
    type_id = ASTT.CPPro_define

    def __init__(self, extent: Line, identifier: str = "", replacement: str | None = None) -> None:
        super().__init__(extent)
        self.identifier = identifier
        self.replacement = replacement
        self.func_args: list[str] = []
        self.id_extent: Line | None = None
        self.in_args = False
        self.has_args = False

    def init_subclass(self) -> None:
        self.identifier = ""
        self.replacement = None
        self.func_args = []
        self.id_extent = None
        self.in_args = False
        self.has_args = False
        self.type_id = ASTT.CPPro_define

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        if not self.identifier:
            self.identifier = token.spelling_str
            self.id_extent = token.line
            return
        if self.in_args:
            self.func_args.append(token.spelling_str)
            return
        if self.replacement is None:
            self.replacement = token.spelling_str
        else:
            self.replacement += " " + token.spelling_str

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        if self.identifier and not self.has_args and self.replacement is None:
            if token.spelling_str == "(" and self.id_extent is not None:
                if (token.line.line_pos[0] == self.id_extent.line_pos[1] and 
                    token.line.char_pos[0] == self.id_extent.char_pos[1]):
                    self.in_args = True
                    self.has_args = True
                    return
        if self.in_args:
            if token.spelling_str == ")":
                self.in_args = False
            return
        if self.replacement is None:
            self.replacement = token.spelling_str
        else:
            self.replacement += " " + token.spelling_str

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        if not self.identifier:
            self.identifier = token.spelling_str
            self.id_extent = token.line
            return
        if self.in_args:
            return
        if self.replacement is None:
            self.replacement = token.spelling_str
        else:
            self.replacement += " " + token.spelling_str

    def exec_literal(self, token: Any, cursor: Any) -> None:
        if self.in_args:
            return
        if self.replacement is None:
            self.replacement = token.spelling_str
        else:
            self.replacement += " " + token.spelling_str

    def extract(self, CS: Any) -> None:
        # Macro continuation backslash expansion (Invariant C)
        parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
        if parser_obj and hasattr(parser_obj, "rawfile"):
            end_line_idx = self.extent.line_pos[1] - 1
            if 0 <= end_line_idx < len(parser_obj.rawfile):
                line_str = parser_obj.rawfile[end_line_idx]
                r_line = line_str.rstrip()
                if r_line.endswith("\\"):
                    end_col = len(r_line) + 1
                    if end_col > self.extent.char_pos[1]:
                        self.extent.char_pos = (self.extent.char_pos[0], end_col)

        type_const = ASTT.CPPro_define if not self.replacement else ASTT.CPPro_define_macro
        self.extract_1arg(CS, type_const, self.identifier[:255], self.extent)


class CPPro_undef(CPPro):
    type_id = ASTT.CPPro_undef

    def __init__(self, extent: Line, identifier: str = "") -> None:
        super().__init__(extent)
        self.identifier = identifier
        self.type_id = ASTT.CPPro_undef

    def init_subclass(self) -> None:
        self.identifier = ""
        self.type_id = ASTT.CPPro_undef

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        if not self.identifier:
            self.identifier = token.spelling_str

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_undef, self.identifier[:255], self.extent)


class CPPro_include(CPPro):
    """Preprocessor Include Directive with Hybrid Resolution & System Header Prefixing."""
    type_id = ASTT.CPPro_include

    def __init__(self, extent: Line, written_include: str = "", resolved_path: str = "") -> None:
        super().__init__(extent)
        self.w_include = written_include
        self.a_include = resolved_path
        self.type_id = ASTT.CPPro_include

    def init_subclass(self) -> None:
        self.w_include = ""
        self.a_include = ""
        self.type_id = ASTT.CPPro_include

    def _check_inc_cursor(self, cursor: Any) -> None:
        if cursor is not None and cursor.kind == cc.CursorKind.INCLUSION_DIRECTIVE:
            try:
                inc_file = cursor.get_included_file()
                if inc_file is not None and inc_file.name:
                    self.a_include = normalize_repo_path(inc_file.name, getattr(G, "CURRENT_PARSING_DIR", None))
            except Exception:
                pass

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        self.w_include += token.spelling_str
        self._check_inc_cursor(cursor)

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        self.w_include += token.spelling_str
        self._check_inc_cursor(cursor)

    def exec_literal(self, token: Any, cursor: Any) -> None:
        self.w_include += token.spelling_str
        self._check_inc_cursor(cursor)

    def exec_keyword(self, token: Any, cursor: Any) -> None:
        self.w_include += token.spelling_str
        self._check_inc_cursor(cursor)

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        base_dir = getattr(CS, "mfdir", None) or getattr(G, "CURRENT_PARSING_DIR", None)
        raw_target = self.a_include if self.a_include else self.w_include
        include_target = normalize_repo_path(raw_target, base_dir)
        safe_target = str(include_target)[:255]

        with CS(REF_POS):
            CS.store(m_file_name.get_set(None, safe_target))
            fnid_route = CS.get_route_parse()

        symbols = getattr(CS, "include_symbols", {}).get(self.extent.line_pos[0], [])
        if symbols:
            if not hasattr(CS, "include_symbol_refs"):
                CS.include_symbol_refs = {}
            flat_container_args = []
            for priority, (sym_name, sym_type) in enumerate(symbols):
                sym_key = (sym_name, sym_type)
                if sym_key not in CS.include_symbol_refs:
                    with CS(REF_POS):
                        CS.store(m_ast.view(((m_ast.ast_id,),), None, sym_name[:255], sym_type))
                        sym_route = CS.get_route_parse()
                    CS.include_symbol_refs[sym_key] = CS.ref(m_ast.ast_id, *sym_route)
                flat_container_args.extend([None, priority, int(sym_type), CS.include_symbol_refs[sym_key]])

            joins = ((m_ast.ast_id, m_ast_include.ast_id, 1), (m_ast.ast_id, m_ast_container.ast_id, len(symbols)))
            with CS(REF_POS):
                CS.store(m_ast.view(
                    joins,
                    None,
                    self.w_include[:255],
                    ASTT.CPPro_include,
                    None,
                    CS.ref(m_file_name.fnid, *fnid_route),
                    *flat_container_args
                ))
                ast_id_route = CS.get_route_parse()
            self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
        else:
            with CS(REF_POS):
                CS.store(m_ast.view(
                    ((m_ast.ast_id, m_ast_include.ast_id, 1),),
                    None,
                    self.w_include[:255],
                    ASTT.CPPro_include,
                    None,
                    CS.ref(m_file_name.fnid, *fnid_route),
                ))
                ast_id_route = CS.get_route_parse()
            self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)

        if create_tag:
            with CS(REF_NO_REF):
                if G.OVERRIDE_FORCE_AST_DEBUG:
                    self.ast_debug(CS, ast_id_route)
                self.tag(CS, ast_id_route, self.extent, ast_name=self.w_include[:255], ast_type=ASTT.CPPro_include)


class CPPro_line(CPPro):
    type_id = ASTT.CPPro_line

    def __init__(self, extent: Line, lineno: int = 0, filename: str | None = None) -> None:
        super().__init__(extent)
        self.lineno = lineno
        self.filename = filename
        self.type_id = ASTT.CPPro_line

    def init_subclass(self) -> None:
        self.lineno = 0
        self.filename = None
        self.type_id = ASTT.CPPro_line

    def exec_literal(self, token: Any, cursor: Any) -> None:
        if self.lineno == 0:
            try:
                self.lineno = int(token.spelling_str)
            except ValueError:
                self.filename = token.spelling_str
        else:
            self.filename = token.spelling_str

    def extract(self, CS: Any) -> None:
        val = f"{self.lineno} {self.filename or ''}".strip()
        self.extract_1arg(CS, ASTT.CPPro_line, val[:255], self.extent)


class CPPro_error(CPPro):
    type_id = ASTT.CPPro_error

    def __init__(self, extent: Line, message: str = "") -> None:
        super().__init__(extent)
        self.message = message
        self.type_id = ASTT.CPPro_error

    def init_subclass(self) -> None:
        self.message = ""
        self.type_id = ASTT.CPPro_error

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        self.message = (self.message + " " + token.spelling_str).strip()

    def exec_literal(self, token: Any, cursor: Any) -> None:
        self.message = (self.message + " " + token.spelling_str).strip()

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        self.message = (self.message + token.spelling_str).strip()

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_error, self.message[:255], self.extent)


class CPPro_warning(CPPro):
    type_id = ASTT.CPPro_warning

    def __init__(self, extent: Line, message: str = "") -> None:
        super().__init__(extent)
        self.message = message
        self.type_id = ASTT.CPPro_warning

    def init_subclass(self) -> None:
        self.message = ""
        self.type_id = ASTT.CPPro_warning

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        self.message = (self.message + " " + token.spelling_str).strip()

    def exec_literal(self, token: Any, cursor: Any) -> None:
        self.message = (self.message + " " + token.spelling_str).strip()

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        self.message = (self.message + token.spelling_str).strip()

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_warning, self.message[:255], self.extent)


class CPPro_pragma(CPPro):
    type_id = ASTT.CPPro_pragma

    def __init__(self, extent: Line, payload: str = "") -> None:
        super().__init__(extent)
        self.payload = payload
        self.type_id = ASTT.CPPro_pragma

    def init_subclass(self) -> None:
        self.payload = ""
        self.type_id = ASTT.CPPro_pragma

    def exec_identifier(self, token: Any, cursor: Any) -> None:
        self.payload = (self.payload + " " + token.spelling_str).strip()

    def exec_literal(self, token: Any, cursor: Any) -> None:
        self.payload = (self.payload + " " + token.spelling_str).strip()

    def exec_punctuation(self, token: Any, cursor: Any) -> None:
        self.payload = (self.payload + token.spelling_str).strip()

    def extract(self, CS: Any) -> None:
        self.extract_1arg(CS, ASTT.CPPro_pragma, self.payload[:255], self.extent)


class CPPro_embed(CPPro):
    pass


class CPPro_defined(CPPro):
    pass


# --- Statements ---

class Ast_CompoundStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_CompoundStmt
        self.cursor = cursor


class Ast_IfStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_IfStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_IfStmt, "", self.extent, create_tag=create_tag)


class Ast_SwitchStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_SwitchStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_SwitchStmt, "", self.extent, create_tag=create_tag)


class Ast_CaseStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_CaseStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_CaseStmt, "", self.extent, create_tag=create_tag)


class Ast_DefaultStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_DefaultStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_DefaultStmt, "", self.extent, create_tag=create_tag)


class Ast_WhileStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_WhileStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_WhileStmt, "", self.extent, create_tag=create_tag)


class Ast_DoStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_DoStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_DoStmt, "", self.extent, create_tag=create_tag)


class Ast_ForStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_ForStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_ForStmt, "", self.extent, create_tag=create_tag)


class Ast_ReturnStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Semicolon, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_ReturnStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_ReturnStmt, "", self.extent, create_tag=create_tag)


class Ast_BreakStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Semicolon, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_BreakStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_BreakStmt, "", self.extent, create_tag=create_tag)


class Ast_ContinueStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Semicolon, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_ContinueStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_ContinueStmt, "", self.extent, create_tag=create_tag)


class Ast_GotoStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Semicolon, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_GotoStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_GotoStmt, "", self.extent, create_tag=create_tag)


class Ast_LabelStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_LabelStmt
        self.cursor = cursor
        self.name = getattr(cursor, "spelling", "") if cursor is not None else ""

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        label_name = str(self.name)[:255] if self.name else "label"
        self.extract_1arg(CS, ASTT.C_LabelStmt, label_name, self.extent, create_tag=create_tag)


class Ast_AsmStmt(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_AsmStmt
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = True) -> None:
        self.extract_1arg(CS, ASTT.C_AsmStmt, "", self.extent, create_tag=create_tag)


# --- Expressions ---

class Ast_CallExpr(Ast):
    def __init__(self, extent: Line, name: str = "", end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_CallExpr
        self.name = name
        self.cursor = cursor
        self.callee_cursor = cursor

    def extract(self, CS: Any, create_tag: bool = False) -> None:
        from parser.c_ast.cursor_tree import resolve_cursor_type_ast
        c = getattr(self, "cursor", None) or getattr(self, "callee_cursor", None)
        t_id, ref_ast_id = resolve_cursor_type_ast(CS, c)
        safe_name = str(getattr(self, "name", ""))[:255]
        if ref_ast_id != 0:
            with CS(REF_POS):
                CS.store(m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, 1),), None, safe_name, self.type_id, None, 0, t_id, ref_ast_id))
                ast_id_route = CS.get_route_parse()
        else:
            with CS(REF_POS):
                CS.store(m_ast.view(((m_ast.ast_id,),), None, safe_name, self.type_id))
                ast_id_route = CS.get_route_parse()
        self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
        if ref_ast_id != 0 and hasattr(CS, "pending_symbol_refs"):
            CS.pending_symbol_refs.append((ref_ast_id, int(SymbolRole.Call), self.extent.line_pos[0], self.extent.char_pos[0]))
        if create_tag:
            with CS(REF_NO_REF):
                if G.OVERRIDE_FORCE_AST_DEBUG:
                    self.ast_debug(CS, ast_id_route)
                self.tag(CS, ast_id_route, self.extent, ast_name=safe_name, ast_type=self.type_id)


class Ast_MemberRefExpr(Ast):
    def __init__(self, extent: Line, name: str = "", end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_MemberRefExpr
        self.name = name
        self.cursor = cursor
        self.member_cursor = cursor

    def extract(self, CS: Any, create_tag: bool = False) -> None:
        from parser.c_ast.cursor_tree import resolve_cursor_type_ast
        c = getattr(self, "cursor", None) or getattr(self, "member_cursor", None)
        t_id, ref_ast_id = resolve_cursor_type_ast(CS, c)
        self.ref_ast_id = ref_ast_id
        safe_name = str(getattr(self, "name", ""))[:255]
        if ref_ast_id != 0:
            with CS(REF_POS):
                CS.store(m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, 1),), None, safe_name, self.type_id, None, 0, t_id, ref_ast_id))
                ast_id_route = CS.get_route_parse()
        else:
            with CS(REF_POS):
                CS.store(m_ast.view(((m_ast.ast_id,),), None, safe_name, self.type_id))
                ast_id_route = CS.get_route_parse()
        self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
        if ref_ast_id != 0 and hasattr(CS, "pending_symbol_refs"):
            CS.pending_symbol_refs.append((ref_ast_id, int(SymbolRole.MemberRef), self.extent.line_pos[0], self.extent.char_pos[0]))
        if create_tag:
            with CS(REF_NO_REF):
                if G.OVERRIDE_FORCE_AST_DEBUG:
                    self.ast_debug(CS, ast_id_route)
                self.tag(CS, ast_id_route, self.extent, ast_name=safe_name, ast_type=self.type_id)


class Ast_DeclRefExpr(Ast):
    def __init__(self, extent: Line, name: str = "", end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_DeclRefExpr
        self.name = name
        self.cursor = cursor
        self.decl_cursor = cursor

    def extract(self, CS: Any, create_tag: bool = False) -> None:
        from parser.c_ast.cursor_tree import resolve_cursor_type_ast
        c = getattr(self, "cursor", None) or getattr(self, "decl_cursor", None)
        t_id, ref_ast_id = resolve_cursor_type_ast(CS, c)
        safe_name = str(getattr(self, "name", ""))[:255]
        if ref_ast_id != 0:
            with CS(REF_POS):
                CS.store(m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, 1),), None, safe_name, self.type_id, None, 0, t_id, ref_ast_id))
                ast_id_route = CS.get_route_parse()
        else:
            with CS(REF_POS):
                CS.store(m_ast.view(((m_ast.ast_id,),), None, safe_name, self.type_id))
                ast_id_route = CS.get_route_parse()
        self.ast_ref = CS.ref(m_ast.ast_id, *ast_id_route)
        if ref_ast_id != 0 and hasattr(CS, "pending_symbol_refs"):
            CS.pending_symbol_refs.append((ref_ast_id, int(SymbolRole.DeclRef), self.extent.line_pos[0], self.extent.char_pos[0]))
        if create_tag:
            with CS(REF_NO_REF):
                if G.OVERRIDE_FORCE_AST_DEBUG:
                    self.ast_debug(CS, ast_id_route)
                self.tag(CS, ast_id_route, self.extent, ast_name=safe_name, ast_type=self.type_id)


class Ast_MacroRefExpr(Ast):
    def __init__(self, extent: Line, name: str = "", end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.CPPro_define
        self.name = name
        self.cursor = cursor

    def extract(self, CS: Any, create_tag: bool = False) -> None:
        from parser.c_ast.cursor_tree import resolve_cursor_type_ast
        c = getattr(self, "cursor", None)
        t_id, ref_ast_id = resolve_cursor_type_ast(CS, c)
        if ref_ast_id != 0 and hasattr(CS, "pending_symbol_refs"):
            CS.pending_symbol_refs.append((ref_ast_id, int(SymbolRole.MacroExpansion), self.extent.line_pos[0], self.extent.char_pos[0]))


class Ast_BinaryOperator(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_BinaryOperator
        self.cursor = cursor


class Ast_UnaryOperator(Ast):
    def __init__(self, extent: Line, end_mode: int = End_Mode.Extent, cursor: Any = None) -> None:
        super().__init__(extent, end_mode)
        self.type_id = ASTT.C_UnaryOperator
        self.cursor = cursor


class Ast_MACRO_INSTANTIATION(Ast):
    def __init__(self, extent: Line) -> None:
        super().__init__(extent, End_Mode.Extent)


# --- Type Tokens and Declarations ---

class TypeToken:
    __slots__ = ("extent", "code", "type", "is_definition", "foreign_name", "foreign_file", "foreign_extent", "cursor")

    def __init__(self, token: Any = None, asttype: int = 0, cursor: Any = None) -> None:
        if hasattr(token, "line"):
            self.extent = token.line
            self.code = getattr(token, "spelling_str", "")
        elif hasattr(token, "extent"):
            self.extent = token.extent
            self.code = getattr(token, "code", "") or getattr(token, "spelling_str", "")
        else:
            self.extent = getattr(token, "line", None) or Line(0, 0)
            self.code = getattr(token, "spelling_str", "")
        self.type = asttype
        self.is_definition = False
        self.foreign_name = None
        self.foreign_file = None
        self.foreign_extent = None
        self.cursor = cursor

    def __repr__(self) -> str:
        return self.code


class TSRef(IntEnum):
    No_Ref = 0
    AST_Ref = 1
    Route_Ref = 2


class TypeSegment:
    def __init__(self) -> None:
        self.content: list[TypeToken] = []
        self.cqual = CQual.Empty
        self.cqual_content: list[TypeToken] = []
        self.ref_type = TSRef.No_Ref
        self.ref: Any = None
        self.type_id: int | None = None
        self.ref_ast_id: Any = None
        self.symbol_refs: list[tuple[Any, int, int]] | None = None

    def append(self, token: TypeToken) -> None:
        self.content.append(token)

    def generate_ast(self, CS: Any) -> None:
        self.type_id = None
        self.ref_ast_id = None
        self.symbol_refs = None

        if self.ref_type == TSRef.No_Ref:
            if (
                len(self.content) == 1
                and self.cqual == CQual.Empty
                and self.content[0].type != ASTT.C_SCtypedef
            ):
                self.type_id = self.content[0].type
                return

            if (
                self.content
                and self.content[0].type in {ASTT.C_struct, ASTT.C_functionproto, ASTT.C_union, ASTT.C_enum}
                and len(self.content) == 2
                and self.cqual == CQual.Empty
            ):
                from parser.c_ast.cursor_tree import get_decl_type
                decl_type = get_decl_type(self.content[0].type)
                sym_name = self.content[1].foreign_name or self.content[1].code[:255]
                f_file = self.content[1].foreign_file
                tok_l = self.content[1].extent.line_pos[0]
                tok_c = self.content[1].extent.char_pos[0]
                if f_file:
                    self.ref = (REF_FILE, f_file, sym_name, int(decl_type))
                    self.type_id = self.content[0].type
                    self.ref_type = TSRef.Route_Ref
                    self.symbol_refs = [(CS.ref(m_ast.ast_id, *self.ref), tok_l, tok_c)]
                    return
                if hasattr(CS, "symbol_dict") and (sym_name, decl_type) in CS.symbol_dict:
                    self.ref = (REF_POS, CS.symbol_dict[(sym_name, decl_type)])
                    self.type_id = self.content[0].type
                    self.ref_type = TSRef.Route_Ref
                    self.symbol_refs = [(CS.ref(m_ast.ast_id, *self.ref), tok_l, tok_c)]
                    return
                notbind_type = get_notbind_type(self.content[0].type)
                op_idx = len(CS.cs)
                with CS(REF_NO_REF):
                    CS.store(m_ast.view(
                        ((m_ast.ast_id,),),
                        None,
                        sym_name,
                        notbind_type,
                    ))
                route_key = (REF_POS, op_idx)
                self.type_id = self.content[0].type
                self.ref_type = TSRef.Route_Ref
                self.ref = route_key
                self.symbol_refs = [(CS.ref(m_ast.ast_id, *self.ref), tok_l, tok_c)]
                return

            if (
                self.content
                and self.content[0].type == ASTT.C_SCtypedef
                and len(self.content) == 1
                and self.cqual == CQual.Empty
                and self.content[0].code != "typedef"
            ):
                sym_name = self.content[0].foreign_name or self.content[0].code[:255]
                f_file = self.content[0].foreign_file
                tok_l = self.content[0].extent.line_pos[0]
                tok_c = self.content[0].extent.char_pos[0]
                if f_file:
                    self.ref = (REF_FILE, f_file, sym_name, int(ASTT.C_SCtypedef))
                elif hasattr(CS, "symbol_dict") and (sym_name, int(ASTT.C_SCtypedef)) in CS.symbol_dict:
                    self.ref = (REF_POS, CS.symbol_dict[(sym_name, int(ASTT.C_SCtypedef))])
                else:
                    op_idx = len(CS.cs)
                    with CS(REF_NO_REF):
                        CS.store(m_ast.view(
                            ((m_ast.ast_id,),),
                            None,
                            sym_name,
                            ASTT.C_SCtypedef,
                        ))
                    self.ref = (REF_POS, op_idx)
                self.type_id = ASTT.C_SCtypedef
                self.ref_type = TSRef.Route_Ref
                self.symbol_refs = [(CS.ref(m_ast.ast_id, *self.ref), tok_l, tok_c)]
                return

            compound = []
            if (cqual_out := self.cqual.output_ast()) is not None:
                for item in cqual_out:
                    compound.append((item, 0))

            for i, typetoken in enumerate(self.content):
                if typetoken.type == 0 or typetoken.type == ASTT.Undefined:
                    continue
                if typetoken.type in {ASTT.C_struct, ASTT.C_functionproto, ASTT.C_union, ASTT.C_enum}:
                    if i > 0 and self.content[i - 1].type == typetoken.type:
                        from parser.c_ast.cursor_tree import get_decl_type
                        decl_type = get_decl_type(typetoken.type)
                        sym_name = typetoken.foreign_name or typetoken.code[:255]
                        f_file = typetoken.foreign_file
                        tok_l = typetoken.extent.line_pos[0]
                        tok_c = typetoken.extent.char_pos[0]
                        if f_file:
                            target_ref = CS.ref(m_ast.ast_id, REF_FILE, f_file, sym_name, int(decl_type))
                            compound.append((typetoken.type, target_ref))
                            if self.symbol_refs is None:
                                self.symbol_refs = []
                            self.symbol_refs.append((target_ref, tok_l, tok_c))
                        elif hasattr(CS, "symbol_dict") and (sym_name, decl_type) in CS.symbol_dict:
                            target_ref = CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(sym_name, decl_type)])
                            compound.append((typetoken.type, target_ref))
                            if self.symbol_refs is None:
                                self.symbol_refs = []
                            self.symbol_refs.append((target_ref, tok_l, tok_c))
                        else:
                            notbind_type = get_notbind_type(typetoken.type)
                            op_idx = len(CS.cs)
                            with CS(REF_NO_REF):
                                CS.store(m_ast.view(
                                    ((m_ast.ast_id,),),
                                    None,
                                    sym_name,
                                    notbind_type,
                                ))
                            target_ref = CS.ref(m_ast.ast_id, REF_POS, op_idx)
                            compound.append((typetoken.type, target_ref))
                            if self.symbol_refs is None:
                                self.symbol_refs = []
                            self.symbol_refs.append((target_ref, tok_l, tok_c))
                    elif typetoken.type == ASTT.C_functionproto:
                        compound.append((typetoken.type, 0))
                elif typetoken.type == ASTT.C_SCtypedef:
                    if typetoken.code == "typedef":
                        compound.append((typetoken.type, 0))
                    else:
                        sym_name = typetoken.foreign_name or typetoken.code[:255]
                        f_file = typetoken.foreign_file
                        tok_l = typetoken.extent.line_pos[0]
                        tok_c = typetoken.extent.char_pos[0]
                        if f_file:
                            target_ref = CS.ref(m_ast.ast_id, REF_FILE, f_file, sym_name, int(ASTT.C_SCtypedef))
                        elif hasattr(CS, "symbol_dict") and (sym_name, int(ASTT.C_SCtypedef)) in CS.symbol_dict:
                            target_ref = CS.ref(m_ast.ast_id, REF_POS, CS.symbol_dict[(sym_name, int(ASTT.C_SCtypedef))])
                        else:
                            op_idx = len(CS.cs)
                            with CS(REF_NO_REF):
                                CS.store(m_ast.view(
                                    ((m_ast.ast_id,),),
                                    None,
                                    sym_name,
                                    ASTT.C_SCtypedef,
                                ))
                            target_ref = CS.ref(m_ast.ast_id, REF_POS, op_idx)
                        compound.append((typetoken.type, target_ref))
                        if self.symbol_refs is None:
                            self.symbol_refs = []
                        self.symbol_refs.append((target_ref, tok_l, tok_c))
                else:
                    compound.append((typetoken.type, 0))

            if not compound:
                return

            view = []
            for i, item in enumerate(compound):
                t_code = int(item[0]) if hasattr(item[0], "value") else int(item[0])
                view.extend((None, i, t_code, item[1]))

            view = tuple(view)
            with CS(REF_NO_REF):
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
