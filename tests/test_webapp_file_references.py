"""tests/test_webapp_file_references.py - "Referenced by" of a file (GET /webapi/fs/references, D10)."""
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



BIND, NLM = "include/linux/lockd/bind.h", "include/linux/lockd/nlm.h"


def include_db(target: str, kind: str = "mock"):
    """bind.h of version 1 with a compiler reference (ref_type 1) to the file named `target`; the checkpoint tree
    holds bind.h and nlm.h (`linux/lockd/nlm.h` is the written spelling a parse without include directories kept)."""
    from tests.db_fixtures import make_db

    return make_db(kind, rows={
        "m_file_name": [{"fnid": 1, "fname": BIND}, {"fnid": 2, "fname": "linux/lockd/nlm.h"}, {"fnid": 3, "fname": NLM}],
        "m_file_blob": [{"blob_id": 10, "blob_hash": b"\x01" * 20, "ftype": 1},
                        {"blob_id": 11, "blob_hash": b"\x02" * 20, "ftype": 1}],
        "m_bridge_checkpoint": [{"commit_id": 1, "fnid": 1, "blob_id": 10}, {"commit_id": 1, "fnid": 3, "blob_id": 11}],
        "m_file_reference": [{"ref_id": 1, "target_fnid": {"linux/lockd/nlm.h": 2, NLM: 3}[target], "ref_type": 1,
                              "line_no": 12, "source_blob_id": 10, "source_fnid": 1, "details": ""}],
    })


class TestResolveInclude(unittest.TestCase):
    """filesystem_service.resolve_include(): a stored reference names its target only when the revision holds it."""

    def resolve(self, db, header: str = "<linux/lockd/nlm.h>") -> dict:
        fss._INCLUDE_CACHE.clear()
        self.addCleanup(fss._INCLUDE_CACHE.clear)
        from webapp.backend.services import arch_layout_service as als
        from webapp.backend.services import tree_resolver as tr
        with patch_reader(db), \
                mock.patch.object(fss, "get_version_info", return_value=(1, "2.3.35pre2")), \
                mock.patch.object(tr, "definition_arch", return_value=("i386", None, 32)), \
                mock.patch.object(als.arch_layout_service, "resolve_include", return_value=None), \
                mock.patch.object(fss.git_tree_resolver, "list_tree", return_value={BIND: "a" * 40, NLM: "b" * 40}):
            return fss.FilesystemService().resolve_include("2.3.35pre2", header, current_file=BIND)

    def test_reference_to_a_file_of_the_tree(self) -> None:
        for kind in ("mock", "sqlite"):
            res = self.resolve(include_db(NLM, kind))
            self.assertEqual((res["path"], res["method"]), (NLM, "file_reference"), kind)

    def test_unresolved_spelling_does_not_hijack_the_search(self) -> None:
        for kind in ("mock", "sqlite"):
            res = self.resolve(include_db("linux/lockd/nlm.h", kind))
            self.assertEqual(res["path"], NLM, kind)
            self.assertNotEqual(res["method"], "file_reference", kind)


def object_db(paths: list[str], kind: str = "mock"):
    """Database with `paths` present as non-directory files in version 1's checkpoint tree."""
    from tests.db_fixtures import make_db

    return make_db(kind, rows={
        "m_file_name": [{"fnid": i + 1, "fname": p} for i, p in enumerate(paths)],
        "m_file_blob": [{"blob_id": i + 10, "blob_hash": bytes([i + 1]) * 20, "ftype": 1} for i in range(len(paths))],
        "m_bridge_checkpoint": [{"commit_id": 1, "fnid": i + 1, "blob_id": i + 10} for i in range(len(paths))],
    })


class TestResolveObject(unittest.TestCase):
    """filesystem_service.resolve_object(): priority resolution, relative paths, git fallback, and traversal checks."""

    def resolve(self, db, obj: str, current_file: str | None = None, *, vid: int | None = 1,
                git_tree: dict[str, str] | None = None) -> dict:
        fss._OBJECT_CACHE.clear()
        self.addCleanup(fss._OBJECT_CACHE.clear)
        with patch_reader(db), \
                mock.patch.object(fss, "get_version_info", return_value=(vid, "v3.0")), \
                mock.patch.object(fss.git_tree_resolver, "list_tree", return_value=git_tree or {}):
            return fss.FilesystemService().resolve_object("v3.0", obj, current_file=current_file)

    def test_priority_order_c_over_S_s_rs(self) -> None:
        for kind in ("mock", "sqlite"):
            # .c wins over .S, .s, .rs
            db_all = object_db(["kernel/foo.rs", "kernel/foo.s", "kernel/foo.S", "kernel/foo.c"], kind)
            res = self.resolve(db_all, "foo.o", "kernel/Makefile")
            self.assertEqual((res["resolved"], res["path"], res["object"]), (True, "kernel/foo.c", "kernel/foo.o"), kind)

            # .S wins over .s, .rs
            db_cap_s = object_db(["kernel/foo.rs", "kernel/foo.s", "kernel/foo.S"], kind)
            self.assertEqual(self.resolve(db_cap_s, "foo.o", "kernel/Makefile")["path"], "kernel/foo.S", kind)

            # .s wins over .rs
            db_low_s = object_db(["kernel/foo.rs", "kernel/foo.s"], kind)
            self.assertEqual(self.resolve(db_low_s, "foo.o", "kernel/Makefile")["path"], "kernel/foo.s", kind)

            # .rs fallback
            db_rs = object_db(["kernel/foo.rs"], kind)
            self.assertEqual(self.resolve(db_rs, "foo.o", "kernel/Makefile")["path"], "kernel/foo.rs", kind)

    def test_relative_sub_and_parent_paths(self) -> None:
        db = object_db(["drivers/net/sub/foo-bar.c", "drivers/common/foo.S"])
        res_sub = self.resolve(db, "sub/foo-bar.o", "drivers/net/Makefile")
        self.assertEqual((res_sub["resolved"], res_sub["path"]), (True, "drivers/net/sub/foo-bar.c"))

        res_parent = self.resolve(db, "../common/foo.o", "drivers/net/Makefile")
        self.assertEqual((res_parent["resolved"], res_parent["path"]), (True, "drivers/common/foo.S"))

    def test_git_tree_fallback_for_unparsed_revision(self) -> None:
        db = object_db([])
        git_tree = {"drivers/tty/serial/8250_core.S": "a" * 40, "drivers/tty/serial/8250_core.c": "b" * 40}
        res = self.resolve(db, "8250_core.o", "drivers/tty/serial/Makefile", vid=0, git_tree=git_tree)
        self.assertEqual((res["resolved"], res["path"], res["method"]), (True, "drivers/tty/serial/8250_core.c", "git_tree"))

    def test_unresolved_composite_object(self) -> None:
        db = object_db(["kernel/a.c", "kernel/b.c"])
        res = self.resolve(db, "my_mod.o", "kernel/Makefile")
        self.assertEqual(res, {"object": "kernel/my_mod.o", "path": None, "resolved": False})

    def test_traversal_and_invalid_inputs_rejected(self) -> None:
        db = object_db(["kernel/fork.c"])
        for bad_obj, cur in [
            ("../etc/passwd.o", None),
            ("../etc/passwd.o", "Makefile"),
            ("../../etc/passwd.o", "kernel/Makefile"),
            ("/etc/passwd.o", "kernel/Makefile"),
            ("foo\0bar.o", "kernel/Makefile"),
            ("", "kernel/Makefile"),
            ("fork.o", "../../etc/passwd"),
        ]:
            with self.assertRaises(HTTPException, msg=f"Expected 400 for obj={bad_obj!r}, cur={cur!r}"):
                self.resolve(db, bad_obj, cur)

        with self.assertRaises(HTTPException):
            fss.FilesystemService().resolve_object("-bad-rev", "fork.o", "kernel/Makefile")


if __name__ == "__main__":
    unittest.main()
