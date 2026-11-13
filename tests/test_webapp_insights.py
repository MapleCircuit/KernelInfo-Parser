"""tests/test_webapp_insights.py - Release reports (H5, H6, H7; webapp/API.md section 4.10)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import DBLayout as D
from db_engine.spec import gt
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


CF_COLUMNS = ["commit_id", "fnid", "blob_id", "old_fnid", "old_blob_id", "change_type"]


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
        from unittest import mock
        from tests.db_fixtures import patch_reader
        from parser.git_ast import release_stats as rst
        from webapp.backend.services import maintainer_service
        self._Repo.setUp(self)
        rst.ingest(self.db, self.dir, repo_id=1)
        patches = [
            mock.patch.object(rs, "get_repo_dir", lambda: self.dir), mock.patch.object(ins, "get_repo_dir", lambda: self.dir),
            mock.patch.object(rs, "_PASS_DISK", _NoDisk()), mock.patch.object(ins, "_DISK", _NoDisk()),
            patch_reader(self.db),
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
        self.db.delete_rows("m_release_segment", {"tag": "v3.1-rc1"})  # one segment of v3.0..v3.1 missing
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

    def test_prefixed_repository(self) -> None:
        """A repository tagged `man-pages-X.Y` (no kernel-scheme tag) answers release stats from both sources."""
        from parser.git_ast import release_stats as rst
        for old_tag, new_tag in (("v3.0", "man-pages-6.00"), ("v3.1-rc1", "man-pages-6.1-rc1"),
                                 ("v3.1", "man-pages-6.01"), ("v3.0.1", "man-pages-6.00.1")):
            self.git("tag", new_tag, f"{old_tag}^{{commit}}")
            self.git("tag", "-d", old_tag)
        for table in ("m_release_segment", "m_release_person", "m_person_first_release", "m_commit_release"):
            self.db.delete_rows(table, None, all_rows=True)
        rst.ingest(self.db, self.dir, repo_id=1)
        rs._TAGS_CACHE.clear()
        ins._RANGES.clear()
        self.assertEqual(rs.releases("mainline"), ["man-pages-6.00", "man-pages-6.1-rc1", "man-pages-6.01"])
        rng = ins._release_range("man-pages-6.01")
        self.assertEqual(rng, {"range_base": "man-pages-6.00", "kind": "mainline",
                               "tags": {"man-pages-6.1-rc1": "man-pages-6.00", "man-pages-6.01": "man-pages-6.1-rc1"}})
        self.assertEqual(ins._release_range("man-pages-6.00")["tags"], {"man-pages-6.00": None})
        self.assertEqual(ins._health_window("man-pages-6.01", 4), ["man-pages-6.00", "man-pages-6.01"])
        for tag in ("man-pages-6.01", "man-pages-6.00.1"):
            db, git = ins.db_release_stats(tag), ins.compute_release_stats(tag)
            self.assertEqual((db.pop("source"), git.pop("source")), ("database", "git"))
            if tag != "man-pages-6.00.1":
                for r in (db, git):
                    r.pop("first_time_authors"), r.pop("first_timers")
            self.assertEqual(db, git, tag)
        db = ins.db_release_stats("man-pages-6.01")
        self.assertEqual((db["prev"], db["commits"], db["merges"], db["rc_count"]), ("man-pages-6.00", 4, 1, 1))
        self.assertEqual(sorted(p["person"] for p in db["first_timers"]), ["Cy D", "Neo N"])
        self.assertEqual(ins.release_stats("man-pages-6.01")["result"]["source"], "database")
        # Another prefixed family has chains of its own: it joins neither the range nor the health window
        self.git("tag", "for-review-1.0", "master")
        rs._TAGS_CACHE.clear()
        ins._RANGES.clear()
        self.assertEqual(ins._release_range("for-review-1.0"), {"range_base": None, "kind": "mainline",
                                                                "tags": {"for-review-1.0": None}})
        self.assertEqual(ins._release_range("man-pages-6.00")["tags"], {"man-pages-6.00": None})
        self.assertEqual(ins._health_window("man-pages-6.01", 4), ["man-pages-6.00", "man-pages-6.01"])


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
        import pipeline.blob_reuse
        from parser.git_ast import GitCommitParser
        db = cls.store.db
        db.create_table([D.m_commit_parent, D.m_commit_file, D.m_bridge_commit_person, D.m_file_name, D.m_maintainer_file])
        db.delete_rows("m_maintainer_person", None, all_rows=True)
        ids = {bytes(h).hex(): cid for cid, h in db.select_rows("m_commit", ["commit_id", "commit_hash"], {"commit_id": gt(0)})}
        persons: dict[tuple[str, str], int] = {}

        def person(name: str, email: str) -> int:
            key = pipeline.blob_reuse._person_key(name, email)
            if key not in persons:
                persons[key] = len(persons) + 1
                db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(persons[key], *key)])
            return persons[key]

        def fnid(path: str) -> int:
            return db.get_or_insert_id(D.m_file_name, {"fname": path})

        for c in GitCommitParser(repo_dir=str(cls.dir)).parse_version_commits(old_rev=None, new_rev="v1.2", with_files=False):
            cid = ids[c.commit_hash]
            db.update_rows("m_commit", {"author_id": person(c.author_name, c.author_email)}, {"commit_id": cid})
            for i, p in enumerate(c.parents or ()):
                db.insert_rows("m_commit_parent", ["commit_id", "parent_commit_id", "parent_idx"], [(cid, ids[p], i)])
            for k in c.contributors:
                db.insert_rows("m_bridge_commit_person", ["commit_id", "person_id", "role_type", "priority"],
                               [(cid, person(k.name, k.email), int(k.role), int(k.priority))], on_conflict="ignore")
            if len(c.parents or ()) > 1:
                continue
            out = cls.g("diff-tree", "-r", "-M", "--no-commit-id", "--name-status", "--root", c.commit_hash)
            for line in out.splitlines():
                parts = line.split("\t")
                if parts[0].startswith("R"):
                    db.insert_rows("m_commit_file", CF_COLUMNS, [(cid, fnid(parts[2]), 1, fnid(parts[1]), 1, "R")])
                else:
                    f = fnid(parts[1])
                    db.insert_rows("m_commit_file", CF_COLUMNS, [(cid, f, 1, 0 if parts[0] == "A" else f, 1, parts[0])])
        tag = lambda t: ids[cls.g("rev-parse", f"{t}^{{commit}}").strip()]  # noqa: E731
        chain = [("v1.0", None), ("v1.1-rc1", "v1.0"), ("v1.1", "v1.1-rc1"), ("v1.2", "v1.1")]
        db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at", "base_commit_id"],
                       [(1, tag(t), 2, 0, tag(b) if b else 0) for t, b in chain])
        secs, matcher = cls.sections()
        for path in cls.g("ls-tree", "-r", "--name-only", "v1.2").split():
            for s in matcher.match_file(path):
                if s.name != "THE REST":
                    db.insert_rows("m_maintainer_file", ["sec_id", "fnid"], [(s.sec_id, fnid(path))], on_conflict="ignore")

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
        self.before = self.store.counts()

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
        self.assertEqual(self.store.counts(), self.before)   # the database report writes nothing

    def test_not_covered_falls_back_to_git(self) -> None:
        secs, matcher = self.sections()
        db = self.store.db
        root = db.select_value("m_ingest_state", "commit_id", {"base_commit_id": 0})
        rc1 = db.select_value("m_ingest_state", "commit_id", {"base_commit_id": root})
        db.update_rows("m_ingest_state", {"status": 1}, {"commit_id": rc1})   # v1.1-rc1 interrupted
        try:
            self.assertIsNone(ins.health_from_db("v1.2", ["v1.1", "v1.2"], secs, matcher))
        finally:
            db.update_rows("m_ingest_state", {"status": 2}, {"commit_id": rc1})
        commit_cols = [c[0] for c in D.m_commit.init_columns]
        saved = db.select_one("m_commit", commit_cols, {"subject": "kernel: x"})
        db.delete_rows("m_commit", {"commit_id": saved[0]})                       # a commit not ingested
        try:
            self.assertIsNone(ins.health_from_db("v1.2", ["v1.1", "v1.2"], secs, matcher))
        finally:
            db.insert_rows("m_commit", commit_cols, [tuple(saved)])
        with __import__("unittest").mock.patch.object(ins, "_maintainer_data", return_value=(secs, matcher)), \
                __import__("unittest").mock.patch.object(ins, "health_from_db", return_value=None):
            self.assertEqual(ins.compute_subsystem_health("v1.2", 2)["source"], "git")
