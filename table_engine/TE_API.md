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
  - `te_cached: bool | tuple[str | int, ...]` &mdash; If configured (`True` or tuple of column names/indices), enables in-memory preloading and multi-indexing in `TECachedDB`.
  - `cached_columns: tuple[int, ...]` &mdash; Canonical 0-indexed column indices retained in memory (defaults to all columns if `te_cached=True`, or empty if `False`).
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
| `db` | `Any \| None` | Active database driver handle (or `None`). |

### 2.2. Extended In-Memory Caching State (`TECachedDB`)

| Attribute | Type | Purpose & Structure |
| :--- | :--- | :--- |
| `_cached_rows` | `dict[int, list[tuple]]` | In-memory row storage for `te_cached` tables: `table_id -> [projected_row_tuple, ...]`. |
| `_pk_index` | `dict[int, dict[Any, tuple]]` | O(1) exact row lookup: `table_id -> {primary_key_val: row_tuple}`. |
| `_nodup_index` | `dict[int, dict[tuple, int]]` | O(1) deduplication lookup: `table_id -> {columns[1:]: assigned_id}`. |
| `_col_indices` | `dict[int, dict[int, dict[SafeDataType, list[tuple]]]]` | Inverted column index: `table_id -> {col_idx: {column_val: [matching_row, ...]}}`. |

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
- `_match_columns(row: tuple, filter_cols: tuple) -> bool`: Verifies if a row matches all non-None criteria in `filter_cols`.
- `_index_row(table: Table, row: tuple) -> None`: Indexes a row into `_pk_index`, `_nodup_index`, and `_col_indices`.
- `_unindex_row(table: Table, row: tuple) -> None`: Removes a row from all internal indices prior to updating.
- `_ensure_table(table_id: int) -> None`: Initializes cache lists and index dictionaries for a table upon first access.

---

## 4. Exhaustive API Method Specifications

### 4.1. Lifecycle & Connection Management

- **`__init__() -> None`**
  - Initializes empty state dictionaries (`tables`, `queued_set`, `queued_update`, `queued_view`, `next_id`) and sets `db = None`.
  - In `TECachedDB`: Also initializes empty index dictionaries (`_cached_rows`, `_pk_index`, `_nodup_index`, `_col_indices`).
- **`start_new_db(db: Callable[[], Any] | type[Any]) -> None`**
  1. Safely closes active `self.db` (if present) and instantiates a new driver: `self.db = db()`.
  2. Resets `queued_view = {}`.
  3. For all registered `self.tables`: resets `queued_set[t_id] = {}`, `queued_update[t_id] = []`, and refreshes `next_id[t_id] = self.db.get_next_id(t)`.
  4. In `TECachedDB`: Calls `clear_cache()`, ensures index structures exist, and unconditionally preloads all database records for all tables where `table.te_cached is True` via `db.view_select_multiple()`, populating `_cached_rows` and indexing all rows into `_pk_index`, `_nodup_index`, and `_col_indices`. If the database is empty and `table.initial_insert` is present, indexes initial seed rows.
- **`start(tables: Sequence[Table] | Table, db: Callable[[], Any] | type[Any]) -> None`**
  1. Normalizes `tables` into a tuple/list.
  2. Registers all tables in `self.tables`.
  3. Calls `self.start_new_db(db)`.
- **`clear_cache() -> None`** *(TECachedDB only)*
  - Clears `_cached_rows`, `_pk_index`, `_nodup_index`, and `_col_indices`.
- **`close() -> None`**
  - Calls `self.db.close()` if available (ignoring exceptions) and sets `self.db = None`.
  - In `TECachedDB`: Calls `clear_cache()` to release memory resources.
- **Context Manager**: `__enter__() -> Self`, `__exit__(...) -> close()`, `__del__() -> close()`.

---

### 4.2. Single-Table Operations (`get`, `set`, `update`)

- **`get(table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - **In `TECachedDB` (`te_cached=True` tables)**:
    1. **Primary Key Fast-Path**: If all primary key columns are non-None, checks `_pk_index[table_id].get(pk)`, verifies full filter match via `_match_columns(row, columns)`, and returns the row or `None`.
    2. **Deduplication Key Fast-Path**: If `table.no_duplicate` and `columns[1:]` non-None, checks `_nodup_index[table_id].get(columns[1:])`, verifies via `_match_columns(row, columns)`.
    3. **Column Index Accelerated Path**: Identifies all indexed non-None columns, selects the column index with the smallest candidate pool, and checks candidate matches via `_match_columns(row, columns)`.
    4. **In-Memory Linear Scan**: Scans `_cached_rows[table_id]` using `_match_columns(row, columns)`.
    - Zero SQL queries issued to the database.
  - **In `TEDirectDB` (or non-cached tables)**:
    1. **Staged Memory Check**:
       - `no_duplicate=True`: Checks `queued_set[table_id].get(columns[1:])`.
       - Explicit PK: Checks `queued_set[table_id].get(pk)` for wildcard matches.
    2. **Emptiness Guard**: If `getattr(table, "has_auto_increment", True) and table.initial_insert is None and self.next_id.get(table_id, 0) <= 1`, returns `None` immediately without querying DB.
    3. **Database Query**: Executes `self.db.select(table, columns)`.
- **`set(table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]`**
  - **Case 1 (`table.no_duplicate == True`)**:
    - Key: `key = columns[1:]`.
    - If in cache/staged: returns `(assigned_id, *columns[1:])`.
    - Else: assigns `assigned_id = next_id[table_id]`, increments `next_id[table_id] += 1`, stages `queued_set[table_id][key] = assigned_id`.
    - In `TECachedDB`: Appends row to `_cached_rows` and calls `_index_row()`.
    - Returns `(assigned_id, *columns[1:])`.
  - **Case 2 (`columns[0] is None` - Auto-Increment Generation)**:
    - Assigns `assigned_id = next_id[table_id]`, increments `next_id[table_id] += 1`.
    - Builds `row = (assigned_id, *columns[1:])`.
    - Stages `queued_set[table_id][assigned_id] = row`.
    - In `TECachedDB`: Appends row to `_cached_rows` and calls `_index_row()`.
    - Returns `row`.
  - **Case 3 (Explicit Primary Key Provided)**:
    - Extracts `pk = itemgetter(*table.primary)(columns)`.
    - Stages `queued_set[table_id][pk] = columns`.
    - In `TECachedDB`: Unindexes any old row matching `pk`, appends `columns` to `_cached_rows`, and calls `_index_row()`.
    - Returns `columns`.
- **`update(table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]`**
  - Appends `columns` to `self.queued_update[table_id]`.
  - In `TECachedDB`: For `te_cached=True` tables, unindexes old row matching PK via `_unindex_row()`, updates `_cached_rows`, and re-indexes updated row via `_index_row()`.
  - Returns `columns`.

---

### 4.3. Multi-Table Relational Views (`view_get`, `view_get_multiple`, `view_set`)

- **`view_get(joins: JoinsType, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - `initial_table_id = PointerGetter(joins).get_first_table_id()`.
  - If `tables[initial_table_id].initial_insert is None and next_id[initial_table_id] <= 1`: returns `None`.
  - **Schema-Driven Hash Fast-Path**: If `table.hashing_table` is configured:
    1. Computes SHA-256 hash `h = compute_ast_hash(joins, filtered_columns)`.
    2. Checks staged buffer: `staged_row = self.queued_set.get(hash_table.table_id, {}).get(h)`. If found, returns view tuple with `ast_id = staged_row[1]`.
    3. Checks DB / cache: `hash_row = self.get(hash_table.table_id, (h, None))`. If found, returns view tuple with `ast_id = hash_row[1]`.
    4. If not found, returns `None` immediately.
  - **Fallback**: Returns `self.db.view_select(self.tables, joins, columns)`.
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

---

### 4.4. Transaction Commit Protocol (`commit`, `commit_all`)

- **`commit(table_id: int) -> None`**
  1. **Inserts (`queued_set`)**: If `queued_set[table_id]` is non-empty:
     - `no_duplicate=True`: formats payload as `tuple(v if isinstance(v, (tuple, list)) else ((v, *k) if isinstance(k, tuple) else (v, k)) for k, v in self.queued_set[table_id].items())`.
     - `no_duplicate=False`: formats payload as `tuple(self.queued_set[table_id].values())`.
     - Calls `self.db.insert(table, payload)` and clears `self.queued_set[table_id]`.
  2. **Updates (`queued_update`)**: If `queued_update[table_id]` is non-empty:
     - Calls `self.db.update(table, tuple(self.queued_update[table_id]))` and clears `self.queued_update[table_id]`.

- **`commit_all(max_workers: int | None = None) -> None`**
  - Gathers payloads `(table, insert_payload, update_payload)` across all modified tables.
  - If `hasattr(self.db, "commit_tables_parallel")`, dispatches to `self.db.commit_tables_parallel(tables_data, max_workers=max_workers)`.
  - Fallback: Sequentially calls `self.db.insert()` and `self.db.update()` for all tables with pending payloads.
  - Clears `queued_set` and `queued_update` buffers across all tables.

---

## 5. Expected Database Driver Interface (`G.DB` / `BaseDBEngine`)

Any backend passed to `TableEngine` must implement:
- `get_next_id(table: Table) -> int`: Queries `COALESCE(MAX(pk), 0) + 1`.
- `select(table: Table, columns: tuple) -> tuple | None`: Single-row select matching non-None filters.
- `view_select(tables: dict, joins: JoinsType, columns: tuple) -> tuple | None`: Joined single-row select.
- `view_select_multiple(tables: dict, joins: JoinsType, columns: tuple) -> list[tuple]`: Joined multi-row select.
- `insert(table: Table, data: tuple[tuple, ...]) -> None`: Batch insert (e.g. 1000 rows/batch).
- `update(table: Table, data: tuple[tuple, ...]) -> None`: Batch upsert (`ON DUPLICATE KEY UPDATE`).
- `commit_tables_parallel(tables_data, max_workers=None) -> None`: Parallel table commit across worker threads.
- `close() -> None`: Closes connections/cursors cleanly.

---

## 6. Critical Invariants for Custom Implementations

1. **Monotonic Sequences**: `next_id` per table must never reuse or decrement assigned IDs within an update cycle.
2. **Buffer Transformations**: On `commit()`, `no_duplicate` tables must reconstruct rows as `(assigned_id, *data_key)` before calling `db.insert()`.
3. **Multiprocessing Isolation**: Re-invoke `start_new_db()` in child workers to ensure separate DB connection sockets.
4. **Tuple Immutability**: All returned and cached rows must be immutable tuples of primitive `SafeDataType`.
5. **Strict Upstream Deduplication**: Existing records in database/cache must be reused without allocating new sequence IDs, and strict `INSERT INTO` must be maintained at the database layer.
