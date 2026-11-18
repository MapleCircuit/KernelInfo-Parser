"""tests/test_version_parse_screen.py - Unit tests for VersionParseScreen (parse specific tags)."""

from __future__ import annotations

import curses
import unittest
from typing import Any
from unittest.mock import patch

from core.tui.screens import version_parse as vp
from core.tui.screens.launch import SAVE_AND_RUN
from core.tui.screens.version_parse import (
    VersionParseScreen,
    format_tag_list,
    tag_matches,
)
from tests.test_tui import MockCursesWindow

LINUX_TAGS = ["v2.6.39", "v3.0-rc1", "v3.0", "v3.1-rc1", "v3.1", "v3.2", "v3.10"]
STABLE_TAGS = ["v3.0.1", "v3.0.2"]
TAGS_BY_PATH = {"linux": LINUX_TAGS, "linux-stable": STABLE_TAGS}

SPACE, ESC, TAB, ENTER = ord(" "), 27, 9, 10


class MockApp:
    """App double recording `request_launch` calls; never reads or writes config.json."""

    def __init__(self, config: dict[str, Any], dirty: bool = False) -> None:
        self.config = config
        self.is_dirty = dirty
        self.config_path = "config.json"
        self.view_state: dict[str, dict[str, Any]] = {}
        self.launch_calls: list[dict[str, Any]] = []
        self.launch_args: dict[str, Any] = {}
        self.popped = False

    def pop_screen(self) -> None:
        self.popped = True

    def disk_config(self) -> dict[str, Any]:
        return self.config

    def request_launch(self, save: bool = False, **launch_args: Any) -> tuple[bool, str]:
        self.launch_calls.append({"save": save, **launch_args})
        self.launch_args = dict(launch_args)
        return True, ""


def make_config(*names: str, enabled: bool = True) -> dict[str, Any]:
    return {"repositories": [{"name": n, "path": f"/nonexistent/{n}", "enabled": enabled} for n in names]}


def fake_tags(path: Any, include_pre_releases: bool = True) -> list[str]:
    return list(TAGS_BY_PATH.get(str(path).rsplit("/", 1)[-1], []))


class VersionParseTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.win = MockCursesWindow(30, 100)
        patches = [
            patch.object(vp, "is_git_repo", side_effect=lambda p: "missing" not in str(p)),
            patch.object(vp, "list_version_tags", side_effect=fake_tags),
        ]
        self.mocks = [p.start() for p in patches]
        self.list_tags_mock = self.mocks[1]
        for p in patches:
            self.addCleanup(p.stop)

    def screen(self, app: MockApp) -> VersionParseScreen:
        return VersionParseScreen(self.win, app)

    def check(self, screen: VersionParseScreen, *tags: str) -> None:
        """Highlight and toggle each tag through the list keys."""
        screen.focus_widget(screen.widgets.index(screen.tag_list))
        for tag in tags:
            screen.tag_list.set_selected(screen.visible_tags.index(tag))
            self.assertTrue(screen.handle_key(SPACE))


class TestVersionParseHelpers(unittest.TestCase):
    def test_tag_matches_substring_and_glob(self) -> None:
        self.assertTrue(tag_matches("v3.10", "3.1"))
        self.assertTrue(tag_matches("V3.1", "v3.1"))
        self.assertFalse(tag_matches("v3.10", "v3.?"))
        self.assertTrue(tag_matches("v3.1", "V3.?"))
        self.assertTrue(tag_matches("v3.0-rc1", "*-RC*"))
        self.assertTrue(tag_matches("anything", "  "))

    def test_format_tag_list_truncates(self) -> None:
        tags = [f"v{i}" for i in range(13)]
        self.assertEqual(format_tag_list(tags[:2]), "v0, v1")
        self.assertTrue(format_tag_list(tags).endswith("v9, ... (+3 more)"))


class TestVersionParseScreen(VersionParseTestBase):
    def test_defaults_highlight_v30_without_checking(self) -> None:
        screen = self.screen(MockApp(make_config("linux")))
        self.assertEqual(screen.tag_list.get_selected(), "v3.0")
        self.assertEqual(screen.selected_tags(), [])
        self.assertTrue(screen.rc_checkbox.is_checked())
        self.assertFalse(screen.continue_checkbox.is_checked())
        self.assertIs(screen.get_focused_widget(), screen.tag_list)
        self.assertNotIn(screen.repo_select, screen.widgets)  # one repository: nothing to switch

    def test_multi_tag_selection_launches_exact_refs_in_version_order(self) -> None:
        app = MockApp(make_config("linux"))
        screen = self.screen(app)
        self.check(screen, "v3.2", "v3.0")
        screen.continue_checkbox.set_checked(True)  # not applicable with several tags
        self.assertTrue(screen.handle_key(ord("P")))
        self.assertEqual(app.launch_calls, [
            {"save": False, "repo": "linux", "ref": ["v3.0", "v3.2"], "single_version": True},
        ])

    def test_single_tag_continue_passes_pre_release_choice(self) -> None:
        app = MockApp(make_config("linux"))
        screen = self.screen(app)
        self.check(screen, "v3.0")
        screen.continue_checkbox.set_checked(True)
        screen.rc_checkbox.set_checked(False)
        screen.apply_filter()
        self.assertTrue(screen.parse())
        self.assertEqual(app.launch_args, {
            "repo": "linux", "ref": ["v3.0"], "single_version": False, "include_pre_releases": False,
        })

    def test_single_tag_without_continue_is_single_version(self) -> None:
        app = MockApp(make_config("linux"))
        screen = self.screen(app)
        self.check(screen, "v3.1")
        screen.focus_widget(screen.widgets.index(screen.parse_btn))
        self.assertTrue(screen.handle_key(ENTER))
        self.assertEqual(app.launch_args, {"repo": "linux", "ref": ["v3.1"], "single_version": True})

    def test_filter_and_toggle_all_visible(self) -> None:
        app = MockApp(make_config("linux"))
        screen = self.screen(app)
        screen.focus_widget(screen.widgets.index(screen.filter_input))
        for ch in "v3.?":
            screen.handle_key(ord(ch))
        self.assertEqual(screen.visible_tags, ["v3.0", "v3.1", "v3.2"])
        screen.handle_key(ENTER)  # filter -> list
        self.assertIs(screen.get_focused_widget(), screen.tag_list)
        screen.handle_key(ord("a"))
        self.assertEqual(screen.selected_tags(), ["v3.0", "v3.1", "v3.2"])
        screen.handle_key(ord("a"))  # all visible checked: unchecks them
        self.assertEqual(screen.selected_tags(), [])

        screen.filter_input.set_value("3.1")
        screen.apply_filter()
        self.assertEqual(screen.visible_tags, ["v3.1-rc1", "v3.1", "v3.10"])
        screen.handle_key(ord("a"))
        screen.handle_key(ord("p"))
        self.assertEqual(app.launch_args["ref"], ["v3.1-rc1", "v3.1", "v3.10"])

    def test_typing_letters_in_filter_does_not_trigger_shortcuts(self) -> None:
        app = MockApp(make_config("linux"))
        screen = self.screen(app)
        screen.focus_widget(screen.widgets.index(screen.filter_input))
        for ch in "pan":
            screen.handle_key(ord(ch))
        self.assertEqual(screen.filter_input.get_value(), "pan")
        self.assertEqual(app.launch_calls, [])
        self.assertEqual(screen.visible_tags, [])

    def test_pre_release_checkbox_hides_rc_tags(self) -> None:
        screen = self.screen(MockApp(make_config("linux")))
        screen.focus_widget(screen.widgets.index(screen.rc_checkbox))
        screen.handle_key(SPACE)
        self.assertFalse(screen.rc_checkbox.is_checked())
        self.assertEqual(screen.visible_tags, ["v2.6.39", "v3.0", "v3.1", "v3.2", "v3.10"])
        self.assertEqual(screen.tag_list.get_selected(), "v3.0")

    def test_clear_selection_keys(self) -> None:
        screen = self.screen(MockApp(make_config("linux")))
        for key in ("n", "c"):
            self.check(screen, "v3.0", "v3.1")
            screen.handle_key(ord(key))
            self.assertEqual(screen.selected_tags(), [])

    def test_no_selection_is_refused(self) -> None:
        app = MockApp(make_config("linux"))
        screen = self.screen(app)
        with patch.object(vp, "safe_modal_dialog", return_value="OK") as dialog:
            self.assertFalse(screen.parse())
        self.assertEqual(dialog.call_args.args[1], "No Tags Selected")
        self.assertEqual(app.launch_calls, [])

    def test_confirmation_lists_repo_tags_and_mode(self) -> None:
        app = MockApp(make_config("linux"))
        screen = self.screen(app)
        screen.handle_key(ord("a"))
        with patch.object(vp, "safe_modal_dialog", return_value="Cancel") as dialog:
            self.assertFalse(screen.parse())
        title, message = dialog.call_args.args[1], dialog.call_args.args[2]
        self.assertEqual(title, "Parse Tags")
        self.assertIn("Repository: linux", message)
        self.assertIn("Tags (7): v2.6.39, v3.0-rc1", message)
        self.assertIn("Exactly these 7 tags", message)
        self.assertEqual(app.launch_calls, [])

    def test_cancel_at_unsaved_changes_prompt(self) -> None:
        app = MockApp(make_config("linux"), dirty=True)
        screen = self.screen(app)
        self.check(screen, "v3.0")
        # Empty key queue: the confirmation takes "Parse", the unsaved-edits prompt its default "Cancel"
        self.assertFalse(screen.parse())
        self.assertEqual(app.launch_calls, [])
        with patch.object(vp, "choose_launch_config", return_value=None) as choose:
            self.assertFalse(screen.parse())
        self.assertEqual(choose.call_args.kwargs, {"action": "Parse"})
        self.assertEqual(app.launch_calls, [])

    def test_save_and_parse_saves_first(self) -> None:
        app = MockApp(make_config("linux"), dirty=True)
        screen = self.screen(app)
        self.check(screen, "v3.0")
        with patch.object(vp, "choose_launch_config", return_value=SAVE_AND_RUN):
            self.assertTrue(screen.parse())
        self.assertEqual(app.launch_calls, [
            {"save": True, "repo": "linux", "ref": ["v3.0"], "single_version": True},
        ])

    def test_repo_missing_from_saved_config_needs_save(self) -> None:
        app = MockApp(make_config("linux"))
        app.disk_config = lambda: {"repositories": []}  # type: ignore[method-assign]
        screen = self.screen(app)
        self.check(screen, "v3.0")
        with patch.object(vp, "safe_modal_dialog", side_effect=["Parse", "OK"]) as dialog:
            self.assertFalse(screen.parse())
        self.assertEqual(dialog.call_args.args[1], "Repository Not Saved")
        self.assertEqual(app.launch_calls, [])

    def test_repo_switching_reloads_tags_and_clears_selection(self) -> None:
        app = MockApp(make_config("linux", "linux-stable"))
        screen = self.screen(app)
        self.assertIn(screen.repo_select, screen.widgets)
        self.check(screen, "v3.0")
        screen.focus_widget(screen.widgets.index(screen.repo_select))
        screen.handle_key(curses.KEY_RIGHT)
        self.assertEqual(screen.current_repo["name"], "linux-stable")
        self.assertEqual(screen.visible_tags, STABLE_TAGS)
        self.assertEqual(screen.selected_tags(), [])
        screen.handle_key(curses.KEY_LEFT)  # back to linux: cached tags, selection still cleared
        self.assertEqual(screen.current_repo["name"], "linux")
        self.assertEqual(screen.selected_tags(), [])
        self.assertEqual(self.list_tags_mock.call_count, 2)
        self.check(screen, "v3.0")
        screen.handle_key(ord("P"))
        self.assertEqual(app.launch_args["repo"], "linux")

    def test_only_enabled_git_repositories_are_listed(self) -> None:
        config = make_config("linux")
        config["repositories"] += [
            {"name": "off", "path": "/nonexistent/off", "enabled": "false"},
            {"name": "missing", "path": "/nonexistent/missing"},
        ]
        screen = self.screen(MockApp(config))
        self.assertEqual([r["name"] for r in screen.repos], ["linux"])

    def test_no_repositories_shows_message_and_esc_goes_back(self) -> None:
        app = MockApp(make_config("linux", enabled=False))
        screen = self.screen(app)
        screen.draw()
        drawn = " ".join(c[1][2] for c in self.win.calls if c[0] == "addstr")
        self.assertIn("No git repositories configured", drawn)
        self.assertFalse(screen.parse())
        self.assertTrue(screen.handle_key(ESC))
        self.assertTrue(app.popped)

    def test_view_state_restores_repo_filter_and_highlight(self) -> None:
        app = MockApp(make_config("linux", "linux-stable"))
        screen = self.screen(app)
        screen.select_repo(0)
        screen.filter_input.set_value("v3")
        screen.apply_filter()
        screen.tag_list.set_selected(screen.visible_tags.index("v3.2"))
        screen.handle_key(ESC)
        self.assertEqual(app.view_state["version_parse"], {"repo": "linux", "filter": "v3", "highlight": "v3.2"})
        again = self.screen(app)
        self.assertEqual(again.current_repo["name"], "linux")
        self.assertEqual(again.filter_input.get_value(), "v3")
        self.assertEqual(again.tag_list.get_selected(), "v3.2")
        self.assertEqual(again.selected_tags(), [])

    def test_draw_shows_summary_and_not_applicable_continue(self) -> None:
        screen = self.screen(MockApp(make_config("linux")))
        self.check(screen, "v3.0", "v3.1")
        screen.draw()
        drawn = " ".join(c[1][2] for c in self.win.calls if c[0] == "addstr")
        self.assertIn("2 selected: v3.0, v3.1", drawn)
        self.assertIn("n/a: 2 tags selected", drawn)
        self.assertIn("[x] v3.0", drawn)
        self.assertIn("[ ] v2.6.39", drawn)

    def test_small_terminal_shows_min_size_message(self) -> None:
        self.win.max_y, self.win.max_x = 10, 40
        screen = self.screen(MockApp(make_config("linux")))
        screen.draw()
        drawn = " ".join(c[1][2] for c in self.win.calls if c[0] == "addstr")
        self.assertIn("Terminal window too small", drawn)

    def test_tab_cycles_focus(self) -> None:
        screen = self.screen(MockApp(make_config("linux")))
        start = screen.focused_widget_index
        for _ in range(len(screen.widgets)):
            screen.handle_key(TAB)
        self.assertEqual(screen.focused_widget_index, start)

    def test_preparse_defaults_and_independent_presets(self) -> None:
        app = MockApp(make_config("linux"))
        screen = self.screen(app)
        # Default is all_main for both insights and api
        self.assertEqual(screen.preparse_tags("insights"), ["v2.6.39", "v3.0", "v3.1", "v3.2", "v3.10"])
        self.assertEqual(screen.preparse_tags("api"), ["v2.6.39", "v3.0", "v3.1", "v3.2", "v3.10"])
        self.assertNotIn("preparse", screen.current_repo)
        self.assertFalse(app.is_dirty)

        screen.draw()
        drawn = " ".join(c[1][2] for c in self.win.calls if c[0] == "addstr")
        self.assertIn("Ins:[G] API:[G]", drawn)
        self.assertIn("Ins: 5  API: 5", drawn)

        # Change Insights to Latest N Main (N=2) independently of API
        screen.focus_widget(screen.widgets.index(screen.insights_mode_select))
        screen.handle_key(curses.KEY_RIGHT)  # All Main -> Latest N Main (default N=10)
        self.assertEqual(screen.preparse_tags("insights"), ["v2.6.39", "v3.0", "v3.1", "v3.2", "v3.10"])
        # Edit N to 2
        screen.focus_widget(screen.widgets.index(screen.insights_n_input))
        screen.insights_n_input.set_value("")
        screen.handle_key(ord("2"))
        self.assertEqual(screen.preparse_tags("insights"), ["v3.2", "v3.10"])
        self.assertEqual(screen.preparse_tags("api"), ["v2.6.39", "v3.0", "v3.1", "v3.2", "v3.10"])
        self.assertTrue(app.is_dirty)
        self.assertEqual(set(screen.current_repo["preparse"]), {"insights"})
        self.assertEqual(screen.current_repo["preparse"]["insights"]["mode"], "latest_n_main")
        self.assertEqual(screen.current_repo["preparse"]["insights"]["latest_n"], 2)

    def test_preparse_independent_tag_toggles_and_save(self) -> None:
        app = MockApp(make_config("linux"))
        save_calls: list[bool] = []
        app.save_config = lambda: (save_calls.append(True) or (True, "Saved!"))  # type: ignore[attr-defined]
        screen = self.screen(app)

        # Toggle v3.0-rc1 for Insights only via 'i'
        screen.focus_widget(screen.widgets.index(screen.tag_list))
        screen.tag_list.set_selected(screen.visible_tags.index("v3.0-rc1"))
        self.assertTrue(screen.handle_key(ord("i")))
        self.assertIn("v3.0-rc1", screen.preparse_tags("insights"))
        self.assertNotIn("v3.0-rc1", screen.preparse_tags("api"))

        # Toggle v3.0 off for API only via 'k' (switches API to custom without v3.0)
        screen.tag_list.set_selected(screen.visible_tags.index("v3.0"))
        self.assertTrue(screen.handle_key(ord("k")))
        self.assertNotIn("v3.0", screen.preparse_tags("api"))
        self.assertIn("v3.1", screen.preparse_tags("api"))
        self.assertIn("v3.0", screen.preparse_tags("insights"))

        # Save via 'S'
        self.assertTrue(screen.handle_key(ord("S")))
        self.assertEqual(len(save_calls), 1)
        self.assertEqual(screen.current_repo["preparse"]["insights"]["versions"], ["v3.0-rc1"])
        self.assertEqual(screen.current_repo["preparse"]["api"]["mode"], "custom")

    def test_main_menu_item_7_mentions_preparse(self) -> None:
        from core.tui.screens.main_menu import MainMenuScreen

        item_7 = next(item for item in MainMenuScreen.MENU_ITEMS if item[0] == "7")
        self.assertIn("Pre-Parse", item_7[1])
        self.assertIn("pre-parse", item_7[2].lower())

    def test_preparse_inline_controls_and_launch_args(self) -> None:
        app = MockApp(make_config("linux"))
        screen = self.screen(app)
        self.assertTrue(screen.preparse_insights_cb.is_checked())
        self.assertTrue(screen.preparse_stable_cb.is_checked())
        self.assertTrue(screen.preparse_api_cb.is_checked())
        self.assertIn(screen.preparse_btn, screen.widgets)

        # Default (nothing checked in list): main release tags via all_main
        self.assertEqual(screen.build_preparse_launch_args(), {
            "repo": "linux",
            "preparse": True,
            "preparse_insights": True,
            "preparse_stable": True,
            "preparse_api": True,
            "preparse_tags": ["v2.6.39", "v3.0", "v3.1", "v3.2", "v3.10"],
        })

        # Checked tags override configured tag selection
        self.check(screen, "v3.1-rc1", "v3.0")
        screen.preparse_api_cb.set_checked(False)
        self.assertEqual(screen.build_preparse_launch_args(), {
            "repo": "linux",
            "preparse": True,
            "preparse_insights": True,
            "preparse_stable": True,
            "preparse_api": False,
            "preparse_tags": ["v3.0", "v3.1-rc1"],
        })

        # Stable-only repository falls back to stable tags when none checked
        stable_app = MockApp(make_config("linux-stable"))
        stable_screen = self.screen(stable_app)
        self.assertEqual(stable_screen.build_preparse_launch_args()["preparse_tags"], STABLE_TAGS)

    def test_run_preparse_confirm_and_launch(self) -> None:
        app = MockApp(make_config("linux"))
        screen = self.screen(app)
        with patch.object(vp, "safe_modal_dialog", return_value="Pre-parse") as dialog, \
                patch.object(vp, "choose_launch_config", return_value=SAVE_AND_RUN) as choose:
            self.assertTrue(screen.handle_key(ord("X")))
        self.assertEqual(dialog.call_args.args[1], "Pre-parse Main / Insights / KAPI")
        self.assertEqual(choose.call_args.kwargs, {"action": "Pre-parse"})
        self.assertEqual(app.launch_calls, [{
            "save": True,
            "repo": "linux",
            "preparse": True,
            "preparse_insights": True,
            "preparse_stable": True,
            "preparse_api": True,
            "preparse_tags": ["v2.6.39", "v3.0", "v3.1", "v3.2", "v3.10"],
        }])

        # Clicking Pre-parse button via Enter
        app.launch_calls.clear()
        screen.focus_widget(screen.widgets.index(screen.preparse_btn))
        with patch.object(vp, "safe_modal_dialog", return_value="Cancel"):
            self.assertTrue(screen.handle_key(ENTER))
        self.assertEqual(app.launch_calls, [])

        # Refuses when all 3 targets are unchecked
        screen.preparse_insights_cb.set_checked(False)
        screen.preparse_stable_cb.set_checked(False)
        screen.preparse_api_cb.set_checked(False)
        with patch.object(vp, "safe_modal_dialog", return_value="OK") as dialog:
            self.assertFalse(screen.run_preparse())
        self.assertEqual(dialog.call_args.args[1], "No Pre-Parse Target")

        # Typing 'x' in filter_input edits filter instead of triggering pre-parse
        screen.focus_widget(screen.widgets.index(screen.filter_input))
        self.assertTrue(screen.handle_key(ord("x")))
        self.assertEqual(screen.filter_input.get_value(), "x")
        self.assertEqual(app.launch_calls, [])

    def test_min_terminal_size_draws_cleanly(self) -> None:
        self.win.max_y, self.win.max_x = vp.MIN_H, vp.MIN_W
        screen = self.screen(MockApp(make_config("linux")))
        screen.draw()
        drawn = " ".join(c[1][2] for c in self.win.calls if c[0] == "addstr")
        self.assertIn("Parse (P)", drawn)
        self.assertIn("Pre-parse (X)", drawn)


if __name__ == "__main__":
    unittest.main()
