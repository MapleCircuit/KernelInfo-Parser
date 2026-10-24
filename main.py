"""main.py - Core Orchestration Loop, Table Definitions & Processing Workflows.

===============================================================================
SYSTEM ORCHESTRATION & PIPELINE ARCHITECTURAL GUIDE
===============================================================================
This module serves as the primary entry point and orchestrator for the parser.
It defines the database table schema array (`gp.Table_Array`), manages git version
lifecycles, coordinates multiprocessing parsing workers, and resolves queued
`ChangeSet` operations into database transactions.

1. DATABASE SCHEMA INITIALIZATION:
-------------------------------------------------------------------------------
  `gp.Table_Array` registers 15 core relational tables defining the schema:
    - Version & File tracking: `m_v_main`, `m_file_name`, `m_file`, `m_bridge_file`, `m_moved_file`
    - AST Schema: `m_type_descriptor`, `m_ast`, `m_ast_container`, `m_ast_include`, `m_ast_debug`, `m_ast_hash`
    - Version Tags & Spatial Coordinates: `m_tag`, `m_bridge_tag`, `m_map_ast`, `m_bridge_map`

2. VERSION PROCESSING PIPELINE (`update(version)`):
-------------------------------------------------------------------------------
  Step 1: Version Registration (`create_new_vid`)
          Registers new release tag in `m_v_main`.
  Step 2: Database Index Optimization (`create_index`)
          Creates temporary B-tree indexes (`ast_index`, `file_name_index`) for fast lookups.
  Step 3: Multiprocessing AST Parsing (`trigger_multicore`)
          Distributes `gp.Change_List` across `G.CPUS - 1` parallel worker processes.
          Each worker invokes `file_processing()`, runs `default_processing()` and `CS.parse()`,
          and returns picklable `ChangeSet` objects.
  Step 4: Unchanged File & Directory Propagation (`processing_unchanges`, `processing_dirs`)
          Propagates file and directory references from `gp.Old_VID` to `gp.VID` for
          unmodified files and directories.
  Step 5: ChangeSet Resolution Loop (`cs_queue.get()`)
          Iterates over queued `ChangeSet` instances in serial, calling `CS.execute()`.
          Unresolved references raise `REF_NOT_RESOLVABLE` and are requeued until satisfied.
  Step 6: Transaction Commit & Reset (`G.TE.commit`)
          Removes temporary indexes, commits database transactions, and purges RAMDISK.

3. DIFF OPERATION ROUTING (`default_processing(CS)`):
-------------------------------------------------------------------------------
  - `"A"` (Added): Inserts new `m_file_name`, `m_file` (vid_s=VID, s_stat='A'), and `m_bridge_file`.
  - `"M"` (Modified): Updates prior `m_file` (vid_e=Old_VID, e_stat='M') and creates new `m_file`.
  - `"R"` (Renamed): Updates prior `m_file` (e_stat='R'), creates new `m_file`, and inserts `m_moved_file`.
  - `"D"` (Deleted): Updates prior `m_file` (vid_e=Old_VID, e_stat='D').
===============================================================================
"""
from core.globalstuff import (
    G,
    COLOR,
    type_check,
    REF_ROOT,
    REF_OLD,
    REF_NOT_RESOLVABLE,
    CONTINUE_EXCEPTION,
    T_DIR,
    T_RAW,
    T_MAINTAINERS,
    T_CREDITS,
    ASTT,
    configure_logging,
    setup_memory_limit,
    FileRefType,
    reclaim_system_memory,
    freeze_system_memory,
    unfreeze_system_memory,
)
from core.quit_listener import GracefulQuitListener
from core.config import (
    init_config,
    get_parser_config,
    get_db_config,
    get_ssh_tunnel_config,
    get_workspace_config,
    get_repository,
    get_default_repo_path,
    resolve_repo_path,
    sync_repositories,
)
from core.ssh_tunnel import start_ssh_tunnel, stop_ssh_tunnel
from core.Profiler import parse_duration, PipelineStageProfiler, format_profiling_report
import os
import sys
import subprocess as sp

# Raise recursion limit for parsing deeply nested ASTs in kernel source files
sys.setrecursionlimit(50000)
import time
import secrets
import logging
import argparse
from typing import Any
import multiprocessing
import queue
import threading
import re
from collections import deque, defaultdict
import pickle
import zlib
import traceback
import gc
import ctypes
from parser.c_ast import c_ast_parse, Line
from core.FileHandler import MasterFile
from core.GreatProcessor import GreatProcessor
from core.TableHandling import Table, ChangeSet
from db_engine import MariaDB, MockDB, get_db_engine
from table_engine import TECachedDB, TEDirectDB, get_table_engine
from core.DBLayout import (
    init_db_layout,
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
    m_commit_file,
    m_commit_parent,
    m_bridge_checkpoint,
    m_git_ref,
    m_bridge_commit_person,
    m_bridge_commit_file,
    m_bridge_commit_tag,
    m_tag_code,
    m_moved_tag,
    m_file_reference,
    m_db_instance,
    m_project,
    m_git_network,
    m_repository,
    m_file_blob,
)


G.DB = MariaDB
G.TE = TECachedDB()
MF = MasterFile()
G.MF = MF
gp = GreatProcessor()
configure_logging(level=logging.INFO, fmt="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

init_db_layout(gp)

T_NO_FOREIGN = frozenset({T_RAW, T_DIR, T_MAINTAINERS, T_CREDITS})




def ensure_db_instance(db: Any) -> None:
    """Ensure that the database instance fingerprint table m_db_instance has at least 1 seed row."""
    try:
        empty_filter = (None,) * m_db_instance.length
        res = db.select(m_db_instance, empty_filter)
        if not res:
            logger.info("m_db_instance is empty; seeding initial database instance fingerprint...")
            init_row = ((secrets.token_hex(32), int(time.time())),)
            db.insert(m_db_instance, init_row)
    except Exception as e:
        logger.warning(f"ensure_db_instance check encountered error: {e}")


def ensure_db_views(db: Any) -> None:
    """Create or replace SQL compatibility views for legacy query interoperability."""
    views = (
        ("m_v_main", "CREATE OR REPLACE VIEW m_v_main AS SELECT commit_id AS vid, ref_name AS vname, repo_id, ref_type, tagger_id, tagger_date, tag_message FROM m_git_ref;"),
        ("m_bridge_file", "CREATE OR REPLACE VIEW m_bridge_file AS SELECT commit_id AS vid, fnid, blob_id AS fid FROM m_bridge_checkpoint;"),
        ("m_file", "CREATE OR REPLACE VIEW m_file AS SELECT fb.blob_id AS fid, COALESCE((SELECT MIN(bc.commit_id) FROM m_bridge_checkpoint bc WHERE bc.blob_id = fb.blob_id), 0) AS vid_s, 0 AS vid_e, fb.ftype, '0' AS s_stat, '0' AS e_stat, fb.blob_hash FROM m_file_blob fb;"),
        ("m_bridge_commit_file", "CREATE OR REPLACE VIEW m_bridge_commit_file AS SELECT commit_id, fnid, blob_id, 0 AS old_fnid, 0 AS old_blob_id, change_type FROM m_commit_file;"),
    )
    for vname, ddl in views:
        try:
            if hasattr(db, "cnx") and db.cnx:
                with db.cnx.cursor() as cur:
                    cur.execute(ddl)
                db.cnx.commit()
        except Exception as e:
            logger.debug(f"ensure_db_views skipping view {vname}: {e}")


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
    c_path = getattr(cs_obj, "current_path", None)
    if not c_path:
        return
    fid = get_fid_for_path(c_path)
    gp_ref = getattr(cs_obj, "gp", None) or getattr(G, "GP", None) or gp
    if hasattr(gp_ref, "completed_paths"):
        gp_ref.completed_paths.add(c_path)

    # 1. Preserve resolved symbol map before internal buffers are cleared
    if hasattr(cs_obj, "symbol_dict") and cs_obj.symbol_dict:
        resolved_symbols = {}
        cs_res = getattr(cs_obj, "cs_result", None)
        cs_ops = getattr(cs_obj, "cs", None)
        for (sym_name, sym_type), op_pos in cs_obj.symbol_dict.items():
            ast_id = None
            if cs_res and op_pos < len(cs_res) and cs_res[op_pos] is not None:
                res_row = cs_res[op_pos]
                if len(res_row) > 0 and isinstance(res_row[0], int):
                    ast_id = res_row[0]
            elif cs_ops and op_pos < len(cs_ops) and cs_ops[op_pos] is not None:
                op = cs_ops[op_pos]
                if len(op) >= 3 and len(op[2]) > 0 and isinstance(op[2][0], int):
                    ast_id = op[2][0]
            if ast_id is not None:
                resolved_symbols[(sym_name, sym_type)] = ast_id
        cs_obj.resolved_symbols = resolved_symbols

        # Register resolved symbols into GreatProcessor per-file symbol tables for O(1) lookups
        if gp_ref is not None:
            if hasattr(gp_ref, "file_symbols") and isinstance(gp_ref.file_symbols, dict):
                gp_ref.file_symbols[c_path] = resolved_symbols
            if hasattr(gp_ref, "file_names") and isinstance(gp_ref.file_names, dict):
                gp_ref.file_names[c_path] = {s_name: a_id for (s_name, _), a_id in resolved_symbols.items()}
            if hasattr(gp_ref, "_ast_staged_symbols") and isinstance(gp_ref._ast_staged_symbols, dict):
                gp_ref._ast_staged_symbols.update(resolved_symbols)
            if hasattr(gp_ref, "_ast_staged_names") and isinstance(gp_ref._ast_staged_names, dict):
                for (s_name, _s_type), a_id in resolved_symbols.items():
                    if s_name not in gp_ref._ast_staged_names:
                        gp_ref._ast_staged_names[s_name] = a_id
            if hasattr(gp_ref, "_ast_negative_cache") and gp_ref._ast_negative_cache:
                for sym_key in resolved_symbols:
                    gp_ref._ast_negative_cache.discard(sym_key)

    # 2. Extract bridge tags with resolved tag_id from cs_result
    if fid is not None and not hasattr(cs_obj, "pre_extracted_tags"):
        file_tags = []
        cs_ops = getattr(cs_obj, "cs", [])
        cs_res = getattr(cs_obj, "cs_result", [])
        for i, op in enumerate(cs_ops):
            if op and len(op) >= 3 and op[0] == m_bridge_tag.table_id:
                cols = op[2]
                if len(cols) >= 4:
                    tag_id = None
                    if i < len(cs_res) and cs_res[i] is not None and len(cs_res[i]) >= 2:
                        res_val = cs_res[i][1]
                        if isinstance(res_val, int):
                            tag_id = res_val
                    if tag_id is None and not isinstance(cols[1], tuple):
                        tag_id = cols[1]

                    line_s = cols[2] if isinstance(cols[2], int) else 1
                    line_e = cols[3] if isinstance(cols[3], int) else line_s
                    if tag_id is not None:
                        file_tags.append((tag_id, fid, line_s, line_e))
        cs_obj.pre_extracted_tags = file_tags

    blob_id = getattr(cs_obj, "blob_id", 0) or fid or 0
    if blob_id:
        cs_obj.blob_id = blob_id
        gp_ref = getattr(cs_obj, "gp", None) or getattr(G, "GP", None)
        if gp_ref is not None and hasattr(gp_ref, "file_blob_ids") and isinstance(gp_ref.file_blob_ids, dict):
            gp_ref.file_blob_ids[c_path] = blob_id

    if hasattr(cs_obj, "pre_extracted_tags") and cs_obj.pre_extracted_tags:
        gp_ref = getattr(cs_obj, "gp", None) or getattr(G, "GP", None)
        if gp_ref is not None and hasattr(gp_ref, "file_pre_extracted_tags") and isinstance(gp_ref.file_pre_extracted_tags, dict):
            gp_ref.file_pre_extracted_tags[c_path] = cs_obj.pre_extracted_tags

    # 3. Evacuate internal AST buffers and bloat
    if hasattr(cs_obj, "cs") and isinstance(cs_obj.cs, list):
        cs_obj.cs.clear()
    if hasattr(cs_obj, "store_dict") and isinstance(cs_obj.store_dict, dict):
        cs_obj.store_dict.clear()
    if hasattr(cs_obj, "cs_result") and isinstance(cs_obj.cs_result, list):
        cs_obj.cs_result.clear()
    if hasattr(cs_obj, "symbol_dict") and isinstance(cs_obj.symbol_dict, dict):
        cs_obj.symbol_dict.clear()
    if hasattr(cs_obj, "foreign_deps") and isinstance(cs_obj.foreign_deps, set):
        cs_obj.foreign_deps.clear()
    if hasattr(cs_obj, "unresolved_indices") and cs_obj.unresolved_indices:
        cs_obj.unresolved_indices.clear()
    if hasattr(cs_obj, "clear_bloat"):
        cs_obj.clear_bloat()
    else:
        cs_obj.gp = None
        cs_obj.mf = None
        cs_obj.parsers = {}
        cs_obj.batch_cs_dict = None
        cs_obj.file = None

    # Record worker micro-profiler into central StageProfiler if active
    if getattr(G, "STAGE_PROFILER", None) is not None and getattr(cs_obj, "profiler", None) is not None:
        G.STAGE_PROFILER.record_worker_profiler(cs_obj.profiler)
        cs_obj.profiler = None


def update(version: str, repo_path: str | None = None) -> None:
    """Execute the full version parsing and database ingestion pipeline for a target release version.
    
    Workflow Steps:
    1. Register/synchronize version in `m_git_ref` via `create_new_vid()`.
    2. Build temporary performance B-tree indexes (`ast_index`, `file_name_index`).
    3. Clone repository branch into RAMDISK via `MF.add_version()`.
    4. Generate git diff change list (`MF.generate_change_list()`) & extract Git blob hashes.
    4.5 Deduplicate files against `m_file_blob` across trees sharing root commit.
    5. Spawn parallel worker processes (`trigger_multicore()`) to parse file diffs into ChangeSets.
    6. Enqueue all generated `ChangeSet` objects into `cs_queue` and resolve operations sequentially.
    7. Remove temporary indexes, commit table transactions (`G.TE.commit()`), and reset state.
    """
    if repo_path:
        MF.set_repo_dir(repo_path)

    # -------------------------------------------------------------------------
    # STEP 1: Register or synchronize active ref/commit in m_git_ref
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 1: Git Ref Synchronization"):
        gp.init_cs_dict()
        if create_new_vid(version):
            logger.info(COLOR.yellow(f"=======================Version/Ref '{version}' already exists in DB. Skipping======================="))
            return

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Step 1. Halting '{version}' early."))
        return

    logger.info(COLOR.green(f"=======================Working on {version} (commit_id {gp.VID})======================="))

    # Ensure TableEngine in-memory indexing is enabled for the new update cycle
    if hasattr(G.TE, "update_in_mem_indexes"):
        G.TE.update_in_mem_indexes = True

    # -------------------------------------------------------------------------
    # STEP 2: Ensure performance B-tree indexes are active for worker queries (in parallel)
    # -------------------------------------------------------------------------
    performance_indexes = (
        ("ast_index", m_ast, (m_ast.name, m_ast.type_id)),
        ("file_name_index", m_file_name, (m_file_name.fname,)),
        ("bridge_tag_fid_idx", m_bridge_tag, (m_bridge_tag.blob_id, m_bridge_tag.tag_id)),
        ("file_ref_target_idx", m_file_reference, (m_file_reference.target_fnid, m_file_reference.ref_type)),
        ("file_ref_source_idx", m_file_reference, (m_file_reference.source_blob_id, m_file_reference.ref_type)),
    )
    with G.measure_stage("Step 2: Performance Index Setup", count=len(performance_indexes), units="indexes"):
        with G.DB() as db:
            db.create_indexes(performance_indexes)

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Step 2. Halting '{version}' early."))
        return

    # -------------------------------------------------------------------------
    # STEP 3: Clone repository branch version to RAMDISK workspace
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 3: Branch Checkout"):
        MF.add_version(version, gp.PURGE_LIST)

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Step 3. Halting '{version}' early."))
        return

    # -------------------------------------------------------------------------
    # STEP 4: Generate git diff change list & extract Git blob hashes
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 4: Tree Diff & Blob Hashes"):
        MF.generate_change_list(gp)

        git_dir = MF._get_git_dir()
        ls_res = sp.run(
            ["git", f"--git-dir={git_dir}", "ls-tree", "-r", f"{version}"],
            capture_output=True,
            text=True,
            check=False,
        )
        file_blob_hashes: dict[str, bytes] = {}
        for line in ls_res.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                path = parts[1]
                meta = parts[0].split()
                if len(meta) >= 3 and meta[1] == "blob":
                    h_hex = meta[2]
                    if len(h_hex) == 40:
                        file_blob_hashes[path] = bytes.fromhex(h_hex)
        gp.file_blob_hashes = file_blob_hashes

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Step 4. Halting '{version}' early."))
        return

    # -------------------------------------------------------------------------
    # STEP 4.5: Content-Addressable Blob Deduplication across Git trees
    # -------------------------------------------------------------------------
    if getattr(G, "STAGE_PROFILER", None) is not None:
        G.STAGE_PROFILER.start_stage("Step 4.5: Content-Addressable Blob Dedup")

    dedup_count = 0
    remaining_changes = []
    has_any_blobs = False
    try:
        with G.DB() as db:
            cur = db.cursor if hasattr(db, "cursor") else (db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None)
            if cur:
                cur.execute("SELECT 1 FROM m_file_blob WHERE blob_id > 0 LIMIT 1")
                has_any_blobs = cur.fetchone() is not None
                cur.close()
    except Exception as e:
        logger.debug(f"Note on checking m_file_blob for deduplication: {e}")

    if not has_any_blobs:
        # Initial run: database has no prior blobs to deduplicate against
        remaining_changes = list(gp.Change_List)
    else:
        candidate_items = []
        for item in gp.Change_List:
            if not item:
                continue
            parts = item.split("\t")
            op = parts[0]
            fpath = parts[-1]
            if op.startswith("D"):
                remaining_changes.append(item)
                continue
            b_hash = file_blob_hashes.get(fpath)
            if not b_hash:
                remaining_changes.append(item)
                continue
            ftype = type_check(fpath)
            candidate_items.append((item, op, fpath, b_hash, ftype))

        known_blobs: dict[tuple[bytes, int], int] = {}
        tagged_blob_ids: set[int] = set()
        fname_to_fnid: dict[str, int] = {}
        existing_chk_fnids: set[int] = set()
        existing_cf_fnids: set[int] = set()
        next_fnid = 1

        try:
            with G.DB() as db:
                cur = db.cursor if hasattr(db, "cursor") else (db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None)
                if cur:
                    cur.execute("SELECT blob_hash, ftype, blob_id FROM m_file_blob WHERE blob_id > 0")
                    for r in cur.fetchall():
                        known_blobs[(bytes(r[0]), int(r[1]))] = int(r[2])

                    cur.execute("SELECT DISTINCT blob_id FROM m_bridge_tag")
                    tagged_blob_ids = {int(r[0]) for r in cur.fetchall()}

                    cur.execute("SELECT fname, fnid FROM m_file_name")
                    for r in cur.fetchall():
                        fname_to_fnid[str(r[0])] = int(r[1])
                    if fname_to_fnid:
                        next_fnid = max(fname_to_fnid.values()) + 1

                    cur.execute("SELECT fnid FROM m_bridge_checkpoint WHERE commit_id = %s", (gp.VID,))
                    existing_chk_fnids = {int(r[0]) for r in cur.fetchall()}

                    cur.execute("SELECT fnid FROM m_commit_file WHERE commit_id = %s", (gp.VID,))
                    existing_cf_fnids = {int(r[0]) for r in cur.fetchall()}
                    cur.close()
        except Exception as e:
            logger.warning(f"Error preloading tables for blob deduplication: {e}")

        new_filenames: list[tuple[int, str]] = []
        staged_checkpoints: list[tuple[int, int, int]] = []
        staged_commit_files: list[tuple[int, int, int, int, int, str]] = []

        for item, op, fpath, b_hash, ftype in candidate_items:
            b_id = known_blobs.get((b_hash, ftype))
            if b_id is not None and b_id in tagged_blob_ids:
                fnid = fname_to_fnid.get(fpath)
                if fnid is None:
                    fnid = next_fnid
                    next_fnid += 1
                    fname_to_fnid[fpath] = fnid
                    new_filenames.append((fnid, fpath))
                if fnid not in existing_chk_fnids:
                    existing_chk_fnids.add(fnid)
                    staged_checkpoints.append((gp.VID, fnid, b_id))
                if fnid not in existing_cf_fnids:
                    existing_cf_fnids.add(fnid)
                    staged_commit_files.append((gp.VID, fnid, b_id, 0, 0, op[:1]))
                if hasattr(gp, "file_blob_ids") and isinstance(gp.file_blob_ids, dict):
                    gp.file_blob_ids[fpath] = b_id
                file_fid_cache[fpath] = b_id
                dedup_count += 1
            else:
                remaining_changes.append(item)

        if new_filenames or staged_checkpoints or staged_commit_files:
            try:
                with G.DB() as db:
                    if new_filenames:
                        db.insert(m_file_name, new_filenames)
                    if staged_checkpoints:
                        db.insert(m_bridge_checkpoint, staged_checkpoints)
                    if staged_commit_files:
                        db.insert(m_commit_file, staged_commit_files)
            except Exception as e:
                logger.warning(f"Error persisting deduplicated blob records: {e}")

    if dedup_count > 0:
        logger.info(COLOR.green(f"Content-Addressable Deduplication: Reused {dedup_count} existing file blobs directly without worker parsing."))
        gp.worker_change_list = remaining_changes
    else:
        gp.worker_change_list = list(gp.Change_List)

    if getattr(G, "STAGE_PROFILER", None) is not None:
        G.STAGE_PROFILER.stop_stage("Step 4.5: Content-Addressable Blob Dedup", count=dedup_count, units="reused")

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Step 4.5. Finalizing '{version}' early."))
        return

    G.TE.start(gp.Table_Array, G.DB)

    # -------------------------------------------------------------------------
    # STEP 5 & 6: Spawn multicore workers to parse files & stream ChangeSet execution
    # -------------------------------------------------------------------------
    chunk_commit_interval = 300 if G.VERY_LOW_MEMORY_MODE else (500 if G.LOW_MEMORY_MODE else 1000)
    scheduler = DependencyScheduler(gp, chunk_commit_interval=chunk_commit_interval, streaming=True)
    trigger_multicore(scheduler=scheduler)
    scheduler.finalize()

    # Commit all pending rows from Multicore / Stage 2 execution before post-processing steps
    G.TE.commit_all(update_in_mem_indexes=False)

    reclaim_system_memory()

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Multicore Parsing. Finalizing '{version}'..."))
        G.TE.commit_all(update_in_mem_indexes=False)
        G.TE.close()
        return

    # -------------------------------------------------------------------------
    # STEP 6.1: Resolve Symbolic Links & Alias directly in m_bridge_file
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 6.1: Symbolic Links Resolution"):
        processing_symlinks()

    # Disable TableEngine in-memory secondary indexing for write-only batch staging phases
    if hasattr(G.TE, "update_in_mem_indexes"):
        G.TE.update_in_mem_indexes = False

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached before Git commits. Finalizing '{version}'..."))
        G.TE.commit_all(update_in_mem_indexes=False)
        G.TE.close()
        return

    # -------------------------------------------------------------------------
    # STEP 6.5: Parse Git Commits, Multi-Contributors & Bridge Tags to Commits
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 6.5: Git Commits & Contributors"):
        processing_git_commits(version)

    # Commit git commits, checkpoint files, and git tags staged during git processing
    G.TE.commit_all(update_in_mem_indexes=False)

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached before File References. Finalizing '{version}'..."))
        G.TE.commit_all(update_in_mem_indexes=False)
        G.TE.close()
        return

    # -------------------------------------------------------------------------
    # STEP 6.6: Parse Kbuild/Makefiles, Documentation & Cross-File References
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 6.6: File References (Kbuild & Docs)"):
        processing_file_references(version)

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached before Maintainers. Finalizing '{version}'..."))
        G.TE.commit_all(update_in_mem_indexes=False)
        G.TE.close()
        return

    # -------------------------------------------------------------------------
    # STEP 6.7: Batch Match Maintainer Sections & Populate m_maintainer_file
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 6.7: Maintainer Files Matching"):
        processing_maintainer_files(version)

    if G.PROFILING_ENABLED and gp.ChangeSet_Dict:
        from core.Profiler import format_profiling_report
        profilers = [cs.profiler for cs in gp.ChangeSet_Dict.values() if getattr(cs, "profiler", None)]
        if profilers:
            print(format_profiling_report(profilers, title=f"UPDATE CYCLE PROFILE: {version}"))

    gp.reset_cs()
    reclaim_system_memory()

    # -------------------------------------------------------------------------
    # STEP 7: Drop secondary indexes, commit transactions, and rebuild indexes
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 7: Final DB Commit & Indexes"):
        if getattr(G.TE, "db", None) is not None and hasattr(G.TE.db, "cnx") and G.TE.db.cnx is not None:
            try:
                G.TE.db.cnx.commit()
            except Exception:
                pass

        if gp.VID == 1:
            with G.DB() as db:
                db.remove_indexes(tuple((item[0],item[1]) for item in performance_indexes))

        G.TE.commit_all(update_in_mem_indexes=False)

        if gp.VID == 1:
            with G.DB() as db:
                db.create_indexes(performance_indexes)

    file_fid_cache.clear()
    file_fnid_cache.clear()
    MF.clear_version_cache(version)
    MF.trim_version(keep=1)
    G.TE.close()
    if hasattr(G.TE, "update_in_mem_indexes"):
        G.TE.update_in_mem_indexes = True
    reclaim_system_memory()
    return


_INCLUDE_REGEX = re.compile(r'^\s*#\s*include\s*["<]([^">]+)[">]', re.M)


def order_changed_files(regular_files: list[str], working_dir: str | None) -> list[str]:
    """Topologically sort changed files so header files (.h) are parsed before dependent .c/.S files."""
    if not regular_files or not working_dir:
        return regular_files

    header_items = []
    other_items = []
    header_path_to_item = {}

    for item in regular_files:
        fpath = item.split("\t")[-1]
        if fpath.endswith((".h", ".hpp", ".hxx")):
            header_items.append(item)
            header_path_to_item[fpath] = item
        else:
            other_items.append(item)

    if len(header_items) <= 1:
        return header_items + other_items

    # Build dependency graph between changed headers
    # edge A -> B means B includes A (A must be parsed before B)
    graph = defaultdict(set)
    in_degree = defaultdict(int)
    for fpath in header_path_to_item:
        in_degree[fpath] = 0

    for fpath, item in header_path_to_item.items():
        if item.startswith("D"):
            continue
        full_path = os.path.join(working_dir, fpath)
        try:
            with open(full_path, "r", encoding="latin-1", errors="ignore") as f:
                content = f.read(131072)  # Read up to 128KB
        except Exception:
            continue

        for inc_match in _INCLUDE_REGEX.finditer(content):
            inc_target = inc_match.group(1)
            target_fpath = None
            if inc_target in header_path_to_item:
                target_fpath = inc_target
            elif f"include/{inc_target}" in header_path_to_item:
                target_fpath = f"include/{inc_target}"
            else:
                rel_candidate = os.path.normpath(os.path.join(os.path.dirname(fpath), inc_target))
                if rel_candidate in header_path_to_item:
                    target_fpath = rel_candidate

            if target_fpath and target_fpath != fpath:
                if fpath not in graph[target_fpath]:
                    graph[target_fpath].add(fpath)
                    in_degree[fpath] += 1

    # Kahn's algorithm for topological sort
    zero_in = deque([f for f, deg in in_degree.items() if deg == 0])
    sorted_header_paths = []

    while zero_in:
        node = zero_in.popleft()
        sorted_header_paths.append(node)
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                zero_in.append(neighbor)

    # If cyclic dependencies exist, append remaining headers in their original order
    if len(sorted_header_paths) < len(header_path_to_item):
        seen = set(sorted_header_paths)
        for item in header_items:
            fpath = item.split("\t")[-1]
            if fpath not in seen:
                sorted_header_paths.append(fpath)
                seen.add(fpath)

    sorted_headers = [header_path_to_item[fpath] for fpath in sorted_header_paths]
    return sorted_headers + other_items


def is_heavy_source_file(
    fpath: str,
    working_dir: str | None = None,
    threshold_bytes: int = 65536,
) -> bool:
    """Check if file has high parsing complexity in a project-agnostic manner.

    Determines weight based on file size and language complexity rather than hardcoded directory paths.
    Compiled AST files (.c, .cpp, .cc, .cxx, .rs, .s, .asm) exceeding threshold_bytes (default 64KB)
    are classified as heavy.

    Args:
        fpath: Relative or absolute file path (or git diff tab-delimited item).
        working_dir: Optional root working directory for disk stat lookups.
        threshold_bytes: Byte size threshold above which AST files are considered heavy.

    Returns:
        True if the file is an AST source file exceeding threshold_bytes, False otherwise.
    """
    if "\t" in fpath:
        fpath = fpath.split("\t")[-1]

    ext = os.path.splitext(fpath)[1].lower()
    is_ast_lang = ext in (
        ".c", ".cpp", ".cc", ".cxx", ".c++", ".cp",
        ".rs",
        ".s", ".asm",
        ".h", ".hpp", ".hxx",
    )
    if not is_ast_lang:
        return False

    if working_dir is None:
        try:
            if hasattr(gp, "Version_Name") and gp.Version_Name:
                working_dir = MF.version_dict.get(gp.Version_Name)
        except Exception:
            pass

    full_path = os.path.join(working_dir, fpath) if (working_dir and not os.path.isabs(fpath)) else fpath
    try:
        if os.path.exists(full_path):
            return os.path.getsize(full_path) >= threshold_bytes
    except Exception:
        pass

    return False


# Backward compatibility alias
is_heavy_source_path = is_heavy_source_file


def partition_source_epochs(
    source_files: list[str],
    working_dir: str | None = None,
    normal_batch_size: int = 200,
    heavy_batch_size: int = 50,
    max_batch_bytes: int = 1500000,
    heavy_file_threshold: int = 65536,
    target_epoch_files: int = 4500,
) -> list[list[tuple[int, list[str]]]]:
    """Partition source files into batches with dynamic complexity sizing and group them into natural subsystem-aligned epochs.

    Uses project-agnostic heuristics applicable to any repository:
    1. File complexity weight: Measures file size and language type rather than fixed directory rules.
    2. Dual-budget batching: Batches are bounded by both file count and cumulative source byte weight.
    3. Natural directory transitions: Epoch boundaries align with top-level directory changes.

    Args:
        source_files: List of git diff items for source files in preserved order.
        working_dir: Optional root working directory for disk stat lookups.
        normal_batch_size: Default batch size for lightweight files (e.g. 200).
        heavy_batch_size: Reduced batch size when heavy files are present (e.g. 50).
        max_batch_bytes: Maximum cumulative source code bytes per batch (e.g. 1.5MB).
        heavy_file_threshold: File size threshold (bytes) to flag an AST file as heavy.
        target_epoch_files: Approximate target count of files per epoch (e.g. 4500).

    Returns:
        List of epochs, where each epoch is a list of (global_batch_id, batch_files) tuples.
    """
    if not source_files:
        return []

    if working_dir is None:
        try:
            if hasattr(gp, "Version_Name") and gp.Version_Name:
                working_dir = MF.version_dict.get(gp.Version_Name)
        except Exception:
            pass

    batches: list[tuple[int, list[str]]] = []
    current_batch: list[str] = []
    current_batch_bytes = 0
    current_has_heavy = False
    batch_counter = 0

    for item in source_files:
        fpath = item.split("\t")[-1]
        sz = 0
        if working_dir:
            full_path = os.path.join(working_dir, fpath) if not os.path.isabs(fpath) else fpath
            try:
                sz = os.path.getsize(full_path)
            except Exception:
                sz = 0

        is_heavy = is_heavy_source_file(fpath, working_dir, threshold_bytes=heavy_file_threshold)
        limit = heavy_batch_size if (current_has_heavy or is_heavy) else normal_batch_size

        if (
            len(current_batch) >= limit
            or (current_batch_bytes + sz > max_batch_bytes and len(current_batch) > 0)
            or (is_heavy and len(current_batch) >= heavy_batch_size)
        ):
            if current_batch:
                batches.append((batch_counter, current_batch))
                batch_counter += 1
                current_batch = []
                current_batch_bytes = 0
                current_has_heavy = False

        current_batch.append(item)
        current_batch_bytes += sz
        if is_heavy:
            current_has_heavy = True

    if current_batch:
        batches.append((batch_counter, current_batch))
        batch_counter += 1

    if len(source_files) <= int(target_epoch_files * 1.3):
        return [batches]

    epochs: list[list[tuple[int, list[str]]]] = []
    current_epoch: list[tuple[int, list[str]]] = []
    current_epoch_files = 0

    for idx, (b_id, b_files) in enumerate(batches):
        current_epoch.append((b_id, b_files))
        current_epoch_files += len(b_files)

        if current_epoch_files >= target_epoch_files and idx + 1 < len(batches):
            curr_f = current_epoch[-1][1][-1].split("\t")[-1]
            next_f = batches[idx + 1][1][0].split("\t")[-1]
            curr_parts = os.path.dirname(curr_f).split(os.sep)
            next_parts = os.path.dirname(next_f).split(os.sep)
            curr_dir = os.sep.join(curr_parts[:2]) if len(curr_parts) > 1 else (curr_parts[0] if curr_parts else "")
            next_dir = os.sep.join(next_parts[:2]) if len(next_parts) > 1 else (next_parts[0] if next_parts else "")

            if curr_dir != next_dir or current_epoch_files >= int(target_epoch_files * 1.35):
                epochs.append(current_epoch)
                current_epoch = []
                current_epoch_files = 0

    if current_epoch:
        epochs.append(current_epoch)

    return epochs


class DependencyScheduler:
    """Dependency-driven ChangeSet resolution and execution scheduler with two-stage streaming ingestion."""

    def __init__(self, gp_ref: GreatProcessor, chunk_commit_interval: int = 0, streaming: bool = True) -> None:
        self.gp = gp_ref
        self.streaming = streaming
        self.deferred_queue: deque[str] = deque()
        self.completed: set[str] = set()
        self.executed_count = 0
        self.chunk_commit_interval = chunk_commit_interval if chunk_commit_interval > 0 else 1000

    def ingest_batch(self, batch_dict: dict[str, ChangeSet], is_header_stage: bool = False) -> None:
        """Ingest newly arrived ChangeSets from a worker batch into gp.ChangeSet_Dict."""
        # Collect worker micro-profiler metrics into StageProfiler if active
        if getattr(G, "STAGE_PROFILER", None) is not None:
            for cs_obj in batch_dict.values():
                if getattr(cs_obj, "profiler", None) is not None:
                    G.STAGE_PROFILER.record_worker_profiler(cs_obj.profiler)
                    cs_obj.profiler = None

        # 1. Extract lightweight foreign dependencies into gp.file_deps before compression
        for path, cs_obj in batch_dict.items():
            f_deps = getattr(cs_obj, "foreign_deps", None)
            if f_deps:
                self.gp.file_deps[path] = set(f_deps)
            else:
                self.gp.file_deps[path] = set()

        purged = execute_and_purge(batch_dict)
        if is_header_stage:
            for path, cs_obj in purged.items():
                cs_obj.batch_cs_dict = purged

            for path, cs_obj in purged.items():
                if getattr(cs_obj, "cs_processed", False):
                    self.completed.add(path)
                    cs_obj.batch_cs_dict = None
                    self.gp.ChangeSet_Dict[path] = cs_obj
                    continue
                if path in self.completed:
                    cs_obj.batch_cs_dict = None
                    self.gp.ChangeSet_Dict[path] = cs_obj
                    continue
                f_deps = getattr(cs_obj, "foreign_deps", None)
                if not f_deps or all(dep in self.completed for dep in f_deps):
                    cs_obj.gp = self.gp
                    if cs_obj.execute():
                        self.completed.add(path)
                        self.executed_count += 1
                        extract_tags_and_evacuate_cs(cs_obj)
                        cs_obj.batch_cs_dict = None
                        self.gp.ChangeSet_Dict[path] = cs_obj
                        continue
                cs_obj.batch_cs_dict = None
                self.gp.ChangeSet_Dict[path] = cs_obj
        else:
            # During source stage (or standalone test ingestion), execute ChangeSets immediately in streaming mode!
            # Avoid upfront double-compression: only store into ChangeSet_Dict after execution & evacuation.
            for path, cs_obj in purged.items():
                cs_obj.batch_cs_dict = purged

            for path, cs_obj in purged.items():
                if getattr(cs_obj, "cs_processed", False):
                    self.completed.add(path)
                    cs_obj.batch_cs_dict = None
                    self.gp.ChangeSet_Dict[path] = cs_obj
                    continue
                if path in self.completed:
                    cs_obj.batch_cs_dict = None
                    self.gp.ChangeSet_Dict[path] = cs_obj
                    continue
                cs_obj.gp = self.gp
                if cs_obj.execute():
                    self.completed.add(path)
                    self.executed_count += 1
                    extract_tags_and_evacuate_cs(cs_obj)
                    cs_obj.batch_cs_dict = None
                    self.gp.ChangeSet_Dict[path] = cs_obj
                    if self.executed_count % self.chunk_commit_interval == 0:
                        try:
                            G.TE.commit_all()
                        except Exception as commit_err:
                            logger.warning(f"Chunk commit hit transient error ({commit_err}); re-verifying DB connection and retrying...")
                            if getattr(G.TE, "db", None) is not None and hasattr(G.TE.db, "check_if_connected"):
                                G.TE.db.check_if_connected()
                            G.TE.commit_all()
                        if hasattr(self.gp, "_ast_negative_cache"):
                            self.gp._ast_negative_cache.clear()
                        reclaim_system_memory()
                else:
                    # Unresolvable reference: defer to final wave pass
                    cs_obj.batch_cs_dict = None
                    if hasattr(cs_obj, "clear_bloat"):
                        cs_obj.clear_bloat()
                    else:
                        cs_obj.gp = None
                    self.gp.ChangeSet_Dict[path] = cs_obj
                    self.deferred_queue.append(path)

    def resolve_headers(self) -> None:
        """Resolve all remaining header ChangeSets in wave order, breaking circular deadlocks with stubs."""
        execute_phase2_parallel_waves(self.gp, chunk_commit_interval=self.chunk_commit_interval, completed_set=self.completed)
        self.completed.update(self.gp.ChangeSet_Dict.keys())
        self.executed_count = len(self.completed)
        if hasattr(self.gp, "_ast_negative_cache"):
            self.gp._ast_negative_cache.clear()
        reclaim_system_memory()

    def drain_deferred_queue(self) -> int:
        """Attempt non-stub execution for deferred queue items that may now be unblocked.

        Returns:
            Number of newly unblocked and completed ChangeSets.
        """
        if not self.deferred_queue:
            return 0
        unblocked_count = 0
        still_deferred = deque()
        while self.deferred_queue:
            current_cs = self.deferred_queue.popleft()
            if current_cs in self.completed:
                continue
            cs_obj = self.gp.ChangeSet_Dict.get(current_cs)
            if cs_obj is None:
                continue
            cs_obj.gp = self.gp
            if cs_obj.execute(force_stubs=False):
                self.completed.add(current_cs)
                self.executed_count += 1
                unblocked_count += 1
                extract_tags_and_evacuate_cs(cs_obj)
                self.gp.ChangeSet_Dict[current_cs] = cs_obj
            else:
                still_deferred.append(current_cs)

        self.deferred_queue = still_deferred
        return unblocked_count

    def finalize(self) -> None:
        """Finalize resolution of all remaining ChangeSets via Phase 2 wave-based execution."""
        # 1. First attempt non-stub execution for deferred queue items that may now be unblocked
        self.drain_deferred_queue()

        # 2. If any unexecuted ChangeSets remain, run wave resolution
        execute_phase2_parallel_waves(self.gp, chunk_commit_interval=self.chunk_commit_interval, completed_set=self.completed)
        self.completed.update(self.gp.ChangeSet_Dict.keys())
        self.executed_count = len(self.completed)
        if hasattr(self.gp, "_ast_negative_cache"):
            self.gp._ast_negative_cache.clear()
        G.TE.commit_all(update_in_mem_indexes=False)
        reclaim_system_memory()
_phase2_shared_symbols: Any | None = None


def _phase2_resolve_worker(task: Any) -> tuple[str, list]:
    """Worker task pre-resolving foreign references and unpacking views for a ChangeSet using zero-copy shared memory."""
    global _phase2_shared_symbols
    if isinstance(task, tuple) and len(task) == 3:
        cs, resolved_symbols_map, resolved_names_map = task
        cs.gp = None
        pre_resolved = cs.pre_resolve_operations(resolved_symbols_map, resolved_names_map)
        return cs.current_path, pre_resolved

    cs = task
    cs.gp = None
    syms_view = None
    names_view = None
    if _phase2_shared_symbols is not None:
        syms_view = _phase2_shared_symbols.symbols
        names_view = _phase2_shared_symbols.names
    elif getattr(gp, "_shared_symbols", None) is not None:
        syms_view = gp._shared_symbols.symbols
        names_view = gp._shared_symbols.names

    pre_resolved = cs.pre_resolve_operations(syms_view, names_view)
    return cs.current_path, pre_resolved


def execute_phase2_parallel_waves(
    gp: GreatProcessor,
    chunk_commit_interval: int = 0,
    completed_set: set[str] | None = None,
) -> None:
    """Execute unexecuted ChangeSets using zero-copy shared memory wave-based resolution.

    Eliminates memory spikes by:
    1. Backing symbol lookups with SharedSymbolReader mmap across forked workers (zero IPC dict copying).
    2. Enforcing micro-chunk database commits every wave or 150-250 files to bound TableEngine heap buffers.
    3. Performing batch cycle breaking on strongly-connected circular dependency deadlocks.
    4. Immediately evacuating AST buffers upon execution.
    """
    global _phase2_shared_symbols

    if chunk_commit_interval <= 0:
        chunk_commit_interval = 150 if G.VERY_LOW_MEMORY_MODE else (250 if G.LOW_MEMORY_MODE else 400)

    if completed_set is None:
        completed_set = set()

    # If file_deps is not yet populated (e.g. synthetic test), populate from ChangeSet_Dict
    if not gp.file_deps and gp.ChangeSet_Dict:
        for path, cs_obj in gp.ChangeSet_Dict.items():
            f_deps = getattr(cs_obj, "foreign_deps", None)
            if f_deps is None:
                f_deps = set()
                cs_ops = getattr(cs_obj, "cs", [])
                for op in cs_ops:
                    if op and len(op) >= 3 and is_data_unsafe(op[2]):
                        for col in op[2]:
                            if type(col) is tuple and len(col) == 3 and col[1] == OP_REF and col[2] and col[2][0] == REF_FILE:
                                f_deps.add(col[2][1])
                cs_obj.foreign_deps = f_deps
            gp.file_deps[path] = set(f_deps)
            if getattr(cs_obj, "cs_processed", False):
                completed_set.add(path)

    # 1. Identify all remaining unexecuted paths (<2.5 MB)
    remaining: set[str] = set()
    for path in gp.file_deps.keys():
        if path in completed_set:
            continue
        cs = gp.ChangeSet_Dict.get(path)
        if cs is None:
            continue
        if not getattr(cs, "cs_processed", False) or (getattr(cs, "unresolved_indices", None) is not None and len(cs.unresolved_indices) > 0):
            remaining.add(path)
        else:
            completed_set.add(path)

    if not remaining:
        return

    logger.info(COLOR.cyan(f"[Phase 2] {len(remaining)} unexecuted ChangeSets entering wave resolution."))

    # 2. Build or reuse structured shared memory symbols hashtable
    from table_engine.shared_buffer import SharedSymbolBuilder
    if getattr(gp, "_shared_symbols", None) is None and gp.file_symbols:
        resolved_symbols_map: dict[tuple[str, int], int] = {}
        for f_map in gp.file_symbols.values():
            resolved_symbols_map.update(f_map)
        resolved_names_map = {k[0]: v for k, v in resolved_symbols_map.items()}
        sym_builder = SharedSymbolBuilder(mode=getattr(G.TE, "hugepages", "auto"))
        gp._shared_symbols = sym_builder.build(resolved_symbols_map, resolved_names_map)
        del resolved_symbols_map
        del resolved_names_map
        reclaim_system_memory()

    _phase2_shared_symbols = getattr(gp, "_shared_symbols", None)

    # 3. Build lightweight dependency DAG and inverted dependent mapping (~10 MB)
    dependents: dict[str, set[str]] = defaultdict(set)
    deps: dict[str, set[str]] = {}
    for path in remaining:
        f_deps = {target for target in gp.file_deps.get(path, ()) if target in remaining and target != path}
        deps[path] = f_deps
        for target in f_deps:
            dependents[target].add(path)

    wave_num = 0
    executed_count = 0
    last_commit_count = 0
    t_phase2_start = time.perf_counter()
    resolution_succeeded = False

    try:
        while remaining:
            if G.is_deadline_exceeded():
                logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during Wave Resolution. Halting early..."))
                break
            wave_num += 1
            t_wave_start = time.perf_counter()
            # Wave selection: files with no unexecuted dependencies in remaining
            wave = [p for p in remaining if not deps.get(p)]

            if not wave:
                # Deadlock detected: precision minimal cycle breaking on top candidate with most dependents
                dep_counts = {p: len(dependents.get(p, ())) for p in remaining}
                sorted_cands = sorted(
                    remaining,
                    key=lambda k: (dep_counts[k], -len(getattr(gp.ChangeSet_Dict.get(k), "cs", ()) or ())),
                    reverse=True,
                )
                cand_path = sorted_cands[0]
                logger.warning(COLOR.yellow(
                    f"[Phase 2] Breaking circular dependency deadlock: precision force-resolving 1 stub "
                    f"(candidate '{cand_path}' has {dep_counts[cand_path]} dependents, {len(remaining)} files remaining)"
                ))

                cand_cs = gp.ChangeSet_Dict[cand_path]
                cand_cs.gp = gp
                cand_cs.execute(force_stubs=True)
                extract_tags_and_evacuate_cs(cand_cs)
                gp.ChangeSet_Dict[cand_path] = cand_cs
                remaining.remove(cand_path)
                completed_set.add(cand_path)
                if cand_path in deps:
                    del deps[cand_path]
                for waiter in dependents.pop(cand_path, ()):
                    if waiter in deps:
                        deps[waiter].discard(cand_path)
                executed_count += 1

                if executed_count - last_commit_count >= chunk_commit_interval:
                    logger.info(COLOR.yellow(
                        f"[Phase 2] Micro-chunk commit: committing batch at {executed_count} executed ChangeSets to database..."
                    ))
                    G.TE.commit_all()
                    reclaim_system_memory()
                    last_commit_count = executed_count
                continue

            logger.info(COLOR.cyan(
                f"[Phase 2] Wave {wave_num}: resolving {len(wave)} files ({len(remaining)} remaining)..."
            ))

            for path in wave:
                cs = gp.ChangeSet_Dict[path]
                cs.gp = gp
                if not cs.execute():
                    # Every dependency of a wave file has executed: references still blocked can only
                    # be resolved by stubs. Evacuating now would silently drop those operations.
                    blocked_on = cs.blocked_on
                    if not cs.execute(force_stubs=True):
                        logger.error(COLOR.red(
                            f"[Phase 2] '{path}': {len(cs.unresolved_indices or ())} operations remain unresolved "
                            f"after stub resolution (blocked on '{cs.blocked_on or blocked_on}'); they are dropped."
                        ))
                extract_tags_and_evacuate_cs(cs)
                gp.ChangeSet_Dict[path] = cs
                remaining.remove(path)
                completed_set.add(path)
                if path in deps:
                    del deps[path]
                for waiter in dependents.pop(path, ()):
                    if waiter in deps:
                        deps[waiter].discard(path)
                executed_count += 1

                if executed_count - last_commit_count >= chunk_commit_interval:
                    logger.info(COLOR.yellow(
                        f"[Phase 2] Micro-chunk commit: committing batch at {executed_count} executed ChangeSets to database..."
                    ))
                    G.TE.commit_all()
                    reclaim_system_memory()
                    last_commit_count = executed_count

            wave_elapsed = time.perf_counter() - t_wave_start
            velocity = len(wave) / max(0.001, wave_elapsed)
            logger.info(COLOR.green(
                f"[Phase 2] Wave {wave_num} resolved {len(wave)} files in {wave_elapsed:.2f}s "
                f"({velocity:.1f} files/s). {len(remaining)} files remaining."
            ))

        resolution_succeeded = True
    finally:
        pass

        if resolution_succeeded and executed_count > last_commit_count:
            logger.info(COLOR.yellow(
                f"[Phase 2] Final commit: committing batch at {executed_count} executed ChangeSets to database..."
            ))
            G.TE.commit_all()
            if hasattr(gp, "_ast_negative_cache"):
                gp._ast_negative_cache.clear()
            reclaim_system_memory()

        _phase2_shared_symbols = None

    total_phase2_time = time.perf_counter() - t_phase2_start
    avg_velocity = executed_count / max(0.001, total_phase2_time)
    logger.info(COLOR.green(
        f"[Phase 2] Completed resolution of {executed_count} ChangeSets across {wave_num} waves "
        f"in {total_phase2_time:.1f}s ({avg_velocity:.1f} files/s)."
    ))


def trigger_multicore(batch_size: int | None = None, scheduler: DependencyScheduler | None = None) -> None:
    """Distribute file parsing across parallel worker processes in two persistent stages: headers first, then sources."""
    change_list = getattr(gp, "worker_change_list", None) or gp.Change_List or []
    working_dir = MF.version_dict.get(gp.Version_Name)

    regular_files = []
    symlink_files = []

    for item in change_list:
        if not item:
            continue
        parts = item.split("\t")
        op = parts[0]
        fpath = parts[-1]
        if not op.startswith("D") and working_dir and os.path.islink(os.path.join(working_dir, fpath)):
            symlink_files.append(item)
        else:
            regular_files.append(item)

    if not hasattr(gp, "Symlink_List") or gp.Symlink_List is None:
        gp.Symlink_List = []
    gp.Symlink_List.extend(symlink_files)

    # Order regular files: header DAG first
    regular_files = order_changed_files(regular_files, working_dir)
    gp._changed_paths_set = set(item.split("\t")[-1] for item in (regular_files + symlink_files) if item)

    header_files = []
    source_files = []
    for item in regular_files:
        fpath = item.split("\t")[-1]
        if fpath.endswith((".h", ".hpp", ".hxx")):
            header_files.append(item)
        else:
            source_files.append(item)

    total_files = len(regular_files)
    total_headers = len(header_files)
    total_sources = len(source_files)

    if batch_size is None:
        if G.VERY_LOW_MEMORY_MODE:
            batch_size = 25
        elif G.LOW_MEMORY_MODE:
            batch_size = 50
        else:
            batch_size = 200

    if G.VERY_LOW_MEMORY_MODE:
        num_workers = max(1, min(2, int(G.CPUS // 4)))
    else:
        num_workers = max(1, int(G.CPUS - 1))

    total_header_batches = (total_headers + batch_size - 1) // batch_size if total_headers > 0 else 0
    total_source_batches = (total_sources + batch_size - 1) // batch_size if total_sources > 0 else 0
    total_batches = total_header_batches + total_source_batches

    logger.info(
        f"Distributing {total_files} changed files ({total_headers} headers, {total_sources} sources) in {total_batches} batches "
        f"(batch size: {batch_size}, memory mode: {G.MEMORY_MODE}) across {num_workers} parallel workers"
    )

    # If no change within this version
    if total_files == 0:
        processing_dirs()
        processing_unchanges()
        if scheduler is not None and gp.ChangeSet_Dict:
            scheduler.ingest_batch(dict(gp.ChangeSet_Dict), is_header_stage=False)
        return

    # Pre-populate all file paths into m_file_name and commit before forking.
    # This guarantees that all current version files exist in m_file_name with assigned IDs,
    # preventing race conditions or duplicate sequence ID assignments across worker chunks.
    for item in (gp.Change_List or (regular_files + symlink_files)):
        if not item:
            continue
        parts = item.split("\t")
        for p in parts[1:]:
            if p:
                G.TE.set(m_file_name.table_id, (None, p))
    G.TE.commit(m_file_name.table_id)

    # Close active DB connection in parent before forking child worker processes
    # to prevent inherited socket file descriptor sharing / corruption across fork
    if getattr(G.TE, "db", None) is not None:
        try:
            G.TE.db.close()
        except Exception:
            pass
        G.TE.db = None
    # Freeze living parent memory so worker GC cycles never scan or dirty inherited parent pages via Copy-on-Write
    freeze_system_memory()
    # Spawn background worker for Git commit and contributor parsing concurrently with AST workers
    commit_queue = multiprocessing.Queue()
    commit_proc = multiprocessing.Process(
        target=_git_commit_worker,
        args=(gp.Old_Version_Name, gp.Version_Name, commit_queue, MF.get_repo_dir()),
    )
    commit_proc.start()
    gp._commit_queue = commit_queue
    gp._commit_proc = commit_proc

    failed_workers = 0
    error_list = []

    # =========================================================================
    # STAGE 1: Parallel Header Parsing & Wave Resolution
    # =========================================================================
    if total_headers > 0:
        header_task_queue = multiprocessing.Queue()
        header_result_queue = multiprocessing.Queue()
        header_error_queue = multiprocessing.Queue()

        for i in range(0, total_headers, batch_size):
            header_task_queue.put((i // batch_size, header_files[i : i + batch_size]))

        for _ in range(num_workers):
            header_task_queue.put(None)

        if getattr(G.TE, "db", None) is not None:
            try:
                G.TE.db.close()
            except Exception:
                pass
            G.TE.db = None

        header_processes = []
        for worker_id in range(num_workers):
            p = multiprocessing.Process(
                target=file_processing_worker,
                args=(header_task_queue, header_error_queue, header_result_queue, gp.VID, gp, MF, worker_id),
            )
            header_processes.append(p)
            p.start()

        if getattr(G.TE, "db", None) is not None:
            try:
                G.TE.db.close()
            except Exception:
                pass
        G.TE.db = G.DB()

        header_prefetch_queue: queue.Queue = queue.Queue(maxsize=max(8, num_workers * 2))

        def _header_prefetch_loop() -> None:
            workers_done = 0
            while workers_done < num_workers:
                try:
                    item = header_result_queue.get()
                except Exception:
                    break
                if item is None:
                    workers_done += 1
                    header_prefetch_queue.put(None)
                    continue
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "__WORKER_RECYCLE__":
                    recycle_id = item[1]
                    new_p = multiprocessing.Process(
                        target=file_processing_worker,
                        args=(header_task_queue, header_error_queue, header_result_queue, gp.VID, gp, MF, recycle_id),
                    )
                    header_processes.append(new_p)
                    new_p.start()
                    continue

                b_id, payload = item
                if payload is None:
                    header_prefetch_queue.put((b_id, None))
                    continue

                try:
                    try:
                        raw_bytes = zlib.decompress(payload)
                        partial_dict = pickle.loads(raw_bytes)
                        del raw_bytes
                    except Exception:
                        partial_dict = pickle.loads(payload)
                    header_prefetch_queue.put((b_id, partial_dict))
                    del partial_dict
                except Exception as e:
                    logger.error(f"Prefetcher failed to deserialize header batch {b_id}: {e}")
                    header_prefetch_queue.put((b_id, None))

        header_prefetch_thread = threading.Thread(target=_header_prefetch_loop, daemon=True)
        header_prefetch_thread.start()

        processing_dirs()
        processing_unchanges()

        if scheduler is not None and gp.ChangeSet_Dict:
            scheduler.ingest_batch(dict(gp.ChangeSet_Dict), is_header_stage=True)

        header_batches_received = 0
        finished_header_workers = 0
        t_stage1_start = time.perf_counter()

        while finished_header_workers < num_workers:
            try:
                item = header_prefetch_queue.get()
                if item is None:
                    finished_header_workers += 1
                    continue
                batch_id, partial_dict = item
                header_batches_received += 1
                if partial_dict is not None:
                    if scheduler is not None:
                        scheduler.ingest_batch(partial_dict, is_header_stage=True)
                    else:
                        gp.ChangeSet_Dict.update(execute_and_purge(partial_dict))
                    del partial_dict

                if G.is_deadline_exceeded():
                    logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during Stage 1 header streaming. Halting early..."))
                    for p in header_processes:
                        if p.is_alive():
                            p.terminate()
                    break

                if header_batches_received % 10 == 0 or header_batches_received == total_header_batches:
                    elapsed = time.perf_counter() - t_stage1_start
                    velocity = header_batches_received / max(0.001, elapsed)
                    logger.info(
                        f"[Stage 1 - Headers] Ingested header batch {header_batches_received}/{total_header_batches} "
                        f"({velocity:.1f} batches/s, elapsed {elapsed:.1f}s)"
                    )
            except Exception as e:
                logger.error(f"Error reading header batch result: {e}", exc_info=True)
                for p in header_processes:
                    if p.is_alive():
                        p.terminate()
                if commit_proc.is_alive():
                    commit_proc.terminate()
                break
            reclaim_system_memory()

        for p in header_processes:
            p.join(timeout=5.0)
            if p.is_alive():
                p.kill()
                p.join(timeout=1.0)
            if p.exitcode != 0:
                if G.is_deadline_exceeded() and p.exitcode in (-15, -9):
                    logger.info(f"Header Worker PID {p.pid} stopped gracefully on execution deadline.")
                else:
                    failed_workers += 1
                    logger.error(COLOR.red(f"Header Worker PID {p.pid} terminated abnormally with exit code {p.exitcode}"))

        while not header_error_queue.empty():
            try:
                error_list.append(header_error_queue.get_nowait())
            except Exception:
                break

        del header_processes
        reclaim_system_memory()

        # Resolve Stage 1: All headers in wave order
        if scheduler is not None:
            with G.measure_stage("Stage 1: Header Wave Resolution", count=total_headers, units="headers"):
                logger.info(COLOR.cyan(f"[Stage 1 - Headers] Resolving {total_headers} header files in wave order..."))
                scheduler.resolve_headers()
                logger.info(COLOR.green(f"[Stage 1 - Headers] Header resolution complete. {len(gp.file_symbols)} header symbol tables indexed."))
    else:
        processing_dirs()
        processing_unchanges()
        if scheduler is not None and gp.ChangeSet_Dict:
            scheduler.ingest_batch(dict(gp.ChangeSet_Dict), is_header_stage=False)

    # =========================================================================
    # STAGE 2: Multi-Epoch Parallel Source Parsing with Periodic Shared Regeneration
    # =========================================================================
    if total_sources > 0:
        stage2_metric_cm = G.measure_stage("Stage 2: Source Streaming & Parsing", count=total_sources, units="sources")
        stage2_metric = stage2_metric_cm.__enter__()
        resolved_symbols_map: dict[tuple[str, int], int] = {}
        for f_map in gp.file_symbols.values():
            resolved_symbols_map.update(f_map)
        resolved_names_map = {k[0]: v for k, v in resolved_symbols_map.items()}

        # Build initial structured shared memory index for Stage 2 workers from Stage 1 headers
        from table_engine.shared_buffer import SharedSymbolBuilder
        sym_builder = SharedSymbolBuilder(mode=getattr(G.TE, "hugepages", "auto"))
        shared_sym_reader = sym_builder.build(resolved_symbols_map, resolved_names_map)
        if shared_sym_reader:
            logger.info(
                f"[Stage 2 - Sources] Packed {shared_sym_reader.sym_count} symbols and "
                f"{shared_sym_reader.name_count} names into {len(shared_sym_reader.mv) // (1024*1024)}MB shared memory hashtable."
            )
            gp._shared_symbols = shared_sym_reader
            total_sym_count = shared_sym_reader.sym_count
            del resolved_symbols_map
            del resolved_names_map
            gp.file_symbols.clear()
            if hasattr(gp, "file_names"):
                gp.file_names.clear()
            if hasattr(gp, "_ast_staged_symbols"):
                gp._ast_staged_symbols.clear()
            if hasattr(gp, "_ast_staged_names"):
                gp._ast_staged_names.clear()
            if hasattr(gp, "unchanged_symbol_cache"):
                gp.unchanged_symbol_cache.clear()
            reclaim_system_memory()
        else:
            gp._shared_symbols = None
            total_sym_count = len(resolved_symbols_map)

        # Project-agnostic dynamic complexity batching and natural epoch partitioning
        working_dir = MF.version_dict.get(gp.Version_Name)
        normal_bs = 200 if not G.LOW_MEMORY_MODE and not G.VERY_LOW_MEMORY_MODE else (50 if G.LOW_MEMORY_MODE else 25)
        heavy_bs = 50 if not G.LOW_MEMORY_MODE and not G.VERY_LOW_MEMORY_MODE else (25 if G.LOW_MEMORY_MODE else 10)
        max_bytes = 1500000 if not G.LOW_MEMORY_MODE and not G.VERY_LOW_MEMORY_MODE else (750000 if G.LOW_MEMORY_MODE else 350000)
        target_epoch_sz = 4500 if not G.VERY_LOW_MEMORY_MODE else 2000

        source_epochs = partition_source_epochs(
            source_files,
            working_dir=working_dir,
            normal_batch_size=normal_bs,
            heavy_batch_size=heavy_bs,
            max_batch_bytes=max_bytes,
            target_epoch_files=target_epoch_sz,
        )
        total_epochs = len(source_epochs)
        total_source_batches = sum(len(ep) for ep in source_epochs)

        logger.info(
            f"[Stage 2 - Sources] Partitioned {total_sources} source files into {total_epochs} natural epochs "
            f"({total_source_batches} complexity-binned batches) with {total_sym_count} resolved header symbols across {num_workers} parallel workers"
        )

        total_source_batches_received = 0
        source_files_streamed = 0
        t_stage2_start = time.perf_counter()

        for epoch_idx, epoch_batches in enumerate(source_epochs):
            epoch_num = epoch_idx + 1
            epoch_files_count = sum(len(b[1]) for b in epoch_batches)
            t_epoch_start = time.perf_counter()

            logger.info(
                f"[Stage 2 - Sources] Starting Epoch {epoch_num}/{total_epochs}: "
                f"{epoch_files_count} files in {len(epoch_batches)} batches with {total_sym_count} resolved symbols across {num_workers} parallel workers"
            )

            epoch_task_queue = multiprocessing.Queue()
            epoch_result_queue = multiprocessing.Queue()
            epoch_error_queue = multiprocessing.Queue()

            for b_id, b_files in epoch_batches:
                epoch_task_queue.put((b_id, b_files))

            for _ in range(num_workers):
                epoch_task_queue.put(None)

            if getattr(G.TE, "db", None) is not None:
                try:
                    G.TE.db.close()
                except Exception:
                    pass
                G.TE.db = None

            # Freeze living parent memory so worker GC cycles never scan or dirty inherited parent pages via Copy-on-Write
            freeze_system_memory()

            epoch_processes = []
            for worker_id in range(num_workers):
                p = multiprocessing.Process(
                    target=file_processing_worker,
                    args=(
                        epoch_task_queue,
                        epoch_error_queue,
                        epoch_result_queue,
                        gp.VID,
                        gp,
                        MF,
                        worker_id,
                        None,
                        None,
                        False,  # allow_mid_epoch_recycle=False (clean epoch-boundary lifecycle)
                    ),
                )
                epoch_processes.append(p)
                p.start()

            if getattr(G.TE, "db", None) is not None:
                try:
                    G.TE.db.close()
                except Exception:
                    pass
            G.TE.db = G.DB()

            epoch_prefetch_queue: queue.Queue = queue.Queue(maxsize=max(8, num_workers * 2))

            def _epoch_prefetch_loop(res_q=epoch_result_queue, pref_q=epoch_prefetch_queue) -> None:
                workers_done = 0
                while workers_done < num_workers:
                    try:
                        item = res_q.get()
                    except Exception:
                        break
                    if item is None:
                        workers_done += 1
                        pref_q.put(None)
                        continue

                    b_id, payload = item
                    if payload is None:
                        pref_q.put((b_id, None))
                        continue

                    try:
                        try:
                            raw_bytes = zlib.decompress(payload)
                            partial_dict = pickle.loads(raw_bytes)
                            del raw_bytes
                        except Exception:
                            partial_dict = pickle.loads(payload)
                        pref_q.put((b_id, partial_dict))
                        del partial_dict
                    except Exception as e:
                        logger.error(f"Prefetcher failed to deserialize source batch {b_id}: {e}")
                        pref_q.put((b_id, None))

            prefetch_thread = threading.Thread(target=_epoch_prefetch_loop, daemon=True)
            prefetch_thread.start()

            epoch_batches_received = 0
            finished_epoch_workers = 0

            while finished_epoch_workers < num_workers:
                try:
                    item = epoch_prefetch_queue.get()
                    if item is None:
                        finished_epoch_workers += 1
                        continue
                    batch_id, partial_dict = item
                    epoch_batches_received += 1
                    total_source_batches_received += 1
                    if partial_dict is not None:
                        source_files_streamed += len(partial_dict)
                        if scheduler is not None:
                            scheduler.ingest_batch(partial_dict, is_header_stage=False)
                        else:
                            gp.ChangeSet_Dict.update(execute_and_purge(partial_dict))
                        del partial_dict

                    if G.is_deadline_exceeded():
                        logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during Stage 2 source streaming. Halting early..."))
                        for p in epoch_processes:
                            if p.is_alive():
                                p.terminate()
                        break

                    if epoch_batches_received % 10 == 0 or epoch_batches_received == len(epoch_batches):
                        elapsed_epoch = time.perf_counter() - t_epoch_start
                        vel = epoch_batches_received / max(0.001, elapsed_epoch)
                        logger.info(
                            f"[Stage 2 - Sources] Epoch {epoch_num}/{total_epochs} streamed batch {epoch_batches_received}/{len(epoch_batches)} "
                            f"(total: {total_source_batches_received}/{total_source_batches}, {vel:.1f} batches/s, "
                            f"{finished_epoch_workers}/{num_workers} workers done, elapsed {elapsed_epoch:.1f}s)"
                        )
                except Exception as e:
                    logger.error(f"Error reading source batch result: {e}", exc_info=True)
                    for p in epoch_processes:
                        if p.is_alive():
                            p.terminate()
                    if commit_proc.is_alive():
                        commit_proc.terminate()
                    break
                if epoch_batches_received % 10 == 0:
                    reclaim_system_memory()

            # Cleanly reap all workers for this epoch
            for p in epoch_processes:
                p.join(timeout=5.0)
                if p.is_alive():
                    p.kill()
                    p.join(timeout=1.0)
                if p.exitcode != 0:
                    if G.is_deadline_exceeded() and p.exitcode in (-15, -9):
                        logger.info(f"Source Worker PID {p.pid} stopped gracefully on execution deadline.")
                    else:
                        failed_workers += 1
                        logger.error(COLOR.red(f"Source Worker PID {p.pid} terminated abnormally with exit code {p.exitcode}"))

            while not epoch_error_queue.empty():
                try:
                    error_list.append(epoch_error_queue.get_nowait())
                except Exception:
                    break

            del epoch_processes
            reclaim_system_memory()

            if G.is_deadline_exceeded():
                break

            # =====================================================================
            # INTER-EPOCH REGENERATION & DRAIN:
            # 1. Drain deferred queue for any newly unblocked cross-file dependencies
            # 2. Chunk commit TableEngine to flush staged rows & update DB
            # 3. Rebuild SharedSymbolReader with all resolved symbols for next epoch
            # =====================================================================
            if epoch_idx < total_epochs - 1:
                if scheduler is not None:
                    unblocked = scheduler.drain_deferred_queue()
                    if unblocked > 0:
                        logger.info(
                            f"[Stage 2 - Sources] Inter-epoch deferred drain unblocked {unblocked} files "
                            f"({len(scheduler.deferred_queue)} still deferred)."
                        )
                try:
                    G.TE.commit_all()
                except Exception as e:
                    logger.warning(f"Inter-epoch TableEngine commit encountered transient issue: {e}")
                    if getattr(G.TE, "db", None) is not None and hasattr(G.TE.db, "check_if_connected"):
                        G.TE.db.check_if_connected()
                    G.TE.commit_all()

                # Harvest all resolved symbols across headers + all completed sources
                resolved_symbols_map = {}
                if getattr(gp, "_shared_symbols", None) is not None:
                    try:
                        for sym_k, sym_id in gp._shared_symbols.items():
                            resolved_symbols_map[sym_k] = sym_id
                    except Exception:
                        pass
                for f_map in gp.file_symbols.values():
                    resolved_symbols_map.update(f_map)
                resolved_names_map = {k[0]: v for k, v in resolved_symbols_map.items()}

                if getattr(gp, "_shared_symbols", None) is not None:
                    try:
                        gp._shared_symbols.close()
                    except Exception:
                        pass
                    gp._shared_symbols = None

                reclaim_system_memory()

                sym_builder = SharedSymbolBuilder(mode=getattr(G.TE, "hugepages", "auto"))
                shared_sym_reader = sym_builder.build(resolved_symbols_map, resolved_names_map)
                if shared_sym_reader:
                    logger.info(
                        f"[Stage 2 - Sources] Epoch {epoch_num} complete. Rebuilt shared memory hashtable with "
                        f"{shared_sym_reader.sym_count} symbols and {shared_sym_reader.name_count} names "
                        f"({len(shared_sym_reader.mv) // (1024*1024)}MB) for subsequent epochs."
                    )
                    gp._shared_symbols = shared_sym_reader
                    total_sym_count = shared_sym_reader.sym_count
                del resolved_symbols_map
                del resolved_names_map
                gp.file_symbols.clear()
                if hasattr(gp, "file_names"):
                    gp.file_names.clear()
                if hasattr(gp, "_ast_staged_symbols"):
                    gp._ast_staged_symbols.clear()
                if hasattr(gp, "_ast_staged_names"):
                    gp._ast_staged_names.clear()
                if hasattr(gp, "unchanged_symbol_cache"):
                    gp.unchanged_symbol_cache.clear()
                reclaim_system_memory()

        if getattr(gp, "_shared_symbols", None) is not None:
            try:
                gp._shared_symbols.close()
            except Exception:
                pass
            gp._shared_symbols = None
        reclaim_system_memory()
        if stage2_metric is not None:
            stage2_metric.count = min(total_sources, source_files_streamed)
        if stage2_metric_cm is not None:
            stage2_metric_cm.__exit__(None, None, None)

    if error_list:
        logger.error(COLOR.red(f"Multicore processing encountered {len(error_list)} file error(s):"))
        for failed_file, err, tb in error_list:
            logger.error(COLOR.red(f"  [ERROR] File: {failed_file} => {err}"))

    if failed_workers > 0 or error_list:
        logger.error(
            COLOR.red(
                f"Multicore execution completed with {failed_workers} crashed worker(s) "
                f"and {len(error_list)} file error(s)!"
            )
        )

    unfreeze_system_memory()
    reclaim_system_memory()
    return


def main() -> None:
    """Set the plan for what version to parse."""
    args = arg_handling()
    setup_memory_limit(float(args.mem_max))
    with G.DB() as db:
        if getattr(args, "reset", False) or getattr(args, "Drop", False):
            logger.info("Resetting and recreating all database tables...")
            db.drop_table(gp.Table_Array)
            db.create_table(gp.Table_Array)
            try:
                db.create_index("v_main_index", m_v_main, (m_v_main.vname,))
            except Exception:
                pass
            ensure_db_instance(db)
            ensure_db_views(db)
            if hasattr(G.TE, "start_new_db") and getattr(G.TE, "tables", None):
                G.TE.start_new_db(G.DB, is_worker=False)
        else:
            missing = db.test_tables(gp.Table_Array)
            if missing:
                logger.info(f"Missing tables detected ({missing}), creating tables...")
                missing_tables = [tbl for tbl in gp.Table_Array if tbl.table_name in missing]
                db.create_table(missing_tables)
                try:
                    db.create_index("v_main_index", m_v_main, (m_v_main.vname,))
                except Exception:
                    pass
            ensure_db_instance(db)
            ensure_db_views(db)

    # Repository: --repo may be a configured repository name or a path; default is the configured default
    repo_arg = getattr(args, "repo", None)
    repo_entry = get_repository(repo_arg)
    if repo_entry:
        repo_path = str(resolve_repo_path(repo_entry))
    elif repo_arg:
        repo_path = repo_arg
    else:
        repo_path = str(get_default_repo_path())
    MF.set_repo_dir(repo_path)
    logger.info(f"[Repository] Using '{(repo_entry or {}).get('name', repo_path)}' at {repo_path}")

    raw_refs = getattr(args, "ref", None)
    if not raw_refs:
        # Default: parse from the first commit of the repository, then every subsequent release
        start_ref = (repo_entry or {}).get("start_ref") or (repo_entry or {}).get("root_commit") or MF.get_root_commit()
        if not start_ref:
            logger.critical(COLOR.red(f"Could not resolve the first commit of repository '{repo_path}'."))
            sys.exit(1)
        logger.info(f"[Release Pipeline] No --ref given; starting from the first commit {start_ref[:12]}")
        raw_refs = [start_ref]
    elif isinstance(raw_refs, str):
        raw_refs = [raw_refs]

    # By default, keep parsing subsequent releases after the first version unless single_version is requested
    single_version = bool(getattr(args, "single_version", False))
    include_pre_releases = getattr(args, "include_pre_releases", None)
    if len(raw_refs) == 1 and not single_version:
        start_ref = raw_refs[0]
        discovered = MF.get_version_tags(start_ref=start_ref, include_pre_releases=include_pre_releases)
        if len(discovered) > 1:
            logger.info(
                f"[Release Pipeline] Continuous multi-version parsing active (default). "
                f"Discovered {len(discovered)} sequential releases starting at '{start_ref}': "
                f"{', '.join(discovered[:6])}{'...' if len(discovered) > 6 else ''}"
            )
            target_refs = discovered
        else:
            target_refs = raw_refs
    else:
        target_refs = raw_refs

    # Interactive terminals: pressing 'q' finishes the version being parsed, then quits
    current = {"ref": None}
    quit_listener = GracefulQuitListener(
        on_request=lambda: logger.warning(COLOR.yellow(
            f"[Quit Requested] Finishing '{current['ref']}' before stopping; remaining versions are skipped. "
            "Press Ctrl+C to abort immediately."
        ))
    )
    if quit_listener.start():
        logger.info(COLOR.cyan("[Release Pipeline] Press 'q' at any time to finish the current version and quit."))

    try:
        total_targets = len(target_refs)
        for ref_idx, ref in enumerate(target_refs):
            if G.is_deadline_exceeded():
                logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached before starting next reference."))
                break
            if quit_listener.requested.is_set():
                logger.warning(COLOR.yellow(
                    f"[Quit Requested] Stopping before '{ref}' ({total_targets - ref_idx} version(s) not parsed)."
                ))
                break
            current["ref"] = ref
            if total_targets > 1:
                logger.info(COLOR.cyan(f"[Release Pipeline] Progress: version {ref_idx + 1}/{total_targets} ('{ref}')"))
            update(ref, repo_path=repo_path)
    except MemoryError:
        logger.critical(COLOR.red("FATAL: Memory limit of 60.0 GB (RLIMIT_AS) exceeded! Terminating execution cleanly."))
        sys.exit(1)
    finally:
        quit_listener.stop()
        if getattr(G, "STAGE_PROFILER", None) is not None:
            print(G.STAGE_PROFILER.format_stage_report())

    logger.info("We are done! Closing")
    G.emergency_shutdown(0)
    return


def arg_handling() -> argparse.Namespace:
    """Handle arguments passed with python."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c", "--config",
        dest="config",
        default=None,
        help="Path to JSON configuration file",
    )
    parser.add_argument(
        "--repo", "--repository",
        dest="repo",
        default=None,
        help="Configured repository name or local repository path (default: the default repository in config 'repositories')",
    )
    parser.add_argument(
        "--detect-repos",
        dest="detect_repos",
        action="store_true",
        help="Detect Git repositories under workspace.repos_dir, register them in the config file, print them and exit",
    )
    parser.add_argument(
        "-v", "--ref", "--version",
        dest="ref",
        nargs="*",
        default=None,
        help="Target Git reference(s), tag(s), branch(es), or starting commit to parse (default: the repository's first commit, continuing through subsequent releases)",
    )
    parser.add_argument(
        "-s", "--single-version", "--single",
        dest="single_version",
        action="store_true",
        default=None,
        help="Stop after parsing the first/specified version instead of continuing through subsequent releases (default: False)",
    )
    parser.add_argument(
        "--rc", "--include-rc", "--include-pre-releases",
        dest="include_pre_releases",
        action="store_true",
        default=None,
        help="Include release candidate tags (-rc#) and pre-releases during continuous version discovery (default)",
    )
    parser.add_argument(
        "--no-rc", "--releases-only",
        dest="include_pre_releases",
        action="store_false",
        help="Skip release candidate tags (-rc#) and pre-releases; parse final releases only",
    )
    parser.add_argument(
        "--project",
        dest="project",
        default="linux",
        help="Project ecosystem slug/name (default: 'linux')",
    )
    parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=None,
        help="Optional maximum commit count to ingest in Git DAG history",
    )
    parser.add_argument(
        "-r", "--reset", "--reset-db",
        dest="reset",
        action="store_true",
        help="Reset and recreate all database tables from scratch",
    )
    parser.add_argument(
        "-D", "--Drop",
        help="Drop all tables", action="store_true",
    )
    parser.add_argument(
        "-C", "--Create-Tables",
        help="Generate all tables", action="store_true",
    )
    parser.add_argument(
        "-u", "--unit-test", "--test-unit",
        dest="unit_test",
        nargs="?",
        const="",
        default=None,
        help="Run all tests in the testing suite (optionally specify a single file to test C-AST)",
    )
    parser.add_argument(
        "-T", "--Test",
        help="Test/Parse a specific file",
    )
    parser.add_argument(
        "-p", "--profile",
        action="store_true",
        help="Enable granular stage timing profiler across tests and update loop cycles",
    )
    parser.add_argument(
        "--duration",
        dest="duration",
        default=None,
        help="Maximum execution duration before graceful halt and profiling report (e.g., '30m', '1800s', '1h')",
    )
    parser.add_argument(
        "-f", "--fidelity",
        dest="fidelity",
        action="store_true",
        default=None,
        help="Display full tag text & source code fidelity audit report across test files (default: True)",
    )
    parser.add_argument(
        "--no-fidelity",
        dest="fidelity",
        action="store_false",
        help="Disable tag text & source code fidelity audit report",
    )
    parser.add_argument(
        "-l", "--low-mem", "--low-memory",
        dest="low_mem",
        action="store_true",
        help="Enable Low Memory Mode: utilizes all CPU cores with reduced batch sizes and intermediate chunk commits",
    )
    parser.add_argument(
        "-vl", "--very-low-mem", "--very-low-memory",
        dest="very_low_mem",
        action="store_true",
        help="Enable Very Low Memory Mode: throttles CPU cores (<=2) with ultra-compact batch sizes and continuous memory compaction",
    )
    parser.add_argument(
        "--mm", "--mem-max",
        dest="mem_max",
        default=60,
        help="Set memory max limit in GB. Defaults to 60GB",
    )
    parser.add_argument(
        "--db", "--db-engine",
        dest="db_engine",
        default=None,
        choices=["mariadb", "mysql", "mock", "mockdb", "inmemory"],
        help="Select database backend engine (default: from config or mariadb)",
    )
    parser.add_argument(
        "--te", "--table-engine",
        dest="table_engine",
        default=None,
        choices=["cached", "direct", "tecacheddb", "tedirectdb"],
        help="Select Table Engine architecture backend (default: from config or cached)",
    )
    parser.add_argument(
        "--hugepages",
        dest="hugepages",
        default=None,
        choices=["auto", "1g", "2m", "thp", "off"],
        help="Configure huge-page backing for TableEngine shared memory (default: auto)",
    )
    parser.add_argument(
        "--no-hugepages",
        dest="hugepages",
        action="store_const",
        const="off",
        help="Disable huge-page shared memory backing in TableEngine",
    )
    parser.add_argument(
        "--ssh-tunnel",
        dest="ssh_tunnel",
        action="store_true",
        default=None,
        help="Enable SSH port forwarding tunnel to remote MariaDB server",
    )
    parser.add_argument(
        "--no-ssh-tunnel",
        dest="ssh_tunnel",
        action="store_false",
        help="Disable SSH port forwarding tunnel to remote MariaDB server",
    )
    parser.add_argument(
        "--ssh-host",
        dest="ssh_host",
        default=None,
        help="Remote SSH server hostname or IP address",
    )
    parser.add_argument(
        "--ssh-port",
        dest="ssh_port",
        type=int,
        default=None,
        help="Remote SSH server port (default: 22)",
    )
    parser.add_argument(
        "--ssh-user",
        dest="ssh_user",
        default=None,
        help="Username for SSH authentication",
    )
    parser.add_argument(
        "--ssh-password",
        dest="ssh_password",
        default=None,
        help="Password for SSH authentication",
    )
    parser.add_argument(
        "--ssh-key-file",
        dest="ssh_key_file",
        default=None,
        help="Path to SSH private key file",
    )
    parser.add_argument(
        "--ssh-remote-host",
        dest="ssh_remote_host",
        default=None,
        help="Target database host from remote server perspective (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--ssh-remote-port",
        dest="ssh_remote_port",
        type=int,
        default=None,
        help="Target database port on remote server (default: 3306)",
    )
    parser.add_argument(
        "--ssh-local-port",
        dest="ssh_local_port",
        type=int,
        default=None,
        help="Local port to bind SSH forward to (default: 0 for dynamic ephemeral port)",
    )
    parser.add_argument(
        "--ssh-strict-host-key-checking",
        dest="ssh_strict_host_key_checking",
        choices=["accept-new", "no", "yes"],
        default=None,
        help="SSH host key verification mode (default: accept-new)",
    )
    args = parser.parse_args()

    if args.config is not None:
        init_config(args.config)

    # Register repositories found under workspace.repos_dir into the config file
    if args.detect_repos or get_workspace_config().get("auto_detect_repos", True):
        repos = sync_repositories(write=True)
        if args.detect_repos:
            if not repos:
                print("No repositories configured or detected.")
            for r in repos:
                print(
                    f"{r.get('name', '?'):<16} {r.get('path', ''):<28} branch={r.get('default_branch', '')} "
                    f"start={str(r.get('start_ref', ''))[:12]} enabled={r.get('enabled', True)}"
                )
            sys.exit(0)

    parser_cfg = get_parser_config()
    db_cfg = get_db_config()
    ssh_cfg = get_ssh_tunnel_config()

    # Apply CLI argument overrides for SSH tunnel (CLI > ENV > JSON > Defaults)
    if args.ssh_tunnel is not None:
        ssh_cfg["enabled"] = args.ssh_tunnel
    if args.ssh_host is not None:
        ssh_cfg["host"] = args.ssh_host
    if args.ssh_port is not None:
        ssh_cfg["port"] = args.ssh_port
    if args.ssh_user is not None:
        ssh_cfg["user"] = args.ssh_user
    if args.ssh_password is not None:
        ssh_cfg["password"] = args.ssh_password
    if args.ssh_key_file is not None:
        ssh_cfg["key_file"] = args.ssh_key_file
    if args.ssh_remote_host is not None:
        ssh_cfg["remote_host"] = args.ssh_remote_host
    if args.ssh_remote_port is not None:
        ssh_cfg["remote_port"] = args.ssh_remote_port
    if args.ssh_local_port is not None:
        ssh_cfg["local_port"] = args.ssh_local_port
    if args.ssh_strict_host_key_checking is not None:
        ssh_cfg["strict_host_key_checking"] = args.ssh_strict_host_key_checking

    if ssh_cfg.get("enabled"):
        start_ssh_tunnel(ssh_cfg)
        # Re-fetch db_cfg since start_ssh_tunnel reroutes host and port
        db_cfg = get_db_config()

    if args.very_low_mem:
        G.MEMORY_MODE = "very_low"
    elif args.low_mem:
        G.MEMORY_MODE = "low"
    elif parser_cfg.get("memory_mode"):
        G.MEMORY_MODE = parser_cfg["memory_mode"]

    if args.fidelity is None:
        args.fidelity = parser_cfg.get("fidelity", True)

    if args.mem_max == 60:
        args.mem_max = parser_cfg.get("mem_max")

    if getattr(args, "single_version", None) is None:
        args.single_version = parser_cfg.get("single_version", False)

    # --limit bounds how many commits ingest_git_dag() records per version range (None = all)
    gp.commit_limit = args.limit if args.limit and args.limit > 0 else None

    if getattr(args, "include_pre_releases", None) is None:
        args.include_pre_releases = parser_cfg.get("include_pre_releases", parser_cfg.get("rc", True))

    gp.init_cs_dict()

    if getattr(args, "duration", None):
        duration_s = parse_duration(args.duration)
        if duration_s is not None:
            G.set_execution_deadline(duration_s)
            G.PROFILING_ENABLED = True
            logger.info(COLOR.cyan(f"[Duration Limit] Configured maximum run duration of {args.duration} ({duration_s:.1f}s)"))

    if args.profile or G.PROFILING_ENABLED:
        G.PROFILING_ENABLED = True
        if getattr(G, "STAGE_PROFILER", None) is None:
            G.STAGE_PROFILER = PipelineStageProfiler()

    effective_db_engine = args.db_engine or db_cfg.get("engine") or "mariadb"
    effective_table_engine = args.table_engine or parser_cfg.get("table_engine") or "cached"
    effective_hugepages = args.hugepages or parser_cfg.get("hugepages") or "auto"

    G.DB = get_db_engine(effective_db_engine)
    EngineCls = get_table_engine(effective_table_engine)
    if issubclass(EngineCls, TECachedDB):
        G.TE = EngineCls(hugepages=effective_hugepages)
    else:
        G.TE = EngineCls()

    if args.Drop:
        logger.info("Dropping all tables")
        gp.drop_all()
    if args.Create_Tables:
        gp.create_table_all()
        G.emergency_shutdown(0)
    if args.unit_test is not None:
        try:
            with G.DB() as db:
                missing = db.test_tables(gp.Table_Array)
                if missing:
                    logger.info(f"Missing tables detected ({missing}), creating tables...")
                    missing_tables = [tbl for tbl in gp.Table_Array if tbl.table_name in missing]
                    db.create_table(missing_tables)
                ensure_db_instance(db)
        except Exception as e:
            logger.debug(f"DB check before unit tests: {e}")

        target = args.unit_test if args.unit_test != "" else None
        from tests.test_c_ast import run_c_ast_tests
        if target:
            code = run_c_ast_tests(target, profile=args.profile, fidelity=args.fidelity, table_engine=args.table_engine)
            sys.exit(code)

        # 1. Multi-Core C-AST regression suite
        c_ast_code = run_c_ast_tests(None, profile=args.profile, fidelity=args.fidelity, table_engine=args.table_engine)

        # 2. Comprehensive Unittest Suite across all test modules
        print(COLOR.cyan("=========================================================================================="))
        print(COLOR.cyan("                        RUNNING COMPREHENSIVE UNIT TEST SUITE                             "))
        print(COLOR.cyan("=========================================================================================="))
        import unittest
        loader = unittest.TestLoader()
        test_modules = [
            "unit_test.test_db_engine",
            "unit_test.test_table_engine",
            "unit_test.test_table_handling",
            "unit_test.test_raw_parser",
            "unit_test.test_c_parser",
            "tests.test_maintainer_ast",
            "tests.test_credits_lifecycle",
            "tests.test_bridge_map_dedup",
            "tests.test_git_commit_parser",
            "tests.test_tree_resolver",
            "tests.test_te_db_integrity",
            "tests.test_kconfig_ast",
            "tests.test_kconfig_engine",
            "tests.test_webapp_defconfig",
            "tests.test_webapp_maintainer",
            "tests.test_raw_ast",
            "tests.test_rust_ast",
            "tests.test_config",
            "tests.test_git_reader",
            "tests.test_webapp_system",
            "tests.test_file_references",
            "tests.test_duration_profiler",
            "tests.test_quit_listener",
            "tests.test_commit_diff",
            "tests.test_webapp_advanced_features",
            "tests.test_history_service",
            "tests.test_cast_golden",
        ]
        suite = unittest.TestSuite()
        for mod_name in test_modules:
            try:
                suite.addTests(loader.loadTestsFromName(mod_name))
            except Exception as e:
                logger.error(f"Failed loading test module {mod_name}: {e}")

        runner = unittest.TextTestRunner(verbosity=2)
        res = runner.run(suite)

        all_ok = (c_ast_code == 0) and res.wasSuccessful()
        if all_ok:
            print(COLOR.green(f"\n[+] ALL {res.testsRun} UNIT TESTS AND C-AST REGRESSIONS COMPLETED SUCCESSFULLY!"))
        else:
            print(COLOR.red(f"\n[-] TEST FAILURES DETECTED IN TEST SUITE."))
        sys.exit(0 if all_ok else 1)
    if args.Test:
        from tests.test_c_ast import run_c_ast_tests
        code = run_c_ast_tests(args.Test, profile=args.profile, fidelity=args.fidelity, table_engine=args.table_engine)
        sys.exit(code)

    return args


def resolve_git_network_and_repo(
    repo_dir: str | None = None,
    repo_name: str | None = None,
    project_name: str = "Linux Kernel",
    project_slug: str = "linux",
) -> tuple[int, int]:
    """Discover or register m_project, m_git_network, and m_repository for the given git tree."""
    repo_dir = repo_dir or getattr(MF, "repo_dir", None) or "linux"
    git_dir = os.path.join(repo_dir, ".git") if os.path.isdir(os.path.join(repo_dir, ".git")) else repo_dir

    roots = []
    try:
        res = sp.run(
            ["git", f"--git-dir={git_dir}", "rev-list", "--max-parents=0", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        roots = [r.strip() for r in res.stdout.splitlines() if r.strip()]
    except Exception as e:
        logger.warning(f"Failed to query root commit for {repo_dir}: {e}")

    if roots:
        root_hex = sorted(roots)[0]
        root_bytes = bytes.fromhex(root_hex) if len(root_hex) == 40 else b"\x00" * 20
    else:
        root_bytes = b"\x00" * 20

    if not repo_name:
        repo_name = os.path.basename(os.path.abspath(repo_dir)) or "linux"

    with G.DB() as db:
        # 1. Resolve m_project
        proj = db.select(m_project, (None, None, project_slug))
        if proj:
            project_id = proj[0]
        else:
            project_id = db.get_next_id(m_project)
            db.insert(m_project, (project_id, project_name, project_slug))

        # 2. Resolve m_git_network by root_commit_hash
        net = db.select(m_git_network, (None, None, None, root_bytes))
        if net:
            network_id = net[0]
        else:
            network_id = db.get_next_id(m_git_network)
            net_name = f"{project_slug}-network"
            db.insert(m_git_network, (network_id, project_id, net_name, root_bytes))

        # 3. Resolve m_repository by (network_id, repo_name)
        repo_match = db.select(m_repository, (None, network_id, repo_name, None, None))
        if repo_match:
            repo_id = repo_match[0]
        else:
            repo_id = db.get_next_id(m_repository)
            git_url = ""
            try:
                url_res = sp.run(
                    ["git", f"--git-dir={git_dir}", "remote", "get-url", "origin"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                git_url = url_res.stdout.strip()
            except Exception:
                pass

            def_branch = "master"
            try:
                branch_res = sp.run(
                    ["git", f"--git-dir={git_dir}", "symbolic-ref", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if branch_res.stdout.strip():
                    def_branch = branch_res.stdout.strip()
            except Exception:
                pass

            db.insert(m_repository, (repo_id, network_id, repo_name, git_url, def_branch))

    gp.network_id = network_id
    gp.repo_id = repo_id
    return network_id, repo_id


def ingest_git_dag(
    version_or_ref: str,
    network_id: int,
    repo_id: int,
    repo_path: str,
    limit: int | None = None,
) -> tuple[int, bool, bool]:
    """Ingest git commit DAG history up to version_or_ref and register git reference in m_git_ref.

    Returns:
        (head_commit_id, is_checkpoint, already_exists)
    """
    repo_path = repo_path or getattr(MF, "repo_dir", None) or "linux"
    git_dir = os.path.join(repo_path, ".git") if os.path.isdir(os.path.join(repo_path, ".git")) else repo_path

    # Resolve commit SHA for version_or_ref
    c_res = sp.run(
        ["git", f"--git-dir={git_dir}", "rev-parse", f"{version_or_ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    head_commit_hex = c_res.stdout.strip()
    if not head_commit_hex or len(head_commit_hex) != 40:
        c_res2 = sp.run(
            ["git", f"--git-dir={git_dir}", "rev-parse", version_or_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        head_commit_hex = c_res2.stdout.strip()

    head_commit_bytes = bytes.fromhex(head_commit_hex) if len(head_commit_hex) == 40 else b"\x00" * 20

    tag_check = sp.run(
        ["git", f"--git-dir={git_dir}", "tag", "-l", version_or_ref],
        capture_output=True,
        text=True,
        check=False,
    )
    is_tag = bool(tag_check.stdout.strip() == version_or_ref)
    branch_check = sp.run(
        ["git", f"--git-dir={git_dir}", "for-each-ref", "--count=1", f"refs/heads/{version_or_ref}", f"refs/remotes/{version_or_ref}"],
        capture_output=True,
        text=True,
        check=False,
    )
    # m_git_ref.ref_type: 1 = tag, 2 = branch, 3 = bare commit (e.g. the repository's first commit)
    ref_type = 1 if is_tag else (2 if branch_check.stdout.strip() else 3)
    is_checkpoint = is_tag or (gp.VID == 0 or getattr(gp, "Old_VID", 0) == 0)

    # Check if already ingested in m_git_ref with committed checkpoint files
    with G.DB() as db:
        ref_row = db.select(m_git_ref, (None, repo_id, None, version_or_ref, None, None, None, None))
        if ref_row:
            existing_cid = ref_row[4]
            chk = db.select(m_bridge_checkpoint, (existing_cid, None, None))
            is_complete = bool(chk)
            if chk and is_checkpoint:
                try:
                    cur = db.cursor if hasattr(db, "cursor") else (db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None)
                    if cur:
                        cur.execute("SELECT COUNT(*) FROM m_bridge_checkpoint WHERE commit_id = %s", (existing_cid,))
                        row_cnt = cur.fetchone()[0]
                        cur.close()
                        tree_files = len(MF.git_file_list(version_or_ref).splitlines())
                        if tree_files > 0 and row_cnt < max(1, int(tree_files * 0.9)):
                            is_complete = False
                except Exception:
                    pass
            if is_complete:
                gp.Old_VID = gp.VID
                gp.VID = existing_cid
                gp.Old_Version_Name = gp.Version_Name
                gp.Version_Name = version_or_ref
                return existing_cid, is_checkpoint, True
            else:
                # Prior ingestion was interrupted/incomplete: purge partial checkpoint & commit records for clean rebuild
                logger.warning(
                    f"Prior ingestion for ref '{version_or_ref}' (commit_id {existing_cid}) was incomplete; "
                    f"purging partial checkpoint and commit records for a clean restart."
                )
                try:
                    cur = db.cursor if hasattr(db, "cursor") else (db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None)
                    if cur:
                        cur.execute("DELETE FROM m_bridge_checkpoint WHERE commit_id = %s", (existing_cid,))
                        cur.execute("DELETE FROM m_commit_file WHERE commit_id = %s", (existing_cid,))
                        cur.execute("DELETE FROM m_bridge_commit_tag WHERE commit_id = %s", (existing_cid,))
                        cur.execute("DELETE FROM m_bridge_commit_person WHERE commit_id = %s", (existing_cid,))
                        if hasattr(db, "cnx") and db.cnx and hasattr(db.cnx, "commit"):
                            db.cnx.commit()
                        cur.close()
                except Exception as clean_err:
                    logger.debug(f"Note on purging partial commit records for existing_cid {existing_cid}: {clean_err}")

    # Ingest commits leading up to target ref
    with G.DB() as db:
        commit_row = db.select(m_commit, (None, network_id, head_commit_bytes, None, None, None, None, None, None))
        head_commit_id = commit_row[0] if commit_row else None
        if head_commit_id is not None and is_checkpoint:
            try:
                cur = db.cursor if hasattr(db, "cursor") else (db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None)
                if cur:
                    cur.execute("SELECT COUNT(*) FROM m_bridge_checkpoint WHERE commit_id = %s", (head_commit_id,))
                    row_cnt = cur.fetchone()[0]
                    tree_files = len(MF.git_file_list(version_or_ref).splitlines())
                    if tree_files > 0 and 0 < row_cnt < max(1, int(tree_files * 0.9)):
                        logger.warning(
                            f"Prior commit records for '{version_or_ref}' (commit_id {head_commit_id}) were incomplete ({row_cnt}/{tree_files} files); "
                            f"purging partial records."
                        )
                        cur.execute("DELETE FROM m_bridge_checkpoint WHERE commit_id = %s", (head_commit_id,))
                        cur.execute("DELETE FROM m_commit_file WHERE commit_id = %s", (head_commit_id,))
                        cur.execute("DELETE FROM m_bridge_commit_tag WHERE commit_id = %s", (head_commit_id,))
                        cur.execute("DELETE FROM m_bridge_commit_person WHERE commit_id = %s", (head_commit_id,))
                        if hasattr(db, "cnx") and db.cnx and hasattr(db.cnx, "commit"):
                            db.cnx.commit()
                    cur.close()
            except Exception as clean_err:
                logger.debug(f"Note on checking/purging partial head_commit_id records: {clean_err}")

    if head_commit_id is None:
        from parser.git_ast import GitCommitParser
        git_parser = GitCommitParser(repo_dir=repo_path)
        base_rev = getattr(gp, "Old_Version_Name", None)
        # The DAG only needs commit metadata and parents; per-commit file lists come from processing_git_commits
        commits = git_parser.parse_version_commits(old_rev=base_rev, new_rev=version_or_ref, limit=limit, with_files=False)
        logger.info(f"[Git DAG] Ingesting {len(commits)} commits up to '{version_or_ref}'")
        commit_hash_to_id: dict[str, int] = {}
        with G.DB() as db:
            for commit in reversed(commits):
                a_name = commit.author_name or commit.author_email or "Unknown"
                a_email = commit.author_email or ""
                a_res = db.select(m_maintainer_person, (None, None, a_email)) if a_email else None
                if a_res:
                    author_pid = a_res[0]
                else:
                    author_pid = db.get_next_id(m_maintainer_person)
                    db.insert(m_maintainer_person, (author_pid, a_name, a_email))

                c_name = commit.committer_name or commit.committer_email or a_name
                c_email = commit.committer_email or a_email
                if c_email == a_email:
                    committer_pid = author_pid
                else:
                    c_res = db.select(m_maintainer_person, (None, None, c_email)) if c_email else None
                    if c_res:
                        committer_pid = c_res[0]
                    else:
                        committer_pid = db.get_next_id(m_maintainer_person)
                        db.insert(m_maintainer_person, (committer_pid, c_name, c_email))

                c_hash = commit.commit_hash
                c_bytes = bytes.fromhex(c_hash) if isinstance(c_hash, str) and len(c_hash) == 40 else b"\x00" * 20
                c_match = db.select(m_commit, (None, network_id, c_bytes, None, None, None, None, None, None))
                if c_match:
                    c_id = c_match[0]
                else:
                    c_id = db.get_next_id(m_commit)
                    db.insert(m_commit, (
                        c_id,
                        network_id,
                        c_bytes,
                        author_pid,
                        int(commit.author_date or 0),
                        committer_pid,
                        int(commit.committer_date or 0),
                        (commit.subject or "")[:500],
                        commit.message or "",
                    ))
                commit_hash_to_id[c_hash] = c_id

                if hasattr(commit, "parents") and commit.parents:
                    for p_idx, p_hash in enumerate(commit.parents):
                        p_cid = commit_hash_to_id.get(p_hash)
                        if p_cid is None:
                            p_bytes = bytes.fromhex(p_hash) if len(p_hash) == 40 else b"\x00" * 20
                            p_match = db.select(m_commit, (None, network_id, p_bytes, None, None, None, None, None, None))
                            if p_match:
                                p_cid = p_match[0]
                                commit_hash_to_id[p_hash] = p_cid
                        if p_cid is not None:
                            try:
                                db.insert(m_commit_parent, (c_id, p_cid, p_idx))
                            except Exception:
                                pass

                for prio, contrib in enumerate(commit.contributors):
                    ct_email = contrib.email or ""
                    ct_name = contrib.name or ct_email or "Unknown"
                    ct_res = db.select(m_maintainer_person, (None, None, ct_email)) if ct_email else None
                    if ct_res:
                        t_pid = ct_res[0]
                    else:
                        t_pid = db.get_next_id(m_maintainer_person)
                        db.insert(m_maintainer_person, (t_pid, ct_name, ct_email))
                    try:
                        db.insert(m_bridge_commit_person, (c_id, t_pid, int(contrib.role), int(contrib.priority)))
                    except Exception:
                        pass

        head_commit_id = commit_hash_to_id.get(head_commit_hex)

    if head_commit_id is None:
        with G.DB() as db:
            commit_row = db.select(m_commit, (None, network_id, head_commit_bytes, None, None, None, None, None, None))
            if commit_row:
                head_commit_id = commit_row[0]
            else:
                head_commit_id = db.get_next_id(m_commit)
                db.insert(m_commit, (
                    head_commit_id,
                    network_id,
                    head_commit_bytes,
                    1, 0, 1, 0, f"Commit {version_or_ref}", "",
                ))

    # Register in m_git_ref
    with G.DB() as db:
        ref_row = db.select(m_git_ref, (None, repo_id, None, version_or_ref, None, None, None, None))
        if ref_row:
            ref_id = ref_row[0]
            try:
                db.update(m_git_ref, (ref_id, repo_id, ref_type, version_or_ref, head_commit_id, None, None, None))
            except Exception:
                pass
        else:
            ref_id = db.get_next_id(m_git_ref)
            db.insert(m_git_ref, (ref_id, repo_id, ref_type, version_or_ref, head_commit_id, None, None, None))

    gp.Old_VID = gp.VID
    gp.VID = head_commit_id
    gp.Old_Version_Name = gp.Version_Name
    gp.Version_Name = version_or_ref
    return head_commit_id, is_checkpoint, False


def create_new_vid(name: str) -> bool:
    """Register or synchronize active version in m_git_ref (m_v_main). Returns True if version already complete."""
    network_id, repo_id = resolve_git_network_and_repo(MF.repo_dir)
    head_cid, is_checkpoint, already_exists = ingest_git_dag(
        name, network_id, repo_id, MF.repo_dir, limit=getattr(gp, "commit_limit", None)
    )
    return already_exists

def default_processing(CS: ChangeSet) -> None:
    """Create m_file_name, m_file, m_bridge_file."""
    try:
        if CS.file_operation == "D":
            # DELETE
            with CS(REF_OLD):
                # Get old file_name
                CS.store(m_file_name.get_set(
                    None,
                    CS.current_path,
                ))
                CS.last_not_none()

                # Get old_bf
                CS.store(m_bridge_file.view(
                    ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                    gp.Old_VID,
                    CS.ref(m_file_name.fnid),
                    None,
                    None,
                    CS.current_path,
                ))
                CS.last_not_none()

        elif CS.file_operation and CS.file_operation[0] == "R":
            if CS.file_operation == "R100":
                # RENAME EXACT (content identical, reuse old fid)
                with CS(REF_OLD):
                    # Get old file_name
                    CS.store(m_file_name.get_set(
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                    # Get old_bf
                    CS.store(m_bridge_file.view(
                        ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                        gp.Old_VID,
                        CS.ref(m_file_name.fnid),
                        None,
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                # Get new file_name
                CS.store(m_file_name.get_set(
                    None,
                    CS.current_path,
                ))

                # Create BRIDGE FILE pointing to old fid
                CS.store(m_bridge_file.set(
                    gp.VID,
                    CS.ref(m_file_name.fnid),
                    CS.ref(m_bridge_file.fid, REF_OLD),
                ))

            else:
                # RENAME MODIFY (content modified, old fid ends and new fid starts)
                with CS(REF_OLD):
                    # Get old file_name
                    CS.store(m_file_name.get_set(
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                    # Get old_bf
                    CS.store(m_bridge_file.view(
                        ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                        gp.Old_VID,
                        CS.ref(m_file_name.fnid),
                        None,
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                # Get new file_name
                CS.store(m_file_name.get_set(
                    None,
                    CS.current_path,
                ))

                # Get FILE
                CS.store(m_file.set(
                    None,
                    getattr(CS, "blob_hash", b"\x00" * 20),
                    CS.get_file_type() if hasattr(CS, "get_file_type") else type_check(CS.current_path),
                ))

                # Create BRIDGE FILE
                CS.store(m_bridge_file.set(
                    gp.VID,
                    CS.ref(m_file_name.fnid),
                    CS.ref(m_file.fid),
                ))

                # Create MOVED FILE
                CS.store(m_moved_file.set(
                    CS.ref(m_bridge_file.fid, REF_OLD),
                    CS.ref(m_file.fid),
                ))

        elif CS.file_operation == "M":
            # MODIFY
            # Get file_name
            CS.store(m_file_name.get_set(
                None,
                CS.current_path,
            ))
            CS.last_not_none()

            with CS(REF_OLD):
                # Get old_bf
                CS.store(m_bridge_file.view(
                    ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                    gp.Old_VID,
                    CS.ref(m_file_name.fnid, REF_ROOT),
                    None,
                    None,
                    CS.current_path,
                ))
                CS.last_not_none()

            # 1 Create FILE
            CS.store(m_file.set(
                None,
                getattr(CS, "blob_hash", b"\x00" * 20),
                CS.get_file_type() if hasattr(CS, "get_file_type") else type_check(CS.current_path),
            ))

            # 2 Create BRIDGE FILE
            CS.store(m_bridge_file.set(
                gp.VID,
                CS.ref(m_file_name.fnid),
                CS.ref(m_file.fid),
            ))

    except CONTINUE_EXCEPTION:
        logger.error(f"CONTINUE_EXCEPTION:'{CS.file_operation}'={CS.current_path}")

    # If not yet processed
    if not CS.cs:
        # Add or other
        # 0 Check if FNAME exist/Create FNAME
        CS.store(m_file_name.get_set(
            None,
            CS.current_path,
        ))

        # 1 Create FILE
        CS.store(m_file.set(
            None,
            getattr(CS, "blob_hash", b"\x00" * 20),
            CS.get_file_type() if hasattr(CS, "get_file_type") else type_check(CS.current_path),
        ))

        # 2 Create BRIDGE FILE
        CS.store(m_bridge_file.set(
            gp.VID,
            CS.ref(m_file_name.fnid),
            CS.ref(m_file.fid),
        ))
    return

def _git_commit_worker(old_ver: str | int, ver: str, out_q: multiprocessing.Queue, repo_dir: str | None = None) -> None:
    """Worker process that parses git commits and diff hunks in parallel with file AST parsing."""
    try:
        from parser.git_ast import GitCommitParser
        git_parser = GitCommitParser(repo_dir=repo_dir)
        commits, file_hunks_map = git_parser.parse_version_commits_with_hunks(old_ver, ver)
        out_q.put((commits, file_hunks_map))
    except Exception as e:
        logger.error(f"Git commit worker error: {e}", exc_info=True)
        out_q.put((None, None))


def file_processing_worker(
    task_queue: multiprocessing.Queue,
    error_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    vid: int,
    gp_ref: GreatProcessor,
    mf_ref: MasterFile,
    worker_id: int = 0,
    resolved_symbols_map: dict[tuple[str, int], int] | None = None,
    resolved_names_map: dict[str, int] | None = None,
    allow_mid_epoch_recycle: bool = False,
) -> None:
    """Worker process that continuously pulls and parses batches of files from `task_queue`."""
    sys.setrecursionlimit(50000)
    # Detach worker gp from large parent-inherited caches to prevent Copy-on-Write page duplication
    if gp_ref is not None:
        import copy
        gp_ref = copy.copy(gp_ref)
        gp_ref.ChangeSet_Dict = {}
        gp_ref._ast_staged_symbols = {}
        gp_ref._ast_staged_names = {}
        gp_ref.unchanged_symbol_cache = {}
        gp_ref.file_symbols = {}
        gp_ref.file_names = {}

    # Ensure dedicated DB connection per worker process to avoid socket sharing across fork
    try:
        G.TE.start_new_db(G.DB, is_worker=True)
    except Exception as e:
        logger.error(f"Worker {worker_id} failed to initialize DB connection: {e}")

    # Determine effective symbol mapping: shared mmap if available, else passed dict
    if resolved_symbols_map is None and getattr(gp_ref, "_shared_symbols", None) is not None:
        effective_symbols = gp_ref._shared_symbols.symbols
        effective_names = gp_ref._shared_symbols.names
    else:
        effective_symbols = resolved_symbols_map
        effective_names = resolved_names_map

    # Worker-side serialization double-buffering pipeline
    worker_serialize_queue: queue.Queue = queue.Queue(maxsize=2)

    def _serializer_loop() -> None:
        while True:
            item = worker_serialize_queue.get()
            if item is None:
                break
            b_id, b_dict = item
            if b_dict:
                try:
                    raw_bytes = pickle.dumps(b_dict, protocol=pickle.HIGHEST_PROTOCOL)
                    if G.LOW_MEMORY_MODE or G.VERY_LOW_MEMORY_MODE:
                        payload = zlib.compress(raw_bytes, level=1)
                    else:
                        payload = raw_bytes
                    result_queue.put((b_id, payload))
                    del raw_bytes
                    del payload
                except Exception as e:
                    logger.error(COLOR.red(f"Worker {worker_id} failed to serialize batch {b_id}: {e}"))
                    error_queue.put((f"Batch-{b_id}", str(e), traceback.format_exc()))
                    result_queue.put((b_id, None))
            else:
                result_queue.put((b_id, None))
            del b_dict

    serializer_thread = threading.Thread(target=_serializer_loop, daemon=True)
    serializer_thread.start()

    is_recycling = False
    processed_batches = 0
    max_worker_batches = 5 if not G.VERY_LOW_MEMORY_MODE else 3  # Recycle worker after 5 batches (~1000 files) to prevent persistent Clang C-heap fragmentation

    try:
        while True:
            try:
                task = task_queue.get()
            except Exception as e:
                logger.error(f"Worker {worker_id} failed to get batch from queue: {e}")
                break

            if task is None:
                # Sentinel received, gracefully exit
                break

            batch_id, changed_files = task
            batch_cs_dict = {}

            for f_idx, changed_file in enumerate(changed_files):
                try:
                    CS = ChangeSet(changed_file)
                    CS.current_vid = vid
                    CS.gp = gp_ref
                    CS.mf = mf_ref
                    CS.batch_cs_dict = batch_cs_dict
                    if hasattr(gp_ref, "file_blob_hashes") and CS.current_path in gp_ref.file_blob_hashes:
                        CS.blob_hash = gp_ref.file_blob_hashes[CS.current_path]
                    G.CURRENT_PARSING_FILE = CS.current_path

                    default_processing(CS)
                    CS.parse()

                    # Pre-unpack intra-file AST view schemas in parallel workers to distribute CPU load
                    if not G.VERY_LOW_MEMORY_MODE:
                        CS.preprocess_ref_views()

                    # Pre-resolve foreign references against resolved header symbols across worker cores
                    if effective_symbols:
                        CS.pre_resolve_operations(effective_symbols, effective_names)

                    # In normal memory mode, pre-resolve unchanged DB dependencies to prune foreign_deps
                    if not G.LOW_MEMORY_MODE and not G.VERY_LOW_MEMORY_MODE:
                        CS.prune_unchanged_dependencies()

                    # Clean bloat and store in batch dict
                    CS.clear_bloat()
                    batch_cs_dict[CS.current_path] = CS
                except Exception as e:
                    err_str = str(e)
                    tb_str = traceback.format_exc()
                    logger.error(COLOR.red(f"Worker {worker_id} error parsing '{changed_file}': {err_str}"))
                    error_queue.put((changed_file, err_str, tb_str))
                finally:
                    G.CURRENT_PARSING_FILE = None

                # Periodic memory reclamation in low/very-low memory modes
                if (G.LOW_MEMORY_MODE or G.VERY_LOW_MEMORY_MODE) and (f_idx + 1) % 25 == 0:
                    reclaim_system_memory()

            worker_serialize_queue.put((batch_id, batch_cs_dict))
            del batch_cs_dict
            reclaim_system_memory()

            processed_batches += 1
            if allow_mid_epoch_recycle and processed_batches >= max_worker_batches and not task_queue.empty():
                is_recycling = True
                result_queue.put(("__WORKER_RECYCLE__", worker_id))
                break
    finally:
        worker_serialize_queue.put(None)
        serializer_thread.join()
        if getattr(G.TE, "db", None) is not None:
            try:
                G.TE.db.close()
            except Exception:
                pass
            G.TE.db = None
        reclaim_system_memory()
        if not is_recycling:
            result_queue.put(None)  # Worker done sentinel
    return


def file_processing(start: int, end: int | None, override_list: list[str] | None = None) -> None:
    """Process gp.Change_List (or override_list) and send CS into gp.ChangeSet_Dict."""
    if override_list:
        changed_files = override_list
    elif end is None:
        changed_files = gp.Change_List[start:]
    else:
        changed_files = gp.Change_List[start:end]

    for changed_file in changed_files:
        try:
            CS = ChangeSet(changed_file)
            CS.current_vid = gp.VID
            CS.gp = gp
            CS.mf = MF
            G.CURRENT_PARSING_FILE = CS.current_path

            default_processing(CS)
            CS.parse()

            # Store Set
            CS.clear_bloat()
            gp.ChangeSet_Dict[CS.current_path] = CS
        except Exception as e:
            logger.error(COLOR.red(f"Error processing file '{changed_file}': {e}\n{traceback.format_exc()}"))
        finally:
            G.CURRENT_PARSING_FILE = None

    if override_list is None:
        gp.push_set_to_main()

    return


def processing_unchanges() -> None:
    """Process everything outside gp.Change_List with self-healing for legacy unparsed files."""
    if gp.Old_VID == 0:
        return
    full_set = set(MF.git_file_list(gp.Version_Name).splitlines())

    changed_set = set()
    deleted_set = set()

    for item in gp.Change_List:
        if not item:
            continue
        parts = item.split("\t")
        op = parts[0]
        if op.startswith("D"):
            deleted_set.add(parts[-1])
        elif op.startswith("R"):
            if len(parts) >= 3:
                deleted_set.add(parts[1])
                changed_set.add(parts[2])
            else:
                changed_set.add(parts[-1])
        else:
            changed_set.add(parts[-1])

    unchanged_set = full_set - changed_set

    old_full_set = set(MF.git_file_list(gp.Old_Version_Name).splitlines())
    forgotten_delete = (old_full_set - full_set) - deleted_set

    if forgotten_delete:
        logger.warning("There seems to be forgotten deletes... Processing...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            logger.debug(forgotten_delete)
        file_processing(0, 0, (f"D\t{x}" for x in forgotten_delete))

    forgotten_new = (full_set - old_full_set) - changed_set
    if forgotten_new:
        logger.warning(f"Found {len(forgotten_new)} forgotten new files... Processing...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            logger.debug(forgotten_new)
        file_processing(0, 0, (f"A\t{x}" for x in forgotten_new))

    te_set = G.TE.set
    t_id = m_bridge_file.table_id
    vid = gp.VID
    old_vid = gp.Old_VID

    working_dir = MF.version_dict.get(gp.Version_Name)
    missing_unchanged = []
    for unchanged in unchanged_set:
        un_m_file_name = m_file_name.get(None, unchanged)
        if un_m_file_name is None:
            missing_unchanged.append(unchanged)
            continue
        fnid = un_m_file_name[2][0]

        # Guard against duplicate primary key insertion if already checkpointed
        if m_bridge_file.get(vid, fnid, None) is not None:
            continue

        un_m_bridge_file = m_bridge_file.get(old_vid, fnid, None)

        if un_m_bridge_file is None:
            missing_unchanged.append(unchanged)
            continue

        # If unchanged is a symlink, defer resolution to processing_symlinks so target fid stays in sync
        if working_dir and os.path.islink(os.path.join(working_dir, unchanged)):
            if not hasattr(gp, "Symlink_List") or gp.Symlink_List is None:
                gp.Symlink_List = []
            gp.Symlink_List.append(f"U\t{unchanged}")
            continue

        te_set(
            t_id,
            (
                vid,
                fnid,
                un_m_bridge_file[2][2],
            ),
        )
        file_fid_cache[unchanged] = un_m_bridge_file[2][2]

    if missing_unchanged:
        missing_syms = []
        missing_regs = []
        for x in missing_unchanged:
            if working_dir and os.path.islink(os.path.join(working_dir, x)):
                missing_syms.append(f"A\t{x}")
            else:
                missing_regs.append(f"A\t{x}")

        if missing_regs:
            logger.warning(
                f"Found {len(missing_regs)} files in unchanged_set missing from prior DB version (Old_VID {old_vid}). "
                f"Self-healing by processing as new additions in VID {vid}..."
            )
            file_processing(0, 0, missing_regs)

        if missing_syms:
            if not hasattr(gp, "Symlink_List") or gp.Symlink_List is None:
                gp.Symlink_List = []
            gp.Symlink_List.extend(missing_syms)

    return


def processing_dirs() -> None:  # noqa: C901
    """Process dirs."""
    dir_list = set(MF.get_dir_list(gp.Version_Name))

    if gp.Old_VID != 0:

        old_dir_list = set(MF.get_dir_list(gp.Old_Version_Name))
        new_dir_list = dir_list - old_dir_list
        unchanged_dirs = dir_list & old_dir_list
        deleted_dirs = old_dir_list - dir_list

        # Unchanged dirs
        for single_dir in unchanged_dirs:
            # Get m_file_name
            un_m_file_name = m_file_name.get(None, single_dir)
            if un_m_file_name is None:
                new_dir_list.add(single_dir)
                logger.warning(f"Unchanged dir missing m_file_name: '{single_dir}', self-healing as new addition...")
                continue
            dir_fnid = un_m_file_name[2][0]
            if m_bridge_file.get(gp.VID, dir_fnid, None) is not None:
                continue
            # Get old_m_bridge_file
            old_m_bridge_file = m_bridge_file.get(gp.Old_VID, dir_fnid, None)
            if old_m_bridge_file is None:
                new_dir_list.add(single_dir)
                logger.warning(
                    f"Unchanged dir '{single_dir}' missing prior m_bridge_file (Old_VID {gp.Old_VID}). "
                    f"Self-healing as new addition in VID {gp.VID}..."
                )
                continue
            G.TE.set(
                m_bridge_file.table_id,
                (
                    gp.VID,
                    dir_fnid,
                    old_m_bridge_file[2][2],
                ),
            )

        # New dirs
        for single_dir in new_dir_list:
            CS = ChangeSet("A", single_dir)
            # 0 Check if FNAME exist/Create FNAME
            CS.store(m_file_name.get_set(None, single_dir))
            # 1 Create FILE
            CS.store(m_file.set(None, b"\x00" * 20, T_DIR))
            # 2 Create BRIDGE FILE
            CS.store(m_bridge_file.set(
                gp.VID,
                CS.ref(m_file_name.fnid),
                CS.ref(m_file.fid),
            ))
            gp.ChangeSet_Dict[single_dir] = CS

    else:
        # If VID = 1, we need all dirs to be added
        for single_dir in dir_list:
            CS = ChangeSet("A", single_dir)
            # 0 Check if FNAME exist/Create FNAME
            CS.store(m_file_name.get_set(None, single_dir))
            # 1 Create FILE
            CS.store(m_file.set(None, b"\x00" * 20, T_DIR))
            # 2 Create BRIDGE FILE
            CS.store(m_bridge_file.set(
                gp.VID,
                CS.ref(m_file_name.fnid),
                CS.ref(m_file.fid),
            ))
            gp.ChangeSet_Dict[single_dir] = CS
    return


def processing_symlinks(override_symlinks: list[str] | None = None) -> None:
    """Resolve symbolic links by aliasing to target fid in m_bridge_file without duplicating AST tags."""
    symlink_list = override_symlinks if override_symlinks is not None else getattr(gp, "Symlink_List", None)
    if not symlink_list or gp.VID == 0:
        return

    working_dir = MF.version_dict.get(gp.Version_Name)
    if not working_dir:
        return

    te_set = G.TE.set
    t_id = m_bridge_file.table_id
    vid = gp.VID

    # Deduplicate paths while preserving order
    seen_paths = set()
    unique_symlinks = []
    for item in symlink_list:
        parts = item.split("\t")
        sym_path = parts[-1]
        if sym_path not in seen_paths:
            seen_paths.add(sym_path)
            unique_symlinks.append((parts[0], sym_path))

    fallback_files = []

    for op, symlink_path in unique_symlinks:
        full_p = os.path.join(working_dir, symlink_path)
        try:
            raw_target = os.readlink(full_p)
        except OSError:
            fallback_files.append(f"{op}\t{symlink_path}")
            continue

        # Normalize target relative to repository root
        norm_target = os.path.normpath(os.path.join(os.path.dirname(symlink_path), raw_target))
        if norm_target.startswith("./"):
            norm_target = norm_target[2:]

        # Lookup target fid in current version
        target_fid = get_fid_for_path(norm_target)

        if target_fid is not None:
            sym_fn_res = G.TE.set(m_file_name.table_id, (None, symlink_path))
            sym_fnid = sym_fn_res[0]
            te_set(t_id, (vid, sym_fnid, target_fid))
            file_fid_cache[symlink_path] = target_fid
            if hasattr(gp, "file_blob_ids") and isinstance(gp.file_blob_ids, dict):
                gp.file_blob_ids[symlink_path] = target_fid
            logger.info(f"Symlink aliased: '{symlink_path}' -> '{norm_target}' (fid {target_fid})")
        else:
            logger.info(
                f"Symlink target '{norm_target}' not found in active tree; falling back to raw processing for '{symlink_path}'"
            )
            fallback_op = "A" if op == "U" else op
            fallback_files.append(f"{fallback_op}\t{symlink_path}")

    if fallback_files:
        file_processing(0, 0, fallback_files)
        for fb in fallback_files:
            fb_path = fb.split("\t")[-1]
            if fb_path in gp.ChangeSet_Dict:
                cs_obj = gp.ChangeSet_Dict[fb_path]
                cs_obj.gp = gp
                # Runs after every file of the version executed: unresolved references fall back to stubs
                if not cs_obj.execute():
                    cs_obj.execute(force_stubs=True)
                extract_tags_and_evacuate_cs(cs_obj)

    if override_symlinks is None and hasattr(gp, "Symlink_List") and gp.Symlink_List:
        gp.Symlink_List.clear()


def processing_git_commits(version: str) -> None:
    """Parse git commits for active version, link contributors, and bridge tags to commits."""
    from parser.git_ast import GitCommitParser
    repo_path = getattr(gp, "repo_path", None) or MF.get_repo_dir()
    git_parser = GitCommitParser(repo_dir=repo_path)
    network_id = getattr(gp, "network_id", 1)

    commits = None
    file_hunks_map = None
    commit_queue = getattr(gp, "_commit_queue", None)
    commit_proc = getattr(gp, "_commit_proc", None)
    if commit_queue is not None:
        try:
            commits, file_hunks_map = commit_queue.get(timeout=120.0)
        except Exception as e:
            logger.warning(f"Failed to retrieve precomputed commits from background worker: {e}")
            commits, file_hunks_map = None, None
        finally:
            gp._commit_queue = None
            if commit_proc is not None:
                commit_proc.join(timeout=2.0)
                gp._commit_proc = None

    if commits is None:
        commits, file_hunks_map = git_parser.parse_version_commits_with_hunks(
            gp.Old_Version_Name, gp.Version_Name
        )
    if not commits:
        return

    logger.info(f"Processing {len(commits)} git commits for version '{version}'...")

    commit_hash_to_id = {}
    pending_parents: list[tuple[int, list[str]]] = []
    head_commit_id = None
    for commit in commits:
        if G.is_deadline_exceeded():
            logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during commit processing. Stopping early..."))
            break
        # Resolve author person_id
        author_res = G.TE.set(
            m_maintainer_person.table_id,
            (None, commit.author_name or commit.author_email, commit.author_email),
        )
        author_pid = author_res[0] if author_res else 1

        # Resolve committer person_id
        committer_res = G.TE.set(
            m_maintainer_person.table_id,
            (None, commit.committer_name or commit.committer_email, commit.committer_email),
        )
        committer_pid = committer_res[0] if committer_res else author_pid

        # Format commit_hash as 20 bytes
        c_hash = commit.commit_hash
        if isinstance(c_hash, str):
            c_hash_bytes = bytes.fromhex(c_hash) if len(c_hash) == 40 else c_hash.encode("ascii")
        else:
            c_hash_bytes = c_hash

        # Check if commit is already present in DB or TE cache
        c_match = G.TE.get(m_commit.table_id, (None, network_id, c_hash_bytes, None, None, None, None, None, None))
        if c_match:
            commit_id = c_match[0]
        else:
            # Insert commit (commit_id, network_id, commit_hash, author_id, author_date, committer_id, committer_date, subject, message)
            commit_res = G.TE.set(
                m_commit.table_id,
                (
                    None,
                    network_id,
                    c_hash_bytes,
                    author_pid,
                    commit.author_date,
                    committer_pid,
                    commit.committer_date,
                    commit.subject[:500],
                    commit.message,
                ),
            )
            commit_id = commit_res[0]
        commit.commit_id = commit_id
        commit_hash_to_id[commit.commit_hash] = commit_id
        if head_commit_id is None:
            head_commit_id = commit_id

        # Commit parents are linked after the loop: git log lists children before their parents
        if not c_match and hasattr(commit, "parents") and commit.parents:
            pending_parents.append((commit_id, commit.parents))

        # Insert contributors (Author, Committer, Co-developed-by, Signed-off-by, Reviewed-by, etc.)
        if not c_match:
            for contrib in commit.contributors:
                c_res = G.TE.set(
                    m_maintainer_person.table_id,
                    (None, contrib.name or contrib.email, contrib.email),
                )
                c_pid = c_res[0] if c_res else 1
                G.TE.set(
                    m_bridge_commit_person.table_id,
                    (
                        commit_id,
                        c_pid,
                        int(contrib.role),
                        int(contrib.priority),
                    ),
                )

        # Insert modified file bridges into m_commit_file
        for change_type, file_path in commit.files:
            fnid = get_fnid_for_path(file_path)
            if fnid is not None:
                blob_id = 0
                if hasattr(gp, "file_blob_ids") and file_path in gp.file_blob_ids:
                    blob_id = gp.file_blob_ids[file_path]
                elif getattr(gp, "ChangeSet_Dict", None):
                    cs_obj = gp.ChangeSet_Dict.get(file_path)
                    blob_id = getattr(cs_obj, "blob_id", 0) if cs_obj else 0
                G.TE.set(
                    m_commit_file.table_id,
                    (
                        commit_id,
                        fnid,
                        blob_id,
                        0,
                        0,
                        change_type[:1],
                    ),
                )

    # Link parents now that every commit of the range has an id; parents outside the range are looked up
    for commit_id, parent_hashes in pending_parents:
        for p_idx, p_hash in enumerate(parent_hashes):
            p_cid = commit_hash_to_id.get(p_hash)
            if p_cid is None and len(p_hash) == 40:
                p_match = G.TE.get(m_commit.table_id, (None, network_id, bytes.fromhex(p_hash), None, None, None, None, None, None))
                p_cid = p_match[0] if p_match else None
            if p_cid is not None:
                G.TE.set(m_commit_parent.table_id, (commit_id, p_cid, p_idx))

    # Link tags to commits for all changed files and evacuate executed ChangeSet memory
    # Use gp.file_pre_extracted_tags and gp.file_blob_ids directly to avoid decompressing 70,000 ChangeSets!
    if hasattr(gp, "file_pre_extracted_tags") and gp.file_pre_extracted_tags:
        for c_path, file_tags in gp.file_pre_extracted_tags.items():
            blob_id = gp.file_blob_ids.get(c_path, 0)
            if blob_id == 0:
                continue
            if file_tags:
                tag_bridges = git_parser.map_tags_to_commits(
                    file_tags,
                    c_path,
                    commit_hash_to_id=commit_hash_to_id,
                    file_hunks_map=file_hunks_map,
                )
                for cid, b_id, tid in tag_bridges:
                    G.TE.set(
                        m_bridge_commit_tag.table_id,
                        (
                            cid,
                            b_id,
                            tid,
                        ),
                    )
    else:
        for file_path, cs_obj in list(gp.ChangeSet_Dict.items()):
            if not cs_obj or not getattr(cs_obj, "current_path", None):
                continue
            c_path = cs_obj.current_path
            blob_id = getattr(cs_obj, "blob_id", 0)
            if blob_id == 0:
                continue

            # Collect tags for this file from ChangeSet operations or pre-extracted tags
            file_tags = getattr(cs_obj, "pre_extracted_tags", None)
            if file_tags is None:
                file_tags = []
                for op in getattr(cs_obj, "cs", []):
                    if op and len(op) >= 3 and op[0] == m_bridge_tag.table_id:
                        cols = op[2]
                        if len(cols) >= 4:
                            tag_id = cols[1] if not isinstance(cols[1], tuple) else None
                            line_s = cols[2] if isinstance(cols[2], int) else 1
                            line_e = cols[3] if isinstance(cols[3], int) else line_s
                            if tag_id is not None:
                                file_tags.append((tag_id, blob_id, line_s, line_e))

            if file_tags:
                tag_bridges = git_parser.map_tags_to_commits(
                    file_tags,
                    c_path,
                    commit_hash_to_id=commit_hash_to_id,
                    file_hunks_map=file_hunks_map,
                )
                for cid, b_id, tid in tag_bridges:
                    G.TE.set(
                        m_bridge_commit_tag.table_id,
                        (
                            cid,
                            b_id,
                            tid,
                        ),
                    )

            # Evacuate internal AST memory from executed ChangeSet
            if hasattr(cs_obj, "cs") and isinstance(cs_obj.cs, list):
                cs_obj.cs.clear()
            if hasattr(cs_obj, "store_dict") and isinstance(cs_obj.store_dict, dict):
                cs_obj.store_dict.clear()
            if hasattr(cs_obj, "cs_result") and isinstance(cs_obj.cs_result, list):
                cs_obj.cs_result.clear()

    # Stage m_bridge_checkpoint for milestone release version
    checkpoint_cid = gp.VID if (gp.VID and gp.VID != 0) else head_commit_id
    if checkpoint_cid is not None and checkpoint_cid != 0:
        if hasattr(gp, "file_blob_ids") and gp.file_blob_ids:
            blob_items = list(gp.file_blob_ids.items())
        elif getattr(gp, "ChangeSet_Dict", None):
            blob_items = [(f_path, getattr(cs_o, "blob_id", 0)) for f_path, cs_o in gp.ChangeSet_Dict.items()]
        else:
            blob_items = []

        if blob_items:
            existing_count = 0
            existing_fnids: set[int] = set()
            try:
                with G.DB() as db:
                    cur = db.cursor if hasattr(db, "cursor") else (db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None)
                    if cur:
                        cur.execute("SELECT COUNT(*) FROM m_bridge_checkpoint WHERE commit_id = %s", (checkpoint_cid,))
                        row = cur.fetchone()
                        existing_count = row[0] if (row and len(row) > 0) else 0
                        if 0 < existing_count < len(blob_items):
                            cur.execute("SELECT fnid FROM m_bridge_checkpoint WHERE commit_id = %s", (checkpoint_cid,))
                            existing_fnids = {r[0] for r in cur.fetchall()}
                        cur.close()
            except Exception as e:
                logger.debug(f"Note on checking m_bridge_checkpoint existing rows: {e}")

            if existing_count >= len(blob_items) and existing_count > 0:
                logger.info(f"Milestone checkpoint for commit {checkpoint_cid} is already fully populated ({existing_count} files); skipping.")
            else:
                staged_count = 0
                for f_path, b_id in blob_items:
                    if b_id != 0:
                        f_nid = get_fnid_for_path(f_path)
                        if f_nid is not None and f_nid not in existing_fnids:
                            existing_fnids.add(f_nid)
                            G.TE.set(
                                m_bridge_checkpoint.table_id,
                                (
                                    checkpoint_cid,
                                    f_nid,
                                    b_id,
                                ),
                            )
                            staged_count += 1
                if staged_count > 0:
                    logger.info(f"Staged {staged_count} milestone snapshot files into m_bridge_checkpoint for commit {checkpoint_cid}.")


def processing_kbuild(
    version: str,
    staged_file_refs: set[tuple[int, int, int, int]] | None = None,
    valid_blob_ids: set[int] | None = None,
    valid_fnids: set[int] | None = None,
) -> None:
    """Parse Makefile and Kbuild files for active version, map Kconfig symbols, and stage build references."""
    if gp.VID == 0:
        return

    if staged_file_refs is None:
        staged_file_refs = set()

    from parser.kbuild_parser import KbuildParser
    kbuild_parser = KbuildParser()

    try:
        file_list_raw = MF.git_file_list(gp.Version_Name)
    except Exception as e:
        logger.debug(f"Failed to get git_file_list for kbuild parsing in {version}: {e}")
        return

    all_files = file_list_raw.splitlines() if file_list_raw else []
    makefile_paths = [
        f for f in all_files
        if f.endswith("Makefile") or f.endswith("Kbuild") or f.endswith("/Makefile") or f.endswith("/Kbuild")
    ]
    if not makefile_paths:
        return

    logger.info(f"Processing {len(makefile_paths)} Makefile/Kbuild files for version '{version}'...")

    kcid_cache: dict[str, int] = {}

    def get_kcid_for_sym(sym: str) -> int:
        if not sym or sym in ("y", "m"):
            return 0
        clean_sym = sym[7:] if sym.startswith("CONFIG_") else sym
        if clean_sym in kcid_cache:
            return kcid_cache[clean_sym]
        sym_row = m_kconfig_symbol.get(None, clean_sym, None, None, None, None, None)
        if sym_row and len(sym_row) >= 3 and sym_row[2]:
            kcid = sym_row[2][0]
            kcid_cache[clean_sym] = kcid
            return kcid
        kcid_cache[clean_sym] = 0
        return 0

    count = 0
    ref_count = 0
    for mk_path in makefile_paths:
        if G.is_deadline_exceeded():
            logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during Kbuild processing. Stopping early..."))
            break
        try:
            content = MF.get_file(mk_path, gp.Version_Name)
            if not content:
                continue
            mk_fid = get_fid_for_path(mk_path)
            dir_path = os.path.dirname(mk_path)
            bindings = kbuild_parser.parse_makefile_content(content, dir_path=dir_path)
            for b in bindings:
                target_fnid = get_fnid_for_path(b.source_file_rel)
                fid = get_fid_for_path(b.source_file_rel)
                if fid is None and b.source_file_rel.endswith(".c"):
                    alt_asm = b.source_file_rel[:-2] + ".S"
                    fid = get_fid_for_path(alt_asm)
                    if target_fnid is None:
                        target_fnid = get_fnid_for_path(alt_asm)

                # Validate foreign keys before staging
                if valid_fnids is not None and target_fnid is not None and target_fnid not in valid_fnids:
                    target_fnid = None
                mk_blob_id = mk_fid if (mk_fid and (valid_blob_ids is None or mk_fid in valid_blob_ids)) else 0

                # 1. Populate m_kconfig_kbuild for compiled source files
                if getattr(b, "ref_type", 3) == 3 and target_fnid is not None:
                    kcid = get_kcid_for_sym(b.symbol_name)
                    G.TE.set(
                        m_kconfig_kbuild.table_id,
                        (
                            kcid,
                            mk_blob_id,
                            target_fnid,
                            int(b.compile_mode),
                            b.target_obj[:64],
                        ),
                    )
                    count += 1

                # 2. Populate m_file_reference for Kbuild compilations and Makefile includes/recursions
                if mk_fid and target_fnid is not None:
                    if valid_blob_ids is not None and mk_fid not in valid_blob_ids:
                        continue
                    ref_t = int(getattr(b, "ref_type", 3))
                    source_line = int(getattr(b, "source_line", 1))
                    ref_key = (mk_fid, target_fnid, ref_t, source_line)
                    if ref_key not in staged_file_refs:
                        staged_file_refs.add(ref_key)
                        G.TE.set(
                            m_file_reference.table_id,
                            (
                                None,
                                mk_fid,
                                target_fnid,
                                ref_t,
                                source_line,
                                getattr(b, "details", "")[:255],
                            ),
                        )
                        ref_count += 1
        except Exception as e:
            logger.debug(f"Error parsing kbuild file '{mk_path}': {e}")

    logger.info(f"Staged {count} Kbuild symbol-to-source mappings and {ref_count} build references for version '{version}'.")


def processing_doc_references(
    version: str,
    staged_file_refs: set[tuple[int, int, int, int]] | None = None,
    valid_blob_ids: set[int] | None = None,
    valid_fnids: set[int] | None = None,
) -> None:
    """Scan documentation and text files for active version and map cross-file references into m_file_reference."""
    if gp.VID == 0:
        return

    if staged_file_refs is None:
        staged_file_refs = set()

    from parser.doc_parser import DocReferenceScanner
    scanner = DocReferenceScanner()

    try:
        file_list_raw = MF.git_file_list(gp.Version_Name)
    except Exception as e:
        logger.debug(f"Failed to get git_file_list for doc reference parsing in {version}: {e}")
        return

    all_files = file_list_raw.splitlines() if file_list_raw else []
    doc_paths = [f for f in all_files if scanner.is_doc_candidate(f)]
    if not doc_paths:
        return

    logger.info(f"Processing {len(doc_paths)} documentation/text files for version '{version}'...")

    fnid_by_path: dict[str, int] = {}
    for p in all_files:
        fnid = get_fnid_for_path(p)
        if fnid is not None:
            fnid_by_path[p] = fnid

    count = 0
    for doc_p in doc_paths:
        if G.is_deadline_exceeded():
            logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during doc references processing. Stopping early..."))
            break
        try:
            doc_fid = get_fid_for_path(doc_p)
            if doc_fid is None or (valid_blob_ids is not None and doc_fid not in valid_blob_ids):
                continue
            content = MF.get_file(doc_p, gp.Version_Name)
            if not content:
                continue
            refs = scanner.scan_file_content(content, doc_fid, fnid_by_path, source_path=doc_p)
            for r in refs:
                if valid_fnids is not None and r.target_fnid not in valid_fnids:
                    continue
                ref_key = (r.source_fid, r.target_fnid, int(FileRefType.Documentation), int(r.line_no))
                if ref_key not in staged_file_refs:
                    staged_file_refs.add(ref_key)
                    G.TE.set(
                        m_file_reference.table_id,
                        (
                            None,
                            r.source_fid,
                            r.target_fnid,
                            int(FileRefType.Documentation),
                            int(r.line_no),
                            r.details[:255],
                        ),
                    )
                    count += 1
        except Exception as e:
            logger.debug(f"Error parsing documentation references in '{doc_p}': {e}")

    logger.info(f"Staged {count} documentation cross-file references into m_file_reference for version '{version}'.")


def processing_include_references(version: str) -> None:
    """Batch populate C/ASM and Kconfig include references into m_file_reference using set-based query."""
    if gp.VID == 0:
        return
    try:
        with G.DB() as db:
            cur = db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None
            if cur is None:
                return

            # Idempotent cleanup for this version's includes & Kconfig sources
            cur.execute(
                """
                DELETE FROM m_file_reference
                WHERE source_blob_id IN (
                    SELECT blob_id FROM m_bridge_checkpoint WHERE commit_id = %s
                ) AND ref_type IN (%s, %s);
                """,
                (gp.VID, int(FileRefType.Include), int(FileRefType.Kconfig)),
            )

            sync_query = f"""
                INSERT IGNORE INTO m_file_reference (source_blob_id, target_fnid, ref_type, line_no, details)
                SELECT STRAIGHT_JOIN DISTINCT bt.blob_id, inc.fnid,
                       CASE WHEN a.type_id IN ({ASTT.Kconfig_Source.value}, {ASTT.Kconfig_Rsource.value}) THEN {int(FileRefType.Kconfig)} ELSE {int(FileRefType.Include)} END AS ref_type,
                       bt.line_s,
                       LEFT(a.name, 255)
                FROM m_bridge_checkpoint bf
                JOIN m_bridge_tag bt ON bf.blob_id = bt.blob_id
                JOIN m_tag t ON bt.tag_id = t.tag_id
                JOIN m_ast_include inc ON t.ast_id = inc.ast_id
                JOIN m_ast a ON inc.ast_id = a.ast_id
                WHERE bf.commit_id = %s;
            """
            cur.execute(sync_query, (gp.VID,))
            inserted = cur.rowcount
            db.cnx.commit()
            cur.close()
            logger.info(f"Synchronized {inserted} include/Kconfig references into m_file_reference for version '{version}'.")
    except Exception as e:
        logger.warning(f"Error in processing_include_references for {version}: {e}", exc_info=True)


def processing_file_references(version: str) -> None:
    """Extract and consolidate all cross-file references (Kbuild, Makefiles, Documentation, Includes, Kconfig)."""
    if gp.VID == 0:
        return

    # Ensure all parent tables (m_file_blob, m_file_name, m_bridge_checkpoint) are committed before referencing
    G.TE.commit_all(update_in_mem_indexes=False)

    # 1. Version-scoped clean reset for idempotency across pipeline re-runs
    try:
        with G.DB() as db:
            cur = db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None
            if cur:
                cur.execute(
                    """
                    DELETE FROM m_file_reference
                    WHERE source_blob_id IN (
                        SELECT blob_id FROM m_bridge_checkpoint WHERE commit_id = %s
                    );
                    """,
                    (gp.VID,),
                )
                db.cnx.commit()
                cur.close()
    except Exception as e:
        logger.debug(f"Note on clearing m_file_reference for {version}: {e}")

    # 2. Resynchronize TableEngine sequence next_id before staging fresh rows
    if hasattr(G.TE, "db") and G.TE.db:
        try:
            G.TE.next_id[m_file_reference.table_id] = G.TE.db.get_next_id(m_file_reference)
        except Exception:
            pass

    staged_file_refs: set[tuple[int, int, int, int]] = set()
    valid_blob_ids: set[int] = set()
    valid_fnids: set[int] = set()
    try:
        with G.DB() as db:
            cur = db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None
            if cur:
                cur.execute("SELECT blob_id FROM m_file_blob")
                valid_blob_ids = {int(r[0]) for r in cur.fetchall()}
                cur.execute("SELECT fnid FROM m_file_name")
                valid_fnids = {int(r[0]) for r in cur.fetchall()}
                cur.execute("SELECT source_blob_id, target_fnid, ref_type, line_no FROM m_file_reference")
                for r in cur.fetchall():
                    staged_file_refs.add((r[0], r[1], r[2], r[3]))
                cur.close()
    except Exception as e:
        logger.debug(f"Preloading existing m_file_reference keys and valid IDs: {e}")

    # 3. Parse Kbuild and Documentation files and stage into TableEngine
    processing_kbuild(version, staged_file_refs=staged_file_refs, valid_blob_ids=valid_blob_ids, valid_fnids=valid_fnids)
    processing_doc_references(version, staged_file_refs=staged_file_refs, valid_blob_ids=valid_blob_ids, valid_fnids=valid_fnids)

    # 4. Commit TableEngine queued rows immediately so they claim sequence IDs 1..N and empty the queue
    if m_file_reference.table_id in G.TE.queued_set and G.TE.queued_set[m_file_reference.table_id]:
        G.TE.commit(m_file_reference.table_id, update_in_mem_indexes=False)

    # 5. Populate C/ASM and Kconfig include references using set-based SQL (claims N+1..M via server AUTO_INCREMENT)
    processing_include_references(version)

    # 6. Resynchronize next_id so subsequent operations and commit_all have the accurate sequence boundary
    if hasattr(G.TE, "db") and G.TE.db:
        try:
            G.TE.next_id[m_file_reference.table_id] = G.TE.db.get_next_id(m_file_reference)
        except Exception:
            pass


def processing_maintainer_files(version: str) -> None:
    """Batch match active files for version against MAINTAINERS patterns and populate m_maintainer_file."""
    if gp.VID == 0:
        return

    try:
        raw_maintainers = MF.get_file("MAINTAINERS", gp.Version_Name)
    except Exception as e:
        logger.debug(f"MAINTAINERS file not found for version '{version}': {e}")
        return

    if not raw_maintainers:
        return

    from parser.maintainer_ast.maintainer_parser import MaintainerParser
    from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher

    sections = MaintainerParser(raw_maintainers).parse()
    if not sections:
        return

    matcher = MaintainerMatcher(sections)

    sec_id_cache: dict[str, int | None] = {}

    def get_sec_id_for_name(name: str) -> int | None:
        if name in sec_id_cache:
            return sec_id_cache[name]
        sec_row = m_maintainer_section.get(None, None, name, None, None, None, None, None)
        if sec_row and len(sec_row) >= 3 and sec_row[2]:
            sec_id = sec_row[2][0]
            sec_id_cache[name] = sec_id
            return sec_id
        sec_id_cache[name] = None
        return None

    for sec in sections:
        if sec.name and sec.name not in sec_id_cache:
            get_sec_id_for_name(sec.name)

    try:
        file_list_raw = MF.git_file_list(gp.Version_Name)
    except Exception as e:
        logger.debug(f"Failed to get git_file_list for maintainer matching in {version}: {e}")
        return

    all_files = file_list_raw.splitlines() if file_list_raw else []
    if not all_files:
        return

    logger.info(f"Matching {len(all_files)} files against {len(sections)} maintainer sections for version '{version}'...")

    maintainers_blob_id = get_fid_for_path("MAINTAINERS") or 1
    matched_count = 0
    seen_pairs: set[tuple[int, int]] = set()
    for file_path in all_files:
        if G.is_deadline_exceeded():
            logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during maintainer matching. Stopping early..."))
            break
        matched_sections = matcher.match_file(file_path)
        if not matched_sections:
            continue
        fnid = get_fnid_for_path(file_path)
        if fnid is None:
            continue
        for sec in matched_sections:
            sec_id = get_sec_id_for_name(sec.name)
            if sec_id is not None:
                pair = (fnid, sec_id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                G.TE.set(
                    m_maintainer_file.table_id,
                    (
                        maintainers_blob_id,
                        fnid,
                        sec_id,
                    ),
                )
                matched_count += 1

    logger.info(f"Staged {matched_count} file-to-section mappings into m_maintainer_file for version '{version}'.")

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

if __name__ == "__main__":
    main()

