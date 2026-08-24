"""parser/kconfig_ast/kconfig_ast.py - Linux Kconfig AST Extraction & ChangeSet Integration.

Parses Kconfig source files, extracts full AST hierarchies, code tags, spatial maps,
normalized symbols, dependency relations, and menu tree structures into ChangeSets.
"""
from __future__ import annotations
import logging
from typing import Any
from pathlib import Path

from core.globalstuff import (
    G,
    REF_ROOT,
    REF_OLD,
    REF_POS,
    REF_C_AST,
    REF_NO_REF,
    ASTT,
    OP_REF,
    FILE_ERROR,
)
m_file_name = m_file = m_bridge_file = m_type_descriptor = m_ast = m_ast_container = None
m_ast_include = m_ast_debug = m_tag = m_bridge_tag = m_map_ast = m_bridge_map = None
m_ast_hash = m_kconfig_symbol = m_kconfig_relation = m_kconfig_tree = None


def _init_tables() -> None:
    global m_file_name, m_file, m_bridge_file, m_type_descriptor, m_ast, m_ast_container
    global m_ast_include, m_ast_debug, m_tag, m_bridge_tag, m_map_ast, m_bridge_map
    global m_ast_hash, m_kconfig_symbol, m_kconfig_relation, m_kconfig_tree
    if m_file_name is not None:
        return
    import core.DBLayout as db_layout
    m_file_name = db_layout.m_file_name
    m_file = db_layout.m_file
    m_bridge_file = db_layout.m_bridge_file
    m_type_descriptor = db_layout.m_type_descriptor
    m_ast = db_layout.m_ast
    m_ast_container = db_layout.m_ast_container
    m_ast_include = db_layout.m_ast_include
    m_ast_debug = db_layout.m_ast_debug
    m_tag = db_layout.m_tag
    m_bridge_tag = db_layout.m_bridge_tag
    m_map_ast = db_layout.m_map_ast
    m_bridge_map = db_layout.m_bridge_map
    m_ast_hash = db_layout.m_ast_hash
    m_kconfig_symbol = db_layout.m_kconfig_symbol
    m_kconfig_relation = db_layout.m_kconfig_relation
    m_kconfig_tree = db_layout.m_kconfig_tree

from parser.c_ast.c_ast_type import Line
from parser.c_ast.c_ast import get_prior_tags, close_prior_tags
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
    KconfigExpr,
    ExprOp,
    TYPE_UNKNOWN,
    TYPE_BOOL,
    TYPE_TRISTATE,
    TYPE_STRING,
    TYPE_HEX,
    TYPE_INT,
)

logger = logging.getLogger(__name__)


def kconfig_ast_parse(CS: Any) -> None:
    """Entry point for parsing Kconfig files into ChangeSet database operations."""
    _init_tables()
    with CS(REF_C_AST):
        if CS.file_operation == "A":
            KconfigManager(CS)
        elif CS.file_operation == "M" or (CS.file_operation and CS.file_operation[0] == "R"):
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

        # Emission state
        self.VID = CS.gp.VID if hasattr(CS, "gp") and hasattr(CS.gp, "VID") else 1
        self.Old_VID = CS.gp.Old_VID if hasattr(CS, "gp") and hasattr(CS.gp, "Old_VID") else 0
        self.tree_priority = 0
        self._extract_all()

    def _extract_all(self) -> None:
        """Walk parsed AST items and emit ChangeSet records across all relational tables."""
        parent_tree_id = 0
        self._extract_items(self.ast_items, parent_tree_id=parent_tree_id, parent_dep_exprs=[])

    def _extract_items(self, items: list[Any], parent_tree_id: int | Any, parent_dep_exprs: list[KconfigExpr]) -> None:
        for item in items:
            if isinstance(item, KconfigMainmenu):
                self._extract_mainmenu(item, parent_tree_id, parent_dep_exprs)
            elif isinstance(item, KconfigConfig):
                self._extract_config(item, parent_tree_id, parent_dep_exprs)
            elif isinstance(item, KconfigMenu):
                self._extract_menu(item, parent_tree_id, parent_dep_exprs)
            elif isinstance(item, KconfigChoice):
                self._extract_choice(item, parent_tree_id, parent_dep_exprs)
            elif isinstance(item, KconfigIf):
                self._extract_if(item, parent_tree_id, parent_dep_exprs)
            elif isinstance(item, KconfigComment):
                self._extract_comment(item, parent_tree_id, parent_dep_exprs)
            elif isinstance(item, KconfigSource):
                self._extract_source(item, parent_tree_id, parent_dep_exprs)

    def _tag_and_map(self, ast_ref: Any, line_s: int, line_e: int, char_s: int, char_e: int, code: str) -> Any:
        """Create or recycle m_tag, m_bridge_tag, m_map_ast, and m_bridge_map entries."""
        CS = self.CS
        extent = Line(line_s, line_e)
        extent.line_pos = (line_s, line_e)
        extent.char_pos = (char_s, char_e)
        extent.code = code

        current_tag = (
            None,
            CS.gp.VID,
            0,
            extent.code,
            ast_ref,
            0,
            0,
        )

        # Attempt to recycle prior version tag if code matches
        if CS.prior_tags and (extent.code != ""):
            lookup = getattr(CS, "prior_tags_map", None)
            if lookup is not None:
                tag_list = lookup.get(extent.code)
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

        with CS(REF_POS):
            CS.store(m_tag.set(*current_tag))
            tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

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

    def _extract_expr_ast(self, expr: KconfigExpr | None) -> Any:
        """Recursively store expression AST nodes into m_ast and m_ast_container."""
        if expr is None:
            return 0
        CS = self.CS

        op_type_map = {
            ExprOp.SYMBOL_REF: (ASTT.Kconfig_Symbol_Ref, expr.value or ""),
            ExprOp.CONSTANT: (ASTT.Kconfig_Constant, expr.value or ""),
            ExprOp.NOT: (ASTT.Kconfig_Op_Not, "!"),
            ExprOp.AND: (ASTT.Kconfig_Op_And, "&&"),
            ExprOp.OR: (ASTT.Kconfig_Op_Or, "||"),
            ExprOp.EQUAL: (ASTT.Kconfig_Op_Equal, "="),
            ExprOp.UNEQUAL: (ASTT.Kconfig_Op_Unequal, "!="),
        }
        astt_type, expr_name = op_type_map.get(expr.op, (ASTT.Undefined, ""))

        with CS(REF_POS):
            CS.store(m_ast.set(None, expr_name, astt_type.value))
            expr_ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        # Container links for operands
        priority = 0
        if expr.left:
            left_ref = self._extract_expr_ast(expr.left)
            with CS(REF_POS):
                CS.store(m_ast_container.set(
                    expr_ast_ref,
                    priority,
                    ASTT.Kconfig_Op_And.value,
                    left_ref,
                ))
            priority += 1
        if expr.right:
            right_ref = self._extract_expr_ast(expr.right)
            with CS(REF_POS):
                CS.store(m_ast_container.set(
                    expr_ast_ref,
                    priority,
                    ASTT.Kconfig_Op_And.value,
                    right_ref,
                ))
            priority += 1

        return expr_ast_ref

    def _extract_mainmenu(self, node: KconfigMainmenu, parent_tree_id: int | Any, parent_dep_exprs: list[KconfigExpr]) -> None:
        CS = self.CS
        with CS(REF_POS):
            CS.store(m_ast.set(None, node.title or "mainmenu", ASTT.Kconfig_Mainmenu.value))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        self._tag_and_map(ast_ref, node.line_s, node.line_e, node.char_s, node.char_e, node.raw_code)

    def _extract_config(self, cfg: KconfigConfig, parent_tree_id: int | Any, parent_dep_exprs: list[KconfigExpr]) -> None:
        CS = self.CS
        ast_type = ASTT.Kconfig_Menuconfig if cfg.is_menuconfig else ASTT.Kconfig_Config

        with CS(REF_POS):
            CS.store(m_ast.set(None, cfg.name, ast_type.value))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        self._tag_and_map(ast_ref, cfg.line_s, cfg.line_e, cfg.char_s, cfg.char_e, cfg.raw_code)

        # Determine default value string
        def_val_str = ""
        if cfg.defaults:
            first_def, _ = cfg.defaults[0]
            def_val_str = first_def.to_string()

        # Store m_kconfig_symbol
        with CS(REF_POS):
            CS.store(m_kconfig_symbol.set(
                None,
                self.VID,
                0,  # vid_e (active)
                cfg.name,
                cfg.sym_type,
                cfg.prompt or "",
                def_val_str,
                cfg.help_text or "",
                ast_ref,
            ))
            kcid_ref = ((m_kconfig_symbol.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        # Combine parent context dependencies with cfg.depends_on
        all_deps = list(parent_dep_exprs) + list(cfg.depends_on)
        rel_priority = 0

        # Store depends_on relations (rel_type=1)
        for dep_expr in all_deps:
            cond_ast_ref = self._extract_expr_ast(dep_expr)
            for target_sym in dep_expr.collect_symbols():
                with CS(REF_POS):
                    CS.store(m_kconfig_relation.set(
                        kcid_ref,
                        target_sym,
                        1,  # depends_on
                        cond_ast_ref,
                        rel_priority,
                    ))
                rel_priority += 1

        # Store select relations (rel_type=2)
        for target_sym, cond_expr in cfg.selects:
            cond_ast_ref = self._extract_expr_ast(cond_expr) if cond_expr else 0
            with CS(REF_POS):
                CS.store(m_kconfig_relation.set(
                    kcid_ref,
                    target_sym,
                    2,  # select
                    cond_ast_ref,
                    rel_priority,
                ))
            rel_priority += 1

        # Store imply relations (rel_type=3)
        for target_sym, cond_expr in cfg.implies:
            cond_ast_ref = self._extract_expr_ast(cond_expr) if cond_expr else 0
            with CS(REF_POS):
                CS.store(m_kconfig_relation.set(
                    kcid_ref,
                    target_sym,
                    3,  # imply
                    cond_ast_ref,
                    rel_priority,
                ))
            rel_priority += 1

        # Tree hierarchy record: node_type: 3=config, 4=menuconfig
        node_type = 4 if cfg.is_menuconfig else 3
        dep_ast_ref = self._extract_expr_ast(all_deps[0]) if all_deps else 0
        display_title = cfg.prompt or cfg.name
        self.tree_priority += 1

        with CS(REF_POS):
            CS.store(m_kconfig_tree.set(
                None,
                self.VID,
                parent_tree_id,
                node_type,
                display_title,
                kcid_ref,
                self.tree_priority,
                dep_ast_ref,
                ast_ref,
            ))

    def _extract_menu(self, menu: KconfigMenu, parent_tree_id: int | Any, parent_dep_exprs: list[KconfigExpr]) -> None:
        CS = self.CS
        with CS(REF_POS):
            CS.store(m_ast.set(None, menu.title or "menu", ASTT.Kconfig_Menu.value))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        self._tag_and_map(ast_ref, menu.line_s, menu.line_e, menu.char_s, menu.char_e, menu.raw_code)

        all_deps = list(parent_dep_exprs) + list(menu.depends_on)
        dep_ast_ref = self._extract_expr_ast(all_deps[0]) if all_deps else 0
        self.tree_priority += 1

        # Tree hierarchy for menu (node_type=1)
        with CS(REF_POS):
            CS.store(m_kconfig_tree.set(
                None,
                self.VID,
                parent_tree_id,
                1,  # menu
                menu.title or "Menu",
                0,  # no kcid
                self.tree_priority,
                dep_ast_ref,
                ast_ref,
            ))
            current_tree_id = ((m_kconfig_tree.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        # Recursively process children within menu scope
        self._extract_items(menu.children, parent_tree_id=current_tree_id, parent_dep_exprs=all_deps)

    def _extract_choice(self, choice: KconfigChoice, parent_tree_id: int | Any, parent_dep_exprs: list[KconfigExpr]) -> None:
        CS = self.CS
        choice_title = choice.prompt or choice.name or "Choice"
        with CS(REF_POS):
            CS.store(m_ast.set(None, choice_title, ASTT.Kconfig_Choice.value))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        self._tag_and_map(ast_ref, choice.line_s, choice.line_e, choice.char_s, choice.char_e, choice.raw_code)

        all_deps = list(parent_dep_exprs) + list(choice.depends_on)
        dep_ast_ref = self._extract_expr_ast(all_deps[0]) if all_deps else 0
        self.tree_priority += 1

        # Tree hierarchy for choice (node_type=2)
        with CS(REF_POS):
            CS.store(m_kconfig_tree.set(
                None,
                self.VID,
                parent_tree_id,
                2,  # choice
                choice_title,
                0,  # no kcid
                self.tree_priority,
                dep_ast_ref,
                ast_ref,
            ))
            current_tree_id = ((m_kconfig_tree.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        self._extract_items(choice.children, parent_tree_id=current_tree_id, parent_dep_exprs=all_deps)

    def _extract_if(self, if_node: KconfigIf, parent_tree_id: int | Any, parent_dep_exprs: list[KconfigExpr]) -> None:
        CS = self.CS
        with CS(REF_POS):
            CS.store(m_ast.set(None, f"if {if_node.cond.to_string()}", ASTT.Kconfig_If.value))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        self._tag_and_map(ast_ref, if_node.line_s, if_node.line_e, if_node.char_s, if_node.char_e, if_node.raw_code)

        all_deps = list(parent_dep_exprs) + [if_node.cond]
        self._extract_items(if_node.children, parent_tree_id=parent_tree_id, parent_dep_exprs=all_deps)

    def _extract_comment(self, comment: KconfigComment, parent_tree_id: int | Any, parent_dep_exprs: list[KconfigExpr]) -> None:
        CS = self.CS
        with CS(REF_POS):
            CS.store(m_ast.set(None, comment.title or "comment", ASTT.Kconfig_Comment.value))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        self._tag_and_map(ast_ref, comment.line_s, comment.line_e, comment.char_s, comment.char_e, comment.raw_code)

        all_deps = list(parent_dep_exprs) + list(comment.depends_on)
        dep_ast_ref = self._extract_expr_ast(all_deps[0]) if all_deps else 0
        self.tree_priority += 1

        # Tree hierarchy for comment (node_type=5)
        with CS(REF_POS):
            CS.store(m_kconfig_tree.set(
                None,
                self.VID,
                parent_tree_id,
                5,  # comment
                comment.title or "Comment",
                0,
                self.tree_priority,
                dep_ast_ref,
                ast_ref,
            ))

    def _extract_source(self, src: KconfigSource, parent_tree_id: int | Any, parent_dep_exprs: list[KconfigExpr]) -> None:
        CS = self.CS
        ast_type = ASTT.Kconfig_Rsource if src.is_rsource else ASTT.Kconfig_Source
        with CS(REF_POS):
            CS.store(m_ast.set(None, src.path, ast_type.value))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        self._tag_and_map(ast_ref, src.line_s, src.line_e, src.char_s, src.char_e, src.raw_code)

        # Register included file path in m_ast_include
        with CS(REF_POS):
            CS.store(m_file_name.set(None, src.path))
            fnid_ref = ((m_file_name.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        with CS(REF_POS):
            CS.store(m_ast_include.set(ast_ref, fnid_ref))
