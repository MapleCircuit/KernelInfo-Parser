"""tests/test_max_allowed_packet.py - Unit tests for dynamic byte-bounded batching and adaptive bisection on OperationalError 1153."""
import unittest
from unit_test.test_db_engine import TestMaxAllowedPacketAdaptiveBisection

__all__ = ["TestMaxAllowedPacketAdaptiveBisection"]

if __name__ == "__main__":
    unittest.main()
