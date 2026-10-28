"""parser/mailing_list/ml_server.py - Standalone query micro-daemon for LKML / public-inbox.

Can be run on the remote mailing list server (e.g. 192.168.2.30) or locally.
Listens on a TCP port (default: 8192) and serves batch commit resolution queries.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import signal
import sys
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


DAEMON_VERSION = "1.4.1"


class MLServerRequestHandler(BaseHTTPRequestHandler):
    """Handles HTTP health, batch resolution, thread, and email search requests."""

    @property
    def resolver(self) -> KernelCommitEmailResolver:
        if hasattr(self.server, "resolver") and self.server.resolver:
            return self.server.resolver
        return getattr(self.__class__, "resolver", None)

    def _send_json(self, status_code: int, data: dict[str, Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        try:
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            store = getattr(self.resolver, "store", None)

            if path == "/health" or path == "/":
                storage_dir = str(getattr(store, "root", "")) if store else ""
                idx_dir = str(getattr(store, "idx_dir", "")) if store else ""
                shards = int(getattr(store, "shard_count", 0)) if store else 0
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "version": DAEMON_VERSION,
                        "storage_dir": storage_dir,
                        "idx_dir": idx_dir,
                        "shards": shards,
                        "has_xapian": HAS_XAPIAN,
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
                thread_data = store.get_thread_messages(tid)
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
        except Exception as exc:
            logger.error("Error processing GET %s: %s", self.path, exc, exc_info=True)
            self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            self._send_json(400, {"error": "Missing or empty request body"})
            return

        try:
            body = self.rfile.read(content_length)
            req_data = json.loads(body.decode("utf-8"))
        except Exception as exc:
            self._send_json(400, {"error": f"Invalid JSON: {exc}"})
            return

        store = getattr(self.resolver, "store", None)

        try:
            if self.path == "/resolve_batch":
                raw_commits = req_data.get("commits", [])
                feats = [CommitFeatures.from_dict(c) for c in raw_commits if isinstance(c, dict)]
                results = self.resolver.resolve_batch(feats)
                self._send_json(
                    200,
                    {
                        "count": len(results),
                        "results": {sha: ctx.to_dict() for sha, ctx in results.items()},
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
                thread_data = store.get_thread_messages(tid)
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
        except Exception as exc:
            logger.error("Error processing POST %s: %s", self.path, exc, exc_info=True)
            self._send_json(500, {"error": f"Internal server error: {exc}"})


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
) -> None:
    """Initialize public-inbox store and run HTTP server."""
    logger.info("Initializing PublicInboxLocalStore from %s...", storage_dir)
    store = PublicInboxLocalStore(storage_dir)
    resolver = KernelCommitEmailResolver(store, kernel_git_dir=git_dir)

    MLServerRequestHandler.resolver = resolver

    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, MLServerRequestHandler)
    httpd.daemon_threads = True
    logger.info(
        "Mailing List Query Server listening on http://%s:%d (shards: %d, xapian: %s)",
        host,
        port,
        store.shard_count,
        HAS_XAPIAN,
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
    args = parser.parse_args()

    run_server(
        storage_dir=args.storage_dir,
        host=args.host,
        port=args.port,
        git_dir=args.git_dir,
    )


if __name__ == "__main__":
    main()
