"""tests/test_ingest_state.py - Recovery of interrupted version runs (core/ingest_state.py)."""
from __future__ import annotations

import unittest

from core import ingest_state as I
from core.DBLayout import TABLES
from db_engine import MockDB, SQLiteDB
from db_engine.spec import CURRENT_TIMESTAMP, NO_DEFAULT, gt, le

BY_NAME = {t.table_name: t for t in TABLES}


def _cols(name: str) -> list[str]:
    return [c[0] for c in BY_NAME[name].init_columns]


class _RowHelpers:
    """Row helpers for the rollback scenarios, shared by the MockDB and the SQLiteDB test databases."""

    def add(self, table: str, *values) -> None:
        """Insert a row; columns past `values` take a neutral filler (0, b"" or "" by type) when they have no default."""
        specs = BY_NAME[table].init_columns
        row = list(values)
        for _name, spec in specs[len(values):]:
            if spec.default is not NO_DEFAULT and spec.default is not CURRENT_TIMESTAMP:
                row.append(spec.default)
            else:
                row.append(b"" if spec.is_binary else "" if spec.type.name in ("CHAR", "VARCHAR", "TEXT", "MEDIUMTEXT", "LONGTEXT") else 0)
        self.insert_rows(table, _cols(table), [tuple(row)])

    def table_rows(self, table: str) -> list[dict]:
        return self.select_rows(table, _cols(table), as_dict=True)

    @property
    def rows(self) -> dict[str, list[dict]]:
        return {t.table_name: self.table_rows(t.table_name) for t in TABLES}

    def run(self, plan: list[I.Delete]) -> None:
        for d in plan:
            d.run(self)

    def dangling(self) -> list[str]:
        out = []
        for t in TABLES:
            for col, parent, pcol in (t.init_foreign or ()):
                keys = {r[pcol] for r in self.table_rows(parent)}
                out += [f"{t.table_name}.{col}={r[col]}" for r in self.table_rows(t.table_name) if r[col] not in keys]
        return out


class MemoryDB(_RowHelpers, MockDB):
    """An isolated MockDB over the whole schema."""

    def __init__(self) -> None:
        MockDB.__init__(self, use_global=False)


class SQLiteMemoryDB(_RowHelpers, SQLiteDB):
    """An in-memory SQLiteDB with every table of the schema, empty."""

    def __init__(self) -> None:
        SQLiteDB.__init__(self, ":memory:", foreign_keys=False)
        self.create_table(list(TABLES))
        for t in TABLES:
            self.delete_rows(t.table_name, None, all_rows=True)


BACKEND: list[type] = [MemoryDB]


def new_db() -> MemoryDB:
    """A fresh empty database of the backend under test."""
    return BACKEND[0]()


def seeded() -> MemoryDB:
    """Rows before the run (ids <= 10) plus rows an interrupted run wrote (ids > 10, and blob 3's rows)."""
    db = new_db()
    db.add("m_type_descriptor", 1, "C_function")
    db.add("m_project", 1, "Linux", "linux")
    db.add("m_git_network", 1, 1, "linux", b"root")
    db.add("m_maintainer_person", 1, "Linus", "l@k.org")
    db.add("m_commit", 5, 1, b"h5", 1, 0, 1, 0, "base", "")
    db.add("m_commit", 10, 1, b"h10", 1, 0, 1, 0, "head", "")
    db.add("m_commit", 11, 1, b"h11", 1, 0, 1, 0, "added later by the run", "")
    db.add("m_commit_parent", 10, 5, 0)
    db.add("m_commit_parent", 11, 10, 0)
    for fnid, name in ((1, "kernel/sched.c"), (2, "mm/slab.c"), (11, "new/file.c")):
        db.add("m_file_name", fnid, name)
    for blob in (1, 2, 3, 11):  # blob 3: created earlier (intermediate commit) but parsed by the run
        db.add("m_file_blob", blob, bytes([blob]) * 20, 1)
    db.add("m_bridge_checkpoint", 5, 1, 1)
    db.add("m_bridge_checkpoint", 10, 1, 1)
    db.add("m_bridge_checkpoint", 10, 11, 11)
    db.add("m_commit_file", 5, 2, 3, 0, 0, "A")  # the old commit that introduced blob 3 keeps its row
    db.add("m_ast", 1, "schedule", 1)
    db.add("m_ast", 11, "new_fn", 1)
    db.add("m_ast_container", 1, 0, 1, 1)
    db.add("m_ast_container", 11, 0, 1, 1)
    db.add("m_ast_hash", b"a11", 11)
    db.add("m_tag", 1, b"c1", 1, 0, 0)
    db.add("m_tag", 11, b"c1", 11, 0, 0)
    db.add("m_tag_def", 1, 0, 7, "schedule", 1, 0, 0, 5)
    db.add("m_tag_def", 11, 0, 8, "new_fn", 1, 0, 0, 5)
    db.add("m_bridge_tag", 1, 1, 1, 5, 0, 0)
    db.add("m_bridge_tag", 3, 1, 1, 5, 0, 0)    # partial rows of the re-parsed blob (old tag only)
    db.add("m_bridge_tag", 11, 11, 1, 5, 0, 0)
    db.add("m_symbol_own", 3, 1, 2)
    db.add("m_blob_symbol_def", 1, 1, 1, 1, "schedule", 1, 0, 1, 5)
    db.add("m_blob_symbol_def", 12, 3, 1, 1, "schedule", 1, 0, 1, 5)
    db.add("m_ingest_state", 1, 10, I.INGEST_PARSING, 0)
    db.add("m_arch_layout", 1, b"L" * 16)
    db.add("m_arch_layout", 11, b"N" * 16)                       # a layout the run created
    db.add("m_arch_layout_entry", 1, 2, 0, "include/", "")
    db.add("m_arch_layout_entry", 11, 2, 0, "include2/", "")
    db.add("m_arch_root", 5, "x86_64", 1)
    db.add("m_arch_root", 10, "x86_64", 1)    # the interrupted version's layout (an older, shared one)
    db.add("m_arch_root", 11, "x86_64", 11)
    return db


MARKS = {name: 10 for name in (t.table_name for t, _ in I.marked_tables(TABLES))}


class TestRollbackPlan(unittest.TestCase):
    def test_marked_tables_are_the_id_allocating_ones(self) -> None:
        names = {t.table_name for t, _ in I.marked_tables(TABLES)}
        self.assertTrue({"m_file_blob", "m_file_name", "m_ast", "m_tag", "m_commit", "m_blob_symbol_def"} <= names)
        # Static registries, hash/composite keys and foreign-key primary keys are not watermarked
        self.assertFalse(names & {"m_type_descriptor", "m_git_ref", "m_repository", "m_bridge_tag",
                                  "m_ast_include", "m_ingest_state", "m_ingest_watermark", "m_ingest_blob"})

    def test_children_are_deleted_before_their_parents(self) -> None:
        plan = I.rollback_plan(TABLES, MARKS, 10, [3])
        last_delete = {d.table: i for i, d in enumerate(plan)}
        for t in TABLES:
            for _, parent, _ in (t.init_foreign or ()):
                if parent != t.table_name and t.table_name in last_delete and parent in MARKS:
                    first_parent = min(i for i, d in enumerate(plan) if d.table == parent and d.condition.endswith(" >"))
                    self.assertLess(
                        min(i for i, d in enumerate(plan) if d.table == t.table_name and d.condition.endswith(" >"))
                        if any(d.table == t.table_name and d.condition.endswith(" >") for d in plan) else -1,
                        first_parent, f"{t.table_name} must be cleared before {parent}",
                    )

    def test_rollback_restores_the_state_before_the_run(self) -> None:
        db = seeded()
        db.run(I.rollback_plan(TABLES, MARKS, 10, [3]))
        self.assertEqual(db.dangling(), [])
        rows = db.rows
        # Everything the run created is gone
        self.assertEqual([r["commit_id"] for r in rows["m_commit"]], [5, 10])
        self.assertEqual({r["fnid"] for r in rows["m_file_name"]}, {1, 2})
        self.assertEqual({r["blob_id"] for r in rows["m_file_blob"]}, {1, 2, 3})
        self.assertEqual({r["tag_id"] for r in rows["m_tag"]}, {1})
        self.assertEqual({r["ast_id"] for r in rows["m_ast"]}, {1})
        self.assertEqual(rows["m_ast_hash"], [])
        # The version's checkpoint and the re-parsed blob's partial rows too
        self.assertEqual([r["commit_id"] for r in rows["m_bridge_checkpoint"]], [5])
        self.assertEqual([r["commit_id"] for r in rows["m_arch_root"]], [5])
        self.assertEqual([r["layout_id"] for r in rows["m_arch_layout"]], [1], "the run's new layout goes, the shared one stays")
        self.assertEqual([r["layout_id"] for r in rows["m_arch_layout_entry"]], [1])
        self.assertEqual([r["blob_id"] for r in rows["m_bridge_tag"]], [1])
        self.assertEqual(rows["m_symbol_own"], [])
        self.assertEqual([r["def_id"] for r in rows["m_blob_symbol_def"]], [1])
        # Rows older than the run survive, including the old commit that introduced blob 3
        self.assertEqual(len(rows["m_commit_file"]), 1)
        self.assertEqual([r["tag_id"] for r in rows["m_tag_def"]], [1])
        self.assertEqual(len(rows["m_ast_container"]), 1)
        self.assertEqual(len(rows["m_ingest_state"]), 1)

    def test_rollback_is_idempotent(self) -> None:
        db = seeded()
        plan = I.rollback_plan(TABLES, MARKS, 10, [3])
        db.run(plan)
        snapshot = db.rows
        db.run(plan)
        self.assertEqual(db.rows, snapshot)

    def test_precheck_flags_older_rows_pointing_at_new_ones(self) -> None:
        queries = dict(I.precheck_queries(TABLES, MARKS))
        select = queries["m_tag.ast_id -> m_ast"]
        self.assertEqual((select.source, list(select.columns), select.limit), ("m_tag", ["tag_id"], 1))
        self.assertEqual(select.where, {"tag_id": le(10), "ast_id": gt(10)})


def _watermarks(db: MemoryDB, commit_id: int = 10) -> None:
    """The watermark rows `begin()` stored for a run whose marks are MARKS."""
    for t, _col in I.marked_tables(TABLES):
        db.add("m_ingest_watermark", commit_id, t.table_id, MARKS[t.table_name])


class TestIngestStateDatabase(unittest.TestCase):
    def test_begin_records_watermarks_and_parsing_status(self) -> None:
        db = seeded()
        marks = I.begin(db, 1, 10, TABLES, base_commit_id=5, worker_id="w1")
        self.assertEqual(marks["m_commit"], 11, "the highest commit id of the seeded rows")
        self.assertEqual(marks["m_file_name"], 11)
        stored = {int(r["marked_table_id"]): int(r["max_id"]) for r in db.table_rows("m_ingest_watermark")}
        self.assertEqual(stored, {t.table_id: marks[t.table_name] for t, _ in I.marked_tables(TABLES)})
        state = db.table_rows("m_ingest_state")[0]
        self.assertEqual((state["network_id"], state["commit_id"], state["status"]), (1, 10, I.INGEST_PARSING))
        self.assertEqual((state["base_commit_id"], state["worker_id"]), (5, "w1"))
        self.assertEqual(I.get_status(db, 1, 10), I.INGEST_PARSING)
        self.assertIsNone(I.get_status(db, 1, 99))

    def test_begin_replaces_the_marks_of_an_earlier_attempt(self) -> None:
        db = seeded()
        I.begin(db, 1, 10, TABLES)
        db.add("m_ingest_blob", 10, 3)
        I.begin(db, 1, 10, TABLES)
        self.assertEqual(db.count_rows("m_ingest_blob"), 0)
        self.assertEqual(db.count_rows("m_ingest_watermark"), len(I.marked_tables(TABLES)))

    def test_done_clears_the_bookkeeping(self) -> None:
        db = seeded()
        _watermarks(db)
        db.add("m_ingest_blob", 10, 3)
        I.set_status(db, 1, 10, I.INGEST_DONE)
        self.assertEqual((db.count_rows("m_ingest_watermark"), db.count_rows("m_ingest_blob")), (0, 0))
        self.assertEqual(I.get_status(db, 1, 10), I.INGEST_DONE)

    def test_a_commit_zero_status_is_ignored(self) -> None:
        db = new_db()
        I.set_status(db, 1, 0, I.INGEST_DONE)
        self.assertEqual(db.count_rows("m_ingest_state"), 0)

    def test_record_blobs_ignores_repeats(self) -> None:
        db = new_db()
        I.record_blobs(db, 10, [3, 3, 4])
        I.record_blobs(db, 10, [4, 5])
        self.assertEqual(sorted(r[0] for r in db.select_rows("m_ingest_blob", ["blob_id"], {"commit_id": 10})), [3, 4, 5])

    def test_rollback_without_watermarks_reports_it(self) -> None:
        self.assertFalse(I.rollback_version(new_db(), 1, 10, TABLES))

    def test_rollback_refuses_when_older_rows_reference_the_run(self) -> None:
        db = new_db()
        db.add("m_ingest_watermark", 10, BY_NAME["m_tag"].table_id, 10)
        db.add("m_ingest_watermark", 10, BY_NAME["m_ast"].table_id, 10)
        db.add("m_tag", 4, b"c4", 99, 0, 0)  # older than the run (tag 4 <= 10) but pointing at an ast of the run
        with self.assertRaises(I.RecoveryError):
            I.rollback_version(db, 1, 10, TABLES)

    def test_rollback_version_undoes_the_run_and_forgets_its_marks(self) -> None:
        db = seeded()
        _watermarks(db)
        db.add("m_ingest_blob", 10, 3)
        self.assertTrue(I.rollback_version(db, 1, 10, TABLES))
        self.assertEqual(db.dangling(), [])
        self.assertEqual([r["commit_id"] for r in db.table_rows("m_commit")], [5, 10])
        self.assertEqual([r["commit_id"] for r in db.table_rows("m_bridge_checkpoint")], [5])
        self.assertEqual([r["blob_id"] for r in db.table_rows("m_bridge_tag")], [1])
        self.assertEqual((db.count_rows("m_ingest_watermark"), db.count_rows("m_ingest_blob")), (0, 0))

    def test_rollback_version_with_stale_marks_discards_them(self) -> None:
        db = seeded()
        _watermarks(db)
        db.add("m_ingest_state", 1, 11, I.INGEST_DONE, 0)  # a version finished on top of the marks
        self.assertFalse(I.rollback_version(db, 1, 10, TABLES))
        self.assertEqual(db.count_rows("m_ingest_watermark"), 0)
        self.assertEqual(len(db.table_rows("m_commit")), 3, "nothing of the finished version is deleted")

    def test_rollback_by_leased_id_ranges(self) -> None:
        db = seeded()
        db.add("m_ingest_id_range", 10, BY_NAME["m_ast"].table_id, 11, 20)
        db.add("m_ingest_blob", 10, 3)
        self.assertTrue(I.rollback_version(db, 1, 10, TABLES))
        self.assertEqual({r["ast_id"] for r in db.table_rows("m_ast")}, {1}, "the leased range goes")
        self.assertEqual({r["ast_id"] for r in db.table_rows("m_ast_container")}, {1})
        self.assertEqual(db.table_rows("m_ast_hash"), [])
        self.assertEqual([r["commit_id"] for r in db.table_rows("m_bridge_checkpoint")], [5])
        self.assertEqual(db.count_rows("m_ingest_state"), 0, "the run's state row goes with it")
        self.assertEqual(db.count_rows("m_ingest_id_range"), 0)
        self.assertEqual({r["blob_id"] for r in db.table_rows("m_file_blob")}, {1, 2, 3, 11}, "registries stay")

    def test_fetch_repo_version_states_classifies_correctly(self) -> None:
        db = new_db()
        refs = (("v1.0", 101), ("v1.1", 102), ("v1.2", 103), ("v1.3", 104), ("unresolved", 0))
        db.insert_rows("m_git_ref", ["repo_id", "ref_type", "ref_name", "commit_id"], [(1, 1, n, c) for n, c in refs])
        db.insert_rows("m_git_ref", ["repo_id", "ref_type", "ref_name", "commit_id"], [(2, 1, "other-repo", 105)])
        db.add("m_ingest_state", 1, 101, I.INGEST_DONE, 0)
        db.add("m_ingest_state", 1, 102, I.INGEST_PARSING, 0)
        db.add("m_ingest_state", 2, 104, I.INGEST_DONE, 0)   # another network's state does not count
        db.add("m_bridge_checkpoint", 103, 1, 1)
        states = I.fetch_repo_version_states(db, repo_id=1, network_id=1)
        self.assertEqual(states["v1.0"], (101, "done"))
        self.assertEqual(states["v1.1"], (102, "interrupted"))
        # Checkpoint rows without a state row: completeness is decided by main._previous_ingest() (90% rule)
        self.assertEqual(states["v1.2"], (103, "legacy"))
        self.assertEqual(states["v1.3"], (104, "new"))
        self.assertNotIn("unresolved", states)
        self.assertNotIn("other-repo", states)

    def _purge_fixture(self) -> MemoryDB:
        db = new_db()
        for cid, blob in ((40, 1), (50, 1), (50, 2)):
            db.add("m_bridge_checkpoint", cid, blob, blob)
        for blob, tag in ((1, 100), (2, 100), (2, 200)):
            db.add("m_bridge_tag", blob, tag, 1, 5, 0, 0)
        db.add("m_moved_tag", 7, 100)
        db.add("m_moved_tag", 7, 200)
        db.add("m_commit_file", 50, 2, 2, 0, 0, "A")
        db.add("m_commit_file", 40, 1, 1, 0, 0, "A")
        db.add("m_ingest_state", 1, 50, I.INGEST_DONE, 0)
        return db

    def test_purge_version_differential_rows_cleans_version_records(self) -> None:
        db = self._purge_fixture()
        deleted = I.purge_version_differential_rows(db, commit_id=50, pred_commit_id=40)
        self.assertGreater(deleted, 0)
        self.assertEqual([r["commit_id"] for r in db.table_rows("m_bridge_checkpoint")], [40])
        self.assertEqual([r["commit_id"] for r in db.table_rows("m_commit_file")], [40])
        self.assertEqual(I.get_status(db, 1, 50), I.INGEST_PARSING)
        # Only tags absent from the predecessor's tree lose their inbound transitions
        self.assertEqual([r["e_tag_id"] for r in db.table_rows("m_moved_tag")], [100])

    def test_purge_without_predecessor_keeps_moved_tags(self) -> None:
        db = self._purge_fixture()
        I.purge_version_differential_rows(db, commit_id=50)
        self.assertEqual(sorted(r["e_tag_id"] for r in db.table_rows("m_moved_tag")), [100, 200])
        self.assertEqual([r["commit_id"] for r in db.table_rows("m_bridge_checkpoint")], [40])

    def test_cleanup_keeps_directories_of_running_instances(self) -> None:
        import tempfile
        import os
        from core.FileHandler import mark_owner
        with tempfile.TemporaryDirectory() as tmp_dir:
            live = tempfile.mkdtemp(prefix="code-parser.live-", dir=tmp_dir)
            mark_owner(live)
            dead = tempfile.mkdtemp(prefix="kip-commits-", dir=tmp_dir)
            with open(os.path.join(dead, ".kip-owner"), "w") as f:
                f.write("999999999")
            removed = I.cleanup_ramdisk_artifacts(ramdisk_dir=tmp_dir, search_system_temps=False)
            self.assertEqual(removed, 1)
            self.assertTrue(os.path.isdir(live))
            self.assertFalse(os.path.isdir(dead))

    def test_cleanup_ramdisk_artifacts(self) -> None:
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_target = tempfile.mkdtemp(prefix="code-parser.test-", dir=tmp_dir)
            self.assertTrue(os.path.isdir(test_target))
            removed = I.cleanup_ramdisk_artifacts(ramdisk_dir=tmp_dir, search_system_temps=False)
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.isdir(test_target))

    def test_begin_skips_delete_when_no_prior_marks_exist(self) -> None:
        from unittest.mock import patch

        db = seeded()
        with patch.object(db, "delete_rows", wraps=db.delete_rows) as spy_delete:
            I.begin(db, 1, 10, TABLES)
        spy_delete.assert_not_called()
        self.assertEqual(db.count_rows("m_ingest_watermark"), len(I.marked_tables(TABLES)))

    def test_begin_retries_on_deadlock(self) -> None:
        from unittest.mock import patch
        from db_engine import DeadlockError

        db = seeded()
        orig_insert_rows = db.insert_rows
        attempts = {"watermark": 0}

        def flaky_insert_rows(table, *args, **kwargs):
            if table == "m_ingest_watermark":
                attempts["watermark"] += 1
                if attempts["watermark"] == 1:
                    raise DeadlockError("1213 (40001): Deadlock found when trying to get lock")
            return orig_insert_rows(table, *args, **kwargs)

        with patch.object(db, "insert_rows", side_effect=flaky_insert_rows), patch("core.ingest_state.time.sleep"):
            marks = I.begin(db, 1, 10, TABLES, base_commit_id=5, worker_id="w1")
        self.assertEqual(attempts["watermark"], 2)
        self.assertEqual(marks["m_commit"], 11)
        self.assertEqual(db.count_rows("m_ingest_watermark"), len(I.marked_tables(TABLES)))
        self.assertEqual(I.get_status(db, 1, 10), I.INGEST_PARSING)


class _OnSQLite:
    """Runs the scenarios of the base class against an in-memory SQLiteDB instead of the MockDB."""

    def setUp(self) -> None:
        BACKEND[0] = SQLiteMemoryDB

    def tearDown(self) -> None:
        BACKEND[0] = MemoryDB


class TestRollbackPlanSQLite(_OnSQLite, TestRollbackPlan):
    pass


class TestIngestStateDatabaseSQLite(_OnSQLite, TestIngestStateDatabase):
    pass


if __name__ == "__main__":
    unittest.main()

