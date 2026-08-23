"""DBLayout.py - Relational Database Schema & Table Definitions.

===============================================================================
RELATIONAL DATABASE SCHEMA REFERENCE GUIDE FOR AI & PARSERS
===============================================================================
This module defines the 15 core relational database tables used across the parser
pipeline (`Table_Array`).

SCHEMA ENTITY-RELATIONSHIP GRAPH:
-------------------------------------------------------------------------------
1. Version & File Lifecycle:
   m_v_main (vid, vname)
      ^
      |-- (vid_s, vid_e) --------> m_file (fid, vid_s, vid_e, ftype, s_stat, e_stat)
      |-- (vid) -----------------> m_bridge_file (vid, fnid, fid)
      |                                  ^            ^
   m_file_name (fnid, fname) -----------|            |-- (s_fid, e_fid) -> m_moved_file
      ^
      |-- (fnid) ----------------> m_ast_include (ast_id, fnid)

2. AST Hierarchy & Relationships:
   m_type_descriptor (type_id, name)
      ^
      |-- (type_id) -------------> m_ast (ast_id, name, type_id)
                                         ^
      |-- (ast_id, ref_ast_id) ---|------|-> m_ast_container (ast_id, priority, type_id, ref_ast_id)
      |-- (ast_id) ----------------------|-> m_ast_debug (ast_id, ast_raw)
      |-- (ast_id) ----------------------|-> m_ast_hash (hash, ast_id)

3. Code Tags & Source Coordinates:
   m_tag (tag_id, vid_s, vid_e, code, ast_id, hl_s, hl_l)
      ^           ^                     ^
      |           |--(vid_s, vid_e)     |--(ast_id -> m_ast.ast_id)
      |
      |-- (tag_id, fid -> m_file.fid) ---> m_bridge_tag (fid, tag_id, line_s, line_e, char_s, char_e)
      |-- (tag_id, map_id) --------------> m_bridge_map (tag_id, map_id)
                                                ^
   m_map_ast (map_id, line_s, char_s, line_e, char_e, ast_id) ----------------|
===============================================================================
"""
from core.globalstuff import ASTT
from core.TableHandling import Table

# Pre-populate AST type descriptor entries from ASTT enum
m_type_descriptor_insert = tuple((ast.value, ast.name) for ast in ASTT)

# -----------------------------------------------------------------------------
# 1. m_v_main (table_id=0): Version Registry
#    - vid: Unique Version ID (PK, AUTO_INCREMENT).
#    - vname: Release tag name (e.g. "v3.0", "v3.1").
# -----------------------------------------------------------------------------
m_v_main = Table(
    table_id=0,
    table_name="m_v_main",
    columns=(
        ("vid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("vname", "VARCHAR(32)", "NOT NULL", "COLLATE utf8mb4_bin"),
    ),
    primary=("vid",),
    foreign=None,
    initial_insert=((0, "latest"),),
    no_duplicate=True,
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 2. m_file_name (table_id=1): Unique Path String Registry
#    - fnid: Unique File Name ID (PK, AUTO_INCREMENT).
#    - fname: Relative file or directory path string.
# -----------------------------------------------------------------------------
m_file_name = Table(
    table_id=1,
    table_name="m_file_name",
    columns=(
        ("fnid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("fname", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
    ),
    primary=("fnid",),
    foreign=None,
    initial_insert=((0, ""),),
    no_duplicate=True,
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 3. m_file (table_id=2): File Lifecycle & Status Instance
#    - fid: Unique File Instance ID (PK, AUTO_INCREMENT).
#    - vid_s: Starting Version ID (FK -> m_v_main.vid).
#    - vid_e: Ending Version ID (FK -> m_v_main.vid, 0 if still active).
#    - ftype: File category (0: Dir, 1: C code/header, 2: Kconfig, 3: Rust).
#    - s_stat: Change status at inception ('A'=Added, 'M'=Modified, 'R'=Renamed).
#    - e_stat: Change status at conclusion ('D'=Deleted, 'R'=Renamed, '0'=Active).
# -----------------------------------------------------------------------------
m_file = Table(
    table_id=2,
    table_name="m_file",
    columns=(
        ("fid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("vid_s", "INT", "NOT NULL"),
        ("vid_e", "INT", "NOT NULL"),
        ("ftype", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("s_stat", "CHAR(1)", "NOT NULL"),
        ("e_stat", "CHAR(1)", "NOT NULL"),
    ),
    primary=("fid",),
    foreign=(("vid_s", "m_v_main", "vid"), ("vid_e", "m_v_main", "vid")),
    initial_insert=((0, 0, 0, 0, 0, 0),),
    no_duplicate=False,
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 4. m_bridge_file (table_id=3): Version to File Instance Mapping
#    - vid: Version ID (FK -> m_v_main.vid).
#    - fnid: File Name ID (FK -> m_file_name.fnid).
#    - fid: File Instance ID (FK -> m_file.fid).
# -----------------------------------------------------------------------------
m_bridge_file = Table(
    table_id=3,
    table_name="m_bridge_file",
    columns=(
        ("vid", "INT", "NOT NULL"),
        ("fnid", "INT", "NOT NULL"),
        ("fid", "INT", "NOT NULL"),
    ),
    primary=("vid", "fnid"),
    foreign=(
        ("vid", "m_v_main", "vid"),
        ("fnid", "m_file_name", "fnid"),
        ("fid", "m_file", "fid"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 5. m_moved_file (table_id=4): File Rename / Movement History
#    - s_fid: Source File Instance ID (FK -> m_file.fid).
#    - e_fid: Destination File Instance ID (FK -> m_file.fid).
# -----------------------------------------------------------------------------
m_moved_file = Table(
    table_id=4,
    table_name="m_moved_file",
    columns=(("s_fid", "INT", "NOT NULL"), ("e_fid", "INT", "NOT NULL")),
    primary=("s_fid", "e_fid"),
    foreign=(("s_fid", "m_file", "fid"), ("e_fid", "m_file", "fid")),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 6. m_type_descriptor (table_id=5): AST Construct Category Registry
#    - type_id: AST Type ID (PK, matched to ASTT enum value).
#    - name: AST Category Name (e.g. "C_structdecl", "C_Compound").
# -----------------------------------------------------------------------------
m_type_descriptor = Table(
    table_id=5,
    table_name="m_type_descriptor",
    columns=(
        ("type_id", "TINYINT", "UNSIGNED", "NOT NULL", "AUTO_INCREMENT"),
        ("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
    ),
    primary=("type_id",),
    foreign=None,
    initial_insert=m_type_descriptor_insert,
    no_duplicate=False,
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 7. m_ast (table_id=6): AST Node Elements
#    - ast_id: Unique AST Node ID (PK, AUTO_INCREMENT).
#    - name: Symbol identifier or expression string.
#    - type_id: AST Category ID (FK -> m_type_descriptor.type_id).
# -----------------------------------------------------------------------------
m_ast = Table(
    table_id=6,
    table_name="m_ast",
    columns=(
        ("ast_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("type_id", "TINYINT", "UNSIGNED", "NOT NULL"),
    ),
    primary=("ast_id",),
    foreign=(("type_id", "m_type_descriptor", "type_id"),),
    initial_insert=((0, "", 0),),
    no_duplicate=False,
    te_cached=False,
    hashing_table="m_ast_hash",
)

# -----------------------------------------------------------------------------
# 8. m_ast_container (table_id=7): AST Hierarchy & Child Relationships
#    - ast_id: Parent AST Node ID (FK -> m_ast.ast_id).
#    - priority: Positional rank/order index (0, 1, 2...).
#    - type_id: Child relationship type ID (FK -> m_type_descriptor.type_id).
#    - ref_ast_id: Referenced Child AST Node ID (FK -> m_ast.ast_id).
# -----------------------------------------------------------------------------
m_ast_container = Table(
    table_id=7,
    table_name="m_ast_container",
    columns=(
        ("ast_id", "INT", "NOT NULL"),
        ("priority", "SMALLINT", "UNSIGNED", "NOT NULL"),
        ("type_id", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("ref_ast_id", "INT", "NOT NULL"),
    ),
    primary=("ast_id", "priority"),
    foreign=(
        ("ast_id", "m_ast", "ast_id"),
        ("type_id", "m_type_descriptor", "type_id"),
        ("ref_ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 9. m_ast_include (table_id=8): Preprocessor Include References
#    - ast_id: Include directive AST Node ID (FK -> m_ast.ast_id).
#    - fnid: Target included file path ID (FK -> m_file_name.fnid).
# -----------------------------------------------------------------------------
m_ast_include = Table(
    table_id=8,
    table_name="m_ast_include",
    columns=(("ast_id", "INT", "NOT NULL"), ("fnid", "INT", "NOT NULL")),
    primary=("ast_id",),
    foreign=(("ast_id", "m_ast", "ast_id"), ("fnid", "m_file_name", "fnid")),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 10. m_ast_debug (table_id=9): AST Debug Dumps
#     - ast_id: AST Node ID (FK -> m_ast.ast_id).
#     - ast_raw: Serialized JSON representation of parser AST node.
# -----------------------------------------------------------------------------
m_ast_debug = Table(
    table_id=9,
    table_name="m_ast_debug",
    columns=(("ast_id", "INT", "NOT NULL"), ("ast_raw", "MEDIUMTEXT", "NOT NULL")),
    primary=("ast_id",),
    foreign=(("ast_id", "m_ast", "ast_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 11. m_tag (table_id=10): Code Occurrence / AST Tag Instance
#     - tag_id: Unique Tag ID (PK with vid_s).
#     - vid_s: Starting Version ID (FK -> m_v_main.vid).
#     - vid_e: Ending Version ID (FK -> m_v_main.vid, 0 if still active).
#     - code: Raw code snippet captured for this tag.
#     - ast_id: Associated AST Node ID (FK -> m_ast.ast_id).
#     - hl_s: Highlight start offset.
#     - hl_l: Highlight length.
# -----------------------------------------------------------------------------
m_tag = Table(
    table_id=10,
    table_name="m_tag",
    columns=(
        ("tag_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("vid_s", "INT", "NOT NULL"),
        ("vid_e", "INT", "NOT NULL"),
        ("code", "LONGTEXT", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
        ("hl_s", "INT", "NOT NULL"),
        ("hl_l", "INT", "NOT NULL"),
    ),
    primary=("tag_id", "vid_s"),
    foreign=(
        ("vid_s", "m_v_main", "vid"),
        ("vid_e", "m_v_main", "vid"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=(0, 0, 0, "", 0, 0, 0),
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 12. m_bridge_tag (table_id=11): Tag to File & Line Range Mapping
#     - fid: File Instance ID (FK -> m_file.fid).
#     - tag_id: Tag ID (FK -> m_tag.tag_id).
#     - line_s / line_e: Start / End line numbers in source file.
#     - char_s / char_e: Start / End character column offsets.
# -----------------------------------------------------------------------------
m_bridge_tag = Table(
    table_id=11,
    table_name="m_bridge_tag",
    columns=(
        ("fid", "INT", "NOT NULL"),
        ("tag_id", "INT", "NOT NULL"),
        ("line_s", "INT", "NOT NULL"),
        ("line_e", "INT", "NOT NULL"),
        ("char_s", "INT", "NOT NULL"),
        ("char_e", "INT", "NOT NULL"),
    ),
    primary=("fid", "tag_id"),
    foreign=(("fid", "m_file", "fid"), ("tag_id", "m_tag", "tag_id")),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 13. m_map_ast (table_id=12): Spatial Source Region to AST Mapping
#     - map_id: Map Set Grouping ID.
#     - line_s / char_s / line_e / char_e: Coordinate region relative to tag snippet.
#     - ast_id: Target AST Node ID (FK -> m_ast.ast_id).
# -----------------------------------------------------------------------------
m_map_ast = Table(
    table_id=12,
    table_name="m_map_ast",
    columns=(
        ("map_id", "INT", "NOT NULL"),
        ("line_s", "INT", "NOT NULL"),
        ("char_s", "INT", "NOT NULL"),
        ("line_e", "INT", "NOT NULL"),
        ("char_e", "INT", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("map_id", "line_s", "char_s", "line_e", "char_e", "ast_id"),
    foreign=(("ast_id", "m_ast", "ast_id"),),
    initial_insert=((0, 0, 0, 0, 0, 0),),
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 14. m_bridge_map (table_id=13): Code Tag to AST Coordinate Map Bridge
#     - tag_id: Code Tag ID (FK -> m_tag.tag_id).
#     - map_id: Map Set ID (matching m_map_ast.map_id).
# -----------------------------------------------------------------------------
m_bridge_map = Table(
    table_id=13,
    table_name="m_bridge_map",
    columns=(
        ("tag_id", "INT", "NOT NULL"),
        ("map_id", "INT", "NOT NULL"),
    ),
    primary=("tag_id", "map_id"),
    foreign=(("tag_id", "m_tag", "tag_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 15. m_ast_hash (table_id=14): AST Structural Hash Deduplication Registry
#     - hash: SHA-256 hex string of canonical AST node & children (PK).
#     - ast_id: Assigned AST Node ID (FK -> m_ast.ast_id).
# -----------------------------------------------------------------------------
m_ast_hash = Table(
    table_id=14,
    table_name="m_ast_hash",
    columns=(
        ("hash", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("hash",),
    foreign=(("ast_id", "m_ast", "ast_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    hashing_table=False,
)

TABLES: tuple[Table, ...] = (
    m_v_main,
    m_file_name,
    m_file,
    m_bridge_file,
    m_moved_file,
    m_type_descriptor,
    m_ast,
    m_ast_container,
    m_ast_include,
    m_ast_debug,
    m_tag,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
    m_ast_hash,
)


def init_db_layout(gp=None) -> tuple[Table, ...]:
    """Initialize and populate gp.Table_Array with the default 15 schema tables.
    
    Args:
        gp: Optional GreatProcessor instance to attach Table_Array to.
        
    Returns:
        Immutable tuple of all 15 Table schema objects.
    """
    if gp is not None:
        gp.Table_Array = list(TABLES)
    return TABLES
