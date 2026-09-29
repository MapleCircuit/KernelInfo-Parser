"""unit_test/parser_harness.py - Reusable ChangeSet Snapshot Extraction & Verification Harness.

Provides parser-agnostic extraction of fully-resolved ChangeSet operations into
canonical, order-agnostic, ID-independent snapshots for gold-master verification.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Sequence
import unittest

from core.globalstuff import (
    G,
    OP_DONE,
    OP_SET,
    OP_UPDATE,
    OP_VIEW_DONE,
    OP_VIEW_SET,
    OP_REF_VIEW,
    REF_OLD,
    REF_ROOT,
    T_DIR,
    T_C,
    T_KCONFIG,
    T_RUST,
    T_ASM,
    T_MAINTAINERS,
    T_CREDITS,
    T_RAW,
)
from core.TableHandling import ChangeSet, Table, is_data_unsafe, to_safe_data
import core.DBLayout as gp

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"

FTYPE_MAP: dict[int, str] = {
    T_DIR: "T_DIR",
    T_C: "T_C",
    T_KCONFIG: "T_KCONFIG",
    T_RUST: "T_RUST",
    T_ASM: "T_ASM",
    T_MAINTAINERS: "T_MAINTAINERS",
    T_CREDITS: "T_CREDITS",
    T_RAW: "T_RAW",
}


def serialize_scalar(val: Any, col_name: str | None = None) -> Any:
    """Serialize scalar value into JSON-friendly format.
    
    Converts bytes to 'hex:<hex_string>' and enums/objects to primitives.
    Formats 'type_id' columns as '<id> (<name>)' (e.g. '144 (Raw_Content)').
    Formats 'ftype' columns as '<id> (<name>)' (e.g. '7 (T_RAW)').
    """
    if isinstance(val, (bytes, bytearray, memoryview)):
        return f"hex:{bytes(val).hex()}"
    if col_name == "type_id" and val is not None:
        from core.globalstuff import ASTT
        try:
            if isinstance(val, ASTT):
                return f"{val.value} ({val.name})"
            int_val = int(val)
            name = ASTT(int_val).name
            return f"{int_val} ({name})"
        except (ValueError, TypeError):
            pass
    if col_name == "ftype" and val is not None:
        try:
            int_val = int(val)
            name = FTYPE_MAP.get(int_val)
            if name:
                return f"{int_val} ({name})"
            return int_val
        except (ValueError, TypeError):
            pass
    return to_safe_data(val)


def stage_file_prelude(cs: ChangeSet, gp_inst: Any) -> None:
    """Stage standard file lifecycle prelude operations (m_file_name, m_file, m_bridge_file, etc.).
    
    Standardized prelude equivalent to main.py default_processing(), shared across
    unit tests for all AST and raw parsers.
    """
    op = cs.file_operation
    ftype = cs.get_file_type() if hasattr(cs, "get_file_type") else 7

    if op == "A":
        cs.store(gp.m_file_name.get_set(None, cs.current_path))
        cs.store(gp.m_file.set(None, gp_inst.VID, 0, ftype, "A", 0))
        cs.store(gp.m_bridge_file.set(gp_inst.VID, cs.ref(gp.m_file_name.fnid), cs.ref(gp.m_file.fid)))
    elif op == "M":
        cs.store(gp.m_file_name.get_set(None, cs.current_path))
        with cs(REF_OLD):
            cs.store(gp.m_bridge_file.view(
                ((gp.m_bridge_file.fnid, gp.m_file_name.fnid, 1),),
                gp_inst.Old_VID,
                cs.ref(gp.m_file_name.fnid, REF_ROOT),
                None,
                None,
                cs.current_path,
            ))
            cs.store(gp.m_file.update(
                cs.ref(gp.m_bridge_file.fid),
                None,
                gp_inst.Old_VID,
                None,
                None,
                "M",
            ))
        cs.store(gp.m_file.set(None, gp_inst.VID, 0, ftype, "M", 0))
        cs.store(gp.m_bridge_file.set(gp_inst.VID, cs.ref(gp.m_file_name.fnid), cs.ref(gp.m_file.fid)))
    elif op == "R100":
        with cs(REF_OLD):
            cs.store(gp.m_file_name.get_set(None, cs.old_path))
            cs.store(gp.m_bridge_file.view(
                ((gp.m_bridge_file.fnid, gp.m_file_name.fnid, 1),),
                gp_inst.Old_VID,
                cs.ref(gp.m_file_name.fnid),
                None,
                None,
                cs.old_path,
            ))
        cs.store(gp.m_file_name.get_set(None, cs.current_path))
        cs.store(gp.m_bridge_file.set(
            gp_inst.VID,
            cs.ref(gp.m_file_name.fnid),
            cs.ref(gp.m_bridge_file.fid, REF_OLD),
        ))


def extract_changeset_snapshot(
    cs: ChangeSet | Sequence[ChangeSet],
    test_conf: dict[str, Any] | None = None,
    tables: Sequence[Table] | None = None,
    filter_tables: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Extract all data operations from completely resolved ChangeSet(s) into a canonical snapshot.
    
    Accepts either a single ChangeSet or a sequence of ChangeSets (e.g. across multi-version
    lifecycles [cs_v1, cs_v2]).
    
    Auto-increment surrogate IDs (ast_id, tag_id, fid, fnid, map_id, etc.) are normalized
    into deterministic symbolic identifiers ($ast_0, $tag_0, $fid_0, etc.) preserving
    relational integrity while rendering the snapshot completely order- and sequence-agnostic.
    Version IDs (vid) are preserved as concrete version sequence integers.
    
    Args:
        cs: Single ChangeSet or sequence of ChangeSets.
        test_conf: Metadata dictionary detailing parser, test_name, operation, etc.
        tables: Optional schema collection (defaults to TABLES from DBLayout).
        filter_tables: Optional subset of table names to include (None includes all).
        
    Returns:
        Dictionary with 'test_conf' and 'tables' mapping table names to deterministically sorted row lists.
    """
    css: list[ChangeSet] = [cs] if isinstance(cs, ChangeSet) else list(cs)
    if not css:
        return {"test_conf": test_conf or {}, "tables": {}}

    primary_gp = getattr(css[0], "gp", None)
    if tables is None:
        tables = getattr(primary_gp, "Table_Array", None) or gp.TABLES

    table_map: dict[int, Table] = {t.table_id: t for t in tables}
    raw_tables: dict[str, list[dict[str, Any]]] = {}

    for c in css:
        if not c.cs_processed or len(c.cs_result) < len(c.cs):
            success = c.execute()
            if not success:
                raise ValueError(f"ChangeSet for {c.current_path} could not be completely resolved.")

        for idx, op in enumerate(c.cs):
            target = op[0]
            op_type = op[1]
            op_data = op[2]
            result = c.cs_result[idx] if idx < len(c.cs_result) else None

            table: Table | None = None
            full_row: tuple[Any, ...] | None = None

            if op_type == OP_REF_VIEW:
                unpacked = c._unpack_ref_view(op)
                if unpacked is not None:
                    target, op_type, op_data = unpacked[0], unpacked[1], unpacked[2]

            if op_type == OP_SET:
                if isinstance(target, int) and target in table_map:
                    table = table_map[target]
                    resolved = c._resolve_ref_from_tuple(op_data) if is_data_unsafe(op_data) else op_data
                    if table.has_auto_increment:
                        if resolved[0] is None:
                            assigned_id = result[0] if (isinstance(result, (tuple, list)) and len(result) > 0) else result
                            full_row = (assigned_id, *resolved[1:])
                        else:
                            full_row = resolved
                    else:
                        full_row = resolved

            elif op_type == OP_UPDATE:
                if isinstance(target, int) and target in table_map:
                    table = table_map[target]
                    full_row = c._resolve_ref_from_tuple(op_data) if is_data_unsafe(op_data) else op_data

            elif op_type == OP_VIEW_SET:
                # target is joins: JoinsType. Root table is target[0][0][0]
                if isinstance(target, tuple) and len(target) > 0 and isinstance(target[0], tuple):
                    root_table_id = target[0][0][0]
                    if root_table_id in table_map:
                        table = table_map[root_table_id]
                        resolved = c._resolve_ref_from_tuple(op_data) if is_data_unsafe(op_data) else op_data
                        assigned_id = result[0] if (isinstance(result, (tuple, list)) and len(result) > 0) else result
                        if table.has_auto_increment and resolved[0] is None:
                            full_row = (assigned_id, *resolved[1:])
                        else:
                            full_row = resolved

            elif op_type in (OP_DONE, OP_VIEW_DONE):
                if isinstance(target, int) and target in table_map:
                    table = table_map[target]
                    full_row = result if result is not None else op_data

            if table is None or full_row is None:
                continue

            if filter_tables is not None and table.table_name not in filter_tables:
                continue

            row_dict: dict[str, Any] = {}
            for col_idx, col_def in enumerate(table.init_columns):
                col_name = col_def[0]
                val = full_row[col_idx] if col_idx < len(full_row) else None
                row_dict[col_name] = serialize_scalar(val, col_name=col_name)

            if table.table_name not in raw_tables:
                raw_tables[table.table_name] = []

            if op_type == OP_UPDATE:
                pk_cols = [table.init_columns[i][0] for i in table.primary] if table.primary else []
                updated = False
                if pk_cols:
                    for existing in raw_tables[table.table_name]:
                        if all(existing.get(pk) == row_dict.get(pk) for pk in pk_cols if row_dict.get(pk) is not None):
                            for k, v in row_dict.items():
                                if v is not None:
                                    existing[k] = v
                            updated = True
                            break
                if not updated and row_dict not in raw_tables[table.table_name]:
                    raw_tables[table.table_name].append(row_dict)
            else:
                if row_dict not in raw_tables[table.table_name]:
                    raw_tables[table.table_name].append(row_dict)

    # Normalize surrogate IDs into symbolic references and preserve DB schema order
    normalized_tables = normalize_snapshot_ids(raw_tables, css, table_map)
    return {
        "test_conf": test_conf or {},
        "tables": normalized_tables,
    }


def normalize_snapshot_ids(
    raw_tables: dict[str, list[dict[str, Any]]],
    css: list[ChangeSet],
    table_map: dict[int, Table] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Normalize surrogate sequential IDs to symbolic tokens based on natural keys.
    
    Preserves exact DB table schema column order within every row dict and orders
    tables by their canonical table_id.
    """
    table_by_name: dict[str, Table] = {t.table_name: t for t in table_map.values()} if table_map else {}
    ast_id_map: dict[Any, str] = {}
    tag_id_map: dict[Any, str] = {}
    fid_map: dict[Any, str] = {}
    fnid_map: dict[Any, str] = {}
    prior_tag_map: dict[Any, str] = {}
    prior_ast_map: dict[Any, str] = {}

    # 1. m_ast normalization: natural key is (name, type_id)
    if "m_ast" in raw_tables:
        sorted_ast = sorted(
            raw_tables["m_ast"],
            key=lambda r: (str(r.get("name")), str(r.get("type_id"))),
        )
        for i, row in enumerate(sorted_ast):
            concrete_id = row.get("ast_id")
            if concrete_id is not None:
                ast_id_map[concrete_id] = f"$ast_{i}"

    # 2. Prior tags mapping (collected across all ChangeSets with prior_tags)
    for c in css:
        if getattr(c, "prior_tags", None):
            sorted_prior = sorted(
                c.prior_tags,
                key=lambda t: (str(t[9]) if len(t) > 9 else "", str(t[2]) if len(t) > 2 else ""),
            )
            for j, ptag in enumerate(sorted_prior):
                ptag_id = ptag[6] if len(ptag) > 6 and ptag[6] is not None else (ptag[1] if len(ptag) > 1 else ptag[0])
                if ptag_id is not None and ptag_id not in prior_tag_map:
                    prior_tag_map[ptag_id] = f"$prior_tag_{j}"
                if len(ptag) > 10 and ptag[10] is not None and ptag[10] not in prior_ast_map:
                    prior_ast_map[ptag[10]] = f"$prior_ast_{j}"

    # 3. m_tag normalization: natural key is (hash, vid_s, vid_e, hl_s, hl_l)
    if "m_tag" in raw_tables:
        sorted_tag = sorted(
            raw_tables["m_tag"],
            key=lambda r: (
                str(r.get("hash")),
                int(r.get("vid_s") or 0),
                int(r.get("vid_e") or 0),
                int(r.get("hl_s") or 0),
                int(r.get("hl_l") or 0),
            ),
        )
        tag_counter = 0
        for row in sorted_tag:
            concrete_id = row.get("tag_id")
            if concrete_id is not None:
                if concrete_id in prior_tag_map:
                    tag_id_map[concrete_id] = prior_tag_map[concrete_id]
                else:
                    tag_id_map[concrete_id] = f"$tag_{tag_counter}"
                    tag_counter += 1

    # 4. m_file normalization: natural key is (vid_s, vid_e, ftype)
    if "m_file" in raw_tables:
        sorted_file = sorted(
            raw_tables["m_file"],
            key=lambda r: (int(r.get("vid_s") or 0), int(r.get("vid_e") or 0), str(r.get("ftype"))),
        )
        for i, row in enumerate(sorted_file):
            concrete_id = row.get("fid")
            if concrete_id is not None:
                fid_map[concrete_id] = f"$fid_{i}"

    # 5. m_file_name normalization: natural key is fname
    if "m_file_name" in raw_tables:
        sorted_fn = sorted(raw_tables["m_file_name"], key=lambda r: str(r.get("fname")))
        for i, row in enumerate(sorted_fn):
            concrete_id = row.get("fnid")
            if concrete_id is not None:
                fnid_map[concrete_id] = f"$fnid_{i}"

    # Replace surrogate IDs in all tables
    normalized: dict[str, list[dict[str, Any]]] = {}

    for tname, rows in raw_tables.items():
        new_rows = []
        for r in rows:
            new_r = dict(r)
            # AST references
            if "ast_id" in new_r:
                aid = new_r["ast_id"]
                if aid in ast_id_map:
                    new_r["ast_id"] = ast_id_map[aid]
                elif aid in prior_ast_map:
                    new_r["ast_id"] = prior_ast_map[aid]
            if "ref_ast_id" in new_r:
                raid = new_r["ref_ast_id"]
                if raid in ast_id_map:
                    new_r["ref_ast_id"] = ast_id_map[raid]
                elif raid in prior_ast_map:
                    new_r["ref_ast_id"] = prior_ast_map[raid]

            # Tag references
            if "tag_id" in new_r:
                tid = new_r["tag_id"]
                if tid in tag_id_map:
                    new_r["tag_id"] = tag_id_map[tid]
                elif tid in prior_tag_map:
                    new_r["tag_id"] = prior_tag_map[tid]

            # Spatial map references
            if "map_id" in new_r:
                mid = new_r["map_id"]
                if mid in tag_id_map:
                    new_r["map_id"] = tag_id_map[mid]
                elif mid in prior_tag_map:
                    new_r["map_id"] = prior_tag_map[mid]

            # Moved tag transitions
            if "s_tag_id" in new_r:
                sid = new_r["s_tag_id"]
                if sid in prior_tag_map:
                    new_r["s_tag_id"] = prior_tag_map[sid]
                elif sid in tag_id_map:
                    new_r["s_tag_id"] = tag_id_map[sid]
            if "e_tag_id" in new_r:
                eid = new_r["e_tag_id"]
                if eid in tag_id_map:
                    new_r["e_tag_id"] = tag_id_map[eid]
            # File references
            if "fid" in new_r and new_r["fid"] in fid_map:
                new_r["fid"] = fid_map[new_r["fid"]]
            if "fnid" in new_r and new_r["fnid"] in fnid_map:
                new_r["fnid"] = fnid_map[new_r["fnid"]]

            # Reorder keys in exact DB table schema column order
            table_obj = table_by_name.get(tname)
            if table_obj and hasattr(table_obj, "init_columns"):
                ordered_r = {}
                for col_def in table_obj.init_columns:
                    col_name = col_def[0]
                    if col_name in new_r:
                        ordered_r[col_name] = new_r[col_name]
                for k, v in new_r.items():
                    if k not in ordered_r:
                        ordered_r[k] = v
                new_rows.append(ordered_r)
            else:
                new_rows.append(new_r)

        # Sort rows deterministically by their canonical JSON representation
        new_rows.sort(key=lambda item: json.dumps(item, sort_keys=True))
        normalized[tname] = new_rows

    # Sort entire dict by canonical DB table_id order
    return dict(sorted(normalized.items(), key=lambda kv: table_by_name[kv[0]].table_id if kv[0] in table_by_name else 999))


def compare_snapshots(
    actual: dict[str, Any],
    expected: dict[str, Any],
) -> tuple[bool, str]:
    """Order-agnostic comparison between actual and expected snapshots.
    
    Validates both 'test_conf' and 'tables'.
    
    Returns:
        (is_match, diff_explanation)
    """
    diff_lines = []

    # 1. Compare test_conf
    act_conf = actual.get("test_conf", {})
    exp_conf = expected.get("test_conf", {})
    if act_conf != exp_conf:
        diff_lines.append("--- Mismatch in 'test_conf' ---")
        diff_lines.append(f"  Expected: {json.dumps(exp_conf, sort_keys=True)}")
        diff_lines.append(f"  Actual:   {json.dumps(act_conf, sort_keys=True)}")

    # 2. Compare tables
    act_tables = actual.get("tables", {})
    exp_tables = expected.get("tables", {})

    actual_table_names = set(act_tables.keys())
    expected_table_names = set(exp_tables.keys())

    missing_tables = expected_table_names - actual_table_names
    extra_tables = actual_table_names - expected_table_names

    if missing_tables:
        diff_lines.append(f"Missing expected tables in snapshot: {sorted(missing_tables)}")
    if extra_tables:
        diff_lines.append(f"Unexpected extra tables in snapshot: {sorted(extra_tables)}")

    for tname in sorted(actual_table_names & expected_table_names):
        act_rows = act_tables[tname]
        exp_rows = exp_tables[tname]

        if act_rows != exp_rows:
            diff_lines.append(f"\n--- Mismatch in table '{tname}' ---")
            diff_lines.append(f"  Expected row count: {len(exp_rows)}")
            diff_lines.append(f"  Actual row count:   {len(act_rows)}")

            # Row-by-row multiset diff
            act_unmatched = list(act_rows)
            exp_unmatched = list(exp_rows)

            for r in exp_rows:
                if r in act_unmatched:
                    act_unmatched.remove(r)
                    exp_unmatched.remove(r)

            if exp_unmatched:
                diff_lines.append(f"  Rows expected but NOT found ({len(exp_unmatched)}):")
                for r in exp_unmatched[:5]:
                    diff_lines.append(f"    - {json.dumps(r, sort_keys=True)}")
                if len(exp_unmatched) > 5:
                    diff_lines.append(f"    ... and {len(exp_unmatched) - 5} more")

            if act_unmatched:
                diff_lines.append(f"  Rows produced but NOT expected ({len(act_unmatched)}):")
                for r in act_unmatched[:5]:
                    diff_lines.append(f"    + {json.dumps(r, sort_keys=True)}")
                if len(act_unmatched) > 5:
                    diff_lines.append(f"    ... and {len(act_unmatched) - 5} more")

    if diff_lines:
        return False, "\n".join(diff_lines)
    return True, ""


def get_snapshot_path(parser_name: str, test_name: str) -> Path:
    """Return the absolute Path to the snapshot JSON file."""
    return SNAPSHOT_DIR / parser_name / f"{test_name}.json"


def load_snapshot(parser_name: str, test_name: str) -> dict[str, Any] | None:
    """Load baseline snapshot JSON file if present, else None."""
    path = get_snapshot_path(parser_name, test_name)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(
    parser_name: str,
    test_name: str,
    data: dict[str, Any],
) -> Path:
    """Save normalized snapshot data to JSON baseline file."""
    path = get_snapshot_path(parser_name, test_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
    return path


def assert_snapshot_matches(
    test_case: unittest.TestCase,
    parser_name: str,
    test_name: str,
    actual_data: dict[str, Any],
    update_if_env: bool = True,
) -> None:
    """Assert actual extracted ChangeSet data matches saved snapshot baseline."""
    update_mode = update_if_env and os.environ.get("UPDATE_SNAPSHOTS") == "1"
    snap_path = get_snapshot_path(parser_name, test_name)

    if update_mode:
        save_snapshot(parser_name, test_name, actual_data)
        return

    expected_data = load_snapshot(parser_name, test_name)
    if expected_data is None:
        save_snapshot(parser_name, test_name, actual_data)
        test_case.fail(
            f"Snapshot baseline missing for {parser_name}/{test_name}. "
            f"Wrote initial snapshot to {snap_path}. Please inspect and re-run."
        )

    is_match, diff = compare_snapshots(actual_data, expected_data)
    if not is_match:
        test_case.fail(
            f"Snapshot mismatch for {parser_name}/{test_name}:\n{diff}\n"
            f"(Run with UPDATE_SNAPSHOTS=1 to overwrite baseline if intentional)"
        )
