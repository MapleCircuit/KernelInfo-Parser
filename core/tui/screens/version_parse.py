"""core/tui/screens/version_parse.py - Parse specific versions / tags of one repository.

The user picks a configured Git repository and one or more release tags, then the TUI exits into
the parser pipeline with `repo`, `ref`, `single_version` (and `include_pre_releases` when the
pipeline continues through later releases) applied as CLI overrides.
"""

from __future__ import annotations

import curses
import fnmatch
from typing import Any

from core.config import _coerce_bool, resolve_repo_path
from core.tui.git_ops import PRE_RELEASE_RE, is_git_repo, list_version_tags
from core.tui.screens.base import BaseScreen
from core.tui.screens.launch import RUN_SAVED, choose_launch_config, launch_pipeline
from core.tui.screens.repos import safe_modal_dialog
from core.tui.theme import (
    PAIR_ERROR,
    PAIR_HEADER,
    PAIR_MUTED,
    PAIR_SUCCESS,
    get_color,
)
from core.tui.widgets import (
    Button,
    Checkbox,
    ScrollableList,
    SelectList,
    StatusBar,
    TextInput,
)

VIEW_KEY = "version_parse"
DEFAULT_HIGHLIGHT_TAG = "v3.0"
MIN_H, MIN_W = 14, 50
CONFIRM_TAG_LIMIT = 10
ENTER_KEYS = (10, 13, curses.KEY_ENTER)
SHIFT_TAB_KEYS = (getattr(curses, "KEY_BTAB", 353), 353)


def repo_label(entry: dict[str, Any]) -> str:
    """Identifier of a repositories[] entry handed to `--repo` (its name, else its path)."""
    return str(entry.get("name") or entry.get("path") or "")


def tag_matches(tag: str, pattern: str) -> bool:
    """Case-insensitive filter: glob when `pattern` holds `*` / `?`, else substring."""
    pat = pattern.strip().lower()
    if not pat:
        return True
    if "*" in pat or "?" in pat:
        return fnmatch.fnmatchcase(tag.lower(), pat)
    return pat in tag.lower()


def format_tag_list(tags: list[str], limit: int = CONFIRM_TAG_LIMIT) -> str:
    """Comma-separated tags, the first `limit` then `... (+N more)`."""
    shown = ", ".join(tags[:limit])
    return f"{shown}, ... (+{len(tags) - limit} more)" if len(tags) > limit else shown


class VersionParseScreen(BaseScreen):
    """Tag selector that launches the parser pipeline on exactly the chosen release tags.

    Keys: Tab / Shift-Tab move focus; in the tag list Space / Enter toggle the highlighted tag,
    `a` toggles every visible tag, `n` / `c` clear the selection; `P` (outside the filter field)
    or Enter on [Parse] launches; Esc goes back. Typing in the filter field refilters live.
    """

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)
        self.repos: list[dict[str, Any]] = self._git_repositories()
        self.repo_index: int = 0
        self._tag_cache: dict[str, list[str]] = {}
        self.all_tags: list[str] = []
        self.visible_tags: list[str] = []
        self.selected: set[str] = set()

        self.repo_select = SelectList(
            y=2, x=2, width=40, label="Repository", label_width=10,
            options=[repo_label(r) for r in self.repos] or ["(none)"],
            on_change=lambda idx, _name: self.select_repo(idx),
        )
        self.filter_input = TextInput(y=3, x=2, width=40, label="Filter", label_width=10)
        self.tag_list = ScrollableList(
            y=5, x=2, height=6, width=60, items=[],
            title="Tags ([Space] Toggle  [a] All visible  [n] None)",
            render_item_func=self._render_tag,
        )
        self.rc_checkbox = Checkbox(y=11, x=2, label="Include -rc / pre-release tags", checked=True)
        self.continue_checkbox = Checkbox(y=12, x=2, label="Continue through later releases", checked=False)
        self.parse_btn = Button(y=14, x=2, label="Parse (P)", on_click=self.parse)
        self.back_btn = Button(y=14, x=18, label="Back (Esc)", on_click=self.go_back)
        self.status_bar = StatusBar(
            shortcuts=[
                ("Tab", "Next"),
                ("Space", "Toggle"),
                ("a", "All visible"),
                ("n", "None"),
                ("P", "Parse"),
                ("Esc", "Back"),
            ],
        )

        if not self.repos:
            self.widgets = [self.back_btn]
            self.focus_widget(0)
            return

        self.widgets = [
            *([self.repo_select] if len(self.repos) > 1 else []),
            self.filter_input,
            self.tag_list,
            self.rc_checkbox,
            self.continue_checkbox,
            self.parse_btn,
            self.back_btn,
        ]
        state = self.recall_view(VIEW_KEY)
        names = [repo_label(r) for r in self.repos]
        start = names.index(state["repo"]) if state.get("repo") in names else 0
        self.filter_input.set_value(str(state.get("filter") or ""))
        self.filter_input.cursor_pos = len(self.filter_input.value)
        self.repo_select.set_options(names, start)
        self.select_repo(start, highlight=state.get("highlight"))
        self.focus_widget(self.widgets.index(self.tag_list))

    # --- Repository / tag model ---

    def _git_repositories(self) -> list[dict[str, Any]]:
        """Enabled `repositories[]` entries of the in-memory config whose path is a Git repository."""
        config = getattr(self.app, "config", None) or {}
        found: list[dict[str, Any]] = []
        for entry in config.get("repositories") or []:
            if not isinstance(entry, dict) or not _coerce_bool(entry.get("enabled"), True):
                continue
            if not repo_label(entry):
                continue
            try:
                is_repo = is_git_repo(resolve_repo_path(entry))
            except (OSError, ValueError, TypeError):
                is_repo = False
            if is_repo:
                found.append(entry)
        return found

    @property
    def current_repo(self) -> dict[str, Any] | None:
        """The repositories[] entry whose tags are listed, or None without Git repositories."""
        if 0 <= self.repo_index < len(self.repos):
            return self.repos[self.repo_index]
        return None

    def _load_tags(self, entry: dict[str, Any]) -> list[str]:
        """Version-sorted release tags of `entry` (pre-releases included), cached per repository path."""
        path = resolve_repo_path(entry)
        key = str(path)
        if key not in self._tag_cache:
            self.show_busy("Loading tags...")
            self._tag_cache[key] = list_version_tags(path, include_pre_releases=True)
        return self._tag_cache[key]

    def select_repo(self, index: int, highlight: str | None = None) -> None:
        """Show the tags of repository `index`, clearing the selection; highlights `highlight` or v3.0."""
        if not self.repos:
            return
        self.repo_index = max(0, min(index, len(self.repos) - 1))
        if self.repo_select.get_selected_index() != self.repo_index:
            self.repo_select.selected_index = self.repo_index
        self.all_tags = self._load_tags(self.repos[self.repo_index])
        self.selected = set()
        self.apply_filter(keep_highlight=False)
        target = highlight if highlight in self.visible_tags else DEFAULT_HIGHLIGHT_TAG
        if target in self.visible_tags:
            self.tag_list.set_selected(self.visible_tags.index(target))
        else:
            self.tag_list.set_selected(0)

    def apply_filter(self, keep_highlight: bool = True) -> None:
        """Recompute the visible tags from the filter text and the pre-release checkbox."""
        current = self.tag_list.get_selected() if keep_highlight else None
        pattern = self.filter_input.get_value()
        include_rc = self.rc_checkbox.is_checked()
        self.visible_tags = [
            t for t in self.all_tags
            if (include_rc or not PRE_RELEASE_RE.search(t)) and tag_matches(t, pattern)
        ]
        self.tag_list.set_items(self.visible_tags, keep_selection=keep_highlight)
        if current in self.visible_tags:
            self.tag_list.set_selected(self.visible_tags.index(current))

    def _render_tag(self, tag: str) -> str:
        return f"[{'x' if tag in self.selected else ' '}] {tag}"

    def selected_tags(self) -> list[str]:
        """Checked tags in version order."""
        return [t for t in self.all_tags if t in self.selected]

    def toggle_highlighted(self) -> bool:
        """Toggle the highlighted tag; False when the list is empty."""
        tag = self.tag_list.get_selected()
        if tag is None:
            return False
        self.selected.symmetric_difference_update({tag})
        return True

    def toggle_all_visible(self) -> None:
        """Check every visible tag, or uncheck them all when all are already checked."""
        if self.visible_tags and all(t in self.selected for t in self.visible_tags):
            self.selected.difference_update(self.visible_tags)
        else:
            self.selected.update(self.visible_tags)

    def clear_selection(self) -> None:
        """Uncheck every tag (hidden ones included)."""
        self.selected.clear()

    def continues(self) -> bool:
        """True when the pipeline continues through later releases (one tag + checkbox on)."""
        return len(self.selected) == 1 and self.continue_checkbox.is_checked()

    def build_launch_args(self) -> dict[str, Any]:
        """CLI overrides for the selected repository and tags."""
        entry = self.current_repo or {}
        args: dict[str, Any] = {
            "repo": repo_label(entry),
            "ref": self.selected_tags(),
            "single_version": not self.continues(),
        }
        if self.continues():
            args["include_pre_releases"] = self.rc_checkbox.is_checked()
        return args

    def mode_description(self) -> str:
        """Human-readable parse mode of the current selection."""
        tags = self.selected_tags()
        if self.continues():
            rc = "including" if self.rc_checkbox.is_checked() else "excluding"
            return f"Continue: {tags[0]} then every later release ({rc} pre-releases)"
        if len(tags) == 1:
            return f"Single version: {tags[0]} only"
        return f"Exactly these {len(tags)} tags, in version order"

    # --- Actions ---

    def _remember_view_state(self) -> None:
        if not self.repos:
            return
        self.remember_view(
            VIEW_KEY,
            repo=repo_label(self.current_repo or {}),
            filter=self.filter_input.get_value(),
            highlight=self.tag_list.get_selected(),
        )

    def on_leave(self) -> None:
        self._remember_view_state()

    def go_back(self) -> bool:
        """Return to the previous screen."""
        self._remember_view_state()
        if self.app is not None and hasattr(self.app, "pop_screen"):
            self.app.pop_screen()
        return True

    def _repo_in_saved_config(self, name: str) -> bool:
        """True when `name` is a repositories[] entry of the saved config (or it cannot be checked)."""
        disk_config = getattr(self.app, "disk_config", None)
        if not callable(disk_config):
            return True
        saved = disk_config().get("repositories") or []
        return any(isinstance(r, dict) and name in (r.get("name"), r.get("path")) for r in saved)

    def parse(self) -> bool:
        """Confirm, resolve the configuration to run with, and launch the pipeline on the selected tags."""
        tags = self.selected_tags()
        if self.current_repo is None:
            return False
        if not tags:
            safe_modal_dialog(self.stdscr, "No Tags Selected",
                              "Select at least one tag (Space toggles, [a] toggles all visible).", ["OK"])
            return False
        args = self.build_launch_args()
        message = (
            f"Repository: {args['repo']}\n"
            f"Tags ({len(tags)}): {format_tag_list(tags)}\n"
            f"Mode: {self.mode_description()}\n\n"
            f"Exit the TUI and run the parser?"
        )
        if safe_modal_dialog(self.stdscr, "Parse Tags", message, ["Parse", "Cancel"], 0) != "Parse":
            self.status_bar.set_message("Parse cancelled.", color=PAIR_MUTED)
            return False
        mode = choose_launch_config(self.stdscr, self.app, action="Parse")
        if mode is None:
            self.status_bar.set_message("Parse cancelled.", color=PAIR_MUTED)
            return False
        if mode == RUN_SAVED and not self._repo_in_saved_config(args["repo"]):
            safe_modal_dialog(
                self.stdscr, "Repository Not Saved",
                f"'{args['repo']}' is not in the saved config.json.\nChoose 'Save & Parse' to include it.",
                ["OK"],
            )
            return False
        self._remember_view_state()
        return launch_pipeline(self.stdscr, self.app, mode, **args)

    # --- Rendering ---

    def _layout(self, max_y: int, max_x: int) -> None:
        field_w = max(16, min(50, max_x - 15))
        self.repo_select.width = field_w
        self.filter_input.width = field_w
        list_h = max(3, max_y - 10)
        self.tag_list.y, self.tag_list.x = 5, 2
        self.tag_list.height, self.tag_list.width = list_h, max(20, max_x - 4)
        bottom = 5 + list_h
        self.rc_checkbox.y = bottom
        self.continue_checkbox.y = bottom + 1
        self.parse_btn.y = self.back_btn.y = bottom + 3
        self.back_btn.x = self.parse_btn.x + len(self.parse_btn.label) + 6
        self.status_bar.y = max_y - 1

    def summary_line(self, width: int) -> str:
        """`N selected: tags...` truncated to `width`."""
        tags = self.selected_tags()
        text = f"{len(tags)} selected" + (f": {', '.join(tags)}" if tags else "")
        return text if len(text) <= width else text[: max(0, width - 1)] + "…"

    def draw(self) -> None:
        """Render the repository picker, tag filter, tag checklist, options and buttons."""
        self.clear()
        max_y, max_x = self.get_maxyx()
        if max_y < MIN_H or max_x < MIN_W:
            self.safe_addstr(0, 0, f"Terminal window too small (min {MIN_W}x{MIN_H}).", get_color(PAIR_ERROR))
            self.refresh()
            return

        header = " Parse Specific Versions / Tags "
        self.safe_addstr(0, max(2, (max_x - len(header)) // 2), header, get_color(PAIR_HEADER) | curses.A_BOLD)

        if not self.repos:
            msg = "No git repositories configured — use the Git Repository Manager or Repositories screen."
            self.safe_addstr(2, 2, msg[: max_x - 4], get_color(PAIR_ERROR))
            self.status_bar.set_shortcuts([("Esc", "Back")])
            self.status_bar.y = max_y - 1
            self.status_bar.draw(self.stdscr)
            self.refresh()
            return

        self._layout(max_y, max_x)
        n_sel = len(self.selected)
        base = "Continue through later releases"
        self.continue_checkbox.label = base if n_sel <= 1 else f"{base} (n/a: {n_sel} tags selected)"
        if len(self.repos) > 1:
            self.repo_select.draw(self.stdscr)
        else:
            self.safe_addstr(2, 2, f"Repository: {repo_label(self.current_repo or {})}"[: max_x - 4])
        self.filter_input.draw(self.stdscr)
        hint_x = self.filter_input.x + 11 + self.filter_input.width + 1
        if max_x - hint_x > 12:
            self.safe_addstr(3, hint_x, "substring or glob (* ?)"[: max_x - hint_x - 1], get_color(PAIR_MUTED))
        self.tag_list.title = f"Tags {len(self.visible_tags)}/{len(self.all_tags)} ([Space] Toggle [a] All [n] None)"
        self.tag_list.draw(self.stdscr)
        if not self.visible_tags:
            empty = "(no release tags found)" if not self.all_tags else "(no tags match the filter)"
            self.safe_addstr(self.tag_list.y + 1, self.tag_list.x + 2, empty, get_color(PAIR_MUTED))
        for widget in (self.rc_checkbox, self.continue_checkbox, self.parse_btn, self.back_btn):
            widget.draw(self.stdscr)
        summary_attr = get_color(PAIR_SUCCESS) if n_sel else get_color(PAIR_MUTED)
        self.safe_addstr(self.continue_checkbox.y + 1, 2, self.summary_line(max_x - 4), summary_attr)
        self.status_bar.draw(self.stdscr)
        self.refresh()

    # --- Input ---

    def handle_key(self, ch: int) -> bool:
        """Focus navigation, tag toggling shortcuts, filter editing and launch."""
        if ch == 27:
            return self.go_back()
        if ch == 9:
            self.focus_next()
            return True
        if ch in SHIFT_TAB_KEYS:
            self.focus_prev()
            return True
        if not self.repos:
            return False

        focused = self.get_focused_widget()

        if focused is self.tag_list:
            if ch in (curses.KEY_UP, curses.KEY_DOWN):
                idx = self.tag_list.get_selected_index()
                last = len(self.tag_list.items) - 1
                if ch == curses.KEY_UP and self.tag_list.items and idx > 0:
                    self.tag_list.select_prev()
                elif ch == curses.KEY_DOWN and self.tag_list.items and idx < last:
                    self.tag_list.select_next()
                elif ch == curses.KEY_UP:
                    self.focus_prev()
                else:
                    self.focus_next()
                return True
            if ch in (ord(" "), *ENTER_KEYS):
                self.toggle_highlighted()
                return True
            if ch in (ord("a"), ord("A")):
                self.toggle_all_visible()
                return True
            if ch in (ord("n"), ord("N"), ord("c"), ord("C")):
                self.clear_selection()
                return True

        if focused is self.filter_input:
            if ch in (curses.KEY_UP, curses.KEY_DOWN):
                if ch == curses.KEY_UP:
                    self.focus_prev()
                else:
                    self.focus_next()
                return True
            if ch in ENTER_KEYS:
                self.focus_widget(self.widgets.index(self.tag_list))
                return True
            consumed = self.filter_input.handle_key(ch)
            if consumed:
                self.apply_filter()
            return consumed

        if ch in (ord("P"), ord("p")):
            self.parse()
            return True
        if ch == curses.KEY_UP:
            self.focus_prev()
            return True
        if ch == curses.KEY_DOWN:
            self.focus_next()
            return True

        consumed = super().handle_key(ch)
        if consumed and focused is self.rc_checkbox:
            self.apply_filter()
        return consumed
