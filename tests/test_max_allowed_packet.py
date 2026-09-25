"""tests/test_max_allowed_packet.py - Unit tests for dynamic byte-bounded batching and adaptive bisection on OperationalError 1153."""
import unittest
from unittest.mock import MagicMock
import mysql.connector
from core.TableHandling import Table
from db_engine.DBHandling import MariaDB


class TestMaxAllowedPacketAdaptiveBisection(unittest.TestCase):
    """Test dynamic batch sizing and recursive bisection upon packet limit errors."""

    def setUp(self) -> None:
        self.t_code = Table(
            table_id=10,
            table_name="m_tag_code",
            columns=(
                ("hash", "BINARY(32)", "NOT NULL"),
                ("code", "LONGTEXT", "NOT NULL"),
            ),
            primary=("hash",),
            no_duplicate=False,
            te_cached=("hash",),
        )

    def test_adaptive_bisection_on_error_1153(self) -> None:
        """When executemany raises OperationalError 1153, insert() bisects chunks and succeeds."""
        # Create a mock MariaDB instance without real DB socket
        maria = object.__new__(MariaDB)
        maria.cursor = MagicMock()
        maria.cnx = MagicMock()
        maria.check_if_connected = MagicMock()
        maria.close = MagicMock()

        # Simulate: batches of 4 or more rows fail with 1153, batches of < 4 rows succeed
        def mock_executemany(sql, chunk_data):
            if len(chunk_data) >= 4:
                err = mysql.connector.errors.OperationalError(
                    errno=1153, msg="Got a packet bigger than 'max_allowed_packet' bytes"
                )
                raise err
            # Succeeds for smaller chunks

        maria.cursor.executemany.side_effect = mock_executemany

        # 6 rows to insert
        sample_data = tuple((b"\x01" * 32, f"int func_{i}() {{ return {i}; }}") for i in range(6))

        # This should bisect 6 -> 3 + 3, and both chunks of 3 rows succeed!
        maria.insert(self.t_code, sample_data)

        # Verify executemany was called multiple times (initially failed, then bisected and succeeded)
        self.assertGreater(maria.cursor.executemany.call_count, 1)
        maria.cnx.commit.assert_called_once()

    def test_byte_bounded_chunking_large_payloads(self) -> None:
        """Tables with large text columns are chunked into smaller batches based on size."""
        maria = object.__new__(MariaDB)
        maria.cursor = MagicMock()
        maria.cnx = MagicMock()
        maria.check_if_connected = MagicMock()
        maria.close = MagicMock()

        # 10 rows of 500KB each = ~5MB total. Should chunk into multiple batches due to 4MB max_bytes limit.
        large_code = "A" * (500 * 1024)
        sample_data = tuple((b"\x02" * 32, large_code) for _ in range(10))

        maria.insert(self.t_code, sample_data)

        # Should have split into at least 2 batches
        self.assertGreaterEqual(maria.cursor.executemany.call_count, 2)


if __name__ == "__main__":
    unittest.main()
