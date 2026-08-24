"""tests/test_webapp_maintainer.py - Unit & Integration Test Suite for WebApp Maintainers & Credits API.

Validates the FastAPI web endpoints for subsystems catalog, section detail with file resolution,
developer profiles with CREDITS cross-referencing, and file browsing enrichment.
"""
from __future__ import annotations
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.main import (
    get_maintainers_overview,
    get_maintainer_section_detail,
    get_person_profile,
    get_credits_overview,
    browse_path,
)


class TestWebAppMaintainerEndpoints(unittest.TestCase):
    """Test web application backend endpoints for Maintainer and Credits subsystems."""

    def test_maintainers_overview_and_search(self) -> None:
        # Search for ext4 subsystem
        res = get_maintainers_overview("v3.0", q="ext4")
        self.assertIn("sections", res)
        self.assertGreaterEqual(res["total_count"], 1)

        ext4_sec = next((s for s in res["sections"] if "EXT4" in s["name"]), None)
        self.assertIsNotNone(ext4_sec)
        self.assertEqual(ext4_sec["name"], "EXT4 FILE SYSTEM")
        self.assertIn("maintainers", ext4_sec)
        self.assertGreaterEqual(len(ext4_sec["maintainers"]), 1)

    def test_maintainer_section_detail_and_files(self) -> None:
        # Fetch EXT4 FILE SYSTEM section details
        res = get_maintainer_section_detail("v3.0", "EXT4 FILE SYSTEM")
        self.assertIn("section", res)
        sec = res["section"]
        self.assertEqual(sec["name"], "EXT4 FILE SYSTEM")
        self.assertIn("members", sec)
        self.assertIn("patterns", sec)
        self.assertIn("files", sec)

        # Verify members have role and in_credits flag
        tytso = next((m for m in sec["members"] if "Ts'o" in m["name"] or "tytso" in m["email"]), None)
        self.assertIsNotNone(tytso)
        self.assertEqual(tytso["role_name"], "Maintainer")
        self.assertTrue(tytso["in_credits"], "Theodore Ts'o should be marked in_credits=True")

        # Verify files matching ext4
        self.assertGreater(sec["file_count"], 0)
        file_names = [f["fname"] for f in sec["files"]]
        self.assertTrue(any("ext4" in fn for fn in file_names))

    def test_person_profile_and_cross_referencing(self) -> None:
        # Profile lookup for Theodore Ts'o
        res = get_person_profile("v3.0", "tytso@mit.edu")
        self.assertIn("person", res)
        self.assertIn("credits", res)
        self.assertIn("subsystems", res)

        self.assertEqual(res["person"]["email"], "tytso@mit.edu")
        self.assertTrue(res["in_credits"])
        self.assertIsNotNone(res["credits"])
        self.assertIn("description", res["credits"])
        self.assertGreaterEqual(res["subsystems_count"], 1)

        subsystem_names = [s["name"] for s in res["subsystems"]]
        self.assertIn("EXT4 FILE SYSTEM", subsystem_names)

        # Test URL encoded email lookups (e.g. tytso%40mit.edu and tytso%2540mit.edu)
        res_enc1 = get_person_profile("v3.0", "tytso%40mit.edu")
        self.assertEqual(res_enc1["person"]["email"], "tytso@mit.edu")

        res_enc2 = get_person_profile("v3.0", "tytso%2540mit.edu")
        self.assertEqual(res_enc2["person"]["email"], "tytso@mit.edu")

        # Test lookup for Andrew Morton (akpm%2540linux-foundation.org)
        res_akpm = get_person_profile("v3.0", "akpm%2540linux-foundation.org")
        self.assertIn("Andrew", res_akpm["person"]["name"])

    def test_credits_overview_and_keyword_search(self) -> None:
        # Search credits for Linus
        res = get_credits_overview("v3.0", q="Linus")
        self.assertIn("credits", res)
        self.assertGreaterEqual(res["total_count"], 1)

        linus = next((c for c in res["credits"] if "Linus" in c["name"]), None)
        self.assertIsNotNone(linus)
        self.assertIn("Torvalds", linus["name"])
        self.assertIn("Original", linus["description"])

    def test_browse_file_subsystems_enrichment(self) -> None:
        # Browse ext4 super.c
        res = browse_path("v3.0", "fs/ext4/super.c")
        self.assertEqual(res.get("type"), "file")
        self.assertIn("subsystems", res)
        subsystems = res["subsystems"]
        self.assertGreaterEqual(len(subsystems), 1)
        self.assertEqual(subsystems[0]["name"], "EXT4 FILE SYSTEM")
        self.assertGreaterEqual(len(subsystems[0]["maintainers"]), 1)


if __name__ == "__main__":
    unittest.main()
