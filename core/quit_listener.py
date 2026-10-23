"""core/quit_listener.py - Interactive "finish current version, then quit" key listener.

While the parser runs in an interactive terminal, a background thread reads single key presses
(terminal in cbreak mode: no line buffering, no echo, Ctrl+C still delivers SIGINT). Pressing
`q` sets `requested`; the release pipeline checks it between versions, so the version being
parsed always completes and is committed before the program exits.
"""
from __future__ import annotations

import atexit
import logging
import os
import select
import sys
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)


class GracefulQuitListener:
    """Listen for a quit key on stdin without blocking the parser."""

    def __init__(self, keys: str = "qQ", on_request: Callable[[], None] | None = None) -> None:
        self.keys = keys
        self.on_request = on_request
        self.requested = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._saved_attrs: list | None = None
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        """Only interactive POSIX terminals, and never under a debugger (it needs stdin itself)."""
        if os.name != "posix" or sys.gettrace() is not None:
            return False
        try:
            return sys.stdin is not None and sys.stdin.isatty()
        except (AttributeError, ValueError):
            return False

    def start(self) -> bool:
        """Put the terminal in cbreak mode and start listening; returns False when not interactive."""
        if self._thread is not None or not self.available():
            return False
        import termios
        import tty

        try:
            self._fd = sys.stdin.fileno()
            self._saved_attrs = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except (termios.error, OSError, ValueError) as e:
            logger.debug("Quit listener unavailable: %s", e)
            self._fd = None
            self._saved_attrs = None
            return False

        atexit.register(self.stop)
        self._thread = threading.Thread(target=self._run, name="quit-listener", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.25)
                if not ready:
                    continue
                ch = os.read(self._fd, 1).decode("utf-8", errors="ignore")
            except (OSError, ValueError):
                return
            if not ch or ch not in self.keys:
                continue
            if self.requested.is_set():
                logger.info("Quit already requested; finishing the current version. Press Ctrl+C to abort immediately.")
                continue
            self.requested.set()
            if self.on_request is not None:
                try:
                    self.on_request()
                except Exception as e:  # noqa: BLE001 - a logging callback must never kill the listener
                    logger.debug("Quit callback failed: %s", e)

    def stop(self) -> None:
        """Stop listening and restore the terminal settings (idempotent)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            if self._fd is not None and self._saved_attrs is not None:
                import termios

                try:
                    termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attrs)
                except (termios.error, OSError, ValueError):
                    pass
            self._saved_attrs = None
