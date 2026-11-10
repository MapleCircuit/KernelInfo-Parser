"""tests/test_webapp_checkpatch.py - Fenced checkpatch.pl runs (P4, webapp/API.md sections 2.18, 4.7)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from webapp.backend.services import checkpatch_service as cp


class TestCheckpatch(unittest.TestCase):
    def test_disabled_by_default_spawns_nothing(self) -> None:
        from core.config import DEFAULT_CONFIG
        self.assertFalse(DEFAULT_CONFIG["webapp"]["tools"]["checkpatch"]["enabled"])
        with mock.patch.object(cp, "tools_config", return_value={"enabled": False, "ref": "latest-release", "sandbox": ""}), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("spawned")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawned")):
            self.assertEqual(cp.check_patch("diff --git a/x b/x\n")["enabled"], False)

    def test_ref_resolution_accepts_release_tags_only(self) -> None:
        tags = ["v6.9", "v6.10", "v6.10.3", "v6.11-rc2", "v5.4"]
        self.assertEqual(cp.resolve_script_ref("latest-release", tags), "v6.10")
        self.assertEqual(cp.resolve_script_ref("v5.4", tags), "v5.4")
        for bad in ("master", "HEAD", "0123456789abcdef", "v6.11-rc2", "v9.9", "refs/heads/v6.10"):
            with self.assertRaises(HTTPException, msg=bad):
                cp.resolve_script_ref(bad, tags)

    def test_terse_parser(self) -> None:
        out = ("input.patch:17: ERROR:TRAILING_WHITESPACE: trailing whitespace\n"
               "input.patch:18: WARNING:LONG_LINE: line length of 120 exceeds 100 columns\n"
               "input.patch:30: CHECK: Alignment should match open parenthesis\n"
               "NOTE: For some of the reported defects\n")
        res, raw = cp.parse_terse(out)
        self.assertEqual([(r["level"], r["type"], r["line"]) for r in res], [("ERROR", "TRAILING_WHITESPACE", 17), ("WARNING", "LONG_LINE", 18), ("CHECK", "", 30)])
        self.assertEqual(raw, ["NOTE: For some of the reported defects"])

    def test_timeout_and_cleanup(self) -> None:
        if not shutil.which("perl"):
            self.skipTest("perl is not installed")
        before = set(os.listdir(os.environ.get("TMPDIR", "/tmp")))
        res = cp.run_checkpatch({"scripts/checkpatch.pl": "sleep 5;\n"}, "x\n", timeout=0.5)
        self.assertTrue(res["timed_out"])
        after = set(os.listdir(os.environ.get("TMPDIR", "/tmp")))
        self.assertFalse({n for n in after - before if n.startswith("kip-checkpatch-")})

    def test_fake_script_sees_only_its_directory(self) -> None:
        if not shutil.which("perl"):
            self.skipTest("perl is not installed")
        script = ('use Cwd; my $f = $ARGV[-1]; print "$f:3: ERROR:FAKE: home=$ENV{HOME} cwd=" . getcwd() . "\\n";'
                  'print "$f:4: WARNING:ARGS: @ARGV\\n";')
        res = cp.run_checkpatch({"scripts/checkpatch.pl": script + "\n# 'show-types!' 'color=s'\n"}, "x\n")
        self.assertEqual(res["counts"], {"ERROR": 1, "WARNING": 1, "CHECK": 0})
        msg = res["results"][0]["message"]
        self.assertIn("kip-checkpatch-", msg)
        home, cwd = msg.split()[0][5:], msg.split()[1][4:]
        self.assertEqual(home, cwd)
        self.assertIn("--no-tree", res["results"][1]["message"])
        self.assertIn("--color=never", res["results"][1]["message"])


if __name__ == "__main__":
    unittest.main()
