"""tests/test_webapp_imports.py - Symbols a file imports through its #include chain (webapp/API_ENDPOINTS.md section 4.2)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from core import DBLayout as D
from core.globalstuff import ASTT, SymbolRole
from tests.db_fixtures import make_db, patch_reader
from webapp.backend.services import imports_service as isvc
from webapp.backend.services.imports_service import ImportsService, import_kind, include_chain

CALL, TYPE, DECL, MACRO, MEMBER = (int(SymbolRole.Call), int(SymbolRole.TypeUsage), int(SymbolRole.DeclRef),
                                   int(SymbolRole.MacroExpansion), int(SymbolRole.MemberRef))
FORCED = "include/linux/kconfig.h"
SHA = {p: f"{i + 1:040x}" for i, p in enumerate(["a.c", "b.h", "c.h", "d.h", "e.h", FORCED, "k2.h", "Makefile", "u.c"])}
BLOB = {"a.c": 1, "b.h": 2, "c.h": 3, "d.h": 4, "e.h": 5, FORCED: 6, "k2.h": 7}
# a.c -> b.h -> {c.h, e.h}; kconfig.h is forced and includes k2.h; d.h is not reached
REACHED = {"a.c": None, FORCED: None, "b.h": "a.c", "k2.h": FORCED, "c.h": "b.h", "e.h": "b.h"}

# (name, header, type name, line, linkage)
DEFS = [
    ("b_func", "b.h", "C_functionprotodecl", 10, 0),
    ("B_MACRO", "b.h", "CPPro_define_macro", 3, 5),
    ("unref_fn", "b.h", "C_functionprotodecl", 20, 0),
    ("local_fn", "b.h", "C_functionprotodecl", 30, 2),
    ("c_proto", "c.h", "C_functionprotodecl", 5, 2),
    ("c_struct", "c.h", "C_structdecl", 7, 0),
    ("c_typedef", "c.h", "C_SCtypedef", 9, 0),
    ("c_enum", "c.h", "C_enumdecl", 11, 0),
    ("c_union", "c.h", "C_uniondecl", 13, 0),
    ("c_var", "c.h", "C_int", 15, 2),
    ("C_CONST", "c.h", "C_enumequal", 17, 0),
    ("field", "c.h", "C_int", 8, 0),
    ("tie_fn", "c.h", "C_functionprotodecl", 19, 2),
    ("tie_fn", "e.h", "C_functionprotodecl", 4, 0),
    ("ghost", "d.h", "C_functionprotodecl", 1, 0),
    ("IS_ENABLED", FORCED, "CPPro_define_macro", 50, 5),
    ("k2_var", "k2.h", "C_int", 2, 0),
    ("local_fn", "a.c", "C_functionprotodecl", 40, 0),
]
# (name, role, line) referenced by a.c
REFS = [
    ("b_func", CALL, 100), ("b_func", CALL, 90), ("c_proto", CALL, 101), ("c_struct", TYPE, 102),
    ("c_typedef", TYPE, 103), ("c_enum", TYPE, 104), ("c_union", TYPE, 105), ("B_MACRO", MACRO, 106),
    ("IS_ENABLED", MACRO, 107), ("k2_var", DECL, 108), ("c_var", DECL, 109), ("C_CONST", DECL, 110),
    ("local_fn", CALL, 111), ("ghost", CALL, 112), ("field", MEMBER, 113), ("tie_fn", CALL, 114),
]


def _db():
    names = sorted({n for n, *_ in DEFS} | {n for n, *_ in REFS})
    ast = {n: i + 1 for i, n in enumerate(names)}
    return make_db("mock", rows={
        "m_type_descriptor": [{"type_id": v, "name": n} for v, n in D.m_type_descriptor_insert],
        "m_ast": [{"ast_id": i, "name": n, "type_id": 0} for n, i in ast.items()],
        "m_blob_symbol_def": [
            {"def_id": i + 1, "blob_id": BLOB[h], "tag_id": 1, "ast_id": ast[n], "name": n, "type_id": ASTT[t].value,
             "linkage": lk, "line_s": line, "line_e": line}
            for i, (n, h, t, line, lk) in enumerate(DEFS)
        ],
        "m_blob_symbol_ref": [
            {"ref_id": i + 1, "blob_id": BLOB["a.c"], "tag_id": 1, "ast_id": ast[n], "target_fnid": 0, "role": role,
             "line": line, "char_s": 1}
            for i, (n, role, line) in enumerate(REFS)
        ],
    })


class TestImportsHelpers(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertEqual(import_kind("C_functionprotodecl", 0), "function")
        self.assertEqual(import_kind("C_functionprotodecl", 2), "prototype")
        self.assertEqual(import_kind("C_structdecl", 0), "struct")
        self.assertEqual(import_kind("C_structnotbind", 0), "struct")
        for t in ("C_SCtypedef", "C_enumdecl", "C_uniondecl"):
            self.assertEqual(import_kind(t, 0), "type")
        for t in ("CPPro_define", "CPPro_define_macro", "ASM_Macro"):
            self.assertEqual(import_kind(t, 5), "macro")
        self.assertEqual(import_kind("C_int", 0), "variable")
        self.assertEqual(import_kind("C_enumequal", 0), "variable")
        for t in ("C_ExportSymbolDef", "Kconfig_Config", "ASM_Instruction", "Rust_Fn", "Rust_Variant"):
            self.assertIsNone(import_kind(t, 0))

    def test_chain(self) -> None:
        self.assertEqual(include_chain(REACHED, "c.h"), ["a.c", "b.h", "c.h"])
        self.assertEqual(include_chain(REACHED, "k2.h"), [FORCED, "k2.h"])
        self.assertEqual(include_chain({"x.h": "y.h", "y.h": "x.h"}, "x.h"), ["y.h", "x.h"])  # cycle stops


class TestImportsService(unittest.TestCase):
    def _svc(self, blob_ids=None, reached=None):
        ids = {SHA[p]: (b, 1) for p, b in (BLOB if blob_ids is None else blob_ids).items()}
        reader = patch.object(isvc, "get_reader", wraps=isvc.get_reader)
        patches = [
            patch_reader(_db()),
            patch.object(isvc.git_tree_resolver, "list_tree", side_effect=lambda rev: dict(SHA) if rev.startswith("v") else None),
            patch.object(isvc.git_tree_resolver, "resolve_commit_sha", return_value="f" * 40),
            patch.object(isvc.GitTreeResolver, "map_blob_hashes",
                         side_effect=lambda _r, shas: {s: ids[s] for s in shas if s in ids}),
            patch.object(isvc.arch_layout_service, "reachable", return_value=dict(REACHED if reached is None else reached)),
            reader,
        ]
        mocks = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        return ImportsService(), mocks[-2], mocks[-1]

    def test_imports_chains_kinds_and_filtering(self) -> None:
        svc, reachable, _ = self._svc()
        out = svc.get_imports("v3.0", "a.c", "arm64")
        reachable.assert_called_once_with("v3.0", "arm64", "a.c")
        self.assertEqual((out["indexed"], out["layout"], out["arch"], out["reachable"]), (True, True, "arm64", 5))
        items = {it["name"]: it for it in out["items"]}
        self.assertEqual(set(items), {"b_func", "B_MACRO", "c_proto", "c_struct", "c_typedef", "c_enum", "c_union",
                                      "c_var", "C_CONST", "tie_fn", "IS_ENABLED", "k2_var"})
        self.assertEqual(out["total"], len(items))
        # Not referenced, defined by a.c itself, unreachable, member references
        for name in ("unref_fn", "local_fn", "ghost", "field"):
            self.assertNotIn(name, items)
        kinds = {n: it["kind"] for n, it in items.items()}
        self.assertEqual(kinds["b_func"], "function")
        self.assertEqual(kinds["c_proto"], "prototype")
        self.assertEqual(kinds["c_struct"], "struct")
        self.assertEqual({kinds[n] for n in ("c_typedef", "c_enum", "c_union")}, {"type"})
        self.assertEqual({kinds[n] for n in ("B_MACRO", "IS_ENABLED")}, {"macro"})
        self.assertEqual({kinds[n] for n in ("c_var", "C_CONST", "k2_var")}, {"variable"})
        # Direct and multi-hop chains
        self.assertEqual((items["b_func"]["chain"], items["b_func"]["forced"]), (["a.c", "b.h"], False))
        self.assertEqual(items["c_struct"]["chain"], ["a.c", "b.h", "c.h"])
        self.assertEqual((items["c_struct"]["header_path"], items["c_struct"]["line"]), ("c.h", 7))
        # Compiler-forced -include roots
        self.assertEqual((items["IS_ENABLED"]["chain"], items["IS_ENABLED"]["forced"]), ([FORCED], True))
        self.assertEqual((items["k2_var"]["chain"], items["k2_var"]["forced"]), ([FORCED, "k2.h"], True))
        # Reference lines in the current file
        self.assertEqual((items["b_func"]["ref_line"], items["b_func"]["ref_lines"], items["b_func"]["ref_count"]), (90, [90, 100], 2))
        # Same chain length: the full definition wins over the prototype
        self.assertEqual((items["tie_fn"]["header_path"], items["tie_fn"]["kind"], items["tie_fn"]["alternatives"]), ("e.h", "function", 1))
        # Deterministic order: kind, then name
        order = [(isvc._KIND_RANK[it["kind"]], it["name"]) for it in out["items"]]
        self.assertEqual(order, sorted(order))

    def test_cached_per_commit_arch_and_path(self) -> None:
        svc, reachable, reader = self._svc()
        first = svc.get_imports("v3.0", "a.c")
        calls = reader.call_count
        self.assertEqual(svc.get_imports("v3.0", "a.c"), first)
        self.assertEqual(reader.call_count, calls)
        svc.get_imports("v3.0", "a.c", "arm")
        self.assertGreater(reader.call_count, calls)
        self.assertEqual(reachable.call_count, 2)

    def test_no_layout(self) -> None:
        svc, _, _ = self._svc(reached={})
        out = svc.get_imports("v3.0", "a.c")
        self.assertEqual((out["indexed"], out["layout"], out["items"]), (True, False, []))

    def test_unindexed_and_unsupported(self) -> None:
        svc, reachable, reader = self._svc(blob_ids={})
        out = svc.get_imports("v3.0", "a.c")
        self.assertEqual((out["indexed"], out["items"], out["reason"]), (False, [], "unindexed"))
        reachable.assert_not_called()
        calls = reader.call_count
        out = svc.get_imports("v3.0", "Makefile")
        self.assertEqual((out["indexed"], out["items"], out["reason"]), (False, [], "unsupported"))
        self.assertEqual(reader.call_count, calls)

    def test_invalid_input(self) -> None:
        svc, _, _ = self._svc()
        for rev, path, arch, code in (("--output=x", "a.c", "x86_64", 400), ("v3.0", "../etc/passwd.h", "x86_64", 400),
                                      ("v3.0", "", "x86_64", 400), ("v3.0", "a.c", "x86;rm", 400),
                                      ("v3.0", "missing.c", "x86_64", 404), ("nope", "a.c", "x86_64", 404)):
            with self.assertRaises(HTTPException) as ctx:
                svc.get_imports(rev, path, arch)
            self.assertEqual(ctx.exception.status_code, code, (rev, path, arch))

    def test_endpoint(self) -> None:
        from fastapi.testclient import TestClient
        from webapp.backend.app import create_app

        self._svc()
        with patch("webapp.backend.routers.filesystem.imports_service", ImportsService()):
            client = TestClient(create_app())
            res = client.get("/api/fs/imports/v3.0", params={"path": "a.c", "arch": "x86_64"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["path"], "a.c")
            self.assertEqual(client.get("/api/fs/imports/v3.0", params={"path": "../x.c"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
