"""tests/test_repos_screen.py - Unit tests for RepositoriesScreen and RepoDetailScreen."""

import copy
import curses
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.config import DEFAULT_TAG_VIEWS
from core.tui.screens.repos import (
    ModalTextInputDialog,
    RepoDetailScreen,
    RepositoriesScreen,
    _render_repo_item,
    safe_modal_dialog,
)
from core.tui.testing import DiagnosticResult
from tests.test_tui import MockCursesWindow


class MockApp:
    """Mock application controller managing config, screens stack, and dirty state."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config if config is not None else {}
        self.is_dirty = False
        self.screen_stack: list[object] = []
        self.popped = False

    def push_screen(self, screen: object) -> None:
        self.screen_stack.append(screen)

    def pop_screen(self) -> object | None:
        self.popped = True
        return self.screen_stack.pop() if self.screen_stack else None


class TestRepositoriesScreen(unittest.TestCase):
    """Unit tests for RepositoriesScreen master view."""

    def setUp(self) -> None:
        self.win = MockCursesWindow(24, 80)
        self.initial_config = {
            "workspace": {"repos_dir": "test_repos"},
            "repositories": [
                {
                    "name": "linux-mainline",
                    "path": "repos/linux-mainline",
                    "project": "linux",
                    "default_branch": "master",
                    "git_url": "git://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
                    "root_commit": "1da177e4c3f41524e886b7f1a8a0c1fc7321cac2",
                    "start_ref": "v3.0",
                    "enabled": True,
                    "branches": ["master", "linux-3.0.y", "linux-3.2.y"],
                    "tag_views": copy.deepcopy(DEFAULT_TAG_VIEWS),
                },
                {
                    "name": "linux-stable",
                    "path": "repos/linux-stable",
                    "project": "linux-stable",
                    "default_branch": "linux-3.0.y",
                    "git_url": "git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
                    "root_commit": "1da177e4c3f41524e886b7f1a8a0c1fc7321cac2",
                    "start_ref": "HEAD",
                    "enabled": False,
                    "branches": ["linux-3.0.y"],
                    "tag_views": copy.deepcopy(DEFAULT_TAG_VIEWS),
                },
            ],
        }
        self.app = MockApp(self.initial_config)

    def test_init_and_render_repo_item(self) -> None:
        """Verify initialization from app.config and item formatting."""
        screen = RepositoriesScreen(self.win, self.app)
        self.assertEqual(len(screen.repositories), 2)
        self.assertEqual(screen.repos_dir, "test_repos")

        # Check item rendering format
        rendered_0 = _render_repo_item(screen.repositories[0])
        self.assertEqual(rendered_0, "[X] linux-mainline (repos/linux-mainline) [default: master, branches: 3]")

        rendered_1 = _render_repo_item(screen.repositories[1])
        self.assertEqual(rendered_1, "[ ] linux-stable (repos/linux-stable) [default: linux-3.0.y, branches: 1]")

    def test_space_toggles_enabled_state(self) -> None:
        """Pressing Space toggles enabled state of highlighted repository and marks app dirty."""
        screen = RepositoriesScreen(self.win, self.app)
        self.assertTrue(screen.repositories[0]["enabled"])
        self.assertFalse(self.app.is_dirty)

        # Toggle first repo to False
        handled = screen.handle_key(ord(" "))
        self.assertTrue(handled)
        self.assertFalse(screen.repositories[0]["enabled"])
        self.assertTrue(self.app.is_dirty)

        # Toggle again back to True
        handled = screen.handle_key(ord(" "))
        self.assertTrue(handled)
        self.assertTrue(screen.repositories[0]["enabled"])

    def test_rescan_repositories(self) -> None:
        """R key triggers rescan of repos_dir."""
        screen = RepositoriesScreen(self.win, self.app)
        with patch.object(screen, "_detect_workspace_repositories", return_value=["added 'new-repo'"]):
            self.app.is_dirty = False
            handled = screen.handle_key(ord("r"))
            self.assertTrue(handled)
            self.assertTrue(self.app.is_dirty)
            self.assertIn("Rescanned repos_dir", screen.status_bar.message)

    def test_add_repository_by_path(self) -> None:
        """A key or add_repository_by_path adds a new entry and sets dirty."""
        screen = RepositoriesScreen(self.win, self.app)
        initial_count = len(screen.repositories)

        # Add unique repo
        added = screen.add_repository_by_path("/tmp/custom_kernel_repo")
        self.assertTrue(added)
        self.assertEqual(len(screen.repositories), initial_count + 1)
        self.assertEqual(screen.repositories[-1]["name"], "custom_kernel_repo")
        self.assertTrue(self.app.is_dirty)

        # Adding same repo again should be rejected as duplicate
        added_duplicate = screen.add_repository_by_path("/tmp/custom_kernel_repo")
        self.assertFalse(added_duplicate)
        self.assertEqual(len(screen.repositories), initial_count + 1)

    def test_prompt_add_repository(self) -> None:
        """Prompt add repository with MockCursesWindow."""
        screen = RepositoriesScreen(self.win, self.app)
        with patch("core.tui.screens.repos.ModalTextInputDialog.show", return_value="/var/git/my_repo"):
            handled = screen.handle_key(ord("a"))
            self.assertTrue(handled)
            self.assertTrue(any(r["path"] == "/var/git/my_repo" for r in screen.repositories))

    def test_remove_or_disable_selected(self) -> None:
        """D key disables or removes selected repository."""
        screen = RepositoriesScreen(self.win, self.app)
        screen.repos_list.set_selected(0)
        self.assertTrue(screen.repositories[0]["enabled"])

        # Explicit action 'Disable'
        success = screen.remove_or_disable_selected(action="Disable")
        self.assertTrue(success)
        self.assertFalse(screen.repositories[0]["enabled"])
        self.assertTrue(self.app.is_dirty)

        # Explicit action 'Enable'
        success = screen.remove_or_disable_selected(action="Enable")
        self.assertTrue(success)
        self.assertTrue(screen.repositories[0]["enabled"])

        # Explicit action 'Remove'
        self.app.is_dirty = False
        repo_to_remove = screen.repositories[0]
        success = screen.remove_or_disable_selected(action="Remove")
        self.assertTrue(success)
        self.assertNotIn(repo_to_remove, screen.repositories)
        self.assertTrue(self.app.is_dirty)

    def test_change_repos_dir(self) -> None:
        """C key prompts to change workspace repos_dir."""
        screen = RepositoriesScreen(self.win, self.app)
        with patch("core.tui.screens.repos.ModalTextInputDialog.show", return_value="custom_repos_dir"):
            handled = screen.handle_key(ord("c"))
            self.assertTrue(handled)
            self.assertEqual(screen.repos_dir, "custom_repos_dir")
            self.assertEqual(self.app.config["workspace"]["repos_dir"], "custom_repos_dir")
            self.assertTrue(self.app.is_dirty)

    def test_test_selected_repository(self) -> None:
        """T key invokes test_repository diagnostics."""
        screen = RepositoriesScreen(self.win, self.app)
        mock_diag = DiagnosticResult(
            name="Repo: linux-mainline",
            passed=True,
            latency_ms=8.5,
            message="Git repository valid",
            details="Default branch: master (1da177e4)",
        )
        with patch("core.tui.screens.repos.test_repository", return_value=mock_diag):
            diag = screen.test_selected_repository()
            self.assertIsNotNone(diag)
            self.assertTrue(diag.passed)
            self.assertEqual(screen.last_test_result, mock_diag)
            self.assertIn("linux-mainline", screen.status_bar.message)

    def test_enter_opens_detail_screen(self) -> None:
        """Enter key pushes RepoDetailScreen onto app stack."""
        screen = RepositoriesScreen(self.win, self.app)
        handled = screen.handle_key(10)
        self.assertTrue(handled)
        self.assertEqual(len(self.app.screen_stack), 1)
        self.assertIsInstance(self.app.screen_stack[0], RepoDetailScreen)

    def test_selected_repository_is_restored_on_reopen(self) -> None:
        """Leaving the list remembers the highlighted repository for the next instance."""
        screen = RepositoriesScreen(self.win, self.app)
        screen.repos_list.set_selected(1)
        screen.on_leave()
        reopened = RepositoriesScreen(self.win, self.app)
        self.assertEqual(reopened.repos_list.get_selected()["name"], "linux-stable")

    def test_esc_pops_screen(self) -> None:
        """Esc key calls app.pop_screen()."""
        screen = RepositoriesScreen(self.win, self.app)
        handled = screen.handle_key(27)
        self.assertTrue(handled)
        self.assertTrue(self.app.popped)

    def test_draw_repositories_screen(self) -> None:
        """draw() renders title, workspace path, list, and status bar without curses errors."""
        screen = RepositoriesScreen(self.win, self.app)
        screen.draw()
        addstr_texts = [call[1][2] for call in self.win.calls if call[0] == "addstr"]
        self.assertTrue(any("Repository & Git Branch Management" in t for t in addstr_texts))
        self.assertTrue(any("Workspace repos_dir:" in t for t in addstr_texts))
        self.assertTrue(any("Registered Repositories" in t for t in addstr_texts))
        self.assertTrue(any("[Space]" in t for t in addstr_texts))


class TestRepoDetailScreen(unittest.TestCase):
    """Unit tests for RepoDetailScreen."""

    def setUp(self) -> None:
        self.win = MockCursesWindow(24, 80)
        self.repo_entry = {
            "name": "linux-tree",
            "path": "repos/linux-tree",
            "project": "linux-kernel",
            "default_branch": "master",
            "git_url": "git://example.com/linux.git",
            "root_commit": "1da177e4",
            "start_ref": "v3.0",
            "enabled": True,
            "branches": ["master", "linux-3.0.y", "linux-3.4.y", "next/master"],
            "tag_views": copy.deepcopy(DEFAULT_TAG_VIEWS),
        }
        self.app = MockApp({"repositories": [self.repo_entry]})

    def test_screen_init_populates_widgets(self) -> None:
        """Field inputs, branch list, and tag views are populated correctly."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        self.assertEqual(screen.project_input.value, "linux-kernel")
        self.assertEqual(screen.default_branch_input.value, "master")
        self.assertEqual(screen.start_ref_input.value, "v3.0")
        self.assertEqual(screen.git_url_input.value, "git://example.com/linux.git")

        # Branches
        self.assertEqual(len(screen.all_branches), 4)
        self.assertEqual(screen.selected_branches, {"master", "linux-3.0.y", "linux-3.4.y", "next/master"})

        # Tag view regex rules
        self.assertGreaterEqual(len(screen.tag_view_rules), 1)

    def test_branch_toggle_and_filter(self) -> None:
        """Space toggles branch selection and filter restricts visible branches."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        screen.focus_widget(screen.widgets.index(screen.branch_list))

        # Currently selected branch is first (master)
        self.assertIn("master", screen.selected_branches)
        screen.handle_key(ord(" "))
        self.assertNotIn("master", screen.selected_branches)

        # Toggle back
        screen.handle_key(ord(" "))
        self.assertIn("master", screen.selected_branches)

        # Test filtering
        screen.filter_branches("3.0")
        self.assertEqual(screen.visible_branches, ["linux-3.0.y"])

        screen.filter_branches("")
        self.assertEqual(len(screen.visible_branches), 4)

    def test_reset_tag_views_to_presets(self) -> None:
        """Reset tag views restores DEFAULT_TAG_VIEWS."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        screen.tag_views = []
        screen.tag_view_rules = []
        self.assertEqual(len(screen.tag_views), 0)

        # Reset
        screen.reset_tag_views_to_presets()
        self.assertEqual(screen.tag_views, DEFAULT_TAG_VIEWS)
        self.assertGreater(len(screen.tag_view_rules), 0)
        self.assertIn("Reset tag views", screen.status_bar.message)

    def test_save_changes_commits_and_pops(self) -> None:
        """Saving updates repo_entry, sets is_dirty, and pops screen."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        screen.project_input.value = "updated-kernel"
        screen.default_branch_input.value = "main"
        screen.start_ref_input.value = "v3.1"
        screen.git_url_input.value = "https://github.com/torvalds/linux.git"
        screen.selected_branches = {"main", "linux-3.0.y"}

        self.assertFalse(self.app.is_dirty)
        self.assertFalse(self.app.popped)

        screen.save_changes()

        self.assertEqual(self.repo_entry["project"], "updated-kernel")
        self.assertEqual(self.repo_entry["default_branch"], "main")
        self.assertEqual(self.repo_entry["start_ref"], "v3.1")
        self.assertEqual(self.repo_entry["git_url"], "https://github.com/torvalds/linux.git")
        self.assertEqual(self.repo_entry["branches"], ["linux-3.0.y"])
        self.assertTrue(self.app.is_dirty)
        self.assertTrue(self.app.popped)

    def test_save_button_action(self) -> None:
        """Pressing Enter or Space on Save Button triggers save."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        screen.focus_widget(screen.widgets.index(screen.save_btn))
        screen.project_input.value = "button-saved-project"

        screen.handle_key(10)  # Enter
        self.assertEqual(self.repo_entry["project"], "button-saved-project")
        self.assertTrue(self.app.is_dirty)
        self.assertTrue(self.app.popped)

    def test_hotkey_s_and_t_when_not_in_textinput(self) -> None:
        """S and T keys trigger save and test when focused outside TextInput."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        screen.focus_widget(screen.widgets.index(screen.branch_list))

        # Test hotkey T
        mock_diag = DiagnosticResult(name="Candidate", passed=True, latency_ms=5.0, message="OK")
        with patch("core.tui.screens.repos.test_repository", return_value=mock_diag):
            screen.handle_key(ord("t"))
            self.assertEqual(screen.last_test_result, mock_diag)

        # Test hotkey S
        screen.handle_key(ord("s"))
        self.assertTrue(self.app.popped)

    def test_esc_discards_and_pops(self) -> None:
        """Esc with unsaved edits asks first; Discard exits without committing them."""
        orig_project = self.repo_entry["project"]
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        screen.project_input.value = "never-saved-project"

        with patch("core.tui.screens.repos.safe_modal_dialog", return_value="Discard") as dlg:
            screen.handle_key(27)  # Esc
        dlg.assert_called_once()
        self.assertEqual(self.repo_entry["project"], orig_project)
        self.assertTrue(self.app.popped)

    def test_esc_cancel_keeps_unsaved_edits(self) -> None:
        """Cancelling the unsaved-changes prompt stays on the screen with the edits intact."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        screen.add_tag_rule("^keep-me$")
        with patch("core.tui.screens.repos.safe_modal_dialog", return_value="Cancel"):
            screen.handle_key(27)
        self.assertFalse(self.app.popped)
        self.assertIn(("— any branch —", "^keep-me$"), screen.tag_view_rules)

    def test_esc_without_changes_pops_without_prompt(self) -> None:
        """Esc on an unchanged form returns immediately."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        with patch("core.tui.screens.repos.safe_modal_dialog") as dlg:
            screen.handle_key(27)
        dlg.assert_not_called()
        self.assertTrue(self.app.popped)

    def test_saved_tag_rule_is_shown_again_on_reopen(self) -> None:
        """A saved rule stays in the config and is re-highlighted (full text shown) on the next visit."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        screen.focus_widget(screen.widgets.index(screen.tag_views_list))
        screen.add_tag_rule("^v3\\.0-custom$")
        screen.save_changes()

        reopened = RepoDetailScreen(self.win, self.app, self.repo_entry)
        self.assertIs(reopened.get_focused_widget(), reopened.tag_views_list)
        self.assertEqual(reopened.tag_views_list.get_selected()[1], "^v3\\.0-custom$")
        self.assertEqual(
            reopened.selected_rule_lines(80, 2), ["Rule [— any branch —]: ^v3\\.0-custom$"]
        )
        reopened.draw()
        drawn = [c[1][2] for c in self.win.calls if c[0] == "addstr"]
        self.assertTrue(any("^v3\\.0-custom$" in t for t in drawn))

    def test_saved_empty_project_and_branch_stay_empty(self) -> None:
        """Empty saved values re-open empty instead of falling back to the name / "master"."""
        self.repo_entry["project"] = ""
        self.repo_entry["default_branch"] = ""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        self.assertEqual(screen.project_input.value, "")
        self.assertEqual(screen.default_branch_input.value, "")

    def test_last_tag_rule_cannot_be_removed(self) -> None:
        """Deleting down to zero rules is refused, so the presets never come back silently."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        while len(screen.tag_view_rules) > 1:
            self.assertTrue(screen.remove_selected_tag_rule())
        self.assertFalse(screen.remove_selected_tag_rule())
        self.assertEqual(len(screen.tag_view_rules), 1)
        self.assertIn("last rule", screen.status_bar.message)

    def test_selected_rule_lines_wraps_long_regex(self) -> None:
        """Long rules wrap over the available rows and end with an ellipsis when cut."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        lines = screen.selected_rule_lines(20, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertEqual(screen.selected_rule_lines(20, 0), [])

    def test_tab_and_arrow_navigation(self) -> None:
        """Tab and arrow keys navigate form widgets."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        self.assertEqual(screen.focused_widget_index, 0)

        # Tab advances
        screen.handle_key(9)
        self.assertEqual(screen.focused_widget_index, 1)

        # Down advances from TextInput
        screen.handle_key(curses.KEY_DOWN)
        self.assertEqual(screen.focused_widget_index, 2)

        # Up retreats
        screen.handle_key(curses.KEY_UP)
        self.assertEqual(screen.focused_widget_index, 1)

        # Shift-Tab retreats
        screen.handle_key(curses.KEY_BTAB)
        self.assertEqual(screen.focused_widget_index, 0)

    def test_arrow_keys_move_rows_inside_branch_list(self) -> None:
        """Down/Up walk every branch row; focus leaves the box only past the last/first row."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        list_idx = screen.widgets.index(screen.branch_list)
        screen.focus_widget(list_idx)

        for expected in range(1, 4):
            screen.handle_key(curses.KEY_DOWN)
            self.assertEqual(screen.focused_widget_index, list_idx)
            self.assertEqual(screen.branch_list.get_selected_index(), expected)

        # Space toggles the non-first (last) branch
        last = screen.branch_list.get_selected()
        self.assertEqual(last, "next/master")
        screen.handle_key(ord(" "))
        self.assertNotIn("next/master", screen.selected_branches)
        self.assertIn("master", screen.selected_branches)

        # Down on the last row leaves the box
        screen.handle_key(curses.KEY_DOWN)
        self.assertIs(screen.get_focused_widget(), screen.tag_views_list)

        # Back up into the branch list, then Up walks rows until the first, then exits
        screen.handle_key(curses.KEY_UP)
        self.assertIs(screen.get_focused_widget(), screen.branch_list)
        for expected in (2, 1, 0):
            screen.handle_key(curses.KEY_UP)
            self.assertIs(screen.get_focused_widget(), screen.branch_list)
            self.assertEqual(screen.branch_list.get_selected_index(), expected)
        screen.handle_key(curses.KEY_UP)
        self.assertIs(screen.get_focused_widget(), screen.branch_filter_input)

    def test_arrow_keys_move_rows_inside_tag_rules_and_edit_non_first(self) -> None:
        """Down reaches the second rule, Enter edits it, A adds after it, D removes it."""
        self.repo_entry["tag_views"] = [
            {"name": "stable", "regex_rules": [r"^v3\.0", r"^v3\.1"]},
            {"name": "next", "regex_rules": [r"^next-"]},
        ]
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        list_idx = screen.widgets.index(screen.tag_views_list)
        screen.focus_widget(list_idx)

        screen.handle_key(curses.KEY_DOWN)
        self.assertEqual(screen.focused_widget_index, list_idx)
        self.assertEqual(screen.tag_views_list.get_selected(), ("stable", r"^v3\.1"))
        screen.handle_key(curses.KEY_DOWN)
        self.assertEqual(screen.tag_views_list.get_selected(), ("next", r"^next-"))
        screen.handle_key(curses.KEY_UP)
        self.assertEqual(screen.tag_views_list.get_selected_index(), 1)

        # Enter edits the highlighted (second) rule via the modal prompt
        with patch.object(ModalTextInputDialog, "show", return_value=r"^v3\.2"):
            screen.handle_key(10)
        self.assertEqual(screen.tag_views[0]["regex_rules"], [r"^v3\.0", r"^v3\.2"])
        self.assertEqual(screen.tag_views_list.get_selected(), ("stable", r"^v3\.2"))

        # Invalid regex is rejected and leaves the rule untouched
        self.assertFalse(screen.edit_selected_tag_rule("^(unclosed"))
        self.assertEqual(screen.tag_views[0]["regex_rules"], [r"^v3\.0", r"^v3\.2"])
        self.assertIn("Invalid regex", screen.status_bar.message)

        # A adds a rule after the highlighted one, in the same view
        with patch.object(ModalTextInputDialog, "show", return_value=r"^v3\.3"):
            screen.handle_key(ord("a"))
        self.assertEqual(screen.tag_views[0]["regex_rules"], [r"^v3\.0", r"^v3\.2", r"^v3\.3"])
        self.assertEqual(screen.tag_views_list.get_selected(), ("stable", r"^v3\.3"))

        # D removes the highlighted rule
        screen.handle_key(ord("d"))
        self.assertEqual(screen.tag_views[0]["regex_rules"], [r"^v3\.0", r"^v3\.2"])

        # Removing the last rule of a view drops that view
        screen.tag_views_list.set_selected(2)
        screen.handle_key(curses.KEY_DC)
        self.assertEqual([v["name"] for v in screen.tag_views], ["stable"])

        # Edits are persisted on save
        screen.save_changes()
        self.assertEqual(self.repo_entry["tag_views"], [{"name": "stable", "regex_rules": [r"^v3\.0", r"^v3\.2"]}])

    def test_draw_detail_screen(self) -> None:
        """draw() renders all form fields, lists, buttons, and status bar without errors."""
        screen = RepoDetailScreen(self.win, self.app, self.repo_entry)
        screen.draw()
        addstr_texts = [call[1][2] for call in self.win.calls if call[0] == "addstr"]
        self.assertTrue(any("Repository Detail:" in t for t in addstr_texts))
        self.assertTrue(any("Project:" in t for t in addstr_texts))
        self.assertTrue(any("Default Branch:" in t for t in addstr_texts))
        self.assertTrue(any("Branches" in t for t in addstr_texts))
        self.assertTrue(any("Tag View Regex Rules" in t for t in addstr_texts))
        self.assertTrue(any("Save (S)" in t for t in addstr_texts))


class TestModalTextInputDialog(unittest.TestCase):
    """Unit tests for ModalTextInputDialog."""

    def setUp(self) -> None:
        self.win = MockCursesWindow(24, 80)

    def test_dialog_typing_and_submit(self) -> None:
        dialog = ModalTextInputDialog(self.win, "Input", "Prompt:", default_val="initial")
        dialog.draw()

        # Type additional char
        dialog.handle_key(ord("!"))
        self.assertEqual(dialog.input_field.value, "initial!")

        # Enter submits
        closed, val = dialog.handle_key(10)
        self.assertTrue(closed)
        self.assertEqual(val, "initial!")

    def test_dialog_esc_cancels(self) -> None:
        dialog = ModalTextInputDialog(self.win, "Input", "Prompt:", default_val="initial")
        closed, val = dialog.handle_key(27)
        self.assertTrue(closed)
        self.assertIsNone(val)


if __name__ == "__main__":
    unittest.main()
