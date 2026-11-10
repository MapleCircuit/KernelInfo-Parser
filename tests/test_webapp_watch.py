"""tests/test_webapp_watch.py - Watch list change checks (G10, webapp/API.md section 4.12)."""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services.watch_service import WatchChecker, compare_texts, section_block

TREES = {
    "A": {"kernel/fork.c": "int a;\nint copy_process(void)\n{\n\treturn 0;\n}\n", "MAINTAINERS": "EXT4 FILE SYSTEM\nM:\tTed\nF:\tfs/ext4/\n\nOTHER\nM:\tx\n",
          "fs/Kconfig": "config EXT4_FS\n\ttristate \"ext4\"\n"},
    "B": {"kernel/fork.c": "int a;\nint copy_process(void)\n{\n\treturn  0; /* same */\n}\n", "MAINTAINERS": "EXT4 FILE SYSTEM\nM:\tTed\nF:\tfs/ext4/\nF:\tinclude/ext4.h\n\n",
          "fs/Kconfig": "config EXT4_FS\n\ttristate \"ext4\"\n"},
}


def read(rev, path):
    return TREES[rev].get(path)


def detail(rev, name, arch):
    if name == "copy_process":
        return {"file_path": "kernel/fork.c", "line_s": 2, "definition": {"line_e": 5}}
    if name == "CONFIG_EXT4_FS":
        return {"file_path": "fs/Kconfig", "line_s": 1, "line_e": 2}
    if name == "gone" and rev == "A":
        return {"file_path": "kernel/fork.c", "line_s": 1, "definition": {"line_e": 1}}
    return None


def git(*args):
    return "M\tfs/ext4/inode.c\nA\tfs/ext4/new.c\n" if "fs/ext4" in args else ""


def normalize(code):
    return re.sub(r"\s+", "", re.sub(r"/\*.*?\*/", "", code, flags=re.S))


CHECK = WatchChecker(git, read, detail, normalize)


class TestWatch(unittest.TestCase):
    def test_item_types(self) -> None:
        res = {r["key"]: r for r in CHECK.check("A", "B", [
            {"key": "f", "type": "file", "path": "kernel/fork.c"}, {"key": "f0", "type": "file", "path": "fs/Kconfig"},
            {"key": "d", "type": "dir", "path": "fs/ext4"}, {"key": "d0", "type": "dir", "path": "mm"},
            {"key": "s", "type": "symbol", "name": "copy_process"}, {"key": "g", "type": "symbol", "name": "gone"},
            {"key": "k", "type": "kconfig", "name": "EXT4_FS"}, {"key": "m", "type": "section", "name": "EXT4 FILE SYSTEM"},
            {"key": "o", "type": "section", "name": "OTHER"}, {"key": "x", "type": "file", "path": "../etc/passwd"}, {"key": "y", "type": "symbol", "name": "a b"},
        ])}
        self.assertEqual((res["f"]["status"], res["f"]["changed"]), ("modified", True))
        self.assertEqual((res["f0"]["status"], res["f0"]["changed"]), ("same", False))
        self.assertEqual((res["d"]["summary"], len(res["d"]["paths"])), ("2 files changed", 2))
        self.assertFalse(res["d0"]["changed"])
        self.assertEqual((res["s"]["status"], res["s"]["changed"]), ("cosmetic", False), "whitespace and comments only")
        self.assertEqual(res["g"]["status"], "removed")
        self.assertEqual(res["k"]["status"], "same")
        self.assertEqual(res["m"]["status"], "modified")
        self.assertEqual(res["o"]["status"], "removed")
        self.assertEqual(res["x"]["error"], "invalid path")
        self.assertEqual(res["y"]["error"], "invalid name")

    def test_helpers(self) -> None:
        self.assertEqual(compare_texts(None, None, str), "absent")
        self.assertEqual(compare_texts("a", None, str), "removed")
        self.assertEqual(section_block("X\nM:\ta\n\nY\nM:\tb\n", "Y"), "Y\nM:\tb")
        self.assertIsNone(section_block("X\n", "Z"))


if __name__ == "__main__":
    unittest.main()
