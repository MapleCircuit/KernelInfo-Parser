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
        self.assertEqual(ins.parse_pull("Merge tag 'edac_urgent_for_v7.3_rc3'"), {"ref": "edac_urgent_for_v7.3_rc3", "url": ""})
        self.assertEqual(ins.parse_pull("Merge remote-tracking branch 'for-linus'"), {"ref": "for-linus", "url": ""})
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

        from parser.git_ast import release_stats as rst
        from tests.db_fixtures import patch_reader
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

    def test_fast_db_authors_before(self) -> None:
        from unittest import mock
        # v3.0 has author Ann B; prior segments up to v3.0 are in DB
        prior = ins._db_authors_before("v3.0", "")
        self.assertIsNotNone(prior)
        self.assertIn(ins.identity("Ann B", "ann@x.org"), prior)

        # compute_release_stats for v3.1 should use prior authors without calling author_first_seen
        with mock.patch.object(ins, "author_first_seen", side_effect=AssertionError("author_first_seen should not be called")):
            stats = ins.compute_release_stats("v3.1")
        self.assertEqual(stats["commits"], 4)
        first_names = sorted(p["person"] for p in stats["first_timers"])
        self.assertEqual(first_names, ["Cy D", "Neo N"])

    def test_db_merge_window(self) -> None:
        import contextlib
        import json
        with contextlib.suppress(Exception):
            self.db.create_table([D.m_release_pull])
        self.db.delete_rows("m_release_pull", None, all_rows=True)
        git_mw = ins.compute_merge_window("v3.1")
        self.assertEqual(git_mw["source"], "git")
        self.assertEqual(len(git_mw["pulls"]), 1)
        p = git_mw["pulls"][0]

        # Insert matching row into m_release_pull for v3.1's segment
        seg = self.db.select_one("m_release_segment", ["segment_id"], {"tag": "v3.1"})
        self.assertIsNotNone(seg)
        sid = int(seg[0])

        self.db.insert_rows(
            "m_release_pull",
            ["segment_id", "pull_idx", "merge_hash", "p1_hash", "p2_hash", "merge_date", "commits",
             "subject", "source_url", "ref_name", "sender", "sec_id", "sec_name", "dirs_json", "body", "format"],
            [(
                sid, 0, bytes.fromhex(p["merge"]), bytes.fromhex(p["parents"][0]), bytes.fromhex(p["parents"][1]),
                p["date"], p["commits"], p["subject"], p["source_url"] or "", p["ref"] or "", p["sender"] or "",
                p["section"]["sec_id"] if p["section"] and p["section"]["sec_id"] else 0,
                p["section"]["name"] if p["section"] else "",
                json.dumps(p["dirs"]) if p["dirs"] else None,
                p["body"], ins.MW_FORMAT,
            )],
        )

        db_mw = ins.db_merge_window("v3.1")
        self.assertIsNotNone(db_mw)
        self.assertEqual(db_mw["source"], "database")
        self.assertEqual(db_mw["release"], "v3.1")
        self.assertEqual(db_mw["totals"], git_mw["totals"])
        self.assertEqual(len(db_mw["pulls"]), len(git_mw["pulls"]))
        self.assertEqual(db_mw["pulls"][0]["merge"], git_mw["pulls"][0]["merge"])
        self.assertEqual(db_mw["pulls"][0]["commits"], git_mw["pulls"][0]["commits"])

        # Test merge_window returns db_merge_window result
        mw_state = ins.merge_window("v3.1")
        self.assertEqual(mw_state["state"], "done")
        self.assertEqual(mw_state["result"]["source"], "database")

    def test_health_from_db_and_m_release_section_health(self) -> None:
        import contextlib
        import json

        from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
        from parser.maintainer_ast.maintainer_parser import MaintainerParser

        with contextlib.suppress(Exception):
            self.db.create_table([D.m_release_section_health])
        self.db.delete_rows("m_release_section_health", None, all_rows=True)
        raw_maint = (
            "FOO SUBSYSTEM\n"
            "M:\tAnn B <ann@x.org>\n"
            "S:\tMaintained\n"
            "F:\ta.c\n\n"
            "THE REST\n"
            "M:\tLinus <torvalds@l.org>\n"
            "S:\tBuried\n"
            "F:\t*\n"
        )
        sections = MaintainerParser(raw_maint).parse()
        for i, s in enumerate(sections, start=1):
            s.sec_id = i
        matcher = MaintainerMatcher(sections)

        foo_sec = sections[0]
        pat_hash = ins._section_pattern_hash(foo_sec)
        manifest_hash = ins._sections_manifest_hash(sections, "THE REST")

        seg = self.db.select_one("m_release_segment", ["segment_id"], {"tag": "v3.1"})
        self.assertIsNotNone(seg)
        sid = int(seg[0])

        authors_dict = {ins.identity("Ann B", "ann@x.org"): 3}
        activity_dict = {ins.identity("Ann B", "ann@x.org"): 12345678}
        self.db.insert_rows(
            "m_release_section_health",
            ["segment_id", "sec_name", "sec_id", "status", "commits", "reviewed_commits", "authors_json", "activity_json", "pat_hash", "format"],
            [
                (sid, foo_sec.name, foo_sec.sec_id, foo_sec.status, 3, 2, json.dumps(authors_dict), json.dumps(activity_dict), pat_hash, ins.HEALTH_FORMAT),
                (sid, "", 0, "", 0, 0, None, None, manifest_hash, ins.HEALTH_FORMAT),
            ],
        )

        res = ins.health_from_db("v3.1", ["v3.1"], sections, matcher)
        self.assertIsNotNone(res)
        self.assertEqual(res["source"], "database")
        self.assertEqual(res["release"], "v3.1")
        self.assertEqual(len(res["sections"]), 1)
        sec_out = res["sections"][0]
        self.assertEqual(sec_out["name"], "FOO SUBSYSTEM")
        self.assertEqual(sec_out["commits"], 3)
        self.assertEqual(sec_out["review_coverage"], round(2 / 3, 4))
        self.assertEqual(sec_out["top_authors"][0]["identity"], ins.identity("Ann B", "ann@x.org"))


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
        from tests.test_commit_relations import Store, git
        cls.dir = Path(tempfile.mkdtemp(prefix="health-"))
        cls.cache = tempfile.TemporaryDirectory()
        g = lambda *a: git(cls.dir, *a)
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
        tag = lambda t: ids[cls.g("rev-parse", f"{t}^{{commit}}").strip()]
        chain = [("v1.0", None), ("v1.1-rc1", "v1.0"), ("v1.1", "v1.1-rc1"), ("v1.2", "v1.1")]
        db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at", "base_commit_id"],
                       [(1, tag(t), 2, 0, tag(b) if b else 0) for t, b in chain])
        _secs, matcher = cls.sections()
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

    def test_subsystem_health_serves_synchronously_from_db(self) -> None:
        from unittest import mock
        secs, matcher = self.sections()
        with mock.patch.object(ins, "_maintainer_data", return_value=(secs, matcher)), \
                mock.patch.object(ins, "_job", side_effect=AssertionError("_job should not run")):
            res = ins.subsystem_health("v1.2", window=2, wait=0.0)
        self.assertEqual(res["state"], "done")
        self.assertEqual(res["result"]["source"], "database")

        with mock.patch.object(ins, "_maintainer_data", return_value=(secs, matcher)), \
                mock.patch.object(ins, "health_from_db", return_value=None), \
                mock.patch.object(ins, "_job", return_value={"state": "building", "progress": {}}) as m_job:
            res_fb = ins.subsystem_health("v1.2", window=2, wait=0.0)
        self.assertEqual(res_fb["state"], "building")
        m_job.assert_called_once()


class TestMergeWindowSectionResolution(unittest.TestCase):
    """Merge-window subsystem resolution via T: tree, changed files/directories, and maintainer."""

    def setUp(self) -> None:
        from db_engine import MockDB
        from tests.db_fixtures import patch_reader
        p = patch_reader(MockDB(use_global=False))
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _build_resolver():
        from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
        from parser.maintainer_ast.maintainer_parser import MaintainerParser

        raw = """Maintainers List
----------------

DRM DRIVERS
M:\tDave Airlie <airlied@linux.ie>
S:\tMaintained
T:\tgit git://anongit.freedesktop.org/drm/drm
F:\tdrivers/gpu/drm/

DRM DRIVERS FOR AMD RADEON AND AMDGPU
M:\tAlex Deucher <alexander.deucher@amd.com>
S:\tMaintained
F:\tdrivers/gpu/drm/amd/

EDAC
M:\tBorislav Petkov (AMD) <bp@alien8.de>
S:\tMaintained
T:\tgit git://git.kernel.org/pub/scm/linux/kernel/git/ras/ras.git
F:\tdrivers/edac/

RAS INFRASTRUCTURE
M:\tBorislav Petkov (AMD) <bp@alien8.de>
S:\tMaintained
T:\tgit git://git.kernel.org/pub/scm/linux/kernel/git/ras/ras.git
F:\tdrivers/ras/

UNIQUE MAINT SUBSYSTEM
M:\tSolo Maintainer <solo@kernel.org>
S:\tMaintained
F:\tdrivers/solo/

KBUILD
M:\tMichal Marek <mmarek@suse.cz>
S:\tMaintained
F:\tscripts/Makefile*

THE REST
M:\tLinus Torvalds <torvalds@linux-foundation.org>
S:\tBuried alive in reporters
F:\t*
F:\t*/
"""
        sections = MaintainerParser(raw).parse()
        for i, s in enumerate(sections, start=1):
            s.sec_id = i
        return ins._PullSectionResolver(sections, MaintainerMatcher(sections))

    def test_directory_and_maintainer_resolution(self) -> None:
        r = self._build_resolver()
        # 1. Unique T: tree wins directly
        sec = r.resolve(
            url="git://anongit.freedesktop.org/drm/drm", ref="drm-next", subject="Merge tag 'drm-next' of ...",
            sender="Dave Airlie", body="", committer="Linus Torvalds", committer_email="", changed_paths=[],
        )
        self.assertEqual(sec["name"], "DRM DRIVERS")

        # 2. Shared T: tree (ras.git) disambiguated by changed files + tag
        sec = r.resolve(
            url="git://git.kernel.org/pub/scm/linux/kernel/git/ras/ras.git",
            ref="edac_urgent_for_v7.3_rc3",
            subject="Merge tag 'edac_urgent_for_v7.3_rc3' of git://git.kernel.org/pub/scm/linux/kernel/git/ras/ras.git",
            sender="Borislav Petkov", body="", committer="Linus Torvalds", committer_email="",
            changed_paths=["drivers/edac/edac_mc.c"],
        )
        self.assertEqual(sec["name"], "EDAC")

        # 3. Missing T: URL resolved via changed files
        sec = r.resolve(
            url="", ref="edac_urgent_for_v7.3_rc3",
            subject="Merge tag 'edac_urgent_for_v7.3_rc3'",
            sender="", body="", committer="Linus Torvalds", committer_email="",
            changed_paths=["drivers/edac/edac_mc.c", "MAINTAINERS"],
        )
        self.assertEqual(sec["name"], "EDAC")

        # 4. Overlapping directories: maintainer breaks tie in favor of child AMD section over parent DRM DRIVERS
        sec = r.resolve(
            url="", ref="amdgpu-fixes", subject="Merge branch 'amdgpu-fixes'",
            sender="Alex Deucher", body="", committer="Linus Torvalds", committer_email="",
            changed_paths=["drivers/gpu/drm/amd/amdgpu/gfx.c"],
        )
        self.assertEqual(sec["name"], "DRM DRIVERS FOR AMD RADEON AND AMDGPU")

        # 5. Maintainer fallback when no files matched
        sec = r.resolve(
            url="", ref="solo-fixes", subject="Merge branch 'solo-fixes'",
            sender="Solo Maintainer", body="", committer="Linus Torvalds", committer_email="",
            changed_paths=[],
        )
        self.assertEqual(sec["name"], "UNIQUE MAINT SUBSYSTEM")

        # 6. Multi-section maintainer without changed files disambiguated by merge tag
        sec = r.resolve(
            url="", ref="edac_urgent_for_v7.3_rc3", subject="Merge tag 'edac_urgent_for_v7.3_rc3'",
            sender="Borislav Petkov", body="", committer="Linus Torvalds", committer_email="",
            changed_paths=[],
        )
        self.assertEqual(sec["name"], "EDAC")

        # 7. Generic ref ("for-linus") uses git URL repo segment ("kbuild-2.6") for keyword disambiguation
        toks = r._ref_tokens("for-linus", "Merge branch 'for-linus' of git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6",
                             "git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6")
        self.assertIn("kbuild", toks)
        self.assertNotIn("linus", toks)
        sec = r.resolve(
            url="git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6",
            ref="for-linus",
            subject="Merge branch 'for-linus' of git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6",
            sender="Michal Marek", body="", committer="Linus Torvalds", committer_email="",
            changed_paths=[],
        )
        self.assertEqual(sec["name"], "KBUILD")

    def test_project_specific_insights_config(self) -> None:
        from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
        from parser.maintainer_ast.maintainer_parser import MaintainerParser

        raw = (
            "DRM DRIVERS\n"
            "M:\tDave Airlie <airlied@linux.ie>\n"
            "S:\tMaintained\n"
            "T:\tgit git://anongit.freedesktop.org/drm/drm\n"
            "F:\tdrivers/gpu/drm/\n\n"
            "CUSTOM CATCHALL\n"
            "M:\tLead Maintainer <lead@example.org>\n"
            "S:\tBuried\n"
            "F:\t*\n\n"
            "THE REST\n"
            "M:\tOther <other@example.org>\n"
            "S:\tBuried\n"
            "F:\t*\n"
        )
        sections = MaintainerParser(raw).parse()
        for i, s in enumerate(sections, start=1):
            s.sec_id = i

        cfg = {
            "lead_mergers": ["Dave Airlie", "Lead Maintainer"],
            "catchall_section": "CUSTOM CATCHALL",
            "generic_repo_names": ["custom-kernel", "my-tree"],
            "generic_ref_words": ["customnext", "myfixes"],
            "milestone_rc": 2,
            "orphan_min_commits": 2,
            "health_min_commits": 4,
            "no_review_threshold": 0.25,
        }
        r = ins._PullSectionResolver(sections, MaintainerMatcher(sections), insights_cfg=cfg)
        self.assertEqual(r.catchall_section, "CUSTOM CATCHALL")
        self.assertIn("dave airlie", r.lead_mergers)
        self.assertIn("custom-kernel", r.generic_repo_names)
        self.assertIn("customnext", r.ref_stopwords)
        # Verify CUSTOM CATCHALL was filtered out, while THE REST was kept
        sec_names = [s.name for s in r.sections]
        self.assertNotIn("CUSTOM CATCHALL", sec_names)
        self.assertIn("THE REST", sec_names)

        # Lead merger Dave Airlie is excluded from candidate people
        self.assertTrue(r._is_lead_merger("Dave Airlie"))
        self.assertTrue(r._is_lead_merger("Lead Maintainer"))
        self.assertFalse(r._is_lead_merger("Linus Torvalds"))

        # _ref_tokens ignores generic repo name
        toks = r._ref_tokens("myfixes", "Merge branch 'myfixes' of git://x.org/custom-kernel.git", "git://x.org/custom-kernel.git")
        self.assertNotIn("myfixes", toks)
        self.assertNotIn("custom-kernel", toks)

        # flags_for with custom config
        flags = ins.flags_for({
            "commits": 2, "status": "Orphan", "bus_factor": 1, "review_coverage": 0.20,
            "maintainers": [],
        }, insights_cfg=cfg)
        self.assertIn("orphan_active", flags)  # >= orphan_min (2)
        self.assertNotIn("no_review", flags)  # commits (2) < health_min (4)

        flags2 = ins.flags_for({
            "commits": 5, "status": "Maintained", "bus_factor": 1, "review_coverage": 0.20,
            "maintainers": [],
        }, insights_cfg=cfg)
        self.assertIn("no_review", flags2)  # commits >= 4 and 0.20 < 0.25

    def test_compute_merge_window_git_maintainers_fallback(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not installed")
        from tests.test_commit_relations import git

        tmp = Path(tempfile.mkdtemp(prefix="mw-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        g = lambda *a: git(tmp, *a)
        maint = (
            "Maintainers List\n----------------\n\n"
            "EDAC\nM:\tBorislav Petkov <bp@alien8.de>\nS:\tMaintained\nF:\tdrivers/edac/\n\n"
            "THE REST\nM:\tLinus <l@x.org>\nS:\tBuried alive\nF:\t*\nF:\t*/\n"
        )
        g("init", "-q", "-b", "master")
        (tmp / "MAINTAINERS").write_text(maint)
        (tmp / "drivers/edac").mkdir(parents=True)
        (tmp / "drivers/edac/mc.c").write_text("int x;\n")
        g("add", "-A")
        g("-c", "user.name=Linus", "-c", "user.email=l@x.org", "commit", "-q", "-m", "init")
        g("tag", "v7.2")

        g("checkout", "-q", "-b", "edac")
        (tmp / "drivers/edac/mc.c").write_text("int x = 1;\n")
        g("add", "-A")
        g("-c", "user.name=Borislav Petkov", "-c", "user.email=bp@alien8.de", "commit", "-q", "-m", "edac: fix")
        g("checkout", "-q", "master")
        g("-c", "user.name=Linus", "-c", "user.email=l@x.org", "merge", "-q", "--no-ff", "-m",
          "Merge tag 'edac_urgent_for_v7.3_rc3'\n\nPull EDAC fix from Borislav Petkov:\n  fix\n", "edac")
        g("tag", "v7.3")

        with mock.patch.object(rs, "get_repo_dir", lambda: tmp), \
             mock.patch.object(ins, "get_repo_dir", lambda: tmp), \
             mock.patch.object(rs, "_PASS_DISK", _NoDisk()), \
             mock.patch.object(ins, "_DISK", _NoDisk()), \
             mock.patch("webapp.backend.services.commit_facts.commit_ids", return_value={}), \
             mock.patch("webapp.backend.services.maintainer_service.MaintainerService.get_maintainer_data", side_effect=RuntimeError("unparsed")):
            rs._TAGS_CACHE.clear()
            self.addCleanup(rs._TAGS_CACHE.clear)
            res = ins.compute_merge_window("v7.3")
        self.assertEqual(len(res["pulls"]), 1)
        pull = res["pulls"][0]
        self.assertEqual(pull["ref"], "edac_urgent_for_v7.3_rc3")
        self.assertEqual(pull["sender"], "Borislav Petkov")
        self.assertEqual(pull["section"], {"sec_id": None, "name": "EDAC"})

    def test_sender_variants_and_parent_tip_fallback(self) -> None:
        self.assertEqual(ins._sender("Pul tty fixes from Greg Kroah-Hartman:\n"), "Greg Kroah-Hartman")
        self.assertEqual(ins._sender("Pull NTB bugfixes frfom Jon Mason:\n"), "Jon Mason")
        self.assertEqual(ins._sender("Pull execve update Kees Cook:\n"), "Kees Cook")
        self.assertEqual(ins._sender("Merge fixes from Andrew Morton:\n"), "Andrew Morton")
        self.assertEqual(ins._sender("Misc fixes for this merge window from Sasha Levin that were posted for rc1\n"), "Sasha Levin")
        self.assertEqual(ins._sender("* 'for_linus' of git://git.kernel.org/pub/scm/linux/kernel/git/jwessel/linux-2.6-kgdb:\n"), "")

        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        from unittest import mock
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not installed")

        tmp = Path(tempfile.mkdtemp(prefix="mw-sender-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        def g_as(name: str, email: str, *args: str) -> str:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            }
            return subprocess.run(["git", "-C", str(tmp), *args], check=True, capture_output=True, text=True, env=env).stdout

        g_as("Linus Torvalds", "torvalds@example.com", "init", "-q", "-b", "master")
        (tmp / "MAINTAINERS").write_text(
            "Maintainers List\n----------------\n\n"
            "KGDB\nM:\tJason Wessel <jason.wessel@windriver.com>\n"
            "T:\tgit git://git.kernel.org/pub/scm/linux/kernel/git/jwessel/linux-2.6-kgdb\nF:\tkernel/debug/\n\n"
            "VFS\nM:\tAl Viro <viro@zeniv.linux.org.uk>\n"
            "T:\tgit git://git.kernel.org/pub/scm/linux/kernel/git/viro/vfs-2.6\nF:\tfs/\n"
        )
        (tmp / "kernel/debug").mkdir(parents=True)
        (tmp / "kernel/debug/core.c").write_text("int a;\n")
        (tmp / "fs").mkdir(parents=True)
        (tmp / "fs/namei.c").write_text("int b;\n")
        g_as("Linus Torvalds", "torvalds@example.com", "add", "-A")
        g_as("Linus Torvalds", "torvalds@example.com", "commit", "-q", "-m", "init")
        g_as("Linus Torvalds", "torvalds@example.com", "tag", "v2.6.39")

        g_as("Jason Wessel", "jason.wessel@windriver.com", "checkout", "-q", "-b", "kgdb")
        (tmp / "kernel/debug/core.c").write_text("int a = 1;\n")
        g_as("Jason Wessel", "jason.wessel@windriver.com", "add", "-A")
        g_as("Jason Wessel", "jason.wessel@windriver.com", "commit", "-q", "-m", "kgdb: fix")
        g_as("Linus Torvalds", "torvalds@example.com", "checkout", "-q", "master")
        g_as("Linus Torvalds", "torvalds@example.com", "merge", "-q", "--no-ff", "-m",
             "Merge branch 'for_linus' of git://git.kernel.org/pub/scm/linux/kernel/git/jwessel/linux-2.6-kgdb\n\n"
             "* 'for_linus' of git://git.kernel.org/pub/scm/linux/kernel/git/jwessel/linux-2.6-kgdb:\n  kgdb: fix\n",
             "kgdb")

        g_as("Linus Torvalds", "torvalds@example.com", "checkout", "-q", "-b", "vfs")
        (tmp / "fs/namei.c").write_text("int b = 1;\n")
        g_as("Linus Torvalds", "torvalds@example.com", "add", "-A")
        g_as("Linus Torvalds", "torvalds@example.com", "commit", "-q", "-m", "vfs: amended by Linus")
        g_as("Linus Torvalds", "torvalds@example.com", "checkout", "-q", "master")
        g_as("Linus Torvalds", "torvalds@example.com", "merge", "-q", "--no-ff", "-m",
             "Merge branch 'for-linus' of git://git.kernel.org/pub/scm/linux/kernel/git/viro/vfs-2.6\n\n"
             "* 'for-linus' of git://git.kernel.org/pub/scm/linux/kernel/git/viro/vfs-2.6:\n  vfs: amended\n",
             "vfs")
        g_as("Linus Torvalds", "torvalds@example.com", "tag", "v3.0")

        with mock.patch.object(rs, "get_repo_dir", lambda: tmp), \
             mock.patch.object(ins, "get_repo_dir", lambda: tmp), \
             mock.patch.object(rs, "_PASS_DISK", _NoDisk()), \
             mock.patch.object(ins, "_DISK", _NoDisk()), \
             mock.patch("webapp.backend.services.commit_facts.commit_ids", return_value={}), \
             mock.patch("webapp.backend.services.maintainer_service.MaintainerService.get_maintainer_data", side_effect=RuntimeError("unparsed")):
            rs._TAGS_CACHE.clear()
            self.addCleanup(rs._TAGS_CACHE.clear)
            res = ins.compute_merge_window("v3.0")
        by_ref = {pl["source_url"].rsplit("/", 1)[-1]: pl for pl in res["pulls"]}
        self.assertEqual(by_ref["linux-2.6-kgdb"]["sender"], "Jason Wessel")
        self.assertEqual(by_ref["linux-2.6-kgdb"]["section"], {"sec_id": None, "name": "KGDB"})
        self.assertEqual(by_ref["vfs-2.6"]["sender"], "Al Viro")
        self.assertEqual(by_ref["vfs-2.6"]["section"], {"sec_id": None, "name": "VFS"})

    def test_latest_release_tracks_unreleased_merge_window_and_post_release_stats(self) -> None:
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        from unittest import mock

        from webapp.backend.services import commit_query as cq
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not installed")
        tmp = Path(tempfile.mkdtemp(prefix="ki_latest_"))
        self.addCleanup(shutil.rmtree, tmp, True)

        def g_as(name: str, email: str, *args: str) -> str:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            }
            return subprocess.run(["git", "-C", str(tmp), *args], check=True, capture_output=True, text=True, env=env).stdout

        g_as("Linus Torvalds", "torvalds@example.com", "init", "-q", "-b", "master")
        (tmp / "MAINTAINERS").write_text(
            "Maintainers List\n----------------\n\n"
            "KGDB\nM:\tJason Wessel <jason.wessel@windriver.com>\nF:\tkernel/debug/\n"
        )
        (tmp / "kernel/debug").mkdir(parents=True)
        (tmp / "kernel/debug/core.c").write_text("int a;\n")
        g_as("Linus Torvalds", "torvalds@example.com", "add", "-A")
        g_as("Linus Torvalds", "torvalds@example.com", "commit", "-q", "-m", "v7.3 base")
        g_as("Linus Torvalds", "torvalds@example.com", "tag", "v7.3")

        # Post-release merge window pull by a brand-new contributor
        g_as("New Dev", "newdev@example.com", "checkout", "-q", "-b", "kgdb")
        (tmp / "kernel/debug/core.c").write_text("int a = 1;\n")
        g_as("New Dev", "newdev@example.com", "add", "-A")
        g_as("New Dev", "newdev@example.com", "commit", "-q", "-m", "kgdb: post-7.3 fix")
        g_as("Linus Torvalds", "torvalds@example.com", "checkout", "-q", "master")
        g_as("Linus Torvalds", "torvalds@example.com", "merge", "-q", "--no-ff", "-m",
             "Merge tag 'kgdb-for-7.4' of git://git.kernel.org/pub/scm/linux/kernel/git/jwessel/kgdb\n\n"
             "* tag 'kgdb-for-7.4':\n  kgdb: post-7.3 fix\n",
             "kgdb")
        tip_before_rc1 = g_as("Linus Torvalds", "torvalds@example.com", "rev-parse", "HEAD").strip()

        repo_meta = {"name": tmp.name, "repo_dir": str(tmp), "default_branch": "master"}
        with mock.patch.object(rs, "get_repo_dir", lambda: tmp), \
             mock.patch.object(ins, "get_repo_dir", lambda: tmp), \
             mock.patch.object(cq, "get_repo_dir", lambda: tmp), \
             mock.patch("webapp.backend.config.get_active_repository", lambda: repo_meta), \
             mock.patch.object(rs, "_PASS_DISK", _NoDisk()), \
             mock.patch.object(ins, "_DISK", _NoDisk()), \
             mock.patch("webapp.backend.services.commit_facts.commit_ids", return_value={}), \
             mock.patch("webapp.backend.services.maintainer_service.MaintainerService.get_maintainer_data", side_effect=RuntimeError("unparsed")):
            rs.clear_caches()
            self.addCleanup(rs.clear_caches)
            info = rs.latest_info()
            self.assertEqual(info["branch"], "master")
            self.assertEqual(info["sha"], tip_before_rc1)
            self.assertEqual(info["prev"], "v7.3")
            self.assertIsNone(info["rc1"])
            self.assertEqual(info["rc_count"], 0)
            self.assertEqual(rs.previous("latest"), "v7.3")

            stats = ins.compute_release_stats("latest")
            self.assertEqual(stats["release"], "latest")
            self.assertEqual(stats["prev"], "v7.3")
            self.assertEqual(stats["branch"], "master")
            self.assertEqual(stats["head"], tip_before_rc1)
            self.assertEqual(stats["commits"], 1)
            self.assertEqual(stats["merges"], 1)
            self.assertEqual(stats["first_time_authors"], 1)
            self.assertEqual(stats["first_timers"][0]["email"], "newdev@example.com")

            mw = ins.compute_merge_window("latest")
            self.assertEqual(mw["release"], "latest")
            self.assertEqual(mw["prev"], "v7.3")
            self.assertEqual(mw["branch"], "master")
            self.assertIsNone(mw["rc1"])
            self.assertEqual(mw["totals"]["pulls"], 1)
            self.assertEqual(mw["pulls"][0]["section"], {"sec_id": None, "name": "KGDB"})
            self.assertFalse(mw["pulls"][0]["after_rc1"])

            health = ins.compute_subsystem_health("latest", window=2)
            self.assertEqual(health["release"], "latest")
            self.assertEqual(health["branch"], "master")
            self.assertEqual(health["window"], ["v7.3", "latest"])
            kgdb = next(s for s in health["sections"] if s["name"] == "KGDB")
            self.assertEqual(kgdb["per_release"], [1, 1])

            res = cq.query_commits("master", release="latest", section="KGDB")
            self.assertIn("v7.3..master (unreleased)", res["range"])
            self.assertEqual([c["subject"] for c in res["commits"]], ["kgdb: post-7.3 fix"])

            # Tag v7.4-rc1 later in the cycle and verify post-rc1 tracking
            g_as("Linus Torvalds", "torvalds@example.com", "tag", "v7.4-rc1")
            rs.clear_caches()
            info2 = rs.latest_info()
            self.assertEqual(info2["prev"], "v7.3")
            self.assertEqual(info2["rc1"], "v7.4-rc1")
            self.assertEqual(info2["rc_count"], 1)

    def test_resolve_release_tag_autodetects_commits_and_branches(self) -> None:
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        from unittest import mock

        from fastapi.testclient import TestClient

        from webapp.backend.app import create_app

        if not shutil.which("git"):
            raise unittest.SkipTest("git is not installed")
        tmp = Path(tempfile.mkdtemp(prefix="ki_resolve_"))
        self.addCleanup(shutil.rmtree, tmp, True)

        def g(*args: str) -> str:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "GIT_AUTHOR_NAME": "Linus", "GIT_AUTHOR_EMAIL": "linus@example.com",
                "GIT_COMMITTER_NAME": "Linus", "GIT_COMMITTER_EMAIL": "linus@example.com",
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            }
            return subprocess.run(["git", "-C", str(tmp), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()

        g("init", "-q", "-b", "master")
        (tmp / "f").write_text("1\n")
        g("add", "-A")
        g("commit", "-q", "-m", "init")
        c_v1 = g("rev-parse", "HEAD")
        g("tag", "v1.0")

        (tmp / "f").write_text("2\n")
        g("add", "-A")
        g("commit", "-q", "-m", "for v1.1-rc1")
        c_mid = g("rev-parse", "HEAD")
        (tmp / "f").write_text("3\n")
        g("add", "-A")
        g("commit", "-q", "-m", "v1.1-rc1")
        g("tag", "v1.1-rc1")
        (tmp / "f").write_text("4\n")
        g("add", "-A")
        g("commit", "-q", "-m", "v1.1")
        g("tag", "v1.1")

        # Stable branch off v1.0
        g("checkout", "-q", "-b", "linux-1.0.y", "v1.0")
        (tmp / "f").write_text("1.1\n")
        g("add", "-A")
        g("commit", "-q", "-m", "stable backport")
        c_stable_mid = g("rev-parse", "HEAD")
        g("tag", "v1.0.1")
        (tmp / "f").write_text("1.2\n")
        g("add", "-A")
        g("commit", "-q", "-m", "untagged stable tip")
        c_stable_tip = g("rev-parse", "HEAD")

        # Unreleased work on master after v1.1
        g("checkout", "-q", "master")
        (tmp / "f").write_text("5\n")
        g("add", "-A")
        g("commit", "-q", "-m", "post-1.1 unreleased")
        c_unrel = g("rev-parse", "HEAD")

        repo_meta = {"name": tmp.name, "repo_dir": str(tmp), "default_branch": "master"}
        with mock.patch.object(rs, "get_repo_dir", lambda: tmp), \
             mock.patch("webapp.backend.config.get_active_repository", lambda: repo_meta), \
             mock.patch("webapp.backend.services.release_service.get_active_repo_name", lambda: tmp.name), \
             mock.patch("webapp.backend.services.release_service.db_covers_all", return_value=False), \
             mock.patch("webapp.backend.services.release_service.release_lookup", return_value={
                 c_mid: {"mainline": "v1.1-rc1", "stable": None},
                 c_stable_mid: {"mainline": None, "stable": "v1.0.1"},
             }), \
             mock.patch("webapp.backend.services.release_service.ensure_release_index", return_value={"state": "done"}):
            rs.clear_caches()
            self.addCleanup(rs.clear_caches)

            # Exact tag & stable tag
            self.assertEqual(rs.resolve_release_tag("v1.1")["tag"], "v1.1")
            self.assertTrue(rs.resolve_release_tag("v1.1")["exact"])
            res_stable_tag = rs.resolve_release_tag("v1.0.1")
            self.assertEqual(res_stable_tag["final"], "v1.0")
            self.assertEqual(res_stable_tag["series"], "v1.0")

            # Commit pointing directly to v1.0
            res_exact_sha = rs.resolve_release_tag(c_v1)
            self.assertEqual(res_exact_sha["tag"], "v1.0")
            self.assertEqual(res_exact_sha["source"], "exact_sha")
            self.assertFalse(res_exact_sha["exact"])

            # Released commit between v1.0 and v1.1-rc1 resolves to v1.1
            res_mid = rs.resolve_release_tag(c_mid)
            self.assertEqual(res_mid["tag"], "v1.1")
            self.assertEqual(res_mid["final"], "v1.1")
            self.assertFalse(res_mid["latest"])
            self.assertEqual(res_mid["source"], "release_index")

            # Commit on stable branch between v1.0 and v1.0.1 resolves to v1.0.1 (series v1.0)
            res_st = rs.resolve_release_tag(c_stable_mid)
            self.assertEqual(res_st["tag"], "v1.0.1")
            self.assertEqual(res_st["final"], "v1.0")
            self.assertEqual(res_st["series"], "v1.0")

            # Untagged commit on stable branch falls back to nearest ancestor tag v1.0.1, not latest
            res_st_tip = rs.resolve_release_tag(c_stable_tip)
            self.assertEqual(res_st_tip["tag"], "v1.0.1")
            self.assertFalse(res_st_tip["latest"])
            self.assertEqual(res_st_tip["source"], "ancestor")

            # Unreleased commit / master branch on default branch marks latest=True and detects v1.1
            res_unrel = rs.resolve_release_tag(c_unrel[:10])
            self.assertEqual(res_unrel["tag"], "v1.1")
            self.assertTrue(res_unrel["latest"])
            self.assertEqual(res_unrel["source"], "ancestor")
            res_master = rs.resolve_release_tag("master")
            self.assertEqual(res_master["tag"], "v1.1")
            self.assertTrue(res_master["latest"])

            # HTTP endpoint test
            client = TestClient(create_app())
            r_ok = client.get(f"/api/insights/resolve?rev={c_mid}")
            self.assertEqual(r_ok.status_code, 200)
            self.assertEqual(r_ok.json()["tag"], "v1.1")
            r_bad = client.get("/api/insights/resolve?rev=deadbeefcafe")
            self.assertEqual(r_bad.status_code, 404)


class TestSecurityInsights(unittest.TestCase):
    def test_first_final_release_resolution_helpers(self) -> None:
        from parser.cve.cve_releases import earliest_final_release, first_final_release

        tags_with_final = {"v6.1-rc1", "v6.1-rc2", "v6.1", "v5.15"}
        self.assertEqual(first_final_release({"mainline": "v6.1-rc2", "stable": None}, tags_with_final), "v6.1")
        self.assertEqual(first_final_release({"mainline": "v6.1", "stable": None}, tags_with_final), "v6.1")
        # -rc only (no final v6.1 tag yet) -> None
        self.assertIsNone(first_final_release({"mainline": "v6.1-rc2", "stable": None}, {"v6.1-rc1", "v6.1-rc2"}))
        # Stable-only, next-*, and unversioned -> None
        self.assertIsNone(first_final_release({"mainline": None, "stable": "v6.1.3"}, tags_with_final))
        self.assertIsNone(first_final_release({"mainline": "next-20260101", "stable": None}, tags_with_final | {"next-20260101"}))
        self.assertIsNone(first_final_release(None, tags_with_final))
        # Earliest across multiple introducing commits
        self.assertEqual(
            earliest_final_release(
                [{"mainline": "v6.1-rc2", "stable": None}, {"mainline": "v5.15", "stable": None}, None],
                tags_with_final,
            ),
            "v5.15",
        )
        self.assertIsNone(earliest_final_release([{"mainline": "v6.2-rc1", "stable": None}, None], tags_with_final))

    def test_security_endpoint_introduced_release_and_cvss_filtering(self) -> None:
        from unittest import mock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from webapp.backend.routers import insights as insights_router

        app = FastAPI()
        app.include_router(insights_router.router)
        client = TestClient(app)

        sample_report = {
            "state": "done",
            "result": {
                "release": "v6.6",
                "head": "a" * 40,
                "layout": "vulns",
                "kinds": {"cna": 4, "commit": 0, "text": 0},
                "counts": {"affected": 2, "possibly": 1, "fixed": 2, "fixed_here": 1},
                "source": "database",
                "cves": [
                    {"id": "CVE-2024-0005", "title": "Exact 7.5 float", "state": "affected", "basis": "record",
                     "kind": "cna", "published": "2024-01-05", "fixes": [], "fixed_here": False,
                     "introduced_release": "v6.1", "score": 7.499999999999999, "severity": "HIGH", "kev": None, "cwes": []},
                    {"id": "CVE-2024-0004", "title": "Critical 9.8", "state": "fixed", "basis": "record",
                     "kind": "cna", "published": "2024-01-04", "fixes": ["b" * 40], "fixed_here": True,
                     "introduced_release": "v5.15", "score": 9.8, "severity": "CRITICAL", "kev": "2024-02-01", "cwes": []},
                    {"id": "CVE-2024-0003", "title": "Exact 7.5", "state": "fixed", "basis": "record",
                     "kind": "cna", "published": "2024-01-03", "fixes": ["c" * 40], "fixed_here": False,
                     "introduced_release": "v6.1", "score": 7.5, "severity": "HIGH", "kev": None, "cwes": []},
                    {"id": "CVE-2024-0002", "title": "Medium 5.2", "state": "possibly", "basis": "description",
                     "kind": "cna", "published": "2024-01-02", "fixes": [], "fixed_here": False,
                     "introduced_release": None, "score": 5.2, "severity": "MEDIUM", "kev": None, "cwes": []},
                    {"id": "CVE-2024-0001", "title": "Unscored RC-only", "state": "affected", "basis": "record",
                     "kind": "cna", "published": "2024-01-01", "fixes": [], "fixed_here": False,
                     "introduced_release": None, "score": None, "severity": "", "kev": None, "cwes": []},
                ],
            },
        }

        with mock.patch("webapp.backend.config.repo_feature_enabled", return_value=True), \
                mock.patch("webapp.backend.services.cve_service.report", return_value=sample_report):
            # Unfiltered preserves all 5 CVEs including null CVSS and introduced_release
            r_all = client.get("/api/insights/v6.6/cves")
            self.assertEqual(r_all.status_code, 200)
            body_all = r_all.json()
            self.assertEqual(body_all["total"], 5)
            self.assertEqual(body_all["cves"][0]["introduced_release"], "v6.1")
            self.assertIsNone(body_all["cves"][-1]["introduced_release"])

            # `< 7.5`: strictly excludes null CVSS (CVE-2024-0001) and 7.5/9.8
            r_lt = client.get("/api/insights/v6.6/cves?cvss_op=lt&cvss_score=7.5")
            self.assertEqual(r_lt.status_code, 200)
            b_lt = r_lt.json()
            self.assertEqual([c["id"] for c in b_lt["cves"]], ["CVE-2024-0002"])
            self.assertEqual(b_lt["total"], 1)
            self.assertEqual(b_lt["counts"], {"affected": 0, "possibly": 1, "fixed": 0, "fixed_here": 0})
            self.assertEqual(b_lt["cvss_filter"], {"op": "lt", "score": 7.5})

            # `> 7.5`: only 9.8
            r_gt = client.get("/api/insights/v6.6/cves?cvss_op=gt&cvss_score=7.5")
            self.assertEqual([c["id"] for c in r_gt.json()["cves"]], ["CVE-2024-0004"])
            self.assertEqual(r_gt.json()["counts"], {"affected": 0, "possibly": 0, "fixed": 1, "fixed_here": 1})

            # `= 7.5`: matches 7.5 and float 7.499999999999999 reliably
            r_eq = client.get("/api/insights/v6.6/cves?cvss_op=eq&cvss_score=7.5")
            self.assertEqual([c["id"] for c in r_eq.json()["cves"]], ["CVE-2024-0005", "CVE-2024-0003"])
            self.assertEqual(r_eq.json()["counts"], {"affected": 1, "possibly": 0, "fixed": 1, "fixed_here": 0})

            # `>= 7.5` (also testing symbol alias `≥`)
            r_ge = client.get("/api/insights/v6.6/cves", params={"cvss_op": "≥", "cvss_score": "7.5"})
            self.assertEqual([c["id"] for c in r_ge.json()["cves"]], ["CVE-2024-0005", "CVE-2024-0004", "CVE-2024-0003"])

            # Out-of-range clamping on backend (-3.2 -> 0.0; 15.8 -> 10.0)
            r_low_clamp = client.get("/api/insights/v6.6/cves?cvss_op=ge&cvss_score=-3.2")
            self.assertEqual(r_low_clamp.status_code, 200)
            self.assertEqual(r_low_clamp.json()["cvss_filter"], {"op": "ge", "score": 0.0})
            self.assertEqual(r_low_clamp.json()["total"], 4)  # excludes null CVSS

            r_high_clamp = client.get("/api/insights/v6.6/cves?cvss_op=lt&cvss_score=15.8")
            self.assertEqual(r_high_clamp.status_code, 200)
            self.assertEqual(r_high_clamp.json()["cvss_filter"], {"op": "lt", "score": 10.0})
            self.assertEqual(r_high_clamp.json()["total"], 4)  # excludes null CVSS

            # Invalid operator / non-finite score rejected cleanly
            self.assertEqual(client.get("/api/insights/v6.6/cves?cvss_op=invalid").status_code, 400)
            self.assertIn(client.get("/api/insights/v6.6/cves?cvss_score=nan").status_code, (400, 422))

        # Uningested response stays clean with CVSS params
        with mock.patch("webapp.backend.config.repo_feature_enabled", return_value=True), \
                mock.patch("webapp.backend.services.cve_service.report",
                           return_value={"state": "done", "result": {"configured": False, "ingested": False, "cves": []}}):
            r_un = client.get("/api/insights/v6.6/cves?cvss_op=ge&cvss_score=7")
            self.assertEqual(r_un.status_code, 200)
            self.assertEqual(r_un.json(), {"configured": False, "ingested": False, "total": 0, "cves": []})

    def test_release_report_introduced_release_rc_only_and_final(self) -> None:
        from unittest import mock

        from webapp.backend.services import cve_service as cs

        c_rc = "1" * 40
        c_rel = "2" * 40
        recs = {
            "CVE-2024-1001": {
                "id": "CVE-2024-1001", "kind": "cna", "title": "Final tag intro",
                "description": "", "published": "2024-01-01", "updated": "2024-01-01",
                "ranges": [{"introduced": c_rel, "fixed": None, "files": []}],
                "entries": [], "files": [], "references": [],
                "cvss": {"score": 7.5, "severity": "HIGH"}, "kev": None, "cwes": [],
            },
            "CVE-2024-1002": {
                "id": "CVE-2024-1002", "kind": "cna", "title": "RC-only intro",
                "description": "", "published": "2024-01-02", "updated": "2024-01-02",
                "ranges": [{"introduced": c_rc, "fixed": None, "files": []}],
                "entries": [], "files": [], "references": [],
                "cvss": None, "kev": None, "cwes": [],
            },
        }
        tag_map = {"v6.1": "a" * 40, "v6.1-rc2": "b" * 40, "v6.2-rc1": "c" * 40}
        lookup_map = {
            c_rel: {"mainline": "v6.1-rc2", "stable": None},
            c_rc: {"mainline": "v6.2-rc1", "stable": None},
        }
        with mock.patch.object(cs, "source", return_value={"name": "vulns", "layout": "vulns", "head": "f" * 40, "built_at": 1}), \
                mock.patch.object(cs, "records", return_value=recs), \
                mock.patch.object(cs.rs, "tag_commits", return_value=tag_map), \
                mock.patch.object(cs.rs, "release_lookup", return_value=lookup_map), \
                mock.patch.object(cs.rs, "release_token", return_value="tok"), \
                mock.patch.object(cs.rs, "db_covers_all", return_value=False):
            cs._PREFETCH_CACHE.clear()
            rep = cs.release_report("v6.2-rc1")
        by_id = {c["id"]: c for c in rep["cves"]}
        # v6.1-rc2 resolves to final mainline v6.1
        self.assertEqual(by_id["CVE-2024-1001"]["introduced_release"], "v6.1")
        # v6.2-rc1 has no final v6.2 tag yet -> None
        self.assertIsNone(by_id["CVE-2024-1002"]["introduced_release"])


class TestSubsystemHealthDeltasAndFixes(unittest.TestCase):
    """Delta range scans, boundary fallbacks, -rc/next/stable windows, and bugfixes in subsystems health."""

    def test_health_window_rc_next_and_stable_tags(self) -> None:
        from unittest import mock
        tag_map = {
            "v1.0": "1" * 40,
            "v1.0.1": "2" * 40,
            "v1.0.2": "3" * 40,
            "v1.1": "4" * 40,
            "v1.2-rc1": "5" * 40,
            "v1.2-rc2": "6" * 40,
            "next-20261109": "7" * 40,
            "next-20261110": "8" * 40,
        }
        with mock.patch.object(rs, "tag_commits", return_value=tag_map):
            self.assertEqual(ins._health_window("v1.2-rc2", 4), ["v1.0", "v1.1", "v1.2-rc2"])
            self.assertEqual(ins._health_window("v1.2-rc2", 1), ["v1.2-rc2"])
            self.assertEqual(ins._health_window("next-20261110", 3), ["v1.1", "next-20261109", "next-20261110"])
            self.assertEqual(ins._health_window("v1.0.2", 3), ["v1.0", "v1.0.1", "v1.0.2"])
            self.assertEqual(ins._health_window("v1.0.2", 4), ["v1.0", "v1.0.1", "v1.0.2"])

    def test_delta_range_scan_dedup_rc_and_boundary_fallback(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock

        from parser.git_ast.release_order import series_ord_for, series_track_for
        from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
        from parser.maintainer_ast.maintainer_parser import MaintainerParser
        from tests.db_fixtures import make_db, patch_reader
        from tests.test_commit_relations import git
        from webapp.backend.services.jobs import JobManager

        if not shutil.which("git"):
            raise unittest.SkipTest("git is not installed")

        tmp = Path(tempfile.mkdtemp(prefix="subsys-delta-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        g = lambda *a: git(tmp, *a)

        maint_v1 = (
            "FOO DRIVER\nM:\tAnn Dev <ann@x.org>\nS:\tMaintained\nF:\tdrivers/foo/\n\n"
            "NO FILES\nM:\tAnn Dev <ann@x.org>\nS:\tMaintained\nN:\tfoo\n\n"
            "THE REST\nM:\tLinus <l@x.org>\nS:\tBuried\nF:\t*\n"
        )

        def commit(author: str, email: str, msg: str, files: dict[str, str]) -> str:
            for path, text in files.items():
                (tmp / path).parent.mkdir(parents=True, exist_ok=True)
                (tmp / path).write_text(text)
            g("add", "-A")
            g("-c", f"user.name={author}", "-c", f"user.email={email}", "commit", "-q", "--author", f"{author} <{email}>", "-m", msg)
            return g("rev-parse", "HEAD").strip()

        g("init", "-q", "-b", "master")
        commit("Ann Dev", "ann@x.org", "init", {"MAINTAINERS": maint_v1, "drivers/foo/a.c": "0\n"})
        g("tag", "v1.0")
        commit("Ann Dev", "ann@x.org", "rc1 fix\n\nReviewed-by: Rev <rev@x.org>", {"drivers/foo/a.c": "1\n"})
        g("tag", "v1.1-rc1")
        commit("Bob Bar", "bob@x.org", "final fix", {"drivers/foo/a.c": "2\n"})
        g("tag", "v1.1")
        commit("Ann Dev", "ann@x.org", "v1.2 work", {"drivers/foo/a.c": "3\n"})
        g("tag", "v1.2")

        secs = MaintainerParser(maint_v1).parse()
        for idx, s in enumerate(secs, start=1):
            s.sec_id = idx
        matcher = MaintainerMatcher(secs)

        db = make_db(
            "sqlite",
            tables=[D.insights_subsystem_version_delta, D.insights_subsystem_author_delta],
        )
        ann_key = ins.identity("Ann Dev", "ann@x.org")
        bob_key = ins.identity("Bob Bar", "bob@x.org")

        # Insert BOTH rolled-up v1.1 (parent_tag=v1.0) AND v1.1-rc1 (parent_tag=v1.0) to verify no double-counting,
        # plus v1.2 (parent_tag=v1.1).
        db.insert_rows(
            "insights_subsystem_version_delta",
            [
                "version_tag", "subsystem_name", "parent_tag", "series_track", "series_ord",
                "release_ts", "maintainer_status", "commit_count", "merge_commit_count",
                "lines_added", "lines_removed", "files_touched_count", "distinct_author_count",
                "fixes_commit_count", "stable_cc_count", "reviewed_commit_count", "tested_commit_count",
            ],
            [
                ("v1.1-rc1", "", "v1.0", series_track_for("v1.1-rc1"), series_ord_for("v1.1-rc1"), 0, "", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                ("v1.1-rc1", "FOO DRIVER", "v1.0", series_track_for("v1.1-rc1"), series_ord_for("v1.1-rc1"), 0, "Maintained", 1, 0, 1, 0, 1, 1, 0, 0, 1, 0),
                ("v1.1", "", "v1.0", series_track_for("v1.1"), series_ord_for("v1.1"), 0, "", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                ("v1.1", "FOO DRIVER", "v1.0", series_track_for("v1.1"), series_ord_for("v1.1"), 0, "Maintained", 2, 0, 2, 0, 1, 2, 0, 0, 1, 0),
                ("v1.2", "", "v1.1", series_track_for("v1.2"), series_ord_for("v1.2"), 0, "", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                ("v1.2", "FOO DRIVER", "v1.1", series_track_for("v1.2"), series_ord_for("v1.2"), 0, "Maintained", 1, 0, 1, 0, 1, 1, 0, 0, 0, 0),
            ],
        )
        db.insert_rows(
            "insights_subsystem_author_delta",
            ["version_tag", "subsystem_name", "author_identity_id", "series_track", "series_ord", "author_key", "commit_count", "last_active_ts"],
            [
                ("v1.1-rc1", "FOO DRIVER", 1, series_track_for("v1.1-rc1"), series_ord_for("v1.1-rc1"), ann_key, 1, 100),
                ("v1.1", "FOO DRIVER", 1, series_track_for("v1.1"), series_ord_for("v1.1"), ann_key, 1, 200),
                ("v1.1", "FOO DRIVER", 2, series_track_for("v1.1"), series_ord_for("v1.1"), bob_key, 1, 250),
                ("v1.2", "FOO DRIVER", 1, series_track_for("v1.2"), series_ord_for("v1.2"), ann_key, 1, 300),
            ],
        )

        with patch_reader(db), \
                mock.patch.object(rs, "get_repo_dir", lambda: tmp), \
                mock.patch.object(ins, "get_repo_dir", lambda: tmp), \
                mock.patch.object(rs, "_PASS_DISK", _NoDisk()), \
                mock.patch.object(ins, "_DISK", _NoDisk()), \
                mock.patch.object(rs, "jobs", JobManager(workers=1)), \
                mock.patch("webapp.backend.database.helpers.get_active_repo_id", lambda cur: 1), \
                mock.patch("webapp.backend.database.helpers.get_active_network_id", lambda cur: 1), \
                mock.patch.object(ins, "_maintainer_data", return_value=(secs, matcher)):
            rs._TAGS_CACHE.clear()
            self.addCleanup(rs._TAGS_CACHE.clear)

            # Full delta coverage serves synchronously from DB without double-counting v1.1-rc1
            res = ins.subsystem_health("v1.2", window=2)
            self.assertEqual(res["state"], "done")
            self.assertEqual(res["result"]["source"], "database")
            rows = {r["name"]: r for r in res["result"]["sections"]}
            self.assertEqual(rows["FOO DRIVER"]["commits"], 3)
            self.assertEqual(rows["FOO DRIVER"]["per_release"], [2, 1])
            self.assertEqual(rows["FOO DRIVER"]["authors"], 2)  # Ann (2) + Bob (1)
            self.assertEqual(rows["FOO DRIVER"]["bus_factor"], 1)
            self.assertEqual(rows["FOO DRIVER"]["maintainers"][0]["last_active"], 300)
            self.assertTrue(rows["FOO DRIVER"]["has_file_patterns"])
            self.assertFalse(rows["NO FILES"]["has_file_patterns"])

            # Delete v1.2 from deltas -> partial window merges v1.1 from delta + ONLY v1.2 from Git!
            db.delete_rows("insights_subsystem_version_delta", {"version_tag": "v1.2"})
            db.delete_rows("insights_subsystem_author_delta", {"version_tag": "v1.2"})
            with mock.patch.object(rs, "release_commits_now", wraps=rs.release_commits_now) as m_pass:
                mixed = ins.compute_subsystem_health("v1.2", 2)
            self.assertEqual(mixed["source"], "git")
            m_pass.assert_called_once()
            self.assertEqual(m_pass.call_args.args[0], "v1.2")
            m_rows = {r["name"]: r for r in mixed["sections"]}
            self.assertEqual(m_rows["FOO DRIVER"]["commits"], 3)
            self.assertEqual(m_rows["FOO DRIVER"]["per_release"], [2, 1])
            self.assertEqual(m_rows["FOO DRIVER"]["authors"], 2)

            # repo_id < 0 or empty tags never falsely return source="database"
            with mock.patch("webapp.backend.database.helpers.get_active_repo_id", lambda cur: -1):
                self.assertIsNone(ins.health_from_db("v1.2", ["v1.1", "v1.2"], secs, matcher))
            self.assertIsNone(ins.health_from_db("v1.2", [], secs, matcher))

    def test_precomputed_health_keeps_earlier_releases_across_maintainers_evolution(self) -> None:
        import json
        from unittest import mock

        from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
        from parser.maintainer_ast.maintainer_parser import MaintainerParser
        from tests.db_fixtures import make_db, patch_reader

        maint_v1 = "FOO SUBSYSTEM\nM:\tAnn B <ann@x.org>\nS:\tMaintained\nF:\ta.c\n\nTHE REST\nF:\t*\n"
        maint_v2 = (
            "FOO SUBSYSTEM\nM:\tAnn B <ann@x.org>\nS:\tMaintained\nF:\ta.c\n\n"
            "NEW SUBSYSTEM\nM:\tBob <bob@x.org>\nS:\tMaintained\nF:\tb.c\n\n"
            "THE REST\nF:\t*\n"
        )
        secs_v1 = MaintainerParser(maint_v1).parse()
        secs_v2 = MaintainerParser(maint_v2).parse()
        matcher_v2 = MaintainerMatcher(secs_v2)

        db = make_db("sqlite", tables=[D.m_release_segment, D.m_release_section_health])
        h1, h2 = "a" * 40, "b" * 40
        db.insert_rows(
            "m_release_segment",
            ["segment_id", "repo_id", "tag", "tag_hash", "chain", "base_tag", "base_hash", "commits", "merges", "diff_base_tag", "files_changed", "lines_added", "lines_removed", "format"],
            [
                (1, 1, "v1.1", bytes.fromhex(h1), 1, "v1.0", b"\x00" * 20, 2, 0, "v1.0", 1, 1, 0, 2),
                (2, 1, "v1.2", bytes.fromhex(h2), 1, "v1.1", bytes.fromhex(h1), 1, 0, "v1.1", 1, 1, 0, 2),
            ],
        )
        foo_v1 = secs_v1[0]
        db.insert_rows(
            "m_release_section_health",
            ["segment_id", "sec_name", "sec_id", "status", "commits", "reviewed_commits", "authors_json", "activity_json", "pat_hash", "format"],
            [
                (1, "FOO SUBSYSTEM", 1, "Maintained", 2, 1, json.dumps({"e:ann@x.org": 2}), json.dumps({"e:ann@x.org": 10}), ins._section_pattern_hash(foo_v1), ins.HEALTH_FORMAT),
                (1, "", 0, "", 0, 0, None, None, ins._sections_manifest_hash(secs_v1, "THE REST"), ins.HEALTH_FORMAT),
                (2, "FOO SUBSYSTEM", 1, "Maintained", 1, 0, json.dumps({"e:ann@x.org": 1}), json.dumps({"e:ann@x.org": 20}), ins._section_pattern_hash(secs_v2[0]), ins.HEALTH_FORMAT),
                (2, "NEW SUBSYSTEM", 2, "Maintained", 1, 1, json.dumps({"e:bob@x.org": 1}), json.dumps({"e:bob@x.org": 21}), ins._section_pattern_hash(secs_v2[1]), ins.HEALTH_FORMAT),
                (2, "", 0, "", 0, 0, None, None, ins._sections_manifest_hash(secs_v2, "THE REST"), ins.HEALTH_FORMAT),
            ],
        )
        from webapp.backend.database.pool import get_reader
        tc = {"v1.0": "0" * 40, "v1.1": h1, "v1.2": h2}
        with patch_reader(db), \
                mock.patch.object(rs, "tag_commits", return_value=tc), \
                mock.patch.object(rs, "previous", side_effect=lambda t: {"v1.1": "v1.0", "v1.2": "v1.1"}.get(t)), \
                get_reader() as cur:
            pre, unans = ins._read_precomputed_health(cur, 1, ["v1.1", "v1.2"], secs_v2, tc, target_tag="v1.2")
        # v1.1 must NOT be invalidated just because NEW SUBSYSTEM was added in v1.2!
        self.assertEqual(unans, [])
        self.assertIn("v1.1", pre)
        self.assertIn("v1.2", pre)
        res = ins._aggregate_health("v1.2", ["v1.1", "v1.2"], secs_v2, matcher_v2, lambda t: [], precomputed=pre)
        by_name = {s["name"]: s for s in res["sections"]}
        self.assertEqual(by_name["FOO SUBSYSTEM"]["per_release"], [2, 1])
        self.assertEqual(by_name["NEW SUBSYSTEM"]["per_release"], [0, 1])

    def test_load_maintainers_skips_db_for_unparsed_and_latest(self) -> None:
        from unittest import mock

        from tests.db_fixtures import make_db, patch_reader

        db = make_db("sqlite", tables=[D.m_git_ref, D.m_commit, D.m_ingest_state])
        with patch_reader(db), \
                mock.patch("webapp.backend.database.helpers.get_active_repo_id", lambda cur: -1), \
                mock.patch.object(ins, "_git", return_value="FOO\nM:\tA <a@x>\nS:\tMaintained\nF:\ta.c\n"), \
                mock.patch("webapp.backend.services.maintainer_service.MaintainerService.get_maintainer_data",
                           side_effect=AssertionError("should not query MaintainerService when unparsed")):
            secs, _matcher = ins._load_maintainers("latest", "a" * 40)
            self.assertEqual([s.name for s in secs], ["FOO"])
            secs2, _ = ins._load_maintainers("v99.0", "b" * 40)
            self.assertEqual([s.name for s in secs2], ["FOO"])

    def test_compute_subsystem_health_reraises_job_cancelled(self) -> None:
        from unittest import mock

        from webapp.backend.services.jobs import JobCancelled

        with mock.patch.object(ins, "_health_window", return_value=["v1.1"]), \
                mock.patch.object(ins, "_maintainer_data", return_value=([], None)), \
                mock.patch.object(ins, "health_from_db", side_effect=JobCancelled()), \
                self.assertRaises(JobCancelled):
            ins.compute_subsystem_health("v1.1", 1)

    def test_subsystem_health_cache_key_changes_with_window_tags(self) -> None:
        from unittest import mock
        seen_keys: list[str] = []

        def fake_job(key, build, ck, wait):
            seen_keys.append(ck)
            return {"state": "building", "progress": {}}

        with mock.patch.object(ins, "_check_release"), \
                mock.patch.object(ins, "_maintainer_data", return_value=([], None)), \
                mock.patch.object(ins, "health_from_db", return_value=None), \
                mock.patch.object(rs, "release_token", return_value="tok"), \
                mock.patch.object(ins, "_job", side_effect=fake_job):
            with mock.patch.object(rs, "tag_commits", return_value={"v1.0": "1" * 40, "v1.2": "2" * 40}), \
                    mock.patch.object(ins, "_health_window", return_value=["v1.0", "v1.2"]):
                ins.subsystem_health("v1.2", window=2)
            with mock.patch.object(rs, "tag_commits", return_value={"v1.0": "1" * 40, "v1.1": "3" * 40, "v1.2": "2" * 40}), \
                    mock.patch.object(ins, "_health_window", return_value=["v1.1", "v1.2"]):
                ins.subsystem_health("v1.2", window=2)
        self.assertEqual(len(seen_keys), 2)
        self.assertNotEqual(seen_keys[0], seen_keys[1])


