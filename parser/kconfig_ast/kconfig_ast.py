"""parser/kconfig_ast/kconfig_ast.py - Linux Kconfig ChangeSet integration (tags and source references).

Every Kconfig entry becomes a code tag like any other construct: an `m_ast` node named after the
entry (`config`/`menuconfig` symbol name, menu/choice/comment title, `if <expr>`, sourced path)
and an `m_tag` + `m_bridge_tag` placement in the blob. `source` entries with a literal path link
the sourced file through `m_ast_include`. Container entries (`menu`, `choice`, `if`) are tagged by
their header lines only, so editing one child never re-stores the whole block.

Menus, dependencies, defaults and values are not stored: the webapp builds kconfig's semantic model
from Git per revision and ARCH (parser/kconfig_ast/KCONFIG_API.md section 8).
"""
from __future__ import annotations
import logging
import os
from typing import Any

from core.globalstuff import (
    G,
    REF_ROOT,
    REF_POS,
    REF_C_AST,
    ASTT,
    OP_REF,
)
m_file_name = m_file = m_ast = m_ast_include = m_tag_code = m_tag = m_bridge_tag = m_map_ast = m_bridge_map = m_moved_tag = None
from core.globalstuff import compute_code_hash


def _init_tables() -> None:
    global m_file_name, m_file, m_ast, m_ast_include, m_tag_code, m_tag, m_bridge_tag, m_map_ast, m_bridge_map, m_moved_tag
    if m_file_name is not None:
        return
    import core.DBLayout as db_layout
    m_file_name = db_layout.m_file_name
    m_file = db_layout.m_file
    m_ast = db_layout.m_ast
    m_ast_include = db_layout.m_ast_include
    m_tag_code = db_layout.m_tag_code
    m_tag = db_layout.m_tag
    m_bridge_tag = db_layout.m_bridge_tag
    m_map_ast = db_layout.m_map_ast
    m_bridge_map = db_layout.m_bridge_map
    m_moved_tag = db_layout.m_moved_tag

from parser.c_ast import Line, get_prior_tags, close_prior_tags
from parser.kconfig_ast.kconfig_lexer import KconfigLexer
from parser.kconfig_ast.kconfig_parser import (
    KconfigParser,
    KconfigConfig,
    KconfigMenu,
    KconfigChoice,
    KconfigIf,
    KconfigComment,
    KconfigSource,
    KconfigMainmenu,
)

logger = logging.getLogger(__name__)

# m_ast.name is VARCHAR(255)
_NAME_MAX = 255


def kconfig_ast_parse(CS: Any) -> None:
    """Entry point for parsing Kconfig files into ChangeSet database operations."""
    if CS.file_operation == "R100":
        return
    _init_tables()
    with CS(REF_C_AST):
        if CS.file_operation == "A":
            KconfigManager(CS)
        elif CS.file_operation == "M" or (CS.file_operation and CS.file_operation.startswith("R")):
            get_prior_tags(CS)
            KconfigManager(CS)
            close_prior_tags(CS)
        elif CS.file_operation == "D":
            get_prior_tags(CS)
            close_prior_tags(CS)
        else:
            logger.warning("Unhandled file operation '%s' for %s", CS.file_operation, CS.current_path)


class KconfigManager:
    """Orchestrates parsing of a single Kconfig file and emits ChangeSet operations."""

    def __init__(self, CS: Any) -> None:
        _init_tables()
        self.CS = CS
        self.current_path = CS.current_path
        G.CURRENT_PARSING_FILE = self.current_path
        CS.parsers["KCONFIG"] = self

        self.rawfile = ""
        if CS.mf:
            try:
                self.rawfile = CS.mf.get_file(self.current_path, CS.gp.Version_Name)
            except Exception as e:
                logger.debug("Failed reading from MasterFile for %s: %s", self.current_path, e)

        if not self.rawfile and hasattr(CS, "raw_content"):
            self.rawfile = CS.raw_content

        self.lexer = KconfigLexer(self.rawfile)
        self.tokens = self.lexer.tokenize()
        self.parser = KconfigParser(self.tokens, self.rawfile)
        self.ast_items = self.parser.parse()
        self._extract_items(self.ast_items)

    def _extract_items(self, items: list[Any]) -> None:
        for item in items:
            if isinstance(item, KconfigConfig):
                ast_type = ASTT.Kconfig_Menuconfig if item.is_menuconfig else ASTT.Kconfig_Config
                self._stage_entry(item.name, ast_type, item, item.line_e, item.char_e, item.raw_code)
            elif isinstance(item, KconfigMenu):
                self._stage_entry(item.title or "menu", ASTT.Kconfig_Menu, item, item.header_line_e, item.header_char_e, item.header_code)
                self._extract_items(item.children)
            elif isinstance(item, KconfigChoice):
                title = item.prompt or item.name or "Choice"
                self._stage_entry(title, ASTT.Kconfig_Choice, item, item.header_line_e, item.header_char_e, item.header_code)
                self._extract_items(item.children)
            elif isinstance(item, KconfigIf):
                self._stage_entry(f"if {item.cond.to_string()}", ASTT.Kconfig_If, item, item.header_line_e, item.header_char_e, item.header_code)
                self._extract_items(item.children)
            elif isinstance(item, KconfigComment):
                self._stage_entry(item.title or "comment", ASTT.Kconfig_Comment, item, item.line_e, item.char_e, item.raw_code)
            elif isinstance(item, KconfigSource):
                self._extract_source(item)
            elif isinstance(item, KconfigMainmenu):
                self._stage_entry(item.title or "mainmenu", ASTT.Kconfig_Mainmenu, item, item.line_e, item.char_e, item.raw_code)

    def _stage_entry(self, name: str, ast_type: ASTT, item: Any, line_e: int, char_e: int, code: str) -> None:
        """One m_ast node (deduplicated by name and type) and the entry's tag."""
        CS = self.CS
        name = name[:_NAME_MAX]
        with CS(REF_POS):
            CS.store(m_ast.view(((m_ast.ast_id,),), None, name, ast_type.value))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
        self._tag_and_map(ast_ref, item.line_s, line_e, item.char_s, char_e, code, ast_name=name, ast_type=ast_type)

    def _tag_and_map(
        self,
        ast_ref: Any,
        line_s: int,
        line_e: int,
        char_s: int,
        char_e: int,
        code: str,
        ast_name: str | None = None,
        ast_type: Any = None,
    ) -> Any:
        """Create or recycle m_tag, m_bridge_tag, m_map_ast, and m_bridge_map entries."""
        CS = self.CS
        extent = Line(line_s, line_e)
        extent.line_pos = (line_s, line_e)
        extent.char_pos = (char_s, char_e)
        extent.code = code

        code_hash = compute_code_hash(code)
        current_tag = (
            None,
            code_hash,
            ast_ref,
            0,
            0,
        )

        # Attempt to recycle prior version tag if code matches
        if CS.prior_tags and (extent.code != ""):
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
                        with CS(REF_POS):
                            CS.store(m_bridge_tag.set(
                                ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                                tag_id,
                                extent.line_pos[0],
                                extent.line_pos[1],
                                extent.char_pos[0],
                                extent.char_pos[1],
                            ))
                        return tag_id

        from parser.c_ast import match_prior_tag_transition
        s_tag_id = match_prior_tag_transition(CS, extent, ast_name, ast_type)

        with CS(REF_POS):
            CS.store(m_tag.set(*current_tag))
            tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
            CS.store(m_tag_code.get_set(code_hash, code))
            if s_tag_id is not None:
                CS.store(m_moved_tag.set(s_tag_id, tag_ref))

        with CS(REF_POS):
            CS.store(m_bridge_tag.set(
                ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                tag_ref,
                extent.line_pos[0],
                extent.line_pos[1],
                extent.char_pos[0],
                extent.char_pos[1],
            ))
        self._map_spatial(ast_ref, tag_ref, extent)
        return tag_ref

    def _map_spatial(self, ast_ref: Any, tag_ref: Any, extent: Line) -> None:
        CS = self.CS
        line_s = 1
        char_s = 1
        line_e = max(1, extent.line_pos[1] - extent.line_pos[0] + 1)
        char_e = extent.char_pos[1]

        ast_target = ast_ref if (isinstance(ast_ref, tuple) and len(ast_ref) == 3 and ast_ref[1] == OP_REF) or isinstance(ast_ref, int) else CS.ref(m_ast.ast_id, *ast_ref)
        tag_target = tag_ref if (isinstance(tag_ref, tuple) and len(tag_ref) == 3 and tag_ref[1] == OP_REF) or isinstance(tag_ref, int) else CS.ref(m_tag.tag_id, *tag_ref)

        with CS(REF_POS):
            CS.store(m_map_ast.set(
                tag_target,
                line_s,
                char_s,
                line_e,
                char_e,
                ast_target,
            ))
        if not hasattr(CS, "register_bridge_map") or CS.register_bridge_map(tag_target, tag_target):
            with CS(REF_POS):
                CS.store(m_bridge_map.set(
                    tag_target,
                    tag_target,
                ))

    def _source_target(self, src: KconfigSource) -> str | None:
        """Repository path a `source` names, or None when it depends on the environment ($SRCARCH...)."""
        path = src.path
        if not path or "$" in path:
            return None
        if src.is_rsource:
            path = os.path.normpath(os.path.join(os.path.dirname(self.current_path), path))
        return path

    def _extract_source(self, src: KconfigSource) -> None:
        CS = self.CS
        if getattr(src, "is_osource", False):
            ast_type = ASTT.Kconfig_Osource
        elif src.is_rsource:
            ast_type = ASTT.Kconfig_Rsource
        else:
            ast_type = ASTT.Kconfig_Source
        name = src.path[:_NAME_MAX]
        target = self._source_target(src)
        if target is None:
            # `source "arch/$(SRCARCH)/Kconfig"` names one file per ARCH: no single file to link
            with CS(REF_POS):
                CS.store(m_ast.view(((m_ast.ast_id,),), None, name, ast_type.value))
                ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
        else:
            with CS(REF_POS):
                CS.store(m_file_name.set(None, target))
                fnid_ref = ((m_file_name.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
            with CS(REF_POS):
                CS.store(m_ast.view(((m_ast.ast_id, m_ast_include.ast_id, 1),), None, name, ast_type.value, None, fnid_ref))
                ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
        self._tag_and_map(ast_ref, src.line_s, src.line_e, src.char_s, src.char_e, src.raw_code, ast_name=name, ast_type=ast_type)
