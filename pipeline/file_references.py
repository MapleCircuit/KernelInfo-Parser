"""File-reference extraction (kbuild, docs, includes)."""
from core.globalstuff import (
    G,
    COLOR,
    ASTT,
    FileRefType,
)
import os

import time
from collections.abc import Callable
from typing import Any
from db_engine import (
    NewPairs,
)
from core.DBLayout import (
    m_kbuild_symbol,
)
from pipeline.changesets import get_fid_for_path, get_fnid_for_path
from pipeline.git_network import feature_enabled
from pipeline.state import MF, gp, logger




def _reference_sources(
    all_files: list[str],
    wanted: Callable[[str], bool],
    new_pairs: set[tuple[int, int]] | None,
) -> list[tuple[str, int, int]]:
    """(path, fnid, blob_id) of the version's files selected by `wanted` whose references are recomputed.

    `new_pairs` holds the (fnid, blob_id) pairs new in this version (`db.new_checkpoint_pairs()`); None
    selects every file.
    """
    sources = []
    for path in all_files:
        if not wanted(path):
            continue
        fnid, blob_id = get_fnid_for_path(path), get_fid_for_path(path)
        if fnid is None or blob_id is None or (new_pairs is not None and (fnid, blob_id) not in new_pairs):
            continue
        sources.append((path, fnid, blob_id))
    return sources


_FILE_REFERENCE_COLUMNS = ("source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details")
# Rows per INSERT transaction of the documentation references
_DOC_INSERT_CHUNK = 5_000
# Step 6.6 transactions re-run on a transient conflict (deadlock, lock wait timeout, AUTO-INC lock failure)
_TX_ATTEMPTS = 5
_TX_BACKOFF_S = 0.5


def _collect_file_reference(
    staged_file_refs: set[tuple[int, int, int, int, int]],
    rows: list[tuple[Any, ...]],
    blob_id: int,
    fnid: int,
    target_fnid: int,
    ref_type: int,
    line_no: int,
    details: str,
) -> bool:
    """Append one m_file_reference row (without `ref_id`) of the source file (fnid, blob_id) unless already
    collected; True if appended."""
    key = (blob_id, fnid, target_fnid, ref_type, line_no)
    if key in staged_file_refs:
        return False
    staged_file_refs.add(key)
    rows.append((blob_id, fnid, target_fnid, ref_type, line_no, details[:255]))
    return True


def _insert_file_references(db: Any, rows: list[tuple[Any, ...]]) -> int:
    """Insert m_file_reference rows with the key omitted (AUTO_INCREMENT assigns `ref_id`, `Table.db_assigned_ids`),
    skipping rows whose unique key exists; each chunk is one transaction re-run on a transient conflict."""
    for i in range(0, len(rows), _DOC_INSERT_CHUNK):
        chunk = rows[i : i + _DOC_INSERT_CHUNK]
        db.run_transaction(
            lambda chunk=chunk: db.insert_rows("m_file_reference", list(_FILE_REFERENCE_COLUMNS), chunk, on_conflict="ignore"),
            max_attempts=_TX_ATTEMPTS,
            backoff_s=_TX_BACKOFF_S,
            label="m_file_reference documentation insert",
        )
    return len(rows)


def _version_file_list(version: str, purpose: str) -> list[str]:
    """Paths of the version's tree (empty when Git cannot list it)."""
    try:
        file_list_raw = MF.git_file_list(gp.Version_Name)
    except Exception as e:
        logger.debug(f"Failed to get git_file_list for {purpose} in {version}: {e}")
        return []
    return file_list_raw.splitlines() if file_list_raw else []


def processing_kbuild(
    version: str,
    staged_file_refs: set[tuple[int, int, int, int, int]] | None = None,
    new_pairs: set[tuple[int, int]] | None = None,
) -> None:
    """Parse the version's Makefile and Kbuild files and stage their rules with the CONFIG_ symbols that
    govern them (m_kbuild_symbol, parser/kbuild_symbols.py).

    Rows are keyed by the Makefile's (blob, path), since the targets resolve against its directory: only
    the (fnid, blob_id) pairs in `new_pairs` (None: every file) are parsed, and a Makefile carried over
    at the same path keeps the rows computed when it first appeared there. Every binding is kept, also
    when its target file does not exist. `staged_file_refs` is unused (kept for callers). Skipped for a project
    that is not a kernel tree (`kernel_steps_apply()`: the man-pages' GNUmakefile is no Kbuild file).
    """
    if gp.VID == 0 or not feature_enabled("kconfig"):
        return
    from parser.kbuild_parser import KbuildParser
    from parser import kbuild_symbols
    kbuild_parser = KbuildParser()

    all_files = _version_file_list(version, "kbuild parsing")
    targets = _reference_sources(all_files, kbuild_symbols.is_kbuild_file, new_pairs)
    if not targets:
        return
    logger.info(
        f"Processing {len(targets)} Makefile/Kbuild files for version '{version}' "
        f"(of {sum(map(kbuild_symbols.is_kbuild_file, all_files))} total)..."
    )

    row_count = 0
    for mk_path, mk_fnid, mk_blob in targets:
        if G.is_deadline_exceeded():
            logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during Kbuild processing. Stopping early..."))
            break
        try:
            # An empty file still gets its marker row (kbuild_symbols.binding_rows), so the pair counts as processed
            content = MF.get_file(mk_path, gp.Version_Name) or ""
            bindings = kbuild_parser.parse_makefile_content(content, dir_path=os.path.dirname(mk_path)) if content else []
            for row in kbuild_symbols.binding_rows(bindings, get_fnid_for_path):
                idx, line, symbol, via, mode, kind, target_obj, target_path, target_fnid, rule = row
                G.TE.set(
                    m_kbuild_symbol.table_id,
                    (mk_blob, mk_fnid, idx, symbol, via, line, mode, kind, target_obj, target_path, target_fnid, rule),
                )
                row_count += 1
        except Exception as e:
            logger.debug(f"Error parsing kbuild file '{mk_path}': {e}")

    logger.info(f"Staged {row_count} Kbuild rule rows (m_kbuild_symbol) for version '{version}'.")


def processing_doc_references(
    version: str,
    staged_file_refs: set[tuple[int, int, int, int, int]] | None = None,
    new_pairs: set[tuple[int, int]] | None = None,
    db: Any = None,
) -> int:
    """Scan documentation and text files for active version and map cross-file references into m_file_reference.

    Rows are keyed by the document's (blob, path) like every m_file_reference row (the scan also skips
    references to the document's own path); `new_pairs` limits the scan to the (fnid, blob_id) pairs new
    in this version (None: all). The rows are written through `db` (default: a `G.DB()` session) with
    `ref_id` omitted, so the database is the only allocator of the key (`Table.db_assigned_ids`), and
    rows whose unique key exists are skipped. Returns the rows sent.
    """
    if gp.VID == 0 or not feature_enabled("docs"):
        return 0
    if staged_file_refs is None:
        staged_file_refs = set()

    from parser.doc_parser import DocReferenceScanner
    scanner = DocReferenceScanner()

    all_files = _version_file_list(version, "doc reference parsing")
    docs = _reference_sources(all_files, scanner.is_doc_candidate, new_pairs)
    if not docs:
        return 0

    logger.info(f"Processing {len(docs)} documentation/text files for version '{version}'...")

    fnid_by_path: dict[str, int] = {}
    for p in all_files:
        fnid = get_fnid_for_path(p)
        if fnid is not None:
            fnid_by_path[p] = fnid

    rows: list[tuple[Any, ...]] = []
    doc_type = int(FileRefType.Documentation)
    for doc_p, doc_fnid, doc_blob in docs:
        if G.is_deadline_exceeded():
            logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during doc references processing. Stopping early..."))
            break
        try:
            content = MF.get_file(doc_p, gp.Version_Name)
            if not content:
                continue
            for r in scanner.scan_file_content(content, doc_blob, fnid_by_path, source_path=doc_p):
                _collect_file_reference(staged_file_refs, rows, doc_blob, doc_fnid, r.target_fnid, doc_type, int(r.line_no), r.details)
        except Exception as e:
            logger.debug(f"Error parsing documentation references in '{doc_p}': {e}")

    if not rows:
        return 0
    if db is None:
        with G.DB() as own_db:
            _insert_file_references(own_db, rows)
    else:
        _insert_file_references(db, rows)
    logger.info(f"Wrote {len(rows)} documentation cross-file references into m_file_reference for version '{version}'.")
    return len(rows)


def processing_include_references(version: str, new_pairs: NewPairs) -> None:
    """Batch populate C/ASM and Kconfig include references into m_file_reference using a set-based copy.

    Only the (path, blob) pairs new in this version (absent from gp.Old_VID's checkpoint) are processed:
    their previous rows were cleared by processing_file_references(), and carried-over pairs keep theirs.
    Every path holding the blob gets its own rows (source_fnid), with the targets of the blob's AST.
    `new_pairs`: the version's new pairs (`db.new_checkpoint_pairs()`), shared with the clearing deletes so the
    anti-join of the two checkpoints is computed once.
    """
    if gp.VID == 0:
        return
    kconfig_types = (int(ASTT.Kconfig_Source.value), int(ASTT.Kconfig_Rsource.value), int(ASTT.Kconfig_Osource.value))
    inserted = new_pairs.insert_include_references(kconfig_types, int(FileRefType.Kconfig), int(FileRefType.Include))
    logger.info(f"Synchronized {inserted} include/Kconfig references into m_file_reference for version '{version}'.")


_FILE_REFERENCE_TABLES = ("m_file_reference", "m_kbuild_symbol")


def processing_file_references(version: str) -> None:
    """Extract and consolidate all cross-file references (Kbuild, Makefiles, Documentation, Includes, Kconfig).

    m_file_reference / m_kbuild_symbol rows are keyed by the source file's (blob, path) pair
    (`source_blob_id`, `source_fnid`), so only the pairs new in this version are cleared and recomputed;
    pairs carried over keep their rows. The new pairs are computed once by `db.new_checkpoint_pairs()`; the
    clearing deletes and the include copy reuse them (the engine decides how they are held).

    Every m_file_reference row is written by the database with `ref_id` omitted (`Table.db_assigned_ids`):
    AUTO_INCREMENT is the single id authority across distributed workers. Each write runs in its own short
    transaction (`run_transaction()`), re-run on a transient conflict; the clearing deletes commit before the
    include copy, so no worker holds the deletes' row and gap locks while it waits for the AUTO-INC lock of the
    `INSERT ... SELECT`. A step that still fails raises: the version is not finished, and its rerun clears and
    recomputes the same pairs (every write is pair-scoped and idempotent).
    """
    if gp.VID == 0:
        return

    # The caller (pipeline/update.py Step 6.5) already committed the parent tables (m_file_blob, m_file_name,
    # m_bridge_checkpoint) before this step, so no commit is repeated here.
    with G.DB() as pairs_db:
        started = time.perf_counter()
        with pairs_db.new_checkpoint_pairs(gp.VID, gp.Old_VID or 0) as new_pairs:
            # 1. Clear the rows of the new pairs (one transaction per table)
            for table in _FILE_REFERENCE_TABLES:
                pairs_db.run_transaction(
                    lambda table=table: new_pairs.delete_rows_of(table),
                    max_attempts=_TX_ATTEMPTS,
                    backoff_s=_TX_BACKOFF_S,
                    label=f"{table} clearing delete",
                )
            logger.info(f"[Step 6.6] New (path, blob) pairs computed once and cleared in {time.perf_counter() - started:.1f}s.")
            logger.info(f"Recomputing file references for {len(new_pairs.pairs)} (path, blob) pairs new in version '{version}'.")

            # 2. Set-based include / Kconfig references
            pairs_db.run_transaction(
                lambda: processing_include_references(version, new_pairs),
                max_attempts=_TX_ATTEMPTS,
                backoff_s=_TX_BACKOFF_S,
                label="m_file_reference include copy",
            )

            # Rows collected in this run (rows of other pairs were cleared or are not recomputed)
            staged_file_refs: set[tuple[int, int, int, int, int]] = set()

            # 3. Kbuild rules are staged into the TableEngine (m_kbuild_symbol, committed by the caller's
            # commit_all); documentation references are written directly (fnids come from the committed TE cache)
            processing_kbuild(version, staged_file_refs=staged_file_refs, new_pairs=new_pairs.pairs)
            processing_doc_references(version, staged_file_refs=staged_file_refs, new_pairs=new_pairs.pairs, db=pairs_db)
