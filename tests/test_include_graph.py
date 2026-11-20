"""tests/test_include_graph.py - #include resolution and reachability for a build layout (parser/include_graph.py)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from parser.arch_layout import ArchLayout
from parser import include_facts
from parser.include_graph import (
    ConditionEvaluator, IncludeResolver, include_chain, include_directives, include_directives_with_conditions,
)

FILES = {
    # 2.6.12 layout: include/asm -> include/asm-um, include/asm-um/arch -> include/asm-x86_64
    "include/linux/sched.h": '#include <linux/sem.h>\n#include "local.h"\n#ifdef CONFIG_X\n#  include <asm/atomic.h>\n#endif\n',
    "include/linux/local.h": "",
    "include/linux/sem.h": "#include <asm/atomic.h>\n#include <linux/sched.h>\n",
    "include/asm-um/atomic.h": '#include "asm/arch/atomic.h"\n',
    "include/asm-x86_64/atomic.h": "typedef struct { int counter; } atomic_t;\n",
    "include/asm-i386/atomic.h": "typedef struct { int counter; } atomic_t;\n",
    "include/asm-um/processor-x86_64.h": "",
    "include/asm-generic/bug.h": "",
    "include/linux/kconfig.h": "",
    "net/rxrpc/internal.h": "",
}
UM = ArchLayout(
    "um", "um", sources=["arch/um/"], includes=["include/", "arch/um/include/"],
    links={"include/asm/": "include/asm-um/", "include/asm-um/arch/": "include/asm-x86_64/",
           "include/asm-um/processor.h": "include/asm-um/processor-x86_64.h"},
    wrappers={"include/generated/asm/bug.h": "include/asm-generic/bug.h"},
    forced=["include/linux/kconfig.h"],
)


def directives(path: str):
    return include_directives(FILES.get(path, ""))


class TestIncludeResolver(unittest.TestCase):
    def setUp(self) -> None:
        self.r = IncludeResolver(UM, FILES.__contains__)

    def test_directives(self) -> None:
        self.assertEqual(include_directives(FILES["include/linux/sched.h"]),
                         [("linux/sem.h", False), ("local.h", True), ("asm/atomic.h", False)])
        self.assertEqual(include_directives("#include FOO_H\n# include_next <a.h>\n"), [("a.h", False)])

    def test_search_order_links_and_wrappers(self) -> None:
        self.assertEqual(self.r.resolve("asm/atomic.h"), "include/asm-um/atomic.h")
        self.assertEqual(self.r.resolve("asm/arch/atomic.h"), "include/asm-x86_64/atomic.h")  # two links
        self.assertEqual(self.r.resolve("asm/processor.h"), "include/asm-um/processor-x86_64.h")  # file link
        self.assertEqual(self.r.resolve("local.h", "include/linux/sched.h", quoted=True), "include/linux/local.h")
        self.assertIsNone(self.r.resolve("local.h", "include/linux/sched.h", quoted=False))
        self.assertEqual(self.r.materialize("include/generated/asm/bug.h"), "include/asm-generic/bug.h")
        self.assertIsNone(self.r.resolve("asm/offsets.h"))
        self.assertIsNone(self.r.resolve("../../etc/passwd"))

    def test_prefetch_receives_each_level(self) -> None:
        levels: list[list[str]] = []
        with_hook = self.r.reachable("include/linux/sched.h", directives, prefetch=lambda paths: levels.append(list(paths)))
        self.assertEqual(with_hook, self.r.reachable("include/linux/sched.h", directives))   # same walk, same order
        self.assertEqual(list(with_hook), list(self.r.reachable("include/linux/sched.h", directives)))
        self.assertEqual([p for level in levels for p in level], list(with_hook))           # every file, level by level
        self.assertEqual(levels[0][-1], "include/linux/sched.h")

    def test_reachable_files_and_chain(self) -> None:
        reached = self.r.reachable("include/linux/sched.h", directives)
        self.assertEqual(list(reached)[:2], ["include/linux/kconfig.h", "include/linux/sched.h"])  # forced first
        self.assertIn("include/asm-x86_64/atomic.h", reached)
        self.assertNotIn("include/asm-i386/atomic.h", reached)
        self.assertNotIn("net/rxrpc/internal.h", reached)
        # Breadth-first: the #ifdef'd direct include is the shortest chain
        self.assertEqual(include_chain(reached, "include/asm-x86_64/atomic.h"),
                         ["include/linux/sched.h", "include/asm-um/atomic.h", "include/asm-x86_64/atomic.h"])
        self.assertEqual(include_chain(reached, "net/rxrpc/internal.h"), [])


class TestDirectiveScan(unittest.TestCase):
    def test_whitespace_comments_continuations_and_crlf(self) -> None:
        text = ('#  include <a.h>\r\n#\tinclude "b.h"\r\n/* #include <hidden.h>\n#include <hidden2.h> */\n'
                '// #include <line.h>\n#include <c.h> /* tail */\n#if 1 /* multi\nline */ && X\n#include <d.h>\n#endif\n'
                '#define LONG \\\n  x\n#include \\\n  <e.h>\n')
        self.assertEqual(include_directives(text),
                         [("a.h", False), ("b.h", True), ("c.h", False), ("d.h", False), ("e.h", False)])
        self.assertEqual(include_directives_with_conditions(text)[3][2], ["(1   && X)"])

    def test_conditions(self) -> None:
        text = ("#include <top.h>\n#ifdef A\n#include <a.h>\n#elif B > 1 /* c */\n#include <b.h>\n#else\n#include <c.h>\n#endif\n"
                "#ifndef __ASSEMBLY__\n#if X\n#include <n.h>\n#endif\n#endif\n#include <after.h>\n")
        got = {n: c for n, _q, c in include_directives_with_conditions(text)}
        self.assertEqual(got["top.h"], [])
        self.assertEqual(got["a.h"], ["defined(A)"])
        self.assertEqual(got["b.h"], ["!(defined(A))", "(B > 1)"])
        self.assertEqual(got["c.h"], ["!(defined(A))", "!((B > 1))"])
        self.assertEqual(got["n.h"], ["!defined(__ASSEMBLY__)", "(X)"])
        self.assertEqual(got["after.h"], [])


def _eval(defines: dict[str, str] | None = None) -> ConditionEvaluator:
    return ConditionEvaluator(defines or {}, lambda n: n.startswith("CONFIG_") or n in ("__ASSEMBLY__", "__KERNEL__"))


class TestConditionEvaluator(unittest.TestCase):
    def test_arithmetic_and_defined(self) -> None:
        ev = _eval({"CONFIG_A": "1", "VER": "0x0204", "EMPTY": ""})
        self.assertEqual(ev.evaluate("(1 + 2 * 3 == 7) && (1 << 4) == 16"), 1)
        self.assertEqual(ev.evaluate("VER >= 0x0200UL && VER < 0x0300"), 1)
        self.assertEqual(ev.evaluate("-1 < 0 && ~0 == -1 && 7 % 4 == 3 && 7 / 2 == 3"), 1)
        self.assertEqual(ev.evaluate("1 ? 2 : 3"), 2)
        self.assertEqual(ev.evaluate("defined(CONFIG_A) && defined EMPTY && !defined(CONFIG_B)"), 1)
        self.assertEqual(ev.evaluate("EMPTY"), 1)             # defined without a numeric value counts as 1
        self.assertEqual(ev.evaluate("CONFIG_B"), 0)          # known and absent: 0

    def test_unknown_and_short_circuit(self) -> None:
        ev = _eval()
        self.assertIsNone(ev.evaluate("FOO"))
        self.assertIsNone(ev.evaluate("FOO && 1"))
        self.assertEqual(ev.evaluate("0 && FOO"), 0)
        self.assertEqual(ev.evaluate("FOO && 0"), 0)
        self.assertEqual(ev.evaluate("1 || FOO"), 1)
        self.assertIsNone(ev.evaluate("0 || FOO"))
        self.assertIsNone(ev.evaluate("FOO ? 1 : 2"))
        self.assertEqual(ev.evaluate("FOO ? 1 : 1"), 1)
        self.assertIsNone(ev.evaluate("__has_include(<x.h>)"))
        self.assertIsNone(ev.evaluate("1 +"))                 # malformed
        self.assertTrue(ev(["(FOO)"]))                        # unknown keeps the include
        self.assertTrue(ev(["(FOO && 1)"]))

    def test_config_macros(self) -> None:
        ev = _eval({"CONFIG_Y": "1", "CONFIG_M_MODULE": "1"})
        self.assertEqual(ev.evaluate("IS_ENABLED(CONFIG_Y)"), 1)
        self.assertEqual(ev.evaluate("IS_ENABLED(CONFIG_M)"), 1)
        self.assertEqual(ev.evaluate("IS_BUILTIN(CONFIG_M)"), 0)
        self.assertEqual(ev.evaluate("IS_MODULE(CONFIG_M)"), 1)
        self.assertEqual(ev.evaluate("IS_REACHABLE(CONFIG_M)"), 1)
        self.assertEqual(ev.evaluate("IS_ENABLED(CONFIG_NONE)"), 0)
        self.assertIsNone(ev.evaluate("IS_ENABLED(SOMETHING_ELSE)"))

    def test_conjunction_filters_includes(self) -> None:
        text = ("#ifdef CONFIG_X\n#include <x.h>\n#endif\n#ifndef __ASSEMBLY__\n#include <c-only.h>\n#endif\n"
                "#if defined(CONFIG_Y) && !defined(CONFIG_Z)\n#include <y.h>\n#else\n#include <noy.h>\n#endif\n"
                "#ifdef UNKNOWN_MACRO\n#include <u.h>\n#endif\n#include <plain.h>\n")
        ev = _eval({"__ASSEMBLY__": "1", "CONFIG_Y": "1"})
        kept = [n for n, _q, c in include_directives_with_conditions(text) if ev(c)]
        self.assertEqual(kept, ["y.h", "u.h", "plain.h"])

    def test_reachable_passes_conditions_to_the_filter(self) -> None:
        files = {"a.c": "#ifdef CONFIG_OFF\n#include <off.h>\n#endif\n#include <on.h>\n", "include/off.h": "", "include/on.h": ""}
        layout = ArchLayout("x", "x", sources=[], includes=["include/"], links={}, wrappers={}, forced=[])
        r = IncludeResolver(layout, files.__contains__)
        seen: list[list[str]] = []

        def keep(conds: list[str]) -> bool:
            seen.append(conds)
            return _eval()(conds)

        got = r.reachable("a.c", lambda p: include_directives_with_conditions(files.get(p, "")), filter_cond=keep)
        self.assertEqual(list(got), ["a.c", "include/on.h"])
        self.assertIn(["defined(CONFIG_OFF)"], seen)


class TestIncludeFacts(unittest.TestCase):
    def test_round_trip_and_suffixes(self) -> None:
        self.assertEqual(include_facts.FORMAT, 2)
        for path in ("a.c", "a.h", "a.S", "a.s"):
            self.assertTrue(include_facts.applies(path))
        self.assertFalse(include_facts.applies("a.rs"))
        rec = include_facts.extract('#include <a.h>\n#ifdef CONFIG_X\n#include "b.c"\n#endif\n')
        self.assertEqual(rec, [["a.h", False, []], ["b.c", True, ["defined(CONFIG_X)"]]])
        decoded = include_facts.decode(include_facts.encode(rec))
        self.assertEqual(decoded, [("a.h", False, []), ("b.c", True, ["defined(CONFIG_X)"])])
        self.assertEqual(include_facts.included_sources(decoded), {"b.c"})
        self.assertEqual(include_facts.included_sources([("c.S", True)]), {"c.S"})   # 2-tuples are accepted
        self.assertEqual(include_facts.encode(include_facts.extract("int x;\n")), b"")
        self.assertEqual(include_facts.decode(b""), [])
        self.assertIsNone(include_facts.decode(b"garbage"))
        self.assertIsNone(include_facts.decode(None))

    def test_stored_pairs_decode_with_empty_conditions(self) -> None:
        self.assertEqual(include_facts.decode(include_facts.encode([["x.h", False], ["y.h", True]])), [("x.h", False, []), ("y.h", True, [])])


class TestDefinitionFromUseSite(unittest.TestCase):
    def test_reached_definition_wins_over_path_rank(self) -> None:
        from core.globalstuff import SymbolLinkage
        from webapp.backend.services import symbol_service as ss

        blob_paths = {1: ["include/asm-i386/atomic.h"], 2: ["include/asm-x86_64/atomic.h"], 3: ["net/rxrpc/internal.h"],
                      4: ["include/linux/proto.h"]}
        rows = [{"fid": b, "fname": blob_paths[b][0], "line_s": 1, "linkage": SymbolLinkage.ExternalDef} for b in (1, 2, 3)]
        rows.append({"fid": 4, "fname": "include/linux/proto.h", "line_s": 1, "linkage": SymbolLinkage.PrototypeDecl})
        reached = {"include/linux/sched.h": None, "include/asm-x86_64/atomic.h": "include/linux/sched.h",
                   "include/linux/proto.h": "include/linux/sched.h"}
        with patch.object(ss.arch_layout_service, "reachable", return_value=reached):
            ss.SymbolService._prefer_reachable(rows, blob_paths, "v2.6.12", "i386", "include/linux/sched.h", "i386", 32)
        # The reached x86_64 header beats i386's own (path rank alone would pick asm-i386); prototypes stay last
        self.assertEqual([r["fname"] for r in rows],
                         ["include/asm-x86_64/atomic.h", "include/asm-i386/atomic.h", "net/rxrpc/internal.h", "include/linux/proto.h"])
        self.assertEqual([r["visible"] for r in rows], [True, False, False, True])

    def test_no_layout_keeps_the_ranking(self) -> None:
        from webapp.backend.services import symbol_service as ss

        rows = [{"fid": 1, "fname": "a.h", "line_s": 1, "linkage": 0}]
        with patch.object(ss.arch_layout_service, "reachable", return_value={}):
            ss.SymbolService._prefer_reachable(rows, {1: ["a.h"]}, "v", "x86_64", "b.c", "x86", 64)
        self.assertNotIn("visible", rows[0])


class TestIncludeEndpoint(unittest.TestCase):
    def test_build_layout_answers_first(self) -> None:
        from webapp.backend.services import filesystem_service as fs

        reader = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = reader
        svc = fs.FilesystemService()
        with patch.object(fs, "get_reader", return_value=ctx), \
             patch.object(fs, "get_version_info", return_value=(1, "v2.6.12")), \
             patch("webapp.backend.services.tree_resolver.definition_arch", return_value=("x86_64", "x86_64", 64)), \
             patch("webapp.backend.services.arch_layout_service.arch_layout_service.resolve_include",
                   return_value="include/asm-x86_64/atomic.h") as resolve:
            fs._INCLUDE_CACHE.clear()
            res = svc.resolve_include("v2.6.12", "<asm/atomic.h>", None, "include/linux/sched.h", "x86_64")
        self.assertEqual((res["path"], res["method"], res["arch"]), ("include/asm-x86_64/atomic.h", "build_layout", "x86_64"))
        resolve.assert_called_once_with("v2.6.12", "x86_64", "asm/atomic.h", "include/linux/sched.h", False)
        reader.select_rows.assert_not_called()
        reader.select_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
