"""tests/test_webapp_existing_data.py - Webapp answers read from rows the parser already writes (D02).

- last changed of a file from m_commit_file (one ingested writer of the content at the path);
- Kconfig definitions of another ARCH from the Kconfig entry tags;
- Git-fallback commit trailers read like the parser's (iter_trailers).
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.DBLayout import m_commit
from db_engine import MockDB
from tests.db_fixtures import patch_reader
from webapp.backend.services.history_service import KCONFIG_TYPES
from webapp.backend.services import git_service as gs
from webapp.backend.services.kconfig_service import KconfigService


COMMIT = {"commit_hash": bytes.fromhex("ab" * 20), "author_date": 1300000000, "subject": "fix it",
          "author_name": "Ann", "author_email": "ann@x.org"}


class TestLastChanged(unittest.TestCase):
    def setUp(self) -> None:
        gs._LAST_WRITER_CACHE.clear()
        gs._FINISHED_CACHE.clear()

    def _run(self, writers: list, version_vid: int = 500, finished: bool = True, staged: tuple = ()):
        """`writers`: commits writing blob 77 at kernel/sched.c; `staged`: commits that are version heads."""
        self.setUp()
        db = MockDB(use_global=False)
        db.insert_rows("m_file_name", ["fnid", "fname"], [(1, "kernel/sched.c")])
        db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(5, COMMIT["author_name"], COMMIT["author_email"])])
        for w in writers:
            db.insert_rows("m_commit_file", ["commit_id", "fnid", "blob_id"], [(w["commit_id"], 1, 77)])
            db.insert_rows(
                "m_commit",
                ["commit_id", "commit_hash", "author_id", "author_date", "subject"],
                [(w["commit_id"], COMMIT["commit_hash"], 5, COMMIT["author_date"], COMMIT["subject"])],
            )
        states = [(c, 1) for c in staged] + ([(version_vid, 2)] if finished else [])
        if states:
            db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status"], [(1, c, s) for c, s in states])
        with patch_reader(db), mock.patch.object(gs, "get_version_info", return_value=(version_vid, "v3.0")):
            return gs.GitService._db_last_changed("v3.0", "kernel/sched.c", 77)

    def test_one_writer_answers_with_the_git_shape(self) -> None:
        hit = self._run([{"commit_id": 120}])
        self.assertEqual(hit["commit_hash"], "ab" * 20)
        self.assertEqual(hit["commit_hash_short"], "ab" * 5)
        self.assertEqual(hit["source"], "db")
        self.assertEqual(set(hit) - {"source"}, {"commit_hash", "commit_hash_short", "author_name", "author_email",
                                                 "author_date", "author_date_iso", "subject"})

    def test_version_head_commits_are_not_writers(self) -> None:
        # Version head commits are left out (version-diff rows of older databases)
        self.assertIsNone(self._run([{"commit_id": 120}], staged=(120,)))
        self.assertEqual(self._run([{"commit_id": 120}, {"commit_id": 130}], staged=(130,))["commit_hash"], "ab" * 20)

    def test_zero_or_two_writers_ask_git(self) -> None:
        self.assertIsNone(self._run([]))
        gs._LAST_WRITER_CACHE.clear()
        self.assertIsNone(self._run([{"commit_id": 1}, {"commit_id": 2}]))

    def test_unfinished_or_older_revision_asks_git(self) -> None:
        self.assertIsNone(self._run([{"commit_id": 120}], finished=False))
        self.assertIsNone(self._run([{"commit_id": 900}], version_vid=500))   # writer ingested after it
        self.assertIsNone(self._run([{"commit_id": 120}], version_vid=-1))    # git-only revision

    def test_get_file_last_changed_falls_back_to_git(self) -> None:
        svc = gs.GitService()
        with mock.patch.object(gs.GitService, "_db_last_changed", return_value=None), \
                mock.patch.object(gs.GitService, "get_file_history", return_value={"last_changed": {"commit_hash": "c"}}) as hist:
            self.assertEqual(svc.get_file_last_changed("v3.0", "a.c", blob_id=5), {"commit_hash": "c"})
        hist.assert_called_once()
        with mock.patch.object(gs.GitService, "_db_last_changed", return_value={"commit_hash": "d"}), \
                mock.patch.object(gs.GitService, "get_file_history") as hist:
            self.assertEqual(svc.get_file_last_changed("v3.0", "a.c", blob_id=5), {"commit_hash": "d"})
        hist.assert_not_called()


class TestOtherArchDefinitions(unittest.TestCase):
    def _run(self, finished: bool):
        db = MockDB(use_global=False)
        kconfig_type = KCONFIG_TYPES[0]
        db.insert_rows("m_ast", ["ast_id", "name", "type_id"], [(1, "ARM_THUMB", kconfig_type), (2, "ARM_THUMB", 999), (3, "OTHER", kconfig_type)])
        db.insert_rows("m_tag", ["tag_id", "ast_id"], [(10, 1), (11, 1), (12, 2), (13, 3), (14, 1)])
        db.insert_rows(
            "m_bridge_tag",
            ["blob_id", "tag_id", "line_s", "line_e"],
            [(3, 10, 40, 52), (9, 11, 7, 9), (3, 12, 1, 2), (3, 13, 60, 70), (4, 14, 5, 6), (77, 14, 5, 6)],
        )
        resolver = "webapp.backend.services.kconfig_service.git_tree_resolver"
        with mock.patch(f"{resolver}.resolve_commit_sha", return_value="c" * 40), \
                mock.patch(f"{resolver}._is_finished_version", return_value=finished), \
                mock.patch(f"{resolver}.blob_paths", return_value={3: ["arch/arm/mm/Kconfig"], 9: ["arch/arm/Kconfig"], 4: ["x.c"]}), \
                patch_reader(db):
            return KconfigService._other_arch_definitions_db("v3.0", "ARM_THUMB"), db

    def test_tags_of_a_finished_version(self) -> None:
        defs, _db = self._run(True)
        # Only the revision's Kconfig blobs and the Kconfig-typed entries named like the symbol count
        self.assertEqual(defs, [
            {"file_path": "arch/arm/Kconfig", "line_s": 7, "line_e": 9},
            {"file_path": "arch/arm/mm/Kconfig", "line_s": 40, "line_e": 52},
        ])

    def test_other_revisions_use_git_grep(self) -> None:
        self.assertIsNone(self._run(False)[0])
        svc = KconfigService()
        with mock.patch.object(KconfigService, "_other_arch_definitions_db", return_value=None), \
                mock.patch.object(KconfigService, "_git", return_value="v3.0:arch/arm/mm/Kconfig:40:config ARM_THUMB\n") as git:
            self.assertEqual(svc._other_arch_definitions("v3.0", "ARM_THUMB"),
                             [{"file_path": "arch/arm/mm/Kconfig", "line_s": 40, "line_e": 40}])
        git.assert_called_once()


class TestGitFallbackTrailers(unittest.TestCase):
    MESSAGE = (
        "subsystem: fix\n\nBody.\n\n"
        "Reported-by: bot@example.org\n"
        "Reported-by: <robot@example.org>\n"
        "Signed-off-by: Ann Dev <ann@x.org>\n"
        "Reviewed-by: Bob <bob@y.org>\n"
        "Merged-by: Carl <carl@z.org>\n"
        "Signed-off-by: Ann Dev <ann@x.org>\n"
    )

    def test_trailers_match_what_ingestion_stores(self) -> None:
        trailers = gs.message_trailers(self.MESSAGE)
        got = [(t["role_type"], t["name"], t["email"]) for t in trailers]
        self.assertEqual(got, [
            (8, "bot@example.org", "bot@example.org"),
            (8, "robot@example.org", "robot@example.org"),
            (4, "Ann Dev", "ann@x.org"),
            (5, "Bob", "bob@y.org"),
            (10, "Carl", "carl@z.org"),
        ])
        self.assertEqual(trailers[4]["role_name"], "Merged-by")
        # The parser's reading of the same message gives the same (role, name, email) set
        from parser.git_ast.git_commit_parser import iter_trailers
        from parser.git_ast.git_types import CommitRole
        stored = {(int(CommitRole.from_trailer_prefix(p)), n or e, e) for p, n, e in iter_trailers(self.MESSAGE)}
        self.assertEqual(set(got), stored)

    def test_contributors_merge_roles_per_person(self) -> None:
        trailers = gs.message_trailers(self.MESSAGE + "Tested-by: Bob <bob@y.org>\n")
        bob = next(c for c in gs.merge_contributors(trailers) if c["email"] == "bob@y.org")
        self.assertEqual(bob["roles"], ["Reviewed-by", "Tested-by"])


class TestRepairHeadCommitFiles(unittest.TestCase):
    """B10: version-diff rows Step 4.5 wrote on version head commits are removed, real ones kept."""

    def test_rows_of_files_the_head_did_not_touch_are_deleted(self) -> None:
        from core import backfills
        head = "ab" * 20

        from core import ingest_state
        db = MockDB(use_global=False)
        db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"],
                       [(1, 10, ingest_state.INGEST_DONE, 0), (1, 20, ingest_state.INGEST_DONE, 0)])
        commit_cols = [c[0] for c in m_commit.init_columns]

        def commit_row(cid: int, sha: bytes) -> tuple:
            return tuple(cid if c[0] == "commit_id" else sha if c[0] == "commit_hash" else 0 if c[1].is_integer else ""
                         for c in m_commit.init_columns)

        db.insert_rows("m_commit", commit_cols, [commit_row(10, bytes.fromhex(head)), commit_row(20, bytes.fromhex("cd" * 20))])
        db.insert_rows("m_commit_parent", ["commit_id", "parent_commit_id", "parent_idx"], [(10, 1, 0), (20, 1, 0), (20, 2, 1)])
        db.insert_rows("m_file_name", ["fnid", "fname"], [(1, "Makefile"), (2, "kernel/sched.c"), (3, "mm/slab.c")])
        cf = ["commit_id", "fnid", "blob_id", "old_fnid", "old_blob_id", "change_type"]
        db.insert_rows("m_commit_file", cf, [(10, 1, 0, 0, 0, "M"), (10, 2, 0, 0, 0, "M"), (20, 3, 0, 0, 0, "M")])
        with mock.patch.object(backfills, "_diff_tree_files", return_value={head: {"Makefile"}}) as diff:
            out = backfills.repair_head_commit_files(db, "/repo", 1)
        diff.assert_called_once_with("/repo", [head])   # the merge head is not asked: it keeps no rows
        self.assertEqual(out["deleted"], 2)
        self.assertEqual(sorted(db.select_rows("m_commit_file", ["commit_id", "fnid"])), [(10, 1)])

    def test_reuse_step_writes_no_head_commit_rows(self) -> None:
        from contextlib import contextmanager
        import main
        import pipeline.blob_reuse
        from core.DBLayout import m_commit_file

        db = MockDB(use_global=False)
        db.insert_rows("m_bridge_tag", ["blob_id", "tag_id", "line_s", "line_e", "char_s", "char_e"], [(5, 1, 1, 1, 0, 0)])

        @contextmanager
        def fake_db():
            yield db

        te = mock.MagicMock()
        te.get.return_value = (5,)
        te.set.return_value = (11,)
        with mock.patch.object(main.G, "DB", fake_db), mock.patch.object(main.G, "TE", te), \
                mock.patch.object(main.gp, "Change_List", ["M\tdrivers/a.c"], create=True), \
                mock.patch.object(main.gp, "VID", 3, create=True), \
                mock.patch.object(main.gp, "file_blob_ids", {}, create=True):
            remaining = pipeline.blob_reuse._reuse_parsed_blobs({"drivers/a.c": b"\x01" * 20})
        self.assertEqual(remaining, [], "the tagged blob is reused")
        written = {c.args[0] for c in te.set.call_args_list}
        self.assertNotIn(m_commit_file.table_id, written)


if __name__ == "__main__":
    unittest.main()
