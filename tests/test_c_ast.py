"""tests/test_c_ast.py - Comprehensive Multi-Core C-AST Parser & ChangeSet Test Suite.

Provides isolated, multi-core unit tests executing in RAMDISK (/dev/shm) with MockDB.
Validates AST generation, length delta tracking for optimization reviews,
and verifies ChangeSet.execute() execution without errors.
"""
from __future__ import annotations

import os
import sys
import time
import shutil
import unittest
import subprocess
import multiprocessing
from typing import Any

from core.globalstuff import G, COLOR
from core.GreatProcessor import GreatProcessor
from core.FileHandler import MasterFile
from core.TableHandling import ChangeSet
from core.DBLayout import (
    init_db_layout,
    m_file_name,
    m_file,
    m_bridge_file,
)
from table_engine.te_direct_db import TEDirectDB
from db_engine import MockDB, MariaDB
from parser.c_ast.c_ast_type import safe_spelling, safe_cursor_spelling

# Standard regression files tested across AST parser
# Baseline operations count corresponds to pure AST operations (excluding the 3 lifecycle operations)
TEST_SUITE: list[dict[str, Any]] = [
    {
        "file": "include/linux/drbd_tag_magic.h",
        "baseline_ast_ops": 442,
        "description": "Kernel Header (drbd_tag_magic.h)",
    },
    {
        "file": "virt/kvm/iodev.h",
        "baseline_ast_ops": 231,
        "description": "Kernel Header (virt/kvm/iodev.h)",
    },
    {
        "file": "include/linux/lockd/bind.h",
        "baseline_ast_ops": 217,
        "description": "Kernel Header (lockd/bind.h)",
    },
    {
        "file": "include/linux/netfilter_bridge/ebtables.h",
        "baseline_ast_ops": 1584,
        "description": "Kernel Header (ebtables.h)",
    },
    {
        "file": "drivers/watchdog/w83627hf_wdt.c",
        "baseline_ast_ops": 1468,
        "description": "Watchdog Driver (Latin-1 byte 0xe1 resilience)",
    },
    {
        "file": "drivers/usb/storage/isd200.c",
        "baseline_ast_ops": 4161,
        "description": "USB Storage Driver (Latin-1 byte 0xf6 resilience)",
    },
    {
        "file": "include/linux/sched.h",
        "baseline_ast_ops": 12507,
        "description": "Kernel Header (sched.h)",
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
        G.DEBUG_TYPECHECK = True
        G.DB = MockDB
        G.TE = TEDirectDB()
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

        default_processing(cs, gp)
        cs.parse()
        actual_total_ops = len(cs.cs)

        # Execute ChangeSet operations against DB
        t_exec = time.time()
        exec_ok = cs.execute()
        exec_time = time.time() - t_exec

        elapsed = time.time() - t0
        return {
            "file": file_path,
            "description": description,
            "baseline_total_ops": baseline_total_ops,
            "actual_total_ops": actual_total_ops,
            "delta": actual_total_ops - baseline_total_ops,
            "execute_success": exec_ok,
            "results_count": len(cs.cs_result),
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


def run_c_ast_tests(target_file: str | None = None, profile: bool = False) -> int:
    """Programmatic multi-core test runner invoked via CLI in main.py.
    
    Args:
        target_file: Optional single file path to test.
        profile: Whether to collect and print granular stage profiler breakdowns.
        
    Returns:
        0 if all tests pass, 1 otherwise.
    """
    if profile:
        G.PROFILING_ENABLED = True

    print(COLOR.cyan("\n=========================================================================================="))
    print(COLOR.cyan("                    MULTI-CORE C-AST PARSER & EXECUTE TEST RUNNER                         "))
    print(COLOR.cyan("=========================================================================================="))
    print(f"Workspace: {COLOR.magenta(G.RAMDISK)} (Isolated /dev/shm RAMDISK)")
    print(f"Dataset:   {COLOR.magenta('Linux v3.0 (Read-Only)')}")
    print(f"Backend:   {COLOR.magenta('In-Memory MockDB (Isolated)')}")
    if G.PROFILING_ENABLED:
        print(f"Profiler:  {COLOR.green('ACTIVE (-p / --profile)')}\n")
    else:
        print()

    start_time = time.time()

    if target_file:
        print(COLOR.cyan(f"[*] Executing Single Target Test: {target_file}"))
        item = {
            "file": target_file,
            "baseline_ast_ops": 0,
            "description": f"Target: {target_file}",
        }
        res = run_single_file_worker(item)
        elapsed = time.time() - start_time
        if res["execute_success"] and not res["error"]:
            print(COLOR.green(f"\n[+] PASS: {target_file}"))
            print(f"    - Operations Staged:    {res['actual_total_ops']:,}")
            print(f"    - CS.execute():         {COLOR.green('SUCCESS')}")
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

    with multiprocessing.Pool(processes=workers) as pool:
        results = pool.map(run_single_file_worker, TEST_SUITE)

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
    args = parser.parse_args()
    sys.exit(run_c_ast_tests(target_file=args.target_file, profile=args.profile))
