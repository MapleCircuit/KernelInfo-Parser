"""tests/test_webapp_callpath.py - Call path finder (N2, webapp/API.md section 4.7)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services.callpath_service import find_call_paths, search_paths

GRAPH = {  # caller -> callees
    "a": ["b", "c", "printk"], "b": ["d"], "c": ["d", "a"], "d": ["e"], "e": ["a"], "printk": ["e"],
    "x": ["y"], "y": ["x"],
}


def callees(names):
    return {n: [(m, f"{n}.c", i + 1) for i, m in enumerate(GRAPH.get(n, []))] for n in names if n in GRAPH}


def callers(names):
    out = {}
    for caller, outs in GRAPH.items():
        for i, m in enumerate(outs):
            if m in names:
                out.setdefault(m, []).append((caller, f"{caller}.c", i + 1))
    return out


def chains(res):
    return sorted(tuple(h["function"] for h in p) for p in res["paths"])


class TestCallpath(unittest.TestCase):
    def test_shortest_paths_with_sites(self) -> None:
        res = search_paths("a", "e", callees, callers)
        self.assertEqual(chains(res), [("a", "printk", "e")], "only the shortest chains")
        first = res["paths"][0]
        self.assertEqual([(h["function"], h["path"], h["line"]) for h in first], [("a", "a.c", 3), ("printk", "printk.c", 1), ("e", None, None)])

    def test_multiple_shortest_and_exclude(self) -> None:
        res = search_paths("a", "e", callees, callers, exclude=["printk"])
        self.assertEqual(chains(res), [("a", "b", "d", "e"), ("a", "c", "d", "e")])
        self.assertEqual(len(search_paths("a", "e", callees, callers, exclude=["printk"], k=1)["paths"]), 1)

    def test_cycles_unreachable_and_budget(self) -> None:
        res = search_paths("x", "e", callees, callers)
        self.assertEqual(res["paths"], [])
        self.assertGreater(res["explored"], 0)
        self.assertFalse(res["budget_exhausted"])
        tight = search_paths("a", "e", callees, callers, exclude=["printk"], budget=1)
        self.assertTrue(tight["budget_exhausted"])
        self.assertEqual(search_paths("a", "e", callees, callers, exclude=["printk"], max_depth=2)["paths"], [])

    def test_one_sided_search_when_callers_are_unknown(self) -> None:
        res = search_paths("a", "e", callees, lambda names: {}, exclude=["printk"])
        self.assertEqual(chains(res), [("a", "b", "d", "e"), ("a", "c", "d", "e")])

    def test_several_sources_and_targets(self) -> None:
        res = search_paths(["x", "b"], ["e", "y"], callees, callers)
        self.assertEqual(chains(res), [("x", "y")], "the shortest chain from any source to any target")

    def test_via_is_kept(self) -> None:
        def via_callees(names):
            return {n: [(m, f"{n}.c", 1, "ops.read")] for n in names if n == "p" for m in ["q"]}

        res = search_paths("p", "q", via_callees, lambda names: {})
        self.assertEqual(res["paths"][0][0], {"function": "p", "path": "p.c", "line": 1, "via": "ops.read"})


class TestCallpathOverSourceIndex(unittest.TestCase):
    def test_names_files_arch_and_indirect(self) -> None:
        from tests.test_webapp_source_index import SOURCES, load_from
        from webapp.backend.services.source_index import RevisionIndex
        files, load = load_from(SOURCES)
        idx = RevisionIndex("0" * 40, files, load)
        res = find_call_paths(idx, "entry", "leaf")
        self.assertEqual(chains(res), [("entry", "helper", "shared", "leaf")])
        hop = res["paths"][0][1]
        self.assertEqual((hop["function"], hop["file"], hop["path"]), ("helper", "kernel/a.c", "kernel/a.c"), "the static helper of a.c")
        # Calls made in another architecture's tree are not followed
        self.assertEqual(chains(find_call_paths(idx, "leaf2", "leaf")), [("leaf2", "shared", "leaf")])
        self.assertEqual(find_call_paths(idx, "leaf2", "leaf", arch_name="x86_64", allowed_test=lambda p: not p.startswith("arch/arm/"))["paths"], [])
        # Through an ops table only when asked
        self.assertEqual(find_call_paths(idx, "vfs_read", "foo_read")["paths"], [])
        res = find_call_paths(idx, "vfs_read", "foo_read", indirect=True)
        self.assertEqual(res["paths"][0][0]["via"], "file_operations.read")
        self.assertEqual(find_call_paths(idx, "nope", "leaf")["found"], {"from": 0, "to": 1})


if __name__ == "__main__":
    unittest.main()
