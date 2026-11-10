"""db_engine/views.py - Canonical Database View Definitions and Registry.

Maintains relational database views in strict dependency order for parser
and webapp consumers.
"""
from __future__ import annotations

# def_id bit-packing constants (must remain unified across parser and views)
DEF_ID_FLAG = 1 << 62
KIND_DEFS = 0
KIND_REFS = 1

# Checkpoint tree view: resolves full trees through parent deltas
TREE_VIEW_SQL = (
    "CREATE OR REPLACE VIEW m_checkpoint_tree AS "
    "SELECT c.commit_id, c.fnid, c.blob_id FROM m_bridge_checkpoint c "
    "WHERE c.blob_id <> 0 OR NOT EXISTS (SELECT 1 FROM m_checkpoint_base b0 WHERE b0.commit_id = c.commit_id) "
    "UNION ALL "
    "SELECT b.commit_id, k.fnid, k.blob_id FROM m_checkpoint_base b "
    "JOIN m_bridge_checkpoint k ON k.commit_id = b.base_commit_id "
    "WHERE NOT EXISTS (SELECT 1 FROM m_bridge_checkpoint d WHERE d.commit_id = b.commit_id AND d.fnid = k.fnid);"
)

_OWN = (
    "NOT EXISTS (SELECT 1 FROM m_symbol_own o "
    "WHERE o.blob_id = bt.blob_id AND o.tag_id = bt.tag_id AND o.kind = {kind})"
)

_VIEW_PARTS: dict[str, tuple[str, str, str, int]] = {
    "m_symbol_def": (
        "d.def_id, d.blob_id, d.tag_id, d.ast_id, d.name, d.type_id, d.linkage, d.line_s, d.line_e FROM m_blob_symbol_def d",
        f"CAST({DEF_ID_FLAG} | (bt.blob_id << 37) | (ti.tag_id << 6) | ti.def_idx AS SIGNED) AS def_id, "
        "bt.blob_id, ti.tag_id, bs.target_ast_id AS ast_id, ti.name, ti.type_id, ti.linkage, "
        "bt.line_s + ti.rel_line_s AS line_s, bt.line_s + ti.rel_line_e AS line_e",
        "m_tag_def",
        KIND_DEFS,
    ),
    "m_symbol_ref": (
        "r.ref_id, r.blob_id, r.tag_id, r.ast_id, r.target_fnid, r.role, r.line, r.char_s FROM m_blob_symbol_ref r",
        "NULL AS ref_id, bt.blob_id, ti.tag_id, bs.target_ast_id AS ast_id, COALESCE(bf.target_fnid, 0) AS target_fnid, "
        "ti.role, bt.line_s + ti.rel_line AS line, ti.char_s",
        "m_tag_ref",
        KIND_REFS,
    ),
}

_FILE_JOIN = {
    "m_symbol_def": "",
    "m_symbol_ref": "LEFT JOIN m_blob_symbol_file bf ON bf.blob_id = bs.blob_id AND bf.sym_key = bs.sym_key ",
}

SYMBOL_VIEW_SQL: dict[str, str] = {
    view: (
        f"CREATE OR REPLACE VIEW {view} AS SELECT {blob_cols} UNION ALL SELECT {shared_cols} "
        f"FROM m_bridge_tag bt JOIN {tag_table} ti ON ti.tag_id = bt.tag_id "
        "JOIN m_blob_symbol bs ON bs.blob_id = bt.blob_id AND bs.sym_key = ti.sym_key "
        f"{_FILE_JOIN[view]}"
        f"WHERE {_OWN.format(kind=kind)};"
    )
    for view, (blob_cols, shared_cols, tag_table, kind) in _VIEW_PARTS.items()
}

# Strict dependency order for view creation
VIEW_DEFINITIONS: list[tuple[str, str]] = [
    (
        "m_v_main",
        "CREATE OR REPLACE VIEW m_v_main AS "
        "SELECT commit_id AS vid, ref_name AS vname, repo_id, ref_type, tagger_id, tagger_date, tag_message "
        "FROM m_git_ref;",
    ),
    (
        "m_checkpoint_tree",
        TREE_VIEW_SQL,
    ),
    (
        "m_bridge_file",
        "CREATE OR REPLACE VIEW m_bridge_file AS "
        "SELECT commit_id AS vid, fnid, blob_id AS fid FROM m_checkpoint_tree;",
    ),
    (
        "m_file",
        "CREATE OR REPLACE VIEW m_file AS "
        "SELECT fb.blob_id AS fid, "
        "COALESCE((SELECT MIN(bc.commit_id) FROM m_bridge_checkpoint bc WHERE bc.blob_id = fb.blob_id), 0) AS vid_s, "
        "0 AS vid_e, fb.ftype, '0' AS s_stat, '0' AS e_stat, fb.blob_hash "
        "FROM m_file_blob fb;",
    ),
    (
        "m_bridge_commit_file",
        "CREATE OR REPLACE VIEW m_bridge_commit_file AS "
        "SELECT commit_id, fnid, blob_id, 0 AS old_fnid, 0 AS old_blob_id, change_type "
        "FROM m_commit_file;",
    ),
    (
        "m_symbol_def",
        SYMBOL_VIEW_SQL["m_symbol_def"],
    ),
    (
        "m_symbol_ref",
        SYMBOL_VIEW_SQL["m_symbol_ref"],
    ),
]

ORDERED_VIEW_NAMES: tuple[str, ...] = tuple(name for name, _ in VIEW_DEFINITIONS)


def extract_select_sql(ddl: str) -> str:
    """Extract SELECT statement from CREATE VIEW DDL."""
    if " AS " in ddl:
        return ddl.split(" AS ", 1)[1].rstrip(";").strip()
    return ddl.rstrip(";").strip()

