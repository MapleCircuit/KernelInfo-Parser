"""tests/test_history_service.py - Release-by-release history of symbol definitions and Kconfig symbols.

Unit tests cover the pure helpers (cosmetic detection, Kconfig property extraction and diffing,
candidate selection). Integration tests run against the indexed database and the tracked Linux
repository and are skipped when either is unavailable.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from webapp.backend.services.history_service import (
    RC_RE,
    HistoryService,
    _kconfig_file_facts,
    history_service,
    kconfig_field_changes,
    normalize_code,
)

KCONFIG_TEXT = """\
menu "Processor"
	depends on ARCH_FOO

if HAS_SMP

config SMP
	bool "Symmetric multi-processing support"
	depends on EXPERIMENTAL
	select USE_GENERIC_SMP_HELPERS
	default y if FOO_BOARD
	help
	  Say Y here.

endif

endmenu
"""


class TestHistoryHelpers(unittest.TestCase):
    def test_normalize_code_detects_cosmetic_changes(self) -> None:
        a = "int f(void)\n{\n\treturn 1; /* one */\n}\n"
        self.assertEqual(normalize_code(a), normalize_code("int f(void) {\n    return 1;\n}"))
        self.assertNotEqual(normalize_code(a), normalize_code("int f(void) { return 2; }"))
        # Kconfig: help text and '#' lines are significant, only whitespace is ignored
        self.assertNotEqual(normalize_code("help\n  a", False), normalize_code("help\n  b", False))

    def test_release_candidate_pattern(self) -> None:
        self.assertTrue(RC_RE.search("v2.6.15-rc3"))
        self.assertFalse(RC_RE.search("v2.6.15"))
        self.assertFalse(RC_RE.search("v3.0"))

    def test_kconfig_facts_include_inherited_dependencies(self) -> None:
        facts = _kconfig_file_facts(KCONFIG_TEXT, "SMP")
        self.assertEqual(len(facts), 1)
        f = facts[0]
        self.assertEqual(f["type"], "bool")
        self.assertEqual(f["prompt"], '"Symmetric multi-processing support"')
        self.assertEqual(f["depends_on"], ["EXPERIMENTAL"])
        self.assertEqual(f["inherited_depends"], ["ARCH_FOO", "HAS_SMP"])
        self.assertEqual(f["selects"], ["USE_GENERIC_SMP_HELPERS"])
        self.assertEqual(f["defaults"], ["y if FOO_BOARD"])
        self.assertIn("Say Y here.", f["help"])
        self.assertEqual(_kconfig_file_facts(KCONFIG_TEXT, "NOT_THERE"), ())

    def test_kconfig_field_changes(self) -> None:
        old = _kconfig_file_facts(KCONFIG_TEXT, "SMP")[0]
        new_text = KCONFIG_TEXT.replace("depends on EXPERIMENTAL", "depends on EXPERIMENTAL && BROKEN").replace("bool", "tristate")
        new = _kconfig_file_facts(new_text, "SMP")[0]
        changes = {c["field"]: c for c in kconfig_field_changes(old, new)}
        self.assertEqual(set(changes), {"type", "depends_on"})
        self.assertEqual(changes["type"]["before"], "bool")
        self.assertEqual(changes["type"]["after"], "tristate")
        self.assertEqual(changes["depends_on"]["removed"], ["EXPERIMENTAL"])
        self.assertEqual(changes["depends_on"]["added"], ["(EXPERIMENTAL && BROKEN)"])
        self.assertEqual(kconfig_field_changes(old, old), [])
        self.assertEqual(kconfig_field_changes(None, new), [])

    def test_pick_prefers_lineage_then_closest_line(self) -> None:
        cands = [
            {"tag_id": 1, "line_s": 10, "same_name": 1},
            {"tag_id": 2, "line_s": 500, "same_name": 1},
            {"tag_id": 3, "line_s": 12, "same_name": 0},
        ]
        self.assertEqual(HistoryService._pick(cands, set(), 490)["tag_id"], 2)
        self.assertEqual(HistoryService._pick(cands, {2}, 10)["tag_id"], 2)
        # A renamed lineage member is used only when no same-name tag remains
        self.assertEqual(HistoryService._pick([cands[2]], {3}, 10)["tag_id"], 3)
        self.assertIsNone(HistoryService._pick([cands[2]], set(), 10))

    def test_change_commits_rejects_option_injection(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            history_service.get_change_commits("kernel/fork.c", "--output=/tmp/x")
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            history_service.get_change_commits("../etc/passwd", "v2.6.15")


class TestHistoryIntegration(unittest.TestCase):
    """Indexed data: copy_process() (kernel/fork.c) changes in most early 2.6 releases."""

    PATH = "kernel/fork.c"
    NAME = "copy_process"

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from webapp.backend.database.pool import get_db_cursor
            from webapp.backend.services.history_service import indexed_versions
            with get_db_cursor() as cursor:
                cls.versions = indexed_versions(cursor, include_rc=True)
        except Exception as e:  # no database
            raise unittest.SkipTest(f"database unavailable: {e}")
        if not cls.versions:
            raise unittest.SkipTest("no indexed version")
        from webapp.backend.config import get_repo_dir
        cls.repo = str(get_repo_dir())
        cls.version = next((v["vname"] for v in reversed(cls.versions) if v["is_release"]), cls.versions[-1]["vname"])
        out = subprocess.run(
            ["git", "-C", cls.repo, "grep", "-n", f"\\*{cls.NAME}(", cls.version, "--", cls.PATH],
            capture_output=True, text=True,
        ).stdout
        if not out:
            raise unittest.SkipTest(f"{cls.NAME} not found in {cls.version}:{cls.PATH}")
        cls.line = int(out.splitlines()[0].split(":")[2])

    def history(self, include_rc: bool = False) -> dict:
        try:
            return history_service.get_symbol_history(self.version, self.PATH, line=self.line, name=self.NAME, include_rc=include_rc)
        except HTTPException as e:
            self.skipTest(f"{self.PATH} not indexed at {self.version}: {e.detail}")

    def test_function_is_followed_across_body_changes(self) -> None:
        """Regression: a body change gives the function a new ast_id; it must read 'modified', not 'absent'."""
        res = self.history()
        self.assertEqual(res["kind"], "symbol")
        self.assertEqual(res["ast_name"], self.NAME)
        timeline = res["timeline"]
        self.assertTrue(all(t["is_release"] for t in timeline))
        known = [t for t in timeline if t["status"] not in ("unindexed", "unparsed")]
        self.assertEqual(known[0]["status"], "introduced")
        self.assertNotIn("absent", [t["status"] for t in known])
        self.assertNotIn("removed", [t["status"] for t in known])
        self.assertGreater(res["change_count"], 0)
        prev = None
        for t in known:
            if prev is not None and t["status"] in ("modified", "cosmetic"):
                self.assertNotEqual(t["tag_id"], prev["tag_id"])
                self.assertTrue(any(d["type"] in ("add", "del") for d in t["diff"]))
                self.assertIn(str(t["tag_id"]), res["snippets"])
            if t["status"] == "unchanged":
                self.assertEqual(t["tag_id"], prev["tag_id"])
            prev = t

    def test_release_candidates_on_demand(self) -> None:
        releases = self.history()["timeline"]
        everything = self.history(include_rc=True)["timeline"]
        self.assertGreaterEqual(len(everything), len(releases))
        self.assertEqual([t["vname"] for t in everything if t["is_release"]], [t["vname"] for t in releases])

    def test_anchor_from_a_call_site(self) -> None:
        res = history_service.get_symbol_history(self.version, "kernel/sched.c", line=1, name=self.NAME)
        self.assertEqual(res["path"], self.PATH)
        self.assertEqual(res["ast_name"], self.NAME)

    def test_commits_behind_a_change(self) -> None:
        changed = next((t for t in self.history()["timeline"] if t["status"] == "modified" and t["prev_vname"]), None)
        if changed is None:
            self.skipTest("no modified release")
        res = history_service.get_change_commits(self.PATH, changed["vname"], changed["prev_vname"], changed["line_s"], changed["line_e"])
        self.assertGreater(res["total"], 0)
        for c in res["commits"]:
            # Every commit belongs to prev..vname
            in_range = subprocess.run(
                ["git", "-C", self.repo, "merge-base", "--is-ancestor", c["commit_hash"], changed["vname"]]
            ).returncode == 0
            before = subprocess.run(
                ["git", "-C", self.repo, "merge-base", "--is-ancestor", c["commit_hash"], changed["prev_vname"]]
            ).returncode == 0
            self.assertTrue(in_range and not before, c["commit_hash"])

    def test_kconfig_symbol_history(self) -> None:
        try:
            res = history_service.get_kconfig_history("CONFIG_SMP", self.version)
        except HTTPException as e:
            self.skipTest(f"CONFIG_SMP not indexed: {e.detail}")
        self.assertEqual(res["kind"], "kconfig")
        self.assertEqual(res["ast_name"], "CONFIG_SMP")
        self.assertGreater(len(res["paths"]), 1)
        first = next(t for t in res["timeline"] if t["defs"])
        self.assertEqual(first["status"], "introduced")
        self.assertTrue(all(d["facts"] and d["facts"]["type"] == "bool" for d in first["defs"] if d["facts"]))
        for t in res["timeline"]:
            for d in t["defs"]:
                if d["status"] == "modified":
                    self.assertTrue(d["changes"] or d["diff"])
                    for c in d["changes"]:
                        self.assertIn(c["field"], ("kind", "type", "prompt", "choice", "help", "defaults", "depends_on",
                                                   "inherited_depends", "selects", "implies", "ranges", "options"))

    def test_kconfig_anchor_from_kconfig_file(self) -> None:
        out = subprocess.run(
            ["git", "-C", self.repo, "grep", "-n", "^config SMP$", self.version, "--", "arch/x86_64/Kconfig", "arch/x86/Kconfig"],
            capture_output=True, text=True,
        ).stdout
        if not out:
            self.skipTest("no x86 'config SMP'")
        _, path, line, _ = out.splitlines()[0].split(":", 3)
        res = history_service.get_symbol_history(self.version, path, line=int(line))
        self.assertEqual(res["kind"], "kconfig")
        self.assertEqual(res["name"], "SMP")


if __name__ == "__main__":
    unittest.main()
