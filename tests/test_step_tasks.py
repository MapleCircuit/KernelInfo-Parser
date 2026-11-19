"""tests/test_step_tasks.py - Independent steps as distributed auxiliary tasks (pipeline/step_tasks.py).

Covers follow-up task creation for failed / incapable version steps, the disabled-step silence, step capability
gating (a node missing a capability still parses versions), repository-level CVE / release statistics tasks and
their worker-first priority, backoff, and recovery rules (zombie sweep and failure cascade never touch a completed
version for an auxiliary task).
"""
from __future__ import annotations

import contextlib
import time
import unittest
from unittest import mock

from core import distributed, ingest_state
from pipeline import step_tasks
from pipeline.scheduler import DistributedTaskScheduler
from pipeline.step_tasks import StepResult
from tests.db_fixtures import DistDB

NOW = int(time.time())


def _version_row(ref: str, *, base: str = "", base_cid: int = 0, cid: int = 0, seq: int = 1,
                 task_type: str = "stable", track: str = "stable:v3.0", priority: int = 5, status: str = "pending") -> tuple:
    return (1, 1, "linux", task_type, track, ref, "", cid, base, base_cid, priority, seq, status, NOW, NOW)


def _scheduler(db: DistDB, node: str, role: str = distributed.ROLE_WORKER, **kw) -> DistributedTaskScheduler:
    kw.setdefault("max_priority", distributed.PRIORITY_WORKER_MAX if role == distributed.ROLE_WORKER else None)
    return DistributedTaskScheduler(lambda: contextlib.nullcontext(db), 1, node, role, **kw)


def _caps(*steps: str, track: str = "*") -> list[dict]:
    return [{"repo_name": "linux", "track_name": track, "steps": list(steps)}]


def _db_patch(db: DistDB):
    return mock.patch.object(step_tasks.G, "DB", lambda: contextlib.nullcontext(db))


class TestFollowupRows(unittest.TestCase):
    TASK = {
        "network_id": 1, "repo_id": 1, "repo_name": "linux", "ref_name": "v3.0.1", "track_name": "stable:v3.0",
        "target_sha": "a" * 40, "commit_id": 11, "base_ref_name": "v3.0", "base_commit_id": 10, "seq_order": 1,
    }

    def test_failed_and_incapable_steps_become_followups(self) -> None:
        rows = step_tasks.followup_rows(self.TASK, {
            "mailing_list": StepResult(step_tasks.STATUS_INCAPABLE, "no archive"),
            "kapi": StepResult(step_tasks.STATUS_FAILED, "boom"),
            "insights": StepResult(step_tasks.STATUS_OK),
        }, now=NOW)
        by_type = {r[3]: r for r in rows}
        self.assertEqual(set(by_type), {distributed.TASK_TYPE_STEP_MAILING_LIST, distributed.TASK_TYPE_STEP_KAPI})
        ml = by_type[distributed.TASK_TYPE_STEP_MAILING_LIST]
        self.assertEqual(ml[4:10], ("stable:v3.0", "v3.0.1", "a" * 40, 11, "v3.0", 10))
        self.assertEqual((ml[10], ml[12], ml[15], ml[16]), (distributed.PRIORITY_FOLLOWUP, "pending", "mailing_list", 0))
        self.assertLess(distributed.PRIORITY_FOLLOWUP, distributed.PRIORITY_STABLE)

    def test_disabled_skipped_and_ok_steps_produce_nothing(self) -> None:
        rows = step_tasks.followup_rows(self.TASK, {
            "mailing_list": StepResult(step_tasks.STATUS_DISABLED),
            "kapi": StepResult(step_tasks.STATUS_SKIPPED, "not_selected"),
            "insights": StepResult(step_tasks.STATUS_OK),
        })
        self.assertEqual(rows, [])

    def test_repo_steps_never_become_version_followups(self) -> None:
        self.assertEqual(step_tasks.followup_rows(self.TASK, {"cve": StepResult(step_tasks.STATUS_FAILED)}), [])

    def test_enqueue_deduplicates_on_unique_key(self) -> None:
        db = DistDB(full_schema=True)
        outcomes = {"mailing_list": StepResult(step_tasks.STATUS_FAILED, "x")}
        step_tasks.enqueue_step_followups(db, self.TASK, outcomes)
        step_tasks.enqueue_step_followups(db, self.TASK, outcomes)
        self.assertEqual(len(db.dist_tasks), 1)
        self.assertEqual(db.dist_tasks[0]["required_capability"], "mailing_list")

    def test_outcome_mapping(self) -> None:
        self.assertEqual(step_tasks.kapi_outcome(None).status, step_tasks.STATUS_DISABLED)
        self.assertEqual(step_tasks.kapi_outcome({"state": "error", "error": "e"}), StepResult(step_tasks.STATUS_FAILED, "e"))
        self.assertEqual(step_tasks.kapi_outcome({"state": "skipped", "reason": "not_selected"}).status, step_tasks.STATUS_SKIPPED)
        self.assertEqual(step_tasks.kapi_outcome({"state": "updated"}).status, step_tasks.STATUS_OK)
        pp = step_tasks.preparse_outcomes({"errors": [
            {"tag": "v3.1", "error": "kapi: no headers"}, {"tag": "v3.1", "error": "deltas: bad"},
            {"tag": "v3.1", "error": "persist failed"},
        ]})
        self.assertEqual(set(pp), {"kapi", "insights"})
        self.assertIn("deltas: bad", pp["insights"].detail)
        self.assertEqual(step_tasks.preparse_outcomes({"errors": []}), {})


class TestInlineGate(unittest.TestCase):
    def setUp(self) -> None:
        step_tasks.reset_step_outcomes()
        self.addCleanup(step_tasks.reset_step_outcomes)
        self.addCleanup(step_tasks._PROBE_CACHE.clear)

    def test_disabled_feature_is_recorded_silently(self) -> None:
        with mock.patch.object(step_tasks, "feature_enabled", lambda f: False):
            res = step_tasks.inline_step_gate("mailing_list", "/repo")
        self.assertEqual(res.status, step_tasks.STATUS_DISABLED)
        self.assertEqual(step_tasks.followup_rows(TestFollowupRows.TASK, step_tasks.step_outcomes()), [])

    def test_distributed_node_without_capability_records_incapable(self) -> None:
        with mock.patch.object(step_tasks, "feature_enabled", lambda f: True), \
                mock.patch.object(step_tasks, "_mailing_list_enabled", lambda: True), \
                mock.patch.object(step_tasks.G, "distributed", True, create=True), \
                mock.patch.dict(step_tasks.STEPS, {"mailing_list": step_tasks.STEPS["mailing_list"].__class__(
                    **{**step_tasks.STEPS["mailing_list"].__dict__, "probe": lambda _p: (False, "no archive"),
                       "extra_enabled": None})}):
            res = step_tasks.inline_step_gate("mailing_list", "/repo")
        self.assertEqual(res, StepResult(step_tasks.STATUS_INCAPABLE, "no archive"))
        self.assertEqual(step_tasks.step_outcomes()["mailing_list"].status, step_tasks.STATUS_INCAPABLE)

    def test_standalone_runs_the_step(self) -> None:
        with mock.patch.object(step_tasks, "feature_enabled", lambda f: True), \
                mock.patch.object(step_tasks, "_mailing_list_enabled", lambda: True), \
                mock.patch.object(step_tasks.G, "distributed", False, create=True):
            self.assertIsNone(step_tasks.inline_step_gate("mailing_list", "/repo"))


def _base_done(db: DistDB, commit_id: int = 10) -> None:
    """Version v3.0 (commit 10), the base of the tasks below, is ingested."""
    db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"],
                   [(1, commit_id, ingest_state.INGEST_DONE, NOW)])


class TestCapabilityGatingAndRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self.db = DistDB(full_schema=True)
        _base_done(self.db)

    def _followup(self, ref: str, step: str = "mailing_list", cid: int = 11, base: str = "v3.0", base_cid: int = 10) -> None:
        task = {"network_id": 1, "repo_id": 1, "repo_name": "linux", "ref_name": ref, "track_name": "stable:v3.0",
                "commit_id": cid, "base_ref_name": base, "base_commit_id": base_cid, "seq_order": 1}
        step_tasks.enqueue_step_followups(self.db, task, {step: StepResult(step_tasks.STATUS_FAILED, "x")})

    def test_incapable_worker_parses_versions_but_never_claims_the_step(self) -> None:
        self.db.enqueue_distributed_tasks([_version_row("v3.0.2", seq=2)])
        self._followup("v3.0.1")
        plain = _scheduler(self.db, "plain")
        plain.register_capabilities(_caps("kapi"), db=self.db)
        task = plain.claim_next(db=self.db)
        self.assertEqual((task["task_type"], task["ref_name"]), ("stable", "v3.0.2"))
        self.assertIsNone(plain.claim_next(db=self.db), "the mailing list follow-up needs the capability")

        capable = _scheduler(self.db, "capable")
        capable.register_capabilities(_caps("mailing_list"), db=self.db)
        ml = capable.claim_next(db=self.db)
        self.assertEqual((ml["task_type"], ml["ref_name"]), (distributed.TASK_TYPE_STEP_MAILING_LIST, "v3.0.1"))
        self.assertTrue(capable.complete(ml["task_id"], ml["commit_id"], ref_name="v3.0.1", db=self.db))
        self.assertEqual(self.db.dist_task(ml["task_id"])["status"], "completed")

    def test_step_caps_are_replaced_and_unregistered(self) -> None:
        sched = _scheduler(self.db, "w1")
        sched.register_capabilities(_caps("mailing_list", "cve"), db=self.db)
        caps = self.db.select_values("distributed_worker_step_caps", "capability", {"worker_id": "w1"})
        self.assertEqual(sorted(caps), ["cve", "mailing_list"])
        sched.register_capabilities(_caps("kapi"), db=self.db)
        self.assertEqual(self.db.select_values("distributed_worker_step_caps", "capability", {"worker_id": "w1"}), ["kapi"])
        self.db.unregister_worker_capabilities("w1")
        self.assertEqual(self.db.count_rows("distributed_worker_step_caps", {"worker_id": "w1"}), 0)

    def test_parent_completes_and_unlocks_dependents_despite_step_failure(self) -> None:
        self.db.enqueue_distributed_tasks([
            _version_row("v3.0.1", base="v3.0", base_cid=10, seq=1),
            _version_row("v3.0.2", base="v3.0.1", seq=2),
        ])
        sched = _scheduler(self.db, "w1")
        sched.register_capabilities(_caps(), db=self.db)
        parent = sched.claim_next(db=self.db)
        self.assertEqual(parent["ref_name"], "v3.0.1")
        step_tasks.enqueue_step_followups(self.db, {**parent, "commit_id": 11}, {
            "mailing_list": StepResult(step_tasks.STATUS_INCAPABLE, "no archive"),
        })
        self.assertTrue(sched.complete(parent["task_id"], 11, ref_name="v3.0.1", db=self.db))
        child = sched.claim_next(db=self.db)
        self.assertEqual((child["ref_name"], child["base_commit_id"]), ("v3.0.2", 11))
        ml = [t for t in self.db.dist_tasks if t["task_type"] == distributed.TASK_TYPE_STEP_MAILING_LIST]
        self.assertEqual([(t["ref_name"], t["status"], t["base_ref_name"]) for t in ml], [("v3.0.1", "pending", "v3.0")])

    def test_completed_followup_does_not_satisfy_a_base_dependency(self) -> None:
        self.db.enqueue_distributed_tasks([
            _version_row("v3.0.1", base="v3.0", seq=1),
            _version_row("v3.0.2", base="v3.0.1", seq=2),
        ])
        self._followup("v3.0.1", step="kapi")
        self.db.update_rows("distributed_tasks", {"status": "completed"}, {"task_type": distributed.TASK_TYPE_STEP_KAPI})
        sched = _scheduler(self.db, "w1")
        sched.register_capabilities(_caps("kapi"), db=self.db)
        self.assertIsNone(sched.claim_next(db=self.db), "v3.0.1 itself is not ingested; neither version is ready")

    def test_completing_a_followup_does_not_backfill_dependents(self) -> None:
        self.db.enqueue_distributed_tasks([_version_row("v3.0.2", base="v3.0.1", seq=2)])
        self._followup("v3.0.1", step="kapi", cid=99)
        sched = _scheduler(self.db, "w1")
        sched.register_capabilities(_caps("kapi"), db=self.db)
        step = sched.claim_next(db=self.db)
        self.assertEqual(step["task_type"], distributed.TASK_TYPE_STEP_KAPI)
        sched.complete(step["task_id"], 99, ref_name="v3.0.1", db=self.db)
        v2 = next(t for t in self.db.dist_tasks if t["ref_name"] == "v3.0.2")
        self.assertEqual(v2["base_commit_id"], 0)

    def test_backoff_holds_a_failed_step_back(self) -> None:
        self._followup("v3.0.1")
        sched = _scheduler(self.db, "w1")
        sched.register_capabilities(_caps("mailing_list"), db=self.db)
        task = sched.claim_next(db=self.db, now=NOW)
        sched.fail(task["task_id"], "archive down", max_attempts=5, retry_delay=120, db=self.db, now=NOW)
        row = self.db.dist_task(task["task_id"])
        self.assertEqual((row["status"], row["available_ts"]), ("pending", NOW + 120))
        self.assertIsNone(sched.claim_next(db=self.db, now=NOW + 60))
        self.assertFalse(sched.is_queue_drained(db=self.db), "a delayed task is still work")
        self.assertIsNotNone(sched.claim_next(db=self.db, now=NOW + 121))

    def test_zombie_followup_resets_without_touching_its_version(self) -> None:
        self.db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"],
                            [(1, 11, ingest_state.INGEST_DONE, NOW)])
        self.db.enqueue_distributed_tasks([_version_row("v3.0.1", cid=11, status="completed")])
        self._followup("v3.0.1", cid=11)
        ml_id = next(t["task_id"] for t in self.db.dist_tasks if t["task_type"] == distributed.TASK_TYPE_STEP_MAILING_LIST)
        self.db.update_rows("distributed_tasks", {"status": "claimed", "worker_id": "dead", "heartbeat_ts": NOW - 10_000},
                            {"task_id": ml_id})
        sched = _scheduler(self.db, "w1", heartbeat_timeout=60)
        with mock.patch.object(ingest_state, "rollback_version") as rollback:
            self.assertEqual(sched.sweep_zombies(db=self.db, now=NOW), 1)
        rollback.assert_not_called()
        self.assertEqual(self.db.dist_task(ml_id)["status"], "pending", "not promoted to completed without running")
        self.assertEqual(ingest_state.get_status(self.db, 1, 11), ingest_state.INGEST_DONE)

    def test_terminal_step_failure_never_cascades(self) -> None:
        self.db.enqueue_distributed_tasks([_version_row("v3.0.2", base="v3.0.1", seq=2)])
        self._followup("v3.0.1")
        self._followup("v3.0.2", step="kapi", base="v3.0.1", cid=12)
        sched = _scheduler(self.db, "w1")
        sched.register_capabilities(_caps("mailing_list"), db=self.db)
        task = sched.claim_next(db=self.db)
        sched.fail(task["task_id"], "boom", max_attempts=1, db=self.db)
        self.assertEqual(self.db.dist_task(task["task_id"])["status"], "failed")
        others = {(t["task_type"], t["ref_name"]): t["status"] for t in self.db.dist_tasks if t["task_id"] != task["task_id"]}
        self.assertEqual(others, {("stable", "v3.0.2"): "pending", (distributed.TASK_TYPE_STEP_KAPI, "v3.0.2"): "pending"})

    def test_version_failure_cascades_to_versions_only(self) -> None:
        self.db.enqueue_distributed_tasks([
            _version_row("v3.0.1", base="v3.0", base_cid=10, seq=1),
            _version_row("v3.0.2", base="v3.0.1", seq=2),
        ])
        self._followup("v3.0.2", step="kapi", base="v3.0.1", cid=12)
        sched = _scheduler(self.db, "w1")
        sched.register_capabilities(_caps(), db=self.db)
        parent = sched.claim_next(db=self.db)
        sched.fail(parent["task_id"], "boom", max_attempts=1, db=self.db)
        status = {(t["task_type"], t["ref_name"]): t["status"] for t in self.db.dist_tasks}
        self.assertEqual(status[("stable", "v3.0.2")], "failed")
        self.assertEqual(status[(distributed.TASK_TYPE_STEP_KAPI, "v3.0.2")], "pending")


class TestRepoTasks(unittest.TestCase):
    def setUp(self) -> None:
        self.db = DistDB(full_schema=True)
        _base_done(self.db)
        fp = mock.patch.object(step_tasks, "repo_fingerprint", lambda step, path, now=None: f"fp-{step.name}")
        fp.start()
        self.addCleanup(fp.stop)

    def _enqueue(self, features: dict, **kw) -> int:
        return step_tasks.enqueue_repo_tasks(
            self.db, 1, 1, "linux", "/repo", feature_lookup=lambda f: features.get(f, False), is_kernel=True, **kw
        )

    def test_enqueue_and_fingerprint_reopen(self) -> None:
        self.assertEqual(self._enqueue({"cve": True, "commits": True}), 2)
        rows = {t["task_type"]: t for t in self.db.dist_tasks}
        cve = rows[distributed.TASK_TYPE_REPO_CVE]
        self.assertEqual((cve["ref_name"], cve["track_name"], cve["priority"], cve["required_capability"]),
                         (distributed.REPO_TASK_REF, "", distributed.PRIORITY_REPO, "cve"))
        self.assertEqual(self._enqueue({"cve": True, "commits": True}), 0, "unchanged fingerprint")
        self.db.update_rows("distributed_tasks", {"status": "completed"}, {})
        self.assertEqual(self._enqueue({"cve": True, "commits": True}), 0, "completed and unchanged stays closed")
        with mock.patch.object(step_tasks, "repo_fingerprint", lambda step, path, now=None: "moved"):
            self.assertEqual(self._enqueue({"cve": True, "commits": True}), 2)
        self.assertEqual({t["status"] for t in self.db.dist_tasks}, {"pending"})

    def test_disabled_and_skipped_steps_are_not_published(self) -> None:
        self.assertEqual(self._enqueue({}), 0)
        self.assertEqual(self._enqueue({"cve": True, "insights": True}, skip_steps=("cve",)), 1)
        self.assertEqual([t["task_type"] for t in self.db.dist_tasks], [distributed.TASK_TYPE_REPO_RELEASE_STATS])
        rows = step_tasks.repo_task_rows(1, 1, "man-pages", "/mp", feature_lookup=lambda f: True, is_kernel=False)
        self.assertEqual([r[3] for r in rows], [distributed.TASK_TYPE_REPO_RELEASE_STATS], "CVE needs a kernel tree")

    def test_capable_worker_claims_cve_first_incapable_one_parses(self) -> None:
        self.db.enqueue_distributed_tasks([_version_row("v3.0.1", base="v3.0", base_cid=10)])
        self._enqueue({"cve": True})
        with_cve = _scheduler(self.db, "with-cve")
        with_cve.register_capabilities(_caps("cve", track="stable:v3.0"), db=self.db)
        without = _scheduler(self.db, "without")
        without.register_capabilities(_caps(track="stable:v3.0"), db=self.db)
        first = with_cve.claim_next(db=self.db)
        self.assertEqual(first["task_type"], distributed.TASK_TYPE_REPO_CVE, "repository task before stable versions")
        self.assertEqual(without.claim_next(db=self.db)["ref_name"], "v3.0.1")
        master = _scheduler(self.db, "master", distributed.ROLE_MASTER, min_priority=distributed.PRIORITY_MAIN)
        master.register_capabilities(_caps("cve"), db=self.db)
        self.db.update_rows("distributed_tasks", {"status": "pending", "worker_id": ""}, {"task_id": first["task_id"]})
        self.assertIsNone(master.claim_next(db=self.db), "the master's version loop never takes repository tasks")

    def test_run_and_finish_cve_task(self) -> None:
        self._enqueue({"cve": True})
        sched = _scheduler(self.db, "w1")
        sched.register_capabilities(_caps("cve"), db=self.db)
        task = sched.claim_next(db=self.db)
        with mock.patch.object(step_tasks, "feature_enabled", lambda f: True), \
                mock.patch.object(step_tasks, "kernel_steps_apply", lambda: True), \
                mock.patch.object(step_tasks, "probe_step", lambda step, path, refresh=False: (True, "")), \
                mock.patch("pipeline.external_steps.processing_cve_list", return_value={"state": "current"}) as cve:
            res = step_tasks.run_step_task(task, "/repo", 1)
        self.assertEqual(res.status, step_tasks.STATUS_OK)
        self.assertEqual(cve.call_args.kwargs["kernel_repo"], "/repo")
        self.assertEqual(step_tasks.finish_step_task(sched, task, res, db=self.db), "completed")
        self.assertEqual(self.db.dist_task(task["task_id"])["status"], "completed")

    def test_failed_and_incapable_results_back_off(self) -> None:
        self._enqueue({"cve": True})
        sched = _scheduler(self.db, "w1")
        sched.register_capabilities(_caps("cve"), db=self.db)
        task = sched.claim_next(db=self.db)
        self.assertEqual(step_tasks.finish_step_task(sched, task, StepResult(step_tasks.STATUS_FAILED, "x"), db=self.db), "failed")
        row = self.db.dist_task(task["task_id"])
        self.assertEqual((row["status"], row["retry_count"]), ("pending", 1))
        self.assertGreater(row["available_ts"], NOW)
        later = sched.claim_next(db=self.db, now=row["available_ts"] + 1)
        self.assertEqual(step_tasks.finish_step_task(sched, later, StepResult(step_tasks.STATUS_INCAPABLE), db=self.db), "released")
        self.assertEqual(self.db.dist_task(task["task_id"])["retry_count"], 1, "a release is no failed attempt")

    def test_master_drain_runs_leftover_aux_tasks_only(self) -> None:
        self.db.enqueue_distributed_tasks([_version_row("v3.0.1", base="v3.0", base_cid=10)])
        self._enqueue({"cve": True, "commits": True})
        sched = _scheduler(self.db, "master", distributed.ROLE_MASTER, max_priority=None,
                           repo_name="linux", task_types=distributed.AUX_TASK_TYPES)
        sched.register_capabilities(_caps("cve", "release_stats"), db=self.db)
        with _db_patch(self.db), mock.patch.object(step_tasks, "run_step_task", return_value=StepResult(step_tasks.STATUS_OK)):
            ran = step_tasks.drain_aux_tasks(sched, "/repo", 1, should_stop=lambda: False)
        self.assertEqual(ran, 2)
        status = {t["task_type"]: t["status"] for t in self.db.dist_tasks}
        self.assertEqual(status, {"stable": "pending", distributed.TASK_TYPE_REPO_CVE: "completed",
                                  distributed.TASK_TYPE_REPO_RELEASE_STATS: "completed"})


class TestMailingListRange(unittest.TestCase):
    def test_standalone_cli_exits_on_missing_archive(self) -> None:
        from pipeline import mailing_list_steps as mls
        with mock.patch.object(mls, "resolve_mailing_list_range", side_effect=FileNotFoundError("no archive")), \
                mock.patch.object(mls.G, "emergency_shutdown") as shutdown:
            mls.standalone_resolve_mailing_list("/repo", "v3.0..v3.1")
        shutdown.assert_called_once_with(1)

    def test_step_task_reports_missing_archive_as_failure(self) -> None:
        ctx = step_tasks.StepContext("/repo", 1, 1, "linux", "v3.1", "v3.0", 5)
        with mock.patch("pipeline.mailing_list_steps.resolve_mailing_list_range", side_effect=ConnectionError("down")) as res:
            out = step_tasks._run_mailing_list(ctx)
        self.assertEqual(out, StepResult(step_tasks.STATUS_FAILED, "down"))
        self.assertEqual(res.call_args.args[1], "v3.0..v3.1")


class TestMainHelpers(unittest.TestCase):
    def test_skipped_repo_steps_follow_cli_flags(self) -> None:
        import main
        args = mock.Mock(no_cve=True, no_release_stats=False)
        self.assertEqual(main._skipped_repo_steps(args), ("cve",))
        args = mock.Mock(no_cve=False, no_release_stats=True)
        self.assertEqual(main._skipped_repo_steps(args), ("release_stats",))


class TestWorkerLoopRouting(unittest.TestCase):
    def test_worker_runs_step_tasks_and_queues_followups_of_its_versions(self) -> None:
        import os
        import subprocess
        import tempfile
        from pathlib import Path

        from main import G, gp, main

        saved = (getattr(G, "distributed_role", None), getattr(G, "distributed", False), getattr(G, "node_id", None), G.DB,
                 getattr(gp, "VID", 0))
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "linux"
            subprocess.run(["git", "init", "--initial-branch=master", str(repo)], check=True, capture_output=True)
            env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
                   "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com"}
            (repo / "f.c").write_text("int f(void) { return 0; }\n")
            for cmd in (["add", "f.c"], ["commit", "-m", "v3.0"], ["tag", "v3.0"]):
                subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env, capture_output=True)

            db = DistDB(full_schema=True)
            db.enqueue_distributed_tasks([
                (1, 1, "linux", "main", "mainline", "v3.0", "", 0, "", 0, 5, 0, "pending", NOW, NOW),
                (1, 1, "linux", distributed.TASK_TYPE_REPO_CVE, "", distributed.REPO_TASK_REF, "fp", 0, "", 0,
                 distributed.PRIORITY_REPO, 0, "pending", NOW, NOW, "cve", 0),
            ])
            order: list[str] = []

            def fake_update(ref_name: str, repo_path=None) -> bool:
                order.append(ref_name)
                gp.VID = 50
                step_tasks.record_step_outcome("mailing_list", StepResult(step_tasks.STATUS_INCAPABLE, "no archive"))
                return True

            def fake_run(task, repo_path, repo_id):
                order.append(task["task_type"])
                return StepResult(step_tasks.STATUS_OK)

            mock_args = mock.MagicMock()
            mock_args.role = "worker"
            mock_args.worker_id = "test-worker"
            mock_args.worker_once = True
            mock_args.repo = str(repo)
            mock_args.ref = None
            mock_args.include_pre_releases = True
            mock_args.lock_dir = None
            mock_args.force_singleton = False
            mock_args.poll_interval = 60
            mock_args.git_update_interval = 300
            mock_args.no_git_update = True
            mock_args.git_auto_update = False
            mock_args.mem_max = 0.0
            mock_args.delegate_branches = []
            for flag in ("reset", "Drop", "cve", "clean_temp", "resolve_ml", "detect_repos", "release_stats", "preparse",
                         "single_version", "branches"):
                setattr(mock_args, flag, False)

            caps = [{"capability": "cve", "detail": ""}]
            patches = [
                mock.patch("main.arg_handling", return_value=mock_args),
                mock.patch("main.get_repository", return_value={"name": "linux", "path": str(repo), "project": "linux", "enabled": True}),
                mock.patch("main.setup_memory_limit"),
                mock.patch("main._apply_db_server_tuning"),
                mock.patch("main._preflight_libclang"),
                mock.patch("main.get_mailing_list_config", return_value={"enabled": False}),
                mock.patch("main.resolve_git_network_and_repo", return_value=(1, 1)),
                mock.patch("main.local_step_capabilities", return_value=caps),
                mock.patch("pipeline.step_tasks.run_step_task", side_effect=fake_run),
                mock.patch("core.ingest_state.fetch_repo_version_states", return_value={}),
                mock.patch("core.distributed.NodeHeartbeatDaemon"),
                mock.patch("main.update", side_effect=fake_update),
            ]
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                G.DB = lambda: mock.MagicMock(__enter__=lambda s: db, __exit__=lambda s, e, t, b: None)
                try:
                    with self.assertRaises(SystemExit) as ctx:
                        main()
                    self.assertEqual(ctx.exception.code, 0)
                finally:
                    G.distributed_role, G.distributed, G.node_id, G.DB, gp.VID = saved

        self.assertEqual(order, [distributed.TASK_TYPE_REPO_CVE, "v3.0"], "the CVE task is claimed before versions")
        status = {(t["task_type"], t["ref_name"]): t["status"] for t in db.dist_tasks}
        self.assertEqual(status[("main", "v3.0")], "completed")
        self.assertEqual(status[(distributed.TASK_TYPE_REPO_CVE, distributed.REPO_TASK_REF)], "completed")
        self.assertEqual(status[(distributed.TASK_TYPE_STEP_MAILING_LIST, "v3.0")], "pending",
                         "the step this worker could not run waits for a capable node")


if __name__ == "__main__":
    unittest.main()
