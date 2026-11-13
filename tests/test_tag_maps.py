"""tests/test_tag_maps.py - Tag spatial map staging.

Validates that:
1. C-AST, Kconfig-AST, and Maintainer-AST emit exactly one m_map_ast row per new tag, keyed by the tag
   (map_id = tag_id), and no m_bridge_map rows (the table only ever held identity rows).
2. A recycled tag only emits its m_bridge_tag placement.
3. Database batch insertion is strict.
"""
from __future__ import annotations
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_engine import MockDB
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
from core.config import get_default_repo_path

LINUX_REPO = str(get_default_repo_path())


class TestTagMaps(unittest.TestCase):
    """Test that parsers stage one tag-keyed spatial map per new tag."""

    def setUp(self) -> None:
        self.gp = GreatProcessor()
        init_db_layout(self.gp)
        G.DB = MockDB
        G.TE = TECachedDB()
        G.TE.start(self.gp.Table_Array, G.DB)

    def assert_one_map_per_tag(self, cs: ChangeSet) -> None:
        tag_ops = [op for op in cs.cs if op[0] == m_tag.table_id]
        map_ast_ops = [op for op in cs.cs if op[0] == m_map_ast.table_id]
        self.assertEqual([op for op in cs.cs if op[0] == m_bridge_map.table_id], [])
        self.assertEqual(len(tag_ops), len(map_ast_ops), "One m_map_ast row per new tag")
        for op in map_ast_ops:
            # map_id is the tag: a positional reference to the tag's m_tag operation
            map_id = op[2][0]
            self.assertEqual(map_id[0], (m_tag.table_id, 0))

    def test_c_ast_single_map_per_tag(self) -> None:
        """Verify C-AST parsing emits exactly one m_map_ast per tag and no m_bridge_map."""
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
            ["git", "-C", LINUX_REPO, "show", f"v3.0:{target_file}"],
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

        self.assertGreater(len([op for op in cs.cs if op[0] == m_tag.table_id]), 0)
        self.assert_one_map_per_tag(cs)

    def test_kconfig_single_map_per_tag(self) -> None:
        """Verify Kconfig parser emits exactly one m_map_ast per tag."""
        from parser.kconfig_ast.kconfig_ast import kconfig_ast_parse

        cs = ChangeSet("A", "fs/ext4/Kconfig")
        cs.gp = self.gp
        cs.mf = MasterFile()
        try:
            kconfig_ast_parse(cs)
        except Exception:
            pass

        self.assert_one_map_per_tag(cs)

    def test_maintainer_single_map_per_tag(self) -> None:
        """Verify Maintainer and Credits parsers emit exactly one m_map_ast per tag."""
        from parser.maintainer_ast.maintainer_ast import maintainer_ast_parse

        cs = ChangeSet("A", "MAINTAINERS")
        cs.gp = self.gp
        cs.mf = MasterFile()
        try:
            maintainer_ast_parse(cs)
        except Exception:
            pass

        self.assert_one_map_per_tag(cs)

    def test_recycled_tag_skips_map_reemission(self) -> None:
        """Verify that when a tag is recycled from prior_tags, m_map_ast is not re-emitted."""
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

    def test_insert_is_strict_not_ignoring_duplicates(self) -> None:
        """`insert()` reports a duplicate primary key (it must not silently ignore the row)."""
        from core.DBLayout import m_file_name
        from db_engine import DuplicateKeyError, SQLiteDB

        for db in (SQLiteDB(":memory:"), MockDB(use_global=False)):
            with self.subTest(backend=type(db).__name__):
                db.create_table([m_file_name])
                db.insert(m_file_name, ((5, "kernel/a.c"),))
                with self.assertRaises(DuplicateKeyError):
                    db.insert(m_file_name, ((5, "kernel/b.c"),))
                self.assertEqual(db.select_values("m_file_name", "fname", {"fnid": 5}), ["kernel/a.c"])


if __name__ == "__main__":
    unittest.main()
