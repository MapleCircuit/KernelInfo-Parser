"""`update()`: the full per-version parse sequence."""
from core.globalstuff import (
    G,
    COLOR,
    reclaim_system_memory,
)
import subprocess as sp

import time
from collections import Counter
from typing import Any
from db_engine import ne
from core.SymbolRows import STATS as SYMBOL_STATS
from core.DBLayout import (
    m_file_name,
    m_ast,
)
from pipeline.arch_layouts import processing_arch_layouts
from pipeline.blob_reuse import _log_preload_summary, _record_reparsed_blobs, _reuse_parsed_blobs
from pipeline.changesets import file_fid_cache, file_fnid_cache
from pipeline.commits import processing_git_commits
from pipeline.external_steps import processing_source_facts
from pipeline.file_references import processing_file_references
from pipeline.git_commit_worker import _start_git_commit_worker
from pipeline.git_ingest import _ensure_full_checkpoint, create_new_vid, finish_version_ingest
from pipeline.maintainers import processing_maintainer_files
from pipeline.multicore import trigger_multicore
from pipeline.scheduler import DependencyScheduler
from pipeline.state import MF, gp, logger
from pipeline.unchanged import processing_symlinks
from pipeline.worker_stage import WORKER_SYMBOL_STATS, _log_symbol_stats




# Step 2 creates the performance indexes once per process
_PERFORMANCE_INDEXES_READY = False


def _has_peer_nodes() -> bool:
    """True if another parser node is currently registered in `m_parse_node` (so deferred `m_ast_hash` writes are needed)."""
    node_id = getattr(G, "node_id", "") or ""
    network_id = getattr(gp, "network_id", None) or 1
    try:
        with G.DB() as db:
            if db is None:
                return True
            where: dict[str, Any] = {"network_id": network_id}
            if node_id:
                where["node_id"] = ne(node_id)
            row = db.select_one("m_parse_node", ["node_id"], where, as_dict=False)
            return row is not None
    except Exception:
        # Fail safe: if we cannot check, defer as distributed mode normally requires
        return True


def update(version: str, repo_path: str | None = None) -> None:
    """Execute the full version parsing and database ingestion pipeline for a target release version.
    
    Workflow Steps:
    1. Register/synchronize version in `m_git_ref` via `create_new_vid()`.
    1.5 Expand the previous version's checkpoint if it is stored as a delta (core/checkpoints.py).
    2. Ensure the declared lookup indexes the workers rely on (`ast_index`, `file_name_index`).
    3. Bring the RAMDISK workspace to the version via `MF.checkout_version()` (incremental from the previous version).
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

    # -------------------------------------------------------------------------
    # STEP 1.5: The previous version's checkpoint is read as a full tree (TableEngine preload, unchanged files,
    # new (path, blob) pairs): expand it when it is stored as a delta (a backfill or repair base)
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 1.5: Previous Checkpoint"):
        _ensure_full_checkpoint(gp.Old_VID)

    # Ensure TableEngine in-memory indexing is enabled for the new update cycle
    if hasattr(G.TE, "update_in_mem_indexes"):
        G.TE.update_in_mem_indexes = True

    # -------------------------------------------------------------------------
    # STEP 2: Ensure performance B-tree indexes are active for worker queries (in parallel)
    # -------------------------------------------------------------------------
    # Both are declared in core/DBLayout.py; this only recreates them on a database that lost them. A
    # (blob_id, tag_id) index on m_bridge_tag or a (target_fnid, ref_type) one on m_file_reference would
    # duplicate the primary key / fref_tgt_src_idx (1.8 GB and 0.2 GB on a full database)
    performance_indexes = (
        ("ast_index", m_ast, (m_ast.name, m_ast.type_id)),
        ("file_name_index", m_file_name, (m_file_name.fname,)),
    )
    global _PERFORMANCE_INDEXES_READY
    with G.measure_stage("Step 2: Performance Index Setup", count=len(performance_indexes), units="indexes"):
        if not _PERFORMANCE_INDEXES_READY:
            with G.DB() as db:
                db.create_indexes(performance_indexes)
            # Once per process: a --reset happens before update() is first called
            _PERFORMANCE_INDEXES_READY = True

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Step 2. Halting '{version}' early."))
        return

    # -------------------------------------------------------------------------
    # STEP 3: Clone repository branch version to RAMDISK workspace
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 3: Branch Checkout"):
        # Rewrites only the files that differ from the previous version when the workspace holds it
        MF.checkout_version(version, gp.Old_Version_Name, gp.PURGE_LIST)

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Step 3. Halting '{version}' early."))
        return

    # -------------------------------------------------------------------------
    # STEP 4: Generate git diff change list & extract Git blob hashes
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 4: Tree Diff & Blob Hashes"):
        MF.generate_change_list(gp)

        if hasattr(MF, "git_tree_blobs"):
            file_blob_hashes = MF.git_tree_blobs(version)
        else:
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
            file_blob_hashes = {}
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
        if hasattr(G.TE, "distributed"):
            G.TE.distributed = getattr(G, "distributed", False)
            G.TE.current_commit_id = getattr(gp, "VID", 0)
            G.TE.defer_ast_hash = _has_peer_nodes() if G.TE.distributed else False
        G.TE.start(gp.Table_Array, G.DB)
        _log_preload_summary(time.perf_counter() - t_preload)

    # -------------------------------------------------------------------------
    # STEP 4.5: Content-Addressable Blob Deduplication across Git trees
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 4.5: Content-Addressable Blob Dedup", units="reused") as dedup_metric:
        gp.worker_change_list = _reuse_parsed_blobs(file_blob_hashes)
        if dedup_metric is not None:
            dedup_metric.count = len(gp.deduped_paths)
        _record_reparsed_blobs(file_blob_hashes)

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Step 4.5. Finalizing '{version}' early."))
        if getattr(G.TE, "defer_ast_hash", False):
            G.TE.defer_ast_hash = False
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

    # scheduler.finalize() already committed and reclaimed memory; the caches stay for Step 6.1, which stages
    # m_file_name / m_file_blob rows whose deduplication needs them
    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached after Multicore Parsing. Finalizing '{version}'..."))
        if getattr(G.TE, "defer_ast_hash", False):
            G.TE.defer_ast_hash = False
        G.TE.commit_all(update_in_mem_indexes=False)
        G.TE.close()
        return

    # -------------------------------------------------------------------------
    # STEP 6.1: Resolve Symbolic Links & Alias directly in m_bridge_file
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 6.1: Symbolic Links Resolution"):
        processing_symlinks()

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached before Git commits. Finalizing '{version}'..."))
        G.TE.commit_all()
        G.TE.close()
        return

    # -------------------------------------------------------------------------
    # STEP 6.5: Parse Git Commits, Multi-Contributors & Bridge Tags to Commits
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 6.5: Git Commits & Contributors"):
        processing_git_commits(version)

    # Commit git commits, checkpoint files, and git tags staged during git processing
    G.TE.commit_all()

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached before File References. Finalizing '{version}'..."))
        G.TE.commit_all()
        G.TE.close()
        return

    # -------------------------------------------------------------------------
    # STEP 6.6: Parse Kbuild/Makefiles, Documentation & Cross-File References
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 6.6: File References (Kbuild & Docs)"):
        processing_file_references(version)

    if G.is_deadline_exceeded():
        logger.warning(COLOR.yellow(f"[Duration Limit] Execution deadline reached before Maintainers. Finalizing '{version}'..."))
        if getattr(G.TE, "defer_ast_hash", False):
            G.TE.defer_ast_hash = False
        G.TE.commit_all(update_in_mem_indexes=False)
        G.TE.close()
        return

    # -------------------------------------------------------------------------
    # STEP 6.7: Batch Match Maintainer Sections & Populate m_maintainer_file
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 6.7: Maintainer Files Matching"):
        processing_maintainer_files(version)

    # -------------------------------------------------------------------------
    # STEP 6.8: Build layout of every ARCH from the version's Makefiles (m_arch_root)
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 6.8: Architecture Build Layouts", units="rows") as layout_metric:
        stored = processing_arch_layouts(version)
        if layout_metric is not None:
            layout_metric.count = stored

    # -------------------------------------------------------------------------
    # STEP 6.9: Per-blob facts the database lacks (m_blob_facts: calls/ops tables, Kernel API macros, includes,
    # Kconfig definitions, doc labels, man page facts)
    # -------------------------------------------------------------------------
    with G.measure_stage("Step 6.9: Source Facts", units="blobs") as facts_metric:
        extracted = processing_source_facts(version)
        if facts_metric is not None:
            facts_metric.count = extracted

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
        if getattr(G.TE, "defer_ast_hash", False):
            G.TE.defer_ast_hash = False
        G.TE.commit_all(update_in_mem_indexes=False)

    file_fid_cache.clear()
    file_fnid_cache.clear()
    MF.clear_version_cache(version)
    MF.trim_version(keep=1)
    G.TE.close()
    if hasattr(G.TE, "update_in_mem_indexes"):
        G.TE.update_in_mem_indexes = True
    finish_version_ingest(gp.VID)
    reclaim_system_memory()
    return
