"""Base screen class for curses TUI screens."""

from abc import ABC, abstractmethod
import curses
from typing import Any, Tuple

from core.tui.theme import safe_addstr, draw_box
from core.tui.widgets import ModalDialog


class BaseScreen(ABC):
    """Abstract base class for all TUI screens.

    Attributes:
        stdscr: Curses standard screen or window handle.
        app: Application or controller reference.
        widgets: List of focusable / interactive widgets on this screen.
        focused_widget_index: Index of currently focused widget.
    """

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        self.stdscr = stdscr
        self.app = app
        self.widgets: list[Any] = []
        self.focused_widget_index: int = 0

    def on_enter(self) -> None:
        """Called when this screen becomes the active screen."""
        pass

    def on_leave(self) -> None:
        """Called when navigating away from this screen."""
        pass

    @abstractmethod
    def draw(self) -> None:
        """Render the screen layout and widgets to stdscr."""
        pass

    def handle_key(self, ch: int) -> bool:
        """Handle an incoming keypress.

        Args:
            ch: Keycode or character int from curses getch().

        Returns:
            bool: True if the key was consumed by this screen or its focused widget,
                  False if the parent or application controller should handle it.
        """
        widget = self.get_focused_widget()
        if widget is not None and hasattr(widget, "handle_key"):
            if widget.handle_key(ch):
                return True
        return False

    # --- Curses helper methods ---

    def get_maxyx(self) -> Tuple[int, int]:
        """Return (height, width) of the screen, safely defaulting to (24, 80)."""
        try:
            return self.stdscr.getmaxyx()
        except Exception:
            return (24, 80)

    def clear(self) -> None:
        """Clear the screen buffer."""
        try:
            self.stdscr.erase()
        except Exception:
            pass

    def refresh(self) -> None:
        """Refresh the curses screen."""
        try:
            self.stdscr.refresh()
        except Exception:
            pass

    def safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """Draw string on stdscr safely within boundaries."""
        safe_addstr(self.stdscr, y, x, text, attr)

    def show_busy(self, message: str) -> None:
        """Paint `message` over the bottom row and refresh, ahead of a blocking diagnostic.

        The next refresh repaints the whole terminal (`request_full_redraw()`), so output a
        diagnostic leaks onto the terminal is overwritten once it returns.
        """
        max_y, max_x = self.get_maxyx()
        width = max(1, max_x - 1)
        safe_addstr(self.stdscr, max_y - 1, 0, f" {message} ".ljust(width)[:width], curses.A_REVERSE)
        self.refresh()
        self.request_full_redraw()

    def request_full_redraw(self) -> None:
        """Make the next refresh repaint every terminal cell instead of only changed ones."""
        try:
            self.stdscr.clearok(True)
        except Exception:
            pass

    # --- Unsaved-edit tracking ---

    def form_snapshot(self) -> list[Any]:
        """Current values of the form widgets (text inputs, checkboxes, select lists)."""
        values: list[Any] = []
        for w in self.widgets:
            if hasattr(w, "get_value"):
                values.append(w.get_value())
            elif hasattr(w, "is_checked"):
                values.append(w.is_checked())
            elif hasattr(w, "options") and hasattr(w, "get_selected"):
                values.append(w.get_selected())
        return values

    def mark_form_clean(self) -> None:
        """Record the current form values as the saved baseline."""
        self._clean_snapshot = self.form_snapshot()

    def form_is_dirty(self) -> bool:
        """True when the form differs from the baseline recorded by `mark_form_clean()`."""
        baseline = getattr(self, "_clean_snapshot", None)
        return baseline is not None and self.form_snapshot() != baseline

    def confirm_discard(self) -> bool:
        """Return True when leaving is fine: no unsaved edits, or the user chose Discard."""
        if not self.form_is_dirty():
            return True
        choice = ModalDialog.show_dialog(
            self.stdscr,
            title="Unsaved Changes",
            message="You have unsaved changes. Discard changes and return?",
            buttons=["Discard", "Cancel"],
            default_button=1,
        )
        return choice == "Discard"

    # --- Per-session view state (survives leaving and re-opening a screen) ---

    def _view_state_store(self) -> dict[str, dict[str, Any]] | None:
        """Return the app's `view_state` dict (created on demand), or None without an app."""
        if self.app is None:
            return None
        store = getattr(self.app, "view_state", None)
        if not isinstance(store, dict):
            store = {}
            try:
                self.app.view_state = store
            except Exception:
                return None
        return store

    def remember_view(self, key: str, **state: Any) -> None:
        """Store UI state (selected rows, focused widget) under `key` for the next screen instance."""
        store = self._view_state_store()
        if store is not None:
            store[key] = dict(state)

    def recall_view(self, key: str) -> dict[str, Any]:
        """Return the UI state stored under `key` by `remember_view()`, or an empty dict."""
        store = self._view_state_store()
        if store is None:
            return {}
        return dict(store.get(key) or {})

    def draw_box(
        self,
        y: int,
        x: int,
        h: int,
        w: int,
        title: str | None = None,
        color_pair: int = 0,
        ascii_only: bool = False,
    ) -> None:
        """Draw a box on stdscr."""
        draw_box(self.stdscr, y, x, h, w, title=title, color_pair=color_pair, ascii_only=ascii_only)

    # --- Widget navigation helpers ---

    def get_focused_widget(self) -> Any | None:
        """Return currently focused widget or None."""
        if 0 <= self.focused_widget_index < len(self.widgets):
            return self.widgets[self.focused_widget_index]
        return None

    def focus_widget(self, index: int) -> None:
        """Set focus to the widget at the given index."""
        if not self.widgets:
            return
        index = max(0, min(index, len(self.widgets) - 1))
        for i, w in enumerate(self.widgets):
            if hasattr(w, "is_focused"):
                w.is_focused = (i == index)
        self.focused_widget_index = index

    def focus_next(self) -> None:
        """Advance focus to the next widget in self.widgets."""
        if not self.widgets:
            return
        next_idx = (self.focused_widget_index + 1) % len(self.widgets)
        self.focus_widget(next_idx)

    def focus_prev(self) -> None:
        """Move focus to the previous widget in self.widgets."""
        if not self.widgets:
            return
        prev_idx = (self.focused_widget_index - 1) % len(self.widgets)
        self.focus_widget(prev_idx)
