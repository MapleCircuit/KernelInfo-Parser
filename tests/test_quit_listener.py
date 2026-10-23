"""tests/test_quit_listener.py - Interactive 'q' (finish current version, then quit) listener."""
from __future__ import annotations

import os
import pty
import select
import sys
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Child program: starts the listener on its pty, reports when ready, then waits for 'q'
CHILD = textwrap.dedent(
    """
    import sys, termios, time
    sys.path.insert(0, ROOT)
    from core.quit_listener import GracefulQuitListener
    before = termios.tcgetattr(0)
    hits = []
    listener = GracefulQuitListener(on_request=lambda: hits.append(1))
    print("STARTED", listener.start(), flush=True)
    deadline = time.time() + 5
    while not listener.requested.is_set() and time.time() < deadline:
        time.sleep(0.05)
    listener.stop()
    print("REQUESTED", listener.requested.is_set(), "CALLBACKS", len(hits), flush=True)
    print("RESTORED", termios.tcgetattr(0) == before, flush=True)
    """
).replace("ROOT", repr(ROOT))


def _read_until(fd: int, marker: bytes, timeout: float = 10.0) -> bytes:
    buf = b""
    end = time.time() + timeout
    while marker not in buf and time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
    return buf


@unittest.skipUnless(os.name == "posix", "pty-based test requires POSIX")
class TestGracefulQuitListener(unittest.TestCase):
    def run_child(self, keys: bytes) -> str:
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, "-c", CHILD])
        try:
            out = _read_until(fd, b"STARTED")
            out += _read_until(fd, b"\n", timeout=2)
            for k in keys:
                os.write(fd, bytes([k]))
                time.sleep(0.1)
            out += _read_until(fd, b"RESTORED")
            out += _read_until(fd, b"\n", timeout=2)
            return out.decode("utf-8", errors="replace")
        finally:
            os.waitpid(pid, 0)
            os.close(fd)

    def test_q_requests_quit_and_restores_terminal(self) -> None:
        out = self.run_child(b"xq")
        self.assertIn("STARTED True", out)
        self.assertIn("REQUESTED True CALLBACKS 1", out)
        self.assertIn("RESTORED True", out)

    def test_other_keys_are_ignored(self) -> None:
        out = self.run_child(b"abc")
        self.assertIn("REQUESTED False", out)
        self.assertIn("RESTORED True", out)

    def test_not_interactive_does_not_start(self) -> None:
        from core.quit_listener import GracefulQuitListener
        # The unittest runner's stdin is not a pty (or a debugger is attached): listener must stay off
        if GracefulQuitListener.available():
            self.skipTest("stdin is an interactive terminal")
        self.assertFalse(GracefulQuitListener().start())


if __name__ == "__main__":
    unittest.main()
