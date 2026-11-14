"""tests/test_distributed_parsing.py - Unit tests for Opt-In Multi-Computer Distributed Parsing."""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock
from unittest.mock import MagicMock

from core.DBLayout import (
    TABLES,
    m_ast,
    m_ast_container,
    m_bridge_checkpoint,
    m_checkpoint_base,
    m_commit,
    m_git_ref,
    m_ingest_state,
    m_repository,
    m_file_blob,
    m_file_name,
    m_ingest_id_range,
    m_maintainer_person,
    m_parse_node,
    m_parse_task,
    m_table_sequence,
)
from core import checkpoints, distributed, ingest_state
from db_engine import MockDB, SQLiteDB
from tests.db_fixtures import DIST_TABLES, DistDB
from table_engine.te_direct_db import TEDirectDB
from table_engine.te_cached_db import TECachedDB


class TestDistributedParsing(unittest.TestCase):
    def test_schema_tables_registered(self):
        """Verify all 66 tables including distributed coordination tables are registered."""
        self.assertEqual(len(TABLES), 66)
        names = {t.table_name for t in TABLES}
        for expected in ("m_table_sequence", "m_ingest_id_range", "m_parse_node", "m_parse_task"):
            self.assertIn(expected, names)
        # Verify unique_keys on m_parse_task uses ("network_id", "ref_name")
        self.assertEqual(m_parse_task.unique_keys, (("network_id", "ref_name"),))

    def test_reserve_id_block_disjoint(self):
        """Verify that successive reserve_id_block calls allocate strictly disjoint ranges and log them."""
        db = DistDB(full_schema=True)

        # Worker A reserves a block for m_ast (50,000), worker B the next one
        start1, end1 = db.reserve_id_block(m_ast, 50_000, commit_id=10)
        start2, end2 = db.reserve_id_block(m_ast, 50_000, commit_id=11)
        self.assertEqual(end1 - start1 + 1, 50_000)
        self.assertEqual(end2 - start2 + 1, 50_000)
        self.assertEqual(start2, end1 + 1)
        self.assertGreater(start2, end1)

        # The blocks are logged per commit in m_ingest_id_range
        ranges = db.select_rows("m_ingest_id_range", ["commit_id", "marked_table_id", "start_id", "end_id"])
        self.assertIn((10, m_ast.table_id, start1, end1), {tuple(r) for r in ranges})
        self.assertIn((11, m_ast.table_id, start2, end2), {tuple(r) for r in ranges})

    def test_reserve_id_block_starts_beyond_existing_rows(self):
        """A sequence seeded from an existing table never hands out an id that is already used."""
        from core.DBLayout import m_file_name
        db = DistDB(full_schema=True)
        db.insert_rows("m_file_name", ["fnid", "fname"], [(50_000, "x.c")])
        start, end = db.reserve_id_block(m_file_name, 1000)
        self.assertGreater(start, 50_000)
        self.assertEqual(end - start + 1, 1000)

    def test_table_engine_block_allocation(self):
        """Verify TableEngine uses block-allocated sequence IDs in distributed mode."""
        db = DistDB(full_schema=True)

        te = TEDirectDB()
        te.distributed = True
        te.current_commit_id = 42
        te.start(TABLES, lambda: db)

        # Stage row without explicit ID
        row1 = te.set(m_file_blob.table_id, (None, b"0" * 20, 1))
        row2 = te.set(m_file_blob.table_id, (None, b"1" * 20, 1))

        self.assertEqual(row2[0], row1[0] + 1)
        # Block of 1000 IDs was leased
        self.assertEqual(te._id_limit[m_file_blob.table_id], row1[0] + 999)

    def test_task_publication_and_worker_claiming(self):
        """Verify Master task publication, dependency unlocking, and worker claiming."""
        db = DistDB()
        tags = ["v3.0", "v3.0.1", "v3.0.2", "v3.1-rc1", "v3.1"]
        version_states = {
            "v3.0": (10, "done"),
            "v3.0.1": (11, "new"),
            "v3.0.2": (12, "new"),
            "v3.1-rc1": (13, "new"),
            "v3.1": (14, "new"),
        }

        published = distributed.publish_delegable_chains(
            db, network_id=1, repo_id=1, all_tags=tags,
            version_states=version_states, delegate_stable=True,
        )
        self.assertEqual(published, 2, "the stable series is delegated; mainline stays with the master")
        by_ref = {t["ref_name"]: t for t in db.tasks}
        self.assertEqual(set(by_ref), {"v3.0.1", "v3.0.2"})
        self.assertEqual(by_ref["v3.0.1"]["status"], distributed.TASK_ELIGIBLE, "its base v3.0 is done")
        self.assertEqual(by_ref["v3.0.2"]["status"], distributed.TASK_PENDING, "waits for v3.0.1")

        # Worker claims next task
        task = distributed.claim_next_task(db, network_id=1, worker_id="worker-node-1")
        self.assertIsNotNone(task)
        self.assertEqual(task["ref_name"], "v3.0.1")
        self.assertEqual(db.task(task["task_id"])["status"], distributed.TASK_LEASED)
        self.assertEqual(db.task(task["task_id"])["worker_id"], "worker-node-1")
        self.assertIsNone(distributed.claim_next_task(db, network_id=1, worker_id="worker-node-2"), "nothing else is eligible")

        # Worker finishes v3.0.1 -> marks done & unlocks v3.0.2
        distributed.mark_task_done(db, task_id=task["task_id"], commit_id=11, network_id=1)
        self.assertEqual(db.task(task["task_id"])["status"], distributed.TASK_DONE)
        self.assertEqual(by_ref["v3.0.2"]["task_id"], db.task(by_ref["v3.0.2"]["task_id"])["task_id"])
        self.assertEqual(db.task(by_ref["v3.0.2"]["task_id"])["status"], distributed.TASK_ELIGIBLE)

    def test_checkpoint_pinning_prevents_compaction(self):
        """Verify is_checkpoint_pinned prevents compacting an ancestor checkpoint into a delta."""
        db = DistDB().add_tasks({"task_id": 1, "base_commit_id": 10, "status": distributed.TASK_ELIGIBLE})

        # commit_id 10 is pinned because task 1 needs it as base
        self.assertTrue(checkpoints.is_checkpoint_pinned(db, 10))
        # commit_id 99 is not pinned
        self.assertFalse(checkpoints.is_checkpoint_pinned(db, 99))

        # Compaction on pinned commit_id 10 should abort cleanly
        result = checkpoints.compact(db, network_id=1, commit_id=10, current=13)
        self.assertIsNone(result)

    def test_worker_waiting_lifecycle(self):
        """Verify worker waiting logic exits only when Master and tasks are finished."""
        db = DistDB()

        # When queue is empty at startup (Master has not published tasks yet)
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

        # When tasks are pending
        db.add_tasks({"task_id": 1, "status": distributed.TASK_PENDING})
        db.add_nodes({"node_id": "master:1", "role": "master"})
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

        # When no runnable tasks remain but Master is still active
        db.update_rows("m_parse_task", {"status": distributed.TASK_DONE}, {"task_id": 1})
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

        # When all tasks done and Master has unregistered
        distributed.unregister_node(db, "master:1")
        self.assertTrue(distributed.is_parsing_finished(db, network_id=1))

    def test_range_isolated_rollback(self):
        """Verify rollback_id_ranges deletes only the leased range and preserves Git registries."""
        db = MockDB(use_global=False)
        db.insert_rows("m_ast", ["ast_id", "name", "type_id"], [(50, "old", 1), (100, "run_a", 1), (50099, "run_b", 1), (50100, "later", 1)])
        db.insert_rows("m_file_name", ["fnid", "fname"], [(150, "kernel/x.c")])
        db.insert_rows("m_ast_container", ["ast_id", "container_type", "kind", "line_e"] if False else [c[0] for c in m_ast_container.init_columns],
                       [tuple(100 if i == 0 else 0 for i, _c in enumerate(m_ast_container.init_columns))])
        removed = ingest_state.rollback_id_ranges(db, commit_id=11, tables=TABLES, id_ranges=[(m_ast.table_id, 100, 50099)])
        self.assertGreater(removed, 0)
        self.assertEqual(sorted(r[0] for r in db.select_rows("m_ast", ["ast_id"])), [50, 50100], "only the range goes")
        self.assertEqual(db.count_rows("m_ast_container"), 0, "its children go with it")
        self.assertEqual(db.count_rows("m_file_name"), 1, "Git registries are preserved")

    def test_unlock_dependent_tasks_base_ref(self):
        """Verify unlock_dependent_tasks updates tasks matching base_ref_name and sets base_commit_id."""
        db = DistDB().add_tasks(
            {"task_id": 1, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 0, "status": distributed.TASK_PENDING},
            {"task_id": 2, "ref_name": "v3.0.2", "base_ref_name": "v3.0.1", "base_commit_id": 0, "status": distributed.TASK_PENDING},
        )
        # Unlocking v3.0 with base_commit_id=10 and base_ref_name="v3.0"
        count = distributed.unlock_dependent_tasks(db, network_id=1, base_commit_id=10, base_ref_name="v3.0")
        self.assertEqual(count, 1)
        self.assertEqual(db.task(1)["status"], distributed.TASK_ELIGIBLE)
        self.assertEqual(db.task(1)["base_commit_id"], 10)
        self.assertEqual(db.task(2)["status"], distributed.TASK_PENDING)

    def test_mark_task_failed_guards_done(self):
        """Verify mark_task_failed guards against updating TASK_DONE tasks."""
        db = DistDB().add_tasks({"task_id": 1, "retry_count": 0, "status": distributed.TASK_DONE})
        result = distributed.mark_task_failed(db, task_id=1, error_msg="Late timeout error")
        self.assertFalse(result)
        self.assertEqual(db.task(1)["status"], distributed.TASK_DONE)
        self.assertEqual(db.task(1)["retry_count"], 0)

    def test_reclaim_expired_leases_lookup_ref(self):
        """Verify reclaim_expired_leases looks up commit_id from m_git_ref when cid is 0."""
        db = DistDB().add_tasks(
            {"task_id": 5, "commit_id": 0, "worker_id": "dead-worker", "ref_name": "v3.0.5",
             "status": distributed.TASK_LEASED, "lease_expires_at": 100}
        ).add_git_refs({"ref_name": "v3.0.5", "commit_id": 42})

        with unittest.mock.patch("core.distributed.rollback_version") as mock_rollback:
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 1)
            mock_rollback.assert_called_once_with(db, 1, 42, TABLES)

    def test_heartbeat_daemon_set_active_preserves_task_id(self):
        """Verify set_active retains current_task_id if task_id is None."""
        daemon = distributed.NodeHeartbeatDaemon(MagicMock, "worker-1")
        daemon.set_active(10, task_id=42)
        self.assertEqual(daemon.active_commit_id, 10)
        self.assertEqual(daemon.current_task_id, 42)

        # Update commit without passing task_id
        daemon.set_active(11)
        self.assertEqual(daemon.active_commit_id, 11)
        self.assertEqual(daemon.current_task_id, 42)

        # Clear active resets both
        daemon.clear_active()
        self.assertEqual(daemon.active_commit_id, 0)
        self.assertIsNone(daemon.current_task_id)

    def test_ingest_git_dag_distributed_reservations_and_heartbeat(self):
        """Verify ingest_git_dag uses reserve_id_block, updates heartbeat, and links leased task."""
        from main import G, gp
        from pipeline.git_ingest import ingest_git_dag
        from core.DBLayout import m_commit, m_git_ref

        mock_db = MagicMock()
        mock_db.select.return_value = None
        mock_db.reserve_id_block.side_effect = lambda table, count: (777, 777) if table == m_commit else (888, 888)

        mock_daemon = MagicMock()
        orig_distributed = getattr(G, "distributed", False)
        orig_role = getattr(G, "distributed_role", None)
        orig_node_id = getattr(G, "node_id", None)
        orig_daemon = getattr(G, "heartbeat_daemon", None)
        orig_active_task = getattr(gp, "_active_task_id", None)
        orig_db_ctx = G.DB

        try:
            G.distributed = True
            # A worker parses against the base of its task (gp.Old_VID), whatever it parsed before
            G.distributed_role = distributed.ROLE_WORKER
            G.node_id = "worker-node-1"
            G.heartbeat_daemon = mock_daemon
            gp._active_task_id = 123
            gp.Old_VID = 555
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: mock_db, __exit__=lambda s, e, t, b: None)

            mock_git_parser = MagicMock()
            mock_git_parser.get_commit_hash.return_value = "0" * 40
            mock_git_parser.parse_version_commits.return_value = []

            with unittest.mock.patch("parser.git_ast.GitCommitParser", return_value=mock_git_parser), \
                 unittest.mock.patch("core.ingest_state.begin") as mock_begin, \
                 unittest.mock.patch("core.ingest_state.fetch_repo_version_states", return_value={}):
                cid, is_chk, already = ingest_git_dag("v3.0.1", 1, 1, "repo")

            self.assertEqual(cid, 777)
            # Verify reserve_id_block called for both m_commit and m_git_ref
            mock_db.reserve_id_block.assert_any_call(m_commit, 1)
            mock_db.reserve_id_block.assert_any_call(m_git_ref, 1)

            # Verify ingest_state.begin called with base_commit_id and worker_id
            mock_begin.assert_called_once_with(
                mock_db, 1, 777, TABLES, base_commit_id=555, worker_id="worker-node-1"
            )

            # Verify heartbeat daemon updated
            mock_daemon.set_active.assert_called_once_with(777)

            # Verify m_parse_task updated with head_commit_id
            mock_db.update_rows.assert_any_call("m_parse_task", {"commit_id": 777}, {"task_id": 123})
        finally:
            G.distributed = orig_distributed
            G.distributed_role = orig_role
            G.node_id = orig_node_id
            G.heartbeat_daemon = orig_daemon
            gp._active_task_id = orig_active_task
            G.DB = orig_db_ctx


    def test_master_leadership_exclusivity(self):
        """Verify only one master can be actively registered per network."""
        import time
        db = DistDB()

        # Register Master 1
        distributed.register_node(db, "master-1", distributed.ROLE_MASTER, network_id=1)
        self.assertEqual(len(db.nodes), 1)

        # Register Master 2 while Master 1 is active -> RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            distributed.register_node(db, "master-2", distributed.ROLE_MASTER, network_id=1)
        self.assertIn("Another master node 'master-1' is already active for network 1", str(ctx.exception))
        self.assertEqual([n["node_id"] for n in db.nodes], ["master-1"])

        # Re-registering Master 1 itself succeeds
        distributed.register_node(db, "master-1", distributed.ROLE_MASTER, network_id=1)

        # If Master 1 died (>120s ago), Master 2 can take over
        db.update_rows("m_parse_node", {"heartbeat_at": int(time.time()) - 200}, {"node_id": "master-1"})
        distributed.register_node(db, "master-2", distributed.ROLE_MASTER, network_id=1)
        self.assertEqual(len(db.nodes), 2)

    def test_reclaim_expired_leases_dead_node_cleanup_and_mutual_exclusion(self):
        """Verify reclaim_expired_leases cleans dead nodes and leaves a task another worker already reclaimed alone."""
        now = int(time.time())
        db = DistDB().add_tasks(
            {"task_id": 1, "commit_id": 10, "worker_id": "reclaiming", "ref_name": "v3.0.1",
             "status": distributed.TASK_RECLAIMING, "lease_expires_at": now + 300, "network_id": 1}
        ).add_nodes({"node_id": "long-gone", "heartbeat_at": now - 2 * 86400}, {"node_id": "alive"})

        with unittest.mock.patch("core.distributed.rollback_version") as mock_rollback:
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 0, "a task already being reclaimed, with a live reclaim lease, is not taken again")
            mock_rollback.assert_not_called()

        # Dead node cleanup removed the node silent for more than a day
        self.assertEqual([n["node_id"] for n in db.nodes], ["alive"])

    def test_mark_task_done_lease_ownership_verification(self):
        """Verify mark_task_done enforces worker lease ownership and handles lost leases."""
        db = DistDB().add_tasks(
            {"task_id": 1, "commit_id": 10, "worker_id": "worker-1", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "network_id": 1}
        )

        # Another worker cannot complete the task
        self.assertFalse(distributed.mark_task_done(db, task_id=1, commit_id=10, network_id=1, worker_id="worker-2"))
        self.assertEqual(db.task(1)["status"], distributed.TASK_LEASED)

        # Valid worker_id completes task
        res = distributed.mark_task_done(db, task_id=1, commit_id=10, network_id=1, worker_id="worker-1")
        self.assertTrue(res)
        self.assertEqual(db.task(1)["status"], distributed.TASK_DONE)

        # A second completion of the already-done task -> returns False
        res2 = distributed.mark_task_done(db, task_id=1, commit_id=10, network_id=1, worker_id="worker-1")
        self.assertFalse(res2)

    def test_unlock_dependent_tasks_fallback_to_git_ref(self):
        """Verify unlock_dependent_tasks resolves the base ref name from m_git_ref when it is not given."""
        db = DistDB().add_tasks(
            {"task_id": 2, "base_ref_name": "v3.0", "base_commit_id": 0, "status": distributed.TASK_PENDING, "network_id": 1}
        ).add_git_refs({"ref_name": "v3.0", "commit_id": 10})

        # Call with base_ref_name=None and base_commit_id=10
        count = distributed.unlock_dependent_tasks(db, network_id=1, base_commit_id=10, base_ref_name=None)
        self.assertEqual(count, 1)
        self.assertEqual(db.task(2)["status"], distributed.TASK_ELIGIBLE)
        self.assertEqual(db.task(2)["base_commit_id"], 10)

    def test_mark_task_failed_cascading(self):
        """Verify cascading task cancellation when task reaches max retries."""
        db = DistDB().add_tasks(
            {"task_id": 1, "retry_count": 2, "status": distributed.TASK_LEASED, "commit_id": 11, "ref_name": "v3.0.1"},
            {"task_id": 2, "retry_count": 0, "status": distributed.TASK_PENDING, "commit_id": 0,
             "ref_name": "v3.0.2", "base_ref_name": "v3.0.1", "base_commit_id": 0},
        )

        # Task 1 fails (3rd failure -> TASK_FAILED)
        distributed.mark_task_failed(db, task_id=1, error_msg="Fatal compilation bug")
        self.assertEqual(db.task(1)["status"], distributed.TASK_FAILED)
        self.assertEqual(db.task(1)["retry_count"], 3)
        # Downstream task 2 should be cancelled
        self.assertEqual(db.task(2)["status"], distributed.TASK_FAILED)
        self.assertEqual(db.task(2)["error_msg"], "Prerequisite base task failed")

    def test_is_parsing_finished_startup_and_dead_chain(self):
        """Verify is_parsing_finished returns False on startup and True when orphaned pending tasks cannot run."""
        db = DistDB()

        # Empty task table -> False (startup guard)
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

        # Pending task with failed base task -> True (cannot be unblocked)
        db.add_tasks(
            {"task_id": 1, "status": distributed.TASK_FAILED, "ref_name": "v3.0", "commit_id": 10},
            {"task_id": 2, "status": distributed.TASK_PENDING, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 10},
        )
        self.assertTrue(distributed.is_parsing_finished(db, network_id=1))

    def test_tecacheddb_negative_cache_poisoning_avoidance(self):
        """Verify TECachedDB does not cache negative nodup misses in distributed mode."""
        db = DistDB(full_schema=True)

        # In standalone mode (distributed=False), misses are cached as None
        te_standalone = TECachedDB()
        te_standalone.distributed = False
        te_standalone.start(TABLES, lambda: db)
        db.select = mock.MagicMock(return_value=None)
        res1 = te_standalone._committed_nodup_id(m_file_name.table_id, (None, "test/path.c"))
        self.assertIsNone(res1)
        self.assertIn(m_file_name.table_id, te_standalone._committed_nodup_keys)
        self.assertIsNone(te_standalone._committed_nodup_keys[m_file_name.table_id].get(("test/path.c",)))

        # In distributed mode (distributed=True), misses are NOT cached in _committed_nodup_keys
        te_dist = TECachedDB()
        te_dist.distributed = True
        te_dist.start(TABLES, lambda: db)
        db.select = mock.MagicMock(return_value=None)
        res2 = te_dist._committed_nodup_id(m_file_name.table_id, (None, "test/path.c"))
        self.assertIsNone(res2)
        keys = te_dist._committed_nodup_keys.get(m_file_name.table_id, {})
        self.assertNotIn(("test/path.c",), keys)

    def test_interrupted_ingests_active_worker_exclusion(self):
        """Verify interrupted_ingests excludes active heartbeating workers in distributed mode."""
        from core import backfills
        now = int(time.time())
        parsing = ingest_state.INGEST_PARSING
        db = DistDB()
        self.assertEqual(backfills.interrupted_ingests(db), 0)

        def state(commit_id: int, status: int, worker: str) -> None:
            db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at", "worker_id"],
                           [(1, commit_id, status, 0, worker)])

        state(1, ingest_state.INGEST_DONE, "")
        state(2, parsing, "alive")          # a live worker parses it: in progress, not interrupted
        db.add_nodes({"node_id": "alive", "heartbeat_at": now})
        self.assertEqual(backfills.interrupted_ingests(db), 0)
        state(3, parsing, "")               # no worker: interrupted
        state(4, parsing, "ghost")          # worker never registered: interrupted
        state(5, parsing, "stale")          # worker silent for more than 180 s: interrupted
        db.add_nodes({"node_id": "stale", "heartbeat_at": now - 400})
        self.assertEqual(backfills.interrupted_ingests(db), 3)

        # Without the node table the plain count answers
        db.drop_tables(["m_parse_node"], ignore_fk=True)
        self.assertEqual(backfills.interrupted_ingests(db), 4)

    def test_master_keeps_mainline_and_non_release_refs(self):
        """The master parses what is not published: pre-3.0 mainline tags and historical names included."""
        tags = ["0.01", "0.95a", "2.4.0", "2.4.1", "v2.6.38", "v2.6.38.1", "v2.6.39-rc1", "v2.6.39", "v3.0",
                "v3.0.1", "v3.1-rc1", "v3.1", "next-20260810"]
        kept, delegated = distributed.partition_master_refs(tags, tags, delegate_stable=True)
        self.assertEqual(kept, ["0.01", "0.95a", "2.4.0", "v2.6.38", "v2.6.39-rc1", "v2.6.39", "v3.0", "v3.1-rc1", "v3.1"])
        self.assertEqual(delegated, ["2.4.1", "v2.6.38.1", "v3.0.1", "next-20260810"])
        # Every ref is either the master's or a published task: none is dropped
        self.assertEqual(sorted(kept + delegated), sorted(tags))

    def test_master_keeps_stable_updates_when_not_delegating(self):
        tags = ["v3.0", "v3.0.1", "v3.1", "next-20260810"]
        kept, delegated = distributed.partition_master_refs(tags, tags, delegate_stable=False)
        self.assertEqual(kept, ["v3.0", "v3.0.1", "v3.1"])
        # The next chains are published either way, so the master leaves them to the workers
        self.assertEqual(delegated, ["next-20260810"])

    def test_master_keeps_trunk_branches_and_delegates_the_others(self):
        refs = ["master", "history/master", "stable/linux-3.0.y", "topic/x"]
        kept, delegated = distributed.partition_master_refs(
            refs, ["v3.0"], delegate_branches=["stable/linux-3.0.y", "master"])
        self.assertEqual(kept, ["master", "history/master", "topic/x"])
        self.assertEqual(delegated, ["stable/linux-3.0.y"])

    def test_delegated_branches_none_discovers_and_empty_list_delegates_none(self):
        with unittest.mock.patch("core.config.find_repository_branches", return_value=["master", "stable/linux-3.0.y"]):
            self.assertEqual(distributed.delegated_branches(None, "repo"), ["stable/linux-3.0.y"])
            self.assertEqual(distributed.delegated_branches([], "repo"), [])
            self.assertEqual(distributed.delegated_branches(["x"], "repo"), ["x"])
        self.assertEqual(distributed.delegated_branches(None, None), [])

    def test_version_base_is_the_previous_version(self):
        """The base of the DAG walk, of the tree diff and of m_ingest_state is the version parsed last."""
        from main import G, gp
        from pipeline import git_ingest

        saved = {k: getattr(gp, k, None) for k in ("VID", "Version_Name", "Old_VID", "Old_Version_Name", "_repair_base_set")}
        orig_role = getattr(G, "distributed_role", None)
        try:
            G.distributed_role = "standalone"
            gp._repair_base_set = False
            # First version of a fresh run: no base
            gp.VID, gp.Version_Name, gp.Old_VID, gp.Old_Version_Name = 0, git_ingest.EMPTY_TREE, 0, 0
            self.assertEqual(git_ingest._version_base(), (0, git_ingest.EMPTY_TREE, False))
            # Second version: the first one, although gp.Old_* still describes the version before it
            gp.VID, gp.Version_Name, gp.Old_VID, gp.Old_Version_Name = 10, "v3.0", 0, git_ingest.EMPTY_TREE
            self.assertEqual(git_ingest._version_base(), (10, "v3.0", False))
            gp.VID, gp.Version_Name, gp.Old_VID, gp.Old_Version_Name = 20, "v3.1-rc1", 10, "v3.0"
            self.assertEqual(git_ingest._version_base(), (20, "v3.1-rc1", False))
            # Resumed run / mainline bridge: the caller's base is kept for the first version of the process
            gp.VID, gp.Version_Name, gp.Old_VID, gp.Old_Version_Name = 0, git_ingest.EMPTY_TREE, 20, "v3.1-rc1"
            self.assertEqual(git_ingest._version_base(), (20, "v3.1-rc1", True))
            # Repair step and worker task: always the caller's base
            gp.VID, gp.Version_Name, gp.Old_VID, gp.Old_Version_Name = 30, "v3.1-rc2", 10, "v3.0"
            gp._repair_base_set = True
            self.assertEqual(git_ingest._version_base(), (10, "v3.0", True))
            gp._repair_base_set = False
            G.distributed_role = distributed.ROLE_WORKER
            self.assertEqual(git_ingest._version_base(), (10, "v3.0", True))
        finally:
            G.distributed_role = orig_role
            for key, value in saved.items():
                if value is None and key == "_repair_base_set":
                    if hasattr(gp, key):
                        delattr(gp, key)
                else:
                    setattr(gp, key, value)

    def test_ingest_git_dag_walks_from_the_previous_version(self):
        """A sequential run walks previous..version and records the previous version as the ingest base."""
        from main import G, gp
        from pipeline.git_ingest import ingest_git_dag
        from core.DBLayout import m_commit

        mock_db = MagicMock()
        mock_db.select.return_value = None
        mock_db.reserve_id_block.side_effect = lambda table, count: (777, 777) if table == m_commit else (888, 888)
        saved = {k: getattr(gp, k, None) for k in ("VID", "Version_Name", "Old_VID", "Old_Version_Name")}
        orig = (getattr(G, "distributed_role", None), getattr(G, "distributed", False), G.DB)
        try:
            G.distributed, G.distributed_role = True, distributed.ROLE_MASTER
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: mock_db, __exit__=lambda s, e, t, b: None)
            gp.VID, gp.Version_Name, gp.Old_VID, gp.Old_Version_Name = 500, "v3.0", 0, "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
            mock_git_parser = MagicMock()
            mock_git_parser.parse_version_commits.return_value = []
            with unittest.mock.patch("parser.git_ast.GitCommitParser", return_value=mock_git_parser), \
                 unittest.mock.patch("core.ingest_state.begin") as mock_begin:
                ingest_git_dag("v3.1-rc1", 1, 1, "repo")
            self.assertEqual(mock_git_parser.parse_version_commits.call_args.kwargs["old_rev"], "v3.0")
            self.assertEqual(mock_begin.call_args.kwargs["base_commit_id"], 500)
            self.assertEqual((gp.Old_VID, gp.Old_Version_Name, gp.VID, gp.Version_Name), (500, "v3.0", 777, "v3.1-rc1"))
        finally:
            G.distributed_role, G.distributed, G.DB = orig
            for key, value in saved.items():
                setattr(gp, key, value)

    def test_mark_task_failed_cid_zero_does_not_cascade_unrelated(self):
        """Verify mark_task_failed when cid==0 does not match or fail tasks with base_commit_id=0."""
        db = DistDB().add_tasks(
            {"task_id": 1, "retry_count": 2, "status": distributed.TASK_LEASED, "commit_id": 0, "ref_name": "v3.0.1"},
            {"task_id": 2, "retry_count": 0, "status": distributed.TASK_PENDING, "commit_id": 0,
             "ref_name": "v3.0.2", "base_ref_name": "v3.0.1", "base_commit_id": 0},
            {"task_id": 3, "retry_count": 0, "status": distributed.TASK_PENDING, "commit_id": 0,
             "ref_name": "v3.1.1", "base_ref_name": "v3.1", "base_commit_id": 0},
        )

        # Task 1 fails (3rd failure -> TASK_FAILED) with cid=0
        distributed.mark_task_failed(db, task_id=1, error_msg="Compilation failure")
        self.assertEqual(db.task(1)["status"], distributed.TASK_FAILED)
        # Downstream task 2 matching base_ref_name "v3.0.1" should fail
        self.assertEqual(db.task(2)["status"], distributed.TASK_FAILED)
        # Unrelated task 3 with base_commit_id 0 and different base_ref_name MUST NOT fail!
        self.assertEqual(db.task(3)["status"], distributed.TASK_PENDING)

    def test_unlock_dependent_tasks_cid_zero_does_not_unlock_unrelated(self):
        """Verify unlock_dependent_tasks with base_commit_id=0 does not unlock unrelated tasks with base_commit_id=0."""
        db = DistDB().add_tasks(
            {"task_id": 1, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 0, "status": distributed.TASK_PENDING},
            {"task_id": 2, "ref_name": "v3.1.1", "base_ref_name": "v3.1", "base_commit_id": 0, "status": distributed.TASK_PENDING},
        )

        count = distributed.unlock_dependent_tasks(db, network_id=1, base_commit_id=0, base_ref_name="v3.0")
        self.assertEqual(count, 1)
        self.assertEqual(db.task(1)["status"], distributed.TASK_ELIGIBLE)
        # Unrelated task 2 MUST remain TASK_PENDING
        self.assertEqual(db.task(2)["status"], distributed.TASK_PENDING)

    def test_update_node_heartbeat_upserts_dropped_node(self):
        """Verify update_node_heartbeat re-registers a node that is not in m_parse_node, and renews its lease."""
        db = DistDB().add_tasks(
            {"task_id": 9, "worker_id": "worker-test:123", "status": distributed.TASK_LEASED, "lease_expires_at": 5}
        )

        distributed.update_node_heartbeat(db, node_id="worker-test:123", active_commit_id=42, current_task_id=9, lease_ttl=100)

        node = db.select_one("m_parse_node", ["node_id", "active_commit_id", "role"], {"node_id": "worker-test:123"}, as_dict=True)
        self.assertEqual((node["active_commit_id"], node["role"]), (42, "worker"))
        self.assertGreater(db.task(9)["lease_expires_at"], int(time.time()) + 90)

        # A second beat updates the row it now has
        distributed.update_node_heartbeat(db, node_id="worker-test:123", active_commit_id=43)
        self.assertEqual(db.count_rows("m_parse_node"), 1)
        self.assertEqual(db.select_value("m_parse_node", "active_commit_id", {"node_id": "worker-test:123"}), 43)

        # allow_insert=False never re-creates a dropped node
        distributed.unregister_node(db, "worker-test:123")
        distributed.update_node_heartbeat(db, node_id="worker-test:123", allow_insert=False)
        self.assertEqual(db.count_rows("m_parse_node"), 0)

    def test_reclaim_expired_leases_locks_with_reclaiming_worker(self):
        """Verify reclaim_expired_leases takes the task over as 'reclaiming' and requeues it after the rollback."""
        db = DistDB().add_tasks(
            {"task_id": 1, "commit_id": 10, "worker_id": "dead-node", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "lease_expires_at": 50, "network_id": 1, "retry_count": 0}
        )

        seen_during_rollback = []

        def rollback(db_arg, net, cid, tables):
            seen_during_rollback.append(db.task(1))
            return True

        with unittest.mock.patch("core.distributed.rollback_version", side_effect=rollback) as mock_rollback:
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 1)
            mock_rollback.assert_called_once_with(db, 1, 10, TABLES)

        locked = seen_during_rollback[0]
        self.assertEqual((locked["status"], locked["worker_id"]), (distributed.TASK_RECLAIMING, "reclaiming"))
        # Task retry_count was 0 (< 3), so final status from mark_task_failed is TASK_ELIGIBLE, NOT TASK_FAILED
        t1 = db.task(1)
        self.assertEqual(t1["status"], distributed.TASK_ELIGIBLE)
        self.assertEqual(t1["retry_count"], 1)
        self.assertEqual(t1["worker_id"], "")

    def test_publish_delegable_chains_preserves_task_eligible_query(self):
        """Publishing again leaves a task another node leased alone, and keeps what is eligible eligible."""
        db = DistDB()
        all_tags = ["v3.0", "v3.0.1", "v3.0.2"]
        version_states = {"v3.0": (10, "done"), "v3.0.1": (0, "todo"), "v3.0.2": (0, "todo")}
        self.assertEqual(distributed.publish_delegable_chains(db, network_id=1, repo_id=1, all_tags=all_tags, version_states=version_states), 2)
        self.assertEqual({t["ref_name"]: t["status"] for t in db.tasks},
                         {"v3.0.1": distributed.TASK_ELIGIBLE, "v3.0.2": distributed.TASK_PENDING})

        # A worker leases v3.0.1; the next publication must not hand it back
        task_id = next(t["task_id"] for t in db.tasks if t["ref_name"] == "v3.0.1")
        db.update_rows("m_parse_task", {"status": distributed.TASK_LEASED, "worker_id": "w1"}, {"task_id": task_id})
        distributed.publish_delegable_chains(db, network_id=1, repo_id=1, all_tags=all_tags, version_states=version_states)
        self.assertEqual((db.task(task_id)["status"], db.task(task_id)["worker_id"]), (distributed.TASK_LEASED, "w1"))

    def test_worker_old_vid_preserved_through_ingest_git_dag(self):
        """Verify worker gp.Old_VID and gp.Old_Version_Name are preserved through ingest_git_dag."""
        from main import G, gp
        from pipeline.git_ingest import ingest_git_dag
        from core.DBLayout import m_commit, m_git_ref

        mock_db = MagicMock()
        mock_db.select.return_value = None
        mock_db.reserve_id_block.side_effect = lambda table, count: (777, 777) if table == m_commit else (888, 888)

        orig_role = getattr(G, "distributed_role", None)
        orig_dist = getattr(G, "distributed", False)
        orig_node = getattr(G, "node_id", None)
        orig_old_vid = getattr(gp, "Old_VID", None)
        orig_vid = getattr(gp, "VID", None)
        orig_old_vname = getattr(gp, "Old_Version_Name", None)
        orig_vname = getattr(gp, "Version_Name", None)
        orig_repair = getattr(gp, "_repair_base_set", None)
        orig_db_ctx = G.DB

        try:
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: mock_db, __exit__=lambda s, e, t, b: None)
            mock_git_parser = MagicMock()
            mock_git_parser.get_commit_hash.return_value = "0" * 40
            mock_git_parser.parse_version_commits.return_value = []

            # 1. Secondary worker mode: Old_VID and Old_Version_Name are preserved from claimed task's base
            G.distributed = True
            G.distributed_role = distributed.ROLE_WORKER
            G.node_id = "worker-node-1"
            gp._repair_base_set = False
            gp.Old_VID = 300
            gp.Old_Version_Name = "v3.0"
            gp.VID = 100
            gp.Version_Name = "v2.6"

            with unittest.mock.patch("parser.git_ast.GitCommitParser", return_value=mock_git_parser), \
                 unittest.mock.patch("core.ingest_state.begin"), \
                 unittest.mock.patch("core.ingest_state.fetch_repo_version_states", return_value={}):
                cid, _, _ = ingest_git_dag("v3.0.1", 1, 1, "repo")

            self.assertEqual(cid, 777)
            self.assertEqual(gp.Old_VID, 300)
            self.assertEqual(gp.Old_Version_Name, "v3.0")
            self.assertEqual(gp.VID, 777)
            self.assertEqual(gp.Version_Name, "v3.0.1")

            # 2. Preset repair run: Old_VID preserved when _repair_base_set is True
            G.distributed_role = "standalone"
            gp._repair_base_set = True
            gp.Old_VID = 400
            gp.Old_Version_Name = "v3.1"
            gp.VID = 200
            gp.Version_Name = "v3.0"

            with unittest.mock.patch("parser.git_ast.GitCommitParser", return_value=mock_git_parser), \
                 unittest.mock.patch("core.ingest_state.begin"), \
                 unittest.mock.patch("core.ingest_state.fetch_repo_version_states", return_value={}):
                cid2, _, _ = ingest_git_dag("v3.1.1", 1, 1, "repo")

            self.assertEqual(gp.Old_VID, 400)
            self.assertEqual(gp.Old_Version_Name, "v3.1")
            self.assertEqual(gp.VID, 777)
            self.assertEqual(gp.Version_Name, "v3.1.1")

            # 3. Normal standalone run: Old_VID shifts from VID
            gp._repair_base_set = False
            gp.VID = 500
            gp.Version_Name = "v3.2"

            with unittest.mock.patch("parser.git_ast.GitCommitParser", return_value=mock_git_parser), \
                 unittest.mock.patch("core.ingest_state.begin"), \
                 unittest.mock.patch("core.ingest_state.fetch_repo_version_states", return_value={}):
                cid3, _, _ = ingest_git_dag("v3.2.1", 1, 1, "repo")

            self.assertEqual(gp.Old_VID, 500)
            self.assertEqual(gp.Old_Version_Name, "v3.2")
            self.assertEqual(gp.VID, 777)
            self.assertEqual(gp.Version_Name, "v3.2.1")
        finally:
            G.distributed_role = orig_role
            G.distributed = orig_dist
            G.node_id = orig_node
            gp.Old_VID = orig_old_vid
            gp.VID = orig_vid
            gp.Old_Version_Name = orig_old_vname
            gp.Version_Name = orig_vname
            if orig_repair is None:
                if hasattr(gp, "_repair_base_set"):
                    delattr(gp, "_repair_base_set")
            else:
                gp._repair_base_set = orig_repair
            G.DB = orig_db_ctx

    def test_finish_version_ingest_aborts_when_worker_lease_lost(self):
        """Verify finish_version_ingest aborts when worker lease is lost/expired."""
        from main import G, gp
        from pipeline.git_ingest import finish_version_ingest

        db = DistDB()

        orig_role = getattr(G, "distributed_role", None)
        orig_dist = getattr(G, "distributed", False)
        orig_node = getattr(G, "node_id", None)
        orig_active = getattr(gp, "_active_task_id", None)
        orig_db_ctx = G.DB

        try:
            G.distributed = True
            G.distributed_role = distributed.ROLE_WORKER
            G.node_id = "worker-node-1"
            gp._active_task_id = 42
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: db, __exit__=lambda s, e, t, b: None)

            # Lease expired in m_parse_task
            db.add_tasks({"task_id": 42, "worker_id": "worker-node-1", "status": distributed.TASK_LEASED,
                          "lease_expires_at": int(time.time()) - 10})

            with unittest.mock.patch("core.ingest_state.set_status") as mock_set_status:
                with self.assertRaises(RuntimeError) as ctx:
                    finish_version_ingest(999)
                self.assertIn("Worker lease for task 42 expired or was revoked", str(ctx.exception))
                mock_set_status.assert_not_called()

            # Now with active valid lease: should succeed
            db.update_rows("m_parse_task", {"lease_expires_at": int(time.time()) + 300}, {"task_id": 42})
            with unittest.mock.patch("core.ingest_state.set_status") as mock_set_status, \
                 unittest.mock.patch("pipeline.git_ingest.refresh_person_identities"), \
                 unittest.mock.patch("pipeline.git_ingest._compact_checkpoint"):
                finish_version_ingest(999)
                mock_set_status.assert_called_once_with(db, 1, 999, ingest_state.INGEST_DONE)
        finally:
            G.distributed_role = orig_role
            G.distributed = orig_dist
            G.node_id = orig_node
            gp._active_task_id = orig_active
            G.DB = orig_db_ctx

    def test_finish_version_ingest_worker_role_skips_unlock_dependent_tasks(self):
        """Verify finish_version_ingest does not call unlock_dependent_tasks when distributed_role is worker."""
        from main import G, gp
        from pipeline.git_ingest import finish_version_ingest
        orig_role = getattr(G, "distributed_role", None)
        orig_dist = getattr(G, "distributed", False)
        orig_node = getattr(G, "node_id", None)
        orig_active = getattr(gp, "_active_task_id", None)
        orig_db_ctx = G.DB

        db = DistDB().add_tasks(
            {"task_id": 42, "worker_id": "worker-1", "status": distributed.TASK_LEASED,
             "lease_expires_at": int(time.time()) + 300}
        )

        try:
            G.distributed = True
            G.distributed_role = distributed.ROLE_WORKER
            G.node_id = "worker-1"
            gp._active_task_id = 42
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: db, __exit__=lambda s, e, t, b: None)

            with unittest.mock.patch("core.ingest_state.set_status"), \
                 unittest.mock.patch("pipeline.git_ingest.refresh_person_identities"), \
                 unittest.mock.patch("pipeline.git_ingest._compact_checkpoint"), \
                 unittest.mock.patch("core.distributed.unlock_dependent_tasks") as mock_unlock:
                finish_version_ingest(999)
                mock_unlock.assert_not_called()

            # For master role, unlock_dependent_tasks should be called
            G.distributed_role = distributed.ROLE_MASTER
            gp._active_task_id = None
            with unittest.mock.patch("core.ingest_state.set_status"), \
                 unittest.mock.patch("pipeline.git_ingest.refresh_person_identities"), \
                 unittest.mock.patch("pipeline.git_ingest._compact_checkpoint"), \
                 unittest.mock.patch("core.distributed.unlock_dependent_tasks") as mock_unlock:
                finish_version_ingest(999)
                mock_unlock.assert_called_once()
        finally:
            G.distributed_role = orig_role
            G.distributed = orig_dist
            G.node_id = orig_node
            gp._active_task_id = orig_active
            G.DB = orig_db_ctx

    def test_ingest_commit_range_id_block_leasing_in_distributed_mode(self):
        """Verify _ingest_commit_range uses block allocation via reserve_id_block in distributed mode."""
        from main import G, gp
        from pipeline.git_ingest import _ingest_commit_range
        orig_dist = getattr(G, "distributed", False)
        orig_vid = getattr(gp, "VID", None)

        try:
            G.distributed = True
            gp.VID = 77

            mock_db = mock.MagicMock()
            # No stored person matches, so the author needs a new m_maintainer_person id
            mock_db.query.return_value = []
            mock_db.select.return_value = None

            # Reserve blocks: for m_commit return (1001, 2000), for m_maintainer_person return (501, 756)
            def mock_reserve(table, block_size, commit_id=0):
                if table.table_name == "m_commit":
                    self.assertEqual(block_size, 1000)
                    self.assertEqual(commit_id, 77)
                    return (1001, 2000)
                elif table.table_name == "m_maintainer_person":
                    self.assertEqual(block_size, 256)
                    self.assertEqual(commit_id, 77)
                    return (501, 756)
                return (1, 10)

            mock_db.reserve_id_block.side_effect = mock_reserve

            c1 = mock.MagicMock()
            c1.commit_hash = "1" * 40
            c1.author_name = "Dev One"
            c1.author_email = "one@kernel.org"
            c1.committer_name = "Dev One"
            c1.committer_email = "one@kernel.org"
            c1.author_date = 100
            c1.committer_date = 100
            c1.subject = "Commit 1"
            c1.message = "Msg 1"
            c1.parents = ()
            c1.contributors = []

            with mock.patch("pipeline.git_ingest._lookup_commit_ids", return_value={}):
                res = _ingest_commit_range(mock_db, [c1], network_id=1)

            self.assertEqual(res["1" * 40], 1001)
            self.assertEqual(mock_db.reserve_id_block.call_count, 2)
            mock_db.get_next_id.assert_not_called()

            # In non-distributed mode, fallback to db.get_next_id
            G.distributed = False
            mock_db.reset_mock()
            mock_db.get_next_id.return_value = 42
            with mock.patch("pipeline.git_ingest._lookup_commit_ids", return_value={}):
                res2 = _ingest_commit_range(mock_db, [c1], network_id=1)
            mock_db.reserve_id_block.assert_not_called()
            self.assertTrue(mock_db.get_next_id.called)
        finally:
            G.distributed = orig_dist
            gp.VID = orig_vid

    def test_previous_ingest_active_worker_detects_in_progress_and_avoids_rollback(self):
        """Verify _previous_ingest detects active heartbeating worker and avoids rollback."""
        from main import G, gp
        from pipeline.git_ingest import _previous_ingest, ingest_git_dag
        orig_dist = getattr(G, "distributed", False)

        try:
            G.distributed = True
            mock_db = mock.MagicMock()
            real_db = DistDB().add_nodes({"node_id": "w1", "heartbeat_at": int(time.time())})
            real_db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at", "base_commit_id", "worker_id"],
                                [(1, 100, ingest_state.INGEST_PARSING, 0, 0, "w1")])

            # 1. When status is INGEST_PARSING and the owning worker heartbeats, the ingestion is in progress
            with mock.patch("core.ingest_state.get_status", return_value=ingest_state.INGEST_PARSING):
                state = _previous_ingest(real_db, network_id=1, commit_id=100, version_or_ref="v3.0.1")
                self.assertEqual(state, "in_progress")

                # The worker's heartbeat went stale
                real_db.update_rows("m_parse_node", {"heartbeat_at": int(time.time()) - 3600}, {"node_id": "w1"})
                state_inactive = _previous_ingest(real_db, network_id=1, commit_id=100, version_or_ref="v3.0.1")
                self.assertEqual(state_inactive, "interrupted")

            # 2. In ingest_git_dag, verify state == 'in_progress' skips rollback
            orig_db_ctx = G.DB
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: mock_db, __exit__=lambda s, e, t, b: None)
            try:
                with mock.patch("pipeline.git_ingest._previous_ingest", return_value="in_progress"), \
                     mock.patch("core.ingest_state.rollback_version") as mock_rollback, \
                     mock.patch("pipeline.git_ingest._purge_partial_version") as mock_purge, \
                     mock.patch("subprocess.run") as mock_subproc, \
                     mock.patch("parser.git_ast.GitCommitParser") as mock_git_parser:
                    mock_subproc.return_value = mock.MagicMock(stdout="40-hex-hash\n", returncode=0)
                    mock_db.select.side_effect = lambda table, pattern: (100, 1) if table.table_name == "m_commit" else None

                    with mock.patch("pipeline.git_ingest.TABLES", []):
                        ingest_git_dag("v3.0.1", network_id=1, repo_id=1, repo_path="/dummy")

                    mock_rollback.assert_not_called()
                    mock_purge.assert_not_called()
            finally:
                G.DB = orig_db_ctx
        finally:
            G.distributed = orig_dist

    def test_publish_delegable_chains_preserves_nonzero_commit_ids(self):
        """Republishing a task whose commit ids are not known yet keeps the ids a worker recorded."""
        db = DistDB()
        all_tags = ["v3.0", "v3.0.1"]
        distributed.publish_delegable_chains(db, network_id=1, repo_id=1, all_tags=all_tags,
                                             version_states={"v3.0": (10, "done"), "v3.0.1": (11, "todo")})
        self.assertEqual((db.tasks[0]["commit_id"], db.tasks[0]["base_commit_id"]), (11, 10))

        # The master has no ids this time (0): the stored ones stay
        distributed.publish_delegable_chains(db, network_id=1, repo_id=1, all_tags=all_tags,
                                             version_states={"v3.0": (0, "done"), "v3.0.1": (0, "todo")})
        self.assertEqual((db.tasks[0]["commit_id"], db.tasks[0]["base_commit_id"]), (11, 10))
        # A new known id replaces the old one
        distributed.publish_delegable_chains(db, network_id=1, repo_id=1, all_tags=all_tags,
                                             version_states={"v3.0": (10, "done"), "v3.0.1": (12, "todo")})
        self.assertEqual(db.tasks[0]["commit_id"], 12)

    def test_claim_next_task_requeries_fresh_values(self):
        """Verify claim_next_task returns the ids stored with the claimed row, not the scan's snapshot."""
        db = DistDB().add_tasks({
            "task_id": 10, "chain_name": "stable:v3.0", "chain_kind": "stable", "seq_order": 1, "ref_name": "v3.0.1",
            "commit_id": 0, "base_ref_name": "v3.0", "base_commit_id": 0, "status": distributed.TASK_ELIGIBLE,
        })
        real_select_rows = db.select_rows

        def select_rows_then_publish(*args, **kwargs):
            rows = real_select_rows(*args, **kwargs)
            if args and args[0] == "m_parse_task" and kwargs.get("limit") in (5, 2000):
                # the master publishes the real ids between the scan and the claim
                db.update_rows("m_parse_task", {"commit_id": 101, "base_commit_id": 99}, {"task_id": 10})
            return rows

        with unittest.mock.patch.object(db, "select_rows", side_effect=select_rows_then_publish):
            claimed = distributed.claim_next_task(db, network_id=1, worker_id="worker-fresh-test")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["task_id"], 10)
        self.assertEqual(claimed["commit_id"], 101)
        self.assertEqual(claimed["base_commit_id"], 99)
        self.assertEqual(claimed["base_ref_name"], "v3.0")

    def test_publish_delegable_chains_resets_task_failed(self):
        """Republishing resets a FAILED task (status as published, retries and error cleared) and a DONE one that is no longer done."""
        db = DistDB()
        all_tags = ["v3.0", "v3.0.1", "v3.0.2"]
        states = {"v3.0": (10, "done"), "v3.0.1": (11, "todo"), "v3.0.2": (12, "todo")}
        distributed.publish_delegable_chains(db, network_id=1, repo_id=1, all_tags=all_tags, version_states=states)
        first = next(t["task_id"] for t in db.tasks if t["ref_name"] == "v3.0.1")
        second = next(t["task_id"] for t in db.tasks if t["ref_name"] == "v3.0.2")
        db.update_rows("m_parse_task", {"status": distributed.TASK_FAILED, "retry_count": 3, "error_msg": "boom"}, {"task_id": first})
        db.update_rows("m_parse_task", {"status": distributed.TASK_DONE, "retry_count": 1, "error_msg": "old"}, {"task_id": second})

        count = distributed.publish_delegable_chains(db, network_id=1, repo_id=1, all_tags=all_tags, version_states=states)
        self.assertEqual(count, 2)
        t1, t2 = db.task(first), db.task(second)
        self.assertEqual((t1["status"], t1["retry_count"], t1["error_msg"]), (distributed.TASK_ELIGIBLE, 0, ""))
        self.assertEqual((t2["status"], t2["retry_count"], t2["error_msg"]), (distributed.TASK_PENDING, 0, ""))

        # A task that finished (as the version states say) stays DONE and keeps its history
        done_states = {**states, "v3.0.1": (11, "done")}
        db.update_rows("m_parse_task", {"retry_count": 2}, {"task_id": first})
        distributed.publish_delegable_chains(db, network_id=1, repo_id=1, all_tags=all_tags, version_states=done_states)
        self.assertEqual((db.task(first)["status"], db.task(first)["retry_count"]), (distributed.TASK_DONE, 2))

    def test_mark_task_failed_cascades_to_eligible_tasks(self):
        """Verify cascading failure marks both TASK_PENDING and TASK_ELIGIBLE dependent tasks as TASK_FAILED."""
        db = DistDB().add_tasks(
            {"task_id": 1, "retry_count": 2, "status": distributed.TASK_LEASED, "commit_id": 11, "ref_name": "v3.0.1"},
            {"task_id": 2, "retry_count": 0, "status": distributed.TASK_PENDING, "commit_id": 0,
             "ref_name": "v3.0.2", "base_ref_name": "v3.0.1", "base_commit_id": 0},
            {"task_id": 3, "retry_count": 0, "status": distributed.TASK_ELIGIBLE, "commit_id": 0,
             "ref_name": "v3.0.3", "base_ref_name": "v3.0.1", "base_commit_id": 11},
            {"task_id": 4, "retry_count": 0, "status": distributed.TASK_LEASED, "commit_id": 0,
             "ref_name": "v3.0.4", "base_ref_name": "v3.0.1", "base_commit_id": 11, "worker_id": "w9"},
        )

        distributed.mark_task_failed(db, task_id=1, error_msg="Fatal bug")
        self.assertEqual(db.task(1)["status"], distributed.TASK_FAILED)
        self.assertEqual(db.task(2)["status"], distributed.TASK_FAILED)
        self.assertEqual(db.task(2)["error_msg"], "Prerequisite base task failed")
        self.assertEqual(db.task(3)["status"], distributed.TASK_FAILED)
        self.assertEqual(db.task(3)["error_msg"], "Prerequisite base task failed")
        self.assertEqual(db.task(4)["status"], distributed.TASK_LEASED, "a task a worker already runs is not cancelled")

    def test_unlock_dependent_tasks_scoped_by_network(self):
        """Verify unlock_dependent_tasks scopes the m_git_ref lookup by network_id and prefers tags over branches."""
        db = DistDB().add_git_refs(
            {"network_id": 1, "commit_id": 10, "ref_type": 2, "ref_name": "refs/heads/v3.0-branch"},
            {"network_id": 1, "commit_id": 10, "ref_type": 1, "ref_name": "v3.0"},
            {"network_id": 2, "commit_id": 10, "ref_type": 1, "ref_name": "v3.0-net2"},
        ).add_tasks(
            {"task_id": 1, "retry_count": 0, "status": distributed.TASK_PENDING, "commit_id": 0,
             "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 0, "network_id": 1},
            {"task_id": 2, "status": distributed.TASK_PENDING, "ref_name": "v3.0.1", "base_ref_name": "v3.0-net2",
             "network_id": 2},
        )

        count = distributed.unlock_dependent_tasks(db, network_id=1, base_commit_id=10, base_ref_name=None)
        self.assertEqual(count, 1)
        self.assertEqual(db.task(1)["status"], distributed.TASK_ELIGIBLE)
        self.assertEqual(db.task(1)["base_commit_id"], 10)
        self.assertEqual(db.task(2)["status"], distributed.TASK_PENDING, "the other network's task is untouched")

    def test_reclaim_expired_leases_scoped_by_network(self):
        """Verify reclaim_expired_leases scopes the m_git_ref commit lookup by network_id."""
        db = DistDB().add_tasks(
            {"task_id": 7, "commit_id": 0, "worker_id": "dead-worker", "ref_name": "v3.0.7",
             "network_id": 1, "status": distributed.TASK_LEASED, "lease_expires_at": 100}
        ).add_git_refs(
            {"network_id": 2, "ref_name": "v3.0.7", "commit_id": 999},
            {"network_id": 1, "ref_name": "v3.0.7", "commit_id": 42},
        )

        with unittest.mock.patch("core.distributed.rollback_version") as mock_rollback:
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 1)
            mock_rollback.assert_called_once_with(db, 1, 42, TABLES)

    def test_is_checkpoint_pinned_matches_base_ref_name(self):
        """Verify is_checkpoint_pinned pins when base_ref_name matches via m_git_ref."""
        db = DistDB().add_tasks(
            {"task_id": 1, "base_commit_id": 0, "base_ref_name": "v3.0", "status": distributed.TASK_ELIGIBLE}
        ).add_git_refs({"commit_id": 100, "ref_name": "v3.0"})

        self.assertTrue(checkpoints.is_checkpoint_pinned(db, 100))
        self.assertFalse(checkpoints.is_checkpoint_pinned(db, 999))

        db.update_rows("m_parse_task", {"status": distributed.TASK_DONE}, {"task_id": 1})
        self.assertFalse(checkpoints.is_checkpoint_pinned(db, 100))

    def test_worker_error_handler_rolls_back_before_marking_failed(self):
        """Verify worker error handler rolls back partial rows before marking task failed."""
        from main import main, G, gp
        orig_role = getattr(G, "distributed_role", None)
        orig_dist = getattr(G, "distributed", False)
        orig_node = getattr(G, "node_id", None)
        orig_db_ctx = G.DB

        db = DistDB(full_schema=True).add_tasks(
            {"task_id": 42, "status": distributed.TASK_LEASED,
             "worker_id": distributed.get_default_node_id("worker", "test-worker-1"), "lease_expires_at": 2000000000}
        )

        task = {
            "task_id": 42,
            "ref_name": "v3.0.1",
            "base_ref_name": "v3.0",
            "base_commit_id": 10,
            "commit_id": 101,
            "chain_name": "stable:v3.0",
        }

        call_order = []

        def mock_rollback(cnx, net_id, vid, tables):
            call_order.append(("rollback", vid))
            return True

        def mock_mark_failed(db, task_id, msg, *args, **kwargs):
            call_order.append(("mark_failed", task_id))

        mock_args = mock.MagicMock()
        mock_args.role = "worker"
        mock_args.worker_id = "test-worker-1"
        mock_args.worker_once = True
        mock_args.repo = "linux"
        mock_args.ref = ["v3.0.1"]
        mock_args.start_version = None
        mock_args.end_version = None
        mock_args.single_version = True
        mock_args.include_pre_releases = None
        mock_args.deadline_minutes = 0
        mock_args.mem_max = 16.0
        mock_args.reset = False
        mock_args.Drop = False
        mock_args.cve = False
        mock_args.clean_temp = False
        mock_args.resolve_ml = False
        mock_args.branches = False
        mock_args.config = None
        mock_args.detect_repos = False
        mock_args.chain = None
        mock_args.delegate_stable = False
        mock_args.delegate_branch = None
        mock_args.unit_tests = False

        def mock_update(ref_name, repo_path=None):
            gp.VID = 101
            raise RuntimeError("Synthetic parsing failure in worker")

        with mock.patch("main.arg_handling", return_value=mock_args), \
             mock.patch("main.setup_memory_limit"), \
             mock.patch("main._apply_db_server_tuning"), \
             mock.patch("main.get_mailing_list_config", return_value={"enabled": False}), \
             mock.patch("main.resolve_git_network_and_repo", return_value=(1, 1)), \
             mock.patch("core.ingest_state.fetch_repo_version_states", return_value={}), \
             mock.patch("core.distributed.register_node"), \
             mock.patch("core.distributed.NodeHeartbeatDaemon"), \
             mock.patch("core.distributed.reclaim_expired_leases"), \
             mock.patch("core.distributed.claim_next_task", side_effect=[task, None]), \
             mock.patch("main.update", side_effect=mock_update), \
             mock.patch("core.ingest_state.rollback_version", side_effect=mock_rollback), \
             mock.patch("core.distributed.mark_task_failed", side_effect=mock_mark_failed):
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: db, __exit__=lambda s, e, t, b: None)
            try:
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 0)
            finally:
                G.distributed_role = orig_role
                G.distributed = orig_dist
                G.node_id = orig_node
                G.DB = orig_db_ctx

        self.assertEqual(len(call_order), 2)
        self.assertEqual(call_order[0], ("rollback", 101))
        self.assertEqual(call_order[1], ("mark_failed", 42))

    def test_worker_systemic_error_releases_task_and_stops(self):
        """A SystemicParseError rolls the version back, releases the task unpenalized and stops the worker."""
        from main import G, gp, main
        from pipeline.systemic import SystemicParseError
        saved = (getattr(G, "distributed_role", None), getattr(G, "distributed", False), getattr(G, "node_id", None), G.DB)

        db = DistDB(full_schema=True).add_tasks(
            {"task_id": 42, "status": distributed.TASK_LEASED,
             "worker_id": distributed.get_default_node_id("worker", "test-worker-1"), "lease_expires_at": 2000000000}
        )
        task = {"task_id": 42, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 10,
                "commit_id": 101, "chain_name": "stable:v3.0"}
        second = dict(task, task_id=43, ref_name="v3.0.2")
        call_order = []

        def mock_update(ref_name, repo_path=None):
            call_order.append(("update", ref_name))
            gp.VID = 101
            raise SystemicParseError("63 of 82 files failed with the same error")

        mock_args = mock.MagicMock()
        mock_args.role = "worker"
        mock_args.worker_id = "test-worker-1"
        mock_args.worker_once = False
        mock_args.repo = "linux"
        mock_args.ref = ["v3.0.1"]
        mock_args.single_version = True
        mock_args.deadline_minutes = 0
        mock_args.mem_max = 16.0
        mock_args.config = None
        mock_args.unit_tests = False
        for flag in ("reset", "Drop", "cve", "clean_temp", "resolve_ml", "branches", "detect_repos", "delegate_stable"):
            setattr(mock_args, flag, False)

        with mock.patch("main.arg_handling", return_value=mock_args), \
             mock.patch("main.setup_memory_limit"), \
             mock.patch("main._apply_db_server_tuning"), \
             mock.patch("main.get_mailing_list_config", return_value={"enabled": False}), \
             mock.patch("main.resolve_git_network_and_repo", return_value=(1, 1)), \
             mock.patch("core.ingest_state.fetch_repo_version_states", return_value={}), \
             mock.patch("core.distributed.register_node"), \
             mock.patch("core.distributed.NodeHeartbeatDaemon"), \
             mock.patch("core.distributed.reclaim_expired_leases"), \
             mock.patch("core.distributed.claim_next_task", side_effect=[task, second, None]), \
             mock.patch("main.update", side_effect=mock_update), \
             mock.patch("core.ingest_state.rollback_version",
                        side_effect=lambda d, n, vid, t: call_order.append(("rollback", vid)) or True), \
             mock.patch("core.distributed.release_task",
                        side_effect=lambda d, t_id, reason, w: call_order.append(("release", t_id, w)) or True), \
             mock.patch("core.distributed.mark_task_failed") as mark_failed:
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: db, __exit__=lambda s, e, t, b: None)
            try:
                with self.assertRaises(SystemExit) as ctx:
                    main()
            finally:
                G.distributed_role, G.distributed, G.node_id, G.DB = saved

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(call_order[:2], [("update", "v3.0.1"), ("rollback", 101)])
        self.assertEqual(len(call_order), 3)
        self.assertEqual(call_order[2][:2], ("release", 42))
        self.assertTrue(call_order[2][2].startswith("test-worker-1"))
        mark_failed.assert_not_called()

    def test_claim_prefers_the_oldest_release_across_chains(self):
        """Eligible chains are claimed in release order, not in text order of their names."""
        elig = distributed.TASK_ELIGIBLE
        db = DistDB().add_tasks(
            {"task_id": 1, "chain_name": "stable:v3.0", "chain_kind": "stable", "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 5, "status": elig},
            {"task_id": 2, "chain_name": "stable:v3.10", "chain_kind": "stable", "ref_name": "v3.10.1", "base_ref_name": "v3.10", "base_commit_id": 6, "status": elig},
            {"task_id": 3, "chain_name": "stable:v2.6.11", "chain_kind": "stable", "ref_name": "v2.6.11.1", "base_ref_name": "v2.6.11", "base_commit_id": 4, "status": elig},
            {"task_id": 4, "chain_name": "next:next-20110101", "chain_kind": "next", "ref_name": "next-20110101", "base_ref_name": "v2.6.37", "base_commit_id": 3, "status": elig},
            {"task_id": 5, "chain_name": "branch:history/2.4-stable", "chain_kind": "history", "ref_name": "history/2.4-stable", "base_ref_name": "2.4.15", "base_commit_id": 2, "status": elig},
            {"task_id": 6, "chain_name": "stable:v1.1", "chain_kind": "stable", "ref_name": "1.1.1", "base_ref_name": "1.1.0", "base_commit_id": 1, "status": elig},
        )
        order = []
        for _ in range(6):
            task = distributed.claim_next_task(db, network_id=1, worker_id="w")
            order.append(task["task_id"])
        self.assertEqual(order, [5, 6, 3, 1, 2, 4], "history, then stable by release, then next")
        self.assertIsNone(distributed.claim_next_task(db, network_id=1, worker_id="w"))

    def test_release_task_returns_lease_without_counting_an_attempt(self):
        db = DistDB().add_tasks({"task_id": 42, "status": distributed.TASK_LEASED, "worker_id": "w1", "retry_count": 1})
        self.assertFalse(distributed.release_task(db, 42, "node broken", "someone-else"))
        self.assertEqual(db.task(42)["status"], distributed.TASK_LEASED)
        self.assertTrue(distributed.release_task(db, 42, "node broken", "w1"))
        t = db.task(42)
        self.assertEqual((t["status"], t["worker_id"], t["retry_count"], t["error_msg"]),
                         (distributed.TASK_ELIGIBLE, "", 1, "node broken"))
        self.assertFalse(distributed.release_task(db, 42, "node broken", "w1"), "no lease left to release")

class TestDistributedCoreFixes(unittest.TestCase):
    """Targeted unit tests for distributed coordination fixes."""

    def test_reclaim_race_renewed_between_select_and_claim(self):
        """Verify that if a lease is renewed between SELECT and claim CAS update, no rollback occurs."""
        now = int(time.time())
        # The owner is alive and its lease was renewed: the task is no candidate at all
        db = DistDB().add_tasks(
            {"task_id": 1, "commit_id": 10, "worker_id": "worker-1", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "lease_expires_at": now + 120, "network_id": 1, "retry_count": 0}
        ).add_nodes({"node_id": "worker-1"})

        with unittest.mock.patch("core.distributed.rollback_version") as mock_rollback:
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 0)
            mock_rollback.assert_not_called()
        self.assertEqual(db.task(1)["status"], distributed.TASK_LEASED)

        # The lease expired when the scan ran but the worker renewed it before the claim: the claim loses
        db.update_rows("m_parse_task", {"lease_expires_at": now - 5}, {"task_id": 1})
        real_select_rows = db.select_rows

        def scan_then_renew(*args, **kwargs):
            rows = real_select_rows(*args, **kwargs)
            if isinstance(args[0], tuple) and args[0][0] == "m_parse_task":
                db.update_rows("m_parse_task", {"lease_expires_at": now + 120}, {"task_id": 1})
            return rows

        with unittest.mock.patch("core.distributed.rollback_version") as mock_rollback, \
                unittest.mock.patch.object(db, "select_rows", side_effect=scan_then_renew):
            self.assertEqual(distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60), 0)
            mock_rollback.assert_not_called()
        self.assertEqual((db.task(1)["status"], db.task(1)["worker_id"]), (distributed.TASK_LEASED, "worker-1"))

    def test_stale_mark_task_failed_from_non_owner(self):
        """Verify stale mark_task_failed from a non-owner is rejected and has no effect."""
        db = DistDB().add_tasks(
            {"task_id": 1, "commit_id": 10, "worker_id": "worker-2", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "lease_expires_at": int(time.time()) + 120, "network_id": 1, "retry_count": 0}
        )

        # worker-1 tries to mark worker-2's task as failed
        res = distributed.mark_task_failed(db, task_id=1, error_msg="Stale failure", worker_id="worker-1")
        self.assertFalse(res)
        self.assertEqual(db.task(1)["status"], distributed.TASK_LEASED)
        self.assertEqual(db.task(1)["retry_count"], 0)
        self.assertEqual(db.task(1)["worker_id"], "worker-2")

    def test_mark_task_failed_on_done_task_no_cascade(self):
        """Verify mark_task_failed on a DONE task with retry_count=2 leaves dependents unchanged."""
        db = DistDB().add_tasks(
            {"task_id": 1, "commit_id": 10, "worker_id": "", "ref_name": "v3.0",
             "status": distributed.TASK_DONE, "lease_expires_at": 0, "network_id": 1, "retry_count": 2},
            {"task_id": 2, "commit_id": 0, "worker_id": "", "ref_name": "v3.0.1",
             "base_ref_name": "v3.0", "base_commit_id": 10, "status": distributed.TASK_PENDING, "network_id": 1, "retry_count": 0},
        )

        res = distributed.mark_task_failed(db, task_id=1, error_msg="Late stale failure")
        self.assertFalse(res)
        self.assertEqual(db.task(1)["status"], distributed.TASK_DONE)
        self.assertEqual(db.task(2)["status"], distributed.TASK_PENDING)

    def test_three_level_chain_failure_cascade(self):
        """Verify failure cascades through all downstream dependent levels until fixed point."""
        db = DistDB().add_tasks(
            {"task_id": 1, "commit_id": 11, "worker_id": "worker-1", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "network_id": 1, "retry_count": 2},
            {"task_id": 2, "commit_id": 12, "worker_id": "", "ref_name": "v3.0.2",
             "base_ref_name": "v3.0.1", "base_commit_id": 11, "status": distributed.TASK_PENDING, "network_id": 1, "retry_count": 0},
            {"task_id": 3, "commit_id": 13, "worker_id": "", "ref_name": "v3.0.3",
             "base_ref_name": "v3.0.2", "base_commit_id": 12, "status": distributed.TASK_PENDING, "network_id": 1, "retry_count": 0},
        )

        res = distributed.mark_task_failed(db, task_id=1, error_msg="Unrecoverable syntax bug", worker_id="worker-1")
        self.assertTrue(res)
        self.assertEqual(db.task(1)["status"], distributed.TASK_FAILED)
        self.assertEqual(db.task(2)["status"], distributed.TASK_FAILED)
        self.assertEqual(db.task(3)["status"], distributed.TASK_FAILED)

    def test_is_parsing_finished_evaluates_all_pending(self):
        """Verify is_parsing_finished returns False when base is in progress, unlocks when done, and True only when blocked."""
        db = DistDB()

        # 1. Base task is in progress (TASK_LEASED) -> False
        db.add_tasks(
            {"task_id": 1, "commit_id": 10, "ref_name": "v3.0", "status": distributed.TASK_LEASED, "network_id": 1},
            {"task_id": 2, "commit_id": 0, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 10,
             "status": distributed.TASK_PENDING, "network_id": 1},
        )
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

        # 2. Base task is TASK_DONE but pending task was not unlocked -> unblocks and returns False
        db.update_rows("m_parse_task", {"status": distributed.TASK_DONE}, {"task_id": 1})
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))
        # Task 2 should now be unlocked to ELIGIBLE
        self.assertEqual(db.task(2)["status"], distributed.TASK_ELIGIBLE)

        # 3. Base task is TASK_FAILED -> cannot be unblocked -> returns True
        db.update_rows("m_parse_task", {"status": distributed.TASK_FAILED}, {"task_id": 1})
        db.update_rows("m_parse_task", {"status": distributed.TASK_PENDING}, {"task_id": 2})
        self.assertTrue(distributed.is_parsing_finished(db, network_id=1))

        # 4. The base ingestion is running (m_ingest_state) although no task of it is -> False
        db.update_rows("m_parse_task", {"status": distributed.TASK_FAILED, "commit_id": 0}, {"task_id": 1})
        db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"], [(1, 10, ingest_state.INGEST_PARSING, 0)])
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

    def test_get_default_node_id_length_and_tokens(self):
        """Verify node_id does not exceed 128 characters and parses tokens by delimiter."""
        nid = distributed.get_default_node_id("worker", "worker-123")
        # Should not falsely match PID 12 inside worker-123
        self.assertIn(str(os.getpid()), nid)

        # Very long custom identifier is hashed to <= 128 chars
        huge_id = "a" * 200
        safe_nid = distributed.get_default_node_id("worker", huge_id)
        self.assertLessEqual(len(safe_nid), 128)

    def test_reclaim_failed_rollback_marks_task_failed_directly(self):
        """Verify that when rollback_version returns False or raises, reclaim marks the task TASK_FAILED directly."""
        db = DistDB().add_tasks(
            {"task_id": 1, "commit_id": 10, "worker_id": "dead-node", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "lease_expires_at": 50, "network_id": 1, "retry_count": 0},
            {"task_id": 2, "ref_name": "v3.0.2", "base_ref_name": "v3.0.1", "base_commit_id": 10},
        )

        with unittest.mock.patch("core.distributed.rollback_version", return_value=False):
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 1)
            # Failed rollback must not leave task retryable
            t1 = db.task(1)
            self.assertEqual(t1["status"], distributed.TASK_FAILED)
            self.assertIn("Rollback failed during reclaim", t1["error_msg"])
            self.assertEqual(db.task(2)["status"], distributed.TASK_FAILED, "its dependents fail with it")

class TestLeaseSettings(unittest.TestCase):
    """parser.distributed lease/heartbeat config resolution."""

    def test_defaults_when_missing_or_invalid(self) -> None:
        defaults = (distributed.DEFAULT_LEASE_TTL, distributed.DEFAULT_HEARTBEAT_INTERVAL)
        self.assertEqual(distributed.resolve_lease_settings(None), defaults)
        self.assertEqual(distributed.resolve_lease_settings({}), defaults)
        self.assertEqual(
            distributed.resolve_lease_settings({"lease_ttl_seconds": "x", "heartbeat_seconds": 0}), defaults
        )

    def test_configured_values_and_lease_floor(self) -> None:
        self.assertEqual(distributed.resolve_lease_settings({"lease_ttl_seconds": 300, "heartbeat_seconds": 20}), (300, 20))
        # A lease shorter than two heartbeats is raised so live nodes renew before expiry
        self.assertEqual(distributed.resolve_lease_settings({"lease_ttl_seconds": 30, "heartbeat_seconds": 25}), (50, 25))


class TestMemMaxResolution(unittest.TestCase):
    """parser.mem_max resolution for the memory guard."""

    def test_resolve_mem_max(self) -> None:
        import main

        import pipeline.cli
        import pipeline.git_ingest
        import pipeline.git_network
        import pipeline.multicore
        import pipeline.update
        self.assertEqual(pipeline.multicore._resolve_mem_max(48), 48.0)
        self.assertEqual(pipeline.multicore._resolve_mem_max(0), 0.0)
        self.assertEqual(pipeline.multicore._resolve_mem_max(None), 60.0)
        self.assertEqual(pipeline.multicore._resolve_mem_max("bad"), 60.0)
        self.assertEqual(pipeline.multicore._resolve_mem_max(-5), 60.0)


if __name__ == "__main__":
    unittest.main()
