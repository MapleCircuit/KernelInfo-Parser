"""tests/test_commit_diff.py - Structured commit diff parsing and the git-backed commit diff service."""
from __future__ import annotations

import subprocess
import unittest

from fastapi import HTTPException

from core.config import get_default_repo_path
from webapp.backend.services.git_service import GitService, parse_unified_diff

LINUX_REPO = str(get_default_repo_path())

SAMPLE = """diff --git a/Makefile b/Makefile
index 1111111..2222222 100644
--- a/Makefile
+++ b/Makefile
@@ -2,4 +2,5 @@ VERSION = 3
 PATCHLEVEL = 1
-SUBLEVEL = 0
+SUBLEVEL = 1
+EXTRAVERSION =
 NAME = Sneaky Weasel
\\ No newline at end of file
diff --git a/old dir/a.c b/new dir/a.c
similarity index 90%
rename from old dir/a.c
rename to new dir/a.c
diff --git a/gone.c b/gone.c
deleted file mode 100644
--- a/gone.c
+++ /dev/null
@@ -1,2 +0,0 @@
-int x;
--- y;
diff --git a/logo.png b/logo.png
new file mode 100644
Binary files /dev/null and b/logo.png differ
"""


class TestParseUnifiedDiff(unittest.TestCase):
    def test_line_numbers_and_counts(self):
        files = parse_unified_diff(SAMPLE)
        self.assertEqual([f["status"] for f in files], ["M", "R", "D", "A"])
        mk = files[0]
        self.assertEqual((mk["additions"], mk["deletions"]), (2, 1))
        hunk = mk["hunks"][0]
        self.assertEqual((hunk["old_start"], hunk["new_start"], hunk["heading"]), (2, 2, "VERSION = 3"))
        self.assertEqual(hunk["lines"], [
            [" ", 2, 2, "PATCHLEVEL = 1"],
            ["-", 3, None, "SUBLEVEL = 0"],
            ["+", None, 3, "SUBLEVEL = 1"],
            ["+", None, 4, "EXTRAVERSION ="],
            [" ", 4, 5, "NAME = Sneaky Weasel"],
        ])

    def test_rename_delete_binary(self):
        _, ren, gone, png = parse_unified_diff(SAMPLE)
        self.assertEqual((ren["old_path"], ren["path"], ren["hunks"]), ("old dir/a.c", "new dir/a.c", []))
        # A removed line starting with "--" stays a deletion, not a file header
        self.assertEqual(gone["hunks"][0]["lines"], [["-", 1, None, "int x;"], ["-", 2, None, "-- y;"]])
        self.assertEqual(gone["path"], "gone.c")
        self.assertTrue(png["binary"])
        self.assertEqual(png["path"], "logo.png")

    def test_row_cap_keeps_exact_counts(self):
        files = parse_unified_diff(SAMPLE, max_lines=3)
        self.assertTrue(files[0]["truncated"])
        self.assertEqual(sum(len(h["lines"]) for f in files for h in f["hunks"]), 3)
        self.assertEqual((files[0]["additions"], files[2]["deletions"]), (2, 2))


class TestCommitDiffService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["git", "-C", LINUX_REPO, "rev-parse", "v3.1^{commit}"], capture_output=True, text=True)
        if out.returncode != 0:
            raise unittest.SkipTest("Linux repository with tag v3.1 not available")
        cls.sha = out.stdout.strip()

    def test_release_commit_diff(self):
        diff = GitService().get_commit_diff(self.sha[:12], context=0)
        self.assertEqual(diff["commit_hash"], self.sha)
        self.assertFalse(diff["is_merge"])
        self.assertEqual([f["path"] for f in diff["files"]], ["Makefile"])
        self.assertEqual(diff["files"][0]["hunks"][0]["lines"], [
            ["-", 4, None, "EXTRAVERSION = -rc10"],
            ["+", None, 4, "EXTRAVERSION ="],
        ])

    def test_invalid_and_unknown_refs(self):
        with self.assertRaises(HTTPException) as bad:
            GitService().get_commit_diff("HEAD~1")
        self.assertEqual(bad.exception.status_code, 400)
        with self.assertRaises(HTTPException) as missing:
            GitService().get_commit_diff("deadbeefdeadbeef")
        self.assertEqual(missing.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
