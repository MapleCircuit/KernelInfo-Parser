# TableEngine (TE) API & Architectural Contract Specification

Stateful caching, sequence coordination, relational view decomposition, and batching layer between `TableHandling.py` (ChangeSets) and `DBHandling.py` (SQL/Storage driver).

---

## 1. Core Data Types & Schemas

- **`SafeDataType`**: `int | str | bytes | None` &mdash; Primitive scalar values (Enums/IntEnums are converted to native `int`/`str`).
- **`PointerType`**: `tuple[int, int]` &mdash; `(table_id, col_idx)` pointing to a table's column.
- **`JoinType`**: `tuple[PointerType, PointerType, int] | tuple[PointerType]` &mdash; `((from_t, from_c), (to_t, to_c), repeat_count)` or single-table root `((t_id, c_idx),)`.
- **`JoinsType`**: `tuple[JoinType, ...]` &mdash; Immutable relational join graph.
- **`Table` Schema Contract**:
  - `table_id: int` &mdash; Table index in `gp.Table_Array`.
  - `table_name: str` &mdash; SQL table name.
  - `length: int` &mdash; Total column count.
  - `primary: tuple[int, ...]` &mdash; 0-indexed column indices forming Primary Key (e.g., `(0,)` or `(0, 1)`).
  - `no_duplicate: bool` &mdash; If `True`, deduplicates rows via in-memory key `columns[1:]`.
  - `initial_insert: tuple[...] | None` &mdash; Initial seed rows.
  - `te_cached: bool | tuple[str | int, ...] | dict[str, Any]` &mdash; If configured (`True`, tuple of column names/indices, or dict), enables in-memory preloading and multi-indexing in `TECachedDB`.
  - `cached_columns: tuple[int, ...]` &mdash; Canonical 0-indexed column indices retained in memory (defaults to all columns if `te_cached=True`, or empty if `False`).
  - `version_scoped: bool` &mdash; If `True`, enables working-window preloading (`vid >= Old_VID`) and historical version pruning in `TECachedDB`.
  - `blob_scoped: bool` &mdash; If `True`, marks table as scoped by content-addressed file blob (`blob_id`) for Git-native preloading.
  - `idempotent_pk: bool` &mdash; If `True`, enables idempotent insert deduplication via `INSERT IGNORE INTO` and ignores duplicate key errno 1062.
  - `unique_keys: tuple[tuple[str, ...], ...] | None` &mdash; Multi-column unique key definitions: `(("col_a", "col_b"), ...)`.
  - `indexes: tuple[tuple[str, tuple[str, ...]], ...] | None` &mdash; Secondary table indexes: `(("idx_name", ("col_a", "col_b")), ...)`.
  - `hashing_table: bool | str | int | Table` &mdash; If set (e.g. `"m_ast_hash"` or `True`), enables automatic structural hash deduplication and acceleration for views rooted at this table.
  - `init_columns: tuple` &mdash; Raw column definition tuples: `(("col_name", "DATA_TYPE", "CONSTRAINTS"), ...)`.
  - `init_primary: tuple[str, ...]` &mdash; Raw column names forming Primary Key: `("fnid",)`.
  - `init_foreign: tuple | None` &mdash; Raw Foreign Key constraints: `(("local_col", "foreign_table", "foreign_col"), ...)`.
  - `has_auto_increment: bool` &mdash; `True` if any column contains `AUTO_INCREMENT`, enabling monotonic sequence assignment and emptiness guard optimizations.

---

## 2. Internal State Architecture

### 2.1. Base TableEngine State (`TEDirectDB`)

| Attribute | Type | Purpose & Structure |
| :--- | :--- | :--- |
| `tables` | `dict[int, Table]` | `table_id -> Table` schema registry. |
| `queued_set` | `dict[int, dict[Any, Any]]` | Staged inserts per table:<br>• `no_duplicate=False`: `{pk_or_assigned_id: (assigned_id, col1, col2, ...)}`<br>• `no_duplicate=True`: `{(col1, col2, ...): assigned_id}` |
| `queued_update` | `dict[int, list[tuple]]` | Staged updates per table: `[row_tuple, ...]`. |
| `queued_view` | `dict[JoinsType, dict[tuple, int]]` | Cache for views: `{joins: {non_none_column_tuple: assigned_view_id}}`. |
| `next_id` | `dict[int, int]` | Monotonic auto-increment counter: `table_id -> current_int_id`. |
| `_table_sec_indexes` | `dict[int, tuple[tuple[int, ...], ...]]` | Precompiled composite secondary index column positions for registered tables. |
| `_table_unique_keys` | `dict[int, tuple[tuple[int, ...], ...]]` | Precompiled multi-column or single-column unique key column positions for registered tables. |
| `_table_single_col_indexes` | `dict[int, tuple[int, ...]]` | Precompiled single-column secondary index column positions for registered tables. |
| `_queued_sec_indexes` | `dict[int, dict[tuple, dict[tuple, list[tuple]]]]` | Staging composite secondary index lookup: `table_id -> {col_positions: {composite_key: [row, ...]}}`. |
| `_queued_single_col_indexes` | `dict[int, dict[int, dict[SafeDataType, list[tuple]]]]` | Staging single-column index lookup: `table_id -> {col_idx: {val: [row, ...]}}`. |
| `_committed_unique_keys` | `dict[int, dict[tuple, set[tuple]]]` | Historical committed unique key set per table and key definition: `table_id -> {col_positions: {u_key, ...}}`. |
| `db` | `Any \| None` | Active database driver handle (or `None`). |
| `async_commits` | `bool` | When `True` (set by the pipeline for the duration of a parsing stage), `commit_all()` hands its rows to a background writer thread (see §4.4). |
| `_pending` | `CommitBatch \| None` | The background commit still being written (`pending_commit_rows` reports its row count). |
| `staged_rows` (property) | `int` | Rows staged for the next commit (inserts and updates of all tables); the pipeline starts a background commit from 100k while it waits for its workers. |
| `commit_stats` | `dict[str, float]` | Cumulative `commits`, `background`, `rows`, `write_s` (seconds the writes took) and `blocked_s` (seconds the caller waited: a whole synchronous write, or the join of a background one). Stages report their difference. |
| `db_reads`, `db_read_s` | `int`, `float` | Database reads issued by the engine (`get` / `view_get` / `view_get_multiple` fallbacks) and their seconds. |

### 2.2. Extended In-Memory Caching State (`TECachedDB`)

| Attribute | Type | Purpose & Structure |
| :--- | :--- | :--- |
| `_cached_rows` | `dict[int, list[tuple] | SharedTableRowSequenceView]` | In-memory row storage for `te_cached` tables: `table_id -> [projected_row_tuple, ...]`, backed by zero-copy `SharedTableRowSequenceView` when huge-page shared memory is active. |
| `_pk_index` | `dict[int, dict[Any, tuple] | SharedTablePKView]` | O(1) exact row lookup: `table_id -> {primary_key_val: row_tuple}`, backed by zero-copy `SharedTablePKView` when huge-page shared memory is active. |
| `_nodup_index` | `dict[int, dict[tuple, int]]` | O(1) deduplication lookup: `table_id -> {columns[1:]: assigned_id}`. |
| `_nodup_complete` | `set[int]` | `no_duplicate` tables whose `_nodup_index` holds every row of the database (preloaded in full and indexed since): a key it misses is new and needs no lookup. A table leaves the set when its cache is purged. |
| `_col_indices` | `dict[int, dict[int, dict[SafeDataType, list[tuple]]]]` | Selective inverted column index: `table_id -> {col_idx: {column_val: [matching_row, ...]}}` (exempts primary key columns). |
| `_shared_buffer` | `SharedTableReader \| None` | Structured shared memory segment backed by huge pages (1GB/2MB/THP) containing preloaded baseline tables. |
| `hugepages` | `str` | Active huge page allocation mode (`"auto"`, `"1g"`, `"2m"`, `"thp"`, `"off"`). |

---

## 3. Helper Functions & Utilities

### 3.1. `compute_ast_hash(joins: JoinsType, filtered_columns: tuple[SafeDataType, ...]) -> bytes`
Computes a deterministic 32-byte binary SHA-256 digest from the canonical join graph structure and non-None column data:
```python
key_str = f"{joins}:{filtered_columns}"
return hashlib.sha256(key_str.encode("utf-8")).digest()
```

### 3.2. `get_hashing_table(table: Table | None) -> Table | None`
Resolves the linked hashing `Table` instance from `table.hashing_table`:
- If `isinstance(table.hashing_table, str)`: searches `self.tables` by `table_name`.
- If `isinstance(table.hashing_table, int)`: retrieves `self.tables.get(table_id)`.
- If `hasattr(table.hashing_table, "table_id")`: retrieves from `self.tables`.
- If `table.hashing_table is True`: searches for `{table.table_name}_hash`.
- Otherwise returns `None`.

### 3.3. `get_table_engine(name: str | type | None) -> type[TEDirectDB | TECachedDB]`
Factory resolver mapping aliases to TableEngine classes:
```python
from table_engine import get_table_engine

EngineClass = get_table_engine("cached")  # -> TECachedDB (default)
EngineClass = get_table_engine("direct")  # -> TEDirectDB
```

| Engine Alias | Resolved Class | Description |
| :--- | :--- | :--- |
| `"cached"`, `"tecacheddb"`, `None` | `TECachedDB` | In-memory cached table engine (default) |
| `"direct"`, `"tedirectdb"` | `TEDirectDB` | Direct database passthrough table engine |

### 3.4. `TECachedDB` Internal Cache Management
Internal helper methods used exclusively by `TECachedDB` to synchronize in-memory caches and indices without querying the database driver:
- `_normalize_binary_val(val: Any) -> bytes`: Coerces binary column values (`bytearray`, `memoryview`, or Latin-1/UTF-8 decoded `str`) to canonical `bytes`.
- `_match_columns(row: tuple, filter_cols: tuple) -> bool`: Verifies if a row matches all non-None criteria in `filter_cols`.
- `_is_version_scoped(table: Table) -> bool`: Returns `True` if `table.version_scoped` or `table.blob_scoped` is enabled, or if table contains `blob_id`, `commit_id`, `vid`, or `vid_s`.
- `_get_vid_col_idx(table: Table) -> int | None`: Locates the 0-indexed column position of the scoping column (`blob_id`, `commit_id`, `vid`, or `vid_s`).
- `_index_row(table: Table, row: tuple) -> None`: Indexes a row into `_pk_index`, `_nodup_index`, and selectively into `_col_indices` (exempting primary key columns).
- `_unindex_row(table: Table, row: tuple) -> None`: Removes a row from all internal indices prior to updating.
- `_ensure_table(table_id: int) -> None`: Initializes cache lists and index dictionaries for a table upon first access.
- `_committed_nodup_id(table_id: int, columns: tuple) -> int | None`: The id a `no_duplicate` key already has, from `_nodup_index`, then `_committed_nodup_keys`. A table outside `_nodup_complete` (its cache was purged by `clear_cache()` or a `commit(update_in_mem_indexes=False)`) resolves a miss with one targeted `db.select()` through `_db_read()`, memoized in `_committed_nodup_keys`. `None` means the key is new. Without it, staging after a purge minted a second id for content already stored (duplicate `m_file_name` rows, `m_file_blob` unique-key violations).

### 3.5. Structured Shared Memory with Huge Pages (`table_engine/shared_buffer.py`)
- **`SharedBufferAllocator.allocate(size: int, mode: str = "auto") -> tuple[mmap.mmap, str, int]`**: Allocates shared memory with tiered fallback: `1GB HugeTLB` (`MAP_HUGE_1GB`) $\to$ `2MB HugeTLB` (`MAP_HUGE_2MB`) $\to$ `2MB Transparent Huge Pages` (`madvise(MADV_HUGEPAGE)`) $\to$ standard `4KB anonymous mmap`.
- **`SharedTableBuilder`**: Packs preloaded relational tables and builds $O(1)$ open-addressing hash indices directly inside raw memory bytes during the parent process preloading phase using 64-bit SplitMix64 / FxHash composite key tuple mixing.
- **`SharedTableReader`**: Zero-deserialization reader providing lock-free $O(1)$ Primary Key probes (`get_row_by_pk`) and scan matching (`scan_matching`) against raw `mmap` memoryviews without allocating intermediate Python objects.
- **`SharedSymbolReader`**: Zero-copy shared memory reader querying pre-resolved symbols in $O(1)$ lock-free time; supports transparent mapping interface (`__contains__`, `__getitem__`, `get`) with `(sym_name, sym_type)` or `sym_name`. Tables built with `build_file_scoped()` are queried per defining file with `get_file_symbol(rel_file, sym_name, sym_type)` and `get_file_name(rel_file, sym_name)` (key `rel_file + "\x00" + sym_name`), so same-named symbols of different files never bind to each other. `nbytes` reports the mapping size; `close()` unmaps it.
- **`SharedSymbolBuilder(mode)`**: Packs symbol maps into open-addressing tables (empty slots initialized with one bulk copy). `build(symbols_map, names_map)` builds a global `(name, type)` table; `build_file_scoped(file_symbols)` builds a table keyed by defining file from `rel_file -> {(name, type): ast_id}`. Symbol tables skip 1GB HugeTLB pages (`SharedBufferAllocator.allocate(..., allow_1g=False)`; a symbol table is far smaller than a 1GB page) and otherwise follow the mode's quiet fallbacks; `4k` maps to `off`.
- **`SharedSymbolChain`**: Ordered list of file-scoped segments queried newest-first (`get_file_symbol`, `get_file_name`, `sym_count`, `nbytes`, `close()`); a newer segment shadows an older one for the same file. Stage 2 appends one segment per epoch instead of re-packing every symbol.

---

## 4. Exhaustive API Method Specifications

### 4.1. Lifecycle & Connection Management

- **`__init__() -> None`**
  - Initializes empty state dictionaries (`tables`, `queued_set`, `queued_update`, `queued_view`, `next_id`) and sets `db = None`.
  - In `TECachedDB`: Also initializes empty index dictionaries (`_cached_rows`, `_pk_index`, `_nodup_index`, `_col_indices`).
- **`start_new_db(db: Callable[[], Any] | type[Any], is_worker: bool = False) -> None`**
  1. Safely closes active `self.db` (if present) and instantiates a new driver: `self.db = db()`.
  2. Resets `queued_view = {}`.
  3. For all registered `self.tables`: resets `queued_set[t_id] = {}`, `queued_update[t_id] = []`, and refreshes `next_id[t_id] = self.db.get_next_id(t)`.
  4. In `TECachedDB`:
     - **Worker Bypass (`is_worker=True`)**: Forked worker processes retain the inherited read-only shared huge-page buffers (`_shared_buffer` and the carried tables' buffer), initializing empty local mutation overlays and bypassing redundant database preloading (`select_preload()`).
     - **Parent Initialization**: Calls `clear_cache()`, ensures index structures exist, and preloads database records for tables where `table.te_cached` is truthy via `db.select_preload(table, cached_columns=cached_cols, min_vid=min_vid)`:
       - **Selective Column Pushdown**: For column-level cached tables (e.g. `m_tag_code`), queries only configured columns (`SELECT hash FROM m_tag_code`) and reconstructs canonical rows with `None` in un-cached positions.
       - **Version Window Predicate Pushdown**: For `version_scoped=True` tables, pushes the working-window filter (`Old_VID` is a commit id) into the SQL query on the table's version column (`WHERE vid >= %s`, `WHERE (vid_e = 0 OR vid_e >= %s)`, or `WHERE commit_id >= %s` for `m_bridge_checkpoint`, i.e. the previous version's checkpoint). Blob ids are never compared with it.
       - **Final misses inside the window**: a successful preload records `_preload_window[table_id] = (version column, first version)` for such tables. A primary-key `get()` miss whose version is at or after it returns `None` without a database read (`_miss_is_final()`): every row of the window is cached or staged. Rows committed and evicted from the heap since (`_committed_pks`) are still read. The window is dropped by `clear_cache()`, by `commit(update_in_mem_indexes=False)` for its table, and when a row is staged while `update_in_mem_indexes` is off. Before this, looking up the current version's checkpoint row of each unchanged file cost one query each (about 18k per version).
       - For standard cached tables: Preloads all records.
       - Indexes all rows into `_pk_index`, `_nodup_index`, and selectively into `_col_indices`.
       - **Shared Huge-Page Packing**: If `hugepages != "off"`, serializes preloaded tables into structured shared memory via `SharedTableBuilder.build()`, backing lookups with 1GB/2MB/THP pages.
       - **Carried Hash Tables (`CARRIED_TABLES`: `m_ast_hash`, `m_tag_code`)**: These deduplication tables are only written through the parent's TableEngine and are needed in full, so they are not reloaded every version. The first load from a non-empty database (a reset database holds their seed rows) packs them into their own long-lived shared buffer (`_carried_reader`, without per-row heap indexing; on a completely empty database they stay in the heap for that version); every commit adds the rows it wrote to a per-table overlay (`_carried_overlays`, also the views' local layer); `close()` and the next `start_new_db()` drop rows that were staged but never committed. A later start reuses snapshot + overlay without a database query (`[TECachedDB] Reused carried tables` log line) until an overlay exceeds `CARRY_RELOAD_RATIO` (0.5) of its snapshot; then snapshot and overlay are repacked into a new buffer from memory (`_carried_rows_in_memory()`, `Rebuilt from memory` log line), since together they hold exactly what the database has. The database is only read again after the carried state was discarded: a commit that failed on a carried table discards it, since the database may hold part of its rows. Carried tables find committed keys through their overlay, so they skip the `_committed_pks` copy (`_tracks_committed_pks()`). `clear_cache()` keeps them attached.
- **`start(tables: Sequence[Table] | Table, db: Callable[[], Any] | type[Any]) -> None`**
  1. Normalizes `tables` into a tuple/list.
  2. Registers all tables in `self.tables`.
  3. Calls `self.start_new_db(db)`.
- **`clear_cache() -> None`** *(TECachedDB only)*
  - Clears `_cached_rows`, `_pk_index`, `_nodup_index`, and `_col_indices` (carried tables are re-attached).
- **`close() -> None`**
  - Waits for a pending background commit (`wait_for_commit()`, raising its error), then calls `self.db.close()` if available (ignoring exceptions) and sets `self.db = None`. `start_new_db()` waits the same way.
  - In `TECachedDB`: Drops uncommitted rows from the carried overlays, then calls `clear_cache()` to release memory resources; the carried tables survive for the next start.
- `discard_carried() -> None` *(TECachedDB)*: Forgets the carried tables' snapshot and overlays, so the next start reloads them from the database. Called after rows were deleted outside the TableEngine (an interrupted version rolled back, `core/ingest_state.py`).
- **Context Manager**: `__enter__() -> Self`, `__exit__(...) -> close()`, `__del__() -> close()`.

---

### 4.2. Single-Table Operations (`get`, `set`, `update`)

- **`get(table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - **In `TECachedDB` (`te_cached=True` tables)**:
    1. **Primary Key Fast-Path**: If all primary key columns are non-None, checks local overlay `_pk_index[table_id].get(pk)`. If missing and `_shared_buffer` is active, probes the shared memory open-addressing hash index via `_shared_buffer.get_row_by_pk(table_id, pk)`. If missing and `version_scoped=True`, falls back to `db.select()`.
    2. **Deduplication Key Fast-Path**: If `table.no_duplicate` and `columns[1:]` non-None, resolves the id with `_committed_nodup_id()` (see below) and verifies the row via `_match_columns(row, columns)`.
    3. **Column Index Accelerated Path**: Identifies all indexed non-None columns in `_col_indices`, selects the column index with the smallest candidate pool, and checks candidate matches via `_match_columns(row, columns)`. If missing and `version_scoped=True`, falls back to `db.select()`.
    4. **In-Memory Linear Scan & Shared Scan**: Scans `_cached_rows[table_id]` using `_match_columns(row, columns)`. If missing and `_shared_buffer` is active, scans shared memory records via `_shared_buffer.scan_matching(table_id, columns)`. Falls back to `db.select()` for out-of-window version-scoped queries.
  - **In `TEDirectDB` (or non-cached tables)**:
    1. **Staged Memory Check (`queued_set`)**:
       - `no_duplicate=True`:
         - Exact key: If all `columns[1:]` are non-None, checks `queued_set[table_id].get(columns[1:])`.
         - Secondary index fast-path: Checks `_queued_sec_indexes` and `_queued_single_col_indexes` in $O(1)$.
         - Wildcard filter: Iterates `queued_set[table_id].items()`, checking `columns` match against staged rows `(v, *k)`.
       - `no_duplicate=False`:
         - Explicit PK: If all primary columns are non-None, checks `queued_set[table_id].get(pk)` for match in $O(1)$.
         - Secondary index fast-path: When primary key is not specified (e.g. `(None, name, type_id)` or `(None, name, None)`), checks `_queued_sec_indexes` (composite keys) and `_queued_single_col_indexes` (single columns) in $O(1)$, resolving symbol hits and immediate misses without linear scans.
         - Wildcard filter fallback: Iterates `queued_set[table_id].values()` only if table has no matching indexes.
    2. **Emptiness Guard**: If `getattr(table, "has_auto_increment", True) and table.initial_insert is None and self.next_id.get(table_id, 0) <= 1 and (table_id not in self.queued_set or not self.queued_set[table_id])`, returns `None` immediately without querying DB.
    3. **Database Query**: Executes `self.db.select(table, columns)` through `_db_read()`: it first joins a background commit that writes this table (read-your-writes barrier) and counts the read in `db_reads` / `db_read_s`. Every database fallback of `get()`, `view_get()` and `view_get_multiple()` (the join graph's tables) goes through it.
- **`set(table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]`**
  - **Case 1 (`table.no_duplicate == True`)**:
    - Key: `key = columns[1:]`.
    - If in cache/staged: returns `(assigned_id, *columns[1:])`.
    - If the database already has the key (`_committed_nodup_id()`): returns its row and, in `TECachedDB`, caches it. This also applies with `update_in_mem_indexes=False`, where nothing is staged under a new id.
    - Else: assigns `assigned_id = next_id[table_id]`, increments `next_id[table_id] += 1`, stages `queued_set[table_id][key] = assigned_id`.
    - In `TECachedDB`: Appends row to `_cached_rows` and calls `_index_row()`.
    - Returns `(assigned_id, *columns[1:])`.
  - **Case 2 (`columns[0] is None` - Auto-Increment Generation)**:
    - **Unique Key Deduplication**: If `table.unique_keys` is configured and matches an already staged row in `_queued_sec_indexes` or a committed row in `_committed_unique_keys` across all unique key columns, returns the existing record immediately without allocating a new ID or queuing duplicate entries.
    - Assigns `assigned_id = next_id[table_id]`, increments `next_id[table_id] += 1`.
    - Builds `row = (assigned_id, *columns[1:])`.
    - Stages `queued_set[table_id][assigned_id] = row`.
    - In `TECachedDB`: Appends row to `_cached_rows` and calls `_index_row()`.
    - Returns `row`.
  - **Case 3 (Explicit Primary Key Provided)**:
    - Extracts `pk = itemgetter(*table.primary)(columns)`.
    - **Idempotent Primary Key & Cryptographic Hash Fast-Path**: For tables with `idempotent_pk=True` (e.g. `m_bridge_commit_person`) or cryptographic hash tables (`init_primary == ("hash",)` or `m_tag_code`, `m_ast_hash`), returns existing row if already staged in `queued_set` or present in `_pk_index` / committed cache, preventing duplicate queuing and preserving no-duplicate integrity.
    - **Duplicate Primary Key Rejection**: For standard relational tables without `idempotent_pk`, `set()` enforces strict database-level primary key uniqueness: if `pk` is already staged in `queued_set[table_id]` (or present in cache for `TECachedDB`), `set()` raises `ValueError(f"Duplicate primary key '{pk}' in table '{table.table_name}'")`. Callers must use `update()` to modify existing records, or `get_set()` / `no_duplicate=True` to safely avoid duplicate inserts.
    - Stages `queued_set[table_id][pk] = columns`.
    - In `TECachedDB`: Appends `columns` to `_cached_rows`, and calls `_index_row()`.
    - Returns `columns`.
- **`update(table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]`**
  - Appends `columns` to `self.queued_update[table_id]`.
  - In `TECachedDB`: For `te_cached=True` tables, unindexes old row matching PK via `_unindex_row()`, updates `_cached_rows`, and re-indexes updated row via `_index_row()`.
  - Returns `columns`.

---

### 4.3. Multi-Table Relational Views (`view_get`, `view_get_multiple`, `view_set`)

- **`view_get(joins: JoinsType, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - `initial_table_id = PointerGetter(joins).get_first_table_id()`.
  - If `getattr(table, "has_auto_increment", True) and table.initial_insert is None and next_id[initial_table_id] <= 1 and initial table has no staged entries`: returns `None`.
  - **Schema-Driven Hash Fast-Path**: If `table.hashing_table` is configured:
    1. Computes SHA-256 hash `h = compute_ast_hash(joins, filtered_columns)`.
    2. Checks staged buffer: `staged_row = self.queued_set.get(hash_table.table_id, {}).get(h)`. If found, returns view tuple with `ast_id = staged_row[1]`.
    3. Checks DB / cache: `hash_row = self.get(hash_table.table_id, (h, None))`. If found, returns view tuple with `ast_id = hash_row[1]`.
    4. If not found, returns `None` immediately.
  - **Fallback**: Returns `self.db.view_select(self.tables, joins, columns)`.
- **`get_in(table_id: int, col_idx: int, values: Sequence[SafeDataType]) -> list[tuple[SafeDataType, ...]]`**
  - Committed rows whose column `col_idx` is one of `values`, from `db.select_in()` through `_db_read()` (staged rows are not included).
- **`view_get_multiple(joins: JoinsType, columns: tuple[SafeDataType, ...]) -> list[tuple[SafeDataType, ...]]`**
  - Returns `self.db.view_select_multiple(self.tables, joins, columns)`.
- **`view_set(joins: JoinsType, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]`**
  1. Extracts non-None values: `filtered_columns = tuple(x for x in columns if x is not None)`.
  2. If `joins` in `queued_view` and `filtered_columns` in `queued_view[joins]`:
     - Returns `tuple(x if x is not None else queued_view[joins][filtered_columns] for x in columns)`.
  3. **Schema-Driven Hash Fast-Path**: If `main_table.hashing_table` is configured:
     - Computes SHA-256 hash `h = compute_ast_hash(joins, filtered_columns)`.
     - Checks staged `queued_set[hash_table.table_id]` for `h`. If match, caches in `queued_view` and returns tuple with existing `ast_id`.
     - Checks DB / cache via `self.get(hash_table.table_id, (h, None))`. If match, caches in `queued_view` and returns tuple with existing `ast_id`.
     - If new:
       - Assigns `current_view_id = next_id[main_table_id]`; increments `next_id[main_table_id] += 1`.
       - Caches in `queued_view[joins][filtered_columns] = current_view_id`.
       - Decomposes `result` across constituent tables using `self.set(pointer[0], row)` for each join segment.
       - Stages `(h, current_view_id)` into `hash_table` via `self.set(hash_table.table_id, (h, current_view_id))`.
       - Returns `result`.
  4. Else (Non-Hashed Views):
     - `main_table_id = PointerGetter(joins).get_first_table_id()`.
     - Assigns `current_view_id = next_id[main_table_id]`; increments `next_id[main_table_id] += 1`.
     - Caches in `queued_view[joins][filtered_columns] = current_view_id`.
     - `result = tuple(x if x is not None else current_view_id for x in columns)`.
     - **Decomposition**: Slices `result` sequentially using `PointerGetter(joins)`:
       ```python
       data_offset = 0
       for repeat, pointer in PointerGetter(joins):
           target_table = self.tables[pointer[0]]
           t_len = target_table.length
           for _ in range(repeat):
               row = result[data_offset : data_offset + t_len]
               self.set(pointer[0], row)
               data_offset += t_len
       ```
     - Returns `result`.

- **Joined View Parameter Flattening**: Container arguments passed to `m_ast.view(...)` with `m_ast_container` must be supplied as flat positional scalars (`*cs_inserter` where `cs_inserter = [None, priority, type_id, ref_ast_id]`), never as nested tuples inside lists (e.g. `[(None, priority, type_id, ref_ast_id)]`), because `is_data_unsafe()` treats nested tuples as unresolved references `(query, OP_REF, route)`, triggering `resolve_ref()` on integer AST types and failing with `TypeError: 'int' object is not subscriptable`. Furthermore, child constructs in AST zones that generate positional routes (`with CS(REF_POS):` and `self.ast_ref`) must be executed outside `with CS(REF_NO_REF):` so their positional routes are preserved.
- **AST Node View Deduplication Invariant**: All AST node creation, structural staging, and deduplication across language parsers (`c_ast`, `kconfig_ast`, `maintainer_ast`, `raw_ast`, `rust_ast`) must use `m_ast.view(...)` for single nodes (`((m_ast.ast_id,),)`) and multi-table joined views for relationships (e.g. `((m_ast.ast_id, m_ast_container.ast_id, N),)` or `((m_ast.ast_id, m_ast_include.ast_id, 1),)`), never `m_ast.set(...)` or `m_ast.get_set(...)`. This allows TableEngine's hash deduplication (`m_ast_hash`) to automatically index and deduplicate AST structures. Direct primary key lookups by `ast_id` (e.g. in carryover tag tracking) remain as direct `Table.get(ast_id, None, None)`.

---

### 4.4. Transaction Commit Protocol (`commit`, `commit_all`)

Both build a **`CommitBatch`** with `_take_staged(table_ids)`:
- **Inserts (`queued_set`)**: `no_duplicate=True` rows become `(assigned_id, *data_key)`; keys already in `_committed_nodup_keys` / `_committed_pks` (hash-keyed or `idempotent_pk` tables) are skipped; the rest are recorded there, together with `_committed_unique_keys`, and the keys this batch added are kept in the batch.
- **Batch Pre-Sorting (Cryptographic Hash Tables)**: Payloads are sorted by primary key, so tables keyed by pseudo-random binary hashes (e.g. `m_tag_code`, `m_ast_hash`) are inserted in ascending B+Tree leaf order in InnoDB, reducing random page splits and improving locality by >5x.
- **Updates (`queued_update`)** become the update payload.
- The staging containers (`queued_set`, `queued_update`, staged secondary indexes) are swapped for empty ones, even when every row was filtered out, so rows staged from then on are a new generation.

**Failure restore (`_restore_batch()`)**: if the database did not commit some tables (`failed_table_ids` on the driver's error, otherwise all of them), their rows go back to staging and the keys the batch added leave the committed-key sets again, so a retried commit writes them instead of skipping them. `TECachedDB` also re-caches rows the commit had evicted from the heap and discards the carried state when a carried table failed.

- **`commit(table_id: int, update_in_mem_indexes: bool = True) -> None`**
  1. Waits for a pending background commit.
  2. Takes the table's batch and writes it on the calling thread with `self.db.insert(table, payload)` / `self.db.update(table, payload)`, inside `self.db.transaction()` when the driver has one; restores and re-raises on failure.
  3. **In-Memory Cache Management**: If `update_in_mem_indexes=False` in `TECachedDB`, purges `table_id` cache and indices from RAM. Otherwise write-heavy tables (`WRITE_HEAVY_TABLES`) drop their committed rows from the heap and keep only their keys in `_committed_pks`; for a table backed by a shared buffer, only the rows staged on top of the snapshot are moved (`SharedTablePKView.drain_local()`), and the view itself stays in place. Carried tables keep their rows (recorded in their overlay). A table with no cache structures at that point (cleared by an earlier `commit_all(update_in_mem_indexes=False)`) is skipped, so `_ensure_table()` rebuilds all of them on its next `set()`.

- **`commit_all(max_workers: int | None = None, update_in_mem_indexes: bool = True) -> None`**
  1. Waits for the previous background commit (`wait_for_commit()`: on failure its rows are restored and its error raised, so `_commit_all_with_retry()` simply calls `commit_all()` again).
  2. Takes one batch across all tables.
  3. **Background write**: with `async_commits`, `update_in_mem_indexes=True` and a driver with `supports_background_commit`, starts a writer thread running `db.commit_tables_parallel(..., dedicated=True)` and returns. The cache upkeep below runs meanwhile as if the write had landed; any database read of the batch's tables joins it first (`_db_read()`), so lookups never miss rows that are being written.
  4. Otherwise writes on the calling thread via `db.commit_tables_parallel(tables_data, max_workers=max_workers)` (fallback: `insert()` / `update()` per table).
  5. **In-Memory Index Teardown**: If `update_in_mem_indexes=False` in `TECachedDB`, immediately invokes `self.clear_cache()` to evacuate all cached rows, position maps, and indices from memory, avoiding redundant post-commit index synchronization before engine closure. Otherwise evicts write-heavy tables as in `commit()`.
  - `TECachedDB.commit_all()` ends with `trim_heap()` (glibc `malloc_trim` only: committed rows are freed by reference counting, so no full garbage collection pass) and logs `[TECachedDB] commit_all: N staged rows written in Xs, cache upkeep Ys; slowest tables: ...` when a synchronous commit takes 2 s or more; a background commit logs `[TECachedDB] background commit: N staged rows written in Xs (Ys overlapped, Zs waited); slowest tables: ...` when it is joined.

- **`wait_for_commit() -> None`**: Joins the pending background commit (no-op without one); restores and raises on failure. Nothing may fork while a writer thread runs, so the pipeline calls it before forking workers and at the end of every stage.

---

## 5. Expected Database Driver Interface (`G.DB` / `BaseDBEngine`)

Any backend passed to `TableEngine` must implement:
- `get_next_id(table: Table) -> int`: Queries `COALESCE(MAX(pk), 0) + 1`.
- `select(table: Table, columns: tuple) -> tuple | None`: Single-row select matching non-None filters.
- `view_select(tables: dict, joins: JoinsType, columns: tuple) -> tuple | None`: Joined single-row select.
- `view_select_multiple(tables: dict, joins: JoinsType, columns: tuple) -> list[tuple]`: Joined multi-row select.
- `insert(table: Table, data: tuple[tuple, ...]) -> None`: Batch insert (e.g. 1000 rows/batch).
- `update(table: Table, data: tuple[tuple, ...]) -> None`: Batch upsert (`ON DUPLICATE KEY UPDATE`).
- `commit_tables_parallel(tables_data, max_workers=None, dedicated=False) -> list[TableCommitStat]`: Parallel table commit across worker threads, each table atomic; a failure carries `failed_table_ids`. `dedicated=True` (background writes, only requested when `supports_background_commit` is set) never uses the instance's own connection.
- `transaction()` *(optional)*: Context manager making a table's `insert()` / `update()` one transaction (used by `commit()`).
- `close() -> None`: Closes connections/cursors cleanly.

---

## 6. Critical Invariants for Custom Implementations

1. **Monotonic Sequences**: `next_id` per table must never reuse or decrement assigned IDs within an update cycle.
2. **Buffer Transformations**: On `commit()`, `no_duplicate` tables must reconstruct rows as `(assigned_id, *data_key)` before calling `db.insert()`.
3. **Multiprocessing Isolation**: Re-invoke `start_new_db()` in child workers to ensure separate DB connection sockets.
4. **Tuple Immutability**: All returned and cached rows must be immutable tuples of primitive `SafeDataType`.
5. **Strict Upstream Deduplication**: Existing records in database/cache must be reused without allocating new sequence IDs, and strict `INSERT INTO` must be maintained at the database layer.
6. **B+Tree Clustered Index Insertion Ordering**: For tables using random cryptographic hashes (`BINARY(32)`) as Primary Keys (`m_tag_code`, `m_ast_hash`), batch insert payloads should maintain sorted primary key ordering to minimize InnoDB page splits and buffer pool thrashing.
7. **Public Engine Invariant**: Code, tests, and workflows must never assume tables are cached in memory or access private engine internals (e.g. `_cached_rows`, `_pk_index`). Queries must use public TableEngine APIs (`Table.get()`, `G.TE.get()`, `Table.get_set()`) or assert against database state (`MockDB._global_store`), ensuring complete compatibility regardless of the active TableEngine (`TEDirectDB` vs `TECachedDB`) or table caching configuration (`te_cached=True/False`).
8. **Duplicate Primary Key Enforcement & Cryptographic Hash Fast-Path**: For standard relational tables, TableEngine `set()` enforces database-level primary key uniqueness: calling `set()` with an explicit primary key that is already staged in `queued_set` or present in cache raises `ValueError`. Callers must use `update()` to mutate existing records or `get_set()` / `no_duplicate=True` to safely avoid duplicate inserts. For cryptographic hash lookup tables (`m_tag_code`, `m_ast_hash`), matching hashes guarantee identical content and return the existing row via an $O(1)$ fast-path.
9. **Teardown Commit Acceleration**: When committing final update cycles, teardowns, or write-only batches before engine closure, use `G.TE.commit_all(update_in_mem_indexes=False)` (or `G.TE.commit(table_id, update_in_mem_indexes=False)`) to bypass redundant in-memory cache/index synchronization and immediately release cache memory before `G.TE.close()`.
10. **Huge-Page Shared Memory & Fork Inheritance**: Preloaded baseline tables stored in structured shared memory (`_shared_buffer`) remain immutable across parallel worker processes. Workers calling `start_new_db(..., is_worker=True)` inherit the shared memory segment across `fork()`, isolating local mutation overlays in process-local heap and eliminating multi-worker database preload stampedes. When `_shared_buffer` is active, workers initialize `_committed_pks[table_id] = set()` and `_committed_nodup_keys[table_id] = {}` directly in $O(1)$ time without unpacking row keys from shared memory views, eliminating multi-gigabyte heap allocations and Copy-on-Write page duplication at worker startup.
11. **Cross-Chunk Deduplication Tracking**: TableEngine implementations (`TEDirectDB`, `TECachedDB`) maintain `_committed_nodup_keys` (mapping unique data keys to assigned IDs for `no_duplicate=True` tables) and `_committed_pks` (tracking primary keys for hash lookup tables like `m_tag_code` and `m_ast_hash`; carried tables use their overlay instead) across intermediate chunk commits. During `commit()` and `commit_all()`, staged rows matching previously committed keys/hashes are filtered out, and `set()` / `get()` provide $O(1)$ fast-path resolution, preventing duplicate `INSERT INTO` attempts in subsequent chunks. Keys are recorded when the batch is taken and removed again for tables the database did not commit (`_restore_batch()`), so a retried commit cannot skip them.
16. **Background Commits & Read-Your-Writes**: While `async_commits` is on, at most one `CommitBatch` is written in the background (the next commit joins it first). Rows of a batch leave staging when it is taken, so every database read the engine issues waits for a batch that writes one of the tables it reads (`_db_read()`), and code reading the database directly must call `wait_for_commit()` first. Forked children never own a writer (`start_new_db(..., is_worker=True)` drops `_pending`), and the parent joins before forking.
12. **Parent Process Fork Socket Refresh**: The parent process must never invoke `start_new_db(..., is_worker=True)` after forking workers. Calling `is_worker=True` without shared huge pages resets and wipes in-memory table caches, causing subsequent operations (e.g. `processing_unchanges()`) to treat existing records as missing. The parent process must only refresh its DB connection handle (`if G.TE.db: G.TE.db.close(); G.TE.db = G.DB()`) without modifying cached table state or staging queues.
13. **Zero-Copy Shared Memory Views & Heap Cache Eviction**: When `_shared_buffer` is constructed via `SharedTableBuilder`, preloaded table row heap collections (`_cached_rows`) and primary key indices (`_pk_index`) are immediately replaced with `SharedTableRowSequenceView` and `SharedTablePKView`. These zero-copy proxy views query the shared mmap buffer directly with dynamic mutation support and $O(1)$ length/truthiness checks (`__len__`, `__bool__`). Upon activation, redundant secondary column indices (`_col_indices`), committed hash caches (`_committed_pks`), and nodup key mappings (`_committed_nodup_keys`) for shared-backed tables are purged, evicting duplicate preloaded heap memory and releasing pages back to the OS via glibc `malloc_trim(0)`.
14. **Write-Heavy Chunk Commit Heap Trimming**: During intermediate and final chunk commits (`commit()`, `commit_all()`), write-heavy relational tables (`m_ast_hash`, `m_tag_code`, `m_ast_container`, `m_bridge_tag`, `m_bridge_checkpoint`, `m_blob_transition`) are purged from heap caches (`_cached_rows`, `_pk_index`) while their compact 32-byte Primary Keys are recorded into `_committed_pks`. This bounds Python process heap growth during massive multi-thousand file ingestion runs while preserving full upstream deduplication guarantees.
15. **Zero-Copy File-Scoped Shared Symbols & Stage 2 Multi-Epoch Offloading**: During Stage 2 source streaming, symbols published by executed files are packed into read-only file-scoped shared memory segments (`SharedSymbolBuilder.build_file_scoped()`) held in a `SharedSymbolChain` (`gp._shared_symbols`). Forked workers and the parent's `TableHandling.resolve_ref()` query them per target file in $O(1)$ time without IPC serialization or heap duplication. Before Stage 2 and at each epoch boundary only the symbols published since the previous segment are packed into a new segment (no re-packing of earlier symbols), and the process-local heap dictionaries (`file_symbols`, `_ast_staged_symbols`) are cleared to prevent Copy-on-Write page duplication across forks. `DependencyScheduler.finalize()`, `gp.init_cs_dict()` and `gp.reset_cs()` unmap the chain (`gp.close_shared_symbols()`), so no segment outlives its version. Intermediate chunk commits and deferred queue draining prevent uncommitted memory accumulation.

---

## 7. Subsystem Verification & Regression Suite (`unit_test/test_table_engine.py`)

Comprehensive contract verification and isolated testing for all TableEngine implementations (`TEDirectDB`, `TECachedDB`) is implemented in `unit_test/test_table_engine.py` using `unit_test/harness.py`.

Key test capabilities:
- **Complete Database Isolation**: Replaces live database engines with `RecordingDriver` (a test spy adhering to `BaseDBEngine`), completely isolating TableEngine buffer transformations, indexing, sequence allocation, and deduplication logic from database networking or storage dependencies.
- **Dynamic Engine Discovery**: Automatically discovers all unique TableEngine implementations registered in `TABLE_ENGINE_MAP` and dynamically synthesizes corresponding test cases (`Test_TEDirectDBEngine`, `Test_TECachedDBEngine`).
- **Feature Tracking & Dual-Trace Diagnostics**: Every test method uses `@test_feature(feature_name, depends_on=...)` and granular `with test_step("...", diagnostic="...")` context blocks. If an assertion or unexpected error occurs, the test harness reports both the high-level failing feature/sub-step context and the complete Python traceback.
- **Relational View & Decomposition Testing**: Validates multi-table view decomposition (`view_set()`) across parent/child tables, AST hash deduplication via `compute_ast_hash()`, and broken view construction resilience (asserting explicit `KeyError` on unregistered table IDs and `IndexError` on malformed graphs or short column lengths).
- **Deduplication & Collision Invariants**: Verifies `no_duplicate=True` sequence reuse, explicit primary key fast-path overwrites, and cross-chunk deduplication tracking (`_committed_nodup_keys`, `_committed_pks`) across multi-phase commit cycles.
- **In-Memory Cache Capabilities (`TECachedDB`)**: Verifies startup selective column cache preloading, zero-DB query dispatch on cache hits, atomic mutation synchronization (`set()`, `update()`), and cache evacuation on `clear_cache()` and `commit_all(update_in_mem_indexes=False)`.

