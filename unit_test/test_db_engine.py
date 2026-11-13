"""unit_test/test_db_engine.py - Generic Contract Verification Suite for Database Engines.

Automatically discovers all database backends registered in db_engine.DB_ENGINES
(e.g., MariaDB, MockDB, and future engines like PostgreSQL, SQLite, DuckDB) and
synthesizes a dedicated TestCase class for each engine.

Contract coverage according to db_engine/DB_API.md:
1. Lifecycle, Session & Health: __init__, __enter__, __exit__, close, check_if_connected
2. DDL & Catalog Management: drop_table, create_table, test_tables
3. Sequence Tracking & Single-Row CRUD: get_next_id, insert, select, update, select_preload, table_size
4. Multi-Table Relational Views: view_select, view_select_multiple
5. Parallel Multi-Table Commits: commit_tables_parallel
6. Index Management: create_index, index_exists, remove_index, create_indexes, remove_indexes
7. Payload & Max Allowed Packet Resilience (for network/packet-bounded drivers)

Safety & Diagnostic Invariants:
- Uses 't_*' table prefix dynamically cloned from core.DBLayout.TABLES.
- Drops all 't_*' tables at startup and teardown, guaranteeing clean isolation.
- Every test uses granular `test_step` contexts with explicit diagnostics distinguishing
  mutation logic from query verification logic.
- If prerequisite features (e.g. 'insert') fail, downstream tests are labeled UNTESTABLE.
- Automatically reloads/resets the database state upon error to prevent test pollution.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from unittest.mock import MagicMock
from typing import Any, Type
import mysql.connector

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import core
from core.globalstuff import JoinsType
from core.TableHandling import Table
from core.DBLayout import m_ast
from db_engine import DB_ENGINES, BaseDBEngine
from db_engine import (
    Agg,
    AnyOf,
    Col,
    Cond,
    Const,
    Exists,
    Inc,
    InSelect,
    Join,
    NotExists,
    Select,
    Value,
    between,
    finished_ref,
    ge,
    gt,
    hash_prefix,
    ieq,
    is_null,
    le,
    len_lt,
    like,
    lt,
    ne,
    not_in,
    not_null,
)
from db_engine.DBHandling import MariaDB
from db_engine.mariadb_support import parse_server_setting
from db_engine.errors import PacketTooLargeError
from unit_test.harness import (
    TestTableCollection,
    test_feature,
    test_step,
    FeatureRegistry,
)


class GenericDBEngineTestCase:
    """Base generic contract test case for any BaseDBEngine driver implementation.
    
    Concrete subclasses inheriting from (GenericDBEngineTestCase, unittest.TestCase)
    are dynamically synthesized for each unique driver registered in DB_ENGINES.
    """

    engine_class: Type[BaseDBEngine]
    engine_name: str
    feature_registry: FeatureRegistry
    test_tables: TestTableCollection
    engine_available: bool = False
    db: Any = None

    # Capability flags introspected or overridden per engine
    has_network_socket: bool = False
    has_packet_bisection: bool = False
    supports_index_tracking: bool = False

    @classmethod
    def setUpClass(cls) -> None:
        """Initialize engine instance, verify connectivity, and clean test tables."""
        cls.test_tables = TestTableCollection(prefix="t_")
        cls.feature_registry = FeatureRegistry()

        # Introspect engine capabilities
        cls.has_network_socket = "concurrent_connections" in getattr(cls.engine_class, "base_capabilities", ())
        cls.has_packet_bisection = (cls.engine_name == "MariaDB")
        cls.supports_index_tracking = (cls.engine_name != "MockDB")

        try:
            # Instantiate driver (pass use_global=False if supported, e.g. MockDB)
            if cls.engine_name == "MockDB":
                cls.db = cls.engine_class(use_global=False)
                cls.engine_available = True
            else:
                cls.db = cls.engine_class()
                # Check connection handle if socket-based
                if hasattr(cls.db, "cnx") and cls.db.cnx:
                    cls.engine_available = bool(cls.db.cnx.is_connected())
                else:
                    cls.engine_available = True

            if cls.engine_available:
                # Clean any stale test tables from prior runs (run twice to test idempotence)
                cls.db.drop_table(cls.test_tables.tables_list)
                cls.db.drop_table(cls.test_tables.tables_list)
        except Exception as e:
            cls.engine_available = False
            cls.db = None
            print(f"\n[!] Database backend '{cls.engine_name}' unavailable for testing: {e}")

    @classmethod
    def tearDownClass(cls) -> None:
        """Drop all 't_*' test tables and release engine resources."""
        if cls.engine_available and cls.db is not None:
            try:
                cls.db.drop_table(cls.test_tables.tables_list)
            except Exception as e:
                print(f"[!] Warning: Failed cleaning test tables during tearDownClass for {cls.engine_name}: {e}")
            finally:
                if hasattr(cls.db, "close"):
                    cls.db.close()

    def setUp(self) -> None:
        """Guard against unreachable engine backend."""
        if not self.engine_available or self.db is None:
            raise unittest.SkipTest(f"UNTESTABLE: Backend '{self.engine_name}' is not reachable.")

    def reload_test_db(self) -> None:
        """Reload DB state on error: rollback dirty transactions and recreate test tables."""
        if not self.engine_available or self.db is None:
            return
        try:
            if hasattr(self.db, "cnx") and self.db.cnx and hasattr(self.db.cnx, "rollback"):
                self.db.cnx.rollback()
            if self.has_network_socket and hasattr(self.db, "check_if_connected"):
                self.db.check_if_connected()
            self.db.drop_table(self.test_tables.tables_list)
            self.db.create_table(self.test_tables.tables_list)
        except Exception as e:
            print(f"[!] DB Reload encountered an error on {self.engine_name}: {e}")

    # =========================================================================
    # 1. Lifecycle, Session & Health
    # =========================================================================

    @test_feature("lifecycle")
    def test_01_lifecycle_and_session(self) -> None:
        """Verify __init__, __enter__, __exit__, check_if_connected, and close."""
        with test_step("Instantiating isolated driver instance"):
            temp_db = self.engine_class(use_global=False) if self.engine_name == "MockDB" else self.engine_class()
            self.assertIsNotNone(temp_db, f"Failed to instantiate {self.engine_name}")

        with test_step("Verifying context manager entry (__enter__) and handle active state"):
            with temp_db as ctx:
                self.assertIs(ctx, temp_db)
                if self.has_network_socket:
                    ctx.check_if_connected()
                    self.assertTrue(ctx.cnx.is_connected(), "Expected active connection handle")

        with test_step("Verifying resource release via close()"):
            temp_db.close()

    # =========================================================================
    # 2. DDL & Catalog Management
    # =========================================================================

    @test_feature("drop_table", depends_on=["lifecycle"])
    def test_02_drop_table(self) -> None:
        """Verify drop_table safely handles both existing and non-existing test tables."""
        with test_step(
            "Executing drop_table on non-existent tables (idempotence verification)",
            diagnostic="drop_table must execute DROP TABLE IF EXISTS safely without raising errors.",
        ):
            self.db.drop_table(self.test_tables.tables_list)

        with test_step("Executing 2nd drop_table pass for double-drop idempotence"):
            self.db.drop_table(self.test_tables.tables_list)

    @test_feature("create_table", depends_on=["drop_table"])
    def test_03_create_table(self) -> None:
        """Verify create_table generates schema, primary/foreign keys, and seeds initial_insert."""
        with test_step("Executing create_table DDL for all registered tables"):
            self.db.create_table(self.test_tables.tables_list)

        with test_step("Verifying table existence in database catalog via test_tables"):
            missing = self.db.test_tables(self.test_tables.tables_list)
            self.assertIsNone(missing, f"Tables failed to create in DB catalog: {missing}")

        t_type = self.test_tables.t_type_descriptor
        with test_step(
            "Verifying initial_insert seed for C_Compound via db.select()",
            diagnostic=(
                "Failure at this sub-step indicates either: "
                "1) create_table() failed to seed table.initial_insert, OR "
                "2) db.select() query logic failed to retrieve the seeded row."
            ),
        ):
            res = self.db.select(t_type, (None, "C_Compound"))
            self.assertIsNotNone(
                res,
                "Expected initial_insert seed for C_Compound in t_type_descriptor. "
                "Note: If create_table succeeded, the failure may stem from db.select() query/filter logic."
            )

    @test_feature("test_tables", depends_on=["create_table"])
    def test_04_test_tables(self) -> None:
        """Verify test_tables verifies existence of registered tables and reports missing ones."""
        with test_step("Verifying all registered test tables are reported as present"):
            missing = self.db.test_tables(self.test_tables.tables_list)
            self.assertIsNone(missing, f"Expected all tables to exist, but reported missing: {missing}")

        with test_step("Verifying test_tables correctly reports a non-existent table"):
            fake_table = MagicMock()
            fake_table.table_name = "t_non_existent_dummy_table_xyz"
            missing_fake = self.db.test_tables([fake_table])
            self.assertEqual(
                missing_fake,
                ["t_non_existent_dummy_table_xyz"],
                "Expected test_tables to report missing non-existent table name.",
            )

    # =========================================================================
    # 3. Sequence Tracking & Single-Row CRUD
    # =========================================================================

    @test_feature("get_next_id", depends_on=["create_table"])
    def test_05_get_next_id(self) -> None:
        """Verify get_next_id calculates COALESCE(MAX(pk), 0) + 1."""
        t_ver = self.test_tables.t_v_main
        with test_step("Querying get_next_id for t_v_main (seeded with ID 0)"):
            next_id = self.db.get_next_id(t_ver)
            self.assertEqual(next_id, 1, "Expected next_id to be 1 for t_v_main having seed row 0")

        with test_step(
            "Inserting row ID 1 and verifying sequence increment via get_next_id",
            diagnostic="If this step fails, either insert() failed to commit or get_next_id() failed to query MAX(pk).",
        ):
            self.db.insert(t_ver, ((1, 0, 1, "v3.0", 0, None, None, None),))
            next_id_after = self.db.get_next_id(t_ver)
            self.assertEqual(next_id_after, 2, "Expected next_id to increment to 2 after inserting ID 1")

    @test_feature("insert", depends_on=["create_table"])
    def test_06_insert(self) -> None:
        """Verify batch insert with single and multiple row payloads."""
        t_ver = self.test_tables.t_v_main
        t_fn = self.test_tables.t_file_name

        with test_step("Batch inserting multiple rows into t_v_main"):
            ver_rows = (
                (10, 0, 1, "v4.0", 0, None, None, None),
                (11, 0, 1, "v4.1", 0, None, None, None),
                (12, 0, 1, "v4.2", 0, None, None, None),
            )
            self.db.insert(t_ver, ver_rows)

        with test_step("Batch inserting multiple rows into t_file_name"):
            fn_rows = (
                (100, "kernel/sched/core.c"),
                (101, "fs/ext4/super.c"),
                (102, "net/ipv4/tcp.c"),
            )
            self.db.insert(t_fn, fn_rows)

    @test_feature("select", depends_on=["insert"])
    def test_07_select(self) -> None:
        """Verify single-row select with wildcard column filters."""
        t_ver = self.test_tables.t_v_main
        t_fn = self.test_tables.t_file_name

        with test_step(
            "Querying single row by primary key",
            diagnostic="Failure indicates select() PK filter matching or parameterized query execution failed.",
        ):
            row = self.db.select(t_ver, (10,) + (None,) * 7)
            self.assertIsNotNone(row, "Expected row (10, 'v4.0') to be found by PK")
            self.assertEqual(row, (10, 0, 1, "v4.0", 0, None, None, None))

        with test_step(
            "Querying single row by secondary column with wildcard None in PK position",
            diagnostic="Failure indicates wildcard matching logic in select() failed.",
        ):
            row_fn = self.db.select(t_fn, (None, "fs/ext4/super.c"))
            self.assertIsNotNone(row_fn, "Expected row to be found by path string")
            self.assertEqual(row_fn, (101, "fs/ext4/super.c"))

        with test_step("Querying non-existent record asserting None return"):
            row_none = self.db.select(t_ver, (9999, None))
            self.assertIsNone(row_none, "Expected non-existent query to return None")

    @test_feature("duplicate_primary_key_rejection", depends_on=["insert"])
    def test_07b_duplicate_primary_key_rejection(self) -> None:
        """Verify insert() raises ValueError upon duplicate primary key collision."""
        t_fn = self.test_tables.t_file_name

        with test_step("Inserting initial unique record"):
            self.db.insert(t_fn, ((900, "arch/x86/kernel/setup.c"),))

        with test_step(
            "Asserting duplicate insert on existing primary key raises ValueError",
            diagnostic="Database engine must catch duplicate primary key collisions and raise ValueError.",
        ):
            with self.assertRaises(ValueError) as ctx:
                self.db.insert(t_fn, ((900, "arch/x86/kernel/setup_dup.c"),))
            self.assertIn("duplicate", str(ctx.exception).lower())

    @test_feature("update", depends_on=["insert"])
    def test_08_update(self) -> None:
        """Verify batch upsert (ON DUPLICATE KEY UPDATE) modifies non-PK columns."""
        t_ver = self.test_tables.t_v_main

        with test_step("Executing batch update() with modified version names"):
            update_rows = (
                (11, 0, 1, "v4.1-rc1", 0, None, None, None),
                (12, 0, 1, "v4.2-final", 0, None, None, None),
            )
            self.db.update(t_ver, update_rows)

        with test_step(
            "Querying updated row (ID 11) to assert persisted modification",
            diagnostic="Failure indicates either update() did not perform upsert or select() returned stale data.",
        ):
            row1 = self.db.select(t_ver, (11,) + (None,) * 7)
            self.assertIsNotNone(row1)
            self.assertEqual(row1, (11, 0, 1, "v4.1-rc1", 0, None, None, None))

        with test_step("Querying updated row (ID 12) to assert persisted modification"):
            row2 = self.db.select(t_ver, (12,) + (None,) * 7)
            self.assertIsNotNone(row2)
            self.assertEqual(row2, (12, 0, 1, "v4.2-final", 0, None, None, None))

    @test_feature("select_preload", depends_on=["insert"])
    def test_09_select_preload(self) -> None:
        """Verify select_preload with column projection and version filtering."""
        t_ver = self.test_tables.t_v_main

        with test_step("Executing select_preload with column projection (cached_columns=(3,))"):
            projected = self.db.select_preload(t_ver, cached_columns=(3,))
            self.assertGreaterEqual(len(projected), 3, "Expected at least 3 version records in preload")
            self.assertTrue(all(len(r) == 1 for r in projected), "Expected projected rows to contain exactly 1 column")

        with test_step("Executing select_preload with version filtering (min_vid=11)"):
            filtered = self.db.select_preload(t_ver, cached_columns=(0, 3), min_vid=11)
            self.assertTrue(all(r[0] >= 11 for r in filtered), "Expected all rows to satisfy vid >= 11 filter")

    @test_feature("table_size", depends_on=["insert"])
    def test_09b_table_size(self) -> None:
        """Verify table_size reports a table's size and scales it to a column projection."""
        t_fn = self.test_tables.t_file_name

        with test_step("Querying the full size of t_file_name"):
            full = self.db.table_size(t_fn)
            self.assertIsInstance(full, int)
            self.assertGreaterEqual(full, 0, "Expected a non-negative size for an existing table")

        with test_step(
            "Querying the size of every column and asserting it equals the full size",
            diagnostic="A projection covering the whole table must not be scaled down.",
        ):
            self.assertEqual(self.db.table_size(t_fn, tuple(range(t_fn.length))), full)

        with test_step(
            "Querying the size of the primary key column alone",
            diagnostic="A narrower projection must never be reported as larger than the table itself.",
        ):
            projected = self.db.table_size(t_fn, (0,))
            self.assertGreaterEqual(projected, 0)
            self.assertLessEqual(projected, full, "Expected the projected size to stay within the table's size")

    # =========================================================================
    # 4. Multi-Table Relational Views
    # =========================================================================

    @test_feature("view_select", depends_on=["insert"])
    def test_10_view_select_single_and_multiple(self) -> None:
        """Verify multi-table relational view_select and view_select_multiple."""
        t_sec = self.test_tables.t_maintainer_section
        t_pat = self.test_tables.t_maintainer_pattern

        t_blob = self.test_tables.t_file_blob
        with test_step("Seeding parent sections and child patterns for join query"):
            self.db.insert(t_blob, (
                (1, b"a" * 20, 1),
            ))
            self.db.insert(t_sec, (
                (1, 1, "EXT4 FILE SYSTEM", "Supported", "git://ext4", "https://ext4.org", "linux-ext4@vger", 0, None, None, None, None),
                (2, 1, "BTRFS FILE SYSTEM", "Maintained", "git://btrfs", "https://btrfs.org", "linux-btrfs@vger", 0, None, None, None, None),
            ))
            self.db.insert(t_pat, (
                (1, 1, "fs/ext4/*", 10),
                (1, 1, "include/trace/events/ext4.h", 20),
                (2, 1, "fs/btrfs/*", 10),
            ))

        tables_dict = {t.table_id: t for t in self.test_tables.tables_list}
        joins: JoinsType = (
            ((t_sec.table_id, 0), (t_pat.table_id, 0), 1),
        )

        with test_step(
            "Executing joined single-row query via view_select() matching pattern column",
            diagnostic="Failure indicates join query generation or multi-table column mapping failed.",
        ):
            query_cols = [None] * (t_sec.length + t_pat.length)
            query_cols[t_sec.length + 2] = "fs/ext4/*"
            res_single = self.db.view_select(tables_dict, joins, tuple(query_cols))
            self.assertIsNotNone(res_single, "Expected view_select to return joined row")
            self.assertEqual(res_single[0], 1)
            self.assertEqual(res_single[2], "EXT4 FILE SYSTEM")

        with test_step("Executing joined multi-row query via view_select_multiple() matching section ID 1"):
            query_cols_multi = [None] * (t_sec.length + t_pat.length)
            query_cols_multi[0] = 1
            res_multi = self.db.view_select_multiple(tables_dict, joins, tuple(query_cols_multi))
            self.assertEqual(len(res_multi), 2, "Expected exactly 2 pattern rows for section ID 1")

    # =========================================================================
    # 5. Parallel Multi-Table Commits
    # =========================================================================

    @test_feature("commit_tables_parallel", depends_on=["create_table"])
    def test_11_commit_tables_parallel(self) -> None:
        """Verify commit_tables_parallel flushes inserts and updates across thread workers."""
        t_fn = self.test_tables.t_file_name
        t_person = self.test_tables.t_maintainer_person

        tables_data = [
            (
                t_fn,
                [(500, "drivers/net/ethernet/intel/e1000/e1000_main.c"), (501, "drivers/net/wireless/ath/ath9k/main.c")],
                [(500, "drivers/net/ethernet/intel/e1000/e1000_core.c")],
            ),
            (
                t_person,
                [(1000, "Alice Tester", "alice@example.com"), (1001, "Bob Reviewer", "bob@example.com")],
                [(1000, "Alice Senior Tester", "alice.senior@example.com")],
            ),
        ]

        with test_step("Executing commit_tables_parallel across 2 worker threads"):
            self.db.commit_tables_parallel(tables_data, max_workers=2)

        with test_step(
            "Verifying updated record in t_file_name was committed",
            diagnostic="Failure indicates commit_tables_parallel worker did not persist or commit t_file_name changes.",
        ):
            fn_row = self.db.select(t_fn, (500, None))
            self.assertIsNotNone(fn_row)
            self.assertEqual(fn_row, (500, "drivers/net/ethernet/intel/e1000/e1000_core.c"))

        with test_step(
            "Verifying updated record in t_maintainer_person was committed",
            diagnostic="Failure indicates commit_tables_parallel worker did not persist or commit t_maintainer_person changes.",
        ):
            person_row = self.db.select(t_person, (1000, None, None))
            self.assertIsNotNone(person_row)
            self.assertEqual(person_row, (1000, "Alice Senior Tester", "alice.senior@example.com"))

    # =========================================================================
    # 6. Index Management (Single & Parallel)
    # =========================================================================

    @test_feature("indexes_single", depends_on=["create_table"])
    def test_12_indexes_single(self) -> None:
        """Verify create_index, index_exists, and remove_index."""
        t_fn = self.test_tables.t_file_name
        idx_name = "idx_test_fname"

        with test_step("Ensuring index does not exist initially"):
            self.db.remove_index(idx_name, t_fn)
            self.assertFalse(self.db.index_exists(idx_name, t_fn))

        with test_step("Creating single-column index via create_index"):
            self.db.create_index(idx_name, t_fn, ((t_fn.table_id, 1),))
            if self.supports_index_tracking:
                self.assertTrue(self.db.index_exists(idx_name, t_fn), "Expected index_exists to return True")

        with test_step("Verifying duplicate create_index is idempotent and safe"):
            self.db.create_index(idx_name, t_fn, ((t_fn.table_id, 1),))

        with test_step("Dropping index via remove_index"):
            self.db.remove_index(idx_name, t_fn)
            self.assertFalse(self.db.index_exists(idx_name, t_fn))

    @test_feature("indexes_parallel", depends_on=["create_table"])
    def test_13_indexes_parallel(self) -> None:
        """Verify create_indexes and remove_indexes across worker connections."""
        t_sec = self.test_tables.t_maintainer_section
        t_pat = self.test_tables.t_maintainer_pattern

        indexes = [
            ("idx_test_sec_name", t_sec, ((t_sec.table_id, 3),)),
            ("idx_test_pat_pattern", t_pat, ((t_pat.table_id, 2),)),
        ]

        with test_step("Executing create_indexes in parallel across worker connections"):
            self.db.create_indexes(indexes, max_workers=2)
            if self.supports_index_tracking:
                self.assertTrue(self.db.index_exists("idx_test_sec_name", t_sec))
                self.assertTrue(self.db.index_exists("idx_test_pat_pattern", t_pat))

        with test_step("Executing remove_indexes in parallel across worker connections"):
            remove_list = [
                ("idx_test_sec_name", t_sec),
                ("idx_test_pat_pattern", t_pat),
            ]
            self.db.remove_indexes(remove_list, max_workers=2)
            self.assertFalse(self.db.index_exists("idx_test_sec_name", t_sec))
            self.assertFalse(self.db.index_exists("idx_test_pat_pattern", t_pat))

    # =========================================================================
    # 7. Payload & Max Allowed Packet Resilience (Network/Packet-Bounded Drivers)
    # =========================================================================

    @test_feature("payload_handled_bisection", depends_on=["create_table"])
    def test_14_payload_handled_bisection(self) -> None:
        """Verify multi-row packet limit error (OperationalError 1153) recovers via bisection."""
        if not self.has_packet_bisection:
            raise unittest.SkipTest(f"SKIPPED: Packet size bisection is not applicable to backend '{self.engine_name}'.")

        t_ast = self.test_tables.t_ast
        t_debug = self.test_tables.t_ast_debug
        self.db.insert(t_ast, tuple((10 + i, f"node_{i}", 1) for i in range(6)))

        original_executemany = self.db._cur.executemany
        call_count = 0

        def simulated_executemany(sql: str, chunk: list[tuple]) -> None:
            nonlocal call_count
            call_count += 1
            if len(chunk) >= 4:
                raise mysql.connector.errors.OperationalError(
                    errno=1153, msg="Got a packet bigger than 'max_allowed_packet' bytes"
                )
            original_executemany(sql, chunk)

        self.db._cur.executemany = simulated_executemany
        try:
            with test_step(
                "Executing insert() with 6 rows triggering simulated packet error 1153",
                diagnostic="Driver must recursively bisect chunk into halves (3 + 3) and complete both chunks.",
            ):
                sample_data = tuple(
                    (10 + i, f"int sample_code_{i}() {{ return {i}; }}")
                    for i in range(6)
                )
                self.db.insert(t_debug, sample_data)
                self.assertGreater(call_count, 1, "Expected bisection to execute multiple smaller batches")

            with test_step("Verifying rows were persisted after bisection recovery"):
                row = self.db.select(t_debug, (10, None))
                self.assertIsNotNone(row)
        finally:
            self.db._cur.executemany = original_executemany

    @test_feature("payload_unhandled_oversized", depends_on=["create_table"])
    def test_15_payload_unhandled_oversized(self) -> None:
        """Verify single atomic row exceeding packet limit cannot be bisected and raises unhandled exception."""
        if not self.has_packet_bisection:
            raise unittest.SkipTest(f"SKIPPED: Packet size bisection is not applicable to backend '{self.engine_name}'.")

        t_debug = self.test_tables.t_ast_debug

        with test_step(
            "Attempting insert of single atomic row exceeding max_allowed_packet",
            diagnostic=(
                "When a single atomic row exceeds max_allowed_packet, bisection cannot split (len(chunk) == 1). "
                "Driver must retry 3 times and re-raise the operational exception with clear context."
            ),
        ):
            mock_driver = object.__new__(self.engine_class)
            mock_driver.cnx = MagicMock()
            mock_driver._cur = MagicMock()
            mock_driver.check_if_connected = MagicMock()
            mock_driver.close = MagicMock()
            mock_driver._cur.executemany.side_effect = mysql.connector.errors.OperationalError(
                errno=1153, msg="Got a packet bigger than 'max_allowed_packet' bytes"
            )

            single_oversized = ((99, "UNHANDLED_OVERSIZED_PAYLOAD"),)
            with self.assertRaises(PacketTooLargeError) as ctx:
                mock_driver.insert(t_debug, single_oversized)
            self.assertIn("1153", str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, mysql.connector.Error)
            self.assertEqual(mock_driver._cur.executemany.call_count, 3, "Expected 3 retry attempts before raising")

    @test_feature("payload_byte_bounded_chunking", depends_on=["create_table"])
    def test_16_payload_byte_bounded_chunking(self) -> None:
        """Verify live multi-megabyte payloads in tables with LONGTEXT columns are smoothly chunked."""
        if not self.has_packet_bisection:
            raise unittest.SkipTest(f"SKIPPED: Byte-bounded live chunking test not configured for backend '{self.engine_name}'.")

        t_ast = self.test_tables.t_ast
        t_debug = self.test_tables.t_ast_debug
        self.db.insert(t_ast, tuple((200 + i, f"node_{i}", 1) for i in range(6)))

        with test_step(
            "Inserting 6 rows (~2.4MB total payload) into LONGTEXT table",
            diagnostic="Failure indicates byte estimation or batch splitting logic in insert() failed.",
        ):
            large_code_block = "/* payload chunk test */\n" + ("x" * 400_000)
            large_rows = tuple(
                (200 + i, f"{large_code_block}_{i}")
                for i in range(6)
            )
            self.db.insert(t_debug, large_rows)

        with test_step("Querying inserted large payload record to verify data integrity"):
            row = self.db.select(t_debug, (200, None))
            self.assertIsNotNone(row)
            self.assertEqual(len(row[1]), len(large_code_block) + 2)

    @test_feature("query_and_execute", depends_on=["create_table"])
    def test_20_query_and_execute(self) -> None:
        """Verify query, query_one, execute, executemany, commit, rollback contracts."""
        with test_step("Testing driver transaction commit and rollback handles"):
            self.db.commit()
            self.db.rollback()

        if self.engine_name == "MockDB":
            from db_engine import UnsupportedOperation

            with test_step("MockDB raw SQL raises UnsupportedOperation"):
                for call in (
                    lambda: self.db._raw_query("SELECT 1 AS val"),
                    lambda: self.db._raw_query_one("SELECT 1 AS val"),
                    lambda: list(self.db._raw_query_iter("SELECT 1")),
                    lambda: self.db._raw_execute("SELECT 1"),
                    lambda: self.db._raw_executemany("SELECT %s", [(1,)]),
                    lambda: self.db._raw_cursor(),
                ):
                    with self.assertRaises(UnsupportedOperation):
                        call()
            with test_step("MockDB schema helpers answer from the in-memory schema"):
                self.assertFalse(self.db.table_exists("t_no_such_table_xyz"))
                self.db.create_view("v_t_demo", "SELECT 1")
                self.assertTrue(self.db.table_exists("v_t_demo"))
                self.db.drop_view("v_t_demo")
                self.assertFalse(self.db.table_exists("v_t_demo"))
            return

        with test_step("Executing driver query and query_one"):
            rows = self.db._raw_query("SELECT 1 AS val")
            self.assertIsInstance(rows, list)
            row = self.db._raw_query_one("SELECT 1 AS val")
            if row is not None:
                val = row.get("val") if isinstance(row, dict) else row[0]
                self.assertEqual(val, 1)

        with test_step("Executing driver execute and executemany"):
            res = self.db._raw_execute("SELECT 1")
            self.assertGreaterEqual(res, 0)
            many_res = self.db._raw_executemany("SELECT %s", [(1,), (2,)])
            self.assertGreaterEqual(many_res, 0)

    @test_feature("spec_reads_and_conditions", depends_on=["create_table"])
    def test_21_spec_reads_and_conditions(self) -> None:
        """Verify spec read primitives, conditions, operators, joins, and dict mapping."""
        t_person = self.test_tables["t_maintainer_person"]
        self.db.delete_rows(t_person, where={"person_id": gt(0)})

        # Seed rows
        self.db.insert_rows(
            t_person,
            ("person_id", "name", "email"),
            [
                (1, "Alice Smith", "alice@example.com"),
                (2, "Bob Jones", "bob@kernel.org"),
                (3, "Charlie Brown", "charlie@example.com"),
                (4, "Dave Wilson", "dave@corp.net"),
            ],
        )

        with test_step("Verifying scalar conditions: ne, lt, le, gt, ge, between, not_in"):
            r_ne = self.db.select_rows(t_person, ("person_id",), where=[{"person_id": ne(1)}, {"person_id": gt(0)}])
            self.assertEqual(len(r_ne), 3)

            r_lt = self.db.select_rows(t_person, ("person_id",), where=[{"person_id": lt(3)}, {"person_id": gt(0)}])
            self.assertEqual(len(r_lt), 2)

            r_le = self.db.select_rows(t_person, ("person_id",), where=[{"person_id": le(3)}, {"person_id": gt(0)}])
            self.assertEqual(len(r_le), 3)

            r_gt = self.db.select_rows(t_person, ("person_id",), where={"person_id": gt(2)})
            self.assertEqual(len(r_gt), 2)

            r_ge = self.db.select_rows(t_person, ("person_id",), where={"person_id": ge(2)})
            self.assertEqual(len(r_ge), 3)

            r_btw = self.db.select_rows(t_person, ("person_id",), where={"person_id": between(2, 3)})
            self.assertEqual(len(r_btw), 2)

            r_nin = self.db.select_rows(t_person, ("person_id",), where=[{"person_id": not_in([1, 2])}, {"person_id": gt(0)}])
            self.assertEqual(len(r_nin), 2)

        with test_step("Verifying like, ieq, len_lt, and AnyOf"):
            r_like = self.db.select_rows(t_person, ("name",), where={"email": like("example.com", "contains")})
            self.assertEqual(len(r_like), 2)

            r_ieq = self.db.select_rows(t_person, ("name",), where={"name": ieq("aLiCe sMiTh")})
            self.assertEqual(len(r_ieq), 1)

            r_len = self.db.select_rows(t_person, ("name",), where=[{"name": len_lt(10)}, {"person_id": gt(0)}])
            self.assertEqual(len(r_len), 1)

            r_any = self.db.select_rows(t_person, ("name",), where=AnyOf({"person_id": 1}, {"person_id": 2}))
            self.assertEqual(len(r_any), 2)

        with test_step("Verifying read helpers: select_one, select_values, select_value, count_rows, row_exists"):
            one = self.db.select_one(t_person, ("person_id", "name"), where={"person_id": 1})
            self.assertIsNotNone(one)
            self.assertEqual(one[0], 1)

            vals = self.db.select_values(t_person, "person_id", where={"person_id": gt(0)}, order_by=("person_id",))
            self.assertEqual(vals, [1, 2, 3, 4])

            val = self.db.select_value(t_person, "name", where={"person_id": 2})
            self.assertEqual(val, "Bob Jones")

            cnt = self.db.count_rows(t_person, where={"person_id": gt(1)})
            self.assertEqual(cnt, 3)

            exists = self.db.row_exists(t_person, where={"person_id": 999})
            self.assertFalse(exists)
            self.assertTrue(self.db.row_exists(t_person, where={"person_id": 1}))

        with test_step("Verifying select_rows as_dict=True"):
            d_rows = self.db.select_rows(t_person, {"pid": "person_id", "pname": "name"}, where={"person_id": 1}, as_dict=True)
            self.assertEqual(len(d_rows), 1)
            self.assertEqual(d_rows[0]["pid"], 1)
            self.assertEqual(d_rows[0]["pname"], "Alice Smith")

    @test_feature("aggregates_and_group_by", depends_on=["spec_reads_and_conditions"])
    def test_22_aggregates_and_group_by(self) -> None:
        """Verify aggregate computations, min_value, and max_value."""
        t_person = self.test_tables["t_maintainer_person"]
        self.db.delete_rows(t_person, where={"person_id": gt(0)})
        self.db.insert_rows(
            t_person,
            ("person_id", "name", "email"),
            [(10, "User10", "u10@test.com"), (20, "User20", "u20@test.com"), (30, "User30", "u30@test.com")],
        )

        with test_step("Testing max_value and min_value"):
            mx = self.db.max_value(t_person, "person_id")
            mn = self.db.min_value(t_person, "person_id", where={"person_id": gt(0)})
            self.assertEqual(mx, 30)
            self.assertEqual(mn, 10)

        with test_step("Testing aggregate expressions"):
            aggs = self.db.aggregate(
                t_person,
                {"total": Agg("COUNT", "*"), "max_id": Agg("MAX", "person_id"), "sum_id": Agg("SUM", "person_id")},
                where={"person_id": gt(0)},
                as_dict=True,
            )
            self.assertEqual(len(aggs), 1)
            self.assertEqual(aggs[0]["total"], 3)
            self.assertEqual(aggs[0]["max_id"], 30)
            self.assertEqual(aggs[0]["sum_id"], 60)

    @test_feature("in_chunking_and_empty_list", depends_on=["spec_reads_and_conditions"])
    def test_23_in_chunking_and_empty_list(self) -> None:
        """Verify empty IN optimization and 1001+ auto-chunking."""
        t_person = self.test_tables["t_maintainer_person"]

        with test_step("Verifying empty IN list returns [] immediately"):
            res = self.db.select_rows(t_person, ("person_id",), where={"person_id": []})
            self.assertEqual(res, [])

        with test_step("Verifying 1001+ IN chunking"):
            oversize_ids = list(range(1, 1201))
            res = self.db.select_rows(t_person, ("person_id",), where={"person_id": oversize_ids}, distinct=True)
            self.assertIsInstance(res, list)

            # Refuse chunking with limit
            with self.assertRaises(ValueError):
                self.db.select_rows(t_person, ("person_id",), where={"person_id": oversize_ids}, limit=10)

    @test_feature("insert_rows_and_rowcounts", depends_on=["create_table"])
    def test_24_insert_rows_and_rowcounts(self) -> None:
        """Verify insert_rows modes and rowcount semantics."""
        t_person = self.test_tables["t_maintainer_person"]
        self.db.delete_rows(t_person, where={"person_id": gt(0)})

        with test_step("Testing plain insert and ignore mode"):
            rc = self.db.insert_rows(t_person, ("person_id", "name", "email"), [(1, "A", "a@test.com")])
            self.assertEqual(rc, 1)

            rc_ign = self.db.insert_rows(t_person, ("person_id", "name", "email"), [(1, "A2", "a@test.com")], on_conflict="ignore")
            self.assertEqual(rc_ign, 0)

        with test_step("Testing upsert (update mode) affected rowcount"):
            rc_up = self.db.insert_rows(
                t_person,
                ("person_id", "name", "email"),
                [(1, "A_Updated", "a@test.com")],
                on_conflict="update",
            )
            self.assertEqual(rc_up, 1, "upsert counts every offered row once on every backend")
            val = self.db.select_value(t_person, "name", where={"person_id": 1})
            self.assertEqual(val, "A_Updated")

        with test_step("Testing get_or_insert_id"):
            gid1 = self.db.get_or_insert_id(t_person, {"person_id": 1})
            self.assertEqual(gid1, 1)

    @test_feature("update_and_delete_rows", depends_on=["spec_reads_and_conditions"])
    def test_25_update_and_delete_rows(self) -> None:
        """Verify update_rows with Inc/Col and delete_rows with safety guards."""
        t_person = self.test_tables["t_maintainer_person"]
        self.db.delete_rows(t_person, where={"person_id": gt(0)})
        self.db.insert_rows(
            t_person,
            ("person_id", "name", "email"),
            [(100, "User100", "u100@test.com"), (200, "User200", "u200@test.com")],
        )

        with test_step("Testing update_rows with Inc and compare-and-set"):
            rc = self.db.update_rows(t_person, {"person_id": Inc(1)}, where={"person_id": 100})
            self.assertEqual(rc, 1)
            self.assertTrue(self.db.row_exists(t_person, where={"person_id": 101}))

        with test_step("Testing delete_rows with safety guard and limits"):
            with self.assertRaises(ValueError):
                self.db.delete_rows(t_person, where=None, all_rows=False)

            rc_del = self.db.delete_rows(t_person, where={"person_id": 101})
            self.assertEqual(rc_del, 1)
            self.assertEqual(self.db.count_rows(t_person, where={"person_id": gt(0)}), 1)

    @test_feature("copy_rows_and_transactions", depends_on=["create_table"])
    def test_26_copy_rows_and_transactions(self) -> None:
        """Verify copy_rows and transaction rollback."""
        t_person = self.test_tables["t_maintainer_person"]
        self.db.delete_rows(t_person, where={"person_id": gt(0)})
        self.db.insert_rows(t_person, ("person_id", "name", "email"), [(1, "User1", "u1@test.com")])

        with test_step("Testing transaction rollback"):
            try:
                with self.db.transaction():
                    self.db.insert_rows(t_person, ("person_id", "name", "email"), [(2, "User2", "u2@test.com")])
                    raise RuntimeError("Forced error for rollback")
            except RuntimeError:
                pass

            self.assertEqual(self.db.count_rows(t_person, where={"person_id": gt(0)}), 1)
            self.assertFalse(self.db.row_exists(t_person, where={"person_id": 2}))

    @test_feature("catalog_and_schema_maintenance", depends_on=["create_table"])
    def test_27_catalog_and_schema_maintenance(self) -> None:
        """Verify catalog queries, column_exists, existing_tables, and ensure_views."""
        t_person = self.test_tables["t_maintainer_person"]

        with test_step("Testing column_exists and column_info"):
            self.assertTrue(self.db.column_exists(t_person, "email"))
            self.assertFalse(self.db.column_exists(t_person, "non_existent_column_xyz"))
            info = self.db.column_info(t_person, "email")
            self.assertIsNotNone(info)

        with test_step("Testing existing_tables and table_stats"):
            existing = self.db.existing_tables([t_person.table_name, "definitely_not_a_table_123"])
            self.assertIn(t_person.table_name, existing)
            self.assertNotIn("definitely_not_a_table_123", existing)

            stats = self.db.table_stats([t_person.table_name])
            self.assertIsInstance(stats, list)

        with test_step("Testing server_variable and tuning defaults"):
            self.assertIsInstance(self.db.apply_server_tuning(), list)

    @test_feature("appendix_a_cases", depends_on=["spec_reads_and_conditions"])
    def test_28_appendix_a_cases(self) -> None:
        """Verify execution of the specific Appendix A specification queries."""
        t_bridge = self.test_tables["t_bridge_checkpoint"]
        t_task = self.test_tables["t_parse_task"]
        t_fn = self.test_tables["t_file_name"]
        t_blob = self.test_tables["t_file_blob"]
        self.db.delete_rows(t_bridge, where=None, all_rows=True)
        self.db.delete_rows(t_task, where=None, all_rows=True)

        # Seed foreign key targets for t_bridge
        self.db.insert_rows(t_fn, ("fnid", "fname"), [(10, "test10"), (20, "test20")], on_conflict="ignore")
        self.db.insert_rows(t_blob, ("blob_id", "blob_hash", "ftype"), [(100, b"1" * 20, 1), (200, b"2" * 20, 1)], on_conflict="ignore")

        # Seed t_bridge
        self.db.insert_rows(
            t_bridge,
            ("commit_id", "fnid", "blob_id"),
            [(1, 10, 100), (1, 20, 200), (2, 10, 100)],
        )

        with test_step("Appendix A: delete_rows with InSelect tuple form"):
            rc = self.db.delete_rows(
                t_bridge,
                [{"commit_id": 2}, InSelect(("fnid", "blob_id"), t_bridge, ("fnid", "blob_id"), where={"commit_id": 1})],
            )
            self.assertEqual(rc, 1)

        with test_step("Appendix A: count_rows with NotExists"):
            cnt = self.db.count_rows(
                (t_bridge, "p"),
                [{"p.commit_id": 1}, NotExists((t_bridge, "k"), on={"k.fnid": "p.fnid", "k.blob_id": "p.blob_id"}, where={"k.commit_id": 2})],
            )
            self.assertEqual(cnt, 2)

        with test_step("Appendix A: update_rows with AnyOf"):
            self.db.insert_rows(
                t_task,
                ("network_id", "chain_name", "chain_kind", "seq_order", "ref_name", "commit_id", "base_ref_name", "base_commit_id", "status"),
                [(0, "main", "main", 1, "v1", 10, "v0", 5, 0)],
            )
            up_rc = self.db.update_rows(
                t_task,
                {"status": 1, "base_commit_id": 10},
                [{"network_id": 0, "status": 0}, AnyOf({"base_commit_id": 5}, {"base_ref_name": "v0"})],
            )
            self.assertEqual(up_rc, 1)
            self.assertEqual(self.db.select_value(t_task, "status", where={"network_id": 0}), 1)


# =============================================================================
# MariaDB Driver Session, Reconnect, and Packet Bisection Verification
# =============================================================================

class _FakeCursor:
    def execute(self, *args, **kwargs) -> None:
        pass


class _FakeCnx:
    def __init__(self) -> None:
        self.connection_id = 1
        self.autocommit = True
        self.lost = False
        self.commits = 0
        self.rollbacks = 0

    def ping(self, reconnect=False, attempts=1, delay=0) -> None:
        if self.lost:
            if not reconnect:
                raise mysql.connector.InterfaceError("server gone")
            self.lost = False
            self.connection_id += 1          # mysql-connector reconnects silently with a new session

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _offline_mariadb() -> MariaDB:
    db = MariaDB.__new__(MariaDB)
    db.cnx = _FakeCnx()
    db._cur = _FakeCursor()
    db.sessions = 0

    def _init_session() -> None:
        db.sessions += 1

    db._init_session = _init_session
    return db


class TestMariaDBSessions(unittest.TestCase):
    def test_silent_reconnect_restores_session_settings(self) -> None:
        db = _offline_mariadb()
        db.cnx.lost = True
        db.check_if_connected()
        self.assertEqual(db.sessions, 1, "a reconnected session lacks sql_mode and the disabled checks")

    def test_transaction_fails_instead_of_reconnecting(self) -> None:
        db = _offline_mariadb()
        with self.assertRaises(ConnectionError):
            with db.transaction():
                self.assertFalse(db.cnx.autocommit)
                db.cnx.lost = True
                db.check_if_connected()
        self.assertEqual(db.cnx.rollbacks, 1)
        self.assertEqual(db.cnx.commits, 0)
        self.assertTrue(db.cnx.autocommit)
        self.assertFalse(db._in_txn)
        self.assertEqual(db.sessions, 0)

    def test_transaction_commits_once(self) -> None:
        db = _offline_mariadb()
        with db.transaction():
            pass
        self.assertEqual((db.cnx.commits, db.cnx.rollbacks), (1, 0))


class _TuningCursor:
    """Answers SELECT @@GLOBAL.x / SET GLOBAL x like a server that caps some variables."""

    def __init__(self, values: dict, caps: dict) -> None:
        self.values = dict(values)
        self.caps = caps
        self.row = None

    def execute(self, sql: str, params=None) -> None:
        m = re.match(r"SELECT @@GLOBAL\.(\w+)", sql)
        if m:
            if m.group(1) not in self.values:
                raise mysql.connector.ProgrammingError(msg=f"Unknown system variable '{m.group(1)}'")
            self.row = (self.values[m.group(1)],)
            return
        name = sql.split()[2]
        self.values[name] = min(params[0], self.caps[name]) if name in self.caps else params[0]

    def fetchone(self):
        return self.row


class TestServerTuning(unittest.TestCase):
    def _db(self, values: dict, caps: dict | None = None) -> MariaDB:
        db = _offline_mariadb()
        db._cur = _TuningCursor(values, caps or {})
        return db

    def test_sizes_parse_and_only_grow(self) -> None:
        self.assertEqual(parse_server_setting("8G"), 8 * 1024**3)
        self.assertEqual(parse_server_setting("O_DIRECT"), "O_DIRECT")
        db = self._db({"innodb_log_file_size": 4 * 1024**3, "innodb_flush_log_at_trx_commit": 1})
        notes = db.apply_server_tuning({"innodb_log_file_size": "2G", "innodb_flush_log_at_trx_commit": 2})
        self.assertEqual(notes, ["innodb_flush_log_at_trx_commit: 1 -> 2"])
        self.assertEqual(db._cur.values["innodb_log_file_size"], 4 * 1024**3)

    def test_capped_and_unknown_variables_are_reported(self) -> None:
        db = self._db({"innodb_buffer_pool_size": 128 * 1024**2}, caps={"innodb_buffer_pool_size": 128 * 1024**2})
        notes = db.apply_server_tuning({"innodb_buffer_pool_size": "8G", "no_such_var": 1, "bad name": 1})
        self.assertTrue(any("capped at 134217728" in n and "restart" in n for n in notes), notes)


class TestMaxAllowedPacketAdaptiveBisection(unittest.TestCase):
    """Test dynamic batch sizing and recursive bisection upon packet limit errors."""

    def setUp(self) -> None:
        self.t_code = Table(
            table_id=99,
            table_name="test_large_payload",
            columns=(
                ("hash", "BINARY(32)", "NOT NULL"),
                ("code", "LONGTEXT", "NOT NULL"),
            ),
            primary=("hash",),
            no_duplicate=False,
            te_cached=("hash",),
        )

    def test_adaptive_bisection_on_error_1153(self) -> None:
        """When executemany raises OperationalError 1153, insert() bisects chunks and succeeds."""
        maria = object.__new__(MariaDB)
        maria._cur = MagicMock()
        maria.cnx = MagicMock()
        maria.check_if_connected = MagicMock()
        maria.close = MagicMock()

        def mock_executemany(sql, chunk_data):
            if len(chunk_data) >= 4:
                err = mysql.connector.errors.OperationalError(
                    errno=1153, msg="Got a packet bigger than 'max_allowed_packet' bytes"
                )
                raise err

        maria._cur.executemany.side_effect = mock_executemany
        sample_data = tuple((b"\x01" * 32, f"int func_{i}() {{ return {i}; }}") for i in range(6))
        maria.insert(self.t_code, sample_data)
        self.assertGreater(maria._cur.executemany.call_count, 1)
        maria.cnx.commit.assert_called_once()

    def test_byte_bounded_chunking_large_payloads(self) -> None:
        """Tables with large text columns are chunked into smaller batches based on size."""
        maria = object.__new__(MariaDB)
        maria._cur = MagicMock()
        maria.cnx = MagicMock()
        maria.check_if_connected = MagicMock()
        maria.close = MagicMock()

        large_code = "A" * (500 * 1024)
        sample_data = tuple((b"\x02" * 32, large_code) for _ in range(10))
        maria.insert(self.t_code, sample_data)
        self.assertGreaterEqual(maria._cur.executemany.call_count, 2)



class TestMariaDBCursorMethodAndPoolConfig(unittest.TestCase):
    """`MariaDB._raw_cursor()` stays callable (the shared cursor lives in `_cur`); the pool never invents credentials."""

    def test_cursor_method_not_shadowed(self) -> None:
        from db_engine.DBHandling import MariaDB

        db = object.__new__(MariaDB)
        db.cnx = MagicMock()
        db._cur = MagicMock()
        db._in_txn = False
        db.check_if_connected = MagicMock()
        handle = db._raw_cursor(dictionary=True)
        db.cnx.cursor.assert_called_once_with(dictionary=True)
        self.assertIs(handle, db.cnx.cursor.return_value)

    def test_connect_without_credentials_is_a_clear_error(self) -> None:
        import os
        from unittest.mock import patch

        from db_engine import ConfigurationError
        from db_engine.DBHandling import MariaDB

        env = {k: v for k, v in os.environ.items() if k not in ("DB_USER", "DB_PASSWORD")}
        with patch.dict(os.environ, env, clear=True), patch("mysql.connector.connect") as connect:
            with self.assertRaises(ConfigurationError) as ctx:
                MariaDB()
        self.assertIn("database.user", str(ctx.exception))
        connect.assert_not_called()

    def test_pool_requires_injected_credentials(self) -> None:
        import db_engine.pool as pool

        saved = pool._CONFIG_PROVIDER
        try:
            pool._CONFIG_PROVIDER = None
            with self.assertRaises(pool.PoolConfigError):
                pool._create_pool()
            pool.configure_pool({"host": "127.0.0.1"})
            with self.assertRaises(pool.PoolConfigError):
                pool._create_pool()
        finally:
            pool._CONFIG_PROVIDER = saved


# =============================================================================
# Dynamic Test Class Generation for all Detected DB Engines
# =============================================================================

def _generate_engine_test_classes() -> None:
    """Discover all unique database driver classes in DB_ENGINES and synthesize TestCase classes."""
    unique_engines: dict[str, Type[BaseDBEngine]] = {}
    for alias, cls in DB_ENGINES.items():
        cls_name = cls.__name__
        if cls_name not in unique_engines:
            unique_engines[cls_name] = cls

    current_module = sys.modules[__name__]
    for engine_name, engine_cls in sorted(unique_engines.items()):
        test_class_name = f"Test_{engine_name}Engine"
        class_attrs = {
            "engine_class": engine_cls,
            "engine_name": engine_name,
            "__doc__": f"Automated contract verification test suite for database backend '{engine_name}'.",
        }
        test_class = type(test_class_name, (GenericDBEngineTestCase, unittest.TestCase), class_attrs)
        setattr(current_module, test_class_name, test_class)


# Execute auto-generation upon module load
class TestMariaDBReserveIdBlockSql(unittest.TestCase):
    """The MariaDB sequence allocator's statements (driver internals, moved from the distributed tests)."""

    def test_reserve_id_block_upsert_initialization(self):
        """Verify reserve_id_block executes sequence upsert before SELECT FOR UPDATE."""
        mock_cnx = MagicMock()
        mock_cur = MagicMock()
        mock_cnx.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (500,)

        class MockBaseEngine:
            def __init__(self, cnx):
                self.cnx = cnx

        engine = MockBaseEngine(mock_cnx)
        start, end = MariaDB.reserve_id_block(engine, m_ast, block_size=1000, commit_id=12)

        self.assertEqual(start, 500)
        self.assertEqual(end, 1499)

        exec_calls = mock_cur.execute.call_args_list
        self.assertIn("INSERT INTO m_table_sequence", exec_calls[0][0][0])
        # The upsert seeds a missing sequence row from MAX(pk) + 1 and never moves an existing one backwards
        self.assertIn("COALESCE(MAX(ast_id), 0) + 1 FROM m_ast", exec_calls[0][0][0])
        self.assertIn("ON DUPLICATE KEY UPDATE next_id = GREATEST(next_id, VALUES(next_id))", exec_calls[0][0][0])
        self.assertIn("SELECT next_id FROM m_table_sequence WHERE seq_table_id = %s FOR UPDATE", exec_calls[1][0][0])
        self.assertIn("UPDATE m_table_sequence SET next_id = %s WHERE seq_table_id = %s", exec_calls[2][0][0])
        self.assertEqual(exec_calls[2][0][1], (1500, m_ast.table_id))

    def test_reserve_id_block_disables_autocommit_during_lock(self):
        """Verify reserve_id_block sets autocommit=False during the lock and restores original autocommit."""
        mock_cnx = MagicMock()
        mock_cnx.autocommit = True
        mock_cur = MagicMock()
        mock_cnx.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (100,)

        autocommit_during_execute = []

        def track_execute(sql, params=()):
            autocommit_during_execute.append(mock_cnx.autocommit)

        mock_cur.execute.side_effect = track_execute

        class MockBaseEngine:
            def __init__(self, cnx):
                self.cnx = cnx

        engine = MockBaseEngine(mock_cnx)
        start, end = MariaDB.reserve_id_block(engine, m_ast, block_size=100, commit_id=5)

        self.assertEqual(start, 100)
        self.assertEqual(end, 199)
        self.assertTrue(len(autocommit_during_execute) >= 3)
        self.assertTrue(all(val is False for val in autocommit_during_execute))
        self.assertTrue(mock_cnx.autocommit)
        mock_cnx.commit.assert_called_once()

    def test_reserve_id_block_sql_advances_sequence_beyond_existing_max(self) -> None:
        from core.DBLayout import m_file_reference

        db = MariaDB.__new__(MariaDB)
        mock_cnx = MagicMock()
        mock_cursor = MagicMock()
        mock_cnx.cursor.return_value = mock_cursor
        db.cnx = mock_cnx

        executed = []
        mock_cursor.execute.side_effect = lambda sql, params=None: executed.append((sql, params))
        mock_cursor.fetchone.return_value = (50000,)

        start_id, end_id = db.reserve_id_block(m_file_reference, 1000)
        self.assertEqual(start_id, 50000)
        self.assertEqual(end_id, 50999)

        upsert_sql = executed[0][0]
        self.assertIn("ON DUPLICATE KEY UPDATE next_id = GREATEST(next_id, VALUES(next_id))", upsert_sql)

    def test_insert_sql_idempotent_tables_skip_only_existing_rows(self) -> None:
        from core.DBLayout import m_commit, m_commit_file
        sql = MariaDB._insert_sql(m_commit_file)
        self.assertTrue(sql.startswith("INSERT INTO `m_commit_file` VALUES"))
        self.assertTrue(sql.endswith("ON DUPLICATE KEY UPDATE `commit_id` = `commit_id`"))
        self.assertNotIn("ON DUPLICATE", MariaDB._insert_sql(m_commit))


_generate_engine_test_classes()


if __name__ == "__main__":
    unittest.main(verbosity=2)
