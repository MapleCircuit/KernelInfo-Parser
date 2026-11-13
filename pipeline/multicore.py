"""Multicore parse driver: source epochs, `trigger_multicore`, DB server tuning."""
from core.globalstuff import (
    G,
    COLOR,
    reclaim_system_memory,
    unfreeze_system_memory,
)
from core.config import (
    get_db_config,
    get_ssh_tunnel_config,
)
import os
import sys

import time
from typing import Any
from collections.abc import Callable
import gc
from db_engine import (
    tuning_note_is_warning,
)
from pipeline.blob_reuse import _parent_rss_mb
from pipeline.changesets import execute_and_purge
from pipeline.scheduler import DependencyScheduler
from pipeline.source_order import order_changed_files, partition_source_epochs
from pipeline.state import MF, gp, logger
from pipeline.systemic import SystemicParseError, _raise_if_systemic
from pipeline.unchanged import processing_dirs, processing_unchanges
from pipeline.worker_stage import _balanced_batch_size, _commit_all_with_retry, _memory_profile, _publish_file_symbols, _register_file_names, _run_worker_stage, _select_worker_changes




def _preflight_libclang() -> None:
    """Exit before parsing when libclang or its Python bindings cannot run the C parser."""
    from parser.c_ast.ctypes_bindings import libclang_self_test
    try:
        logger.info(f"[Preflight] libclang: {libclang_self_test()}")
    except RuntimeError as e:
        logger.critical(COLOR.red(
            f"[Preflight] {e}. The C parser cannot run with these bindings; install Python bindings matching "
            "the libclang library (requirements.txt pins clang==21.1.7) before parsing."
        ))
        sys.exit(1)


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
        largest_first=True,
        num_workers=num_workers,
    )
    header_batches = [b for ep in header_epochs for b in ep]
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
    # Across both stages, which each check on their own while they run; file entries are change items
    # ("M\tpath"), unlike worker and batch entries
    files_parsed = len(header_files) + len(source_files) + (len(extra_files) if header_files else 0)
    file_errors = [e for e in error_list if "\t" in e[0]]
    try:
        _raise_if_systemic(f"Version '{gp.Version_Name}'", file_errors, files_parsed)
    except SystemicParseError:
        gp.close_shared_symbols()
        raise


def _resolve_mem_max(value: Any) -> float:
    """Return the memory guard ceiling in GB from a config value: a number >= 0, else the 60 GB default."""
    try:
        gb = float(value)
    except (TypeError, ValueError):
        return 60.0
    return gb if gb >= 0 else 60.0


def _apply_db_server_tuning(db: Any, mem_max_gb: float) -> None:
    """Raise the server settings listed in `database.server_tuning` (config) for this run.

    The defaults trade an InnoDB buffer pool far above the 128MB default, a larger redo log and a once-per-second
    log flush for commit throughput; the database can be rebuilt with --reset. The engine caps a local server's
    buffer pool at a quarter of RAM and warns when the memory guard plus the pool do not fit
    (`BaseDBEngine.apply_server_tuning()`); engines without server tuning return no notes.
    """
    settings = dict(get_db_config().get("server_tuning") or {})
    if not settings:
        return
    try:
        notes = db.apply_server_tuning(
            settings,
            tunnel_active=bool(get_ssh_tunnel_config().get("enabled")),
            mem_max_gb=mem_max_gb,
        )
    except Exception as e:
        logger.warning(COLOR.yellow(f"[DB Tuning] Could not apply database.server_tuning: {e}"))
        return
    for note in notes:
        if tuning_note_is_warning(note):
            logger.warning(COLOR.yellow(f"[DB Tuning] {note}"))
        else:
            logger.info(f"[DB Tuning] {note}")
