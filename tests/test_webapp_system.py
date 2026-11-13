"""tests/test_webapp_system.py - Read-only database instance token (sync service) and Rule 35 compliance."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.db_setup import ensure_db_instance
from core.DBLayout import m_db_instance
from db_engine import MockDB
from tests.db_fixtures import patch_reader
from webapp.backend.database.pool import get_reader
from webapp.backend.services.sync_service import SyncService


class TestWebappSystem(unittest.TestCase):
    """Test suite for the database instance token and database read-only invariants."""

    def _db_token(self, db: MockDB) -> str:
        with patch_reader(db):
            return SyncService().db_token(fresh=True)

    def test_db_token_populated(self) -> None:
        """The `db` sync token is the instance hash, read through the read-only session."""
        db = MockDB(use_global=False)
        db.insert_rows("m_db_instance", ["instance_hash", "created_at"], [("a" * 64, 1)])
        self.assertEqual(self._db_token(db), "a" * 12)
        self.assertEqual(db.count_rows("m_db_instance"), 1, "nothing was written")

    def test_db_token_empty_or_unavailable(self) -> None:
        """An empty or missing m_db_instance gives an empty token, without writing anything."""
        empty = MockDB(use_global=False)
        self.assertEqual(self._db_token(empty), "")
        self.assertEqual(empty.count_rows("m_db_instance"), 0)

        broken = MockDB(use_global=False)
        broken.drop_tables(["m_db_instance"])
        self.assertEqual(self._db_token(broken), "")

    def test_webapp_reader_is_read_only(self) -> None:
        """webapp/API.md 2.5: every engine's request session refuses writes, DDL and commits."""
        from db_engine import ReadOnlyViolation, SQLiteDB
        for engine in (MockDB(use_global=False), SQLiteDB(":memory:")):
            with patch_reader(engine), get_reader() as reader:
                for call in (
                    lambda: reader.insert_rows("m_db_instance", ["instance_hash"], [(b"x",)]),
                    lambda: reader.delete_rows("m_db_instance", all_rows=True),
                    lambda: reader.drop_tables(["m_db_instance"]),
                    lambda: reader.commit(),
                ):
                    with self.assertRaises(ReadOnlyViolation, msg=type(engine).__name__):
                        call()

    def test_ensure_db_instance_seeding(self) -> None:
        """Verify parser helper ensure_db_instance checks and seeds m_db_instance if empty."""
        mock_db = MagicMock()
        # Case 1: Already has row
        mock_db.select.return_value = ("hash123", 100)
        ensure_db_instance(mock_db)
        mock_db.insert.assert_not_called()

        # Case 2: Empty table
        mock_db.select.return_value = None
        ensure_db_instance(mock_db)
        mock_db.insert.assert_called_once()
        self.assertEqual(mock_db.insert.call_args[0][0], m_db_instance)
        inserted_row = mock_db.insert.call_args[0][1]
        self.assertEqual(len(inserted_row), 1)
        self.assertEqual(len(inserted_row[0][0]), 64)  # 32-byte hex string


if __name__ == "__main__":
    unittest.main()


class TestIngestStatus(unittest.TestCase):
    """GET /api/system/ingest (G11): shape, stale detection, read-only SQL, repository scoping."""

    def _run(self, states, refs, now=1_000_000.0, network_id=7, repo_id=3):
        from webapp.backend.routers import system
        db = MockDB(use_global=False)
        for commit_id, status, updated_at, commit_hash in states:
            db.insert_rows(
                "m_commit",
                ["commit_id", "network_id", "commit_hash", "author_id", "author_date", "committer_id", "committer_date", "subject", "message"],
                [(commit_id, network_id, commit_hash, 0, 0, 0, 0, "", "")],
            )
            db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"], [(network_id, commit_id, status, updated_at)])
        # Another network's state and another repository's refs must stay out
        db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"], [(network_id + 1, 99, 2, 5)])
        for i, (commit_id, name) in enumerate(refs):
            db.insert_rows("m_git_ref", ["ref_id", "repo_id", "ref_type", "ref_name", "commit_id"], [(i + 1, repo_id, 1, name, commit_id)])
        db.insert_rows("m_git_ref", ["ref_id", "repo_id", "ref_type", "ref_name", "commit_id"], [(50, repo_id + 1, 1, "other", 1)])
        with patch_reader(db), \
                patch.object(system, "get_active_network_id", return_value=network_id), \
                patch.object(system, "get_active_repo_id", return_value=repo_id), \
                patch.object(system, "_git_tag_count", return_value=10):
            out = system.ingest_status(now)
        return out, db

    def test_shape_and_staleness(self) -> None:
        now = 1_000_000.0
        states = [
            (1, 1, int(now - 60), b"a" * 20),
            (2, 1, int(now - 3600), b"b" * 20),
            (3, 2, int(now - 10), b"c" * 20),
        ]
        refs = [(1, "v3.2-rc4"), (3, "v3.1")]
        out, db = self._run(states, refs, now)
        self.assertEqual(out["catalog"], {"tags": 10, "parsed": 1, "parsing": 2, "git_only": 9})
        self.assertEqual([(p["version"], p["stale"]) for p in out["parsing"]], [("v3.2-rc4", False), (("b" * 20).encode().hex()[:12], True)])
        self.assertEqual(out["recent"], [{"version": "v3.1", "commit": ("c" * 20).encode().hex()[:12], "finished_at": int(now - 10)}])
        names = {t["name"]: t for t in out["db"]["tables"]}
        self.assertIn("m_ingest_state", names)
        self.assertEqual(names["m_ingest_state"]["rows_estimate"], db.count_rows("m_ingest_state"))
        self.assertTrue(out["available"])

    def test_unparsed_repository_and_missing_database(self) -> None:
        from webapp.backend.routers import system
        with patch_reader(MockDB(use_global=False)), \
                patch.object(system, "get_active_network_id", return_value=None), \
                patch.object(system, "get_active_repo_id", return_value=-1), \
                patch.object(system, "_git_tag_count", return_value=4):
            out = system.ingest_status(1.0)
        self.assertEqual(out["catalog"]["parsed"], 0)
        self.assertEqual(out["catalog"]["git_only"], 4)
        self.assertEqual(out["parsing"], [])
        with patch.object(system, "get_reader", side_effect=Exception("no database")), \
                patch.object(system, "_git_tag_count", return_value=None):
            out = system.ingest_status(1.0)
        self.assertFalse(out["available"])
        self.assertIn("no database", out["error"])
