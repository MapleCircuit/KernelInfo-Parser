"""tests/test_c_ast.py - Comprehensive Multi-Core C-AST Parser & ChangeSet Test Suite.

Provides isolated, multi-core unit tests executing in RAMDISK (/dev/shm) with MockDB.
Validates AST generation, length delta tracking for optimization reviews,
and verifies ChangeSet.execute() execution without errors.
"""
from __future__ import annotations

import os
import sys

# Raise recursion limit for parsing deeply nested ASTs in kernel source files
sys.setrecursionlimit(50000)
import time
import shutil
import unittest
import subprocess
import multiprocessing
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.globalstuff import G, COLOR
from core.GreatProcessor import GreatProcessor
from core.FileHandler import MasterFile
from core.TableHandling import ChangeSet
from core.DBLayout import (
    init_db_layout,
    m_file_name,
    m_file,
    m_bridge_file,
    m_tag,
    m_bridge_tag,
    m_ast,
)
from table_engine import TEDirectDB, TECachedDB, get_table_engine
from db_engine import MockDB, MariaDB
from parser.c_ast.c_ast_type import safe_spelling, safe_cursor_spelling

# Standard regression files tested across AST parser
# Baseline operations count corresponds to pure AST operations (excluding the 3 lifecycle operations)
TEST_SUITE: list[dict[str, Any]] = [
    {
        "file": "include/linux/drbd_tag_magic.h",
        "baseline_ast_ops": 265,
        "description": "Kernel Header (drbd_tag_magic.h)",
    },
    {
        "file": "virt/kvm/iodev.h",
        "baseline_ast_ops": 395,
        "description": "Kernel Header (virt/kvm/iodev.h)",
    },
    {
        "file": "include/linux/lockd/bind.h",
        "baseline_ast_ops": 220,
        "description": "Kernel Header (lockd/bind.h)",
    },
    {
        "file": "include/linux/netfilter_bridge/ebtables.h",
        "baseline_ast_ops": 1479,
        "description": "Kernel Header (ebtables.h)",
    },
    {
        "file": "drivers/watchdog/w83627hf_wdt.c",
        "baseline_ast_ops": 1789,
        "description": "Watchdog Driver (Latin-1 byte 0xe1 resilience)",
    },
    {
        "file": "drivers/usb/storage/isd200.c",
        "baseline_ast_ops": 8452,
        "description": "USB Storage Driver (Latin-1 byte 0xf6 resilience)",
    },
    {
        "file": "include/linux/sched.h",
        "baseline_ast_ops": 13085,
        "description": "Kernel Header (sched.h)",
    },
    {
        "file": "arch/mips/include/asm/mach-cavium-octeon/kernel-entry-init.h",
        "baseline_ast_ops": 70,
        "description": "Assembly Header (kernel-entry-init.h)",
    },
    {
        "file": "arch/alpha/lib/clear_page.S",
        "baseline_ast_ops": 145,
        "description": "Assembly Source (clear_page.S)",
    },
    {
        "file": "arch/powerpc/xmon/ppc-opc.c",
        "baseline_ast_ops": 7296,
        "description": "PowerPC Opcode Table & Large Initializer Array (ppc-opc.c)",
    },
]

# Total expected operations including the 3 default lifecycle operations (FNAME, FILE, BRIDGE_FILE)
LIFECYCLE_OPS_COUNT = 3


def default_processing(CS: ChangeSet, gp: GreatProcessor) -> None:
    """Stage default file lifecycle operations for an added file."""
    # 0 Check if FNAME exists / Create FNAME
    CS.store(m_file_name.get_set(None, CS.current_path))
    # 1 Create FILE
    CS.store(m_file.set(None, gp.VID, 0, 1, "A", 0))
    # 2 Create BRIDGE FILE
    CS.store(m_bridge_file.set(
        gp.VID,
        CS.ref(m_file_name.fnid),
        CS.ref(m_file.fid),
    ))


def run_single_file_worker(item: dict[str, Any]) -> dict[str, Any]:
    """Execute isolated C-AST parsing and ChangeSet.execute() in a worker process.
    
    Returns structured results tracking operation counts, length deltas, and execute success.
    """
    file_path = item["file"]
    baseline_ast_ops = item.get("baseline_ast_ops", 0)
    baseline_total_ops = baseline_ast_ops + LIFECYCLE_OPS_COUNT
    description = item.get("description", file_path)

    t0 = time.time()
    temp_dir = None
    try:
        # Isolated in-memory DB per worker
        MockDB._global_store.clear()
        G.DEBUG_TYPECHECK = True
        G.DB = MockDB
        te_choice = item.get("table_engine", "cached")
        G.TE = get_table_engine(te_choice)()
        gp = GreatProcessor()
        init_db_layout(gp)
        G.TE.start(gp.Table_Array, G.DB)

        mf = MasterFile()
        temp_dir = mf.create_temp_dir()
        mf.version_dict["v3.0"] = temp_dir
        G.MF = mf
        gp.Version_Name = "v3.0"
        gp.VID = 1

        # Extract file to /dev/shm workspace
        full_path = os.path.join(temp_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        file_content = subprocess.check_output(
            ["git", "-C", "linux", "show", f"v3.0:{file_path}"],
            stderr=subprocess.PIPE,
        )
        with open(full_path, "wb") as f:
            f.write(file_content)

        # Stage and parse ChangeSet
        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = gp
        cs.mf = mf
        G.CURRENT_PARSING_FILE = file_path

        default_processing(cs, gp)
        cs.parse()
        actual_total_ops = len(cs.cs)

        # Execute ChangeSet operations against DB
        t_exec = time.time()
        exec_ok = cs.execute()
        exec_time = time.time() - t_exec
        G.TE.commit_all()

        # Compute tag text fidelity and source code coverage metrics
        raw_lines = file_content.decode("latin-1").replace("\r\n", "\n").split("\n")

        mock_tags = MockDB._global_store.get("m_tag", {})
        mock_bridge = MockDB._global_store.get("m_bridge_tag", {})
        tag_map = {row[0]: row for row in mock_tags.values()}

        mismatches_count = 0
        zero_extent_tags = 0
        covered_char_mask = [[False] * len(line) for line in raw_lines]

        for b_pk, b_row in mock_bridge.items():
            fid, tag_id, line_s, line_e, char_s, char_e = b_row
            tag_row = tag_map.get(tag_id)
            if not tag_row:
                continue
            tag_code = tag_row[3]

            if line_s == 0 and line_e == 0:
                zero_extent_tags += 1
                continue

            # Slice raw file using 1-based start inclusive, end exclusive coordinates
            if line_s == line_e:
                if 1 <= line_s <= len(raw_lines):
                    line_str = raw_lines[line_s - 1]
                    s_idx = max(0, char_s - 1)
                    e_idx = min(len(line_str), char_e - 1) if char_e > 0 else len(line_str)
                    raw_slice = line_str[s_idx:e_idx]
                    for c in range(s_idx, e_idx):
                        covered_char_mask[line_s - 1][c] = True
                else:
                    raw_slice = ""
            else:
                slices = []
                for l in range(line_s, line_e + 1):
                    if 1 <= l <= len(raw_lines):
                        line_str = raw_lines[l - 1]
                        if l == line_s:
                            s_idx = max(0, char_s - 1)
                            slices.append(line_str[s_idx:])
                            for c in range(s_idx, len(line_str)):
                                covered_char_mask[l - 1][c] = True
                        elif l == line_e:
                            e_idx = min(len(line_str), char_e - 1) if char_e > 0 else len(line_str)
                            slices.append(line_str[:e_idx])
                            for c in range(0, e_idx):
                                covered_char_mask[l - 1][c] = True
                        else:
                            slices.append(line_str)
                            for c in range(len(line_str)):
                                covered_char_mask[l - 1][c] = True
                raw_slice = "\n".join(slices)

            # Compare tag code vs raw source slice
            match_exact = (tag_code == raw_slice)
            match_trimmed = (tag_code.rstrip(",; \t\r\n") == raw_slice.rstrip(",; \t\r\n"))
            if not (match_exact or match_trimmed):
                mismatches_count += 1

        total_non_ws = sum(len([c for c in line if not c.isspace()]) for line in raw_lines)
        uncovered_non_ws = 0
        uncovered_samples: list[str] = []
        for l_idx, (line, mask) in enumerate(zip(raw_lines, covered_char_mask)):
            un_text = "".join(c for c, m in zip(line, mask) if not m and not c.isspace())
            if un_text:
                uncovered_non_ws += len(un_text)
                if len(uncovered_samples) < 3:
                    uncovered_samples.append(f"Line {l_idx + 1}: '{un_text[:40]}' (in: {line.strip()[:50]})")

        coverage_ratio = (total_non_ws - uncovered_non_ws) / max(1, total_non_ws)

        elapsed = time.time() - t0
        return {
            "file": file_path,
            "description": description,
            "baseline_total_ops": baseline_total_ops,
            "actual_total_ops": actual_total_ops,
            "delta": actual_total_ops - baseline_total_ops,
            "execute_success": exec_ok,
            "results_count": len(cs.cs_result),
            "total_tags": len(mock_tags),
            "zero_extent_tags": zero_extent_tags,
            "mismatches_count": mismatches_count,
            "coverage_ratio": coverage_ratio,
            "uncovered_non_ws": uncovered_non_ws,
            "uncovered_samples": uncovered_samples,
            "elapsed_s": elapsed,
            "exec_time_s": exec_time,
            "profiler": cs.profiler.to_dict() if cs.profiler else None,
            "error": None,
        }
    except Exception as e:
        elapsed = time.time() - t0
        return {
            "file": file_path,
            "description": description,
            "baseline_total_ops": baseline_total_ops,
            "actual_total_ops": 0,
            "delta": 0,
            "execute_success": False,
            "results_count": 0,
            "total_tags": 0,
            "zero_extent_tags": 0,
            "mismatches_count": 0,
            "coverage_ratio": 0.0,
            "uncovered_non_ws": 0,
            "uncovered_samples": [],
            "elapsed_s": elapsed,
            "exec_time_s": 0,
            "profiler": None,
            "error": str(e),
        }
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestCASTParser(unittest.TestCase):
    """Unit test suite for Clang C-AST parser and ChangeSet execution."""

    def test_multicore_ast_suite(self) -> None:
        """Run multi-core parallel C-AST parsing and ChangeSet.execute() validation."""
        num_cpus = os.cpu_count() or 4
        workers = min(len(TEST_SUITE), num_cpus)
        with multiprocessing.Pool(processes=workers) as pool:
            results = pool.map(run_single_file_worker, TEST_SUITE)

        for r in results:
            self.assertIsNone(r["error"], f"Error in {r['file']}: {r['error']}")
            self.assertGreater(r["actual_total_ops"], 0, f"No operations generated for {r['file']}")
            self.assertTrue(r["execute_success"], f"ChangeSet.execute() failed for {r['file']}")
            self.assertEqual(
                r["actual_total_ops"],
                r["results_count"],
                f"Operation result count mismatch for {r['file']}",
            )

    def test_token_spelling_attributes(self) -> None:
        """Test safe_spelling and safe_cursor_spelling self-caching fallback."""
        class DummyToken:
            def __init__(self, spelling_val: str) -> None:
                self._spelling = spelling_val
            @property
            def spelling(self) -> str:
                return self._spelling

        token = DummyToken("int")
        self.assertEqual(safe_spelling(token), "int")
        self.assertEqual(getattr(token, "spelling_str", None), "int")

        class DummyCursor:
            def __init__(self, spelling_val: str) -> None:
                self._spelling = spelling_val
            @property
            def spelling(self) -> str:
                return self._spelling

        cursor = DummyCursor("my_func")
        self.assertEqual(safe_cursor_spelling(cursor), "my_func")
        self.assertEqual(getattr(cursor, "_spelling_str", None), "my_func")

    def test_table_engine_resolver(self) -> None:
        """Test get_table_engine resolver and alias mappings."""
        self.assertEqual(get_table_engine("cached"), TECachedDB)
        self.assertEqual(get_table_engine("tecacheddb"), TECachedDB)
        self.assertEqual(get_table_engine("direct"), TEDirectDB)
        self.assertEqual(get_table_engine("tedirectdb"), TEDirectDB)
        self.assertEqual(get_table_engine(None), TECachedDB)
        self.assertEqual(get_table_engine(TEDirectDB), TEDirectDB)
        self.assertEqual(get_table_engine(TECachedDB), TECachedDB)
        with self.assertRaises(ValueError):
            get_table_engine("invalid_engine")

    def test_direct_table_engine_execution(self) -> None:
        """Verify parsing and ChangeSet execution with TEDirectDB."""
        item = {
            "file": "virt/kvm/iodev.h",
            "baseline_ast_ops": 395,
            "description": "Kernel Header (virt/kvm/iodev.h)",
            "table_engine": "direct",
        }
        res = run_single_file_worker(item)
        self.assertIsNone(res["error"])
        self.assertTrue(res["execute_success"])
        self.assertEqual(res["actual_total_ops"], res["baseline_total_ops"])

    def test_static_assert_keyword_handling(self) -> None:
        """Verify _Static_assert and static_assert are handled cleanly without warnings."""
        temp_dir = None
        try:
            G.DEBUG_TYPECHECK = True
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v7.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v7.0"
            gp.VID = 1

            file_path = "arch/x86/include/uapi/asm/elf.h"
            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            file_content = subprocess.check_output(
                ["git", "-C", "linux", "show", f"v7.0:{file_path}"],
                stderr=subprocess.PIPE,
            )
            with open(full_path, "wb") as f:
                f.write(file_content)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertGreater(len(cs.cs), 0)
            self.assertTrue(cs.execute())
            self.assertEqual(len(cs.cs), len(cs.cs_result))
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_builtin_types_compatible_p_handling(self) -> None:
        """Verify __builtin_types_compatible_p and compiler intrinsics parse cleanly."""
        temp_dir = None
        try:
            G.DEBUG_TYPECHECK = True
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            file_path = "include/linux/test_compat.h"
            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            snippet = """
#define type_is_int(var) __builtin_types_compatible_p(typeof(var), int)
#define check_compat(a, b) __builtin_choose_expr(__builtin_types_compatible_p(typeof(a), typeof(b)), 1, 0)

static inline int test_compat_fn(int x, long y) {
    int is_int = __builtin_types_compatible_p(typeof(x), int);
    int is_const = __builtin_constant_p(x);
    return is_int + is_const;
}
"""
            with open(full_path, "w") as f:
                f.write(snippet)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertGreater(len(cs.cs), 0)
            self.assertTrue(cs.execute())
            self.assertEqual(len(cs.cs), len(cs.cs_result))
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_function_body_and_type_linking(self) -> None:
        """Verify function statements, call expressions, member accesses, and type linking."""
        temp_dir = None
        try:
            G.DEBUG_TYPECHECK = True
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            file_path = "test_func_body.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
struct device {
    int id;
    void *priv;
};

int helper_calc(int val) {
    return val * 2;
}

int process_device(struct device *dev) {
    int total = 0;
    if (dev != 0) {
        total = helper_calc(dev->id);
    }
    for (int i = 0; i < 10; i = i + 1) {
        total = total + 1;
    }
    while (total > 100) {
        total = total - 1;
        break;
    }
    switch (total) {
        case 1:
            total = 2;
            break;
        default:
            break;
    }
    return total;
}
"""
            with open(full_path, "w") as f:
                f.write(snippet)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertGreater(len(cs.cs), 0)
            self.assertTrue(cs.execute())
            self.assertEqual(len(cs.cs), len(cs.cs_result))

            from core.globalstuff import ASTT

            types_in_ast = set()
            for op in cs.cs:
                if isinstance(op[0], tuple) and len(op) >= 3 and isinstance(op[2], tuple):
                    for val in op[2]:
                        if isinstance(val, (int, ASTT)) and not isinstance(val, bool):
                            try:
                                types_in_ast.add(ASTT(val))
                            except:
                                pass

            self.assertIn(ASTT.C_IfStmt, types_in_ast)
            self.assertIn(ASTT.C_ForStmt, types_in_ast)
            self.assertIn(ASTT.C_WhileStmt, types_in_ast)
            self.assertIn(ASTT.C_SwitchStmt, types_in_ast)
            self.assertIn(ASTT.C_ReturnStmt, types_in_ast)
            self.assertIn(ASTT.C_CallExpr, types_in_ast)
            self.assertIn(ASTT.C_MemberRefExpr, types_in_ast)
            self.assertIn(ASTT.C_DeclRefExpr, types_in_ast)
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_declaration_initializer_and_subsequent_expressions(self) -> None:
        """Verify declarations with '=' do not swallow subsequent statements or expressions."""
        temp_dir = None
        try:
            G.DEBUG_TYPECHECK = True
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            file_path = "test_decl_init.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
void notify_user(int code);

int compute_metrics(int base) {
    int factor = 10;
    int scaled = base * factor;
    scaled = scaled + 5;
    notify_user(scaled);
    return scaled;
}
"""
            with open(full_path, "w") as f:
                f.write(snippet)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertGreater(len(cs.cs), 0)
            self.assertTrue(cs.execute())
            self.assertEqual(len(cs.cs), len(cs.cs_result))

            from core.globalstuff import ASTT
            types_in_ast = set()
            tag_codes = []
            for op in cs.cs:
                if op[0] == 10:
                    tag_codes.append(op[2][3])
                elif isinstance(op[0], tuple) and len(op) >= 3 and isinstance(op[2], tuple):
                    for val in op[2]:
                        if isinstance(val, (int, ASTT)) and not isinstance(val, bool):
                            try:
                                types_in_ast.add(ASTT(val))
                            except:
                                pass

            self.assertIn(ASTT.C_BinaryOperator, types_in_ast)
            self.assertIn(ASTT.C_CallExpr, types_in_ast)
            self.assertIn(ASTT.C_ReturnStmt, types_in_ast)

            # Ensure 'int factor = 10;' tag does NOT swallow subsequent lines
            factor_tags = [t for t in tag_codes if "int factor" in t and "compute_metrics" not in t]
            self.assertTrue(any("int factor = 10;" in t for t in factor_tags))
            self.assertFalse(any("notify_user" in t for t in factor_tags))
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_function_parameter_boundaries(self) -> None:
        """Verify function parameter declarations terminate cleanly and do not bleed into function body."""
        temp_dir = None
        try:
            G.DEBUG_TYPECHECK = True
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            file_path = "test_param_boundaries.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
int add_three(int a, int b, int c) {
    int res = a + b;
    res = res + c;
    return res;
}
"""
            with open(full_path, "w") as f:
                f.write(snippet)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertGreater(len(cs.cs), 0)
            self.assertTrue(cs.execute())
            self.assertEqual(len(cs.cs), len(cs.cs_result))

            tag_codes = [op[2][3] for op in cs.cs if op[0] == 10]
            # Parameter c must strictly be 'int c' without bleeding into '{' or 'int res'
            param_c_tags = [t for t in tag_codes if "int c" in t and "add_three" not in t]
            self.assertTrue(len(param_c_tags) > 0)
            for t in param_c_tags:
                self.assertNotIn("int res", t)
                self.assertNotIn("return res", t)
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_nested_function_definitions(self) -> None:
        """Verify GNU C nested functions inside compound statements parse cleanly."""
        temp_dir = None
        try:
            G.DEBUG_TYPECHECK = True
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            file_path = "test_nested_fn.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
int outer_calc(int x) {
    int square(int val) {
        return val * val;
    }
    return square(x) + 1;
}
"""
            with open(full_path, "w") as f:
                f.write(snippet)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertGreater(len(cs.cs), 0)
            self.assertTrue(cs.execute())
            self.assertEqual(len(cs.cs), len(cs.cs_result))

            from core.globalstuff import ASTT
            types_in_ast = set()
            for op in cs.cs:
                if isinstance(op[0], tuple) and len(op) >= 3 and isinstance(op[2], tuple):
                    for val in op[2]:
                        if isinstance(val, (int, ASTT)) and not isinstance(val, bool):
                            try:
                                types_in_ast.add(ASTT(val))
                            except:
                                pass

            self.assertIn(ASTT.C_CallExpr, types_in_ast)
            self.assertIn(ASTT.C_ReturnStmt, types_in_ast)
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_initializer_expression_references(self) -> None:
        """Verify calls, member accesses, and variable refs inside initializers are extracted."""
        temp_dir = None
        try:
            G.DEBUG_TYPECHECK = True
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            file_path = "test_init_refs.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
struct item {
    int value;
};

int get_multiplier(void) { return 3; }

int process_item(struct item *it) {
    int factor = get_multiplier();
    int base = it->value;
    return factor * base;
}
"""
            with open(full_path, "w") as f:
                f.write(snippet)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertGreater(len(cs.cs), 0)
            self.assertTrue(cs.execute())
            self.assertEqual(len(cs.cs), len(cs.cs_result))

            from core.globalstuff import ASTT
            types_in_ast = set()
            for op in cs.cs:
                if isinstance(op[0], tuple) and len(op) >= 3 and isinstance(op[2], tuple):
                    for val in op[2]:
                        if isinstance(val, (int, ASTT)) and not isinstance(val, bool):
                            try:
                                types_in_ast.add(ASTT(val))
                            except:
                                pass

            self.assertIn(ASTT.C_CallExpr, types_in_ast)
            self.assertIn(ASTT.C_MemberRefExpr, types_in_ast)
            self.assertIn(ASTT.C_DeclRefExpr, types_in_ast)
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_struct_with_intermediate_and_trailing_ifdef(self) -> None:
        """Verify struct definitions with intermediate and trailing #ifdef/#endif parse cleanly without leaking declarators."""
        temp_dir = None
        try:
            G.DEBUG_TYPECHECK = True
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            file_path = "test_sched_task.h"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
struct task_struct {
    volatile long state;
    void *stack;
    int flags;
#ifdef CONFIG_SMP
    int on_cpu;
    int cpu;
#endif
    int prio;
#ifdef CONFIG_PREEMPT_RCU
    int rcu_read_lock_nesting;
#endif
};
"""
            with open(full_path, "w") as f:
                f.write(snippet)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertGreater(len(cs.cs), 0)
            self.assertTrue(cs.execute())
            self.assertEqual(len(cs.cs), len(cs.cs_result))

            from core.globalstuff import ASTT
            ast_names = []
            tag_codes = []
            for op in cs.cs:
                if op[0] == 10:
                    tag_codes.append(op[2][3])
                elif isinstance(op[0], tuple) and len(op) >= 3 and isinstance(op[2], tuple):
                    name = op[2][1] if len(op[2]) > 1 else None
                    if name:
                        ast_names.append(name)

            # Ensure '#endif' or directive keywords were NOT mis-parsed as variable declarators
            self.assertNotIn("endif", ast_names)
            self.assertIn("task_struct", ast_names)
            self.assertIn("state", ast_names)
            self.assertIn("prio", ast_names)

            # Ensure struct tag covers the entire struct definition
            struct_tags = [t for t in tag_codes if "struct task_struct" in t]
            self.assertTrue(len(struct_tags) > 0)
            self.assertTrue(any("rcu_read_lock_nesting" in t for t in struct_tags))
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_tag_fidelity_sched_h(self) -> None:
        """Verify full tag text fidelity and 100% source code coverage on linux/include/linux/sched.h."""
        res = assert_file_tag_fidelity("include/linux/sched.h", min_coverage=1.0)
        self.assertEqual(res["mismatches_count"], 0)
        self.assertEqual(res["uncovered_non_ws"], 0)
        self.assertEqual(res["coverage_ratio"], 1.0)
        self.assertGreater(res["total_tags"], 2000)

    def test_tag_fidelity_drbd_magic(self) -> None:
        """Verify tag text fidelity on include/linux/drbd_tag_magic.h."""
        res = assert_file_tag_fidelity("include/linux/drbd_tag_magic.h", min_coverage=1.0)
        self.assertEqual(res["mismatches_count"], 0)
        self.assertEqual(res["uncovered_non_ws"], 0)
        self.assertEqual(res["coverage_ratio"], 1.0)

    def test_tag_fidelity_kvm_iodev(self) -> None:
        """Verify tag text fidelity on virt/kvm/iodev.h."""
        res = assert_file_tag_fidelity("virt/kvm/iodev.h", min_coverage=1.0)
        self.assertEqual(res["mismatches_count"], 0)
        self.assertEqual(res["uncovered_non_ws"], 0)
        self.assertEqual(res["coverage_ratio"], 1.0)

    def test_tag_fidelity_lockd_bind(self) -> None:
        """Verify tag text fidelity on include/linux/lockd/bind.h."""
        res = assert_file_tag_fidelity("include/linux/lockd/bind.h", min_coverage=1.0)
        self.assertEqual(res["mismatches_count"], 0)
        self.assertEqual(res["uncovered_non_ws"], 0)
        self.assertEqual(res["coverage_ratio"], 1.0)

    def test_tag_fidelity_ppc_opc(self) -> None:
        """Verify tag text fidelity on arch/powerpc/xmon/ppc-opc.c."""
        res = assert_file_tag_fidelity("arch/powerpc/xmon/ppc-opc.c", min_coverage=1.0)
        self.assertEqual(res["mismatches_count"], 0)
        self.assertEqual(res["uncovered_non_ws"], 0)
        self.assertEqual(res["coverage_ratio"], 1.0)

    def test_tag_fidelity_ebtables(self) -> None:
        """Verify tag text fidelity on include/linux/netfilter_bridge/ebtables.h."""
        res = assert_file_tag_fidelity("include/linux/netfilter_bridge/ebtables.h", min_coverage=1.0)
        self.assertEqual(res["mismatches_count"], 0)
        self.assertEqual(res["uncovered_non_ws"], 0)
        self.assertEqual(res["coverage_ratio"], 1.0)

    def test_tag_fidelity_watchdog(self) -> None:
        """Verify tag text fidelity on drivers/watchdog/w83627hf_wdt.c."""
        res = assert_file_tag_fidelity("drivers/watchdog/w83627hf_wdt.c", min_coverage=1.0)
        self.assertEqual(res["mismatches_count"], 0)
        self.assertEqual(res["uncovered_non_ws"], 0)
        self.assertEqual(res["coverage_ratio"], 1.0)

    def test_tag_fidelity_isd200(self) -> None:
        """Verify tag text fidelity on drivers/usb/storage/isd200.c."""
        res = assert_file_tag_fidelity("drivers/usb/storage/isd200.c", min_coverage=1.0)
        self.assertEqual(res["mismatches_count"], 0)
        self.assertEqual(res["uncovered_non_ws"], 0)
        self.assertEqual(res["coverage_ratio"], 1.0)





def assert_file_tag_fidelity(
    file_path: str,
    min_coverage: float = 0.99,
) -> dict[str, Any]:
    """Audit every created AST tag against the raw source file.
    
    Verifies:
    1. Every tag in m_bridge_tag has valid coordinates within file boundaries.
    2. Slicing the raw source file with (line_s, line_e, char_s, char_e) matches m_tag.code
       (accounting for delimiter alignment on declarations and enum items).
    3. Non-whitespace character coverage across the file meets or exceeds `min_coverage`.
    
    Args:
        file_path: Relative repository file path (e.g. 'include/linux/sched.h').
        min_coverage: Minimum acceptable ratio of non-whitespace source characters tagged.
        
    Returns:
        Dictionary containing audit statistics and fidelity metrics.
    """
    MockDB._global_store.clear()
    G.DEBUG_TYPECHECK = True
    G.DB = MockDB
    G.TE = TECachedDB()
    gp = GreatProcessor()
    init_db_layout(gp)
    G.TE.start(gp.Table_Array, G.DB)

    mf = MasterFile()
    temp_dir = mf.create_temp_dir()
    mf.version_dict["v3.0"] = temp_dir
    G.MF = mf
    gp.Version_Name = "v3.0"
    gp.VID = 1

    try:
        full_path = os.path.join(temp_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        file_content_bytes = subprocess.check_output(
            ["git", "-C", "linux", "show", f"v3.0:{file_path}"],
            stderr=subprocess.PIPE,
        )
        with open(full_path, "wb") as f:
            f.write(file_content_bytes)

        raw_lines = file_content_bytes.decode("latin-1").replace("\r\n", "\n").split("\n")

        cs = ChangeSet(f"A\t{file_path}")
        cs.current_vid = 1
        cs.gp = gp
        cs.mf = mf
        G.CURRENT_PARSING_FILE = file_path

        default_processing(cs, gp)
        cs.parse()
        exec_ok = cs.execute()
        G.TE.commit_all()

        if not exec_ok:
            raise AssertionError(f"CS.execute() failed for {file_path}")

        mock_tags = MockDB._global_store.get("m_tag", {})
        mock_bridge = MockDB._global_store.get("m_bridge_tag", {})
        tag_map = {row[0]: row for row in mock_tags.values()}

        mismatches: list[dict[str, Any]] = []
        zero_extent_tags = 0
        covered_char_mask = [[False] * len(line) for line in raw_lines]

        for b_pk, b_row in mock_bridge.items():
            fid, tag_id, line_s, line_e, char_s, char_e = b_row
            tag_row = tag_map.get(tag_id)
            if not tag_row:
                continue
            tag_code = tag_row[3]

            if line_s == 0 and line_e == 0:
                zero_extent_tags += 1
                continue

            # Slice raw file using 1-based start inclusive, end exclusive coordinates
            if line_s == line_e:
                if 1 <= line_s <= len(raw_lines):
                    line_str = raw_lines[line_s - 1]
                    s_idx = max(0, char_s - 1)
                    e_idx = min(len(line_str), char_e - 1) if char_e > 0 else len(line_str)
                    raw_slice = line_str[s_idx:e_idx]
                    for c in range(s_idx, e_idx):
                        covered_char_mask[line_s - 1][c] = True
                else:
                    raw_slice = ""
            else:
                slices = []
                for l in range(line_s, line_e + 1):
                    if 1 <= l <= len(raw_lines):
                        line_str = raw_lines[l - 1]
                        if l == line_s:
                            s_idx = max(0, char_s - 1)
                            slices.append(line_str[s_idx:])
                            for c in range(s_idx, len(line_str)):
                                covered_char_mask[l - 1][c] = True
                        elif l == line_e:
                            e_idx = min(len(line_str), char_e - 1) if char_e > 0 else len(line_str)
                            slices.append(line_str[:e_idx])
                            for c in range(0, e_idx):
                                covered_char_mask[l - 1][c] = True
                        else:
                            slices.append(line_str)
                            for c in range(len(line_str)):
                                covered_char_mask[l - 1][c] = True
                raw_slice = "\n".join(slices)

            # Compare tag code vs raw source slice
            match_exact = (tag_code == raw_slice)
            match_trimmed = (tag_code.rstrip(",; \t\r\n") == raw_slice.rstrip(",; \t\r\n"))
            if not (match_exact or match_trimmed):
                mismatches.append({
                    "tag_id": tag_id,
                    "coords": (line_s, line_e, char_s, char_e),
                    "tag_code": tag_code,
                    "raw_slice": raw_slice,
                })

        total_non_ws = sum(len([c for c in line if not c.isspace()]) for line in raw_lines)
        uncovered_non_ws = 0
        uncovered_lines: list[tuple[int, str, str]] = []
        for l_idx, (line, mask) in enumerate(zip(raw_lines, covered_char_mask)):
            un_text = "".join(c for c, m in zip(line, mask) if not m and not c.isspace())
            if un_text:
                uncovered_non_ws += len(un_text)
                uncovered_lines.append((l_idx + 1, un_text, line.strip()))

        coverage_ratio = (total_non_ws - uncovered_non_ws) / max(1, total_non_ws)

        if mismatches:
            sample_mismatches = mismatches[:3]
            raise AssertionError(
                f"Tag text mismatch detected in {file_path} ({len(mismatches)} mismatches). Samples: {sample_mismatches}"
            )

        if coverage_ratio < min_coverage:
            raise AssertionError(
                f"Tag coverage ratio {coverage_ratio:.4f} below threshold {min_coverage:.4f} for {file_path} "
                f"({uncovered_non_ws} uncovered non-whitespace characters across {len(uncovered_lines)} lines)."
            )

        return {
            "file": file_path,
            "execute_success": exec_ok,
            "total_tags": len(mock_tags),
            "total_bridge": len(mock_bridge),
            "zero_extent_tags": zero_extent_tags,
            "mismatches_count": len(mismatches),
            "coverage_ratio": coverage_ratio,
            "uncovered_non_ws": uncovered_non_ws,
        }

    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)



def format_fidelity_table(results: list[dict[str, Any]]) -> str:
    """Format a clean colorized report table summarizing tag fidelity and code coverage."""
    lines = [
        "",
        COLOR.cyan("=" * 105),
        COLOR.cyan("                    TAG TEXT & RAW SOURCE FIDELITY AUDIT REPORT"),
        COLOR.cyan("=" * 105),
        f"{'Target File':<42} | {'Tags':>6} | {'Mismatches':>10} | {'Coverage':>8} | {'Uncovered':>9} | {'Fidelity':<8}",
        COLOR.cyan("-" * 105),
    ]
    for r in results:
        file_display = r["file"]
        if len(file_display) > 42:
            file_display = "..." + file_display[-39:]
        tags_cnt = r.get("total_tags", 0)
        mismatches = r.get("mismatches_count", 0)
        cov_pct = r.get("coverage_ratio", 0.0) * 100.0
        uncovered = r.get("uncovered_non_ws", 0)

        if mismatches == 0 and cov_pct >= 99.0:
            status_str = COLOR.green("PASS")
            mismatches_str = COLOR.green("0")
        elif mismatches == 0:
            status_str = COLOR.yellow("PASS (COV)")
            mismatches_str = COLOR.green("0")
        else:
            status_str = COLOR.red("FAIL")
            mismatches_str = COLOR.red(str(mismatches))

        lines.append(
            f"{file_display:<42} | {tags_cnt:>6} | {mismatches_str:>19} | {cov_pct:>7.2f}% | {uncovered:>9} | {status_str:<17}"
        )
    lines.append(COLOR.cyan("=" * 105))
    return "\n".join(lines)


def run_c_ast_tests(
    target_file: str | None = None,
    profile: bool = False,
    fidelity: bool = True,
    table_engine: str = "cached",
) -> int:

    """Programmatic multi-core test runner invoked via CLI in main.py.
    
    Args:
        target_file: Optional single file path to test.
        profile: Whether to collect and print granular stage profiler breakdowns.
        fidelity: Whether to print detailed tag text and source code fidelity audit.
        table_engine: Table engine variant ('cached' or 'direct').
        
    Returns:
        0 if all tests pass, 1 otherwise.
    """
    if profile:
        G.PROFILING_ENABLED = True

    te_display = table_engine if isinstance(table_engine, str) else getattr(table_engine, "__name__", str(table_engine))

    print(COLOR.cyan("\n=========================================================================================="))
    print(COLOR.cyan("                    MULTI-CORE C-AST PARSER & EXECUTE TEST RUNNER                         "))
    print(COLOR.cyan("=========================================================================================="))
    print(f"Workspace:   {COLOR.magenta(G.RAMDISK)} (Isolated /dev/shm RAMDISK)")
    print(f"Dataset:     {COLOR.magenta('Linux v3.0 (Read-Only)')}")
    print(f"Backend:     {COLOR.magenta('In-Memory MockDB (Isolated)')}")
    print(f"TableEngine: {COLOR.magenta(te_display.capitalize())}")
    if G.PROFILING_ENABLED:
        print(f"Profiler:    {COLOR.green('ACTIVE (-p / --profile)')}")
    if fidelity:
        print(f"Fidelity:    {COLOR.green('ACTIVE (-f / --fidelity)')}")
    print()

    start_time = time.time()

    if target_file:
        print(COLOR.cyan(f"[*] Executing Single Target Test: {target_file} (TableEngine: {te_display})"))
        item = {
            "file": target_file,
            "baseline_ast_ops": 0,
            "description": f"Target: {target_file}",
            "table_engine": table_engine,
        }
        res = run_single_file_worker(item)
        elapsed = time.time() - start_time
        if res["execute_success"] and not res["error"]:
            print(COLOR.green(f"\n[+] PASS: {target_file}"))
            print(f"    - Operations Staged:    {res['actual_total_ops']:,}")
            print(f"    - CS.execute():         {COLOR.green('SUCCESS')}")
            print(f"    - AST Tags Created:     {res.get('total_tags', 0):,}")
            mismatches = res.get("mismatches_count", 0)
            mismatch_str = COLOR.green("0") if mismatches == 0 else COLOR.red(str(mismatches))
            print(f"    - Tag Text Mismatches:  {mismatch_str}")
            cov_pct = res.get("coverage_ratio", 0.0) * 100.0
            cov_str = COLOR.green(f"{cov_pct:.2f}%") if cov_pct >= 99.0 else COLOR.yellow(f"{cov_pct:.2f}%")
            print(f"    - Source Code Coverage: {cov_str} ({res.get('uncovered_non_ws', 0)} uncovered non-whitespace chars)")
            if res.get("uncovered_samples"):
                print("    - Uncovered Samples:")
                for s in res["uncovered_samples"]:
                    print(f"        * {s}")
            print(f"    - Execution Time:       {elapsed:.2f}s\n")
            if res.get("profiler"):
                from core.Profiler import PipelineProfiler, format_profiling_report
                prof_obj = PipelineProfiler.from_dict(res["profiler"])
                print(format_profiling_report([prof_obj], title=f"PIPELINE PROFILE: {target_file}"))
            return 0
        else:
            print(COLOR.red(f"\n[-] FAIL: {target_file} after {elapsed:.2f}s"))
            print(COLOR.red(f"    Error: {res['error'] or 'CS.execute() returned False'}"))
            return 1

    # Run multi-core test suite
    num_cpus = os.cpu_count() or 4
    workers = min(len(TEST_SUITE), num_cpus)
    print(COLOR.cyan(f"[*] Dispatching test suite across {workers} parallel CPU workers..."))

    suite_items = [{**item, "table_engine": table_engine} for item in TEST_SUITE]

    with multiprocessing.Pool(processes=workers) as pool:
        results = pool.map(run_single_file_worker, suite_items)

    elapsed = time.time() - start_time

    print("\n" + "=" * 90)
    print(f"{'Target File':<42} | {'Base':>6} | {'Ops':>6} | {'Delta':>7} | {'CS.execute':<10} | {'Time':>6}")
    print("=" * 90)

    all_passed = True
    any_delta = False
    profiler_list = []
    for r in results:
        if r.get("profiler"):
            from core.Profiler import PipelineProfiler
            profiler_list.append(PipelineProfiler.from_dict(r["profiler"]))

        if r["error"]:
            all_passed = False
            exec_str = COLOR.red("ERROR")
        elif r["execute_success"] and r["actual_total_ops"] > 0:
            exec_str = COLOR.green("SUCCESS")
        else:
            all_passed = False
            exec_str = COLOR.red("FAILED")

        delta = r["delta"]
        if delta > 0:
            delta_str = COLOR.yellow(f"+{delta}")
            any_delta = True
        elif delta < 0:
            delta_str = COLOR.yellow(f"{delta}")
            any_delta = True
        else:
            delta_str = COLOR.green("0")

        file_display = r["file"]
        if len(file_display) > 42:
            file_display = "..." + file_display[-39:]

        print(f"{file_display:<42} | {r['baseline_total_ops']:>6} | {r['actual_total_ops']:>6} | {delta_str:>16} | {exec_str:<19} | {r['elapsed_s']:>5.2f}s")
        if r["error"]:
            print(COLOR.red(f"   --> Error: {r['error']}"))

    print("=" * 90)
    if any_delta:
        print(COLOR.yellow("[*] NOTE: Operation count deltas detected."))
        print("    Length changes reflect AST optimization / output changes and are tracked for review.\n")

    if fidelity:
        print(format_fidelity_table(results))

    if G.PROFILING_ENABLED and profiler_list:
        from core.Profiler import format_profiling_report
        print(format_profiling_report(profiler_list, title="MULTI-CORE PIPELINE STAGE TIMING BREAKDOWN"))

    if all_passed:
        print(COLOR.green(f"[+] ALL {len(results)} MULTI-CORE TESTS PASSED & EXECUTED CLEANLY in {elapsed:.2f}s!"))
        print(COLOR.cyan("==========================================================================================\n"))
        return 0
    else:
        print(COLOR.red(f"[-] TEST FAILURES DETECTED in {elapsed:.2f}s."))
        print(COLOR.cyan("==========================================================================================\n"))
        return 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="C-AST Parser Test Suite")
    parser.add_argument("target_file", nargs="?", default=None, help="Target file to parse")
    parser.add_argument("-p", "--profile", action="store_true", help="Enable granular stage timing profiler")
    parser.add_argument("-f", "--fidelity", action="store_true", default=True, help="Display full tag text & source code fidelity audit report (default: True)")
    parser.add_argument("--no-fidelity", dest="fidelity", action="store_false", help="Disable tag text & source code fidelity audit report")

    parser.add_argument(
        "--te", "--table-engine",
        dest="table_engine",
        default="cached",
        choices=["cached", "direct", "tecacheddb", "tedirectdb"],
        help="Select Table Engine architecture backend (default: cached)",
    )
    args = parser.parse_args()
    sys.exit(run_c_ast_tests(
        target_file=args.target_file,
        profile=args.profile,
        fidelity=args.fidelity,
        table_engine=args.table_engine,
    ))

