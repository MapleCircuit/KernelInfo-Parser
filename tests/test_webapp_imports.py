"""tests/test_webapp_imports.py - Symbols a file imports through its #include chain (webapp/API_ENDPOINTS.md section 4.2)."""
from __future__ import annotations

import os
import sys
import unittest
from typing import ClassVar
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from core import DBLayout as D
from core.globalstuff import ASTT, SymbolRole
from tests.db_fixtures import make_db, patch_reader
from webapp.backend.services import imports_service as isvc
from webapp.backend.services.imports_service import (
    ImportsService,
    def_namespace,
    import_kind,
    include_chain,
    include_spellings,
    ref_namespaces,
)

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
            res = client.get("/webapi/fs/imports/v3.0", params={"path": "a.c", "arch": "x86_64"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["path"], "a.c")
            self.assertEqual(client.get("/webapi/fs/imports/v3.0", params={"path": "../x.c"}).status_code, 400)


# A header compiled in its includer's context (2.6.0 include/linux/lockd/bind.h): h.h includes nothing that
# defines what it uses; x.c includes fs.h, then h.h
CSHA = {"h.h": "a" * 40, "x.c": "b" * 40, "fs.h": "c" * 40, "late.h": "d" * 40}
CBLOB = {"h.h": 10, "x.c": 11, "fs.h": 12, "late.h": 13}
# (name, header, type name, line, linkage)
CDEFS = [
    ("file", "fs.h", "C_structdecl", 20, 0),
    ("file", "fs.h", "C_functionprotodecl", 30, 2),     # an ordinary `file()` must not answer `struct file`
    ("u32", "fs.h", "C_SCtypedef", 5, 0),
    ("own_tag", "fs.h", "C_structdecl", 40, 0),
    ("own_tag", "h.h", "C_functionprotodecl", 3, 0),    # h.h defines a function own_tag: the tag stays an import
    ("own_fn", "h.h", "C_functionprotodecl", 4, 0),
    ("own_fn", "fs.h", "C_functionprotodecl", 50, 2),
    ("CONFIG_FOO", "fs.h", "CPPro_define", 60, 5),
    ("body_fn", "fs.h", "C_functionprotodecl", 61, 2),
    ("late_fn", "late.h", "C_functionprotodecl", 1, 2),  # included after h.h: not context
]
# (name, role, line, target type)
CREFS = [
    ("file", TYPE, 7, "C_structnotbind"), ("u32", TYPE, 8, "C_SCtypedef"), ("own_tag", TYPE, 9, "C_structnotbind"),
    ("own_fn", CALL, 10, "C_functionprotodecl"), ("helper", CALL, 11, "C_functionprotnotbind"),
    ("CONFIG_FOO", int(SymbolRole.CondRef), 12, "CPPro_define"), ("body_fn", int(SymbolRole.MacroBodyRef), 13, "C_DeclRefExpr"),
    ("late_fn", CALL, 14, "C_functionprotnotbind"),
]


def _context_db():
    names = sorted({n for n, *_ in CDEFS} | {n for n, *_ in CREFS})
    ast = {n: i + 1 for i, n in enumerate(names)}
    target = {n: t for n, _r, _l, t in CREFS}
    return make_db("mock", rows={
        "m_type_descriptor": [{"type_id": v, "name": n} for v, n in D.m_type_descriptor_insert],
        "m_ast": [{"ast_id": i, "name": n, "type_id": ASTT[target[n]].value if n in target else 0} for n, i in ast.items()],
        "m_blob_symbol_def": [
            {"def_id": i + 1, "blob_id": CBLOB[h], "tag_id": 1, "ast_id": ast[n], "name": n, "type_id": ASTT[t].value,
             "linkage": lk, "line_s": line, "line_e": line}
            for i, (n, h, t, line, lk) in enumerate(CDEFS)
        ],
        "m_blob_symbol_ref": [
            {"ref_id": i + 1, "blob_id": CBLOB["h.h"], "tag_id": 1, "ast_id": ast[n], "target_fnid": 0, "role": role,
             "line": line, "char_s": 1}
            for i, (n, role, line, _t) in enumerate(CREFS)
        ],
        "m_file_name": [{"fnid": 1, "fname": "h.h"}, {"fnid": 2, "fname": "x.c"}, {"fnid": 3, "fname": "gone.c"}],
        "m_file_reference": [
            {"ref_id": 1, "source_blob_id": CBLOB["x.c"], "source_fnid": 2, "target_fnid": 1, "ref_type": 1, "line_no": 3, "details": ""},
            # An includer whose stored blob is not the revision's (another version) is ignored
            {"ref_id": 2, "source_blob_id": 99, "source_fnid": 3, "target_fnid": 1, "ref_type": 1, "line_no": 3, "details": ""},
        ],
    })


class TestImportsNameSpacesAndContext(unittest.TestCase):
    def _svc(self, own_reach=None, layouts=("x86_64",)):
        ids = {CSHA[p]: (b, 1) for p, b in CBLOB.items()}
        tree = {**CSHA, "gone.c": "e" * 40}
        own = {"h.h": None} if own_reach is None else own_reach
        patches = [
            patch_reader(_context_db()),
            patch.object(isvc.git_tree_resolver, "list_tree", return_value=tree),
            patch.object(isvc.git_tree_resolver, "resolve_commit_sha", return_value="f" * 40),
            patch.object(isvc.GitTreeResolver, "map_blob_hashes", side_effect=lambda _r, shas: {s: ids[s] for s in shas if s in ids}),
            patch.object(isvc.arch_layout_service, "layout", side_effect=lambda _rev, arch: object() if arch in layouts else None),
            patch.object(isvc.arch_layout_service, "reachable", side_effect=lambda _rev, arch, _p: dict(own) if arch in layouts else {}),
            patch.object(isvc.arch_layout_service, "reachable_before", return_value={"x.c": None, "fs.h": "x.c"}),
        ]
        mocks = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        return ImportsService(), mocks

    def test_name_spaces(self) -> None:
        self.assertEqual(ref_namespaces(TYPE, "C_structnotbind"), {"tag"})
        self.assertEqual(ref_namespaces(MACRO, "CPPro_define"), {"macro"})
        self.assertEqual(ref_namespaces(int(SymbolRole.CondRef), "CPPro_define"), {"macro"})
        self.assertEqual(ref_namespaces(CALL, "C_functionprotnotbind"), {"ordinary", "macro"})
        self.assertEqual(ref_namespaces(CALL, ""), {"tag", "macro", "ordinary"})
        self.assertEqual((def_namespace("C_uniondecl"), def_namespace("CPPro_define_macro"), def_namespace("C_SCtypedef")),
                         ("tag", "macro", "ordinary"))

    def test_context_imports_of_a_header_compiled_in_its_includer(self) -> None:
        svc, mocks = self._svc()
        out = svc.get_imports("v2.6.0", "h.h")
        items = {it["name"]: it for it in out["items"]}
        self.assertEqual(out["context"], ["x.c"])
        mocks[-1].assert_called_once_with("v2.6.0", "x86_64", "x.c", "h.h")
        self.assertEqual(set(items), {"file", "u32", "own_tag", "CONFIG_FOO", "body_fn"})
        # struct file, not the function file()
        self.assertEqual((items["file"]["kind"], items["file"]["line"], items["file"]["via"], items["file"]["context"]),
                         ("struct", 20, "context", "x.c"))
        self.assertEqual(items["file"]["chain"], ["x.c", "fs.h"])
        self.assertFalse(items["file"]["forced"])
        # h.h's function own_tag does not mask the struct tag; its own_fn masks the prototype in fs.h
        self.assertEqual(items["own_tag"]["kind"], "struct")
        self.assertNotIn("own_fn", items)
        # Tested macros and names used in macro bodies are imports too
        self.assertEqual((items["CONFIG_FOO"]["kind"], items["body_fn"]["kind"]), ("macro", "prototype"))
        # Declared nowhere before the include, or after it
        self.assertNotIn("helper", items)
        self.assertNotIn("late_fn", items)

    def test_own_chain_wins_over_context(self) -> None:
        svc, _mocks = self._svc(own_reach={"h.h": None, "fs.h": "h.h"})
        out = svc.get_imports("v2.6.0", "h.h")
        items = {it["name"]: it for it in out["items"]}
        self.assertEqual((items["file"]["via"], items["file"]["chain"]), ("include", ["h.h", "fs.h"]))
        # Only names the own chain leaves unresolved (helper, late_fn) are looked up in the context
        self.assertEqual({n for n, it in items.items() if it["via"] == "context"}, set())

    def test_arch_fallback(self) -> None:
        svc, _ = self._svc(layouts=("x86_64",))
        out = svc.get_imports("v2.6.0", "h.h", "x86")
        self.assertEqual((out["arch"], out["arch_used"]), ("x86", "x86_64"))
        svc, _ = self._svc(layouts=())
        out = svc.get_imports("v2.6.0", "h.h", "x86")
        self.assertEqual((out["arch_used"], out["layout"], out["items"], out["context"]), (None, False, [], []))

    def test_diversified_includers_and_forward_decl_completed_in_includer_header(self) -> None:
        # bind.h forward-declares `struct nlm_host;` and references `struct svc_rqst` and `CONFIG_LOCKD_V4`.
        # 5 client .c files in fs/nfs/ do not include svc.h; fs/nfsd/nfssvc.c includes svc.h;
        # include/linux/lockd/lockd.h includes bind.h and defines `struct nlm_host` later in lockd.h itself;
        # fs/lockd/Kconfig defines LOCKD_V4.
        paths = [
            "include/linux/lockd/bind.h",
            "include/linux/lockd/lockd.h",
            "include/linux/sunrpc/svc.h",
            "fs/nfs/a.c", "fs/nfs/b.c", "fs/nfs/c.c", "fs/nfs/d.c", "fs/nfs/e.c",
            "fs/nfsd/nfssvc.c",
            "fs/lockd/Kconfig",
            "arch/arm/Kconfig",
        ]
        shas = {p: f"{i + 20:040x}" for i, p in enumerate(paths)}
        blobs = {p: i + 20 for i, p in enumerate(paths)}
        ids = {shas[p]: (blobs[p], 1) for p in paths}
        fnids = {p: i + 1 for i, p in enumerate(paths)}
        includers = ["fs/nfs/a.c", "fs/nfs/b.c", "fs/nfs/c.c", "fs/nfs/d.c", "fs/nfs/e.c",
                     "fs/nfsd/nfssvc.c", "include/linux/lockd/lockd.h"]
        db = make_db("mock", rows={
            "m_type_descriptor": [{"type_id": v, "name": n} for v, n in D.m_type_descriptor_insert],
            "m_ast": [
                {"ast_id": 1, "name": "nlm_host", "type_id": ASTT.C_structnotbind.value},
                {"ast_id": 2, "name": "svc_rqst", "type_id": ASTT.C_structnotbind.value},
                {"ast_id": 3, "name": "CONFIG_LOCKD_V4", "type_id": ASTT.CPPro_define.value},
                {"ast_id": 4, "name": "LOCKD_V4", "type_id": ASTT.Kconfig_Config.value},
            ],
            "m_blob_symbol_def": [
                # Forward decl in bind.h itself (must not mask external full definition)
                {"def_id": 1, "blob_id": blobs["include/linux/lockd/bind.h"], "tag_id": 1, "ast_id": 1,
                 "name": "nlm_host", "type_id": ASTT.C_structnotbind.value, "linkage": 0, "line_s": 18, "line_e": 18},
                # Full definition of nlm_host later in direct includer lockd.h
                {"def_id": 2, "blob_id": blobs["include/linux/lockd/lockd.h"], "tag_id": 1, "ast_id": 1,
                 "name": "nlm_host", "type_id": ASTT.C_structdecl.value, "linkage": 0, "line_s": 42, "line_e": 75},
                # Full definition of svc_rqst in svc.h (reached via fs/nfsd/nfssvc.c)
                {"def_id": 3, "blob_id": blobs["include/linux/sunrpc/svc.h"], "tag_id": 1, "ast_id": 2,
                 "name": "svc_rqst", "type_id": ASTT.C_structdecl.value, "linkage": 0, "line_s": 110, "line_e": 160},
                # Kconfig definition of LOCKD_V4 in fs/lockd/Kconfig (and an alternative in arch/arm/Kconfig)
                {"def_id": 4, "blob_id": blobs["fs/lockd/Kconfig"], "tag_id": 1, "ast_id": 4,
                 "name": "LOCKD_V4", "type_id": ASTT.Kconfig_Config.value, "linkage": 0, "line_s": 25, "line_e": 31},
                {"def_id": 5, "blob_id": blobs["arch/arm/Kconfig"], "tag_id": 1, "ast_id": 4,
                 "name": "LOCKD_V4", "type_id": ASTT.Kconfig_Config.value, "linkage": 0, "line_s": 99, "line_e": 102},
            ],
            "m_blob_symbol_ref": [
                {"ref_id": 1, "blob_id": blobs["include/linux/lockd/bind.h"], "tag_id": 1, "ast_id": 1,
                 "target_fnid": 0, "role": TYPE, "line": 28, "char_s": 1},
                {"ref_id": 2, "blob_id": blobs["include/linux/lockd/bind.h"], "tag_id": 1, "ast_id": 2,
                 "target_fnid": 0, "role": TYPE, "line": 30, "char_s": 1},
                {"ref_id": 3, "blob_id": blobs["include/linux/lockd/bind.h"], "tag_id": 1, "ast_id": 3,
                 "target_fnid": 0, "role": int(SymbolRole.CondRef), "line": 15, "char_s": 1},
            ],
            "m_file_name": [{"fnid": fid, "fname": p} for p, fid in fnids.items()],
            "m_file_reference": [
                {"ref_id": i + 1, "source_blob_id": blobs[inc], "source_fnid": fnids[inc],
                 "target_fnid": fnids["include/linux/lockd/bind.h"], "ref_type": 1, "line_no": 12, "details": ""}
                for i, inc in enumerate(includers)
            ],
        })

        def reach_before(_rev: str, _arch: str, includer: str, _target: str):
            if includer == "fs/nfsd/nfssvc.c":
                return {"fs/nfsd/nfssvc.c": None, "include/linux/sunrpc/svc.h": "fs/nfsd/nfssvc.c"}
            return {includer: None}

        with patch_reader(db), \
                patch.object(isvc.git_tree_resolver, "list_tree", return_value=shas), \
                patch.object(isvc.git_tree_resolver, "resolve_commit_sha", return_value="f" * 40), \
                patch.object(isvc.GitTreeResolver, "map_blob_hashes",
                             side_effect=lambda _r, s_list: {s: ids[s] for s in s_list if s in ids}), \
                patch.object(isvc.arch_layout_service, "layout", return_value=object()), \
                patch.object(isvc.arch_layout_service, "reachable",
                             return_value={"include/linux/lockd/bind.h": None}), \
                patch.object(isvc.arch_layout_service, "reachable_before", side_effect=reach_before):
            out = ImportsService().get_imports("v6.19", "include/linux/lockd/bind.h", "x86_64")

        items = {it["name"]: it for it in out["items"]}
        self.assertEqual(set(items), {"nlm_host", "svc_rqst", "CONFIG_LOCKD_V4"})
        # Forward-declared nlm_host resolved from direct includer header lockd.h itself
        self.assertEqual(
            (items["nlm_host"]["kind"], items["nlm_host"]["header_path"], items["nlm_host"]["line"],
             items["nlm_host"]["via"], items["nlm_host"]["context"], items["nlm_host"]["chain"]),
            ("struct", "include/linux/lockd/lockd.h", 42, "context", "include/linux/lockd/lockd.h",
             ["include/linux/lockd/lockd.h"]),
        )
        # svc_rqst resolved via diversified includer fs/nfsd/nfssvc.c despite 5 fs/nfs/*.c files sorting earlier
        self.assertEqual(
            (items["svc_rqst"]["kind"], items["svc_rqst"]["header_path"], items["svc_rqst"]["line"],
             items["svc_rqst"]["via"], items["svc_rqst"]["context"], items["svc_rqst"]["chain"]),
            ("struct", "include/linux/sunrpc/svc.h", 110, "context", "fs/nfsd/nfssvc.c",
             ["fs/nfsd/nfssvc.c", "include/linux/sunrpc/svc.h"]),
        )
        # CONFIG_LOCKD_V4 resolved to fs/lockd/Kconfig with kind="config", via="kconfig", alternatives=1
        self.assertEqual(
            (items["CONFIG_LOCKD_V4"]["kind"], items["CONFIG_LOCKD_V4"]["header_path"],
             items["CONFIG_LOCKD_V4"]["line"], items["CONFIG_LOCKD_V4"]["via"],
             items["CONFIG_LOCKD_V4"]["chain"], items["CONFIG_LOCKD_V4"]["alternatives"]),
            ("config", "fs/lockd/Kconfig", 25, "kconfig",
             ["include/linux/lockd/bind.h", "fs/lockd/Kconfig"], 1),
        )


class TestIncluderSpellings(unittest.TestCase):
    """Includer rows whose target is the include as written, and includers with rows of several blobs."""

    def test_include_spellings(self) -> None:
        self.assertEqual(include_spellings("include/linux/lockd/bind.h"), ["linux/lockd/bind.h"])
        self.assertEqual(include_spellings("include/asm-x86_64/types.h"), ["asm-x86_64/types.h", "asm/types.h"])
        self.assertEqual(include_spellings("include/asm-generic/bug.h"), ["asm-generic/bug.h"])
        self.assertEqual(include_spellings("arch/x86/include/asm/types.h"), ["asm/types.h"])
        self.assertEqual(include_spellings("include/uapi/linux/nfs.h"), ["uapi/linux/nfs.h", "linux/nfs.h"])
        self.assertEqual(include_spellings("fs/nfsd/state.h"), [])

    def test_includers_of_written_targets_and_older_blob_rows(self) -> None:
        path = "include/linux/lockd/bind.h"
        tree = {path: "a" * 40, "fs/nfsd/lockd.c": "b" * 40, "fs/other/x.c": "c" * 40, "fs/nfs/client.c": "d" * 40}
        current = {"b" * 40: (11, 1), "c" * 40: (12, 1), "d" * 40: (13, 1)}
        db = make_db("mock", rows={
            "m_file_name": [{"fnid": 1, "fname": path}, {"fnid": 2, "fname": "linux/lockd/bind.h"},
                            {"fnid": 3, "fname": "fs/nfsd/lockd.c"}, {"fnid": 4, "fname": "fs/other/x.c"},
                            {"fnid": 5, "fname": "fs/nfs/client.c"}],
            "m_file_reference": [
                # Kept as written by the parser; the directive resolves to bind.h
                {"ref_id": 1, "source_blob_id": 11, "source_fnid": 3, "target_fnid": 2, "ref_type": 1, "line_no": 18,
                 "details": "<linux/lockd/bind.h>"},
                # Kept as written; resolves to another file for this ARCH
                {"ref_id": 2, "source_blob_id": 12, "source_fnid": 4, "target_fnid": 2, "ref_type": 1, "line_no": 3,
                 "details": "<linux/lockd/bind.h>"},
                # The current blob's row followed by an older revision's row of the same path
                {"ref_id": 3, "source_blob_id": 13, "source_fnid": 5, "target_fnid": 1, "ref_type": 1, "line_no": 29,
                 "details": "<linux/lockd/bind.h>"},
                {"ref_id": 4, "source_blob_id": 7, "source_fnid": 5, "target_fnid": 1, "ref_type": 1, "line_no": 28,
                 "details": "<linux/lockd/bind.h>"},
            ],
        })

        def resolve(_rev, _arch, name, including, quoted):
            self.assertFalse(quoted)
            return path if including == "fs/nfsd/lockd.c" and name == "linux/lockd/bind.h" else "include/other.h"

        with patch.object(isvc.GitTreeResolver, "map_blob_hashes",
                          side_effect=lambda _r, shas: {x: current[x] for x in shas if x in current}), \
                patch.object(isvc.arch_layout_service, "resolve_include", side_effect=resolve) as res, \
                make_db_reader(db) as reader:
            found = ImportsService._includers(reader, path, tree, "v2.6.23", "x86_64")
            exact_only = ImportsService._includers(reader, path, tree)
        self.assertEqual(sorted(found), ["fs/nfs/client.c", "fs/nfsd/lockd.c"])
        self.assertEqual(exact_only, ["fs/nfs/client.c"])
        # Only the current blobs' written rows are resolved
        self.assertEqual(res.call_count, 2)


def make_db_reader(db):
    """`with make_db_reader(db) as reader:` - the webapp read session of `db`."""
    return _ReaderCtx(db)


class _ReaderCtx:
    def __init__(self, db) -> None:
        self._patch = patch_reader(db)

    def __enter__(self):
        self._patch.start()
        self._ctx = isvc.get_reader()
        return self._ctx.__enter__()

    def __exit__(self, *exc):
        try:
            return self._ctx.__exit__(*exc)
        finally:
            self._patch.stop()


class TestIncludeSymbols(unittest.TestCase):
    """symbol_service.get_include_symbols(): symbols of the revision, definitions through the #include chain."""

    XDR = "include/linux/lockd/xdr.h"
    BIND = "include/linux/lockd/bind.h"
    PATHS: ClassVar[list[str]] = [BIND, XDR, "include/linux/fs.h", "include/linux/types.h", "include/asm-x86_64/types.h",
                                  "arch/powerpc/boot/types.h", "security/selinux/include/av_inherit.h", "fs/jffs2/summary.c",
                                  "fs/nfsd/nfsproc.c", "include/linux/sunrpc/svc.h", "fs/nfsd/nfsxdr.c", "include/linux/nfs.h",
                                  "arch/arm/include/asm/nfs.h"]

    def _run(self, refs: list[str], arch: str | None = "x86_64") -> dict:
        from webapp.backend.services import symbol_service as ssvc
        from webapp.backend.services.tree_resolver import ArchScope

        shas = {p: f"{i + 1:040x}" for i, p in enumerate(self.PATHS)}
        blobs = {p: i + 1 for i, p in enumerate(self.PATHS)}
        ids = {shas[p]: (blobs[p], 1) for p in self.PATHS}
        names = ["u32", "file", "inode", "svc_rqst", "gone_sym", "nfs_fh", "__be32"]
        ast = {n: i + 1 for i, n in enumerate(names)}
        # (name, path, type, line, linkage)
        defs = [
            ("u32", "arch/powerpc/boot/types.h", "C_SCtypedef", 8, 0),
            ("u32", "include/asm-x86_64/types.h", "C_SCtypedef", 43, 0),
            ("file", "security/selinux/include/av_inherit.h", "CPPro_define_macro", 2, 5),
            ("file", "include/linux/fs.h", "C_structdecl", 721, 0),
            ("inode", "fs/jffs2/summary.c", "C_structdecl", 120, 0),
            ("inode", "include/linux/fs.h", "C_structdecl", 533, 0),
            ("svc_rqst", "fs/nfsd/nfsproc.c", "C_structdecl", 28, 0),
            ("svc_rqst", "include/linux/sunrpc/svc.h", "C_structdecl", 204, 0),
            ("nfs_fh", "fs/nfsd/nfsxdr.c", "C_structdecl", 5, 0),
            ("nfs_fh", "arch/arm/include/asm/nfs.h", "C_structdecl", 3, 0),
            ("nfs_fh", "include/linux/nfs.h", "C_structdecl", 137, 0),
            ("__be32", "include/linux/types.h", "C_SCtypedef", 184, 0),
        ]
        db = make_db("mock", rows={
            "m_type_descriptor": [{"type_id": v, "name": n} for v, n in D.m_type_descriptor_insert],
            "m_ast": [{"ast_id": i, "name": n, "type_id": 0} for n, i in ast.items()]
                     + [{"ast_id": 100, "name": "<linux/lockd/xdr.h>", "type_id": ASTT.CPPro_include.value}],
            "m_ast_include": [{"ast_id": 100, "fnid": 50}],
            "m_file_name": [{"fnid": 50, "fname": self.XDR}],
            "m_blob_symbol_def": [
                {"def_id": i + 1, "blob_id": blobs[p], "tag_id": 1, "ast_id": ast[n], "name": n,
                 "type_id": ASTT[t].value, "linkage": lk, "line_s": line, "line_e": line}
                for i, (n, p, t, line, lk) in enumerate(defs)
            ],
            "m_blob_symbol_ref": [
                {"ref_id": i + 1, "blob_id": blobs[self.BIND], "tag_id": 1, "ast_id": ast[n], "target_fnid": 0,
                 "role": TYPE, "line": 26 + i, "char_s": 1}
                for i, n in enumerate(refs)
            ],
        })
        # xdr.h -> fs.h -> types.h -> asm-x86_64/types.h; svc.h and nfs.h are outside both closures
        closure = {self.XDR: None, "include/linux/fs.h": self.XDR, "include/linux/types.h": "include/linux/fs.h",
                   "include/asm-x86_64/types.h": "include/linux/types.h"}
        reach = {self.XDR: closure, self.BIND: {self.BIND: None, **{k: (v or self.BIND) for k, v in closure.items()}}}
        via_xdr = [self.BIND, self.XDR]
        all_items = [
            # Context import (another file's #include): never a symbol of this directive
            {"name": "svc_rqst", "kind": "struct", "type_name": "C_structdecl", "header_path": "include/linux/sunrpc/svc.h",
             "line": 204, "via": "context", "chain": ["fs/nfsd/lockd.c", "include/linux/sunrpc/svc.h"]},
            {"name": "u32", "kind": "type", "type_name": "C_SCtypedef", "header_path": "include/asm-x86_64/types.h",
             "line": 43, "via": "include",
             "chain": [*via_xdr, "include/linux/fs.h", "include/linux/types.h", "include/asm-x86_64/types.h"]},
            {"name": "file", "kind": "struct", "type_name": "C_structdecl", "header_path": "include/linux/fs.h",
             "line": 721, "via": "include", "chain": [*via_xdr, "include/linux/fs.h"]},
            {"name": "inode", "kind": "struct", "type_name": "C_structdecl", "header_path": "include/linux/fs.h",
             "line": 533, "via": "include", "chain": [*via_xdr, "include/linux/fs.h"]},
            # Reached through another directive of bind.h
            {"name": "nfs_fh", "kind": "struct", "type_name": "C_structdecl", "header_path": "include/linux/nfs.h",
             "line": 137, "via": "include", "chain": [self.BIND, "include/linux/nfs.h"]},
            # Through a forced header (-include): not this directive
            {"name": "gone_sym", "kind": "type", "type_name": "C_SCtypedef", "header_path": self.XDR,
             "line": 3, "via": "include", "forced": True, "chain": ["include/linux/autoconf.h", self.XDR]},
            {"name": "__be32", "kind": "type", "type_name": "C_SCtypedef", "header_path": "include/linux/types.h",
             "line": 184, "via": "include",
             "chain": [*via_xdr, "include/linux/fs.h", "include/linux/types.h"]},
        ]
        # The Imports panel lists the names bind.h references (m_symbol_ref) at this revision
        imports = {"items": [it for it in all_items if it["name"] in refs]}
        blob_paths = {b: [p] for p, b in blobs.items()}
        with patch_reader(db), \
                patch.object(isvc.git_tree_resolver, "list_tree", return_value=dict(shas)), \
                patch.object(isvc.GitTreeResolver, "map_blob_hashes",
                             side_effect=lambda _r, s_list: {s: ids[s] for s in s_list if s in ids}), \
                patch.object(isvc.arch_layout_service, "layout", return_value=object()), \
                patch.object(isvc.arch_layout_service, "reachable", side_effect=lambda _rev, _a, start: dict(reach.get(start, {}))), \
                patch.object(ssvc.SymbolService, "_scope", return_value=(1, "v2.6.23", blob_paths)), \
                patch.object(ssvc, "definition_arch", return_value=("x86_64", ArchScope.of("x86"), 64)), \
                patch.object(ssvc.imports_service, "get_imports", return_value=imports) as get_imports:
            out = ssvc.SymbolService().get_include_symbols("v2.6.23", 100, file_path=self.BIND, line=14, arch=arch)
        get_imports.assert_called_once_with("v2.6.23", self.BIND, arch or "x86_64")
        return out

    def test_definitions_follow_the_include_chain(self) -> None:
        out = self._run(["u32", "file", "inode", "svc_rqst", "nfs_fh", "gone_sym", "__be32"])
        self.assertEqual((out["header_file"], out["arch"]), (self.XDR, "x86_64"))
        got = {s["name"]: (s["def_file"], s["def_line_s"]) for s in out["symbols"]}
        self.assertEqual(got, {
            # The header's closure for ARCH=x86_64, not arch/powerpc
            "u32": ("include/asm-x86_64/types.h", 43),
            # struct file / inode: tag name space, not the macro `file` or a .c file's struct
            "file": ("include/linux/fs.h", 721),
            "inode": ("include/linux/fs.h", 533),
            "__be32": ("include/linux/types.h", 184),
        })
        # svc_rqst (context import), nfs_fh (another directive) and gone_sym (forced header) are not imported
        # through this directive
        self.assertEqual({s["name"]: s["category"] for s in out["symbols"]}["u32"], "Typedef")
        self.assertTrue(all(s["ast_id"] is None for s in out["symbols"]))

    def test_symbols_of_the_revision(self) -> None:
        # bind.h no longer uses u32 (v2.6.20+ uses __be32): only the names its blob references are listed
        out = self._run(["file", "svc_rqst", "__be32"], arch=None)
        self.assertEqual([s["name"] for s in out["symbols"]], ["file", "__be32"])
        self.assertEqual(out["arch"], "x86_64")

    def test_unindexed_include_node_with_fallback_params(self) -> None:
        from webapp.backend.services import symbol_service as ssvc
        from webapp.backend.services.filesystem_service import filesystem_service

        db = make_db("mock", rows={
            "m_type_descriptor": [{"type_id": v, "name": n} for v, n in D.m_type_descriptor_insert],
        })
        with patch_reader(db), \
                patch.object(ssvc.git_tree_resolver, "list_tree", return_value={}), \
                patch.object(ssvc.SymbolService, "_scope", return_value=(1, "v2.6.23", {})), \
                patch.object(filesystem_service, "resolve_include", return_value={"path": self.XDR}):
            out = ssvc.SymbolService().get_include_symbols("v2.6.23", 0, file_path=self.BIND, line=14,
                                                           header="<linux/lockd/xdr.h>")
        self.assertEqual(out, {"ast_id": 0, "include_text": "<linux/lockd/xdr.h>", "header_file": self.XDR,
                               "symbols": []})

    def test_invalid_arch(self) -> None:
        from webapp.backend.services import symbol_service as ssvc
        with self.assertRaises(HTTPException) as ctx:
            ssvc.SymbolService().get_include_symbols("v2.6.23", 100, arch="x86;drop")
        self.assertEqual(ctx.exception.status_code, 400)


class TestContextWalk(unittest.TestCase):
    """arch_layout_service.reachable_before() and the walk's condition filter."""

    def test_reachable_before_and_assembly_filter(self) -> None:
        from parser.arch_layout import ArchLayout
        from parser.include_graph import IncludeResolver
        from webapp.backend.services.arch_layout_service import (
            ArchLayoutService,
            condition_filter,
        )

        layout = ArchLayout("x86_64", "x86", includes=["include/"])
        files = {p: f"{i:040x}" for i, p in enumerate(["x.c", "include/fs.h", "include/h.h", "include/late.h",
                                                       "include/c_only.h", "e.S"])}
        directives = {
            "x.c": [("fs.h", False, []), ("h.h", False, []), ("late.h", False, [])],
            "include/fs.h": [("h.h", False, [])],  # h.h itself is never part of its context
            "e.S": [("c_only.h", False, ["!defined(__ASSEMBLY__)"]), ("fs.h", False, ["defined(CONFIG_X)"])],
        }
        svc = ArchLayoutService()
        with patch.object(svc, "resolver", return_value=(IncludeResolver(layout, files.__contains__), "f" * 40, files)), \
                patch.object(svc, "_file_directives", side_effect=lambda _sha, path, _f: directives.get(path, [])), \
                patch.object(svc, "_prefetch_directives"):
            self.assertEqual(svc.reachable_before("v1", "x86_64", "x.c", "include/h.h"), {"x.c": None, "include/fs.h": "x.c"})
            walk = svc.reachable("v1", "x86_64", "e.S")
        # An assembly file skips the C-only branch; a CONFIG_ condition stays open
        self.assertEqual(set(walk), {"e.S", "include/fs.h", "include/h.h"})
        self.assertTrue(condition_filter("a.c")(["!defined(__ASSEMBLY__)"]))
        self.assertFalse(condition_filter("a.S")(["!defined(__ASSEMBLY__)"]))
        self.assertTrue(condition_filter("a.S")(["defined(CONFIG_SMP)"]))



class TestPre26Layout(unittest.TestCase):
    """Imports and include-line symbols of a 2.3.35-style revision: its layout comes from `-I$(HPATH)` with
    `TOPDIR := $(shell ... pwd ...)` and the `symlinks:` rule, and it builds i386 only."""

    BIND, NLM, TYPES = "include/linux/lockd/bind.h", "include/linux/lockd/nlm.h", "include/asm-i386/types.h"
    MAKEFILE = (
        "ARCH := $(shell uname -m | sed -e s/i.86/i386/)\n"
        'TOPDIR := $(shell if [ "$$PWD" != "" ]; then echo $$PWD; else pwd; fi)\n'
        "HPATH = $(TOPDIR)/include\nCPPFLAGS := -D__KERNEL__ -I$(HPATH)\ninclude arch/$(ARCH)/Makefile\n"
        "symlinks:\n\trm -f include/asm\n\t( cd include ; ln -sf asm-$(ARCH) asm)\n"
    )

    def setUp(self) -> None:
        from parser.arch_layout import compute_layouts
        from webapp.backend.services.arch_layout_service import ArchLayoutService

        files = {"Makefile": self.MAKEFILE, "arch/i386/Makefile": "", self.BIND: "", self.NLM: "", self.TYPES: ""}
        self.layouts = compute_layouts(files, files.get)
        self.shas = {p: f"{i + 1:040x}" for i, p in enumerate(files)}
        directives = {self.BIND: [("linux/lockd/nlm.h", False, []), ("asm/types.h", False, [])]}
        self.als = ArchLayoutService()
        blobs = {self.BIND: 1, self.NLM: 2, self.TYPES: 3}
        ids = {self.shas[p]: (b, 1) for p, b in blobs.items()}
        names = ["NLM_PROGRAM", "__u32"]
        self.db = make_db("mock", rows={
            "m_type_descriptor": [{"type_id": v, "name": n} for v, n in D.m_type_descriptor_insert],
            "m_ast": [{"ast_id": i + 1, "name": n, "type_id": 0} for i, n in enumerate(names)]
                     + [{"ast_id": 100, "name": "<linux/lockd/nlm.h>", "type_id": ASTT.CPPro_include.value}],
            "m_ast_include": [{"ast_id": 100, "fnid": 50}],
            "m_file_name": [{"fnid": 50, "fname": self.NLM}],
            "m_blob_symbol_def": [
                {"def_id": 1, "blob_id": 2, "tag_id": 1, "ast_id": 1, "name": "NLM_PROGRAM",
                 "type_id": ASTT.CPPro_define_macro.value, "linkage": 5, "line_s": 20, "line_e": 20},
                {"def_id": 2, "blob_id": 3, "tag_id": 1, "ast_id": 2, "name": "__u32",
                 "type_id": ASTT.C_SCtypedef.value, "linkage": 0, "line_s": 9, "line_e": 9},
            ],
            "m_blob_symbol_ref": [
                {"ref_id": 1, "blob_id": 1, "tag_id": 1, "ast_id": 1, "target_fnid": 0, "role": MACRO, "line": 14, "char_s": 1},
                {"ref_id": 2, "blob_id": 1, "tag_id": 1, "ast_id": 2, "target_fnid": 0, "role": TYPE, "line": 14, "char_s": 1},
            ],
        })
        self.blob_paths = {b: [p] for p, b in blobs.items()}
        patches = [
            patch_reader(self.db),
            patch.object(isvc.git_tree_resolver, "list_tree", return_value=dict(self.shas)),
            patch.object(isvc.git_tree_resolver, "resolve_commit_sha", return_value="f" * 40),
            patch.object(isvc.GitTreeResolver, "map_blob_hashes", side_effect=lambda _r, s: {x: ids[x] for x in s if x in ids}),
            patch.object(self.als, "_from_db", return_value=None),
            patch.object(self.als, "_from_git", side_effect=lambda _sha, arch: self.layouts.get(arch)),
            patch.object(self.als, "_file_directives", side_effect=lambda _sha, path, _f: directives.get(path, [])),
            patch.object(self.als, "_prefetch_directives"),
            patch.object(isvc, "arch_layout_service", self.als),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])

    def test_layout_and_walk(self) -> None:
        self.assertEqual(sorted(self.layouts), ["i386"])
        self.assertIsNone(self.als.layout("2.3.35pre2", "x86_64"))
        self.assertEqual(self.als.resolve_include("2.3.35pre2", "i386", "linux/lockd/nlm.h", self.BIND, False), self.NLM)
        self.assertEqual(self.als.reachable("2.3.35pre2", "i386", self.BIND),
                         {self.BIND: None, self.NLM: self.BIND, self.TYPES: self.BIND})

    def test_imports_fall_back_to_i386(self) -> None:
        out = ImportsService().get_imports("2.3.35pre2", self.BIND, "x86_64")
        self.assertEqual((out["arch_used"], out["layout"], out["reachable"]), ("i386", True, 2))
        self.assertEqual({it["name"]: (it["header_path"], it["chain"]) for it in out["items"]}, {
            "NLM_PROGRAM": (self.NLM, [self.BIND, self.NLM]),
            "__u32": (self.TYPES, [self.BIND, self.TYPES]),
        })

    def test_include_line_symbols(self) -> None:
        from webapp.backend.services import symbol_service as ssvc
        from webapp.backend.services.tree_resolver import ArchScope

        with patch.object(ssvc.SymbolService, "_scope", return_value=(1, "2.3.35pre2", self.blob_paths)), \
                patch.object(ssvc, "definition_arch", return_value=("i386", ArchScope.of("i386"), 32)), \
                patch.object(ssvc.imports_service, "_cache", isvc.RepoScopedCache(maxsize=4)):
            out = ssvc.SymbolService().get_include_symbols("2.3.35pre2", 100, file_path=self.BIND, line=12, arch="i386")
        self.assertEqual(out["header_file"], self.NLM)
        self.assertEqual([(s["name"], s["def_file"]) for s in out["symbols"]], [("NLM_PROGRAM", self.NLM)])


class TestDefinitionArch(unittest.TestCase):
    """tree_resolver.definition_arch(): a revision without x86_64 ranks definitions for i386."""

    def test_falls_back_to_i386(self) -> None:
        from fastapi import HTTPException as Missing

        from webapp.backend.services import tree_resolver as tr
        from webapp.backend.services.kconfig_service import KconfigService

        def resolve_arch(_self, version, arch):
            if arch != "i386":
                raise Missing(status_code=404, detail="no such ARCH")
            return {"rev": version, "arch": "i386", "srcarch": "i386", "kernelversion": "2.3.35"}

        with patch.object(KconfigService, "resolve_arch", resolve_arch), \
                patch.object(isvc.arch_layout_service, "layout", return_value=None):
            arch, _scope, bits = tr.definition_arch("2.3.35pre2", None)
        self.assertEqual((arch, bits), ("i386", 32))

    def test_tree_before_arch_resolves_i386(self) -> None:
        from webapp.backend.services import kconfig_service as ksvc

        svc = ksvc.KconfigService()
        with patch.object(svc, "_rev", side_effect=lambda v: v), \
                patch.object(ksvc.git_tree_resolver, "resolve_commit_sha", return_value=None), \
                patch.object(svc, "_read", return_value="VERSION = 1\nPATCHLEVEL = 0\n"), \
                patch.object(svc, "_tree_listing", return_value=[]):
            self.assertEqual(svc.resolve_arch("1.0", "i386")["srcarch"], "i386")
            with self.assertRaises(HTTPException):
                svc.resolve_arch("1.0", "x86_64")


if __name__ == "__main__":
    unittest.main()
