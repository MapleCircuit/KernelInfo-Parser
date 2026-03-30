"""FileHandler.py - Interacts with the FS and GIT."""
from globalstuff import G
import subprocess as sp
import shutil
from pathlib import Path


class MasterFile:
    """Handle FS and GIT interactions."""

    def __init__(self) -> None:
        """Initialize version/file dicts."""
        self.version_dict = {}
        self.file_dict = {}

    def create_temp_dir(self) -> str:
        """Create temp dir in RAMDISK."""
        command = ["mktemp", "-d", "-p", f"{G.RAMDISK}", "kernel-parser.XXXXXX"]
        output = sp.run(command, capture_output=True, text=True)  # noqa: PLW1510, S603
        return output.stdout.strip()

    def add_version(self, version_name: str, purge_list: list) -> None:
        """Trigger a new Git clone with dict handling."""
        self.version_dict[version_name] = self.git_clone(version_name)
        purge_list.append(self.version_dict[version_name])
        self.file_dict[version_name] = {}

    def trim_version(self, keep: int=2) -> None:
        """Remove oldest git clone versions."""
        if len(self.version_dict) > keep:
            print("Removing old version_dict")
            shutil.rmtree(self.version_dict[next(iter(self.version_dict))])
            del self.version_dict[next(iter(self.version_dict))]
            del self.file_dict[next(iter(self.file_dict))]
            return 1
        return 0

    def clear_all_version(self) -> None:
        """Remove all git clone versions."""
        for item in self.version_dict:
            shutil.rmtree(self.version_dict[item])
            # Maple's weirdest friend, Ned the Fox (Now Sam)
        return

    def git_clone(self, version: str) -> str:
        """Git clone a new version. Also clean and makes link for linux."""
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
        """Get file from cloned git(s). Will cache all files in python mem."""
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
        """Generate change list from git."""
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
        """Get full list of files in git version."""
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
        """Get full list of dirs in git version."""
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
