"""api/mcp_stdio.py - Local stdio transport for the KernelInfo-Parser MCP server.

Reads newline-delimited JSON-RPC 2.0 messages from stdin and writes one JSON-RPC response per line to
stdout, sharing the dispatcher and tool registry of `POST /api/mcp` (`api/mcp.py`) and the lifecycle
cleanup of the HTTP service (`api/app.py` `startup_resources()` / `shutdown_resources()`):

    python3 api/mcp_stdio.py [-c config.json] [--repo NAME]

stdout carries JSON-RPC frames only: file descriptor 1 is redirected to stderr before anything else is
imported, so logging, `print()`, warnings, and child processes all land on stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KernelInfo-Parser MCP server (stdio transport)")
    parser.add_argument("-c", "--config", type=str, default=None, help="Path to config.json file")
    parser.add_argument("--repo", type=str, default=None, help="Default repository for tool calls")
    return parser.parse_args(argv)


def claim_stdout() -> BinaryIO:
    """Reserve the real stdout for JSON-RPC frames and point fd 1 and `sys.stdout` at stderr."""
    sys.stdout.flush()
    frame_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    return os.fdopen(frame_fd, "wb")


def encode_frame(response: Any) -> bytes:
    """One newline-terminated JSON-RPC frame (compact JSON never contains a raw newline)."""
    return (json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def read_lines(stream: BinaryIO, limit: int) -> Iterator[bytes]:
    """Lines of `stream`; a line longer than `limit` bytes is yielded truncated to `limit + 1` and its rest skipped."""
    while True:
        line = stream.readline(limit + 1)
        if not line:
            return
        if len(line) > limit and not line.endswith(b"\n"):
            rest = line
            while rest and not rest.endswith(b"\n"):
                rest = stream.readline(limit + 1)
        yield line


def serve(lines: Iterable[bytes | str], out: BinaryIO) -> None:
    """Dispatch each non-blank stdin line and write its response (notifications produce no output)."""
    from api.mcp import INVALID_REQUEST, MAX_BODY_BYTES, handle_raw

    for line in lines:
        text = line.strip()
        if not text:
            continue
        if len(text) > MAX_BODY_BYTES:
            response: Any = {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "Message too large."}}
        else:
            response = handle_raw(text)
        if response is not None:
            out.write(encode_frame(response))
            out.flush()


def _terminate(_signum: int, _frame: Any) -> None:
    raise SystemExit(0)


def main(argv: list[str] | None = None) -> int:
    out = claim_stdout()
    signal.signal(signal.SIGTERM, _terminate)
    args = parse_args(argv)
    if args.config:
        from api.config import apply_config_file

        try:
            apply_config_file(args.config)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    from api.app import shutdown_resources, startup_resources
    from api.config import refresh_default_repo_dir, reset_active_repository, set_active_repository
    from api.mcp import MAX_BODY_BYTES
    from core.logging_setup import setup_api_logging

    token = None
    try:
        refresh_default_repo_dir()
        setup_api_logging(capture_stdio=False)
        if args.repo:
            token = set_active_repository(args.repo.strip())
            if token is None:
                print(f"Unknown repository: {args.repo!r}", file=sys.stderr)
                return 2
        startup_resources()
        serve(read_lines(sys.stdin.buffer, MAX_BODY_BYTES), out)
    except KeyboardInterrupt:
        pass
    finally:
        reset_active_repository(token)
        shutdown_resources()
        out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
