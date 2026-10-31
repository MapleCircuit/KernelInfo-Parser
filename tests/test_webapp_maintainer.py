"""tests/test_webapp_maintainer.py - Unit & Integration Test Suite for WebApp Maintainers & Credits API.

Validates the FastAPI web endpoints for the subsystems catalog, developer profiles with CREDITS
cross-referencing, and the credits overview.
"""
from __future__ import annotations
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import require_indexed_ref
from webapp.backend.database.pool import get_db_cursor
from webapp.main import (
    get_maintainers_overview,
    get_person_profile,
    get_credits_overview,
)


class TestWebAppMaintainerEndpoints(unittest.TestCase):
    """Test web application backend endpoints for Maintainer and Credits subsystems."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            with get_db_cursor() as cur:
                cur.execute("SELECT COUNT(*) as c FROM m_maintainer_section WHERE sec_id > 0;")
                row = cur.fetchone()
                count = row.get("c", 0) if isinstance(row, dict) else (row[0] if row else 0)
                if count == 0:
                    raise unittest.SkipTest("m_maintainer_section is not populated in active database.")
        except unittest.SkipTest:
            raise
        except Exception as e:
            raise unittest.SkipTest(f"Database check failed: {e}")

    def test_maintainers_overview_and_search(self) -> None:
        require_indexed_ref("v3.0")
        # Search for ext4 subsystem
        res = get_maintainers_overview("v3.0", q="ext4")
        self.assertIn("sections", res)
        self.assertGreaterEqual(res["total_count"], 1)

        ext4_sec = next((s for s in res["sections"] if "EXT4" in s["name"]), None)
        self.assertIsNotNone(ext4_sec)
        self.assertEqual(ext4_sec["name"], "EXT4 FILE SYSTEM")
        self.assertIn("maintainers", ext4_sec)
        self.assertGreaterEqual(len(ext4_sec["maintainers"]), 1)

    def test_person_profile_and_cross_referencing(self) -> None:
        require_indexed_ref("v3.0")
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


if __name__ == "__main__":
    unittest.main()
