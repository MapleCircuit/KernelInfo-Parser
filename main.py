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
    REF_FILE,
    OP_REF,
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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import contextlib
import copy
import io
import math
import multiprocessing
from multiprocessing.connection import wait as mp_wait
import queue
import signal
import struct
import tempfile
import threading
import re
from collections import Counter, deque, defaultdict
import pickle
import zlib
import traceback
import gc
import ctypes
from parser.c_ast import c_ast_parse, Line
from core.FileHandler import MasterFile
from core.GreatProcessor import GreatProcessor
from core.TableHandling import Table, ChangeSet, is_data_unsafe
from table_engine.shared_buffer import SharedSymbolBuilder, SharedSymbolChain
from db_engine import MariaDB, MockDB, get_db_engine
from db_engine.DBHandling import parse_server_setting
from core.globalstuff import MEMORY_RECLAIM_STATS
from core.SymbolRows import STATS as SYMBOL_STATS
from core.SymbolRows import SYMBOL_VIEW_SQL
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

# Workers rely on fork inheritance (gp, MF, TableEngine caches, shared memory symbols), so pin the start
# method instead of following the platform default (forkserver from Python 3.14).
_MP = multiprocessing.get_context("fork")

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


def _legacy_symbol_tables(db: Any) -> list[str]:
    """Names of symbol views (core.SymbolRows.SYMBOL_VIEW_SQL) that still exist as tables of the per-blob layout."""
    if not (hasattr(db, "cnx") and db.cnx):
        return []
    names = tuple(SYMBOL_VIEW_SQL)
    with contextlib.closing(db.cnx.cursor()) as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() "
            f"AND table_type = 'BASE TABLE' AND table_name IN ({','.join(['%s'] * len(names))})",
            names,
        )
        return sorted(r[0].decode() if isinstance(r[0], (bytes, bytearray)) else r[0] for r in cur.fetchall())


def drop_legacy_symbol_tables(db: Any) -> None:
    """On --reset, drop the per-blob symbol tables of the earlier layout so their names can become views."""
    legacy = _legacy_symbol_tables(db)
    if legacy:
        with contextlib.closing(db.cnx.cursor()) as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0;")
            cur.execute(f"DROP TABLE IF EXISTS {', '.join(f'`{n}`' for n in legacy)};")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1;")
        db.cnx.commit()
        logger.info(f"Dropped tables of the per-blob symbol layout: {', '.join(legacy)}")


def ensure_db_views(db: Any) -> None:
    """Create or replace SQL compatibility views for legacy query interoperability.

    Raises:
        RuntimeError: The database still has `m_symbol_def` / `m_symbol_ref` tables from before symbol rows were
            stored once per tag; its rows cannot be combined with the new layout, so it must be rebuilt (--reset).
    """
    legacy = _legacy_symbol_tables(db)
    if legacy:
        raise RuntimeError(
            f"The database predates the shared symbol layout ({', '.join(legacy)} are tables, not views). "
            "Rebuild it with --reset (core/SCHEMA_PLAN_SYMBOL_ROWS.md)."
        )
    views = (
        ("m_v_main", "CREATE OR REPLACE VIEW m_v_main AS SELECT commit_id AS vid, ref_name AS vname, repo_id, ref_type, tagger_id, tagger_date, tag_message FROM m_git_ref;"),
        ("m_bridge_file", "CREATE OR REPLACE VIEW m_bridge_file AS SELECT commit_id AS vid, fnid, blob_id AS fid FROM m_bridge_checkpoint;"),
        ("m_file", "CREATE OR REPLACE VIEW m_file AS SELECT fb.blob_id AS fid, COALESCE((SELECT MIN(bc.commit_id) FROM m_bridge_checkpoint bc WHERE bc.blob_id = fb.blob_id), 0) AS vid_s, 0 AS vid_e, fb.ftype, '0' AS s_stat, '0' AS e_stat, fb.blob_hash FROM m_file_blob fb;"),
        ("m_bridge_commit_file", "CREATE OR REPLACE VIEW m_bridge_commit_file AS SELECT commit_id, fnid, blob_id, 0 AS old_fnid, 0 AS old_blob_id, change_type FROM m_commit_file;"),
        *SYMBOL_VIEW_SQL.items(),
    )
    for vname, ddl in views:
        try:
            if hasattr(db, "cnx") and db.cnx:
                with contextlib.closing(db.cnx.cursor()) as cur:
                    cur.execute(ddl)
                db.cnx.commit()
        except Exception as e:
            if vname in SYMBOL_VIEW_SQL:
                logger.warning(COLOR.yellow(f"ensure_db_views could not create view {vname}: {e}"))
            else:
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
    # ChangeSets executed straight from a worker batch (execute_and_purge) arrive with gp cleared
    gp_ref = getattr(cs_obj, "gp", None) or gp
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
        if gp_ref is not None and hasattr(gp_ref, "file_blob_ids") and isinstance(gp_ref.file_blob_ids, dict):
            gp_ref.file_blob_ids[c_path] = blob_id

    if hasattr(cs_obj, "pre_extracted_tags") and cs_obj.pre_extracted_tags:
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


_SQL_IN_CHUNK = 1000


def _person_key(name: str | None, email: str | None) -> tuple[str, str]:
    """Identity of an m_maintainer_person row: `(name, email)`, the TableEngine's no-duplicate key."""
    email = email or ""
    return (name or email or "Unknown", email)


def _raw_cursor(db: Any) -> Any | None:
    """A fresh cursor on a MariaDB connection, or None for drivers without SQL access (MockDB)."""
    cnx = getattr(db, "cnx", None)
    return cnx.cursor() if cnx else None


def _log_preload_summary(elapsed_s: float) -> None:
    """Log how long the TableEngine preload took and which cached tables are largest."""
    cached_rows = getattr(G.TE, "_cached_rows", None) or {}
    sizes = []
    for table_id, rows in cached_rows.items():
        try:
            sizes.append((len(rows), G.TE.tables[table_id].table_name))
        except Exception:
            continue
    largest = ", ".join(f"{name} {count:,}" for count, name in sorted(sizes, reverse=True)[:5])
    logger.info(f"[TableEngine] Preloaded cached tables in {elapsed_s:.1f}s (parent RSS {_parent_rss_mb():.0f}MB); largest: {largest}")


def _tagged_blob_ids(blob_ids: Iterable[int]) -> set[int]:
    """The subset of `blob_ids` that already has tags in m_bridge_tag (their parse results are complete)."""
    wanted = sorted(set(blob_ids))
    tagged: set[int] = set()
    if not wanted:
        return tagged
    with G.DB() as db:
        cur = _raw_cursor(db)
        if cur is None:
            for blob_id in wanted:
                if m_bridge_tag.view_get_multiple(((m_bridge_tag.blob_id,),), blob_id, None, None, None, None, None):
                    tagged.add(blob_id)
            return tagged
        try:
            for i in range(0, len(wanted), _SQL_IN_CHUNK):
                chunk = wanted[i : i + _SQL_IN_CHUNK]
                cur.execute(
                    f"SELECT DISTINCT blob_id FROM m_bridge_tag WHERE blob_id IN ({','.join(['%s'] * len(chunk))})",  # noqa: S608
                    tuple(chunk),
                )
                tagged.update(int(r[0]) for r in cur.fetchall())
        finally:
            cur.close()
    return tagged


def _reuse_parsed_blobs(file_blob_hashes: dict[str, bytes]) -> list[str]:
    """Step 4.5: link changed files whose exact blob was already parsed instead of parsing them again.

    Blobs and file names are looked up in the TableEngine caches (preloaded by `G.TE.start()`), tagged
    blobs are confirmed with one targeted query, and the reused files' m_file_name, m_bridge_checkpoint
    and m_commit_file rows are staged through the TableEngine. Reused paths are recorded in
    `gp.deduped_paths` so `processing_git_commits()` can still link their tags to this range's commits.

    Returns:
        The change lines that still need parsing, in `gp.Change_List` order.
    """
    candidates: dict[str, tuple[str, str, int]] = {}
    for item in gp.Change_List or ():
        if not item:
            continue
        parts = item.split("\t")
        op, fpath = parts[0], parts[-1]
        if op.startswith("D"):
            continue
        b_hash = file_blob_hashes.get(fpath)
        if not b_hash:
            continue
        blob_row = G.TE.get(m_file_blob.table_id, (None, b_hash, type_check(fpath)))
        if blob_row is not None:
            candidates[item] = (op, fpath, blob_row[0])

    tagged = _tagged_blob_ids(b_id for _op, _path, b_id in candidates.values())
    # Checkpoint rows of this version from an interrupted earlier run (one query instead of one per file)
    checkpointed: set[int] | None = None
    if tagged:
        with G.DB() as db:
            cur = _raw_cursor(db)
            if cur is not None:
                try:
                    cur.execute("SELECT fnid FROM m_bridge_checkpoint WHERE commit_id = %s", (gp.VID,))
                    checkpointed = {int(r[0]) for r in cur.fetchall()}
                finally:
                    cur.close()
    reused: set[str] = set()
    gp.deduped_paths = set()
    for item, (op, fpath, b_id) in candidates.items():
        if b_id not in tagged:
            continue
        fnid = G.TE.set(m_file_name.table_id, (None, fpath))[0]
        if checkpointed is not None:
            is_checkpointed = fnid in checkpointed
            checkpointed.add(fnid)
        else:
            is_checkpointed = G.TE.get(m_bridge_checkpoint.table_id, (gp.VID, fnid, None)) is not None
        if not is_checkpointed:
            G.TE.set(m_bridge_checkpoint.table_id, (gp.VID, fnid, b_id))
        G.TE.set(m_commit_file.table_id, (gp.VID, fnid, b_id, 0, 0, op[:1]))
        gp.file_blob_ids[fpath] = b_id
        file_fid_cache[fpath] = b_id
        gp.deduped_paths.add(fpath)
        reused.add(item)

    if reused:
        logger.info(COLOR.green(f"Content-Addressable Deduplication: Reused {len(reused)} existing file blobs directly without worker parsing."))
    return [item for item in (gp.Change_List or ()) if item not in reused]


def update(version: str, repo_path: str | None = None) -> None:
    """Execute the full version parsing and database ingestion pipeline for a target release version.
    
    Workflow Steps:
    1. Register/synchronize version in `m_git_ref` via `create_new_vid()`.
    2. Build temporary performance B-tree indexes (`ast_index`, `file_name_index`).
    3. Clone repository branch into RAMDISK via `MF.add_version()`.
    4. Generate git diff change list (`MF.generate_change_list()`) & extract Git blob hashes.
    4.4 Start the TableEngine (preload cached tables).
    4.5 Deduplicate files against `m_file_blob` across trees sharing root commit (TableEngine caches).
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
        if ls_res.returncode != 0:
            # Without blob hashes every parsed file would share the all-zero hash (one m_file_blob row per type)
            raise RuntimeError(f"git ls-tree failed for '{version}': {ls_res.stderr.strip()}")
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

    # Parse this version's git commits in the background while blobs are deduplicated and files parsed;
    # forked before the TableEngine preload so the child shares as little parent memory as possible
    _start_git_commit_worker()

    # -------------------------------------------------------------------------
    # STEP 4.4: TableEngine preload (te_cached tables, shared huge-page buffer)
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 4.4: TableEngine Preload"):
        t_preload = time.perf_counter()
        G.TE.start(gp.Table_Array, G.DB)
        _log_preload_summary(time.perf_counter() - t_preload)

    # -------------------------------------------------------------------------
    # STEP 4.5: Content-Addressable Blob Deduplication across Git trees
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 4.5: Content-Addressable Blob Dedup", units="reused") as dedup_metric:
        gp.worker_change_list = _reuse_parsed_blobs(file_blob_hashes)
        if dedup_metric is not None:
            dedup_metric.count = len(gp.deduped_paths)

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Step 4.5. Finalizing '{version}' early."))
        G.TE.commit_all(update_in_mem_indexes=False)
        G.TE.close()
        return

    # -------------------------------------------------------------------------
    # STEP 5 & 6: Spawn multicore workers to parse files & stream ChangeSet execution
    # -------------------------------------------------------------------------
    chunk_commit_interval = 300 if G.VERY_LOW_MEMORY_MODE else (500 if G.LOW_MEMORY_MODE else 1000)
    scheduler = DependencyScheduler(gp, chunk_commit_interval=chunk_commit_interval, streaming=True)
    symbols_base = Counter(SYMBOL_STATS)
    WORKER_SYMBOL_STATS.clear()
    trigger_multicore(scheduler=scheduler)
    scheduler.finalize()
    _log_symbol_stats(version, symbols_base)

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
    # STEP 7: Commit the remaining staged rows (the indexes stay in place: the stages already
    # streamed nearly every row of the version into them, so a drop/rebuild would only add work)
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 7: Final DB Commit & Indexes"):
        if getattr(G.TE, "db", None) is not None and hasattr(G.TE.db, "cnx") and G.TE.db.cnx is not None:
            try:
                G.TE.db.cnx.commit()
            except Exception:
                pass

        G.TE.commit_all(update_in_mem_indexes=False)

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


_AST_SOURCE_EXTS = frozenset((
    ".c", ".cpp", ".cc", ".cxx", ".c++", ".cp",
    ".rs",
    ".s", ".asm",
    ".h", ".hpp", ".hxx",
))


def _is_ast_source_path(fpath: str) -> bool:
    """Return True if `fpath` is in a language parsed into a full AST (C/C++, Rust, assembly)."""
    return os.path.splitext(fpath)[1].lower() in _AST_SOURCE_EXTS


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

    if not _is_ast_source_path(fpath):
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
    largest_first: bool = True,
    num_workers: int = 0,
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
        largest_first: Order each epoch's batches by descending source bytes, so the heaviest batches
            start first and the epoch does not end waiting on one late heavy batch.
        num_workers: When given and the files fit in one epoch, the byte budget per batch is also capped at an
            equal share of the bytes (`_BATCHES_PER_WORKER` batches per worker), so batches cost about the same.

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

    sizes: list[int] = []
    for item in source_files:
        fpath = item.split("\t")[-1]
        sz = 0
        if working_dir:
            full_path = os.path.join(working_dir, fpath) if not os.path.isabs(fpath) else fpath
            try:
                sz = os.path.getsize(full_path)
            except Exception:
                sz = 0
        sizes.append(sz)
    single_epoch = len(source_files) <= int(target_epoch_files * 1.3)
    if num_workers > 0 and single_epoch:
        equal_share = math.ceil(sum(sizes) / (num_workers * _BATCHES_PER_WORKER))
        max_batch_bytes = min(max_batch_bytes, max(heavy_file_threshold, equal_share))

    batches: list[tuple[int, list[str]]] = []
    batch_bytes: dict[int, int] = {}
    current_batch: list[str] = []
    current_batch_bytes = 0
    current_has_heavy = False
    batch_counter = 0

    for item, sz in zip(source_files, sizes):
        fpath = item.split("\t")[-1]

        is_heavy = sz >= heavy_file_threshold and _is_ast_source_path(fpath)
        limit = heavy_batch_size if (current_has_heavy or is_heavy) else normal_batch_size

        if len(current_batch) >= limit or (current_batch_bytes + sz > max_batch_bytes and len(current_batch) > 0):
            if current_batch:
                batches.append((batch_counter, current_batch))
                batch_bytes[batch_counter] = current_batch_bytes
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
        batch_bytes[batch_counter] = current_batch_bytes
        batch_counter += 1

    def _ordered(epoch: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
        if not largest_first:
            return epoch
        return sorted(epoch, key=lambda batch: batch_bytes[batch[0]], reverse=True)

    if single_epoch:
        return [_ordered(batches)]

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
                epochs.append(_ordered(current_epoch))
                current_epoch = []
                current_epoch_files = 0

    if current_epoch:
        epochs.append(_ordered(current_epoch))

    return epochs


class DependencyScheduler:
    """Dependency-driven ChangeSet resolution and execution scheduler with two-stage streaming ingestion.

    A ChangeSet that references files of this update still being parsed waits on them: when a file
    completes, the ChangeSets waiting on it run right away, cascading. Most files therefore execute while
    the workers are still parsing, and the serial wave passes (`resolve_headers()`, `finalize()`) only
    handle dependency cycles and files nothing woke up.
    """

    def __init__(self, gp_ref: GreatProcessor, chunk_commit_interval: int = 0, streaming: bool = True) -> None:
        self.gp = gp_ref
        self.streaming = streaming
        # Blocked on something that will not complete during ingestion (retried at epoch boundaries / finalize)
        self.deferred_queue: deque[str] = deque()
        self.completed: set[str] = set()
        self.executed_count = 0
        self.chunk_commit_interval = chunk_commit_interval if chunk_commit_interval > 0 else 1000
        # In-flight file -> ChangeSets to run again once it completes
        self.waiters: defaultdict[str, set[str]] = defaultdict(set)
        # Header ChangeSets held until every in-flight file they reference has completed
        self.pending_deps: dict[str, set[str]] = {}
        self._ready: deque[str] = deque()

    def _in_flight(self, path: str) -> bool:
        """True if `path` belongs to this update but has not executed yet, so it will complete later."""
        if path in self.completed:
            return False
        return path in getattr(self.gp, "_changed_paths_set", ()) or path in self.gp.ChangeSet_Dict

    def _complete(self, path: str) -> None:
        """Record `path` as executed and queue the ChangeSets that were waiting only on it."""
        self.completed.add(path)
        for waiter in self.waiters.pop(path, ()):
            pending = self.pending_deps.get(waiter)
            if pending is not None:
                pending.discard(path)
                if pending:
                    continue
                del self.pending_deps[waiter]
            self._ready.append(waiter)

    def _store(self, path: str, cs_obj: ChangeSet) -> None:
        cs_obj.batch_cs_dict = None
        self.gp.ChangeSet_Dict[path] = cs_obj

    def _maybe_chunk_commit(self) -> None:
        if self.executed_count % self.chunk_commit_interval == 0:
            _commit_all_with_retry("Chunk")
            if hasattr(self.gp, "_ast_negative_cache"):
                self.gp._ast_negative_cache.clear()
            reclaim_system_memory()

    def _execute(self, path: str, cs_obj: ChangeSet, defer: bool) -> bool:
        """Execute a ChangeSet: on success evacuate it and wake its waiters, otherwise park it."""
        cs_obj.gp = self.gp
        if cs_obj.execute():
            self.executed_count += 1
            extract_tags_and_evacuate_cs(cs_obj)
            self._store(path, cs_obj)
            self._complete(path)
            self._maybe_chunk_commit()
            return True
        blocked_on = cs_obj.blocked_on
        if hasattr(cs_obj, "clear_bloat"):
            cs_obj.clear_bloat()
        else:
            cs_obj.gp = None
        self._store(path, cs_obj)
        if blocked_on and blocked_on != path and self._in_flight(blocked_on):
            self.waiters[blocked_on].add(path)
        elif defer:
            # Unresolvable for now: retried at epoch boundaries, stubbed by the final wave pass
            self.deferred_queue.append(path)
        return False

    def _drain_ready(self) -> None:
        """Run the ChangeSets woken by completed files, and in turn the ones they wake."""
        while self._ready:
            path = self._ready.popleft()
            if path in self.completed:
                continue
            cs_obj = self.gp.ChangeSet_Dict.get(path)
            if cs_obj is not None:
                self._execute(path, cs_obj, defer=True)

    def _wake_completed(self) -> None:
        """Wake waiters of files completed outside the scheduler (e.g. by the wave passes)."""
        for dep in [d for d in self.waiters if d in self.completed]:
            self._complete(dep)
        self._drain_ready()

    def ingest_batch(self, batch_dict: dict[str, ChangeSet], is_header_stage: bool = False) -> None:
        """Ingest newly arrived ChangeSets from a worker batch, executing every one whose dependencies allow."""
        # Collect worker micro-profiler metrics into StageProfiler if active
        if getattr(G, "STAGE_PROFILER", None) is not None:
            for cs_obj in batch_dict.values():
                if getattr(cs_obj, "profiler", None) is not None:
                    G.STAGE_PROFILER.record_worker_profiler(cs_obj.profiler)
                    cs_obj.profiler = None

        # 1. Extract lightweight foreign dependencies and sizes into gp before compression
        op_counts = getattr(self.gp, "cs_op_counts", None)
        for path, cs_obj in batch_dict.items():
            f_deps = getattr(cs_obj, "foreign_deps", None)
            self.gp.file_deps[path] = set(f_deps) if f_deps else set()
            if op_counts is not None:
                op_counts[path] = len(getattr(cs_obj, "cs", None) or ())

        purged = execute_and_purge(batch_dict)
        for cs_obj in purged.values():
            cs_obj.batch_cs_dict = purged

        for path, cs_obj in purged.items():
            if getattr(cs_obj, "cs_processed", False):
                # Executed by execute_and_purge (no foreign references)
                self._store(path, cs_obj)
                self._complete(path)
                continue
            if path in self.completed:
                self._store(path, cs_obj)
                continue
            if is_header_stage:
                # Hold headers until the in-flight files they reference have executed
                pending = {d for d in (getattr(cs_obj, "foreign_deps", None) or ()) if d != path and self._in_flight(d)}
                if pending:
                    self.pending_deps[path] = pending
                    for dep in pending:
                        self.waiters[dep].add(path)
                    self._store(path, cs_obj)
                    continue
            # Header failures stay for resolve_headers(); source failures are deferred
            self._execute(path, cs_obj, defer=not is_header_stage)
        self._drain_ready()

    def resolve_headers(self) -> None:
        """Resolve all remaining header ChangeSets in wave order, breaking circular deadlocks with stubs."""
        execute_phase2_parallel_waves(self.gp, chunk_commit_interval=self.chunk_commit_interval, completed_set=self.completed)
        self.completed.update(self.gp.ChangeSet_Dict.keys())
        self.pending_deps.clear()
        self._wake_completed()
        self.executed_count = len(self.completed)
        if hasattr(self.gp, "_ast_negative_cache"):
            self.gp._ast_negative_cache.clear()
        reclaim_system_memory()

    def drain_deferred_queue(self) -> int:
        """Retry deferred ChangeSets (without stubs) and any woken waiters.

        Returns:
            Number of newly executed ChangeSets.
        """
        executed_before = self.executed_count
        self._wake_completed()
        deferred, self.deferred_queue = self.deferred_queue, deque()
        for path in deferred:
            if path in self.completed:
                continue
            cs_obj = self.gp.ChangeSet_Dict.get(path)
            if cs_obj is not None:
                self._execute(path, cs_obj, defer=True)
        self._drain_ready()
        return self.executed_count - executed_before

    def finalize(self) -> None:
        """Finalize resolution of all remaining ChangeSets via Phase 2 wave-based execution."""
        # 1. First attempt non-stub execution for deferred queue items that may now be unblocked
        self.drain_deferred_queue()

        # 2. Every ChangeSet still waiting (cycles, files that never arrived) goes through the wave pass
        execute_phase2_parallel_waves(self.gp, chunk_commit_interval=self.chunk_commit_interval, completed_set=self.completed)
        self.completed.update(self.gp.ChangeSet_Dict.keys())
        self.waiters.clear()
        self.pending_deps.clear()
        self.executed_count = len(self.completed)
        if hasattr(self.gp, "_ast_negative_cache"):
            self.gp._ast_negative_cache.clear()
        G.TE.commit_all(update_in_mem_indexes=False)
        # Every ChangeSet has executed: the Stage 2 shared symbol segments are no longer read
        self.gp.close_shared_symbols()
        reclaim_system_memory()


def _stored_changeset_executed(cs_dict: Any, path: str) -> bool | None:
    """Whether the ChangeSet stored under `path` has fully executed (None if there is none).

    For a CompressedChangeSetDict the answer comes from where the entry lives, without decompressing it:
    executed ChangeSets are evacuated into `_evacuated_store`, and evacuated ones are never compressed.
    """
    evacuated_store = getattr(cs_dict, "_evacuated_store", None)
    if evacuated_store is not None:
        if path in evacuated_store:
            return True
        cs = cs_dict._lru_cache.get(path)
        if cs is None:
            return False if cs_dict._compressed_store.get(path) is not None else None
    else:
        cs = cs_dict.get(path)
        if cs is None:
            return None
    unresolved = getattr(cs, "unresolved_indices", None)
    return bool(getattr(cs, "cs_processed", False)) and not unresolved


def execute_phase2_parallel_waves(
    gp: GreatProcessor,
    chunk_commit_interval: int = 0,
    completed_set: set[str] | None = None,
) -> None:
    """Execute unexecuted ChangeSets in-process in dependency waves.

    Bounds memory by:
    1. Enforcing micro-chunk database commits every wave or 150-250 files to bound TableEngine heap buffers.
    2. Performing batch cycle breaking on strongly-connected circular dependency deadlocks.
    3. Immediately evacuating AST buffers upon execution.
    """
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

    # 1. Identify all remaining unexecuted paths without decompressing stored ChangeSets
    remaining: set[str] = set()
    for path in gp.file_deps.keys():
        if path in completed_set:
            continue
        executed = _stored_changeset_executed(gp.ChangeSet_Dict, path)
        if executed is None:
            continue
        if executed:
            completed_set.add(path)
        else:
            remaining.add(path)

    if not remaining:
        return

    logger.info(COLOR.cyan(f"[Phase 2] {len(remaining)} unexecuted ChangeSets entering wave resolution."))

    # 2. Build lightweight dependency DAG and inverted dependent mapping (~10 MB)
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
    op_counts = getattr(gp, "cs_op_counts", None) or {}
    # Files whose dependencies have all executed; filled as files complete (next wave)
    ready: set[str] = {p for p in remaining if not deps[p]}

    def _finish(path: str) -> None:
        remaining.discard(path)
        completed_set.add(path)
        deps.pop(path, None)
        for waiter in dependents.pop(path, ()):
            waiter_deps = deps.get(waiter)
            if waiter_deps is not None:
                waiter_deps.discard(path)
                if not waiter_deps and waiter in remaining:
                    ready.add(waiter)

    try:
        while remaining:
            if G.is_deadline_exceeded():
                logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during Wave Resolution. Halting early..."))
                break
            wave_num += 1
            t_wave_start = time.perf_counter()
            # Wave selection: files with no unexecuted dependencies in remaining (sorted for reproducible ids)
            wave = sorted(ready)
            ready.clear()

            if not wave:
                # Deadlock: force the file most others wait on (ties: fewest operations to stub, then path).
                # Chosen from the dependency counts alone: decompressing every candidate cost seconds per break.
                cand_path = max(remaining, key=lambda k: (len(dependents.get(k, ())), -op_counts.get(k, 0), k))
                logger.warning(COLOR.yellow(
                    f"[Phase 2] Breaking circular dependency deadlock: precision force-resolving 1 stub "
                    f"(candidate '{cand_path}' has {len(dependents.get(cand_path, ()))} dependents, {len(remaining)} files remaining)"
                ))

                cand_cs = gp.ChangeSet_Dict[cand_path]
                cand_cs.gp = gp
                cand_cs.execute(force_stubs=True)
                extract_tags_and_evacuate_cs(cand_cs)
                gp.ChangeSet_Dict[cand_path] = cand_cs
                _finish(cand_path)
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
                _finish(path)
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
        resolution_time = time.perf_counter() - t_phase2_start
        commit_time = 0.0
        if resolution_succeeded and executed_count > last_commit_count:
            logger.info(COLOR.yellow(
                f"[Phase 2] Final commit: committing batch at {executed_count} executed ChangeSets to database..."
            ))
            t_commit = time.perf_counter()
            G.TE.commit_all()
            if hasattr(gp, "_ast_negative_cache"):
                gp._ast_negative_cache.clear()
            reclaim_system_memory()
            commit_time = time.perf_counter() - t_commit

    avg_velocity = executed_count / max(0.001, resolution_time)
    logger.info(COLOR.green(
        f"[Phase 2] Completed resolution of {executed_count} ChangeSets across {wave_num} waves "
        f"in {resolution_time:.1f}s ({avg_velocity:.1f} files/s); final commit {commit_time:.1f}s."
    ))


# Frames on each worker's private duplex pipe. The parent sends task frames (two ahead, so a worker never
# waits for its next batch) and a stop frame once no batch is left; the worker answers each task with a
# result frame and ends with a done frame. A pipe that closes before the done frame means the worker died
# (signal, OOM kill, libclang abort) while parsing the oldest batch it had not returned.
_FRAME_TASK = b"T"
_FRAME_STOP = b"S"
_FRAME_RESULT = b"R"
_FRAME_INIT_FAILED = b"I"
_FRAME_DONE = b"D"
_RESULT_HEADER = struct.Struct("<cqB")  # frame tag, batch id, zlib-compressed flag
_STAGE_END = object()
# Batches sent to a worker ahead of the one it parses
_TASKS_AHEAD = 2
# Serialized batches the reader thread may hold for the ingest thread; more only adds parent memory
_PREFETCH_DEPTH = 2


def _recv_task(conn: Any) -> tuple[int, list[str]] | None:
    """Worker side: wait for the next `(batch_id, files)` task, or None on the stop frame or a closed pipe."""
    try:
        frame = conn.recv_bytes()
    except (EOFError, OSError):
        return None
    if frame[:1] != _FRAME_TASK:
        return None
    return pickle.loads(frame[1:])


@dataclass
class WorkerStageResult:
    """Outcome of one `_run_worker_stage()` call."""

    files_ingested: int = 0
    failed_workers: int = 0
    deadline_hit: bool = False
    elapsed_s: float = 0.0
    tail_idle_s: float = 0.0
    lost_files: list[str] = field(default_factory=list)
    # Where the stage's time went (see _stage_time_split())
    split: dict[str, float] = field(default_factory=dict)


def _encode_batch_result(
    batch_id: int,
    batch_cs_dict: dict[str, Any],
    errors: list[tuple[str, str, str]],
    compress: bool,
) -> bytes | memoryview:
    """Serialize a parsed batch and its per-file errors into one result frame."""
    buf = io.BytesIO()
    buf.write(_RESULT_HEADER.pack(_FRAME_RESULT, batch_id, 0))
    pickle.dump((batch_cs_dict, errors), buf, protocol=pickle.HIGHEST_PROTOCOL)
    if not compress:
        return buf.getbuffer()
    body = zlib.compress(buf.getbuffer()[_RESULT_HEADER.size :], level=1)
    return _RESULT_HEADER.pack(_FRAME_RESULT, batch_id, 1) + body


def _decode_batch_result(frame: bytes) -> tuple[int, bytes | memoryview]:
    """Split a result frame into its batch id and pickled `(batch_cs_dict, errors)` body."""
    _tag, batch_id, compressed = _RESULT_HEADER.unpack_from(frame, 0)
    body = memoryview(frame)[_RESULT_HEADER.size :]
    return batch_id, (zlib.decompress(body) if compressed else body)


# Small stages are split into about this many batches per worker: with one large batch per worker the
# others sit idle while the slowest finishes (10-40s per small version)
_BATCHES_PER_WORKER = 4
_MIN_BATCH_FILES = 4


def _balanced_batch_size(n_files: int, num_workers: int, limit: int) -> int:
    """Largest batch size up to `limit` that still gives each worker about `_BATCHES_PER_WORKER` batches."""
    target = math.ceil(n_files / max(1, num_workers * _BATCHES_PER_WORKER))
    return max(1, min(limit, max(_MIN_BATCH_FILES, target)))


def _select_worker_changes() -> list[str]:
    """Return the change lines workers must parse: Step 4.5's filtered list when it ran, else gp.Change_List.

    An empty filtered list means every change was deduplicated, not "parse everything".
    """
    if gp.worker_change_list is not None:
        return gp.worker_change_list
    return gp.Change_List or []


def _memory_profile() -> dict[str, int]:
    """Worker count and batch budgets for the active memory mode (`G.MEMORY_MODE`)."""
    cpus = int(G.CPUS)
    if G.VERY_LOW_MEMORY_MODE:
        return {"workers": max(1, min(2, cpus // 4)), "header_batch": 25, "normal_batch": 25,
                "heavy_batch": 10, "max_batch_bytes": 350_000, "epoch_files": 2000}
    if G.LOW_MEMORY_MODE:
        return {"workers": max(1, cpus // 2), "header_batch": 50, "normal_batch": 50,
                "heavy_batch": 25, "max_batch_bytes": 750_000, "epoch_files": 4500}
    return {"workers": max(1, cpus - 1), "header_batch": 200, "normal_batch": 200,
            "heavy_batch": 50, "max_batch_bytes": 1_500_000, "epoch_files": 4500}


def _close_parent_db() -> None:
    """Close the parent's TableEngine connection so no forked child inherits a live socket."""
    db = getattr(G.TE, "db", None)
    G.TE.db = None
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


def _parent_rss_mb() -> float:
    """Resident set size of this process in MB (0 when /proc is unavailable)."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except Exception:
        return 0.0


def _register_file_names(items: list[str]) -> None:
    """Assign m_file_name ids to every path in `items` and commit them before workers fork.

    Workers find these ids in the inherited cache instead of allocating colliding ids of their own.
    """
    for item in items:
        if not item:
            continue
        for p in item.split("\t")[1:]:
            if p:
                G.TE.set(m_file_name.table_id, (None, p))
    G.TE.commit(m_file_name.table_id)


def _commit_all_with_retry(context: str) -> None:
    """Commit every staged TableEngine row, re-checking the DB connection once on a transient failure."""
    try:
        G.TE.commit_all()
    except Exception as e:
        logger.warning(f"{context} TableEngine commit hit transient error ({e}); re-verifying DB connection and retrying...")
        if getattr(G.TE, "db", None) is not None and hasattr(G.TE.db, "check_if_connected"):
            G.TE.db.check_if_connected()
        G.TE.commit_all()


def _begin_background_commits() -> None:
    """Let in-stage commits run on the TableEngine's writer thread while this thread keeps ingesting."""
    if hasattr(G.TE, "wait_for_commit"):
        G.TE.async_commits = True


def _end_background_commits(context: str) -> None:
    """Switch background commits off and wait for the last one (nothing may fork while a writer runs).

    A failed background commit put its rows back into staging; they are committed again here.
    """
    if not hasattr(G.TE, "wait_for_commit"):
        return
    G.TE.async_commits = False
    try:
        G.TE.wait_for_commit()
    except Exception as e:
        logger.warning(COLOR.yellow(f"{context} background commit failed ({e}); its rows were put back, committing them again..."))
        _commit_all_with_retry(context)


# Staged rows that make a parent waiting for its workers start a background commit (`_maybe_idle_commit()`)
_IDLE_COMMIT_MIN_ROWS = 100_000


def _maybe_idle_commit() -> None:
    """Start a background commit while the parent waits for worker results.

    Chunk commits come every `chunk_commit_interval` executed files, so a small version used to write all its rows
    in one commit after parsing, with every worker idle. Committing while waiting overlaps that write with parsing.
    """
    te = G.TE
    if not getattr(te, "async_commits", False) or te.pending_commit_rows or te.staged_rows < _IDLE_COMMIT_MIN_ROWS:
        return
    try:
        te.commit_all()
    except Exception as e:
        # A failed batch is back in staging; the next commit writes it again
        logger.warning(COLOR.yellow(f"Background commit while waiting for workers failed ({e}); retrying with the next commit."))


def _parent_split_counters() -> dict[str, float]:
    """Snapshot of the parent's commit, database-read and memory-reclaim counters (diffed per stage)."""
    commit = getattr(G.TE, "commit_stats", None) or {}
    return {
        "commit_blocked_s": float(commit.get("blocked_s", 0.0)),
        "commit_write_s": float(commit.get("write_s", 0.0)),
        "commit_rows": float(commit.get("rows", 0)),
        "db_reads": float(getattr(G.TE, "db_reads", 0)),
        "db_read_s": float(getattr(G.TE, "db_read_s", 0.0)),
        "gc_s": MEMORY_RECLAIM_STATS["collect_s"] + MEMORY_RECLAIM_STATS["trim_s"],
    }


# Per-worker counters a worker sends with its done frame (file_processing_worker)
_WORKER_SPLIT_KEYS = (
    "busy_s", "task_wait_s", "send_wait_s", "gc_s", "prune_s", "pre_resolve_s", "ref_views_s", "encode_s", "db_reads", "db_read_s",
)


def _stage_time_split(
    parent: dict[str, float], base: dict[str, float], worker_stats: list[dict[str, float]]
) -> dict[str, float]:
    """Combine the parent's loop timings, its counter deltas since `base` and the workers' reported counters."""
    split = dict(parent)
    for key, value in _parent_split_counters().items():
        split[key] = value - base.get(key, 0.0)
    split["workers"] = float(len(worker_stats))
    for key in _WORKER_SPLIT_KEYS:
        split[f"worker_{key}"] = sum(float(ws.get(key, 0.0)) for ws in worker_stats)
    return split


def _format_time_split(split: dict[str, float]) -> str:
    """One-line summary of `_stage_time_split()`."""
    text = (
        f"parent waited {split.get('wait_s', 0.0):.1f}s for results, decoded {split.get('decode_s', 0.0):.1f}s, "
        f"ingested {split.get('ingest_s', 0.0):.1f}s (DB reads {int(split.get('db_reads', 0)):,} in "
        f"{split.get('db_read_s', 0.0):.1f}s), blocked on commits {split.get('commit_blocked_s', 0.0):.1f}s of "
        f"{split.get('commit_write_s', 0.0):.1f}s writing {int(split.get('commit_rows', 0)):,} rows, "
        f"GC/trim {split.get('gc_s', 0.0):.1f}s"
    )
    total = sum(split.get(f"worker_{k}", 0.0) for k in ("busy_s", "task_wait_s", "send_wait_s", "gc_s", "tail_idle_s"))
    if split.get("workers") and total > 0:
        def pct(key: str) -> float:
            return 100.0 * split.get(f"worker_{key}", 0.0) / total

        text += (
            f"; workers {pct('busy_s'):.0f}% parsing (dependency pruning {split.get('worker_prune_s', 0.0):.1f}s, "
            f"DB reads {int(split.get('worker_db_reads', 0)):,} in {split.get('worker_db_read_s', 0.0):.1f}s), "
            f"{pct('send_wait_s'):.0f}% blocked sending to the parent, "
            f"{pct('task_wait_s') + pct('tail_idle_s'):.0f}% without a task (or exited), "
            f"{pct('gc_s'):.0f}% GC"
        )
    return text


# Symbol item counters (core/SymbolRows.STATS) reported by this version's parsing workers
WORKER_SYMBOL_STATS: Counter = Counter()


def _log_symbol_stats(version: str, parent_base: Counter) -> None:
    """Log how this version's symbol items were stored: in tags (shared) or per blob, and why."""
    stats = WORKER_SYMBOL_STATS + (SYMBOL_STATS - parent_base)
    WORKER_SYMBOL_STATS.clear()
    if not stats:
        return
    in_tags = sum(stats[f"{kind} {how}"] for kind in ("refs", "defs") for how in ("reused", "new tag"))
    reused = stats["refs reused"] + stats["defs reused"]
    reasons = ("changed reused tag", "conflicting key", "outside tag", "without tag")
    per_blob = {how: stats[f"refs {how}"] + stats[f"defs {how}"] for how in reasons}
    logger.info(
        f"[Symbols] '{version}': {in_tags:,} items stored in tags ({reused:,} of them in reused tags, not rewritten); "
        f"{sum(per_blob.values()):,} per blob (" + ", ".join(f"{how} {n:,}" for how, n in per_blob.items()) + "); "
        f"{stats['keys refined']:,} keys refined by position"
    )


def _publish_file_symbols() -> None:
    """Pack the symbols published since the previous call into a new file-scoped shared segment.

    Forked workers query the segments (`gp._shared_symbols`) per target file without copying them,
    so the parent's heap copies are dropped once packed.
    """
    if gp.file_symbols:
        t_build = time.perf_counter()
        builder = SharedSymbolBuilder(mode=getattr(G.TE, "hugepages", "auto"))
        segment = builder.build_file_scoped(gp.file_symbols)
        if segment is not None:
            if gp._shared_symbols is None:
                gp._shared_symbols = SharedSymbolChain()
            gp._shared_symbols.append(segment)
            logger.info(
                f"[Stage 2 - Sources] Published {segment.sym_count} symbols of {len(gp.file_symbols)} files into shared "
                f"segment {len(gp._shared_symbols.segments)} ({segment.nbytes // (1024 * 1024)}MB, "
                f"{time.perf_counter() - t_build:.1f}s); {gp._shared_symbols.sym_count} shared symbols in total."
            )
    gp.file_symbols.clear()
    gp.file_names.clear()
    gp._ast_staged_symbols.clear()
    gp._ast_staged_names.clear()
    gp.unchanged_symbol_cache.clear()
    reclaim_system_memory()


def _run_worker_stage(
    label: str,
    batches: list[tuple[int, list[str]]],
    ingest: Callable[[dict[str, Any]], None],
    num_workers: int,
    error_list: list[tuple[str, str, str]],
    on_started: Callable[[], None] | None = None,
    retry_lost: bool = True,
    worker_target: Callable[..., None] | None = None,
) -> WorkerStageResult:
    """Parse `batches` on forked workers and stream every batch result into `ingest` on this thread.

    Each worker owns a duplex pipe. A reader thread sends it batches in the given order (`_TASKS_AHEAD`
    in advance), receives its result frames (blocking reads release the GIL) and hands at most
    `_PREFETCH_DEPTH` serialized batches to this thread, so a parent that falls behind stalls the
    workers instead of buffering their output. `on_started` runs once the workers are running.

    A worker that dies is attributed the oldest batch it had not returned; its other queued batches go
    back to the surviving workers. Batches that never came back are parsed again afterwards, the dead
    workers' batches one file per batch to isolate the file that kills a worker. An `ingest` failure is
    recorded with its batch and does not stop the stage. If no worker can start (e.g. the database is
    unreachable), the stage raises.
    """
    result = WorkerStageResult()
    if not batches:
        if on_started is not None:
            on_started()
        return result

    target = worker_target or file_processing_worker
    batch_files = dict(batches)
    num_workers = max(1, min(num_workers, len(batches)))
    t_start = time.perf_counter()

    procs: dict[int, Any] = {}
    conns: dict[Any, int] = {}
    _end_background_commits(label)
    _close_parent_db()
    try:
        freeze_system_memory()
        for worker_id in range(num_workers):
            parent_conn, child_conn = _MP.Pipe(duplex=True)
            p = _MP.Process(target=target, args=(child_conn, gp.VID, gp, MF, worker_id))
            p.start()
            # The worker now holds the only other end: its exit closes the pipe
            child_conn.close()
            procs[worker_id] = p
            conns[parent_conn] = worker_id
    finally:
        G.TE.db = G.DB()

    pending: deque[tuple[int, list[str]]] = deque(batches)
    assigned: dict[int, deque[int]] = {worker_id: deque() for worker_id in procs}
    stopped: set[int] = set()
    done_workers: set[int] = set()
    init_failed: set[int] = set()
    suspect_batches: list[int] = []
    finish_times: list[float] = []
    frames: queue.Queue = queue.Queue(maxsize=_PREFETCH_DEPTH)
    stop = threading.Event()
    reader_failures: list[BaseException] = []
    worker_stats: list[dict[str, float]] = []
    # Per worker: (header symbol maps it loaded, blob ids it looked up), merged into gp.blob_symbol_cache
    blob_symbol_reports: list[tuple[Any, Any]] = []

    def _offer(item: Any) -> bool:
        while not stop.is_set():
            try:
                frames.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def _dispatch(conn: Any, worker_id: int) -> None:
        """Send `worker_id` its next batch, or its stop frame once no batch is left."""
        if worker_id in stopped:
            return
        try:
            if pending:
                b_id, b_files = pending.popleft()
                assigned[worker_id].append(b_id)
                conn.send_bytes(_FRAME_TASK + pickle.dumps((b_id, b_files), protocol=pickle.HIGHEST_PROTOCOL))
            else:
                stopped.add(worker_id)
                conn.send_bytes(_FRAME_STOP)
        except OSError:
            # The worker is gone; its EOF is handled by the read loop
            pass

    def _reader_loop() -> None:
        open_conns = dict(conns)
        try:
            for _ in range(_TASKS_AHEAD):
                for conn, worker_id in open_conns.items():
                    _dispatch(conn, worker_id)
            while open_conns and not stop.is_set():
                for conn in mp_wait(list(open_conns), timeout=0.5):
                    worker_id = open_conns[conn]
                    try:
                        frame = conn.recv_bytes()
                    except (EOFError, OSError):
                        del open_conns[conn]
                        conn.close()
                        finish_times.append(time.perf_counter())
                        unreturned = assigned[worker_id]
                        if worker_id not in done_workers and unreturned:
                            if worker_id not in init_failed:
                                # The oldest unreturned batch was being parsed when the worker died
                                suspect_batches.append(unreturned.popleft())
                            # Queued but never started: back to the front for the surviving workers
                            pending.extendleft(reversed([(b_id, batch_files[b_id]) for b_id in unreturned]))
                            unreturned.clear()
                            # Only top up workers below their queue depth, so no pipe fills up with tasks
                            for other_conn, other_id in open_conns.items():
                                if len(assigned[other_id]) < _TASKS_AHEAD:
                                    _dispatch(other_conn, other_id)
                        continue
                    tag = frame[:1]
                    if tag == _FRAME_DONE:
                        done_workers.add(worker_id)
                        if len(frame) > 1:
                            try:
                                stats = pickle.loads(frame[1:])
                                WORKER_SYMBOL_STATS.update(stats.pop("symbol_stats", None) or {})
                                blob_symbol_reports.append((stats.pop("blob_symbols", None), stats.pop("blob_symbols_used", None)))
                                worker_stats.append(stats)
                            except Exception:
                                pass
                    elif tag == _FRAME_INIT_FAILED:
                        init_failed.add(worker_id)
                        error_list.append(pickle.loads(frame[1:]))
                    elif tag == _FRAME_RESULT:
                        try:
                            decoded = _decode_batch_result(frame)
                        except Exception as e:
                            # The batch stays unreceived and is parsed again
                            error_list.append((f"{label} worker {worker_id}", f"Unreadable result frame: {e}", traceback.format_exc()))
                            continue
                        del frame
                        if decoded[0] in assigned[worker_id]:
                            assigned[worker_id].remove(decoded[0])
                        # Keep the worker supplied before possibly blocking on the ingest thread
                        _dispatch(conn, worker_id)
                        if not _offer(decoded):
                            return
        except BaseException as e:
            reader_failures.append(e)
        finally:
            for conn in open_conns:
                conn.close()
            _offer(_STAGE_END)

    reader = threading.Thread(target=_reader_loop, name=f"{label} reader", daemon=True)
    reader.start()

    received: set[int] = set()
    total = len(batches)
    completed = False
    parent_split = {"wait_s": 0.0, "decode_s": 0.0, "ingest_s": 0.0}
    split_base = _parent_split_counters()
    try:
        if on_started is not None:
            on_started()
        _begin_background_commits()
        while True:
            t_wait = time.perf_counter()
            try:
                item = frames.get(timeout=1.0)
            except queue.Empty:
                parent_split["wait_s"] += time.perf_counter() - t_wait
                _maybe_idle_commit()
                if G.is_deadline_exceeded():
                    logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached during {label}. Halting early..."))
                    result.deadline_hit = True
                    break
                continue
            parent_split["wait_s"] += time.perf_counter() - t_wait
            if item is _STAGE_END:
                break
            b_id, body = item
            del item
            received.add(b_id)
            t_decode = time.perf_counter()
            try:
                batch_dict, batch_errors = pickle.loads(body)
            except Exception as e:
                error_list.extend(
                    (f, f"Batch {b_id} result could not be deserialized: {e}", traceback.format_exc())
                    for f in batch_files.get(b_id, ())
                )
                continue
            finally:
                del body
                parent_split["decode_s"] += time.perf_counter() - t_decode
            error_list.extend(batch_errors)
            t_ingest = time.perf_counter()
            try:
                ingest(batch_dict)
                result.files_ingested += len(batch_dict)
            except Exception as e:
                logger.error(COLOR.red(f"{label}: ingesting batch {b_id} failed: {e}"), exc_info=True)
                error_list.append((
                    f"Batch {b_id} ({len(batch_files.get(b_id, ()))} files)", f"Ingest failed: {e}", traceback.format_exc()
                ))
            parent_split["ingest_s"] += time.perf_counter() - t_ingest
            del batch_dict

            n_received = len(received)
            if n_received % 10 == 0 or n_received == total:
                elapsed = time.perf_counter() - t_start
                logger.info(
                    f"{label} ingested batch {n_received}/{total} "
                    f"({n_received / max(0.001, elapsed):.1f} batches/s, {len(done_workers)}/{num_workers} workers done, "
                    f"elapsed {elapsed:.1f}s)"
                )
                # Normal modes rely on the automatic collector and the trim after each commit
                if G.LOW_MEMORY_MODE:
                    reclaim_system_memory()
            if G.is_deadline_exceeded():
                logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached during {label}. Halting early..."))
                result.deadline_hit = True
                break
        if reader_failures:
            raise reader_failures[0]
        completed = not result.deadline_hit
    finally:
        stop.set()
        if not completed:
            for p in procs.values():
                if p.is_alive():
                    p.terminate()
        reader.join(timeout=10.0)
        for p in procs.values():
            p.join(timeout=10.0 if completed else 2.0)
            if p.is_alive():
                p.kill()
                p.join(timeout=2.0)
        _end_background_commits(label)

    if len(init_failed) == len(procs):
        raise RuntimeError(
            f"{label}: no parsing worker could start ({len(procs)} failed to initialize); "
            f"first error: {next((e[1] for e in error_list if e[0].startswith('Worker ')), 'unknown')}"
        )

    for worker_id, p in procs.items():
        if worker_id in done_workers or worker_id in init_failed:
            continue
        if result.deadline_hit and p.exitcode in (-signal.SIGTERM, -signal.SIGKILL):
            logger.info(f"{label}: worker PID {p.pid} stopped on execution deadline.")
            continue
        result.failed_workers += 1
        logger.error(COLOR.red(f"{label}: worker {worker_id} (PID {p.pid}) died before finishing (exit code {p.exitcode})"))

    for fetched, used in blob_symbol_reports:
        gp.merge_blob_symbols(fetched, used)

    result.elapsed_s = time.perf_counter() - t_start
    if len(finish_times) > 1:
        result.tail_idle_s = max(finish_times) - min(finish_times)
    result.split = _stage_time_split(parent_split, split_base, worker_stats)
    if finish_times:
        # Workers that exited before the last one were idle until the stage ended
        result.split["worker_tail_idle_s"] = sum(max(finish_times) - t for t in finish_times)
    logger.info(f"{label} time split: {_format_time_split(result.split)}")

    if result.deadline_hit:
        return result
    suspects = [b_id for b_id in suspect_batches if b_id not in received]
    unfinished = [b_id for b_id, _ in batches if b_id not in received and b_id not in suspects]
    if not suspects and not unfinished:
        return result

    next_id = max(batch_files) + 1
    if retry_lost:
        # First failure of these batches: retry each file of a dead worker's batch on its own
        suspect_files = [f for b_id in suspects for f in batch_files[b_id]]
        retry_batches = [(next_id + i, [f]) for i, f in enumerate(suspect_files)]
        logger.warning(COLOR.yellow(
            f"{label}: {len(suspects)} batch(es) lost with a dead worker ({len(suspect_files)} files, re-parsed one "
            f"file per batch) and {len(unfinished)} batch(es) not parsed yet; parsing them again."
        ))
    else:
        # These single files killed a worker twice: report them and keep going with the rest
        retry_batches = []
        for b_id in suspects:
            for f in batch_files[b_id]:
                result.lost_files.append(f)
                error_list.append((f, "A worker process died while parsing this file; it was not ingested.", ""))
    next_id += len(retry_batches)
    retry_batches += [(next_id + i, batch_files[b_id]) for i, b_id in enumerate(unfinished)]

    if not retry_batches:
        return result
    if not retry_lost and not suspects and not received:
        # No progress at all in this round: stop instead of retrying forever
        for b_id in unfinished:
            for f in batch_files[b_id]:
                result.lost_files.append(f)
                error_list.append((f, "No worker was left to parse this file; it was not ingested.", ""))
        return result
    retry = _run_worker_stage(
        label if label.endswith("[retry]") else f"{label} [retry]",
        retry_batches,
        ingest,
        num_workers,
        error_list,
        retry_lost=False,
        worker_target=target,
    )
    result.files_ingested += retry.files_ingested
    result.failed_workers += retry.failed_workers
    result.deadline_hit = result.deadline_hit or retry.deadline_hit
    result.lost_files += retry.lost_files
    return result


def _report_worker_errors(error_list: list[tuple[str, str, str]], failed_workers: int) -> None:
    """Log every file error collected from the workers (tracebacks at debug level)."""
    if error_list:
        logger.error(COLOR.red(f"Multicore processing encountered {len(error_list)} file error(s):"))
        for failed_file, err, tb in error_list:
            logger.error(COLOR.red(f"  [ERROR] File: {failed_file} => {err}"))
            if tb:
                logger.debug(tb)
    if failed_workers > 0 or error_list:
        logger.error(COLOR.red(
            f"Multicore execution completed with {failed_workers} crashed worker(s) and {len(error_list)} file error(s)!"
        ))


def _run_source_epochs(
    source_files: list[str],
    working_dir: str | None,
    profile: dict[str, int],
    scheduler: DependencyScheduler | None,
    ingest: Callable[[dict[str, Any]], None],
    error_list: list[tuple[str, str, str]],
) -> tuple[int, int]:
    """Run Stage 2: parse sources epoch by epoch, publishing each epoch's symbols for the epochs after it.

    Returns:
        (files ingested, workers that died).
    """
    _publish_file_symbols()
    normal_batch = _balanced_batch_size(min(len(source_files), profile["epoch_files"]), profile["workers"], profile["normal_batch"])
    source_epochs = partition_source_epochs(
        source_files,
        working_dir=working_dir,
        normal_batch_size=normal_batch,
        heavy_batch_size=min(normal_batch, profile["heavy_batch"]),
        max_batch_bytes=profile["max_batch_bytes"],
        target_epoch_files=profile["epoch_files"],
        num_workers=profile["workers"],
    )
    total_epochs = len(source_epochs)
    shared_count = gp._shared_symbols.sym_count if gp._shared_symbols is not None else 0
    logger.info(
        f"[Stage 2 - Sources] Partitioned {len(source_files)} source files into {total_epochs} epochs "
        f"({sum(len(ep) for ep in source_epochs)} batches) with {shared_count} shared symbols "
        f"across {profile['workers']} parallel workers"
    )

    files_streamed = 0
    failed_workers = 0
    for epoch_idx, epoch_batches in enumerate(source_epochs):
        if G.is_deadline_exceeded():
            logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached before the next Stage 2 epoch. Halting early..."))
            break
        label = f"[Stage 2 - Sources] Epoch {epoch_idx + 1}/{total_epochs}"
        logger.info(f"{label}: {sum(len(b[1]) for b in epoch_batches)} files in {len(epoch_batches)} batches")
        outcome = _run_worker_stage(label, epoch_batches, ingest, profile["workers"], error_list)
        files_streamed += outcome.files_ingested
        failed_workers += outcome.failed_workers
        if outcome.deadline_hit:
            break

        t_boundary = time.perf_counter()
        if epoch_idx < total_epochs - 1:
            if scheduler is not None:
                unblocked = scheduler.drain_deferred_queue()
                if unblocked > 0:
                    logger.info(
                        f"{label}: deferred drain unblocked {unblocked} files ({len(scheduler.deferred_queue)} still deferred)."
                    )
            _commit_all_with_retry("Inter-epoch")
            _publish_file_symbols()
        logger.info(
            f"{label} done: {outcome.elapsed_s:.1f}s parsing (last worker exited {outcome.tail_idle_s:.1f}s after the first), "
            f"{time.perf_counter() - t_boundary:.1f}s boundary drain/commit/publish; parent RSS {_parent_rss_mb():.0f}MB, "
            f"{gc.get_freeze_count()} frozen objects"
        )
    return files_streamed, failed_workers


def trigger_multicore(batch_size: int | None = None, scheduler: DependencyScheduler | None = None) -> None:
    """Parse the version's changed files on forked workers in two stages: headers first, then sources.

    Stage 1 parses headers while the parent propagates directories and unchanged files, then resolves
    the headers in wave order. Stage 2 parses everything else epoch by epoch; each epoch's symbols are
    published to a file-scoped shared segment that later epochs' workers resolve against. With a
    scheduler, the segments stay mapped for `scheduler.finalize()`, which releases them.
    """
    working_dir = MF.version_dict.get(gp.Version_Name)
    profile = _memory_profile()
    num_workers = profile["workers"]
    if batch_size is None:
        batch_size = profile["header_batch"]

    regular_files: list[str] = []
    symlink_files: list[str] = []
    for item in _select_worker_changes():
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
    gp.close_shared_symbols()

    header_files: list[str] = []
    source_files: list[str] = []
    for item in regular_files:
        if item.split("\t")[-1].endswith((".h", ".hpp", ".hxx")):
            header_files.append(item)
        else:
            source_files.append(item)

    # Byte-bounded batches kept in include order: headers that others wait on arrive first, so the
    # scheduler can run their dependents while the workers are still parsing
    header_batch = _balanced_batch_size(len(header_files), num_workers, batch_size)
    header_epochs = partition_source_epochs(
        header_files,
        working_dir=working_dir,
        normal_batch_size=header_batch,
        heavy_batch_size=min(header_batch, profile["heavy_batch"]),
        max_batch_bytes=profile["max_batch_bytes"],
        target_epoch_files=len(header_files),
        largest_first=False,
        num_workers=num_workers,
    )
    header_batches = header_epochs[0] if header_epochs else []
    logger.info(
        f"Distributing {len(regular_files)} changed files ({len(header_files)} headers in {len(header_batches)} batches, "
        f"{len(source_files)} sources) (memory mode: {G.MEMORY_MODE}) across {num_workers} parallel workers"
    )

    def _ingester(is_header_stage: bool) -> Callable[[dict[str, Any]], None]:
        def _ingest(batch_dict: dict[str, Any]) -> None:
            if scheduler is not None:
                scheduler.ingest_batch(batch_dict, is_header_stage=is_header_stage)
            else:
                gp.ChangeSet_Dict.update(execute_and_purge(batch_dict))
        return _ingest

    extra_files: list[str] = []

    def _propagate_tree(is_header_stage: bool) -> None:
        """Stage new directories, carry unchanged files forward, and collect files that still need parsing."""
        dir_changesets = processing_dirs()
        if dir_changesets:
            _ingester(is_header_stage)(dir_changesets)
        missing = processing_unchanges()
        if missing:
            # Known before any of their dependents execute, so references to them wait instead of stubbing
            gp._changed_paths_set.update(item.split("\t")[-1] for item in missing)
            extra_files.extend(missing)

    if not regular_files:
        _propagate_tree(is_header_stage=False)
        if not extra_files:
            return
    _register_file_names(gp.Change_List or (regular_files + symlink_files))

    error_list: list[tuple[str, str, str]] = []
    failed_workers = 0
    try:
        if header_files:
            stage1 = _run_worker_stage(
                "[Stage 1 - Headers]",
                header_batches,
                _ingester(True),
                num_workers,
                error_list,
                on_started=lambda: _propagate_tree(is_header_stage=True),
            )
            failed_workers += stage1.failed_workers
            logger.info(
                f"[Stage 1 - Headers] done: {stage1.elapsed_s:.1f}s parsing (last worker exited {stage1.tail_idle_s:.1f}s "
                f"after the first); parent RSS {_parent_rss_mb():.0f}MB"
            )
            if extra_files and not stage1.deadline_hit:
                # Parsed before wave resolution, like the changed headers they may define symbols for
                _register_file_names(extra_files)
                logger.info(f"[Stage 1 - Headers] Parsing {len(extra_files)} files missing from the previous version.")
                first_id = len(header_batches)
                extra_size = _balanced_batch_size(len(extra_files), num_workers, batch_size)
                extra_batches = [
                    (first_id + i // extra_size, extra_files[i : i + extra_size]) for i in range(0, len(extra_files), extra_size)
                ]
                extra = _run_worker_stage("[Stage 1 - Missing files]", extra_batches, _ingester(True), num_workers, error_list)
                failed_workers += extra.failed_workers
            if scheduler is not None and not G.is_deadline_exceeded():
                t_resolve = time.perf_counter()
                with G.measure_stage("Stage 1: Header Wave Resolution", count=len(header_files), units="headers"):
                    logger.info(COLOR.cyan(f"[Stage 1 - Headers] Resolving {len(header_files)} header files in wave order..."))
                    scheduler.resolve_headers()
                logger.info(COLOR.green(
                    f"[Stage 1 - Headers] Header resolution complete in {time.perf_counter() - t_resolve:.1f}s "
                    f"(no parsing workers run meanwhile). {len(gp.file_symbols)} header symbol tables indexed."
                ))
        else:
            if regular_files:
                _propagate_tree(is_header_stage=False)
            if extra_files:
                _register_file_names(extra_files)
                source_files.extend(extra_files)

        if source_files and G.is_deadline_exceeded():
            logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached; skipping Stage 2 ({len(source_files)} sources)."))
        elif source_files:
            with G.measure_stage("Stage 2: Source Streaming & Parsing", count=len(source_files), units="sources") as stage2_metric:
                files_streamed, stage2_failed = _run_source_epochs(
                    source_files, working_dir, profile, scheduler, _ingester(False), error_list
                )
                failed_workers += stage2_failed
                if stage2_metric is not None:
                    stage2_metric.count = min(len(source_files), files_streamed)
    except BaseException:
        gp.close_shared_symbols()
        raise
    finally:
        unfreeze_system_memory()
        reclaim_system_memory()

    if scheduler is None:
        gp.close_shared_symbols()
    _report_worker_errors(error_list, failed_workers)


def _apply_db_server_tuning(db: Any, mem_max_gb: float) -> None:
    """Raise the MariaDB settings listed in `database.server_tuning` (config) for this run.

    The defaults trade an InnoDB buffer pool far above the 128MB default, a larger redo log and a once-per-second
    log flush for commit throughput; the database can be rebuilt with --reset. A local server's buffer pool is
    capped at a quarter of RAM.
    """
    settings = dict(get_db_config().get("server_tuning") or {})
    if not settings or not hasattr(db, "apply_server_tuning"):
        return
    host = getattr(db, "host", None) or "localhost"
    local = host in ("localhost", "127.0.0.1", "::1") and not get_ssh_tunnel_config().get("enabled")
    try:
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        ram = 0
    pool = parse_server_setting(settings["innodb_buffer_pool_size"]) if "innodb_buffer_pool_size" in settings else None
    if local and ram and isinstance(pool, int) and pool > ram // 4:
        pool = (ram // 4) // (128 * 1024 * 1024) * (128 * 1024 * 1024)
        settings["innodb_buffer_pool_size"] = pool
    try:
        notes = db.apply_server_tuning(settings)
    except Exception as e:
        logger.warning(COLOR.yellow(f"[DB Tuning] Could not apply database.server_tuning: {e}"))
        return
    for note in notes:
        if " capped at " in note or ": kept " in note or "not available" in note:
            logger.warning(COLOR.yellow(f"[DB Tuning] {note}"))
        else:
            logger.info(f"[DB Tuning] {note}")
    if local and ram and isinstance(pool, int):
        # The pool the server actually runs with (a pool capped at its startup maximum is smaller)
        try:
            db.cursor.execute("SELECT @@GLOBAL.innodb_buffer_pool_size")
            row = db.cursor.fetchone()
            pool = int(row[0]) if row and row[0] is not None else pool
        except Exception:
            pass
    if local and ram and isinstance(pool, int) and mem_max_gb * 1024**3 + pool > ram:
        logger.warning(COLOR.yellow(
            f"[DB Tuning] The memory guard ({mem_max_gb:.0f} GB) plus the InnoDB buffer pool ({pool / 1024**3:.0f} GB) "
            f"exceed this machine's {ram / 1024**3:.0f} GB of RAM; lower parser.mem_max if the parser ever gets close."
        ))


def main() -> None:
    """Set the plan for what version to parse."""
    args = arg_handling()
    setup_memory_limit(float(args.mem_max))
    with G.DB() as db:
        _apply_db_server_tuning(db, float(args.mem_max))
        if getattr(args, "reset", False) or getattr(args, "Drop", False):
            logger.info("Resetting and recreating all database tables...")
            db.drop_table(gp.Table_Array)
            drop_legacy_symbol_tables(db)
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
    G.SYMBOL_LAYOUT = "blob" if str(parser_cfg.get("symbol_layout", "tag")).lower() == "blob" else "tag"

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
            "tests.test_multicore_pipeline",
            "tests.test_pipeline_steps",
            "tests.test_commit_pipeline",
            "tests.test_symbol_rows",
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


def _lookup_commit_ids(db: Any, network_id: int, hashes: Iterable[str]) -> dict[str, int]:
    """Map 40-hex commit hashes already registered in m_commit for `network_id` to their commit ids."""
    wanted = [h for h in dict.fromkeys(hashes) if isinstance(h, str) and len(h) == 40]
    found: dict[str, int] = {}
    if not wanted:
        return found
    cur = _raw_cursor(db)
    if cur is None:
        for h in wanted:
            row = db.select(m_commit, (None, network_id, bytes.fromhex(h), None, None, None, None, None, None))
            if row:
                found[h] = row[0]
        return found
    try:
        for i in range(0, len(wanted), _SQL_IN_CHUNK):
            chunk = wanted[i : i + _SQL_IN_CHUNK]
            cur.execute(
                f"SELECT commit_hash, commit_id FROM m_commit WHERE network_id = %s "  # noqa: S608
                f"AND commit_hash IN ({','.join(['%s'] * len(chunk))})",
                (network_id, *(bytes.fromhex(h) for h in chunk)),
            )
            for c_hash, c_id in cur.fetchall():
                found[bytes(c_hash).hex()] = int(c_id)
    finally:
        cur.close()
    return found


def _ingest_commit_range(db: Any, commits: list[Any], network_id: int) -> dict[str, int]:
    """Register `commits` (newest first, as git log lists them) with their parents and contributors.

    Existing commits and persons are looked up and new rows inserted in bulk: a few statements per
    thousand commits instead of several round trips and transaction commits per commit.

    Returns:
        Commit hash -> commit_id for every commit of the range and every parent already registered.
    """
    if not commits:
        return {}
    ordered = [c for c in reversed(commits) if isinstance(c.commit_hash, str) and len(c.commit_hash) == 40]
    in_range = [c.commit_hash for c in ordered]
    outside_parents = {p for c in ordered for p in (c.parents or ())} - set(in_range)
    commit_ids = _lookup_commit_ids(db, network_id, [*in_range, *outside_parents])

    cur = _raw_cursor(db)
    person_ids: dict[tuple[str, str], int] = {}
    if cur is not None:
        try:
            cur.execute("SELECT person_id, name, email FROM m_maintainer_person")
            person_ids = {_person_key(name, email): int(pid) for pid, name, email in cur.fetchall()}
        finally:
            cur.close()
    new_persons: list[tuple[int, str, str]] = []
    next_ids: dict[str, int] = {}

    def _next_id(table: Table) -> int:
        if table.table_name not in next_ids:
            next_ids[table.table_name] = db.get_next_id(table)
        assigned = next_ids[table.table_name]
        next_ids[table.table_name] = assigned + 1
        return assigned

    def _person_id(name: str | None, email: str | None) -> int:
        key = _person_key(name, email)
        pid = person_ids.get(key)
        if pid is None and cur is None:
            row = db.select(m_maintainer_person, (None, key[0], key[1]))
            pid = row[0] if row else None
        if pid is None:
            pid = _next_id(m_maintainer_person)
            new_persons.append((pid, key[0], key[1]))
        person_ids[key] = pid
        return pid

    new_commits: list[tuple] = []
    for commit in ordered:
        if commit.commit_hash in commit_ids:
            continue
        author_pid = _person_id(commit.author_name, commit.author_email)
        committer_pid = _person_id(
            commit.committer_name or commit.committer_email or commit.author_name,
            commit.committer_email or commit.author_email,
        )
        c_id = _next_id(m_commit)
        commit_ids[commit.commit_hash] = c_id
        new_commits.append((
            c_id,
            network_id,
            bytes.fromhex(commit.commit_hash),
            author_pid,
            int(commit.author_date or 0),
            committer_pid,
            int(commit.committer_date or 0),
            (commit.subject or "")[:500],
            commit.message or "",
        ))

    # Parents and contributors of every commit in the range (both tables skip rows already present)
    parent_rows: list[tuple[int, int, int]] = []
    contributor_rows: dict[tuple[int, int, int], tuple[int, int, int, int]] = {}
    for commit in ordered:
        c_id = commit_ids[commit.commit_hash]
        for p_idx, p_hash in enumerate(commit.parents or ()):
            p_cid = commit_ids.get(p_hash)
            if p_cid is not None:
                parent_rows.append((c_id, p_cid, p_idx))
        for contrib in commit.contributors:
            pid = _person_id(contrib.name, contrib.email)
            contributor_rows.setdefault((c_id, pid, int(contrib.role)), (c_id, pid, int(contrib.role), int(contrib.priority)))

    for table, rows in (
        (m_maintainer_person, new_persons),
        (m_commit, new_commits),
        (m_commit_parent, parent_rows),
        (m_bridge_commit_person, list(contributor_rows.values())),
    ):
        if rows:
            db.insert(table, tuple(rows))
    return commit_ids


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

    gp.commit_ids_by_hash = {}
    if head_commit_id is None:
        from parser.git_ast import GitCommitParser
        git_parser = GitCommitParser(repo_dir=repo_path)
        base_rev = getattr(gp, "Old_Version_Name", None)
        # The DAG only needs commit metadata and parents; per-commit file lists come from processing_git_commits
        commits = git_parser.parse_version_commits(old_rev=base_rev, new_rev=version_or_ref, limit=limit, with_files=False)
        logger.info(f"[Git DAG] Ingesting {len(commits)} commits up to '{version_or_ref}'")
        with G.DB() as db:
            commit_hash_to_id = _ingest_commit_range(db, commits, network_id)
        # Reused by processing_git_commits() instead of one m_commit lookup per commit
        gp.commit_ids_by_hash = commit_hash_to_id
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
            # DELETE: the file is simply absent from gp.VID's checkpoint; parsers close its prior tags
            with CS(REF_OLD):
                # Get old file_name
                CS.store(m_file_name.get_set(
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
            # MODIFY (parsers look up the previous version's tags themselves; only renames reuse the old fid)
            # Get file_name
            CS.store(m_file_name.get_set(
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

def _attach_blob_hash(CS: ChangeSet, blob_hashes: dict[str, bytes] | None) -> bool:
    """Set `CS.blob_hash` from the version's `git ls-tree` hashes.

    Returns:
        False when the version's hashes are known but the file, which needs one (it creates an m_file_blob
        row), has none, e.g. a submodule gitlink: parsing it would file it under the all-zero hash shared
        by every such file.
    """
    if not blob_hashes:
        return True
    blob_hash = blob_hashes.get(CS.current_path)
    if blob_hash is not None:
        CS.blob_hash = blob_hash
        return True
    return CS.file_operation in ("D", "R100")


_GIT_COMMIT_RESULT = "commits.pkl"


def _git_commit_worker(old_ver: str | int, ver: str, result_path: str, repo_dir: str | None = None) -> None:
    """Worker process that parses git commits and diff hunks in parallel with file AST parsing.

    The result is pickled to `result_path` instead of a queue so the process exits as soon as parsing
    ends, and stops sharing the parent's fork-time memory long before the parent reads the result.
    """
    try:
        from parser.git_ast import GitCommitParser
        git_parser = GitCommitParser(repo_dir=repo_dir)
        commits, file_hunks_map = git_parser.parse_version_commits_with_hunks(old_ver, ver)
        tmp_path = f"{result_path}.tmp"
        with open(tmp_path, "wb") as f:
            pickle.dump((commits, file_hunks_map), f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp_path, result_path)
    except Exception as e:
        logger.error(f"Git commit worker error: {e}", exc_info=True)
        sys.exit(1)


def _start_git_commit_worker() -> None:
    """Fork the git commit parser for gp.Old_Version_Name..gp.Version_Name to run alongside AST parsing."""
    gp.stop_commit_worker()
    base_dir = G.RAMDISK if os.path.isdir(G.RAMDISK) else None
    result_dir = tempfile.mkdtemp(prefix="kip-commits-", dir=base_dir)
    gp.PURGE_LIST.append(result_dir)
    gp._commit_result_dir = result_dir
    proc = _MP.Process(
        target=_git_commit_worker,
        args=(gp.Old_Version_Name, gp.Version_Name, os.path.join(result_dir, _GIT_COMMIT_RESULT), MF.get_repo_dir()),
        name="git-commit-parser",
        daemon=True,
    )
    proc.start()
    gp._commit_proc = proc


def _collect_git_commit_result() -> tuple[Any, Any] | None:
    """Wait for the background git commit parser and load its result.

    Returns:
        `(commits, file_hunks_map)`, or None when no worker ran, it failed, or the deadline passed.
    """
    proc = getattr(gp, "_commit_proc", None)
    result_dir = getattr(gp, "_commit_result_dir", None)
    if proc is None or not result_dir:
        return None
    try:
        while proc.is_alive():
            proc.join(timeout=1.0)
            if proc.is_alive() and G.is_deadline_exceeded():
                logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached while waiting for the git commit worker."))
                return None
        if proc.exitcode != 0:
            logger.warning(f"Git commit worker exited with code {proc.exitcode}; parsing commits in-process.")
            return None
        with open(os.path.join(result_dir, _GIT_COMMIT_RESULT), "rb") as f:
            return pickle.load(f)  # noqa: S301
    except Exception as e:
        logger.warning(f"Failed to load precomputed commits from background worker: {e}")
        return None
    finally:
        gp.stop_commit_worker()


def file_processing_worker(
    conn: Any,
    vid: int,
    gp_ref: GreatProcessor,
    mf_ref: MasterFile,
    worker_id: int = 0,
) -> None:
    """Worker process that receives batches of changed files on `conn` and parses them into ChangeSets.

    A serializer thread pickles each finished batch (zlib-compressed in low memory modes) together with
    its per-file errors and sends it back on `conn` as one result frame, so parsing the next batch
    (already queued by the parent) overlaps sending the last one. `send_bytes` blocks while the parent is
    behind, which caps worker memory at the batches in flight. A done frame is sent last; closing the
    pipe without it marks the worker as crashed.
    """
    sys.setrecursionlimit(50000)
    # Detach worker gp from large parent-inherited caches to prevent Copy-on-Write page duplication
    if gp_ref is not None:
        gp_ref = copy.copy(gp_ref)
        gp_ref.ChangeSet_Dict = {}
        gp_ref._ast_staged_symbols = {}
        gp_ref._ast_staged_names = {}
        gp_ref.unchanged_symbol_cache = {}
        # The parent's blob_symbol_cache is only read; what this worker loads goes back with its done frame
        gp_ref._blob_symbols_fetched = {}
        gp_ref._blob_symbols_used = set()
        gp_ref.file_symbols = {}
        gp_ref.file_names = {}
        gp_ref._ast_negative_cache = set()
        # This TableEngine is never committed: resolve_ref leaves stubs to the parent
        gp_ref._is_worker = True

    send_lock = threading.Lock()

    def _send(frame: bytes | memoryview) -> None:
        with send_lock:
            conn.send_bytes(frame)

    # Dedicated DB connection per worker process to avoid socket sharing across fork
    try:
        G.TE.start_new_db(G.DB, is_worker=True)
    except Exception as e:
        logger.error(COLOR.red(f"Worker {worker_id} failed to initialize DB connection: {e}"))
        try:
            _send(_FRAME_INIT_FAILED + pickle.dumps((f"Worker {worker_id}", f"DB connection failed: {e}", traceback.format_exc())))
        except OSError:
            pass
        conn.close()
        return

    # File-scoped shared symbol segments inherited from the parent (None during Stage 1)
    shared_symbols = getattr(gp_ref, "_shared_symbols", None) if gp_ref is not None else None
    compress = G.LOW_MEMORY_MODE
    worker_serialize_queue: queue.Queue = queue.Queue(maxsize=2)
    # Time split sent to the parent with the done frame (keys: _WORKER_SPLIT_KEYS); the serializer thread
    # keeps its own counter so the two threads never update the same entry
    split = dict.fromkeys(_WORKER_SPLIT_KEYS, 0.0)
    encode_s = [0.0]
    reads_base = (getattr(G.TE, "db_reads", 0), getattr(G.TE, "db_read_s", 0.0))
    symbols_base = Counter(SYMBOL_STATS)

    def _serializer_loop() -> None:
        while True:
            item = worker_serialize_queue.get()
            if item is None:
                break
            b_id, b_files, b_dict, b_errors = item
            del item
            t_encode = time.perf_counter()
            try:
                frame = _encode_batch_result(b_id, b_dict, b_errors, compress)
            except Exception as e:
                logger.error(COLOR.red(f"Worker {worker_id} failed to serialize batch {b_id}: {e}"))
                tb_str = traceback.format_exc()
                failed = [(f, f"Batch {b_id} could not be serialized: {e}", tb_str) for f in b_files]
                frame = _encode_batch_result(b_id, {}, b_errors + failed, compress)
            encode_s[0] += time.perf_counter() - t_encode
            del b_dict
            try:
                _send(frame)
            except OSError:
                # The parent stopped this stage and will terminate the worker
                return
            del frame

    serializer_thread = threading.Thread(target=_serializer_loop, daemon=True)
    serializer_thread.start()

    try:
        while True:
            t_wait = time.perf_counter()
            task = _recv_task(conn)
            t_task = time.perf_counter()
            split["task_wait_s"] += t_task - t_wait
            if task is None:
                # Stop frame (or the parent went away)
                break

            batch_id, changed_files = task
            batch_cs_dict = {}
            batch_errors: list[tuple[str, str, str]] = []

            for f_idx, changed_file in enumerate(changed_files):
                try:
                    CS = ChangeSet(changed_file)
                    CS.current_vid = vid
                    CS.gp = gp_ref
                    CS.mf = mf_ref
                    CS.batch_cs_dict = batch_cs_dict
                    if not _attach_blob_hash(CS, getattr(gp_ref, "file_blob_hashes", None)):
                        batch_errors.append((
                            changed_file, "No git blob hash in this version (not a regular file?); skipped.", ""
                        ))
                        continue
                    G.CURRENT_PARSING_FILE = CS.current_path

                    default_processing(CS)
                    CS.parse()

                    # Pre-unpack intra-file AST view schemas in parallel workers to distribute CPU load
                    t_phase = time.perf_counter()
                    if not G.VERY_LOW_MEMORY_MODE:
                        CS.preprocess_ref_views()
                    t_views = time.perf_counter()
                    split["ref_views_s"] += t_views - t_phase

                    # Pre-resolve foreign references against symbols published by earlier stages and epochs
                    if shared_symbols:
                        CS.pre_resolve_operations(shared_symbols)
                    t_resolve = time.perf_counter()
                    split["pre_resolve_s"] += t_resolve - t_views

                    # In normal memory mode, pre-resolve unchanged DB dependencies to prune foreign_deps
                    if not G.LOW_MEMORY_MODE:
                        CS.prune_unchanged_dependencies()
                    split["prune_s"] += time.perf_counter() - t_resolve

                    # Clean bloat and store in batch dict
                    CS.clear_bloat()
                    batch_cs_dict[CS.current_path] = CS
                except Exception as e:
                    err_str = str(e)
                    logger.error(COLOR.red(f"Worker {worker_id} error parsing '{changed_file}': {err_str}"))
                    batch_errors.append((changed_file, err_str, traceback.format_exc()))
                finally:
                    G.CURRENT_PARSING_FILE = None

                # Periodic memory reclamation in low/very-low memory modes
                if G.LOW_MEMORY_MODE and (f_idx + 1) % 25 == 0:
                    reclaim_system_memory()

            t_put = time.perf_counter()
            split["busy_s"] += t_put - t_task
            # Blocks while two finished batches wait for the parent to read them
            worker_serialize_queue.put((batch_id, changed_files, batch_cs_dict, batch_errors))
            t_queued = time.perf_counter()
            split["send_wait_s"] += t_queued - t_put
            del batch_cs_dict
            reclaim_system_memory()
            split["gc_s"] += time.perf_counter() - t_queued
    finally:
        while serializer_thread.is_alive():
            try:
                worker_serialize_queue.put(None, timeout=1.0)
                break
            except queue.Full:
                continue
        serializer_thread.join()
        if getattr(G.TE, "db", None) is not None:
            try:
                G.TE.db.close()
            except Exception:
                pass
            G.TE.db = None
        reclaim_system_memory()
        split["encode_s"] = encode_s[0]
        split["db_reads"] = getattr(G.TE, "db_reads", 0) - reads_base[0]
        split["db_read_s"] = getattr(G.TE, "db_read_s", 0.0) - reads_base[1]
        split["symbol_stats"] = dict(SYMBOL_STATS - symbols_base)
        if gp_ref is not None:
            split["blob_symbols"] = getattr(gp_ref, "_blob_symbols_fetched", None) or {}
            split["blob_symbols_used"] = list(getattr(gp_ref, "_blob_symbols_used", ()))
        try:
            _send(_FRAME_DONE + pickle.dumps(split, protocol=pickle.HIGHEST_PROTOCOL))
        except OSError:
            pass
        conn.close()
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
            if not _attach_blob_hash(CS, getattr(gp, "file_blob_hashes", None)):
                logger.error(COLOR.red(f"Skipping '{changed_file}': no git blob hash in this version (not a regular file?)"))
                continue
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


def processing_unchanges() -> list[str]:
    """Carry files outside gp.Change_List forward into gp.VID and return the files that still need parsing.

    Files the diff missed (forgotten deletes and additions) and unchanged files absent from the previous
    version (self-healing) are returned as change lines for the parsing workers instead of being parsed
    serially here.
    """
    to_parse: list[str] = []
    if gp.Old_VID == 0:
        return to_parse
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
        to_parse.extend(f"D\t{x}" for x in sorted(forgotten_delete))

    forgotten_new = (full_set - old_full_set) - changed_set
    if forgotten_new:
        logger.warning(f"Found {len(forgotten_new)} forgotten new files... Processing...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            logger.debug(forgotten_new)
        to_parse.extend(f"A\t{x}" for x in sorted(forgotten_new))

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
            to_parse.extend(sorted(missing_regs))

        if missing_syms:
            if not hasattr(gp, "Symlink_List") or gp.Symlink_List is None:
                gp.Symlink_List = []
            gp.Symlink_List.extend(missing_syms)

    return to_parse


def processing_dirs() -> dict[str, ChangeSet]:  # noqa: C901
    """Carry unchanged directories forward into gp.VID and return ChangeSets for new directories."""
    dir_changesets: dict[str, ChangeSet] = {}
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
            dir_changesets[single_dir] = CS

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
            dir_changesets[single_dir] = CS
    return dir_changesets


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


def _load_blob_tags(blob_ids: Iterable[int]) -> dict[int, list[tuple[int, int, int, int]]]:
    """Tags of already-parsed blobs as `blob_id -> [(tag_id, blob_id, line_s, line_e), ...]` from m_bridge_tag."""
    wanted = sorted(set(blob_ids))
    tags: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
    if not wanted:
        return tags
    with G.DB() as db:
        cur = _raw_cursor(db)
        if cur is None:
            for blob_id in wanted:
                for row in m_bridge_tag.view_get_multiple(((m_bridge_tag.blob_id,),), blob_id, None, None, None, None, None) or ():
                    tags[blob_id].append((row[1], blob_id, row[2], row[3]))
            return tags
        try:
            for i in range(0, len(wanted), _SQL_IN_CHUNK):
                chunk = wanted[i : i + _SQL_IN_CHUNK]
                cur.execute(
                    f"SELECT blob_id, tag_id, line_s, line_e FROM m_bridge_tag WHERE blob_id IN ({','.join(['%s'] * len(chunk))})",  # noqa: S608
                    tuple(chunk),
                )
                for blob_id, tag_id, line_s, line_e in cur.fetchall():
                    tags[int(blob_id)].append((int(tag_id), int(blob_id), int(line_s), int(line_e)))
        finally:
            cur.close()
    return tags


def processing_git_commits(version: str) -> None:
    """Parse git commits for active version, link contributors, and bridge tags to commits."""
    from parser.git_ast import GitCommitParser
    repo_path = getattr(gp, "repo_path", None) or MF.get_repo_dir()
    git_parser = GitCommitParser(repo_dir=repo_path)
    network_id = getattr(gp, "network_id", 1)

    commits = None
    file_hunks_map = None
    precomputed = _collect_git_commit_result()
    if precomputed is not None:
        commits, file_hunks_map = precomputed

    if commits is None:
        commits, file_hunks_map = git_parser.parse_version_commits_with_hunks(
            gp.Old_Version_Name, gp.Version_Name
        )
    if not commits:
        return

    logger.info(f"Processing {len(commits)} git commits for version '{version}'...")

    # Commit ids of the range: Step 1 (ingest_git_dag) registered them, anything else is looked up in bulk
    commit_hash_to_id: dict[str, int] = {}
    known_ids = dict(getattr(gp, "commit_ids_by_hash", None) or {})
    unknown = [h for c in commits for h in (c.commit_hash, *(c.parents or ())) if h not in known_ids]
    if unknown:
        with G.DB() as db:
            known_ids.update(_lookup_commit_ids(db, network_id, unknown))

    pending_parents: list[tuple[int, list[str]]] = []
    head_commit_id = None
    for commit in commits:
        if G.is_deadline_exceeded():
            logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during commit processing. Stopping early..."))
            break
        c_hash = commit.commit_hash
        commit_id = known_ids.get(c_hash)
        c_match = commit_id is not None
        if not c_match:
            # Format commit_hash as 20 bytes
            if isinstance(c_hash, str):
                c_hash_bytes = bytes.fromhex(c_hash) if len(c_hash) == 40 else c_hash.encode("ascii")
            else:
                c_hash_bytes = c_hash
            author_res = G.TE.set(m_maintainer_person.table_id, (None, *_person_key(commit.author_name, commit.author_email)))
            author_pid = author_res[0] if author_res else 1
            committer_res = G.TE.set(
                m_maintainer_person.table_id,
                (None, *_person_key(commit.committer_name or commit.committer_email or commit.author_name,
                                    commit.committer_email or commit.author_email)),
            )
            committer_pid = committer_res[0] if committer_res else author_pid
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
                c_res = G.TE.set(m_maintainer_person.table_id, (None, *_person_key(contrib.name, contrib.email)))
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

    # Link parents now that every commit of the range has an id; parents outside the range were looked up above
    for commit_id, parent_hashes in pending_parents:
        for p_idx, p_hash in enumerate(parent_hashes):
            p_cid = commit_hash_to_id.get(p_hash) or known_ids.get(p_hash)
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

    # Files reused by Step 4.5's blob deduplication were not parsed: their tags come from m_bridge_tag
    deduped = [
        p for p in sorted(getattr(gp, "deduped_paths", ()))
        if file_hunks_map and p in file_hunks_map and p not in gp.file_pre_extracted_tags and gp.file_blob_ids.get(p)
    ]
    if deduped:
        blob_tags = _load_blob_tags({gp.file_blob_ids[p] for p in deduped})
        for c_path in deduped:
            file_tags = blob_tags.get(gp.file_blob_ids[c_path])
            if not file_tags:
                continue
            tag_bridges = git_parser.map_tags_to_commits(
                file_tags,
                c_path,
                commit_hash_to_id=commit_hash_to_id,
                file_hunks_map=file_hunks_map,
            )
            for cid, b_id, tid in tag_bridges:
                G.TE.set(m_bridge_commit_tag.table_id, (cid, b_id, tid))

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
    new_blob_ids: set[int] | None = None,
) -> None:
    """Parse Makefile and Kbuild files for active version, map Kconfig symbols, and stage build references.

    `new_blob_ids` limits m_file_reference staging to Makefile blobs whose references were cleared for
    recomputation (None: every blob); m_kconfig_kbuild rows are staged for every Makefile.
    """
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
                    if new_blob_ids is not None and mk_fid not in new_blob_ids:
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
    new_blob_ids: set[int] | None = None,
) -> None:
    """Scan documentation and text files for active version and map cross-file references into m_file_reference.

    `new_blob_ids` limits the scan to documents whose references were cleared for recomputation (None: all).
    """
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
    if new_blob_ids is not None:
        doc_paths = [f for f in doc_paths if get_fid_for_path(f) in new_blob_ids]
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
    """Batch populate C/ASM and Kconfig include references into m_file_reference using set-based query.

    Only blobs that are new in this version (absent from gp.Old_VID's checkpoint) are processed: their
    previous rows were cleared by processing_file_references(), and carried-over blobs keep theirs.
    """
    if gp.VID == 0:
        return
    try:
        with G.DB() as db:
            cur = db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None
            if cur is None:
                return

            sync_query = f"""
                INSERT IGNORE INTO m_file_reference (source_blob_id, target_fnid, ref_type, line_no, details)
                SELECT STRAIGHT_JOIN DISTINCT bt.blob_id, inc.fnid,
                       CASE WHEN a.type_id IN ({ASTT.Kconfig_Source.value}, {ASTT.Kconfig_Rsource.value}) THEN {int(FileRefType.Kconfig)} ELSE {int(FileRefType.Include)} END AS ref_type,
                       bt.line_s,
                       LEFT(a.name, 255)
                FROM m_bridge_checkpoint bf
                LEFT JOIN m_bridge_checkpoint old_v ON old_v.blob_id = bf.blob_id AND old_v.commit_id = %s
                JOIN m_bridge_tag bt ON bf.blob_id = bt.blob_id
                JOIN m_tag t ON bt.tag_id = t.tag_id
                JOIN m_ast_include inc ON t.ast_id = inc.ast_id
                JOIN m_ast a ON inc.ast_id = a.ast_id
                WHERE bf.commit_id = %s AND old_v.blob_id IS NULL;
            """
            cur.execute(sync_query, (gp.Old_VID or 0, gp.VID))
            inserted = cur.rowcount
            db.cnx.commit()
            cur.close()
            logger.info(f"Synchronized {inserted} include/Kconfig references into m_file_reference for version '{version}'.")
    except Exception as e:
        logger.warning(f"Error in processing_include_references for {version}: {e}", exc_info=True)


def _new_version_blob_ids() -> tuple[set[int], set[int]] | None:
    """Blob ids of this version's tree, and the subset absent from gp.Old_VID's tree (new in this version).

    Returns None when the driver has no SQL access (MockDB).
    """
    with G.DB() as db:
        cur = _raw_cursor(db)
        if cur is None:
            return None
        try:
            cur.execute("SELECT DISTINCT blob_id FROM m_bridge_checkpoint WHERE commit_id = %s", (gp.VID,))
            current = {int(r[0]) for r in cur.fetchall()}
            previous: set[int] = set()
            if gp.Old_VID:
                cur.execute("SELECT DISTINCT blob_id FROM m_bridge_checkpoint WHERE commit_id = %s", (gp.Old_VID,))
                previous = {int(r[0]) for r in cur.fetchall()}
        finally:
            cur.close()
    return current, current - previous


def processing_file_references(version: str) -> None:
    """Extract and consolidate all cross-file references (Kbuild, Makefiles, Documentation, Includes, Kconfig).

    m_file_reference rows are keyed by source blob, so only blobs that are new in this version (absent
    from gp.Old_VID's tree) are cleared and recomputed; blobs carried over keep the rows computed when they
    first appeared.
    """
    if gp.VID == 0:
        return

    # Ensure all parent tables (m_file_blob, m_file_name, m_bridge_checkpoint) are committed before referencing
    G.TE.commit_all(update_in_mem_indexes=False)

    scope = _new_version_blob_ids()
    valid_blob_ids, new_blob_ids = scope if scope is not None else (None, None)
    if new_blob_ids is not None:
        logger.info(f"Recomputing file references for {len(new_blob_ids)} of {len(valid_blob_ids)} blobs new in version '{version}'.")

    # 1. Clear the rows of the blobs about to be recomputed (idempotent across pipeline re-runs)
    try:
        with G.DB() as db:
            cur = _raw_cursor(db)
            if cur:
                cur.execute(
                    """
                    DELETE r FROM m_file_reference r
                    JOIN m_bridge_checkpoint cur_v ON cur_v.blob_id = r.source_blob_id AND cur_v.commit_id = %s
                    LEFT JOIN m_bridge_checkpoint old_v ON old_v.blob_id = cur_v.blob_id AND old_v.commit_id = %s
                    WHERE old_v.blob_id IS NULL;
                    """,
                    (gp.VID, gp.Old_VID or 0),
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

    # Rows staged for one source blob in this run (rows of other blobs were cleared or are not recomputed)
    staged_file_refs: set[tuple[int, int, int, int]] = set()

    # 3. Parse Kbuild and Documentation files and stage into TableEngine (fnids come from the committed TE cache)
    processing_kbuild(version, staged_file_refs=staged_file_refs, valid_blob_ids=valid_blob_ids, new_blob_ids=new_blob_ids)
    processing_doc_references(version, staged_file_refs=staged_file_refs, valid_blob_ids=valid_blob_ids, new_blob_ids=new_blob_ids)

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
    # Rows are keyed by the MAINTAINERS blob: files mapped for it in an earlier version need no matching
    already_mapped: set[int] = set()
    with G.DB() as db:
        cur = _raw_cursor(db)
        if cur is not None:
            try:
                cur.execute("SELECT DISTINCT fnid FROM m_maintainer_file WHERE maintainers_blob_id = %s", (maintainers_blob_id,))
                already_mapped = {int(r[0]) for r in cur.fetchall()}
            finally:
                cur.close()
    if already_mapped:
        logger.info(f"{len(already_mapped)} files are already mapped for this MAINTAINERS blob; matching the others only.")

    matched_count = 0
    seen_pairs: set[tuple[int, int]] = set()
    for file_path in all_files:
        if G.is_deadline_exceeded():
            logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during maintainer matching. Stopping early..."))
            break
        fnid = get_fnid_for_path(file_path)
        if fnid is None or fnid in already_mapped:
            continue
        matched_sections = matcher.match_file(file_path)
        if not matched_sections:
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

