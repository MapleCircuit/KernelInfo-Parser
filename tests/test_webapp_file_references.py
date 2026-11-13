"""tests/test_webapp_file_references.py - "Referenced by" of a file (GET /api/fs/references, D10)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from db_engine import MockDB
from tests.db_fixtures import patch_reader
from webapp.backend.services import filesystem_service as fss

REFS = [
    # ref_type, line, details, source blob, source path
    (1, 3, "", 10, "kernel/fork.c"),
    (1, 7, "", 99, "kernel/fork.c"),            # an older blob of the file: not in the revision
    (1, 1, "", 11, "kernel/a.c"),
    (2, 4, "", 12, "arch/x/Kconfig"),
    (5, 9, "see sched.h", 13, "Documentation/a.txt"),
]
KBUILD = [
    (14, 2, "FOO", "obj-$(CONFIG_FOO) += x.o", "kernel/Makefile"),
    (14, 2, "BAR", "obj-$(CONFIG_FOO) += x.o", "kernel/Makefile"),   # an enclosing condition of the same rule
    (98, 5, "OLD", "obj-y += x.o", "kernel/Makefile"),
]
BLOB_PATHS = {10: ["kernel/fork.c"], 11: ["kernel/a.c"], 12: ["arch/x/Kconfig"], 13: ["Documentation/a.txt"],
              14: ["kernel/Makefile"], 20: ["include/linux/sched.h"]}


TARGET = "include/linux/sched.h"


def make_db(known: bool = True) -> MockDB:
    """A database holding the references and Kbuild rules of `TARGET` (nothing when `known` is false)."""
    db = MockDB(use_global=False)
    paths = sorted({r[4] for r in REFS} | {r[4] for r in KBUILD})
    fnid = {p: 100 + i for i, p in enumerate(paths)}
    db.insert_rows("m_file_name", ["fnid", "fname"], [(f, p) for p, f in fnid.items()] + ([(5, TARGET)] if known else []))
    db.insert_rows(
        "m_file_reference",
        ["ref_id", "target_fnid", "ref_type", "line_no", "source_blob_id", "source_fnid", "details"],
        [(i + 1, 5, t, ln, b, fnid[p], d) for i, (t, ln, d, b, p) in enumerate(REFS)],
    )
    db.insert_rows(
        "m_kbuild_symbol",
        ["source_blob_id", "source_fnid", "idx", "via", "line_no", "symbol", "rule", "target_fnid"],
        [(b, fnid[p], i, 1, ln, s, r, 5) for i, (b, ln, s, r, p) in enumerate(KBUILD)],
    )
    return db


class TestFileReferences(unittest.TestCase):
    def call(self, db: MockDB, path: str = TARGET, limit: int = 200) -> dict:
        with patch_reader(db), \
                mock.patch.object(fss, "get_version_info", return_value=(1, "v3.0")), \
                mock.patch.object(fss.git_tree_resolver, "blob_paths", return_value=BLOB_PATHS):
            return fss.FilesystemService().get_file_references("v3.0", path, limit)

    def test_kinds_and_tree_filter(self) -> None:
        res = self.call(make_db())
        self.assertTrue(res["indexed"])
        self.assertEqual(res["included_by"], [{"path": "kernel/a.c", "line": 1}, {"path": "kernel/fork.c", "line": 3}])
        self.assertEqual(res["sourced_by"], [{"path": "arch/x/Kconfig", "line": 4}])
        self.assertEqual(res["documented_in"], [{"path": "Documentation/a.txt", "line": 9, "details": "see sched.h"}])
        self.assertEqual(res["built_by"], [{"path": "kernel/Makefile", "line": 2, "rule": "obj-$(CONFIG_FOO) += x.o", "symbols": ["FOO", "BAR"]}])
        self.assertEqual(res["totals"], {"included_by": 2, "sourced_by": 1, "documented_in": 1, "built_by": 1})
        self.assertFalse(res["truncated"])
        self.assertEqual(res["includes_source"], "parser")

    def test_cap_and_truncated(self) -> None:
        res = self.call(make_db(), limit=1)
        self.assertEqual(len(res["included_by"]), 1)
        self.assertEqual(res["totals"]["included_by"], 2)
        self.assertTrue(res["truncated"])

    def test_unknown_path_and_validation(self) -> None:
        res = self.call(make_db(known=False), path="no/such.h")
        self.assertEqual((res["included_by"], res["built_by"], res["indexed"]), ([], [], False))
        with self.assertRaises(HTTPException):
            self.call(make_db(), path="../etc/passwd")


if __name__ == "__main__":
    unittest.main()
