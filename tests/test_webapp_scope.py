"""tests/test_webapp_scope.py - Local scope resolution of C variables, parameters and struct fields in the webapp
(`webapp/backend/services/scope_service.py`, webapp/API.md section 2.21, webapp/API_ENDPOINTS.md section 4.3).

Driven by Linux v3.0 `drivers/pci/syscall.c` (`tests/fixtures/c_scope/`): the blob reads and the ranked symbol
definitions are doubles (no git, no database), the scope model is the real `parser/c_ast/c_scope.py` one.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.routers import symbols as router
from webapp.backend.services import scope_service as sc
from webapp.backend.services import source_index

FIXTURES = Path(__file__).parent / "fixtures" / "c_scope"
SYSCALL = "drivers/pci/syscall.c"
PCI_H = "include/linux/pci.h"
SEARCH_C = "drivers/pci/search.c"
TYPES_H = "include/asm-generic/int-ll64.h"
CHAIN_C = "drivers/pci/chain.c"

SEARCH_SRC = "\n" * 153 + ("struct pci_dev * pci_get_bus_and_slot(unsigned int bus, unsigned int devfn)\n"
                           "{\n\treturn NULL;\n}\n")
TYPES_SRC = "typedef unsigned char u8;\ntypedef unsigned short u16;\ntypedef unsigned int u32;\n"
CHAIN_SRC = """\
#include <linux/pci.h>
#include "local.h"
extern int pci_flag;

static void reset(struct pci_dev *dev, pci_bus_t *b, anon_t s)
{
	dev->bus->number = 0;
	b->primary = dev->bus->number;
	s.a = pci_flag;
	unknown_macro(dev);
}
"""
LOCAL_H = """\
typedef struct pci_bus pci_bus_t;
typedef struct {
	int a;
	long b;
} anon_t;
"""


def _def(file: str, line_s: int, line_e: int, type_name: str, linkage: int = 0) -> dict[str, Any]:
    return {"file": file, "line_s": line_s, "line_e": line_e, "type_name": type_name, "linkage": linkage}


DEFS: dict[str, list[dict[str, Any]]] = {
    # a same-named function: never the type definition
    "pci_dev": [_def("arch/x86/pci/fake.c", 10, 20, "C_functionproto"), _def(PCI_H, 2, 110, "C_structdecl")],
    "pci_bus": [_def(PCI_H, 50, 50, "C_structdecl", 2), _def(PCI_H, 112, 142, "C_structdecl")],
    "pci_get_bus_and_slot": [_def(SEARCH_C, 154, 157, "C_functionproto")],
    "u8": [_def(TYPES_H, 1, 1, "C_SCtypedef")],
    "u16": [_def(TYPES_H, 2, 2, "C_SCtypedef")],
    "get_user": [_def("arch/x86/include/asm/uaccess.h", 160, 170, "CPPro_define")],
    "pci_bus_t": [_def("drivers/pci/local.h", 1, 1, "C_SCtypedef")],
    "anon_t": [_def("drivers/pci/local.h", 5, 5, "C_SCtypedef")],
}


def _col(src: str, line: int, token: str, nth: int = 0) -> int:
    text = src.split("\n")[line - 1]
    at = -1
    for _ in range(nth + 1):
        at = text.index(token, at + 1)
    return at + 1


class _ScopeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = {
            SYSCALL: (FIXTURES / "pci_syscall_v3.0.c").read_bytes().decode("latin-1"),
            PCI_H: (FIXTURES / "pci_h_v3.0_excerpt.h").read_bytes().decode("latin-1"),
            SEARCH_C: SEARCH_SRC, TYPES_H: TYPES_SRC, CHAIN_C: CHAIN_SRC, "drivers/pci/local.h": LOCAL_H,
        }
        self.def_calls: list[tuple[str, Any]] = []

        def definitions(version: str, name: str, arch: str, from_file: str, kind: Any = None) -> list[dict[str, Any]]:
            self.def_calls.append((name, kind))
            return [dict(d) for d in DEFS.get(name, [])]

        self.svc = sc.ScopeService(definitions_fn=definitions, read_source=lambda v, p: self.sources.get(p))
        self.loaded = patch.object(source_index, "loaded_revision", return_value=None)
        self.loaded.start()
        self.addCleanup(self.loaded.stop)

    def at(self, path: str, line: int, token: str, nth: int = 0) -> dict[str, Any]:
        return self.svc.resolve("v3.0", path, line, _col(self.sources[path], line, token, nth), token, "x86_64")


class TestLocalVariables(_ScopeCase):
    def test_dev_local_with_inferred_type(self) -> None:
        info = self.at(SYSCALL, 98, "dev")
        self.assertTrue(info["found"])
        self.assertEqual((info["kind"], info["name"], info["type"]), ("local", "dev", "struct pci_dev *"))
        self.assertEqual(info["type_ref"], {"kind": "struct", "name": "pci_dev", "pointer": 1, "array": False})
        self.assertEqual(info["decl"], {"line": 89, "col": 18})
        self.assertEqual([(u["line"], u["access"]) for u in info["uses"]],
                         [(89, "decl"), (98, "w"), (99, "r"), (107, "r"), (116, "r"), (125, "r"), (134, "r")])
        self.assertEqual(info["counts"], {"decl": 1, "r": 5, "w": 1, "rw": 0, "addr": 0})
        self.assertEqual(info["access"], "w")
        self.assertEqual(info["function"], {"name": "sys_pciconfig_write", "line_s": 86, "line_e": 136})
        self.assertEqual(info["inferred"], [{"callee": "pci_get_bus_and_slot", "line": 98,
                                             "return_type": "struct pci_dev *", "agrees": True,
                                             "file": SEARCH_C, "def_line": 154}])
        self.assertEqual(info["effective_type"], "struct pci_dev *")
        self.assertEqual(info["last_assign"], {"line": 98, "col": 2})
        # the struct, not the same-named function ranked before it
        self.assertEqual((info["type_def"]["file"], info["type_def"]["line"], info["type_def"]["kind"]),
                         (PCI_H, 2, "struct"))
        self.assertIn(("pci_dev", "type"), self.def_calls)
        self.assertIsNone(info["member"])

    def test_dev_uses_stay_in_the_write_function(self) -> None:
        info = self.at(SYSCALL, 134, "dev")
        self.assertFalse([u for u in info["uses"] if 16 <= u["line"] <= 84])
        self.assertEqual(info["last_assign"], {"line": 98, "col": 2})
        read_fn = self.at(SYSCALL, 30, "dev")
        self.assertEqual(read_fn["decl"]["line"], 19)
        self.assertFalse([u for u in read_fn["uses"] if u["line"] > 85])

    def test_byte_and_word(self) -> None:
        byte = self.at(SYSCALL, 104, "byte")
        self.assertEqual((byte["type"], byte["access"]), ("u8", "w"), "get_user writes its first argument")
        self.assertEqual([u["line"] for u in byte["uses"]], [90, 104, 107])
        self.assertEqual(byte["type_ref"]["kind"], "typedef")
        self.assertEqual((byte["type_def"]["file"], byte["type_def"]["line"], byte["type_def"]["kind"]),
                         (TYPES_H, 1, "typedef"))
        self.assertEqual(byte["inferred"], [], "get_user is a macro: nothing to infer")
        word = self.at(SYSCALL, 113, "word")
        self.assertEqual((word["type"], word["decl"]["line"]), ("u16", 91))

    def test_err_assigned_from_macro_is_not_inferred(self) -> None:
        err = self.at(SYSCALL, 104, "err")
        self.assertEqual(err["type"], "int")
        self.assertEqual(err["inferred"], [])
        self.assertEqual(err["last_assign"], {"line": 104, "col": 3})

    def test_params(self) -> None:
        bus = self.at(SYSCALL, 98, "bus")
        self.assertEqual((bus["kind"], bus["type"], bus["type_def"]), ("param", "unsigned long", None))
        self.assertEqual(bus["type_ref"]["kind"], "builtin")
        buf = self.at(SYSCALL, 104, "buf")
        self.assertEqual((buf["kind"], buf["type"]), ("param", "void __user *"))
        self.assertEqual(buf["decl"]["line"], 87)

    def test_inexact_column_and_unknown_names(self) -> None:
        exact = self.at(SYSCALL, 98, "dev")
        loose = self.svc.resolve("v3.0", SYSCALL, 98, 0, "dev", "x86_64")
        self.assertEqual(loose["decl"], exact["decl"])
        self.assertEqual(self.at(SYSCALL, 95, "capable"), {"found": False})
        self.assertEqual(self.svc.resolve("v3.0", "Makefile", 1, 1, "obj", "x86_64"), {"found": False})
        self.assertEqual(self.svc.resolve("v3.0", "../etc/passwd.c", 1, 1, "x", "x86_64"), {"found": False})

    def test_extern_falls_through(self) -> None:
        self.assertEqual(self.at(CHAIN_C, 9, "pci_flag"), {"found": False})


class TestMembers(_ScopeCase):
    def test_member_chain_write(self) -> None:
        number = self.at(CHAIN_C, 7, "number")
        self.assertEqual((number["kind"], number["name"], number["access"]), ("field", "number", "w"))
        member = number["member"]
        self.assertEqual((member["base"], member["base_kind"], member["base_type"], member["path"], member["op"]),
                         ("dev", "param", "struct pci_dev *", ["bus", "number"], "->"))
        self.assertEqual(member["struct"], {"kind": "struct", "name": "pci_bus", "file": PCI_H, "line": 112})
        self.assertEqual(member["field_def"]["line"], 126)
        self.assertEqual(member["field_def"]["type"], "unsigned char")
        self.assertEqual(number["type"], "unsigned char")
        self.assertEqual([(u["line"], u["access"]) for u in number["uses"]], [(7, "w"), (8, "r")])
        self.assertEqual(number["last_assign"], {"line": 7, "col": _col(CHAIN_SRC, 7, "number")})
        self.assertIsNone(number["decl"])

    def test_member_step_read(self) -> None:
        bus = self.at(CHAIN_C, 7, "bus")
        self.assertEqual((bus["kind"], bus["access"], bus["type"]), ("field", "r", "struct pci_bus *"))
        self.assertEqual(bus["member"]["struct"]["name"], "pci_dev")
        self.assertEqual((bus["member"]["field_def"]["line"], bus["member"]["field_def"]["col"]), (4, 18))
        self.assertEqual((bus["type_def"]["name"], bus["type_def"]["line"]), ("pci_bus", 112),
                         "a prototype row of the struct sorts after its body")

    def test_typedefs_to_structs(self) -> None:
        primary = self.at(CHAIN_C, 8, "primary")
        self.assertEqual(primary["member"]["struct"]["name"], "pci_bus", "typedef struct pci_bus pci_bus_t")
        self.assertEqual(primary["member"]["field_def"]["line"], 127)
        a = self.at(CHAIN_C, 9, "a")
        self.assertEqual(a["member"]["op"], ".")
        self.assertEqual((a["member"]["struct"]["kind"], a["member"]["struct"]["name"]), ("typedef", "anon_t"))
        self.assertEqual((a["member"]["field_def"]["line"], a["type"]), (3, "int"))


class TestHelpers(unittest.TestCase):
    def test_normalize_type(self) -> None:
        n = sc.normalize_type
        self.assertEqual(n("struct pci_dev *"), n("struct  pci_dev*"))
        self.assertEqual(n("const char *"), n("char const *"))
        self.assertEqual(n("void __user *"), n("void *"))
        self.assertEqual(n("unsigned long"), n("long unsigned"))
        self.assertNotEqual(n("char *const"), n("const char *"))
        self.assertNotEqual(n("int *"), n("int"))

    def test_declaration_text(self) -> None:
        d = sc.declaration_text
        self.assertEqual(d("struct pci_dev *", "dev"), "struct pci_dev *dev")
        self.assertEqual(d("u8", "byte"), "u8 byte")
        self.assertEqual(d("int [4]", "c"), "int c[4]")
        self.assertEqual(d("void (*)(int)", "fn"), "void (*fn)(int)")
        self.assertEqual(d(None, "x"), "x")


class TestRoutes(_ScopeCase):
    def setUp(self) -> None:
        super().setUp()
        boom = MagicMock(side_effect=AssertionError("global lookup for a local"))
        self.patches = [patch.object(sc, "scope_service", self.svc),
                        patch.object(router.symbol_service, "get_symbol_detail", boom),
                        patch.object(router.symbol_service, "get_symbol_xref", boom)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.boom = boom

    def test_scope_endpoint(self) -> None:
        info = router.get_symbol_scope("v3.0", SYSCALL, 98, 2, "dev", "x86_64")
        self.assertEqual((info["kind"], info["decl"]["line"]), ("local", 89))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            router.get_symbol_scope("v3.0", "../x.c", 1, 1, "dev", "x86_64")
        with self.assertRaises(HTTPException):
            router.get_symbol_scope("--output=x", SYSCALL, 1, 1, "dev", "x86_64")

    def test_hover_detail_xref_with_position(self) -> None:
        from webapp.backend.services import hover_service as hs
        with patch.object(hs.hover_service, "hover", MagicMock(side_effect=AssertionError("global hover"))):
            hover = router.get_symbol_hover("v3.0", "dev", "x86_64", SYSCALL, 98, 2)
        self.assertEqual((hover["kind"], hover["signature"], hover["file"], hover["line"], hover["definitions"]),
                         ("local", "struct pci_dev *dev", SYSCALL, 89, 1))
        self.assertIsNone(hover["doc"])
        self.assertEqual(hover["scope"]["name"], "dev")

        detail = router.get_symbol_detail("v3.0", "dev", "x86_64", SYSCALL, 98, 2)
        self.assertEqual((detail["file_path"], detail["line_s"]), (SYSCALL, 89))
        self.assertEqual(detail["definition"], {"file": SYSCALL, "line_s": 89, "line_e": 89,
                                                "type_name": "local variable"})
        self.assertEqual(detail["definitions"], [detail["definition"]])

        xref = router.get_symbol_xref("v3.0", "dev", None, "x86_64", SYSCALL, 98, 2)
        self.assertEqual(xref["definition"]["line_s"], 89)
        self.assertEqual([(r["line"], r["type"], r["access"]) for r in xref["references"]][:3],
                         [(89, "Decl", "decl"), (98, "Write", "w"), (99, "Read", "r")])
        self.assertEqual({r["caller"] for r in xref["references"]}, {"sys_pciconfig_write"})
        self.assertEqual(len(router.get_symbol_xref("v3.0", "dev", 2, "x86_64", SYSCALL, 98, 2)["references"]), 2)

        field = router.get_symbol_hover("v3.0", "number", "x86_64", CHAIN_C, 7, _col(CHAIN_SRC, 7, "number"))
        self.assertEqual((field["kind"], field["signature"], field["file"], field["line"]),
                         ("field", "unsigned char number", PCI_H, 126))
        fdetail = router.get_symbol_detail("v3.0", "bus", "x86_64", CHAIN_C, 7, _col(CHAIN_SRC, 7, "bus"))
        self.assertEqual((fdetail["file_path"], fdetail["line"], fdetail["definition"]["type_name"]),
                         (PCI_H, 4, "struct field"))
        self.boom.assert_not_called()

    def test_without_position_nothing_changes(self) -> None:
        self.boom.side_effect = None
        self.boom.return_value = {"global": True}
        self.assertEqual(router.get_symbol_detail("v3.0", "dev", "x86_64", SYSCALL), {"global": True})
        self.boom.assert_called_with("v3.0", "dev", "x86_64", SYSCALL, kind=None)
        self.assertEqual(router.get_symbol_xref("v3.0", "dev", None, "x86_64"), {"global": True})
        self.boom.assert_called_with("v3.0", "dev", None, "x86_64", name_fallback=True)
        # kind=type always asks the global ranking
        self.assertEqual(router.get_symbol_detail("v3.0", "pci_dev", "x86_64", SYSCALL, 89, 9, "type"),
                         {"global": True})
        self.boom.assert_called_with("v3.0", "pci_dev", "x86_64", SYSCALL, kind="type")

    def test_non_variable_in_function_skips_name_fallback(self) -> None:
        self.boom.side_effect = None
        self.boom.return_value = {"global": True}
        router.get_symbol_xref("v3.0", "capable", None, "x86_64", SYSCALL, 95, _col(self.sources[SYSCALL], 95, "capable"))
        self.boom.assert_called_with("v3.0", "capable", None, "x86_64", name_fallback=False)
        router.get_symbol_xref("v3.0", "pci_dev", None, "x86_64", SYSCALL, 10, 1)
        self.boom.assert_called_with("v3.0", "pci_dev", None, "x86_64", name_fallback=True)
        # extern declarations resolve globally
        router.get_symbol_detail("v3.0", "pci_flag", "x86_64", CHAIN_C, 9, _col(CHAIN_SRC, 9, "pci_flag"))
        self.boom.assert_called_with("v3.0", "pci_flag", "x86_64", CHAIN_C, kind=None)


class TestTypeKindRanking(unittest.TestCase):
    def test_kind_type_ranks_the_struct_first(self) -> None:
        from webapp.backend.services import symbol_service as ss
        rows = [
            {"def_id": 1, "ast_id": 1, "name": "pci_dev", "type_id": 1, "type_name": "C_functionproto", "fid": 10,
             "line_s": 5, "line_e": 9, "tag_id": 1, "linkage": 0},
            {"def_id": 2, "ast_id": 2, "name": "pci_dev", "type_id": 2, "type_name": "C_structdecl", "fid": 20,
             "line_s": 227, "line_e": 340, "tag_id": 2, "linkage": 0},
        ]
        reader = MagicMock()
        reader.select_rows.side_effect = lambda *a, **k: [dict(r) for r in rows] if a[0] == ("m_symbol_def", "s") else []
        ctx = MagicMock()
        ctx.__enter__.return_value = reader
        blob_paths = {10: ["arch/x86/pci/fake.c"], 20: ["include/linux/pci.h"]}
        with patch.object(ss, "get_reader", return_value=ctx), \
             patch.object(ss.SymbolService, "_scope", return_value=(1, "v3.0", blob_paths)), \
             patch.object(ss, "definition_arch", return_value=("x86_64", "x86", 64)):
            plain = ss.SymbolService().get_symbol_detail("v3.0", "pci_dev", "x86_64")
            typed = ss.SymbolService().get_symbol_detail("v3.0", "pci_dev", "x86_64", kind="type")
            ranked = ss.SymbolService().ranked_definitions("v3.0", "pci_dev", "x86_64", None, "type")
        self.assertEqual(plain["file_path"], "arch/x86/pci/fake.c")
        self.assertEqual((typed["file_path"], typed["line_s"], typed["type_name"]),
                         ("include/linux/pci.h", 227, "C_structdecl"))
        self.assertEqual([r["type_name"] for r in ranked], ["C_structdecl", "C_functionproto"])


if __name__ == "__main__":
    unittest.main()
