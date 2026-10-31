"""parser/mailing_list/ml_server.py - Standalone query micro-daemon for LKML / public-inbox.

Can be run on the remote mailing list server (e.g. 192.168.2.30) or locally.
Listens on a TCP port (default: 8192) and serves batch commit resolution queries.
"""
from __future__ import annotations

import argparse
import errno
import faulthandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import resource
import signal
import sys
import threading
import time
from typing import Any

# Add repository root to path if executed standalone
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from parser.mailing_list.ml_resolver import KernelCommitEmailResolver
from parser.mailing_list.ml_store import HAS_XAPIAN, PublicInboxLocalStore
from parser.mailing_list.ml_types import CommitFeatures

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ml_server")


DAEMON_VERSION = "1.4.6"

# Seconds to keep retrying the listen bind while a killed predecessor releases the port.
# Kept short so a daemon that loses the race exits instead of lingering and later
# stealing the port from the daemon that won it.
BIND_RETRY_SECONDS = 5.0

# Resident memory ceiling. This daemon shares its host with the archive mirror and a
# database server, so it must never be the process that triggers the kernel OOM killer
# (which picks the largest victim, not the guilty one). Above the soft limit new heavy
# requests are refused with 503 so in-flight work can finish; above the hard limit the
# process exits and the client relaunches it.
DEFAULT_MAX_RSS_MB = 1024
SOFT_RSS_FRACTION = 0.75
# Heavy requests served at once. Xapian access is serialized anyway, so extra
# concurrency buys little and multiplies the per-request working set.
DEFAULT_MAX_CONCURRENT = 4
# Request threads get a small stack: the default 8MB per thread is pure waste here.
THREAD_STACK_BYTES = 512 * 1024
# How often the watchdog samples memory.
WATCHDOG_INTERVAL_SECONDS = 1.0

HEAVY_PATHS = ("/resolve_batch", "/resolve_commit", "/search", "/thread")


def current_rss_mb() -> float:
    """Resident set size of this process in MB (0.0 if unavailable)."""
    try:
        with open("/proc/self/statm", "rb") as fh:
            pages = int(fh.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0))
    except Exception:
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        except Exception:
            return 0.0


class MemoryGuard:
    """Keeps the daemon's footprint inside an explicit ceiling."""

    def __init__(self, max_rss_mb: float = DEFAULT_MAX_RSS_MB):
        self.max_rss_mb = float(max_rss_mb)
        self.soft_rss_mb = self.max_rss_mb * SOFT_RSS_FRACTION
        self._lock = threading.Lock()
        self._peak = 0.0

    def observe(self) -> float:
        """Record current RSS without enforcing, for reporting on /health."""
        rss = current_rss_mb()
        with self._lock:
            self._peak = max(self._peak, rss)
        return rss

    def check(self, label: str) -> float | None:
        """Return the current RSS if it exceeds the soft limit, else None.

        Exits the process when the hard limit is passed, so the host's OOM killer never
        has to choose between this daemon and its neighbours.
        """
        rss = current_rss_mb()
        with self._lock:
            self._peak = max(self._peak, rss)
        if rss >= self.max_rss_mb:
            logger.error(
                "Memory ceiling exceeded (%.0fMB >= %.0fMB) while serving %s; exiting so the "
                "client can relaunch a clean daemon.",
                rss, self.max_rss_mb, label,
            )
            os._exit(3)
        if rss >= self.soft_rss_mb:
            return rss
        return None

    @property
    def peak_mb(self) -> float:
        with self._lock:
            return self._peak

    def apply_heap_limit(self) -> None:
        """Cap the heap so a runaway allocation raises MemoryError instead of growing.

        RLIMIT_DATA covers the heap and private anonymous mappings — the memory a
        runaway query actually consumes — while leaving the file-backed mappings of the
        Xapian shards and SQLite database untouched. Headroom above the reported
        ceiling leaves room for the traceback and the error response.
        """
        try:
            cap = int((self.max_rss_mb + 256) * 1024 * 1024)
            soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
            if hard != resource.RLIM_INFINITY:
                cap = min(cap, hard)
            resource.setrlimit(resource.RLIMIT_DATA, (cap, hard))
            logger.info("Heap limit set to %dMB (RLIMIT_DATA)", cap >> 20)
        except (ValueError, OSError, resource.error) as exc:  # type: ignore[attr-defined]
            logger.warning("Could not set heap limit: %s", exc)

    def start_watchdog(self) -> None:
        """Sample memory continuously, not only at request boundaries.

        Admission checks and the per-commit budget cannot see a single request that
        allocates gigabytes in one operation, which is exactly how this daemon reached
        7GB. The watchdog samples independently, dumps every thread's stack the first
        time the soft limit is crossed (so the offending query is identifiable), and
        exits at the hard limit.
        """
        def _loop() -> None:
            dumped = False
            while True:
                time.sleep(WATCHDOG_INTERVAL_SECONDS)
                rss = self.observe()
                if rss >= self.max_rss_mb:
                    logger.error(
                        "Watchdog: %.0fMB exceeds the %.0fMB ceiling; dumping stacks and exiting.",
                        rss, self.max_rss_mb,
                    )
                    try:
                        faulthandler.dump_traceback(all_threads=True)
                    except Exception:
                        pass
                    sys.stderr.flush()
                    os._exit(3)
                if rss >= self.soft_rss_mb and not dumped:
                    dumped = True
                    logger.warning(
                        "Watchdog: %.0fMB is above the %.0fMB soft limit; stacks of all threads follow.",
                        rss, self.soft_rss_mb,
                    )
                    try:
                        faulthandler.dump_traceback(all_threads=True)
                        sys.stderr.flush()
                    except Exception:
                        pass
                elif rss < self.soft_rss_mb:
                    dumped = False

        threading.Thread(target=_loop, name="memory-watchdog", daemon=True).start()


class MLServerRequestHandler(BaseHTTPRequestHandler):
    """Handles HTTP health, batch resolution, thread, and email search requests."""

    @property
    def resolver(self) -> KernelCommitEmailResolver:
        if hasattr(self.server, "resolver") and self.server.resolver:
            return self.server.resolver
        return getattr(self.__class__, "resolver", None)

    @property
    def guard(self) -> MemoryGuard:
        return getattr(self.server, "memory_guard", None) or getattr(self.__class__, "memory_guard", None)

    @property
    def slots(self) -> Any:
        return getattr(self.server, "request_slots", None) or getattr(self.__class__, "request_slots", None)

    def _release_thread_resources(self) -> None:
        """Drop this request thread's SQLite handles so they do not accumulate."""
        store = getattr(self.resolver, "store", None)
        if store is not None and hasattr(store, "close_thread_handles"):
            try:
                store.close_thread_handles()
            except Exception as exc:
                logger.debug("Error releasing thread-local store handles: %s", exc)

    def _admit(self) -> bool:
        """Admit a heavy request, or answer 503 when the daemon is at its limits."""
        guard, slots = self.guard, self.slots
        if guard is not None:
            rss = guard.check(self.path)
            if rss is not None:
                logger.warning(
                    "Refusing %s: resident memory %.0fMB is above the %.0fMB soft limit",
                    self.path, rss, guard.soft_rss_mb,
                )
                self._send_json(503, {
                    "error": "Daemon is above its memory soft limit; retry with a smaller request",
                    "rss_mb": round(rss, 1),
                    "soft_limit_mb": round(guard.soft_rss_mb, 1),
                })
                return False
        if slots is not None and not slots.acquire(timeout=30.0):
            logger.warning("Refusing %s: all %d request slots are busy", self.path, DEFAULT_MAX_CONCURRENT)
            self._send_json(503, {"error": "Daemon is busy; retry shortly"})
            return False
        return True

    def _finish_heavy(self, acquired: bool) -> None:
        if acquired and self.slots is not None:
            self.slots.release()
        self._release_thread_resources()

    def _send_json(self, status_code: int, data: dict[str, Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError) as exc:
            # Client abandoned a slow request (its own timeout fired); not an error.
            logger.debug("Client disconnected before response for %s: %s", self.path, exc)

    def do_GET(self) -> None:
        import urllib.parse
        heavy = urllib.parse.urlparse(self.path).path in HEAVY_PATHS
        if heavy and not self._admit():
            return
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            store = getattr(self.resolver, "store", None)

            if path == "/health" or path == "/":
                storage_dir = str(getattr(store, "root", "")) if store else ""
                idx_dir = str(getattr(store, "idx_dir", "")) if store else ""
                shards = int(getattr(store, "shard_count", 0)) if store else 0
                guard = self.guard
                rss_now = guard.observe() if guard else current_rss_mb()
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "version": DAEMON_VERSION,
                        "storage_dir": storage_dir,
                        "idx_dir": idx_dir,
                        "shards": shards,
                        "has_xapian": HAS_XAPIAN,
                        "rss_mb": round(rss_now, 1),
                        "peak_rss_mb": round(guard.peak_mb, 1) if guard else None,
                        "max_rss_mb": round(guard.max_rss_mb, 1) if guard else None,
                    },
                )
            elif path == "/message":
                if not store:
                    self._send_json(503, {"error": "Store not initialized"})
                    return
                mid = qs.get("mid", [None])[0]
                num_str = qs.get("num", [None])[0]
                target = num_str if num_str else mid
                if not target:
                    self._send_json(400, {"error": "Missing 'mid' or 'num' query parameter"})
                    return
                detail = store.get_email_detail(target)
                if not detail:
                    self._send_json(404, {"error": f"Email '{target}' not found"})
                    return
                self._send_json(200, {"message": detail})
            elif path == "/thread":
                if not store:
                    self._send_json(503, {"error": "Store not initialized"})
                    return
                tid_str = qs.get("tid", ["0"])[0]
                try:
                    tid = int(tid_str)
                except ValueError:
                    self._send_json(400, {"error": "Invalid 'tid' query parameter"})
                    return
                t_limit = int(qs.get("limit", [0])[0] or 0)
                t_offset = int(qs.get("offset", [0])[0] or 0)
                thread_data = (
                    store.get_thread_messages(tid, limit=t_limit, offset=t_offset)
                    if t_limit > 0 else store.get_thread_messages(tid, offset=t_offset)
                )
                self._send_json(200, thread_data)
            elif path == "/search":
                if not store:
                    self._send_json(503, {"error": "Store not initialized"})
                    return
                q = qs.get("q", [""])[0]
                limit = int(qs.get("limit", [50])[0])
                offset = int(qs.get("offset", [0])[0])
                filter_type = qs.get("filter", ["all"])[0]
                results = store.search_emails(q, limit=limit, offset=offset, filter_type=filter_type)
                self._send_json(200, results)
            else:
                self._send_json(404, {"error": "Not found"})
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.debug("Client disconnected during GET %s: %s", self.path, exc)
        except MemoryError:
            logger.error("Out of memory serving GET %s; request refused", self.path)
            self._send_json(503, {"error": "Daemon ran out of memory for this request"})
        except Exception as exc:
            logger.error("Error processing GET %s: %s", self.path, exc, exc_info=True)
            self._send_json(500, {"error": str(exc)})
        finally:
            self._finish_heavy(heavy)

    MAX_REQUEST_BYTES = 64 << 20

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            self._send_json(400, {"error": "Missing or empty request body"})
            return
        if content_length > self.MAX_REQUEST_BYTES:
            # Refuse before reading: the body would be held in memory in full.
            self._send_json(413, {"error": f"Request body exceeds {self.MAX_REQUEST_BYTES} bytes"})
            return

        heavy = self.path in HEAVY_PATHS
        if heavy and not self._admit():
            return

        try:
            body = self.rfile.read(content_length)
            req_data = json.loads(body.decode("utf-8"))
            del body
        except Exception as exc:
            self._finish_heavy(heavy)
            self._send_json(400, {"error": f"Invalid JSON: {exc}"})
            return

        store = getattr(self.resolver, "store", None)

        try:
            if self.path == "/resolve_batch":
                raw_commits = req_data.get("commits", [])
                feats = [CommitFeatures.from_dict(c) for c in raw_commits if isinstance(c, dict)]

                # Honour the client's time budget: always answer before its socket
                # timeout fires, deferring whatever could not be resolved in time.
                try:
                    budget_s = float(req_data.get("budget_s", 0) or 0)
                except (TypeError, ValueError):
                    budget_s = 0.0
                deadline = time.monotonic() + budget_s if budget_s > 0 else None

                t0 = time.monotonic()
                guard = self.guard
                results, pending = self.resolver.resolve_batch_until(
                    feats,
                    deadline,
                    should_stop=(lambda: guard.check("/resolve_batch") is not None) if guard else None,
                )
                elapsed = time.monotonic() - t0
                logger.info(
                    "resolve_batch: %d commits -> %d matched, %d deferred in %.1fs (rss %.0fMB)",
                    len(feats),
                    len(results),
                    len(pending),
                    elapsed,
                    current_rss_mb(),
                )
                self._send_json(
                    200,
                    {
                        "count": len(results),
                        "results": {sha: ctx.to_dict() for sha, ctx in results.items()},
                        "pending": pending,
                        "elapsed_s": round(elapsed, 3),
                    },
                )
            elif self.path == "/resolve_commit":
                feat = CommitFeatures.from_dict(req_data)
                ctx = self.resolver.resolve(feat)
                self._send_json(
                    200,
                    {"result": ctx.to_dict() if ctx else None},
                )
            elif self.path == "/message":
                if not store:
                    self._send_json(503, {"error": "Store not initialized"})
                    return
                target = req_data.get("mid") or req_data.get("num")
                if not target:
                    self._send_json(400, {"error": "Missing 'mid' or 'num' in payload"})
                    return
                detail = store.get_email_detail(target)
                if not detail:
                    self._send_json(404, {"error": f"Email '{target}' not found"})
                    return
                self._send_json(200, {"message": detail})
            elif self.path == "/thread":
                if not store:
                    self._send_json(503, {"error": "Store not initialized"})
                    return
                tid = int(req_data.get("tid", 0))
                t_limit = int(req_data.get("limit", 0) or 0)
                t_offset = int(req_data.get("offset", 0) or 0)
                thread_data = (
                    store.get_thread_messages(tid, limit=t_limit, offset=t_offset)
                    if t_limit > 0 else store.get_thread_messages(tid, offset=t_offset)
                )
                self._send_json(200, thread_data)
            elif self.path == "/search":
                if not store:
                    self._send_json(503, {"error": "Store not initialized"})
                    return
                q = str(req_data.get("q", ""))
                limit = int(req_data.get("limit", 50))
                offset = int(req_data.get("offset", 0))
                filter_type = str(req_data.get("filter", "all"))
                results = store.search_emails(q, limit=limit, offset=offset, filter_type=filter_type)
                self._send_json(200, results)
            else:
                self._send_json(404, {"error": "Not found"})
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.debug("Client disconnected during POST %s: %s", self.path, exc)
        except MemoryError:
            logger.error("Out of memory serving POST %s; request refused", self.path)
            self._send_json(503, {"error": "Daemon ran out of memory for this request"})
        except Exception as exc:
            logger.error("Error processing POST %s: %s", self.path, exc, exc_info=True)
            self._send_json(500, {"error": f"Internal server error: {exc}"})
        finally:
            self._finish_heavy(heavy)


    def log_message(self, format: str, *args: Any) -> None:
        # Reduce verbosity for routine requests
        if args and str(args[0]).startswith("POST"):
            logger.info("%s - %s", self.address_string(), format % args)
        else:
            logger.debug("%s - %s", self.address_string(), format % args)


def run_server(
    storage_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8192,
    git_dir: str | Path | None = None,
    max_rss_mb: float = DEFAULT_MAX_RSS_MB,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> None:
    """Initialize public-inbox store and run HTTP server."""
    logger.info("Initializing PublicInboxLocalStore from %s...", storage_dir)
    store = PublicInboxLocalStore(storage_dir)
    resolver = KernelCommitEmailResolver(store, kernel_git_dir=git_dir)

    MLServerRequestHandler.resolver = resolver
    guard = MemoryGuard(max_rss_mb)
    guard.apply_heap_limit()
    guard.start_watchdog()
    faulthandler.enable()
    MLServerRequestHandler.memory_guard = guard
    MLServerRequestHandler.request_slots = threading.BoundedSemaphore(max(1, max_concurrent))
    # Request threads do not need the default 8MB stack each.
    try:
        threading.stack_size(THREAD_STACK_BYTES)
    except (ValueError, RuntimeError) as exc:
        logger.debug("Could not reduce thread stack size: %s", exc)

    # Bind with retries: a just-killed predecessor can still hold the listening socket
    # for a moment, and SO_REUSEADDR does not override a live listener.
    server_address = (host, port)
    httpd: ThreadingHTTPServer | None = None
    deadline = time.monotonic() + BIND_RETRY_SECONDS
    while True:
        try:
            httpd = ThreadingHTTPServer(server_address, MLServerRequestHandler)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                logger.error("Cannot bind %s:%d: %s", host, port, exc)
                store.close()
                raise
            logger.info("Port %d still held by a previous daemon; retrying bind...", port)
            time.sleep(0.5)
    httpd.daemon_threads = True
    logger.info(
        "Mailing List Query Server listening on http://%s:%d (shards: %d, xapian: %s, "
        "rss limit: %.0fMB soft / %.0fMB hard, %d concurrent)",
        host,
        port,
        store.shard_count,
        HAS_XAPIAN,
        guard.soft_rss_mb,
        guard.max_rss_mb,
        max_concurrent,
    )

    def _sig_handler(sig: int, frame: Any) -> None:
        logger.info("Shutting down mailing list server...")
        store.close()
        httpd.server_close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    try:
        httpd.serve_forever()
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Public-Inbox / LKML Commit Query Micro-Daemon for KernelInfo-Parser"
    )
    parser.add_argument(
        "--storage-dir",
        default=os.environ.get("ML_STORAGE_DIR", "/srv/lore"),
        help="Path to public-inbox root or index directory (default: /srv/lore)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ML_PORT", 8192)),
        help="Port to bind to (default: 8192)",
    )
    parser.add_argument(
        "--git-dir",
        default=None,
        help="Optional path to local Linux git repository",
    )
    parser.add_argument(
        "--max-rss-mb",
        type=float,
        default=float(os.environ.get("ML_MAX_RSS_MB", DEFAULT_MAX_RSS_MB)),
        help=(
            "Resident memory ceiling in MB (default: %(default)s). Heavy requests are "
            "refused above 75%% of it, and the daemon exits if it is reached, so it "
            "never becomes the host's OOM victim."
        ),
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=int(os.environ.get("ML_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT)),
        help="Heavy requests served concurrently (default: %(default)s)",
    )
    args = parser.parse_args()

    run_server(
        storage_dir=args.storage_dir,
        host=args.host,
        port=args.port,
        git_dir=args.git_dir,
        max_rss_mb=args.max_rss_mb,
        max_concurrent=args.max_concurrent,
    )


if __name__ == "__main__":
    main()
