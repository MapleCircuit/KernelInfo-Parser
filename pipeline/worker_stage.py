"""Parent side of one worker-pool stage: batches, background commits, time splits."""
from core.globalstuff import (
    G,
    COLOR,
    reclaim_system_memory,
    freeze_system_memory,
)

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
from core.globalstuff import MEMORY_RECLAIM_STATS
from core.SymbolRows import STATS as SYMBOL_STATS
from core.DBLayout import (
    m_file_name,
)
from pipeline.file_processing import file_processing_worker
from pipeline.state import MF, _MP, gp, logger
from pipeline.systemic import _raise_if_systemic
from pipeline.worker_protocol import _FRAME_DONE, _FRAME_INIT_FAILED, _FRAME_RESULT, _FRAME_STOP, _FRAME_TASK, _PREFETCH_DEPTH, _STAGE_END, _TASKS_AHEAD, _WORKER_SPLIT_KEYS, _decode_batch_result




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


def _commit_all_with_retry(context: str) -> None:
    """Commit every staged TableEngine row, re-checking the DB connection once on a transient failure."""
    try:
        G.TE.commit_all()
    except Exception as e:
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
    # Per-file errors the workers returned in this stage, to stop on an error that hits most files
    stage_file_errors: list[tuple[str, str, str]] = []
    files_returned = 0
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
                for f in b_items:
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
