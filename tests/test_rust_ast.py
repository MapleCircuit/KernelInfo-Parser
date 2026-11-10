"""tests/test_rust_ast.py - Comprehensive Unit & Integration Test Suite for Rust AST Parser.

Validates Rust AST tree parsing, relational database ChangeSet extraction,
all language constructs, tag lifecycle and cross-version tag recycling, and MockDB pipeline execution.
"""
from __future__ import annotations

import json
import re
import os
import shutil
import sys
import tempfile
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
from parser.rust_ast.rust_ast import rust_ast_parse
from core.config import get_default_repo_path

LINUX_REPO = str(get_default_repo_path())


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

    @unittest.skipUnless(shutil.which("rustc"), "rustc not installed")
    def test_parse_comprehensive_sample(self) -> None:
        fd, tmp_file = tempfile.mkstemp(suffix=".rs")
        with os.fdopen(fd, "w") as f:
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
            self.assertEqual(impl_node.name, "impl BufferOps for KernelBuffer")
            self.assertEqual(impl_node.of_trait, "BufferOps")
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
        cs.store(m_file.set(None, getattr(cs, "blob_hash", b"\x00" * 20), 3))
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
        content = subprocess.check_output(["git", "-C", LINUX_REPO, "show", f"v7.2:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, getattr(cs, "blob_hash", b"\x00" * 20), 3))
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
        content = subprocess.check_output(["git", "-C", LINUX_REPO, "show", f"v7.2:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, getattr(cs, "blob_hash", b"\x00" * 20), 3))
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
        content = subprocess.check_output(["git", "-C", LINUX_REPO, "show", f"v7.2:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, getattr(cs, "blob_hash", b"\x00" * 20), 3))
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
        content = subprocess.check_output(["git", "-C", LINUX_REPO, "show", f"v7.2:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, getattr(cs, "blob_hash", b"\x00" * 20), 3))
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
        content = subprocess.check_output(["git", "-C", LINUX_REPO, "show", f"v7.2:{file_path}"])
        with open(full_path, "wb") as f:
            f.write(content)

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = file_path

        cs.store(m_file_name.get_set(None, cs.current_path))
        cs.store(m_file.set(None, getattr(cs, "blob_hash", b"\x00" * 20), 3))
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
        cs1.store(m_file.set(None, getattr(cs1, "blob_hash", b"\x00" * 20), 3))
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
        cs2.store(m_file.set(None, getattr(cs2, "blob_hash", b"\x00" * 20), 3))
        cs2.store(m_bridge_file.set(2, cs2.ref(m_file_name.fnid), cs2.ref(m_file.fid)))

        cs2.parse()
        self.assertTrue(len(cs2.active_tag_list) > 0, "Preserved function should have recycled prior tag")
        success = cs2.execute()
        self.assertTrue(success, "ChangeSet v7.2 should execute cleanly")



# ----------------------------------------------------------------------------------------------------------------
# Extraction details, golden snapshot and staging (fixture: tests/fixtures/rust/sample.rs)
# ----------------------------------------------------------------------------------------------------------------
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "rust")
FIXTURE_FILE = os.path.join(FIXTURE_DIR, "sample.rs")
GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "rust_ast_golden.json")
HAS_RUSTC = shutil.which("rustc") is not None


def _rustc_dump(path: str) -> str:
    env = dict(os.environ, RUSTC_BOOTSTRAP="1")
    return subprocess.run(
        ["rustc", "--edition=2021", "--crate-type=lib", "-Z", "unpretty=ast-tree", "-o", "-", path],
        capture_output=True, text=True, env=env, check=True,
    ).stdout


def _parse_fixture() -> list[Any]:
    with open(FIXTURE_FILE, encoding="latin-1") as f:
        raw = tuple(f.read().split("\n"))
    return parse_rust_ast_tree(
        _rustc_dump(FIXTURE_FILE), raw, rel_path="sample.rs",
        file_exists=lambda p: os.path.isfile(os.path.join(FIXTURE_DIR, p)),
    )


def _node_json(n: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "type": n.ast_type.name,
        "name": n.name,
        "lines": list(n.extent.line_pos),
        "cols": list(n.extent.char_pos),
    }
    if n.def_name:
        d["def"] = [n.def_name, int(n.linkage)]
    if n.include_target:
        d["include"] = n.include_target
    if n.children:
        d["children"] = [_node_json(c) for c in n.children]
    return d


def _flat(nodes: list[Any]) -> list[Any]:
    return [x for n in nodes for x in n.walk()]


class TestRustSourceText(unittest.TestCase):
    """Span text, columns, identifiers, dump pre-processing and comments (no rustc needed)."""

    def test_snippet_end_is_exclusive(self) -> None:
        from parser.rust_ast.rust_tree_parser import extract_code_snippet
        lines = ("struct A; // x", "fn f() {", "}")
        self.assertEqual(extract_code_snippet(lines, 1, 1, 1, 10), "struct A;")
        self.assertEqual(extract_code_snippet(lines, 2, 3, 1, 2), "fn f() {\n}")

    def test_character_columns_map_to_bytes(self) -> None:
        from parser.rust_ast.rust_tree_parser import RustSource, Span
        line = "/* café */ struct A;".encode("utf-8").decode("latin-1")
        src = RustSource((line,))
        # rustc counts `struct` at character 12; it is byte 13 of the line
        self.assertEqual(src.byte_col(1, 12), 13)
        self.assertEqual(src.span_text(Span((1, 12, 1, 21))), "struct A;")
        self.assertEqual(RustSource(("plain",)).byte_col(1, 3), 3)

    def test_clean_ident(self) -> None:
        from parser.rust_ast.rust_tree_parser import clean_ident
        self.assertEqual(clean_ident("foo#0"), "foo")
        self.assertEqual(clean_ident("r#type#0"), "r#type")
        self.assertEqual(clean_ident({"_type": "Some", "_items": ["a#3"]}), "a")
        self.assertEqual(clean_ident("None"), "")

    def test_strip_unused_blocks(self) -> None:
        from parser.rust_ast.rust_tree_parser import strip_unused_blocks
        dump = "Fn {\n    sig: X,\n    body: Some(\n        Block {\n            stmts: [],\n        },\n    ),\n    tokens: None,\n}"
        out = strip_unused_blocks(dump)
        self.assertIn("body: Stripped,", out)
        self.assertNotIn("Block", out)
        self.assertIn("tokens: None,", out)

    def test_shorten_span_paths(self) -> None:
        from parser.rust_ast.rust_tree_parser import shorten_span_paths
        dump = "span: /tmp/a b/x.rs:1:1: 1:5 (#0),\nspan: /tmp/a b/x.rs:2:1: 2:3 (#0),"
        self.assertEqual(shorten_span_paths(dump).count("@:"), 2)

    def test_scan_comments(self) -> None:
        from parser.rust_ast.rust_tree_parser import scan_comments
        lines = ("// a", "// b", "", "/* x /* y */ z */", "struct A; // t")
        covered = [Line(5, 5, 1, 10)]
        found = [(c.extent.line_pos, c.extent.char_pos, c.name) for c in scan_comments(lines, covered)]
        self.assertEqual(found, [
            ([1, 2], [1, 5], "// a\n// b"),
            ([4, 4], [1, 18], "/* x /* y */ z */"),
            ([5, 5], [11, 15], "// t"),
        ])

    def test_module_file_resolution(self) -> None:
        from parser.rust_ast.rust_tree_parser import RustTreeBuilder, _Ctx
        tree = {"rust/kernel/lib.rs", "rust/kernel/sync.rs", "rust/kernel/sync/arc.rs", "rust/kernel/alloc/mod.rs",
                "drivers/block/rnull/rnull.rs", "drivers/block/rnull/configfs.rs", "rust/kernel/x/y.rs"}

        def resolve(rel: str, ident: str, path_attr: str | None = None) -> str | None:
            b = RustTreeBuilder((), rel, tree.__contains__)
            base = os.path.basename(rel)
            file_dir = os.path.dirname(rel)
            mod_dir = file_dir if base in ("lib.rs", "mod.rs", "main.rs") else os.path.join(file_dir, base[:-3])
            return b.resolve_module_file(ident, path_attr, _Ctx(mod_dir, file_dir))

        self.assertEqual(resolve("rust/kernel/lib.rs", "sync"), "rust/kernel/sync.rs")
        self.assertEqual(resolve("rust/kernel/lib.rs", "alloc"), "rust/kernel/alloc/mod.rs")
        self.assertEqual(resolve("rust/kernel/sync.rs", "arc"), "rust/kernel/sync/arc.rs")
        self.assertEqual(resolve("drivers/block/rnull/rnull.rs", "configfs"), "drivers/block/rnull/configfs.rs")
        self.assertEqual(resolve("rust/kernel/lib.rs", "z", "x/y.rs"), "rust/kernel/x/y.rs")
        self.assertIsNone(resolve("rust/kernel/lib.rs", "missing"))
        self.assertIsNone(resolve("rust/kernel/lib.rs", "z", "../../../etc/passwd"))
        from parser.rust_ast.rust_tree_parser import _PATH_ATTR_RE
        self.assertTrue(_PATH_ATTR_RE.match('#[path = "sub.rs"]'))
        self.assertTrue(_PATH_ATTR_RE.match('#[path = r"sub.rs"]'))
        self.assertTrue(_PATH_ATTR_RE.match('#[path = r#"sub.rs"#]'))
        m = _PATH_ATTR_RE.match('#[path = r#"sub.rs"#]')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "sub.rs")

    def test_driver_macro_type_usage_reference(self) -> None:
        from parser.rust_ast.rust_tree_parser import RustTreeBuilder, Line, _Ctx, Span
        from core.globalstuff import SymbolRole
        code = 'kernel::module_pci_driver! {\n    type: SampleDriver,\n    name: "rust_driver_pci",\n}'
        lines = tuple(code.split("\n"))
        b = RustTreeBuilder(lines, "drivers/x/sample.rs", lambda _: False)
        span = Span((1, 1, 4, 2))
        item = {
            "kind": {"_type": "MacCall", "_items": [{"path": {"span": Span((1, 1, 1, 27))}}]},
            "ident": {"name": "module_pci_driver"},
            "span": span,
            "attrs": [],
            "vis": {"kind": {"_type": "Inherited"}},
        }
        node = b.convert_item(item, _Ctx("drivers/x", "drivers/x"))
        self.assertIsNotNone(node)
        self.assertEqual(node.binding_refs, [("SampleDriver", int(SymbolRole.TypeUsage), 2, 11)])

    def test_mask_code(self) -> None:
        from parser.rust_ast.rust_text import mask_code
        code = 'a /* x /* y */ */ "s\\"t" \'c\' \'\\n\' r#"q"# \'a b // c\nd'
        masked = mask_code(code)
        self.assertEqual(len(masked), len(code))
        self.assertEqual(masked.split(), ["a", "'a", "b", "d"])  # lifetimes are code, literals and comments are not
        self.assertEqual(mask_code('"x\ny"').count("\n"), 1)  # newlines kept: offsets map to the same lines

    def test_macro_fields(self) -> None:
        from parser.rust_ast.rust_text import macro_fields
        code = 'module! {\n  type: M,\n  name: "m, n", // c: d\n  params: { p: i64 { default: 1, description: "x" } },\n}'
        fields = macro_fields(code)
        self.assertEqual([(f.key, f.value) for f in fields][:2], [("type", "M"), ("name", '"m, n"')])
        self.assertEqual(code[fields[1].start:fields[1].end], 'name: "m, n"')
        params = fields[2]
        self.assertEqual([c.key for c in params.children], ["p"])
        self.assertEqual([(g.key, g.value) for g in params.children[0].children], [("default", "1"), ("description", '"x"')])
        self.assertEqual(macro_fields("kernel::module_firmware!(firmware::Builder);"), [])

    def test_binding_refs_and_roles(self) -> None:
        from core.globalstuff import SymbolRole as R
        from parser.rust_ast.rust_text import scan_binding_refs
        code = (
            "fn f(p: *mut bindings::page, q: &'a mut uapi::flat_binder_object) -> bindings::gfp_t {\n"
            "    // bindings::in_comment()\n"
            '    let s = "bindings::in_string";\n'
            "    unsafe { kernel::bindings::kfree(p) };\n"
            "    let x = bindings::GFP_KERNEL | $crate::bindings::irqreturn_IRQ_NONE;\n"
            "    let y = bindings::lru_status::LRU_SKIP; let z = my_bindings::nope;\n"
            "    size_of::<bindings::spinlock_t>(); Some(bindings::seq_lseek); bindings::pci_device_id { a: 0 };\n"
            "    bindings::__BindgenBitfieldUnit::new(); bindings::foo!();\n"
            "}"
        )
        refs = [(r.name, R(r.role).name) for r in scan_binding_refs(code)]
        self.assertEqual(refs, [
            ("page", "TypeUsage"), ("flat_binder_object", "TypeUsage"), ("gfp_t", "TypeUsage"), ("kfree", "Call"),
            ("GFP_KERNEL", "DeclRef"), ("IRQ_NONE", "DeclRef"), ("lru_status", "TypeUsage"),
            ("spinlock_t", "TypeUsage"), ("seq_lseek", "DeclRef"), ("pci_device_id", "TypeUsage"),
        ])
        first = scan_binding_refs(code)[0]
        self.assertEqual(code[first.offset:first.offset + 4], "page")

    def test_c_symbols_and_module_macros(self) -> None:
        from parser.rust_ast.rust_tree_parser import c_symbol_of, is_module_macro
        self.assertEqual(c_symbol_of(["#[no_mangle]"], "f"), "f")
        self.assertEqual(c_symbol_of(["#[inline]", "#[unsafe(no_mangle)]"], "f"), "f")
        self.assertEqual(c_symbol_of(["#[export]"], "f"), "f")
        self.assertEqual(c_symbol_of(['#[export_name = "rust_x"]'], "f"), "rust_x")
        self.assertEqual(c_symbol_of(['#[unsafe(export_name = "rust_y")]'], "f"), "rust_y")
        self.assertIsNone(c_symbol_of(["#[inline]", "#[cfg(CONFIG_X)]"], "f"))
        self.assertTrue(is_module_macro("module") and is_module_macro("kernel::module_platform_driver"))
        self.assertFalse(is_module_macro("pin_init"))


@unittest.skipUnless(HAS_RUSTC, "rustc not installed")
class TestRustExtractionDetails(unittest.TestCase):
    """Names, types, headers, linkage and module links of every construct in the fixture."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.nodes = _parse_fixture()
        cls.all = _flat(cls.nodes)

    def named(self, type_name: str) -> dict[str, Any]:
        return {n.name: n for n in self.all if n.ast_type.name == type_name}

    def test_fields_and_params_keep_names_and_types(self) -> None:
        fields = [n.name for n in self.all if n.ast_type.name == "Rust_Field"]
        for expected in ("pub a: &'a mut T", "b: [u8; 4]", "c: Vec<Option<u32>>", "pub 0: u32", "1: *const u8", "x: i32"):
            self.assertIn(expected, fields)
        params = [n.name for n in self.all if n.ast_type.name == "Rust_Param"]
        for expected in ("x: i32", "...", "&self", "mut y: impl Fn(u8) -> u8", "(a, b): (u8, u8)", "r#type: u8", "&mut self"):
            self.assertIn(expected, params)

    def test_impl_headers(self) -> None:
        impls = self.named("Rust_Impl")
        self.assertIn("impl<T> Drop for Foo<'_, T>", impls)
        self.assertEqual(impls["impl<T> Drop for Foo<'_, T>"].of_trait, "Drop")
        self.assertIn("unsafe impl Send for Unit", impls)
        self.assertEqual(impls["impl !Sync for Tup"].of_trait, "!Sync")
        self.assertIn("impl Foo<'static, u8>", impls)

    def test_signatures_and_qualifiers(self) -> None:
        fns = self.named("Rust_Fn")
        self.assertEqual(fns["cf"].signature, "pub(super) const fn cf<T>(&self, mut y: impl Fn(u8) -> u8, (a, b): (u8, u8), r#type: u8) -> Result<(), Error> where T: Send")
        self.assertEqual(fns["cf"].vis, "pub(super)")
        self.assertEqual(fns["ffi"].qualifiers, ["unsafe", 'extern "C"'])
        self.assertEqual(fns["later"].qualifiers, ["async"])
        self.assertEqual(fns["m"].signature, "fn m(&self)")
        sigs = {n.name for n in self.all if n.ast_type.name == "Rust_Signature"}
        self.assertIn("pub(crate) struct Foo<'a, T: Clone> where T: Copy", sigs)
        self.assertIn("pub trait Tr: Send + Sync", sigs)

    def test_variants_uses_and_types(self) -> None:
        self.assertIn("C = 5", self.named("Rust_Variant"))
        self.assertEqual([f.name for f in self.named("Rust_Variant")["C = 5"].fields], ["x: i32"])
        uses = self.named("Rust_Use")
        self.assertIn("use kernel::{prelude::*, sync::Arc as A}", uses)
        self.assertIn("extern crate alloc", uses)
        self.assertIn("type R<T> = core::result::Result<T, Error>", self.named("Rust_Type"))
        self.assertIn("static EXTV: u32", self.named("Rust_Static"))

    def test_linkage(self) -> None:
        from core.globalstuff import SymbolLinkage
        fns = self.named("Rust_Fn")
        self.assertEqual(fns["ffi"].linkage, SymbolLinkage.ExternalDef)
        self.assertEqual(fns["private"].linkage, SymbolLinkage.InternalStaticDef)
        self.assertEqual(fns["drop"].linkage, SymbolLinkage.ExternalDef)  # trait impl member
        self.assertEqual(fns["m"].linkage, SymbolLinkage.PrototypeDecl)  # no body
        self.assertEqual(fns["ext"].linkage, SymbolLinkage.PrototypeDecl)  # extern block
        self.assertEqual(self.named("Rust_MacroDef")["macro_rules! mm"].linkage, SymbolLinkage.MacroOrKernelDef)
        self.assertIsNone(self.named("Rust_Const")["const _: ()"].def_name)

    def test_modules(self) -> None:
        mods = self.named("Rust_Mod")
        self.assertTrue(mods["inner"].is_inline)
        self.assertFalse(mods["sub"].is_inline)
        self.assertEqual(mods["sub"].include_target, "sample/sub.rs")
        self.assertEqual(mods["deep"].include_target, "sample/inner/deep.rs")
        self.assertEqual(mods["renamed"].include_target, "sample/sub/other.rs")
        self.assertIsNone(mods["missing"].include_target)

    def test_docs_attributes_comments_and_coverage(self) -> None:
        docs = [n.name for n in self.nodes if n.ast_type.name == "Rust_DocComment"]
        self.assertEqual(docs, ["Crate documentation,\ntwo lines.", "Struct doc\nsecond line"])
        attrs = [n.name for n in self.nodes if n.ast_type.name == "Rust_Attribute"]
        self.assertIn("#![allow(dead_code)]", attrs)
        self.assertIn("#[no_mangle]", attrs)
        # Attributes of nested items are children of the item, not tags
        self.assertIn("#[cfg(CONFIG_X86)]", [c.name for c in self.named("Rust_Fn")["new"].children])
        comments = [n.name for n in self.nodes if n.ast_type.name == "Rust_Comment"]
        self.assertIn("/* Block comment /* nested */ still the comment */", comments)
        self.assertIn("// trailing comment", comments)
        with open(FIXTURE_FILE, encoding="latin-1") as f:
            raw = f.read().split("\n")
        covered = {ln for n in self.nodes for ln in range(n.extent.line_pos[0], n.extent.line_pos[1] + 1)}
        self.assertEqual([i for i, ln in enumerate(raw, 1) if ln.strip() and i not in covered], [])

    def test_module_macro_entries(self) -> None:
        from core.globalstuff import SymbolLinkage
        mac = self.named("Rust_MacroCall")["module!"]
        attrs = [c for c in mac.children if c.ast_type.name == "Rust_ModuleAttr"]
        self.assertEqual([a.name for a in attrs], ["type: X", 'name: "x"', 'authors: ["A <a@b.c>", "B"]', 'license: "GPL"', "params"])
        params = attrs[-1]
        self.assertEqual([c.name for c in params.children], ["p: i64"])
        self.assertEqual([c.name for c in params.children[0].children], ["default: 1", 'description: "a, b"'])
        # Definitions: the module's name and its parameters
        defs = {(a.def_name, a.linkage) for a in self.all if a.ast_type.name == "Rust_ModuleAttr" and a.def_name}
        self.assertEqual(defs, {("x", SymbolLinkage.MacroOrKernelDef), ("p", SymbolLinkage.MacroOrKernelDef)})
        self.assertEqual(attrs[1].extent.code, 'name: "x"')
        self.assertEqual(attrs[1].extent.line_pos, [58, 58])

    def test_c_callable_items(self) -> None:
        from core.globalstuff import SymbolLinkage
        fns = self.named("Rust_Fn")
        self.assertEqual(fns["ffi"].c_symbol, "ffi")
        self.assertEqual(fns["exported"].c_symbol, "c_name")
        self.assertEqual(fns["exported"].linkage, SymbolLinkage.ExternalDef)  # private, but C links to it
        self.assertIsNone(fns["ext"].c_symbol)  # declared for Rust, defined in C
        self.assertIsNone(fns["private"].c_symbol)
        self.assertEqual(self.named("Rust_Static")["static mut CVAR: u32"].c_symbol, "CVAR")

    def test_binding_references(self) -> None:
        from core.globalstuff import SymbolRole as R
        fn = self.named("Rust_Fn")["exported"]
        self.assertIn(fn, self.nodes)  # recorded on the top-level node whose tag holds them
        self.assertEqual([(n, R(r).name, ln) for n, r, ln, _c in fn.binding_refs], [
            ("page", "TypeUsage", 74), ("gfp_t", "TypeUsage", 74), ("kfree", "Call", 77), ("GFP_KERNEL", "DeclRef", 78),
            ("BMCR_FULLDPLX", "DeclRef", 78), ("PHY_RUNNING", "DeclRef", 79), ("pci_device_id", "TypeUsage", 80),
        ])
        with open(FIXTURE_FILE, encoding="latin-1") as f:
            raw = f.read().split("\n")
        for name, _role, line, col in fn.binding_refs:
            self.assertTrue(re.match(r"\w+", raw[line - 1][col - 1:]).group(0).endswith(name))
        # Comments, doc comments and attributes hold no uses
        self.assertFalse(any(n.binding_refs for n in self.nodes if n.ast_type.name in ("Rust_Comment", "Rust_DocComment", "Rust_Attribute")))

    def test_non_ascii_columns(self) -> None:
        fn = self.named("Rust_Fn")["after_utf8"]
        self.assertEqual(fn.extent.char_pos, [13, 35])
        self.assertEqual(fn.extent.code, "pub fn after_utf8() {}")

    def test_snippets_are_exact(self) -> None:
        # A field's or parameter's span text is exactly its name (tuple fields are named by their index)
        for n in self.all:
            if n.ast_type.name == "Rust_Param" or (n.ast_type.name == "Rust_Field" and n.field_name):
                self.assertEqual(n.extent.code, n.name)

    def test_golden_snapshot(self) -> None:
        current = {"rustc": rustc_version_string(), "nodes": [_node_json(n) for n in self.nodes]}
        if os.environ.get("REGEN_GOLDEN") == "1" or not os.path.exists(GOLDEN_PATH):
            with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=1, ensure_ascii=False)
                f.write("\n")
        with open(GOLDEN_PATH, encoding="utf-8") as f:
            golden = json.load(f)
        self.assertEqual(
            current["nodes"], golden["nodes"],
            f"Rust parse of {FIXTURE_FILE} changed (golden made with {golden.get('rustc')}, now {current['rustc']}); "
            "regenerate with REGEN_GOLDEN=1 python3 -m unittest tests.test_rust_ast once the change is intended",
        )


def rustc_version_string() -> str:
    from parser.rust_ast.rust_ast import rustc_version
    return rustc_version() or ""


class _FixtureChangeSetMixin:
    """A MockDB pipeline with the fixture tree copied into a version's work tree."""

    def setUp(self) -> None:
        G.DB = MockDB
        G.TE = TECachedDB()
        self.gp = GreatProcessor()
        init_db_layout(self.gp)
        G.TE.start(self.gp.Table_Array, G.DB)
        self.mf = MasterFile()
        G.MF = self.mf

    def tearDown(self) -> None:
        self.mf.clear_all_version()

    def version(self, name: str, vid: int, old_vid: int) -> str:
        d = self.mf.create_temp_dir()
        self.mf.version_dict[name] = d
        self.gp.Version_Name = name
        self.gp.VID = vid
        self.gp.Old_VID = old_vid
        shutil.copytree(FIXTURE_DIR, os.path.join(d, "drivers", "x"))
        return d

    def changeset(self, op: str, path: str, vid: int, old_vid: int = 0) -> ChangeSet:
        cs = ChangeSet(f"{op}\t{path}")
        cs.current_vid = vid
        cs.gp = self.gp
        cs.mf = self.mf
        G.CURRENT_PARSING_FILE = path
        cs.store(m_file_name.get_set(None, cs.current_path))
        if old_vid:
            with cs(REF_OLD):
                cs.store(m_bridge_file.view(
                    ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                    old_vid, cs.ref(m_file_name.fnid, REF_ROOT), None, None, cs.current_path,
                ))
        cs.store(m_file.set(None, b"\x00" * 20, 3))
        cs.store(m_bridge_file.set(vid, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))
        return cs

    @staticmethod
    def count(cs: ChangeSet, table: Any) -> int:
        return sum(1 for op in cs.cs if op[0] == table.table_id)


@unittest.skipUnless(HAS_RUSTC, "rustc not installed")
class TestRustStaging(_FixtureChangeSetMixin, unittest.TestCase):
    """Module links, per-tag symbol definitions, tier-1 recycling and the raw fallback."""

    PATH = "drivers/x/sample.rs"

    def test_module_links_and_definitions(self) -> None:
        from core.DBLayout import m_tag_def, m_blob_symbol_def
        self.version("v7.2", 1, 0)
        cs = self.changeset("A", self.PATH, 1)
        cs.parse()
        targets = {op[2][1] for op in cs.cs if op[0] == m_file_name.table_id}
        self.assertTrue({"drivers/x/sample/sub.rs", "drivers/x/sample/inner/deep.rs", "drivers/x/sample/sub/other.rs"} <= targets)
        includes = sum(1 for op in cs.cs if isinstance(op[0], tuple) and any(j[1] == (8, 0) for j in op[0] if len(j) > 1))
        self.assertEqual(includes, 3)
        # Definitions are stored once per tag (core/SymbolRows.py), not per blob
        self.assertGreater(self.count(cs, m_tag_def), 30)
        self.assertEqual(self.count(cs, m_blob_symbol_def), 0)
        def_names = {op[2][3] for op in cs.cs if op[0] == m_tag_def.table_id}
        self.assertTrue({"exported", "c_name", "CVAR", "x", "p"} <= def_names)
        self.assertTrue(cs.execute())

    def test_binding_references_are_staged_once_per_tag(self) -> None:
        from core.DBLayout import m_ast, m_tag_ref, m_blob_symbol_ref
        from core.globalstuff import ASTT
        self.version("v7.2", 1, 0)
        cs = self.changeset("A", self.PATH, 1)
        cs.parse()
        stubs = [op[2][1] for op in cs.cs if op[0] == (((m_ast.table_id, 0),),) and op[2][2:] == (int(ASTT.Rust_BindingRef),)]
        self.assertEqual(stubs, ["page", "gfp_t", "kfree", "GFP_KERNEL", "BMCR_FULLDPLX", "PHY_RUNNING", "pci_device_id"])
        self.assertEqual(self.count(cs, m_tag_ref), 7)
        self.assertEqual(self.count(cs, m_blob_symbol_ref), 0)
        self.assertTrue(cs.execute())

    def test_unchanged_file_recycles_every_tag(self) -> None:
        self.version("v7.1", 1, 0)
        cs1 = self.changeset("A", self.PATH, 1)
        cs1.parse()
        new_tags = self.count(cs1, m_tag)
        self.assertTrue(cs1.execute())
        G.TE.commit_all()

        self.version("v7.2", 2, 1)
        cs2 = self.changeset("M", self.PATH, 2, old_vid=1)
        cs2.parse()
        self.assertEqual(len(cs2.active_tag_list), new_tags)
        self.assertEqual(self.count(cs2, m_tag), 0)
        self.assertEqual(self.count(cs2, m_map_ast), 0)  # a recycled tag keeps its map
        self.assertEqual(self.count(cs2, m_bridge_tag), new_tags)
        # The recycled tags' definitions and references equal the stored ones: nothing per blob
        from core.DBLayout import m_blob_symbol_def, m_blob_symbol_ref, m_symbol_own, m_tag_ref
        self.assertEqual(self.count(cs2, m_tag_ref), 0)
        self.assertEqual(self.count(cs2, m_blob_symbol_def) + self.count(cs2, m_blob_symbol_ref) + self.count(cs2, m_symbol_own), 0)
        self.assertTrue(cs2.execute())

    def test_rustc_failure_falls_back_to_raw_tag(self) -> None:
        from unittest import mock
        from parser.rust_ast import rust_ast
        self.version("v7.2", 1, 0)
        cs = self.changeset("A", self.PATH, 1)
        with mock.patch.object(rust_ast, "run_rustc_ast_tree", return_value=""):
            cs.parse()
        self.assertEqual(self.count(cs, m_tag), 1)
        self.assertEqual(self.count(cs, m_bridge_tag), 1)
        self.assertTrue(cs.execute())


if __name__ == "__main__":
    unittest.main()
