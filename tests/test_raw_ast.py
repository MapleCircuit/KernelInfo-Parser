"""tests/test_raw_ast.py - Unit & Integration Test Suite for Fallback Raw Content Parser.

Validates raw content extraction, tag generation, cross-version tag recycling,
and ChangeSet execution across all file lifecycle operations (A, M, D, R100).
"""
from __future__ import annotations

import os
import sys
import unittest
import subprocess
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.globalstuff import (
    G,
    ASTT,
    REF_ROOT,
    REF_OLD,
    REF_POS,
    REF_NO_REF,
    T_C,
    T_ASM,
    T_KCONFIG,
    T_RUST,
    T_MAINTAINERS,
    T_CREDITS,
    T_RAW,
    type_check,
)
from core.GreatProcessor import GreatProcessor
from core.FileHandler import MasterFile
from core.TableHandling import ChangeSet
from core.DBLayout import (
    init_db_layout,
    m_file_name,
    m_file,
    m_bridge_file,
    m_ast,
    m_tag,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
)
from db_engine import MockDB
from table_engine import TECachedDB
from parser.raw_ast.raw_ast import raw_ast_parse, get_prior_tags, close_prior_tags


SAMPLE_DOC_TEXT = """Linux Kernel Documentation
==========================

This is a plain text documentation file describing subsystem internals.
Line 4: Configuration guidelines.
Line 5: Architecture notes.
"""

SAMPLE_SCRIPT_TEXT = """#!/bin/bash
# Sample maintenance script
echo "Starting kernel build tool..."
exit 0
"""


class TestRawAstParser(unittest.TestCase):
    """Test suite for fallback raw content AST parser."""

    def setUp(self) -> None:
        G.DB = MockDB
        G.TE = TECachedDB()
        self.gp = GreatProcessor()
        init_db_layout(self.gp)
        G.TE.start(self.gp.Table_Array, G.DB)

        self.mf = MasterFile()
        self.temp_dir = self.mf.create_temp_dir()
        self.mf.version_dict["v3.0"] = self.temp_dir
        G.MF = self.mf
        self.gp.Version_Name = "v3.0"
        self.gp.VID = 1

    def tearDown(self) -> None:
        if hasattr(self, "mf") and self.mf:
            self.mf.clear_all_version()

    def test_type_check_fallback(self) -> None:
        """Verify type_check returns T_RAW for unparsed files and correct types for known files."""
        self.assertEqual(type_check("kernel/sched.c"), T_C)
        self.assertEqual(type_check("include/linux/types.h"), T_C)
        self.assertEqual(type_check("arch/x86/kernel/entry_64.S"), T_ASM)
        self.assertEqual(type_check("arch/x86/Kconfig"), T_KCONFIG)
        self.assertEqual(type_check("rust/kernel/lib.rs"), T_RUST)
        self.assertEqual(type_check("MAINTAINERS"), T_MAINTAINERS)
        self.assertEqual(type_check("CREDITS"), T_CREDITS)

        # Fallback raw files
        self.assertEqual(type_check("Documentation/00-INDEX"), T_RAW)
        self.assertEqual(type_check("Makefile"), T_RAW)
        self.assertEqual(type_check("scripts/checkpatch.pl"), T_RAW)
        self.assertEqual(type_check("README"), T_RAW)
        self.assertEqual(type_check("arch/x86/Makefile"), T_RAW)
        self.assertEqual(type_check("tools/perf/design.txt"), T_RAW)

    def test_raw_ast_added_file(self) -> None:
        """Verify adding a raw file extracts m_ast, m_tag, m_bridge_tag, and spatial maps."""
        file_path = "Documentation/overview.txt"
        full_path = os.path.join(self.temp_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="latin-1") as f:
            f.write(SAMPLE_DOC_TEXT)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, self.gp.VID, 0, T_RAW, "A", 0))
        cs.store(m_bridge_file.set(self.gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

        cs.parse()
        # Should have staged m_ast, m_tag, m_bridge_tag, m_map_ast, m_bridge_map
        self.assertGreaterEqual(len(cs.cs), 7)

        success = cs.execute()
        self.assertTrue(success, "ChangeSet execution should succeed")
        self.assertEqual(len(cs.cs_result), len(cs.cs))

        # Check tag content is SHA-256 hash
        import hashlib
        expected_hash = hashlib.sha256(SAMPLE_DOC_TEXT.encode("latin-1")).digest()
        tag_rows = [row for row in cs.cs if isinstance(row, tuple) and len(row) == 3 and row[0] == m_tag.table_id]
        self.assertEqual(len(tag_rows), 1)
        self.assertEqual(tag_rows[0][2][3], expected_hash)

    def test_raw_ast_empty_file(self) -> None:
        """Verify adding an empty 0-byte file extracts cleanly with valid extents."""
        file_path = "empty.txt"
        full_path = os.path.join(self.temp_dir, file_path)
        with open(full_path, "w", encoding="latin-1") as f:
            f.write("")

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, self.gp.VID, 0, T_RAW, "A", 0))
        cs.store(m_bridge_file.set(self.gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

        cs.parse()
        success = cs.execute()
        self.assertTrue(success)

        # Check tag row has empty string hash
        import hashlib
        empty_hash = hashlib.sha256(b"").digest()
        tag_rows = [row for row in cs.cs if isinstance(row, tuple) and len(row) == 3 and row[0] == m_tag.table_id]
        self.assertEqual(len(tag_rows), 1)
        self.assertEqual(tag_rows[0][2][3], empty_hash)

    def test_raw_ast_r100_rename_noop(self) -> None:
        """Verify exact rename (R100) performs no operations in raw_ast_parse."""
        cs = ChangeSet("R100\told_doc.txt\tnew_doc.txt")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf

        cs.parse()
        self.assertEqual(len(cs.cs), 0, "R100 exact rename should produce 0 AST operations")

    def test_raw_ast_modified_unchanged_content_recycles_tag(self) -> None:
        """Verify modifying a file with identical content recycles the prior tag without creating new m_tag."""
        file_path = "scripts/tool.sh"

        # --- Version 1 (VID = 1) ---
        p_v1 = os.path.join(self.temp_dir, file_path)
        os.makedirs(os.path.dirname(p_v1), exist_ok=True)
        with open(p_v1, "w", encoding="latin-1") as f:
            f.write(SAMPLE_SCRIPT_TEXT)

        cs1 = ChangeSet(f"A\t{file_path}")
        cs1.current_vid = 1
        cs1.gp = self.gp
        cs1.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs1.store(m_file_name.get_set(None, cs1.current_path))
        cs1.store(m_file.set(None, 1, 0, T_RAW, "A", 0))
        cs1.store(m_bridge_file.set(1, cs1.ref(m_file_name.fnid), cs1.ref(m_file.fid)))
        cs1.parse()
        cs1.execute()
        G.TE.commit_all()

        # --- Version 2 (VID = 2, Old_VID = 1) with SAME content ---
        dir_v2 = self.mf.create_temp_dir()
        self.mf.version_dict["v3.1"] = dir_v2
        self.gp.Version_Name = "v3.1"
        self.gp.VID = 2
        self.gp.Old_VID = 1

        p_v2 = os.path.join(dir_v2, file_path)
        os.makedirs(os.path.dirname(p_v2), exist_ok=True)
        with open(p_v2, "w", encoding="latin-1") as f:
            f.write(SAMPLE_SCRIPT_TEXT)

        cs2 = ChangeSet(f"M\t{file_path}")
        cs2.current_vid = 2
        cs2.gp = self.gp
        cs2.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs2.store(m_file_name.get_set(None, cs2.current_path))
        with cs2(REF_OLD):
            cs2.store(m_bridge_file.view(
                ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                1,
                cs2.ref(m_file_name.fnid, REF_ROOT),
                None,
                None,
                cs2.current_path,
            ))
            cs2.store(m_file.update(cs2.ref(m_bridge_file.fid), None, 1, None, None, "M"))
        cs2.store(m_file.set(None, 2, 0, T_RAW, "M", 0))
        cs2.store(m_bridge_file.set(2, cs2.ref(m_file_name.fnid), cs2.ref(m_file.fid)))

        cs2.parse()
        self.assertTrue(len(cs2.active_tag_list) > 0, "Unchanged content should have recycled prior tag")

        # Verify no new m_tag was staged
        new_tag_ops = [
            op for op in cs2.cs
            if isinstance(op, tuple) and len(op) == 3 and op[0] == m_tag.table_id and op[1] == 1  # OP_SET
        ]
        self.assertEqual(len(new_tag_ops), 0, "No new m_tag should be created when content is unchanged")

        success = cs2.execute()
        self.assertTrue(success, "ChangeSet v3.1 should execute cleanly")

    def test_raw_ast_modified_changed_content_creates_new_tag_and_closes_old(self) -> None:
        """Verify modifying a file with altered content stages new tag and closes previous tag."""
        file_path = "Documentation/config.txt"

        # --- Version 1 (VID = 1) ---
        p_v1 = os.path.join(self.temp_dir, file_path)
        os.makedirs(os.path.dirname(p_v1), exist_ok=True)
        with open(p_v1, "w", encoding="latin-1") as f:
            f.write("Initial config v1")

        cs1 = ChangeSet(f"A\t{file_path}")
        cs1.current_vid = 1
        cs1.gp = self.gp
        cs1.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs1.store(m_file_name.get_set(None, cs1.current_path))
        cs1.store(m_file.set(None, 1, 0, T_RAW, "A", 0))
        cs1.store(m_bridge_file.set(1, cs1.ref(m_file_name.fnid), cs1.ref(m_file.fid)))
        cs1.parse()
        cs1.execute()
        G.TE.commit_all()

        # --- Version 2 (VID = 2, Old_VID = 1) with CHANGED content ---
        dir_v2 = self.mf.create_temp_dir()
        self.mf.version_dict["v3.1"] = dir_v2
        self.gp.Version_Name = "v3.1"
        self.gp.VID = 2
        self.gp.Old_VID = 1

        p_v2 = os.path.join(dir_v2, file_path)
        os.makedirs(os.path.dirname(p_v2), exist_ok=True)
        with open(p_v2, "w", encoding="latin-1") as f:
            f.write("Updated config v2 - new options added")

        cs2 = ChangeSet(f"M\t{file_path}")
        cs2.current_vid = 2
        cs2.gp = self.gp
        cs2.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs2.store(m_file_name.get_set(None, cs2.current_path))
        with cs2(REF_OLD):
            cs2.store(m_bridge_file.view(
                ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                1,
                cs2.ref(m_file_name.fnid, REF_ROOT),
                None,
                None,
                cs2.current_path,
            ))
            cs2.store(m_file.update(cs2.ref(m_bridge_file.fid), None, 1, None, None, "M"))
        cs2.store(m_file.set(None, 2, 0, T_RAW, "M", 0))
        cs2.store(m_bridge_file.set(2, cs2.ref(m_file_name.fnid), cs2.ref(m_file.fid)))

        cs2.parse()

        # Should have staged a new m_tag.set
        new_tag_ops = [
            op for op in cs2.cs
            if isinstance(op, tuple) and len(op) == 3 and op[0] == m_tag.table_id and op[1] == 1  # OP_SET
        ]
        self.assertEqual(len(new_tag_ops), 1, "New m_tag should be created when content changed")

        # Should have staged an m_tag.update closing old tag
        close_tag_ops = [
            op for op in cs2.cs
            if isinstance(op, tuple) and len(op) == 3 and op[0] == m_tag.table_id and op[1] == 2  # OP_UPDATE
        ]
        self.assertEqual(len(close_tag_ops), 1, "Old m_tag should be updated to closed")
        self.assertEqual(close_tag_ops[0][2][2], 1, "Old tag vid_e should be set to Old_VID (1)")

        success = cs2.execute()
        self.assertTrue(success, "ChangeSet v3.1 should execute cleanly")

    def test_raw_ast_deleted_file_closes_tag(self) -> None:
        """Verify deleting a raw file closes the prior version's tag."""
        file_path = "Documentation/deprecated.txt"

        # --- Version 1 (VID = 1) ---
        p_v1 = os.path.join(self.temp_dir, file_path)
        os.makedirs(os.path.dirname(p_v1), exist_ok=True)
        with open(p_v1, "w", encoding="latin-1") as f:
            f.write("To be deleted")

        cs1 = ChangeSet(f"A\t{file_path}")
        cs1.current_vid = 1
        cs1.gp = self.gp
        cs1.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs1.store(m_file_name.get_set(None, cs1.current_path))
        cs1.store(m_file.set(None, 1, 0, T_RAW, "A", 0))
        cs1.store(m_bridge_file.set(1, cs1.ref(m_file_name.fnid), cs1.ref(m_file.fid)))
        cs1.parse()
        cs1.execute()
        G.TE.commit_all()

        # --- Version 2 (VID = 2, Old_VID = 1) - DELETED ---
        dir_v2 = self.mf.create_temp_dir()
        self.mf.version_dict["v3.1"] = dir_v2
        self.gp.Version_Name = "v3.1"
        self.gp.VID = 2
        self.gp.Old_VID = 1

        cs2 = ChangeSet(f"D\t{file_path}")
        cs2.current_vid = 2
        cs2.gp = self.gp
        cs2.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs2.store(m_file_name.get_set(None, cs2.current_path))
        with cs2(REF_OLD):
            cs2.store(m_bridge_file.view(
                ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                1,
                cs2.ref(m_file_name.fnid, REF_ROOT),
                None,
                None,
                cs2.current_path,
            ))
            cs2.store(m_file.update(cs2.ref(m_bridge_file.fid), None, 1, None, None, "D"))

        cs2.parse()

        # Should have staged an m_tag.update closing old tag
        close_tag_ops = [
            op for op in cs2.cs
            if isinstance(op, tuple) and len(op) == 3 and op[0] == m_tag.table_id and op[1] == 2  # OP_UPDATE
        ]
        self.assertEqual(len(close_tag_ops), 1, "Old m_tag should be updated to closed on deletion")
        self.assertEqual(close_tag_ops[0][2][2], 1, "Old tag vid_e should be set to Old_VID (1)")

        success = cs2.execute()
        self.assertTrue(success)

    def test_raw_ast_real_linux_file(self) -> None:
        """Verify fallback parsing on a real Linux git tracked raw file."""
        file_path = "README"
        full_path = os.path.join(self.temp_dir, file_path)
        content = subprocess.check_output(["git", "-C", "linux", "show", f"v3.0:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, self.gp.VID, 0, T_RAW, "A", 0))
        cs.store(m_bridge_file.set(self.gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

        cs.parse()
        self.assertGreater(len(cs.cs), 3)

        success = cs.execute()
        self.assertTrue(success)
        self.assertEqual(len(cs.cs_result), len(cs.cs))


if __name__ == "__main__":
    unittest.main()

