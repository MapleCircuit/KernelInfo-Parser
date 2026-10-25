"""tests/test_symbol_rows.py - Symbol definitions/references stored once per tag (core/SymbolRows.py).

Parses real files at two versions (the second one reusing the first one's tags) under both storage layouts and
checks that the m_symbol_def / m_symbol_ref view rows are identical, that reused tags write no shared rows, that
symbol keys do not depend on how far the parser resolved a target, the view reconstruction rules, the refusal of
a database with the per-blob tables, and repository-relative paths of headers resolved without a ChangeSet.
"""
import collections
import hashlib
import os
import shutil
import subprocess
import tempfile
import types
import unittest

import main
from core.config import get_default_repo_path
from core.DBLayout import init_db_layout, m_v_main
from core.FileHandler import MasterFile
from core.globalstuff import OP_REF, REF_POS, G
from core.GreatProcessor import GreatProcessor
from core.SymbolRows import (
    DEF_ID_FLAG,
    KIND_DEFS,
    KIND_REFS,
    STATS,
    SymbolRowCollector,
    _KeyContext,
    symbol_view_rows,
    synthetic_def_id,
)
from core.TableHandling import ChangeSet
from db_engine import MockDB
from parser.c_ast.c_ast import c_ast_parse
from parser.c_ast.cursor_tree import clear_tu_caches, get_rel_file_for_cursor
from table_engine import TECachedDB
from unit_test.parser_harness import stage_file_prelude

REPO = str(get_default_repo_path())
SYMBOL_TABLES = ("m_blob_symbol_def", "m_blob_symbol_ref", "m_tag_def", "m_tag_ref", "m_blob_symbol", "m_symbol_own")


def _eager_ref(orig_ref):
    """CS.ref() that returns an id for every positional route, like a parse against a database that already holds
    the file's previous version (the in-memory database leaves them as routes). Equal content gets one id, as
    content-addressed rows would."""
    def ref(self, query, *route_args):
        val = orig_ref(self, query, *route_args)
        if type(val) is tuple and len(val) == 3 and val[1] == OP_REF and val[2] and val[2][0] == REF_POS:
            fingerprint = _KeyContext(self.cs, SymbolRowCollector()).fingerprint(val)
            return 10_000_000 + int.from_bytes(hashlib.blake2b(repr(fingerprint).encode(), digest_size=3).digest(), "big")
        return val
    return ref


def _parse_two_versions(path: str, versions: tuple[str, str], layout: str, eager_second: bool = False) -> dict:
    """Parse `path` as added at versions[0] and modified at versions[1] (single-file workspaces).

    With `eager_second`, the second version's targets are AST ids (`_eager_ref()`); it is parsed but not executed.
    """
    saved = (G.DB, G.TE, G.MF, G.SYMBOL_LAYOUT)
    MockDB.reset()
    G.DB = MockDB
    G.SYMBOL_LAYOUT = layout
    te = TECachedDB(hugepages="off")
    G.TE = te
    gp = GreatProcessor()
    init_db_layout(gp)
    te.start(gp.Table_Array, MockDB)
    mf = MasterFile()
    G.MF = mf
    dirs = []
    second_version_rows = {}
    try:
        for vid, (op, version) in enumerate((("A", versions[0]), ("M", versions[1])), start=1):
            d = tempfile.mkdtemp(prefix="symrows_")
            dirs.append(d)
            content = subprocess.check_output(["git", "-C", REPO, "show", f"{version}:{path}"])
            os.makedirs(os.path.dirname(os.path.join(d, path)), exist_ok=True)
            with open(os.path.join(d, path), "wb") as f:
                f.write(content)
            mf.version_dict[version] = d
            gp.Version_Name, gp.VID, gp.Old_VID = version, vid, vid - 1
            cs = ChangeSet(f"{op}\t{path}")
            cs.gp, cs.mf, cs.current_vid = gp, mf, vid
            cs.blob_hash = hashlib.sha1(content).digest()
            G.CURRENT_PARSING_FILE = path
            # The file's m_file_name operation first, as in main.default_processing() (target_fnid routes point at it)
            stage_file_prelude(cs, gp)
            cs.store(m_v_main.set(vid, version))
            before = {t: len(MockDB._global_store.get(t, {})) for t in SYMBOL_TABLES}
            if eager_second and op == "M":
                orig_ref = ChangeSet.ref
                ChangeSet.ref = _eager_ref(orig_ref)
                try:
                    c_ast_parse(cs)
                finally:
                    ChangeSet.ref = orig_ref
                return {}
            c_ast_parse(cs)
            if not cs.execute():
                raise AssertionError(f"{path} at {version} did not execute")
            te.commit_all()
            second_version_rows = {t: len(MockDB._global_store.get(t, {})) - before[t] for t in SYMBOL_TABLES}
        store = MockDB._global_store
        return {
            "views": {v: collections.Counter(r[1:] for r in symbol_view_rows(store, v)) for v in ("m_symbol_def", "m_symbol_ref")},
            "second": second_version_rows,
        }
    finally:
        te.close()
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
        clear_tu_caches()
        G.CURRENT_PARSING_FILE = None
        G.CURRENT_PARSING_DIR = None
        G.DB, G.TE, G.MF, G.SYMBOL_LAYOUT = saved
        MockDB.reset()


class TestLayoutsAgree(unittest.TestCase):
    """Both layouts must give the views the same rows; the shared one writes fewer rows for a changed file."""

    CASES = (
        ("include/linux/lockd/bind.h", ("v2.6.28", "v2.6.29")),
        ("kernel/fork.c", ("v2.6.12", "v2.6.13")),
    )

    def test_views_are_identical_and_reused_tags_are_not_rewritten(self) -> None:
        for path, versions in self.CASES:
            with self.subTest(path=path):
                per_blob = _parse_two_versions(path, versions, "blob")
                STATS.clear()
                shared = _parse_two_versions(path, versions, "tag")
                self.assertEqual(per_blob["views"], shared["views"])
                self.assertTrue(sum(per_blob["views"]["m_symbol_ref"].values()) > 0)
                self.assertGreater(STATS["refs reused"] + STATS["defs reused"], 0, "the second version reuses tags")
                if path == "kernel/fork.c":
                    self.assertLess(sum(shared["second"].values()), sum(per_blob["second"].values()))


class TestKeysIgnoreResolution(unittest.TestCase):
    """A reused tag's items must compare equal whether its targets are routes or already AST ids."""

    def test_resolved_targets_keep_the_stored_keys(self) -> None:
        for path, versions in (("include/asm-ppc/mmu.h", ("v2.6.12-rc2", "v2.6.12-rc3")), ("kernel/fork.c", ("v2.6.12", "v2.6.13"))):
            with self.subTest(path=path):
                STATS.clear()
                routes = _parse_two_versions(path, versions, "tag")
                changed = STATS["refs changed reused tag"] + STATS["defs changed reused tag"]
                self.assertGreater(STATS["refs reused"] + STATS["defs reused"], 0)
                STATS.clear()
                _parse_two_versions(path, versions, "tag", eager_second=True)
                self.assertEqual(STATS["refs changed reused tag"] + STATS["defs changed reused tag"], changed)
                self.assertGreater(STATS["refs reused"] + STATS["defs reused"], 0)
                self.assertTrue(routes["views"]["m_symbol_def"])


class TestViewRows(unittest.TestCase):
    """symbol_view_rows(): the in-memory equivalent of the m_symbol_def / m_symbol_ref views."""

    def tables(self, own: tuple = ()) -> dict:
        return {
            "m_bridge_tag": [(7, 30, 100, 110, 1, 1), (8, 30, 200, 210, 1, 1)],
            "m_tag_def": [(30, 0, 111, "foo", 21, 1, 0, 10)],
            "m_tag_ref": [(30, 0, 222, 3, 4, 9)],
            "m_blob_symbol": [(7, 111, 500, 0), (7, 222, 600, 0), (8, 111, 501, 0), (8, 222, 601, 5)],
            "m_symbol_own": list(own),
            "m_blob_symbol_def": [(1, 8, 30, 777, "foo", 21, 1, 201, 211)],
            "m_blob_symbol_ref": [],
        }

    def test_shared_rows_follow_each_placement(self) -> None:
        defs = symbol_view_rows(self.tables(), "m_symbol_def")
        refs = symbol_view_rows(self.tables(), "m_symbol_ref")
        self.assertIn((synthetic_def_id(7, 30, 0), 7, 30, 500, "foo", 21, 1, 100, 110), defs)
        self.assertIn((synthetic_def_id(8, 30, 0), 8, 30, 501, "foo", 21, 1, 200, 210), defs)
        self.assertEqual(sorted(refs), [(None, 7, 30, 600, 0, 3, 104, 9), (None, 8, 30, 601, 5, 3, 204, 9)])
        self.assertTrue(synthetic_def_id(7, 30, 0) & DEF_ID_FLAG)

    def test_own_placements_use_their_per_blob_rows(self) -> None:
        defs = symbol_view_rows(self.tables(own=((8, 30, KIND_DEFS),)), "m_symbol_def")
        self.assertEqual([r for r in defs if r[1] == 8], [(1, 8, 30, 777, "foo", 21, 1, 201, 211)])
        refs = symbol_view_rows(self.tables(own=((8, 30, KIND_REFS),)), "m_symbol_ref")
        self.assertEqual([r[1] for r in refs], [7])


class _LegacyCursor:
    """Like mysql-connector 8.0's MySQLCursor: no context manager protocol, only close()."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def execute(self, sql: str, params=None) -> None:
        self.sql = sql

    def fetchall(self) -> list:
        return [(n,) for n in self.names]


class TestOldLayoutGuard(unittest.TestCase):
    def test_database_with_per_blob_tables_is_refused(self) -> None:
        cursors: list[_LegacyCursor] = []

        def cursor() -> _LegacyCursor:
            cursors.append(_LegacyCursor(["m_symbol_def", "m_symbol_ref"]))
            return cursors[-1]

        db = types.SimpleNamespace(cnx=types.SimpleNamespace(cursor=cursor))
        with self.assertRaises(RuntimeError) as ctx:
            main.ensure_db_views(db)
        self.assertIn("--reset", str(ctx.exception))
        self.assertTrue(all(c.closed for c in cursors))


class TestRelativePaths(unittest.TestCase):
    def test_header_path_without_changeset_uses_the_parsed_version(self) -> None:
        saved = (G.MF, G.CURRENT_PARSING_DIR)
        try:
            mf = MasterFile()
            mf.version_dict["v2.6.12"] = "/dev/shm/code-parser.abc"
            G.MF = mf
            G.CURRENT_PARSING_DIR = "/dev/shm/code-parser.abc"
            cursor = types.SimpleNamespace(location=types.SimpleNamespace(file=types.SimpleNamespace(name="/dev/shm/code-parser.abc/include/linux/slab_def.h")))
            self.assertEqual(get_rel_file_for_cursor(None, cursor), "include/linux/slab_def.h")
        finally:
            G.MF, G.CURRENT_PARSING_DIR = saved


if __name__ == "__main__":
    unittest.main()
