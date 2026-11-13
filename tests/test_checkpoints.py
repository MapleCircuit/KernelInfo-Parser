"""tests/test_checkpoints.py - Checkpoints stored as deltas (core/checkpoints.py), on an in-memory SQLiteDB
(the module only uses db_engine query specs, so every backend runs the same operations)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import DBLayout as D
from core import checkpoints as cp
from db_engine import SQLiteDB


class _Conn(SQLiteDB):
    """In-memory SQLite database with the checkpoint tables and the `m_checkpoint_tree` view."""

    def __init__(self) -> None:
        super().__init__(":memory:", foreign_keys=False)
        self.create_table([D.m_ingest_state, D.m_bridge_checkpoint, D.m_checkpoint_base, D.m_parse_task, D.m_git_ref])
        errors = self.ensure_views(["m_checkpoint_tree"])
        assert errors["m_checkpoint_tree"] is None, errors

    def tree(self, commit_id: int) -> dict[int, int]:
        """The version's tree through the m_checkpoint_tree view."""
        return dict(self.select_rows("m_checkpoint_tree", ["fnid", "blob_id"], {"commit_id": commit_id}))

    def stored(self, commit_id: int) -> dict[int, int]:
        return dict(self.select_rows("m_bridge_checkpoint", ["fnid", "blob_id"], {"commit_id": commit_id}))


BASE = {f: 1000 + f for f in range(1, 101)}             # 100 files
STABLE = {**BASE, 5: 5005, 6: 5006, 200: 5200}          # 2 changed, 1 added...
del STABLE[7]                                           # ...and 1 removed
NEW_RELEASE = {f: 9000 + f for f in range(1, 101)}      # every file changed


class TestCheckpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _Conn()
        for cid, tree in ((10, BASE), (11, STABLE), (12, NEW_RELEASE)):
            self.db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(cid, f, b) for f, b in tree.items()])
            self.db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"], [(1, cid, 2, 0)])

    def test_should_compact(self) -> None:
        self.assertTrue(cp.should_compact(3, 1, 100, 0.3))
        self.assertFalse(cp.should_compact(40, 0, 100, 0.3), "too different: stays full")
        self.assertFalse(cp.should_compact(0, 0, 100, 0.0), "0 turns compaction off")
        self.assertFalse(cp.should_compact(0, 0, 0, 0.3), "an empty tree has nothing to save")

    def test_compact_keeps_the_difference(self) -> None:
        res = cp.compact(self.db, 1, 11, current=12, ratio=0.3)
        self.assertEqual((res["base"], res["changed"], res["removed"], res["total"]), (10, 3, 1, 100))
        self.assertEqual(self.db.stored(11), {5: 5005, 6: 5006, 200: 5200, 7: 0}, "changed, added, and a tombstone")
        self.assertEqual(self.db.tree(11), STABLE, "the view returns the full tree")
        self.assertEqual(self.db.tree(10), BASE)
        self.assertEqual(dict(cp.tree_rows(self.db, 11)), STABLE)
        self.assertEqual(cp.base_of(self.db, 11), 10)
        self.assertEqual(cp.dependents(self.db, 10), [11])

    def test_a_base_is_never_compacted(self) -> None:
        cp.compact(self.db, 1, 11, current=12, ratio=0.3)
        self.db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(9, f, b) for f, b in BASE.items()])
        self.db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"], [(1, 9, 2, 0)])
        self.assertIsNone(cp.compact(self.db, 1, 10, current=12, ratio=0.3), "11 rests on 10")

    def test_a_distant_version_stays_full(self) -> None:
        self.assertIsNone(cp.compact(self.db, 1, 12, current=13, ratio=0.3))
        self.assertEqual(self.db.stored(12), NEW_RELEASE)

    def test_no_base_before_the_first_version(self) -> None:
        self.assertIsNone(cp.compact(self.db, 1, 10, current=11, ratio=0.3))

    def test_the_current_version_is_never_the_base(self) -> None:
        # 11 is newer than 10 but is the version just ingested: 10 has no earlier full checkpoint
        self.assertIsNone(cp.compact(self.db, 1, 10, current=11, ratio=0.3))
        self.assertEqual(self.db.stored(10), BASE)

    def test_expand_restores_the_full_rows(self) -> None:
        cp.compact(self.db, 1, 11, current=12, ratio=0.3)
        restored = cp.expand(self.db, 11)
        self.assertEqual(restored, 100 - 3)
        self.assertEqual(self.db.stored(11), STABLE)
        self.assertEqual(self.db.tree(11), STABLE)
        self.assertIsNone(cp.base_of(self.db, 11))
        self.assertEqual(cp.expand(self.db, 11), 0, "a full checkpoint is left alone")

    def test_expand_dependents(self) -> None:
        cp.compact(self.db, 1, 11, current=12, ratio=0.3)
        self.assertEqual(cp.expand_dependents(self.db, 10), [11])
        self.assertEqual(self.db.stored(11), STABLE)

    def test_compact_and_expand_ignore_rows_already_stored(self) -> None:
        """A tombstone or restored row that is already there (an interrupted earlier run) is not an error."""
        self.db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(11, 7, 0)])
        self.assertIsNotNone(cp.compact(self.db, 1, 11, current=12, ratio=0.3))
        self.assertEqual(self.db.tree(11), STABLE)
        self.db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(11, 1, BASE[1])])
        cp.expand(self.db, 11)
        self.assertEqual(self.db.stored(11), STABLE)

    def test_is_checkpoint_pinned(self) -> None:
        self.assertFalse(cp.is_checkpoint_pinned(self.db, 10))
        self.assertFalse(cp.is_checkpoint_pinned(self.db, 0))
        # a running ingestion resting on the checkpoint
        self.db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at", "base_commit_id"],
                            [(2, 13, 1, 0, 10)])
        self.assertTrue(cp.is_checkpoint_pinned(self.db, 10))
        self.assertFalse(cp.is_checkpoint_pinned(self.db, 11))
        # a pending task naming the checkpoint as its base commit
        self.db.insert_rows("m_parse_task", ["network_id", "chain_name", "chain_kind", "ref_name", "base_commit_id", "status"],
                            [(1, "c", "k", "v1", 11, 0)])
        self.assertTrue(cp.is_checkpoint_pinned(self.db, 11))
        # a pending task whose base ref points at the checkpoint
        self.db.insert_rows("m_git_ref", ["repo_id", "ref_type", "ref_name", "commit_id"], [(1, 1, "v0", 12)])
        self.db.insert_rows("m_parse_task", ["network_id", "chain_name", "chain_kind", "ref_name", "base_ref_name", "status"],
                            [(1, "c", "k", "v2", "v0", 1)])
        self.assertTrue(cp.is_checkpoint_pinned(self.db, 12))
        # a finished task pins nothing
        self.db.update_rows("m_parse_task", {"status": 3}, {"ref_name": "v2"})
        self.assertFalse(cp.is_checkpoint_pinned(self.db, 12))


if __name__ == "__main__":
    unittest.main()
