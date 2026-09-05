"""tests/benchmark_suite.py - Comprehensive Performance & Microbenchmark Suite.

Provides isolated performance profiling and microbenchmarks for:
1. Multi-Core & Single-File C-AST Parsing and Execution (cProfile & Stage Breakdown)
2. Maintainer Pattern Matching (Linear vs Indexed)
3. Data Sanitization & to_safe_data() Fast-Path Performance
4. Token Membership & AST Keyword Lookups
5. Spatial Coordinate & Line Range Checks (Line.is_inside, Line.grow)
"""
from __future__ import annotations

import os
import sys
import time
import timeit
import cProfile
import pstats
import io
import argparse
from typing import Any

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Rule 9: Ensure core is imported before parser.c_ast to avoid circular imports
import core
from core.globalstuff import G, COLOR, SafeDataType
from core.TableHandling import to_safe_data
from tests.test_c_ast import TEST_SUITE, run_single_file_worker


def benchmark_cast_profile(top_n: int = 25) -> None:
    """Run cProfile on the C-AST parser test suite and display top bottlenecks."""
    print(COLOR.cyan("\n=========================================================================================="))
    print(COLOR.cyan("                    C-AST PARSER & EXECUTION CPROFILE BENCHMARK                           "))
    print(COLOR.cyan("=========================================================================================="))

    pr = cProfile.Profile()
    pr.enable()
    for item in TEST_SUITE:
        run_single_file_worker({**item, "table_engine": "cached"})
    pr.disable()

    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("tottime")
    ps.print_stats(top_n)
    print(f"\n--- Top {top_n} Functions by Self-Time (tottime) ---")
    print(s.getvalue())

    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumtime")
    ps.print_stats(top_n)
    print(f"\n--- Top {top_n} Functions by Cumulative Time (cumtime) ---")
    print(s.getvalue())


def benchmark_to_safe_data(iterations: int = 100) -> None:
    """Microbenchmark to_safe_data() scalar conversion."""
    print(COLOR.cyan("\n=========================================================================================="))
    print(COLOR.cyan("                    DATA SANITIZATION (to_safe_data) BENCHMARK                            "))
    print(COLOR.cyan("=========================================================================================="))

    from enum import Enum, IntEnum, auto

    class DummyASTT(IntEnum):
        C_struct = auto()
        C_Compound = auto()
        C_int = auto()

    test_data = [123, "string_test", None, True, False, DummyASTT.C_struct, DummyASTT.C_int] * 10000

    t = timeit.timeit(lambda: [to_safe_data(x) for x in test_data], number=iterations)
    print(f"Total time ({iterations} runs x {len(test_data):,} items): {t:.4f}s")
    print(f"Throughput: {len(test_data) * iterations / t:,.0f} conversions/sec\n")


def benchmark_maintainer_matching(files_count: int = 1500, sections_count: int = 500) -> None:
    """Microbenchmark Maintainer matching (Linear vs Prefix-Indexed)."""
    print(COLOR.cyan("\n=========================================================================================="))
    print(COLOR.cyan("                    MAINTAINER PATTERN MATCHING BENCHMARK                                 "))
    print(COLOR.cyan("=========================================================================================="))

    from parser.maintainer_ast.maintainer_types import PatternRule, MaintainerSection, PatternType
    from parser.maintainer_ast.maintainer_matcher import CompiledSectionRules, _pattern_to_regex

    sections = []
    for i in range(sections_count):
        patterns = [
            PatternRule(PatternType.FILE, f"drivers/net/ethernet/intel/e1000_{i}/"),
            PatternRule(PatternType.FILE, f"include/linux/e1000_{i}.h"),
            PatternRule(PatternType.EXCLUDE, f"drivers/net/ethernet/intel/e1000_{i}/test/"),
        ]
        sections.append(MaintainerSection(f"SECTION_{i}", patterns=patterns))

    compiled_rules = [CompiledSectionRules(s) for s in sections]

    # Generate file paths
    files = [
        f"drivers/net/ethernet/intel/e1000_{i % sections_count}/main.c" for i in range(files_count)
    ]

    def linear_match_all():
        results = {}
        for f in files:
            matched = []
            for r in compiled_rules:
                if r.matches(f):
                    matched.append(r.section.name)
            results[f] = matched
        return results

    # Prefix-indexed match
    from collections import defaultdict
    exact_index = defaultdict(list)
    prefix_index = defaultdict(list)
    general_rules = []

    for r in compiled_rules:
        for ep in r.exact_includes:
            exact_index[ep].append(r)
        for pfx in r.prefix_includes:
            top = pfx.split("/")[0]
            prefix_index[top].append((pfx, r))
        if r.regex_includes:
            general_rules.append(r)

    def indexed_match_all():
        results = {}
        for f in files:
            matched = []
            seen = set()
            for r in exact_index.get(f, ()):
                if r.matches(f) and r.section.name not in seen:
                    seen.add(r.section.name)
                    matched.append(r.section.name)
            top = f.split("/")[0]
            for pfx, r in prefix_index.get(top, ()):
                if r.section.name not in seen and f.startswith(pfx) and r.matches(f):
                    seen.add(r.section.name)
                    matched.append(r.section.name)
            for r in general_rules:
                if r.section.name not in seen and r.matches(f):
                    seen.add(r.section.name)
                    matched.append(r.section.name)
            results[f] = matched
        return results

    t1 = timeit.timeit(linear_match_all, number=5)
    t2 = timeit.timeit(indexed_match_all, number=5)

    print(f"Linear Scan ({sections_count} sections, {files_count} files):  {t1:.4f}s")
    print(f"Indexed Scan ({sections_count} sections, {files_count} files): {t2:.4f}s")
    print(f"Speedup: {COLOR.green(f'{t1/t2:.2f}x faster')}\n")


def benchmark_keyword_membership(iterations: int = 10) -> None:
    """Microbenchmark tuple vs frozenset membership checking in token filters."""
    print(COLOR.cyan("\n=========================================================================================="))
    print(COLOR.cyan("                    KEYWORD MEMBERSHIP (TUPLE vs FROZENSET) BENCHMARK                     "))
    print(COLOR.cyan("=========================================================================================="))

    kw_tuple = ("if", "else", "return", "switch", "case", "default", "break", "continue", "for", "while", "do", "goto", "_Static_assert", "static_assert")
    kw_set = frozenset(kw_tuple)

    test_tokens = ["if", "int", "struct", "static_assert", "variable_foo", "return", "while", "u32", "void"] * 100000

    t1 = timeit.timeit(lambda: [x in kw_tuple for x in test_tokens], number=iterations)
    t2 = timeit.timeit(lambda: [x in kw_set for x in test_tokens], number=iterations)

    print(f"Tuple Membership ({len(test_tokens) * iterations:,} checks):    {t1:.4f}s")
    print(f"FrozenSet Membership ({len(test_tokens) * iterations:,} checks): {t2:.4f}s")
    print(f"Speedup: {COLOR.green(f'{t1/t2:.2f}x faster')}\n")


def benchmark_hash_insertion_and_sorting(row_count: int = 20000, batch_size: int = 1000) -> None:
    """Microbenchmark hash serialization, in-memory staging, sorting, and B-Tree page locality."""
    print(COLOR.cyan("\n=========================================================================================="))
    print(COLOR.cyan("               BINARY(32) HASH INSERTION, SERIALIZATION & SORTING BENCHMARK               "))
    print(COLOR.cyan("=========================================================================================="))

    import hashlib
    import random

    # 1. Prepare synthetic code snippets and cryptographic hashes
    raw_snippets = [f"static inline int func_{i}(void *ptr, u32 flags) {{ return {i} * 42; }}" for i in range(row_count)]
    hashes = [hashlib.sha256(s.encode("latin-1")).digest() for s in raw_snippets]
    hex_hashes = [h.hex() for h in hashes]
    int_ids = list(range(1, row_count + 1))

    # 2. Benchmark Serialization & Payload Construction
    print(f"\n1. Serialization & Payload Construction ({row_count:,} rows):")
    t_int = timeit.timeit(lambda: [(i, s) for i, s in zip(int_ids, raw_snippets)], number=20) / 20
    t_bytes = timeit.timeit(lambda: [(h, s) for h, s in zip(hashes, raw_snippets)], number=20) / 20
    t_hex = timeit.timeit(lambda: [(hx, s) for hx, s in zip(hex_hashes, raw_snippets)], number=20) / 20

    print(f"   • Integer PK (INT AUTO_INCREMENT): {t_int * 1000:.3f} ms ({row_count / t_int:,.0f} rows/s)")
    print(f"   • Binary Hash PK (BINARY(32)):     {t_bytes * 1000:.3f} ms ({row_count / t_bytes:,.0f} rows/s)")
    print(f"   • Hex Hash PK (VARCHAR(64)):       {t_hex * 1000:.3f} ms ({row_count / t_hex:,.0f} rows/s)")

    # 3. Benchmark In-Memory Dictionary Staging & Dedup Lookup
    print(f"\n2. In-Memory Staging & Index Lookup ({row_count:,} items):")
    def stage_dict(keys, values):
        d = {}
        for k, v in zip(keys, values):
            d[k] = v
        return d

    t_stage_int = timeit.timeit(lambda: stage_dict(int_ids, raw_snippets), number=20) / 20
    t_stage_bytes = timeit.timeit(lambda: stage_dict(hashes, raw_snippets), number=20) / 20

    print(f"   • Integer Key Dict Staging:        {t_stage_int * 1000:.3f} ms ({row_count / t_stage_int:,.0f} ops/s)")
    print(f"   • BINARY(32) Key Dict Staging:     {t_stage_bytes * 1000:.3f} ms ({row_count / t_stage_bytes:,.0f} ops/s)")

    # 4. Benchmark Batch Pre-Sorting Overhead
    print(f"\n3. Batch Pre-Sorting Overhead ({row_count:,} rows split into {batch_size:,}-row batches):")
    batches = [list(zip(hashes[i : i + batch_size], raw_snippets[i : i + batch_size])) for i in range(0, row_count, batch_size)]

    def sort_all_batches(batch_list):
        return [sorted(b, key=lambda x: x[0]) for b in batch_list]

    t_sort = timeit.timeit(lambda: sort_all_batches(batches), number=20) / 20
    avg_per_batch = (t_sort / len(batches)) * 1000
    print(f"   • Total Sort Time ({len(batches)} batches):   {t_sort * 1000:.3f} ms")
    print(f"   • Average Cost per {batch_size:,}-row Batch: {COLOR.green(f'{avg_per_batch:.4f} ms')} (negligible in-memory cost)")

    # 5. Simulated B+Tree Leaf Page Splits and Access Locality
    print(f"\n4. Simulated InnoDB B+Tree Page Insertion Locality ({row_count:,} inserts, page capacity = 256 keys):")
    PAGE_CAPACITY = 256

    def simulate_btree_inserts(key_list):
        pages = [[]]
        page_splits = 0
        page_switches = 0
        last_page = 0
        for k in key_list:
            # Binary search for page
            idx = 0
            while idx < len(pages) - 1 and len(pages[idx]) > 0 and pages[idx][-1] < k:
                idx += 1
            if idx != last_page:
                page_switches += 1
                last_page = idx
            page = pages[idx]
            page.append(k)
            if len(page) > PAGE_CAPACITY:
                page.sort()
                mid = len(page) // 2
                pages.insert(idx + 1, page[mid:])
                pages[idx] = page[:mid]
                page_splits += 1
        return page_splits, page_switches, len(pages)

    splits_seq, switches_seq, total_p_seq = simulate_btree_inserts(int_ids)
    splits_rand, switches_rand, total_p_rand = simulate_btree_inserts(hashes)
    
    # Simulate batch pre-sorted inserts
    presorted_hashes = []
    for b in batches:
        presorted_hashes.extend([item[0] for item in sorted(b, key=lambda x: x[0])])
    splits_presorted, switches_presorted, total_p_presorted = simulate_btree_inserts(presorted_hashes)

    print(f"   • Sequential Keys (INT PK):          {splits_seq:,} splits | {switches_seq:,} page switches | {total_p_seq:,} total pages")
    print(f"   • Random Unsorted (BINARY(32) PK):   {splits_rand:,} splits | {switches_rand:,} page switches | {total_p_rand:,} total pages")
    print(f"   • Batch Pre-Sorted (BINARY(32) PK):  {splits_presorted:,} splits | {COLOR.green(f'{switches_presorted:,}')} page switches | {total_p_presorted:,} total pages")
    print(f"   • Locality Gain: {COLOR.green(f'{switches_rand / max(switches_presorted, 1):.2f}x fewer page jumps')} across {batch_size}-row batches!\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="KernelInfo-Parser Performance & Benchmark Suite")
    parser.add_argument("--all", action="store_true", help="Run all benchmarks and profiling suites")
    parser.add_argument("--profile-cast", action="store_true", help="Run cProfile on C-AST parser suite")
    parser.add_argument("--safe-data", action="store_true", help="Run to_safe_data microbenchmark")
    parser.add_argument("--maintainers", action="store_true", help="Run maintainer pattern matcher benchmark")
    parser.add_argument("--keywords", action="store_true", help="Run keyword membership benchmark")
    parser.add_argument("--hashes", action="store_true", help="Run BINARY(32) hash insertion & sorting benchmark")
    args = parser.parse_args()

    # Default to running all if no specific benchmark selected
    run_all = args.all or not any([args.profile_cast, args.safe_data, args.maintainers, args.keywords, args.hashes])

    if run_all or args.keywords:
        benchmark_keyword_membership()
    if run_all or args.safe_data:
        benchmark_to_safe_data()
    if run_all or args.maintainers:
        benchmark_maintainer_matching()
    if run_all or args.hashes:
        benchmark_hash_insertion_and_sorting()
    if run_all or args.profile_cast:
        benchmark_cast_profile()


if __name__ == "__main__":
    main()

