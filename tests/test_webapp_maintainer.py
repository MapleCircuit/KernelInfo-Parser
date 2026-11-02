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
from webapp.backend.services.maintainer_service import (
    _build_identity_index,
    _clean_name,
    _identity_key,
    MaintainerService,
)
maintainer_service = MaintainerService()
get_maintainers_overview = maintainer_service.get_overview
get_person_profile = maintainer_service.get_person_profile
get_credits_overview = maintainer_service.get_credits


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
        res = get_maintainers_overview("v3.0", "ext4")
        self.assertIn("sections", res)
        self.assertGreaterEqual(len(res["sections"]), 1)

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
        res = get_credits_overview("v3.0", "Linus")
        self.assertIn("credits", res)
        self.assertGreaterEqual(res["total"], 1)

        linus = next((c for c in res["credits"] if "Linus" in c["name"]), None)
        self.assertIsNotNone(linus)
        self.assertIn("Torvalds", linus["name"])
        self.assertIn("Original", linus["description"])

    def test_identity_index_rules(self) -> None:
        # Pure grouping rules: name folding, name-less attach to dominant owner, no email chaining
        self.assertEqual(_clean_name('  "Ben  Dooks" '), "Ben Dooks")
        self.assertEqual(_clean_name("From: Greg Kroah-Hartman"), "Greg Kroah-Hartman")
        idx = _build_identity_index([
            (1, "Marcel Holtmann", "marcel@holtmann.org"),
            (2, "Marcel Holtmann", ""),
            (3, "Marcel Holtman", "marcel@holtmann.org"),
            (4, "", "marcel@holtmann.org"),
            (5, "Jeff Layton", "shared@example.org"),
            (6, "Cyrill Gorcunov", "shared@example.org"),
        ])
        self.assertEqual(idx[1], idx[2])
        self.assertEqual(idx[4], idx[1])  # dominant owner (larger identity) wins the name-less alias
        self.assertNotEqual(idx[3], idx[1])  # named identities never merge by email
        self.assertNotEqual(idx[5], idx[6])

    def test_developers_and_credits_have_no_duplicates(self) -> None:
        devs = maintainer_service.get_developers("v3.0")["developers"]
        keys = [_identity_key(d["name"], d["email"]) for d in devs]
        self.assertEqual(len(keys), len(set(keys)), "Developers directory lists an identity twice")
        self.assertEqual(len({d["person_id"] for d in devs}), len(devs))

        credits = maintainer_service.get_credits("v3.0")["credits"]
        c_keys = [_identity_key(c["name"], c["email"]) for c in credits]
        self.assertEqual(len(c_keys), len(set(c_keys)), "CREDITS lists an identity twice")

        # A merged card and its profile agree on aliases and subsystem count
        top = devs[0]
        prof = get_person_profile("v3.0", top["person_id"])
        self.assertEqual(prof["alias_person_ids"], top["alias_person_ids"])
        self.assertEqual(prof["subsystems_count"], top["subsystems_count"])


if __name__ == "__main__":
    unittest.main()
