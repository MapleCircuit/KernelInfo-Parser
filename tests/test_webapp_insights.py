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


class TestMergeWindowSectionResolution(unittest.TestCase):
    """Merge-window subsystem resolution via T: tree, changed files/directories, and maintainer."""

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
        g = lambda *a: git(tmp, *a)  # noqa: E731
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
