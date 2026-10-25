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
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

INGEST_PARSING = 1
INGEST_DONE = 2

# Single-id tables whose rows are not created by a version run (seeds and repository registry)
_STATIC_TABLES = {"m_type_descriptor", "m_project", "m_git_network", "m_repository", "m_git_ref"}
# Tables referencing blobs from a commit's point of view (rows of other versions point at the blob too)
_COMMIT_SCOPED = {"m_bridge_checkpoint", "m_commit_file", "m_bridge_commit_tag", "m_blob_transition"}
# Rows describing the version's head commit itself (written by the run, whatever the commit's id)
_VERSION_ROWS = ("m_bridge_checkpoint", "m_arch_root")
_DELETE_CHUNK = 50_000
_IN_CHUNK = 1000


class RecoveryError(RuntimeError):
    """The interrupted run cannot be rolled back safely (rows older than the run reference its rows)."""


@dataclass(frozen=True)
class Delete:
    """One `DELETE FROM table WHERE where` (params bound to the %s placeholders)."""
    table: str
    where: str
    params: tuple[Any, ...] = ()

    def sql(self) -> str:
        return f"DELETE FROM {self.table} WHERE {self.where}"


def _columns(table: Any) -> list[tuple[str, str]]:
    return [(c[0], c[1].upper()) for c in table.init_columns]


def _foreign(table: Any) -> list[tuple[str, str, str]]:
    return [(f[0], f[1], f[2]) for f in (table.init_foreign or ())]


def id_column(table: Any) -> str | None:
    """The allocated id column of a table (single INT primary key that is not a foreign key), else None."""
    primary = tuple(table.init_primary or ())
    if len(primary) != 1 or table.table_name in _STATIC_TABLES:
        return None
    col = primary[0]
    col_type = dict(_columns(table)).get(col, "")
    if "INT" not in col_type or any(fk_col == col for fk_col, _, _ in _foreign(table)):
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


def precheck_queries(tables: Sequence[Any], marks: dict[str, int]) -> list[tuple[str, str, tuple[Any, ...]]]:
    """(description, SELECT, params) finding rows older than the run that reference rows the run created.

    Such rows would have to be deleted with the run's rows, which would destroy older data, so their
    presence aborts the rollback.
    """
    queries = []
    for t, col in marked_tables(tables):
        if t.table_name not in marks:
            continue
        for fk_col, parent, _ in _foreign(t):
            if parent in marks and parent != t.table_name:
                queries.append((
                    f"{t.table_name}.{fk_col} -> {parent}",
                    f"SELECT {col} FROM {t.table_name} WHERE {col} <= %s AND {fk_col} > %s LIMIT 1",
                    (marks[t.table_name], marks[parent]),
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
    plan: list[Delete] = [Delete(name, "commit_id = %s", (commit_id,)) for name in _VERSION_ROWS]

    # 1. Rows derived from pre-existing blobs the run parsed (and the rows hanging off them)
    blobs = sorted(set(blob_ids))
    for i in range(0, len(blobs), _IN_CHUNK):
        chunk = tuple(blobs[i:i + _IN_CHUNK])
        in_list = ",".join(["%s"] * len(chunk))
        for t, col in blob_derived_tables(tables):
            pk = id_column(t)
            if pk:
                for child, child_col in children.get(t.table_name, ()):
                    plan.append(Delete(
                        child.table_name,
                        f"{child_col} IN (SELECT {pk} FROM {t.table_name} WHERE {col} IN ({in_list}))",
                        chunk,
                    ))
            plan.append(Delete(t.table_name, f"{col} IN ({in_list})", chunk))

    # 2. Rows above the watermarks: link tables referencing them first, then the tables themselves
    marked = [(t, c) for t, c in marked_tables(tables) if t.table_name in marks]
    for t, col in _child_first_order(marked):
        mark = marks[t.table_name]
        for child, child_col in children.get(t.table_name, ()):
            if child.table_name in marks and id_column(child):
                continue  # a marked child's new rows go with its own watermark (precheck guards older rows)
            plan.append(Delete(child.table_name, f"{child_col} > %s", (mark,)))
        plan.append(Delete(t.table_name, f"{col} > %s", (mark,)))
    # Keep the plan free of exact duplicates (several parents can share a link table)
    seen: set[tuple[str, str, tuple[Any, ...]]] = set()
    unique = []
    for d in plan:
        key = (d.table, d.where, d.params)
        if key not in seen and d.table in by_name:
            seen.add(key)
            unique.append(d)
    return unique


# ---------------------------------------------------------------------------------------------- database
def _fetch_marks(cur: Any, commit_id: int, tables: Sequence[Any]) -> dict[str, int]:
    names = {t.table_id: t.table_name for t in tables}
    cur.execute("SELECT marked_table_id, max_id FROM m_ingest_watermark WHERE commit_id = %s", (commit_id,))
    return {names[int(tid)]: int(mid) for tid, mid in cur.fetchall() if int(tid) in names}


def get_status(cur: Any, network_id: int, commit_id: int) -> int | None:
    cur.execute("SELECT status FROM m_ingest_state WHERE network_id = %s AND commit_id = %s", (network_id, commit_id))
    row = cur.fetchone()
    return int(row[0]) if row else None


def set_status(cnx: Any, network_id: int, commit_id: int, status: int) -> None:
    """Record a version's ingestion status (upsert) and commit."""
    with contextlib.closing(cnx.cursor()) as cur:
        cur.execute(
            "INSERT INTO m_ingest_state (network_id, commit_id, status, updated_at) VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE status = VALUES(status), updated_at = VALUES(updated_at)",
            (network_id, commit_id, status, int(time.time())),
        )
        if status == INGEST_DONE:
            cur.execute("DELETE FROM m_ingest_watermark WHERE commit_id = %s", (commit_id,))
            cur.execute("DELETE FROM m_ingest_blob WHERE commit_id = %s", (commit_id,))
    cnx.commit()


def begin(cnx: Any, network_id: int, commit_id: int, tables: Sequence[Any]) -> dict[str, int]:
    """Mark a version as being parsed and record the watermarks of its run. Returns the marks."""
    marks: dict[str, int] = {}
    with contextlib.closing(cnx.cursor()) as cur:
        cur.execute("DELETE FROM m_ingest_watermark WHERE commit_id = %s", (commit_id,))
        cur.execute("DELETE FROM m_ingest_blob WHERE commit_id = %s", (commit_id,))
        for t, col in marked_tables(tables):
            cur.execute(f"SELECT COALESCE(MAX({col}), 0) FROM {t.table_name}")
            marks[t.table_name] = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO m_ingest_watermark (commit_id, marked_table_id, max_id) VALUES (%s, %s, %s)",
                (commit_id, t.table_id, marks[t.table_name]),
            )
    set_status(cnx, network_id, commit_id, INGEST_PARSING)
    return marks


def record_blobs(cnx: Any, commit_id: int, blob_ids: Iterable[int]) -> None:
    """Record pre-existing blobs the run is about to parse (before any of their rows are written)."""
    ids = sorted(set(blob_ids))
    if not ids:
        return
    with contextlib.closing(cnx.cursor()) as cur:
        for i in range(0, len(ids), _IN_CHUNK):
            chunk = ids[i:i + _IN_CHUNK]
            cur.executemany(
                "INSERT INTO m_ingest_blob (commit_id, blob_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE blob_id = blob_id",
                [(commit_id, b) for b in chunk],
            )
    cnx.commit()


def rollback_version(cnx: Any, network_id: int, commit_id: int, tables: Sequence[Any]) -> bool:
    """Undo the rows of an interrupted run of `commit_id`. Returns False when the run left no watermarks
    (it predates them); raises RecoveryError when older rows reference rows of the run."""
    with contextlib.closing(cnx.cursor()) as cur:
        marks = _fetch_marks(cur, commit_id, tables)
        if not marks:
            return False
        for desc, sql, params in precheck_queries(tables, marks):
            cur.execute(sql, params)
            row = cur.fetchone()
            if row:
                raise RecoveryError(
                    f"Cannot roll back the interrupted run of commit_id {commit_id}: row {row[0]} of {desc} "
                    "predates the run but references a row it created."
                )
        cur.execute("SELECT blob_id FROM m_ingest_blob WHERE commit_id = %s", (commit_id,))
        blob_ids = [int(r[0]) for r in cur.fetchall()]
    plan = rollback_plan(tables, marks, commit_id, blob_ids)
    started = time.perf_counter()
    total = 0
    with contextlib.closing(cnx.cursor()) as cur:
        for d in plan:
            while True:
                cur.execute(f"{d.sql()} LIMIT {_DELETE_CHUNK}", d.params)
                deleted = cur.rowcount or 0
                cnx.commit()
                total += deleted
                if deleted < _DELETE_CHUNK:
                    break
            if total and d.table in marks:
                logger.debug("Rollback of commit_id %s: %s cleared above %s", commit_id, d.table, marks[d.table])
    logger.info(
        f"Rolled back the interrupted run of commit_id {commit_id}: {total} rows removed "
        f"({len(blob_ids)} pre-existing blobs re-queued) in {time.perf_counter() - started:.1f}s."
    )
    return True
