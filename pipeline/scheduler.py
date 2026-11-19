"""Dependency-driven ChangeSet resolution and the parallel wave executor."""
from core.globalstuff import (
    G,
    COLOR,
    REF_FILE,
    OP_REF,
    reclaim_system_memory,
)

import contextlib
import time
from collections import defaultdict, deque
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from core import ingest_state
from core.DBLayout import TABLES
from core.GreatProcessor import GreatProcessor
from core.TableHandling import ChangeSet, is_data_unsafe
from db_engine.spec import AnyOf, Col, Exists, Join, Pred, Value, ge, gt, le, lt, ne, sub
from pipeline.changesets import execute_and_purge, extract_tags_and_evacuate_cs
from pipeline.state import logger
from pipeline.worker_stage import _commit_all_with_retry




_CHUNK_COMMIT_ROW_CAP = 500_000


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
        raw_staged = getattr(G.TE, "staged_rows", 0)
        staged_rows = raw_staged if isinstance(raw_staged, int) else 0
        row_cap_hit = staged_rows >= _CHUNK_COMMIT_ROW_CAP
        count_hit = self.executed_count % self.chunk_commit_interval == 0
        if not row_cap_hit and not count_hit:
            return
        # If an async background commit is still writing and memory is healthy, defer to allow async overlap
        # (unless the staged row cap itself has been reached, or we are at 2x the count interval).
        if getattr(G.TE, "async_commits", False) and getattr(G.TE, "pending_commit_rows", 0) > 0 and not G.LOW_MEMORY_MODE:
            if not row_cap_hit and self.executed_count % (2 * self.chunk_commit_interval) != 0:
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
        # Keep async_commits enabled so the final Stage 1 wave commit overlaps _publish_file_symbols()
        if hasattr(G.TE, "wait_for_commit"):
            G.TE.async_commits = True
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
        if hasattr(G.TE, "wait_for_commit"):
            G.TE.async_commits = False
            G.TE.wait_for_commit()
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


def _source_scc_candidates(remaining: set[str], deps: dict[str, set[str]]) -> list[str]:
    """Return nodes in SCCs that have no unresolved dependencies outside their own SCC (iterative Tarjan)."""
    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    source_nodes: list[str] = []

    for start in sorted(remaining):
        if start in indices:
            continue
        call_stack: list[tuple[str, Any]] = [(start, iter(deps.get(start, ())))]
        indices[start] = lowlink[start] = index_counter
        index_counter += 1
        stack.append(start)
        on_stack.add(start)

        while call_stack:
            v, it = call_stack[-1]
            try:
                w = next(it)
                if w not in remaining:
                    continue
                if w not in indices:
                    indices[w] = lowlink[w] = index_counter
                    index_counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    call_stack.append((w, iter(deps.get(w, ()))))
                elif w in on_stack and indices[w] < lowlink[v]:
                    lowlink[v] = indices[w]
            except StopIteration:
                call_stack.pop()
                if call_stack:
                    parent = call_stack[-1][0]
                    if lowlink[v] < lowlink[parent]:
                        lowlink[parent] = lowlink[v]
                if lowlink[v] == indices[v]:
                    scc: list[str] = []
                    while True:
                        w = stack.pop()
                        on_stack.remove(w)
                        scc.append(w)
                        if w == v:
                            break
                    scc_set = set(scc)
                    if all(dep in scc_set for node in scc for dep in deps.get(node, ()) if dep in remaining):
                        source_nodes.extend(scc)
    return source_nodes


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
    cycle_breaks = 0
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
            t_wave_start = time.perf_counter()
            # Wave selection: files with no unexecuted dependencies in remaining (sorted for reproducible ids)
            wave = sorted(ready)
            ready.clear()

            if not wave:
                # Deadlock: pick the victim inside a source SCC (no unresolved dependencies outside itself),
                # preferring the file most others wait on (ties: fewest operations to stub, then path).
                candidates = _source_scc_candidates(remaining, deps) or list(remaining)
                cand_path = max(candidates, key=lambda k: (len(dependents.get(k, ())), -op_counts.get(k, 0), k))
                cycle_breaks += 1
                logger.warning(COLOR.yellow(
                    f"[Phase 2] Breaking circular dependency deadlock #{cycle_breaks}: precision force-resolving 1 stub "
                    f"(candidate '{cand_path}' has {len(dependents.get(cand_path, ()))} dependents, {len(remaining)} files remaining)"
                ))

                cand_cs = gp.ChangeSet_Dict[cand_path]
                cand_cs.gp = gp
                if not cand_cs.execute(force_stubs=True):
                    logger.error(COLOR.red(
                        f"[Phase 2] '{cand_path}': {len(cand_cs.unresolved_indices or ())} operations remain unresolved "
                        f"after stub resolution (blocked on '{cand_cs.blocked_on}'); they are dropped."
                    ))
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

            wave_num += 1

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
    breaks_suffix = f" ({cycle_breaks} cycle breaks)" if cycle_breaks else ""
    logger.info(COLOR.green(
        f"[Phase 2] Completed resolution of {executed_count} ChangeSets across {wave_num} waves{breaks_suffix} "
        f"in {resolution_time:.1f}s ({avg_velocity:.1f} files/s); final commit {commit_time:.1f}s."
    ))


class DistributedTaskScheduler:
    """Pure-DB distributed task queue scheduler backed by `distributed_tasks` and `distributed_worker_capabilities`."""

    def __init__(
        self,
        db_factory: Any,
        network_id: int,
        node_id: str,
        role: str,
        *,
        heartbeat_timeout: int = 180,
        repo_name: str | None = None,
        min_priority: int | None = None,
        max_priority: int | None = None,
        task_types: Sequence[str] | None = None,
    ) -> None:
        self.db_factory = db_factory
        self.network_id = int(network_id)
        self.node_id = str(node_id)
        self.role = str(role)
        self.heartbeat_timeout = int(heartbeat_timeout)
        self.repo_name = repo_name
        self.min_priority = min_priority
        self.max_priority = max_priority
        self.task_types = tuple(task_types) if task_types is not None else None

    @contextlib.contextmanager
    def _with_db(self, db: Any) -> Iterator[Any]:
        if db is not None:
            yield db
            return
        with self.db_factory() as conn:
            yield conn

    def register_capabilities(self, capabilities: Sequence[Mapping[str, Any]], db: Any = None) -> int:
        """Register or replace this worker's capabilities in `distributed_worker_capabilities`."""
        with self._with_db(db) as conn:
            if conn is None:
                return 0
            return int(conn.register_worker_capabilities(self.node_id, self.network_id, capabilities, role=self.role) or 0)

    def _resolve_task_commit_id(self, conn: Any, row: Mapping[str, Any]) -> int:
        cid = int(row.get("commit_id") or 0)
        ref_name = str(row.get("ref_name") or "")
        if cid == 0 and ref_name:
            found = conn.select_one(
                ("m_git_ref", "r"),
                ["r.commit_id"],
                {"repo.network_id": self.network_id, "r.ref_name": ref_name, "r.commit_id": gt(0)},
                joins=[Join(("m_repository", "repo"), {"repo.repo_id": "r.repo_id"})],
                as_dict=False,
            )
            if found and found[0]:
                cid = int(found[0])
        return cid

    def _sweep_commit_zombie(
        self,
        conn: Any,
        row: Mapping[str, Any],
        cid: int,
        ts: int,
        expired_filter: Any,
    ) -> int:
        tid = int(row["task_id"])
        ref_name = str(row.get("ref_name") or "")
        orig_worker = str(row.get("worker_id") or "")

        if conn.row_exists(
            "m_ingest_state",
            {"network_id": self.network_id, "commit_id": cid, "status": ingest_state.INGEST_DONE},
        ):
            promoted = conn.complete_distributed_task(
                tid,
                cid,
                worker_id=orig_worker,
                ref_name=ref_name or None,
                network_id=self.network_id,
                now=ts,
            )
            return 1 if promoted else 0

        rc = conn.update_rows(
            "distributed_tasks",
            {"worker_id": "recovering", "heartbeat_ts": ts, "updated_ts": ts},
            [{"task_id": tid, "status": "claimed", "worker_id": orig_worker}, expired_filter],
        )
        if not (rc and rc > 0):
            return 0

        logger.warning(
            f"[Distributed] Sweeping zombie task {tid} ('{ref_name}', worker '{orig_worker}', commit_id {cid})."
        )
        try:
            if not ingest_state.rollback_version(conn, self.network_id, cid, TABLES):
                ingest_state.purge_partial_version(conn, cid)
            conn.delete_rows(
                "m_ingest_state",
                {"network_id": self.network_id, "commit_id": cid, "status": ingest_state.INGEST_PARSING},
            )
        except ingest_state.RecoveryError as exc:
            logger.error(f"[Distributed] Zombie rollback refused for task {tid} (commit_id {cid}): {exc}")
            fail_rc = conn.update_rows(
                "distributed_tasks",
                {
                    "status": "failed",
                    "worker_id": "",
                    "heartbeat_ts": 0,
                    "updated_ts": ts,
                    "error_msg": f"Rollback refused during zombie recovery: {exc}"[:1000],
                },
                {"task_id": tid, "status": "claimed", "worker_id": "recovering"},
            )
            if fail_rc and fail_rc > 0:
                self._cascade_failed_dependents(conn, tid, ts)
                return 1
            return 0

        reset_rc = conn.update_rows(
            "distributed_tasks",
            {
                "status": "pending",
                "worker_id": "",
                "heartbeat_ts": 0,
                "updated_ts": ts,
                "error_msg": "Zombie task recovered after heartbeat expiration",
            },
            {"task_id": tid, "status": "claimed", "worker_id": "recovering"},
        )
        return 1 if (reset_rc and reset_rc > 0) else 0

    def sweep_zombies(self, db: Any = None, now: int | None = None) -> int:
        """Recover expired claimed tasks, rolling back partial version writes before resetting to pending."""
        with self._with_db(db) as conn:
            if conn is None:
                return 0
            ts = int(time.time()) if now is None else int(now)
            cutoff = ts - max(1, int(self.heartbeat_timeout))
            expired_filter = AnyOf({"heartbeat_ts": lt(cutoff)}, {"heartbeat_ts": 0, "updated_ts": lt(cutoff)})
            expired_rows = conn.select_rows(
                "distributed_tasks",
                ["task_id", "task_type", "ref_name", "commit_id", "worker_id"],
                [{"network_id": self.network_id, "status": "claimed"}, expired_filter],
                as_dict=True,
            )
            if not expired_rows:
                return 0

            recovered = 0
            plain_ids: list[int] = []
            for row in expired_rows:
                tid = int(row["task_id"])
                if str(row.get("task_type") or "") == "preparse":
                    plain_ids.append(tid)
                    continue
                cid = self._resolve_task_commit_id(conn, row)
                if cid == 0:
                    plain_ids.append(tid)
                    continue
                recovered += self._sweep_commit_zombie(conn, row, cid, ts, expired_filter)

            if plain_ids:
                recovered += int(
                    conn.reset_zombie_distributed_tasks(
                        self.network_id,
                        self.heartbeat_timeout,
                        now=ts,
                        task_ids=plain_ids,
                    )
                    or 0
                )
            return recovered

    def claim_next(
        self,
        db: Any = None,
        *,
        preferred_track: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        """Sweep zombie tasks and atomically claim the highest-priority task matching worker capabilities."""
        with self._with_db(db) as conn:
            if conn is None:
                return None
            self.sweep_zombies(conn, now=now)
            task = conn.claim_distributed_task(
                self.network_id,
                self.node_id,
                min_priority=self.min_priority,
                max_priority=self.max_priority,
                task_types=self.task_types,
                repo_name=self.repo_name,
                preferred_track=preferred_track,
                now=now,
            )
            if task is not None:
                task["_queue"] = "distributed_tasks"
            return task

    def heartbeat(self, task_id: int, db: Any = None, now: int | None = None) -> bool:
        """Renew `heartbeat_ts` on an actively claimed task owned by this node."""
        with self._with_db(db) as conn:
            if conn is None:
                return False
            return bool(conn.heartbeat_distributed_task(task_id, self.node_id, now=now))

    def complete(
        self,
        task_id: int,
        commit_id: int,
        *,
        ref_name: str | None = None,
        db: Any = None,
        now: int | None = None,
    ) -> bool:
        """Mark a claimed task completed and backfill dependent tasks' `base_commit_id`."""
        with self._with_db(db) as conn:
            if conn is None:
                return False
            return bool(
                conn.complete_distributed_task(
                    task_id,
                    commit_id,
                    worker_id=self.node_id,
                    ref_name=ref_name,
                    network_id=self.network_id,
                    now=now,
                )
            )

    def _cascade_failed_dependents(self, conn: Any, initial_task_id: int, ts: int) -> int:
        """Iteratively fail downstream pending tasks whose base matches a failed task in the same repo."""
        row = conn.select_one(
            "distributed_tasks",
            ["network_id", "repo_name", "task_type", "commit_id", "ref_name"],
            {"task_id": int(initial_task_id)},
            as_dict=False,
        )
        if not row or str(row[2] or "") == "preparse":
            return 0
        net_id = int(row[0])
        repo_name = str(row[1] or "")
        init_cid = int(row[3]) if row[3] and int(row[3]) > 0 else 0
        init_ref = str(row[4] or "")

        pending_cids = {init_cid} if init_cid > 0 else set()
        pending_refs = {init_ref} if init_ref else set()
        seen_cids: set[int] = set()
        seen_refs: set[str] = set()
        total_cascaded = 0

        with conn.transaction():
            while pending_cids or pending_refs:
                step_cids = pending_cids - seen_cids
                step_refs = pending_refs - seen_refs
                seen_cids.update(step_cids)
                seen_refs.update(step_refs)
                if not step_cids and not step_refs:
                    break
                branches: list[Any] = []
                if step_cids:
                    branches.append({"base_commit_id": sorted(step_cids)})
                if step_refs:
                    branches.append({"base_ref_name": sorted(step_refs)})
                down_rows = conn.select_rows(
                    "distributed_tasks",
                    ["task_id", "task_type", "commit_id", "ref_name"],
                    [{"network_id": net_id, "repo_name": repo_name, "status": "pending"}, AnyOf(*branches)],
                    as_dict=False,
                )
                if not down_rows:
                    break
                target_ids = [int(r[0]) for r in down_rows]
                affected = int(
                    conn.update_rows(
                        "distributed_tasks",
                        {"status": "failed", "error_msg": "Prerequisite base task failed", "updated_ts": ts},
                        {"task_id": target_ids, "status": "pending"},
                    )
                    or 0
                )
                total_cascaded += affected
                if affected == 0:
                    break
                next_cids: set[int] = set()
                next_refs: set[str] = set()
                for _tid, d_type, d_cid, d_ref in down_rows:
                    if str(d_type or "") == "preparse":
                        continue
                    dc = int(d_cid) if d_cid and int(d_cid) > 0 else 0
                    dr = str(d_ref or "")
                    if dc > 0 and dc not in seen_cids:
                        next_cids.add(dc)
                    if dr and dr not in seen_refs:
                        next_refs.add(dr)
                pending_cids = next_cids
                pending_refs = next_refs
        return total_cascaded

    def fail(
        self,
        task_id: int,
        error_msg: str,
        *,
        max_attempts: int = 3,
        db: Any = None,
        now: int | None = None,
    ) -> bool:
        """Record a task failure; on terminal `"failed"` status, cascade failure to downstream dependents."""
        with self._with_db(db) as conn:
            if conn is None:
                return False
            ts = int(time.time()) if now is None else int(now)
            rc = conn.fail_distributed_task(
                task_id,
                error_msg,
                worker_id=self.node_id,
                max_attempts=max_attempts,
                now=ts,
            )
            if not (rc and rc > 0):
                return False
            row = conn.select_one("distributed_tasks", ["status"], {"task_id": int(task_id)}, as_dict=False)
            if row and row[0] == "failed":
                self._cascade_failed_dependents(conn, int(task_id), ts)
            return True

    def release(self, task_id: int, reason: str, *, db: Any = None, now: int | None = None) -> bool:
        """Return a claimed task to `pending` without incrementing `retry_count`."""
        with self._with_db(db) as conn:
            if conn is None:
                return False
            return bool(conn.release_distributed_task(task_id, reason, self.node_id, now=now))

    def is_queue_drained(self, db: Any = None, *, coordinator_timeout: int = 120) -> bool:
        """True only when tasks exist, none are claimed, no master/daemon is active, and no matching task is claimable."""
        with self._with_db(db) as conn:
            if conn is None:
                return False
            if conn.count_rows("distributed_tasks", {"network_id": self.network_id}) == 0:
                return False
            if conn.row_exists("distributed_tasks", {"network_id": self.network_id, "status": "claimed"}):
                return False
            now = int(time.time())
            cutoff = now - max(1, int(coordinator_timeout))
            if conn.row_exists(
                "m_parse_node",
                {"network_id": self.network_id, "role": ["master", "daemon"], "heartbeat_at": ge(cutoff)},
            ):
                return False

            base_filter: dict[str, Any] = {"t.network_id": self.network_id, "t.status": "pending"}
            pri_conds = []
            if self.min_priority is not None:
                pri_conds.append(ge(int(self.min_priority)))
            if self.max_priority is not None:
                pri_conds.append(le(int(self.max_priority)))
            if len(pri_conds) == 1:
                base_filter["t.priority"] = pri_conds[0]
            elif len(pri_conds) > 1:
                base_filter["t.priority"] = pri_conds
            if self.task_types:
                base_filter["t.task_type"] = list(self.task_types)
            if self.repo_name:
                base_filter["t.repo_name"] = self.repo_name

            base_ready = AnyOf(
                {"t.base_ref_name": ""},
                [
                    {"t.base_commit_id": gt(0)},
                    Exists(
                        ("m_ingest_state", "bs"),
                        {"bs.network_id": "t.network_id", "bs.commit_id": "t.base_commit_id"},
                        {"bs.status": ingest_state.INGEST_DONE},
                    ),
                ],
                Exists(
                    ("distributed_tasks", "bt"),
                    {"bt.network_id": "t.network_id", "bt.repo_name": "t.repo_name", "bt.ref_name": "t.base_ref_name"},
                    {"bt.status": "completed", "bt.task_type": ne("preparse")},
                ),
            )
            where_terms: list[Any] = [
                base_filter,
                AnyOf(
                    {"c.track_name": Col("t.track_name")},
                    {"c.track_name": "*"},
                    [{"t.task_type": "preparse"}, {"c.track_name": "preparse"}],
                ),
                AnyOf({"t.task_type": ne("preparse")}, {"c.supports_preparse": 1}),
                AnyOf({"c.min_seq_order": 0}, {"t.seq_order": 0}, Pred(sub("t.seq_order", "c.min_seq_order"), ge(0))),
                AnyOf({"c.max_seq_order": 0}, {"t.seq_order": 0}, Pred(sub("c.max_seq_order", "t.seq_order"), ge(0))),
                base_ready,
            ]
            cap_join = [
                Join(
                    ("distributed_worker_capabilities", "c"),
                    {"c.worker_id": Value(self.node_id), "c.network_id": "t.network_id", "c.repo_name": "t.repo_name"},
                    "inner",
                )
            ]
            return not conn.row_exists(("distributed_tasks", "t"), where_terms, joins=cap_join)
