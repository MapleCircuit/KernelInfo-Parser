"""tests/test_te_chunk_dedup.py - Unit tests for TableEngine cross-chunk deduplication and DB fork safety."""
import unittest
from core.globalstuff import G
from core.TableHandling import Table
from db_engine.mock_db import MockDB
from table_engine.te_direct_db import TEDirectDB
from table_engine.te_cached_db import TECachedDB


class TestTableEngineChunkDedup(unittest.TestCase):
    """Test cross-chunk deduplication for no_duplicate and hash tables across commit cycles."""

    def setUp(self) -> None:
        MockDB.reset()
        self.mock_db = MockDB()

        self.t_nodup = Table(
            table_id=101,
            table_name="test_nodup",
            columns=(
                ("id", "INT", "NOT NULL"),
                ("name", "VARCHAR(255)", "NOT NULL"),
            ),
            primary=("id",),
            no_duplicate=True,
            te_cached=True,
        )

        self.t_hash = Table(
            table_id=102,
            table_name="test_hash",
            columns=(
                ("hash", "BINARY(32)", "NOT NULL"),
                ("content", "TEXT", "NOT NULL"),
            ),
            primary=("hash",),
            no_duplicate=False,
            te_cached=True,
        )

        self.mock_db.create_table((self.t_nodup, self.t_hash))

    def test_te_direct_db_cross_chunk_nodup(self) -> None:
        """TEDirectDB preserves sequence ID and skips duplicate inserts across chunk commits."""
        te = TEDirectDB()
        te.start((self.t_nodup, self.t_hash), lambda: self.mock_db)

        # Chunk 1
        res1 = te.set(self.t_nodup.table_id, (None, "drivers/net/ethernet.c"))
        self.assertEqual(res1[0], 1)
        te.commit(self.t_nodup.table_id)

        # Verify staged queue was cleared
        self.assertEqual(len(te.queued_set[self.t_nodup.table_id]), 0)

        # Chunk 2: Same path
        res2 = te.set(self.t_nodup.table_id, (None, "drivers/net/ethernet.c"))
        self.assertEqual(res2[0], 1, "Should return existing sequence ID from _committed_nodup_keys")
        te.commit(self.t_nodup.table_id)

        # Verify DB store contains exactly 1 row
        db_rows = MockDB._global_store["test_nodup"]
        self.assertEqual(len(db_rows), 1)

    def test_te_direct_db_cross_chunk_hash(self) -> None:
        """TEDirectDB skips duplicate hash inserts across chunk commits."""
        te = TEDirectDB()
        te.start((self.t_nodup, self.t_hash), lambda: self.mock_db)

        h = b"\xaa" * 32
        # Chunk 1
        te.set(self.t_hash.table_id, (h, "inline void foo() {}"))
        te.commit(self.t_hash.table_id)

        # Chunk 2: Same hash
        te.set(self.t_hash.table_id, (h, "inline void foo() {}"))
        te.commit(self.t_hash.table_id)

        # Verify DB store contains exactly 1 row
        db_rows = MockDB._global_store["test_hash"]
        self.assertEqual(len(db_rows), 1)

    def test_te_cached_db_cross_chunk_nodup_and_hash(self) -> None:
        """TECachedDB correctly tracks committed keys across chunk commits."""
        te = TECachedDB(hugepages="off")
        te.start((self.t_nodup, self.t_hash), lambda: self.mock_db)

        # Chunk 1
        r1 = te.set(self.t_nodup.table_id, (None, "fs/ext4/super.c"))
        h1 = b"\xbb" * 32
        te.set(self.t_hash.table_id, (h1, "int bar();"))
        te.commit_all()

        # Chunk 2
        r2 = te.set(self.t_nodup.table_id, (None, "fs/ext4/super.c"))
        self.assertEqual(r2[0], r1[0])
        te.set(self.t_hash.table_id, (h1, "int bar();"))
        te.commit_all()

        self.assertEqual(len(MockDB._global_store["test_nodup"]), 1)
        self.assertEqual(len(MockDB._global_store["test_hash"]), 1)


if __name__ == "__main__":
    unittest.main()
