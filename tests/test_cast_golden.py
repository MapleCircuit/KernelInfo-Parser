"""tests/test_cast_golden.py - Permanent Sequential Golden-Diff Regression Test Suite.

Validates that C-AST and ASM-AST parsing output (staged operations and MockDB committed rows)
remains 100.00% byte-identical to the baseline across 15 critical kernel files in both
full-tree mode (with kernel headers) and isolated mode (single-file sandbox).

Fixtures: tests/fixtures/cast_golden.json
Regeneration: REGEN_GOLDEN=1 python3 -m unittest tests.test_cast_golden
"""
from __future__ import annotations

import os
import sys
import json
import hashlib
import unittest
import subprocess
import shutil
from pathlib import Path
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Rule 9: Ensure core modules precede parser.c_ast to prevent circular imports
import core  # noqa: F401
from core.globalstuff import G
from core.GreatProcessor import GreatProcessor
from core.FileHandler import MasterFile
from core.TableHandling import ChangeSet
from core.DBLayout import init_db_layout
from core.SymbolRows import symbol_view_rows
from table_engine import get_table_engine
from db_engine import MockDB
from parser.c_ast.cursor_tree import clear_tu_caches
from tests.test_c_ast import default_processing
from core.config import get_default_repo_path

LINUX_REPO = str(get_default_repo_path())

FIXTURE_PATH = os.path.join(ROOT, "tests", "fixtures", "cast_golden.json")
# Physical symbol tables: hashed through their m_symbol_def / m_symbol_ref view rows instead (core/SymbolRows.py)
_PHYSICAL_SYMBOL_TABLES = frozenset({"m_blob_symbol_def", "m_blob_symbol_ref", "m_tag_def", "m_tag_ref", "m_blob_symbol", "m_symbol_own"})
GOLDEN_CACHE = "/dev/shm/kip_v30_golden_cache"


def ensure_golden_tree() -> str:
    """Ensure Linux v3.0 archive is extracted into /dev/shm for full-mode header resolution."""
    if not os.path.exists(os.path.join(GOLDEN_CACHE, "Makefile")):
        os.makedirs(GOLDEN_CACHE, exist_ok=True)
        p_arch = subprocess.Popen(
            ["git", "-C", LINUX_REPO, "archive", "v3.0"],
            stdout=subprocess.PIPE,
        )
        subprocess.run(["tar", "-x", "-C", GOLDEN_CACHE], stdin=p_arch.stdout, check=True)
        p_arch.wait()
        asm_path = Path(f"{GOLDEN_CACHE}/include/asm")
        if not asm_path.exists():
            os.symlink("asm-generic", asm_path)
        uapi_asm = Path(f"{GOLDEN_CACHE}/include/uapi/asm")
        if not uapi_asm.exists() and os.path.exists(f"{GOLDEN_CACHE}/include/uapi"):
            os.symlink("asm-generic", uapi_asm)
    return GOLDEN_CACHE


def execute_parse(mode: str, file_path: str, tree_dir: str) -> tuple[list[str], dict[str, str]]:
    """Execute parsing and return list of staged operation reprs and dict of table store hashes."""
    MockDB._global_store.clear()
    MockDB._global_next_id.clear()
    saved_typecheck = G.DEBUG_TYPECHECK
    G.DEBUG_TYPECHECK = True
    G.DB = MockDB
    G.TE = get_table_engine("cached")()
    gp = GreatProcessor()
    init_db_layout(gp)
    G.TE.start(gp.Table_Array, G.DB)

    mf = MasterFile()
    temp_dir = None
    if mode == "full":
        mf.version_dict["v3.0"] = tree_dir
    else:
        temp_dir = mf.create_temp_dir()
        mf.version_dict["v3.0"] = temp_dir
        dest = os.path.join(temp_dir, file_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        content = subprocess.check_output(["git", "-C", LINUX_REPO, "show", f"v3.0:{file_path}"])
        with open(dest, "wb") as f:
            f.write(content)

    G.MF = mf
    gp.Version_Name = "v3.0"
    gp.VID = 1

    cs = ChangeSet(f"A\t{file_path}")
    cs.current_vid = 1
    cs.gp = gp
    cs.mf = mf
    G.CURRENT_PARSING_FILE = file_path

    try:
        default_processing(cs, gp)
        cs.parse()
        cs.execute()
        G.TE.commit_all()

        ops_reprs = [repr(op) for op in cs.cs]
        table_hashes = {}
        for tbl_name, rows_dict in MockDB._global_store.items():
            if rows_dict and tbl_name not in _PHYSICAL_SYMBOL_TABLES:
                sorted_rows = sorted(repr(r) for r in rows_dict.values())
                table_hashes[tbl_name] = hashlib.sha256("\n".join(sorted_rows).encode("utf-8")).hexdigest()[:16]
        # Symbol rows as the m_symbol_def / m_symbol_ref views return them (either storage layout), without the
        # surrogate def_id / ref_id
        for view in ("m_symbol_def", "m_symbol_ref"):
            rows = symbol_view_rows(MockDB._global_store, view)
            if rows:
                sorted_rows = sorted(repr(r[1:]) for r in rows)
                table_hashes[view] = hashlib.sha256("\n".join(sorted_rows).encode("utf-8")).hexdigest()[:16]

        return ops_reprs, table_hashes
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        clear_tu_caches()
        G.CURRENT_PARSING_FILE = None
        G.CURRENT_PARSING_DIR = None
        G.DEBUG_TYPECHECK = saved_typecheck
        if G.TE:
            try:
                G.TE.close()
            except Exception:
                pass


class TestCASTGoldenRegression(unittest.TestCase):
    """Sequential golden regression tests verifying exact output equivalence."""

    golden_data: dict[str, Any] = {}
    tree_dir: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree_dir = ensure_golden_tree()
        if os.path.exists(FIXTURE_PATH):
            with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
                cls.golden_data = json.load(f)

    def _assert_file_equivalence(self, file_path: str) -> None:
        if os.environ.get("REGEN_GOLDEN") == "1":
            if file_path not in self.golden_data:
                self.golden_data[file_path] = {}
            for mode in ("full", "isolated"):
                ops_reprs, table_hashes = execute_parse(mode, file_path, self.tree_dir)
                self.golden_data[file_path][mode] = {
                    "ops_count": len(ops_reprs),
                    "ops_hash": hashlib.sha256("\n".join(ops_reprs).encode("utf-8")).hexdigest(),
                    "store_hashes": table_hashes,
                }
            with open(FIXTURE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.golden_data, f, indent=2, sort_keys=True)
            return

        if file_path not in self.golden_data:
            self.skipTest(f"File {file_path} not found in golden fixtures")

        expected_modes = self.golden_data[file_path]
        for mode in ("full", "isolated"):
            expected = expected_modes[mode]
            ops_reprs, table_hashes = execute_parse(mode, file_path, self.tree_dir)
            actual_ops_count = len(ops_reprs)
            actual_ops_hash = hashlib.sha256("\n".join(ops_reprs).encode("utf-8")).hexdigest()

            if (
                actual_ops_count != expected["ops_count"]
                or actual_ops_hash != expected["ops_hash"]
                or table_hashes != expected["store_hashes"]
            ):
                dump_path = f"/tmp/cast_golden_mismatch_{file_path.replace('/', '_')}_{mode}.txt"
                with open(dump_path, "w", encoding="utf-8") as f:
                    f.write(f"=== MISMATCH: {file_path} [{mode}] ===\n")
                    f.write(f"Ops count: expected {expected['ops_count']}, actual {actual_ops_count}\n")
                    f.write(f"Ops hash:  expected {expected['ops_hash']}, actual {actual_ops_hash}\n")
                    f.write("\n--- STORE HASHES ---\n")
                    for k in sorted(set(expected["store_hashes"]) | set(table_hashes)):
                        f.write(f"{k}: expected {expected['store_hashes'].get(k)} vs actual {table_hashes.get(k)}\n")
                    f.write("\n--- ACTUAL OPS DUMP ---\n")
                    for i, op in enumerate(ops_reprs):
                        f.write(f"[{i:04d}] {op}\n")

                self.fail(
                    f"Golden regression mismatch for '{file_path}' in [{mode}] mode!\n"
                    f"  Ops count: expected {expected['ops_count']}, got {actual_ops_count} (delta: {actual_ops_count - expected['ops_count']})\n"
                    f"  Ops hash:  expected {expected['ops_hash'][:16]}, got {actual_ops_hash[:16]}\n"
                    f"  Full dump saved to: {dump_path}"
                )

    def test_drbd_tag_magic_h(self) -> None:
        self._assert_file_equivalence("include/linux/drbd_tag_magic.h")

    def test_kvm_iodev_h(self) -> None:
        self._assert_file_equivalence("virt/kvm/iodev.h")

    def test_lockd_bind_h(self) -> None:
        self._assert_file_equivalence("include/linux/lockd/bind.h")

    def test_netfilter_ebtables_h(self) -> None:
        self._assert_file_equivalence("include/linux/netfilter_bridge/ebtables.h")

    def test_watchdog_w83627hf_wdt_c(self) -> None:
        self._assert_file_equivalence("drivers/watchdog/w83627hf_wdt.c")

    def test_usb_storage_isd200_c(self) -> None:
        self._assert_file_equivalence("drivers/usb/storage/isd200.c")

    def test_sched_h(self) -> None:
        self._assert_file_equivalence("include/linux/sched.h")

    def test_mach_cavium_octeon_kernel_entry_init_h(self) -> None:
        self._assert_file_equivalence("arch/mips/include/asm/mach-cavium-octeon/kernel-entry-init.h")

    def test_alpha_clear_page_s(self) -> None:
        self._assert_file_equivalence("arch/alpha/lib/clear_page.S")

    def test_powerpc_ppc_opc_c(self) -> None:
        self._assert_file_equivalence("arch/powerpc/xmon/ppc-opc.c")

    def test_kernel_sched_c(self) -> None:
        self._assert_file_equivalence("kernel/sched.c")

    def test_net_wanxl_c(self) -> None:
        self._assert_file_equivalence("drivers/net/wan/wanxl.c")

    def test_input_hilkbd_c(self) -> None:
        self._assert_file_equivalence("drivers/input/keyboard/hilkbd.c")

    def test_nls_cp936_c(self) -> None:
        self._assert_file_equivalence("fs/nls/nls_cp936.c")

    def test_x86_entry_64_s(self) -> None:
        self._assert_file_equivalence("arch/x86/kernel/entry_64.S")


if __name__ == "__main__":
    unittest.main()
