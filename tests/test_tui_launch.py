"""Unit tests for TUI pipeline launch actions: Run Saved, Table Reset, and the launch hand-off."""

import json
import os
import select
import signal
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.tui.app import TUIApp, run_config_tui, run_config_tui_launch
from core.tui.screens import launch as launch_mod
from core.tui.screens import table_reset
from core.tui.screens.main_menu import MainMenuScreen


class _Prompt:
    """ModalTextInputDialog stand-in returning a fixed answer."""

    answer: str | None = ""

    def __init__(self, *args, **kwargs):
        pass

    def show(self):
        return type(self).answer


def _prompt_with(answer):
    return type("Prompt", (_Prompt,), {"answer": answer})


class LaunchTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.config_path.write_text(json.dumps({"database": {"host": "disk-host", "database": "disk_db"}}))
        self.app = TUIApp(self.config_path)
        self.app.is_running = True
        self.stdscr = MagicMock()
        self.stdscr.getmaxyx.return_value = (30, 100)

    def tearDown(self):
        self.temp_dir.cleanup()

    def disk(self):
        return json.loads(self.config_path.read_text())


class TestRequestLaunch(LaunchTestBase):
    def test_request_launch_without_save_keeps_disk_file(self):
        self.app.config["database"]["host"] = "edited-host"
        self.app.is_dirty = True
        ok, _ = self.app.request_launch(ref=["v6.1"], single_version=True)
        self.assertTrue(ok)
        self.assertTrue(self.app.should_run_pipeline)
        self.assertFalse(self.app.is_running)
        self.assertEqual(self.app.launch_args, {"ref": ["v6.1"], "single_version": True})
        self.assertEqual(self.disk()["database"]["host"], "disk-host")

    def test_request_launch_with_save_writes_config(self):
        self.app.config["database"]["host"] = "edited-host"
        ok, _ = self.app.request_launch(save=True)
        self.assertTrue(ok)
        self.assertEqual(self.disk()["database"]["host"], "edited-host")

    def test_request_launch_failed_save_keeps_tui_running(self):
        with patch.object(self.app, "save_config", return_value=(False, "disk full")):
            ok, msg = self.app.request_launch(save=True, reset=True)
        self.assertFalse(ok)
        self.assertEqual(msg, "disk full")
        self.assertTrue(self.app.is_running)
        self.assertFalse(self.app.should_run_pipeline)
        self.assertEqual(self.app.launch_args, {})

    def test_disk_config_ignores_memory_edits(self):
        self.app.config["database"]["host"] = "edited-host"
        self.assertEqual(self.app.disk_config()["database"]["host"], "disk-host")

    def test_run_config_tui_launch_returns_overrides_or_none(self):
        def fake_run(app_self, stdscr):
            app_self.launch_args = {"reset": True, "reset_only": True}
            app_self.should_run_pipeline = True
            return True

        with patch("core.tui.app.curses.wrapper", side_effect=lambda fn: fn(None)), \
                patch.object(TUIApp, "run", fake_run):
            self.assertEqual(run_config_tui_launch(self.config_path), {"reset": True, "reset_only": True})
            self.assertTrue(run_config_tui(self.config_path))

        with patch("core.tui.app.curses.wrapper", return_value=False):
            self.assertIsNone(run_config_tui_launch(self.config_path))
            self.assertFalse(run_config_tui(self.config_path))

    def test_run_config_tui_launch_restores_signal_handlers(self):
        """Handlers curses installs (SIGTSTP/SIGWINCH) are put back once the TUI exits."""
        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTSTP, signal.SIGWINCH)}

        def fake_wrapper(fn, fail=False):
            def curses_handler(signum, frame):
                pass

            signal.signal(signal.SIGTSTP, curses_handler)
            signal.signal(signal.SIGWINCH, curses_handler)
            if fail:
                raise RuntimeError("boom")
            return False

        try:
            for fail in (False, True):
                with self.subTest(fail=fail), \
                        patch("core.tui.app.curses.wrapper", side_effect=lambda fn: fake_wrapper(fn, fail)), \
                        patch("builtins.print"):
                    self.assertIsNone(run_config_tui_launch(self.config_path))
                    for sig, handler in saved.items():
                        self.assertIs(signal.getsignal(sig), handler)
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)


class TestChooseLaunchConfig(LaunchTestBase):
    def test_clean_config_runs_saved_without_prompt(self):
        with patch("core.tui.screens.launch.ModalDialog.show_dialog") as dlg:
            self.assertEqual(launch_mod.choose_launch_config(self.stdscr, self.app, "Parse"), launch_mod.RUN_SAVED)
        dlg.assert_not_called()

    def test_dirty_config_prompts(self):
        self.app.is_dirty = True
        for answer, expected in (
            ("Save & Parse", launch_mod.SAVE_AND_RUN),
            ("Use saved config.json", launch_mod.RUN_SAVED),
            ("Cancel", None),
            (None, None),
        ):
            with patch("core.tui.screens.launch.ModalDialog.show_dialog", return_value=answer):
                self.assertEqual(launch_mod.choose_launch_config(self.stdscr, self.app, "Parse"), expected)


class TestMainMenuRunSaved(LaunchTestBase):
    def setUp(self):
        super().setUp()
        self.screen = MainMenuScreen(self.stdscr, self.app)

    def test_run_saved_clean_launches_without_saving(self):
        with patch.object(self.app, "save_config") as save:
            self.screen.handle_key(ord("p"))
        save.assert_not_called()
        self.assertTrue(self.app.should_run_pipeline)
        self.assertEqual(self.app.launch_args, {})

    def test_run_saved_dirty_requires_confirmation(self):
        self.app.config["database"]["host"] = "edited-host"
        self.app.is_dirty = True
        with patch("core.tui.screens.main_menu.ModalDialog.show_dialog", return_value="Cancel"):
            self.screen.handle_key(ord("P"))
        self.assertFalse(self.app.should_run_pipeline)

        with patch("core.tui.screens.main_menu.ModalDialog.show_dialog", return_value="Run (discard edits)"):
            self.screen.handle_key(ord("P"))
        self.assertTrue(self.app.should_run_pipeline)
        self.assertEqual(self.disk()["database"]["host"], "disk-host")

    def test_new_hotkeys_dispatch(self):
        for key, target in (("6", "GitRepoSelectScreen"), ("7", "VersionParseScreen")):
            with patch(f"core.tui.screens.main_menu.{target}") as cls, \
                    patch.object(self.app, "push_screen") as push:
                self.screen.handle_key(ord(key))
            cls.assert_called_once_with(self.stdscr, self.app)
            push.assert_called_once()
        with patch("core.tui.screens.main_menu.run_table_reset") as reset:
            self.screen.handle_key(ord("8"))
        reset.assert_called_once_with(self.stdscr, self.app)

    def test_draw_small_terminal_scrolls_to_selection(self):
        self.stdscr.getmaxyx.return_value = (14, 80)
        self.screen.selected_index = len(MainMenuScreen.MENU_ITEMS) - 1
        self.screen.draw()
        drawn = " ".join(str(c.args[2]) for c in self.stdscr.addstr.call_args_list if len(c.args) >= 3)
        self.assertIn("Exit", drawn)


class TestTableReset(LaunchTestBase):
    def _run(self, button, typed):
        with patch("core.tui.screens.table_reset.ModalDialog.show_dialog", return_value=button) as dlg:
            result = table_reset.run_table_reset(self.stdscr, self.app, prompt=_prompt_with(typed))
        return result, dlg

    def test_reset_only_requires_exact_word(self):
        result, dlg = self._run(table_reset.RESET_ONLY, "RESET")
        self.assertTrue(result)
        self.assertEqual(self.app.launch_args, {"reset": True, "reset_only": True})
        self.assertIn("disk_db", dlg.call_args_list[0].kwargs["message"])

    def test_reset_and_parse(self):
        result, _ = self._run(table_reset.RESET_AND_PARSE, "RESET")
        self.assertTrue(result)
        self.assertEqual(self.app.launch_args, {"reset": True, "reset_only": False})

    def test_wrong_word_aborts(self):
        for typed in ("reset", "RESE", "", None, "yes"):
            self.app.should_run_pipeline = False
            result, dlg = self._run(table_reset.RESET_ONLY, typed)
            self.assertFalse(result)
            self.assertFalse(self.app.should_run_pipeline)
            self.assertEqual(dlg.call_args_list[-1].args[1], "Reset Aborted")

    def test_cancel_at_warning(self):
        result, _ = self._run("Cancel", "RESET")
        self.assertFalse(result)
        self.assertFalse(self.app.should_run_pipeline)

    def test_target_follows_chosen_config(self):
        self.app.config["database"]["database"] = "edited_db"
        self.app.is_dirty = True
        with patch("core.tui.screens.table_reset.choose_launch_config", return_value=launch_mod.RUN_SAVED):
            _, dlg = self._run("Cancel", "")
        self.assertIn("disk_db", dlg.call_args_list[0].kwargs["message"])
        with patch("core.tui.screens.table_reset.choose_launch_config", return_value=launch_mod.SAVE_AND_RUN):
            _, dlg = self._run("Cancel", "")
        self.assertIn("edited_db", dlg.call_args_list[0].kwargs["message"])

    def test_describe_target_with_tunnel(self):
        cfg = {"database": {"engine": "mariadb", "database": "kdb", "host": "127.0.0.1", "port": 3308,
                            "ssh_tunnel": {"enabled": True, "user": "u", "host": "srv"}}}
        self.assertEqual(table_reset.describe_reset_target(cfg), "mariadb 'kdb' at 127.0.0.1:3308 via SSH u@srv")


class TestConfigureReexecPreservesLaunch(LaunchTestBase):
    def test_configure_reset_and_parse_survives_memory_guard_reexec(self):
        import os
        import pipeline.cli

        captured_env = {}

        def fake_setup_memory_limit(limit_gb):
            captured_env["KERNELINFO_TUI_LAUNCH"] = os.environ.get("KERNELINFO_TUI_LAUNCH")

        with patch("sys.argv", ["main.py", "--configure", "-c", str(self.config_path)]), \
                patch("core.tui.app.run_config_tui_launch", return_value={"reset": True, "reset_only": False}) as run_tui, \
                patch("pipeline.cli.sync_repositories", return_value=[]), \
                patch("pipeline.cli.setup_memory_limit", side_effect=fake_setup_memory_limit):
            args = pipeline.cli.arg_handling()

        run_tui.assert_called_once_with(str(self.config_path))
        self.assertEqual(captured_env["KERNELINFO_TUI_LAUNCH"], json.dumps({"reset": True, "reset_only": False}))
        self.assertNotIn("KERNELINFO_TUI_LAUNCH", os.environ)
        self.assertTrue(args.reset)
        self.assertFalse(args.reset_only)

        # Simulate the re-execed child process inheriting KERNELINFO_TUI_LAUNCH with --configure still in sys.argv
        with patch.dict(os.environ, {"KERNELINFO_TUI_LAUNCH": captured_env["KERNELINFO_TUI_LAUNCH"]}), \
                patch("sys.argv", ["main.py", "--configure", "-c", str(self.config_path)]), \
                patch("core.tui.app.run_config_tui_launch") as run_tui_child, \
                patch("pipeline.cli.sync_repositories", return_value=[]), \
                patch("pipeline.cli.setup_memory_limit"):
            child_args = pipeline.cli.arg_handling()

        run_tui_child.assert_not_called()
        self.assertTrue(child_args.reset)
        self.assertFalse(child_args.reset_only)



class _FakeServer:
    """subprocess.Popen stand-in: runs until signalled (or `exit_after` polls), then reports `code`."""

    _next_pid = 4000

    def __init__(self, cmd, exit_after=None, code=0):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.cmd = cmd
        self.returncode = None
        self.signals = []
        self._polls = 0
        self._exit_after = exit_after
        self._code = code

    def poll(self):
        self._polls += 1
        if self.returncode is None and self._exit_after is not None and self._polls >= self._exit_after:
            self.returncode = self._code
        return self.returncode

    def on_signal(self, signum):
        self.signals.append(signum)
        if self.returncode is None:
            self.returncode = 0


class TestServerLaunch(LaunchTestBase):
    """TUI "W" launches: `pipeline.cli.arg_handling()` hands off to `launch_servers()` (no parser setup)."""

    def _run(self, servers, *, exits=None, stop_after_polls=None):
        import pipeline.cli

        exits = exits or {}
        spawned = {}

        def fake_popen(cmd, cwd=None, start_new_session=False):
            self.assertTrue(start_new_session)
            name = "api" if cmd[1].endswith("api/main.py") else "webapp"
            exit_after, code = exits.get(name, (None, 0))
            spawned[name] = _FakeServer(cmd, exit_after, code)
            return spawned[name]

        def fake_killpg(pid, signum):
            for proc in spawned.values():
                if proc.pid == pid:
                    proc.on_signal(signum)

        sleeps = {"n": 0}

        def fake_sleep(_s):
            sleeps["n"] += 1
            if stop_after_polls is not None and sleeps["n"] == stop_after_polls:
                # What the SIGINT handler does on Ctrl+C
                handler = signal.getsignal(signal.SIGINT)
                handler(signal.SIGINT, None)

        with patch("pipeline.cli.time.sleep", side_effect=fake_sleep), patch("builtins.print"):
            code = pipeline.cli.launch_servers(
                servers, str(self.config_path), popen=fake_popen, killpg=fake_killpg, poll_interval=0
            )
        return code, spawned

    def test_both_servers_stop_together_on_sigint(self):
        prev = signal.getsignal(signal.SIGINT)
        code, spawned = self._run(["webapp", "api"], stop_after_polls=3)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(spawned), ["api", "webapp"])
        for name, entry in (("webapp", "webapp/main.py"), ("api", "api/main.py")):
            cmd = spawned[name].cmd
            self.assertTrue(cmd[1].endswith(entry))
            self.assertEqual(cmd[2:], ["-c", str(self.config_path)])
            self.assertEqual(spawned[name].signals, [signal.SIGINT])
        self.assertIs(signal.getsignal(signal.SIGINT), prev)

    def test_one_server_failing_stops_the_other(self):
        code, spawned = self._run(["webapp", "api"], exits={"api": (2, 3)})
        self.assertEqual(code, 3)
        self.assertEqual(spawned["webapp"].signals, [signal.SIGINT])
        self.assertEqual(spawned["api"].signals, [])

    def test_single_server_and_invalid_selection(self):
        code, spawned = self._run(["api"], exits={"api": (2, 0)})
        self.assertEqual((code, list(spawned)), (0, ["api"]))
        import pipeline.cli
        with patch("builtins.print"):
            self.assertEqual(pipeline.cli.launch_servers(["bogus"], None), 2)

    def test_arg_handling_dispatches_before_parser_setup(self):
        import pipeline.cli

        for servers in (["webapp"], ["api"], ["webapp", "api"]):
            launch = {"servers": servers, "config_path": str(self.config_path)}
            with patch("sys.argv", ["main.py", "--configure", "-c", str(self.config_path)]), \
                    patch("core.tui.app.run_config_tui_launch", return_value=launch), \
                    patch("pipeline.cli.launch_servers", return_value=0) as run_servers, \
                    patch("pipeline.cli.setup_memory_limit") as mem_guard, \
                    patch("pipeline.cli.setup_parser_logging") as parser_logs, \
                    patch("pipeline.cli.sync_repositories") as sync_repos:
                with self.assertRaises(SystemExit) as cm:
                    pipeline.cli.arg_handling()
            self.assertEqual(cm.exception.code, 0)
            run_servers.assert_called_once_with(servers, str(self.config_path))
            mem_guard.assert_not_called()
            parser_logs.assert_not_called()
            sync_repos.assert_not_called()


# Child program for the PTY test: the terminal starts with `ONLCR` off (curses program mode left
# behind by a TUI killed while it owned the terminal), a real curses session requests a "W" launch
# of both servers, then `pipeline.cli.launch_servers()` runs with a Popen stand-in that reports the
# tty output modes at spawn time.
_SERVER_LAUNCH_CHILD = textwrap.dedent(
    """
    import os, sys, termios
    sys.path.insert(0, ROOT)
    attrs = termios.tcgetattr(0)
    attrs[1] &= ~termios.ONLCR
    termios.tcsetattr(0, termios.TCSANOW, attrs)

    from core.tui.app import TUIApp, run_config_tui_launch
    from core.tui.screens.launch import RUN_SAVED, launch_servers as request_servers
    import pipeline.cli

    def run_loop(self, stdscr):
        request_servers(stdscr, self, RUN_SAVED, ("webapp", "api"))
        return self.should_run_pipeline

    TUIApp._run_loop = run_loop
    launch = run_config_tui_launch(CONFIG)

    class Server:
        pid = 4242
        returncode = 0
        def poll(self):
            return 0

    def popen(cmd, cwd=None, start_new_session=False):
        oflag = termios.tcgetattr(0)[1]
        print("SPAWN", bool(oflag & termios.OPOST), bool(oflag & termios.ONLCR), flush=True)
        return Server()

    code = pipeline.cli.launch_servers(
        launch["servers"], launch["config_path"], popen=popen, killpg=lambda *a: None, poll_interval=0
    )
    print("DONE", code, flush=True)
    """
)


def _read_pty_until(fd: int, marker: bytes, timeout: float = 20.0) -> bytes:
    buf = b""
    end = time.time() + timeout
    while marker not in buf and time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
    return buf


@unittest.skipUnless(os.name == "posix", "pty-based test requires POSIX")
class TestServerLaunchTerminalModes(LaunchTestBase):
    """A TUI server launch prints CR-LF newlines even when the TUI started on a tty with `ONLCR` off."""

    def test_launch_servers_output_after_tui_uses_crlf(self):
        import pty

        root = str(Path(__file__).resolve().parent.parent)
        program = _SERVER_LAUNCH_CHILD.replace("ROOT", repr(root)).replace("CONFIG", repr(str(self.config_path)))
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child process
            try:
                os.environ["TERM"] = "xterm"
                os.environ["CONFIG_FILE"] = str(self.config_path)
                os.environ["KERNELINFO_CONFIG"] = str(self.config_path)
                os.execv(sys.executable, [sys.executable, "-c", program])
            finally:
                os._exit(127)
        try:
            out = _read_pty_until(fd, b"DONE")
            out += _read_pty_until(fd, b"\n", timeout=2)
        finally:
            os.waitpid(pid, 0)
            os.close(fd)
        # Output modes are back before the first server is spawned ...
        self.assertEqual(out.count(b"SPAWN True True"), 2, out)
        self.assertIn(b"DONE 0", out)
        # ... and every line launch_servers() prints ends with CR-LF (no bare LF staircase)
        tail = out[out.index(b"Started webapp server"):]
        self.assertIn(b"Started api server", tail)
        self.assertNotRegex(tail, rb"[^\r]\n")


if __name__ == "__main__":
    unittest.main()
