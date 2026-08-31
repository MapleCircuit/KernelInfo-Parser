# Database Engine (DBEngine) API & Driver Contract Specification

Low-level database persistence and query layer executing SQL statements, connection pooling, batch inserts/upserts, parallel table commits, and DDL schema/index management for `TableEngine` and `ChangeSet`.

---

## 1. Architectural Architecture & Interface Overview

Any database backend assigned to `G.DB` or passed to `TableEngine.start()` must implement the abstract contract defined in [`BaseDBEngine`](db_engine/base.py).

### Core Drivers

| Driver | File | Description |
| :--- | :--- | :--- |
| `MariaDB` | [`DBHandling.py`](db_engine/DBHandling.py) | High-performance MySQL / MariaDB direct driver utilizing parameterized SQL statements, batch chunking, socket reconnect guards, and parallel table commit workers. |
| `MockDB` | [`mock_db.py`](db_engine/mock_db.py) | In-memory mock database engine with relational join evaluation for isolated, ultra-fast unit testing without an active MySQL daemon. |


---

## 2. Core Method Specifications

### 2.1. Lifecycle, Session & Health
- **`__init__() -> None`**
  - Connects to database socket and configures session variables (`sql_mode = 'NO_AUTO_VALUE_ON_ZERO'`, `max_allowed_packet = 1073741824`).
- **`__enter__() -> Self` & `__exit__(...) -> None`**
  - Context manager lifecycle; rolls back pending transaction on unhandled exception.
- **`close() -> None`**
  - Safely releases cursor and database connection socket handles.
- **`check_if_connected() -> None`** *(MariaDB only)*
  - Auto-reconnect retry loop (up to 3 attempts) with session parameter re-initialization if connection drops.

### 2.2. Sequence Tracking & Single-Row CRUD
- **`get_next_id(table: Table) -> int`**
  - Queries `COALESCE(MAX(pk), 0) + 1` on table primary key. Returns 1 if table is empty.
- **`select(table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - Executes single-row query matching non-None column filters (`SELECT * FROM table WHERE ... LIMIT 1`). None positions act as wildcards.

### 2.3. Multi-Table Relational Views
- **`view_select(tables: Sequence[Table] | dict[int, Table], joins: JoinsType, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - Executes joined single-row query across relational join graph (`JoinsType`) matching non-None column filters.
- **`view_select_multiple(tables: Sequence[Table] | dict[int, Table], joins: JoinsType, columns: tuple[SafeDataType, ...]) -> list[tuple[SafeDataType, ...]]`**
  - Executes joined multi-row query across relational join graph (`JoinsType`) and fetches all matching rows.
  - **Large Graph Chunking**: Graphs exceeding 50 tables are chunked into slices of <= 30 tables per step.

### 2.4. Batch Insert, Upsert & Parallel Commits
- **`insert(table: Table, data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]) -> None`**
  - Batch executes parameterized `INSERT INTO table VALUES (%s, ...)`. Chunks batches into 1000-row slices and commits.
- **`update(table: Table, data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]) -> None`**
  - Batch executes upsert `INSERT INTO table VALUES (...) ON DUPLICATE KEY UPDATE col=VALUES(col)` for all non-primary key columns.
- **`commit_tables_parallel(tables_data: Sequence[tuple[Table, Sequence[tuple], Sequence[tuple]]], max_workers: int | None = None) -> None`**
  - Flushes insert and update payloads across multiple tables concurrently using dedicated worker connection threads.

### 2.5. DDL & Index Management
- **`create_table(tables: Sequence[Table] | Table) -> None`**
  - Generates DDL from `table.init_columns`, `table.init_primary`, `table.init_foreign`, creates tables, inserts seed rows (`table.initial_insert`), and commits.
- **`drop_table(tables: Sequence[Table] | Table) -> None`**
  - Temporarily sets `SET FOREIGN_KEY_CHECKS = 0;`, drops tables via `DROP TABLE IF EXISTS`, and restores `SET FOREIGN_KEY_CHECKS = 1;`.
- **`index_exists(index_name: str, table: Table) -> bool`**
  - Queries `SHOW INDEX FROM table WHERE Key_name = %s`.
- **`create_index(index_name: str, table: Table, rows: tuple[PointerType, ...]) -> None`**
  - Creates single-column or composite index if not already present.
- **`remove_index(index_name: str, table: Table) -> None`**
  - Drops index if present via `ALTER TABLE table DROP INDEX index_name`.
- **`create_indexes(indexes: Sequence[tuple[str, Table, tuple[PointerType, ...]]], max_workers: int | None = None) -> None`**
  - Creates multiple database indexes in parallel across worker connections.
- **`remove_indexes(indexes: Sequence[tuple[str, Table]], max_workers: int | None = None) -> None`**
  - Removes multiple database indexes in parallel across worker connections.
- **`test_tables(tables: Sequence[Table] | Table) -> list[str] | None`**
  - Checks if all registered schema tables exist in the database catalog; returns list of missing table names or `None`.

---

## 3. Factory Resolver (`get_db_engine`)

```python
from db_engine import get_db_engine

EngineClass = get_db_engine("mariadb")  # -> MariaDB (default)
EngineClass = get_db_engine("mock")     # -> MockDB
```

| Engine Alias | Resolved Class | Description |
| :--- | :--- | :--- |
| `"mariadb"`, `"mysql"`, `None` | `MariaDB` | Production MySQL / MariaDB direct driver |
| `"mock"`, `"mockdb"`, `"inmemory"` | `MockDB` | In-memory mock database driver |
