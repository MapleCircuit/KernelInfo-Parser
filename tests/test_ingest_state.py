"""tests/test_ingest_state.py - Recovery of interrupted version runs (core/ingest_state.py)."""
from __future__ import annotations

import re
import unittest

from core import ingest_state as I
from core.DBLayout import TABLES

BY_NAME = {t.table_name: t for t in TABLES}


def _cols(name: str) -> list[str]:
    return [c[0] for c in BY_NAME[name].init_columns]


class MemoryDB:
    """Rows as dicts per table; executes the DELETE forms rollback_plan() generates."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {t.table_name: [] for t in TABLES}

    def add(self, table: str, *values) -> None:
        self.rows[table].append(dict(zip(_cols(table), values)))

    def _match(self, row: dict, where: str, params: tuple) -> bool:
        m = re.fullmatch(r"(\w+) (=|>) %s", where)
        if m:
            v = row[m.group(1)]
            return v == params[0] if m.group(2) == "=" else v > params[0]
        m = re.fullmatch(r"(\w+) IN \(SELECT (\w+) FROM (\w+) WHERE (\w+) IN \(([%s,]+)\)\)", where)
        if m:
            ids = {r[m.group(2)] for r in self.rows[m.group(3)] if r[m.group(4)] in params}
            return row[m.group(1)] in ids
        m = re.fullmatch(r"(\w+) IN \(([%s,]+)\)", where)
        if m:
            return row[m.group(1)] in params
        raise AssertionError(f"unsupported WHERE: {where}")

    def run(self, plan: list[I.Delete]) -> None:
        for d in plan:
            self.rows[d.table] = [r for r in self.rows[d.table] if not self._match(r, d.where, d.params)]

    def dangling(self) -> list[str]:
        out = []
        for t in TABLES:
            for col, parent, pcol in (t.init_foreign or ()):
                keys = {r[pcol] for r in self.rows[parent]}
                out += [f"{t.table_name}.{col}={r[col]}" for r in self.rows[t.table_name] if r[col] not in keys]
        return out


def seeded() -> MemoryDB:
    """Rows before the run (ids <= 10) plus rows an interrupted run wrote (ids > 10, and blob 3's rows)."""
    db = MemoryDB()
    db.add("m_type_descriptor", 1, "C_function")
    db.add("m_project", 1, "Linux", "linux")
    db.add("m_git_network", 1, 1, "linux", b"root")
    db.add("m_maintainer_person", 1, "Linus", "l@k.org")
    db.add("m_commit", 5, 1, b"h5", 1, 0, 1, 0, "base", "")
    db.add("m_commit", 10, 1, b"h10", 1, 0, 1, 0, "head", "")
    db.add("m_commit", 11, 1, b"h11", 1, 0, 1, 0, "added later by the run", "")
    db.add("m_commit_parent", 10, 5, 0)
    db.add("m_commit_parent", 11, 10, 0)
    for fnid, name in ((1, "kernel/sched.c"), (2, "mm/slab.c"), (11, "new/file.c")):
        db.add("m_file_name", fnid, name)
    for blob in (1, 2, 3, 11):  # blob 3: created earlier (intermediate commit) but parsed by the run
        db.add("m_file_blob", blob, bytes([blob]) * 20, 1)
    db.add("m_bridge_checkpoint", 5, 1, 1)
    db.add("m_bridge_checkpoint", 10, 1, 1)
    db.add("m_bridge_checkpoint", 10, 11, 11)
    db.add("m_commit_file", 5, 2, 3, 0, 0, "A")  # the old commit that introduced blob 3 keeps its row
    db.add("m_tag_code", b"c1", "code")
    db.add("m_ast", 1, "schedule", 1)
    db.add("m_ast", 11, "new_fn", 1)
    db.add("m_ast_container", 1, 0, 1, 1)
    db.add("m_ast_container", 11, 0, 1, 1)
    db.add("m_ast_hash", b"a11", 11)
    db.add("m_tag", 1, b"c1", 1, 0, 0)
    db.add("m_tag", 11, b"c1", 11, 0, 0)
    db.add("m_tag_def", 1, 0, 7, "schedule", 1, 0, 0, 5)
    db.add("m_tag_def", 11, 0, 8, "new_fn", 1, 0, 0, 5)
    db.add("m_bridge_tag", 1, 1, 1, 5, 0, 0)
    db.add("m_bridge_tag", 3, 1, 1, 5, 0, 0)    # partial rows of the re-parsed blob (old tag only)
    db.add("m_bridge_tag", 11, 11, 1, 5, 0, 0)
    db.add("m_symbol_own", 3, 1, 2)
    db.add("m_blob_symbol_def", 1, 1, 1, 1, "schedule", 1, 0, 1, 5)
    db.add("m_blob_symbol_def", 12, 3, 1, 1, "schedule", 1, 0, 1, 5)
    db.add("m_ingest_state", 1, 10, I.INGEST_PARSING, 0)
    db.add("m_arch_root", 5, "x86_64", 2, 0, "include/", "")
    db.add("m_arch_root", 10, "x86_64", 2, 0, "include/", "")   # the interrupted version's layout
    db.add("m_arch_root", 11, "x86_64", 2, 0, "include/", "")
    return db


MARKS = {name: 10 for name in (t.table_name for t, _ in I.marked_tables(TABLES))}


class TestRollbackPlan(unittest.TestCase):
    def test_marked_tables_are_the_id_allocating_ones(self) -> None:
        names = {t.table_name for t, _ in I.marked_tables(TABLES)}
        self.assertTrue({"m_file_blob", "m_file_name", "m_ast", "m_tag", "m_commit", "m_blob_symbol_def"} <= names)
        # Static registries, hash/composite keys and foreign-key primary keys are not watermarked
        self.assertFalse(names & {"m_type_descriptor", "m_git_ref", "m_repository", "m_tag_code", "m_bridge_tag",
                                  "m_ast_include", "m_ingest_state", "m_ingest_watermark", "m_ingest_blob"})

    def test_children_are_deleted_before_their_parents(self) -> None:
        plan = I.rollback_plan(TABLES, MARKS, 10, [3])
        last_delete = {d.table: i for i, d in enumerate(plan)}
        for t in TABLES:
            for _, parent, _ in (t.init_foreign or ()):
                if parent != t.table_name and t.table_name in last_delete and parent in MARKS:
                    first_parent = min(i for i, d in enumerate(plan) if d.table == parent and "> %s" in d.where)
                    self.assertLess(
                        min(i for i, d in enumerate(plan) if d.table == t.table_name and "> %s" in d.where)
                        if any(d.table == t.table_name and "> %s" in d.where for d in plan) else -1,
                        first_parent, f"{t.table_name} must be cleared before {parent}",
                    )

    def test_rollback_restores_the_state_before_the_run(self) -> None:
        db = seeded()
        db.run(I.rollback_plan(TABLES, MARKS, 10, [3]))
        self.assertEqual(db.dangling(), [])
        rows = db.rows
        # Everything the run created is gone
        self.assertEqual([r["commit_id"] for r in rows["m_commit"]], [5, 10])
        self.assertEqual({r["fnid"] for r in rows["m_file_name"]}, {1, 2})
        self.assertEqual({r["blob_id"] for r in rows["m_file_blob"]}, {1, 2, 3})
        self.assertEqual({r["tag_id"] for r in rows["m_tag"]}, {1})
        self.assertEqual({r["ast_id"] for r in rows["m_ast"]}, {1})
        self.assertEqual(rows["m_ast_hash"], [])
        # The version's checkpoint and the re-parsed blob's partial rows too
        self.assertEqual([r["commit_id"] for r in rows["m_bridge_checkpoint"]], [5])
        self.assertEqual([r["commit_id"] for r in rows["m_arch_root"]], [5])
        self.assertEqual([r["blob_id"] for r in rows["m_bridge_tag"]], [1])
        self.assertEqual(rows["m_symbol_own"], [])
        self.assertEqual([r["def_id"] for r in rows["m_blob_symbol_def"]], [1])
        # Rows older than the run survive, including the old commit that introduced blob 3
        self.assertEqual(len(rows["m_commit_file"]), 1)
        self.assertEqual([r["tag_id"] for r in rows["m_tag_def"]], [1])
        self.assertEqual(len(rows["m_ast_container"]), 1)
        self.assertEqual(len(rows["m_ingest_state"]), 1)

    def test_rollback_is_idempotent(self) -> None:
        db = seeded()
        plan = I.rollback_plan(TABLES, MARKS, 10, [3])
        db.run(plan)
        snapshot = {k: list(v) for k, v in db.rows.items()}
        db.run(plan)
        self.assertEqual(db.rows, snapshot)

    def test_precheck_flags_older_rows_pointing_at_new_ones(self) -> None:
        queries = I.precheck_queries(TABLES, MARKS)
        self.assertIn("m_tag.ast_id -> m_ast", [q[0] for q in queries])
        sql = dict((q[0], q[1]) for q in queries)["m_tag.ast_id -> m_ast"]
        self.assertEqual(sql, "SELECT tag_id FROM m_tag WHERE tag_id <= %s AND ast_id > %s LIMIT 1")


class FakeCursor:
    def __init__(self, log: list, results: dict) -> None:
        self.log, self.results, self.rowcount, self._last = log, results, 0, None

    def execute(self, sql: str, params=()) -> None:
        self.log.append((sql, tuple(params or ())))
        self._last = next((v for k, v in self.results.items() if sql.startswith(k)), None)
        self.rowcount = 0

    def executemany(self, sql: str, rows) -> None:
        self.log.extend((sql, tuple(r)) for r in rows)

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last or [])

    def close(self) -> None:
        pass


class FakeCnx:
    def __init__(self, results: dict | None = None) -> None:
        self.log: list = []
        self.results = results or {}
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.log, self.results)

    def commit(self) -> None:
        self.commits += 1


class TestIngestStateDatabase(unittest.TestCase):
    def test_begin_records_watermarks_and_parsing_status(self) -> None:
        cnx = FakeCnx({"SELECT COALESCE(MAX(": [(42,)]})
        marks = I.begin(cnx, 1, 10, TABLES)
        self.assertEqual(set(marks.values()), {42})
        inserted = [p for sql, p in cnx.log if sql.startswith("INSERT INTO m_ingest_watermark")]
        self.assertEqual(len(inserted), len(I.marked_tables(TABLES)))
        status = [p for sql, p in cnx.log if sql.startswith("INSERT INTO m_ingest_state")]
        self.assertEqual(status[0][:3], (1, 10, I.INGEST_PARSING))

    def test_done_clears_the_bookkeeping(self) -> None:
        cnx = FakeCnx()
        I.set_status(cnx, 1, 10, I.INGEST_DONE)
        sqls = [sql for sql, _ in cnx.log]
        self.assertTrue(any(s.startswith("DELETE FROM m_ingest_watermark") for s in sqls))
        self.assertTrue(any(s.startswith("DELETE FROM m_ingest_blob") for s in sqls))

    def test_rollback_without_watermarks_reports_it(self) -> None:
        self.assertFalse(I.rollback_version(FakeCnx({"SELECT marked_table_id": []}), 1, 10, TABLES))

    def test_rollback_refuses_when_older_rows_reference_the_run(self) -> None:
        cnx = FakeCnx({"SELECT marked_table_id": [(BY_NAME["m_tag"].table_id, 10), (BY_NAME["m_ast"].table_id, 10)],
                       "SELECT tag_id FROM m_tag": [(4,)]})
        with self.assertRaises(I.RecoveryError):
            I.rollback_version(cnx, 1, 10, TABLES)


if __name__ == "__main__":
    unittest.main()
