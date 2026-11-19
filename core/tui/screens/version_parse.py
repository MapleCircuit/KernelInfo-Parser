"""core/tui/screens/version_parse.py - Parse and pre-parse version/tag configuration screen.

The user picks a configured Git repository and can:
1. Select one or more release tags for immediate parsing (with `repo`, `ref`, `single_version`,
   and optional `include_pre_releases` passed as CLI overrides).
2. Independently configure per-feature pre-parsing rules (`insights` and `api`) via mode/group
   presets (All Main Tags, Latest N Main, Main + Stable, Stable Tags, RC Tags, Main + Stable + RC,
   Custom) and per-tag explicit toggles, persisted to `entry["preparse"]`.
"""

from __future__ import annotations

import copy
import curses
import fnmatch
from pathlib import Path
from typing import Any

from core.config import (
    PREPARSE_FEATURES,
    _coerce_bool,
    normalize_preparse_spec,
    prune_default_preparse_overrides,
    repository_project,
    resolve_preparse_tags,
    resolve_repo_path,
    resolve_repo_preparse,
)
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
from parser.git_ast.release_order import parse_tag, release_key

VIEW_KEY = "version_parse"
DEFAULT_HIGHLIGHT_TAG = "v3.0"
MIN_H, MIN_W = 14, 50
CONFIRM_TAG_LIMIT = 10
ENTER_KEYS = (10, 13, curses.KEY_ENTER)
SHIFT_TAB_KEYS = (getattr(curses, "KEY_BTAB", 353), 353)

PREPARSE_PRESETS: list[tuple[str, dict[str, Any]]] = [
    ("All Main Tags", {"mode": "all_main", "include_main": True, "include_stable": False, "include_rc": False}),
    ("Latest N Main", {"mode": "latest_n_main", "include_main": True, "include_stable": False, "include_rc": False}),
    ("Main + Stable", {"mode": "groups", "include_main": True, "include_stable": True, "include_rc": False}),
    ("Stable Tags", {"mode": "groups", "include_main": False, "include_stable": True, "include_rc": False}),
    ("RC Tags", {"mode": "groups", "include_main": False, "include_stable": False, "include_rc": True}),
    ("Main + Stable + RC", {"mode": "groups", "include_main": True, "include_stable": True, "include_rc": True}),
    ("Custom", {"mode": "custom", "include_main": False, "include_stable": False, "include_rc": False}),
]
PREPARSE_PRESET_LABELS = [label for label, _ in PREPARSE_PRESETS]


def _preset_index_for_spec(spec: dict[str, Any]) -> int:
    mode = spec.get("mode", "all_main")
    if mode == "all_main":
        return 0
    if mode == "latest_n_main":
        return 1
    if mode == "custom":
        return 6
    flags = (
        bool(spec.get("include_main")),
        bool(spec.get("include_stable")),
        bool(spec.get("include_rc")),
    )
    flag_map = {
        (True, True, False): 2,
        (False, True, False): 3,
        (False, False, True): 4,
        (True, True, True): 5,
    }
    return flag_map.get(flags, 2)


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
    """Shared screen to select tags for immediate parsing and configure Insights/API pre-parsing.

    Keys: Tab / Shift-Tab move focus; in the tag list Space / Enter toggle the highlighted tag for
    Parse, `i` toggles Insights pre-parse, `k` toggles API pre-parse, `a` toggles every visible tag
    for Parse, `n` / `c` clear Parse selection; `P` launches Parse; `S` saves pre-parse config;
    Esc goes back. Typing in filter or N fields edits live.
    """

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)
        self.repos: list[dict[str, Any]] = self._git_repositories()
        self.repo_index: int = 0
        self._tag_cache: dict[str, list[str]] = {}
        self.all_tags: list[str] = []
        self.visible_tags: list[str] = []
        self.selected: set[str] = set()
        self.preparse_specs: dict[str, dict[str, Any]] = {}
        self._preparse_resolved: dict[str, set[str]] = {f: set() for f in PREPARSE_FEATURES}
        self._syncing_preparse_widgets = False

        self.repo_select = SelectList(
            y=2, x=2, width=40, label="Repository", label_width=10,
            options=[repo_label(r) for r in self.repos] or ["(none)"],
            on_change=lambda idx, _name: self.select_repo(idx),
        )
        self.filter_input = TextInput(y=3, x=2, width=40, label="Filter", label_width=10)
        self.insights_mode_select = SelectList(
            y=4, x=2, width=26, label="Insights", label_width=10,
            options=PREPARSE_PRESET_LABELS,
            on_change=lambda idx, _name: self._on_preset_change("insights", idx),
        )
        self.insights_n_input = TextInput(y=4, x=42, width=6, label="N", value="10", max_len=4, label_width=2)
        self.api_mode_select = SelectList(
            y=5, x=2, width=26, label="API", label_width=10,
            options=PREPARSE_PRESET_LABELS,
            on_change=lambda idx, _name: self._on_preset_change("api", idx),
        )
        self.api_n_input = TextInput(y=5, x=42, width=6, label="N", value="10", max_len=4, label_width=2)

        self.tag_list = ScrollableList(
            y=6, x=2, height=6, width=60, items=[],
            title="Tags ([Space] Parse  [i] Ins  [k] API  [a] All  [n] None)",
            render_item_func=self._render_tag,
        )
        self.rc_checkbox = Checkbox(y=12, x=2, label="Include -rc / pre-release tags", checked=True)
        self.continue_checkbox = Checkbox(y=13, x=2, label="Continue through later releases", checked=False)
        self.preparse_insights_cb = Checkbox(y=14, x=2, label="Insights (Subsystems)", checked=True)
        self.preparse_stable_cb = Checkbox(y=14, x=28, label="Stable deltas", checked=True)
        self.preparse_api_cb = Checkbox(y=14, x=47, label="KAPI", checked=True)
        self._summary_y: int | None = 15
        self.parse_btn = Button(y=16, x=2, label="Parse (P)", on_click=self.parse)
        self.preparse_btn = Button(y=16, x=17, label="Pre-parse (X)", on_click=self.run_preparse)
        self.save_btn = Button(y=16, x=36, label="Save Pre-parse (S)", on_click=self.save_preparse)
        self.back_btn = Button(y=16, x=60, label="Back (Esc)", on_click=self.go_back)
        self.status_bar = StatusBar(
            shortcuts=[
                ("Tab", "Next"),
                ("Space", "Parse"),
                ("i", "Ins"),
                ("k", "API"),
                ("a", "All"),
                ("S", "Save"),
                ("P", "Run"),
                ("X", "Pre-parse"),
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
            self.insights_mode_select,
            self.insights_n_input,
            self.api_mode_select,
            self.api_n_input,
            self.tag_list,
            self.rc_checkbox,
            self.continue_checkbox,
            self.preparse_insights_cb,
            self.preparse_stable_cb,
            self.preparse_api_cb,
            self.parse_btn,
            self.preparse_btn,
            self.save_btn,
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

    def _sync_preparse_widgets(self) -> None:
        """Update mode SelectLists and N TextInputs from `self.preparse_specs`."""
        self._syncing_preparse_widgets = True
        try:
            ins = self.preparse_specs.get("insights", {})
            api = self.preparse_specs.get("api", {})
            self.insights_mode_select.selected_index = _preset_index_for_spec(ins)
            self.insights_n_input.set_value(str(ins.get("latest_n", 10)))
            self.api_mode_select.selected_index = _preset_index_for_spec(api)
            self.api_n_input.set_value(str(api.get("latest_n", 10)))
        finally:
            self._syncing_preparse_widgets = False

    def _refresh_preparse_resolved(self) -> None:
        """Recompute resolved tag sets for both `insights` and `api` against `self.all_tags`."""
        dummy_entry = {"preparse": self.preparse_specs}
        for feat in PREPARSE_FEATURES:
            self._preparse_resolved[feat] = set(resolve_preparse_tags(dummy_entry, feat, self.all_tags))

    def _persist_preparse_to_repo(self) -> None:
        """Write pruned `preparse` overrides to `self.current_repo` and mark app dirty if changed."""
        repo = self.current_repo
        if repo is None:
            return
        old_val = copy.deepcopy(repo.get("preparse"))
        pruned = prune_default_preparse_overrides(repository_project(repo), self.preparse_specs)
        if pruned:
            repo["preparse"] = pruned
        else:
            repo.pop("preparse", None)
        if repo.get("preparse") != old_val and self.app is not None:
            self.app.is_dirty = True

    def _on_preset_change(self, feature: str, preset_idx: int) -> None:
        """Apply a mode/group preset for `feature` (`insights` or `api`)."""
        if self._syncing_preparse_widgets or not (0 <= preset_idx < len(PREPARSE_PRESETS)):
            return
        _, updates = PREPARSE_PRESETS[preset_idx]
        spec = self.preparse_specs.setdefault(feature, normalize_preparse_spec(None))
        spec.update(updates)
        self.preparse_specs[feature] = normalize_preparse_spec(spec)
        self._refresh_preparse_resolved()
        self._persist_preparse_to_repo()

    def _on_n_input_change(self, feature: str, widget: TextInput) -> None:
        """Update `latest_n` for `feature` when the user edits its N field."""
        raw = widget.get_value().strip()
        if not raw.isdigit():
            return
        val = max(1, int(raw))
        spec = self.preparse_specs.setdefault(feature, normalize_preparse_spec(None))
        if spec.get("latest_n") != val:
            spec["latest_n"] = val
            self._refresh_preparse_resolved()
            self._persist_preparse_to_repo()

    def preparse_tags(self, feature: str) -> list[str]:
        """Resolved pre-parse tags for `feature` in version order."""
        resolved = self._preparse_resolved.get(feature, set())
        return [t for t in self.all_tags if t in resolved]

    def toggle_preparse_tag(self, feature: str, tag: str | None = None) -> bool:
        """Toggle `tag` (or the highlighted tag) for `feature` (`insights` or `api`)."""
        target = tag if tag is not None else self.tag_list.get_selected()
        if target is None or feature not in PREPARSE_FEATURES:
            return False
        spec = self.preparse_specs.setdefault(feature, normalize_preparse_spec(None))
        versions = list(spec.get("versions") or [])
        resolved = self._preparse_resolved.get(feature, set())
        if target in versions:
            versions = [v for v in versions if v != target]
            spec["versions"] = versions
        elif target in resolved:
            # Already included by mode/group rule: switch to custom without `target` so toggling unchecks it.
            remaining = [t for t in self.all_tags if t in resolved and t != target]
            spec["mode"] = "custom"
            spec["include_main"] = False
            spec["include_stable"] = False
            spec["include_rc"] = False
            spec["versions"] = remaining
            self._sync_preparse_widgets()
        else:
            versions.append(target)
            spec["versions"] = versions
        self.preparse_specs[feature] = normalize_preparse_spec(spec)
        self._refresh_preparse_resolved()
        self._persist_preparse_to_repo()
        return True

    def save_preparse(self) -> bool:
        """Persist current pre-parse rules to the repository entry and save config if supported."""
        if self.current_repo is None:
            return False
        self._persist_preparse_to_repo()
        save_fn = getattr(self.app, "save_config", None)
        if callable(save_fn):
            ok, msg = save_fn()
            self.status_bar.set_message(msg, color=PAIR_SUCCESS if ok else PAIR_ERROR)
            return bool(ok)
        self.status_bar.set_message("Pre-parse configuration updated.", color=PAIR_SUCCESS)
        return True

    def select_repo(self, index: int, highlight: str | None = None) -> None:
        """Show the tags of repository `index`, clearing Parse selection; highlights `highlight` or v3.0."""
        if not self.repos:
            return
        self.repo_index = max(0, min(index, len(self.repos) - 1))
        if self.repo_select.get_selected_index() != self.repo_index:
            self.repo_select.selected_index = self.repo_index
        self.all_tags = self._load_tags(self.repos[self.repo_index])
        self.selected = set()
        self.preparse_specs = resolve_repo_preparse(self.repos[self.repo_index])
        self._sync_preparse_widgets()
        self._refresh_preparse_resolved()
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

    def _preparse_mark(self, feature: str, tag: str) -> str:
        spec = self.preparse_specs.get(feature, {})
        if tag in (spec.get("versions") or ()):
            return "x"
        if tag in self._preparse_resolved.get(feature, set()):
            return "G"
        return " "

    def _render_tag(self, tag: str) -> str:
        parse_mark = "x" if tag in self.selected else " "
        ins_mark = self._preparse_mark("insights", tag)
        api_mark = self._preparse_mark("api", tag)
        return f"[{parse_mark}] {tag:<22} Ins:[{ins_mark}] API:[{api_mark}]"

    def selected_tags(self) -> list[str]:
        """Checked tags in version order."""
        return [t for t in self.all_tags if t in self.selected]

    def toggle_highlighted(self) -> bool:
        """Toggle the highlighted tag for immediate Parse; False when the list is empty."""
        tag = self.tag_list.get_selected()
        if tag is None:
            return False
        self.selected.symmetric_difference_update({tag})
        return True

    def toggle_all_visible(self) -> None:
        """Check every visible tag for Parse, or uncheck them all when all are already checked."""
        if self.visible_tags and all(t in self.selected for t in self.visible_tags):
            self.selected.difference_update(self.visible_tags)
        else:
            self.selected.update(self.visible_tags)

    def clear_selection(self) -> None:
        """Uncheck every Parse tag (hidden ones included)."""
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

    def preparse_target_tags(self) -> list[str]:
        """Target tags for pre-parsing: checked tags in list, else configured pre-parse rules."""
        checked = self.selected_tags()
        if checked:
            return checked
        include_insights = self.preparse_insights_cb.is_checked()
        include_stable = self.preparse_stable_cb.is_checked()
        include_api = self.preparse_api_cb.is_checked()
        if not (include_insights or include_stable or include_api):
            return []

        wanted: set[str] = set()
        if include_insights:
            wanted.update(self.preparse_tags("insights"))
        if include_api:
            wanted.update(self.preparse_tags("api"))
        if include_stable and ((not include_insights and not include_api) or not wanted):
            stable = [
                t for t in self.all_tags
                if (parsed := parse_tag(t)) is not None and parsed["kind"] == "stable"
            ]
            wanted.update(stable or self.preparse_tags("insights"))
        return sorted(wanted, key=release_key)

    def build_preparse_launch_args(self) -> dict[str, Any]:
        """CLI overrides for unified multi-core pre-parsing of selected modes and tags."""
        entry = self.current_repo or {}
        return {
            "repo": repo_label(entry),
            "preparse": True,
            "preparse_insights": self.preparse_insights_cb.is_checked(),
            "preparse_stable": self.preparse_stable_cb.is_checked(),
            "preparse_api": self.preparse_api_cb.is_checked(),
            "preparse_tags": self.preparse_target_tags(),
        }

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
            cfg_name = Path(str(getattr(self.app, "config_path", "config.json"))).name
            safe_modal_dialog(
                self.stdscr, "Repository Not Saved",
                f"'{args['repo']}' is not in the saved {cfg_name}.\nChoose 'Save & Parse' to include it.",
                ["OK"],
            )
            return False
        self._remember_view_state()
        return launch_pipeline(self.stdscr, self.app, mode, **args)

    def run_preparse(self) -> bool:
        """Confirm, resolve configuration choice, and launch unified pre-parsing."""
        if self.current_repo is None:
            return False
        args = self.build_preparse_launch_args()
        targets = [
            label
            for enabled, label in (
                (args["preparse_insights"], "Insights (Subsystems)"),
                (args["preparse_stable"], "Stable deltas"),
                (args["preparse_api"], "KAPI"),
            )
            if enabled
        ]
        if not targets:
            safe_modal_dialog(
                self.stdscr,
                "No Pre-Parse Target",
                "Select at least one pre-parse target (Insights, Stable deltas, or KAPI).",
                ["OK"],
            )
            return False
        tags = args["preparse_tags"]
        if not tags:
            safe_modal_dialog(
                self.stdscr,
                "No Tags Selected",
                "No tags matched the selected pre-parse rules (or check tags in the list).",
                ["OK"],
            )
            return False
        source_note = "Checked tags in list" if self.selected_tags() else "Configured pre-parse rules"
        message = (
            f"Repository: {args['repo']}\n"
            f"Targets: {', '.join(targets)}\n"
            f"Tags ({len(tags)}, {source_note}): {format_tag_list(tags)}\n\n"
            f"Exit the TUI and run unified pre-parsing across all CPU cores?"
        )
        if safe_modal_dialog(
            self.stdscr,
            "Pre-parse Main / Insights / KAPI",
            message,
            ["Pre-parse", "Cancel"],
            0,
        ) != "Pre-parse":
            self.status_bar.set_message("Pre-parse cancelled.", color=PAIR_MUTED)
            return False
        mode = choose_launch_config(self.stdscr, self.app, action="Pre-parse")
        if mode is None:
            self.status_bar.set_message("Pre-parse cancelled.", color=PAIR_MUTED)
            return False
        if mode == RUN_SAVED and not self._repo_in_saved_config(args["repo"]):
            cfg_name = Path(str(getattr(self.app, "config_path", "config.json"))).name
            safe_modal_dialog(
                self.stdscr,
                "Repository Not Saved",
                f"'{args['repo']}' is not in the saved {cfg_name}.\nChoose 'Save & Pre-parse' to include it.",
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
        mode_w = max(16, min(28, max_x - 28))
        n_x = min(max_x - 10, 14 + mode_w + 2)
        self.insights_mode_select.y, self.insights_mode_select.x, self.insights_mode_select.width = 4, 2, mode_w
        self.insights_n_input.y, self.insights_n_input.x = 4, n_x
        self.api_mode_select.y, self.api_mode_select.x, self.api_mode_select.width = 5, 2, mode_w
        self.api_n_input.y, self.api_n_input.x = 5, n_x

        list_h = max(3, max_y - 14)
        self.tag_list.y, self.tag_list.x = 6, 2
        self.tag_list.height, self.tag_list.width = list_h, max(20, max_x - 4)
        bottom = 6 + list_h
        self.rc_checkbox.y = bottom
        self.continue_checkbox.y = bottom + 1

        if max_y >= 16:
            self._summary_y = bottom + 2
            cb_y = bottom + 3
            if max_x >= 60:
                self.preparse_insights_cb.y = self.preparse_stable_cb.y = self.preparse_api_cb.y = cb_y
                self.preparse_insights_cb.x = 2
                self.preparse_stable_cb.x = 28
                self.preparse_api_cb.x = 47
                cb_rows = 1
            else:
                self.preparse_insights_cb.y, self.preparse_insights_cb.x = cb_y, 2
                self.preparse_api_cb.y, self.preparse_api_cb.x = cb_y, 29
                self.preparse_stable_cb.y, self.preparse_stable_cb.x = cb_y + 1, 2
                cb_rows = 2
            btn_y = cb_y + cb_rows + (1 if cb_y + cb_rows + 2 < max_y else 0)
        elif max_y == 15:
            self._summary_y = bottom + 2
            cb_y = bottom + 3
            self.preparse_insights_cb.y = self.preparse_stable_cb.y = self.preparse_api_cb.y = cb_y
            self.preparse_insights_cb.x, self.preparse_stable_cb.x, self.preparse_api_cb.x = 2, 27, 44
            btn_y = bottom + 4
        else:
            self._summary_y = None
            cb_y = bottom + 2
            self.preparse_insights_cb.y = self.preparse_stable_cb.y = self.preparse_api_cb.y = cb_y
            self.preparse_insights_cb.x, self.preparse_stable_cb.x, self.preparse_api_cb.x = 2, 27, 44
            btn_y = bottom + 3

        if max_x >= 76 or btn_y + 1 >= max_y - 1:
            self.parse_btn.y = self.preparse_btn.y = self.save_btn.y = self.back_btn.y = btn_y
            gap = 2 if max_x >= 76 else 1
            self.parse_btn.x = 2
            self.preparse_btn.x = self.parse_btn.x + len(self.parse_btn.label) + 4 + gap
            self.save_btn.x = self.preparse_btn.x + len(self.preparse_btn.label) + 4 + gap
            self.back_btn.x = self.save_btn.x + len(self.save_btn.label) + 4 + gap
        else:
            self.parse_btn.y = self.preparse_btn.y = btn_y
            self.save_btn.y = self.back_btn.y = btn_y + 1
            self.parse_btn.x = 2
            self.preparse_btn.x = self.parse_btn.x + len(self.parse_btn.label) + 6
            self.save_btn.x = 2
            self.back_btn.x = self.save_btn.x + len(self.save_btn.label) + 6
        self.status_bar.y = max_y - 1

    def summary_line(self, width: int) -> str:
        """`N selected: tags... | Ins: M  API: K` truncated to `width`."""
        tags = self.selected_tags()
        n_ins = len(self._preparse_resolved.get("insights", set()))
        n_api = len(self._preparse_resolved.get("api", set()))
        parse_part = f"{len(tags)} selected" + (f": {', '.join(tags)}" if tags else "")
        text = f"{parse_part}  |  Ins: {n_ins}  API: {n_api}"
        return text if len(text) <= width else text[: max(0, width - 1)] + "…"

    def draw(self) -> None:
        """Render the repository picker, filter, pre-parse controls, tag checklist, options and buttons."""
        self.clear()
        max_y, max_x = self.get_maxyx()
        if max_y < MIN_H or max_x < MIN_W:
            self.safe_addstr(0, 0, f"Terminal window too small (min {MIN_W}x{MIN_H}).", get_color(PAIR_ERROR))
            self.refresh()
            return

        header = " Parse & Pre-Parse Versions / Tags "
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

        self.insights_mode_select.draw(self.stdscr)
        self.insights_n_input.draw(self.stdscr)
        self.api_mode_select.draw(self.stdscr)
        self.api_n_input.draw(self.stdscr)

        self.tag_list.title = (
            f"Tags {len(self.visible_tags)}/{len(self.all_tags)} "
            "([Space] Parse [i] Ins [k] API [a] All [n] None)"
        )
        self.tag_list.draw(self.stdscr)
        if not self.visible_tags:
            empty = "(no release tags found)" if not self.all_tags else "(no tags match the filter)"
            self.safe_addstr(self.tag_list.y + 1, self.tag_list.x + 2, empty, get_color(PAIR_MUTED))
        for widget in (
            self.rc_checkbox,
            self.continue_checkbox,
            self.preparse_insights_cb,
            self.preparse_stable_cb,
            self.preparse_api_cb,
            self.parse_btn,
            self.preparse_btn,
            self.save_btn,
            self.back_btn,
        ):
            widget.draw(self.stdscr)
        if self._summary_y is not None:
            summary_attr = get_color(PAIR_SUCCESS) if n_sel else get_color(PAIR_MUTED)
            self.safe_addstr(self._summary_y, 2, self.summary_line(max_x - 4), summary_attr)
        self.status_bar.draw(self.stdscr)
        self.refresh()

    # --- Input ---

    def handle_key(self, ch: int) -> bool:
        """Focus navigation, tag toggling shortcuts, pre-parse controls, filter editing and launch."""
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
            if ch in (ord("i"), ord("I")):
                self.toggle_preparse_tag("insights")
                return True
            if ch in (ord("k"), ord("K")):
                self.toggle_preparse_tag("api")
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

        if focused in (self.insights_n_input, self.api_n_input):
            if ch in (curses.KEY_UP, curses.KEY_DOWN):
                if ch == curses.KEY_UP:
                    self.focus_prev()
                else:
                    self.focus_next()
                return True
            if ch in ENTER_KEYS:
                self.focus_next()
                return True
            consumed = focused.handle_key(ch)
            if consumed:
                feat = "insights" if focused is self.insights_n_input else "api"
                self._on_n_input_change(feat, focused)
            return consumed

        if ch in (ord("P"), ord("p")):
            self.parse()
            return True
        if ch in (ord("X"), ord("x")):
            self.run_preparse()
            return True
        if ch in (ord("S"), ord("s")):
            self.save_preparse()
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
