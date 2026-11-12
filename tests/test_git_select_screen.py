"""tests/test_git_select_screen.py - Unit tests for the Git repository selection hub (GitRepoSelectScreen).

Repositories are throw-away local `git init` directories under a temporary `repos_dir`; clone and
fetch go through an injected fake runner, so no network access or real `repos/` directory is used.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.tui.git_ops import (
    DEFAULT_CVE_URL,
    DEFAULT_LINUX_URL,
    DEFAULT_MANPAGES_URL,
    add_remote,
    clone_command,
    fetch_command,
)
from core.tui.process_view import ProcessResult
from core.tui.screens.git_manager import (
    KIND_CVE,
    KIND_LINUX,
    KIND_MANPAGES,
    GitManagerScreen,
)
from core.tui.screens.git_select import GitRepoSelectScreen
from core.tui.screens.main_menu import MainMenuScreen
from tests.test_git_manager_screen import FakeRunner, _git_init
from tests.test_repos_screen import MockApp
from tests.test_tui import MockCursesWindow


class SequenceRunner(FakeRunner):
    """FakeRunner returning `results` in turn (the last one repeats)."""

    def __init__(self, results: list[ProcessResult]) -> None:
        super().__init__()
        self.results = results

    def __call__(self, stdscr: Any, argv: list[str], title: str, cwd: str | None = None,
                 env: dict[str, str] | None = None, **kw: Any) -> ProcessResult:
        self.result = self.results[min(len(self.calls), len(self.results) - 1)]
        return super().__call__(stdscr, argv, title, cwd=cwd, env=env, **kw)


class GitSelectTestBase(unittest.TestCase):
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

    def make_screen(self) -> GitRepoSelectScreen:
        return GitRepoSelectScreen(self.win, self.app, run_command=self.runner)

    def select(self, screen: GitRepoSelectScreen, name: str) -> None:
        index = next(i for i, r in enumerate(screen.rows) if r.candidate.name == name)
        screen.repo_list.set_selected(index)

    def text(self, win: MockCursesWindow | None = None) -> str:
        return " ".join(str(c[1][2]) for c in (win or self.win).calls if c[0] == "addstr")


class TestRows(GitSelectTestBase):
    def test_rows_state_and_remote_counts(self) -> None:
        add_remote(self.work, "origin", DEFAULT_LINUX_URL)
        add_remote(self.work, "other", "https://example.com/o.git")
        screen = self.make_screen()
        summary = [(r.candidate.name, r.candidate.kind, r.exists, r.remotes) for r in screen.rows]
        self.assertEqual(summary, [
            ("work", "repo", True, 2),
            ("linux", KIND_LINUX, False, 0),
            ("man-pages", KIND_MANPAGES, False, 0),
            ("cve", KIND_CVE, False, 0),
        ])

    def test_enter_pushes_manager_on_highlighted_repo(self) -> None:
        screen = self.make_screen()
        self.select(screen, "cve")
        self.assertTrue(screen.handle_key(10))
        pushed = self.app.screen_stack[-1]
        self.assertIsInstance(pushed, GitManagerScreen)
        self.assertEqual(pushed.current_repo.path, self.cve_dir)
        self.assertEqual(pushed.current_repo.kind, KIND_CVE)
        self.select(screen, "work")
        screen.handle_key(10)
        self.assertEqual(self.app.screen_stack[-1].current_repo.path, self.work)

    def test_esc_pops_and_remembers_selection(self) -> None:
        screen = self.make_screen()
        self.select(screen, "linux")
        self.app.screen_stack.append(screen)
        self.assertTrue(screen.handle_key(27))
        self.assertTrue(self.app.popped)
        again = self.make_screen()
        self.assertEqual(again.selected_row().candidate.name, "linux")

    def test_on_enter_refreshes_rows(self) -> None:
        screen = self.make_screen()
        self.select(screen, "cve")
        _git_init(self.cve_dir)
        screen.on_enter()
        self.assertTrue(screen.selected_row().exists)
        self.assertEqual(screen.selected_row().candidate.name, "cve")

    def test_draw_normal_and_small_terminal(self) -> None:
        screen = self.make_screen()
        self.select(screen, "linux")
        screen.draw()
        text = self.text()
        self.assertIn("Git Repository Manager", text)
        self.assertIn("Fetch all", text)
        self.assertIn("Clone Linux", text)
        self.assertIn("not cloned", text)
        small = MockCursesWindow(10, 40)
        GitRepoSelectScreen(small, self.app, run_command=self.runner).draw()
        self.assertIn("too small", self.text(small))


class TestClone(GitSelectTestBase):
    def test_clone_selected_linux(self) -> None:
        self.runner.effect = lambda argv: _git_init(Path(argv[-1]))
        screen = self.make_screen()
        self.select(screen, "linux")
        # Empty key queue: the URL prompt returns its default, the confirm dialog its default (Clone)
        self.assertTrue(screen.handle_key(ord("c")))
        dest = self.repos_dir / "linux"
        self.assertEqual(self.runner.calls[0]["argv"], clone_command(DEFAULT_LINUX_URL, dest))
        self.assertIn(str(dest), [r["path"] for r in self.app.config["repositories"]])
        self.assertTrue(self.app.is_dirty)
        self.assertEqual(screen.selected_row().candidate.path, dest)
        self.assertTrue(screen.selected_row().exists)
        self.assertIn("Cloned", screen.status_bar.message)

    def test_clone_selected_manpages(self) -> None:
        self.runner.effect = lambda argv: _git_init(Path(argv[-1]))
        screen = self.make_screen()
        self.select(screen, "man-pages")
        screen.draw()
        self.assertIn("Clone man-pages", self.text())
        self.assertIn("[man  ]", self.text())
        # Empty key queue: the URL prompt returns its default, the confirm dialog its default (Clone)
        self.assertTrue(screen.handle_key(ord("c")))
        dest = self.repos_dir / "man-pages"
        self.assertEqual(self.runner.calls[0]["argv"], clone_command(DEFAULT_MANPAGES_URL, dest))
        entry = next(r for r in self.app.config["repositories"] if r["path"] == str(dest))
        self.assertEqual(entry["project"], "man-pages")
        self.assertTrue(screen.selected_row().exists)

    def test_clone_selected_cve(self) -> None:
        self.runner.effect = lambda argv: _git_init(Path(argv[-1]))
        screen = self.make_screen()
        self.select(screen, "cve")
        self.assertTrue(screen.clone_selected())
        self.assertEqual(self.runner.calls[0]["argv"], clone_command(DEFAULT_CVE_URL, self.cve_dir))
        self.assertEqual([r["path"] for r in self.app.config["repositories"]], [str(self.work)])
        row = screen.selected_row()
        self.assertEqual(row.candidate.kind, KIND_CVE)
        self.assertTrue(row.exists)

    def test_clone_refused_for_configured_repo(self) -> None:
        screen = self.make_screen()
        self.select(screen, "work")
        self.assertFalse(screen.clone_selected())
        self.assertEqual(self.runner.calls, [])
        self.assertIn("Nothing to clone", screen.status_bar.message)

    def test_clone_repository_validation(self) -> None:
        screen = self.make_screen()
        self.assertFalse(screen.clone_repository(url="ssh://git@example.com/x.git", name="x", confirmed=True))
        self.assertIn("https://", screen.status_bar.message)
        self.assertFalse(screen.clone_repository(url="https://example.com/x.git", name="../x", confirmed=True))
        self.assertIn("Invalid directory name", screen.status_bar.message)
        self.assertFalse(screen.clone_repository(url="https://example.com/x.git", name="work", confirmed=True))
        self.assertIn("already exists", screen.status_bar.message)
        self.assertEqual(self.runner.calls, [])

    def test_clone_repository_registers_new_repo(self) -> None:
        self.runner.effect = lambda argv: _git_init(Path(argv[-1]))
        screen = self.make_screen()
        url = "https://example.com/trees/linux-next.git"
        # Empty key queue: the directory-name prompt defaults to the URL's last component
        self.assertTrue(screen.clone_repository(url=url, confirmed=True))
        dest = self.repos_dir / "linux-next"
        self.assertEqual(self.runner.calls[0]["argv"], clone_command(url, dest))
        self.assertIn(str(dest), [r["path"] for r in self.app.config["repositories"]])
        self.assertTrue(self.app.is_dirty)
        self.assertEqual(screen.selected_row().candidate.path, dest)

    def test_failed_clone_repository_is_not_registered(self) -> None:
        self.runner.result = ProcessResult(returncode=128)
        screen = self.make_screen()
        self.assertFalse(screen.clone_repository(url="https://example.com/x.git", name="x", confirmed=True))
        self.assertEqual(len(self.app.config["repositories"]), 1)
        self.assertIn("failed", screen.status_bar.message)


class TestAddLocal(GitSelectTestBase):
    def test_add_local_success(self) -> None:
        other = _git_init(self.root / "elsewhere")
        screen = self.make_screen()
        self.assertTrue(screen.add_local_repository(str(other)))
        self.assertIn(str(other), [r["path"] for r in self.app.config["repositories"]])
        self.assertTrue(self.app.is_dirty)
        self.assertEqual(screen.selected_row().candidate.path, other)

    def test_add_local_rejects_non_git_and_duplicate(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        screen = self.make_screen()
        self.assertFalse(screen.add_local_repository(str(plain)))
        self.assertIn("not a git repository", screen.status_bar.message)
        self.assertFalse(screen.add_local_repository(""))
        self.assertFalse(screen.add_local_repository(str(self.work)))
        self.assertIn("already in repositories", screen.status_bar.message)
        self.assertEqual(len(self.app.config["repositories"]), 1)
        self.assertFalse(self.app.is_dirty)


class TestFetchAll(GitSelectTestBase):
    def test_fetch_all_fetches_every_cloned_repo_with_remotes(self) -> None:
        add_remote(self.work, "origin", DEFAULT_LINUX_URL)
        _git_init(self.cve_dir)
        add_remote(self.cve_dir, "origin", DEFAULT_CVE_URL)
        _git_init(self.repos_dir / "linux")  # cloned but without remotes: skipped
        screen = self.make_screen()
        self.assertTrue(screen.handle_key(ord("a")))
        argvs = [c["argv"] for c in self.runner.calls]
        self.assertEqual(argvs, [fetch_command(self.work, None), fetch_command(self.cve_dir, None)])
        self.assertEqual(self.runner.calls[0]["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("Fetched work, cve", screen.status_bar.message)

    def test_fetch_all_without_remotes(self) -> None:
        screen = self.make_screen()
        self.assertEqual(screen.fetch_all(), [])
        self.assertEqual(self.runner.calls, [])
        self.assertIn("No cloned repository", screen.status_bar.message)

    def test_fetch_all_stops_on_cancel(self) -> None:
        add_remote(self.work, "origin", DEFAULT_LINUX_URL)
        _git_init(self.cve_dir)
        add_remote(self.cve_dir, "origin", DEFAULT_CVE_URL)
        self.runner = SequenceRunner([ProcessResult(returncode=-15, cancelled=True)])
        screen = self.make_screen()
        self.assertEqual(screen.fetch_all(), [])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertIn("0/2", screen.status_bar.message)
        self.assertIn("1 not started", screen.status_bar.message)

    def test_fetch_all_continues_after_failure(self) -> None:
        add_remote(self.work, "origin", DEFAULT_LINUX_URL)
        _git_init(self.cve_dir)
        add_remote(self.cve_dir, "origin", DEFAULT_CVE_URL)
        self.runner = SequenceRunner([ProcessResult(returncode=128), ProcessResult(returncode=0)])
        screen = self.make_screen()
        self.assertEqual(screen.fetch_all(), ["cve"])
        self.assertEqual(len(self.runner.calls), 2)
        self.assertIn("failed or cancelled: work", screen.status_bar.message)


class TestMainMenuEntry(GitSelectTestBase):
    def test_menu_item_6_opens_hub_and_save_description(self) -> None:
        menu = MainMenuScreen(self.win, self.app)
        self.assertTrue(menu._activate_key("6"))
        self.assertIsInstance(self.app.screen_stack[-1], GitRepoSelectScreen)
        save = next(item for item in MainMenuScreen.MENU_ITEMS if item[0] == "S")
        self.assertEqual(save[2], "Saves to Active Config File")


if __name__ == "__main__":
    unittest.main()
