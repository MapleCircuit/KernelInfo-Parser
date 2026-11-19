"""tests/test_systemd_tui.py - Unit tests for SystemdServicesScreen, ScopedConfigMenuScreen, WebappSettingsScreen, and TUIApp context/suspend."""

from __future__ import annotations

import curses
import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from core.config import (
    DAEMON_CONFIG_FILENAME,
    WEBAPP_CONFIG_FILENAME,
    get_service_config_path,
)
from core.systemd_manager import (
    DAEMON_UNIT_NAME,
    WEBAPP_UNIT_NAME,
    CommandResult,
    ServiceStatus,
)
from core.tui.app import TUIApp
from core.tui.output_capture import TerminalOutputCapture
from core.tui.screens.database_ssh import DatabaseSSHScreen
from core.tui.screens.logging_cfg import LoggingScreen
from core.tui.screens.main_menu import MainMenuScreen
from core.tui.screens.parser_dist import ParserDistributedScreen
from core.tui.screens.scoped_config import ScopedConfigMenuScreen
from core.tui.screens.systemd_services import ScrollableTextViewer, SystemdServicesScreen
from core.tui.screens.webapp_cfg import WebappSettingsScreen
from core.tui.widgets import ModalDialog
from tests.test_tui import MockCursesWindow


def _window_text(win: MockCursesWindow) -> str:
    return "\n".join(str(args[2]) for name, args, _ in win.calls if name == "addstr" and len(args) >= 3)


class TestMainMenuSystemdEntry(unittest.TestCase):
    """Verify MainMenuScreen exposes Systemd Services at '0' and keeps 'Q' last."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp.name) / "config.json"
        self.app = TUIApp(self.config_path)
        self.win = MockCursesWindow(30, 100)
        self.menu = MainMenuScreen(self.win, self.app)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_menu_items_include_systemd_and_q_last(self) -> None:
        keys = [item[0] for item in MainMenuScreen.MENU_ITEMS]
        self.assertIn("0", keys)
        self.assertEqual(keys[-1], "Q")
        self.assertEqual(MainMenuScreen.HOTKEYS["0"], "0")

    def test_hotkey_0_pushes_systemd_services_screen(self) -> None:
        with patch.object(self.app, "push_screen") as mock_push, \
                patch("core.tui.screens.systemd_services.get_service_status") as mock_status:
            mock_status.side_effect = lambda svc, **kw: ServiceStatus(
                service=svc,
                unit_name=DAEMON_UNIT_NAME if svc == "daemon" else WEBAPP_UNIT_NAME,
                unit_path=Path(f"/tmp/{svc}.service"),
                installed=False,
                enabled="disabled",
                active="inactive",
                sub_state="dead",
                mode="direct",
                user="",
                group="",
                attach_command=None,
                main_pid=None,
                error=None,
            )
            handled = self.menu.handle_key(ord("0"))
        self.assertTrue(handled)
        self.assertTrue(mock_push.called)
        self.assertIsInstance(mock_push.call_args[0][0], SystemdServicesScreen)


class TestTUIAppContextAndSuspend(unittest.TestCase):
    """Test TUIApp config context stack, webapp pruning, output_capture pause/resume, and suspend()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base_cfg = self.root / "config.json"
        self.base_cfg.write_text(
            json.dumps({
                "database": {
                    "host": "main-db",
                    "port": 3306,
                    "server_tuning": {"innodb_buffer_pool_size": "4G"},
                },
                "parser": {"mem_max": 32},
                "logging": {
                    "parser": {"enabled": True, "file": "p.log"},
                    "webapp": {"enabled": False, "file": "w.log"},
                },
                "repositories": [
                    {"name": "linux", "path": "repos/linux", "preparse": {"insights": {"mode": "all"}}}
                ],
            }),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_push_and_restore_config_context(self) -> None:
        app = TUIApp(self.base_cfg)
        app.config["database"]["host"] = "unsaved-main-edit"
        app.is_dirty = True
        app.view_state = {"main": {"row": 3}}

        daemon_cfg = self.root / DAEMON_CONFIG_FILENAME
        app.push_config_context(daemon_cfg, scope="daemon", seed_service="daemon")

        self.assertTrue(daemon_cfg.is_file())
        self.assertEqual(app.config_path, daemon_cfg.resolve())
        self.assertEqual(app.config_scope, "daemon")
        self.assertFalse(app.is_dirty)
        self.assertEqual(app.view_state, {})
        self.assertEqual(app.config["database"]["host"], "main-db")
        self.assertEqual(app.config["parser"]["distributed"]["role"], "daemon")

        # Mutate daemon config in memory and restore
        app.config["database"]["host"] = "daemon-only-edit"
        app.is_dirty = True
        self.assertTrue(app.restore_config_context())

        self.assertEqual(app.config_path, self.base_cfg.resolve())
        self.assertEqual(app.config_scope, "full")
        self.assertTrue(app.is_dirty)
        self.assertEqual(app.config["database"]["host"], "unsaved-main-edit")
        self.assertEqual(app.view_state, {"main": {"row": 3}})
        self.assertFalse(app.restore_config_context())

    def test_webapp_scope_prunes_parser_keys_on_save(self) -> None:
        app = TUIApp(self.base_cfg)
        webapp_cfg = self.root / WEBAPP_CONFIG_FILENAME
        app.push_config_context(webapp_cfg, scope="webapp", seed_service="webapp")

        app.config["webapp"]["port"] = 9090
        ok, _ = app.save_config()
        self.assertTrue(ok)

        saved = json.loads(webapp_cfg.read_text(encoding="utf-8"))
        self.assertEqual(saved["webapp"]["port"], 9090)
        self.assertNotIn("parser", saved)
        self.assertNotIn("server_tuning", saved["database"])
        self.assertNotIn("parser", saved["logging"])
        self.assertNotIn("preparse", saved["repositories"][0])

    def test_output_capture_pause_resume_preserves_buffers(self) -> None:
        logger = logging.getLogger("tests.systemd_tui.capture")
        logger.setLevel(logging.INFO)
        term = io.StringIO()
        term.name = "<stderr>"
        handler = logging.StreamHandler(term)
        logger.addHandler(handler)
        logger.propagate = False
        try:
            cap = TerminalOutputCapture(redirect_fd=False)
            with cap:
                logger.info("captured-before")
                print("stdout-before")
                self.assertTrue(cap.pause())
                self.assertIn(handler, logger.handlers)
                logger.info("live-during-suspend")
                cap.resume()
                self.assertNotIn(handler, logger.handlers)
                logger.info("captured-after")
            self.assertIn("live-during-suspend", term.getvalue())
            self.assertNotIn("captured-before", term.getvalue())
            msgs = [r.getMessage() for r in cap.records]
            self.assertEqual(msgs, ["captured-before", "captured-after"])
            self.assertIn("stdout-before", "".join(cap.text))
        finally:
            logger.removeHandler(handler)
            logger.propagate = True

    def test_app_suspend_manages_curses_and_output_capture(self) -> None:
        app = TUIApp(self.base_cfg)
        win = MagicMock()
        app.stdscr = win
        cap = TerminalOutputCapture(redirect_fd=False)
        app.output_capture = cap

        events: list[str] = []
        with cap, \
                patch("core.tui.app.curses.def_prog_mode", side_effect=lambda: events.append("def_prog_mode")), \
                patch("core.tui.app.curses.endwin", side_effect=lambda: events.append("endwin")), \
                patch("core.tui.app.curses.reset_prog_mode", side_effect=lambda: events.append("reset_prog_mode")):
            with app.suspend():
                events.append("inside")
                self.assertFalse(cap._active)
            self.assertTrue(cap._active)

        self.assertEqual(events, ["def_prog_mode", "endwin", "inside", "reset_prog_mode"])
        win.clearok.assert_called_with(True)
        win.refresh.assert_called()

    def test_app_suspend_headless_curses_error_is_ignored(self) -> None:
        app = TUIApp(self.base_cfg)
        win = MagicMock()
        app.stdscr = win
        ran = False
        with patch("core.tui.app.curses.def_prog_mode", side_effect=curses.error("no initscr")):
            with app.suspend():
                ran = True
        self.assertTrue(ran)
        win.clearok.assert_called_with(True)


class TestScopedConfigFlows(unittest.TestCase):
    """End-to-end scoped config flows for daemon and webapp configs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base_cfg = self.root / "config.json"
        self.base_cfg.write_text(
            json.dumps({
                "database": {"host": "prod-db", "port": 3306, "user": "kip", "database": "linux"},
                "parser": {"mem_max": 64, "distributed": {"enabled": False, "role": "standalone"}},
                "webapp": {"host": "0.0.0.0", "port": 8000, "reload": True},
                "logging": {
                    "parser": {"enabled": True, "file": "parser.log", "level": "INFO"},
                    "webapp": {"enabled": False, "file": "webapp.log", "request_mode": "all", "level": "INFO"},
                },
            }),
            encoding="utf-8",
        )
        self.win = MockCursesWindow(32, 110)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_daemon_scoped_edit_saves_only_daemon_config_and_restores_base(self) -> None:
        daemon_cfg = self.root / DAEMON_CONFIG_FILENAME
        app = TUIApp(self.base_cfg)
        app.stdscr = self.win
        menu = MainMenuScreen(self.win, app)
        app.push_screen(menu)

        scoped = ScopedConfigMenuScreen(self.win, app, "daemon", config_path=daemon_cfg)
        app.push_screen(scoped)

        self.assertEqual(app.config_path, daemon_cfg.resolve())
        self.assertEqual(app.config_scope, "daemon")
        self.assertTrue(daemon_cfg.is_file())

        # Draw banner shows exact editing path
        scoped.draw()
        self.assertIn(f"Editing: {daemon_cfg.resolve()}", _window_text(self.win))

        # Open DatabaseSSHScreen, edit host, save (pops back to ScopedConfigMenuScreen)
        scoped._activate_key("2")
        db_screen = app.current_screen()
        self.assertIsInstance(db_screen, DatabaseSSHScreen)
        db_screen.host.set_value("daemon-db.internal")
        db_screen.save()

        # Returning to ScopedConfigMenuScreen must NOT overwrite unsaved edits!
        self.assertIs(app.current_screen(), scoped)
        self.assertTrue(app.is_dirty)
        self.assertEqual(app.config["database"]["host"], "daemon-db.internal")

        # Open LoggingScreen under daemon scope -> hides webapp logging
        scoped._activate_key("L")
        log_screen = app.current_screen()
        self.assertIsInstance(log_screen, LoggingScreen)
        self.assertTrue(log_screen.show_parser)
        self.assertFalse(log_screen.show_webapp)
        log_screen.inp_parser_file.set_value("daemon_only.log")
        log_screen.save()
        self.assertIs(app.current_screen(), scoped)

        # Save in ScopedConfigMenuScreen writes strictly to config_daemon.json
        with patch("core.tui.screens.scoped_config.ModalDialog.show", return_value="OK"):
            scoped._activate_key("S")

        written_daemon = json.loads(daemon_cfg.read_text(encoding="utf-8"))
        self.assertEqual(written_daemon["database"]["host"], "daemon-db.internal")
        self.assertEqual(written_daemon["logging"]["parser"]["file"], "daemon_only.log")
        written_base = json.loads(self.base_cfg.read_text(encoding="utf-8"))
        self.assertEqual(written_base["database"]["host"], "prod-db")

        # Return to parent restores config.json context
        scoped._activate_key("B")
        self.assertIs(app.current_screen(), menu)
        self.assertEqual(app.config_path, self.base_cfg.resolve())
        self.assertEqual(app.config_scope, "full")
        self.assertEqual(app.config["database"]["host"], "prod-db")

    def test_webapp_scoped_hides_parser_screens_and_prunes_on_save(self) -> None:
        webapp_cfg = self.root / WEBAPP_CONFIG_FILENAME
        app = TUIApp(self.base_cfg)
        app.stdscr = self.win
        menu = MainMenuScreen(self.win, app)
        app.push_screen(menu)

        scoped = ScopedConfigMenuScreen(self.win, app, "webapp", config_path=webapp_cfg)
        app.push_screen(scoped)

        titles = [item[1] for item in scoped.menu_items]
        self.assertTrue(any("Webapp Server" in t for t in titles))
        self.assertFalse(any("Parser Engine" in t for t in titles))
        self.assertFalse(any("Table Reset" in t for t in titles))
        self.assertFalse(any("Parse & Pre-Parse" in t for t in titles))

        # Option '1' opens WebappSettingsScreen
        scoped._activate_key("1")
        wa_screen = app.current_screen()
        self.assertIsInstance(wa_screen, WebappSettingsScreen)
        wa_screen.inp_host.set_value("127.0.0.1")
        wa_screen.inp_port.set_value("8888")
        wa_screen.chk_reload.set_checked(False)
        wa_screen.inp_cache_max_mb.set_value("2048")
        wa_screen.inp_jobs_workers.set_value("4")
        wa_screen.chk_prewarm_indexes.set_checked(True)
        wa_screen.chk_checkpatch_enabled.set_checked(True)
        wa_screen.inp_checkpatch_ref.set_value("v6.12")
        wa_screen.inp_tools_sandbox.set_value("bwrap")
        wa_screen.inp_source_index_workers.set_value("3")
        wa_screen.inp_source_index_max_mb.set_value("512")
        wa_screen.inp_source_index_revisions.set_value("12")
        self.assertTrue(wa_screen.save())
        self.assertIs(app.current_screen(), scoped)

        # LoggingScreen under webapp scope hides parser logging
        scoped._activate_key("L")
        log_screen = app.current_screen()
        self.assertIsInstance(log_screen, LoggingScreen)
        self.assertFalse(log_screen.show_parser)
        self.assertTrue(log_screen.show_webapp)
        self.assertNotIn(log_screen.chk_parser_enabled, log_screen.widgets)
        log_screen.draw()
        drawn = _window_text(self.win)
        self.assertIn("Webapp Backend Logging", drawn)
        self.assertNotIn("Parser Logging", drawn)
        log_screen.chk_webapp_enabled.set_checked(True)
        log_screen.inp_webapp_file.set_value("wa_service.log")
        self.assertTrue(log_screen.save())

        with patch("core.tui.screens.scoped_config.ModalDialog.show", return_value="OK"):
            scoped._activate_key("S")

        saved = json.loads(webapp_cfg.read_text(encoding="utf-8"))
        self.assertEqual(saved["webapp"]["host"], "127.0.0.1")
        self.assertEqual(saved["webapp"]["port"], 8888)
        self.assertFalse(saved["webapp"]["reload"])
        self.assertEqual(saved["webapp"]["cache"]["max_mb"], 2048)
        self.assertEqual(saved["webapp"]["jobs"]["workers"], 4)
        self.assertTrue(saved["webapp"]["prewarm_indexes"])
        self.assertTrue(saved["webapp"]["tools"]["checkpatch"]["enabled"])
        self.assertEqual(saved["webapp"]["tools"]["checkpatch"]["ref"], "v6.12")
        self.assertEqual(saved["webapp"]["tools"]["sandbox"], "bwrap")
        self.assertEqual(saved["webapp"]["source_index"], {"workers": 3, "max_mb": 512, "revisions": 12})
        self.assertEqual(saved["logging"]["webapp"]["file"], "wa_service.log")
        self.assertNotIn("parser", saved)
        self.assertNotIn("parser", saved["logging"])

        scoped._activate_key("B")
        self.assertEqual(app.config_path, self.base_cfg.resolve())
        self.assertEqual(app.config_scope, "full")

    def test_bootstrap_only_never_overwrites_existing_standalone_config(self) -> None:
        daemon_cfg = self.root / DAEMON_CONFIG_FILENAME
        daemon_cfg.write_text(
            json.dumps({
                "database": {"host": "keep-existing-daemon-host", "port": 3307},
                "parser": {"distributed": {"enabled": True, "role": "daemon", "worker_id": "node-42"}},
            }),
            encoding="utf-8",
        )
        app = TUIApp(self.base_cfg)
        app.stdscr = self.win
        scoped = ScopedConfigMenuScreen(self.win, app, "daemon", config_path=daemon_cfg)
        app.push_screen(scoped)

        self.assertEqual(app.config["database"]["host"], "keep-existing-daemon-host")
        self.assertEqual(app.config["parser"]["distributed"]["worker_id"], "node-42")

    def test_webapp_settings_validation_rejects_bad_port_and_workers(self) -> None:
        app = TUIApp(self.base_cfg)
        wa_screen = WebappSettingsScreen(self.win, app)
        wa_screen.inp_port.set_value("70000")
        wa_screen.inp_jobs_workers.set_value("0")
        with patch("core.tui.screens.webapp_cfg.ModalDialog.show", return_value="OK") as mock_dlg:
            ok = wa_screen.save()
        self.assertFalse(ok)
        self.assertTrue(mock_dlg.called)
        self.assertEqual(app.config["webapp"]["port"], 8000)


class TestSystemdServicesScreen(unittest.TestCase):
    """Tests for SystemdServicesScreen UI, tmux checks, preflight, suspend(), and journalctl."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.unit_dir = self.root / "units"
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.base_cfg = self.root / "config.json"
        self.base_cfg.write_text(
            json.dumps({
                "database": {"engine": "sqlite", "path": str(self.root / "test.db")},
                "parser": {"mem_max": 16},
                "webapp": {"host": "127.0.0.1", "port": 8000, "reload": False},
            }),
            encoding="utf-8",
        )
        self.app = TUIApp(self.base_cfg)
        self.win = MockCursesWindow(24, 90)
        self.app.stdscr = self.win

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_screen(
        self,
        *,
        statuses: dict[str, ServiceStatus] | None = None,
        runner: Any = None,
        priv_runner: Any = None,
    ) -> SystemdServicesScreen:
        default_statuses = {
            "daemon": ServiceStatus(
                service="daemon",
                unit_name=DAEMON_UNIT_NAME,
                unit_path=self.unit_dir / DAEMON_UNIT_NAME,
                installed=False,
                enabled="disabled",
                active="inactive",
                sub_state="dead",
                mode="direct",
                user="",
                group="",
                attach_command=None,
                main_pid=None,
                error=None,
            ),
            "webapp": ServiceStatus(
                service="webapp",
                unit_name=WEBAPP_UNIT_NAME,
                unit_path=self.unit_dir / WEBAPP_UNIT_NAME,
                installed=True,
                enabled="enabled",
                active="active",
                sub_state="running",
                mode="tmux",
                user="otheruser",
                group="othergrp",
                attach_command="sudo -u otheruser tmux -L kernelinfo-webapp attach -t kernelinfo-webapp",
                main_pid=1234,
                error=None,
            ),
        }
        status_map = statuses or default_statuses
        with patch("core.tui.screens.systemd_services.get_service_status", side_effect=lambda svc, **kw: status_map[svc]):
            return SystemdServicesScreen(
                self.win,
                self.app,
                unit_dir=self.unit_dir,
                runner=runner,
                privileged_runner=priv_runner,
                project_root=self.root,
            )

    def test_switch_services_and_render_attach_commands(self) -> None:
        screen = self._make_screen()
        self.assertEqual(screen.selected_service, "daemon")

        screen.inp_user.set_value("alice")
        screen.inp_group.set_value("devs")
        screen.select_service("webapp")
        self.assertEqual(screen.selected_service, "webapp")
        self.assertEqual(screen.inp_user.get_value(), "otheruser")
        self.assertTrue(screen.chk_tmux.is_checked())

        screen.draw()
        text = _window_text(self.win)
        self.assertIn("sudo -u otheruser tmux -L kernelinfo-webapp attach -t kernelinfo-webapp", text)
        self.assertIn("Installed", text)

        # Switch back to daemon preserves draft edits
        screen.select_service("daemon")
        self.assertEqual(screen.inp_user.get_value(), "alice")
        self.assertEqual(screen.inp_group.get_value(), "devs")

    def test_same_user_attach_command_displayed_when_tmux_checked(self) -> None:
        screen = self._make_screen()
        with patch("core.systemd_manager.default_user_group", return_value=("scott", "scott")), \
                patch("core.tui.screens.systemd_services.find_tmux_binary", return_value="/usr/bin/tmux"):
            screen.inp_user.set_value("scott")
            screen.chk_tmux.set_checked(True)
            screen.draw()
        text = _window_text(self.win)
        self.assertIn("Attach: tmux -L kernelinfo-daemon attach -t kernelinfo-daemon", text)

    def test_toggling_tmux_without_binary_shows_error_and_reverts(self) -> None:
        screen = self._make_screen()
        screen.focus_widget(screen.widgets.index(screen.chk_tmux))
        with patch("core.tui.screens.systemd_services.find_tmux_binary", return_value=None), \
                patch("core.tui.screens.systemd_services.ModalDialog.show", autospec=True, return_value="OK") as mock_dlg:
            handled = screen.handle_key(ord(" "))
        self.assertTrue(handled)
        self.assertFalse(screen.chk_tmux.is_checked())
        self.assertTrue(mock_dlg.called)
        self.assertIn("tmux Not Found", mock_dlg.call_args[0][0].title)

    def test_invalid_user_group_blocks_install_at_preflight(self) -> None:
        screen = self._make_screen()
        screen.inp_user.set_value("no_such_user_xyz_999")
        with patch("core.tui.screens.systemd_services.install_service") as mock_install, \
                patch("core.tui.screens.systemd_services.ModalDialog.show", return_value="OK") as mock_dlg:
            ok = screen.install_selected_service()
        self.assertFalse(ok)
        mock_install.assert_not_called()
        self.assertTrue(mock_dlg.called)

    def test_preview_unit_displays_systemd_unit_text(self) -> None:
        screen = self._make_screen()
        with patch.object(ScrollableTextViewer, "show") as mock_show:
            unit_text = screen.preview_unit()
        self.assertTrue(mock_show.called)
        self.assertIn("[Service]", unit_text)
        self.assertIn(f"KERNELINFO_CONFIG={self.root / DAEMON_CONFIG_FILENAME}", unit_text)

    def test_install_control_uninstall_use_app_suspend(self) -> None:
        screen = self._make_screen()
        suspend_entries = 0
        orig_suspend = self.app.suspend

        def counting_suspend():
            nonlocal suspend_entries
            suspend_entries += 1
            return orig_suspend()

        with patch.object(self.app, "suspend", side_effect=counting_suspend), \
                patch("core.tui.screens.systemd_services.preflight_service_install", return_value=(True, [], [])), \
                patch("core.tui.screens.systemd_services.install_service", return_value=(True, "Installed")) as mock_inst, \
                patch("core.tui.screens.systemd_services.control_service", return_value=CommandResult(True, 0, "", "", [])) as mock_ctrl, \
                patch("core.tui.screens.systemd_services.uninstall_service", return_value=(True, "Removed")) as mock_uninst, \
                patch("core.tui.screens.systemd_services.get_service_status", side_effect=lambda svc, **kw: screen.statuses[svc]), \
                patch("core.tui.screens.systemd_services.ModalDialog.show", side_effect=["OK", "Uninstall", "OK"]):
            self.assertTrue(screen.install_selected_service())
            self.assertTrue(screen.control_selected_service("start"))
            self.assertTrue(screen.control_selected_service("restart"))
            self.assertTrue(screen.uninstall_selected_service())

        self.assertTrue(mock_inst.called)
        self.assertEqual(mock_ctrl.call_count, 2)
        self.assertTrue(mock_uninst.called)
        self.assertEqual(suspend_entries, 4)

    def test_journalctl_viewer_displays_logs(self) -> None:
        def fake_runner(cmd: list[str]) -> CommandResult:
            return CommandResult(True, 0, "Nov 19 systemd: started daemon\nline2", "", cmd)

        screen = self._make_screen(runner=fake_runner)
        with patch.object(ScrollableTextViewer, "show") as mock_show:
            logs = screen.view_logs(lines=50)
        self.assertTrue(mock_show.called)
        self.assertIn("started daemon", logs)

    def test_small_terminal_draws_without_error(self) -> None:
        screen = self._make_screen()
        tiny = MockCursesWindow(16, 45)
        screen.stdscr = tiny
        screen.draw()
        self.assertIn("Terminal too small", _window_text(tiny))

        min_win = MockCursesWindow(24, 80)
        screen.stdscr = min_win
        screen.draw()
        self.assertIn("Systemd Services", _window_text(min_win))

    def test_preflight_warnings_cancel_and_proceed(self) -> None:
        screen = self._make_screen()
        with patch.object(self.app, "suspend"), \
                patch("core.tui.screens.systemd_services.preflight_service_install", return_value=(True, [], ["unreadable repo"])), \
                patch("core.tui.screens.systemd_services.install_service", return_value=(True, "Installed")) as mock_inst, \
                patch("core.tui.screens.systemd_services.ModalDialog.show", return_value="Cancel"):
            self.assertFalse(screen.install_selected_service())
        mock_inst.assert_not_called()

        with patch.object(self.app, "suspend"), \
                patch("core.tui.screens.systemd_services.preflight_service_install", return_value=(True, [], ["unreadable repo"])), \
                patch("core.tui.screens.systemd_services.install_service", return_value=(True, "Installed")) as mock_inst, \
                patch("core.tui.screens.systemd_services.get_service_status", side_effect=lambda svc, **kw: screen.statuses[svc]), \
                patch("core.tui.screens.systemd_services.ModalDialog.show", side_effect=["Install Anyway", "OK"]):
            self.assertTrue(screen.install_selected_service())
        self.assertTrue(mock_inst.called)

    def test_scrollable_text_viewer_scrolling_and_close(self) -> None:
        win = MockCursesWindow(14, 70)
        lines = "\n".join(f"line-{i}" for i in range(40))
        viewer = ScrollableTextViewer(win, "Test Logs", lines)
        self.assertEqual(viewer.scroll_offset, 0)
        self.assertFalse(viewer.handle_key(curses.KEY_DOWN))
        self.assertEqual(viewer.scroll_offset, 1)
        self.assertFalse(viewer.handle_key(curses.KEY_NPAGE))
        self.assertGreater(viewer.scroll_offset, 1)
        self.assertFalse(viewer.handle_key(curses.KEY_HOME))
        self.assertEqual(viewer.scroll_offset, 0)
        self.assertFalse(viewer.handle_key(curses.KEY_END))
        self.assertGreater(viewer.scroll_offset, 0)
        viewer.draw()
        self.assertIn("Test Logs", _window_text(win))
        self.assertTrue(viewer.handle_key(27))


if __name__ == "__main__":
    unittest.main()
