"""tests/test_tui_reentry.py - Save -> back to main menu -> re-open round trips for the TUI config screens.

Each test drives a real `TUIApp` with real key codes (`handle_key()` followed by `draw()`), saves a screen,
returns to the main menu, opens the same menu entry again (a fresh screen instance), and checks that every
editable widget shows the saved value. The same values are then checked after `app.save_config()` and a
fresh `TUIApp` loaded from the written file.
"""

from __future__ import annotations

import curses
import json
import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from core.tui.app import TUIApp
from core.tui.screens.cve_ml import CVEMailingListScreen
from core.tui.screens.database_ssh import DatabaseSSHScreen
from core.tui.screens.main_menu import MainMenuScreen
from core.tui.screens.parser_dist import ParserDistributedScreen
from core.tui.widgets import Button, Checkbox, ModalDialog, SelectList, TextInput
from tests.test_tui import MockCursesWindow

KEY_TAB = 9
KEY_ENTER = 10
KEY_ESC = 27
KEY_BACKSPACE = 127
KEY_SPACE = ord(" ")

MENU_DATABASE = ord("2")
MENU_PARSER = ord("3")
MENU_CVE = ord("4")


class _ModalRecorder:
    """Stand-in for `ModalDialog.show` recording each dialog and answering with its default button."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []
        # dialog title -> button to answer with instead of the default one
        self.answers: dict[str, str] = {}

    def __call__(self, dialog: ModalDialog) -> str | None:
        self.shown.append((dialog.title, dialog.message))
        if dialog.title in self.answers:
            return self.answers[dialog.title]
        buttons = getattr(dialog, "buttons", None) or ["OK"]
        idx = getattr(dialog, "selected_button_index", 0)
        return buttons[idx] if 0 <= idx < len(buttons) else buttons[0]


class _ReentryCase(unittest.TestCase):
    """Shared harness: a temp config file, a real TUIApp and real key driving."""

    menu_key: int = 0
    screen_cls: type = object

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp.name) / "config.json"
        self.modals = _ModalRecorder()
        recorder = self.modals

        def _show(dialog: ModalDialog) -> str | None:
            return recorder(dialog)

        app_logger = logging.getLogger("core.tui.app")
        prev_level = app_logger.level
        app_logger.setLevel(logging.WARNING)
        self.addCleanup(app_logger.setLevel, prev_level)

        patcher = patch.object(ModalDialog, "show", _show)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    # --- app lifecycle ---------------------------------------------------------------------------------

    def write_config(self, data: dict[str, Any]) -> None:
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

    def new_app(self) -> TUIApp:
        app = TUIApp(self.config_path)
        app.stdscr = MockCursesWindow(40, 120)
        app.push_screen(MainMenuScreen(app.stdscr, app))
        app.current_screen().draw()
        return app

    def press(self, app: TUIApp, *keys: int) -> None:
        for ch in keys:
            screen = app.current_screen()
            self.assertIsNotNone(screen)
            screen.handle_key(ch)
            current = app.current_screen()
            if current is not None:
                current.draw()

    def open_screen(self, app: TUIApp) -> Any:
        self.assertIsInstance(app.current_screen(), MainMenuScreen)
        self.press(app, self.menu_key)
        screen = app.current_screen()
        self.assertIsInstance(screen, self.screen_cls)
        return screen

    def back_to_menu(self, app: TUIApp) -> None:
        """Esc from an unmodified screen returns to the main menu without a dialog."""
        before = len(self.modals.shown)
        self.press(app, KEY_ESC)
        self.assertIsInstance(app.current_screen(), MainMenuScreen)
        self.assertEqual(len(self.modals.shown), before, self.modals.shown[before:])

    # --- widget driving --------------------------------------------------------------------------------

    def focus(self, app: TUIApp, widget: Any) -> None:
        screen = app.current_screen()
        self.assertIn(widget, screen.widgets)
        for _ in range(len(screen.widgets)):
            if screen.get_focused_widget() is widget:
                return
            self.press(app, KEY_TAB)
        self.assertIs(screen.get_focused_widget(), widget)

    def type_text(self, app: TUIApp, widget: TextInput, text: str) -> None:
        self.focus(app, widget)
        self.press(app, curses.KEY_END)
        self.press(app, *([KEY_BACKSPACE] * len(widget.get_value())))
        self.press(app, *[ord(c) for c in text])
        self.assertEqual(widget.get_value(), text)

    def choose(self, app: TUIApp, widget: SelectList, option: str) -> None:
        self.assertIn(option, widget.options)
        self.focus(app, widget)
        for _ in range(len(widget.options)):
            if widget.get_selected() == option:
                return
            self.press(app, curses.KEY_RIGHT)
        self.assertEqual(widget.get_selected(), option)

    def tick(self, app: TUIApp, widget: Checkbox, checked: bool) -> None:
        self.focus(app, widget)
        if widget.is_checked() != checked:
            self.press(app, KEY_SPACE)
        self.assertEqual(widget.is_checked(), checked)

    def apply(self, app: TUIApp, screen: Any, values: dict[str, Any]) -> None:
        for attr, value in values.items():
            widget = getattr(screen, attr)
            if isinstance(widget, TextInput):
                self.type_text(app, widget, value)
            elif isinstance(widget, SelectList):
                self.choose(app, widget, value)
            elif isinstance(widget, Checkbox):
                self.tick(app, widget, value)
            else:  # pragma: no cover - harness misuse
                self.fail(f"unsupported widget {attr}: {type(widget).__name__}")

    def press_save_button(self, app: TUIApp, screen: Any, button: Button) -> None:
        before = len(self.modals.shown)
        self.focus(app, button)
        self.press(app, KEY_ENTER)
        self.assertIsInstance(app.current_screen(), MainMenuScreen, self.modals.shown[before:])
        self.assertEqual(len(self.modals.shown), before, self.modals.shown[before:])
        self.assertTrue(app.is_dirty)

    def read_widgets(self, screen: Any, attrs: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for attr in attrs:
            widget = getattr(screen, attr)
            if isinstance(widget, TextInput):
                out[attr] = widget.get_value()
            elif isinstance(widget, SelectList):
                out[attr] = widget.get_selected()
            elif isinstance(widget, Checkbox):
                out[attr] = widget.is_checked()
        return out

    def round_trip(self, values: dict[str, Any], save_button: str = "btn_save") -> TUIApp:
        """Edit, save, re-open in the same app, then save to disk and re-open in a fresh app."""
        app = self.new_app()
        screen = self.open_screen(app)
        self.apply(app, screen, values)
        self.press_save_button(app, screen, getattr(screen, save_button))

        reopened = self.open_screen(app)
        self.assertIsNot(reopened, screen)
        self.assertEqual(self.read_widgets(reopened, list(values)), values)
        self.back_to_menu(app)

        # A second visit after a no-change re-entry still shows the values.
        again = self.open_screen(app)
        self.assertEqual(self.read_widgets(again, list(values)), values)
        self.back_to_menu(app)

        ok, msg = app.save_config()
        self.assertTrue(ok, msg)
        self.assertFalse(app.is_dirty)

        fresh = self.new_app()
        from_disk = self.open_screen(fresh)
        self.assertEqual(self.read_widgets(from_disk, list(values)), values)
        self.back_to_menu(fresh)
        return fresh


class TestParserDistributedReentry(_ReentryCase):
    """ParserDistributedScreen: every widget keeps its saved value across re-entry and reload."""

    menu_key = MENU_PARSER
    screen_cls = ParserDistributedScreen

    TRUTHY = {
        "sel_table_engine": "direct",
        "sel_memory_mode": "very_low",
        "inp_mem_max": "48",
        "sel_hugepages": "off",
        "inp_cache_max_table_mb": "1024",
        "sel_cache_engine": "sqlite",
        "inp_cache_dir": "/var/cache/kernelinfo",
        "chk_dist_enabled": True,
        "sel_role": "worker",
        "inp_worker_id": "node-7",
        "chk_delegate_stable": True,
    }

    FALSY = {
        "sel_table_engine": "cached",
        "sel_memory_mode": "normal",
        "inp_mem_max": "0",
        "sel_hugepages": "auto",
        "inp_cache_max_table_mb": "0",
        "sel_cache_engine": "ram",
        "inp_cache_dir": "",
        "chk_dist_enabled": False,
        "sel_role": "standalone",
        "inp_worker_id": "",
        "chk_delegate_stable": False,
    }

    def test_all_fields_round_trip(self) -> None:
        self.round_trip(dict(self.TRUTHY))
        disk = json.loads(self.config_path.read_text(encoding="utf-8"))
        cfg = disk["parser"]
        self.assertEqual(cfg["mem_max"], 48)
        self.assertEqual(cfg["cache_max_table_mb"], 1024)
        self.assertEqual(disk["cache"], {"engine": "sqlite", "dir": "/var/cache/kernelinfo"})
        self.assertEqual(cfg["distributed"], {
            "enabled": True, "role": "worker", "worker_id": "node-7", "delegate_stable": True,
        })

    def test_falsy_values_round_trip_after_truthy(self) -> None:
        self.round_trip(dict(self.TRUTHY))
        self.round_trip(dict(self.FALSY))
        disk = json.loads(self.config_path.read_text(encoding="utf-8"))
        cfg = disk["parser"]
        self.assertEqual(cfg["mem_max"], 0)
        self.assertEqual(cfg["cache_max_table_mb"], 0)
        self.assertEqual(disk["cache"], {"engine": "ram", "dir": ""})
        self.assertEqual(cfg["distributed"]["worker_id"], "")
        self.assertIs(cfg["distributed"]["delegate_stable"], False)

    def test_cleared_worker_id_does_not_fall_back_to_legacy_key(self) -> None:
        self.write_config({"parser": {"worker_id": "legacy-node", "role": "master"}})
        app = self.new_app()
        screen = self.open_screen(app)
        # Without a parser.distributed entry, the legacy flat keys seed the form.
        self.assertEqual(screen.inp_worker_id.get_value(), "legacy-node")
        self.assertEqual(screen.sel_role.get_selected(), "master")
        self.press(app, KEY_ESC)
        self.round_trip({"inp_worker_id": "", "sel_role": "standalone"})

    def test_string_booleans_in_file_are_read_as_booleans(self) -> None:
        self.write_config({"parser": {"distributed": {"enabled": "0", "delegate_stable": "yes"}}})
        app = self.new_app()
        screen = self.open_screen(app)
        self.assertFalse(screen.chk_dist_enabled.is_checked())
        self.assertTrue(screen.chk_delegate_stable.is_checked())

    def test_invalid_number_keeps_screen_open_and_reports(self) -> None:
        app = self.new_app()
        screen = self.open_screen(app)
        self.type_text(app, screen.inp_mem_max, "lots")
        self.focus(app, screen.btn_save)
        self.press(app, KEY_ENTER)
        self.assertIs(app.current_screen(), screen)
        self.assertEqual(len(self.modals.shown), 1)
        self.assertIn("RAM Ceiling", self.modals.shown[0][1])
        self.assertFalse(app.is_dirty)
        self.assertEqual(app.config["parser"]["mem_max"], 60)

    def test_ctrl_s_saves_from_text_input(self) -> None:
        app = self.new_app()
        screen = self.open_screen(app)
        self.type_text(app, screen.inp_worker_id, "sss")
        self.press(app, 19)  # Ctrl+S
        self.assertIsInstance(app.current_screen(), MainMenuScreen)
        reopened = self.open_screen(app)
        self.assertEqual(reopened.inp_worker_id.get_value(), "sss")

    def test_esc_discards_edits(self) -> None:
        app = self.new_app()
        screen = self.open_screen(app)
        self.type_text(app, screen.inp_mem_max, "12")
        self.modals.answers["Unsaved Changes"] = "Discard"
        self.press(app, KEY_ESC)
        self.assertIn("Unsaved Changes", [t for t, _ in self.modals.shown])
        reopened = self.open_screen(app)
        self.assertEqual(reopened.inp_mem_max.get_value(), "60")

    def test_esc_with_edits_cancel_stays_on_screen(self) -> None:
        app = self.new_app()
        screen = self.open_screen(app)
        self.type_text(app, screen.inp_mem_max, "12")
        self.press(app, KEY_ESC)  # default answer of the prompt is Cancel
        self.assertIs(app.current_screen(), screen)
        self.assertEqual(screen.inp_mem_max.get_value(), "12")


class TestCVEMailingListReentry(_ReentryCase):
    """CVEMailingListScreen: every widget keeps its saved value across re-entry and reload."""

    menu_key = MENU_CVE
    screen_cls = CVEMailingListScreen

    TRUTHY = {
        "inp_cve_repo_dir": "/data/vulns",
        "inp_ml_storage_dir": "/mnt/lore",
        "inp_ml_port": "9001",
        "chk_ml_auto_start": False,
        "chk_ssh_enabled": True,
        "inp_ssh_host": "lore.example.net",
        "inp_ssh_port": "2222",
        "inp_ssh_user": "lore",
        "inp_ssh_key_file": "~/.ssh/id_lore",
    }

    FALSY = {
        "inp_cve_repo_dir": "",
        "inp_ml_storage_dir": "",
        "inp_ml_port": "8192",
        "chk_ml_auto_start": True,
        "chk_ssh_enabled": False,
        "inp_ssh_host": "",
        "inp_ssh_port": "22",
        "inp_ssh_user": "",
        "inp_ssh_key_file": "",
    }

    def test_all_fields_round_trip(self) -> None:
        self.round_trip(dict(self.TRUTHY))
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["cve"]["repo_dir"], "/data/vulns")
        ml = data["mailing_list"]
        self.assertEqual(ml["port"], 9001)
        self.assertIs(ml["auto_start_daemon"], False)
        self.assertEqual(ml["ssh_tunnel"]["port"], 2222)
        # Config-only tunnel fields without a widget survive the save.
        self.assertEqual(ml["ssh_tunnel"]["remote_port"], 8192)
        self.assertEqual(ml["ssh_tunnel"]["strict_host_key_checking"], "accept-new")

    def test_falsy_values_round_trip_after_truthy(self) -> None:
        self.round_trip(dict(self.TRUTHY))
        self.round_trip(dict(self.FALSY))
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["cve"]["repo_dir"], "")
        self.assertEqual(data["mailing_list"]["ssh_tunnel"]["host"], "")

    def test_string_booleans_in_file_are_read_as_booleans(self) -> None:
        self.write_config({"mailing_list": {"auto_start_daemon": "false", "ssh_tunnel": {"enabled": "off"}}})
        app = self.new_app()
        screen = self.open_screen(app)
        self.assertFalse(screen.chk_ml_auto_start.is_checked())
        self.assertFalse(screen.chk_ssh_enabled.is_checked())

    def test_invalid_port_keeps_screen_open_and_reports(self) -> None:
        app = self.new_app()
        screen = self.open_screen(app)
        self.type_text(app, screen.inp_ssh_port, "")
        self.focus(app, screen.btn_save)
        self.press(app, KEY_ENTER)
        self.assertIs(app.current_screen(), screen)
        self.assertEqual(len(self.modals.shown), 1)
        self.assertIn("SSH Port", self.modals.shown[0][1])
        self.assertFalse(app.is_dirty)
        self.assertEqual(app.config["mailing_list"]["ssh_tunnel"]["port"], 22)


class TestDatabaseSSHReentry(_ReentryCase):
    """DatabaseSSHScreen: every widget keeps its saved value across re-entry and reload.

    The SSH tunnel stays disabled, or the database port equals the tunnel's local port, so no
    confirmation dialog is involved. No test depends on a "Remote Host" widget.
    """

    menu_key = MENU_DATABASE
    screen_cls = DatabaseSSHScreen

    DB_FIELDS = {
        "engine": "sqlite",
        "host": "db.internal",
        "port": "3310",
        "user": "kinfo",
        "password": "s3cret!",
        "database": "kernelinfo",
        "unix_socket": "",
    }

    SSH_FIELDS_DISABLED = {
        "ssh_enabled": False,
        "ssh_host": "bastion.example.net",
        "ssh_port": "2201",
        "ssh_user": "tunnel",
        "ssh_key_file": "~/.ssh/id_db",
        "ssh_password": "pw",
        "ssh_remote_port": "3307",
        "ssh_local_port": "3399",
    }

    def test_all_fields_round_trip_tunnel_disabled(self) -> None:
        values = dict(self.DB_FIELDS)
        values.update(self.SSH_FIELDS_DISABLED)
        self.round_trip(values)
        db = json.loads(self.config_path.read_text(encoding="utf-8"))["database"]
        self.assertEqual(db["port"], 3310)
        self.assertEqual(db["unix_socket"], "")
        self.assertEqual(db["engine"], "sqlite")
        self.assertIs(db["ssh_tunnel"]["enabled"], False)
        self.assertEqual(db["ssh_tunnel"]["local_port"], 3399)
        # Fields without a widget survive the save.
        self.assertEqual(db["server_tuning"]["innodb_log_file_size"], "2G")
        self.assertEqual(db["ssh_tunnel"]["connect_timeout"], 10)

    def test_tunnel_enabled_with_matching_ports_round_trip(self) -> None:
        values = {
            "engine": "mariadb",
            "host": "127.0.0.1",
            "port": "3308",
            "ssh_enabled": True,
            "ssh_host": "bastion.example.net",
            "ssh_user": "tunnel",
            "ssh_local_port": "3308",
        }
        self.round_trip(values)
        db = json.loads(self.config_path.read_text(encoding="utf-8"))["database"]
        self.assertIs(db["ssh_tunnel"]["enabled"], True)
        self.assertEqual(db["port"], db["ssh_tunnel"]["local_port"])

    def test_falsy_values_round_trip_after_truthy(self) -> None:
        values = dict(self.DB_FIELDS)
        values.update(self.SSH_FIELDS_DISABLED)
        self.round_trip(values)
        self.round_trip({
            "engine": "mariadb",
            "user": "",
            "password": "",
            "database": "",
            "unix_socket": "auto",
            "ssh_host": "",
            "ssh_user": "",
            "ssh_key_file": "",
            "ssh_password": "",
            "ssh_local_port": "0",
        })

    def test_saving_twice_in_a_row_keeps_values(self) -> None:
        app = self.new_app()
        screen = self.open_screen(app)
        self.apply(app, screen, {"user": "first"})
        self.press_save_button(app, screen, screen.btn_save)
        screen = self.open_screen(app)
        self.apply(app, screen, {"database": "second"})
        self.press_save_button(app, screen, screen.btn_save)
        screen = self.open_screen(app)
        self.assertEqual(screen.user.get_value(), "first")
        self.assertEqual(screen.database.get_value(), "second")


if __name__ == "__main__":
    unittest.main()
