"""tests/test_git_manager_screen.py - Unit tests for the Git Repository Manager TUI screen.

Remote add/list/remove run against throw-away local repositories (`git init`); clone and fetch
go through an injected fake runner, so no network access or real `repos/` directory is used.
"""

from __future__ import annotations

import curses
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from core.config import REPO_ROOT
from core.tui.git_ops import (
    DEFAULT_CVE_URL,
    DEFAULT_LINUX_URL,
    DEFAULT_MANPAGES_URL,
    KERNEL_CVE_URL,
    STANDARD_REMOTES,
    add_remote,
    clone_command,
    fetch_command,
    is_valid_repo_dirname,
    list_remotes,
    repo_dirname_from_url,
)
from core.tui.process_view import ProcessResult
from core.tui.screens.git_manager import (
    BRANCH_HINT,
    KIND_CVE,
    KIND_LINUX,
    KIND_MANPAGES,
    KIND_REPO,
    GitManagerScreen,
    StandardRemotesDialog,
    cve_repo_path,
    preset_is_present,
    remote_url_key,
    repository_candidates,
)
from tests.test_repos_screen import MockApp
from tests.test_tui import MockCursesWindow


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


class FakeRunner:
    """Stand-in for `run_streaming_command`: records calls and runs an optional side effect."""

    def __init__(self, result: ProcessResult | None = None,
                 effect: Callable[[list[str]], None] | None = None) -> None:
        self.result = result or ProcessResult(returncode=0)
        self.effect = effect
        self.calls: list[dict[str, Any]] = []

    def __call__(self, stdscr: Any, argv: list[str], title: str, cwd: str | None = None,
                 env: dict[str, str] | None = None, **_: Any) -> ProcessResult:
        self.calls.append({"argv": argv, "title": title, "cwd": cwd, "env": env})
        if self.effect is not None:
            self.effect(argv)
        return self.result


class GitManagerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repos_dir = self.root / "repos"
        self.repos_dir.mkdir()
        self.work = _git_init(self.repos_dir / "work")
        self.cve_dir = self.root / "cvelistV5"
        self.win = MockCursesWindow(24, 80)
        self.app = MockApp({
            "workspace": {"repos_dir": str(self.repos_dir)},
            "repositories": [{"name": "work", "path": str(self.work)}],
            "cve": {"repo_dir": str(self.cve_dir)},
        })
        self.runner = FakeRunner()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_screen(self) -> GitManagerScreen:
        return GitManagerScreen(self.win, self.app, run_command=self.runner)

    def work_screen(self) -> GitManagerScreen:
        screen = self.make_screen()
        while screen.current_repo.name != "work":
            screen.switch_repo()
        return screen


class TestRepositorySelection(GitManagerTestBase):
    def test_candidates_include_clone_target_and_default_to_linux(self) -> None:
        screen = self.make_screen()
        names = [c.name for c in screen.candidates]
        self.assertEqual(names, ["work", "linux", "man-pages", "cve"])
        self.assertEqual(screen.current_repo.path, self.repos_dir / "linux")
        self.assertFalse(screen.repo_exists)
        self.assertEqual(screen.remotes, [])

    def test_non_git_entries_are_skipped(self) -> None:
        (self.repos_dir / "plain").mkdir()
        self.app.config["repositories"].append({"name": "plain", "path": str(self.repos_dir / "plain")})
        screen = self.make_screen()
        self.assertNotIn("plain", [c.name for c in screen.candidates])

    def test_repo_switch_cycles_and_loads_remotes(self) -> None:
        add_remote(self.work, "origin", DEFAULT_LINUX_URL)
        screen = self.make_screen()
        self.assertTrue(screen.handle_key(ord("r")))
        self.assertEqual(screen.current_repo.name, "man-pages")
        self.assertTrue(screen.handle_key(ord("r")))
        self.assertEqual(screen.current_repo.name, "cve")
        self.assertTrue(screen.handle_key(ord("r")))
        self.assertEqual(screen.current_repo.name, "work")
        self.assertTrue(screen.repo_exists)
        self.assertEqual(screen.remotes, [("origin", DEFAULT_LINUX_URL)])
        screen.handle_key(ord("R"))
        self.assertEqual(screen.current_repo.name, "cve")
        screen.handle_key(ord("R"))
        self.assertEqual(screen.current_repo.name, "man-pages")
        screen.handle_key(ord("R"))
        self.assertEqual(screen.current_repo.name, "linux")

    def test_repo_path_argument_preselects_repository(self) -> None:
        screen = GitManagerScreen(self.win, self.app, run_command=self.runner, repo_path=str(self.work))
        self.assertEqual(screen.current_repo.name, "work")
        screen = GitManagerScreen(self.win, self.app, run_command=self.runner, repo_path=str(self.cve_dir))
        self.assertEqual(screen.current_repo.kind, KIND_CVE)

    def test_view_state_restores_repo_and_remote(self) -> None:
        add_remote(self.work, "aaa", "https://example.com/a.git")
        add_remote(self.work, "bbb", "https://example.com/b.git")
        screen = self.work_screen()
        screen.remote_list.set_selected(1)
        self.app.screen_stack.append(screen)
        screen.handle_key(27)
        self.assertTrue(self.app.popped)
        again = self.make_screen()
        self.assertEqual(again.current_repo.name, "work")
        self.assertEqual(again.selected_remote(), ("bbb", "https://example.com/b.git"))

    def test_draw_normal_and_small_terminal(self) -> None:
        screen = self.work_screen()
        screen.status_bar.set_message(None)
        screen.draw()
        text = " ".join(str(c[1][2]) for c in self.win.calls if c[0] == "addstr")
        self.assertIn("Git Repository Manager", text)
        self.assertIn("[A]  Add", text)
        self.assertNotIn("[S]", text)
        self.assertNotIn("[U]", text)
        small = MockCursesWindow(10, 40)
        GitManagerScreen(small, self.app, run_command=self.runner).draw()
        small_text = " ".join(str(c[1][2]) for c in small.calls if c[0] == "addstr")
        self.assertIn("too small", small_text)

    def test_draw_missing_repo_offers_clone(self) -> None:
        screen = self.make_screen()
        screen.draw()
        text = " ".join(str(c[1][2]) for c in self.win.calls if c[0] == "addstr")
        self.assertIn("Clone Linux", text)
        self.assertIn("Not cloned yet", text)


class TestClone(GitManagerTestBase):
    def dest(self) -> Path:
        return self.repos_dir / "linux"

    def test_clone_success_registers_repository(self) -> None:
        self.runner.effect = lambda argv: _git_init(Path(argv[-1]))
        screen = self.make_screen()
        self.assertTrue(screen.clone_linux(url=DEFAULT_LINUX_URL, confirmed=True))
        self.assertEqual(self.runner.calls[0]["argv"], clone_command(DEFAULT_LINUX_URL, self.dest()))
        self.assertEqual(self.runner.calls[0]["env"]["GIT_TERMINAL_PROMPT"], "0")
        paths = [r["path"] for r in self.app.config["repositories"]]
        self.assertIn(str(self.dest()), paths)
        self.assertTrue(self.app.is_dirty)
        self.assertTrue(screen.repo_exists)
        self.assertEqual(screen.current_repo.path, self.dest())
        self.assertIn("Cloned", screen.status_bar.message)

    def test_clone_key_uses_prompt_and_confirm_defaults(self) -> None:
        self.runner.effect = lambda argv: _git_init(Path(argv[-1]))
        screen = self.make_screen()
        # Empty key queue: the URL prompt returns its default, the confirm dialog its default (Clone)
        self.assertTrue(screen.handle_key(ord("c")))
        self.assertEqual(self.runner.calls[0]["argv"][-2], DEFAULT_LINUX_URL)

    def test_clone_refused_when_destination_exists(self) -> None:
        self.dest().mkdir()
        screen = self.make_screen()
        self.assertFalse(screen.clone_linux(url=DEFAULT_LINUX_URL, confirmed=True))
        self.assertEqual(self.runner.calls, [])
        self.assertIn("already exists", screen.status_bar.message)

    def test_clone_rejects_invalid_url(self) -> None:
        screen = self.make_screen()
        self.assertFalse(screen.clone_linux(url="ssh://git@example.com/linux.git", confirmed=True))
        self.assertEqual(self.runner.calls, [])
        self.assertIn("https://", screen.status_bar.message)

    def test_cancelled_clone_keeps_partial_directory_by_default(self) -> None:
        self.runner.result = ProcessResult(returncode=-15, cancelled=True)
        self.runner.effect = lambda argv: Path(argv[-1]).mkdir()
        screen = self.make_screen()
        self.assertFalse(screen.clone_linux(url=DEFAULT_LINUX_URL, confirmed=True))
        self.assertTrue(self.dest().exists())
        self.assertIn("cancelled", screen.status_bar.message)
        self.assertFalse(self.app.is_dirty)

    def test_cancelled_clone_deletes_partial_directory_on_request(self) -> None:
        self.runner.result = ProcessResult(returncode=-15, cancelled=True)
        self.runner.effect = lambda argv: Path(argv[-1]).mkdir()
        self.win.keys_queue = [curses.KEY_RIGHT, 10]  # Keep -> Delete
        screen = self.make_screen()
        self.assertFalse(screen.clone_linux(url=DEFAULT_LINUX_URL, confirmed=True))
        self.assertFalse(self.dest().exists())
        self.assertIn("deleted", screen.status_bar.message)


class TestStandardRemotes(GitManagerTestBase):
    def test_url_key_and_presence(self) -> None:
        stable = STANDARD_REMOTES[0]
        self.assertEqual(remote_url_key("https://Host/x.git/"), remote_url_key("git://host/x"))
        self.assertTrue(preset_is_present(stable, [("other", stable.url.replace("https", "git"))]))
        self.assertTrue(preset_is_present(stable, [(stable.name, "https://example.com/y.git")]))
        self.assertFalse(preset_is_present(stable, [("origin", DEFAULT_LINUX_URL)]))

    def test_dialog_toggles_only_missing_presets(self) -> None:
        presets = STANDARD_REMOTES[:3]
        dialog = StandardRemotesDialog(self.win, presets, [(presets[0].name, presets[0].url)])
        self.assertEqual(dialog.cursor, 1)
        self.assertFalse(dialog.toggle(0))
        dialog.handle_key(ord(" "))
        dialog.handle_key(curses.KEY_DOWN)
        dialog.handle_key(ord(" "))
        dialog.handle_key(ord(" "))  # untoggle
        dialog.draw()
        done, chosen = dialog.handle_key(10)
        self.assertTrue(done)
        self.assertEqual(chosen, [presets[1]])
        self.assertEqual(dialog.handle_key(27), (True, None))

    def test_add_standard_remotes_by_name(self) -> None:
        screen = self.work_screen()
        added = screen.add_standard_remotes(names=["stable", "net"], fetch=False)
        self.assertEqual(added, ["stable", "net"])
        self.assertEqual([n for n, _ in list_remotes(self.work)], ["net", "stable"])
        self.assertEqual(self.runner.calls, [])
        # Already-present presets are skipped
        self.assertEqual(screen.add_standard_remotes(names=["stable"], fetch=False), [])

    def test_a_key_standard_adds_dialog_choice_and_offers_fetch(self) -> None:
        screen = self.work_screen()
        preset = STANDARD_REMOTES[1]
        # Empty key queue: the Add Remote chooser's default button is Standard
        with patch.object(StandardRemotesDialog, "show", return_value=[preset]):
            self.assertTrue(screen.handle_key(ord("a")))
        self.assertIn(preset.name, [n for n, _ in screen.remotes])
        # Empty key queue picks the default "Fetch now"
        self.assertEqual(self.runner.calls[0]["argv"], fetch_command(self.work, preset.name))
        self.assertIn(BRANCH_HINT, screen.status_bar.message)

    def test_a_key_standard_dialog_cancel_adds_nothing(self) -> None:
        screen = self.work_screen()
        with patch.object(StandardRemotesDialog, "show", return_value=None):
            screen.handle_key(ord("A"))
        self.assertEqual(list_remotes(self.work), [])
        self.assertIn("cancelled", screen.status_bar.message)

    def test_a_key_chooser_cancel_adds_nothing(self) -> None:
        screen = self.work_screen()
        with patch("core.tui.screens.git_manager.safe_modal_dialog", return_value="Cancel"), \
                patch.object(StandardRemotesDialog, "show") as standard:
            self.assertTrue(screen.handle_key(ord("a")))
        standard.assert_not_called()
        self.assertEqual(list_remotes(self.work), [])
        self.assertIn("cancelled", screen.status_bar.message)

    def test_missing_repo_blocks_remote_actions(self) -> None:
        screen = self.make_screen()
        self.assertEqual(screen.add_standard_remotes(names=["stable"], fetch=False), [])
        self.assertFalse(screen.add_custom_remote("x", "https://example.com/x.git", fetch=False))
        self.assertIn("press C", screen.status_bar.message)


class TestCustomRemoteFetchRemove(GitManagerTestBase):
    def test_custom_remote_validation(self) -> None:
        screen = self.work_screen()
        self.assertFalse(screen.add_custom_remote("bad name", "https://example.com/x.git", fetch=False))
        self.assertFalse(screen.add_custom_remote("mine", "http://example.com/x.git", fetch=False))
        self.assertIn("URL must start with https:// or git://", screen.status_bar.message)
        self.assertFalse(screen.add_custom_remote("mine", "ssh://example.com/x.git", fetch=False))
        self.assertEqual(list_remotes(self.work), [])

    def test_custom_remote_added_and_duplicate_rejected(self) -> None:
        screen = self.work_screen()
        self.assertTrue(screen.add_custom_remote("mine", "git://example.com/x.git", fetch=False))
        self.assertEqual(screen.selected_remote(), ("mine", "git://example.com/x.git"))
        self.assertFalse(screen.add_custom_remote("mine", "https://example.com/y.git", fetch=False))
        self.assertIn("already exists", screen.status_bar.message)

    def test_a_key_custom_prompts_name_then_url_and_fetches(self) -> None:
        screen = self.work_screen()
        with patch("core.tui.screens.git_manager.safe_modal_dialog", side_effect=["Custom", "Fetch now"]), \
                patch("core.tui.screens.git_manager.ModalTextInputDialog.show",
                      side_effect=["mine", "https://example.com/x.git"]):
            self.assertTrue(screen.handle_key(ord("a")))
        self.assertEqual(list_remotes(self.work), [("mine", "https://example.com/x.git")])
        self.assertEqual(self.runner.calls[0]["argv"], fetch_command(self.work, "mine"))

    def test_fetch_selected_and_all(self) -> None:
        add_remote(self.work, "origin", DEFAULT_LINUX_URL)
        add_remote(self.work, "stable", STANDARD_REMOTES[0].url)
        screen = self.work_screen()
        screen.remote_list.set_selected(1)
        screen.handle_key(ord("f"))
        self.assertEqual(self.runner.calls[-1]["argv"], fetch_command(self.work, "stable"))
        self.assertIn(BRANCH_HINT, screen.status_bar.message)
        screen.handle_key(ord("G"))
        self.assertEqual(self.runner.calls[-1]["argv"], fetch_command(self.work, None))
        self.assertEqual(self.runner.calls[-1]["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_fetch_failure_and_no_remotes(self) -> None:
        screen = self.work_screen()
        self.assertIsNone(screen.fetch(None))
        self.assertIn("No remotes", screen.status_bar.message)
        add_remote(self.work, "origin", DEFAULT_LINUX_URL)
        screen.refresh_remotes()
        self.runner.result = ProcessResult(returncode=128)
        screen.fetch_selected()
        self.assertIn("failed", screen.status_bar.message)

    def test_remove_requires_confirmation(self) -> None:
        add_remote(self.work, "origin", DEFAULT_LINUX_URL)
        screen = self.work_screen()
        # Empty key queue: the dialog's default button is Cancel
        screen.handle_key(ord("d"))
        self.assertEqual(len(list_remotes(self.work)), 1)
        self.assertIn("cancelled", screen.status_bar.message)
        self.win.keys_queue = [curses.KEY_LEFT, 10]  # Cancel -> Remove
        screen.handle_key(curses.KEY_DC)
        self.assertEqual(list_remotes(self.work), [])
        self.assertEqual(screen.remotes, [])

    def test_remove_without_selection(self) -> None:
        screen = self.work_screen()
        self.assertFalse(screen.remove_selected_remote(confirmed=True))
        self.assertIn("No remote selected", screen.status_bar.message)


class TestCveCatalog(GitManagerTestBase):
    def cve_screen(self) -> GitManagerScreen:
        return GitManagerScreen(self.win, self.app, run_command=self.runner, repo_path=str(self.cve_dir))

    def test_candidate_kinds_and_cve_path(self) -> None:
        kinds = [(c.name, c.kind) for c in repository_candidates(self.app.config)]
        self.assertEqual(kinds, [("work", KIND_REPO), ("linux", KIND_LINUX), ("man-pages", KIND_MANPAGES), ("cve", KIND_CVE)])
        self.assertEqual(cve_repo_path(self.app.config), self.cve_dir)
        self.assertEqual(cve_repo_path({"cve": {"repo_dir": ""}}), REPO_ROOT / "cvelistV5")
        self.assertEqual(cve_repo_path({}), REPO_ROOT / "cvelistV5")
        self.assertEqual(cve_repo_path({"cve": {"repo_dir": "data/cves"}}), REPO_ROOT / "data/cves")

    def test_clone_cve_does_not_register_and_keeps_same_repo_dir(self) -> None:
        self.runner.effect = lambda argv: _git_init(Path(argv[-1]))
        screen = self.cve_screen()
        self.assertTrue(screen.clone_cve(url=KERNEL_CVE_URL, confirmed=True))
        self.assertEqual(self.runner.calls[0]["argv"], clone_command(KERNEL_CVE_URL, self.cve_dir))
        self.assertEqual([r["path"] for r in self.app.config["repositories"]], [str(self.work)])
        self.assertEqual(self.app.config["cve"]["repo_dir"], str(self.cve_dir))
        self.assertFalse(self.app.is_dirty)
        self.assertTrue(screen.repo_exists)
        self.assertEqual(screen.current_repo.kind, KIND_CVE)

    def test_clone_cve_sets_repo_dir_when_changed(self) -> None:
        self.runner.effect = lambda argv: _git_init(Path(argv[-1]))
        # Same directory, different spelling: the clone rewrites cve.repo_dir and marks the config dirty
        self.app.config["cve"]["repo_dir"] = str(self.cve_dir) + "/"
        screen = self.cve_screen()
        self.assertTrue(screen.clone_cve(url=DEFAULT_CVE_URL, confirmed=True))
        self.assertEqual(self.app.config["cve"]["repo_dir"], str(self.cve_dir))
        self.assertTrue(self.app.is_dirty)
        self.assertIn("cve.repo_dir set", screen.status_bar.message)
        self.assertFalse(screen.set_cve_repo_dir(self.cve_dir))

    def test_clone_cve_refused_when_present(self) -> None:
        self.cve_dir.mkdir()
        screen = self.cve_screen()
        self.assertFalse(screen.clone_cve(url=DEFAULT_CVE_URL, confirmed=True))
        self.assertEqual(self.runner.calls, [])
        self.assertIn("already exists", screen.status_bar.message)

    def test_c_key_on_cve_uses_default_cvelist(self) -> None:
        self.runner.effect = lambda argv: _git_init(Path(argv[-1]))
        screen = self.cve_screen()
        screen.draw()
        text = " ".join(str(c[1][2]) for c in self.win.calls if c[0] == "addstr")
        self.assertIn("Clone CVE", text)
        self.assertTrue(screen.handle_key(ord("c")))
        self.assertEqual(self.runner.calls[0]["argv"], clone_command(DEFAULT_CVE_URL, self.cve_dir))
        self.assertTrue(screen.repo_exists)

    def test_standard_remotes_refused_on_cve(self) -> None:
        _git_init(self.cve_dir)
        screen = self.cve_screen()
        self.assertEqual(screen.add_standard_remotes(names=[STANDARD_REMOTES[0].name], fetch=False), [])
        self.assertIn("CVE", screen.status_bar.message)
        self.assertEqual(list_remotes(self.cve_dir), [])
        self.assertIn(("A", "Add"), screen._shortcuts())
        # On the CVE catalog `a` skips the Standard/Custom chooser and goes straight to Custom
        with patch("core.tui.screens.git_manager.safe_modal_dialog", return_value="Later") as dialog, \
                patch("core.tui.screens.git_manager.ModalTextInputDialog.show",
                      side_effect=["mirror", "https://example.com/cve.git"]):
            self.assertTrue(screen.handle_key(ord("a")))
        self.assertEqual([c.kwargs.get("title") for c in dialog.call_args_list], ["Fetch New Remotes"])
        self.assertEqual(list_remotes(self.cve_dir), [("mirror", "https://example.com/cve.git")])


class TestGitOpsHelpers(unittest.TestCase):
    def test_repo_dirname_helpers(self) -> None:
        self.assertEqual(repo_dirname_from_url("https://example.com/x/linux-next.git"), "linux-next")
        self.assertEqual(repo_dirname_from_url("https://example.com/x/repo/"), "repo")
        self.assertEqual(repo_dirname_from_url(KERNEL_CVE_URL), "vulns")
        for ok in ("linux", "linux-next", "a.b_c", "9x"):
            self.assertTrue(is_valid_repo_dirname(ok), ok)
        for bad in ("", ".hidden", "a/b", "..", "a..b", "-x", "a b"):
            self.assertFalse(is_valid_repo_dirname(bad), bad)


if __name__ == "__main__":
    unittest.main()


class TestManPages(GitManagerTestBase):
    def dest(self) -> Path:
        return self.repos_dir / "man-pages"

    def manpages_screen(self) -> GitManagerScreen:
        return GitManagerScreen(self.win, self.app, run_command=self.runner, repo_path=str(self.dest()))

    def test_candidate_is_offered_until_configured(self) -> None:
        screen = self.manpages_screen()
        self.assertEqual(screen.current_repo.kind, KIND_MANPAGES)
        self.assertTrue(screen.can_clone_current())
        self.assertIn(("C", "Clone man-pages"), screen._shortcuts())
        screen.draw()
        text = " ".join(str(c[1][2]) for c in self.win.calls if c[0] == "addstr")
        self.assertIn("Clone man-pages", text)
        self.assertIn("Linux man-pages", text)
        # A configured clone at the target path replaces the clone candidate
        _git_init(self.dest())
        self.app.config["repositories"].append({"name": "man-pages", "path": str(self.dest()), "project": "man-pages"})
        kinds = [(c.name, c.kind) for c in repository_candidates(self.app.config)]
        self.assertEqual(kinds, [("work", KIND_REPO), ("man-pages", KIND_REPO), ("linux", KIND_LINUX), ("cve", KIND_CVE)])

    def test_c_key_clones_default_url_and_registers_project(self) -> None:
        self.runner.effect = lambda argv: _git_init(Path(argv[-1]))
        screen = self.manpages_screen()
        # Empty key queue: the URL prompt returns its default, the confirm dialog its default (Clone)
        self.assertTrue(screen.handle_key(ord("c")))
        self.assertEqual(self.runner.calls[0]["argv"], clone_command(DEFAULT_MANPAGES_URL, self.dest()))
        entry = next(r for r in self.app.config["repositories"] if r["path"] == str(self.dest()))
        self.assertEqual(entry["project"], "man-pages")
        self.assertEqual(entry["branch_filter"], {"include": ["master"], "exclude": []})
        self.assertTrue(self.app.is_dirty)
        self.assertTrue(screen.repo_exists)
        self.assertEqual(screen.current_repo.path, self.dest())
        self.assertIn("Cloned", screen.status_bar.message)

    def test_clone_refused_when_present(self) -> None:
        self.dest().mkdir()
        screen = self.make_screen()
        self.assertFalse(screen.clone_manpages(url=DEFAULT_MANPAGES_URL, confirmed=True))
        self.assertEqual(self.runner.calls, [])
        self.assertIn("already exists", screen.status_bar.message)

    def test_standard_remotes_refused_on_manpages(self) -> None:
        _git_init(self.dest())
        self.app.config["repositories"].append({"name": "man-pages", "path": str(self.dest()), "project": "man-pages"})
        screen = self.manpages_screen()
        self.assertEqual(screen.current_repo.kind, KIND_REPO)
        self.assertFalse(screen.current_repo.is_kernel_tree)
        self.assertEqual(screen.add_standard_remotes(names=[STANDARD_REMOTES[0].name], fetch=False), [])
        self.assertIn("man-pages", screen.status_bar.message)
        self.assertEqual(list_remotes(self.dest()), [])
        with patch("core.tui.screens.git_manager.safe_modal_dialog", return_value="Later") as dialog, \
                patch("core.tui.screens.git_manager.ModalTextInputDialog.show",
                      side_effect=["mirror", "https://example.com/man-pages.git"]):
            self.assertTrue(screen.handle_key(ord("a")))
        self.assertEqual([c.kwargs.get("title") for c in dialog.call_args_list], ["Fetch New Remotes"])
        self.assertEqual(list_remotes(self.dest()), [("mirror", "https://example.com/man-pages.git")])
