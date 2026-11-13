"""core/ingest_state.py - Version ingestion state and recovery of interrupted versions.

A version (the head commit of a tag/branch) is ingested in one `main.update()` run. The run records:
- `m_ingest_state(network_id, commit_id, status, updated_at)`: INGEST_PARSING when it starts,
  INGEST_DONE once its last rows are committed. The webapp lists only finished versions.
- `m_ingest_watermark(commit_id, marked_table_id, max_id)`: the highest id of every id-allocating table
  when the run started. Ids are allocated increasingly by a single writer, so every row above a
  watermark was created by this run.
- `m_ingest_blob(commit_id, blob_id)`: blobs that already existed but are parsed by this run (blobs
  of intermediate commits that were never parsed); their derived rows may be partial too.

The TableEngine commits each table atomically but tables independently, so a run stopped in the
middle (crash, kill, duration limit) can leave half-written rows: a blob with tags but without its
symbol rows, a tag without its definitions, an AST node without its children. Content-addressed
deduplication would then reuse them forever. `rollback_plan()` lists the deletes that bring the
database back to the state before the run; `rollback_version()` executes them, after which the
version is parsed again from scratch.

Watermarks only point at one run's rows while that run is the newest: once another version has been
ingested to completion on top of an interrupted one, the ids above its marks belong to both.
`rollback_version()` detects that (`_completed_after()`) and refuses to delete, so stale marks left by
older interrupted runs cannot take a finished version down with them.
"""
from __future__ import annotations

import contextlib
import glob
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from db_engine import DBError
from db_engine.spec import Join, NotExists, Select, Value, Where, between, gt, in_select, is_null, le

logger = logging.getLogger(__name__)


INGEST_PARSING = 1
INGEST_DONE = 2

# Single-id tables whose rows are not created by a version run (seeds and repository registry)
_STATIC_TABLES = {"m_type_descriptor", "m_project", "m_git_network", "m_repository", "m_git_ref"}
# Tables referencing blobs from a commit's point of view (rows of other versions point at the blob too)
_COMMIT_SCOPED = {"m_bridge_checkpoint", "m_commit_file", "m_bridge_commit_tag", "m_blob_transition"}
# Rows describing the version's head commit itself (written by the run, whatever the commit's id)
_VERSION_ROWS = ("m_checkpoint_base", "m_bridge_checkpoint", "m_arch_root")
_DELETE_CHUNK = 50_000
_IN_CHUNK = 1000
# A rollback deletes millions of rows; without this heartbeat a long step looks like a hang
_PROGRESS_EVERY = 20.0


class RecoveryError(RuntimeError):
    """The interrupted run cannot be rolled back safely (rows older than the run reference its rows)."""


@dataclass(frozen=True)
class Delete:
    """One delete step of a rollback: the rows of `table` matching `where` (a `db_engine` Where spec).

    `condition` names the condition in words (`"commit_id ="`, `"segment_id >"`, `"blob_id IN"`) and `values` holds
    the bound values (a commit id, a watermark or a chunk of blob ids): both identify the step (equality, logs).
    """
    table: str
    condition: str
    values: tuple[Any, ...] = ()
    where: Where | None = field(default=None, compare=False, repr=False)

    def exists(self, db: Any) -> bool:
        """Whether the step would delete anything (most steps match nothing)."""
        return db.row_exists(self.table, self.where)

    def run(self, db: Any, limit: int | None = None) -> int:
        """Delete the matching rows (at most `limit`); returns the deleted count."""
        return int(db.delete_rows(self.table, self.where, limit=limit) or 0)


def _delete_eq(table: str, column: str, value: int) -> Delete:
    return Delete(table, f"{column} =", (value,), {column: value})


def _delete_above(table: str, column: str, mark: int) -> Delete:
    return Delete(table, f"{column} >", (mark,), {column: gt(mark)})


def _delete_in(table: str, column: str, ids: Sequence[int]) -> Delete:
    return Delete(table, f"{column} IN", tuple(ids), {column: list(ids)})


def _delete_in_parent_rows(child: str, child_col: str, parent: str, pk: str, parent_col: str, ids: Sequence[int]) -> Delete:
    """Rows of `child` whose `child_col` names a `parent` row whose `parent_col` is one of `ids`."""
    return Delete(
        child,
        f"{child_col} IN {parent}.{pk} WHERE {parent_col} IN",
        tuple(ids),
        [in_select(child_col, Select(parent, [pk], {parent_col: list(ids)}))],
    )


def _foreign(table: Any) -> list[tuple[str, str, str]]:
    return [(f[0], f[1], f[2]) for f in (table.init_foreign or ())]


def id_column(table: Any) -> str | None:
    """The allocated id column of a table (single INT primary key that is not a foreign key), else None."""
    primary = tuple(table.init_primary or ())
    if len(primary) != 1 or table.table_name in _STATIC_TABLES:
        return None
    col = primary[0]
    spec = dict(table.init_columns).get(col)
    if spec is None or not spec.is_integer or any(fk_col == col for fk_col, _, _ in _foreign(table)):
        return None
    return col


def marked_tables(tables: Sequence[Any]) -> list[tuple[Any, str]]:
    """(table, id column) of every table whose watermark a run records."""
    return [(t, col) for t in tables if (col := id_column(t))]


def _children(tables: Sequence[Any]) -> dict[str, list[tuple[Any, str]]]:
    """Parent table name -> [(child table, referencing column)]."""
    out: dict[str, list[tuple[Any, str]]] = {}
    for t in tables:
        for col, parent, _ in _foreign(t):
            out.setdefault(parent, []).append((t, col))
    return out


def _child_first_order(marked: list[tuple[Any, str]]) -> list[tuple[Any, str]]:
    """Marked tables ordered so that a table comes before every marked table it references."""
    names = {t.table_name for t, _ in marked}
    refs = {t.table_name: {p for _, p, _ in _foreign(t) if p in names and p != t.table_name} for t, _ in marked}
    order: list[tuple[Any, str]] = []
    placed: set[str] = set()
    pending = list(marked)
    while pending:
        # A table is ready once no pending table references it
        ready = [(t, c) for t, c in pending if not any(t.table_name in refs[o.table_name] for o, _ in pending if o is not t)]
        if not ready:  # reference cycle: keep declaration order for the rest
            ready = pending[:1]
        for item in ready:
            order.append(item)
            placed.add(item[0].table_name)
        pending = [p for p in pending if p[0].table_name not in placed]
    return order


def blob_derived_tables(tables: Sequence[Any]) -> list[tuple[Any, str]]:
    """(table, blob column) of the rows parsing a blob produces (tags, symbols, references, sections...)."""
    out = []
    for t in tables:
        if t.table_name in _COMMIT_SCOPED:
            continue
        for col, parent, _ in _foreign(t):
            if parent == "m_file_blob":
                out.append((t, col))
    return out


def precheck_queries(tables: Sequence[Any], marks: dict[str, int]) -> list[tuple[str, Select]]:
    """(description, Select) finding rows older than the run that reference rows the run created.

    Such rows would have to be deleted with the run's rows, which would destroy older data, so their
    presence aborts the rollback. Each Select yields at most one row (the older row's id).
    """
    queries = []
    for t, col in marked_tables(tables):
        if t.table_name not in marks:
            continue
        for fk_col, parent, _ in _foreign(t):
            if parent in marks and parent != t.table_name:
                queries.append((
                    f"{t.table_name}.{fk_col} -> {parent}",
                    Select(t.table_name, [col], {col: le(marks[t.table_name]), fk_col: gt(marks[parent])}, limit=1),
                ))
    return queries


def rollback_plan(tables: Sequence[Any], marks: dict[str, int], commit_id: int, blob_ids: Iterable[int] = ()) -> list[Delete]:
    """Deletes undoing an interrupted run, children before parents.

    Args:
        tables: the schema (core.DBLayout.TABLES).
        marks: table name -> highest id before the run (m_ingest_watermark).
        commit_id: head commit of the interrupted version (its checkpoint and arch layout rows are removed).
        blob_ids: blobs that existed before the run but were parsed by it (m_ingest_blob).
    """
    by_name = {t.table_name: t for t in tables}
    children = _children(tables)
    plan: list[Delete] = [_delete_eq(name, "commit_id", commit_id) for name in _VERSION_ROWS]

    # 1. Rows derived from pre-existing blobs the run parsed (and the rows hanging off them)
    blobs = sorted(set(blob_ids))
    for i in range(0, len(blobs), _IN_CHUNK):
        chunk = tuple(blobs[i:i + _IN_CHUNK])
        for t, col in blob_derived_tables(tables):
            pk = id_column(t)
            if pk:
                for child, child_col in children.get(t.table_name, ()):
                    plan.append(_delete_in_parent_rows(child.table_name, child_col, t.table_name, pk, col, chunk))
            plan.append(_delete_in(t.table_name, col, chunk))

    # 2. Rows above the watermarks: link tables referencing them first, then the tables themselves
    marked = [(t, c) for t, c in marked_tables(tables) if t.table_name in marks]
    for t, col in _child_first_order(marked):
        mark = marks[t.table_name]
        for child, child_col in children.get(t.table_name, ()):
            if child.table_name in marks and id_column(child):
                continue  # a marked child's new rows go with its own watermark (precheck guards older rows)
            plan.append(_delete_above(child.table_name, child_col, mark))
        plan.append(_delete_above(t.table_name, col, mark))
    # Keep the plan free of exact duplicates (several parents can share a link table)
    seen: set[tuple[str, str, tuple[Any, ...]]] = set()
    unique = []
    for d in plan:
        key = (d.table, d.condition, d.values)
        if key not in seen and d.table in by_name:
            seen.add(key)
            unique.append(d)
    return unique


# ---------------------------------------------------------------------------------------------- database
def _fetch_marks(db: Any, commit_id: int, tables: Sequence[Any]) -> dict[str, int]:
    names = {t.table_id: t.table_name for t in tables}
    rows = db.select_rows("m_ingest_watermark", ["marked_table_id", "max_id"], {"commit_id": commit_id}, as_dict=False)
    return {names[int(tid)]: int(mid) for tid, mid in rows if int(tid) in names}


def _completed_after(db: Any, marks: dict[str, int]) -> int | None:
    """Head commit of a version ingested to completion after `marks` were taken, else None.

    Watermarks only identify a run's rows while that run is the newest one: `begin()` records the
    highest commit id together with the other marks, so a finished version above it was ingested
    afterwards and its rows sit above the marks too. Deleting everything above them would then
    destroy that finished version instead of the interrupted run.
    """
    commit_mark = marks.get("m_commit")
    if commit_mark is None:
        return None
    newest = db.min_value("m_ingest_state", "commit_id", {"status": INGEST_DONE, "commit_id": gt(commit_mark)}, default=None)
    return int(newest) if newest is not None else None


def _delete_marks(db: Any, commit_id: int) -> None:
    """Delete the watermark, blob and id-range rows of a run (the id-range table may not exist)."""
    db.delete_rows("m_ingest_watermark", {"commit_id": commit_id})
    db.delete_rows("m_ingest_blob", {"commit_id": commit_id})
    try:
        db.delete_rows("m_ingest_id_range", {"commit_id": commit_id})
    except DBError:
        pass


def discard_marks(db: Any, commit_id: int) -> None:
    """Forget a run's watermarks, id ranges, and parsed blobs (spent after a rollback, unusable when stale)."""
    with db.transaction():
        _delete_marks(db, commit_id)


def get_status(db: Any, network_id: int, commit_id: int) -> int | None:
    row = db.select_one("m_ingest_state", ["status"], {"network_id": network_id, "commit_id": commit_id}, as_dict=False)
    return int(row[0]) if row else None


def set_status(
    db: Any,
    network_id: int,
    commit_id: int,
    status: int,
    base_commit_id: int = 0,
    worker_id: str = "",
) -> None:
    """Record a version's ingestion status (upsert); a finished version also drops its bookkeeping (one transaction)."""
    if not commit_id:
        # commit_id 0 is the "unresolved ref" sentinel of m_git_ref, not a version being ingested
        logger.warning("Ignoring an ingestion status for commit_id 0 (the ref has no resolved head commit).")
        return
    with db.transaction():
        db.insert_rows(
            "m_ingest_state",
            ["network_id", "commit_id", "status", "updated_at", "base_commit_id", "worker_id"],
            [(network_id, commit_id, status, int(time.time()), base_commit_id, worker_id)],
            on_conflict="update",
        )
        if status == INGEST_DONE:
            _delete_marks(db, commit_id)


def begin(
    db: Any,
    network_id: int,
    commit_id: int,
    tables: Sequence[Any],
    base_commit_id: int = 0,
    worker_id: str = "",
) -> dict[str, int]:
    """Mark a version as being parsed and record the watermarks of its run. Returns the marks."""
    marks: dict[str, int] = {}
    if not commit_id:
        logger.warning("Not watermarking a run for commit_id 0 (the ref has no resolved head commit).")
        return marks
    with db.transaction():
        _delete_marks(db, commit_id)
        rows = []
        for t, col in marked_tables(tables):
            marks[t.table_name] = int(db.max_value(t.table_name, col, default=0) or 0)
            rows.append((commit_id, t.table_id, marks[t.table_name]))
        if rows:
            db.insert_rows("m_ingest_watermark", ["commit_id", "marked_table_id", "max_id"], rows)
    set_status(db, network_id, commit_id, INGEST_PARSING, base_commit_id=base_commit_id, worker_id=worker_id)
    return marks


def fetch_id_ranges(db: Any, commit_id: int) -> list[tuple[int, int, int]]:
    """Fetch leased (marked_table_id, start_id, end_id) ranges allocated to commit_id."""
    try:
        rows = db.select_rows("m_ingest_id_range", ["marked_table_id", "start_id", "end_id"], {"commit_id": commit_id}, as_dict=False)
        return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]
    except DBError:
        return []


def record_blobs(db: Any, commit_id: int, blob_ids: Iterable[int]) -> None:
    """Record pre-existing blobs the run is about to parse (before any of their rows are written)."""
    ids = sorted(set(blob_ids))
    if not ids or not commit_id:
        return
    with db.transaction():
        for i in range(0, len(ids), _IN_CHUNK):
            db.insert_rows("m_ingest_blob", ["commit_id", "blob_id"], [(commit_id, b) for b in ids[i:i + _IN_CHUNK]],
                           on_conflict="ignore")


def rollback_id_ranges(
    db: Any,
    commit_id: int,
    tables: Sequence[Any],
    id_ranges: list[tuple[int, int, int]],
) -> int:
    """Range-isolated rollback for an interrupted run that leased discrete ID blocks.

    Preserves universal Git registry entries (m_file_name, m_file_blob, m_maintainer_person, m_commit)
    and removes only the version-scoped and ID-range-allocated rows of this commit_id. Every delete is
    idempotent and atomic on its own: an interrupted rollback is simply run again.
    """
    total = 0
    by_id = {t.table_id: t for t in tables}
    children = _children(tables)
    started = time.perf_counter()

    # 1. Version-scoped rows
    for vtable in _VERSION_ROWS + (
        "m_commit_file",
        "m_bridge_commit_tag",
        "m_bridge_commit_person",
        "m_commit_email",
    ):
        try:
            total += int(db.delete_rows(vtable, {"commit_id": commit_id}) or 0)
        except DBError:
            pass

    # 2. Ingest blob derived rows
    blob_rows = db.select_rows("m_ingest_blob", ["blob_id"], {"commit_id": commit_id}, as_dict=False)
    blob_ids = [int(r[0]) for r in blob_rows]
    for i in range(0, len(blob_ids), _IN_CHUNK):
        chunk = blob_ids[i : i + _IN_CHUNK]
        for t, col in blob_derived_tables(tables):
            pk = id_column(t)
            if pk:
                for child, child_col in children.get(t.table_name, ()):
                    total += _delete_in_parent_rows(child.table_name, child_col, t.table_name, pk, col, chunk).run(db)
            total += _delete_in(t.table_name, col, chunk).run(db)

    # 3. Leased ID range deletes
    for tid, start_id, end_id in id_ranges:
        tbl = by_id.get(tid)
        if tbl is None or tbl.table_name in _STATIC_TABLES or tbl.table_name in (
            "m_file_name",
            "m_file_blob",
            "m_maintainer_person",
            "m_commit",
        ):
            continue
        pk = id_column(tbl)
        if not pk:
            continue

        # Special AST handling for children
        if tbl.table_name == "m_ast":
            for ast_child in ("m_ast_container", "m_ast_include", "m_ast_attribute"):
                total += int(db.delete_rows(ast_child, {"ast_id": between(start_id, end_id)}) or 0)
            try:
                total += int(db.delete_rows("m_ast_hash", {"ast_id": between(start_id, end_id)}) or 0)
            except DBError:
                pass

        for child, child_col in children.get(tbl.table_name, ()):
            if child.table_name in by_id and id_column(child):
                continue  # Child will have its own range if marked
            try:
                total += int(db.delete_rows(child.table_name, {child_col: between(start_id, end_id)}) or 0)
            except DBError:
                pass

        total += int(db.delete_rows(tbl.table_name, {pk: between(start_id, end_id)}) or 0)

    # 4. Clean up state and marks
    db.delete_rows("m_ingest_state", {"commit_id": commit_id})
    discard_marks(db, commit_id)
    logger.info(
        f"Rolled back interrupted run of commit_id {commit_id} using leased ID ranges: "
        f"{total} rows removed in {time.perf_counter() - started:.1f}s."
    )
    return total


def rollback_version(db: Any, network_id: int, commit_id: int, tables: Sequence[Any]) -> bool:
    """Undo the rows of an interrupted run of `commit_id`. Returns False when the run left no usable
    watermarks (it predates them, or a later version finished on top of them and the caller has to fall
    back to a version-scoped purge); raises RecoveryError when older rows reference rows of the run."""
    id_ranges = fetch_id_ranges(db, commit_id)
    if id_ranges:
        rollback_id_ranges(db, commit_id, tables, id_ranges)
        return True
    marks = _fetch_marks(db, commit_id, tables)
    if not marks:
        return False
    newer = _completed_after(db, marks)
    if newer is not None:
        logger.warning(
            f"Watermarks of the interrupted run of commit_id {commit_id} are stale: version commit_id "
            f"{newer} was ingested to completion after them, so the ids above the marks no longer "
            "belong to this run alone. Discarding them instead of deleting a finished version; only "
            "the version-scoped rows of the interrupted run are purged."
        )
        discard_marks(db, commit_id)
        return False
    for desc, select in precheck_queries(tables, marks):
        found = db.select_spec(select, as_dict=False)
        if found:
            raise RecoveryError(
                f"Cannot roll back the interrupted run of commit_id {commit_id}: row {found[0][0]} of {desc} "
                "predates the run but references a row it created."
            )
    blob_rows = db.select_rows("m_ingest_blob", ["blob_id"], {"commit_id": commit_id}, as_dict=False)
    blob_ids = [int(r[0]) for r in blob_rows]
    plan = rollback_plan(tables, marks, commit_id, blob_ids)
    started = time.perf_counter()
    total = 0
    logger.info(f"Rolling back the interrupted run of commit_id {commit_id}: {len(plan)} delete step(s) to run.")
    for step, d in enumerate(plan, 1):
        # Most steps match nothing; probing first keeps them off the write path and out of the log
        if not d.exists(db):
            continue
        step_started = last_logged = time.perf_counter()
        removed = 0
        while True:
            deleted = d.run(db, limit=_DELETE_CHUNK)  # each chunk is its own atomic statement
            removed += deleted
            if deleted < _DELETE_CHUNK:
                break
            if time.perf_counter() - last_logged >= _PROGRESS_EVERY:
                last_logged = time.perf_counter()
                logger.info(f"  [{step}/{len(plan)}] {d.table} ({d.condition}): {removed} rows removed so far...")
        total += removed
        logger.info(
            f"  [{step}/{len(plan)}] {d.table} ({d.condition}): {removed} rows "
            f"in {time.perf_counter() - step_started:.1f}s (total {total})"
        )
    discard_marks(db, commit_id)
    logger.info(
        f"Rolled back the interrupted run of commit_id {commit_id}: {total} rows removed "
        f"({len(blob_ids)} pre-existing blobs re-queued) in {time.perf_counter() - started:.1f}s."
    )
    return True


# Commit-scoped rows of a version run; `purge_partial_version()` deletes them when the run left no watermarks
PARTIAL_VERSION_TABLES = (
    "m_checkpoint_base",
    "m_bridge_checkpoint",
    "m_arch_root",
    "m_commit_file",
    "m_bridge_commit_tag",
    "m_bridge_commit_person",
    "m_commit_email",
)


def purge_partial_version(db: Any, commit_id: int) -> int:
    """Remove the version-scoped rows (`PARTIAL_VERSION_TABLES`) of an interrupted run that left no watermarks.

    Versions stored as checkpoint deltas of `commit_id` are expanded first. Returns the deleted row count.
    """
    if db is None or not commit_id:
        return 0
    from core import checkpoints

    checkpoints.expand_dependents(db, commit_id)
    total = 0
    for table in PARTIAL_VERSION_TABLES:
        total += int(db.delete_rows(table, {"commit_id": commit_id}) or 0)
    return total


def clean_interrupted_version(db: Any, network_id: int, commit_id: int, tables: Sequence[Any]) -> str:
    """Remove every row an interrupted run of `commit_id` wrote and its `INGEST_PARSING` state row.

    Rolls back by ID range or watermark (`rollback_version()`) and falls back to `purge_partial_version()`
    when the run left no usable watermarks. The version then reads as "new" and the next run parses it
    from scratch. Returns "rollback" or "purge" (the method used); raises RecoveryError like
    `rollback_version()`.
    """
    method = "rollback" if rollback_version(db, network_id, commit_id, tables) else "purge"
    if method == "purge":
        purge_partial_version(db, commit_id)
    db.delete_rows("m_ingest_state", {"network_id": network_id, "commit_id": commit_id, "status": INGEST_PARSING})
    return method


def cleanup_ramdisk_artifacts(ramdisk_dir: str | None = None, search_system_temps: bool = True) -> int:
    """Purge orphaned scratch directories left in RAMDISK or temp directories from previous crashed parser runs.

    Searches for directories matching 'code-parser.*' and 'kip-*' (which includes 'kip-commits-*'). A directory
    whose owner (`core.FileHandler.mark_owner()`) is a process still running belongs to a live parser
    instance and is kept.
    Returns number of directories removed.
    """
    from core.FileHandler import owner_alive

    removed = 0
    search_dirs: list[str] = []
    if ramdisk_dir and os.path.isdir(ramdisk_dir):
        search_dirs.append(ramdisk_dir)
    if search_system_temps:
        default_ramdisk = "/dev/shm"  # noqa: S108
        if os.path.isdir(default_ramdisk) and default_ramdisk not in search_dirs:
            search_dirs.append(default_ramdisk)
        tmp = tempfile.gettempdir()
        if tmp and os.path.isdir(tmp) and tmp not in search_dirs:
            search_dirs.append(tmp)

    for base in search_dirs:
        for pattern in ("code-parser.*", "kip-*"):
            for path in glob.glob(os.path.join(base, pattern)):
                if not os.path.isdir(path) or os.path.islink(path):
                    continue
                if owner_alive(path):
                    logger.debug(f"Keeping scratch directory of a running parser: {path}")
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
                logger.debug(f"Removed stale scratch directory: {path}")

    logger.info(f"[Cleanup] Removed {removed} orphaned scratch directory/directories from RAMDISK/temp storage.")
    return removed


def fetch_repo_version_states(db: Any, repo_id: int, network_id: int) -> dict[str, tuple[int, str]]:
    """Return map of ref_name -> (commit_id, state) for all registered refs of the repository.

    States:
        "done": fully ingested version (INGEST_DONE)
        "interrupted": crashed or halted run with INGEST_PARSING
        "legacy": no state row but checkpoint rows (ingested before m_ingest_state existed); whether it is
            complete is decided by `main._previous_ingest()` (at least 90% of the tree checkpointed)
        "new": registered ref that has not been ingested yet
    """
    states: dict[str, tuple[int, str]] = {}
    if db is None:
        return states
    legacy_candidates: list[tuple[str, int]] = []
    rows = db.select_rows(
        ("m_git_ref", "r"),
        ["r.ref_name", "r.commit_id", "s.status"],
        {"r.repo_id": repo_id, "r.commit_id": gt(0)},
        joins=[Join(("m_ingest_state", "s"), {"s.commit_id": "r.commit_id", "s.network_id": Value(network_id)}, "left")],
        as_dict=False,
    )
    for row in rows:
        ref_name = row[0]
        if isinstance(ref_name, (bytes, bytearray)):
            ref_name = ref_name.decode("utf-8", errors="replace")
        commit_id = int(row[1])
        status = row[2]
        if status == INGEST_DONE:
            states[ref_name] = (commit_id, "done")
        elif status == INGEST_PARSING:
            states[ref_name] = (commit_id, "interrupted")
        else:
            legacy_candidates.append((ref_name, commit_id))

    # One query per 1000 refs (the primary key's commit_id prefix answers it) instead of a COUNT(*) per ref
    checkpointed: set[int] = set()
    cids = sorted({cid for _name, cid in legacy_candidates})
    for i in range(0, len(cids), _IN_CHUNK):
        res = db.select_rows("m_bridge_checkpoint", ["commit_id"], {"commit_id": cids[i:i + _IN_CHUNK]},
                             distinct=True, as_dict=False)
        checkpointed.update(int(r[0]) for r in res)
    for rname, cid in legacy_candidates:
        states[rname] = (cid, "legacy" if cid in checkpointed else "new")
    return states


def purge_version_differential_rows(db: Any, commit_id: int, pred_commit_id: int | None = None) -> int:
    """Purge version-scoped differential rows of a version to prepare clean re-alignment with a new Old_VID base.

    Cleans:
      - Inbound m_moved_tag transitions whose target tag only appears in blobs new in commit_id's tree
        relative to pred_commit_id (tags are content-addressed and shared across versions: a tag also placed
        in the predecessor's tree keeps the transitions earlier versions recorded for it). Without
        pred_commit_id no transition is removed.
      - m_bridge_checkpoint for commit_id (checkpoints stored as deltas of it are expanded first, and so are the
        version's and the predecessor's own: core/checkpoints.py)
      - m_commit_file, m_bridge_commit_tag, m_bridge_commit_person, m_commit_email for commit_id
      - m_arch_root for commit_id
      - Resets m_ingest_state to INGEST_PARSING

    Leaves universal content-addressable tables (m_file_blob, m_ast, m_tag) intact.
    Returns total count of deleted rows.
    """
    total = 0
    if db is None:
        return 0
    from core import checkpoints
    checkpoints.expand_dependents(db, commit_id)
    checkpoints.expand(db, commit_id)
    if pred_commit_id:
        checkpoints.expand(db, pred_commit_id)

    # 1. Transitions into tags this version introduced (placed in its new blobs, absent from the predecessor)
    if pred_commit_id:
        try:
            tag_rows = db.select_rows(
                ("m_bridge_checkpoint", "bcp"),
                ["bt.tag_id"],
                [
                    {"bcp.commit_id": commit_id, "pred.blob_id": is_null()},
                    NotExists(
                        ("m_bridge_tag", "pbt"),
                        {"pbt.tag_id": "bt.tag_id"},
                        {"pcp.commit_id": pred_commit_id},
                        joins=[Join(("m_bridge_checkpoint", "pcp"), {"pcp.blob_id": "pbt.blob_id"})],
                    ),
                ],
                joins=[
                    Join(("m_bridge_checkpoint", "pred"),
                         {"pred.commit_id": Value(pred_commit_id), "pred.blob_id": "bcp.blob_id"}, "left"),
                    Join(("m_bridge_tag", "bt"), {"bt.blob_id": "bcp.blob_id"}),
                ],
                distinct=True,
                as_dict=False,
            )
            target_tag_ids = [int(r[0]) for r in tag_rows]
            for i in range(0, len(target_tag_ids), _IN_CHUNK):
                total += int(db.delete_rows("m_moved_tag", {"e_tag_id": target_tag_ids[i:i + _IN_CHUNK]}) or 0)
        except DBError as e:
            logger.debug(f"Inbound m_moved_tag purge error for commit_id {commit_id}: {e}")

    # 2. Delete version-scoped records
    for table in (
        "m_bridge_checkpoint",
        "m_commit_file",
        "m_bridge_commit_tag",
        "m_bridge_commit_person",
        "m_commit_email",
        "m_arch_root",
    ):
        try:
            total += int(db.delete_rows(table, {"commit_id": commit_id}) or 0)
        except DBError as e:
            logger.debug(f"Version row delete error for {table} (commit_id {commit_id}): {e}")

    # 3. Reset ingestion state to INGEST_PARSING
    try:
        db.update_rows("m_ingest_state", {"status": INGEST_PARSING, "updated_at": int(time.time())}, {"commit_id": commit_id})
    except DBError as e:
        logger.debug(f"m_ingest_state status reset error for commit_id {commit_id}: {e}")

    logger.info(
        f"Purged {total} version-scoped differential rows for commit_id {commit_id} to prepare clean re-alignment."
    )
    return total
