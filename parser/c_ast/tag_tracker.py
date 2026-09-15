"""Tag lifecycle tracking and 4-tier prior tag evolution matching engine for c_ast.

Provides:
- get_prior_tags(CS): Queries prior version active tags via 3-way join (m_bridge_tag -> m_tag -> m_ast) and constructs inverted lookup indices.
- match_prior_tag_transition(CS, extent, ast_name, ast_type, prev_anchor, next_anchor):
  4-tier transition matching engine:
    1. Exact snippet hash match (handled in Ast.tag / check_exact_match)
    2. Exact symbol name & category match (functions, structs, unions, enums, macros)
    3. Bulk prefix/suffix rename refactoring detection
    4. Spatial proximity / line overlap fallback
- close_prior_tags(CS): Marks obsolete prior tags as closed (vid_e = Old_VID).
"""
from __future__ import annotations
from typing import Any
import logging
from core.globalstuff import G, ASTT, compute_code_hash, REF_POS, REF_OLD
from core.DBLayout import m_file_name, m_bridge_file, m_bridge_tag, m_tag, m_ast

logger = logging.getLogger(__name__)

COMMENT_TYPES = {
    int(ASTT.C_Comment),
    int(ASTT.ASM_Comment),
    int(ASTT.Kconfig_Comment),
    int(ASTT.Rust_DocComment),
    int(ASTT.Rust_Comment),
}

NAMED_CONSTRUCT_TYPES = {
    int(ASTT.C_functionproto),
    int(ASTT.C_functionprotodecl),
    int(ASTT.C_struct),
    int(ASTT.C_structdecl),
    int(ASTT.C_union),
    int(ASTT.C_uniondecl),
    int(ASTT.C_enum),
    int(ASTT.C_enumdecl),
    int(ASTT.C_SCtypedef),
    int(ASTT.CPPro_define),
    int(ASTT.ASM_Macro),
    int(ASTT.ASM_Label),
}


def get_prior_tags(CS: Any) -> None:
    """Query Table Engine for active AST tags registered in the previous version and build inverted indices."""
    CS.active_tag_list = set()
    CS.transitioned_tag_list = set()
    CS.prior_tags = None
    CS.prior_tags_map = {}
    CS.prior_tags_by_name = {}
    CS.prior_tags_by_pos = []
    CS.prior_unmatched_symbols = []  # list of (idx, tag_id, name, type_val, line_s, line_e)

    old_vid = getattr(CS.gp, "Old_VID", 0)
    if old_vid <= 0:
        return

    lookup_path = (
        CS.old_path
        if (CS.file_operation and CS.file_operation.startswith("R") and CS.old_path)
        else CS.current_path
    )
    fn_row = m_file_name.get(None, lookup_path)
    if not fn_row or len(fn_row) < 3 or not fn_row[2]:
        return
    fnid = fn_row[2][0]

    bf_row = m_bridge_file.get(old_vid, fnid, None)
    if not bf_row or len(bf_row) < 3 or not bf_row[2]:
        return
    old_fid = bf_row[2][2]

    # 3-way join: m_bridge_tag -> m_tag -> m_ast
    # Resolves tag coordinates, version metadata, snippet hash, and AST symbol name/type in a single query
    joins = (
        (m_bridge_tag.tag_id, m_tag.tag_id, 1),
        (m_tag.ast_id, m_ast.ast_id, 1),
    )
    cols = (old_fid,) + (None,) * 15
    CS.prior_tags = m_bridge_tag.view_get_multiple(joins, *cols)

    if not CS.prior_tags:
        return

    # In-memory ast cache for fallback if caller provides shorter tuples
    ast_cache: dict[int, tuple[str, int | None]] = {}

    for x, tag in enumerate(CS.prior_tags):
        line_s = tag[2]
        line_e = tag[3]
        tag_id = tag[1] if len(tag) > 1 else tag[0]

        # Inverted Hash Index (Tier 1)
        if len(tag) > 9:
            tag_hash = tag[9]
            if tag_hash:
                CS.prior_tags_map.setdefault(tag_hash, []).append((x, tag_id))

        ast_id = tag[10] if len(tag) > 10 else 0
        ast_name = ""
        ast_type = None

        if len(tag) >= 16:
            # Direct resolution from 3-way join (columns: m_ast.name at index 14, m_ast.type_id at index 15)
            ast_name = tag[14] or ""
            ast_type = tag[15]
        elif ast_id:
            # Fallback for 2-way join tuples with per-run dictionary memoization
            if ast_id in ast_cache:
                ast_name, ast_type = ast_cache[ast_id]
            else:
                ast_row = m_ast.get(ast_id, None, None)
                if ast_row and len(ast_row) >= 3 and ast_row[2]:
                    ast_name = ast_row[2][1] or ""
                    ast_type = ast_row[2][2]
                ast_cache[ast_id] = (ast_name, ast_type)

        type_val = int(ast_type) if ast_type is not None else None

        if ast_name:
            key = (ast_name, type_val)
            CS.prior_tags_by_name.setdefault(key, []).append((x, tag_id, line_s, line_e))
            CS.prior_tags_by_name.setdefault(ast_name, []).append((x, tag_id, line_s, line_e))
            if type_val in NAMED_CONSTRUCT_TYPES:
                CS.prior_unmatched_symbols.append((x, tag_id, ast_name, type_val, line_s, line_e))

        CS.prior_tags_by_pos.append((line_s, line_e, x, tag_id, ast_name, ast_type))


def check_exact_match(CS: Any, code_hash: bytes) -> int | None:
    """Check Tier 1 exact snippet code hash match against prior version tags."""
    if not getattr(CS, "prior_tags", None) or not code_hash:
        return None
    lookup = getattr(CS, "prior_tags_map", None)
    if not lookup:
        return None
    tag_list = lookup.get(code_hash)
    if not tag_list:
        return None
    active_set = CS.active_tag_list if isinstance(CS.active_tag_list, set) else set(CS.active_tag_list)
    for item in tag_list:
        if item[0] not in active_set:
            x, tag_id = item
            if isinstance(CS.active_tag_list, set):
                CS.active_tag_list.add(x)
            else:
                CS.active_tag_list.append(x)
            return tag_id
    return None


def _detect_bulk_rename_candidate(
    ast_name: str,
    type_val: int | None,
    unmatched_symbols: list[tuple[int, int, str, int, int, int]],
    active_set: set[int],
    transitioned: set[int],
    line_s: int,
) -> tuple[int, int] | None:
    """Detect if ast_name evolved from an unmatched prior symbol via common prefix/suffix renaming."""
    if not ast_name or len(ast_name) < 3:
        return None

    best_candidate: tuple[int, int] | None = None
    min_dist = float("inf")

    for idx, tag_id, p_name, p_type, p_ls, p_le in unmatched_symbols:
        if idx in active_set or idx in transitioned:
            continue
        if type_val is not None and p_type != type_val:
            continue
        if not p_name or p_name == ast_name or len(p_name) < 3:
            continue

        # Check prefix transformation: prefix added or removed
        # e.g., isd200_action vs action, or old_foo vs new_foo
        is_affine_rename = False
        common_len = 0

        # Suffix matching (e.g. prefix added): ast_name.endswith(p_name) or p_name.endswith(ast_name)
        if ast_name.endswith(p_name) and len(p_name) >= 3:
            is_affine_rename = True
            common_len = len(p_name)
        elif p_name.endswith(ast_name) and len(ast_name) >= 3:
            is_affine_rename = True
            common_len = len(ast_name)
        # Prefix matching (e.g. suffix added): ast_name.startswith(p_name) or p_name.startswith(ast_name)
        elif ast_name.startswith(p_name) and len(p_name) >= 3:
            is_affine_rename = True
            common_len = len(p_name)
        elif p_name.startswith(ast_name) and len(ast_name) >= 3:
            is_affine_rename = True
            common_len = len(ast_name)
        else:
            # Common prefix + suffix with small middle edit (or systematic prefix change)
            # Find common prefix length
            cp = 0
            while cp < len(ast_name) and cp < len(p_name) and ast_name[cp] == p_name[cp]:
                cp += 1
            # Find common suffix length
            cs = 0
            while (
                cs < (len(ast_name) - cp)
                and cs < (len(p_name) - cp)
                and ast_name[-(cs + 1)] == p_name[-(cs + 1)]
            ):
                cs += 1
            if cp + cs >= 3 and (cp >= 2 or cs >= 2):
                # Strong base overlap
                is_affine_rename = True
                common_len = cp + cs

        if is_affine_rename:
            dist = abs(line_s - p_ls) - (common_len * 10)
            if dist < min_dist:
                min_dist = dist
                best_candidate = (idx, tag_id)

    return best_candidate


def match_prior_tag_transition(
    CS: Any,
    extent: Any,
    ast_name: str | None = None,
    ast_type: Any = None,
    prev_anchor: str | None = None,
    next_anchor: str | None = None,
) -> int | None:
    """Find the best matching un-recycled prior tag for a modified code construct across 4 tiers."""
    if not getattr(CS, "prior_tags", None):
        return None

    transitioned = getattr(CS, "transitioned_tag_list", None)
    if transitioned is None:
        CS.transitioned_tag_list = set()
        transitioned = CS.transitioned_tag_list

    active = getattr(CS, "active_tag_list", set())
    active_set = set(active) if isinstance(active, list) else active

    line_s = 0
    line_e = 0
    if hasattr(extent, "line_pos") and extent.line_pos:
        line_s = extent.line_pos[0]
        line_e = extent.line_pos[1]
    elif hasattr(extent, "line_s"):
        line_s = getattr(extent, "line_s", 0)
        line_e = getattr(extent, "line_e", line_s)
    elif isinstance(extent, (tuple, list)) and len(extent) >= 2:
        line_s = extent[0]
        line_e = extent[1]

    type_val = int(ast_type) if ast_type is not None else None
    is_comment = type_val in COMMENT_TYPES

    # =========================================================================
    # Tier 2: Named Construct Match (Structs, Functions, Enums, Typedefs, Macros)
    # =========================================================================
    if ast_name and not is_comment:
        by_name = getattr(CS, "prior_tags_by_name", {})
        candidates = by_name.get((ast_name, type_val))
        if not candidates:
            candidates = by_name.get(ast_name)
        if candidates:
            # Pick candidate closest in line position
            best_idx = None
            best_tag_id = None
            best_dist = float("inf")
            for item in candidates:
                idx, tag_id, p_ls, p_le = item
                if idx not in active_set and idx not in transitioned:
                    dist = abs(p_ls - line_s)
                    if dist < best_dist:
                        best_dist = dist
                        best_idx = idx
                        best_tag_id = tag_id
            if best_idx is not None:
                transitioned.add(best_idx)
                return best_tag_id

        # =====================================================================
        # Tier 3: Bulk Prefix / Suffix Rename Refactoring Detection
        # =====================================================================
        unmatched_symbols = getattr(CS, "prior_unmatched_symbols", [])
        if unmatched_symbols:
            bulk_match = _detect_bulk_rename_candidate(
                ast_name, type_val, unmatched_symbols, active_set, transitioned, line_s
            )
            if bulk_match is not None:
                idx, tag_id = bulk_match
                transitioned.add(idx)
                return tag_id

        return None

    # =========================================================================
    # Tier 4: Spatial Overlap & Proximity Match Fallback
    # =========================================================================
    by_pos = getattr(CS, "prior_tags_by_pos", [])
    if by_pos and (line_s > 0 or line_e > 0):
        best_match = None
        best_overlap = -1
        for p_ls, p_le, idx, tag_id, p_name, p_type in by_pos:
            if idx in active_set or idx in transitioned:
                continue
            p_type_val = int(p_type) if p_type is not None else None
            p_is_comment = p_type_val in COMMENT_TYPES
            if is_comment:
                if not p_is_comment:
                    continue
            else:
                if p_is_comment:
                    continue
                if p_name:
                    continue
                if type_val is not None and p_type_val is not None and int(p_type_val) != type_val:
                    continue

            overlap_s = max(line_s, p_ls)
            overlap_e = min(line_e, p_le)
            if overlap_e >= overlap_s:
                overlap = overlap_e - overlap_s + 1
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_match = (idx, tag_id)
            elif abs(line_s - p_ls) <= 2 and best_overlap < 0:
                best_match = (idx, tag_id)

        if best_match:
            idx, tag_id = best_match
            transitioned.add(idx)
            return tag_id

    return None


def close_prior_tags(CS: Any) -> None:
    """Mark prior version AST tags as closed (vid_e = Old_VID) if not recycled in the current version."""
    if not getattr(CS, "prior_tags", None):
        return

    active_set = CS.active_tag_list if isinstance(CS.active_tag_list, set) else set(CS.active_tag_list)
    old_vid = getattr(CS.gp, "Old_VID", 0)
    if old_vid <= 0:
        return

    with CS(REF_OLD):
        for x, tag in enumerate(CS.prior_tags):
            if x in active_set:
                continue
            if len(tag) >= 13:
                with CS(REF_POS):
                    CS.store(m_tag.update(
                        tag[6],      # m_tag.tag_id
                        tag[7],      # m_tag.vid_s
                        old_vid,     # m_tag.vid_e (marked closed)
                        tag[9],      # m_tag.hash
                        tag[10],     # m_tag.ast_id
                        tag[11],     # m_tag.hl_s
                        tag[12],     # m_tag.hl_l
                    ))
