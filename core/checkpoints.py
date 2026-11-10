"""core/checkpoints.py - Checkpoints stored as a delta of an earlier one.

`m_bridge_checkpoint` holds the tree of every ingested version: one `(commit_id, fnid, blob_id)` row per path.
Consecutive versions share nearly all of them (a stable release changes well under 1% of the files), so a
version's full rows are only kept while it is the newest one, which the next run reads as its previous version
(TableEngine preload, `main.processing_unchanges()`, `_new_version_file_pairs()`...):

- `compact()`, at the end of the next version's run, compares the version with the newest full checkpoint
  ingested before it (`choose_base()`). When they differ in at most `ratio` of the version's rows
  (`parser.checkpoint_delta_ratio`, default 0.3), the version keeps only the difference: its rows whose blob
  differs from the base's, plus `blob_id = 0` for the base's paths it lacks, and `m_checkpoint_base` records the
  base. Otherwise the version stays full and becomes the base of the versions after it.
- `expand()` turns a delta back into the full tree. A run whose previous version is a delta (a backfill, a
  repair) expands it first; `expand_dependents()` does the same for the versions resting on a checkpoint that
  is about to be deleted.
- The `m_checkpoint_tree` view (`TREE_VIEW_SQL`, created by `main.ensure_db_views()`) returns the full tree of
  every version, delta or not; filtered by `commit_id` the condition reaches both of its branches. Read through it
  wherever the version may be an older one.

A base is never compacted itself (`dependents()`), so every tree is at most one base plus one delta.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_RATIO = 0.3
INGEST_DONE = 2

TREE_VIEW_SQL = (
    "CREATE OR REPLACE VIEW m_checkpoint_tree AS "
    "SELECT c.commit_id, c.fnid, c.blob_id FROM m_bridge_checkpoint c "
    "WHERE c.blob_id <> 0 OR NOT EXISTS (SELECT 1 FROM m_checkpoint_base b0 WHERE b0.commit_id = c.commit_id) "
    "UNION ALL "
    "SELECT b.commit_id, k.fnid, k.blob_id FROM m_checkpoint_base b "
    "JOIN m_bridge_checkpoint k ON k.commit_id = b.base_commit_id "
    "WHERE NOT EXISTS (SELECT 1 FROM m_bridge_checkpoint d WHERE d.commit_id = b.commit_id AND d.fnid = k.fnid);"
)


def delta_ratio() -> float:
    """`parser.checkpoint_delta_ratio` (0 disables compaction)."""
    try:
        from core.config import get_parser_config
        value = float((get_parser_config() or {}).get("checkpoint_delta_ratio", DEFAULT_RATIO))
    except Exception:  # noqa: BLE001 - no configuration: the default
        value = DEFAULT_RATIO
    return min(max(value, 0.0), 1.0)


def base_of(db: Any, commit_id: int) -> int | None:
    """The checkpoint `commit_id` is a delta of, or None when its rows are its full tree."""
    row = db.query_one("SELECT base_commit_id FROM m_checkpoint_base WHERE commit_id = %s", (commit_id,), dictionary=False)
    return int(row[0]) if row else None


def dependents(db: Any, commit_id: int) -> list[int]:
    """Versions stored as a delta of `commit_id`."""
    rows = db.query("SELECT commit_id FROM m_checkpoint_base WHERE base_commit_id = %s", (commit_id,), dictionary=False)
    return [int(r[0]) for r in rows]


def should_compact(changed: int, removed: int, total: int, ratio: float) -> bool:
    """Keep a delta when it is at most `ratio` of the version's rows (and saves rows at all)."""
    delta = changed + removed
    return ratio > 0 and total > 0 and delta < total and delta <= ratio * total


def is_checkpoint_pinned(db: Any, commit_id: int) -> bool:
    """Whether commit_id is pinned as an ancestor base for an active or pending task or parse run."""
    if not commit_id:
        return False
    try:
        if db.query_one(
            "SELECT 1 FROM m_parse_task WHERE base_commit_id = %s AND status IN (0, 1, 2) LIMIT 1",
            (commit_id,),
            dictionary=False,
        ):
            return True
    except Exception:
        pass
    try:
        if db.query_one(
            "SELECT 1 FROM m_parse_task t JOIN m_git_ref r ON t.base_ref_name = r.ref_name WHERE r.commit_id = %s AND t.status IN (0, 1, 2) LIMIT 1",
            (commit_id,),
            dictionary=False,
        ):
            return True
    except Exception:
        pass
    try:
        if db.query_one(
            "SELECT 1 FROM m_ingest_state WHERE base_commit_id = %s AND status = 1 LIMIT 1",
            (commit_id,),
            dictionary=False,
        ):
            return True
    except Exception:
        pass
    return False


def choose_base(
    db: Any,
    network_id: int,
    commit_id: int,
    exclude: tuple[int, ...] = (),
    preferred_base: int | None = None,
) -> int | None:
    """The newest finished version ingested before `commit_id` whose checkpoint is full (ids grow with ingestion)."""
    skip = tuple(c for c in exclude if c) or (0,)
    if preferred_base and preferred_base not in skip:
        row = db.query_one(
            "SELECT s.commit_id FROM m_ingest_state s LEFT JOIN m_checkpoint_base b ON b.commit_id = s.commit_id "
            "WHERE s.network_id = %s AND s.commit_id = %s AND s.status = %s AND b.commit_id IS NULL",
            (network_id, preferred_base, INGEST_DONE),
            dictionary=False,
        )
        if row:
            return int(row[0])

    placeholders = ",".join(["%s"] * len(skip))
    row = db.query_one(
        "SELECT s.commit_id FROM m_ingest_state s LEFT JOIN m_checkpoint_base b ON b.commit_id = s.commit_id "
        f"WHERE s.network_id = %s AND s.status = %s AND s.commit_id < %s AND b.commit_id IS NULL "
        f"AND s.commit_id NOT IN ({placeholders}) ORDER BY s.commit_id DESC LIMIT 1",
        (network_id, INGEST_DONE, commit_id, *skip),
        dictionary=False,
    )
    return int(row[0]) if row else None


def delta_size(db: Any, commit_id: int, base: int) -> tuple[int, int, int]:
    """(rows of `commit_id` the base lacks or holds another blob for, base paths `commit_id` lacks, rows of `commit_id`)."""
    c_row = db.query_one(
        "SELECT COUNT(*) FROM m_bridge_checkpoint p LEFT JOIN m_bridge_checkpoint k "
        "ON k.commit_id = %s AND k.fnid = p.fnid AND k.blob_id = p.blob_id WHERE p.commit_id = %s AND k.commit_id IS NULL",
        (base, commit_id),
        dictionary=False,
    )
    changed = int(c_row[0]) if c_row else 0
    r_row = db.query_one(
        "SELECT COUNT(*) FROM m_bridge_checkpoint k LEFT JOIN m_bridge_checkpoint p "
        "ON p.commit_id = %s AND p.fnid = k.fnid WHERE k.commit_id = %s AND p.commit_id IS NULL",
        (commit_id, base),
        dictionary=False,
    )
    removed = int(r_row[0]) if r_row else 0
    t_row = db.query_one("SELECT COUNT(*) FROM m_bridge_checkpoint WHERE commit_id = %s", (commit_id,), dictionary=False)
    total = int(t_row[0]) if t_row else 0
    return changed, removed, total


def compact(
    db: Any,
    network_id: int,
    commit_id: int,
    current: int = 0,
    ratio: float | None = None,
    preferred_base: int | None = None,
) -> dict[str, int] | None:
    """Store `commit_id`'s checkpoint as a delta of `choose_base()` when it is small enough (one transaction).

    `current` is the version just ingested (never chosen as the base: it is compacted next). Returns the counts
    when the version was compacted, else None.
    """
    ratio = delta_ratio() if ratio is None else ratio
    if not commit_id or ratio <= 0 or db is None:
        return None
    if base_of(db, commit_id) is not None or dependents(db, commit_id) or is_checkpoint_pinned(db, commit_id):
        return None  # already a delta, the base of others, or pinned by active/pending tasks
    base = choose_base(db, network_id, commit_id, exclude=(current, commit_id), preferred_base=preferred_base)
    if base is None:
        return None
    changed, removed, total = delta_size(db, commit_id, base)
    if not should_compact(changed, removed, total, ratio):
        return None
    try:
        db.execute(
            "INSERT IGNORE INTO m_bridge_checkpoint (commit_id, fnid, blob_id) "
            "SELECT %s, k.fnid, 0 FROM m_bridge_checkpoint k LEFT JOIN m_bridge_checkpoint p "
            "ON p.commit_id = %s AND p.fnid = k.fnid WHERE k.commit_id = %s AND p.commit_id IS NULL",
            (commit_id, commit_id, base),
        )
        # The rows the base holds identically (MariaDB 10.3+ reads a DELETE's own table in a subquery)
        dropped = int(db.execute(
            "DELETE FROM m_bridge_checkpoint WHERE commit_id = %s AND (fnid, blob_id) IN "
            "(SELECT k.fnid, k.blob_id FROM m_bridge_checkpoint k WHERE k.commit_id = %s)",
            (commit_id, base),
        ) or 0)
        db.execute("DELETE FROM m_checkpoint_base WHERE commit_id = %s", (commit_id,))
        db.execute(
            "INSERT INTO m_checkpoint_base (commit_id, base_commit_id, delta_rows) VALUES (%s, %s, %s)",
            (commit_id, base, changed + removed),
        )
        db.commit()
    except Exception:
        if hasattr(db, "rollback"):
            db.rollback()
        raise
    return {"base": base, "changed": changed, "removed": removed, "total": total, "dropped": dropped}


def expand(db: Any, commit_id: int) -> int:
    """Turn the delta checkpoint of `commit_id` back into its full tree (one transaction). Returns the rows restored
    (0 when it was full)."""
    if not commit_id or db is None:
        return 0
    base = base_of(db, commit_id)
    if base is None:
        return 0
    try:
        restored = int(db.execute(
            "INSERT IGNORE INTO m_bridge_checkpoint (commit_id, fnid, blob_id) "
            "SELECT %s, k.fnid, k.blob_id FROM m_bridge_checkpoint k LEFT JOIN m_bridge_checkpoint d "
            "ON d.commit_id = %s AND d.fnid = k.fnid WHERE k.commit_id = %s AND d.commit_id IS NULL",
            (commit_id, commit_id, base),
        ) or 0)
        db.execute("DELETE FROM m_bridge_checkpoint WHERE commit_id = %s AND blob_id = 0", (commit_id,))
        db.execute("DELETE FROM m_checkpoint_base WHERE commit_id = %s", (commit_id,))
        db.commit()
    except Exception:
        if hasattr(db, "rollback"):
            db.rollback()
        raise
    return restored


def expand_dependents(db: Any, commit_id: int) -> list[int]:
    """Expand every version stored as a delta of `commit_id` (before its checkpoint rows are deleted)."""
    if not commit_id or db is None:
        return []
    deps = dependents(db, commit_id)
    for dep in deps:
        expand(db, dep)
    if deps:
        logger.info(f"Expanded {len(deps)} checkpoint(s) stored as deltas of commit_id {commit_id}.")
    return deps


def tree_rows(db: Any, commit_id: int) -> list[tuple[int, int]]:
    """(fnid, blob_id) of a version's full tree, delta or not (what the view returns, without it)."""
    base = base_of(db, commit_id)
    rows = db.query("SELECT fnid, blob_id FROM m_bridge_checkpoint WHERE commit_id = %s", (commit_id,), dictionary=False)
    own = {int(f): int(b) for f, b in rows}
    if base is None:
        return list(own.items())
    base_rows = db.query("SELECT fnid, blob_id FROM m_bridge_checkpoint WHERE commit_id = %s", (base,), dictionary=False)
    tree = {int(f): int(b) for f, b in base_rows if int(f) not in own}
    tree.update((f, b) for f, b in own.items() if b != 0)  # blob 0: a path of the base the version lacks
    return list(tree.items())
