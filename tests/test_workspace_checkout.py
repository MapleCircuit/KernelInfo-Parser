"""tests/test_workspace_checkout.py - Incremental RAMDISK workspace checkout (MasterFile.checkout_version)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.FileHandler import MasterFile, OWNER_FILE


def _git(repo: str, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True, env=env).stdout


class TestWorkspaceCheckout(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="kip-test-ws-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "d"))
        _git(self.repo, "init", "-q")
        Path(self.repo, "a.txt").write_text("alpha\n")
        Path(self.repo, "d", "x.c").write_text("int x;\n")
        os.symlink("a.txt", os.path.join(self.repo, "link"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "one")
        _git(self.repo, "tag", "v1")
        # v2: a.txt modified, link retargeted to a new file, d/x.c deleted, an executable added
        Path(self.repo, "a.txt").write_text("alpha 2\n")
        Path(self.repo, "b.txt").write_text("beta\n")
        os.unlink(os.path.join(self.repo, "link"))
        os.symlink("b.txt", os.path.join(self.repo, "link"))
        os.unlink(os.path.join(self.repo, "d", "x.c"))
        Path(self.repo, "run.sh").write_text("#!/bin/sh\n")
        os.chmod(os.path.join(self.repo, "run.sh"), 0o755)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "two")
        _git(self.repo, "tag", "v2")

        self.mf = MasterFile()
        self.mf.set_repo_dir(self.repo)
        self.purge: list[str] = []

    def tearDown(self) -> None:
        self.mf.clear_all_version()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_incremental_checkout_matches_a_full_extraction(self) -> None:
        first = self.mf.checkout_version("v1", None, self.purge)
        self.assertTrue(os.path.isfile(os.path.join(first, OWNER_FILE)))
        second = self.mf.checkout_version("v2", "v1", self.purge)
        self.assertEqual(first, second, "the workspace is updated in place")
        self.assertEqual(list(self.mf.version_dict), ["v2"], "the previous version no longer maps to the workspace")

        ws = Path(second)
        # Writing through the old symlink would have overwritten a.txt with the new link target
        self.assertEqual((ws / "a.txt").read_text(), "alpha 2\n")
        self.assertTrue((ws / "link").is_symlink())
        self.assertEqual(os.readlink(ws / "link"), "b.txt")
        self.assertFalse((ws / "d").exists(), "emptied directories are removed like in a fresh extraction")
        self.assertTrue(os.access(ws / "run.sh", os.X_OK))

        fresh = self.mf.git_clone("v2")
        try:
            res = subprocess.run(
                ["diff", "-r", "--no-dereference", "-x", OWNER_FILE, second, fresh], capture_output=True, text=True
            )
            self.assertEqual(res.returncode, 0, res.stdout)
        finally:
            shutil.rmtree(fresh, ignore_errors=True)
        # A read of the previous version goes to git, not to the updated workspace
        self.assertEqual(self.mf.get_file("a.txt", "v1"), "alpha\n")

    def test_unrelated_base_extracts_in_full(self) -> None:
        first = self.mf.checkout_version("v2", None, self.purge)
        again = self.mf.checkout_version("v1", "v0-missing", self.purge)
        self.assertNotEqual(first, again)
        self.assertFalse(os.path.exists(first))
        self.assertEqual(Path(again, "a.txt").read_text(), "alpha\n")


if __name__ == "__main__":
    unittest.main()
