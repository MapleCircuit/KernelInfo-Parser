"""Requested and automatic derived-table backfills."""
from core.globalstuff import (
    G,
)
from core import ingest_state
from core.config import (
    get_parser_config,
)

import argparse
import contextlib
from pipeline.git_network import kernel_steps_apply
from pipeline.state import MF, logger




def run_requested_backfills(args: argparse.Namespace, network_id: int, repo_id: int) -> bool:
    """Run the backfills named on the command line (core/backfills.py); returns whether any was requested
    (the run then exits). Standalone and master roles only."""
    from core import backfills
    requested = False
    with G.DB() as db:
        if getattr(args, "backfill_maintainer_files", False):
            requested = True
            summary = backfills.backfill_maintainer_files(db, MF, network_id, repo_id, getattr(args, "backfill_from", None))
            logger.info(f"[Backfill maintainer files] {summary}")
        if getattr(args, "backfill_kbuild", False):
            requested = True
            if db is not None:
                backfills.delete_old_kbuild_references(db)
            summary = backfills.backfill_kbuild(db, MF)
            logger.info(f"[Backfill Kbuild] {summary}")
        if getattr(args, "backfill_facts", False):
            requested = True
            try:
                workers = max(1, min(int(get_parser_config().get("facts_workers", 4)), G.CPUS or 1))
            except (TypeError, ValueError):
                workers = 4
            summary = backfills.backfill_facts(db, MF, network_id, repo_id, getattr(args, "facts_families", None), workers)
            logger.info(f"[Backfill facts] {summary}")
        if getattr(args, "backfill_commit_relations", False):
            requested = True
            summary = backfills.backfill_commit_relations(db, str(MF.repo_dir), getattr(args, "from_commit_id", None))
            logger.info(f"[Backfill commit relations] {summary}")
        if getattr(args, "repair_head_commit_files", False):
            requested = True
            summary = backfills.repair_head_commit_files(db, str(MF.repo_dir), network_id)
            logger.info(f"[Repair head commit files] {summary}")
        if getattr(args, "backfill_doc_references", False):
            requested = True
            try:
                workers = max(1, min(int(get_parser_config().get("facts_workers", 4)), G.CPUS or 1))
            except (TypeError, ValueError):
                workers = 4
            summary = backfills.backfill_doc_references(db, MF, network_id, repo_id, workers)
            logger.info(f"[Backfill doc references] {summary}")
        if getattr(args, "backfill_cve_files", False):
            requested = True
            try:
                workers = max(1, min(int(get_parser_config().get("facts_workers", 4)), G.CPUS or 1))
            except (TypeError, ValueError):
                workers = 4
            summary = backfills.backfill_cve_files(db, MF, network_id, repo_id, workers)
            logger.info(f"[Backfill CVE files] {summary}")
        if getattr(args, "backfill_mailing_list", False):
            requested = True
            commit_range = getattr(args, "ml_range", None)
            storage_dir = getattr(args, "ml_storage_dir", None)
            summary = backfills.backfill_mailing_list(db, str(MF.repo_dir), commit_range, storage_dir)
            logger.info(f"[Backfill mailing list] {summary}")
    return requested


def run_automatic_backfills(args: argparse.Namespace) -> None:
    """One-off backfills of tables that are empty while the database holds finished versions (the table was
    just added; core/API.md section 12), unless --no-backfill. Standalone and master roles only."""
    from core import backfills
    from parser.git_ast.commit_relations import BACKFILL_DONE
    with G.DB() as db:
        if db is None:
            return
        try:
            relations_state = backfills.commit_relations_state(db)
            finished = db.row_exists("m_ingest_state", {"status": ingest_state.INGEST_DONE})
            kbuild_empty = finished and not db.row_exists("m_kbuild_symbol")
        except Exception as exc:  # noqa: BLE001 - never blocks parsing
            logger.debug(f"Automatic backfill check failed: {exc}")
            return
        if not relations_state.startswith(BACKFILL_DONE):
            # Until the state row says done, the webapp keeps reading its own relation index (webapp/API.md 2.16)
            if getattr(args, "no_backfill", False):
                logger.info("[Backfill commit relations] Stored commits are not covered yet; skipped (--no-backfill). "
                            "Run --backfill-commit-relations to fill m_commit_relation / m_commit_link.")
            else:
                logger.info("[Backfill commit relations] Parsing the stored commit messages once into m_commit_relation / "
                            "m_commit_link (--no-backfill skips this)" + (f", resuming after {relations_state}." if relations_state else "."))
                backfills.backfill_commit_relations(db, str(MF.repo_dir))
        if not finished:
            return
        # The Kbuild backfill reads the Makefiles of this run's repository: none for a project that is not a kernel tree
        if kbuild_empty and kernel_steps_apply():
            if getattr(args, "no_backfill", False):
                logger.info("[Backfill Kbuild] m_kbuild_symbol is empty; skipped (--no-backfill). Run --backfill-kbuild to fill it.")
            else:
                logger.info("[Backfill Kbuild] m_kbuild_symbol is empty: filling it once from the stored Makefiles (--no-backfill skips this).")
                with contextlib.suppress(Exception):
                    backfills.delete_old_kbuild_references(db)
                backfills.backfill_kbuild(db, MF)
