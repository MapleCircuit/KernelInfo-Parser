"""tests/test_tree_resolver.py - Unit Tests for the Dual-Path TreeResolver, Git tree resolver & arch-aware definition ranking."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from core.globalstuff import SymbolLinkage
from webapp.backend.services.tree_resolver import (
    GitTreeResolver,
    TreeResolverService,
    arch_bits,
    best_path,
    definition_rank,
    path_arch_group,
)


class TestTreeResolverService(unittest.TestCase):
    """Test suite for TreeResolverService (Path A single-file and Path B full-tree resolution)."""

    def setUp(self) -> None:
        self.resolver = TreeResolverService(max_cache_size=4)
        self.mock_cursor = MagicMock()

    def test_path_a_direct_checkpoint_lookup(self) -> None:
        """Verify Path A returns baseline blob from checkpoint when no deltas intervened."""
        # Commit 100 is a checkpoint with fnid=5 -> blob_id=42
        def execute_side_effect(sql: str, params: tuple | list) -> None:
            if "FROM m_bridge_checkpoint WHERE commit_id = %s LIMIT 1" in sql:
                self.mock_cursor.fetchone.return_value = {"1": 1}
            elif "FROM m_bridge_checkpoint" in sql and "fnid = %s" in sql:
                self.mock_cursor.fetchone.return_value = {"blob_id": 42}
            else:
                self.mock_cursor.fetchone.return_value = None

        self.mock_cursor.execute.side_effect = execute_side_effect

        blob_id = self.resolver.resolve_file_blob(commit_id=100, fnid=5, cursor=self.mock_cursor)
        self.assertEqual(blob_id, 42)

    def test_path_a_delta_override(self) -> None:
        """Verify Path A correctly returns updated blob_id from m_commit_file deltas."""
        # Commit 102 has parent 101, parent of 101 is checkpoint 100
        # In commit 102, fnid 5 was modified to blob 99
        parents = {102: 101, 101: 100}

        def execute_side_effect(sql: str, params: tuple | list) -> None:
            if "FROM m_bridge_checkpoint WHERE commit_id = %s LIMIT 1" in sql:
                cid = params[0]
                self.mock_cursor.fetchone.return_value = {"1": 1} if cid == 100 else None
            elif "FROM m_commit_parent" in sql:
                cid = params[0]
                p = parents.get(cid)
                self.mock_cursor.fetchone.return_value = {"parent_commit_id": p} if p else None
            elif "FROM m_commit_file" in sql:
                self.mock_cursor.fetchall.return_value = [
                    {"commit_id": 102, "blob_id": 99, "change_type": "M"}
                ]
            else:
                self.mock_cursor.fetchone.return_value = None

        self.mock_cursor.execute.side_effect = execute_side_effect

        blob_id = self.resolver.resolve_file_blob(commit_id=102, fnid=5, cursor=self.mock_cursor)
        self.assertEqual(blob_id, 99)

    def test_path_a_file_deletion_returns_none(self) -> None:
        """Verify Path A returns None when file was deleted in intermediate commit."""
        parents = {101: 100}

        def execute_side_effect(sql: str, params: tuple | list) -> None:
            if "FROM m_bridge_checkpoint WHERE commit_id = %s LIMIT 1" in sql:
                cid = params[0]
                self.mock_cursor.fetchone.return_value = {"1": 1} if cid == 100 else None
            elif "FROM m_commit_parent" in sql:
                self.mock_cursor.fetchone.return_value = {"parent_commit_id": 100}
            elif "FROM m_commit_file" in sql:
                self.mock_cursor.fetchall.return_value = [
                    {"commit_id": 101, "blob_id": 0, "change_type": "D"}
                ]
            else:
                self.mock_cursor.fetchone.return_value = None

        self.mock_cursor.execute.side_effect = execute_side_effect

        blob_id = self.resolver.resolve_file_blob(commit_id=101, fnid=5, cursor=self.mock_cursor)
        self.assertIsNone(blob_id)

    def test_path_b_full_tree_assembly_and_lru_caching(self) -> None:
        """Verify Path B constructs full tree by applying deltas to checkpoint and caches result."""
        # Checkpoint 10: {1: 101, 2: 102, 3: 103}
        # Commit 11 (parent 10): modifies 2 -> 202, deletes 3, adds 4 -> 404
        parents = {11: 10}

        def execute_side_effect(sql: str, params: tuple | list) -> None:
            if "FROM m_bridge_checkpoint WHERE commit_id = %s LIMIT 1" in sql:
                cid = params[0]
                self.mock_cursor.fetchone.return_value = {"1": 1} if cid == 10 else None
            elif "FROM m_commit_parent" in sql:
                self.mock_cursor.fetchone.return_value = {"parent_commit_id": 10}
            elif "SELECT fnid, blob_id FROM m_bridge_checkpoint" in sql:
                self.mock_cursor.fetchall.return_value = [
                    {"fnid": 1, "blob_id": 101},
                    {"fnid": 2, "blob_id": 102},
                    {"fnid": 3, "blob_id": 103},
                ]
            elif "FROM m_commit_file" in sql:
                self.mock_cursor.fetchall.return_value = [
                    {"commit_id": 11, "fnid": 2, "blob_id": 202, "change_type": "M"},
                    {"commit_id": 11, "fnid": 3, "blob_id": 0, "change_type": "D"},
                    {"commit_id": 11, "fnid": 4, "blob_id": 404, "change_type": "A"},
                ]
            else:
                self.mock_cursor.fetchone.return_value = None

        self.mock_cursor.execute.side_effect = execute_side_effect

        tree = self.resolver.resolve_tree(commit_id=11, cursor=self.mock_cursor)
        self.assertEqual(tree, {1: 101, 2: 202, 4: 404})
        self.assertNotIn(3, tree)

        # LRU cache verification: subsequent call should hit cache without querying DB
        self.mock_cursor.execute.reset_mock()
        cached_tree = self.resolver.resolve_tree(commit_id=11, cursor=self.mock_cursor)
        self.assertEqual(cached_tree, {1: 101, 2: 202, 4: 404})
        self.mock_cursor.execute.assert_not_called()


class TestGitTreeBlobPaths(unittest.TestCase):
    """GitTreeResolver.blob_paths and the commit-scoped symbol search built on it."""

    SHA = "a" * 40
    TREE = {"init/main.c": "1" * 40, "mm/slab.c": "2" * 40, "copy/main.c": "1" * 40, "new.c": "3" * 40}

    def _resolver(self) -> GitTreeResolver:
        resolver = GitTreeResolver(repo_dir="/nonexistent")
        resolver.resolve_commit_sha = MagicMock(return_value=self.SHA)
        resolver.list_tree = MagicMock(return_value=self.TREE)
        # new.c ("3"*40) was never parsed, so it has no blob_id
        resolver.map_blob_hashes = MagicMock(return_value={"1" * 40: (10, 1), "2" * 40: (20, 1)})
        return resolver

    def test_blob_paths_groups_paths_and_caches(self) -> None:
        resolver = self._resolver()
        paths = resolver.blob_paths(MagicMock(), "deadbeef")
        self.assertEqual(paths, {10: ["copy/main.c", "init/main.c"], 20: ["mm/slab.c"]})
        resolver.blob_paths(MagicMock(), "deadbeef")
        resolver.map_blob_hashes.assert_called_once()

    def test_blob_paths_unknown_revision(self) -> None:
        resolver = self._resolver()
        resolver.resolve_commit_sha = MagicMock(return_value=None)
        self.assertIsNone(resolver.blob_paths(MagicMock(), "nope"))

    def test_symbol_search_scoped_to_commit_tree(self) -> None:
        from unittest.mock import patch
        from webapp.backend.services import symbol_service as ss

        cursor = MagicMock()
        state: dict = {}

        def execute(sql: str, params=None) -> None:
            state["sql"] = sql

        def fetchall() -> list:
            if "SELECT s.def_id, s.blob_id" in state["sql"]:
                # blob 99 belongs to another version and must be dropped
                return [{"def_id": 1, "blob_id": 99}, {"def_id": 2, "blob_id": 10}, {"def_id": 3, "blob_id": 20}]
            return [
                {"def_id": d, "name": "start_kernel", "type_id": 1, "type_name": "C_function", "fid": b, "line_s": 5, "line_e": 9, "ast_id": 7}
                for d, b in ((2, 10), (3, 20))
            ]

        cursor.execute.side_effect = execute
        cursor.fetchall.side_effect = fetchall
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor
        with patch.object(ss, "get_db_cursor", return_value=ctx), \
             patch.object(ss, "get_version_info", return_value=(None, "")), \
             patch.object(ss.git_tree_resolver, "blob_paths", return_value={10: ["init/main.c"], 20: ["mm/slab.c"]}):
            results = ss.SymbolService().search_symbols("0123abcd", q="start_kernel", limit=10)
        self.assertEqual([(r["def_id"], r["file_path"]) for r in results], [(2, "init/main.c"), (3, "mm/slab.c")])


class TestDefinitionRanking(unittest.TestCase):
    """Arch-aware ordering of a symbol's definitions (definition_rank / best_path)."""

    def _order(self, defs: list[tuple[str, int]], srcarch: str = "x86", bits: int | None = 64) -> list[str]:
        return [p for p, _ in sorted(defs, key=lambda d: definition_rank(d[0], d[1], 1, srcarch, bits))]

    def test_own_arch_before_generic_asm_generic_and_other_arches(self) -> None:
        defs = [
            ("arch/alpha/include/asm/ptrace.h", SymbolLinkage.ExternalDef),
            ("include/asm-generic/ptrace.h", SymbolLinkage.ExternalDef),
            ("kernel/ptrace.c", SymbolLinkage.ExternalDef),
            ("arch/x86/include/asm/ptrace.h", SymbolLinkage.ExternalDef),
        ]
        self.assertEqual(self._order(defs), [
            "arch/x86/include/asm/ptrace.h", "kernel/ptrace.c", "include/asm-generic/ptrace.h", "arch/alpha/include/asm/ptrace.h",
        ])
        self.assertEqual(self._order(defs, "alpha", 64)[0], "arch/alpha/include/asm/ptrace.h")

    def test_word_size_and_linkage(self) -> None:
        defs = [
            ("arch/x86/kernel/process_32.c", SymbolLinkage.ExternalDef),
            ("arch/x86/kernel/process_64.c", SymbolLinkage.ExternalDef),
        ]
        self.assertEqual(self._order(defs, bits=64)[0], "arch/x86/kernel/process_64.c")
        self.assertEqual(self._order(defs, bits=32)[0], "arch/x86/kernel/process_32.c")
        # A prototype never wins over a body, even in the own arch tree; weak definitions come after strong ones
        defs = [
            ("arch/x86/include/asm/setup.h", SymbolLinkage.PrototypeDecl),
            ("kernel/weak.c", SymbolLinkage.WeakOrAliasDef),
            ("lib/strong.c", SymbolLinkage.ExternalDef),
        ]
        self.assertEqual(self._order(defs), ["lib/strong.c", "kernel/weak.c", "arch/x86/include/asm/setup.h"])

    def test_best_path_and_arch_helpers(self) -> None:
        self.assertEqual(best_path(["arch/arm/x.h", "arch/x86/x.h", "include/x.h"], "x86", 64), "arch/x86/x.h")
        self.assertEqual(best_path(["arch/x86/x.h", "arch/um/x.h"], "um", None), "arch/um/x.h")
        self.assertIsNone(best_path([], "x86", 64))
        self.assertEqual((arch_bits("x86_64"), arch_bits("i386"), arch_bits("x86"), arch_bits("arm64")), (64, 32, None, 64))
        self.assertEqual(path_arch_group("arch/x86/kernel/setup.c", "um"), 0)

    def test_symbol_detail_picks_the_arch_definition(self) -> None:
        from unittest.mock import patch
        from webapp.backend.services import symbol_service as ss

        rows = [
            {"def_id": d, "ast_id": d, "name": "setup_arch", "type_id": 1, "type_name": "C_function", "fid": b,
             "line_s": 10 * d, "line_e": 10 * d + 5, "tag_id": d, "linkage": 0}
            for d, b in ((1, 10), (2, 20), (3, 30))
        ]
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor
        blob_paths = {10: ["arch/alpha/kernel/setup.c"], 20: ["arch/x86/kernel/setup.c"], 30: ["arch/arm/kernel/setup.c"]}
        with patch.object(ss, "get_db_cursor", return_value=ctx), \
             patch.object(ss.SymbolService, "_scope", return_value=(1, "v3.0", blob_paths)), \
             patch.object(ss, "definition_arch", side_effect=lambda v, a: (a, {"x86_64": "x86"}.get(a, a), 64)), \
             patch.object(ss.SymbolService, "get_symbol_xref", return_value={}):
            x86 = ss.SymbolService().get_symbol_detail("v3.0", "setup_arch", "x86_64")
            arm = ss.SymbolService().get_symbol_detail("v3.0", "setup_arch", "arm")
        self.assertEqual((x86["file_path"], x86["line_s"], x86["arch"]), ("arch/x86/kernel/setup.c", 20, "x86_64"))
        self.assertEqual([d["file"] for d in x86["definitions"]],
                         ["arch/x86/kernel/setup.c", "arch/alpha/kernel/setup.c", "arch/arm/kernel/setup.c"])
        self.assertEqual(arm["file_path"], "arch/arm/kernel/setup.c")


if __name__ == "__main__":
    unittest.main()
