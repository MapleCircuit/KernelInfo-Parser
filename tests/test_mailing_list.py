"""Unit tests for the Mailing List Commit Resolver Subsystem (parser/mailing_list)."""

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
import zlib
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path

from parser.mailing_list.ml_types import (
    CommitFeatures,
    MLMatchTier,
    MLRelationType,
    ResolvedCommitContext,
    SmsgRecord,
)
from parser.mailing_list.ml_store import PublicInboxLocalStore
from parser.mailing_list.ml_resolver import (
    KernelCommitEmailResolver,
    extract_patch_metadata,
)
from parser.mailing_list.ml_server import MLServerRequestHandler
from parser.mailing_list.ml_client import MailingListClient


class TestPatchMetadataExtraction(unittest.TestCase):
    """Test subject line parsing for patch version, sequence index, and total count."""

    def test_single_patch_default(self):
        v, idx, total = extract_patch_metadata("[PATCH] fs/ext4: fix null pointer dereference")
        self.assertEqual(v, 1)
        self.assertEqual(idx, 1)
        self.assertEqual(total, 1)

    def test_numbered_patch_series(self):
        v, idx, total = extract_patch_metadata("[PATCH v3 02/10] mm/slub: optimize kmalloc fast path")
        self.assertEqual(v, 3)
        self.assertEqual(idx, 2)
        self.assertEqual(total, 10)

    def test_cover_letter(self):
        v, idx, total = extract_patch_metadata("[PATCH v4 0/8] btrfs: improve transaction commit latency")
        self.assertEqual(v, 4)
        self.assertEqual(idx, 0)
        self.assertEqual(total, 8)

    def test_rfc_patch(self):
        v, idx, total = extract_patch_metadata("[RFC PATCH v2 1/3] sched/fair: balance wakeups")
        self.assertEqual(v, 2)
        self.assertEqual(idx, 1)
        self.assertEqual(total, 3)

    def test_no_patch_prefix(self):
        v, idx, total = extract_patch_metadata("Merge tag 'pull-request' of git://git.kernel.org")
        self.assertEqual(v, 1)
        self.assertEqual(idx, 1)
        self.assertEqual(total, 1)


class TestTypeSerialization(unittest.TestCase):
    """Test to_dict and from_dict serialization of DTO classes."""

    def test_commit_features_round_trip(self):
        feat = CommitFeatures(
            commit_id=42,
            commit_hash="0123456789abcdef0123456789abcdef01234567",
            subject="net: core: improve buffer recycling",
            author_name="Alice Dev",
            author_email="alice@example.org",
            author_date=1600000000,
            commit_message="Signed-off-by: Alice Dev <alice@example.org>\nLink: https://lore.kernel.org/r/20200913.patch1@example.org",
            patch_id="deadbeefcafebabe11223344556677889900aabb",
            blob_hashes=["feedface0123456789abcdef0123456789abcdef"],
        )
        d = feat.to_dict()
        restored = CommitFeatures.from_dict(d)
        self.assertEqual(restored.commit_id, 42)
        self.assertEqual(restored.commit_hash, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(restored.patch_id, "deadbeefcafebabe11223344556677889900aabb")
        self.assertEqual(len(restored.blob_hashes), 1)

    def test_resolved_context_round_trip(self):
        patch = SmsgRecord(
            num=101,
            ts=1600000100,
            ds=1600000100,
            blob="feedface0123456789abcdef0123456789abcdef",
            mids=["20200913.patch1@example.org"],
            tid=100,
            subj="[PATCH v2 1/2] net: core: improve buffer recycling",
            from_hdr="Alice Dev <alice@example.org>",
            bytes=2048,
            lines=50,
        )
        cover = SmsgRecord(
            num=100,
            ts=1600000000,
            ds=1600000000,
            blob="aabbccdd0123456789abcdef0123456789abcdef",
            mids=["20200913.cover@example.org"],
            tid=100,
            subj="[PATCH v2 0/2] net: core buffer series",
            from_hdr="Alice Dev <alice@example.org>",
            bytes=1500,
            lines=35,
        )
        ctx = ResolvedCommitContext(
            commit_id=42,
            commit_hash="0123456789abcdef0123456789abcdef01234567",
            patch_email=patch,
            cover_letter=cover,
            pull_request=None,
            matched_tier=MLMatchTier.Trailer,
            patch_version=2,
            patch_idx=1,
            patch_total=2,
        )
        d = ctx.to_dict()
        restored = ResolvedCommitContext.from_dict(d)
        self.assertEqual(restored.commit_id, 42)
        self.assertEqual(restored.matched_tier, MLMatchTier.Trailer)
        self.assertEqual(restored.patch_version, 2)
        self.assertIsNotNone(restored.patch_email)
        self.assertEqual(restored.patch_email.mids[0], "20200913.patch1@example.org")
        self.assertIsNotNone(restored.cover_letter)
        self.assertEqual(restored.cover_letter.mids[0], "20200913.cover@example.org")


class TestPublicInboxLocalStore(unittest.TestCase):
    """Test SQLite operations over public-inbox v2 format."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self.test_dir.name)

        # 1. Create over.sqlite3
        over_path = self.storage_dir / "over.sqlite3"
        conn = sqlite3.connect(str(over_path))
        conn.execute(
            """
            CREATE TABLE over (
                num INTEGER PRIMARY KEY,
                tid INTEGER,
                sid INTEGER,
                ts INTEGER,
                ds INTEGER,
                ddd BLOB
            )
            """
        )
        conn.execute("CREATE TABLE id2num (id TEXT PRIMARY KEY, num INTEGER)")

        # Create compressed ddd blob
        # Format: subject\nfrom\nrefs\nmessage_id\nkv\n
        from_hdr = "Alice Dev <alice@example.org>"
        subj = "[PATCH 1/2] driver: core: bus probe"
        mid = "20210515.patch1@example.org"
        refs = ""
        blob_sha = ":deadbeef0123456789abcdef0123456789abcdef"
        raw_ddd = f"{subj}\n{from_hdr}\n{refs}\n{mid}\n{blob_sha}\n".encode("utf-8")
        compressed_ddd = zlib.compress(raw_ddd)

        conn.execute(
            "INSERT INTO over (num, tid, sid, ts, ds, ddd) VALUES (?, ?, ?, ?, ?, ?)",
            (10, 10, 1, 1621000000, 1621000000, compressed_ddd),
        )
        conn.execute(
            "INSERT INTO id2num (id, num) VALUES (?, ?)",
            (mid, 10),
        )

        # Cover letter
        c_subj = "[PATCH 0/2] driver: core: bus series"
        c_mid = "20210515.cover@example.org"
        c_blob = ":feedface0123456789abcdef0123456789abcdef"
        c_raw = f"{c_subj}\n{from_hdr}\n{refs}\n{c_mid}\n{c_blob}\n".encode("utf-8")
        conn.execute(
            "INSERT INTO over (num, tid, sid, ts, ds, ddd) VALUES (?, ?, ?, ?, ?, ?)",
            (9, 10, 1, 1620999900, 1620999900, zlib.compress(c_raw)),
        )
        conn.execute(
            "INSERT INTO id2num (id, num) VALUES (?, ?)",
            (c_mid, 9),
        )

        conn.commit()
        conn.close()

        # 2. Create msgmap.sqlite3
        msgmap_path = self.storage_dir / "msgmap.sqlite3"
        conn2 = sqlite3.connect(str(msgmap_path))
        conn2.execute("CREATE TABLE msg_map (num INTEGER PRIMARY KEY, mid TEXT)")
        conn2.execute("INSERT INTO msg_map (num, mid) VALUES (10, ?)", (mid,))
        conn2.execute("INSERT INTO msg_map (num, mid) VALUES (9, ?)", (c_mid,))
        conn2.commit()
        conn2.close()

        self.store = PublicInboxLocalStore(self.storage_dir)

    def tearDown(self):
        self.store.close()
        self.test_dir.cleanup()

    def test_lookup_by_mid(self):
        rec = self.store.lookup_by_mid("20210515.patch1@example.org")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.num, 10)
        self.assertEqual(rec.tid, 10)
        self.assertEqual(rec.blob, "deadbeef0123456789abcdef0123456789abcdef")
        self.assertIn("20210515.patch1@example.org", rec.mids)

    def test_lookup_by_blob(self):
        recs = self.store.lookup_by_blob("deadbeef0123456789abcdef0123456789abcdef")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].num, 10)

    def test_lookup_thread_and_cover_letter(self):
        thread = self.store.lookup_thread(10)
        self.assertEqual(len(thread), 2)
        cover = self.store.get_cover_letter(10)
        self.assertIsNotNone(cover)
        self.assertEqual(cover.num, 9)
        self.assertIn("0/2", cover.subj)

    def _insert_future_dated_message(self):
        """Add a message whose Date: header is a year ahead of now; returns its Message-ID."""
        mid = "20990101.future@example.org"
        raw = (
            "[PATCH 1/1] driver: core: dated in the future\n"
            "Bob Dev <bob@example.org>\n"
            "\n"
            f"{mid}\n"
            ":abcdef010123456789abcdef0123456789abcdef\n"
        ).encode("utf-8")
        ahead = int(time.time()) + 86400 * 365
        conn = sqlite3.connect(str(self.storage_dir / "over.sqlite3"))
        conn.execute(
            "INSERT INTO over (num, tid, sid, ts, ds, ddd) VALUES (?, ?, ?, ?, ?, ?)",
            (11, 11, 2, ahead, ahead, zlib.compress(raw)),
        )
        conn.execute("INSERT INTO id2num (id, num) VALUES (?, ?)", (mid, 11))
        conn.commit()
        conn.close()
        return mid

    def test_empty_query_browse_excludes_future_dated(self):
        self._insert_future_dated_message()
        found = self.store.search_emails(query="", limit=50)
        nums = sorted(r["num"] for r in found["results"])
        self.assertEqual(nums, [9, 10])
        self.assertEqual(found["total"], 2)

    def test_explicit_query_still_finds_future_dated(self):
        self._insert_future_dated_message()
        found = self.store.search_emails(query="dated in the future", limit=50)
        self.assertEqual([r["num"] for r in found["results"]], [11])

    def test_unpack_standard_public_inbox_ddd(self):
        raw_ddd = (
            "[GIT PULL] hwmon updates for v7.4\n"
            "Guenter Roeck <linux@roeck-us.net>\n"
            "<ref1@example.org> <ref2@example.org>\n"
            "Linus Torvalds <torvalds@linux-foundation.org>\n"
            "linux-hwmon@vger.kernel.org\n"
            "97d1c1e335274a9b7fe1944f10f568c3b9fb81c8\n"
            "20261025145958.3032379-1-linux@roeck-us.net\n"
            "24650\n"
            "428\n"
        ).encode("utf-8")
        row = (100, 50, 1, 1600000000, 1600000000, zlib.compress(raw_ddd))
        rec = PublicInboxLocalStore._unpack_row(row)
        self.assertEqual(rec.subject, "[GIT PULL] hwmon updates for v7.4")
        self.assertEqual(rec.author, "Guenter Roeck <linux@roeck-us.net>")
        self.assertEqual(rec.references, ["ref1@example.org", "ref2@example.org"])
        self.assertEqual(rec.mids, ["20261025145958.3032379-1-linux@roeck-us.net"])
        self.assertEqual(rec.git_blob, "97d1c1e335274a9b7fe1944f10f568c3b9fb81c8")
        self.assertEqual(rec.bytes, 24650)
        self.assertEqual(rec.lines, 428)


class MockStore:
    """Mock PublicInboxLocalStore for tier resolution tests."""

    def __init__(self):
        self.mid_map: dict[str, SmsgRecord] = {}
        self.blob_map: dict[str, list[SmsgRecord]] = {}
        self.threads: dict[int, list[SmsgRecord]] = {}
        self.root = Path("/tmp")
        self.idx_dir = Path("/tmp")
        self.shard_count = 0
        self.xap_db = None
        self.qp = None

    def add_record(self, rec: SmsgRecord):
        for mid in rec.mids:
            self.mid_map[mid] = rec
        if rec.blob:
            self.blob_map.setdefault(rec.blob, []).append(rec)
        self.threads.setdefault(rec.tid, []).append(rec)

    def lookup_by_mid(self, mid: str) -> SmsgRecord | None:
        return self.mid_map.get(mid)

    get_by_mid = lookup_by_mid

    def lookup_by_blob(self, blob_sha: str) -> list[SmsgRecord]:
        return self.blob_map.get(blob_sha, [])

    def lookup_thread(self, tid: int, limit: int = 1000, offset: int = 0) -> list[SmsgRecord]:
        return self.threads.get(tid, [])[offset : offset + limit]

    get_thread_by_tid = lookup_thread

    def get_cover_letter(self, tid: int) -> SmsgRecord | None:
        for r in self.threads.get(tid, []):
            if "[patch 0/" in r.subj.lower() or ("[patch v" in r.subj.lower() and " 0/" in r.subj):
                return r
        return None

    def search_over_subjects(self, pattern: str, limit: int = 20, scan_limit: int = 500) -> list[SmsgRecord]:
        matches = []
        for r in self.mid_map.values():
            if pattern.lower() in r.subject.lower():
                matches.append(r)
        return matches[:limit]

    def search_patch_id(self, patch_id: str) -> list[SmsgRecord]:
        return []

    def search_metadata(self, author_name: str, author_date: int, subject: str) -> list[SmsgRecord]:
        results = []
        for r in self.mid_map.values():
            if author_name.lower() in r.from_hdr.lower() and abs(author_date - r.ds) < 86400 * 7:
                results.append(r)
        return results

    def search_pull_request(self, tag_or_branch: str, repo_url: str | None = None) -> list[SmsgRecord]:
        results = []
        for r in self.mid_map.values():
            if "pull" in r.subj.lower() and tag_or_branch.lower() in r.subj.lower():
                results.append(r)
        return results

    def get_email_detail(self, mid_or_num):
        rec = self.mid_map.get(str(mid_or_num))
        if not rec:
            for r in self.mid_map.values():
                if r.num == mid_or_num or str(r.num) == str(mid_or_num):
                    rec = r
                    break
        if not rec:
            return None
        return {
            "num": rec.num,
            "tid": rec.tid,
            "sid": rec.sid,
            "subject": rec.subject,
            "author": rec.author,
            "timestamp": rec.ts,
            "message_id": rec.mids[0] if rec.mids else "",
            "mids": rec.mids,
            "body": "Mock body content",
            "is_patch": "[patch" in rec.subject.lower(),
            "raw": f"From: {rec.author}\nSubject: {rec.subject}\n\nMock body content\n",
        }

    def get_thread_messages(self, tid, limit=1000, offset=0):
        thread = self.threads.get(tid, [])
        total = len(thread)
        thread = thread[offset : offset + limit]
        return {
            "tid": tid,
            "subject": thread[0].subject if thread else "",
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": (offset + len(thread)) < total,
            "participants": list({r.author for r in thread}),
            "messages": [
                {
                    "num": r.num,
                    "tid": r.tid,
                    "sid": r.sid,
                    "subject": r.subject,
                    "author": r.author,
                    "timestamp": r.ts,
                    "message_id": r.mids[0] if r.mids else "",
                }
                for r in thread
            ],
        }

    def search_emails(self, query="", limit=50, offset=0, filter_type="all"):
        matches = []
        for r in self.mid_map.values():
            if not query or query.lower() in r.subject.lower():
                matches.append(r)
        return {
            "query": query,
            "total": len(matches),
            "results": [
                {"num": r.num, "tid": r.tid, "subject": r.subject, "message_id": r.mids[0] if r.mids else ""}
                for r in matches[offset : offset + limit]
            ],
        }

    def close(self):
        pass


class TestKernelCommitEmailResolver(unittest.TestCase):
    """Test the 5-tier waterfall resolution in KernelCommitEmailResolver."""

    def setUp(self):
        self.mock_store = MockStore()
        self.resolver = KernelCommitEmailResolver(self.mock_store)

    def test_tier_1_trailer_resolution(self):
        rec = SmsgRecord(
            num=1,
            ts=1600000000,
            ds=1600000000,
            blob="abc123",
            mids=["20201010.patch@lore.kernel.org"],
            tid=1,
            subj="[PATCH 1/1] kbuild: fix typo",
            from_hdr="Bob Developer <bob@kernel.org>",
            bytes=1000,
            lines=20,
        )
        self.mock_store.add_record(rec)

        commit = CommitFeatures(
            commit_id=1,
            commit_hash="aaaa1111bbbb2222cccc3333dddd4444eeee5555",
            subject="kbuild: fix typo",
            author_name="Bob Developer",
            author_email="bob@kernel.org",
            author_date=1600000000,
            commit_message="Fix typo in Makefile\n\nLink: https://lore.kernel.org/r/20201010.patch@lore.kernel.org\nSigned-off-by: Bob Developer <bob@kernel.org>",
        )

        ctx = self.resolver.resolve_commit(commit)
        self.assertEqual(ctx.matched_tier, MLMatchTier.Trailer)
        self.assertIsNotNone(ctx.patch_email)
        self.assertEqual(ctx.patch_email.mids[0], "20201010.patch@lore.kernel.org")

    def test_tier_3_blob_resolution(self):
        rec = SmsgRecord(
            num=2,
            ts=1600000000,
            ds=1600000000,
            blob="0123456789abcdef0123456789abcdef01234567",
            mids=["20201011.blob@lore.kernel.org"],
            tid=2,
            subj="[PATCH 1/2] fs: optimize lookup",
            from_hdr="Carol Dev <carol@kernel.org>",
            bytes=2000,
            lines=40,
        )
        self.mock_store.add_record(rec)

        commit = CommitFeatures(
            commit_id=2,
            commit_hash="bbbb1111bbbb2222cccc3333dddd4444eeee5555",
            subject="fs: optimize lookup",
            author_name="Carol Dev",
            author_email="carol@kernel.org",
            author_date=1600000000,
            commit_message="FS improvements without link trailers.",
            blob_hashes=["0123456789abcdef0123456789abcdef01234567"],
        )

        ctx = self.resolver.resolve_commit(commit)
        self.assertEqual(ctx.matched_tier, MLMatchTier.BlobHash)
        self.assertIsNotNone(ctx.patch_email)
        self.assertEqual(ctx.patch_email.mids[0], "20201011.blob@lore.kernel.org")

    def test_tier_5_metadata_fallback(self):
        rec = SmsgRecord(
            num=3,
            ts=1600000000,
            ds=1600000000,
            blob="someblob",
            mids=["20201012.meta@lore.kernel.org"],
            tid=3,
            subj="[PATCH] sched: update balance threshold",
            from_hdr="Dave Engineer <dave@kernel.org>",
            bytes=1500,
            lines=30,
        )
        self.mock_store.add_record(rec)

        commit = CommitFeatures(
            commit_id=3,
            commit_hash="cccc1111bbbb2222cccc3333dddd4444eeee5555",
            subject="sched: update balance threshold",
            author_name="Dave Engineer",
            author_email="dave@kernel.org",
            author_date=1600000010,
            commit_message="Sched tune without links or blob matches.",
        )

        ctx = self.resolver.resolve_commit(commit)
        self.assertEqual(ctx.matched_tier, MLMatchTier.MetadataFallback)
        self.assertIsNotNone(ctx.patch_email)
        self.assertEqual(ctx.patch_email.mids[0], "20201012.meta@lore.kernel.org")

    def test_pull_request_resolution(self):
        pr_rec = SmsgRecord(
            num=4,
            ts=1600000000,
            ds=1600000000,
            blob="prblob",
            mids=["20201015.pull@lore.kernel.org"],
            tid=4,
            subj="[GIT PULL] ext4 fixes for v5.10-rc1",
            from_hdr="Theodore Ts'o <tytso@mit.edu>",
            bytes=5000,
            lines=100,
        )
        self.mock_store.add_record(pr_rec)

        commit = CommitFeatures(
            commit_id=4,
            commit_hash="dddd1111bbbb2222cccc3333dddd4444eeee5555",
            subject="Merge tag 'ext4_for_5.10-rc1' of git://git.kernel.org/pub/scm/linux/kernel/git/tytso/ext4",
            author_name="Linus Torvalds",
            author_email="torvalds@linux-foundation.org",
            author_date=1600001000,
            commit_message="Merge tag 'ext4_for_5.10-rc1'...",
        )

        ctx = self.resolver.resolve_commit(commit)
        self.assertIsNotNone(ctx.pull_request)
        self.assertEqual(ctx.pull_request.mids[0], "20201015.pull@lore.kernel.org")


class TestStagingAndPropagation(unittest.TestCase):
    """Test staging resolved records into TableEngine bridge format."""

    class MockTableEngine:
        def __init__(self):
            self.rows = []

        def set(self, table_id, row):
            self.rows.append((table_id, row))

    def test_stage_resolved_context(self):
        te = self.MockTableEngine()
        patch = SmsgRecord(
            num=1,
            ts=1600000000,
            ds=1600000000,
            blob="b1",
            mids=["patch@example.org"],
            tid=10,
            subj="[PATCH v2 1/3] driver: update",
            from_hdr="Eve <eve@example.org>",
            bytes=100,
            lines=10,
        )
        cover = SmsgRecord(
            num=2,
            ts=1600000000,
            ds=1600000000,
            blob="b2",
            mids=["cover@example.org"],
            tid=10,
            subj="[PATCH v2 0/3] driver: series",
            from_hdr="Eve <eve@example.org>",
            bytes=200,
            lines=20,
        )
        ctx = ResolvedCommitContext(
            commit_id=99,
            commit_hash="abcdef",
            patch_email=patch,
            cover_letter=cover,
            pull_request=None,
            matched_tier=MLMatchTier.Trailer,
            patch_version=2,
            patch_idx=1,
            patch_total=3,
        )

        staged = MailingListClient.stage_resolved_context(te, 99, ctx)
        self.assertEqual(len(staged), 2)
        self.assertEqual(len(te.rows), 2)
        # Check relation types: Patch (1), CoverLetter (2)
        self.assertEqual(te.rows[0][1][3], int(MLRelationType.Patch))
        self.assertEqual(te.rows[1][1][3], int(MLRelationType.CoverLetter))

    def test_propagate_pull_request(self):
        te = self.MockTableEngine()
        count = MailingListClient.propagate_pull_request_to_children(
            te,
            child_commit_ids=[101, 102, 103],
            pr_mid="pr-mid@example.org",
            pr_tid=555,
        )
        self.assertEqual(count, 3)
        self.assertEqual(len(te.rows), 3)
        for _, row in te.rows:
            self.assertEqual(row[1], "pr-mid@example.org")
            self.assertEqual(row[2], 555)
            self.assertEqual(row[3], int(MLRelationType.PullRequest))
            self.assertEqual(row[4], int(MLMatchTier.PullRequest))

    def test_stage_resolved_context_with_table_schema(self):
        """Verify staging works when passing Table instance (m_commit_email) into G.TE."""
        from core.DBLayout import m_commit_email
        from core.globalstuff import G

        te = self.MockTableEngine()
        orig_te = getattr(G, "TE", None)
        G.TE = te
        try:
            ctx = ResolvedCommitContext(
                commit_id=123,
                commit_hash="abcdef123",
                patch_email=SmsgRecord(
                    num=1, ts=1, ds=1, blob="b", mids=["p@ex.org"], tid=10, subj="[PATCH] test", from_hdr="A", bytes=1, lines=1
                ),
                matched_tier=MLMatchTier.Trailer,
                patch_version=1,
                patch_idx=1,
                patch_total=1,
            )
            staged = MailingListClient.stage_resolved_context(m_commit_email, 123, ctx)
            self.assertEqual(len(staged), 1)
            self.assertEqual(len(te.rows), 1)
            self.assertEqual(te.rows[0][0], m_commit_email.table_id)
            self.assertEqual(te.rows[0][1][0], 123)
            self.assertEqual(te.rows[0][1][1], "p@ex.org")
        finally:
            G.TE = orig_te


class TestServerClientHTTPRoundTrip(unittest.TestCase):
    """Test HTTP daemon requests between MLServerRequestHandler and MailingListClient."""

    @classmethod
    def setUpClass(cls):
        # Create a mock resolver on the server
        mock_store = MockStore()
        rec = SmsgRecord(
            num=1,
            ts=1600000000,
            ds=1600000000,
            blob="b_http",
            mids=["http.test@example.org"],
            tid=77,
            subj="[PATCH 1/1] net: http test",
            from_hdr="Frank <frank@example.org>",
            bytes=500,
            lines=25,
        )
        mock_store.add_record(rec)
        cls.resolver = KernelCommitEmailResolver(mock_store)

        # Start HTTP server on an ephemeral port
        cls.server = HTTPServer(("127.0.0.1", 0), MLServerRequestHandler)
        cls.server.resolver = cls.resolver
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_health_check(self):
        url = f"http://127.0.0.1:{self.port}/health"
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "ok")

    def test_resolve_batch_over_http(self):
        commit = CommitFeatures(
            commit_id=10,
            commit_hash="ffff111122223333444455556666777788889999",
            subject="net: http test",
            author_name="Frank",
            author_email="frank@example.org",
            author_date=1600000000,
            commit_message="Trailer Link: https://lore.kernel.org/r/http.test@example.org",
        )
        url = f"http://127.0.0.1:{self.port}/resolve_batch"
        payload = json.dumps({"commits": [commit.to_dict()]}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", {})
            self.assertIn("ffff111122223333444455556666777788889999", results)
            res = ResolvedCommitContext.from_dict(results["ffff111122223333444455556666777788889999"])
            self.assertIsNotNone(res.patch_email)
            self.assertEqual(res.patch_email.mids[0], "http.test@example.org")

    def test_get_message_over_http(self):
        url = f"http://127.0.0.1:{self.port}/message?mid=http.test@example.org"
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            msg = data.get("message")
            self.assertIsNotNone(msg)
            self.assertEqual(msg.get("message_id"), "http.test@example.org")
            self.assertEqual(msg.get("subject"), "[PATCH 1/1] net: http test")

    def test_get_thread_over_http(self):
        url = f"http://127.0.0.1:{self.port}/thread?tid=77"
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("tid"), 77)
            self.assertEqual(data.get("total"), 1)
            self.assertEqual(len(data.get("messages", [])), 1)

    def test_search_over_http(self):
        url = f"http://127.0.0.1:{self.port}/search?q=http+test"
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertGreaterEqual(data.get("total", 0), 1)
            self.assertEqual(data.get("results")[0]["message_id"], "http.test@example.org")


class TestMemoryBounds(unittest.TestCase):
    """Query results must stay proportional to the page, not to the archive.

    The archive this runs against holds 22M messages in a 19GB overview database, where
    one unbounded fetch exhausted the host's RAM and got the daemon OOM-killed.
    """

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self.test_dir.name)
        conn = sqlite3.connect(str(self.storage_dir / "over.sqlite3"))
        conn.execute(
            "CREATE TABLE over (num INTEGER PRIMARY KEY, tid INTEGER, sid INTEGER, "
            "ts INTEGER, ds INTEGER, ddd BLOB)"
        )
        conn.execute("CREATE TABLE id2num (id TEXT PRIMARY KEY, num INTEGER)")
        # One thread of 40 messages, newest last.
        for i in range(40):
            subj = f"[PATCH {i}/40] subsystem: change number {i}"
            ddd = zlib.compress(
                f"{subj}\nDev <dev@example.org>\n\nTo\nCc\n{'a' * 40}\nmid{i}@example.org\n100\n5\n".encode()
            )
            conn.execute(
                "INSERT INTO over (num, tid, sid, ts, ds, ddd) VALUES (?, ?, ?, ?, ?, ?)",
                (i + 1, 7, 1, 1600000000 + i, 1600000000 + i, ddd),
            )
            conn.execute("INSERT INTO id2num (id, num) VALUES (?, ?)", (f"mid{i}@example.org", i + 1))
        conn.commit()
        conn.close()
        self.store = PublicInboxLocalStore(self.storage_dir)

    def tearDown(self):
        self.store.close()
        self.test_dir.cleanup()

    def test_thread_fetch_is_bounded_and_reports_real_total(self):
        self.assertEqual(self.store.count_thread_messages(7), 40)
        page = self.store.get_thread_by_tid(7, limit=10)
        self.assertEqual(len(page), 10)
        data = self.store.get_thread_messages(7, limit=10)
        self.assertEqual(len(data["messages"]), 10)
        self.assertEqual(data["total"], 40, "total must be the real thread size")
        self.assertTrue(data["truncated"])

    def test_thread_fetch_cap_cannot_be_exceeded_by_a_caller(self):
        from parser.mailing_list.ml_store import MAX_THREAD_MESSAGES

        page = self.store.get_thread_by_tid(7, limit=MAX_THREAD_MESSAGES * 100)
        self.assertLessEqual(len(page), MAX_THREAD_MESSAGES)

    def test_thread_offset_pages_without_overlap(self):
        first = self.store.get_thread_by_tid(7, limit=10, offset=0)
        second = self.store.get_thread_by_tid(7, limit=10, offset=10)
        self.assertEqual(len(second), 10)
        self.assertFalse({r.num for r in first} & {r.num for r in second})

    def test_search_page_size_is_independent_of_offset(self):
        """A deep page must not materialize everything skipped before it."""
        shallow = self.store.search_emails(query="", limit=5, offset=0)
        deep = self.store.search_emails(query="", limit=5, offset=30)
        self.assertEqual(len(shallow["results"]), 5)
        self.assertEqual(len(deep["results"]), 5)
        self.assertFalse(
            {r["num"] for r in shallow["results"]} & {r["num"] for r in deep["results"]},
            "offset must be applied by the query, not by slicing a fetched list",
        )

    def test_search_clamps_hostile_limit_and_offset(self):
        from parser.mailing_list.ml_store import MAX_SEARCH_LIMIT, MAX_SEARCH_OFFSET

        out = self.store.search_emails(query="", limit=10_000_000, offset=10 ** 12)
        self.assertEqual(out["limit"], MAX_SEARCH_LIMIT)
        self.assertEqual(out["offset"], MAX_SEARCH_OFFSET)
        self.assertLessEqual(len(out["results"]), MAX_SEARCH_LIMIT)

    def test_search_reports_has_more(self):
        page = self.store.search_emails(query="", limit=5, offset=0)
        self.assertTrue(page["has_more"])
        tail = self.store.search_emails(query="", limit=5, offset=38)
        self.assertFalse(tail["has_more"])

    def test_cat_file_payload_rejects_implausible_size(self):
        """A desynchronized cat-file stream must not be handed a huge read()."""
        from parser.mailing_list.ml_store import MAX_RAW_EMAIL_BYTES

        class _Proc:
            class stdout:
                @staticmethod
                def read(n):
                    raise AssertionError("read() must not be called for a bogus size")

        with self.assertRaises(ValueError):
            PublicInboxLocalStore._read_cat_payload(_Proc(), MAX_RAW_EMAIL_BYTES + 1, keep=1024)
        with self.assertRaises(ValueError):
            PublicInboxLocalStore._read_cat_payload(_Proc(), -5, keep=1024)

    def test_cat_file_payload_drains_oversized_body_without_keeping_it(self):
        """The tail beyond `keep` is consumed to resync, not retained."""
        body = b"x" * 4096

        class _Proc:
            def __init__(self):
                self.buf = body + b"\n"

            class _Out:
                def __init__(self, outer):
                    self.outer = outer

                def read(self, n):
                    chunk = self.outer.buf[:n]
                    self.outer.buf = self.outer.buf[n:]
                    return chunk

            @property
            def stdout(self):
                if not hasattr(self, "_out"):
                    self._out = self._Out(self)
                return self._out

        proc = _Proc()
        kept = PublicInboxLocalStore._read_cat_payload(proc, len(body), keep=100)
        self.assertEqual(len(kept), 100)
        self.assertEqual(proc.buf, b"", "payload tail and trailing LF must be consumed")


class TestXapianSerialization(unittest.TestCase):
    """A xapian.Database is not thread-safe: searching must hold the store lock.

    Concurrent searches on the shared handle abort the daemon process inside
    libxapian, so `execute_xapian` and `parse_query` must serialize on `_qp_lock`.
    """

    class _FakeMSet(list):
        """Stands in for a xapian.MSet, which is a sequence plus match estimates."""

        def get_matches_estimated(self) -> int:
            return len(self)

    @staticmethod
    def _bare_store() -> PublicInboxLocalStore:
        """A store with only the Xapian attributes set, bypassing on-disk discovery."""
        store = object.__new__(PublicInboxLocalStore)
        store._qp_lock = threading.Lock()
        store.xap_db = object()
        store.qp = None
        store.get_by_mid = lambda mid: None
        return store

    def test_execute_xapian_holds_lock_while_searching(self):
        import parser.mailing_list.ml_store as ml_store

        held: list[bool] = []
        store = self._bare_store()

        class _FakeEnquire:
            def __init__(self, db):
                held.append(store._qp_lock.locked())

            def set_query(self, q):
                pass

            def set_weighting_scheme(self, w):
                pass

            def set_sort_by_value(self, slot, reverse):
                pass

            def get_mset(self, start, limit):
                held.append(store._qp_lock.locked())
                return TestXapianSerialization._FakeMSet()

        class _FakeXapian:
            Enquire = _FakeEnquire
            DatabaseModifiedError = RuntimeError

            @staticmethod
            def BoolWeight():
                return None

        orig_xapian, orig_has = ml_store.xapian, ml_store.HAS_XAPIAN
        ml_store.xapian, ml_store.HAS_XAPIAN = _FakeXapian, True
        try:
            self.assertEqual(store.execute_xapian(object(), limit=5), [])
        finally:
            ml_store.xapian, ml_store.HAS_XAPIAN = orig_xapian, orig_has

        self.assertEqual(held, [True, True], "Xapian search ran without holding _qp_lock")
        self.assertFalse(store._qp_lock.locked(), "lock was not released")

    def test_parse_query_holds_lock(self):
        import parser.mailing_list.ml_store as ml_store

        seen: list[bool] = []

        class _FakeQP:
            def parse_query(self, q, flags=None):
                seen.append(store._qp_lock.locked())
                return "query"

        store = self._bare_store()
        store.qp = _FakeQP()

        orig_has = ml_store.HAS_XAPIAN
        orig_xapian = ml_store.xapian
        ml_store.HAS_XAPIAN, ml_store.xapian = True, object()
        try:
            self.assertEqual(store.parse_query("s:test"), "query")
            self.assertEqual(store.parse_query("s:test", 3), "query")
        finally:
            ml_store.HAS_XAPIAN, ml_store.xapian = orig_has, orig_xapian

        self.assertEqual(seen, [True, True], "parse_query ran without holding _qp_lock")
        self.assertFalse(store._qp_lock.locked())


class TestBlobQueryConstruction(unittest.TestCase):
    """A blob term must never reach Xapian as a bare prefix.

    A deleted file has no post-image, and the extractor represents it as an empty
    string. `"".startswith("0000000")` is False, so the old guard let the term become
    the bare prefix `XDFPOST`, whose wildcard expands to every blob term in the
    archive: over a gigabyte per commit, which grew the daemon until the kernel killed
    it, and a candidate list so broad it could produce false BlobHash matches.
    """

    class _FakeQuery:
        OP_OR = "OR"
        OP_WILDCARD = "WILDCARD"
        WILDCARD_LIMIT_FIRST = "FIRST"
        built: list[tuple] = []

        def __init__(self, op, arg, *rest):
            type(self).built.append((op, arg, rest))
            self.op, self.arg, self.rest = op, arg, rest

    def setUp(self):
        import parser.mailing_list.ml_resolver as mr

        self.mr = mr
        self._orig = mr.xapian
        self._FakeQuery.built = []

        class _FakeXapian:
            Query = self._FakeQuery

        mr.xapian = _FakeXapian

    def tearDown(self):
        self.mr.xapian = self._orig

    def test_empty_and_placeholder_blobs_are_rejected(self):
        norm = KernelCommitEmailResolver._normalize_blob
        self.assertEqual(norm(""), "")
        self.assertEqual(norm(None), "")
        self.assertEqual(norm("0" * 40), "")
        self.assertEqual(norm("0000000"), "")
        self.assertEqual(norm("abc"), "", "too short to be selective")
        self.assertEqual(norm("zzzznothex1234"), "")
        self.assertEqual(norm("DEADBEEF12345678"), "deadbeef12345678")

    def test_deletion_falls_back_to_the_pre_image(self):
        """A deletion has no post-image, so its pre-image term is used instead."""
        pre = "a" * 40
        queries = KernelCommitEmailResolver.blob_queries_for([(pre, "", "fs/removed.c")])
        self.assertEqual(len(queries), 1)
        patterns = [arg for op, arg, _ in self._FakeQuery.built if op == "WILDCARD"]
        self.assertEqual(patterns, ["XDFPRE" + pre[:10]])

    def test_no_query_is_built_when_both_sides_are_missing(self):
        self.assertEqual(KernelCommitEmailResolver.blob_queries_for([("", "", "x.c")]), [])
        self.assertEqual(KernelCommitEmailResolver.blob_queries_for([("0" * 40, "", "x.c")]), [])
        self.assertEqual(self._FakeQuery.built, [])

    def test_bare_prefix_wildcard_is_never_constructed(self):
        blobs = [
            ("b" * 40, "", "deleted.c"),          # deletion
            ("", "c" * 40, "added.c"),            # addition
            ("d" * 40, "e" * 40, "modified.c"),   # modification
            ("", "", "nothing.c"),                # neither side
            ("0" * 40, "0" * 40, "zeroes.c"),     # placeholders
        ]
        KernelCommitEmailResolver.blob_queries_for(blobs)
        patterns = [arg for op, arg, _ in self._FakeQuery.built if op == "WILDCARD"]
        for pattern in patterns:
            self.assertNotIn(pattern, ("XDFPOST", "XDFPRE"), "bare prefix wildcard built")
            self.assertGreater(
                len(pattern.replace("XDFPOST", "").replace("XDFPRE", "")),
                KernelCommitEmailResolver.MIN_BLOB_PREFIX - 1,
            )

    def test_probe_count_is_capped(self):
        blobs = [("", f"{i:040x}", f"f{i}.c") for i in range(1, 40)]
        queries = KernelCommitEmailResolver.blob_queries_for(blobs)
        self.assertEqual(len(queries), KernelCommitEmailResolver.MAX_BLOB_PROBES)

    def test_wildcard_expansion_is_bounded(self):
        """Every wildcard carries an expansion limit, whatever the pattern looks like."""
        KernelCommitEmailResolver.blob_queries_for([("", "f" * 40, "x.c")])
        wildcards = [(arg, rest) for op, arg, rest in self._FakeQuery.built if op == "WILDCARD"]
        self.assertEqual(len(wildcards), 1)
        _arg, rest = wildcards[0]
        self.assertEqual(rest[0], KernelCommitEmailResolver.MAX_WILDCARD_EXPANSION)
        self.assertEqual(rest[1], "FIRST")


class TestDaemonMemoryGuard(unittest.TestCase):
    """The daemon must refuse work rather than become the host's OOM victim."""

    def test_soft_limit_refuses_heavy_requests(self):
        from parser.mailing_list.ml_server import MemoryGuard
        import parser.mailing_list.ml_server as ml_server

        guard = MemoryGuard(max_rss_mb=100)
        orig = ml_server.current_rss_mb
        try:
            ml_server.current_rss_mb = lambda: 10.0
            self.assertIsNone(guard.check("/resolve_batch"), "well under the limit must pass")
            ml_server.current_rss_mb = lambda: 80.0
            self.assertEqual(guard.check("/resolve_batch"), 80.0, "above 75% must be reported")
        finally:
            ml_server.current_rss_mb = orig

    def test_peak_is_tracked_for_health(self):
        from parser.mailing_list.ml_server import MemoryGuard
        import parser.mailing_list.ml_server as ml_server

        guard = MemoryGuard(max_rss_mb=1000)
        orig = ml_server.current_rss_mb
        try:
            ml_server.current_rss_mb = lambda: 42.0
            guard.observe()
            ml_server.current_rss_mb = lambda: 17.0
            guard.observe()
        finally:
            ml_server.current_rss_mb = orig
        self.assertEqual(guard.peak_mb, 42.0)

    def test_heap_limit_is_applied(self):
        """RLIMIT_DATA turns a runaway allocation into a MemoryError, mid-request."""
        import resource as res
        from parser.mailing_list.ml_server import MemoryGuard

        original = res.getrlimit(res.RLIMIT_DATA)
        try:
            MemoryGuard(max_rss_mb=512).apply_heap_limit()
            soft, _hard = res.getrlimit(res.RLIMIT_DATA)
            self.assertNotEqual(soft, res.RLIM_INFINITY, "heap must be bounded")
            self.assertLessEqual(soft, int((512 + 256) * 1024 * 1024))
        finally:
            res.setrlimit(res.RLIMIT_DATA, original)

    def test_resolve_batch_defers_under_memory_pressure(self):
        """A stop predicate must defer the rest of the batch, like the time budget."""
        store = MockStore()
        resolver = KernelCommitEmailResolver(store)
        feats = [
            CommitFeatures(commit_hash=f"cc{i:038d}", subject=f"net: pressure {i}", author_name="Ida")
            for i in range(6)
        ]
        results, pending = resolver.resolve_batch_until(feats, deadline=None, should_stop=lambda: True)
        self.assertEqual(len(pending), 5)
        self.assertEqual(pending, [c.sha for c in feats[1:]])


class TestResolveBatchTimeBudget(unittest.TestCase):
    """Time-budget deferral in the resolver and adaptive chunk retry in the client."""

    @staticmethod
    def _features(count: int, prefix: str = "ab") -> list:
        return [
            CommitFeatures(
                commit_hash=f"{prefix}{i:038d}",
                subject=f"net: budget test {i}",
                author_name="Grace",
                author_email="grace@example.org",
                author_date=1600000000,
            )
            for i in range(count)
        ]

    def test_resolver_defers_commits_past_deadline(self):
        """An exhausted budget returns partial results plus the deferred SHAs."""
        store = MockStore()
        resolver = KernelCommitEmailResolver(store)
        feats = self._features(5)

        # Deadline already elapsed: first commit still runs, the rest are deferred.
        results, pending = resolver.resolve_batch_until(feats, deadline=time.monotonic() - 1.0)
        self.assertEqual(len(pending), 4)
        self.assertEqual(pending, [c.sha for c in feats[1:]])
        self.assertNotIn(feats[1].sha, results)

    def test_resolver_no_deadline_processes_everything(self):
        store = MockStore()
        resolver = KernelCommitEmailResolver(store)
        feats = self._features(3)
        _results, pending = resolver.resolve_batch_until(feats, deadline=None)
        self.assertEqual(pending, [])

    def _make_client(self, port: int, batch_size: int, timeout: float) -> MailingListClient:
        """Build a tunnel-mode client bound to a local stub daemon, bypassing _init_backend."""
        client = object.__new__(MailingListClient)
        client.cfg = {}
        client.git_dir = ""
        client.storage_dir = "/srv/lore"
        client.port = port
        client.batch_size = batch_size
        client.request_timeout = timeout
        client.auto_start = False
        client.tunnel_cfg = {}
        client.use_tunnel = True
        client._in_process_resolver = None
        client._effective_port = port
        return client

    def test_client_splits_chunk_on_timeout(self):
        """A chunk that times out is halved and retried instead of aborting the run."""
        slow_threshold = 4
        seen_sizes: list[int] = []

        class _StubHandler(MLServerRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                commits = req.get("commits", [])
                seen_sizes.append(len(commits))
                if len(commits) >= slow_threshold:
                    # Simulate a daemon too slow to answer within the client timeout.
                    time.sleep(2.0)
                try:
                    self._send_json(200, {"count": 0, "results": {}, "pending": []})
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, fmt, *args):
                pass

        # Threaded, so the abandoned slow request does not block the retries behind it.
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        server.daemon_threads = True
        server.resolver = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = self._make_client(server.server_port, batch_size=4, timeout=1.0)
            client.resolve_commits(self._features(4, prefix="cd"))
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(seen_sizes[0], 4)
        self.assertTrue(seen_sizes[1:], "timed-out chunk was never retried")
        self.assertTrue(all(n < slow_threshold for n in seen_sizes[1:]))
        self.assertEqual(sum(seen_sizes[1:]), 4)

    def test_client_requeues_pending_shas(self):
        """SHAs the daemon defers are re-sent in smaller chunks and still resolved."""
        requests: list[int] = []

        class _StubHandler(MLServerRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                commits = req.get("commits", [])
                requests.append(len(commits))
                # First call resolves one commit and defers the rest; later calls resolve all.
                if len(requests) == 1:
                    head, tail = commits[:1], commits[1:]
                else:
                    head, tail = commits, []
                results = {
                    c["sha"]: ResolvedCommitContext(
                        commit_sha=c["sha"],
                        matched_tier=int(MLMatchTier.Trailer),
                        patch_email=SmsgRecord(num=1, tid=5, subject="[PATCH] x", mids=["m@e.org"]),
                    ).to_dict()
                    for c in head
                }
                self._send_json(200, {
                    "count": len(results),
                    "results": results,
                    "pending": [c["sha"] for c in tail],
                })

            def log_message(self, fmt, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), _StubHandler)
        server.resolver = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = self._make_client(server.server_port, batch_size=4, timeout=10.0)
            feats = self._features(4, prefix="ef")
            results = client.resolve_commits(feats)
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(len(results), 4)
        self.assertGreater(len(requests), 1)

    def test_client_shrinks_chunk_on_503_backpressure(self):
        """A daemon refusing work for memory reasons must shrink the request, not fail."""
        sizes: list[int] = []

        class _StubHandler(MLServerRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                commits = req.get("commits", [])
                sizes.append(len(commits))
                if len(commits) > 1:
                    self._send_json(503, {"error": "above memory soft limit"})
                else:
                    self._send_json(200, {"count": 0, "results": {}, "pending": []})

            def log_message(self, fmt, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        server.daemon_threads = True
        server.resolver = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = self._make_client(server.server_port, batch_size=4, timeout=10.0)
            client.BACKPRESSURE_SLEEP = 0.0
            results = client.resolve_commits(self._features(4, prefix="bp"))
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(results, {})
        self.assertEqual(sizes[0], 4)
        self.assertEqual(sizes[-1], 1, "chunks must shrink to single commits under backpressure")
        self.assertEqual(sizes.count(1), 4, "every commit must still be attempted")

    def test_client_sends_budget_below_timeout(self):
        """The daemon budget must be strictly below the client socket timeout."""
        captured: list[float] = []

        class _StubHandler(MLServerRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                captured.append(float(req.get("budget_s", 0)))
                self._send_json(200, {"count": 0, "results": {}, "pending": []})

            def log_message(self, fmt, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), _StubHandler)
        server.resolver = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = self._make_client(server.server_port, batch_size=10, timeout=100.0)
            client.resolve_commits(self._features(2, prefix="01"))
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(len(captured), 1)
        self.assertGreater(captured[0], 0.0)
        self.assertLess(captured[0], 100.0)


if __name__ == "__main__":
    unittest.main()
