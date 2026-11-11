"""Reusable TUI widgets for curses interfaces."""

from typing import Any, Callable, List, Optional, Tuple
import curses

from core.tui.theme import (
    PAIR_DEFAULT,
    PAIR_ACTIVE,
    PAIR_ERROR,
    PAIR_SUCCESS,
    PAIR_MUTED,
    PAIR_HIGHLIGHT,
    PAIR_BORDER,
    PAIR_CURSOR,
    get_color,
    safe_addstr,
    draw_box,
)


class TextInput:
    """Single-line text input field widget with cursor navigation and password masking.

    Attributes:
        y: Row position.
        x: Column position.
        width: Field entry width (excluding label).
        label: Label text displayed before the input field.
        value: Current text string in the field.
        is_password: Mask value with '*' when True.
        max_len: Maximum permitted characters.
        cursor_pos: 0-based insertion cursor position.
        scroll_offset: Horizontal scroll offset for values longer than field width.
        is_focused: Focus state boolean.
    """

    def __init__(
        self,
        y: int,
        x: int,
        width: int,
        label: str = "",
        value: str = "",
        is_password: bool = False,
        max_len: int = 256,
        label_width: Optional[int] = None,
    ) -> None:
        self.y = y
        self.x = x
        self.width = max(4, width)
        self.label = label
        self.label_width = label_width
        self.value = str(value)
        self.is_password = bool(is_password)
        self.max_len = max_len
        self.cursor_pos = len(self.value)
        self.scroll_offset = 0
        self.is_focused = False

    def get_value(self) -> str:
        """Return the current string value."""
        return self.value

    def set_value(self, val: str) -> None:
        """Set the field value and clamp cursor."""
        self.value = str(val)[: self.max_len]
        self.cursor_pos = min(self.cursor_pos, len(self.value))

    def _get_label_str(self) -> str:
        if not self.label:
            return ""
        lbl = self.label if self.label.endswith(":") else f"{self.label}:"
        if self.label_width is not None and self.label_width > len(lbl):
            return lbl.ljust(self.label_width) + " "
        return f"{lbl} "

    def draw(self, win: Any) -> None:
        """Render the label and input box on the window."""
        label_str = self._get_label_str()
        lbl_len = len(label_str)

        # Draw label
        if lbl_len > 0:
            lbl_attr = (get_color(PAIR_ACTIVE) | curses.A_BOLD) if self.is_focused else get_color(PAIR_DEFAULT)
            safe_addstr(win, self.y, self.x, label_str, lbl_attr)

        field_x = self.x + lbl_len
        inner_w = max(1, self.width - 2)

        # Clamp cursor and adjust horizontal scrolling
        self.cursor_pos = max(0, min(self.cursor_pos, len(self.value)))
        if self.cursor_pos < self.scroll_offset:
            self.scroll_offset = self.cursor_pos
        elif self.cursor_pos >= self.scroll_offset + inner_w:
            self.scroll_offset = self.cursor_pos - inner_w + 1

        disp_text = ("*" * len(self.value)) if self.is_password else self.value
        visible_text = disp_text[self.scroll_offset : self.scroll_offset + inner_w]
        padded_text = visible_text.ljust(inner_w)

        # Border brackets and field body
        bracket_attr = (get_color(PAIR_ACTIVE) | curses.A_BOLD) if self.is_focused else get_color(PAIR_MUTED)
        text_attr = (get_color(PAIR_HIGHLIGHT) if self.is_focused else get_color(PAIR_DEFAULT))

        safe_addstr(win, self.y, field_x, "[", bracket_attr)
        safe_addstr(win, self.y, field_x + 1, padded_text, text_attr)
        safe_addstr(win, self.y, field_x + inner_w + 1, "]", bracket_attr)

        # Paint a block cursor cell at the insertion point when focused (the
        # terminal cursor is hidden app-wide via curs_set(0)).
        if self.is_focused:
            cursor_col = self.cursor_pos - self.scroll_offset
            cursor_char = padded_text[cursor_col] if 0 <= cursor_col < len(padded_text) else " "
            cursor_attr = get_color(PAIR_CURSOR) or curses.A_REVERSE
            safe_addstr(win, self.y, field_x + 1 + cursor_col, cursor_char, cursor_attr)

    def handle_key(self, ch: int) -> bool:
        """Handle cursor motion, text typing, and deletions.

        Returns True if consumed, False otherwise.
        """
        # Navigation
        if ch == curses.KEY_LEFT:
            if self.cursor_pos > 0:
                self.cursor_pos -= 1
            return True
        elif ch == curses.KEY_RIGHT:
            if self.cursor_pos < len(self.value):
                self.cursor_pos += 1
            return True
        elif ch in (curses.KEY_HOME, 1):  # 1 = Ctrl+A
            self.cursor_pos = 0
            return True
        elif ch in (curses.KEY_END, 5):  # 5 = Ctrl+E
            self.cursor_pos = len(self.value)
            return True

        # Deletions
        elif ch in (curses.KEY_BACKSPACE, 8, 127):
            if self.cursor_pos > 0:
                self.value = self.value[: self.cursor_pos - 1] + self.value[self.cursor_pos :]
                self.cursor_pos -= 1
            return True
        elif ch in (getattr(curses, "KEY_DC", -1), getattr(curses, "KEY_SDC", -1)):
            if self.cursor_pos < len(self.value):
                self.value = self.value[: self.cursor_pos] + self.value[self.cursor_pos + 1 :]
            return True

        # Character insertion
        if isinstance(ch, str):
            if ch.isprintable() and len(self.value) < self.max_len:
                self.value = self.value[: self.cursor_pos] + ch + self.value[self.cursor_pos :]
                self.cursor_pos += len(ch)
                return True
        elif isinstance(ch, int) and 32 <= ch < 256 and ch != 127:
            if len(self.value) < self.max_len:
                c = chr(ch)
                self.value = self.value[: self.cursor_pos] + c + self.value[self.cursor_pos :]
                self.cursor_pos += 1
                return True

        return False


class Checkbox:
    """Toggleable checkbox widget rendered as [X] Label or [ ] Label."""

    def __init__(self, y: int, x: int, label: str, checked: bool = False) -> None:
        self.y = y
        self.x = x
        self.label = label
        self.checked = bool(checked)
        self.is_focused = False

    def is_checked(self) -> bool:
        """Return True if checked, False otherwise."""
        return self.checked

    def set_checked(self, val: bool) -> None:
        """Set checkbox state."""
        self.checked = bool(val)

    def toggle(self) -> bool:
        """Toggle checkbox state and return new state."""
        self.checked = not self.checked
        return self.checked

    def draw(self, win: Any) -> None:
        """Render checkbox and label."""
        bracket_attr = (get_color(PAIR_ACTIVE) | curses.A_BOLD) if self.is_focused else get_color(PAIR_MUTED)
        mark_attr = (get_color(PAIR_SUCCESS) | curses.A_BOLD) if self.checked else get_color(PAIR_MUTED)
        lbl_attr = (get_color(PAIR_ACTIVE) | curses.A_BOLD) if self.is_focused else get_color(PAIR_DEFAULT)

        mark = "X" if self.checked else " "
        safe_addstr(win, self.y, self.x, "[", bracket_attr)
        safe_addstr(win, self.y, self.x + 1, mark, mark_attr)
        safe_addstr(win, self.y, self.x + 2, "] ", bracket_attr)
        safe_addstr(win, self.y, self.x + 4, self.label, lbl_attr)

        if self.is_focused:
            try:
                win.move(self.y, self.x + 1)
            except Exception:
                pass

    def handle_key(self, ch: int) -> bool:
        """Toggle on Space or Enter."""
        if ch in (ord(" "), 10, 13, curses.KEY_ENTER):
            self.toggle()
            return True
        return False


class SelectList:
    """Horizontal single-select cycling option widget rendered as < option >."""

    def __init__(
        self,
        y: int,
        x: int,
        width: int,
        label: str,
        options: List[str],
        selected_index: int = 0,
        label_width: Optional[int] = None,
        on_change: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        self.y = y
        self.x = x
        self.width = max(6, width)
        self.label = label
        self.label_width = label_width
        self.options = list(options) if options else [""]
        self.selected_index = max(0, min(selected_index, len(self.options) - 1))
        self.is_focused = False
        self.on_change = on_change

    def get_selected(self) -> str:
        """Return currently selected option string."""
        if self.options and 0 <= self.selected_index < len(self.options):
            return self.options[self.selected_index]
        return ""

    def get_selected_index(self) -> int:
        """Return index of selected option."""
        return self.selected_index

    def set_selected(self, index: int) -> None:
        """Set selected option by index."""
        if not self.options:
            return
        clamped = max(0, min(index, len(self.options) - 1))
        if clamped != self.selected_index:
            self.selected_index = clamped
            if callable(self.on_change):
                self.on_change(self.selected_index, self.get_selected())

    def set_options(self, options: List[str], selected_index: int = 0) -> None:
        """Update available options and selection."""
        self.options = list(options) if options else [""]
        self.selected_index = max(0, min(selected_index, len(self.options) - 1))

    def _get_label_str(self) -> str:
        if not self.label:
            return ""
        lbl = self.label if self.label.endswith(":") else f"{self.label}:"
        if self.label_width is not None and self.label_width > len(lbl):
            return lbl.ljust(self.label_width) + " "
        return f"{lbl} "

    def draw(self, win: Any) -> None:
        """Render label and cycling options selector."""
        label_str = self._get_label_str()
        lbl_len = len(label_str)

        if lbl_len > 0:
            lbl_attr = (get_color(PAIR_ACTIVE) | curses.A_BOLD) if self.is_focused else get_color(PAIR_DEFAULT)
            safe_addstr(win, self.y, self.x, label_str, lbl_attr)

        ctrl_x = self.x + lbl_len
        inner_w = max(1, self.width - 4)

        opt_text = self.get_selected()
        disp_text = opt_text[:inner_w].center(inner_w)

        arrow_attr = (get_color(PAIR_ACTIVE) | curses.A_BOLD) if self.is_focused else get_color(PAIR_MUTED)
        body_attr = (get_color(PAIR_HIGHLIGHT) | curses.A_BOLD) if self.is_focused else get_color(PAIR_DEFAULT)

        safe_addstr(win, self.y, ctrl_x, "< ", arrow_attr)
        safe_addstr(win, self.y, ctrl_x + 2, disp_text, body_attr)
        safe_addstr(win, self.y, ctrl_x + 2 + inner_w, " >", arrow_attr)

        if self.is_focused:
            try:
                win.move(self.y, ctrl_x + 1)
            except Exception:
                pass

    def select_prev(self) -> None:
        """Cycle to previous option."""
        if not self.options:
            return
        new_idx = (self.selected_index - 1) % len(self.options)
        self.set_selected(new_idx)

    def select_next(self) -> None:
        """Cycle to next option."""
        if not self.options:
            return
        new_idx = (self.selected_index + 1) % len(self.options)
        self.set_selected(new_idx)

    def handle_key(self, ch: int) -> bool:
        """Cycle left/right on arrow keys, space, or enter."""
        if ch in (curses.KEY_LEFT, ord("h"), ord("H")):
            self.select_prev()
            return True
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("L"), 10, 13, curses.KEY_ENTER, ord(" ")):
            self.select_next()
            return True
        return False


class ScrollableList:
    """Vertically scrollable list widget with box border, scroll indicators, and selection.

    Attributes:
        y: Top y-coordinate.
        x: Left x-coordinate.
        height: Total box height.
        width: Total box width.
        items: List of items.
        title: Box title.
        render_item_func: Function to convert an item into a display string.
        selected_index: Index of currently selected item.
        scroll_offset: First visible item row index.
        is_focused: Focus state boolean.
    """

    def __init__(
        self,
        y: int,
        x: int,
        height: int,
        width: int,
        items: Optional[List[Any]] = None,
        title: str = "",
        render_item_func: Optional[Callable[[Any], str]] = None,
    ) -> None:
        self.y = y
        self.x = x
        self.height = max(3, height)
        self.width = max(6, width)
        self.items = list(items) if items is not None else []
        self.title = title
        self.render_item_func = render_item_func
        self.selected_index = 0
        self.scroll_offset = 0
        self.is_focused = True

    def get_selected(self) -> Optional[Any]:
        """Return currently selected item or None."""
        if self.items and 0 <= self.selected_index < len(self.items):
            return self.items[self.selected_index]
        return None

    def get_selected_index(self) -> int:
        """Return index of selected item."""
        return self.selected_index

    def set_selected(self, index: int) -> None:
        """Set selected item by index."""
        if not self.items:
            self.selected_index = 0
            return
        self.selected_index = max(0, min(index, len(self.items) - 1))

    def set_items(self, items: List[Any], keep_selection: bool = True) -> None:
        """Update items in list."""
        self.items = list(items)
        if not self.items:
            self.selected_index = 0
            self.scroll_offset = 0
        elif not keep_selection:
            self.selected_index = 0
            self.scroll_offset = 0
        else:
            self.selected_index = max(0, min(self.selected_index, len(self.items) - 1))

    def select_prev(self) -> None:
        """Move selection up one item."""
        if self.selected_index > 0:
            self.selected_index -= 1

    def select_next(self) -> None:
        """Move selection down one item."""
        if self.selected_index < len(self.items) - 1:
            self.selected_index += 1

    def page_up(self) -> None:
        """Page up by visible inner height."""
        inner_h = max(1, self.height - 2)
        self.selected_index = max(0, self.selected_index - inner_h)

    def page_down(self) -> None:
        """Page down by visible inner height."""
        inner_h = max(1, self.height - 2)
        self.selected_index = min(max(0, len(self.items) - 1), self.selected_index + inner_h)

    def select_first(self) -> None:
        """Move selection to first item."""
        self.selected_index = 0

    def select_last(self) -> None:
        """Move selection to last item."""
        if self.items:
            self.selected_index = len(self.items) - 1

    def draw(self, win: Any) -> None:
        """Draw the scrollable list container, items, and indicators."""
        border_pair = PAIR_ACTIVE if self.is_focused else PAIR_BORDER
        draw_box(win, self.y, self.x, self.height, self.width, title=self.title, color_pair=border_pair)

        inner_h = max(1, self.height - 2)
        inner_w = max(1, self.width - 2)

        if self.items:
            self.selected_index = max(0, min(self.selected_index, len(self.items) - 1))
            if self.selected_index < self.scroll_offset:
                self.scroll_offset = self.selected_index
            elif self.selected_index >= self.scroll_offset + inner_h:
                self.scroll_offset = self.selected_index - inner_h + 1
        else:
            self.selected_index = 0
            self.scroll_offset = 0

        # Scroll indicators on top and bottom right border
        if self.scroll_offset > 0:
            safe_addstr(win, self.y, self.x + self.width - 2, "↑", get_color(PAIR_ACTIVE) | curses.A_BOLD)
        if self.scroll_offset + inner_h < len(self.items):
            safe_addstr(win, self.y + self.height - 1, self.x + self.width - 2, "↓", get_color(PAIR_ACTIVE) | curses.A_BOLD)

        # Draw visible rows
        for r in range(inner_h):
            idx = self.scroll_offset + r
            row_y = self.y + 1 + r
            row_x = self.x + 1

            if idx < len(self.items):
                item = self.items[idx]
                text = self.render_item_func(item) if self.render_item_func else str(item)
                is_sel = (idx == self.selected_index)

                if is_sel:
                    pointer = "► " if self.is_focused else "> "
                    line = (pointer + text)[:inner_w].ljust(inner_w)
                    attr = (get_color(PAIR_HIGHLIGHT) | curses.A_BOLD) if self.is_focused else (get_color(PAIR_ACTIVE) | curses.A_BOLD)
                else:
                    line = ("  " + text)[:inner_w].ljust(inner_w)
                    attr = get_color(PAIR_DEFAULT)

                safe_addstr(win, row_y, row_x, line, attr)
            else:
                safe_addstr(win, row_y, row_x, " " * inner_w, get_color(PAIR_DEFAULT))

    def handle_key(self, ch: int) -> bool:
        """Handle list navigation keys."""
        if ch in (curses.KEY_UP, ord("k")):
            self.select_prev()
            return True
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.select_next()
            return True
        elif ch in (curses.KEY_PPAGE, getattr(curses, "KEY_PREVIOUS", -1)):
            self.page_up()
            return True
        elif ch in (curses.KEY_NPAGE, getattr(curses, "KEY_NEXT", -1)):
            self.page_down()
            return True
        elif ch == curses.KEY_HOME:
            self.select_first()
            return True
        elif ch == curses.KEY_END:
            self.select_last()
            return True
        return False


class Button:
    """Focusable button widget triggered by Enter or Space."""

    def __init__(
        self,
        y: int,
        x: int,
        label: str,
        on_click: Optional[Callable[[], Any]] = None,
        width: Optional[int] = None,
    ) -> None:
        self.y = y
        self.x = x
        self.label = label
        self.on_click = on_click
        self.width = width or (len(label) + 4)
        self.is_focused = False

    def draw(self, win: Any) -> None:
        """Render button with brackets."""
        btn_text = f"[ {self.label} ]"
        if self.is_focused:
            attr = get_color(PAIR_HIGHLIGHT) | curses.A_BOLD
        else:
            attr = get_color(PAIR_ACTIVE)
        safe_addstr(win, self.y, self.x, btn_text, attr)
        if self.is_focused:
            try:
                win.move(self.y, self.x + 2)
            except Exception:
                pass

    def handle_key(self, ch: int) -> bool:
        """Trigger on Space or Enter."""
        if ch in (ord(" "), 10, 13, curses.KEY_ENTER):
            if callable(self.on_click):
                self.on_click()
            return True
        return False


class StatusBar:
    """Bottom status bar displaying keyboard shortcuts and status messages."""

    def __init__(
        self,
        shortcuts: Optional[List[Tuple[str, str]]] = None,
        y: Optional[int] = None,
    ) -> None:
        self.shortcuts = list(shortcuts) if shortcuts is not None else []
        self.y = y
        self.message: Optional[str] = None
        self.message_color: int = PAIR_MUTED

    def set_shortcuts(self, shortcuts: List[Tuple[str, str]]) -> None:
        """Update shortcuts list."""
        self.shortcuts = list(shortcuts)

    def set_message(self, message: Optional[str], color: int = PAIR_MUTED) -> None:
        """Set a temporary message over the status bar."""
        self.message = message
        self.message_color = color

    def draw(self, win: Any) -> None:
        """Render status bar on the specified or bottom line."""
        try:
            max_y, max_x = win.getmaxyx()
        except Exception:
            return

        row_y = self.y if self.y is not None else (max_y - 1)
        if row_y < 0 or row_y >= max_y or max_x <= 0:
            return

        # Fill line with background
        base_attr = curses.A_REVERSE if hasattr(curses, "A_REVERSE") else 0
        safe_addstr(win, row_y, 0, " " * max_x, base_attr)

        if self.message:
            msg_attr = base_attr | get_color(self.message_color) | curses.A_BOLD
            safe_addstr(win, row_y, 1, f" {self.message} ", msg_attr)
            return

        col = 1
        for key, desc in self.shortcuts:
            key_text = f"[{key}]"
            desc_text = f" {desc}  "
            entry_len = len(key_text) + len(desc_text)
            if col + entry_len >= max_x:
                break

            key_attr = base_attr | get_color(PAIR_ACTIVE) | curses.A_BOLD
            desc_attr = base_attr | get_color(PAIR_DEFAULT)

            safe_addstr(win, row_y, col, key_text, key_attr)
            safe_addstr(win, row_y, col + len(key_text), desc_text, desc_attr)
            col += entry_len


class ModalDialog:
    """Centered modal popup dialog with title, multi-line message, and selectable buttons."""

    def __init__(
        self,
        stdscr: Any,
        title: str,
        message: str,
        buttons: Optional[List[str]] = None,
        default_button: int = 0,
    ) -> None:
        self.stdscr = stdscr
        self.title = title
        self.message = message
        self.buttons = list(buttons) if buttons is not None else ["OK"]
        if not self.buttons:
            self.buttons = ["OK"]
        self.selected_button_index = max(0, min(default_button, len(self.buttons) - 1))

    def _calculate_geometry(self) -> Tuple[int, int, int, int, List[str]]:
        """Calculate dialog dimensions and wrapped message lines."""
        try:
            max_y, max_x = self.stdscr.getmaxyx()
        except Exception:
            max_y, max_x = 24, 80

        # Wrap message lines
        content_max_w = max(24, min(60, max_x - 8))
        wrapped_lines: List[str] = []
        for raw_line in self.message.split("\n"):
            if not raw_line:
                wrapped_lines.append("")
                continue
            words = raw_line.split(" ")
            curr_line = ""
            for word in words:
                if not curr_line:
                    curr_line = word
                elif len(curr_line) + 1 + len(word) <= content_max_w:
                    curr_line += " " + word
                else:
                    wrapped_lines.append(curr_line)
                    curr_line = word
            if curr_line:
                wrapped_lines.append(curr_line)

        # Calculate width
        buttons_w = sum(len(b) + 6 for b in self.buttons)
        msg_w = max((len(l) for l in wrapped_lines), default=10)
        title_w = len(self.title) + 6
        dialog_w = max(buttons_w + 4, msg_w + 6, title_w, 36)
        dialog_w = min(dialog_w, max_x - 4)

        # Calculate height: top border (1) + blank (1) + text + blank (1) + buttons (1) + bottom border (1)
        dialog_h = len(wrapped_lines) + 6
        dialog_h = min(dialog_h, max_y - 2)

        dialog_y = max(0, (max_y - dialog_h) // 2)
        dialog_x = max(0, (max_x - dialog_w) // 2)

        return dialog_y, dialog_x, dialog_h, dialog_w, wrapped_lines

    def draw(self, win: Optional[Any] = None) -> None:
        """Render the modal dialog and buttons."""
        target_win = win or self.stdscr
        y, x, h, w, wrapped_lines = self._calculate_geometry()

        # Clear background box interior
        for row in range(1, h - 1):
            safe_addstr(target_win, y + row, x + 1, " " * (w - 2), get_color(PAIR_DEFAULT))

        # Border
        draw_box(target_win, y, x, h, w, title=self.title, color_pair=PAIR_ACTIVE, title_align="center")

        # Message text
        text_y = y + 2
        for i, line in enumerate(wrapped_lines):
            if text_y + i >= y + h - 3:
                break
            safe_addstr(target_win, text_y + i, x + 3, line[: w - 6], get_color(PAIR_DEFAULT))

        # Buttons row
        btn_y = y + h - 2
        btn_strings = [f"[ {b} ]" for b in self.buttons]
        total_btns_len = sum(len(s) for s in btn_strings) + (2 * (len(btn_strings) - 1))
        btn_start_x = max(x + 2, x + (w - total_btns_len) // 2)

        curr_btn_x = btn_start_x
        for i, b_text in enumerate(self.buttons):
            s = f"[ {b_text} ]"
            if i == self.selected_button_index:
                attr = get_color(PAIR_HIGHLIGHT) | curses.A_BOLD
            else:
                attr = get_color(PAIR_MUTED)
            safe_addstr(target_win, btn_y, curr_btn_x, s, attr)
            curr_btn_x += len(s) + 2

    def handle_key(self, ch: int) -> Optional[str]:
        """Handle key input for modal navigation.

        Returns chosen button string if selected, None otherwise.
        """
        if ch in (curses.KEY_LEFT, ord("h"), ord("H"), 9):  # 9 = Tab
            if ch == 9 and self.selected_button_index == len(self.buttons) - 1:
                self.selected_button_index = 0
            else:
                self.selected_button_index = (self.selected_button_index - 1) % len(self.buttons)
            return None
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("L")):
            self.selected_button_index = (self.selected_button_index + 1) % len(self.buttons)
            return None
        elif ch in (10, 13, curses.KEY_ENTER, ord(" ")):
            return self.buttons[self.selected_button_index]
        elif ch == 27:  # Esc
            if "Cancel" in self.buttons:
                return "Cancel"
            return self.buttons[-1]
        return None

    def show(self) -> Optional[str]:
        """Run modal event loop until button selected or closed.

        Returns chosen button label (e.g. "OK", "Cancel") or None.
        """
        try:
            self.stdscr.keypad(True)
        except Exception:
            pass

        while True:
            self.draw()
            try:
                self.stdscr.refresh()
            except Exception:
                pass

            try:
                ch = self.stdscr.getch()
            except KeyboardInterrupt:
                return "Cancel" if "Cancel" in self.buttons else None
            except Exception:
                return self.buttons[self.selected_button_index]
            if ch == -1:  # no input source (non-blocking window): take the highlighted button
                return self.buttons[self.selected_button_index]

            result = self.handle_key(ch)
            if result is not None:
                return result

    def run(self) -> Optional[str]:
        """Alias for show()."""
        return self.show()

    def __call__(self) -> Optional[str]:
        """Callable invocation runs show()."""
        return self.show()

    @classmethod
    def show_dialog(
        cls,
        stdscr: Any,
        title: str,
        message: str,
        buttons: Optional[List[str]] = None,
        default_button: int = 0,
    ) -> Optional[str]:
        """Convenience helper to instantiate and display a modal dialog."""
        return cls(stdscr, title, message, buttons=buttons, default_button=default_button).show()
