"""tests/test_webapp_defconfig.py - Unit tests for WebApp Defconfig retrieval, architecture scoping, and 64BIT resolution."""
from __future__ import annotations
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.main import (
    get_kconfig_defconfigs,
    get_kconfig_defconfig_content,
    get_kconfig_tree,
    get_kconfig_symbol_detail,
)


class TestWebappDefconfig(unittest.TestCase):
    """Test defconfig listing, content retrieval, architecture-scoped dependencies, and symbol lifecycle."""

    def test_get_kconfig_defconfigs_x86(self) -> None:
        """Test retrieving x86 architecture defconfigs."""
        res = get_kconfig_defconfigs("v3.0", "x86")
        self.assertIn("defconfigs", res)
        self.assertIn("canonical_default", res)
        self.assertGreaterEqual(res["total_count"], 1)

        canonical = res["canonical_default"]
        self.assertIsNotNone(canonical)
        self.assertEqual(canonical["name"], "x86_64_defconfig")
        self.assertTrue(canonical["is_canonical"])

    def test_get_kconfig_defconfig_content_full_path(self) -> None:
        """Test parsing defconfig using full relative file path."""
        res = get_kconfig_defconfig_content("v3.0", "arch/x86/configs/x86_64_defconfig", "x86")
        self.assertEqual(res["name"], "x86_64_defconfig")
        self.assertEqual(res["file_path"], "arch/x86/configs/x86_64_defconfig")
        self.assertEqual(res["bits"], 64)
        self.assertGreater(res["symbol_count"], 50)
        self.assertIn("values", res)
        self.assertEqual(res["values"].get("64BIT"), "y")
        self.assertEqual(res["values"].get("EXPERIMENTAL"), "y")

    def test_get_kconfig_defconfig_content_short_name(self) -> None:
        """Test parsing defconfig using short base name."""
        res = get_kconfig_defconfig_content("v3.0", "x86_64_defconfig", "x86")
        self.assertEqual(res["name"], "x86_64_defconfig")
        self.assertEqual(res["file_path"], "arch/x86/configs/x86_64_defconfig")
        self.assertEqual(res["bits"], 64)
        self.assertGreater(res["symbol_count"], 50)
        self.assertEqual(res["values"].get("64BIT"), "y")

    def test_get_kconfig_defconfig_content_i386(self) -> None:
        """Test parsing 32-bit i386 defconfig."""
        res = get_kconfig_defconfig_content("v3.0", "i386_defconfig", "x86")
        self.assertEqual(res["name"], "i386_defconfig")
        self.assertEqual(res["file_path"], "arch/x86/configs/i386_defconfig")
        self.assertEqual(res["bits"], 32)
        self.assertGreater(res["symbol_count"], 50)
        self.assertIn("values", res)

    def test_get_kconfig_defconfigs_arm(self) -> None:
        """Test retrieving ARM architecture defconfigs."""
        res = get_kconfig_defconfigs("v3.0", "arm")
        self.assertIn("defconfigs", res)
        self.assertGreaterEqual(res["total_count"], 1)

    def test_x86_tree_scoped_arch_dependencies(self) -> None:
        """Verify that x86 menu tree only retains x86 architecture dependencies."""
        tree = get_kconfig_tree("v3.0", "x86")
        self.assertIn("nodes", tree)

        sym_64bit_node = None
        for n in tree["nodes"]:
            if n.get("symbol_name") == "64BIT":
                sym_64bit_node = n
                break

        if sym_64bit_node:
            deps = sym_64bit_node.get("depends_on", [])
            self.assertNotIn("PA8X00", deps)
            self.assertNotIn("TILEGX", deps)

    def test_kconfig_symbol_lifecycle_metadata(self) -> None:
        """Verify Kconfig symbol lifecycle tracking fields (vid_s, vid_e, added_version, lifecycle_status)."""
        sym = get_kconfig_symbol_detail("v3.0", "EXT4_FS")
        self.assertEqual(sym["name"], "EXT4_FS")
        self.assertEqual(sym["vname_s"], "v3.0")
        self.assertEqual(sym["added_version"], "v3.0")
        self.assertTrue(sym["is_active"])
        self.assertEqual(sym["lifecycle_status"], "Active")


if __name__ == "__main__":
    unittest.main()
