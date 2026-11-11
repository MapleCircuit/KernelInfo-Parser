"""core/tui/screens/repos.py - TUI screens for repository, git, branch, and tag rules management.

Provides:
- RepositoriesScreen: Master view listing all configured and detected repositories.
- RepoDetailScreen: Detail view editing project name, default branch, start ref, git URL,
  interactive branch selection with pattern filtering, and tag view regex rules.
"""

from __future__ import annotations

import copy
import curses
import os
from pathlib import Path
import re
from typing import Any, Callable, List, Optional, Set, Tuple

from core.config import (
    DEFAULT_TAG_VIEWS,
    REPO_ROOT,
    _merge_repositories,
    detect_repository,
    find_repository_branches,
    find_repository_dirs,
    get_repos_dir,
    is_git_repository,
    resolve_repo_path,
)
from core.tui.screens.base import BaseScreen
from core.tui.testing import DiagnosticResult, test_repository
from core.tui.theme import (
    PAIR_ACTIVE,
    PAIR_BORDER,
    PAIR_DEFAULT,
    PAIR_ERROR,
    PAIR_HEADER,
    PAIR_HIGHLIGHT,
    PAIR_MUTED,
    PAIR_SUCCESS,
    draw_box,
    get_color,
    safe_addstr,
)
from core.tui.widgets import (
    Button,
    ModalDialog,
    ScrollableList,
    StatusBar,
    TextInput,
)


def safe_modal_dialog(
    stdscr: Any,
    title: str,
    message: str,
    buttons: Optional[List[str]] = None,
    default_button: int = 0,
) -> Optional[str]:
    """Display ModalDialog safely, returning default button without hanging on empty mock keys."""
    dialog = ModalDialog(stdscr, title=title, message=message, buttons=buttons, default_button=default_button)
    # Check if mock curses window without queued keys to prevent infinite loops in tests
    if hasattr(stdscr, "keys_queue") and not getattr(stdscr, "keys_queue"):
        return dialog.buttons[dialog.selected_button_index]
    return dialog.show()


class ModalTextInputDialog:
    """Modal dialog with single-line text input field."""

    def __init__(
        self,
        stdscr: Any,
        title: str,
        prompt: str,
        default_val: str = "",
        buttons: Optional[List[str]] = None,
    ) -> None:
        self.stdscr = stdscr
        self.title = title
        self.prompt = prompt
        self.buttons = list(buttons) if buttons is not None else ["OK", "Cancel"]
        self.selected_button_idx = 0
        self.focus_button = False

        try:
            max_y, max_x = stdscr.getmaxyx()
        except Exception:
            max_y, max_x = 24, 80

        dialog_w = max(40, min(65, max_x - 6))
        dialog_h = 8
        dialog_y = max(0, (max_y - dialog_h) // 2)
        dialog_x = max(0, (max_x - dialog_w) // 2)

        self.y = dialog_y
        self.x = dialog_x
        self.h = dialog_h
        self.w = dialog_w

        input_w = max(10, dialog_w - 6)
        self.input_field = TextInput(
            y=dialog_y + 3,
            x=dialog_x + 3,
            width=input_w,
            label="",
            value=default_val,
        )
        self.input_field.is_focused = True

    def draw(self, win: Optional[Any] = None) -> None:
        target = win or self.stdscr
        # Clear dialog background
        for row in range(1, self.h - 1):
            safe_addstr(target, self.y + row, self.x + 1, " " * (self.w - 2), get_color(PAIR_DEFAULT))
        draw_box(target, self.y, self.x, self.h, self.w, title=self.title, color_pair=PAIR_ACTIVE, title_align="center")
        safe_addstr(target, self.y + 2, self.x + 3, self.prompt[: self.w - 6], get_color(PAIR_DEFAULT))
        self.input_field.draw(target)

        # Buttons
        btn_y = self.y + self.h - 2
        curr_x = self.x + 3
        for i, b in enumerate(self.buttons):
            s = f"[ {b} ]"
            is_active = self.focus_button and (i == self.selected_button_idx)
            attr = (get_color(PAIR_HIGHLIGHT) | curses.A_BOLD) if is_active else get_color(PAIR_MUTED)
            safe_addstr(target, btn_y, curr_x, s, attr)
            curr_x += len(s) + 2

    def handle_key(self, ch: int) -> Tuple[bool, Optional[str]]:
        """Process key input. Returns (is_done, result_string_or_none)."""
        if ch == 27:  # Esc
            return True, None
        if ch in (9,):  # Tab
            self.focus_button = not self.focus_button
            self.input_field.is_focused = not self.focus_button
            return False, None

        if self.focus_button:
            if ch in (curses.KEY_LEFT, ord("h")):
                self.selected_button_idx = (self.selected_button_idx - 1) % len(self.buttons)
                return False, None
            if ch in (curses.KEY_RIGHT, ord("l")):
                self.selected_button_idx = (self.selected_button_idx + 1) % len(self.buttons)
                return False, None
            if ch in (10, 13, curses.KEY_ENTER, ord(" ")):
                chosen = self.buttons[self.selected_button_idx]
                if chosen == "OK":
                    return True, self.input_field.value
                return True, None
        else:
            if ch in (10, 13, curses.KEY_ENTER):
                return True, self.input_field.value
            if ch in (curses.KEY_DOWN,):
                self.focus_button = True
                self.input_field.is_focused = False
                return False, None
            self.input_field.handle_key(ch)
            return False, None

        return False, None

    def show(self) -> Optional[str]:
        """Run modal text input loop."""
        if hasattr(self.stdscr, "keys_queue") and not getattr(self.stdscr, "keys_queue"):
            return self.input_field.value
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
                return None
            except Exception:
                return self.input_field.value

            if ch == -1:
                return self.input_field.value

            closed, val = self.handle_key(ch)
            if closed:
                return val


def _render_repo_item(repo: dict[str, Any]) -> str:
    """Format repository entry for ScrollableList display."""
    enabled_marker = "[X]" if repo.get("enabled", True) else "[ ]"
    name = repo.get("name") or "unnamed"
    path = repo.get("path") or ""
    default_branch = repo.get("default_branch") or "HEAD"
    branches = repo.get("branches") or []
    count = len(branches)
    return f"{enabled_marker} {name} ({path}) [default: {default_branch}, branches: {count}]"


class RepositoriesScreen(BaseScreen):
    """Master repository view for managing git repositories, paths, and presets."""

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)
        self.cfg = getattr(self.app, "config", {}) or {}
        self.repos_dir: str = str(self.cfg.get("workspace", {}).get("repos_dir", "repos"))
        self.repositories: list[dict[str, Any]] = list(self.cfg.get("repositories", []))
        self.last_test_result: Optional[DiagnosticResult] = None
        self._last_yx: Tuple[int, int] = (0, 0)

        # Detect git repositories if list is initially empty
        if not self.repositories:
            self._detect_workspace_repositories()

        max_y, max_x = self.get_maxyx()
        list_h = max(5, max_y - 6)
        list_w = max(40, max_x - 4)

        self.repos_list = ScrollableList(
            y=4,
            x=2,
            height=list_h,
            width=list_w,
            items=self.repositories,
            title="Registered Repositories",
            render_item_func=_render_repo_item,
        )
        self.repos_list.is_focused = True

        self.status_bar = StatusBar(
            shortcuts=[
                ("Space", "Toggle"),
                ("R", "Rescan"),
                ("A", "Add"),
                ("D", "Remove"),
                ("Enter", "Details"),
                ("T", "Test"),
                ("C", "Dir"),
                ("Esc", "Back"),
            ],
            y=max_y - 1,
        )

        self.widgets = [self.repos_list]
        self.focus_widget(0)
        self.repos_list.set_selected(int(self.recall_view(self.VIEW_KEY).get("selected", 0)))

    VIEW_KEY = "repositories"

    def _remember_selection(self) -> None:
        """Keep the highlighted repository row for the next time this menu opens."""
        self.remember_view(self.VIEW_KEY, selected=self.repos_list.get_selected_index())

    def on_leave(self) -> None:
        """Remember the highlighted repository when returning to the main menu."""
        self._remember_selection()

    def on_enter(self) -> None:
        """Synchronize repository list upon entering screen."""
        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            self.cfg = self.app.config
            if "repositories" in self.cfg:
                self.repositories = list(self.cfg["repositories"])
        self.repos_list.set_items(self.repositories)

    def _resolve_base_repos_path(self) -> Path:
        """Resolve current repos_dir path against REPO_ROOT if relative."""
        p = Path(self.repos_dir)
        return p if p.is_absolute() else REPO_ROOT / p

    def _detect_workspace_repositories(self) -> list[str]:
        """Scan workspace repos_dir for git repositories and merge with known entries."""
        base_path = self._resolve_base_repos_path()
        found_dirs = find_repository_dirs(base_path)
        merged, changes = _merge_repositories(self.repositories, found_dirs)
        self.repositories = merged
        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            self.app.config["repositories"] = self.repositories
        if changes and self.app is not None:
            self.app.is_dirty = True
        return changes

    def rescan_repositories(self) -> int:
        """Trigger interactive rescan of workspace repos_dir."""
        changes = self._detect_workspace_repositories()
        self.repos_list.set_items(self.repositories)
        if self.app is not None:
            self.app.is_dirty = True
        msg = f"Rescanned repos_dir: {len(changes)} changes detected ({len(self.repositories)} total)."
        self.status_bar.set_message(msg, color=PAIR_SUCCESS if changes else PAIR_MUTED)
        return len(changes)

    def toggle_selected_repo(self) -> bool:
        """Toggle enabled flag for the currently selected repository."""
        repo = self.repos_list.get_selected()
        if repo is None:
            return False
        repo["enabled"] = not repo.get("enabled", True)
        if self.app is not None:
            self.app.is_dirty = True
        state_str = "enabled" if repo["enabled"] else "disabled"
        self.status_bar.set_message(f"Repository '{repo.get('name')}' {state_str}.")
        return True

    def add_repository_by_path(self, path_str: str) -> bool:
        """Add git repository at path_str to configuration."""
        raw_path = path_str.strip()
        if not raw_path:
            return False

        p = Path(raw_path)
        base_path = self._resolve_base_repos_path()
        if not p.is_absolute():
            if (base_path / p).exists():
                resolved = base_path / p
            elif (REPO_ROOT / p).exists():
                resolved = REPO_ROOT / p
            else:
                resolved = p
        else:
            resolved = p

        if resolved.exists() and is_git_repository(resolved):
            repo_entry = detect_repository(resolved)
        else:
            name = p.name or "repo"
            repo_entry = {
                "name": name,
                "path": raw_path,
                "project": name,
                "default_branch": "master",
                "git_url": "",
                "root_commit": "",
                "start_ref": "HEAD",
                "enabled": True,
                "branches": [],
                "tag_views": copy.deepcopy(DEFAULT_TAG_VIEWS),
            }

        # Check for duplicate
        for existing in self.repositories:
            if existing.get("path") == repo_entry.get("path"):
                self.status_bar.set_message(f"Repository already added: '{repo_entry.get('name')}'", color=PAIR_ERROR)
                return False

        self.repositories.append(repo_entry)
        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            self.app.config["repositories"] = self.repositories
            self.app.is_dirty = True
        self.repos_list.set_items(self.repositories)
        self.repos_list.set_selected(len(self.repositories) - 1)
        self.status_bar.set_message(f"Added repository '{repo_entry.get('name')}'.", color=PAIR_SUCCESS)
        return True

    def prompt_add_repository(self) -> bool:
        """Prompt user for repository path and add it."""
        dialog = ModalTextInputDialog(
            self.stdscr,
            title="Add Repository",
            prompt="Enter filesystem path to Git repository:",
            default_val="",
        )
        path_str = dialog.show()
        if path_str:
            return self.add_repository_by_path(path_str)
        return False

    def remove_or_disable_selected(self, action: Optional[str] = None) -> bool:
        """Disable or remove currently selected repository."""
        repo = self.repos_list.get_selected()
        if repo is None:
            self.status_bar.set_message("No repository selected", color=PAIR_ERROR)
            return False

        name = repo.get("name") or "unnamed"
        is_enabled = repo.get("enabled", True)

        if action is None:
            btn_toggle = "Disable" if is_enabled else "Enable"
            action = safe_modal_dialog(
                self.stdscr,
                title="Manage Repository",
                message=f"Repository: '{name}'\nPath: {repo.get('path')}\n\nChoose an action:",
                buttons=[btn_toggle, "Remove", "Cancel"],
            )

        if action in ("Disable", "disable"):
            repo["enabled"] = False
            if self.app is not None:
                self.app.is_dirty = True
            self.status_bar.set_message(f"Disabled repository '{name}'.")
            return True
        elif action in ("Enable", "enable"):
            repo["enabled"] = True
            if self.app is not None:
                self.app.is_dirty = True
            self.status_bar.set_message(f"Enabled repository '{name}'.")
            return True
        elif action in ("Remove", "remove"):
            if repo in self.repositories:
                self.repositories.remove(repo)
                if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
                    self.app.config["repositories"] = self.repositories
                    self.app.is_dirty = True
                self.repos_list.set_items(self.repositories)
                self.status_bar.set_message(f"Removed repository '{name}'.", color=PAIR_SUCCESS)
                return True
        return False

    def change_repos_dir(self, new_dir: Optional[str] = None) -> bool:
        """Change workspace repos_dir path and offer rescan."""
        if new_dir is None:
            dialog = ModalTextInputDialog(
                self.stdscr,
                title="Change Repos Directory",
                prompt="Enter new workspace repos_dir path:",
                default_val=self.repos_dir,
            )
            new_dir = dialog.show()

        if new_dir and new_dir.strip():
            self.repos_dir = new_dir.strip()
            if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
                ws = self.app.config.setdefault("workspace", {})
                ws["repos_dir"] = self.repos_dir
                self.app.is_dirty = True
            self.rescan_repositories()
            self.status_bar.set_message(f"Updated workspace repos_dir: {self.repos_dir}", color=PAIR_SUCCESS)
            return True
        return False

    def test_selected_repository(self) -> Optional[DiagnosticResult]:
        """Run in-place validation test on selected repository."""
        repo = self.repos_list.get_selected()
        if repo is None:
            self.status_bar.set_message("No repository selected", color=PAIR_ERROR)
            return None

        diag = test_repository(repo, repos_dir=self.repos_dir)
        self.last_test_result = diag
        status_tag = "PASSED" if diag.passed else "FAILED"
        title = f"Test Result: {diag.name} [{status_tag}]"
        msg = f"Status: {status_tag}\nLatency: {diag.latency_ms:.1f}ms\nMessage: {diag.message}\n\nDetails:\n{diag.details}"
        safe_modal_dialog(self.stdscr, title=title, message=msg, buttons=["OK"])

        color = PAIR_SUCCESS if diag.passed else PAIR_ERROR
        self.status_bar.set_message(f"Test '{diag.name}': {diag.message}", color=color)
        return diag

    def open_selected_detail(self) -> Optional[RepoDetailScreen]:
        """Open detail screen for selected repository."""
        repo = self.repos_list.get_selected()
        if repo is None:
            return None

        self._remember_selection()
        detail = RepoDetailScreen(self.stdscr, app=self.app, repo_entry=repo)
        if self.app is not None and hasattr(self.app, "push_screen"):
            self.app.push_screen(detail)
        return detail

    def draw(self) -> None:
        """Render repository management master screen."""
        self.clear()
        max_y, max_x = self.get_maxyx()

        # Update geometry on terminal resize
        if (max_y, max_x) != self._last_yx:
            self._last_yx = (max_y, max_x)
            self.repos_list.height = max(5, max_y - 6)
            self.repos_list.width = max(40, max_x - 4)
            self.status_bar.y = max_y - 1

        # Title Header
        header_text = " Repository & Git Branch Management "
        self.safe_addstr(0, max(2, (max_x - len(header_text)) // 2), header_text, get_color(PAIR_HEADER) | curses.A_BOLD)

        # Workspace repos_dir display
        dir_text = f" Workspace repos_dir: {self.repos_dir}   [Press 'C' to change] "
        self.safe_addstr(2, 2, dir_text, get_color(PAIR_ACTIVE) | curses.A_BOLD)

        # Render list and status bar
        self.repos_list.draw(self.stdscr)
        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Handle screen hotkeys and widget navigation."""
        if ch == ord(" "):
            return self.toggle_selected_repo()

        if ch in (ord("R"), ord("r")):
            self.rescan_repositories()
            return True

        if ch in (ord("A"), ord("a")):
            self.prompt_add_repository()
            return True

        if ch in (ord("D"), ord("d")):
            return self.remove_or_disable_selected()

        if ch in (ord("T"), ord("t")):
            self.test_selected_repository()
            return True

        if ch in (ord("C"), ord("c")):
            self.change_repos_dir()
            return True

        if ch in (10, 13, curses.KEY_ENTER):
            self.open_selected_detail()
            return True

        if ch == 27:  # Esc
            if self.app is not None and hasattr(self.app, "pop_screen"):
                self.app.pop_screen()
            return True

        return super().handle_key(ch)


class RepoDetailScreen(BaseScreen):
    """Detail view for a repository entry: form fields, branch picker, and tag rules."""

    def __init__(self, stdscr: Any, app: Any = None, repo_entry: Optional[dict[str, Any]] = None) -> None:
        super().__init__(stdscr, app)
        self.repo_entry: dict[str, Any] = repo_entry if repo_entry is not None else {}
        self.last_test_result: Optional[DiagnosticResult] = None
        self._last_yx: Tuple[int, int] = (0, 0)

        # Initial field values
        # A saved key (an empty string included) wins; name / "master" only fill a missing key
        if "project" in self.repo_entry:
            project_val = str(self.repo_entry.get("project") or "")
        else:
            project_val = str(self.repo_entry.get("name") or "")
        if "default_branch" in self.repo_entry:
            default_b_val = str(self.repo_entry.get("default_branch") or "")
        else:
            default_b_val = "master"
        start_ref_val = str(self.repo_entry.get("start_ref") or "")
        git_url_val = str(self.repo_entry.get("git_url") or "")

        # Form fields
        self.project_input = TextInput(y=3, x=2, width=38, label="Project:        ", value=project_val)
        self.default_branch_input = TextInput(y=4, x=2, width=38, label="Default Branch: ", value=default_b_val)
        self.start_ref_input = TextInput(y=5, x=2, width=38, label="Start Ref:      ", value=start_ref_val)
        self.git_url_input = TextInput(y=6, x=2, width=38, label="Git URL:        ", value=git_url_val)

        # Detect branches from repo path or fall back to config
        self.all_branches = self._detect_repo_branches(default_b_val)
        existing_branches = self.repo_entry.get("branches")
        if existing_branches is not None:
            self.selected_branches: Set[str] = set(existing_branches)
        else:
            self.selected_branches = set(self.all_branches)

        self.visible_branches = list(self.all_branches)

        # Branch filter input
        self.branch_filter_input = TextInput(y=8, x=2, width=38, label="Branch Filter:  ", value="")

        # Branch list
        self.branch_list = ScrollableList(
            y=10,
            x=2,
            height=8,
            width=38,
            items=self.visible_branches,
            title="Branches ([Space] Toggle)",
            render_item_func=self._render_branch_item,
        )

        # Tag Views section
        self.tag_views: list[dict[str, Any]] = copy.deepcopy(
            self.repo_entry.get("tag_views") or DEFAULT_TAG_VIEWS
        )
        self.tag_view_rules = self._flatten_tag_views()
        self.tag_views_list = ScrollableList(
            y=10,
            x=42,
            height=8,
            width=36,
            items=self.tag_view_rules,
            title="Tag View Regex Rules",
            render_item_func=self._render_tag_view_item,
        )

        # Action Buttons
        self.save_btn = Button(y=19, x=2, label="Save (S)", on_click=self.save_changes)
        self.test_btn = Button(y=19, x=16, label="Test (T)", on_click=self.test_repo_settings)
        self.reset_tags_btn = Button(y=19, x=30, label="Reset Tags (R)", on_click=self.reset_tag_views_to_presets)
        self.cancel_btn = Button(y=19, x=51, label="Cancel (Esc)", on_click=self.on_esc)

        self.status_bar = StatusBar(
            shortcuts=[
                ("Tab", "Next"),
                ("Shift-Tab", "Prev"),
                ("↑/↓", "Row"),
                ("Space", "Toggle"),
                ("Enter/A/D", "Edit/Add/Del Rule"),
                ("T", "Test"),
                ("S", "Save"),
                ("R", "Reset Tags"),
                ("Esc", "Back"),
            ],
            y=23,
        )

        # Register widgets in focus navigation order
        self.widgets = [
            self.project_input,
            self.default_branch_input,
            self.start_ref_input,
            self.git_url_input,
            self.branch_filter_input,
            self.branch_list,
            self.tag_views_list,
            self.save_btn,
            self.test_btn,
            self.reset_tags_btn,
            self.cancel_btn,
        ]
        self.focus_widget(0)
        self._initial_snapshot = self._get_form_snapshot()
        self._restore_view_state()

    @property
    def view_key(self) -> str:
        """View-state key of this repository (its configured path, else its name)."""
        return f"repo_detail:{self.repo_entry.get('path') or self.repo_entry.get('name') or ''}"

    def _restore_view_state(self) -> None:
        """Re-apply the focused widget and highlighted branch / tag rule rows of the last visit."""
        state = self.recall_view(self.view_key)
        if not state:
            return
        self.branch_list.set_selected(int(state.get("branch_index", 0)))
        self.tag_views_list.set_selected(int(state.get("tag_rule_index", 0)))
        focus = int(state.get("focus_index", 0))
        if 0 <= focus < len(self.widgets) and isinstance(self.widgets[focus], ScrollableList):
            self.focus_widget(focus)

    def _remember_view_state(self) -> None:
        """Store the focused widget and highlighted rows for the next visit of this repository."""
        self.remember_view(
            self.view_key,
            focus_index=self.focused_widget_index,
            branch_index=self.branch_list.get_selected_index(),
            tag_rule_index=self.tag_views_list.get_selected_index(),
        )

    def _get_form_snapshot(self) -> dict[str, Any]:
        """Capture every editable value for unsaved-change detection."""
        return {
            "project": self.project_input.value.strip(),
            "default_branch": self.default_branch_input.value.strip(),
            "start_ref": self.start_ref_input.value.strip(),
            "git_url": self.git_url_input.value.strip(),
            "branches": sorted(self.selected_branches),
            "tag_views": copy.deepcopy(self.tag_views),
        }

    def has_unsaved_changes(self) -> bool:
        """Return True when the form differs from the values the screen opened with."""
        return self._get_form_snapshot() != self._initial_snapshot

    def _detect_repo_branches(self, default_branch: str) -> list[str]:
        """Detect branches for repository or return configured / default branches."""
        try:
            repo_path = resolve_repo_path(self.repo_entry)
            if repo_path.exists() and is_git_repository(repo_path):
                branches = find_repository_branches(repo_path, branch_filter=None, default_branch=default_branch)
                if branches:
                    return branches
        except Exception:
            pass

        # Fallback to configured branches or default branch
        configured = self.repo_entry.get("branches")
        if configured:
            return list(configured)
        return [default_branch] if default_branch else ["master"]

    def _flatten_tag_views(self) -> list[Tuple[str, str]]:
        """Flatten tag_views list into (view_name, regex_rule) pairs.

        Also records `self._tag_rule_locs`, the (view_index, rule_index) of each flattened row.
        """
        rules: list[Tuple[str, str]] = []
        locs: list[Tuple[int, int]] = []
        for v_idx, view in enumerate(self.tag_views):
            v_name = view.get("name", "all")
            for r_idx, rule in enumerate(view.get("regex_rules", [])):
                rules.append((v_name, rule))
                locs.append((v_idx, r_idx))
        self._tag_rule_locs = locs
        return rules

    def _refresh_tag_rules(self, select_index: Optional[int] = None) -> None:
        """Rebuild the flattened rule rows and optionally move the selection."""
        self.tag_view_rules = self._flatten_tag_views()
        self.tag_views_list.set_items(self.tag_view_rules)
        if select_index is not None:
            self.tag_views_list.set_selected(select_index)

    def _prompt_tag_rule(self, title: str, default_val: str) -> Optional[str]:
        """Prompt for a regex rule via ModalTextInputDialog; returns None when cancelled."""
        dialog = ModalTextInputDialog(self.stdscr, title=title, prompt="Regex rule:", default_val=default_val)
        return dialog.show()

    def _validate_tag_rule(self, rule: str) -> bool:
        """Return True for a non-empty compilable regex; report errors on the status bar."""
        if not rule:
            self.status_bar.set_message("Regex rule cannot be empty.", color=PAIR_ERROR)
            return False
        try:
            re.compile(rule)
        except re.error as exc:
            self.status_bar.set_message(f"Invalid regex: {exc}", color=PAIR_ERROR)
            return False
        return True

    def edit_selected_tag_rule(self, new_rule: Optional[str] = None) -> bool:
        """Replace the highlighted regex rule (prompts when `new_rule` is None)."""
        idx = self.tag_views_list.get_selected_index()
        if not self.tag_view_rules or not (0 <= idx < len(self._tag_rule_locs)):
            return False
        v_idx, r_idx = self._tag_rule_locs[idx]
        current = self.tag_views[v_idx]["regex_rules"][r_idx]
        if new_rule is None:
            new_rule = self._prompt_tag_rule("Edit Tag View Rule", current)
            if new_rule is None:
                return False
        new_rule = new_rule.strip()
        if not self._validate_tag_rule(new_rule):
            return False
        self.tag_views[v_idx]["regex_rules"][r_idx] = new_rule
        self._refresh_tag_rules(idx)
        self.status_bar.set_message("Tag view rule updated.", color=PAIR_SUCCESS)
        return True

    def add_tag_rule(self, rule: Optional[str] = None) -> bool:
        """Insert a regex rule after the highlighted one, in the same tag view (prompts when `rule` is None)."""
        if rule is None:
            rule = self._prompt_tag_rule("Add Tag View Rule", "")
            if rule is None:
                return False
        rule = rule.strip()
        if not self._validate_tag_rule(rule):
            return False

        idx = self.tag_views_list.get_selected_index()
        if self.tag_view_rules and 0 <= idx < len(self._tag_rule_locs):
            v_idx, r_idx = self._tag_rule_locs[idx]
            insert_at = r_idx + 1
        else:
            if not self.tag_views:
                self.tag_views.append({"name": copy.deepcopy(DEFAULT_TAG_VIEWS[0]["name"]), "regex_rules": []})
            v_idx = 0
            insert_at = len(self.tag_views[0].setdefault("regex_rules", []))
        self.tag_views[v_idx].setdefault("regex_rules", []).insert(insert_at, rule)
        self._refresh_tag_rules()
        self.tag_views_list.set_selected(self._tag_rule_locs.index((v_idx, insert_at)))
        self.status_bar.set_message("Tag view rule added.", color=PAIR_SUCCESS)
        return True

    def remove_selected_tag_rule(self) -> bool:
        """Delete the highlighted regex rule; a tag view left without rules is dropped.

        The last remaining rule cannot be deleted: an empty `tag_views` list means "use
        DEFAULT_TAG_VIEWS" to every reader, so removing it would bring the presets back.
        """
        idx = self.tag_views_list.get_selected_index()
        if not self.tag_view_rules or not (0 <= idx < len(self._tag_rule_locs)):
            return False
        if len(self._tag_rule_locs) == 1:
            self.status_bar.set_message(
                "Cannot delete the last rule (no rules = presets). Edit it with Enter instead.",
                color=PAIR_ERROR,
            )
            return False
        v_idx, r_idx = self._tag_rule_locs[idx]
        rules = self.tag_views[v_idx]["regex_rules"]
        rules.pop(r_idx)
        if not rules:
            self.tag_views.pop(v_idx)
        self._refresh_tag_rules(min(idx, max(0, len(self._tag_rule_locs) - 1)))
        self.status_bar.set_message("Tag view rule removed.", color=PAIR_SUCCESS)
        return True

    def _render_branch_item(self, branch: str) -> str:
        """Render branch item with toggle marker."""
        marker = "[X]" if branch in self.selected_branches else "[ ]"
        return f"{marker} {branch}"

    def _render_tag_view_item(self, item: Tuple[str, str]) -> str:
        """Render tag view regex rule."""
        v_name, rule = item
        return f"[{v_name}] {rule}"

    def filter_branches(self, pattern: str) -> None:
        """Filter visible branches matching regex or substring pattern."""
        raw_pat = pattern.strip()
        if not raw_pat:
            self.visible_branches = list(self.all_branches)
        else:
            matched: list[str] = []
            for b in self.all_branches:
                try:
                    if re.search(raw_pat, b, re.IGNORECASE):
                        matched.append(b)
                except re.error:
                    if raw_pat.lower() in b.lower():
                        matched.append(b)
            self.visible_branches = matched
        self.branch_list.set_items(self.visible_branches)

    def toggle_selected_branch(self) -> bool:
        """Toggle inclusion of currently highlighted branch."""
        b = self.branch_list.get_selected()
        if b is None:
            return False
        if b in self.selected_branches:
            self.selected_branches.remove(b)
        else:
            self.selected_branches.add(b)
        return True

    def toggle_all_visible_branches(self) -> None:
        """Toggle all currently visible branches."""
        all_checked = all(b in self.selected_branches for b in self.visible_branches)
        if all_checked:
            for b in self.visible_branches:
                self.selected_branches.discard(b)
        else:
            for b in self.visible_branches:
                self.selected_branches.add(b)

    def reset_tag_views_to_presets(self) -> None:
        """Reset tag views to Linux standard kernel presets."""
        self.tag_views = copy.deepcopy(DEFAULT_TAG_VIEWS)
        self._refresh_tag_rules()
        self.status_bar.set_message("Reset tag views to Linux standard kernel presets.", color=PAIR_SUCCESS)

    def save_changes(self) -> None:
        """Commit edits back into repo entry, set is_dirty, and return to caller."""
        self.repo_entry["project"] = self.project_input.value.strip()
        self.repo_entry["default_branch"] = self.default_branch_input.value.strip()
        self.repo_entry["start_ref"] = self.start_ref_input.value.strip()
        self.repo_entry["git_url"] = self.git_url_input.value.strip()
        self.repo_entry["branches"] = [b for b in self.all_branches if b in self.selected_branches]
        self.repo_entry["tag_views"] = copy.deepcopy(self.tag_views)
        self._initial_snapshot = self._get_form_snapshot()
        self._remember_view_state()

        if self.app is not None:
            self.app.is_dirty = True
            if hasattr(self.app, "pop_screen"):
                self.app.pop_screen()

    def discard_and_back(self) -> None:
        """Discard edits and return to caller."""
        if self.app is not None and hasattr(self.app, "pop_screen"):
            self.app.pop_screen()

    def on_esc(self) -> bool:
        """Return to the repository list, confirming first when edits are unsaved."""
        if self.has_unsaved_changes():
            choice = safe_modal_dialog(
                self.stdscr,
                title="Unsaved Changes",
                message="You have unsaved changes (press S to save them).\nDiscard changes and return?",
                buttons=["Discard", "Cancel"],
                default_button=1,
            )
            if choice != "Discard":
                return True
        self.discard_and_back()
        return True

    def test_repo_settings(self) -> DiagnosticResult:
        """Run validation test using current form settings."""
        candidate = dict(self.repo_entry)
        candidate["project"] = self.project_input.value.strip()
        candidate["default_branch"] = self.default_branch_input.value.strip()
        candidate["start_ref"] = self.start_ref_input.value.strip()
        candidate["git_url"] = self.git_url_input.value.strip()
        candidate["branches"] = [b for b in self.all_branches if b in self.selected_branches]

        repos_dir = None
        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            repos_dir = self.app.config.get("workspace", {}).get("repos_dir")

        diag = test_repository(candidate, repos_dir=repos_dir)
        self.last_test_result = diag

        status_tag = "PASSED" if diag.passed else "FAILED"
        title = f"Test Result: {diag.name} [{status_tag}]"
        msg = f"Status: {status_tag}\nLatency: {diag.latency_ms:.1f}ms\nMessage: {diag.message}\n\nDetails:\n{diag.details}"
        safe_modal_dialog(self.stdscr, title=title, message=msg, buttons=["OK"])

        color = PAIR_SUCCESS if diag.passed else PAIR_ERROR
        self.status_bar.set_message(f"Test '{diag.name}': {diag.message}", color=color)
        return diag

    def draw(self) -> None:
        """Render repository detail and edit screen."""
        self.clear()
        max_y, max_x = self.get_maxyx()

        # Dynamic layout adaptation on resize
        if (max_y, max_x) != self._last_yx:
            self._last_yx = (max_y, max_x)
            field_w = max(20, min(50, max_x - 22))
            self.project_input.width = field_w
            self.default_branch_input.width = field_w
            self.start_ref_input.width = field_w
            self.git_url_input.width = field_w
            self.branch_filter_input.width = field_w

            col_w = max(24, (max_x - 6) // 2)
            list_h = max(4, max_y - 15)

            self.branch_list.height = list_h
            self.branch_list.width = col_w
            self.branch_list.y = 10
            self.branch_list.x = 2

            self.tag_views_list.height = list_h
            self.tag_views_list.width = col_w
            self.tag_views_list.y = 10
            self.tag_views_list.x = 2 + col_w + 2

            btn_y = max(15, 10 + list_h + 1)
            self.save_btn.y = btn_y
            self.test_btn.y = btn_y
            self.reset_tags_btn.y = btn_y
            self.cancel_btn.y = btn_y

            self.status_bar.y = max_y - 1

        # Header Title
        repo_name = self.repo_entry.get("name") or "New Repository"
        repo_path = self.repo_entry.get("path") or ""
        header_text = f" Repository Detail: {repo_name} ({repo_path}) "
        self.safe_addstr(0, max(2, (max_x - len(header_text)) // 2), header_text, get_color(PAIR_HEADER) | curses.A_BOLD)

        # Draw all registered widgets
        for widget in self.widgets:
            if hasattr(widget, "draw"):
                widget.draw(self.stdscr)

        self._draw_selected_rule(max_y, max_x)
        self.status_bar.draw(self.stdscr)
        self.refresh()

    def selected_rule_lines(self, width: int, max_lines: int) -> list[str]:
        """Wrap the full highlighted tag rule (`Rule [view]: regex`) into at most `max_lines` rows."""
        item = self.tag_views_list.get_selected()
        if item is None or width <= 0 or max_lines <= 0:
            return []
        v_name, rule = item
        text = f"Rule [{v_name}]: {rule}"
        lines = [text[i : i + width] for i in range(0, len(text), width)] or [""]
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1][:-1] + "…"
        return lines

    def _draw_selected_rule(self, max_y: int, max_x: int) -> None:
        """Show the untruncated highlighted regex between the buttons and the status bar."""
        top = self.save_btn.y + 1
        rows = (max_y - 1) - top
        for i, line in enumerate(self.selected_rule_lines(max(1, max_x - 4), rows)):
            self.safe_addstr(top + i, 2, line, get_color(PAIR_MUTED))

    def handle_key(self, ch: int) -> bool:
        """Handle detail screen navigation and shortcut hotkeys."""
        if ch == 27:  # Esc
            return self.on_esc()

        if ch == 9:  # Tab
            self.focus_next()
            return True

        if ch in (curses.KEY_BTAB, 353):  # Shift-Tab
            self.focus_prev()
            return True

        focused = self.get_focused_widget()

        # Up/Down move between rows inside a list box; focus leaves only past the first/last row
        if isinstance(focused, ScrollableList) and ch in (curses.KEY_UP, curses.KEY_DOWN):
            idx = focused.get_selected_index()
            if ch == curses.KEY_UP:
                if focused.items and idx > 0:
                    focused.select_prev()
                else:
                    self.focus_prev()
            else:
                if focused.items and idx < len(focused.items) - 1:
                    focused.select_next()
                else:
                    self.focus_next()
            return True

        # Space toggles branch when branch_list is focused
        if focused is self.branch_list and ch == ord(" "):
            self.toggle_selected_branch()
            return True

        # Enter edits, A/Insert adds, D/Delete removes the highlighted tag view rule
        if focused is self.tag_views_list:
            if ch in (10, 13, curses.KEY_ENTER):
                self.edit_selected_tag_rule()
                return True
            if ch in (ord("a"), ord("A"), curses.KEY_IC):
                self.add_tag_rule()
                return True
            if ch in (ord("d"), ord("D"), curses.KEY_DC):
                self.remove_selected_tag_rule()
                return True

        # Enter or Space triggers focused Button
        if isinstance(focused, Button) and ch in (10, 13, curses.KEY_ENTER, ord(" ")):
            return focused.handle_key(ch)

        # Global shortcuts when NOT typing inside a TextInput
        if not isinstance(focused, TextInput):
            if ch in (ord("S"), ord("s")):
                self.save_changes()
                return True
            if ch in (ord("T"), ord("t")):
                self.test_repo_settings()
                return True
            if ch in (ord("R"), ord("r")):
                self.reset_tag_views_to_presets()
                return True
            if ch == curses.KEY_UP:
                self.focus_prev()
                return True
            if ch == curses.KEY_DOWN:
                self.focus_next()
                return True
        else:
            # Inside a TextInput
            if ch == 19:  # Ctrl+S
                self.save_changes()
                return True
            if ch == curses.KEY_UP:
                self.focus_prev()
                return True
            if ch == curses.KEY_DOWN:
                self.focus_next()
                return True
            if ch in (10, 13, curses.KEY_ENTER):
                if focused is self.branch_filter_input:
                    self.filter_branches(self.branch_filter_input.value)
                    self.focus_widget(self.widgets.index(self.branch_list))
                else:
                    self.focus_next()
                return True

        res = super().handle_key(ch)
        if focused is self.branch_filter_input and res:
            self.filter_branches(self.branch_filter_input.value)
        return res
