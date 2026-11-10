"""tests/test_webapp_system.py - Read-only database instance token (sync service) and Rule 35 compliance."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import ensure_db_instance
from core.DBLayout import m_db_instance
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.services.sync_service import SyncService


class TestWebappSystem(unittest.TestCase):
    """Test suite for the database instance token and database read-only invariants."""

    def _db_token(self, cursor: MagicMock) -> str:
        with patch("webapp.backend.services.sync_service.get_db_cursor") as mock_get_cursor:
            mock_get_cursor.return_value.__enter__.return_value = cursor
            return SyncService().db_token(fresh=True)

    def test_db_token_populated(self) -> None:
        """The `db` sync token is the instance hash, read with a read-only SELECT."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"instance_hash": "a" * 64}
        self.assertEqual(self._db_token(mock_cursor), "a" * 12)
        mock_cursor.execute.assert_called_once()
        query = mock_cursor.execute.call_args[0][0]
        self.assertIn("SELECT", query.upper())
        self.assertNotIn("INSERT INTO", query.upper())
        self.assertNotIn("CREATE TABLE", query.upper())

    def test_db_token_empty_or_unavailable(self) -> None:
        """An empty or missing m_db_instance gives an empty token, without writing anything."""
        empty = MagicMock()
        empty.fetchone.return_value = None
        self.assertEqual(self._db_token(empty), "")
        for call_args in empty.execute.call_args_list:
            self.assertNotIn("INSERT INTO", call_args[0][0].upper())

        broken = MagicMock()
        broken.execute.side_effect = Exception("Table 'test.m_db_instance' doesn't exist")
        self.assertEqual(self._db_token(broken), "")

    def test_get_db_cursor_rejects_commit_argument(self) -> None:
        """Rule 35: get_db_cursor must not accept commit argument."""
        with self.assertRaises(TypeError):
            get_db_cursor(commit=True)  # type: ignore[call-arg]

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

    def _run(self, states, refs, tables, now=1_000_000.0, network_id=7, repo_id=3):
        from webapp.backend.routers import system
        cursor = MagicMock()
        results = [states, refs, tables]
        cursor.fetchall.side_effect = lambda: results.pop(0)
        with patch.object(system, "get_db_cursor") as get_cursor, \
                patch.object(system, "get_active_network_id", return_value=network_id), \
                patch.object(system, "get_active_repo_id", return_value=repo_id), \
                patch.object(system, "_git_tag_count", return_value=10):
            get_cursor.return_value.__enter__.return_value = cursor
            out = system.ingest_status(now)
        return out, cursor

    def test_shape_and_staleness(self) -> None:
        now = 1_000_000.0
        states = [
            {"commit_id": 1, "status": 1, "updated_at": int(now - 60), "commit_hash": "a" * 40},
            {"commit_id": 2, "status": 1, "updated_at": int(now - 3600), "commit_hash": "b" * 40},
            {"commit_id": 3, "status": 2, "updated_at": int(now - 10), "commit_hash": "c" * 40},
        ]
        refs = [{"commit_id": 1, "ref_name": "v3.2-rc4"}, {"commit_id": 3, "ref_name": "v3.1"}]
        tables = [{"TABLE_NAME": "m_ast", "TABLE_ROWS": 5, "DATA_LENGTH": 3 << 20, "INDEX_LENGTH": 1 << 20}]
        out, cursor = self._run(states, refs, tables, now)
        self.assertEqual(out["catalog"], {"tags": 10, "parsed": 1, "parsing": 2, "git_only": 9})
        self.assertEqual([(p["version"], p["stale"]) for p in out["parsing"]], [("v3.2-rc4", False), ("b" * 12, True)])
        self.assertEqual(out["recent"], [{"version": "v3.1", "commit": "c" * 12, "finished_at": int(now - 10)}])
        self.assertEqual(out["db"], {"tables": [{"name": "m_ast", "rows_estimate": 5, "data_mb": 3.0, "index_mb": 1.0}], "total_mb": 4.0})
        # Read-only (section 2.5) and scoped to the active repository's network and refs
        for call in cursor.execute.call_args_list:
            sql = call[0][0].strip().upper()
            self.assertTrue(sql.startswith("SELECT"), sql)
            for word in ("INSERT", "UPDATE ", "DELETE", "CREATE", "ALTER", "DROP"):
                self.assertNotIn(word, sql)
        self.assertEqual(cursor.execute.call_args_list[0][0][1][0], 7)
        self.assertEqual(cursor.execute.call_args_list[1][0][1], (3,))

    def test_unparsed_repository_and_missing_database(self) -> None:
        from webapp.backend.routers import system
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        with patch.object(system, "get_db_cursor") as get_cursor, \
                patch.object(system, "get_active_network_id", return_value=None), \
                patch.object(system, "get_active_repo_id", return_value=-1), \
                patch.object(system, "_git_tag_count", return_value=4):
            get_cursor.return_value.__enter__.return_value = cursor
            out = system.ingest_status(1.0)
        self.assertEqual(out["catalog"]["parsed"], 0)
        self.assertEqual(out["catalog"]["git_only"], 4)
        self.assertEqual(cursor.execute.call_count, 1)  # only information_schema
        with patch.object(system, "get_db_cursor", side_effect=Exception("no database")), \
                patch.object(system, "_git_tag_count", return_value=None):
            out = system.ingest_status(1.0)
        self.assertFalse(out["available"])
        self.assertIn("no database", out["error"])
