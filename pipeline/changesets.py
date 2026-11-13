"""File-id lookups and ChangeSet helpers shared by the parse steps."""
from core.globalstuff import (
    G,
    type_check,
)

from core.TableHandling import ChangeSet
from core.DBLayout import (
    m_file_name,
    m_bridge_file,
    m_bridge_tag,
)
from pipeline.state import T_NO_FOREIGN, gp




file_fid_cache: dict[str, int | None] = {}


file_fnid_cache: dict[str, int | None] = {}


def get_fnid_for_path(path: str) -> int | None:
    """Resolve m_file_name.fnid for a given path."""
    if path in file_fnid_cache:
        return file_fnid_cache[path]
    te = getattr(G, "TE", None)
    if not te or m_file_name.table_id not in getattr(te, "tables", {}):
        return None
    fn_row = m_file_name.get(None, path)
    if fn_row and len(fn_row) >= 3 and fn_row[2]:
        fnid = fn_row[2][0]
        file_fnid_cache[path] = fnid
        return fnid
    file_fnid_cache[path] = None
    return None


def get_fid_for_path(path: str) -> int | None:
    """Resolve m_file.fid for a given path in active gp.VID (falling back to gp.Old_VID for unchanged files)."""
    if path in file_fid_cache:
        return file_fid_cache[path]
    if hasattr(gp, "file_blob_ids") and isinstance(gp.file_blob_ids, dict) and path in gp.file_blob_ids:
        fid = gp.file_blob_ids[path]
        file_fid_cache[path] = fid
        return fid
    te = getattr(G, "TE", None)
    if not te or m_bridge_file.table_id not in getattr(te, "tables", {}):
        return None
    fnid = get_fnid_for_path(path)
    if fnid is not None:
        bf_row = m_bridge_file.get(gp.VID, fnid, None)
        if (bf_row is None or len(bf_row) < 3 or not bf_row[2]) and getattr(gp, "Old_VID", 0) > 0:
            bf_row = m_bridge_file.get(gp.Old_VID, fnid, None)
        if bf_row and len(bf_row) >= 3 and bf_row[2]:
            fid = bf_row[2][2]
            file_fid_cache[path] = fid
            return fid
    file_fid_cache[path] = None
    return None


def extract_tags_and_evacuate_cs(cs_obj: ChangeSet) -> None:
    """Extract bridge tags, preserve resolved symbols, and purge internal AST buffers from an executed ChangeSet."""
    c_path = cs_obj.current_path
    if not c_path:
        return
    fid = get_fid_for_path(c_path)
    # ChangeSets executed straight from a worker batch (execute_and_purge) arrive with gp cleared
    gp_ref = cs_obj.gp or gp
    gp_ref.completed_paths.add(c_path)

    # 1. Preserve the resolved symbol map before internal buffers are cleared
    if cs_obj.symbol_dict:
        resolved_symbols = {}
        cs_res, cs_ops = cs_obj.cs_result, cs_obj.cs
        for sym_key, op_pos in cs_obj.symbol_dict.items():
            ast_id = None
            if op_pos < len(cs_res) and cs_res[op_pos] is not None:
                res_row = cs_res[op_pos]
                if res_row and isinstance(res_row[0], int):
                    ast_id = res_row[0]
            elif op_pos < len(cs_ops) and cs_ops[op_pos] is not None:
                op = cs_ops[op_pos]
                if len(op) >= 3 and op[2] and isinstance(op[2][0], int):
                    ast_id = op[2][0]
            if ast_id is not None:
                resolved_symbols[sym_key] = ast_id
        cs_obj.resolved_symbols = resolved_symbols

        # Per-file symbol tables for O(1) lookups by the files referencing this one
        gp_ref.file_symbols[c_path] = resolved_symbols
        gp_ref.file_names[c_path] = {s_name: a_id for (s_name, _), a_id in resolved_symbols.items()}
        gp_ref._ast_staged_symbols.update(resolved_symbols)
        staged_names = gp_ref._ast_staged_names
        for (s_name, _s_type), a_id in resolved_symbols.items():
            staged_names.setdefault(s_name, a_id)
        if gp_ref._ast_negative_cache:
            gp_ref._ast_negative_cache.difference_update(resolved_symbols)

    # 2. Extract bridge tags with resolved tag_id from cs_result
    if fid is not None and not hasattr(cs_obj, "pre_extracted_tags"):
        file_tags = []
        cs_res = cs_obj.cs_result
        for i, op in enumerate(cs_obj.cs):
            if op and len(op) >= 3 and op[0] == m_bridge_tag.table_id and len(op[2]) >= 4:
                cols = op[2]
                tag_id = None
                if i < len(cs_res) and cs_res[i] is not None and len(cs_res[i]) >= 2 and isinstance(cs_res[i][1], int):
                    tag_id = cs_res[i][1]
                if tag_id is None and not isinstance(cols[1], tuple):
                    tag_id = cols[1]
                if tag_id is not None:
                    line_s = cols[2] if isinstance(cols[2], int) else 1
                    line_e = cols[3] if isinstance(cols[3], int) else line_s
                    file_tags.append((tag_id, fid, line_s, line_e))
        cs_obj.pre_extracted_tags = file_tags

    blob_id = getattr(cs_obj, "blob_id", 0) or fid or 0
    if blob_id:
        cs_obj.blob_id = blob_id
        gp_ref.file_blob_ids[c_path] = blob_id

    if getattr(cs_obj, "pre_extracted_tags", None):
        gp_ref.file_pre_extracted_tags[c_path] = cs_obj.pre_extracted_tags

    # 3. Evacuate internal AST buffers and bloat
    cs_obj.cs.clear()
    cs_obj.store_dict.clear()
    cs_obj.cs_result.clear()
    cs_obj.symbol_dict.clear()
    cs_obj.foreign_deps.clear()
    if cs_obj.unresolved_indices:
        cs_obj.unresolved_indices.clear()
    cs_obj.clear_bloat()

    # Record worker micro-profiler into central StageProfiler if active
    if G.STAGE_PROFILER is not None and cs_obj.profiler is not None:
        G.STAGE_PROFILER.record_worker_profiler(cs_obj.profiler)
        cs_obj.profiler = None


def execute_and_purge(cs_dict: dict) -> dict:
    """Execute non-foreign ChangeSets immediately and purge internal AST buffers to minimize RAM usage."""
    for CS in cs_dict.values():
        if not CS:
            continue
        c_path = getattr(CS, "current_path", None)
        if c_path and type_check(c_path) in T_NO_FOREIGN:
            if CS.execute():
                extract_tags_and_evacuate_cs(CS)
    return cs_dict
