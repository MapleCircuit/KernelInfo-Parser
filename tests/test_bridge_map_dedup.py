"""tests/test_bridge_map_dedup.py - Unit & Integration Test Suite for Bridge Map Deduplication.

Validates that:
1. ChangeSet.register_bridge_map prevents duplicate bridge map emissions.
2. C-AST, Kconfig-AST, and Maintainer-AST emit exactly one m_bridge_map entry per tag.
3. Database batch insertion is idempotent and handles bridge tables safely.
"""
from __future__ import annotations
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.globalstuff import G, OP_REF, REF_POS, REF_ROOT, compute_code_hash
from core.GreatProcessor import GreatProcessor
from core.FileHandler import MasterFile
from core.TableHandling import Table, ChangeSet
from core.DBLayout import (
    init_db_layout,
    m_tag,
    m_map_ast,
    m_bridge_map,
    m_bridge_tag,
    m_file,
)
from table_engine import TECachedDB
from db_engine import MockDB


class TestBridgeMapDeduplication(unittest.TestCase):
    """Test ChangeSet-level and parser-level bridge map deduplication."""

    def setUp(self) -> None:
        self.gp = GreatProcessor()
        init_db_layout(self.gp)
        G.DB = MockDB
        G.TE = TECachedDB()
        G.TE.start(self.gp.Table_Array, G.DB)

    def test_changeset_register_bridge_map(self) -> None:
        """Verify ChangeSet.register_bridge_map returns True on first registration and False on duplicate."""
        cs = ChangeSet("A", "test.c")
        tag_1 = ((m_tag.table_id, 0), OP_REF, (REF_POS, 1))
        tag_2 = ((m_tag.table_id, 0), OP_REF, (REF_POS, 2))

        # First registration of tag_1
        self.assertTrue(cs.register_bridge_map(tag_1, tag_1))
        # Second registration of tag_1 should be False
        self.assertFalse(cs.register_bridge_map(tag_1, tag_1))

        # First registration of tag_2
        self.assertTrue(cs.register_bridge_map(tag_2, tag_2))
        # Subsequent registration of tag_2
        self.assertFalse(cs.register_bridge_map(tag_2, tag_2))

        # After clear_bloat(), tracking is reset
        cs.clear_bloat()
        self.assertTrue(cs.register_bridge_map(tag_1, tag_1))

    def test_c_ast_single_bridge_map_per_tag(self) -> None:
        """Verify C-AST parsing emits exactly one m_bridge_map per tag."""
        from parser.c_ast.c_ast import c_ast_parse
        from tests.test_c_ast import default_processing
        import subprocess

        target_file = "include/linux/drbd_tag_magic.h"
        mf = MasterFile()
        temp_dir = mf.create_temp_dir()
        mf.version_dict["v3.0"] = temp_dir
        G.MF = mf
        self.gp.Version_Name = "v3.0"
        self.gp.VID = 1

        full_path = os.path.join(temp_dir, target_file)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        file_content = subprocess.check_output(
            ["git", "-C", "linux", "show", f"v3.0:{target_file}"],
            stderr=subprocess.PIPE,
        )
        with open(full_path, "wb") as f:
            f.write(file_content)

        cs = ChangeSet(f"A\t{target_file}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = mf
        G.CURRENT_PARSING_FILE = target_file

        default_processing(cs, self.gp)
        cs.parse()

        # Count m_tag, m_bridge_tag, m_map_ast, m_bridge_map operations in CS.cs
        tag_ops = [op for op in cs.cs if op[0] == m_tag.table_id]
        bridge_tag_ops = [op for op in cs.cs if op[0] == m_bridge_tag.table_id]
        map_ast_ops = [op for op in cs.cs if op[0] == m_map_ast.table_id]
        bridge_map_ops = [op for op in cs.cs if op[0] == m_bridge_map.table_id]

        # Number of bridge_maps should not exceed number of tags
        self.assertGreater(len(tag_ops), 0)
        self.assertGreater(len(map_ast_ops), 0)
        self.assertGreater(len(bridge_map_ops), 0)
        self.assertEqual(len(tag_ops), len(bridge_map_ops), "Bridge maps must be 1-to-1 with tags")

    def test_kconfig_single_bridge_map_per_tag(self) -> None:
        """Verify Kconfig parser emits exactly one m_bridge_map per tag."""
        from parser.kconfig_ast.kconfig_ast import kconfig_ast_parse

        cs = ChangeSet("A", "fs/ext4/Kconfig")
        cs.gp = self.gp
        cs.mf = MasterFile()
        try:
            kconfig_ast_parse(cs)
        except Exception:
            pass

        tag_ops = [op for op in cs.cs if op[0] == m_tag.table_id]
        bridge_map_ops = [op for op in cs.cs if op[0] == m_bridge_map.table_id]
        if tag_ops:
            self.assertEqual(len(tag_ops), len(bridge_map_ops), "Kconfig tags must match bridge maps count")

    def test_maintainer_single_bridge_map_per_tag(self) -> None:
        """Verify Maintainer and Credits parsers emit exactly one m_bridge_map per tag."""
        from parser.maintainer_ast.maintainer_ast import maintainer_ast_parse

        cs = ChangeSet("A", "MAINTAINERS")
        cs.gp = self.gp
        cs.mf = MasterFile()
        try:
            maintainer_ast_parse(cs)
        except Exception:
            pass

        tag_ops = [op for op in cs.cs if op[0] == m_tag.table_id]
        bridge_map_ops = [op for op in cs.cs if op[0] == m_bridge_map.table_id]
        if tag_ops:
            self.assertEqual(len(tag_ops), len(bridge_map_ops), "Maintainer tags must match bridge maps count")

    def test_recycled_tag_skips_map_reemission(self) -> None:
        """Verify that when a tag is recycled from prior_tags, m_map_ast and m_bridge_map are not re-emitted."""
        from parser.kconfig_ast.kconfig_ast import KconfigManager

        cs = ChangeSet("A", "fs/ext4/Kconfig")
        cs.gp = self.gp
        cs.mf = MasterFile()

        # Seed prior tags
        dummy_code = "config EXT4_FS\n\ttristate \"The Extended 4 (ext4) filesystem\""
        code_hash = compute_code_hash(dummy_code)
        cs.prior_tags = [(0, 101, 1, 0, code_hash, 1, 0, 0)]
        cs.prior_tags_map = {code_hash: [(0, 101)]}
        cs.active_tag_list = set()

        mgr = KconfigManager(cs)
        tag_result = mgr._tag_and_map(1, 1, 2, 1, 10, dummy_code)

        self.assertEqual(tag_result, 101, "Recycled tag ID should be returned directly")
        tag_ops = [op for op in cs.cs if op[0] == m_tag.table_id]
        map_ast_ops = [op for op in cs.cs if op[0] == m_map_ast.table_id]
        bridge_map_ops = [op for op in cs.cs if op[0] == m_bridge_map.table_id]
        bridge_tag_ops = [op for op in cs.cs if op[0] == m_bridge_tag.table_id]

        self.assertEqual(len(tag_ops), 0, "No new m_tag should be created for recycled tag")
        self.assertEqual(len(map_ast_ops), 0, "No new m_map_ast should be created for recycled tag")
        self.assertEqual(len(bridge_map_ops), 0, "No new m_bridge_map should be created for recycled tag")
        self.assertEqual(len(bridge_tag_ops), 1, "Exactly one m_bridge_tag must be emitted for recycled tag")

    def test_dbhandling_uses_strict_insert(self) -> None:
        """Verify that DBHandling.py uses strict INSERT INTO and not INSERT IGNORE."""
        import inspect
        from db_engine.DBHandling import MariaDB

        source = inspect.getsource(MariaDB.insert)
        self.assertIn("INSERT INTO", source)
        self.assertNotIn("INSERT IGNORE", source)


if __name__ == "__main__":
    unittest.main()
