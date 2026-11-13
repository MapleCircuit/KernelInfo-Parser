"""unit_test/test_ddl_render.py - Neutral DBLayout schema renders byte-identical MariaDB DDL.

`unit_test/fixtures/mariadb_ddl_baseline.json` holds the `CREATE TABLE` text the pre-neutral (DDL-fragment)
DBLayout produced for every table; `column_meta_baseline.json` holds the per-column facts consumers derive
from the column types (binary / large / NOT NULL positions, estimated widths, AUTO_INCREMENT presence).
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from core import DBLayout as D
from db_engine.base import estimate_column_bytes
from db_engine.spec import ColumnSpec, ColumnType, parse_column_fragment
from db_engine.sql_render import render_create_table, render_create_table_statements

FIX = Path(__file__).parent / "fixtures"
DDL = json.loads((FIX / "mariadb_ddl_baseline.json").read_text())["ddl"]
META = json.loads((FIX / "column_meta_baseline.json").read_text())


class TestMariaDBDdlBaseline(unittest.TestCase):
    def test_every_table_has_baseline(self) -> None:
        self.assertEqual({t.table_name for t in D.TABLES}, set(DDL))

    def test_rendered_ddl_is_byte_identical(self) -> None:
        for table in D.TABLES:
            with self.subTest(table=table.table_name):
                self.assertEqual(render_create_table(table, "mariadb"), DDL[table.table_name])

    def test_column_metadata_matches_baseline(self) -> None:
        for table in D.TABLES:
            with self.subTest(table=table.table_name):
                meta = META[table.table_name]
                cols = table.init_columns
                self.assertEqual(table.has_auto_increment, meta["has_auto_increment"])
                self.assertEqual([estimate_column_bytes(c) for c in cols], meta["est_bytes"])
                self.assertEqual([i for i, c in enumerate(cols) if c[1].is_binary], meta["binary"])
                self.assertEqual([i for i, c in enumerate(cols) if c[1].is_large], meta["large"])
                self.assertEqual([i for i, c in enumerate(cols) if not c[1].nullable], meta["not_null"])

    def test_columns_are_neutral_specs(self) -> None:
        for table in D.TABLES:
            for name, spec in table.init_columns:
                self.assertIsInstance(spec, ColumnSpec, f"{table.table_name}.{name}")


class TestLegacyFragments(unittest.TestCase):
    def test_fragment_parser(self) -> None:
        spec = parse_column_fragment("INT", "UNSIGNED", "NOT NULL", "DEFAULT 5")
        self.assertEqual((spec.type, spec.unsigned, spec.nullable, spec.default), (ColumnType.INT, True, False, 5))
        self.assertTrue(parse_column_fragment("INT", "NOT NULL", "AUTO_INCREMENT").auto_increment)
        self.assertTrue(parse_column_fragment("VARCHAR(8)", "COLLATE utf8mb4_bin").bin_collation)


class TestSQLiteDdl(unittest.TestCase):
    def test_sqlite_ddl_executes_for_every_table(self) -> None:
        con = sqlite3.connect(":memory:")
        for table in D.TABLES:
            for stmt in render_create_table_statements(table, "sqlite"):
                with self.subTest(table=table.table_name, stmt=stmt[:60]):
                    con.execute(stmt)
        names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")} - {"sqlite_sequence"}
        self.assertEqual(names, {t.table_name for t in D.TABLES})

    def test_auto_increment_is_rowid_primary_key(self) -> None:
        ddl = render_create_table(D.m_file_name, "sqlite")
        self.assertIn("fnid INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT", ddl)
        self.assertNotIn("UNSIGNED", render_create_table(D.m_file_blob, "sqlite"))
        self.assertIn("blob_hash BLOB", render_create_table(D.m_file_blob, "sqlite"))


if __name__ == "__main__":
    unittest.main()
