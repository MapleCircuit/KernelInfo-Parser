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

    def test_makefile_duplicate_rule_items_deduplication(self) -> None:
        """Verify duplicate items in a single rule (e.g. traps.o in CRIS Makefile) do not produce duplicate bindings."""
        makefile_content = """# CRIS duplicate object test
obj-y := entry.o traps.o shadows.o debugport.o irq.o \\
         process.o setup.o signal.o traps.o time.o
"""
        bindings = self.parser.parse_makefile_content(makefile_content, dir_path="arch/cris")
        traps_bindings = [b for b in bindings if "traps.c" in b.source_file_rel]
        self.assertEqual(len(traps_bindings), 1)
        self.assertEqual(traps_bindings[0].source_line, 2)


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

    def test_relative_paths_and_kernel_doc_directive(self) -> None:
        text = """
        See the filesystem documentation in ext4.txt for details.
        .. kernel-doc:: include/linux/fs.h
        """
        refs = self.scanner.scan_file_content(
            content=text,
            source_fid=10,
            fnid_by_path=self.mock_fnid_by_path,
            source_path="Documentation/filesystems/overview.rst",
        )
        matched_fnids = {r.target_fnid for r in refs}
        self.assertIn(103, matched_fnids)  # ext4.txt resolved relative to Documentation/filesystems/
        self.assertIn(101, matched_fnids)  # .. kernel-doc:: include/linux/fs.h matched

    def test_extensionless_doc_and_docbook_directives(self) -> None:
        text = """
        See :doc:`../filesystems/ext4` for filesystem architecture.
        !Einclude/linux/fs.h
        !Ffs/ext4/super.c ext4_sync_fs
        .. kernel-doc:: include/linux/fs.h
           :functions: file_operations generic_file_read_iter
        """
        refs = self.scanner.scan_file_content(
            content=text,
            source_fid=15,
            fnid_by_path=self.mock_fnid_by_path,
            source_path="Documentation/DocBook/vfs.tmpl",
        )
        fnid_map = {r.target_fnid: r for r in refs}
        self.assertIn(103, fnid_map)  # ext4 resolved from :doc:`../filesystems/ext4` -> Documentation/filesystems/ext4.txt
        self.assertIn(101, fnid_map)  # !Einclude/linux/fs.h
        self.assertIn(102, fnid_map)  # !Ffs/ext4/super.c
        kdoc_ref = [r for r in refs if r.target_fnid == 101 and "functions" in r.details]
        self.assertTrue(len(kdoc_ref) > 0)
        self.assertIn("file_operations", kdoc_ref[0].details)


class TestDBLayoutAndWebappReferenceAPI(unittest.TestCase):
    """Test suite verifying the m_file_reference schema and its table-engine deduplication."""

    def test_m_file_reference_schema(self) -> None:
        self.assertEqual(m_file_reference.table_id, 28)
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

    def test_m_file_reference_unique_key_and_table_engine_deduplication(self) -> None:
        """Verify TableEngine deduplicates duplicate unique key entries for m_file_reference."""
        from table_engine.te_direct_db import TEDirectDB
        te = TEDirectDB()
        te._register_table(m_file_reference)
        te.next_id[m_file_reference.table_id] = 1

        # First insert: (ref_id, source_blob_id, source_fnid, target_fnid, ref_type, line_no, details)
        row1 = te.set(m_file_reference.table_id, (None, 18278, 500, 18291, 3, 9, "details 1"))
        self.assertEqual(row1[0], 1)
        self.assertEqual(tuple(row1[1:6]), (18278, 500, 18291, 3, 9))

        # Duplicate insert with identical (source_blob_id, source_fnid, target_fnid, ref_type, line_no)
        row2 = te.set(m_file_reference.table_id, (None, 18278, 500, 18291, 3, 9, "details 2"))
        # Must return the existing staged row without allocating a new ID
        self.assertEqual(row2[0], 1)
        self.assertEqual(len(te.queued_set[m_file_reference.table_id]), 1)

        # The same blob at another path is another source: its own row
        row3 = te.set(m_file_reference.table_id, (None, 18278, 501, 18291, 3, 9, "details 1"))
        self.assertEqual(row3[0], 2)
        self.assertEqual(len(te.queued_set[m_file_reference.table_id]), 2)


class TestPathScopedReferences(unittest.TestCase):
    """Kbuild/documentation rows are keyed by the source file's (blob, path) pair (source_fnid)."""

    MAKEFILE = "obj-y += x.o\n"
    DOC = "See fs/a/x.c and fs/b/x.c.\n"
    FNIDS = {
        "fs/a/Makefile": 1, "fs/b/Makefile": 2, "fs/a/x.c": 11, "fs/b/x.c": 12,
        "Documentation/a.txt": 21, "Documentation/copy.txt": 22,
    }
    BLOBS = {"fs/a/Makefile": 9, "fs/b/Makefile": 9, "fs/a/x.c": 31, "fs/b/x.c": 32,
             "Documentation/a.txt": 40, "Documentation/copy.txt": 40}

    def _run(self, func, new_pairs):
        from unittest import mock
        import main

        import pipeline.file_references
        staged: list[tuple] = []

        class FakeTE:
            def set(self, table_id, row):
                staged.append(row)
                return row

        class FakeMF:
            def get_file(_self, path, version):
                return self.MAKEFILE if path.endswith("Makefile") else self.DOC

            def git_file_list(_self, version):
                return "\n".join(self.FNIDS) + "\n"

        with mock.patch.object(pipeline.file_references, "MF", FakeMF()), mock.patch.object(main.G, "TE", FakeTE()), \
                mock.patch.object(pipeline.file_references, "get_fnid_for_path", self.FNIDS.get), \
                mock.patch.object(pipeline.file_references, "get_fid_for_path", self.BLOBS.get), \
                mock.patch.object(main.gp, "VID", 2, create=True), \
                mock.patch.object(main.gp, "Version_Name", "v3.1", create=True):
            func("v3.1", new_pairs=new_pairs)
        return staged

    # m_kbuild_symbol rows: (source_blob_id, source_fnid, idx, symbol, via, line_no, compile_mode, kind,
    # target_obj, target_path, target_fnid, rule); (blob, path, target fnid, symbol, line) compared
    @staticmethod
    def _kbuild(rows):
        return {(r[0], r[1], r[10], r[3], r[5]) for r in rows}

    def test_identical_makefiles_reference_their_own_directory(self) -> None:
        from pipeline.file_references import processing_kbuild
        rows = self._kbuild(self._run(processing_kbuild, None))
        self.assertEqual(rows, {(9, 1, 11, "y", 1), (9, 2, 12, "y", 1)})

    def test_only_new_pairs_are_recomputed(self) -> None:
        from pipeline.file_references import processing_kbuild
        # Blob 9 was already at fs/a/Makefile in the previous version: only its new path is parsed
        rows = self._kbuild(self._run(processing_kbuild, {(2, 9)}))
        self.assertEqual(rows, {(9, 2, 12, "y", 1)})

    def test_documentation_rows_per_path(self) -> None:
        from pipeline.file_references import processing_doc_references
        doc = int(FileRefType.Documentation)
        rows = {tuple(r[1:5]) for r in self._run(processing_doc_references, {(22, 40)})}
        self.assertEqual(rows, {(40, 22, 11, doc), (40, 22, 12, doc)})


class TestNewPairsOnce(unittest.TestCase):
    """Step 6.6 clears and recomputes the references of the (path, blob) pairs new in the version only."""

    def _db(self):
        from db_engine import SQLiteDB
        from core.DBLayout import TABLES as ALL

        db = SQLiteDB(":memory:", foreign_keys=False)
        db.create_table(list(ALL))
        db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(1, 1, 9), (2, 1, 9), (2, 2, 9), (2, 3, 8)])
        refs = [(1, 9, 1, 7, 1, 1, "kept"), (2, 9, 2, 7, 1, 1, "stale"), (3, 8, 3, 7, 1, 1, "stale")]
        cols = ["ref_id", "source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details"]
        db.insert_rows("m_file_reference", cols, refs)
        return db

    def test_clears_only_new_pairs_and_hands_them_to_the_stagers(self) -> None:
        from contextlib import contextmanager
        from unittest import mock
        import main

        import pipeline.file_references
        db = self._db()

        @contextmanager
        def fake_db():
            yield db

        with mock.patch.object(main.G, "DB", fake_db), mock.patch.object(main.G, "TE", mock.MagicMock()), \
                mock.patch.object(pipeline.file_references, "processing_kbuild") as kbuild, mock.patch.object(pipeline.file_references, "processing_doc_references"), \
                mock.patch.object(main.gp, "VID", 2, create=True), mock.patch.object(main.gp, "Old_VID", 1, create=True):
            pipeline.file_references.processing_file_references("v3.1")
        self.assertEqual(kbuild.call_args.kwargs["new_pairs"], {(2, 9), (3, 8)})
        left = db.select_values("m_file_reference", "details", order_by=["ref_id"])
        self.assertEqual(left, ["kept"])

    def test_processing_file_references_invalidates_te_id_cursor(self) -> None:
        from unittest import mock
        from contextlib import contextmanager
        import main
        import pipeline.file_references
        from core.DBLayout import m_file_reference

        te_mock = mock.MagicMock()
        te_mock.db = None
        te_mock._id_cursor = {m_file_reference.table_id: 1353}
        te_mock._id_limit = {m_file_reference.table_id: 10000}
        te_mock.next_id = {m_file_reference.table_id: 1353}
        te_mock.queued_set = {}
        db = self._db()

        @contextmanager
        def fake_db():
            yield db

        with mock.patch.object(main.G, "DB", fake_db), mock.patch.object(main.G, "TE", te_mock), \
                mock.patch.object(pipeline.file_references, "processing_kbuild"), mock.patch.object(pipeline.file_references, "processing_doc_references"), \
                mock.patch.object(pipeline.file_references, "processing_include_references"), \
                mock.patch.object(main.gp, "VID", 2, create=True), mock.patch.object(main.gp, "Old_VID", 1, create=True):
            pipeline.file_references.processing_file_references("v3.1")

        self.assertNotIn(m_file_reference.table_id, te_mock._id_cursor)
        self.assertNotIn(m_file_reference.table_id, te_mock._id_limit)
        self.assertEqual(te_mock.next_id[m_file_reference.table_id], db.get_next_id(m_file_reference))


if __name__ == "__main__":
    unittest.main()
