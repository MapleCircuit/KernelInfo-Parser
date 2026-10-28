"""tests/test_webapp_mail.py - Unit & Integration Tests for WebApp Mailing List Subsystem.

Tests:
1. MailingListService lifecycle, status reporting, and fallback
2. Email message fetching, patch diff parsing, and LRU caching
3. Thread retrieval by TID and MID hint resolution
4. Search querying, pagination, and filter types
5. Commit-to-email linkage in get_commit_detail
6. On-demand live commit mail resolution (resolve_commit_mail) with read-only DB safety
7. FastAPI router endpoint mechanics
8. Frontend integration sanity checks (db.js, api.js, mail_view.js, mail.css, index.html)
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from webapp.backend.services.mailing_list_service import MailingListService, LORE_BASE_URL
from webapp.backend.routers.mail import (
    get_mail_status,
    get_mail_message,
    get_mail_thread,
    search_mailing_list,
)
from webapp.backend.routers.commits import resolve_commit_mail as router_resolve_commit_mail
from webapp.backend.services.git_service import GitService
from parser.mailing_list.ml_patch_parser import parse_patch_from_body
from parser.mailing_list.ml_types import (
    ResolvedCommitContext,
    SmsgRecord,
    MLMatchTier,
    MLRelationType,
)


class TestMailingListService(unittest.TestCase):
    """Test unit behavior, caching, and fallback logic of MailingListService."""

    def setUp(self) -> None:
        self.service = MailingListService(max_email_cache=5, max_thread_cache=5, max_search_cache=5)

    def test_status_when_subsystem_disabled(self) -> None:
        with patch("webapp.backend.services.mailing_list_service.get_mailing_list_config", return_value={"enabled": False}):
            status = self.service.get_status()
            self.assertFalse(status["enabled"])
            self.assertFalse(status["available"])
            self.assertEqual(status["lore_url"], LORE_BASE_URL)

    def test_status_when_daemon_online(self) -> None:
        mock_client = MagicMock()
        mock_client._in_process_resolver = None
        mock_client._check_health.return_value = True
        mock_client.use_tunnel = True
        mock_client._effective_port = 8089

        with patch("webapp.backend.services.mailing_list_service.get_mailing_list_config", return_value={"enabled": True}):
            self.service._client = mock_client
            status = self.service.get_status()
            self.assertTrue(status["enabled"])
            self.assertTrue(status["available"])
            self.assertTrue(status["use_tunnel"])
            self.assertEqual(status["port"], 8089)
            self.assertIsNone(status["reason"])

    def test_status_when_daemon_offline(self) -> None:
        mock_client = MagicMock()
        mock_client._in_process_resolver = None
        mock_client._check_health.return_value = False
        mock_client.use_tunnel = False
        mock_client._effective_port = 8088

        with patch("webapp.backend.services.mailing_list_service.get_mailing_list_config", return_value={"enabled": True}):
            self.service._client = mock_client
            status = self.service.get_status()
            self.assertTrue(status["enabled"])
            self.assertFalse(status["available"])
            self.assertIn("not responding", status["reason"])

    def test_get_message_success_and_caching(self) -> None:
        mock_client = MagicMock()
        fake_email = {
            "num": 42,
            "message_id": "test-msg-1@kernel.org",
            "subject": "[PATCH] net: fix socket leak",
            "from_name": "Alice Dev",
            "from_email": "alice@kernel.org",
            "date": 1600000000,
            "tid": 1001,
            "raw_body": "This fixes a leak.\n\nSigned-off-by: Alice <alice@kernel.org>\n",
            "is_patch": False,
        }
        mock_client.get_email_detail.return_value = fake_email
        self.service._client = mock_client

        # First retrieval - fetched from client
        msg = self.service.get_message("test-msg-1@kernel.org")
        self.assertTrue(msg["available"])
        self.assertEqual(msg["subject"], "[PATCH] net: fix socket leak")
        self.assertIn("lore_url", msg)
        self.assertEqual(mock_client.get_email_detail.call_count, 1)

        # Second retrieval - fetched from in-memory LRU cache
        cached_msg = self.service.get_message("test-msg-1@kernel.org")
        self.assertEqual(cached_msg["subject"], msg["subject"])
        self.assertEqual(mock_client.get_email_detail.call_count, 1)  # No extra client call

    def test_get_message_patch_diff_parsing(self) -> None:
        sample_patch = (
            "From: Alice <alice@kernel.org>\n"
            "Subject: [PATCH] drivers/net: fix null ptr\n\n"
            "Commit message.\n\n"
            "---\n"
            " drivers/net/eth.c | 2 +-\n"
            " 1 file changed, 1 insertion(+), 1 deletion(-)\n\n"
            "diff --git a/drivers/net/eth.c b/drivers/net/eth.c\n"
            "index 1111111..2222222 100644\n"
            "--- a/drivers/net/eth.c\n"
            "+++ b/drivers/net/eth.c\n"
            "@@ -10,3 +10,3 @@\n"
            "-    int a = 0;\n"
            "+    int a = 1;\n"
        )
        parsed = parse_patch_from_body(sample_patch)
        self.assertTrue(parsed["is_patch"])
        self.assertEqual(parsed["diff"]["additions"], 1)
        self.assertEqual(parsed["diff"]["deletions"], 1)
        self.assertEqual(len(parsed["diff"]["files"]), 1)
        self.assertEqual(parsed["diff"]["files"][0]["path"], "drivers/net/eth.c")

        mock_client = MagicMock()
        mock_client.get_email_detail.return_value = {
            "num": 43,
            "message_id": "patch-1@kernel.org",
            "subject": "[PATCH] drivers/net: fix null ptr",
            "raw_body": sample_patch,
            **parsed,
        }
        self.service._client = mock_client
        msg = self.service.get_message("patch-1@kernel.org")
        self.assertTrue(msg["available"])
        self.assertTrue(msg["is_patch"])
        self.assertIn("diff", msg)
        self.assertEqual(len(msg["diff"]["files"]), 1)

    def test_get_message_not_found(self) -> None:
        mock_client = MagicMock()
        mock_client.get_email_detail.return_value = None
        self.service._client = mock_client

        msg = self.service.get_message("non-existent@kernel.org")
        self.assertFalse(msg["available"])
        self.assertIn("not found", msg["error"])
        self.assertIn("lore.kernel.org", msg["lore_url"])

    def test_get_thread_by_tid(self) -> None:
        mock_client = MagicMock()
        mock_client.get_thread.return_value = {
            "tid": 2002,
            "total": 2,
            "messages": [
                {"num": 1, "message_id": "m1@k.org", "subject": "[PATCH 0/1] Cover", "date": 100},
                {"num": 2, "message_id": "m2@k.org", "subject": "[PATCH 1/1] Code", "date": 105},
            ],
        }
        self.service._client = mock_client

        thread = self.service.get_thread(2002)
        self.assertTrue(thread["available"])
        self.assertEqual(thread["total"], 2)
        self.assertEqual(len(thread["messages"]), 2)
        self.assertEqual(mock_client.get_thread.call_count, 1)

        # LRU cache hit
        thread_cached = self.service.get_thread(2002)
        self.assertEqual(thread_cached["total"], 2)
        self.assertEqual(mock_client.get_thread.call_count, 1)

    def test_get_thread_by_mid_hint_resolution(self) -> None:
        mock_client = MagicMock()
        mock_client.get_email_detail.return_value = {
            "num": 10,
            "message_id": "hint@k.org",
            "tid": 3003,
            "subject": "Some Patch",
        }
        mock_client.get_thread.return_value = {
            "tid": 3003,
            "total": 1,
            "messages": [{"num": 10, "message_id": "hint@k.org", "subject": "Some Patch"}],
        }
        self.service._client = mock_client

        # tid=0, should resolve tid via get_message("hint@k.org") first
        thread = self.service.get_thread(0, mid_hint="hint@k.org")
        self.assertTrue(thread["available"])
        self.assertEqual(thread["tid"], 3003)
        self.assertEqual(thread["total"], 1)

    def test_search_pagination_and_cache(self) -> None:
        mock_client = MagicMock()
        mock_client.search_emails.return_value = {
            "total": 12,
            "results": [
                {"num": 1, "message_id": "s1@k.org", "subject": "Fix 1", "is_patch": True},
                {"num": 2, "message_id": "s2@k.org", "subject": "Fix 2", "is_patch": False},
            ],
        }
        self.service._client = mock_client

        res = self.service.search(query="fix", page=1, limit=10, filter_type="patch")
        self.assertTrue(res["available"])
        self.assertEqual(res["total"], 12)
        self.assertEqual(len(res["results"]), 2)
        self.assertEqual(mock_client.search_emails.call_count, 1)

        # Cache hit
        res_cached = self.service.search(query="fix", page=1, limit=10, filter_type="patch")
        self.assertEqual(res_cached["total"], 12)
        self.assertEqual(mock_client.search_emails.call_count, 1)


class TestMailRouterEndpoints(unittest.TestCase):
    """Test FastAPI router functions for mail."""

    @patch("webapp.backend.routers.mail.mailing_list_service")
    def test_router_functions(self, mock_service: MagicMock) -> None:
        mock_service.get_status.return_value = {"enabled": True, "available": True}
        mock_service.get_message.return_value = {"available": True, "message_id": "m1@k.org"}
        mock_service.get_thread.return_value = {"available": True, "tid": 100, "messages": []}
        mock_service.search.return_value = {"available": True, "total": 0, "results": []}

        # 1. Status
        st = get_mail_status()
        self.assertTrue(st["available"])

        # 2. Message
        msg = get_mail_message(mid="m1@k.org")
        self.assertEqual(msg["message_id"], "m1@k.org")

        # 3. Thread
        th = get_mail_thread(tid=100, mid="")
        self.assertEqual(th["tid"], 100)

        # 4. Search
        s = search_mailing_list(q="ext4", page=1, limit=50, filter_type="patch")
        self.assertEqual(s["total"], 0)


class TestCommitMailIntegration(unittest.TestCase):
    """Test commit detail mail linkage and on-demand live resolution."""

    def test_get_commit_detail_includes_linked_emails(self) -> None:
        git_svc = GitService()

        mock_cursor = MagicMock()
        commit_row = {
            "commit_id": 5001,
            "commit_hash": b"\xab\xcd\xef\x12\x34\x56\x78\x90\xab\xcd\xef\x12\x34\x56\x78\x90\xab\xcd\xef\x12",
            "subject": "btrfs: optimize block group allocation",
            "message": "Optimize allocation.\n",
            "author_id": 1,
            "author_date": 1600000000,
            "committer_id": 2,
            "committer_date": 1600001000,
            "parent_count": 1,
            "files_changed": 1,
            "lines_added": 10,
            "lines_deleted": 2,
        }
        mock_email_row = {
            "message_id": "patch-123@vger.kernel.org",
            "thread_id": 7777,
            "relation_type": 1,
            "match_tier": 2,
            "patch_version": 2,
            "patch_idx": 1,
            "patch_total": 1,
        }

        with patch("webapp.backend.services.git_service.get_db_cursor") as mock_get_cursor:
            mock_ctx = MagicMock()
            mock_ctx.__enter__.return_value = mock_cursor
            mock_get_cursor.return_value = mock_ctx

            mock_cursor.fetchone.side_effect = [
                commit_row,                                                    # commit detail
                {"name": b"Dev", "email": b"dev@btrfs.org", "person_id": 1},   # author
                {"name": b"Maint", "email": b"maint@btrfs.org", "person_id": 2}, # committer
            ]
            mock_cursor.fetchall.side_effect = [
                [],                  # trailers
                [],                  # files
                [mock_email_row],    # m_commit_email
            ]

            detail = git_svc.get_commit_detail("v3.0", "5001")
            self.assertEqual(detail["commit_id"], 5001)
            self.assertIn("linked_emails", detail)
            self.assertEqual(len(detail["linked_emails"]), 1)
            linked = detail["linked_emails"][0]
            self.assertEqual(linked["message_id"], "patch-123@vger.kernel.org")
            self.assertEqual(linked["thread_id"], 7777)
            self.assertEqual(linked["relation_type"], 1)
            self.assertIn("lore.kernel.org", linked["lore_url"])

    def test_on_demand_resolve_commit_mail_read_only(self) -> None:
        git_svc = GitService()

        c_hash = "deadbeef1234567890abcdef1234567890abcdef"
        mock_detail = {
            "commit_id": 5002,
            "commit_hash": c_hash,
            "subject": "ext4: fix use-after-free in ext4_ext_remove_space",
            "author_name": "Bob Dev",
            "author_email": "bob@kernel.org",
            "author_date": 1600000000,
            "message": "Fix UAF.\n",
            "linked_emails": [],
        }

        mock_smsg = SmsgRecord(
            num=101,
            mids=["bob-ext4-fix@vger.kernel.org"],
            tid=8888,
            ts=1600000000,
            author="Bob Dev <bob@kernel.org>",
            subject="[PATCH v2] ext4: fix use-after-free in ext4_ext_remove_space",
            bytes=1200,
            lines=40,
            git_blob="abc1234",
        )
        mock_resolved_ctx = ResolvedCommitContext(
            commit_id=5002,
            commit_hash=c_hash,
            patch_email=mock_smsg,
            cover_letter=None,
            matched_tier=MLMatchTier.MetadataFallback,
            patch_version=2,
            patch_idx=1,
            patch_total=1,
        )

        with patch.object(git_svc, "get_commit_detail", return_value=mock_detail), \
             patch("parser.mailing_list.ml_client.MailingListClient") as mock_client_cls, \
             patch("webapp.backend.services.git_service.get_db_cursor") as mock_get_cursor:

            mock_cursor = MagicMock()
            mock_ctx = MagicMock()
            mock_ctx.__enter__.return_value = mock_cursor
            mock_get_cursor.return_value = mock_ctx

            mock_client_inst = MagicMock()
            mock_client_inst.resolve_commits.return_value = {c_hash: mock_resolved_ctx}
            mock_client_cls.return_value = mock_client_inst

            result = git_svc.resolve_commit_mail("v3.0", "5002")
            self.assertTrue(result["resolved"])
            self.assertEqual(len(result["linked_emails"]), 1)
            match = result["linked_emails"][0]
            self.assertEqual(match["message_id"], "bob-ext4-fix@vger.kernel.org")
            self.assertEqual(match["relation_type"], int(MLRelationType.Patch))
            self.assertEqual(match["match_tier"], int(MLMatchTier.MetadataFallback))

            # CRITICAL READ-ONLY INVARIANT: Verify no INSERT, UPDATE, or DELETE executed on DB!
            for call in mock_cursor.execute.call_args_list:
                sql = call[0][0].strip().upper()
                self.assertFalse(sql.startswith("INSERT"), f"Violation: execute called with {sql}")
                self.assertFalse(sql.startswith("UPDATE"), f"Violation: execute called with {sql}")
                self.assertFalse(sql.startswith("DELETE"), f"Violation: execute called with {sql}")


class TestFrontendMailAssets(unittest.TestCase):
    """Sanity checks for frontend assets and wire-ins."""

    def test_mail_view_js_exists_and_contains_core_logic(self) -> None:
        view_path = os.path.join(REPO_ROOT, "webapp", "frontend", "js", "views", "mail", "mail_view.js")
        self.assertTrue(os.path.exists(view_path), f"File {view_path} does not exist")
        with open(view_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("class MailView", content)
        self.assertIn("api.getMailThread", content)
        self.assertIn("renderThreadView", content)
        self.assertIn("renderPatchDiff", content)
        self.assertIn("wireDiffInteractions", content)
        self.assertIn("formatEmailBodyText", content)
        self.assertIn("copyToClipboard", content)

    def test_mail_css_exists_and_contains_styles(self) -> None:
        css_path = os.path.join(REPO_ROOT, "webapp", "frontend", "css", "mail.css")
        self.assertTrue(os.path.exists(css_path), f"File {css_path} does not exist")
        with open(css_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn(".mail-view-container", content)
        self.assertIn(".mail-list-col", content)
        self.assertIn(".mail-thread-outline", content)
        self.assertIn(".mail-card", content)
        self.assertIn(".mail-raw-container", content)

    def test_db_js_includes_mail_store(self) -> None:
        db_path = os.path.join(REPO_ROOT, "webapp", "frontend", "js", "db.js")
        with open(db_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn('"mail"', content)
        self.assertIn("DB_VERSION = 4", content)

    def test_api_js_includes_mail_methods(self) -> None:
        api_path = os.path.join(REPO_ROOT, "webapp", "frontend", "js", "api.js")
        with open(api_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("getMailStatus(", content)
        self.assertIn("getMailMessage(", content)
        self.assertIn("getMailThread(", content)
        self.assertIn("searchMailingList(", content)
        self.assertIn("resolveCommitMail(", content)

    def test_index_html_includes_mail_tab_and_css(self) -> None:
        index_path = os.path.join(REPO_ROOT, "webapp", "frontend", "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn('href="/static/css/mail.css"', content)
        self.assertIn('data-action="mail"', content)
        self.assertIn("Mailing List &amp; LKML Threads", content)


if __name__ == "__main__":
    unittest.main()
