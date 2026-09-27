"""unit_test/test_db_engine.py - Generic Contract Verification Suite for Database Engines.

Automatically discovers all database backends registered in db_engine.DB_ENGINES
(e.g., MariaDB, MockDB, and future engines like PostgreSQL, SQLite, DuckDB) and
synthesizes a dedicated TestCase class for each engine.

Contract coverage according to db_engine/DB_API.md:
1. Lifecycle, Session & Health: __init__, __enter__, __exit__, close, check_if_connected
2. DDL & Catalog Management: drop_table, create_table, test_tables
3. Sequence Tracking & Single-Row CRUD: get_next_id, insert, select, update, select_preload
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
from db_engine import DB_ENGINES, BaseDBEngine
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
        cls.has_network_socket = hasattr(cls.engine_class, "check_if_connected")
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
            self.assertEqual(next_id, 1, "Expected next_id to be 1 for t_v_main having seed row 0 ('latest')")

        with test_step(
            "Inserting row ID 1 and verifying sequence increment via get_next_id",
            diagnostic="If this step fails, either insert() failed to commit or get_next_id() failed to query MAX(pk).",
        ):
            self.db.insert(t_ver, ((1, "v3.0"),))
            next_id_after = self.db.get_next_id(t_ver)
            self.assertEqual(next_id_after, 2, "Expected next_id to increment to 2 after inserting ID 1")

    @test_feature("insert", depends_on=["create_table"])
    def test_06_insert(self) -> None:
        """Verify batch insert with single and multiple row payloads."""
        t_ver = self.test_tables.t_v_main
        t_fn = self.test_tables.t_file_name

        with test_step("Batch inserting multiple rows into t_v_main"):
            ver_rows = (
                (10, "v4.0"),
                (11, "v4.1"),
                (12, "v4.2"),
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
            row = self.db.select(t_ver, (10, None))
            self.assertIsNotNone(row, "Expected row (10, 'v4.0') to be found by PK")
            self.assertEqual(row, (10, "v4.0"))

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
                (11, "v4.1-rc1"),
                (12, "v4.2-final"),
            )
            self.db.update(t_ver, update_rows)

        with test_step(
            "Querying updated row (ID 11) to assert persisted modification",
            diagnostic="Failure indicates either update() did not perform upsert or select() returned stale data.",
        ):
            row1 = self.db.select(t_ver, (11, None))
            self.assertIsNotNone(row1)
            self.assertEqual(row1, (11, "v4.1-rc1"))

        with test_step("Querying updated row (ID 12) to assert persisted modification"):
            row2 = self.db.select(t_ver, (12, None))
            self.assertIsNotNone(row2)
            self.assertEqual(row2, (12, "v4.2-final"))

    @test_feature("select_preload", depends_on=["insert"])
    def test_09_select_preload(self) -> None:
        """Verify select_preload with column projection and version filtering."""
        t_ver = self.test_tables.t_v_main

        with test_step("Executing select_preload with column projection (cached_columns=(1,))"):
            projected = self.db.select_preload(t_ver, cached_columns=(1,))
            self.assertGreaterEqual(len(projected), 3, "Expected at least 3 version records in preload")
            self.assertTrue(all(len(r) == 1 for r in projected), "Expected projected rows to contain exactly 1 column")

        with test_step("Executing select_preload with version filtering (min_vid=11)"):
            filtered = self.db.select_preload(t_ver, cached_columns=(0, 1), min_vid=11)
            self.assertTrue(all(r[0] >= 11 for r in filtered), "Expected all rows to satisfy vid >= 11 filter")

    # =========================================================================
    # 4. Multi-Table Relational Views
    # =========================================================================

    @test_feature("view_select", depends_on=["insert"])
    def test_10_view_select_single_and_multiple(self) -> None:
        """Verify multi-table relational view_select and view_select_multiple."""
        t_sec = self.test_tables.t_maintainer_section
        t_pat = self.test_tables.t_maintainer_pattern

        with test_step("Seeding parent sections and child patterns for join query"):
            self.db.insert(t_sec, (
                (1, 1, 0, "EXT4 FILE SYSTEM", "Supported", "git://ext4", "https://ext4.org", "linux-ext4@vger", 0),
                (2, 1, 0, "BTRFS FILE SYSTEM", "Maintained", "git://btrfs", "https://btrfs.org", "linux-btrfs@vger", 0),
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
            self.assertEqual(res_single[3], "EXT4 FILE SYSTEM")

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

        t_code = self.test_tables.t_tag_code
        original_executemany = self.db.cursor.executemany
        call_count = 0

        def simulated_executemany(sql: str, chunk: list[tuple]) -> None:
            nonlocal call_count
            call_count += 1
            if len(chunk) >= 4:
                raise mysql.connector.errors.OperationalError(
                    errno=1153, msg="Got a packet bigger than 'max_allowed_packet' bytes"
                )
            original_executemany(sql, chunk)

        self.db.cursor.executemany = simulated_executemany
        try:
            with test_step(
                "Executing insert() with 6 rows triggering simulated packet error 1153",
                diagnostic="Driver must recursively bisect chunk into halves (3 + 3) and complete both chunks.",
            ):
                sample_data = tuple(
                    (bytes([10 + i] * 32), f"int sample_code_{i}() {{ return {i}; }}")
                    for i in range(6)
                )
                self.db.insert(t_code, sample_data)
                self.assertGreater(call_count, 1, "Expected bisection to execute multiple smaller batches")

            with test_step("Verifying rows were persisted after bisection recovery"):
                row = self.db.select(t_code, (bytes([10] * 32), None))
                self.assertIsNotNone(row)
        finally:
            self.db.cursor.executemany = original_executemany

    @test_feature("payload_unhandled_oversized", depends_on=["create_table"])
    def test_15_payload_unhandled_oversized(self) -> None:
        """Verify single atomic row exceeding packet limit cannot be bisected and raises unhandled exception."""
        if not self.has_packet_bisection:
            raise unittest.SkipTest(f"SKIPPED: Packet size bisection is not applicable to backend '{self.engine_name}'.")

        t_code = self.test_tables.t_tag_code

        with test_step(
            "Attempting insert of single atomic row exceeding max_allowed_packet",
            diagnostic=(
                "When a single atomic row exceeds max_allowed_packet, bisection cannot split (len(chunk) == 1). "
                "Driver must retry 3 times and re-raise the operational exception with clear context."
            ),
        ):
            mock_driver = object.__new__(self.engine_class)
            mock_driver.cnx = MagicMock()
            mock_driver.cursor = MagicMock()
            mock_driver.check_if_connected = MagicMock()
            mock_driver.close = MagicMock()
            mock_driver.cursor.executemany.side_effect = mysql.connector.errors.OperationalError(
                errno=1153, msg="Got a packet bigger than 'max_allowed_packet' bytes"
            )

            single_oversized = ((bytes([99] * 32), "UNHANDLED_OVERSIZED_PAYLOAD"),)
            with self.assertRaises(mysql.connector.Error) as ctx:
                mock_driver.insert(t_code, single_oversized)
            self.assertIn("1153", str(ctx.exception))
            self.assertEqual(mock_driver.cursor.executemany.call_count, 3, "Expected 3 retry attempts before raising")

    @test_feature("payload_byte_bounded_chunking", depends_on=["create_table"])
    def test_16_payload_byte_bounded_chunking(self) -> None:
        """Verify live multi-megabyte payloads in tables with LONGTEXT columns are smoothly chunked."""
        if not self.has_packet_bisection:
            raise unittest.SkipTest(f"SKIPPED: Byte-bounded live chunking test not configured for backend '{self.engine_name}'.")

        t_code = self.test_tables.t_tag_code

        with test_step(
            "Inserting 6 rows (~2.4MB total payload) into LONGTEXT table",
            diagnostic="Failure indicates byte estimation or batch splitting logic in insert() failed.",
        ):
            large_code_block = "/* payload chunk test */\n" + ("x" * 400_000)
            large_rows = tuple(
                (bytes([200 + i] * 32), f"{large_code_block}_{i}")
                for i in range(6)
            )
            self.db.insert(t_code, large_rows)

        with test_step("Querying inserted large payload record to verify data integrity"):
            row = self.db.select(t_code, (bytes([200] * 32), None))
            self.assertIsNotNone(row)
            self.assertEqual(len(row[1]), len(large_code_block) + 2)


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
_generate_engine_test_classes()


if __name__ == "__main__":
    unittest.main(verbosity=2)
