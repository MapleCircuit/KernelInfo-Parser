"""tests/test_webapp_insights.py - Release reports (H5, H6, H7; webapp/API.md section 4.10)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services import insights_service as ins
from webapp.backend.services import release_service as rs


class TestInsightsHelpers(unittest.TestCase):
    def test_pull_subjects(self) -> None:
        self.assertEqual(ins.parse_pull("Merge tag 'net-3.1' of git://git.kernel.org/pub/scm/linux/kernel/git/davem/net"),
                         {"ref": "net-3.1", "url": "git://git.kernel.org/pub/scm/linux/kernel/git/davem/net"})
        self.assertEqual(ins.parse_pull("Merge branch 'for_linus' of git://x.org/a.git"), {"ref": "for_linus", "url": "git://x.org/a.git"})
        self.assertEqual(ins.parse_pull("Merge git://git.kernel.org/pub/scm/linux/kernel/git/x/y"), {"ref": "", "url": "git://git.kernel.org/pub/scm/linux/kernel/git/x/y"})
        self.assertEqual(ins.parse_pull("Merge branch 'akpm'"), {"ref": "akpm", "url": ""})
        self.assertEqual(ins.parse_pull("Revert x"), {"ref": "", "url": ""})

    def test_tree_matching(self) -> None:
        self.assertEqual(ins.normalize_url("git://git.kernel.org/pub/scm/linux/kernel/git/davem/net.git/"),
                         ins.normalize_url("https://git.kernel.org/linux/kernel/git/davem/net"))
        trees = {"git.kernel.org/linux/kernel/git/jwessel/kgdb": {"name": "KGDB"}, "owner:git.kernel.org/linux/kernel/git/jwessel": {"name": "KGDB"}}
        self.assertEqual(ins.match_tree(trees, "git://git.kernel.org/pub/scm/linux/kernel/git/jwessel/linux-2.6-kgdb")["name"], "KGDB")
        self.assertIsNone(ins.match_tree(trees, "git://example.org/other"))

    def test_metrics(self) -> None:
        self.assertEqual(ins.bus_factor([10, 1, 1]), 1)
        self.assertEqual(ins.bus_factor([3, 3, 3, 3]), 2)
        self.assertEqual(ins.bus_factor([]), 0)
        row = {"commits": 20, "status": "Orphan", "bus_factor": 1, "review_coverage": 0.05,
               "maintainers": [{"person": "A", "last_active": None}]}
        self.assertEqual(sorted(ins.flags_for(row)), ["no_review", "orphan_active", "silent_maintainers", "single_author"])
        self.assertEqual(ins.flags_for({"commits": 0, "status": "Maintained", "bus_factor": 0, "review_coverage": 0, "maintainers": []}), ["no_activity"])

    def test_employers(self) -> None:
        self.assertEqual(ins.employer("dan.j.williams@intel.com"), "Intel")
        self.assertEqual(ins.employer("x@linux.intel.com"), "Intel")
        self.assertEqual(ins.employer("x@gmail.com"), "(Individual / unknown)")
        self.assertEqual(ins.employer("x@unknown-corp.example"), "(unknown-corp.example)")
        self.assertEqual(ins.employer("nobody"), "(Individual / unknown)")


class _DictCursor:
    """The webapp's dictionary cursor over sqlite3 (`%s` placeholders)."""

    def __init__(self, db) -> None:
        self.c = db.cursor()

    def execute(self, sql: str, params=()) -> None:
        self.c.execute(sql.replace("%s", "?").rstrip().rstrip(";"), tuple(params))

    def fetchall(self):
        cols = [d[0] for d in self.c.description]
        return [dict(zip(cols, r)) for r in self.c.fetchall()]

    def fetchone(self):
        r = self.c.fetchone()
        return None if r is None else dict(zip([d[0] for d in self.c.description], r))


class _NoDisk:
    def get(self, key):
        return None

    def put(self, key, obj) -> bool:
        return False


class TestReleaseStatsSources(unittest.TestCase):
    """Release stats from the parser's tables equal the Git computation, and Git answers whenever the
    tables do not cover the release (webapp/API.md section 4.10)."""

    from tests.test_release_stats import TestIngest as _Repo
    git, commit, rows = _Repo.git, _Repo.commit, _Repo.rows

    def setUp(self) -> None:
        import contextlib
        from unittest import mock
        from parser.git_ast import release_stats as rst
        from webapp.backend.services import maintainer_service
        self._Repo.setUp(self)
        rst.ingest(self.cnx, self.dir, repo_id=1)
        patches = [
            mock.patch.object(rs, "get_repo_dir", lambda: self.dir), mock.patch.object(ins, "get_repo_dir", lambda: self.dir),
            mock.patch.object(rs, "_PASS_DISK", _NoDisk()), mock.patch.object(ins, "_DISK", _NoDisk()),
            mock.patch("webapp.backend.database.pool.get_db_cursor", contextlib.contextmanager(lambda: (yield _DictCursor(self.cnx.db)))),
            mock.patch("webapp.backend.database.helpers.get_active_repo_id", lambda cur: 1),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for cache in (rs._TAGS_CACHE, ins._RANGES, maintainer_service._IDENTITY_CACHE):
            cache.clear()
            self.addCleanup(cache.clear)

    def tearDown(self) -> None:
        self._Repo.tearDown(self)

    def test_database_equals_git(self) -> None:
        for tag in ("v3.1", "v3.1-rc1", "v3.0.1"):
            db, git = ins.db_release_stats(tag), ins.compute_release_stats(tag)
            self.assertEqual((db.pop("source"), git.pop("source")), ("database", "git"))
            if tag != "v3.0.1":  # git counts first-timers by author date, the parser by release order
                for r in (db, git):
                    r.pop("first_time_authors"), r.pop("first_timers")
            self.assertEqual(db, git, tag)
        db = ins.db_release_stats("v3.1")
        self.assertEqual((db["commits"], db["merges"], db["authors"], db["files_changed"]), (4, 1, 3, 3))
        self.assertEqual(sorted(p["person"] for p in db["first_timers"]), ["Cy D", "Neo N"])
        self.assertEqual([(p["person"], p["count"]) for p in db["top"]["signers"]], [("Ann B", 1)])
        self.assertIsNone(ins.db_release_stats("v3.0.1")["first_time_authors"])  # a stable update: not applicable
        self.assertEqual(ins.release_stats("v3.1")["result"]["source"], "database")

    def test_git_fallback(self) -> None:
        self.cnx.db.execute("DELETE FROM m_release_segment WHERE tag = 'v3.1-rc1'")  # one segment of v3.0..v3.1 missing
        self.assertIsNone(ins.db_release_stats("v3.1"))
        self.assertIsNotNone(ins.db_release_stats("v3.0.1"))
        self.git("checkout", "-q", "stable")
        self.commit("Neo N", "neo@n.org", "next backport", "a.c", "1\nD\n")
        self.git("tag", "v3.0.2")
        rs._TAGS_CACHE.clear()
        self.assertIsNone(ins.db_release_stats("v3.0.2"))  # a tag fetched after the parser ran
        self.git("tag", "-f", "v3.0.1", "HEAD")
        rs._TAGS_CACHE.clear()
        self.assertIsNone(ins.db_release_stats("v3.0.1"))  # moved since it was summarized
        state = ins.release_stats("v3.1", wait=60)
        self.assertEqual((state["state"], state["result"]["source"], state["result"]["commits"]), ("done", "git", 4))


if __name__ == "__main__":
    unittest.main()


MAINTAINERS = """Maintainers List
----------------

FOO DRIVER
M:	Ann Dev <ann@x.org>
R:	Rev Iewer <rev@x.org>
S:	Maintained
F:	drivers/foo/

BAR LIB
M:	Bob Bar <bob@x.org>
S:	Orphan
F:	lib/bar.c
F:	lib/old.c

THE REST
M:	Linus <l@x.org>
S:	Buried alive in reporters
F:	*
F:	*/
"""


class TestSubsystemHealthFromDatabase(unittest.TestCase):
    """Subsystem health (D11): the database path equals the `git log` path on a temporary repository with a
    reviewed commit, a rename, a merge, a commit outside every section and a file deleted before the tag."""

    @classmethod
    def setUpClass(cls) -> None:
        import shutil
        import tempfile
        from pathlib import Path
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not installed")
        from tests.test_commit_relations import ENV, Store, git
        cls.dir = Path(tempfile.mkdtemp(prefix="health-"))
        cls.cache = tempfile.TemporaryDirectory()
        g = lambda *a: git(cls.dir, *a)  # noqa: E731
        cls.g = staticmethod(g)

        def commit(author: str, email: str, msg: str, files: dict[str, str | None] | None = None) -> None:
            for path, text in (files or {}).items():
                if text is None:
                    g("rm", "-q", path)
                    continue
                (cls.dir / path).parent.mkdir(parents=True, exist_ok=True)
                (cls.dir / path).write_text(text)
            g("add", "-A")
            g("-c", f"user.name={author}", "-c", f"user.email={email}", "commit", "-q", "--allow-empty",
              "--author", f"{author} <{email}>", "-m", msg)

        g("init", "-q", "-b", "master")
        commit("Ann Dev", "ann@x.org", "base", {"MAINTAINERS": MAINTAINERS, "drivers/foo/a.c": "a\n", "lib/bar.c": "b\n",
                                                "lib/old.c": "o\n", "kernel/x.c": "x\n"})
        g("tag", "v1.0")
        commit("Ann Dev", "ann@x.org", "foo: fix\n\nReviewed-by: Rev Iewer <rev@x.org>\nSigned-off-by: Ann Dev <ann@x.org>",
               {"drivers/foo/a.c": "a2\n"})
        commit("Bob Bar", "bob@x.org", "bar: change\n\nSigned-off-by: Bob Bar <bob@x.org>", {"lib/bar.c": "b2\n"})
        g("tag", "v1.1-rc1")
        g("mv", "lib/old.c", "lib/new.c")
        commit("Carl C", "carl@x.org", "lib: rename old.c")
        g("checkout", "-q", "-b", "side")
        commit("Dan D", "dan@x.org", "foo: side work\n\nAcked-by: Ann Dev <ann@x.org>", {"drivers/foo/b.c": "b\n"})
        g("checkout", "-q", "master")
        g("-c", "user.name=Ann Dev", "-c", "user.email=ann@x.org", "merge", "-q", "--no-ff", "-m", "Merge side", "side")
        g("tag", "v1.1")
        commit("Ann Dev", "ann@x.org", "kernel: x", {"kernel/x.c": "x2\n"})
        commit("Eve E", "eve@x.org", "bar: remove\n\nTested-by: Bob Bar <bob@x.org>", {"lib/bar.c": None})
        g("tag", "v1.2")
        cls.store = Store(cls.dir, with_relations=False)
        cls.build_rows()

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls.dir, ignore_errors=True)
        cls.cache.cleanup()

    @classmethod
    def sections(cls):
        from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
        from parser.maintainer_ast.maintainer_parser import MaintainerParser
        secs = MaintainerParser(MAINTAINERS).parse()
        for i, s in enumerate(secs, start=1):
            s.sec_id = i
        return secs, MaintainerMatcher(secs)

    @classmethod
    def build_rows(cls) -> None:
        """What the parser stores for v1.0 -> v1.1-rc1 -> v1.1 -> v1.2: persons, authors, parents, the commits'
        files (renames as new + old name, merges none), trailers, ingest states, and the v1.2 tree's
        m_maintainer_file rows."""
        import main
        from parser.git_ast import GitCommitParser
        db = cls.store.db
        db.executescript("""
            DROP TABLE m_ingest_state;
            CREATE TABLE m_ingest_state (network_id INT, commit_id INT, status INT, base_commit_id INT);
            DELETE FROM m_maintainer_person;
            CREATE TABLE m_commit_parent (commit_id INT, parent_commit_id INT, parent_idx INT);
            CREATE TABLE m_commit_file (commit_id INT, fnid INT, blob_id INT, old_fnid INT, old_blob_id INT, change_type TEXT);
            CREATE TABLE m_bridge_commit_person (commit_id INT, person_id INT, role_type INT, priority INT);
            CREATE TABLE m_file_name (fnid INTEGER PRIMARY KEY, fname TEXT UNIQUE);
            CREATE TABLE m_maintainer_file (sec_id INT, fnid INT);
        """)
        ids = {bytes(h).hex(): cid for cid, h in db.execute("SELECT commit_id, commit_hash FROM m_commit WHERE commit_id > 0")}
        persons: dict[tuple[str, str], int] = {}

        def person(name: str, email: str) -> int:
            key = main._person_key(name, email)
            if key not in persons:
                persons[key] = len(persons) + 1
                db.execute("INSERT INTO m_maintainer_person VALUES (?, ?, ?)", (persons[key], *key))
            return persons[key]

        def fnid(path: str) -> int:
            db.execute("INSERT OR IGNORE INTO m_file_name (fname) VALUES (?)", (path,))
            return db.execute("SELECT fnid FROM m_file_name WHERE fname = ?", (path,)).fetchone()[0]

        for c in GitCommitParser(repo_dir=str(cls.dir)).parse_version_commits(old_rev=None, new_rev="v1.2", with_files=False):
            cid = ids[c.commit_hash]
            db.execute("UPDATE m_commit SET author_id = ? WHERE commit_id = ?", (person(c.author_name, c.author_email), cid))
            for i, p in enumerate(c.parents or ()):
                db.execute("INSERT INTO m_commit_parent VALUES (?, ?, ?)", (cid, ids[p], i))
            for k in c.contributors:
                db.execute("INSERT INTO m_bridge_commit_person VALUES (?, ?, ?, ?)", (cid, person(k.name, k.email), int(k.role), int(k.priority)))
            if len(c.parents or ()) > 1:
                continue
            out = cls.g("diff-tree", "-r", "-M", "--no-commit-id", "--name-status", "--root", c.commit_hash)
            for line in out.splitlines():
                parts = line.split("\t")
                if parts[0].startswith("R"):
                    db.execute("INSERT INTO m_commit_file VALUES (?, ?, 1, ?, 1, 'R')", (cid, fnid(parts[2]), fnid(parts[1])))
                else:
                    f = fnid(parts[1])
                    db.execute("INSERT INTO m_commit_file VALUES (?, ?, 1, ?, 1, ?)", (cid, f, 0 if parts[0] == "A" else f, parts[0]))
        tag = lambda t: ids[cls.g("rev-parse", f"{t}^{{commit}}").strip()]  # noqa: E731
        chain = [("v1.0", None), ("v1.1-rc1", "v1.0"), ("v1.1", "v1.1-rc1"), ("v1.2", "v1.1")]
        db.executemany("INSERT INTO m_ingest_state VALUES (1, ?, 2, ?)", [(tag(t), tag(b) if b else 0) for t, b in chain])
        secs, matcher = cls.sections()
        for path in cls.g("ls-tree", "-r", "--name-only", "v1.2").split():
            for s in matcher.match_file(path):
                if s.name != "THE REST":
                    db.execute("INSERT INTO m_maintainer_file VALUES (?, ?)", (s.sec_id, fnid(path)))

    def setUp(self) -> None:
        from unittest import mock
        from webapp.backend.services.jobs import JobManager
        self.patches = [
            mock.patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": self.cache.name}),
            mock.patch.object(rs, "get_repo_dir", lambda: self.dir),
            mock.patch.object(rs, "_PASS_DISK", _NoDisk()),
            mock.patch.object(rs, "jobs", JobManager(workers=1)),
            *self.store.patches(),
        ]
        for p in self.patches:
            p.start()
        rs._TAGS_CACHE.clear()
        rs._DB_COVERAGE.clear()
        self.store.statements.clear()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        rs._TAGS_CACHE.clear()

    def test_database_report_equals_the_git_report(self) -> None:
        secs, matcher = self.sections()
        tags = ins._health_window("v1.2", 2)
        self.assertEqual(tags, ["v1.1", "v1.2"])
        git = ins._aggregate_health("v1.2", tags, secs, matcher, ins._release_pass)
        db = ins.health_from_db("v1.2", tags, secs, matcher)
        self.assertIsNotNone(db)
        self.assertEqual(db["source"], "database")
        self.assertEqual(db["sections"], git["sections"])
        rows = {r["name"]: r for r in db["sections"]}
        self.assertEqual(rows["FOO DRIVER"]["commits"], 2)                 # the fix and the side commit; the merge is not counted
        self.assertEqual(rows["BAR LIB"]["commits"], 3)                    # change, rename (old name), deletion
        self.assertGreater(rows["FOO DRIVER"]["review_coverage"], 0)
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in self.store.statements))

    def test_not_covered_falls_back_to_git(self) -> None:
        secs, matcher = self.sections()
        db = self.store.db
        rc1 = db.execute("SELECT commit_id FROM m_ingest_state WHERE base_commit_id = (SELECT commit_id FROM m_ingest_state WHERE base_commit_id = 0)").fetchone()[0]
        db.execute("UPDATE m_ingest_state SET status = 1 WHERE commit_id = ?", (rc1,))   # v1.1-rc1 interrupted
        try:
            self.assertIsNone(ins.health_from_db("v1.2", ["v1.1", "v1.2"], secs, matcher))
        finally:
            db.execute("UPDATE m_ingest_state SET status = 2 WHERE commit_id = ?", (rc1,))
        row = db.execute("SELECT commit_id FROM m_commit WHERE subject = 'kernel: x'").fetchone()
        saved = db.execute("SELECT * FROM m_commit WHERE commit_id = ?", row).fetchone()
        db.execute("DELETE FROM m_commit WHERE commit_id = ?", row)                       # a commit not ingested
        try:
            self.assertIsNone(ins.health_from_db("v1.2", ["v1.1", "v1.2"], secs, matcher))
        finally:
            db.execute("INSERT INTO m_commit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", saved)
        with __import__("unittest").mock.patch.object(ins, "_maintainer_data", return_value=(secs, matcher)), \
                __import__("unittest").mock.patch.object(ins, "health_from_db", return_value=None):
            self.assertEqual(ins.compute_subsystem_health("v1.2", 2)["source"], "git")
