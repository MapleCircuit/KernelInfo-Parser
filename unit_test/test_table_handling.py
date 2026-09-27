"""unit_test/test_table_handling.py - Comprehensive Unit Test Suite for core/TableHandling.py.

Verifies:
1. Data Sanitization & Normalization (`to_safe_data`, `is_data_unsafe`, `normalize_data_tuple`)
2. Table Class Schema Introspection & Operation Builders (`Table`, `set`, `update`, `get`, `get_set`, `view`, `view_get`, `view_get_multiple`, `ref_view`)
3. ChangeSet Context Routing & Staging Protocol (`ChangeSet`, `route_parse`, `store`, `ref`, context managers)
4. ChangeSet Reference Resolution (`resolve_ref`, `_resolve_ref_from_tuple`, foreign symbols, dependency blocking, circular stub generation)
5. ChangeSet Pipeline Execution & Lifecycle (`execute`, multi-pass dependency loop, `preprocess_ref_views`, `get_file_type`, `clear_bloat`, `register_bridge_map`)

Uses RecordingTableEngine stand-in for pure unit isolation and TestTableCollection with 't_' prefixes.
"""
from __future__ import annotations

import os
import pickle
import sys
import unittest
from enum import Enum, IntEnum
from typing import Any, Sequence

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import core.globalstuff as G_module
from core.globalstuff import G
import core
from core import (
    ASTT,
    OP_DONE,
    OP_REF,
    OP_REF_VIEW,
    OP_SET,
    OP_UPDATE,
    OP_VIEW_DONE,
    OP_VIEW_SET,
    REF_C_AST,
    REF_FILE,
    REF_MULTI,
    REF_NO_REF,
    REF_OLD,
    REF_POS,
    REF_ROOT,
    REF_NOT_RESOLVABLE,
    CONTINUE_EXCEPTION,
    T_ASM,
    T_C,
    T_CREDITS,
    T_KCONFIG,
    T_MAINTAINERS,
    T_RAW,
    T_RUST,
    JoinsType,
    SafeDataType,
    PointerGetter,
)
from core.TableHandling import (
    ChangeSet,
    Table,
    is_data_unsafe,
    normalize_data_tuple,
    to_safe_data,
)
from unit_test.harness import (
    FeatureRegistry,
    TestTableCollection,
    test_feature,
    test_step,
)


# =============================================================================
# Stand-in Recording TableEngine for Pure Unit Isolation
# =============================================================================

class RecordingTableEngine:
    """Mock TableEngine stand-in recording all calls and returning predictable synthetic data."""

    def __init__(self, tables_list: Sequence[Table] | None = None) -> None:
        self.tables: dict[int, Table] = {t.table_id: t for t in tables_list} if tables_list else {}
        self.get_calls: list[tuple[int, tuple]] = []
        self.set_calls: list[tuple[int, tuple]] = []
        self.update_calls: list[tuple[int, tuple]] = []
        self.view_get_calls: list[tuple[Any, tuple]] = []
        self.view_get_multiple_calls: list[tuple[Any, tuple]] = []
        self.view_set_calls: list[tuple[Any, tuple]] = []
        self.canned_get: dict[tuple[int, tuple], tuple] = {}
        self.canned_view_get: dict[tuple[Any, tuple], tuple] = {}
        self.canned_view_get_multiple: dict[tuple[Any, tuple], list[tuple]] = {}
        self.next_id: dict[int, int] = {}
        self.stored_records: dict[int, dict[Any, tuple]] = {}
        self.is_closed: bool = False

    def get(self, table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None:
        self.get_calls.append((table_id, columns))
        # 1. Canned lookup
        if (table_id, columns) in self.canned_get:
            return self.canned_get[(table_id, columns)]
        # 2. Check stored_records by exact match or PK match
        if table_id in self.stored_records:
            tbl = self.tables.get(table_id)
            if tbl and tbl.primary:
                pk_vals = tuple(columns[i] for i in tbl.primary if i < len(columns))
                if all(x is not None for x in pk_vals) and pk_vals in self.stored_records[table_id]:
                    return self.stored_records[table_id][pk_vals]
            for row in self.stored_records[table_id].values():
                match = True
                for i, val in enumerate(columns):
                    if val is not None and (i >= len(row) or row[i] != val):
                        match = False
                        break
                if match:
                    return row
        return None

    def set(self, table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]:
        self.set_calls.append((table_id, columns))
        tbl = self.tables.get(table_id)
        res_row = columns
        if columns and columns[0] is None:
            curr_id = self.next_id.get(table_id, 1)
            self.next_id[table_id] = curr_id + 1
            res_row = (curr_id, *columns[1:])

        if table_id not in self.stored_records:
            self.stored_records[table_id] = {}
        if tbl and tbl.primary:
            pk = tuple(res_row[i] for i in tbl.primary if i < len(res_row))
            self.stored_records[table_id][pk] = res_row
        else:
            self.stored_records[table_id][res_row[0]] = res_row
        return res_row

    def update(self, table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]:
        self.update_calls.append((table_id, columns))
        tbl = self.tables.get(table_id)
        if table_id not in self.stored_records:
            self.stored_records[table_id] = {}
        if tbl and tbl.primary:
            pk = tuple(columns[i] for i in tbl.primary if i < len(columns))
            self.stored_records[table_id][pk] = columns
        return columns

    def view_get(self, joins: Any, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None:
        self.view_get_calls.append((joins, columns))
        return self.canned_view_get.get((joins, columns))

    def view_get_multiple(self, joins: Any, columns: tuple[SafeDataType, ...]) -> list[tuple[SafeDataType, ...]] | None:
        self.view_get_multiple_calls.append((joins, columns))
        return self.canned_view_get_multiple.get((joins, columns), [])

    def view_set(self, joins: Any, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]:
        self.view_set_calls.append((joins, columns))
        if columns and columns[0] is None:
            first_tid = PointerGetter(joins).get_first_table_id() if hasattr(joins, "__iter__") else 0
            curr_id = self.next_id.get(first_tid, 1)
            self.next_id[first_tid] = curr_id + 1
            return (curr_id, *columns[1:])
        return columns

    def close(self) -> None:
        self.is_closed = True


# =============================================================================
# Base Test Case with TE Isolation and Schema Fixtures
# =============================================================================

class BaseTableHandlingTestCase(unittest.TestCase):
    """Base fixture setting up isolated RecordingTableEngine and test schemas."""
    feature_registry: FeatureRegistry
    test_tables: TestTableCollection
    mock_te: RecordingTableEngine
    old_te: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.feature_registry = FeatureRegistry()

    def setUp(self) -> None:
        self.test_tables = TestTableCollection(prefix="t_")
        self.mock_te = RecordingTableEngine(self.test_tables.tables_list)
        self.old_te = G.TE
        G.TE = self.mock_te

    def tearDown(self) -> None:
        G.TE = self.old_te
        if hasattr(self, "mock_te"):
            self.mock_te.close()


# =============================================================================
# 1. Data Sanitization & Normalization
# =============================================================================

class Test_DataSanitization(BaseTableHandlingTestCase):
    """Verification of primitive type coercion, enum unboxing, and reference detection."""

    @test_feature("to_safe_data_primitives")
    def test_01_primitive_types(self) -> None:
        """Verify to_safe_data correctly handles primitive scalars, booleans, and byte buffers."""
        with test_step("Testing primitive passthrough (int, str, bytes, None)"):
            self.assertEqual(to_safe_data(42), 42)
            self.assertEqual(to_safe_data("kernel_sym"), "kernel_sym")
            self.assertEqual(to_safe_data(b"sha256_bytes"), b"sha256_bytes")
            self.assertIsNone(to_safe_data(None))

        with test_step("Testing boolean conversion to int (True -> 1, False -> 0)"):
            self.assertIs(to_safe_data(True), 1)
            self.assertIs(to_safe_data(False), 0)

        with test_step("Testing bytearray and memoryview coercion to immutable bytes"):
            b_arr = bytearray(b"raw_bytes")
            m_view = memoryview(b"view_bytes")
            res_arr = to_safe_data(b_arr)
            res_view = to_safe_data(m_view)
            self.assertIsInstance(res_arr, bytes)
            self.assertEqual(res_arr, b"raw_bytes")
            self.assertIsInstance(res_view, bytes)
            self.assertEqual(res_view, b"view_bytes")

    @test_feature("to_safe_data_enums_and_objects", depends_on=["to_safe_data_primitives"])
    def test_02_enums_and_custom_objects(self) -> None:
        """Verify to_safe_data unboxes Enums, IntEnums, nested enums, and objects with .value."""
        class SampleStrEnum(Enum):
            FOO = "foo_val"

        class SampleIntEnum(IntEnum):
            BAR = 101

        class NestedEnum(Enum):
            INNER = SampleStrEnum.FOO

        class CustomValuedObject:
            def __init__(self, val: Any) -> None:
                self.value = val

        with test_step("Unboxing standard Enum and IntEnum to primitive values"):
            self.assertEqual(to_safe_data(SampleStrEnum.FOO), "foo_val")
            self.assertEqual(to_safe_data(SampleIntEnum.BAR), 101)
            self.assertIsInstance(to_safe_data(SampleIntEnum.BAR), int)
            self.assertEqual(to_safe_data(ASTT.C_struct), int(ASTT.C_struct))

        with test_step("Unboxing nested Enum instances"):
            self.assertEqual(to_safe_data(NestedEnum.INNER), "foo_val")

        with test_step("Unboxing custom objects providing a .value attribute"):
            obj_int = CustomValuedObject(999)
            obj_str = CustomValuedObject("custom_str")
            obj_bytes = CustomValuedObject(bytearray(b"obj_byte"))
            self.assertEqual(to_safe_data(obj_int), 999)
            self.assertEqual(to_safe_data(obj_str), "custom_str")
            self.assertEqual(to_safe_data(obj_bytes), b"obj_byte")

    @test_feature("is_data_unsafe_detection")
    def test_03_is_data_unsafe(self) -> None:
        """Verify is_data_unsafe detects tuples containing unresolved reference structures."""
        with test_step("Checking pure primitive tuples return False"):
            self.assertFalse(is_data_unsafe((1, "foo", None, b"bytes")))

        with test_step("Checking tuples containing nested tuples return True"):
            dummy_ref = ((1, 0), OP_REF, (REF_ROOT,))
            self.assertTrue(is_data_unsafe((dummy_ref,)))
            self.assertTrue(is_data_unsafe((1, "bar", dummy_ref, None)))
            self.assertTrue(is_data_unsafe(((1, 2),)))

    @test_feature("normalize_data_tuple", depends_on=["to_safe_data_primitives", "is_data_unsafe_detection"])
    def test_04_normalize_data_tuple(self) -> None:
        """Verify normalize_data_tuple coerces primitives while preserving RefType references intact."""
        dummy_ref = ((1, 0), OP_REF, (REF_ROOT,))

        with test_step("Normalizing mixed tuple with enums, bools, and RefType references"):
            input_tuple = (True, ASTT.C_Compound, dummy_ref, bytearray(b"abc"), None)
            normalized = normalize_data_tuple(input_tuple)

            self.assertEqual(normalized[0], 1)
            self.assertEqual(normalized[1], int(ASTT.C_Compound))
            self.assertEqual(normalized[2], dummy_ref, "Unresolved reference tuple must remain intact")
            self.assertEqual(normalized[3], b"abc")
            self.assertIsNone(normalized[4])


# =============================================================================
# 2. Table Schema Introspection & Operation Builders
# =============================================================================

class Test_TableSchemaAndOperations(BaseTableHandlingTestCase):
    """Verification of Table schema reflection, pointer attributes, and operation builders."""

    @test_feature("table_schema_introspection")
    def test_01_table_schema_introspection(self) -> None:
        """Verify dynamic column pointer attribute generation, PK detection, and auto_increment flag."""
        t_fn = self.test_tables.t_file_name
        t_bridge = self.test_tables.t_bridge_file

        with test_step("Verifying dynamic column pointer attributes on Table instance"):
            self.assertTrue(hasattr(t_fn, "fnid"))
            self.assertTrue(hasattr(t_fn, "fname"))
            self.assertEqual(t_fn.fnid, (t_fn.table_id, 0))
            self.assertEqual(t_fn.fname, (t_fn.table_id, 1))

        with test_step("Verifying has_auto_increment detection"):
            self.assertTrue(t_fn.has_auto_increment, "t_file_name should have auto-increment")
            self.assertFalse(t_bridge.has_auto_increment, "t_bridge_file composite PK should not have auto-increment")

        with test_step("Verifying composite primary key mapping"):
            # primary indices: vid=0, fnid=1
            self.assertEqual(t_bridge.primary, (0, 1))
            self.assertEqual(t_bridge.length, 3)

    @test_feature("table_set_operation", depends_on=["table_schema_introspection"])
    def test_02_table_set_operation_building(self) -> None:
        """Verify set() builds OP_SET operations and redirects to get_set() for no_duplicate tables."""
        t_file = self.test_tables.t_file
        t_fn = self.test_tables.t_file_name

        with test_step("Calling set() on regular table builds OP_SET operation"):
            op = t_file.set(None, 1, 0, 1, "M", "M")
            self.assertEqual(op[0], t_file.table_id)
            self.assertEqual(op[1], OP_SET)
            self.assertEqual(op[2], (None, 1, 0, 1, "M", "M"))

        with test_step("Calling set() on no_duplicate table automatically delegates to get_set()"):
            self.assertTrue(t_fn.no_duplicate)
            # Empty mock TE means get_set will construct OP_SET
            op_nodup = t_fn.set(None, "kernel/panic.c")
            self.assertEqual(op_nodup[0], t_fn.table_id)
            self.assertEqual(op_nodup[1], OP_SET)
            self.assertEqual(op_nodup[2], (None, "kernel/panic.c"))
            # Pre-populate TE cache and assert set() returns OP_DONE
            self.mock_te.canned_get[(t_fn.table_id, (None, "kernel/panic.c"))] = (10, "kernel/panic.c")
            op_existing = t_fn.set(None, "kernel/panic.c")
            self.assertEqual(op_existing[1], OP_DONE)
            self.assertEqual(op_existing[2], (10, "kernel/panic.c"))

    @test_feature("table_update_operation", depends_on=["table_schema_introspection"])
    def test_03_table_update_expansion(self) -> None:
        """Verify update() builds OP_UPDATE and expands partial rows with None values via G.TE.get()."""
        t_file = self.test_tables.t_file

        with test_step("Full column tuple update"):
            op_full = t_file.update(5, 1, 2, 1, "M", "M")
            self.assertEqual(op_full[0], t_file.table_id)
            self.assertEqual(op_full[1], OP_UPDATE)
            self.assertEqual(op_full[2], (5, 1, 2, 1, "M", "M"))

        with test_step("Partial update with None values triggers G.TE.get to backfill existing values"):
            # Set canned existing record in mock TE: (fid=5, vid_s=1, vid_e=0, ftype=1, s_stat='M', e_stat='M')
            self.mock_te.canned_get[(t_file.table_id, (5, None, None, None, None, None))] = (5, 1, 0, 1, "M", "M")
            # Update only vid_e=2 and e_stat='D' (fid=5, vid_s=None, vid_e=2, ftype=None, s_stat=None, e_stat='D')
            op_partial = t_file.update(5, None, 2, None, None, "D")
            self.assertEqual(op_partial[1], OP_UPDATE)
            # Expect backfilled: (5, 1, 2, 1, 'M', 'D')
            self.assertEqual(op_partial[2], (5, 1, 2, 1, "M", "D"))

        with test_step("Unsafe reference in update tuple bypasses G.TE.get"):
            dummy_ref = ((t_file.table_id, 0), OP_REF, (REF_ROOT,))
            op_unsafe = t_file.update(dummy_ref, None, 2, None, None, "D")
            self.assertEqual(op_unsafe[1], OP_UPDATE)
            self.assertEqual(op_unsafe[2][0], dummy_ref)

    @test_feature("table_get_operation", depends_on=["table_schema_introspection"])
    def test_04_table_get_and_unsafe_crash_guard(self) -> None:
        """Verify get() queries G.TE.get returning OP_DONE or None, and crashes on unsafe refs."""
        t_fn = self.test_tables.t_file_name

        with test_step("Querying non-existent record returns None"):
            res_none = t_fn.get(None, "non_existent.c")
            self.assertIsNone(res_none)

        with test_step("Querying existing record returns (table_id, OP_DONE, row)"):
            self.mock_te.canned_get[(t_fn.table_id, (None, "fs/read_write.c"))] = (44, "fs/read_write.c")
            res_found = t_fn.get(None, "fs/read_write.c")
            self.assertIsNotNone(res_found)
            self.assertEqual(res_found, (t_fn.table_id, OP_DONE, (44, "fs/read_write.c")))

        with test_step("Passing unsafe reference tuple into get() triggers emergency shutdown"):
            dummy_ref = ((t_fn.table_id, 0), OP_REF, (REF_ROOT,))
            with self.assertRaises(SystemExit):
                t_fn.get(dummy_ref, "crash.c")

    @test_feature("table_get_set_operation", depends_on=["table_get_operation"])
    def test_05_table_get_set(self) -> None:
        """Verify get_set() returns OP_DONE if match exists, OP_SET if match missing, and safely passes refs."""
        t_fn = self.test_tables.t_file_name

        with test_step("Missing record returns OP_SET"):
            op_set = t_fn.get_set(None, "init/main.c")
            self.assertEqual(op_set[1], OP_SET)
            self.assertEqual(op_set[2], (None, "init/main.c"))

        with test_step("Existing record returns OP_DONE"):
            self.mock_te.canned_get[(t_fn.table_id, (None, "init/main.c"))] = (1, "init/main.c")
            op_done = t_fn.get_set(None, "init/main.c")
            self.assertEqual(op_done[1], OP_DONE)
            self.assertEqual(op_done[2], (1, "init/main.c"))

        with test_step("Composite primary key get_set returns OP_SET when missing and OP_DONE when present"):
            t_bridge = self.test_tables.t_bridge_file
            op_bridge_set = t_bridge.get_set(1, 10, 100)
            self.assertEqual(op_bridge_set[1], OP_SET)
            self.assertEqual(op_bridge_set[2], (1, 10, 100))

            self.mock_te.canned_get[(t_bridge.table_id, (1, 10, 100))] = (1, 10, 100)
            op_bridge_done = t_bridge.get_set(1, 10, 100)
            self.assertEqual(op_bridge_done[1], OP_DONE)
            self.assertEqual(op_bridge_done[2], (1, 10, 100))

        with test_step("Unsafe reference bypasses G.TE.get returning OP_SET"):
            dummy_ref = ((t_fn.table_id, 0), OP_REF, (REF_ROOT,))
            op_unsafe = t_fn.get_set(dummy_ref, "init/main.c")
            self.assertEqual(op_unsafe[1], OP_SET)
            self.assertEqual(op_unsafe[2], (dummy_ref, "init/main.c"))

    @test_feature("table_views_operations", depends_on=["table_schema_introspection"])
    def test_06_table_views(self) -> None:
        """Verify view(), view_get(), view_get_multiple(), and ref_view() behavior."""
        t_file = self.test_tables.t_file
        t_bridge = self.test_tables.t_bridge_file
        joins: JoinsType = (((t_file.table_id, 0), (t_bridge.table_id, 2), 1),)

        with test_step("view() with cache miss constructs OP_VIEW_SET"):
            cols = (None, 1, 0, 1, "M", "M", 1, 10, None)
            op_view_set = t_file.view(joins, *cols)
            self.assertEqual(op_view_set[0], joins)
            self.assertEqual(op_view_set[1], OP_VIEW_SET)
            self.assertEqual(op_view_set[2], cols)

        with test_step("view() with cache hit in G.TE.view_get returns OP_VIEW_DONE"):
            expected_row = (50, 1, 0, 1, "M", "M", 1, 10, 50)
            self.mock_te.canned_view_get[(joins, cols)] = expected_row
            op_view_done = t_file.view(joins, *cols)
            self.assertEqual(op_view_done[1], OP_VIEW_DONE)
            self.assertEqual(op_view_done[2], expected_row)

        with test_step("view_get() queries G.TE.view_get"):
            self.assertEqual(t_file.view_get(joins, *cols), (joins, OP_VIEW_DONE, expected_row))
            self.assertIsNone(t_file.view_get(joins, *(None,) * 9))

        with test_step("view_get_multiple() returns list of rows"):
            self.mock_te.canned_view_get_multiple[(joins, cols)] = [expected_row]
            res_mult = t_file.view_get_multiple(joins, *cols)
            self.assertEqual(res_mult, [expected_row])

        with test_step("ref_view() constructs OP_REF_VIEW"):
            op_ref_view = t_file.ref_view(joins, *cols)
            self.assertEqual(op_ref_view[1], OP_REF_VIEW)

    @test_feature("table_cached_columns_validation")
    def test_07_table_cached_columns_validation(self) -> None:
        """Verify Table initialization validates te_cached configuration and column names."""
        with test_step("te_cached=False sets empty cached_columns"):
            tbl_nocache = Table(
                table_id=901,
                table_name="t_test_nocache",
                columns=(("id", "INT"), ("name", "VARCHAR(50)")),
                primary=("id",),
                te_cached=False,
            )
            self.assertEqual(tbl_nocache.cached_columns, ())
            self.assertFalse(tbl_nocache.te_cached)

        with test_step("te_cached with column names resolves indices"):
            tbl_named = Table(
                table_id=902,
                table_name="t_test_named",
                columns=(("id", "INT"), ("val", "INT"), ("extra", "VARCHAR(50)")),
                primary=("id",),
                te_cached=["id", "val"],
            )
            self.assertEqual(tbl_named.cached_columns, (0, 1))

        with test_step("te_cached with unknown column name raises ValueError"):
            with self.assertRaises(ValueError) as ctx:
                Table(
                    table_id=903,
                    table_name="t_test_invalid",
                    columns=(("id", "INT"), ("name", "VARCHAR(50)")),
                    primary=("id",),
                    te_cached=["non_existent_column"],
                )
            self.assertIn("Unknown column 'non_existent_column'", str(ctx.exception))


# =============================================================================
# 3. ChangeSet Context Routing & Staging Protocol
# =============================================================================

class Test_ChangeSetRoutingAndStaging(BaseTableHandlingTestCase):
    """Verification of ChangeSet path parsing, context route management, and store indexing."""

    @test_feature("changeset_initialization")
    def test_01_changeset_initialization(self) -> None:
        """Verify ChangeSet parses git diff lines (M, A, D, R100) and initializes route stacks."""
        with test_step("Parsing standard git modification line ('M\\tdrivers/net/e1000.c')"):
            cs_m = ChangeSet("M\tdrivers/net/e1000.c")
            self.assertEqual(cs_m.file_operation, "M")
            self.assertEqual(cs_m.current_path, "drivers/net/e1000.c")
            self.assertIsNone(cs_m.old_path)
            self.assertEqual(cs_m.route, [REF_ROOT])
            self.assertIn(REF_MULTI, cs_m.store_dict)

        with test_step("Parsing rename line ('R100\\told/name.c\\tnew/name.c')"):
            cs_r = ChangeSet("R100\told/name.c\tnew/name.c")
            self.assertEqual(cs_r.file_operation, "R100")
            self.assertEqual(cs_r.old_path, "old/name.c")
            self.assertEqual(cs_r.current_path, "new/name.c")

        with test_step("Explicit keyword arguments"):
            cs_explicit = ChangeSet(operation="A", current_path="include/linux/sched.h")
            self.assertEqual(cs_explicit.file_operation, "A")
            self.assertEqual(cs_explicit.current_path, "include/linux/sched.h")

    @test_feature("context_routing_lifecycle", depends_on=["changeset_initialization"])
    def test_02_context_routing_lifecycle(self) -> None:
        """Verify context manager pushes/pops routing links and safely unwinds on exceptions."""
        cs = ChangeSet("M\tmm/memory.c")

        with test_step("Single link push and pop via with cs(REF_OLD)"):
            with cs(REF_OLD):
                self.assertEqual(cs.route, [REF_ROOT, REF_OLD])
            self.assertEqual(cs.route, [REF_ROOT])

        with test_step("Nested link scopes (with cs('scope1'): with cs('scope2'):)"):
            with cs("scope1"):
                self.assertEqual(cs.route, [REF_ROOT, "scope1"])
                with cs("scope2"):
                    self.assertEqual(cs.route, [REF_ROOT, "scope1", "scope2"])
                self.assertEqual(cs.route, [REF_ROOT, "scope1"])
            self.assertEqual(cs.route, [REF_ROOT])

        with test_step("REF_MULTI link context tracks bucket index on multi_stack"):
            with cs(REF_MULTI):
                self.assertEqual(cs.route, [REF_ROOT, REF_MULTI, 0])
                self.assertEqual(cs.multi_stack, [0])
            self.assertEqual(cs.route, [REF_ROOT])
            self.assertEqual(cs.multi_stack, [])

        with test_step("Context manager unwinds route stack properly even when an exception is raised"):
            try:
                with cs("error_scope"):
                    self.assertEqual(cs.route, [REF_ROOT, "error_scope"])
                    raise RuntimeError("Intentional error inside route scope")
            except RuntimeError:
                pass
            self.assertEqual(cs.route, [REF_ROOT], "Route stack must recover to root context after exception")

    @test_feature("route_parse_canonicalization", depends_on=["context_routing_lifecycle"])
    def test_03_route_parse_canonicalization(self) -> None:
        """Verify route_parse applies link reduction rules (REF_ROOT, REF_POS, REF_MULTI, REF_FILE)."""
        cs = ChangeSet("M\tnet/socket.c")

        with test_step("REF_ROOT, REF_C_AST, REF_NO_REF clear preceding link prefixes"):
            self.assertEqual(cs.route_parse([REF_ROOT, "scope1", REF_ROOT]), [REF_ROOT])
            self.assertEqual(cs.route_parse(["scope1", REF_C_AST, "child"]), [REF_C_AST, "child"])
            self.assertEqual(cs.route_parse(["scope1", REF_NO_REF]), [REF_NO_REF])

        with test_step("REF_POS clears preceding links and preserves position index"):
            self.assertEqual(cs.route_parse([REF_ROOT, "scope", REF_POS, 7]), [REF_POS, 7])

        with test_step("REF_MULTI clears preceding links and preserves bucket index"):
            self.assertEqual(cs.route_parse([REF_ROOT, "scope", REF_MULTI, 3]), [REF_MULTI, 3])

        with test_step("REF_FILE prefixes route with [REF_FILE, target_file, ...]"):
            parsed = cs.route_parse([REF_ROOT, REF_FILE, "include/linux/fs.h", "sym_name", 1])
            self.assertEqual(parsed, [REF_FILE, "include/linux/fs.h", "sym_name", 1])

    @test_feature("store_routing_indexing", depends_on=["route_parse_canonicalization"])
    def test_04_store_routing(self) -> None:
        """Verify store() appends operations, canonicalizes route, and indexes in store_dict."""
        cs = ChangeSet("M\tfs/inode.c")
        t_file = self.test_tables.t_file
        t_fn = self.test_tables.t_file_name

        with test_step("Storing operation in default REF_ROOT route"):
            op1 = t_fn.set(None, "fs/inode.c")
            cs.store(op1)
            self.assertEqual(len(cs.cs), 1)
            self.assertEqual(cs.store_dict[(REF_ROOT,)][t_fn.table_id], 0)

        with test_step("Storing operation in custom nested route"):
            with cs("sub_scope"):
                op2 = t_file.set(None, 1, 0, 1, "M", "M")
                cs.store(op2)
                self.assertEqual(len(cs.cs), 2)
                self.assertEqual(cs.store_dict[(REF_ROOT, "sub_scope")][t_file.table_id], 1)

        with test_step("Storing operation inside REF_MULTI bucket"):
            with cs(REF_MULTI):
                op3 = t_file.set(None, 2, 0, 1, "M", "M")
                cs.store(op3)
                self.assertEqual(len(cs.cs), 3)
                self.assertEqual(cs.store_dict[REF_MULTI][0], [2])

    @test_feature("ref_generation", depends_on=["store_routing_indexing"])
    def test_05_ref_generation(self) -> None:
        """Verify ref() returns immediate value if resolvable or constructs (pointer, OP_REF, route)."""
        cs = ChangeSet("M\tkernel/sys.c")
        t_fn = self.test_tables.t_file_name

        with test_step("Unresolved reference constructs RefType tuple (query, OP_REF, route)"):
            ref_unresolved = cs.ref(t_fn.fnid, REF_ROOT)
            self.assertEqual(ref_unresolved, (t_fn.fnid, OP_REF, (REF_ROOT,)))

        with test_step("Resolved reference in cs_result resolves to primitive value immediately"):
            cs.store(t_fn.set(None, "kernel/sys.c"))
            cs.cs_result = [(101, "kernel/sys.c")]
            ref_resolved = cs.ref(t_fn.fnid, REF_ROOT)
            self.assertEqual(ref_resolved, 101)

    @test_feature("last_not_none_check")
    def test_06_last_not_none(self) -> None:
        """Verify last_not_none raises CONTINUE_EXCEPTION on empty cs or trailing None."""
        cs = ChangeSet("M\tkernel/sched.c")
        with test_step("Empty cs raises CONTINUE_EXCEPTION"):
            with self.assertRaises(CONTINUE_EXCEPTION):
                cs.last_not_none()

        with test_step("Trailing None operation raises CONTINUE_EXCEPTION"):
            cs.cs.append(None)
            with self.assertRaises(CONTINUE_EXCEPTION):
                cs.last_not_none()

        with test_step("Valid trailing operation passes cleanly"):
            t_fn = self.test_tables.t_file_name
            cs.cs[-1] = t_fn.set(None, "kernel/sched.c")
            cs.last_not_none()

    @test_feature("store_special_routing_rules")
    def test_07_store_with_ref_no_ref_and_ref_pos(self) -> None:
        """Verify store() behavior with REF_NO_REF, REF_POS, and None file_operation."""
        t_fn = self.test_tables.t_file_name
        op = t_fn.set(None, "fs/select.c")

        with test_step("Storing with REF_NO_REF appends to cs without indexing in store_dict"):
            cs1 = ChangeSet("M\tfs/select.c")
            cs1.store(op, REF_NO_REF)
            self.assertEqual(len(cs1.cs), 1)
            self.assertNotIn((REF_ROOT,), cs1.store_dict)

        with test_step("Storing inside with cs(REF_NO_REF): appends to cs without indexing in store_dict"):
            cs2 = ChangeSet("M\tfs/select.c")
            with cs2(REF_NO_REF):
                cs2.store(op)
            self.assertEqual(len(cs2.cs), 1)
            self.assertNotIn((REF_ROOT,), cs2.store_dict)

        with test_step("Storing with REF_POS captures current operation index onto route"):
            cs3 = ChangeSet("M\tfs/select.c")
            cs3.store(op, REF_POS)
            self.assertEqual(len(cs3.cs), 1)
            self.assertEqual(cs3.route, [REF_ROOT, REF_POS, 0])

        with test_step("ChangeSet with file_operation=None appends to cs without indexing in store_dict"):
            cs_noop = ChangeSet(operation=None)
            cs_noop.store(op)
            self.assertEqual(len(cs_noop.cs), 1)
            self.assertNotIn((REF_ROOT,), cs_noop.store_dict)


# =============================================================================
# 4. ChangeSet Reference Resolution
# =============================================================================

class Test_ChangeSetReferenceResolution(BaseTableHandlingTestCase):
    """Verification of resolve_ref across REF_NO_REF, REF_POS, REF_MULTI, context routes, and foreign files."""

    @test_feature("resolve_ref_no_ref")
    def test_01_resolve_ref_no_ref(self) -> None:
        """Verify parsed_route starting with REF_NO_REF resolves to None."""
        cs = ChangeSet("M\tkernel/workqueue.c")
        t_fn = self.test_tables.t_file_name
        self.assertIsNone(cs.resolve_ref(t_fn.fnid, (REF_NO_REF,)))

    @test_feature("resolve_ref_direct_pos")
    def test_02_resolve_ref_direct_pos(self) -> None:
        """Verify REF_POS resolves directly from cs.cs[pos] or cs.cs_result[pos]."""
        cs = ChangeSet("M\tmm/vmscan.c")
        t_file = self.test_tables.t_file

        with test_step("Resolving from cs.cs when not yet executed"):
            cs.cs.append((t_file.table_id, OP_SET, (12, 1, 0, 1, "M", "M")))
            val = cs.resolve_ref(t_file.vid_s, (REF_POS, 0))
            self.assertEqual(val, 1)

        with test_step("Resolving from cs.cs_result when executed"):
            cs.cs_result = [(12, 1, 0, 1, "M", "M")]
            val_executed = cs.resolve_ref(t_file.fid, (REF_POS, 0))
            self.assertEqual(val_executed, 12)

    @test_feature("resolve_ref_multi")
    def test_03_resolve_ref_multi(self) -> None:
        """Verify REF_MULTI resolves list of column values across stored bucket entries."""
        cs = ChangeSet("M\tdrivers/char/random.c")
        t_file = self.test_tables.t_file

        with cs(REF_MULTI):
            cs.store(t_file.set(None, 1, 0, 1, "M", "M"))
            cs.store(t_file.set(None, 2, 0, 1, "M", "M"))
        cs.cs_result = [(10, 1, 0, 1, "M", "M"), (20, 2, 0, 1, "M", "M")]

        with test_step("Resolving column values across REF_MULTI bucket"):
            multi_fids = cs.resolve_ref(t_file.fid, (REF_MULTI, 0))
            self.assertEqual(multi_fids, [10, 20])

    @test_feature("resolve_ref_context_route")
    def test_04_resolve_ref_context_route(self) -> None:
        """Verify resolving pointer value from store_dict[route][table_id]."""
        cs = ChangeSet("M\tlib/string.c")
        t_fn = self.test_tables.t_file_name

        cs.store(t_fn.set(None, "lib/string.c"))
        cs.cs_result = [(55, "lib/string.c")]

        with test_step("Resolving column fnid from REF_ROOT route"):
            fnid = cs.resolve_ref(t_fn.fnid, (REF_ROOT,))
            self.assertEqual(fnid, 55)

        with test_step("Resolving column fname from REF_ROOT route"):
            fname = cs.resolve_ref(t_fn.fname, (REF_ROOT,))
            self.assertEqual(fname, "lib/string.c")

    @test_feature("resolve_ref_foreign_fast_paths")
    def test_05_resolve_ref_foreign_fast_paths(self) -> None:
        """Verify REF_FILE foreign lookups check gp.file_symbols and gp.file_names fast-paths."""
        cs = ChangeSet("M\tarch/x86/kernel/cpu/common.c")
        t_ast = self.test_tables.t_ast

        class MockGP:
            def __init__(self) -> None:
                self.file_symbols = {
                    "include/linux/types.h": {("size_t", 1): 888},
                }
                self.file_names = {
                    "include/linux/types.h": {"size_t": 888},
                }

        cs.gp = MockGP()

        with test_step("Resolving symbol via gp.file_symbols fast-path"):
            route = (REF_FILE, "include/linux/types.h", "size_t", 1)
            ast_id = cs.resolve_ref(t_ast.ast_id, route)
            self.assertEqual(ast_id, 888)

    @test_feature("resolve_ref_foreign_in_flight_blocking", depends_on=["resolve_ref_foreign_fast_paths"])
    def test_06_resolve_ref_foreign_in_flight_blocking(self) -> None:
        """Verify REF_FILE on in-flight incomplete foreign file sets blocked_on and returns None."""
        cs = ChangeSet("M\tdrivers/pci/probe.c")
        t_ast = self.test_tables.t_ast

        class MockChangeSetDict(dict):
            _lru_cache = {}

        class MockInFlightGP:
            def __init__(self) -> None:
                self.file_symbols = {}
                self.file_names = {}
                self._changed_paths_set = {"include/linux/pci.h"}
                self.ChangeSet_Dict = MockChangeSetDict()

        cs.gp = MockInFlightGP()

        with test_step("Target foreign file is in-flight: resolve_ref sets blocked_on"):
            route = (REF_FILE, "include/linux/pci.h", "pci_dev", 1)
            val = cs.resolve_ref(t_ast.ast_id, route, force_stubs=False)
            self.assertIsNone(val)
            self.assertEqual(cs.blocked_on, "include/linux/pci.h")

    @test_feature("resolve_ref_foreign_force_stubs", depends_on=["resolve_ref_foreign_in_flight_blocking"])
    def test_07_resolve_ref_foreign_force_stubs(self) -> None:
        """Verify force_stubs=True stages notbind stub symbol into G.TE to break circular dependencies."""
        cs = ChangeSet("M\tfs/btrfs/inode.c")
        t_ast = self.test_tables.t_ast

        class MockEmptyGP:
            def __init__(self) -> None:
                self.file_symbols = {}
                self.file_names = {}
                self.staged_symbols = {}
                self.staged_names = {}
                self.ChangeSet_Dict = None

        cs.gp = MockEmptyGP()
        self.mock_te.next_id[t_ast.table_id] = 500

        with test_step("Calling resolve_ref with force_stubs=True stages stub symbol"):
            route = (REF_FILE, "fs/btrfs/transaction.h", "btrfs_trans_handle", 1)
            stub_id = cs.resolve_ref(t_ast.ast_id, route, force_stubs=True)
            self.assertEqual(stub_id, 500)
            self.assertEqual(len(self.mock_te.set_calls), 1)
            tid, data = self.mock_te.set_calls[0]
            self.assertEqual(tid, t_ast.table_id)
            self.assertEqual(data[1], "btrfs_trans_handle")

    @test_feature("resolve_ref_from_tuple", depends_on=["resolve_ref_context_route"])
    def test_08_resolve_ref_from_tuple(self) -> None:
        """Verify _resolve_ref_from_tuple resolves embedded RefType references inside tuples."""
        cs = ChangeSet("M\tnet/ipv4/tcp.c")
        t_fn = self.test_tables.t_file_name
        t_file = self.test_tables.t_file

        cs.store(t_fn.set(None, "net/ipv4/tcp.c"))
        cs.cs_result = [(77, "net/ipv4/tcp.c")]

        with test_step("Resolving data tuple with mixed primitives and reference tuples"):
            ref_fnid = ((t_fn.table_id, 0), OP_REF, (REF_ROOT,))
            data_tuple = (1, ref_fnid, 500)
            resolved = cs._resolve_ref_from_tuple(data_tuple)
            self.assertEqual(resolved, (1, 77, 500))

    @test_feature("get_available_data_filtering")
    def test_09_get_available_data(self) -> None:
        """Verify get_available_data filters candidate operations by route and table ID."""
        cs = ChangeSet("M\tkernel/kthread.c")
        t_fn = self.test_tables.t_file_name
        t_file = self.test_tables.t_file

        cs.store(t_fn.set(None, "kernel/kthread.c"))
        cs.store(t_file.set(None, 1, 0, 1, "M", "M"))
        cs.cs_result = [(10, "kernel/kthread.c"), (20, 1, 0, 1, "M", "M")]

        with test_step("Querying available data for t_fn table ID returns t_fn entry"):
            fn_avail = cs.get_available_data((REF_ROOT,), tableid=t_fn.table_id)
            self.assertEqual(len(fn_avail), 1)
            self.assertEqual(fn_avail[0][1], (10, "kernel/kthread.c"))

        with test_step("Querying available data for mismatched table ID returns empty list"):
            none_avail = cs.get_available_data((REF_ROOT,), tableid=999)
            self.assertEqual(len(none_avail), 0)


# =============================================================================
# 5. ChangeSet Execution & Lifecycle
# =============================================================================

class Test_ChangeSetExecutionAndLifecycle(BaseTableHandlingTestCase):
    """Verification of execute() multi-pass resolution, view preprocessing, bloat clearing, and file types."""

    @test_feature("execute_multi_pass_loop")
    def test_01_execute_resolution_loop(self) -> None:
        """Verify execute() resolves forward and backward interdependent operations across multiple passes."""
        cs = ChangeSet("M\tkernel/time/timer.c")
        t_fn = self.test_tables.t_file_name
        t_file = self.test_tables.t_file
        t_bridge = self.test_tables.t_bridge_file

        with test_step("Staging operations where op3 references op1 and op2"):
            # op0: t_fn.set -> assigns fnid=1
            cs.store(t_fn.set(None, "kernel/time/timer.c"))
            # op1: t_file.set -> assigns fid=1
            cs.store(t_file.set(None, 1, 0, 1, "M", "M"))
            # op2: t_bridge.set -> references op0 fnid and op1 fid
            cs.store(t_bridge.set(1, cs.ref(t_fn.fnid, REF_ROOT), cs.ref(t_file.fid, REF_ROOT)))

        with test_step("Executing ChangeSet resolves all operations"):
            success = cs.execute()
            self.assertTrue(success, "execute() should return True when all operations resolve")
            self.assertTrue(cs.cs_processed)
            self.assertEqual(len(cs.cs_result), 3)
            # Verify bridge was set with resolved values: (1, 1, 1)
            self.assertEqual(cs.cs_result[2], (1, 1, 1))

    @test_feature("execute_operation_dispatch", depends_on=["execute_multi_pass_loop"])
    def test_02_execute_operation_dispatch(self) -> None:
        """Verify execute() dispatches OP_SET, OP_UPDATE, and OP_VIEW_SET to respective TableEngine APIs."""
        cs = ChangeSet("M\tkernel/fork.c")
        t_fn = self.test_tables.t_file_name
        t_file = self.test_tables.t_file

        with test_step("Staging OP_SET, OP_UPDATE, and OP_DONE"):
            cs.store(t_fn.set(None, "kernel/fork.c"))  # OP_SET
            cs.store(t_file.update(10, 1, 0, 1, "M", "M"))  # OP_UPDATE
            cs.store((t_fn.table_id, OP_DONE, (5, "pre_done.c")))  # OP_DONE

        with test_step("Executing dispatches to mock TE set and update"):
            self.assertTrue(cs.execute())
            self.assertEqual(len(self.mock_te.set_calls), 1)
            self.assertEqual(len(self.mock_te.update_calls), 1)
            self.assertEqual(cs.cs_result[2], (5, "pre_done.c"))

    @test_feature("execute_unresolved_failure", depends_on=["execute_multi_pass_loop"])
    def test_03_execute_unresolved_failure(self) -> None:
        """Verify execute() returns False and leaves operations unresolved when dependencies cannot be met."""
        cs = ChangeSet("M\tmm/oom_kill.c")
        t_bridge = self.test_tables.t_bridge_file

        # Unresolvable foreign ref without force_stubs
        unresolvable_ref = ((1, 0), OP_REF, (REF_FILE, "non_existent.h", "oom_sym", 1))
        cs.store((t_bridge.table_id, OP_SET, (1, unresolvable_ref, 10)))

        with test_step("Executing with unresolvable reference returns False"):
            success = cs.execute(force_stubs=False)
            self.assertFalse(success)
            self.assertFalse(cs.cs_processed)
            self.assertIn(0, cs.unresolved_indices)

    @test_feature("get_file_type_classification")
    def test_04_get_file_type(self) -> None:
        """Verify get_file_type accurately classifies source, config, documentation, and raw file paths."""
        mappings = [
            ("kernel/sched/core.c", T_C),
            ("include/linux/mm.h", T_C),
            ("rust/kernel/lib.rs", T_RUST),
            ("arch/x86/entry/entry_64.S", T_ASM),
            ("arch/arm/boot/compressed/head.s", T_ASM),
            ("init/Kconfig", T_KCONFIG),
            ("drivers/net/Kconfig", T_KCONFIG),
            ("MAINTAINERS", T_MAINTAINERS),
            ("CREDITS", T_CREDITS),
            ("README", T_RAW),
            ("Makefile", T_RAW),
            ("Documentation/admin-guide/kernel-parameters.txt", T_RAW),
        ]
        for path, expected_type in mappings:
            with test_step(f"Classifying file path '{path}' as type {expected_type}"):
                cs = ChangeSet(f"M\t{path}")
                self.assertEqual(cs.get_file_type(), expected_type)

    @test_feature("clear_bloat_and_pickle")
    def test_05_clear_bloat_and_pickle(self) -> None:
        """Verify clear_bloat() purges unpicklable attributes and ChangeSet successfully serializes."""
        cs = ChangeSet("M\tkernel/futex.c")
        t_fn = self.test_tables.t_file_name
        cs.store(t_fn.set(None, "kernel/futex.c"))

        # Attach unpicklable handles
        class UnpicklableObject:
            pass

        cs.gp = UnpicklableObject()
        cs.mf = UnpicklableObject()
        cs.parsers = {"c": UnpicklableObject()}

        with test_step("Calling clear_bloat() nulls unpicklable objects"):
            cs.clear_bloat()
            self.assertIsNone(cs.gp)
            self.assertIsNone(cs.mf)
            self.assertEqual(cs.parsers, {})

        with test_step("Serializing and deserializing sanitized ChangeSet via pickle"):
            serialized = pickle.dumps(cs)
            deserialized: ChangeSet = pickle.loads(serialized)
            self.assertEqual(deserialized.current_path, "kernel/futex.c")
            self.assertEqual(len(deserialized.cs), 1)

    @test_feature("register_bridge_map")
    def test_06_register_bridge_map(self) -> None:
        """Verify register_bridge_map deduplicates tag-to-map registrations."""
        cs = ChangeSet("M\tsecurity/selinux/hooks.c")

        with test_step("First registration returns True"):
            is_new = cs.register_bridge_map(tag_ref=100, map_ref=200)
            self.assertTrue(is_new)

        with test_step("Duplicate registration returns False"):
            is_dup = cs.register_bridge_map(tag_ref=100, map_ref=200)
            self.assertFalse(is_dup)

        with test_step("Distinct registration returns True"):
            is_distinct = cs.register_bridge_map(tag_ref=100, map_ref=201)
            self.assertTrue(is_distinct)

    @test_feature("preprocess_ref_views")
    def test_07_preprocess_ref_views(self) -> None:
        """Verify preprocess_ref_views unpacks intra-file OP_REF_VIEW operations into OP_VIEW_SET."""
        cs = ChangeSet("M\tkernel/pid.c")
        t_file = self.test_tables.t_file
        t_bridge = self.test_tables.t_bridge_file
        joins: JoinsType = (((t_file.table_id, 0), (t_bridge.table_id, 2), 1),)

        cs.store(t_file.set(None, 1, 0, 1, "M", "M"))
        schema_ifs = [((t_file.fid, 1), 1)]
        schema_thens = [([], [t_file.fid])]
        schema = (schema_ifs, schema_thens, (REF_ROOT,), 0, None)

        cs.cs.append((joins, OP_REF_VIEW, (None, 1, 0, 1, "M", "M", 1, 10, None, schema)))
        cs.preprocess_ref_views()

        with test_step("Asserting OP_REF_VIEW was converted to OP_VIEW_SET"):
            self.assertEqual(cs.cs[-1][1], OP_VIEW_SET)

    @test_feature("prune_unchanged_dependencies")
    def test_08_prune_unchanged_dependencies(self) -> None:
        """Verify prune_unchanged_dependencies resolves references and prunes satisfied foreign_deps."""
        cs = ChangeSet("M\tdrivers/char/mem.c")
        t_ast = self.test_tables.t_ast
        t_bridge = self.test_tables.t_bridge_file

        class MockUnchangedGP:
            def __init__(self) -> None:
                self._changed_paths_set = set()

            def get_file_symbols_from_db(self, rel_path: str) -> dict:
                if rel_path == "include/linux/types.h":
                    return {("size_t", 1): 999}
                return {}

        cs.gp = MockUnchangedGP()
        cs.foreign_deps.add("include/linux/types.h")

        ref_foreign = (t_ast.ast_id, OP_REF, (REF_FILE, "include/linux/types.h", "size_t", 1))
        cs.cs.append((t_bridge.table_id, OP_SET, (1, 10, ref_foreign)))

        with test_step("Calling prune_unchanged_dependencies resolves reference and prunes foreign_deps"):
            cs.prune_unchanged_dependencies()
            self.assertNotIn("include/linux/types.h", cs.foreign_deps)
            self.assertEqual(cs.cs[0][2][2], 999)

    @test_feature("pre_resolve_operations")
    def test_09_pre_resolve_operations(self) -> None:
        """Verify pre_resolve_operations pre-resolves foreign references in worker cores."""
        cs = ChangeSet("M\tdrivers/block/loop.c")
        t_ast = self.test_tables.t_ast
        t_bridge = self.test_tables.t_bridge_file

        ref_foreign = (t_ast.ast_id, OP_REF, (REF_FILE, "include/linux/genhd.h", "gendisk", 1))
        cs.cs.append((t_bridge.table_id, OP_SET, (1, 20, ref_foreign)))

        with test_step("Calling pre_resolve_operations with known symbols map"):
            resolved_ops = cs.pre_resolve_operations(
                resolved_symbols_map={("gendisk", 1): 444},
                resolved_names_map={"gendisk": 444},
            )
            self.assertEqual(len(resolved_ops), 1)
            self.assertEqual(resolved_ops[0][2][2], 444)

    @test_feature("execute_force_stubs_breaks_deadlock")
    def test_10_execute_force_stubs_breaks_deadlock(self) -> None:
        """Verify execute(force_stubs=True) breaks circular deadlock by staging notbind stub symbol."""
        cs = ChangeSet("M\tfs/xfs/xfs_super.c")
        t_ast = self.test_tables.t_ast
        t_bridge = self.test_tables.t_bridge_file

        class MockDeadlockGP:
            def __init__(self) -> None:
                self.file_symbols = {}
                self.file_names = {}
                self.staged_symbols = {}
                self.staged_names = {}
                self._changed_paths_set = {"fs/xfs/xfs_mount.h"}
                self.ChangeSet_Dict = {}

        cs.gp = MockDeadlockGP()
        self.mock_te.next_id[t_ast.table_id] = 777

        ref_foreign = (t_ast.ast_id, OP_REF, (REF_FILE, "fs/xfs/xfs_mount.h", "xfs_mount", 1))
        cs.cs.append((t_bridge.table_id, OP_SET, (1, 10, ref_foreign)))

        with test_step("execute(force_stubs=False) fails due to unresolved in-flight dependency"):
            success_fail = cs.execute(force_stubs=False)
            self.assertFalse(success_fail)

        with test_step("execute(force_stubs=True) breaks deadlock by generating stub and returns True"):
            success_stub = cs.execute(force_stubs=True)
            self.assertTrue(success_stub)
            self.assertEqual(cs.cs_result[0][2], 777)

    @test_feature("changeset_string_representation")
    def test_11_string_representation(self) -> None:
        """Verify ChangeSet.__str__ formats current_path, file_operation, and queued operations."""
        cs = ChangeSet("M\tinit/version.c")
        t_fn = self.test_tables.t_file_name
        cs.store(t_fn.set(None, "init/version.c"))
        cs_str = str(cs)

        with test_step("Checking string representation contains file, op, and cs"):
            self.assertIn("CS:file(init/version.c)", cs_str)
            self.assertIn("op(M)", cs_str)
            self.assertIn("cs(", cs_str)


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
