"""tests/test_parser_cve_screens.py - Unit tests for ParserDistributedScreen, CVEMailingListScreen, and DiagnosticsScreen."""

import curses
import unittest
from unittest.mock import MagicMock, patch

from core.tui.screens.cve_ml import CVEMailingListScreen
from core.tui.screens.diagnostics import DiagnosticsScreen
from core.tui.screens.parser_dist import ParserDistributedScreen
from core.tui.testing import DiagnosticResult
from tests.test_tui import MockCursesWindow


class MockApp:
    """Mock application controller exposing config, dirty tracking, and screen stack operations."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config if config is not None else {}
        self.is_dirty = False
        self.popped = False

    def pop_screen(self) -> None:
        self.popped = True


class TestParserDistributedScreen(unittest.TestCase):
    """Test suite for ParserDistributedScreen configuration, hotkeys, and persistence."""

    def setUp(self) -> None:
        self.win = MockCursesWindow(24, 80)
        self.initial_config = {
            "cache": {
                "engine": "ram",
                "dir": "",
            },
            "parser": {
                "table_engine": "cached",
                "memory_mode": "normal",
                "mem_max": 60,
                "hugepages": "auto",
                "cache_max_table_mb": 0,
                "distributed": {
                    "enabled": False,
                    "role": "standalone",
                    "worker_id": "worker-node-1",
                    "delegate_stable": False,
                },
            },
        }
        self.app = MockApp(self.initial_config)
        self.screen = ParserDistributedScreen(self.win, self.app)

    def test_initial_values_loaded(self) -> None:
        self.assertEqual(self.screen.sel_table_engine.get_selected(), "cached")
        self.assertEqual(self.screen.sel_memory_mode.get_selected(), "normal")
        self.assertEqual(self.screen.inp_mem_max.get_value(), "60")
        self.assertEqual(self.screen.sel_hugepages.get_selected(), "auto")
        self.assertEqual(self.screen.inp_cache_max_table_mb.get_value(), "0")
        self.assertEqual(self.screen.sel_cache_engine.get_selected(), "ram")
        self.assertEqual(self.screen.inp_cache_dir.get_value(), "")
        self.assertFalse(self.screen.chk_dist_enabled.is_checked())
        self.assertEqual(self.screen.sel_role.get_selected(), "standalone")
        self.assertEqual(self.screen.inp_worker_id.get_value(), "worker-node-1")
        self.assertFalse(self.screen.chk_delegate_stable.is_checked())

    def test_cache_engine_options_alias_and_unknown(self) -> None:
        self.assertEqual(self.screen.sel_cache_engine.options, ["ram", "sqlite"])
        self.app.config["cache"] = {"engine": "sqlite3", "dir": "/tmp/kcache"}
        screen = ParserDistributedScreen(self.win, self.app)
        self.assertEqual(screen.sel_cache_engine.get_selected(), "sqlite")
        self.assertEqual(screen.inp_cache_dir.get_value(), "/tmp/kcache")

        self.app.config["cache"]["engine"] = "nonexistent-engine"
        screen = ParserDistributedScreen(self.win, self.app)
        self.assertEqual(screen.sel_cache_engine.get_selected(), "ram")

    def test_memory_mode_options_and_legacy_alias(self) -> None:
        self.assertEqual(self.screen.sel_memory_mode.options, ["normal", "low", "very_low"])
        self.app.config["parser"]["memory_mode"] = "compact"
        screen = ParserDistributedScreen(self.win, self.app)
        self.assertEqual(screen.sel_memory_mode.get_selected(), "very_low")

    def _screen_text(self, cols: int) -> str:
        win = MockCursesWindow(24, cols)
        screen = ParserDistributedScreen(win, self.app)
        screen.focus_widget(1)  # Memory Mode
        screen.draw()
        return "\n".join(c[1][2] for c in win.calls if c[0] == "addstr")

    def test_help_panel_follows_focus_when_wide(self) -> None:
        self.assertIn("very_low", self._screen_text(120))
        self.assertNotIn("Trades parse speed", self._screen_text(80))
        self.screen.focus_widget(4)
        self.assertEqual(self.screen.current_help_topic(), "max_te_table")
        self.screen.focus_widget(5)
        self.assertEqual(self.screen.current_help_topic(), "cache_engine")
        self.screen.focus_widget(6)
        self.assertEqual(self.screen.current_help_topic(), "cache_dir")
        self.screen.focus_widget(8)
        self.assertEqual(self.screen.current_help_topic(), "distributed")
        self.screen.focus_widget(2)
        self.assertEqual(self.screen.current_help_topic(), "overview")

    def test_f1_opens_help_dialog(self) -> None:
        with patch("core.tui.screens.parser_dist.ModalDialog") as dlg:
            self.assertTrue(self.screen.handle_key(curses.KEY_F1))
        self.assertEqual(dlg.call_args[0][1], "Table Engine")

    def test_focus_navigation(self) -> None:
        self.assertEqual(self.screen.focused_widget_index, 0)
        self.screen.handle_key(curses.KEY_DOWN)
        self.assertEqual(self.screen.focused_widget_index, 1)
        self.screen.handle_key(9)  # Tab
        self.assertEqual(self.screen.focused_widget_index, 2)
        self.screen.handle_key(curses.KEY_UP)
        self.assertEqual(self.screen.focused_widget_index, 1)

    def test_save_via_hotkey_s(self) -> None:
        # Modify some values
        self.screen.sel_table_engine.set_selected(1)  # "direct"
        self.screen.sel_memory_mode.set_selected(2)   # "very_low"
        self.screen.inp_mem_max.set_value("48")
        self.screen.sel_hugepages.set_selected(1)     # "on"
        self.screen.inp_cache_max_table_mb.set_value("1024")
        self.screen.sel_cache_engine.set_selected(1)  # "sqlite"
        self.screen.inp_cache_dir.set_value(" /var/cache/kernelinfo ")
        self.screen.chk_dist_enabled.set_checked(True)
        self.screen.sel_role.set_selected(1)          # "master"
        self.screen.inp_worker_id.set_value("master-node-0")
        self.screen.chk_delegate_stable.set_checked(True)

        # Trigger save via 's'
        self.screen.focus_widget(0)  # On SelectList, not TextInput
        handled = self.screen.handle_key(ord("s"))
        self.assertTrue(handled)
        self.assertTrue(self.app.is_dirty)
        self.assertTrue(self.app.popped)

        # Verify saved config
        p_cfg = self.app.config["parser"]
        self.assertEqual(p_cfg["table_engine"], "direct")
        self.assertEqual(p_cfg["memory_mode"], "very_low")
        self.assertEqual(p_cfg["mem_max"], 48)
        self.assertEqual(p_cfg["hugepages"], "on")
        self.assertEqual(p_cfg["cache_max_table_mb"], 1024)

        c_cfg = self.app.config["cache"]
        self.assertEqual(c_cfg["engine"], "sqlite")
        self.assertEqual(c_cfg["dir"], "/var/cache/kernelinfo")

        d_cfg = p_cfg["distributed"]
        self.assertTrue(d_cfg["enabled"])
        self.assertEqual(d_cfg["role"], "master")
        self.assertEqual(d_cfg["worker_id"], "master-node-0")
        self.assertTrue(d_cfg["delegate_stable"])

    def test_save_via_button_enter(self) -> None:
        self.screen.inp_mem_max.set_value("32")
        # Focus save button
        btn_idx = self.screen.widgets.index(self.screen.btn_save)
        self.screen.focus_widget(btn_idx)
        handled = self.screen.handle_key(10)  # Enter
        self.assertTrue(handled)
        self.assertTrue(self.app.is_dirty)
        self.assertTrue(self.app.popped)
        self.assertEqual(self.app.config["parser"]["mem_max"], 32)

    def test_cancel_via_esc(self) -> None:
        self.screen.inp_mem_max.set_value("999")
        with patch("core.tui.widgets.ModalDialog.show_dialog", return_value="Discard") as dlg:
            handled = self.screen.handle_key(27)  # Esc
        dlg.assert_called_once()
        self.assertTrue(handled)
        self.assertTrue(self.app.popped)
        self.assertFalse(self.app.is_dirty)
        self.assertEqual(self.app.config["parser"]["mem_max"], 60)

    def test_draw_renders_cleanly(self) -> None:
        self.screen.draw()
        # Verify calls to stdscr
        addstr_calls = [c[1][2] for c in self.win.calls if c[0] == "addstr"]
        self.assertTrue(any("Parser" in text for text in addstr_calls))
        self.assertTrue(any("Local Cache" in text for text in addstr_calls))
        self.assertTrue(any("Distributed" in text for text in addstr_calls))


class TestCVEMailingListScreen(unittest.TestCase):
    """Test suite for CVEMailingListScreen configuration, test triggers, and persistence."""

    def setUp(self) -> None:
        self.win = MockCursesWindow(24, 80)
        self.initial_config = {
            "cve": {
                "repo_dir": "cvelistV5",
            },
            "mailing_list": {
                "storage_dir": "/srv/lore",
                "port": 8192,
                "auto_start_daemon": True,
                "ssh_tunnel": {
                    "enabled": False,
                    "host": "",
                    "port": 22,
                    "user": "",
                    "key_file": "",
                },
            },
        }
        self.app = MockApp(self.initial_config)
        self.screen = CVEMailingListScreen(self.win, self.app)

    def test_initial_values_loaded(self) -> None:
        self.assertEqual(self.screen.inp_cve_repo_dir.get_value(), "cvelistV5")
        self.assertEqual(self.screen.inp_ml_storage_dir.get_value(), "/srv/lore")
        self.assertEqual(self.screen.inp_ml_port.get_value(), "8192")
        self.assertTrue(self.screen.chk_ml_auto_start.is_checked())
        self.assertFalse(self.screen.chk_ssh_enabled.is_checked())
        self.assertEqual(self.screen.inp_ssh_port.get_value(), "22")

    @patch("core.tui.screens.cve_ml.ModalDialog.show", return_value="OK")
    @patch("core.tui.screens.cve_ml.test_cve_catalog")
    def test_cve_test_trigger_hotkey(self, mock_test_cve, mock_modal_show) -> None:
        mock_test_cve.return_value = DiagnosticResult(
            name="CVE Catalog",
            passed=True,
            latency_ms=15.0,
            message="Detected format: cvelistV5",
            details="Latest commit: 2026-03-01",
        )
        self.screen.inp_cve_repo_dir.set_value("vulns.git")
        self.screen.focus_widget(1)  # On btn_test_cve

        # Press 'c' to trigger test
        handled = self.screen.handle_key(ord("c"))
        self.assertTrue(handled)
        mock_test_cve.assert_called_once_with({"repo_dir": "vulns.git"})
        mock_modal_show.assert_called_once()

    @patch("core.tui.screens.cve_ml.ModalDialog.show", return_value="OK")
    @patch("core.tui.screens.cve_ml.test_ml_service")
    def test_mailing_list_test_trigger_hotkey(self, mock_test_ml, mock_modal_show) -> None:
        mock_test_ml.return_value = DiagnosticResult(
            name="Mailing List",
            passed=True,
            latency_ms=25.0,
            message="Storage dir accessible, daemon healthy",
            details="Port 8192 listening",
        )
        self.screen.inp_ml_port.set_value("9000")
        self.screen.chk_ssh_enabled.set_checked(True)
        self.screen.inp_ssh_host.set_value("tunnel.internal")
        self.screen.focus_widget(10)  # On btn_test_ml

        # Press 'm' to trigger ML test
        handled = self.screen.handle_key(ord("m"))
        self.assertTrue(handled)
        mock_test_ml.assert_called_once()
        called_arg = mock_test_ml.call_args[0][0]
        self.assertEqual(called_arg["port"], 9000)
        self.assertTrue(called_arg["ssh_tunnel"]["enabled"])
        self.assertEqual(called_arg["ssh_tunnel"]["host"], "tunnel.internal")
        mock_modal_show.assert_called_once()

    @patch("core.tui.screens.cve_ml.ModalDialog.show", return_value="OK")
    @patch("core.tui.screens.cve_ml.test_ml_service")
    def test_mailing_list_test_keeps_config_only_tunnel_fields(self, mock_test_ml, _mock_show) -> None:
        """Tunnel fields without a widget (password, remote/local port) reach the diagnostic."""
        mock_test_ml.return_value = DiagnosticResult(name="Mailing List", passed=True, latency_ms=1.0, message="ok")
        self.app.config["mailing_list"]["ssh_tunnel"].update(
            {"password": "s3cret", "remote_port": 8200, "local_port": 18192}
        )
        screen = CVEMailingListScreen(self.win, self.app)
        screen.chk_ssh_enabled.set_checked(True)
        screen.inp_ssh_host.set_value("lore.example")
        screen.test_mailing_list()

        ssh_arg = mock_test_ml.call_args[0][0]["ssh_tunnel"]
        self.assertEqual(ssh_arg["host"], "lore.example")
        self.assertEqual(ssh_arg["password"], "s3cret")
        self.assertEqual(ssh_arg["remote_port"], 8200)
        self.assertEqual(ssh_arg["local_port"], 18192)

    def test_save_persists_config(self) -> None:
        self.screen.inp_cve_repo_dir.set_value("custom_cve")
        self.screen.inp_ml_storage_dir.set_value("/mnt/storage/lore")
        self.screen.inp_ml_port.set_value("8500")
        self.screen.chk_ml_auto_start.set_checked(False)
        self.screen.chk_ssh_enabled.set_checked(True)
        self.screen.inp_ssh_host.set_value("ssh.proxy.local")
        self.screen.inp_ssh_port.set_value("2222")
        self.screen.inp_ssh_user.set_value("lore_user")
        self.screen.inp_ssh_key_file.set_value("~/.ssh/lore_id")

        self.screen.save()
        self.assertTrue(self.app.is_dirty)
        self.assertTrue(self.app.popped)

        self.assertEqual(self.app.config["cve"]["repo_dir"], "custom_cve")
        ml_cfg = self.app.config["mailing_list"]
        self.assertEqual(ml_cfg["storage_dir"], "/mnt/storage/lore")
        self.assertEqual(ml_cfg["port"], 8500)
        self.assertFalse(ml_cfg["auto_start_daemon"])
        ssh_cfg = ml_cfg["ssh_tunnel"]
        self.assertTrue(ssh_cfg["enabled"])
        self.assertEqual(ssh_cfg["host"], "ssh.proxy.local")
        self.assertEqual(ssh_cfg["port"], 2222)
        self.assertEqual(ssh_cfg["user"], "lore_user")
        self.assertEqual(ssh_cfg["key_file"], "~/.ssh/lore_id")

    def test_cancel_via_esc(self) -> None:
        self.screen.inp_cve_repo_dir.set_value("will_not_save")
        with patch("core.tui.widgets.ModalDialog.show_dialog", return_value="Discard") as dlg:
            self.screen.handle_key(27)  # Esc
        dlg.assert_called_once()
        self.assertTrue(self.app.popped)
        self.assertFalse(self.app.is_dirty)
        self.assertEqual(self.app.config["cve"]["repo_dir"], "cvelistV5")

    def test_draw_renders_cleanly(self) -> None:
        self.screen.draw()
        addstr_calls = [c[1][2] for c in self.win.calls if c[0] == "addstr"]
        self.assertTrue(any("CVE Catalog" in text for text in addstr_calls))
        self.assertTrue(any("Mailing List" in text for text in addstr_calls))


class TestDiagnosticsScreen(unittest.TestCase):
    """Test suite for DiagnosticsScreen dashboard, test refreshes, details dialog, and scrolling."""

    def setUp(self) -> None:
        self.win = MockCursesWindow(24, 80)
        self.app = MockApp({"database": {"host": "127.0.0.1"}})
        self.screen = DiagnosticsScreen(self.win, self.app)

    @patch("core.tui.screens.diagnostics.run_all_diagnostics")
    def test_on_enter_runs_diagnostics(self, mock_run_diag) -> None:
        mock_run_diag.return_value = [
            DiagnosticResult(name="Database", passed=True, latency_ms=12.0, message="Connected successfully"),
            DiagnosticResult(name="Git Repos", passed=False, latency_ms=8.0, message="Missing repo clone", details="Path not found"),
        ]
        self.screen.on_enter()
        mock_run_diag.assert_called_once_with(self.app.config)
        self.assertEqual(len(self.screen.results), 2)
        self.assertTrue(self.screen.results[0].passed)
        self.assertFalse(self.screen.results[1].passed)

    @patch("core.tui.screens.diagnostics.run_all_diagnostics")
    def test_refresh_hotkey_r(self, mock_run_diag) -> None:
        mock_run_diag.return_value = [
            DiagnosticResult(name="SSH Tunnel", passed=True, latency_ms=5.0, message="Port 22 reachable"),
        ]
        handled = self.screen.handle_key(ord("r"))
        self.assertTrue(handled)
        mock_run_diag.assert_called_once()
        self.assertEqual(len(self.screen.results), 1)

    def test_scroll_navigation(self) -> None:
        self.screen.results = [
            DiagnosticResult(name=f"Test-{i}", passed=True, latency_ms=float(i), message=f"Msg {i}")
            for i in range(10)
        ]
        self.screen.list_widget.set_items(self.screen.results)
        self.assertEqual(self.screen.list_widget.selected_index, 0)

        # Down navigation
        self.screen.handle_key(curses.KEY_DOWN)
        self.assertEqual(self.screen.list_widget.selected_index, 1)

        self.screen.handle_key(ord("j"))
        self.assertEqual(self.screen.list_widget.selected_index, 2)

        # Up navigation
        self.screen.handle_key(curses.KEY_UP)
        self.assertEqual(self.screen.list_widget.selected_index, 1)

        self.screen.handle_key(ord("k"))
        self.assertEqual(self.screen.list_widget.selected_index, 0)

        # Home and End
        self.screen.handle_key(curses.KEY_END)
        self.assertEqual(self.screen.list_widget.selected_index, 9)

        self.screen.handle_key(curses.KEY_HOME)
        self.assertEqual(self.screen.list_widget.selected_index, 0)

    @patch("core.tui.screens.diagnostics.ModalDialog.show", return_value="OK")
    def test_show_details_dialog(self, mock_modal_show) -> None:
        self.screen.results = [
            DiagnosticResult(
                name="Database Connection",
                passed=False,
                latency_ms=105.2,
                message="Access denied for user 'root'@'localhost'",
                details="Traceback (most recent call last):\n  File 'test.py', line 10\nOperationalError 1045",
            )
        ]
        self.screen.list_widget.set_items(self.screen.results)

        # Press Enter on item
        handled = self.screen.handle_key(10)
        self.assertTrue(handled)
        mock_modal_show.assert_called_once()

        # Press Space on item
        handled_space = self.screen.handle_key(ord(" "))
        self.assertTrue(handled_space)
        self.assertEqual(mock_modal_show.call_count, 2)

    def test_esc_pops_screen(self) -> None:
        handled = self.screen.handle_key(27)  # Esc
        self.assertTrue(handled)
        self.assertTrue(self.app.popped)

    def test_draw_renders_pass_fail_badges(self) -> None:
        self.screen.results = [
            DiagnosticResult(name="DB Test", passed=True, latency_ms=15.0, message="OK"),
            DiagnosticResult(name="Git Test", passed=False, latency_ms=45.0, message="Failed", details="Err"),
        ]
        self.screen.list_widget.set_items(self.screen.results)
        self.screen.draw()

        addstr_calls = [c[1][2] for c in self.win.calls if c[0] == "addstr"]
        self.assertTrue(any("[ PASS ]" in text for text in addstr_calls))
        self.assertTrue(any("[ FAIL ]" in text for text in addstr_calls))
        self.assertTrue(any("DB Test" in text for text in addstr_calls))
        self.assertTrue(any("Git Test" in text for text in addstr_calls))
        self.assertTrue(any("Total: 2 | Passed: 1 | Failed: 1" in text for text in addstr_calls))

    def test_draw_empty_results(self) -> None:
        self.screen.results = []
        self.screen.list_widget.set_items([])
        self.screen.draw()
        addstr_calls = [c[1][2] for c in self.win.calls if c[0] == "addstr"]
        self.assertTrue(any("No diagnostic tests" in text for text in addstr_calls))


if __name__ == "__main__":
    unittest.main()
