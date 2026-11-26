"""core/tui/screens/scoped_config.py - Scoped configuration menu for standalone daemon/webapp/api configs."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Any

from core.config import get_service_config_path
from core.tui.screens.base import BaseScreen
from core.tui.screens.api_cfg import ApiSettingsScreen
from core.tui.screens.cve_ml import CVEMailingListScreen
from core.tui.screens.database_ssh import DatabaseSSHScreen
from core.tui.screens.diagnostics import DiagnosticsScreen
from core.tui.screens.logging_cfg import LoggingScreen
from core.tui.screens.parser_dist import ParserDistributedScreen
from core.tui.screens.repos import RepositoriesScreen
from core.tui.screens.webapp_cfg import WebappSettingsScreen
from core.tui.theme import (
    PAIR_ACTIVE,
    PAIR_BORDER,
    PAIR_ERROR,
    PAIR_HEADER,
    PAIR_HIGHLIGHT,
    PAIR_MUTED,
    PAIR_SUCCESS,
    get_color,
)
from core.tui.widgets import ModalDialog, StatusBar


class ScopedConfigMenuScreen(BaseScreen):
    """Scoped configuration menu editing `config_daemon.json`, `config_webapp.json` or `config_api.json`."""

    DAEMON_MENU_ITEMS = [
        ("1", "Repositories, Git & Branch Filters", "Detect repos, configure branches, start_ref, tag rules"),
        ("2", "Database, SSH Tunnel & Auth", "Database credentials, socket, and SSH port forwarding"),
        ("3", "Parser Engine & Distributed Node ID", "Memory limit, hugepages, worker_id, daemon/master/worker role"),
        ("4", "CVE Catalog & Mailing List", "CVE list clone directory, lore storage & ML daemon tunnel"),
        ("L", "Parser Logging Configuration", "Parser log files, directories, error split & log level"),
        ("5", "Run Diagnostics", "Verify database, SSH, git, CVE, and mailing list configurations"),
        ("S", "Save Configuration", "Save changes strictly to config_daemon.json"),
        ("B", "Back to Systemd Services", "Return to Systemd Services screen"),
    ]

    WEBAPP_MENU_ITEMS = [
        ("1", "Webapp Server, Cache & Tools", "Host, port, reload, cache engine/limits, jobs, checkpatch & source index"),
        ("2", "Repositories, Git & Branch Filters", "Detect repos, configure branches, start_ref, tag rules"),
        ("3", "Database, SSH Tunnel & Auth", "Database credentials, socket, and SSH port forwarding"),
        ("4", "CVE Catalog & Mailing List", "CVE list clone directory, lore storage & ML daemon tunnel"),
        ("L", "Webapp Logging Configuration", "Webapp backend log files, request mode, error split & level"),
        ("5", "Run Diagnostics", "Verify database, SSH, git, CVE, and mailing list configurations"),
        ("S", "Save Configuration", "Save changes strictly to config_webapp.json (parser-only keys pruned)"),
        ("B", "Back to Systemd Services", "Return to Systemd Services screen"),
    ]

    API_MENU_ITEMS = [
        ("1", "API Server, Keys & Limits", "Host, port, reload, API keys, rate limit, auth lockout & trusted proxies"),
        ("2", "Repositories, Git & Branch Filters", "Detect repos, configure branches, start_ref, tag rules"),
        ("3", "Database, SSH Tunnel & Auth", "Database credentials, socket, and SSH port forwarding"),
        ("L", "API Logging Configuration", "API service log files, request mode, error split & level"),
        ("5", "Run Diagnostics", "Verify database, SSH and git configurations"),
        ("S", "Save Configuration", "Save changes strictly to config_api.json (other sections pruned)"),
        ("B", "Back to Systemd Services", "Return to Systemd Services screen"),
    ]

    def __init__(
        self,
        stdscr: Any,
        app: Any = None,
        service: str = "daemon",
        *,
        config_path: str | Path | None = None,
    ) -> None:
        super().__init__(stdscr, app)
        svc = str(service or "").strip().lower()
        if svc not in ("daemon", "webapp", "api"):
            raise ValueError(f"Unknown service {service!r}; expected 'daemon', 'webapp' or 'api'")
        self.service: str = svc
        self.target_path: Path = (
            Path(config_path).expanduser().resolve()
            if config_path is not None
            else get_service_config_path(self.service).resolve()
        )
        self._context_pushed: bool = False

        self.menu_items = list(
            {"daemon": self.DAEMON_MENU_ITEMS, "webapp": self.WEBAPP_MENU_ITEMS, "api": self.API_MENU_ITEMS}[self.service]
        )
        self.hotkeys: dict[str, str] = {item[0].lower(): item[0] for item in self.menu_items}
        self.hotkeys["q"] = "B"

        self.selected_index: int = 0
        self.status_bar = StatusBar([
            ("↑/↓", "Navigate"),
            ("Enter", "Select"),
            ("S", "Save"),
            ("B/Q/Esc", "Back"),
        ])

    def on_enter(self) -> None:
        """Push the scoped config context once on first entry; preserve edits on sub-screen return."""
        if self._context_pushed or self.app is None:
            return
        push_ctx = getattr(self.app, "push_config_context", None)
        if not callable(push_ctx):
            return
        try:
            push_ctx(self.target_path, scope=self.service, seed_service=self.service)
            self._context_pushed = True
        except Exception as exc:
            ModalDialog(
                self.stdscr,
                "Configuration Error",
                f"Failed to prepare {self.target_path}:\n{exc}",
                ["OK"],
            ).show()
            if hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
                self.app.pop_screen()

    def draw(self) -> None:
        """Render the scoped configuration menu."""
        self.clear()
        max_y, max_x = self.get_maxyx()
        if max_y < 12 or max_x < 50:
            self.safe_addstr(0, 0, "Terminal window too small (min 50x12).", get_color(PAIR_ERROR))
            self.refresh()
            return

        svc_title = {
            "daemon": "Daemon Service Configuration",
            "webapp": "Webapp Service Configuration",
            "api": "API Service Configuration",
        }[self.service]
        self.draw_box(0, 0, 5, max_x, title=f" {svc_title} ", color_pair=PAIR_HEADER)

        cfg_path = getattr(self.app, "config_path", self.target_path)
        is_dirty = getattr(self.app, "is_dirty", False)
        status_str = "[MODIFIED / UNSAVED]" if is_dirty else "[SAVED]"
        status_color = get_color(PAIR_ERROR if is_dirty else PAIR_SUCCESS)

        editing_text = f"Editing: {cfg_path}"
        max_edit_len = max(10, max_x - len(status_str) - 6)
        if len(editing_text) > max_edit_len:
            editing_text = editing_text[: max_edit_len - 3] + "..."
        self.safe_addstr(1, 2, editing_text, get_color(PAIR_MUTED))
        self.safe_addstr(1, max(2, max_x - len(status_str) - 3), status_str, status_color)
        self.safe_addstr(
            2,
            2,
            "Use Arrow keys and [Enter] to navigate, or press hotkeys [1-5, L, S, B/Q].",
            curses.A_NORMAL,
        )
        scope_note = {
            "daemon": "Scope: Daemon parser service (writes strictly to config_daemon.json).",
            "webapp": "Scope: Webapp backend service (parser-only sections hidden & pruned on save).",
            "api": "Scope: AI API service (writes strictly to config_api.json; other sections pruned on save).",
        }[self.service]
        self.safe_addstr(3, 2, scope_note[: max(1, max_x - 4)], get_color(PAIR_MUTED))

        menu_y = 5
        menu_h = max_y - menu_y - 2
        self.draw_box(menu_y, 0, menu_h, max_x, title=" Configuration Options ", color_pair=PAIR_BORDER)

        rows = max(1, menu_h - 2)
        step = 2 if len(self.menu_items) * 2 <= rows else 1
        per_page = max(1, rows // step)
        first = min(max(0, self.selected_index - per_page + 1), max(0, len(self.menu_items) - per_page))
        if first:
            self.safe_addstr(menu_y, max_x - 6, " ↑ ", get_color(PAIR_MUTED))
        if first + per_page < len(self.menu_items):
            self.safe_addstr(menu_y + menu_h - 1, max_x - 6, " ↓ ", get_color(PAIR_MUTED))

        for i, (key, title_text, desc) in enumerate(self.menu_items):
            if i < first:
                continue
            item_y = menu_y + 1 + ((i - first) * step)
            if item_y >= menu_y + menu_h - 1:
                break

            is_selected = i == self.selected_index
            prefix = " > " if is_selected else "   "
            attr_key = get_color(PAIR_ACTIVE) | curses.A_BOLD if is_selected else get_color(PAIR_HIGHLIGHT)
            attr_title = get_color(PAIR_ACTIVE) | curses.A_BOLD if is_selected else curses.A_BOLD
            attr_desc = get_color(PAIR_ACTIVE) if is_selected else get_color(PAIR_MUTED)

            line_prefix = f"{prefix}[{key}] "
            self.safe_addstr(item_y, 2, line_prefix, attr_key)
            x_offset = 2 + len(line_prefix)
            self.safe_addstr(item_y, x_offset, title_text, attr_title)

            desc_text = f"  - {desc}"
            if max_x > len(line_prefix) + len(title_text) + len(desc_text) + 4:
                self.safe_addstr(item_y, x_offset + len(title_text), desc_text, attr_desc)
            elif step == 2 and item_y + 1 < menu_y + menu_h - 1:
                self.safe_addstr(item_y + 1, x_offset, desc, attr_desc)

        self.status_bar.y = max_y - 1
        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Handle navigation and hotkeys in the scoped menu."""
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            self.selected_index = (self.selected_index - 1) % len(self.menu_items)
            return True
        if ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            self.selected_index = (self.selected_index + 1) % len(self.menu_items)
            return True
        if ch in (curses.KEY_ENTER, 10, 13, 32):
            return self._activate_item(self.selected_index)
        if ch == 27:
            return self._activate_key("B")
        if 0 <= ch < 256 and chr(ch).lower() in self.hotkeys:
            return self._activate_key(self.hotkeys[chr(ch).lower()])
        return False

    def _activate_item(self, index: int) -> bool:
        if not 0 <= index < len(self.menu_items):
            return False
        return self._activate_key(self.menu_items[index][0])

    def _activate_key(self, key: str) -> bool:
        self.selected_index = next(
            (i for i, item in enumerate(self.menu_items) if item[0] == key),
            self.selected_index,
        )
        if self.app is None:
            return key in self.hotkeys.values()

        if self.service == "daemon":
            screens = {
                "1": RepositoriesScreen,
                "2": DatabaseSSHScreen,
                "3": ParserDistributedScreen,
                "4": CVEMailingListScreen,
                "L": LoggingScreen,
                "5": DiagnosticsScreen,
            }
        elif self.service == "api":
            screens = {
                "1": ApiSettingsScreen,
                "2": RepositoriesScreen,
                "3": DatabaseSSHScreen,
                "L": LoggingScreen,
                "5": DiagnosticsScreen,
            }
        else:
            screens = {
                "1": WebappSettingsScreen,
                "2": RepositoriesScreen,
                "3": DatabaseSSHScreen,
                "4": CVEMailingListScreen,
                "L": LoggingScreen,
                "5": DiagnosticsScreen,
            }

        if key in screens:
            self.app.push_screen(screens[key](self.stdscr, self.app))
            return True
        if key == "S":
            success, msg = self.app.save_config()
            title = "Configuration Saved" if success else "Save Failed"
            ModalDialog(self.stdscr, title, msg, ["OK"]).show()
            return True
        if key == "B":
            return self._go_back()
        return False

    def _go_back(self) -> bool:
        """Confirm discarding unsaved edits if dirty, restore parent config context, and pop."""
        if self.app is not None and getattr(self.app, "is_dirty", False):
            choice = ModalDialog(
                self.stdscr,
                "Unsaved Changes",
                f"You have unsaved changes in {self.target_path.name}.\nDo you want to discard them and return?",
                ["Discard & Return", "Cancel"],
                default_button=1,
            ).show()
            if choice != "Discard & Return":
                return True

        if self.app is not None:
            if self._context_pushed and hasattr(self.app, "restore_config_context"):
                self.app.restore_config_context()
                self._context_pushed = False
            if hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
                self.app.pop_screen()
        return True
