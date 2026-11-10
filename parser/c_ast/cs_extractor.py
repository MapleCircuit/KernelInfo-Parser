"""parser/c_ast/cs_extractor.py - ChangeSet Relational Stager for c_ast.

Encapsulates relational ChangeSet generation with strict enforcement of:
- Rule 12 (m_tag.set staged FIRST within `with CS(REF_POS):`, followed by m_moved_tag)
- Rule 14 (32-byte binary SHA-256 via compute_code_hash)
- Invariant A: m_bridge_tag coordinates (1-based, inclusive start, exclusive end)
- Invariant I: VARCHAR(255) bounds on m_ast.name
- m_map_ast is keyed by the tag (map_id = tag_id); m_bridge_map is no longer written
"""
from __future__ import annotations
import time
from typing import Any
from core.globalstuff import OP_REF, compute_code_hash
from core.DBLayout import m_ast
from core.SymbolRows import (
    flush_pending_refs as flush_pending_symbol_refs,
    register_tag as register_symbol_tag,
)
from parser.c_ast.tag_tracker import (
    check_exact_match,
    match_prior_tag_transition,
    stage_bridge_tag,
    stage_new_tag,
    stage_tag_map,
)


class CSExtractor:
    """Relational ChangeSet Extraction Driver."""

    @staticmethod
    def extract_zone(CS: Any, zone: Any) -> None:
        """Extract all AST nodes from root Zone into ChangeSet operations."""
        prof = getattr(CS, "profiler", None)
        t_start = time.perf_counter() if prof is not None else 0.0

        zone.extract(CS)

        # References left over after the last tag go to it (outside its lines, so they are stored per blob)
        flush_pending_symbol_refs(CS, getattr(CS, "last_tag_ref", 0) or 0)

        if prof is not None:
            prof.ast_extraction_s = time.perf_counter() - t_start

    @staticmethod
    def stage_tag(
        CS: Any,
        ast_id_route: Any,
        extent: Any,
        ast_name: str | None = None,
        ast_type: Any = None,
    ) -> Any:
        """Stage or recycle AST code tag adhering strictly to Rule 12 and Rule 14."""
        if extent and not extent.code:
            parser_obj = CS.parsers.get("C_AM") or CS.parsers.get("ASM_AM")
            if parser_obj and hasattr(parser_obj, "rawfile"):
                extent.cc(parser_obj.rawfile)

        code_str = extent.code if extent else ""
        code_hash = compute_code_hash(code_str)

        # Tier 1: Check exact code hash match against prior version tags
        if code_str:
            recycled_tag_id = check_exact_match(CS, code_hash)
            if recycled_tag_id is not None:
                stage_bridge_tag(CS, recycled_tag_id, extent.line_pos, extent.char_pos)
                register_symbol_tag(CS, recycled_tag_id, extent.line_pos[0], extent.line_pos[1], extent.char_pos[0], code_str)
                CS.last_tag_ref = recycled_tag_id
                flush_pending_symbol_refs(CS, recycled_tag_id)
                return recycled_tag_id

        # Check evolved prior tag match via 5-tier tag_tracker
        s_tag_id = match_prior_tag_transition(CS, extent, ast_name, ast_type)

        ast_ref = (
            CS.ref(m_ast.ast_id, *ast_id_route)
            if not (isinstance(ast_id_route, tuple) and len(ast_id_route) == 3 and ast_id_route[1] == OP_REF)
            else ast_id_route
        )

        tag_ref = stage_new_tag(CS, code_hash, ast_ref, s_tag_id)
        # File-to-Tag Bridge (coordinates: 1-based, inclusive start, exclusive end)
        stage_bridge_tag(CS, tag_ref, extent.line_pos, extent.char_pos)
        register_symbol_tag(CS, tag_ref, extent.line_pos[0], extent.line_pos[1], extent.char_pos[0], code_str)
        stage_tag_map(CS, tag_ref, ast_ref, max(1, extent.line_pos[1] - extent.line_pos[0] + 1), extent.char_pos[1])
        CS.last_tag_ref = tag_ref
        flush_pending_symbol_refs(CS, tag_ref)
        return tag_ref
