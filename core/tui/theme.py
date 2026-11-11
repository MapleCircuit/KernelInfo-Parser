"""Theme, color pairs, and low-level curses drawing primitives for TUI."""

import curses
import sys

# Color Pair Identifiers
PAIR_DEFAULT = 0
PAIR_HEADER = 1
PAIR_ACTIVE = 2
PAIR_ERROR = 3
PAIR_SUCCESS = 4
PAIR_MUTED = 5
PAIR_HIGHLIGHT = 6
PAIR_BORDER = 7
PAIR_CURSOR = 8

# Box Drawing Characters
BOX_UNICODE = {
    "tl": "┌",
    "tr": "┐",
    "bl": "└",
    "br": "┘",
    "h": "─",
    "v": "│",
}

BOX_ASCII = {
    "tl": "+",
    "tr": "+",
    "bl": "+",
    "br": "+",
    "h": "-",
    "v": "|",
}


def _supports_unicode() -> bool:
    try:
        encoding = sys.stdout.encoding or "utf-8"
        "┌─┐│└┘".encode(encoding)
        return True
    except Exception:
        return False


USE_UNICODE_BOX = _supports_unicode()


def init_theme() -> bool:
    """Initialize curses color pairs for the TUI theme.

    Returns:
        bool: True if curses colors were successfully initialized, False otherwise.
    """
    try:
        if not curses.has_colors():
            return False
        try:
            curses.start_color()
        except curses.error:
            pass

        bg = -1
        try:
            curses.use_default_colors()
        except (curses.error, AttributeError):
            bg = curses.COLOR_BLACK

        num_colors = getattr(curses, "COLORS", 8)
        hl_bg = curses.COLOR_CYAN if num_colors >= 8 else curses.COLOR_WHITE

        pairs = [
            (PAIR_HEADER, curses.COLOR_CYAN, bg),
            (PAIR_ACTIVE, curses.COLOR_YELLOW, bg),
            (PAIR_ERROR, curses.COLOR_RED, bg),
            (PAIR_SUCCESS, curses.COLOR_GREEN, bg),
            (PAIR_MUTED, curses.COLOR_WHITE, bg),
            (PAIR_HIGHLIGHT, curses.COLOR_BLACK, hl_bg),
            (PAIR_BORDER, curses.COLOR_CYAN, bg),
            (PAIR_CURSOR, curses.COLOR_BLACK, curses.COLOR_WHITE),
        ]
        for pair_id, fg, b in pairs:
            try:
                curses.init_pair(pair_id, fg, b)
            except curses.error:
                pass
        return True
    except Exception:
        return False


def get_color(pair_id: int) -> int:
    """Return the curses color attribute for the given color pair ID.

    If colors are unsupported or curses raises an error, returns 0.
    """
    try:
        if curses.has_colors():
            return curses.color_pair(pair_id)
    except (curses.error, AttributeError):
        pass
    except Exception:
        pass
    return 0


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Write string to window catching curses.error (e.g. bottom-right corner wrap exception).

    Safely handles boundary checking, negative offsets, and prevents crashing
    on terminal edges or window resizing edge cases.
    """
    if not text:
        return
    try:
        max_y, max_x = win.getmaxyx()
    except Exception:
        return

    if y < 0 or y >= max_y or max_x <= 0 or max_y <= 0:
        return

    if x < 0:
        offset = -x
        if offset >= len(text):
            return
        text = text[offset:]
        x = 0

    if x >= max_x:
        return

    avail = max_x - x
    if avail <= 0:
        return

    if len(text) > avail:
        text = text[:avail]

    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        # Curses raises curses.error on bottom-right cell write even when successful
        pass
    except Exception:
        pass


def draw_box(
    win,
    y: int,
    x: int,
    h: int,
    w: int,
    title: str | None = None,
    color_pair: int = 0,
    ascii_only: bool = False,
    title_align: str = "left",
) -> None:
    """Draw a clean border box with optional title centered or left-aligned on the top line.

    Args:
        win: Curses window object.
        y: Top y-coordinate.
        x: Left x-coordinate.
        h: Height of the box in rows.
        w: Width of the box in columns.
        title: Optional title string to display on top border.
        color_pair: Curses color pair ID or attribute bitmask.
        ascii_only: Force standard ASCII border characters (+, -, |).
        title_align: "left" or "center".
    """
    if h < 2 or w < 2:
        return

    attr = get_color(color_pair) if (0 < color_pair < 64) else color_pair

    chars = BOX_ASCII if (ascii_only or not USE_UNICODE_BOX) else BOX_UNICODE
    tl, tr = chars["tl"], chars["tr"]
    bl, br = chars["bl"], chars["br"]
    h_char, v_char = chars["h"], chars["v"]

    inner_w = w - 2
    top_line = tl + (h_char * inner_w) + tr
    bot_line = bl + (h_char * inner_w) + br

    # Draw top border
    safe_addstr(win, y, x, top_line, attr)

    # Render title if provided
    if title:
        clean_title = title.strip()
        formatted_title = f" {clean_title} "
        max_title_len = max(0, w - 4)
        if len(formatted_title) > max_title_len:
            truncated = clean_title[:max(0, max_title_len - 3)]
            formatted_title = f" {truncated}.. " if truncated else ""
            if len(formatted_title) > max_title_len:
                formatted_title = formatted_title[:max_title_len]

        if formatted_title and len(formatted_title) <= inner_w:
            if title_align == "center":
                title_x = x + (w - len(formatted_title)) // 2
            else:
                title_x = x + 2
            bold_attr = attr | curses.A_BOLD if hasattr(curses, "A_BOLD") else attr
            safe_addstr(win, y, title_x, formatted_title, bold_attr)

    # Draw vertical side walls
    for row in range(1, h - 1):
        safe_addstr(win, y + row, x, v_char, attr)
        safe_addstr(win, y + row, x + w - 1, v_char, attr)

    # Draw bottom border
    safe_addstr(win, y + h - 1, x, bot_line, attr)
