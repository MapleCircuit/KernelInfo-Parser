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
