"""tests/test_webapp_advanced_features.py - Unit & Integration Tests for 14 Advanced WebApp Systems.

Tests:
1. Cross-Version File & Kconfig Diff
2. Global Symbol XRef & Autocomplete Lookup
3. Interactive Canvas DAG Graph Payload
4. Kconfig Auto-Solver & Config Diff
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
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.main import (
    get_versions_diff,
    get_kconfig_diff,
    get_symbol_xref,
    lookup_symbols,
    get_kconfig_graph,
    autosolve_kconfig,
    diff_kconfig_configurations,
    match_patch_maintainers,
    query_ast_semantic_sandbox,
    export_compile_commands,
    get_struct_layout,
    get_codebase_treemap,
    estimate_kconfig_footprint,
    get_function_callgraph,
    get_code_tour_presets,
    generate_formatted_patch,
    AutoSolveRequest,
    DiffConfigRequest,
    PatchReviewRequest,
    AstQueryRequest,
    FootprintRequest,
    FormatPatchRequest,
)


class TestWebappAdvancedFeatures(unittest.TestCase):
    """Test suite for all 14 advanced web application systems."""

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

    def test_symbol_xref_and_lookup(self) -> None:
        # Lookup autocomplete
        lookup = lookup_symbols("v3.0", q="ext4", limit=10)
        self.assertIsInstance(lookup, list)

        # XRef search for kmalloc or ext4 symbol
        xref = get_symbol_xref("v3.0", "kmalloc")
        self.assertIn("symbol", xref)
        self.assertEqual(xref["symbol"], "kmalloc")
        self.assertIn("definitions", xref)
        self.assertIn("references", xref)

    def test_kconfig_dag_graph(self) -> None:
        # Graph for EXT4_FS
        graph = get_kconfig_graph("v3.0", "EXT4_FS", depth=2)
        self.assertIn("nodes", graph)
        self.assertIn("edges", graph)
        self.assertGreaterEqual(len(graph["nodes"]), 1)

        root = next((n for n in graph["nodes"] if n["id"] == "EXT4_FS"), None)
        self.assertIsNotNone(root)
        self.assertTrue(root["is_root"])

    def test_kconfig_autosolve_and_config_diff(self) -> None:
        # Autosolve for EXT4_FS
        req = AutoSolveRequest(target_symbol="EXT4_FS", current_values={})
        res = autosolve_kconfig("v3.0", req)
        self.assertTrue(res["solution_found"])
        self.assertIn("toggles_needed", res)

        # Config Diff
        diff_req = DiffConfigRequest(
            active_config={"EXT4_FS": "y", "BTRFS_FS": "n"},
            custom_config={"EXT4_FS": "y", "BTRFS_FS": "y"},
        )
        diff_res = diff_kconfig_configurations("v3.0", diff_req)
        self.assertEqual(diff_res["matching_symbols"], 1)
        self.assertEqual(diff_res["mismatched_symbols"], 1)

    def test_patch_reviewer_maintainers_matcher(self) -> None:
        sample_patch = """diff --git a/fs/ext4/super.c b/fs/ext4/super.c
--- a/fs/ext4/super.c
+++ b/fs/ext4/super.c
@@ -10,6 +10,12 @@
+/* Added sample comment for patch reviewer test */
+int test_func(void) { return 0; }
"""
        req = PatchReviewRequest(patch_text=sample_patch)
        res = match_patch_maintainers("v3.0", req)
        self.assertEqual(res["touched_files_count"], 1)
        self.assertGreater(len(res["files"]), 0)
        self.assertEqual(res["files"][0]["file_path"], "fs/ext4/super.c")
        self.assertIn("suggested_to", res)
        self.assertIn("suggested_cc", res)

    def test_ast_semantic_query_sandbox(self) -> None:
        req = AstQueryRequest(path_prefix="fs/ext4/", limit=20)
        res = query_ast_semantic_sandbox("v3.0", req)
        self.assertIn("total", res)
        self.assertIn("items", res)
        self.assertGreater(len(res["items"]), 0)
        self.assertTrue(res["items"][0]["file_path"].startswith("fs/ext4/"))

    def test_clang_compile_commands_exporter(self) -> None:
        cmds = export_compile_commands("v3.0", arch="x86")
        self.assertIsInstance(cmds, list)
        self.assertGreater(len(cmds), 0)
        self.assertIn("directory", cmds[0])
        self.assertIn("command", cmds[0])
        self.assertIn("file", cmds[0])
        self.assertTrue(cmds[0]["file"].endswith(".c"))

    def test_struct_layout_pahole(self) -> None:
        res = get_struct_layout("v3.0", "task_struct")
        self.assertIn("total_size", res)
        self.assertIn("alignment", res)
        self.assertIn("members", res)
        self.assertIn("cache_lines_used", res)
        self.assertIn("optimization", res)
        self.assertGreater(res["total_size"], 0)



    def test_codebase_treemap(self) -> None:
        tree = get_codebase_treemap("v3.0", max_depth=3)
        self.assertIn("name", tree)
        self.assertIn("children", tree)
        self.assertGreater(len(tree["children"]), 0)

    def test_kconfig_footprint_bloatometer(self) -> None:
        req = FootprintRequest(kconfig_values={"EXT4_FS": "y", "NET": "y"})
        res = estimate_kconfig_footprint("v3.0", req)
        self.assertIn("active_symbols_count", res)
        self.assertIn("total_compiled_files", res)
        self.assertIn("estimated_loc", res)
        self.assertIn("estimated_binary_kb", res)
        self.assertEqual(res["active_symbols_count"], 2)

    def test_function_callgraph(self) -> None:
        res = get_function_callgraph("v3.0", "ext4_fill_super")
        self.assertEqual(res["function_name"], "ext4_fill_super")
        self.assertIn("callers", res)
        self.assertIn("callees", res)

    def test_code_tour_presets(self) -> None:
        presets = get_code_tour_presets("v3.0")
        self.assertIsInstance(presets, list)
        self.assertGreater(len(presets), 0)
        self.assertIn("steps", presets[0])
        self.assertGreater(len(presets[0]["steps"]), 0)

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

    def test_edge_cases_empty_and_fallback(self) -> None:
        # 1. Empty Kconfig Footprint
        empty_footprint = estimate_kconfig_footprint("v3.0", FootprintRequest(kconfig_values={}))
        self.assertEqual(empty_footprint["active_symbols_count"], 0)
        self.assertEqual(empty_footprint["total_compiled_files"], 0)
        self.assertEqual(empty_footprint["estimated_loc"], 0)

        # 2. Empty Patch Review
        empty_patch = match_patch_maintainers("v3.0", PatchReviewRequest(patch_text=""))
        self.assertEqual(empty_patch["touched_files_count"], 0)
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

        # 4. Unknown Struct Fallback
        struct_res = get_struct_layout("v3.0", "unknown_custom_struct")
        self.assertIn("total_size", struct_res)
        self.assertGreater(struct_res["total_size"], 0)
        self.assertIn("members", struct_res)

        # 5. Alternate Architectures for Clang Database
        arm_cmds = export_compile_commands("v3.0", arch="arm")
        self.assertGreater(len(arm_cmds), 0)
        self.assertIn("arch/arm/include", arm_cmds[0]["command"])

        # 7. Non-existent Function Callgraph
        empty_callgraph = get_function_callgraph("v3.0", "non_existent_fn_xyz")
        self.assertEqual(empty_callgraph["caller_count"], 0)
        self.assertEqual(empty_callgraph["callee_count"], 0)

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
        g1 = get_kconfig_graph("v3.0", "EXT4_FS", depth=2)
        g2 = get_kconfig_graph("v3.0", "BTRFS_FS", depth=2)
        self.assertGreater(len(g1["nodes"]), 0)
        self.assertGreater(len(g2["nodes"]), 0)

    def test_webapp_html_tabs_and_modals(self) -> None:
        html_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp", "webapp.html")
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check dedicated workspaces exist in DOM
        self.assertIn('id="subsystemWorkspace"', content)
        self.assertIn('id="personWorkspace"', content)
        self.assertIn('id="commitWorkspace"', content)
        self.assertIn('id="dagWorkspace"', content)
        self.assertIn('id="structWorkspace"', content)

        # Check openModalAsNewTab has cases for subsystem, person, commit
        self.assertIn('case "subsystemModal":', content)
        self.assertIn('case "personModal":', content)
        self.assertIn('case "commitModal":', content)

        # Check tab title formatters
        self.assertIn('case "subsystem":', content)
        self.assertIn('case "person":', content)
        self.assertIn('case "commit":', content)

        # Check allWorkspaces includes the workspaces
        self.assertIn('"subsystemWorkspace"', content)
        self.assertIn('"personWorkspace"', content)
        self.assertIn('"commitWorkspace"', content)


if __name__ == "__main__":
    unittest.main()

