"""unit_test/test_db_spec_ext.py - Phase 1 db_engine extensions: expressions, group/having, subqueries, conditional
upsert, neutral errors, capability flags, neutral column specs, foreign SQLite reader, read-only sessions."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import mysql.connector

from db_engine import (
    CAP_BACKGROUND_COMMIT,
    CAP_PERSISTENT,
    CAP_SERVER_TUNING,
    Agg,
    Case,
    Coalesce,
    Col,
    ColumnSpec,
    ColumnType,
    Concat,
    ConnectionLostError,
    DBError,
    DuplicateKeyError,
    Hex,
    Incoming,
    Length,
    MariaDB,
    MissingTableError,
    MockDB,
    NullIf,
    Order,
    PacketTooLargeError,
    Pred,
    ReadOnlyViolation,
    Select,
    Substr,
    Value,
    add,
    all_of,
    any_of,
    between,
    eq,
    ge,
    le,
    lt,
    ne,
    is_null,
    SpecError,
    UnsupportedOperation,
    Const,
    available_engines,
    default_socket_paths,
    exists_select,
    get_db_engine,
    gt,
    in_select,
    open_foreign_sqlite,
    tuning_note_is_warning,
)
from db_engine.base import BaseDBEngine
from db_engine.mariadb_support import _coerce_bytes, _normalize_row, _wrap_driver_errors
from db_engine.errors import translate_mysql_error
from db_engine.spec import CURRENT_TIMESTAMP, Join
from db_engine.sql_render import (
    inline_params,
    render_column_ddl,
    render_copy,
    render_delete,
    render_drop_tables,
    render_insert_with_params,
    render_select,
    render_select_literal,
    render_update,
)

T = "m_project"  # canonical table: project_id, name, slug


class TestRenderExpressions(unittest.TestCase):
    def _both(self, select: Select) -> dict[str, tuple[str, list]]:
        return {d: render_select(select, d)[:2] for d in ("mariadb", "sqlite")}

    def test_concat_length_substr_dialects(self) -> None:
        sel = Select(("m_x", "a"), {"k": Concat("a.name", Value("_")), "n": Length("a.name"), "s": Substr("a.name", 1, 3)})
        out = self._both(sel)
        self.assertIn("CONCAT(a.name, %s)", out["mariadb"][0])
        self.assertIn("CHAR_LENGTH(a.name)", out["mariadb"][0])
        self.assertIn("(a.name || ?)", out["sqlite"][0])
        self.assertIn("LENGTH(a.name)", out["sqlite"][0])
        self.assertNotIn("CHAR_LENGTH", out["sqlite"][0])
        self.assertIn("SUBSTR(a.name, %s, %s)", out["mariadb"][0])
        self.assertEqual(out["mariadb"][1], ["_", 1, 3])

    def test_case_coalesce_nullif_arith_hex(self) -> None:
        case = Case([({"status": 5}, Value("x"))], Coalesce("note", NullIf("other", Value(""))))
        sel = Select("t", {"c": case, "p": add("n", 1), "h": Hex("blob")})
        sql, params, names = render_select(sel, "sqlite")
        self.assertEqual(
            sql,
            "SELECT CASE WHEN status = ? THEN ? ELSE COALESCE(note, NULLIF(other, ?)) END AS c, (n + ?) AS p, "
            "HEX(blob) AS h FROM t",
        )
        self.assertEqual(params, [5, "x", "", 1])
        self.assertEqual(names, ["c", "p", "h"])

    def test_group_by_having_order_hint(self) -> None:
        sel = Select(
            ("m_x", "a"),
            {"k": "a.k", "c": Agg("COUNT")},
            where={"a.id": gt(1)},
            group_by=["a.k"],
            having=Pred(Agg("COUNT"), gt(2)),
            order_by=[Order(Length("a.k"), True), "-a.k"],
            hint="straight_join",
            limit=5,
        )
        sql, params = render_select(sel, "mariadb")[:2]
        self.assertEqual(
            sql,
            "SELECT STRAIGHT_JOIN a.k AS k, COUNT(*) AS c FROM m_x a WHERE a.id > %s GROUP BY a.k "
            "HAVING COUNT(*) > %s ORDER BY CHAR_LENGTH(a.k) DESC, a.k DESC LIMIT 5",
        )
        self.assertEqual(params, [1, 2])
        sql_s, _ = render_select(sel, "sqlite")[:2]
        self.assertNotIn("STRAIGHT_JOIN", sql_s)

    def test_unknown_hint_rejected(self) -> None:
        with self.assertRaises(ValueError):
            render_select(Select("t", ["a"], hint="bogus"), "mariadb")  # type: ignore[arg-type]

    def test_pred_in_where(self) -> None:
        sel = Select("t", ["a"], where=[Pred(Length("name"), gt(3)), Pred(add("n", 1), 4)])
        sql, params = render_select(sel, "mariadb")[:2]
        self.assertIn("CHAR_LENGTH(name) > %s AND (n + %s) = %s", sql)
        self.assertEqual(params, [3, 1, 4])

    def test_in_select_and_exists_subqueries(self) -> None:
        sub = Select(("m_b", "b"), ["b.id"], where={"b.flag": 1}, distinct=True)
        sel = Select(("m_a", "a"), ["a.id"], where=[in_select("a.id", sub), exists_select(Select("m_c", ["x"], {"x": Col("a.id")}), negate=True)])
        sql, params = render_select(sel, "sqlite")[:2]
        self.assertIn("a.id IN (SELECT DISTINCT b.id FROM m_b b WHERE b.flag = ?)", sql)
        self.assertIn("NOT EXISTS (SELECT x FROM m_c WHERE x = a.id)", sql)
        self.assertEqual(params, [1])

    def test_tuple_in_select(self) -> None:
        sub = Select("m_k", ["fnid", "blob_id"], {"commit_id": 7})
        sql, params = render_select(Select("m_b", ["a"], [in_select(("fnid", "blob_id"), sub)]), "mariadb")[:2]
        self.assertIn("(fnid, blob_id) IN (SELECT fnid, blob_id FROM m_k WHERE commit_id = %s)", sql)
        self.assertEqual(params, [7])

    def test_offset_without_limit(self) -> None:
        self.assertIn("LIMIT -1 OFFSET 4", render_select(Select("t", ["a"], offset=4), "sqlite")[0])
        self.assertIn("OFFSET 4", render_select(Select("t", ["a"], offset=4), "mariadb")[0])


class TestRenderWrites(unittest.TestCase):
    EXPRS = {
        "status": Case([({"status": 3}, Incoming("status"))], "status"),
        "commit_id": Case([(Pred(Incoming("commit_id"), gt(0)), Incoming("commit_id"))], "commit_id"),
    }

    def test_conditional_upsert_both_dialects(self) -> None:
        sql_m, trail_m = render_insert_with_params(
            "m_parse_task", ["a", "status", "commit_id"], 1, "update", update_columns=["a"], update_exprs=self.EXPRS
        )
        self.assertEqual(
            sql_m,
            "INSERT INTO m_parse_task (a, status, commit_id) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE "
            "a = VALUES(a), status = CASE WHEN status = %s THEN VALUES(status) ELSE status END, "
            "commit_id = CASE WHEN VALUES(commit_id) > %s THEN VALUES(commit_id) ELSE commit_id END",
        )
        self.assertEqual(trail_m, [3, 0])
        sql_s, trail_s = render_insert_with_params(
            "m_parse_task", ["a", "status", "commit_id"], 2, "update", update_columns=["a"], update_exprs=self.EXPRS,
            dialect="sqlite",
        )
        self.assertIn("ON CONFLICT DO UPDATE SET a = excluded.a, status = CASE WHEN status = ? THEN excluded.status", sql_s)
        self.assertEqual(trail_s, [3, 0])

    def test_update_exprs_default_columns_and_validation(self) -> None:
        sql, _ = render_insert_with_params("t", ["a", "b"], 1, "update", update_exprs={"b": add("b", Incoming("b"))})
        self.assertTrue(sql.endswith("ON DUPLICATE KEY UPDATE b = (b + VALUES(b))"))
        with self.assertRaises(ValueError):
            render_insert_with_params("t", ["a"], 1, "ignore", update_exprs={"a": Incoming("a")})
        with self.assertRaises(ValueError):
            render_select(Select("t", [Incoming("a")]), "mariadb")

    def test_update_with_case_expr(self) -> None:
        sql, params = render_update(
            "m_parse_task",
            {"status": Case([(Pred(add("retry_count", 1), gt(2)), 9)], 1), "retry_count": add("retry_count", 1)},
            {"task_id": 4},
        )
        self.assertEqual(
            sql,
            "UPDATE m_parse_task SET status = CASE WHEN (retry_count + %s) > %s THEN %s ELSE %s END, "
            "retry_count = (retry_count + %s) WHERE task_id = %s",
        )
        self.assertEqual(params, [1, 2, 9, 1, 1, 4])

    def test_delete_limit_dialects(self) -> None:
        self.assertEqual(render_delete("t", {"a": 1}, limit=5)[0], "DELETE FROM t WHERE a = %s LIMIT 5")
        self.assertEqual(
            render_delete("t", {"a": 1}, limit=5, dialect="sqlite")[0],
            "DELETE FROM t WHERE rowid IN (SELECT rowid FROM t WHERE a = ? LIMIT 5)",
        )

    def test_update_limit_sqlite_uses_rowid(self) -> None:
        sql, params = render_update("t", {"a": 2}, {"b": 1}, limit=3, dialect="sqlite")
        self.assertEqual(sql, "UPDATE t SET a = ? WHERE rowid IN (SELECT rowid FROM t WHERE b = ? LIMIT 3)")
        self.assertEqual(params, [2, 1])

    def test_copy_with_join_distinct_expressions(self) -> None:
        sel = Select(
            ("m_a", "a"),
            ["a.x", Concat("a.name", Value("!")), Substr("a.name", 1, 255)],
            where={"b.id": Value(1)} if False else {"a.k": 1},
            joins=[Join(("m_b", "b"), {"b.id": "a.id"}, "left")],
            distinct=True,
            hint="straight_join",
        )
        sql, params = render_copy("m_t", ["x", "y", "z"], sel, "ignore")
        self.assertTrue(sql.startswith("INSERT IGNORE INTO m_t (x, y, z) SELECT STRAIGHT_JOIN DISTINCT a.x, CONCAT(a.name, %s)"))
        self.assertIn("LEFT JOIN m_b b ON b.id = a.id", sql)
        self.assertEqual(params, ["!", 1, 255, 1][:1] + [1, 255, 1])
        sql_s, _ = render_copy("m_t", ["x", "y", "z"], sel, "ignore", dialect="sqlite")
        self.assertTrue(sql_s.startswith("INSERT OR IGNORE INTO m_t (x, y, z) SELECT DISTINCT a.x, (a.name || ?)"))

    def test_drop_tables_render(self) -> None:
        self.assertEqual(render_drop_tables(["a", "b"]), ["DROP TABLE IF EXISTS a, b"])
        self.assertEqual(render_drop_tables(["a", "b"], if_exists=False, dialect="sqlite"), ["DROP TABLE a", "DROP TABLE b"])
        with self.assertRaises(ValueError):
            render_drop_tables(["a; DROP"])

    def test_inline_literals_for_views(self) -> None:
        sel = Select("t", ["a"], where={"n": "it's", "b": b"\x01", "i": 4})
        self.assertEqual(
            render_select_literal(sel, "sqlite"), "SELECT a FROM t WHERE n = 'it''s' AND b = X'01' AND i = 4"
        )
        self.assertEqual(inline_params("a = %s", ["x\\y"], "mariadb"), "a = 'x\\\\y'")
        with self.assertRaises(ValueError):
            inline_params("a = ?", [1, 2], "sqlite")


class TestColumnSpec(unittest.TestCase):
    def test_mariadb_ddl(self) -> None:
        self.assertEqual(
            render_column_ddl("n", ColumnSpec(ColumnType.INT, unsigned=True, nullable=False, default=0)),
            "n INT UNSIGNED NOT NULL DEFAULT 0",
        )
        self.assertEqual(render_column_ddl("h", ColumnSpec(ColumnType.BINARY, 16, nullable=False)), "h BINARY(16) NOT NULL")
        self.assertEqual(render_column_ddl("t", ColumnSpec(ColumnType.VARCHAR, 255, default="a'b")), "t VARCHAR(255) DEFAULT 'a''b'")
        self.assertEqual(
            render_column_ddl("ts", ColumnSpec(ColumnType.TIMESTAMP, default=CURRENT_TIMESTAMP)),
            "ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        )
        self.assertEqual(
            render_column_ddl("id", ColumnSpec(ColumnType.BIGINT, nullable=False, auto_increment=True)),
            "id BIGINT NOT NULL AUTO_INCREMENT",
        )
        self.assertEqual(render_column_ddl("f", ColumnSpec(ColumnType.BOOL, default=None)), "f TINYINT(1) DEFAULT NULL")

    def test_sqlite_ddl(self) -> None:
        self.assertEqual(render_column_ddl("n", ColumnSpec(ColumnType.INT, unsigned=True), "sqlite"), "n INTEGER")
        self.assertEqual(render_column_ddl("h", ColumnSpec(ColumnType.BINARY, 16), "sqlite"), "h BLOB")
        self.assertEqual(render_column_ddl("d", ColumnSpec(ColumnType.DOUBLE), "sqlite"), "d REAL")
        self.assertEqual(render_column_ddl("j", ColumnSpec(ColumnType.JSON), "sqlite"), "j TEXT")

    def test_length_required(self) -> None:
        with self.assertRaises(ValueError):
            ColumnSpec(ColumnType.VARCHAR)
        self.assertEqual(ColumnSpec("int").type, ColumnType.INT)


class TestMockEvaluation(unittest.TestCase):
    def setUp(self) -> None:
        self.db = MockDB(use_global=False)
        self.db.insert_rows(T, ["project_id", "name", "slug"], [(1, "foo", "a"), (2, "foo", "bb"), (3, "bar", "ccc"), (4, "baz", None)])

    def test_group_by_having_aggregates(self) -> None:
        rows = self.db.select_rows(
            T,
            {"name": "name", "c": Agg("COUNT"), "s": Agg("SUM", "project_id")},
            group_by=["name"],
            having=Pred(Agg("COUNT"), gt(1)),
            as_dict=True,
        )
        self.assertEqual(rows, [{"name": "foo", "c": 2, "s": 3}])

    def test_aggregate_group_by_is_honored(self) -> None:
        rows = self.db.aggregate(T, {"c": Agg("COUNT")}, group_by=["name"], order_by=["name"], as_dict=True)
        self.assertEqual([(r["name"], r["c"]) for r in rows], [("bar", 1), ("baz", 1), ("foo", 2)])

    def test_aggregate_having_and_empty_total(self) -> None:
        self.assertEqual(self.db.count_rows(T, {"name": "none"}), 0)
        self.assertEqual(self.db.select_rows(T, [Agg("MAX", "project_id")], {"name": "none"}, as_dict=False), [(None,)])

    def test_expressions_projection_and_order(self) -> None:
        rows = self.db.select_rows(
            T,
            {"id": "project_id", "k": Concat("name", Value("_"), "slug"), "n": Length("name"), "p": Substr("name", 2, 2)},
            order_by=[Order(Length("slug"), True), "project_id"],
            as_dict=True,
        )
        self.assertEqual([r["id"] for r in rows], [3, 2, 1, 4])  # NULL slug sorts last when descending
        self.assertIsNone(rows[3]["k"])
        self.assertEqual((rows[0]["k"], rows[0]["n"], rows[0]["p"]), ("bar_ccc", 3, "ar"))

    def test_case_coalesce_nullif_add(self) -> None:
        rows = self.db.select_rows(
            T,
            {
                "c": Case([({"project_id": [1, 2]}, Value("low"))], Value("high")),
                "s": Coalesce("slug", Value("-")),
                "z": NullIf("name", Value("foo")),
                "a": add("project_id", 10),
            },
            order_by=["project_id"],
            as_dict=True,
        )
        self.assertEqual([r["c"] for r in rows], ["low", "low", "high", "high"])
        self.assertEqual(rows[3]["s"], "-")
        self.assertEqual([r["z"] for r in rows], [None, None, "bar", "baz"])
        self.assertEqual([r["a"] for r in rows], [11, 12, 13, 14])

    def test_pred_filters(self) -> None:
        got = self.db.select_values(T, "project_id", [Pred(Length("slug"), gt(1))], order_by=["project_id"])
        self.assertEqual(got, [2, 3])

    def test_in_select_and_exists_subqueries(self) -> None:
        sub = Select(T, ["name"], {"project_id": gt(2)})
        self.assertEqual(self.db.select_values(T, "project_id", [in_select("name", sub)], order_by=["project_id"]), [3, 4])
        self.assertEqual(self.db.select_values(T, "project_id", [in_select("name", sub, negate=True)], order_by=["project_id"]), [1, 2])
        from db_engine import Col

        correlated = exists_select(Select(("m_project", "q"), ["q.project_id"], {"q.name": Col("p.name"), "q.project_id": gt(1)}))
        got = self.db.select_values((T, "p"), "p.project_id", [correlated], order_by=["p.project_id"])
        # every row with id > 1 matches itself; foo(1) matches its sibling foo(2)
        self.assertEqual(got, [1, 2, 3, 4])

    def test_tuple_in_select(self) -> None:
        sub = Select(T, ["name", "slug"], {"project_id": 3})
        self.assertEqual(self.db.select_values(T, "project_id", [in_select(("name", "slug"), sub)]), [3])

    def test_distinct_join_copy_rows(self) -> None:
        self.db.insert_rows("m_project_alias", ["a", "b"], []) if False else None
        sel = Select(T, {"x": "project_id", "y": Concat("name", Value("!"))}, {"name": "foo"}, distinct=True)
        n = self.db.copy_rows("m_project", ["project_id", "name"], Select(T, ["project_id", Concat("name", Value("2"))], {"name": "bar"}, ), on_conflict="ignore")
        self.assertEqual(n, 0)
        self.assertEqual(len(self.db.select_spec(sel)), 2)

    def test_conditional_upsert(self) -> None:
        exprs = {"slug": Case([(Pred(Incoming("project_id"), gt(1)), Concat("slug", Incoming("name")))], "slug")}
        self.db.insert_rows(T, ["project_id", "name"], [(1, "z"), (2, "y")], on_conflict="update", update_exprs=exprs)
        got = dict(self.db.select_rows(T, ["project_id", "slug"], {"project_id": [1, 2]}))
        self.assertEqual(got, {1: "a", 2: "bby"})
        # name was not named in update_columns/update_exprs, so it is unchanged
        self.assertEqual(self.db.select_value(T, "name", {"project_id": 2}), "foo")

    def test_update_exprs_requires_update_mode(self) -> None:
        with self.assertRaises(ValueError):
            self.db.insert_rows(T, ["project_id"], [(9,)], update_exprs={"name": Incoming("name")})

    def test_update_rows_expression(self) -> None:
        self.db.update_rows(T, {"slug": Concat("name", Value("-"), "slug")}, {"project_id": 1})
        self.assertEqual(self.db.select_value(T, "slug", {"project_id": 1}), "foo-a")

    def test_delete_rows_limit(self) -> None:
        self.assertEqual(self.db.delete_rows(T, {"name": "foo"}, limit=1), 1)
        self.assertEqual(self.db.count_rows(T, {"name": "foo"}), 1)

    def test_neutral_errors(self) -> None:
        with self.assertRaises(DuplicateKeyError):
            self.db.insert_rows(T, ["project_id"], [(1,)])
        with self.assertRaises(ValueError):  # historic contract
            self.db.insert_rows(T, ["project_id"], [(1,)])
        with self.assertRaises(MissingTableError):
            self.db.select_rows("no_such_table", ["a"])
        with self.assertRaises(DBError):
            self.db.delete_rows("no_such_table", {"a": 1})

    def test_drop_tables_ignore_fk_and_view_select(self) -> None:
        self.db.create_view("v_demo", Select(T, ["name"]))
        self.assertTrue(self.db.table_exists("v_demo"))
        self.db.drop_view("v_demo")
        self.assertFalse(self.db.table_exists("v_demo"))
        self.db.drop_tables(["m_project"], if_exists=True)
        with self.assertRaises(MissingTableError):
            self.db.count_rows(T)

    def test_add_column_and_column_info(self) -> None:
        self.assertTrue(self.db.add_column(T, "extra", ColumnSpec(ColumnType.BINARY, 20, nullable=False)))
        self.assertFalse(self.db.add_column(T, "extra", ColumnSpec(ColumnType.BINARY, 20)))
        info = self.db.column_info(T, "extra")
        self.assertIsNotNone(info)
        self.assertEqual(info.octet_length, 20)
        self.assertEqual(info.data_type, "binary")

    def test_column_info_octet_length_of_binary_schema(self) -> None:
        info = self.db.column_info("m_ast_hash", "hash")
        self.assertIsNotNone(info)
        self.assertEqual(info.octet_length, 16)


class TestErrorMapping(unittest.TestCase):
    def test_translate_errnos(self) -> None:
        cases = [
            (mysql.connector.IntegrityError(msg="Duplicate entry '1'", errno=1062), DuplicateKeyError),
            (mysql.connector.ProgrammingError(msg="Table 'x.y' doesn't exist", errno=1146), MissingTableError),
            (mysql.connector.OperationalError(msg="Got a packet bigger than 'max_allowed_packet'", errno=1153), PacketTooLargeError),
            (mysql.connector.OperationalError(msg="gone away", errno=2006), ConnectionLostError),
            (mysql.connector.OperationalError(msg="lost", errno=2013), ConnectionLostError),
            (mysql.connector.OperationalError(msg="x", errno=2055), ConnectionLostError),
            (mysql.connector.InterfaceError("server gone"), ConnectionLostError),
            (mysql.connector.errors.PoolError("no connection free"), ConnectionLostError),
            (mysql.connector.OperationalError("socket closed"), ConnectionLostError),
        ]
        for exc, want in cases:
            got = translate_mysql_error(exc)
            self.assertIsInstance(got, want, f"{exc!r} -> {got!r}")

    def test_unmapped_stays_none(self) -> None:
        self.assertIsNone(translate_mysql_error(mysql.connector.ProgrammingError(msg="syntax", errno=1064)))
        self.assertIsNone(translate_mysql_error(mysql.connector.OperationalError(msg="denied", errno=1227)))

    def test_neutral_classes_keep_builtin_bases(self) -> None:
        self.assertTrue(issubclass(DuplicateKeyError, ValueError))
        self.assertTrue(issubclass(ConnectionLostError, ConnectionError))
        self.assertTrue(issubclass(PacketTooLargeError, DBError))

    def test_wrapper_chains_original(self) -> None:
        err = mysql.connector.IntegrityError(msg="Duplicate entry", errno=1062)

        @_wrap_driver_errors
        def boom() -> None:
            raise err

        with self.assertRaises(DuplicateKeyError) as ctx:
            boom()
        self.assertIs(ctx.exception.__cause__, err)

    def test_wrapper_generator_and_passthrough(self) -> None:
        @_wrap_driver_errors
        def gen():
            yield 1
            raise mysql.connector.InterfaceError("gone")

        it = gen()
        self.assertEqual(next(it), 1)
        with self.assertRaises(ConnectionLostError):
            next(it)

        @_wrap_driver_errors
        def other() -> None:
            raise mysql.connector.ProgrammingError(msg="syntax", errno=1064)

        with self.assertRaises(mysql.connector.ProgrammingError):
            other()

    def test_maria_public_methods_translate(self) -> None:
        drv = MariaDB.__new__(MariaDB)
        drv.cnx = mock.MagicMock()
        drv._cur = mock.MagicMock()
        drv._in_txn = False
        drv.check_if_connected = lambda: None  # type: ignore[method-assign]
        cursor = mock.MagicMock()
        cursor.execute.side_effect = mysql.connector.IntegrityError(msg="Duplicate entry", errno=1062)
        drv.cnx.cursor.return_value = cursor
        with self.assertRaises(DuplicateKeyError):
            drv._raw_execute("INSERT INTO t VALUES (1)")
        cursor.execute.side_effect = mysql.connector.OperationalError(msg="gone", errno=2013)
        with self.assertRaises(ConnectionLostError):
            drv._raw_query("SELECT 1")


class TestCapabilitiesAndEngines(unittest.TestCase):
    def test_maria_capabilities(self) -> None:
        caps = MariaDB.__new__(MariaDB).capabilities
        for flag in (CAP_PERSISTENT, CAP_SERVER_TUNING, CAP_BACKGROUND_COMMIT):
            self.assertIn(flag, caps)

    def test_mock_capabilities(self) -> None:
        mock = MockDB(use_global=False)
        self.assertNotIn(CAP_PERSISTENT, mock.capabilities)
        self.assertNotIn(CAP_BACKGROUND_COMMIT, mock.capabilities)
        self.assertTrue(MariaDB.__new__(MariaDB).has_capability(CAP_PERSISTENT))

    def test_available_engines_and_sockets(self) -> None:
        self.assertEqual(available_engines(), ("mariadb", "mock", "sqlite"))
        self.assertIn("mysql", available_engines(include_aliases=True))
        self.assertIn("/run/mysqld/mysqld.sock", default_socket_paths("mariadb"))
        self.assertEqual(default_socket_paths("mock"), ())
        for name in available_engines(include_aliases=True):
            self.assertTrue(issubclass(get_db_engine(name), BaseDBEngine))

    def test_server_tuning_note_classification(self) -> None:
        self.assertTrue(tuning_note_is_warning("innodb_buffer_pool_size: capped at 1 instead of 2"))
        self.assertTrue(tuning_note_is_warning("x: kept 5 (err)"))
        self.assertTrue(tuning_note_is_warning("memory guard (10 GB) plus the pool exceed"))
        self.assertFalse(tuning_note_is_warning("innodb_flush_log_at_trx_commit: 1 -> 2"))

    def test_base_and_mock_tuning_are_noops(self) -> None:
        self.assertEqual(MockDB(use_global=False).apply_server_tuning({"a": 1}, tunnel_active=False, mem_max_gb=4.0), [])

    def test_maria_tuning_caps_local_pool_and_warns(self) -> None:
        drv = MariaDB.__new__(MariaDB)
        drv.host = "localhost"
        captured: dict = {}

        def fake_apply(settings: dict) -> list[str]:
            captured.update(settings)
            return ["innodb_buffer_pool_size: 1 -> 2"]

        drv._apply_server_variables = fake_apply  # type: ignore[method-assign]
        drv.server_variable = lambda name: str(captured["innodb_buffer_pool_size"])  # type: ignore[method-assign]
        ram = 8 * 1024**3
        with mock.patch("os.sysconf", side_effect=lambda k: 4096 if k == "SC_PAGE_SIZE" else ram // 4096):
            notes = drv.apply_server_tuning({"innodb_buffer_pool_size": "64G"}, tunnel_active=False, mem_max_gb=20.0)
        self.assertEqual(captured["innodb_buffer_pool_size"], 2 * 1024**3)  # a quarter of RAM
        self.assertTrue(any("memory guard" in n for n in notes))
        # a tunnel (remote server) skips the local RAM logic
        captured.clear()
        drv.apply_server_tuning({"innodb_buffer_pool_size": "64G"}, tunnel_active=True, mem_max_gb=20.0)
        self.assertEqual(captured["innodb_buffer_pool_size"], "64G")


class TestProbeConnection(unittest.TestCase):
    def test_mock_probe(self) -> None:
        res = MockDB.probe_connection({})
        self.assertTrue(res.ok)

    def test_base_probe_unsupported(self) -> None:
        class Dummy(BaseDBEngine):
            def __init__(self) -> None: ...

        self.assertFalse(Dummy.probe_connection({}).ok)

    def test_maria_probe_incomplete_config(self) -> None:
        res = MariaDB.probe_connection({"user": "", "database": ""})
        self.assertFalse(res.ok)
        self.assertIn("incomplete", res.error)

    def test_maria_probe_success_and_tcp_fallback(self) -> None:
        cnx = mock.MagicMock()
        cnx.get_server_info.return_value = "10.11.0-MariaDB"
        calls: list[dict] = []

        def fake_connect(**kw):
            calls.append(kw)
            if "unix_socket" in kw:
                raise mysql.connector.Error("socket refused")
            return cnx

        with tempfile.NamedTemporaryFile() as sock, mock.patch("mysql.connector.connect", side_effect=fake_connect):
            res = MariaDB.probe_connection(
                {"user": "u", "password": "p", "database": "d", "host": "db.example", "port": 3307, "unix_socket": sock.name}
            )
        self.assertTrue(res.ok)
        self.assertEqual(res.version, "10.11.0-MariaDB")
        self.assertEqual(res.target, "db.example:3307")
        self.assertIn("unix_socket", calls[0])
        self.assertEqual(calls[1]["port"], 3307)

    def test_maria_probe_failure_reports_error(self) -> None:
        with mock.patch("mysql.connector.connect", side_effect=mysql.connector.Error("denied")):
            res = MariaDB.probe_connection({"user": "u", "database": "d", "host": "h"})
        self.assertFalse(res.ok)
        self.assertIn("denied", res.error)
        self.assertEqual(res.target, "h:3306")

    def test_socket_off_forces_tcp(self) -> None:
        cnx = mock.MagicMock()
        cnx.get_server_info.return_value = "x"
        with mock.patch("mysql.connector.connect", return_value=cnx) as conn:
            MariaDB.probe_connection({"user": "u", "database": "d", "unix_socket": "off"})
        self.assertNotIn("unix_socket", conn.call_args.kwargs)


class TestReaderLifecycle(unittest.TestCase):
    def test_mock_reader_on_class_and_instance(self) -> None:
        MockDB.reset()
        db = MockDB(use_global=False)
        db.insert_rows(T, ["project_id", "name"], [(1, "a")])
        with db.reader() as r:
            self.assertEqual(r.select_rows(T, ["name"]), [{"name": "a"}])
        MockDB().insert_rows(T, ["project_id", "name"], [(7, "g")])
        with MockDB.reader() as r:
            self.assertEqual(r.select_rows(T, ["project_id"], as_dict=False), [(7,)])
        MockDB.reset()

    def test_mock_reader_is_read_only(self) -> None:
        with MockDB(use_global=False).reader() as r:
            with self.assertRaises(ReadOnlyViolation):
                r.insert_rows(T, ["project_id"], [(1,)])
            with self.assertRaises(ReadOnlyViolation):
                r.delete_rows(T, {"project_id": 1})

    def test_maria_class_lifecycle_calls_pool(self) -> None:
        with mock.patch("db_engine.pool.get_db_pool") as warm, mock.patch("db_engine.pool.close_db_pool") as close:
            MariaDB.warm_up()
            MariaDB.shutdown()
        warm.assert_called_once()
        close.assert_called_once()
        MockDB.warm_up()
        MockDB.shutdown()

    def test_maria_reader_is_pooled_session(self) -> None:
        with mock.patch("db_engine.pool.get_db_reader", return_value="session") as rd:
            self.assertEqual(MariaDB.reader(), "session")
        rd.assert_called_once()

    def test_read_session_refuses_writes(self) -> None:
        from db_engine.pool import ReadSession

        cnx = mock.MagicMock()
        session = ReadSession(cnx, mock.MagicMock())
        for name in ("execute", "insert_rows", "update_rows", "delete_rows", "copy_rows", "drop_tables", "create_view"):
            with self.assertRaises(ReadOnlyViolation, msg=name):
                getattr(session, name)("x")
        with self.assertRaises(ReadOnlyViolation):
            session._driver.delete_rows(T, {"project_id": 1})
        with self.assertRaises(ReadOnlyViolation):
            session._driver.insert_rows("t", ["a"], [(1,)])
        with self.assertRaises(AttributeError):
            session.no_such_thing  # noqa: B018

    def test_read_session_catalog_pass_throughs(self) -> None:
        from db_engine.pool import ReadSession

        session = ReadSession(mock.MagicMock(), mock.MagicMock())
        for name in ("table_stats", "column_exists", "column_info", "index_names", "existing_tables", "resolve_relation_name"):
            self.assertTrue(callable(getattr(session, name)), name)
        for name in ("table_stats", "column_exists", "column_info", "index_names", "existing_tables", "resolve_relation_name"):
            self.assertTrue(callable(getattr(MockDB(use_global=False).reader(), name)), name)


class TestBinaryNormalization(unittest.TestCase):
    def test_coerce_bytes(self) -> None:
        self.assertEqual(_coerce_bytes(bytearray(b"ab")), b"ab")
        self.assertEqual(_coerce_bytes(memoryview(b"ab")), b"ab")
        self.assertEqual(_coerce_bytes("ab"), b"ab")
        self.assertEqual(_coerce_bytes("é"), "é".encode("utf-8"))
        self.assertIsNone(_coerce_bytes(None))
        self.assertEqual(_coerce_bytes(5), 5)

    def test_normalize_row_binary_columns_as_str(self) -> None:
        names = ["commit_hash", "commit_id"]
        self.assertEqual(_normalize_row(("abc", 3), False, frozenset({"commit_hash"}), names), (b"abc", 3))
        self.assertEqual(_normalize_row({"commit_hash": "abc", "commit_id": 3}, True, frozenset({"commit_hash"}), names), {"commit_hash": b"abc", "commit_id": 3})
        # a non-binary str column is left alone
        self.assertEqual(_normalize_row(("abc", 3), False, frozenset(), names), ("abc", 3))

    def test_table_row_normalizer(self) -> None:
        import core.DBLayout as dbl

        table = next(t for t in dbl.TABLES if t.table_name == "m_ast_hash")
        row = MariaDB._normalize_table_row(table, ("abcdefghijklmnop", 7))
        self.assertEqual(row, (b"abcdefghijklmnop", 7))
        row = MariaDB._normalize_table_row(table, (bytearray(b"x" * 16), 7))
        self.assertIsInstance(row[0], bytes)

    def test_binary_output_columns_from_schema(self) -> None:
        got = MariaDB._binary_output_columns("m_ast_hash", (), ["hash", "ast_id"], ["hash", "ast_id"])
        self.assertEqual(got, frozenset({"hash"}))
        got = MariaDB._binary_output_columns("m_ast_hash", (), {"h": "hash", "i": "ast_id"}, ["h", "i"])
        self.assertEqual(got, frozenset({"h"}))


class TestForeignSqlite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "over.sqlite3")
        con = sqlite3.connect(self.path)
        con.executescript(
            "CREATE TABLE over (num INTEGER PRIMARY KEY, tid INTEGER, ds INTEGER, oidbin BLOB);"
            "INSERT INTO over VALUES (1, 10, 100, x'0a0b'), (2, 10, 200, x'0c'), (3, 11, 150, x'ff'), (0, 10, 1, x'00');"
        )
        con.commit()
        con.close()
        self.addCleanup(self.tmp.cleanup)

    def test_spec_reads(self) -> None:
        with open_foreign_sqlite(self.path) as db:
            self.assertEqual(db.select_one("over", ["num", "tid"], {"num": 1}), (1, 10))
            self.assertEqual(db.select_values("over", "num", [{"num": gt(0)}], order_by=["-ds"], limit=2), [2, 3])
            self.assertEqual(db.count_rows("over", {"tid": 10}), 3)
            self.assertTrue(db.row_exists("over", {"num": 3}))
            self.assertFalse(db.row_exists("over", {"num": 99}))
            self.assertEqual(db.select_value("over", Hex("oidbin"), {"num": 1}) if False else db.select_rows("over", [Hex("oidbin")], {"num": 1}), [("0A0B",)])
            rows = db.select_rows("over", {"tid": "tid", "n": Agg("COUNT")}, [{"num": gt(0)}], group_by=["tid"], order_by=["tid"], as_dict=True)
            self.assertEqual(rows, [{"tid": 10, "n": 2}, {"tid": 11, "n": 1}])
            self.assertEqual(list(db.iter_rows("over", ["num"], [{"num": gt(0)}], order_by=["num"], chunk_size=2)), [(1,), (2,), (3,)])
            self.assertTrue(db.table_exists("over"))
            self.assertFalse(db.table_exists("nope"))

    def test_missing_table_and_file(self) -> None:
        with open_foreign_sqlite(self.path) as db:
            with self.assertRaises(MissingTableError):
                db.select_rows("nope", ["a"])
        with self.assertRaises(ConnectionLostError):
            open_foreign_sqlite(os.path.join(self.tmp.name, "absent.sqlite3"))

    def test_read_only_pragma(self) -> None:
        with open_foreign_sqlite(self.path) as db:
            conn = db._connection()
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.Error):
                conn.execute("DELETE FROM over")

    def test_threads_get_own_connection(self) -> None:
        import threading

        with open_foreign_sqlite(self.path) as db:
            out: list = []
            t = threading.Thread(target=lambda: out.append(db.count_rows("over")))
            t.start()
            t.join()
            self.assertEqual(out, [4])
            self.assertEqual(db.count_rows("over"), 4)
            self.assertEqual(len(db._connections), 2)


class TestPhaseBSpecs(unittest.TestCase):
    """Stored-row assignment order, NULL / list conditions, alias validation and actionable errors."""

    def test_mariadb_orders_readers_before_the_column_they_read(self) -> None:
        exprs = {
            "retry_count": Case([(Pred("status", gt(1)), 0)], "retry_count"),
            "status": Case([({"status": 3}, Incoming("status"))], "status"),
        }
        sql, _ = render_insert_with_params("t", ["a", "status", "retry_count"], 1, "update", update_exprs=exprs)
        self.assertLess(sql.index("retry_count = CASE"), sql.index("status = CASE"))
        swapped = dict(reversed(list(exprs.items())))  # written with the reader last
        sql2, _ = render_insert_with_params("t", ["a", "status", "retry_count"], 1, "update", update_exprs=swapped)
        self.assertLess(sql2.index("retry_count = CASE"), sql2.index("status = CASE"))
        sql3, _ = render_insert_with_params("t", ["a", "status", "retry_count"], 1, "update", update_exprs=swapped, dialect="sqlite")
        self.assertLess(sql3.index("status = CASE"), sql3.index("retry_count = CASE"), "sqlite keeps the written order")

    def test_incoming_assignment_follows_its_readers(self) -> None:
        sql, _ = render_insert_with_params(
            "t", ["a", "status"], 1, "update", update_columns=["status"],
            update_exprs={"a": Case([(Pred("status", gt(1)), 7)], Incoming("a"))},
        )
        self.assertLess(sql.index("a = CASE"), sql.index("status = VALUES(status)"))

    def test_existing_queue_upserts_keep_their_assignment_order(self) -> None:
        from core import distributed  # noqa: F401  (the queue intents are implemented in db_engine.base)
        sql, _ = render_insert_with_params(
            "m_parse_task", ["network_id", "ref_name", "status", "commit_id"], 1, "update",
            update_columns=["chain_name"],
            update_exprs={
                "commit_id": Case([(Pred(Incoming("commit_id"), gt(0)), Incoming("commit_id"))], "commit_id"),
                "retry_count": Case([({"status": 4}, 0)], "retry_count"),
                "status": Case([({"status": 4}, Incoming("status"))], Incoming("status")),
            },
        )
        order = [sql.index(f"{c} = ") for c in ("chain_name", "commit_id", "retry_count", "status")]
        self.assertEqual(order, sorted(order))

    def test_update_orders_mariadb_assignments_and_rejects_cycles(self) -> None:
        values = {"retry_count": 0, "status": Case([(Pred("retry_count", ge(5)), 9)], 1)}
        sql, _ = render_update("t", values, {"id": 1})
        self.assertLess(sql.index("status = CASE"), sql.index("retry_count = %s"))
        sql_s, _ = render_update("t", values, {"id": 1}, dialect="sqlite")
        self.assertLess(sql_s.index("retry_count = ?"), sql_s.index("status = CASE"))
        cyc = {"a": add("b", 1), "b": add("a", 1)}
        for dialect in ("mariadb", "sqlite"):
            with self.assertRaises(SpecError) as ctx:
                render_update("t", cyc, None, dialect=dialect)
            self.assertIn("DB_API.md", str(ctx.exception))
        with self.assertRaises(SpecError):
            render_insert_with_params("t", ["a", "b"], 1, "update", update_exprs=cyc)

    def test_none_and_eq_ne_render(self) -> None:
        for dialect, ph in (("mariadb", "%s"), ("sqlite", "?")):
            sql, params, _ = render_select(
                Select("t", ["a"], {"a": None, "b": ne(None), "c": eq(None), "d": eq(5), "e": is_null()}), dialect=dialect
            )
            self.assertIn(f"a IS NULL AND b IS NOT NULL AND c IS NULL AND d = {ph} AND e IS NULL", sql)
            self.assertEqual(params, [5])
            sql, _, _ = render_select(Select("t", ["a"], [Pred("x", None)]), dialect=dialect)
            self.assertIn("x IS NULL", sql)
        for bad in (lt(None), gt(None), le(None), ge(None), between(1, None)):
            with self.assertRaises(SpecError):
                render_select(Select("t", ["a"], {"a": bad}))

    def test_several_conditions_on_one_column_render(self) -> None:
        sql, params, _ = render_select(Select("t", ["a"], {"a": [gt(0), lt(10)]}))
        self.assertIn("a > %s AND a < %s", sql)
        self.assertEqual(params, [0, 10])
        sql, params, _ = render_select(Select("t", ["a"], all_of({"a": gt(0)}, {"a": lt(10)}, any_of({"b": 1}, {"b": 2}))))
        self.assertIn("a > %s AND a < %s AND ((b = %s) OR (b = %s))", sql)
        for bad in ({"a": [gt(0), 5]}, {"a": {"x": 1}}):
            with self.assertRaises(SpecError) as ctx:
                render_select(Select("t", ["a"], bad))
            self.assertIn("DB_API.md 'Declarative Query Specification Primitives'", str(ctx.exception))

    def test_empty_in_list_matches_nothing(self) -> None:
        sql, params, _ = render_select(Select("t", ["a"], {"a": [], "b": 1}))
        self.assertIn("1 = 0 AND b = %s", sql)
        self.assertEqual(params, [1])

    def test_alias_validation(self) -> None:
        from db_engine.spec import finished_ref
        src = ("m_git_ref", "bc")
        render_select(Select(src, ["bc.commit_id"], [finished_ref("bc")]))  # matching alias is fine
        with self.assertRaises(SpecError) as ctx:
            render_select(Select(src, ["bc.commit_id"], [finished_ref("r")]))
        msg = str(ctx.exception)
        self.assertIn("'r.commit_id'", msg)
        self.assertIn("known aliases of this select: bc", msg)
        self.assertIn("DB_API.md", msg)
        joined = Select(("t", "a"), ["a.x", "b.y"], None, [Join(("u", "b"), {"b.id": "a.id"})], order_by=["-b.y"], group_by=["a.x"])
        render_select(joined)
        for bad in (
            Select(("t", "a"), ["z.x"]),
            Select(("t", "a"), ["a.x"], {"z.y": 1}),
            Select(("t", "a"), ["a.x"], None, [Join(("u", "b"), {"b.id": "q.id"})]),
            Select(("t", "a"), ["a.x"], None, order_by=["z.x"]),
            Select(("t", "a"), ["a.x"], None, group_by=["z.x"]),
            Select(("t", "a"), {"v": Concat("a.x", "z.y")}),
            Select(("t", "a"), ["a.x"], [exists_select(Select(("u", "b"), [Const(1)], {"b.id": Col("zz.id")}))]),
        ):
            with self.assertRaises(SpecError):
                render_select(bad)
        # a correlated subquery may use the aliases of the enclosing select
        render_select(Select(("t", "a"), ["a.x"], [exists_select(Select(("u", "b"), [Const(1)], {"b.id": Col("a.id")}))]))

    def test_mock_validates_aliases_too(self) -> None:
        db = MockDB(use_global=False)
        with self.assertRaises(SpecError):
            db.select_rows(("m_project", "p"), ["q.name"])
        self.assertEqual(db.select_rows(("m_project", "p"), ["p.name"]), [])

    def test_errors_name_the_replacement_and_the_doc_section(self) -> None:
        db = MockDB(use_global=False)
        with self.assertRaises(UnsupportedOperation) as u:
            db._raw_query("SELECT 1")
        self.assertIn("select_rows", str(u.exception))
        self.assertIn("DB_API.md '", str(u.exception))
        with db.reader() as session:
            with self.assertRaises(ReadOnlyViolation) as r:
                session.insert_rows("m_project", ["name"], [("x",)])
        self.assertIn("DB_API.md 'Webapp Read Session & Query Primitives'", str(r.exception))
        with self.assertRaises(MissingTableError) as m:
            db.update_rows("m_nope", {"a": 1}, {"b": 1})
        self.assertIn("DB_API.md '", str(m.exception))
        from db_engine.pool import PoolConfigError, _load_config
        import db_engine.pool as pool
        old = pool._CONFIG_PROVIDER
        pool._CONFIG_PROVIDER = None
        try:
            with self.assertRaises(PoolConfigError) as p:
                _load_config()
        finally:
            pool._CONFIG_PROVIDER = old
        self.assertIn("configure_pool", str(p.exception))
        self.assertIn("DB_API.md '", str(p.exception))
        self.assertIn("DB_API.md '", str(translate_mysql_error(mysql.connector.Error(msg="no", errno=1146))))
        with self.assertRaises(UnsupportedOperation) as b:
            BaseDBEngine.insert_rows(db, "t", ["a"], [])
        self.assertIn("DB_API.md 'Spec Write Primitives'", str(b.exception))



class TestEnqueueParseTasksBatch(unittest.TestCase):
    """`enqueue_parse_tasks()` sends one multi-row upsert with the same per-row rules as one call per row."""

    COLS = ["task_id", "network_id", "chain_name", "chain_kind", "seq_order", "ref_name", "commit_id",
            "base_ref_name", "base_commit_id", "status", "worker_id", "lease_expires_at", "retry_count", "error_msg"]
    # existing tasks: FAILED, DONE, LEASED, RECLAIMING, PENDING (with progress that must survive or reset)
    SEED = [
        (1, 1, "c", "stable", 1, "v1", 11, "v0", 10, 4, "w", 5, 3, "boom"),
        (2, 1, "c", "stable", 2, "v2", 12, "v1", 11, 3, "w", 5, 1, "old"),
        (3, 1, "c", "stable", 3, "v3", 13, "v2", 12, 2, "w", 5, 0, ""),
        (4, 1, "c", "stable", 4, "v4", 14, "v3", 13, 5, "w", 5, 0, ""),
        (5, 1, "c", "stable", 5, "v5", 15, "v4", 14, 0, "", 0, 2, "x"),
    ]
    PUBLISH = [
        (1, "c2", "stable", 1, "v1", 0, "v0", 0, 1),     # FAILED -> starts over, ids kept
        (1, "c2", "stable", 2, "v2", 22, "v1", 0, 1),    # DONE published as not done -> reopened, commit_id 22
        (1, "c2", "stable", 3, "v3", 0, "v2", 0, 1),     # LEASED keeps its state
        (1, "c2", "stable", 4, "v4", 0, "v3", 0, 3),     # RECLAIMING published as DONE -> DONE
        (1, "c2", "stable", 5, "v5", 0, "v4", 0, 1),     # PENDING takes the published status
        (1, "c2", "stable", 6, "v6", 16, "v5", 15, 0),   # new task
        (1, "c2", "stable", 6, "v6", 0, "v5", 0, 1),     # same task again: applied after the row above
    ]

    def _engines(self):
        from core.DBLayout import m_parse_task
        from db_engine import SQLiteDB

        def sqlite():
            db = SQLiteDB(":memory:", foreign_keys=False)
            db.create_table([m_parse_task])
            return db

        return {"SQLiteDB": sqlite, "MockDB": lambda: MockDB(use_global=False)}

    def _tasks(self, db):
        return db.select_rows("m_parse_task", self.COLS[1:], order_by=["ref_name"])

    def test_batch_matches_one_call_per_row(self) -> None:
        for name, make in self._engines().items():
            with self.subTest(engine=name):
                batch, single = make(), make()
                for db in (batch, single):
                    db.insert_rows("m_parse_task", self.COLS, self.SEED)
                self.assertEqual(batch.enqueue_parse_tasks(self.PUBLISH), len(self.PUBLISH))
                self.assertEqual(sum(single.enqueue_parse_tasks([row]) for row in self.PUBLISH), len(self.PUBLISH))
                self.assertEqual(self._tasks(batch), self._tasks(single))
                by_ref = {t[4]: t for t in self._tasks(batch)}
                self.assertEqual(by_ref["v1"][5:], (11, "v0", 10, 1, "w", 5, 0, ""))
                self.assertEqual(by_ref["v2"][5], 22)
                self.assertEqual(by_ref["v3"][8], 2)
                self.assertEqual(by_ref["v4"][8], 3)
                self.assertEqual(by_ref["v6"][5:9], (16, "v5", 15, 1))


if __name__ == "__main__":
    unittest.main()
