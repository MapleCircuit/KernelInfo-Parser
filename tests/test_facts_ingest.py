"""tests/test_facts_ingest.py - Step 6.9: per-blob source facts written by the parser (parser/facts_ingest.py)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import facts_ingest as fi
from parser import kapi_facts as kf
from parser import source_facts as sf

CALLS, KAPI = sf.FAMILY, kf.FAMILY
INC, KCONF, LABELS = 3, 4, 5


class FactsCursor:
    """m_blob_facts in a dict keyed (SHA, family): answers the SELECT of present SHAs, applies the upserts."""

    def __init__(self, rows: dict[tuple[bytes, int], tuple[int, bytes]]) -> None:
        self.rows = rows
        self._result: list = []

    def execute(self, sql: str, params=()) -> None:
        assert sql.startswith("SELECT blob_hash FROM m_blob_facts WHERE family = %s AND format = %s"), sql
        family, fmt, *shas = params
        self._result = [(s,) for s in shas if (s, family) in self.rows and self.rows[(s, family)][0] == fmt]

    def executemany(self, sql: str, rows) -> None:
        assert sql.startswith("INSERT INTO m_blob_facts (blob_hash, family, format, data)"), sql
        for sha, family, fmt, data in rows:
            self.rows[(sha, family)] = (fmt, data)

    def fetchall(self):
        return self._result

    def close(self) -> None:
        pass


class FactsConnection:
    def __init__(self, rows=None) -> None:
        self.rows = rows if rows is not None else {}
        self.commits = 0

    def query(self, sql: str, params=(), dictionary: bool = True) -> list:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def executemany(self, sql: str, rows) -> int:
        cur = self.cursor()
        cur.executemany(sql, rows)
        return len(rows)

    def execute(self, sql: str, params=()) -> int:
        cur = self.cursor()
        cur.execute(sql, params)
        return 1

    def cursor(self) -> FactsCursor:
        return FactsCursor(self.rows)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


A, B, C, D = b"\x0a" * 20, b"\x0b" * 20, b"\x0c" * 20, b"\x0d" * 20
TEXTS = {"kernel/a.c": "static int a(void) { return b(1); }\nEXPORT_SYMBOL(a);\n", "include/b.h": "#define B(x) b(x)\n",
         "lib/c.c": "void c(void) { a(); }\n", "arch/x/lib/memcpy.S": "ENTRY(memcpy)\n\tret\nEXPORT_SYMBOL(memcpy)\n"}


class TestFactsIngest(unittest.TestCase):
    def test_fact_blobs_and_families(self) -> None:
        E, F = b"\x0e" * 20, b"\x0f" * 20
        listing = {"kernel/a.c": A, "include/b.h": B, "copy/a.c": A, "Makefile": C, "arch/x/entry.S": D,
                   "arch/x/Kconfig.debug": E, "Documentation/a/b.rst": F, "README.rst": b"\x01" * 20}
        self.assertEqual(fi.fact_blobs(listing), {A: "kernel/a.c", B: "include/b.h", D: "arch/x/entry.S",
                                                  E: "arch/x/Kconfig.debug", F: "Documentation/a/b.rst"})
        self.assertEqual(fi.families_of("kernel/a.c"), (CALLS, KAPI, INC))
        self.assertEqual(fi.families_of("arch/x/entry.S"), (KAPI, INC), "assembly: Kernel API and includes")
        self.assertEqual(fi.families_of("drivers/Kconfig"), (KCONF,))
        self.assertEqual(fi.families_of("Documentation/x/y.rst"), (LABELS,))
        self.assertEqual(fi.families_of("Makefile"), ())
        self.assertEqual(fi.families_of("README.rst"), ())

    def test_only_missing_families_are_extracted(self) -> None:
        conn = FactsConnection({(A, CALLS): (sf.FACTS_FORMAT, sf.encode(sf.extract(TEXTS["kernel/a.c"]))),
                                (A, KAPI): (kf.KAPI_FORMAT, kf.encode(kf.extract(TEXTS["kernel/a.c"]))),
                                (C, CALLS): (sf.FACTS_FORMAT - 1, b"old format"),
                                (C, KAPI): (kf.KAPI_FORMAT, b"")})
        reads: list[str] = []

        def read(path: str) -> str:
            reads.append(path)
            return TEXTS[path]

        blobs = {A: "kernel/a.c", B: "include/b.h", C: "lib/c.c", D: "arch/x/lib/memcpy.S"}
        res = fi.ingest(conn, blobs, read, workers=1, families=(CALLS, KAPI))
        self.assertEqual((res["blobs"], res["extracted"], res["rows"]), (4, 3, 4))
        self.assertEqual(sorted(reads), ["arch/x/lib/memcpy.S", "include/b.h", "lib/c.c"], "a stored blob is not read again")
        self.assertEqual(conn.rows[(C, CALLS)][0], sf.FACTS_FORMAT, "a row of an older format is replaced")
        self.assertEqual(sf.decode(conn.rows[(C, CALLS)][1])["c"], [[0, "a", 1]])
        self.assertEqual(sf.decode(conn.rows[(B, CALLS)][1])["d"][0][0], "B")
        self.assertEqual(conn.rows[(B, KAPI)], (kf.KAPI_FORMAT, b""), "a file without Kernel API macros is stored empty")
        self.assertNotIn((D, CALLS), conn.rows, "no call facts for assembly")
        self.assertEqual(kf.decode(conn.rows[(D, KAPI)][1])["x"], [["memcpy", "plain", "", 3]])
        self.assertEqual(fi.ingest(conn, blobs, read, workers=1, families=(CALLS, KAPI))["extracted"], 0)

    def test_a_new_family_reads_the_blob_once(self) -> None:
        conn = FactsConnection({(A, CALLS): (sf.FACTS_FORMAT, sf.encode(sf.extract(TEXTS["kernel/a.c"])))})
        reads: list[str] = []
        res = fi.ingest(conn, {A: "kernel/a.c"}, lambda p: reads.append(p) or TEXTS[p], workers=1, families=(CALLS, KAPI))
        self.assertEqual((res["extracted"], res["rows"], reads), (1, 1, ["kernel/a.c"]))
        self.assertEqual(kf.decode(conn.rows[(A, KAPI)][1])["x"], [["a", "plain", "", 2]])

    def test_unreadable_and_huge_blobs_are_stored_empty(self) -> None:
        conn = FactsConnection()
        res = fi.ingest(conn, {A: "gone.c"}, lambda path: None, workers=1)
        self.assertEqual(res["extracted"], 1)
        self.assertEqual(sf.decode(conn.rows[(A, CALLS)][1]), sf.EMPTY, "stored, so it is not tried again every version")
        self.assertEqual(kf.decode(conn.rows[(A, KAPI)][1]), {})


class TestNewFamilies(unittest.TestCase):
    KCONFIG = 'config FOO\n\tbool "Foo"\n\tdepends on BAR\n\thelp\n\t  Some help.\n\nconfig BAZ\n\tint\n'
    RST = (".. _top:\n\nTitle\n=====\n\ntext\n\n.. _`Quoted Label`:\n.. _second:\n\n=======\nSection\n=======\n"
           + "\n" * 12 + ".. _far:\n\n\n\n\n\n\nToo far\n=======\n")

    def test_round_trips(self) -> None:
        from parser import doc_labels as dl
        from parser import include_facts as inc
        from parser.kconfig_ast import kconfig_definitions as kd
        rec = inc.extract('#include <linux/a.h>\n# include "b.c"\n#include_next <c.h>\n')
        self.assertEqual(inc.decode(inc.encode(rec)), [("linux/a.h", False), ("b.c", True), ("c.h", False)])
        self.assertEqual(inc.included_sources(inc.decode(inc.encode(rec))), {"b.c"})
        self.assertEqual(inc.encode(inc.extract("int x;\n")), b"")
        defs = kd.extract(self.KCONFIG)
        self.assertEqual(kd.decode(kd.encode(defs)), defs)
        self.assertEqual(defs["FOO"][0]["help"].strip(), "Some help.")        # help kept in full
        labels = dl.decode(dl.encode(dl.extract(self.RST)))
        self.assertEqual(labels, [("top", 1, "Title"), ("quoted label", 8, "Section"), ("second", 9, "Section"), ("far", 26, "")])

    def test_mask_only_for_families_1_and_2(self) -> None:
        from unittest import mock
        with mock.patch.object(sf, "mask", side_effect=AssertionError("masked")) as mask:
            out = fi.extract_families([("ab" * 20, self.KCONFIG, (KCONF,)), ("cd" * 20, "#include <x.h>\n", (INC,))])
        mask.assert_not_called()
        self.assertEqual(len(out), 2)

    def test_read_by_sha_and_family_filter(self) -> None:
        conn = FactsConnection()
        res = fi.ingest(conn, {A: "drivers/Kconfig", B: "kernel/a.c"}, lambda p: self.fail("read by path"), workers=1,
                        families=(KCONF,), read_sha=lambda sha: self.KCONFIG if sha == A else "int x;\n")
        self.assertEqual((res["extracted"], res["rows"]), (1, 1))
        self.assertEqual(set(conn.rows), {(A, KCONF)})

    def test_multi_language_extractors(self) -> None:
        from parser.rust_ast import rust_facts as rf
        rust_src = (
            'mod helper;\nextern crate alloc;\n\n'
            '#[no_mangle]\npub fn rust_init() -> i32 { 0 }\n\n'
            'EXPORT_SYMBOL_RUST_GPL!(rust_init);\n\n'
            'impl Driver for MyDriver {\n    fn probe() -> i32 { 0 }\n}\n'
        )
        asm_src = (
            '.include "arch_def.h"\n'
            'ENTRY(my_entry)\n'
            '\tcall my_helper\n'
            'ENDPROC(my_entry)\n'
        )
        res = fi.extract_families([
            ("11" * 20, rust_src, (CALLS, KAPI, INC), "drivers/sample.rs"),
            ("22" * 20, asm_src, (CALLS, INC), "arch/x86/entry.S"),
        ])
        self.assertEqual(len(res), 2)
        rust_data = res[0][1]
        rust_f1 = rf.decode(rust_data[CALLS])
        self.assertTrue(any(t[0] == "Driver" for t in rust_f1.get("t", [])))
        rust_f2 = kf.decode(rust_data[KAPI])
        symbols = [item[0] for item in rust_f2.get("x", [])]
        self.assertIn("rust_init", symbols)
        from parser import include_facts as inc
        rust_f3 = inc.decode(rust_data[INC])
        self.assertIn(("helper", False), rust_f3)
        self.assertIn(("alloc", False), rust_f3)

        asm_data = res[1][1]
        asm_f1 = sf.decode(asm_data[CALLS])
        self.assertEqual(asm_f1["d"][0][0], "my_entry")
        self.assertEqual(asm_f1["c"][0][1], "my_helper")
        asm_f3 = inc.decode(asm_data[INC])
        self.assertIn(("arch_def.h", True), asm_f3)

    def test_backfill_doc_references_and_cve_files(self) -> None:
        import json
        import zlib
        from unittest import mock
        from core import backfills

        # Test backfill_cve_files
        db_cve = mock.MagicMock()
        rec_data = zlib.compress(json.dumps({"id": "CVE-2024-9999", "files": ["drivers/net/foo.c", "include/net/foo.h"]}).encode())
        db_cve.query_one.side_effect = [(1,)]
        db_cve.query.side_effect = [[("CVE-2024-9999", rec_data)]]

        with mock.patch.object(backfills, "interrupted_ingests", return_value=0), \
                mock.patch.object(backfills, "insert_ignore", return_value=2) as mock_ins:
            res_cve = backfills.backfill_cve_files(db_cve)
            self.assertEqual(res_cve["state"], "done")
            self.assertEqual(res_cve["cves"], 1)
            self.assertEqual(res_cve["rows"], 2)
            mock_ins.assert_called_once()
            called_rows = mock_ins.call_args[0][3]
            self.assertEqual(called_rows, [("CVE-2024-9999", "drivers/net/foo.c"), ("CVE-2024-9999", "include/net/foo.h")])

        # Test backfill_doc_references
        db_doc = mock.MagicMock()
        db_doc.query.side_effect = [
            [(1, "Documentation/foo.rst"), (2, "include/linux/foo.h")],  # m_file_name
            [(1, 10)],                                                   # m_bridge_checkpoint
            [],                                                          # existing m_file_reference
            [(10, b"\x01" * 20)],                                        # m_file_blob
        ]
        mf = mock.MagicMock()
        mf._read_blob.return_value = b".. kernel-doc:: include/linux/foo.h\n"

        with mock.patch.object(backfills, "interrupted_ingests", return_value=0), \
                mock.patch.object(backfills, "finished_versions", return_value=[("v1", 100, 0)]), \
                mock.patch.object(backfills, "insert_ignore", return_value=1) as mock_doc_ins:
            res_doc = backfills.backfill_doc_references(db_doc, mf, 1, 1)
            self.assertEqual(res_doc["state"], "done")
            self.assertEqual(res_doc["pairs"], 1)
            self.assertEqual(res_doc["rows"], 1)
            mock_doc_ins.assert_called_once()



class TestManFacts(unittest.TestCase):
    """Family 6: man pages of the Linux man-pages tree (parser/man_ast/man_facts.py)."""

    PAGE = (".TH READ 2 2024-05-02 \"Linux man-pages 6.9\"\n.SH NAME\nread \\- read from a file descriptor\n"
            ".SH SYNOPSIS\n.nf\n.B #include <unistd.h>\n.P\n.BI \"ssize_t read(int \" fd \", void \" buf \", size_t \" count );\n"
            ".fi\n.SH SEE ALSO\n.BR close (2),\n.BR readv (2)\n")

    def test_routing(self) -> None:
        from parser.man_ast import man_facts as mf
        self.assertEqual(mf.FAMILY, 6)
        self.assertEqual(fi.FAMILIES[mf.FAMILY], mf.FORMAT)
        for path in ("man/man2/read.2", "man2/read.2", "man/man3type/size_t.3type", "man/man2const/EPOLL_CTL_ADD.2const"):
            self.assertEqual(fi.families_of(path), (mf.FAMILY,), path)
        for path in ("man2", "GNUmakefile", "Documentation/man/man2/x.2", "README", "man/man2/read.3"):
            self.assertEqual(fi.families_of(path), (), path)
        tree = {"man/man2/read.2": b"\1" * 20, "man2": b"\2" * 20, "README": b"\3" * 20}
        self.assertEqual(fi.fact_blobs(tree), {b"\1" * 20: "man/man2/read.2"})

    def test_ingest_stores_family_6(self) -> None:
        from parser.man_ast import man_facts as mf
        sha = b"\7" * 20
        conn = FactsConnection()
        res = fi.ingest(conn, {sha: "man/man2/read.2"}, lambda path: self.PAGE, workers=1)
        self.assertEqual((res["extracted"], res["rows"]), (1, 1))
        fmt, data = conn.rows[(sha, mf.FAMILY)]
        self.assertEqual(fmt, mf.FORMAT)
        self.assertEqual(mf.decode(data), mf.decode(mf.encode(mf.extract(self.PAGE))))
        self.assertIn("read", mf.decode(data)["n"])
        # Stored once: a second version finds the row
        self.assertEqual(fi.ingest(conn, {sha: "man/man2/read.2"}, lambda path: self.fail("read again"))["extracted"], 0)


if __name__ == "__main__":
    unittest.main()
