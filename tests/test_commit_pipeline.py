"""tests/test_commit_pipeline.py - Regression tests for the commit path and stage instrumentation.

Covers TableEngine commits written by a background thread (read-your-writes barrier, failure restore), retried
commits after a failed write (rows were dropped before), cache misses inside the preloaded version window (no
database read), background commits started while the parent waits for workers, MariaDB transactions that fail
instead of reconnecting, server variable tuning, per-version stage counts, worker profiles of ChangeSets nothing
was parsed for, the per-stage time split and symbol counters reported by the parsing workers, and batches of equal
size in bytes.
"""
import copy
import gc
from collections import Counter
import os
import pickle
import re
import signal
import tempfile
import threading
import unittest

import main
import pipeline.source_order
import pipeline.worker_protocol
import pipeline.worker_stage
from core.DBLayout import m_ast, m_ast_hash, m_blob_symbol_def, m_bridge_checkpoint, m_bridge_tag, m_file_name, m_tag
from core.GreatProcessor import GreatProcessor
from core.SymbolRows import STATS as SYMBOL_STATS
from core.globalstuff import G
from core.Profiler import PipelineProfiler, PipelineStageProfiler, _format_pct
from db_engine import MockDB
from table_engine import TECachedDB, TEDirectDB


class _GatedDB(MockDB):
    """MockDB whose parallel commits wait for `gate` and fail for `fail_tables`, to observe background writes."""

    supports_background_commit = True
    gate = threading.Event()
    fail_tables: set[str] = set()
    dedicated_calls = 0

    def commit_tables_parallel(self, tables_data, max_workers=None, dedicated=False):
        if dedicated:
            type(self).dedicated_calls += 1
        type(self).gate.wait(5)
        failing = [t for t, _ins, _upd in tables_data if t.table_name in type(self).fail_tables]
        stats = super().commit_tables_parallel(
            [item for item in tables_data if item[0].table_name not in type(self).fail_tables], max_workers
        )
        if failing:
            err = RuntimeError("write failed")
            err.failed_table_ids = {t.table_id for t in failing}
            raise err
        return stats


def _stored(table) -> dict:
    return MockDB.store.get(table.table_name, {})


class _EngineCase(unittest.TestCase):
    engine = TECachedDB

    def setUp(self) -> None:
        self.saved = (G.DB, G.TE)
        MockDB.reset()
        _GatedDB.gate.set()
        _GatedDB.fail_tables = set()
        _GatedDB.dedicated_calls = 0
        G.DB = _GatedDB
        self.te = self.engine() if self.engine is TEDirectDB else self.engine(hugepages="off")
        G.TE = self.te
        self.te.start(main.gp.Table_Array, _GatedDB)

    def tearDown(self) -> None:
        _GatedDB.gate.set()
        _GatedDB.fail_tables = set()
        try:
            self.te.close()
        except Exception:
            pass
        G.DB, G.TE = self.saved
        MockDB.reset()

    def _stage_symbol(self, name: str) -> tuple:
        return self.te.set(m_blob_symbol_def.table_id, (None, 7, 8, 9, name, 1, 0, 1, 2))


class TestBackgroundCommits(_EngineCase):
    def test_commit_returns_before_the_write_and_reads_wait_for_it(self) -> None:
        self.te.async_commits = True
        row = self._stage_symbol("probe")
        _GatedDB.gate.clear()
        self.te.commit_all()
        self.assertEqual(self.te.pending_commit_rows, 1, "commit_all() must return while the write is pending")
        self.assertEqual(_stored(m_blob_symbol_def), {})
        self.assertEqual(self.te.queued_set[m_blob_symbol_def.table_id], {}, "staging starts a new generation")
        # O-01: get() finds the row in the in-flight batch without joining the writer thread
        found = self.te.get(m_blob_symbol_def.table_id, (row[0],) + (None,) * (m_blob_symbol_def.length - 1))
        self.assertEqual(found[0], row[0])
        self.assertEqual(self.te.pending_commit_rows, 1)
        threading.Timer(0.15, _GatedDB.gate.set).start()
        # A join-graph read still waits for the background write
        self.te._await_writes((m_blob_symbol_def.table_id,))
        self.assertEqual(self.te.pending_commit_rows, 0)
        self.assertGreaterEqual(self.te.commit_stats["blocked_s"], 0.08)
        self.assertEqual(self.te.commit_stats["background"], 1)
        self.assertEqual(_GatedDB.dedicated_calls, 1, "a background write must not use the engine's connection")

    def test_reads_of_other_tables_do_not_wait(self) -> None:
        self.te.async_commits = True
        self._stage_symbol("probe")
        _GatedDB.gate.clear()
        self.te.commit_all()
        try:
            self.te._await_writes((m_file_name.table_id,))
            self.assertEqual(self.te.pending_commit_rows, 1)
        finally:
            _GatedDB.gate.set()
            self.te.wait_for_commit()

    def test_interrupted_join_keeps_the_batch_pending(self) -> None:
        self.te.async_commits = True
        row = self._stage_symbol("interrupted")
        _GatedDB.gate.clear()
        self.te.commit_all()
        batch = self.te._pending
        real_join = batch.thread.join
        calls = []

        def _join_once_interrupted(*args):
            calls.append(args)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return real_join(*args)

        batch.thread.join = _join_once_interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.te.wait_for_commit()
        self.assertIs(self.te._pending, batch, "an interrupted join must not drop the batch")
        self.assertEqual(self.te.commit_stats["commits"], 0)
        _GatedDB.gate.set()
        self.te.wait_for_commit()
        self.assertIsNone(self.te._pending)
        self.assertEqual((self.te.commit_stats["commits"], self.te.commit_stats["rows"]), (1, 1))
        self.assertIn(row[0], _stored(m_blob_symbol_def))

    def test_failed_background_commit_puts_its_rows_back(self) -> None:
        self.te.async_commits = True
        name_row = self.te.set(m_file_name.table_id, (None, "drivers/net/foo.c"))
        sym_row = self._stage_symbol("kept")
        _GatedDB.fail_tables = {"m_file_name"}
        self.te.commit_all()
        with self.assertRaises(RuntimeError):
            self.te.wait_for_commit()
        # The table that failed is staged again, the one that was written is not
        self.assertEqual(self.te.set(m_file_name.table_id, (None, "drivers/net/foo.c"))[0], name_row[0])
        self.assertIn(sym_row[0], _stored(m_blob_symbol_def))
        self.assertEqual(self.te.queued_set[m_blob_symbol_def.table_id], {})
        _GatedDB.fail_tables = set()
        self.te.commit_all()
        self.te.wait_for_commit()
        self.assertEqual(
            [r for r in _stored(m_file_name).values() if r[1] == "drivers/net/foo.c"], [(name_row[0], "drivers/net/foo.c")]
        )

    def test_end_of_stage_commits_a_failed_background_batch_again(self) -> None:
        self.te.async_commits = True
        self.te.set(m_file_name.table_id, (None, "fs/ext2/inode.c"))
        _GatedDB.fail_tables = {"m_file_name"}
        self.te.commit_all()
        _GatedDB.fail_tables = set()
        pipeline.worker_stage._end_background_commits("[test]")
        self.assertFalse(self.te.async_commits)
        self.assertEqual([r[1] for r in _stored(m_file_name).values() if r[1] == "fs/ext2/inode.c"], ["fs/ext2/inode.c"])


class TestPreloadWindow(_EngineCase):
    """Checkpoint lookups of a version not written yet used to cost one database read each."""

    def _get(self, commit_id: int, fnid: int):
        before = self.te.db_reads
        row = self.te.get(m_bridge_checkpoint.table_id, (commit_id, fnid, None))
        return row, self.te.db_reads - before

    def test_miss_inside_the_window_is_final(self) -> None:
        self.assertEqual(self._get(5, 77), (None, 0))

    def test_committed_and_evicted_row_is_read_back(self) -> None:
        self.te.set(m_bridge_checkpoint.table_id, (5, 77, 9))
        self.te.commit_all()
        row, reads = self._get(5, 77)
        self.assertEqual((row[:3], reads), ((5, 77, 9), 1))

    def test_rows_staged_without_indexes_drop_the_window(self) -> None:
        self.te.update_in_mem_indexes = False
        self.te.set(m_bridge_checkpoint.table_id, (5, 78, 9))
        self.te.update_in_mem_indexes = True
        self.assertEqual(self._get(5, 79)[1], 1)


class TestDeferredHashEviction(_EngineCase):
    """Deferred m_ast_hash rows of a heap-cached (not carried) table were evicted into _committed_pks by the
    intermediate commits, so the final commit skipped them and they were never written."""

    def test_deferred_rows_survive_intermediate_commits(self) -> None:
        self.assertNotIn(m_ast_hash.table_id, self.te._carried_overlays)
        self.te.defer_ast_hash = True
        h = b"\x05" * 16
        self.te.set(m_ast_hash.table_id, (h, 42))
        self.te.commit_all()
        self.te.commit(m_ast_hash.table_id)
        self.assertNotIn(h, self.te._committed_pks[m_ast_hash.table_id])
        self.assertEqual(self.te.get(m_ast_hash.table_id, (h, None)), (h, 42))
        self.te.defer_ast_hash = False
        self.te.commit_all()
        self.assertIn(h, _stored(m_ast_hash))

    def test_rows_staged_but_not_taken_stay_cached(self) -> None:
        cp = m_bridge_checkpoint.table_id
        self.te.set(cp, (5, 1, 9))
        self.te.commit_all()
        self.te.set(cp, (5, 2, 9))
        self.te._evict_committed_rows(cp)
        self.assertIn((5, 1), self.te._committed_pks[cp])
        self.assertNotIn((5, 2), self.te._committed_pks[cp])
        self.te.commit_all()
        self.assertIn((5, 2), _stored(m_bridge_checkpoint))
        # The column index never references evicted rows
        self.assertFalse(any(self.te._col_indices[cp].values()))


class TestCarriedUpkeep(unittest.TestCase):
    """Every commit_all copied and re-projected the whole deferred m_ast_hash set into the carried overlay."""

    def setUp(self) -> None:
        self.saved = (G.DB, G.TE, main.gp.Old_VID)
        MockDB.reset()
        G.DB = MockDB
        main.gp.Old_VID = 0
        self.te = TECachedDB(hugepages="auto")
        G.TE = self.te
        self.te.start(main.gp.Table_Array, MockDB)
        self.te.set(m_ast_hash.table_id, (b"\x01" * 16, 1))
        self.te.commit_all()
        self.te.close()
        self.te.start(main.gp.Table_Array, MockDB)  # loads the carried snapshot

    def tearDown(self) -> None:
        try:
            self.te.close()
        except Exception:
            pass
        G.DB, G.TE, main.gp.Old_VID = self.saved
        MockDB.reset()

    def test_deferred_set_costs_nothing_per_commit_and_overlay_matches_database(self) -> None:
        t_id = m_ast_hash.table_id
        if t_id not in self.te._carried_overlays:
            self.skipTest("shared buffer unavailable")
        self.te.defer_ast_hash = True
        hashes = [i.to_bytes(16, "big") for i in range(2, 3002)]
        for n, h in enumerate(hashes):
            self.te.set(t_id, (h, 10 + n))
        self.te.update_in_mem_indexes = False
        unindexed = b"\xee" * 16
        self.te.set(t_id, (unindexed, 9))
        self.te.update_in_mem_indexes = True
        self.te.set(m_file_name.table_id, (None, "kernel/fork.c"))
        projected = []
        real_project = self.te._project_row
        self.te._project_row = lambda table, row: projected.append(row) or real_project(table, row)
        self.te.commit_all()
        self.te.commit_all()
        self.assertEqual(projected, [], "a commit that does not take the deferred rows must not touch them")
        self.te.defer_ast_hash = False
        self.te.commit_all()
        self.assertEqual(len(projected), 3001, "the commit taking a row staged without indexes records its table once")
        del self.te._project_row
        in_memory = {row for row in self.te._carried_rows_in_memory()[t_id]}
        stored = set(_stored(m_ast_hash).values())
        self.assertEqual(in_memory - {(b"\x00" * 16, 0)}, stored - {(b"\x00" * 16, 0)})
        self.assertIn((unindexed, 9), in_memory)
        self.te.close()
        self.te.start(main.gp.Table_Array, MockDB)
        self.assertEqual(self.te.get(t_id, (unindexed, None)), (unindexed, 9))
        # A carried table stages into its PK overlay only (no row list, positions or column index)
        staged = self.te.set(t_id, (b"\xdd" * 16, 5))
        self.assertEqual(self.te._cached_rows[t_id]._appended, [])
        self.assertEqual((self.te._cached_rows_pos[t_id], self.te._col_indices[t_id]), ({}, {}))
        self.assertEqual(self.te.get(t_id, (b"\xdd" * 16, None)), staged)
        self.assertEqual(self.te.get(t_id, (hashes[-1], None)), (hashes[-1], 3009))


class TestEvictionAndRestartLeaks(_EngineCase):
    def test_evicted_row_in_flight_is_read_without_joining_the_write(self) -> None:
        cp = m_bridge_checkpoint.table_id
        self.te.async_commits = True
        self.te.set(cp, (5, 77, 9))
        _GatedDB.gate.clear()
        self.te.commit_all()
        self.assertIsNotNone(self.te._pending)
        self.assertIn((5, 77), self.te._committed_pks[cp], "evicted from the heap while being written")
        try:
            before = self.te.db_reads
            self.assertEqual(self.te.get(cp, (5, 77, None))[:3], (5, 77, 9))
            self.assertEqual(self.te.db_reads - before, 0)
            self.assertIsNotNone(self.te._pending, "in-flight hit does not join the write")
            # A lookup by the non-key column also hits the in-flight batch via its preserved staged index
            self.assertEqual(self.te.get(cp, (None, None, 9))[:3], (5, 77, 9))
            self.assertEqual(self.te.db_reads - before, 0)
            self.assertIsNotNone(self.te._pending)
            # An in-flight miss on a non-PK column falls back to the database without joining the write
            self.assertIsNone(self.te.get(cp, (None, 999, None)))
            self.assertEqual(self.te.db_reads - before, 1)
            self.assertIsNotNone(self.te._pending)
        finally:
            _GatedDB.gate.set()
            self.te.wait_for_commit()

    def test_restart_resets_committed_unique_keys_and_join_cache(self) -> None:
        self.te._committed_unique_keys[m_file_name.table_id] = {(1,): {("x",)}}
        self.te._join_table_ids((((m_file_name.table_id, 0),),))
        self.te.start_new_db(_GatedDB)
        self.assertEqual((self.te._committed_unique_keys, self.te._join_tables_cache), ({}, {}))

    def test_failing_next_id_is_logged(self) -> None:
        from unittest import mock
        with mock.patch.object(_GatedDB, "get_next_id", side_effect=RuntimeError("gone")):
            with self.assertLogs("table_engine.te_direct_db", "WARNING"):
                self.te.start_new_db(_GatedDB)


class TestEmptyCommitAll(_EngineCase):
    """commit_all() with nothing staged evicted the write-heavy tables and trimmed the heap every time."""

    def test_empty_commit_skips_the_upkeep_once_it_ran(self) -> None:
        from unittest import mock
        self._stage_symbol("probe")
        with mock.patch("core.globalstuff.trim_heap") as trim:
            self.te.commit_all()
            self.assertEqual(trim.call_count, 1)
            self.te.commit_all()
            self.te.commit_all()
            self.assertEqual(trim.call_count, 1, "an empty commit_all() does no upkeep")
            self.te.set(m_bridge_checkpoint.table_id, (5, 1, 2))
            self.te.commit(m_bridge_checkpoint.table_id)
            self.te.commit_all()
            self.assertEqual(trim.call_count, 2, "rows committed since the last upkeep make it due again")
            self.te.commit_all(update_in_mem_indexes=False)
            self.assertEqual(trim.call_count, 3, "a teardown commit always purges the caches")

    def test_empty_commit_still_raises_a_failed_background_commit(self) -> None:
        self.te.async_commits = True
        self.te.set(m_file_name.table_id, (None, "lib/sort.c"))
        _GatedDB.fail_tables = {"m_file_name"}
        self.te.commit_all()
        self.te._pending.thread.join()
        with self.assertRaises(RuntimeError):
            self.te.commit_all()


class TestIdleCommit(_EngineCase):
    def test_waiting_parent_commits_in_the_background(self) -> None:
        self.te.async_commits = True
        self._stage_symbol("idle")
        saved = pipeline.worker_stage._IDLE_COMMIT_MIN_ROWS
        try:
            pipeline.worker_stage._IDLE_COMMIT_MIN_ROWS = 2
            pipeline.worker_stage._maybe_idle_commit()
            self.assertEqual(self.te.staged_rows, 1, "below the threshold nothing is committed")
            pipeline.worker_stage._IDLE_COMMIT_MIN_ROWS = 1
            pipeline.worker_stage._maybe_idle_commit()
            self.assertEqual(self.te.staged_rows, 0)
            self.te.wait_for_commit()
            self.assertEqual(self.te.commit_stats["background"], 1)
        finally:
            pipeline.worker_stage._IDLE_COMMIT_MIN_ROWS = saved

    def test_no_commit_outside_a_stage(self) -> None:
        self._stage_symbol("idle")
        saved = pipeline.worker_stage._IDLE_COMMIT_MIN_ROWS
        try:
            pipeline.worker_stage._IDLE_COMMIT_MIN_ROWS = 1
            pipeline.worker_stage._maybe_idle_commit()
            self.assertEqual(self.te.staged_rows, 1)
        finally:
            pipeline.worker_stage._IDLE_COMMIT_MIN_ROWS = saved


class TestBlobSymbolCache(_EngineCase):
    """Symbol maps of unchanged headers are loaded once per blob, not once per worker and version."""

    def setUp(self) -> None:
        super().setUp()
        self.gp = GreatProcessor()
        self.gp.VID, self.gp.Old_VID = 2, 1
        fnid = self.te.set(m_file_name.table_id, (None, "include/linux/list.h"))[0]
        ast_id = self.te.set(m_ast.table_id, (None, "list_add", 1))[0]
        tag_id = self.te.set(m_tag.table_id, (None, b"h" * 32, ast_id, 0, 0))[0]
        self.te.set(m_bridge_tag.table_id, (9, tag_id, 1, 2, 1, 1))
        self.te.set(m_bridge_checkpoint.table_id, (1, fnid, 9))
        self.te.commit_all()
        # A version starts with a preload: the previous checkpoint is cached, the symbol rows are not
        self.te.start(main.gp.Table_Array, _GatedDB)
        self.expected = {("list_add", 1): ast_id}

    def _lookup(self) -> tuple[dict, int]:
        before = self.te.db_reads
        self.gp.unchanged_symbol_cache.clear()
        return self.gp.get_file_symbols_from_db("include/linux/list.h"), self.te.db_reads - before

    def test_next_version_reuses_the_blob_map(self) -> None:
        self.assertEqual(self._lookup()[0], self.expected)
        self.gp.prune_blob_symbol_cache()
        self.assertEqual(self._lookup(), (self.expected, 0))

    def test_worker_maps_reach_the_parent_and_unused_maps_expire(self) -> None:
        worker = copy.copy(self.gp)
        worker._is_worker = True
        worker._blob_symbols_fetched, worker._blob_symbols_used = {}, set()
        worker.unchanged_symbol_cache = {}
        self.assertEqual(worker.get_file_symbols_from_db("include/linux/list.h"), self.expected)
        self.assertEqual(self.gp.blob_symbol_cache, {}, "a worker never writes the parent's cache")
        self.gp.merge_blob_symbols(worker._blob_symbols_fetched, worker._blob_symbols_used)
        self.assertEqual(self._lookup(), (self.expected, 0))
        for _ in range(9):
            self.gp.prune_blob_symbol_cache()
        self.assertEqual(self.gp.blob_symbol_cache, {})

    def test_changed_files_are_not_cached(self) -> None:
        self.gp._changed_paths_set = {"include/linux/list.h"}
        self.gp.get_file_symbols_from_db("include/linux/list.h")
        self.assertEqual(self.gp.blob_symbol_cache, {})


class TestRetriedCommit(_EngineCase):
    engine = TEDirectDB

    def test_retry_writes_rows_the_failed_attempt_marked_committed(self) -> None:
        """A failed write used to leave no_duplicate keys in _committed_nodup_keys, so the retry skipped them."""
        row = self.te.set(m_file_name.table_id, (None, "kernel/sched.c"))
        _GatedDB.fail_tables = {"m_file_name"}
        with self.assertRaises(RuntimeError):
            self.te.commit_all()
        _GatedDB.fail_tables = set()
        self.te.commit_all()
        self.assertIn(row[0], _stored(m_file_name))

    def test_queues_are_cleared_when_every_row_was_already_committed(self) -> None:
        self.te.set(m_file_name.table_id, (None, "init/main.c"))
        key = next(iter(self.te.queued_set[m_file_name.table_id]))
        self.te._committed_nodup_keys[m_file_name.table_id][key] = 99
        self.te.commit_all()
        self.assertEqual(self.te.queued_set[m_file_name.table_id], {})


class TestStageCounts(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = getattr(G, "STAGE_PROFILER", None)
        G.STAGE_PROFILER = PipelineStageProfiler()

    def tearDown(self) -> None:
        G.STAGE_PROFILER = self.saved

    def test_counts_add_up_over_versions(self) -> None:
        for headers in (7003, 149, 357):
            with G.measure_stage("Stage 1: Header Wave Resolution", count=headers, units="headers"):
                pass
        for streamed in (10288, 164):
            with G.measure_stage("Stage 2: Source Streaming & Parsing", count=99999, units="sources") as metric:
                metric.count = streamed
        stages = G.STAGE_PROFILER.stages
        self.assertEqual(stages["Stage 1: Header Wave Resolution"].count, 7509)
        self.assertEqual(stages["Stage 2: Source Streaming & Parsing"].count, 10452)

    def test_profiles_without_parsing_are_not_counted(self) -> None:
        prof = G.STAGE_PROFILER
        prof.record_worker_profiler(PipelineProfiler(file_path="drivers/"))
        prof.record_worker_profiler(PipelineProfiler(file_path="a.c", clang_parse_tu_s=0.2, include_symbols_s=0.1))
        self.assertEqual(prof.worker_count, 1)
        self.assertAlmostEqual(prof.worker_aggregate.include_symbols_s, 0.1)
        report = prof.format_stage_report()
        self.assertIn("Include Symbols", report)
        self.assertNotIn("CS Execution", report)

    def test_percent_cells_have_the_same_visible_width(self) -> None:
        strip = re.compile(r"\x1b\[[0-9;]*m")
        for value in (0.4, 30.0, 69.4):
            self.assertEqual(len(strip.sub("", _format_pct(value, 100.0, 9))), 9)


def _reporting_worker(conn, vid, gp_ref, mf_ref, worker_id=0) -> None:
    """Fake worker that answers every task and reports a fixed time split with its done frame."""
    while True:
        task = pipeline.worker_protocol._recv_task(conn)
        if task is None:
            break
        batch_id, files = task
        conn.send_bytes(pipeline.worker_protocol._encode_batch_result(batch_id, {f: 1 for f in files}, [], compress=False))
    split = dict.fromkeys(pipeline.worker_protocol._WORKER_SPLIT_KEYS, 0.0)
    split.update(busy_s=3.0, task_wait_s=1.0, db_reads=5, symbol_stats={"refs reused": 2})
    conn.send_bytes(pipeline.worker_protocol._FRAME_DONE + pickle.dumps(split))
    conn.close()


class TestStageTimeSplit(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (G.DB, G.TE)
        MockDB.reset()
        G.DB = MockDB
        G.TE = TECachedDB(hugepages="off")
        G.TE.start(main.gp.Table_Array, MockDB)
        signal.signal(signal.SIGALRM, self._timeout)
        signal.alarm(120)

    def tearDown(self) -> None:
        signal.alarm(0)
        G.TE.close()
        G.DB, G.TE = self.saved
        gc.unfreeze()
        MockDB.reset()

    @staticmethod
    def _timeout(signum, frame) -> None:
        raise TimeoutError("_run_worker_stage hung")

    def test_workers_report_their_time_split(self) -> None:
        ingested: dict = {}
        result = pipeline.worker_stage._run_worker_stage(
            "[test]", [(0, ["a.c"]), (1, ["b.c"]), (2, ["c.c"])], ingested.update, 2, [], worker_target=_reporting_worker
        )
        self.assertEqual(set(ingested), {"a.c", "b.c", "c.c"})
        self.assertEqual(result.split["workers"], 2)
        self.assertEqual(result.split["worker_busy_s"], 6.0)
        self.assertEqual(result.split["worker_db_reads"], 10)
        self.assertGreaterEqual(result.split["ingest_s"], 0.0)
        self.assertFalse(G.TE.async_commits, "background commits end with the stage")
        text = pipeline.worker_stage._format_time_split(result.split)
        self.assertIn("workers 75% parsing", text)
        self.assertIn("25% without a task", text)
        self.assertEqual(pipeline.worker_stage.WORKER_SYMBOL_STATS["refs reused"], 4)
        with self.assertLogs(main.logger, "INFO") as logs:
            pipeline.worker_stage._log_symbol_stats("v-test", Counter(SYMBOL_STATS))
        self.assertIn("4 items stored in tags (4 of them in reused tags", logs.output[0])
        self.assertFalse(pipeline.worker_stage.WORKER_SYMBOL_STATS)


class TestBatchBytes(unittest.TestCase):
    def test_single_epoch_batches_share_the_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            files = []
            for i, size in enumerate([400_000] + [10_000] * 40):
                with open(os.path.join(d, f"f{i}.c"), "wb") as f:
                    f.write(b"x" * size)
                files.append(f"M\tf{i}.c")
            epochs = pipeline.source_order.partition_source_epochs(files, working_dir=d, normal_batch_size=50, num_workers=2)
        self.assertEqual(len(epochs), 1)
        batches = epochs[0]
        self.assertEqual(batches[0][1], ["M\tf0.c"], "the large file is its own batch, dispatched first")
        self.assertGreater(len(batches), 2)


class TestPendingReadBypass(_EngineCase):
    """O-01: reads search the in-flight batch and skip joining `_pending` when they miss or don't overlap."""

    def test_get_hits_pending_and_misses_read_db_without_joining(self) -> None:
        self.te.async_commits = True
        sym = self._stage_symbol("in_flight")
        _GatedDB.gate.clear()
        self.te.commit_all()
        try:
            before = self.te.db_reads
            # Hit in _pending by PK: returns without joining or querying DB
            found = self.te.get(m_blob_symbol_def.table_id, (sym[0],) + (None,) * (m_blob_symbol_def.length - 1))
            self.assertEqual(found[0], sym[0])
            self.assertEqual(self.te.db_reads - before, 0)
            self.assertIsNotNone(self.te._pending)

            # Miss in _pending: queries DB without joining _pending
            miss = self.te.get(m_blob_symbol_def.table_id, (sym[0] + 1000,) + (None,) * (m_blob_symbol_def.length - 1))
            self.assertIsNone(miss)
            self.assertEqual(self.te.db_reads - before, 1)
            self.assertIsNotNone(self.te._pending)
        finally:
            _GatedDB.gate.set()
            self.te.wait_for_commit()

    def test_get_joins_when_table_has_updates_in_flight(self) -> None:
        self.te.async_commits = True
        sym = self._stage_symbol("to_update")
        self.te.commit_all()
        self.te.wait_for_commit()
        updated = (sym[0], sym[1], sym[2], sym[3], "updated_name", sym[5], sym[6], sym[7], sym[8])
        self.te.update(m_blob_symbol_def.table_id, updated)
        _GatedDB.gate.clear()
        self.te.commit_all()
        self.assertIsNotNone(self.te._pending)
        threading.Timer(0.1, _GatedDB.gate.set).start()
        found = self.te.get(m_blob_symbol_def.table_id, (sym[0],) + (None,) * (m_blob_symbol_def.length - 1))
        self.assertIsNone(self.te._pending, "in-flight update must join the commit")
        self.assertEqual(found[4], "updated_name")

    def test_get_in_skips_join_without_overlap_and_joins_on_overlap(self) -> None:
        self.te.async_commits = True
        # Commit an earlier row first so DB has something to return
        early = self._stage_symbol("early")
        self.te.commit_all()
        self.te.wait_for_commit()

        # Now stage two new rows in flight
        s1 = self._stage_symbol("flight_1")
        s2 = self._stage_symbol("flight_2")
        _GatedDB.gate.clear()
        self.te.commit_all()
        try:
            # 1. PK column (col 0) with values strictly outside [s1[0], s2[0]]: no join
            got = self.te.get_in(m_blob_symbol_def.table_id, 0, [early[0]])
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0][0], early[0])
            self.assertIsNotNone(self.te._pending, "non-overlapping PK range must not join")

            # 2. Non-PK column (col 4 = symbol name) without overlap: no join
            got_name = self.te.get_in(m_blob_symbol_def.table_id, 4, ["early", "absent"])
            self.assertEqual(len(got_name), 1)
            self.assertIsNotNone(self.te._pending, "non-overlapping non-PK values must not join")

            # 3. Overlapping value on PK column: joins the commit
            threading.Timer(0.1, _GatedDB.gate.set).start()
            got_overlap = self.te.get_in(m_blob_symbol_def.table_id, 0, [s1[0]])
            self.assertIsNone(self.te._pending, "overlapping get_in must join the commit")
            self.assertEqual(got_overlap[0][0], s1[0])
        finally:
            _GatedDB.gate.set()
            self.te.wait_for_commit()


class TestDeferredCommitError(_EngineCase):
    """B-18: a failed background commit joined by `_await_writes()` restores rows and raises on commit/wait/close."""

    def test_await_writes_restores_and_defers_error_to_wait_commit_close(self) -> None:
        for trigger_method in ("wait_for_commit", "commit_all", "close"):
            with self.subTest(trigger=trigger_method):
                self.te.async_commits = True
                row = self.te.set(m_file_name.table_id, (None, f"drivers/{trigger_method}.c"))
                _GatedDB.fail_tables = {"m_file_name"}
                self.te.commit_all()
                self.te._pending.thread.join()
                # _await_writes() must NOT raise inside an unrelated read, and must restore the failed row
                self.te._await_writes((m_file_name.table_id,))
                self.assertIn(self.te._sanitize_key((f"drivers/{trigger_method}.c",)), self.te.queued_set[m_file_name.table_id])
                _GatedDB.fail_tables = set()
                with self.assertRaises(RuntimeError):
                    getattr(self.te, trigger_method)()
                if trigger_method == "close":
                    # Re-open for tearDown / next subtest
                    self.te.start_new_db(_GatedDB)
                else:
                    # Subsequent commit succeeds and writes the restored row
                    self.te.commit_all()
                    self.te.wait_for_commit()
                    self.assertIn(row[0], _stored(m_file_name))


if __name__ == "__main__":
    unittest.main()
