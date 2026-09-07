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
    m_moved_tag,
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

        # Should have staged m_moved_tag linking prior tag to new tag
        moved_tag_ops = [
            op for op in cs2.cs
            if isinstance(op, tuple) and len(op) == 3 and op[0] == m_moved_tag.table_id and op[1] == 1  # OP_SET
        ]
        self.assertEqual(len(moved_tag_ops), 1, "m_moved_tag should be staged linking prior tag to new tag")
        self.assertEqual(moved_tag_ops[0][2][0], close_tag_ops[0][2][0], "s_tag_id should match old tag id")

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

    def test_raw_ast_directory_symlink(self) -> None:
        """Verify fallback parsing on a directory symlink like arch/arm/boot/dts/include/dt-bindings."""
        target_dir = os.path.join(self.temp_dir, "include", "dt-bindings")
        os.makedirs(target_dir, exist_ok=True)
        link_dir = os.path.join(self.temp_dir, "arch", "arm", "boot", "dts", "include")
        os.makedirs(link_dir, exist_ok=True)
        link_path = os.path.join(link_dir, "dt-bindings")
        rel_target = "../../../../../include/dt-bindings"
        os.symlink(rel_target, link_path)

        file_path = "arch/arm/boot/dts/include/dt-bindings"
        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, self.gp.VID, 0, type_check(file_path), "A", 0))
        cs.store(m_bridge_file.set(self.gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

        cs.parse()
        self.assertGreater(len(cs.cs), 3)

        success = cs.execute()
        self.assertTrue(success)
        self.assertEqual(len(cs.cs_result), len(cs.cs))



class TestSymlinkAliasing(unittest.TestCase):
    """Unit and integration test suite for zero-duplication symlink aliasing."""

    def setUp(self) -> None:
        from main import MF as main_MF
        from main import file_fid_cache
        from main import gp as main_gp
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
        self.gp.Old_VID = 0

        # Sync main module globals to test instance
        main_gp.Table_Array = list(self.gp.Table_Array)
        main_gp.VID = 1
        main_gp.Old_VID = 0
        main_gp.Version_Name = "v3.0"
        main_gp.Symlink_List = []
        main_MF.version_dict["v3.0"] = self.temp_dir
        file_fid_cache.clear()

    def tearDown(self) -> None:
        from main import file_fid_cache
        file_fid_cache.clear()
        if hasattr(self, "mf") and self.mf:
            self.mf.clear_all_version()

    def test_file_symlink_aliasing_zero_duplication(self) -> None:
        """Verify file symlink aliases to target fid in m_bridge_file with 0 duplicate m_file/tags."""
        from main import file_fid_cache, processing_symlinks

        # 1. Setup real target file on disk and in database
        target_rel = "arch/microblaze/platform/generic/system.dts"
        target_full = os.path.join(self.temp_dir, target_rel)
        os.makedirs(os.path.dirname(target_full), exist_ok=True)
        with open(target_full, "w", encoding="utf-8") as f:
            f.write('/dts-v1/;\n/ { model = "Generic"; };\n')

        # Setup symlink on disk pointing to target
        symlink_rel = "arch/microblaze/boot/dts/system.dts"
        symlink_full = os.path.join(self.temp_dir, symlink_rel)
        os.makedirs(os.path.dirname(symlink_full), exist_ok=True)
        os.symlink("../../platform/generic/system.dts", symlink_full)

        # Register target file in TE/DB
        target_fn = G.TE.set(m_file_name.table_id, (None, target_rel))[0]
        target_f = G.TE.set(m_file.table_id, (None, 1, 0, T_RAW, "A", 0))
        target_fid = target_f[0]
        G.TE.set(m_bridge_file.table_id, (1, target_fn, target_fid))
        G.TE.commit_all()

        m_file_count_before = len(MockDB._global_store.get(m_file.table_name, {}))
        m_tag_count_before = len(MockDB._global_store.get(m_tag.table_name, {}))

        # 2. Process symlink
        processing_symlinks([f"A\t{symlink_rel}"])

        # 3. Assertions
        # Symlink file name registered
        sym_fn_row = m_file_name.get(None, symlink_rel)
        self.assertIsNotNone(sym_fn_row)
        sym_fnid = sym_fn_row[2][0]

        # m_bridge_file maps symlink fnid to target_fid
        bf_row = m_bridge_file.get(1, sym_fnid, None)
        self.assertIsNotNone(bf_row)
        self.assertEqual(bf_row[2][2], target_fid)
        self.assertEqual(file_fid_cache.get(symlink_rel), target_fid)

        # Zero duplication: No new m_file or m_tag records created for the symlink
        m_file_count_after = len(MockDB._global_store.get(m_file.table_name, {}))
        m_tag_count_after = len(MockDB._global_store.get(m_tag.table_name, {}))
        self.assertEqual(m_file_count_after, m_file_count_before, "Symlink aliasing must not create duplicate m_file")
        self.assertEqual(m_tag_count_after, m_tag_count_before, "Symlink aliasing must not create duplicate m_tag")

        # Verify staged symlink records commit cleanly through unified commit_all()
        G.TE.commit_all()
        self.assertIn(sym_fnid, MockDB._global_store.get(m_file_name.table_name, {}))
        self.assertIn((1, sym_fnid), MockDB._global_store.get(m_bridge_file.table_name, {}))

    def test_directory_symlink_aliasing(self) -> None:
        """Verify directory symlink aliases directly to target directory fid without directory errors."""
        from core.globalstuff import T_DIR
        from main import file_fid_cache, processing_symlinks

        # 1. Setup real target dir and symlink on disk
        target_dir = os.path.join(self.temp_dir, "include", "dt-bindings")
        os.makedirs(target_dir, exist_ok=True)
        link_dir = os.path.join(self.temp_dir, "arch", "arm", "boot", "dts", "include")
        os.makedirs(link_dir, exist_ok=True)
        link_path = os.path.join(link_dir, "dt-bindings")
        os.symlink("../../../../../include/dt-bindings", link_path)

        dir_rel = "include/dt-bindings"
        symlink_rel = "arch/arm/boot/dts/include/dt-bindings"

        # Register target directory in TE/DB
        dir_fn = G.TE.set(m_file_name.table_id, (None, dir_rel))[0]
        dir_f = G.TE.set(m_file.table_id, (None, 1, 0, T_DIR, "A", 0))
        dir_fid = dir_f[0]
        G.TE.set(m_bridge_file.table_id, (1, dir_fn, dir_fid))
        G.TE.commit_all()

        m_file_count_before = len(MockDB._global_store.get(m_file.table_name, {}))

        # 2. Process directory symlink
        processing_symlinks([f"A\t{symlink_rel}"])

        # 3. Assertions
        sym_fn_row = m_file_name.get(None, symlink_rel)
        self.assertIsNotNone(sym_fn_row)
        sym_fnid = sym_fn_row[2][0]

        bf_row = m_bridge_file.get(1, sym_fnid, None)
        self.assertIsNotNone(bf_row)
        self.assertEqual(bf_row[2][2], dir_fid)
        self.assertEqual(file_fid_cache.get(symlink_rel), dir_fid)

        m_file_count_after = len(MockDB._global_store.get(m_file.table_name, {}))
        self.assertEqual(m_file_count_after, m_file_count_before, "Directory symlink aliasing must not create duplicate m_file")

        # Verify staged directory symlink records commit cleanly through unified commit_all()
        G.TE.commit_all()
        self.assertIn(sym_fnid, MockDB._global_store.get(m_file_name.table_name, {}))
        self.assertIn((1, sym_fnid), MockDB._global_store.get(m_bridge_file.table_name, {}))

    def test_broken_symlink_fallback(self) -> None:
        """Verify dangling/broken symlinks gracefully fall back to raw tracking."""
        from main import processing_symlinks

        broken_rel = "tools/broken_link.txt"
        broken_full = os.path.join(self.temp_dir, broken_rel)
        os.makedirs(os.path.dirname(broken_full), exist_ok=True)
        os.symlink("missing_nonexistent_file.txt", broken_full)

        # Process broken symlink
        processing_symlinks([f"A\t{broken_rel}"])

        # Should fall back to raw processing, registering m_file_name and m_bridge_file
        sym_fn_row = m_file_name.get(None, broken_rel)
        self.assertIsNotNone(sym_fn_row)
        sym_fnid = sym_fn_row[2][0]

        bf_row = m_bridge_file.get(1, sym_fnid, None)
        self.assertIsNotNone(bf_row)
        self.assertIsNotNone(bf_row[2][2])

        # Verify fallback symlink records commit cleanly through unified commit_all()
        G.TE.commit_all()
        self.assertIn(sym_fnid, MockDB._global_store.get(m_file_name.table_name, {}))
        self.assertIn((1, sym_fnid), MockDB._global_store.get(m_bridge_file.table_name, {}))

    def test_symlink_deletion_does_not_close_target(self) -> None:
        """Verify deleting an aliased symlink does not close the target m_file record."""
        from main import MF as main_MF
        from main import default_processing
        from main import gp as main_gp

        old_dir = self.mf.create_temp_dir()
        self.mf.version_dict["v3.0"] = old_dir
        main_MF.version_dict["v3.0"] = old_dir
        self.gp.Old_Version_Name = "v3.0"
        main_gp.Old_Version_Name = "v3.0"

        # Create symlink on disk in Old_Version tree so islink evaluates True
        sym_rel = "arch/arm/symlink.dts"
        old_sym_full = os.path.join(old_dir, sym_rel)
        os.makedirs(os.path.dirname(old_sym_full), exist_ok=True)
        os.symlink("target.dts", old_sym_full)

        # Setup target file fid=100 and symlink bridge in VID 1
        target_fn = G.TE.set(m_file_name.table_id, (None, "arch/arm/target.dts"))[0]
        G.TE.set(m_file.table_id, (100, 1, 0, T_RAW, "A", 0))
        G.TE.set(m_bridge_file.table_id, (1, target_fn, 100))

        sym_fn = G.TE.set(m_file_name.table_id, (None, sym_rel))[0]
        G.TE.set(m_bridge_file.table_id, (1, sym_fn, 100))
        G.TE.commit_all()

        # Transition to VID 2
        self.gp.Old_VID = 1
        self.gp.VID = 2
        main_gp.Old_VID = 1
        main_gp.VID = 2

        # Symlink is deleted in VID 2
        cs_del = ChangeSet(f"D\t{sym_rel}")
        cs_del.current_vid = 2
        cs_del.gp = self.gp
        cs_del.mf = self.mf

        default_processing(cs_del)
        self.assertTrue(cs_del.execute())
        G.TE.commit_all()

        # Target file (fid=100) must remain open (vid_e == 0, e_stat == 0)
        target_f_row = m_file.get(100, None, None, None, None, None)
        self.assertIsNotNone(target_f_row)
        self.assertEqual(target_f_row[2][2], 0, "Target m_file vid_e must remain 0 (open)")
        self.assertEqual(target_f_row[2][5], 0, "Target m_file e_stat must remain 0 (not closed with 'D')")

    def test_symlink_topological_commit_ordering(self) -> None:
        """Verify processing_symlinks does not flush m_bridge_file prematurely before commit_all."""
        from main import processing_symlinks

        # 1. Setup target file and symlink
        target_rel = "arch/x86/boot/compressed/misc.h"
        target_full = os.path.join(self.temp_dir, target_rel)
        os.makedirs(os.path.dirname(target_full), exist_ok=True)
        with open(target_full, "w", encoding="utf-8") as f:
            f.write("/* header */\n")

        symlink_rel = "arch/x86/include/asm/misc.h"
        symlink_full = os.path.join(self.temp_dir, symlink_rel)
        os.makedirs(os.path.dirname(symlink_full), exist_ok=True)
        os.symlink("../../boot/compressed/misc.h", symlink_full)

        # Target file registered and committed
        target_fn = G.TE.set(m_file_name.table_id, (None, target_rel))[0]
        target_f = G.TE.set(m_file.table_id, (None, 1, 0, T_RAW, "A", 0))
        target_fid = target_f[0]
        G.TE.set(m_bridge_file.table_id, (1, target_fn, target_fid))
        G.TE.commit_all()

        # Count records in MockDB prior to symlink processing
        bridge_count_before = len(MockDB._global_store.get(m_bridge_file.table_name, {}))
        fn_count_before = len(MockDB._global_store.get(m_file_name.table_name, {}))

        # 2. Process symlink
        processing_symlinks([f"A\t{symlink_rel}"])

        # 3. Assert processing_symlinks did NOT prematurely commit to DB
        bridge_count_staged = len(MockDB._global_store.get(m_bridge_file.table_name, {}))
        fn_count_staged = len(MockDB._global_store.get(m_file_name.table_name, {}))
        self.assertEqual(
            bridge_count_staged,
            bridge_count_before,
            "processing_symlinks must NOT commit m_bridge_file prematurely before Step 7 commit_all()",
        )
        self.assertEqual(
            fn_count_staged,
            fn_count_before,
            "processing_symlinks must NOT commit m_file_name prematurely before Step 7 commit_all()",
        )

        # 4. Commit all and verify both are committed in proper topological order
        G.TE.commit_all()
        bridge_store = MockDB._global_store.get(m_bridge_file.table_name, {})
        fn_store = MockDB._global_store.get(m_file_name.table_name, {})

        self.assertEqual(len(bridge_store), bridge_count_before + 1)
        self.assertEqual(len(fn_store), fn_count_before + 1)

        # Verify foreign key integrity in MockDB: every bridge row's fnid exists in fn_store
        for (vid, fnid), b_row in bridge_store.items():
            self.assertIn(fnid, fn_store, f"Foreign key violation: fnid {fnid} in m_bridge_file missing in m_file_name")

    def test_file_symlink_aliasing_multi_version_unchanged(self) -> None:
        """Verify unchanged symlinks in multi-version cycles resolve target fid from gp.Old_VID without duplicate m_file."""
        from main import MF as main_MF
        from main import file_fid_cache, get_fid_for_path, processing_symlinks
        from main import gp as main_gp

        # 1. Setup real target file on disk and in database for VID 1
        target_rel = "arch/microblaze/platform/generic/system.dts"
        target_full = os.path.join(self.temp_dir, target_rel)
        os.makedirs(os.path.dirname(target_full), exist_ok=True)
        with open(target_full, "w", encoding="utf-8") as f:
            f.write('/dts-v1/;\n/ { model = "Generic"; };\n')

        symlink_rel = "arch/microblaze/boot/dts/system.dts"
        symlink_full = os.path.join(self.temp_dir, symlink_rel)
        os.makedirs(os.path.dirname(symlink_full), exist_ok=True)
        os.symlink("../../platform/generic/system.dts", symlink_full)

        target_fn = G.TE.set(m_file_name.table_id, (None, target_rel))[0]
        target_f = G.TE.set(m_file.table_id, (None, 1, 0, T_RAW, "A", 0))
        target_fid = target_f[0]
        G.TE.set(m_bridge_file.table_id, (1, target_fn, target_fid))

        # Process symlink in VID 1
        processing_symlinks([f"A\t{symlink_rel}"])
        G.TE.commit_all()

        m_file_count_v1 = len(MockDB._global_store.get(m_file.table_name, {}))

        # 2. Advance to VID 2 (target and symlink are unchanged)
        main_gp.Old_VID = 1
        main_gp.VID = 2
        main_gp.Version_Name = "v3.1"
        self.gp.Old_VID = 1
        self.gp.VID = 2
        self.gp.Version_Name = "v3.1"
        temp_dir_v2 = self.mf.create_temp_dir()
        main_MF.version_dict["v3.1"] = temp_dir_v2
        self.mf.version_dict["v3.1"] = temp_dir_v2

        symlink_v2 = os.path.join(temp_dir_v2, symlink_rel)
        os.makedirs(os.path.dirname(symlink_v2), exist_ok=True)
        os.symlink("../../platform/generic/system.dts", symlink_v2)
        file_fid_cache.clear()

        # Simulate get_fid_for_path fallback to gp.Old_VID before target is staged
        resolved_fid = get_fid_for_path(target_rel)
        self.assertEqual(resolved_fid, target_fid, "get_fid_for_path must resolve target fid from gp.Old_VID")

        # Process unchanged symlink in VID 2
        file_fid_cache.clear()
        processing_symlinks([f"U\t{symlink_rel}"])

        # Assertions
        sym_fn_row = m_file_name.get(None, symlink_rel)
        self.assertIsNotNone(sym_fn_row)
        sym_fnid = sym_fn_row[2][0]

        bf_row_v2 = m_bridge_file.get(2, sym_fnid, None)
        self.assertIsNotNone(bf_row_v2)
        self.assertEqual(bf_row_v2[2][2], target_fid)
        self.assertEqual(file_fid_cache.get(symlink_rel), target_fid)

        # Zero new m_file records created in VID 2
        m_file_count_v2 = len(MockDB._global_store.get(m_file.table_name, {}))
        self.assertEqual(m_file_count_v2, m_file_count_v1, "Unchanged symlink in VID 2 must not allocate new m_file")

    def test_raw_ast_operation_u_graceful(self) -> None:
        """Verify raw_ast_parse handles file_operation 'U' without warnings or errors."""
        from core.TableHandling import ChangeSet
        from main import gp as main_gp
        from parser.raw_ast.raw_ast import raw_ast_parse

        cs = ChangeSet("U\tarch/microblaze/boot/dts/system.dts")
        cs.file_operation = "U"
        cs.current_path = "arch/microblaze/boot/dts/system.dts"
        cs.gp = main_gp

        # raw_ast_parse should execute cleanly
        raw_ast_parse(cs)


if __name__ == "__main__":
    unittest.main()

