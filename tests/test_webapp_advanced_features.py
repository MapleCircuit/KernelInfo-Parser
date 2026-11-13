"""tests/test_webapp_advanced_features.py - Unit & Integration Tests for 14 Advanced WebApp Systems.

Tests:
1. Cross-Version File & Kconfig Diff
2. Global Symbol XRef & Search
3. Interactive Canvas DAG Graph Payload
4. Kconfig Auto-Solver
5. Patch Reviewer & Maintainers Matcher
6. AST Semantic Query Sandbox
7. Clang compile_commands.json Exporter
8. Struct Memory Layout & Pahole Alignment Visualizer
9. Kernel Security & Anti-Pattern Vulnerability Scanner
10. Interactive Codebase Treemap Hierarchy
11. Kconfig Footprint & Kernel Size Estimator (Bloat-O-Meter)
12. Function Call Graph & Callers / Callees Flow
13. Interactive Code Tour Presets
14. In-Browser Patch Staging & git format-patch Generator
"""
from __future__ import annotations

import os
import sys
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import require_indexed_ref
from tests.db_fixtures import requires_live_db
from fastapi import HTTPException
from webapp.backend.database.pool import get_reader
from webapp.backend.models import FormatPatchRequest
from webapp.backend.services.callgraph_service import CallgraphService
from webapp.backend.services.diff_service import DiffService
from webapp.backend.services.filesystem_service import FilesystemService
from webapp.backend.services.git_service import GitService, _compute_structured_diff
from webapp.backend.services.kconfig_service import KconfigService
from webapp.backend.services.maintainer_service import MaintainerService
from webapp.backend.services.pahole_service import PaholeService
from webapp.backend.services import (
    callgraph_service as callgraph_mod,
    filesystem_service as filesystem_mod,
    pahole_service as pahole_mod,
    symbol_service as symbol_mod,
)
from webapp.backend.services.arch_layout_service import arch_layout_service
from webapp.backend.services.symbol_service import SymbolService
from webapp.backend.services.tree_resolver import ArchScope

_diff, _fs, _kconfig, _symbols = DiffService(), FilesystemService(), KconfigService(), SymbolService()
get_versions_diff = _diff.get_versions_diff
get_kconfig_diff = _diff.get_kconfig_diff
export_compile_commands = _fs.export_compile_commands
get_codebase_treemap = _fs.get_codebase_treemap
get_kconfig_graph = _kconfig.get_graph
autosolve_kconfig = _kconfig.autosolve
get_symbol_xref = _symbols.get_symbol_xref
get_symbol_detail = _symbols.get_symbol_detail
get_include_symbols = _symbols.get_include_symbols
search_symbols = _symbols.search_symbols
get_function_callgraph = CallgraphService().get_function_callgraph
get_struct_layout = PaholeService().get_struct_layout
match_patch_maintainers = MaintainerService().match_maintainers
generate_formatted_patch = GitService().format_patch


# --- Fixture data for the tests that must not depend on a parsed database ---------------------------------
# Revision tree: blob_id -> paths
_FIXTURE_BLOBS: dict[int, list[str]] = {
    101: ["include/linux/slab.h"],
    102: ["mm/slab.c"],
    103: ["include/linux/sched.h"],
    104: ["fs/ext4/super.c"],
    105: ["fs/ext4/inode.c"],
}
# Symbol definitions (the m_symbol_def view's columns; `fid` is the view's blob_id alias)
_FIXTURE_DEFS: list[dict] = [
    {"def_id": 1, "name": "kmalloc", "type_id": 10, "type_name": "C_functiondecl", "blob_id": 101,
     "line_s": 300, "line_e": 312, "ast_id": 5001, "tag_id": 1, "linkage": None, "ast_name": "kmalloc"},
    {"def_id": 2, "name": "task_struct", "type_id": 11, "type_name": "C_structdecl", "blob_id": 103,
     "line_s": 1193, "line_e": 1580, "ast_id": 5002, "tag_id": 2, "linkage": None, "ast_name": "task_struct"},
    {"def_id": 3, "name": "task_nice", "type_id": 10, "type_name": "C_functiondecl", "blob_id": 103,
     "line_s": 1700, "line_e": 1703, "ast_id": 5003, "tag_id": 3, "linkage": None, "ast_name": "task_nice"},
    {"def_id": 4, "name": "ext4_fill_super", "type_id": 10, "type_name": "C_functiondecl", "blob_id": 104,
     "line_s": 3000, "line_e": 3700, "ast_id": 5004, "tag_id": 4, "linkage": None, "ast_name": "ext4_fill_super"},
    {"def_id": 5, "name": "ext4_iget", "type_id": 10, "type_name": "C_functiondecl", "blob_id": 105,
     "line_s": 3800, "line_e": 4000, "ast_id": 5005, "tag_id": 5, "linkage": None, "ast_name": "ext4_iget"},
    # A definition whose blob is not in the revision's tree: never returned for this revision
    {"def_id": 6, "name": "ext4_old_helper", "type_id": 10, "type_name": "C_functiondecl", "blob_id": 999,
     "line_s": 10, "line_e": 20, "ast_id": 5006, "tag_id": 6, "linkage": None, "ast_name": "ext4_old_helper"},
]
# References to kmalloc (ast 5001): a call in ext4 and a declaration in mm/slab.c
_FIXTURE_REFS: list[dict] = [
    {"role": 3, "line": 3120, "char_s": 9, "fid": 104, "tag_id": 4, "caller": "ext4_fill_super", "target": 5001},
    {"role": 1, "line": 42, "char_s": 1, "fid": 102, "tag_id": 7, "caller": None, "target": 5001},
    {"role": 3, "line": 77, "char_s": 3, "fid": 999, "tag_id": 8, "caller": "gone", "target": 5001},  # out of tree
]
_FIXTURE_REV = "fixture-rev"


class _FixtureReader:
    """Read session double answering the symbol reads of the webapp services from the fixture lists above (the
    symbol read intents they exercise are stubbed: seeding the AST / tag graph behind them is not what is tested)."""

    def __enter__(self) -> "_FixtureReader":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def select_rows(self, source, columns, where=None, **kw) -> list[dict]:
        if source != ("m_symbol_def", "s"):
            return []
        names = where["s.name"]
        names = {names} if isinstance(names, str) else set(names)
        rows = [{**d, "fid": d["blob_id"]} for d in _FIXTURE_DEFS if d["name"] in names]
        if "t.name" in where:
            rows = [r for r in rows if r["type_name"] in where["t.name"]]
        return [dict(r) for r in rows]

    def select_one(self, *args, **kw):
        return None

    def aggregate(self, *args, **kw) -> list[dict]:
        return []

    def symbol_name_page(self, prefix: str, *, after=None, limit: int = 400) -> list[str]:
        names = sorted({d["name"] for d in _FIXTURE_DEFS if d["name"].startswith(prefix)}, key=lambda n: (len(n), n))
        return [n for n in names if after is None or (len(n), n) > tuple(after)][:limit]

    def symbol_refs_of_targets(self, ast_ids, *, role=None, with_caller: bool = False, limit=None) -> list[dict]:
        targets = set(ast_ids)
        keep = ("role", "line", "char_s", "fid", "tag_id", "ast_id") + (("caller",) if with_caller else ())
        rows = [
            {**{k: v for k, v in r.items() if k in keep}, "ast_id": r["target"]}
            for r in _FIXTURE_REFS if r["target"] in targets and role in (None, r["role"])
        ]
        return rows[:limit] if limit else rows

    def symbol_def_blobs_of_targets(self, ast_ids) -> set:
        return set()


def _fixture_reader() -> _FixtureReader:
    return _FixtureReader()


def _fixture_scope(cursor, version_name):
    return 1, version_name, {k: list(v) for k, v in _FIXTURE_BLOBS.items()}


def _fixture_arch(version_name, arch):
    return "x86_64", ArchScope.of("x86"), 64


def _fixture_tree() -> dict[str, int]:
    """{path: size} of a small synthetic kernel tree (more than 1000 files under arch/)."""
    sizes: dict[str, int] = {}
    for arch in ("arm", "x86"):
        for i in range(600):
            sizes[f"arch/{arch}/mach-{i % 12}/board-{i}.c"] = 1000 + i
        sizes[f"arch/{arch}/include/asm/io.h"] = 4096
    for i in range(50):
        sizes[f"fs/ext4/file{i}.c"] = 2000 + i
        sizes[f"kernel/sched/part{i}.c"] = 3000 + i
    sizes["include/linux/sched.h"] = 80000
    sizes["Makefile"] = 50000
    return sizes


def _fixture_kconfig_model():
    """A Kconfig model entry (KconfigService.get_model) with EXT4_FS and BTRFS_FS and their relations."""
    symbols = {
        "EXT4_FS": {"selects": [("JBD2", None), ("CRC16", None)], "implies": []},
        "BTRFS_FS": {"selects": [("LIBCRC32C", None), ("ZLIB_DEFLATE", None)], "implies": []},
        "BLOCK": {"selects": [], "implies": []},
    }
    return SimpleNamespace(
        model={"symbols": symbols},
        own_depends={"EXT4_FS": ["BLOCK"], "BTRFS_FS": ["BLOCK"]},
        selected_by={},
        arch={"arch": "x86_64"},
    )


@contextmanager
def _fixture_services():
    """Point the symbol, struct-layout, callgraph, filesystem and Kconfig services at the fixtures above
    (no database rows, Git tree or parsed version needed)."""
    with ExitStack() as stack:
        for mod in (symbol_mod, callgraph_mod):
            stack.enter_context(patch.object(mod, "get_reader", _fixture_reader))
        stack.enter_context(patch.object(filesystem_mod, "get_reader", _fixture_reader))
        for mod in (symbol_mod, pahole_mod, callgraph_mod):
            stack.enter_context(patch.object(mod, "definition_arch", _fixture_arch))
        stack.enter_context(patch.object(SymbolService, "_scope", staticmethod(_fixture_scope)))
        stack.enter_context(patch.object(filesystem_mod, "get_version_info", lambda cursor, v: (1, _FIXTURE_REV)))
        tree = _fixture_tree()
        stack.enter_context(patch.object(filesystem_mod.git_tree_resolver, "list_tree", lambda rev: list(tree)))
        stack.enter_context(patch.object(filesystem_mod.git_tree_resolver, "list_tree_sizes", lambda rev: dict(tree)))
        stack.enter_context(patch.object(arch_layout_service, "layout", lambda rev, arch: None))
        stack.enter_context(patch.object(KconfigService, "get_model", lambda self, *a, **k: _fixture_kconfig_model()))
        filesystem_mod._TREEMAP_CACHE.clear()
        try:
            yield
        finally:
            filesystem_mod._TREEMAP_CACHE.clear()


class TestWebappAdvancedFeatures(unittest.TestCase):
    """Test suite for advanced web application systems."""

    @requires_live_db("reads the live database")
    def test_version_diff_and_kconfig_diff(self) -> None:
        # File Tree Diff (same version self-diff should be 100% unchanged)
        res = get_versions_diff("v3.0", "v3.0")
        self.assertIn("summary", res)
        self.assertEqual(res["summary"]["added_count"], 0)
        self.assertEqual(res["summary"]["removed_count"], 0)
        self.assertGreater(res["summary"]["unchanged_count"], 0)

        # Kconfig Diff
        k_res = get_kconfig_diff("v3.0", "v3.0")
        self.assertIn("summary", k_res)
        self.assertEqual(k_res["summary"]["added"], 0)
        self.assertEqual(k_res["summary"]["removed"], 0)

    def test_symbol_xref_and_search(self) -> None:
        with _fixture_services():
            # Rich symbol search from m_symbol_def: in-tree prefix matches only, shorter names first
            search_res = search_symbols("v3.0", "ext4", limit=10)
            self.assertIsInstance(search_res, list)
            self.assertEqual([s["name"] for s in search_res], ["ext4_iget", "ext4_fill_super"])
            sym = search_res[0]
            for key in ("name", "type_name", "file_path", "line_s", "ast_id"):
                self.assertIn(key, sym)
            self.assertEqual(sym["file_path"], "fs/ext4/inode.c")

            # XRef search for kmalloc
            xref = get_symbol_xref("v3.0", "kmalloc")
        self.assertIn("symbol", xref)
        self.assertEqual(xref["symbol"], "kmalloc")
        self.assertIn("definitions", xref)
        self.assertIn("references", xref)
        self.assertEqual([d["file"] for d in xref["definitions"]], ["include/linux/slab.h"])
        self.assertEqual(xref["ast_id"], 5001)
        # Only references from blobs in the revision's tree, grouped by role
        self.assertEqual([(r["type"], r["file"]) for r in xref["references"]],
                         [("Decl", "mm/slab.c"), ("Call", "fs/ext4/super.c")])
        self.assertEqual(xref["references"][1]["caller"], "ext4_fill_super")
        # Every reference carries its role badge (the categories are not repeated as separate lists)
        badges = {"Decl", "TypeUsage", "Call", "MemberRef", "DeclRef", "MacroExp", "Ref"}
        self.assertTrue(all(r["type"] in badges for r in xref["references"]))
        for key in ("calls", "member_refs", "type_usages", "macro_expansions", "usages", "references_count"):
            self.assertNotIn(key, xref)

    def test_kconfig_dag_graph(self) -> None:
        # Graph for EXT4_FS
        graph = get_kconfig_graph("v3.0", "EXT4_FS")
        self.assertEqual(graph["symbol"], "EXT4_FS")
        self.assertIn("edges", graph)
        self.assertNotIn("nodes", graph)
        # EXT4_FS selects JBD2 at v3.0
        self.assertIn({"from": "EXT4_FS", "to": "JBD2", "type": "selects"}, graph["edges"])

    def test_kconfig_autosolve(self) -> None:
        # Autosolve for EXT4_FS (the body of POST /api/kconfig/{v}/autosolve)
        res = autosolve_kconfig("v3.0", {"target_symbol": "EXT4_FS", "current_values": {}})
        self.assertTrue(res["solution_found"])
        self.assertIn("toggles_needed", res)

    @requires_live_db("reads the live database")
    def test_patch_reviewer_maintainers_matcher(self) -> None:
        sample_patch = """diff --git a/fs/ext4/super.c b/fs/ext4/super.c
--- a/fs/ext4/super.c
+++ b/fs/ext4/super.c
@@ -10,6 +10,12 @@
+/* Added sample comment for patch reviewer test */
+int test_func(void) { return 0; }
"""
        res = match_patch_maintainers("v3.0", patch_text=sample_patch)
        self.assertEqual(len(res["files"]), 1)
        self.assertEqual(res["skipped_files_count"], 0)
        self.assertEqual(res["files"][0]["file_path"], "fs/ext4/super.c")
        self.assertIn("suggested_to", res)
        self.assertIn("suggested_cc", res)

    def test_history_diff_and_release_slider(self) -> None:
        """Verify the structured diff of history timelines and the release slider assets."""
        # 1. Test diff computation helper
        code_v1 = "int foo(void) {\n    return 1;\n}\n"
        code_v2 = "int foo(int x) {\n    return x + 1;\n}\n"
        diffs = _compute_structured_diff(code_v1, code_v2)
        self.assertGreater(len(diffs), 0)
        types = [d["type"] for d in diffs]
        self.assertIn("del", types)
        self.assertIn("add", types)

        # 2. The release slider lives in the modular frontend (the legacy webapp.html page is gone)
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertFalse(os.path.exists(os.path.join(base_dir, "webapp", "webapp.html")))
        modal_js = os.path.join(base_dir, "webapp", "frontend", "js", "components", "symbol_history_modal.js")
        with open(modal_js, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("export function showSymbolHistoryModal", content)
        self.assertIn("export function showKconfigHistoryModal", content)
        self.assertIn('type="range"', content)
        self.assertIn("symhist-gap", content)
        self.assertIn('data-act="rc"', content)
        self.assertIn("api.getHistoryCommits", content)

    @requires_live_db("reads the live database")
    def test_clang_compile_commands_exporter(self) -> None:
        cmds = export_compile_commands("v3.0", arch="x86")
        self.assertIsInstance(cmds, list)
        self.assertGreater(len(cmds), 0)
        self.assertIn("directory", cmds[0])
        self.assertIn("command", cmds[0])
        self.assertIn("file", cmds[0])
        self.assertTrue(cmds[0]["file"].endswith(".c"))

    def test_struct_layout_pahole(self) -> None:
        require_indexed_ref("v3.0")
        res = get_struct_layout("v3.0", "task_struct")
        self.assertIn("total_size", res)
        self.assertIn("alignment", res)
        self.assertIn("members", res)
        self.assertIn("cache_lines_used", res)
        self.assertIn("optimization", res)
        self.assertGreater(res["total_size"], 0)
        self.assertEqual(res["file_path"], "include/linux/sched.h")
        # Members typed by AST category only (typedefs, embedded structs) are estimated
        self.assertTrue(res["is_estimate"])
        self.assertEqual(res["estimated_members"], sum(1 for m in res["members"] if m["estimated"]))

        # The struct definition, not a macro of the same name: struct list_head is two pointers
        lh = get_struct_layout("v3.0", "list_head")
        self.assertEqual(lh["file_path"], "include/linux/types.h")
        self.assertEqual([m["name"] for m in lh["members"]], ["next", "prev"])
        self.assertEqual(lh["total_size"], 16)

        # Compiler-extracted array sizes (e.g. unsigned char id[16] in struct adb_ids)
        adb = get_struct_layout("v3.0", "adb_ids")
        self.assertEqual(adb["file_path"], "include/linux/adb.h")
        self.assertEqual([m["name"] for m in adb["members"]], ["nids", "id"])
        self.assertEqual(adb["members"][0]["size"], 4)
        self.assertEqual(adb["members"][1]["size"], 16)
        self.assertEqual(adb["members"][1]["offset"], 4)
        self.assertEqual(adb["members"][1]["type"], "unsigned char [16]")
        self.assertEqual(adb["total_size"], 20)
        self.assertFalse(adb["is_estimate"])



    @requires_live_db("reads the live database")
    def test_codebase_treemap(self) -> None:
        tree = get_codebase_treemap("v3.0", max_depth=3)
        self.assertIn("name", tree)
        self.assertIn("children", tree)
        self.assertGreater(len(tree["children"]), 0)

    def test_function_callgraph(self) -> None:
        require_indexed_ref("v3.0")
        res = get_function_callgraph("v3.0", "ext4_fill_super")
        self.assertEqual(res["function"], "ext4_fill_super")
        self.assertEqual(res["file_path"], "fs/ext4/super.c")
        self.assertIn("callers", res)
        self.assertIn("callees", res)
        if len(res["callees"]) > 0:
            callee = res["callees"][0]
            self.assertIn("name", callee)
            self.assertIn("file_path", callee)

    def test_in_browser_patch_format(self) -> None:
        req = FormatPatchRequest(
            file_path="fs/ext4/super.c",
            original_content="int a = 1;\nint b = 2;\n",
            modified_content="int a = 1;\nint b = 3;\n",
            commit_subject="ext4: update b value",
        )
        res = generate_formatted_patch("v3.0", req)
        self.assertIn("diff", res)
        self.assertIn("formatted_patch", res)
        self.assertIn("From:", res["formatted_patch"])
        self.assertIn("Subject: [PATCH] ext4: update b value", res["formatted_patch"])

    @requires_live_db("reads the live database")
    def test_edge_cases_empty_and_fallback(self) -> None:
        with _fixture_services():
            self._check_edge_cases()

    def _check_edge_cases(self) -> None:
        # 1. Empty Patch Review
        empty_patch = match_patch_maintainers("v3.0", patch_text="")
        self.assertEqual(len(empty_patch["files"]), 0)

        # 3. Patch Format with Unchanged Code
        no_change_patch = generate_formatted_patch("v3.0", FormatPatchRequest(
            file_path="fs/ext4/super.c",
            original_content="int a = 1;\n",
            modified_content="int a = 1;\n",
            commit_subject="ext4: no-op commit",
        ))
        self.assertEqual(no_change_patch["diff"], "")
        self.assertIn("[PATCH] ext4: no-op commit", no_change_patch["formatted_patch"])

        # 4. Unknown struct: 404, never an invented layout
        with self.assertRaises(HTTPException) as ctx:
            get_struct_layout("v3.0", "unknown_custom_struct")
        self.assertEqual(ctx.exception.status_code, 404)

        # 5. Alternate Architectures for Clang Database
        arm_cmds = export_compile_commands("v3.0", arch="arm")
        self.assertGreater(len(arm_cmds), 0)
        self.assertIn("arch/arm/include", arm_cmds[0]["command"])

        # 7. Non-existent Function Callgraph
        empty_callgraph = get_function_callgraph("v3.0", "non_existent_fn_xyz")
        self.assertEqual(empty_callgraph["callers"], [])
        self.assertEqual(empty_callgraph["callees"], [])

        # 8. Treemap Depth Variations
        shallow_tree = get_codebase_treemap("v3.0", max_depth=1)
        self.assertIn("children", shallow_tree)
        self.assertGreater(len(shallow_tree["children"]), 0)

        # 10. Multi-depth Treemap Hierarchy Validation
        deep_tree = get_codebase_treemap("v3.0", max_depth=3)
        self.assertIn("children", deep_tree)
        arch_node = next((c for c in deep_tree["children"] if c["name"] == "arch"), None)
        self.assertIsNotNone(arch_node)
        self.assertGreater(len(arch_node.get("children", [])), 0)
        self.assertGreater(arch_node.get("file_count", 0), 1000)

        # 11. Multi-symbol DAG Graph
        g1 = get_kconfig_graph("v3.0", "EXT4_FS")
        g2 = get_kconfig_graph("v3.0", "BTRFS_FS")
        self.assertGreater(len(g1["edges"]), 0)
        self.assertGreater(len(g2["edges"]), 0)

    def test_symbol_endpoints(self) -> None:
        with _fixture_services():
            detail = get_symbol_detail("v3.0", "task_struct")
            self.assertIn("symbol_name", detail)
            self.assertEqual(detail["symbol_name"], "task_struct")
            self.assertIn("definition", detail)
            self.assertIn("definitions", detail)
            self.assertEqual(detail["file_path"], "include/linux/sched.h")
            self.assertEqual(detail["definition"]["ast_id"], 5002)
            # References are xref's: symbol detail (Go to Definition) does not compute them
            self.assertNotIn("usages", detail)

            # A symbol the revision does not define is a 404
            with self.assertRaises(HTTPException) as ctx:
                get_symbol_detail("v3.0", "no_such_symbol_xyz")
            self.assertEqual(ctx.exception.status_code, 404)

            results = search_symbols("v3.0", "task", limit=10)
        self.assertIsInstance(results, list)
        self.assertEqual([r["name"] for r in results], ["task_nice", "task_struct"])

    @requires_live_db("reads m_ast_include of the live database")
    def test_include_symbols_endpoint_and_dom(self) -> None:
        """Verify get_include_symbols endpoint and includeSymbolsPopover UI structure."""
        # 1. 404 on non-existent AST ID
        with self.assertRaises(HTTPException) as ctx:
            get_include_symbols("v3.0", 999999999)
        self.assertEqual(ctx.exception.status_code, 404)

        # 2. Existing AST ID query if any include exists in DB
        with get_reader() as reader:
            row = reader.select_one("m_ast_include", ["ast_id"])
        if row:
            res = get_include_symbols("v3.0", row["ast_id"])
            self.assertIn("ast_id", res)
            self.assertIn("include_text", res)
            self.assertIn("header_file", res)
            self.assertIn("symbols", res)
            self.assertIsInstance(res["symbols"], list)

        # 3. Fallback resolution when AST ID is 0 or missing
        fb_res = get_include_symbols("v3.0", 0, header="linux/const.h", file_path="include/linux/const.h")
        self.assertIn("ast_id", fb_res)
        self.assertIn("header_file", fb_res)
        self.assertEqual(fb_res["header_file"], "include/linux/const.h")
        self.assertEqual(fb_res["symbols"], [])

        # 4. Verify modular frontend popover component and stylesheet
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        popover_js = os.path.join(base_dir, "webapp", "frontend", "js", "components", "include_symbols_popover.js")
        with open(popover_js, "r", encoding="utf-8") as f:
            p_content = f.read()
        self.assertIn("class IncludeSymbolsPopover", p_content)
        self.assertIn("includeSymbolsPopover = new IncludeSymbolsPopover()", p_content)
        self.assertIn("include-symbols-popover", p_content)
        self.assertIn("include-category-pills", p_content)
        self.assertIn("btn-include-open-header", p_content)

        modals_css = os.path.join(base_dir, "webapp", "frontend", "css", "modals.css")
        with open(modals_css, "r", encoding="utf-8") as f:
            c_content = f.read()
        self.assertIn(".include-symbols-popover", c_content)
        self.assertIn(".include-category-pills", c_content)
        # One rule per backend category (include-badge-<category lowercased>)
        for cat in ("function", "struct", "union", "enum", "enumconstant", "typedef", "macro", "variable", "symbol"):
            self.assertIn(f".include-badge-{cat}", c_content)

        context_menu_js = os.path.join(base_dir, "webapp", "frontend", "js", "components", "context_menu.js")
        with open(context_menu_js, "r", encoding="utf-8") as f:
            cm_content = f.read()
        self.assertIn("import { includeSymbolsPopover }", cm_content)
        self.assertIn("includeSymbolsPopover.show", cm_content)

    def test_normalize_repo_path(self) -> None:
        """Verify normalize_repo_path strips RAMDISK and temp clone prefixes down to relative git paths."""
        from core.globalstuff import normalize_repo_path

        # 1. /dev/shm/code-parser.XXXXXX with ../../ traversal
        p1 = "/dev/shm/code-parser.6sqil09b/Documentation/virtual/lguest/../../../include/linux/lguest_launcher.h"
        self.assertEqual(normalize_repo_path(p1), "include/linux/lguest_launcher.h")

        # 2. /dev/shm/code-parser.XXXXXX direct relative path
        p2 = "/dev/shm/code-parser.6sqil09b/arch/alpha/kernel/pci_impl.h"
        self.assertEqual(normalize_repo_path(p2), "arch/alpha/kernel/pci_impl.h")

        # 3. /tmp/code-parser.XXXXXX
        p3 = "/tmp/code-parser.xyz123/drivers/net/e1000.c"
        self.assertEqual(normalize_repo_path(p3), "drivers/net/e1000.c")

        # 4. Explicit base_dir
        base = "/dev/shm/code-parser.custom"
        p4 = "/dev/shm/code-parser.custom/include/linux/lockd/xdr.h"
        self.assertEqual(normalize_repo_path(p4, base), "include/linux/lockd/xdr.h")

        # 5. System headers preserved
        p5 = "/usr/include/stdio.h"
        self.assertEqual(normalize_repo_path(p5), "/usr/include/stdio.h")

        # 6. Written include syntax preserved
        self.assertEqual(normalize_repo_path("<linux/types.h>"), "<linux/types.h>")
        self.assertEqual(normalize_repo_path('"lockd/xdr.h"'), '"lockd/xdr.h"')

    def test_pure_asm_detection_and_classification(self) -> None:
        """Verify is_pure_asm_content and type_check identify pure ASM .h/.c files as T_ASM."""
        from core.globalstuff import is_pure_asm_content, type_check, T_C, T_ASM

        # 1. Typical ASM header file like arch/m68k/fpsp040/fpsp.h
        asm_header = """|
| fpsp.h
|
| Motorola 68040 Floating Point Software Package
|
\t.set\tLOCAL_SIZE,128
\t.set\tEXC_SR,0
\t.global\t_fpsp_init
_fpsp_init:
\trts
"""
        self.assertTrue(is_pure_asm_content(asm_header))
        self.assertEqual(type_check("arch/m68k/fpsp040/fpsp.h", asm_header), T_ASM)

        # 2. Typical C header file
        c_header = """#ifndef _LINUX_FOO_H
#define _LINUX_FOO_H

struct foo {
    int x;
};

int get_foo(void);

#endif
"""
        self.assertFalse(is_pure_asm_content(c_header))
        self.assertEqual(type_check("include/linux/foo.h", c_header), T_C)

        # 3. Typical C source file
        c_source = """#include <linux/foo.h>

int get_foo(void) {
    return 42;
}
"""
        self.assertFalse(is_pure_asm_content(c_source))
        self.assertEqual(type_check("drivers/foo.c", c_source), T_C)

    def test_statusbar_removal_and_global_loading_spinner(self) -> None:
        """Verify status bar removal and bottom-right global loading spinner architecture."""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # 1. Verify index.html does NOT contain footer#status-bar and DOES contain #global-loading-spinner
        index_html_path = os.path.join(base_dir, "webapp", "frontend", "index.html")
        with open(index_html_path, "r", encoding="utf-8") as f:
            html = f.read()

        self.assertNotIn('<footer id="status-bar">', html)
        self.assertNotIn('id="status-bar"', html)
        self.assertNotIn('id="status-branch"', html)
        self.assertIn('id="global-loading-spinner"', html)
        self.assertIn('class="global-loading-spinner"', html)

        # 2. Verify variables.css does NOT contain --statusbar-height
        var_css_path = os.path.join(base_dir, "webapp", "frontend", "css", "variables.css")
        with open(var_css_path, "r", encoding="utf-8") as f:
            var_css = f.read()
        self.assertNotIn("--statusbar-height", var_css)

        # 3. Verify layout.css contains .global-loading-spinner styles and does NOT contain #status-bar rule
        layout_css_path = os.path.join(base_dir, "webapp", "frontend", "css", "layout.css")
        with open(layout_css_path, "r", encoding="utf-8") as f:
            layout_css = f.read()
        self.assertNotIn("#status-bar {", layout_css)
        self.assertIn(".global-loading-spinner", layout_css)
        self.assertIn("@keyframes spinnerRotate", layout_css)

        # 4. Verify api.js implements activeRequests tracking and anti-flicker timing
        api_js_path = os.path.join(base_dir, "webapp", "frontend", "js", "api.js")
        with open(api_js_path, "r", encoding="utf-8") as f:
            api_js = f.read()
        self.assertIn("this.activeRequests", api_js)
        self.assertIn("_onRequestStart", api_js)
        self.assertIn("_onRequestEnd", api_js)
        self.assertIn("_setSpinnerActive", api_js)
        self.assertIn("global-loading-spinner", api_js)
        # Nothing listens for a loading event: the spinner element is the only consumer
        self.assertNotIn("app:loading", api_js)

    @requires_live_db("reads the live database")
    def test_kconfig_declaration_coordinates(self) -> None:
        """Verify Kconfig symbol detail retrieves declaration file_path and line range with fallback."""
        from webapp.backend.services.kconfig_service import KconfigService
        from webapp.backend.services.symbol_service import SymbolService

        k_svc = KconfigService()
        sym_svc = SymbolService()

        # 1. Kconfig service lookup for SMP
        smp = k_svc.get_symbol_detail("v3.0", "SMP")
        self.assertIsNotNone(smp)
        self.assertEqual(smp["name"], "SMP")
        self.assertIn("file_path", smp)
        self.assertTrue(smp["file_path"].endswith("Kconfig") or smp["file_path"].endswith(".c"))
        self.assertIn("line_s", smp)
        self.assertGreaterEqual(smp["line_s"], 1)

        # 2. Symbol service lookup for CONFIG_SMP delegates to Kconfig declaration coordinates
        sym_res = sym_svc.get_symbol_detail("v3.0", "CONFIG_SMP")
        self.assertIsNotNone(sym_res)
        self.assertEqual(sym_res["type_name"], "kconfig")
        self.assertIn("file_path", sym_res)
        self.assertTrue(sym_res["file_path"].endswith("Kconfig") or sym_res["file_path"].endswith(".c"))
        self.assertIn("line_s", sym_res)

    @requires_live_db("reads the live database")
    def test_projects_and_branches_endpoints(self) -> None:
        """Verify /api/projects, /api/branches, and /api/versions include_rc functionality."""
        from webapp.backend.routers.versions import get_projects, get_branches, get_versions

        # 1. Projects
        projs = get_projects()
        self.assertIn("projects", projs)
        self.assertGreaterEqual(len(projs["projects"]), 1)
        self.assertIn(projs["projects"][0]["name"], ("linux", "Linux Kernel"))
        self.assertIn("tag_views", projs["projects"][0])
        self.assertGreaterEqual(len(projs["projects"][0]["tag_views"]), 1)

        # 2. Branches
        branches = get_branches()
        self.assertIn("branches", branches)
        self.assertGreaterEqual(len(branches["branches"]), 1)
        self.assertIn("tag_views", branches)
        self.assertGreaterEqual(len(branches["tag_views"]), 1)
        branch_names = [b["name"] for b in branches["branches"]]
        self.assertIn("master", branch_names)

        # 3. Versions with include_rc
        vers_standard = get_versions(include_rc=False)
        self.assertIn("versions", vers_standard)

        vers_rc = get_versions(include_rc=True)
        self.assertIn("versions", vers_rc)
        self.assertGreaterEqual(len(vers_rc["versions"]), len(vers_standard["versions"]))

    def test_anchored_bidirectional_timeline(self) -> None:
        """Verify commit timeline anchors on version tag and supports past and future directions."""
        from webapp.backend.services.git_service import GitService
        v30 = require_indexed_ref("v3.0")
        git_svc = GitService()

        # 1. Past direction (commits leading up to or before anchor)
        past_res = git_svc.get_commit_timeline("v3.0", limit=15, direction="past")
        self.assertEqual(past_res["version"], "v3.0")
        self.assertEqual(past_res["direction"], "past")
        self.assertGreater(len(past_res["timeline"]), 0)
        # The past timeline starts at the anchor: the version's own commit
        self.assertEqual(past_res["timeline"][0]["commit_id"], v30)
        self.assertEqual(past_res["timeline"][0]["subject"], "Linux 3.0")
        # All commits in past timeline must be <= anchor commit_id
        for c in past_res["timeline"]:
            self.assertLessEqual(c["commit_id"], v30)

        # 2. Future direction (commits after anchor)
        future_res = git_svc.get_commit_timeline("v3.0", limit=15, direction="future")
        self.assertEqual(future_res["version"], "v3.0")
        self.assertEqual(future_res["direction"], "future")
        self.assertGreater(len(future_res["timeline"]), 0)
        # All commits in future timeline must be >= anchor commit_id
        for c in future_res["timeline"]:
            self.assertGreaterEqual(c["commit_id"], v30)

    def test_commit_level_version_info_resolution(self) -> None:
        """Verify get_version_info resolves tags, -rc tags, and arbitrary commit SHAs via git rev-parse."""
        from webapp.backend.database.helpers import get_version_info

        v30 = require_indexed_ref("v3.0")
        with get_reader() as cursor:
            # 1. Standard indexed tag
            cid, vname = get_version_info(cursor, "v3.0")
            self.assertEqual(cid, v30)
            self.assertEqual(vname, "v3.0")

            # 2. Git release candidate tag fallback
            rc_cid, rc_vname = get_version_info(cursor, "v3.0-rc1")
            self.assertEqual(rc_vname, "v3.0-rc1")
            self.assertIsInstance(rc_cid, int)

            # 3. Direct commit SHA
            sha = "02f8c6aee8df3cdc935e9bdd4f2d020306035dbe"
            sha_cid, sha_vname = get_version_info(cursor, sha)
            self.assertEqual(sha_cid, v30)
            self.assertEqual(sha_vname.lower(), sha.lower())

    @requires_live_db("reads the live database")
    def test_file_history_and_last_changed(self) -> None:
        """Verify file history and last changed metadata retrieval via git_service and filesystem_service."""
        from webapp.backend.services.git_service import GitService
        from webapp.backend.services.filesystem_service import FilesystemService

        git_svc = GitService()
        fs_svc = FilesystemService()

        # 1. Test GitService.get_file_history
        history = git_svc.get_file_history("v3.0", "init/main.c", limit=10)
        self.assertEqual(history["version"], "v3.0")
        self.assertEqual(history["path"], "init/main.c")
        self.assertGreater(len(history["commits"]), 0)
        self.assertIsNotNone(history["last_changed"])
        self.assertIn("commit_hash", history["last_changed"])
        self.assertIn("author_name", history["last_changed"])
        self.assertIn("author_date_iso", history["last_changed"])
        self.assertIn("subject", history["last_changed"])
        self.assertGreater(len(history["commits"]), 0)

        # 2. Test GitService.get_file_last_changed
        last_changed = git_svc.get_file_last_changed("v3.0", "init/main.c")
        self.assertIsNotNone(last_changed)
        self.assertEqual(last_changed["commit_hash"], history["last_changed"]["commit_hash"])

        # 3. Test FilesystemService.get_file contains file_info.last_changed
        file_res = fs_svc.get_file("v3.0", "init/main.c")
        self.assertIn("file_info", file_res)
        self.assertIn("last_changed", file_res["file_info"])
        self.assertIsNotNone(file_res["file_info"]["last_changed"])
        self.assertEqual(file_res["file_info"]["last_changed"]["commit_hash"], history["last_changed"]["commit_hash"])


class _LadderReader:
    """Read session double over an in-memory AST where node k holds node k + 1 twice (content-addressed sharing):
    expanded to depth d it serializes to 2^(d+1) - 1 positions from only d + 1 distinct nodes."""

    def __init__(self, levels: int) -> None:
        self.levels = levels
        self.queries = 0

    def __enter__(self) -> "_LadderReader":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def select_rows(self, source, columns, where=None, **kw) -> list[dict]:
        self.queries += 1
        if source == "m_ast_container":
            return [{"ast_id": i, "ref_ast_id": i + 1, "priority": p}
                    for i in where["ast_id"] if i < self.levels for p in (0, 1)]
        if isinstance(source, tuple) and source[0] == "m_ast":
            return [{"ast_id": i, "name": f"n{i}", "type_id": 1, "type_name": "Block"}
                    for i in where["a.ast_id"] if i <= self.levels]
        return []  # m_tag: the nodes carry no tag


class TestAstTreeBudget(unittest.TestCase):
    """GET /api/symbols/{v}/ast/{id}: the answer never serializes more than MAX_AST_TREE_NODES nodes."""

    def _tree(self, depth: int, levels: int = 12) -> tuple[dict, _LadderReader]:
        from unittest.mock import patch
        reader = _LadderReader(levels)
        with patch("webapp.backend.services.symbol_service.get_reader", return_value=reader):
            return SymbolService().get_ast_tree(0, depth), reader

    @staticmethod
    def _positions(node: dict) -> int:
        return 1 + sum(TestAstTreeBudget._positions(c) for c in node["children"])

    @staticmethod
    def _truncated(node: dict) -> bool:
        return bool(node.get("truncated")) or any(TestAstTreeBudget._truncated(c) for c in node["children"])

    def test_shared_subtrees_serialize_in_full_within_budget(self) -> None:
        tree, cursor = self._tree(depth=10)
        self.assertEqual(self._positions(tree), 2 ** 11 - 1)
        self.assertFalse(self._truncated(tree))
        self.assertNotIn("containers", tree)
        # One container read, one node read and one tag read per level, not one per node
        self.assertLessEqual(cursor.queries, 3 * 10 + 2)

    def test_budget_bounds_positions_at_any_depth(self) -> None:
        from unittest.mock import patch
        with patch("webapp.backend.services.symbol_service.MAX_AST_TREE_NODES", 1000):
            tree, _ = self._tree(depth=12)
        self.assertLessEqual(self._positions(tree), 1000)
        self.assertTrue(self._truncated(tree))


if __name__ == "__main__":
    unittest.main()


