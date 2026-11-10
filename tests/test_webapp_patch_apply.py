"""tests/test_webapp_patch_apply.py - In-memory patch applier (P1, webapp/API.md section 4.8)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services import patch_apply as pa

BASE = "".join(f"line {i}\n" for i in range(1, 41))
FILES = {"lib/a.c": BASE, "lib/old.c": "keep\nme\n"}

MAIL = """From: Ada Dev <ada@example.org>
Subject: [PATCH 1/2] lib: change line 10
Date: Thu, 5 Nov 2026 10:00:00 +0000
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: quoted-printable

Change it.

Signed-off-by: Ada Dev <ada@example.org>
---
 lib/a.c | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)

diff --git a/lib/a.c b/lib/a.c
index 1111111..2222222 100644
--- a/lib/a.c
+++ b/lib/a.c
@@ -7,7 +7,7 @@
 line 7
 line 8
 line 9
-line 10
+line 10 =E2=9C=93
 line 11
 line 12
 line 13
--=20
2.43.0

base-commit: 0123456789abcdef0123456789abcdef01234567
"""


def hunk_patch(path: str, start: int, before: list[str], minus: str, plus: str, after: list[str]) -> str:
    body = "".join(f" {x}\n" for x in before) + f"-{minus}\n+{plus}\n" + "".join(f" {x}\n" for x in after)
    n = len(before) + len(after) + 1
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -{start},{n} +{start},{n} @@\n{body}"


def run(patches: list[str], files: dict[str, str] = FILES) -> dict:
    return pa.apply_series("sha", [(f"p{i}", pa.parse_patch(p)) for i, p in enumerate(patches)], files.get)


class TestPatchApply(unittest.TestCase):
    def test_extract_decodes_mail(self) -> None:
        got = pa.extract_patch(MAIL)
        self.assertEqual(got["subject"], "[PATCH 1/2] lib: change line 10")
        self.assertEqual(got["base_commit"], "0123456789abcdef0123456789abcdef01234567")
        self.assertTrue(got["diff"].startswith("diff --git a/lib/a.c"))
        res = run([got["diff"]])
        self.assertEqual(res["patches"][0]["status"], "applied")
        self.assertIn("line 10 ✓\n", res["files"]["lib/a.c"])

    def test_exact_offset_and_failure(self) -> None:
        exact = hunk_patch("lib/a.c", 9, ["line 9"], "line 10", "LINE 10", ["line 11"])
        self.assertEqual(run([exact])["patches"][0]["files"][0]["hunks"], [{"status": "applied", "line": 9, "offset": 0}])
        shifted = hunk_patch("lib/a.c", 4, ["line 9"], "line 10", "LINE 10", ["line 11"])
        res = run([shifted])["patches"][0]["files"][0]
        self.assertEqual((res["status"], res["hunks"][0]["offset"]), ("offset", 5))
        bad = hunk_patch("lib/a.c", 9, ["line 9"], "line 99", "x", ["line 11"])
        res = run([bad])
        self.assertEqual(res["patches"][0]["status"], "failed")
        self.assertEqual(res["patches"][0]["files"][0]["hunks"][0]["nearest"]["line"], 9)
        self.assertEqual(res["files"], {})

    def test_create_delete_rename_binary(self) -> None:
        create = "diff --git a/lib/new.c b/lib/new.c\nnew file mode 100644\n--- /dev/null\n+++ b/lib/new.c\n@@ -0,0 +1,2 @@\n+one\n+two\n"
        delete = "diff --git a/lib/old.c b/lib/old.c\ndeleted file mode 100644\n--- a/lib/old.c\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-keep\n-me\n"
        rename = "diff --git a/lib/old.c b/lib/moved.c\nsimilarity index 100%\nrename from lib/old.c\nrename to lib/moved.c\n"
        binary = "diff --git a/fw.bin b/fw.bin\nindex 1..2 100644\nBinary files a/fw.bin and b/fw.bin differ\n"
        res = run([create, delete])
        self.assertEqual([p["files"][0]["status"] for p in res["patches"]], ["created", "deleted"])
        self.assertEqual(res["files"], {"lib/new.c": "one\ntwo\n", "lib/old.c": None})
        res = run([rename])
        self.assertEqual(res["patches"][0]["files"][0]["status"], "renamed")
        self.assertEqual(res["files"], {"lib/old.c": None, "lib/moved.c": "keep\nme\n"})
        self.assertEqual(run([binary])["patches"][0]["files"][0]["status"], "unsupported")
        self.assertEqual(run([create, create])["patches"][1]["files"][0]["reason"], "already exists")

    def test_series_dependency_and_no_newline(self) -> None:
        first = hunk_patch("lib/a.c", 9, ["line 9"], "line 10", "LINE 10", ["line 11"])
        second = hunk_patch("lib/a.c", 9, ["line 9"], "LINE 10", "Line 10", ["line 11"])
        res = run([first, second])
        self.assertEqual([p["status"] for p in res["patches"]], ["applied", "applied"])
        self.assertIn("Line 10\n", res["files"]["lib/a.c"])
        self.assertEqual(run([second])["patches"][0]["status"], "failed")
        eof = "diff --git a/lib/old.c b/lib/old.c\n--- a/lib/old.c\n+++ b/lib/old.c\n@@ -1,2 +1,2 @@\n keep\n-me\n+you\n\\ No newline at end of file\n"
        self.assertEqual(run([eof])["files"]["lib/old.c"], "keep\nyou")

    def test_path_jail(self) -> None:
        for path in ("../etc/passwd", "/etc/passwd", ".git/config", "a//b", "a\\b"):
            self.assertFalse(pa.safe_path(path), path)
        evil = "--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n"
        res = run([evil])
        self.assertEqual((res["patches"][0]["files"][0]["status"], res["patches"][0]["files"][0]["reason"]), ("failed", "path outside the tree"))

    def test_diff_hunks_shape(self) -> None:
        h = pa.diff_hunks("a\nb\nc\n", "a\nB\nc\nd\n")
        self.assertEqual([r for x in h for r in x["lines"]], [["-", 2, None, "b"], ["+", None, 2, "B"], ["+", None, 4, "d"]])
        self.assertEqual(pa.apply_id("s", ["m1"]), pa.apply_id("s", ["m1"]))

    def test_parity_with_git_apply_check(self) -> None:
        git = shutil.which("git")
        if not git:
            self.skipTest("git is not installed")
        cases = [
            hunk_patch("lib/a.c", 9, ["line 7", "line 8", "line 9"], "line 10", "X", ["line 11", "line 12", "line 13"]),
            hunk_patch("lib/a.c", 2, ["line 7", "line 8", "line 9"], "line 10", "X", ["line 11", "line 12", "line 13"]),
            hunk_patch("lib/a.c", 9, ["line 7", "line 8", "line 9"], "line 10", "X", ["line 11", "line 12", "nope"]),
            hunk_patch("lib/a.c", 36, ["line 37", "line 38", "line 39"], "line 40", "X", []),
            hunk_patch("lib/a.c", 30, ["line 37", "line 38", "line 39"], "line 40", "X", []),
            hunk_patch("lib/a.c", 1, [], "line 1", "X", ["line 2", "line 3", "line 4"]),
            hunk_patch("lib/a.c", 5, [], "line 2", "X", ["line 3", "line 4", "line 5"]),
        ]
        tmp = tempfile.mkdtemp(prefix="kip-apply-")
        try:
            for path, text in FILES.items():
                os.makedirs(os.path.join(tmp, os.path.dirname(path)), exist_ok=True)
                with open(os.path.join(tmp, path), "w") as fh:
                    fh.write(text)
            subprocess.run([git, "init", "-q"], cwd=tmp, check=True)
            for i, patch in enumerate(cases):
                with open(os.path.join(tmp, "p.diff"), "w") as fh:
                    fh.write(patch)
                ok_git = subprocess.run([git, "apply", "--check", "p.diff"], cwd=tmp, capture_output=True).returncode == 0
                ok_ours = run([patch])["patches"][0]["status"] == "applied"
                self.assertEqual(ok_ours, ok_git, f"case {i}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
