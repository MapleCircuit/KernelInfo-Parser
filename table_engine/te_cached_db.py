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
   A cached table measured above `cache_max_bytes` (`parser.cache_max_table_mb`) is left
   out of the cache entirely and served straight from the database (`TEDirectDB`).

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
from table_engine.shared_buffer import (
    estimate_packed_bytes,
    SharedTableBuilder,
    SharedTablePKView,
    SharedTableRowSequenceView,
    SharedTableSegments,
)
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
    # Default budget (MB) of preloaded rows held on the heap before they are packed into a shared-memory
    # segment and dropped; overridden by `parser.preload_flush_mb`. Preloading every cached table first and
    # packing afterwards needed the rows twice over (heap plus segment) at its peak.
    PRELOAD_FLUSH_MB = 512
    # Default ceiling (MB) on the size of one cached table; a table the database reports above it is not
    # preloaded and not cached at all, however `te_cached` configures it. 0 disables the ceiling.
    # Overridden by `parser.cache_max_table_mb`.
    CACHE_MAX_TABLE_MB = 0
    # Tables whose committed rows leave the heap at each commit (their keys stay in _committed_pks)
    WRITE_HEAVY_TABLES = frozenset({
        "m_ast_hash", "m_tag_code", "m_blob_symbol_def", "m_ast_container", "m_bridge_tag", "m_bridge_checkpoint", "m_blob_transition",
    })

    def __init__(
        self,
        hugepages: str | None = None,
        preload_flush_mb: int | None = None,
        cache_max_table_mb: int | None = None,
    ) -> None:
        """Initialize Table Engine state and in-memory cache structures.

        Args:
            hugepages: Huge page allocation mode, or None for `parser.hugepages`.
            preload_flush_mb: MB of preloaded rows held on the heap before they are packed into a
                shared-memory segment, or None for `parser.preload_flush_mb`.
            cache_max_table_mb: Ceiling (MB) on the size of one cached table, 0 for no ceiling, or
                None for `parser.cache_max_table_mb`.
        """
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
        # Cached tables the size ceiling excluded at the last start: table_id -> measured bytes
        self._oversized: dict[int, int] = {}
        try:
            from core.config import get_parser_config
            parser_cfg = get_parser_config()
        except Exception:
            parser_cfg = {}
        if hugepages is None:
            hugepages = parser_cfg.get("hugepages", "auto")
        self.hugepages: str = str(hugepages).lower().strip()
        if preload_flush_mb is None:
            preload_flush_mb = parser_cfg.get("preload_flush_mb", self.PRELOAD_FLUSH_MB)
        try:
            flush_mb = max(1, int(preload_flush_mb))
        except (TypeError, ValueError):
            flush_mb = self.PRELOAD_FLUSH_MB
        # Preloaded rows are packed into a shared-memory segment once a batch reaches this many bytes
        self.preload_flush_bytes: int = flush_mb * 1024 * 1024
        if cache_max_table_mb is None:
            cache_max_table_mb = parser_cfg.get("cache_max_table_mb", self.CACHE_MAX_TABLE_MB)
        try:
            max_table_mb = max(0, int(cache_max_table_mb))
        except (TypeError, ValueError):
            max_table_mb = self.CACHE_MAX_TABLE_MB
        # A cached table measured above this many bytes is not cached at all (0: no ceiling)
        self.cache_max_bytes: int = max_table_mb * 1024 * 1024

    def _is_cached(self, table: Table) -> bool:
        """Check if target table is configured for in-memory caching and fits the cache size ceiling.

        A table the ceiling excluded (`_apply_cache_ceiling()`) reports as uncached, so every one of its
        reads and writes takes the `TEDirectDB` path to the database instead of the in-memory caches.
        """
        return bool(getattr(table, "te_cached", False)) and table.table_id not in self._oversized

    def _apply_cache_ceiling(self) -> None:
        """Measure every cached table and exclude the ones above `cache_max_bytes` from caching.

        Each table is measured as it would be cached: only its cached columns, and for a version-scoped
        table only its working window, which is what `_load_preload_rows()` goes on to read.
        Run before preloading on each start, so a table that grew past the ceiling since the last version
        is dropped from the cache instead of preloaded, and one that is back under it is cached again.
        A table whose size the driver cannot report (no `table_size()`, or it raised) is cached as configured.
        """
        self._oversized = {}
        if not self.cache_max_bytes or self.db is None or not hasattr(self.db, "table_size"):
            return
        min_active_vid = self._get_min_active_vid()
        for table in self.tables.values():
            if not getattr(table, "te_cached", False):
                continue
            min_vid = min_active_vid if (self._is_version_scoped(table) and min_active_vid > 0) else None
            try:
                size = self.db.table_size(table, getattr(table, "cached_columns", None), min_vid)
            except Exception as e:
                logger.warning(f"[TECachedDB] Could not measure {table.table_name}: {e}. Caching it as configured.")
                continue
            if size > self.cache_max_bytes:
                self._oversized[table.table_id] = size
        if self._oversized:
            excluded = ", ".join(
                f"{self.tables[t_id].table_name} {size // (1024 * 1024):,}MB"
                for t_id, size in sorted(self._oversized.items(), key=lambda kv: -kv[1])
            )
            logger.info(
                f"[TECachedDB] {len(self._oversized)} cached table(s) above the "
                f"{self.cache_max_bytes // (1024 * 1024):,}MB ceiling are read straight from the database: {excluded}"
            )

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
        if table.no_duplicate:
            self._index_nodup_row(table, row)

        # 3. Secondary Column Indices (Skip columns covered by Primary Key to eliminate redundant memory allocations)
        for col_idx in range(table.length):
            if col_idx in pk_set:
                continue
            if cached_cols is None or col_idx in cached_cols:
                val = row[col_idx]
                if val is not None:
                    val = bytes(val) if isinstance(val, (bytearray, memoryview)) else val
                    self._col_indices[table_id].setdefault(col_idx, {}).setdefault(val, []).append(row)

    def _index_nodup_row(self, table: Table, row: tuple[SafeDataType, ...]) -> None:
        """Index a row's deduplication key, the one index a shared-memory backed table keeps on the heap.

        Its primary-key and column lookups are answered by its segment, but a deduplication key is the
        whole row minus its id, which no index inside the segment is keyed by.
        """
        if len(row) > 1 and all(c is not None for c in row[1:]):
            nodup_key = self._sanitize_key(row[1:])
            self._nodup_index[table.table_id][nodup_key] = int(row[0]) if isinstance(row[0], int) else row[0]

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
            reader = self._carried_reader.reader_for(table_id)
            self._cached_rows[table_id] = SharedTableRowSequenceView(reader, table_id)
            self._cached_rows_pos[table_id] = {}
            self._pk_index[table_id] = SharedTablePKView(reader, table_id, local=overlay)
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
            for row in SharedTableRowSequenceView(self._carried_reader.reader_for(table_id), table_id):
                if overlay and self._sanitize_key(pk_fn(row)) in overlay:
                    continue
                rows.append(row)
            rows.extend(overlay.values())
            rows_by_table[table_id] = rows
        return rows_by_table

    def _tracks_committed_pks(self, table_id: int) -> bool:
        """Carried tables find committed rows through their overlay, so they skip the `_committed_pks` copy."""
        return table_id not in self._carried_overlays

    def _build_carried(self, carried_rows: dict[int, list[tuple[SafeDataType, ...]]]) -> None:
        """Pack the freshly loaded carried tables into their own long-lived buffer and attach their views.

        Each table becomes its own segment and its rows are released as soon as that segment holds them, so
        the carried tables are never all on the heap while a packed copy of all of them is built. The rows of
        a table that could not be packed stay in `carried_rows` for the caller to cache on the heap; its
        snapshot is then missing, which makes the next start reload all of them (`_carry_rebuildable()`).
        """
        from core.globalstuff import reclaim_system_memory
        segments = SharedTableSegments()
        overlays: dict[int, dict[Any, tuple[SafeDataType, ...]]] = {}
        for table_id in list(carried_rows):
            table = self.tables[table_id]
            builder = SharedTableBuilder(mode=self.hugepages)
            builder.add_table(table_id, table.length, table.primary, carried_rows[table_id])
            try:
                reader = builder.build()
            except Exception as e:
                logger.warning(
                    f"[TECachedDB] Failed to build the carried table {table.table_name}'s shared buffer: {e}. Using the heap."
                )
                break
            if reader is None:
                break
            segments.add_segment(reader)
            overlays[table_id] = {}
            # Packed: release the heap rows before the next carried table is packed
            del carried_rows[table_id]
            reclaim_system_memory()
        if not segments:
            return
        self._carried_reader = segments
        self._carried_overlays = overlays
        self._attach_carried_views()

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
        super().start_new_db(db, is_worker=is_worker)
        self.clear_cache()
        self.update_in_mem_indexes = True
        # Decided before anything is preloaded or carried: a table the ceiling excludes is not cached at all
        self._apply_cache_ceiling()

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
        # Tables read but not packed yet, and the shared-memory bytes they are estimated to take. A batch is
        # packed and dropped from the heap once it reaches `preload_flush_bytes`, before the next table is read.
        segments = SharedTableSegments()
        pending: dict[int, list[tuple[SafeDataType, ...]]] = {}
        pending_bytes = 0
        pack_shared = self.hugepages != "off"

        for table in self.tables.values():
            table_id = table.table_id
            if skip_carried_load and table_id in carried_ids:
                continue
            self._ensure_table(table_id)
            if not (self._is_cached(table) and self.db is not None):
                continue

            rows = self._load_preload_rows(table, min_active_vid)
            if rows and table_id in carried_rows:
                # Packed into the carried buffer below: no heap indexing for rows about to be replaced
                carried_rows[table_id] = rows
                continue
            if not rows and table.initial_insert:
                # The table holds no rows (or could not be read): its seed rows are the baseline
                rows = [self._project_row(table, row) for row in table.initial_insert]
            if not rows:
                continue

            if not pack_shared:
                for proj_row in rows:
                    self._cache_row(table, proj_row)
                continue

            # These rows are about to live in a segment: only the deduplication index survives packing,
            # so the primary-key, position and column indices are never built for them.
            if table.no_duplicate:
                for proj_row in rows:
                    self._index_nodup_row(table, proj_row)
            pending[table_id] = rows
            pending_bytes += estimate_packed_bytes(table.length, rows)
            del rows
            if pending_bytes >= self.preload_flush_bytes:
                self._flush_preload_batch(pending, segments)
                pending_bytes = 0

        # Nothing to carry on an empty database: the first version keeps the heap cache and its eviction
        if any(carried_rows.values()):
            self._build_carried(carried_rows)
        # Whatever _build_carried could not pack (and the rows of an empty database) stays on the heap
        for table_id, rows in carried_rows.items():
            for proj_row in rows:
                self._cache_row(self.tables[table_id], proj_row)
        carried_rows = {}

        self._flush_preload_batch(pending, segments)
        self._shared_buffer = segments if segments else None

        for table in self.tables.values():
            t_id = table.table_id
            if t_id in self._carried_overlays or segments.contains_table(t_id):
                # A packed or carried table resolves committed keys through its segment and overlay instead
                continue
            if table.no_duplicate:
                self._committed_nodup_keys[t_id] = dict(self._nodup_index.get(t_id, {}))
            if getattr(table, "init_primary", ()) == ("hash",) or table.table_name in ("m_tag_code", "m_ast_hash"):
                self._committed_pks[t_id] = set(self._pk_index.get(t_id, {}).keys())

        if segments:
            logger.info(
                f"[TECachedDB] Backed {len(segments.tables)} tables ({segments.total_size // (1024*1024)}MB) with "
                f"structured shared memory in {len(segments)} segment(s) "
                f"(page size: {segments.page_size // 1024}KB, mode: {self.hugepages}) and activated zero-copy views."
            )
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

    def _load_preload_rows(self, table: Table, min_active_vid: int) -> list[tuple[SafeDataType, ...]]:
        """Read a cached table's preload window from the database and project its rows.

        The rows are taken over one at a time as the projected rows are built, so a table is never held
        twice over (the driver's own list is left untouched).
        Returns an empty list when the table has no rows in the window or could not be read.
        """
        table_id = table.table_id
        is_v_scoped = self._is_version_scoped(table)
        cached_cols = getattr(table, "cached_columns", None)
        min_vid = min_active_vid if (is_v_scoped and min_active_vid > 0) else None
        has_preload = hasattr(self.db, "select_preload")

        try:
            if has_preload:
                raw_rows = self.db.select_preload(table, cached_columns=cached_cols, min_vid=min_vid)
            else:
                joins: JoinsType = (((table.table_id, 0),),)
                raw_rows = self.db.view_select_multiple(self.tables, joins, (None,) * table.length)
        except Exception:
            return []

        vid_col = self._get_vid_col_idx(table) if getattr(table, "version_scoped", False) else None
        if vid_col is not None:
            self._preload_window[table_id] = (vid_col, min_vid or 0)
        if table.no_duplicate and not is_v_scoped:
            # The query returned every row, so the index built below answers every deduplication key
            self._nodup_complete.add(table_id)
        if not raw_rows:
            return []

        is_partial_query = cached_cols is not None and len(cached_cols) < table.length and has_preload
        # Fallback version check if the driver did not filter
        fallback_vid_col = (
            self._get_vid_col_idx(table) if (is_v_scoped and min_active_vid > 0 and not has_preload) else None
        )
        # Take the rows over one at a time, in order, so they are released as the projected ones are built
        # instead of a table being held twice over. The drained list is this engine's own copy of the
        # references (cheap: one pointer per row), never the list the driver returned, which it may still own.
        pending_raw = list(raw_rows)
        del raw_rows
        pending_raw.reverse()
        rows: list[tuple[SafeDataType, ...]] = []
        while pending_raw:
            row = pending_raw.pop()
            if is_partial_query:
                rows.append(self._reconstruct_partial_row(table, row, cached_cols))
                continue
            if fallback_vid_col is not None and row[fallback_vid_col] is not None and row[fallback_vid_col] < min_active_vid:
                continue
            rows.append(self._project_row(table, row))
        return rows

    def _attach_shared_views(self, reader: Any, table_id: int) -> None:
        """Expose a packed table through zero-copy views over its segment, dropping its heap structures.

        The segment answers primary-key probes and scans, so the heap keeps no rows, positions or column
        index for it; rows staged afterwards land in the views' local overlay.
        """
        self._cached_rows[table_id] = SharedTableRowSequenceView(reader, table_id)
        self._cached_rows_pos[table_id] = {}
        self._pk_index[table_id] = SharedTablePKView(reader, table_id)
        self._col_indices[table_id] = {}
        self._committed_pks[table_id] = set()
        self._committed_nodup_keys[table_id] = {}

    def _flush_preload_batch(self, pending: dict[int, list[tuple[SafeDataType, ...]]], segments: SharedTableSegments) -> None:
        """Pack the preloaded tables held on the heap into their own shared-memory segment and free them.

        Called while preloading, as soon as a batch reaches `preload_flush_bytes`, and once more for the
        remainder: the heap then holds one batch at a time instead of every cached table at once. A segment
        that cannot be allocated leaves its batch in the heap cache, fully indexed, as before.
        """
        if not pending:
            return
        try:
            builder = SharedTableBuilder(mode=self.hugepages)
            for t_id, rows in pending.items():
                table = self.tables[t_id]
                builder.add_table(t_id, table.length, table.primary, rows)
            reader = builder.build()
        except Exception as e:
            logger.warning(f"[TECachedDB] Failed to pack a preload batch into shared memory: {e}. Falling back to standard heap.")
            reader = None
        if reader is None:
            for t_id, rows in pending.items():
                table = self.tables[t_id]
                for proj_row in rows:
                    self._cache_row(table, proj_row)
            pending.clear()
            return
        segments.add_segment(reader)
        for t_id in pending:
            self._attach_shared_views(reader, t_id)
        # The rows now live in the segment: release the heap copy before the next tables are read
        pending.clear()
        from core.globalstuff import reclaim_system_memory
        reclaim_system_memory()

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
