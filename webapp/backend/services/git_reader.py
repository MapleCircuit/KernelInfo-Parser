"""webapp/backend/services/git_reader.py - Persistent Git Object Reader via git cat-file.

Provides a thread-safe, high-performance reader for version-specific file contents
and directory checks in the Linux Git repository, using persistent 'git cat-file --batch'.
"""
from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
import subprocess
import threading
from typing import Any

from webapp.backend.config import LINUX_REPO_DIR

logger = logging.getLogger(__name__)


class GitReader:
    """Thread-safe persistent git cat-file --batch manager for sub-millisecond source retrieval."""

    def __init__(self, repo_dir: Path | str | None = None) -> None:
        self.repo_dir = Path(repo_dir or LINUX_REPO_DIR).resolve()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None

    def _ensure_process(self) -> subprocess.Popen[bytes]:
        """Ensure the git cat-file process is active, spawning or respawning if necessary."""
        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        self._close_process()

        git_dir = self.repo_dir / ".git"
        if not git_dir.exists() and not self.repo_dir.exists():
            raise FileNotFoundError(f"Git repository directory not found: {self.repo_dir}")

        cmd = ["git", "-C", str(self.repo_dir), "cat-file", "--batch"]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,  # Unbuffered binary streams for precise byte reading
            )
            logger.info("Spawned persistent git cat-file --batch process (PID: %d) in %s", self._proc.pid, self.repo_dir)
        except Exception as e:
            logger.error("Failed to spawn git cat-file --batch in %s: %s", self.repo_dir, e)
            raise

        return self._proc

    def _close_process(self) -> None:
        """Terminate the active subprocess safely."""
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                if self._proc.stdout:
                    self._proc.stdout.close()
                if self._proc.stderr:
                    self._proc.stderr.close()
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            finally:
                self._proc = None

    def close(self) -> None:
        """Close the reader and release subprocess resources."""
        with self._lock:
            self._close_process()

    def read_object(self, rev: str, path: str) -> tuple[str, bytes | None, str | None]:
        """Read a Git object at rev:path.

        Args:
            rev: Git revision / tag name (e.g. 'v3.0', 'HEAD').
            path: Relative repository file or directory path.

        Returns:
            tuple: (object_type, content_bytes, object_sha1)
                   object_type is 'blob', 'tree', or 'missing'.
                   If missing, content_bytes and object_sha1 are None.
        """
        clean_path = path.strip().strip("/")
        if not clean_path:
            target_spec = rev
        else:
            target_spec = f"{rev}:{clean_path}"

        with self._lock:
            return self._read_with_retry(target_spec)

    def _read_with_retry(self, target_spec: str) -> tuple[str, bytes | None, str | None]:
        """Execute read with a single retry on pipe errors / process termination."""
        for attempt in range(2):
            try:
                proc = self._ensure_process()
                assert proc.stdin is not None
                assert proc.stdout is not None

                proc.stdin.write(f"{target_spec}\n".encode("utf-8"))
                proc.stdin.flush()

                header_bytes = proc.stdout.readline()
                if not header_bytes:
                    raise BrokenPipeError("Empty header read from git cat-file stdout.")

                header = header_bytes.decode("utf-8", errors="replace").strip()
                if header.endswith("missing"):
                    return ("missing", None, None)

                # Format: <sha1> <type> <size>
                parts = header.split()
                if len(parts) < 3:
                    raise ValueError(f"Malformed git cat-file header: {header}")

                sha1 = parts[0]
                obj_type = parts[1]
                size = int(parts[2])

                # Read exact byte payload
                data = bytearray()
                while len(data) < size:
                    chunk = proc.stdout.read(size - len(data))
                    if not chunk:
                        raise BrokenPipeError("Unexpected EOF while reading git blob content.")
                    data.extend(chunk)

                # Consume the trailing newline emitted by git cat-file --batch
                _ = proc.stdout.read(1)

                return (obj_type, bytes(data), sha1)

            except (BrokenPipeError, OSError, ValueError) as err:
                logger.warning(
                    "git cat-file error on '%s' (attempt %d/2): %s; respawning...",
                    target_spec,
                    attempt + 1,
                    err,
                )
                self._close_process()
                if attempt == 1:
                    raise

        return ("missing", None, None)

    def read_file(self, rev: str, path: str) -> tuple[bytes | None, bool, str | None]:
        """High-level file helper.

        Returns:
            (raw_bytes, is_directory, sha1)
            - If object is a tree: (None, True, sha1)
            - If object is a blob: (raw_bytes, False, sha1)
            - If missing: (None, False, None)
        """
        obj_type, data, sha1 = self.read_object(rev, path)
        if obj_type == "tree":
            return (None, True, sha1)
        if obj_type == "blob":
            return (data, False, sha1)
        return (None, False, None)


# Default module-level singleton instance
git_reader = GitReader()
atexit.register(git_reader.close)
