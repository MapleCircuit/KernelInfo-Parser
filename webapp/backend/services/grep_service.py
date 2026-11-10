"""webapp/backend/services/grep_service.py - Bounded `git grep` over a revision (webapp/API.md section 2.1.3).

One safe way to search source text: the revision is resolved to its commit SHA first, patterns are
passed after `-e` as fixed strings (`-F`) or POSIX ERE (`-E`, never PCRE), path filters get their
pathspec magic on the server, output is streamed and capped (matches, bytes, time), and match
ranges come from git's own colouring, so they follow git's regex semantics exactly.

    grep_service.grep("v3.0", "SYSCALL_DEFINE3(read", mode="fixed")
    -> {sha, matches: [{path, line, text, ranges, clipped}], files, total, truncated, timed_out, elapsed_ms}
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from typing import Any

from fastapi import HTTPException

from webapp.backend.config import RepoScopedCache, get_active_repo_name, get_repo_dir
from webapp.backend.security.git import git_pathspecs, validate_grep_pattern, validate_pathspecs
from webapp.backend.services.tree_resolver import git_tree_resolver

logger = logging.getLogger(__name__)

MAX_LINE_CHARS = 500
CLIP_CONTEXT = 120
CONCURRENCY = 2
BUSY_WAIT_S = 5.0

# Only the match colour is left on: every other slot prints as "normal" (no escape sequence)
_COLOR_SLOTS = ("filename", "lineNumber", "column", "separator", "context", "function", "selected", "matchContext")
_MATCH_COLOR = "bold red"
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def _color_args() -> list[str]:
    args = []
    for slot in _COLOR_SLOTS:
        args += ["-c", f"color.grep.{slot}=normal"]
    args += ["-c", f"color.grep.matchSelected={_MATCH_COLOR}", "-c", "grep.lineNumber=false",
             "-c", "grep.column=false", "-c", "grep.fullName=false", "-c", "grep.patternType=basic"]
    return args


def parse_colored_line(raw: str) -> tuple[str, list[list[int]]]:
    """Strip SGR sequences from one `--color=always` line: (plain text, [[start, end], ...]).

    A match starts at a colour sequence other than a reset and ends at the next reset (`ESC[m`)."""
    text_parts: list[str] = []
    ranges: list[list[int]] = []
    pos = 0
    out_len = 0
    open_at: int | None = None
    for m in _SGR_RE.finditer(raw):
        chunk = raw[pos:m.start()]
        text_parts.append(chunk)
        out_len += len(chunk)
        code = m.group(0)
        if code in ("\x1b[m", "\x1b[0m"):
            if open_at is not None:
                if out_len > open_at:
                    ranges.append([open_at, out_len])
                open_at = None
        elif open_at is None:
            open_at = out_len
        pos = m.end()
    tail = raw[pos:]
    text_parts.append(tail)
    out_len += len(tail)
    if open_at is not None and out_len > open_at:
        ranges.append([open_at, out_len])
    return "".join(text_parts), ranges


def clip_line(text: str, ranges: list[list[int]], limit: int = MAX_LINE_CHARS) -> tuple[str, list[list[int]], bool]:
    """Clip a long line around its first match; ranges are shifted (and dropped when cut away)."""
    if len(text) <= limit:
        return text, ranges, False
    first = ranges[0][0] if ranges else 0
    start = max(0, min(first - CLIP_CONTEXT, len(text) - limit))
    end = start + limit
    clipped = text[start:end]
    shifted = [[max(0, a - start), min(limit, b - start)] for a, b in ranges if b > start and a < end]
    return clipped, [r for r in shifted if r[1] > r[0]], True


_GIT_VERSION: tuple[int, int] | None = None
_VERSION_LOCK = threading.Lock()


def git_supports_max_count() -> bool:
    """`git grep --max-count` exists since Git 2.38; older gits count per file in Python instead."""
    global _GIT_VERSION
    with _VERSION_LOCK:
        if _GIT_VERSION is None:
            try:
                out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5).stdout
                m = re.search(r"(\d+)\.(\d+)", out)
                _GIT_VERSION = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            except (OSError, subprocess.SubprocessError):
                _GIT_VERSION = (0, 0)
        return _GIT_VERSION >= (2, 38)


class GrepService:
    """`git grep` with validation, caps, a per-repository semaphore and a bounded result cache."""

    def __init__(self) -> None:
        self._cache: RepoScopedCache = RepoScopedCache(maxsize=128)
        self._sems: dict[str, threading.BoundedSemaphore] = {}
        self._sem_lock = threading.Lock()

    def _semaphore(self) -> threading.BoundedSemaphore:
        name = get_active_repo_name()
        with self._sem_lock:
            sem = self._sems.get(name)
            if sem is None:
                sem = self._sems[name] = threading.BoundedSemaphore(CONCURRENCY)
            return sem

    def grep(self, rev: str, pattern: str, *, mode: str = "fixed", ignore_case: bool = False, word: bool = False,
             paths: Any = (), max_matches: int = 2000, max_per_file: int = 50, max_bytes: int = 8 << 20,
             timeout: float = 20.0, busy_wait: float = BUSY_WAIT_S) -> dict[str, Any]:
        pattern = validate_grep_pattern(pattern)
        if mode not in ("fixed", "regex"):
            raise HTTPException(status_code=400, detail="mode must be 'fixed' or 'regex'.")
        specs = validate_pathspecs(paths)
        sha = git_tree_resolver.resolve_commit_sha(rev)
        if not sha:
            raise HTTPException(status_code=404, detail=f"Revision not found: {rev}")
        max_matches = max(1, min(int(max_matches), 300000))  # endpoints cap their own requests (Query le=20000)
        max_per_file = max(1, min(int(max_per_file), 5000))
        key = (sha, pattern, mode, bool(ignore_case), bool(word), tuple(specs), max_matches, max_per_file, max_bytes)
        hit = self._cache.get(key)
        if hit is not None:
            logger.debug("grep cache hit %s %r", sha[:12], pattern)
            return {**hit, "cached": True}
        sem = self._semaphore()
        if not sem.acquire(timeout=busy_wait):
            raise HTTPException(status_code=503, detail="search busy, retry")
        try:
            result = self._run(sha, pattern, mode, ignore_case, word, specs, max_matches, max_per_file, max_bytes, timeout)
        finally:
            sem.release()
        if not result["timed_out"]:
            self._cache[key] = result
        logger.debug("grep %s %r: %d matches in %d files, %d ms%s%s", sha[:12], pattern, result["total"],
                     result["files"], result["elapsed_ms"], " truncated" if result["truncated"] else "",
                     " timed out" if result["timed_out"] else "")
        return result

    def _run(self, sha: str, pattern: str, mode: str, ignore_case: bool, word: bool, specs: list[str],
             max_matches: int, max_per_file: int, max_bytes: int, timeout: float) -> dict[str, Any]:
        repo = get_repo_dir()
        cmd = ["git", "-C", str(repo), *_color_args(), "grep", "-n", "-I", "-z", "--color=always",
               "-F" if mode == "fixed" else "-E"]
        if ignore_case:
            cmd.append("-i")
        if word:
            cmd.append("-w")
        native_max = git_supports_max_count()
        if native_max:
            cmd.append(f"--max-count={max_per_file}")
        cmd += ["-e", pattern, sha, "--", *git_pathspecs(specs)]
        started = time.monotonic()
        matches: list[dict[str, Any]] = []
        per_file: dict[str, int] = {}
        truncated = timed_out = False
        read_bytes = 0
        prefix = f"{sha}:".encode()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"git grep unavailable: {exc}") from exc
        expired = threading.Event()

        def _expire() -> None:
            expired.set()
            try:
                proc.kill()
            except OSError:
                pass

        timer = threading.Timer(timeout, _expire)
        timer.daemon = True
        timer.start()
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                read_bytes += len(raw)
                if read_bytes > max_bytes:
                    truncated = True
                    break
                parts = raw.rstrip(b"\n").split(b"\0", 2)
                if len(parts) != 3:
                    continue
                name, lineno, body = parts
                if name.startswith(prefix):
                    name = name[len(prefix):]
                path = name.decode("utf-8", errors="replace")
                count = per_file.get(path, 0)
                if count >= max_per_file:
                    continue  # older git: no --max-count
                try:
                    line = int(lineno)
                except ValueError:
                    continue
                text, ranges = parse_colored_line(body.decode("utf-8", errors="replace"))
                text, ranges, clipped = clip_line(text, ranges)
                per_file[path] = count + 1
                matches.append({"path": path, "line": line, "text": text, "ranges": ranges, "clipped": clipped})
                if len(matches) >= max_matches:
                    truncated = True
                    break
        finally:
            timer.cancel()
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                _, err = proc.communicate(timeout=5)
            except (subprocess.SubprocessError, ValueError):
                err = b""
        timed_out = expired.is_set()
        if not truncated and not timed_out and proc.returncode not in (0, 1):
            message = (err or b"").decode("utf-8", errors="replace").strip().splitlines()
            detail = message[-1] if message else f"git grep exited with {proc.returncode}"
            if "regex" in detail.lower() or "invalid" in detail.lower() or "unmatched" in detail.lower():
                raise HTTPException(status_code=400, detail=f"Invalid pattern: {detail[:200]}")
            raise HTTPException(status_code=500, detail=detail[:300])
        return {
            "sha": sha,
            "matches": matches,
            "files": len(per_file),
            "total": len(matches),
            "truncated": truncated,
            "timed_out": timed_out,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    def clear(self) -> None:
        self._cache.clear()


grep_service = GrepService()
