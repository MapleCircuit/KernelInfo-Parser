"""tests/test_webapp_jobs.py - Background jobs and 202 responses (webapp/API.md section 2.15)."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from webapp.backend.config import get_active_repo_name, set_active_repository
from webapp.backend.services import jobs as jobs_mod
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.jobs import JobCancelled, JobManager, respond_job


def _wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class TestJobs(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = JobManager(workers=2)

    def tearDown(self) -> None:
        self.mgr.cancel_all()

    def test_two_submits_share_one_run(self) -> None:
        calls = []
        gate = threading.Event()

        def fn(progress):
            calls.append(1)
            gate.wait(5)
            return {"ok": True}

        a = self.mgr.submit("k", fn)
        b = self.mgr.submit("k", fn)
        self.assertEqual(a["state"], "building")
        self.assertEqual(b["state"], "building")
        gate.set()
        state = self.mgr.wait("k", 5)
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["result"], {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.mgr.submit("k", fn)["result"], {"ok": True})
        self.assertEqual(len(calls), 1)

    def test_progress_is_visible(self) -> None:
        step = threading.Event()
        release = threading.Event()

        def fn(progress):
            progress(3, 10, "walking")
            step.set()
            release.wait(5)
            return 1

        self.mgr.submit("p", fn)
        self.assertTrue(step.wait(5))
        state = self.mgr.get("p")
        self.assertEqual(state["progress"], {"done": 3, "total": 10, "note": "walking"})
        release.set()
        self.assertEqual(self.mgr.wait("p", 5)["state"], "done")

    def test_failure_reported_then_expires(self) -> None:
        def fn(progress):
            raise RuntimeError("boom")

        self.mgr.submit("f", fn)
        state = self.mgr.wait("f", 5)
        self.assertEqual(state["state"], "failed")
        self.assertIn("boom", state["error"])
        self.assertEqual(self.mgr.submit("f", fn)["state"], "failed")  # kept for pollers
        with patch.object(jobs_mod, "FAILURE_TTL", -1):
            again = self.mgr.submit("f", lambda p: 7)
        self.assertEqual(again["state"], "building")
        self.assertEqual(self.mgr.wait("f", 5)["result"], 7)

    def test_cancel_stops_a_sleeping_job(self) -> None:
        started = threading.Event()

        def fn(progress):
            started.set()
            while True:
                progress(note="sleeping")
                time.sleep(0.01)

        self.mgr.submit("c", fn)
        self.assertTrue(started.wait(5))
        self.mgr.cancel_all()
        state = self.mgr.wait("c", 5)
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["error"], "cancelled")
        job = jobs_mod._Job("x", None, None, None, None)
        job.cancel_event.set()
        with self.assertRaises(JobCancelled):
            jobs_mod._Progress(job)(1, 2)

    def test_results_go_to_disk_and_failures_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": tmp}):
            cache = DiskCache("jobs-test")
            self.mgr.submit("d", lambda p: {"n": 1}, cache=cache, cache_key="ck")
            self.mgr.wait("d", 5)
            self.assertEqual(cache.get("ck"), {"n": 1})
            fresh = JobManager(workers=1)
            state = fresh.submit("d", lambda p: {"n": 2}, cache=cache, cache_key="ck")
            self.assertEqual(state["state"], "done")
            self.assertEqual(state["result"], {"n": 1})
            fresh.submit("e", lambda p: 1 / 0, cache=cache, cache_key="bad")
            fresh.wait("e", 5)
            self.assertIsNone(cache.get("bad"))
            fresh.cancel_all()

    def test_job_runs_in_the_submitting_repository_context(self) -> None:
        seen = []
        with patch("webapp.backend.config.get_repository", side_effect=lambda name=None: {"name": name or "default-repo", "path": "."}):
            token = set_active_repository("other-repo")
            try:
                self.mgr.submit("ctx", lambda p: seen.append(get_active_repo_name()) or 1)
            finally:
                from webapp.backend.config import reset_active_repository
                reset_active_repository(token)
            self.mgr.wait("ctx", 5)
        self.assertEqual(seen, ["other-repo"])

    def test_respond_job_shapes(self) -> None:
        gate = threading.Event()
        with patch.object(jobs_mod, "jobs", self.mgr):
            resp = respond_job("r", lambda p: gate.wait(5) and {"v": 1})
            self.assertIsInstance(resp, JSONResponse)
            self.assertEqual(resp.status_code, 202)
            self.assertEqual(resp.headers["cache-control"], "no-store")
            gate.set()
            self.mgr.wait("r", 5)
            self.assertEqual(respond_job("r", lambda p: None), {"v": 1})
            self.assertEqual(respond_job("w", lambda p: {"quick": True}, wait=5), {"quick": True})
            self.assertEqual(respond_job("w", lambda p: None, shape=lambda r: r["quick"]), True)
            self.mgr.submit("x", lambda p: 1 / 0)
            self.mgr.wait("x", 5)
            with self.assertRaises(HTTPException) as ctx:
                respond_job("x", lambda p: None)
            self.assertEqual(ctx.exception.status_code, 500)

    def test_retry_after_grows(self) -> None:
        now = time.time()
        self.assertEqual(jobs_mod.retry_after_ms({"started_at": now}), 500)
        self.assertEqual(jobs_mod.retry_after_ms({"started_at": now - 100}), 5000)


if __name__ == "__main__":
    unittest.main()
