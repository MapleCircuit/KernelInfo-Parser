"""tests/test_webapp_patch_series.py - Multi-file patch series formatter (P3, webapp/API.md section 4.6)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from webapp.backend.services import patch_series as ps
from webapp.backend.services.patch_apply import apply_series, parse_patch

FILES = {"lib/a.c": "".join(f"int a{i};\n" for i in range(20)), "lib/b.h": "#define B 1\n#define C 2"}
AUTHOR = {"name": "Ada Dev", "email": "ada@example.org"}


def read(sha: str, path: str):
    text = FILES.get(path)
    return (text, ps.blob_sha(text) if text is not None else None, "100644")


def request(**extra):
    a = FILES["lib/a.c"].replace("int a5;\n", "int a5 = 1;\n")
    b = FILES["lib/b.h"].replace("C 2", "C 3")
    req = {"author": AUTHOR, "to": ["Maint Ainer <m@example.org>"], "cc": ["list@vger.kernel.org"],
           "cover": {"subject": "lib: two fixes", "body": "Two small fixes."},
           "patches": [{"subject": "lib: init a5", "body": "Initialize it.", "files": [{"path": "lib/a.c", "original_blob": ps.blob_sha(FILES["lib/a.c"]), "modified_content": a}]},
                       {"subject": "lib: bump C", "body": "", "files": [{"path": "lib/b.h", "original_blob": ps.blob_sha(FILES["lib/b.h"]), "modified_content": b},
                                                                       {"path": "lib/new.c", "original_blob": "", "modified_content": "new\n"}]}]}
    req.update(extra)
    return req


def fmt(req):
    return ps.PatchSeriesFormatter(read, now=datetime(2026, 11, 5, tzinfo=timezone.utc)).format("f" * 40, req)


class TestPatchSeries(unittest.TestCase):
    def test_format(self) -> None:
        res = fmt(request())
        names = [p["name"] for p in res["patches"]]
        self.assertEqual(names, ["0000-cover-letter.patch", "0001-lib-init-a5.patch", "0002-lib-bump-c.patch"])
        cover, one, two = (p["text"] for p in res["patches"])
        self.assertIn("Subject: [PATCH 0/2] lib: two fixes", cover)
        self.assertIn("Ada Dev (2):\n  lib: init a5\n  lib: bump C\n", cover)
        self.assertIn("base-commit: " + "f" * 40, cover)
        self.assertIn("Subject: [PATCH 1/2] lib: init a5", one)
        self.assertIn("To: Maint Ainer <m@example.org>\nCc: list@vger.kernel.org\n", one)
        self.assertIn("Signed-off-by: Ada Dev <ada@example.org>\n---\n lib/a.c | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n", one)
        self.assertIn(f"index {ps.blob_sha(FILES['lib/a.c'])[:7]}..", one)
        self.assertIn("\\ No newline at end of file", two)
        self.assertIn("new file mode 100644\nindex 0000000..", two)
        self.assertIn(" create mode 100644 lib/new.c\n", two)
        self.assertTrue(res["mbox"].startswith("From " + "0" * 40))
        # The series applies to its base in memory
        check = apply_series("x", [(p["name"], parse_patch(p["text"])) for p in res["patches"][1:]], FILES.get)
        self.assertEqual([p["status"] for p in check["patches"]], ["applied", "applied"])
        self.assertEqual(check["files"]["lib/b.h"], "#define B 1\n#define C 3")

    def test_single_patch_has_no_cover_and_base_in_first(self) -> None:
        req = request()
        req["patches"] = req["patches"][:1]
        res = fmt(req)
        self.assertEqual([p["name"] for p in res["patches"]], ["0001-lib-init-a5.patch"])
        self.assertIn("Subject: [PATCH] lib: init a5", res["patches"][0]["text"])
        self.assertIn("base-commit: ", res["patches"][0]["text"])

    def test_rejections(self) -> None:
        stale = request()
        stale["patches"][0]["files"][0]["original_blob"] = "1" * 40
        cases = [(stale, 409), (request(to=["not an address"]), 400), (request(author={"name": "", "email": "x@y.z"}), 400)]
        jail = request()
        jail["patches"][0]["files"][0]["path"] = "../etc/passwd"
        cases.append((jail, 400))
        dup = request()
        dup["patches"][1]["files"][0]["path"] = "lib/a.c"
        cases.append((dup, 400))
        for req, code in cases:
            with self.assertRaises(HTTPException) as cm:
                fmt(req)
            self.assertEqual(cm.exception.status_code, code)

    def test_git_am_accepts_the_mbox(self) -> None:
        git = shutil.which("git")
        if not git:
            self.skipTest("git is not installed")
        tmp = tempfile.mkdtemp(prefix="kip-series-")
        try:
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.t"}
            for path, text in FILES.items():
                os.makedirs(os.path.join(tmp, os.path.dirname(path)), exist_ok=True)
                with open(os.path.join(tmp, path), "w") as fh:
                    fh.write(text)
            for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "base"]):
                subprocess.run([git, *cmd], cwd=tmp, check=True, env=env)
            res = fmt(request())
            mbox = os.path.join(tmp, "series.mbox")
            with open(mbox, "w") as fh:
                fh.write("".join(p["text"] for p in res["patches"][1:]))
            am = subprocess.run([git, "am", "-q", mbox], cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(am.returncode, 0, am.stderr)
            log = subprocess.run([git, "log", "--format=%an|%s", "-2"], cwd=tmp, capture_output=True, text=True).stdout.split("\n")
            self.assertEqual(log[:2], ["Ada Dev|lib: bump C", "Ada Dev|lib: init a5"])
            with open(os.path.join(tmp, "lib/new.c")) as fh:
                self.assertEqual(fh.read(), "new\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mboxrd_from_escaping(self) -> None:
        """Body lines starting with 'From ' or '>From ' are escaped in mboxrd format."""
        req = request()
        req["patches"][0]["body"] = "From Bob:\n>From Alice:\nRegular line."
        res = fmt(req)
        p_text = res["patches"][1]["text"]
        self.assertIn(">From Bob:\n", p_text)
        self.assertIn(">>From Alice:\n", p_text)
        self.assertIn("Regular line.", p_text)


if __name__ == "__main__":
    unittest.main()
