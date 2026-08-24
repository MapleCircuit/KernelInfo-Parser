"""tests/test_kconfig_ast.py - Unit & Integration Test Suite for Kconfig AST Subsystem.

Validates lexer tokenization, recursive-descent grammar parsing, expression precedence,
ChangeSet relational mapping, and MockDB pipeline execution.
"""
from __future__ import annotations
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.globalstuff import G, ASTT, REF_ROOT, REF_C_AST
from core.GreatProcessor import GreatProcessor
from core.TableHandling import ChangeSet
from core.DBLayout import (
    init_db_layout,
    m_file_name,
    m_file,
    m_bridge_file,
    m_ast,
    m_ast_container,
    m_ast_include,
    m_tag,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
    m_kconfig_symbol,
    m_kconfig_relation,
    m_kconfig_tree,
)
from db_engine import MockDB
from table_engine import TECachedDB
from parser.kconfig_ast.kconfig_lexer import KconfigLexer, TokenType
from parser.kconfig_ast.kconfig_parser import (
    KconfigParser,
    KconfigConfig,
    KconfigMenu,
    KconfigChoice,
    KconfigIf,
    KconfigComment,
    KconfigSource,
    KconfigMainmenu,
    ExprOp,
    TYPE_BOOL,
    TYPE_TRISTATE,
    TYPE_STRING,
    TYPE_HEX,
    TYPE_INT,
)
from parser.kconfig_ast.kconfig_ast import kconfig_ast_parse


SAMPLE_KCONFIG = """# Sample Kconfig for Testing
mainmenu "Linux Kernel Configuration"

config MODULES
\tbool "Enable loadable module support"
\tdefault y
\thelp
\t  This option allows the kernel to load external modules.
\t  Say Y unless you are building a monolithic kernel.

menu "General setup"

config LOCALVERSION
\tstring "Local version - append to kernel release"
\thelp
\t  Append an extra string to the end of your kernel version.

config DEFAULT_HOSTNAME
\tstring "Default hostname"
\tdefault "(none)"

endmenu

if MODULES

menuconfig NETDEVICES
\ttristate "Network device support"
\tdepends on NET
\tselect NET_CORE
\timply E1000
\tdefault y
\thelp
\t  Support for network devices.

choice
\tprompt "Choose compression algorithm"
\tdefault ZLIB_COMPRESSION

config ZLIB_COMPRESSION
\tbool "ZLIB"

config LZO_COMPRESSION
\tbool "LZO"

endchoice

endif

source "drivers/net/Kconfig"
"""


class TestKconfigLexer(unittest.TestCase):
    def test_basic_tokens(self) -> None:
        text = 'config FOO\n\tbool "Prompt"\n\tdefault y\n'
        lexer = KconfigLexer(text)
        tokens = lexer.tokenize()
        types = [t.type for t in tokens if t.type != TokenType.NEWLINE and t.type != TokenType.EOF]
        self.assertIn(TokenType.CONFIG, types)
        self.assertIn(TokenType.SYMBOL, types)
        self.assertIn(TokenType.BOOL, types)
        self.assertIn(TokenType.CONST_STRING, types)
        self.assertIn(TokenType.DEFAULT, types)

    def test_help_indentation(self) -> None:
        text = """config BAR
\tbool "Bar"
\thelp
\t  Line 1 of help.
\t  Line 2 of help.

config NEXT
\tbool "Next"
"""
        lexer = KconfigLexer(text)
        tokens = lexer.tokenize()
        help_tokens = [t for t in tokens if t.type == TokenType.HELP_TEXT]
        self.assertEqual(len(help_tokens), 1)
        self.assertIn("Line 1 of help.", help_tokens[0].value)
        self.assertIn("Line 2 of help.", help_tokens[0].value)

    def test_line_continuation(self) -> None:
        text = "config MULTILINE\n\tdepends on FOO && \\\n\t\tBAR\n"
        lexer = KconfigLexer(text)
        tokens = lexer.tokenize()
        sym_values = [t.value for t in tokens if t.type == TokenType.SYMBOL]
        self.assertIn("MULTILINE", sym_values)
        self.assertIn("FOO", sym_values)
        self.assertIn("BAR", sym_values)


class TestKconfigParser(unittest.TestCase):
    def test_parse_sample(self) -> None:
        lexer = KconfigLexer(SAMPLE_KCONFIG)
        tokens = lexer.tokenize()
        parser = KconfigParser(tokens, SAMPLE_KCONFIG)
        ast_items = parser.parse()

        self.assertGreater(len(ast_items), 0)
        self.assertIsInstance(ast_items[0], KconfigMainmenu)
        self.assertEqual(ast_items[0].title, "Linux Kernel Configuration")

        # Find MODULES config
        modules_cfg = next((it for it in ast_items if isinstance(it, KconfigConfig) and it.name == "MODULES"), None)
        self.assertIsNotNone(modules_cfg)
        self.assertEqual(modules_cfg.sym_type, TYPE_BOOL)
        self.assertEqual(modules_cfg.prompt, "Enable loadable module support")
        self.assertIn("load external modules", modules_cfg.help_text)

        # Find General Setup Menu
        menu = next((it for it in ast_items if isinstance(it, KconfigMenu) and it.title == "General setup"), None)
        self.assertIsNotNone(menu)
        self.assertEqual(len(menu.children), 2)

        # Find If block
        if_block = next((it for it in ast_items if isinstance(it, KconfigIf)), None)
        self.assertIsNotNone(if_block)
        self.assertEqual(if_block.cond.value, "MODULES")

        # Find Source
        src = next((it for it in ast_items if isinstance(it, KconfigSource)), None)
        self.assertIsNotNone(src)
        self.assertEqual(src.path, "drivers/net/Kconfig")

    def test_expression_precedence(self) -> None:
        text = 'config TEST_EXPR\n\tdepends on A || B && !C = "val"\n'
        lexer = KconfigLexer(text)
        tokens = lexer.tokenize()
        parser = KconfigParser(tokens, text)
        ast_items = parser.parse()

        cfg = ast_items[0]
        self.assertEqual(len(cfg.depends_on), 1)
        expr = cfg.depends_on[0]
        # Top level must be OR: A || (B && (!C = "val"))
        self.assertEqual(expr.op, ExprOp.OR)
        self.assertEqual(expr.left.op, ExprOp.SYMBOL_REF)
        self.assertEqual(expr.left.value, "A")
        self.assertEqual(expr.right.op, ExprOp.AND)


class TestKconfigAstIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.gp = GreatProcessor()
        init_db_layout(self.gp)
        G.DB = MockDB
        G.TE = TECachedDB()
        G.TE.start(self.gp.Table_Array, MockDB)
        self.gp.VID = 1
        self.gp.Old_VID = 0

    def test_changeset_kconfig_execution(self) -> None:
        cs = ChangeSet("A\tKconfig")
        cs.gp = self.gp
        cs.mf = None
        cs.raw_content = SAMPLE_KCONFIG

        # Seed file lifecycle operations
        cs.store(m_file_name.set(None, "Kconfig"))
        cs.store(m_file.set(None, 1, 0, 2, "A", "0"))
        cs.store(m_bridge_file.set(1, ((m_file_name.table_id, 0), 3, (REF_ROOT,)), ((m_file.table_id, 0), 3, (REF_ROOT,))))

        # Run Kconfig AST parser
        kconfig_ast_parse(cs)

        self.assertGreater(len(cs.cs), 10)

        # Execute ChangeSet against MockDB & TECachedDB
        executed = cs.execute()
        self.assertTrue(executed)

        # Validate that symbol and tree rows were created in TE cache
        sym_rows = G.TE._cached_rows.get(m_kconfig_symbol.table_id, [])
        self.assertGreater(len(sym_rows), 0)
        sym_names = [row[3] for row in sym_rows]
        self.assertIn("MODULES", sym_names)
        self.assertIn("NETDEVICES", sym_names)
        self.assertIn("DEFAULT_HOSTNAME", sym_names)
        # Check version tracking on symbols
        for row in sym_rows:
            self.assertEqual(row[1], self.gp.VID)  # vid_s
            self.assertEqual(row[2], 0)            # vid_e (active)

        # Validate relations
        rel_rows = G.TE.queued_set.get(m_kconfig_relation.table_id, {})
        self.assertGreater(len(rel_rows), 0)

        # Validate menu tree
        tree_rows = G.TE.queued_set.get(m_kconfig_tree.table_id, {})
        self.assertGreater(len(tree_rows), 0)
        # Check version binding on tree records
        for _, trow in tree_rows.items():
            self.assertEqual(trow[1], self.gp.VID)  # vid

    def test_kconfig_export_import_roundtrip(self) -> None:
        from webapp.main import export_kconfig_file, import_kconfig_file
        test_symbols = {
            "MODULES": "y",
            "NETDEVICES": "m",
            "WATCHDOG": "n",
            "DEFAULT_HOSTNAME": "(none)",
            "NR_CPUS": "64",
        }
        export_res = export_kconfig_file("v3.0", {"symbols": test_symbols})
        self.assertIn("CONFIG_MODULES=y", export_res["content"])
        self.assertIn("CONFIG_NETDEVICES=m", export_res["content"])
        self.assertIn("# CONFIG_WATCHDOG is not set", export_res["content"])
        self.assertIn('CONFIG_DEFAULT_HOSTNAME="(none)"', export_res["content"])
        self.assertIn("CONFIG_NR_CPUS=64", export_res["content"])

        import_res = import_kconfig_file("v3.0", {"content": export_res["content"]})
        imported_symbols = import_res["symbols"]
        self.assertEqual(imported_symbols.get("MODULES"), "y")
        self.assertEqual(imported_symbols.get("NETDEVICES"), "m")
        self.assertEqual(imported_symbols.get("WATCHDOG"), "n")
        self.assertEqual(imported_symbols.get("DEFAULT_HOSTNAME"), "(none)")
        self.assertEqual(imported_symbols.get("NR_CPUS"), "64")

    def test_kconfig_env_presets(self) -> None:
        from webapp.main import get_kconfig_env_presets
        presets = get_kconfig_env_presets("v3.0")
        self.assertIn("architectures", presets)
        self.assertIn("compilers", presets)

        arch_ids = [a["id"] for a in presets["architectures"]]
        # Verify dynamically discovered architectures in Linux v3.0
        self.assertIn("x86_64", arch_ids)
        self.assertIn("i386", arch_ids)
        self.assertIn("arm", arch_ids)
        self.assertIn("alpha", arch_ids)
        self.assertIn("mips", arch_ids)
        self.assertIn("powerpc_64", arch_ids)
        self.assertIn("s390", arch_ids)
        self.assertIn("sparc64", arch_ids)
        self.assertIn("xtensa", arch_ids)
        self.assertGreaterEqual(len(arch_ids), 20)

        compiler_ids = [c["id"] for c in presets["compilers"]]
        self.assertIn("gcc", compiler_ids)
        self.assertIn("clang", compiler_ids)

        x86_64_preset = next(a for a in presets["architectures"] if a["id"] == "x86_64")
        self.assertEqual(x86_64_preset["symbols"]["64BIT"], "y")
        self.assertEqual(x86_64_preset["symbols"]["X86_64"], "y")
        self.assertEqual(x86_64_preset["symbols"]["ARCH"], "x86")

    def test_kbuild_parser_composite_and_direct(self) -> None:
        from parser.kbuild_parser import KbuildParser
        parser = KbuildParser()

        makefile_snippet = """
# Sample Kbuild Makefile
obj-$(CONFIG_EXT4_FS) += ext4.o
ext4-y := balloc.o bitmap.o dir.o file.o fsync.o ialloc.o inode.o page-io.o \\
          ioctl.o namei.o super.o symlink.o hash.o resize.o extents.o \\
          ext4_jbd2.o migrate.o mballoc.o block_validity.o move_extent.o \\
          mmp.o
ext4-$(CONFIG_EXT4_FS_POSIX_ACL) += acl.o
ext4-$(CONFIG_EXT4_FS_SECURITY)  += xattr_security.o

obj-$(CONFIG_DRBD) += drbd.o
obj-$(CONFIG_W83627HF_WDT) += w83627hf_wdt.o
obj-y += core.o
"""
        bindings = parser.parse_makefile_content(makefile_snippet, dir_path="fs/ext4")
        self.assertGreater(len(bindings), 5)

        # Check composite bindings for EXT4_FS
        ext4_syms = [b for b in bindings if b.symbol_name == "EXT4_FS"]
        ext4_files = [b.source_file_rel for b in ext4_syms]
        self.assertIn("fs/ext4/balloc.c", ext4_files)
        self.assertIn("fs/ext4/inode.c", ext4_files)
        self.assertIn("fs/ext4/super.c", ext4_files)

        # Check sub-config in composite
        acl_syms = [b for b in bindings if b.symbol_name == "EXT4_FS_POSIX_ACL"]
        self.assertEqual(len(acl_syms), 1)
        self.assertEqual(acl_syms[0].source_file_rel, "fs/ext4/acl.c")

        # Check direct obj bindings
        drbd_syms = [b for b in bindings if b.symbol_name == "DRBD"]
        self.assertEqual(len(drbd_syms), 1)
        self.assertEqual(drbd_syms[0].source_file_rel, "fs/ext4/drbd.c")

    def test_kconfig_tree_scoped_hierarchy(self) -> None:
        from webapp.main import get_kconfig_tree
        tree_res = get_kconfig_tree("v3.0", arch="x86")
        self.assertIn("nodes", tree_res)
        self.assertEqual(tree_res["arch"], "x86")

        nodes = tree_res["nodes"]
        # Root nodes (parent_id == 0) should be cleanly organized top-level categories
        root_nodes = [n for n in nodes if n["parent_id"] == 0]
        visible_root_nodes = [n for n in root_nodes if (n["node_type"] in (1, 2) or n["prompt"])]
        
        # Verify that visible root nodes contain authentic high-level categories (~15-20)
        self.assertLessEqual(len(visible_root_nodes), 25)
        self.assertGreaterEqual(len(visible_root_nodes), 10)
        self.assertLessEqual(len(root_nodes), 100)


    def test_kconfig_symbol_detail_compiled_files(self) -> None:
        from webapp.main import get_kconfig_symbol_detail
        detail = get_kconfig_symbol_detail("v3.0", "EXT4_FS")
        self.assertIn("compiled_files", detail)
        self.assertIsInstance(detail["compiled_files"], list)
        if detail["compiled_files"]:
            file_paths = [f["file_path"] for f in detail["compiled_files"]]
            self.assertTrue(any("ext4" in p.lower() for p in file_paths))


    def test_kconfig_search_with_and_without_config_prefix(self) -> None:
        from webapp.main import search_kconfig_symbols
        # Search with CONFIG_ prefix
        res_with_prefix = search_kconfig_symbols("v3.0", q="CONFIG_USER_STACKTRACE_SUPPORT")
        self.assertIn("symbols", res_with_prefix)
        names_with_prefix = [s["name"] for s in res_with_prefix["symbols"]]
        self.assertIn("USER_STACKTRACE_SUPPORT", names_with_prefix)

        # Search without prefix
        res_bare = search_kconfig_symbols("v3.0", q="USER_STACKTRACE_SUPPORT")
        self.assertIn("symbols", res_bare)
        names_bare = [s["name"] for s in res_bare["symbols"]]
        self.assertIn("USER_STACKTRACE_SUPPORT", names_bare)


    def test_kconfig_defconfig_discovery(self) -> None:
        from webapp.main import get_kconfig_defconfigs
        # Test x86 defconfigs
        x86_res = get_kconfig_defconfigs("v3.0", arch="x86")
        self.assertIn("defconfigs", x86_res)
        self.assertGreaterEqual(x86_res["total_count"], 2)
        names = [d["name"] for d in x86_res["defconfigs"]]
        self.assertIn("x86_64_defconfig", names)
        self.assertIn("i386_defconfig", names)
        self.assertIsNotNone(x86_res["canonical_default"])
        self.assertEqual(x86_res["canonical_default"]["name"], "x86_64_defconfig")

        # Test ARM multi-defconfig discovery (100+ board configs)
        arm_res = get_kconfig_defconfigs("v3.0", arch="arm")
        self.assertIn("defconfigs", arm_res)
        self.assertGreaterEqual(arm_res["total_count"], 50)
        self.assertIsNotNone(arm_res["canonical_default"])

    def test_kconfig_defconfig_content_parsing(self) -> None:
        from webapp.main import get_kconfig_defconfig_content
        # Test x86_64_defconfig content
        x86_64_cfg = get_kconfig_defconfig_content("v3.0", file_path="arch/x86/configs/x86_64_defconfig")
        self.assertIn("values", x86_64_cfg)
        self.assertGreaterEqual(x86_64_cfg["symbol_count"], 10)
        self.assertEqual(x86_64_cfg["values"].get("64BIT"), "y")

        # Test i386_defconfig content
        i386_cfg = get_kconfig_defconfig_content("v3.0", file_path="arch/x86/configs/i386_defconfig")
        self.assertIn("values", i386_cfg)
        self.assertGreaterEqual(i386_cfg["symbol_count"], 10)
        self.assertEqual(i386_cfg["values"].get("64BIT", "n"), "n")

    def test_kconfig_symbol_detail_resolution(self) -> None:
        from webapp.main import get_kconfig_symbol_detail
        # Test with CONFIG_ prefix
        detail_prefix = get_kconfig_symbol_detail("v3.0", "CONFIG_EXT4_FS")
        self.assertEqual(detail_prefix["name"], "EXT4_FS")
        self.assertEqual(detail_prefix["type_name"], "tristate")
        self.assertIn("compiled_files", detail_prefix)

        # Test without prefix
        detail_bare = get_kconfig_symbol_detail("v3.0", "EXT4_FS")
        self.assertEqual(detail_bare["name"], "EXT4_FS")
        self.assertEqual(detail_bare["type_name"], "tristate")

    def test_kconfig_tree_relations(self) -> None:
        from webapp.main import get_kconfig_tree
        tree_res = get_kconfig_tree("v3.0", arch="x86")
        self.assertIn("relations", tree_res)
        self.assertIn("reverse_relations", tree_res)
        self.assertGreater(len(tree_res["relations"]), 10)

        # Check nodes have relation fields
        for node in tree_res["nodes"]:
            self.assertIn("depends_on", node)
            self.assertIn("selects", node)
            self.assertIn("selected_by", node)

    def test_kconfig_validate_constraints(self) -> None:
        from webapp.main import validate_kconfig_assignments
        # Test forced select cascade
        payload = {"symbols": {"EXT4_FS": "y", "NET": "y"}}
        val_res = validate_kconfig_assignments("v3.0", payload)
        self.assertIn("forced_symbols", val_res)
        self.assertIn("adjusted_symbols", val_res)


if __name__ == "__main__":
    unittest.main()


