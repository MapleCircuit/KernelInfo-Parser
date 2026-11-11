"""Git helpers for the TUI: tag listing, remote management, and clone/fetch command builders.

Read-only queries run synchronously through `subprocess.run`. Long-running network operations
(`git clone`, `git fetch`) are only *built* here as argv lists; screens execute them through
`core.tui.process_view.run_streaming_command()` so progress stays visible inside curses.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Canonical release tags (v2.6.12, v3.0, v6.12.1, v2.6.12-rc2); `-tree` snapshot tags are excluded
VERSION_TAG_RE = re.compile(r"^v?\d+(?:\.\d+)+(?:-(?:rc|pre)\d*)?$")
PRE_RELEASE_RE = re.compile(r"-(?:rc|pre|alpha|beta)\d*$")

# Remote names git accepts and that stay readable in `<remote>/<branch>` refs
REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Accepted remote URL schemes for user-supplied remotes
REMOTE_URL_RE = re.compile(r"^(?:https|git)://[^\s/]+/\S+$")

DEFAULT_LINUX_URL = "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"
DEFAULT_LINUX_DIRNAME = "linux"
# CVE catalog clones (config `cve.repo_dir`): the CVE Program's list or the kernel CNA's vulns.git
DEFAULT_CVE_URL = "https://github.com/CVEProject/cvelistV5.git"
KERNEL_CVE_URL = "https://git.kernel.org/pub/scm/linux/security/vulns.git"
DEFAULT_CVE_DIRNAME = "cvelistV5"
# Directory names for repositories cloned under `workspace.repos_dir` from the TUI
REPO_DIRNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class RemotePreset:
    """A well-known kernel tree offered by the Git manager."""

    name: str
    url: str
    description: str


STANDARD_REMOTES: tuple[RemotePreset, ...] = (
    RemotePreset("stable", "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
                 "Stable & LTS releases (linux-X.Y.y branches)"),
    RemotePreset("linux-next", "https://git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git",
                 "Integration tree for the next merge window"),
    RemotePreset("net", "https://git.kernel.org/pub/scm/linux/kernel/git/netdev/net.git",
                 "Networking fixes"),
    RemotePreset("net-next", "https://git.kernel.org/pub/scm/linux/kernel/git/netdev/net-next.git",
                 "Networking development"),
    RemotePreset("tip", "https://git.kernel.org/pub/scm/linux/kernel/git/tip/tip.git",
                 "x86 / scheduler / timers / locking (tip)"),
    RemotePreset("mm", "https://git.kernel.org/pub/scm/linux/kernel/git/akpm/mm.git",
                 "Memory management (akpm)"),
    RemotePreset("bpf-next", "https://git.kernel.org/pub/scm/linux/kernel/git/bpf/bpf-next.git",
                 "BPF development"),
    RemotePreset("rt-devel", "https://git.kernel.org/pub/scm/linux/kernel/git/rt/linux-rt-devel.git",
                 "PREEMPT_RT development"),
    RemotePreset("rust", "https://github.com/Rust-for-Linux/linux.git",
                 "Rust-for-Linux"),
)


def git_env() -> dict[str, str]:
    """Environment for TUI-spawned git: never prompt on the terminal curses owns."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "")
    env["LC_ALL"] = "C"
    return env


def _git(repo: Path | str, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=git_env(),
    )


def is_git_repo(path: Path | str) -> bool:
    """True when `path` is the top of a work tree or a bare repository."""
    p = Path(path)
    if not p.is_dir():
        return False
    try:
        res = _git(p, "rev-parse", "--git-dir", timeout=10.0)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def is_valid_remote_name(name: str) -> bool:
    return bool(REMOTE_NAME_RE.match(name or "")) and ".." not in name and not name.endswith((".", ".lock"))


def is_valid_repo_dirname(name: str) -> bool:
    """A single path component usable as `<repos_dir>/<name>` (no separators, no `..`)."""
    return bool(REPO_DIRNAME_RE.match(name or "")) and ".." not in name


def repo_dirname_from_url(url: str) -> str:
    """Last path component of `url` without a trailing `/` or `.git` (`.../linux-next.git` -> `linux-next`)."""
    tail = (url or "").strip().rstrip("/").rsplit("/", 1)[-1]
    return tail.removesuffix(".git")


def is_valid_remote_url(url: str) -> bool:
    """Only `https://` and `git://` URLs are accepted for remotes added from the TUI."""
    return bool(REMOTE_URL_RE.match((url or "").strip()))


def list_version_tags(repo: Path | str, include_pre_releases: bool = True) -> list[str]:
    """Release tags of `repo` in version order (`v3.0-rc1` sorts before `v3.0`); [] on failure."""
    try:
        res = _git(
            repo,
            "-c", "versionsort.suffix=-rc",
            "-c", "versionsort.suffix=-pre",
            "tag", "-l", "--sort=v:refname",
            timeout=60.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    tags = [t.strip() for t in res.stdout.splitlines() if VERSION_TAG_RE.match(t.strip())]
    if not include_pre_releases:
        tags = [t for t in tags if not PRE_RELEASE_RE.search(t)]
    return tags


def list_remotes(repo: Path | str) -> list[tuple[str, str]]:
    """`(name, fetch_url)` for each remote of `repo`, in `git remote` order."""
    try:
        res = _git(repo, "remote", "-v", timeout=10.0)
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    seen: dict[str, str] = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)" and parts[0] not in seen:
            seen[parts[0]] = parts[1]
    return list(seen.items())


def add_remote(repo: Path | str, name: str, url: str) -> tuple[bool, str]:
    """`git remote add <name> <url>` after validating both; returns (ok, message)."""
    name = (name or "").strip()
    url = (url or "").strip()
    if not is_valid_remote_name(name):
        return False, f"Invalid remote name: '{name}'"
    if not is_valid_remote_url(url):
        return False, f"Remote URL must start with https:// or git:// : '{url}'"
    if any(n == name for n, _ in list_remotes(repo)):
        return False, f"Remote '{name}' already exists"
    try:
        res = _git(repo, "remote", "add", name, url)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip() or f"git remote add exited {res.returncode}"
    return True, f"Added remote '{name}' -> {url}"


def remove_remote(repo: Path | str, name: str) -> tuple[bool, str]:
    """`git remote remove <name>`; returns (ok, message)."""
    if not is_valid_remote_name(name):
        return False, f"Invalid remote name: '{name}'"
    try:
        res = _git(repo, "remote", "remove", name)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip() or f"git remote remove exited {res.returncode}"
    return True, f"Removed remote '{name}'"


def clone_command(url: str, dest: Path | str) -> list[str]:
    """argv for a full clone with progress output (progress is forced although stderr is a pipe)."""
    return ["git", "clone", "--progress", url, str(dest)]


def fetch_command(repo: Path | str, remote: str | None = None) -> list[str]:
    """argv for `git fetch` of one remote (with tags), or of every remote when `remote` is None."""
    base = ["git", "-C", str(repo), "fetch", "--progress", "--tags"]
    return [*base, "--all"] if remote is None else [*base, remote]
