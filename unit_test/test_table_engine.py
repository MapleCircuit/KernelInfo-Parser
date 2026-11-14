"""unit_test/test_table_engine.py - Generic Contract Verification Suite for Table Engines.

Validates all functions, caching strategies, deduplication behaviors, and architectural invariants
specified in table_engine/TE_API.md across both TEDirectDB and TECachedDB.

Test Architecture:
- Uses an isolated in-memory RecordingDriver (Spy) to intercept and assert outbound database payloads
  without connecting to a real database socket.
- Automatically discovers all TableEngine backends (TECachedDB, TEDirectDB) and synthesizes TestCase classes.
- Tests broken view construction, duplicate key deduplication, cross-chunk commit tracking,
  cryptographic hash pre-sorting, and extended cache capabilities.
- Every test uses granular `test_step` contexts with explicit diagnostic notes.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
import os
import sys
from types import TracebackType
from typing import Any, Callable, Sequence, Type
import unittest

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import core
from core.globalstuff import JoinsType, PointerType, SafeDataType
from core.TableHandling import Table
from db_engine.base import BaseDBEngine
from table_engine import TABLE_ENGINE_MAP, TEDirectDB, TECachedDB
from unit_test.harness import (
    TestTableCollection,
    test_feature,
    test_step,
    FeatureRegistry,
)


class RecordingDriver(BaseDBEngine):
    """In-memory recording spy driver that captures all outbound DB operations from TableEngine."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.inserts: list[tuple[str, tuple[tuple[Any, ...], ...]]] = []
        self.updates: list[tuple[str, tuple[tuple[Any, ...], ...]]] = []
        self.parallel_commits: list[list[tuple[str, tuple, tuple]]] = []
        self.select_calls: list[tuple[str, tuple[SafeDataType, ...]]] = []
        self.preload_calls: list[tuple[str, tuple[int, ...] | None, int | None]] = []
        self.next_ids: dict[str, int] = defaultdict(lambda: 1)
        self.select_returns: dict[tuple[str, tuple], tuple | None] = {}
        self.preload_returns: dict[str, list[tuple]] = defaultdict(list)
        self.table_sizes: dict[str, int] = {}
        self.size_calls: list[tuple[str, tuple[int, ...] | None, int | None]] = []
        self.view_select_returns: dict[tuple, tuple | None] = {}
        self.view_select_multiple_returns: dict[tuple, list[tuple]] = defaultdict(list)
        self.select_in_calls: list[tuple[str, int, tuple]] = []
        # Rows select_in() answers from (rows written by other nodes, not preloaded)
        self.db_rows: dict[str, list[tuple]] = defaultdict(list)
        # (table names, exception) select_preload() raises for those tables
        self.preload_error: tuple[set[str], BaseException] | None = None
        self.is_closed: bool = False

    def __enter__(self) -> RecordingDriver:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        pass

    def close(self) -> None:
        self.is_closed = True
        self.calls.append(("close", (), {}))

    def get_next_id(self, table: Table) -> int:
        self.calls.append(("get_next_id", (table.table_name,), {}))
        return self.next_ids[table.table_name]

    def select(self, table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None:
        self.calls.append(("select", (table.table_name, data), {}))
        self.select_calls.append((table.table_name, data))
        return self.select_returns.get((table.table_name, data))

    def select_in(self, table: Table, col_idx: int, values: Sequence[SafeDataType]) -> list[tuple[SafeDataType, ...]]:
        self.calls.append(("select_in", (table.table_name, col_idx, tuple(values)), {}))
        self.select_in_calls.append((table.table_name, col_idx, tuple(values)))
        wanted = set(values)
        return [row for row in self.db_rows.get(table.table_name, []) if row[col_idx] in wanted]

    def select_preload(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> list[tuple[SafeDataType, ...]]:
        self.calls.append(("select_preload", (table.table_name, cached_columns, min_vid), {}))
        self.preload_calls.append((table.table_name, cached_columns, min_vid))
        if self.preload_error is not None and table.table_name in self.preload_error[0]:
            raise self.preload_error[1]
        return list(self.preload_returns.get(table.table_name, []))

    def table_size(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> int:
        self.calls.append(("table_size", (table.table_name, cached_columns, min_vid), {}))
        self.size_calls.append((table.table_name, cached_columns, min_vid))
        return self.table_sizes.get(table.table_name, 0)

    def view_select(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        self.calls.append(("view_select", (joins, columns), {}))
        return self.view_select_returns.get((joins, columns))

    def view_select_multiple(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> list[tuple[SafeDataType, ...]]:
        self.calls.append(("view_select_multiple", (joins, columns), {}))
        return list(self.view_select_multiple_returns.get((joins, columns), []))

    def insert(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        if not data:
            return
        rows = (data,) if not isinstance(data[0], (tuple, list)) else tuple(data)
        self.calls.append(("insert", (table.table_name, rows), {}))
        self.inserts.append((table.table_name, rows))

    def update(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        if not data:
            return
        rows = (data,) if not isinstance(data[0], (tuple, list)) else tuple(data)
        self.calls.append(("update", (table.table_name, rows), {}))
        self.updates.append((table.table_name, rows))

    def commit_tables_parallel(
        self,
        tables_data: Sequence[tuple[Table, Sequence[tuple], Sequence[tuple]]],
        max_workers: int | None = None,
    ) -> None:
        summary = [(t.table_name, tuple(ins), tuple(upd)) for t, ins, upd in tables_data]
        self.calls.append(("commit_tables_parallel", (summary, max_workers), {}))
        self.parallel_commits.append(summary)

    def create_table(self, tables: Sequence[Table] | Table) -> None:
        pass

    def drop_table(self, tables: Sequence[Table] | Table) -> None:
        pass

    def test_tables(self, tables: Sequence[Table] | Table) -> list[str] | None:
        return None

    def index_exists(self, index_name: str, table: Table) -> bool:
        return False

    def create_index(self, index_name: str, table: Table, rows: tuple[PointerType, ...]) -> None:
        pass

    def remove_index(self, index_name: str, table: Table) -> None:
        pass

    def create_indexes(
        self,
        indexes: Sequence[tuple[str, Table, tuple[PointerType, ...]]],
        max_workers: int | None = None,
    ) -> None:
        pass

    def remove_indexes(
        self,
        indexes: Sequence[tuple[str, Table]],
        max_workers: int | None = None,
    ) -> None:
        pass

    def verify_relational_integrity(self, tables: Sequence[Table]) -> dict[str, int]:
        return {}

    def query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> list[dict[str, Any] | tuple[SafeDataType, ...]]:
        self.calls.append(("query", (sql, params, dictionary), {}))
        return []

    def query_one(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> dict[str, Any] | tuple[SafeDataType, ...] | None:
        self.calls.append(("query_one", (sql, params, dictionary), {}))
        return None

    def query_iter(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
        chunk_size: int = 2000,
    ) -> Iterator[dict[str, Any] | tuple[SafeDataType, ...]]:
        self.calls.append(("query_iter", (sql, params, dictionary, chunk_size), {}))
        return iter(())

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        self.calls.append(("execute", (sql, params), {}))
        return 0

    def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
        self.calls.append(("executemany", (sql, params_seq), {}))
        return len(params_seq)


class GenericTableEngineTestCase:
    """Base generic contract test suite for any TableEngine implementation (TEDirectDB, TECachedDB)."""

    te_class: Type[TEDirectDB | TECachedDB]
    engine_name: str
    feature_registry: FeatureRegistry
    test_tables: TestTableCollection
    te: Any
    driver: RecordingDriver

    # Capability flags
    is_cached_engine: bool = False

    @classmethod
    def setUpClass(cls) -> None:
        """Initialize class-level feature registry and capability introspections."""
        cls.feature_registry = FeatureRegistry()
        cls.is_cached_engine = issubclass(cls.te_class, TECachedDB)

    def setUp(self) -> None:
        """Create fresh isolated TableEngine instance, RecordingDriver, and schema tables."""
        self.test_tables = TestTableCollection(prefix="t_")
        self.driver = RecordingDriver()

        # Initialize TableEngine
        self.te = self.te_class()
        self.te.start(self.test_tables.tables_list, lambda: self.driver)

    def tearDown(self) -> None:
        """Safely close engine."""
        if hasattr(self, "te") and self.te is not None:
            self.te.close()

    def reload_test_db(self) -> None:
        """Reset TableEngine and driver state on error to prevent cascading test pollution."""
        try:
            if hasattr(self, "te") and self.te is not None:
                self.te.close()
            self.driver = RecordingDriver()
            self.te = self.te_class()
            self.te.start(self.test_tables.tables_list, lambda: self.driver)
        except Exception as e:
            print(f"[!] TableEngine reload encountered an error on {self.engine_name}: {e}")

    # =========================================================================
    # 1. Lifecycle & Initialization
    # =========================================================================

    @test_feature("lifecycle_and_init")
    def test_01_lifecycle_and_start(self) -> None:
        """Verify TableEngine initialization, start(), start_new_db(), and close()."""
        with test_step("Instantiating isolated TableEngine and verifying empty state"):
            fresh_te = self.te_class()
            self.assertEqual(len(fresh_te.tables), 0)
            self.assertIsNone(fresh_te.db)

        with test_step("Calling start() with test schema and RecordingDriver factory"):
            fresh_driver = RecordingDriver()
            fresh_te.start(self.test_tables.tables_list, lambda: fresh_driver)
            self.assertEqual(len(fresh_te.tables), len(self.test_tables))
            self.assertIs(fresh_te.db, fresh_driver)

        with test_step("Calling start_new_db() to rotate DB connection handle"):
            second_driver = RecordingDriver()
            fresh_te.start_new_db(lambda: second_driver)
            self.assertIs(fresh_te.db, second_driver)
            self.assertTrue(fresh_driver.is_closed, "Expected first driver handle to be closed cleanly")

        with test_step("Calling start_new_db(is_worker=True) to simulate worker DB rotation"):
            worker_driver = RecordingDriver()
            fresh_te.start_new_db(lambda: worker_driver, is_worker=True)
            self.assertIs(fresh_te.db, worker_driver)
            self.assertFalse(fresh_te.async_commits)
            self.assertIsNone(fresh_te._pending)
            self.assertTrue(second_driver.is_closed, "Expected second driver handle to be closed cleanly")

        with test_step("Calling close() to release DB handle"):
            fresh_te.close()
            self.assertTrue(worker_driver.is_closed)
            self.assertIsNone(fresh_te.db)

    # =========================================================================
    # 2. Single-Table Operations & Deduplication
    # =========================================================================

    @test_feature("set_and_get_staged", depends_on=["lifecycle_and_init"])
    def test_02_set_and_get_staged(self) -> None:
        """Verify set() stages row in memory and get() resolves it without DB queries."""
        t_fn = self.test_tables.t_file_name

        with test_step("Calling set() with auto-increment generation (None at PK position)"):
            row = self.te.set(t_fn.table_id, (None, "kernel/sched.c"))
            self.assertEqual(row, (1, "kernel/sched.c"))

        with test_step(
            "Calling get() to query staged row by primary key",
            diagnostic="TableEngine must resolve newly staged rows from memory without issuing a DB select query.",
        ):
            queried = self.te.get(t_fn.table_id, (1, None))
            self.assertEqual(queried, (1, "kernel/sched.c"))
            self.assertEqual(
                len(self.driver.select_calls),
                0,
                "Expected 0 DB select calls when querying staged in-memory row",
            )

        with test_step(
            "Verifying emptiness guard on non-existent query",
            diagnostic="Empty table with auto-increment and next_id <= 1 must return None without querying DB.",
        ):
            t_cred = self.test_tables.t_credits_entry
            res_none = self.te.get(t_cred.table_id, (None, "Nobody", "nobody@kernel.org", None, None, None, None, None))
            self.assertIsNone(res_none)

    @test_feature("no_duplicate_deduplication", depends_on=["set_and_get_staged"])
    def test_03_no_duplicate_deduplication(self) -> None:
        """Verify no_duplicate=True tables deduplicate rows and reuse assigned IDs."""
        t_fn = self.test_tables.t_file_name
        self.assertTrue(t_fn.no_duplicate, "t_file_name must have no_duplicate=True")

        with test_step("First set() for unique file path"):
            row1 = self.te.set(t_fn.table_id, (None, "kernel/sched/core.c"))
            self.assertEqual(row1, (1, "kernel/sched/core.c"))
            self.assertEqual(self.te.next_id[t_fn.table_id], 2)

        with test_step(
            "Second set() with identical file path asserts assigned_id is reused without incrementing next_id",
            diagnostic=(
                "Failure indicates no_duplicate key lookup in staged/cached memory failed, "
                "allocating a duplicate ID instead of returning existing assigned_id."
            ),
        ):
            row2 = self.te.set(t_fn.table_id, (None, "kernel/sched/core.c"))
            self.assertEqual(row2, (1, "kernel/sched/core.c"))
            self.assertEqual(
                self.te.next_id[t_fn.table_id],
                2,
                "next_id should NOT increment when setting an existing duplicate key",
            )

        with test_step(
            "Calling commit() and verifying payload row format reconstruction (assigned_id, *data_key)",
            diagnostic="On commit, no_duplicate rows must be reconstructed as (assigned_id, *data_key) for db.insert().",
        ):
            self.te.commit(t_fn.table_id)
            self.assertEqual(len(self.driver.inserts), 1)
            tbl_name, payload = self.driver.inserts[0]
            self.assertEqual(tbl_name, t_fn.table_name)
            self.assertEqual(payload, ((1, "kernel/sched/core.c"),))

    @test_feature("duplicate_primary_key_rejection", depends_on=["set_and_get_staged"])
    def test_04_duplicate_primary_key_rejection(self) -> None:
        """Verify set() rejects duplicate primary keys with ValueError; update() must be used instead."""
        t_bridge = self.test_tables.t_bridge_file

        with test_step("Setting initial row with explicit multi-column primary key"):
            row1 = self.te.set(t_bridge.table_id, (1, 10, 100))
            self.assertEqual(row1, (1, 10, 100))

        with test_step(
            "Asserting duplicate set() with identical primary key raises ValueError",
            diagnostic=(
                "TableEngine must enforce database primary key uniqueness rules and raise ValueError "
                "when set() is called on an existing primary key. Callers must use update() to modify existing records."
            ),
        ):
            with self.assertRaises(ValueError) as ctx:
                self.te.set(t_bridge.table_id, (1, 10, 200))
            self.assertIn("duplicate", str(ctx.exception).lower())

        with test_step("Modifying existing record via update() succeeds"):
            upd_row = self.te.update(t_bridge.table_id, (1, 10, 200))
            self.assertEqual(upd_row, (1, 10, 200))

        with test_step("Calling commit() and verifying exactly 1 insert and 1 update dispatched"):
            self.te.commit(t_bridge.table_id)
            self.assertEqual(len(self.driver.inserts), 1)
            tbl_name, payload = self.driver.inserts[0]
            self.assertEqual(tbl_name, t_bridge.table_name)
            self.assertEqual(payload[0], (1, 10, 100))

            self.assertEqual(len(self.driver.updates), 1)
            upd_tbl, upd_payload = self.driver.updates[0]
            self.assertEqual(upd_tbl, t_bridge.table_name)
            self.assertEqual(upd_payload[0], (1, 10, 200))

    @test_feature("update_and_staging", depends_on=["set_and_get_staged"])
    def test_05_update_and_staging(self) -> None:
        """Verify update() stages modifications and commit() flushes to db.update()."""
        t_fn = self.test_tables.t_file_name

        with test_step("Staging row update via update()"):
            self.te.update(t_fn.table_id, (1, "kernel/sched_modified.c"))
            self.assertEqual(len(self.te.queued_update[t_fn.table_id]), 1)

        with test_step("Flushing update via commit()"):
            self.te.commit(t_fn.table_id)
            self.assertEqual(len(self.driver.updates), 1)
            tbl_name, payload = self.driver.updates[0]
            self.assertEqual(tbl_name, t_fn.table_name)
            self.assertEqual(payload, ((1, "kernel/sched_modified.c"),))
            self.assertEqual(len(self.te.queued_update[t_fn.table_id]), 0)

    @test_feature("cross_chunk_deduplication", depends_on=["no_duplicate_deduplication"])
    def test_06_cross_chunk_deduplication(self) -> None:
        """Verify cross-chunk deduplication tracking (_committed_nodup_keys and _committed_pks)."""
        t_fn = self.test_tables.t_file_name

        with test_step("Cycle 1: Set and commit a unique path in first chunk"):
            self.te.set(t_fn.table_id, (None, "fs/inode.c"))
            self.te.commit(t_fn.table_id)
            self.assertEqual(len(self.driver.inserts), 1)

        with test_step(
            "Cycle 2: Set identical path in subsequent chunk and verify no duplicate insert occurs",
            diagnostic=(
                "TableEngine must maintain _committed_nodup_keys across intermediate commits, "
                "filtering out previously committed keys so duplicate INSERT INTO statements are not sent."
            ),
        ):
            res_cycle2 = self.te.set(t_fn.table_id, (None, "fs/inode.c"))
            self.assertEqual(res_cycle2[0], 1, "Should reuse committed ID 1")

            self.te.commit(t_fn.table_id)
            # Inserts count should still be 1 (filtered out)
            self.assertEqual(
                len(self.driver.inserts),
                1,
                "Expected no additional db.insert call for previously committed key",
            )

    # =========================================================================
    # 3. Relational Views & Broken View Construction
    # =========================================================================

    @test_feature("view_set_decomposition", depends_on=["set_and_get_staged"])
    def test_07_view_set_decomposition(self) -> None:
        """Verify multi-table view_set decomposes rows across constituent tables and caches view IDs."""
        t_file = self.test_tables.t_file
        t_bridge = self.test_tables.t_bridge_file

        joins: JoinsType = (
            ((t_file.table_id, 0), (t_bridge.table_id, 2), 1),
        )

        with test_step("Executing view_set with concatenated multi-table columns"):
            # Columns: 3 columns for t_file + 3 columns for t_bridge = 6 columns
            # t_file: (blob_id (None), blob_hash, ftype)
            # t_bridge: (commit_id, fnid, blob_id (None))
            file_cols = [None, b"\xaa" * 20, 1]
            bridge_cols = [1, 10, None]
            joined_cols = tuple(file_cols + bridge_cols)

            result = self.te.view_set(joins, joined_cols)
            self.assertIsNotNone(result)
            self.assertEqual(result[0], 1, "Expected view_id 1 assigned to t_file PK")
            self.assertEqual(result[5], 1, "Expected view_id 1 decomposed into t_bridge blob_id position")

        with test_step("Verifying constituent rows were decomposed and staged in queued_set"):
            self.assertIn((b"\xaa" * 20, 1), self.te.queued_set[t_file.table_id])
            self.assertEqual(self.te.queued_set[t_file.table_id][(b"\xaa" * 20, 1)], 1)
            self.assertIn((1, 10), self.te.queued_set[t_bridge.table_id])
            self.assertEqual(self.te.queued_set[t_bridge.table_id][(1, 10)], (1, 10, 1))

        with test_step("Executing duplicate view_set asserts view_id is returned from queued_view cache"):
            result_dup = self.te.view_set(joins, joined_cols)
            self.assertEqual(result_dup[0], 1)
            self.assertEqual(self.te.next_id[t_file.table_id], 2, "next_id must not increment for cached view")

    @test_feature("ast_view_hash_deduplication", depends_on=["view_set_decomposition"])
    def test_08_ast_view_hash_deduplication(self) -> None:
        """Verify AST node views rooted at tables with hashing_table reuse ast_id via SHA-256 hash match."""
        t_ast = self.test_tables.t_ast
        t_hash = self.test_tables.t_ast_hash

        joins: JoinsType = (((t_ast.table_id, 0),),)

        with test_step("Executing initial view_set for AST node"):
            ast_cols = (None, "my_func", 1)  # ast_id, name, type_id
            res1 = self.te.view_set(joins, ast_cols)
            self.assertEqual(res1[0], 1)

        with test_step(
            "Executing duplicate structural view_set asserts SHA-256 hash match reuses ast_id",
            diagnostic="TableEngine must compute compute_ast_hash and reuse existing ast_id for identical AST views.",
        ):
            res2 = self.te.view_set(joins, ast_cols)
            self.assertEqual(res2[0], 1, "Expected duplicate structural AST view to reuse ast_id 1")
            self.assertEqual(self.te.next_id[t_ast.table_id], 2)

    @test_feature("broken_view_construction", depends_on=["view_set_decomposition"])
    def test_09_broken_view_construction(self) -> None:
        """Verify constructing broken views raises explicit exceptions identifying malformed components."""
        t_sec = self.test_tables.t_maintainer_section
        t_pat = self.test_tables.t_maintainer_pattern

        with test_step(
            "Asserting join graph with unregistered table ID raises KeyError",
            diagnostic="TableEngine must reject joins referencing table IDs not in self.tables with KeyError.",
        ):
            broken_joins: JoinsType = (((99999, 0), (t_pat.table_id, 0), 1),)
            valid_cols = (None,) * (t_sec.length + t_pat.length)
            with self.assertRaises(KeyError) as ctx:
                self.te.view_set(broken_joins, valid_cols)
            self.assertIn("99999", str(ctx.exception))

        with test_step(
            "Asserting empty join graph raises IndexError",
            diagnostic="TableEngine must reject empty join tuples joins=() with IndexError.",
        ):
            with self.assertRaises(IndexError):
                self.te.view_set((), (None, "data"))

        with test_step(
            "Asserting column count mismatch (columns length < required join length) raises IndexError",
            diagnostic="Passing fewer columns than required across the join graph must fail during decomposition.",
        ):
            valid_joins: JoinsType = (((t_sec.table_id, 0), (t_pat.table_id, 0), 1),)
            short_cols = (None, 1, "too_few_columns")
            with self.assertRaises(IndexError):
                self.te.view_set(valid_joins, short_cols)

    # =========================================================================
    # 4. Commit Protocol & Batch Transformation
    # =========================================================================

    @test_feature("commit_all_parallel_dispatch", depends_on=["set_and_get_staged"])
    def test_10_commit_all_parallel_dispatch(self) -> None:
        """Verify commit_all() flushes multi-table staged buffers via db.commit_tables_parallel()."""
        t_fn = self.test_tables.t_file_name
        t_blob = self.test_tables.t_file_blob

        with test_step("Staging operations across multiple tables"):
            self.te.set(t_fn.table_id, (None, "lib/string.c"))
            self.te.set(t_blob.table_id, (None, b"\x11" * 20, 1))
            self.te.update(t_fn.table_id, (10, "lib/string_opt.c"))

        with test_step("Calling commit_all() and asserting dispatch to commit_tables_parallel"):
            self.te.commit_all(max_workers=2)
            self.assertEqual(len(self.driver.parallel_commits), 1)
            commit_summary = self.driver.parallel_commits[0]
            comm_tbl_names = [item[0] for item in commit_summary]
            self.assertIn(t_fn.table_name, comm_tbl_names)
            self.assertIn(t_blob.table_name, comm_tbl_names)

        with test_step("Verifying all queued buffers are empty after commit_all()"):
            self.assertEqual(len(self.te.queued_set[t_fn.table_id]), 0)
            self.assertEqual(len(self.te.queued_set[t_blob.table_id]), 0)
            self.assertEqual(len(self.te.queued_update[t_fn.table_id]), 0)

    @test_feature("cryptographic_hash_presorting", depends_on=["duplicate_primary_key_rejection"])
    def test_11_cryptographic_hash_presorting(self) -> None:
        """Verify batch insert payloads on tables with hash primary keys are pre-sorted in ascending order."""
        t_hash = self.test_tables.t_ast_hash

        with test_step("Staging rows with pseudo-random unsorted binary hashes"):
            hashes = [
                b"\xff" * 32,
                b"\x11" * 32,
                b"\x88" * 32,
                b"\x05" * 32,
            ]
            for h in hashes:
                self.te.set(t_hash.table_id, (h, 1))

        with test_step(
            "Calling commit() and verifying payload rows are sorted by primary key",
            diagnostic="TableEngine must pre-sort cryptographic hash batch payloads to optimize B+Tree leaf insertion.",
        ):
            self.te.commit(t_hash.table_id)
            self.assertEqual(len(self.driver.inserts), 1)
            _, payload = self.driver.inserts[0]
            pks = [row[0] for row in payload]
            self.assertEqual(pks, sorted(hashes), "Payload rows must be ordered in ascending primary key order")

    # =========================================================================
    # 5. Extended Cache Capabilities (TECachedDB specific)
    # =========================================================================

    @test_feature("cache_preload_startup", depends_on=["lifecycle_and_init"])
    def test_12_cache_preload_startup(self) -> None:
        """Verify TECachedDB preloading pushes selective column projection and version window predicates."""
        if not self.is_cached_engine:
            raise unittest.SkipTest(f"SKIPPED: Extended in-memory caching is not applicable to '{self.engine_name}'.")

        t_fn = self.test_tables.t_file_name
        t_hash = self.test_tables.t_ast_hash

        with test_step("Configuring mock preloaded rows in RecordingDriver"):
            fresh_driver = RecordingDriver()
            fresh_driver.preload_returns[t_fn.table_name] = [(1, "fs/ext4/super.c"), (2, "fs/ext4/inode.c")]
            fresh_driver.preload_returns[t_hash.table_name] = [(b"\x12" * 32, 1)]

        with test_step("Starting TECachedDB and asserting select_preload calls"):
            fresh_te = self.te_class()
            fresh_te.start(self.test_tables.tables_list, lambda: fresh_driver)

            # Assert select_preload was called for cached tables
            preload_tables = [item[0] for item in fresh_driver.preload_calls]
            self.assertIn(t_fn.table_name, preload_tables)
            self.assertIn(t_hash.table_name, preload_tables)

        with test_step("Verifying preloaded rows were populated in in-memory cache and indices"):
            cached_row = fresh_te.get(t_fn.table_id, (1, None))
            self.assertEqual(cached_row, (1, "fs/ext4/super.c"))
            fresh_te.close()

    @test_feature("zero_db_queries_on_cache_hits", depends_on=["cache_preload_startup"])
    def test_13_zero_db_queries_on_cache_hits(self) -> None:
        """Verify Primary Key, deduplication, and column index lookups hit in-memory without querying DB."""
        if not self.is_cached_engine:
            raise unittest.SkipTest(f"SKIPPED: Extended in-memory caching is not applicable to '{self.engine_name}'.")

        t_fn = self.test_tables.t_file_name

        with test_step("Preloading row into TECachedDB"):
            fresh_driver = RecordingDriver()
            fresh_driver.preload_returns[t_fn.table_name] = [(10, "init/main.c"), (11, "init/version.c")]
            fresh_te = self.te_class()
            fresh_te.start(self.test_tables.tables_list, lambda: fresh_driver)

        with test_step(
            "Querying by Primary Key asserts 0 DB select calls",
            diagnostic="Cached row lookup by primary key must hit _pk_index in O(1) without touching DB.",
        ):
            row_pk = fresh_te.get(t_fn.table_id, (10, None))
            self.assertEqual(row_pk, (10, "init/main.c"))
            self.assertEqual(len(fresh_driver.select_calls), 0)

        with test_step(
            "Querying by deduplication key (columns[1:]) asserts 0 DB select calls",
            diagnostic="Cached row lookup by unique data key must hit _nodup_index without touching DB.",
        ):
            row_nodup = fresh_te.get(t_fn.table_id, (None, "init/version.c"))
            self.assertEqual(row_nodup, (11, "init/version.c"))
            self.assertEqual(len(fresh_driver.select_calls), 0)

        fresh_te.close()

    @test_feature("cache_mutation_sync", depends_on=["zero_db_queries_on_cache_hits"])
    def test_14_cache_mutation_sync(self) -> None:
        """Verify set() and update() keep in-memory row storage and indices strictly synchronized."""
        if not self.is_cached_engine:
            raise unittest.SkipTest(f"SKIPPED: Extended in-memory caching is not applicable to '{self.engine_name}'.")

        t_fn = self.test_tables.t_file_name

        with test_step("Calling set() asserts immediate availability in cache and indices"):
            self.te.set(t_fn.table_id, (None, "kernel/panic.c"))
            self.assertIn(1, self.te._pk_index[t_fn.table_id])
            cached = self.te.get(t_fn.table_id, (1, None))
            self.assertEqual(cached, (1, "kernel/panic.c"))

        with test_step(
            "Calling update() asserts old index values are purged and new values re-indexed",
            diagnostic="update() must call _unindex_row and _index_row to maintain index consistency.",
        ):
            self.te.update(t_fn.table_id, (1, "kernel/panic_modified.c"))
            updated_row = self.te.get(t_fn.table_id, (1, None))
            self.assertEqual(updated_row, (1, "kernel/panic_modified.c"))
            self.assertIn(("kernel/panic_modified.c",), self.te._nodup_index[t_fn.table_id])

    @test_feature("cache_purge_and_teardown", depends_on=["zero_db_queries_on_cache_hits"])
    def test_15_cache_purge_and_teardown(self) -> None:
        """Verify clear_cache() and commit_all(update_in_mem_indexes=False) purge in-memory indices."""
        if not self.is_cached_engine:
            raise unittest.SkipTest(f"SKIPPED: Extended in-memory caching is not applicable to '{self.engine_name}'.")

        t_fn = self.test_tables.t_file_name

        with test_step("Populating cache and calling clear_cache()"):
            self.te.set(t_fn.table_id, (None, "ipc/mqueue.c"))
            self.assertGreater(len(self.te._cached_rows[t_fn.table_id]), 0)
            self.te.clear_cache()
            self.assertEqual(len(self.te._cached_rows), 0)
            self.assertEqual(len(self.te._pk_index), 0)

        with test_step(
            "Calling commit_all(update_in_mem_indexes=False) asserts immediate cache evacuation",
            diagnostic="When update_in_mem_indexes=False, TableEngine must clear in-memory caches on teardown.",
        ):
            self.te.set(t_fn.table_id, (None, "ipc/msg.c"))
            self.te.commit_all(update_in_mem_indexes=False)
            self.assertEqual(len(self.te._cached_rows), 0)

    @test_feature("cache_size_ceiling", depends_on=["cache_preload_startup"])
    def test_16_cache_size_ceiling(self) -> None:
        """Verify a cached table measured above `cache_max_bytes` is neither preloaded nor cached."""
        if not self.is_cached_engine:
            raise unittest.SkipTest(f"SKIPPED: Extended in-memory caching is not applicable to '{self.engine_name}'.")

        t_fn = self.test_tables.t_file_name
        t_hash = self.test_tables.t_ast_hash

        with test_step("Reporting one cached table above and one below the 1MB ceiling"):
            fresh_driver = RecordingDriver()
            fresh_driver.table_sizes[t_fn.table_name] = 4 * 1024 * 1024
            fresh_driver.table_sizes[t_hash.table_name] = 64 * 1024
            fresh_driver.preload_returns[t_fn.table_name] = [(1, "fs/ext4/super.c")]
            fresh_driver.preload_returns[t_hash.table_name] = [(b"\x12" * 32, 1)]
            fresh_te = self.te_class(cache_max_table_mb=1)
            fresh_te.start(self.test_tables.tables_list, lambda: fresh_driver)

        with test_step(
            "Asserting the oversized table is excluded and never preloaded",
            diagnostic="A table above the ceiling must report as uncached so every read takes the TEDirectDB path.",
        ):
            self.assertIn(t_fn.table_id, fresh_te._oversized)
            self.assertFalse(fresh_te._is_cached(t_fn))
            preload_tables = [item[0] for item in fresh_driver.preload_calls]
            self.assertNotIn(t_fn.table_name, preload_tables)

        with test_step(
            "Asserting each table was measured as it would be cached",
            diagnostic="The ceiling must be compared against the cached column projection, not the whole row.",
        ):
            measured = {name: cols for name, cols, _vid in fresh_driver.size_calls}
            self.assertEqual(measured.get(t_hash.table_name), t_hash.cached_columns)
            self.assertEqual(measured.get(t_fn.table_name), t_fn.cached_columns)

        with test_step("Asserting the table below the ceiling is still preloaded and cached"):
            self.assertNotIn(t_hash.table_id, fresh_te._oversized)
            self.assertTrue(fresh_te._is_cached(t_hash))
            self.assertIn(t_hash.table_name, [item[0] for item in fresh_driver.preload_calls])

        with test_step(
            "Asserting a read of the excluded table reaches the database",
            diagnostic="Without preloaded rows the engine must query the driver instead of answering None.",
        ):
            fresh_driver.select_returns[(t_fn.table_name, (1, None))] = (1, "fs/ext4/super.c")
            row = fresh_te.get(t_fn.table_id, (1, None))
            self.assertEqual(row, (1, "fs/ext4/super.c"))
            self.assertIn((t_fn.table_name, (1, None)), fresh_driver.select_calls)
            fresh_te.close()

        with test_step(
            "Asserting the ceiling is measured per start and lifts when the table fits again",
            diagnostic="A table back under the ceiling must be cached again on the next start_new_db().",
        ):
            small_driver = RecordingDriver()
            small_driver.table_sizes[t_fn.table_name] = 32 * 1024
            small_driver.preload_returns[t_fn.table_name] = [(1, "fs/ext4/super.c")]
            lifted_te = self.te_class(cache_max_table_mb=1)
            lifted_te.start(self.test_tables.tables_list, lambda: small_driver)
            self.assertEqual(lifted_te._oversized, {})
            self.assertTrue(lifted_te._is_cached(t_fn))
            self.assertEqual(lifted_te.get(t_fn.table_id, (1, None)), (1, "fs/ext4/super.c"))
            self.assertEqual(len(small_driver.select_calls), 0)
            lifted_te.close()

        with test_step(
            "Asserting no ceiling (0) never measures a table",
            diagnostic="cache_max_table_mb=0 must keep the previous behaviour: no table_size() calls at all.",
        ):
            off_driver = RecordingDriver()
            off_driver.table_sizes[t_fn.table_name] = 4 * 1024 * 1024
            off_te = self.te_class(cache_max_table_mb=0)
            off_te.start(self.test_tables.tables_list, lambda: off_driver)
            self.assertEqual(off_driver.size_calls, [])
            self.assertTrue(off_te._is_cached(t_fn))
            off_te.close()


class _CachedEngineCase(unittest.TestCase):
    """TECachedDB behaviour tested directly against a RecordingDriver (no generic engine dispatch)."""

    def setUp(self) -> None:
        self.tt = TestTableCollection(prefix="t_")
        self.driver = RecordingDriver()
        self.te = TECachedDB(hugepages="off")
        self.te.start(self.tt.tables_list, lambda: self.driver)

    def tearDown(self) -> None:
        self.te.close()


class TestNodupPrefetch(_CachedEngineCase):
    """Distributed lookups of new no-duplicate keys cost one database read each before prefetch_nodup()."""

    def setUp(self) -> None:
        super().setUp()
        self.te.distributed = True
        self.fn = self.tt.t_file_name
        self.blob = self.tt.t_file_blob

    def _reads(self) -> int:
        return len(self.driver.select_in_calls) + len(self.driver.select_calls)

    def test_prefetch_reads_in_chunks_and_lookups_need_no_read(self) -> None:
        self.te.NODUP_PREFETCH_CHUNK = 4
        paths = [f"drivers/f{i}.c" for i in range(10)]
        self.driver.db_rows[self.fn.table_name] = [(500 + i, p) for i, p in enumerate(paths[:3])]
        before = self.te.db_reads
        self.te.prefetch_nodup(self.fn.table_id, [(None, p) for p in paths])
        self.assertEqual(len(self.driver.select_in_calls), 3, "10 keys in chunks of 4: 3 reads")
        self.assertEqual(self.te.db_reads - before, 3, "prefetch reads are counted in db_reads")
        found = [self.te.get(self.fn.table_id, (None, p)) for p in paths]
        self.assertEqual(found[:3], [(500, paths[0]), (501, paths[1]), (502, paths[2])])
        self.assertEqual(found[3:], [None] * 7)
        self.assertEqual(self._reads(), 3, "every lookup was answered by the prefetch")
        self.te.prefetch_nodup(self.fn.table_id, [(None, p) for p in paths])
        self.assertEqual(self._reads(), 3, "keys already answered are not read again")

    def test_remaining_key_columns_are_matched(self) -> None:
        self.driver.db_rows[self.blob.table_name] = [(7, b"h" * 20, 1)]
        self.te.prefetch_nodup(self.blob.table_id, [(None, b"h" * 20, 1), (None, b"h" * 20, 2)])
        self.assertEqual(len(self.driver.select_in_calls), 1)
        self.assertEqual(self.te.get(self.blob.table_id, (None, b"h" * 20, 1)), (7, b"h" * 20, 1))
        self.assertIsNone(self.te.get(self.blob.table_id, (None, b"h" * 20, 2)))
        self.assertEqual(self._reads(), 1)

    def test_misses_last_until_the_tables_next_commit(self) -> None:
        self.te.prefetch_nodup(self.fn.table_id, [(None, "a.c"), (None, "b.c")])
        self.assertIsNone(self.te.get(self.fn.table_id, (None, "a.c")))
        self.assertEqual(self._reads(), 1)
        # A peer inserts a.c; our own commit of the table ends the remembered misses
        self.driver.select_returns[(self.fn.table_name, (None, "a.c"))] = (900, "a.c")
        self.te.set(self.fn.table_id, (None, "c.c"))
        self.te.commit_all()
        self.assertEqual(self.te.get(self.fn.table_id, (None, "a.c")), (900, "a.c"))
        a_reads = [c for c in self.driver.select_calls if c == (self.fn.table_name, (None, "a.c"))]
        self.assertEqual(len(a_reads), 1, "the miss was read again after the commit")

    def test_staged_key_is_found_and_leaves_the_misses(self) -> None:
        self.te.prefetch_nodup(self.fn.table_id, [(None, "new.c")])
        row = self.te.set(self.fn.table_id, (None, "new.c"))
        self.assertNotIn(("new.c",), self.te._nodup_neg.get(self.fn.table_id, set()))
        self.assertEqual(self.te.get(self.fn.table_id, (None, "new.c")), row)
        self.assertEqual(self.te.set(self.fn.table_id, (None, "new.c")), row)

    def test_not_distributed_is_a_no_op(self) -> None:
        self.te.distributed = False
        self.te.prefetch_nodup(self.fn.table_id, [(None, "x.c"), (None, "y.c")])
        self.assertEqual(self._reads(), 0)
        self.assertIsNone(self.te.get(self.fn.table_id, (None, "x.c")))
        self.assertEqual(self._reads(), 0, "a complete preloaded table answers misses itself")

    def test_direct_engine_prefetch_is_a_no_op(self) -> None:
        te = TEDirectDB()
        te.start(self.tt.tables_list, lambda: self.driver)
        te.distributed = True
        te.prefetch_nodup(self.fn.table_id, [(None, "x.c")])
        self.assertEqual(self.driver.select_in_calls, [])
        te.close()


class TestDistributedMissIsFinal(_CachedEngineCase):
    """Peers write the versions between the base and this node's own after the preload."""

    def test_only_own_and_base_versions_are_final(self) -> None:
        cp = self.tt.t_bridge_checkpoint
        self.te._preload_window[cp.table_id] = (0, 10)
        self.te.current_commit_id = 14
        final = {v: self.te._miss_is_final(cp.table_id, (v, 1, None), (v, 1)) for v in (9, 10, 12, 14, 15)}
        self.assertEqual(final, {9: False, 10: True, 12: True, 14: True, 15: True})
        self.te.distributed = True
        final = {v: self.te._miss_is_final(cp.table_id, (v, 1, None), (v, 1)) for v in (9, 10, 12, 14, 15)}
        self.assertEqual(final, {9: False, 10: True, 12: False, 14: True, 15: False})


class TestPreloadFailure(unittest.TestCase):
    """A failed preload became an empty cache with no database fallback (silently disabling deduplication)."""

    def setUp(self) -> None:
        self.tt = TestTableCollection(prefix="t_")
        self.driver = RecordingDriver()

    def test_hash_keyed_table_fails_the_start(self) -> None:
        t_hash = self.tt.t_ast_hash
        self.driver.preload_error = ({t_hash.table_name}, RuntimeError("lost connection"))
        te = TECachedDB(hugepages="off")
        with self.assertRaises(RuntimeError):
            te.start(self.tt.tables_list, lambda: self.driver)

    def test_other_table_is_read_from_the_database(self) -> None:
        fn = self.tt.t_file_name
        self.driver.preload_error = ({fn.table_name}, RuntimeError("timeout"))
        te = TECachedDB(hugepages="off")
        with self.assertLogs("table_engine.te_cached_db", "WARNING"):
            te.start(self.tt.tables_list, lambda: self.driver)
        self.assertFalse(te._is_cached(fn))
        self.driver.select_returns[(fn.table_name, (None, "mm/slab.c"))] = (77, "mm/slab.c")
        self.assertEqual(te.get(fn.table_id, (None, "mm/slab.c")), (77, "mm/slab.c"))
        self.assertIn((fn.table_name, (None, "mm/slab.c")), self.driver.select_calls)
        # The next start that preloads it caches it again
        self.driver.preload_error = None
        te.start_new_db(lambda: self.driver)
        self.assertTrue(te._is_cached(fn))
        te.close()


class TestResyncNextId(unittest.TestCase):
    def test_drops_the_leased_block_and_rereads_next_id(self) -> None:
        tt = TestTableCollection(prefix="t_")
        driver = RecordingDriver()
        te = TEDirectDB()
        te.start(tt.tables_list, lambda: driver)
        fn = tt.t_file_name
        te._id_cursor[fn.table_id], te._id_limit[fn.table_id] = 5, 9
        driver.next_ids[fn.table_name] = 40
        te.resync_next_id(fn)
        self.assertNotIn(fn.table_id, te._id_cursor)
        self.assertNotIn(fn.table_id, te._id_limit)
        self.assertEqual(te.set(fn.table_id, (None, "a.c"))[0], 40)
        te.distributed = True
        driver.next_ids[fn.table_name] = 99
        te._id_cursor[fn.table_id], te._id_limit[fn.table_id] = 50, 60
        te.resync_next_id(fn)
        self.assertNotIn(fn.table_id, te._id_cursor)
        self.assertEqual(te.next_id[fn.table_id], 41, "distributed mode leases a new block instead of re-reading")
        te.close()


# =============================================================================
# Dynamic Test Class Generation for all Detected TableEngine Backends
# =============================================================================

def _generate_table_engine_test_classes() -> None:
    """Discover all unique TableEngine classes in TABLE_ENGINE_MAP and synthesize TestCase classes."""
    unique_engines: dict[str, Type[TEDirectDB | TECachedDB]] = {}
    for alias, cls in TABLE_ENGINE_MAP.items():
        cls_name = cls.__name__
        if cls_name not in unique_engines:
            unique_engines[cls_name] = cls

    current_module = sys.modules[__name__]
    for engine_name, engine_cls in sorted(unique_engines.items()):
        test_class_name = f"Test_{engine_name}Engine"
        class_attrs = {
            "te_class": engine_cls,
            "engine_name": engine_name,
            "__doc__": f"Automated contract verification test suite for TableEngine backend '{engine_name}'.",
        }
        test_class = type(test_class_name, (GenericTableEngineTestCase, unittest.TestCase), class_attrs)
        setattr(current_module, test_class_name, test_class)


# Execute auto-generation upon module load
_generate_table_engine_test_classes()


if __name__ == "__main__":
    unittest.main(verbosity=2)
