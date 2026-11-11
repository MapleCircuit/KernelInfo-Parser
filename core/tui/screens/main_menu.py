"""Main navigation menu screen for the KernelInfo-Parser TUI."""

import curses
from typing import Any

from core.tui.screens.base import BaseScreen
from core.tui.screens.cve_ml import CVEMailingListScreen
from core.tui.screens.database_ssh import DatabaseSSHScreen
from core.tui.screens.db_content import DBContentScreen
from core.tui.screens.diagnostics import DiagnosticsScreen
from core.tui.screens.git_select import GitRepoSelectScreen
from core.tui.screens.launch import RUN_SAVED, launch_pipeline
from core.tui.screens.parser_dist import ParserDistributedScreen
from core.tui.screens.repos import RepositoriesScreen
from core.tui.screens.table_reset import run_table_reset
from core.tui.screens.version_parse import VersionParseScreen
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


class MainMenuScreen(BaseScreen):
    """Main navigation menu screen for configuring KernelInfo-Parser."""

    MENU_ITEMS = [
        ("1", "Repositories, Git & Branch Filters", "Detect repos, configure branches, start_ref, tag rules"),
        ("2", "Database, SSH Tunnel & Auth", "Database credentials, socket, and SSH port forwarding"),
        ("3", "Parser Engine & Distributed Node ID", "Memory limit, hugepages, worker_id, master/worker role"),
        ("4", "CVE Catalog & Mailing List", "CVE list clone directory, lore storage & ML daemon tunnel"),
        ("5", "Run Diagnostics (Test All)", "Verify all database, SSH, git, CVE, and daemon configurations"),
        ("6", "Git Repository Manager", "Linux, CVE & other repos: select, clone, add, remotes, fetch all"),
        ("7", "Parse Specific Versions / Tags", "Pick one or more release tags with the tag selector and parse them"),
        ("8", "Table Reset", "Drop & recreate every database table (type RESET to confirm)"),
        ("9", "DB Content Viewer", "Parsed versions, repos & branches, ML/CVE data, table sizes, clean partial runs"),
        ("S", "Save Configuration", "Saves to Active Config File"),
        ("R", "Save & Run Pipeline", "Save settings and proceed directly to running the parser pipeline"),
        ("P", "Run Pipeline (saved config.json)", "Run the parser with config.json as saved on disk, without saving"),
        ("Q", "Exit", "Exit the configuration utility"),
    ]
    # Hotkey (lower-case) -> MENU_ITEMS key
    HOTKEYS = {item[0].lower(): item[0] for item in MENU_ITEMS}

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)
        self.selected_index: int = 0
        self.status_bar = StatusBar([
            ("↑/↓", "Navigate"),
            ("Enter", "Select"),
            ("S", "Save"),
            ("R", "Save & Run"),
            ("P", "Run Saved"),
            ("Q", "Exit"),
        ])

    def on_enter(self) -> None:
        """Called when returning or entering main menu."""
        pass

    def draw(self) -> None:
        """Render the main menu layout."""
        self.clear()
        max_y, max_x = self.get_maxyx()
        if max_y < 12 or max_x < 50:
            self.safe_addstr(0, 0, "Terminal window too small (min 50x12).", get_color(PAIR_ERROR))
            self.refresh()
            return

        # Header Box
        title = " KernelInfo-Parser Configuration Utility "
        self.draw_box(0, 0, 5, max_x, title=title, color_pair=PAIR_HEADER)

        cfg_path = getattr(self.app, "config_path", "config.json")
        is_dirty = getattr(self.app, "is_dirty", False)

        status_str = "[MODIFIED / UNSAVED]" if is_dirty else "[SAVED]"
        status_color = get_color(PAIR_ERROR if is_dirty else PAIR_SUCCESS)

        self.safe_addstr(1, 2, f"Active File: {cfg_path}", get_color(PAIR_MUTED))
        self.safe_addstr(1, max_x - len(status_str) - 3, status_str, status_color)
        self.safe_addstr(
            2,
            2,
            "Use Arrow keys and [Enter] to navigate, or press hotkeys [1-9, S, R, P, Q].",
            curses.A_NORMAL,
        )

        # Menu List Box
        menu_y = 5
        menu_h = max_y - menu_y - 2
        self.draw_box(menu_y, 0, menu_h, max_x, title=" Menu Options ", color_pair=PAIR_BORDER)

        # Two rows per item when they fit, otherwise one; scroll so the selection stays visible
        rows = max(1, menu_h - 2)
        step = 2 if len(self.MENU_ITEMS) * 2 <= rows else 1
        per_page = max(1, rows // step)
        first = min(max(0, self.selected_index - per_page + 1), max(0, len(self.MENU_ITEMS) - per_page))
        if first:
            self.safe_addstr(menu_y, max_x - 6, " ↑ ", get_color(PAIR_MUTED))
        if first + per_page < len(self.MENU_ITEMS):
            self.safe_addstr(menu_y + menu_h - 1, max_x - 6, " ↓ ", get_color(PAIR_MUTED))

        for i, (key, title_text, desc) in enumerate(self.MENU_ITEMS):
            if i < first:
                continue
            item_y = menu_y + 1 + ((i - first) * step)
            if item_y >= menu_y + menu_h - 1:
                break

            is_selected = (i == self.selected_index)
            prefix = " > " if is_selected else "   "

            attr_key = get_color(PAIR_ACTIVE) | curses.A_BOLD if is_selected else get_color(PAIR_HIGHLIGHT)
            attr_title = (
                get_color(PAIR_ACTIVE) | curses.A_BOLD if is_selected else curses.A_BOLD
            )
            attr_desc = get_color(PAIR_ACTIVE) if is_selected else get_color(PAIR_MUTED)

            line_prefix = f"{prefix}[{key}] "
            self.safe_addstr(item_y, 2, line_prefix, attr_key)

            x_offset = 2 + len(line_prefix)
            self.safe_addstr(item_y, x_offset, title_text, attr_title)

            # Draw description on the line right below or inline
            desc_text = f"  - {desc}"
            if max_x > len(line_prefix) + len(title_text) + len(desc_text) + 4:
                self.safe_addstr(item_y, x_offset + len(title_text), desc_text, attr_desc)
            elif step == 2 and item_y + 1 < menu_y + menu_h - 1:
                self.safe_addstr(item_y + 1, x_offset, desc, attr_desc)

        # Render Status Bar
        self.status_bar.y = max_y - 1
        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Handle key input for main menu navigation."""
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            self.selected_index = (self.selected_index - 1) % len(self.MENU_ITEMS)
            return True
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            self.selected_index = (self.selected_index + 1) % len(self.MENU_ITEMS)
            return True
        elif ch in (curses.KEY_ENTER, 10, 13, 32):  # Enter or Space
            return self._activate_item(self.selected_index)
        elif ch == 27:  # Esc
            return self._activate_key("Q")
        elif 0 <= ch < 256 and chr(ch).lower() in self.HOTKEYS:
            return self._activate_key(self.HOTKEYS[chr(ch).lower()])

        return False

    def _activate_item(self, index: int) -> bool:
        """Execute the menu option at `index` of MENU_ITEMS."""
        if not 0 <= index < len(self.MENU_ITEMS):
            return False
        return self._activate_key(self.MENU_ITEMS[index][0])

    def _activate_key(self, key: str) -> bool:
        """Execute the menu option whose hotkey is `key`."""
        self.selected_index = next(
            (i for i, item in enumerate(self.MENU_ITEMS) if item[0] == key), self.selected_index
        )
        if self.app is None:
            return key in self.HOTKEYS.values()
        screens = {
            "1": RepositoriesScreen,
            "2": DatabaseSSHScreen,
            "3": ParserDistributedScreen,
            "4": CVEMailingListScreen,
            "5": DiagnosticsScreen,
            "6": GitRepoSelectScreen,
            "7": VersionParseScreen,
            "9": DBContentScreen,
        }
        if key in screens:
            self.app.push_screen(screens[key](self.stdscr, self.app))
            return True
        if key == "8":  # Table Reset
            run_table_reset(self.stdscr, self.app)
            return True
        if key == "S":  # Save
            success, msg = self.app.save_config()
            title = "Configuration Saved" if success else "Save Failed"
            ModalDialog(self.stdscr, title, msg, ["OK"]).show()
            return True
        if key == "R":  # Save & Run Pipeline
            ok, msg = self.app.request_launch(save=True)
            if not ok:
                ModalDialog(self.stdscr, "Save Failed", msg, ["OK"]).show()
            return True
        if key == "P":  # Run Pipeline with the saved config.json
            return self._run_saved_config()
        if key == "Q":  # Exit
            if self.app.is_dirty:
                dlg = ModalDialog(
                    self.stdscr,
                    "Unsaved Changes",
                    "You have unsaved changes in your configuration.\nDo you want to discard them and exit?",
                    ["Discard & Exit", "Cancel"],
                )
                if dlg.show() == "Discard & Exit":
                    self.app.exit()
            else:
                self.app.exit()
            return True
        return False

    def _run_saved_config(self) -> bool:
        """Run the pipeline with config.json as saved on disk; unsaved edits are not written."""
        if self.app.is_dirty:
            choice = ModalDialog.show_dialog(
                self.stdscr,
                "Run Without Saving",
                (
                    f"The pipeline runs with {self.app.config_path} as saved on disk.\n"
                    "Your unsaved edits are NOT written and are lost when the TUI exits."
                ),
                ["Run (discard edits)", "Cancel"],
                default_button=1,
            )
            if choice != "Run (discard edits)":
                return True
        launch_pipeline(self.stdscr, self.app, RUN_SAVED)
        return True
