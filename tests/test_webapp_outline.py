"""tests/test_webapp_outline.py - Definitions of one file for the code view outline (webapp/API.md section 4.2)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from core import DBLayout as D
from core.globalstuff import ASTT
from tests.db_fixtures import make_db, patch_reader
from webapp.backend.services import outline_service as osvc
from webapp.backend.services.outline_service import OutlineService, build_outline, flatten, outline_kind


def row(name, type_name, s, e, linkage=1):
    return {"name": name, "type_name": type_name, "line_s": s, "line_e": e, "linkage": linkage}


class TestOutlineModel(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertEqual(outline_kind("C_functionprotodecl", 1), "function")
        self.assertEqual(outline_kind("C_functionprotodecl", 2), "prototype")
        self.assertEqual(outline_kind("C_structdecl", 0), "type")
        self.assertEqual(outline_kind("CPPro_define_macro", 5), "macro")
        self.assertEqual(outline_kind("C_enumequal", 0), "constant")
        self.assertEqual(outline_kind("C_ExportSymbolDef", 5), "api")
        self.assertEqual(outline_kind("Rust_ModuleAttr", 5), "api")
        self.assertEqual(outline_kind("C_unsigned", 2), "variable")
        self.assertEqual(outline_kind("Rust_Fn", 0), "function")
        self.assertEqual(outline_kind("Kconfig_Config", None), "kconfig")
        self.assertEqual(outline_kind("ASM_Label", 1, 10, 20), "function")
        self.assertEqual(outline_kind("ASM_Label", None, 15, 15), "label")
        self.assertEqual(outline_kind("ASM_Instruction", None, 12, 12), "instruction")

    def test_assembly_nesting(self) -> None:
        rows = [
            row("common_startup", "ASM_Label", 10, 30, 1),
            row("loop_entry", "ASM_Label", 15, 15, None),
            row("nop", "ASM_Instruction", 16, 16, None),
        ]
        items = build_outline(rows)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "common_startup")
        self.assertEqual(items[0]["kind"], "function")
        self.assertEqual([c["name"] for c in items[0]["children"]], ["loop_entry", "nop"])

    def test_sorting_and_nesting(self) -> None:
        rows = [
            row("copy_process", "C_functionprotodecl", 1049, 1418),
            row("cpu_idle_type", "C_enumdecl", 10, 15, 0),
            row("CPU_IDLE", "C_enumequal", 11, 11, 0),
            row("CPU_NOT_IDLE", "C_enumequal", 12, 12, 0),
            row("do_fork", "C_functionprotodecl", 5, 5, 2),
            row("lock", "C_Compound", 1100, 1100, 1),  # a static inside copy_process
            row("copy_process", "C_functionprotodecl", 1049, 1418),  # duplicate row (shared + per-blob)
        ]
        items = build_outline(rows)
        self.assertEqual([i["name"] for i in items], ["do_fork", "cpu_idle_type", "copy_process"])
        self.assertEqual([c["name"] for c in items[1]["children"]], ["CPU_IDLE", "CPU_NOT_IDLE"])
        self.assertEqual([c["name"] for c in items[2]["children"]], ["lock"])
        self.assertEqual(len(flatten(items)), 6)
        self.assertEqual(items[0]["kind"], "prototype")


class TestOutlineService(unittest.TestCase):
    def _svc(self, tree, blob_ids, rows):
        """Service over a database whose blob 42 defines `rows`; `reader` counts the `get_reader()` calls."""
        db = make_db("mock", rows={
            "m_type_descriptor": [{"type_id": v, "name": n} for v, n in D.m_type_descriptor_insert],
            "m_blob_symbol_def": [
                {"def_id": i + 1, "blob_id": 42, "tag_id": 1, "ast_id": 1, "name": r["name"], "type_id": ASTT[r["type_name"]].value,
                 "linkage": r["linkage"], "line_s": r["line_s"], "line_e": r["line_e"]}
                for i, r in enumerate(rows)
            ],
        })
        reader = patch.object(osvc, "get_reader", wraps=osvc.get_reader)
        patches = [
            patch_reader(db),
            patch.object(osvc.git_tree_resolver, "list_tree", return_value=tree),
            patch.object(osvc.GitTreeResolver, "map_blob_hashes", return_value=blob_ids),
            reader,
        ]
        mocks = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        return OutlineService(), mocks[-1]

    def test_indexed_blob_read_only_and_cached(self) -> None:
        sha = "a" * 40
        svc, reader = self._svc({"kernel/fork.c": sha}, {sha: (42, 1)}, [row("copy_process", "C_functionprotodecl", 10, 20)])
        out = svc.get_outline("v3.0", "kernel/fork.c")
        self.assertTrue(out["indexed"])
        self.assertEqual(out["count"], 1)
        self.assertEqual([i["name"] for i in out["items"]], ["copy_process"])
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(svc.get_outline("v3.1", "kernel/fork.c")["count"], 1)  # same blob: cached
        self.assertEqual(reader.call_count, 1)

    def test_unindexed_blob_and_errors(self) -> None:
        sha = "b" * 40
        svc, cursor = self._svc({"x.c": sha}, {}, [])
        out = svc.get_outline("v3.0", "x.c")
        self.assertEqual((out["indexed"], out["items"]), (False, []))
        for bad in ("../etc/passwd", ""):
            with self.assertRaises(HTTPException) as ctx:
                svc.get_outline("v3.0", bad)
            self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            svc.get_outline("v3.0", "missing.c")
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            svc.get_outline("--output=x", "x.c")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_git_fallback_pure_extractors(self) -> None:
        c_sha = "c" * 40
        svc, _ = self._svc({"test.c": c_sha, "test.S": "d" * 40}, {}, [])
        with patch("webapp.backend.services.git_reader.git_reader.read_object", return_value=("blob", b"void test_fn() {}\n", c_sha)):
            out = svc.get_outline("v3.0", "test.c")
            self.assertTrue(out["indexed"])
            self.assertEqual(out["items"][0]["name"], "test_fn")
            self.assertEqual(out["items"][0]["kind"], "function")


if __name__ == "__main__":
    unittest.main()
