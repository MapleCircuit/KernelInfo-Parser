"""tests/test_maintainer_files.py - Files of each MAINTAINERS section version (m_maintainer_file, D01).

Covers parser/maintainer_ast/maintainer_store.py (section versions of a blob, the incremental mapping),
Step 6.7 (`main.processing_maintainer_files`), the migration of the old layout, the backfill guard, and the
webapp readers (`MaintainerService.sections_for_paths`, section detail, in_credits).
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ingest_state as I
from core.DBLayout import TABLES, m_maintainer_file
from parser.maintainer_ast import maintainer_store as store
from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
from parser.maintainer_ast.maintainer_parser import MaintainerParser

V1 = """\
ALPHA DRIVER
M:\tAlice <a@x.org>
S:\tMaintained
F:\tdrivers/alpha/

BETA DRIVER
M:\tBob <b@x.org>
S:\tMaintained
F:\tdrivers/beta/
F:\tinclude/linux/beta.h

THE REST
M:\tLinus <l@x.org>
S:\tBuried alive in reporters
F:\t*
F:\t*/
"""

V2 = V1.replace("F:\tinclude/linux/beta.h", "F:\tinclude/linux/beta*.h")

PATHS_V1 = ["drivers/alpha/a.c", "drivers/beta/b.c", "include/linux/beta.h", "include/linux/betax.h", "Makefile"]
PATHS_V2 = PATHS_V1 + ["drivers/alpha/new.c"]


class FakeCursor:
    """Answers the queries of maintainer_store from canned rows; records every statement."""

    def __init__(self, answers: dict[str, list]) -> None:
        self.answers = answers
        self.statements: list[str] = []
        self._rows: list = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append(sql)
        for needle, rows in self.answers.items():
            if needle in sql:
                self._rows = list(rows(params) if callable(rows) else rows)
                return
        self._rows = []

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def query(self, sql: str, params: tuple = (), dictionary: bool = True) -> list:
        self.execute(sql, params)
        return self._rows

    def query_one(self, sql: str, params: tuple = (), dictionary: bool = True):
        self.execute(sql, params)
        return self.fetchone()

    def close(self) -> None:
        pass


class TestSectionVersions(unittest.TestCase):
    def test_pick_prefers_identical_text_then_prior_rows(self) -> None:
        rows = [
            ("a", 10, 100, True), ("a", 30, 300, False),   # later version of a, text differs
            ("b", 11, 100, False), ("b", 31, 300, False),  # fields-only change: newest row not after the blob
            ("c", 40, 400, False),                         # only a later row exists
        ]
        self.assertEqual(store.pick_versions(rows, 200), {"a": 10, "b": 11, "c": 40})

    def test_blob_section_ids_keys_by_lower_case_name(self) -> None:
        cur = FakeCursor({"m_maintainer_section": [(5, "Alpha Driver", 1, True), (9, "ALPHA DRIVER", 7, False)]})
        self.assertEqual(store.blob_section_ids(cur, 3), {"alpha driver": 5})
        self.assertEqual(store.blob_section_ids(cur, 0), {})

    def test_blob_credit_ids_one_per_entry_and_person(self) -> None:
        cur = FakeCursor({"m_credits_entry": [(1, 70, 4, 1, True), (2, 70, 4, 5, False), (3, 71, 6, 1, True)]})
        self.assertEqual(store.blob_credit_ids(cur, 2), [1, 3])

    def test_queries_are_select_only(self) -> None:
        cur = FakeCursor({})
        store.blob_section_ids(cur, 1)
        store.blob_credit_ids(cur, 1)
        store.version_blob(cur, 1, "MAINTAINERS")
        store.sections_with_rows(cur, [1, 2])
        self.assertTrue(cur.statements)
        self.assertTrue(all(s.strip().upper().startswith("SELECT") for s in cur.statements))


class TestMapFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.v1 = MaintainerParser(V1).parse()
        self.v2 = MaintainerParser(V2).parse()

    def _full(self, sections, ids, paths) -> set[tuple[int, str]]:
        matcher = MaintainerMatcher(sections)
        return {(ids[store.section_key(s.name)], p) for p in paths for s in matcher.match_file(p)}

    def test_first_version_maps_every_file(self) -> None:
        ids = {"alpha driver": 1, "beta driver": 2, "the rest": 3}
        pairs = store.map_files(self.v1, ids, None, PATHS_V1, [])
        self.assertEqual(pairs, self._full(self.v1, ids, PATHS_V1))
        self.assertIn((2, "include/linux/beta.h"), pairs)

    def test_changed_section_over_every_path_others_over_new_paths(self) -> None:
        ids = {"alpha driver": 1, "beta driver": 4, "the rest": 3}  # BETA changed: new sec_id 4
        pairs = store.map_files(self.v2, ids, {4}, PATHS_V2, ["drivers/alpha/new.c"])
        self.assertEqual({p for s, p in pairs if s == 4}, {"drivers/beta/b.c", "include/linux/beta.h", "include/linux/betax.h"})
        self.assertEqual({p for s, p in pairs if s in (1, 3)}, {"drivers/alpha/new.c"})
        # Together with version 1's rows of the unchanged sections, version 2 is complete
        v1_rows = store.map_files(self.v1, {"alpha driver": 1, "beta driver": 2, "the rest": 3}, None, PATHS_V1, [])
        combined = {r for r in v1_rows if r[0] in (1, 3)} | pairs
        self.assertEqual(combined, self._full(self.v2, ids, PATHS_V2))

    def test_nothing_changed_and_no_new_path_maps_nothing(self) -> None:
        ids = {"alpha driver": 1, "beta driver": 2, "the rest": 3}
        self.assertEqual(store.map_files(self.v1, ids, set(), PATHS_V1, []), set())


class TestStep67(unittest.TestCase):
    """main.processing_maintainer_files with the database answers patched in."""

    def _run(self, scope, base_files=PATHS_V1, files=PATHS_V2, text=V2):
        import main
        mf = mock.MagicMock()
        mf.git_file_list.side_effect = lambda rev: "\n".join(base_files if rev == "v1" else files)
        mf.get_file.return_value = text
        staged = []
        te = mock.MagicMock()
        te.set.side_effect = lambda tid, row: staged.append((tid, row))
        fnids = {p: i + 100 for i, p in enumerate(files)}
        with mock.patch.object(main, "MF", mf), mock.patch.object(main.G, "TE", te), \
                mock.patch.object(main, "_maintainer_scope", return_value=scope), \
                mock.patch.object(main, "get_fid_for_path", return_value=77), \
                mock.patch.object(main, "get_fnid_for_path", side_effect=fnids.get), \
                mock.patch.object(main.gp, "VID", 2), mock.patch.object(main.gp, "Old_VID", 1, create=True), \
                mock.patch.object(main.gp, "Version_Name", "v2", create=True), \
                mock.patch.object(main.gp, "Old_Version_Name", "v1", create=True), \
                mock.patch.object(MaintainerParser, "parse", autospec=True, side_effect=MaintainerParser.parse) as parse:
            main.processing_maintainer_files("v2")
        return staged, parse, fnids

    def test_unchanged_sections_and_paths_do_not_parse(self) -> None:
        ids = {"alpha driver": 1, "beta driver": 2, "the rest": 3}
        staged, parse, _ = self._run((ids, {1, 2, 3}, {1, 2, 3}, True), base_files=PATHS_V1, files=PATHS_V1, text=V1)
        self.assertEqual(staged, [])
        parse.assert_not_called()

    def test_changed_section_and_new_path(self) -> None:
        ids = {"alpha driver": 1, "beta driver": 4, "the rest": 3}
        staged, parse, fnids = self._run((ids, {1, 2, 3}, {1, 2, 3}, True))
        parse.assert_called_once()
        rows = {row for tid, row in staged if tid == m_maintainer_file.table_id}
        self.assertIn((4, fnids["include/linux/betax.h"]), rows)
        self.assertIn((1, fnids["drivers/alpha/new.c"]), rows)
        self.assertNotIn((1, fnids["drivers/alpha/a.c"]), rows)   # stored with version 1

    def test_first_version_after_the_migration_maps_every_section(self) -> None:
        ids = {"alpha driver": 1, "beta driver": 2, "the rest": 3}
        staged, _parse, fnids = self._run((ids, {1, 2, 3}, set(), True), base_files=PATHS_V1, files=PATHS_V1, text=V1)
        rows = {row for _tid, row in staged}
        self.assertIn((1, fnids["drivers/alpha/a.c"]), rows)
        self.assertIn((3, fnids["Makefile"]), rows)


class TestMigrationAndRollback(unittest.TestCase):
    def _db(self, has_table: bool, has_old_column: bool):
        cur = FakeCursor({
            "information_schema.TABLES": [(1 if has_table else 0,)],
            "information_schema.COLUMNS": [(1 if has_old_column else 0,)],
        })
        db = mock.MagicMock(spec=[])
        db.cursor = lambda: cur
        db.execute = cur.execute
        db.query_one = lambda sql, params=(), dictionary=False: cur.execute(sql, params) or cur.fetchone()
        return db, cur

    def test_old_layout_is_dropped(self) -> None:
        import main
        db, cur = self._db(True, True)
        self.assertEqual(main.drop_outdated_derived_tables(db), ["m_maintainer_file"])
        self.assertIn("DROP TABLE m_maintainer_file", cur.statements)

    def test_current_layout_is_kept(self) -> None:
        import main
        for has_table, old in ((True, False), (False, False)):
            db, cur = self._db(has_table, old)
            self.assertEqual(main.drop_outdated_derived_tables(db), [])
            self.assertFalse(any(s.startswith("DROP") for s in cur.statements))

    def test_rows_go_with_their_section_and_file_name(self) -> None:
        marks = {t.table_name: 10 for t, _ in I.marked_tables(TABLES)}
        plan = I.rollback_plan(TABLES, marks, 10, [3])
        wheres = {d.where for d in plan if d.table == "m_maintainer_file"}
        self.assertIn("fnid > %s", wheres)
        self.assertIn("sec_id > %s", wheres)
        # Sections of a re-parsed MAINTAINERS blob take their file rows with them
        self.assertTrue(any(w.startswith("sec_id IN (SELECT sec_id FROM m_maintainer_section WHERE blob_id IN") for w in wheres))

    def test_backfill_is_deferred_while_an_ingestion_is_interrupted(self) -> None:
        from core import backfills
        cur = FakeCursor({"m_ingest_state": [(1,)]})
        db = mock.MagicMock()
        db.query_one = lambda sql, params=(), dictionary=False: cur.execute(sql, params) or cur.fetchone()
        out = backfills.backfill_maintainer_files(db, mock.MagicMock(), 1, 1)
        self.assertEqual(out["state"], "deferred")
        self.assertEqual(backfills.backfill_maintainer_files(None, None, 1, 1)["state"], "unavailable")


class TestWebappReaders(unittest.TestCase):
    """MaintainerService database paths, without a database (cursor and caches patched)."""

    def setUp(self) -> None:
        from webapp.backend.services import maintainer_service as ms
        self.ms = ms
        self.sections = MaintainerParser(V1).parse()
        for sid, sec in zip((1, 2, 3), self.sections):
            sec.sec_id = sid
        self.matcher = MaintainerMatcher(self.sections)
        ms._FILE_SUBSYSTEMS_CACHE.clear()
        ms._MAINT_FILE_LAYOUT.clear()

    def _service(self, rows: dict[str, set[int]]):
        svc = self.ms.MaintainerService()
        svc.get_maintainer_data = lambda version: (self.sections, self.matcher)
        cursor = mock.MagicMock()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = cursor
        patches = [
            mock.patch.object(self.ms, "get_db_cursor", return_value=ctx),
            mock.patch.object(self.ms.MaintainerService, "_stored_sections_for_paths",
                              staticmethod(lambda cur, paths, ids: {p: rows[p] for p in paths if p in rows})),
        ]
        return svc, patches

    def test_database_rows_give_the_matcher_order(self) -> None:
        rows = {"include/linux/beta.h": {2, 3}, "drivers/alpha/a.c": {1, 3}}
        svc, patches = self._service(rows)
        with patches[0], patches[1]:
            got = svc.sections_for_paths("v3.0", ["include/linux/beta.h", "drivers/alpha/a.c", "Makefile"])
        for path in ("include/linux/beta.h", "drivers/alpha/a.c", "Makefile"):
            self.assertEqual([s.name for s in got[path]], [s.name for s in self.matcher.match_file(path)])

    def test_missing_rows_fall_back_to_the_matcher(self) -> None:
        svc, patches = self._service({})
        with patches[0], patches[1]:
            got = svc.sections_for_paths("v3.0", ["drivers/beta/b.c"])
        self.assertEqual([s.name for s in got["drivers/beta/b.c"]], ["BETA DRIVER", "THE REST"])

    def test_old_layout_is_not_read(self) -> None:
        cursor = mock.MagicMock()
        cursor.fetchone.return_value = {"n": 1}
        self.assertFalse(self.ms.MaintainerService._section_file_rows_readable(cursor))
        self.assertEqual(self.ms.MaintainerService._stored_sections_for_paths(cursor, ["a"], [1]), {})

    def test_section_files_are_limited_to_the_version_tree(self) -> None:
        svc = self.ms.MaintainerService()
        cursor = mock.MagicMock()
        cursor.fetchall.return_value = [{"fname": "drivers/alpha/a.c"}, {"fname": "drivers/alpha/gone.c"}]
        with mock.patch("webapp.backend.services.tree_resolver.git_tree_resolver.list_tree",
                        return_value={"drivers/alpha/a.c": "0" * 40}):
            self.assertEqual(svc._section_files(cursor, 1, 5, "v3.0"), ["drivers/alpha/a.c"])
        sql = " ".join(c.args[0] for c in cursor.execute.call_args_list)
        self.assertNotIn("LIMIT", sql)  # the count is the real one; the page is cut afterwards

    def test_in_credits_uses_the_version_entries_and_aliases(self) -> None:
        svc = self.ms.MaintainerService()
        cursor = mock.MagicMock()
        cursor.fetchall.return_value = [{"person_id": 8}]
        with mock.patch.object(self.ms.MaintainerService, "_identity_index",
                               staticmethod(lambda cur: ({8: "n:alice", 9: "n:alice"}, {"n:alice": [8, 9]}))):
            self.assertEqual(svc._credited_identities(cursor, [41, 42]), {"n:alice"})
            self.assertEqual(svc._credited_identities(cursor, []), set())
        sql = cursor.execute.call_args_list[0].args[0]
        self.assertIn("credit_id IN (%s,%s)", sql)


if __name__ == "__main__":
    unittest.main()
