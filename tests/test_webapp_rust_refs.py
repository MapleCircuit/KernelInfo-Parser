"""tests/test_webapp_rust_refs.py - Rust uses of C symbols in xref and the callgraph (read session double).

A Rust use through bindgen (`bindings::kfree(p)`) references the `Rust_BindingRef` AST named by the symbol
(parser/rust_ast/RUST_API.md §7): `get_symbol_xref()` and `get_function_callgraph()` add that AST to their
reference targets, so Rust callers are listed next to the C ones (webapp/API.md §4.3, §4.7).
"""
from __future__ import annotations

import os
import sys
import unittest
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.globalstuff import ASTT, SymbolLinkage
from webapp.backend.services import callgraph_service as cgs
from webapp.backend.services import symbol_service as ss

C_DEF_AST, BINDING_AST, RUST_FN_AST = 42, 77, 90
BLOB_C, BLOB_RS = 1, 2
PATHS = {BLOB_C: ["mm/slab_common.c"], BLOB_RS: ["rust/kernel/alloc.rs"]}


C_DEF = {"ast_id": C_DEF_AST, "type_id": int(ASTT.C_functionproto), "type_name": "C_functionproto",
         "line_s": 10, "line_e": 20, "blob_id": BLOB_C, "linkage": 0, "ast_name": "kfree"}


class _Reader:
    """A read session double answering by the source and conditions of each spec read and stubbing the symbol read
    intents (their reference/caller joins would need a seeded AST and tag graph); `defs` are the definitions `kfree` has."""

    def __init__(self) -> None:
        self.defs: list[dict[str, Any]] = [dict(C_DEF)]
        self.ref_targets: list[list[int]] = []

    def __enter__(self) -> "_Reader":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def select_rows(self, source: Any, columns: Any, where: Any = None, **kw: Any) -> list[dict[str, Any]]:
        if source == "m_ast":
            hit = where == {"name": "kfree", "type_id": int(ASTT.Rust_BindingRef)}
            return [{"ast_id": BINDING_AST}] if hit else []
        if source == ("m_symbol_def", "s") and where == {"s.name": "kfree"}:
            return [dict(r) for r in self.defs]
        return []  # callees of the function: none

    def symbol_refs_of_targets(self, ast_ids: Any, *, role: int | None = None, with_caller: bool = False,
                               limit: int | None = None) -> list[dict[str, Any]]:
        self.ref_targets.append(list(ast_ids))
        # Only the binding AST has a reference (the Rust call)
        if BINDING_AST not in ast_ids or role not in (None, 3):
            return []
        return [{"role": 3, "line": 5, "char_s": 9, "fid": BLOB_RS, "tag_id": 3, "ast_id": BINDING_AST, "caller": "free"}]

    def symbol_def_blobs_of_targets(self, ast_ids: Any) -> set[tuple[int, int]]:
        return set()


class TestRustReferencesInWebapp(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = _Reader()
        arch = ("x86_64", None, 64)
        self.patches = [
            patch.object(ss, "get_reader", lambda: self.reader),
            patch.object(cgs, "get_reader", lambda: self.reader),
            patch.object(ss.SymbolService, "_scope", staticmethod(lambda cur, v: (1, "v7.3", PATHS))),
            patch.object(ss, "definition_arch", lambda v, a: arch),
            patch.object(cgs, "definition_arch", lambda v, a: arch),
            patch.object(ss, "best_path", lambda paths, *a: paths[0] if paths else None),
            patch.object(cgs, "best_path", lambda paths, *a: paths[0] if paths else None),
            patch.object(ss, "definition_rank", lambda *a: 0),
            patch.object(cgs, "definition_rank", lambda *a: 0),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()

    def test_binding_ref_asts(self) -> None:
        self.assertEqual(ss.binding_ref_asts(self.reader, "kfree"), [BINDING_AST])
        self.assertEqual(ss.binding_ref_asts(self.reader, "nothing"), [])

    def test_xref_lists_rust_uses(self) -> None:
        res = ss.SymbolService().get_symbol_xref("v7.3", "kfree")
        self.assertEqual(res["definition"]["file"], "mm/slab_common.c")
        self.assertEqual(res["ast_id"], C_DEF_AST)  # the definition stays the primary AST
        self.assertEqual(
            [(r["file"], r["caller"], r["type"]) for r in res["references"]],
            [("rust/kernel/alloc.rs", "free", "Call")],
        )

    def test_callgraph_lists_rust_callers(self) -> None:
        res = cgs.CallgraphService().get_function_callgraph("v7.3", "kfree")
        self.assertEqual([(c["name"], c["file_path"]) for c in res["callers"]], [("free", "rust/kernel/alloc.rs")])

    def test_callgraph_prefers_c_definition_over_rust_method(self) -> None:
        rows = [
            {"ast_id": RUST_FN_AST, "blob_id": BLOB_RS, "line_s": 1, "line_e": 3, "type_id": int(ASTT.Rust_Fn),
             "linkage": int(SymbolLinkage.ExternalDef), "ast_name": "kfree"},
            {"ast_id": C_DEF_AST, "blob_id": BLOB_C, "line_s": 10, "line_e": 20, "type_id": int(ASTT.C_functionproto),
             "linkage": int(SymbolLinkage.ExternalDef), "ast_name": "kfree"},
        ]
        self.reader.defs = rows
        res = cgs.CallgraphService().get_function_callgraph("v7.3", "kfree")
        self.assertEqual(res["file_path"], "mm/slab_common.c")


if __name__ == "__main__":
    unittest.main()
