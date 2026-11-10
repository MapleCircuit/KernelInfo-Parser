"""tests/test_webapp_release_index.py - Release index (webapp/API.md section 2.17) and commit relation
index (section 2.16): ordering, previous releases, describe parsing, first containing tag, release
commit pass, and relation parsing / storage on a temporary repository."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import unittest.mock
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services import commit_relations as cr
from webapp.backend.services import release_service as rs
from webapp.backend.services.jobs import JobManager

REAL_TAGS = [
    "v2.6.12-rc2", "v2.6.12", "v2.6.38", "v2.6.39-rc1", "v2.6.39", "v2.6.39.1", "v2.6.39.4",
    "v3.0-rc1", "v3.0-rc7", "v3.0", "v3.0.1", "v3.0.4", "v3.0.5", "v3.1-rc1", "v3.1-rc2", "v3.1-rc10", "v3.1", "v3.1.1",
]


class TestReleaseOrdering(unittest.TestCase):
    def test_parse_tag(self) -> None:
        self.assertEqual(rs.parse_tag("v3.1-rc2")["series"], (3, 1, 0))
        self.assertEqual(rs.parse_tag("v2.6.39.4")["stable"], 4)
        self.assertEqual(rs.parse_tag("v3.0.5")["kind"], "stable")
        self.assertTrue(rs.parse_tag("v3.0")["final"])
        for bad in ("v2.6.11-tree", "foo", "v3.0.1.2", "v2.6", "v3.0.1-rc1"):
            self.assertIsNone(rs.parse_tag(bad), bad)

    def test_sorting(self) -> None:
        import random
        shuffled = REAL_TAGS[:]
        random.Random(4).shuffle(shuffled)
        self.assertEqual(sorted(shuffled, key=rs.release_key), [
            "v2.6.12-rc2", "v2.6.12", "v2.6.38", "v2.6.39-rc1", "v2.6.39", "v2.6.39.1", "v2.6.39.4",
            "v3.0-rc1", "v3.0-rc7", "v3.0", "v3.0.1", "v3.0.4", "v3.0.5", "v3.1-rc1", "v3.1-rc2", "v3.1-rc10", "v3.1", "v3.1.1",
        ])

    def test_previous(self) -> None:
        prev = lambda t: rs.previous_in(t, REAL_TAGS)  # noqa: E731
        self.assertEqual(prev("v3.1"), "v3.0")
        self.assertEqual(prev("v3.1-rc1"), "v3.0")
        self.assertEqual(prev("v3.1-rc2"), "v3.1-rc1")
        self.assertEqual(prev("v3.1-rc10"), "v3.1-rc2")
        self.assertEqual(prev("v3.0.5"), "v3.0.4")
        self.assertEqual(prev("v3.0.1"), "v3.0")
        self.assertEqual(prev("v2.6.39"), "v2.6.38")
        self.assertEqual(prev("v3.0"), "v2.6.39")
        self.assertIsNone(prev("v2.6.12-rc2"))
        self.assertEqual(rs.final_of("v3.1-rc2", set(REAL_TAGS)), "v3.1")
        self.assertIsNone(rs.final_of("v3.2-rc1", set(REAL_TAGS)))

    def test_describe(self) -> None:
        self.assertEqual(rs.parse_describe("v3.1-rc1~12^2~3"), "v3.1-rc1")
        self.assertEqual(rs.parse_describe("tags/v3.0^0"), "v3.0")
        self.assertEqual(rs.parse_describe("v3.0"), "v3.0")
        self.assertIsNone(rs.parse_describe("undefined"))
        self.assertIsNone(rs.parse_describe(""))

    def test_trailers(self) -> None:
        msg = "x\n\nReviewed-by: Ann B <ANN@x.org>\nSigned-off-by: C D <c@d>\nTested-by: E <e@f>\n"
        tr = rs.parse_trailers(msg)
        self.assertEqual(tr["reviewed_by"], [["Ann B", "ann@x.org"]])
        self.assertEqual(tr["signed_off_by"], [["C D", "c@d"]])
        self.assertEqual(tr["tested_by"], [["E", "e@f"]])

    def test_trailers_without_name_or_brackets(self) -> None:
        # The parser's TRAILER_PATTERN accepts these; a cold release pass used to raise AttributeError on them
        msg = "x\n\nReported-by: syzbot+abc@syzkaller.appspotmail.com\nReported-by: <Ann@x.org>\nTested-by: Bo <bo@y>\n"
        tr = rs.parse_trailers(msg)
        self.assertEqual(tr["reported_by"], [["syzbot+abc@syzkaller.appspotmail.com", "syzbot+abc@syzkaller.appspotmail.com"],
                                             ["Ann@x.org", "ann@x.org"]])
        self.assertEqual(tr["tested_by"], [["Bo", "bo@y"]])


class TestRelationParsing(unittest.TestCase):
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def test_patterns(self) -> None:
        msg = (
            "net: fix it\n\nThis reverts commit " + self.SHA + ".\n\n"
            "commit " + self.SHA + " upstream.\n"
            "[ Upstream commit " + self.SHA[::-1] + " ]\n"
            "(cherry picked from commit fedcba9876543210fedcba9876543210fedcba98)\n"
            "Fixes: 1da177e4c3f4 (\"Linux-2.6.12-rc2\")\n"
            "Fixes: abcdef0123456789\n"
            "Cc: stable@vger.kernel.org # 3.0+\n"
            "Cc: <stable@kernel.org>\n"
            "Link: https://lore.kernel.org/r/20110701.ABC@Example.org\n"
            "Link: https://lore.kernel.org/all/%3Cxyz@host%3E/\n"
            "Message-Id: <MID-1@host>\n"
        )
        r = cr.parse_relations(msg)
        self.assertEqual(r["fixes"], [("1da177e4c3f4", "Linux-2.6.12-rc2"), ("abcdef0123456789", "")])
        self.assertEqual(r["reverts"], [self.SHA])
        self.assertEqual(r["upstream"], [self.SHA, self.SHA[::-1], "fedcba9876543210fedcba9876543210fedcba98"])
        self.assertEqual(r["stable_cc"], ["3.0+", ""])
        self.assertEqual(r["links"], ["20110701.abc@example.org", "xyz@host", "mid-1@host"])

    def test_negatives(self) -> None:
        prose = (
            "This Fixes: nothing in prose; fixes 1da177e4c3f4 too.\n"
            "> commit " + self.SHA + " upstream.\n"
            "Cc: someone@vger.kernel.org\n"
            "Link: https://example.org/not/a/message\n"
            "Fixes: 123456\n"  # too short (< 7 hex digits)
        )
        r = cr.parse_relations(prose)
        self.assertEqual((r["fixes"], r["upstream"], r["stable_cc"], r["links"]), ([], [], [], []))

    def test_records(self) -> None:
        chunks = ["\x1ea\x1f1\x1fs\x1fbody", "\n\x1eb\x1f2\x1f", "t\x1fz"]
        self.assertEqual(list(cr.iter_records(chunks)), [["a", "1", "s", "body\n"], ["b", "2", "t", "z"]])


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestIndexesOnRepository(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cache = tempfile.TemporaryDirectory()
        repo = Path(cls.tmp.name)
        cls.env = {**os.environ, "GIT_AUTHOR_NAME": "Ann", "GIT_AUTHOR_EMAIL": "ann@x.org", "GIT_COMMITTER_NAME": "Ann",
                   "GIT_COMMITTER_EMAIL": "ann@x.org", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        cls.repo = repo
        run = cls.run_git
        run("init", "-q", "-b", "master")
        cls.commit("a.c", "1\n", "initial")
        run("tag", "v1.0")
        cls.bug = cls.commit("a.c", "1\n2\n", "add a bug")
        cls.fix = cls.commit("a.c", "1\n2 fixed\n", f"fix the bug\n\nFixes: {cls.bug[:12]} (\"add a bug\")\nCc: stable@vger.kernel.org # 1.0+\nLink: https://lore.kernel.org/r/fix-1@x.org\nReviewed-by: Bob <bob@x.org>\nSigned-off-by: Ann <ann@x.org>")
        run("tag", "v1.1-rc1")
        cls.later = cls.commit("b.c", "x\n", "later work")
        run("tag", "v1.1")
        # stable branch from v1.0 with a backport of the fix
        run("checkout", "-q", "-b", "stable-1.0", "v1.0")
        cls.backport = cls.commit("a.c", "1\n2 fixed\n", f"fix the bug\n\ncommit {cls.fix} upstream.\n")
        run("tag", "v1.0.1")
        run("checkout", "-q", "master")
        cls.revert = cls.commit("b.c", "", f"Revert \"later work\"\n\nThis reverts commit {cls.later}.")

    @classmethod
    def run_git(cls, *args: str) -> str:
        return subprocess.run(["git", "-C", str(cls.repo), *args], check=True, capture_output=True, text=True, env=cls.env).stdout

    @classmethod
    def commit(cls, path: str, content: str, message: str) -> str:
        (cls.repo / path).write_text(content)
        cls.run_git("add", "-A")
        cls.run_git("commit", "-q", "-m", message)
        return cls.run_git("rev-parse", "HEAD").strip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()
        cls.cache.cleanup()

    def setUp(self) -> None:
        self.patches = [
            patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": self.cache.name}),
            patch.object(rs, "get_repo_dir", lambda: self.repo),
            patch.object(cr, "get_repo_dir", lambda: self.repo),
            patch.object(rs, "jobs", JobManager(workers=1)),
            patch.object(cr, "jobs", JobManager(workers=1)),
            # The SQLite index path (the database path is tested in TestDatabaseMembership)
            patch.object(rs, "db_covers_all", lambda: False),
            patch.object(rs, "db_lookup", lambda shas: {}),
        ]
        for p in self.patches:
            p.start()
        rs._TAGS_CACHE.clear()
        rs._CONTAINS_CACHE.clear()
        cr._STALE.clear()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def test_release_index_and_ranges(self) -> None:
        self.assertEqual(rs.releases("mainline"), ["v1.0", "v1.1-rc1", "v1.1"])
        self.assertEqual(rs.releases("stable"), ["v1.0.1"])
        self.assertEqual(rs.previous("v1.1-rc1"), "v1.0")
        prev, sha = rs.range_of("v1.1")
        self.assertEqual(prev, self.run_git("rev-parse", "v1.0^{commit}").strip())
        out = rs.release_index.build()
        self.assertEqual(out["ranges_indexed"], 4)
        self.assertEqual(rs.release_index.build()["ranges_indexed"], 0)  # incremental: nothing new
        self.assertTrue(rs.release_index.is_current())
        info = rs.first_containing(self.fix)
        self.assertEqual((info["first_tag"], info["first_release"], info["released"]), ("v1.1-rc1", "v1.1", True))
        self.assertEqual(rs.first_containing(self.backport)["first_stable"], "v1.0.1")
        unreleased = rs.first_containing(self.revert)
        self.assertFalse(unreleased["released"])
        self.assertIn("master", unreleased["branches"])
        commits = rs.release_commits("v1.1-rc1", wait=10)["result"]
        self.assertEqual([c["subject"] for c in commits], ["fix the bug", "add a bug"])
        self.assertEqual(commits[0]["trailers"]["reviewed_by"], [["Bob", "bob@x.org"]])
        self.assertEqual(commits[0]["files"], ["a.c"])
        count = int(self.run_git("rev-list", "--count", "--no-merges", "v1.0..v1.1"))
        self.assertEqual(len(rs.release_commits("v1.1", wait=10)["result"]), count)

    def test_relation_index(self) -> None:
        out = cr.relation_index.build()
        self.assertFalse(out["incremental"])
        r = cr.relation_index.relations(self.fix)
        self.assertEqual([f["sha"] for f in r["fixes"]], [self.bug])
        self.assertEqual(r["fixes"][0]["subject"], "add a bug")
        self.assertEqual(r["stable_cc"], ["1.0+"])
        self.assertEqual(r["links"], ["fix-1@x.org"])
        self.assertEqual([b["sha"] for b in r["backports"]], [self.backport])
        self.assertEqual([f["sha"] for f in cr.relation_index.relations(self.bug)["fixed_by"]], [self.fix])
        self.assertEqual([u["sha"] for u in cr.relation_index.relations(self.backport)["upstream"]], [self.fix])
        self.assertEqual([x["sha"] for x in cr.relation_index.relations(self.later)["reverted_by"]], [self.revert])
        self.assertEqual(cr.relation_index.commits_for_mid("<FIX-1@x.org>"), [self.fix])
        # Prefix resolution: unique, unknown
        self.assertEqual(cr.relation_index.resolve_prefix(self.bug[:10]), self.bug)
        self.assertIsNone(cr.relation_index.resolve_prefix("deadbeefdeadbeef"))
        # Incremental: a new commit on a new head is read without a full pass
        self.run_git("checkout", "-q", "-b", "topic")
        fix2 = self.commit("c.c", "z\n", f"fix again\n\nFixes: {self.bug[:12]}")
        self.run_git("checkout", "-q", "master")
        self.assertFalse(cr.relation_index.is_current())
        inc = cr.relation_index.build()
        self.assertTrue(inc["incremental"])
        self.assertEqual(inc["commits_scanned"], 1)
        self.assertEqual(sorted(f["sha"] for f in cr.relation_index.relations(self.bug)["fixed_by"]), sorted([self.fix, fix2]))


class _DictCursor:
    """sqlite3 cursor answering with dict rows and the MariaDB placeholders (the webapp's cursor shape)."""

    def __init__(self, db) -> None:
        self.db = db
        self.rows: list = []
        self.statements: list[str] = []

    def execute(self, sql: str, params=()) -> None:
        self.statements.append(sql)
        cur = self.db.execute(sql.replace("%s", "?"), tuple(params))
        cols = [d[0] for d in cur.description or ()]
        self.rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class TestDatabaseMembership(TestIndexesOnRepository):
    """m_commit_release (written by the parser's release statistics) answers like the SQLite index."""

    def setUp(self) -> None:
        super().setUp()
        for p in self.patches[-2:]:
            p.stop()
        self.patches = self.patches[:-2]
        from tests.test_release_stats import _Connection
        from parser.git_ast import release_stats
        self.cnx = _Connection()
        release_stats.ingest(self.cnx, self.repo, repo_id=1, workers=2)
        self.cursor = _DictCursor(self.cnx.db)
        ctx = unittest.mock.MagicMock()
        ctx.__enter__.return_value = self.cursor
        for p in (patch.object(rs, "get_db_cursor", return_value=ctx), patch.object(rs, "get_active_repo_id", return_value=1)):
            p.start()
            self.patches.append(p)
        rs._DB_COVERAGE.clear()

    def all_commits(self) -> list[str]:
        return self.run_git("rev-list", "--all").split()

    def test_database_answers_like_the_sqlite_index(self) -> None:
        self.assertTrue(rs.db_covers_all())
        rs.release_index.build()
        shas = self.all_commits()
        db = rs.release_lookup(shas)
        sqlite = rs.release_index.lookup_many(shas)
        for sha in shas:
            self.assertEqual(db[sha], sqlite.get(sha) or {"mainline": None, "stable": None}, sha)
        self.assertEqual(db[self.revert], {"mainline": None, "stable": None})   # on no release chain
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in self.cursor.statements))

    def test_no_sqlite_build_when_the_database_covers_every_tag(self) -> None:
        with patch.object(rs.release_index, "build") as build:
            self.assertEqual(rs.ensure_release_index()["state"], "done")
            info = rs.first_containing(self.fix)
        build.assert_not_called()
        self.assertEqual((info["first_tag"], info["first_release"]), ("v1.1-rc1", "v1.1"))
        self.assertEqual(rs.first_containing(self.backport)["first_stable"], "v1.0.1")
        from webapp.backend.services import commit_facts
        badges = commit_facts.release_badges([self.fix, self.revert])
        self.assertEqual(badges[self.fix]["first_release"], "v1.1")
        self.assertFalse(badges[self.revert]["released"])

    def test_moved_tag_and_new_tags_fall_back(self) -> None:
        self.run_git("tag", "v1.2-rc1", self.revert)     # fetched since the last parse
        rs._TAGS_CACHE.clear()
        rs._DB_COVERAGE.clear()
        try:
            self.assertFalse(rs.db_covers_all())
            # Not covered: the SQLite index answers once it is current
            state = rs.ensure_release_index(wait=30)
            self.assertEqual(state["state"], "done")
            self.assertEqual(rs.release_lookup([self.revert])[self.revert]["mainline"], "v1.2-rc1")
            # A row whose tag moved is not used
            self.run_git("tag", "-f", "v1.1-rc1", self.bug)
            rs._TAGS_CACHE.clear()
            self.assertNotIn(self.fix, rs.db_lookup([self.fix]))
        finally:
            self.run_git("tag", "-f", "v1.1-rc1", self.fix)
            self.run_git("tag", "-d", "v1.2-rc1")
            rs._TAGS_CACHE.clear()

    def test_segments_of_a_range(self) -> None:
        self.assertEqual(len(rs.walk_segments("v1.0", "v1.1")), 2)               # v1.0..v1.1-rc1, v1.1-rc1..v1.1
        self.assertIsNone(rs.walk_segments("v1.0.1", "v1.1"))                    # not on that chain
        commits = set(rs.segment_commits(rs.walk_segments("v1.0", "v1.1")))
        self.assertEqual(commits, set(self.run_git("rev-list", "v1.1", "^v1.0").split()))

    # The SQLite-path tests of the parent class are not repeated here
    test_release_index_and_ranges = None
    test_relation_index = None


class TestHistoryEndpoints(unittest.TestCase):
    """H1/H3 endpoints over mocked indexes: shapes, 202 while building, validation."""

    def test_released_endpoint(self) -> None:
        from fastapi import HTTPException
        from fastapi.responses import JSONResponse
        from webapp.backend.routers import commits
        released = {"sha": "a" * 40, "indexed": True, "released": True, "first_tag": "v3.1-rc1", "first_release": "v3.1", "first_stable": None}
        with patch.object(commits.release_service, "first_containing", return_value=released):
            body = commits.get_released("v3.0", "a" * 12)
        self.assertEqual((body["commit"], body["first_tag"], body["first_release"]), ("a" * 40, "v3.1-rc1", "v3.1"))
        unreleased = {"sha": "b" * 40, "indexed": True, "released": False, "first_tag": None, "branches": ["master"]}
        with patch.object(commits.release_service, "first_containing", return_value=unreleased):
            resp = commits.get_released("v3.0", "b" * 40)
        self.assertIsInstance(resp, JSONResponse)
        self.assertEqual(resp.headers["cache-control"], "no-store")
        building = {"sha": "c" * 40, "indexed": False, "released": False}
        with patch.object(commits.release_service, "first_containing", return_value=building), \
                patch.object(commits.release_service, "ensure_release_index", return_value={"state": "building", "progress": {"done": 3, "total": 9}}):
            resp = commits.get_released("v3.0", "c" * 40)
        self.assertEqual(resp.status_code, 202)
        for bad in ("xyz", "abc", "a" * 41, "--output"):
            with self.assertRaises(HTTPException):
                commits.get_released("v3.0", bad)
        with self.assertRaises(HTTPException):
            commits.post_released("v3.0", {"hashes": ["a" * 40] * 201})

    def test_relations_endpoint(self) -> None:
        from webapp.backend.routers import commits
        rel = {"state": "done", "sha": "a" * 40, "fixes": [{"sha": None, "prefix": "deadbeef12", "subject": "old bug", "resolved": False}],
               "fixed_by": [{"sha": "b" * 40, "subject": ""}], "reverts": [], "reverted_by": [], "upstream": [], "backports": [],
               "stable_cc": ["3.0+"], "links": []}
        with patch.object(commits.commit_relations, "relations", return_value=rel), \
                patch.object(commits.commit_relations, "status", return_value={"state": "done"}), \
                patch.object(commits, "git_facts", return_value={"b" * 40: {"subject": "fix it", "author": "Ann", "email": "a@x", "date": 5}}), \
                patch.object(commits, "commit_ids", return_value={"b" * 40: 77}), \
                patch.object(commits, "release_badges", return_value={"b" * 40: {"first_tag": "v3.2-rc3", "released": True}}):
            body = commits.get_relations("v3.0", "a" * 40)
        self.assertEqual(body["commit"], "a" * 40)
        self.assertEqual(body["fixes"][0]["subject"], "old bug")  # an unresolved prefix keeps its quoted subject
        self.assertIsNone(body["fixes"][0]["released"])
        fb = body["fixed_by"][0]
        self.assertEqual((fb["subject"], fb["author"], fb["commit_id"], fb["released"]["first_tag"]), ("fix it", "Ann", 77, "v3.2-rc3"))
        with patch.object(commits.commit_relations, "relations", return_value={"state": "building", "progress": {"note": "x"}}):
            resp = commits.get_relations("v3.0", "a" * 40)
        self.assertEqual(resp.status_code, 202)


if __name__ == "__main__":
    unittest.main()
