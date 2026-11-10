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

from webapp.backend.services import git_service as gs
from webapp.backend.services.kconfig_service import KconfigService


class RecordingCursor:
    """Answers queries by substring; records the statements."""

    def __init__(self, answers: list[tuple[str, list]]) -> None:
        self.answers = answers
        self.statements: list[str] = []
        self._rows: list = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append(sql)
        for needle, rows in self.answers:
            if needle in sql:
                self._rows = list(rows)
                return
        self._rows = []

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _ctx(cursor: RecordingCursor):
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = cursor
    return ctx


COMMIT = {"commit_hash": bytes.fromhex("ab" * 20), "author_date": 1300000000, "subject": "fix it",
          "author_name": "Ann", "author_email": "ann@x.org"}


class TestLastChanged(unittest.TestCase):
    def setUp(self) -> None:
        gs._LAST_WRITER_CACHE.clear()
        gs._FINISHED_CACHE.clear()

    def _run(self, writers: list, version_vid: int = 500, finished: bool = True):
        self.setUp()
        cursor = RecordingCursor([
            ("FROM m_file_name fn", writers),
            ("FROM m_commit c", [COMMIT]),
            ("FROM m_ingest_state", [{"ok": 1}] if finished else []),
        ])
        with mock.patch.object(gs, "get_db_cursor", return_value=_ctx(cursor)), \
                mock.patch.object(gs, "get_version_info", return_value=(version_vid, "v3.0")):
            return gs.GitService._db_last_changed("v3.0", "kernel/sched.c", 77), cursor

    def test_one_writer_answers_with_the_git_shape(self) -> None:
        hit, cursor = self._run([{"commit_id": 120}])
        self.assertEqual(hit["commit_hash"], "ab" * 20)
        self.assertEqual(hit["commit_hash_short"], "ab" * 5)
        self.assertEqual(hit["source"], "db")
        self.assertEqual(set(hit) - {"source"}, {"commit_hash", "commit_hash_short", "author_name", "author_email",
                                                 "author_date", "author_date_iso", "subject"})
        self.assertTrue(all(s.strip().upper().startswith("SELECT") for s in cursor.statements))
        # Version head commits are left out (version-diff rows of older databases)
        self.assertIn("NOT EXISTS (SELECT 1 FROM m_ingest_state", cursor.statements[0])

    def test_zero_or_two_writers_ask_git(self) -> None:
        self.assertIsNone(self._run([])[0])
        gs._LAST_WRITER_CACHE.clear()
        self.assertIsNone(self._run([{"commit_id": 1}, {"commit_id": 2}])[0])

    def test_unfinished_or_older_revision_asks_git(self) -> None:
        self.assertIsNone(self._run([{"commit_id": 120}], finished=False)[0])
        self.assertIsNone(self._run([{"commit_id": 900}], version_vid=500)[0])   # writer ingested after it
        self.assertIsNone(self._run([{"commit_id": 120}], version_vid=-1)[0])    # git-only revision

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
        cursor = RecordingCursor([("FROM m_bridge_tag bt", [
            {"blob_id": 3, "line_s": 40, "line_e": 52},
            {"blob_id": 9, "line_s": 7, "line_e": 9},
        ])])
        resolver = "webapp.backend.services.kconfig_service.git_tree_resolver"
        with mock.patch(f"{resolver}.resolve_commit_sha", return_value="c" * 40), \
                mock.patch(f"{resolver}._is_finished_version", return_value=finished), \
                mock.patch(f"{resolver}.blob_paths", return_value={3: ["arch/arm/mm/Kconfig"], 9: ["arch/arm/Kconfig"], 4: ["x.c"]}), \
                mock.patch("webapp.backend.services.kconfig_service.get_db_cursor", return_value=_ctx(cursor)):
            return KconfigService._other_arch_definitions_db("v3.0", "ARM_THUMB"), cursor

    def test_tags_of_a_finished_version(self) -> None:
        defs, cursor = self._run(True)
        self.assertEqual(defs, [
            {"file_path": "arch/arm/Kconfig", "line_s": 7, "line_e": 9},
            {"file_path": "arch/arm/mm/Kconfig", "line_s": 40, "line_e": 52},
        ])
        # Only the revision's Kconfig blobs are searched
        self.assertIn("bt.blob_id IN (%s,%s)", cursor.statements[0])

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

        class Cur(RecordingCursor):
            def __init__(self) -> None:
                super().__init__([
                    ("AND EXISTS", [(10, bytes.fromhex(head), 1), (20, bytes.fromhex("cd" * 20), 2)]),
                    ("WHERE s.status = %s", [(0,)]),
                ])
                self.deletes: list[tuple] = []

            def execute(self, sql: str, params: tuple = ()) -> None:
                if sql.startswith("SELECT cf.fnid"):
                    self.statements.append(sql)
                    self._rows = [(1, "Makefile"), (2, "kernel/sched.c")] if params[0] == 10 else [(3, "mm/slab.c")]
                    return
                if sql.startswith("DELETE"):
                    self.deletes.append(params)
                    return
                super().execute(sql, params)

            def query(self, sql: str, params: tuple = (), dictionary: bool = False) -> list:
                self.execute(sql, params)
                return self.fetchall()

            def commit(self) -> None:
                pass

            def rollback(self) -> None:
                pass

            def close(self) -> None:
                pass

        cur = Cur()
        with mock.patch.object(backfills, "_diff_tree_files", return_value={head: {"Makefile"}}) as diff:
            out = backfills.repair_head_commit_files(cur, "/repo", 1)
        diff.assert_called_once_with("/repo", [head])   # the merge head is not asked: it keeps no rows
        self.assertEqual(out["deleted"], 2)
        self.assertEqual(sorted(cur.deletes), [(10, 2), (20, 3)])

    def test_reuse_step_writes_no_head_commit_rows(self) -> None:
        import inspect
        import main
        self.assertNotIn("m_commit_file.table_id", inspect.getsource(main._reuse_parsed_blobs))


if __name__ == "__main__":
    unittest.main()
