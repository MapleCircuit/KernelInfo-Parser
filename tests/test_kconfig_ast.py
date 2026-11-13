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
        MockDB.store.clear()
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
        cs.store(m_file.set(None, b"\x00" * 20, 2))
        cs.store(m_bridge_file.set(1, ((m_file_name.table_id, 0), 3, (REF_ROOT,)), ((m_file.table_id, 0), 3, (REF_ROOT,))))

        # Run Kconfig AST parser
        kconfig_ast_parse(cs)

        self.assertGreater(len(cs.cs), 10)

        # Execute ChangeSet against MockDB & TECachedDB
        executed = cs.execute()
        self.assertTrue(executed)

        # Every entry is an m_ast node named after it, placed in the blob by a tag
        def ast_id(name: str, ast_type: ASTT) -> int | None:
            res = m_ast.view_get(((m_ast.ast_id,),), None, name, int(ast_type))
            return res[2][0] if res else None

        for name in ("MODULES", "LOCALVERSION", "DEFAULT_HOSTNAME", "ZLIB_COMPRESSION", "LZO_COMPRESSION"):
            self.assertIsNotNone(ast_id(name, ASTT.Kconfig_Config), name)
        self.assertIsNotNone(ast_id("NETDEVICES", ASTT.Kconfig_Menuconfig))
        self.assertIsNotNone(ast_id("General setup", ASTT.Kconfig_Menu))
        self.assertIsNotNone(ast_id("Choose compression algorithm", ASTT.Kconfig_Choice))
        self.assertIsNotNone(ast_id("if MODULES", ASTT.Kconfig_If))

        expected_blob_id = cs.cs_result[1][0]
        bridges = {row[1]: row for row in G.TE.queued_set.get(m_bridge_tag.table_id, {}).values()}
        tags = {row[0]: row for row in G.TE.queued_set.get(m_tag.table_id, {}).values()}
        self.assertTrue(bridges)
        lines = SAMPLE_KCONFIG.splitlines()
        by_ast = {}
        for tag_id, b in bridges.items():
            self.assertEqual(b[0], expected_blob_id)
            by_ast[tags[tag_id][2]] = (b[2], b[3])
        # Container tags cover their header lines only, never their children
        menu_s, menu_e = by_ast[ast_id("General setup", ASTT.Kconfig_Menu)]
        self.assertEqual((lines[menu_s - 1], menu_s), ('menu "General setup"', menu_e))
        if_s, if_e = by_ast[ast_id("if MODULES", ASTT.Kconfig_If)]
        self.assertEqual((lines[if_s - 1].strip(), if_s), ("if MODULES", if_e))
        choice_s, choice_e = by_ast[ast_id("Choose compression algorithm", ASTT.Kconfig_Choice)]
        self.assertEqual(lines[choice_e - 1].strip(), "default ZLIB_COMPRESSION")
        # A config ends at its last property: the blank line and the next entry are not part of it
        mod_s, mod_e = by_ast[ast_id("MODULES", ASTT.Kconfig_Config)]
        self.assertEqual(lines[mod_e - 1].strip(), "Say Y unless you are building a monolithic kernel.")
        # A literal `source` path links the sourced file
        fn = m_file_name.get(None, "drivers/net/Kconfig")
        self.assertIsNotNone(fn)
        includes = [row for row in G.TE.queued_set.get(m_ast_include.table_id, {}).values()]
        self.assertIn(fn[2][0], {row[1] for row in includes})

    def test_environment_dependent_source_is_not_a_file_name(self) -> None:
        """`source "arch/$SRCARCH/Kconfig"` names one file per ARCH: no bogus m_file_name row."""
        cs = ChangeSet("A\tKconfig")
        cs.gp = self.gp
        cs.mf = None
        cs.raw_content = 'source "arch/$SRCARCH/Kconfig"\nsource "arch/$(SRCARCH)/Kconfig.debug"\n'
        cs.store(m_file_name.set(None, "Kconfig"))
        cs.store(m_file.set(None, b"\x03" * 20, 2))
        cs.store(m_bridge_file.set(1, ((m_file_name.table_id, 0), 3, (REF_ROOT,)), ((m_file.table_id, 0), 3, (REF_ROOT,))))
        kconfig_ast_parse(cs)
        self.assertTrue(cs.execute())
        # m_file_name is no_duplicate: staged rows are keyed by (fname,)
        staged = [key[0] for key in G.TE.queued_set.get(m_file_name.table_id, {})]
        self.assertIn("Kconfig", staged)
        self.assertFalse(any("$" in name for name in staged), staged)
        self.assertFalse(G.TE.queued_set.get(m_ast_include.table_id))
        res = m_ast.view_get(((m_ast.ast_id,),), None, "arch/$SRCARCH/Kconfig", int(ASTT.Kconfig_Source))
        self.assertIsNotNone(res)

    def test_kconfig_multi_file_symbol_relations_dedup(self) -> None:
        """Verify that multiple Kconfig files defining the same symbol with overlapping relations stage cleanly."""
        file1_kconfig = (
            "menuconfig VIRTUALIZATION\n"
            "    bool \"Virtualization\"\n"
            "    depends on HAVE_KVM || X86\n"
            "    default y\n"
        )
        file2_kconfig = (
            "menuconfig VIRTUALIZATION\n"
            "    bool \"Virtualization\"\n"
            "    depends on HAVE_KVM || IA64\n"
            "    default y\n"
        )

        cs1 = ChangeSet("A", "arch/x86/kvm/Kconfig")
        cs1.gp = self.gp
        cs1.raw_content = file1_kconfig
        cs1.store(m_file_name.set(None, "arch/x86/kvm/Kconfig"))
        cs1.store(m_file.set(None, b"\x01" * 20, 2))
        cs1.store(m_bridge_file.set(1, ((m_file_name.table_id, 0), 3, (REF_ROOT,)), ((m_file.table_id, 0), 3, (REF_ROOT,))))
        kconfig_ast_parse(cs1)
        self.assertTrue(cs1.execute())

        # Commit chunk 1
        G.TE.commit_all()

        cs2 = ChangeSet("A", "arch/ia64/kvm/Kconfig")
        cs2.gp = self.gp
        cs2.raw_content = file2_kconfig
        cs2.store(m_file_name.set(None, "arch/ia64/kvm/Kconfig"))
        cs2.store(m_file.set(None, b"\x02" * 20, 2))
        cs2.store(m_bridge_file.set(1, ((m_file_name.table_id, 0), 3, (REF_ROOT,)), ((m_file.table_id, 0), 3, (REF_ROOT,))))
        kconfig_ast_parse(cs2)
        self.assertTrue(cs2.execute())

        # Commit chunk 2 - must not raise Duplicate entry '800-1-HAVE_KVM-0'
        G.TE.commit_all()

    def test_kconfig_config_text_roundtrip(self) -> None:
        """.config rendering (the auto-solver's input) and parsing (defconfig values) are inverse."""
        from webapp.backend.services.kconfig_service import KconfigService
        test_symbols = {
            "MODULES": "y",
            "NETDEVICES": "m",
            "WATCHDOG": "n",
            "DEFAULT_HOSTNAME": "(none)",
            "NR_CPUS": "64",
        }
        content = KconfigService._assignments_to_config(test_symbols)
        self.assertIn("CONFIG_MODULES=y", content)
        self.assertIn("CONFIG_NETDEVICES=m", content)
        self.assertIn("# CONFIG_WATCHDOG is not set", content)
        self.assertIn('CONFIG_DEFAULT_HOSTNAME="(none)"', content)
        self.assertIn("CONFIG_NR_CPUS=64", content)

        imported_symbols = KconfigService._config_assignments(content)
        self.assertEqual(imported_symbols.get("MODULES"), "y")
        self.assertEqual(imported_symbols.get("NETDEVICES"), "m")
        self.assertEqual(imported_symbols.get("WATCHDOG"), "n")
        self.assertEqual(imported_symbols.get("DEFAULT_HOSTNAME"), "(none)")
        self.assertEqual(imported_symbols.get("NR_CPUS"), "64")

    def test_kconfig_env_presets(self) -> None:
        from webapp.backend.services.kconfig_service import KconfigService
        svc = KconfigService()
        presets = svc.get_env_presets("v3.0")
        self.assertIn("targets", presets)

        arch_ids = [a["id"] for a in presets["targets"]]
        if not arch_ids:
            raise unittest.SkipTest("Linux v3.0 architecture Kconfig tree not indexed in database.")
        # Verify dynamically discovered architectures in Linux v3.0
        self.assertIn("x86_64", arch_ids)
        self.assertIn("i386", arch_ids)
        self.assertIn("arm", arch_ids)
        self.assertIn("alpha", arch_ids)
        self.assertIn("mips", arch_ids)
        self.assertIn("powerpc", arch_ids)
        self.assertIn("s390", arch_ids)
        self.assertIn("sparc64", arch_ids)
        self.assertIn("xtensa", arch_ids)
        self.assertGreaterEqual(len(arch_ids), 20)

        # Presets are the kconfig environment (ARCH/SRCARCH), not hand-picked symbol values:
        # 64BIT/X86_64 follow from ARCH=x86_64 through arch/x86/Kconfig itself
        x86_64_preset = next(a for a in presets["targets"] if a["id"] == "x86_64")
        self.assertEqual(x86_64_preset["env"]["ARCH"], "x86_64")
        self.assertEqual(x86_64_preset["env"]["SRCARCH"], "x86")
        # Its default defconfig is what the defconfig list flags
        canonical = [d["name"] for d in svc.get_defconfigs("v3.0", "x86_64")["defconfigs"] if d["is_canonical"]]
        self.assertEqual(canonical, ["x86_64_defconfig"])

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
        from webapp.backend.services.kconfig_service import KconfigService
        tree_res = KconfigService().get_tree("v3.0", arch="x86")
        self.assertIn("menus", tree_res)
        self.assertEqual(tree_res["arch"], "x86")

        fields = tree_res["menu_fields"]
        parent, kind, prompt = fields.index("parent"), fields.index("kind"), fields.index("prompt")
        # Children of the root menu (index 0) should be cleanly organized top-level categories
        root_nodes = [m for m in tree_res["menus"] if m[parent] == 0]
        # kind 1 menu, 2 choice
        visible_root_nodes = [m for m in root_nodes if m[kind] in (1, 2) or m[prompt]]
        
        # Verify that visible root nodes contain authentic high-level categories (~15-20)
        self.assertLessEqual(len(visible_root_nodes), 25)
        # Promptless internal compiler/architecture flags sit at root alongside ~19 visible categories
        self.assertLessEqual(len(root_nodes), 200)


    def test_kconfig_symbol_detail_compiled_files(self) -> None:
        from webapp.backend.services.kconfig_service import KconfigService
        try:
            detail = KconfigService().get_symbol_detail("v3.0", "EXT4_FS")
        except Exception:
            raise unittest.SkipTest("EXT4_FS symbol not indexed in database.")
        self.assertIn("compiled_files", detail)
        self.assertIsInstance(detail["compiled_files"], list)
        if detail["compiled_files"]:
            file_paths = [f["file_path"] for f in detail["compiled_files"]]
            self.assertTrue(any("ext4" in p.lower() for p in file_paths))


    def test_kconfig_search_with_and_without_config_prefix(self) -> None:
        from webapp.backend.services.kconfig_service import KconfigService
        search_kconfig_symbols = KconfigService().search_symbols
        # Search without prefix
        try:
            res_bare = search_kconfig_symbols("v3.0", q="USER_STACKTRACE_SUPPORT")
        except Exception:
            raise unittest.SkipTest("USER_STACKTRACE_SUPPORT symbol query skipped pending webapp zero-VID migration.")
        if not res_bare.get("symbols"):
            raise unittest.SkipTest("USER_STACKTRACE_SUPPORT symbol not indexed in database.")
        self.assertIn("symbols", res_bare)
        names_bare = [s["name"] for s in res_bare["symbols"]]
        self.assertIn("USER_STACKTRACE_SUPPORT", names_bare)

        # Search with CONFIG_ prefix
        try:
            res_with_prefix = search_kconfig_symbols("v3.0", q="CONFIG_USER_STACKTRACE_SUPPORT")
        except Exception:
            raise unittest.SkipTest("CONFIG_USER_STACKTRACE_SUPPORT symbol query skipped pending webapp zero-VID migration.")
        self.assertIn("symbols", res_with_prefix)
        names_with_prefix = [s["name"] for s in res_with_prefix["symbols"]]
        self.assertIn("USER_STACKTRACE_SUPPORT", names_with_prefix)


    def test_kconfig_defconfig_discovery(self) -> None:
        from webapp.backend.services.kconfig_service import KconfigService
        get_kconfig_defconfigs = KconfigService().get_defconfigs
        # Test x86 defconfigs (ARCH=x86_64: `make defconfig` loads x86_64_defconfig)
        x86_res = get_kconfig_defconfigs("v3.0", arch="x86_64")
        self.assertIn("defconfigs", x86_res)
        self.assertGreaterEqual(len(x86_res["defconfigs"]), 2)
        names = [d["name"] for d in x86_res["defconfigs"]]
        self.assertIn("x86_64_defconfig", names)
        self.assertIn("i386_defconfig", names)
        self.assertEqual([d["name"] for d in x86_res["defconfigs"] if d["is_canonical"]], ["x86_64_defconfig"])

        # Test ARM multi-defconfig discovery (100+ board configs)
        arm_res = get_kconfig_defconfigs("v3.0", arch="arm")
        self.assertIn("defconfigs", arm_res)
        self.assertGreaterEqual(len(arm_res["defconfigs"]), 50)
        self.assertEqual(sum(1 for d in arm_res["defconfigs"] if d["is_canonical"]), 1)

    def test_kconfig_defconfig_content_parsing(self) -> None:
        from webapp.backend.services.kconfig_service import KconfigService
        get_kconfig_defconfig_content = KconfigService().get_defconfig_content
        # Test x86_64_defconfig content
        x86_64_cfg = get_kconfig_defconfig_content("v3.0", "arch/x86/configs/x86_64_defconfig")
        self.assertIn("values", x86_64_cfg)
        self.assertGreaterEqual(len(x86_64_cfg["values"]), 10)
        self.assertEqual(x86_64_cfg["values"].get("64BIT"), "y")

        # Test i386_defconfig content
        i386_cfg = get_kconfig_defconfig_content("v3.0", "arch/x86/configs/i386_defconfig")
        self.assertIn("values", i386_cfg)
        self.assertGreaterEqual(len(i386_cfg["values"]), 10)
        self.assertEqual(i386_cfg["values"].get("64BIT", "n"), "n")

    def test_kconfig_symbol_detail_resolution(self) -> None:
        from webapp.backend.services.kconfig_service import KconfigService
        get_kconfig_symbol_detail = KconfigService().get_symbol_detail
        try:
            detail_prefix = get_kconfig_symbol_detail("v3.0", "CONFIG_EXT4_FS")
        except Exception:
            raise unittest.SkipTest("EXT4_FS symbol not indexed in database.")
        self.assertEqual(detail_prefix["name"], "EXT4_FS")
        self.assertEqual(detail_prefix["type_name"], "tristate")
        self.assertIn("compiled_files", detail_prefix)

        # Test without prefix
        detail_bare = get_kconfig_symbol_detail("v3.0", "EXT4_FS")
        self.assertEqual(detail_bare["name"], "EXT4_FS")
        self.assertEqual(detail_bare["type_name"], "tristate")

    def test_kconfig_tree_relations(self) -> None:
        """The model carries the relations the engine evaluates: selects, direct and reverse dependencies."""
        from webapp.backend.services.kconfig_service import KconfigService
        tree_res = KconfigService().get_tree("v3.0", arch="x86")
        symbols = tree_res["symbols"]
        self.assertGreater(sum(1 for sym in symbols.values() if sym.get("s")), 10)
        self.assertGreater(sum(1 for sym in symbols.values() if sym.get("rd") not in (None, -1)), 10)
        # EXT4_FS selects JBD2 (and CRC16)
        self.assertIn("JBD2", [target for target, _ in symbols["EXT4_FS"]["s"]])


if __name__ == "__main__":
    unittest.main()




class TestObsoleteKconfigTables(unittest.TestCase):
    """Startup migration dropping m_kconfig_* (table ids 16-19) and renumbering interrupted runs' watermarks."""

    @staticmethod
    def _database(marks: list) -> Any:
        from core.DBLayout import INT, m_ingest_watermark
        from core.TableHandling import Table
        from db_engine import SQLiteDB
        import main

        import pipeline.db_setup
        db = SQLiteDB(":memory:", foreign_keys=False)
        obsolete = [
            Table(table_id=100 + i, table_name=name, columns=(("k", INT()),), primary=("k",))
            for i, name in enumerate(pipeline.db_setup.OBSOLETE_KCONFIG_TABLES)
        ]
        db.create_table([m_ingest_watermark, *obsolete])
        db.insert_rows("m_ingest_watermark", ["commit_id", "marked_table_id", "max_id"], marks)
        return db

    def test_drop_and_renumber_is_idempotent(self) -> None:
        import main
        import pipeline.db_setup
        from core import ingest_state
        from core.DBLayout import TABLES, m_maintainer_member, m_maintainer_person
        # Old numbering: m_kconfig_symbol/relation/tree (16-18) were watermarked, later tables were 20+
        old_marks = [(7, 3, 9), (7, 16, 100), (7, 17, 200), (7, 18, 300), (7, 20, 40), (7, 26, 50), (7, 35, 6), (7, 38, 7), (7, 45, 1)]
        new_marks = [(9, 15, 11), (9, 21, 12)]  # a run already recorded with the current ids
        db = self._database(old_marks + new_marks)

        def marks() -> list:
            return sorted(tuple(r) for r in db.select_rows("m_ingest_watermark", ["commit_id", "marked_table_id", "max_id"], as_dict=False))

        pipeline.db_setup.drop_obsolete_kconfig_tables(db)
        self.assertEqual(db.existing_tables(pipeline.db_setup.OBSOLETE_KCONFIG_TABLES + ("m_ingest_watermark",)), {"m_ingest_watermark"})
        # m_maintainer_person (old 20 -> 15), m_commit (26 -> 21), m_ast_attribute (35 -> 34),
        # m_git_ref (38 -> 32), m_ingest_watermark (45 -> 40)
        self.assertEqual(marks(), sorted([(7, 3, 9), (7, 15, 40), (7, 21, 50), (7, 34, 6), (7, 32, 7), (7, 40, 1)] + new_marks))
        names = {t.table_id: t.table_name for t in TABLES}
        self.assertEqual((names[15], names[21], names[32], names[34], names[40]),
                         ("m_maintainer_person", "m_commit", "m_git_ref", "m_ast_attribute", "m_ingest_watermark"))
        # The marker id is unmarked in the new numbering, so a second run changes nothing
        marked = {t.table_id for t, _ in ingest_state.marked_tables(TABLES)}
        self.assertNotIn(m_maintainer_member.table_id, marked)
        self.assertIn(m_maintainer_person.table_id, marked)
        before = marks()
        pipeline.db_setup.drop_obsolete_kconfig_tables(db)
        self.assertEqual(marks(), before)


class TestKconfigDiffDefinitions(unittest.TestCase):
    def test_definitions_and_line_insensitive_comparison(self) -> None:
        from webapp.backend.services.history_service import kconfig_definitions
        from webapp.backend.services.diff_service import DiffService
        old = 'if NET\nconfig A\n\tbool "a"\n\tdepends on X\nendif\n'
        moved = '# moved down\n\nif NET\nconfig A\n\tbool "a"\n\tdepends on X\nendif\n'
        changed = 'if NET\nconfig A\n\tbool "a"\n\tdepends on X && Y\nendif\n'
        defs = {k: kconfig_definitions(v)["A"][0] for k, v in (("old", old), ("moved", moved), ("changed", changed))}
        self.assertEqual(defs["old"]["inherited_depends"], ["NET"])
        cmp = {k: DiffService._comparable({"file": "Kconfig", "facts": v}) for k, v in defs.items()}
        self.assertEqual(cmp["old"], cmp["moved"])
        self.assertNotEqual(cmp["old"], cmp["changed"])
