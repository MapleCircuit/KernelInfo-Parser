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
    get_developers,
    browse_path,
    get_file_by_id,
)


class TestWebAppMaintainerEndpoints(unittest.TestCase):
    """Test web application backend endpoints for Maintainer and Credits subsystems."""

    def test_developers_roster_and_filtering(self) -> None:
        """Verify get_developers roster, role filtering, and sorting mechanics."""
        # 1. Total developers count
        res = get_developers("v3.0")
        self.assertIn("developers", res)
        self.assertGreaterEqual(res["total_count"], 1000)
        self.assertGreaterEqual(len(res["developers"]), 1000)

        # 2. Activity sorting check (most active maintainers first)
        devs_activity = res["developers"]
        self.assertGreater(devs_activity[0]["subsystems_count"], 5)
        self.assertGreaterEqual(devs_activity[0]["subsystems_count"], devs_activity[-1]["subsystems_count"])

        # 3. Alphabetical sorting check
        res_alpha = get_developers("v3.0", sort="alpha")
        self.assertEqual(res_alpha["sort"], "alpha")
        names = [d["name"] for d in res_alpha["developers"] if d["name"]]
        self.assertEqual(names, sorted(names))

        # 4. Search query filter
        res_linus = get_developers("v3.0", q="Torvalds")
        self.assertGreaterEqual(res_linus["total_count"], 1)
        linus = next((d for d in res_linus["developers"] if "Torvalds" in d["name"]), None)
        self.assertIsNotNone(linus)
        self.assertIn("Linus", linus["name"])

        # 5. Role filter: maintainer
        res_m = get_developers("v3.0", role="maintainer")
        self.assertGreaterEqual(res_m["total_count"], 500)
        for d in res_m["developers"][:20]:
            self.assertTrue(d["is_maintainer"])
            self.assertEqual(d["primary_role"], "Maintainer")

        # 6. Role filter: credits
        res_c = get_developers("v3.0", role="credits")
        self.assertGreaterEqual(res_c["total_count"], 400)
        for d in res_c["developers"][:20]:
            self.assertTrue(d["in_credits"])

        # 7. Frontend component and CSS checks
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        m_view_path = os.path.join(base_dir, "webapp", "frontend", "js", "views", "maintainers", "maintainers_view.js")
        with open(m_view_path, "r", encoding="utf-8") as f:
            mv_content = f.read()

        self.assertIn('data-tab="developers">Developers</div>', mv_content)
        self.assertIn('id="m-filter-toolbar"', mv_content)
        self.assertIn('data-role="maintainer"', mv_content)
        self.assertIn('id="btn-dev-sort"', mv_content)
        self.assertIn('renderDevelopersList', mv_content)
        self.assertIn('btn-view-person-commits', mv_content)

        css_path = os.path.join(base_dir, "webapp", "frontend", "css", "maintainers.css")
        with open(css_path, "r", encoding="utf-8") as f:
            css_content = f.read()

        self.assertIn('.m-filter-toolbar', css_content)
        self.assertIn('.m-role-pill', css_content)
        self.assertIn('.btn-dev-sort', css_content)
        self.assertIn('.m-role-badge-m', css_content)
        self.assertIn('.m-role-badge-r', css_content)
        self.assertIn('.m-role-badge-c', css_content)

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
        if sec.get("file_count", 0) == 0:
            raise unittest.SkipTest("m_maintainer_file table is unpopulated for v3.0 in active database.")
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

    def test_browse_file_lifecycle_metadata(self) -> None:
        # Browse ext4 super.c and verify lifecycle metadata
        res = browse_path("v3.0", "fs/ext4/super.c")
        self.assertEqual(res.get("type"), "file")
        self.assertIn("file_info", res)
        fi = res["file_info"]
        self.assertEqual(fi["fname"], "fs/ext4/super.c")
        self.assertEqual(fi["vname_s"], "v3.0")
        self.assertEqual(fi["added_version"], "v3.0")
        self.assertIn("s_stat_label", fi)
        self.assertIn("e_stat_label", fi)
        self.assertIn(fi["s_stat_label"], ("Added", "Modified", "Renamed"))
        self.assertIn(fi["e_stat_label"], ("Active", "Modified", "Deleted", "Renamed"))
        self.assertIn("history", fi)
        self.assertIsInstance(fi["history"], list)
        self.assertGreaterEqual(len(fi["history"]), 1)
        self.assertEqual(fi["history"][0]["vname_s"], "v3.0")

        # Test get_file_by_id endpoint
        fid = fi["fid"]
        f_res = get_file_by_id(fid)
        self.assertEqual(f_res.get("type"), "file")
        self.assertIn("file_info", f_res)
        self.assertEqual(f_res["file_info"]["fid"], fid)
        self.assertEqual(f_res["file_info"]["added_version"], "v3.0")
        self.assertEqual(f_res["file_info"]["s_stat_label"], fi["s_stat_label"])


if __name__ == "__main__":
    unittest.main()
