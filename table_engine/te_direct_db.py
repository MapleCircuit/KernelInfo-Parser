"""table_engine/te_direct_db.py - Direct Database Table Engine (TEDirectDB).

===============================================================================
TABLE ENGINE (TEDirectDB) ARCHITECTURAL GUIDE & CONTRACT SPECIFICATION
===============================================================================
This module implements the Direct Database Table Engine (TEDirectDB) for
KernelInfo-Parser. It serves as the stateful caching, sequence coordination,
and batching layer between high-level ChangeSet operations and the lower-level
database driver (e.g., MariaDB or MockDB in db_engine).

KEY RESPONSIBILITIES:
1. SEQUENCE ASSIGNMENT (next_id):
   Maintains local auto-increment primary key counters initialized via
   `db.get_next_id(table)` to eliminate per-row SELECT MAX() database queries.

2. LOCAL STAGING & DEDUPLICATION:
   - `queued_set`: Staging dictionary for single-table inserts. For deduplicated
     tables (no_duplicate=True), maps unique data tuples to assigned sequence IDs.
   - `queued_update`: Staging list of row update tuples.
   - `queued_view`: In-memory cache for relational joins (JoinsType) mapping
     non-None column values to assigned primary view IDs.

3. RELATIONAL VIEW EXPANSION:
   `view_set()` decomposes multi-table joined structures into their constituent
   table rows, correctly slicing data offsets and keying on each table's primary key.

4. TRANSACTION COMMIT:
   `commit()` / `commit_all()` move the staged rows into a `CommitBatch` and write it
   with `db.commit_tables_parallel()`. With `async_commits` on, `commit_all()` hands the
   batch to a background writer thread and returns; every database read whose tables the
   batch writes, the next commit, `start_new_db()` and `close()` first wait for it. Tables
   the database did not commit go back to staging, so a retried commit writes them.

5. RESOURCE MANAGEMENT:
   Safely manages database connection lifecycles during start, restart, and shutdown.
===============================================================================
"""
from __future__ import annotations

import contextlib
import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence, Self
from types import TracebackType
from operator import itemgetter
from core.globalstuff import (
    SafeDataType,
    JoinsType,
    PointerGetter,
)

if TYPE_CHECKING:
    from core.TableHandling import Table
    from db_engine.base import BaseDBEngine


def compute_ast_hash(joins: JoinsType, filtered_columns: tuple[SafeDataType, ...]) -> bytes:
    """Compute deterministic 32-byte binary SHA-256 hash for canonical AST join graph and filtered column data."""
    key_str = f"{joins}:{filtered_columns}"
    return hashlib.sha256(key_str.encode("utf-8")).digest()


def _is_hash_keyed(table: Table) -> bool:
    """Tables deduplicated by primary key across commits (`_committed_pks`): idempotent or content-hash keyed."""
    return bool(
        getattr(table, "idempotent_pk", False)
        or getattr(table, "init_primary", ()) == ("hash",)
        or table.table_name in ("m_tag_code", "m_ast_hash")
    )


@dataclass(eq=False)
class CommitBatch:
    """Rows one commit took out of the staging queues, kept until the database has written them.

    `staged` keeps the swapped-out staging containers (`queued_set` dict, `queued_update` list per table) and the
    `added_*` fields the keys this commit added to the committed-key sets, so the tables the database did not
    commit can be put back (`TEDirectDB._restore_batch()`) for the next commit to write.
    """

    tables_data: list[tuple[Any, tuple[tuple[SafeDataType, ...], ...], tuple[tuple[SafeDataType, ...], ...]]] = field(
        default_factory=list
    )
    staged: dict[int, tuple[dict[Any, Any], list[tuple[SafeDataType, ...]]]] = field(default_factory=dict)
    added_nodup: dict[int, list[Any]] = field(default_factory=dict)
    added_pks: dict[int, list[Any]] = field(default_factory=dict)
    added_unique: dict[int, dict[tuple[int, ...], list[tuple[SafeDataType, ...]]]] = field(default_factory=dict)
    table_ids: frozenset[int] = frozenset()
    rows: int = 0
    # (table_name, rows inserted, rows updated, seconds) per written table
    stats: list[tuple[str, int, int, float]] = field(default_factory=list)
    write_s: float = 0.0
    wait_s: float = 0.0
    background: bool = False
    error: BaseException | None = None
    thread: threading.Thread | None = None
    restored: frozenset[int] = frozenset()

    def slowest_tables(self, limit: int = 4) -> str:
        """`name rows/seconds` of the tables that took longest (tables are written in parallel, so they overlap)."""
        top = sorted(self.stats, key=lambda st: st[3], reverse=True)[:limit]
        return ", ".join(f"{st[0]} {st[1] + st[2]:,} rows {st[3]:.1f}s" for st in top)


class TEDirectDB:
    """Direct Database Table Engine (Unoptimized direct-to-database table engine).

    Acts as a stateful caching, sequence coordination, and batching layer between
    ChangeSet execution in TableHandling and the database backend in db_engine.
    """

    def __init__(self) -> None:
        """Initialize Table Engine internal state structures.

        Note: You must still call `start()` or `start_new_db()` before performing operations.

        Attributes initialized:
            tables: Maps table_id (int) to Table schema instance.
            queued_set: Maps table_id (int) to dictionary of staged insert rows.
            queued_update: Maps table_id (int) to list of staged update rows.
            queued_view: Maps JoinsType tuple to dictionary mapping non-None column tuples to view IDs.
            next_id: Maps table_id (int) to current auto-increment sequence integer ID.
            db: Database driver instance handle (or None until started).
        """
        self.tables: dict[int, Table] = {}
        self.queued_set: dict[int, dict[Any, Any]] = {}
        self.queued_update: dict[int, list[tuple[SafeDataType, ...]]] = {}
        self.queued_view: dict[JoinsType, dict[tuple[SafeDataType, ...], int]] = {}
        self.next_id: dict[int, int] = {}
        self._pk_getters: dict[int, Callable[[tuple[SafeDataType, ...]], Any]] = {}
        self._join_plan_cache: dict[JoinsType, list[tuple[int, int, int]]] = {}
        self._committed_pks: dict[int, set[Any]] = {}
        self._committed_nodup_keys: dict[int, dict[Any, int]] = {}
        self._committed_unique_keys: dict[int, dict[tuple[int, ...], set[tuple[SafeDataType, ...]]]] = {}
        self._table_unique_keys: dict[int, tuple[tuple[int, ...], ...]] = {}
        self._table_sec_indexes: dict[int, tuple[tuple[int, ...], ...]] = {}
        self._table_single_col_indexes: dict[int, tuple[int, ...]] = {}
        self._queued_sec_indexes: dict[int, dict[tuple[int, ...], dict[tuple[SafeDataType, ...], list[tuple[SafeDataType, ...]]]]] = {}
        self._queued_single_col_indexes: dict[int, dict[int, dict[SafeDataType, list[tuple[SafeDataType, ...]]]]] = {}
        self.db: Any | None = None
        # Background commits: commit_all() returns once a writer thread has the rows (see wait_for_commit())
        self.async_commits: bool = False
        self._pending: CommitBatch | None = None
        self._join_tables_cache: dict[JoinsType, frozenset[int]] = {}
        # Cumulative counters (callers diff them per stage): commits, rows written, seconds the writes took,
        # seconds the caller was blocked by them (all of a synchronous write, the join of a background one)
        self.commit_stats: dict[str, float] = {"commits": 0, "background": 0, "rows": 0, "write_s": 0.0, "blocked_s": 0.0}
        # Database reads issued by this engine (get/view fallbacks) and their seconds
        self.db_reads: int = 0
        self.db_read_s: float = 0.0
        self.update_in_mem_indexes: bool = True

    def close(self) -> None:
        """Wait for a pending background commit (raising its error), then safely close the database handle."""
        self.wait_for_commit()
        if getattr(self, "db", None) is not None:
            if hasattr(self.db, "close") and callable(self.db.close):
                try:
                    self.db.close()
                except Exception:
                    pass
            self.db = None

    def __enter__(self) -> Self:
        """Enter context manager scope."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Exit context manager scope and close DB connection."""
        self.close()

    def __del__(self) -> None:
        """Clean up resources upon garbage collection."""
        try:
            self.close()
        except Exception:
            pass

    def _register_table(self, table: Table) -> None:
        """Register table schema, precompile PK extractors, and build secondary index structures."""
        table_id = table.table_id
        self.tables[table_id] = table
        self.queued_set.setdefault(table_id, {})
        self.queued_update.setdefault(table_id, [])
        self.next_id.setdefault(table_id, 1)
        self._committed_pks.setdefault(table_id, set())
        self._committed_nodup_keys.setdefault(table_id, {})
        if table.primary:
            if len(table.primary) == 1:
                pk_idx = table.primary[0]
                self._pk_getters[table_id] = lambda row, idx=pk_idx: row[idx]
            else:
                self._pk_getters[table_id] = itemgetter(*table.primary)

        # Precompile secondary index and unique key column positions
        col_name_to_idx = {col[0]: idx for idx, col in enumerate(table.init_columns)}
        sec_indexes: list[tuple[int, ...]] = []
        single_indexes: set[int] = set()

        for idx_def in getattr(table, "indexes", ()) or ():
            col_names = ()
            if isinstance(idx_def, (tuple, list)):
                if len(idx_def) >= 2 and isinstance(idx_def[1], (tuple, list)):
                    col_names = idx_def[1]
                elif len(idx_def) == 1 and isinstance(idx_def[0], (tuple, list)):
                    col_names = idx_def[0]
                elif all(isinstance(x, str) for x in idx_def):
                    col_names = idx_def
            if col_names:
                col_indices = tuple(col_name_to_idx[c] for c in col_names if c in col_name_to_idx)
                if len(col_indices) == len(col_names) and col_indices:
                    if len(col_indices) > 1 and col_indices not in sec_indexes:
                        sec_indexes.append(col_indices)
                    for c in col_indices:
                        single_indexes.add(c)

        unique_indexes: list[tuple[int, ...]] = []
        for u_def in getattr(table, "unique_keys", ()) or ():
            col_names = ()
            if isinstance(u_def, (tuple, list)):
                if all(isinstance(x, str) for x in u_def):
                    col_names = u_def
                elif len(u_def) >= 2 and isinstance(u_def[1], (tuple, list)):
                    col_names = u_def[1]
            if col_names:
                col_indices = tuple(col_name_to_idx[c] for c in col_names if c in col_name_to_idx)
                if len(col_indices) == len(col_names) and col_indices:
                    if col_indices not in unique_indexes:
                        unique_indexes.append(col_indices)
                    if len(col_indices) > 1 and col_indices not in sec_indexes:
                        sec_indexes.append(col_indices)
                    for c in col_indices:
                        single_indexes.add(c)

        self._table_unique_keys[table_id] = tuple(unique_indexes)
        self._table_sec_indexes[table_id] = tuple(sec_indexes)
        self._table_single_col_indexes[table_id] = tuple(sorted(single_indexes))
        self._queued_sec_indexes[table_id] = {idx_cols: {} for idx_cols in self._table_sec_indexes[table_id]}
        self._queued_single_col_indexes[table_id] = {col_idx: {} for col_idx in self._table_single_col_indexes[table_id]}

    def start_new_db(self, db: Callable[[], Any] | type[Any], is_worker: bool = False) -> None:
        """Start or restart the database connection handle for Table Engine use.

        Args:
            db: Database class or factory callable (e.g., MariaDB or G.DB).
            is_worker: If True, indicates invocation in a forked worker process.

        Process:
            1. Closes any existing active database connection handle safely.
            2. Instantiates a new database driver instance: `self.db = db()`.
            3. Refreshes next_id and resets staging queues for registered tables.

        Outputs:
            None.
        """
        if is_worker:
            # A forked worker never owns the parent's background commit
            self._pending = None
            self.async_commits = False
        self.close()
        self.db = db()
        self.queued_view = {}
        for table_id, table in self.tables.items():
            self.queued_set[table_id] = {}
            self.queued_update[table_id] = []
            self._queued_sec_indexes[table_id] = {idx_cols: {} for idx_cols in self._table_sec_indexes.get(table_id, ())}
            self._queued_single_col_indexes[table_id] = {col_idx: {} for col_idx in self._table_single_col_indexes.get(table_id, ())}
            self._committed_pks[table_id] = set()
            self._committed_nodup_keys[table_id] = {}
            if table.initial_insert:
                for row in table.initial_insert:
                    if table.no_duplicate and len(row) > 1:
                        k = self._sanitize_key(row[1:])
                        self._committed_nodup_keys[table_id][k] = row[0]
                    if table.primary:
                        pk_fn = self._pk_getters.get(table_id)
                        pk = self._sanitize_key(pk_fn(row) if pk_fn is not None else itemgetter(*table.primary)(row))
                        self._committed_pks[table_id].add(pk)
            if self.db is not None:
                try:
                    self.next_id[table_id] = self.db.get_next_id(table)
                except Exception:
                    pass
        self.update_in_mem_indexes = True

    def start(self, tables: Sequence[Table] | Table, db: Callable[[], Any] | type[Any]) -> None:
        """Initialize Table Engine with schema tables and connect to database.

        Args:
            tables: Single Table instance or sequence of Table instances (gp.Table_Array).
            db: Database class or factory callable (e.g., MariaDB or G.DB).

        Process:
            1. Normalizes `tables` into a tuple if a single table was provided.
            2. Registers tables into `self.tables` and precompiles PK getters.
            3. Starts a fresh database connection and initializes queues via `self.start_new_db(db)`.

        Outputs:
            None.
        """
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        for table in tables:
            self._register_table(table)
        self.start_new_db(db)

    @staticmethod
    def _sanitize_key(key: Any) -> Any:
        """Coerce mutable bytearray or memoryview objects inside keys to immutable hashable bytes."""
        if type(key) in (int, str, bytes):
            return key
        if isinstance(key, (bytearray, memoryview)):
            return bytes(key)
        if isinstance(key, tuple):
            if any(isinstance(x, (bytearray, memoryview)) for x in key):
                return tuple(bytes(x) if isinstance(x, (bytearray, memoryview)) else x for x in key)
        return key

    def _index_staged_row(self, table_id: int, row: tuple[SafeDataType, ...]) -> None:
        """Index a staged row into secondary and single-column in-memory queue indexes."""
        sec_indexes = self._table_sec_indexes.get(table_id)
        if sec_indexes:
            sec_dict = self._queued_sec_indexes.get(table_id)
            if sec_dict is not None:
                for col_positions in sec_indexes:
                    if all(c < len(row) and row[c] is not None for c in col_positions):
                        key = tuple(self._sanitize_key(row[c]) for c in col_positions)
                        idx_map = sec_dict.setdefault(col_positions, {})
                        bucket = idx_map.get(key)
                        if bucket is None:
                            idx_map[key] = [row]
                        else:
                            bucket.append(row)

        single_indexes = self._table_single_col_indexes.get(table_id)
        if single_indexes:
            single_dict = self._queued_single_col_indexes.get(table_id)
            if single_dict is not None:
                for col_idx in single_indexes:
                    if col_idx < len(row):
                        val = row[col_idx]
                        if val is not None:
                            key = self._sanitize_key(val)
                            col_map = single_dict.setdefault(col_idx, {})
                            bucket = col_map.get(key)
                            if bucket is None:
                                col_map[key] = [row]
                            else:
                                bucket.append(row)

    def get(
        self,
        table_id: int,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Query single matching row from queued staged operations or directly from database.

        Args:
            table_id: Target table identifier integer.
            columns: Tuple of column values where None represents a wildcard.

        Process:
            1. Staging check in `queued_set`:
               - If `no_duplicate=True`: checks staged dictionary using `columns[1:]`.
               - If explicit PK provided: checks staged dictionary via PK key.
               - Otherwise: consults secondary and single-column queue indexes in O(1)
                 before falling back to scanning staged dictionary entries.
            2. Emptiness guard: If table has `has_auto_increment=True`, `initial_insert=None`,
               and `next_id <= 1`, returns `None` immediately without issuing SQL queries.
            3. Fallback: Executes direct database select via `self.db.select(table, columns)`.

        Outputs:
            Matching row tuple `tuple[SafeDataType, ...]` or None.
        """
        table = self.tables[table_id]

        # 1. Staging check in queued_set
        if table_id in self.queued_set:
            if table.no_duplicate:
                if len(columns) > 1 and all(c is not None for c in columns[1:]):
                    key = self._sanitize_key(columns[1:])
                    cached_id = self.queued_set[table_id].get(key)
                    if cached_id is not None:
                        row = (cached_id, *columns[1:])
                        if columns[0] is None or row[0] == columns[0]:
                            return row
                    committed_id = self._committed_nodup_keys.get(table_id, {}).get(key)
                    if committed_id is not None:
                        row = (committed_id, *columns[1:])
                        if columns[0] is None or row[0] == columns[0]:
                            return row
                else:
                    found_sec_index = False
                    sec_indexes = self._table_sec_indexes.get(table_id)
                    if sec_indexes and table_id in self._queued_sec_indexes:
                        sec_dict = self._queued_sec_indexes[table_id]
                        for col_positions in sec_indexes:
                            if all(c < len(columns) and columns[c] is not None for c in col_positions):
                                found_sec_index = True
                                key = tuple(self._sanitize_key(columns[c]) for c in col_positions)
                                candidates = sec_dict.get(col_positions, {}).get(key)
                                if candidates:
                                    for staged_row in candidates:
                                        match = True
                                        for i, val in enumerate(columns):
                                            if val is not None and (i >= len(staged_row) or staged_row[i] != val):
                                                match = False
                                                break
                                        if match:
                                            return staged_row
                                break

                    if not found_sec_index:
                        single_indexes = self._table_single_col_indexes.get(table_id)
                        if single_indexes and table_id in self._queued_single_col_indexes:
                            single_dict = self._queued_single_col_indexes[table_id]
                            matching_cols = [
                                c for c in single_indexes
                                if c < len(columns) and columns[c] is not None
                            ]
                            if matching_cols:
                                best_candidates: list[tuple[SafeDataType, ...]] | None = None
                                for c in matching_cols:
                                    val_key = self._sanitize_key(columns[c])
                                    c_candidates = single_dict.get(c, {}).get(val_key)
                                    if c_candidates is None:
                                        best_candidates = None
                                        found_sec_index = True
                                        break
                                    if best_candidates is None or len(c_candidates) < len(best_candidates):
                                        best_candidates = c_candidates
                                        found_sec_index = True

                                if best_candidates is not None:
                                    for staged_row in best_candidates:
                                        match = True
                                        for i, val in enumerate(columns):
                                            if val is not None and (i >= len(staged_row) or staged_row[i] != val):
                                                match = False
                                                break
                                        if match:
                                            return staged_row

                    if not found_sec_index:
                        for k, v in self.queued_set[table_id].items():
                            staged_row = (v, *k) if isinstance(k, tuple) else (v, k)
                            match = True
                            for i, val in enumerate(columns):
                                if val is not None and (i >= len(staged_row) or staged_row[i] != val):
                                    match = False
                                    break
                            if match:
                                return staged_row
            else:
                pk_specified = all(columns[i] is not None for i in table.primary)
                if pk_specified:
                    pk_fn = self._pk_getters.get(table_id)
                    pk = self._sanitize_key(pk_fn(columns) if pk_fn is not None else itemgetter(*table.primary)(columns))
                    staged_row = self.queued_set[table_id].get(pk)
                    if staged_row is not None:
                        match = True
                        for i, val in enumerate(columns):
                            if val is not None and (i >= len(staged_row) or staged_row[i] != val):
                                match = False
                                break
                        if match:
                            return staged_row
                else:
                    found_sec_index = False
                    sec_indexes = self._table_sec_indexes.get(table_id)
                    if sec_indexes and table_id in self._queued_sec_indexes:
                        sec_dict = self._queued_sec_indexes[table_id]
                        for col_positions in sec_indexes:
                            if all(c < len(columns) and columns[c] is not None for c in col_positions):
                                found_sec_index = True
                                key = tuple(self._sanitize_key(columns[c]) for c in col_positions)
                                candidates = sec_dict.get(col_positions, {}).get(key)
                                if candidates:
                                    for staged_row in candidates:
                                        match = True
                                        for i, val in enumerate(columns):
                                            if val is not None and (i >= len(staged_row) or staged_row[i] != val):
                                                match = False
                                                break
                                        if match:
                                            return staged_row
                                break

                    if not found_sec_index:
                        single_indexes = self._table_single_col_indexes.get(table_id)
                        if single_indexes and table_id in self._queued_single_col_indexes:
                            single_dict = self._queued_single_col_indexes[table_id]
                            matching_cols = [
                                c for c in single_indexes
                                if c < len(columns) and columns[c] is not None
                            ]
                            if matching_cols:
                                best_candidates: list[tuple[SafeDataType, ...]] | None = None
                                for c in matching_cols:
                                    val_key = self._sanitize_key(columns[c])
                                    c_candidates = single_dict.get(c, {}).get(val_key)
                                    if c_candidates is None:
                                        best_candidates = None
                                        found_sec_index = True
                                        break
                                    if best_candidates is None or len(c_candidates) < len(best_candidates):
                                        best_candidates = c_candidates
                                        found_sec_index = True

                                if best_candidates is not None:
                                    for staged_row in best_candidates:
                                        match = True
                                        for i, val in enumerate(columns):
                                            if val is not None and (i >= len(staged_row) or staged_row[i] != val):
                                                match = False
                                                break
                                        if match:
                                            return staged_row

                    if not found_sec_index:
                        for staged_row in self.queued_set[table_id].values():
                            match = True
                            for i, val in enumerate(columns):
                                if val is not None and (i >= len(staged_row) or staged_row[i] != val):
                                    match = False
                                    break
                            if match:
                                return staged_row

        # 2. Emptiness guard: Table has never received data and has no seed insert
        if getattr(table, "has_auto_increment", True) and table.initial_insert is None and self.next_id.get(table_id, 0) <= 1 and (table_id not in self.queued_set or not self.queued_set[table_id]):
            return None

        if self.db is None:
            return None

        return self._db_read((table_id,), self.db.select, table, columns)

    def set(
        self,
        table_id: int,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...]:
        """Stage an insert row in local memory, handling deduplication and sequence generation.

        Args:
            table_id: Target table identifier integer.
            columns: Row data tuple matching the table's column schema.

        Process:
            1. If table has `no_duplicate=True`:
               - Checks `self.queued_set[table_id]` using `columns[1:]` as key.
               - If match exists: returns `(cached_id, *columns[1:])`.
               - If new: assigns `self.next_id[table_id]`, increments counter,
                 stores `self.queued_set[table_id][columns[1:]] = assigned_id`,
                 and returns `(assigned_id, *columns[1:])`.
            2. If `columns[0] is None` (auto-increment generation):
               - Assigns `assigned_id = self.next_id[table_id]`, increments counter.
               - Builds `new_row = (assigned_id, *columns[1:])`.
               - Stores `self.queued_set[table_id][assigned_id] = new_row`.
               - Returns `new_row`.
            3. If explicit primary key provided (`columns[0] is not None`):
               - Extracts primary key keying via `self._pk_getters[table_id]`.
               - Stores `self.queued_set[table_id][pk] = columns`.
               - Returns `columns`.

        Outputs:
            Complete resolved row tuple `tuple[SafeDataType, ...]`.
        """
        table = self.tables[table_id]

        if table.no_duplicate:
            key = self._sanitize_key(columns[1:])
            current_set = self.queued_set[table_id].get(key)
            if current_set is not None:
                return (current_set, *columns[1:])

            committed_id = self._committed_nodup_keys.get(table_id, {}).get(key)
            if committed_id is not None:
                return (committed_id, *columns[1:])

            if columns[0] is not None:
                assigned_id = columns[0]
                if isinstance(assigned_id, int) and assigned_id >= self.next_id[table_id]:
                    self.next_id[table_id] = assigned_id + 1
            else:
                assigned_id = self.next_id[table_id]
                self.next_id[table_id] += 1
            self.queued_set[table_id][key] = assigned_id
            row = (assigned_id, *columns[1:])
            self._index_staged_row(table_id, row)
            return row

        if columns[0] is None:
            u_indexes = self._table_unique_keys.get(table_id)
            if u_indexes:
                sec_dict = self._queued_sec_indexes.get(table_id, {})
                single_dict = self._queued_single_col_indexes.get(table_id, {})
                for col_positions in u_indexes:
                    if all(c < len(columns) and columns[c] is not None for c in col_positions):
                        u_key = tuple(self._sanitize_key(columns[c]) for c in col_positions)
                        if len(col_positions) > 1:
                            existing_rows = sec_dict.get(col_positions, {}).get(u_key)
                            if existing_rows:
                                return existing_rows[0]
                        else:
                            c_idx = col_positions[0]
                            existing_rows = single_dict.get(c_idx, {}).get(u_key[0])
                            if existing_rows:
                                return existing_rows[0]
                        committed_u = self._committed_unique_keys.get(table_id, {}).get(col_positions)
                        if committed_u and u_key in committed_u:
                            return (None, *columns[1:])

            assigned_id = self.next_id[table_id]
            row = (assigned_id, *columns[1:])
            self.queued_set[table_id][assigned_id] = row
            self.next_id[table_id] += 1
            self._index_staged_row(table_id, row)
            return row

        pk_fn = self._pk_getters.get(table_id)
        pk = self._sanitize_key(pk_fn(columns) if pk_fn is not None else itemgetter(*table.primary)(columns))
        if getattr(table, "idempotent_pk", False) or getattr(table, "init_primary", ()) == ("hash",) or table.table_name in ("m_tag_code", "m_ast_hash"):
            if pk in self.queued_set[table_id]:
                return self.queued_set[table_id][pk]
            if pk in self._committed_pks.get(table_id, set()):
                return columns
        elif pk in self.queued_set[table_id]:
            raise ValueError(f"Duplicate primary key '{pk}' in table '{table.table_name}'")
        self.queued_set[table_id][pk] = columns
        self._index_staged_row(table_id, columns)
        return columns

    def get_hashing_table(self, table: Table | None) -> Table | None:
        """Resolve associated hashing table schema for target table if configured.

        Args:
            table: Target Table schema instance.

        Returns:
            Linked hashing Table schema instance, or None.
        """
        if table is None or not getattr(table, "hashing_table", False):
            return None

        ht = table.hashing_table
        if isinstance(ht, str):
            return next((t for t in self.tables.values() if t.table_name == ht), None)
        if isinstance(ht, int):
            return self.tables.get(ht)
        if hasattr(ht, "table_id"):
            return self.tables.get(ht.table_id, ht)
        if ht is True:
            default_name = f"{table.table_name}_hash"
            return next((t for t in self.tables.values() if t.table_name == default_name), None)
        return None

    def view_get(
        self,
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Execute single-row SELECT query across multi-table relational join graph or AST hash table.

        Args:
            joins: Relational join graph tuple (JoinsType).
            columns: Concatenated tuple of column filter values across joined tables.

        Process:
            1. Emptiness guard: Checks if the initial table in the join graph has data,
               safely handling cases where `table.initial_insert is None`.
            2. If initial table has `hashing_table` configured:
               - Computes deterministic SHA-256 hash from `(joins, filtered_columns)`.
               - Queries hash table (or checks staged buffer) for existing `ast_id`.
               - Returns matching view tuple with `ast_id` if found.
            3. Fallback: Executes multi-table join SELECT via `self.db.view_select(self.tables, joins, columns)`.

        Outputs:
            First matching joined row tuple `tuple[SafeDataType, ...]` or None.
        """
        initial_table_id = PointerGetter(joins).get_first_table_id()
        table = self.tables.get(initial_table_id)
        if table is None:
            return None
        if (
            getattr(table, "has_auto_increment", True)
            and table.initial_insert is None
            and self.next_id.get(initial_table_id, 0) <= 1
            and (initial_table_id not in self.queued_set or not self.queued_set[initial_table_id])
        ):
            return None

        filtered_columns = tuple(val for val in columns if val is not None)

        # Check if AST hash deduplication applies via table.hashing_table
        hash_table = self.get_hashing_table(table)
        if hash_table is not None:
            h = compute_ast_hash(joins, filtered_columns)
            # Check staged in-memory queued_set
            staged_row = self.queued_set.get(hash_table.table_id, {}).get(h)
            if staged_row is not None:
                ast_id = staged_row[1]
                return tuple(val if val is not None else ast_id for val in columns)

            # Check DB for existing hash
            hash_row = self.get(hash_table.table_id, (h, None))
            if hash_row is not None:
                ast_id = hash_row[1]
                return tuple(val if val is not None else ast_id for val in columns)

            return None

        if self.db is None:
            return None

        return self._db_read(self._join_table_ids(joins), self.db.view_select, self.tables, joins, columns)

    def view_get_multiple(
        self,
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> list[tuple[SafeDataType, ...]]:
        """Execute multi-row SELECT query across relational join graph, fetching all matches.

        Args:
            joins: Relational join graph tuple (JoinsType).
            columns: Concatenated tuple of column filter values across joined tables.

        Process:
            Executes multi-table join SELECT via `self.db.view_select_multiple(self.tables, joins, columns)`.

        Outputs:
            List of matching joined row tuples `list[tuple[SafeDataType, ...]]`.
        """
        if self.db is None:
            return []
        return self._db_read(self._join_table_ids(joins), self.db.view_select_multiple, self.tables, joins, columns)

    def get_in(self, table_id: int, col_idx: int, values: Sequence[SafeDataType]) -> list[tuple[SafeDataType, ...]]:
        """Fetch every committed row of a table whose column `col_idx` is one of `values` from the database.

        Staged rows are not included. The read waits for a background commit writing the table (`_db_read()`).
        """
        if self.db is None or not values:
            return []
        return self._db_read((table_id,), self.db.select_in, self.tables[table_id], col_idx, tuple(values))

    def view_set(
        self,
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...]:
        """Stage multi-table relational view, deduplicate via hashing_table / memory, and decompose.

        Args:
            joins: Relational join graph tuple (JoinsType).
            columns: Concatenated data tuple across joined tables (None at view ID positions).

        Process:
            1. Extracts non-None column values: `filtered = tuple(x for x in columns if x is not None)`.
            2. Checks `self.queued_view` cache for matching `joins` and `filtered`:
               - If cached: returns `columns` with None positions replaced by cached `view_id`.
            3. If main table has `hashing_table` configured:
               - Computes deterministic SHA-256 hash `h = compute_ast_hash(joins, filtered)`.
               - Checks staged `queued_set[hash_table]` or queries `self.get(hash_table.table_id, (h, None))`.
               - If existing: caches in `queued_view` and returns tuple with existing `ast_id`.
               - If new: generates `current_view_id = self.next_id[main_table_id]`, decomposes across
                 constituent tables, and stages `(h, current_view_id)` into `queued_set[hash_table]`.
            4. Fallback for non-hashed views: Standard relational view decomposition across tables.

        Outputs:
            Complete joined row tuple `tuple[SafeDataType, ...]`.
        """
        filtered_columns = tuple(val for val in columns if val is not None)

        if joins in self.queued_view:
            current_view_id = self.queued_view[joins].get(filtered_columns)
            if current_view_id is not None:
                return tuple(val if val is not None else current_view_id for val in columns)
        else:
            self.queued_view[joins] = {}

        main_table_id = PointerGetter(joins).get_first_table_id()
        main_table = self.tables.get(main_table_id)

        # Check if AST hash deduplication applies via table.hashing_table
        hash_table = self.get_hashing_table(main_table)
        if hash_table is not None:
            h = compute_ast_hash(joins, filtered_columns)

            # Check staged in-memory queued_set
            staged_row = self.queued_set.get(hash_table.table_id, {}).get(h)
            if staged_row is not None:
                current_view_id = staged_row[1]
                self.queued_view[joins][filtered_columns] = current_view_id
                return tuple(val if val is not None else current_view_id for val in columns)

            # Check DB / cache for existing hash
            hash_row = self.get(hash_table.table_id, (h, None))
            if hash_row is not None:
                current_view_id = hash_row[1]
                self.queued_view[joins][filtered_columns] = current_view_id
                return tuple(val if val is not None else current_view_id for val in columns)

            # New AST - allocate monotonic ID
            current_view_id = self.next_id[main_table_id]
            self.queued_view[joins][filtered_columns] = current_view_id
            self.next_id[main_table_id] += 1

            result = tuple(val if val is not None else current_view_id for val in columns)

            # Decompose result across tables using cached join plan
            if joins not in self._join_plan_cache:
                self._join_plan_cache[joins] = [
                    (pointer[0], self.tables[pointer[0]].length, repeat)
                    for repeat, pointer in PointerGetter(joins)
                ]

            data_offset = 0
            for t_id, t_len, repeat in self._join_plan_cache[joins]:
                for _ in range(repeat):
                    row = result[data_offset : data_offset + t_len]
                    self.set(t_id, row)
                    data_offset += t_len

            # Stage row in hash_table using self.set
            self.set(hash_table.table_id, (h, current_view_id))
            return result

        # Standard non-hashed view_set decomposition
        current_view_id = self.next_id[main_table_id]
        self.queued_view[joins][filtered_columns] = current_view_id
        self.next_id[main_table_id] += 1

        result = tuple(val if val is not None else current_view_id for val in columns)

        if joins not in self._join_plan_cache:
            self._join_plan_cache[joins] = [
                (pointer[0], self.tables[pointer[0]].length, repeat)
                for repeat, pointer in PointerGetter(joins)
            ]

        data_offset = 0
        for t_id, t_len, repeat in self._join_plan_cache[joins]:
            for _ in range(repeat):
                row = result[data_offset : data_offset + t_len]
                self.set(t_id, row)
                data_offset += t_len

        return result

    def update(
        self,
        table_id: int,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...]:
        """Stage a row update in local memory for batch execution on commit.

        Args:
            table_id: Target table identifier integer.
            columns: Fully resolved row tuple to update.

        Process:
            Appends `columns` to `self.queued_update[table_id]`.

        Outputs:
            The input `columns` tuple.
        """
        self.queued_update[table_id].append(columns)
        return columns

    def commit(self, table_id: int, update_in_mem_indexes: bool = True) -> None:
        """Flush staged insert and update operations for target table to database and clear buffers.

        Args:
            table_id: Target table identifier integer.
            update_in_mem_indexes: Whether in-memory index updates are enabled (passed to cached engines).

        Process:
            1. Waits for a pending background commit.
            2. Moves the table's staged rows into a `CommitBatch` (`_take_staged()`): `no_duplicate=True` rows
               become `(id_val, *data_key)`, keys already committed are skipped, rows are sorted by primary key.
            3. Writes it on this thread with `db.insert()` / `db.update()` (inside `db.transaction()` when the
               driver has one); if the database does not commit the table, its rows go back to staging and the
               error is raised.

        Outputs:
            None.
        """
        self.wait_for_commit()
        batch = self._take_staged((table_id,))
        if batch is not None:
            self._write_batch(batch, None, parallel=False)

    def commit_all(self, max_workers: int | None = None, update_in_mem_indexes: bool = True) -> None:
        """Flush staged insert and update operations across ALL tables concurrently and clear buffers.

        Args:
            max_workers: Maximum concurrent worker threads.
            update_in_mem_indexes: Whether in-memory index updates are enabled (passed to cached engines).

        Process:
            1. Waits for the previous background commit (raising its error after restoring its rows).
            2. Moves every table's staged rows into one `CommitBatch`.
            3. With `async_commits` (and a driver with `supports_background_commit`), starts a writer thread
               and returns; otherwise writes on this thread via `db.commit_tables_parallel()`.

        Outputs:
            None.
        """
        self.wait_for_commit()
        batch = self._take_staged(tuple(self.tables))
        if batch is None:
            return
        if self._background_commit_allowed(update_in_mem_indexes):
            self._submit(batch, max_workers)
        else:
            self._write_batch(batch, max_workers)

    # ------------------------------------------------------------------
    # Commit batches, background writer and read-your-writes barrier
    # ------------------------------------------------------------------
    def _reset_staged_indexes(self, table_id: int) -> None:
        """Replace a table's staged secondary indexes with empty ones."""
        if table_id in self._queued_sec_indexes:
            self._queued_sec_indexes[table_id] = {idx_cols: {} for idx_cols in self._table_sec_indexes.get(table_id, ())}
        if table_id in self._queued_single_col_indexes:
            self._queued_single_col_indexes[table_id] = {col_idx: {} for col_idx in self._table_single_col_indexes.get(table_id, ())}

    def _take_staged(self, table_ids: Iterable[int]) -> CommitBatch | None:
        """Move the staged rows of `table_ids` into a CommitBatch and record them as committed for deduplication.

        The staging containers are swapped for empty ones, so rows staged from now on are not part of the batch.
        Returns None when none of the tables has staged rows.
        """
        batch = CommitBatch()
        for table_id in table_ids:
            staged_set = self.queued_set.get(table_id)
            staged_upd = self.queued_update.get(table_id)
            if not staged_set and not staged_upd:
                continue
            table = self.tables[table_id]
            insert_payload: tuple[tuple[SafeDataType, ...], ...] = ()
            if staged_set:
                if table.no_duplicate:
                    committed_map = self._committed_nodup_keys.setdefault(table_id, {})
                    added: list[Any] = []
                    rows = []
                    for k, v in staged_set.items():
                        if k in committed_map:
                            continue
                        row = v if isinstance(v, (tuple, list)) else ((v, *k) if isinstance(k, tuple) else (v, k))
                        rows.append(row)
                        committed_map[k] = row[0]
                        added.append(k)
                    batch.added_nodup[table_id] = added
                elif _is_hash_keyed(table) and self._tracks_committed_pks(table_id):
                    committed_pks = self._committed_pks.setdefault(table_id, set())
                    added = []
                    rows = []
                    for pk, row in staged_set.items():
                        if pk in committed_pks:
                            continue
                        rows.append(row)
                        committed_pks.add(pk)
                        added.append(pk)
                    batch.added_pks[table_id] = added
                else:
                    rows = list(staged_set.values())

                if rows:
                    if table.primary and len(rows) > 1:
                        pk_fn = self._pk_getters.get(table_id)
                        rows.sort(key=pk_fn if pk_fn is not None else itemgetter(*table.primary))

                    u_indexes = self._table_unique_keys.get(table_id)
                    if u_indexes:
                        committed_u = self._committed_unique_keys.setdefault(table_id, {})
                        added_u = batch.added_unique.setdefault(table_id, {})
                        for col_positions in u_indexes:
                            u_set = committed_u.setdefault(col_positions, set())
                            new_keys = added_u.setdefault(col_positions, [])
                            for r in rows:
                                if all(c < len(r) and r[c] is not None for c in col_positions):
                                    u_key = tuple(self._sanitize_key(r[c]) for c in col_positions)
                                    if u_key not in u_set:
                                        u_set.add(u_key)
                                        new_keys.append(u_key)

                    insert_payload = tuple(rows)
                self.queued_set[table_id] = {}
                self._reset_staged_indexes(table_id)

            update_payload: tuple[tuple[SafeDataType, ...], ...] = ()
            if staged_upd:
                update_payload = tuple(staged_upd)
                self.queued_update[table_id] = []

            batch.staged[table_id] = (staged_set or {}, staged_upd or [])
            if insert_payload or update_payload:
                batch.tables_data.append((table, insert_payload, update_payload))
                batch.rows += len(insert_payload) + len(update_payload)
        if not batch.staged:
            return None
        batch.table_ids = frozenset(table.table_id for table, _ins, _upd in batch.tables_data)
        return batch

    def _tracks_committed_pks(self, table_id: int) -> bool:
        """Whether committed primary keys of a hash-keyed table are kept in `_committed_pks` for deduplication."""
        return True

    def _write_tables(
        self, batch: CommitBatch, db: Any, max_workers: int | None, dedicated: bool = False, parallel: bool = True
    ) -> None:
        """Write a batch with `db`; per-table stats land in `batch.stats`.

        `parallel` uses `db.commit_tables_parallel()` when available; otherwise each table is written with
        `db.insert()` / `db.update()`, as one transaction when the driver offers `transaction()`. A failure carries
        `failed_table_ids` (the tables not committed) when that can be told.
        """
        if not batch.tables_data:
            return
        if parallel and hasattr(db, "commit_tables_parallel"):
            if dedicated:
                stats = db.commit_tables_parallel(batch.tables_data, max_workers=max_workers, dedicated=True)
            else:
                stats = db.commit_tables_parallel(batch.tables_data, max_workers=max_workers)
            batch.stats = [tuple(st) for st in (stats or ())]
            return
        transaction = getattr(db, "transaction", None)
        for pos, (table, ins_p, upd_p) in enumerate(batch.tables_data):
            t_start = time.perf_counter()
            try:
                with transaction() if callable(transaction) else contextlib.nullcontext():
                    if ins_p:
                        db.insert(table, ins_p)
                    if upd_p:
                        db.update(table, upd_p)
            except Exception as e:
                if getattr(e, "failed_table_ids", None) is None:
                    e.failed_table_ids = {t.table_id for t, _ins, _upd in batch.tables_data[pos:]}
                raise
            batch.stats.append((table.table_name, len(ins_p), len(upd_p), time.perf_counter() - t_start))

    def _write_batch(self, batch: CommitBatch, max_workers: int | None, parallel: bool = True) -> None:
        """Write a batch on this thread; tables the database did not commit go back to staging and the error is raised."""
        t_start = time.perf_counter()
        try:
            self._write_tables(batch, self.db, max_workers, parallel=parallel)
        except BaseException as e:
            batch.write_s = time.perf_counter() - t_start
            self._account_commit(batch, blocked_s=batch.write_s)
            self._restore_batch(batch, getattr(e, "failed_table_ids", None))
            raise
        batch.write_s = time.perf_counter() - t_start
        self._account_commit(batch, blocked_s=batch.write_s)

    def _background_commit_allowed(self, update_in_mem_indexes: bool) -> bool:
        """Background writes need them switched on, a driver that writes on its own connections, and a live engine."""
        return bool(
            self.async_commits
            and update_in_mem_indexes
            and self.db is not None
            and getattr(self.db, "supports_background_commit", False)
        )

    def _submit(self, batch: CommitBatch, max_workers: int | None) -> None:
        """Write a batch on a background thread (`wait_for_commit()` joins it)."""
        db = self.db

        def _run() -> None:
            t_start = time.perf_counter()
            try:
                self._write_tables(batch, db, max_workers, dedicated=True)
            except BaseException as e:
                batch.error = e
            finally:
                batch.write_s = time.perf_counter() - t_start

        batch.background = True
        batch.thread = threading.Thread(target=_run, name="TableEngine writer", daemon=True)
        self._pending = batch
        batch.thread.start()

    @property
    def pending_commit_rows(self) -> int:
        """Rows of the background commit still being written (0 if none)."""
        return self._pending.rows if self._pending is not None else 0

    @property
    def staged_rows(self) -> int:
        """Rows staged for the next commit (inserts and updates, all tables)."""
        return sum(len(q) for q in self.queued_set.values()) + sum(len(u) for u in self.queued_update.values())

    def wait_for_commit(self) -> None:
        """Join the pending background commit.

        If it failed, the tables it did not commit go back to staging and its error is raised (a retried
        `commit_all()` writes them again).
        """
        batch = getattr(self, "_pending", None)
        if batch is None:
            return
        self._pending = None
        t_start = time.perf_counter()
        if batch.thread is not None:
            batch.thread.join()
        batch.wait_s = time.perf_counter() - t_start
        self._account_commit(batch, blocked_s=batch.wait_s)
        if batch.error is not None:
            self._restore_batch(batch, getattr(batch.error, "failed_table_ids", None))
            raise batch.error
        self._on_background_commit_done(batch)

    def _on_background_commit_done(self, batch: CommitBatch) -> None:
        """Hook run after a background commit was joined successfully."""

    def _account_commit(self, batch: CommitBatch, blocked_s: float) -> None:
        """Add a finished write to `commit_stats`."""
        stats = self.commit_stats
        stats["commits"] += 1
        stats["rows"] += batch.rows
        stats["write_s"] += batch.write_s
        stats["blocked_s"] += blocked_s
        if batch.background:
            stats["background"] += 1

    def _restore_batch(self, batch: CommitBatch, failed_ids: Iterable[int] | None = None) -> None:
        """Put the rows of the tables the database did not commit back into staging (all tables if unknown).

        Their keys leave the committed-key sets again, so the next commit writes them instead of skipping them.
        """
        staged_ids = frozenset(batch.staged)
        failed = staged_ids if failed_ids is None else frozenset(failed_ids) & staged_ids
        batch.restored = failed
        for table_id in failed:
            staged_set, staged_upd = batch.staged[table_id]
            committed_map = self._committed_nodup_keys.get(table_id)
            if committed_map is not None:
                for k in batch.added_nodup.get(table_id, ()):
                    committed_map.pop(k, None)
            if batch.added_pks.get(table_id):
                self._committed_pks.setdefault(table_id, set()).difference_update(batch.added_pks[table_id])
            for col_positions, keys in batch.added_unique.get(table_id, {}).items():
                self._committed_unique_keys.get(table_id, {}).get(col_positions, set()).difference_update(keys)
            if staged_set:
                current = self.queued_set.setdefault(table_id, {})
                for k, v in staged_set.items():
                    if k in current:
                        continue
                    current[k] = v
                    self._index_staged_row(
                        table_id, v if isinstance(v, (tuple, list)) else ((v, *k) if isinstance(k, tuple) else (v, k))
                    )
            if staged_upd:
                self.queued_update[table_id] = list(staged_upd) + self.queued_update.get(table_id, [])

    def _await_writes(self, table_ids: Iterable[int] | None = None) -> None:
        """Read-your-writes barrier: join the pending background commit if it writes any of `table_ids` (None: any)."""
        batch = self._pending
        if batch is not None and (table_ids is None or not batch.table_ids.isdisjoint(table_ids)):
            self.wait_for_commit()

    def _join_table_ids(self, joins: JoinsType) -> frozenset[int]:
        """Ids of every table a join graph reads."""
        ids = self._join_tables_cache.get(joins)
        if ids is None:
            if isinstance(joins, int):
                ids = frozenset((joins,))
            else:
                ids = frozenset(pointer[0] for _repeat, pointer in PointerGetter(joins))
            self._join_tables_cache[joins] = ids
        return ids

    def _db_read(self, table_ids: Iterable[int] | None, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a database read after the write barrier for `table_ids`, counting it in `db_reads`/`db_read_s`."""
        self._await_writes(table_ids)
        t_start = time.perf_counter()
        try:
            return fn(*args)
        finally:
            self.db_reads += 1
            self.db_read_s += time.perf_counter() - t_start
