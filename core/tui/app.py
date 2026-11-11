"""Application controller and screen stack orchestrator for the KernelInfo-Parser TUI."""

import copy
import curses
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from core.config import DEFAULT_CONFIG, REPO_ROOT, find_config_path
from core.tui.screens.base import BaseScreen
from core.tui.output_capture import TerminalOutputCapture
from core.tui.screens.main_menu import MainMenuScreen
from core.tui.theme import init_theme

logger = logging.getLogger(__name__)


class TUIApp:
    """Main application controller managing the curses lifecycle and screen stack."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        if config_path:
            self.config_path = Path(config_path).resolve()
        else:
            resolved = find_config_path(None)
            self.config_path = Path(resolved).resolve() if resolved is not None else (REPO_ROOT / "config.json")

        self.config: dict[str, Any] = self._load_initial_config()
        self.initial_config: dict[str, Any] = copy.deepcopy(self.config)
        self.screen_stack: list[BaseScreen] = []
        self.is_running: bool = False
        self.should_run_pipeline: bool = False
        # CLI argument overrides handed to main.py when the TUI exits to run the pipeline
        # (e.g. {"ref": ["v6.1"], "single_version": True} or {"reset": True, "reset_only": True})
        self.launch_args: dict[str, Any] = {}
        self.is_dirty: bool = False
        self.stdscr: Any = None
        # UI state (selected rows, focused widget) per screen key, restored when a screen re-opens
        self.view_state: dict[str, dict[str, Any]] = {}
        # Output diverted away from the terminal while curses runs (replayed by run_config_tui)
        self.output_capture: TerminalOutputCapture | None = None

    def _load_initial_config(self) -> dict[str, Any]:
        """Load configuration from file, or return a copy of DEFAULT_CONFIG."""
        if self.config_path.is_file():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # Legacy top-level ssh_tunnel moves into database.ssh_tunnel (as core/config.py does)
                    legacy = data.get("ssh_tunnel")
                    db_data = data.get("database")
                    if isinstance(legacy, dict) and (db_data is None or isinstance(db_data, dict)):
                        db_data = data.setdefault("database", {})
                        if "ssh_tunnel" not in db_data:
                            db_data["ssh_tunnel"] = copy.deepcopy(legacy)
                    merged = copy.deepcopy(DEFAULT_CONFIG)
                    # Deep merge file data over defaults
                    self._deep_merge(merged, data)
                    return merged
            except Exception as exc:
                logger.warning("Failed to load existing config %s: %s; using defaults", self.config_path, exc)

        return copy.deepcopy(DEFAULT_CONFIG)

    @staticmethod
    def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
        """Merge source dict into target dict recursively."""
        for k, v in source.items():
            if isinstance(v, dict) and isinstance(target.get(k), dict):
                TUIApp._deep_merge(target[k], v)
            else:
                target[k] = copy.deepcopy(v)

    def current_screen(self) -> BaseScreen | None:
        """Return the active screen at the top of the stack."""
        if self.screen_stack:
            return self.screen_stack[-1]
        return None

    def push_screen(self, screen: BaseScreen) -> None:
        """Push a new screen onto the stack and activate it."""
        self.screen_stack.append(screen)
        screen.on_enter()

    def pop_screen(self) -> BaseScreen | None:
        """Pop and leave the active screen, restoring the previous one."""
        if not self.screen_stack:
            return None
        leaving = self.screen_stack.pop()
        leaving.on_leave()
        current = self.current_screen()
        if current is not None:
            current.on_enter()
        return leaving

    def exit(self) -> None:
        """Signal the event loop to stop."""
        self.is_running = False

    def disk_config(self) -> dict[str, Any]:
        """Configuration as currently saved on disk (defaults when the file is missing or unreadable).

        Launch actions that run with the saved `config.json` show their targets from this view, not
        from the in-memory edits.
        """
        return self._load_initial_config()

    def request_launch(self, save: bool = False, **launch_args: Any) -> tuple[bool, str]:
        """Exit the TUI and run the pipeline with `launch_args` applied as CLI overrides.

        With `save=False` the pipeline reads the saved `config.json`: in-memory edits are not written.
        Returns (ok, message); a failed save leaves the TUI running.
        """
        if save:
            ok, msg = self.save_config()
            if not ok:
                return False, msg
        self.launch_args = dict(launch_args)
        self.should_run_pipeline = True
        self.exit()
        return True, ""

    def _config_for_disk(self) -> dict[str, Any]:
        """Configuration as written to disk: `database.ssh_tunnel` is the only database tunnel section.

        The top-level `ssh_tunnel` key is a legacy alias of `database.ssh_tunnel`; it is left out so
        a stale copy never shadows the tunnel edited in the TUI.
        """
        data = dict(self.config)
        if isinstance(data.get("database"), dict) and isinstance(data["database"].get("ssh_tunnel"), dict):
            data.pop("ssh_tunnel", None)
        return data

    def save_config(self, target_path: Path | None = None) -> tuple[bool, str]:
        """Persist the active configuration atomically to disk."""
        target = (target_path or self.config_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target.with_suffix(".tmp")
        try:
            with open(tmp_target, "w", encoding="utf-8") as f:
                json.dump(self._config_for_disk(), f, indent=2)
                f.write("\n")
            os.replace(tmp_target, target)
            self.is_dirty = False
            self.initial_config = copy.deepcopy(self.config)
            logger.info("Saved configuration to %s", target)
            return True, f"Saved configuration to:\n{target}"
        except Exception as exc:
            logger.error("Failed to save config to %s: %s", target, exc)
            if tmp_target.exists():
                try:
                    tmp_target.unlink()
                except OSError:
                    pass
            return False, f"Failed to save configuration:\n{exc}"

    def run(self, stdscr: Any) -> bool:
        """Main curses event loop executed inside curses.wrapper.

        Logging, `print()` and stderr (including child processes) are diverted into
        `self.output_capture` for the duration of the loop, so nothing draws over the screen.
        """
        if self.output_capture is None:
            self.output_capture = TerminalOutputCapture()
        with self.output_capture:
            return self._run_loop(stdscr)

    def _output_seen(self) -> int:
        """Monotonic count of output captured so far (writes plus redirected stderr bytes)."""
        cap = self.output_capture
        if cap is None:
            return 0
        return cap.output_count + cap.fd_output_size()

    def _run_loop(self, stdscr: Any) -> bool:
        self.stdscr = stdscr
        self.is_running = True

        try:
            curses.curs_set(0)
        except Exception:
            pass

        init_theme()

        try:
            stdscr.keypad(True)
        except Exception:
            pass

        # Push the main menu screen initially
        self.push_screen(MainMenuScreen(stdscr, self))

        output_seen = self._output_seen()
        while self.is_running and self.screen_stack:
            active = self.current_screen()
            if active is None:
                break

            # Something wrote to the terminal behind curses' back: repaint every cell
            seen = self._output_seen()
            if seen != output_seen:
                output_seen = seen
                try:
                    stdscr.clearok(True)
                except Exception:
                    pass

            active.draw()

            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                break
            except Exception:
                ch = -1

            if ch == -1:
                continue

            # Handle global window resize
            if ch == curses.KEY_RESIZE:
                continue

            # Delegate to active screen
            active.handle_key(ch)

        return self.should_run_pipeline


# Signals whose disposition ncurses' initscr() replaces with its own C handlers when they are
# SIG_DFL; endwin() leaves those handlers installed (Python's signal.getsignal() does not see them).
_CURSES_SIGNALS: tuple[int, ...] = tuple(
    getattr(signal, name)
    for name in ("SIGTSTP", "SIGCONT", "SIGWINCH", "SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT")
    if hasattr(signal, name)
)


class TerminalState:
    """Terminal attributes and signal dispositions saved before curses starts, restored after it exits.

    `endwin()` restores the tty modes but keeps ncurses' signal handlers. Its SIGTSTP handler, on
    the following SIGCONT (Ctrl+Z then `fg`, also delivered to every forked parser worker), switches
    the terminal back to curses program mode: `ONLCR` off, so every later newline moves down without
    returning to column 0. `restore()` reinstalls the saved dispositions and tty attributes.
    """

    def __init__(self) -> None:
        self.fd: int | None = None
        self.attrs: list[Any] | None = None
        self.handlers: dict[int, Any] = {}

    @classmethod
    def save(cls) -> "TerminalState":
        state = cls()
        if threading.current_thread() is threading.main_thread():
            for sig in _CURSES_SIGNALS:
                try:
                    handler = signal.getsignal(sig)
                except (ValueError, OSError):
                    continue
                if handler is not None:
                    state.handlers[sig] = handler
        try:
            import termios

            for stream in (sys.__stdout__, sys.__stdin__):
                if stream is not None and stream.isatty():
                    state.fd = stream.fileno()
                    state.attrs = termios.tcgetattr(state.fd)
                    break
        except (ImportError, OSError, ValueError, AttributeError):
            state.fd = None
            state.attrs = None
        return state

    def restore(self) -> None:
        if threading.current_thread() is threading.main_thread():
            for sig, handler in self.handlers.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError, TypeError):
                    pass
        if self.fd is not None and self.attrs is not None:
            try:
                import termios

                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.attrs)
            except (ImportError, OSError, ValueError):
                pass


def run_config_tui_launch(config_path: str | Path | None = None) -> dict[str, Any] | None:
    """Run the configuration TUI and return the pipeline launch request.

    Returns:
        None when the user exits without running the pipeline; otherwise the CLI argument
        overrides to apply (`{}` for a plain run).
    """
    app = TUIApp(config_path)
    terminal_state = TerminalState.save()
    try:
        should_run = bool(curses.wrapper(app.run))
    except Exception as exc:
        terminal_state.restore()
        print(f"\n[!] Configuration TUI encountered an error: {exc}")
        return None
    finally:
        # Drop ncurses' signal handlers and the curses tty modes before anything else prints
        terminal_state.restore()
        # Show what was logged / printed while curses owned the terminal
        if app.output_capture is not None:
            app.output_capture.replay()
    return dict(app.launch_args) if should_run else None


def run_config_tui(config_path: str | Path | None = None) -> bool:
    """Public entrypoint to run the configuration TUI.

    Returns:
        bool: True if the user chose to run the pipeline, False otherwise.
    """
    return run_config_tui_launch(config_path) is not None
