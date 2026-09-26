"""tests/test_webapp_system.py - Unit tests for read-only database instance endpoint and Rule 35 compliance."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import ensure_db_instance
from core.DBLayout import m_db_instance
from webapp.backend.routers.system import get_db_instance, _FALLBACK_INSTANCE_HASH, _FALLBACK_CREATED_AT
from webapp.backend.database.pool import get_db_cursor


class TestWebappSystem(unittest.TestCase):
    """Test suite for system endpoints and database read-only invariants."""

    def test_get_db_instance_populated(self) -> None:
        """Test get_db_instance with populated database row."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {
            "instance_hash": "a" * 64,
            "created_at": 1727000000,
        }

        with patch("webapp.backend.routers.system.get_db_cursor") as mock_get_cursor:
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            res = get_db_instance()

        self.assertEqual(res["instance_hash"], "a" * 64)
        self.assertEqual(res["created_at"], 1727000000)
        # Verify query executed was read-only SELECT
        mock_cursor.execute.assert_called_once()
        query = mock_cursor.execute.call_args[0][0]
        self.assertIn("SELECT", query.upper())
        self.assertNotIn("INSERT INTO", query.upper())
        self.assertNotIn("CREATE TABLE", query.upper())

    def test_get_db_instance_fallback_when_empty(self) -> None:
        """Test get_db_instance returns fallback when table has 0 rows without modifying DB."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None

        with patch("webapp.backend.routers.system.get_db_cursor") as mock_get_cursor:
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            res = get_db_instance()

        self.assertEqual(res["instance_hash"], _FALLBACK_INSTANCE_HASH)
        self.assertEqual(res["created_at"], _FALLBACK_CREATED_AT)
        # Must not perform any write
        for call_args in mock_cursor.execute.call_args_list:
            query = call_args[0][0]
            self.assertNotIn("INSERT INTO", query.upper())
            self.assertNotIn("CREATE TABLE", query.upper())

    def test_get_db_instance_fallback_on_db_error(self) -> None:
        """Test get_db_instance catches database errors (e.g. table missing) gracefully."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("Table 'test.m_db_instance' doesn't exist")

        with patch("webapp.backend.routers.system.get_db_cursor") as mock_get_cursor:
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            res = get_db_instance()

        self.assertEqual(res["instance_hash"], _FALLBACK_INSTANCE_HASH)
        self.assertEqual(res["created_at"], _FALLBACK_CREATED_AT)

    def test_get_db_cursor_rejects_commit_argument(self) -> None:
        """Rule 35: get_db_cursor must not accept commit argument."""
        with self.assertRaises(TypeError):
            get_db_cursor(commit=True)  # type: ignore[call-arg]

    def test_ensure_db_instance_seeding(self) -> None:
        """Verify parser helper ensure_db_instance checks and seeds m_db_instance if empty."""
        mock_db = MagicMock()
        # Case 1: Already has row
        mock_db.get.return_value = [(1, "hash123", 100)]
        ensure_db_instance(mock_db)
        mock_db.insert.assert_not_called()

        # Case 2: Empty table
        mock_db.get.return_value = None
        ensure_db_instance(mock_db)
        mock_db.insert.assert_called_once()
        self.assertEqual(mock_db.insert.call_args[0][0], m_db_instance)
        inserted_row = mock_db.insert.call_args[0][1]
        self.assertEqual(len(inserted_row), 1)
        self.assertEqual(len(inserted_row[0][0]), 64)  # 32-byte hex string


if __name__ == "__main__":
    unittest.main()
