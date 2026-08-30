"""FileHandler.py - Interacts with the FS and GIT."""
import os
import shutil
import tempfile
import subprocess as sp
from pathlib import Path
from core.globalstuff import G


class MasterFile:
    """Handle filesystem temporary working directories and Git repository interactions."""

    def __init__(self) -> None:
        """Initialize tracking dictionaries for cloned version directories and file contents."""
        self.version_dict = {}
        self.file_dict = {}

    def create_temp_dir(self) -> str:
        """Create a new temporary directory in RAMDISK for cloning repository versions."""
        return tempfile.mkdtemp(prefix="code-parser.", dir=G.RAMDISK)

    def add_version(self, version_name: str, purge_list: list) -> None:
        """Clone a new repository version, register its directory, and track it in purge_list."""
        self.version_dict[version_name] = self.git_clone(version_name)
        purge_list.append(self.version_dict[version_name])
        self.file_dict[version_name] = {}

    def trim_version(self, keep: int = 2) -> int:
        """Remove the oldest cloned repository version directory from disk and dictionaries."""
        if len(self.version_dict) > keep:
            oldest_v = next(iter(self.version_dict))
            oldest_dir = self.version_dict.pop(oldest_v)
            self.file_dict.pop(oldest_v, None)
            if os.path.exists(oldest_dir):
                shutil.rmtree(oldest_dir, ignore_errors=True)
            return 1
        return 0

    def clear_all_version(self) -> None:
        """Remove all cloned repository version directories from disk and clear state."""
        for item in self.version_dict:
            shutil.rmtree(self.version_dict[item])
        self.version_dict.clear()
        self.file_dict.clear()
        return

    def git_clone(self, version: str) -> str:
        """Extract a repository version branch directly into RAMDISK and set up include symlinks."""
        temp_path = self.create_temp_dir()
        git_dir = f"{G.linux_directory}/.git" if Path(f"{G.linux_directory}/.git").exists() else "linux/.git"
        p_arch = sp.Popen(
            ["git", f"--git-dir={git_dir}", "archive", f"{version}"],
            stdout=sp.PIPE,
        )
        sp.run(["tar", "-x", "-C", f"{temp_path}"], stdin=p_arch.stdout, check=True)  # noqa: S603, PLW1510
        p_arch.wait()

        # Symlinks for asm
        asm_path = Path(f"{temp_path}/include/asm")
        if not asm_path.exists():
            try:
                os.symlink("asm-generic", asm_path)
            except OSError:
                sp.run(["ln", "-s", "asm-generic", str(asm_path)], check=False)

        uapi_asm_parent = Path(f"{temp_path}/include/uapi")
        if uapi_asm_parent.exists():
            uapi_asm_path = Path(f"{temp_path}/include/uapi/asm")
            if not uapi_asm_path.exists():
                try:
                    os.symlink("asm-generic", uapi_asm_path)
                except OSError:
                    sp.run(["ln", "-s", "asm-generic", str(uapi_asm_path)], check=False)
        return temp_path

    def get_file(self, file_path: str, version: str) -> str:
        """Retrieve content of a file at a specific version (from disk cache or git show)."""
        if version not in self.version_dict:
            command = ["git", "--git-dir=linux/.git", "show", f"{version}:{file_path}"]
            raw_file = sp.run(command, capture_output=True, text=True, encoding="latin-1")  # noqa: PLW1510, S603
            return raw_file.stdout

        if version not in self.file_dict:
            self.file_dict[version] = {}

        if file_path not in self.file_dict[version]:
            self.file_dict[version][file_path] = Path(
                f"{self.version_dict[version]}/{file_path}",
            ).read_text(encoding="latin-1")
        return self.file_dict[version][file_path]

    def generate_change_list(self, gp: object) -> list[str]:
        """Generate git diff change list (--name-status) between old and current version."""
        command = [
            "git",
            "--git-dir=linux/.git",
            "diff",
            f"{gp.Old_Version_Name}",
            f"{gp.Version_Name}",
            "--name-status",
        ]

        gp.Change_List = sp.run(command, capture_output=True, text=True).stdout.splitlines()  # noqa: PLW1510, S603
        return gp.Change_List

    def git_file_list(self, version: str) -> str:
        """Retrieve full list of tracked files in git repository for a version."""
        command = [
            "git",
            "--git-dir=linux/.git",
            "ls-tree",
            "-r",
            "--name-only",
            f"{version}",
        ]
        raw_file = sp.run(command, capture_output=True, text=True)  # noqa: PLW1510, S603
        return raw_file.stdout

    def get_dir_list(self, version_name: str) -> list[str]:
        """Retrieve full list of relative directory paths for a version from git repository."""
        command = [
            "git",
            "--git-dir=linux/.git",
            "ls-tree",
            "-r",
            "-d",
            "--name-only",
            f"{version_name}",
        ]
        return sp.run(command, capture_output=True, text=True).stdout.splitlines()  # noqa: PLW1510, S603

    def resolve_path(self, file_path: str) -> str:
        """Convert an absolute working directory path back into a relative repository file path."""
        for version, working_dir in self.version_dict.items():
            if file_path.startswith(working_dir):
                return file_path[len(working_dir)+1:]

        return file_path
