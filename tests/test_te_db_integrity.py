"""tests/test_te_db_integrity.py - TableEngine and Database Backend Integrity Test Suite.

Rigorously verifies the integrity, data contracts, state management, and interaction
between the Database Engine (db_engine) and Table Engine (TEDirectDB / TECachedDB).
Uses strictly isolated fake tables (_test_fake_*) to ensure zero interference with
existing production databases.
"""
from __future__ import annotations

import os
import sys
import argparse
import unittest
from typing import Any

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.globalstuff import G, COLOR, SafeDataType, JoinsType
from core.TableHandling import Table
from table_engine import TEDirectDB, TECachedDB, get_table_engine
from table_engine.te_direct_db import compute_ast_hash
from db_engine import MockDB, MariaDB, get_db_engine


# =============================================================================
# Isolated Synthetic / Fake Table Definitions
# =============================================================================

FAKE_TBL_SIMPLE_ID = 100
FAKE_TBL_NODUP_ID = 101
FAKE_TBL_PARENT_ID = 102
FAKE_TBL_CHILD_ID = 103
FAKE_TBL_CACHED_ID = 104
FAKE_TBL_CACHED_NODUP_ID = 105
FAKE_TBL_HROOT_ID = 106
FAKE_TBL_HASH_ID = 107

# 1. Simple auto-increment table (no_duplicate=False, te_cached=False)
fake_tbl_simple = Table(
    table_id=FAKE_TBL_SIMPLE_ID,
    table_name="_test_fake_simple",
    columns=(
        ("id", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("name", "VARCHAR(64)", "NOT NULL"),
        ("value", "INT", "NOT NULL"),
    ),
    primary=("id",),
    foreign=None,
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# 2. In-memory deduplication table (no_duplicate=True, te_cached=False)
fake_tbl_nodup = Table(
    table_id=FAKE_TBL_NODUP_ID,
    table_name="_test_fake_nodup",
    columns=(
        ("nid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("symbol", "VARCHAR(128)", "NOT NULL"),
    ),
    primary=("nid",),
    foreign=None,
    initial_insert=None,
    no_duplicate=True,
    te_cached=False,
    hashing_table=False,
)

# 3. Relational parent table for view tests
fake_tbl_parent = Table(
    table_id=FAKE_TBL_PARENT_ID,
    table_name="_test_fake_parent",
    columns=(
        ("pid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("title", "VARCHAR(64)", "NOT NULL"),
    ),
    primary=("pid",),
    foreign=None,
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# 4. Relational child table referencing parent for view tests
fake_tbl_child = Table(
    table_id=FAKE_TBL_CHILD_ID,
    table_name="_test_fake_child",
    columns=(
        ("cid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("pid", "INT", "NOT NULL"),
        ("tag", "VARCHAR(32)", "NOT NULL"),
    ),
    primary=("cid",),
    foreign=(("pid", "_test_fake_parent", "pid"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table=False,
)

# 5. In-Memory Cached table (no_duplicate=False, te_cached=True)
fake_tbl_cached = Table(
    table_id=FAKE_TBL_CACHED_ID,
    table_name="_test_fake_cached",
    columns=(
        ("cid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("key_name", "VARCHAR(64)", "NOT NULL"),
        ("val", "INT", "NOT NULL"),
    ),
    primary=("cid",),
    foreign=None,
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    hashing_table=False,
)

# 6. In-Memory Cached deduplicated table (no_duplicate=True, te_cached=True)
fake_tbl_cached_nodup = Table(
    table_id=FAKE_TBL_CACHED_NODUP_ID,
    table_name="_test_fake_cached_nodup",
    columns=(
        ("nid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("name", "VARCHAR(64)", "NOT NULL"),
    ),
    primary=("nid",),
    foreign=None,
    initial_insert=None,
    no_duplicate=True,
    te_cached=True,
    hashing_table=False,
)

# 7. Relational root table with hashing_table configured
fake_tbl_hroot = Table(
    table_id=FAKE_TBL_HROOT_ID,
    table_name="_test_fake_hroot",
    columns=(
        ("hid", "INT", "NOT NULL", "AUTO_INCREMENT"),
        ("name", "VARCHAR(64)", "NOT NULL"),
        ("val", "INT", "NOT NULL"),
    ),
    primary=("hid",),
    foreign=None,
    initial_insert=None,
    no_duplicate=False,
    te_cached=False,
    hashing_table="_test_fake_hash",
)

# 8. Structural hash table associated with fake_tbl_hroot
fake_tbl_hash = Table(
    table_id=FAKE_TBL_HASH_ID,
    table_name="_test_fake_hash",
    columns=(
        ("hash", "BINARY(32)", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("hash",),
    foreign=(("ast_id", "_test_fake_hroot", "hid"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    hashing_table=False,
)

# 9. In-Memory Cached bridge table (vid, fnid) -> fid
FAKE_TBL_CACHED_BRIDGE_ID = 108
fake_tbl_cached_bridge = Table(
    table_id=FAKE_TBL_CACHED_BRIDGE_ID,
    table_name="_test_fake_cached_bridge",
    columns=(
        ("vid", "INT", "NOT NULL"),
        ("fnid", "INT", "NOT NULL"),
        ("fid", "INT", "NOT NULL"),
    ),
    primary=("vid", "fnid"),
    foreign=None,
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
    version_scoped=True,
    hashing_table=False,
)

# 10. In-Memory Partial Cached table (only column "hash" / index 0 cached)
FAKE_TBL_PARTIAL_CACHED_ID = 109
fake_tbl_partial_cached = Table(
    table_id=FAKE_TBL_PARTIAL_CACHED_ID,
    table_name="_test_fake_partial_cached",
    columns=(
        ("hash", "BINARY(32)", "NOT NULL"),
        ("code", "LONGTEXT", "NOT NULL"),
    ),
    primary=("hash",),
    foreign=None,
    initial_insert=((b"\x00" * 32, ""),),
    no_duplicate=False,
    te_cached=("hash",),
    hashing_table=False,
)

ALL_FAKE_TABLES = (
    fake_tbl_simple,
    fake_tbl_nodup,
    fake_tbl_parent,
    fake_tbl_child,
    fake_tbl_cached,
    fake_tbl_cached_nodup,
    fake_tbl_hroot,
    fake_tbl_hash,
    fake_tbl_cached_bridge,
    fake_tbl_partial_cached,
)
FAKE_TABLES_DICT = {tbl.table_id: tbl for tbl in ALL_FAKE_TABLES}

# Global test configuration configured via CLI or defaults
CONFIGURED_DB_ENGINE_CLS = MockDB
CONFIGURED_TE_ENGINE_CLS = TECachedDB


# =============================================================================
# Test Suite 1: Database Engine Driver Integrity
# =============================================================================

class TestDBEngineIntegrity(unittest.TestCase):
    """Test lower-level database backend driver DDL, DML, joins, and transaction operations."""

    def setUp(self) -> None:
        """Create fresh DB instance and create fake tables."""
        self.db = CONFIGURED_DB_ENGINE_CLS()
        self.addCleanup(self._cleanup_db)
        self.db.drop_table(ALL_FAKE_TABLES)
        self.db.create_table(ALL_FAKE_TABLES)

    def _cleanup_db(self) -> None:
        """Safely drop fake tables and close DB connection."""
        try:
            self.db.drop_table(ALL_FAKE_TABLES)
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass

    def test_ddl_create_test_drop_tables(self) -> None:
        """Verify create_table, test_tables, and drop_table lifecycle."""
        missing = self.db.test_tables(ALL_FAKE_TABLES)
        self.assertIsNone(missing, f"Expected all fake tables to exist, but missing: {missing}")

        self.db.drop_table(ALL_FAKE_TABLES)
        missing_after = self.db.test_tables(ALL_FAKE_TABLES)
        self.assertIsNotNone(missing_after)
        self.assertEqual(len(missing_after), len(ALL_FAKE_TABLES))

        self.db.create_table(ALL_FAKE_TABLES)

    def test_index_lifecycle(self) -> None:
        """Verify dynamic create_index, index_exists, and remove_index operations."""
        index_name = "idx_test_simple_name"

        self.db.create_index(index_name, fake_tbl_simple, ((fake_tbl_simple.table_id, 1),))

        if not isinstance(self.db, MockDB):
            self.assertTrue(self.db.index_exists(index_name, fake_tbl_simple))
            self.db.remove_index(index_name, fake_tbl_simple)
            self.assertFalse(self.db.index_exists(index_name, fake_tbl_simple))
        else:
            self.db.remove_index(index_name, fake_tbl_simple)

    def test_parallel_index_lifecycle(self) -> None:
        """Verify concurrent create_indexes and remove_indexes operations."""
        idx_specs = (
            ("idx_par_simple_1", fake_tbl_simple, ((fake_tbl_simple.table_id, 1),)),
            ("idx_par_simple_2", fake_tbl_simple, ((fake_tbl_simple.table_id, 2),)),
            ("idx_par_parent", fake_tbl_parent, ((fake_tbl_parent.table_id, 1),)),
        )
        remove_specs = (
            ("idx_par_simple_1", fake_tbl_simple),
            ("idx_par_simple_2", fake_tbl_simple),
            ("idx_par_parent", fake_tbl_parent),
        )

        self.db.create_indexes(idx_specs)

        if not isinstance(self.db, MockDB):
            for name, tbl, _ in idx_specs:
                self.assertTrue(self.db.index_exists(name, tbl))

            self.db.remove_indexes(remove_specs)

            for name, tbl in remove_specs:
                self.assertFalse(self.db.index_exists(name, tbl))
        else:
            self.db.remove_indexes(remove_specs)

    def test_get_next_id_empty_and_populated(self) -> None:
        """Verify get_next_id returns 1 on empty table and MAX(pk) + 1 on populated table."""
        next_id = self.db.get_next_id(fake_tbl_simple)
        self.assertEqual(next_id, 1)

        self.db.insert(fake_tbl_simple, ((1, "row_1", 100), (5, "row_5", 500)))
        next_id_after = self.db.get_next_id(fake_tbl_simple)
        self.assertEqual(next_id_after, 6)

    def test_insert_and_select_with_wildcards(self) -> None:
        """Verify batch insert and single-row select with exact match and wildcard None."""
        rows = (
            (1, "alpha", 10),
            (2, "beta", 20),
            (3, "gamma", 30),
        )
        self.db.insert(fake_tbl_simple, rows)

        result = self.db.select(fake_tbl_simple, (2, "beta", 20))
        self.assertEqual(result, (2, "beta", 20))

        result_wildcard = self.db.select(fake_tbl_simple, (None, "gamma", None))
        self.assertEqual(result_wildcard, (3, "gamma", 30))

        result_val = self.db.select(fake_tbl_simple, (None, None, 10))
        self.assertEqual(result_val, (1, "alpha", 10))

        result_none = self.db.select(fake_tbl_simple, (None, "non_existent", None))
        self.assertIsNone(result_none)

    def test_update_upsert(self) -> None:
        """Verify update/upsert executes ON DUPLICATE KEY UPDATE behavior."""
        self.db.insert(fake_tbl_simple, ((1, "initial", 10),))

        self.db.update(fake_tbl_simple, ((1, "modified", 999),))
        updated = self.db.select(fake_tbl_simple, (1, None, None))
        self.assertEqual(updated, (1, "modified", 999))

    def test_view_select_single_and_multiple(self) -> None:
        """Verify joined view queries across parent and child fake tables."""
        self.db.insert(fake_tbl_parent, ((1, "ParentAlpha"), (2, "ParentBeta")))
        self.db.insert(fake_tbl_child, (
            (10, 1, "ChildAlpha1"),
            (11, 1, "ChildAlpha2"),
            (12, 2, "ChildBeta1"),
        ))

        joins: JoinsType = (
            (fake_tbl_parent.pid, fake_tbl_child.pid, 1),
        )

        res_single = self.db.view_select(
            FAKE_TABLES_DICT,
            joins,
            (None, "ParentAlpha", None, None, "ChildAlpha2"),
        )
        self.assertEqual(res_single, (1, "ParentAlpha", 11, 1, "ChildAlpha2"))

        res_multi = self.db.view_select_multiple(
            FAKE_TABLES_DICT,
            joins,
            (1, None, None, None, None),
        )
        self.assertEqual(len(res_multi), 2)
        self.assertIn((1, "ParentAlpha", 10, 1, "ChildAlpha1"), res_multi)
        self.assertIn((1, "ParentAlpha", 11, 1, "ChildAlpha2"), res_multi)

    def test_commit_tables_parallel(self) -> None:
        """Verify commit_tables_parallel concurrently flushes inserts and updates."""
        tables_data = [
            (
                fake_tbl_simple,
                [(1, "p_simple", 100), (2, "p_simple2", 200)],
                [(1, "p_simple_upd", 150)],
            ),
            (
                fake_tbl_parent,
                [(10, "p_parent10"), (20, "p_parent20")],
                [],
            ),
        ]
        self.db.commit_tables_parallel(tables_data)

        row1 = self.db.select(fake_tbl_simple, (1, None, None))
        self.assertEqual(row1, (1, "p_simple_upd", 150))
        row2 = self.db.select(fake_tbl_simple, (2, None, None))
        self.assertEqual(row2, (2, "p_simple2", 200))

        p1 = self.db.select(fake_tbl_parent, (10, None))
        self.assertEqual(p1, (10, "p_parent10"))

    def test_verify_relational_integrity(self) -> None:
        """Verify verify_relational_integrity accurately detects valid and orphaned foreign keys."""
        # Clean state: insert valid parent and child
        self.db.insert(fake_tbl_parent, ((1, "Parent_1"),))
        self.db.insert(fake_tbl_child, ((1, 1, "Child_1"),))

        violations = self.db.verify_relational_integrity([fake_tbl_parent, fake_tbl_child])
        self.assertEqual(len(violations), 0)

        # Inject orphan child (referencing parent_id 999 which does not exist)
        self.db.insert(fake_tbl_child, ((2, 999, "Child_Orphan"),))
        violations_orphan = self.db.verify_relational_integrity([fake_tbl_parent, fake_tbl_child])
        self.assertGreater(len(violations_orphan), 0)
        self.assertIn("_test_fake_child.pid -> _test_fake_parent.pid", violations_orphan)
        self.assertEqual(violations_orphan["_test_fake_child.pid -> _test_fake_parent.pid"], 1)

    def test_select_preload_selective_columns(self) -> None:
        """Verify select_preload queries only requested columns and excludes un-cached columns."""
        import hashlib
        h1 = hashlib.sha256(b"code_1").digest()
        h2 = hashlib.sha256(b"code_2").digest()
        self.db.insert(fake_tbl_partial_cached, (
            (h1, "long_code_payload_1"),
            (h2, "long_code_payload_2"),
        ))

        # Query only column 0 (hash)
        rows_hash_only = self.db.select_preload(fake_tbl_partial_cached, cached_columns=(0,))
        self.assertEqual(len(rows_hash_only), 3)  # 1 initial_insert seed + 2 inserted
        # Each row should only contain 1 element (the hash), not the code payload
        for row in rows_hash_only:
            self.assertEqual(len(row), 1)
        self.assertIn((h1,), rows_hash_only)
        self.assertIn((h2,), rows_hash_only)

        # Query all columns (None or full range)
        rows_full = self.db.select_preload(fake_tbl_partial_cached, cached_columns=None)
        self.assertEqual(len(rows_full), 3)
        for row in rows_full:
            self.assertEqual(len(row), 2)
        self.assertIn((h1, "long_code_payload_1"), rows_full)
        self.assertIn((h2, "long_code_payload_2"), rows_full)


    def test_select_preload_version_filtering(self) -> None:
        """Verify select_preload filters out historical rows when min_vid is specified."""
        self.db.insert(fake_tbl_cached_bridge, (
            (1, 100, 10),
            (2, 100, 20),
            (3, 100, 30),
        ))

        # Without min_vid, returns all rows
        rows_all = self.db.select_preload(fake_tbl_cached_bridge, min_vid=None)
        self.assertEqual(len(rows_all), 3)

        # With min_vid=2, returns only rows with vid >= 2
        rows_v2 = self.db.select_preload(fake_tbl_cached_bridge, min_vid=2)
        self.assertEqual(len(rows_v2), 2)
        vids = [r[0] for r in rows_v2]
        self.assertNotIn(1, vids)
        self.assertIn(2, vids)
        self.assertIn(3, vids)




# =============================================================================
# Test Suite 2: Table Engine Base Integrity (TEDirectDB / Common)
# =============================================================================

class TestTableEngineIntegrity(unittest.TestCase):
    """Test TableEngine staging buffers, deduplication, sequences, and commit."""

    def setUp(self) -> None:
        """Set up fresh database and start TableEngine with fake tables."""
        self.db = CONFIGURED_DB_ENGINE_CLS()
        self.db.drop_table(ALL_FAKE_TABLES)
        self.db.create_table(ALL_FAKE_TABLES)
        self.te = CONFIGURED_TE_ENGINE_CLS()
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        """Clean up TableEngine and database resources."""
        try:
            self.te.close()
        except Exception:
            pass
        try:
            self.db.drop_table(ALL_FAKE_TABLES)
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass

    def test_engine_lifecycle_and_start(self) -> None:
        """Verify TableEngine initialization and table registration."""
        self.assertEqual(len(self.te.tables), len(ALL_FAKE_TABLES))
        for tbl in ALL_FAKE_TABLES:
            self.assertIn(tbl.table_id, self.te.tables)
            self.assertIn(tbl.table_id, self.te.queued_set)
            self.assertIn(tbl.table_id, self.te.queued_update)
            self.assertEqual(self.te.next_id[tbl.table_id], 1)

    def test_set_auto_increment_and_explicit_pk(self) -> None:
        """Verify set() auto-assigns monotonic IDs for None PK and preserves explicit PK."""
        row1 = self.te.set(fake_tbl_simple.table_id, (None, "alpha", 100))
        self.assertEqual(row1, (1, "alpha", 100))
        self.assertEqual(self.te.next_id[fake_tbl_simple.table_id], 2)
        self.assertIn(1, self.te.queued_set[fake_tbl_simple.table_id])

        row2 = self.te.set(fake_tbl_simple.table_id, (None, "beta", 200))
        self.assertEqual(row2, (2, "beta", 200))
        self.assertEqual(self.te.next_id[fake_tbl_simple.table_id], 3)
        self.assertIn(2, self.te.queued_set[fake_tbl_simple.table_id])

        row3 = self.te.set(fake_tbl_simple.table_id, (50, "custom_pk", 500))
        self.assertEqual(row3, (50, "custom_pk", 500))
        self.assertIn(50, self.te.queued_set[fake_tbl_simple.table_id])
        self.assertEqual(self.te.queued_set[fake_tbl_simple.table_id][50], (50, "custom_pk", 500))

    def test_set_no_duplicate_deduplication(self) -> None:
        """Verify no_duplicate=True tables deduplicate rows in memory."""
        res1 = self.te.set(fake_tbl_nodup.table_id, (None, "unique_symbol_A"))
        self.assertEqual(res1, (1, "unique_symbol_A"))
        self.assertEqual(self.te.next_id[fake_tbl_nodup.table_id], 2)

        res1_dup = self.te.set(fake_tbl_nodup.table_id, (None, "unique_symbol_A"))
        self.assertEqual(res1_dup, (1, "unique_symbol_A"))
        self.assertEqual(self.te.next_id[fake_tbl_nodup.table_id], 2)

        res2 = self.te.set(fake_tbl_nodup.table_id, (None, "unique_symbol_B"))
        self.assertEqual(res2, (2, "unique_symbol_B"))
        self.assertEqual(self.te.next_id[fake_tbl_nodup.table_id], 3)

    def test_get_emptiness_guard_and_fallback(self) -> None:
        """Verify get() emptiness guard avoids unnecessary DB queries and queries DB when populated."""
        res_empty = self.te.get(fake_tbl_simple.table_id, (None, "not_there", None))
        self.assertIsNone(res_empty)

        self.te.set(fake_tbl_simple.table_id, (None, "committed_item", 42))
        self.te.commit(fake_tbl_simple.table_id)

        res_found = self.te.get(fake_tbl_simple.table_id, (None, "committed_item", None))
        self.assertEqual(res_found, (1, "committed_item", 42))

    def test_get_staged_wildcard_lookups_before_commit(self) -> None:
        """Verify get() resolves partial/wildcard queries against uncommitted staged rows in queued_set."""
        # 1. Staged row in non-no_duplicate table
        self.te.set(fake_tbl_simple.table_id, (None, "staged_wildcard_target", 999))
        # Query by column 1 with wildcard on column 0 and column 2
        res_simple = self.te.get(fake_tbl_simple.table_id, (None, "staged_wildcard_target", None))
        self.assertIsNotNone(res_simple)
        self.assertEqual(res_simple, (1, "staged_wildcard_target", 999))

        # 2. Staged row in no_duplicate table
        self.te.set(fake_tbl_nodup.table_id, (None, "staged_nodup_target"))
        res_nodup = self.te.get(fake_tbl_nodup.table_id, (None, "staged_nodup_target"))
        self.assertIsNotNone(res_nodup)
        self.assertEqual(res_nodup, (1, "staged_nodup_target"))


    def test_update_staging(self) -> None:
        """Verify update() stages rows in queued_update buffer."""
        self.te.update(fake_tbl_simple.table_id, (1, "staged_update", 99))
        self.assertEqual(len(self.te.queued_update[fake_tbl_simple.table_id]), 1)
        self.assertEqual(
            self.te.queued_update[fake_tbl_simple.table_id][0],
            (1, "staged_update", 99),
        )

    def test_view_set_and_decomposition(self) -> None:
        """Verify view_set decomposes flat tuples across constituent tables into queued_set."""
        joins: JoinsType = (
            (fake_tbl_parent.pid, fake_tbl_child.pid, 1),
        )

        view_row = self.te.view_set(joins, (None, "P1", None, None, "C1"))
        self.assertEqual(view_row, (1, "P1", 1, 1, "C1"))

        self.assertIn(1, self.te.queued_set[fake_tbl_parent.table_id])
        self.assertEqual(self.te.queued_set[fake_tbl_parent.table_id][1], (1, "P1"))

        self.assertIn(1, self.te.queued_set[fake_tbl_child.table_id])
        self.assertEqual(self.te.queued_set[fake_tbl_child.table_id][1], (1, 1, "C1"))

        view_row_cached = self.te.view_set(joins, (None, "P1", None, None, "C1"))
        self.assertEqual(view_row_cached, (1, "P1", 1, 1, "C1"))

    def test_view_set_and_get_with_hashing_table(self) -> None:
        """Verify view_set automatically hashes into hashing_table and view_get uses hash lookup."""
        joins: JoinsType = ((fake_tbl_hroot.hid,),)

        # 1. view_set generates hash and stages into fake_tbl_hash
        view_row = self.te.view_set(joins, (None, "HashedASTNode", 42))
        self.assertEqual(view_row, (1, "HashedASTNode", 42))

        # Expected hash
        h = compute_ast_hash(joins, ("HashedASTNode", 42))
        self.assertIn(h, self.te.queued_set[fake_tbl_hash.table_id])
        self.assertEqual(self.te.queued_set[fake_tbl_hash.table_id][h], (h, 1))

        # 2. view_get retrieves matching row using the hash
        got_row = self.te.view_get(joins, (None, "HashedASTNode", 42))
        self.assertEqual(got_row, (1, "HashedASTNode", 42))

        # 3. view_get on non-existent hash returns None
        none_row = self.te.view_get(joins, (None, "NonExistentNode", 999))
        self.assertIsNone(none_row)

    def test_commit_single_table_with_nodup_transform(self) -> None:
        """Verify commit() reconstructs rows for no_duplicate tables before writing to DB."""
        self.te.set(fake_tbl_nodup.table_id, (None, "sym_1"))
        self.te.set(fake_tbl_nodup.table_id, (None, "sym_2"))

        self.te.commit(fake_tbl_nodup.table_id)

        self.assertEqual(len(self.te.queued_set[fake_tbl_nodup.table_id]), 0)

        row1 = self.db.select(fake_tbl_nodup, (1, None))
        row2 = self.db.select(fake_tbl_nodup, (2, None))
        self.assertEqual(row1, (1, "sym_1"))
        self.assertEqual(row2, (2, "sym_2"))

    def test_commit_batch_presorting_by_primary_key(self) -> None:
        """Verify commit() delivers insert payloads sorted strictly ascending by Primary Key."""
        import hashlib

        raw_keys = [f"key_{i}".encode("latin-1") for i in [9, 3, 7, 1, 5, 2, 8, 0, 4, 6]]
        hashes = [hashlib.sha256(k).digest() for k in raw_keys]

        inserted_batches = []
        orig_insert = self.db.insert

        def capture_insert(table, data):
            inserted_batches.append((table, data))
            return orig_insert(table, data)

        self.db.insert = capture_insert

        for h in hashes:
            self.te.set(fake_tbl_partial_cached.table_id, (h, f"code_{h.hex()[:8]}"))

        self.te.commit(fake_tbl_partial_cached.table_id)

        self.assertTrue(len(inserted_batches) > 0)
        table, payload = inserted_batches[0]
        self.assertEqual(table.table_id, fake_tbl_partial_cached.table_id)

        # Verify rows in payload are strictly sorted by PK (column 0)
        extracted_keys = [row[0] for row in payload]
        sorted_keys = sorted(hashes)
        self.assertEqual(extracted_keys, sorted_keys, "Payload rows were not pre-sorted by primary key!")


    def test_commit_all_parallel_flush(self) -> None:
        """Verify commit_all flushes all staged sets and updates across all tables."""
        self.te.set(fake_tbl_simple.table_id, (None, "init_simple", 10))
        self.te.update(fake_tbl_simple.table_id, (1, "init_simple_mod", 20))

        self.te.set(fake_tbl_nodup.table_id, (None, "nodup_sym"))

        self.te.set(fake_tbl_parent.table_id, (None, "parent_title"))

        self.te.commit_all()

        for tbl in ALL_FAKE_TABLES:
            self.assertEqual(len(self.te.queued_set[tbl.table_id]), 0)
            self.assertEqual(len(self.te.queued_update[tbl.table_id]), 0)

        s_row = self.db.select(fake_tbl_simple, (1, None, None))
        self.assertEqual(s_row, (1, "init_simple_mod", 20))

        n_row = self.db.select(fake_tbl_nodup, (1, None))
        self.assertEqual(n_row, (1, "nodup_sym"))

        p_row = self.db.select(fake_tbl_parent, (1, None))
        self.assertEqual(p_row, (1, "parent_title"))


# =============================================================================
# Test Suite 3: In-Memory Cached Table Engine Integrity (TECachedDB)
# =============================================================================

class TestTECachedDBIntegrity(unittest.TestCase):
    """Test TECachedDB preloading, in-memory multi-indexing, and real-time cache synchronization."""

    def setUp(self) -> None:
        """Initialize DB and TECachedDB with clean fake tables."""
        self.db = MockDB()
        self.db.drop_table(ALL_FAKE_TABLES)
        self.db.create_table(ALL_FAKE_TABLES)
        self.te = TECachedDB()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        """Tear down resources."""
        try:
            self.te.close()
        except Exception:
            pass
        try:
            self.db.drop_table(ALL_FAKE_TABLES)
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass

    def test_preload_existing_records_on_start(self) -> None:
        """Verify start() preloads all existing rows for tables with te_cached=True."""
        self.db.insert(fake_tbl_cached, (
            (1, "preloaded_alpha", 100),
            (2, "preloaded_beta", 200),
        ))
        self.db.insert(fake_tbl_cached_nodup, (
            (1, "cached_sym_1"),
            (2, "cached_sym_2"),
        ))

        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        self.assertEqual(len(self.te._cached_rows[fake_tbl_cached.table_id]), 2)
        self.assertEqual(len(self.te._cached_rows[fake_tbl_cached_nodup.table_id]), 2)

        self.assertEqual(self.te._pk_index[fake_tbl_cached.table_id][1], (1, "preloaded_alpha", 100))
        self.assertEqual(self.te._nodup_index[fake_tbl_cached_nodup.table_id][("cached_sym_1",)], 1)

    def test_in_memory_get_primary_key_and_column_index(self) -> None:
        """Verify get() resolves queries via in-memory indices without querying DB."""
        self.db.insert(fake_tbl_cached, (
            (1, "item_one", 10),
            (2, "item_two", 20),
        ))
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        res_pk = self.te.get(fake_tbl_cached.table_id, (2, None, None))
        self.assertEqual(res_pk, (2, "item_two", 20))

        res_col = self.te.get(fake_tbl_cached.table_id, (None, "item_one", None))
        self.assertEqual(res_col, (1, "item_one", 10))

        res_val = self.te.get(fake_tbl_cached.table_id, (None, None, 20))
        self.assertEqual(res_val, (2, "item_two", 20))

        res_none = self.te.get(fake_tbl_cached.table_id, (None, "non_existent", None))
        self.assertIsNone(res_none)

    def test_in_memory_deduplication_set(self) -> None:
        """Verify set() for cached no_duplicate tables deduplicates in-memory."""
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        r1 = self.te.set(fake_tbl_cached_nodup.table_id, (None, "symbol_A"))
        self.assertEqual(r1, (1, "symbol_A"))
        self.assertEqual(self.te.next_id[fake_tbl_cached_nodup.table_id], 2)

        r1_dup = self.te.set(fake_tbl_cached_nodup.table_id, (None, "symbol_A"))
        self.assertEqual(r1_dup, (1, "symbol_A"))
        self.assertEqual(self.te.next_id[fake_tbl_cached_nodup.table_id], 2)

        res = self.te.get(fake_tbl_cached_nodup.table_id, (None, "symbol_A"))
        self.assertEqual(res, (1, "symbol_A"))

    def test_realtime_cache_sync_on_set_and_update(self) -> None:
        """Verify set() and update() immediately update in-memory caches and indices."""
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        self.te.set(fake_tbl_cached.table_id, (None, "sync_test", 50))
        self.assertEqual(self.te.get(fake_tbl_cached.table_id, (1, None, None)), (1, "sync_test", 50))

        self.te.update(fake_tbl_cached.table_id, (1, "sync_test_mod", 99))
        self.assertEqual(self.te.get(fake_tbl_cached.table_id, (1, None, None)), (1, "sync_test_mod", 99))
        self.assertEqual(self.te.get(fake_tbl_cached.table_id, (None, "sync_test_mod", None)), (1, "sync_test_mod", 99))

        self.assertIsNone(self.te.get(fake_tbl_cached.table_id, (None, "sync_test", None)))

    def test_automatic_view_hashing_in_cached_engine(self) -> None:
        """Verify automatic view hashing in TECachedDB synchronizes cached hash table."""
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)
        joins: JoinsType = ((fake_tbl_hroot.hid,),)

        # Stage view with hash
        v_row = self.te.view_set(joins, (None, "CachedHNode", 100))
        self.assertEqual(v_row, (1, "CachedHNode", 100))

        # Hash is immediately indexed in-memory in fake_tbl_hash
        h = compute_ast_hash(joins, ("CachedHNode", 100))
        self.assertIn(h, self.te._pk_index[fake_tbl_hash.table_id])
        self.assertEqual(self.te._pk_index[fake_tbl_hash.table_id][h], (h, 1))

        # view_get resolves via in-memory hash index with ZERO DB queries
        got = self.te.view_get(joins, (None, "CachedHNode", 100))
        self.assertEqual(got, (1, "CachedHNode", 100))

    def test_cache_persistence_across_commits(self) -> None:
        """Verify commit() flushes to DB while in-memory caches and indices remain active."""
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        self.te.set(fake_tbl_cached.table_id, (None, "persist_key", 777))
        self.te.commit(fake_tbl_cached.table_id)

        self.assertEqual(len(self.te.queued_set[fake_tbl_cached.table_id]), 0)

        db_row = self.db.select(fake_tbl_cached, (1, None, None))
        self.assertEqual(db_row, (1, "persist_key", 777))

        cached_row = self.te.get(fake_tbl_cached.table_id, (1, None, None))
        self.assertEqual(cached_row, (1, "persist_key", 777))

    def test_column_level_caching_preload_and_existence(self) -> None:
        """Verify column-level te_cached preloads only cached columns and strips un-cached payloads."""
        import hashlib
        h1 = hashlib.sha256(b"code_block_1").digest()
        h2 = hashlib.sha256(b"code_block_2").digest()

        self.db.insert(fake_tbl_partial_cached, (
            (h1, "int main() { return 0; }"),
            (h2, "static void foo(void) {}"),
        ))

        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        # Preloaded row should project un-cached column (index 1 code) to None
        self.assertIn(h1, self.te._pk_index[fake_tbl_partial_cached.table_id])
        self.assertEqual(
            self.te._pk_index[fake_tbl_partial_cached.table_id][h1],
            (h1, None),
        )

        # get() with (hash, code) matches via PK fast-path and returns projected row
        res_full = self.te.get(fake_tbl_partial_cached.table_id, (h1, "int main() { return 0; }"))
        self.assertEqual(res_full, (h1, None))

        # get() with (hash, None) also matches
        res_partial = self.te.get(fake_tbl_partial_cached.table_id, (h1, None))
        self.assertEqual(res_partial, (h1, None))

        # get() with un-cached column alone cannot match from partial cache
        res_uncached_only = self.te.get(fake_tbl_partial_cached.table_id, (None, "int main() { return 0; }"))
        self.assertIsNone(res_uncached_only)

        # Non-existent hash
        h_nonexistent = hashlib.sha256(b"unknown").digest()
        self.assertIsNone(self.te.get(fake_tbl_partial_cached.table_id, (h_nonexistent, None)))

    def test_column_level_caching_set_and_commit(self) -> None:
        """Verify set() preserves full row in queued_set while caching projected row in-memory."""
        import hashlib
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        h_new = hashlib.sha256(b"new_function()").digest()
        code_str = "void new_function() { printf(\"hello\"); }"

        # Stage insert
        res = self.te.set(fake_tbl_partial_cached.table_id, (h_new, code_str))
        self.assertEqual(res, (h_new, code_str))

        # In queued_set, full row is preserved for SQL flush
        self.assertEqual(
            self.te.queued_set[fake_tbl_partial_cached.table_id][h_new],
            (h_new, code_str),
        )

        # In in-memory cache, projected row (h_new, None) is stored
        self.assertEqual(
            self.te._pk_index[fake_tbl_partial_cached.table_id][h_new],
            (h_new, None),
        )

        # Querying existence returns projected row in O(1)
        self.assertEqual(
            self.te.get(fake_tbl_partial_cached.table_id, (h_new, code_str)),
            (h_new, None),
        )

        # Commit flushes full row to DB
        self.te.commit(fake_tbl_partial_cached.table_id)
        self.assertEqual(len(self.te.queued_set[fake_tbl_partial_cached.table_id]), 0)

        db_row = self.db.select(fake_tbl_partial_cached, (h_new, None))
        self.assertEqual(db_row, (h_new, code_str))

    def test_commit_all_skip_in_mem_indexes(self) -> None:
        """Verify commit_all(update_in_mem_indexes=False) flushes to DB and clears in-memory caches."""
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        self.te.set(fake_tbl_cached.table_id, (None, "teardown_key", 12345))
        self.te.set(fake_tbl_cached_nodup.table_id, (None, "teardown_sym"))

        # Verify cached structures contain the data prior to commit
        self.assertIn(1, self.te._pk_index[fake_tbl_cached.table_id])
        self.assertIn(("teardown_sym",), self.te._nodup_index[fake_tbl_cached_nodup.table_id])

        # Commit without updating in-mem indexes (clears caches for shutdown)
        self.te.commit_all(update_in_mem_indexes=False)

        # Queued buffers must be empty
        self.assertEqual(len(self.te.queued_set[fake_tbl_cached.table_id]), 0)
        self.assertEqual(len(self.te.queued_set[fake_tbl_cached_nodup.table_id]), 0)

        # Database must have committed records
        db_cached = self.db.select(fake_tbl_cached, (1, None, None))
        self.assertEqual(db_cached, (1, "teardown_key", 12345))
        db_nodup = self.db.select(fake_tbl_cached_nodup, (1, None))
        self.assertEqual(db_nodup, (1, "teardown_sym"))

        # In-memory structures must be cleared
        self.assertEqual(len(self.te._cached_rows), 0)
        self.assertEqual(len(self.te._cached_rows_pos), 0)
        self.assertEqual(len(self.te._pk_index), 0)
        self.assertEqual(len(self.te._nodup_index), 0)
        self.assertEqual(len(self.te._col_indices), 0)

    def test_commit_single_table_skip_in_mem_indexes(self) -> None:
        """Verify commit(table_id, update_in_mem_indexes=False) flushes and purges target table cache."""
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        self.te.set(fake_tbl_cached.table_id, (None, "single_k", 555))
        self.te.set(fake_tbl_cached_nodup.table_id, (None, "nodup_retain"))

        # Commit only fake_tbl_cached with update_in_mem_indexes=False
        self.te.commit(fake_tbl_cached.table_id, update_in_mem_indexes=False)

        # Database must have the committed row
        db_cached = self.db.select(fake_tbl_cached, (1, None, None))
        self.assertEqual(db_cached, (1, "single_k", 555))

        # fake_tbl_cached must be purged from cache structures
        self.assertNotIn(fake_tbl_cached.table_id, self.te._cached_rows)
        self.assertNotIn(fake_tbl_cached.table_id, self.te._pk_index)

        # fake_tbl_cached_nodup must retain its in-memory cache
        self.assertIn(fake_tbl_cached_nodup.table_id, self.te._nodup_index)
        self.assertEqual(self.te._nodup_index[fake_tbl_cached_nodup.table_id][("nodup_retain",)], 1)

    def test_dynamic_update_in_mem_indexes_toggle(self) -> None:
        """Verify disabling update_in_mem_indexes skips in-memory index updates on set and update."""
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        self.te.update_in_mem_indexes = False

        # Staging a row when update_in_mem_indexes is False
        res = self.te.set(fake_tbl_cached.table_id, (None, "bypass_index", 888))
        self.assertEqual(res, (1, "bypass_index", 888))

        # Row is staged in queued_set for DB
        self.assertIn(1, self.te.queued_set[fake_tbl_cached.table_id])

        # Row was NOT indexed in _pk_index or _cached_rows
        self.assertNotIn(1, self.te._pk_index.get(fake_tbl_cached.table_id, {}))

    def test_schema_table_ordering_and_backward_foreign_keys(self) -> None:
        """Verify m_tag_code is table_id=10 and all foreign keys point strictly backward."""
        from core.DBLayout import TABLES, m_tag_code, m_tag

        self.assertEqual(m_tag_code.table_id, 10)
        self.assertEqual(m_tag_code.te_cached, (0,))
        self.assertEqual(m_tag.table_id, 11)

        # Verify TABLES order matches table_id
        for idx, tbl in enumerate(TABLES):
            self.assertEqual(tbl.table_id, idx, f"Table {tbl.table_name} table_id mismatch")

        # Verify all foreign keys point to lower table_ids
        tbl_id_map = {tbl.table_name: tbl.table_id for tbl in TABLES}
        for tbl in TABLES:
            if tbl.init_foreign:
                for fk in tbl.init_foreign:
                    foreign_tbl_name = fk[1]
                    foreign_tbl_id = tbl_id_map[foreign_tbl_name]
                    self.assertLess(
                        foreign_tbl_id,
                        tbl.table_id,
                        f"Forward FK constraint detected: {tbl.table_name} (id={tbl.table_id}) -> {foreign_tbl_name} (id={foreign_tbl_id})",
                    )

    def test_high_volume_duplicate_pk_set_performance(self) -> None:
        """Verify set() maintains O(1) sub-millisecond execution when re-setting existing PKs in large cache."""
        import hashlib
        import time

        fake_tbl_perf = Table(
            table_id=89,
            table_name="m_fake_perf",
            columns=(
                ("hash", "BINARY(32)", "NOT NULL"),
                ("code", "LONGTEXT", "NOT NULL"),
            ),
            primary=("hash",),
            te_cached=("hash",),
        )
        self.te.start([fake_tbl_perf], MockDB)

        # Seed 10,000 items in cache
        base_hashes = [hashlib.sha256(f"seed_{i}".encode()).digest() for i in range(10000)]
        for h in base_hashes:
            self.te.set(fake_tbl_perf.table_id, (h, "sample_code"))

        self.assertEqual(len(self.te._cached_rows[fake_tbl_perf.table_id]), 10000)

        # Re-set 20,000 duplicate keys and verify speed
        t0 = time.perf_counter()
        for i in range(20000):
            h = base_hashes[i % len(base_hashes)]
            res = self.te.set(fake_tbl_perf.table_id, (h, "sample_code"))
            self.assertEqual(res[0], h)
        elapsed = time.perf_counter() - t0

        # Should execute 20,000 duplicate checks in < 0.1s (well over 200,000 ops/sec)
        self.assertLess(elapsed, 0.1, f"High volume duplicate set took too long: {elapsed:.4f}s")

    def test_bytearray_preloading_and_indexing(self) -> None:
        """Verify bytearray and memoryview instances returned by DB driver are converted to immutable bytes without error."""
        import hashlib

        fake_tbl_binary = Table(
            table_id=90,
            table_name="_test_fake_binary_cache",
            columns=(
                ("hash", "BINARY(32)", "NOT NULL"),
                ("code", "LONGTEXT", "NOT NULL"),
            ),
            primary=("hash",),
            te_cached=("hash",),
        )

        class MockDBReturningBytearray(MockDB):
            def view_select_multiple(self, tables, joins, columns):
                # Return rows with raw bytearray objects as mysql-connector does for BINARY columns
                h = bytearray(hashlib.sha256(b"code_test").digest())
                return [(h, "code_test_content")]

            def select_preload(self, table, cached_columns=None, min_vid=None):
                h = bytearray(hashlib.sha256(b"code_test").digest())
                if cached_columns is not None and len(cached_columns) < table.length:
                    return [(h,)]
                return [(h, "code_test_content")]


        self.te.start([fake_tbl_binary], MockDBReturningBytearray)

        # Verify row is indexed without unhashable bytearray error
        self.assertEqual(len(self.te._cached_rows[fake_tbl_binary.table_id]), 1)
        cached_row = self.te._cached_rows[fake_tbl_binary.table_id][0]
        self.assertIsInstance(cached_row[0], bytes)

        # Query using bytes
        expected_h = hashlib.sha256(b"code_test").digest()
        got_row = self.te.get(fake_tbl_binary.table_id, (expected_h, None))
        self.assertIsNotNone(got_row)
        self.assertEqual(got_row[0], expected_h)

        # Query using bytearray
        got_row_ba = self.te.get(fake_tbl_binary.table_id, (bytearray(expected_h), None))
        self.assertIsNotNone(got_row_ba)
        self.assertEqual(got_row_ba[0], expected_h)

    def test_version_scoped_table_schema_configuration(self) -> None:
        """Verify Table supports version_scoped across bool, dict, and tuple te_cached configurations."""
        # 1. Standalone keyword
        tbl_kw = Table(
            table_id=881,
            table_name="_test_v_scoped_kw",
            columns=(("vid", "INT", "NOT NULL"), ("fnid", "INT", "NOT NULL")),
            primary=("vid", "fnid"),
            te_cached=True,
            version_scoped=True,
        )
        self.assertTrue(tbl_kw.version_scoped)
        self.assertEqual(tbl_kw.cached_columns, (0, 1))

        # 2. Combined with column projection
        tbl_proj = Table(
            table_id=882,
            table_name="_test_v_scoped_proj",
            columns=(("vid", "INT", "NOT NULL"), ("fnid", "INT", "NOT NULL"), ("data", "TEXT", "NOT NULL")),
            primary=("vid", "fnid"),
            te_cached=("vid", "fnid"),
            version_scoped=True,
        )
        self.assertTrue(tbl_proj.version_scoped)
        self.assertEqual(tbl_proj.cached_columns, (0, 1))

        # 3. Dict configuration
        tbl_dict = Table(
            table_id=883,
            table_name="_test_v_scoped_dict",
            columns=(("vid", "INT", "NOT NULL"), ("fnid", "INT", "NOT NULL")),
            primary=("vid", "fnid"),
            te_cached={"columns": ("vid", "fnid"), "version_scoped": True},
        )
        self.assertTrue(tbl_dict.version_scoped)
        self.assertEqual(tbl_dict.cached_columns, (0, 1))

    def test_version_scoped_working_window_preloading_and_eviction(self) -> None:
        """Verify version_scoped tables only preload active window (vid >= Old_VID) and fallback to DB for out-of-window."""
        import sys
        main_mod = sys.modules.get("__main__")
        orig_gp = getattr(main_mod, "gp", None) if main_mod is not None else None

        class DummyGP:
            Old_VID = 2
            VID = 3

        try:
            if main_mod is not None:
                main_mod.gp = DummyGP()

            # Seed database directly with rows for VID=1, VID=2, VID=3
            self.db.insert(fake_tbl_cached_bridge, (1, 100, 10))
            self.db.insert(fake_tbl_cached_bridge, (2, 100, 20))
            self.db.insert(fake_tbl_cached_bridge, (3, 100, 30))

            # Start TECachedDB with Old_VID = 2
            v_te = TECachedDB()
            v_te.start(ALL_FAKE_TABLES, lambda: self.db)
            try:
                # Assert that in-memory _cached_rows ONLY contains rows for VID >= 2 (VID=1 is evicted/not preloaded)
                cached = v_te._cached_rows[fake_tbl_cached_bridge.table_id]
                cached_vids = [r[0] for r in cached]
                self.assertNotIn(1, cached_vids, "Historical VID=1 rows should not be preloaded into in-memory cache")
                self.assertIn(2, cached_vids, "Active Old_VID=2 row must be preloaded in cache")
                self.assertIn(3, cached_vids, "Active VID=3 row must be preloaded in cache")

                # In-window lookups resolve from _pk_index in O(1)
                r2 = v_te.get(fake_tbl_cached_bridge.table_id, (2, 100, None))
                self.assertEqual(r2, (2, 100, 20))

                r3 = v_te.get(fake_tbl_cached_bridge.table_id, (3, 100, None))
                self.assertEqual(r3, (3, 100, 30))

                # Out-of-window lookup (VID=1) is not in _pk_index, but safely falls back to DB query
                r1 = v_te.get(fake_tbl_cached_bridge.table_id, (1, 100, None))
                self.assertEqual(r1, (1, 100, 10))
            finally:
                v_te.close()
        finally:
            if main_mod is not None:
                main_mod.gp = orig_gp

    def test_selective_inverted_column_indexing_pk_exemption(self) -> None:
        """Verify _col_indices does not create redundant inverted index buckets for primary key columns."""
        te = TECachedDB()
        te.start(ALL_FAKE_TABLES, lambda: self.db)
        try:
            # For fake_tbl_hash (primary=("hash",)), column 0 should be exempt from _col_indices
            self.assertNotIn(
                0,
                te._col_indices[fake_tbl_hash.table_id],
                "Primary key column 0 should be exempt from _col_indices",
            )

            # For fake_tbl_cached_bridge (primary=("vid", "fnid")), columns 0 and 1 should be exempt
            self.assertNotIn(
                0,
                te._col_indices[fake_tbl_cached_bridge.table_id],
                "Primary key column 'vid' should be exempt from _col_indices",
            )
            self.assertNotIn(
                1,
                te._col_indices[fake_tbl_cached_bridge.table_id],
                "Primary key column 'fnid' should be exempt from _col_indices",
            )
            self.assertIn(
                2,
                te._col_indices[fake_tbl_cached_bridge.table_id],
                "Non-primary column 'fid' should be present in _col_indices",
            )
        finally:
            te.close()

    def test_start_new_db_selective_preload_reconstruction(self) -> None:
        """Verify start_new_db preloads partial column queries and accurately reconstructs canonical rows."""
        import hashlib
        h1 = hashlib.sha256(b"code_partial_1").digest()
        h2 = hashlib.sha256(b"code_partial_2").digest()

        self.db.insert(fake_tbl_partial_cached, (
            (h1, "long_text_content_1"),
            (h2, "long_text_content_2"),
        ))

        te = TECachedDB()
        te.start(ALL_FAKE_TABLES, lambda: self.db)
        try:
            # Check that _cached_rows and _pk_index have canonical row (h, None)
            self.assertEqual(len(te._cached_rows[fake_tbl_partial_cached.table_id]), 3)  # 1 initial_insert seed + 2 inserted
            self.assertEqual(te._pk_index[fake_tbl_partial_cached.table_id][h1], (h1, None))
            self.assertEqual(te._pk_index[fake_tbl_partial_cached.table_id][h2], (h2, None))

            # Query via get
            res1 = te.get(fake_tbl_partial_cached.table_id, (h1, None))
            self.assertEqual(res1, (h1, None))
        finally:
            te.close()






# =============================================================================
# Test Suite 4: End-to-End Integration & Monotonic Integrity
# =============================================================================

class TestDBAndTEIntegration(unittest.TestCase):
    """Test full multi-transaction lifecycles and sequence monotonicity between TE and DB."""

    def setUp(self) -> None:
        """Initialize DB and TE with clean fake tables."""
        self.db = CONFIGURED_DB_ENGINE_CLS()
        self.db.drop_table(ALL_FAKE_TABLES)
        self.db.create_table(ALL_FAKE_TABLES)
        self.te = CONFIGURED_TE_ENGINE_CLS()
        self.te.start(ALL_FAKE_TABLES, lambda: self.db)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        """Tear down resources."""
        try:
            self.te.close()
        except Exception:
            pass
        try:
            self.db.drop_table(ALL_FAKE_TABLES)
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass

    def test_end_to_end_view_insert_commit_query_lifecycle(self) -> None:
        """Verify full lifecycle: view_set -> commit_all -> view_get -> view_get_multiple."""
        joins: JoinsType = (
            (fake_tbl_parent.pid, fake_tbl_child.pid, 1),
        )

        v1 = self.te.view_set(joins, (None, "ProjectRoot", None, None, "ModuleA"))
        v2 = self.te.view_set(joins, (None, "ProjectRoot", None, None, "ModuleB"))

        self.assertEqual(v1, (1, "ProjectRoot", 1, 1, "ModuleA"))
        self.assertEqual(v2, (2, "ProjectRoot", 2, 2, "ModuleB"))

        self.te.commit_all()

        self.te.start(ALL_FAKE_TABLES, lambda: self.db)

        res_v1 = self.te.view_get(joins, (None, "ProjectRoot", None, None, "ModuleA"))
        self.assertEqual(res_v1, (1, "ProjectRoot", 1, 1, "ModuleA"))

        res_all = self.te.view_get_multiple(joins, (None, "ProjectRoot", None, None, None))
        self.assertEqual(len(res_all), 2)

    def test_monotonic_sequence_integrity_across_transactions(self) -> None:
        """Verify next_id never regresses or reuses IDs across multiple commit passes and restarts."""
        for i in range(1, 4):
            self.te.set(fake_tbl_simple.table_id, (None, f"item_{i}", i * 10))
        self.te.commit(fake_tbl_simple.table_id)

        r4 = self.te.set(fake_tbl_simple.table_id, (None, "item_4", 40))
        r5 = self.te.set(fake_tbl_simple.table_id, (None, "item_5", 50))
        self.assertEqual(r4[0], 4)
        self.assertEqual(r5[0], 5)
        self.te.commit(fake_tbl_simple.table_id)

        new_te = CONFIGURED_TE_ENGINE_CLS()
        new_te.start(ALL_FAKE_TABLES, lambda: self.db)
        try:
            self.assertEqual(new_te.next_id[fake_tbl_simple.table_id], 6)
            r6 = new_te.set(fake_tbl_simple.table_id, (None, "item_6", 60))
            self.assertEqual(r6[0], 6)
        finally:
            new_te.close()

    def test_hash_deduplication_across_reconnect_and_commits(self) -> None:
        """Verify that AST hashes committed in cycle 1 are recognized in cycle 2 without recreation or duplicate inserts."""
        joins: JoinsType = ((fake_tbl_hroot.hid,),)

        # Cycle 1: Insert AST node with hash
        v1_row = self.te.view_set(joins, (None, "PersistentNode", 999))
        self.assertEqual(v1_row, (1, "PersistentNode", 999))
        self.te.commit_all()

        h = compute_ast_hash(joins, ("PersistentNode", 999))

        # Simulate Cycle 2: Start fresh Table Engine connection against the populated database
        cycle2_te = CONFIGURED_TE_ENGINE_CLS()
        cycle2_te.start(ALL_FAKE_TABLES, lambda: self.db)
        try:
            # view_get should find the existing node
            got_row = cycle2_te.view_get(joins, (None, "PersistentNode", 999))
            self.assertEqual(got_row, (1, "PersistentNode", 999))

            # view_set should reuse existing ast_id=1 and NOT stage a new row
            v2_row = cycle2_te.view_set(joins, (None, "PersistentNode", 999))
            self.assertEqual(v2_row, (1, "PersistentNode", 999))

            # Queued set for hash table should be empty (not re-staged)
            self.assertEqual(len(cycle2_te.queued_set[fake_tbl_hash.table_id]), 0)

            # Commit should succeed cleanly without duplicate key error
            cycle2_te.commit_all()
        finally:
            cycle2_te.close()

    def test_multi_version_bridge_propagation(self) -> None:
        """Verify that unchanged bridge records propagate across versions without loss."""
        # Cycle 1: VID=1
        fnid = 1
        fid = 10
        self.te.set(fake_tbl_cached_bridge.table_id, (1, fnid, fid))
        self.te.commit_all()

        # Verify DB has VID=1
        db_row = self.db.select(fake_tbl_cached_bridge, (1, fnid, None))
        self.assertEqual(db_row, (1, fnid, fid))

        # Cycle 2: VID=2 (Simulate next version update)
        cycle2_te = CONFIGURED_TE_ENGINE_CLS()
        cycle2_te.start(ALL_FAKE_TABLES, lambda: self.db)
        try:
            # Query Old_VID=1 bridge record (like processing_dirs does)
            old_bf = cycle2_te.get(fake_tbl_cached_bridge.table_id, (1, fnid, None))
            self.assertIsNotNone(old_bf)
            self.assertEqual(old_bf, (1, fnid, fid))

            # Propagate unchanged directory/file bridge to VID=2
            cycle2_te.set(fake_tbl_cached_bridge.table_id, (2, fnid, old_bf[2]))
            cycle2_te.commit_all()
        finally:
            cycle2_te.close()

        # Cycle 3: VID=3 (Simulate next version update)
        cycle3_te = CONFIGURED_TE_ENGINE_CLS()
        cycle3_te.start(ALL_FAKE_TABLES, lambda: self.db)
        try:
            # Query Old_VID=2 bridge record
            old_bf = cycle3_te.get(fake_tbl_cached_bridge.table_id, (2, fnid, None))
            self.assertIsNotNone(old_bf)
            self.assertEqual(old_bf, (2, fnid, fid))

            # Propagate unchanged directory/file bridge to VID=3
            cycle3_te.set(fake_tbl_cached_bridge.table_id, (3, fnid, old_bf[2]))
            cycle3_te.commit_all()
        finally:
            cycle3_te.close()

        # Verify all 3 versions exist in DB
        for v in (1, 2, 3):
            row = self.db.select(fake_tbl_cached_bridge, (v, fnid, None))
            self.assertEqual(row, (v, fnid, fid))


# =============================================================================
# CLI & Programmatic Test Runner
# =============================================================================

def run_integrity_tests(db_engine_name: str = "mock", te_engine_name: str = "cached") -> int:
    """Execute the integrity test suite with the specified DB and TableEngine backends.

    Args:
        db_engine_name: Target database backend ('mock', 'mariadb').
        te_engine_name: Target TableEngine backend ('cached', 'direct', 'tecacheddb', 'tedirectdb').

    Returns:
        0 if all tests pass, 1 if any tests fail.
    """
    global CONFIGURED_DB_ENGINE_CLS, CONFIGURED_TE_ENGINE_CLS

    # Resolve DB engine class
    CONFIGURED_DB_ENGINE_CLS = get_db_engine(db_engine_name)

    # Resolve TableEngine class
    CONFIGURED_TE_ENGINE_CLS = get_table_engine(te_engine_name)

    print(COLOR.cyan("=========================================================================================="))
    print(COLOR.cyan(f"[*] Running TableEngine & Database Integrity Test Suite"))
    print(COLOR.cyan(f"    - Database Engine    : {CONFIGURED_DB_ENGINE_CLS.__name__} ({db_engine_name})"))
    print(COLOR.cyan(f"    - Table Engine       : {CONFIGURED_TE_ENGINE_CLS.__name__} ({te_engine_name})"))
    print(COLOR.cyan(f"    - Fake Tables Prefix : _test_fake_* (Production DB is completely isolated)"))
    print(COLOR.cyan("=========================================================================================="))

    # Build and run unittest test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestDBEngineIntegrity))
    suite.addTests(loader.loadTestsFromTestCase(TestTableEngineIntegrity))
    suite.addTests(loader.loadTestsFromTestCase(TestTECachedDBIntegrity))
    suite.addTests(loader.loadTestsFromTestCase(TestDBAndTEIntegration))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    if result.wasSuccessful():
        print(COLOR.green(f"\n[+] ALL {result.testsRun} INTEGRITY TESTS PASSED SUCCESSFULLY!"))
        print(COLOR.cyan("==========================================================================================\n"))
        return 0
    else:
        print(COLOR.red(f"\n[-] INTEGRITY TEST FAILURES: {len(result.failures)} failures, {len(result.errors)} errors."))
        print(COLOR.cyan("==========================================================================================\n"))
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TableEngine & Database Integrity Test Suite")
    parser.add_argument(
        "--db", "--db-engine",
        dest="db_engine",
        default="mock",
        choices=["mock", "mariadb"],
        help="Select Database backend engine for integrity testing (default: mock)",
    )
    parser.add_argument(
        "--te", "--table-engine",
        dest="table_engine",
        default="cached",
        choices=["cached", "tecacheddb", "direct", "tedirectdb"],
        help="Select Table Engine architecture backend (default: cached)",
    )
    args = parser.parse_args()
    sys.exit(run_integrity_tests(db_engine_name=args.db_engine, te_engine_name=args.table_engine))
