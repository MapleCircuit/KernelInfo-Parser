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
    m_symbol_def,
    m_symbol_ref,
)
from db_engine import MockDB
from table_engine import TECachedDB
from parser.c_ast.c_ast import c_ast_parse
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

    def prepare_c_file(
        self,
        version: str,
        dest_path: str,
        src_path: str | None = None,
        repo_path: str = "linux",
    ) -> str:
        """Extract git blob directly into temporary disk directory for libclang parsing."""
        if version not in self.mf.version_dict:
            self.mf.version_dict[version] = self.mf.create_temp_dir()
        temp_dir = self.mf.version_dict[version]
        full_dest = os.path.join(temp_dir, dest_path)
        os.makedirs(os.path.dirname(full_dest), exist_ok=True)
        git_target = src_path or dest_path
        content = subprocess.check_output(
            ["git", "-C", repo_path, "show", f"{version}:{git_target}"],
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

            # Check that initial struct tag was closed (vid_e = 1)
            prior_tags = [r for r in tables["m_tag"] if r.get("vid_e") == 1]
            self.assertGreater(len(prior_tags), 0, "Prior modified tag should be marked closed with vid_e=1")

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


if __name__ == "__main__":
    unittest.main()
