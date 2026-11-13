"""tests/test_linux_branches_distributed.py - Unit tests for Linux history, mainline, and linux-next branch sequencing and distributed parsing."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from core import config, distributed
from parser.git_ast import release_order
from tests.test_distributed_parsing import DistDB


class TestLinuxBranchesAndDistributed(unittest.TestCase):
    """Verify branch discovery, topological ordering, tag parsing, and distributed task generation."""

    def test_topological_branch_ordering(self):
        """Verify find_repository_branches sorts branches into the 4 topological tiers."""
        raw_branches = [
            "linux-next/akpm",
            "stable/linux-3.0.y",
            "history/0.10-fix-RCS",
            "master",
            "linux-next/master",
            "stable/linux-2.6.11.y",
            "history/master",
            "stable/linux-7.3.y",
            "history/2.4-stable",
            "linux-next/pending-fixes",
            "stable/linux-6.6.y",
        ]

        with patch("core.config._git_out") as mock_git:
            # Build simulated for-each-ref output
            lines = [f"refs/remotes/{b} {b}" for b in raw_branches]
            mock_git.return_value = "\n".join(lines)

            sorted_branches = config.find_repository_branches(Path("repos/linux"))

        expected_order = [
            "history/master",
            "history/0.10-fix-RCS",
            "history/2.4-stable",
            "master",
            "stable/linux-2.6.11.y",
            "stable/linux-3.0.y",
            "stable/linux-6.6.y",
            "stable/linux-7.3.y",
            "linux-next/master",
            "linux-next/akpm",
            "linux-next/pending-fixes",
        ]
        self.assertEqual(sorted_branches, expected_order)

    def test_release_order_history_and_next_tags(self):
        """Verify release_order parses and sequences historical tags, mainline tags, and daily next tags."""
        # Historical tag without 'v'
        p_hist = release_order.parse_tag("2.4.0")
        self.assertIsNotNone(p_hist)
        self.assertEqual(p_hist["series"], (2, 4, 0))
        self.assertEqual(p_hist["kind"], "mainline")
        self.assertTrue(p_hist["final"])

        p_hist_stable = release_order.parse_tag("2.4.1")
        self.assertIsNotNone(p_hist_stable)
        self.assertEqual(p_hist_stable["series"], (2, 4, 0))
        self.assertEqual(p_hist_stable["stable"], 1)
        self.assertEqual(p_hist_stable["kind"], "stable")

        # Linux-Next daily tag
        p_next = release_order.parse_tag("next-20260810")
        self.assertIsNotNone(p_next)
        self.assertEqual(p_next["kind"], "next")
        self.assertEqual(p_next["series"], (999, 2026, 8))
        self.assertEqual(p_next["stable"], 10)

        # Chronological sort key order
        tags = [
            "next-20260811",
            "v3.0",
            "2.4.0",
            "next-20260810",
            "2.4.1",
            "v2.6.12",
            "v2.6.12-rc1",
        ]
        sorted_tags = sorted(tags, key=release_order.release_key)
        expected_tags = [
            "2.4.0",
            "2.4.1",
            "v2.6.12-rc1",
            "v2.6.12",
            "v3.0",
            "next-20260810",
            "next-20260811",
        ]
        self.assertEqual(sorted_tags, expected_tags)

    def test_release_ranges_with_next_tags(self):
        """Verify release_ranges creates sequential base ranges for daily next tags."""
        tags = ["v3.0", "next-20260810", "next-20260811"]
        ranges = release_order.release_ranges(tags)

        self.assertEqual(len(ranges), 3)
        self.assertEqual(ranges[0]["tag"], "v3.0")
        self.assertEqual(ranges[0]["kind"], "mainline")

        self.assertEqual(ranges[1]["tag"], "next-20260810")
        self.assertEqual(ranges[1]["kind"], "next")
        self.assertEqual(ranges[1]["segment_base"], "v3.0")

        self.assertEqual(ranges[2]["tag"], "next-20260811")
        self.assertEqual(ranges[2]["kind"], "next")
        self.assertEqual(ranges[2]["segment_base"], "next-20260810")

    def test_resolve_branch_base_dependency(self):
        """Verify automated merge-base dependency resolution against repos/linux."""
        # linux-next/master forks from master
        base_ref, base_cid = distributed.resolve_branch_base_dependency("repos/linux", "linux-next/master")
        self.assertEqual(base_ref, "master")

        # stable/linux-3.0.y forks from v3.0
        base_ref, base_cid = distributed.resolve_branch_base_dependency("repos/linux", "stable/linux-3.0.y")
        self.assertEqual(base_ref, "v3.0")

        # history/2.4-stable forks from 2.4.15 in history
        base_ref, base_cid = distributed.resolve_branch_base_dependency("repos/linux", "history/2.4-stable")
        self.assertEqual(base_ref, "2.4.15")

    def test_publish_delegable_chains_with_branches(self):
        """Verify publish_delegable_chains populates tasks with merge-base dependencies."""
        db = DistDB()

        version_states = {
            "v3.0": (100, "done"),
            "master": (500, "done"),
        }

        enqueued = distributed.publish_delegable_chains(
            db,
            network_id=1,
            repo_id=1,
            all_tags=["v3.0", "v3.0.1", "next-20260810"],
            version_states=version_states,
            delegate_branches=["linux-next/master", "stable/linux-3.0.y"],
            repo_dir="repos/linux",
        )
        self.assertTrue(enqueued >= 2)

        tasks = {t["ref_name"]: t for t in db.tasks}
        self.assertIn("v3.0.1", tasks)
        self.assertIn("next-20260810", tasks)
        self.assertIn("linux-next/master", tasks)
        self.assertIn("stable/linux-3.0.y", tasks)

        # Status of linux-next/master: base is master (commit_id 500, state done) -> TASK_ELIGIBLE (1)
        self.assertEqual(tasks["linux-next/master"]["base_ref_name"], "master")
        self.assertEqual(tasks["linux-next/master"]["status"], distributed.TASK_ELIGIBLE)
        self.assertEqual(tasks["stable/linux-3.0.y"]["base_ref_name"], "v3.0")
        self.assertEqual(tasks["stable/linux-3.0.y"]["status"], distributed.TASK_ELIGIBLE)

    def test_claim_next_task_priority(self):
        """Verify claim_next_task hands out chains in the order history -> stable -> next -> other branches."""
        elig = distributed.TASK_ELIGIBLE
        db = DistDB().add_tasks(
            {"task_id": 1, "chain_name": "branch:topic/x", "chain_kind": "branch", "ref_name": "topic/x", "status": elig},
            {"task_id": 2, "chain_name": "next:next-20260810", "chain_kind": "next", "ref_name": "next-20260810", "status": elig},
            {"task_id": 3, "chain_name": "stable:v3.0", "chain_kind": "stable", "ref_name": "v3.0.1", "status": elig},
            {"task_id": 4, "chain_name": "branch:history/2.4-stable", "chain_kind": "history", "ref_name": "history/2.4-stable",
             "base_ref_name": "2.4.15", "base_commit_id": 10, "status": elig},
        )

        task = distributed.claim_next_task(db, network_id=1, worker_id="test-worker")
        self.assertIsNotNone(task)
        self.assertEqual(task["ref_name"], "history/2.4-stable")
        order = [task["ref_name"]]
        while (task := distributed.claim_next_task(db, network_id=1, worker_id="test-worker")) is not None:
            order.append(task["ref_name"])
        self.assertEqual(order, ["history/2.4-stable", "v3.0.1", "next-20260810", "topic/x"])


if __name__ == "__main__":
    unittest.main()

