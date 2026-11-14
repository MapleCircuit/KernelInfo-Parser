"""core/tui/screens/logging_cfg.py - Parser and webapp file logging configuration screen."""

from __future__ import annotations

import curses
from typing import Any

from core.config import (
    VALID_LOG_LEVELS,
    VALID_WEBAPP_REQUEST_MODES,
    _coerce_bool,
    normalize_log_level,
    normalize_webapp_request_mode,
)
from core.tui.screens.base import BaseScreen
from core.tui.screens.parser_dist import wrap_help
from core.tui.theme import (
    PAIR_BORDER,
    PAIR_DEFAULT,
    PAIR_HEADER,
    PAIR_MUTED,
    draw_box,
    get_color,
    safe_addstr,
)
from core.tui.widgets import (
    Button,
    Checkbox,
    ModalDialog,
    SelectList,
    StatusBar,
    TextInput,
)

# Side-panel help: topic key -> (panel title, paragraphs). A paragraph starting with "- " wraps with a hanging indent.
HELP_TOPICS: dict[str, tuple[str, list[str]]] = {
    "parser_enabled": (
        "Parser File Logging",
        [
            "Enable or disable writing parser pipeline logs to disk.",
            "When disabled, parser output is not written to a log file unless overridden by CLI/environment.",
        ],
    ),
    "parser_dir": (
        "Parser Log Directory",
        [
            "Directory where the parser log file is written.",
            "Leave empty to use the project root or default log location when enabled.",
        ],
    ),
    "parser_file": (
        "Parser Log File",
        [
            "Filename (or path) for the parser log file. Defaults to parser.log.",
        ],
    ),
    "error_split": (
        "Optional Error Log",
        [
            "Optionally write ERROR and CRITICAL records to a separate error log file.",
            "Leave Error Dir and Error File empty to keep all log levels in the main log file.",
        ],
    ),
    "log_level": (
        "Log Level",
        [
            "Minimum severity written to the log file.",
            "- DEBUG: verbose diagnostics and tracing.",
            "- INFO: standard progress and lifecycle messages (default).",
            "- WARNING / ERROR / CRITICAL: increasingly strict filtering.",
        ],
    ),
    "webapp_enabled": (
        "Webapp File Logging",
        [
            "Enable or disable writing webapp backend logs to disk.",
            "When enabled, FastAPI/uvicorn backend and request logs are recorded according to Request Logging and Log Level.",
        ],
    ),
    "request_mode": (
        "Request Logging",
        [
            "Controls which HTTP requests the webapp backend writes to its log.",
            "- all: log every HTTP request (default).",
            "- errors: log only failed/error HTTP requests (4xx/5xx or exceptions).",
        ],
    ),
    "webapp_dir": (
        "Webapp Log Directory",
        [
            "Directory where the webapp backend log file is written.",
            "Leave empty to use the project root or default log location when enabled.",
        ],
    ),
    "webapp_file": (
        "Webapp Log File",
        [
            "Filename (or path) for the webapp backend log file. Defaults to webapp.log.",
        ],
    ),
    "overview": (
        "Help",
        [
            "Move to a field to see its help here (F1 opens it in a dialog).",
            "- Parser Logging: file output, optional error split, and level for the parser pipeline.",
            "- Webapp Backend Logging: file output, request filter (all vs. errors), optional error split, and level.",
        ],
    ),
}

_FORM_MIN_W = 52
_HELP_MIN_W = 30


class LoggingScreen(BaseScreen):
    """Configuration screen for Parser and Webapp Backend file logging options.

    Attributes:
        app: Controller reference exposing .config, .is_dirty, and .pop_screen().
    """

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)

        # Retrieve or initialize config sections
        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            logging_cfg = self.app.config.get("logging")
            if not isinstance(logging_cfg, dict):
                logging_cfg = {}
                self.app.config["logging"] = logging_cfg
        else:
            logging_cfg = {}

        parser_cfg = logging_cfg.get("parser")
        if not isinstance(parser_cfg, dict):
            parser_cfg = {}
            logging_cfg["parser"] = parser_cfg
        webapp_cfg = logging_cfg.get("webapp")
        if not isinstance(webapp_cfg, dict):
            webapp_cfg = {}
            logging_cfg["webapp"] = webapp_cfg

        self.logging_cfg = logging_cfg
        self.parser_log_cfg = parser_cfg
        self.webapp_log_cfg = webapp_cfg

        level_opts = list(VALID_LOG_LEVELS)
        req_mode_opts = list(VALID_WEBAPP_REQUEST_MODES)

        # Initial parser logging values
        cur_p_enabled = _coerce_bool(parser_cfg.get("enabled"), False)
        cur_p_dir = str(parser_cfg.get("dir") or "")
        p_file_raw = parser_cfg.get("file")
        cur_p_file = "parser.log" if p_file_raw is None else str(p_file_raw)
        cur_p_err_dir = str(parser_cfg.get("error_dir") or "")
        cur_p_err_file = str(parser_cfg.get("error_file") or "")
        cur_p_level = normalize_log_level(parser_cfg.get("level", "INFO"))
        idx_p_level = level_opts.index(cur_p_level) if cur_p_level in level_opts else 1

        # Initial webapp logging values
        cur_w_enabled = _coerce_bool(webapp_cfg.get("enabled"), False)
        cur_w_req_mode = normalize_webapp_request_mode(webapp_cfg.get("request_mode", "all"))
        idx_w_req_mode = req_mode_opts.index(cur_w_req_mode) if cur_w_req_mode in req_mode_opts else 0
        cur_w_dir = str(webapp_cfg.get("dir") or "")
        w_file_raw = webapp_cfg.get("file")
        cur_w_file = "webapp.log" if w_file_raw is None else str(w_file_raw)
        cur_w_err_dir = str(webapp_cfg.get("error_dir") or "")
        cur_w_err_file = str(webapp_cfg.get("error_file") or "")
        cur_w_level = normalize_log_level(webapp_cfg.get("level", "INFO"))
        idx_w_level = level_opts.index(cur_w_level) if cur_w_level in level_opts else 1

        # Box 1: Parser Logging widgets (y=4..9 inside box y=3, h=8)
        self.chk_parser_enabled = Checkbox(
            y=4, x=4, label="Enable parser file logging", checked=cur_p_enabled
        )
        self.inp_parser_dir = TextInput(
            y=5, x=4, width=26, label="Log Directory", value=cur_p_dir, label_width=20
        )
        self.inp_parser_file = TextInput(
            y=6, x=4, width=26, label="Log File", value=cur_p_file, label_width=20
        )
        self.inp_parser_error_dir = TextInput(
            y=7, x=4, width=26, label="Error Dir (opt)", value=cur_p_err_dir, label_width=20
        )
        self.inp_parser_error_file = TextInput(
            y=8, x=4, width=26, label="Error File (opt)", value=cur_p_err_file, label_width=20
        )
        self.sel_parser_level = SelectList(
            y=9, x=4, width=16, label="Log Level", options=level_opts, selected_index=idx_p_level, label_width=20
        )

        # Box 2: Webapp Backend Logging widgets (y=12..18 inside box y=11, h=9)
        self.chk_webapp_enabled = Checkbox(
            y=12, x=4, label="Enable webapp file logging", checked=cur_w_enabled
        )
        self.sel_webapp_request_mode = SelectList(
            y=13,
            x=4,
            width=16,
            label="Request Logging",
            options=req_mode_opts,
            selected_index=idx_w_req_mode,
            label_width=20,
        )
        self.inp_webapp_dir = TextInput(
            y=14, x=4, width=26, label="Log Directory", value=cur_w_dir, label_width=20
        )
        self.inp_webapp_file = TextInput(
            y=15, x=4, width=26, label="Log File", value=cur_w_file, label_width=20
        )
        self.inp_webapp_error_dir = TextInput(
            y=16, x=4, width=26, label="Error Dir (opt)", value=cur_w_err_dir, label_width=20
        )
        self.inp_webapp_error_file = TextInput(
            y=17, x=4, width=26, label="Error File (opt)", value=cur_w_err_file, label_width=20
        )
        self.sel_webapp_level = SelectList(
            y=18, x=4, width=16, label="Log Level", options=level_opts, selected_index=idx_w_level, label_width=20
        )

        # Action buttons
        self.btn_save = Button(y=21, x=4, label="Save Settings", on_click=self.save)
        self.btn_cancel = Button(y=21, x=24, label="Cancel", on_click=self.cancel)

        # Status Bar
        self.status_bar = StatusBar(
            shortcuts=[
                ("Tab/Down", "Next"),
                ("Shift-Tab/Up", "Prev"),
                ("S", "Save"),
                ("F1/?", "Help"),
                ("Esc", "Back"),
            ]
        )

        self.widgets = [
            self.chk_parser_enabled,
            self.inp_parser_dir,
            self.inp_parser_file,
            self.inp_parser_error_dir,
            self.inp_parser_error_file,
            self.sel_parser_level,
            self.chk_webapp_enabled,
            self.sel_webapp_request_mode,
            self.inp_webapp_dir,
            self.inp_webapp_file,
            self.inp_webapp_error_dir,
            self.inp_webapp_error_file,
            self.sel_webapp_level,
            self.btn_save,
            self.btn_cancel,
        ]

        self.help_topic_for = {
            id(self.chk_parser_enabled): "parser_enabled",
            id(self.inp_parser_dir): "parser_dir",
            id(self.inp_parser_file): "parser_file",
            id(self.inp_parser_error_dir): "error_split",
            id(self.inp_parser_error_file): "error_split",
            id(self.sel_parser_level): "log_level",
            id(self.chk_webapp_enabled): "webapp_enabled",
            id(self.sel_webapp_request_mode): "request_mode",
            id(self.inp_webapp_dir): "webapp_dir",
            id(self.inp_webapp_file): "webapp_file",
            id(self.inp_webapp_error_dir): "error_split",
            id(self.inp_webapp_error_file): "error_split",
            id(self.sel_webapp_level): "log_level",
        }
        self.focus_widget(0)
        self.mark_form_clean()

    def current_help_topic(self) -> str:
        """Return the HELP_TOPICS key for the focused widget ('overview' when it has none)."""
        return self.help_topic_for.get(id(self.get_focused_widget()), "overview")

    def show_help_dialog(self) -> None:
        """Show the focused widget's help topic in a modal dialog."""
        title, paragraphs = HELP_TOPICS[self.current_help_topic()]
        ModalDialog(self.stdscr, title, "\n".join(paragraphs), ["OK"]).show()

    def _layout_widths(self, max_x: int) -> tuple[int, int]:
        """Return (form box width, help panel width); the panel width is 0 when it does not fit."""
        if max_x - 4 - 1 - _FORM_MIN_W < _HELP_MIN_W:
            return max(40, min(76, max_x - 4)), 0
        box_w = min(76, max(_FORM_MIN_W, max_x - 4 - 1 - 40))
        return box_w, max_x - 2 - box_w - 1 - 2

    def _draw_help_panel(self, y: int, x: int, h: int, w: int) -> None:
        """Draw the focused widget's help topic in a bordered panel."""
        title, paragraphs = HELP_TOPICS[self.current_help_topic()]
        draw_box(self.stdscr, y, x, h, w, title=title, color_pair=PAIR_BORDER)
        inner_h = h - 2
        lines = wrap_help(paragraphs, w - 4)
        if len(lines) > inner_h:
            lines = lines[: max(0, inner_h - 1)] + ["... F1: full text"]
        for i, line in enumerate(lines):
            attr = get_color(PAIR_MUTED) if line.startswith("... F1") else get_color(PAIR_DEFAULT)
            safe_addstr(self.stdscr, y + 1 + i, x + 2, line, attr)

    def _validate_path_fields(self) -> list[str]:
        """Validate all path/file inputs do not contain NUL bytes; return list of error messages."""
        errors: list[str] = []
        path_widgets = (
            ("Parser", self.inp_parser_dir),
            ("Parser", self.inp_parser_file),
            ("Parser", self.inp_parser_error_dir),
            ("Parser", self.inp_parser_error_file),
            ("Webapp", self.inp_webapp_dir),
            ("Webapp", self.inp_webapp_file),
            ("Webapp", self.inp_webapp_error_dir),
            ("Webapp", self.inp_webapp_error_file),
        )
        for section, widget in path_widgets:
            if "\x00" in widget.get_value():
                errors.append(f"{section} {widget.label}: path cannot contain NUL bytes")
        return errors

    def draw(self) -> None:
        """Render parser and webapp logging configuration screen."""
        self.clear()
        max_y, max_x = self.get_maxyx()

        # Header Title
        title = "Logging Configuration"
        safe_addstr(self.stdscr, 1, 2, title, get_color(PAIR_HEADER) | curses.A_BOLD)

        box_w, help_w = self._layout_widths(max_x)

        # Box 1: Parser Logging
        draw_box(self.stdscr, 3, 2, 8, box_w, title="Parser Logging", color_pair=PAIR_BORDER)

        # Box 2: Webapp Backend Logging
        draw_box(self.stdscr, 11, 2, 9, box_w, title="Webapp Backend Logging", color_pair=PAIR_BORDER)

        # Side help panel for the focused field (F1 shows it as a dialog when the terminal is too narrow)
        if help_w:
            self._draw_help_panel(3, 2 + box_w + 1, max(4, max_y - 4), help_w)

        # Render all interactive widgets
        for widget in self.widgets:
            if hasattr(widget, "draw"):
                widget.draw(self.stdscr)

        # Render bottom status bar
        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Handle screen navigation, shortcuts, and focus delegation."""
        # Tab and Down navigation
        if ch in (9, curses.KEY_DOWN):
            self.focus_next()
            return True

        # Shift-Tab and Up navigation
        if ch in (curses.KEY_UP, getattr(curses, "KEY_BTAB", 353), 353):
            self.focus_prev()
            return True

        # Escape cancels/exits
        if ch == 27:
            self.cancel()
            return True

        # Ctrl+S save hotkey
        if ch == 19:
            self.save()
            return True

        # F1 opens the focused field's help topic in a dialog
        if ch == getattr(curses, "KEY_F1", 265):
            self.show_help_dialog()
            return True

        # Delegate to focused widget first (handles typing in TextInput, Left/Right in SelectList, Space in Checkbox)
        focused = self.get_focused_widget()
        if focused is not None and hasattr(focused, "handle_key"):
            if focused.handle_key(ch):
                return True

        # Hotkey '?' when not editing a text input
        if ch == ord("?"):
            self.show_help_dialog()
            return True

        # Hotkey 'S' or 's' when not editing a text input
        if ch in (ord("s"), ord("S")):
            self.save()
            return True

        # Enter on save/cancel button
        if ch in (10, 13, curses.KEY_ENTER):
            if focused is self.btn_save:
                self.save()
                return True
            elif focused is self.btn_cancel:
                self.cancel()
                return True

        return False

    def save(self) -> bool:
        """Persist current logging widget values to app.config, set is_dirty = True, and pop screen."""
        errors = self._validate_path_fields()
        if errors:
            ModalDialog(
                self.stdscr,
                "Invalid Value",
                "Settings were not saved:\n" + "\n".join(errors),
                ["OK"],
            ).show()
            return False

        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            logging_cfg = self.app.config.get("logging")
            if not isinstance(logging_cfg, dict):
                logging_cfg = {}
                self.app.config["logging"] = logging_cfg
        else:
            logging_cfg = self.logging_cfg

        parser_cfg = logging_cfg.get("parser")
        if not isinstance(parser_cfg, dict):
            parser_cfg = {}
            logging_cfg["parser"] = parser_cfg

        webapp_cfg = logging_cfg.get("webapp")
        if not isinstance(webapp_cfg, dict):
            webapp_cfg = {}
            logging_cfg["webapp"] = webapp_cfg

        # Parser logging attributes
        parser_cfg["enabled"] = self.chk_parser_enabled.is_checked()
        parser_cfg["dir"] = self.inp_parser_dir.get_value().strip()
        parser_cfg["file"] = self.inp_parser_file.get_value().strip()
        parser_cfg["error_dir"] = self.inp_parser_error_dir.get_value().strip()
        parser_cfg["error_file"] = self.inp_parser_error_file.get_value().strip()
        parser_cfg["level"] = normalize_log_level(self.sel_parser_level.get_selected())

        # Webapp logging attributes
        webapp_cfg["enabled"] = self.chk_webapp_enabled.is_checked()
        webapp_cfg["request_mode"] = normalize_webapp_request_mode(self.sel_webapp_request_mode.get_selected())
        webapp_cfg["dir"] = self.inp_webapp_dir.get_value().strip()
        webapp_cfg["file"] = self.inp_webapp_file.get_value().strip()
        webapp_cfg["error_dir"] = self.inp_webapp_error_dir.get_value().strip()
        webapp_cfg["error_file"] = self.inp_webapp_error_file.get_value().strip()
        webapp_cfg["level"] = normalize_log_level(self.sel_webapp_level.get_selected())

        # Update references
        self.logging_cfg = logging_cfg
        self.parser_log_cfg = parser_cfg
        self.webapp_log_cfg = webapp_cfg

        # Mark application dirty and pop screen if supported
        if self.app is not None:
            if hasattr(self.app, "is_dirty"):
                self.app.is_dirty = True
            if hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
                self.app.pop_screen()
        self.mark_form_clean()
        return True

    def cancel(self) -> None:
        """Dismiss screen without persisting changes (confirming first when edits are unsaved)."""
        if not self.confirm_discard():
            return
        if self.app is not None and hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
            self.app.pop_screen()
