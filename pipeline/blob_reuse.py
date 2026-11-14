"""Reuse of already-parsed blobs across versions and parent process memory logging."""
from core.globalstuff import (
    G,
    COLOR,
    type_check,
    T_ASM,
)
from core import ingest_state
import os
import time

from collections.abc import Iterable
from db_engine import (
    Join,
)
from core.DBLayout import (
    m_file_name,
    m_bridge_checkpoint,
    m_file_blob,
)
from pipeline.changesets import file_fid_cache, file_fnid_cache
from pipeline.state import gp, logger




_SQL_IN_CHUNK = 1000


def _person_key(name: str | None, email: str | None) -> tuple[str, str]:
    """
    Identity of an m_maintainer_person row: `(name, email)`, the TableEngine's no-duplicate key
    (`parser/git_ast/identity.py` `person_key`, shared with the release statistics step).

    Both halves are normalized (`clean_identity_text`): a commit trailer carrying a stray control
    byte — `Olof Johansson\\x01 <olof@lixom.net\\x01>` in `a929478f` — otherwise keys a second
    identity for a contributor who already has one.
    """
    from parser.git_ast.identity import person_key
    return person_key(name, email)


def _parent_rss_mb() -> float:
    """Resident set size of this process in MB (0 when /proc is unavailable)."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except Exception:
        return 0.0


def _log_preload_summary(elapsed_s: float) -> None:
    """Log how long the TableEngine preload took and which cached tables are largest."""
    cached_rows = getattr(G.TE, "_cached_rows", None) or {}
    sizes = []
    for table_id, rows in cached_rows.items():
        try:
            sizes.append((len(rows), G.TE.tables[table_id].table_name))
        except Exception:
            continue
    largest = ", ".join(f"{name} {count:,}" for count, name in sorted(sizes, reverse=True)[:5]) or "none"
    logger.info(f"[TableEngine] Preloaded cached tables in {elapsed_s:.1f}s (parent RSS {_parent_rss_mb():.0f}MB); largest: {largest}")


def _tagged_blob_ids(blob_ids: Iterable[int]) -> set[int]:
    """The subset of `blob_ids` that already has tags in m_bridge_tag (their parse results are complete)."""
    wanted = sorted(set(blob_ids))
    tagged: set[int] = set()
    if not wanted:
        return tagged
    with G.DB() as db:
        try:
            for i in range(0, len(wanted), _SQL_IN_CHUNK):
                chunk = wanted[i : i + _SQL_IN_CHUNK]
                chunk_tagged = {
                    int(b) for b in db.select_values("m_bridge_tag", "blob_id", {"blob_id": chunk}, distinct=True)
                }
                if chunk_tagged:
                    # Blobs of a version still being ingested may be only partly tagged
                    try:
                        in_flight = {
                            int(b) for b in db.select_values(
                                ("m_ingest_blob", "ib"), "ib.blob_id",
                                {"s.status": 1, "ib.blob_id": sorted(chunk_tagged)},
                                joins=[Join(("m_ingest_state", "s"), {"s.commit_id": "ib.commit_id"})],
                                distinct=True,
                            )
                        }
                        chunk_tagged.difference_update(in_flight)
                    except Exception as exc:  # noqa: BLE001 - without the in-flight check the tagged blobs are trusted
                        logger.warning(f"_tagged_blob_ids in-flight check failed ({exc}); trusting the tagged blobs.")
                tagged.update(chunk_tagged)
        except Exception as exc:
            logger.debug(f"_tagged_blob_ids query failed: {exc}")
    return tagged


def _blob_types(fpath: str) -> tuple[int, ...]:
    """File types an m_file_blob row of `fpath`'s blob can be stored under: the name-based one, and for a
    `.c`/`.h` path the content-aware one (`ChangeSet.get_file_type()`: T_ASM for a header written in assembly)."""
    return (type_check(fpath), T_ASM) if fpath.endswith((".c", ".h")) else (type_check(fpath),)


def _known_blob_id(fpath: str, b_hash: bytes) -> int | None:
    """Id of an existing m_file_blob row for a path's blob, else None (every type of `_blob_types()` is tried)."""
    for ftype in _blob_types(fpath):
        row = G.TE.get(m_file_blob.table_id, (None, b_hash, ftype))
        if row is not None:
            return row[0]
    return None


def _prefetch_known_blobs(items: Iterable[str], file_blob_hashes: dict[str, bytes]) -> int:
    """Announce to the TableEngine the m_file_blob keys `_known_blob_id()` is about to look up for the change
    lines `items`, so they are resolved with a few chunked reads instead of one read per file and type.

    Returns the number of paths announced."""
    rows: list[tuple] = []
    paths = 0
    for item in items:
        if not item:
            continue
        parts = item.split("\t")
        if parts[0].startswith("D"):
            continue
        b_hash = file_blob_hashes.get(parts[-1])
        if b_hash:
            paths += 1
            rows.extend((None, b_hash, ftype) for ftype in _blob_types(parts[-1]))
    if rows:
        G.TE.prefetch_nodup(m_file_blob.table_id, rows)
    return paths


def _reuse_parsed_blobs(file_blob_hashes: dict[str, bytes]) -> list[str]:
    """Step 4.5: link changed files whose exact blob was already parsed instead of parsing them again.

    Blobs and file names are looked up in the TableEngine caches (preloaded by `G.TE.start()`), tagged
    blobs are confirmed with one targeted query, and the reused files' m_file_name and m_bridge_checkpoint
    rows are staged through the TableEngine. Reused paths are recorded in `gp.deduped_paths` so
    `processing_git_commits()` can still link their tags to this range's commits. No m_commit_file row is
    written for the version's head commit: those rows are the files each commit touched (Step 6.5), and a
    version-diff row on the head commit made a tag commit list every reused file of its version.

    Returns:
        The change lines that still need parsing, in `gp.Change_List` order.
    """
    t_lookup = time.perf_counter()
    checked = _prefetch_known_blobs(gp.Change_List or (), file_blob_hashes)
    candidates: dict[str, tuple[str, str, int]] = {}
    for item in gp.Change_List or ():
        if not item:
            continue
        parts = item.split("\t")
        op, fpath = parts[0], parts[-1]
        if op.startswith("D"):
            continue
        b_hash = file_blob_hashes.get(fpath)
        if not b_hash:
            continue
        blob_id = _known_blob_id(fpath, b_hash)
        if blob_id is not None:
            candidates[item] = (op, fpath, blob_id)

    logger.info(
        f"[Step 4.5] {checked} changed file(s) checked against m_file_blob in {time.perf_counter() - t_lookup:.1f}s "
        f"({len(candidates)} blob(s) already stored)."
    )
    tagged = _tagged_blob_ids(b_id for _op, _path, b_id in candidates.values())
    # Checkpoint rows of this version from an interrupted earlier run (one query instead of one per file)
    checkpointed: set[int] | None = None
    if tagged:
        with G.DB() as db:
            try:
                checkpointed = {int(f) for f in db.select_values("m_bridge_checkpoint", "fnid", {"commit_id": gp.VID})}
            except Exception as exc:  # noqa: BLE001 - falls back to one TableEngine lookup per file
                logger.warning(f"Could not read the checkpoint rows of commit {gp.VID} ({exc}); checking per file.")
    reused: set[str] = set()
    gp.deduped_paths = set()
    for item, (op, fpath, b_id) in candidates.items():
        if b_id not in tagged:
            continue
        fnid = G.TE.set(m_file_name.table_id, (None, fpath))[0]
        if checkpointed is not None:
            is_checkpointed = fnid in checkpointed
            checkpointed.add(fnid)
        else:
            is_checkpointed = G.TE.get(m_bridge_checkpoint.table_id, (gp.VID, fnid, None)) is not None
        if not is_checkpointed:
            G.TE.set(m_bridge_checkpoint.table_id, (gp.VID, fnid, b_id))
        gp.file_blob_ids[fpath] = b_id
        file_fid_cache[fpath] = b_id
        file_fnid_cache[fpath] = fnid
        gp.deduped_paths.add(fpath)
        reused.add(item)

    if reused:
        logger.info(COLOR.green(f"Content-Addressable Deduplication: Reused {len(reused)} existing file blobs directly without worker parsing."))
    return [item for item in (gp.Change_List or ()) if item not in reused]


def _record_reparsed_blobs(file_blob_hashes: dict[str, bytes]) -> None:
    """Record blobs that already exist (unparsed blobs of intermediate commits) and are about to be parsed,
    so an interruption can clear their partial rows (core/ingest_state.py)."""
    blob_ids = set()
    items = [*(gp.worker_change_list or ()), *(getattr(gp, "Symlink_List", None) or ())]
    # Mostly answered by the lookups of _reuse_parsed_blobs(); only keys it did not see are read
    _prefetch_known_blobs(items, file_blob_hashes)
    for item in items:
        if not item:
            continue
        parts = item.split("\t")
        if parts[0].startswith("D"):
            continue
        b_hash = file_blob_hashes.get(parts[-1])
        if b_hash:
            blob_id = _known_blob_id(parts[-1], b_hash)
            if blob_id is not None:
                blob_ids.add(blob_id)
    if blob_ids:
        with G.DB() as db:
            if db is not None:
                ingest_state.record_blobs(db, gp.VID, blob_ids)
