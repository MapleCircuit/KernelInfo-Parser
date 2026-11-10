"""tests/test_webapp_kbuild_usage.py - Kbuild rules with their CONFIG_ symbols (m_kbuild_symbol, D06).

Rows built by parser/kbuild_symbols.py (as Step 6.6 and --backfill-kbuild store them) give the webapp's
`kbuild_usage()` the same rules as the Git path, on a temporary repository.
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

from core import ingest_state as I
from core.DBLayout import TABLES
from parser import kbuild_symbols as ks
from parser.kbuild_parser import KbuildParser
from webapp.backend.services import kconfig_service as kcs

MAKEFILE = """\
obj-$(CONFIG_FOO) += foo.o
obj-$(CONFIG_FOO) += rusty.o
obj-$(CONFIG_FOO) += sub/
ifdef CONFIG_FOO
obj-y += cond.o
endif
obj-$(CONFIG_BAR) += bar.o
"""


class TestRows(unittest.TestCase):
    def test_rule_and_condition_rows(self) -> None:
        bindings = KbuildParser().parse_makefile_content(MAKEFILE, dir_path="drivers/x")
        rows = ks.binding_rows(bindings, {"drivers/x/rusty.rs": 9}.get)
        by_target = {}
        for idx, line, symbol, via, mode, kind, obj, target, fnid, rule in rows:
            by_target.setdefault(target, []).append((symbol, via))
            if target.endswith("rusty.rs"):
                self.assertEqual(fnid, 9)                    # .rs variant resolved
        self.assertIn(("FOO", ks.VIA_RULE), by_target["drivers/x/foo.c"])
        self.assertEqual(sorted(by_target["drivers/x/cond.c"]), [("FOO", ks.VIA_CONDITION), ("y", ks.VIA_RULE)])
        self.assertIn(ks.KIND_DIRECTORY, {r[5] for r in rows})

    def test_file_without_bindings_gets_a_marker(self) -> None:
        self.assertEqual(ks.binding_rows([], {}.get), [(ks.MARKER_IDX, 0, "", ks.VIA_NONE, 0, 0, "", "", 0, "")])

    def test_rust_target_exists(self) -> None:
        self.assertEqual(ks.resolve_target("a/b.c", ks.KIND_OBJECT, {"a/b.rs"}), ("a/b.rs", True))
        self.assertEqual(ks.resolve_target("a/b.c", ks.KIND_OBJECT, {"a/b.S", "a/b.rs"}), ("a/b.S", True))
        self.assertEqual(ks.resolve_target("a/sub/Makefile", ks.KIND_DIRECTORY, {"a/sub/x.c"}), ("a/sub/", True))

    def test_rollback_and_blob_derived(self) -> None:
        marks = {t.table_name: 10 for t, _ in I.marked_tables(TABLES)}
        plan = {(d.table, d.where) for d in I.rollback_plan(TABLES, marks, 10, [3])}
        self.assertIn(("m_kbuild_symbol", "source_fnid > %s"), plan)
        self.assertIn(("m_kbuild_symbol", "source_blob_id IN (%s)"), plan)   # rows of a re-parsed blob

    def test_migration_deletes_only_kbuild_and_makefile_rows(self) -> None:
        from core import backfills
        db = mock.MagicMock()
        counts = iter([50_000, 1234])
        db.execute.side_effect = lambda sql, params=(): next(counts)
        self.assertEqual(backfills.delete_old_kbuild_references(db, chunk=50_000), 51_234)
        sqls = {c.args[0] for c in db.execute.call_args_list}
        self.assertEqual(sqls, {"DELETE FROM m_file_reference WHERE ref_type IN (3, 4) LIMIT %s"})

    def test_parser_stages_no_kbuild_file_references(self) -> None:
        import inspect
        import main
        src = inspect.getsource(main.processing_kbuild)
        self.assertIn("m_kbuild_symbol.table_id", src)
        self.assertNotIn("_stage_file_reference", src)


class TestUsageOnRepository(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = Path(tempfile.mkdtemp(prefix="kbuild-"))
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
               "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@x"}
        cls.env = env
        files = {
            "drivers/x/Makefile": MAKEFILE,
            "drivers/x/foo.c": "int foo;\n",
            "drivers/x/rusty.rs": "fn x() {}\n",
            "drivers/x/cond.c": "int c;\n",
            "drivers/x/sub/s.c": "int s;\n",
            "drivers/Makefile": "obj-y += x/\n",
            "Kbuild": "# nothing here\n",
        }
        for path, text in files.items():
            (cls.dir / path).parent.mkdir(parents=True, exist_ok=True)
            (cls.dir / path).write_text(text)
        cls.git("init", "-q", "-b", "master")
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", "tree")
        cls.sha = cls.git("rev-parse", "HEAD").strip()
        cls.tree = {line.split("\t")[1]: line.split()[2] for line in cls.git("ls-tree", "-r", "HEAD").splitlines()}

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def git(cls, *args: str) -> str:
        return subprocess.run(["git", "-C", str(cls.dir), *args], check=True, capture_output=True, text=True, env=cls.env).stdout

    def setUp(self) -> None:
        kcs._USAGE_CACHE.clear()
        kcs._PATHS_CACHE.clear()
        kcs._KBUILD_COVERAGE.clear()
        self.svc = kcs.KconfigService()
        self.patches = [
            mock.patch.object(kcs, "get_repo_dir", lambda: self.dir),
            mock.patch.object(kcs.KconfigService, "_read", staticmethod(
                lambda rev, path: (self.dir / path).read_text() if (self.dir / path).is_file() else None)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def _stored(self, skip: str | None = None) -> tuple[list[dict], dict]:
        """m_kbuild_symbol rows of the tree's Makefiles (as Step 6.6 stores them), path -> (blob, fnid)."""
        parser = KbuildParser()
        fnid_of = {p: i + 1 for i, p in enumerate(sorted(self.tree))}
        pair_of = {p: (100 + fnid_of[p], fnid_of[p]) for p in self.tree if ks.is_kbuild_file(p) and p != skip}
        rows = []
        for path, (blob, fnid) in pair_of.items():
            text = (self.dir / path).read_text()
            for idx, line, symbol, via, mode, kind, obj, target, tfnid, rule in ks.binding_rows(
                    parser.parse_makefile_content(text, dir_path=os.path.dirname(path)), fnid_of.get):
                rows.append({"source_blob_id": blob, "source_fnid": fnid, "idx": idx, "symbol": symbol, "via": via,
                             "line_no": line, "compile_mode": mode, "kind": kind, "target_obj": obj,
                             "target_path": target, "rule": rule})
        return rows, pair_of

    def _db(self, rows: list[dict], pair_of: dict, covered: bool = True):
        statements: list[str] = []

        class Cur:
            def __init__(self) -> None:
                self.rows: list = []

            def execute(self, sql: str, params: tuple = ()) -> None:
                statements.append(sql)
                self.rows = [r for r in rows if r["symbol"] == params[0] and r["via"] in (1, 2)]

            def fetchall(self):
                return self.rows

        ctx = mock.MagicMock()
        ctx.__enter__.return_value = Cur()
        pairs = {v: k for k, v in pair_of.items()} if covered else None
        return [
            mock.patch.object(kcs, "get_db_cursor", return_value=ctx),
            mock.patch.object(kcs.git_tree_resolver, "resolve_commit_sha", return_value=self.sha),
            mock.patch.object(kcs.KconfigService, "_kbuild_pairs_covered", staticmethod(lambda cur, sha: pairs)),
        ], statements

    def test_database_and_git_give_the_same_rules(self) -> None:
        rows, pair_of = self._stored()
        patches, statements = self._db(rows, pair_of)
        for name in ("FOO", "BAR", "NOPE"):
            git = self.svc._kbuild_usage_git(self.sha, name)
            with patches[0], patches[1], patches[2]:
                db = self.svc._kbuild_usage_db(self.sha, name)
            self.assertEqual(db, git, name)
        foo = {r["path"]: r for r in self.svc._kbuild_usage_git(self.sha, "FOO")}
        self.assertTrue(foo["drivers/x/rusty.rs"]["exists"])                 # B6: Rust objects exist
        self.assertEqual(foo["drivers/x/cond.c"]["via"], "condition")
        self.assertEqual(foo["drivers/x/sub/"]["kind"], "directory")
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in statements))

    def test_not_covered_falls_back_to_git(self) -> None:
        rows, pair_of = self._stored(skip="drivers/x/Makefile")
        patches, _ = self._db(rows, pair_of, covered=False)
        with patches[0], patches[1], patches[2]:
            self.assertIsNone(self.svc._kbuild_usage_db(self.sha, "FOO"))

    def test_coverage_needs_every_makefile_pair(self) -> None:
        cursor = mock.MagicMock()
        fnids = {p: i + 1 for i, p in enumerate(sorted(self.tree))}
        mk = [p for p in self.tree if ks.is_kbuild_file(p)]
        meta = {self.tree[p]: (100 + fnids[p], 1) for p in mk}
        stored = [{"source_blob_id": 100 + fnids[p], "source_fnid": fnids[p]} for p in mk]
        answers = {"FROM m_file_name": [{"fnid": fnids[p], "fname": p} for p in mk]}

        def execute(sql, params=()):
            cursor.rows = answers["FROM m_file_name"] if "FROM m_file_name" in sql else cursor.stored

        cursor.execute.side_effect = execute
        cursor.fetchall.side_effect = lambda: cursor.rows
        resolver = kcs.git_tree_resolver
        with mock.patch.object(resolver, "list_tree", return_value=self.tree), \
                mock.patch.object(resolver, "map_blob_hashes", return_value=meta):
            cursor.stored = stored[:-1]
            self.assertIsNone(kcs.KconfigService._kbuild_pairs_covered(cursor, self.sha))
            cursor.stored = stored
            got = kcs.KconfigService._kbuild_pairs_covered(cursor, self.sha)
        self.assertEqual(sorted(got.values()), sorted(mk))


if __name__ == "__main__":
    unittest.main()
