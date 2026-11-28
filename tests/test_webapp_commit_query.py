"""tests/test_webapp_commit_query.py - Structured commit queries (H8, webapp/API.md section 4.6)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from webapp.backend.services import commit_query as cq


class TestArgumentBuilder(unittest.TestCase):
    def test_escaping_and_layout(self) -> None:
        self.assertEqual(cq.ere_escape("a.b(c)*[x]"), r"a\.b\(c\)\*\[x\]")
        args = cq.build_log_args(start=["a" * 40, "^" + "b" * 40], paths=["fs/ext4", "!*.h"], author="O'Brien (x)",
                                 roles=[("Reviewed-by", "Jan.Kara")], text="journal", since="2011-01-01",
                                 merges="none", skip=50, limit=50)
        self.assertIn("--author=O'Brien \\(x\\)", args)
        self.assertIn("--grep=^Reviewed-by:.*Jan\\.Kara", args)
        self.assertIn("--grep=journal", args)
        self.assertIn("--all-match", args)
        self.assertIn("--no-merges", args)
        self.assertIn("--skip=50", args)
        self.assertIn("-n51", args)
        sep = args.index("--")
        self.assertEqual(args[sep + 1:], [":(glob)fs/ext4", ":(exclude,glob)**/*.h"])
        self.assertLess(args.index("a" * 40), sep)

    def test_validation(self) -> None:
        for kwargs in ({"author": "x" * 101}, {"author": "a\nb"}, {"since": "2011-1-1"}, {"until": "2011-02-30"},
                       {"roles": ["boss:x"]}, {"paths": ["../etc"]}, {"range_": "--output=x..v1"}, {"range_": "v1"},
                       {"merges": "maybe"}):
            with self.assertRaises(HTTPException, msg=kwargs), patch.object(cq, "_resolve", return_value="a" * 40):
                cq.query_commits("v1.0", **kwargs)
        self.assertEqual(cq.parse_roles(["reviewer:Ann", "acked:"]), [("Reviewed-by", "Ann")])


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestQueryRepository(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        repo = Path(cls.tmp.name)
        cls.repo = repo
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_COMMITTER_NAME": "C",
               "GIT_COMMITTER_EMAIL": "c@x.org"}

        def commit(path, message, author, date):
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(message)
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message, f"--author={author}", f"--date={date}"],
                           check=True, env={**env, "GIT_COMMITTER_DATE": date})

        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "master"], check=True, env=env)
        commit("fs/ext4/a.c", "ext4: one\n\nReviewed-by: Jan Kara <jack@suse.cz>", "Ann <ann@x.org>", "2011-02-01T10:00:00")
        subprocess.run(["git", "-C", str(repo), "tag", "v1.0"], check=True, env=env)
        commit("fs/ext4/b.c", "ext4: two journal\n\nAcked-by: Jan Kara <jack@suse.cz>", "Bob <bob@x.org>", "2011-03-01T10:00:00")
        commit("mm/c.c", "mm: three journal", "Ann <ann@x.org>", "2011-04-01T10:00:00")
        subprocess.run(["git", "-C", str(repo), "tag", "v1.1"], check=True, env=env)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def run_query(self, **kwargs):
        from webapp.backend.services import release_service as rs
        with patch.object(cq, "get_repo_dir", lambda: self.repo), patch.object(rs, "get_repo_dir", lambda: self.repo), \
                patch("webapp.backend.services.commit_facts.commit_ids", return_value={}):
            rs._TAGS_CACHE.clear()
            return [c["subject"] for c in cq.query_commits("v1.1", **kwargs)["commits"]]

    def test_queries(self) -> None:
        self.assertEqual(self.run_query(paths=["fs/ext4"]), ["ext4: two journal", "ext4: one"])
        self.assertEqual(self.run_query(roles=["reviewer:Jan Kara"]), ["ext4: one"])
        self.assertEqual(self.run_query(roles=["acked:jan kara"], text="journal"), ["ext4: two journal"])
        self.assertEqual(self.run_query(author="ann@x.org"), ["mm: three journal", "ext4: one"])
        self.assertEqual(self.run_query(since="2011-02-15", until="2011-03-15"), ["ext4: two journal"])
        self.assertEqual(self.run_query(release="v1.1"), ["mm: three journal", "ext4: two journal"])
        self.assertEqual(self.run_query(range_="v1.0..v1.1", paths=["mm"]), ["mm: three journal"])
        self.assertEqual(self.run_query(text="a.c"), [])  # escaped: "." is not a wildcard

    def test_token_boundary_ranking_and_files_count_fallback(self) -> None:
        from webapp.backend.services.commit_query import commit_text_match_rank
        from webapp.backend.services.git_service import GitService

        subjects = [
            "bpf: (zext32 (zext32 X)) -> (zext32 X) SCEV simplification rule",
            "Remove long-stale ext3 defconfig option",
            "Use CONFIG_EXT4_FS instead of CONFIG_EXT3_FS in all of the defconfigs",
            "bpf: Add missed var_off setting in set_sext32_default_val()",
            "scsi: lpfc: Define types in a union for generic void *context3 ptr",
            "Merge tag 'timers-conversion-next3' into timers/core",
            "ext3: Don't check quota format when there are no quota files",
        ]
        ranked = sorted(subjects, key=lambda s: commit_text_match_rank("ext3", s))
        self.assertEqual(
            ranked[:2],
            [
                "Remove long-stale ext3 defconfig option",
                "ext3: Don't check quota format when there are no quota files",
            ],
        )
        self.assertEqual(ranked[2], "Use CONFIG_EXT4_FS instead of CONFIG_EXT3_FS in all of the defconfigs")
        self.assertTrue(all(any(w in s for w in ("zext32", "sext32", "context3", "next3")) for s in ranked[3:]))
        # Explicit substring search still matches rank 0 for its own token
        self.assertEqual(commit_text_match_rank("zext32", subjects[0]), 0)

        sha = "a" * 40
        items = [{"commit_hash": sha, "files_count": 0}]
        with patch.object(GitService, "run_git", return_value=(f"\x1e{sha}\nkernel/bpf/scev.c\n", "", 0)):
            GitService.fill_missing_files_counts(items)
        self.assertEqual(items[0]["files_count"], 1)


class TestBackportFoldSQL(unittest.TestCase):
    """Text search folds stable backports into the mainline row in SQL (`list_network_commits(collapse_backports=True)`),
    over the ingest-time `m_commit.subject_key` / `backport` columns, at any offset."""

    MAIN, BP1, BP2 = 5, 700, 800  # one change: mainline 5 and two stable backports
    ONLY1, ONLY2 = 900, 950       # a change only stable backports carry (its mainline is not indexed)

    @staticmethod
    def _rows():
        from parser.git_ast.git_commit_parser import commit_fold_columns

        def row(cid, subject, message=""):
            key, bp = commit_fold_columns(subject, message)
            return (cid, 1, cid.to_bytes(20, "big"), 1, cid, 1, cid, subject, message, key, bp)

        rows = [row(i, f"ext2: filler {i}") for i in range(1, 651) if i not in (5, 10, 20)]
        rows += [
            row(5, "ext2: drain in-flight DIO"),
            row(700, "ext2:  Drain in-flight DIO", f"commit {'5' * 40} upstream."),
            row(800, "ext2: drain in-flight DIO", f"[ Upstream commit {'5' * 40} ]"),
            # distinct mainline commits sharing a generic subject: never folded
            row(10, "ext2: fix typo"), row(20, "ext2: fix typo"),
            row(900, "ext2: stable only", "(cherry picked from commit abcdef1)"),
            row(950, "ext2: stable only", "commit abcdef1 upstream."),
        ]
        return rows

    def _db(self, kind):
        from core.DBLayout import m_commit, m_commit_file, m_maintainer_person
        from tests.db_fixtures import make_db

        return make_db(kind, tables=[m_commit, m_maintainer_person, m_commit_file], rows={
            "m_maintainer_person": (["person_id", "name", "email"], [(1, "Ann", "ann@x.org")]),
            "m_commit": ([c for c, _ in m_commit.init_columns], self._rows()),
        })

    def _walk(self, db, limit, collapse=True):
        out, offset = [], 0
        while True:
            page = db.list_network_commits(1, text="ext2", limit=limit + 1, offset=offset, collapse_backports=collapse)
            out.extend(page[:limit])
            if len(page) <= limit:
                return out
            offset += limit

    def test_fold_is_exact_at_every_offset(self) -> None:
        for kind in ("mock", "sqlite"):
            with self.subTest(kind=kind), self._db(kind).reader() as db:
                every = self._walk(db, 50, collapse=False)
                self.assertEqual(len(every), 650 - 3 + 7, "backports=include lists every copy")
                self.assertTrue(all("backport_count" not in r for r in every))
                hidden = {self.BP1, self.BP2, self.ONLY2}
                expected = [r["commit_id"] for r in every if r["commit_id"] not in hidden]
                for limit in (50, 37, 200):
                    folded = self._walk(db, limit)
                    self.assertEqual([r["commit_id"] for r in folded], expected, f"limit {limit}")
                by_id = {r["commit_id"]: r for r in self._walk(db, 50)}
                # The mainline row sits past offset 500 and still carries its backports (newest first)
                self.assertGreater(expected.index(self.MAIN), 500)
                self.assertEqual(by_id[self.MAIN]["backport_count"], 2)
                self.assertEqual(by_id[self.MAIN]["backport_hashes"],
                                 [self.BP2.to_bytes(20, "big").hex(), self.BP1.to_bytes(20, "big").hex()])
                self.assertEqual(by_id[self.ONLY1]["backport_count"], 1, "no mainline: the oldest backport stands in")
                self.assertNotIn("backport_count", by_id[10])
                self.assertNotIn("backport_count", by_id[20])
                self.assertNotIn("subject_key", by_id[10])
                deep = db.list_network_commits(1, text="ext2", limit=5, offset=expected.index(self.MAIN) - 2,
                                               collapse_backports=True)
                self.assertEqual([r["commit_id"] for r in deep], expected[expected.index(self.MAIN) - 2:][:5])

    def test_router_pages_the_folded_listing(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.routers import common as api_common
        from api.routers import history

        calls = []

        def fake(ver, page=1, limit=50, query="", *, offset=None, collapse_backports=False):
            calls.append((offset, limit, collapse_backports))
            return {"version": ver, "commits": [{"commit_hash": f"{i:040x}", "subject": "s"} for i in range(limit)]}

        app = FastAPI()
        app.include_router(history.router)
        with patch.object(api_common._git_service, "get_commits", side_effect=fake):
            body = TestClient(app).get("/commits?q=ext2&limit=20&offset=740").json()
        self.assertEqual(calls, [(740, 21, True)], "the folded page is read at its exact offset")
        self.assertEqual((len(body["commits"]), body["truncated"], body["offset"]), (20, True, 740))


if __name__ == "__main__":
    unittest.main()
