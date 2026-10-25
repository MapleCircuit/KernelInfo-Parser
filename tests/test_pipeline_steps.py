"""tests/test_pipeline_steps.py - Regression tests for the update() pipeline steps around trigger_multicore.

Covers idempotent inserts, blob hash guarding, batched commit DAG ingestion (Step 1), blob reuse through the
TableEngine caches (Step 4.5), event-driven ChangeSet wakeups in DependencyScheduler, compressed-store state
checks used by the wave pass, the unused previous-version lookup removed from default_processing, the hunk
interval index used to link tags to commits, the indexed MAINTAINERS matcher, the per-version tree listing
cache, hash tables carried across TableEngine restarts, the commit_id preload window, commit eviction that keeps
shared views, balanced batch sizes and quiet symbol table allocation.
"""
import os
import random
import shutil
import subprocess
import tempfile
import unittest

import main
from core.DBLayout import (
    m_ast,
    m_ast_container,
    m_ast_hash,
    m_bridge_checkpoint,
    m_bridge_commit_person,
    m_bridge_file,
    m_bridge_tag,
    m_commit,
    m_commit_file,
    m_commit_parent,
    m_file_blob,
    m_maintainer_person,
    m_blob_symbol_def,
)
from core.FileHandler import MasterFile
from core.GreatProcessor import CompressedChangeSetDict, GreatProcessor
from core.TableHandling import ChangeSet
from core.globalstuff import ASTT, G, OP_VIEW_DONE, OP_VIEW_SET, REF_FILE, type_check
from db_engine import MockDB
from db_engine.DBHandling import MariaDB
from parser.git_ast.git_commit_parser import _HunkIndex
from parser.git_ast.git_types import CommitRole, GitCommit, GitContributor
from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
from parser.maintainer_ast.maintainer_parser import MaintainerParser
from table_engine import TECachedDB
from table_engine.shared_buffer import SharedSymbolBuilder, SharedTablePKView
from tests.test_maintainer_ast import SAMPLE_MAINTAINERS


def _reset_mock_db() -> None:
    MockDB._global_store.clear()
    MockDB._global_next_id.clear()


class _TableEngineTestCase(unittest.TestCase):
    """Runs each test with a fresh MockDB-backed TableEngine and restores the module state afterwards."""

    def setUp(self) -> None:
        self.saved = (G.DB, G.TE, main.gp.VID, main.gp.Old_VID, main.gp.Change_List, main.gp.file_blob_ids)
        _reset_mock_db()
        G.DB = MockDB
        G.TE = TECachedDB()
        G.TE.start(main.gp.Table_Array, MockDB)
        main.gp.file_blob_ids = {}
        main.file_fid_cache.clear()

    def tearDown(self) -> None:
        G.DB, G.TE, main.gp.VID, main.gp.Old_VID, main.gp.Change_List, main.gp.file_blob_ids = self.saved
        main.gp.deduped_paths = set()
        main.file_fid_cache.clear()
        _reset_mock_db()


class TestInsertSemantics(unittest.TestCase):
    def test_idempotent_tables_skip_only_existing_rows(self) -> None:
        sql = MariaDB._insert_sql(m_commit_file)
        self.assertTrue(sql.startswith("INSERT INTO `m_commit_file` VALUES"))
        self.assertTrue(sql.endswith("ON DUPLICATE KEY UPDATE `commit_id` = `commit_id`"))
        self.assertNotIn("ON DUPLICATE", MariaDB._insert_sql(m_commit))

    def test_symbol_definitions_are_not_cached(self) -> None:
        self.assertFalse(m_blob_symbol_def.te_cached)


class TestBlobHashGuard(unittest.TestCase):
    def test_files_needing_a_hash_without_one_are_refused(self) -> None:
        hashes = {"kernel/fork.c": b"\x01" * 20}
        cs = ChangeSet("M\tkernel/fork.c")
        self.assertTrue(main._attach_blob_hash(cs, hashes))
        self.assertEqual(cs.blob_hash, b"\x01" * 20)
        self.assertFalse(main._attach_blob_hash(ChangeSet("M\tlib/submodule"), hashes))
        self.assertTrue(main._attach_blob_hash(ChangeSet("D\tlib/gone.c"), hashes))
        self.assertTrue(main._attach_blob_hash(ChangeSet("R100\told.c\tnew.c"), hashes))
        self.assertTrue(main._attach_blob_hash(ChangeSet("M\tlib/any.c"), {}))


def _commit(c_hash: str, name: str, email: str, parents: list[str], contributors: list[tuple[str, str]] = ()) -> GitCommit:
    return GitCommit(
        commit_hash=c_hash,
        author_name=name,
        author_email=email,
        author_date=1,
        committer_name=name,
        committer_email=email,
        committer_date=2,
        subject=f"subject {c_hash[:4]}",
        message="body",
        contributors=[GitContributor(n, e, CommitRole.SIGNED_OFF_BY, i) for i, (n, e) in enumerate(contributors)],
        parents=parents,
    )


class TestCommitRangeIngestion(unittest.TestCase):
    def setUp(self) -> None:
        _reset_mock_db()

    def tearDown(self) -> None:
        _reset_mock_db()

    def test_range_is_ingested_in_bulk_with_parents_and_people(self) -> None:
        c1, c2, c3, outside = ("1" * 40), ("2" * 40), ("3" * 40), ("9" * 40)
        commits = [
            _commit(c3, "A. Dev", "a@x", [c2, outside], [("A. Dev", "a@x"), ("A. Dev", "a@x")]),
            _commit(c2, "B Dev", "b@x", [c1]),
            _commit(c1, "A Dev", "a@x", [], [("A Dev", "a@x")]),
        ]
        with MockDB() as db:
            db.insert(m_commit, (100, 1, bytes.fromhex(outside), 1, 0, 1, 0, "outside", ""))
            ids = main._ingest_commit_range(db, commits, network_id=1)
            self.assertEqual(ids[outside], 100)
            self.assertLess(ids[c1], ids[c2])
            self.assertLess(ids[c2], ids[c3])

            parents = set(MockDB._global_store[m_commit_parent.table_name])
            self.assertEqual(parents, {(ids[c2], 0), (ids[c3], 0), (ids[c3], 1)})
            people = {(row[1], row[2]) for row in MockDB._global_store[m_maintainer_person.table_name].values()}
            self.assertEqual(people, {("A Dev", "a@x"), ("B Dev", "b@x"), ("A. Dev", "a@x")})
            self.assertEqual(len(MockDB._global_store[m_bridge_commit_person.table_name]), 2)

            # Re-ingesting the same range adds nothing and returns the same ids
            rows_before = {name: len(rows) for name, rows in MockDB._global_store.items()}
            self.assertEqual(main._ingest_commit_range(db, commits, network_id=1), ids)
            self.assertEqual({name: len(rows) for name, rows in MockDB._global_store.items()}, rows_before)
            self.assertEqual(main._lookup_commit_ids(db, 1, [c1, "f" * 40]), {c1: ids[c1]})

    def test_person_identity_is_name_and_email(self) -> None:
        self.assertEqual(main._person_key("A", "a@x"), ("A", "a@x"))
        self.assertEqual(main._person_key("", "a@x"), ("a@x", "a@x"))
        self.assertEqual(main._person_key(None, None), ("Unknown", ""))


class TestReuseParsedBlobs(_TableEngineTestCase):
    def test_already_parsed_blobs_are_linked_instead_of_parsed(self) -> None:
        known = b"\x07" * 20
        G.TE.set(m_file_blob.table_id, (7, known, type_check("drivers/a.c")))
        G.TE.set(m_bridge_tag.table_id, (7, 70, 1, 3, 0, 0))
        G.TE.commit_all()
        main.gp.VID = 5
        main.gp.Change_List = ["M\tdrivers/a.c", "A\tdrivers/b.c", "D\tdrivers/c.c"]
        hashes = {"drivers/a.c": known, "drivers/b.c": b"\x08" * 20}

        remaining = main._reuse_parsed_blobs(hashes)

        self.assertEqual(remaining, ["A\tdrivers/b.c", "D\tdrivers/c.c"])
        self.assertEqual(main.gp.deduped_paths, {"drivers/a.c"})
        self.assertEqual(main.gp.file_blob_ids["drivers/a.c"], 7)
        fnid = G.TE.get(main.m_file_name.table_id, (None, "drivers/a.c"))[0]
        self.assertIsNotNone(G.TE.get(m_bridge_checkpoint.table_id, (5, fnid, None)))
        self.assertIn((5, fnid), G.TE.queued_set[m_commit_file.table_id])
        self.assertEqual(main._load_blob_tags([7]), {7: [(70, 7, 1, 3)]})

    def test_untagged_blobs_are_parsed_again(self) -> None:
        known = b"\x07" * 20
        G.TE.set(m_file_blob.table_id, (7, known, type_check("drivers/a.c")))
        G.TE.commit_all()
        main.gp.VID = 5
        main.gp.Change_List = ["M\tdrivers/a.c"]
        self.assertEqual(main._reuse_parsed_blobs({"drivers/a.c": known}), ["M\tdrivers/a.c"])
        self.assertEqual(main.gp.deduped_paths, set())


def _header(path: str) -> ChangeSet:
    cs = ChangeSet(f"A\t{path}")
    struct_name = f"struct_{os.path.basename(path).split('.')[0]}"
    cs.store(m_ast.view(((m_ast.ast_id,),), None, struct_name, int(ASTT.C_struct)))
    cs.symbol_dict[(struct_name, int(ASTT.C_struct))] = 0
    return cs


def _consumer(path: str, target: str) -> ChangeSet:
    cs = ChangeSet(f"A\t{path}")
    target_struct = f"struct_{os.path.basename(target).split('.')[0]}"
    ref = cs.ref(m_ast.ast_id, REF_FILE, target, target_struct, int(ASTT.C_struct))
    cs.store(m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, 1),), None, f"use_{path}", int(ASTT.C_TypeRef), None, 0, int(ASTT.C_TypeRef), ref))
    cs.foreign_deps.add(target)
    return cs


class TestSchedulerWakeups(_TableEngineTestCase):
    def _scheduler(self, changed: set[str]) -> tuple[GreatProcessor, main.DependencyScheduler]:
        gp = GreatProcessor()
        gp.init_cs_dict()
        gp._changed_paths_set = set(changed)
        return gp, main.DependencyScheduler(gp)

    def test_waiting_header_runs_as_soon_as_its_dependency_completes(self) -> None:
        gp, scheduler = self._scheduler({"include/a.h", "include/b.h"})
        scheduler.ingest_batch({"include/a.h": _consumer("include/a.h", "include/b.h")}, is_header_stage=True)
        self.assertNotIn("include/a.h", scheduler.completed)
        self.assertEqual(scheduler.pending_deps, {"include/a.h": {"include/b.h"}})

        scheduler.ingest_batch({"include/b.h": _header("include/b.h")}, is_header_stage=True)
        self.assertEqual(scheduler.completed, {"include/a.h", "include/b.h"})
        self.assertEqual(scheduler.pending_deps, {})

    def test_blocked_source_wakes_without_waiting_for_an_epoch_boundary(self) -> None:
        gp, scheduler = self._scheduler({"kernel/c.c", "include/d.h"})
        scheduler.ingest_batch({"kernel/c.c": _consumer("kernel/c.c", "include/d.h")})
        self.assertNotIn("kernel/c.c", scheduler.completed)
        self.assertIn("kernel/c.c", scheduler.waiters["include/d.h"])
        self.assertEqual(list(scheduler.deferred_queue), [])

        scheduler.ingest_batch({"include/d.h": _header("include/d.h")})
        self.assertEqual(scheduler.completed, {"kernel/c.c", "include/d.h"})
        self.assertEqual(scheduler.executed_count, 2)


class TestStoredChangesetState(unittest.TestCase):
    def test_state_is_read_without_decompressing(self) -> None:
        store = CompressedChangeSetDict(lru_cache_size=1)
        done = ChangeSet("A\tdone.c")
        done.cs_processed = True
        done.store_dict.clear()  # as extract_tags_and_evacuate_cs() leaves an executed ChangeSet
        store["done.c"] = done
        store["pending_old.c"] = ChangeSet("A\tpending_old.c")
        store["pending_new.c"] = ChangeSet("A\tpending_new.c")  # evicts pending_old.c into the compressed store

        def _no_decompress(*args, **kwargs):
            raise AssertionError("decompressed")

        store.get = _no_decompress
        store.__class__ = type("NoDecompress", (CompressedChangeSetDict,), {"__getitem__": _no_decompress})
        self.assertIs(main._stored_changeset_executed(store, "done.c"), True)
        self.assertIs(main._stored_changeset_executed(store, "pending_old.c"), False)
        self.assertIs(main._stored_changeset_executed(store, "pending_new.c"), False)
        self.assertIsNone(main._stored_changeset_executed(store, "absent.c"))


class TestDefaultProcessing(_TableEngineTestCase):
    def test_modified_file_no_longer_looks_up_its_previous_version_row(self) -> None:
        main.gp.VID, main.gp.Old_VID = 2, 1
        cs = ChangeSet("M\tkernel/fork.c")
        cs.blob_hash = b"\x02" * 20
        main.default_processing(cs)
        bridge_views = [
            op for op in cs.cs
            if op[1] in (OP_VIEW_SET, OP_VIEW_DONE) and isinstance(op[0], tuple) and op[0][0][0][0] == m_bridge_file.table_id
        ]
        self.assertEqual(bridge_views, [])
        self.assertTrue(any(op[0] == m_bridge_file.table_id for op in cs.cs))


class TestHunkIndex(unittest.TestCase):
    def test_matches_a_linear_scan(self) -> None:
        rnd = random.Random(7)
        for _ in range(50):
            hunks = []
            for cid in range(rnd.randint(1, 60)):
                start = rnd.randint(1, 500)
                hunks.append((start, start + rnd.randint(0, 40), cid))
            index = _HunkIndex(hunks)
            ordered = sorted(hunks, key=lambda h: h[0])
            for _ in range(40):
                line_s = rnd.randint(1, 560)
                line_e = line_s + rnd.randint(0, 80)
                expected = [cid for start, end, cid in ordered if start <= line_e and end >= line_s]
                self.assertEqual(index.overlapping(line_s, line_e), expected)


class TestMaintainerIndex(unittest.TestCase):
    def test_indexed_matching_equals_testing_every_section(self) -> None:
        matcher = MaintainerMatcher(MaintainerParser(SAMPLE_MAINTAINERS).parse())
        paths = [
            "drivers/net/3c505.c", "drivers/net/3c505.h", "drivers/net/e1000.c", "fs/ext4/super.c",
            "fs/ext4/crypto/keyinfo.c", "Documentation/filesystems/ext4.txt", "net/core/dev.c",
            "net/ipv6/ip6_output.c", "MAINTAINERS", "README", "include/linux/sched.h",
        ]
        for path in paths:
            expected = []
            for rule in matcher.compiled_rules:
                if rule.section.name not in {s.name for s in expected} and rule.matches(path):
                    expected.append(rule.section)
            self.assertEqual({s.name for s in matcher.match_file(path)}, {s.name for s in expected}, path)


class TestFileListCache(unittest.TestCase):
    def test_listing_is_cached_per_version_until_cleared(self) -> None:
        repo = tempfile.mkdtemp()
        try:
            subprocess.run(["git", "init", "-q", repo], check=True)
            with open(os.path.join(repo, "a.c"), "w") as f:
                f.write("int a;\n")
            subprocess.run(["git", "-C", repo, "add", "a.c"], check=True)
            subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a"], check=True)
            mf = MasterFile()
            mf.set_repo_dir(repo)
            self.assertEqual(mf.git_file_list("HEAD"), "a.c\n")
            shutil.rmtree(os.path.join(repo, ".git"))
            self.assertEqual(mf.git_file_list("HEAD"), "a.c\n")
            mf.clear_version_cache("HEAD")
            self.assertEqual(mf.git_file_list("HEAD"), "")
        finally:
            shutil.rmtree(repo, ignore_errors=True)


class _CountingDB(MockDB):
    """MockDB recording which tables the TableEngine preloads."""

    preloaded: list[str] = []
    fail_inserts = False

    def select_preload(self, table, cached_columns=None, min_vid=None):
        _CountingDB.preloaded.append(table.table_name)
        return super().select_preload(table, cached_columns=cached_columns, min_vid=min_vid)

    def insert(self, table, data):
        if _CountingDB.fail_inserts:
            raise RuntimeError("insert failed")
        return super().insert(table, data)


class TestCarriedHashTables(unittest.TestCase):
    """m_ast_hash / m_tag_code survive TableEngine restarts instead of being reloaded every version."""

    def setUp(self) -> None:
        self.saved = (G.DB, G.TE, main.gp.Old_VID)
        _reset_mock_db()
        _CountingDB.preloaded = []
        _CountingDB.fail_inserts = False
        G.DB = _CountingDB
        main.gp.Old_VID = 0
        self.te = TECachedDB(hugepages="auto")
        G.TE = self.te

    def tearDown(self) -> None:
        _CountingDB.fail_inserts = False
        try:
            self.te.close()
        except Exception:
            pass
        G.DB, G.TE, main.gp.Old_VID = self.saved
        _reset_mock_db()

    def _start(self) -> None:
        _CountingDB.preloaded = []
        self.te.start(main.gp.Table_Array, _CountingDB)

    def _stage_hash(self, n: int) -> bytes:
        h = bytes([n]) * 32
        self.te.set(m_ast_hash.table_id, (h, 1000 + n))
        return h

    def _ast_id(self, h: bytes):
        row = self.te.get(m_ast_hash.table_id, (h, None))
        return row[1] if row else None

    def test_snapshot_is_reused_with_rows_committed_since(self) -> None:
        self._start()                                    # version 1: empty database, nothing to carry
        old = [self._stage_hash(n) for n in range(1, 6)]
        self.te.commit_all()
        self.te.close()

        self._start()                                    # version 2: loads the snapshot once
        self.assertIn("m_ast_hash", _CountingDB.preloaded)
        self.assertEqual([self._ast_id(h) for h in old], [1001, 1002, 1003, 1004, 1005])
        committed = self._stage_hash(10)
        self.te.commit_all()
        uncommitted = self._stage_hash(11)
        self.assertEqual(self._ast_id(uncommitted), 1011)
        self.te.close()

        self._start()                                    # version 3: reuses it
        self.assertNotIn("m_ast_hash", _CountingDB.preloaded)
        self.assertNotIn("m_tag_code", _CountingDB.preloaded)
        self.assertIn("m_file_name", _CountingDB.preloaded)
        self.assertEqual(self._ast_id(old[0]), 1001)
        self.assertEqual(self._ast_id(committed), 1010)
        self.assertIsNone(self._ast_id(uncommitted), "a row that was never committed must not be carried")

    def test_outgrown_snapshot_is_rebuilt_from_memory(self) -> None:
        self._start()
        self._stage_hash(1)
        self.te.commit_all()
        self.te.close()
        self._start()                                    # snapshot: 1 row
        for n in range(2, 5):
            self._stage_hash(n)
        self.te.commit_all()                             # 3 rows since > 0.5 x snapshot
        uncommitted = self._stage_hash(9)
        self.te.close()
        self._start()                                    # snapshot + overlay repacked without a reload
        self.assertNotIn("m_ast_hash", _CountingDB.preloaded)
        self.assertEqual(self.te._carried_reader.tables[m_ast_hash.table_id]["row_count"], 4)
        self.assertEqual(len(self.te._carried_overlays[m_ast_hash.table_id]), 0)
        self.assertEqual([self._ast_id(bytes([n]) * 32) for n in range(1, 5)], [1001, 1002, 1003, 1004])
        self.assertIsNone(self._ast_id(uncommitted), "a row that was never committed must not be repacked")

    def test_failed_commit_drops_the_snapshot(self) -> None:
        self._start()
        self._stage_hash(1)
        self.te.commit_all()
        self.te.close()
        self._start()
        self._stage_hash(2)
        _CountingDB.fail_inserts = True
        with self.assertRaises(RuntimeError):
            self.te.commit_all()
        _CountingDB.fail_inserts = False
        self.te.close()
        self._start()
        self.assertIn("m_ast_hash", _CountingDB.preloaded)


class TestPreloadWindow(unittest.TestCase):
    def setUp(self) -> None:
        _reset_mock_db()

    def tearDown(self) -> None:
        _reset_mock_db()

    def test_checkpoints_are_filtered_by_commit_id_not_blob_id(self) -> None:
        with MockDB() as db:
            db.insert(m_bridge_checkpoint, ((1, 10, 9000), (5, 10, 3), (5, 11, 9001)))
            db.insert(m_blob_symbol_def, ((1, 3, 1, 1, "f", 1, 0, 1, 2),))
            rows = db.select_preload(m_bridge_checkpoint, min_vid=5)
            self.assertEqual(sorted(rows), [(5, 10, 3), (5, 11, 9001)])
            self.assertEqual(len(db.select_preload(m_blob_symbol_def, min_vid=5)), 1)


class TestCommitEviction(_TableEngineTestCase):
    def test_commit_keeps_shared_views_and_only_records_new_keys(self) -> None:
        main.gp.Old_VID = 0
        G.TE.set(m_bridge_checkpoint.table_id, (1, 1, 1))
        G.TE.set(m_bridge_checkpoint.table_id, (1, 2, 2))
        G.TE.commit_all()
        G.TE.close()
        te = TECachedDB(hugepages="auto")
        G.TE = te
        te.start(main.gp.Table_Array, MockDB)
        if not isinstance(te._pk_index[m_bridge_checkpoint.table_id], SharedTablePKView):
            self.skipTest("shared buffer unavailable")
        te.set(m_bridge_checkpoint.table_id, (2, 3, 3))
        te.commit_all()
        self.assertIsInstance(te._pk_index[m_bridge_checkpoint.table_id], SharedTablePKView)
        self.assertEqual(te._committed_pks[m_bridge_checkpoint.table_id], {(2, 3)})
        self.assertIsNotNone(te.get(m_bridge_checkpoint.table_id, (1, 2, None)))


class TestBatchSizing(unittest.TestCase):
    def test_small_stages_give_every_worker_several_batches(self) -> None:
        self.assertEqual(main._balanced_batch_size(164, 7, 200), 6)
        self.assertEqual(main._balanced_batch_size(17000, 7, 200), 200)
        self.assertEqual(main._balanced_batch_size(10, 7, 200), 4)
        self.assertEqual(main._balanced_batch_size(500, 7, 50), 18)


class TestSymbolTableAllocation(unittest.TestCase):
    def test_no_hugetlb_warning_for_symbol_tables(self) -> None:
        with self.assertNoLogs("table_engine.shared_buffer", level="WARNING"):
            reader = SharedSymbolBuilder(mode="auto").build({("f", 1): 1})
        reader.close()


if __name__ == "__main__":
    unittest.main()
