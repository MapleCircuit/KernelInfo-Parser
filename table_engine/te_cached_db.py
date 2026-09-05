"""table_engine/te_cached_db.py - In-Memory Cached Table Engine (TECachedDB).

===============================================================================
TABLE ENGINE (TECachedDB) ARCHITECTURAL GUIDE & CONTRACT SPECIFICATION
===============================================================================
This module implements the In-Memory Cached Table Engine (TECachedDB) for
KernelInfo-Parser. Extending TEDirectDB, it provides in-memory preloading,
multi-index acceleration (Primary Key, unique deduplication, and column indices),
and zero-latency query resolution for tables marked with `Table.te_cached`.

KEY RESPONSIBILITIES:
1. SELECTIVE TABLE PRELOADING:
   Preloads records from the database at startup (`start()`) for tables
   where `table.te_cached` is configured, eliminating repeated database SELECT queries.
   For tables with column-level caching, only cached columns are retained in memory.

2. MULTI-INDEX IN-MEMORY ACCELERATION:
   - `_pk_index`: O(1) row retrieval via Primary Key (`itemgetter(*table.primary)`).
   - `_nodup_index`: O(1) deduplication and ID resolution on `columns[1:]`.
   - `_col_indices`: In-memory inverted column indices for fast partial filter matching.

3. REAL-TIME CACHE SYNCHRONIZATION:
   Staged inserts (`set()`), updates (`update()`), and decomposed views (`view_set()`)
   immediately update in-memory caches and indices so queries reflect live modifications.

4. TRANSACTION COMMIT CONTINUITY:
   Flushes staged queues (`queued_set`, `queued_update`) to the database via
   `commit()` and `commit_all()` while retaining in-memory caches and indices.
===============================================================================
"""
from __future__ import annotations

import logging
from operator import itemgetter
from typing import TYPE_CHECKING, Any, Callable, Sequence
from types import TracebackType

from core.globalstuff import JoinsType, PointerGetter, SafeDataType
from table_engine.te_direct_db import TEDirectDB

if TYPE_CHECKING:
    from core.TableHandling import Table

logger = logging.getLogger(__name__)


class TECachedDB(TEDirectDB):
    """In-Memory Cached Table Engine with selective table preloading and internal indexing."""

    def __init__(self) -> None:
        """Initialize Table Engine state and in-memory cache structures."""
        super().__init__()
        self._cached_rows: dict[int, list[tuple[SafeDataType, ...]]] = {}
        self._cached_rows_pos: dict[int, dict[Any, int]] = {}
        self._pk_index: dict[int, dict[Any, tuple[SafeDataType, ...]]] = {}
        self._nodup_index: dict[int, dict[tuple[SafeDataType, ...], int]] = {}
        self._col_indices: dict[int, dict[int, dict[SafeDataType, list[tuple[SafeDataType, ...]]]]] = {}
        self.update_in_mem_indexes: bool = True

    def _is_cached(self, table: Table) -> bool:
        """Check if target table is configured for in-memory caching."""
        return bool(getattr(table, "te_cached", False))

    def _is_version_scoped(self, table: Table) -> bool:
        """Check if target table is configured for version-scoped working-window caching."""
        return bool(getattr(table, "version_scoped", False))

    def _get_vid_col_idx(self, table: Table) -> int | None:
        """Locate the 0-indexed column position of the version ID column ('vid' or 'vid_s')."""
        for idx, col in enumerate(table.init_columns):
            if col[0] in ("vid", "vid_s"):
                return idx
        return None

    def _get_min_active_vid(self) -> int:
        """Retrieve minimum active version ID (Old_VID) from global state if available."""
        import sys
        main_mod = sys.modules.get("__main__")
        if main_mod is not None:
            gp = getattr(main_mod, "gp", None)
            if gp is not None:
                return getattr(gp, "Old_VID", 0)
        return 0

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

    @classmethod
    def _project_row(cls, table: Table, row: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]:
        """Project row to only retain configured cached columns, substituting un-cached with None."""
        cached_cols = getattr(table, "cached_columns", None)
        if cached_cols is None or len(cached_cols) == table.length:
            if any(isinstance(val, (bytearray, memoryview)) for val in row):
                return tuple(bytes(val) if isinstance(val, (bytearray, memoryview)) else val for val in row)
            return row
        sanitized = tuple(bytes(val) if isinstance(val, (bytearray, memoryview)) else val for val in row)
        return tuple(sanitized[i] if i in cached_cols else None for i in range(table.length))

    @classmethod
    def _reconstruct_partial_row(
        cls,
        table: Table,
        selected_row: tuple[SafeDataType, ...],
        cached_cols: tuple[int, ...],
    ) -> tuple[SafeDataType, ...]:
        """Reconstruct full canonical row of length table.length from a column-projected query result."""
        full_row = [None] * table.length
        for idx, col_pos in enumerate(cached_cols):
            val = selected_row[idx]
            full_row[col_pos] = bytes(val) if isinstance(val, (bytearray, memoryview)) else val
        return tuple(full_row)

    @staticmethod
    def _match_columns(table: Table, row: tuple[SafeDataType, ...], filter_cols: tuple[SafeDataType, ...]) -> bool:
        """Check if row matches non-None column filter criteria for cached columns."""
        cached_cols = getattr(table, "cached_columns", None)
        has_cached_filter = False
        for i, val in enumerate(filter_cols):
            if val is not None:
                match_val = bytes(val) if isinstance(val, (bytearray, memoryview)) else val
                if cached_cols is not None and i not in cached_cols:
                    if row[i] is not None and row[i] != match_val:
                        return False
                else:
                    has_cached_filter = True
                    if row[i] != match_val:
                        return False
        if cached_cols is not None and len(cached_cols) < table.length:
            any_filter = any(val is not None for val in filter_cols)
            if any_filter and not has_cached_filter:
                return False
        return True

    def _index_row(self, table: Table, row: tuple[SafeDataType, ...]) -> None:
        """Add row to internal primary key, deduplication, and column indices."""
        table_id = table.table_id
        cached_cols = getattr(table, "cached_columns", None)
        pk_set = set(table.primary) if table.primary else set()

        # 1. Primary Key Index
        if all(row[i] is not None for i in table.primary):
            pk_fn = self._pk_getters.get(table_id)
            pk = self._sanitize_key(pk_fn(row) if pk_fn is not None else itemgetter(*table.primary)(row))
            self._pk_index[table_id][pk] = row

        # 2. Deduplication Index (for no_duplicate=True tables)
        if table.no_duplicate and len(row) > 1:
            if all(c is not None for c in row[1:]):
                nodup_key = self._sanitize_key(row[1:])
                self._nodup_index[table_id][nodup_key] = int(row[0]) if isinstance(row[0], int) else row[0]

        # 3. Secondary Column Indices (Skip columns covered by Primary Key to eliminate redundant memory allocations)
        for col_idx in range(table.length):
            if col_idx in pk_set:
                continue
            if cached_cols is None or col_idx in cached_cols:
                val = row[col_idx]
                if val is not None:
                    val = bytes(val) if isinstance(val, (bytearray, memoryview)) else val
                    self._col_indices[table_id].setdefault(col_idx, {}).setdefault(val, []).append(row)

    def _unindex_row(self, table: Table, row: tuple[SafeDataType, ...]) -> None:
        """Remove row from internal indices prior to updating."""
        table_id = table.table_id
        cached_cols = getattr(table, "cached_columns", None)
        pk_set = set(table.primary) if table.primary else set()

        # 1. Primary Key Index
        if all(row[i] is not None for i in table.primary):
            pk_fn = self._pk_getters.get(table_id)
            pk = self._sanitize_key(pk_fn(row) if pk_fn is not None else itemgetter(*table.primary)(row))
            self._pk_index[table_id].pop(pk, None)

        # 2. Deduplication Index
        if table.no_duplicate and len(row) > 1:
            if all(c is not None for c in row[1:]):
                nodup_key = self._sanitize_key(row[1:])
                self._nodup_index[table_id].pop(nodup_key, None)

        # 3. Secondary Column Indices
        for col_idx in range(table.length):
            if col_idx in pk_set:
                continue
            if cached_cols is None or col_idx in cached_cols:
                val = row[col_idx]
                if val is not None:
                    val = bytes(val) if isinstance(val, (bytearray, memoryview)) else val
                    if col_idx in self._col_indices[table_id] and val in self._col_indices[table_id][col_idx]:
                        try:
                            self._col_indices[table_id][col_idx][val].remove(row)
                        except ValueError:
                            pass

    def _ensure_table(self, table_id: int) -> None:
        """Ensure internal cache and index structures exist for the given table_id."""
        if table_id not in self._cached_rows:
            table = self.tables.get(table_id)
            length = table.length if table is not None else 0
            pk_set = set(table.primary) if table is not None and table.primary else set()
            self._cached_rows[table_id] = []
            self._cached_rows_pos[table_id] = {}
            self._pk_index[table_id] = {}
            self._nodup_index[table_id] = {}
            self._col_indices[table_id] = {col_idx: {} for col_idx in range(length) if col_idx not in pk_set}

    def clear_cache(self) -> None:
        """Clear all in-memory row storage and internal index structures."""
        self._cached_rows.clear()
        self._cached_rows_pos.clear()
        self._pk_index.clear()
        self._nodup_index.clear()
        self._col_indices.clear()

    def start_new_db(self, db: Callable[[], Any] | type[Any]) -> None:
        """Start or restart database connection and preload cached tables.

        Args:
            db: Database class or factory callable (e.g., MariaDB or MockDB).
        """
        super().start_new_db(db)
        self.clear_cache()

        min_active_vid = self._get_min_active_vid()

        for table in self.tables.values():
            table_id = table.table_id
            self._ensure_table(table_id)

            if self._is_cached(table) and self.db is not None:
                is_v_scoped = self._is_version_scoped(table)
                cached_cols = getattr(table, "cached_columns", None)
                min_vid = min_active_vid if (is_v_scoped and min_active_vid > 0) else None

                try:
                    if hasattr(self.db, "select_preload"):
                        raw_rows = self.db.select_preload(table, cached_columns=cached_cols, min_vid=min_vid)
                    else:
                        joins: JoinsType = (((table.table_id, 0),),)
                        cols = (None,) * table.length
                        raw_rows = self.db.view_select_multiple(self.tables, joins, cols)
                except Exception:
                    raw_rows = []

                if raw_rows:
                    is_partial_query = (
                        cached_cols is not None
                        and len(cached_cols) < table.length
                        and hasattr(self.db, "select_preload")
                    )
                    for row in raw_rows:
                        if is_partial_query:
                            proj_row = self._reconstruct_partial_row(table, row, cached_cols)
                        else:
                            # Fallback version check if driver did not filter
                            if is_v_scoped and min_active_vid > 0 and not hasattr(self.db, "select_preload"):
                                vid_col = self._get_vid_col_idx(table)
                                if vid_col is not None and row[vid_col] is not None and row[vid_col] < min_active_vid:
                                    continue
                            proj_row = self._project_row(table, row)
                        pos = len(self._cached_rows[table_id])
                        self._cached_rows[table_id].append(proj_row)
                        if table.primary and all(proj_row[i] is not None for i in table.primary):
                            pk_fn = self._pk_getters.get(table_id)
                            pk = self._sanitize_key(pk_fn(proj_row) if pk_fn is not None else itemgetter(*table.primary)(proj_row))
                            self._cached_rows_pos[table_id][pk] = pos
                        self._index_row(table, proj_row)
                elif table.initial_insert:
                    for row in table.initial_insert:
                        proj_row = self._project_row(table, row)
                        pos = len(self._cached_rows[table_id])
                        self._cached_rows[table_id].append(proj_row)
                        if table.primary and all(proj_row[i] is not None for i in table.primary):
                            pk_fn = self._pk_getters.get(table_id)
                            pk = self._sanitize_key(pk_fn(proj_row) if pk_fn is not None else itemgetter(*table.primary)(proj_row))
                            self._cached_rows_pos[table_id][pk] = pos
                        self._index_row(table, proj_row)

    def start(self, tables: Sequence[Table] | Table, db: Callable[[], Any] | type[Any]) -> None:
        """Initialize Table Engine, connect to database, and preload cached tables.

        Args:
            tables: Single Table instance or sequence of Table instances.
            db: Database class or factory callable (e.g., MariaDB or MockDB).
        """
        super().start(tables, db)

    def get(
        self,
        table_id: int,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Query single row matching non-None column filter criteria.

        For tables where `table.te_cached` is configured, resolves entirely from in-memory
        cache and internal indices with zero database SELECT queries.

        Args:
            table_id: Target table identifier integer.
            columns: Row filter tuple with None positions acting as wildcards.

        Returns:
            Matching row tuple or None.
        """
        table = self.tables[table_id]
        if not self._is_cached(table):
            return super().get(table_id, columns)

        self._ensure_table(table_id)

        # 1. Primary Key Fast-Path
        pk_specified = all(columns[i] is not None for i in table.primary)
        if pk_specified:
            pk_fn = self._pk_getters.get(table_id)
            pk = self._sanitize_key(pk_fn(columns) if pk_fn is not None else itemgetter(*table.primary)(columns))
            row = self._pk_index[table_id].get(pk)
            if row is not None and self._match_columns(table, row, columns):
                return row
            if self._is_version_scoped(table) and self.db is not None:
                return self.db.select(table, columns)
            return None

        # 2. Deduplication Key Fast-Path (no_duplicate=True)
        if table.no_duplicate and len(columns) > 1 and all(c is not None for c in columns[1:]):
            nodup_key = self._sanitize_key(columns[1:])
            assigned_id = self._nodup_index[table_id].get(nodup_key)
            if assigned_id is not None:
                row = (assigned_id, *columns[1:])
                if self._match_columns(table, row, columns):
                    return row
            return None

        # 3. Column Index Accelerated Path
        indexed_cols = [
            (col_idx, val) for col_idx, val in enumerate(columns)
            if val is not None and col_idx in self._col_indices[table_id]
        ]
        if indexed_cols:
            # Pick column index with smallest candidate pool
            best_candidates: list[tuple[SafeDataType, ...]] | None = None
            for col_idx, val in indexed_cols:
                col_dict = self._col_indices[table_id].get(col_idx)
                if col_dict is not None:
                    lookup_val = bytes(val) if isinstance(val, (bytearray, memoryview)) else val
                    candidates = col_dict.get(lookup_val)
                    if candidates is None:
                        return None
                    if best_candidates is None or len(candidates) < len(best_candidates):
                        best_candidates = candidates

            if best_candidates is not None:
                for row in best_candidates:
                    if self._match_columns(table, row, columns):
                        return row
                if self._is_version_scoped(table) and self.db is not None:
                    return self.db.select(table, columns)
                return None

        # 4. In-Memory Linear Scan Fallback with Hoisted Filters
        non_none_filters = [
            (i, bytes(val) if isinstance(val, (bytearray, memoryview)) else val)
            for i, val in enumerate(columns)
            if val is not None
        ]
        if not non_none_filters:
            if self._cached_rows[table_id]:
                return self._cached_rows[table_id][0]
        else:
            cached_cols = getattr(table, "cached_columns", None)
            is_full_cached = cached_cols is None or len(cached_cols) == table.length
            for row in self._cached_rows[table_id]:
                if is_full_cached:
                    match = True
                    for i, match_val in non_none_filters:
                        if row[i] != match_val:
                            match = False
                            break
                    if match:
                        return row
                else:
                    if self._match_columns(table, row, columns):
                        return row

        if self._is_version_scoped(table) and self.db is not None:
            return self.db.select(table, columns)

        return None

    def set(
        self,
        table_id: int,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...]:
        """Stage an insert row in local memory, handling deduplication and sequence generation.

        Synchronizes in-memory caches and indices in real time for cached tables.

        Args:
            table_id: Target table identifier integer.
            columns: Row data tuple matching table column schema.

        Returns:
            Complete resolved row tuple.
        """
        table = self.tables[table_id]
        if not self._is_cached(table) or not self.update_in_mem_indexes:
            return super().set(table_id, columns)

        self._ensure_table(table_id)

        if table.no_duplicate:
            key = self._sanitize_key(columns[1:])
            cached_id = self._nodup_index[table_id].get(key)
            if cached_id is not None:
                return (cached_id, *columns[1:])

            # Check staged queued_set
            staged_id = self.queued_set[table_id].get(key)
            if staged_id is not None:
                return (staged_id, *columns[1:])

            assigned_id = self.next_id[table_id]
            self.queued_set[table_id][key] = assigned_id
            self.next_id[table_id] += 1
            row = (assigned_id, *columns[1:])
            proj_row = self._project_row(table, row)
            pos = len(self._cached_rows[table_id])
            self._cached_rows[table_id].append(proj_row)
            if table.primary:
                pk_fn = self._pk_getters.get(table_id)
                pk = self._sanitize_key(pk_fn(proj_row) if pk_fn is not None else itemgetter(*table.primary)(proj_row))
                self._cached_rows_pos[table_id][pk] = pos
            self._index_row(table, proj_row)
            return row

        if columns[0] is None:
            assigned_id = self.next_id[table_id]
            row = (assigned_id, *columns[1:])
            self.queued_set[table_id][assigned_id] = row
            self.next_id[table_id] += 1
            proj_row = self._project_row(table, row)
            pos = len(self._cached_rows[table_id])
            self._cached_rows[table_id].append(proj_row)
            if table.primary:
                pk_fn = self._pk_getters.get(table_id)
                pk = self._sanitize_key(pk_fn(proj_row) if pk_fn is not None else itemgetter(*table.primary)(proj_row))
                self._cached_rows_pos[table_id][pk] = pos
            self._index_row(table, proj_row)
            return row

        pk_fn = self._pk_getters.get(table_id)
        pk = self._sanitize_key(pk_fn(columns) if pk_fn is not None else itemgetter(*table.primary)(columns))

        existing_row = self._pk_index[table_id].get(pk)
        if existing_row is not None:
            proj_row = self._project_row(table, columns)
            if existing_row == proj_row:
                return columns

            self._unindex_row(table, existing_row)
            pos = self._cached_rows_pos[table_id].get(pk)
            if pos is not None and pos < len(self._cached_rows[table_id]) and self._cached_rows[table_id][pos] == existing_row:
                self._cached_rows[table_id][pos] = proj_row
            else:
                try:
                    self._cached_rows[table_id].remove(existing_row)
                except ValueError:
                    pass
                pos = len(self._cached_rows[table_id])
                self._cached_rows[table_id].append(proj_row)
                self._cached_rows_pos[table_id][pk] = pos
        else:
            proj_row = self._project_row(table, columns)
            pos = len(self._cached_rows[table_id])
            self._cached_rows[table_id].append(proj_row)
            self._cached_rows_pos[table_id][pk] = pos

        self.queued_set[table_id][pk] = columns
        self._index_row(table, proj_row)
        return columns

    def update(
        self,
        table_id: int,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...]:
        """Stage a row update in local memory and update in-memory cache and indices.

        Args:
            table_id: Target table identifier integer.
            columns: Fully resolved row tuple to update.

        Returns:
            The input columns tuple.
        """
        table = self.tables[table_id]
        super().update(table_id, columns)

        if self._is_cached(table) and self.update_in_mem_indexes:
            self._ensure_table(table_id)
            pk_fn = self._pk_getters.get(table_id)
            pk = self._sanitize_key(pk_fn(columns) if pk_fn is not None else itemgetter(*table.primary)(columns))
            existing_row = self._pk_index[table_id].get(pk)
            proj_row = self._project_row(table, columns)
            if existing_row is not None:
                if existing_row != proj_row:
                    self._unindex_row(table, existing_row)
                    pos = self._cached_rows_pos[table_id].get(pk)
                    if pos is not None and pos < len(self._cached_rows[table_id]) and self._cached_rows[table_id][pos] == existing_row:
                        self._cached_rows[table_id][pos] = proj_row
                    else:
                        try:
                            self._cached_rows[table_id].remove(existing_row)
                        except ValueError:
                            pass
                        pos = len(self._cached_rows[table_id])
                        self._cached_rows[table_id].append(proj_row)
                        self._cached_rows_pos[table_id][pk] = pos
                    self._index_row(table, proj_row)
            else:
                pos = len(self._cached_rows[table_id])
                self._cached_rows[table_id].append(proj_row)
                self._cached_rows_pos[table_id][pk] = pos
                self._index_row(table, proj_row)

        return columns

    def commit(self, table_id: int, update_in_mem_indexes: bool = True) -> None:
        """Flush staged insert and update operations for target table to database.

        Args:
            table_id: Target table identifier integer.
            update_in_mem_indexes: If False, clears target table from in-memory cache structures.
        """
        super().commit(table_id, update_in_mem_indexes=update_in_mem_indexes)
        if not update_in_mem_indexes and table_id in self.tables:
            self._cached_rows.pop(table_id, None)
            self._cached_rows_pos.pop(table_id, None)
            self._pk_index.pop(table_id, None)
            self._nodup_index.pop(table_id, None)
            self._col_indices.pop(table_id, None)

    def commit_all(self, max_workers: int | None = None, update_in_mem_indexes: bool = True) -> None:
        """Flush staged operations across ALL tables and optionally clear in-memory caches.

        Args:
            max_workers: Maximum concurrent worker threads.
            update_in_mem_indexes: If False, immediately evacuates all in-memory caches and indices.
        """
        super().commit_all(max_workers=max_workers, update_in_mem_indexes=update_in_mem_indexes)
        if not update_in_mem_indexes:
            self.clear_cache()

    def close(self) -> None:
        """Safely clean up in-memory cache structures and close DB connection."""
        self.clear_cache()
        super().close()
