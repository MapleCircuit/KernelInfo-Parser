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

    def test_m_file_reference_ids_come_from_the_database_alone(self) -> None:
        """m_file_reference is db_assigned_ids: the TableEngine refuses to allocate its ids, and the database
        assigns them and skips rows whose unique key exists."""
        from table_engine.te_direct_db import TEDirectDB
        from db_engine import SQLiteDB

        self.assertTrue(m_file_reference.db_assigned_ids)
        for distributed in (False, True):
            te = TEDirectDB()
            te._register_table(m_file_reference)
            te.distributed = distributed
            with self.assertRaisesRegex(ValueError, "assigned by the database"):
                te.set(m_file_reference.table_id, (None, 18278, 500, 18291, 3, 9, "details 1"))

        db = SQLiteDB(":memory:", foreign_keys=False)
        db.create_table([m_file_reference])
        cols = ["source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details"]
        db.insert_rows("m_file_reference", cols, [(18278, 500, 18291, 3, 9, "details 1")], on_conflict="ignore")
        # Duplicate unique key (another details text) is skipped; the same blob at another path is its own row
        db.insert_rows("m_file_reference", cols, [(18278, 500, 18291, 3, 9, "details 2"), (18278, 501, 18291, 3, 9, "details 1")],
                       on_conflict="ignore")
        rows = db.select_rows("m_file_reference", ["ref_id", "source_fnid", "details"], order_by=["ref_id"], as_dict=False)
        self.assertEqual([tuple(r[1:]) for r in rows], [(500, "details 1"), (501, "details 1")])
        self.assertLess(rows[0][0], rows[1][0])

    def test_db_assigned_ids_requires_auto_increment(self) -> None:
        from core.TableHandling import Table
        from core.DBLayout import INT

        with self.assertRaisesRegex(ValueError, "auto-increment"):
            Table(table_id=9999, table_name="t_bad", columns=(("a", INT()),), primary=("a",), db_assigned_ids=True)


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

        from contextlib import contextmanager

        class FakeDB:
            """Collects the rows written directly (m_file_reference documentation rows, ref_id omitted)."""

            def run_transaction(self, fn, **_kw):
                return fn()

            def insert_rows(self, table, columns, rows, on_conflict="error"):
                assert table == "m_file_reference" and "ref_id" not in columns and on_conflict == "ignore"
                staged.extend(rows)
                return len(rows)

        @contextmanager
        def fake_db():
            yield FakeDB()

        class FakeMF:
            def get_file(_self, path, version):
                return self.MAKEFILE if path.endswith("Makefile") else self.DOC

            def git_file_list(_self, version):
                return "\n".join(self.FNIDS) + "\n"

        with mock.patch.object(pipeline.file_references, "MF", FakeMF()), mock.patch.object(main.G, "TE", FakeTE()), \
                mock.patch.object(main.G, "DB", fake_db), \
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
        rows = {tuple(r[0:4]) for r in self._run(processing_doc_references, {(22, 40)})}
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

    def _patched(self, db, **patches):
        """Context of processing_file_references() for versions VID 2 / Old_VID 1 over `db`."""
        from contextlib import ExitStack, contextmanager
        from unittest import mock
        import main
        import pipeline.file_references

        @contextmanager
        def fake_db():
            yield db

        stack = ExitStack()
        stack.enter_context(mock.patch.object(main.G, "DB", fake_db))
        stack.enter_context(mock.patch.object(main.G, "TE", mock.MagicMock()))
        stack.enter_context(mock.patch.object(main.gp, "VID", 2, create=True))
        stack.enter_context(mock.patch.object(main.gp, "Old_VID", 1, create=True))
        stack.enter_context(mock.patch.object(pipeline.file_references, "_TX_BACKOFF_S", 0.0))
        stack.enter_context(mock.patch.object(pipeline.file_references, "processing_kbuild"))
        for name, value in patches.items():
            stack.enter_context(mock.patch.object(pipeline.file_references, name, value))
        return stack

    def _include_db(self, engine):
        """`_db()` on `engine` plus one include tag of blob 8 (the new pair (3, 8)) targeting fnid 5."""
        from core.DBLayout import TABLES as ALL

        db = engine(use_global=False) if engine.__name__ == "MockDB" else engine(":memory:", foreign_keys=False)
        db.create_table(list(ALL))
        db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(1, 1, 9), (2, 1, 9), (2, 2, 9), (2, 3, 8)])
        cols = ["ref_id", "source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details"]
        db.insert_rows("m_file_reference", cols, [(1, 9, 1, 7, 1, 1, "kept"), (2, 9, 2, 7, 1, 1, "stale"), (3, 8, 3, 7, 1, 1, "stale")])
        db.insert_rows("m_bridge_tag", ["blob_id", "tag_id", "line_s", "line_e", "char_s", "char_e"], [(8, 70, 4, 4, 0, 1)])
        db.insert_rows("m_tag", ["tag_id", "hash", "ast_id", "hl_s", "hl_l"], [(70, b"\x01" * 32, 80, 0, 1)])
        db.insert_rows("m_ast_include", ["ast_id", "fnid"], [(80, 5)])
        db.insert_rows("m_ast", ["ast_id", "type_id", "name"], [(80, 1, "linux/x.h")])
        return db

    def test_sqlite_and_mock_parity_single_id_authority(self) -> None:
        """Include and documentation rows share AUTO_INCREMENT (no TableEngine id) on SQLiteDB and MockDB, even
        when a leased m_table_sequence block would overlap the database's next id; a rerun is idempotent."""
        import pipeline.file_references
        from db_engine import MockDB, SQLiteDB
        from core.DBLayout import m_file_reference

        def docs(version, staged_file_refs=None, new_pairs=None, db=None):
            assert new_pairs == {(2, 9), (3, 8)}
            rows = []
            for blob, fnid in ((8, 3), (9, 2)):
                pipeline.file_references._collect_file_reference(staged_file_refs, rows, blob, fnid, 7, 5, 2, "doc")
            # A duplicate of a collected row is dropped before the insert
            pipeline.file_references._collect_file_reference(staged_file_refs, rows, 8, 3, 7, 5, 2, "doc again")
            return pipeline.file_references._insert_file_references(db, rows)

        results = {}
        for engine in (SQLiteDB, MockDB):
            db = self._include_db(engine)
            # A stale TableEngine lease that would collide with the database's next id (4) is never consulted
            db.insert_rows("m_table_sequence", ["seq_table_id", "next_id"], [(m_file_reference.table_id, 4)])
            for _ in range(2):
                with self._patched(db, processing_doc_references=docs):
                    pipeline.file_references.processing_file_references("v3.1")
            rows = db.select_rows("m_file_reference", ["ref_id", "source_blob_id", "source_fnid", "target_fnid", "ref_type", "details"],
                                  order_by=["ref_id"], as_dict=False)
            ids = [r[0] for r in rows]
            self.assertEqual(len(ids), len(set(ids)))
            results[engine.__name__] = sorted(tuple(r[1:]) for r in rows)
        self.assertEqual(results["SQLiteDB"], results["MockDB"])
        self.assertEqual(results["SQLiteDB"], [
            (8, 3, 5, 1, "linux/x.h"), (8, 3, 7, 5, "doc"), (9, 1, 7, 1, "kept"), (9, 2, 7, 5, "doc"),
        ])

    def test_include_copy_retries_transient_conflicts(self) -> None:
        """A DeadlockError (MariaDB 1213 / 1205 / 1467) re-runs the include transaction; the copy then completes."""
        import pipeline.file_references
        from db_engine import DeadlockError

        db = self._db()
        calls = []
        real = pipeline.file_references.processing_include_references

        def flaky(version, new_pairs):
            calls.append(version)
            if len(calls) < 3:
                raise DeadlockError("1467 (HY000): Failed to read auto-increment value from storage engine")
            return real(version, new_pairs)

        with self._patched(db, processing_include_references=flaky, processing_doc_references=lambda *a, **k: 0):
            pipeline.file_references.processing_file_references("v3.1")
        self.assertEqual(len(calls), 3)
        self.assertEqual(db.select_values("m_file_reference", "details", order_by=["ref_id"]), ["kept"])

    def test_exhausted_retries_propagate_without_doc_rows(self) -> None:
        """When the include copy keeps failing the error propagates: no documentation rows are written after it."""
        from unittest import mock
        import pipeline.file_references
        from db_engine import DeadlockError

        db = self._db()
        include = mock.MagicMock(side_effect=DeadlockError("Deadlock found when trying to get lock"))
        doc = mock.MagicMock(return_value=0)
        with self._patched(db, processing_include_references=include, processing_doc_references=doc):
            with self.assertRaises(DeadlockError):
                pipeline.file_references.processing_file_references("v3.1")
        self.assertEqual(include.call_count, pipeline.file_references._TX_ATTEMPTS)
        doc.assert_not_called()

    def test_non_transient_failure_propagates_at_once(self) -> None:
        """A non-transient error is not retried and is not swallowed (b29: the copy failure stops Step 6.6)."""
        from unittest import mock
        import pipeline.file_references

        db = self._db()
        include = mock.MagicMock(side_effect=RuntimeError("copy failed"))
        doc = mock.MagicMock(return_value=0)
        with self._patched(db, processing_include_references=include, processing_doc_references=doc):
            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                pipeline.file_references.processing_file_references("v3.1")
        self.assertEqual(include.call_count, 1)
        doc.assert_not_called()

    def test_failed_doc_transaction_rolls_back_its_chunk(self) -> None:
        """A failing documentation chunk is rolled back as a whole and the error propagates."""
        from unittest import mock
        import pipeline.file_references

        db = self._db()
        real_insert = db.insert_rows

        def insert_then_fail(table, columns, rows, **kw):
            real_insert(table, columns, rows, **kw)
            raise RuntimeError("doc insert failed")

        rows = [(9, 2, 7, 5, 2, "doc")]
        with mock.patch.object(db, "insert_rows", side_effect=insert_then_fail):
            with self.assertRaisesRegex(RuntimeError, "doc insert failed"):
                pipeline.file_references._insert_file_references(db, rows)
        self.assertEqual(db.select_values("m_file_reference", "details", order_by=["ref_id"]), ["kept", "stale", "stale"])


class TestRollbackSkipsDatabaseAssignedIds(unittest.TestCase):
    """A leased m_ingest_id_range row of m_file_reference never deletes rows (AUTO_INCREMENT ids of other runs)."""

    def test_rollback_id_ranges_ignores_m_file_reference_ranges(self) -> None:
        from db_engine import SQLiteDB
        from core.DBLayout import TABLES as ALL
        from core.ingest_state import rollback_id_ranges

        db = SQLiteDB(":memory:", foreign_keys=False)
        db.create_table(list(ALL))
        cols = ["ref_id", "source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details"]
        db.insert_rows("m_file_reference", cols, [(15477647, 9, 1, 7, 1, 1, "other run")])
        rollback_id_ranges(db, 5, list(ALL), [(m_file_reference.table_id, 15477647, 15487646)])
        self.assertEqual(db.select_values("m_file_reference", "details"), ["other run"])


class TestRunTransaction(unittest.TestCase):
    """`BaseDBEngine.run_transaction()` and the MariaDB errno mapping it relies on."""

    def test_mariadb_transient_errnos_map_to_deadlock_error(self) -> None:
        from db_engine import DeadlockError
        from db_engine.errors import translate_mysql_error

        class FakeMySQLError(Exception):
            def __init__(self, errno, msg):
                super().__init__(f"{errno} (HY000): {msg}")
                self.errno = errno

        for errno in (1213, 1205, 1467):
            self.assertIsInstance(translate_mysql_error(FakeMySQLError(errno, "x")), DeadlockError)

    def test_retries_then_rolls_back_and_propagates(self) -> None:
        from db_engine import DeadlockError, SQLiteDB
        from core.DBLayout import m_kbuild_symbol

        db = SQLiteDB(":memory:", foreign_keys=False)
        db.create_table([m_file_reference, m_kbuild_symbol])
        cols = ["source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details"]
        attempts = []

        def op():
            attempts.append(1)
            db.insert_rows("m_file_reference", cols, [(1, 2, 3, 5, len(attempts), "x")])
            raise DeadlockError("Lock wait timeout exceeded")

        with self.assertRaises(DeadlockError):
            db.run_transaction(op, max_attempts=3, backoff_s=0.0)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(db.select_values("m_file_reference", "line_no"), [])
        self.assertEqual(db.run_transaction(lambda: 42, backoff_s=0.0), 42)

    def test_refuses_to_nest_inside_a_transaction(self) -> None:
        from db_engine import SQLiteDB, UnsupportedOperation

        db = SQLiteDB(":memory:", foreign_keys=False)
        with db.transaction():
            with self.assertRaises(UnsupportedOperation):
                db.run_transaction(lambda: None)


if __name__ == "__main__":
    unittest.main()
