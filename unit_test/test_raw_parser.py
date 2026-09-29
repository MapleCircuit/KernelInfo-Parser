"""unit_test/test_raw_parser.py - Unit Test Suite for Fallback Raw AST Parser with Snapshots.

Validates raw content parser lifecycle operations (Added, Modified changed, Exact rename)
using real Linux kernel git tree files and order-agnostic gold-master snapshot verification.
"""
from __future__ import annotations

import os
import unittest
from typing import Any

from core.globalstuff import (
    G,
    T_RAW,
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
)
from db_engine import MockDB
from table_engine import TECachedDB
from parser.raw_ast.raw_ast import raw_ast_parse
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


class BaseRawParserTestCase(unittest.TestCase):
    """Base test fixture providing isolated DB, TableEngine, and MasterFile."""

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


class Test_RawParserLifecycle(BaseRawParserTestCase):
    """Lifecycle verification for fallback raw files across Git change types."""

    @test_feature("raw_ast_added_file")
    def test_01_added_raw_file(self) -> None:
        """Verify adding a raw file stages m_v_main, m_file, m_ast, m_tag, m_tag_code, m_bridge_tag, and matches snapshot."""
        file_path = "Documentation/ABI/testing/sysfs-kernel-fscaps"
        self.gp.Version_Name = "v3.0"
        self.gp.VID = 1
        self.gp.Old_VID = 0

        with test_step(f"Initialize ChangeSet for Added file: {file_path}"):
            cs = ChangeSet(f"A\t{file_path}")
            cs.gp = self.gp
            cs.mf = self.mf
            cs.current_vid = self.gp.VID
            G.CURRENT_PARSING_FILE = file_path

            # Version and file prelude registration
            cs.store(m_v_main.set(1, self.gp.Version_Name))
            stage_file_prelude(cs, self.gp)

        with test_step("Execute raw_ast_parse and ChangeSet resolution"):
            raw_ast_parse(cs)
            self.assertGreaterEqual(len(cs.cs), 7, "raw_ast_parse should stage AST, tag, maps, code")
            success = cs.execute()
            self.assertTrue(success, "ChangeSet execution should succeed without deferrals")

        with test_step("Extract order-agnostic snapshot and verify against baseline"):
            test_conf = {
                "parser": "raw_ast",
                "test_name": "added_file",
                "file_operation": "A",
                "file_path": file_path,
                "versions": ["v3.0"],
                "description": "Verify added raw file stages m_v_main, file prelude, AST tags, coordinates, and code content",
            }
            snapshot = extract_changeset_snapshot(cs, test_conf=test_conf)
            tables = snapshot["tables"]
            self.assertIn("m_v_main", tables)
            self.assertIn("m_ast", tables)
            self.assertIn("m_tag", tables)
            self.assertIn("m_tag_code", tables)
            self.assertIn("m_bridge_file", tables)
            self.assertIn("m_bridge_tag", tables)
            self.assertIn("m_map_ast", tables)
            self.assertIn("m_bridge_map", tables)

            assert_snapshot_matches(self, "raw_ast", "added_file", snapshot)

        with test_step("Verify semantic relationships and cryptographic hash"):
            ast_rows = tables["m_ast"]
            tag_rows = tables["m_tag"]
            code_rows = tables["m_tag_code"]
            v_rows = tables["m_v_main"]
            self.assertEqual(len(v_rows), 1)
            self.assertEqual(v_rows[0]["vname"], "v3.0")
            self.assertEqual(len(ast_rows), 1)
            self.assertEqual(len(tag_rows), 1)
            self.assertEqual(len(code_rows), 1)

            # m_tag.ast_id must point to m_ast.ast_id
            self.assertEqual(tag_rows[0]["ast_id"], ast_rows[0]["ast_id"])
            # m_tag.hash must match m_tag_code.hash
            self.assertEqual(tag_rows[0]["hash"], code_rows[0]["hash"])

    @test_feature("raw_ast_modified_changed_file", depends_on=["raw_ast_added_file"])
    def test_02_modified_changed_raw_file(self) -> None:
        """Verify modifying a raw file tracks tag evolution via m_moved_tag, retains both tag codes, and closes prior tag."""
        file_path = "Documentation/00-INDEX"

        with test_step("Stage Version 1 (v2.6.39) baseline"):
            self.gp.Version_Name = "v2.6.39"
            self.gp.VID = 1
            self.gp.Old_VID = 0

            cs_v1 = ChangeSet(f"A\t{file_path}")
            cs_v1.gp = self.gp
            cs_v1.mf = self.mf
            cs_v1.current_vid = 1
            G.CURRENT_PARSING_FILE = file_path

            cs_v1.store(m_v_main.set(1, self.gp.Version_Name))
            stage_file_prelude(cs_v1, self.gp)
            raw_ast_parse(cs_v1)
            self.assertTrue(cs_v1.execute())
            self.te.commit_all()

        with test_step("Stage Version 2 (v3.0) modification with changed content"):
            self.gp.Old_Version_Name = "v2.6.39"
            self.gp.Old_VID = 1
            self.gp.Version_Name = "v3.0"
            self.gp.VID = 2

            cs_v2 = ChangeSet(f"M\t{file_path}")
            cs_v2.gp = self.gp
            cs_v2.mf = self.mf
            cs_v2.current_vid = 2
            G.CURRENT_PARSING_FILE = file_path

            cs_v2.store(m_v_main.set(2, self.gp.Version_Name))
            stage_file_prelude(cs_v2, self.gp)
            raw_ast_parse(cs_v2)
            self.assertTrue(cs_v2.execute())

        with test_step("Extract snapshot across both versions and verify against baseline"):
            test_conf = {
                "parser": "raw_ast",
                "test_name": "modified_changed_file",
                "file_operation": "M",
                "file_path": file_path,
                "versions": ["v2.6.39", "v3.0"],
                "description": "Verify modified raw file captures tag evolution via m_moved_tag, closes prior tags, and retains both tag codes",
            }
            snapshot = extract_changeset_snapshot([cs_v1, cs_v2], test_conf=test_conf)
            tables = snapshot["tables"]
            self.assertIn("m_v_main", tables)
            self.assertIn("m_moved_tag", tables, "Modified changed file must record m_moved_tag")
            self.assertIn("m_tag", tables)
            self.assertIn("m_tag_code", tables)

            assert_snapshot_matches(self, "raw_ast", "modified_changed_file", snapshot)

        with test_step("Verify tag evolution, prior tag closure, and dual tag codes"):
            moved_rows = tables["m_moved_tag"]
            tag_rows = tables["m_tag"]
            code_rows = tables["m_tag_code"]
            v_rows = tables["m_v_main"]

            self.assertEqual(len(v_rows), 2, "Expected 2 versions in m_v_main (v2.6.39 and v3.0)")
            self.assertEqual(len(code_rows), 2, "Expected 2 m_tag_code entries across modified versions")
            self.assertEqual(len(moved_rows), 1)
            self.assertEqual(len(tag_rows), 2, "Expected new tag and closed prior tag")

            # One tag must have vid_s=2, vid_e=0 (new tag)
            new_tags = [t for t in tag_rows if t["vid_s"] == 2 and t["vid_e"] == 0]
            self.assertEqual(len(new_tags), 1)

            # One tag must have vid_s=1, vid_e=1 (closed prior tag)
            closed_tags = [t for t in tag_rows if t["vid_s"] == 1 and t["vid_e"] == 1]
            self.assertEqual(len(closed_tags), 1)

            # m_moved_tag links prior tag to new tag
            self.assertEqual(moved_rows[0]["s_tag_id"], closed_tags[0]["tag_id"])
            self.assertEqual(moved_rows[0]["e_tag_id"], new_tags[0]["tag_id"])

    @test_feature("raw_ast_exact_rename_file", depends_on=["raw_ast_added_file"])
    def test_03_exact_rename_raw_file(self) -> None:
        """Verify exact rename (R100) reuses old fid, links new m_bridge_file, registers m_v_main, and preserves tags."""
        old_path = "Documentation/usb/hiddev.txt"
        new_path = "Documentation/hid/hiddev.txt"

        with test_step(f"Stage Version 1 (v2.6.39) baseline at {old_path}"):
            self.gp.Version_Name = "v2.6.39"
            self.gp.VID = 1
            self.gp.Old_VID = 0

            cs_v1 = ChangeSet(f"A\t{old_path}")
            cs_v1.gp = self.gp
            cs_v1.mf = self.mf
            cs_v1.current_vid = 1
            G.CURRENT_PARSING_FILE = old_path

            cs_v1.store(m_v_main.set(1, self.gp.Version_Name))
            stage_file_prelude(cs_v1, self.gp)
            raw_ast_parse(cs_v1)
            self.assertTrue(cs_v1.execute())
            self.te.commit_all()

        with test_step(f"Stage Version 2 (v3.0) exact rename (R100) to {new_path}"):
            self.gp.Old_Version_Name = "v2.6.39"
            self.gp.Old_VID = 1
            self.gp.Version_Name = "v3.0"
            self.gp.VID = 2

            cs_v2 = ChangeSet(f"R100\t{old_path}\t{new_path}")
            cs_v2.gp = self.gp
            cs_v2.mf = self.mf
            cs_v2.current_vid = 2
            G.CURRENT_PARSING_FILE = new_path

            cs_v2.store(m_v_main.set(2, self.gp.Version_Name))
            stage_file_prelude(cs_v2, self.gp)
            raw_ast_parse(cs_v2)  # Contractual no-op for R100
            self.assertTrue(cs_v2.execute())

        with test_step("Extract snapshot across both versions and verify against baseline"):
            test_conf = {
                "parser": "raw_ast",
                "test_name": "exact_rename_file",
                "file_operation": "R100",
                "file_path": new_path,
                "old_path": old_path,
                "versions": ["v2.6.39", "v3.0"],
                "description": "Verify exact rename (R100) reuses old fid, links new m_bridge_file, registers m_v_main, and creates zero duplicate AST tags",
            }
            snapshot = extract_changeset_snapshot([cs_v1, cs_v2], test_conf=test_conf)
            tables = snapshot["tables"]
            self.assertIn("m_v_main", tables)
            self.assertIn("m_file_name", tables)
            self.assertIn("m_file", tables)
            self.assertIn("m_bridge_file", tables)
            self.assertIn("m_tag", tables)
            self.assertIn("m_tag_code", tables)
            self.assertIn("m_ast", tables)

            assert_snapshot_matches(self, "raw_ast", "exact_rename_file", snapshot)

        with test_step("Verify fid reuse, dual bridge files, and zero duplicate tags"):
            v_rows = tables["m_v_main"]
            fn_rows = tables["m_file_name"]
            f_rows = tables["m_file"]
            bf_rows = tables["m_bridge_file"]
            tag_rows = tables["m_tag"]

            self.assertEqual(len(v_rows), 2, "Expected 2 versions in m_v_main")
            self.assertEqual(len(fn_rows), 2, "Expected 2 file paths in m_file_name (old and new)")
            self.assertEqual(len(f_rows), 1, "R100 must reuse the exact same fid (single m_file record)")
            self.assertEqual(len(bf_rows), 2, "Expected 2 m_bridge_file records (v1 and v2)")
            self.assertEqual(len(tag_rows), 1, "R100 must produce zero duplicate AST occurrence tags")

            # Both bridge files point to the same fid
            self.assertEqual(bf_rows[0]["fid"], bf_rows[1]["fid"])


if __name__ == "__main__":
    unittest.main()
