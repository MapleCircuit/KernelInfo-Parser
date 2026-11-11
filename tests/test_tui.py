"""Unit tests for core.tui curses framework and widgets."""

import curses
import unittest
from unittest.mock import MagicMock, patch

from core.tui.theme import (
    PAIR_DEFAULT,
    PAIR_HEADER,
    PAIR_ACTIVE,
    PAIR_ERROR,
    PAIR_SUCCESS,
    PAIR_MUTED,
    PAIR_HIGHLIGHT,
    PAIR_BORDER,
    init_theme,
    get_color,
    safe_addstr,
    draw_box,
)
from core.tui.screens.base import BaseScreen
from core.tui.widgets import (
    TextInput,
    Checkbox,
    SelectList,
    ScrollableList,
    StatusBar,
    ModalDialog,
)


class MockCursesWindow:
    """Mock curses window recording calls for deterministic testing."""

    def __init__(self, max_y: int = 24, max_x: int = 80) -> None:
        self.max_y = max_y
        self.max_x = max_x
        self.calls: list[tuple[str, tuple, dict]] = []
        self.cursor_pos = (0, 0)
        self.keys_queue: list[int] = []

    def getmaxyx(self) -> tuple[int, int]:
        return (self.max_y, self.max_x)

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if y < 0 or y >= self.max_y or x < 0 or x >= self.max_x:
            raise curses.error("addstr coordinates out of bounds")
        if y == self.max_y - 1 and x + len(text) >= self.max_x:
            # Emulate bottom-right corner curses error
            self.calls.append(("addstr", (y, x, text, attr), {}))
            raise curses.error("write to bottom-right cell")
        self.calls.append(("addstr", (y, x, text, attr), {}))

    def move(self, y: int, x: int) -> None:
        self.cursor_pos = (y, x)
        self.calls.append(("move", (y, x), {}))

    def erase(self) -> None:
        self.calls.append(("erase", (), {}))

    def clear(self) -> None:
        self.calls.append(("clear", (), {}))

    def refresh(self) -> None:
        self.calls.append(("refresh", (), {}))

    def keypad(self, flag: bool) -> None:
        self.calls.append(("keypad", (flag,), {}))

    def timeout(self, ms: int) -> None:
        self.calls.append(("timeout", (ms,), {}))

    def getch(self) -> int:
        if self.keys_queue:
            return self.keys_queue.pop(0)
        return -1


class TestTuiTheme(unittest.TestCase):
    """Test theme constants, colors, and drawing primitives."""

    def test_color_constants(self) -> None:
        self.assertEqual(PAIR_DEFAULT, 0)
        self.assertEqual(PAIR_HEADER, 1)
        self.assertEqual(PAIR_ACTIVE, 2)
        self.assertEqual(PAIR_ERROR, 3)
        self.assertEqual(PAIR_SUCCESS, 4)
        self.assertEqual(PAIR_MUTED, 5)
        self.assertEqual(PAIR_HIGHLIGHT, 6)
        self.assertEqual(PAIR_BORDER, 7)

    @patch("curses.has_colors", return_value=False)
    def test_init_theme_no_colors(self, mock_has_colors) -> None:
        res = init_theme()
        self.assertFalse(res)

    @patch("curses.has_colors", return_value=True)
    @patch("curses.start_color")
    @patch("curses.use_default_colors")
    @patch("curses.init_pair")
    def test_init_theme_success(self, mock_init_pair, mock_use_def, mock_start, mock_has_colors) -> None:
        res = init_theme()
        self.assertTrue(res)
        mock_start.assert_called_once()
        self.assertGreaterEqual(mock_init_pair.call_count, 7)

    def test_safe_addstr_bounds(self) -> None:
        win = MockCursesWindow(24, 80)

        # Normal within bounds
        safe_addstr(win, 5, 10, "Hello World", 0)
        self.assertTrue(any(call[1][2] == "Hello World" for call in win.calls if call[0] == "addstr"))

        # Empty string should do nothing
        calls_before = len(win.calls)
        safe_addstr(win, 5, 10, "", 0)
        self.assertEqual(len(win.calls), calls_before)

        # Offscreen row
        safe_addstr(win, -1, 10, "Bad Row", 0)
        safe_addstr(win, 25, 10, "Bad Row", 0)

        # Offscreen col
        safe_addstr(win, 5, 80, "Bad Col", 0)
        safe_addstr(win, 5, 100, "Bad Col", 0)

        # Negative col slices string
        win.calls.clear()
        safe_addstr(win, 5, -2, "Negative", 0)
        self.assertEqual(win.calls[0][1][2], "gative")
        self.assertEqual(win.calls[0][1][1], 0)

        # Text truncated when extending beyond max_x
        win.calls.clear()
        safe_addstr(win, 5, 75, "1234567890", 0)
        self.assertEqual(win.calls[0][1][2], "12345")

        # Bottom right corner exception caught safely
        win.calls.clear()
        safe_addstr(win, 23, 70, "123456789012345", 0)
        # Should not raise exception
        self.assertTrue(len(win.calls) > 0)

    def test_draw_box(self) -> None:
        win = MockCursesWindow(24, 80)
        draw_box(win, 2, 5, 10, 30, title="Test Box", color_pair=PAIR_BORDER)

        addstr_texts = [call[1][2] for call in win.calls if call[0] == "addstr"]
        # Check title was written
        self.assertTrue(any("Test Box" in t for t in addstr_texts))

        # Check small dimensions do not crash or draw invalid boxes
        win.calls.clear()
        draw_box(win, 0, 0, 1, 10)
        self.assertEqual(len(win.calls), 0)
        draw_box(win, 0, 0, 10, 1)
        self.assertEqual(len(win.calls), 0)

        # Center title alignment
        win.calls.clear()
        draw_box(win, 0, 0, 5, 20, title="Centered", title_align="center")
        self.assertTrue(any("Centered" in call[1][2] for call in win.calls if call[0] == "addstr"))


class DummyScreen(BaseScreen):
    """Concrete screen for testing BaseScreen."""

    def __init__(self, stdscr, app=None) -> None:
        super().__init__(stdscr, app)
        self.drawn = False

    def draw(self) -> None:
        self.drawn = True


class TestTuiScreens(unittest.TestCase):
    """Test BaseScreen lifecycle and navigation."""

    def test_base_screen_lifecycle(self) -> None:
        win = MockCursesWindow(24, 80)
        screen = DummyScreen(win, app="test_app")

        self.assertEqual(screen.app, "test_app")
        screen.on_enter()
        screen.draw()
        self.assertTrue(screen.drawn)
        screen.on_leave()

        # Helpers
        self.assertEqual(screen.get_maxyx(), (24, 80))
        screen.clear()
        screen.refresh()
        self.assertTrue(any(c[0] == "erase" for c in win.calls))
        self.assertTrue(any(c[0] == "refresh" for c in win.calls))

    def test_screen_widget_focus_delegation(self) -> None:
        win = MockCursesWindow(24, 80)
        screen = DummyScreen(win)

        w1 = TextInput(2, 2, 20, label="Input1")
        w2 = TextInput(4, 2, 20, label="Input2")
        screen.widgets = [w1, w2]
        screen.focus_widget(0)

        self.assertTrue(w1.is_focused)
        self.assertFalse(w2.is_focused)

        # Test key delegation
        handled = screen.handle_key(ord("a"))
        self.assertTrue(handled)
        self.assertEqual(w1.get_value(), "a")

        # Focus next
        screen.focus_next()
        self.assertFalse(w1.is_focused)
        self.assertTrue(w2.is_focused)

        # Focus prev wraps back
        screen.focus_prev()
        self.assertTrue(w1.is_focused)


class TestTuiWidgets(unittest.TestCase):
    """Test all widgets in core.tui.widgets."""

    def test_text_input(self) -> None:
        inp = TextInput(y=2, x=2, width=15, label="User", value="admin")
        self.assertEqual(inp.get_value(), "admin")
        self.assertEqual(inp.cursor_pos, 5)

        # Typing character
        inp.handle_key(ord("1"))
        self.assertEqual(inp.get_value(), "admin1")

        # Cursor navigation
        inp.handle_key(curses.KEY_LEFT)
        self.assertEqual(inp.cursor_pos, 5)
        inp.handle_key(curses.KEY_HOME)
        self.assertEqual(inp.cursor_pos, 0)
        inp.handle_key(curses.KEY_END)
        self.assertEqual(inp.cursor_pos, 6)

        # Backspace
        inp.handle_key(127)
        self.assertEqual(inp.get_value(), "admin")

        # Delete key
        inp.handle_key(curses.KEY_HOME)
        inp.handle_key(getattr(curses, "KEY_DC", 330))
        self.assertEqual(inp.get_value(), "dmin")

        # Draw password masked and cursor movement
        win = MockCursesWindow(24, 80)
        inp.is_password = True
        inp.is_focused = True
        inp.draw(win)
        self.assertTrue(any("*" in call[1][2] for call in win.calls if call[0] == "addstr"))
        # Focused field paints a one-cell block cursor at the insertion point
        cursor_x = inp.x + len(inp._get_label_str()) + 1 + (inp.cursor_pos - inp.scroll_offset)
        self.assertTrue(any(
            call[0] == "addstr" and call[1][0] == inp.y and call[1][1] == cursor_x and call[1][2] == "*"
            for call in win.calls
        ))

        # Test horizontal scrolling when value is long
        long_val = "very_long_input_string_that_exceeds_width"
        inp.set_value(long_val)
        inp.cursor_pos = len(long_val)
        inp.draw(win)
        self.assertGreater(inp.scroll_offset, 0)

        # Test unhandled keys
        self.assertFalse(inp.handle_key(curses.KEY_UP))
        self.assertFalse(inp.handle_key(9))  # Tab

        # Test string character input
        inp.handle_key("X")
        self.assertIn("X", inp.get_value())

    def test_checkbox(self) -> None:
        cb = Checkbox(y=5, x=5, label="Enable Cache", checked=False)
        self.assertFalse(cb.is_checked())

        # Toggle on Space
        handled = cb.handle_key(ord(" "))
        self.assertTrue(handled)
        self.assertTrue(cb.is_checked())

        # Toggle on Enter
        handled = cb.handle_key(10)
        self.assertTrue(handled)
        self.assertFalse(cb.is_checked())

        # Draw
        win = MockCursesWindow(24, 80)
        cb.draw(win)
        addstr_texts = [call[1][2] for call in win.calls if call[0] == "addstr"]
        self.assertTrue(any("Enable Cache" in t for t in addstr_texts))

    def test_select_list(self) -> None:
        changes: list[tuple[int, str]] = []
        sl = SelectList(
            y=3,
            x=2,
            width=20,
            label="Database",
            options=["sqlite", "mysql", "duckdb"],
            selected_index=0,
            on_change=lambda idx, val: changes.append((idx, val)),
        )

        self.assertEqual(sl.get_selected(), "sqlite")

        # Cycle right
        sl.handle_key(curses.KEY_RIGHT)
        self.assertEqual(sl.get_selected(), "mysql")
        self.assertEqual(changes[-1], (1, "mysql"))

        # Cycle right again
        sl.handle_key(ord(" "))
        self.assertEqual(sl.get_selected(), "duckdb")

        # Wrap around
        sl.handle_key(curses.KEY_RIGHT)
        self.assertEqual(sl.get_selected(), "sqlite")

        # Cycle left
        sl.handle_key(curses.KEY_LEFT)
        self.assertEqual(sl.get_selected(), "duckdb")

        # Draw
        win = MockCursesWindow(24, 80)
        sl.is_focused = True
        sl.draw(win)
        addstr_texts = [call[1][2] for call in win.calls if call[0] == "addstr"]
        self.assertTrue(any("< " in t for t in addstr_texts))

    def test_scrollable_list(self) -> None:
        items = [f"Item {i}" for i in range(20)]
        slist = ScrollableList(
            y=2,
            x=2,
            height=8,
            width=25,
            items=items,
            title="Items List",
        )

        self.assertEqual(slist.get_selected(), "Item 0")

        # Down
        slist.handle_key(curses.KEY_DOWN)
        self.assertEqual(slist.get_selected(), "Item 1")

        # Page Down
        slist.handle_key(curses.KEY_NPAGE)
        self.assertGreater(slist.selected_index, 1)

        # End
        slist.handle_key(curses.KEY_END)
        self.assertEqual(slist.get_selected(), "Item 19")

        # Home
        slist.handle_key(curses.KEY_HOME)
        self.assertEqual(slist.get_selected(), "Item 0")

        # Draw
        win = MockCursesWindow(24, 80)
        slist.draw(win)
        addstr_texts = [call[1][2] for call in win.calls if call[0] == "addstr"]
        self.assertTrue(any("Items List" in t for t in addstr_texts))

    def test_status_bar(self) -> None:
        bar = StatusBar(shortcuts=[("Tab", "Next"), ("Enter", "Select"), ("Esc", "Quit")])
        win = MockCursesWindow(24, 80)
        bar.draw(win)

        addstr_texts = [call[1][2] for call in win.calls if call[0] == "addstr"]
        self.assertTrue(any("[Tab]" in t for t in addstr_texts))
        self.assertTrue(any("[Enter]" in t for t in addstr_texts))

        # Status message
        win.calls.clear()
        bar.set_message("Error: Connection Failed", color=PAIR_ERROR)
        bar.draw(win)
        addstr_texts = [call[1][2] for call in win.calls if call[0] == "addstr"]
        self.assertTrue(any("Connection Failed" in t for t in addstr_texts))

    def test_modal_dialog(self) -> None:
        win = MockCursesWindow(24, 80)
        dialog = ModalDialog(
            win,
            title="Confirm Action",
            message="Are you sure you want to proceed with database backfill?",
            buttons=["OK", "Cancel"],
        )

        # Navigation between buttons
        dialog.handle_key(curses.KEY_RIGHT)
        self.assertEqual(dialog.selected_button_index, 1)

        dialog.handle_key(curses.KEY_LEFT)
        self.assertEqual(dialog.selected_button_index, 0)

        # Enter triggers selection
        res = dialog.handle_key(10)
        self.assertEqual(res, "OK")

        # Esc triggers cancel
        dialog.handle_key(curses.KEY_LEFT)
        cancel_res = dialog.handle_key(27)
        self.assertEqual(cancel_res, "Cancel")

        # Test show loop with mocked keys
        dialog.selected_button_index = 0
        win.keys_queue = [curses.KEY_RIGHT, 10]
        choice = dialog.show()
        self.assertEqual(choice, "Cancel")

        # Test run alias and __call__
        win.keys_queue = [10]
        self.assertEqual(dialog.run(), "Cancel")
        win.keys_queue = [10]
        self.assertEqual(dialog(), "Cancel")

        # Test show_dialog classmethod
        win.keys_queue = [10]
        res_class = ModalDialog.show_dialog(win, "Alert", "Operation completed", ["Dismiss"])
        self.assertEqual(res_class, "Dismiss")

        # Small window clamping
        small_win = MockCursesWindow(10, 30)
        small_dialog = ModalDialog(small_win, "Title", "A very long message " * 5, ["OK", "Cancel"])
        small_dialog.draw()
        self.assertTrue(len(small_win.calls) > 0)


if __name__ == "__main__":
    unittest.main()
