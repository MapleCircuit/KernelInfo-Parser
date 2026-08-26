"""tests/test_rust_ast.py - Comprehensive Unit & Integration Test Suite for Rust AST Parser.

Validates Rust AST tree parsing, relational database ChangeSet extraction,
all language constructs, tag lifecycle and cross-version tag recycling, and MockDB pipeline execution.
"""
from __future__ import annotations

import os
import sys
import unittest
import subprocess
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.globalstuff import G, ASTT, REF_ROOT, REF_OLD, REF_POS, REF_NO_REF
from core.GreatProcessor import GreatProcessor
from core.FileHandler import MasterFile
from core.TableHandling import ChangeSet
from core.DBLayout import (
    init_db_layout,
    m_file_name,
    m_file,
    m_bridge_file,
    m_ast,
    m_ast_container,
    m_tag,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
    m_ast_hash,
)
from db_engine import MockDB
from table_engine import TECachedDB
from parser.rust_ast.rust_ast_type import (
    Line,
    Ast_Rust_Struct,
    Ast_Rust_Field,
    Ast_Rust_Enum,
    Ast_Rust_Variant,
    Ast_Rust_Union,
    Ast_Rust_Fn,
    Ast_Rust_Param,
    Ast_Rust_Trait,
    Ast_Rust_TraitAlias,
    Ast_Rust_Impl,
    Ast_Rust_Mod,
    Ast_Rust_Use,
    Ast_Rust_Const,
    Ast_Rust_Static,
    Ast_Rust_Type,
    Ast_Rust_MacroDef,
    Ast_Rust_MacroCall,
    Ast_Rust_ForeignMod,
    Ast_Rust_DocComment,
)
from parser.rust_ast.rust_tree_parser import parse_rust_ast_tree, parse_span_coords
from parser.rust_ast.rust_ast import rust_ast_parse, get_prior_tags, close_prior_tags


SAMPLE_COMPREHENSIVE_RUST = """//! Comprehensive test module for Rust AST constructs.
use core::fmt::Debug;
pub use core::sync::atomic::{AtomicU32, Ordering};

pub const BUFFER_MAX: usize = 4096;
pub static mut GLOBAL_COUNTER: AtomicU32 = AtomicU32::new(0);

pub type BufferResult<T> = core::result::Result<T, i32>;

#[repr(C)]
pub union WordSplit {
    pub whole: u32,
    pub halves: [u16; 2],
}

pub struct TuplePoint(pub i32, pub i32);
pub struct UnitMarker;

#[derive(Debug, Clone)]
pub struct KernelBuffer {
    pub size: usize,
    pub data: *mut u8,
}

pub enum BufferEvent {
    Empty,
    Read(usize),
    Write { offset: usize, len: usize },
}

pub trait BufferOps {
    type Error;
    const CAPACITY: usize;
    fn reset(&mut self);
    fn len(&self) -> usize;
}

impl BufferOps for KernelBuffer {
    type Error = i32;
    const CAPACITY: usize = BUFFER_MAX;
    fn reset(&mut self) {
        self.size = 0;
    }
    fn len(&self) -> usize {
        self.size
    }
}

pub mod inner_tools {
    pub fn helper() -> bool {
        true
    }
}

extern "C" {
    pub fn external_c_helper(arg: i32) -> i32;
}

macro_rules! emit_log {
    ($msg:expr) => {
        // Log macro implementation
    };
}

pub fn create_buffer(size: usize) -> KernelBuffer {
    emit_log!("Creating buffer");
    KernelBuffer { size, data: core::ptr::null_mut() }
}
"""


class TestRustAstGrammarAndParsing(unittest.TestCase):
    """Test AST tree parsing across all Rust language constructs and span variations."""

    def setUp(self) -> None:
        self.env = os.environ.copy()
        self.env["RUSTC_BOOTSTRAP"] = "1"

    def test_parse_span_coords_formats(self) -> None:
        # 4-part multi-line / full format
        s1 = "/dev/shm/test.rs:10:5: 20:15 (#0)"
        ls, le, cs, ce = parse_span_coords(s1)
        self.assertEqual((ls, le, cs, ce), (10, 20, 5, 15))

        # 3-part same-line format
        s2 = "/dev/shm/test.rs:12:1: 35 (#0)"
        ls, le, cs, ce = parse_span_coords(s2)
        self.assertEqual((ls, le, cs, ce), (12, 12, 1, 35))

        # 2-part point format
        s3 = "/dev/shm/test.rs:15:8 (#0)"
        ls, le, cs, ce = parse_span_coords(s3)
        self.assertEqual((ls, le, cs, ce), (15, 15, 8, 8))

    def test_parse_comprehensive_sample(self) -> None:
        tmp_file = "/dev/shm/test_comprehensive_sample.rs"
        with open(tmp_file, "w") as f:
            f.write(SAMPLE_COMPREHENSIVE_RUST)

        try:
            p = subprocess.run(
                ["rustc", "--edition=2021", "--crate-type=lib", "-Z", "unpretty=ast-tree", "-o", "-", tmp_file],
                capture_output=True,
                text=True,
                env=self.env,
                check=True,
            )
            raw_lines = tuple(SAMPLE_COMPREHENSIVE_RUST.split("\n"))
            nodes = parse_rust_ast_tree(p.stdout, raw_lines)

            node_type_map = {}
            for n in nodes:
                node_type_map.setdefault(type(n).__name__, []).append(n)

            self.assertIn("Ast_Rust_DocComment", node_type_map)
            self.assertIn("Ast_Rust_Use", node_type_map)
            self.assertIn("Ast_Rust_Const", node_type_map)
            self.assertIn("Ast_Rust_Static", node_type_map)
            self.assertIn("Ast_Rust_Type", node_type_map)
            self.assertIn("Ast_Rust_Union", node_type_map)
            self.assertIn("Ast_Rust_Struct", node_type_map)
            self.assertIn("Ast_Rust_Enum", node_type_map)
            self.assertIn("Ast_Rust_Trait", node_type_map)
            self.assertIn("Ast_Rust_Impl", node_type_map)
            self.assertIn("Ast_Rust_Mod", node_type_map)
            self.assertIn("Ast_Rust_ForeignMod", node_type_map)
            self.assertIn("Ast_Rust_MacroDef", node_type_map)
            self.assertIn("Ast_Rust_Fn", node_type_map)

            # Check Union
            union_node = node_type_map["Ast_Rust_Union"][0]
            self.assertEqual(union_node.name, "WordSplit")
            self.assertEqual(len(union_node.fields), 2)

            # Check Struct variants (named, tuple, unit)
            struct_names = [s.name for s in node_type_map["Ast_Rust_Struct"]]
            self.assertIn("KernelBuffer", struct_names)
            self.assertIn("TuplePoint", struct_names)
            self.assertIn("UnitMarker", struct_names)

            # Check Enum variants
            enum_node = node_type_map["Ast_Rust_Enum"][0]
            self.assertEqual(enum_node.name, "BufferEvent")
            variant_names = [v.name for v in enum_node.variants]
            self.assertEqual(variant_names, ["Empty", "Read", "Write"])

            # Check Trait items (abstract associated type, const, methods)
            trait_node = node_type_map["Ast_Rust_Trait"][0]
            self.assertEqual(trait_node.name, "BufferOps")
            trait_item_names = [item.name for item in trait_node.items]
            self.assertIn("type Error", trait_item_names)
            self.assertIn("const CAPACITY: usize", trait_item_names)

            # Check Impl items (associated type with assignment, associated const, methods)
            impl_node = node_type_map["Ast_Rust_Impl"][0]
            impl_item_names = [item.name for item in impl_node.items]
            self.assertIn("type Error = i32", impl_item_names)
            self.assertIn("const CAPACITY: usize", impl_item_names)

            # Check ForeignMod items
            foreign_node = node_type_map["Ast_Rust_ForeignMod"][0]
            self.assertEqual(foreign_node.abi, "C")
            self.assertGreaterEqual(len(foreign_node.items), 1)

            # Check Mod nested items
            mod_node = node_type_map["Ast_Rust_Mod"][0]
            self.assertEqual(mod_node.name, "inner_tools")
            self.assertEqual(len(mod_node.items), 1)
            self.assertIsInstance(mod_node.items[0], Ast_Rust_Fn)

        finally:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)


class TestRustChangeSetExecution(unittest.TestCase):
    """Test ChangeSet operation staging and execution on MockDB across synthetic and kernel files."""

    def setUp(self) -> None:
        G.DB = MockDB
        G.TE = TECachedDB()
        self.gp = GreatProcessor()
        init_db_layout(self.gp)
        G.TE.start(self.gp.Table_Array, G.DB)

        self.mf = MasterFile()
        self.temp_dir = self.mf.create_temp_dir()
        self.mf.version_dict["v7.2"] = self.temp_dir
        G.MF = self.mf
        self.gp.Version_Name = "v7.2"
        self.gp.VID = 1

    def tearDown(self) -> None:
        if hasattr(self, "mf") and self.mf:
            self.mf.clear_all_version()

    def test_comprehensive_synthetic_execution(self) -> None:
        file_path = "drivers/sample/comprehensive.rs"
        full_path = os.path.join(self.temp_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(SAMPLE_COMPREHENSIVE_RUST)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, self.gp.VID, 0, 3, "A", 0))
        cs.store(m_bridge_file.set(self.gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

        cs.parse()
        self.assertGreater(len(cs.cs), 20)

        success = cs.execute()
        self.assertTrue(success, "ChangeSet execution should succeed")
        self.assertEqual(len(cs.cs_result), len(cs.cs))

    def test_linux_v7_2_file_stats_rs(self) -> None:
        file_path = "drivers/android/binder/stats.rs"
        full_path = os.path.join(self.temp_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        content = subprocess.check_output(["git", "-C", "linux", "show", f"v7.2:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, self.gp.VID, 0, 3, "A", 0))
        cs.store(m_bridge_file.set(self.gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

        cs.parse()
        self.assertGreater(len(cs.cs), 20)
        success = cs.execute()
        self.assertTrue(success)
        self.assertEqual(len(cs.cs_result), len(cs.cs))

    def test_linux_v7_2_file_defs_rs(self) -> None:
        file_path = "drivers/android/binder/defs.rs"
        full_path = os.path.join(self.temp_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        content = subprocess.check_output(["git", "-C", "linux", "show", f"v7.2:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, self.gp.VID, 0, 3, "A", 0))
        cs.store(m_bridge_file.set(self.gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

        cs.parse()
        self.assertGreater(len(cs.cs), 20)
        success = cs.execute()
        self.assertTrue(success)
        self.assertEqual(len(cs.cs_result), len(cs.cs))

    def test_linux_v7_2_file_rnull_rs(self) -> None:
        file_path = "drivers/block/rnull/rnull.rs"
        full_path = os.path.join(self.temp_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        content = subprocess.check_output(["git", "-C", "linux", "show", f"v7.2:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, self.gp.VID, 0, 3, "A", 0))
        cs.store(m_bridge_file.set(self.gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

        cs.parse()
        self.assertGreater(len(cs.cs), 50)
        success = cs.execute()
        self.assertTrue(success)
        self.assertEqual(len(cs.cs_result), len(cs.cs))

    def test_linux_v7_2_file_sync_rs(self) -> None:
        file_path = "rust/kernel/sync.rs"
        full_path = os.path.join(self.temp_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        content = subprocess.check_output(["git", "-C", "linux", "show", f"v7.2:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, self.gp.VID, 0, 3, "A", 0))
        cs.store(m_bridge_file.set(self.gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

        cs.parse()
        self.assertGreater(len(cs.cs), 30)
        success = cs.execute()
        self.assertTrue(success)
        self.assertEqual(len(cs.cs_result), len(cs.cs))

    def test_linux_v7_2_file_lib_rs(self) -> None:
        file_path = "rust/kernel/lib.rs"
        full_path = os.path.join(self.temp_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        content = subprocess.check_output(["git", "-C", "linux", "show", f"v7.2:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, self.gp.VID, 0, 3, "A", 0))
        cs.store(m_bridge_file.set(self.gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

        cs.parse()
        self.assertGreater(len(cs.cs), 50)
        success = cs.execute()
        self.assertTrue(success)
        self.assertEqual(len(cs.cs_result), len(cs.cs))


class TestRustTagLifecycle(unittest.TestCase):
    """Test cross-version tag lifecycle tracking and recycling (v7.1 -> v7.2)."""

    def setUp(self) -> None:
        G.DB = MockDB
        G.TE = TECachedDB()
        self.gp = GreatProcessor()
        init_db_layout(self.gp)
        G.TE.start(self.gp.Table_Array, G.DB)
        self.mf = MasterFile()
        G.MF = self.mf

    def tearDown(self) -> None:
        if hasattr(self, "mf") and self.mf:
            self.mf.clear_all_version()

    def test_tag_recycling_and_closing(self) -> None:
        file_path = "drivers/sample/lifecycle.rs"

        v1_code = """pub fn preserved_fn() -> i32 { 100 }\npub fn altered_fn() -> i32 { 1 }"""
        v2_code = """pub fn preserved_fn() -> i32 { 100 }\npub fn altered_fn() -> i32 { 2 }\npub fn new_fn() -> i32 { 3 }"""

        # --- Version 1 (VID = 1) ---
        dir_v1 = self.mf.create_temp_dir()
        self.mf.version_dict["v7.1"] = dir_v1
        self.gp.Version_Name = "v7.1"
        self.gp.VID = 1
        self.gp.Old_VID = 0

        p_v1 = os.path.join(dir_v1, file_path)
        os.makedirs(os.path.dirname(p_v1), exist_ok=True)
        with open(p_v1, "w") as f:
            f.write(v1_code)

        cs1 = ChangeSet(f"A\t{file_path}")
        cs1.current_vid = 1
        cs1.gp = self.gp
        cs1.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs1.store(m_file_name.get_set(None, cs1.current_path))
        cs1.store(m_file.set(None, 1, 0, 3, "A", 0))
        cs1.store(m_bridge_file.set(1, cs1.ref(m_file_name.fnid), cs1.ref(m_file.fid)))
        cs1.parse()
        cs1.execute()
        G.TE.commit_all()

        # --- Version 2 (VID = 2, Old_VID = 1) ---
        dir_v2 = self.mf.create_temp_dir()
        self.mf.version_dict["v7.2"] = dir_v2
        self.gp.Version_Name = "v7.2"
        self.gp.VID = 2
        self.gp.Old_VID = 1

        p_v2 = os.path.join(dir_v2, file_path)
        os.makedirs(os.path.dirname(p_v2), exist_ok=True)
        with open(p_v2, "w") as f:
            f.write(v2_code)

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
        cs2.store(m_file.set(None, 2, 0, 3, "M", 0))
        cs2.store(m_bridge_file.set(2, cs2.ref(m_file_name.fnid), cs2.ref(m_file.fid)))

        cs2.parse()
        self.assertTrue(len(cs2.active_tag_list) > 0, "Preserved function should have recycled prior tag")
        success = cs2.execute()
        self.assertTrue(success, "ChangeSet v7.2 should execute cleanly")


if __name__ == "__main__":
    unittest.main()
