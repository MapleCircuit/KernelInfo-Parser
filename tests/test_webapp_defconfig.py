"""tests/test_webapp_defconfig.py - Unit tests for WebApp Defconfig retrieval, architecture scoping, and 64BIT resolution."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services.kconfig_service import KconfigService

_kconfig = KconfigService()
get_kconfig_defconfigs = _kconfig.get_defconfigs
get_kconfig_defconfig_content = _kconfig.get_defconfig_content
get_kconfig_tree = _kconfig.get_tree
get_kconfig_symbol_detail = _kconfig.get_symbol_detail


class TestWebappDefconfig(unittest.TestCase):
    """Test defconfig listing, content retrieval, architecture-scoped dependencies, and symbol detail."""

    def test_get_kconfig_defconfigs_x86(self) -> None:
        """Test retrieving x86 architecture defconfigs (default = what `make ARCH=<arch> defconfig` loads)."""
        res = get_kconfig_defconfigs("v3.0", "x86_64")
        self.assertIn("defconfigs", res)
        self.assertGreaterEqual(len(res["defconfigs"]), 1)

        def canonical(items: list[dict]) -> list[str]:
            return [d["name"] for d in items if d["is_canonical"]]

        # Exactly one default
        self.assertEqual(canonical(res["defconfigs"]), ["x86_64_defconfig"])
        # arch/x86/Makefile: ARCH=x86 selects i386_defconfig
        self.assertEqual(canonical(get_kconfig_defconfigs("v3.0", "x86")["defconfigs"]), ["i386_defconfig"])

    def test_get_kconfig_defconfig_content_full_path(self) -> None:
        """Test parsing defconfig using full relative file path."""
        res = get_kconfig_defconfig_content("v3.0", "arch/x86/configs/x86_64_defconfig", "x86")
        self.assertEqual(res["name"], "x86_64_defconfig")
        self.assertEqual(res["file_path"], "arch/x86/configs/x86_64_defconfig")
        self.assertEqual(res["bits"], 64)
        self.assertGreater(len(res["values"]), 50)
        self.assertIn("values", res)
        self.assertIn("content", res)
        self.assertGreater(len(res["content"]), 0)
        self.assertEqual(res["values"].get("64BIT"), "y")
        self.assertEqual(res["values"].get("EXPERIMENTAL"), "y")

    def test_get_kconfig_defconfig_content_short_name(self) -> None:
        """Test parsing defconfig using short base name."""
        res = get_kconfig_defconfig_content("v3.0", "x86_64_defconfig", "x86")
        self.assertEqual(res["name"], "x86_64_defconfig")
        self.assertEqual(res["file_path"], "arch/x86/configs/x86_64_defconfig")
        self.assertEqual(res["bits"], 64)
        self.assertGreater(len(res["values"]), 50)
        self.assertIn("values", res)
        self.assertIn("content", res)
        self.assertGreater(len(res["content"]), 0)
        self.assertEqual(res["values"].get("64BIT"), "y")

    def test_get_kconfig_defconfig_content_i386(self) -> None:
        """Test parsing 32-bit i386 defconfig."""
        res = get_kconfig_defconfig_content("v3.0", "i386_defconfig", "x86")
        self.assertEqual(res["name"], "i386_defconfig")
        self.assertEqual(res["file_path"], "arch/x86/configs/i386_defconfig")
        self.assertEqual(res["bits"], 32)
        self.assertGreater(len(res["values"]), 50)
        self.assertIn("values", res)

    def test_get_kconfig_defconfigs_arm(self) -> None:
        """Test retrieving ARM architecture defconfigs."""
        res = get_kconfig_defconfigs("v3.0", "arm")
        self.assertIn("defconfigs", res)
        self.assertGreaterEqual(len(res["defconfigs"]), 1)

    def test_x86_tree_scoped_arch_dependencies(self) -> None:
        """Verify that the x86 model only holds the x86 architecture's symbols."""
        tree = get_kconfig_tree("v3.0", "x86")
        self.assertIn("symbols", tree)
        self.assertIn("64BIT", tree["symbols"])
        # Symbols of other architectures' Kconfig files never enter the x86 model
        self.assertNotIn("PA8X00", tree["symbols"])
        self.assertNotIn("TILEGX", tree["symbols"])

    def test_kconfig_symbol_detail(self) -> None:
        """Verify the Kconfig symbol detail the KConfig view reads (definition sites, help, Kbuild rules)."""
        try:
            sym = get_kconfig_symbol_detail("v3.0", "EXT4_FS")
        except Exception as exc:
            raise unittest.SkipTest("EXT4_FS symbol not indexed in database.") from exc
        self.assertEqual(sym["name"], "EXT4_FS")
        self.assertTrue(sym["defined_for_arch"])
        self.assertEqual(sym["declarations"][0]["file_path"], "fs/ext4/Kconfig")
        self.assertIn("help", sym)
        self.assertIn("kbuild", sym)
        self.assertIn("compiled_files", sym)
        # The per-revision model has no lifecycle: those compatibility fields are gone
        for key in ("vname_s", "added_version", "is_active", "lifecycle_status", "menu_path"):
            self.assertNotIn(key, sym)


class TestKconfigServiceAndFactsHardening(unittest.TestCase):
    """BE-013 / BE-010b regression: implied_by exposure, reverse_dependencies sha scoping, and batch draining."""

    def test_reverse_dependencies_shas_filter_and_read_blobs_drains_non_blob(self) -> None:
        import subprocess
        from unittest.mock import MagicMock, patch

        from webapp.backend.services import kconfig_facts
        from webapp.backend.services.kconfig_service import _ModelEntry

        with kconfig_facts._LOCK:
            orig_defs = dict(kconfig_facts._DEFS_CACHE)
            kconfig_facts._DEFS_CACHE.clear()
            kconfig_facts._DEFS_CACHE["sha_a"] = {"DRIVER_A": [{"selects": ["SHARED_DEP"], "implies": []}]}
            kconfig_facts._DEFS_CACHE["sha_b"] = {"DRIVER_B": [{"selects": [], "implies": ["SHARED_DEP"]}]}
        try:
            self.assertEqual(kconfig_facts.reverse_dependencies("SHARED_DEP", shas=["sha_a"]), (["DRIVER_A"], []))
            self.assertEqual(kconfig_facts.reverse_dependencies("SHARED_DEP", shas=["sha_b"]), ([], ["DRIVER_B"]))
        finally:
            with kconfig_facts._LOCK:
                kconfig_facts._DEFS_CACHE.clear()
                kconfig_facts._DEFS_CACHE.update(orig_defs)

        sha_tree = "a" * 40
        sha_missing = "b" * 40
        sha_blob = "c" * 40
        payload = (
            f"{sha_tree} tree 4\n1234\n"
            f"{sha_missing} missing\n"
            f"{sha_blob} blob 12\nconfig TEST\n\n"
        ).encode()
        proc_res = MagicMock(stdout=payload)
        with patch.object(kconfig_facts, "load_stored"), \
                patch.object(kconfig_facts, "cached", return_value=None), \
                patch.object(kconfig_facts, "get_repo_dir", return_value="/tmp"), \
                patch.object(subprocess, "run", return_value=proc_res):
            texts = kconfig_facts.read_blobs([sha_blob, sha_tree, sha_missing], skip_cached=False)
        self.assertEqual(texts, {sha_blob: "config TEST\n"})

        # _ModelEntry carries implied_by alongside selected_by
        entry = _ModelEntry(
            {"symbols": {"FOO": {"selects": [], "implies": [("BAR", -1)], "type": "bool", "defaults": [], "ranges": []}},
             "nodes": [], "files": [], "exprs": []},
            {"rev": "v3.0", "arch": "x86", "srcarch": "x86"},
            {"help": {}, "menu_help": {}, "references": {}},
        )
        self.assertEqual(entry.implied_by.get("BAR"), [("FOO", -1)])


if __name__ == "__main__":
    unittest.main()
