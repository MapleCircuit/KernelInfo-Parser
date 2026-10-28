"""Unit tests for the Mailing List Commit Resolver Subsystem (parser/mailing_list)."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import zlib
from http.server import HTTPServer
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

    def lookup_thread(self, tid: int) -> list[SmsgRecord]:
        return self.threads.get(tid, [])

    get_thread_by_tid = lookup_thread

    def get_cover_letter(self, tid: int) -> SmsgRecord | None:
        for r in self.threads.get(tid, []):
            if "[patch 0/" in r.subj.lower() or ("[patch v" in r.subj.lower() and " 0/" in r.subj):
                return r
        return None

    def search_over_subjects(self, pattern: str, limit: int = 20) -> list[SmsgRecord]:
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


if __name__ == "__main__":
    unittest.main()
