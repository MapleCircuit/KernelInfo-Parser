"""DBLayout.py - Relational Database Schema & Table Definitions.

===============================================================================
RELATIONAL DATABASE SCHEMA REFERENCE GUIDE FOR AI & PARSERS
===============================================================================
This module defines the 30 core relational database tables used across the parser
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
   m_tag_code (hash, code)
      ^
      |-- (hash) ------------------> m_tag (tag_id, vid_s, vid_e, hash, ast_id, hl_s, hl_l)
                                        ^           ^                     ^
                                        |           |--(vid_s, vid_e)     |--(ast_id -> m_ast.ast_id)
                                        |
                                        |-- (tag_id, fid -> m_file.fid) ---> m_bridge_tag (fid, tag_id, line_s, line_e, char_s, char_e)
                                        |-- (tag_id, map_id) --------------> m_bridge_map (tag_id, map_id)
                                                  ^
   m_map_ast (map_id, line_s, char_s, line_e, char_e, ast_id) ----------------|

4. Kconfig Relational Acceleration & Dependency Graph:
   m_kconfig_symbol (kcid, vid_s, vid_e, name, type, prompt, def_val, help, ast_id -> m_ast.ast_id)
      ^            ^
      |            |-- (kcid) ---------> m_kconfig_relation (kcid, target_name, rel_type, cond_ast_id, priority)
      |
      |-- (kcid) ----------------------> m_kconfig_tree (tree_id, vid, parent_id, node_type, title, kcid, priority, dep_ast_id, ast_id)
      |
      |-- (kcid, fid) -----------------> m_kconfig_kbuild (kcid, vid, fid, compile_mode, target_obj)

5. Maintainer & Ownership Subsystem:
   m_v_main (vid, vname)
      ^
      |-- (vid_s, vid_e) ----------> m_maintainer_section (sec_id, vid_s, vid_e, name, status, scm_tree, web_page, mailing_list, ast_id)
      |                                     ^                   ^
      |                                     |                   |-- (sec_id) -> m_maintainer_pattern (sec_id, pat_type, pattern, priority)
      |                                     |                   |
      |                                     |                   |-- (sec_id) -> m_maintainer_member (sec_id, person_id, role_type, priority)
      |                                     |                                         ^
      |                                     |                                         |-- (person_id) -> m_maintainer_person (person_id, name, email)
      |                                     |                                         |
      |-- (vid, fid, sec_id) ---------------> m_maintainer_file (vid, fid, sec_id)    |
      |                                        ^                                      |
      |-- (vid_s, vid_e, person_id) --------> m_credits_entry (credit_id, vid_s, ...) -|
                                               ^
   m_file (fid, ...) --------------------------|

6. Git Commit, Multi-Contributor & Multi-Commit Tag Subsystem:
   m_v_main (vid, vname)
      ^
      |-- (vid) ---------------------> m_commit (commit_id, vid, commit_hash, author_id, author_date,
      |                                          committer_id, committer_date, subject, message)
      |                                  ^              ^              ^
   m_maintainer_person (person_id) ------| (author_id)  | (committer)  |
      ^                                                                |
      |-- (person_id) <------- m_bridge_commit_person (commit_id, person_id, role_type, priority)
                                         ^
   m_file (fid, ...) --------------------|--------> m_bridge_commit_file (commit_id, vid, fid, change_type)
                                         |
   m_tag (tag_id, ...) ------------------|--------> m_bridge_commit_tag (commit_id, vid, fid, tag_id)
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
# 11. m_tag_code (table_id=10): Code Snippet Text Registry
#     - hash: 32-byte binary SHA-256 digest of code snippet string (PK).
#     - code: Raw code snippet text content.
# -----------------------------------------------------------------------------
m_tag_code = Table(
    table_id=10,
    table_name="m_tag_code",
    columns=(
        ("hash", "BINARY(32)", "NOT NULL"),
        ("code", "LONGTEXT", "NOT NULL"),
    ),
    primary=("hash",),
    foreign=None,
    initial_insert=((b"\x00" * 32, ""),),
    no_duplicate=False,
    te_cached=("hash",),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 12. m_tag (table_id=11): Code Occurrence / AST Tag Instance
#     - tag_id: Unique Tag ID (PK with vid_s).
#     - vid_s: Starting Version ID (FK -> m_v_main.vid).
#     - vid_e: Ending Version ID (FK -> m_v_main.vid, 0 if still active).
#     - hash: Associated code snippet SHA-256 hash (FK -> m_tag_code.hash).
#     - ast_id: Associated AST Node ID (FK -> m_ast.ast_id).
#     - hl_s: Highlight start offset.
#     - hl_l: Highlight length.
# -----------------------------------------------------------------------------
m_tag = Table(
    table_id=11,
    table_name="m_tag",
    columns=(
        ("tag_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("vid_s", "INT", "NOT NULL"),
        ("vid_e", "INT", "NOT NULL"),
        ("hash", "BINARY(32)", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
        ("hl_s", "INT", "NOT NULL"),
        ("hl_l", "INT", "NOT NULL"),
    ),
    primary=("tag_id", "vid_s"),
    foreign=(
        ("vid_s", "m_v_main", "vid"),
        ("vid_e", "m_v_main", "vid"),
        ("hash", "m_tag_code", "hash"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=((0, 0, 0, b"\x00" * 32, 0, 0, 0),),
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 13. m_bridge_tag (table_id=12): Tag to File & Line Range Mapping
#     - fid: File Instance ID (FK -> m_file.fid).
#     - tag_id: Tag ID (FK -> m_tag.tag_id).
#     - line_s / line_e: Start / End line numbers in source file.
#     - char_s / char_e: Start / End character column offsets.
# -----------------------------------------------------------------------------
m_bridge_tag = Table(
    table_id=12,
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
# 14. m_map_ast (table_id=13): Spatial Source Region to AST Mapping
#     - map_id: Map Set Grouping ID.
#     - line_s / char_s / line_e / char_e: Coordinate region relative to tag snippet.
#     - ast_id: Target AST Node ID (FK -> m_ast.ast_id).
# -----------------------------------------------------------------------------
m_map_ast = Table(
    table_id=13,
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
# 15. m_bridge_map (table_id=14): Code Tag to AST Coordinate Map Bridge
#     - tag_id: Code Tag ID (FK -> m_tag.tag_id).
#     - map_id: Map Set ID (matching m_map_ast.map_id).
# -----------------------------------------------------------------------------
m_bridge_map = Table(
    table_id=14,
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
# 16. m_ast_hash (table_id=15): AST Structural Hash Deduplication Registry
#     - hash: 32-byte binary SHA-256 digest of canonical AST node & children (PK).
#     - ast_id: Assigned AST Node ID (FK -> m_ast.ast_id).
# -----------------------------------------------------------------------------
m_ast_hash = Table(
    table_id=15,
    table_name="m_ast_hash",
    columns=(
        ("hash", "BINARY(32)", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("hash",),
    foreign=(("ast_id", "m_ast", "ast_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 17. m_kconfig_symbol (table_id=16): Kconfig Configuration Symbol Registry
#     - kcid: Unique Kconfig Symbol ID (PK with vid_s).
#     - vid_s: Starting Version ID (FK -> m_v_main.vid).
#     - vid_e: Ending Version ID (FK -> m_v_main.vid, 0 if still active).
#     - name: Symbol name without CONFIG_ prefix (e.g. "64BIT", "EXT4_FS").
#     - type: Data type (1: bool, 2: tristate, 3: string, 4: hex, 5: int).
#     - prompt: Primary user-visible prompt label.
#     - def_val: Default value expression / literal string.
#     - help: Multi-line help / documentation text.
#     - ast_id: Associated AST Node ID (FK -> m_ast.ast_id).
# -----------------------------------------------------------------------------
m_kconfig_symbol = Table(
    table_id=16,
    table_name="m_kconfig_symbol",
    columns=(
        ("kcid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("vid_s", "INT", "NOT NULL"),
        ("vid_e", "INT", "NOT NULL"),
        ("name", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("prompt", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("def_val", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("help", "MEDIUMTEXT", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("kcid", "vid_s"),
    foreign=(
        ("vid_s", "m_v_main", "vid"),
        ("vid_e", "m_v_main", "vid"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=True,
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 18. m_kconfig_relation (table_id=17): Dependency & Reverse-Dependency Graph
#     - kcid: Source Kconfig Symbol ID (FK -> m_kconfig_symbol.kcid).
#     - target_name: Depended-upon or selected symbol name (e.g. "BLOCK", "CRC32").
#     - rel_type: Category (1: depends_on, 2: select, 3: imply, 4: choice_member).
#     - cond_ast_id: Conditional guard AST Node ID (0 if unconditional).
#     - priority: Positional rank in multi-clause expression lists.
# -----------------------------------------------------------------------------
m_kconfig_relation = Table(
    table_id=17,
    table_name="m_kconfig_relation",
    columns=(
        ("kcid", "INT", "NOT NULL"),
        ("target_name", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("rel_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("cond_ast_id", "INT", "NOT NULL"),
        ("priority", "SMALLINT", "UNSIGNED", "NOT NULL"),
    ),
    primary=("kcid", "rel_type", "target_name", "priority"),
    foreign=(("kcid", "m_kconfig_symbol", "kcid"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 19. m_kconfig_tree (table_id=18): Menuconfig Hierarchy & Display Structure
#     - tree_id: Unique Tree Item ID (PK with vid).
#     - vid: Kernel Version ID (FK -> m_v_main.vid).
#     - parent_id: Parent menu / choice tree ID (0 for root).
#     - node_type: Category (1: menu, 2: choice, 3: config, 4: menuconfig, 5: comment).
#     - title: Display prompt / menu header title.
#     - kcid: Linked Symbol ID (FK -> m_kconfig_symbol.kcid, 0 if menu/comment).
#     - priority: Sibling display ordering rank.
#     - dep_ast_id: Visibility / conditional dependency AST ID (0 if unconditional).
#     - ast_id: Associated AST Node ID (FK -> m_ast.ast_id).
# -----------------------------------------------------------------------------
m_kconfig_tree = Table(
    table_id=18,
    table_name="m_kconfig_tree",
    columns=(
        ("tree_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("vid", "INT", "NOT NULL"),
        ("parent_id", "INT", "NOT NULL"),
        ("node_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("title", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("kcid", "INT", "NOT NULL"),
        ("priority", "INT", "NOT NULL"),
        ("dep_ast_id", "INT", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("tree_id", "vid"),
    foreign=(
        ("vid", "m_v_main", "vid"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 20. m_kconfig_kbuild (table_id=19): Kconfig to Source File Compilation Map
#     - kcid: Target Kconfig Symbol ID (FK -> m_kconfig_symbol.kcid, 0 for core obj-y).
#     - vid: Kernel Version ID (FK -> m_v_main.vid).
#     - fid: Source File ID (FK -> m_file.fid).
#     - compile_mode: Compilation state (1: built-in obj-y, 2: module obj-m, 3: conditional obj-$(CONFIG_...)).
#     - target_obj: Target object or composite module name (e.g. "ext4.o", "drbd.o").
# -----------------------------------------------------------------------------
m_kconfig_kbuild = Table(
    table_id=19,
    table_name="m_kconfig_kbuild",
    columns=(
        ("kcid", "INT", "NOT NULL"),
        ("vid", "INT", "NOT NULL"),
        ("fid", "INT", "NOT NULL"),
        ("compile_mode", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("target_obj", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
    ),
    primary=("kcid", "vid", "fid", "compile_mode"),
    foreign=(
        ("vid", "m_v_main", "vid"),
        ("fid", "m_file", "fid"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 21. m_maintainer_person (table_id=20): Maintainer & Reviewer Persona Registry
#     - person_id: Unique Person ID (PK, AUTO_INCREMENT).
#     - name: Full name string (e.g. "Linus Torvalds").
#     - email: Primary email address (e.g. "torvalds@linux-foundation.org").
# -----------------------------------------------------------------------------
m_maintainer_person = Table(
    table_id=20,
    table_name="m_maintainer_person",
    columns=(
        ("person_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("email", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
    ),
    primary=("person_id",),
    foreign=None,
    initial_insert=None,
    no_duplicate=True,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 22. m_maintainer_section (table_id=21): Subsystem Section Registry
#     - sec_id: Unique Section ID (PK with vid_s, AUTO_INCREMENT).
#     - vid_s: Starting Version ID (FK -> m_v_main.vid).
#     - vid_e: Ending Version ID (FK -> m_v_main.vid, 0 if active).
#     - name: Subsystem title/name (e.g. "EXT4 FILE SYSTEM").
#     - status: Status string (e.g. "Maintained", "Supported", "Orphan").
#     - scm_tree: SCM tree URL string (or "").
#     - web_page: Web page URL string (or "").
#     - mailing_list: Primary mailing list address (or "").
#     - ast_id: Associated AST Node ID (FK -> m_ast.ast_id).
# -----------------------------------------------------------------------------
m_maintainer_section = Table(
    table_id=21,
    table_name="m_maintainer_section",
    columns=(
        ("sec_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("vid_s", "INT", "NOT NULL"),
        ("vid_e", "INT", "NOT NULL"),
        ("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("status", "VARCHAR(32)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("scm_tree", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("web_page", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("mailing_list", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("sec_id", "vid_s"),
    foreign=(
        ("vid_s", "m_v_main", "vid"),
        ("vid_e", "m_v_main", "vid"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=True,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 23. m_maintainer_member (table_id=22): Section to Person Role Mapping
#     - sec_id: Subsystem Section ID (FK -> m_maintainer_section.sec_id).
#     - person_id: Person ID (FK -> m_maintainer_person.person_id).
#     - role_type: Role (1: Maintainer 'M', 2: Reviewer 'R', 3: Person 'P', 4: Other).
#     - priority: Display order rank index.
# -----------------------------------------------------------------------------
m_maintainer_member = Table(
    table_id=22,
    table_name="m_maintainer_member",
    columns=(
        ("sec_id", "INT", "NOT NULL"),
        ("person_id", "INT", "NOT NULL"),
        ("role_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("priority", "SMALLINT", "UNSIGNED", "NOT NULL"),
    ),
    primary=("sec_id", "person_id", "role_type"),
    foreign=(
        ("sec_id", "m_maintainer_section", "sec_id"),
        ("person_id", "m_maintainer_person", "person_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 24. m_maintainer_pattern (table_id=23): Section Pattern & Rule Registry
#     - sec_id: Subsystem Section ID (FK -> m_maintainer_section.sec_id).
#     - pat_type: Pattern Type (1: File 'F', 2: Exclude 'X', 3: Keyword 'K', 4: Regex 'N').
#     - pattern: Raw pattern string (e.g. "fs/ext4/", "drivers/net/3c505*").
#     - priority: Rule evaluation order rank.
# -----------------------------------------------------------------------------
m_maintainer_pattern = Table(
    table_id=23,
    table_name="m_maintainer_pattern",
    columns=(
        ("sec_id", "INT", "NOT NULL"),
        ("pat_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("pattern", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("priority", "SMALLINT", "UNSIGNED", "NOT NULL"),
    ),
    primary=("sec_id", "pat_type", "pattern", "priority"),
    foreign=(("sec_id", "m_maintainer_section", "sec_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 25. m_maintainer_file (table_id=24): Materialized File-to-Section Bridge
#     - vid: Kernel Version ID (FK -> m_v_main.vid).
#     - fid: File Instance ID (FK -> m_file.fid).
#     - sec_id: Subsystem Section ID (FK -> m_maintainer_section.sec_id).
# -----------------------------------------------------------------------------
m_maintainer_file = Table(
    table_id=24,
    table_name="m_maintainer_file",
    columns=(
        ("vid", "INT", "NOT NULL"),
        ("fid", "INT", "NOT NULL"),
        ("sec_id", "INT", "NOT NULL"),
    ),
    primary=("vid", "fid", "sec_id"),
    foreign=(
        ("vid", "m_v_main", "vid"),
        ("fid", "m_file", "fid"),
        ("sec_id", "m_maintainer_section", "sec_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 26. m_credits_entry (table_id=25): Contributor & Author Credits Registry
#     - credit_id: Unique Credit ID (PK with vid_s, AUTO_INCREMENT).
#     - vid_s: Starting Version ID (FK -> m_v_main.vid).
#     - vid_e: Ending Version ID (FK -> m_v_main.vid, 0 if active).
#     - person_id: Person ID (FK -> m_maintainer_person.person_id).
#     - web_page: Contributor homepage / web address.
#     - pgp_key: PGP key fingerprint / ID.
#     - description: Detailed summary of contributions.
#     - snail_mail: Postal / snail-mail address.
#     - ast_id: Associated AST Node ID (FK -> m_ast.ast_id).
# -----------------------------------------------------------------------------
m_credits_entry = Table(
    table_id=25,
    table_name="m_credits_entry",
    columns=(
        ("credit_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("vid_s", "INT", "NOT NULL"),
        ("vid_e", "INT", "NOT NULL"),
        ("person_id", "INT", "NOT NULL"),
        ("web_page", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("pgp_key", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("description", "MEDIUMTEXT", "NOT NULL"),
        ("snail_mail", "MEDIUMTEXT", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("credit_id", "vid_s"),
    foreign=(
        ("vid_s", "m_v_main", "vid"),
        ("vid_e", "m_v_main", "vid"),
        ("person_id", "m_maintainer_person", "person_id"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=True,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 27. m_commit (table_id=26): Git Commit Registry
#     - commit_id: Unique Commit ID (PK, AUTO_INCREMENT).
#     - vid: Target Kernel Version ID (FK -> m_v_main.vid).
#     - commit_hash: 40-char SHA-1 Git commit hash.
#     - author_id: Commit Author Person ID (FK -> m_maintainer_person.person_id).
#     - author_date: Author date as Unix epoch timestamp.
#     - committer_id: Committer Person ID (FK -> m_maintainer_person.person_id).
#     - committer_date: Committer date as Unix epoch timestamp.
#     - subject: Single-line commit subject / summary.
#     - message: Full commit message body (including trailers).
# -----------------------------------------------------------------------------
m_commit = Table(
    table_id=26,
    table_name="m_commit",
    columns=(
        ("commit_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("vid", "INT", "NOT NULL"),
        ("commit_hash", "VARCHAR(40)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("author_id", "INT", "NOT NULL"),
        ("author_date", "INT", "NOT NULL"),
        ("committer_id", "INT", "NOT NULL"),
        ("committer_date", "INT", "NOT NULL"),
        ("subject", "VARCHAR(500)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("message", "MEDIUMTEXT", "NOT NULL"),
    ),
    primary=("commit_id",),
    foreign=(
        ("vid", "m_v_main", "vid"),
        ("author_id", "m_maintainer_person", "person_id"),
        ("committer_id", "m_maintainer_person", "person_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 28. m_bridge_commit_person (table_id=27): Commit Multi-Contributor Bridge
#     - commit_id: Git Commit ID (FK -> m_commit.commit_id).
#     - person_id: Contributor Person ID (FK -> m_maintainer_person.person_id).
#     - role_type: Role (1: Author, 2: Committer, 3: Co-developed-by, 4: Signed-off-by, 5: Reviewed-by, 6: Acked-by, 7: Tested-by, 8: Reported-by, 9: Suggested-by).
#     - priority: Occurrence order / rank.
# -----------------------------------------------------------------------------
m_bridge_commit_person = Table(
    table_id=27,
    table_name="m_bridge_commit_person",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        ("person_id", "INT", "NOT NULL"),
        ("role_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("priority", "SMALLINT", "UNSIGNED", "NOT NULL"),
    ),
    primary=("commit_id", "person_id", "role_type"),
    foreign=(
        ("commit_id", "m_commit", "commit_id"),
        ("person_id", "m_maintainer_person", "person_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 29. m_bridge_commit_file (table_id=28): Commit Modified File Bridge
#     - commit_id: Git Commit ID (FK -> m_commit.commit_id).
#     - vid: Target Kernel Version ID (FK -> m_v_main.vid).
#     - fid: Modified File Instance ID (FK -> m_file.fid).
#     - change_type: Modification flag ('A'=Added, 'M'=Modified, 'D'=Deleted, 'R'=Renamed).
# -----------------------------------------------------------------------------
m_bridge_commit_file = Table(
    table_id=28,
    table_name="m_bridge_commit_file",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        ("vid", "INT", "NOT NULL"),
        ("fid", "INT", "NOT NULL"),
        ("change_type", "CHAR(1)", "NOT NULL"),
    ),
    primary=("commit_id", "fid"),
    foreign=(
        ("commit_id", "m_commit", "commit_id"),
        ("vid", "m_v_main", "vid"),
        ("fid", "m_file", "fid"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 30. m_bridge_commit_tag (table_id=29): Multi-Commit Tag Mapping Bridge
#     - commit_id: Git Commit ID (FK -> m_commit.commit_id).
#     - vid: Target Kernel Version ID (FK -> m_v_main.vid).
#     - fid: File Instance ID (FK -> m_file.fid).
#     - tag_id: Code Snippet Tag ID (FK -> m_tag.tag_id).
# -----------------------------------------------------------------------------
m_bridge_commit_tag = Table(
    table_id=29,
    table_name="m_bridge_commit_tag",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        ("vid", "INT", "NOT NULL"),
        ("fid", "INT", "NOT NULL"),
        ("tag_id", "INT", "NOT NULL"),
    ),
    primary=("commit_id", "tag_id"),
    foreign=(
        ("commit_id", "m_commit", "commit_id"),
        ("vid", "m_v_main", "vid"),
        ("fid", "m_file", "fid"),
        ("tag_id", "m_tag", "tag_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
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
    m_tag_code,
    m_tag,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
    m_ast_hash,
    m_kconfig_symbol,
    m_kconfig_relation,
    m_kconfig_tree,
    m_kconfig_kbuild,
    m_maintainer_person,
    m_maintainer_section,
    m_maintainer_member,
    m_maintainer_pattern,
    m_maintainer_file,
    m_credits_entry,
    m_commit,
    m_bridge_commit_person,
    m_bridge_commit_file,
    m_bridge_commit_tag,
)


def init_db_layout(gp=None) -> tuple[Table, ...]:
    """Initialize and populate gp.Table_Array with the default 30 schema tables.
    
    Args:
        gp: Optional GreatProcessor instance to attach Table_Array to.
        
        
    Returns:
        Immutable tuple of all 30 Table schema objects.
    """
    if gp is not None:
        gp.Table_Array = list(TABLES)
    return TABLES





