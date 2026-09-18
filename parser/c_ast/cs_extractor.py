"""parser/c_ast/cs_extractor.py - ChangeSet Relational Stager for c_ast.

Encapsulates relational ChangeSet generation with strict enforcement of:
- Rule 12 (m_tag.set staged FIRST within `with CS(REF_POS):`, followed by m_tag_code and m_moved_tag)
- Rule 14 (32-byte binary SHA-256 via compute_code_hash)
- Invariant A: m_bridge_tag coordinates (1-based, inclusive start, exclusive end)
- Invariant I: VARCHAR(255) bounds on m_ast.name
- Invariant J: CS.register_bridge_map in-memory deduplication
"""
from __future__ import annotations
import time
from typing import Any
from core.globalstuff import (
    G,
    REF_POS,
    REF_ROOT,
    REF_NO_REF,
    OP_REF,
    compute_code_hash,
)
from core.DBLayout import (
    m_file,
    m_tag,
    m_tag_code,
    m_moved_tag,
    m_bridge_tag,
    m_ast,
    m_map_ast,
    m_bridge_map,
    m_symbol_ref,
)
from parser.c_ast.tag_tracker import check_exact_match, match_prior_tag_transition


class CSExtractor:
    """Relational ChangeSet Extraction Driver."""

    @staticmethod
    def extract_zone(CS: Any, zone: Any) -> None:
        """Extract all AST nodes from root Zone into ChangeSet operations."""
        prof = getattr(CS, "profiler", None)
        t_start = time.perf_counter() if prof is not None else 0.0

        zone.extract(CS)

        if getattr(CS, "pending_symbol_refs", None):
            last_tag = getattr(CS, "last_tag_ref", 0) or 0
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
                        last_tag,
                        ref_ast_id,
                        int(role),
                        line,
                        col,
                    ))
            CS.pending_symbol_refs.clear()

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
        if getattr(CS, "prior_tags", None) and code_str != "":
            recycled_tag_id = check_exact_match(CS, code_hash)
            if recycled_tag_id is not None:
                CS.store(m_bridge_tag.set(
                    ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                    recycled_tag_id,
                    extent.line_pos[0], extent.line_pos[1],
                    extent.char_pos[0], extent.char_pos[1],
                ))
                return recycled_tag_id

        # Check evolved prior tag match via 5-tier tag_tracker
        s_tag_id = match_prior_tag_transition(CS, extent, ast_name, ast_type)

        ast_ref = (
            CS.ref(m_ast.ast_id, *ast_id_route)
            if not (isinstance(ast_id_route, tuple) and len(ast_id_route) == 3 and ast_id_route[1] == OP_REF)
            else ast_id_route
        )

        # Rule 12 Invariant: m_tag.set MUST be first inside with CS(REF_POS)
        with CS(REF_POS):
            CS.store(m_tag.set(None, CS.gp.VID, 0, code_hash, ast_ref, 0, 0))
            tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
            # Auxiliary deduplication table MUST be staged after m_tag.set
            CS.store(m_tag_code.get_set(code_hash, code_str))
            if s_tag_id is not None:
                CS.store(m_moved_tag.set(s_tag_id, tag_ref))

        # Stage File-to-Tag Bridge (coordinates: 1-based, inclusive start, exclusive end)
        CS.store(m_bridge_tag.set(
            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
            tag_ref,
            extent.line_pos[0], extent.line_pos[1],
            extent.char_pos[0], extent.char_pos[1],
        ))

        # Stage Intra-Tag Spatial Mapping & Bridge Map (with deduplication)
        rel_line_e = max(1, extent.line_pos[1] - extent.line_pos[0] + 1)
        rel_char_e = extent.char_pos[1]
        CS.store(m_map_ast.set(
            tag_ref,
            1, 1,
            rel_line_e, rel_char_e,
            ast_ref,
        ))

        if not hasattr(CS, "register_bridge_map") or CS.register_bridge_map(tag_ref, tag_ref):
            CS.store(m_bridge_map.set(tag_ref, tag_ref))

        return tag_ref
