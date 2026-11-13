"""tests/test_history_service.py - Release-by-release history of symbol definitions and Kconfig symbols.

Unit tests cover the pure helpers (cosmetic detection, Kconfig property extraction and diffing,
candidate selection). Integration tests run against the indexed database and the tracked Linux
repository and are skipped when either is unavailable.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from unittest import mock

from core import DBLayout as D
from db_engine import MockDB
from db_engine.spec import gt
from tests.db_fixtures import patch_reader, requires_live_db
from fastapi import HTTPException
from webapp.backend.services.history_service import (
    RC_RE,
    HistoryService,
    _kconfig_file_facts,
    _slice_extent,
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
        self.assertTrue(RC_RE.search("v2.6.16.30-pre1"))
        self.assertTrue(RC_RE.search("v2.6.16.28-rc1"))
        self.assertFalse(RC_RE.search("v2.6.16.28"))

    def test_slice_extent_coordinates(self) -> None:
        raw_lines = [
            "/* header */",
            "int calculate(int x, int y) {",
            "    int res = x + y;",
            "    return res;",
            "}",
        ]
        # Single line slice
        self.assertEqual(_slice_extent(raw_lines, 2, 2, 1, 30), "int calculate(int x, int y) {")
        self.assertEqual(_slice_extent(raw_lines, 2, 2, 5, 14), "calculate")
        # Multi-line slice
        func_slice = _slice_extent(raw_lines, 2, 5, 1, 2)
        expected = "int calculate(int x, int y) {\n    int res = x + y;\n    return res;\n}"
        self.assertEqual(func_slice, expected)
        # Out of bounds / invalid coordinates
        self.assertEqual(_slice_extent(raw_lines, 0, 0, 0, 0), "")
        self.assertEqual(_slice_extent(raw_lines, 10, 15, 1, 1), "")

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

    def test_kconfig_blob_is_parsed_once_for_all_its_symbols(self) -> None:
        """B7: history (per symbol) and Compare share one parse per Kconfig blob SHA."""
        from unittest import mock
        from webapp.backend.services import diff_service, kconfig_facts
        text = KCONFIG_TEXT + "\nconfig NR_CPUS\n\tint \"Maximum CPUs\"\n\tdefault 8\n"
        sha = "f" * 40
        kconfig_facts._DEFS_CACHE.pop(sha, None)
        before = kconfig_facts.STATS["parsed"]
        with mock.patch("webapp.backend.services.history_service.git_reader.read_file",
                        return_value=(text.encode(), None, None)) as read:
            smp = HistoryService._file_facts("v3.0", "arch/x86/Kconfig", sha, "SMP")
            cpus = HistoryService._file_facts("v3.0", "arch/x86/Kconfig", sha, "NR_CPUS")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(kconfig_facts.STATS["parsed"] - before, 1)
        self.assertEqual(smp, _kconfig_file_facts(text, "SMP"))
        self.assertEqual(cpus[0]["type"], "int")
        self.assertNotIn("prompt_text", smp[0])  # history's shape is unchanged
        # Compare reads the same entry, with its raw prompt and first default
        defs = diff_service._blob_definitions(sha, None)
        self.assertEqual(kconfig_facts.STATS["parsed"] - before, 1)
        self.assertEqual(defs["NR_CPUS"][0]["prompt_text"], "Maximum CPUs")
        self.assertEqual(defs["NR_CPUS"][0]["first_default"], "8")

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


@requires_live_db("indexed history of the live database")
class TestHistoryIntegration(unittest.TestCase):
    """Indexed data: copy_process() (kernel/fork.c) changes in most early 2.6 releases."""

    PATH = "kernel/fork.c"
    NAME = "copy_process"

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from webapp.backend.database.pool import get_reader
            from webapp.backend.services.history_service import indexed_versions
            with get_reader() as reader:
                cls.versions = indexed_versions(reader, include_rc=True)
        except Exception as e:  # no database
            raise unittest.SkipTest(f"database unavailable: {e}")
        if not cls.versions:
            raise unittest.SkipTest("no indexed version")
        from webapp.backend.config import get_repo_dir
        cls.repo = str(get_repo_dir())
        releases = [v["vname"] for v in cls.versions if v["is_release"]]
        if not releases:
            raise unittest.SkipTest("no indexed release version")
        cls.version = releases[-1]
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
        if any(t["status"] in ("modified", "cosmetic") for t in known):
            self.assertGreater(res["change_count"], 0)
        prev = None
        for t in known:
            if prev is not None and t["status"] in ("modified", "cosmetic"):
                self.assertNotEqual(t["tag_id"], prev["tag_id"])
                self.assertTrue(any(d["type"] in ("add", "del") for d in t["diff"]))
                self.assertIn(str(t["tag_id"]), res["snippets"])
            if t["status"] == "unchanged":
                # The same tag, or a new tag with identical code (B14)
                self.assertTrue(t["tag_id"] == prev["tag_id"]
                                or res["snippets"].get(str(t["tag_id"])) == res["snippets"].get(str(prev["tag_id"])))
            prev = t

    def test_release_candidates_on_demand(self) -> None:
        releases = self.history()["timeline"]
        everything = self.history(include_rc=True)["timeline"]
        self.assertGreaterEqual(len(everything), len(releases))
        self.assertEqual([t["vname"] for t in everything if t["is_release"]], [t["vname"] for t in releases])

    def test_timeline_follows_one_branch(self) -> None:
        """Releases of other branches never interleave: each listed tag is reachable from the branch."""
        res = self.history()
        branch = res["branch"]
        self.assertTrue(branch)
        reachable = set(subprocess.run(
            ["git", "-C", self.repo, "for-each-ref", f"--merged={branch}", "--format=%(refname:short)", "refs/tags"],
            capture_output=True, text=True,
        ).stdout.split())
        self.assertTrue(all(t["vname"] in reachable or t["vname"] == branch for t in res["timeline"]))
        self.assertGreaterEqual(res["rc_count"], 0)

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


F_V1 = "int f(int x)\n{\n\treturn x;\n}\n\nint g(int y)\n{\n\treturn y;\n}\n"


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestChangeCommitsFromDatabase(unittest.TestCase):
    """GET /api/history/commits from m_bridge_commit_tag / m_commit_file (D05), against `git log` on a
    temporary repository: v1.0 -> (f changed, g changed, f changed) -> v1.1 -> a.c renamed to b.c -> v1.2."""

    @classmethod
    def setUpClass(cls) -> None:
        from tests.test_commit_relations import ENV, Store, git
        cls.dir = Path(tempfile.mkdtemp(prefix="histdb-"))
        cls.git = staticmethod(lambda *a: git(cls.dir, *a))
        cls.git("init", "-q", "-b", "master")
        cls.write("a.c", F_V1, "base")
        cls.git("tag", "v1.0")
        cls.f1 = cls.write("a.c", F_V1.replace("return x;", "return x + 1;"), "f: add one")
        cls.g1 = cls.write("a.c", F_V1.replace("return x;", "return x + 1;").replace("return y;", "return y * 2;"), "g: double")
        text_v2 = F_V1.replace("return x;", "if (x)\n\t\treturn 0;\n\treturn x + 1;").replace("return y;", "return y * 2;")
        cls.f2 = cls.write("a.c", text_v2, "f: zero")
        cls.git("tag", "v1.1")
        cls.git("mv", "a.c", "b.c")
        cls.git("commit", "-q", "-m", "rename a.c")
        cls.rename = cls.git("rev-parse", "HEAD").strip()
        cls.git("tag", "v1.2")
        cls.text_v2 = text_v2
        cls.store = Store(cls.dir, with_relations=False)
        cls.build_rows()

    @classmethod
    def write(cls, path: str, text: str, msg: str) -> str:
        (cls.dir / path).write_text(text)
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", msg)
        return cls.git("rev-parse", "HEAD").strip()

    @classmethod
    def build_rows(cls) -> None:
        """What the parser stores: ingest states (v1.1 on v1.0, v1.2 on v1.1), the v1.1 blob's tags (f lines 1-6, g
        lines 8-11), their commit bridges (GitCommitParser.map_tags_to_commits), the commit files."""
        from parser.git_ast import GitCommitParser
        db = cls.store.db
        db.create_table([D.m_bridge_tag, D.m_bridge_commit_tag, D.m_file_name, D.m_commit_file])
        db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(1, "Ann", "ann@x.org")], on_conflict="ignore")
        db.insert_rows("m_file_name", ["fnid", "fname"], [(1, "a.c"), (2, "b.c")])
        ids = {bytes(h).hex(): cid for cid, h in db.select_rows("m_commit", ["commit_id", "commit_hash"], {"commit_id": gt(0)})}
        cls.ids = ids
        tag = lambda t: cls.git("rev-parse", f"{t}^{{commit}}").strip()  # noqa: E731
        db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at", "base_commit_id"],
                       [(1, ids[tag("v1.0")], 2, 0, 0), (1, ids[tag("v1.1")], 2, 0, ids[tag("v1.0")]),
                        (1, ids[tag("v1.2")], 2, 0, ids[tag("v1.1")])])
        cls.blob = 500
        db.insert_rows("m_bridge_tag", ["blob_id", "tag_id", "line_s", "line_e", "char_s", "char_e"],
                       [(cls.blob, 1, 1, 6, 0, 0), (cls.blob, 2, 8, 11, 0, 0)])
        gp = GitCommitParser(repo_dir=str(cls.dir))
        _commits, hunks = gp.parse_version_commits_with_hunks("v1.0", "v1.1")
        bridges = gp.map_tags_to_commits([(1, cls.blob, 1, 6), (2, cls.blob, 8, 11)], "a.c", commit_hash_to_id=ids,
                                         file_hunks_map=hunks)
        db.insert_rows("m_bridge_commit_tag", ["commit_id", "blob_id", "tag_id"], bridges)
        cf_cols = ["commit_id", "fnid", "blob_id", "old_fnid", "old_blob_id", "change_type"]
        for sha, cid in ids.items():
            out = cls.git("diff-tree", "-r", "-M", "--no-commit-id", "--name-status", "--root", sha)
            for line in out.splitlines():
                parts = line.split("\t")
                fn = {"a.c": 1, "b.c": 2}
                if parts[0].startswith("R"):
                    db.insert_rows("m_commit_file", cf_cols, [(cid, fn[parts[2]], 0, fn[parts[1]], 0, "R")])
                else:
                    db.insert_rows("m_commit_file", cf_cols, [(cid, fn[parts[1]], 0, fn[parts[1]], 0, parts[0])])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self) -> None:
        from webapp.backend.services import history_service as hs
        from webapp.backend.services import release_service as rs
        self.hs, self.rs = hs, rs
        sha_v2 = self.git("rev-parse", "v1.1:a.c").strip()
        self.patches = [
            *self.store.patches(),
            mock.patch.object(hs, "get_active_network_id", return_value=1),
            mock.patch.object(hs, "get_active_repo_id", return_value=1),
            mock.patch.object(hs, "get_repo_dir", lambda: self.dir),
            mock.patch.object(rs, "get_repo_dir", lambda: self.dir),
            mock.patch.object(hs.git_tree_resolver, "map_blob_hashes",
                              side_effect=lambda cur, shas: {s: (self.blob, 1) for s in shas if s == sha_v2}),
        ]
        for p in self.patches:
            p.start()
        self.enterContext(patch_reader(self.store.engine))
        rs._TAGS_CACHE.clear()
        hs._cached_change_commits.cache_clear()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.rs._TAGS_CACHE.clear()

    def git_answer(self, path: str, frm: str, to: str, s: int = 0, e: int = 0) -> list[str]:
        return [c["commit_hash"] for c in self.hs._cached_change_commits(str(self.dir), path, frm, to, s, e, 100)]

    def test_lines_equal_git_log_L(self) -> None:
        for s, e, want in ((1, 6, [self.f2, self.f1]), (8, 11, [self.g1])):
            res = self.hs.history_service.get_change_commits("a.c", "v1.1", "v1.0", s, e)
            self.assertEqual(res["source"], "db")
            self.assertEqual([c["commit_hash"] for c in res["commits"]], self.git_answer("a.c", "v1.0", "v1.1", s, e))
            self.assertEqual({c["commit_hash"] for c in res["commits"]}, set(want))
        self.assertEqual(res["commits"][0]["author_name"], "Ann")

    def test_without_lines_lists_the_file_commits(self) -> None:
        res = self.hs.history_service.get_change_commits("a.c", "v1.2", "v1.1")
        self.assertEqual(res["source"], "db")
        self.assertEqual([c["commit_hash"] for c in res["commits"]], [self.rename])     # renamed away (old_fnid)
        res = self.hs.history_service.get_change_commits("a.c", "v1.1", "v1.0")
        self.assertEqual([c["commit_hash"] for c in res["commits"]], self.git_answer("a.c", "v1.0", "v1.1"))

    def test_branch_and_unjoined_ranges_use_git(self) -> None:
        self.assertEqual(self.hs.history_service.get_change_commits("a.c", "master", "v1.0", 1, 6)["source"], "git")
        # v1.0 -> v1.2 spans two ingestions: the line range cannot be mapped from the stored bridges
        self.assertEqual(self.hs.history_service.get_change_commits("b.c", "v1.2", "v1.0", 1, 6)["source"], "git")

    def test_reappeared_blob_does_not_leak_older_commits(self) -> None:
        db = self.store.db
        older = self.ids[self.git("rev-parse", "v1.0").strip()]
        db.insert_rows("m_bridge_commit_tag", ["commit_id", "blob_id", "tag_id"], [(older, self.blob, 1)])
        try:
            res = self.hs.history_service.get_change_commits("a.c", "v1.1", "v1.0", 1, 6)
            self.assertNotIn(self.git("rev-parse", "v1.0").strip(), [c["commit_hash"] for c in res["commits"]])
        finally:
            db.delete_rows("m_bridge_commit_tag", {"commit_id": older})

    def test_rename_is_followed(self) -> None:
        tag = lambda t: self.git("rev-parse", f"{t}^{{commit}}").strip()  # noqa: E731
        versions = [{"vname": v, "vid": self.ids[tag(v)]} for v in ("v1.0", "v1.1", "v1.2")]
        shas = self.hs.batch_blob_shas([f"{v['vname']}:a.c" for v in versions])
        self.assertIsNone(shas[2])
        sha_v2 = shas[1]
        new_sha = self.git("rev-parse", "v1.2:b.c").strip()
        db = MockDB(use_global=False)
        # v1.2 stores the new path in its checkpoint tree; the blob moved to 501 (a transition of the old blob)
        db.insert_rows("m_blob_transition", ["old_blob_id", "new_blob_id"], [(self.blob, 501)])
        db.insert_rows("m_file_name", ["fnid", "fname"], [(1, "a.c"), (2, "b.c")])
        db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(versions[2]["vid"], 2, 501)])
        meta = {sha_v2: (self.blob, 1)}
        with mock.patch.object(self.hs.git_tree_resolver, "map_blob_hashes", side_effect=lambda c, s: {x: (501, 1) for x in s}), \
                patch_reader(db), self.hs.get_reader() as reader:
            paths, renamed = self.hs.HistoryService._follow_renames(reader, versions, "a.c", shas, meta)
        self.assertEqual(paths, ["a.c", "a.c", "b.c"])
        self.assertEqual(renamed, {2: "a.c"})
        self.assertEqual(shas[2], new_sha)          # the timeline continues on the new path
        self.assertIn(new_sha, meta)                # a pure rename keeps the blob


if __name__ == "__main__":
    unittest.main()
