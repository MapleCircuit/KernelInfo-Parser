"""tests/test_blob_fact_readers.py - Webapp readers of the per-blob fact families 3-5 (m_blob_facts, D07).

Rows built by the parser's extractors (parser/include_facts.py, parser/kconfig_ast/kconfig_definitions.py,
parser/doc_labels.py) give the same answers as the webapp's Git paths, on a temporary repository.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import doc_labels, facts_ingest, include_facts
from parser.kconfig_ast import kconfig_definitions as kd

ENV = {**os.environ, "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@x",
       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

FILES = {
    "Documentation/index.rst": ".. _top:\n\nKernel docs\n===========\n\n.. _dup:\n\nFirst\n-----\n",
    "Documentation/a/b.rst": (".. _dup:\n\nSecond\n------\n\n.. _`Quoted`:\n.. _also:\n\n=====\nOver\n=====\n" + "x\n" * 10
                              + ".. _late:\n\n\n\n\n\n\nToo late\n========\n"),
    "Documentation/c.txt": ".. _not_rst:\n",
    "arch/x/Kconfig": 'config X86\n\tbool "x86"\n\tselect FOO\n\thelp\n\t  The x86.\n\nif X86\nconfig FOO\n\ttristate\nendif\n',
    "lib/Kconfig.debug": 'menu "Debug"\nconfig DEBUG_X\n\tbool "dbg" if X86\n\tdefault y\nendmenu\n',
    "kernel/a.c": '#include <linux/a.h>\n#include "b.c"\n/* #include "c.c" */\nint a;\n',
    "kernel/b.c": "int b;\n",
    "arch/x/entry.S": '#include "tbl.S"\n',
    "include/linux/a.h": "#pragma once\n",
}


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestReaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = Path(tempfile.mkdtemp(prefix="facts-"))
        for path, text in FILES.items():
            (cls.dir / path).parent.mkdir(parents=True, exist_ok=True)
            (cls.dir / path).write_text(text)
        cls.git("init", "-q", "-b", "master")
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", "tree")
        cls.sha = cls.git("rev-parse", "HEAD").strip()
        cls.tree = {line.split("\t")[1]: line.split()[2] for line in cls.git("ls-tree", "-r", "HEAD").splitlines()}
        # What Step 6.9 stores: every family of every blob
        cls.rows: dict[tuple[str, int], bytes] = {}
        items = [(sha, FILES[path], facts_ingest.families_of(path)) for path, sha in cls.tree.items() if facts_ingest.families_of(path)]
        for sha, data in facts_ingest.extract_families(items):
            for family, payload in data.items():
                cls.rows[(sha, family)] = payload

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def git(cls, *args: str) -> str:
        return subprocess.run(["git", "-C", str(cls.dir), *args], check=True, capture_output=True, text=True, env=ENV).stdout

    def db_facts(self, skip: str | None = None):
        rows = {k: v for k, v in self.rows.items() if not (skip and k[0] == self.tree[skip])}

        def fake(shas, family, fmt, decoder):
            out = {}
            for s in shas:
                if (s, family) in rows:
                    out[s] = decoder(rows[(s, family)])
            return out
        return fake

    def setUp(self) -> None:
        from webapp.backend.services import tree_resolver
        self.patches = [
            mock.patch.object(tree_resolver.git_tree_resolver, "list_tree", return_value=self.tree),
            mock.patch.object(tree_resolver.git_tree_resolver, "resolve_commit_sha", return_value=self.sha),
            mock.patch("webapp.backend.services.docs_service.get_repo_dir", lambda: self.dir),
            mock.patch("webapp.backend.services.kconfig_facts.get_repo_dir", lambda: self.dir),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def git_labels(self) -> dict:
        from webapp.backend.services import docs_service as ds
        svc = ds.DocsService()
        with mock.patch.object(ds, "get_db_cursor"), mock.patch.object(ds, "get_version_info", return_value=(1, "v1")), \
                mock.patch.object(ds.DocsService, "_labels_from_facts", staticmethod(lambda sha: None)):
            return svc.get_label_index("v1")

    def test_label_index_from_family_5_equals_git_grep(self) -> None:
        from webapp.backend.services import docs_service as ds
        git = self.git_labels()
        self.assertEqual(git["source"], "git")
        with mock.patch("webapp.backend.services.source_index.db_facts", self.db_facts()):
            stored = ds.DocsService._labels_from_facts(self.sha)
        self.assertEqual(stored, git["labels"])
        self.assertEqual(stored["dup"]["path"], "Documentation/a/b.rst")         # tree order: the first label wins
        self.assertEqual(stored["quoted"]["title"], "Over")
        self.assertNotIn("not_rst", stored)
        with mock.patch("webapp.backend.services.source_index.db_facts", self.db_facts(skip="Documentation/index.rst")):
            self.assertIsNone(ds.DocsService._labels_from_facts(self.sha))         # one blob missing: git grep

    def test_kconfig_definitions_from_family_4_equal_the_git_parse(self) -> None:
        from webapp.backend.services import kconfig_facts as kf
        for path in ("arch/x/Kconfig", "lib/Kconfig.debug"):
            sha = self.tree[path]
            kf.clear()
            parsed = kf.definitions(sha, lambda: FILES[path])
            kf.clear()
            before = dict(kf.STATS)
            with mock.patch("webapp.backend.services.source_index.db_facts", self.db_facts()):
                stored = kf.definitions(sha, lambda: self.fail("parsed from Git"))
            self.assertEqual(stored, parsed, path)
            self.assertEqual(kf.STATS["parsed"], before["parsed"])
        self.assertEqual(stored["DEBUG_X"][0]["inherited_depends"], parsed["DEBUG_X"][0]["inherited_depends"])
        kf.clear()
        with mock.patch("webapp.backend.services.source_index.db_facts", self.db_facts()):
            self.assertEqual(kf.read_blobs([self.tree["arch/x/Kconfig"]]), {})   # loaded from the database, not read
        kf.clear()

    def test_included_sources_and_directives(self) -> None:
        from webapp.backend.services import kconfig_service as ks
        with mock.patch("webapp.backend.services.source_index.db_facts", self.db_facts()):
            self.assertEqual(ks.KconfigService._included_sources_db(self.sha), {"b.c", "tbl.S"})   # not the commented c.c
        with mock.patch("webapp.backend.services.source_index.db_facts", self.db_facts(skip="kernel/b.c")):
            self.assertIsNone(ks.KconfigService._included_sources_db(self.sha))
        from webapp.backend.services.arch_layout_service import ArchLayoutService
        svc = ArchLayoutService()
        files = {"kernel/a.c": self.tree["kernel/a.c"]}
        with mock.patch("webapp.backend.services.source_index.db_facts", self.db_facts()), \
                mock.patch("webapp.backend.services.arch_layout_service.git_reader.read_file", side_effect=AssertionError("git")):
            svc._prefetch_directives(["kernel/a.c"], files)
            self.assertEqual(svc._file_directives(self.sha, "kernel/a.c", files),
                             [("linux/a.h", False), ("b.c", True)])

    def test_family_formats_match_the_parser(self) -> None:
        self.assertEqual(facts_ingest.FAMILIES[include_facts.FAMILY], include_facts.FORMAT)
        self.assertEqual(facts_ingest.FAMILIES[kd.FAMILY], kd.FORMAT)
        self.assertEqual(facts_ingest.FAMILIES[doc_labels.FAMILY], doc_labels.FORMAT)


class TestBackfill(unittest.TestCase):
    def test_backfill_reads_by_sha_only_missing(self) -> None:
        from core import backfills
        cnx = mock.MagicMock()
        mf = mock.MagicMock()
        mf._read_blob.side_effect = lambda sha: b"#include <x.h>\n"
        with mock.patch.object(backfills, "interrupted_ingests", return_value=0), \
                mock.patch.object(backfills, "finished_versions", return_value=[("v1", 5, 0)]), \
                mock.patch.object(backfills, "fact_blob_paths", return_value={b"\x01" * 20: "a.c"}), \
                mock.patch.object(facts_ingest, "ingest", return_value={"blobs": 1, "extracted": 1, "rows": 1, "seconds": 0}) as ingest:
            out = backfills.backfill_facts(cnx, mf, 1, 1, families=[3])
        self.assertEqual(out["state"], "done")
        kwargs = ingest.call_args.kwargs
        self.assertEqual(kwargs["families"], [3])
        self.assertEqual(kwargs["read_sha"](b"\x01" * 20), "#include <x.h>\n")

    def test_fact_blob_paths_one_path_per_blob(self) -> None:
        from core import backfills
        answers = {
            "m_bridge_checkpoint": [[(1, 10), (2, 11), (3, 12)], [(1, 10), (4, 13)]],
        }
        cur = mock.MagicMock()
        calls = {"cp": 0}

        def execute(sql, params=()):
            if "m_bridge_checkpoint" in sql:
                cur.rows = answers["m_bridge_checkpoint"][calls["cp"]]
                calls["cp"] += 1
            elif "m_file_name" in sql:
                names = {1: "kernel/a.c", 2: "Makefile", 3: "Documentation/x.rst", 4: "drivers/Kconfig"}
                cur.rows = [(f, names[f]) for f in params]
            else:
                cur.rows = [(b, bytes([b]) * 20) for b in params]

        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: cur.rows
        cur.query.side_effect = lambda sql, params=(), dictionary=False: (execute(sql, params), cur.rows)[1]
        out = backfills.fact_blob_paths(cur, [("v1", 1, 0), ("v2", 2, 1)])
        self.assertEqual(out, {bytes([10]) * 20: "kernel/a.c", bytes([12]) * 20: "Documentation/x.rst",
                               bytes([13]) * 20: "drivers/Kconfig"})


if __name__ == "__main__":
    unittest.main()
