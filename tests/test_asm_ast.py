"""tests/test_asm_ast.py - Targeted unit tests for native assembly AST & outline extraction."""
from __future__ import annotations

import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.DBLayout import (
    init_db_layout,
    m_bridge_file,
    m_file,
    m_file_name,
)
from core.FileHandler import MasterFile
from core.GreatProcessor import GreatProcessor
from core.SymbolRows import symbol_view_rows
from core.TableHandling import ChangeSet
from core.globalstuff import ASTT, G, SymbolLinkage, SymbolRole
from db_engine import MockDB
from parser.asm_ast import asm_ast_parse
from parser.asm_ast.asm_pure_extractor import extract_asm_outline
from table_engine import get_table_engine


def _symbol_store(view: str) -> dict[int, tuple]:
    """Rows of m_symbol_def / m_symbol_ref over MockDB.store."""
    return dict(enumerate(symbol_view_rows(MockDB.store, view)))


def _stage_file(cs: ChangeSet, gp: GreatProcessor) -> None:
    CS = cs
    CS.store(m_file_name.get_set(None, CS.current_path))
    CS.store(m_file.set(None, getattr(CS, "blob_hash", b"\x00" * 20), 1))
    CS.store(m_bridge_file.set(gp.VID, CS.ref(m_file_name.fnid), CS.ref(m_file.fid)))


class TestAsmAst(unittest.TestCase):
    """Tests for assembly symbol assignments, multiline #define containment, labels, and outlines."""

    def _parse_asm(self, rel_path: str, source: str, extra_files: dict[str, str] | None = None
                   ) -> tuple[dict[str, Any], list[tuple], list[tuple], list[tuple]]:
        mf = None
        try:
            MockDB.store.clear()
            G.DB = MockDB
            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf

            gp = GreatProcessor()
            gp.Version_Name = "v3.0"
            gp.VID = 1
            init_db_layout(gp)
            G.TE = get_table_engine("cached")()
            G.TE.start(gp.Table_Array, MockDB)

            for path, text in {**(extra_files or {}), rel_path: source}.items():
                full_path = os.path.join(temp_dir, path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="latin-1") as f:
                    f.write(text)

            cs = ChangeSet(f"A\t{rel_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = rel_path
            _stage_file(cs, gp)
            asm_ast_parse(cs)
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            ast_rows = list(MockDB.store.get("m_ast", {}).values())
            sym_defs = list(_symbol_store("m_symbol_def").values())
            sym_refs = list(_symbol_store("m_symbol_ref").values())
            ast_by_id = {row[0]: row[1] for row in ast_rows}
            return ast_by_id, ast_rows, sym_defs, sym_refs
        finally:
            if mf:
                mf.clear_all_version()

    def test_symbol_assignments_and_equ_directives(self) -> None:
        """SYM = expr, .equ, .set, .equiv record definitions and RHS DeclRefs without LHS self-refs."""
        src = """/* Symbol assignment constants */
SYM = 0x2C
.equ SYM2, 0x30
.set SYM3, SYM + 4
.equiv SYM4, SYM2 + 8
"""
        ast_by_id, _ast_rows, sym_defs, sym_refs = self._parse_asm("arch/x86/kernel/test_assign.S", src)

        defs_by_name = {row[4]: row for row in sym_defs}
        for sym in ("SYM", "SYM2", "SYM3", "SYM4"):
            self.assertIn(sym, defs_by_name, f"Missing definition for {sym}")
            self.assertEqual(defs_by_name[sym][5], int(ASTT.ASM_Label))
            self.assertEqual(defs_by_name[sym][6], int(SymbolLinkage.InternalStaticDef))

        # References: RHS uses of SYM and SYM2 are DeclRefs; LHS names must NOT emit MacroExpansion/DeclRef
        refs = [(ast_by_id.get(row[3], row[3]), row[5]) for row in sym_refs]
        self.assertIn(("SYM", int(SymbolRole.DeclRef)), refs)
        self.assertIn(("SYM2", int(SymbolRole.DeclRef)), refs)
        self.assertNotIn(("SYM", int(SymbolRole.MacroExpansion)), refs)
        self.assertNotIn(("SYM2", int(SymbolRole.MacroExpansion)), refs)
        self.assertNotIn(("SYM3", int(SymbolRole.MacroExpansion)), refs)
        self.assertNotIn(("SYM3", int(SymbolRole.DeclRef)), refs)
        self.assertNotIn(("SYM4", int(SymbolRole.MacroExpansion)), refs)
        self.assertNotIn(("SYM4", int(SymbolRole.DeclRef)), refs)

    def test_multiline_define_does_not_leak_mnemonics_or_labels(self) -> None:
        """Multiline #define with labels, /**/ comments, and macro calls absorbs all body lines."""
        src = """#define RESTORE_ALL(name) \\
    RESTORE_REGS \\
    addl $4, %esp; \\
1:  popl %ds; \\
    movl %eax, %ebx; \\
    call smp_/**/name; \\
    pushl %ecx; \\
    sti; \\
    iret

ENTRY(real_func)
    nop
END(real_func)
"""
        _ast_by_id, ast_rows, sym_defs, _sym_refs = self._parse_asm("arch/x86/kernel/test_define.S", src)

        insn_names = {row[1] for row in ast_rows if row[2] == int(ASTT.ASM_Instruction)}
        label_names = {row[1] for row in ast_rows if row[2] == int(ASTT.ASM_Label)}
        leaked = {"addl", "popl", "movl", "iret", "sti", "pushl", "call", "RESTORE_REGS"}
        self.assertTrue(
            leaked.isdisjoint(insn_names),
            f"Leaked mnemonics from #define body into ASM_Instruction: {leaked & insn_names}",
        )
        self.assertNotIn("1", label_names)
        self.assertEqual(label_names, {"real_func"})

        def_names = {row[4] for row in sym_defs}
        self.assertIn("RESTORE_ALL", def_names)
        self.assertIn("real_func", def_names)

    def test_inner_labels_gas_macro_labels_and_uppercase_l_calls(self) -> None:
        """SYM_INNER_LABEL, SYM_DATA*, GAS macro \\:/\\lbl: suppression, and call Lookup_fn."""
        src = """.macro MY_MAC
\\:
\\lbl:
    nop
.endm

SYM_FUNC_START(outer_fn)
    call Lookup_fn
SYM_INNER_LABEL(inner_local, SYM_L_LOCAL)
    nop
SYM_INNER_LABEL(inner_global, SYM_L_GLOBAL)
    ret
SYM_FUNC_END(outer_fn)

SYM_DATA(global_data, .quad 0)
SYM_DATA_LOCAL(local_data, .quad 0)
"""
        ast_by_id, ast_rows, sym_defs, sym_refs = self._parse_asm("arch/x86/entry/test_inner.S", src)

        label_names = {row[1] for row in ast_rows if row[2] == int(ASTT.ASM_Label)}
        self.assertNotIn("\\", label_names)
        self.assertNotIn("lbl", label_names)

        defs_by_name = {row[4]: row for row in sym_defs}
        self.assertNotIn("\\", defs_by_name)
        self.assertNotIn("lbl", defs_by_name)
        self.assertIn("outer_fn", defs_by_name)
        self.assertEqual(defs_by_name["outer_fn"][7], 7)
        self.assertEqual(defs_by_name["outer_fn"][8], 13)
        self.assertIn("inner_local", defs_by_name)
        self.assertEqual(defs_by_name["inner_local"][6], int(SymbolLinkage.InternalStaticDef))
        self.assertIn("inner_global", defs_by_name)
        self.assertEqual(defs_by_name["inner_global"][6], int(SymbolLinkage.ExternalDef))
        self.assertIn("global_data", defs_by_name)
        self.assertEqual(defs_by_name["global_data"][6], int(SymbolLinkage.ExternalDef))
        self.assertIn("local_data", defs_by_name)
        self.assertEqual(defs_by_name["local_data"][6], int(SymbolLinkage.InternalStaticDef))

        call_refs = [ast_by_id.get(row[3], row[3]) for row in sym_refs if row[5] == int(SymbolRole.Call)]
        self.assertIn("Lookup_fn", call_refs)

    def test_extract_asm_outline_constants_and_multiline_define(self) -> None:
        """extract_asm_outline extracts SYM = expr / .equ / .set and skips multiline #define bodies."""
        asm_bytes = b"""#define FAKE_ENTRY \\
    ENTRY(fake) \\
    1: \\
    fake_lbl: \\
    FAKE_CONST = 0x10

SYM = 0x2C
.equ SYM2, 0x30
.set SYM3, SYM + 4
.equiv SYM4, 0x40

SYM_FUNC_START(real_fn)
    nop
SYM_FUNC_END(real_fn)
"""
        outline = extract_asm_outline(asm_bytes, path="arch/x86/kernel/test.S")
        by_name = {item["name"]: item for item in outline}

        for leaked in ("fake", "fake_lbl", "FAKE_CONST"):
            self.assertNotIn(leaked, by_name)

        for sym, line_no in (("SYM", 7), ("SYM2", 8), ("SYM3", 9), ("SYM4", 10)):
            self.assertIn(sym, by_name)
            self.assertEqual(by_name[sym]["kind"], "constant")
            self.assertEqual(by_name[sym]["type_name"], "ASM_Label")
            self.assertEqual(by_name[sym]["line_s"], line_no)
            self.assertEqual(by_name[sym]["line_e"], line_no)
            self.assertEqual(by_name[sym]["linkage"], int(SymbolLinkage.InternalStaticDef))

        self.assertIn("real_fn", by_name)
        self.assertEqual(by_name["real_fn"]["kind"], "function")
        self.assertEqual(by_name["real_fn"]["line_s"], 12)
        self.assertEqual(by_name["real_fn"]["line_e"], 14)

    def test_include_targets_are_canonical_repo_paths(self) -> None:
        source = (
            "#include <linux/linkage.h>\n"
            "#ifdef CONFIG_NEVER_SET\n"
            "#include <asm/never.h>\n"
            "#endif\n"
            "#include \"local.h\"\n"
            "#include <generated/asm-offsets.h>\n"
            "ENTRY(f)\n\tret\n"
        )
        extra = {"include/linux/linkage.h": "#define ENTRY(x) x:\n", "include/asm/never.h": "\n",
                 "arch/x86/kernel/local.h": "\n"}
        self._parse_asm("arch/x86/kernel/entry.S", source, extra)
        names = {row[0]: row[1] for row in MockDB.store.get("m_file_name", {}).values()}
        inc = {row[0]: names[row[1]] for row in MockDB.store.get("m_ast_include", {}).values()}
        asts = {row[0]: row for row in MockDB.store.get("m_ast", {}).values()}
        got = {asts[a][1]: target for a, target in inc.items() if asts[a][2] == int(ASTT.CPPro_include)}
        self.assertEqual(got, {
            "<linux/linkage.h>": "include/linux/linkage.h",
            "<asm/never.h>": "include/asm/never.h",  # inactive branch
            '"local.h"': "arch/x86/kernel/local.h",
            "<generated/asm-offsets.h>": "generated/asm-offsets.h",  # build-generated, not in the tree
        })
        # Directive text and target only: no m_ast_container row on include nodes
        self.assertFalse([r for r in MockDB.store.get("m_ast_container", {}).values() if r[0] in inc])


if __name__ == "__main__":
    unittest.main()
