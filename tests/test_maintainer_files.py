"""tests/test_maintainer_files.py - Files of each MAINTAINERS section version (m_maintainer_file, D01).

Covers parser/maintainer_ast/maintainer_store.py (section versions of a blob, the incremental mapping),
Step 6.7 (`pipeline.maintainers.processing_maintainer_files`), the migration of the old layout, the backfill guard, and the
webapp readers (`MaintainerService.sections_for_paths`, section detail, in_credits).
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ingest_state as I
from core.DBLayout import TABLES, m_credits_entry, m_maintainer_file, m_maintainer_section
from db_engine import MockDB
from db_engine.spec import INT
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


def maintainer_db() -> MockDB:
    """An isolated MockDB with the tables `maintainer_store` joins (rows added through `place`, `section`, `credit`)."""
    return MockDB(use_global=False)


def place(db: MockDB, blob: int, tag: int, ast: int) -> None:
    """The blob holds a tag naming `ast` (m_bridge_tag + m_tag)."""
    db.insert_rows("m_bridge_tag", ["blob_id", "tag_id", "line_s", "line_e", "char_s", "char_e"], [(blob, tag, 1, 2, 0, 0)],
                   on_conflict="ignore")
    db.insert_rows("m_tag", ["tag_id", "hash", "ast_id", "hl_s", "hl_l"], [(tag, bytes([tag]) * 32, ast, 0, 0)], on_conflict="ignore")


def section(db: MockDB, sec_id: int, name: str, blob: int, ast: int) -> None:
    cols = [c[0] for c in m_maintainer_section.init_columns]
    row = {"sec_id": sec_id, "blob_id": blob, "name": name, "ast_id": ast}
    db.insert_rows("m_maintainer_section", cols, [tuple(row.get(c, "") if c not in ("sec_id", "blob_id", "ast_id") else row[c] for c in cols)])


def credit(db: MockDB, credit_id: int, ast: int, person: int, blob: int) -> None:
    cols = [c[0] for c in m_credits_entry.init_columns]
    row = {"credit_id": credit_id, "blob_id": blob, "person_id": person, "ast_id": ast}
    db.insert_rows("m_credits_entry", cols, [tuple(row.get(c, "") if c not in row else row[c] for c in cols)])


class TestSectionVersions(unittest.TestCase):
    def test_pick_prefers_identical_text_then_prior_rows(self) -> None:
        rows = [
            ("a", 10, 100, True), ("a", 30, 300, False),   # later version of a, text differs
            ("b", 11, 100, False), ("b", 31, 300, False),  # fields-only change: newest row not after the blob
            ("c", 40, 400, False),                         # only a later row exists
        ]
        self.assertEqual(store.pick_versions(rows, 200), {"a": 10, "b": 11, "c": 40})

    def test_blob_section_ids_keys_by_lower_case_name(self) -> None:
        db = maintainer_db()
        place(db, 3, 100, 20)       # MAINTAINERS blob 3 holds the tag of the section named by ast 20
        place(db, 1, 100, 20)       # blob 1 (an older MAINTAINERS) places the same tag: its row has the identical text
        section(db, 5, "Alpha Driver", 1, 20)
        section(db, 9, "ALPHA DRIVER", 7, 20)   # a later version with other text
        self.assertEqual(store.blob_section_ids(db, 3), {"alpha driver": 5})
        self.assertEqual(store.blob_section_ids(db, 0), {})
        self.assertEqual(store.blob_section_ids(db, 99), {}, "a blob without tags holds no section")

    def test_blob_credit_ids_one_per_entry_and_person(self) -> None:
        db = maintainer_db()
        place(db, 2, 100, 70)
        place(db, 1, 100, 70)       # entry 1 was stored with a blob that places the same tag
        place(db, 2, 101, 71)
        place(db, 1, 101, 71)
        credit(db, 1, 70, 4, 1)
        credit(db, 2, 70, 4, 5)     # a later row of the same entry and person: not the identical text
        credit(db, 3, 71, 6, 1)
        self.assertEqual(store.blob_credit_ids(db, 2), [1, 3])

    def test_version_blob_and_sections_with_rows(self) -> None:
        db = maintainer_db()
        db.insert_rows("m_file_name", ["fnid", "fname"], [(1, "MAINTAINERS"), (2, "CREDITS")])
        db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(7, 1, 30), (7, 2, 31), (8, 1, 32)])
        self.assertEqual(store.version_blob(db, 7, "MAINTAINERS", tree="m_bridge_checkpoint"), 30)
        self.assertEqual(store.version_blob(db, 7, "CREDITS", tree="m_bridge_checkpoint"), 31)
        self.assertIsNone(store.version_blob(db, 9, "CREDITS", tree="m_bridge_checkpoint"))
        self.assertIsNone(store.version_blob(db, 0, "CREDITS"))
        db.insert_rows("m_maintainer_file", ["sec_id", "fnid"], [(1, 1), (1, 2), (3, 1)])
        self.assertEqual(store.sections_with_rows(db, [1, 2, 3]), {1, 3})
        self.assertEqual(store.sections_with_rows(db, []), set())


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
    """pipeline.maintainers.processing_maintainer_files with the database answers patched in."""

    def _run(self, scope, base_files=PATHS_V1, files=PATHS_V2, text=V2):
        import main
        import pipeline.db_setup
        import pipeline.maintainers
        mf = mock.MagicMock()
        mf.git_file_list.side_effect = lambda rev: "\n".join(base_files if rev == "v1" else files)
        mf.get_file.return_value = text
        staged = []
        te = mock.MagicMock()
        te.set.side_effect = lambda tid, row: staged.append((tid, row))
        fnids = {p: i + 100 for i, p in enumerate(files)}
        with mock.patch.object(pipeline.maintainers, "MF", mf), mock.patch.object(main.G, "TE", te), \
                mock.patch.object(pipeline.maintainers, "_maintainer_scope", return_value=scope), \
                mock.patch.object(pipeline.maintainers, "get_fid_for_path", return_value=77), \
                mock.patch.object(pipeline.maintainers, "get_fnid_for_path", side_effect=fnids.get), \
                mock.patch.object(main.gp, "VID", 2), mock.patch.object(main.gp, "Old_VID", 1, create=True), \
                mock.patch.object(main.gp, "Version_Name", "v2", create=True), \
                mock.patch.object(main.gp, "Old_Version_Name", "v1", create=True), \
                mock.patch.object(MaintainerParser, "parse", autospec=True, side_effect=MaintainerParser.parse) as parse:
            pipeline.maintainers.processing_maintainer_files("v2")
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
        from db_engine import SQLiteDB
        from core.DBLayout import INT

        db = SQLiteDB(":memory:", foreign_keys=False)
        if has_table:
            db.create_table([m_maintainer_file])
            if has_old_column:
                db.add_column("m_maintainer_file", "maintainers_blob_id", INT())
        return db

    def test_old_layout_is_dropped(self) -> None:
        import main
        import pipeline.db_setup
        import pipeline.maintainers
        db = self._db(True, True)
        self.assertEqual(pipeline.db_setup.drop_outdated_derived_tables(db), ["m_maintainer_file"])
        self.assertFalse(db.table_exists("m_maintainer_file"))

    def test_current_layout_is_kept(self) -> None:
        import main
        import pipeline.db_setup
        import pipeline.maintainers
        for has_table, old in ((True, False), (False, False)):
            db = self._db(has_table, old)
            self.assertEqual(pipeline.db_setup.drop_outdated_derived_tables(db), [])
            self.assertEqual(db.table_exists("m_maintainer_file"), has_table)

    def test_rows_go_with_their_section_and_file_name(self) -> None:
        marks = {t.table_name: 10 for t, _ in I.marked_tables(TABLES)}
        plan = I.rollback_plan(TABLES, marks, 10, [3])
        wheres = {d.condition for d in plan if d.table == "m_maintainer_file"}
        self.assertIn("fnid >", wheres)
        self.assertIn("sec_id >", wheres)
        # Sections of a re-parsed MAINTAINERS blob take their file rows with them
        self.assertTrue(any(w.startswith("sec_id IN m_maintainer_section.sec_id WHERE blob_id IN") for w in wheres))

    def test_backfill_is_deferred_while_an_ingestion_is_interrupted(self) -> None:
        from core import backfills
        db = MockDB(use_global=False)
        db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"], [(1, 5, I.INGEST_PARSING, 0)])
        out = backfills.backfill_maintainer_files(db, mock.MagicMock(), 1, 1)
        self.assertEqual(out["state"], "deferred")
        db.update_rows("m_ingest_state", {"status": I.INGEST_DONE}, {"commit_id": 5})
        self.assertEqual(backfills.backfill_maintainer_files(db, mock.MagicMock(), 1, 1)["state"], "done", "no version to fill")
        self.assertEqual(backfills.backfill_maintainer_files(None, None, 1, 1)["state"], "unavailable")


class TestWebappReaders(unittest.TestCase):
    """MaintainerService database paths over a MockDB read session (caches patched)."""

    def setUp(self) -> None:
        from webapp.backend.services import maintainer_service as ms
        self.ms = ms
        self.sections = MaintainerParser(V1).parse()
        for sid, sec in zip((1, 2, 3), self.sections):
            sec.sec_id = sid
        self.matcher = MaintainerMatcher(self.sections)
        ms._FILE_SUBSYSTEMS_CACHE.clear()
        self.db = MockDB(use_global=False)

    def _service(self, rows: dict[str, set[int]]):
        from tests.db_fixtures import patch_reader
        svc = self.ms.MaintainerService()
        svc.get_maintainer_data = lambda version: (self.sections, self.matcher)
        patches = [
            patch_reader(self.db),
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

    def test_stored_rows_are_read_per_path_and_section_version(self) -> None:
        self.db.insert_rows("m_file_name", ["fnid", "fname"], [(1, "a.c"), (2, "b.c")])
        self.db.insert_rows("m_maintainer_file", ["sec_id", "fnid"], [(1, 1), (2, 1), (3, 2)])
        with self.db.reader() as reader:
            got = self.ms.MaintainerService._stored_sections_for_paths(reader, ["a.c", "b.c", "c.c"], [1, 3])
        self.assertEqual(got, {"a.c": {1}, "b.c": {3}}, "other section versions and unknown paths are left out")

    def test_old_layout_is_not_read(self) -> None:
        with self.db.reader() as reader:
            self.assertTrue(self.ms.MaintainerService._section_file_rows_readable(reader))
        self.db.add_column("m_maintainer_file", "maintainers_blob_id", INT())
        with self.db.reader() as reader:
            self.assertFalse(self.ms.MaintainerService._section_file_rows_readable(reader))
            self.assertEqual(self.ms.MaintainerService._stored_sections_for_paths(reader, ["a"], [1]), {})

    def test_section_files_are_limited_to_the_version_tree(self) -> None:
        svc = self.ms.MaintainerService()
        self.db.insert_rows("m_file_name", ["fnid", "fname"], [(1, "drivers/alpha/a.c"), (2, "drivers/alpha/gone.c")])
        self.db.insert_rows("m_maintainer_file", ["sec_id", "fnid"], [(1, 1), (1, 2), (2, 1)])
        with self.db.reader() as reader, mock.patch(
            "webapp.backend.services.tree_resolver.git_tree_resolver.list_tree", return_value={"drivers/alpha/a.c": "0" * 40}
        ):
            # the count is the real one; the page is cut afterwards
            self.assertEqual(svc._section_files(reader, 1, 5, "v3.0"), ["drivers/alpha/a.c"])
        with self.db.reader() as reader, mock.patch(
            "webapp.backend.services.tree_resolver.git_tree_resolver.list_tree", return_value=None
        ):
            self.db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(5, 2, 9)])
            self.assertEqual(svc._section_files(reader, 1, 5, "v3.0"), ["drivers/alpha/gone.c"], "the version's checkpoint tree decides")

    def test_in_credits_uses_the_version_entries_and_aliases(self) -> None:
        svc = self.ms.MaintainerService()
        self.db.insert_rows(
            "m_credits_entry",
            ["credit_id", "blob_id", "person_id", "web_page", "pgp_key", "description", "snail_mail", "ast_id"],
            [(41, 1, 8, "", "", "", "", 0), (43, 1, 9, "", "", "", "", 0)],
        )
        with self.db.reader() as reader, mock.patch.object(
            self.ms.MaintainerService, "_identity_index",
            staticmethod(lambda cur: ({8: "n:alice", 9: "n:bob"}, {"n:alice": [8], "n:bob": [9]})),
        ):
            self.assertEqual(svc._credited_identities(reader, [41, 42]), {"n:alice"}, "only the version's entries count")
            self.assertEqual(svc._credited_identities(reader, []), set())


if __name__ == "__main__":
    unittest.main()
