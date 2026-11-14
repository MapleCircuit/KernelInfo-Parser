"""tests/test_multicore_pipeline.py - Regression tests for pipeline.multicore.trigger_multicore and its worker pool.

Covers worker change selection after blob deduplication, memory-mode budgets, ChangeSet evacuation
bookkeeping, the shared symbol segment lifecycle and file scoping, in-flight reference blocking,
worker-side stub suppression, crash detection and retry in `_run_worker_stage`, the background git
commit worker handoff, and largest-first epoch batch ordering.
"""
import gc
import os
import pickle
import signal
import tempfile
import types
import unittest
from unittest import mock

import main
import pipeline.changesets
import pipeline.file_processing
import pipeline.git_commit_worker
import pipeline.multicore
import pipeline.scheduler
import pipeline.source_order
import pipeline.systemic
import pipeline.unchanged
import pipeline.worker_protocol
import pipeline.worker_stage
from core.DBLayout import m_ast
from core.GreatProcessor import GreatProcessor
from core.globalstuff import FILE_ERROR, G, REF_FILE
from core.TableHandling import ChangeSet
from db_engine import MockDB
from tests.db_fixtures import make_db
from table_engine.shared_buffer import SharedSymbolBuilder, SharedSymbolChain


class _RecordingTE:
    """TableEngine stand-in: every lookup misses and every insert is recorded."""

    def __init__(self) -> None:
        self.set_calls: list[tuple[int, tuple]] = []
        self.db = None

    def get(self, table_id: int, columns: tuple) -> None:
        return None

    def set(self, table_id: int, columns: tuple) -> tuple:
        self.set_calls.append((table_id, columns))
        return (900 + len(self.set_calls), *columns[1:])

    def commit_all(self, *args, **kwargs) -> None:
        return None

    def start_new_db(self, *args, **kwargs) -> None:
        return None


def _resolver_gp(**overrides) -> types.SimpleNamespace:
    """Minimal GreatProcessor stand-in for ChangeSet.resolve_ref."""
    attrs = {
        "file_symbols": {},
        "file_names": {},
        "_changed_paths_set": set(),
        "completed_paths": set(),
        "ChangeSet_Dict": {},
        "_ast_staged_symbols": {},
        "_ast_staged_names": {},
        "_ast_negative_cache": set(),
        "_shared_symbols": None,
    }
    attrs.update(overrides)
    return types.SimpleNamespace(**attrs)


def _crashing_worker(conn, vid, gp_ref, mf_ref, worker_id=0) -> None:
    """Fake parsing worker speaking the real frame protocol; dies on any batch naming a 'crash' file.

    Each result maps a file to the size of the batch it arrived in, so tests can see how files were batched.
    """
    while True:
        task = pipeline.worker_protocol._recv_task(conn)
        if task is None:
            break
        batch_id, files = task
        if any("crash" in f for f in files):
            os.kill(os.getpid(), signal.SIGKILL)
        conn.send_bytes(pipeline.worker_protocol._encode_batch_result(batch_id, {f: len(files) for f in files}, [], compress=False))
    conn.send_bytes(pipeline.worker_protocol._FRAME_DONE)
    conn.close()


def _failing_worker(conn, vid, gp_ref, mf_ref, worker_id=0) -> None:
    """Fake parsing worker that reports the same error for every file, like broken libclang bindings."""
    while True:
        task = pipeline.worker_protocol._recv_task(conn)
        if task is None:
            break
        batch_id, files = task
        errors = [(f"M\t{f}", "Tried calling method on a null-cursor.", _NULL_CURSOR_TB) for f in files]
        conn.send_bytes(pipeline.worker_protocol._encode_batch_result(batch_id, {}, errors, compress=False))
    conn.send_bytes(pipeline.worker_protocol._FRAME_DONE)
    conn.close()


_REPO = os.path.dirname(os.path.abspath(main.__file__))
_NULL_CURSOR_TB = (
    "Traceback (most recent call last):\n"
    f'  File "{_REPO}/parser/c_ast/c_ast.py", line 137, in c_ast_parse\n'
    "    process_c_ast(CS)\n"
    f'  File "{_REPO}/parser/c_ast/cursor_tree.py", line 2607, in exec_punctuation\n'
    "    kids = tuple(cursor.get_children())\n"
    '  File "/usr/lib/python3.14/site-packages/clang/cindex.py", line 1629, in inner\n'
    '    raise Exception("Tried calling method on a null-cursor.")\n'
    "Exception: Tried calling method on a null-cursor.\n"
)


def _unstartable_worker(conn, vid, gp_ref, mf_ref, worker_id=0) -> None:
    """Fake worker that cannot reach the database."""
    conn.send_bytes(pipeline.worker_protocol._FRAME_INIT_FAILED + pickle.dumps((f"Worker {worker_id}", "DB connection failed: refused", "")))
    conn.close()


class TestWorkerChangeSelection(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (main.gp.worker_change_list, main.gp.Change_List)

    def tearDown(self) -> None:
        main.gp.worker_change_list, main.gp.Change_List = self._saved

    def test_fully_deduplicated_version_parses_nothing(self) -> None:
        main.gp.Change_List = ["M\tinclude/a.h", "M\tkernel/b.c"]
        main.gp.worker_change_list = []
        self.assertEqual(pipeline.worker_stage._select_worker_changes(), [])

    def test_without_dedup_uses_change_list(self) -> None:
        main.gp.Change_List = ["M\tkernel/b.c"]
        main.gp.worker_change_list = None
        self.assertEqual(pipeline.worker_stage._select_worker_changes(), ["M\tkernel/b.c"])


class TestMemoryProfile(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (G.MEMORY_MODE, G.CPUS)
        G.CPUS = 16

    def tearDown(self) -> None:
        G.MEMORY_MODE, G.CPUS = self._saved

    def test_budgets_per_mode(self) -> None:
        G.MEMORY_MODE = "very_low"
        self.assertEqual(
            pipeline.worker_stage._memory_profile(),
            {"workers": 2, "header_batch": 25, "normal_batch": 25, "heavy_batch": 10, "max_batch_bytes": 350_000, "epoch_files": 2000},
        )
        G.MEMORY_MODE = "low"
        self.assertEqual(
            pipeline.worker_stage._memory_profile(),
            {"workers": 8, "header_batch": 50, "normal_batch": 50, "heavy_batch": 25, "max_batch_bytes": 750_000, "epoch_files": 4500},
        )
        G.MEMORY_MODE = "normal"
        self.assertEqual(
            pipeline.worker_stage._memory_profile(),
            {"workers": 15, "header_batch": 200, "normal_batch": 200, "heavy_batch": 50, "max_batch_bytes": 1_500_000, "epoch_files": 4500},
        )


class TestEvacuationBookkeeping(unittest.TestCase):
    def test_worker_changeset_without_gp_registers_blob_and_tags(self) -> None:
        """execute_and_purge runs worker ChangeSets whose gp was cleared; their tags must still reach commits."""
        main.gp.file_blob_ids.clear()
        main.gp.file_pre_extracted_tags.clear()
        cs = ChangeSet("A\tDocumentation/foo.txt")
        cs.gp = None
        cs.blob_id = 5
        cs.pre_extracted_tags = [(11, 5, 1, 3)]
        cs.cs_processed = True
        try:
            pipeline.changesets.extract_tags_and_evacuate_cs(cs)
            self.assertEqual(main.gp.file_blob_ids.get("Documentation/foo.txt"), 5)
            self.assertEqual(main.gp.file_pre_extracted_tags.get("Documentation/foo.txt"), [(11, 5, 1, 3)])
        finally:
            main.gp.file_blob_ids.clear()
            main.gp.file_pre_extracted_tags.clear()
            main.gp.completed_paths.discard("Documentation/foo.txt")


class TestSharedSymbols(unittest.TestCase):
    def _chain(self, *file_maps: dict) -> SharedSymbolChain:
        chain = SharedSymbolChain()
        for file_map in file_maps:
            chain.append(SharedSymbolBuilder(mode="off").build_file_scoped(file_map))
        return chain

    def test_lookups_are_scoped_to_the_defining_file(self) -> None:
        chain = self._chain({
            "arch/x86/include/asm/page.h": {("PAGE_SIZE", 7): 10},
            "arch/arm/include/asm/page.h": {("PAGE_SIZE", 7): 20},
        })
        try:
            self.assertEqual(chain.get_file_symbol("arch/x86/include/asm/page.h", "PAGE_SIZE", 7), 10)
            self.assertEqual(chain.get_file_symbol("arch/arm/include/asm/page.h", "PAGE_SIZE", 7), 20)
            self.assertEqual(chain.get_file_name("arch/arm/include/asm/page.h", "PAGE_SIZE"), 20)
            self.assertIsNone(chain.get_file_symbol("include/linux/mm.h", "PAGE_SIZE", 7))
        finally:
            chain.close()

    def test_newer_segment_shadows_older(self) -> None:
        chain = self._chain({"a.h": {("foo", 1): 1}}, {"a.h": {("foo", 1): 2}, "b.h": {("bar", 1): 3}})
        try:
            self.assertEqual(chain.get_file_symbol("a.h", "foo", 1), 2)
            self.assertEqual(chain.get_file_symbol("b.h", "bar", 1), 3)
            self.assertEqual(chain.sym_count, 3)
        finally:
            chain.close()
        self.assertEqual(chain.segments, [])

    def test_bulk_initialized_table_matches_lookups(self) -> None:
        symbols = {(f"sym_{i}", i % 5): i for i in range(2000)}
        reader = SharedSymbolBuilder(mode="off").build(symbols)
        try:
            for (name, sym_type), ast_id in symbols.items():
                self.assertEqual(reader.get_symbol(name, sym_type), ast_id)
            self.assertIsNone(reader.get_symbol("missing", 1))
            self.assertEqual(len(dict(reader.items())), len(symbols))
        finally:
            reader.close()

    def test_segments_released_between_versions(self) -> None:
        gp = GreatProcessor()
        for release in (gp.init_cs_dict, gp.reset_cs):
            chain = self._chain({"a.h": {("foo", 1): 1}})
            gp._shared_symbols = chain
            release()
            self.assertIsNone(gp._shared_symbols)
            self.assertEqual(chain.segments, [])

    def test_finalize_releases_segments(self) -> None:
        gp = GreatProcessor()
        gp._shared_symbols = self._chain({"a.h": {("foo", 1): 1}})
        old_te = G.TE
        G.TE = _RecordingTE()
        try:
            pipeline.scheduler.DependencyScheduler(gp).finalize()
        finally:
            G.TE = old_te
        self.assertIsNone(gp._shared_symbols)


class TestReferenceResolution(unittest.TestCase):
    def setUp(self) -> None:
        self.old_te = G.TE
        self.te = _RecordingTE()
        G.TE = self.te

    def tearDown(self) -> None:
        G.TE = self.old_te

    def test_in_flight_changed_file_blocks_instead_of_using_previous_version(self) -> None:
        db_calls: list[str] = []

        def _old_version_symbols(rel_file: str) -> dict:
            db_calls.append(rel_file)
            return {("pci_dev", 1): 111}

        cs = ChangeSet("M\tdrivers/pci/probe.c")
        cs.gp = _resolver_gp(_changed_paths_set={"include/linux/pci.h"}, get_file_symbols_from_db=_old_version_symbols)
        self.assertIsNone(cs.resolve_ref(m_ast.ast_id, (REF_FILE, "include/linux/pci.h", "pci_dev", 1)))
        self.assertEqual(cs.blocked_on, "include/linux/pci.h")
        self.assertEqual(db_calls, [])

    def test_completed_changed_file_still_uses_db(self) -> None:
        cs = ChangeSet("M\tdrivers/pci/probe.c")
        cs.gp = _resolver_gp(
            _changed_paths_set={"include/linux/pci.h"},
            completed_paths={"include/linux/pci.h"},
            get_file_symbols_from_db=lambda rel_file: {("pci_dev", 1): 222},
        )
        self.assertEqual(cs.resolve_ref(m_ast.ast_id, (REF_FILE, "include/linux/pci.h", "pci_dev", 1)), 222)

    def test_worker_never_stages_stubs(self) -> None:
        route = (REF_FILE, "include/linux/kernel.h", "implicit_fn", 21)
        cs = ChangeSet("M\tkernel/fork.c")
        cs.gp = _resolver_gp(_is_worker=True, get_file_symbols_from_db=lambda rel_file: {})
        self.assertIsNone(cs.resolve_ref(m_ast.ast_id, route))
        self.assertEqual(self.te.set_calls, [])

        cs_parent = ChangeSet("M\tkernel/fork.c")
        cs_parent.gp = _resolver_gp(get_file_symbols_from_db=lambda rel_file: {})
        self.assertIsNotNone(cs_parent.resolve_ref(m_ast.ast_id, route))
        self.assertEqual(len(self.te.set_calls), 1)

    def test_pre_resolution_binds_each_reference_to_its_target_file(self) -> None:
        chain = SharedSymbolChain()
        chain.append(SharedSymbolBuilder(mode="off").build_file_scoped({
            "arch/x86/include/asm/page.h": {("PAGE_SIZE", 7): 10},
            "arch/arm/include/asm/page.h": {("PAGE_SIZE", 7): 20},
        }))
        try:
            cs = ChangeSet("M\tarch/arm/mm/init.c")
            cs.gp = _resolver_gp(_is_worker=True, get_file_symbols_from_db=lambda rel_file: {})
            ref = cs.ref(m_ast.ast_id, REF_FILE, "arch/arm/include/asm/page.h", "PAGE_SIZE", 7)
            cs.store((m_ast.table_id, 1, (None, "init_fn", 3, ref)))
            cs.pre_resolve_operations(chain)
            self.assertEqual(cs.cs[0][2][3], 20)
        finally:
            chain.close()


class TestWorkerStage(unittest.TestCase):
    def setUp(self) -> None:
        self.old_db = G.DB
        G.DB = MockDB
        signal.signal(signal.SIGALRM, self._timeout)
        signal.alarm(120)

    def tearDown(self) -> None:
        signal.alarm(0)
        G.DB = self.old_db
        gc.unfreeze()

    @staticmethod
    def _timeout(signum, frame) -> None:
        raise TimeoutError("_run_worker_stage hung")

    def test_crashed_worker_is_detected_and_its_batch_retried(self) -> None:
        ingested: dict[str, int] = {}
        errors: list = []
        batches = [(0, ["a.c", "b.c"]), (1, ["c.c", "crash.c", "d.c"]), (2, ["e.c"])]
        result = pipeline.worker_stage._run_worker_stage(
            "[test]", batches, ingested.update, 2, errors, worker_target=_crashing_worker
        )
        self.assertEqual(set(ingested), {"a.c", "b.c", "c.c", "d.c", "e.c"})
        self.assertEqual(result.lost_files, ["crash.c"])
        self.assertIn("crash.c", [e[0] for e in errors])
        self.assertGreaterEqual(result.failed_workers, 2)

    def test_only_the_batch_being_parsed_is_split_after_a_crash(self) -> None:
        """Batches queued behind the crashing one keep their shape; the crashing batch is retried file by file."""
        ingested: dict[str, int] = {}
        errors: list = []
        batches = [(0, ["a.c", "b.c"]), (1, ["c.c", "crash.c", "d.c"]), (2, ["e.c", "f.c"]), (3, ["g.c", "h.c"])]
        result = pipeline.worker_stage._run_worker_stage("[test]", batches, ingested.update, 1, errors, worker_target=_crashing_worker)
        self.assertEqual(result.lost_files, ["crash.c"])
        self.assertEqual(ingested, {"a.c": 2, "b.c": 2, "c.c": 1, "d.c": 1, "e.c": 2, "f.c": 2, "g.c": 2, "h.c": 2})

    def test_stage_aborts_when_no_worker_can_start(self) -> None:
        errors: list = []
        with self.assertRaises(RuntimeError):
            pipeline.worker_stage._run_worker_stage("[test]", [(0, ["a.c"]), (1, ["b.c"])], lambda batch: None, 2, errors,
                                   worker_target=_unstartable_worker)
        self.assertTrue(any("DB connection failed" in e[1] for e in errors))

    def test_ingest_failure_is_recorded_and_stage_continues(self) -> None:
        ingested: dict[str, str] = {}
        errors: list = []

        def _ingest(batch: dict) -> None:
            if "bad.c" in batch:
                raise RuntimeError("commit failed")
            ingested.update(batch)

        batches = [(0, ["a.c"]), (1, ["bad.c"]), (2, ["c.c"])]
        result = pipeline.worker_stage._run_worker_stage("[test]", batches, _ingest, 2, errors, worker_target=_crashing_worker)
        self.assertEqual(set(ingested), {"a.c", "c.c"})
        self.assertEqual(result.failed_workers, 0)
        self.assertTrue(any("commit failed" in e[1] for e in errors))

    def test_on_started_runs_with_db_connection(self) -> None:
        seen: list = []
        pipeline.worker_stage._run_worker_stage(
            "[test]", [(0, ["a.c"])], lambda batch: None, 1, [],
            on_started=lambda: seen.append(G.TE.db), worker_target=_crashing_worker,
        )
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])

    def test_same_error_on_most_files_stops_the_stage(self) -> None:
        """A stage stops as soon as one error message covers most files, without ingesting the rest."""
        errors: list = []
        ingested: list = []
        batches = [(i, [f"f{i}_{j}.c" for j in range(2)]) for i in range(20)]
        with self.assertLogs(main.logger, level="CRITICAL"), self.assertRaises(pipeline.systemic.SystemicParseError) as ctx:
            pipeline.worker_stage._run_worker_stage("[test]", batches, ingested.append, 2, errors, worker_target=_failing_worker)
        message = str(ctx.exception)
        self.assertIn("'Tried calling method on a null-cursor.'", message)
        self.assertIn("clang/cindex.py:1629 in inner, called from parser/c_ast/cursor_tree.py:2607 in exec_punctuation", message)
        self.assertLess(len(ingested), len(batches))


class TestSystemicFailure(unittest.TestCase):
    """pipeline.systemic._raise_if_systemic(): one error across most files stops the version."""

    @staticmethod
    def _errors(n: int, message: str = "boom") -> list:
        return [(f"M\tdir/f{i}.c", message, _NULL_CURSOR_TB) for i in range(n)]

    def test_scattered_file_errors_do_not_stop(self) -> None:
        errors = [(f"M\tf{i}.c", f"error {i}", "") for i in range(30)]
        pipeline.systemic._raise_if_systemic("[test]", errors, 40)

    def test_few_files_do_not_stop(self) -> None:
        pipeline.systemic._raise_if_systemic("[test]", self._errors(pipeline.systemic.SYSTEMIC_ERROR_MIN_FILES - 1), pipeline.systemic.SYSTEMIC_ERROR_MIN_FILES - 1)

    def test_same_error_on_a_minority_of_files_does_not_stop(self) -> None:
        pipeline.systemic._raise_if_systemic("[test]", self._errors(10), 100)

    def test_same_error_on_most_files_stops(self) -> None:
        with self.assertLogs(main.logger, level="CRITICAL"), self.assertRaises(pipeline.systemic.SystemicParseError) as ctx:
            pipeline.systemic._raise_if_systemic("Version 'v3.0.1'", self._errors(63), 82)
        self.assertIn("63 of 82 files", str(ctx.exception))
        self.assertIn("dir/f0.c", str(ctx.exception))

    def test_error_origin_names_the_raising_and_the_calling_frame(self) -> None:
        self.assertEqual(
            pipeline.systemic._error_origin(_NULL_CURSOR_TB),
            "/usr/lib/python3.14/site-packages/clang/cindex.py:1629 in inner, "
            "called from parser/c_ast/cursor_tree.py:2607 in exec_punctuation",
        )
        self.assertEqual(pipeline.systemic._error_origin(""), "")


class TestTriggerMulticore(unittest.TestCase):
    """Drive both stages of trigger_multicore with the fake worker and no scheduler."""

    def setUp(self) -> None:
        # Each name is replaced on the module that looks it up
        self.patched = {
            "file_processing_worker": pipeline.worker_stage,
            "processing_dirs": pipeline.multicore,
            "processing_unchanges": pipeline.multicore,
            "_register_file_names": pipeline.multicore,
        }
        self.saved = {name: getattr(module, name) for name, module in self.patched.items()}
        self.saved_gp = (main.gp.worker_change_list, main.gp.Change_List, main.gp.Version_Name)
        self.old_db, self.old_te = G.DB, G.TE
        G.DB = MockDB
        G.TE = _RecordingTE()
        self.registered: list[list[str]] = []
        pipeline.worker_stage.file_processing_worker = _crashing_worker
        pipeline.multicore.processing_dirs = lambda: {}
        pipeline.multicore.processing_unchanges = lambda: ["A\tdrivers/missing.c"]
        pipeline.multicore._register_file_names = lambda items: self.registered.append(list(items))
        main.gp.Version_Name = "test-version-not-checked-out"
        main.gp.init_cs_dict()
        signal.signal(signal.SIGALRM, TestWorkerStage._timeout)
        signal.alarm(120)

    def tearDown(self) -> None:
        signal.alarm(0)
        for name, value in self.saved.items():
            setattr(self.patched[name], name, value)
        main.gp.worker_change_list, main.gp.Change_List, main.gp.Version_Name = self.saved_gp
        G.DB, G.TE = self.old_db, self.old_te
        main.gp.init_cs_dict()

    def test_headers_missing_files_and_sources_are_all_ingested(self) -> None:
        main.gp.Change_List = ["M\tinclude/linux/a.h", "M\tkernel/b.c", "M\tDocumentation/c.txt"]
        main.gp.worker_change_list = None
        frozen_before = gc.get_freeze_count()
        pipeline.multicore.trigger_multicore()
        # The fake worker keys each result by its change line
        self.assertEqual(
            set(main.gp.ChangeSet_Dict.keys()),
            {"M\tinclude/linux/a.h", "M\tkernel/b.c", "M\tDocumentation/c.txt", "A\tdrivers/missing.c"},
        )
        self.assertIn("drivers/missing.c", main.gp._changed_paths_set)
        self.assertIn(["A\tdrivers/missing.c"], self.registered)
        self.assertIsNone(main.gp._shared_symbols)
        self.assertLessEqual(gc.get_freeze_count(), frozen_before)


class TestGitCommitWorker(unittest.TestCase):
    def setUp(self) -> None:
        import parser.git_ast as git_ast
        self.git_ast = git_ast
        self.saved_parser = git_ast.GitCommitParser

    def tearDown(self) -> None:
        self.git_ast.GitCommitParser = self.saved_parser
        main.gp.stop_commit_worker()

    def _install_parser(self, result) -> None:
        class _FakeParser:
            def __init__(self, repo_dir=None) -> None:
                pass

            def parse_version_commits_with_hunks(self, old_ver, ver, limit=None):
                if isinstance(result, Exception):
                    raise result
                return result

        self.git_ast.GitCommitParser = _FakeParser

    def test_result_handed_over_through_file(self) -> None:
        self._install_parser((["c1", "c2"], {"kernel/fork.c": [1, 2]}))
        pipeline.git_commit_worker._start_git_commit_worker()
        result_dir = main.gp._commit_result_dir
        self.assertEqual(pipeline.git_commit_worker._collect_git_commit_result(), (["c1", "c2"], {"kernel/fork.c": [1, 2]}))
        self.assertIsNone(main.gp._commit_proc)
        self.assertFalse(os.path.exists(result_dir))
        self.assertNotIn(result_dir, main.gp.PURGE_LIST)

    def test_failed_worker_falls_back(self) -> None:
        self._install_parser(RuntimeError("git log failed"))
        pipeline.git_commit_worker._start_git_commit_worker()
        self.assertIsNone(pipeline.git_commit_worker._collect_git_commit_result())


class TestEpochOrdering(unittest.TestCase):
    def test_batches_start_largest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            items = []
            for i, size in enumerate((100, 90_000, 500, 70_000, 200)):
                rel = f"drivers/f{i}.c"
                os.makedirs(os.path.join(tmpdir, "drivers"), exist_ok=True)
                with open(os.path.join(tmpdir, rel), "wb") as f:
                    f.write(b"x" * size)
                items.append(f"M\t{rel}")
            epochs = pipeline.source_order.partition_source_epochs(
                items, working_dir=tmpdir, normal_batch_size=1, heavy_batch_size=1, max_batch_bytes=10**9
            )
            sizes = [os.path.getsize(os.path.join(tmpdir, b[1][0].split("\t")[-1])) for b in epochs[0]]
            self.assertEqual(sizes, sorted(sizes, reverse=True))
            in_order = pipeline.source_order.partition_source_epochs(
                items, working_dir=tmpdir, normal_batch_size=1, heavy_batch_size=1, max_batch_bytes=10**9, largest_first=False
            )
            self.assertEqual([b[0] for b in in_order[0]], [0, 1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()


class _FakeConn:
    """Worker-side pipe end: serves one task frame, then reports a closed pipe; keeps what is sent."""

    def __init__(self, batches: list[tuple[int, list[str]]]) -> None:
        self._frames = [pipeline.worker_protocol._FRAME_TASK + pickle.dumps(b) for b in batches]
        self.sent: list[bytes] = []

    def recv_bytes(self) -> bytes:
        if not self._frames:
            raise EOFError
        return self._frames.pop(0)

    def send_bytes(self, frame) -> None:
        self.sent.append(bytes(frame))

    def close(self) -> None:
        return None


def _run_worker(files: list[str], *, shared_symbols=None, mf=None, parse=None):
    """Run `file_processing_worker` over one batch; returns (ChangeSets by path, errors, calls)."""
    gp_ref = types.SimpleNamespace(
        VID=2, Old_VID=0, Version_Name="v", file_blob_hashes=None, _shared_symbols=shared_symbols,
    )
    captured: dict = {}
    calls = {"prune": 0, "resolve": 0}

    def _encode(batch_id, batch_dict, errors, compress, blob_symbols=None):
        captured["dict"], captured["errors"] = dict(batch_dict), list(errors)
        return b"frame"

    def _prune(self):
        calls["prune"] += 1

    def _resolve(self, shared):
        calls["resolve"] += 1

    with mock.patch.object(G, "TE", _RecordingTE()), \
            mock.patch.object(pipeline.file_processing, "_encode_batch_result", _encode), \
            mock.patch.object(ChangeSet, "prune_unchanged_dependencies", _prune), \
            mock.patch.object(ChangeSet, "pre_resolve_operations", _resolve), \
            mock.patch("parser.raw_ast.raw_ast.raw_ast_parse", parse or (lambda cs: None)), \
            mock.patch.object(ChangeSet, "preprocess_ref_views", lambda self: None), \
            mock.patch.object(type(G), "LOW_MEMORY_MODE", False), \
            mock.patch.object(type(G), "VERY_LOW_MEMORY_MODE", False):
        pipeline.file_processing.file_processing_worker(
            _FakeConn([(0, files)]), 2, gp_ref, mf or types.SimpleNamespace(get_file=lambda p, v: "text"), 0
        )
    return captured["dict"], captured["errors"], calls


class TestWorkerBody(unittest.TestCase):
    def test_stage_two_does_not_prune_dependencies(self) -> None:
        _, _, calls = _run_worker(["A\tdocs/a.txt"], shared_symbols=object())
        self.assertEqual(calls, {"prune": 0, "resolve": 1})

    def test_stage_one_prunes_dependencies(self) -> None:
        _, _, calls = _run_worker(["A\tdocs/a.txt"])
        self.assertEqual(calls, {"prune": 1, "resolve": 0})

    def test_result_changeset_carries_no_prior_tag_data(self) -> None:
        done, errors, _ = _run_worker(["A\tdocs/a.txt"])
        self.assertEqual(errors, [])
        state = done["docs/a.txt"].__dict__
        for name in (
            "_batch_prior_tags", "_batch_prior_items", "prior_tags_by_name", "prior_tags_by_pos",
            "prior_unmatched_symbols", "prior_tags_max_span", "_prior_tags_by_pos_sorted",
        ):
            self.assertNotIn(name, state)

    def test_clear_bloat_pickle_does_not_grow_with_prior_tag_data(self) -> None:
        def pickled(rows: int) -> int:
            cs = ChangeSet("M\tdocs/a.txt")
            cs._batch_prior_tags = {1: [(i,) * 14 for i in range(rows)]}
            cs._batch_prior_items = {i: ({}, {}) for i in range(rows)}
            cs.prior_tags_by_name = {str(i): [(i, i, i, i)] for i in range(rows)}
            cs.prior_tags_by_pos = [(i, i, i, i, "n", 1) for i in range(rows)]
            cs.prior_unmatched_symbols = [(i, i, "n", 1, i, i) for i in range(rows)]
            cs.prior_tags_max_span = 3
            cs._prior_tags_by_pos_sorted = True
            cs.clear_bloat()
            for name in ("_batch_prior_tags", "_batch_prior_items", "prior_tags_by_name", "prior_tags_by_pos",
                         "prior_unmatched_symbols", "prior_tags_max_span", "_prior_tags_by_pos_sorted"):
                self.assertNotIn(name, cs.__dict__)
            return len(pickle.dumps(cs))

        self.assertEqual(pickled(10), pickled(5000))

    def test_unreadable_file_is_a_per_file_error_and_stays_in_the_tree(self) -> None:
        def unreadable(cs):
            cs.store(m_ast.set(None, "half-staged", 3))  # staged by the parser before it failed
            raise FILE_ERROR(OSError("cannot read docs/a.txt"))

        done, errors, _ = _run_worker(["A\tdocs/a.txt"], parse=unreadable)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], "A\tdocs/a.txt")
        self.assertIn("Unreadable source file", errors[0][1])
        cs = done["docs/a.txt"]
        self.assertTrue(cs.cs, "the file name / file / bridge operations staged before parsing are kept")
        self.assertEqual(len(cs.cs), 3)
        # the error is counted by the systemic-failure check, which looks at keys holding a tab
        self.assertIn("\t", errors[0][0])


class TestPriorTagBatchQuery(unittest.TestCase):
    def test_batched_rows_match_per_blob_rows(self) -> None:
        from core.DBLayout import TABLES, m_ast as ast_t, m_bridge_tag, m_tag
        from parser.c_ast.tag_tracker import query_prior_tags, query_prior_tags_many

        db = make_db("sqlite", rows={
            "m_ast": [{"ast_id": 1, "name": "a", "type_id": 3}, {"ast_id": 2, "name": "b", "type_id": 4}],
            "m_tag": [
                {"tag_id": 1, "hash": b"\x01" * 32, "ast_id": 1, "hl_s": 0, "hl_l": 2},
                {"tag_id": 2, "hash": b"\x02" * 32, "ast_id": 2, "hl_s": 1, "hl_l": 3},
                {"tag_id": 3, "hash": b"\x03" * 32, "ast_id": 1, "hl_s": 4, "hl_l": 1},
            ],
            "m_bridge_tag": [
                {"blob_id": 10, "tag_id": 1, "line_s": 1, "line_e": 2, "char_s": 0, "char_e": 5},
                {"blob_id": 10, "tag_id": 2, "line_s": 3, "line_e": 4, "char_s": 0, "char_e": 5},
                {"blob_id": 11, "tag_id": 3, "line_s": 1, "line_e": 9, "char_s": 0, "char_e": 5},
            ],
        })
        tables = {t.table_id: t for t in TABLES}

        class _TE:
            def view_get_multiple(self, joins, columns):
                return db.view_select_multiple(tables, joins, columns)

        with mock.patch.object(G, "TE", _TE()), mock.patch("parser.c_ast.tag_tracker.PRIOR_TAG_CHUNK", 2):
            batched = query_prior_tags_many(db, [10, 11, 12])
            for fid in (10, 11, 12):
                self.assertEqual(batched[fid], list(query_prior_tags(fid) or []), fid)
        self.assertEqual(batched[12], [])
        self.assertEqual(len(batched[10]), 2)


class TestSchedulerAndStageFixes(unittest.TestCase):
    """Tests for B-25, B-26, B-27, O-02.2, O-04(b), O-13, O-14, O-15, O-16, A-05."""

    def test_b25_cycle_breaker_picks_source_scc_and_counts_breaks(self) -> None:
        from pipeline.scheduler import _source_scc_candidates, execute_phase2_parallel_waves

        # Cycle: a.h <-> b.h. Downstream chain: c.h -> a.h, d.h -> c.h, e.h -> c.h (c.h has more dependents!)
        remaining = {"a.h", "b.h", "c.h", "d.h", "e.h"}
        deps = {
            "a.h": {"b.h"},
            "b.h": {"a.h"},
            "c.h": {"a.h"},
            "d.h": {"c.h"},
            "e.h": {"c.h"},
        }
        candidates = _source_scc_candidates(remaining, deps)
        self.assertEqual(set(candidates), {"a.h", "b.h"})

        # Full execute_phase2_parallel_waves check: c.h must NOT be stubbed; only a.h or b.h is stubbed
        class _FakeCS:
            def __init__(self, path: str, f_deps: set[str]):
                self.path = path
                self.foreign_deps = f_deps
                self.cs_processed = False
                self.unresolved_indices = [0]
                self.blocked_on = None
                self.gp = None
                self.forced = False
                self.cs = []

            def execute(self, force_stubs: bool = False) -> bool:
                if force_stubs:
                    self.forced = True
                    self.cs_processed = True
                    return True
                # Succeeds once all foreign_deps are processed
                if all(gp.ChangeSet_Dict[d].cs_processed for d in self.foreign_deps):
                    self.cs_processed = True
                    return True
                return False

        class _FakeGP:
            def __init__(self):
                self.file_deps = {k: set(v) for k, v in deps.items()}
                self.ChangeSet_Dict = {k: _FakeCS(k, v) for k, v in deps.items()}
                self.cs_op_counts = {k: 1 for k in deps}
                self.completed_paths = set()

        gp = _FakeGP()
        fake_te = mock.MagicMock()
        with mock.patch("pipeline.scheduler.G.TE", fake_te), \
                mock.patch("pipeline.scheduler.extract_tags_and_evacuate_cs"), \
                mock.patch("pipeline.scheduler.reclaim_system_memory"), \
                self.assertLogs(main.logger, "INFO") as logs:
            execute_phase2_parallel_waves(gp, completed_set=set())

        forced_files = [k for k, cs in gp.ChangeSet_Dict.items() if cs.forced]
        self.assertEqual(len(forced_files), 1)
        self.assertIn(forced_files[0], {"a.h", "b.h"})
        self.assertFalse(gp.ChangeSet_Dict["c.h"].forced)
        self.assertTrue(any("1 cycle breaks" in msg for msg in logs.output))

    def test_o02_2_maybe_chunk_commit_triggers_on_staged_rows_cap(self) -> None:
        from pipeline.scheduler import DependencyScheduler, _CHUNK_COMMIT_ROW_CAP

        gp = mock.MagicMock()
        sched = DependencyScheduler(gp, chunk_commit_interval=1000)
        sched.executed_count = 1  # Not on count interval

        fake_te = mock.MagicMock()
        fake_te.staged_rows = _CHUNK_COMMIT_ROW_CAP
        fake_te.async_commits = True
        fake_te.pending_commit_rows = 100
        with mock.patch("pipeline.scheduler.G.TE", fake_te), \
                mock.patch("pipeline.scheduler._commit_all_with_retry") as commit_mock, \
                mock.patch("pipeline.scheduler.reclaim_system_memory"):
            sched._maybe_chunk_commit()
        commit_mock.assert_called_once_with("Chunk")

    def test_o13_o14_async_commits_through_resolve_headers_and_publish_clears_resolved(self) -> None:
        from pipeline.scheduler import DependencyScheduler

        gp = mock.MagicMock()
        gp.ChangeSet_Dict = {}
        gp.file_deps = {}
        sched = DependencyScheduler(gp)

        fake_te = mock.MagicMock()
        fake_te.async_commits = False
        with mock.patch("pipeline.scheduler.G.TE", fake_te), \
                mock.patch("pipeline.scheduler.execute_phase2_parallel_waves"):
            sched.resolve_headers()
        self.assertTrue(fake_te.async_commits)

        # O-14: _publish_file_symbols clears resolved_symbols and resolved_names on published paths
        cs_a = mock.MagicMock()
        cs_a.resolved_symbols = {("foo", 1): 10}
        cs_a.resolved_names = {"foo": 10}
        fake_gp = mock.MagicMock()
        fake_gp.file_symbols = {"a.h": {("foo", 1): 10}}
        fake_gp._ast_staged_symbols = {}
        fake_gp._ast_staged_names = {}
        fake_gp.file_names = {}
        fake_gp.unchanged_symbol_cache = {}
        fake_gp.ChangeSet_Dict = {"a.h": cs_a}
        fake_gp._shared_symbols = None

        fake_seg = mock.MagicMock()
        fake_seg.sym_count = 1
        fake_seg.nbytes = 0
        with mock.patch("pipeline.worker_stage.gp", fake_gp), \
                mock.patch("pipeline.worker_stage.SharedSymbolBuilder") as builder_cls, \
                mock.patch("pipeline.worker_stage.reclaim_system_memory"):
            builder_cls.return_value.build_file_scoped.return_value = fake_seg
            pipeline.worker_stage._publish_file_symbols()

        self.assertIsNone(cs_a.resolved_symbols)
        self.assertIsNone(cs_a.resolved_names)

    def test_b26_ingest_failure_records_per_file_tabbed_errors(self) -> None:
        errors: list = []

        def _ingest(batch: dict) -> None:
            raise RuntimeError("boom")

        batches = [(0, ["M\ta.c", "M\tb.c"])]
        with mock.patch.object(G, "DB", MockDB):
            pipeline.worker_stage._run_worker_stage("[test]", batches, _ingest, 1, errors, worker_target=_crashing_worker)
        tabbed = [e for e in errors if "\t" in e[0]]
        self.assertEqual([e[0] for e in tabbed], ["M\ta.c", "M\tb.c"])

    def test_b27_retry_round_splits_multi_file_unfinished_batch(self) -> None:
        ingested: dict[str, int] = {}
        errors: list = []
        # Batch 0 crashes in round 1; batch 1 is unfinished in round 1, then crashes on "crash.c" in round 2!
        # Round 2 must split batch 1 into single-file batches so "ok.c" is NOT marked lost.
        batches = [(0, ["crash.c"]), (1, ["ok.c", "crash.c"])]
        with mock.patch.object(G, "DB", MockDB):
            result = pipeline.worker_stage._run_worker_stage(
                "[test]", batches, ingested.update, 1, errors, worker_target=_crashing_worker
            )
        self.assertIn("ok.c", ingested)
        self.assertEqual(result.lost_files, ["crash.c", "crash.c"])

    def test_o16_format_time_split_includes_worker_subphases(self) -> None:
        text = pipeline.worker_stage._format_time_split({
            "workers": 2.0,
            "worker_busy_s": 8.0,
            "worker_task_wait_s": 1.0,
            "worker_send_wait_s": 1.0,
            "worker_gc_s": 0.0,
            "worker_tail_idle_s": 0.0,
            "worker_prune_s": 0.5,
            "worker_ref_views_s": 1.2,
            "worker_pre_resolve_s": 2.3,
            "worker_encode_s": 0.4,
            "worker_db_reads": 5.0,
            "worker_db_read_s": 0.1,
        })
        self.assertIn("ref views 1.2s", text)
        self.assertIn("pre-resolve 2.3s", text)
        self.assertIn("encode 0.4s", text)

    def test_o15_partition_source_epochs_cuts_by_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            files = []
            # 10 files of 100KB each (total 1MB); target_epoch_bytes=300KB -> multiple epochs
            for i in range(10):
                p = os.path.join(d, f"dir{i // 2}", f"f{i}.c")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(b"x" * 100_000)
                files.append(f"M\tdir{i // 2}/f{i}.c")
            epochs = pipeline.source_order.partition_source_epochs(
                files,
                working_dir=d,
                normal_batch_size=1,
                heavy_batch_size=1,
                target_epoch_files=100,
                target_epoch_bytes=300_000,
            )
        self.assertGreater(len(epochs), 1)
        self.assertEqual(sum(len(b[1]) for ep in epochs for b in ep), 10)

    def test_o04b_has_peer_nodes_checks_m_parse_node(self) -> None:
        from contextlib import contextmanager
        import pipeline.update

        db_no_peers = make_db("sqlite", rows={"m_parse_node": [{"node_id": "self-1", "network_id": 1, "role": "master", "hostname": "h", "pid": 1, "active_commit_id": 0, "heartbeat_at": 1}]})
        db_with_peer = make_db("sqlite", rows={"m_parse_node": [
            {"node_id": "self-1", "network_id": 1, "role": "master", "hostname": "h", "pid": 1, "active_commit_id": 0, "heartbeat_at": 1},
            {"node_id": "peer-2", "network_id": 1, "role": "worker", "hostname": "h", "pid": 2, "active_commit_id": 0, "heartbeat_at": 1},
        ]})

        @contextmanager
        def _ctx(db):
            yield db

        with mock.patch.object(G, "node_id", "self-1", create=True), \
                mock.patch.object(pipeline.update.gp, "network_id", 1, create=True):
            with mock.patch.object(G, "DB", lambda: _ctx(db_no_peers)):
                self.assertFalse(pipeline.update._has_peer_nodes())
            with mock.patch.object(G, "DB", lambda: _ctx(db_with_peer)):
                self.assertTrue(pipeline.update._has_peer_nodes())

    def test_a05_git_network_uses_reserve_id_block_when_distributed(self) -> None:
        from core.DBLayout import m_project
        import pipeline.git_network

        fake_db = mock.MagicMock()
        fake_db.reserve_id_block.return_value = (42, 42)
        with mock.patch.object(G.TE, "distributed", True, create=True):
            nid = pipeline.git_network._next_id(fake_db, m_project)
        self.assertEqual(nid, 42)
        fake_db.reserve_id_block.assert_called_once_with(m_project, 1)
        fake_db.get_next_id.assert_not_called()
