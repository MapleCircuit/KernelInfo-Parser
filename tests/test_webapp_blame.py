"""tests/test_webapp_blame.py - git blame options and porcelain fields (webapp/API.md section 4.6)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services import git_service as gs_mod
from db_engine import MockDB
from tests.db_fixtures import patch_reader
from webapp.backend.services.git_service import GitService

PORCELAIN = """\
1111111111111111111111111111111111111111 3 1 2
author Ann
author-mail <ann@example.org>
author-time 1700000000
summary second change
previous 2222222222222222222222222222222222222222 old/name.c
filename new/name.c
\tint a;
1111111111111111111111111111111111111111 4 2
filename new/name.c
\tint b;
3333333333333333333333333333333333333333 1 3 1
author Bob
author-mail <bob@example.org>
author-time 1600000000
summary initial
boundary
filename new/name.c
\tint c;
"""


class TestBlameParsing(unittest.TestCase):
    def test_porcelain_fields(self) -> None:
        lines = GitService.parse_blame_porcelain(PORCELAIN)
        self.assertEqual([x["line"] for x in lines], [1, 2, 3])
        self.assertEqual([x["orig_line"] for x in lines], [3, 4, 1])
        first = lines[0]
        self.assertEqual((first["author_name"], first["author_email"], first["subject"]), ("Ann", "ann@example.org", "second change"))
        self.assertEqual((first["previous_sha"], first["previous_path"]), ("2" * 40, "old/name.c"))
        self.assertEqual(first["source_path"], "new/name.c")
        # Later lines of the same commit reuse its author and summary
        self.assertEqual(lines[1]["author_name"], "Ann")
        self.assertEqual(lines[1]["previous_sha"], "2" * 40)
        # A root commit has no previous commit
        self.assertNotIn("previous_sha", lines[2])

    def test_flag_mapping(self) -> None:
        self.assertEqual(GitService.blame_flags(), [])
        self.assertEqual(GitService.blame_flags(move=True, copy=True, ignore_ws=True), ["-M", "-C", "-w"])
        self.assertEqual(GitService.blame_flags(ignore_ws=True), ["-w"])


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestBlameRepository(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        repo = Path(cls.tmp.name)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

        def run(*a: str) -> str:
            return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env=env).stdout

        run("init", "-q")
        body = "".join(f"\tvalue_{i} = compute_something_long({i}, {i * 7});\n" for i in range(12))
        func = f"static int helper(void)\n{{\n{body}\treturn 0;\n}}\n"
        (repo / "a.c").write_text("/* a */\n" + func)
        (repo / "b.c").write_text("/* b */\nint b;\n")
        run("add", "-A")
        run("commit", "-q", "-m", "add helper in a.c")
        cls.first = run("rev-parse", "HEAD").strip()
        # The function moves from a.c to b.c in one commit
        (repo / "a.c").write_text("/* a */\n")
        (repo / "b.c").write_text("/* b */\nint b;\n" + func)
        run("commit", "-q", "-am", "move helper to b.c")
        cls.second = run("rev-parse", "HEAD").strip()
        run("tag", "v1")
        cls.repo = repo

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    @contextmanager
    def service(self):
        with patch.object(gs_mod, "get_repo_dir", lambda: self.repo), \
                patch("webapp.backend.security.jail.get_repo_dir", lambda: self.repo), \
                patch_reader(MockDB(use_global=False)), \
                patch.object(gs_mod, "get_version_info", lambda reader, v: (0, v)):
            yield GitService()

    def test_copy_follows_the_moved_function(self) -> None:
        with self.service() as svc:
            plain = svc.get_blame("v1", "b.c")
            followed = svc.get_blame("v1", "b.c", copy=True)
        line = 5  # inside the moved function
        self.assertEqual(plain["lines"][line - 1]["commit_hash"], self.second)
        self.assertEqual(followed["lines"][line - 1]["commit_hash"], self.first)
        self.assertEqual(followed["lines"][line - 1]["source_path"], "a.c")
        self.assertEqual(followed["options"], {"move": False, "copy": True, "ignore_ws": False})
        self.assertIn("slow", followed)
        # The line of the second commit points at its parent for "blame before this change"
        moved = plain["lines"][line - 1]
        self.assertEqual(moved["previous_sha"], self.first)
        self.assertEqual(moved["previous_path"], "b.c")

    def test_rejects_unsafe_revision(self) -> None:
        from fastapi import HTTPException
        with self.service() as svc, self.assertRaises(HTTPException):
            svc.get_blame("--output=/tmp/x", "b.c")


if __name__ == "__main__":
    unittest.main()
