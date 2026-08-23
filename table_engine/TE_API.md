# TableEngine (TE) API & Architectural Contract Specification

Stateful caching, sequence coordination, relational view decomposition, and batching layer between `TableHandling.py` (ChangeSets) and `DBHandling.py` (SQL/Storage driver).
---

## 1. Core Data Types & Schemas

- **`SafeDataType`**: `int | str | None` &mdash; Primitive scalar values (Enums/IntEnums are converted to native `int`/`str`).
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
  - `te_cached: bool` &mdash; If `True`, enables in-memory preloading and multi-indexing in `TECachedDB`.
  - `hashing_table: bool | str` &mdash; If set (e.g. `"m_ast_hash"`), enables automatic structural hash deduplication and acceleration for views rooted at this table.

---

## 2. Internal State Architecture

| Attribute | Type | Purpose & Structure |
| :--- | :--- | :--- |
| `tables` | `dict[int, Table]` | `table_id -> Table` schema registry. |
| `queued_set` | `dict[int, dict[Any, Any]]` | Staged inserts per table:<br>• `no_duplicate=False`: `{pk_or_assigned_id: (assigned_id, col1, col2, ...)}`<br>• `no_duplicate=True`: `{(col1, col2, ...): assigned_id}` |
| `queued_update` | `dict[int, list[tuple]]` | Staged updates per table: `[row_tuple, ...]`. |
| `queued_view` | `dict[JoinsType, dict]` | Cache for views: `{joins: {non_none_column_tuple: assigned_view_id}}`. |
| `next_id` | `dict[int, int]` | Monotonic auto-increment counter: `table_id -> current_int_id`. |
| `db` | `Any \| None` | Active database driver handle (or `None`). |

---

## 3. Exhaustive API Method Specifications

### 3.1. Lifecycle & Connection Management

- **`__init__() -> None`**
  - Initializes empty dictionaries for `tables`, `queued_set`, `queued_update`, `queued_view`, `next_id`, and sets `db = None`.
- **`start_new_db(db: Callable[[], Any] | type[Any]) -> None`**
  1. Safely closes active `self.db` (if present) and instantiates a new driver: `self.db = db()`.
  2. Resets `queued_view = {}`.
  3. For all registered `self.tables`: resets `queued_set[t_id] = {}`, `queued_update[t_id] = []`, and refreshes `next_id[t_id] = self.db.get_next_id(t)`.
  4. (In `TECachedDB`): Re-initializes `_cached_rows`, `_pk_index`, `_nodup_index`, and `_col_indices`, and preloads all records from the database for tables with `te_cached=True`.
- **`start(tables: Sequence[Table] | Table, db: Callable[[], Any] | type[Any]) -> None`**
  1. Normalizes `tables` into a tuple.
  2. Registers all tables in `self.tables`.
  3. Calls `self.start_new_db(db)`.
- **`close() -> None`**
  - Calls `self.db.close()` if available (ignoring exceptions) and sets `self.db = None`.
- **Context Manager**: `__enter__() -> Self`, `__exit__(...) -> close()`, `__del__() -> close()`.

---

### 3.2. Single-Table Operations (`get`, `set`, `update`)

- **`get(table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - **Emptiness Guard**: If `table.initial_insert is None and self.next_id[table_id] <= 1`, returns `None` immediately without querying DB.
  - **Query**: Returns `self.db.select(table, columns)` where `None` positions act as wildcards. (In `TECachedDB`, resolves in-memory via indices for `te_cached=True` tables).
- **`set(table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]`**
  - **Case 1 (`table.no_duplicate == True`)**:
    - Key: `key = columns[1:]`.
    - If `key` in `queued_set[table_id]`: returns `(queued_set[table_id][key], *columns[1:])`.
    - Else: assigns `assigned_id = next_id[table_id]`, increments `next_id[table_id] += 1`, stores `queued_set[table_id][key] = assigned_id`, returns `(assigned_id, *columns[1:])`.
  - **Case 2 (`columns[0] is None` - Auto-Increment Generation)**:
    - Assigns `assigned_id = next_id[table_id]`, increments `next_id[table_id] += 1`.
    - Builds `row = (assigned_id, *columns[1:])`.
    - Stores `queued_set[table_id][assigned_id] = row`. Returns `row`.
  - **Case 3 (Explicit Primary Key Provided)**:
    - Extracts `pk = itemgetter(*table.primary)(columns)`.
    - Stores `queued_set[table_id][pk] = columns`. Returns `columns`.
- **`update(table_id: int, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]`**
  - Appends `columns` to `self.queued_update[table_id]`. Returns `columns`.

---

### 3.3. Multi-Table Relational Views (`view_get`, `view_get_multiple`, `view_set`)

- **`view_get(joins: JoinsType, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - `init_id = PointerGetter(joins).get_first_table_id()`.
  - If `tables[init_id].initial_insert is None and next_id[init_id] <= 1`: returns `None`.
  - **Schema-Driven Hash Fast-Path**: If `tables[init_id]` has `hashing_table` configured and registered:
    1. Computes SHA-256 hash `h = compute_ast_hash(joins, filtered_columns)`.
    2. Queries `hash_table` (or checks staged buffer/in-memory cache) for `(h, None)`.
    3. If match found with `ast_id`, returns `tuple(val if val is not None else ast_id for val in columns)`.
    4. If not found and `hash_table.te_cached is True`: returns `None` immediately (zero SQL queries).
  - **Fallback**: Returns `self.db.view_select(self.tables, joins, columns)`.
- **`view_get_multiple(joins: JoinsType, columns: tuple[SafeDataType, ...]) -> list[tuple[SafeDataType, ...]]`**
  - Returns `self.db.view_select_multiple(self.tables, joins, columns)`.
- **`view_set(joins: JoinsType, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...]`**
  1. Extracts non-None values: `filtered = tuple(x for x in columns if x is not None)`.
  2. If `joins` in `queued_view` and `filtered` in `queued_view[joins]`:
     - Returns `tuple(x if x is not None else queued_view[joins][filtered] for x in columns)`.
  3. **Schema-Driven Hash Fast-Path & Staging**: If `tables[main_id]` has `hashing_table` configured and registered:
     - Computes SHA-256 hash `h = compute_ast_hash(joins, filtered)`.
     - Checks staged `queued_set[hash_table]` or queries `self.get(hash_table.table_id, (h, None))`.
     - If match exists: returns `columns` with None positions replaced by existing `ast_id`.
     - If new:
       - Assigns `current_view_id = next_id[main_id]`; increments `next_id[main_id] += 1`.
       - Stages `(h, current_view_id)` into `queued_set[hash_table.table_id]`.
       - Decomposes `result` across constituent tables.
       - Returns `result`.
  4. Else (Non-Hashed Views):
     - `main_id = PointerGetter(joins).get_first_table_id()`.
     - `current_view_id = next_id[main_id]`; increments `next_id[main_id] += 1`.
     - `queued_view.setdefault(joins, {})[filtered] = current_view_id`.
     - `result = tuple(x if x is not None else current_view_id for x in columns)`.
     - **Decomposition**: Slices `result` sequentially using `PointerGetter(joins)`:
       ```python
       data_offset = 0
       for repeat, pointer in PointerGetter(joins):
           target_table = self.tables[pointer[0]]
           t_len = target_table.length
           for _ in range(repeat):
               row = result[data_offset : data_offset + t_len]
               pk = itemgetter(*target_table.primary)(row)
               self.queued_set[pointer[0]][pk] = row
               data_offset += t_len
       ```
     - Returns `result`.

---

### 3.4. Transaction Commit Protocol (`commit`)

- **`commit(table_id: int) -> None`**
  1. **Inserts (`queued_set`)**: If `queued_set[table_id]` is non-empty:
     - `no_duplicate=True`: `payload = tuple((item[1], *item[0]) for item in queued_set[table_id].items())`.
     - `no_duplicate=False`: `payload = tuple(queued_set[table_id].values())`.
     - Calls `self.db.insert(table, payload)` and clears `self.queued_set[table_id]`.
  2. **Updates (`queued_update`)**: If `queued_update[table_id]` is non-empty:
     - Calls `self.db.update(table, tuple(queued_update[table_id]))` and clears `self.queued_update[table_id]`.

- **`commit_all(max_workers: int | None = None) -> None`**
  - Gathers payloads `(table, insert_payload, update_payload)` across all modified tables.
  - Dispatches concurrently to `self.db.commit_tables_parallel(tables_data, max_workers=max_workers)`.
  - Clears all queued buffers in one pass.

---

## 4. Expected Database Driver Interface (`G.DB`)

Any backend passed to `TableEngine` must implement:
- `get_next_id(table: Table) -> int`: Queries `COALESCE(MAX(pk), 0) + 1`.
- `select(table: Table, columns: tuple) -> tuple | None`: Single-row select matching non-None filters.
- `view_select(tables: dict, joins: JoinsType, columns: tuple) -> tuple | None`: Joined single-row select.
- `view_select_multiple(tables: dict, joins: JoinsType, columns: tuple) -> list[tuple]`: Joined multi-row select.
- `insert(table: Table, data: tuple[tuple, ...]) -> None`: Batch insert (e.g. 1000 rows/batch).
- `update(table: Table, data: tuple[tuple, ...]) -> None`: Batch upsert (`ON DUPLICATE KEY UPDATE`).
- `close() -> None`: Closes connections/cursors cleanly.

---

## 5. Critical Invariants for Custom Implementations

1. **Monotonic Sequences**: `next_id` per table must never reuse or decrement assigned IDs.
2. **Buffer Transformations**: On `commit()`, `no_duplicate` tables must reconstruct rows as `(assigned_id, *data_key)` before calling `db.insert()`.
3. **Multiprocessing Isolation**: Re-invoke `start_new_db()` in child workers to ensure separate DB connection sockets.
4. **Tuple Immutability**: All returned and cached rows must be immutable tuples of primitive `SafeDataType`.

---

## 6. TableEngine Variants & Factory Resolver

- **`TEDirectDB` (`table_engine/te_direct_db.py`)**: Direct passthrough engine with local deduplication staging, monotonic sequence assignment, `m_ast_hash` acceleration, and high-throughput batch execution.
- **`TECachedDB` (`table_engine/te_cached_db.py`)** *(Default)*: Extends `TEDirectDB` with in-memory preloading, multi-index acceleration, and zero-latency single-table query resolution:
  - **Selective Hot-Table Preloading**: At `start()`, all database rows for registered tables with `table.te_cached = True` are preloaded via `db.view_select_multiple()` into `_cached_rows`.
  - **Multi-Index In-Memory Architecture**:
    - `_pk_index[table_id][pk] -> row`: O(1) primary key exact row lookup.
    - `_nodup_index[table_id][columns[1:]] -> assigned_id`: O(1) deduplication key lookup for `no_duplicate=True` tables.
    - `_col_indices[table_id][col_idx][val] -> [row, ...]`: Inverted column indices for fast partial filter matching.
  - **In-Memory Query Fast-Path**: `get(table_id, columns)` for cached tables is resolved entirely in memory across primary key, deduplication, and column indices without issuing SQL queries to the database.
  - **Real-Time Synchronization**: Staged `set()`, `update()`, and `view_set()` operations instantly update `_cached_rows` and internal indices.
  - **Lifecycle Cleanup**: Explicit `clear_cache()` and `close()` support to release memory resources cleanly.

### 6.1. Factory Resolver (`get_table_engine`)

The `table_engine` package provides a dynamic resolver `get_table_engine(name)`:
```python
from table_engine import get_table_engine

# Resolved from CLI option (--te cached | --te direct)
EngineClass = get_table_engine("cached")  # -> TECachedDB
G.TE = EngineClass()
```

| Engine Alias | Resolved Class | Description |
| :--- | :--- | :--- |
| `"cached"`, `"tecacheddb"` | `TECachedDB` | In-memory cached table engine (default) |
| `"direct"`, `"tedirectdb"` | `TEDirectDB` | Direct database passthrough table engine |


