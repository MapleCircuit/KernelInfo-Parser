"""tests/test_tree_resolver.py - Comprehensive Unit Tests for Dual-Path TreeResolver & 6-Tier Disambiguation."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from core.globalstuff import SymbolLinkage, SymbolRole
from webapp.backend.services.tree_resolver import (
    GitTreeResolver,
    TreeResolverService,
    DisambiguationEngine,
    SymbolRefContext,
    SymbolDefCandidate,
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


class TestDisambiguationEngine(unittest.TestCase):
    """Test suite for the 6-Tier Disambiguation Engine."""

    def test_tier_1_same_blob_and_static_disqualification(self) -> None:
        """Verify Tier 1 awards 100 to same-blob definitions and disqualifies foreign static definitions."""
        ref = SymbolRefContext(
            name="foo",
            source_fnid=1,
            source_blob_id=10,
            ref_ast_id=50,
            source_path="kernel/sched/core.c",
        )

        cand_local = SymbolDefCandidate(
            def_id=1,
            blob_id=10,  # same blob
            fnid=1,
            file_path="kernel/sched/core.c",
            name="foo",
            type_id=1,
            ast_id=50,
            linkage=SymbolLinkage.InternalStaticDef,
        )

        cand_foreign_static = SymbolDefCandidate(
            def_id=2,
            blob_id=20,  # different blob
            fnid=2,
            file_path="kernel/fork.c",
            name="foo",
            type_id=1,
            ast_id=50,
            linkage=SymbolLinkage.InternalStaticDef,
        )

        score_local = DisambiguationEngine.score_candidate(ref, cand_local)
        self.assertEqual(score_local, 100.0)

        score_foreign_static = DisambiguationEngine.score_candidate(ref, cand_foreign_static)
        self.assertEqual(score_foreign_static, -1.0)

        ranked = DisambiguationEngine.rank_candidates(ref, [cand_local, cand_foreign_static])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0][1].def_id, 1)

    def test_tier_2_compiler_target_fnid_provenance(self) -> None:
        """Verify Tier 2 scores 95.0 when cand.fnid matches compiler-verified ref.target_fnid."""
        ref = SymbolRefContext(
            name="schedule",
            source_fnid=1,
            source_blob_id=10,
            ref_ast_id=100,
            target_fnid=7,  # include/linux/sched.h
            source_path="kernel/sched/core.c",
        )

        cand_verified = SymbolDefCandidate(
            def_id=10,
            blob_id=70,
            fnid=7,  # matches target_fnid
            file_path="include/linux/sched.h",
            name="schedule",
            type_id=1,
            ast_id=100,
            linkage=SymbolLinkage.PrototypeDecl,
        )

        cand_unverified = SymbolDefCandidate(
            def_id=11,
            blob_id=80,
            fnid=8,
            file_path="drivers/net/other.h",
            name="schedule",
            type_id=1,
            ast_id=100,
            linkage=SymbolLinkage.PrototypeDecl,
        )

        ranked = DisambiguationEngine.rank_candidates(ref, [cand_unverified, cand_verified])
        self.assertEqual(ranked[0][0], 95.0)
        self.assertEqual(ranked[0][1].def_id, 10)

    def test_tier_3_include_graph_closure(self) -> None:
        """Verify Tier 3 scores 88.0 for direct includes (depth=1) and 82.0 for transitive includes."""
        ref = SymbolRefContext(
            name="kfree",
            source_fnid=1,
            source_blob_id=10,
            ref_ast_id=200,
            source_path="mm/slab.c",
        )

        cand_direct = SymbolDefCandidate(
            def_id=1,
            blob_id=20,
            fnid=2,
            file_path="include/linux/slab.h",
            name="kfree",
            type_id=1,
            ast_id=200,
            include_depth=1,
        )

        cand_transitive = SymbolDefCandidate(
            def_id=2,
            blob_id=30,
            fnid=3,
            file_path="include/linux/types.h",
            name="kfree",
            type_id=1,
            ast_id=200,
            include_depth=3,
        )

        self.assertEqual(DisambiguationEngine.score_candidate(ref, cand_direct), 88.0)
        self.assertEqual(DisambiguationEngine.score_candidate(ref, cand_transitive), 82.0)

    def test_tier_4_kbuild_compilation_unit_and_directory_proximity(self) -> None:
        """Verify Tier 4 scores candidates in same Kbuild compilation unit and awards directory bonus."""
        ref = SymbolRefContext(
            name="e1000_init",
            source_fnid=10,
            source_blob_id=100,
            ref_ast_id=300,
            source_path="drivers/net/ethernet/intel/e1000/e1000_main.c",
            kbuild_target_obj="drivers/net/ethernet/intel/e1000/e1000.o",
        )

        cand_same_unit = SymbolDefCandidate(
            def_id=1,
            blob_id=101,
            fnid=11,
            file_path="drivers/net/ethernet/intel/e1000/e1000_hw.c",
            name="e1000_init",
            type_id=1,
            ast_id=300,
            kbuild_target_obj="drivers/net/ethernet/intel/e1000/e1000.o",
        )

        cand_other_dir = SymbolDefCandidate(
            def_id=2,
            blob_id=200,
            fnid=20,
            file_path="sound/pci/e1000.c",
            name="e1000_init",
            type_id=1,
            ast_id=300,
            kbuild_target_obj="sound/pci/snd.o",
        )

        ranked = DisambiguationEngine.rank_candidates(ref, [cand_other_dir, cand_same_unit])
        self.assertGreater(ranked[0][0], 75.0)
        self.assertEqual(ranked[0][1].def_id, 1)

    def test_tier_5_structural_ast_and_arch_affinity(self) -> None:
        """Verify Tier 5 scores exact structural AST match and architecture affinity."""
        ref = SymbolRefContext(
            name="setup_arch",
            source_fnid=1,
            source_blob_id=10,
            ref_ast_id=500,
            source_path="init/main.c",
            target_arch="x86",
        )

        cand_x86 = SymbolDefCandidate(
            def_id=1,
            blob_id=50,
            fnid=5,
            file_path="arch/x86/kernel/setup.c",
            name="setup_arch",
            type_id=1,
            ast_id=500,
        )

        cand_arm = SymbolDefCandidate(
            def_id=2,
            blob_id=60,
            fnid=6,
            file_path="arch/arm/kernel/setup.c",
            name="setup_arch",
            type_id=1,
            ast_id=500,
        )

        score_x86 = DisambiguationEngine.score_candidate(ref, cand_x86)
        score_arm = DisambiguationEngine.score_candidate(ref, cand_arm)
        self.assertEqual(score_x86, 70.0)  # 65 + 5 arch bonus
        self.assertEqual(score_arm, 65.0)

    def test_tier_6_weak_linkage_penalty_and_export_symbol_boost(self) -> None:
        """Verify Tier 6 penalizes weak linkage and boosts EXPORT_SYMBOL definitions."""
        ref = SymbolRefContext(
            name="do_something",
            source_fnid=1,
            source_blob_id=10,
            ref_ast_id=600,
        )

        cand_strong_exported = SymbolDefCandidate(
            def_id=1,
            blob_id=70,
            fnid=7,
            file_path="kernel/foo.c",
            name="do_something",
            type_id=1,
            ast_id=999,  # different AST shape
            linkage=SymbolLinkage.ExternalDef,
            has_export_symbol=True,
        )

        cand_weak = SymbolDefCandidate(
            def_id=2,
            blob_id=80,
            fnid=8,
            file_path="kernel/bar.c",
            name="do_something",
            type_id=1,
            ast_id=999,
            linkage=SymbolLinkage.WeakOrAliasDef,
            has_export_symbol=False,
        )

        score_strong = DisambiguationEngine.score_candidate(ref, cand_strong_exported)
        score_weak = DisambiguationEngine.score_candidate(ref, cand_weak)

        self.assertEqual(score_strong, 58.0)  # 55 + 3
        self.assertEqual(score_weak, 45.0)    # 55 - 10


if __name__ == "__main__":
    unittest.main()
