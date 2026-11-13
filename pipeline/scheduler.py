"""Dependency-driven ChangeSet resolution and the parallel wave executor."""
from core.globalstuff import (
    G,
    COLOR,
    REF_FILE,
    OP_REF,
    reclaim_system_memory,
)

import time
from typing import Any
from collections import deque, defaultdict
from core.GreatProcessor import GreatProcessor
from core.TableHandling import ChangeSet, is_data_unsafe
from pipeline.changesets import execute_and_purge, extract_tags_and_evacuate_cs
from pipeline.state import logger
from pipeline.worker_stage import _commit_all_with_retry




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
            # If an async background commit is still writing and memory is healthy, defer to allow async overlap
            if getattr(G.TE, "async_commits", False) and getattr(G.TE, "pending_commit_rows", 0) > 0 and not G.LOW_MEMORY_MODE:
                if self.executed_count % (2 * self.chunk_commit_interval) != 0:
                    return
            _commit_all_with_retry("Chunk")
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
        # The caches stay: Step 6.1 (symlink resolution) still stages m_file_name / m_file_blob rows and a
        # cleared cache mints new ids for paths and blobs already in the database. They are cleared after
        # git commit processing, where nothing stages into those parent tables any more.
        G.TE.commit_all()
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
