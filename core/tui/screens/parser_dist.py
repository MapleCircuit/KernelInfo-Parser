"""core/tui/screens/parser_dist.py - Parser and distributed coordination configuration screen."""

from __future__ import annotations

import curses
import textwrap
from typing import Any

from cache_engine import DEFAULT_ENGINE, UnknownCacheEngine, available_cache_engines, get_cache_engine
from core.config import MEMORY_MODES, _coerce_bool, normalize_memory_mode
from core.tui.screens.base import BaseScreen
from core.tui.theme import (
    PAIR_ACTIVE,
    PAIR_BORDER,
    PAIR_DEFAULT,
    PAIR_HEADER,
    PAIR_MUTED,
    get_color,
    safe_addstr,
    draw_box,
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
    "memory_mode": (
        "Memory Mode",
        [
            "Trades parse speed for lower peak RAM. CLI flags --low-mem / --very-low-mem override it.",
            "- normal: CPUs-1 workers, large batches, 500 ChangeSets cached. Fastest; needs the most RAM.",
            "- low: CPUs/2 workers, ~4x smaller batches, 50 ChangeSets cached, worker results compressed, "
            "memory reclaimed every 25 files.",
            "- very_low: at most 2 workers, smallest batches and epochs, 25 ChangeSets cached, AST views "
            "unpacked lazily. Slowest; for small machines.",
        ],
    ),
    "table_engine": ("Table Engine", [
            "How tables are held while parsing. CLI --te / env TABLE_ENGINE override it.",
            "- cached (default): preloads te_cached tables into RAM (HugePages when available) with in-memory "
            "indexes. Fastest; more RAM and a slower start.",
            "- direct: every lookup goes to the database. Little RAM, quick start, far more DB load. "
            "For tight RAM.",
        ]),
    "max_te_table": ("Max TE Table (MB)", [
            "Cached engine only. Caps the size of one te_cached table, measured from DB statistics at each start.",
            "A bigger table is not cached: its reads and writes go straight to the database, as in direct mode.",
            "0 = no cap. Lower it to keep huge tables out of RAM.",
        ]),
    "cache_engine": ("Cache Engine", [
            "Backend for local rebuildable caches (webapp git-derived indexes). Env KERNELINFO_CACHE_ENGINE overrides it.",
            "- ram (default): pure-Python in-memory cache; fastest, cleared on process exit.",
            "- sqlite: persistent SQLite files under Cache Directory; survives restarts.",
        ]),
    "cache_dir": ("Cache Directory", [
            "Base directory for directory-based cache engines (sqlite) and scope key for ram. "
            "Env KERNELINFO_CACHE_DIR overrides it.",
            "Leave empty to use ~/.cache/kernelinfo.",
        ]),
    "distributed": ("Distributed (TL;DR)", [
            "Several machines parse into one shared database (directly or via the SSH tunnel).",
            "- master: parses mainline in order, runs global jobs, posts other branches as tasks. One per network.",
            "- worker: leases ready tasks, parses them, exits when all work is done.",
            "- standalone / unchecked: one machine, no coordination.",
            "Node ID names this node (empty: role:host:pid). Delegate stable: the master hands stable chains (vX.Y.z) to "
            "workers.",
        ]),
    "overview": (
        "Help",
        [
            "Move to a field to see its help here (F1 opens it in a dialog).",
            "- Table Engine: how tables are held while parsing.",
            "- Memory Mode: speed vs. peak RAM.",
            "- Max TE Table: size cap for one cached table.",
            "- Cache Engine / Dir: backend and path for local rebuildable caches.",
            "- Distributed: split parsing across machines.",
        ],
    ),
}

# Columns the form itself needs (labels + fields + checkbox text) and the narrowest useful help panel.
_FORM_MIN_W = 52
_HELP_MIN_W = 30


def wrap_help(paragraphs: list[str], width: int) -> list[str]:
    """Wrap help paragraphs to `width`, separating paragraphs with a blank line; bullets get a hanging indent."""
    lines: list[str] = []
    width = max(8, width)
    for i, para in enumerate(paragraphs):
        if i and not para.startswith("- "):
            lines.append("")
        indent = "  " if para.startswith("- ") else ""
        lines.extend(textwrap.wrap(para, width, subsequent_indent=indent, break_on_hyphens=False) or [""])
    return lines


class ParserDistributedScreen(BaseScreen):
    """Configuration screen for Parser memory, caching, and distributed worker options.

    Attributes:
        app: Controller reference exposing .config, .is_dirty, and .pop_screen().
    """

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)

        # Retrieve or initialize config sections
        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            self.parser_cfg = self.app.config.setdefault("parser", {})
            self.cache_cfg = self.app.config.setdefault("cache", {})
        else:
            self.parser_cfg = {}
            self.cache_cfg = {}

        if not isinstance(self.parser_cfg, dict):
            self.parser_cfg = {}
        if not isinstance(self.cache_cfg, dict):
            self.cache_cfg = {}

        self.dist_cfg = self.parser_cfg.setdefault("distributed", {})
        if not isinstance(self.dist_cfg, dict):
            self.dist_cfg = {}

        # Initial values
        table_engine_opts = ["cached", "direct"]
        cur_te = str(self.parser_cfg.get("table_engine", "cached")).strip().lower()
        idx_te = table_engine_opts.index(cur_te) if cur_te in table_engine_opts else 0

        mem_mode_opts = list(MEMORY_MODES)
        cur_mm = normalize_memory_mode(self.parser_cfg.get("memory_mode", "normal"))
        idx_mm = mem_mode_opts.index(cur_mm) if cur_mm in mem_mode_opts else 0

        cur_mem_max = str(self.parser_cfg.get("mem_max", 60))

        hp_opts = ["auto", "on", "off"]
        cur_hp = str(self.parser_cfg.get("hugepages", "auto")).strip().lower()
        idx_hp = hp_opts.index(cur_hp) if cur_hp in hp_opts else 0

        cur_cache_max = str(self.parser_cfg.get("cache_max_table_mb", 0))

        cache_engine_opts = list(available_cache_engines()) or [DEFAULT_ENGINE]
        cur_ce_raw = str(self.cache_cfg.get("engine") or DEFAULT_ENGINE).strip().lower()
        try:
            cur_ce = get_cache_engine(cur_ce_raw).name.lower()
        except UnknownCacheEngine:
            cur_ce = DEFAULT_ENGINE
        idx_ce = cache_engine_opts.index(cur_ce) if cur_ce in cache_engine_opts else 0
        cur_cache_dir = str(self.cache_cfg.get("dir") or "")

        cur_dist_enabled = _coerce_bool(self.dist_cfg.get("enabled"), False)

        role_opts = ["standalone", "master", "worker"]
        cur_role = str(self._dist_value("role", "standalone")).strip().lower()
        idx_role = role_opts.index(cur_role) if cur_role in role_opts else 0

        cur_worker_id = str(self._dist_value("worker_id", "") or "")
        cur_delegate_stable = _coerce_bool(self._dist_value("delegate_stable", True), True)

        # Instantiate Parser widgets
        self.sel_table_engine = SelectList(
            y=4, x=4, width=16, label="Table Engine", options=table_engine_opts, selected_index=idx_te, label_width=20
        )
        self.sel_memory_mode = SelectList(
            y=5, x=4, width=16, label="Memory Mode", options=mem_mode_opts, selected_index=idx_mm, label_width=20
        )
        self.inp_mem_max = TextInput(
            y=6, x=4, width=12, label="RAM Ceiling (GB)", value=cur_mem_max, label_width=20
        )
        self.sel_hugepages = SelectList(
            y=7, x=4, width=16, label="HugePages Mode", options=hp_opts, selected_index=idx_hp, label_width=20
        )
        self.inp_cache_max_table_mb = TextInput(
            y=8, x=4, width=12, label="Max TE Table (MB)", value=cur_cache_max, label_width=20
        )

        # Instantiate Local Cache widgets
        self.sel_cache_engine = SelectList(
            y=11, x=4, width=16, label="Cache Engine", options=cache_engine_opts, selected_index=idx_ce, label_width=20
        )
        self.inp_cache_dir = TextInput(
            y=12, x=4, width=26, label="Cache Directory", value=cur_cache_dir, label_width=20
        )

        # Instantiate Distributed widgets
        self.chk_dist_enabled = Checkbox(
            y=15, x=4, label="Enable Distributed Worker Coordination", checked=cur_dist_enabled
        )
        self.sel_role = SelectList(
            y=16, x=4, width=16, label="Worker Role", options=role_opts, selected_index=idx_role, label_width=20
        )
        self.inp_worker_id = TextInput(
            y=17, x=4, width=24, label="Worker Node ID", value=cur_worker_id, label_width=20
        )
        self.chk_delegate_stable = Checkbox(
            y=18, x=4, label="Delegate stable branches to workers", checked=cur_delegate_stable
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
            self.sel_table_engine,
            self.sel_memory_mode,
            self.inp_mem_max,
            self.sel_hugepages,
            self.inp_cache_max_table_mb,
            self.sel_cache_engine,
            self.inp_cache_dir,
            self.chk_dist_enabled,
            self.sel_role,
            self.inp_worker_id,
            self.chk_delegate_stable,
            self.btn_save,
            self.btn_cancel,
        ]
        self.help_topic_for = {
            id(self.sel_table_engine): "table_engine",
            id(self.sel_memory_mode): "memory_mode",
            id(self.inp_cache_max_table_mb): "max_te_table",
            id(self.sel_cache_engine): "cache_engine",
            id(self.inp_cache_dir): "cache_dir",
            id(self.chk_dist_enabled): "distributed",
            id(self.sel_role): "distributed",
            id(self.inp_worker_id): "distributed",
            id(self.chk_delegate_stable): "distributed",
        }
        self.focus_widget(0)
        self.mark_form_clean()

    def current_help_topic(self) -> str:
        """Return the HELP_TOPICS key for the focused widget ("overview" when it has none)."""
        return self.help_topic_for.get(id(self.get_focused_widget()), "overview")

    def show_help_dialog(self) -> None:
        """Show the focused widget's help topic in a modal dialog (for terminals too narrow for the side panel)."""
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

    def _dist_value(self, key: str, default: Any) -> Any:
        """Return `parser.distributed[key]`, else the legacy flat `parser[key]`, else `default`.

        A key present in `parser.distributed` wins even when falsy (an empty `worker_id` is a saved value).
        """
        if key in self.dist_cfg:
            return self.dist_cfg[key]
        return self.parser_cfg.get(key, default)

    def _parse_int_fields(self) -> tuple[dict[str, int], list[str]]:
        """Parse the integer inputs; return ({config key: value}, [labels of invalid inputs])."""
        values: dict[str, int] = {}
        errors: list[str] = []
        for key, widget in (("mem_max", self.inp_mem_max), ("cache_max_table_mb", self.inp_cache_max_table_mb)):
            raw = widget.get_value().strip()
            try:
                num = int(raw)
            except (ValueError, TypeError):
                num = -1
            if num < 0:
                errors.append(f"{widget.label}: '{raw}' is not a whole number >= 0")
            else:
                values[key] = num
        return values, errors

    def draw(self) -> None:
        """Render parser and distributed configuration screen."""
        self.clear()
        max_y, max_x = self.get_maxyx()

        # Header Title
        title = "Parser & Distributed Worker Configuration"
        safe_addstr(self.stdscr, 1, 2, title, get_color(PAIR_HEADER) | curses.A_BOLD)

        box_w, help_w = self._layout_widths(max_x)

        # Box 1: Parser Configuration
        draw_box(self.stdscr, 3, 2, 7, box_w, title="Parser Engine Configuration", color_pair=PAIR_BORDER)

        # Box 2: Local Cache Configuration
        draw_box(self.stdscr, 10, 2, 4, box_w, title="Local Cache Configuration", color_pair=PAIR_BORDER)

        # Box 3: Distributed & Node Identification
        draw_box(self.stdscr, 14, 2, 6, box_w, title="Distributed & Node Identification", color_pair=PAIR_BORDER)

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

        # Enter on save button or fallback save
        if ch in (10, 13, curses.KEY_ENTER):
            if focused is self.btn_save:
                self.save()
                return True
            elif focused is self.btn_cancel:
                self.cancel()
                return True

        return False

    def save(self) -> bool:
        """Persist current widget values to app.config, set is_dirty = True, and pop screen.

        An invalid integer input leaves the configuration untouched, reports the field in a dialog,
        and keeps the screen open. Returns True when the values were saved.
        """
        int_values, errors = self._parse_int_fields()
        if errors:
            ModalDialog(
                self.stdscr,
                "Invalid Value",
                "Settings were not saved:\n" + "\n".join(errors),
                ["OK"],
            ).show()
            return False

        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            parser_cfg = self.app.config.setdefault("parser", {})
            cache_cfg = self.app.config.setdefault("cache", {})
        else:
            parser_cfg = self.parser_cfg
            cache_cfg = self.cache_cfg

        dist_cfg = parser_cfg.setdefault("distributed", {})

        # Parser engine attributes
        parser_cfg["table_engine"] = self.sel_table_engine.get_selected()
        parser_cfg["memory_mode"] = self.sel_memory_mode.get_selected()
        parser_cfg["hugepages"] = self.sel_hugepages.get_selected()
        parser_cfg.update(int_values)

        # Local cache attributes
        cache_cfg["engine"] = self.sel_cache_engine.get_selected()
        cache_cfg["dir"] = self.inp_cache_dir.get_value().strip()

        # Distributed & Node attributes
        dist_cfg["enabled"] = self.chk_dist_enabled.is_checked()
        dist_cfg["role"] = self.sel_role.get_selected()
        dist_cfg["worker_id"] = self.inp_worker_id.get_value().strip()
        dist_cfg["delegate_stable"] = self.chk_delegate_stable.is_checked()

        # Update references
        self.parser_cfg = parser_cfg
        self.cache_cfg = cache_cfg
        self.dist_cfg = dist_cfg

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
