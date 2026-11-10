"""tests/test_webapp_grep.py - Bounded git grep over a revision (webapp/API.md section 2.1.3)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from webapp.backend.security.git import git_pathspecs, validate_grep_pattern, validate_pathspecs
from webapp.backend.services import grep_service as gs
from webapp.backend.services.grep_service import GrepService, clip_line, parse_colored_line
from webapp.backend.services.tree_resolver import GitTreeResolver


class TestGrepValidation(unittest.TestCase):
    def test_patterns(self) -> None:
        for ok in ("x", "-e", "--output=/tmp/x", "a" * 256, "SYSCALL_DEFINE3(read", "(a+)+$"):
            self.assertEqual(validate_grep_pattern(ok), ok)
        for bad in ("", "a" * 257, "a\nb", "a\0b", "a\rb", None, 5):
            with self.assertRaises(HTTPException) as ctx:
                validate_grep_pattern(bad)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_pathspecs(self) -> None:
        self.assertEqual(validate_pathspecs("fs/, *.c ,!drivers/staging"), ["fs/", "*.c", "!drivers/staging"])
        self.assertEqual(validate_pathspecs(None), [])
        for bad in ("../x", "/etc/passwd", "a/../b", ":(top)x", ":!x", "!", "!/x", "a\\b", "x\ny", "x" * 201):
            with self.assertRaises(HTTPException, msg=bad):
                validate_pathspecs([bad])
        with self.assertRaises(HTTPException):
            validate_pathspecs([f"d{i}" for i in range(9)])

    def test_server_side_magic(self) -> None:
        self.assertEqual(git_pathspecs(["*.c", "fs/", "drivers/**/*.h", "!arch/"]),
                         [":(glob)**/*.c", ":(glob)fs", ":(glob)drivers/**/*.h", ":(exclude,glob)arch"])
        self.assertEqual(git_pathspecs(["!drivers"]), [".", ":(exclude,glob)drivers"])


class TestGrepParsing(unittest.TestCase):
    def test_colored_ranges(self) -> None:
        text, ranges = parse_colored_line("a \x1b[1;31mfoo\x1b[m b \x1b[1;31mfoo\x1b[m")
        self.assertEqual(text, "a foo b foo")
        self.assertEqual(ranges, [[2, 5], [8, 11]])

    def test_utf8_and_tabs(self) -> None:
        text, ranges = parse_colored_line("\té\x1b[1;31mñx\x1b[m")
        self.assertEqual(text, "\téñx")
        self.assertEqual(ranges, [[2, 4]])
        self.assertEqual(text[2:4], "ñx")

    def test_unterminated_and_empty(self) -> None:
        self.assertEqual(parse_colored_line("x\x1b[31my"), ("xy", [[1, 2]]))
        self.assertEqual(parse_colored_line("x\x1b[31m\x1b[my"), ("xy", []))

    def test_clipping(self) -> None:
        line = "a" * 1000 + "MATCH" + "b" * 1000
        text, ranges, clipped = clip_line(line, [[1000, 1005]])
        self.assertTrue(clipped)
        self.assertEqual(len(text), gs.MAX_LINE_CHARS)
        self.assertEqual(text[ranges[0][0]:ranges[0][1]], "MATCH")
        self.assertEqual(clip_line("short", [[0, 1]]), ("short", [[0, 1]], False))


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestGrepIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        repo = Path(cls.tmp.name)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, env=env)  # noqa: E731
        run("init", "-q")
        (repo / "fs").mkdir()
        (repo / "drivers" / "staging").mkdir(parents=True)
        (repo / "fs" / "read_write.c").write_text("SYSCALL_DEFINE3(read, unsigned int, fd)\nfoo bar foo\nFOO\nfoobar\n")
        (repo / "drivers" / "x.c").write_text("foo\n" * 30)
        (repo / "drivers" / "staging" / "y.c").write_text("foo staging\n")
        (repo / "README").write_text("foo readme\n")
        (repo / "long.txt").write_text("z" * 800 + "foo\n")
        run("add", "-A")
        run("commit", "-q", "-m", "init")
        run("tag", "v1.0")
        cls.repo = repo

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.resolver = GitTreeResolver(self.repo)
        self.patches = [
            patch.object(gs, "git_tree_resolver", self.resolver),
            patch.object(gs, "get_repo_dir", lambda: self.repo),
        ]
        for p in self.patches:
            p.start()
        self.svc = GrepService()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def test_fixed_finds_exact_range(self) -> None:
        r = self.svc.grep("v1.0", "SYSCALL_DEFINE3(read", mode="fixed")
        self.assertEqual(r["total"], 1)
        m = r["matches"][0]
        self.assertEqual((m["path"], m["line"], m["ranges"]), ("fs/read_write.c", 1, [[0, 20]]))
        self.assertFalse(r["truncated"] or r["timed_out"])
        self.assertTrue(self.svc.grep("v1.0", "SYSCALL_DEFINE3(read").get("cached"))

    def test_regex_ignore_case_word(self) -> None:
        r = self.svc.grep("v1.0", "fo+", mode="regex", paths=["fs/"])
        self.assertEqual([m["line"] for m in r["matches"]], [2, 4])
        self.assertEqual(r["matches"][0]["ranges"], [[0, 3], [8, 11]])
        r = self.svc.grep("v1.0", "foo", ignore_case=True, paths=["fs"])
        self.assertEqual([m["line"] for m in r["matches"]], [2, 3, 4])
        r = self.svc.grep("v1.0", "foo", word=True, paths=["fs"])
        self.assertEqual([m["line"] for m in r["matches"]], [2])

    def test_glob_and_exclude(self) -> None:
        r = self.svc.grep("v1.0", "foo", paths=["*.c", "!drivers/staging"])
        paths = {m["path"] for m in r["matches"]}
        self.assertEqual(paths, {"fs/read_write.c", "drivers/x.c"})
        r = self.svc.grep("v1.0", "foo", paths=["!drivers"])
        self.assertNotIn("drivers/x.c", {m["path"] for m in r["matches"]})
        self.assertIn("README", {m["path"] for m in r["matches"]})

    def test_caps_and_clipping(self) -> None:
        r = self.svc.grep("v1.0", "foo", paths=["drivers/x.c"], max_per_file=5)
        self.assertEqual(r["total"], 5)
        r = self.svc.grep("v1.0", "foo", max_matches=3)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["total"], 3)
        r = self.svc.grep("v1.0", "foo", paths=["long.txt"])
        m = r["matches"][0]
        self.assertTrue(m["clipped"])
        self.assertEqual(m["text"][m["ranges"][0][0]:m["ranges"][0][1]], "foo")

    def test_timeout_is_partial_and_not_cached(self) -> None:
        r = self.svc.grep("v1.0", "foo", timeout=0.0)
        self.assertTrue(r["timed_out"])
        self.assertFalse(self.svc.grep("v1.0", "foo", timeout=0.0).get("cached", False))

    def test_errors(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self.svc.grep("v9.9", "foo")
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            self.svc.grep("--output=/tmp/x", "foo")
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            self.svc.grep("v1.0", "a(", mode="regex")
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            self.svc.grep("v1.0", "foo", paths=["../x"])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_busy(self) -> None:
        sem = self.svc._semaphore()
        sem.acquire()
        sem.acquire()
        try:
            with self.assertRaises(HTTPException) as ctx:
                self.svc.grep("v1.0", "unique-pattern", busy_wait=0.05)
            self.assertEqual(ctx.exception.status_code, 503)
        finally:
            sem.release()
            sem.release()

    def test_endpoint_parameter_mapping(self) -> None:
        from webapp.backend.routers import filesystem as fs_router
        with patch.object(fs_router, "grep_service", self.svc):
            body = fs_router.grep_code("v1.0", q="FOO", case=0, path=["fs/"])
            self.assertEqual((body["version"], body["query"], body["total"]), ("v1.0", "FOO", 3))
            self.assertEqual(fs_router.grep_code("v1.0", q="FOO", case=1, path=["fs/"])["total"], 1)
            # Case-insensitive by default: "foo" (line 2) and "FOO" (line 3) are whole words, "foobar" is not
            self.assertEqual(fs_router.grep_code("v1.0", q="fo+", mode="regex", word=1, path=["fs/"])["total"], 2)
            limited = fs_router.grep_code("v1.0", q="foo", limit=2)
            self.assertTrue(limited["truncated"])
            self.assertNotIn("cached", fs_router.grep_code("v1.0", q="foo", limit=2))
            for bad in (dict(q="foo", path=["../etc"]), dict(q=""), dict(q="a\nb")):
                with self.assertRaises(HTTPException) as ctx:
                    fs_router.grep_code("v1.0", **bad)
                self.assertEqual(ctx.exception.status_code, 400)
            with self.assertRaises(HTTPException) as ctx:
                fs_router.grep_code("--output=x", q="foo")
            self.assertEqual(ctx.exception.status_code, 400)
            with patch.object(self.svc, "grep", return_value={"sha": "x", "matches": [], "files": 0, "total": 0,
                                                              "truncated": False, "timed_out": True, "elapsed_ms": 1}):
                resp = fs_router.grep_code("v1.0", q="foo")
            self.assertEqual(resp.headers["cache-control"], "no-store")

    def test_without_native_max_count(self) -> None:
        with patch.object(gs, "git_supports_max_count", return_value=False):
            r = self.svc.grep("v1.0", "foo", paths=["drivers/x.c"], max_per_file=4)
        self.assertEqual(r["total"], 4)


if __name__ == "__main__":
    unittest.main()
