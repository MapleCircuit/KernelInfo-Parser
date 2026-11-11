"""core/tui/screens/git_select.py - Repository selection hub in front of the Git Repository Manager.

Lists every repository the Git screens operate on (`repository_candidates()`: configured git
repositories, the `<repos_dir>/linux` clone target and the CVE catalog `cve.repo_dir`) with its
clone state and remote count. `Enter` opens `GitManagerScreen` on the highlighted repository;
the hub also clones the Linux tree or the CVE catalog, adds another repository (clone from a URL
into `<repos_dir>/<name>`, or register an existing local clone) and fetches every remote of every
cloned repository in one pass.
"""

from __future__ import annotations

import curses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import REPO_ROOT
from core.tui.git_ops import (
    fetch_command,
    git_env,
    is_git_repo,
    is_valid_remote_url,
    is_valid_repo_dirname,
    list_remotes,
    repo_dirname_from_url,
)
from core.tui.process_view import ProcessResult, run_streaming_command
from core.tui.screens.base import BaseScreen
from core.tui.screens.git_manager import (
    BRANCH_HINT,
    KIND_CVE,
    KIND_LINUX,
    URL_RULE,
    GitManagerScreen,
    RepoCandidate,
    RunCommand,
    config_repos_dir,
    repository_candidates,
)
from core.tui.screens.repos import ModalTextInputDialog, safe_modal_dialog
from core.tui.theme import PAIR_ERROR, PAIR_HEADER, PAIR_MUTED, PAIR_SUCCESS, get_color
from core.tui.widgets import ScrollableList, StatusBar

MIN_W, MIN_H = 50, 12
KIND_LABELS = {KIND_LINUX: "linux", KIND_CVE: "cve"}
DIRNAME_RULE = "Directory names start with a letter or digit and use only letters, digits, '.', '_' or '-'."


@dataclass
class RepoRow:
    """One hub row: a candidate plus its clone state and remote count."""

    candidate: RepoCandidate
    exists: bool
    remotes: int


def _render_row(row: RepoRow) -> str:
    cand = row.candidate
    kind = KIND_LABELS.get(cand.kind, "repo")
    state = f"{row.remotes} remote(s)" if row.exists else "not cloned"
    return f"{cand.name:<18} [{kind:<5}] {state:<13} {cand.path}"


class GitRepoSelectScreen(BaseScreen):
    """Pick the repository to manage, clone Linux / CVE / other repositories, and fetch everything."""

    VIEW_KEY = "git_select"

    def __init__(self, stdscr: Any, app: Any = None, run_command: RunCommand | None = None) -> None:
        super().__init__(stdscr, app)
        self._run_command = run_command
        self.rows: list[RepoRow] = []
        self._last_yx: tuple[int, int] = (0, 0)

        max_y, max_x = self.get_maxyx()
        self.repo_list = ScrollableList(
            y=4, x=2, height=max(3, max_y - 6), width=max(20, max_x - 4),
            title=" Repositories ", render_item_func=_render_row,
        )
        self.repo_list.is_focused = True
        self.status_bar = StatusBar(y=max_y - 1)
        self.widgets = [self.repo_list]
        self.focus_widget(0)
        self.refresh_rows(prefer_path=self.recall_view(self.VIEW_KEY).get("repo"))

    @property
    def config(self) -> dict[str, Any]:
        cfg = getattr(self.app, "config", None)
        return cfg if isinstance(cfg, dict) else {}

    # --- Rows ---

    def refresh_rows(self, prefer_path: str | None = None) -> list[RepoRow]:
        """Rebuild the rows from the in-memory config and select `prefer_path` (else keep the selection)."""
        current = self.selected_row()
        wanted = prefer_path or (str(current.candidate.path) if current else None)
        rows = []
        for cand in repository_candidates(self.config):
            exists = is_git_repo(cand.path)
            rows.append(RepoRow(cand, exists, len(list_remotes(cand.path)) if exists else 0))
        self.rows = rows
        self.repo_list.set_items(rows, keep_selection=True)
        index = next((i for i, r in enumerate(rows) if wanted and str(r.candidate.path) == wanted), None)
        if index is not None:
            self.repo_list.set_selected(index)
        return rows

    def selected_row(self) -> RepoRow | None:
        return self.repo_list.get_selected()

    def on_enter(self) -> None:
        """Re-read the rows (clones, remotes or configuration may have changed in the manager)."""
        self.refresh_rows()

    def on_leave(self) -> None:
        self._remember()

    def _remember(self) -> None:
        row = self.selected_row()
        self.remember_view(self.VIEW_KEY, repo=str(row.candidate.path) if row else None)

    # --- Helpers ---

    def _manager(self, repo_path: Path | None = None) -> GitManagerScreen:
        """A `GitManagerScreen` on `repo_path`, used for its clone/register actions (not pushed)."""
        return GitManagerScreen(self.stdscr, self.app, run_command=self._run_command,
                                repo_path=str(repo_path) if repo_path else None)

    def _adopt_status(self, manager: GitManagerScreen) -> None:
        self.status_bar.set_message(manager.status_bar.message, color=manager.status_bar.message_color)

    def _error(self, title: str, message: str) -> None:
        safe_modal_dialog(self.stdscr, title=title, message=message, buttons=["OK"])
        self.status_bar.set_message(message.splitlines()[0], color=PAIR_ERROR)

    @staticmethod
    def _prompt(stdscr: Any, title: str, prompt: str, default: str = "") -> str | None:
        value = ModalTextInputDialog(stdscr, title=title, prompt=prompt, default_val=default).show()
        return value.strip() if value is not None else None

    def _run(self, argv: list[str], title: str, cwd: Path | None = None) -> ProcessResult:
        runner = self._run_command or run_streaming_command
        try:
            return runner(self.stdscr, argv, title, cwd=str(cwd) if cwd else None, env=git_env())
        finally:
            self.request_full_redraw()

    # --- Actions ---

    def open_selected(self) -> GitManagerScreen | None:
        """Push `GitManagerScreen` on the highlighted repository."""
        row = self.selected_row()
        if row is None:
            return None
        self._remember()
        screen = self._manager(row.candidate.path)
        if self.app is not None and hasattr(self.app, "push_screen"):
            self.app.push_screen(screen)
        return screen

    def clone_selected(self) -> bool:
        """Clone the highlighted Linux or CVE target when it is missing."""
        row = self.selected_row()
        if row is None:
            return False
        cand = row.candidate
        if cand.kind not in (KIND_LINUX, KIND_CVE) or cand.path.exists():
            self.status_bar.set_message(
                f"Nothing to clone for '{cand.name}' (press N to clone another repository).", color=PAIR_ERROR)
            return False
        manager = self._manager(cand.path)
        ok = manager.clone_current()
        self._adopt_status(manager)
        self.request_full_redraw()
        self.refresh_rows(prefer_path=str(cand.path))
        return ok

    def add_repository(self) -> bool:
        """Add another repository: clone it from a URL or register an existing local clone."""
        choice = safe_modal_dialog(
            self.stdscr, title="Add Repository",
            message=(f"Clone a repository into {config_repos_dir(self.config)}/<name>,\n"
                     "or register a git repository that already exists on disk.\n\n"
                     "Either way it is added to repositories[] (unsaved)."),
            buttons=["Clone URL", "Add local", "Cancel"], default_button=0,
        )
        if choice == "Clone URL":
            return self.clone_repository()
        if choice == "Add local":
            return self.add_local_repository()
        self.status_bar.set_message("Adding repository cancelled.")
        return False

    def clone_repository(self, url: str | None = None, name: str | None = None, confirmed: bool = False) -> bool:
        """Clone `url` into `<repos_dir>/<name>` and register it in repositories[]."""
        if url is None:
            url = self._prompt(self.stdscr, "Clone Repository", "Repository URL (https:// or git://):")
            if url is None:
                self.status_bar.set_message("Clone cancelled.")
                return False
        url = url.strip()
        if not is_valid_remote_url(url):
            self._error("Invalid URL", f"{URL_RULE}.\nGot: '{url}'")
            return False
        if name is None:
            name = self._prompt(self.stdscr, "Clone Repository", f"Directory name under {config_repos_dir(self.config)}:",
                                repo_dirname_from_url(url))
            if name is None:
                self.status_bar.set_message("Clone cancelled.")
                return False
        name = name.strip()
        if not is_valid_repo_dirname(name):
            self._error("Invalid Directory Name", f"Invalid directory name: '{name}'.\n{DIRNAME_RULE}")
            return False
        dest = config_repos_dir(self.config) / name
        if dest.exists():
            self._error("Directory Exists", f"{dest} already exists.\nUse 'Add local' to register an existing clone.")
            return False
        manager = self._manager(dest)
        ok = manager.clone_into(url, dest, "Clone Repository", confirmed=confirmed, register=True)
        self._adopt_status(manager)
        self.request_full_redraw()
        self.refresh_rows(prefer_path=str(dest))
        return ok

    def add_local_repository(self, path: str | None = None) -> bool:
        """Register an existing git repository (relative paths resolve against the project root)."""
        if path is None:
            path = self._prompt(self.stdscr, "Add Local Repository", "Path of an existing git repository:")
            if path is None:
                self.status_bar.set_message("Adding repository cancelled.")
                return False
        raw = Path(path.strip()).expanduser()
        repo = raw if raw.is_absolute() else REPO_ROOT / raw
        if not path.strip() or not is_git_repo(repo):
            self._error("Not a Git Repository", f"'{repo}' is not a git repository.")
            return False
        manager = self._manager(repo)
        if not manager.register_repository(repo):
            self.status_bar.set_message(f"'{repo}' is already in repositories.", color=PAIR_ERROR)
            self.refresh_rows(prefer_path=str(repo))
            return False
        self.refresh_rows(prefer_path=str(repo))
        self.status_bar.set_message(f"Registered {repo} in repositories (unsaved). {BRANCH_HINT}", color=PAIR_SUCCESS)
        return True

    def fetch_all(self) -> list[str]:
        """`git fetch --all --tags` in every cloned repository with remotes; stops on cancel."""
        targets = [r for r in self.rows if r.exists and r.remotes]
        if not targets:
            self.status_bar.set_message("No cloned repository has remotes to fetch.", color=PAIR_ERROR)
            return []
        done: list[str] = []
        failed: list[str] = []
        for i, row in enumerate(targets, 1):
            cand = row.candidate
            title = f"Fetching all remotes of {cand.name} ({i}/{len(targets)})"
            result = self._run(fetch_command(cand.path, None), title, cwd=cand.path)
            if result.ok:
                done.append(cand.name)
            else:
                failed.append(cand.name)
                if result.cancelled:
                    break
        self.refresh_rows()
        skipped = len(targets) - len(done) - len(failed)
        if failed or skipped:
            extra = f"; {skipped} not started" if skipped else ""
            self.status_bar.set_message(
                f"Fetched {len(done)}/{len(targets)} repositories; failed or cancelled: {', '.join(failed)}{extra}.",
                color=PAIR_ERROR)
        else:
            self.status_bar.set_message(f"Fetched {', '.join(done)}. {BRANCH_HINT}", color=PAIR_SUCCESS)
        return done

    # --- Rendering & keys ---

    def _shortcuts(self) -> list[tuple[str, str]]:
        keys = [("Enter", "Manage"), ("A", "Fetch all"), ("N", "Add repo")]
        row = self.selected_row()
        if row and row.candidate.kind in (KIND_LINUX, KIND_CVE) and not row.candidate.path.exists():
            keys.insert(1, ("C", "Clone CVE" if row.candidate.kind == KIND_CVE else "Clone Linux"))
        return [*keys, ("Esc", "Back"), ("↑/↓", "Navigate")]

    def draw(self) -> None:
        """Render the repository rows and status bar."""
        self.clear()
        max_y, max_x = self.get_maxyx()
        if max_y < MIN_H or max_x < MIN_W:
            self.safe_addstr(0, 0, f"Terminal window too small (min {MIN_W}x{MIN_H}).", get_color(PAIR_ERROR))
            self.refresh()
            return
        if (max_y, max_x) != self._last_yx:
            self._last_yx = (max_y, max_x)
            self.repo_list.height = max(3, max_y - 6)
            self.repo_list.width = max(20, max_x - 4)
            self.status_bar.y = max_y - 1

        header = " Git Repository Manager "
        self.safe_addstr(0, max(2, (max_x - len(header)) // 2), header, get_color(PAIR_HEADER) | curses.A_BOLD)
        cloned = sum(1 for r in self.rows if r.exists)
        summary = f" {len(self.rows)} repositories, {cloned} cloned - select one to manage its remotes"
        self.safe_addstr(2, 2, summary[: max_x - 4], get_color(PAIR_MUTED))

        self.repo_list.draw(self.stdscr)
        self.status_bar.set_shortcuts(self._shortcuts())
        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Dispatch hub hotkeys; navigation keys go to the repository list."""
        self.status_bar.set_message(None)
        if ch == 27:
            self._remember()
            if self.app is not None and hasattr(self.app, "pop_screen"):
                self.app.pop_screen()
            return True
        if ch in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
            self.open_selected()
            return True
        if ch in (ord("c"), ord("C")):
            self.clone_selected()
            return True
        if ch in (ord("n"), ord("N")):
            self.add_repository()
            return True
        if ch in (ord("a"), ord("A")):
            self.fetch_all()
            return True
        return super().handle_key(ch)


__all__ = ["GitRepoSelectScreen", "RepoRow"]
