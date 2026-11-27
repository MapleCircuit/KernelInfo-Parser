"""tests/test_api_tui.py - Unit tests for ApiSettingsScreen, the api config scope and the main-menu server launcher."""

from __future__ import annotations

import curses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.config import API_CONFIG_FILENAME
from core.tui.app import TUIApp
from core.tui.screens.api_cfg import ApiSettingsScreen, mask_key
from core.tui.screens.main_menu import MainMenuScreen
from core.tui.widgets import ModalDialog
from tests.test_tui import MockCursesWindow


class _MockApp:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config if config is not None else {}
        self.is_dirty = False
        self.popped = False

    def pop_screen(self) -> None:
        self.popped = True


def _screen(config: dict | None = None, rows: int = 24) -> tuple[ApiSettingsScreen, _MockApp, MockCursesWindow]:
    app = _MockApp(config)
    win = MockCursesWindow(rows, 100)
    return ApiSettingsScreen(win, app), app, win


def _go(screen: ApiSettingsScreen, section: str) -> None:
    screen.sel_section.set_selected(screen.sel_section.options.index(section))
    screen._rebuild_widgets()


class TestApiSettingsScreen(unittest.TestCase):
    def test_defaults_when_section_missing(self) -> None:
        screen, _app, _win = _screen({})
        self.assertEqual(screen.inp_host.get_value(), "127.0.0.1")
        self.assertEqual(screen.inp_port.get_value(), "8001")
        self.assertFalse(screen.chk_reload.is_checked())
        self.assertEqual(screen.inp_proxies.get_value(), "127.0.0.1, ::1")
        self.assertEqual(screen.sel_client_ip_header.get_selected(), "X-Forwarded-For")
        self.assertEqual(screen.inp_rl_max.get_value(), "120")
        self.assertEqual(screen.inp_lo_max_lock.get_value(), "3600")
        self.assertEqual(screen.keys, [])

    def test_load_edit_and_save_all_fields(self) -> None:
        cfg = {
            "api": {
                "host": "0.0.0.0",
                "port": 9000,
                "reload": True,
                "default_version": "v6.1",
                "keys": ["abcd1234efgh"],
                "rate_limit": {"max_requests": 10, "window_seconds": 5},
                "auth_lockout": {
                    "max_failures": 2, "failure_window_seconds": 7,
                    "base_lockout_seconds": 11, "max_lockout_seconds": 99,
                },
                "trusted_proxies": ["10.0.0.0/8"],
                "client_ip_header": "CF-Connecting-IP",
            }
        }
        screen, app, _ = _screen(cfg)
        self.assertEqual(screen.inp_host.get_value(), "0.0.0.0")
        self.assertTrue(screen.chk_reload.is_checked())
        self.assertEqual(screen.sel_client_ip_header.get_selected(), "CF-Connecting-IP")
        self.assertEqual(screen.inp_lo_base.get_value(), "11")
        self.assertFalse(screen.form_is_dirty())

        screen.inp_host.set_value("127.0.0.1")
        screen.inp_port.set_value("8123")
        screen.chk_reload.set_checked(False)
        screen.inp_proxies.set_value("127.0.0.1, ::1 , 192.168.0.0/16")
        screen.sel_client_ip_header.set_selected(1)
        screen.inp_rl_max.set_value("50")
        screen.inp_rl_window.set_value("30")
        screen.inp_lo_max.set_value("3")
        screen.inp_lo_window.set_value("40")
        screen.inp_lo_base.set_value("20")
        screen.inp_lo_max_lock.set_value("200")
        self.assertTrue(screen.form_is_dirty())
        self.assertTrue(screen.save())

        self.assertTrue(app.is_dirty)
        self.assertTrue(app.popped)
        api = app.config["api"]
        self.assertEqual(api["host"], "127.0.0.1")
        self.assertEqual(api["port"], 8123)
        self.assertFalse(api["reload"])
        self.assertEqual(api["default_version"], "v6.1")
        self.assertEqual(api["trusted_proxies"], ["127.0.0.1", "::1", "192.168.0.0/16"])
        self.assertEqual(api["client_ip_header"], "X-Real-IP")
        self.assertEqual(api["rate_limit"], {"max_requests": 50, "window_seconds": 30})
        self.assertEqual(api["auth_lockout"], {
            "max_failures": 3, "failure_window_seconds": 40, "base_lockout_seconds": 20, "max_lockout_seconds": 200,
        })
        self.assertEqual(api["keys"], ["abcd1234efgh"])

    def test_validation_blocks_save(self) -> None:
        bad_values = [
            ("inp_port", "0"), ("inp_port", "70000"), ("inp_port", "abc"),
            ("inp_rl_max", "0"), ("inp_rl_window", "-1"), ("inp_lo_max", "x"),
            ("inp_proxies", "not-an-ip"), ("inp_host", "  "),
        ]
        for attr, value in bad_values:
            screen, app, _ = _screen({})
            getattr(screen, attr).set_value(value)
            with patch("core.tui.screens.api_cfg.ModalDialog.show", return_value="OK") as dlg:
                self.assertFalse(screen.save(), (attr, value))
            self.assertTrue(dlg.called)
            self.assertFalse(app.popped)
            self.assertNotIn("port", app.config["api"])

    def test_max_lockout_must_not_be_below_base(self) -> None:
        screen, app, _ = _screen({})
        screen.inp_lo_base.set_value("100")
        screen.inp_lo_max_lock.set_value("50")
        with patch("core.tui.screens.api_cfg.ModalDialog.show", return_value="OK"):
            self.assertFalse(screen.save())

    def test_keys_are_masked_and_never_rendered_in_full(self) -> None:
        secret = "supersecretkey123"
        screen, _app, win = _screen({"api": {"keys": [secret, "zzzz9999"]}})
        self.assertEqual(mask_key(secret), "supe…")
        self.assertEqual(screen._render_key(0), "key 1  supe…")
        _go(screen, "Keys")
        screen.draw()
        text = "\n".join(str(a[2]) for n, a, _ in win.calls if n == "addstr")
        self.assertIn("supe…", text)
        self.assertIn("•••• (8 chars)", text)
        self.assertNotIn("zzzz", text)
        self.assertNotIn(secret, text)
        self.assertNotIn("zzzz9999", text)

    def test_add_and_remove_key_list_form(self) -> None:
        screen, app, _ = _screen({"api": {"keys": ["firstkey1"]}})
        with patch.object(ApiSettingsScreen, "_prompt", return_value="secondkey2"):
            self.assertTrue(screen.add_key())
        self.assertEqual([k for _n, k in screen.keys], ["firstkey1", "secondkey2"])
        self.assertEqual(len(screen.lst_keys.items), 2)
        self.assertTrue(screen.form_is_dirty())

        # duplicate rejected
        with patch.object(ApiSettingsScreen, "_prompt", return_value="firstkey1"), \
                patch("core.tui.screens.api_cfg.ModalDialog.show", return_value="OK"):
            self.assertFalse(screen.add_key())
        # cancelled / empty prompt
        with patch.object(ApiSettingsScreen, "_prompt", return_value=None):
            self.assertFalse(screen.add_key())

        screen.lst_keys.selected_index = 0
        with patch.object(ModalDialog, "show_dialog", return_value="Remove"):
            self.assertTrue(screen.remove_key())
        self.assertEqual([k for _n, k in screen.keys], ["secondkey2"])

        self.assertTrue(screen.save())
        self.assertEqual(app.config["api"]["keys"], ["secondkey2"])

    def test_remove_key_cancel_keeps_key(self) -> None:
        screen, _app, _ = _screen({"api": {"keys": ["onlykey1"]}})
        with patch.object(ModalDialog, "show_dialog", return_value="Cancel"):
            self.assertFalse(screen.remove_key())
        self.assertEqual(len(screen.keys), 1)

    def test_dict_form_keeps_names_and_asks_for_name(self) -> None:
        screen, app, _ = _screen({"api": {"keys": {"alice": "alicekey1", "bob": "bobkey22"}}})
        self.assertTrue(screen.keys_as_dict)
        self.assertEqual(screen._render_key(0), "alice  •••• (9 chars)")
        with patch.object(ApiSettingsScreen, "_prompt", side_effect=["carolkey3", "carol"]):
            self.assertTrue(screen.add_key())
        screen.lst_keys.selected_index = 1
        with patch.object(ModalDialog, "show_dialog", return_value="Remove"):
            screen.remove_key()
        self.assertTrue(screen.save())
        self.assertEqual(app.config["api"]["keys"], {"alice": "alicekey1", "carol": "carolkey3"})

    def test_dict_form_derives_name_when_blank(self) -> None:
        screen, app, _ = _screen({"api": {"keys": {"key1": "k1k1k1k1"}}})
        with patch.object(ApiSettingsScreen, "_prompt", side_effect=["newkey999", ""]):
            self.assertTrue(screen.add_key())
        self.assertTrue(screen.save())
        self.assertEqual(app.config["api"]["keys"], {"key1": "k1k1k1k1", "key2": "newkey999"})

    def test_section_switch_and_navigation(self) -> None:
        screen, _app, _ = _screen({"api": {"keys": ["a1b2c3d4", "e5f6g7h8"]}})
        self.assertIs(screen.widgets[0], screen.sel_section)
        screen.focus_widget(0)
        self.assertTrue(screen.handle_key(curses.KEY_RIGHT))
        self.assertEqual(screen.active_section(), "Keys")
        self.assertIn(screen.lst_keys, screen.widgets)
        screen.focus_widget(screen.widgets.index(screen.lst_keys))
        screen.handle_key(curses.KEY_DOWN)
        self.assertEqual(screen.lst_keys.selected_index, 1)
        self.assertIs(screen.get_focused_widget(), screen.lst_keys)

    def test_layout_fits_24_and_30_rows(self) -> None:
        for rows in (24, 30):
            screen, _app, win = _screen({"api": {"keys": ["k%07d" % i for i in range(12)]}}, rows=rows)
            for section in ("Server", "Keys", "Limits"):
                _go(screen, section)
                win.calls.clear()
                screen.draw()
                ys = [a[0] for n, a, _ in win.calls if n == "addstr"]
                self.assertLess(max(ys), rows)
                for w in screen.widgets:
                    self.assertLess(w.y + getattr(w, "height", 1), rows - 1, (rows, section, w))

    def test_cancel_confirms_when_dirty(self) -> None:
        screen, app, _ = _screen({})
        screen.inp_port.set_value("1234")
        with patch.object(screen, "confirm_discard", return_value=False):
            screen.cancel()
        self.assertFalse(app.popped)
        screen.mark_form_clean()
        screen.cancel()
        self.assertTrue(app.popped)

    def test_allow_writes_canvas_dir_and_scoped_keys(self) -> None:
        cfg = {
            "api": {
                "allow_writes": True,
                "nodemap_canvas_dir": "/tmp/custom-canvases",
                "keys": {
                    "reader": "readsecret1",
                    "writer": {"key": "writesecret2", "scopes": ["read", "nodemap:write"]},
                },
            }
        }
        screen, app, _ = _screen(cfg)
        self.assertTrue(screen.chk_allow_writes.is_checked())
        self.assertEqual(screen.inp_canvas_dir.get_value(), "/tmp/custom-canvases")
        self.assertIn("[read, nodemap:write]", screen._render_key(1))

        # Edit scopes on reader key
        screen.lst_keys.selected_index = 0
        with patch.object(ApiSettingsScreen, "_prompt", return_value="read, nodemap:write"):
            self.assertTrue(screen.edit_key_scopes())
        self.assertTrue(screen.save())
        saved_keys = app.config["api"]["keys"]
        self.assertEqual(
            saved_keys["reader"],
            {"key": "readsecret1", "scopes": ["read", "nodemap:write"]},
        )
        self.assertTrue(app.config["api"]["allow_writes"])
        self.assertEqual(app.config["api"]["nodemap_canvas_dir"], "/tmp/custom-canvases")


class TestApiConfigScope(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base = self.root / "config.json"
        self.base.write_text(
            json.dumps({
                "database": {"host": "db", "port": 3306, "user": "kip", "database": "linux"},
                "parser": {"mem_max": 64},
                "webapp": {"port": 8000},
                "cve": {"repo_dir": "cvelistV5"},
                "logging": {"parser": {"file": "p.log"}, "webapp": {"file": "w.log"}},
            }),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_push_api_scope_seeds_and_prunes_on_save(self) -> None:
        app = TUIApp(self.base)
        api_path = self.root / API_CONFIG_FILENAME
        app.push_config_context(api_path, scope="api", seed_service="api")
        self.assertEqual(app.config_scope, "api")
        self.assertTrue(api_path.is_file())

        app.config["api"]["port"] = 9191
        app.config["api"]["keys"] = ["abcd1234"]
        app.config["parser"] = {"mem_max": 1}
        app.config["webapp"] = {"port": 1}
        ok, _msg = app.save_config()
        self.assertTrue(ok)
        saved = json.loads(api_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["api"]["port"], 9191)
        self.assertEqual(saved["api"]["keys"], ["abcd1234"])
        self.assertEqual(saved["database"]["host"], "db")
        for dropped in ("parser", "webapp", "cve"):
            self.assertNotIn(dropped, saved)
        self.assertNotIn("parser", saved.get("logging", {}))
        self.assertNotIn("webapp", saved.get("logging", {}))
        self.assertTrue(app.restore_config_context())
        self.assertEqual(app.config_scope, "full")

    def test_invalid_scope_rejected(self) -> None:
        app = TUIApp(self.base)
        with self.assertRaises(ValueError):
            app.push_config_context(self.root / "x.json", scope="bogus")


class TestMainMenuServerLaunch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp.name) / "config.json"
        self.app = TUIApp(self.config_path)
        self.win = MagicMock()
        self.win.getmaxyx.return_value = (30, 100)
        self.menu = MainMenuScreen(self.win, self.app)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_menu_entries(self) -> None:
        items = {item[0]: item[1] for item in MainMenuScreen.MENU_ITEMS}
        self.assertEqual(items["0"], "Systemd Services (Daemon, Webapp & API)")
        self.assertEqual(items["W"], "Launch Webapp / API Servers")
        self.assertEqual(MainMenuScreen.HOTKEYS["w"], "W")
        self.assertEqual(MainMenuScreen.MENU_ITEMS[-1][0], "Q")

    def test_chooser_produces_server_launch_requests(self) -> None:
        for label, servers in (
            ("Webapp only", ["webapp"]),
            ("API only", ["api"]),
            ("Both Webapp & API", ["webapp", "api"]),
        ):
            app = TUIApp(self.config_path)
            menu = MainMenuScreen(self.win, app)
            with patch.object(ModalDialog, "show_dialog", return_value=label):
                self.assertTrue(menu.handle_key(ord("W")))
            self.assertTrue(app.should_run_pipeline, label)
            self.assertFalse(app.is_running)
            self.assertEqual(
                app.launch_args,
                {"servers": servers, "config_path": str(app.config_path)},
                label,
            )

    def test_cancel_chooser_does_not_launch(self) -> None:
        with patch.object(ModalDialog, "show_dialog", return_value="Cancel"):
            self.assertTrue(self.menu.handle_key(ord("w")))
        self.assertFalse(self.app.should_run_pipeline)
        self.assertEqual(self.app.launch_args, {})

    def test_dirty_config_uses_unsaved_changes_flow(self) -> None:
        self.app.is_dirty = True
        # Cancel at the unsaved-changes dialog: no launch
        with patch.object(ModalDialog, "show_dialog", side_effect=["API only", "Cancel"]):
            self.menu.handle_key(ord("W"))
        self.assertFalse(self.app.should_run_pipeline)
        # Save & Launch writes the config, then launches
        with patch.object(ModalDialog, "show_dialog", side_effect=["API only", "Save & Launch"]):
            self.menu.handle_key(ord("W"))
        self.assertTrue(self.app.should_run_pipeline)
        self.assertTrue(self.config_path.is_file())
        self.assertEqual(self.app.launch_args["servers"], ["api"])
        # Use saved config.json: launch without saving
        app = TUIApp(self.config_path)
        app.is_dirty = True
        menu = MainMenuScreen(self.win, app)
        with patch.object(ModalDialog, "show_dialog", side_effect=["Webapp only", "Use saved config.json"]):
            menu.handle_key(ord("W"))
        self.assertTrue(app.should_run_pipeline)
        self.assertTrue(app.is_dirty)

    def test_existing_pipeline_launch_unchanged(self) -> None:
        self.menu.handle_key(ord("R"))
        self.assertTrue(self.app.should_run_pipeline)
        self.assertEqual(self.app.launch_args, {})


if __name__ == "__main__":
    unittest.main()
