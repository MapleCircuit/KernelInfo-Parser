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
import time
from operator import itemgetter
from typing import TYPE_CHECKING, Any, Callable, Sequence
from types import TracebackType

from core.globalstuff import JoinsType, PointerGetter, SafeDataType
from table_engine.shared_buffer import SharedTableBuilder, SharedTablePKView, SharedTableRowSequenceView
from table_engine.te_direct_db import CommitBatch, TEDirectDB

if TYPE_CHECKING:
    from core.TableHandling import Table

logger = logging.getLogger(__name__)


class TECachedDB(TEDirectDB):
    """In-Memory Cached Table Engine with selective table preloading and internal indexing."""

    # Hash-keyed deduplication tables, written only through this TableEngine and needed in full. Their snapshot
    # outlives close()/start() (single writer): the next start reuses it plus the rows committed since, instead
    # of fetching, indexing and repacking millions of hashes for every version.
    CARRIED_TABLES = frozenset({"m_ast_hash", "m_tag_code"})
    # Reload carried tables from the database once rows committed since the snapshot exceed this share of it
    CARRY_RELOAD_RATIO = 0.5
    # Tables whose committed rows leave the heap at each commit (their keys stay in _committed_pks)
    WRITE_HEAVY_TABLES = frozenset({
        "m_ast_hash", "m_tag_code", "m_blob_symbol_def", "m_ast_container", "m_bridge_tag", "m_bridge_checkpoint", "m_blob_transition",
    })

    def __init__(self, hugepages: str | None = None) -> None:
        """Initialize Table Engine state and in-memory cache structures."""
        super().__init__()
        self._cached_rows: dict[int, list[tuple[SafeDataType, ...]]] = {}
        self._cached_rows_pos: dict[int, dict[Any, int]] = {}
        self._pk_index: dict[int, dict[Any, tuple[SafeDataType, ...]]] = {}
        self._nodup_index: dict[int, dict[tuple[SafeDataType, ...], int]] = {}
        # No-duplicate tables whose _nodup_index holds every row the database has (preloaded and indexed
        # since): a key they miss is new, so it needs no lookup (`_committed_nodup_id()`).
        self._nodup_complete: set[int] = set()
        self._col_indices: dict[int, dict[int, dict[SafeDataType, list[tuple[SafeDataType, ...]]]]] = {}
        self.update_in_mem_indexes: bool = True
        self._shared_buffer: Any | None = None
        # Carried tables (CARRIED_TABLES): their snapshot reader and, per table, the rows committed since
        self._carried_reader: Any | None = None
        self._carried_overlays: dict[int, dict[Any, tuple[SafeDataType, ...]]] = {}
        # Version-scoped tables preloaded in full from a version on: table_id -> (version column, first version).
        # A primary-key miss at or after that version is final (`_miss_is_final()`).
        self._preload_window: dict[int, tuple[int, int]] = {}
        if hugepages is None:
            try:
                from core.config import get_parser_config
                hugepages = get_parser_config().get("hugepages", "auto")
            except Exception:
                hugepages = "auto"
        self.hugepages: str = str(hugepages).lower().strip()

    def _is_cached(self, table: Table) -> bool:
        """Check if target table is configured for in-memory caching."""
        return bool(getattr(table, "te_cached", False))

    def _is_version_scoped(self, table: Table) -> bool:
        """Check if target table is configured for version-scoped or blob-scoped working-window caching."""
        return bool(getattr(table, "blob_scoped", False) or getattr(table, "version_scoped", False))

    def _get_vid_col_idx(self, table: Table) -> int | None:
        """Locate the 0-indexed column position of the version scope column ('vid', 'vid_s' or 'commit_id').

        Blob ids are a different id space than the commit id the preload window is expressed in.
        """
        for idx, col in enumerate(table.init_columns):
            if col[0] in ("vid", "vid_s", "commit_id"):
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

    @staticmethod
    def _normalize_binary_val(val: Any) -> bytes:
        """Coerce binary column value (bytearray, bytes, or latin1/utf-8 decoded string) to canonical bytes."""
        if isinstance(val, (bytearray, memoryview)):
            return bytes(val)
        if isinstance(val, str):
            try:
                return val.encode("latin1")
            except UnicodeEncodeError:
                return val.encode("utf-8")
        return bytes(val)

    @classmethod
    def _project_row(cls, table: Table, row: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]:
        """Project row to only retain configured cached columns, substituting un-cached with None."""
        cached_cols = getattr(table, "cached_columns", None)
        cols_def = table.init_columns
        if cached_cols is None or len(cached_cols) == table.length:
            return tuple(
                (bytes(val) if isinstance(val, (bytearray, memoryview))
                 else (cls._normalize_binary_val(val) if isinstance(val, str) and cols_def[i][1].upper().startswith("BINARY") else val))
                for i, val in enumerate(row)
            )
        return tuple(
            (bytes(row[i]) if isinstance(row[i], (bytearray, memoryview))
             else (cls._normalize_binary_val(row[i]) if isinstance(row[i], str) and cols_def[i][1].upper().startswith("BINARY") else row[i]))
            if i in cached_cols else None
            for i in range(table.length)
        )

    @classmethod
    def _reconstruct_partial_row(
        cls,
        table: Table,
        selected_row: tuple[SafeDataType, ...],
        cached_cols: tuple[int, ...],
    ) -> tuple[SafeDataType, ...]:
        """Reconstruct full canonical row of length table.length from a column-projected query result."""
        full_row = [None] * table.length
        cols_def = table.init_columns
        for idx, col_pos in enumerate(cached_cols):
            val = selected_row[idx]
            if isinstance(val, (bytearray, memoryview)):
                val = bytes(val)
            elif isinstance(val, str) and cols_def[col_pos][1].upper().startswith("BINARY"):
                val = cls._normalize_binary_val(val)
            full_row[col_pos] = val
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
        """Clear all in-memory row storage and internal index structures (carried tables stay attached)."""
        self._cached_rows.clear()
        self._cached_rows_pos.clear()
        self._pk_index.clear()
        self._nodup_index.clear()
        self._nodup_complete.clear()
        self._col_indices.clear()
        self._shared_buffer = None
        self._preload_window.clear()
        self._attach_carried_views()

    def _attach_carried_views(self) -> None:
        """Expose each carried table as a view over its snapshot whose local overlay holds the rows since."""
        for table_id, overlay in self._carried_overlays.items():
            self._cached_rows[table_id] = SharedTableRowSequenceView(self._carried_reader, table_id)
            self._cached_rows_pos[table_id] = {}
            self._pk_index[table_id] = SharedTablePKView(self._carried_reader, table_id, local=overlay)
            self._nodup_index[table_id] = {}
            self._col_indices[table_id] = {}

    def _detach_carried(self) -> None:
        """Forget the carried snapshot; the next start reloads those tables from the database."""
        for table_id in self._carried_overlays:
            for cache in (self._cached_rows, self._cached_rows_pos, self._pk_index, self._nodup_index, self._col_indices):
                cache.pop(table_id, None)
        self._carried_overlays = {}
        self._carried_reader = None

    def _drop_uncommitted_carried(self) -> None:
        """Remove rows staged but never committed from the carried overlays (the database does not have them)."""
        for table_id, overlay in self._carried_overlays.items():
            for pk in self.queued_set.get(table_id, ()):
                overlay.pop(pk, None)

    def _carry_rebuildable(self, carried_ids: set[int]) -> bool:
        """True if the carried snapshot and overlays still mirror the database for every carried table."""
        return bool(carried_ids) and set(self._carried_overlays) == carried_ids and self._carried_reader is not None

    def _carry_reusable(self, carried_ids: set[int]) -> bool:
        """True if every carried table has a snapshot and has not outgrown it since (see CARRY_RELOAD_RATIO)."""
        if not self._carry_rebuildable(carried_ids):
            return False
        for table_id, overlay in self._carried_overlays.items():
            snapshot_rows = self._carried_reader.tables.get(table_id, {}).get("row_count", 0)
            if len(overlay) > self.CARRY_RELOAD_RATIO * max(snapshot_rows, 1):
                return False
        return True

    def _carried_rows_in_memory(self) -> dict[int, list[tuple[SafeDataType, ...]]]:
        """Every row the carried tables hold: snapshot rows (minus any the overlay replaced), then the overlay rows.

        Only committed rows are left in the overlays at this point, so this is what the database has, without
        reading it back.
        """
        rows_by_table: dict[int, list[tuple[SafeDataType, ...]]] = {}
        for table_id, overlay in self._carried_overlays.items():
            table = self.tables[table_id]
            pk_fn = self._pk_getters.get(table_id) or itemgetter(*table.primary)
            rows: list[tuple[SafeDataType, ...]] = []
            for row in SharedTableRowSequenceView(self._carried_reader, table_id):
                if overlay and self._sanitize_key(pk_fn(row)) in overlay:
                    continue
                rows.append(row)
            rows.extend(overlay.values())
            rows_by_table[table_id] = rows
        return rows_by_table

    def _tracks_committed_pks(self, table_id: int) -> bool:
        """Carried tables find committed rows through their overlay, so they skip the `_committed_pks` copy."""
        return table_id not in self._carried_overlays

    def _build_carried(self, carried_rows: dict[int, list[tuple[SafeDataType, ...]]]) -> bool:
        """Pack freshly loaded carried tables into their own long-lived shared buffer and attach their views."""
        builder = SharedTableBuilder(mode=self.hugepages)
        for table_id, rows in carried_rows.items():
            table = self.tables[table_id]
            builder.add_table(table_id, table.length, table.primary, rows)
        try:
            reader = builder.build()
        except Exception as e:
            logger.warning(f"[TECachedDB] Failed to build the carried tables' shared buffer: {e}. Using the heap.")
            reader = None
        if reader is None:
            return False
        self._carried_reader = reader
        self._carried_overlays = {table_id: {} for table_id in carried_rows}
        self._attach_carried_views()
        return True

    def start_new_db(self, db: Callable[[], Any] | type[Any], is_worker: bool = False) -> None:
        """Start or restart database connection and preload cached tables.

        Args:
            db: Database class or factory callable (e.g., MariaDB or MockDB).
            is_worker: If True, connects dedicated DB socket but retains inherited shared huge-page buffer.
        """
        if is_worker:
            # A forked worker never owns the parent's background commit
            self._pending = None
            self.async_commits = False
        if is_worker and (self._shared_buffer is not None or self._carried_reader is not None):
            if getattr(self, "db", None) is not None:
                try:
                    self.db.close()
                except Exception:
                    pass
            self.db = db()
            self.queued_view = {}
            for table_id, table in self.tables.items():
                self.queued_set[table_id] = {}
                self.queued_update[table_id] = []
                self._queued_sec_indexes[table_id] = {idx_cols: {} for idx_cols in self._table_sec_indexes.get(table_id, ())}
                self._queued_single_col_indexes[table_id] = {col_idx: {} for col_idx in self._table_single_col_indexes.get(table_id, ())}
                self._ensure_table(table_id)
                if table.no_duplicate:
                    self._committed_nodup_keys[table_id] = {}
                if getattr(table, "init_primary", ()) == ("hash",) or table.table_name in ("m_tag_code", "m_ast_hash"):
                    self._committed_pks[table_id] = set()
                if self.db is not None:
                    try:
                        self.next_id[table_id] = self.db.get_next_id(table)
                    except Exception:
                        pass
            self.update_in_mem_indexes = True
            return

        # Rows still queued from an unfinished version never reached the database
        self.wait_for_commit()
        self._drop_uncommitted_carried()
        super().start_new_db(db)
        self.clear_cache()
        self.update_in_mem_indexes = True

        carried_ids = {
            t.table_id for t in self.tables.values() if t.table_name in self.CARRIED_TABLES and self._is_cached(t)
        } if self.hugepages != "off" else set()
        reuse_carried = self._carry_reusable(carried_ids)
        # An overlay that outgrew its snapshot is repacked with it from memory instead of reloading the table
        rebuilt_rows = None
        if not reuse_carried and self._carry_rebuildable(carried_ids):
            t_rebuild = time.perf_counter()
            rebuilt_rows = self._carried_rows_in_memory()
        if not reuse_carried:
            self._detach_carried()
        # Every carried table gets a snapshot, even an empty one, so all of them can be reused next time
        if reuse_carried:
            carried_rows: dict[int, list[tuple[SafeDataType, ...]]] = {}
        elif rebuilt_rows is not None:
            carried_rows = rebuilt_rows
        else:
            carried_rows = {t_id: [] for t_id in carried_ids}
        skip_carried_load = reuse_carried or rebuilt_rows is not None

        min_active_vid = self._get_min_active_vid()

        for table in self.tables.values():
            table_id = table.table_id
            if skip_carried_load and table_id in carried_ids:
                continue
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
                    raw_rows = None
                else:
                    vid_col = self._get_vid_col_idx(table) if getattr(table, "version_scoped", False) else None
                    if vid_col is not None:
                        self._preload_window[table_id] = (vid_col, min_vid or 0)
                    if table.no_duplicate and not is_v_scoped:
                        # The query returned every row, so the index below answers every deduplication key
                        self._nodup_complete.add(table_id)

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
                        if table_id in carried_rows:
                            # Packed into the carried buffer below: no heap indexing for rows about to be replaced
                            carried_rows[table_id].append(proj_row)
                            continue
                        self._cache_row(table, proj_row)
                elif table.initial_insert:
                    for row in table.initial_insert:
                        self._cache_row(table, self._project_row(table, row))

        # Nothing to carry on an empty database: the first version keeps the heap cache and its eviction
        if carried_rows and not (any(carried_rows.values()) and self._build_carried(carried_rows)):
            for table_id, rows in carried_rows.items():
                for proj_row in rows:
                    self._cache_row(self.tables[table_id], proj_row)

        for table in self.tables.values():
            t_id = table.table_id
            if t_id in self._carried_overlays:
                continue
            if table.no_duplicate:
                self._committed_nodup_keys[t_id] = dict(self._nodup_index.get(t_id, {}))
            if getattr(table, "init_primary", ()) == ("hash",) or table.table_name in ("m_tag_code", "m_ast_hash"):
                self._committed_pks[t_id] = set(self._pk_index.get(t_id, {}).keys())

        # Build structured shared memory segment with huge pages if enabled (carried tables have their own)
        version_tables = [t for t in self.tables.values() if t.table_id not in self._carried_overlays]
        if self.hugepages != "off" and any(self._cached_rows.get(t.table_id) for t in version_tables):
            try:
                builder = SharedTableBuilder(mode=self.hugepages)
                for table in version_tables:
                    t_id = table.table_id
                    rows = self._cached_rows.get(t_id, [])
                    if rows:
                        builder.add_table(t_id, table.length, table.primary, rows)
                self._shared_buffer = builder.build()
                if self._shared_buffer:
                    # Replace heap cache structures with zero-copy shared memory views
                    # to prevent duplicate memory usage across heap and mmap while satisfying sequence/dict lookups.
                    for t_id in self._shared_buffer.tables:
                        self._cached_rows[t_id] = SharedTableRowSequenceView(self._shared_buffer, t_id)
                        self._cached_rows_pos[t_id] = {}
                        self._pk_index[t_id] = SharedTablePKView(self._shared_buffer, t_id)
                        self._col_indices[t_id] = {}
                        self._committed_pks[t_id] = set()
                        self._committed_nodup_keys[t_id] = {}
                    from core.globalstuff import reclaim_system_memory
                    reclaim_system_memory()
                    logger.info(
                        f"[TECachedDB] Backed {len(self._shared_buffer.tables)} tables "
                        f"({self._shared_buffer.total_size // (1024*1024)}MB) with structured shared memory "
                        f"(page size: {self._shared_buffer.page_size // 1024}KB, mode: {self.hugepages}) and activated zero-copy views."
                    )
            except Exception as e:
                logger.warning(f"[TECachedDB] Failed to build shared huge page buffer: {e}. Falling back to standard heap.")
                self._shared_buffer = None
        if self._carried_overlays:
            carried = ", ".join(
                f"{self.tables[t_id].table_name} {self._carried_reader.tables[t_id]['row_count']:,}+{len(overlay):,}"
                for t_id, overlay in self._carried_overlays.items()
            )
            if reuse_carried:
                how = "Reused"
            elif rebuilt_rows is not None:
                how = f"Rebuilt from memory in {time.perf_counter() - t_rebuild:.1f}s"
            else:
                how = "Loaded"
            logger.info(f"[TECachedDB] {how} carried tables (snapshot+since): {carried}")

    def _cache_row(self, table: Table, proj_row: tuple[SafeDataType, ...]) -> None:
        """Append a preloaded row to the heap cache and index it."""
        table_id = table.table_id
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
            if self._shared_buffer is not None:
                shared_row = self._shared_buffer.get_row_by_pk(table_id, pk)
                if shared_row is not None and self._match_columns(table, shared_row, columns):
                    return shared_row
            if self._is_version_scoped(table) and self.db is not None and not self._miss_is_final(table_id, columns, pk):
                return self._db_read((table_id,), self.db.select, table, columns)
            return None

        # 2. Deduplication Key Fast-Path (no_duplicate=True)
        if table.no_duplicate and len(columns) > 1 and all(c is not None for c in columns[1:]):
            assigned_id = self._committed_nodup_id(table_id, columns)
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
                    return self._db_read((table_id,), self.db.select, table, columns)
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

        if self._shared_buffer is not None:
            shared_row = self._shared_buffer.scan_matching(table_id, columns)
            if shared_row is not None:
                return shared_row

        if self._is_version_scoped(table) and self.db is not None:
            return self._db_read((table_id,), self.db.select, table, columns)

        return None

    def _committed_nodup_id(self, table_id: int, columns: tuple[SafeDataType, ...]) -> int | None:
        """The id a no-duplicate key already has in the database, or None when the key is new.

        The in-memory indices answer this while they hold every row of the table. Once they were purged
        (`clear_cache()`, `commit(update_in_mem_indexes=False)`), a key they miss is resolved with one
        targeted read and memoized in `_committed_nodup_keys`, so staging it cannot mint a second id for
        content the database already stores (duplicate m_file_name rows, m_file_blob unique-key violations).
        """
        key = self._sanitize_key(columns[1:])
        assigned_id = self._nodup_index.get(table_id, {}).get(key)
        if assigned_id is None:
            assigned_id = self._committed_nodup_keys.get(table_id, {}).get(key)
        if assigned_id is not None:
            return assigned_id
        if table_id in self._nodup_complete or self.db is None:
            return None
        table = self.tables[table_id]
        filter_cols = (None, *columns[1:]) + (None,) * (table.length - len(columns))
        row = self._db_read((table_id,), self.db.select, table, filter_cols)
        if not row:
            return None
        self._committed_nodup_keys.setdefault(table_id, {})[key] = row[0]
        return row[0]

    def _miss_is_final(self, table_id: int, columns: tuple[SafeDataType, ...], pk: Any) -> bool:
        """Whether a primary-key miss needs no database read: the key's version is inside the preloaded window,
        whose rows are all cached or staged, and it is not a row committed and evicted from the heap since.

        Saves one query per lookup of a version not written yet (e.g. the current version's checkpoint row of
        every unchanged file).
        """
        window = self._preload_window.get(table_id)
        if window is None or not self.update_in_mem_indexes:
            return False
        version = columns[window[0]]
        if type(version) is not int or version < window[1]:
            return False
        return pk not in self._committed_pks.get(table_id, ())

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

        if not self.update_in_mem_indexes:
            # Not indexed: the cache no longer holds every row of the window
            self._preload_window.pop(table_id, None)
            if table.no_duplicate and len(columns) > 1 and all(c is not None for c in columns[1:]):
                # Staging is not indexed, but a key the database already has must still resolve to its row
                existing_id = self._committed_nodup_id(table_id, columns)
                if existing_id is not None:
                    return (existing_id, *columns[1:])
            if table.primary and not table.no_duplicate and columns[0] is not None:
                pk_fn = self._pk_getters.get(table_id)
                pk = self._sanitize_key(pk_fn(columns) if pk_fn is not None else itemgetter(*table.primary)(columns))
                existing_row = self._pk_index.get(table_id, {}).get(pk)
                if existing_row is not None:
                    proj_row = self._project_row(table, columns)
                    if existing_row == proj_row:
                        return columns
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

            committed_id = self._committed_nodup_id(table_id, columns)
            if committed_id is not None:
                # Already in the database although the index misses it (the cache was purged): cache that row
                # instead of minting a second id for the same key
                row = (committed_id, *columns[1:])
                self._cache_row(table, self._project_row(table, row))
                return row

            if columns[0] is not None:
                assigned_id = columns[0]
                if isinstance(assigned_id, int) and assigned_id >= self.next_id[table_id]:
                    self.next_id[table_id] = assigned_id + 1
            else:
                assigned_id = self.next_id[table_id]
                self.next_id[table_id] += 1
            self.queued_set[table_id][key] = assigned_id
            row = (assigned_id, *columns[1:])
            proj_row = self._project_row(table, row)
            pos = len(self._cached_rows[table_id])
            self._cached_rows[table_id].append(proj_row)
            if table.primary:
                pk_fn = self._pk_getters.get(table_id)
                pk = self._sanitize_key(pk_fn(proj_row) if pk_fn is not None else itemgetter(*table.primary)(proj_row))
                self._cached_rows_pos[table_id][pk] = pos
            self._index_row(table, proj_row)
            self._index_staged_row(table_id, row)
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
            self._index_staged_row(table_id, row)
            return row

        pk_fn = self._pk_getters.get(table_id)
        pk = self._sanitize_key(pk_fn(columns) if pk_fn is not None else itemgetter(*table.primary)(columns))

        if getattr(table, "idempotent_pk", False) or getattr(table, "init_primary", ()) == ("hash",) or table.table_name in ("m_tag_code", "m_ast_hash"):
            if pk in self.queued_set[table_id]:
                return self.queued_set[table_id][pk]
            existing_row = self._pk_index[table_id].get(pk)
            if existing_row is not None:
                return existing_row
            if pk in self._committed_pks.get(table_id, set()):
                return columns
        else:
            if pk in self.queued_set[table_id]:
                raise ValueError(f"Duplicate primary key '{pk}' in table '{table.table_name}'")
            existing_row = self._pk_index[table_id].get(pk)
            if existing_row is not None:
                raise ValueError(f"Duplicate primary key '{pk}' in table '{table.table_name}'")

        proj_row = self._project_row(table, columns)
        pos = len(self._cached_rows[table_id])
        self._cached_rows[table_id].append(proj_row)
        self._cached_rows_pos[table_id][pk] = pos

        self.queued_set[table_id][pk] = columns
        self._index_row(table, proj_row)
        self._index_staged_row(table_id, columns)
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

    def _queued_carried_rows(self, table_ids: Sequence[int]) -> dict[int, list[tuple[Any, tuple[SafeDataType, ...]]]]:
        """Rows of carried tables about to be committed, as (primary key, row) pairs."""
        return {
            t_id: list(self.queued_set[t_id].items())
            for t_id in table_ids
            if t_id in self._carried_overlays and self.queued_set.get(t_id)
        }

    def _record_carried(self, committed: dict[int, list[tuple[Any, tuple[SafeDataType, ...]]]]) -> None:
        """Add rows now in the database to the carried overlays, whether or not the heap cache saw them."""
        for t_id, items in committed.items():
            overlay = self._carried_overlays.get(t_id)
            if overlay is None:
                continue
            table = self.tables[t_id]
            for pk, row in items:
                overlay[self._sanitize_key(pk)] = self._project_row(table, row)

    def _evict_committed_rows(self, table_id: int) -> None:
        """Drop a write-heavy table's committed rows from the heap, keeping their keys for deduplication.

        Rows preloaded into a shared buffer stay reachable through its views, so only rows staged since then
        move to `_committed_pks` (copying every shared key into a set took seconds per commit). Carried tables
        keep their rows: they are the snapshot extension the next version reuses. A table without cache
        structures (e.g. after `clear_cache()`) is left alone: recreating only some of them made `_ensure_table()`
        skip the rest, and the next `set()` failed on `_col_indices`.
        """
        if table_id in self._carried_overlays or table_id not in self._cached_rows:
            return
        committed = self._committed_pks.setdefault(table_id, set())
        pk_index = self._pk_index.get(table_id)
        if isinstance(pk_index, SharedTablePKView):
            committed.update(pk_index.drain_local())
        else:
            if pk_index:
                committed.update(pk_index.keys())
            self._pk_index[table_id] = {}
        rows = self._cached_rows.get(table_id)
        if isinstance(rows, SharedTableRowSequenceView):
            rows.drop_appended()
        else:
            self._cached_rows[table_id] = []
        self._cached_rows_pos[table_id] = {}

    def commit(self, table_id: int, update_in_mem_indexes: bool = True) -> None:
        """Flush staged insert and update operations for target table to database.

        Args:
            table_id: Target table identifier integer.
            update_in_mem_indexes: If False, clears target table from in-memory cache structures.
        """
        self.wait_for_commit()
        carried = self._queued_carried_rows((table_id,))
        super().commit(table_id, update_in_mem_indexes=update_in_mem_indexes)
        self._record_carried(carried)
        if not update_in_mem_indexes and table_id in self.tables:
            self._preload_window.pop(table_id, None)
            self._cached_rows.pop(table_id, None)
            self._cached_rows_pos.pop(table_id, None)
            self._pk_index.pop(table_id, None)
            self._nodup_index.pop(table_id, None)
            self._nodup_complete.discard(table_id)
            self._col_indices.pop(table_id, None)
            self._attach_carried_views()
        elif update_in_mem_indexes and table_id in self.tables:
            # Write-heavy deduplicated/bridge tables are evicted from heap on commit to bound RAM
            if self.tables[table_id].table_name in self.WRITE_HEAVY_TABLES:
                self._evict_committed_rows(table_id)

    def commit_all(self, max_workers: int | None = None, update_in_mem_indexes: bool = True) -> None:
        """Flush staged operations across ALL tables and optionally clear in-memory caches.

        With `async_commits` the rows are written by a background thread: the cache upkeep below (carried overlays,
        write-heavy eviction) runs meanwhile, as if the write had landed, and any database read of those tables
        waits for it (`_db_read()`).

        Args:
            max_workers: Maximum concurrent worker threads.
            update_in_mem_indexes: If False, immediately evacuates all in-memory caches and indices.
        """
        self.wait_for_commit()
        t_start = time.perf_counter()
        carried = self._queued_carried_rows(tuple(self._carried_overlays))
        batch = self._take_staged(tuple(self.tables))
        background = batch is not None and self._background_commit_allowed(update_in_mem_indexes)
        if background:
            self._submit(batch, max_workers)
        elif batch is not None:
            # On failure the tables not committed are back in staging (see _restore_batch) and the error propagates
            self._write_batch(batch, max_workers)
        t_written = time.perf_counter()
        self._record_carried(carried)
        if not update_in_mem_indexes:
            self.clear_cache()
        else:
            # Bound RAM by purging write-heavy tables from heap cache while retaining compact deduplication keys
            for t_id, tbl in self.tables.items():
                if tbl.table_name in self.WRITE_HEAVY_TABLES:
                    self._evict_committed_rows(t_id)
        # Committed rows are freed by reference counting: return the freed C heap pages without a full GC pass
        from core.globalstuff import trim_heap
        trim_heap()
        elapsed = time.perf_counter() - t_start
        if batch is not None and not background and elapsed >= 2.0:
            logger.info(
                f"[TECachedDB] commit_all: {batch.rows:,} staged rows written in {t_written - t_start:.1f}s, "
                f"cache upkeep {elapsed - (t_written - t_start):.1f}s; slowest tables: {batch.slowest_tables()}"
            )

    def _on_background_commit_done(self, batch: CommitBatch) -> None:
        """Log a background commit that took a while, with how much of it the caller still had to wait for."""
        if batch.write_s >= 2.0:
            logger.info(
                f"[TECachedDB] background commit: {batch.rows:,} staged rows written in {batch.write_s:.1f}s "
                f"({max(0.0, batch.write_s - batch.wait_s):.1f}s overlapped, {batch.wait_s:.1f}s waited); "
                f"slowest tables: {batch.slowest_tables()}"
            )

    def _restore_batch(self, batch: CommitBatch, failed_ids: Any = None) -> None:
        """Put uncommitted tables back into staging, re-caching rows the commit had already evicted from the heap."""
        super()._restore_batch(batch, failed_ids)
        if any(t_id in self._carried_overlays for t_id in batch.restored):
            # The database may hold part of these rows: the carried snapshot can no longer be trusted
            self._detach_carried()
        if not self.update_in_mem_indexes:
            return
        for table, ins_payload, _upd in batch.tables_data:
            t_id = table.table_id
            if (
                t_id not in batch.restored
                or not ins_payload
                or not self._is_cached(table)
                or table.table_name not in self.WRITE_HEAVY_TABLES
            ):
                continue
            self._ensure_table(t_id)
            pk_fn = self._pk_getters.get(t_id)
            for row in ins_payload:
                proj_row = self._project_row(table, row)
                if table.primary and all(proj_row[i] is not None for i in table.primary):
                    pk = self._sanitize_key(pk_fn(proj_row) if pk_fn is not None else itemgetter(*table.primary)(proj_row))
                    if self._pk_index[t_id].get(pk) is not None:
                        continue
                    self._committed_pks.get(t_id, set()).discard(pk)
                self._cache_row(table, proj_row)

    def discard_carried(self) -> None:
        """Forget the carried tables' snapshot and overlays (rows were deleted outside the TableEngine,
        e.g. an interrupted version rolled back); the next start reloads them from the database."""
        self._detach_carried()

    def close(self) -> None:
        """Safely clean up in-memory cache structures and close DB connection (carried tables are kept)."""
        self.wait_for_commit()
        self._drop_uncommitted_carried()
        self.clear_cache()
        self.update_in_mem_indexes = True
        super().close()
