"""tests/test_webapp_file_references.py - "Referenced by" of a file (GET /api/fs/references, D10)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

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


class Cur:
    def __init__(self, known: bool = True) -> None:
        self.rows: list = []
        self.statements: list[str] = []
        self.known = known

    def execute(self, sql: str, params=()) -> None:
        self.statements.append(sql)
        names = {p: 100 + i for i, p in enumerate(sorted({r[4] for r in REFS}))}
        if "FROM m_file_name WHERE fname" in sql:
            self.rows = [{"fnid": 5}] if self.known else []
        elif "FROM m_file_name WHERE fnid IN" in sql:
            self.rows = [{"fnid": f, "fname": p} for p, f in names.items() if f in params]
        elif "SELECT ref_id, details FROM m_file_reference" in sql:
            self.rows = [{"ref_id": i, "details": REFS[i][2]} for i in params]
        elif "FROM m_file_reference" in sql:
            self.rows = [{"ref_id": i, "ref_type": t, "line_no": ln, "source_blob_id": b, "source_fnid": names[p]}
                         for i, (t, ln, d, b, p) in enumerate(REFS)]
        elif "FROM m_kbuild_symbol" in sql:
            self.rows = [{"source_blob_id": b, "line_no": ln, "symbol": s, "rule": r, "fname": p} for b, ln, s, r, p in KBUILD]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class TestFileReferences(unittest.TestCase):
    def call(self, cur: Cur, path: str = "include/linux/sched.h", limit: int = 200) -> dict:
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = cur
        with mock.patch.object(fss, "get_db_cursor", return_value=ctx), \
                mock.patch.object(fss, "get_version_info", return_value=(1, "v3.0")), \
                mock.patch.object(fss.git_tree_resolver, "blob_paths", return_value=BLOB_PATHS):
            return fss.FilesystemService().get_file_references("v3.0", path, limit)

    def test_kinds_and_tree_filter(self) -> None:
        cur = Cur()
        res = self.call(cur)
        self.assertTrue(res["indexed"])
        self.assertEqual(res["included_by"], [{"path": "kernel/a.c", "line": 1}, {"path": "kernel/fork.c", "line": 3}])
        self.assertEqual(res["sourced_by"], [{"path": "arch/x/Kconfig", "line": 4}])
        self.assertEqual(res["documented_in"], [{"path": "Documentation/a.txt", "line": 9, "details": "see sched.h"}])
        self.assertEqual(res["built_by"], [{"path": "kernel/Makefile", "line": 2, "rule": "obj-$(CONFIG_FOO) += x.o", "symbols": ["FOO", "BAR"]}])
        self.assertEqual(res["totals"], {"included_by": 2, "sourced_by": 1, "documented_in": 1, "built_by": 1})
        self.assertFalse(res["truncated"])
        self.assertEqual(res["includes_source"], "parser")
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in cur.statements))

    def test_cap_and_truncated(self) -> None:
        res = self.call(Cur(), limit=1)
        self.assertEqual(len(res["included_by"]), 1)
        self.assertEqual(res["totals"]["included_by"], 2)
        self.assertTrue(res["truncated"])

    def test_unknown_path_and_validation(self) -> None:
        res = self.call(Cur(known=False), path="no/such.h")
        self.assertEqual((res["included_by"], res["built_by"], res["indexed"]), ([], [], False))
        with self.assertRaises(HTTPException):
            self.call(Cur(), path="../etc/passwd")


if __name__ == "__main__":
    unittest.main()
