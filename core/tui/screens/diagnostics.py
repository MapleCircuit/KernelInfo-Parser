"""core/tui/screens/diagnostics.py - Unified System Diagnostics Dashboard screen."""

from __future__ import annotations

import curses
from typing import Any, Optional

from core.tui.screens.base import BaseScreen
from core.tui.testing import DiagnosticResult, run_all_diagnostics
from core.tui.theme import (
    PAIR_ACTIVE,
    PAIR_BORDER,
    PAIR_DEFAULT,
    PAIR_ERROR,
    PAIR_HEADER,
    PAIR_HIGHLIGHT,
    PAIR_MUTED,
    PAIR_SUCCESS,
    get_color,
    safe_addstr,
    draw_box,
)
from core.tui.widgets import (
    ModalDialog,
    ScrollableList,
    StatusBar,
)


class DiagnosticsScreen(BaseScreen):
    """Unified system diagnostics dashboard displaying tests, latency, and full multiline output.

    Attributes:
        app: Controller reference exposing .config and .pop_screen().
        results: List of DiagnosticResult objects collected from diagnostic runners.
    """

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)
        self.results: list[DiagnosticResult] = []

        self.list_widget = ScrollableList(
            y=3,
            x=2,
            height=18,
            width=76,
            title="Diagnostic Results",
            render_item_func=self._render_item_str,
        )

        self.status_bar = StatusBar(
            shortcuts=[
                ("Enter/Space", "View Details"),
                ("R", "Re-run Diagnostics"),
                ("Up/Down", "Navigate"),
                ("Esc", "Back"),
            ]
        )

        self.widgets = [self.list_widget]
        self.focus_widget(0)

    def _render_item_str(self, res: DiagnosticResult) -> str:
        """Format a single DiagnosticResult into a summary row string."""
        tag = "[ PASS ]" if res.passed else "[ FAIL ]"
        dur = f"({res.latency_ms:.0f}ms)".rjust(8)
        return f"{tag} {dur}  {res.name:<24} - {res.message}"

    def on_enter(self) -> None:
        """Automatically execute all diagnostics when entering this dashboard screen."""
        self.refresh_diagnostics()

    def refresh_diagnostics(self) -> list[DiagnosticResult]:
        """Execute all diagnostics against current app.config."""
        config_data = {}
        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            config_data = self.app.config

        self.results = run_all_diagnostics(config_data)
        self.list_widget.set_items(self.results)
        return self.results

    def get_selected_result(self) -> Optional[DiagnosticResult]:
        """Return currently selected DiagnosticResult or None."""
        if self.results and 0 <= self.list_widget.selected_index < len(self.results):
            return self.results[self.list_widget.selected_index]
        return None

    def show_details(self) -> Optional[str]:
        """Display centered ModalDialog with full diagnostic metadata and details."""
        item = self.get_selected_result()
        if item is None:
            return None

        status = "PASS" if item.passed else "FAIL"
        title = f"{item.name} [{status}]"

        lines = [
            f"Test Name:    {item.name}",
            f"Outcome:      {status}",
            f"Duration:     {item.latency_ms:.2f} ms",
            f"Summary:      {item.message}",
        ]
        if item.details:
            lines.append("")
            lines.append("Full Diagnostics Details:")
            lines.append(item.details)

        msg = "\n".join(lines)
        dialog = ModalDialog(self.stdscr, title=title, message=msg, buttons=["OK"])
        return dialog.show()

    def draw(self) -> None:
        """Render diagnostics dashboard with colored status indicators and latency."""
        self.clear()
        max_y, max_x = self.get_maxyx()

        # Top Header
        title = "System Diagnostics Dashboard"
        safe_addstr(self.stdscr, 1, 2, title, get_color(PAIR_HEADER) | curses.A_BOLD)

        # Summary Counters
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        failed = total - passed
        summary = f"Total: {total} | Passed: {passed} | Failed: {failed}"
        sum_x = max(34, max_x - len(summary) - 3)
        safe_addstr(self.stdscr, 1, sum_x, summary, get_color(PAIR_MUTED))

        # Box dimensions
        box_y = 3
        box_x = 2
        box_h = max(6, max_y - 5)
        box_w = max(40, max_x - 4)

        # Synchronize list widget dimensions
        self.list_widget.y = box_y
        self.list_widget.x = box_x
        self.list_widget.height = box_h
        self.list_widget.width = box_w

        # Draw outer box border
        draw_box(self.stdscr, box_y, box_x, box_h, box_w, title="Diagnostic Results", color_pair=PAIR_BORDER)

        inner_h = box_h - 2
        inner_w = box_w - 2

        # Scroll arrows
        if self.list_widget.scroll_offset > 0:
            safe_addstr(self.stdscr, box_y, box_x + box_w - 2, "↑", get_color(PAIR_ACTIVE) | curses.A_BOLD)
        if self.list_widget.scroll_offset + inner_h < len(self.results):
            safe_addstr(self.stdscr, box_y + box_h - 1, box_x + box_w - 2, "↓", get_color(PAIR_ACTIVE) | curses.A_BOLD)

        if not self.results:
            msg = "No diagnostic tests have run yet. Press [R] to run diagnostics."
            safe_addstr(self.stdscr, box_y + 2, box_x + 4, msg, get_color(PAIR_MUTED))
        else:
            # Clamp scroll offset
            if self.list_widget.selected_index < self.list_widget.scroll_offset:
                self.list_widget.scroll_offset = self.list_widget.selected_index
            elif self.list_widget.selected_index >= self.list_widget.scroll_offset + inner_h:
                self.list_widget.scroll_offset = max(0, self.list_widget.selected_index - inner_h + 1)

            # Render each visible row with distinct attribute styling
            for r in range(inner_h):
                idx = self.list_widget.scroll_offset + r
                row_y = box_y + 1 + r
                row_x = box_x + 1

                if idx < len(self.results):
                    item = self.results[idx]
                    is_sel = (idx == self.list_widget.selected_index)

                    # Clear row background
                    safe_addstr(self.stdscr, row_y, row_x, " " * inner_w, get_color(PAIR_DEFAULT))

                    col = row_x
                    # 1. Selection indicator
                    pointer = "► " if is_sel else "  "
                    ptr_attr = (get_color(PAIR_HIGHLIGHT) | curses.A_BOLD) if is_sel else get_color(PAIR_DEFAULT)
                    safe_addstr(self.stdscr, row_y, col, pointer, ptr_attr)
                    col += len(pointer)

                    # 2. Status Badge: [ PASS ] in green/bold, [ FAIL ] in red/bold
                    if item.passed:
                        tag_str = "[ PASS ]"
                        tag_attr = get_color(PAIR_SUCCESS) | curses.A_BOLD
                    else:
                        tag_str = "[ FAIL ]"
                        tag_attr = get_color(PAIR_ERROR) | curses.A_BOLD

                    safe_addstr(self.stdscr, row_y, col, tag_str, tag_attr)
                    col += len(tag_str) + 1

                    # 3. Latency in ms (XXms)
                    dur_str = f"({item.latency_ms:.0f}ms)".rjust(8)
                    dur_attr = (get_color(PAIR_HIGHLIGHT) | curses.A_BOLD) if is_sel else get_color(PAIR_MUTED)
                    safe_addstr(self.stdscr, row_y, col, dur_str, dur_attr)
                    col += len(dur_str) + 1

                    # 4. Diagnostic test name
                    name_str = f" {item.name:<24} "
                    name_attr = (get_color(PAIR_HIGHLIGHT) | curses.A_BOLD) if is_sel else (get_color(PAIR_DEFAULT) | curses.A_BOLD)
                    safe_addstr(self.stdscr, row_y, col, name_str, name_attr)
                    col += len(name_str)

                    # 5. Concise message
                    avail_w = max(0, (box_x + box_w - 2) - col)
                    if avail_w > 0:
                        msg_str = ("- " + item.message)[:avail_w]
                        msg_attr = get_color(PAIR_HIGHLIGHT) if is_sel else get_color(PAIR_DEFAULT)
                        safe_addstr(self.stdscr, row_y, col, msg_str, msg_attr)
                else:
                    safe_addstr(self.stdscr, row_y, row_x, " " * inner_w, get_color(PAIR_DEFAULT))

        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Handle dashboard navigation, test refresh, and details popup."""
        # Scroll navigation
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            self.list_widget.select_prev()
            return True

        if ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            self.list_widget.select_next()
            return True

        if ch in (curses.KEY_PPAGE, getattr(curses, "KEY_PREVIOUS", -1)):
            self.list_widget.page_up()
            return True

        if ch in (curses.KEY_NPAGE, getattr(curses, "KEY_NEXT", -1)):
            self.list_widget.page_down()
            return True

        if ch == curses.KEY_HOME:
            self.list_widget.select_first()
            return True

        if ch == curses.KEY_END:
            self.list_widget.select_last()
            return True

        # Refresh diagnostics on 'R' / 'r'
        if ch in (ord("r"), ord("R")):
            self.refresh_diagnostics()
            return True

        # Open multiline details modal on Enter or Space
        if ch in (10, 13, curses.KEY_ENTER, ord(" ")):
            self.show_details()
            return True

        # Esc returns to main menu / pops screen
        if ch == 27:
            if self.app is not None and hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
                self.app.pop_screen()
            return True

        return False
