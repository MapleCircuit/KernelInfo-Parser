"""parser/mailing_list/ml_server.py - Standalone query micro-daemon for LKML / public-inbox.

Can be run on the remote mailing list server (e.g. 192.168.2.30) or locally.
Listens on a TCP port (default: 8192) and serves batch commit resolution queries.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
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


DAEMON_VERSION = "1.3.0"


class MLServerRequestHandler(BaseHTTPRequestHandler):
    """Handles HTTP health and batch resolution requests."""

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
        if self.path == "/health" or self.path == "/":
            store = getattr(self.resolver, "store", None)
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
        else:
            self._send_json(404, {"error": "Not found"})

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
    httpd = HTTPServer(server_address, MLServerRequestHandler)
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
