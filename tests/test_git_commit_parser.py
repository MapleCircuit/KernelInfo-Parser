"""tests/test_git_commit_parser.py - Unit & Integration Test Suite for Git Commit Parser & WebApp Git API.

Validates git log parsing, trailer extraction (Co-developed-by, Signed-off-by, etc.),
multi-contributor bridges, tag-to-commit mapping, and FastAPI Git endpoints.
"""
from __future__ import annotations
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser.git_ast.git_types import CommitRole, GitContributor, GitCommit, CommitDiffHunk
from parser.git_ast.git_commit_parser import GitCommitParser
from webapp.main import (
    get_person_profile,
    get_version_commits,
    get_commit_detail,
    get_file_blame,
    get_commit_timeline,
)


SAMPLE_GIT_LOG = """COMMIT_DELIM_START_7f9a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a
HASH:7f9a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a
ANAME:Linus Torvalds
AEMAIL:torvalds@linux-foundation.org
ADATE:1311280000
CNAME:Linus Torvalds
CEMAIL:torvalds@linux-foundation.org
CDATE:1311280000
SUBJ:Linux 3.0 Release
BODY_START
Linux 3.0 final release!

Co-developed-by: Greg Kroah-Hartman <gregkh@linuxfoundation.org>
Signed-off-by: Greg Kroah-Hartman <gregkh@linuxfoundation.org>
Reviewed-by: Theodore Ts'o <tytso@mit.edu>
Acked-by: Ingo Molnar <mingo@kernel.org>
Signed-off-by: Linus Torvalds <torvalds@linux-foundation.org>
BODY_END
M	Makefile
M	include/linux/version.h
COMMIT_DELIM_END

COMMIT_DELIM_START_1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b
HASH:1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b
ANAME:Theodore Ts'o
AEMAIL:tytso@mit.edu
ADATE:1311270000
CNAME:Theodore Ts'o
CEMAIL:tytso@mit.edu
CDATE:1311270000
SUBJ:ext4: fix metadata allocation race
BODY_START
Fix a race condition in ext4 metadata allocation.

Signed-off-by: Theodore Ts'o <tytso@mit.edu>
BODY_END
M	fs/ext4/mballoc.c
COMMIT_DELIM_END
"""


class TestGitCommitParser(unittest.TestCase):
    """Test suite for Git Commit AST Parser and Trailer Extractor."""

    def test_parse_commit_log_basic(self) -> None:
        commits = GitCommitParser.parse_commit_log(SAMPLE_GIT_LOG)
        self.assertEqual(len(commits), 2)

        c1 = commits[0]
        self.assertEqual(c1.commit_hash, "7f9a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a")
        self.assertEqual(c1.author_name, "Linus Torvalds")
        self.assertEqual(c1.author_email, "torvalds@linux-foundation.org")
        self.assertEqual(c1.author_date, 1311280000)
        self.assertEqual(c1.subject, "Linux 3.0 Release")
        self.assertEqual(c1.files, [("M", "Makefile"), ("M", "include/linux/version.h")])

        # Test contributors & trailers
        linus_roles = [cb.role for cb in c1.contributors if cb.email == "torvalds@linux-foundation.org"]
        self.assertIn(CommitRole.AUTHOR, linus_roles)
        self.assertIn(CommitRole.SIGNED_OFF_BY, linus_roles)

        greg_roles = [cb.role for cb in c1.contributors if cb.email == "gregkh@linuxfoundation.org"]
        self.assertIn(CommitRole.CO_DEVELOPED_BY, greg_roles)

        tytso_roles = [cb.role for cb in c1.contributors if cb.email == "tytso@mit.edu"]
        self.assertIn(CommitRole.REVIEWED_BY, tytso_roles)

        mingo_roles = [cb.role for cb in c1.contributors if cb.email == "mingo@kernel.org"]
        self.assertIn(CommitRole.ACKED_BY, mingo_roles)

    def test_parse_commit_log_single_author(self) -> None:
        commits = GitCommitParser.parse_commit_log(SAMPLE_GIT_LOG)
        c2 = commits[1]
        self.assertEqual(c2.commit_hash, "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b")
        self.assertEqual(c2.author_name, "Theodore Ts'o")
        self.assertEqual(c2.subject, "ext4: fix metadata allocation race")
        # Author + Signed-off-by
        self.assertEqual(len(c2.contributors), 2)
        roles = [cb.role for cb in c2.contributors]
        self.assertIn(CommitRole.AUTHOR, roles)
        self.assertIn(CommitRole.SIGNED_OFF_BY, roles)

    def test_diff_hunk_extraction(self) -> None:
        patch_text = """diff --git a/fs/ext4/super.c b/fs/ext4/super.c
index 1111111..2222222 100644
--- a/fs/ext4/super.c
+++ b/fs/ext4/super.c
@@ -100,5 +100,8 @@ int ext4_init(void) {
+    // new line 1
+    // new line 2
+    // new line 3
 }
@@ -250,10 +253,12 @@ void ext4_exit(void) {
+    ext4_cleanup();
 }
"""
        hunks = GitCommitParser.extract_diff_hunks_from_patch(patch_text, "fs/ext4/super.c")
        self.assertEqual(len(hunks), 2)
        self.assertEqual(hunks[0].new_start, 100)
        self.assertEqual(hunks[0].new_count, 8)
        self.assertEqual(hunks[1].new_start, 253)
        self.assertEqual(hunks[1].new_count, 12)

    def test_tag_to_commit_mapping_multi_commit(self) -> None:
        # Define 3 tags
        tags = [
            {"tag_id": 1, "line_s": 102, "line_e": 105},   # Intersects Commit 1 & 2
            {"tag_id": 2, "line_s": 255, "line_e": 260},   # Intersects Commit 1 only
            {"tag_id": 3, "line_s": 500, "line_e": 510},   # Intersects Commit 3 only
        ]

        hunks_by_commit = {
            101: [CommitDiffHunk("hash101", "fs/ext4/super.c", 100, 10, 100, 15), CommitDiffHunk("hash101", "fs/ext4/super.c", 250, 10, 253, 12)],
            102: [CommitDiffHunk("hash102", "fs/ext4/super.c", 101, 5, 101, 8)],
            103: [CommitDiffHunk("hash103", "fs/ext4/super.c", 490, 20, 490, 25)],
        }

        parser = GitCommitParser()
        mapping = parser.map_tags_to_commits(tags, hunks_by_commit=hunks_by_commit)

        # Tag 1 should be associated with both Commit 101 and Commit 102 (Multi-commit tag!)
        self.assertIn(1, mapping)
        self.assertIn(101, mapping[1])
        self.assertIn(102, mapping[1])

        # Tag 2 should be associated with Commit 101
        self.assertIn(2, mapping)
        self.assertEqual(mapping[2], [101])

        # Tag 3 should be associated with Commit 103
        self.assertIn(3, mapping)
        self.assertEqual(mapping[3], [103])

    def test_parse_commit_log_with_hunks(self) -> None:
        patch_log = """COMMIT_DELIM_START_abc1234567890
HASH:abc1234567890
ANAME:Developer A
AEMAIL:dev@example.com
ADATE:1311280000
CNAME:Developer A
CEMAIL:dev@example.com
CDATE:1311280000
SUBJ:test hunk parsing
BODY_START
Test commit body with diff
BODY_END
diff --git a/drivers/watchdog/wdt.c b/drivers/watchdog/wdt.c
index 1234567..7654321 100644
--- a/drivers/watchdog/wdt.c
+++ b/drivers/watchdog/wdt.c
@@ -50,5 +50,10 @@ int init_wdt(void)
+    // modified line
COMMIT_DELIM_END
"""
        commits, file_hunks = GitCommitParser.parse_commit_log_with_hunks(patch_log)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].commit_hash, "abc1234567890")
        self.assertEqual(commits[0].author_name, "Developer A")
        self.assertIn("drivers/watchdog/wdt.c", file_hunks)
        self.assertEqual(file_hunks["drivers/watchdog/wdt.c"], [(50, 59, "abc1234567890")])

    def test_tag_to_commit_mapping_file_hunks_map(self) -> None:
        file_hunks_map = {
            "drivers/net/e1000.c": [
                (100, 120, "hash_aaa"),
                (200, 250, "hash_bbb"),
            ]
        }
        commit_hash_to_id = {
            "hash_aaa": 10,
            "hash_bbb": 20,
        }
        tags = [
            (1, 101, 105, 110),  # Intersects hash_aaa
            (2, 101, 210, 220),  # Intersects hash_bbb
            (3, 101, 300, 310),  # Outside ranges -> fallback to first
        ]
        parser = GitCommitParser()
        bridges = parser.map_tags_to_commits(
            tags,
            file_path="drivers/net/e1000.c",
            commit_hash_to_id=commit_hash_to_id,
            file_hunks_map=file_hunks_map,
        )
        self.assertEqual(len(bridges), 3)
        self.assertEqual(bridges[0], (10, 101, 1))
        self.assertEqual(bridges[1], (20, 101, 2))
        self.assertEqual(bridges[2], (10, 101, 3))


class TestWebAppGitEndpoints(unittest.TestCase):
    """Test suite for FastAPI Git Blame, Timeline, Commit, and Contributor Endpoints."""

    def test_commits_list_and_search(self) -> None:
        res = get_version_commits("v3.0", limit=10, offset=0)
        self.assertIn("commits", res)
        self.assertIn("total_count", res)
        self.assertGreaterEqual(res["total_count"], 1)
        self.assertLessEqual(len(res["commits"]), 10)

        # Verify commit structure
        c = res["commits"][0]
        self.assertIn("commit_hash", c)
        self.assertIn("subject", c)
        self.assertIn("author", c)
        self.assertIn("files_count", c)

    def test_commit_detail_lookup(self) -> None:
        commits_res = get_version_commits("v3.0", limit=1)
        self.assertGreaterEqual(len(commits_res["commits"]), 1)
        target_hash = commits_res["commits"][0]["commit_hash"]

        detail = get_commit_detail("v3.0", target_hash)
        self.assertEqual(detail["commit_hash"], target_hash)
        self.assertIn("author", detail)
        self.assertIn("committer", detail)
        self.assertIn("message", detail)
        self.assertIn("contributors", detail)
        self.assertIn("files", detail)

    def test_person_profile_git_enhancements(self) -> None:
        # Profile lookup for Linus Torvalds
        res = get_person_profile("v3.0", "torvalds@linux-foundation.org")
        self.assertIn("person", res)
        self.assertIn("latest_patch", res)
        self.assertIn("contribution_stats", res)
        self.assertIn("recent_commits", res)

        # Check latest patch
        lp = res["latest_patch"]
        if lp:
            self.assertIn("commit_hash", lp)
            self.assertIn("subject", lp)
            self.assertIn("author_date_iso", lp)

        # Check contribution stats
        stats = res["contribution_stats"]
        self.assertIn("authored_commits", stats)
        self.assertIn("co_developed_commits", stats)
        self.assertIn("signed_off_commits", stats)

    def test_commit_timeline_and_top_contributors(self) -> None:
        res = get_commit_timeline("v3.0", limit=20)
        self.assertIn("timeline", res)
        self.assertIn("total_commits", res)
        self.assertIn("top_contributors", res)
        self.assertGreaterEqual(len(res["timeline"]), 1)
        self.assertGreaterEqual(len(res["top_contributors"]), 1)

        top_contrib = res["top_contributors"][0]
        self.assertIn("name", top_contrib)
        self.assertIn("commits_count", top_contrib)

    def test_file_blame_endpoint(self) -> None:
        # Test blame for a known file in v3.0 (e.g. fid 1 or 2)
        res = get_file_blame("v3.0", 1)
        self.assertIn("fid", res)
        self.assertIn("tags", res)
        self.assertIn("total_tags", res)


if __name__ == "__main__":
    unittest.main()
