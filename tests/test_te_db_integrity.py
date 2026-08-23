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
        ("hash", "VARCHAR(64)", "NOT NULL"),
        ("ast_id", "INT", "NOT NULL"),
    ),
    primary=("hash",),
    foreign=(("ast_id", "_test_fake_hroot", "hid"),),
    initial_insert=None,
    no_duplicate=False,
    te_cached=True,
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
