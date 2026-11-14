"""tests/test_logging_screen.py - Unit tests for LoggingScreen and MainMenuScreen Logging entry."""

import curses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.tui.app import TUIApp
from core.tui.screens.logging_cfg import HELP_TOPICS, LoggingScreen
from core.tui.screens.main_menu import MainMenuScreen
from tests.test_tui import MockCursesWindow


class _MockApp:
    """Minimal TUIApp stub exposing config, dirty flag, and pop_screen tracking."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config if config is not None else {}
        self.is_dirty = False
        self.popped = False

    def pop_screen(self) -> None:
        self.popped = True


class TestMainMenuLoggingIntegration(unittest.TestCase):
    """Verify MainMenuScreen exposes Logging Configuration via menu item and 'L' hotkey."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.app = TUIApp(self.config_path)
        self.mock_stdscr = MagicMock()
        self.mock_stdscr.getmaxyx.return_value = (30, 100)
        self.menu = MainMenuScreen(self.mock_stdscr, self.app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_logging_menu_item_present(self) -> None:
        keys = [item[0] for item in MainMenuScreen.MENU_ITEMS]
        self.assertIn("L", keys)
        self.assertEqual(MainMenuScreen.HOTKEYS["l"], "L")

    def test_hotkey_l_pushes_logging_screen(self) -> None:
        for key in (ord("l"), ord("L")):
            with patch.object(self.app, "push_screen") as mock_push:
                handled = self.menu.handle_key(key)
                self.assertTrue(handled)
                self.assertTrue(mock_push.called)
                pushed = mock_push.call_args[0][0]
                self.assertIsInstance(pushed, LoggingScreen)


class TestLoggingScreen(unittest.TestCase):
    """Test suite for LoggingScreen loading, editing, validation, help, and persistence."""

    def setUp(self) -> None:
        self.win = MockCursesWindow(24, 90)
        self.initial_config = {
            "logging": {
                "parser": {
                    "enabled": True,
                    "dir": "/tmp/p_logs",
                    "file": "p.log",
                    "error_dir": "/tmp/p_errs",
                    "error_file": "p_err.log",
                    "level": "DEBUG",
                },
                "webapp": {
                    "enabled": False,
                    "dir": "/tmp/w_logs",
                    "file": "w.log",
                    "request_mode": "errors",
                    "error_dir": "/tmp/w_errs",
                    "error_file": "w_err.log",
                    "level": "WARNING",
                },
            }
        }
        self.app = _MockApp(self.initial_config)
        self.screen = LoggingScreen(self.win, self.app)

    def test_initial_values_loaded(self) -> None:
        self.assertTrue(self.screen.chk_parser_enabled.is_checked())
        self.assertEqual(self.screen.inp_parser_dir.get_value(), "/tmp/p_logs")
        self.assertEqual(self.screen.inp_parser_file.get_value(), "p.log")
        self.assertEqual(self.screen.inp_parser_error_dir.get_value(), "/tmp/p_errs")
        self.assertEqual(self.screen.inp_parser_error_file.get_value(), "p_err.log")
        self.assertEqual(self.screen.sel_parser_level.get_selected(), "DEBUG")

        self.assertFalse(self.screen.chk_webapp_enabled.is_checked())
        self.assertEqual(self.screen.sel_webapp_request_mode.get_selected(), "errors")
        self.assertEqual(self.screen.inp_webapp_dir.get_value(), "/tmp/w_logs")
        self.assertEqual(self.screen.inp_webapp_file.get_value(), "w.log")
        self.assertEqual(self.screen.inp_webapp_error_dir.get_value(), "/tmp/w_errs")
        self.assertEqual(self.screen.inp_webapp_error_file.get_value(), "w_err.log")
        self.assertEqual(self.screen.sel_webapp_level.get_selected(), "WARNING")

    def test_defaults_when_logging_section_missing(self) -> None:
        app = _MockApp({})
        screen = LoggingScreen(self.win, app)
        self.assertFalse(screen.chk_parser_enabled.is_checked())
        self.assertEqual(screen.inp_parser_file.get_value(), "parser.log")
        self.assertEqual(screen.sel_parser_level.get_selected(), "INFO")
        self.assertFalse(screen.chk_webapp_enabled.is_checked())
        self.assertEqual(screen.sel_webapp_request_mode.get_selected(), "all")
        self.assertEqual(screen.inp_webapp_file.get_value(), "webapp.log")
        self.assertEqual(screen.sel_webapp_level.get_selected(), "INFO")

    def test_draw_narrow_and_wide(self) -> None:
        self.screen.draw()
        wide_text = "\n".join(str(args[2]) for name, args, _ in self.win.calls if name == "addstr" and len(args) >= 3)
        self.assertIn("Logging Configuration", wide_text)
        self.assertIn("Parser Logging", wide_text)
        self.assertIn("Webapp Backend Logging", wide_text)

        narrow = MockCursesWindow(24, 78)
        narrow_screen = LoggingScreen(narrow, self.app)
        narrow_screen.draw()
        narrow_text = "\n".join(str(args[2]) for name, args, _ in narrow.calls if name == "addstr" and len(args) >= 3)
        self.assertIn("Logging Configuration", narrow_text)

    def test_edit_and_save_persists_all_fields(self) -> None:
        self.screen.chk_parser_enabled.checked = False
        self.screen.inp_parser_dir.set_value("logs/parser")
        self.screen.inp_parser_file.set_value("custom_parser.log")
        self.screen.inp_parser_error_dir.set_value("logs/parser_err")
        self.screen.inp_parser_error_file.set_value("custom_p_err.log")
        self.screen.sel_parser_level.selected_index = self.screen.sel_parser_level.options.index("ERROR")

        self.screen.chk_webapp_enabled.checked = True
        self.screen.sel_webapp_request_mode.selected_index = (
            self.screen.sel_webapp_request_mode.options.index("all")
        )
        self.screen.inp_webapp_dir.set_value("logs/webapp")
        self.screen.inp_webapp_file.set_value("custom_webapp.log")
        self.screen.inp_webapp_error_dir.set_value("logs/webapp_err")
        self.screen.inp_webapp_error_file.set_value("custom_w_err.log")
        self.screen.sel_webapp_level.selected_index = self.screen.sel_webapp_level.options.index("CRITICAL")

        self.assertTrue(self.screen.form_is_dirty())
        ok = self.screen.save()
        self.assertTrue(ok)
        self.assertTrue(self.app.is_dirty)
        self.assertTrue(self.app.popped)
        self.assertFalse(self.screen.form_is_dirty())

        p = self.app.config["logging"]["parser"]
        self.assertFalse(p["enabled"])
        self.assertEqual(p["dir"], "logs/parser")
        self.assertEqual(p["file"], "custom_parser.log")
        self.assertEqual(p["error_dir"], "logs/parser_err")
        self.assertEqual(p["error_file"], "custom_p_err.log")
        self.assertEqual(p["level"], "ERROR")

        w = self.app.config["logging"]["webapp"]
        self.assertTrue(w["enabled"])
        self.assertEqual(w["request_mode"], "all")
        self.assertEqual(w["dir"], "logs/webapp")
        self.assertEqual(w["file"], "custom_webapp.log")
        self.assertEqual(w["error_dir"], "logs/webapp_err")
        self.assertEqual(w["error_file"], "custom_w_err.log")
        self.assertEqual(w["level"], "CRITICAL")

    def test_nul_byte_validation_blocks_save(self) -> None:
        self.screen.inp_webapp_dir.set_value("/tmp/bad\x00path")
        with patch("core.tui.screens.logging_cfg.ModalDialog.show", return_value="OK") as mock_dlg:
            ok = self.screen.save()
        self.assertFalse(ok)
        self.assertTrue(mock_dlg.called)
        self.assertFalse(self.app.popped)
        self.assertEqual(self.app.config["logging"]["webapp"]["dir"], "/tmp/w_logs")

    def test_cancel_confirms_only_when_dirty(self) -> None:
        with patch.object(self.screen, "confirm_discard", return_value=False) as mock_cd:
            self.screen.inp_parser_file.set_value("changed.log")
            self.screen.cancel()
        self.assertTrue(mock_cd.called)
        self.assertFalse(self.app.popped)

        self.screen.mark_form_clean()
        self.screen.cancel()
        self.assertTrue(self.app.popped)

    def test_help_topic_and_f1_dialog(self) -> None:
        self.screen.focus_widget(self.screen.widgets.index(self.screen.sel_webapp_request_mode))
        self.assertEqual(self.screen.current_help_topic(), "request_mode")
        self.assertIn("request_mode", HELP_TOPICS)
        with patch("core.tui.screens.logging_cfg.ModalDialog.show", return_value="OK") as mock_dlg:
            handled = self.screen.handle_key(getattr(curses, "KEY_F1", 265))
        self.assertTrue(handled)
        self.assertTrue(mock_dlg.called)


if __name__ == "__main__":
    unittest.main()
