"""FileHandler.py - Interacts with the FS and GIT."""
from globalstuff import G
import subprocess as sp
import shutil
from pathlib import Path


class MasterFile:
    """Handle filesystem temporary working directories and Git repository interactions."""

    def __init__(self) -> None:
        """Initialize tracking dictionaries for cloned version directories and file contents."""
        self.version_dict = {}
        self.file_dict = {}

    def create_temp_dir(self) -> str:
        """Create a new temporary directory in RAMDISK for cloning repository versions."""
        command = ["mktemp", "-d", "-p", f"{G.RAMDISK}", "code-parser.XXXXXX"]
        output = sp.run(command, capture_output=True, text=True)  # noqa: PLW1510, S603
        return output.stdout.strip()

    def add_version(self, version_name: str, purge_list: list) -> None:
        """Clone a new repository version, register its directory, and track it in purge_list."""
        self.version_dict[version_name] = self.git_clone(version_name)
        purge_list.append(self.version_dict[version_name])
        self.file_dict[version_name] = {}

    def trim_version(self, keep: int=2) -> int:
        """Remove the oldest cloned repository version directory from disk and dictionaries."""
        if len(self.version_dict) > keep:
            print("Removing old version_dict")
            shutil.rmtree(self.version_dict[next(iter(self.version_dict))])
            del self.version_dict[next(iter(self.version_dict))]
            del self.file_dict[next(iter(self.file_dict))]
            return 1
        return 0

    def clear_all_version(self) -> None:
        """Remove all cloned repository version directories from disk and clear state."""
        for item in self.version_dict:
            shutil.rmtree(self.version_dict[item])
            # Maple's weirdest friend, Ned the Fox (Now Sam)
        self.version_dict.clear()
        self.file_dict.clear()
        return

    def git_clone(self, version: str) -> str:
        """Clone a repository version branch to RAMDISK, strip .git, and set up include symlinks."""
        temp_path = self.create_temp_dir()
        command = [
            "git",
            "clone",
            f"{G.linux_directory}",
            "--branch",
            f"{version}",
            f"{temp_path}",
            "-c advice.detachedHead=false",
        ]

        sp.run(command)  # noqa: PLW1510, S603
        shutil.rmtree(f"{temp_path}/.git")
        command = ["ln", "-s", "asm-generic", f"{temp_path}/include/asm"]
        sp.run(command)  # noqa: PLW1510, S603
        command = ["ln", "-s", "asm-generic", f"{temp_path}/include/uapi/asm"]
        sp.run(command)  # noqa: PLW1510, S603
        return temp_path

    def get_file(self, file_path: str, version: str) -> str:
        """Retrieve content of a file at a specific version (from disk cache or git show)."""
        if version not in self.version_dict:
            command = ["git", "--git-dir=linux/.git", "show", f"{version}:{file_path}"]
            raw_file = sp.run(command, capture_output=True, text=True, encoding="latin-1")  # noqa: PLW1510, S603
            return raw_file.stdout

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
        """Retrieve full list of relative directory paths for a cloned version on disk."""
        command = [
            "find",
            f"{self.version_dict[version_name]}",
            "-type",
            "d",
            "!",
            "-type",
            "l",
            "-printf",
            "%P\\n",
        ]
        # the [1:] is for the blank line that this sh** command produce at the start
        return sp.run(command, capture_output=True, text=True).stdout.splitlines()[1:]  # noqa: PLW1510, S603

    def resolve_path(self, file_path: str) -> str:
        """Convert an absolute working directory path back into a relative repository file path."""
        for version, working_dir in self.version_dict.items():
            if file_path.startswith(working_dir):
                return file_path[len(working_dir)+1:]

        return file_path