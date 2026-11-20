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
from webapp.backend.services.imports_service import (
    ImportsService,
    def_namespace,
    import_kind,
    include_chain,
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
            res = client.get("/api/fs/imports/v3.0", params={"path": "a.c", "arch": "x86_64"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["path"], "a.c")
            self.assertEqual(client.get("/api/fs/imports/v3.0", params={"path": "../x.c"}).status_code, 400)


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
        svc, mocks = self._svc(own_reach={"h.h": None, "fs.h": "h.h"})
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
        ast = {"nlm_host": 1, "svc_rqst": 2, "CONFIG_LOCKD_V4": 3, "LOCKD_V4": 4}
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


class TestContextWalk(unittest.TestCase):
    """arch_layout_service.reachable_before() and the walk's condition filter."""

    def test_reachable_before_and_assembly_filter(self) -> None:
        from parser.arch_layout import ArchLayout
        from parser.include_graph import IncludeResolver
        from webapp.backend.services.arch_layout_service import ArchLayoutService, condition_filter

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


if __name__ == "__main__":
    unittest.main()
