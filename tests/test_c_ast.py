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

from core.globalstuff import G, COLOR, REF_OLD, REF_ROOT, ASTT
from core.GreatProcessor import GreatProcessor, CompressedChangeSetDict
from core.FileHandler import MasterFile
from core.TableHandling import ChangeSet
from core.DBLayout import (
    TABLES,
    init_db_layout,
    m_file_name,
    m_file,
    m_bridge_file,
    m_tag_code,
    m_tag,
    m_moved_tag,
    m_bridge_tag,
    m_ast,
    m_ast_container,
    m_map_ast,
    m_bridge_map,
)
from table_engine import TEDirectDB, TECachedDB, get_table_engine
from db_engine import MockDB, MariaDB
from parser.c_ast import safe_spelling, safe_cursor_spelling

# Standard regression files tested across AST parser
# Baseline operations count corresponds to pure AST operations (excluding the 3 lifecycle operations)
TEST_SUITE: list[dict[str, Any]] = [
    {
        "file": "include/linux/drbd_tag_magic.h",
        "baseline_ast_ops": 328,
        "description": "Kernel Header (drbd_tag_magic.h)",
    },
    {
        "file": "virt/kvm/iodev.h",
        "baseline_ast_ops": 227,
        "description": "Kernel Header (virt/kvm/iodev.h)",
    },
    {
        "file": "include/linux/lockd/bind.h",
        "baseline_ast_ops": 207,
        "description": "Kernel Header (lockd/bind.h)",
    },
    {
        "file": "include/linux/netfilter_bridge/ebtables.h",
        "baseline_ast_ops": 1001,
        "description": "Kernel Header (ebtables.h)",
    },
    {
        "file": "drivers/watchdog/w83627hf_wdt.c",
        "baseline_ast_ops": 1187,
        "description": "Watchdog Driver (Latin-1 byte 0xe1 resilience)",
    },
    {
        "file": "drivers/usb/storage/isd200.c",
        "baseline_ast_ops": 4295,
        "description": "USB Storage Driver (Latin-1 byte 0xf6 resilience)",
    },
    {
        "file": "include/linux/sched.h",
        "baseline_ast_ops": 10227,
        "description": "Kernel Header (sched.h)",
    },
    {
        "file": "arch/mips/include/asm/mach-cavium-octeon/kernel-entry-init.h",
        "baseline_ast_ops": 111,
        "description": "Assembly Header (kernel-entry-init.h)",
    },
    {
        "file": "arch/alpha/lib/clear_page.S",
        "baseline_ast_ops": 12,
        "description": "Assembly Source (clear_page.S)",
    },
    {
        "file": "arch/powerpc/xmon/ppc-opc.c",
        "baseline_ast_ops": 18700,
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
        mock_tag_codes = MockDB._global_store.get("m_tag_code", {})
        mock_bridge = MockDB._global_store.get("m_bridge_tag", {})
        tag_map = {row[0]: row for row in mock_tags.values()}
        code_map = {row[0]: row[1] for row in mock_tag_codes.values()}

        mismatches_count = 0
        zero_extent_tags = 0
        covered_char_mask = [[False] * len(line) for line in raw_lines]

        for b_pk, b_row in mock_bridge.items():
            fid, tag_id, line_s, line_e, char_s, char_e = b_row
            tag_row = tag_map.get(tag_id)
            if not tag_row:
                continue
            tag_hash = tag_row[3]
            tag_code = code_map.get(tag_hash, "")

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
        res_dict = {
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
        if item.get("capture_details", False):
            res_dict["staged_ops"] = list(cs.cs)
            res_dict["result_ops"] = list(cs.cs_result)
            res_dict["store"] = {
                "m_tag": dict(mock_tags),
                "m_tag_code": dict(mock_tag_codes),
                "m_bridge_tag": dict(mock_bridge),
                "m_ast": dict(MockDB._global_store.get("m_ast", {})),
                "m_ast_container": dict(MockDB._global_store.get("m_ast_container", {})),
                "m_map_ast": dict(MockDB._global_store.get("m_map_ast", {})),
                "m_bridge_map": dict(MockDB._global_store.get("m_bridge_map", {})),
                "m_file_name": dict(MockDB._global_store.get("m_file_name", {})),
                "m_file": dict(MockDB._global_store.get("m_file", {})),
                "m_bridge_file": dict(MockDB._global_store.get("m_bridge_file", {})),
                "m_moved_tag": dict(MockDB._global_store.get("m_moved_tag", {})),
                "m_ast_include": dict(MockDB._global_store.get("m_ast_include", {})),
            }
        return res_dict
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

    def tearDown(self) -> None:
        if G.TE:
            try:
                G.TE.close()
            except Exception:
                pass
        MockDB._global_store.clear()

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

    def test_sched_task_struct_canonical_tag(self) -> None:
        """Verify struct task_struct in sched.h produces a canonical C_structdecl tag rather than tsk."""
        file_path = "include/linux/sched.h"
        res = run_single_file_worker({
            "file": file_path,
            "baseline_ast_ops": 9307,
            "description": "Kernel Header (sched.h)",
        })
        self.assertIsNone(res["error"])
        self.assertTrue(res["execute_success"])

        # Check DB state
        asts = MockDB._global_store.get("m_ast", {})
        bridge_tags = MockDB._global_store.get("m_bridge_tag", {})
        tags = MockDB._global_store.get("m_tag", {})

        # Find the tag spanning line 1220 to 1573
        task_struct_tags = []
        for b_pk, b_row in bridge_tags.items():
            fid, tag_id, line_s, line_e, char_s, char_e = b_row
            if line_s == 1220 and line_e == 1573:
                tag = tags.get(tag_id) or tags.get((tag_id, 1))
                if tag:
                    ast_obj = asts.get(tag[4])
                    task_struct_tags.append((tag_id, ast_obj))

        self.assertEqual(len(task_struct_tags), 1, f"Expected exactly 1 tag spanning 1220..1573, found {task_struct_tags}")
        tag_id, ast_obj = task_struct_tags[0]
        self.assertIsNotNone(ast_obj)
        self.assertEqual(ast_obj[1], "task_struct", f"Expected tag AST name 'task_struct', got {ast_obj[1]}")
        self.assertEqual(ast_obj[2], ASTT.C_structdecl, f"Expected C_structdecl ({ASTT.C_structdecl}), got {ast_obj[2]}")

    def test_macro_symbol_def_and_expansion_ref(self) -> None:
        """Verify #define macros are staged in m_symbol_def and expansions are staged in m_symbol_ref as MacroExpansion."""
        from core.globalstuff import SymbolRole
        res = run_single_file_worker({
            "file": "drivers/watchdog/w83627hf_wdt.c",
            "baseline_ast_ops": 1187,
            "description": "Watchdog Driver",
        })
        self.assertIsNone(res["error"])
        self.assertTrue(res["execute_success"])

        sym_defs = MockDB._global_store.get("m_symbol_def", {})
        macro_defs = {row[5]: row for row in sym_defs.values() if row[6] in (int(ASTT.CPPro_define), int(ASTT.CPPro_define_macro))}
        self.assertIn("WATCHDOG_NAME", macro_defs)
        self.assertIn("WATCHDOG_TIMEOUT", macro_defs)
        self.assertIn("WDT_EFER", macro_defs)
        self.assertIn("WDT_EFIR", macro_defs)

        # Verify line range on macro definition
        wd_name_def = macro_defs["WATCHDOG_NAME"]
        self.assertEqual(wd_name_def[7], 45) # line_s
        self.assertEqual(wd_name_def[8], 45) # line_e

        # Verify macro expansions in m_symbol_ref
        sym_refs = MockDB._global_store.get("m_symbol_ref", {})
        macro_expansions = [row for row in sym_refs.values() if row[5] == int(SymbolRole.MacroExpansion)]
        self.assertGreaterEqual(len(macro_expansions), 5, f"Expected at least 5 macro expansions, found {len(macro_expansions)}")

    def test_struct_simple_members_no_compound_container(self) -> None:
        """Verify simple struct members (e.g. int, long, char) generate zero rows in m_ast_container."""
        snippet = (
            "struct sample_device {\n"
            "    int device_id;\n"
            "    long timeout_ms;\n"
            "    char active_state;\n"
            "    unsigned int flags;\n"
            "    int *buffer_ptr;\n"
            "};\n"
        )
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

        file_path = "sample_device.h"
        full_path = os.path.join(temp_dir, file_path)
        with open(full_path, "w") as f:
            f.write(snippet)

        try:
            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            cs.store(m_file_name.get_set(None, cs.current_path))
            cs.store(m_file.set(None, gp.VID, 0, 1, "A", 0))
            cs.store(m_bridge_file.set(gp.VID, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

            cs.parse()
            cs.execute()
            G.TE.commit_all()

            asts = MockDB._global_store.get("m_ast", {})
            containers = MockDB._global_store.get("m_ast_container", {})

            # Map by name
            ast_by_name = {v[1]: (k, v) for k, v in asts.items() if v[1]}

            # Simple members: device_id (int), timeout_ms (long), active_state (char)
            for simple_name, expected_type in [("device_id", ASTT.C_int), ("timeout_ms", ASTT.C_long), ("active_state", ASTT.C_char)]:
                self.assertIn(simple_name, ast_by_name)
                aid, arow = ast_by_name[simple_name]
                self.assertEqual(arow[2], expected_type)
                # Verify ZERO container rows for this simple member AST
                member_containers = [c for c in containers.values() if c[0] == aid]
                self.assertEqual(len(member_containers), 0, f"Simple member {simple_name} should have 0 container rows, got {member_containers}")

            # Compound members: flags (unsigned int -> C_unsigned with container ref), buffer_ptr (int * -> C_Compound)
            self.assertIn("buffer_ptr", ast_by_name)
            ptr_id, ptr_row = ast_by_name["buffer_ptr"]
            self.assertEqual(ptr_row[2], ASTT.C_Compound)
            ptr_containers = [c for c in containers.values() if c[0] == ptr_id]
            self.assertGreater(len(ptr_containers), 0, "Compound member buffer_ptr must have container rows")

            # Struct itself must link all members in its container
            self.assertIn("sample_device", ast_by_name)
            s_id, s_row = ast_by_name["sample_device"]
            self.assertEqual(s_row[2], ASTT.C_structdecl)
            struct_containers = [c for c in containers.values() if c[0] == s_id]
            self.assertEqual(len(struct_containers), 5, f"Struct sample_device should have 5 member containers, got {len(struct_containers)}")
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

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

    def test_compressed_changeset_dict_lru500(self) -> None:
        """Verify CompressedChangeSetDict with default LRU 500 and memory mode initialization."""
        # 1. Verify GreatProcessor init defaults
        gp = GreatProcessor()
        self.assertIsInstance(gp.ChangeSet_Dict, CompressedChangeSetDict)
        self.assertIsInstance(gp.Alt_ChangeSet_Dict, CompressedChangeSetDict)
        self.assertEqual(gp.ChangeSet_Dict._lru_size, 500)
        self.assertEqual(gp.Alt_ChangeSet_Dict._lru_size, 500)

        # 2. Verify init_cs_dict under normal, low, and very_low modes
        orig_mode = G.MEMORY_MODE
        try:
            G.MEMORY_MODE = "normal"
            gp.init_cs_dict()
            self.assertEqual(gp.ChangeSet_Dict._lru_size, 500)

            G.MEMORY_MODE = "low"
            gp.init_cs_dict()
            self.assertEqual(gp.ChangeSet_Dict._lru_size, 50)

            G.MEMORY_MODE = "very_low"
            gp.init_cs_dict()
            self.assertEqual(gp.ChangeSet_Dict._lru_size, 25)
        finally:
            G.MEMORY_MODE = orig_mode
            gp.init_cs_dict()

        # 3. Test LRU bypass on insert, decompress on-demand, LRU eviction, mutation persistence
        c_dict = CompressedChangeSetDict(lru_cache_size=3)
        c_dict["a"] = {"count": 1}
        c_dict["b"] = {"count": 2}
        c_dict["c"] = {"count": 3}
        self.assertEqual(len(c_dict), 3)
        self.assertEqual(len(c_dict._lru_cache), 0)  # Insert bypasses LRU cache

        # Accessing items brings them into LRU cache
        self.assertEqual(c_dict["a"], {"count": 1})
        self.assertEqual(len(c_dict._lru_cache), 1)
        self.assertIn("a", c_dict._lru_cache)

        self.assertEqual(c_dict["b"], {"count": 2})
        self.assertEqual(c_dict["c"], {"count": 3})
        self.assertEqual(len(c_dict._lru_cache), 3)

        # Inserting a new item does not populate LRU
        c_dict["d"] = {"count": 4}
        self.assertEqual(len(c_dict), 4)
        self.assertEqual(len(c_dict._lru_cache), 3)

        # Accessing 'd' exceeds LRU capacity -> evicts least recently accessed ('a')
        self.assertEqual(c_dict["d"], {"count": 4})
        self.assertEqual(len(c_dict._lru_cache), 3)
        self.assertNotIn("a", c_dict._lru_cache)

        # Access evicted item -> decompressed and brought back into LRU
        item_a = c_dict["a"]
        self.assertEqual(item_a, {"count": 1})
        self.assertIn("a", c_dict._lru_cache)

        # In-place mutation and eviction roundtrip
        item_a["count"] = 99
        # Evict 'a' again by accessing other items
        c_dict["e"] = {"count": 5}
        c_dict["f"] = {"count": 6}
        c_dict["g"] = {"count": 7}
        _ = c_dict["e"]
        _ = c_dict["f"]
        _ = c_dict["g"]
        self.assertNotIn("a", c_dict._lru_cache)

        # Access again to verify mutated state was persisted
        self.assertEqual(c_dict["a"]["count"], 99)
        self.assertEqual(c_dict.get("missing", "default_val"), "default_val")
        self.assertEqual(c_dict.pop("a")["count"], 99)
        self.assertNotIn("a", c_dict)

    def test_direct_table_engine_execution(self) -> None:
        """Verify parsing and ChangeSet execution with TEDirectDB."""
        item = {
            "file": "virt/kvm/iodev.h",
            "baseline_ast_ops": 227,
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
                if op[0] == m_tag_code.table_id:
                    tag_codes.append(op[2][1])
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

            # Function tag encloses the entire definition, while statements are not tagged as sub-tags
            func_tags = [t for t in tag_codes if "compute_metrics" in t]
            self.assertEqual(len(func_tags), 1)
            self.assertIn("int factor = 10;", func_tags[0])
            self.assertIn("notify_user", func_tags[0])
            factor_tags = [t for t in tag_codes if "int factor" in t and "compute_metrics" not in t]
            self.assertEqual(len(factor_tags), 0)
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

            tag_codes = [op[2][1] for op in cs.cs if op[0] == m_tag_code.table_id]
            # Function tag encloses the entire definition, parameters are not tagged as separate sub-tags
            func_tags = [t for t in tag_codes if "add_three" in t]
            self.assertEqual(len(func_tags), 1)
            self.assertIn("int a, int b, int c", func_tags[0])
            self.assertIn("return res;", func_tags[0])
            param_c_tags = [t for t in tag_codes if "int c" in t and "add_three" not in t]
            self.assertEqual(len(param_c_tags), 0)
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
            MockDB._global_store.clear()
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
                if op[0] == m_tag_code.table_id:
                    if len(op[2]) > 1 and op[2][1] is not None:
                        tag_codes.append(op[2][1])
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
            if G.TE:
                try:
                    G.TE.close()
                except Exception:
                    pass
            MockDB._global_store.clear()
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_struct_function_pointer_members(self) -> None:
        """Verify struct definitions with function pointers (e.g. struct nlmsvc_binding) produce exact member containers."""
        temp_dir = None
        try:
            MockDB._global_store.clear()
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

            file_path = "include/linux/lockd/bind.h"
            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            file_content = subprocess.check_output(
                ["git", "-C", "linux", "show", f"v3.0:{file_path}"],
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
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            from core.globalstuff import ASTT
            mock_containers = MockDB._global_store.get("m_ast_container", {})
            mock_asts = MockDB._global_store.get("m_ast", {})

            # Find nlmsvc_binding struct definition AST ID
            nlmsvc_ast_id = None
            for ast_row in mock_asts.values():
                if ast_row[1] == "nlmsvc_binding" and ast_row[2] == ASTT.C_structdecl:
                    nlmsvc_ast_id = ast_row[0]
                    break

            self.assertIsNotNone(nlmsvc_ast_id, "struct nlmsvc_binding C_structdecl not found in m_ast")

            # Must have exactly 2 m_ast_container entries: fopen and fclose
            nlmsvc_containers = [
                row for row in mock_containers.values() if row[0] == nlmsvc_ast_id
            ]
            nlmsvc_containers.sort(key=lambda r: r[1])
            self.assertEqual(len(nlmsvc_containers), 2, f"Expected 2 containers for nlmsvc_binding, got {len(nlmsvc_containers)}: {nlmsvc_containers}")

            ref_ast_0 = mock_asts.get(nlmsvc_containers[0][3])
            ref_ast_1 = mock_asts.get(nlmsvc_containers[1][3])
            self.assertIsNotNone(ref_ast_0)
            self.assertIsNotNone(ref_ast_1)
            self.assertEqual(ref_ast_0[1], "fopen")
            self.assertEqual(ref_ast_1[1], "fclose")

            # Check nlmclnt_initdata has exactly 6 containers
            initdata_ast_id = None
            for ast_row in mock_asts.values():
                if ast_row[1] == "nlmclnt_initdata" and ast_row[2] == ASTT.C_structdecl:
                    initdata_ast_id = ast_row[0]
                    break

            self.assertIsNotNone(initdata_ast_id, "struct nlmclnt_initdata C_structdecl not found in m_ast")
            initdata_containers = [
                row for row in mock_containers.values() if row[0] == initdata_ast_id
            ]
            initdata_containers.sort(key=lambda r: r[1])
            self.assertEqual(len(initdata_containers), 6, f"Expected 6 containers for nlmclnt_initdata, got {len(initdata_containers)}: {initdata_containers}")
            initdata_member_names = [mock_asts[r[3]][1] for r in initdata_containers]
            self.assertEqual(initdata_member_names, ["hostname", "address", "addrlen", "protocol", "nfs_version", "noresvport"])
        finally:
            if G.TE:
                try:
                    G.TE.close()
                except Exception:
                    pass
            MockDB._global_store.clear()
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_struct_typedef_members(self) -> None:
        """Verify struct definitions with multiple typedef members extract all fields into m_ast_container."""
        temp_dir = None
        try:
            MockDB._global_store.clear()
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

            file_path = "test_typedef_members.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
typedef unsigned long size_t;
typedef unsigned int u32;
typedef unsigned long long u64;

struct custom_data {
    size_t size;
    u32 flags;
    u64 offset;
    int status;
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
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            from core.globalstuff import ASTT
            mock_containers = MockDB._global_store.get("m_ast_container", {})
            mock_asts = MockDB._global_store.get("m_ast", {})

            struct_ast_id = None
            for ast_row in mock_asts.values():
                if ast_row[1] == "custom_data" and ast_row[2] == ASTT.C_structdecl:
                    struct_ast_id = ast_row[0]
                    break

            self.assertIsNotNone(struct_ast_id, "struct custom_data not found in m_ast")
            containers = [r for r in mock_containers.values() if r[0] == struct_ast_id]
            containers.sort(key=lambda r: r[1])
            self.assertEqual(len(containers), 4)
            names = [mock_asts[r[3]][1] for r in containers]
            self.assertEqual(names, ["size", "flags", "offset", "status"])
        finally:
            if G.TE:
                try:
                    G.TE.close()
                except Exception:
                    pass
            MockDB._global_store.clear()
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_typedef_definition_underlying_type_and_type_usage_refs(self) -> None:
        """Verify typedef definitions link underlying types in m_ast_container and record TypeUsage in m_symbol_ref."""
        temp_dir = None
        try:
            MockDB._global_store.clear()
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

            file_path = "test_typedef_chain.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
typedef unsigned int __u32;
typedef __u32 __be32;

struct nlmsvc_binding {
    __be32 (*fopen)(int arg);
};
"""
            with open(full_path, "w") as f:
                f.write(snippet.strip())

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            from core.globalstuff import ASTT, SymbolRole
            mock_containers = MockDB._global_store.get("m_ast_container", {})
            mock_asts = MockDB._global_store.get("m_ast", {})
            mock_sym_defs = MockDB._global_store.get("m_symbol_def", {})
            mock_sym_refs = MockDB._global_store.get("m_symbol_ref", {})

            # 1. __u32 AST & container
            u32_ast = next((row for row in mock_asts.values() if row[1] == "__u32" and row[2] == ASTT.C_SCtypedef), None)
            self.assertIsNotNone(u32_ast, "__u32 typedef not found in m_ast")
            u32_containers = [r for r in mock_containers.values() if r[0] == u32_ast[0]]
            self.assertGreater(len(u32_containers), 0, "__u32 must have m_ast_container record")
            u32_ref_ast_id = u32_containers[0][3]
            self.assertGreater(u32_ref_ast_id, 0, "__u32 container must link underlying type compound")
            underlying_compound = mock_asts.get(u32_ref_ast_id)
            self.assertEqual(underlying_compound[2], ASTT.C_Compound)

            # 2. __be32 AST & container pointing to __u32
            be32_ast = next((row for row in mock_asts.values() if row[1] == "__be32" and row[2] == ASTT.C_SCtypedef), None)
            self.assertIsNotNone(be32_ast, "__be32 typedef not found in m_ast")
            be32_containers = [r for r in mock_containers.values() if r[0] == be32_ast[0]]
            self.assertGreater(len(be32_containers), 0, "__be32 must have m_ast_container record")
            self.assertEqual(be32_containers[0][3], u32_ast[0], "__be32 container must link directly to __u32 ast_id")

            # 3. m_symbol_def has __u32 and __be32 with type_id = ASTT.C_SCtypedef
            u32_def = next((row for row in mock_sym_defs.values() if row[5] == "__u32" and row[6] == ASTT.C_SCtypedef), None)
            self.assertIsNotNone(u32_def, "__u32 not found in m_symbol_def")
            be32_def = next((row for row in mock_sym_defs.values() if row[5] == "__be32" and row[6] == ASTT.C_SCtypedef), None)
            self.assertIsNotNone(be32_def, "__be32 not found in m_symbol_def")

            # 4. m_symbol_ref has TypeUsage for __u32 and __be32
            u32_usage = next((row for row in mock_sym_refs.values() if row[4] == u32_ast[0] and row[5] == int(SymbolRole.TypeUsage)), None)
            self.assertIsNotNone(u32_usage, "__u32 TypeUsage not found in m_symbol_ref")
            be32_usage = next((row for row in mock_sym_refs.values() if row[4] == be32_ast[0] and row[5] == int(SymbolRole.TypeUsage)), None)
            self.assertIsNotNone(be32_usage, "__be32 TypeUsage not found in m_symbol_ref")
        finally:
            if G.TE:
                try:
                    G.TE.close()
                except Exception:
                    pass
            MockDB._global_store.clear()
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_tag_fidelity_sched_h(self) -> None:
        """Verify full tag text fidelity and 100% source code coverage on linux/include/linux/sched.h."""
        res = assert_file_tag_fidelity("include/linux/sched.h", min_coverage=1.0)
        self.assertEqual(res["mismatches_count"], 0)
        self.assertEqual(res["uncovered_non_ws"], 0)
        self.assertEqual(res["coverage_ratio"], 1.0)
        self.assertGreater(res["total_tags"], 1000)

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

    def test_struct_nlmsvc_binding_clean_ast(self) -> None:
        """Verify struct nlmsvc_binding and declarators contain zero type-0 or anonymous container rows."""
        temp_dir = None
        try:
            MockDB._global_store.clear()
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

            file_path = "include/linux/lockd/bind.h"
            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            file_content = subprocess.check_output(
                ["git", "-C", "linux", "show", f"v3.0:{file_path}"],
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
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            containers = list(MockDB._global_store.get("m_ast_container", {}).values())
            type_0_containers = [c for c in containers if c[2] == 0]
            self.assertEqual(
                len(type_0_containers),
                0,
                f"Found {len(type_0_containers)} type-0 containers in bind.h: {type_0_containers}",
            )

            asts = {r[0]: r for r in MockDB._global_store.get("m_ast", {}).values()}
            nlmsvc_ops_asts = [a for a in asts.values() if a[1] == "nlmsvc_ops"]
            self.assertTrue(bool(nlmsvc_ops_asts))
            nlmsvc_ops_containers = [c for c in containers if c[0] == nlmsvc_ops_asts[0][0]]
            # Should have exactly 2 valid containers: struct nlmsvc_binding and pointer (no trailing type 0)
            self.assertEqual(len(nlmsvc_ops_containers), 2)
            self.assertEqual(nlmsvc_ops_containers[0][2], ASTT.C_struct)
            self.assertEqual(nlmsvc_ops_containers[1][2], ASTT.C_pointer)
        finally:
            if G.TE:
                try:
                    G.TE.close()
                except Exception:
                    pass
            MockDB._global_store.clear()
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_function_proto_parameter_container_depths(self) -> None:
        """Verify function prototype parameters are linked in m_ast_container at depth 1 with function at depth 0."""
        from collections import defaultdict
        temp_dir = None
        try:
            MockDB._global_store.clear()
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

            file_path = "include/linux/lockd/bind.h"
            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            file_content = subprocess.check_output(
                ["git", "-C", "linux", "show", f"v3.0:{file_path}"],
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
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            asts = {r[0]: r for r in MockDB._global_store.get("m_ast", {}).values()}
            containers = list(MockDB._global_store.get("m_ast_container", {}).values())

            nlmclnt_proc_asts = [a for a in asts.values() if a[1] == "nlmclnt_proc"]
            self.assertTrue(bool(nlmclnt_proc_asts))
            proc_ast = nlmclnt_proc_asts[0]
            proc_id = proc_ast[0]

            proc_containers = sorted([c for c in containers if c[0] == proc_id], key=lambda x: x[1])
            self.assertGreaterEqual(len(proc_containers), 4)
            self.assertEqual(proc_containers[0][1], 0)
            self.assertEqual(proc_containers[0][2], ASTT.C_int)
            self.assertEqual(proc_containers[0][3], 0)

            param1 = proc_containers[1]
            param2 = proc_containers[2]
            param3 = proc_containers[3]

            self.assertEqual(asts[param1[3]][1], "host")
            self.assertEqual(asts[param2[3]][1], "cmd")
            self.assertEqual(asts[param3[3]][1], "fl")

            parent_to_children = defaultdict(list)
            child_to_parents = defaultdict(list)
            all_container_nodes = set()
            for c_row in containers:
                p_id, _, _, child_id = c_row
                all_container_nodes.add(p_id)
                if child_id and child_id != 0:
                    parent_to_children[p_id].append(child_id)
                    all_container_nodes.add(child_id)
                    child_to_parents[child_id].append(p_id)

            root_nodes = [nid for nid in all_container_nodes if nid in parent_to_children and not child_to_parents.get(nid)]
            ast_depth_map = {}
            queue = [(r_id, 0) for r_id in root_nodes]
            visited = set()
            while queue:
                curr_id, curr_depth = queue.pop(0)
                if curr_id in visited:
                    continue
                visited.add(curr_id)
                ast_depth_map[curr_id] = curr_depth
                for ch_id in parent_to_children.get(curr_id, []):
                    if ch_id and ch_id != 0 and ch_id not in visited:
                        queue.append((ch_id, curr_depth + 1))

            self.assertEqual(ast_depth_map.get(proc_id), 0)
            self.assertEqual(ast_depth_map.get(param1[3]), 1)
            self.assertEqual(ast_depth_map.get(param2[3]), 1)
            self.assertEqual(ast_depth_map.get(param3[3]), 1)

            host_containers = [c for c in containers if c[0] == param1[3] and c[3] != 0]
            self.assertTrue(bool(host_containers))
            nlm_host_ast_id = host_containers[0][3]
            self.assertEqual(ast_depth_map.get(nlm_host_ast_id), 2)
        finally:
            if G.TE:
                try:
                    G.TE.close()
                except Exception:
                    pass
            MockDB._global_store.clear()
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_function_proto_struct_return_container_depths(self) -> None:
        """Verify function prototypes returning struct pointers (nlmclnt_init) are linked at depth 0 with parameters at depth 1."""
        from collections import defaultdict
        temp_dir = None
        try:
            MockDB._global_store.clear()
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

            file_path = "include/linux/lockd/bind.h"
            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            file_content = subprocess.check_output(
                ["git", "-C", "linux", "show", f"v3.0:{file_path}"],
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
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            asts = {r[0]: r for r in MockDB._global_store.get("m_ast", {}).values()}
            containers = list(MockDB._global_store.get("m_ast_container", {}).values())

            nlmclnt_init_asts = [a for a in asts.values() if a[1] == "nlmclnt_init"]
            self.assertTrue(bool(nlmclnt_init_asts), "nlmclnt_init AST node not found")
            init_ast = nlmclnt_init_asts[0]
            init_id = init_ast[0]
            self.assertEqual(init_ast[2], ASTT.C_functionprotodecl)

            init_containers = sorted([c for c in containers if c[0] == init_id], key=lambda x: x[1])
            self.assertGreaterEqual(len(init_containers), 2)
            # Priority 0 is return type
            self.assertEqual(init_containers[0][1], 0)
            self.assertEqual(init_containers[0][2], ASTT.C_struct)
            # Priority 1 is parameter nlm_init
            self.assertEqual(init_containers[1][1], 1)
            self.assertEqual(init_containers[1][2], ASTT.C_Compound)
            param_init_id = init_containers[1][3]
            self.assertEqual(asts[param_init_id][1], "nlm_init")

            parent_to_children = defaultdict(list)
            child_to_parents = defaultdict(list)
            all_container_nodes = set()
            for c_row in containers:
                p_id, _, _, child_id = c_row
                all_container_nodes.add(p_id)
                if child_id and child_id != 0:
                    parent_to_children[p_id].append(child_id)
                    all_container_nodes.add(child_id)
                    child_to_parents[child_id].append(p_id)

            root_nodes = [nid for nid in all_container_nodes if nid in parent_to_children and not child_to_parents.get(nid)]
            ast_depth_map = {}
            queue = [(r_id, 0) for r_id in root_nodes]
            visited = set()
            while queue:
                curr_id, curr_depth = queue.pop(0)
                if curr_id in visited:
                    continue
                visited.add(curr_id)
                ast_depth_map[curr_id] = curr_depth
                for ch_id in parent_to_children.get(curr_id, []):
                    if ch_id and ch_id != 0 and ch_id not in visited:
                        queue.append((ch_id, curr_depth + 1))

            self.assertEqual(ast_depth_map.get(init_id), 0)
            self.assertEqual(ast_depth_map.get(param_init_id), 1)
        finally:
            if G.TE:
                try:
                    G.TE.close()
                except Exception:
                    pass
            MockDB._global_store.clear()
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_typedef_function_pointer_arguments(self) -> None:
        """Verify typedef function pointers correctly extract argument tags without phantom declarator tags."""
        temp_dir = None
        try:
            MockDB._global_store.clear()
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

            file_path = "test_typedef_fn.h"
            full_path = os.path.join(temp_dir, file_path)
            code = "typedef void (*nlm_host_match_fn_t)(struct nlm_host *host);\n"
            with open(full_path, "w") as f:
                f.write(code)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            db_tag = MockDB._global_store.get(m_tag.table_name, {})
            db_tag_code = MockDB._global_store.get(m_tag_code.table_name, {})
            db_bridge_tag = MockDB._global_store.get(m_bridge_tag.table_name, {})

            # Should have exactly 1 tag: the full typedef tag (argument is linked in m_ast_container, no phantom *nlm_host_match_fn_t tag)
            self.assertEqual(len(db_tag), 1)
            self.assertEqual(len(db_bridge_tag), 1)
            tags_code = [db_tag_code[t[3]][1] for t in db_tag.values()]
            self.assertIn("typedef void (*nlm_host_match_fn_t)(struct nlm_host *host);", tags_code)
            self.assertNotIn("*nlm_host_match_fn_t", tags_code)
            self.assertNotIn("struct nlm_host *host", tags_code)
        finally:
            if G.TE:
                try:
                    G.TE.close()
                except Exception:
                    pass
            MockDB._global_store.clear()
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

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

    def test_tag_lineage_struct_and_comment_modification(self) -> None:
        """Verify modifying a struct and comment records lineage in m_moved_tag and closes prior tags."""
        temp_dir_v1 = None
        temp_dir_v2 = None
        try:
            G.DEBUG_TYPECHECK = True
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            mf = MasterFile()
            temp_dir_v1 = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir_v1
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            file_path = "include/linux/test_binding.h"
            full_path_v1 = os.path.join(temp_dir_v1, file_path)
            os.makedirs(os.path.dirname(full_path_v1), exist_ok=True)
            snippet_v1 = """/* Initial lockd binding */
struct nlmsvc_binding {
    int id;
    void (*fclose)(void);
};
"""
            with open(full_path_v1, "w") as f:
                f.write(snippet_v1)

            cs1 = ChangeSet(f"A\t{file_path}")
            cs1.current_vid = 1
            cs1.gp = gp
            cs1.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs1, gp)
            cs1.parse()
            self.assertTrue(cs1.execute())
            G.TE.commit_all()

            # Record tags created in v1
            v1_tags = {row[0]: row for row in MockDB._global_store.get("m_tag", {}).values()}
            self.assertGreaterEqual(len(v1_tags), 2, "Expected at least comment and struct tags in v1")

            # --- Version 2: Modified struct and comment ---
            temp_dir_v2 = mf.create_temp_dir()
            mf.version_dict["v3.1"] = temp_dir_v2
            gp.Version_Name = "v3.1"
            gp.VID = 2
            gp.Old_VID = 1

            full_path_v2 = os.path.join(temp_dir_v2, file_path)
            os.makedirs(os.path.dirname(full_path_v2), exist_ok=True)
            snippet_v2 = """/* Updated lockd binding with flags */
struct nlmsvc_binding {
    int id;
    int flags;
    void (*fclose)(void);
};
"""
            with open(full_path_v2, "w") as f:
                f.write(snippet_v2)

            cs2 = ChangeSet(f"M\t{file_path}")
            cs2.current_vid = 2
            cs2.gp = gp
            cs2.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            cs2.store(m_file_name.get_set(None, cs2.current_path))
            with cs2(REF_OLD):
                cs2.store(m_bridge_file.view(
                    ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                    1,
                    cs2.ref(m_file_name.fnid, REF_ROOT),
                    None,
                    None,
                    cs2.current_path,
                ))
                cs2.store(m_file.update(cs2.ref(m_bridge_file.fid), None, 1, None, None, "M"))
            cs2.store(m_file.set(None, 2, 0, 1, "M", 0))
            cs2.store(m_bridge_file.set(2, cs2.ref(m_file_name.fnid), cs2.ref(m_file.fid)))

            cs2.parse()
            self.assertTrue(cs2.execute())
            G.TE.commit_all()

            # Verify m_moved_tag entries exist linking v1 tags to v2 tags
            moved_tags = MockDB._global_store.get("m_moved_tag", {})
            self.assertGreaterEqual(len(moved_tags), 2, f"Expected at least 2 m_moved_tag records, got {len(moved_tags)}")

            # Verify that each moved tag's s_tag_id belongs to v1 tags, and was marked closed (vid_e = 1)
            v2_tag_by_id = {}
            for (t_id, v_s), row in MockDB._global_store.get("m_tag", {}).items():
                v2_tag_by_id.setdefault(t_id, {})[v_s] = row

            for (s_tag_id, e_tag_id), row in moved_tags.items():
                self.assertIn(s_tag_id, v1_tags, f"Source tag {s_tag_id} should have been from v1")
                old_tag_row = v2_tag_by_id.get(s_tag_id, {}).get(1)
                self.assertIsNotNone(old_tag_row)
                self.assertEqual(old_tag_row[2], 1, f"Superseded tag {s_tag_id} should have vid_e = 1")
                new_tag_row = v2_tag_by_id.get(e_tag_id, {}).get(2)
                self.assertIsNotNone(new_tag_row)
                self.assertEqual(new_tag_row[1], 2, f"New tag {e_tag_id} should have vid_s = 2")
                self.assertEqual(new_tag_row[2], 0, f"New tag {e_tag_id} should have vid_e = 0")
        finally:
            if mf:
                mf.clear_all_version()

    def test_tag_lineage_unchanged_tags_recycled_without_movement(self) -> None:
        """Verify identical tags are recycled across versions without creating m_moved_tag records."""
        temp_dir_v1 = None
        temp_dir_v2 = None
        try:
            G.DEBUG_TYPECHECK = True
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            mf = MasterFile()
            temp_dir_v1 = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir_v1
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            file_path = "include/linux/test_recycle.h"
            full_path_v1 = os.path.join(temp_dir_v1, file_path)
            os.makedirs(os.path.dirname(full_path_v1), exist_ok=True)
            snippet_v1 = """/* Constant config */
struct config_static {
    int mode;
};

struct config_dynamic {
    int threshold;
};
"""
            with open(full_path_v1, "w") as f:
                f.write(snippet_v1)

            cs1 = ChangeSet(f"A\t{file_path}")
            cs1.current_vid = 1
            cs1.gp = gp
            cs1.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            default_processing(cs1, gp)
            cs1.parse()
            self.assertTrue(cs1.execute())
            G.TE.commit_all()

            # --- Version 2: Only config_dynamic modified, config_static & comment unchanged ---
            temp_dir_v2 = mf.create_temp_dir()
            mf.version_dict["v3.1"] = temp_dir_v2
            gp.Version_Name = "v3.1"
            gp.VID = 2
            gp.Old_VID = 1

            full_path_v2 = os.path.join(temp_dir_v2, file_path)
            os.makedirs(os.path.dirname(full_path_v2), exist_ok=True)
            snippet_v2 = """/* Constant config */
struct config_static {
    int mode;
};

struct config_dynamic {
    int threshold;
    int max_limit;
};
"""
            with open(full_path_v2, "w") as f:
                f.write(snippet_v2)

            cs2 = ChangeSet(f"M\t{file_path}")
            cs2.current_vid = 2
            cs2.gp = gp
            cs2.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            cs2.store(m_file_name.get_set(None, cs2.current_path))
            with cs2(REF_OLD):
                cs2.store(m_bridge_file.view(
                    ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                    1,
                    cs2.ref(m_file_name.fnid, REF_ROOT),
                    None,
                    None,
                    cs2.current_path,
                ))
                cs2.store(m_file.update(cs2.ref(m_bridge_file.fid), None, 1, None, None, "M"))
            cs2.store(m_file.set(None, 2, 0, 1, "M", 0))
            cs2.store(m_bridge_file.set(2, cs2.ref(m_file_name.fnid), cs2.ref(m_file.fid)))

            cs2.parse()
            self.assertTrue(cs2.execute())
            G.TE.commit_all()

            # Verify m_moved_tag: unchanged field threshold is recycled cleanly without moving; only config_dynamic moved
            expected_moved = 1
            moved_tags = MockDB._global_store.get("m_moved_tag", {})
            self.assertEqual(len(moved_tags), expected_moved, f"Expected exactly {expected_moved} moved tags, got {len(moved_tags)}")
            moved_s_ids = {s_id for (s_id, e_id) in moved_tags}
            # Tags 1 (comment), 2 (mode), 3 (config_static) should NOT be moved
            self.assertNotIn(1, moved_s_ids, "Comment tag must not be moved")
            self.assertNotIn(2, moved_s_ids, "config_static field mode must not be moved")
            self.assertNotIn(3, moved_s_ids, "config_static struct must not be moved")
        finally:
            if mf:
                mf.clear_all_version()

    def test_anonymous_enum_relative_path(self) -> None:
        """Verify anonymous enums/structs strip /dev/shm full paths down to git relative paths."""
        mf = None
        try:
            MockDB._global_store.clear()
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            file_path = "drivers/net/test_anon_enum.c"
            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            snippet = """enum {
    FLAG_A = 1,
    FLAG_B = 2
};

struct anon_container {
    enum {
        SUB_A,
        SUB_B
    } flag;
};
"""
            with open(full_path, "w") as f:
                f.write(snippet)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            cs.store(m_file_name.get_set(None, cs.current_path))
            cs.store(m_file.set(None, 1, 0, 0, "A", 0))
            cs.store(m_bridge_file.set(1, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

            cs.parse()
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            # Verify that none of the m_ast or m_tag entries contain '/dev/shm'
            ast_store = MockDB._global_store.get("m_ast", {})
            for ast_id, row in ast_store.items():
                ast_name = row[1]
                if ast_name:
                    self.assertNotIn("/dev/shm", ast_name, f"m_ast.name '{ast_name}' must not contain /dev/shm")
                    if "(unnamed at " in ast_name:
                        self.assertIn("drivers/net/test_anon_enum.c", ast_name, f"m_ast.name '{ast_name}' should contain relative path")

            tag_store = MockDB._global_store.get("m_tag", {})
            for tag_id, row in tag_store.items():
                tag_name = row[5]
                if tag_name:
                    self.assertNotIn("/dev/shm", tag_name, f"m_tag.name '{tag_name}' must not contain /dev/shm")
        finally:
            if mf:
                mf.clear_all_version()

    def test_function_signature_and_body_container_hierarchy(self) -> None:
        """Verify priority 0 (return type), priority 1..N (arguments), and priority N+1 (compound statement body)."""
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

            file_path = "test_fn_hierarchy.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
struct payload {
    int id;
};

int calculate_payload(struct payload *p, int factor) {
    if (p != 0) {
        return p->id * factor;
    }
    return 0;
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
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            ast_store = MockDB._global_store.get("m_ast", {})
            container_store = MockDB._global_store.get("m_ast_container", {})

            # Find function AST for calculate_payload
            fn_ast_id = None
            for aid, row in ast_store.items():
                if row[1] == "calculate_payload" and row[2] in (ASTT.C_functionproto, ASTT.C_functionprotodecl):
                    fn_ast_id = aid
                    break

            self.assertIsNotNone(fn_ast_id, "calculate_payload function AST not found")

            # Get container entries for fn_ast_id sorted by priority
            fn_containers = [
                row for row in container_store.values() if row[0] == fn_ast_id
            ]
            fn_containers.sort(key=lambda r: r[1])

            # Priority 0: return type (int)
            self.assertTrue(len(fn_containers) >= 4, f"Expected at least 4 containers for function, got {len(fn_containers)}")
            self.assertEqual(fn_containers[0][1], 0, "Priority 0 should be return type")
            self.assertEqual(fn_containers[0][2], ASTT.C_int)

            # Priority 1: first param (p)
            self.assertEqual(fn_containers[1][1], 1, "Priority 1 should be first param")
            param1_ast = ast_store.get(fn_containers[1][3])
            self.assertIsNotNone(param1_ast)
            self.assertEqual(param1_ast[1], "p")

            # Priority 2: second param (factor)
            self.assertEqual(fn_containers[2][1], 2, "Priority 2 should be second param")
            param2_ast = ast_store.get(fn_containers[2][3])
            self.assertIsNotNone(param2_ast)
            self.assertEqual(param2_ast[1], "factor")

            # Priority 3: compound statement body
            self.assertEqual(fn_containers[3][1], 3, "Priority 3 should be compound statement body")
            self.assertEqual(fn_containers[3][2], ASTT.C_CompoundStmt)
            body_ast_id = fn_containers[3][3]

            # Inspect body containers
            body_containers = [
                row for row in container_store.values() if row[0] == body_ast_id
            ]
            self.assertGreater(len(body_containers), 0, "Compound statement body should have child container records")
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_function_types_used_detection(self) -> None:
        """Verify types used within a function body are detected and linked in m_ast_container with C_TypeRef."""
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

            file_path = "test_types_used.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
struct device {
    int id;
};

struct context {
    struct device *dev;
};

void run_device(struct context *ctx) {
    struct device *d = ctx->dev;
    d->id = 42;
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
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            ast_store = MockDB._global_store.get("m_ast", {})
            container_store = MockDB._global_store.get("m_ast_container", {})

            # Find run_device function
            fn_ast_id = None
            for aid, row in ast_store.items():
                if row[1] == "run_device":
                    fn_ast_id = aid
                    break

            self.assertIsNotNone(fn_ast_id)
            fn_body_row = next(
                (row for row in container_store.values() if row[0] == fn_ast_id and row[2] == ASTT.C_CompoundStmt),
                None,
            )
            self.assertIsNotNone(fn_body_row, "Function body container link must exist")
            body_ast_id = fn_body_row[3]

            # Find used types linked under body_ast_id with C_TypeRef
            used_type_refs = [
                row for row in container_store.values()
                if row[0] == body_ast_id and row[2] == ASTT.C_TypeRef
            ]
            self.assertGreater(len(used_type_refs), 0, "Body should have C_TypeRef records")

            used_names = {
                ast_store[row[3]][1] for row in used_type_refs if row[3] in ast_store
            }
            self.assertIn("device", used_names, "Type 'device' must be detected as used in run_device")
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_multiple_identical_function_signatures_no_duplicate_container(self) -> None:
        """Verify multiple files defining identical function signatures (e.g. static void usage(void)) do not collide in m_ast_container."""
        temp_dir = None
        try:
            MockDB._global_store.clear()
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

            file_a = "driver_a.c"
            full_a = os.path.join(temp_dir, file_a)
            with open(full_a, "w") as f:
                f.write("static void usage(void) {\n    int a = 1;\n}\n")

            file_b = "driver_b.c"
            full_b = os.path.join(temp_dir, file_b)
            with open(full_b, "w") as f:
                f.write("static void usage(void) {\n    int b = 2;\n}\n")

            cs_a = ChangeSet(f"A\t{file_a}")
            cs_a.current_vid = 1
            cs_a.gp = gp
            cs_a.mf = mf
            G.CURRENT_PARSING_FILE = file_a
            default_processing(cs_a, gp)
            cs_a.parse()
            self.assertTrue(cs_a.execute())

            cs_b = ChangeSet(f"A\t{file_b}")
            cs_b.current_vid = 1
            cs_b.gp = gp
            cs_b.mf = mf
            G.CURRENT_PARSING_FILE = file_b
            default_processing(cs_b, gp)
            cs_b.parse()
            self.assertTrue(cs_b.execute())

            G.TE.commit_all()

            ast_store = MockDB._global_store.get("m_ast", {})
            container_store = MockDB._global_store.get("m_ast_container", {})

            # Locate all AST nodes for 'usage' with functionprotodecl
            usage_nodes = [
                aid for aid, row in ast_store.items()
                if row[1] == "usage" and row[2] == ASTT.C_functionprotodecl
            ]
            self.assertGreaterEqual(len(usage_nodes), 1, "Expected at least 1 usage AST node")

            # Check that container rows for each usage node have distinct priorities and valid compound body
            for u_node in usage_nodes:
                u_containers = sorted(
                    [row for row in container_store.values() if row[0] == u_node],
                    key=lambda r: r[1],
                )
                self.assertGreaterEqual(len(u_containers), 2)
                self.assertEqual(u_containers[0][1], 0)  # Priority 0: return type
                self.assertEqual(u_containers[0][2], ASTT.C_void)
                self.assertEqual(u_containers[1][1], 1)  # Priority 1: compound body
                self.assertEqual(u_containers[1][2], ASTT.C_CompoundStmt)

            # Ensure absolutely no duplicate (ast_id, priority) pairs exist across all containers
            pk_set = set()
            for row in container_store.values():
                pk = (row[0], row[1])
                self.assertNotIn(pk, pk_set, f"Duplicate primary key in m_ast_container: {pk}")
                pk_set.add(pk)
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_standard_c_keywords_mapping(self) -> None:
        """Verify STANDARD_C_KEYWORDS maps all standard C keywords to distinct ASTT tokens."""
        from core.globalstuff import STANDARD_C_KEYWORDS, ASTT
        expected_keywords = [
            "if", "switch", "case", "default", "while", "do", "for", "return",
            "break", "continue", "goto", "asm", "sizeof", "auto", "register",
            "static", "extern", "typedef", "const", "volatile", "restrict",
            "inline", "void", "char", "short", "int", "long", "signed",
            "unsigned", "float", "double", "struct", "union", "enum"
        ]
        for kw in expected_keywords:
            self.assertIn(kw, STANDARD_C_KEYWORDS, f"Keyword '{kw}' must be in STANDARD_C_KEYWORDS")
            self.assertIsInstance(STANDARD_C_KEYWORDS[kw], ASTT)

    def test_assembly_preprocessor_conditionals_scope_resolution(self) -> None:
        """Verify assembly parser correctly resolves #ifdef/#else/#endif scopes without NoneType errors."""
        temp_dir = None
        try:
            import tempfile
            from parser.asm_ast import asm_ast_parse
            temp_dir = tempfile.mkdtemp(dir="/dev/shm")
            mf = MasterFile()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf

            gp = GreatProcessor()
            gp.Version_Name = "v3.0"
            gp.VID = 1
            init_db_layout(gp)
            G.TE = get_table_engine("cached")()
            G.TE.start(gp.Table_Array, MockDB)

            asm_snippet = """/* Assembly with nested conditionals */
#ifdef CONFIG_PPC64
.globl my_func
my_func:
    nop
#else
.globl my_func_32
my_func_32:
    blr
#endif
"""
            target_path = "arch/powerpc/test_conditional.S"
            full_path = os.path.join(temp_dir, target_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(asm_snippet)

            cs = ChangeSet(f"A\t{target_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = target_path

            default_processing(cs, gp)
            asm_ast_parse(cs)
            self.assertTrue(cs.execute())
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_label_stmt_name_extraction(self) -> None:
        """Verify C_LabelStmt extracts and preserves the actual label identifier name (e.g. 'err_out')."""
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

            file_path = "test_label.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
int compute(int val) {
    if (val < 0)
        goto err_out;
    return val * 2;
err_out:
    return -1;
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
            G.TE.commit_all()

            asts = MockDB._global_store.get("m_ast", {})
            label_asts = [v for v in asts.values() if len(v) >= 3 and v[2] == ASTT.C_LabelStmt]
            self.assertGreater(len(label_asts), 0, "Expected at least one C_LabelStmt")
            label_names = [v[1] for v in label_asts]
            self.assertIn("err_out", label_names, f"Expected 'err_out' in label names, got: {label_names}")
            for name in label_names:
                self.assertNotEqual(name, "label", "Label name must not be placeholder 'label'")
                self.assertNotEqual(name, "return", "Label name must not be overwritten by subsequent keyword 'return'")
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_struct_designated_initializer_extraction(self) -> None:
        """Verify struct designated initializers parse C_InitListExpr, members, and value refs."""
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

            file_path = "test_designated_init.c"
            full_path = os.path.join(temp_dir, file_path)
            snippet = """
struct xattr_handler {
    const char *prefix;
    int flags;
    int (*get)(void);
    int (*set)(void);
};

int btrfs_xattr_acl_get(void) { return 0; }
int btrfs_xattr_acl_set(void) { return 0; }

const struct xattr_handler btrfs_xattr_acl_access_handler = {
    .prefix = "posix_acl_access",
    .flags  = 1,
    .get    = btrfs_xattr_acl_get,
    .set    = btrfs_xattr_acl_set,
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
            G.TE.commit_all()

            asts = MockDB._global_store.get("m_ast", {})
            containers = MockDB._global_store.get("m_ast_container", {})

            # 1. Verify btrfs_xattr_acl_access_handler exists
            handler_ast = None
            for aid, arow in asts.items():
                if arow[1] == "btrfs_xattr_acl_access_handler":
                    handler_ast = (aid, arow)
                    break
            self.assertIsNotNone(handler_ast, "btrfs_xattr_acl_access_handler AST not found")
            handler_id = handler_ast[0]

            # 2. Verify handler container links to C_InitListExpr
            init_list_ast = None
            for crow in containers.values():
                if crow[0] == handler_id:
                    ref_id = crow[3]
                    if ref_id in asts and asts[ref_id][2] == ASTT.C_InitListExpr:
                        init_list_ast = (ref_id, asts[ref_id])
                        break
            self.assertIsNotNone(init_list_ast, "C_InitListExpr not linked in btrfs_xattr_acl_access_handler container")
            init_list_id = init_list_ast[0]

            # 3. Verify C_InitListExpr links to members: prefix, flags, get, set
            member_names = set()
            value_names = set()
            for crow in containers.values():
                if crow[0] == init_list_id:
                    ref_id = crow[3]
                    if ref_id in asts:
                        if asts[ref_id][2] == ASTT.C_MemberRefExpr:
                            member_names.add(asts[ref_id][1])
                        elif asts[ref_id][2] == ASTT.C_DeclRefExpr:
                            value_names.add(asts[ref_id][1])
            self.assertEqual(member_names, {"prefix", "flags", "get", "set"}, f"Expected all designated fields, got {member_names}")
            self.assertIn("btrfs_xattr_acl_get", value_names)
            self.assertIn("btrfs_xattr_acl_set", value_names)
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_struct_designated_initializer_canonical_tag(self) -> None:
        """Verify struct variable declarations with designated initializers produce exactly 1 tag covering the complete statement."""
        temp_dir = None
        try:
            MockDB._global_store.clear()
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

            file_path = "fs/9p/acl.c"
            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            snippet = """const struct xattr_handler v9fs_xattr_acl_access_handler = {
\t.prefix\t= POSIX_ACL_XATTR_ACCESS,
\t.flags\t= ACL_TYPE_ACCESS,
\t.get\t= v9fs_xattr_get_acl,
\t.set\t= v9fs_xattr_set_acl,
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
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            db_tag = MockDB._global_store.get(m_tag.table_name, {})
            db_tag_code = MockDB._global_store.get(m_tag_code.table_name, {})
            db_ast = MockDB._global_store.get(m_ast.table_name, {})
            db_cont = MockDB._global_store.get(m_ast_container.table_name, {})

            # Exactly 1 tag produced for the complete struct declaration
            self.assertEqual(len(db_tag), 1, f"Expected 1 tag for struct declaration, got {len(db_tag)}")
            trow = next(iter(db_tag.values()))
            tag_code = db_tag_code[trow[3]][1]
            self.assertTrue(tag_code.startswith("const struct xattr_handler v9fs_xattr_acl_access_handler"))
            self.assertTrue(tag_code.rstrip().endswith("};"))

            # All inner members and values linked under C_InitListExpr in m_ast_container
            handler_ast_id = trow[4]
            self.assertEqual(db_ast[handler_ast_id][1], "v9fs_xattr_acl_access_handler")

            init_list_id = None
            for crow in db_cont.values():
                if crow[0] == handler_ast_id and crow[2] == ASTT.C_InitListExpr:
                    init_list_id = crow[3]
                    break
            self.assertIsNotNone(init_list_id, "C_InitListExpr not found in handler container")

            member_names = {db_ast[crow[3]][1] for crow in db_cont.values() if crow[0] == init_list_id and crow[2] == ASTT.C_MemberRefExpr}
            val_names = {db_ast[crow[3]][1] for crow in db_cont.values() if crow[0] == init_list_id and crow[2] == ASTT.C_DeclRefExpr}
            self.assertEqual(member_names, {"prefix", "flags", "get", "set"})
            self.assertEqual(val_names, {"POSIX_ACL_XATTR_ACCESS", "ACL_TYPE_ACCESS", "v9fs_xattr_get_acl", "v9fs_xattr_set_acl"})
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
            if G.TE:
                try:
                    G.TE.close()
                except Exception:
                    pass

    def test_include_target_path_normalization(self) -> None:
        """Verify CPPro_include normalizes included file paths to clean relative repo paths without /dev/shm/code-parser."""
        temp_dir = None
        try:
            MockDB._global_store.clear()
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

            # Create header inside include/linux
            hdr_dir = os.path.join(temp_dir, "include", "linux")
            os.makedirs(hdr_dir, exist_ok=True)
            with open(os.path.join(hdr_dir, "my_test_hdr.h"), "w") as f:
                f.write("#define MY_TEST_MACRO 123\n")

            # Create C file that includes it
            c_dir = os.path.join(temp_dir, "drivers", "sample")
            os.makedirs(c_dir, exist_ok=True)
            c_file_path = "drivers/sample/main.c"
            with open(os.path.join(temp_dir, c_file_path), "w") as f:
                f.write('#include <linux/my_test_hdr.h>\nint foo = MY_TEST_MACRO;\n')

            cs = ChangeSet(f"A\t{c_file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = c_file_path

            default_processing(cs, gp)
            cs.parse()
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            db_fn = MockDB._global_store.get(m_file_name.table_name, {})
            fnames = [row[1] for row in db_fn.values()]

            # Must contain the clean relative path
            self.assertIn("include/linux/my_test_hdr.h", fnames)
            # Must NOT contain any /dev/shm or code-parser prefix
            for fn in fnames:
                self.assertNotIn("code-parser", fn)
                self.assertFalse(fn.startswith("/dev/shm"))
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
            if G.TE:
                try:
                    G.TE.close()
                except Exception:
                    pass

    def test_enum_constant_definition_staging(self) -> None:
        """Verify that named and anonymous enum constants are staged into m_symbol_def with type C_enumequal."""
        mf = None
        try:
            MockDB._global_store.clear()
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            file_path = "drivers/net/test_enum_defs.c"
            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            snippet = """enum my_color {
    COLOR_RED = 1,
    COLOR_GREEN,
    COLOR_BLUE
};

enum {
    FLAG_A = 10,
    FLAG_B
};
"""
            with open(full_path, "w") as f:
                f.write(snippet)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            cs.store(m_file_name.get_set(None, cs.current_path))
            cs.store(m_file.set(None, 1, 0, 1, "A", 0))
            cs.store(m_bridge_file.set(1, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

            cs.parse()
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            sym_defs = MockDB._global_store.get("m_symbol_def", {})
            defs_by_name = {row[5]: row for row in sym_defs.values()}

            # 1. Named enum definition exists
            self.assertIn("my_color", defs_by_name)
            self.assertEqual(defs_by_name["my_color"][6], int(ASTT.C_enumdecl))

            # 2. Enumerator constants are staged with C_enumequal
            for const_name in ("COLOR_RED", "COLOR_GREEN", "COLOR_BLUE", "FLAG_A", "FLAG_B"):
                self.assertIn(const_name, defs_by_name, f"{const_name} must be staged in m_symbol_def")
                self.assertEqual(defs_by_name[const_name][6], int(ASTT.C_enumequal))

            # 3. Exact line numbers for each enumerator
            self.assertEqual(defs_by_name["COLOR_RED"][7], 2)
            self.assertEqual(defs_by_name["COLOR_GREEN"][7], 3)
            self.assertEqual(defs_by_name["COLOR_BLUE"][7], 4)
            self.assertEqual(defs_by_name["FLAG_A"][7], 8)
            self.assertEqual(defs_by_name["FLAG_B"][7], 9)

            # 4. Anonymous enum container ('(unnamed at ...)') is NOT staged in m_symbol_def
            for row in sym_defs.values():
                s_name = row[5]
                self.assertNotIn("(unnamed at ", s_name)
                self.assertNotIn("(anonymous at ", s_name)
        finally:
            if mf:
                mf.clear_all_version()

    def test_enum_constant_usage_staging(self) -> None:
        """Verify that enum constant usages in statements are staged in m_symbol_ref as DeclRef."""
        mf = None
        try:
            MockDB._global_store.clear()
            G.DB = MockDB
            G.TE = get_table_engine("cached")()
            gp = GreatProcessor()
            init_db_layout(gp)
            G.TE.start(gp.Table_Array, G.DB)

            file_path = "drivers/net/test_enum_refs.c"
            mf = MasterFile()
            temp_dir = mf.create_temp_dir()
            mf.version_dict["v3.0"] = temp_dir
            G.MF = mf
            gp.Version_Name = "v3.0"
            gp.VID = 1

            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            snippet = """enum my_state {
    STATE_IDLE = 0,
    STATE_RUNNING = 1,
};

int handle_state(enum my_state s) {
    if (s == STATE_IDLE)
        return STATE_RUNNING;
    return 0;
}
"""
            with open(full_path, "w") as f:
                f.write(snippet)

            cs = ChangeSet(f"A\t{file_path}")
            cs.current_vid = 1
            cs.gp = gp
            cs.mf = mf
            G.CURRENT_PARSING_FILE = file_path

            cs.store(m_file_name.get_set(None, cs.current_path))
            cs.store(m_file.set(None, 1, 0, 1, "A", 0))
            cs.store(m_bridge_file.set(1, cs.ref(m_file_name.fnid), cs.ref(m_file.fid)))

            cs.parse()
            self.assertTrue(cs.execute())
            G.TE.commit_all()

            from core.globalstuff import SymbolRole
            sym_defs = MockDB._global_store.get("m_symbol_def", {})
            defs_by_name = {row[5]: row for row in sym_defs.values()}
            idle_ast_id = defs_by_name["STATE_IDLE"][4]
            running_ast_id = defs_by_name["STATE_RUNNING"][4]

            sym_refs = MockDB._global_store.get("m_symbol_ref", {})
            decl_refs = [r for r in sym_refs.values() if r[5] == int(SymbolRole.DeclRef)]

            idle_refs = [r for r in decl_refs if r[4] == idle_ast_id]
            running_refs = [r for r in decl_refs if r[4] == running_ast_id]

            self.assertTrue(len(idle_refs) >= 1, "STATE_IDLE must be referenced in m_symbol_ref as DeclRef")
            self.assertTrue(len(running_refs) >= 1, "STATE_RUNNING must be referenced in m_symbol_ref as DeclRef")
            self.assertEqual(idle_refs[0][6], 7)
            self.assertEqual(running_refs[0][6], 8)
        finally:
            if mf:
                mf.clear_all_version()

    def test_enum_constant_cross_file_reference(self) -> None:
        """Verify cross-file enum constant reference resolution via REF_FILE."""
        mf = None
        try:
            MockDB._global_store.clear()
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

            header_path = "include/linux/test_status.h"
            driver_path = "drivers/net/test_driver.c"

            os.makedirs(os.path.dirname(os.path.join(temp_dir, header_path)), exist_ok=True)
            os.makedirs(os.path.dirname(os.path.join(temp_dir, driver_path)), exist_ok=True)

            header_code = """#ifndef _TEST_STATUS_H
#define _TEST_STATUS_H
enum net_status {
    NET_DOWN = 0,
    NET_UP = 1,
};
#endif
"""
            driver_code = """#include <linux/test_status.h>

int check_link(int up) {
    if (up)
        return NET_UP;
    return NET_DOWN;
}
"""
            with open(os.path.join(temp_dir, header_path), "w") as f:
                f.write(header_code)
            with open(os.path.join(temp_dir, driver_path), "w") as f:
                f.write(driver_code)

            cs_h = ChangeSet(f"A\t{header_path}")
            cs_h.current_vid = 1
            cs_h.gp = gp
            cs_h.mf = mf
            G.CURRENT_PARSING_FILE = header_path
            cs_h.store(m_file_name.get_set(None, cs_h.current_path))
            cs_h.store(m_file.set(None, 1, 0, 1, "A", 0))
            cs_h.store(m_bridge_file.set(1, cs_h.ref(m_file_name.fnid), cs_h.ref(m_file.fid)))
            cs_h.parse()
            self.assertTrue(cs_h.execute())

            cs_d = ChangeSet(f"A\t{driver_path}")
            cs_d.current_vid = 1
            cs_d.gp = gp
            cs_d.mf = mf
            cs_d.batch_cs_dict = {header_path: cs_h}
            G.CURRENT_PARSING_FILE = driver_path
            cs_d.store(m_file_name.get_set(None, cs_d.current_path))
            cs_d.store(m_file.set(None, 1, 0, 1, "A", 0))
            cs_d.store(m_bridge_file.set(1, cs_d.ref(m_file_name.fnid), cs_d.ref(m_file.fid)))
            cs_d.parse()
            self.assertTrue(cs_d.execute())

            G.TE.commit_all()

            from core.globalstuff import SymbolRole
            sym_defs = MockDB._global_store.get("m_symbol_def", {})
            defs_by_name = {row[5]: row for row in sym_defs.values()}
            self.assertIn("NET_UP", defs_by_name)
            self.assertIn("NET_DOWN", defs_by_name)

            net_up_ast_id = defs_by_name["NET_UP"][4]
            net_down_ast_id = defs_by_name["NET_DOWN"][4]

            sym_refs = MockDB._global_store.get("m_symbol_ref", {})
            driver_refs = [row for row in sym_refs.values() if row[5] == int(SymbolRole.DeclRef)]

            up_refs = [r for r in driver_refs if r[4] == net_up_ast_id]
            down_refs = [r for r in driver_refs if r[4] == net_down_ast_id]

            self.assertTrue(len(up_refs) >= 1, "NET_UP reference should be resolved across files")
            self.assertTrue(len(down_refs) >= 1, "NET_DOWN reference should be resolved across files")
        finally:
            if mf:
                mf.clear_all_version()



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
        mock_tag_codes = MockDB._global_store.get("m_tag_code", {})
        mock_bridge = MockDB._global_store.get("m_bridge_tag", {})
        tag_map = {row[0]: row for row in mock_tags.values()}
        code_map = {row[0]: row[1] for row in mock_tag_codes.values()}

        mismatches: list[dict[str, Any]] = []
        zero_extent_tags = 0
        covered_char_mask = [[False] * len(line) for line in raw_lines]

        for b_pk, b_row in mock_bridge.items():
            fid, tag_id, line_s, line_e, char_s, char_e = b_row
            tag_row = tag_map.get(tag_id)
            if not tag_row:
                continue
            tag_hash = tag_row[3]
            tag_code = code_map.get(tag_hash, "")

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



TABLE_NAME_BY_ID: dict[int, str] = {t.table_id: t.table_name for t in TABLES}

OP_CODE_NAMES: dict[int, str] = {
    0: "OP_DONE",
    1: "OP_SET",
    2: "OP_UPDATE",
    3: "OP_REF",
    4: "OP_REF_VIEW",
    5: "OP_VIEW_DONE",
    6: "OP_VIEW_SET",
}


def get_ast_type_name(type_id: int | None) -> str:
    """Resolve ASTT integer constant to its symbolic identifier name."""
    if type_id is None:
        return "None"
    try:
        return ASTT(type_id).name
    except Exception:
        return f"type_{type_id}"


def format_joins_desc(joins: tuple) -> str:
    """Format relational join tuples into a readable string."""
    parts = []
    for j in joins:
        if isinstance(j, tuple) and len(j) == 3:
            (t1, c1), (t2, c2), rep = j
            n1 = TABLE_NAME_BY_ID.get(t1, f"t{t1}")
            n2 = TABLE_NAME_BY_ID.get(t2, f"t{t2}")
            rep_str = f" [x{rep}]" if rep > 1 else ""
            parts.append(f"{n1}[col_{c1}] ⋈ {n2}[col_{c2}]{rep_str}")
        elif isinstance(j, tuple) and len(j) == 1 and isinstance(j[0], tuple):
            t1, c1 = j[0]
            n1 = TABLE_NAME_BY_ID.get(t1, f"t{t1}")
            parts.append(f"{n1}[col_{c1}]")
        else:
            parts.append(str(j))
    return f"VIEW({', '.join(parts)})"


def format_cell_val(val: Any) -> str:
    """Format a single data value or reference for CLI table display."""
    if val is None:
        return "NULL"
    if isinstance(val, tuple) and len(val) == 3 and val[1] == 3:  # RefType: (query, OP_REF, route)
        ptr, _, route = val
        t_id, c_idx = ptr if isinstance(ptr, tuple) and len(ptr) == 2 else (None, None)
        t_name = TABLE_NAME_BY_ID.get(t_id, f"t{t_id}") if t_id is not None else "?"
        if isinstance(route, (list, tuple)) and len(route) >= 2 and route[0] == 2:  # REF_POS
            route_str = f"POS:{route[1]}"
        elif isinstance(route, (list, tuple)) and len(route) >= 1 and route[0] == 0:  # REF_ROOT
            route_str = "ROOT"
        elif isinstance(route, (list, tuple)) and len(route) >= 1 and route[0] == 6:  # REF_NO_REF
            route_str = "NO_REF"
        else:
            route_str = str(route)
        return f"REF({t_name}[{c_idx}]@{route_str})"
    if isinstance(val, bytes):
        if len(val) == 32:
            return f"0x{val[:4].hex()}..{val[-2:].hex()}"
        return f"0x{val.hex()[:10]}..({len(val)}B)"
    if isinstance(val, str):
        val_clean = val.replace("\n", "\\n").replace("\t", "\\t")
        if len(val_clean) > 30:
            return repr(val_clean[:27] + "...")
        return repr(val_clean)
    if isinstance(val, int):
        return str(val)
    return str(val)


def format_operations_table(staged_ops: list[Any], result_ops: list[Any]) -> str:
    """Format staged ChangeSet operations and executed results in a clean table."""
    lines = [
        COLOR.cyan("\n" + "=" * 115),
        COLOR.cyan("                          STAGED & RESOLVED DATABASE OPERATIONS (ChangeSet.cs)"),
        COLOR.cyan("=" * 115),
        f"{'Op #':<5} | {'Target Table / View':<30} | {'OpCode':<12} | {'Staged Parameters / Data':<36} | {'Result Row'}",
        COLOR.cyan("-" * 115),
    ]

    for idx, op in enumerate(staged_ops):
        if not (isinstance(op, tuple) and len(op) >= 3):
            lines.append(f"#{idx:<4} | {str(op)}")
            continue
        target, op_code, data = op[0], op[1], op[2]

        # Target name
        if isinstance(target, int):
            target_str = TABLE_NAME_BY_ID.get(target, f"table_{target}")
        elif isinstance(target, tuple):
            target_str = format_joins_desc(target)
            if len(target_str) > 30:
                target_str = target_str[:27] + "..."
        else:
            target_str = str(target)[:30]

        # OpCode name
        op_code_str = OP_CODE_NAMES.get(op_code, str(op_code))

        # Data formatting
        if isinstance(data, (tuple, list)):
            data_items = [format_cell_val(v) for v in data]
            data_str = f"({', '.join(data_items)})"
        else:
            data_str = format_cell_val(data)
        if len(data_str) > 36:
            data_str = data_str[:33] + "..."

        # Result row
        if idx < len(result_ops):
            res_row = result_ops[idx]
            if isinstance(res_row, (tuple, list)):
                res_items = [format_cell_val(v) for v in res_row]
                res_str = f"({', '.join(res_items)})"
            else:
                res_str = format_cell_val(res_row)
        else:
            res_str = COLOR.yellow("(unresolved)")

        lines.append(f"#{idx:<4} | {target_str:<30} | {op_code_str:<12} | {data_str:<36} | {res_str}")

    lines.append(COLOR.cyan("=" * 115))
    return "\n".join(lines)


def format_bridge_tag_map(store: dict[str, dict]) -> str:
    """Format Tag-to-Coordinate Bridge Map (m_bridge_tag, m_tag, m_tag_code, m_ast)."""
    mock_bridge = store.get("m_bridge_tag", {})
    mock_tags = store.get("m_tag", {})
    mock_codes = store.get("m_tag_code", {})
    mock_ast = store.get("m_ast", {})

    tag_map = {row[0]: row for row in mock_tags.values()} if mock_tags else {}
    code_map = {row[0]: row[1] for row in mock_codes.values()} if mock_codes else {}
    ast_map = {row[0]: row for row in mock_ast.values()} if mock_ast else {}

    lines = [
        COLOR.cyan("\n" + "=" * 115),
        COLOR.cyan("                    TAG-TO-COORDINATE BRIDGE MAP (m_bridge_tag & m_tag & m_tag_code)"),
        COLOR.cyan("=" * 115),
        f"{'Tag ID':<7} | {'Line Range':<12} | {'Char Range':<12} | {'AST Node Ref':<26} | {'Hash (SHA-256)':<14} | {'Code Snippet Preview'}",
        COLOR.cyan("-" * 115),
    ]

    sorted_bridge = sorted(mock_bridge.values(), key=lambda r: (r[2], r[4], r[1]))

    for b_row in sorted_bridge:
        fid, tag_id, line_s, line_e, char_s, char_e = b_row
        tag_row = tag_map.get(tag_id)
        ast_ref_str = "-"
        hash_str = "-"
        code_preview = ""

        if tag_row:
            tag_hash = tag_row[3]
            ast_id = tag_row[4]
            if isinstance(tag_hash, bytes):
                hash_str = f"0x{tag_hash[:4].hex()}..{tag_hash[-2:].hex()}"
            elif tag_hash:
                hash_str = str(tag_hash)[:12]
            code_raw = code_map.get(tag_hash, "")
            code_preview = code_raw.replace("\n", "\\n").replace("\t", " ")[:36]

            if ast_id and ast_id in ast_map:
                a_row = ast_map[ast_id]
                a_name = a_row[1] or ""
                a_type = get_ast_type_name(a_row[2])
                ast_ref_str = f"[#{ast_id}] {a_name} ({a_type})"
            elif ast_id:
                ast_ref_str = f"[#{ast_id}]"

        if len(ast_ref_str) > 26:
            ast_ref_str = ast_ref_str[:23] + "..."

        line_range = f"L{line_s} -> L{line_e}"
        char_range = f"C{char_s} -> C{char_e}"

        lines.append(f"#{tag_id:<6} | {line_range:<12} | {char_range:<12} | {ast_ref_str:<26} | {hash_str:<14} | {code_preview}")

    lines.append(COLOR.cyan("=" * 115))
    return "\n".join(lines)


def format_spatial_ast_map(store: dict[str, dict]) -> str:
    """Format Spatial AST Region Map (m_map_ast & m_bridge_map)."""
    mock_map_ast = store.get("m_map_ast", {})
    mock_bridge_map = store.get("m_bridge_map", {})
    mock_ast = store.get("m_ast", {})

    ast_map = {row[0]: row for row in mock_ast.values()} if mock_ast else {}

    map_to_tags: dict[int, list[int]] = {}
    for b_row in mock_bridge_map.values():
        t_id, m_id = b_row[0], b_row[1]
        map_to_tags.setdefault(m_id, []).append(t_id)

    lines = [
        COLOR.cyan("\n" + "=" * 115),
        COLOR.cyan("                            SPATIAL AST REGION MAP (m_map_ast & m_bridge_map)"),
        COLOR.cyan("=" * 115),
        f"{'Map ID':<7} | {'Tag ID(s)':<10} | {'AST ID':<7} | {'Relative Extent':<20} | {'AST Symbol Name':<28} | {'AST Construct Type'}",
        COLOR.cyan("-" * 115),
    ]

    sorted_maps = sorted(mock_map_ast.values(), key=lambda r: (r[0], r[1], r[2]))

    for m_row in sorted_maps:
        map_id, line_s, char_s, line_e, char_e, ast_id = m_row
        tags = map_to_tags.get(map_id, [])
        tags_str = ", ".join(f"#{t}" for t in tags) if tags else "-"
        if len(tags_str) > 10:
            tags_str = tags_str[:8] + ".."

        rel_extent = f"L{line_s}:C{char_s} -> L{line_e}:C{char_e}"

        ast_name = "-"
        ast_type = "-"
        if ast_id and ast_id in ast_map:
            a_row = ast_map[ast_id]
            ast_name = str(a_row[1]) if a_row[1] is not None else ""
            ast_type = f"{get_ast_type_name(a_row[2])} ({a_row[2]})"

        if len(ast_name) > 28:
            ast_name = ast_name[:25] + "..."

        lines.append(f"#{map_id:<6} | {tags_str:<10} | #{ast_id:<6} | {rel_extent:<20} | {ast_name:<28} | {ast_type}")

    lines.append(COLOR.cyan("=" * 115))
    return "\n".join(lines)


def format_ast_container_hierarchy(store: dict[str, dict]) -> str:
    """Format AST Container Hierarchy (m_ast_container & m_ast)."""
    mock_container = store.get("m_ast_container", {})
    mock_ast = store.get("m_ast", {})

    ast_map = {row[0]: row for row in mock_ast.values()} if mock_ast else {}

    lines = [
        COLOR.cyan("\n" + "=" * 115),
        COLOR.cyan("                            AST CONTAINER & HIERARCHY MAP (m_ast_container)"),
        COLOR.cyan("=" * 115),
    ]

    if not mock_container:
        lines.append(COLOR.yellow("  (No child container hierarchy links in this file)"))
        lines.append(COLOR.cyan("=" * 115))
        return "\n".join(lines)

    lines.append(f"{'Parent AST Node':<35} | {'Prio':<5} | {'Child AST Node':<35} | {'Link Type ID & Name'}")
    lines.append(COLOR.cyan("-" * 115))

    sorted_container = sorted(mock_container.values(), key=lambda r: (r[0], r[1]))

    for c_row in sorted_container:
        parent_id, priority, type_id, child_id = c_row

        parent_str = f"[#{parent_id}]"
        if parent_id in ast_map:
            p_row = ast_map[parent_id]
            p_name = p_row[1] or ""
            p_type = get_ast_type_name(p_row[2])
            parent_str = f"[#{parent_id}] '{p_name}' ({p_type})"
        if len(parent_str) > 35:
            parent_str = parent_str[:32] + "..."

        child_str = f"[#{child_id}]"
        if child_id in ast_map:
            ch_row = ast_map[child_id]
            ch_name = ch_row[1] or ""
            ch_type = get_ast_type_name(ch_row[2])
            child_str = f"[#{child_id}] '{ch_name}' ({ch_type})"
        if len(child_str) > 35:
            child_str = child_str[:32] + "..."

        type_name = get_ast_type_name(type_id)
        link_str = f"{type_id} ({type_name})"

        lines.append(f"{parent_str:<35} | {priority:<5} | {child_str:<35} | {link_str}")

    lines.append(COLOR.cyan("=" * 115))
    return "\n".join(lines)


def format_ast_includes_map(store: dict[str, dict]) -> str:
    """Format AST Include Directives (m_ast_include & m_file_name)."""
    mock_include = store.get("m_ast_include", {})
    mock_ast = store.get("m_ast", {})
    mock_file_name = store.get("m_file_name", {})
    if not mock_include:
        return ""
    ast_map = {row[0]: row for row in mock_ast.values()} if mock_ast else {}
    file_map = {row[0]: row[1] for row in mock_file_name.values()} if mock_file_name else {}
    lines = [
        COLOR.cyan("\n" + "=" * 115),
        COLOR.cyan("                                AST INCLUDE DIRECTIVES (m_ast_include)"),
        COLOR.cyan("=" * 115),
        f"{'AST ID':<8} | {'AST Symbol / Directive':<35} | {'Target File ID':<16} | {'Target File Path'}",
        COLOR.cyan("-" * 115),
    ]
    for row in sorted(mock_include.values(), key=lambda r: r[0]):
        ast_id, fnid = row[0], row[1]
        ast_str = f"[#{ast_id}]"
        if ast_id in ast_map:
            a_row = ast_map[ast_id]
            ast_str = f"[#{ast_id}] '{a_row[1]}' ({get_ast_type_name(a_row[2])})"
        target_path = file_map.get(fnid, "-")
        lines.append(f"#{ast_id:<7} | {ast_str:<35} | #{fnid:<15} | {target_path}")
    lines.append(COLOR.cyan("=" * 115))
    return "\n".join(lines)


def format_moved_tags_map(store: dict[str, dict]) -> str:
    """Format Tag Evolution Transitions (m_moved_tag)."""
    mock_moved = store.get("m_moved_tag", {})
    if not mock_moved:
        return ""
    lines = [
        COLOR.cyan("\n" + "=" * 115),
        COLOR.cyan("                              TAG EVOLUTION TRANSITIONS (m_moved_tag)"),
        COLOR.cyan("=" * 115),
        f"{'Source Tag ID':<20} | {'Destination / Evolved Tag ID'}",
        COLOR.cyan("-" * 115),
    ]
    for row in sorted(mock_moved.values(), key=lambda r: (r[0], r[1])):
        s_tag_id, e_tag_id = row[0], row[1]
        lines.append(f"Tag #{s_tag_id:<15} -> Tag #{e_tag_id}")
    lines.append(COLOR.cyan("=" * 115))
    return "\n".join(lines)


def format_consolidated_spatial_map(store: dict[str, dict]) -> str:
    """Format Consolidated Spatial Code-to-AST Map ordered by line/char coordinates."""
    mock_bridge = store.get("m_bridge_tag", {})
    mock_tags = store.get("m_tag", {})
    mock_codes = store.get("m_tag_code", {})
    mock_ast = store.get("m_ast", {})

    tag_map = {row[0]: row for row in mock_tags.values()} if mock_tags else {}
    code_map = {row[0]: row[1] for row in mock_codes.values()} if mock_codes else {}
    ast_map = {row[0]: row for row in mock_ast.values()} if mock_ast else {}

    lines = [
        COLOR.cyan("\n" + "=" * 115),
        COLOR.cyan("                                 CONSOLIDATED SPATIAL CODE-TO-AST FILE MAP"),
        COLOR.cyan("=" * 115),
        f"{'Coordinates':<16} | {'Tag ID':<7} | {'AST ID':<7} | {'AST Construct Type':<22} | {'AST Symbol Name':<20} | {'Source Code'}",
        COLOR.cyan("-" * 115),
    ]

    sorted_bridge = sorted(mock_bridge.values(), key=lambda r: (r[2], r[4], r[1]))

    for b_row in sorted_bridge:
        fid, tag_id, line_s, line_e, char_s, char_e = b_row
        tag_row = tag_map.get(tag_id)

        ast_id_str = "-"
        ast_type_str = "-"
        ast_name_str = "-"
        code_str = ""

        if tag_row:
            tag_hash = tag_row[3]
            ast_id = tag_row[4]
            code_raw = code_map.get(tag_hash, "")
            code_str = code_raw.replace("\n", "\\n").replace("\t", " ")[:34]

            if ast_id:
                ast_id_str = f"#{ast_id}"
                if ast_id in ast_map:
                    a_row = ast_map[ast_id]
                    ast_name_str = str(a_row[1]) if a_row[1] is not None else "''"
                    ast_type_str = get_ast_type_name(a_row[2])

        if len(ast_name_str) > 20:
            ast_name_str = ast_name_str[:17] + "..."
        if len(ast_type_str) > 22:
            ast_type_str = ast_type_str[:19] + "..."

        coord_str = f"L{line_s}:{char_s}->L{line_e}:{char_e}"

        lines.append(f"{coord_str:<16} | #{tag_id:<6} | {ast_id_str:<7} | {ast_type_str:<22} | {ast_name_str:<20} | {code_str}")

    lines.append(COLOR.cyan("=" * 115))
    return "\n".join(lines)


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
            "capture_details": True,
        }
        res = run_single_file_worker(item)
        elapsed = time.time() - start_time
        if res["execute_success"] and not res["error"]:
            # 1. Output Operations Table
            if "staged_ops" in res and "result_ops" in res:
                print(format_operations_table(res["staged_ops"], res["result_ops"]))

            # 2. Output Relational Maps
            if "store" in res:
                store = res["store"]
                print(format_bridge_tag_map(store))
                print(format_spatial_ast_map(store))
                print(format_ast_container_hierarchy(store))
                includes_str = format_ast_includes_map(store)
                if includes_str:
                    print(includes_str)
                moved_str = format_moved_tags_map(store)
                if moved_str:
                    print(moved_str)
                print(format_consolidated_spatial_map(store))

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
            if "staged_ops" in res and res["staged_ops"]:
                print(format_operations_table(res["staged_ops"], res.get("result_ops", [])))
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

