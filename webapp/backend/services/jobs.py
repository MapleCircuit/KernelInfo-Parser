"""webapp/backend/services/jobs.py - Background jobs and 202 responses (webapp/API.md section 2.15).

Work that takes seconds to minutes (indexes, per-release passes, reports) runs on a small pool of
daemon worker threads instead of holding an HTTP request open:

    state = jobs.submit(key, fn, cache=disk_cache, cache_key=ck)   # fn(progress) -> JSON-able result
    jobs.get(key)                                                   # JobState or None

- One job per key: concurrent submissions share the running (or queued) job.
- ``fn(progress)`` reports with ``progress(done, total, note)``; the call raises ``JobCancelled``
  once the job is cancelled, and ``progress.cancelled`` (a ``threading.Event``) lets long
  subprocess loops check the flag themselves.
- A result goes to the disk cache (S09) when one is given, and to a small in-memory LRU.
- Failures are logged and kept for ``FAILURE_TTL`` seconds so pollers see them, never cached.
- Jobs run in a copy of the submitting request's context, so the active repository (section 2.10)
  is the one of the request that started them.

Routers answer through ``respond_job()``: 200 with the body when ready, 202 with progress while
building (``Cache-Control: no-store``), 500 when the job failed.
"""
from __future__ import annotations

import contextvars
import logging
import queue
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from fastapi import HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

FAILURE_TTL = 60.0
RESULT_LRU = 16


class JobCancelled(Exception):
    """Raised by ``progress()`` inside a job that was cancelled (shutdown or ``cancel()``)."""


class _Progress:
    """Callable handed to a job function: ``progress(done, total, note)``."""

    def __init__(self, job: "_Job") -> None:
        self._job = job
        self.cancelled = job.cancel_event

    def __call__(self, done: int | None = None, total: int | None = None, note: str | None = None) -> None:
        if self.cancelled.is_set():
            raise JobCancelled()
        p = self._job.progress
        if done is not None:
            p["done"] = int(done)
        if total is not None:
            p["total"] = int(total)
        if note is not None:
            p["note"] = str(note)[:200]


class _Job:
    __slots__ = ("key", "fn", "cache", "cache_key", "ctx", "state", "progress", "started_at",
                 "finished_at", "result", "error", "cancel_event", "done_event")

    def __init__(self, key: str, fn: Callable[[_Progress], Any], cache: Any, cache_key: str | None,
                 ctx: contextvars.Context) -> None:
        self.key = key
        self.fn = fn
        self.cache = cache
        self.cache_key = cache_key
        self.ctx = ctx
        self.state = "building"
        self.progress: dict[str, Any] = {"done": 0, "total": 0, "note": "queued"}
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.result: Any = None
        self.error: str | None = None
        self.cancel_event = threading.Event()
        self.done_event = threading.Event()

    def snapshot(self, with_result: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "state": self.state,
            "progress": dict(self.progress),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.error:
            out["error"] = self.error
        if with_result and self.state == "done":
            out["result"] = self.result
        return out


class JobManager:
    """Bounded pool of daemon worker threads running keyed jobs."""

    def __init__(self, workers: int | None = None) -> None:
        self._workers = workers
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}          # building (queued or running)
        self._done: OrderedDict[str, _Job] = OrderedDict()  # finished: results (LRU) and failures (TTL)
        self._queue: "queue.Queue[_Job | None]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._shutdown = False

    # ----------------------------------------------------------------- workers
    def _worker_count(self) -> int:
        if self._workers is not None:
            return max(1, int(self._workers))
        try:
            from core.config import get_webapp_config
            return max(1, int((get_webapp_config().get("jobs") or {}).get("workers", 2)))
        except Exception:
            return 2

    def _ensure_workers(self) -> None:
        alive = [t for t in self._threads if t.is_alive()]
        self._threads = alive
        for i in range(len(alive), self._worker_count()):
            t = threading.Thread(target=self._run, name=f"kip-job-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            if job.cancel_event.is_set():
                self._finish(job, error="cancelled")
                continue
            job.progress["note"] = ""
            job.started_at = time.time()
            try:
                result = job.ctx.run(job.fn, _Progress(job))
            except JobCancelled:
                self._finish(job, error="cancelled")
                continue
            except HTTPException as exc:
                self._finish(job, error=str(exc.detail))
                continue
            except Exception as exc:  # noqa: BLE001 - a job failure is reported, never raised
                logger.warning("Background job %s failed: %s", job.key, exc, exc_info=True)
                self._finish(job, error=f"{type(exc).__name__}: {exc}")
                continue
            if job.cancel_event.is_set():
                self._finish(job, error="cancelled")
                continue
            if job.cache is not None and job.cache_key:
                try:
                    job.cache.put(job.cache_key, result)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Job %s result not stored on disk: %s", job.key, exc)
            self._finish(job, result=result)

    def _finish(self, job: _Job, result: Any = None, error: str | None = None) -> None:
        job.finished_at = time.time()
        if error is None:
            job.state = "done"
            job.result = result
        else:
            job.state = "failed"
            job.error = error
        with self._lock:
            self._jobs.pop(job.key, None)
            self._done.pop(job.key, None)
            self._done[job.key] = job
            self._prune_locked()
        job.done_event.set()

    def _prune_locked(self) -> None:
        now = time.time()
        for key in [k for k, j in self._done.items() if j.state == "failed" and now - (j.finished_at or now) > FAILURE_TTL]:
            self._done.pop(key, None)
        done = [k for k, j in self._done.items() if j.state == "done"]
        for key in done[:max(0, len(done) - RESULT_LRU)]:
            self._done.pop(key, None)

    # --------------------------------------------------------------------- API
    def submit(self, key: str, fn: Callable[[_Progress], Any], *, cache: Any = None,
               cache_key: str | None = None) -> dict[str, Any]:
        """Start ``fn`` under ``key`` unless a job with that key is already building or done.

        Returns the job's state (``result`` included when it is already available)."""
        with self._lock:
            self._prune_locked()
            running = self._jobs.get(key)
            if running is not None:
                return running.snapshot()
            finished = self._done.get(key)
            if finished is not None and finished.state == "done":
                self._done.move_to_end(key)
                return finished.snapshot(with_result=True)
            if finished is not None and finished.state == "failed":
                return finished.snapshot()
        if cache is not None and cache_key:
            hit = cache.get(cache_key)
            if hit is not None:
                return {"key": key, "state": "done", "progress": {"done": 1, "total": 1, "note": "cached"},
                        "started_at": None, "finished_at": None, "result": hit}
        with self._lock:
            if self._shutdown:
                return {"key": key, "state": "failed", "error": "server shutting down",
                        "progress": {"done": 0, "total": 0, "note": ""}, "started_at": None, "finished_at": None}
            running = self._jobs.get(key)
            if running is not None:
                return running.snapshot()
            job = _Job(key, fn, cache, cache_key, contextvars.copy_context())
            self._jobs[key] = job
            self._ensure_workers()
        self._queue.put(job)
        return job.snapshot()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(key) or self._done.get(key)
            return job.snapshot(with_result=True) if job is not None else None

    def wait(self, key: str, timeout: float) -> dict[str, Any] | None:
        """Block until the job with ``key`` finishes or ``timeout`` elapses; returns its state."""
        with self._lock:
            job = self._jobs.get(key) or self._done.get(key)
        if job is None:
            return None
        job.done_event.wait(timeout)
        return job.snapshot(with_result=True)

    def run_sync(self, key: str, fn: Callable[[_Progress], Any], *, cache: Any = None,
                 cache_key: str | None = None, timeout: float = 30.0) -> Any | None:
        """Submit (or join) a job and wait for it: the result, or None when it failed or is still building."""
        state = self.submit(key, fn, cache=cache, cache_key=cache_key)
        if state["state"] == "building":
            state = self.wait(key, timeout) or state
        return state.get("result") if state.get("state") == "done" else None

    def cancel(self, key: str) -> bool:
        with self._lock:
            job = self._jobs.get(key)
        if job is None:
            return False
        job.cancel_event.set()
        return True

    def forget(self, key: str) -> None:
        """Drop a finished job (a failure can then be retried at once)."""
        with self._lock:
            self._done.pop(key, None)

    def cancel_all(self) -> None:
        """Shutdown: cancel every queued and running job and stop the workers."""
        with self._lock:
            self._shutdown = True
            pending = list(self._jobs.values())
        for job in pending:
            job.cancel_event.set()
        for _ in self._threads:
            self._queue.put(None)

    def reset(self) -> None:
        """Tests: forget everything and accept jobs again."""
        self.cancel_all()
        with self._lock:
            self._jobs.clear()
            self._done.clear()
            self._threads = []
            self._queue = queue.Queue()
            self._shutdown = False

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "building": [j.snapshot() for j in self._jobs.values()],
                "done": sum(1 for j in self._done.values() if j.state == "done"),
                "failed": sum(1 for j in self._done.values() if j.state == "failed"),
                "workers": len([t for t in self._threads if t.is_alive()]),
            }


jobs = JobManager()


def retry_after_ms(state: dict[str, Any]) -> int:
    started = state.get("started_at") or time.time()
    age = max(0.0, time.time() - started)
    return int(min(5000, max(500, age * 250)))


def respond_job(key: str, fn: Callable[[_Progress], Any], *, cache: Any = None, cache_key: str | None = None,
                wait: float = 0.0, shape: Callable[[Any], Any] | None = None) -> Any:
    """Router helper: the job's result (200), a 202 progress body while building, or HTTP 500.

    ``wait`` lets cheap jobs answer in the same request (seconds to block before answering 202).
    ``shape(result)`` turns the stored result into the response body (e.g. filtering)."""
    state = jobs.submit(key, fn, cache=cache, cache_key=cache_key)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(key, wait) or state
    if state["state"] == "done":
        result = state.get("result")
        return shape(result) if shape is not None else result
    if state["state"] == "failed":
        if state.get("error") == "cancelled":
            jobs.forget(key)
        raise HTTPException(status_code=500, detail=f"background job failed: {state.get('error') or 'unknown error'}")
    body = {"state": "building", "key": key, "progress": state.get("progress") or {},
            "retry_after_ms": retry_after_ms(state)}
    return JSONResponse(body, status_code=202, headers={"Cache-Control": "no-store"})
