"""Terminal output capture for the curses TUI.

While curses owns the terminal, anything else written to it (log records, `print()`, stderr of
child processes such as `ssh`) lands on top of the drawn screen. `TerminalOutputCapture` diverts
those writers for the lifetime of the TUI and keeps what they produced, so it can be replayed on
the restored terminal once curses has exited.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from typing import Any, TextIO

_STD_STREAM_NAMES = ("<stdout>", "<stderr>")


class _CaptureHandler(logging.Handler):
    """Logging handler that stores records for later replay."""

    def __init__(self, capture: "TerminalOutputCapture") -> None:
        super().__init__(level=logging.NOTSET)
        self._capture = capture

    def emit(self, record: logging.LogRecord) -> None:
        self._capture.records.append(record)
        self._capture.output_count += 1


class _CaptureStream:
    """File-like replacement for `sys.stdout` / `sys.stderr` that buffers writes."""

    def __init__(self, capture: "TerminalOutputCapture", original: TextIO | None) -> None:
        self._capture = capture
        self._original = original
        self.encoding = getattr(original, "encoding", None) or "utf-8"

    def write(self, data: str) -> int:
        if data:
            self._capture.text.append(data)
            self._capture.output_count += 1
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        if self._original is None:
            raise OSError("captured stream has no file descriptor")
        return self._original.fileno()


def _is_terminal_handler(handler: logging.Handler) -> bool:
    """True for a StreamHandler that writes to the process stdout / stderr (not a file)."""
    if not isinstance(handler, logging.StreamHandler) or isinstance(handler, logging.FileHandler):
        return False
    stream = getattr(handler, "stream", None)
    if stream is None:
        return False
    if stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        return True
    return getattr(stream, "name", None) in _STD_STREAM_NAMES


class TerminalOutputCapture:
    """Context manager diverting logging, `sys.stdout`/`sys.stderr` and file descriptor 2.

    - Log handlers that write to the terminal are detached (file handlers keep working) and a
      single capture handler on the root logger, also installed as `logging.lastResort`,
      collects every record.
    - `sys.stdout` and `sys.stderr` are swapped for buffers, so `print()` never reaches the
      terminal (curses itself writes to file descriptor 1 directly and is unaffected).
    - File descriptor 2 is redirected to a temporary file, catching stderr of child processes
      and C extensions.

    `output_count` grows with every captured write, letting the TUI repaint after leaks.
    `replay()` re-emits the captured records through the restored handlers and writes the
    captured text to the restored stderr; call it after curses has exited.
    """

    def __init__(self, redirect_fd: bool = True) -> None:
        self.redirect_fd = redirect_fd
        self.records: list[logging.LogRecord] = []
        self.text: list[str] = []
        self.output_count = 0
        self._handler = _CaptureHandler(self)
        self._detached: list[tuple[logging.Logger, logging.Handler]] = []
        self._saved_last_resort: logging.Handler | None = None
        self._saved_streams: tuple[Any, Any] | None = None
        self._saved_fd2: int | None = None
        self._fd2_file: Any = None
        self._active = False

    # --- context manager -------------------------------------------------------------

    def __enter__(self) -> "TerminalOutputCapture":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> None:
        if self._active:
            return
        self._active = True

        loggers: list[logging.Logger] = [logging.getLogger()]
        loggers += [lg for lg in logging.Logger.manager.loggerDict.values() if isinstance(lg, logging.Logger)]
        for lg in loggers:
            for handler in list(lg.handlers):
                if _is_terminal_handler(handler):
                    lg.removeHandler(handler)
                    self._detached.append((lg, handler))
        logging.getLogger().addHandler(self._handler)
        self._saved_last_resort = logging.lastResort
        logging.lastResort = self._handler

        self._saved_streams = (sys.stdout, sys.stderr)
        sys.stdout = _CaptureStream(self, self._saved_streams[0])  # type: ignore[assignment]
        sys.stderr = _CaptureStream(self, self._saved_streams[1])  # type: ignore[assignment]

        if self.redirect_fd:
            try:
                self._fd2_file = tempfile.TemporaryFile(mode="w+b")
                self._saved_fd2 = os.dup(2)
                os.dup2(self._fd2_file.fileno(), 2)
            except OSError:
                self._close_fd2_file()
                self._saved_fd2 = None

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False

        if self._saved_fd2 is not None:
            try:
                os.dup2(self._saved_fd2, 2)
                os.close(self._saved_fd2)
            except OSError:
                pass
            self._saved_fd2 = None
            try:
                self._fd2_file.seek(0)
                data = self._fd2_file.read().decode("utf-8", errors="replace")
                if data:
                    self.text.append(data)
            except (OSError, ValueError):
                pass
        self._close_fd2_file()

        if self._saved_streams is not None:
            sys.stdout, sys.stderr = self._saved_streams
            self._saved_streams = None

        logging.getLogger().removeHandler(self._handler)
        logging.lastResort = self._saved_last_resort
        for lg, handler in self._detached:
            if handler not in lg.handlers:
                lg.addHandler(handler)
        self._detached = []

    def _close_fd2_file(self) -> None:
        if self._fd2_file is not None:
            try:
                self._fd2_file.close()
            except OSError:
                pass
            self._fd2_file = None

    # --- replay ----------------------------------------------------------------------

    def fd_output_size(self) -> int:
        """Bytes written to the redirected file descriptor 2 so far (0 when not redirected)."""
        if self._fd2_file is None:
            return 0
        try:
            return os.fstat(self._fd2_file.fileno()).st_size
        except OSError:
            return 0

    def replay(self) -> None:
        """Re-emit captured log records and text on the restored terminal, then forget them."""
        records, text = self.records, self.text
        self.records, self.text = [], []
        for record in records:
            logging.getLogger(record.name).handle(record)
        joined = "".join(text)
        if joined:
            sys.stderr.write(joined if joined.endswith("\n") else joined + "\n")
            sys.stderr.flush()
