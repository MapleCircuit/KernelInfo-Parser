"""tests/test_file_references.py - Unit and functional tests for cross-file usage tracking.

Validates:
1. KbuildParser extraction of compilations, composite targets, Makefile includes, and subdirectories.
2. Accurate line number preservation across multi-line continuations in Makefiles.
3. DocReferenceScanner path detection, punctuation stripping, diff handling, and validation.
4. Schema definition of m_file_reference in core.DBLayout.
5. Webapp backend file reference retrieval and aggregation.
"""
import os
import unittest
from core.globalstuff import FileRefType, format_ref_type_label
from core.DBLayout import m_file_reference, TABLES
from parser.kbuild_parser import KbuildParser, KbuildBinding
from parser.doc_parser import DocReferenceScanner, DocReference


class TestKbuildParserReferences(unittest.TestCase):
    """Test suite for Makefile/Kbuild parsing of cross-file references."""

    def setUp(self) -> None:
        self.parser = KbuildParser()

    def test_makefile_line_continuation_and_composite(self) -> None:
        makefile_content = """# Makefile test
obj-$(CONFIG_EXT4_FS) += ext4.o

ext4-y := balloc.o \\
          bitmap.o \\
          dir.o \\
          super.o

ext4-$(CONFIG_EXT4_FS_POSIX_ACL) += acl.o
"""
        bindings = self.parser.parse_makefile_content(makefile_content, dir_path="fs/ext4")
        
        # Verify compiled sources extracted
        ext4_sources = {b.source_file_rel: b for b in bindings if b.symbol_name == "EXT4_FS"}
        self.assertIn("fs/ext4/balloc.c", ext4_sources)
        self.assertIn("fs/ext4/bitmap.c", ext4_sources)
        self.assertIn("fs/ext4/dir.c", ext4_sources)
        self.assertIn("fs/ext4/super.c", ext4_sources)
        
        # Verify line numbers are tracked (start line of composite definition)
        self.assertGreaterEqual(ext4_sources["fs/ext4/balloc.c"].source_line, 4)
        self.assertEqual(ext4_sources["fs/ext4/balloc.c"].ref_type, int(FileRefType.Kbuild))

        # Check sub-config in composite
        acl_binding = [b for b in bindings if b.symbol_name == "EXT4_FS_POSIX_ACL"][0]
        self.assertEqual(acl_binding.source_file_rel, "fs/ext4/acl.c")
        self.assertGreaterEqual(acl_binding.source_line, 9)

    def test_makefile_includes_and_subdir_recursions(self) -> None:
        makefile_content = """# Top-level or subsystem Makefile
include scripts/Makefile.build
-include $(srctree)/arch/x86/Makefile

obj-y += net/
obj-$(CONFIG_DRIVERS) += drivers/
obj-$(CONFIG_CORE) += core.o
"""
        bindings = self.parser.parse_makefile_content(makefile_content, dir_path="")
        
        # Verify Makefile includes
        includes = [b for b in bindings if b.ref_type == int(FileRefType.Makefile) and "include" in b.details]
        inc_targets = [b.source_file_rel for b in includes]
        self.assertIn("scripts/Makefile.build", inc_targets)
        self.assertIn("arch/x86/Makefile", inc_targets)

        # Verify subdirectory recursions
        recursions = [b for b in bindings if b.ref_type == int(FileRefType.Makefile) and "+=" in b.details]
        rec_targets = [b.source_file_rel for b in recursions]
        self.assertIn("net/Makefile", rec_targets)
        self.assertIn("drivers/Makefile", rec_targets)

        # Verify direct object
        direct_objs = [b for b in bindings if b.ref_type == int(FileRefType.Kbuild)]
        self.assertEqual(len(direct_objs), 1)
        self.assertEqual(direct_objs[0].source_file_rel, "core.c")


class TestDocReferenceScanner(unittest.TestCase):
    """Test suite for documentation cross-file reference scanner."""

    def setUp(self) -> None:
        self.scanner = DocReferenceScanner()
        self.mock_fnid_by_path = {
            "include/linux/fs.h": 101,
            "fs/ext4/super.c": 102,
            "Documentation/filesystems/ext4.txt": 103,
            "drivers/net/Makefile": 104,
            "MAINTAINERS": 105,
        }

    def test_path_sanitization_and_matching(self) -> None:
        text = """
        Linux Ext4 Filesystem Documentation
        ===================================
        Please refer to "include/linux/fs.h" for inode structure definitions.
        Implementation details are located in fs/ext4/super.c:245.
        See also (Documentation/filesystems/ext4.txt).
        Build configuration is managed via drivers/net/Makefile.
        Check MAINTAINERS for point of contact.
        
        Ignore this url: https://www.kernel.org/doc/Documentation/filesystems/ext4.txt
        Ignore this email: developer@kernel.org
        """
        refs = self.scanner.scan_file_content(
            content=text,
            source_fid=1,
            fnid_by_path=self.mock_fnid_by_path,
            source_path="Documentation/intro.txt",
        )

        matched_fnids = {r.target_fnid for r in refs}
        self.assertIn(101, matched_fnids)  # include/linux/fs.h
        self.assertIn(102, matched_fnids)  # fs/ext4/super.c
        self.assertIn(103, matched_fnids)  # Documentation/filesystems/ext4.txt
        self.assertIn(104, matched_fnids)  # drivers/net/Makefile
        self.assertIn(105, matched_fnids)  # MAINTAINERS

        # Verify self-reference filtering
        self_refs = self.scanner.scan_file_content(
            content=text,
            source_fid=103,
            fnid_by_path=self.mock_fnid_by_path,
            source_path="Documentation/filesystems/ext4.txt",
        )
        self_fnids = {r.target_fnid for r in self_refs}
        self.assertNotIn(103, self_fnids)  # Self-reference ignored

    def test_diff_prefix_stripping(self) -> None:
        diff_text = """
        --- a/fs/ext4/super.c
        +++ b/fs/ext4/super.c
        @@ -10,6 +10,7 @@
        """
        refs = self.scanner.scan_file_content(
            content=diff_text,
            source_fid=2,
            fnid_by_path=self.mock_fnid_by_path,
            source_path="patches/patch1.txt",
        )
        matched_fnids = {r.target_fnid for r in refs}
        self.assertIn(102, matched_fnids)  # fs/ext4/super.c matched despite a/ and b/ prefix


class TestDBLayoutAndWebappReferenceAPI(unittest.TestCase):
    """Test suite verifying m_file_reference schema and webapp helpers."""

    def test_m_file_reference_schema(self) -> None:
        self.assertEqual(m_file_reference.table_id, 33)
        self.assertEqual(m_file_reference.table_name, "m_file_reference")
        self.assertEqual(m_file_reference.init_primary, ("ref_id",))
        self.assertEqual(m_file_reference.primary, (0,))
        self.assertIn(m_file_reference, TABLES)

        # Check enum labels
        self.assertEqual(format_ref_type_label(FileRefType.Include), "Include")
        self.assertEqual(format_ref_type_label(FileRefType.Kbuild), "Kbuild")
        self.assertEqual(format_ref_type_label(FileRefType.Kconfig), "Kconfig")
        self.assertEqual(format_ref_type_label(FileRefType.Makefile), "Makefile")
        self.assertEqual(format_ref_type_label(FileRefType.Documentation), "Documentation")

    def test_webapp_get_file_references_empty(self) -> None:
        from webapp.main import get_file_references_internal
        res = get_file_references_internal(None, 1, 100)
        self.assertEqual(res["total"], 0)
        self.assertIn("counts", res)
        self.assertIn("include", res["counts"])


if __name__ == "__main__":
    unittest.main()
