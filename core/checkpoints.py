"""core/checkpoints.py - Checkpoints stored as a delta of an earlier one.

`m_bridge_checkpoint` holds the tree of every ingested version: one `(commit_id, fnid, blob_id)` row per path.
Consecutive versions share nearly all of them (a stable release changes well under 1% of the files), so a
version's full rows are only kept while it is the newest one, which the next run reads as its previous version
(TableEngine preload, `main.processing_unchanges()`, `db.new_checkpoint_pairs()`...):

- `compact()`, at the end of the next version's run, compares the version with the newest full checkpoint
  ingested before it (`choose_base()`). When they differ in at most `ratio` of the version's rows
  (`parser.checkpoint_delta_ratio`, default 0.3), the version keeps only the difference: its rows whose blob
  differs from the base's, plus `blob_id = 0` for the base's paths it lacks, and `m_checkpoint_base` records the
  base. Otherwise the version stays full and becomes the base of the versions after it.
- `expand()` turns a delta back into the full tree. A run whose previous version is a delta (a backfill, a
  repair) expands it first; `expand_dependents()` does the same for the versions resting on a checkpoint that
  is about to be deleted.
- The `m_checkpoint_tree` view (defined in `db_engine/views.py`, created by `db.ensure_views()`) returns the full tree of
  every version, delta or not; filtered by `commit_id` the condition reaches both of its branches. Read through it
  wherever the version may be an older one.

A base is never compacted itself (`dependents()`), so every tree is at most one base plus one delta.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any

from db_engine import DBError
from db_engine.spec import Const, Join, Select, Value, in_select, is_null, lt, not_in

logger = logging.getLogger(__name__)


DEFAULT_RATIO = 0.3
INGEST_DONE = 2

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
    row = db.select_one("m_checkpoint_base", ["base_commit_id"], {"commit_id": commit_id}, as_dict=False)
    return int(row[0]) if row else None


def dependents(db: Any, commit_id: int) -> list[int]:
    """Versions stored as a delta of `commit_id`."""
    rows = db.select_rows("m_checkpoint_base", ["commit_id"], {"base_commit_id": commit_id}, as_dict=False)
    return [int(r[0]) for r in rows]


def should_compact(changed: int, removed: int, total: int, ratio: float) -> bool:
    """Keep a delta when it is at most `ratio` of the version's rows (and saves rows at all)."""
    delta = changed + removed
    return ratio > 0 and total > 0 and delta < total and delta <= ratio * total


def is_checkpoint_pinned(db: Any, commit_id: int) -> bool:
    """Whether commit_id is pinned as an ancestor base for an active or pending task or parse run."""
    if not commit_id:
        return False
    active = [0, 1, 2]
    probes = (
        lambda: db.row_exists("m_parse_task", {"base_commit_id": commit_id, "status": active}),
        lambda: db.row_exists(
            ("m_parse_task", "t"),
            {"r.commit_id": commit_id, "t.status": active},
            joins=[Join(("m_git_ref", "r"), {"r.ref_name": "t.base_ref_name"})],
        ),
        lambda: db.row_exists("m_ingest_state", {"base_commit_id": commit_id, "status": 1}),
    )
    for probe in probes:
        try:
            if probe():
                return True
        except DBError:
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
    skip = [c for c in exclude if c] or [0]
    base_join = [Join(("m_checkpoint_base", "b"), {"b.commit_id": "s.commit_id"}, "left")]
    if preferred_base and preferred_base not in skip:
        row = db.select_one(
            ("m_ingest_state", "s"),
            ["s.commit_id"],
            {"s.network_id": network_id, "s.commit_id": preferred_base, "s.status": INGEST_DONE, "b.commit_id": is_null()},
            joins=base_join,
            as_dict=False,
        )
        if row:
            return int(row[0])

    row = db.select_one(
        ("m_ingest_state", "s"),
        ["s.commit_id"],
        [
            {"s.network_id": network_id, "s.status": INGEST_DONE, "b.commit_id": is_null()},
            {"s.commit_id": lt(commit_id)},
            {"s.commit_id": not_in(skip)},
        ],
        joins=base_join,
        order_by=["-s.commit_id"],
        as_dict=False,
    )
    return int(row[0]) if row else None


def delta_size(db: Any, commit_id: int, base: int) -> tuple[int, int, int]:
    """(rows of `commit_id` the base lacks or holds another blob for, base paths `commit_id` lacks, rows of `commit_id`)."""
    changed = db.count_rows(
        ("m_bridge_checkpoint", "p"),
        {"p.commit_id": commit_id, "k.commit_id": is_null()},
        joins=[Join(("m_bridge_checkpoint", "k"),
                    {"k.commit_id": Value(base), "k.fnid": "p.fnid", "k.blob_id": "p.blob_id"}, "left")],
    )
    removed = db.count_rows(
        ("m_bridge_checkpoint", "k"),
        {"k.commit_id": base, "p.commit_id": is_null()},
        joins=[Join(("m_bridge_checkpoint", "p"), {"p.commit_id": Value(commit_id), "p.fnid": "k.fnid"}, "left")],
    )
    total = db.count_rows("m_bridge_checkpoint", {"commit_id": commit_id})
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
    with db.transaction():
        # Paths the base holds and this version lacks: a tombstone row (blob 0)
        db.copy_rows(
            "m_bridge_checkpoint",
            ["commit_id", "fnid", "blob_id"],
            Select(
                ("m_bridge_checkpoint", "k"),
                [Const(commit_id), "k.fnid", Const(0)],
                {"k.commit_id": base, "p.commit_id": is_null()},
                joins=[Join(("m_bridge_checkpoint", "p"), {"p.commit_id": Value(commit_id), "p.fnid": "k.fnid"}, "left")],
            ),
            on_conflict="ignore",
        )
        # The rows the base holds identically
        dropped = db.delete_rows(
            "m_bridge_checkpoint",
            [
                {"commit_id": commit_id},
                in_select(("fnid", "blob_id"),
                          Select(("m_bridge_checkpoint", "k"), ["k.fnid", "k.blob_id"], {"k.commit_id": base})),
            ],
        )
        db.delete_rows("m_checkpoint_base", {"commit_id": commit_id})
        db.insert_rows("m_checkpoint_base", ["commit_id", "base_commit_id", "delta_rows"],
                       [(commit_id, base, changed + removed)])
    return {"base": base, "changed": changed, "removed": removed, "total": total, "dropped": dropped}


def expand(db: Any, commit_id: int) -> int:
    """Turn the delta checkpoint of `commit_id` back into its full tree (one transaction). Returns the rows restored
    (0 when it was full)."""
    if not commit_id or db is None:
        return 0
    base = base_of(db, commit_id)
    if base is None:
        return 0
    with db.transaction():
        restored = db.copy_rows(
            "m_bridge_checkpoint",
            ["commit_id", "fnid", "blob_id"],
            Select(
                ("m_bridge_checkpoint", "k"),
                [Const(commit_id), "k.fnid", "k.blob_id"],
                {"k.commit_id": base, "d.commit_id": is_null()},
                joins=[Join(("m_bridge_checkpoint", "d"), {"d.commit_id": Value(commit_id), "d.fnid": "k.fnid"}, "left")],
            ),
            on_conflict="ignore",
        )
        db.delete_rows("m_bridge_checkpoint", {"commit_id": commit_id, "blob_id": 0})
        db.delete_rows("m_checkpoint_base", {"commit_id": commit_id})
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
    rows = db.select_rows("m_bridge_checkpoint", ["fnid", "blob_id"], {"commit_id": commit_id}, as_dict=False)
    own = {int(f): int(b) for f, b in rows}
    if base is None:
        return list(own.items())
    base_rows = db.select_rows("m_bridge_checkpoint", ["fnid", "blob_id"], {"commit_id": base}, as_dict=False)
    tree = {int(f): int(b) for f, b in base_rows if int(f) not in own}
    tree.update((f, b) for f, b in own.items() if b != 0)  # blob 0: a path of the base the version lacks
    return list(tree.items())
