"""Parent side of one worker-pool stage: batches, background commits, time splits."""
from core.globalstuff import (
    G,
    COLOR,
    reclaim_system_memory,
    freeze_system_memory,
)

import os
import time
from typing import Any
from collections.abc import Callable
from dataclasses import dataclass, field
import math
from multiprocessing.connection import wait as mp_wait
import queue
import signal
import threading
from collections import Counter, deque
import pickle
import traceback
from table_engine.shared_buffer import SharedSymbolBuilder, SharedSymbolChain
from core.config import get_parser_config
from db_engine import is_retryable_error
from core.ssh_tunnel import ensure_ssh_tunnel
from core.globalstuff import MEMORY_RECLAIM_STATS
from core.SymbolRows import STATS as SYMBOL_STATS
from core.DBLayout import (
    m_file_name,
)
from pipeline.file_processing import file_processing_worker
from pipeline.state import MF, _MP, gp, logger
from pipeline.systemic import _raise_if_systemic
from pipeline.worker_protocol import _FRAME_DONE, _FRAME_INIT_FAILED, _FRAME_READY, _FRAME_RESULT, _FRAME_STOP, _FRAME_TASK, _PREFETCH_DEPTH, _STAGE_END, _TASKS_AHEAD, _WORKER_SPLIT_KEYS, _decode_batch_result




@dataclass
class WorkerStageResult:
    """Outcome of one `_run_worker_stage()` call."""

    files_ingested: int = 0
    failed_workers: int = 0
    deadline_hit: bool = False
    elapsed_s: float = 0.0
    tail_idle_s: float = 0.0
    # Items no worker ingested; `crashed_files` are those of them whose single-file batch killed or stalled a
    # worker in the retry round; `raw_fallback_files` the crashed items the raw fallback stage ingested instead
    lost_files: list[str] = field(default_factory=list)
    crashed_files: list[str] = field(default_factory=list)
    raw_fallback_files: list[str] = field(default_factory=list)
    # Where the stage's time went (see _stage_time_split())
    split: dict[str, float] = field(default_factory=dict)


# Seconds a worker may hold its oldest unreturned batch before the watchdog kills it (`parser.worker_batch_timeout_s`)
DEFAULT_BATCH_TIMEOUT_S = 300.0
# Without any returned batch for this long, the reader logs which workers hold which batches
_STALL_HEARTBEAT_S = 60.0
# Seconds a stalled worker gets to write its SIGUSR1 stack dump before it is terminated
_STALL_DUMP_GRACE_S = 1.5
# Extra seconds a worker gets on its first batch until its ready frame arrives (start-up: DB connection, cache
# detach); a worker that sends none (an older worker body) keeps the grace until its first result frame
_WORKER_INIT_GRACE_S = 120.0
# Seconds a single-file batch of a retry round may take (`parser.worker_retry_file_timeout_s`; capped by the
# batch timeout, <= 0 keeps the batch timeout)
DEFAULT_RETRY_FILE_TIMEOUT_S = 90.0
# Seconds a worker that sent its done frame may take to exit (close its pipe) before it is killed
_DONE_EXIT_GRACE_S = 60.0
# Seconds between SSH tunnel liveness checks while the parent waits for worker results
_TUNNEL_CHECK_S = 30.0


def _configured_batch_timeout() -> float:
    """`parser.worker_batch_timeout_s` (env WORKER_BATCH_TIMEOUT_S); <= 0 disables the stall watchdog."""
    try:
        return float(get_parser_config().get("worker_batch_timeout_s", DEFAULT_BATCH_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_BATCH_TIMEOUT_S


def _configured_retry_file_timeout() -> float:
    """`parser.worker_retry_file_timeout_s` (env WORKER_RETRY_FILE_TIMEOUT_S); <= 0 keeps the batch timeout."""
    try:
        return float(get_parser_config().get("worker_retry_file_timeout_s", DEFAULT_RETRY_FILE_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_FILE_TIMEOUT_S


def _ensure_db_tunnel(label: str) -> None:
    """Restart a dead database SSH tunnel (owner process only; no-op without a tunnel)."""
    try:
        ensure_ssh_tunnel("database")
    except Exception as e:
        logger.warning(COLOR.yellow(f"{label}: SSH tunnel liveness check failed: {e}"))


def _stop_process(p: Any, label: str, grace_s: float = 2.0) -> None:
    """Terminate `p` and kill it if it is still alive `grace_s` later."""
    try:
        if p.is_alive():
            p.terminate()
            p.join(timeout=grace_s)
        if p.is_alive():
            p.kill()
            p.join(timeout=grace_s)
    except Exception as e:
        logger.warning(f"{label}: stopping worker PID {getattr(p, 'pid', '?')} failed: {e}")


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


def _register_file_names(items: list[str]) -> None:
    """Assign m_file_name ids to every path in `items` and commit them before workers fork.

    Workers find these ids in the inherited cache instead of allocating colliding ids of their own.
    """
    paths = [p for item in items if item for p in item.split("\t")[1:] if p]
    # One chunked read for the paths another node may have registered, instead of one read per new path
    G.TE.prefetch_nodup(m_file_name.table_id, [(None, p) for p in paths])
    for p in paths:
        G.TE.set(m_file_name.table_id, (None, p))
    G.TE.commit(m_file_name.table_id)


def _raise_if_not_retryable(context: str, e: BaseException) -> None:
    """Re-raise a commit error a retry would only repeat (`db_engine.is_retryable_error()`: a duplicate key, ...)."""
    if not is_retryable_error(e):
        logger.error(f"{context} TableEngine commit failed with a non-retryable error ({type(e).__name__}: {e}); not retrying.")
        raise e


def _commit_all_with_retry(context: str) -> None:
    """Commit every staged TableEngine row, re-checking the DB connection once on a transient failure.

    A non-retryable error (`db_engine.is_retryable_error()`: duplicate key, malformed spec, missing table, ...) is
    raised at once: the rows that failed are back in staging and would fail the same way again.
    """
    try:
        G.TE.commit_all()
    except Exception as e:
        _raise_if_not_retryable(context, e)
        logger.warning(f"{context} TableEngine commit hit transient error ({e}); re-verifying DB connection and retrying...")
        if getattr(G.TE, "db", None) is not None:
            G.TE.db.check_if_connected()
        G.TE.commit_all()


def _begin_background_commits() -> None:
    """Let in-stage commits run on the TableEngine's writer thread while this thread keeps ingesting."""
    if hasattr(G.TE, "wait_for_commit"):
        G.TE.async_commits = True


def _end_background_commits(context: str) -> None:
    """Switch background commits off and wait for the last one (nothing may fork while a writer runs).

    A failed background commit put its rows back into staging; they are committed again here unless its error is
    non-retryable (`db_engine.is_retryable_error()`), which is raised.
    """
    if not hasattr(G.TE, "wait_for_commit"):
        return
    G.TE.async_commits = False
    try:
        G.TE.wait_for_commit()
    except Exception as e:
        _raise_if_not_retryable(context, e)
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
        _raise_if_not_retryable("Background", e)
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
            f"ref views {split.get('worker_ref_views_s', 0.0):.1f}s, "
            f"pre-resolve {split.get('worker_pre_resolve_s', 0.0):.1f}s, "
            f"encode {split.get('worker_encode_s', 0.0):.1f}s, "
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
    so the parent's heap copies (including `resolved_symbols` / `resolved_names` on evacuated ChangeSets)
    are dropped once packed.
    """
    published_paths = list(gp.file_symbols.keys())
    if gp.file_symbols or gp._ast_staged_symbols or gp._ast_staged_names:
        t_build = time.perf_counter()
        builder = SharedSymbolBuilder(mode=getattr(G.TE, "hugepages", "auto"))
        segment = builder.build_file_scoped(
            gp.file_symbols,
            extra_symbols=gp._ast_staged_symbols,
            extra_names=gp._ast_staged_names,
        )
        if segment is not None:
            if gp._shared_symbols is None:
                gp._shared_symbols = SharedSymbolChain()
            gp._shared_symbols.append(segment)
            logger.info(
                f"[Stage 2 - Sources] Published {segment.sym_count} symbols of {len(gp.file_symbols)} files into shared "
                f"segment {len(gp._shared_symbols.segments)} ({segment.nbytes // (1024 * 1024)}MB, "
                f"{time.perf_counter() - t_build:.1f}s); {gp._shared_symbols.sym_count} shared symbols in total."
            )
    cs_dict = getattr(gp, "ChangeSet_Dict", None)
    if cs_dict is not None and published_paths:
        evacuated_store = getattr(cs_dict, "_evacuated_store", None)
        lru_cache = getattr(cs_dict, "_lru_cache", None)
        for path in published_paths:
            if evacuated_store is not None:
                cs_obj = evacuated_store.get(path) or (lru_cache.get(path) if lru_cache is not None else None)
            else:
                cs_obj = cs_dict.get(path)
            if cs_obj is not None:
                if hasattr(cs_obj, "resolved_symbols"):
                    cs_obj.resolved_symbols = None
                if hasattr(cs_obj, "resolved_names"):
                    cs_obj.resolved_names = None
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
    batch_timeout_s: float | None = None,
    raw_fallback: bool = True,
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

    Stall watchdog: a worker that holds its oldest unreturned batch for more than `batch_timeout_s` seconds
    (default `parser.worker_batch_timeout_s`; <= 0 disables it) without a result frame pending on its pipe is
    logged with the batch's files, sent `SIGUSR1` (stack dump to stderr), terminated and killed, then handled
    like a worker that died: the batch is retried one file per batch. Until a worker's ready frame (or, without
    one, its first result frame) the limit grows by `_WORKER_INIT_GRACE_S`; the ready frame restarts the clock of
    its first batch. In a retry round (`retry_lost=False`) a single-file batch is limited to
    `parser.worker_retry_file_timeout_s` when that is lower.

    Raw fallback (`raw_fallback`, first round only): the items whose single-file batch killed or stalled a worker
    in the retry round are parsed once more on new workers with `gp.raw_fallback_paths` naming them, so
    `ChangeSet.parse()` reads them with `raw_ast_parse` (no libclang) and the version still records their
    blob and checkpoint rows; their `error_list` entry says so. Items lost there too stay in `lost_files`. A worker that sent its done frame but
    does not exit within `_DONE_EXIT_GRACE_S` is killed. Every `_STALL_HEARTBEAT_S` without a returned batch
    the reader logs the batches still in flight; while waiting, the parent keeps the database SSH tunnel alive.
    """
    result = WorkerStageResult()
    if not batches:
        if on_started is not None:
            on_started()
        return result

    target = worker_target or file_processing_worker
    stall_timeout_s = float(_configured_batch_timeout() if batch_timeout_s is None else batch_timeout_s)
    retry_file_timeout_s = _configured_retry_file_timeout() if not retry_lost else 0.0
    single_file_timeout_s = (
        min(stall_timeout_s, retry_file_timeout_s) if retry_file_timeout_s > 0 else stall_timeout_s
    )
    batch_files = dict(batches)
    num_workers = max(1, min(num_workers, len(batches)))
    t_start = time.perf_counter()

    procs: dict[int, Any] = {}
    conns: dict[Any, int] = {}
    _end_background_commits(label)
    # Workers connect through the tunnel as soon as they start
    _ensure_db_tunnel(label)
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
    # Watchdog state (reader thread only): when each worker started its oldest unreturned batch (reset when a
    # batch reaches an idle worker and on every result frame), when it sent its done frame, which it killed
    last_activity: dict[int, float] = {}
    done_at: dict[int, float] = {}
    stalled_workers: dict[int, tuple[int, float]] = {}
    # Stalled batch -> the limit it exceeded
    stalled_batches: dict[int, float] = {}
    # Workers that sent their ready frame or returned at least one result frame (past their start-up)
    started_workers: set[int] = set()

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
                if not assigned[worker_id]:
                    last_activity[worker_id] = time.monotonic()
                assigned[worker_id].append(b_id)
                conn.send_bytes(_FRAME_TASK + pickle.dumps((b_id, b_files), protocol=pickle.HIGHEST_PROTOCOL))
            else:
                stopped.add(worker_id)
                conn.send_bytes(_FRAME_STOP)
        except OSError:
            # The worker is gone; its EOF is handled by the read loop
            pass

    def _worker_lost(open_conns: dict[Any, int], conn: Any, worker_id: int) -> None:
        """A worker's pipe closed (or the watchdog killed it): re-queue the batches it had not returned."""
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

    def _has_pending_frame(conn: Any) -> bool:
        try:
            return bool(conn.poll(0))
        except (EOFError, OSError):
            return True  # closed: the next wait reports it

    def _batch_limit(worker_id: int) -> float:
        """Seconds `worker_id` may hold its oldest unreturned batch."""
        if worker_id not in started_workers:
            return stall_timeout_s + _WORKER_INIT_GRACE_S
        if len(batch_files.get(assigned[worker_id][0], ())) == 1:
            return single_file_timeout_s
        return stall_timeout_s

    def _kill_stalled(open_conns: dict[Any, int], conn: Any, worker_id: int, elapsed: float, limit: float) -> None:
        p = procs[worker_id]
        b_id = assigned[worker_id][0]
        files = batch_files.get(b_id, [])
        stalled_workers[worker_id] = (b_id, elapsed)
        stalled_batches[b_id] = limit
        logger.error(COLOR.red(
            f"{label}: worker {worker_id} (PID {p.pid}) stalled for {elapsed:.0f}s (limit {limit:.0f}s) "
            f"on batch {b_id} ({len(files)} files: {', '.join(files)}); dumping its stacks to stderr and killing it."
        ))
        sigusr1 = getattr(signal, "SIGUSR1", None)
        if sigusr1 is not None and p.pid:
            try:
                os.kill(p.pid, sigusr1)
                p.join(timeout=_STALL_DUMP_GRACE_S)
            except OSError:
                pass
        _stop_process(p, label)
        _worker_lost(open_conns, conn, worker_id)

    def _watchdog(open_conns: dict[Any, int], now: float) -> None:
        for conn, worker_id in list(open_conns.items()):
            if worker_id in done_workers:
                if now - done_at.get(worker_id, now) > _DONE_EXIT_GRACE_S and not _has_pending_frame(conn):
                    logger.warning(COLOR.yellow(
                        f"{label}: worker {worker_id} (PID {procs[worker_id].pid}) finished but did not exit "
                        f"within {_DONE_EXIT_GRACE_S:.0f}s; killing it."
                    ))
                    _stop_process(procs[worker_id], label)
                    _worker_lost(open_conns, conn, worker_id)
                continue
            if stall_timeout_s <= 0 or not assigned[worker_id] or worker_id in init_failed:
                continue
            elapsed = now - last_activity.get(worker_id, now)
            limit = _batch_limit(worker_id)
            # A result waiting on the pipe means the worker is sending, not stuck
            if elapsed > limit and not _has_pending_frame(conn):
                _kill_stalled(open_conns, conn, worker_id, elapsed, limit)

    def _heartbeat(open_conns: dict[Any, int], now: float, idle_s: float) -> None:
        in_flight = [
            f"worker {worker_id} (PID {procs[worker_id].pid}) batch {assigned[worker_id][0]} "
            f"({len(batch_files.get(assigned[worker_id][0], ()))} files) for {now - last_activity.get(worker_id, now):.0f}s"
            for worker_id in sorted(open_conns.values())
            if assigned[worker_id] and worker_id not in done_workers
        ]
        logger.warning(COLOR.yellow(
            f"{label}: no batch returned for {idle_s:.0f}s; {len(open_conns)} worker(s) still open, "
            f"{len(pending)} batch(es) pending; in flight: {'; '.join(in_flight) or 'none'}"
        ))

    def _reader_loop() -> None:
        open_conns = dict(conns)
        last_progress = last_heartbeat = time.monotonic()
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
                        _worker_lost(open_conns, conn, worker_id)
                        continue
                    tag = frame[:1]
                    if tag == _FRAME_DONE:
                        done_workers.add(worker_id)
                        done_at[worker_id] = time.monotonic()
                        if len(frame) > 1:
                            try:
                                stats = pickle.loads(frame[1:])
                                WORKER_SYMBOL_STATS.update(stats.pop("symbol_stats", None) or {})
                                blob_symbol_reports.append((stats.pop("blob_symbols", None), stats.pop("blob_symbols_used", None)))
                                worker_stats.append(stats)
                            except Exception:
                                pass
                    elif tag == _FRAME_READY:
                        # Start-up is over: the first batch's clock starts now, without the start-up grace
                        if worker_id not in started_workers:
                            started_workers.add(worker_id)
                            if assigned[worker_id]:
                                last_activity[worker_id] = time.monotonic()
                    elif tag == _FRAME_INIT_FAILED:
                        init_failed.add(worker_id)
                        error_list.append(pickle.loads(frame[1:]))
                    elif tag == _FRAME_RESULT:
                        last_progress = time.monotonic()
                        last_activity[worker_id] = last_progress
                        started_workers.add(worker_id)
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
                if not open_conns or stop.is_set():
                    break
                now = time.monotonic()
                _watchdog(open_conns, now)
                if open_conns and now - max(last_progress, last_heartbeat) >= _STALL_HEARTBEAT_S:
                    last_heartbeat = now
                    _heartbeat(open_conns, now, now - last_progress)
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
    # Per-file errors the workers returned in this stage, to stop on an error that hits most files
    stage_file_errors: list[tuple[str, str, str]] = []
    files_returned = 0
    parent_split = {"wait_s": 0.0, "decode_s": 0.0, "ingest_s": 0.0}
    split_base = _parent_split_counters()
    last_tunnel_check = time.monotonic()
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
                if time.monotonic() - last_tunnel_check >= _TUNNEL_CHECK_S:
                    last_tunnel_check = time.monotonic()
                    _ensure_db_tunnel(label)
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
                decoded_payload = pickle.loads(body)
                if isinstance(decoded_payload, tuple) and len(decoded_payload) == 3:
                    batch_dict, batch_errors, batch_blob_symbols = decoded_payload
                    if batch_blob_symbols and hasattr(gp, "blob_symbol_cache"):
                        gp.blob_symbol_cache.update(batch_blob_symbols)
                else:
                    batch_dict, batch_errors = decoded_payload
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
            files_returned += len(batch_files.get(b_id, ()))
            if batch_errors:
                stage_file_errors.extend(batch_errors)
                _raise_if_systemic(label, stage_file_errors, files_returned)
            t_ingest = time.perf_counter()
            try:
                ingest(batch_dict)
                result.files_ingested += len(batch_dict)
            except Exception as e:
                logger.error(COLOR.red(f"{label}: ingesting batch {b_id} failed: {e}"), exc_info=True)
                tb = traceback.format_exc()
                batch_items = batch_files.get(b_id, ())
                if batch_items:
                    ingest_errors = [
                        (item if "\t" in item else f"?\t{item}", f"Ingest failed: {e}", tb)
                        for item in batch_items
                    ]
                    error_list.extend(ingest_errors)
                    stage_file_errors.extend(ingest_errors)
                    _raise_if_systemic(label, stage_file_errors, files_returned)
                else:
                    error_list.append((
                        f"Batch {b_id} (0 files)", f"Ingest failed: {e}", tb
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
        if worker_id in stalled_workers:
            b_id, elapsed = stalled_workers[worker_id]
            logger.error(COLOR.red(
                f"{label}: worker {worker_id} (PID {p.pid}) was killed after stalling {elapsed:.0f}s on batch {b_id}"
            ))
            continue
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
        # Single-file batches that killed a worker twice are lost; multi-file batches (from `unfinished`)
        # are split into single-file batches so one bad file does not fail its batchmates.
        retry_batches = []
        for b_id in suspects:
            b_items = batch_files[b_id]
            if len(b_items) > 1:
                for f in b_items:
                    retry_batches.append((next_id + len(retry_batches), [f]))
            else:
                reason = (
                    f"A worker stalled for more than {stalled_batches[b_id]:.0f}s while parsing this file and was killed"
                    if b_id in stalled_batches else "A worker process died while parsing this file"
                )
                for f in b_items:
                    result.lost_files.append(f)
                    result.crashed_files.append(f)
                    error_list.append((f, f"{reason}; it was not ingested.", ""))
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
        batch_timeout_s=stall_timeout_s,
        raw_fallback=False,
    )
    result.files_ingested += retry.files_ingested
    result.failed_workers += retry.failed_workers
    result.deadline_hit = result.deadline_hit or retry.deadline_hit
    result.lost_files += retry.lost_files
    result.crashed_files += retry.crashed_files
    if raw_fallback and retry_lost and result.crashed_files and not result.deadline_hit:
        _run_raw_fallback(label, result, ingest, num_workers, error_list, target, stall_timeout_s)
    return result


def _run_raw_fallback(
    label: str,
    result: WorkerStageResult,
    ingest: Callable[[dict[str, Any]], None],
    num_workers: int,
    error_list: list[tuple[str, str, str]],
    target: Callable[..., None],
    stall_timeout_s: float,
) -> None:
    """Parse `result.crashed_files` once more with `raw_ast_parse` (see `_run_worker_stage()`), in place.

    A file left out of the version would be "missing from the prior version" in the next one
    (`pipeline/unchanged.py`) and parsed, and lost, again in every later version; ingested raw, its blob is
    recorded once and Step 4.5 reuses it for every later version holding the same blob.
    """
    items = list(result.crashed_files)
    fallback_label = f"{label} [raw fallback]"
    logger.warning(COLOR.yellow(
        f"{fallback_label}: {len(items)} file(s) killed or stalled a worker twice; ingesting them with the raw_ast "
        f"parser instead: {', '.join(items)}"
    ))
    fallback_errors: list[tuple[str, str, str]] = []
    ingested_paths: set[str] = set()

    def _ingest(batch: dict[str, Any]) -> None:
        ingest(batch)
        ingested_paths.update(batch)

    gp.raw_fallback_paths = frozenset(item.split("\t")[-1] for item in items)
    try:
        fallback = _run_worker_stage(
            fallback_label,
            [(i, [item]) for i, item in enumerate(items)],
            _ingest,
            num_workers,
            fallback_errors,
            retry_lost=False,
            worker_target=target,
            batch_timeout_s=stall_timeout_s,
            raw_fallback=False,
        )
    except RuntimeError as e:
        logger.error(COLOR.red(f"{fallback_label}: could not run: {e}"))
        error_list.extend(fallback_errors)
        return
    finally:
        gp.raw_fallback_paths = frozenset()
    result.failed_workers += fallback.failed_workers
    result.deadline_hit = result.deadline_hit or fallback.deadline_hit
    # The fallback's own "not ingested" entries repeat the first ones; its other errors (raw parse) are kept
    error_list.extend(err for err in fallback_errors if not err[1].endswith("it was not ingested."))
    recovered = [item for item in items if item.split("\t")[-1] in ingested_paths]
    pending_recovered = list(recovered)
    for idx, (item, message, tb) in enumerate(error_list):
        if item in pending_recovered and message.endswith("it was not ingested."):
            pending_recovered.remove(item)
            error_list[idx] = (
                item,
                message.removesuffix("it was not ingested.")
                + "it was ingested through the raw_ast fallback (whole-file tag, no language AST).",
                tb,
            )
    for item in recovered:
        if item in result.lost_files:
            result.lost_files.remove(item)
        if item in result.crashed_files:
            result.crashed_files.remove(item)
    result.files_ingested += fallback.files_ingested
    result.raw_fallback_files = recovered
    if recovered:
        logger.warning(COLOR.yellow(f"{fallback_label}: ingested {len(recovered)} file(s) as raw_ast: {', '.join(recovered)}"))
