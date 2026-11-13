"""DBLayout.py - Relational Database Schema & Content-Addressable Table Definitions.

===============================================================================
RELATIONAL DATABASE SCHEMA REFERENCE GUIDE FOR GIT DAG & CONTENT-ADDRESSABLE ARCH
===============================================================================
This module defines the 66 core relational database tables used across the parser
pipeline (`Table_Array` / `TABLES`).

COLUMN DEFINITIONS are neutral `(name, ColumnSpec)` pairs built with the constructors of `db_engine.spec`
(`INT`, `BIGINT`, `MEDIUMINT`, `SMALLINT`, `TINYINT`, `CHAR`, `VARCHAR`, `TEXT`, `MEDIUMTEXT`, `BIN`, `MEDIUMBLOB`):
`unsigned=True`, `null=True|None` (columns are NOT NULL by default), `default=<literal>`, `auto=True`
(generated id), `bin_collation=True` (case-sensitive text). Each backend renders its own DDL from them
(`db_engine.sql_render.render_create_table`, MariaDB output pinned by `unit_test/test_ddl_render.py`).

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
     ├── m_ingest_state (network_id, commit_id, status, updated_at, base_commit_id, worker_id)
     ├── m_ingest_watermark (commit_id, marked_table_id, max_id)
     ├── m_ingest_blob (commit_id, blob_id)
     ├── m_checkpoint_base (commit_id, base_commit_id, delta_rows)   -- checkpoint kept as a delta of base_commit_id
     ├── m_arch_root (commit_id, arch, layout_id)   -- build layout per ARCH
     │     └── m_arch_layout (layout_id, layout_hash) -> m_arch_layout_entry (layout_id, role, priority, path, target)
     ├── m_commit_email (commit_id, message_id, thread_id, relation_type, match_tier, patch_version, patch_idx, patch_total)
     ├── m_commit_relation (commit_id, kind, idx, target_hash, target_prefix, note)   -- Fixes:, reverts, backports, Cc: stable
     └── m_commit_link (commit_id, message_id)                     -- Link: / Message-Id: trailers

3. Universal Content-Addressable Stores:
   m_file_name (fnid, fname)
   m_file_blob (blob_id, blob_hash, ftype)
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
   m_blob_symbol (blob_id, sym_key, target_ast_id)                -- per-blob resolution of sym_key
   m_blob_symbol_file (blob_id, sym_key, target_fnid)             -- the few keys whose target has a file
   m_symbol_own (blob_id, tag_id, kind)                           -- placements stored per blob instead
   m_blob_symbol_def (def_id, blob_id, tag_id, ast_id, name, type_id, linkage, line_s, line_e)
   m_blob_symbol_ref (ref_id, blob_id, tag_id, ast_id, target_fnid, role, line, char_s)
   Views m_symbol_def / m_symbol_ref (main.ensure_db_views) return both layouts in the per-blob shape.
   m_file_reference (ref_id, source_blob_id, source_fnid, target_fnid, ref_type, line_no, details)
   m_kbuild_symbol (source_blob_id, source_fnid, idx, symbol, via, line_no, compile_mode, kind, target_obj,
                    target_path, target_fnid, rule)                -- Kbuild rules with their CONFIG_ symbols

6. Kconfig: no dedicated tables. Kconfig entries are tags like any other code (m_ast name + type
   Kconfig_Config/Kconfig_Menuconfig/... -> m_tag -> m_bridge_tag); everything else (menus,
   dependencies, values) comes from the semantic model built from Git by the webapp
   (parser/kconfig_ast/KCONFIG_API.md section 8).

7. Source facts and CVEs (read by the webapp's source index and CVE pages):
   m_blob_facts (blob_hash, family, format, data)                 -- per-blob facts: calls/ops tables (1), Kernel API (2),
                                                                     includes (3), Kconfig definitions (4), doc labels (5),
                                                                     man pages (6)
   m_cve_source (name, layout, head, format, records, built_at)   -- the CVE list ingested
     └── m_cve (cve_id, kind, published, updated, score, kev, record_path, title, data)
           ├── m_cve_commit (commit_hash, role, cve_id)
           └── m_cve_file (cve_id, file_path)                     -- affected kernel source and header files
   m_cve_commit_hash (repo_id, prefix, commit_hash)               -- abbreviated CVE hashes resolved
   m_release_segment (segment_id, repo_id, tag, tag_hash, chain, base_tag, base_hash, commits, merges,
                      diff_base_tag, files_changed, lines_added, lines_removed, format)  -- release ranges
     ├── m_release_person (segment_id, person_id, role_type, commits)     -- contributors per range
     ├── m_person_first_release (repo_id, person_id, segment_id)        -- first mainline release authored in
     └── m_commit_release (repo_id, commit_hash, chain, segment_id)      -- first segment of each commit, per chain

8. Maintainers & Credits Subsystem:
   m_maintainer_section (sec_id, blob_id, name, status, scm_tree, web_page, mailing_list, ast_id, patchwork, bug_url,
                         chat_channel, profile_doc)
     ├── m_maintainer_member (sec_id, person_id, role_type, priority)
     ├── m_maintainer_pattern (sec_id, pat_type, pattern, priority)
     ├── m_maintainer_file (sec_id, fnid)                          -- files of a section version
   m_credits_entry (credit_id, blob_id, person_id, web_page, pgp_key, description, snail_mail, ast_id)
   m_person_identity (person_id, identity_key)                    -- developer identity of each person alias

9. Distributed Parsing & Multi-Computer Coordination:
   m_table_sequence (seq_table_id, next_id)                       -- atomic ID block allocator sequences
   m_ingest_id_range (commit_id, marked_table_id, start_id, end_id) -- leased ID ranges per version run
   m_parse_node (node_id, network_id, role, hostname, pid, active_commit_id, heartbeat_at) -- active parser nodes
   m_parse_task (task_id, network_id, chain_name, chain_kind, seq_order, ref_name, commit_id, base_ref_name,
                 base_commit_id, status, worker_id, lease_expires_at, retry_count, error_msg) -- secondary work queue
===============================================================================
"""
from __future__ import annotations

import secrets
import time
from core.globalstuff import ASTT
from core.TableHandling import Table
from db_engine.spec import (
    BIGINT,
    BIN,
    CHAR,
    INT,
    MEDIUMBLOB,
    MEDIUMINT,
    MEDIUMTEXT,
    SMALLINT,
    TEXT,
    TINYINT,
    VARCHAR,
)

# Pre-populate AST type descriptor entries from ASTT enum
m_type_descriptor_insert = tuple((ast.value, ast.name) for ast in ASTT)

# -----------------------------------------------------------------------------
# 0. m_project (table_id=0): Multi-Tenant Project / Ecosystem Scope
# -----------------------------------------------------------------------------
m_project = Table(
    table_id=0,
    table_name="m_project",
    columns=(
        ("project_id", INT(auto=True)),
        ("name", VARCHAR(64, bin_collation=True)),
        ("slug", VARCHAR(32, bin_collation=True)),
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
        ("fnid", INT(auto=True)),
        ("fname", VARCHAR(255, bin_collation=True)),
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
        ("blob_id", INT(auto=True)),
        ("blob_hash", BIN(20)),
        ("ftype", TINYINT(unsigned=True)),
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
        ("commit_id", INT()),
        ("fnid", INT()),
        ("blob_id", INT()),
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
        ("old_blob_id", INT()),
        ("new_blob_id", INT()),
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
        ("type_id", SMALLINT(unsigned=True)),
        ("name", VARCHAR(64, bin_collation=True)),
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
        ("ast_id", INT(auto=True)),
        ("name", VARCHAR(255, bin_collation=True)),
        ("type_id", SMALLINT(unsigned=True)),
    ),
    primary=("ast_id",),
    # type_id names an m_type_descriptor row (the ASTT values, seeded at creation and never written again): no
    # foreign key, which only made InnoDB keep a type_id index nothing searches (lookups go through ast_index)
    foreign=None,
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
        ("ast_id", INT()),
        ("priority", SMALLINT(unsigned=True)),
        ("type_id", SMALLINT(unsigned=True)),
        ("ref_ast_id", INT()),
    ),
    primary=("ast_id", "priority"),
    # type_id (the child's role in its parent, differing from the child's own m_ast.type_id in 41% of rows) names
    # an m_type_descriptor row without a foreign key: InnoDB would keep a type_id index for it (~5 GB on a full
    # database) that no query uses; children are read by ast_id (primary key) and ref_ast_id (ast_cont_ref_idx)
    foreign=(
        ("ast_id", "m_ast", "ast_id"),
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
        ("ast_id", INT()),
        ("fnid", INT()),
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
        ("ast_id", INT()),
        ("ast_raw", MEDIUMTEXT()),
    ),
    primary=("ast_id",),
    foreign=(("ast_id", "m_ast", "ast_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 10. m_tag (table_id=10): Immutable & Recyclable AST Code Tag Entity
# -----------------------------------------------------------------------------
m_tag = Table(
    table_id=10,
    table_name="m_tag",
    columns=(
        ("tag_id", INT(auto=True)),
        ("hash", BIN(32)),
        ("ast_id", INT()),
        ("hl_s", INT()),
        ("hl_l", INT()),
    ),
    primary=("tag_id",),
    foreign=(
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=((0, b"\x00" * 32, 0, 0, 0),),
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
    indexes=(("tag_hash_idx", ("hash",)), ("tag_ast_idx", ("ast_id",))),
)

# -----------------------------------------------------------------------------
# 11. m_bridge_tag (table_id=11): Tag Occurrence Coordinates within Content Blob
# -----------------------------------------------------------------------------
m_bridge_tag = Table(
    table_id=11,
    table_name="m_bridge_tag",
    columns=(
        ("blob_id", INT()),
        ("tag_id", INT()),
        ("line_s", MEDIUMINT()),
        ("line_e", MEDIUMINT()),
        ("char_s", MEDIUMINT()),
        ("char_e", MEDIUMINT()),
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
# 12. m_map_ast (table_id=12): Tag-Relative AST Overlay Spans
# -----------------------------------------------------------------------------
m_map_ast = Table(
    table_id=12,
    table_name="m_map_ast",
    columns=(
        ("map_id", INT()),
        ("line_s", MEDIUMINT()),
        ("char_s", MEDIUMINT()),
        ("line_e", MEDIUMINT()),
        ("char_e", MEDIUMINT()),
        ("ast_id", INT()),
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
# 13. m_bridge_map (table_id=13): Tag to AST Map Linkage (no longer written)
# Every row it ever held was (tag_id, tag_id): m_map_ast.map_id is the tag id, so readers join
# m_map_ast.map_id = m_bridge_tag.tag_id directly. The table stays registered so table ids never shift
# (m_ast_hash hashes join graphs, table ids included).
# -----------------------------------------------------------------------------
m_bridge_map = Table(
    table_id=13,
    table_name="m_bridge_map",
    columns=(
        ("tag_id", INT()),
        ("map_id", INT()),
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
# 14. m_ast_hash (table_id=14): Cryptographic AST Content Hash Index
#     The first 16 bytes of the SHA-256 of a node's join graph and columns (table_engine compute_ast_hash()):
#     128 bits keep collisions out of reach (~1e-23 for 1e8 nodes) at half the key size of the full digest.
# -----------------------------------------------------------------------------
m_ast_hash = Table(
    table_id=14,
    table_name="m_ast_hash",
    columns=(
        ("hash", BIN(16)),
        ("ast_id", INT()),
    ),
    primary=("hash",),
    foreign=(("ast_id", "m_ast", "ast_id"),),
    initial_insert=((b"\x00" * 16, 0),),
    no_duplicate=False,
    te_cached=True,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 15. m_maintainer_person (table_id=15): Contributor Identity Registry
# -----------------------------------------------------------------------------
m_maintainer_person = Table(
    table_id=15,
    table_name="m_maintainer_person",
    columns=(
        ("person_id", INT(auto=True)),
        ("name", VARCHAR(128, bin_collation=True)),
        ("email", VARCHAR(128, bin_collation=True)),
    ),
    primary=("person_id",),
    foreign=None,
    initial_insert=((0, "", ""),),
    no_duplicate=True,
    te_cached=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 16. m_maintainer_section (table_id=16): Blob-Scoped Subsystem Definitions
# -----------------------------------------------------------------------------
m_maintainer_section = Table(
    table_id=16,
    table_name="m_maintainer_section",
    columns=(
        ("sec_id", INT(auto=True)),
        ("blob_id", INT()),
        ("name", VARCHAR(128, bin_collation=True)),
        ("status", VARCHAR(32, bin_collation=True)),
        ("scm_tree", VARCHAR(255, bin_collation=True)),
        ("web_page", VARCHAR(255, bin_collation=True)),
        ("mailing_list", VARCHAR(255, bin_collation=True)),
        ("ast_id", INT()),
        ("patchwork", VARCHAR(255, null=True)),
        ("bug_url", VARCHAR(255, null=True)),
        ("chat_channel", VARCHAR(255, null=True)),
        ("profile_doc", VARCHAR(255, null=True)),
    ),
    primary=("sec_id",),
    foreign=(
        ("blob_id", "m_file_blob", "blob_id"),
        ("ast_id", "m_ast", "ast_id"),
    ),
    initial_insert=((0, 0, "", "", "", "", "", 0, None, None, None, None),),
    no_duplicate=True,
    te_cached=True,
    indexes=(("maint_sec_blob_idx", ("blob_id",)), ("maint_sec_ast_idx", ("ast_id",))),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 17. m_maintainer_member (table_id=17): Section Maintainer Members
# -----------------------------------------------------------------------------
m_maintainer_member = Table(
    table_id=17,
    table_name="m_maintainer_member",
    columns=(
        ("sec_id", INT()),
        ("person_id", INT()),
        ("role_type", TINYINT(unsigned=True)),
        ("priority", INT()),
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
    # The profile and the Developers directory look members up by person
    indexes=(("maint_member_person_idx", ("person_id",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 18. m_maintainer_pattern (table_id=18): Section File Matching Patterns
# -----------------------------------------------------------------------------
m_maintainer_pattern = Table(
    table_id=18,
    table_name="m_maintainer_pattern",
    columns=(
        ("sec_id", INT()),
        ("pat_type", TINYINT(unsigned=True)),
        ("pattern", VARCHAR(255, bin_collation=True)),
        ("priority", INT()),
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
# 19. m_maintainer_file (table_id=19): Files a MAINTAINERS Section Version Covers
#     A section matches a path through its own F:/X:/N: patterns only, and an unchanged section keeps its
#     sec_id across MAINTAINERS blobs, so (sec_id, fnid) holds for every MAINTAINERS blob containing the
#     section: Step 6.7 matches changed sections against every path and the others against new paths only.
# -----------------------------------------------------------------------------
m_maintainer_file = Table(
    table_id=19,
    table_name="m_maintainer_file",
    columns=(
        ("sec_id", INT()),
        ("fnid", INT()),
    ),
    primary=("sec_id", "fnid"),
    foreign=(
        ("sec_id", "m_maintainer_section", "sec_id"),
        ("fnid", "m_file_name", "fnid"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    # file -> sections (the webapp's reader); section -> files is the primary key's prefix
    indexes=(("maint_file_fnid_idx", ("fnid", "sec_id")),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 20. m_credits_entry (table_id=20): Linux CREDITS Contributor Biographies
# -----------------------------------------------------------------------------
m_credits_entry = Table(
    table_id=20,
    table_name="m_credits_entry",
    columns=(
        ("credit_id", INT(auto=True)),
        ("blob_id", INT()),
        ("person_id", INT()),
        ("web_page", VARCHAR(255, bin_collation=True)),
        ("pgp_key", VARCHAR(255, bin_collation=True)),
        ("description", MEDIUMTEXT()),
        ("snail_mail", MEDIUMTEXT()),
        ("ast_id", INT()),
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
    indexes=(("credits_blob_idx", ("blob_id",)), ("credits_ast_idx", ("ast_id",))),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 21. m_commit (table_id=21): Git Commit Metadata & Topology Node
# -----------------------------------------------------------------------------
m_commit = Table(
    table_id=21,
    table_name="m_commit",
    columns=(
        ("commit_id", INT(auto=True)),
        ("network_id", INT()),
        ("commit_hash", BIN(20)),
        ("author_id", INT()),
        ("author_date", INT(unsigned=True)),
        ("committer_id", INT()),
        ("committer_date", INT(unsigned=True)),
        ("subject", VARCHAR(500, bin_collation=True)),
        ("message", MEDIUMTEXT()),
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
# 22. m_bridge_commit_person (table_id=22): Git Commit Contributor Attribution
# -----------------------------------------------------------------------------
m_bridge_commit_person = Table(
    table_id=22,
    table_name="m_bridge_commit_person",
    columns=(
        ("commit_id", INT()),
        ("person_id", INT()),
        ("role_type", TINYINT(unsigned=True)),
        ("priority", SMALLINT(unsigned=True)),
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
# 23. m_commit_file (table_id=23): First-Parent Commit Tree Delta
# -----------------------------------------------------------------------------
m_commit_file = Table(
    table_id=23,
    table_name="m_commit_file",
    columns=(
        ("commit_id", INT()),
        ("fnid", INT()),
        ("blob_id", INT()),
        ("old_fnid", INT()),
        ("old_blob_id", INT()),
        ("change_type", CHAR(1)),
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
# 24. m_bridge_commit_tag (table_id=24): Tag Lifecycle Introduced in Commit
# -----------------------------------------------------------------------------
m_bridge_commit_tag = Table(
    table_id=24,
    table_name="m_bridge_commit_tag",
    columns=(
        ("commit_id", INT()),
        ("blob_id", INT()),
        ("tag_id", INT()),
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
# 25. m_moved_tag (table_id=25): Content Tag Evolution (M:N Cross-Branch DAG)
# -----------------------------------------------------------------------------
m_moved_tag = Table(
    table_id=25,
    table_name="m_moved_tag",
    columns=(
        ("s_tag_id", INT()),
        ("e_tag_id", INT()),
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
# 26. m_blob_symbol_def (table_id=26): Per-Blob Symbol Definitions
#     Definitions that cannot be stored once per tag (outside their tag's lines, tagless, ambiguous key, or a
#     reused tag whose definitions changed); the m_symbol_def view adds the m_tag_def rows (core/SymbolRows.py)
# -----------------------------------------------------------------------------
m_blob_symbol_def = Table(
    table_id=26,
    table_name="m_blob_symbol_def",
    columns=(
        ("def_id", INT(auto=True)),
        ("blob_id", INT()),
        ("tag_id", INT()),
        ("ast_id", INT()),
        ("name", VARCHAR(255, bin_collation=True)),
        ("type_id", TINYINT(unsigned=True)),
        ("linkage", TINYINT(unsigned=True, default=0)),
        ("line_s", MEDIUMINT()),
        ("line_e", MEDIUMINT()),
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
# 27. m_blob_symbol_ref (table_id=27): Per-Blob Symbol Occurrences & Usages
#     References that cannot be stored once per tag (same cases as m_blob_symbol_def); the m_symbol_ref view
#     adds the m_tag_ref rows resolved through m_blob_symbol
# -----------------------------------------------------------------------------
m_blob_symbol_ref = Table(
    table_id=27,
    table_name="m_blob_symbol_ref",
    columns=(
        ("ref_id", INT(auto=True)),
        ("blob_id", INT()),
        ("tag_id", INT()),
        ("ast_id", INT()),
        ("target_fnid", INT(default=0)),
        ("role", TINYINT(unsigned=True)),
        ("line", MEDIUMINT()),
        ("char_s", MEDIUMINT()),
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
    ),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 28. m_file_reference (table_id=28): Unified Blob-Scoped Cross-File Provenance
# -----------------------------------------------------------------------------
m_file_reference = Table(
    table_id=28,
    table_name="m_file_reference",
    columns=(
        ("ref_id", INT(auto=True)),
        ("source_blob_id", INT()),
        ("source_fnid", INT()),
        ("target_fnid", INT()),
        ("ref_type", TINYINT(unsigned=True)),
        ("line_no", MEDIUMINT()),
        ("details", VARCHAR(255, null=None, default='', bin_collation=True)),
    ),
    primary=("ref_id",),
    foreign=(
        ("source_blob_id", "m_file_blob", "blob_id"),
        ("source_fnid", "m_file_name", "fnid"),
        ("target_fnid", "m_file_name", "fnid"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    blob_scoped=True,
    # "Who references this file" (webapp GET /api/fs/references): covering, so the ~100k rows of a common
    # header are read from the index alone (the source blob and path filter the revision). It replaces
    # fref_tgt_idx (target_fnid, ref_type), its prefix; databases that have it keep it. Existing databases
    # get it at parser start (main.ensure_declared_indexes()).
    indexes=(
        ("fref_tgt_src_idx", ("target_fnid", "ref_type", "source_blob_id", "source_fnid", "line_no")),
    ),
    # One row set per (path, blob) pair: Kbuild and documentation references resolve relative to the
    # source file's path, so identical copies of a Makefile in two directories reference different files.
    # The unique key's (source_blob_id, source_fnid, ...) prefix serves lookups from the source side.
    unique_keys=(("source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no"),),
    hashing_table=False,
)
setattr(m_file_reference, "source_fid", m_file_reference.source_blob_id)

# -----------------------------------------------------------------------------
# 29. m_db_instance (table_id=29): Database Instance Fingerprint
# -----------------------------------------------------------------------------
m_db_instance = Table(
    table_id=29,
    table_name="m_db_instance",
    columns=(
        ("instance_hash", VARCHAR(64, bin_collation=True)),
        ("created_at", BIGINT()),
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
# 30. m_git_network (table_id=30): Git Object Pool & Root Commit Scope
# -----------------------------------------------------------------------------
m_git_network = Table(
    table_id=30,
    table_name="m_git_network",
    columns=(
        ("network_id", INT(auto=True)),
        ("project_id", INT()),
        ("name", VARCHAR(64, bin_collation=True)),
        ("root_commit_hash", BIN(20)),
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
# 31. m_repository (table_id=31): Repository Remote / Clone Descriptor
# -----------------------------------------------------------------------------
m_repository = Table(
    table_id=31,
    table_name="m_repository",
    columns=(
        ("repo_id", INT(auto=True)),
        ("network_id", INT()),
        ("name", VARCHAR(64, bin_collation=True)),
        ("git_url", VARCHAR(255, bin_collation=True)),
        ("default_branch", VARCHAR(64, default='main', bin_collation=True)),
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
# 32. m_git_ref (table_id=32): Named Git Reference (Branch, Tag, Release)
# -----------------------------------------------------------------------------
m_git_ref = Table(
    table_id=32,
    table_name="m_git_ref",
    columns=(
        ("ref_id", INT(auto=True)),
        ("repo_id", INT()),
        ("ref_type", TINYINT(unsigned=True)),
        ("ref_name", VARCHAR(255, bin_collation=True)),
        ("commit_id", INT()),
        ("tagger_id", INT(null=True)),
        ("tagger_date", INT(unsigned=True, null=True)),
        ("tag_message", TEXT(null=True)),
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
# 33. m_commit_parent (table_id=33): Full Git Commit Topology (First & Merge Parents)
# -----------------------------------------------------------------------------
m_commit_parent = Table(
    table_id=33,
    table_name="m_commit_parent",
    columns=(
        ("commit_id", INT()),
        ("parent_commit_id", INT()),
        ("parent_idx", SMALLINT(unsigned=True)),
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
# 34. m_ast_attribute (table_id=34): AST Compiler & Kernel Attributes
# -----------------------------------------------------------------------------
m_ast_attribute = Table(
    table_id=34,
    table_name="m_ast_attribute",
    columns=(
        ("ast_id", INT()),
        ("priority", SMALLINT(unsigned=True)),
        ("name", VARCHAR(64, bin_collation=True)),
        ("val", VARCHAR(255, null=None, default='', bin_collation=True)),
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
# 35. m_ingest_state (table_id=35): Atomic Ingestion Progress & Crash Recovery
# -----------------------------------------------------------------------------
m_ingest_state = Table(
    table_id=35,
    table_name="m_ingest_state",
    columns=(
        ("network_id", INT()),
        ("commit_id", INT()),
        ("status", TINYINT(unsigned=True)),
        ("updated_at", BIGINT()),
        ("base_commit_id", INT(default=0)),
        ("worker_id", VARCHAR(128, default='', bin_collation=True)),
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
# 36. m_tag_def (table_id=36): Symbol Definitions of a Tag, Relative to the Tag's First Line
#     Written once, when the tag is new; every blob placing the tag (m_bridge_tag) shares them. ast_id is not
#     stored: it is resolved per blob through m_blob_symbol (sym_key), like a reference's target.
# -----------------------------------------------------------------------------
m_tag_def = Table(
    table_id=36,
    table_name="m_tag_def",
    columns=(
        ("tag_id", INT()),
        ("def_idx", SMALLINT(unsigned=True)),
        ("sym_key", BIGINT()),
        ("name", VARCHAR(255, bin_collation=True)),
        ("type_id", TINYINT(unsigned=True)),
        ("linkage", TINYINT(unsigned=True, default=0)),
        ("rel_line_s", MEDIUMINT()),
        ("rel_line_e", MEDIUMINT()),
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
# 37. m_tag_ref (table_id=37): Symbol References of a Tag, Relative to the Tag's First Line
#     Written once, when the tag is new. sym_key identifies the target without resolving it (core/SymbolRows.py);
#     m_blob_symbol resolves it per blob, since a target's AST id changes whenever its definition does.
# -----------------------------------------------------------------------------
m_tag_ref = Table(
    table_id=37,
    table_name="m_tag_ref",
    columns=(
        ("tag_id", INT()),
        ("ref_idx", MEDIUMINT(unsigned=True)),
        ("sym_key", BIGINT()),
        ("role", TINYINT(unsigned=True)),
        ("rel_line", MEDIUMINT()),
        ("char_s", MEDIUMINT()),
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
# 38. m_blob_symbol (table_id=38): Per-Blob Resolution of Symbol Keys
#     One row per distinct sym_key the blob's shared tag rows use: the AST id it resolves to here. The target's
#     file, set for 0.02% of the keys (declarations), is in m_blob_symbol_file.
# -----------------------------------------------------------------------------
m_blob_symbol = Table(
    table_id=38,
    table_name="m_blob_symbol",
    columns=(
        ("blob_id", INT()),
        ("sym_key", BIGINT()),
        ("target_ast_id", INT()),
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
# 39. m_symbol_own (table_id=39): Tag Placements Whose Symbols Are Stored Per Blob
#     kind 1 = references, 2 = definitions: the views skip that tag's shared rows in that blob and the
#     m_blob_symbol_ref / m_blob_symbol_def rows stand in for them.
# -----------------------------------------------------------------------------
m_symbol_own = Table(
    table_id=39,
    table_name="m_symbol_own",
    columns=(
        ("blob_id", INT()),
        ("tag_id", INT()),
        ("kind", TINYINT(unsigned=True)),
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
# 40. m_ingest_watermark (table_id=40): Highest Id of Each Id-Allocating Table When a Version Run Started
#     Rows above these marks were created by that run; core/ingest_state.py rolls them back when the
#     run was interrupted (m_ingest_state still INGEST_PARSING). Removed once the version is done.
#     No foreign keys: the bookkeeping must survive the rollback it drives.
# -----------------------------------------------------------------------------
m_ingest_watermark = Table(
    table_id=40,
    table_name="m_ingest_watermark",
    columns=(
        ("commit_id", INT()),
        # Not "table_id": Table binds every column name as an attribute of the instance
        ("marked_table_id", SMALLINT(unsigned=True)),
        ("max_id", BIGINT()),
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
# 41. m_ingest_blob (table_id=41): Pre-Existing Blobs a Version Run Parses
#     Blobs created by earlier runs without being parsed (intermediate commits): their derived rows
#     are cleared too when the run is rolled back. Removed once the version is done.
# -----------------------------------------------------------------------------
m_ingest_blob = Table(
    table_id=41,
    table_name="m_ingest_blob",
    columns=(
        ("commit_id", INT()),
        ("blob_id", INT()),
    ),
    primary=("commit_id", "blob_id"),
    foreign=(),
    indexes=(("ingest_blob_blob_idx", ("blob_id",)),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 42. m_arch_root (table_id=42): Build Layout of Each ARCH at a Version
#     What `make ARCH=<arch>` uses at the version's head commit, evaluated from its Makefiles by
#     parser/arch_layout.py. The layout itself is stored once (m_arch_layout / m_arch_layout_entry):
#     consecutive versions nearly always share it (11,075 (commit, ARCH) pairs held 106 distinct layouts).
# -----------------------------------------------------------------------------
m_arch_root = Table(
    table_id=42,
    table_name="m_arch_root",
    columns=(
        ("commit_id", INT()),
        ("arch", VARCHAR(32, bin_collation=True)),
        ("layout_id", INT()),
    ),
    primary=("commit_id", "arch"),
    # layout_id names an m_arch_layout row; no foreign key: m_arch_layout (45) comes after this table, and every
    # foreign key points to an earlier table. A rollback removes the version's rows by commit_id.
    foreign=(("commit_id", "m_commit", "commit_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 43. m_commit_email (table_id=43): Git Commit to Mailing List Message Linkage
# -----------------------------------------------------------------------------
m_commit_email = Table(
    table_id=43,
    table_name="m_commit_email",
    columns=(
        ("commit_id", INT()),
        ("message_id", VARCHAR(255, bin_collation=True)),
        ("thread_id", BIGINT(unsigned=True, default=0)),
        ("relation_type", TINYINT(unsigned=True)),
        ("match_tier", TINYINT(unsigned=True)),
        ("patch_version", TINYINT(unsigned=True, default=1)),
        ("patch_idx", SMALLINT(unsigned=True, default=0)),
        ("patch_total", SMALLINT(unsigned=True, default=0)),
    ),
    primary=("commit_id", "message_id", "relation_type"),
    foreign=(("commit_id", "m_commit", "commit_id"),),
    # Mail -> commits lookups (webapp "merged as", webapp/API.md section 4.8): the primary key starts
    # with commit_id, so without these a message or thread lookup scans the table. Existing databases
    # get them at parser start (main.ensure_declared_indexes()).
    indexes=(("commit_email_mid_idx", ("message_id",)), ("commit_email_tid_idx", ("thread_id",))),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 44. m_blob_symbol_file (table_id=44): File of a Symbol Key's Target, Per Blob
#     The m_blob_symbol rows whose target is known to live in a file (declarations: the file declaring a
#     function prototype or a struct/union/enum); every other key has none. Kept apart because 99.98% of
#     the keys have no file: the views read it with a LEFT JOIN (0 when absent).
# -----------------------------------------------------------------------------
m_blob_symbol_file = Table(
    table_id=44,
    table_name="m_blob_symbol_file",
    columns=(
        ("blob_id", INT()),
        ("sym_key", BIGINT()),
        ("target_fnid", INT()),
    ),
    primary=("blob_id", "sym_key"),
    foreign=(
        ("blob_id", "m_file_blob", "blob_id"),
        ("target_fnid", "m_file_name", "fnid"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 45. m_arch_layout (table_id=45): One Distinct ARCH Build Layout
#     layout_hash: first 16 bytes of the SHA-256 of the layout's rows (parser/arch_layout.py layout_digest()).
# -----------------------------------------------------------------------------
m_arch_layout = Table(
    table_id=45,
    table_name="m_arch_layout",
    columns=(
        ("layout_id", INT(auto=True)),
        ("layout_hash", BIN(16)),
    ),
    primary=("layout_id",),
    foreign=None,
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    unique_keys=(("layout_hash",),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 46. m_arch_layout_entry (table_id=46): Rows of an ARCH Build Layout
#     role 1 source directory, 2 include directory (priority = -I order), 3 symlink the build creates
#     (path -> target), 4 generated asm-generic wrapper (path -> the header it includes), 5 forced -include
#     file. Directories end with "/".
# -----------------------------------------------------------------------------
m_arch_layout_entry = Table(
    table_id=46,
    table_name="m_arch_layout_entry",
    columns=(
        ("layout_id", INT()),
        ("role", TINYINT(unsigned=True)),
        ("priority", SMALLINT(unsigned=True)),
        ("path", VARCHAR(255, bin_collation=True)),
        ("target", VARCHAR(255, null=None, default='', bin_collation=True)),
    ),
    primary=("layout_id", "role", "path"),
    foreign=(("layout_id", "m_arch_layout", "layout_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 47. m_checkpoint_base (table_id=47): Checkpoints Stored as a Delta of Another One
#     A version's m_bridge_checkpoint rows are its full tree until the next version is ingested; then, when
#     it differs from an earlier full checkpoint (base_commit_id) in few files, only the difference is kept
#     (core/checkpoints.py): its changed and added files, plus blob_id 0 for the base's files it lacks. The
#     m_checkpoint_tree view returns every version's full tree (base rows the delta does not replace).
# -----------------------------------------------------------------------------
m_checkpoint_base = Table(
    table_id=47,
    table_name="m_checkpoint_base",
    columns=(
        ("commit_id", INT()),
        ("base_commit_id", INT()),
        ("delta_rows", INT(unsigned=True)),
    ),
    primary=("commit_id",),
    foreign=(
        ("commit_id", "m_commit", "commit_id"),
        ("base_commit_id", "m_commit", "commit_id"),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 48. m_blob_facts (table_id=48): Per-Blob Facts of the Source Files
#     One row per (Git blob SHA, family), keyed by the SHA so any revision's tree (git ls-tree) finds its blobs
#     without the checkpoint; each family has its own format. Written by Step 6.9 (parser/facts_ingest.py):
#       family 1: parser/source_facts.py extract() of a .c/.h blob (functions, calls, ops tables; zlib JSON),
#                 assembled by the webapp's source index (call paths, ops implementations);
#       family 2: parser/kapi_facts.py extract() of a .c/.h/.S blob (Kernel API macros; empty data for a blob
#                 without any, else zlib JSON), assembled into the webapp's Kernel API catalogs.
#       family 3: parser/include_facts.py extract() of a .c/.h/.S blob ([[name, quoted]] #include directives),
#                 read by the webapp's include walks and its included .c/.S list;
#       family 4: parser/kconfig_ast/kconfig_definitions.py extract() of a Kconfig blob ({symbol: [facts]}, help
#                 texts included), read by the webapp's Kconfig history and Compare;
#       family 5: parser/doc_labels.py extract() of a Documentation/**/*.rst blob ([[label, line, title]]),
#                 read by the webapp's documentation label index;
#       family 6: parser/man_ast/man_facts.py extract() of a man page blob (man/manS/name.S or manS/name.S:
#                 NAME names and summary, .TH header, .so target, cross references, SYNOPSIS functions, headers
#                 and identifiers; zlib JSON), read by the webapp's man page index.
#     Every family stores empty data for a blob without anything to record. --backfill-facts fills them for
#     versions ingested earlier (core/backfills.py).
# -----------------------------------------------------------------------------
m_blob_facts = Table(
    table_id=48,
    table_name="m_blob_facts",
    columns=(
        ("blob_hash", BIN(20)),
        ("family", TINYINT(unsigned=True)),
        ("format", SMALLINT(unsigned=True)),
        ("data", MEDIUMBLOB()),
    ),
    primary=("blob_hash", "family"),
    foreign=None,
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 49. m_cve_source (table_id=49): The CVE List the Parser Ingested
#     One row per CVE repository (config cve.repo_dir): its layout ("cvelist" for the CVE Program's
#     cvelistV5, "vulns" for the kernel CNA's vulns.git), the HEAD ingested and when (parser/cve/).
# -----------------------------------------------------------------------------
m_cve_source = Table(
    table_id=49,
    table_name="m_cve_source",
    columns=(
        ("name", VARCHAR(64, bin_collation=True)),
        ("layout", VARCHAR(16, bin_collation=True)),
        ("head", BIN(20)),
        ("format", SMALLINT(unsigned=True)),
        ("records", INT(unsigned=True)),
        ("built_at", BIGINT()),
    ),
    primary=("name",),
    foreign=None,
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 50. m_cve (table_id=50): A CVE Record About the Linux Kernel
#     kind 1 = kernel CNA record (git ranges and versions), 2 = other CNA naming a kernel fix commit,
#     3 = description only. score = CVSS base score x 10 (0: none). data = the parsed record
#     (parser/cve/cve_records.py parse_record(), zlib-compressed JSON).
# -----------------------------------------------------------------------------
m_cve = Table(
    table_id=50,
    table_name="m_cve",
    columns=(
        ("cve_id", VARCHAR(24, bin_collation=True)),
        ("kind", TINYINT(unsigned=True)),
        ("published", INT(unsigned=True)),
        ("updated", INT(unsigned=True)),
        ("score", SMALLINT(unsigned=True)),
        ("kev", TINYINT(unsigned=True)),
        ("record_path", VARCHAR(255, bin_collation=True)),
        ("title", VARCHAR(512, bin_collation=True)),
        ("data", MEDIUMBLOB()),
    ),
    primary=("cve_id",),
    foreign=None,
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    indexes=(("cve_path_idx", ("record_path",)),),
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 51. m_cve_commit (table_id=51): Kernel Commits a CVE Names
#     role 1 = fixes the bug, 2 = introduced it. commit_hash is the lowercase hex the record gives (12 to 40
#     characters: CVE references abbreviate), so a commit is looked up by its full SHA and its prefixes.
# -----------------------------------------------------------------------------
m_cve_commit = Table(
    table_id=51,
    table_name="m_cve_commit",
    columns=(
        ("commit_hash", VARCHAR(40, bin_collation=True)),
        ("role", TINYINT(unsigned=True)),
        ("cve_id", VARCHAR(24, bin_collation=True)),
    ),
    primary=("commit_hash", "role", "cve_id"),
    foreign=(("cve_id", "m_cve", "cve_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 52. m_release_segment (table_id=52): A Release Tag and the History Range Ending at It
#     Written by the release statistics step (parser/git_ast/release_stats.py, core/API.md section 10)
#     for every release tag of the repository, ingested or not. chain 1 = mainline (releases and
#     release candidates), 2 = stable updates. base_tag..tag is the segment: the previous tag of the
#     same chain, so a chain's segments are disjoint and add up to any range of it; commits / merges
#     count it. diff_base_tag..tag is the range a release report covers (a release: its whole cycle
#     from the previous release; otherwise the segment), and files_changed / lines_added /
#     lines_removed are its `git diff --shortstat`. Tags are '' for none (the first tag of a chain).
# -----------------------------------------------------------------------------
m_release_segment = Table(
    table_id=52,
    table_name="m_release_segment",
    columns=(
        ("segment_id", INT(auto=True)),
        ("repo_id", INT()),
        ("tag", VARCHAR(64, bin_collation=True)),
        ("tag_hash", BIN(20)),
        ("chain", TINYINT(unsigned=True)),
        ("base_tag", VARCHAR(64, bin_collation=True)),
        ("base_hash", BIN(20)),
        ("commits", INT(unsigned=True)),
        ("merges", INT(unsigned=True)),
        ("diff_base_tag", VARCHAR(64, bin_collation=True)),
        ("files_changed", INT(unsigned=True)),
        ("lines_added", INT(unsigned=True)),
        ("lines_removed", INT(unsigned=True)),
        ("format", SMALLINT(unsigned=True)),
    ),
    primary=("segment_id",),
    foreign=(("repo_id", "m_repository", "repo_id"),),
    unique_keys=(("repo_id", "tag"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 53. m_release_person (table_id=53): Contributors of a Release Segment
#     Distinct non-merge commits of the segment per (person, role): role_type is the CommitRole of
#     m_bridge_commit_person (1 author, 3-11 trailers; parser/git_ast/GIT_API.md section 3). Within one
#     commit and role an identity counts once (aliases of one developer are not counted twice), and the
#     author's own Signed-off-by is left out.
# -----------------------------------------------------------------------------
m_release_person = Table(
    table_id=53,
    table_name="m_release_person",
    columns=(
        ("segment_id", INT()),
        ("person_id", INT()),
        ("role_type", TINYINT(unsigned=True)),
        ("commits", INT(unsigned=True)),
    ),
    primary=("segment_id", "person_id", "role_type"),
    foreign=(("segment_id", "m_release_segment", "segment_id"), ("person_id", "m_maintainer_person", "person_id")),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 54. m_person_first_release (table_id=54): The First Mainline Release a Person Authored In
#     The mainline segment (m_release_segment, chain 1) holding the person's first authored commit, in
#     release order (not by author date: patches merged long after they were written carry old dates).
#     A release's first-time authors are the identities whose aliases' earliest segment is in its range.
# -----------------------------------------------------------------------------
m_person_first_release = Table(
    table_id=54,
    table_name="m_person_first_release",
    columns=(
        ("repo_id", INT()),
        ("person_id", INT()),
        ("segment_id", INT()),
    ),
    primary=("repo_id", "person_id"),
    foreign=(("repo_id", "m_repository", "repo_id"), ("person_id", "m_maintainer_person", "person_id"),
             ("segment_id", "m_release_segment", "segment_id")),
    indexes=(("person_first_segment_idx", ("segment_id",)),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 55. m_table_sequence (table_id=55): Atomic Table ID Allocation Sequences
#     Stores the next primary key value to allocate for seq_table_id across distributed parser nodes.
# -----------------------------------------------------------------------------
m_table_sequence = Table(
    table_id=55,
    table_name="m_table_sequence",
    columns=(
        ("seq_table_id", SMALLINT(unsigned=True)),
        ("next_id", BIGINT(unsigned=True)),
    ),
    primary=("seq_table_id",),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 56. m_ingest_id_range (table_id=56): Leased ID Ranges per Ingest Run
#     Records the [start_id, end_id] primary key blocks leased by a version run (commit_id)
#     for each marked table. Enables surgical, isolated rollback of an interrupted run.
# -----------------------------------------------------------------------------
m_ingest_id_range = Table(
    table_id=56,
    table_name="m_ingest_id_range",
    columns=(
        ("commit_id", INT()),
        ("marked_table_id", SMALLINT(unsigned=True)),
        ("start_id", BIGINT(unsigned=True)),
        ("end_id", BIGINT(unsigned=True)),
    ),
    primary=("commit_id", "marked_table_id", "start_id"),
    foreign=(("commit_id", "m_commit", "commit_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 57. m_parse_node (table_id=57): Active Parser Node Registry & Exclusivity
#     Tracks registered parser instances (master and workers), active commit, and heartbeat.
# -----------------------------------------------------------------------------
m_parse_node = Table(
    table_id=57,
    table_name="m_parse_node",
    columns=(
        ("node_id", VARCHAR(128, bin_collation=True)),
        ("network_id", INT(default=1)),
        ("role", VARCHAR(32, bin_collation=True)),
        ("hostname", VARCHAR(255, bin_collation=True)),
        ("pid", INT(unsigned=True)),
        ("active_commit_id", INT(default=0)),
        ("heartbeat_at", BIGINT(unsigned=True)),
    ),
    primary=("node_id",),
    foreign=(("network_id", "m_git_network", "network_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 58. m_parse_task (table_id=58): Work Queue & Instruction Leasing for Secondary Parsers
#     Instructions populated by Master on what past or stable/branched tags can be processed.
# -----------------------------------------------------------------------------
m_parse_task = Table(
    table_id=58,
    table_name="m_parse_task",
    columns=(
        ("task_id", INT(auto=True)),
        ("network_id", INT(default=1)),
        ("chain_name", VARCHAR(128, bin_collation=True)),
        ("chain_kind", VARCHAR(32, bin_collation=True)),
        ("seq_order", INT(unsigned=True, default=0)),
        ("ref_name", VARCHAR(255, bin_collation=True)),
        ("commit_id", INT(default=0)),
        ("base_ref_name", VARCHAR(255, default='', bin_collation=True)),
        ("base_commit_id", INT(default=0)),
        ("status", TINYINT(unsigned=True, default=0)),
        ("worker_id", VARCHAR(128, default='', bin_collation=True)),
        ("lease_expires_at", BIGINT(unsigned=True, default=0)),
        ("retry_count", SMALLINT(unsigned=True, default=0)),
        ("error_msg", TEXT(null=None, default='', bin_collation=True)),
    ),
    primary=("task_id",),
    foreign=(("network_id", "m_git_network", "network_id"),),
    unique_keys=(("network_id", "ref_name"),),
    indexes=(
        ("parse_task_status_idx", ("network_id", "status", "chain_name", "seq_order")),
        ("parse_task_base_idx", ("base_commit_id", "status")),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 59. m_person_identity (table_id=59): Developer Identity of Each Person Alias
#     m_maintainer_person holds one row per (name, email) alias; the identity key groups the aliases of one
#     developer (parser/git_ast/identity.py: build_identity_index), stored by refresh_person_identities()
#     after each version, after release statistics and at start. The webapp reads it instead of regrouping
#     the whole registry.
# -----------------------------------------------------------------------------
m_person_identity = Table(
    table_id=59,
    table_name="m_person_identity",
    columns=(
        ("person_id", INT()),
        ("identity_key", VARCHAR(255, bin_collation=True)),
    ),
    primary=("person_id",),
    foreign=(("person_id", "m_maintainer_person", "person_id"),),
    indexes=(("person_identity_key_idx", ("identity_key",)),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 60. m_commit_release (table_id=60): First Release Segment of Every Commit, per Chain
#     Written by release statistics (parser/git_ast/release_stats.py) with each segment: every commit of
#     the segment, the earliest segment of a chain winning (segments are written in release order). Keyed
#     by hash: segments cover release tags never ingested, whose commits may have no m_commit row.
#     Replaces the webapp's SQLite release index (webapp/API.md section 2.17).
# -----------------------------------------------------------------------------
m_commit_release = Table(
    table_id=60,
    table_name="m_commit_release",
    columns=(
        ("repo_id", INT()),
        ("commit_hash", BIN(20)),
        ("chain", TINYINT(unsigned=True)),
        ("segment_id", INT()),
    ),
    primary=("repo_id", "commit_hash", "chain"),
    foreign=(
        ("repo_id", "m_repository", "repo_id"),
        ("segment_id", "m_release_segment", "segment_id"),
    ),
    indexes=(("commit_release_seg_idx", ("segment_id",)),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 61. m_kbuild_symbol (table_id=61): Kbuild Rules of a Makefile With the Symbols Governing Them
#     Rows of one Makefile/Kbuild (blob, path) pair (parser/kbuild_symbols.py): per binding of
#     KbuildParser one row for the symbol of its rule (via 1) and one per CONFIG_ symbol of its enclosing
#     ifeq/ifdef blocks (via 2). Written by Step 6.6 for the pairs new in a version (replacing the Kbuild /
#     Makefile rows m_file_reference held) and by --backfill-kbuild; read by the webapp's Kconfig usage.
# -----------------------------------------------------------------------------
m_kbuild_symbol = Table(
    table_id=61,
    table_name="m_kbuild_symbol",
    columns=(
        ("source_blob_id", INT()),
        ("source_fnid", INT()),
        ("idx", SMALLINT(unsigned=True)),
        ("symbol", VARCHAR(64, bin_collation=True)),
        ("via", TINYINT(unsigned=True)),
        ("line_no", MEDIUMINT()),
        ("compile_mode", TINYINT(unsigned=True)),
        ("kind", TINYINT(unsigned=True)),
        ("target_obj", VARCHAR(128, bin_collation=True)),
        ("target_path", VARCHAR(255, bin_collation=True)),
        ("target_fnid", INT(default=0)),
        ("rule", VARCHAR(255, bin_collation=True)),
    ),
    primary=("source_blob_id", "source_fnid", "idx", "symbol", "via"),
    foreign=(
        ("source_blob_id", "m_file_blob", "blob_id"),
        ("source_fnid", "m_file_name", "fnid"),
        ("target_fnid", "m_file_name", "fnid"),
    ),
    indexes=(
        ("kbuild_sym_idx", ("symbol",)),
        ("kbuild_target_idx", ("target_fnid",)),
    ),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 62. m_cve_commit_hash (table_id=62): Abbreviated CVE Commit Hashes Resolved in a Repository
#     m_cve_commit keeps the 12-40 hex digits a CVE record gives; the parser resolves the shorter ones
#     against the kernel repository after each CVE ingestion (parser/cve/cve_ingest.py), so the webapp's
#     CVE reports run no `git rev-parse` per hash. A prefix naming no commit or several gets no row and is
#     tried again at the next ingestion.
# -----------------------------------------------------------------------------
m_cve_commit_hash = Table(
    table_id=62,
    table_name="m_cve_commit_hash",
    columns=(
        ("repo_id", INT()),
        ("prefix", VARCHAR(40, bin_collation=True)),
        ("commit_hash", BIN(20)),
    ),
    primary=("repo_id", "prefix"),
    foreign=(("repo_id", "m_repository", "repo_id"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 63. m_commit_relation (table_id=63): What a Commit Message Says About Other Commits
#     Written at Step 6.5 for the commits a run inserts, and by --backfill-commit-relations for older ones
#     (parser/git_ast/commit_relations.py). kind: 1 Fixes: (note = quoted subject), 2 reverts, 3 upstream
#     (this commit is a backport of the target), 4 Cc: stable (no target; note = annotation). target_hash is
#     the full SHA, zero when the written prefix (target_prefix) names no commit or several. Reverse lists
#     (fixed by, reverted by, backports) read commit_rel_target_idx. Row (commit_id 0, kind 0, idx 0) holds
#     the backfill state in note ("next:<id>" while it runs, "done:<id>" once every older commit is covered).
# -----------------------------------------------------------------------------
m_commit_relation = Table(
    table_id=63,
    table_name="m_commit_relation",
    columns=(
        ("commit_id", INT()),
        ("kind", TINYINT(unsigned=True)),
        ("idx", SMALLINT(unsigned=True)),
        ("target_hash", BIN(20)),
        ("target_prefix", VARCHAR(40, bin_collation=True)),
        ("note", VARCHAR(255, default='')),
    ),
    primary=("commit_id", "kind", "idx"),
    foreign=(("commit_id", "m_commit", "commit_id"),),
    indexes=(("commit_rel_target_idx", ("target_hash", "kind", "target_prefix")),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 64. m_commit_link (table_id=64): Message-Ids a Commit Message Names (Link:, Message-Id:)
#     Normalized like normalize_mid(): unbracketed, URL-decoded, lower-case. Kept apart from m_commit_email,
#     which holds the archive-resolved links (with thread ids) of the mailing-list feature.
# -----------------------------------------------------------------------------
m_commit_link = Table(
    table_id=64,
    table_name="m_commit_link",
    columns=(
        ("commit_id", INT()),
        ("message_id", VARCHAR(255, bin_collation=True)),
    ),
    primary=("commit_id", "message_id"),
    foreign=(("commit_id", "m_commit", "commit_id"),),
    indexes=(("commit_link_mid_idx", ("message_id",)),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    idempotent_pk=True,
    hashing_table=False,
)

# -----------------------------------------------------------------------------
# 65. m_cve_file (table_id=65): Linux Kernel Source Files Affected by a CVE
#     Affected source and header file paths extracted from CVE records or associated fix commits.
# -----------------------------------------------------------------------------
m_cve_file = Table(
    table_id=65,
    table_name="m_cve_file",
    columns=(
        ("cve_id", VARCHAR(24, bin_collation=True)),
        ("file_path", VARCHAR(255, bin_collation=True)),
    ),
    primary=("cve_id", "file_path"),
    foreign=(("cve_id", "m_cve", "cve_id"),),
    indexes=(("cve_file_path_idx", ("file_path",)),),
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
    m_tag,                 # 10
    m_bridge_tag,          # 11
    m_map_ast,             # 12
    m_bridge_map,          # 13
    m_ast_hash,            # 14
    m_maintainer_person,   # 15
    m_maintainer_section,  # 16
    m_maintainer_member,   # 17
    m_maintainer_pattern,  # 18
    m_maintainer_file,     # 19
    m_credits_entry,       # 20
    m_commit,              # 21
    m_bridge_commit_person,# 22
    m_commit_file,         # 23
    m_bridge_commit_tag,   # 24
    m_moved_tag,           # 25
    m_blob_symbol_def,     # 26
    m_blob_symbol_ref,     # 27
    m_file_reference,      # 28
    m_db_instance,         # 29
    m_git_network,         # 30
    m_repository,          # 31
    m_git_ref,             # 32
    m_commit_parent,       # 33
    m_ast_attribute,       # 34
    m_ingest_state,        # 35
    m_tag_def,             # 36
    m_tag_ref,             # 37
    m_blob_symbol,         # 38
    m_symbol_own,          # 39
    m_ingest_watermark,    # 40
    m_ingest_blob,         # 41
    m_arch_root,           # 42
    m_commit_email,        # 43
    m_blob_symbol_file,    # 44
    m_arch_layout,         # 45
    m_arch_layout_entry,   # 46
    m_checkpoint_base,     # 47
    m_blob_facts,          # 48
    m_cve_source,          # 49
    m_cve,                 # 50
    m_cve_commit,          # 51
    m_release_segment,     # 52
    m_release_person,      # 53
    m_person_first_release,# 54
    m_table_sequence,      # 55
    m_ingest_id_range,     # 56
    m_parse_node,          # 57
    m_parse_task,          # 58
    m_person_identity,     # 59
    m_commit_release,      # 60
    m_kbuild_symbol,       # 61
    m_cve_commit_hash,     # 62
    m_commit_relation,     # 63
    m_commit_link,         # 64
    m_cve_file,            # 65
)

Table_Array = TABLES


def init_db_layout(gp=None) -> tuple[Table, ...]:
    """Initialize and populate gp.Table_Array with all 66 schema tables.

    Args:
        gp: Optional GreatProcessor instance to attach Table_Array to.

    Returns:
        Immutable tuple of all 66 Table schema objects.
    """
    if gp is not None:
        gp.Table_Array = list(TABLES)
    return TABLES
