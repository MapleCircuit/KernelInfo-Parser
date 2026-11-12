"""core/tui/screens/git_manager.py - Git Repository Manager screen.

Operates on one repository at a time (configured git repositories, the `<repos_dir>/linux` and
`<repos_dir>/man-pages` clone targets and the CVE catalog `cve.repo_dir`; `repository_candidates()`, also
listed by `core/tui/screens/git_select.py`): clones the torvalds tree, the Linux man-pages or the CVE
catalog, adds standard or custom remotes, fetches one or all remotes, and removes remotes after confirmation. Clone and fetch run through
`core.tui.process_view.run_streaming_command()` so progress stays visible and cancellable.
"""

from __future__ import annotations

import curses
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import (
    MANPAGES_PROJECT,
    REPO_ROOT,
    detect_repository,
    resolve_repo_path,
)
from core.tui.git_ops import (
    DEFAULT_CVE_DIRNAME,
    DEFAULT_CVE_URL,
    DEFAULT_LINUX_DIRNAME,
    DEFAULT_LINUX_URL,
    DEFAULT_MANPAGES_DIRNAME,
    DEFAULT_MANPAGES_URL,
    KERNEL_CVE_URL,
    STANDARD_REMOTES,
    RemotePreset,
    add_remote,
    clone_command,
    fetch_command,
    git_env,
    is_git_repo,
    is_valid_remote_name,
    is_valid_remote_url,
    list_remotes,
    remove_remote,
)
from core.tui.process_view import ProcessResult, run_streaming_command
from core.tui.screens.base import BaseScreen
from core.tui.screens.repos import ModalTextInputDialog, safe_modal_dialog
from core.tui.theme import (
    PAIR_ACTIVE,
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
from core.tui.widgets import ScrollableList, StatusBar

MIN_W, MIN_H = 50, 12
BRANCH_HINT = "Select new branches in 'Repositories, Git & Branch Filters'."
URL_RULE = "URL must start with https:// or git://"
NAME_RULE = "Remote names start with a letter or digit and use only letters, digits, '.', '_' or '-'."

RunCommand = Callable[..., ProcessResult]


KIND_REPO = "repo"    # a configured repositories[] entry
KIND_LINUX = "linux"  # the `<repos_dir>/linux` clone target (not configured yet)
KIND_MANPAGES = "man-pages"  # the `<repos_dir>/man-pages` clone target (not configured yet)
KIND_CVE = "cve"      # the CVE catalog clone (`cve.repo_dir`)
# Clone targets the Git screens offer to clone when their directory is missing
CLONE_KINDS = (KIND_LINUX, KIND_MANPAGES, KIND_CVE)
CLONE_LABELS = {KIND_LINUX: "Clone Linux", KIND_MANPAGES: "Clone man-pages", KIND_CVE: "Clone CVE"}


@dataclass
class RepoCandidate:
    """A repository the Git screens can operate on."""

    name: str
    path: Path
    entry: dict[str, Any] | None = None
    kind: str = KIND_REPO

    @property
    def is_kernel_tree(self) -> bool:
        """False for the CVE catalog and the man-pages tree (target or configured `project: "man-pages"`),
        which the kernel `STANDARD_REMOTES` do not apply to."""
        if self.kind in (KIND_CVE, KIND_MANPAGES):
            return False
        return str((self.entry or {}).get("project") or "") != MANPAGES_PROJECT


def config_repos_dir(config: dict[str, Any]) -> Path:
    """`workspace.repos_dir` of `config` (relative paths resolve against REPO_ROOT)."""
    raw = str((config.get("workspace") or {}).get("repos_dir") or "repos")
    p = Path(raw).expanduser()
    return p if p.is_absolute() else REPO_ROOT / p


def cve_repo_path(config: dict[str, Any]) -> Path:
    """Path of the CVE catalog clone: `cve.repo_dir` (relative to REPO_ROOT; empty -> `cvelistV5`)."""
    raw = str((config.get("cve") or {}).get("repo_dir") or "").strip() or DEFAULT_CVE_DIRNAME
    p = Path(raw).expanduser()
    return p if p.is_absolute() else REPO_ROOT / p


def repository_candidates(config: dict[str, Any]) -> list[RepoCandidate]:
    """Configured git repositories, then the Linux and man-pages clone targets and the CVE catalog unless
    already listed."""
    cands: list[RepoCandidate] = []
    seen: set[Path] = set()
    for entry in config.get("repositories") or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        path = resolve_repo_path(entry)
        key = path.resolve()
        if key in seen or not is_git_repo(path):
            continue
        seen.add(key)
        cands.append(RepoCandidate(str(entry.get("name") or path.name), path, entry))
    for dirname, kind in ((DEFAULT_LINUX_DIRNAME, KIND_LINUX), (DEFAULT_MANPAGES_DIRNAME, KIND_MANPAGES)):
        target = config_repos_dir(config) / dirname
        if target.resolve() not in seen:
            seen.add(target.resolve())
            cands.append(RepoCandidate(dirname, target, None, kind))
    cve = cve_repo_path(config)
    if cve.resolve() not in seen:
        cands.append(RepoCandidate("cve", cve, None, KIND_CVE))
    return cands


def remote_url_key(url: str) -> str:
    """Scheme-less, lower-case URL without trailing `/` or `.git`, for duplicate detection."""
    key = (url or "").strip().lower()
    if "://" in key:
        key = key.split("://", 1)[1]
    key = key.rstrip("/")
    return key.removesuffix(".git")


def preset_is_present(preset: RemotePreset, remotes: list[tuple[str, str]]) -> bool:
    """True when a remote already uses the preset's name or URL."""
    url_key = remote_url_key(preset.url)
    return any(name == preset.name or remote_url_key(url) == url_key for name, url in remotes)


def _render_remote(item: tuple[str, str]) -> str:
    name, url = item
    return f"{name:<16} {url}"


class StandardRemotesDialog:
    """Modal checklist of `STANDARD_REMOTES`; presets already among the remotes are not toggleable."""

    def __init__(
        self,
        stdscr: Any,
        presets: tuple[RemotePreset, ...] | list[RemotePreset],
        remotes: list[tuple[str, str]],
    ) -> None:
        self.stdscr = stdscr
        self.presets = list(presets)
        self.present = [preset_is_present(p, remotes) for p in self.presets]
        self.checked: set[str] = set()
        self.cursor = next((i for i, p in enumerate(self.present) if not p), 0)
        self.scroll = 0

    def toggle(self, index: int) -> bool:
        """Toggle the preset at `index`; returns False for an already-added preset."""
        if not (0 <= index < len(self.presets)) or self.present[index]:
            return False
        name = self.presets[index].name
        if name in self.checked:
            self.checked.discard(name)
        else:
            self.checked.add(name)
        return True

    def selected(self) -> list[RemotePreset]:
        """Checked presets in list order."""
        return [p for p in self.presets if p.name in self.checked]

    def handle_key(self, ch: int) -> tuple[bool, list[RemotePreset] | None]:
        """Process a key; returns (done, chosen presets or None when cancelled)."""
        if ch == 27:
            return True, None
        if ch in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(len(self.presets) - 1, self.cursor + 1)
        elif ch == ord(" "):
            self.toggle(self.cursor)
        elif ch in (10, 13, curses.KEY_ENTER):
            return True, self.selected()
        return False, None

    def draw(self) -> None:
        try:
            max_y, max_x = self.stdscr.getmaxyx()
        except Exception:
            max_y, max_x = 24, 80
        w = max(30, min(78, max_x - 4))
        h = max(6, min(len(self.presets) + 6, max_y - 2))
        y = max(0, (max_y - h) // 2)
        x = max(0, (max_x - w) // 2)
        rows = max(1, h - 5)
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + rows:
            self.scroll = self.cursor - rows + 1

        for r in range(1, h - 1):
            safe_addstr(self.stdscr, y + r, x + 1, " " * (w - 2), get_color(PAIR_DEFAULT))
        draw_box(self.stdscr, y, x, h, w, title=" Add Standard Remotes ", color_pair=PAIR_ACTIVE,
                 title_align="center")
        inner = w - 4
        for r in range(rows):
            idx = self.scroll + r
            if idx >= len(self.presets):
                break
            preset = self.presets[idx]
            if self.present[idx]:
                mark, attr = "[-]", get_color(PAIR_MUTED)
                text = f"{mark} {preset.name:<11} (added)  {preset.description}"
            else:
                mark = "[X]" if preset.name in self.checked else "[ ]"
                attr = get_color(PAIR_DEFAULT)
                text = f"{mark} {preset.name:<11} {preset.description}"
            if idx == self.cursor:
                attr = get_color(PAIR_HIGHLIGHT) | curses.A_BOLD
            safe_addstr(self.stdscr, y + 2 + r, x + 2, text[:inner].ljust(inner), attr)
        footer = "Space toggle   Enter add checked   Esc cancel"
        safe_addstr(self.stdscr, y + h - 2, x + 2, footer[:inner], get_color(PAIR_MUTED))

    def show(self) -> list[RemotePreset] | None:
        """Run the modal loop; returns the checked presets, or None when cancelled."""
        if hasattr(self.stdscr, "keys_queue") and not self.stdscr.keys_queue:
            return None
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
                return None
            if ch == -1:
                return None
            done, result = self.handle_key(ch)
            if done:
                return result


class GitManagerScreen(BaseScreen):
    """Clone the Linux tree and manage/fetch the remotes of one repository at a time."""

    VIEW_KEY = "git_manager"

    def __init__(
        self, stdscr: Any, app: Any = None, run_command: RunCommand | None = None, repo_path: str | None = None
    ) -> None:
        super().__init__(stdscr, app)
        self._run_command = run_command
        self.candidates: list[RepoCandidate] = []
        self.repo_index = 0
        self.remotes: list[tuple[str, str]] = []
        self.repo_exists = False
        self._last_yx: tuple[int, int] = (0, 0)

        max_y, max_x = self.get_maxyx()
        self.remote_list = ScrollableList(
            y=6, x=2, height=max(3, max_y - 8), width=max(20, max_x - 4),
            title=" Remotes ", render_item_func=_render_remote,
        )
        self.remote_list.is_focused = True
        self.status_bar = StatusBar(y=max_y - 1)
        self.widgets = [self.remote_list]
        self.focus_widget(0)

        view = self.recall_view(self.VIEW_KEY)
        self.reload_candidates(prefer_path=repo_path or view.get("repo"))
        if not repo_path or repo_path == view.get("repo"):
            self._select_remote_by_name(view.get("remote"))

    # --- Repository selection ---

    @property
    def config(self) -> dict[str, Any]:
        cfg = getattr(self.app, "config", None)
        return cfg if isinstance(cfg, dict) else {}

    def repos_dir(self) -> Path:
        """`workspace.repos_dir` of the TUI's in-memory config (relative paths resolve against REPO_ROOT)."""
        return config_repos_dir(self.config)

    def clone_target(self) -> Path:
        """Destination of the default Linux clone: `<repos_dir>/linux`."""
        return self.repos_dir() / DEFAULT_LINUX_DIRNAME

    def manpages_target(self) -> Path:
        """Destination of the default man-pages clone: `<repos_dir>/man-pages`."""
        return self.repos_dir() / DEFAULT_MANPAGES_DIRNAME

    def reload_candidates(self, prefer_path: str | None = None) -> None:
        """Rebuild the repository candidates and select `prefer_path`, else `linux`, else the first."""
        current = str(self.current_repo.path) if self.candidates else None
        cands = repository_candidates(self.config)
        self.candidates = cands

        wanted = prefer_path or current
        index = next((i for i, c in enumerate(cands) if wanted and str(c.path) == wanted), None)
        if index is None:
            index = next((i for i, c in enumerate(cands) if c.kind == KIND_LINUX), None)
        if index is None:
            index = next((i for i, c in enumerate(cands) if c.name == DEFAULT_LINUX_DIRNAME), 0)
        self.repo_index = index
        self.refresh_remotes(keep_selection=False)

    @property
    def current_repo(self) -> RepoCandidate:
        return self.candidates[self.repo_index]

    def switch_repo(self, step: int = 1) -> RepoCandidate:
        """Select the next (or previous) repository candidate."""
        if self.candidates:
            self.repo_index = (self.repo_index + step) % len(self.candidates)
        self.refresh_remotes(keep_selection=False)
        self.status_bar.set_message(f"Repository: {self.current_repo.name} ({self.current_repo.path})")
        return self.current_repo

    def refresh_remotes(self, keep_selection: bool = True) -> list[tuple[str, str]]:
        """Re-read the remotes of the current repository."""
        repo = self.current_repo
        self.repo_exists = is_git_repo(repo.path)
        self.remotes = list_remotes(repo.path) if self.repo_exists else []
        self.remote_list.set_items(self.remotes, keep_selection=keep_selection)
        return self.remotes

    def selected_remote(self) -> tuple[str, str] | None:
        return self.remote_list.get_selected()

    def _select_remote_by_name(self, name: str | None) -> None:
        for i, (n, _url) in enumerate(self.remotes):
            if n == name:
                self.remote_list.set_selected(i)
                return

    def _remember(self) -> None:
        remote = self.selected_remote()
        self.remember_view(self.VIEW_KEY, repo=str(self.current_repo.path),
                           remote=remote[0] if remote else None)

    def on_enter(self) -> None:
        """Re-read candidates and remotes (configuration may have changed elsewhere)."""
        remote = self.selected_remote()
        self.reload_candidates()
        self._select_remote_by_name(remote[0] if remote else None)

    def on_leave(self) -> None:
        self._remember()

    # --- Helpers ---

    def _run(self, argv: list[str], title: str, cwd: Path | None = None) -> ProcessResult:
        runner = self._run_command or run_streaming_command
        try:
            return runner(self.stdscr, argv, title, cwd=str(cwd) if cwd else None, env=git_env())
        finally:
            self.request_full_redraw()

    def _error(self, title: str, message: str) -> None:
        safe_modal_dialog(self.stdscr, title=title, message=message, buttons=["OK"])
        self.status_bar.set_message(message.splitlines()[0], color=PAIR_ERROR)

    def _require_repo(self) -> bool:
        if self.repo_exists:
            return True
        hint = " (press C to clone)" if self.can_clone_current() else ""
        self.status_bar.set_message(
            f"'{self.current_repo.path}' is not a git repository yet{hint}.", color=PAIR_ERROR)
        return False

    @staticmethod
    def _prompt(stdscr: Any, title: str, prompt: str, default: str = "") -> str | None:
        value = ModalTextInputDialog(stdscr, title=title, prompt=prompt, default_val=default).show()
        return value.strip() if value is not None else None

    # --- Clone ---

    def register_repository(self, path: Path) -> bool:
        """Add the repository at `path` to `app.config["repositories"]` unless its path is known."""
        entry = detect_repository(path)
        repos = self.config.setdefault("repositories", []) if self.app is not None else []
        target = path.resolve()
        for existing in repos:
            if not isinstance(existing, dict):
                continue
            if existing.get("path") == entry.get("path") or (
                existing.get("path") and resolve_repo_path(existing).resolve() == target
            ):
                return False
        repos.append(entry)
        if self.app is not None:
            self.app.is_dirty = True
        return True

    def set_cve_repo_dir(self, path: Path) -> bool:
        """Point `cve.repo_dir` at `path` (relative to REPO_ROOT when inside it); True when it changed."""
        try:
            value = str(path.resolve().relative_to(REPO_ROOT.resolve()))
        except ValueError:
            value = str(path)
        cve_cfg = self.config.setdefault("cve", {}) if self.app is not None else {}
        if not isinstance(cve_cfg, dict) or str(cve_cfg.get("repo_dir") or "") == value:
            return False
        cve_cfg["repo_dir"] = value
        if self.app is not None:
            self.app.is_dirty = True
        return True

    def can_clone_current(self) -> bool:
        """True when the current candidate is the Linux, man-pages or CVE clone target and its directory is missing."""
        repo = self.current_repo
        return repo.kind in CLONE_KINDS and not repo.path.exists()

    def clone_current(self) -> bool:
        """Clone the current candidate (Linux tree, man-pages or CVE catalog) when its directory is missing."""
        repo = self.current_repo
        if repo.kind == KIND_CVE:
            return self.clone_cve()
        if repo.kind == KIND_LINUX:
            return self.clone_linux()
        if repo.kind == KIND_MANPAGES:
            return self.clone_manpages()
        self.status_bar.set_message(f"'{repo.name}' is already present at {repo.path}.", color=PAIR_ERROR)
        return False

    def clone_linux(self, url: str | None = None, confirmed: bool = False) -> bool:
        """Clone the Linux tree into `<repos_dir>/linux` and register it; returns True on success."""
        dest = self.clone_target()
        if dest.exists():
            self.status_bar.set_message(f"Clone target already exists: {dest}", color=PAIR_ERROR)
            return False
        if url is None:
            url = self._prompt(self.stdscr, "Clone Linux", "Repository URL to clone:", DEFAULT_LINUX_URL)
            if url is None:
                self.status_bar.set_message("Clone cancelled.")
                return False
        return self.clone_into(url, dest, "Clone Linux", confirmed=confirmed, register=True,
                               size_note="This is a multi-GB download and can take a long time.")

    def clone_manpages(self, url: str | None = None, confirmed: bool = False) -> bool:
        """Clone the Linux man-pages into `<repos_dir>/man-pages` and register it; returns True on success."""
        dest = self.manpages_target()
        if dest.exists():
            self.status_bar.set_message(f"Clone target already exists: {dest}", color=PAIR_ERROR)
            return False
        if url is None:
            url = self._prompt(self.stdscr, "Clone man-pages", "Repository URL to clone:", DEFAULT_MANPAGES_URL)
            if url is None:
                self.status_bar.set_message("Clone cancelled.")
                return False
        return self.clone_into(url, dest, "Clone man-pages", confirmed=confirmed, register=True,
                               size_note="This is a download of about 50 MB.")

    def clone_cve(self, url: str | None = None, confirmed: bool = False) -> bool:
        """Clone a CVE catalog into the `cve.repo_dir` path and point `cve.repo_dir` at it."""
        dest = cve_repo_path(self.config)
        if dest.exists():
            self.status_bar.set_message(f"CVE catalog directory already exists: {dest}", color=PAIR_ERROR)
            return False
        if url is None:
            choice = safe_modal_dialog(
                self.stdscr, title="Clone CVE Catalog",
                message=(f"Clone a CVE catalog into\n{dest}\n\n"
                         f"cvelistV5: CVE Program list ({DEFAULT_CVE_URL})\n"
                         f"vulns.git: kernel CNA records ({KERNEL_CVE_URL})"),
                buttons=["cvelistV5", "vulns.git", "Custom URL", "Cancel"], default_button=0,
            )
            if choice == "cvelistV5":
                url = DEFAULT_CVE_URL
            elif choice == "vulns.git":
                url = KERNEL_CVE_URL
            elif choice == "Custom URL":
                url = self._prompt(self.stdscr, "Clone CVE Catalog", "Repository URL to clone:", DEFAULT_CVE_URL)
            if url is None:
                self.status_bar.set_message("Clone cancelled.")
                return False
        ok = self.clone_into(url, dest, "Clone CVE Catalog", confirmed=confirmed, register=False,
                             size_note="This is a multi-GB download and can take a long time.")
        if ok and self.set_cve_repo_dir(dest):
            self.status_bar.set_message(f"Cloned {dest}; cve.repo_dir set to it (unsaved).", color=PAIR_SUCCESS)
        return ok

    def clone_into(
        self, url: str, dest: Path, title: str, confirmed: bool = False, register: bool = True,
        size_note: str = "",
    ) -> bool:
        """Validate `url`, confirm, clone it into `dest` and optionally register it in repositories[]."""
        url = (url or "").strip()
        if not is_valid_remote_url(url):
            self._error("Invalid URL", f"{URL_RULE}.\nGot: '{url}'")
            return False
        if dest.exists():
            self.status_bar.set_message(f"Clone target already exists: {dest}", color=PAIR_ERROR)
            return False
        if not confirmed:
            note = f"\n\n{size_note}" if size_note else ""
            choice = safe_modal_dialog(
                self.stdscr, title=title,
                message=f"Clone {url}\ninto {dest}?{note}",
                buttons=["Clone", "Cancel"], default_button=0,
            )
            if choice != "Clone":
                self.status_bar.set_message("Clone cancelled.")
                return False
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._error("Clone Failed", f"Cannot create {dest.parent}: {exc}")
            return False

        result = self._run(clone_command(url, dest), f"Cloning {dest.name}", cwd=dest.parent)
        if result.ok and is_git_repo(dest):
            added = self.register_repository(dest) if register else False
            self.reload_candidates(prefer_path=str(dest))
            note = " and registered it in repositories" if added else ""
            hint = f" {BRANCH_HINT}" if register else ""
            self.status_bar.set_message(f"Cloned {dest}{note}.{hint}", color=PAIR_SUCCESS)
            return True

        if result.cancelled:
            reason = "Clone cancelled"
        elif result.error:
            reason = f"Clone failed: {result.error}"
        else:
            reason = f"Clone failed (exit {result.returncode})"
        if dest.exists():
            choice = safe_modal_dialog(
                self.stdscr, title="Partial Clone",
                message=f"{reason}.\nA partial directory remains at:\n{dest}\n\nDelete it?",
                buttons=["Keep", "Delete"], default_button=0,
            )
            if choice == "Delete":
                try:
                    shutil.rmtree(dest)
                    reason += "; partial directory deleted"
                except OSError as exc:
                    reason += f"; could not delete partial directory: {exc}"
            else:
                reason += f"; partial directory kept at {dest}"
        self.reload_candidates(prefer_path=str(dest))
        self.status_bar.set_message(reason + ".", color=PAIR_ERROR)
        return False

    # --- Fetch ---

    def fetch(self, remote: str | None = None) -> ProcessResult | None:
        """`git fetch` one remote, or every remote when `remote` is None."""
        if not self._require_repo():
            return None
        if not self.remotes:
            self.status_bar.set_message("No remotes to fetch.", color=PAIR_ERROR)
            return None
        label = remote or "all remotes"
        repo = self.current_repo.path
        result = self._run(fetch_command(repo, remote), f"Fetching {label}", cwd=repo)
        self.refresh_remotes()
        if result.ok:
            self.status_bar.set_message(f"Fetched {label}. {BRANCH_HINT}", color=PAIR_SUCCESS)
        elif result.cancelled:
            self.status_bar.set_message(f"Fetch of {label} cancelled.", color=PAIR_ERROR)
        else:
            detail = result.error or f"exit {result.returncode}"
            self.status_bar.set_message(f"Fetch of {label} failed ({detail}).", color=PAIR_ERROR)
        return result

    def fetch_selected(self) -> ProcessResult | None:
        remote = self.selected_remote()
        if remote is None:
            self.status_bar.set_message("No remote selected.", color=PAIR_ERROR)
            return None
        return self.fetch(remote[0])

    def fetch_many(self, names: list[str]) -> list[str]:
        """Fetch each remote in turn (stops on cancel); returns the names fetched successfully."""
        done: list[str] = []
        failed: list[str] = []
        repo = self.current_repo.path
        for name in names:
            result = self._run(fetch_command(repo, name), f"Fetching {name}", cwd=repo)
            if result.ok:
                done.append(name)
            else:
                failed.append(name)
                if result.cancelled:
                    break
        self.refresh_remotes()
        if failed:
            self.status_bar.set_message(
                f"Fetched {len(done)}/{len(names)} remotes; failed or cancelled: {', '.join(failed)}.",
                color=PAIR_ERROR)
        else:
            self.status_bar.set_message(f"Fetched {', '.join(done)}. {BRANCH_HINT}", color=PAIR_SUCCESS)
        return done

    def _offer_fetch(self, names: list[str], summary: str, fetch: bool | None) -> None:
        if fetch is None:
            choice = safe_modal_dialog(
                self.stdscr, title="Fetch New Remotes",
                message=f"{summary}\n\nFetch the new remotes now?",
                buttons=["Fetch now", "Later"], default_button=0,
            )
            fetch = choice == "Fetch now"
        if fetch:
            self.fetch_many(names)

    # --- Add / remove remotes ---

    def add_standard_remotes(self, names: list[str] | None = None, fetch: bool | None = None) -> list[str]:
        """Add the chosen `STANDARD_REMOTES` (dialog when `names` is None); returns the names added."""
        if not self._require_repo():
            return []
        if not self.current_repo.is_kernel_tree:
            what = "the CVE catalog" if self.current_repo.kind == KIND_CVE else "the man-pages tree"
            self.status_bar.set_message(f"Standard kernel remotes do not apply to {what}.", color=PAIR_ERROR)
            return []
        if names is None:
            chosen = StandardRemotesDialog(self.stdscr, STANDARD_REMOTES, self.remotes).show()
            self.request_full_redraw()
            if chosen is None:
                self.status_bar.set_message("Adding standard remotes cancelled.")
                return []
        else:
            chosen = [p for p in STANDARD_REMOTES if p.name in names]
        chosen = [p for p in chosen if not preset_is_present(p, self.remotes)]
        if not chosen:
            self.status_bar.set_message("No standard remotes selected.")
            return []

        repo = self.current_repo.path
        added: list[str] = []
        failures: list[str] = []
        for preset in chosen:
            ok, msg = add_remote(repo, preset.name, preset.url)
            if ok:
                added.append(preset.name)
            else:
                failures.append(f"{preset.name}: {msg}")
        self.refresh_remotes()
        self._select_remote_by_name(added[-1] if added else None)

        lines = []
        if added:
            lines.append(f"Added: {', '.join(added)}")
        if failures:
            lines.append("Failed:\n" + "\n".join(failures))
        summary = "\n".join(lines)
        if not added:
            self._error("Add Remotes Failed", summary)
            return []
        self.status_bar.set_message(summary.splitlines()[0] + (" (some failed)" if failures else ""),
                                    color=PAIR_ERROR if failures else PAIR_SUCCESS)
        self._offer_fetch(added, summary, fetch)
        return added

    def add_remote_menu(self) -> bool:
        """Ask Standard vs Custom (Custom only for the CVE catalog and the man-pages tree) and run that add flow."""
        if not self._require_repo():
            return False
        if not self.current_repo.is_kernel_tree:
            return self.add_custom_remote()
        choice = safe_modal_dialog(
            self.stdscr, title="Add Remote",
            message=("Standard: pick kernel trees from the preset list.\n"
                     "Custom: enter a remote name and URL."),
            buttons=["Standard", "Custom", "Cancel"], default_button=0,
        )
        self.request_full_redraw()
        if choice == "Standard":
            return bool(self.add_standard_remotes())
        if choice == "Custom":
            return self.add_custom_remote()
        self.status_bar.set_message("Adding remote cancelled.")
        return False

    def add_custom_remote(
        self, name: str | None = None, url: str | None = None, fetch: bool | None = None
    ) -> bool:
        """Add a remote by name + URL (prompted when omitted); the URL must be https:// or git://."""
        if not self._require_repo():
            return False
        if name is None:
            name = self._prompt(self.stdscr, "Add Custom Remote", "Remote name:")
            if name is None:
                self.status_bar.set_message("Adding remote cancelled.")
                return False
        name = name.strip()
        if not is_valid_remote_name(name):
            self._error("Invalid Remote Name", f"Invalid remote name: '{name}'.\n{NAME_RULE}")
            return False
        if any(n == name for n, _ in self.remotes):
            self._error("Remote Exists", f"Remote '{name}' already exists.")
            return False
        if url is None:
            url = self._prompt(self.stdscr, "Add Custom Remote", f"URL for '{name}' (https:// or git://):")
            if url is None:
                self.status_bar.set_message("Adding remote cancelled.")
                return False
        url = url.strip()
        if not is_valid_remote_url(url):
            self._error("Invalid Remote URL", f"{URL_RULE}.\nGot: '{url}'")
            return False
        ok, msg = add_remote(self.current_repo.path, name, url)
        self.refresh_remotes()
        if not ok:
            self._error("Add Remote Failed", msg)
            return False
        self._select_remote_by_name(name)
        self.status_bar.set_message(msg, color=PAIR_SUCCESS)
        self._offer_fetch([name], msg, fetch)
        return True

    def remove_selected_remote(self, confirmed: bool = False) -> bool:
        """Remove the highlighted remote after a confirmation dialog (default button: Cancel)."""
        if not self._require_repo():
            return False
        remote = self.selected_remote()
        if remote is None:
            self.status_bar.set_message("No remote selected.", color=PAIR_ERROR)
            return False
        name, url = remote
        if not confirmed:
            choice = safe_modal_dialog(
                self.stdscr, title="Remove Remote",
                message=(f"Remove remote '{name}' ({url})\nfrom {self.current_repo.path}?\n\n"
                         "Its remote-tracking branches are deleted as well."),
                buttons=["Remove", "Cancel"], default_button=1,
            )
            if choice != "Remove":
                self.status_bar.set_message("Remove cancelled.")
                return False
        ok, msg = remove_remote(self.current_repo.path, name)
        self.refresh_remotes()
        self.status_bar.set_message(msg, color=PAIR_SUCCESS if ok else PAIR_ERROR)
        return ok

    # --- Rendering & keys ---

    def _clone_label(self) -> str:
        return CLONE_LABELS.get(self.current_repo.kind, "Clone Linux")

    def _shortcuts(self) -> list[tuple[str, str]]:
        if not self.repo_exists:
            keys = [("R", "Repo")]
            if self.can_clone_current():
                keys.insert(0, ("C", self._clone_label()))
            return [*keys, ("Esc", "Back")]
        return [("A", "Add"), ("F", "Fetch"), ("G", "Fetch all"), ("D", "Remove"),
                ("Esc", "Back"), ("R", "Repo"), ("↑/↓", "Navigate")]

    def draw(self) -> None:
        """Render repository info, remote list and status bar."""
        self.clear()
        max_y, max_x = self.get_maxyx()
        if max_y < MIN_H or max_x < MIN_W:
            self.safe_addstr(0, 0, f"Terminal window too small (min {MIN_W}x{MIN_H}).", get_color(PAIR_ERROR))
            self.refresh()
            return
        if (max_y, max_x) != self._last_yx:
            self._last_yx = (max_y, max_x)
            self.remote_list.height = max(3, max_y - 8)
            self.remote_list.width = max(20, max_x - 4)
            self.status_bar.y = max_y - 1

        header = " Git Repository Manager "
        self.safe_addstr(0, max(2, (max_x - len(header)) // 2), header, get_color(PAIR_HEADER) | curses.A_BOLD)

        repo = self.current_repo
        exists = self.repo_exists
        count = f"({self.repo_index + 1}/{len(self.candidates)})"
        self.safe_addstr(2, 2, f" Repository: {repo.name} {count}   [R] switch "[: max_x - 4],
                         get_color(PAIR_ACTIVE) | curses.A_BOLD)
        self.safe_addstr(3, 2, f" Path: {repo.path}"[: max_x - 4], get_color(PAIR_DEFAULT))
        if exists and repo.kind == KIND_CVE:
            state = f" CVE catalog (cve.repo_dir) - {len(self.remotes)} remote(s)"
            self.safe_addstr(4, 2, state[: max_x - 4], get_color(PAIR_SUCCESS))
        elif exists and not repo.is_kernel_tree:
            state = f" Linux man-pages - {len(self.remotes)} remote(s)"
            self.safe_addstr(4, 2, state[: max_x - 4], get_color(PAIR_SUCCESS))
        elif exists:
            missing = sum(1 for p in STANDARD_REMOTES if not preset_is_present(p, self.remotes))
            state = f" Git repository - {len(self.remotes)} remote(s), {missing} standard preset(s) available [A]"
            self.safe_addstr(4, 2, state[: max_x - 4], get_color(PAIR_SUCCESS))
        else:
            state = " Not cloned yet"
            if self.can_clone_current():
                if repo.kind == KIND_MANPAGES:
                    state += " - press [C] to clone the Linux man-pages (~50 MB download)"
                else:
                    what = "the CVE catalog" if repo.kind == KIND_CVE else "the Linux tree"
                    state += f" - press [C] to clone {what} (multi-GB download)"
            self.safe_addstr(4, 2, state[: max_x - 4], get_color(PAIR_ERROR))

        self.remote_list.title = f" Remotes of {repo.name} "
        self.remote_list.draw(self.stdscr)
        if not self.remotes:
            if not exists:
                hint = "Repository not present."
            elif not repo.is_kernel_tree:
                hint = "No remotes - press A (custom URL)."
            else:
                hint = "No remotes - press A (standard trees or custom URL)."
            self.safe_addstr(self.remote_list.y + 1, self.remote_list.x + 3,
                             hint[: max(1, self.remote_list.width - 6)], get_color(PAIR_MUTED))

        self.status_bar.set_shortcuts(self._shortcuts())
        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Dispatch screen hotkeys; navigation keys go to the remote list."""
        self.status_bar.set_message(None)
        if ch == 27:
            self._remember()
            if self.app is not None and hasattr(self.app, "pop_screen"):
                self.app.pop_screen()
            return True
        if ch in (ord("c"), ord("C")):
            self.clone_current()
            return True
        if ch in (ord("r"), ord("R")):
            self.switch_repo(1 if ch == ord("r") else -1)
            return True
        if ch in (ord("a"), ord("A")):
            self.add_remote_menu()
            return True
        if ch in (ord("f"), ord("F")):
            self.fetch_selected()
            return True
        if ch in (ord("g"), ord("G")):
            self.fetch(None)
            return True
        if ch in (ord("d"), ord("D"), curses.KEY_DC):
            self.remove_selected_remote()
            return True
        return super().handle_key(ch)


__all__ = [
    "CLONE_KINDS",
    "CLONE_LABELS",
    "KIND_CVE",
    "KIND_LINUX",
    "KIND_MANPAGES",
    "KIND_REPO",
    "GitManagerScreen",
    "RepoCandidate",
    "StandardRemotesDialog",
    "config_repos_dir",
    "cve_repo_path",
    "preset_is_present",
    "remote_url_key",
    "repository_candidates",
]
