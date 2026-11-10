"""tests/test_checkpoints.py - Checkpoints stored as deltas (core/checkpoints.py), on an in-memory SQLite database
(the module's statements are portable SQL; MariaDB runs the same ones)."""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import checkpoints as cp


class _Cursor:
    def __init__(self, cur: sqlite3.Cursor) -> None:
        self.cur = cur

    def execute(self, sql: str, params=()) -> None:
        converted = sql.replace("%s", "?").replace("INSERT IGNORE INTO", "INSERT OR IGNORE INTO")
        self.cur.execute(converted, tuple(params))

    def query(self, sql: str, params=(), dictionary: bool = True) -> list:
        self.execute(sql, params)
        return self.fetchall()

    def query_one(self, sql: str, params=(), dictionary: bool = True):
        self.execute(sql, params)
        return self.fetchone()

    @property
    def rowcount(self) -> int:
        return self.cur.rowcount

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def close(self) -> None:
        self.cur.close()


class _Conn:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE m_ingest_state (network_id INT, commit_id INT, status INT, updated_at INT, PRIMARY KEY (network_id, commit_id));
            CREATE TABLE m_bridge_checkpoint (commit_id INT, fnid INT, blob_id INT, PRIMARY KEY (commit_id, fnid));
            CREATE TABLE m_checkpoint_base (commit_id INT PRIMARY KEY, base_commit_id INT, delta_rows INT);
            """
        )
        self.db.execute(cp.TREE_VIEW_SQL.replace("CREATE OR REPLACE VIEW", "CREATE VIEW").rstrip(";"))

    def query(self, sql: str, params=(), dictionary: bool = True) -> list:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def query_one(self, sql: str, params=(), dictionary: bool = True):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur.fetchone()

    def execute(self, sql: str, params=()) -> int:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur.rowcount

    def executemany(self, sql: str, params_seq) -> int:
        cur = self.cursor()
        for p in params_seq:
            cur.execute(sql, p)
        return len(params_seq)

    def cursor(self) -> _Cursor:
        return _Cursor(self.db.cursor())

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    def tree(self, commit_id: int) -> dict[int, int]:
        """The version's tree through the m_checkpoint_tree view."""
        return dict(self.db.execute("SELECT fnid, blob_id FROM m_checkpoint_tree WHERE commit_id = ?", (commit_id,)))

    def stored(self, commit_id: int) -> dict[int, int]:
        return dict(self.db.execute("SELECT fnid, blob_id FROM m_bridge_checkpoint WHERE commit_id = ?", (commit_id,)))


BASE = {f: 1000 + f for f in range(1, 101)}             # 100 files
STABLE = {**BASE, 5: 5005, 6: 5006, 200: 5200}          # 2 changed, 1 added...
del STABLE[7]                                           # ...and 1 removed
NEW_RELEASE = {f: 9000 + f for f in range(1, 101)}      # every file changed


class TestCheckpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _Conn()
        for cid, tree in ((10, BASE), (11, STABLE), (12, NEW_RELEASE)):
            self.db.db.executemany("INSERT INTO m_bridge_checkpoint VALUES (?, ?, ?)", [(cid, f, b) for f, b in tree.items()])
            self.db.db.execute("INSERT INTO m_ingest_state VALUES (1, ?, 2, 0)", (cid,))
        self.db.commit()

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
        with self.db.db:
            self.assertEqual(dict(cp.tree_rows(self.db, 11)), STABLE)
            self.assertEqual(cp.base_of(self.db, 11), 10)
            self.assertEqual(cp.dependents(self.db, 10), [11])

    def test_a_base_is_never_compacted(self) -> None:
        cp.compact(self.db, 1, 11, current=12, ratio=0.3)
        self.db.db.execute("INSERT INTO m_bridge_checkpoint SELECT 9, fnid, blob_id FROM m_bridge_checkpoint WHERE commit_id = 10")
        self.db.db.execute("INSERT INTO m_ingest_state VALUES (1, 9, 2, 0)")
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
        with self.db.db:
            self.assertIsNone(cp.base_of(self.db, 11))
        self.assertEqual(cp.expand(self.db, 11), 0, "a full checkpoint is left alone")

    def test_expand_dependents(self) -> None:
        cp.compact(self.db, 1, 11, current=12, ratio=0.3)
        self.assertEqual(cp.expand_dependents(self.db, 10), [11])
        self.assertEqual(self.db.stored(11), STABLE)

    def test_insert_ignore_in_compact_and_expand_sql(self) -> None:
        """Verify compact and expand use INSERT IGNORE INTO m_bridge_checkpoint."""
        import inspect
        compact_src = inspect.getsource(cp.compact)
        expand_src = inspect.getsource(cp.expand)
        self.assertIn("INSERT IGNORE INTO m_bridge_checkpoint", compact_src)
        self.assertIn("INSERT IGNORE INTO m_bridge_checkpoint", expand_src)


if __name__ == "__main__":
    unittest.main()
