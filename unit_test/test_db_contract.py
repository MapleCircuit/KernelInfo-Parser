"""unit_test/test_db_contract.py - One behavioural contract run against every `BaseDBEngine` backend.

Backends: `MockDB` (isolated store), `SQLiteDB(":memory:")` and MariaDB (only when a server answers the configured
connection; skipped otherwise). A test fails when a backend disagrees with the others, so a divergence is fixed in
the backend that differs from MariaDB semantics.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any, Callable

from core import DBLayout as D
from db_engine import (
    Agg,
    Case,
    Col,
    Concat,
    DuplicateKeyError,
    Incoming,
    Join,
    Length,
    MissingTableError,
    MockDB,
    ReadOnlyViolation,
    Select,
    SpecError,
    SQLiteDB,
    ge,
    gt,
    in_select,
    is_null,
    le,
    like,
    lt,
    ne,
)
from db_engine.base import BaseDBEngine
from db_engine.spec import Pred, Value, all_of, any_of, eq, add, between, exists_select, not_in, not_null

SCHEMA = (
    D.m_project, D.m_git_network, D.m_file_name, D.m_file_blob, D.m_bridge_checkpoint, D.m_parse_task, D.m_table_sequence,
    D.m_ingest_state, D.m_checkpoint_base, D.m_git_ref, D.m_ast, D.m_tag, D.m_bridge_tag, D.m_symbol_own, D.m_blob_symbol,
    D.m_blob_symbol_ref, D.m_blob_symbol_def, D.m_tag_ref, D.m_tag_def,
)
SCHEMA_NAMES = [t.table_name for t in SCHEMA]


def _mariadb_factory() -> Callable[[], BaseDBEngine] | None:
    try:
        from db_engine import MariaDB
        from core.config import get_db_config

        cfg = get_db_config()
        if MariaDB.probe_connection(dict(cfg, timeout=1)).ok:
            return MariaDB
    except Exception:
        return None
    return None


def _backends() -> dict[str, Callable[[], BaseDBEngine] | None]:
    return {
        "mock": lambda: MockDB(use_global=False),
        "sqlite": lambda: SQLiteDB(":memory:"),
        # MariaDB runs against the live configured database: never executed implicitly, opt in with KIP_CONTRACT_MARIADB=1
        "mariadb": _mariadb_factory() if os.environ.get("KIP_CONTRACT_MARIADB") == "1" else None,
    }


class _BackendCase:
    """Creates the contract schema on the backend chosen by `make_db` and drops it afterwards."""

    make_db: Callable[[], BaseDBEngine]
    db: BaseDBEngine

    def setUp(self) -> None:
        self.db = self.make_db()
        self.db.drop_tables(SCHEMA_NAMES, ignore_fk=True)
        self.db.create_table(list(SCHEMA))

    def tearDown(self) -> None:
        try:
            self.db.drop_tables(SCHEMA_NAMES, ignore_fk=True)
        finally:
            self.db.close()


class ContractMixin(_BackendCase):
    """The contract; subclasses pick the backend by `make_db`."""

    # ---- schema / catalog ------------------------------------------------------------------------------------
    def test_create_schema_and_catalog(self) -> None:
        self.assertIsNone(self.db.test_tables(list(SCHEMA)))
        self.assertEqual(self.db.existing_tables(SCHEMA_NAMES + ["m_nope"]), set(SCHEMA_NAMES))
        self.assertTrue(self.db.table_exists("m_file_name"))
        self.assertFalse(self.db.table_exists("m_nope"))
        self.assertTrue(self.db.column_exists("m_file_name", "fname"))
        self.assertFalse(self.db.column_exists("m_file_name", "nope"))
        info = self.db.column_info("m_file_blob", "blob_hash")
        self.assertIsNotNone(info)
        self.assertEqual(info.octet_length, 20)
        self.assertFalse(info.nullable)
        self.assertIn("file_name_index", self.db.index_names("m_file_name"))
        self.assertIn("m_file_name", {s.name for s in self.db.table_stats(["m_file_name"])})
        self.assertEqual(self.db.resolve_relation_name("m_other"), "m_other")

    def test_initial_rows_seeded(self) -> None:
        self.assertEqual(self.db.select_rows("m_file_name", ["fnid", "fname"], as_dict=False), [(0, "")])

    def test_create_index_roundtrip(self) -> None:
        self.db.create_index("contract_idx", D.m_file_name, ((D.m_file_name.table_id, 1),))
        self.assertTrue(self.db.index_exists("contract_idx", D.m_file_name))
        self.db.remove_index("contract_idx", D.m_file_name)
        self.assertFalse(self.db.index_exists("contract_idx", D.m_file_name))

    def test_add_column_and_drop(self) -> None:
        from db_engine import ColumnSpec, ColumnType

        self.assertTrue(self.db.add_column("m_file_name", "note", ColumnSpec(ColumnType.INT, default=0, nullable=False)))
        self.assertFalse(self.db.add_column("m_file_name", "note", ColumnSpec(ColumnType.INT)))
        self.assertTrue(self.db.column_exists("m_file_name", "note"))
        self.db.drop_tables(["m_file_name"], ignore_fk=True)
        self.assertFalse(self.db.table_exists("m_file_name"))

    def test_views(self) -> None:
        self.db.insert_rows("m_file_name", ["fname"], [("a",), ("b",)])
        self.db.create_view("v_contract", Select("m_file_name", ["fnid", "fname"], {"fname": ge("a")}))
        self.assertTrue(self.db.table_exists("v_contract"))
        rows = self.db.select_rows("v_contract", ["fname"], order_by=["fname"], as_dict=False)
        self.assertEqual(rows, [("a",), ("b",)])
        self.db.drop_view("v_contract")
        self.assertFalse(self.db.table_exists("v_contract"))

    # ---- inserts ---------------------------------------------------------------------------------------------
    def test_insert_error_duplicate(self) -> None:
        self.db.insert_rows("m_project", ["name", "slug"], [("p1", "s1")])
        with self.assertRaises(DuplicateKeyError):
            self.db.insert_rows("m_project", ["name", "slug"], [("p2", "s1")])

    def test_insert_ignore(self) -> None:
        self.db.insert_rows("m_project", ["name", "slug"], [("p1", "s1")])
        self.db.insert_rows("m_project", ["name", "slug"], [("p2", "s1"), ("p3", "s3")], on_conflict="ignore")
        self.assertEqual(
            self.db.select_rows("m_project", ["name", "slug"], {"slug": ["s1", "s3"]}, order_by=["slug"], as_dict=False),
            [("p1", "s1"), ("p3", "s3")],
        )

    def test_insert_update_columns(self) -> None:
        self.db.insert_rows("m_project", ["name", "slug"], [("p1", "s1")])
        self.db.insert_rows("m_project", ["name", "slug"], [("renamed", "s1")], on_conflict="update", update_columns=["name"])
        self.assertEqual(self.db.select_value("m_project", "name", {"slug": "s1"}), "renamed")

    def test_insert_update_exprs(self) -> None:
        self.db.insert_rows("m_project", ["name", "slug"], [("p1", "s1")])
        self.db.insert_rows(
            "m_project",
            ["name", "slug"],
            [("longer-name", "s1")],
            on_conflict="update",
            update_exprs={"name": Case([(Pred(Length("name"), ge(11)), "name")], Incoming("name"))},
        )
        self.assertEqual(self.db.select_value("m_project", "name", {"slug": "s1"}), "longer-name")
        self.db.insert_rows(
            "m_project",
            ["name", "slug"],
            [("short", "s1")],
            on_conflict="update",
            update_exprs={"name": Case([(Pred(Length("name"), ge(11)), "name")], Incoming("name"))},
        )
        self.assertEqual(self.db.select_value("m_project", "name", {"slug": "s1"}), "longer-name")

    def test_insert_replace(self) -> None:
        self.db.insert_rows("m_project", ["name", "slug"], [("p1", "s1")])
        self.db.insert_rows("m_project", ["name", "slug"], [("p2", "s1")], on_conflict="replace")
        self.assertEqual(self.db.select_values("m_project", "name", {"slug": "s1"}), ["p2"])

    def test_get_or_insert_id(self) -> None:
        first = self.db.get_or_insert_id(D.m_file_name, {"fname": "x"})
        self.assertEqual(self.db.get_or_insert_id(D.m_file_name, {"fname": "x"}), first)
        self.assertNotEqual(self.db.get_or_insert_id(D.m_file_name, {"fname": "y"}), first)

    # ---- reads -----------------------------------------------------------------------------------------------
    def _names(self, *names: str) -> None:
        self.db.insert_rows("m_file_name", ["fname"], [(n,) for n in names])

    def test_select_order_limit_offset_distinct(self) -> None:
        self._names("b", "a", "c", "a2")
        got = self.db.select_values("m_file_name", "fname", {"fname": ge("a")}, order_by=["-fname"], limit=2)
        self.assertEqual(got, ["c", "b"])
        page = self.db.select_rows("m_file_name", ["fname"], {"fname": ge("a")}, order_by=["fname"], limit=2, offset=1, as_dict=False)
        self.assertEqual(page, [("a2",), ("b",)])
        self.assertEqual(self.db.count_rows("m_file_name", {"fname": ge("a")}), 4)
        self.assertTrue(self.db.row_exists("m_file_name", {"fname": "a"}))
        self.assertFalse(self.db.row_exists("m_file_name", {"fname": "zz"}))
        self.assertEqual(self.db.max_value("m_file_name", "fname"), "c")

    def test_select_dict_and_like_and_in(self) -> None:
        self._names("alpha", "alps", "beta")
        rows = self.db.select_rows("m_file_name", ["fname"], {"fname": like("alp", "prefix")}, order_by=["fname"], as_dict=True)
        self.assertEqual(rows, [{"fname": "alpha"}, {"fname": "alps"}])
        self.assertEqual(self.db.select_values("m_file_name", "fname", {"fname": ["beta", "alps"]}, order_by=["fname"]), ["alps", "beta"])
        self.assertEqual(self.db.select_rows("m_file_name", ["fname"], {"fname": []}), [])

    def test_select_large_in_list_is_chunked(self) -> None:
        self._names("a", "b")
        values = [f"x{i}" for i in range(2500)] + ["a", "b"]
        self.assertEqual(self.db.select_values("m_file_name", "fname", {"fname": values}, order_by=["fname"]), ["a", "b"])

    def test_expressions_and_aggregates(self) -> None:
        self._names("ab", "abc", "abd")
        rows = self.db.select_rows(
            "m_file_name", {"n": Concat("fname", Value("!")), "len": Length("fname")}, {"fname": like("ab", "prefix")},
            order_by=["fname"], as_dict=True,
        )
        self.assertEqual(rows[0], {"n": "ab!", "len": 2})
        grouped = self.db.aggregate(
            "m_file_name", {"c": Agg("COUNT")}, {"fname": like("ab", "prefix")}, group_by=[Length("fname")],
            order_by=["-c"], as_dict=False,
        )
        self.assertEqual([r[-1] for r in grouped], [2, 1])

    def test_joins_and_subqueries(self) -> None:
        self._names("f1", "f2")
        fn = {r[1]: r[0] for r in self.db.select_rows("m_file_name", ["fnid", "fname"], as_dict=False)}
        self.db.insert_rows("m_file_blob", ["blob_hash", "ftype"], [(b"\x01" * 20, 1)])
        blob = self.db.select_value("m_file_blob", "blob_id", {"ftype": 1})
        self.db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(1, fn["f1"], blob), (1, fn["f2"], blob)])
        joined = self.db.select_rows(
            ("m_bridge_checkpoint", "c"), ["f.fname", "c.commit_id"], {"c.commit_id": 1},
            joins=[Join(("m_file_name", "f"), {"f.fnid": "c.fnid"})], order_by=["f.fname"], as_dict=False,
        )
        self.assertEqual(joined, [("f1", 1), ("f2", 1)])
        inside = self.db.select_values(
            "m_file_name", "fname",
            [in_select("fnid", Select("m_bridge_checkpoint", ["fnid"], {"commit_id": 1})), {"fname": "f2"}],
        )
        self.assertEqual(inside, ["f2"])
        left = self.db.select_values(
            ("m_file_name", "f"), "f.fname", {"c.fnid": is_null()},
            joins=[Join(("m_bridge_checkpoint", "c"), {"c.fnid": "f.fnid"}, "left")], order_by=["f.fname"],
        )
        self.assertEqual(left, [""])

    def test_binary_values_roundtrip_as_bytes(self) -> None:
        digest = bytes(range(20))
        self.db.insert_rows("m_file_blob", ["blob_hash", "ftype"], [(digest, 7)])
        self.assertEqual(self.db.select_value("m_file_blob", "blob_hash", {"ftype": 7}), digest)

    # ---- updates / deletes -----------------------------------------------------------------------------------
    def test_update_and_delete_with_limit(self) -> None:
        self._names("a", "b", "c")
        self.assertEqual(self.db.update_rows("m_file_name", {"fname": "z"}, {"fname": "a"}), 1)
        self.assertEqual(self.db.select_values("m_file_name", "fname", {"fname": "z"}), ["z"])
        self.assertEqual(self.db.delete_rows("m_file_name", {"fname": ge("b")}, limit=2), 2)
        self.assertEqual(self.db.count_rows("m_file_name", {"fname": ge("b")}), 1)
        with self.assertRaises(ValueError):
            self.db.delete_rows("m_file_name", None)
        self.assertGreaterEqual(self.db.delete_rows("m_file_name", None, all_rows=True), 1)
        self.assertEqual(self.db.count_rows("m_file_name"), 0)

    def test_copy_rows_ignore(self) -> None:
        self.db.insert_rows("m_project", ["name", "slug"], [("p1", "s1"), ("p2", "s2")])
        self.db.insert_rows("m_file_name", ["fname"], [("s1",), ("s9",)])
        self.db.copy_rows("m_project", ["name", "slug"], Select("m_file_name", ["fname", "fname"], {"fname": ["s1", "s9"]}), on_conflict="ignore")
        self.assertEqual(self.db.select_values("m_project", "slug", {"slug": ["s1", "s9"]}, order_by=["slug"]), ["s1", "s9"])
        with self.assertRaises(DuplicateKeyError):
            self.db.copy_rows("m_project", ["name", "slug"], Select("m_file_name", ["fname", "fname"], {"fname": "s1"}))

    # ---- transactions ----------------------------------------------------------------------------------------
    def test_transaction_commit_and_rollback(self) -> None:
        with self.db.transaction():
            self.db.insert_rows("m_file_name", ["fname"], [("kept",)])
        self.assertEqual(self.db.count_rows("m_file_name", {"fname": "kept"}), 1)
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.db.insert_rows("m_file_name", ["fname"], [("gone",)])
                raise RuntimeError("boom")
        self.assertEqual(self.db.count_rows("m_file_name", {"fname": "gone"}), 0)

    # ---- errors ----------------------------------------------------------------------------------------------
    def test_missing_table_error(self) -> None:
        with self.assertRaises(MissingTableError):
            self.db.select_rows("m_does_not_exist", ["x"])

    # ---- legacy Table API ------------------------------------------------------------------------------------
    def test_table_api_insert_select_update(self) -> None:
        self.db.insert(D.m_file_name, ((5, "five"), (6, "six")))
        self.assertEqual(self.db.select(D.m_file_name, (5, None)), (5, "five"))
        self.assertIsNone(self.db.select(D.m_file_name, (None, "nope")))
        self.db.update(D.m_file_name, ((5, "FIVE"), (7, "seven")))
        self.assertEqual(self.db.select(D.m_file_name, (5, None)), (5, "FIVE"))
        self.assertEqual(self.db.select(D.m_file_name, (7, None)), (7, "seven"))
        self.assertEqual(sorted(self.db.select_in(D.m_file_name, 0, [5, 6, 99])), [(5, "FIVE"), (6, "six")])
        self.assertEqual(self.db.get_next_id(D.m_file_name), 8)
        self.assertEqual(sorted(self.db.select_preload(D.m_file_name, (1,))), [("",), ("FIVE",), ("seven",), ("six",)])

    def test_table_api_view_select_multiple(self) -> None:
        self.db.insert(D.m_file_name, ((1, "f"),))
        self.db.insert(D.m_file_blob, ((1, b"\x02" * 20, 1),))
        self.db.insert(D.m_bridge_checkpoint, ((9, 1, 1),))
        joins = (((D.m_bridge_checkpoint.table_id, 1), (D.m_file_name.table_id, 0), 1),)
        tables = {D.m_bridge_checkpoint.table_id: D.m_bridge_checkpoint, D.m_file_name.table_id: D.m_file_name}
        cols = (9, None, None, None, None)
        self.assertEqual(self.db.view_select_multiple(tables, joins, cols), [(9, 1, 1, 1, "f")])
        self.assertEqual(self.db.view_select(tables, joins, cols), (9, 1, 1, 1, "f"))

    def test_commit_tables_parallel(self) -> None:
        stats = self.db.commit_tables_parallel([(D.m_file_name, [(11, "a")], []), (D.m_project, [], [])])
        self.assertEqual([(s.table_name, s.inserted, s.updated) for s in stats], [("m_file_name", 1, 0)])
        self.assertEqual(self.db.select(D.m_file_name, (11, None)), (11, "a"))

    def test_reserve_id_block(self) -> None:
        self.db.insert(D.m_file_name, ((3, "c"),))
        first = self.db.reserve_id_block(D.m_file_name, 5)
        second = self.db.reserve_id_block(D.m_file_name, 5)
        self.assertEqual(first[1] - first[0], 4)
        self.assertEqual(second[0], first[1] + 1)

    # ---- pipeline intents ------------------------------------------------------------------------------------
    def test_parse_task_queue(self) -> None:
        rows = [
            (0, "stable-6.1", "stable", 1, "v6.1", 10, "", 0, 1),
            (0, "stable-6.1", "stable", 2, "v6.1.1", 11, "", 0, 1),
            (0, "mainline", "mainline", 1, "v6.2", 12, "", 0, 1),
        ]
        self.assertEqual(self.db.enqueue_parse_tasks(rows), 3)
        got = self.db.next_eligible_parse_tasks(0, 1, limit=10)
        self.assertEqual([r[3] for r in got], ["v6.1", "v6.1.1", "v6.2"])
        task_id = got[0][0]
        self.db.update_rows("m_parse_task", {"status": 2, "worker_id": "w1"}, {"task_id": task_id})
        self.assertEqual(self.db.record_task_failure(task_id, "bad", worker_id="w1", max_attempts=2), 1)
        self.assertEqual(self.db.select_value("m_parse_task", "status", {"task_id": task_id}), 1)
        self.db.update_rows("m_parse_task", {"status": 2, "worker_id": "w1"}, {"task_id": task_id})
        self.db.record_task_failure(task_id, "bad", max_attempts=2)
        self.assertEqual(self.db.select_value("m_parse_task", "status", {"task_id": task_id}), 4)
        self.assertEqual(self.db.record_task_failure(task_id, "again"), 0)

    # ---- reader ----------------------------------------------------------------------------------------------
    # ---- phase B: uniform semantics -------------------------------------------------------------------------
    TASK_COLS = ["network_id", "chain_name", "chain_kind", "ref_name", "status", "retry_count", "commit_id", "base_commit_id"]

    def _task(self, ref: str, status: int = 1, retry: int = 0, cid: int = 0, base: int = 0, err: Any = "") -> None:
        self.db.insert_rows("m_parse_task", self.TASK_COLS + ["error_msg"], [(0, "c", "stable", ref, status, retry, cid, base, err)])

    def _get(self, ref: str, col: str) -> Any:
        return self.db.select_value("m_parse_task", col, {"ref_name": ref})

    def test_upsert_expressions_read_the_stored_row_in_any_written_order(self) -> None:
        self._task("t", status=1, retry=5)
        offered = [(0, "c", "stable", "t", 4, 0, 0, 0)]
        for order in (("retry_count", "status"), ("status", "retry_count")):
            exprs = {
                "status": Case([(Pred("retry_count", ge(5)), 9)], Incoming("status")),
                "retry_count": Incoming("retry_count"),
            }
            self.db.update_rows("m_parse_task", {"status": 1, "retry_count": 5}, {"ref_name": "t"})
            self.db.insert_rows(
                "m_parse_task", self.TASK_COLS, offered, on_conflict="update",
                update_exprs={k: exprs[k] for k in order},
            )
            self.assertEqual((self._get("t", "status"), self._get("t", "retry_count")), (9, 0), order)

    def test_upsert_plain_incoming_columns_do_not_hide_stored_reads(self) -> None:
        self._task("t", status=1, retry=5)
        self.db.insert_rows(
            "m_parse_task", self.TASK_COLS, [(0, "c", "stable", "t", 4, 0, 0, 0)], on_conflict="update",
            update_columns=["retry_count"],
            update_exprs={"status": Case([(Pred("retry_count", ge(5)), 9)], Incoming("status"))},
        )
        self.assertEqual((self._get("t", "status"), self._get("t", "retry_count")), (9, 0))

    def test_update_rows_expressions_read_the_stored_row(self) -> None:
        self._task("t", status=1, retry=5)
        self.db.update_rows(
            "m_parse_task",
            {"retry_count": 0, "status": Case([(Pred("retry_count", ge(5)), 9)], 1), "commit_id": Col("retry_count")},
            {"ref_name": "t"},
        )
        self.assertEqual((self._get("t", "status"), self._get("t", "retry_count"), self._get("t", "commit_id")), (9, 0, 5))

    def test_assignments_that_read_each_other_raise(self) -> None:
        self._task("t")
        with self.assertRaises(SpecError):
            self.db.update_rows("m_parse_task", {"commit_id": add("base_commit_id", 1), "base_commit_id": add("commit_id", 1)},
                                {"ref_name": "t"})
        with self.assertRaises(SpecError):
            self.db.insert_rows(
                "m_parse_task", self.TASK_COLS, [(0, "c", "stable", "t", 1, 0, 0, 0)], on_conflict="update",
                update_exprs={"commit_id": add("base_commit_id", 1), "base_commit_id": add("commit_id", 1)},
            )

    def test_affected_row_counts_are_uniform(self) -> None:
        row = lambda ref, status=1: (0, "c", "stable", ref, status, 0, 0, 0)  # noqa: E731
        self.assertEqual(self.db.insert_rows("m_parse_task", self.TASK_COLS, [row("a"), row("b")]), 2)
        self.assertEqual(self.db.insert_rows("m_parse_task", self.TASK_COLS, [row("a"), row("c")], on_conflict="ignore"), 1)
        # update / replace count every offered row: inserted, changed or left identical
        self.assertEqual(self.db.insert_rows("m_parse_task", self.TASK_COLS, [row("a", 2), row("d")],
                                             on_conflict="update", update_columns=["status"]), 2)
        self.assertEqual(self.db.insert_rows("m_parse_task", self.TASK_COLS, [row("a", 2)],
                                             on_conflict="update", update_columns=["status"]), 1)
        self.assertEqual(self.db.insert_rows("m_parse_task", self.TASK_COLS, [row("a", 3), row("e")], on_conflict="replace"), 2)
        self.assertEqual(self.db.insert_rows("m_parse_task", self.TASK_COLS, [row("a", 3)], on_conflict="replace"), 1)
        # update_rows counts the matched rows, also when the values do not change
        self.assertEqual(self.db.update_rows("m_parse_task", {"status": 7}, {"ref_name": ["a", "b"]}), 2)
        self.assertEqual(self.db.update_rows("m_parse_task", {"status": 7}, {"ref_name": ["a", "b"]}), 2)
        self.assertEqual(self.db.update_rows("m_parse_task", {"status": 7}, {"ref_name": "nope"}), 0)
        self.assertEqual(self.db.update_rows("m_parse_task", {"status": 8}, {"status": 7}, limit=1), 1)
        self.assertEqual(self.db.delete_rows("m_parse_task", {"ref_name": ["a", "b", "zzz"]}), 2)
        self.assertEqual(self.db.delete_rows("m_parse_task", {"ref_name": "a"}), 0)

    def test_compare_and_set_claims_once(self) -> None:
        self._task("t", status=1)
        won = [self.db.update_rows("m_parse_task", {"status": 2, "worker_id": w}, {"ref_name": "t", "status": 1}) for w in ("w1", "w2")]
        self.assertEqual(won, [1, 0])
        self.assertEqual(self._get("t", "worker_id"), "w1")

    def test_none_means_is_null(self) -> None:
        self._task("n", err=None)
        self._task("e", err="boom")
        refs = lambda w: sorted(self.db.select_values("m_parse_task", "ref_name", w))  # noqa: E731
        self.assertEqual(refs({"error_msg": None}), ["n"])
        self.assertEqual(refs({"error_msg": is_null()}), ["n"])
        self.assertEqual(refs({"error_msg": eq(None)}), ["n"])
        self.assertEqual(refs({"error_msg": ne(None)}), ["e"])
        self.assertEqual(refs({"error_msg": not_null()}), ["e"])
        self.assertEqual(refs({"error_msg": ne("boom")}), [], "!= never matches NULL")
        self.assertEqual(refs([Pred("error_msg", None)]), ["n"])
        self.assertEqual(self.db.count_rows("m_parse_task", {"error_msg": None}), 1)
        for bad in (lt(None), gt(None), le(None), between(1, None)):
            with self.assertRaises(SpecError):
                self.db.select_values("m_parse_task", "ref_name", {"commit_id": bad})
        self.assertEqual(self.db.update_rows("m_parse_task", {"status": 6}, {"error_msg": None}), 1)
        self.assertEqual(self.db.delete_rows("m_parse_task", {"error_msg": None}), 1)

    def test_several_conditions_on_one_column(self) -> None:
        self._names("a", "b", "c", "d")
        names = lambda w: sorted(self.db.select_values("m_file_name", "fname", w))  # noqa: E731
        self.assertEqual(names({"fname": [ge("b"), lt("d")]}), ["b", "c"])
        self.assertEqual(names(all_of({"fname": ge("b")}, {"fname": lt("d")})), ["b", "c"])
        self.assertEqual(names(any_of({"fname": "a"}, {"fname": ge("d")})), ["a", "d"])
        self.assertEqual(names({"fname": [ne("a"), not_in(["d"]), ge("a")]}), ["b", "c"])
        self.assertEqual(names({"fname": []}), [], "empty IN matches nothing")
        with self.assertRaises(SpecError):
            names({"fname": [ge("a"), "b"]})
        with self.assertRaises(SpecError):
            names({"fname": {"x": 1}})

    def test_unknown_alias_is_rejected(self) -> None:
        self._names("a")
        from db_engine.spec import finished_ref
        with self.assertRaises(SpecError) as ctx:
            self.db.select_rows(("m_file_name", "f"), ["f.fname"], [finished_ref("r")])
        self.assertIn("'r.commit_id'", str(ctx.exception))
        self.assertIn("f", str(ctx.exception))
        with self.assertRaises(SpecError):
            self.db.select_rows(("m_file_name", "f"), ["x.fname"])
        with self.assertRaises(SpecError):
            self.db.select_rows(("m_file_name", "f"), ["f.fname"], order_by=["-g.fname"])

    def test_reader_is_read_only(self) -> None:
        self._names("r1")
        with self.db.reader() as session:
            self.assertEqual(session.select_values("m_file_name", "fname", {"fname": "r1"}), ["r1"])
            self.assertEqual(session.count_rows("m_file_name", {"fname": "r1"}), 1)
            self.assertTrue(session.column_exists("m_file_name", "fname"))
            with self.assertRaises(ReadOnlyViolation):
                session.insert_rows("m_file_name", ["fname"], [("w",)])
            with self.assertRaises(ReadOnlyViolation):
                session.delete_rows("m_file_name", None, all_rows=True)
        self.assertEqual(self.db.count_rows("m_file_name", {"fname": "r1"}), 1)


class PipelineContractMixin(_BackendCase):
    """Pipeline intents over several tables: a backend that enforces foreign keys needs every parent row, so the
    in-memory SQLite and MockDB variants run without them (the scenarios are about the operations, not the schema)."""

    def test_enqueue_parse_tasks_upsert_rules(self) -> None:
        def row(ref: str, status: int, cid: int = 0, base: int = 0) -> tuple:
            return (0, "stable-6.1", "stable", 1, ref, cid, "v6.1", base, status)

        def task(ref: str) -> tuple:
            return self.db.select_one("m_parse_task", ["status", "commit_id", "base_commit_id", "retry_count", "error_msg",
                                                       "worker_id"], {"ref_name": ref}, as_dict=False)

        self.db.enqueue_parse_tasks([row("failed", 0, 5, 1), row("done", 0), row("leased", 0), row("plain", 0)])
        self.db.update_rows("m_parse_task", {"status": 4, "retry_count": 3, "error_msg": "boom"}, {"ref_name": "failed"})
        self.db.update_rows("m_parse_task", {"status": 3, "retry_count": 2, "error_msg": "old"}, {"ref_name": "done"})
        self.db.update_rows("m_parse_task", {"status": 2, "worker_id": "w1", "retry_count": 1}, {"ref_name": "leased"})
        # published again: failed and no-longer-done tasks start over, a leased one keeps its state, known ids stay
        self.db.enqueue_parse_tasks([row("failed", 1, 0, 0), row("done", 0), row("leased", 1), row("plain", 1, 7, 8)])
        self.assertEqual(task("failed"), (1, 5, 1, 0, "", ""))
        self.assertEqual(task("done")[0], 0)
        self.assertEqual(task("done")[3:5], (0, ""))
        self.assertEqual(task("leased"), (2, 0, 0, 1, "", "w1"))
        self.assertEqual(task("plain")[:3], (1, 7, 8))
        # a leased task published as done is done; a done task published as done keeps its retry history
        self.db.enqueue_parse_tasks([row("leased", 3)])
        self.assertEqual(task("leased")[0], 3)
        self.db.update_rows("m_parse_task", {"retry_count": 2}, {"ref_name": "leased"})
        self.db.enqueue_parse_tasks([row("leased", 3)])
        self.assertEqual(task("leased")[0], 3)
        self.assertEqual(task("leased")[3], 2)
        self.assertEqual(self.db.count_rows("m_parse_task"), 4)

    def test_record_task_failure_counts_attempts(self) -> None:
        self.db.enqueue_parse_tasks([(0, "c", "stable", 1, "v1", 0, "", 0, 1)])
        task_id = self.db.select_value("m_parse_task", "task_id", {"ref_name": "v1"})
        statuses = []
        for _ in range(3):
            self.db.update_rows("m_parse_task", {"status": 2, "worker_id": "w1"}, {"task_id": task_id})
            self.assertEqual(self.db.record_task_failure(task_id, "x" * 2000, worker_id="w1"), 1)
            statuses.append(self.db.select_value("m_parse_task", "status", {"task_id": task_id}))
        self.assertEqual(statuses, [1, 1, 4], "FAILED on the third attempt")
        self.assertEqual(self.db.select_value("m_parse_task", "retry_count", {"task_id": task_id}), 3)
        self.assertEqual(len(self.db.select_value("m_parse_task", "error_msg", {"task_id": task_id})), 1000)
        self.assertEqual(self.db.select_value("m_parse_task", "worker_id", {"task_id": task_id}), "")
        # another worker's task is not touched
        self.db.update_rows("m_parse_task", {"status": 2, "worker_id": "w2"}, {"task_id": task_id})
        self.assertEqual(self.db.record_task_failure(task_id, "late", worker_id="w1"), 0)

    def test_checkpoint_deltas(self) -> None:
        from core import checkpoints

        base_tree = {f: 100 + f for f in range(1, 21)}
        head_tree = {**base_tree, 3: 903, 21: 921}
        del head_tree[4]
        for commit_id, tree in ((10, base_tree), (11, head_tree)):
            self.db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(commit_id, f, b) for f, b in tree.items()])
            self.db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"], [(1, commit_id, 2, 0)])
        self.assertFalse(checkpoints.is_checkpoint_pinned(self.db, 10))
        result = checkpoints.compact(self.db, 1, 11, current=12, ratio=0.5)
        self.assertEqual((result["base"], result["changed"], result["removed"], result["total"]), (10, 2, 1, 20))
        stored = dict(self.db.select_rows("m_bridge_checkpoint", ["fnid", "blob_id"], {"commit_id": 11}, as_dict=False))
        self.assertEqual(stored, {3: 903, 21: 921, 4: 0})
        self.assertEqual(dict(checkpoints.tree_rows(self.db, 11)), head_tree)
        self.assertEqual((checkpoints.base_of(self.db, 11), checkpoints.dependents(self.db, 10)), (10, [11]))
        self.assertEqual(checkpoints.expand(self.db, 11), len(head_tree) - 2)
        stored = dict(self.db.select_rows("m_bridge_checkpoint", ["fnid", "blob_id"], {"commit_id": 11}, as_dict=False))
        self.assertEqual(stored, head_tree)
        self.assertIsNone(checkpoints.base_of(self.db, 11))

    def _symbol_fixture(self) -> None:
        """Two blobs place tag 50 (one shared definition and reference of key 7 naming ast 1); blob 3 stores its own rows."""
        self.db.insert_rows("m_ast", ["ast_id", "name", "type_id"], [(1, "target", 1), (2, "caller", 1)])
        self.db.insert_rows("m_tag", ["tag_id", "hash", "ast_id", "hl_s", "hl_l"], [(50, b"\x01" * 32, 2, 0, 0)])
        self.db.insert_rows("m_bridge_tag", ["blob_id", "tag_id", "line_s", "line_e", "char_s", "char_e"],
                            [(1, 50, 100, 120, 0, 0), (2, 50, 10, 30, 0, 0), (3, 50, 5, 9, 0, 0)])
        self.db.insert_rows("m_blob_symbol", ["blob_id", "sym_key", "target_ast_id"], [(1, 7, 1), (2, 7, 1), (3, 7, 1), (2, 8, 2)])
        self.db.insert_rows("m_tag_ref", ["tag_id", "ref_idx", "sym_key", "role", "rel_line", "char_s"], [(50, 0, 7, 3, 4, 2)])
        self.db.insert_rows("m_tag_def", ["tag_id", "def_idx", "sym_key", "name", "type_id", "linkage", "rel_line_s", "rel_line_e"],
                            [(50, 0, 7, "target", 1, 0, 0, 2)])
        self.db.insert_rows("m_symbol_own", ["blob_id", "tag_id", "kind"], [(3, 50, 1), (3, 50, 2)])
        self.db.insert_rows("m_blob_symbol_ref", ["blob_id", "tag_id", "ast_id", "role", "line", "char_s"], [(3, 50, 1, 3, 6, 1)])
        self.db.insert_rows("m_blob_symbol_def", ["blob_id", "tag_id", "ast_id", "name", "type_id", "linkage", "line_s", "line_e"],
                            [(3, 50, 1, "target", 1, 0, 5, 7)])

    def test_symbol_refs_and_def_blobs_of_targets(self) -> None:
        self._symbol_fixture()
        refs = self.db.symbol_refs_of_targets([1])
        got = sorted((r["fid"], r["line"], r["char_s"], r["role"], r["tag_id"], r["ast_id"]) for r in refs)
        self.assertEqual(got, [(1, 104, 2, 3, 50, 1), (2, 14, 2, 3, 50, 1), (3, 6, 1, 3, 50, 1)])
        with_caller = self.db.symbol_refs_of_targets([1], role=3, with_caller=True, limit=2)
        self.assertEqual(len(with_caller), 2)
        self.assertTrue(all(r["caller"] == "caller" for r in with_caller))
        self.assertEqual(self.db.symbol_refs_of_targets([1], role=4), [])
        self.assertEqual(self.db.symbol_refs_of_targets([]), [])
        self.assertEqual(self.db.symbol_def_blobs_of_targets([1, 99]), {(1, 1), (1, 2), (1, 3)})
        self.assertEqual(self.db.symbol_def_blobs_of_targets([2]), set(), "key 8 has no tag definition")


    def test_read_session_symbol_readers(self) -> None:
        self._symbol_fixture()
        with self.db.reader() as r:
            self.assertEqual(len(r.symbol_refs_of_targets([1])), 3)
            self.assertEqual(r.symbol_def_blobs_of_targets([1, 99]), {(1, 1), (1, 2), (1, 3)})
            self.assertEqual(r.symbol_refs_of_targets([]), [])

    def test_new_checkpoint_pairs_clear_and_include_references(self) -> None:
        extra = [D.m_file_reference, D.m_ast_include, D.m_kbuild_symbol, D.m_arch_layout, D.m_arch_root, D.m_arch_layout_entry]
        names = [t.table_name for t in extra]
        self.db.create_table(extra)
        try:
            db = self.db
            db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(1, 1, 10), (2, 1, 10), (2, 2, 10), (2, 3, 30)])
            db.insert_rows("m_ast", ["ast_id", "name", "type_id"], [(1, "linux/a.h", 5), (2, "b.h", 6)])
            db.insert_rows("m_ast_include", ["ast_id", "fnid"], [(1, 7), (2, 8)])
            db.insert_rows("m_tag", ["tag_id", "hash", "ast_id", "hl_s", "hl_l"], [(1, b"" * 20, 1, 0, 0), (2, b"" * 20, 2, 0, 0)])
            db.insert_rows("m_bridge_tag", ["blob_id", "tag_id", "line_s", "line_e", "char_s", "char_e"],
                           [(10, 1, 3, 3, 0, 0), (10, 2, 4, 4, 0, 0)])
            stale = [(1, 10, 1, 7, 1, 1, "x"), (2, 10, 2, 7, 1, 1, "x"), (3, 99, 3, 7, 1, 1, "x")]
            db.insert_rows("m_file_reference", ["ref_id", "source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details"], stale)
            with db.new_checkpoint_pairs(2, 1) as pairs:
                self.assertEqual(pairs.pairs, {(2, 10), (3, 30)})
                self.assertEqual(pairs.delete_rows_of("m_file_reference"), 1)
                self.assertEqual(pairs.insert_include_references((5,), 11, 12), 2)
            rows = db.select_rows("m_file_reference", ["source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details"],
                                  order_by=["source_fnid", "target_fnid"], as_dict=False)
            self.assertEqual([tuple(r) for r in rows if r[1] == 2], [(10, 2, 7, 11, 3, "linux/a.h"), (10, 2, 8, 12, 4, "b.h")])
            self.assertEqual(len([r for r in rows if r[1] == 1]), 1, "a carried-over pair keeps its rows")
        finally:
            self.db.drop_tables(names, ignore_fk=True)

    def test_arch_layout_bulk_upserts(self) -> None:
        extra = [D.m_arch_layout, D.m_arch_root, D.m_arch_layout_entry]
        self.db.create_table(extra)
        try:
            self.db.insert_rows("m_arch_layout", ["layout_hash"], [(b"a" * 16,), (b"b" * 16,)], on_conflict="ignore")
            self.db.insert_rows("m_arch_layout", ["layout_hash"], [(b"a" * 16,), (b"c" * 16,)], on_conflict="ignore")
            ids = {bytes(h): i for h, i in self.db.select_rows("m_arch_layout", ["layout_hash", "layout_id"],
                                                               {"layout_hash": [b"a" * 16, b"c" * 16]}, as_dict=False)}
            self.assertEqual(set(ids), {b"a" * 16, b"c" * 16})
            self.assertTrue(all(isinstance(k, bytes) for k in ids))
            roots = [(1, "x86", ids[b"a" * 16]), (1, "arm", ids[b"c" * 16])]
            self.db.insert_rows("m_arch_root", ["commit_id", "arch", "layout_id"], roots, on_conflict="update", update_columns=["layout_id"])
            self.db.insert_rows("m_arch_root", ["commit_id", "arch", "layout_id"], [(1, "x86", ids[b"c" * 16])],
                                on_conflict="update", update_columns=["layout_id"])
            self.assertEqual(self.db.select_value("m_arch_root", "layout_id", {"commit_id": 1, "arch": "x86"}), ids[b"c" * 16])
            self.assertEqual(self.db.count_rows("m_arch_root"), 2)
        finally:
            self.db.drop_tables([t.table_name for t in extra], ignore_fk=True)

    def _webapp_fixture(self) -> list[str]:
        extra = [D.m_maintainer_person, D.m_maintainer_section, D.m_maintainer_member, D.m_credits_entry, D.m_commit, D.m_commit_file]
        self.db.create_table(extra)
        db = self.db
        db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(1, "Ann Dev", "ann@x.org"), (2, "", "bob@y.org"), (3, "Bob", "bob@y.org")])
        db.insert_rows("m_maintainer_section", ["sec_id", "blob_id", "name", "status", "scm_tree", "web_page", "mailing_list", "ast_id"],
                       [(1, 1, "NET", "Maintained", "", "", "", 0), (2, 1, "FS_100%", "Odd Fixes", "", "", "", 0)])
        db.insert_rows("m_maintainer_member", ["sec_id", "person_id", "role_type", "priority"], [(1, 1, 1, 0), (1, 3, 2, 0), (2, 1, 2, 0)])
        db.insert_rows("m_credits_entry", ["credit_id", "blob_id", "person_id", "web_page", "pgp_key", "description", "snail_mail", "ast_id"],
                       [(7, 1, 3, "", "", "", "", 0)])
        db.insert_rows("m_commit", ["commit_id", "network_id", "commit_hash", "author_id", "author_date", "committer_id", "committer_date", "subject", "message"],
                       [(1, 1, bytes.fromhex("ab" * 20), 1, 100, 1, 100, "net: fix 100% leak", ""),
                        (2, 1, bytes.fromhex("ac" * 20), 3, 200, 3, 200, "fs: other", ""),
                        (3, 2, bytes.fromhex("ad" * 20), 3, 300, 3, 300, "elsewhere", "")])
        db.insert_rows("m_commit_file", ["commit_id", "fnid", "blob_id", "old_fnid", "old_blob_id", "change_type"], [(1, 1, 1, 0, 0, "M"), (1, 2, 2, 0, 0, "A")])
        return [t.table_name for t in extra]

    def test_read_session_intents(self) -> None:
        names = self._webapp_fixture()
        try:
            with self.db.reader() as r:
                commits = r.list_network_commits(1)
                self.assertEqual([c["commit_id"] for c in commits], [2, 1])
                self.assertEqual([c["files_count"] for c in commits], [0, 2])
                self.assertEqual(commits[1]["author_name"], "Ann Dev")
                self.assertEqual([c["commit_id"] for c in r.list_network_commits(1, newest_first=False, limit=1, offset=1)], [2])
                self.assertEqual([c["commit_id"] for c in r.list_network_commits(1, hash_prefix="ab")], [1])
                self.assertEqual([c["commit_id"] for c in r.list_network_commits(1, text="100%")], [1])
                self.assertEqual([c["commit_id"] for c in r.list_network_commits(1, text="BOB")], [2])
                self.assertEqual([c["commit_id"] for c in r.list_network_commits(1, id_min=1, id_min_exclusive=True, id_max=2)], [2])
                self.assertEqual([c["commit_id"] for c in r.list_network_commits(1, authored_before=150)], [1])

                secs = r.maintainer_sections()
                self.assertEqual([s["sec_id"] for s in secs][:2], [2, 1])
                self.assertIn("patchwork", secs[0])
                self.assertEqual([s["name"] for s in r.maintainer_sections([1])], ["NET"])
                self.assertEqual(r.maintainer_sections([]), [])
                self.assertEqual([s["sec_id"] for s in r.maintainer_sections(member_person_ids=[3])], [1])
                self.assertEqual([s["sec_id"] for s in r.maintainer_sections(text="100%")], [2])
                self.assertEqual([s["sec_id"] for s in r.maintainer_sections(text="BOB@")], [1])
                self.assertEqual([s["sec_id"] for s in r.maintainer_sections(name="NET", limit=1)], [1])

                directory = {d["person_id"]: d for d in r.maintainer_developer_directory()}
                self.assertEqual(set(directory), {1, 2, 3})
                self.assertEqual((directory[1]["subsystems_count"], directory[1]["is_maintainer"], directory[1]["is_reviewer"]), (2, 1, 1))
                self.assertEqual((directory[3]["in_credits"], directory[3]["has_commits"], directory[2]["has_commits"]), (1, 1, 0))
                restricted = {d["person_id"]: d for d in r.maintainer_developer_directory([2], [])}
                self.assertEqual((restricted[1]["subsystems_count"], restricted[3]["subsystems_count"], restricted[3]["in_credits"]), (1, 0, 0))

                self.assertEqual(r.find_maintainer_person("2", "2", "", "")["person_id"], 2)
                self.assertEqual(r.find_maintainer_person("<BOB@y.org>", "bob@y.org", "bob@y.org", "")["person_id"], 3, "named person first")
                self.assertEqual(r.find_maintainer_person("x", "x", "", "ann dev")["person_id"], 1)
                self.assertIsNone(r.find_maintainer_person("nobody", "nobody", "", ""))
        finally:
            self.db.drop_tables(names, ignore_fk=True)

    def test_read_session_symbol_names_and_checkpoint_tree(self) -> None:
        db = self.db
        db.insert_rows("m_tag_def", ["tag_id", "def_idx", "sym_key", "name", "type_id", "linkage", "rel_line_s", "rel_line_e"],
                       [(1, 0, 1, "foo", 1, 0, 0, 1), (2, 0, 2, "foobar", 1, 0, 0, 1), (3, 0, 3, "fo_x", 1, 0, 0, 1), (4, 0, 4, "foo", 1, 0, 0, 1)])
        db.insert_rows("m_file_name", ["fnid", "fname"], [(1, "a/x.h"), (2, "b/x.h"), (3, "dir")])
        db.insert_rows("m_file_blob", ["blob_id", "blob_hash", "ftype"], [(1, b"\x01" * 20, 1), (2, b"\x02" * 20, 1), (3, b"\x03" * 20, 0)])
        db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(5, 1, 1), (5, 2, 2), (5, 3, 3)])
        with db.reader() as r:
            self.assertEqual(r.symbol_name_page("fo"), ["foo", "fo_x", "foobar"])
            self.assertEqual(r.symbol_name_page("fo_"), ["fo_x"], "underscore is literal")
            self.assertEqual(r.symbol_name_page("fo", after=(3, "foo")), ["fo_x", "foobar"])
            self.assertEqual(r.symbol_name_page("fo", limit=1), ["foo"])
            self.assertTrue(r.checkpoint_tree_exists(5))
            self.assertFalse(r.checkpoint_tree_exists(6))
            self.assertEqual(r.checkpoint_tree_source(), "m_bridge_checkpoint" if not db.table_exists("m_checkpoint_tree") else "m_checkpoint_tree")
            rows = r.checkpoint_tree_files(5, files_only=True, order_by_name=True, with_ftype=True)
            self.assertEqual([(x["fname"], x["blob_id"], x["ftype"]) for x in rows], [("a/x.h", 1, 1), ("b/x.h", 2, 1)])
            self.assertEqual([x["fname"] for x in r.checkpoint_tree_files(5, fnames=["dir"])], ["dir"])
            self.assertEqual([x["fname"] for x in r.checkpoint_tree_files(5, name=like("b/", "prefix"))], ["b/x.h"])
            self.assertEqual(r.checkpoint_tree_files(5, fnames=[]), [])
            self.assertEqual(r.checkpoint_find_file(5, ["zzz", "b/x.h"], suffix="/x.h"), "b/x.h")
            self.assertEqual(r.checkpoint_find_file(5, ["zzz"], suffix="/x.h"), "a/x.h")
            self.assertIsNone(r.checkpoint_find_file(5, ["dir"]), "directories are skipped")
            self.assertIsNone(r.checkpoint_find_file(5, []))

    def test_catalog_helpers_used_by_start_migrations(self) -> None:
        self.assertEqual(self.db.existing_tables(["m_file_name", "m_nope"], include_views=False), {"m_file_name"})
        info = self.db.column_info("m_file_blob", "blob_hash")
        self.assertIsNotNone(info)
        self.assertIsNone(self.db.column_info("m_nope", "x"))
        self.assertTrue(self.db.column_exists("m_file_name", "fnid"))
        self.assertFalse(self.db.column_exists("m_file_name", "nope"))
        self.db.check_if_connected()


def _make_case(name: str, factory: Callable[[], BaseDBEngine] | None) -> type[unittest.TestCase]:
    cls = type(f"TestContract_{name}", (ContractMixin, unittest.TestCase), {"make_db": staticmethod(factory) if factory else None})
    if factory is None:
        cls = unittest.skip(f"{name} backend is not available")(cls)
    return cls


for _name, _factory in _backends().items():
    globals()[f"TestContract_{_name}"] = _make_case(_name, _factory)
del _name, _factory

for _name, _factory in {"mock": lambda: MockDB(use_global=False), "sqlite": lambda: SQLiteDB(":memory:", foreign_keys=False)}.items():
    globals()[f"TestPipelineContract_{_name}"] = type(
        f"TestPipelineContract_{_name}", (PipelineContractMixin, unittest.TestCase), {"make_db": staticmethod(_factory)}
    )
del _name, _factory


class TestSQLiteFileDatabase(unittest.TestCase):
    """What only a file-backed `SQLiteDB` can show: persistence, a separate read-only connection, capabilities."""

    def test_persistence_and_reader_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "k.sqlite3")
            db = SQLiteDB(path)
            self.assertTrue(db.has_capability("persistent"))
            db.create_table([D.m_file_name])
            db.insert_rows("m_file_name", ["fname"], [("kept",)])
            with db.reader() as session:
                self.assertEqual(session.select_values("m_file_name", "fname", {"fname": "kept"}), ["kept"])
                with self.assertRaises(ReadOnlyViolation):
                    session.delete_rows("m_file_name", {"fname": "kept"})
            db.close()
            again = SQLiteDB(path)
            self.assertEqual(again.select_values("m_file_name", "fname", {"fname": "kept"}), ["kept"])
            again.close()
            ro = SQLiteDB(path, read_only=True)
            with self.assertRaises(ReadOnlyViolation):
                ro.insert_rows("m_file_name", ["fname"], [("x",)])
            with self.assertRaises(ReadOnlyViolation):
                ro.delete_rows("m_file_name", {"fname": "kept"})
            ro.close()

    def test_memory_capabilities(self) -> None:
        mem = SQLiteDB(":memory:")
        self.assertFalse(mem.has_capability("persistent"))
        self.assertFalse(mem.has_capability("server_tuning"))
        probe = SQLiteDB.probe_connection({"path": ":memory:"})
        self.assertTrue(probe.ok)
        self.assertTrue(probe.version)

    def test_probe_missing_directory(self) -> None:
        self.assertFalse(SQLiteDB.probe_connection({"path": "/nonexistent-dir-xyz/k.sqlite3"}).ok)


class TestForeignSQLiteFiles(unittest.TestCase):
    """Foreign (externally produced) SQLite files: `create_foreign_sqlite()` builds one, `open_foreign_sqlite()` reads it."""

    def test_write_then_read_only_access(self) -> None:
        import threading

        from db_engine import DBError, MissingTableError, create_foreign_sqlite, open_foreign_sqlite
        from db_engine.spec import INT, TEXT, Hex, MEDIUMBLOB

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "over.sqlite3")
            with create_foreign_sqlite(path) as w:
                w.create_table("over", {"num": INT(), "subject": TEXT(null=True), "oid": MEDIUMBLOB(null=True)}, primary=("num",))
                w.insert_rows("over", ["num", "subject", "oid"], [(1, "a", b"\x0a\x0b"), (2, "b", None), (3, "c", None)])
            reader = open_foreign_sqlite(path)
            self.assertEqual(reader.select_rows("over", ["num"], order_by=["-num"], limit=2), [(3,), (2,)])
            self.assertEqual(reader.select_one("over", {"h": Hex("oid")}, {"num": 1}), ("0A0B",))
            self.assertEqual(reader.count_rows("over"), 3)
            self.assertEqual(list(reader.iter_rows("over", ["subject"], order_by=["num"], chunk_size=2)), [("a",), ("b",), ("c",)])
            with self.assertRaises(MissingTableError):
                reader.select_rows("nope", ["x"])
            with self.assertRaises(DBError):
                reader.select_rows("over", ["nope"])
            # one connection per thread; a request thread closes its own
            seen: list[int] = []
            worker = threading.Thread(target=lambda: (seen.append(reader.count_rows("over")), reader.close_thread()))
            worker.start()
            worker.join()
            self.assertEqual(seen, [3])
            reader.close()


if __name__ == "__main__":
    unittest.main()
