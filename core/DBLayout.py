"""DBLayout.py - Relational Database Schema & Content-Addressable Table Definitions.

===============================================================================
RELATIONAL DATABASE SCHEMA REFERENCE GUIDE FOR GIT DAG & CONTENT-ADDRESSABLE ARCH
===============================================================================
This module defines the 48 core relational database tables used across the parser
pipeline (`Table_Array` / `TABLES`).

SCHEMA ENTITY-RELATIONSHIP GRAPH:
-------------------------------------------------------------------------------
1. Project, Git Network, Repository & Ref Hierarchy:
   m_project (project_id, name, slug)
     └── m_git_network (network_id, project_id, name, root_commit_hash)
           └── m_repository (repo_id, network_id, name, git_url, default_branch)
                 └── m_git_ref (ref_id, repo_id, ref_type, ref_name, commit_id, tagger_id, tagger_date, tag_message)

2. Git Commit DAG, Delta Snapshot & Checkpoint Layer:
   m_commit (commit_id, network_id, commit_hash, author_id, author_date, committer_id, committer_date, subject, message)
     ├── m_commit_parent (commit_id, parent_commit_id, parent_idx)
     ├── m_commit_file (commit_id, fnid, blob_id, old_fnid, old_blob_id, change_type)
     ├── m_bridge_checkpoint (commit_id, fnid, blob_id)
     ├── m_ingest_state (network_id, commit_id, status, updated_at)
     ├── m_ingest_watermark (commit_id, marked_table_id, max_id)
     ├── m_ingest_blob (commit_id, blob_id)
     ├── m_arch_root (commit_id, arch, role, priority, path, target)   -- build layout per ARCH
     └── m_commit_email (commit_id, message_id, thread_id, relation_type, match_tier, patch_version, patch_idx, patch_total)

3. Universal Content-Addressable Stores:
   m_file_name (fnid, fname)
   m_file_blob (blob_id, blob_hash, ftype)
   m_tag_code (hash, code)
   m_ast (ast_id, name, type_id)
   m_ast_hash (hash, ast_id)
   m_type_descriptor (type_id, name)
   m_maintainer_person (person_id, name, email)

4. Blob-Scoped Code Tags & Source Coordinates:
   m_tag (tag_id, hash, ast_id, hl_s, hl_l)
     ├── m_bridge_tag (blob_id, tag_id, line_s, line_e, char_s, char_e)
     ├── m_moved_tag (s_tag_id, e_tag_id)
     ├── m_bridge_commit_tag (commit_id, blob_id, tag_id)
     └── m_bridge_map (tag_id, map_id) -> m_map_ast (map_id, line_s, char_s, line_e, char_e, ast_id)

5. Symbol Definitions & References (written once per tag, see core/SymbolRows.py):
   m_tag_def (tag_id, def_idx, sym_key, name, type_id, linkage, rel_line_s, rel_line_e)
   m_tag_ref (tag_id, ref_idx, sym_key, role, rel_line, char_s)
   m_blob_symbol (blob_id, sym_key, target_ast_id, target_fnid)   -- per-blob resolution of sym_key
   m_symbol_own (blob_id, tag_id, kind)                           -- placements stored per blob instead
   m_blob_symbol_def (def_id, blob_id, tag_id, ast_id, name, type_id, linkage, line_s, line_e)
   m_blob_symbol_ref (ref_id, blob_id, tag_id, ast_id, target_fnid, role, line, char_s)
   Views m_symbol_def / m_symbol_ref (main.ensure_db_views) return both layouts in the per-blob shape.
   m_file_reference (ref_id, source_blob_id, target_fnid, ref_type, line_no, details)

6. Kconfig Relational Hierarchy & Build Mapping:
   m_kconfig_symbol (kcid, name, type, prompt, def_val, help, ast_id)
     ├── m_kconfig_relation (rel_id, kcid, target_name, rel_type, cond_ast_id, priority)
     ├── m_kconfig_tree (tree_id, blob_id, parent_id, node_type, title, kcid, priority, dep_ast_id, ast_id)
     └── m_kconfig_kbuild (kcid, makefile_blob_id, target_fnid, compile_mode, target_obj)

7. Maintainers & Credits Subsystem:
   m_maintainer_section (sec_id, blob_id, name, status, scm_tree, web_page, mailing_list, ast_id)
     ├── m_maintainer_member (sec_id, person_id, role_type, priority)
     ├── m_maintainer_pattern (sec_id, pat_type, pattern, priority)
     ├── m_maintainer_file (maintainers_blob_id, fnid, sec_id)
   m_credits_entry (credit_id, blob_id, person_id, web_page, pgp_key, description, snail_mail, ast_id)
===============================================================================
"""
from __future__ import annotations

import secrets
import time
from core.globalstuff import ASTT
from core.TableHandling import Table

# Pre-populate AST type descriptor entries from ASTT enum
m_type_descriptor_insert = tuple((ast.value, ast.name) for ast in ASTT)

# -----------------------------------------------------------------------------
# 0. m_project (table_id=0): Multi-Tenant Project / Ecosystem Scope
# -----------------------------------------------------------------------------
m_project = Table(
    table_id=0,
    table_name="m_project",
    columns=(
        ("project_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("name", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("slug", "VARCHAR(32)", "NOT NULL", "COLLATE utf8mb4_bin"),
    ),
    primary=("project_id",),
    foreign=None,
    initial_insert=((0, "default", "default"),),
    no_duplicate=True,
    te_cached=True,
    unique_keys=(("slug",),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 1. m_file_name (table_id=1): Unique Repository Path String Registry
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
    indexes=(("file_name_index", ("fname",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 2. m_file_blob (table_id=2): Unique File Content Universal Store
# -----------------------------------------------------------------------------
m_file_blob = Table(
    table_id=2,
    table_name="m_file_blob",
    columns=(
        ("blob_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("blob_hash", "BINARY(20)", "NOT NULL"),
        ("ftype", "TINYINT", "UNSIGNED", "NOT NULL"),
    ),
    primary=("blob_id",),
    foreign=None,
    initial_insert=((0, b"\x00" * 20, 0),),
    no_duplicate=True,
    te_cached=True,
    unique_keys=(("blob_hash", "ftype"),),
    hashing_table=False,
)
m_file = m_file_blob
setattr(m_file_blob, "fid", m_file_blob.blob_id)
setattr(m_file_blob, "vid_s", (m_file_blob.table_id, 1))
setattr(m_file_blob, "vid_e", (m_file_blob.table_id, 2))

# -----------------------------------------------------------------------------
# 3. m_bridge_checkpoint (table_id=3): Milestone Full-Tree Snapshots
# -----------------------------------------------------------------------------
m_bridge_checkpoint = Table(
    table_id=3,
    table_name="m_bridge_checkpoint",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        ("fnid", "INT", "NOT NULL"),
        ("blob_id", "INT", "NOT NULL"),
    ),
    primary=("commit_id", "fnid"),
    foreign=(
        ("fnid", "m_file_name", "fnid"),
        ("blob_id", "m_file_blob", "blob_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    blob_scoped=True,
    version_scoped=True,
    indexes=(("chk_blob_idx", ("blob_id",)),),
    hashing_table=False,
)
m_bridge_file = m_bridge_checkpoint
setattr(m_bridge_checkpoint, "vid", m_bridge_checkpoint.commit_id)
setattr(m_bridge_checkpoint, "fid", m_bridge_checkpoint.blob_id)

# -----------------------------------------------------------------------------
# 4. m_blob_transition (table_id=4): Content Evolution & Cherry-Pick Registry
# -----------------------------------------------------------------------------
m_blob_transition = Table(
    table_id=4,
    table_name="m_blob_transition",
    columns=(
        ("old_blob_id", "INT", "NOT NULL"),
        ("new_blob_id", "INT", "NOT NULL"),
    ),
    primary=("old_blob_id", "new_blob_id"),
    foreign=(
        ("old_blob_id", "m_file_blob", "blob_id"),
        ("new_blob_id", "m_file_blob", "blob_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    idempotent_pk=True,
    hashing_table=False,
)
m_moved_file = m_blob_transition
setattr(m_blob_transition, "s_fid", m_blob_transition.old_blob_id)
setattr(m_blob_transition, "e_fid", m_blob_transition.new_blob_id)

# -----------------------------------------------------------------------------
# 5. m_type_descriptor (table_id=5): AST Type Discriminator Catalog
# -----------------------------------------------------------------------------
m_type_descriptor = Table(
    table_id=5,
    table_name="m_type_descriptor",
    columns=(
        ("type_id", "SMALLINT", "UNSIGNED", "NOT NULL"),
        ("name", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
    ),
    primary=("type_id",),
    foreign=None,
    initial_insert=m_type_descriptor_insert,
    no_duplicate=False,
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 6. m_ast (table_id=6): Relational AST Structure Catalog
# -----------------------------------------------------------------------------
m_ast = Table(
    table_id=6,
    table_name="m_ast",
    columns=(
        ("ast_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("type_id", "SMALLINT", "UNSIGNED", "NOT NULL"),
    ),
    primary=("ast_id",),
    foreign=(("type_id", "m_type_descriptor", "type_id"),),
    initial_insert=((0, "", 0),),
    no_duplicate=False,
    te_cached=False,
    hashing_table="m_ast_hash",
    indexes=(("ast_index", ("name", "type_id")),),
)

# -----------------------------------------------------------------------------
# 7. m_ast_container (table_id=7): AST Child Nesting & Parameter Relations
# -----------------------------------------------------------------------------
m_ast_container = Table(
    table_id=7,
    table_name="m_ast_container",
    columns=(
        ("ast_id", "INT", "NOT NULL"),
        ("priority", "SMALLINT", "UNSIGNED", "NOT NULL"),
        ("type_id", "SMALLINT", "UNSIGNED", "NOT NULL"),
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
    indexes=(("ast_cont_ref_idx", ("ref_ast_id",)),),
)

# -----------------------------------------------------------------------------
# 8. m_ast_include (table_id=8): AST File Dependency Registry
# -----------------------------------------------------------------------------
m_ast_include = Table(
    table_id=8,
    table_name="m_ast_include",
    columns=(
        ("ast_id", "INT", "NOT NULL"),
        ("fnid", "INT", "NOT NULL"),
    ),
    primary=("ast_id",),
    foreign=(
        ("ast_id", "m_ast", "ast_id"),
        ("fnid", "m_file_name", "fnid"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
    indexes=(("ast_inc_fnid_idx", ("fnid",)),),
)

# -----------------------------------------------------------------------------
# 9. m_ast_debug (table_id=9): Raw Textual AST Diagnostics
# -----------------------------------------------------------------------------
m_ast_debug = Table(
    table_id=9,
    table_name="m_ast_debug",
    columns=(
        ("ast_id", "INT", "NOT NULL"),
        ("ast_raw", "MEDIUMTEXT", "NOT NULL"),
    ),
    primary=("ast_id",),
    foreign=(("ast_id", "m_ast", "ast_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 10. m_tag_code (table_id=10): Content-Addressable Source Code Snippets
# -----------------------------------------------------------------------------
m_tag_code = Table(
    table_id=10,
    table_name="m_tag_code",
    columns=(
        ("hash", "BINARY(32)", "NOT NULL"),
        ("code", "MEDIUMTEXT", "NOT NULL"),
    ),
    primary=("hash",),
    foreign=None,
    initial_insert=((b"\x00" * 32, ""),),
    no_duplicate=False,
    te_cached=("hash",),
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 11. m_tag (table_id=11): Immutable & Recyclable AST Code Tag Entity
# -----------------------------------------------------------------------------
m_tag = Table(
    table_id=11,
    table_name="m_tag",
    columns=(
        ("tag_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("hash", "BINARY(32)", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
        ("hl_s", "INT", "NOT NULL"),
        ("hl_l", "INT", "NOT NULL"),
    ),
    primary=("tag_id",),
    foreign=(
        ("hash", "m_tag_code", "hash"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=((0, b"\x00" * 32, 0, 0, 0),),
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
    indexes=(("tag_hash_idx", ("hash",)), ("tag_ast_idx", ("ast_id",))),
)

# -----------------------------------------------------------------------------
# 12. m_bridge_tag (table_id=12): Tag Occurrence Coordinates within Content Blob
# -----------------------------------------------------------------------------
m_bridge_tag = Table(
    table_id=12,
    table_name="m_bridge_tag",
    columns=(
        ("blob_id", "INT", "NOT NULL"),
        ("tag_id", "INT", "NOT NULL"),
        ("line_s", "INT", "NOT NULL"),
        ("line_e", "INT", "NOT NULL"),
        ("char_s", "INT", "NOT NULL"),
        ("char_e", "INT", "NOT NULL"),
    ),
    primary=("blob_id", "tag_id"),
    foreign=(
        ("blob_id", "m_file_blob", "blob_id"),
        ("tag_id", "m_tag", "tag_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    indexes=(("bridge_tag_tid_idx", ("tag_id",)),),
    hashing_table=False,
)
setattr(m_bridge_tag, "fid", m_bridge_tag.blob_id)

# -----------------------------------------------------------------------------
# 13. m_map_ast (table_id=13): Tag-Relative AST Overlay Spans
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
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 14. m_bridge_map (table_id=14): Tag to AST Map Linkage
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
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 15. m_ast_hash (table_id=15): Cryptographic AST Content Hash Index
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
    initial_insert=((b"\x00" * 32, 0),),
    no_duplicate=False,
    te_cached=True,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 16. m_kconfig_symbol (table_id=16): Content-Deduplicated Kconfig Symbols
# -----------------------------------------------------------------------------
m_kconfig_symbol = Table(
    table_id=16,
    table_name="m_kconfig_symbol",
    columns=(
        ("kcid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("name", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("prompt", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("def_val", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("help", "MEDIUMTEXT", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("kcid",),
    foreign=(("ast_id", "m_ast", "ast_id"),),
    initial_insert=((0, "", 0, "", "", "", 0),),
    no_duplicate=True,
    te_cached=True,
    indexes=(("kc_sym_name_idx", ("name",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 17. m_kconfig_relation (table_id=17): Kconfig Dependency & Select Rules
# -----------------------------------------------------------------------------
m_kconfig_relation = Table(
    table_id=17,
    table_name="m_kconfig_relation",
    columns=(
        ("rel_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("kcid", "INT", "NOT NULL"),
        ("target_name", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("rel_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("cond_ast_id", "INT", "NOT NULL"),
        ("priority", "INT", "NOT NULL"),
    ),
    primary=("rel_id",),
    foreign=(
        ("kcid", "m_kconfig_symbol", "kcid"),
        ("cond_ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=True,
    te_cached=True,
    indexes=(("kc_rel_kcid_idx", ("kcid",)), ("kc_rel_tgt_idx", ("target_name",))),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 18. m_kconfig_tree (table_id=18): Blob-Scoped Menu Hierarchy
# -----------------------------------------------------------------------------
m_kconfig_tree = Table(
    table_id=18,
    table_name="m_kconfig_tree",
    columns=(
        ("tree_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("blob_id", "INT", "NOT NULL"),
        ("parent_id", "INT", "NOT NULL"),
        ("node_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("title", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("kcid", "INT", "NOT NULL"),
        ("priority", "INT", "NOT NULL"),
        ("dep_ast_id", "INT", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("tree_id",),
    foreign=(
        ("blob_id", "m_file_blob", "blob_id"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    indexes=(("kc_tree_blob_idx", ("blob_id", "parent_id", "priority")),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 19. m_kconfig_kbuild (table_id=19): Makefile Build Object Mappings
# -----------------------------------------------------------------------------
m_kconfig_kbuild = Table(
    table_id=19,
    table_name="m_kconfig_kbuild",
    columns=(
        ("kcid", "INT", "NOT NULL"),
        ("makefile_blob_id", "INT", "NOT NULL"),
        ("target_fnid", "INT", "NOT NULL"),
        ("compile_mode", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("target_obj", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
    ),
    primary=("kcid", "makefile_blob_id", "target_fnid", "compile_mode"),
    foreign=(
        ("kcid", "m_kconfig_symbol", "kcid"),
        ("makefile_blob_id", "m_file_blob", "blob_id"),
        ("target_fnid", "m_file_name", "fnid"),
    ),
    initial_insert=None,
    idempotent_pk=True,
    te_cached=True,
    indexes=(("kc_kb_fnid_idx", ("target_fnid",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 20. m_maintainer_person (table_id=20): Contributor Identity Registry
# -----------------------------------------------------------------------------
m_maintainer_person = Table(
    table_id=20,
    table_name="m_maintainer_person",
    columns=(
        ("person_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("name", "VARCHAR(128)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("email", "VARCHAR(128)", "NOT NULL", "COLLATE utf8mb4_bin"),
    ),
    primary=("person_id",),
    foreign=None,
    initial_insert=((0, "", ""),),
    no_duplicate=True,
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 21. m_maintainer_section (table_id=21): Blob-Scoped Subsystem Definitions
# -----------------------------------------------------------------------------
m_maintainer_section = Table(
    table_id=21,
    table_name="m_maintainer_section",
    columns=(
        ("sec_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("blob_id", "INT", "NOT NULL"),
        ("name", "VARCHAR(128)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("status", "VARCHAR(32)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("scm_tree", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("web_page", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("mailing_list", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("sec_id",),
    foreign=(
        ("blob_id", "m_file_blob", "blob_id"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=((0, 0, "", "", "", "", "", 0),),
    no_duplicate=True,
    te_cached=True,
    indexes=(("maint_sec_blob_idx", ("blob_id",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 22. m_maintainer_member (table_id=22): Section Maintainer Members
# -----------------------------------------------------------------------------
m_maintainer_member = Table(
    table_id=22,
    table_name="m_maintainer_member",
    columns=(
        ("sec_id", "INT", "NOT NULL"),
        ("person_id", "INT", "NOT NULL"),
        ("role_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("priority", "INT", "NOT NULL"),
    ),
    primary=("sec_id", "person_id", "role_type"),
    foreign=(
        ("sec_id", "m_maintainer_section", "sec_id"),
        ("person_id", "m_maintainer_person", "person_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 23. m_maintainer_pattern (table_id=23): Section File Matching Patterns
# -----------------------------------------------------------------------------
m_maintainer_pattern = Table(
    table_id=23,
    table_name="m_maintainer_pattern",
    columns=(
        ("sec_id", "INT", "NOT NULL"),
        ("pat_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("pattern", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("priority", "INT", "NOT NULL"),
    ),
    primary=("sec_id", "pat_type", "pattern", "priority"),
    foreign=(("sec_id", "m_maintainer_section", "sec_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 24. m_maintainer_file (table_id=24): Path to Maintainer Section Resolution
# -----------------------------------------------------------------------------
m_maintainer_file = Table(
    table_id=24,
    table_name="m_maintainer_file",
    columns=(
        ("maintainers_blob_id", "INT", "NOT NULL"),
        ("fnid", "INT", "NOT NULL"),
        ("sec_id", "INT", "NOT NULL"),
    ),
    primary=("maintainers_blob_id", "fnid", "sec_id"),
    foreign=(
        ("maintainers_blob_id", "m_file_blob", "blob_id"),
        ("fnid", "m_file_name", "fnid"),
        ("sec_id", "m_maintainer_section", "sec_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    indexes=(("maint_file_fnid_idx", ("maintainers_blob_id", "fnid")),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 25. m_credits_entry (table_id=25): Linux CREDITS Contributor Biographies
# -----------------------------------------------------------------------------
m_credits_entry = Table(
    table_id=25,
    table_name="m_credits_entry",
    columns=(
        ("credit_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("blob_id", "INT", "NOT NULL"),
        ("person_id", "INT", "NOT NULL"),
        ("web_page", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("pgp_key", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("description", "MEDIUMTEXT", "NOT NULL"),
        ("snail_mail", "MEDIUMTEXT", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("credit_id",),
    foreign=(
        ("blob_id", "m_file_blob", "blob_id"),
        ("person_id", "m_maintainer_person", "person_id"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=True,
    te_cached=True,
    indexes=(("credits_blob_idx", ("blob_id",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 26. m_commit (table_id=26): Git Commit Metadata & Topology Node
# -----------------------------------------------------------------------------
m_commit = Table(
    table_id=26,
    table_name="m_commit",
    columns=(
        ("commit_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("network_id", "INT", "NOT NULL"),
        ("commit_hash", "BINARY(20)", "NOT NULL"),
        ("author_id", "INT", "NOT NULL"),
        ("author_date", "INT", "NOT NULL"),
        ("committer_id", "INT", "NOT NULL"),
        ("committer_date", "INT", "NOT NULL"),
        ("subject", "VARCHAR(500)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("message", "MEDIUMTEXT", "NOT NULL"),
    ),
    primary=("commit_id",),
    foreign=(
        ("author_id", "m_maintainer_person", "person_id"),
        ("committer_id", "m_maintainer_person", "person_id"),
    ),
    initial_insert=((0, 0, b"\x00" * 20, 0, 0, 0, 0, "", ""),),
    no_duplicate=False,
    te_cached=False,
    unique_keys=(("network_id", "commit_hash"),),
    indexes=(("commit_author_idx", ("author_id",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 27. m_bridge_commit_person (table_id=27): Git Commit Contributor Attribution
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
    # Write-only for the pipeline and idempotent: duplicate rows are skipped by the database, so the
    # table is not preloaded every version (it grows with every ingested commit)
    te_cached=False,
    idempotent_pk=True,
    indexes=(("bcp_person_idx", ("person_id",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 28. m_commit_file (table_id=28): First-Parent Commit Tree Delta
# -----------------------------------------------------------------------------
m_commit_file = Table(
    table_id=28,
    table_name="m_commit_file",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        ("fnid", "INT", "NOT NULL"),
        ("blob_id", "INT", "NOT NULL"),
        ("old_fnid", "INT", "NOT NULL"),
        ("old_blob_id", "INT", "NOT NULL"),
        ("change_type", "CHAR(1)", "NOT NULL"),
    ),
    primary=("commit_id", "fnid"),
    foreign=(
        ("commit_id", "m_commit", "commit_id"),
        ("fnid", "m_file_name", "fnid"),
        ("blob_id", "m_file_blob", "blob_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    indexes=(("cf_fnid_cid_idx", ("fnid", "commit_id")), ("cf_blob_idx", ("blob_id",))),
    hashing_table=False,
)
m_bridge_commit_file = m_commit_file

# -----------------------------------------------------------------------------
# 29. m_bridge_commit_tag (table_id=29): Tag Lifecycle Introduced in Commit
# -----------------------------------------------------------------------------
m_bridge_commit_tag = Table(
    table_id=29,
    table_name="m_bridge_commit_tag",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        ("blob_id", "INT", "NOT NULL"),
        ("tag_id", "INT", "NOT NULL"),
    ),
    primary=("commit_id", "tag_id"),
    foreign=(
        ("commit_id", "m_commit", "commit_id"),
        ("blob_id", "m_file_blob", "blob_id"),
        ("tag_id", "m_tag", "tag_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    indexes=(("bct_tag_idx", ("tag_id",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 30. m_moved_tag (table_id=30): Content Tag Evolution (M:N Cross-Branch DAG)
# -----------------------------------------------------------------------------
m_moved_tag = Table(
    table_id=30,
    table_name="m_moved_tag",
    columns=(
        ("s_tag_id", "INT", "NOT NULL"),
        ("e_tag_id", "INT", "NOT NULL"),
    ),
    primary=("s_tag_id", "e_tag_id"),
    foreign=(
        ("s_tag_id", "m_tag", "tag_id"),
        ("e_tag_id", "m_tag", "tag_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    indexes=(("moved_tag_end_idx", ("e_tag_id",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 31. m_blob_symbol_def (table_id=31): Per-Blob Symbol Definitions
#     Definitions that cannot be stored once per tag (outside their tag's lines, tagless, ambiguous key, or a
#     reused tag whose definitions changed); the m_symbol_def view adds the m_tag_def rows (core/SymbolRows.py)
# -----------------------------------------------------------------------------
m_blob_symbol_def = Table(
    table_id=31,
    table_name="m_blob_symbol_def",
    columns=(
        ("def_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("blob_id", "INT", "NOT NULL"),
        ("tag_id", "INT", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
        ("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("type_id", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("linkage", "TINYINT", "UNSIGNED", "NOT NULL", "DEFAULT 0"),
        ("line_s", "INT", "NOT NULL"),
        ("line_e", "INT", "NOT NULL"),
    ),
    primary=("def_id",),
    foreign=(
        ("blob_id", "m_file_blob", "blob_id"),
        ("tag_id", "m_tag", "tag_id"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    # Write-only for the parser (rows are only staged with set()): caching them would maintain in-memory rows
    # and indexes per definition, and the startup preload would filter blob_id against a commit id (Old_VID)
    te_cached=False,
    blob_scoped=True,
    indexes=(
        ("sym_def_blob_name_idx", ("blob_id", "name", "type_id")),
        ("sym_def_name_idx", ("name", "type_id")),
        ("sym_def_ast_idx", ("ast_id",)),
    ),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 32. m_blob_symbol_ref (table_id=32): Per-Blob Symbol Occurrences & Usages
#     References that cannot be stored once per tag (same cases as m_blob_symbol_def); the m_symbol_ref view
#     adds the m_tag_ref rows resolved through m_blob_symbol
# -----------------------------------------------------------------------------
m_blob_symbol_ref = Table(
    table_id=32,
    table_name="m_blob_symbol_ref",
    columns=(
        ("ref_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("blob_id", "INT", "NOT NULL"),
        ("tag_id", "INT", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
        ("target_fnid", "INT", "NOT NULL", "DEFAULT 0"),
        ("role", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("line", "INT", "NOT NULL"),
        ("char_s", "INT", "NOT NULL"),
    ),
    primary=("ref_id",),
    foreign=(
        ("blob_id", "m_file_blob", "blob_id"),
        ("tag_id", "m_tag", "tag_id"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    blob_scoped=True,
    indexes=(
        ("sym_ref_blob_idx", ("blob_id",)),
        ("sym_ref_ast_idx", ("ast_id", "role")),
        ("sym_ref_tgt_idx", ("target_fnid", "ast_id")),
    ),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 33. m_file_reference (table_id=33): Unified Blob-Scoped Cross-File Provenance
# -----------------------------------------------------------------------------
m_file_reference = Table(
    table_id=33,
    table_name="m_file_reference",
    columns=(
        ("ref_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("source_blob_id", "INT", "NOT NULL"),
        ("target_fnid", "INT", "NOT NULL"),
        ("ref_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("line_no", "INT", "NOT NULL"),
        ("details", "VARCHAR(255)", "DEFAULT ''", "COLLATE utf8mb4_bin"),
    ),
    primary=("ref_id",),
    foreign=(
        ("source_blob_id", "m_file_blob", "blob_id"),
        ("target_fnid", "m_file_name", "fnid"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    blob_scoped=True,
    indexes=(
        ("fref_src_idx", ("source_blob_id", "ref_type")),
        ("fref_tgt_idx", ("target_fnid", "ref_type")),
    ),
    unique_keys=(("source_blob_id", "target_fnid", "ref_type", "line_no"),),
    hashing_table=False,
)
setattr(m_file_reference, "source_fid", m_file_reference.source_blob_id)

# -----------------------------------------------------------------------------
# 34. m_db_instance (table_id=34): Database Instance Fingerprint
# -----------------------------------------------------------------------------
m_db_instance = Table(
    table_id=34,
    table_name="m_db_instance",
    columns=(
        ("instance_hash", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("created_at", "BIGINT", "NOT NULL"),
    ),
    primary=("instance_hash",),
    foreign=None,
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 35. m_ast_attribute (table_id=35): AST Compiler & Kernel Attributes
# -----------------------------------------------------------------------------
m_ast_attribute = Table(
    table_id=35,
    table_name="m_ast_attribute",
    columns=(
        ("ast_id", "INT", "NOT NULL"),
        ("priority", "SMALLINT", "UNSIGNED", "NOT NULL"),
        ("name", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("val", "VARCHAR(255)", "DEFAULT ''", "COLLATE utf8mb4_bin"),
    ),
    primary=("ast_id", "priority"),
    foreign=(("ast_id", "m_ast", "ast_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 36. m_git_network (table_id=36): Git Object Pool & Root Commit Scope
# -----------------------------------------------------------------------------
m_git_network = Table(
    table_id=36,
    table_name="m_git_network",
    columns=(
        ("network_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("project_id", "INT", "NOT NULL"),
        ("name", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("root_commit_hash", "BINARY(20)", "NOT NULL"),
    ),
    primary=("network_id",),
    foreign=(("project_id", "m_project", "project_id"),),
    initial_insert=((0, 0, "default", b"\x00" * 20),),
    no_duplicate=False,
    te_cached=True,
    unique_keys=(("root_commit_hash",),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 37. m_repository (table_id=37): Repository Remote / Clone Descriptor
# -----------------------------------------------------------------------------
m_repository = Table(
    table_id=37,
    table_name="m_repository",
    columns=(
        ("repo_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("network_id", "INT", "NOT NULL"),
        ("name", "VARCHAR(64)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("git_url", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("default_branch", "VARCHAR(64)", "NOT NULL", "DEFAULT 'main'", "COLLATE utf8mb4_bin"),
    ),
    primary=("repo_id",),
    foreign=(("network_id", "m_git_network", "network_id"),),
    initial_insert=((0, 0, "default", "", "main"),),
    no_duplicate=True,
    unique_keys=(("network_id", "name"),),
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 38. m_git_ref (table_id=38): Named Git Reference (Branch, Tag, Release)
# -----------------------------------------------------------------------------
m_git_ref = Table(
    table_id=38,
    table_name="m_git_ref",
    columns=(
        ("ref_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("repo_id", "INT", "NOT NULL"),
        ("ref_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("ref_name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("commit_id", "INT", "NOT NULL"),
        ("tagger_id", "INT", "NULL"),
        ("tagger_date", "INT", "NULL"),
        ("tag_message", "TEXT", "NULL"),
    ),
    primary=("ref_id",),
    foreign=(
        ("repo_id", "m_repository", "repo_id"),
        ("commit_id", "m_commit", "commit_id"),
    ),
    initial_insert=((0, 0, 1, "default", 0, None, None, None),),
    no_duplicate=False,
    te_cached=True,
    unique_keys=(("repo_id", "ref_name"),),
    indexes=(("git_ref_cid_idx", ("commit_id",)),),
    hashing_table=False,
)
m_v_main = m_git_ref
setattr(m_git_ref, "vid", m_git_ref.commit_id)
setattr(m_git_ref, "vname", m_git_ref.ref_name)

# -----------------------------------------------------------------------------
# 39. m_commit_parent (table_id=39): Full Git Commit Topology (First & Merge Parents)
# -----------------------------------------------------------------------------
m_commit_parent = Table(
    table_id=39,
    table_name="m_commit_parent",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        ("parent_commit_id", "INT", "NOT NULL"),
        ("parent_idx", "SMALLINT", "UNSIGNED", "NOT NULL"),
    ),
    primary=("commit_id", "parent_idx"),
    foreign=(
        ("commit_id", "m_commit", "commit_id"),
        ("parent_commit_id", "m_commit", "commit_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    # Write-only for the pipeline and idempotent: duplicate rows are skipped by the database, so the
    # table is not preloaded every version (it grows with every ingested commit)
    te_cached=False,
    idempotent_pk=True,
    indexes=(("cp_parent_idx", ("parent_commit_id", "parent_idx")),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 40. m_ingest_state (table_id=40): Atomic Ingestion Progress & Crash Recovery
# -----------------------------------------------------------------------------
m_ingest_state = Table(
    table_id=40,
    table_name="m_ingest_state",
    columns=(
        ("network_id", "INT", "NOT NULL"),
        ("commit_id", "INT", "NOT NULL"),
        ("status", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("updated_at", "BIGINT", "NOT NULL"),
    ),
    primary=("network_id", "commit_id"),
    foreign=(
        ("network_id", "m_git_network", "network_id"),
        ("commit_id", "m_commit", "commit_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 41. m_tag_def (table_id=41): Symbol Definitions of a Tag, Relative to the Tag's First Line
#     Written once, when the tag is new; every blob placing the tag (m_bridge_tag) shares them. ast_id is not
#     stored: it is resolved per blob through m_blob_symbol (sym_key), like a reference's target.
# -----------------------------------------------------------------------------
m_tag_def = Table(
    table_id=41,
    table_name="m_tag_def",
    columns=(
        ("tag_id", "INT", "NOT NULL"),
        ("def_idx", "SMALLINT", "UNSIGNED", "NOT NULL"),
        ("sym_key", "BIGINT", "NOT NULL"),
        ("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("type_id", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("linkage", "TINYINT", "UNSIGNED", "NOT NULL", "DEFAULT 0"),
        ("rel_line_s", "INT", "NOT NULL"),
        ("rel_line_e", "INT", "NOT NULL"),
    ),
    primary=("tag_id", "def_idx"),
    foreign=(("tag_id", "m_tag", "tag_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    indexes=(("tag_def_name_idx", ("name", "type_id")), ("tag_def_key_idx", ("sym_key", "tag_id"))),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 42. m_tag_ref (table_id=42): Symbol References of a Tag, Relative to the Tag's First Line
#     Written once, when the tag is new. sym_key identifies the target without resolving it (core/SymbolRows.py);
#     m_blob_symbol resolves it per blob, since a target's AST id changes whenever its definition does.
# -----------------------------------------------------------------------------
m_tag_ref = Table(
    table_id=42,
    table_name="m_tag_ref",
    columns=(
        ("tag_id", "INT", "NOT NULL"),
        ("ref_idx", "INT", "UNSIGNED", "NOT NULL"),
        ("sym_key", "BIGINT", "NOT NULL"),
        ("role", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("rel_line", "INT", "NOT NULL"),
        ("char_s", "INT", "NOT NULL"),
    ),
    primary=("tag_id", "ref_idx"),
    foreign=(("tag_id", "m_tag", "tag_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    indexes=(("tag_ref_key_idx", ("sym_key", "tag_id")),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 43. m_blob_symbol (table_id=43): Per-Blob Resolution of Symbol Keys
#     One row per distinct sym_key the blob's shared tag rows use: the AST id (and file) it resolves to here.
# -----------------------------------------------------------------------------
m_blob_symbol = Table(
    table_id=43,
    table_name="m_blob_symbol",
    columns=(
        ("blob_id", "INT", "NOT NULL"),
        ("sym_key", "BIGINT", "NOT NULL"),
        ("target_ast_id", "INT", "NOT NULL"),
        ("target_fnid", "INT", "NOT NULL", "DEFAULT 0"),
    ),
    primary=("blob_id", "sym_key"),
    foreign=(
        ("blob_id", "m_file_blob", "blob_id"),
        ("target_ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    # A blob parsed twice in one run writes the same rows: duplicates are skipped
    idempotent_pk=True,
    indexes=(("blob_symbol_target_idx", ("target_ast_id",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 44. m_symbol_own (table_id=44): Tag Placements Whose Symbols Are Stored Per Blob
#     kind 1 = references, 2 = definitions: the views skip that tag's shared rows in that blob and the
#     m_blob_symbol_ref / m_blob_symbol_def rows stand in for them.
# -----------------------------------------------------------------------------
m_symbol_own = Table(
    table_id=44,
    table_name="m_symbol_own",
    columns=(
        ("blob_id", "INT", "NOT NULL"),
        ("tag_id", "INT", "NOT NULL"),
        ("kind", "TINYINT", "UNSIGNED", "NOT NULL"),
    ),
    primary=("blob_id", "tag_id", "kind"),
    foreign=(
        ("blob_id", "m_file_blob", "blob_id"),
        ("tag_id", "m_tag", "tag_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 45. m_ingest_watermark (table_id=45): Highest Id of Each Id-Allocating Table When a Version Run Started
#     Rows above these marks were created by that run; core/ingest_state.py rolls them back when the
#     run was interrupted (m_ingest_state still INGEST_PARSING). Removed once the version is done.
#     No foreign keys: the bookkeeping must survive the rollback it drives.
# -----------------------------------------------------------------------------
m_ingest_watermark = Table(
    table_id=45,
    table_name="m_ingest_watermark",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        # Not "table_id": Table binds every column name as an attribute of the instance
        ("marked_table_id", "SMALLINT", "UNSIGNED", "NOT NULL"),
        ("max_id", "BIGINT", "NOT NULL"),
    ),
    primary=("commit_id", "marked_table_id"),
    foreign=(),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 46. m_ingest_blob (table_id=46): Pre-Existing Blobs a Version Run Parses
#     Blobs created by earlier runs without being parsed (intermediate commits): their derived rows
#     are cleared too when the run is rolled back. Removed once the version is done.
# -----------------------------------------------------------------------------
m_ingest_blob = Table(
    table_id=46,
    table_name="m_ingest_blob",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        ("blob_id", "INT", "NOT NULL"),
    ),
    primary=("commit_id", "blob_id"),
    foreign=(),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 47. m_arch_root (table_id=47): Build Layout of Each ARCH at a Version
#     What `make ARCH=<arch>` uses at the version's head commit, evaluated from its Makefiles by
#     parser/arch_layout.py: role 1 source directory, 2 include directory (priority = -I order),
#     3 symlink the build creates (path -> target), 4 generated asm-generic wrapper (path -> the
#     header it includes), 5 forced -include file. Directories end with "/".
# -----------------------------------------------------------------------------
m_arch_root = Table(
    table_id=47,
    table_name="m_arch_root",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        ("arch", "VARCHAR(32)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("role", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("priority", "SMALLINT", "UNSIGNED", "NOT NULL"),
        ("path", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("target", "VARCHAR(255)", "DEFAULT ''", "COLLATE utf8mb4_bin"),
    ),
    primary=("commit_id", "arch", "role", "path"),
    foreign=(("commit_id", "m_commit", "commit_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 48. m_commit_email (table_id=48): Git Commit to Mailing List Message Linkage
# -----------------------------------------------------------------------------
m_commit_email = Table(
    table_id=48,
    table_name="m_commit_email",
    columns=(
        ("commit_id", "INT", "NOT NULL"),
        ("message_id", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ("thread_id", "BIGINT", "UNSIGNED", "NOT NULL", "DEFAULT 0"),
        ("relation_type", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("match_tier", "TINYINT", "UNSIGNED", "NOT NULL"),
        ("patch_version", "TINYINT", "UNSIGNED", "NOT NULL", "DEFAULT 1"),
        ("patch_idx", "SMALLINT", "UNSIGNED", "NOT NULL", "DEFAULT 0"),
        ("patch_total", "SMALLINT", "UNSIGNED", "NOT NULL", "DEFAULT 0"),
    ),
    primary=("commit_id", "message_id", "relation_type"),
    foreign=(("commit_id", "m_commit", "commit_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# Exported Table Array tuple
# -----------------------------------------------------------------------------
TABLES: tuple[Table, ...] = (
    m_project,             # 0
    m_file_name,           # 1
    m_file_blob,           # 2
    m_bridge_checkpoint,   # 3
    m_blob_transition,     # 4
    m_type_descriptor,     # 5
    m_ast,                 # 6
    m_ast_container,       # 7
    m_ast_include,         # 8
    m_ast_debug,           # 9
    m_tag_code,            # 10
    m_tag,                 # 11
    m_bridge_tag,          # 12
    m_map_ast,             # 13
    m_bridge_map,          # 14
    m_ast_hash,            # 15
    m_kconfig_symbol,      # 16
    m_kconfig_relation,    # 17
    m_kconfig_tree,        # 18
    m_kconfig_kbuild,      # 19
    m_maintainer_person,   # 20
    m_maintainer_section,  # 21
    m_maintainer_member,   # 22
    m_maintainer_pattern,  # 23
    m_maintainer_file,     # 24
    m_credits_entry,       # 25
    m_commit,              # 26
    m_bridge_commit_person,# 27
    m_commit_file,         # 28
    m_bridge_commit_tag,   # 29
    m_moved_tag,           # 30
    m_blob_symbol_def,     # 31
    m_blob_symbol_ref,     # 32
    m_file_reference,      # 33
    m_db_instance,         # 34
    m_ast_attribute,       # 35
    m_git_network,         # 36
    m_repository,          # 37
    m_git_ref,             # 38
    m_commit_parent,       # 39
    m_ingest_state,        # 40
    m_tag_def,             # 41
    m_tag_ref,             # 42
    m_blob_symbol,         # 43
    m_symbol_own,          # 44
    m_ingest_watermark,    # 45
    m_ingest_blob,         # 46
    m_arch_root,           # 47
    m_commit_email,        # 48
)

Table_Array = TABLES


def init_db_layout(gp=None) -> tuple[Table, ...]:
    """Initialize and populate gp.Table_Array with all 49 schema tables.

    Args:
        gp: Optional GreatProcessor instance to attach Table_Array to.

    Returns:
        Immutable tuple of all 49 Table schema objects.
    """
    if gp is not None:
        gp.Table_Array = list(TABLES)
    return TABLES
