"""Tag lifecycle tracking and 4-tier prior tag evolution matching engine for c_ast.

Provides:
- get_prior_tags(CS): Queries prior version active tags via 3-way join (m_bridge_tag -> m_tag -> m_ast) and constructs inverted lookup indices.
- match_prior_tag_transition(CS, extent, ast_name, ast_type, prev_anchor, next_anchor):
  4-tier transition matching engine:
    1. Exact snippet hash match (handled in Ast.tag / check_exact_match)
    2. Exact symbol name & category match (functions, structs, unions, enums, macros)
    3. Bulk prefix/suffix rename refactoring detection
    4. Spatial proximity / line overlap fallback
- stage_bridge_tag(CS, tag, line_pos, char_pos), stage_new_tag(CS, code_hash, ast_ref, s_tag_id),
  stage_tag_map(CS, tag, ast_ref, line_e, char_e): the tag rows every parser stages (Rule 12).
Prior tags are immutable content records under the Git DAG: nothing closes them.
"""
from __future__ import annotations
from typing import Any
import logging
from core.globalstuff import ASTT, OP_REF, REF_POS, REF_ROOT, compute_code_hash
from core.DBLayout import m_file_name, m_file_blob, m_bridge_file, m_bridge_tag, m_tag, m_ast, m_map_ast, m_moved_tag
from core.SymbolRows import load_prior_items as load_prior_symbol_items

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


# The blob of the file being parsed (m_bridge_tag.blob_id), resolved from the ChangeSet's own m_file_blob row
FILE_BLOB_REF = ((m_file_blob.table_id, 0), OP_REF, (REF_ROOT,))
# m_bridge_tag (6 cols) -> m_tag (5 cols) -> m_ast (3 cols): 14 columns, tag hash at 7, m_tag.tag_id at 6
_PRIOR_TAG_JOINS = (
    (m_bridge_tag.tag_id, m_tag.tag_id, 1),
    (m_tag.ast_id, m_ast.ast_id, 1),
)
PRIOR_TAG_COLUMNS = m_bridge_tag.length + m_tag.length + m_ast.length


def query_prior_tags(old_fid: int) -> list[tuple[Any, ...]] | None:
    """Tags of a previous-version blob joined with their m_tag and m_ast rows (`PRIOR_TAG_COLUMNS` columns)."""
    return m_bridge_tag.view_get_multiple(_PRIOR_TAG_JOINS, old_fid, *(None,) * (PRIOR_TAG_COLUMNS - 1))


def get_prior_tags(CS: Any, with_indexes: bool = True, with_symbols: bool = True) -> None:
    """Load the previous version's tags of the file and build their lookup indices.

    Always builds the code-hash index (`prior_tags_map`, Tier 1 recycling). `with_indexes` adds the name and
    position indices `match_prior_tag_transition()` uses; `with_symbols` loads the stored symbol items the
    recycled tags are compared with (core/SymbolRows.py), needed only by parsers that record symbols.
    """
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

    # The parsing worker prefetches the prior tags of a whole batch in one pass
    batch_tags = getattr(CS, "_batch_prior_tags", None)
    if batch_tags is not None and old_fid in batch_tags:
        CS.prior_tags = batch_tags[old_fid]
    else:
        CS.prior_tags = query_prior_tags(old_fid)

    if not CS.prior_tags:
        return
    if not with_indexes:
        for x, tag in enumerate(CS.prior_tags):
            if len(tag) > 7 and tag[7] is not None:
                CS.prior_tags_map.setdefault(tag[7], []).append((x, tag[1]))
        return
    if with_symbols:
        # Their stored symbol items, compared with those of the tags recycled below (core/SymbolRows.py)
        load_prior_symbol_items(CS, (tag[1] for tag in CS.prior_tags))

    # In-memory ast cache for fallback if caller provides shorter tuples
    ast_cache: dict[int, tuple[str, int | None]] = {}

    for x, tag in enumerate(CS.prior_tags):
        line_s = tag[2]
        line_e = tag[3]
        tag_id = tag[1] if len(tag) > 1 else tag[0]

        # Inverted Hash Index (Tier 1)
        if len(tag) >= 14:
            tag_hash = tag[7]
            ast_id = tag[8]
            ast_name = tag[12] or ""
            ast_type = tag[13]
        else:
            tag_hash = tag[9] if len(tag) > 9 else None
            ast_id = tag[10] if len(tag) > 10 else 0
            ast_name = tag[14] if len(tag) >= 16 else ""
            ast_type = tag[15] if len(tag) >= 16 else None

        if tag_hash:
            CS.prior_tags_map.setdefault(tag_hash, []).append((x, tag_id))

        if not ast_name and ast_id:
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
    """Tier 1: a prior tag with the same code hash not placed yet in this version (marked placed), else None."""
    lookup = getattr(CS, "prior_tags_map", None) if getattr(CS, "prior_tags", None) else None
    if not lookup or not code_hash:
        return None
    active = CS.active_tag_list
    for x, tag_id in lookup.get(code_hash, ()):
        if x not in active:
            if isinstance(active, set):
                active.add(x)
            else:
                active.append(x)
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


def stage_bridge_tag(CS: Any, tag: Any, line_pos: tuple[int, int], char_pos: tuple[int, int], positional: bool = False) -> None:
    """Place a tag (new tag route or recycled tag id) in the file's blob at the given coordinates."""
    op = m_bridge_tag.set(FILE_BLOB_REF, tag, line_pos[0], line_pos[1], char_pos[0], char_pos[1])
    if positional:
        with CS(REF_POS):
            CS.store(op)
    else:
        CS.store(op)


def stage_new_tag(CS: Any, code_hash: bytes, ast_ref: Any, s_tag_id: int | None = None, hl: tuple[int, int] = (0, 0)) -> Any:
    """Stage a new m_tag row (Rule 12: first operation of its REF_POS scope) and its transition from `s_tag_id`.

    `hl` is the tag's (hl_s, hl_l) highlight. Returns the tag's reference.
    """
    with CS(REF_POS):
        CS.store(m_tag.set(None, code_hash, ast_ref, hl[0], hl[1]))
        tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
        if s_tag_id is not None:
            CS.store(m_moved_tag.set(s_tag_id, tag_ref))
    return tag_ref


def stage_tag_map(CS: Any, tag: Any, ast_ref: Any, line_e: int, char_e: int, positional: bool = False) -> None:
    """Stage the tag's spatial map (m_map_ast keyed by the tag: map_id = tag_id), relative to the tag's start."""
    op = m_map_ast.set(tag, 1, 1, line_e, char_e, ast_ref)
    if positional:
        with CS(REF_POS):
            CS.store(op)
    else:
        CS.store(op)


def stage_entry_tag(
    CS: Any,
    ast_ref: Any,
    line_s: int,
    line_e: int,
    char_s: int,
    char_e: int,
    code: str,
    ast_name: str | None = None,
    ast_type: Any = None,
) -> Any:
    """Recycle (Tier 1) or create the tag of a whole entry (Kconfig, MAINTAINERS, CREDITS) with its map.

    Every row is staged positionally. Returns the recycled tag id or the new tag's reference.
    """
    from parser.c_ast.tokenizer import Line

    extent = Line(line_s, line_e)
    extent.line_pos = (line_s, line_e)
    extent.char_pos = (char_s, char_e)
    extent.code = code
    code_hash = compute_code_hash(code)
    if code:
        tag_id = check_exact_match(CS, code_hash)
        if tag_id is not None:
            stage_bridge_tag(CS, tag_id, extent.line_pos, extent.char_pos, positional=True)
            return tag_id

    s_tag_id = match_prior_tag_transition(CS, extent, ast_name, ast_type)
    tag_ref = stage_new_tag(CS, code_hash, ast_ref, s_tag_id)
    stage_bridge_tag(CS, tag_ref, extent.line_pos, extent.char_pos, positional=True)
    stage_tag_map(CS, tag_ref, ast_ref, max(1, line_e - line_s + 1), char_e, positional=True)
    return tag_ref
