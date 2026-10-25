"""tests/test_include_graph.py - #include resolution and reachability for a build layout (parser/include_graph.py)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from parser.arch_layout import ArchLayout
from parser.include_graph import IncludeResolver, include_chain, include_directives

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
            chain = ss.SymbolService._prefer_reachable(rows, blob_paths, "v2.6.12", "i386", "include/linux/sched.h", "i386", 32)
        # The reached x86_64 header beats i386's own (path rank alone would pick asm-i386); prototypes stay last
        self.assertEqual([r["fname"] for r in rows],
                         ["include/asm-x86_64/atomic.h", "include/asm-i386/atomic.h", "net/rxrpc/internal.h", "include/linux/proto.h"])
        self.assertEqual(chain, ["include/linux/sched.h", "include/asm-x86_64/atomic.h"])
        self.assertEqual([r["visible"] for r in rows], [True, False, False, True])

    def test_no_layout_keeps_the_ranking(self) -> None:
        from webapp.backend.services import symbol_service as ss

        rows = [{"fid": 1, "fname": "a.h", "line_s": 1, "linkage": 0}]
        with patch.object(ss.arch_layout_service, "reachable", return_value={}):
            self.assertEqual(ss.SymbolService._prefer_reachable(rows, {1: ["a.h"]}, "v", "x86_64", "b.c", "x86", 64), [])
        self.assertNotIn("visible", rows[0])


class TestIncludeEndpoint(unittest.TestCase):
    def test_build_layout_answers_first(self) -> None:
        from webapp.backend.services import filesystem_service as fs

        cursor = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor
        svc = fs.FilesystemService()
        with patch.object(fs, "get_db_cursor", return_value=ctx), \
             patch.object(fs, "get_version_info", return_value=(1, "v2.6.12")), \
             patch("webapp.backend.services.tree_resolver.definition_arch", return_value=("x86_64", "x86_64", 64)), \
             patch("webapp.backend.services.arch_layout_service.arch_layout_service.resolve_include",
                   return_value="include/asm-x86_64/atomic.h") as resolve:
            fs._INCLUDE_CACHE.clear()
            res = svc.resolve_include("v2.6.12", "<asm/atomic.h>", None, "include/linux/sched.h", "x86_64")
        self.assertEqual((res["path"], res["method"], res["arch"]), ("include/asm-x86_64/atomic.h", "build_layout", "x86_64"))
        resolve.assert_called_once_with("v2.6.12", "x86_64", "asm/atomic.h", "include/linux/sched.h", False)
        cursor.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
