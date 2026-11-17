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
    ASTT,
    G,
    SymbolLinkage,
    T_C,
)
from core.SymbolRows import symbol_view_rows
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
    m_map_ast,
    m_bridge_map,
)
from db_engine import MockDB
from table_engine import TECachedDB
from parser.c_ast.c_ast import c_ast_parse
from parser.c_ast.cursor_tree import clear_tu_caches, split_syscall_macro
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
            cs.store(m_v_main.set(None, 0, 4, self.gp.Version_Name, 1, None, None, None))
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
            self.assertIn("m_bridge_tag", tables)
            self.assertIn("m_map_ast", tables)
            self.assertNotIn("m_bridge_map", tables)
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
            cs1.store(m_v_main.set(None, 0, 4, self.gp.Version_Name, 1, None, None, None))
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
            cs2.store(m_v_main.set(None, 0, 4, self.gp.Version_Name, 2, None, None, None))
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
            cs1.store(m_v_main.set(None, 0, 4, self.gp.Version_Name, 1, None, None, None))
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
            cs2.store(m_v_main.set(None, 0, 4, self.gp.Version_Name, 2, None, None, None))
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
            cs.store(m_v_main.set(None, 0, 4, self.gp.Version_Name, 1, None, None, None))
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
            cs.store(m_v_main.set(None, 0, 4, self.gp.Version_Name, 1, None, None, None))
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
            cs.store(m_v_main.set(None, 0, 4, self.gp.Version_Name, 1, None, None, None))
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
            cs.store(m_v_main.set(None, 0, 4, self.gp.Version_Name, 1, None, None, None))
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


# Spelling of the m_ast_container roles a function signature uses (render_type below)
_ROLE_WORDS = {
    ASTT.C_void: "void", ASTT.C_unsigned: "unsigned", ASTT.C_signed: "signed", ASTT.C_char: "char",
    ASTT.C_short: "short", ASTT.C_int: "int", ASTT.C_long: "long", ASTT.C_longlong: "long long",
    ASTT.C_pointer: "*", ASTT.C_Qconst: "const", ASTT.C_Qvolatile: "volatile",
}


class Test_CParserSyscallSignature(BaseCParserTestCase):
    """SYSCALL_DEFINE<n> definitions carry a `long` return type and typed parameters (drivers/pci/syscall.c, v3.0).

    Function rows: m_ast (name = sys_<name>, type_id = C_SyscallDef) -> m_ast_container priority 0 = return type,
    1..N = parameters (m_ast name = parameter name), N+1 = C_CompoundStmt body; m_symbol_def records the definition.
    """

    FILE_PATH = "drivers/pci/syscall.c"
    VERSION = "v3.0"
    FULL_TREE = "/dev/shm/kip_v30_golden_cache"  # tests/test_cast_golden.py (ensure_golden_tree)

    def _parse(self, tree_dir: str | None) -> dict[str, Any]:
        self.gp.Version_Name = self.VERSION
        self.gp.VID = 1
        self.gp.Old_VID = 0
        if tree_dir is None:
            try:
                self.prepare_c_file(self.VERSION, self.FILE_PATH)
            except (subprocess.CalledProcessError, OSError) as exc:
                self.skipTest(f"{self.VERSION}:{self.FILE_PATH} unavailable: {exc}")
        else:
            self.mf.version_dict[self.VERSION] = tree_dir
        try:
            cs = ChangeSet(f"A\t{self.FILE_PATH}")
            cs.gp = self.gp
            cs.mf = self.mf
            cs.current_vid = 1
            G.CURRENT_PARSING_FILE = self.FILE_PATH
            cs.store(m_v_main.set(None, 0, 4, self.gp.Version_Name, 1, None, None, None))
            stage_file_prelude(cs, self.gp)
            c_ast_parse(cs)
            self.assertTrue(cs.execute(), "ChangeSet execution must succeed")
            self.te.commit_all()
        finally:
            if tree_dir is not None:
                # The shared kernel tree must survive tearDown's clear_all_version()
                self.mf.version_dict.pop(self.VERSION, None)
        return MockDB.store

    @staticmethod
    def _describe(store: dict[str, Any], fn_ast_id: int) -> tuple[str, list[tuple[str, str, list[tuple[str, str]]]]]:
        """(return type, [(param name, param type, attributes)]) rendered from the m_ast / m_ast_container rows."""
        ast = {r[0]: r for r in store["m_ast"].values()}
        children: dict[int, list[tuple[Any, ...]]] = {}
        for r in store["m_ast_container"].values():
            children.setdefault(r[0], []).append(r)
        attrs: dict[int, list[tuple[str, str]]] = {}
        for r in store.get("m_ast_attribute", {}).values():
            attrs.setdefault(r[0], []).append((r[2], r[3]))

        def render_type(role: int, ref: int) -> str:
            if not ref:
                return _ROLE_WORDS.get(role, str(role))
            name, type_id = ast[ref][1], ast[ref][2]
            if type_id == ASTT.C_Compound and not name:
                return " ".join(render_type(c[2], c[3]) for c in sorted(children.get(ref, ()), key=lambda c: c[1]))
            if role == ASTT.C_struct:
                return f"struct {name}"
            return name

        rows = sorted(children[fn_ast_id], key=lambda c: c[1])
        ret = render_type(rows[0][2], rows[0][3])
        params = []
        for _ast_id, _prio, _role, ref in rows[1:]:
            p_name, p_type = ast[ref][1], ast[ref][2]
            if p_type == ASTT.C_CompoundStmt:
                continue
            p_rows = sorted(children.get(ref, ()), key=lambda c: c[1])
            p_str = " ".join(render_type(c[2], c[3]) for c in p_rows) if p_rows else _ROLE_WORDS.get(p_type, str(p_type))
            params.append((p_name, p_str, attrs.get(ref, [])))
        return ret, params

    def _assert_syscalls(self, store: dict[str, Any]) -> None:
        defs = {r[4]: r for r in symbol_view_rows(store, "m_symbol_def") if r[5] == ASTT.C_SyscallDef}
        self.assertEqual(sorted(defs), ["sys_pciconfig_read", "sys_pciconfig_write"])
        self.assertEqual((defs["sys_pciconfig_read"][7], defs["sys_pciconfig_read"][8]), (16, 84))
        self.assertEqual((defs["sys_pciconfig_write"][7], defs["sys_pciconfig_write"][8]), (86, 136))
        expected_params = [
            ("bus", "unsigned long", []),
            ("dfn", "unsigned long", []),
            ("off", "unsigned long", []),
            ("len", "unsigned long", []),
            ("buf", "void *", [("user", "")]),
        ]
        ast = {r[0]: r for r in store["m_ast"].values()}
        for name, row in defs.items():
            fn_ast_id = row[3]
            self.assertEqual(ast[fn_ast_id][1:], (name, ASTT.C_SyscallDef))
            self.assertEqual(row[6], SymbolLinkage.MacroOrKernelDef)
            ret, params = self._describe(store, fn_ast_id)
            self.assertEqual(ret, "long", name)
            self.assertEqual(params, expected_params, name)
        # No definition is named after a macro argument (full mode used to name the function after `buf`)
        names = {r[4] for r in symbol_view_rows(store, "m_symbol_def")}
        self.assertFalse(names & {"buf", "SYSCALL_DEFINE5", "pciconfig_read", "pciconfig_write"})

    def test_01_split_syscall_macro(self) -> None:
        """The pure splitter returns the syscall name and `(type words, name)` pairs with offsets."""
        text = "SYSCALL_DEFINE3(open, const char __user *, filename, /* flags */ int, flags,\n\tint, mode)\n{"
        parsed = split_syscall_macro(text)
        self.assertIsNotNone(parsed)
        macro_off, is_compat, (name, name_off), params = parsed
        self.assertEqual((macro_off, is_compat, name, text[name_off:name_off + 4]), (0, False, "open", "open"))
        self.assertEqual([([w for w, _ in t], p) for t, (p, _o) in params],
                         [(["const", "char", "__user", "*"], "filename"), (["int"], "flags"), (["int"], "mode")])
        self.assertEqual(text[params[2][1][1]:params[2][1][1] + 4], "mode")
        self.assertEqual(split_syscall_macro("COMPAT_SYSCALL_DEFINE0(restart_syscall)")[1:3], (True, ("restart_syscall", 23)))
        paren = split_syscall_macro("SYSCALL_DEFINE(pread64)(unsigned int fd, char __user *buf,\n\tloff_t pos)\n{")
        self.assertEqual((paren[2][0], [([w for w, _ in t], p) for t, (p, _o) in paren[3]]),
                         ("pread64", [(["unsigned", "int"], "fd"), (["char", "__user", "*"], "buf"), (["loff_t"], "pos")]))
        self.assertEqual(split_syscall_macro("SYSCALL_DEFINE(x)(void)")[3], [])
        self.assertIsNone(split_syscall_macro("SYSCALL_DEFINE2(x, int, a)"))  # count disagrees with <n>
        self.assertIsNone(split_syscall_macro("SYSCALL_DEFINE1(x, int, a"))  # unbalanced

    @test_feature("c_ast_syscall_signature_isolated")
    def test_02_syscall_signature_isolated(self) -> None:
        """Isolated parse (no kernel headers): sys_pciconfig_read/write with long return and typed parameters."""
        with test_step("Parse drivers/pci/syscall.c without headers and check both syscall signatures"):
            self._assert_syscalls(self._parse(None))

    @test_feature("c_ast_syscall_signature_full")
    def test_03_syscall_signature_full(self) -> None:
        """Full-tree parse (macro expanded by libclang): same definitions, return type and parameters."""
        if not os.path.exists(os.path.join(self.FULL_TREE, "Makefile")):
            self.skipTest(f"{self.FULL_TREE} not extracted (run tests.test_cast_golden first)")
        with test_step("Parse drivers/pci/syscall.c against the v3.0 tree and check both syscall signatures"):
            self._assert_syscalls(self._parse(self.FULL_TREE))


if __name__ == "__main__":
    unittest.main()
