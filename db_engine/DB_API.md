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
  - Auto-reconnect retry loop (up to 3 attempts) with session parameter re-initialization if connection drops. A reconnect made by `ping(reconnect=True)` itself (new `connection_id`) also re-runs `_init_session()`, so the new session keeps `sql_mode` and the disabled checks.
  - Inside `transaction()` it never reconnects: a lost connection raises `ConnectionError`, because the server already rolled the transaction back.
- **`transaction() -> ContextManager[Self]`** *(MariaDB only)*
  - Runs the block's `insert()` / `update()` calls as one transaction (`autocommit` off, one `COMMIT` on exit, `ROLLBACK` on error). Inside it `insert()` / `update()` neither commit per call nor reconnect and retry per chunk, so a table is written completely or not at all and can be retried as a whole.
- **`apply_server_tuning(settings: dict[str, Any]) -> list[str]`** *(MariaDB only)*
  - Raises global server variables with `SET GLOBAL` (needs SUPER; lasts until the server restarts) and returns one note per change or failure. Sizes may carry a K/M/G/T suffix (`parse_server_setting()`); `innodb_buffer_pool_size`, `innodb_log_file_size`, `innodb_io_capacity(_max)` are never lowered, and `innodb_io_capacity_max` goes first. Each value is read back: a server that caps it (e.g. `innodb_buffer_pool_size` beyond `innodb_buffer_pool_size_max`, which MariaDB 10.11 fixes at startup) is reported as `capped at ...` with the restart hint.

### 2.2. Sequence Tracking & Single-Row CRUD
- **`get_next_id(table: Table) -> int`**
  - Queries `COALESCE(MAX(pk), 0) + 1` on table primary key. Returns 1 if table is empty.
- **`select(table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - Executes single-row query matching non-None column filters (`SELECT * FROM table WHERE ... LIMIT 1`). None positions act as wildcards.
- **`select_preload(table: Table, cached_columns: tuple[int, ...] | None = None, min_vid: int | None = None) -> list[tuple[SafeDataType, ...]]`**
  - Queries records for TableEngine startup preloading with column projection (e.g. `SELECT hash FROM m_tag_code`) and version filtering on the table's version column (`WHERE vid >= min_vid`, `WHERE (vid_e = 0 OR vid_e >= min_vid)`, or `WHERE commit_id >= min_vid` for `m_bridge_checkpoint`). `min_vid` is a commit id (`gp.Old_VID`), so tables without a version column are never filtered (blob ids are a different id space).

- **`select_in(table: Table, col_idx: int, values: Sequence[SafeDataType]) -> list[tuple[SafeDataType, ...]]`**
  - Every row whose column `col_idx` is one of `values` (`MariaDB`: `IN` queries of 1000 values; `MockDB`: filter; base default: one `view_select_multiple()` per value). Used to load the stored symbol items of a file's previous tags.

### 2.3. Multi-Table Relational Views
- **`view_select(tables: Sequence[Table] | dict[int, Table], joins: JoinsType, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - Executes joined single-row query across relational join graph (`JoinsType`) matching non-None column filters.
- **`view_select_multiple(tables: Sequence[Table] | dict[int, Table], joins: JoinsType, columns: tuple[SafeDataType, ...]) -> list[tuple[SafeDataType, ...]]`**
  - Executes joined multi-row query across relational join graph (`JoinsType`) and fetches all matching rows.
  - **Large Graph Chunking**: Graphs exceeding 50 tables are chunked into slices of <= 30 tables per step.

### 2.4. Batch Insert, Upsert & Parallel Commits
- **`insert(table: Table, data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]) -> None`**
  - Batch executes parameterized `INSERT INTO table VALUES (%s, ...)`. Dynamically sizes batches based on table schema: tables with `TEXT`, `LONGTEXT`, or `BLOB` columns (e.g. `m_tag_code`) are capped at <= 500 rows and <= 4MB estimated payload, while regular tables use <= 2000 rows (or <= 5000 rows for short tables) and <= 8MB. On `OperationalError 1153` (`ER_NET_PACKET_TOO_LARGE` / `max_allowed_packet`), recursively bisects chunks into halves until execution succeeds. On duplicate primary key collisions (MySQL `errno 1062` / `IntegrityError` in `MariaDB`, or existing PK in `MockDB`), raises `ValueError` to preserve driver contract. Tables with `idempotent_pk=True` skip only the rows whose key already exists: `MariaDB` appends a no-op `ON DUPLICATE KEY UPDATE pk = pk` (`_insert_sql()`), since a plain multi-row INSERT fails as a whole on one duplicate and `INSERT IGNORE` is no longer batched by mysql-connector (one round trip per row); `MockDB` skips existing keys row by row. Commits transaction.
- **`update(table: Table, data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]) -> None`**
  - Batch executes upsert `INSERT INTO table VALUES (...) ON DUPLICATE KEY UPDATE col=VALUES(col)` for all non-primary key columns. Uses dynamic byte-bounded chunking and recursive bisection on packet limit errors. Commits transaction.
- **`commit_tables_parallel(tables_data: Sequence[tuple[Table, Sequence[tuple], Sequence[tuple]]], max_workers: int | None = None, dedicated: bool = False) -> list[TableCommitStat]`**
  - Flushes insert and update payloads across multiple tables concurrently using dedicated worker connection threads (`MariaDB`); a single table is written on the instance's own connection unless `dedicated=True` (set by the TableEngine's background writer thread, which must never touch the owner's connection).
  - Each table is written by its own connection as one `transaction()`. A lost connection rolls that table back and it is written again on a new connection (3 attempts); other errors are not retried.
  - Every table runs to completion; if any failed, the first error is raised with `failed_table_ids` (the tables that were not committed; all others were). `MockDB` writes tables one after the other and names the failing table and the ones after it.
  - Returns one `TableCommitStat(table_name, inserted, updated, seconds)` per written table (exported by `db_engine`).
  - `supports_background_commit` (class attribute, `True` for `MariaDB`) tells the TableEngine that `dedicated=True` keeps the call off the instance's connection, so it may run on a writer thread.

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
- **`verify_relational_integrity(tables: Sequence[Table]) -> dict[str, int]`**
  - Verifies relational foreign key integrity across tables and returns any orphan record counts (empty dictionary if all constraints valid).

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

---

## 4. Subsystem Verification & Regression Suite (`unit_test/test_db_engine.py`)

Comprehensive unit testing and contract verification across all database engines is implemented in `unit_test/test_db_engine.py` using the reusable harness in `unit_test/harness.py`.

Key test capabilities:
- **Prefix Isolation**: Dynamically generates `t_*` test schemas from all tables in `core.DBLayout.TABLES`, rewriting foreign keys to eliminate collision with production `m_*` data.
- **Automated Cleanup & DB Reload**: Drops `t_*` tables at suite start and tearDown, and automatically reloads clean schemas upon test failure.
- **Dual Error Tracing**: On test failure, prints rich diagnostic reports featuring both feature-level error context and full Python tracebacks.
- **Dependency Propagation**: Enforces prerequisites (e.g. `depends_on=["insert"]`), automatically labeling downstream dependent tests as `UNTESTABLE` if prerequisite features fail.
- **Payload & Packet Resilience**: Tests handled recursive chunk bisection under `OperationalError 1153` (`max_allowed_packet`), verifies unhandled exception raising on oversized atomic rows, and exercises multi-megabyte byte-bounded chunking on live `LONGTEXT` columns.

