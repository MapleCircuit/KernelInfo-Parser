"""Full-screen live view of a long-running child process (git clone / fetch) inside curses.

`run_streaming_command()` spawns the process with stdout and stderr merged into a pipe, reads it
on a background thread, and repaints the last lines ~10 times per second. Carriage-return progress
updates (`Receiving objects:  42%\\r`) overwrite the current line instead of scrolling. `Esc`/`c`
asks to cancel; after exit the view waits for a key so the result stays readable.
"""

from __future__ import annotations

import curses
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from core.tui.theme import PAIR_ERROR, PAIR_HEADER, PAIR_MUTED, PAIR_SUCCESS, draw_box, get_color, safe_addstr
from core.tui.widgets import ModalDialog

MAX_LINES = 2000


@dataclass
class ProcessResult:
    returncode: int | None
    cancelled: bool = False
    lines: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.cancelled and self.error is None


class _LineBuffer:
    """Thread-safe line store where `\\r` rewrites the current line."""

    def __init__(self) -> None:
        self.lines: deque[str] = deque(maxlen=MAX_LINES)
        self.partial = ""
        self.lock = threading.Lock()
        # Set by `\r`: the current line stays visible until the next character replaces it
        self.rewind = False

    def feed(self, chunk: str) -> None:
        with self.lock:
            for ch in chunk:
                if ch == "\n":
                    self.lines.append(self.partial)
                    self.partial = ""
                    self.rewind = False
                elif ch == "\r":
                    self.rewind = True
                else:
                    if self.rewind:
                        self.partial = ""
                        self.rewind = False
                    self.partial += ch

    def snapshot(self) -> list[str]:
        with self.lock:
            out = list(self.lines)
            if self.partial:
                out.append(self.partial)
            return out


def _reader(stream: Any, buf: _LineBuffer) -> None:
    try:
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            buf.feed(chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk)
    except Exception:  # noqa: BLE001
        pass


def _draw(stdscr: Any, title: str, command: str, lines: list[str], footer: str, footer_attr: int) -> None:
    try:
        stdscr.erase()
    except Exception:
        pass
    try:
        max_y, max_x = stdscr.getmaxyx()
    except Exception:
        max_y, max_x = 24, 80
    draw_box(stdscr, 0, 0, max_y - 1, max_x, title=f" {title} ", color_pair=PAIR_HEADER)
    safe_addstr(stdscr, 1, 2, f"$ {command}"[: max_x - 4], get_color(PAIR_MUTED))
    body_h = max(1, max_y - 4)
    visible = lines[-body_h:]
    for i, line in enumerate(visible):
        safe_addstr(stdscr, 2 + i, 2, line.expandtabs(4)[: max_x - 4])
    safe_addstr(stdscr, max_y - 1, 0, f" {footer} ".ljust(max_x - 1)[: max_x - 1], footer_attr | curses.A_REVERSE)
    try:
        stdscr.refresh()
    except Exception:
        pass


def run_streaming_command(
    stdscr: Any,
    argv: list[str],
    title: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    wait_for_key: bool = True,
    popen: Callable[..., Any] = subprocess.Popen,
) -> ProcessResult:
    """Run `argv`, showing its merged output live; returns the exit status and captured lines."""
    command = " ".join(argv)
    buf = _LineBuffer()
    try:
        proc = popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return ProcessResult(returncode=None, error=f"Failed to start '{argv[0]}': {exc}")

    reader = threading.Thread(target=_reader, args=(proc.stdout, buf), daemon=True)
    reader.start()

    cancelled = False
    started = time.monotonic()
    try:
        stdscr.timeout(100)
    except Exception:
        pass
    try:
        while proc.poll() is None:
            elapsed = int(time.monotonic() - started)
            _draw(stdscr, title, command, buf.snapshot(), f"Running... {elapsed}s   [Esc/c] Cancel",
                  get_color(PAIR_MUTED))
            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                ch = 27
            except Exception:
                ch = -1
            if ch in (27, ord("c"), ord("C")):
                try:
                    stdscr.timeout(-1)
                except Exception:
                    pass
                choice = ModalDialog.show_dialog(
                    stdscr, "Cancel Operation", f"Stop the running command?\n{command}",
                    buttons=["Keep Running", "Stop"], default_button=0,
                )
                try:
                    stdscr.timeout(100)
                except Exception:
                    pass
                if choice == "Stop" and proc.poll() is None:
                    cancelled = True
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    break
        reader.join(timeout=2)
    finally:
        if proc.stdout is not None and not reader.is_alive():
            try:
                proc.stdout.close()
            except Exception:
                pass
        try:
            stdscr.timeout(-1)
        except Exception:
            pass

    rc = proc.returncode
    lines = buf.snapshot()
    result = ProcessResult(returncode=rc, cancelled=cancelled, lines=lines)
    if wait_for_key:
        if cancelled:
            footer, attr = "Cancelled.  Press any key to continue", get_color(PAIR_ERROR)
        elif rc == 0:
            footer, attr = "Completed successfully.  Press any key to continue", get_color(PAIR_SUCCESS)
        else:
            footer, attr = f"Failed (exit {rc}).  Press any key to continue", get_color(PAIR_ERROR)
        _draw(stdscr, title, command, lines, footer, attr)
        try:
            stdscr.getch()
        except Exception:
            pass
    return result
