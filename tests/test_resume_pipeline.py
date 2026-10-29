"""tests/test_resume_pipeline.py - Unit tests for fast-forward startup, crash recovery, and repair."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core import ingest_state
from main import orchestrate_repair_pipeline


class TestResumePipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tags = ["v1.0", "v1.1", "v1.2", "v1.3", "v1.4"]

    def test_repair_detects_gap_and_successor(self) -> None:
        # v1.0 and v1.1 done; v1.2 missing; v1.3 and v1.4 done
        states = {
            "v1.0": (100, "done"),
            "v1.1": (101, "done"),
            "v1.2": (102, "new"),
            "v1.3": (103, "done"),
            "v1.4": (104, "done"),
        }
        # In dry run, it should detect the gap at v1.2 with pred v1.1 and succ v1.3
        repaired = orchestrate_repair_pipeline(
            self.tags,
            states,
            network_id=1,
            repo_id=1,
            repo_path="/fake/repo",
            dry_run=True,
        )
        self.assertTrue(repaired)

    def test_repair_reports_contiguous_when_no_gaps(self) -> None:
        # All versions contiguous: v1.0, v1.1, v1.2 done, remainder new
        states = {
            "v1.0": (100, "done"),
            "v1.1": (101, "done"),
            "v1.2": (102, "done"),
            "v1.3": (103, "new"),
            "v1.4": (104, "new"),
        }
        repaired = orchestrate_repair_pipeline(
            self.tags,
            states,
            network_id=1,
            repo_id=1,
            repo_path="/fake/repo",
            dry_run=True,
        )
        self.assertFalse(repaired)

    def test_backfill_specific_version_dry_run(self) -> None:
        states = {
            "v1.0": (100, "done"),
            "v1.1": (101, "new"),
            "v1.2": (102, "done"),
        }
        repaired = orchestrate_repair_pipeline(
            self.tags,
            states,
            network_id=1,
            repo_id=1,
            repo_path="/fake/repo",
            backfill_target="v1.1",
            dry_run=True,
        )
        self.assertTrue(repaired)

    def test_backfill_unknown_target_returns_false(self) -> None:
        states = {"v1.0": (100, "done")}
        repaired = orchestrate_repair_pipeline(
            self.tags,
            states,
            network_id=1,
            repo_id=1,
            repo_path="/fake/repo",
            backfill_target="v9.9.9-nonexistent",
            dry_run=True,
        )
        self.assertFalse(repaired)

    @patch("main.update")
    @patch("core.ingest_state.purge_version_differential_rows")
    @patch("core.globalstuff.G.DB")
    def test_repair_execution_backfills_and_realigns(
        self, mock_db_ctx: MagicMock, mock_purge: MagicMock, mock_update: MagicMock
    ) -> None:
        states = {
            "v1.0": (100, "done"),
            "v1.1": (101, "new"),
            "v1.2": (102, "done"),
        }
        mock_db = MagicMock()
        mock_db.cnx = MagicMock()
        mock_db.select.return_value = (1, 1, 1, "v1.1", 101, None, None, None)
        mock_db_ctx.return_value.__enter__.return_value = mock_db

        repaired = orchestrate_repair_pipeline(
            ["v1.0", "v1.1", "v1.2"],
            states,
            network_id=1,
            repo_id=1,
            repo_path="/fake/repo",
            backfill_target="v1.1",
            dry_run=False,
        )
        self.assertTrue(repaired)
        # Should have updated v1.1 and then re-aligned v1.2
        self.assertEqual(mock_update.call_count, 2)
        mock_update.assert_any_call("v1.1", repo_path="/fake/repo")
        mock_update.assert_any_call("v1.2", repo_path="/fake/repo")
        # Should have purged differential rows of v1.2
        mock_purge.assert_called_once_with(mock_db.cnx, 102, pred_commit_id=101)


if __name__ == "__main__":
    unittest.main()
