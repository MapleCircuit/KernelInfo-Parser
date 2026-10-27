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

from tests import require_indexed_ref
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
    get_codebase_treemap,
    get_function_callgraph,
    get_kconfig_diff,
    get_kconfig_graph,
    get_struct_layout,
    get_symbol_xref,
    get_symbol_detail,
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

        # 3. The release slider lives in the modular frontend (the legacy webapp.html page is gone)
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

    def test_projects_and_branches_endpoints(self) -> None:
        """Verify /api/projects, /api/branches, and /api/versions include_rc functionality."""
        from webapp.backend.routers.versions import get_projects, get_branches, get_versions

        # 1. Projects
        projs = get_projects()
        self.assertIn("projects", projs)
        self.assertGreaterEqual(len(projs["projects"]), 1)
        self.assertEqual(projs["projects"][0]["name"], "Linux Kernel")

        # 2. Branches
        branches = get_branches()
        self.assertIn("branches", branches)
        self.assertGreaterEqual(len(branches["branches"]), 1)
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
        self.assertIsNotNone(past_res["anchor_commit"])
        self.assertEqual(past_res["anchor_commit"]["commit_id"], v30)
        self.assertEqual(past_res["anchor_commit"]["subject"], "Linux 3.0")
        self.assertGreater(len(past_res["timeline"]), 0)
        # All commits in past timeline must be <= anchor commit_id
        for c in past_res["timeline"]:
            self.assertLessEqual(c["commit_id"], v30)

        # 2. Future direction (commits after anchor)
        future_res = git_svc.get_commit_timeline("v3.0", limit=15, direction="future")
        self.assertEqual(future_res["version"], "v3.0")
        self.assertEqual(future_res["direction"], "future")
        self.assertIsNotNone(future_res["anchor_commit"])
        self.assertEqual(future_res["anchor_commit"]["commit_id"], v30)
        self.assertGreater(len(future_res["timeline"]), 0)
        # All commits in future timeline must be >= anchor commit_id
        for c in future_res["timeline"]:
            self.assertGreaterEqual(c["commit_id"], v30)

    def test_commit_level_version_info_resolution(self) -> None:
        """Verify get_version_info resolves tags, -rc tags, and arbitrary commit SHAs via git rev-parse."""
        from webapp.backend.database.pool import get_db_cursor
        from webapp.backend.database.helpers import get_version_info

        v30 = require_indexed_ref("v3.0")
        with get_db_cursor() as cursor:
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
        self.assertGreater(history["total_commits"], 0)
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


if __name__ == "__main__":
    unittest.main()


