"""unit_test/test_c_parser.py - Unit Test Suite for C & Preprocessor AST Parser with Snapshots.

Validates C-AST parser lifecycle operations (Added, Modified changed, Exact rename)
using real Linux kernel git tree files and order-agnostic gold-master snapshot verification.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from typing import Any

from core.globalstuff import (
    G,
    T_C,
)
from core.GreatProcessor import GreatProcessor
from core.FileHandler import MasterFile
from core.TableHandling import ChangeSet
from core.DBLayout import (
    init_db_layout,
    m_v_main,
    m_file_name,
    m_file,
    m_bridge_file,
    m_ast,
    m_tag,
    m_moved_tag,
    m_bridge_tag,
    m_tag_code,
    m_map_ast,
    m_bridge_map,
)
from db_engine import MockDB
from table_engine import TECachedDB
from parser.c_ast.c_ast import c_ast_parse
from parser.c_ast.cursor_tree import clear_tu_caches
from core.config import get_default_repo_path
from unit_test.harness import (
    FeatureRegistry,
    test_feature,
    test_step,
)
from unit_test.parser_harness import (
    stage_file_prelude,
    extract_changeset_snapshot,
    assert_snapshot_matches,
)


class BaseCParserTestCase(unittest.TestCase):
    """Base test fixture providing isolated DB, TableEngine, and MasterFile with tempdir support."""

    feature_registry: FeatureRegistry
    gp: GreatProcessor
    mf: MasterFile
    te: TECachedDB

    @classmethod
    def setUpClass(cls) -> None:
        cls.feature_registry = FeatureRegistry()

    def setUp(self) -> None:
        MockDB.reset()
        G.DB = MockDB
        self.te = TECachedDB()
        G.TE = self.te
        self.gp = GreatProcessor()
        init_db_layout(self.gp)
        self.te.start(self.gp.Table_Array, G.DB)

        self.mf = MasterFile()
        G.MF = self.mf

    def tearDown(self) -> None:
        if hasattr(self, "te") and self.te:
            self.te.close()
        if hasattr(self, "mf") and self.mf:
            self.mf.clear_all_version()
        clear_tu_caches()
        G.CURRENT_PARSING_FILE = None
        G.CURRENT_PARSING_DIR = None

    def prepare_c_file(
        self,
        version: str,
        dest_path: str,
        src_path: str | None = None,
        repo_path: str | None = None,
    ) -> str:
        """Extract git blob directly into temporary disk directory for libclang parsing."""
        if version not in self.mf.version_dict:
            self.mf.version_dict[version] = self.mf.create_temp_dir()
        temp_dir = self.mf.version_dict[version]
        full_dest = os.path.join(temp_dir, dest_path)
        os.makedirs(os.path.dirname(full_dest), exist_ok=True)
        git_target = src_path or dest_path
        content = subprocess.check_output(
            ["git", "-C", repo_path or str(get_default_repo_path()), "show", f"{version}:{git_target}"],
            stderr=subprocess.PIPE,
        )
        with open(full_dest, "wb") as f:
            f.write(content)
        return full_dest


class Test_CParserLifecycle(BaseCParserTestCase):
    """Lifecycle verification for C & Preprocessor source/header files across Git change types."""

    @test_feature("c_ast_added_file")
    def test_01_added_c_file(self) -> None:
        """Verify adding a C header stages AST nodes, tags, bridges, maps, symbols, and matches snapshot."""
        file_path = "include/linux/lockd/bind.h"
        self.gp.Version_Name = "v3.0"
        self.gp.VID = 1
        self.gp.Old_VID = 0

        with test_step(f"Prepare C source on disk for libclang: {file_path}"):
            self.prepare_c_file(self.gp.Version_Name, file_path)

        with test_step(f"Initialize ChangeSet for Added file: {file_path}"):
            cs = ChangeSet(f"A\t{file_path}")
            cs.gp = self.gp
            cs.mf = self.mf
            cs.current_vid = self.gp.VID
            G.CURRENT_PARSING_FILE = file_path

            # Version and file prelude registration
            cs.store(m_v_main.set(1, self.gp.Version_Name))
            stage_file_prelude(cs, self.gp)

        with test_step("Execute c_ast_parse and ChangeSet resolution"):
            c_ast_parse(cs)
            exec_ok = cs.execute()
            self.assertTrue(exec_ok, "ChangeSet execution should succeed for added C file")
            self.te.commit_all()

        with test_step("Extract ChangeSet snapshot and verify against baseline"):
            test_conf = {
                "parser": "c_ast",
                "test_name": "added_file",
                "file_operation": "A",
                "file_path": file_path,
                "versions": [self.gp.Version_Name],
                "description": "Verify added C header stages AST nodes, tags, bridges, maps, symbol defs/refs, and matches snapshot",
            }
            snapshot = extract_changeset_snapshot(cs, test_conf=test_conf)

            # Assert expected schema components are populated
            tables = snapshot["tables"]
            self.assertIn("m_ast", tables)
            self.assertIn("m_tag", tables)
            self.assertIn("m_tag_code", tables)
            self.assertIn("m_bridge_tag", tables)
            self.assertIn("m_map_ast", tables)
            self.assertIn("m_bridge_map", tables)
            self.assertIn("m_symbol_def", tables)
            self.assertIn("m_symbol_ref", tables)

            assert_snapshot_matches(self, "c_ast", "added_file", snapshot)

    @test_feature("c_ast_modified_changed_file")
    def test_02_modified_changed_c_file(self) -> None:
        """Verify modifying a C header records prior tags, links m_moved_tag, recycles unchanged tags, and matches snapshot."""
        file_path = "include/linux/lockd/bind.h"

        # --- Stage 1: Initial baseline version (v2.6.28) ---
        self.gp.Version_Name = "v2.6.28"
        self.gp.VID = 1
        self.gp.Old_VID = 0

        with test_step(f"Prepare initial version file on disk: {file_path} (v2.6.28)"):
            self.prepare_c_file(self.gp.Version_Name, file_path)

        with test_step("Parse and commit initial baseline ChangeSet (v2.6.28)"):
            cs1 = ChangeSet(f"A\t{file_path}")
            cs1.gp = self.gp
            cs1.mf = self.mf
            cs1.current_vid = 1
            G.CURRENT_PARSING_FILE = file_path
            cs1.store(m_v_main.set(1, self.gp.Version_Name))
            stage_file_prelude(cs1, self.gp)
            c_ast_parse(cs1)
            ok1 = cs1.execute()
            self.assertTrue(ok1, "Initial baseline ChangeSet execution must succeed")
            self.te.commit_all()

        # --- Stage 2: Modified version (v2.6.29) ---
        # Commit 0cb2659b added 'int noresvport;' to struct nlmclnt_initdata
        self.gp.Version_Name = "v2.6.29"
        self.gp.VID = 2
        self.gp.Old_VID = 1

        with test_step(f"Prepare modified version file on disk: {file_path} (v2.6.29)"):
            self.prepare_c_file(self.gp.Version_Name, file_path)

        with test_step("Parse modified ChangeSet (v2.6.29)"):
            cs2 = ChangeSet(f"M\t{file_path}")
            cs2.gp = self.gp
            cs2.mf = self.mf
            cs2.current_vid = 2
            G.CURRENT_PARSING_FILE = file_path
            cs2.store(m_v_main.set(2, self.gp.Version_Name))
            stage_file_prelude(cs2, self.gp)
            c_ast_parse(cs2)
            ok2 = cs2.execute()
            self.assertTrue(ok2, "Modified ChangeSet execution must succeed")
            self.te.commit_all()

        with test_step("Extract accumulated multi-version snapshot and verify"):
            test_conf = {
                "parser": "c_ast",
                "test_name": "modified_changed_file",
                "file_operation": "M",
                "file_path": file_path,
                "versions": ["v2.6.28", "v2.6.29"],
                "description": "Verify modified C header captures struct evolution via m_moved_tag, recycles unchanged tags, and closes prior tags",
            }
            snapshot = extract_changeset_snapshot([cs1, cs2], test_conf=test_conf)

            tables = snapshot["tables"]
            self.assertIn("m_moved_tag", tables)
            self.assertEqual(len(tables["m_moved_tag"]), 1, "Expected exactly 1 evolved struct tag in m_moved_tag")
            moved_row = tables["m_moved_tag"][0]
            self.assertNotEqual(moved_row["s_tag_id"], moved_row["e_tag_id"])
            all_tag_ids = {r["tag_id"] for r in tables["m_tag"]}
            self.assertIn(moved_row["s_tag_id"], all_tag_ids)
            self.assertIn(moved_row["e_tag_id"], all_tag_ids)

            assert_snapshot_matches(self, "c_ast", "modified_changed_file", snapshot)

    @test_feature("c_ast_exact_rename_file")
    def test_03_exact_rename_c_file(self) -> None:
        """Verify exact rename (R100) reuses old fid, links new m_bridge_file, and creates zero duplicate AST tags."""
        old_path = "include/linux/lockd/bind_legacy.h"
        new_path = "include/linux/lockd/bind.h"

        # --- Stage 1: Initial version before rename (v2.6.39) ---
        self.gp.Version_Name = "v2.6.39"
        self.gp.VID = 1
        self.gp.Old_VID = 0

        with test_step(f"Prepare initial file on disk: {old_path} (v2.6.39)"):
            self.prepare_c_file(self.gp.Version_Name, old_path, src_path=new_path)

        with test_step("Parse initial ChangeSet before rename"):
            cs1 = ChangeSet(f"A\t{old_path}")
            cs1.gp = self.gp
            cs1.mf = self.mf
            cs1.current_vid = 1
            G.CURRENT_PARSING_FILE = old_path
            cs1.store(m_v_main.set(1, self.gp.Version_Name))
            stage_file_prelude(cs1, self.gp)
            c_ast_parse(cs1)
            ok1 = cs1.execute()
            self.assertTrue(ok1, "Initial file ChangeSet execution must succeed")
            self.te.commit_all()

        # --- Stage 2: Exact rename version (v3.0) ---
        self.gp.Version_Name = "v3.0"
        self.gp.VID = 2
        self.gp.Old_VID = 1

        with test_step("Stage exact rename ChangeSet (R100)"):
            cs2 = ChangeSet(f"R100\t{old_path}\t{new_path}")
            cs2.gp = self.gp
            cs2.mf = self.mf
            cs2.current_vid = 2
            G.CURRENT_PARSING_FILE = new_path
            cs2.store(m_v_main.set(2, self.gp.Version_Name))
            stage_file_prelude(cs2, self.gp)
            c_ast_parse(cs2)
            ok2 = cs2.execute()
            self.assertTrue(ok2, "Rename ChangeSet execution must succeed")
            self.te.commit_all()

        with test_step("Extract accumulated multi-version snapshot and verify zero duplicate AST tags"):
            test_conf = {
                "parser": "c_ast",
                "test_name": "exact_rename_file",
                "file_operation": "R100",
                "file_path": new_path,
                "old_path": old_path,
                "versions": ["v2.6.39", "v3.0"],
                "description": "Verify exact rename (R100) reuses old fid, links new m_bridge_file, registers m_v_main, and creates zero duplicate AST tags",
            }
            snapshot = extract_changeset_snapshot([cs1, cs2], test_conf=test_conf)

            tables = snapshot["tables"]
            self.assertEqual(len(tables["m_file"]), 1, "m_file should have exactly 1 record (reused fid)")
            self.assertEqual(len(tables["m_bridge_file"]), 2, "m_bridge_file should have 2 records (one per version)")
            self.assertEqual(len(tables["m_v_main"]), 2, "m_v_main should have 2 records")

            assert_snapshot_matches(self, "c_ast", "exact_rename_file", snapshot)


class Test_CParserCornerCases(BaseCParserTestCase):
    """Corner-case snapshot unit tests validating advanced C-AST parser capabilities.

    Covers:
      1. System Call definitions (SYSCALL_DEFINE1) & module_init demotion in kernel/exec_domain.c
      2. Synchronization & Macro variable declarations (DEFINE_SPINLOCK) & designated initializers in kernel/dma.c
      3. Kernel tracepoint events (TRACE_EVENT, DECLARE_EVENT_CLASS, DEFINE_EVENT) in include/trace/events/net.h
      4. Low-level assembly source file parsing (.S) in arch/alpha/lib/clear_page.S
    """

    @test_feature("c_ast_syscall_file")
    def test_04_syscall_file(self) -> None:
        """Validate system call parsing (SYSCALL_DEFINE1) and macro demotion in kernel/exec_domain.c."""
        file_path = "kernel/exec_domain.c"
        version = "v3.0"

        with test_step(f"Prepare kernel source file {file_path} from git tag {version}"):
            self.gp.Version_Name = version
            self.gp.VID = 1
            self.gp.Old_VID = 0
            self.prepare_c_file(version, file_path)

        with test_step("Parse file addition ChangeSet"):
            cs = ChangeSet(f"A\t{file_path}")
            cs.gp = self.gp
            cs.mf = self.mf
            cs.current_vid = 1
            G.CURRENT_PARSING_FILE = file_path
            cs.store(m_v_main.set(1, self.gp.Version_Name))
            stage_file_prelude(cs, self.gp)
            c_ast_parse(cs)
            exec_ok = cs.execute()
            self.assertTrue(exec_ok, "ChangeSet execution must succeed")
            self.te.commit_all()

        with test_step("Extract snapshot and assert match"):
            test_conf = {
                "parser": "c_ast",
                "test_name": "syscall_file",
                "file_operation": "A",
                "file_path": file_path,
                "versions": [version],
                "description": "Verify system call parsing (sys_personality as C_SyscallDef 147), DEFINE_RWLOCK (exec_domains_lock as C_MacroVarDef 150), and exclusion of demoted module_init and EXPORT_SYMBOL from m_symbol_def",
            }
            snapshot = extract_changeset_snapshot(cs, test_conf=test_conf)
            assert_snapshot_matches(self, "c_ast", "syscall_file", snapshot)

    @test_feature("c_ast_macro_variable_file")
    def test_05_macro_variable_file(self) -> None:
        """Validate macro variable definitions and designated initializers in kernel/dma.c."""
        file_path = "kernel/dma.c"
        version = "v3.0"

        with test_step(f"Prepare kernel source file {file_path} from git tag {version}"):
            self.gp.Version_Name = version
            self.gp.VID = 1
            self.gp.Old_VID = 0
            self.prepare_c_file(version, file_path)

        with test_step("Parse file addition ChangeSet"):
            cs = ChangeSet(f"A\t{file_path}")
            cs.gp = self.gp
            cs.mf = self.mf
            cs.current_vid = 1
            G.CURRENT_PARSING_FILE = file_path
            cs.store(m_v_main.set(1, self.gp.Version_Name))
            stage_file_prelude(cs, self.gp)
            c_ast_parse(cs)
            exec_ok = cs.execute()
            self.assertTrue(exec_ok, "ChangeSet execution must succeed")
            self.te.commit_all()

        with test_step("Extract snapshot and assert match"):
            test_conf = {
                "parser": "c_ast",
                "test_name": "macro_variable_file",
                "file_operation": "A",
                "file_path": file_path,
                "versions": [version],
                "description": "Verify DEFINE_SPINLOCK (dma_spin_lock as C_MacroVarDef 150), struct dma_chan declaration, designated initializers, and exclusion of demoted __initcall and EXPORT_SYMBOL from m_symbol_def",
            }
            snapshot = extract_changeset_snapshot(cs, test_conf=test_conf)
            assert_snapshot_matches(self, "c_ast", "macro_variable_file", snapshot)

    @test_feature("c_ast_tracepoint_file")
    def test_06_tracepoint_file(self) -> None:
        """Validate tracepoint definitions (TRACE_EVENT, DECLARE_EVENT_CLASS, DEFINE_EVENT) in include/trace/events/net.h."""
        file_path = "include/trace/events/net.h"
        version = "v3.0"

        with test_step(f"Prepare kernel source file {file_path} from git tag {version}"):
            self.gp.Version_Name = version
            self.gp.VID = 1
            self.gp.Old_VID = 0
            self.prepare_c_file(version, file_path)

        with test_step("Parse file addition ChangeSet"):
            cs = ChangeSet(f"A\t{file_path}")
            cs.gp = self.gp
            cs.mf = self.mf
            cs.current_vid = 1
            G.CURRENT_PARSING_FILE = file_path
            cs.store(m_v_main.set(1, self.gp.Version_Name))
            stage_file_prelude(cs, self.gp)
            c_ast_parse(cs)
            exec_ok = cs.execute()
            self.assertTrue(exec_ok, "ChangeSet execution must succeed")
            self.te.commit_all()

        with test_step("Extract snapshot and assert match"):
            test_conf = {
                "parser": "c_ast",
                "test_name": "tracepoint_file",
                "file_operation": "A",
                "file_path": file_path,
                "versions": [version],
                "description": "Verify tracepoint events (net_dev_xmit, net_dev_template, net_dev_queue, netif_receive_skb, netif_rx as C_TracepointDef 151), semicolonless macro scoping, and clean preprocessor symbol definitions",
            }
            snapshot = extract_changeset_snapshot(cs, test_conf=test_conf)
            assert_snapshot_matches(self, "c_ast", "tracepoint_file", snapshot)

    @test_feature("c_ast_assembly_file")
    def test_07_assembly_file(self) -> None:
        """Validate assembly source parsing (.S) in arch/alpha/lib/clear_page.S."""
        file_path = "arch/alpha/lib/clear_page.S"
        version = "v3.0"

        with test_step(f"Prepare kernel source file {file_path} from git tag {version}"):
            self.gp.Version_Name = version
            self.gp.VID = 1
            self.gp.Old_VID = 0
            self.prepare_c_file(version, file_path)

        with test_step("Parse file addition ChangeSet"):
            cs = ChangeSet(f"A\t{file_path}")
            cs.gp = self.gp
            cs.mf = self.mf
            cs.current_vid = 1
            G.CURRENT_PARSING_FILE = file_path
            cs.store(m_v_main.set(1, self.gp.Version_Name))
            stage_file_prelude(cs, self.gp)
            c_ast_parse(cs)
            exec_ok = cs.execute()
            self.assertTrue(exec_ok, "ChangeSet execution must succeed")
            self.te.commit_all()

        with test_step("Extract snapshot and assert match"):
            test_conf = {
                "parser": "c_ast",
                "test_name": "assembly_file",
                "file_operation": "A",
                "file_path": file_path,
                "versions": [version],
                "description": "Verify assembly source parsing (.S) into AST statements, tags, and bridge mappings with zero C-specific symbol hallucination",
            }
            snapshot = extract_changeset_snapshot(cs, test_conf=test_conf)
            assert_snapshot_matches(self, "c_ast", "assembly_file", snapshot)


if __name__ == "__main__":
    unittest.main()
