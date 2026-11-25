"""tests/test_webapp_stable.py - Stable backports report (H2, webapp/API.md section 4.10)."""
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

from cache_engine import get_cache_engine
from webapp.backend.services import commit_relations as cr
from webapp.backend.services import release_service as rs
from webapp.backend.services import stable_service as ss
from webapp.backend.services.jobs import JobManager


def release_cache_scope(root) -> None:
    """End a test's temporary cache scope on every engine: RAM spaces outlive ``close()``, so the scope is dropped;
    SQLite handles are closed before the directory goes."""
    get_cache_engine("ram").discard_scope(root)
    get_cache_engine("sqlite").close()


class TestAnnotations(unittest.TestCase):
    def test_annotation_applies(self) -> None:
        self.assertTrue(ss.annotation_applies("", "v3.0"))
        self.assertTrue(ss.annotation_applies("3.0+", "v3.0"))
        self.assertTrue(ss.annotation_applies("2.6.32+", "v3.0"))
        self.assertFalse(ss.annotation_applies("3.1+", "v3.0"))
        self.assertTrue(ss.annotation_applies("v3.0", "v3.0"))
        self.assertTrue(ss.annotation_applies("3.0.x", "v3.0"))
        self.assertFalse(ss.annotation_applies("3.2", "v3.0"))
        self.assertTrue(ss.annotation_applies("2.6.32-3.2", "v3.0"))
        self.assertTrue(ss.annotation_applies("2.6.32-3.2", "v2.6.35"))
        self.assertFalse(ss.annotation_applies("2.6.32-3.2", "v3.4"))
        self.assertTrue(ss.annotation_applies("3.2, 3.0", "v3.0"))
        self.assertTrue(ss.annotation_applies("for all stable trees", "v3.0"))
        self.assertTrue(ss.annotation_applies("2.6.39+", "v2.6.39"))
        self.assertFalse(ss.annotation_applies("3.0+", "v2.6.39"))


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestMissingReport(unittest.TestCase):
    CACHE_ENGINE = "ram"   # pinned: the developer's config.json must not pick the engine under test

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cache = tempfile.TemporaryDirectory()
        repo = Path(cls.tmp.name)
        cls.repo = repo
        cls.env = {**os.environ, "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "A",
                   "GIT_COMMITTER_EMAIL": "a@x", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        g = cls.git
        g("init", "-q", "-b", "master")
        cls.base = cls.commit("a.c", "1\n", "base")
        g("tag", "v1.0")
        cls.cc = cls.commit("a.c", "2\n", "fix one\n\nCc: stable@vger.kernel.org # 1.0+")
        cls.cc_other = cls.commit("a.c", "3\n", "fix for later only\n\nCc: stable@vger.kernel.org # 1.1+")
        cls.fix = cls.commit("a.c", "4\n", f"fix base\n\nFixes: {cls.base[:12]} (\"base\")")
        cls.ported = cls.commit("a.c", "5\n", "fix ported\n\nCc: stable@vger.kernel.org")
        g("tag", "v1.1")
        g("checkout", "-q", "-b", "stable-1.0", "v1.0")
        cls.commit("b.c", "x\n", f"fix ported\n\ncommit {cls.ported} upstream.")
        g("tag", "v1.0.1")
        g("checkout", "-q", "master")

    @classmethod
    def git(cls, *args: str) -> str:
        return subprocess.run(["git", "-C", str(cls.repo), *args], check=True, capture_output=True, text=True, env=cls.env).stdout

    @classmethod
    def commit(cls, path: str, content: str, msg: str) -> str:
        (cls.repo / path).write_text(content)
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", msg)
        return cls.git("rev-parse", "HEAD").strip()

    @classmethod
    def tearDownClass(cls) -> None:
        release_cache_scope(cls.cache.name)
        cls.tmp.cleanup()
        cls.cache.cleanup()

    def test_report(self) -> None:
        with patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": self.cache.name, "KERNELINFO_CACHE_ENGINE": self.CACHE_ENGINE}), \
                patch.object(rs, "get_repo_dir", lambda: self.repo), patch.object(cr, "get_repo_dir", lambda: self.repo), \
                patch.object(rs, "jobs", JobManager(workers=1)), patch.object(cr, "jobs", JobManager(workers=1)):
            rs._TAGS_CACHE.clear()
            rs.release_index.build()
            cr.relation_index.build()
            r = ss.build_report("v1.0")
            self.assertTrue(r["available"])
            shas = {c["sha"]: c for c in r["candidates"]}
            self.assertIn(self.cc, shas)                     # Cc: stable # 1.0+ without backport
            self.assertNotIn(self.cc_other, shas)            # annotation excludes 1.0
            self.assertIn(self.fix, shas)                    # fixes a commit v1.0 contains
            self.assertEqual(shas[self.fix]["fixes"][0]["sha"], self.base)
            self.assertNotIn(self.ported, shas)              # backported in v1.0.1
            self.assertEqual(shas[self.cc]["mainline_release"], "v1.1")
            self.assertFalse(ss.build_report("v1.1")["available"])  # no stable tags of 1.1.y
            self.assertEqual([s["series"] for s in ss.stable_series()], ["v1.0"])


class TestScopedGitAndStableDeltas(unittest.TestCase):
    """Scoped Git fallback for unparsed versions, bugfixes, and stable_summary delta aggregation."""

    def test_scoped_git_fallback_avoids_index_build_and_matches_sqlite(self) -> None:
        from tests.db_fixtures import make_db, patch_reader
        from tests.test_webapp_stable import TestMissingReport as T
        T.setUpClass()
        try:
            empty_db = make_db("mock")
            with patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": T.cache.name, "KERNELINFO_CACHE_ENGINE": "ram"}), \
                    patch_reader(empty_db), \
                    patch.object(rs, "get_repo_dir", lambda: T.repo), \
                    patch.object(cr, "get_repo_dir", lambda: T.repo), \
                    patch("webapp.backend.database.helpers.get_active_repo_id", lambda cur: -1), \
                    patch.object(ss, "jobs", JobManager(workers=1)), \
                    patch.object(rs, "jobs", JobManager(workers=1)), \
                    patch.object(cr, "jobs", JobManager(workers=1)):
                rs._TAGS_CACHE.clear()
                cr._DB_READY.clear()
                # Without building relation_index or release_index, missing_report runs scoped Git without triggering full builds!
                with patch.object(rs.release_index, "build", side_effect=AssertionError("release_index should not build")), \
                        patch.object(cr.relation_index, "build", side_effect=AssertionError("relation_index should not build")):
                    st = ss.missing_report("v1.0", wait=10.0)
                    self.assertEqual(st["state"], "done")
                    res = st["result"]
                    self.assertTrue(res["available"])
                    self.assertEqual(res["source"], "git")
                    self.assertIn("summary", res)
                    shas = {c["sha"]: c for c in res["candidates"]}
                    self.assertIn(T.cc, shas)
                    self.assertIn(T.fix, shas)
                    self.assertNotIn(T.cc_other, shas)
                    self.assertNotIn(T.ported, shas)
                    self.assertEqual(shas[T.cc]["mainline_release"], "v1.1")
                    self.assertEqual(shas[T.fix]["fixes"][0]["sha"], T.base)

                    # Unavailable series returns immediately without starting index builds
                    unavail = ss.missing_report("v1.1", wait=0.0)
                    self.assertEqual(unavail["state"], "done")
                    self.assertFalse(unavail["result"]["available"])
        finally:
            T.tearDownClass()

    def test_series_scope_ignores_next_and_prefixed_zero_padded_series(self) -> None:
        tag_map = {
            "v3.0": "1" * 40,
            "v3.0.1": "2" * 40,
            "v3.1": "3" * 40,
            "next-20261110": "4" * 40,
        }
        dates = {"v3.0": 100, "v3.0.1": 200, "v3.1": 150, "next-20261110": 180}
        with patch.object(rs, "tag_commits", return_value=tag_map), \
                patch.object(rs, "tag_dates", return_value=dates):
            scope = ss.series_scope("v3.0")
            self.assertEqual(scope["later"], {"v3.1"})
            self.assertEqual([s["series"] for s in ss.stable_series()], ["v3.0"])

        pref_tags = {"man-pages-6.04": "a" * 40, "man-pages-6.04.01": "b" * 40}
        with patch.object(rs, "tag_commits", return_value=pref_tags):
            self.assertEqual(
                ss.stable_series(),
                [{"series": "man-pages-6.04", "stable_tags": 1, "last": "man-pages-6.04.01"}],
            )

    def test_failed_and_refreshing_index_states_and_db_failure(self) -> None:
        scope = {"available": True, "stable_tags": ["v1.0.1"], "last_stable": "v1.0.1", "later": {"v1.1"}}
        with patch.object(ss, "series_scope", return_value=scope), \
                patch.object(ss, "db_scope_covered", return_value=False), \
                patch.object(ss, "_indexes_ready", return_value=True), \
                patch.object(rs, "ensure_release_index", return_value={"state": "done"}), \
                patch.object(cr, "ensure_index", return_value={"state": "failed", "error": "boom"}):
            st = ss.missing_report("v1.0")
            self.assertEqual(st["state"], "failed")
            self.assertEqual(st["error"], "boom")

        with patch.object(ss, "series_scope", return_value=scope), \
                patch.object(ss, "_report_db", return_value=None), \
                self.assertRaises(RuntimeError):
            ss.build_report("v1.0", use_db=True)

    def test_tags_ingested_without_db_covers_all(self) -> None:
        from contextlib import ExitStack

        from tests.test_commit_relations import Store
        from tests.test_webapp_stable import TestMissingReport as T
        T.setUpClass()
        try:
            store = Store(T.repo)
            with ExitStack() as stack:
                for cm in (
                    patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": T.cache.name, "KERNELINFO_CACHE_ENGINE": "ram"}),
                    patch.object(rs, "get_repo_dir", lambda: T.repo),
                    patch.object(cr, "get_repo_dir", lambda: T.repo),
                    patch.object(rs, "db_covers_all", return_value=False),
                    *store.patches(),
                ):
                    stack.enter_context(cm)
                rs._TAGS_CACHE.clear()
                cr._DB_READY.clear()
                # Even with db_covers_all() False (e.g. an unrelated future tag is unparsed),
                # tags_ingested and db_scope_covered still return True for the parsed series!
                self.assertTrue(cr.tags_ingested(["v1.0.1", "v1.1"]))
                scope = ss.series_scope("v1.0")
                self.assertTrue(ss.db_scope_covered(scope))
        finally:
            cr._DB_READY.clear()
            T.tearDownClass()

    def test_stable_summary_delta_aggregation_boundary_fallback_and_endpoint(self) -> None:
        from fastapi.testclient import TestClient

        from core import DBLayout as D
        from parser.git_ast.release_order import series_ord_for, series_track_for
        from tests.db_fixtures import make_db, patch_reader
        from webapp.backend.app import create_app

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        env = {**os.environ, "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "A",
               "GIT_COMMITTER_EMAIL": "a@x", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

        def g(*args: str) -> str:
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout

        def commit(path: str, content: str, msg: str) -> str:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(content)
            g("add", "-A")
            g("commit", "-q", "-m", msg)
            return g("rev-parse", "HEAD").strip()

        maint = "FOO\nM:\tA <a@x>\nS:\tMaintained\nF:\ta.c\n\nTHE REST\nF:\t*\n"
        g("init", "-q", "-b", "master")
        base = commit("MAINTAINERS", maint, "init")
        base_a = commit("a.c", "1\n", "base a")
        g("tag", "v3.0")
        commit("a.c", "2\n", f"stable fix 1\n\nFixes: {base_a[:10]} (\"base a\")\nCc: stable@vger.kernel.org\ncommit {base} upstream.")
        g("tag", "v3.0.1")
        commit("a.c", "3\n4\n", f"stable fix 2\n\nFixes: {base_a[:10]} (\"base a\")\ncommit {base_a} upstream.")
        g("tag", "v3.0.2")

        db = make_db("sqlite", tables=[D.insights_stable_version_delta])
        track = series_track_for("v3.0.1")
        # Only v3.0.1 is pre-parsed in insights_stable_version_delta; v3.0.2 is an unparsed Git boundary!
        db.insert_rows(
            "insights_stable_version_delta",
            [
                "version_tag", "subsystem_name", "parent_tag", "base_mainline_tag", "series_track", "series_ord",
                "release_ts", "total_commits", "backport_commits", "fixes_tag_commits", "cc_stable_commits",
                "cve_linked_commits", "lines_added", "lines_removed", "same_cycle_fixes_count",
                "cross_release_fixes_count", "sum_fix_latency_days", "resolved_fixes_count",
            ],
            [
                ("v3.0.1", "", "v3.0", "v3.0", track, series_ord_for("v3.0.1"), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0),
                ("v3.0.1", "FOO", "v3.0", "v3.0", track, series_ord_for("v3.0.1"), 0, 5, 4, 2, 1, 1, 20, 5, 1, 1, 6.0, 2),
            ],
        )

        with patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": tmp.name, "KERNELINFO_CACHE_ENGINE": "ram"}), \
                patch_reader(db), \
                patch.object(rs, "get_repo_dir", lambda: repo), \
                patch("webapp.backend.services.insights_service.get_repo_dir", lambda: repo), \
                patch("webapp.backend.database.helpers.get_active_repo_id", lambda cur: 1), \
                patch("webapp.backend.config.repo_feature_enabled", return_value=True):
            rs._TAGS_CACHE.clear()
            self.addCleanup(rs._TAGS_CACHE.clear)

            # v3.0.1 only -> pure database
            db_only = ss.stable_summary("v3.0", to_ref="v3.0.1")
            self.assertEqual(db_only["source"], "database")
            self.assertEqual(db_only["commits"], 5)
            self.assertEqual(db_only["avg_fix_latency_days"], 3.0)

            # Full series v3.0.1 (DB) + v3.0.2 (Git boundary) -> mixed with weighted latency
            fake_delta = {
                "stable_deltas": [{
                    "subsystem_name": "FOO",
                    "total_commits": 1, "backport_commits": 1, "fixes_tag_commits": 1, "cc_stable_commits": 0,
                    "cve_linked_commits": 0, "lines_added": 2, "lines_removed": 1,
                    "same_cycle_fixes_count": 0, "cross_release_fixes_count": 1,
                    "sum_fix_latency_days": 9.0, "resolved_fixes_count": 1,
                }]
            }
            with patch.object(ss, "extract_release_deltas_from_git", return_value=fake_delta) as m_ext:
                mixed = ss.stable_summary("v3.0")
            m_ext.assert_called_once()
            self.assertEqual(mixed["source"], "mixed")
            self.assertEqual(mixed["db_releases"], 1)
            self.assertEqual(mixed["git_releases"], 1)
            self.assertEqual(mixed["commits"], 6)
            self.assertEqual(mixed["backports"], 5)
            self.assertEqual(mixed["fixes"], 3)
            self.assertEqual(mixed["cc_stable"], 1)
            self.assertEqual(mixed["cve_commits"], 1)
            self.assertEqual(mixed["lines_added"], 22)
            self.assertEqual(mixed["lines_removed"], 6)
            self.assertEqual(mixed["same_cycle_fixes"], 1)
            self.assertEqual(mixed["cross_release_fixes"], 2)
            self.assertEqual(mixed["avg_fix_latency_days"], 5.0)  # (6.0 + 9.0) / (2 + 1)

            # HTTP endpoint
            client = TestClient(create_app())
            resp = client.get("/api/insights/v3.0/stable/summary?series=v3.0&from_ref=v3.0&to_ref=v3.0.1")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["commits"], 5)
            bad = client.get("/api/insights/v3.0/stable/summary?series=v3.0&from_ref=v3.0.2&to_ref=v3.0.1")
            self.assertEqual(bad.status_code, 400)



class TestMissingReportSQLite(TestMissingReport):
    CACHE_ENGINE = "sqlite"


if __name__ == "__main__":
    unittest.main()
