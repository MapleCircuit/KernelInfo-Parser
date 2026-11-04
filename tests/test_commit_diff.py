"""tests/test_commit_diff.py - Structured commit diff parsing, the git-backed commit diff service and
the any-file blob diff of the code view ("⇄ Diff")."""
from __future__ import annotations

import subprocess
import unittest

from fastapi import HTTPException

from core.config import get_default_repo_path
from webapp.backend.services.git_service import GitService, parse_unified_diff
from webapp.backend.services.diff_service import DiffService

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

    def test_line_clipping(self):
        long = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-" + "a" * 500 + "\n+b\n"
        self.assertEqual(len(parse_unified_diff(long)[0]["hunks"][0]["lines"][0][3]), 400)
        self.assertEqual(len(parse_unified_diff(long, max_chars=None)[0]["hunks"][0]["lines"][0][3]), 500)


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


class TestBlobDiffService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["git", "-C", LINUX_REPO, "rev-parse", "v3.1^{commit}"], capture_output=True, text=True)
        if out.returncode != 0:
            raise unittest.SkipTest("Linux repository with tag v3.1 not available")

    def test_same_file_two_revisions(self):
        diff = DiffService().get_blob_diff("v3.1-rc10", "Makefile", "v3.1", "Makefile")
        self.assertFalse(diff["identical"] or diff["binary"] or diff["truncated"])
        self.assertEqual((diff["additions"], diff["deletions"]), (1, 1))
        self.assertEqual(diff["hunks"][0]["lines"], [
            ["-", 4, None, "EXTRAVERSION = -rc10"],
            ["+", None, 4, "EXTRAVERSION ="],
        ])

    def test_two_different_files_and_identical(self):
        diff = DiffService().get_blob_diff("v3.0", "kernel/sched.c", "v3.0", "kernel/sched_fair.c")
        self.assertNotEqual(diff["blob_a"], diff["blob_b"])
        self.assertGreater(len(diff["hunks"]), 0)
        # -U0: hunks carry changed lines only
        self.assertTrue(all(r[0] in "+-" for h in diff["hunks"] for r in h["lines"]))
        same = DiffService().get_blob_diff("v3.0", "Makefile", "v3.0", "Makefile")
        self.assertTrue(same["identical"])
        self.assertEqual(same["hunks"], [])

    def test_invalid_paths_and_refs(self):
        cases = [
            (("v3.0", "../etc/passwd", "v3.0", "Makefile"), 400),
            (("v3.0", "kernel", "v3.0", "Makefile"), 400),
            (("v3.0", "no/such/file.c", "v3.0", "Makefile"), 404),
            (("--output=x", "Makefile", "v3.0", "Makefile"), 404),
        ]
        for args, status in cases:
            with self.assertRaises(HTTPException) as err:
                DiffService().get_blob_diff(*args)
            self.assertEqual(err.exception.status_code, status, args)


if __name__ == "__main__":
    unittest.main()
