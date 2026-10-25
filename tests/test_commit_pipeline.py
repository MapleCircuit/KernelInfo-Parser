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
import os
import pickle
import re
import signal
import tempfile
import threading
import unittest

import mysql.connector

import main
from core.DBLayout import m_ast, m_blob_symbol_def, m_bridge_checkpoint, m_bridge_tag, m_file_name, m_tag
from core.GreatProcessor import GreatProcessor
from core.globalstuff import G
from core.Profiler import PipelineProfiler, PipelineStageProfiler, _format_pct
from db_engine import MockDB
from db_engine.DBHandling import MariaDB, parse_server_setting
from table_engine import TECachedDB, TEDirectDB


def _reset_mock_db() -> None:
    MockDB._global_store.clear()
    MockDB._global_next_id.clear()


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
    return MockDB._global_store.get(table.table_name, {})


class _EngineCase(unittest.TestCase):
    engine = TECachedDB

    def setUp(self) -> None:
        self.saved = (G.DB, G.TE)
        _reset_mock_db()
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
        _reset_mock_db()

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
        threading.Timer(0.2, _GatedDB.gate.set).start()
        # m_blob_symbol_def is not cached: this read goes to the database and must see the pending write
        found = self.te.get(m_blob_symbol_def.table_id, (row[0],) + (None,) * (m_blob_symbol_def.length - 1))
        self.assertEqual(found[0], row[0])
        self.assertEqual(self.te.pending_commit_rows, 0)
        self.assertGreaterEqual(self.te.commit_stats["blocked_s"], 0.1)
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
        main._end_background_commits("[test]")
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


class TestIdleCommit(_EngineCase):
    def test_waiting_parent_commits_in_the_background(self) -> None:
        self.te.async_commits = True
        self._stage_symbol("idle")
        saved = main._IDLE_COMMIT_MIN_ROWS
        try:
            main._IDLE_COMMIT_MIN_ROWS = 2
            main._maybe_idle_commit()
            self.assertEqual(self.te.staged_rows, 1, "below the threshold nothing is committed")
            main._IDLE_COMMIT_MIN_ROWS = 1
            main._maybe_idle_commit()
            self.assertEqual(self.te.staged_rows, 0)
            self.te.wait_for_commit()
            self.assertEqual(self.te.commit_stats["background"], 1)
        finally:
            main._IDLE_COMMIT_MIN_ROWS = saved

    def test_no_commit_outside_a_stage(self) -> None:
        self._stage_symbol("idle")
        saved = main._IDLE_COMMIT_MIN_ROWS
        try:
            main._IDLE_COMMIT_MIN_ROWS = 1
            main._maybe_idle_commit()
            self.assertEqual(self.te.staged_rows, 1)
        finally:
            main._IDLE_COMMIT_MIN_ROWS = saved


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


class _FakeCursor:
    def execute(self, *args, **kwargs) -> None:
        pass


class _FakeCnx:
    def __init__(self) -> None:
        self.connection_id = 1
        self.autocommit = True
        self.lost = False
        self.commits = 0
        self.rollbacks = 0

    def ping(self, reconnect=False, attempts=1, delay=0) -> None:
        if self.lost:
            if not reconnect:
                raise mysql.connector.InterfaceError("server gone")
            self.lost = False
            self.connection_id += 1          # mysql-connector reconnects silently with a new session

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _offline_mariadb() -> MariaDB:
    db = MariaDB.__new__(MariaDB)
    db.cnx = _FakeCnx()
    db.cursor = _FakeCursor()
    db.sessions = 0

    def _init_session() -> None:
        db.sessions += 1

    db._init_session = _init_session
    return db


class TestMariaDBSessions(unittest.TestCase):
    def test_silent_reconnect_restores_session_settings(self) -> None:
        db = _offline_mariadb()
        db.cnx.lost = True
        db.check_if_connected()
        self.assertEqual(db.sessions, 1, "a reconnected session lacks sql_mode and the disabled checks")

    def test_transaction_fails_instead_of_reconnecting(self) -> None:
        db = _offline_mariadb()
        with self.assertRaises(ConnectionError):
            with db.transaction():
                self.assertFalse(db.cnx.autocommit)
                db.cnx.lost = True
                db.check_if_connected()
        self.assertEqual(db.cnx.rollbacks, 1)
        self.assertEqual(db.cnx.commits, 0)
        self.assertTrue(db.cnx.autocommit)
        self.assertFalse(db._in_txn)
        self.assertEqual(db.sessions, 0)

    def test_transaction_commits_once(self) -> None:
        db = _offline_mariadb()
        with db.transaction():
            pass
        self.assertEqual((db.cnx.commits, db.cnx.rollbacks), (1, 0))


class _TuningCursor:
    """Answers SELECT @@GLOBAL.x / SET GLOBAL x like a server that caps some variables."""

    def __init__(self, values: dict, caps: dict) -> None:
        self.values = dict(values)
        self.caps = caps
        self.row = None

    def execute(self, sql: str, params=None) -> None:
        m = re.match(r"SELECT @@GLOBAL\.(\w+)", sql)
        if m:
            if m.group(1) not in self.values:
                raise mysql.connector.ProgrammingError(msg=f"Unknown system variable '{m.group(1)}'")
            self.row = (self.values[m.group(1)],)
            return
        name = sql.split()[2]
        self.values[name] = min(params[0], self.caps[name]) if name in self.caps else params[0]

    def fetchone(self):
        return self.row


class TestServerTuning(unittest.TestCase):
    def _db(self, values: dict, caps: dict | None = None) -> MariaDB:
        db = _offline_mariadb()
        db.cursor = _TuningCursor(values, caps or {})
        return db

    def test_sizes_parse_and_only_grow(self) -> None:
        self.assertEqual(parse_server_setting("8G"), 8 * 1024**3)
        self.assertEqual(parse_server_setting("O_DIRECT"), "O_DIRECT")
        db = self._db({"innodb_log_file_size": 4 * 1024**3, "innodb_flush_log_at_trx_commit": 1})
        notes = db.apply_server_tuning({"innodb_log_file_size": "2G", "innodb_flush_log_at_trx_commit": 2})
        self.assertEqual(notes, ["innodb_flush_log_at_trx_commit: 1 -> 2"])
        self.assertEqual(db.cursor.values["innodb_log_file_size"], 4 * 1024**3)

    def test_capped_and_unknown_variables_are_reported(self) -> None:
        db = self._db({"innodb_buffer_pool_size": 128 * 1024**2}, caps={"innodb_buffer_pool_size": 128 * 1024**2})
        notes = db.apply_server_tuning({"innodb_buffer_pool_size": "8G", "no_such_var": 1, "bad name": 1})
        self.assertTrue(any("capped at 134217728" in n and "restart" in n for n in notes), notes)
        self.assertTrue(any(n.startswith("no_such_var: not available") for n in notes), notes)
        self.assertTrue(any(n.startswith("bad name: skipped") for n in notes), notes)


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
        task = main._recv_task(conn)
        if task is None:
            break
        batch_id, files = task
        conn.send_bytes(main._encode_batch_result(batch_id, {f: 1 for f in files}, [], compress=False))
    split = dict.fromkeys(main._WORKER_SPLIT_KEYS, 0.0)
    split.update(busy_s=3.0, task_wait_s=1.0, db_reads=5, symbol_stats={"refs reused": 2})
    conn.send_bytes(main._FRAME_DONE + pickle.dumps(split))
    conn.close()


class TestStageTimeSplit(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (G.DB, G.TE)
        _reset_mock_db()
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
        _reset_mock_db()

    @staticmethod
    def _timeout(signum, frame) -> None:
        raise TimeoutError("_run_worker_stage hung")

    def test_workers_report_their_time_split(self) -> None:
        ingested: dict = {}
        result = main._run_worker_stage(
            "[test]", [(0, ["a.c"]), (1, ["b.c"]), (2, ["c.c"])], ingested.update, 2, [], worker_target=_reporting_worker
        )
        self.assertEqual(set(ingested), {"a.c", "b.c", "c.c"})
        self.assertEqual(result.split["workers"], 2)
        self.assertEqual(result.split["worker_busy_s"], 6.0)
        self.assertEqual(result.split["worker_db_reads"], 10)
        self.assertGreaterEqual(result.split["ingest_s"], 0.0)
        self.assertFalse(G.TE.async_commits, "background commits end with the stage")
        text = main._format_time_split(result.split)
        self.assertIn("workers 75% parsing", text)
        self.assertIn("25% without a task", text)
        self.assertEqual(main.WORKER_SYMBOL_STATS["refs reused"], 4)
        with self.assertLogs(main.logger, "INFO") as logs:
            main._log_symbol_stats("v-test", main.Counter(main.SYMBOL_STATS))
        self.assertIn("4 items stored in tags (4 of them in reused tags", logs.output[0])
        self.assertFalse(main.WORKER_SYMBOL_STATS)


class TestBatchBytes(unittest.TestCase):
    def test_single_epoch_batches_share_the_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            files = []
            for i, size in enumerate([400_000] + [10_000] * 40):
                with open(os.path.join(d, f"f{i}.c"), "wb") as f:
                    f.write(b"x" * size)
                files.append(f"M\tf{i}.c")
            epochs = main.partition_source_epochs(files, working_dir=d, normal_batch_size=50, num_workers=2)
        self.assertEqual(len(epochs), 1)
        batches = epochs[0]
        self.assertEqual(batches[0][1], ["M\tf0.c"], "the large file is its own batch, dispatched first")
        self.assertGreater(len(batches), 2)


if __name__ == "__main__":
    unittest.main()
