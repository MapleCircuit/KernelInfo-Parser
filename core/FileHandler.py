"""FileHandler.py - Interacts with the FS and GIT."""
import os
import re
import shutil
import tempfile
import subprocess as sp
from pathlib import Path
from core.globalstuff import G, is_safe_git_rev, is_safe_git_spec

_VERSION_TAG_RE = re.compile(r"^v?\d+(?:\.\d+)*(?:-(?:rc|alpha|beta|pre)\d*)?$")


def _ensure_asm_symlinks(base_dir: str | Path) -> None:
    """Ensure include/asm and include/uapi/asm symlinks point to asm-generic."""
    base = Path(base_dir)
    asm_path = base / "include" / "asm"
    if not asm_path.exists() and not asm_path.is_symlink():
        try:
            os.symlink("asm-generic", asm_path)
        except OSError:
            sp.run(["ln", "-s", "asm-generic", str(asm_path)], check=False)

    uapi_asm_parent = base / "include" / "uapi"
    if uapi_asm_parent.exists():
        uapi_asm_path = uapi_asm_parent / "asm"
        if not uapi_asm_path.exists() and not uapi_asm_path.is_symlink():
            try:
                os.symlink("asm-generic", uapi_asm_path)
            except OSError:
                sp.run(["ln", "-s", "asm-generic", str(uapi_asm_path)], check=False)


class MasterFile:
    """Handle filesystem temporary working directories and Git repository interactions."""

    def __init__(self) -> None:
        """Initialize tracking dictionaries for cloned version directories and file contents."""
        self.version_dict = {}
        self.file_dict = {}
        self.workspace_dir: str | None = None
        self._cat_file_proc: sp.Popen | None = None
        self.repo_dir: Path = Path(G.linux_directory).resolve()
        # (repo_dir, version) -> `git ls-tree -r --name-only` output: one version is listed by several steps
        self._file_list_cache: dict[tuple[Path, str], str] = {}

    def set_repo_dir(self, repo_dir: str | Path) -> None:
        """Set the active Git repository directory and restart cat-file subprocess if path changes."""
        new_dir = Path(repo_dir).resolve()
        if getattr(self, "repo_dir", None) != new_dir:
            self._close_cat_file_proc()
            self._file_list_cache.clear()
            self.repo_dir = new_dir
            G.linux_directory = new_dir

    def get_repo_dir(self) -> str:
        """Get the active Git repository directory as a string path."""
        repo = getattr(self, "repo_dir", None) or Path(G.linux_directory)
        return str(repo)

    def _get_git_dir(self) -> str:
        """Resolve the active .git directory for Git subprocess execution."""
        repo = getattr(self, "repo_dir", None) or Path(G.linux_directory)
        dot_git = repo / ".git"
        if dot_git.exists():
            return str(dot_git)
        return str(repo)

    def _ensure_cat_file_proc(self) -> sp.Popen:
        """Ensure persistent git cat-file --batch subprocess is alive."""
        if self._cat_file_proc is not None and self._cat_file_proc.poll() is None:
            return self._cat_file_proc

        self._close_cat_file_proc()
        git_dir = self._get_git_dir()
        self._cat_file_proc = sp.Popen(
            ["git", f"--git-dir={git_dir}", "cat-file", "--batch"],
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            bufsize=0,
        )
        return self._cat_file_proc

    def _close_cat_file_proc(self) -> None:
        """Safely terminate persistent git cat-file --batch subprocess."""
        if self._cat_file_proc is not None:
            try:
                if self._cat_file_proc.stdin:
                    self._cat_file_proc.stdin.close()
                if self._cat_file_proc.stdout:
                    self._cat_file_proc.stdout.close()
                if self._cat_file_proc.stderr:
                    self._cat_file_proc.stderr.close()
                self._cat_file_proc.terminate()
                self._cat_file_proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._cat_file_proc.kill()
                except Exception:
                    pass
            finally:
                self._cat_file_proc = None

    @staticmethod
    def _checked_rev(rev: str) -> str:
        """Validate a revision before it reaches a git argument.

        A revision starting with "-" would be read as an option by every git command below
        (`archive`, `diff`, `ls-tree`, `show`), so only plain refs and SHAs are accepted.
        """
        if not is_safe_git_rev(rev):
            raise ValueError(f"Unsafe git revision: {rev!r}")
        return rev

    def _read_blob(self, target_spec: str) -> bytes | None:
        """Read blob bytes from persistent git cat-file --batch subprocess."""
        # cat-file --batch reads one request per line: a newline in the spec would inject further
        # requests and desynchronize every later answer on this long-lived stream.
        if not is_safe_git_spec(target_spec):
            return None
        for attempt in range(2):
            try:
                proc = self._ensure_cat_file_proc()
                assert proc.stdin is not None
                assert proc.stdout is not None

                proc.stdin.write(f"{target_spec}\n".encode("utf-8"))
                proc.stdin.flush()

                header_bytes = proc.stdout.readline()
                if not header_bytes:
                    raise BrokenPipeError("Empty header read from git cat-file stdout.")

                header = header_bytes.decode("utf-8", errors="replace").strip()
                if header.endswith("missing"):
                    return None

                parts = header.split()
                if len(parts) < 3:
                    return None

                obj_type = parts[1]
                size = int(parts[2])

                data = bytearray()
                while len(data) < size:
                    chunk = proc.stdout.read(size - len(data))
                    if not chunk:
                        raise BrokenPipeError("Unexpected EOF while reading blob content.")
                    data.extend(chunk)

                # Consume trailing newline
                _ = proc.stdout.read(1)

                if obj_type != "blob":
                    return None
                return bytes(data)
            except (BrokenPipeError, OSError, ValueError):
                self._close_cat_file_proc()
                if attempt == 1:
                    raise
        return None

    def sync_workspace(self, target_rev: str, change_list: list[str]) -> str:
        """Incrementally synchronize persistent RAMDISK workspace with target_rev using git cat-file."""
        if not self.workspace_dir or not os.path.exists(self.workspace_dir):
            self.workspace_dir = self.git_clone(target_rev)
            self.version_dict[target_rev] = self.workspace_dir
            self.file_dict.clear()
            self.file_dict[target_rev] = {}
            return self.workspace_dir

        self.version_dict[target_rev] = self.workspace_dir
        self.file_dict.clear()
        self.file_dict[target_rev] = {}

        for item in change_list:
            if not item or not item.strip():
                continue
            tokens = item.strip().split("\t")
            status = tokens[0].split()[-1] if " " in tokens[0] else tokens[0]

            if status.startswith("D"):
                path = tokens[1]
                target_file = os.path.join(self.workspace_dir, path)
                if os.path.exists(target_file) or os.path.islink(target_file):
                    try:
                        os.unlink(target_file)
                    except OSError:
                        pass
            elif status.startswith("R"):
                old_path = tokens[1]
                new_path = tokens[2]
                old_target = os.path.join(self.workspace_dir, old_path)
                if os.path.exists(old_target) or os.path.islink(old_target):
                    try:
                        os.unlink(old_target)
                    except OSError:
                        pass
                blob_bytes = self._read_blob(f"{target_rev}:{new_path}")
                if blob_bytes is not None:
                    new_target = os.path.join(self.workspace_dir, new_path)
                    os.makedirs(os.path.dirname(new_target), exist_ok=True)
                    with open(new_target, "wb") as f:
                        f.write(blob_bytes)
            elif status.startswith("A") or status.startswith("M") or status.startswith("C"):
                path = tokens[1]
                blob_bytes = self._read_blob(f"{target_rev}:{path}")
                if blob_bytes is not None:
                    target_file = os.path.join(self.workspace_dir, path)
                    os.makedirs(os.path.dirname(target_file), exist_ok=True)
                    with open(target_file, "wb") as f:
                        f.write(blob_bytes)

        # Ensure asm symlinks remain intact
        _ensure_asm_symlinks(self.workspace_dir)

        return self.workspace_dir

    def create_temp_dir(self) -> str:
        """Create a new temporary directory in RAMDISK for cloning repository versions."""
        return tempfile.mkdtemp(prefix="code-parser.", dir=G.RAMDISK)

    def add_version(self, version_name: str, purge_list: list) -> None:
        """Clone a new repository version, register its directory, and track it in purge_list."""
        self.version_dict[version_name] = self.git_clone(version_name)
        purge_list.append(self.version_dict[version_name])
        self.file_dict[version_name] = {}

    def clear_version_cache(self, version: str) -> None:
        """Evict cached file contents and file listings for a completed version."""
        for key in [k for k in self._file_list_cache if k[1] == version]:
            del self._file_list_cache[key]
        if version in self.file_dict:
            self.file_dict[version].clear()
            del self.file_dict[version]

    def trim_version(self, keep: int = 1) -> int:
        """Remove the oldest cloned repository version directories from disk and dictionaries."""
        trimmed = 0
        while len(self.version_dict) > keep:
            oldest_v = next(iter(self.version_dict))
            oldest_dir = self.version_dict.pop(oldest_v)
            self.file_dict.pop(oldest_v, None)
            if os.path.exists(oldest_dir):
                shutil.rmtree(oldest_dir, ignore_errors=True)
            trimmed += 1
        return trimmed

    def clear_all_version(self) -> None:
        """Remove all cloned repository version directories from disk and clear state."""
        self._close_cat_file_proc()
        for item in self.version_dict:
            if os.path.exists(self.version_dict[item]):
                shutil.rmtree(self.version_dict[item], ignore_errors=True)
        if self.workspace_dir and os.path.exists(self.workspace_dir):
            shutil.rmtree(self.workspace_dir, ignore_errors=True)
        self.workspace_dir = None
        self.version_dict.clear()
        self.file_dict.clear()
        self._file_list_cache.clear()
        return

    def __del__(self) -> None:
        """Safely clean up persistent subprocess on destruction."""
        self._close_cat_file_proc()

    def git_clone(self, version: str) -> str:
        """Extract a repository version branch directly into RAMDISK and set up include symlinks."""
        temp_path = self.create_temp_dir()
        git_dir = self._get_git_dir()
        p_arch = sp.Popen(
            ["git", f"--git-dir={git_dir}", "archive", self._checked_rev(version)],
            stdout=sp.PIPE,
        )
        try:
            sp.run(["tar", "-x", "-C", f"{temp_path}"], stdin=p_arch.stdout, check=True)  # noqa: S603, PLW1510
        finally:
            if p_arch.stdout:
                p_arch.stdout.close()
        p_arch.wait()

        _ensure_asm_symlinks(temp_path)
        return temp_path

    def get_file(self, file_path: str, version: str) -> str:
        """Retrieve content of a file at a specific version (from disk cache or git show)."""
        if version not in self.version_dict:
            git_dir = self._get_git_dir()
            command = ["git", f"--git-dir={git_dir}", "show", f"{self._checked_rev(version)}:{file_path}"]
            raw_file = sp.run(command, capture_output=True, text=True, encoding="latin-1")  # noqa: PLW1510
            return raw_file.stdout

        if version not in self.file_dict:
            self.file_dict[version] = {}

        if file_path not in self.file_dict[version]:
            p = Path(f"{self.version_dict[version]}/{file_path}")
            if p.is_symlink():
                if p.is_dir():
                    self.file_dict[version][file_path] = os.readlink(p)
                else:
                    try:
                        self.file_dict[version][file_path] = p.read_text(encoding="latin-1")
                    except (OSError, FileNotFoundError):
                        self.file_dict[version][file_path] = os.readlink(p)
            elif p.is_dir():
                self.file_dict[version][file_path] = ""
            else:
                self.file_dict[version][file_path] = p.read_text(encoding="latin-1")
        return self.file_dict[version][file_path]

    def generate_change_list(self, gp: object, base_rev: str | None = None, target_rev: str | None = None) -> list[str]:
        """Generate git diff change list (--name-status) between base_rev (or Old_Version_Name) and target_rev."""
        old_rev = base_rev if base_rev is not None else getattr(gp, "Old_Version_Name", None)
        new_rev = target_rev if target_rev is not None else getattr(gp, "Version_Name", None)
        git_dir = self._get_git_dir()

        EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        if not old_rev or str(old_rev) == "0":
            old_rev = EMPTY_TREE

        command = [
            "git",
            f"--git-dir={git_dir}",
            "diff",
            self._checked_rev(str(old_rev)),
            self._checked_rev(str(new_rev)),
            "--name-status",
        ]

        gp.Change_List = sp.run(command, capture_output=True, text=True).stdout.splitlines()  # noqa: PLW1510, S603
        gp._changed_paths_set = set(item.split("\t")[-1] for item in gp.Change_List if item)
        return gp.Change_List

    def git_file_list(self, version: str) -> str:
        """Retrieve full list of tracked files in git repository for a version (cached per version)."""
        cache_key = (self.repo_dir, str(version))
        cached = self._file_list_cache.get(cache_key)
        if cached is not None:
            return cached
        git_dir = self._get_git_dir()
        command = [
            "git",
            f"--git-dir={git_dir}",
            "ls-tree",
            "-r",
            "--name-only",
            self._checked_rev(str(version)),
        ]
        raw_file = sp.run(command, capture_output=True, text=True)  # noqa: PLW1510, S603
        if raw_file.returncode == 0:
            self._file_list_cache[cache_key] = raw_file.stdout
            while len(self._file_list_cache) > 4:
                del self._file_list_cache[next(iter(self._file_list_cache))]
        return raw_file.stdout

    def get_dir_list(self, version_name: str) -> list[str]:
        """Retrieve full list of relative directory paths for a version from git repository."""
        git_dir = self._get_git_dir()
        command = [
            "git",
            f"--git-dir={git_dir}",
            "ls-tree",
            "-r",
            "-d",
            "--name-only",
            self._checked_rev(str(version_name)),
        ]
        return sp.run(command, capture_output=True, text=True).stdout.splitlines()  # noqa: PLW1510, S603

    def get_version_tags(
        self,
        start_ref: str | None = None,
        include_pre_releases: bool | None = None,
    ) -> list[str]:
        """Discover and sequence repository version tags starting from start_ref using git version sorting."""
        git_dir = self._get_git_dir()
        command = [
            "git",
            f"--git-dir={git_dir}",
            "-c",
            "versionsort.suffix=-rc",
            "-c",
            "versionsort.suffix=-pre",
            "tag",
            "-l",
            "--sort=v:refname",
        ]
        res = sp.run(command, capture_output=True, text=True, check=False)
        if res.returncode != 0 or not res.stdout.strip():
            return [start_ref] if start_ref else ["v3.0"]

        raw_tags = [t.strip() for t in res.stdout.splitlines() if t.strip()]
        if not raw_tags:
            return [start_ref] if start_ref else ["v3.0"]

        start = start_ref or "v3.0"
        # Release candidates are parsed by default (each -rc# becomes its own snapshot)
        if include_pre_releases is None:
            include_pre_releases = True

        # Filter for canonical version release tags (e.g. v2.6.12, v3.0, v6.12.1, v2.6.12-rc2).
        # Excludes non-release topic tags (e.g. assoc-array-fixes) and tree tags (v2.6.11-tree).
        raw_tags = [t for t in raw_tags if not t.endswith("-tree") and _VERSION_TAG_RE.match(t)]
        if not include_pre_releases:
            tags = [t for t in raw_tags if not any(pre in t for pre in ("-rc", "-alpha", "-beta", "-pre"))]
            if not tags:
                tags = raw_tags
        else:
            tags = raw_tags

        matched_idx = None
        for candidate in [start, f"v{start}", start.lstrip("v")]:
            if candidate in tags:
                matched_idx = tags.index(candidate)
                start = candidate
                break

        if matched_idx is None:
            return self._commit_start_sequence(start, tags)

        return tags[matched_idx:]

    def _git_lines(self, *args: str) -> list[str]:
        res = sp.run(["git", f"--git-dir={self._get_git_dir()}", *args], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def _commit_start_sequence(self, start: str, tags: list[str]) -> list[str]:
        """Sequence for a start ref that is not a listed release tag (e.g. the repository's first commit).

        Returns [start, <every listed tag whose history contains start>] in version order. A tag
        pointing exactly at start replaces the raw SHA so the snapshot is registered under its name.
        """
        if not is_safe_git_rev(start) or not self._git_lines("rev-parse", "--verify", "--quiet", f"{start}^{{commit}}"):
            return [start]
        points_at = set(self._git_lines("tag", "--points-at", start))
        descendants = set(self._git_lines("tag", "--contains", start)) - points_at
        # Name the snapshot after a tag on that exact commit even when pre-release tags are filtered out
        exact = sorted(t for t in points_at if not t.endswith("-tree") and _VERSION_TAG_RE.match(t))
        first = exact[0] if exact else start
        return [first] + [t for t in tags if t in descendants]

    def get_root_commit(self, rev: str = "HEAD") -> str | None:
        """First commit of rev's history (the parentless commit reached by following first parents)."""
        if not is_safe_git_rev(rev):
            return None
        roots = self._git_lines("rev-list", "--max-parents=0", "--first-parent", rev)
        return roots[-1] if roots else None
