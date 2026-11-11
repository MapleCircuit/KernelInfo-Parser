"""Unit tests for core/tui/app.py and core/tui/screens/main_menu.py."""

import curses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.tui.app import TUIApp, run_config_tui
from core.tui.screens.base import BaseScreen
from core.tui.screens.main_menu import MainMenuScreen


class DummyScreen(BaseScreen):
    """Dummy screen for testing screen stack transitions."""

    def __init__(self, stdscr=None, app=None):
        super().__init__(stdscr, app)
        self.entered = False
        self.left = False

    def on_enter(self) -> None:
        self.entered = True

    def on_leave(self) -> None:
        self.left = True

    def draw(self) -> None:
        pass


class TestTUIApp(unittest.TestCase):
    """Test suite for TUIApp orchestrator."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_init_with_nonexistent_file_uses_defaults(self):
        app = TUIApp(self.config_path)
        self.assertFalse(self.config_path.exists())
        self.assertIn("database", app.config)
        self.assertIn("parser", app.config)
        self.assertIn("workspace", app.config)
        self.assertFalse(app.is_dirty)

    def test_init_with_existing_file_loads_and_merges(self):
        test_data = {
            "database": {"host": "192.168.1.100", "port": 3307},
            "custom_key": "custom_value",
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(test_data, f)

        app = TUIApp(self.config_path)
        self.assertEqual(app.config["database"]["host"], "192.168.1.100")
        self.assertEqual(app.config["database"]["port"], 3307)
        # Defaults merged
        self.assertEqual(app.config["database"]["engine"], "mariadb")
        self.assertEqual(app.config["custom_key"], "custom_value")

    def test_screen_stack_push_pop(self):
        app = TUIApp(self.config_path)
        s1 = DummyScreen(None, app)
        s2 = DummyScreen(None, app)

        self.assertIsNone(app.current_screen())

        app.push_screen(s1)
        self.assertTrue(s1.entered)
        self.assertIs(app.current_screen(), s1)

        app.push_screen(s2)
        self.assertTrue(s2.entered)
        self.assertIs(app.current_screen(), s2)

        popped = app.pop_screen()
        self.assertIs(popped, s2)
        self.assertTrue(s2.left)
        self.assertIs(app.current_screen(), s1)

        popped_first = app.pop_screen()
        self.assertIs(popped_first, s1)
        self.assertTrue(s1.left)
        self.assertIsNone(app.current_screen())

    def test_legacy_top_level_ssh_tunnel_is_migrated_and_not_duplicated(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"ssh_tunnel": {"enabled": True, "host": "legacy.example"}}))
            app = TUIApp(path)
            tunnel = app.config["database"]["ssh_tunnel"]
            self.assertTrue(tunnel["enabled"])
            self.assertEqual(tunnel["host"], "legacy.example")

            ok, _ = app.save_config()
            self.assertTrue(ok)
            written = json.loads(path.read_text())
            self.assertNotIn("ssh_tunnel", written)
            self.assertEqual(written["database"]["ssh_tunnel"]["host"], "legacy.example")
            self.assertIn("ssh_tunnel", app.config)  # in-memory alias untouched

    def test_save_config_atomic(self):
        app = TUIApp(self.config_path)
        app.config["database"]["user"] = "new_user"
        app.is_dirty = True

        success, msg = app.save_config()
        self.assertTrue(success)
        self.assertFalse(app.is_dirty)
        self.assertTrue(self.config_path.exists())

        with open(self.config_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["database"]["user"], "new_user")

    def test_exit(self):
        app = TUIApp(self.config_path)
        app.is_running = True
        app.exit()
        self.assertFalse(app.is_running)


class TestMainMenuScreen(unittest.TestCase):
    """Test suite for MainMenuScreen navigation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.app = TUIApp(self.config_path)
        self.mock_stdscr = MagicMock()
        self.mock_stdscr.getmaxyx.return_value = (30, 100)
        self.screen = MainMenuScreen(self.mock_stdscr, self.app)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_draw_renders_cleanly(self):
        self.screen.draw()
        self.assertTrue(self.mock_stdscr.erase.called)
        self.assertTrue(self.mock_stdscr.refresh.called)

    def test_keyboard_navigation(self):
        self.assertEqual(self.screen.selected_index, 0)

        # Down arrow
        self.screen.handle_key(curses.KEY_DOWN)
        self.assertEqual(self.screen.selected_index, 1)

        # Up arrow
        self.screen.handle_key(curses.KEY_UP)
        self.assertEqual(self.screen.selected_index, 0)

        # Wrap around up
        self.screen.handle_key(curses.KEY_UP)
        self.assertEqual(self.screen.selected_index, len(MainMenuScreen.MENU_ITEMS) - 1)

    def test_hotkey_screens_activation(self):
        # Hotkey 1: Repositories
        with patch.object(self.app, "push_screen") as mock_push:
            self.screen.handle_key(ord("1"))
            self.assertTrue(mock_push.called)

        # Hotkey 2: Database
        with patch.object(self.app, "push_screen") as mock_push:
            self.screen.handle_key(ord("2"))
            self.assertTrue(mock_push.called)

        # Hotkey 3: Parser
        with patch.object(self.app, "push_screen") as mock_push:
            self.screen.handle_key(ord("3"))
            self.assertTrue(mock_push.called)

        # Hotkey 4: CVE
        with patch.object(self.app, "push_screen") as mock_push:
            self.screen.handle_key(ord("4"))
            self.assertTrue(mock_push.called)

        # Hotkey 5: Diagnostics
        with patch.object(self.app, "push_screen") as mock_push:
            self.screen.handle_key(ord("5"))
            self.assertTrue(mock_push.called)

    def test_hotkey_save_and_exit(self):
        # Hotkey S: Save
        with patch("core.tui.screens.main_menu.ModalDialog.show") as mock_dlg:
            self.screen.handle_key(ord("s"))
            self.assertTrue(mock_dlg.called)
            self.assertTrue(self.config_path.exists())

        # Hotkey R: Save & Run Pipeline
        self.screen.handle_key(ord("r"))
        self.assertTrue(self.app.should_run_pipeline)
        self.assertFalse(self.app.is_running)

        # Hotkey Q: Exit without dirty flag
        self.app.is_running = True
        self.app.is_dirty = False
        self.screen.handle_key(ord("q"))
        self.assertFalse(self.app.is_running)

    def test_real_dialogs_on_save_and_dirty_exit(self):
        """Save / unsaved-exit dialogs run the real ModalDialog against the screen's stdscr."""
        self.mock_stdscr.getch.return_value = 10  # Enter selects the default (first) button

        self.screen.handle_key(ord("s"))
        self.assertTrue(self.config_path.exists())

        self.app.is_running = True
        self.app.is_dirty = True
        self.screen.handle_key(ord("q"))  # "Discard & Exit" is the default button
        self.assertFalse(self.app.is_running)


if __name__ == "__main__":
    unittest.main()

