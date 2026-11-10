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
        for kwargs in (dict(author="x" * 101), dict(author="a\nb"), dict(since="2011-1-1"), dict(until="2011-02-30"),
                       dict(roles=["boss:x"]), dict(paths=["../etc"]), dict(range_="--output=x..v1"), dict(range_="v1"),
                       dict(merges="maybe")):
            with self.assertRaises(HTTPException, msg=kwargs):
                with patch.object(cq, "_resolve", return_value="a" * 40):
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


if __name__ == "__main__":
    unittest.main()
