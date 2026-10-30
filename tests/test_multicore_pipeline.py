"""tests/test_multicore_pipeline.py - Regression tests for main.trigger_multicore and its worker pool.

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

import main
from core.DBLayout import m_ast
from core.globalstuff import G, REF_FILE
from core.TableHandling import ChangeSet
from db_engine import MockDB
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
        task = main._recv_task(conn)
        if task is None:
            break
        batch_id, files = task
        if any("crash" in f for f in files):
            os.kill(os.getpid(), signal.SIGKILL)
        conn.send_bytes(main._encode_batch_result(batch_id, {f: len(files) for f in files}, [], compress=False))
    conn.send_bytes(main._FRAME_DONE)
    conn.close()


def _unstartable_worker(conn, vid, gp_ref, mf_ref, worker_id=0) -> None:
    """Fake worker that cannot reach the database."""
    conn.send_bytes(main._FRAME_INIT_FAILED + pickle.dumps((f"Worker {worker_id}", "DB connection failed: refused", "")))
    conn.close()


class TestWorkerChangeSelection(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (main.gp.worker_change_list, main.gp.Change_List)

    def tearDown(self) -> None:
        main.gp.worker_change_list, main.gp.Change_List = self._saved

    def test_fully_deduplicated_version_parses_nothing(self) -> None:
        main.gp.Change_List = ["M\tinclude/a.h", "M\tkernel/b.c"]
        main.gp.worker_change_list = []
        self.assertEqual(main._select_worker_changes(), [])

    def test_without_dedup_uses_change_list(self) -> None:
        main.gp.Change_List = ["M\tkernel/b.c"]
        main.gp.worker_change_list = None
        self.assertEqual(main._select_worker_changes(), ["M\tkernel/b.c"])


class TestMemoryProfile(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (G.MEMORY_MODE, G.CPUS)
        G.CPUS = 16

    def tearDown(self) -> None:
        G.MEMORY_MODE, G.CPUS = self._saved

    def test_budgets_per_mode(self) -> None:
        G.MEMORY_MODE = "very_low"
        self.assertEqual(
            main._memory_profile(),
            {"workers": 2, "header_batch": 25, "normal_batch": 25, "heavy_batch": 10, "max_batch_bytes": 350_000, "epoch_files": 2000},
        )
        G.MEMORY_MODE = "low"
        self.assertEqual(
            main._memory_profile(),
            {"workers": 8, "header_batch": 50, "normal_batch": 50, "heavy_batch": 25, "max_batch_bytes": 750_000, "epoch_files": 4500},
        )
        G.MEMORY_MODE = "normal"
        self.assertEqual(
            main._memory_profile(),
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
            main.extract_tags_and_evacuate_cs(cs)
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
        gp = main.GreatProcessor()
        for release in (gp.init_cs_dict, gp.reset_cs):
            chain = self._chain({"a.h": {("foo", 1): 1}})
            gp._shared_symbols = chain
            release()
            self.assertIsNone(gp._shared_symbols)
            self.assertEqual(chain.segments, [])

    def test_finalize_releases_segments(self) -> None:
        gp = main.GreatProcessor()
        gp._shared_symbols = self._chain({"a.h": {("foo", 1): 1}})
        old_te = G.TE
        G.TE = _RecordingTE()
        try:
            main.DependencyScheduler(gp).finalize()
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
        result = main._run_worker_stage(
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
        result = main._run_worker_stage("[test]", batches, ingested.update, 1, errors, worker_target=_crashing_worker)
        self.assertEqual(result.lost_files, ["crash.c"])
        self.assertEqual(ingested, {"a.c": 2, "b.c": 2, "c.c": 1, "d.c": 1, "e.c": 2, "f.c": 2, "g.c": 2, "h.c": 2})

    def test_stage_aborts_when_no_worker_can_start(self) -> None:
        errors: list = []
        with self.assertRaises(RuntimeError):
            main._run_worker_stage("[test]", [(0, ["a.c"]), (1, ["b.c"])], lambda batch: None, 2, errors,
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
        result = main._run_worker_stage("[test]", batches, _ingest, 2, errors, worker_target=_crashing_worker)
        self.assertEqual(set(ingested), {"a.c", "c.c"})
        self.assertEqual(result.failed_workers, 0)
        self.assertTrue(any("commit failed" in e[1] for e in errors))

    def test_on_started_runs_with_db_connection(self) -> None:
        seen: list = []
        main._run_worker_stage(
            "[test]", [(0, ["a.c"])], lambda batch: None, 1, [],
            on_started=lambda: seen.append(G.TE.db), worker_target=_crashing_worker,
        )
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])


class TestTriggerMulticore(unittest.TestCase):
    """Drive both stages of trigger_multicore with the fake worker and no scheduler."""

    def setUp(self) -> None:
        self.saved = {
            name: getattr(main, name)
            for name in ("file_processing_worker", "processing_dirs", "processing_unchanges", "_register_file_names")
        }
        self.saved_gp = (main.gp.worker_change_list, main.gp.Change_List, main.gp.Version_Name)
        self.old_db, self.old_te = G.DB, G.TE
        G.DB = MockDB
        G.TE = _RecordingTE()
        self.registered: list[list[str]] = []
        main.file_processing_worker = _crashing_worker
        main.processing_dirs = lambda: {}
        main.processing_unchanges = lambda: ["A\tdrivers/missing.c"]
        main._register_file_names = lambda items: self.registered.append(list(items))
        main.gp.Version_Name = "test-version-not-checked-out"
        main.gp.init_cs_dict()
        signal.signal(signal.SIGALRM, TestWorkerStage._timeout)
        signal.alarm(120)

    def tearDown(self) -> None:
        signal.alarm(0)
        for name, value in self.saved.items():
            setattr(main, name, value)
        main.gp.worker_change_list, main.gp.Change_List, main.gp.Version_Name = self.saved_gp
        G.DB, G.TE = self.old_db, self.old_te
        main.gp.init_cs_dict()

    def test_headers_missing_files_and_sources_are_all_ingested(self) -> None:
        main.gp.Change_List = ["M\tinclude/linux/a.h", "M\tkernel/b.c", "M\tDocumentation/c.txt"]
        main.gp.worker_change_list = None
        frozen_before = gc.get_freeze_count()
        main.trigger_multicore()
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
        main._start_git_commit_worker()
        result_dir = main.gp._commit_result_dir
        self.assertEqual(main._collect_git_commit_result(), (["c1", "c2"], {"kernel/fork.c": [1, 2]}))
        self.assertIsNone(main.gp._commit_proc)
        self.assertFalse(os.path.exists(result_dir))
        self.assertNotIn(result_dir, main.gp.PURGE_LIST)

    def test_failed_worker_falls_back(self) -> None:
        self._install_parser(RuntimeError("git log failed"))
        main._start_git_commit_worker()
        self.assertIsNone(main._collect_git_commit_result())


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
            epochs = main.partition_source_epochs(
                items, working_dir=tmpdir, normal_batch_size=1, heavy_batch_size=1, max_batch_bytes=10**9
            )
            sizes = [os.path.getsize(os.path.join(tmpdir, b[1][0].split("\t")[-1])) for b in epochs[0]]
            self.assertEqual(sizes, sorted(sizes, reverse=True))
            in_order = main.partition_source_epochs(
                items, working_dir=tmpdir, normal_batch_size=1, heavy_batch_size=1, max_batch_bytes=10**9, largest_first=False
            )
            self.assertEqual([b[0] for b in in_order[0]], [0, 1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
