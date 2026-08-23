"""table_engine/te_cached_db.py - In-Memory Cached Table Engine (TECachedDB).

===============================================================================
TABLE ENGINE (TECachedDB) ARCHITECTURAL GUIDE & CONTRACT SPECIFICATION
===============================================================================
This module implements the In-Memory Cached Table Engine (TECachedDB) for
KernelInfo-Parser. Extending TEDirectDB, it provides in-memory preloading,
multi-index acceleration (Primary Key, unique deduplication, and column indices),
and zero-latency query resolution for tables marked with `Table.te_cached = True`.

KEY RESPONSIBILITIES:
1. SELECTIVE TABLE PRELOADING:
   Preloads all records from the database at startup (`start()`) for tables
   where `table.te_cached is True`, eliminating repeated database SELECT queries.

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
        self._pk_index: dict[int, dict[Any, tuple[SafeDataType, ...]]] = {}
        self._nodup_index: dict[int, dict[tuple[SafeDataType, ...], int]] = {}
        self._col_indices: dict[int, dict[int, dict[SafeDataType, list[tuple[SafeDataType, ...]]]]] = {}

    def _is_cached(self, table: Table) -> bool:
        """Check if target table is configured for in-memory caching."""
        return bool(getattr(table, "te_cached", False))

    @staticmethod
    def _match_columns(row: tuple[SafeDataType, ...], filter_cols: tuple[SafeDataType, ...]) -> bool:
        """Check if row matches all non-None column filter criteria."""
        for i, val in enumerate(filter_cols):
            if val is not None and row[i] != val:
                return False
        return True

    def _index_row(self, table: Table, row: tuple[SafeDataType, ...]) -> None:
        """Add row to internal primary key, deduplication, and column indices."""
        table_id = table.table_id

        # 1. Primary Key Index
        pk = itemgetter(*table.primary)(row)
        self._pk_index[table_id][pk] = row

        # 2. Deduplication Index (for no_duplicate=True tables)
        if table.no_duplicate and len(row) > 1:
            self._nodup_index[table_id][row[1:]] = int(row[0]) if isinstance(row[0], int) else row[0]

        # 3. Secondary Column Indices
        for col_idx in range(table.length):
            val = row[col_idx]
            self._col_indices[table_id][col_idx].setdefault(val, []).append(row)

    def _unindex_row(self, table: Table, row: tuple[SafeDataType, ...]) -> None:
        """Remove row from internal indices prior to updating."""
        table_id = table.table_id

        # 1. Primary Key Index
        pk = itemgetter(*table.primary)(row)
        self._pk_index[table_id].pop(pk, None)

        # 2. Deduplication Index
        if table.no_duplicate and len(row) > 1:
            self._nodup_index[table_id].pop(row[1:], None)

        # 3. Secondary Column Indices
        for col_idx in range(table.length):
            val = row[col_idx]
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
            self._cached_rows[table_id] = []
            self._pk_index[table_id] = {}
            self._nodup_index[table_id] = {}
            self._col_indices[table_id] = {col_idx: {} for col_idx in range(length)}

    def clear_cache(self) -> None:
        """Clear all in-memory row storage and internal index structures."""
        self._cached_rows.clear()
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

        for table in self.tables.values():
            table_id = table.table_id
            self._ensure_table(table_id)

            if self._is_cached(table) and self.db is not None:
                joins: JoinsType = (((table.table_id, 0),),)
                cols = (None,) * table.length
                try:
                    rows = self.db.view_select_multiple(self.tables, joins, cols)
                except Exception:
                    rows = []
                if rows:
                    for row in rows:
                        self._cached_rows[table_id].append(row)
                        self._index_row(table, row)
                elif table.initial_insert:
                    for row in table.initial_insert:
                        self._cached_rows[table_id].append(row)
                        self._index_row(table, row)

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

        For tables where `table.te_cached is True`, resolves entirely from in-memory
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
            pk = itemgetter(*table.primary)(columns)
            row = self._pk_index[table_id].get(pk)
            if row is not None and self._match_columns(row, columns):
                return row
            return None

        # 2. Deduplication Key Fast-Path (no_duplicate=True)
        if table.no_duplicate and len(columns) > 1 and all(c is not None for c in columns[1:]):
            assigned_id = self._nodup_index[table_id].get(columns[1:])
            if assigned_id is not None:
                row = (assigned_id, *columns[1:])
                if self._match_columns(row, columns):
                    return row
            return None

        # 3. Column Index Accelerated Path
        indexed_cols = [(col_idx, val) for col_idx, val in enumerate(columns) if val is not None]
        if indexed_cols:
            # Pick column index with smallest candidate pool
            best_candidates: list[tuple[SafeDataType, ...]] | None = None
            for col_idx, val in indexed_cols:
                col_dict = self._col_indices[table_id].get(col_idx)
                if col_dict is not None:
                    candidates = col_dict.get(val)
                    if candidates is None:
                        return None
                    if best_candidates is None or len(candidates) < len(best_candidates):
                        best_candidates = candidates

            if best_candidates is not None:
                for row in best_candidates:
                    if self._match_columns(row, columns):
                        return row
                return None

        # 4. In-Memory Linear Scan Fallback
        for row in self._cached_rows[table_id]:
            if self._match_columns(row, columns):
                return row

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
        if not self._is_cached(table):
            return super().set(table_id, columns)

        self._ensure_table(table_id)

        if table.no_duplicate:
            key = columns[1:]
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
            self._cached_rows[table_id].append(row)
            self._index_row(table, row)
            return row

        if columns[0] is None:
            assigned_id = self.next_id[table_id]
            row = (assigned_id, *columns[1:])
            self.queued_set[table_id][assigned_id] = row
            self.next_id[table_id] += 1
            self._cached_rows[table_id].append(row)
            self._index_row(table, row)
            return row

        pk = itemgetter(*table.primary)(columns)
        self.queued_set[table_id][pk] = columns

        existing_row = self._pk_index[table_id].get(pk)
        if existing_row is not None:
            self._unindex_row(table, existing_row)
            try:
                self._cached_rows[table_id].remove(existing_row)
            except ValueError:
                pass

        self._cached_rows[table_id].append(columns)
        self._index_row(table, columns)
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

        if self._is_cached(table):
            self._ensure_table(table_id)
            pk = itemgetter(*table.primary)(columns)
            existing_row = self._pk_index[table_id].get(pk)
            if existing_row is not None:
                self._unindex_row(table, existing_row)
                try:
                    self._cached_rows[table_id].remove(existing_row)
                except ValueError:
                    pass
            self._cached_rows[table_id].append(columns)
            self._index_row(table, columns)

        return columns

    def close(self) -> None:
        """Safely clean up in-memory cache structures and close DB connection."""
        self.clear_cache()
        super().close()
