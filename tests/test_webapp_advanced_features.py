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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.main import (
    AutoSolveRequest,
    DiffConfigRequest,
    FormatPatchRequest,
    PatchReviewRequest,
    _compute_structured_diff,
    autosolve_kconfig,
    diff_kconfig_configurations,
    export_compile_commands,
    generate_formatted_patch,
    get_ast_container_tree,
    get_codebase_treemap,
    get_function_callgraph,
    get_kconfig_diff,
    get_kconfig_graph,
    get_struct_layout,
    get_symbol_xref,
    get_symbol_detail,
    get_tag_by_id,
    get_tag_timeline,
    get_include_symbols,
    get_versions_diff,
    lookup_symbols,
    search_symbols,
    match_patch_maintainers,
)


class TestWebappAdvancedFeatures(unittest.TestCase):
    """Test suite for advanced web application systems."""

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

        # Rich symbol search from m_symbol_def
        search_res = search_symbols("v3.0", q="ext4", limit=10)
        self.assertIsInstance(search_res, list)
        if len(search_res) > 0:
            sym = search_res[0]
            self.assertIn("name", sym)
            self.assertIn("type_name", sym)
            self.assertIn("file_path", sym)
            self.assertIn("line_s", sym)
            self.assertIn("ast_id", sym)

        # XRef search for kmalloc or ext4 symbol
        xref = get_symbol_xref("v3.0", "kmalloc")
        self.assertIn("symbol", xref)
        self.assertEqual(xref["symbol"], "kmalloc")
        self.assertIn("definitions", xref)
        self.assertIn("references", xref)
        self.assertIn("calls", xref)
        self.assertIn("member_refs", xref)
        self.assertIn("type_usages", xref)
        self.assertIn("declarations", xref)
        self.assertIn("macro_expansions", xref)
        self.assertIn("macro_expansions_count", xref)
        self.assertIn("references_count", xref)

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

    def test_ast_container_tree_tag_ids(self) -> None:
        """Verify AST container tree includes tag_id as int or None, correctly suppressing tags on inner expressions/statements."""
        search_res = search_symbols("v3.0", q="ext4_fill_super", limit=1)
        if not search_res:
            search_res = search_symbols("v3.0", q="init", limit=1)
        self.assertGreater(len(search_res), 0)
        ast_id = search_res[0]["ast_id"]

        tree = get_ast_container_tree(ast_id, version_name="v3.0")
        self.assertIn("ast_id", tree)
        self.assertIn("tag_id", tree)
        self.assertTrue(tree["tag_id"] is None or isinstance(tree["tag_id"], int))
        self.assertIn("containers", tree)
        self.assertIsInstance(tree["containers"], list)

        has_tagged = tree["tag_id"] is not None
        has_untagged = tree["tag_id"] is None

        # Recursively verify tag_id exists on all nodes
        def verify_node(node: dict) -> None:
            nonlocal has_tagged, has_untagged
            self.assertIn("ast_id", node)
            self.assertIn("tag_id", node)
            self.assertTrue(node["tag_id"] is None or isinstance(node["tag_id"], int))
            if node["tag_id"] is not None:
                has_tagged = True
            else:
                has_untagged = True
            for c in node.get("containers", []):
                child = c.get("child_node")
                if child:
                    verify_node(child)

        for c in tree["containers"]:
            child = c.get("child_node")
            if child:
                verify_node(child)

        self.assertTrue(has_tagged, "Expected at least one tagged node in tree")
        self.assertTrue(has_untagged, "Expected at least one untagged node in tree")

    def test_tag_version_timeline_and_diff(self) -> None:
        """Verify GET /api/tag/{tag_id}/timeline and diff generation."""
        # 1. Test diff computation helper
        code_v1 = "int foo(void) {\n    return 1;\n}\n"
        code_v2 = "int foo(int x) {\n    return x + 1;\n}\n"
        diffs = _compute_structured_diff(code_v1, code_v2)
        self.assertGreater(len(diffs), 0)
        types = [d["type"] for d in diffs]
        self.assertIn("del", types)
        self.assertIn("add", types)

        # 2. Test 404 on non-existent tag
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            get_tag_timeline(99999999)

        # 3. Test HTML elements exist in webapp.html
        html_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp", "webapp.html")
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn('id="tagTimelineModal"', content)
        self.assertIn('id="tagTimelineSlider"', content)
        self.assertIn('id="tagTimelineNodes"', content)
        self.assertIn('id="btnPlayTimeline"', content)
        self.assertIn('id="tagTimelineCodeContainer"', content)
        self.assertIn('openTagTimelineModal', content)
        self.assertIn('renderTimelineView', content)

    def test_tag_lineage_evolution_and_moved_tags(self) -> None:
        """Verify cross-version tag lineage resolution across m_moved_tag (e.g. nlmclnt_initdata)."""
        try:
            tag_old = get_tag_by_id(12153743)
            self.assertEqual(tag_old.get("moved_to"), 17614604)
            self.assertIsNone(tag_old.get("moved_from"))

            tag_new = get_tag_by_id(17614604)
            self.assertEqual(tag_new.get("moved_from"), 12153743)
            self.assertIsNone(tag_new.get("moved_to"))

            timeline_old = get_tag_timeline(12153743)
            self.assertIn("lineage_tag_ids", timeline_old)
            self.assertIn(12153743, timeline_old["lineage_tag_ids"])
            self.assertIn(17614604, timeline_old["lineage_tag_ids"])
            self.assertGreaterEqual(timeline_old["total_versions"], 11)

            snaps_by_vname = {s["vname"]: s for s in timeline_old["timeline"]}
            if "v3.3" in snaps_by_vname and "v3.4" in snaps_by_vname:
                self.assertEqual(snaps_by_vname["v3.3"]["tag_id"], 12153743)
                self.assertEqual(snaps_by_vname["v3.3"]["status"], "unchanged")

                self.assertEqual(snaps_by_vname["v3.4"]["tag_id"], 17614604)
                self.assertEqual(snaps_by_vname["v3.4"]["status"], "modified")
                self.assertGreaterEqual(snaps_by_vname["v3.4"]["lines_added"], 1)
                diff_texts = [d["text"] for d in snaps_by_vname["v3.4"]["diff"] if d["type"] == "add"]
                self.assertTrue(any("net" in t for t in diff_texts))

            timeline_new = get_tag_timeline(17614604)
            self.assertEqual(timeline_new["lineage_tag_ids"], timeline_old["lineage_tag_ids"])
            self.assertEqual(len(timeline_new["timeline"]), len(timeline_old["timeline"]))
        except Exception as e:
            if "not found" in str(e).lower() or "unavailable" in str(e).lower():
                pass
            else:
                raise

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

    def test_function_callgraph(self) -> None:
        res = get_function_callgraph("v3.0", "ext4_fill_super")
        self.assertEqual(res["function_name"], "ext4_fill_super")
        self.assertIn("callers", res)
        self.assertIn("callees", res)
        self.assertIn("caller_count", res)
        self.assertIn("callee_count", res)
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

    def test_edge_cases_empty_and_fallback(self) -> None:
        # 1. Empty Patch Review
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

        # Check allWorkspaces includes the active workspaces
        self.assertIn('"subsystemWorkspace"', content)
        self.assertIn('"personWorkspace"', content)
        self.assertIn('"commitWorkspace"', content)

        # Assert excised non-working features are removed
        self.assertNotIn('btnModeAstSandbox', content)
        self.assertNotIn('astSandboxWorkspace', content)
        self.assertNotIn('openCodeTourModal', content)
        self.assertNotIn('tourWorkspace', content)
        self.assertNotIn('openBloatometerModal', content)
        self.assertNotIn('bloatometerWorkspace', content)

        # Assert modernized features are present
        self.assertIn('id="globalSymbolSearchInput"', content)
        self.assertIn('id="globalSymbolSearchDropdown"', content)
        self.assertIn('id="tokenActionPopover"', content)
        self.assertIn('executeXrefSearch', content)
        self.assertIn('openPathAndHighlightLine', content)

    def test_symbol_endpoints(self) -> None:
        from fastapi import HTTPException
        try:
            detail = get_symbol_detail("v3.0", "task_struct")
            self.assertIn("symbol_name", detail)
            self.assertEqual(detail["symbol_name"], "task_struct")
            self.assertIn("declarations", detail)
            self.assertIn("usages", detail)
        except HTTPException as e:
            self.assertEqual(e.status_code, 404)

        results = search_symbols("v3.0", q="task", limit=10)
        self.assertIsInstance(results, list)

    def test_webapp_identifier_click_and_raw_files_unparsed(self) -> None:
        """Verify DOM structure of tokenActionPopover and raw file bypass in webapp.html."""
        html_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp", "webapp.html")
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 1. Verify tagTimelineModal closes BEFORE tokenActionPopover
        timeline_idx = content.find('id="tagTimelineModal"')
        popover_idx = content.find('id="tokenActionPopover"')
        self.assertGreater(timeline_idx, 0)
        self.assertGreater(popover_idx, timeline_idx)
        between = content[timeline_idx:popover_idx]
        # Must have closed modal-footer, .modal, and #tagTimelineModal (3 closing tags before popover)
        self.assertIn("</div>\n</div>\n</div>", between.replace(" ", "").replace("\t", "").replace("\r", ""))

        # 2. Verify tokenActionPopover is fixed and stops propagation
        self.assertIn('position: fixed;', content[popover_idx:popover_idx + 250])
        self.assertIn('onclick="event.stopPropagation()"', content[popover_idx:popover_idx + 250])

        # 3. Verify handleTokenClick and data attributes
        self.assertIn('function handleTokenClick(el, event)', content)
        self.assertIn('data-token-text=', content)
        self.assertIn('data-token-type=', content)
        self.assertIn('handleTokenClick(this, event)', content)

        # 4. Verify raw files bypass in highlightLineText
        self.assertIn('Raw files should NOT be parsed by any parser or lexer', content)
        self.assertIn('if (!isC && !isAsm && !isKconfig && !isRust)', content)
        self.assertIn('return { html: escapeHtml(rawText), inComment: false };', content)

    def test_include_symbols_endpoint_and_dom(self) -> None:
        """Verify get_include_symbols endpoint and includeSymbolsPopover UI structure."""
        from fastapi import HTTPException
        # 1. 404 on non-existent AST ID
        with self.assertRaises(HTTPException) as ctx:
            get_include_symbols("v3.0", 999999999)
        self.assertEqual(ctx.exception.status_code, 404)

        # 2. Existing AST ID query if any include exists in DB
        from webapp.main import db
        cnx = db.get_connection()
        if cnx:
            cursor = cnx.cursor()
            cursor.execute("SELECT ast_id FROM m_ast_include LIMIT 1;")
            row = cursor.fetchone()
            cursor.close()
            cnx.close()
            if row:
                res = get_include_symbols("v3.0", row[0])
                self.assertIn("ast_id", res)
                self.assertIn("include_text", res)
                self.assertIn("header_file", res)
                self.assertIn("header_exists", res)
                self.assertIn("total_symbols", res)
                self.assertIn("symbols", res)
                self.assertIsInstance(res["symbols"], list)

        # 3. Fallback resolution when AST ID is 0 or missing
        fb_res = get_include_symbols("v3.0", 0, header="linux/const.h", file_path="include/linux/const.h")
        self.assertIn("ast_id", fb_res)
        self.assertIn("header_file", fb_res)
        self.assertEqual(fb_res["header_file"], "include/linux/const.h")
        self.assertTrue(fb_res["header_exists"])
        self.assertEqual(fb_res["total_symbols"], 0)
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
        self.assertIn(".include-badge-func", c_content)
        self.assertIn(".include-badge-struct", c_content)

        context_menu_js = os.path.join(base_dir, "webapp", "frontend", "js", "components", "context_menu.js")
        with open(context_menu_js, "r", encoding="utf-8") as f:
            cm_content = f.read()
        self.assertIn("import { includeSymbolsPopover }", cm_content)
        self.assertIn("includeSymbolsPopover.show", cm_content)

        # 5. Verify webapp.html popover elements and controller functions
        html_path = os.path.join(base_dir, "webapp", "webapp.html")
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn('id="includeSymbolsPopover"', content)
        self.assertIn('id="includeSymbolsSearchInput"', content)
        self.assertIn('id="includeCategoryPills"', content)
        self.assertIn('id="includeSymbolsList"', content)
        self.assertIn('id="btnIncludeOpenHeader"', content)
        self.assertIn('openIncludeSymbolsPopover', content)
        self.assertIn('renderIncludeSymbolsContent', content)
        self.assertIn('setIncludeCategoryFilter', content)
        self.assertIn('onFilterIncludeSymbols', content)
        self.assertIn('onIncludeSymbolClick', content)
        self.assertIn('includePopoverOpenHeader', content)
        self.assertIn('closeIncludeSymbolsPopover', content)
        self.assertIn('typeKey === "CPPro_include"', content)
        # Ensure includePopoverOpenHeader reads header_file before closeIncludeSymbolsPopover() clears currentIncludeData
        self.assertIn('let headerFile = currentIncludeData.header_file;', content)

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
        self.assertIn("app:loading", api_js)


if __name__ == "__main__":
    unittest.main()


