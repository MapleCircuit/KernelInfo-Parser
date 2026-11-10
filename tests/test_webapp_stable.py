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

from webapp.backend.services import commit_relations as cr
from webapp.backend.services import release_service as rs
from webapp.backend.services import stable_service as ss
from webapp.backend.services.jobs import JobManager


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
        cls.tmp.cleanup()
        cls.cache.cleanup()

    def test_report(self) -> None:
        with patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": self.cache.name}), \
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


if __name__ == "__main__":
    unittest.main()
