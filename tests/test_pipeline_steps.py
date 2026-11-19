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
import time
import unittest
from unittest import mock

import main
import pipeline.arch_layouts
import pipeline.blob_reuse
import pipeline.changesets
import pipeline.commits
import pipeline.file_processing
import pipeline.file_references
import pipeline.git_ingest
import pipeline.git_network
import pipeline.maintainers
import pipeline.scheduler
import pipeline.worker_stage
from core.DBLayout import (
    m_ast,
    m_file_name,
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
from parser.git_ast.git_commit_parser import _HunkIndex
from parser.git_ast.git_types import CommitRole, GitCommit, GitContributor
from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
from parser.maintainer_ast.maintainer_parser import MaintainerParser
from table_engine import TECachedDB
from table_engine.shared_buffer import SharedSymbolBuilder, SharedTablePKView
from tests.test_maintainer_ast import SAMPLE_MAINTAINERS


class _TableEngineTestCase(unittest.TestCase):
    """Runs each test with a fresh MockDB-backed TableEngine and restores the module state afterwards."""

    def setUp(self) -> None:
        self.saved = (G.DB, G.TE, main.gp.VID, main.gp.Old_VID, main.gp.Change_List, main.gp.file_blob_ids)
        MockDB.reset()
        G.DB = MockDB
        G.TE = TECachedDB()
        G.TE.start(main.gp.Table_Array, MockDB)
        main.gp.file_blob_ids = {}
        pipeline.changesets.file_fid_cache.clear()

    def tearDown(self) -> None:
        G.DB, G.TE, main.gp.VID, main.gp.Old_VID, main.gp.Change_List, main.gp.file_blob_ids = self.saved
        main.gp.deduped_paths = set()
        pipeline.changesets.file_fid_cache.clear()
        MockDB.reset()


class TestInsertSemantics(unittest.TestCase):
    def test_idempotent_tables_skip_only_existing_rows(self) -> None:
        """Re-inserting an identical row of an idempotent table is a no-op; a plain table refuses duplicates."""
        from db_engine import SQLiteDB
        db = SQLiteDB(":memory:", foreign_keys=False)
        db.create_table([m_commit_file, m_commit])
        row = (1, 2, 3, 0, 0, 0)
        db.insert(m_commit_file, (row,))
        db.insert(m_commit_file, (row,))
        self.assertEqual(db.count_rows("m_commit_file"), 1)

    def test_symbol_definitions_are_not_cached(self) -> None:
        self.assertFalse(m_blob_symbol_def.te_cached)


class TestBlobHashGuard(unittest.TestCase):
    def test_files_needing_a_hash_without_one_are_refused(self) -> None:
        hashes = {"kernel/fork.c": b"\x01" * 20}
        cs = ChangeSet("M\tkernel/fork.c")
        self.assertTrue(pipeline.file_processing._attach_blob_hash(cs, hashes))
        self.assertEqual(cs.blob_hash, b"\x01" * 20)
        self.assertFalse(pipeline.file_processing._attach_blob_hash(ChangeSet("M\tlib/submodule"), hashes))
        self.assertTrue(pipeline.file_processing._attach_blob_hash(ChangeSet("D\tlib/gone.c"), hashes))
        self.assertTrue(pipeline.file_processing._attach_blob_hash(ChangeSet("R100\told.c\tnew.c"), hashes))
        self.assertTrue(pipeline.file_processing._attach_blob_hash(ChangeSet("M\tlib/any.c"), {}))


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
        MockDB.reset()

    def tearDown(self) -> None:
        MockDB.reset()

    def test_range_is_ingested_in_bulk_with_parents_and_people(self) -> None:
        c1, c2, c3, outside = ("1" * 40), ("2" * 40), ("3" * 40), ("9" * 40)
        commits = [
            _commit(c3, "A. Dev", "a@x", [c2, outside], [("A. Dev", "a@x"), ("A. Dev", "a@x")]),
            _commit(c2, "B Dev", "b@x", [c1]),
            _commit(c1, "A Dev", "a@x", [], [("A Dev", "a@x")]),
        ]
        with MockDB() as db:
            db.insert(m_commit, (100, 1, bytes.fromhex(outside), 1, 0, 1, 0, "outside", ""))
            ids = pipeline.git_ingest._ingest_commit_range(db, commits, network_id=1)
            self.assertEqual(ids[outside], 100)
            self.assertLess(ids[c1], ids[c2])
            self.assertLess(ids[c2], ids[c3])

            parents = set(MockDB.store[m_commit_parent.table_name])
            self.assertEqual(parents, {(ids[c2], 0), (ids[c3], 0), (ids[c3], 1)})
            people = {(row[1], row[2]) for row in MockDB.store[m_maintainer_person.table_name].values()}
            self.assertEqual(people, {("A Dev", "a@x"), ("B Dev", "b@x"), ("A. Dev", "a@x")})
            self.assertEqual(len(MockDB.store[m_bridge_commit_person.table_name]), 2)

            # Re-ingesting the same range adds nothing and returns the same ids
            rows_before = {name: len(rows) for name, rows in MockDB.store.items()}
            self.assertEqual(pipeline.git_ingest._ingest_commit_range(db, commits, network_id=1), ids)
            self.assertEqual({name: len(rows) for name, rows in MockDB.store.items()}, rows_before)
            self.assertEqual(pipeline.git_network._lookup_commit_ids(db, 1, [c1, "f" * 40]), {c1: ids[c1]})

    def _chain(self) -> tuple[list[str], list[GitCommit]]:
        """a <- b <- c <- d <- e, listed newest first with c before d (a parent dated after its child)."""
        a, b, c, d, e = (ch * 40 for ch in "abcde")
        commits = [
            _commit(e, "E", "e@x", [d], [("E", "e@x")]),
            _commit(c, "C", "c@x", [b]),
            _commit(d, "D", "d@x", [c]),
            _commit(b, "B", "b@x", [a]),
            _commit(a, "A", "a@x", []),
        ]
        return [a, b, c, d, e], commits

    def _parent_links(self) -> set[tuple[int, int]]:
        return {(row[0], row[1]) for row in MockDB.store[m_commit_parent.table_name].values()}

    def test_batches_write_a_parent_link_with_the_later_commit(self) -> None:
        (a, b, c, d, e), commits = self._chain()
        with mock.patch.object(pipeline.git_ingest, "_DAG_BATCH_COMMITS", 1), MockDB() as db:
            ids = pipeline.git_ingest._ingest_commit_range(db, commits, network_id=1)
        self.assertEqual(self._parent_links(), {(ids[b], ids[a]), (ids[c], ids[b]), (ids[d], ids[c]), (ids[e], ids[d])})
        self.assertEqual(len(MockDB.store[m_commit.table_name]), 5)

    def test_interrupted_ingest_resumes_without_losing_parent_links(self) -> None:
        (a, b, c, d, e), commits = self._chain()
        with mock.patch.object(pipeline.git_ingest, "_DAG_BATCH_COMMITS", 1), MockDB() as db:
            real_insert = db.insert

            def failing_insert(table, rows):
                # The batch of `c` (written after `d`: it is listed before it) never reaches the database
                if table is m_commit and any(bytes(r[2]).hex() == c for r in rows):
                    raise RuntimeError("connection lost")
                return real_insert(table, rows)

            with mock.patch.object(db, "insert", side_effect=failing_insert), self.assertRaises(RuntimeError):
                pipeline.git_ingest._ingest_commit_range(db, commits, network_id=1)
            stored = {bytes(row[2]).hex(): row[0] for row in MockDB.store[m_commit.table_name].values()}
            self.assertEqual(set(stored), {a, b, d})
            # The head is written last, and d's link to the missing c is not stored with a lost id
            self.assertEqual(self._parent_links(), {(stored[b], stored[a])})

            ids = pipeline.git_ingest._ingest_commit_range(db, commits, network_id=1)
        self.assertEqual({h: ids[h] for h in (a, b, d)}, stored)
        self.assertEqual(self._parent_links(), {(ids[b], ids[a]), (ids[c], ids[b]), (ids[d], ids[c]), (ids[e], ids[d])})
        self.assertEqual(len(MockDB.store[m_commit.table_name]), 5)

    def test_failed_commit_lookup_stops_the_ingest(self) -> None:
        _hashes, commits = self._chain()
        with MockDB() as db, mock.patch.object(db, "select_rows", side_effect=RuntimeError("read failed")):
            with self.assertRaises(RuntimeError):
                pipeline.git_ingest._ingest_commit_range(db, commits, network_id=1)
        self.assertFalse(MockDB.store.get(m_commit.table_name))

    def test_person_identity_is_name_and_email(self) -> None:
        self.assertEqual(pipeline.blob_reuse._person_key("A", "a@x"), ("A", "a@x"))
        self.assertEqual(pipeline.blob_reuse._person_key("", "a@x"), ("a@x", "a@x"))
        self.assertEqual(pipeline.blob_reuse._person_key(None, None), ("Unknown", ""))


class TestPreloadSummary(_TableEngineTestCase):
    def test_preload_summary_logs_cached_tables(self) -> None:
        with self.assertLogs("main", level="INFO") as cm:
            pipeline.blob_reuse._log_preload_summary(0.42)
        self.assertTrue(any("Preloaded cached tables in 0.4s" in output for output in cm.output))


class TestReuseParsedBlobs(_TableEngineTestCase):
    def test_already_parsed_blobs_are_linked_instead_of_parsed(self) -> None:
        known = b"\x07" * 20
        G.TE.set(m_file_blob.table_id, (7, known, type_check("drivers/a.c")))
        G.TE.set(m_bridge_tag.table_id, (7, 70, 1, 3, 0, 0))
        G.TE.commit_all()
        main.gp.VID = 5
        main.gp.Change_List = ["M\tdrivers/a.c", "A\tdrivers/b.c", "D\tdrivers/c.c"]
        hashes = {"drivers/a.c": known, "drivers/b.c": b"\x08" * 20}

        remaining = pipeline.blob_reuse._reuse_parsed_blobs(hashes)

        self.assertEqual(remaining, ["A\tdrivers/b.c", "D\tdrivers/c.c"])
        self.assertEqual(main.gp.deduped_paths, {"drivers/a.c"})
        self.assertEqual(main.gp.file_blob_ids["drivers/a.c"], 7)
        fnid = G.TE.get(m_file_name.table_id, (None, "drivers/a.c"))[0]
        self.assertIsNotNone(G.TE.get(m_bridge_checkpoint.table_id, (5, fnid, None)))
        # The head commit gets no version-diff m_commit_file row (only Step 6.5 writes what commits touched)
        self.assertNotIn((5, fnid), G.TE.queued_set.get(m_commit_file.table_id, {}))
        self.assertEqual(pipeline.commits._load_blob_tags([7]), {7: [(70, 7, 1, 3)]})

    def test_untagged_blobs_are_parsed_again(self) -> None:
        known = b"\x07" * 20
        G.TE.set(m_file_blob.table_id, (7, known, type_check("drivers/a.c")))
        G.TE.commit_all()
        main.gp.VID = 5
        main.gp.Change_List = ["M\tdrivers/a.c"]
        self.assertEqual(pipeline.blob_reuse._reuse_parsed_blobs({"drivers/a.c": known}), ["M\tdrivers/a.c"])
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
    def _scheduler(self, changed: set[str]) -> tuple[GreatProcessor, pipeline.scheduler.DependencyScheduler]:
        gp = GreatProcessor()
        gp.init_cs_dict()
        gp._changed_paths_set = set(changed)
        return gp, pipeline.scheduler.DependencyScheduler(gp)

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
        self.assertIs(pipeline.scheduler._stored_changeset_executed(store, "done.c"), True)
        self.assertIs(pipeline.scheduler._stored_changeset_executed(store, "pending_old.c"), False)
        self.assertIs(pipeline.scheduler._stored_changeset_executed(store, "pending_new.c"), False)
        self.assertIsNone(pipeline.scheduler._stored_changeset_executed(store, "absent.c"))


class TestDefaultProcessing(_TableEngineTestCase):
    def test_modified_file_no_longer_looks_up_its_previous_version_row(self) -> None:
        main.gp.VID, main.gp.Old_VID = 2, 1
        cs = ChangeSet("M\tkernel/fork.c")
        cs.blob_hash = b"\x02" * 20
        pipeline.file_processing.default_processing(cs)
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
    """m_ast_hash survives TableEngine restarts instead of being reloaded every version."""

    def setUp(self) -> None:
        self.saved = (G.DB, G.TE, main.gp.Old_VID)
        MockDB.reset()
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
        MockDB.reset()

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
        MockDB.reset()

    def tearDown(self) -> None:
        MockDB.reset()

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
        self.assertEqual(pipeline.worker_stage._balanced_batch_size(164, 7, 200), 6)
        self.assertEqual(pipeline.worker_stage._balanced_batch_size(17000, 7, 200), 200)
        self.assertEqual(pipeline.worker_stage._balanced_batch_size(10, 7, 200), 4)
        self.assertEqual(pipeline.worker_stage._balanced_batch_size(500, 7, 50), 18)


class TestSymbolTableAllocation(unittest.TestCase):
    def test_no_hugetlb_warning_for_symbol_tables(self) -> None:
        with self.assertNoLogs("table_engine.shared_buffer", level="WARNING"):
            reader = SharedSymbolBuilder(mode="auto").build({("f", 1): 1})
        reader.close()



class TestNonKernelProjects(unittest.TestCase):
    """Projects from repositories[].project (m_project / m_git_network) and the kernel-only steps a man-pages
    tree skips (core/API.md section 7.1.1)."""

    def setUp(self) -> None:
        from unittest import mock
        self.mock = mock
        self.saved = (G.DB, main.gp.VID, getattr(main.gp, "project_slug", None), getattr(main.gp, "file_blob_hashes", None))
        MockDB.reset()
        G.DB = MockDB
        pipeline.git_network._network_repo_cache.clear()
        pipeline.git_network._repo_project_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "pages")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", "-b", "master", self.repo], check=True, env=env)
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "--allow-empty", "-m", "c0"], check=True, env=env)

    def tearDown(self) -> None:
        G.DB, main.gp.VID, main.gp.project_slug, main.gp.file_blob_hashes = self.saved
        pipeline.git_network._network_repo_cache.clear()
        pipeline.git_network._repo_project_cache.clear()
        MockDB.reset()
        self.tmp.cleanup()

    def _resolve(self, entry):
        with self.mock.patch.object(pipeline.git_network, "get_repository", return_value=entry):
            return pipeline.git_network.resolve_git_network_and_repo(self.repo)

    def test_manpages_entry_gets_its_own_project_and_network(self) -> None:
        from core.DBLayout import m_git_network, m_project, m_repository
        net_id, repo_id = self._resolve({"name": "man-pages", "path": self.repo, "project": "man-pages"})
        self.assertEqual(main.gp.project_slug, "man-pages")
        self.assertFalse(pipeline.git_network.kernel_steps_apply())
        projects = list(MockDB.store[m_project.table_name].values())
        self.assertEqual([(p[1], p[2]) for p in projects], [("Linux man-pages", "man-pages")])
        networks = list(MockDB.store[m_git_network.table_name].values())
        self.assertEqual([(n[0], n[1], n[2]) for n in networks], [(net_id, projects[0][0], "man-pages-network")])
        self.assertIn(repo_id, [r[0] for r in MockDB.store[m_repository.table_name].values()])
        # Cached: a second call answers without the configuration
        with self.mock.patch.object(pipeline.git_network, "get_repository", side_effect=AssertionError("looked up again")):
            self.assertEqual(pipeline.git_network.resolve_git_network_and_repo(self.repo), (net_id, repo_id))

    def test_unconfigured_tree_stays_linux(self) -> None:
        from core.DBLayout import m_git_network, m_project
        self._resolve(None)
        self.assertEqual(main.gp.project_slug, "linux")
        self.assertTrue(pipeline.git_network.kernel_steps_apply())
        projects = list(MockDB.store[m_project.table_name].values())
        self.assertEqual([(p[1], p[2]) for p in projects], [("Linux Kernel", "linux")])
        self.assertEqual([n[2] for n in MockDB.store[m_git_network.table_name].values()], ["linux-network"])

    def test_project_slug_from_root_commit_and_explicit_arguments(self) -> None:
        from core.config import MANPAGES_ROOT_COMMIT
        with self.mock.patch.object(pipeline.git_network, "get_repository", return_value=None):
            self.assertEqual(pipeline.git_network.repo_project_slug(self.repo, [MANPAGES_ROOT_COMMIT]), "man-pages")
            pipeline.git_network._repo_project_cache.clear()
            self.assertEqual(pipeline.git_network.repo_project_slug(self.repo, ["0" * 40]), "linux")
        with self.mock.patch.object(pipeline.git_network, "get_repository", return_value={"project": "linux-stable"}):
            pipeline.git_network._repo_project_cache.clear()
            self.assertEqual(pipeline.git_network.repo_project_slug(self.repo, []), "linux-stable")
        pipeline.git_network._repo_project_cache.clear()
        pipeline.git_network.resolve_git_network_and_repo(self.repo, project_slug="rt")
        self.assertEqual(main.gp.project_slug, "rt")
        from core.DBLayout import m_project
        self.assertIn(("rt", "rt"), [(p[1], p[2]) for p in MockDB.store[m_project.table_name].values()])

    def test_kernel_only_steps_skip_manpages(self) -> None:
        main.gp.VID = 5
        main.gp.project_slug = "man-pages"
        main.gp.file_blob_hashes = {"GNUmakefile": b"\1" * 20, "man/man2/read.2": b"\2" * 20}
        boom = AssertionError("kernel-only step ran")
        with self.mock.patch.object(pipeline.file_references, "_version_file_list", side_effect=boom), \
                self.mock.patch.object(pipeline.file_references, "get_fid_for_path", side_effect=boom), \
                self.mock.patch.object(pipeline.maintainers, "get_fid_for_path", side_effect=boom), \
                self.mock.patch.object(pipeline.arch_layouts, "compute_layout_rows", side_effect=boom):
            pipeline.file_references.processing_kbuild("man-pages-6.9")
            pipeline.maintainers.processing_maintainer_files("man-pages-6.9")
            self.assertEqual(pipeline.arch_layouts.processing_arch_layouts("man-pages-6.9"), 0)
        # A kernel project without any kernel build input skips the ARCH layouts too
        main.gp.project_slug = "linux"
        with self.mock.patch.object(pipeline.arch_layouts, "compute_layout_rows", side_effect=boom):
            self.assertEqual(pipeline.arch_layouts.processing_arch_layouts("v0"), 0)


class TestFeatureGating(unittest.TestCase):
    """Per-repository feature flags and pipeline step gating."""

    def setUp(self) -> None:
        self.mock = unittest.mock
        self.saved = (
            G.DB,
            getattr(main.gp, "VID", 0),
            getattr(main.gp, "project_slug", None),
            getattr(main.gp, "file_blob_hashes", None),
            getattr(main.gp, "project_features", None),
        )
        MockDB.reset()
        G.DB = MockDB
        pipeline.git_network._network_repo_cache.clear()
        pipeline.git_network._repo_project_cache.clear()
        pipeline.external_steps._FACTS_MEMO.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "linux")
        os.makedirs(self.repo, exist_ok=True)
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        }
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "--allow-empty", "-m", "c0"], check=True, env=env)

    def tearDown(self) -> None:
        G.DB, main.gp.VID, main.gp.project_slug, main.gp.file_blob_hashes, main.gp.project_features = self.saved
        pipeline.git_network._network_repo_cache.clear()
        pipeline.git_network._repo_project_cache.clear()
        pipeline.external_steps._FACTS_MEMO.clear()
        MockDB.reset()
        self.tmp.cleanup()

    def test_resolve_stores_project_features_and_caches(self) -> None:
        entry = {"name": "linux", "path": self.repo, "project": "linux", "features": {"api": False, "kconfig": False}}
        with self.mock.patch.object(pipeline.git_network, "get_repository", return_value=entry):
            net_id, repo_id = pipeline.git_network.resolve_git_network_and_repo(self.repo)
        self.assertFalse(pipeline.git_network.feature_enabled("api"))
        self.assertFalse(pipeline.git_network.feature_enabled("kconfig"))
        self.assertTrue(pipeline.git_network.feature_enabled("docs"))
        main.gp.project_features = None
        with self.mock.patch.object(pipeline.git_network, "get_repository", side_effect=AssertionError("looked up again")):
            self.assertEqual(pipeline.git_network.resolve_git_network_and_repo(self.repo), (net_id, repo_id))
        self.assertFalse(pipeline.git_network.feature_enabled("api"))

    def test_disabled_features_gate_pipeline_steps(self) -> None:
        main.gp.VID = 5
        main.gp.project_slug = "linux"
        main.gp.file_blob_hashes = {"Makefile": b"\1" * 20}
        main.gp.project_features = {
            "kconfig": False,
            "docs": False,
            "maintainers": False,
            "mail": False,
            "commits": False,
            "insights": False,
        }
        boom = AssertionError("gated step ran")
        with self.mock.patch.object(pipeline.file_references, "_version_file_list", side_effect=boom), \
                self.mock.patch.object(pipeline.arch_layouts, "compute_layout_rows", side_effect=boom), \
                self.mock.patch.object(pipeline.maintainers, "get_fid_for_path", side_effect=boom):
            pipeline.file_references.processing_kbuild("v3.0")
            pipeline.file_references.processing_doc_references("v3.0")
            self.assertEqual(pipeline.arch_layouts.processing_arch_layouts("v3.0"), 0)
            pipeline.maintainers.processing_maintainer_files("v3.0")

        self.assertEqual(
            pipeline.mailing_list_steps.resolve_version_mailing_list_commits([object()], {}),
            0,
        )
        self.assertIsNone(pipeline.external_steps.processing_release_stats(self.repo, 1))

    def test_source_facts_filters_families_by_feature(self) -> None:
        main.gp.VID = 5
        main.gp.project_slug = "man-pages"
        main.gp.file_blob_hashes = {"man/man2/read.2": b"\2" * 20}
        main.gp.project_features = {"api": False, "kconfig": False, "docs": False, "man": True}
        with self.mock.patch("parser.facts_ingest.ingest", return_value={"extracted": 0, "blobs": 1, "rows": 0, "seconds": 0.0}) as mock_ingest:
            pipeline.external_steps.processing_source_facts("man-pages-6.9")
        mock_ingest.assert_called_once()
        self.assertEqual(mock_ingest.call_args.kwargs.get("families"), (1, 3, 6))

        main.gp.project_features = {"api": True, "kconfig": True, "docs": True, "man": True}
        with self.mock.patch("parser.facts_ingest.ingest", return_value={"extracted": 0, "blobs": 1, "rows": 0, "seconds": 0.0}) as mock_ingest:
            pipeline.external_steps.processing_source_facts("v3.0")
        mock_ingest.assert_called_once()
        self.assertNotIn("families", mock_ingest.call_args.kwargs)

    def test_automatic_kbuild_backfill_gated_by_kconfig(self) -> None:
        import argparse
        from core import backfills
        from parser.git_ast.commit_relations import BACKFILL_DONE

        main.gp.project_slug = "linux"
        main.gp.project_features = {"kconfig": False}
        args = argparse.Namespace(no_backfill=False)
        fake_db = self.mock.MagicMock()
        fake_db.row_exists.side_effect = [True, False]
        with self.mock.patch.object(G, "DB", return_value=self.mock.MagicMock(__enter__=lambda s: fake_db, __exit__=lambda s, *a: None)), \
                self.mock.patch.object(backfills, "commit_relations_state", return_value=BACKFILL_DONE), \
                self.mock.patch.object(backfills, "backfill_kbuild", side_effect=AssertionError("kbuild backfill ran")):
            pipeline.backfill_steps.run_automatic_backfills(args)


class TestUnifiedPreparseCLIAndOrchestration(unittest.TestCase):
    """Tests for --preparse CLI flags, backfill worker saturation, and unified single-pass orchestration."""

    def test_cli_preparse_flags_and_from_help(self) -> None:
        import pipeline.cli as cli

        parser = cli.build_arg_parser()
        args = parser.parse_args([
            "--preparse",
            "--no-preparse-stable",
            "--preparse-insights",
            "--preparse-api",
            "--preparse-tag", "v3.0",
            "--preparse-tag", "v3.1",
            "--kapi-arch", "x86_64",
            "--kapi-arch", "arm64",
            "--preparse-workers", "12",
            "--from", "v3.0",
        ])
        self.assertTrue(args.preparse)
        self.assertTrue(args.preparse_insights)
        self.assertFalse(args.preparse_stable)
        self.assertTrue(args.preparse_api)
        self.assertEqual(args.preparse_tags, ["v3.0", "v3.1"])
        self.assertEqual(args.kapi_arches, ["x86_64", "arm64"])
        self.assertEqual(args.preparse_workers, 12)
        self.assertEqual(args.backfill_from, "v3.0")
        from_opt = next(a for a in parser._actions if a.dest == "backfill_from")
        self.assertIn("pre-parse", from_opt.help.lower())

    def test_backfills_and_kapi_default_to_preparse_worker_count(self) -> None:
        import argparse
        from contextlib import contextmanager
        from core import backfills
        import pipeline.backfill_steps as bfs
        import pipeline.external_steps as ext

        @contextmanager
        def _ctx(db):
            yield db

        fake_db = mock.MagicMock()
        args = argparse.Namespace(
            backfill_facts=True,
            facts_families=None,
            backfill_doc_references=True,
            backfill_cve_files=True,
        )
        with mock.patch.object(G, "DB", lambda: _ctx(fake_db)), \
                mock.patch.object(bfs, "preparse_worker_count", return_value=16), \
                mock.patch.object(backfills, "backfill_facts", return_value={"ok": 1}) as bf_facts, \
                mock.patch.object(backfills, "backfill_doc_references", return_value={"ok": 1}) as bf_docs, \
                mock.patch.object(backfills, "backfill_cve_files", return_value={"ok": 1}) as bf_cve:
            self.assertTrue(bfs.run_requested_backfills(args, 1, 1))
        self.assertEqual(bf_facts.call_args.args[5], 16)
        self.assertEqual(bf_docs.call_args.args[4], 16)
        self.assertEqual(bf_cve.call_args.args[4], 16)

        with mock.patch.object(ext, "feature_enabled", return_value=True), \
                mock.patch.object(ext, "preparse_worker_count", return_value=16), \
                mock.patch.object(ext, "get_repository", return_value=None), \
                mock.patch.object(G, "DB", lambda: _ctx(fake_db)), \
                mock.patch("parser.kapi_ingest.ingest_kapi", return_value={"tags": 0, "built": 0, "skipped": 0, "errors": []}) as ki_mock:
            ext.processing_kapi("/tmp/repo", 1)
        self.assertEqual(ki_mock.call_args.kwargs["workers"], 16)

    def test_unified_preparse_runs_insights_stable_and_kapi_in_single_pass(self) -> None:
        from contextlib import contextmanager
        from core.DBLayout import TABLES
        import pipeline.external_steps as ext
        from parser import kapi_catalog as kc, kapi_facts as kf, kapi_ingest as ki
        from parser.git_ast import insights_deltas as real_deltas, release_stats

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = os.path.join(tmp.name, "linux")
        os.makedirs(repo)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True, env=env)
        for fname, content, tag in [
            ("a.c", "int v30;\n", "v3.0"),
            ("a.c", "int v301;\n", "v3.0.1"),
            ("a.c", "int v31;\n", "v3.1"),
        ]:
            with open(os.path.join(repo, fname), "w") as f:
                f.write(content)
            subprocess.run(["git", "-C", repo, "add", fname], check=True, env=env)
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", tag], check=True, env=env)
            subprocess.run(["git", "-C", repo, "tag", tag], check=True, env=env)

        def _sha(t):
            return subprocess.run(["git", "-C", repo, "rev-parse", f"{t}^{{commit}}"], capture_output=True, text=True, check=True).stdout.strip()

        db = MockDB(use_global=False)
        db.create_table(TABLES)
        self.addCleanup(db.close)
        db.insert_rows("m_project", ["project_id", "name", "slug"], [(1, "Linux", "linux")])
        db.insert_rows("m_git_network", ["network_id", "project_id", "name", "root_commit_hash"], [(1, 1, "main", bytes.fromhex(_sha("v3.0")))])
        db.insert_rows("m_repository", ["repo_id", "network_id", "name", "git_url", "default_branch"], [(1, 1, "linux", repo, "master")])
        db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(1, "t", "t@t")])
        db.insert_rows(
            "m_commit",
            ["commit_id", "network_id", "commit_hash", "author_id", "author_date", "committer_id", "committer_date", "subject", "message"],
            [
                (10, 1, bytes.fromhex(_sha("v3.0")), 1, 1, 1, 1, "v3.0", "v3.0"),
                (11, 1, bytes.fromhex(_sha("v3.1")), 1, 2, 1, 2, "v3.1", "v3.1"),
            ],
        )
        db.insert_rows(
            "m_git_ref",
            ["ref_id", "repo_id", "ref_type", "ref_name", "commit_id", "tagger_id", "tagger_date", "tag_message"],
            [(1, 1, 1, "v3.0", 10, 1, 1, ""), (2, 1, 1, "v3.1", 11, 1, 2, "")],
        )
        # Mark v3.0 KAPI already current so only v3.1 KAPI runs, while v3.0 insights/v3.0.1 stable/v3.1 all run
        for kind in kc.ARCH_INDEPENDENT_KINDS:
            db.insert_rows("m_kapi_catalog", ["commit_id", "arch", "kind", "catalog_format", "kapi_format", "total_count", "summary_json", "data", "built_at"],
                           [(10, "", kind, kc.CATALOG_FORMAT, kf.KAPI_FORMAT, 0, "{}", b"{}", 1)])
        db.insert_rows("m_kapi_release_summary", ["commit_id", "arch", "catalog_format", "kapi_format", "syscalls_count", "exports_count", "params_count", "tracepoints_count", "modules_count", "devices_count", "subsystems_json", "built_at"],
                       [(10, "x86_64", kc.CATALOG_FORMAT, kf.KAPI_FORMAT, 0, 0, 0, 0, 0, 0, "{}", 1)])

        write_delta_records = []
        write_kapi_records = []

        def _fake_extract_deltas(repo_arg, tag, sha, parent_tag, parent_sha, **kwargs):
            return {
                "tag": tag,
                "parent_tag": parent_tag,
                "compute_pulls": kwargs.get("compute_pulls"),
                "pulls": [],
                "sec_stats": {},
                "sections": [],
            }

        def _fake_write_deltas_in_tx(db_arg, extracted, replace=False):
            write_delta_records.append(
                (extracted["tag"], extracted.get("parent_tag"), extracted.get("compute_pulls"), bool(replace))
            )

        def _fake_extract_kapi(rp, tag, *, arches=("x86_64",), existing_facts=None, workers=1):
            return {"tag": tag, "arches": list(arches)}

        def _fake_write_kapi(db_arg, commit_id, payload):
            write_kapi_records.append((commit_id, payload["tag"], tuple(payload["arches"])))
            return {"status": "built"}

        @contextmanager
        def _ctx():
            yield db

        pool_calls = []
        real_pool = ext.run_unified_preparse_pool

        def _spy_pool(*args, **kwargs):
            pool_calls.append(len(args[0]))
            return real_pool(*args, **kwargs)

        with mock.patch.object(G, "DB", _ctx), \
                mock.patch.object(ext, "feature_enabled", return_value=True), \
                mock.patch.object(ext, "get_repository", return_value=None), \
                mock.patch.object(ext, "run_unified_preparse_pool", side_effect=_spy_pool), \
                mock.patch.object(real_deltas, "extract_release_deltas_from_git", side_effect=_fake_extract_deltas), \
                mock.patch.object(real_deltas, "write_deltas_in_tx", side_effect=_fake_write_deltas_in_tx), \
                mock.patch.object(real_deltas, "load_cve_commit_hashes", return_value=set()), \
                mock.patch.object(real_deltas, "build_sha_to_release", return_value=None), \
                mock.patch.object(ki, "extract_kapi_release_payload", side_effect=_fake_extract_kapi), \
                mock.patch.object(ki, "write_kapi_release_payload", side_effect=_fake_write_kapi):
            summary = ext.processing_unified_preparse(repo, 1, workers=2)
            first_deltas = sorted(write_delta_records)
            # Also verify tags override + from_tag filtering (v3.0.1 has no commit_id -> api_skipped_no_commit=1)
            write_delta_records.clear()
            write_kapi_records.clear()
            summary_filtered = ext.processing_unified_preparse(
                repo, 1, tags=["v3.0", "v3.0.1", "v3.1"], from_tag="v3.0.1", workers=2
            )
            # A version a heartbeating peer node is parsing is in progress, not interrupted: writes go on
            db.insert_rows("m_parse_node", ["node_id", "network_id", "role", "hostname", "pid", "heartbeat_at"],
                           [("peer", 1, "worker", "h", 1, int(time.time()))])
            db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at", "worker_id"],
                           [(1, 11, 1, 1, "peer")])
            db.delete_rows("m_release_segment", {"repo_id": 1, "tag": "v3.1"})
            write_delta_records.clear()
            summary_peer = ext.processing_unified_preparse(repo, 1, tags=["v3.1"], force=True, workers=1)
            # Verify interrupted ingest skips m_release_segment writes
            db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"], [(1, 10, 1, 1)])
            write_delta_records.clear()
            write_kapi_records.clear()
            summary_interrupted = ext.processing_unified_preparse(repo, 1, tags=["v3.1"], force=True, workers=1)

        self.assertEqual(len(pool_calls), 4, "each call must execute in a single unified pool pass")
        self.assertEqual(summary_peer["segments_built"], 1, "a heartbeating peer ingest must not defer segment writes")
        self.assertEqual(summary["state"], "updated")
        self.assertEqual(summary["insights_built"], 2)
        self.assertEqual(summary["stable_built"], 1)
        self.assertEqual(summary["api_built"], 1)
        self.assertEqual(summary["segments_built"], 3)
        self.assertEqual(
            first_deltas,
            [
                ("v3.0", None, True, False),
                ("v3.0.1", "v3.0", False, False),
                ("v3.1", "v3.0", True, False),
            ],
        )
        self.assertEqual(summary_filtered["api_skipped_no_commit"], 1)
        self.assertEqual(summary_filtered["tags"], 2)
        self.assertEqual(summary_interrupted["segments_built"], 0)
        self.assertEqual(summary_interrupted["insights_built"], 1)
        self.assertEqual(summary_interrupted["api_built"], 1)
        self.assertEqual(len(release_stats.stored_segments(db, 1)), 3)
        self.assertEqual(
            write_delta_records,
            [("v3.1", "v3.0", True, True)],
        )
        self.assertEqual(write_kapi_records, [(11, "v3.1", ("x86_64",))])

    def test_unified_preparse_end_to_end_real_extractors_and_sqlite_tables(self) -> None:
        from contextlib import contextmanager
        from core.DBLayout import TABLES
        from db_engine import SQLiteDB
        import pipeline.external_steps as ext

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = os.path.join(tmp.name, "linux")
        os.makedirs(repo)
        env = {**os.environ, "GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "alice@example.com",
               "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "alice@example.com"}
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True, env=env)

        def _commit_tag(files: dict[str, str], tag: str, msg: str) -> str:
            for rel, txt in files.items():
                p = os.path.join(repo, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(txt)
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True, env=env)
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", msg], check=True, env=env)
            subprocess.run(["git", "-C", repo, "tag", tag], check=True, env=env)
            return subprocess.run(
                ["git", "-C", repo, "rev-parse", f"{tag}^{{commit}}"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

        maint = "CORE SUBSYSTEM\nM:\tAlice <alice@example.com>\nS:\tMaintained\nF:\tfs/\n"
        sha30 = _commit_tag(
            {
                "MAINTAINERS": maint,
                "Makefile": "VERSION = 3\nPATCHLEVEL = 0\nSRCARCH := x86\ncore-y := fs/\n",
                "arch/x86/Makefile": "",
                "arch/x86/entry/syscalls/syscall_64.tbl": "0\tcommon\tread\tsys_read\n",
                "fs/Makefile": "obj-y += rw.o\n",
                "fs/rw.c": "SYSCALL_DEFINE3(read, int, fd, char *, buf, size_t, n) { return 0; }\nEXPORT_SYMBOL(vfs_read);\n",
            },
            "v3.0",
            "Linux v3.0",
        )
        sha301 = _commit_tag(
            {
                "fs/rw.c": (
                    "SYSCALL_DEFINE3(read, int, fd, char *, buf, size_t, n) { return 0; }\n"
                    "EXPORT_SYMBOL(vfs_read);\nEXPORT_SYMBOL(vfs_write);\n"
                ),
            },
            "v3.0.1",
            f"fix read\n\nFixes: {sha30[:12]} (\"Linux v3.0\")\nCc: stable@vger.kernel.org\n",
        )
        sha31 = _commit_tag(
            {
                "fs/rw.c": (
                    "SYSCALL_DEFINE3(read, int, fd, char *, buf, size_t, n) { return 0; }\n"
                    "EXPORT_SYMBOL(vfs_read);\nEXPORT_SYMBOL(vfs_write);\nEXPORT_SYMBOL(vfs_open);\n"
                ),
            },
            "v3.1",
            "Linux v3.1",
        )

        db = SQLiteDB(":memory:")
        db.create_table(TABLES)
        self.addCleanup(db.close)
        db.insert_rows("m_project", ["project_id", "name", "slug"], [(1, "Linux", "linux")])
        db.insert_rows("m_git_network", ["network_id", "project_id", "name", "root_commit_hash"], [(1, 1, "mainline", bytes.fromhex(sha30))])
        db.insert_rows("m_repository", ["repo_id", "network_id", "name", "git_url", "default_branch"], [(1, 1, "linux", repo, "master")])
        db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(1, "Alice", "alice@example.com")])
        db.insert_rows(
            "m_commit",
            ["commit_id", "network_id", "commit_hash", "author_id", "author_date", "committer_id", "committer_date", "subject", "message"],
            [
                (10, 1, bytes.fromhex(sha30), 1, 1, 1, 1, "v3.0", "v3.0"),
                (11, 1, bytes.fromhex(sha301), 1, 2, 1, 2, "v3.0.1", "v3.0.1"),
                (12, 1, bytes.fromhex(sha31), 1, 3, 1, 3, "v3.1", "v3.1"),
            ],
        )
        db.insert_rows(
            "m_git_ref",
            ["ref_id", "repo_id", "ref_type", "ref_name", "commit_id", "tagger_id", "tagger_date", "tag_message"],
            [(1, 1, 1, "v3.0", 10, 1, 1, ""), (2, 1, 1, "v3.0.1", 11, 1, 2, ""), (3, 1, 1, "v3.1", 12, 1, 3, "")],
        )

        @contextmanager
        def _ctx():
            yield db

        with mock.patch.object(G, "DB", _ctx), \
                mock.patch.object(ext, "feature_enabled", return_value=True), \
                mock.patch.object(ext, "get_repository", return_value=None):
            summary = ext.processing_unified_preparse(repo, 1, workers=2)
            again = ext.processing_unified_preparse(repo, 1, workers=2)

        self.assertEqual(summary["state"], "updated")
        self.assertEqual(summary["insights_built"], 2)
        self.assertEqual(summary["stable_built"], 1)
        self.assertEqual(summary["api_built"], 2)  # all_main -> v3.0 and v3.1
        self.assertEqual(summary["errors"], [])
        self.assertGreater(db.count_rows("insights_subsystem_version_delta"), 0)
        self.assertGreater(db.count_rows("insights_subsystem_author_delta"), 0)
        self.assertGreater(db.count_rows("insights_stable_version_delta"), 0)
        self.assertGreater(db.count_rows("m_release_segment"), 0)
        self.assertGreater(db.count_rows("m_release_pull"), 0)
        self.assertGreater(db.count_rows("m_release_section_health"), 0)
        self.assertGreater(db.count_rows("m_kapi_catalog"), 0)
        self.assertEqual(db.count_rows("m_kapi_release_summary"), 2)
        self.assertGreater(db.count_rows("m_blob_facts"), 0)

        # Second pass skips all already-current tags
        self.assertEqual(again["state"], "current")
        self.assertEqual(again["work_items"], 0)
        self.assertEqual(again["skipped_current"], 3)

    def test_main_dispatches_preparse_mode_without_libclang_preflight(self) -> None:
        import argparse
        import tempfile as tf

        tmp = tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = os.path.join(tmp.name, "linux")
        os.makedirs(repo)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True, env=env)
        subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)

        args = argparse.Namespace(
            mem_max=0,
            role="standalone",
            reset=False,
            Drop=False,
            repo=repo,
            cve=False,
            no_cve=True,
            clean_temp=False,
            resolve_ml=False,
            branches=False,
            ref=["HEAD"],
            single_version=True,
            include_pre_releases=True,
            release_stats=False,
            no_release_stats=True,
            dry_run=False,
            backfill=None,
            preparse=True,
            preparse_insights=True,
            preparse_stable=False,
            preparse_api=True,
            preparse_tags=["v3.0"],
            kapi_arches=["x86_64"],
            preparse_workers=6,
            backfill_from="v3.0",
        )
        fake_db = MockDB(use_global=False)
        fake_db.create_table(main.TABLES)
        self.addCleanup(fake_db.close)

        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield fake_db

        with mock.patch.object(main, "arg_handling", return_value=args), \
                mock.patch.object(G, "DB", _ctx), \
                mock.patch.object(main, "_apply_db_server_tuning"), \
                mock.patch.object(main, "get_ssh_tunnel_config", return_value={"enabled": False}), \
                mock.patch.object(main, "resolve_git_network_and_repo", return_value=(1, 1)), \
                mock.patch.object(main.ingest_state, "fetch_repo_version_states", return_value={}), \
                mock.patch.object(main, "_preflight_libclang", side_effect=AssertionError("preflight ran")), \
                mock.patch.object(main, "processing_unified_preparse", return_value={"state": "updated"}) as prep_mock, \
                mock.patch.object(G, "emergency_shutdown") as shut_mock:
            main._run()
        prep_mock.assert_called_once_with(
            repo,
            1,
            include_insights=True,
            include_stable=False,
            include_api=True,
            tags=["v3.0"],
            from_tag="v3.0",
            arches=("x86_64",),
            workers=6,
        )
        shut_mock.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
