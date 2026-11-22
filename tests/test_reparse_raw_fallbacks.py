"""tests/test_reparse_raw_fallbacks.py - `--reparse-raw-fallbacks [--dry-run]` (core/backfills.py
`reparse_raw_fallbacks()`): structured-type blobs whose only tags are raw_ast whole-file tags."""
from __future__ import annotations

import unittest
from unittest import mock

from core import backfills, ingest_state
from core.DBLayout import TABLES
from core.globalstuff import ASTT, T_ASM, T_C, T_KCONFIG, T_RAW, T_RUST
from db_engine.spec import gt

RAW = int(ASTT.Raw_Content)
STRUCT = int(ASTT.C_Comment)

# blob_id: (ftype, [ast type of each tag])
BLOBS = {
    1: (T_C, [RAW]),             # raw fallback of a C file -> found
    2: (T_RAW, [RAW]),           # a genuine raw file -> ignored
    3: (T_C, [STRUCT, STRUCT]),  # a normally parsed C file -> ignored
    4: (T_C, []),                # an empty / unreadable C file (no tags) -> ignored
    5: (T_ASM, [RAW, STRUCT]),   # raw tag next to structured ones -> ignored
    6: (T_KCONFIG, [RAW]),       # raw fallback of a Kconfig file -> found
    7: (T_RUST, [RAW]),          # rust_ast's own whole-file tag (rustc gave no AST) -> ignored
}
PATHS = {1: "arch/m68k/kernel/a.c", 2: "README", 3: "fs/b.c", 4: "fs/empty.c", 5: "arch/x86/c.S", 6: "arch/m68k/Kconfig", 7: "rust/x.rs"}
V1, V2, V3 = 1000, 1001, 1002


def _db(engine: str):
    if engine == "sqlite":
        from db_engine import SQLiteDB
        db = SQLiteDB(":memory:", foreign_keys=False)
        db.create_table(list(TABLES))
    else:
        from db_engine import MockDB
        db = MockDB(use_global=False)
    ast_id = tag_id = 100
    for blob_id, (ftype, types) in BLOBS.items():
        db.insert_rows("m_file_blob", ["blob_id", "blob_hash", "ftype"], [(blob_id, bytes([blob_id]) * 20, ftype)])
        db.insert_rows("m_file_name", ["fnid", "fname"], [(blob_id, PATHS[blob_id])])
        for i, type_id in enumerate(types):
            ast_id += 1
            tag_id += 1
            db.insert_rows("m_ast", ["ast_id", "name", "type_id"], [(ast_id, f"n{ast_id}", type_id)])
            db.insert_rows("m_tag", ["tag_id", "hash", "ast_id", "hl_s", "hl_l"], [(tag_id, bytes([tag_id]) * 32, ast_id, 0, 0)])
            db.insert_rows("m_bridge_tag", ["blob_id", "tag_id", "line_s", "line_e", "char_s", "char_e"],
                           [(blob_id, tag_id, 1 + i, 9, 1, 1)])
            db.insert_rows("m_bridge_commit_tag", ["commit_id", "blob_id", "tag_id"], [(V1, blob_id, tag_id)])
        commits = (V1, V2) if blob_id == 1 else (V2,)
        db.insert_rows("m_bridge_checkpoint", ["commit_id", "fnid", "blob_id"], [(c, blob_id, blob_id) for c in commits])
        db.insert_rows("m_blob_symbol", ["blob_id", "sym_key", "target_ast_id"], [(blob_id, 7, 0)])
        db.insert_rows("m_file_reference", ["ref_id", "source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details"],
                       [(blob_id, blob_id, blob_id, 3, 3, 1, "")])
    for i, (cid, name) in enumerate(((V1, "v3.0"), (V2, "v3.1"))):
        db.insert_rows("m_git_ref", ["ref_id", "repo_id", "ref_type", "ref_name", "commit_id"], [(i + 1, 1, 1, name, cid)])
        db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"],
                       [(1, cid, ingest_state.INGEST_DONE, 10 + i)])
    return db


def _count(db, table: str, where: dict) -> int:
    return db.count_rows(table, where)


class TestReparseRawFallbacks(unittest.TestCase):

    def each_engine(self, check) -> None:
        """Run `check(db)` on a fresh SQLiteDB(":memory:") and a fresh MockDB."""
        for engine in ("sqlite", "mock"):
            with self.subTest(engine=engine):
                check(_db(engine))

    def test_detection_finds_only_raw_only_structured_blobs(self) -> None:
        def check(db) -> None:
            self.assertEqual(backfills.raw_fallback_blobs(db), [(1, T_C), (6, T_KCONFIG)])
            self.assertEqual(backfills.raw_fallback_blobs(db, [1, 2, 3]), [(1, T_C)])
            self.assertEqual(backfills.raw_fallback_blobs(db, []), [])
        self.each_engine(check)

    def test_dry_run_reports_and_changes_nothing(self) -> None:
        def check(db) -> None:
            before = {t: _count(db, t, {}) for t in ("m_bridge_tag", "m_blob_symbol", "m_bridge_checkpoint", "m_file_blob")}
            summary = backfills.reparse_raw_fallbacks(db, 1, 1, dry_run=True)
            self.assertEqual(summary["state"], "dry_run")
            self.assertEqual(summary["blobs"], 2)
            self.assertEqual(summary["reset"], 0)
            first = summary["items"][0]
            self.assertEqual(first, {"blob_id": 1, "ftype": "C", "paths": [PATHS[1]], "checkpoints": 2,
                                     "first_version": "v3.0", "latest_version": "v3.1"})
            self.assertEqual(summary["items"][1]["ftype"], "KCONFIG")
            self.assertEqual({t: _count(db, t, {}) for t in before}, before)
        self.each_engine(check)

    def test_execute_resets_parse_products_and_keeps_blob_rows(self) -> None:
        def check(db) -> None:
            summary = backfills.reparse_raw_fallbacks(db, 1, 1)
            self.assertEqual(summary["state"], "done")
            self.assertEqual(summary["reset"], 2)
            for blob_id in (1, 6):
                self.assertEqual(_count(db, "m_bridge_tag", {"blob_id": blob_id}), 0)
                self.assertEqual(_count(db, "m_blob_symbol", {"blob_id": blob_id}), 0)
                # Blob, checkpoints, commit tags, content-derived references and the shared tags stay
                self.assertEqual(_count(db, "m_file_blob", {"blob_id": blob_id}), 1)
                self.assertGreater(_count(db, "m_bridge_checkpoint", {"blob_id": blob_id}), 0)
                self.assertEqual(_count(db, "m_bridge_commit_tag", {"blob_id": blob_id}), 1)
                self.assertEqual(_count(db, "m_file_reference", {"source_blob_id": blob_id}), 1)
            self.assertEqual(_count(db, "m_tag", {"tag_id": gt(0)}), sum(len(t) for _, t in BLOBS.values()))
            # Every other blob is untouched
            for blob_id in (2, 3, 5, 7):
                self.assertEqual(_count(db, "m_bridge_tag", {"blob_id": blob_id}), len(BLOBS[blob_id][1]))
                self.assertEqual(_count(db, "m_blob_symbol", {"blob_id": blob_id}), 1)
            self.assertEqual(_count(db, "m_blob_symbol", {"blob_id": 4}), 1)
            # Idempotent: nothing matches any more
            again = backfills.reparse_raw_fallbacks(db, 1, 1)
            self.assertEqual((again["state"], again["blobs"], again["reset"]), ("done", 0, 0))
        self.each_engine(check)

    def test_execute_is_refused_while_a_version_is_being_ingested(self) -> None:
        def check(db) -> None:
            db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"],
                           [(1, V3, ingest_state.INGEST_PARSING, 20)])
            summary = backfills.reparse_raw_fallbacks(db, 1, 1)
            self.assertEqual(summary["state"], "deferred")
            self.assertEqual(_count(db, "m_bridge_tag", {"blob_id": 1}), 1)
        self.each_engine(check)

    def test_blob_rows_plan_matches_rollback_and_honours_keep(self) -> None:
        plan = ingest_state.blob_rows_plan(TABLES, [1, 2])
        tables = {d.table for d in plan}
        self.assertTrue({"m_bridge_tag", "m_blob_symbol", "m_file_reference", "m_maintainer_member"} <= tables)
        self.assertFalse(tables & {"m_bridge_checkpoint", "m_commit_file", "m_bridge_commit_tag", "m_file_blob"})
        kept = {d.table for d in ingest_state.blob_rows_plan(TABLES, [1], keep=backfills.RAW_FALLBACK_KEEP)}
        self.assertFalse(kept & set(backfills.RAW_FALLBACK_KEEP))
        rollback = ingest_state.rollback_plan(TABLES, {}, V1, [1, 2])
        self.assertTrue(set(plan) <= set(rollback))


class TestReparseRawFallbacksCli(unittest.TestCase):

    def test_flag_dispatches_with_dry_run(self) -> None:
        from pipeline import backfill_steps, cli

        for argv, dry in ((["--reparse-raw-fallbacks", "--dry-run"], True), (["--reparse-raw-fallbacks"], False)):
            with self.subTest(argv=argv):
                args = cli.build_arg_parser().parse_args(argv)
                from db_engine import MockDB
                with mock.patch.object(backfill_steps.G, "DB", MockDB), \
                        mock.patch.object(backfills, "reparse_raw_fallbacks", return_value={"state": "done", "items": []}) as run:
                    self.assertTrue(backfill_steps.run_requested_backfills(args, 1, 1))
                run.assert_called_once()
                self.assertEqual(run.call_args.args[1:], (1, 1))
                self.assertEqual(run.call_args.kwargs, {"dry_run": dry})
        self.assertIs(backfills.BACKFILL_HANDLERS["reparse_raw_fallbacks"], backfills.reparse_raw_fallbacks)


if __name__ == "__main__":
    unittest.main()
