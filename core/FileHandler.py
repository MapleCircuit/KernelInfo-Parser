"""FileHandler.py - Interacts with the FS and GIT."""
import contextlib
import logging
import os
import re
import shutil
import tempfile
import subprocess as sp
from pathlib import Path
from core.globalstuff import G, is_safe_git_rev, is_safe_git_spec

# Kernel-scheme release tags: kernel versions (v3.0, 2.0.31pre1, v2.6.12-rc2) and next-YYYYMMDD snapshots. A
# repository without any is sequenced by its prefixed releases (man-pages-6.9.1, foo-1.2-rc1; release_order)
_VERSION_TAG_RE = re.compile(
    r"^(?:v?\d+(?:\.\d+)*(?:[a-zA-Z](?:\+[a-zA-Z]?)?)?(?:-?(?:rc|alpha|beta|pre|patch)\d*)?|next-\d{8})$"
)
# Pre-release suffixes git's version sort places before their release: v3.1-rc1 < v3.1, v2.5.4-pre1 <
# v2.5.4, and the historical 2.0.31pre1 < 2.0.31, 1.0alpha < 1.0
_VERSIONSORT_SUFFIX_ARGS = tuple(
    arg for suffix in ("-rc", "-pre", "pre", "alpha", "beta") for arg in ("-c", f"versionsort.suffix={suffix}")
)
logger = logging.getLogger(__name__)


def _version_tag_predicate(names: list[str]):
    """Release-tag test for a repository with tags `names`: the kernel scheme (`_VERSION_TAG_RE`, `-tree` tags
    left out) when any name uses it, else the prefixed releases of `release_order.release_tag_names()`."""
    if any(not t.endswith("-tree") and _VERSION_TAG_RE.match(t) for t in names):
        return lambda t: not t.endswith("-tree") and bool(_VERSION_TAG_RE.match(t))
    from parser.git_ast.release_order import release_tag_names
    releases = set(release_tag_names(names))
    return lambda t: t in releases

# Scratch directories carry the PID of the process that created them, so `--clean-temp` can leave the
# directories of a parser instance that is still running alone (core/ingest_state.cleanup_ramdisk_artifacts)
OWNER_FILE = ".kip-owner"


def mark_owner(path: str | Path) -> None:
    """Record the current process as the owner of a scratch directory."""
    with contextlib.suppress(OSError):
        Path(path, OWNER_FILE).write_text(str(os.getpid()))


def owner_alive(path: str | Path) -> bool:
    """True when the process recorded by `mark_owner()` for `path` is still running."""
    try:
        pid = int(Path(path, OWNER_FILE).read_text().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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
        # Revision the workspace currently holds (None while it is being changed)
        self.workspace_rev: str | None = None
        self._cat_file_proc: sp.Popen | None = None
        self._cat_file_owner: int = os.getpid()
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
        """Ensure this process's persistent git cat-file --batch subprocess is alive.

        A forked worker inherits the parent's handle but must not share its pipes (requests and answers of
        two processes would interleave) nor signal it: it starts its own.
        """
        if self._cat_file_owner != os.getpid():
            self._cat_file_proc = None
            self._cat_file_owner = os.getpid()
        if self._cat_file_proc is not None and self._cat_file_proc.poll() is None:
            return self._cat_file_proc

        self._close_cat_file_proc()
        git_dir = self._get_git_dir()
        self._cat_file_proc = sp.Popen(
            ["git", f"--git-dir={git_dir}", "cat-file", "--batch"],
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            stderr=sp.DEVNULL,
            bufsize=0,
        )
        return self._cat_file_proc

    def _close_cat_file_proc(self) -> None:
        """Terminate this process's git cat-file --batch subprocess (never one inherited from a parent)."""
        proc, self._cat_file_proc = self._cat_file_proc, None
        if proc is None or self._cat_file_owner != os.getpid():
            return
        try:
            for pipe in (proc.stdin, proc.stdout):
                if pipe:
                    pipe.close()
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

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

    def checkout_version(self, version: str, base_rev: str | None, purge_list: list) -> str:
        """Make the persistent RAMDISK workspace hold `version` and register it in `version_dict`.

        When the workspace holds `base_rev`, only the files that differ are rewritten (`_apply_tree_diff()`);
        otherwise (first version, a skipped base, an earlier failure) the tree is extracted in full. Other
        versions mapped to the workspace are dropped from `version_dict`, so a read of the previous version
        goes to git instead of returning the new content from disk.
        """
        ws = self.workspace_dir
        updated = False
        if ws and os.path.isdir(ws) and base_rev and self.workspace_rev == str(base_rev):
            self.workspace_rev = None
            try:
                self._apply_tree_diff(ws, str(base_rev), version)
                updated = True
            except Exception as e:
                logger.warning(f"Incremental checkout {base_rev}..{version} failed ({e}); extracting the full tree.")
        if not updated:
            if ws:
                shutil.rmtree(ws, ignore_errors=True)
            ws = self.git_clone(version)
            if ws not in purge_list:
                purge_list.append(ws)
        for stale in [v for v, d in self.version_dict.items() if d == ws]:
            del self.version_dict[stale]
            self.file_dict.pop(stale, None)
        self.workspace_dir = ws
        self.workspace_rev = version
        self.version_dict[version] = ws
        self.file_dict[version] = {}
        return ws

    def _apply_tree_diff(self, ws: str, base_rev: str, target_rev: str) -> None:
        """Rewrite the files of `ws` that differ between two revisions (symlinks and modes included)."""
        git_dir = self._get_git_dir()
        out = sp.run(
            ["git", f"--git-dir={git_dir}", "diff-tree", "-r", "-z", "--no-renames",
             self._checked_rev(base_rev), self._checked_rev(target_rev)],
            capture_output=True,
            check=True,
        ).stdout
        fields = out.split(b"\0")
        for i in range(0, len(fields) - 1, 2):
            meta, path = fields[i].decode("ascii"), fields[i + 1].decode("utf-8", "surrogateescape")
            _old_mode, new_mode, _old_sha, new_sha, status = meta.lstrip(":").split()
            target = os.path.join(ws, path)
            if os.path.islink(target) or os.path.isfile(target):
                # Unlink first: writing through an existing symlink would overwrite the file it points at
                os.unlink(target)
            if status.startswith("D"):
                self._prune_empty_dirs(ws, os.path.dirname(target))
                continue
            if new_mode == "160000":
                continue  # submodule commit: no content in this repository
            data = self._read_blob(new_sha)
            if data is None:
                raise RuntimeError(f"blob {new_sha} of '{path}' is missing")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if new_mode == "120000":
                os.symlink(os.fsdecode(data), target)
                continue
            with open(target, "wb") as f:
                f.write(data)
            if new_mode == "100755":
                os.chmod(target, 0o755)
        _ensure_asm_symlinks(ws)

    @staticmethod
    def _prune_empty_dirs(root: str, directory: str) -> None:
        """Remove `directory` and its parents below `root` while they are empty (as a fresh extraction has them)."""
        root = os.path.normpath(root)
        directory = os.path.normpath(directory)
        while directory != root and directory.startswith(root + os.sep):
            try:
                os.rmdir(directory)
            except OSError:
                return
            directory = os.path.dirname(directory)

    def create_temp_dir(self) -> str:
        """Create a new temporary directory in RAMDISK for cloning repository versions."""
        path = tempfile.mkdtemp(prefix="code-parser.", dir=G.RAMDISK)
        mark_owner(path)
        return path

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
            if oldest_dir not in self.version_dict.values():
                shutil.rmtree(oldest_dir, ignore_errors=True)
                if oldest_dir == self.workspace_dir:
                    self.workspace_dir = self.workspace_rev = None
            trimmed += 1
        return trimmed

    def clear_all_version(self) -> None:
        """Remove all cloned repository version directories from disk and clear state."""
        self._close_cat_file_proc()
        for directory in {*self.version_dict.values(), self.workspace_dir} - {None}:
            shutil.rmtree(directory, ignore_errors=True)
        self.workspace_dir = None
        self.workspace_rev = None
        self.version_dict.clear()
        self.file_dict.clear()
        self._file_list_cache.clear()

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
            # One request on the persistent cat-file stream instead of a `git show` process per file
            data = self._read_blob(f"{self._checked_rev(version)}:{file_path}")
            return data.decode("latin-1") if data is not None else ""

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
            *_VERSIONSORT_SUFFIX_ARGS,
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

        # Canonical release tags (e.g. v2.6.12, v3.0, v6.12.1, v2.6.12-rc2); excludes non-release topic tags
        # (e.g. assoc-array-fixes) and tree tags (v2.6.11-tree). A repository without kernel-scheme tags uses its
        # prefixed releases (man-pages-6.9.1, foo-1.2.3) in release order and, without a start ref, starts at
        # its first release
        is_version_tag = _version_tag_predicate(raw_tags)
        prefixed = not any(not t.endswith("-tree") and _VERSION_TAG_RE.match(t) for t in raw_tags)
        raw_tags = [t for t in raw_tags if is_version_tag(t)]
        if prefixed:
            from parser.git_ast.release_order import release_key
            raw_tags.sort(key=release_key)
            if not start_ref and raw_tags:
                start_ref = raw_tags[0]
        start = start_ref or "v3.0"
        # Release candidates are parsed by default (each -rc# becomes its own snapshot)
        if include_pre_releases is None:
            include_pre_releases = True

        # Daily next-YYYYMMDD snapshots follow every release (name sorting would put them between the
        # unprefixed historical tags such as 2.4.31 and the v-prefixed ones)
        raw_tags = [t for t in raw_tags if not t.startswith("next-")] + [t for t in raw_tags if t.startswith("next-")]
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
            return self._commit_start_sequence(start, tags, is_version_tag)

        return tags[matched_idx:]

    def _git_lines(self, *args: str) -> list[str]:
        res = sp.run(["git", f"--git-dir={self._get_git_dir()}", *args], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def _commit_start_sequence(self, start: str, tags: list[str], is_version_tag=None) -> list[str]:
        """Sequence for a start ref that is not a listed release tag (e.g. the repository's first commit).

        Returns [start, <every listed tag whose history contains start>] in version order. A tag
        pointing exactly at start replaces the raw SHA so the snapshot is registered under its name.
        """
        if not is_safe_git_rev(start) or not self._git_lines("rev-parse", "--verify", "--quiet", f"{start}^{{commit}}"):
            return [start]
        points_at = set(self._git_lines("tag", "--points-at", start))
        descendants = set(self._git_lines("tag", "--contains", start)) - points_at
        # Name the snapshot after a tag on that exact commit even when pre-release tags are filtered out
        is_version_tag = is_version_tag or _version_tag_predicate(list(points_at))
        exact = sorted(t for t in points_at if is_version_tag(t))
        first = exact[0] if exact else start
        return [first] + [t for t in tags if t in descendants]

    def get_root_commit(self, rev: str = "HEAD") -> str | None:
        """First commit of rev's history (the parentless commit reached by following first parents)."""
        if not is_safe_git_rev(rev):
            return None
        roots = self._git_lines("rev-list", "--max-parents=0", "--first-parent", rev)
        return roots[-1] if roots else None
