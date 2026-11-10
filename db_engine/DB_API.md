# Database Engine (DBEngine) API & Driver Contract Specification

Low-level database persistence and query layer executing SQL statements, connection pooling, batch
inserts/upserts, parallel table commits, declarative query specifications, and DDL schema/view management for `TableEngine`, `ChangeSet`, and `webapp`.

## Table of Contents
- **§1**: Interface Overview & Core Drivers [L14-L40]
- **§2**: Core Method Specifications [L41-L260]
- **§3**: Factory Resolver (`get_db_engine`) [L261-L280]
- **§4**: Verification & Regression Suite [L281-L310]

---

## 1. Architectural Architecture & Interface Overview

Any database backend assigned to `G.DB` or passed to `TableEngine.start()` must implement the abstract
contract defined in [`BaseDBEngine`](db_engine/base.py).

### Encapsulation & Query Authority Contract

`db_engine` is the single authoritative subsystem for interacting with the database across the entire project. All SQL execution, DDL/schema migrations, view declarations, transactions, batch operations, connection pooling, and sequence coordination must reside inside `db_engine/` and adhere to `BaseDBEngine`.

Direct driver imports (`mysql.connector`, `pymysql`, `sqlite3`), raw connection/cursor handles (`cnx`, `db.cnx`, `cnx.cursor()`), and raw query execution (`cur.execute()`, `cursor.execute()`) outside `db_engine/` are strictly prohibited across all callers (`main.py`, `core/`, `parser/`, `webapp/`). Any specialized query capability or access pattern must be exposed as a formal method on `BaseDBEngine` (and implemented in `MariaDB`, `MockDB`, and `SQLiteStore` where appropriate).

### Core Drivers & Subsystem Components

| Component | File | Description |
| :--- | :--- | :--- |
| `MariaDB` | [`DBHandling.py`](db_engine/DBHandling.py) | High-performance MySQL / MariaDB direct driver utilizing parameterized SQL statements, batch chunking, socket reconnect guards, parallel table commit workers, and declarative spec execution. |
| `MockDB` | [`mock_db.py`](db_engine/mock_db.py) | In-memory mock database engine with relational join evaluation, snapshot rollback transactions, and spec query interpretation for isolated, ultra-fast unit testing without an active MySQL daemon. |
| `SQLiteStore` | [`sqlite_store.py`](db_engine/sqlite_store.py) | Embedded SQLite store with WAL mode, thread-safe connection caching, schema reflection, key-value metadata store, declarative spec reads, and batch inserts. |
| `Declarative Specs` | [`spec.py`](db_engine/spec.py) | Pure data types and condition helpers (`Cond`, `Join`, `Agg`, `Const`, `Select`, `AnyOf`, `Exists`, `InSelect`, etc.) enabling typed, SQL-free query formulation. |
| `SQL Renderer` | [`sql_render.py`](db_engine/sql_render.py) | Multi-dialect SQL compiler rendering declarative query specs to parameterized SQL for `mariadb` and `sqlite`. |
| `View Registry` | [`views.py`](db_engine/views.py) | Canonical dependency-ordered view definitions registry (`VIEW_DEFINITIONS`, `ORDERED_VIEW_NAMES`, `extract_select_sql`). |
| `ReadSession` | [`pool.py`](db_engine/pool.py) | Read-only connection wrapper (`ReadAPI`) providing dictionary-oriented spec reads and domain intent methods for `webapp/`. |


---

## 2. Core Method Specifications

### 2.1. Lifecycle, Session & Health
- **`__init__() -> None`**
  - Connects to database via Unix domain socket (`DB_SOCKET` / `MYSQL_UNIX_PORT`, e.g.
    `/run/mysqld/mysqld.sock`) when local and non-tunneled, or falls back to TCP socket (`DB_HOST`,
    `DB_PORT`). Configures session variables (`sql_mode = 'NO_AUTO_VALUE_ON_ZERO'`, `max_allowed_packet =
    1073741824`).
- **`__enter__() -> Self` & `__exit__(...) -> None`**
  - Context manager lifecycle; rolls back pending transaction on unhandled exception.
- **`close() -> None`**
  - Safely releases cursor and database connection socket handles.
- **`check_if_connected() -> None`** *(MariaDB only)*
  - Auto-reconnect retry loop (up to 3 attempts) with session parameter re-initialization if connection drops.
    A reconnect made by `ping(reconnect=True)` itself (new `connection_id`) also re-runs `_init_session()`, so
    the new session keeps `sql_mode` and the disabled checks.
  - Inside `transaction()` it never reconnects: a lost connection raises `ConnectionError`, because the server
    already rolled the transaction back.
- **`transaction() -> ContextManager[Self]`** *(MariaDB only)*
  - Runs the block's `insert()` / `update()` calls as one transaction (`autocommit` off, one `COMMIT` on exit,
    `ROLLBACK` on error). Inside it `insert()` / `update()` neither commit per call nor reconnect and retry
    per chunk, so a table is written completely or not at all and can be retried as a whole.
- **`apply_server_tuning(settings: dict[str, Any]) -> list[str]`** *(MariaDB only)*
  - Raises global server variables with `SET GLOBAL` (needs SUPER; lasts until the server restarts) and
    returns one note per change or failure. Sizes may carry a K/M/G/T suffix (`parse_server_setting()`);
    `innodb_buffer_pool_size`, `innodb_log_file_size`, `innodb_io_capacity(_max)` are never lowered, and
    `innodb_io_capacity_max` goes first. Each value is read back: a server that caps it (e.g.
    `innodb_buffer_pool_size` beyond `innodb_buffer_pool_size_max`, which MariaDB 10.11 fixes at startup) is
    reported as `capped at ...` with the restart hint. If the user lacks the privilege (`errno 1227`, e.g. a
    remote server reached over the SSH tunnel), the remaining variables are not tried and a single `server
    tuning skipped: ...` note is returned, which `main.py` logs at INFO level instead of one warning per
    variable.

### 2.2. Sequence Tracking & Single-Row CRUD
- **`get_next_id(table: Table) -> int`**
  - Queries `COALESCE(MAX(pk), 0) + 1` on table primary key. Returns 1 if table is empty.
- **`reserve_id_block(table: Table, block_size: int, commit_id: int = 0) -> tuple[int, int]`**
  - Atomically reserves a block of primary key IDs `[start_id, end_id]` via `m_table_sequence` (ensuring sequence row presence via upsert before `SELECT FOR UPDATE` locking) for distributed
    multi-computer coordination and logs the range in `m_ingest_id_range`. Falls back to `get_next_id()` when
    sequence table or multi-computer mode is unavailable.
- **`select(table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None`**
  - Executes single-row query matching non-None column filters (`SELECT * FROM table WHERE ... LIMIT 1`). None
    positions act as wildcards.
- **`select_preload(table: Table, cached_columns: tuple[int, ...] | None = None, min_vid: int | None = None)
  -> list[tuple[SafeDataType, ...]]`**
  - Queries records for TableEngine startup preloading with column projection (e.g. `SELECT hash, ast_id FROM
    m_ast_hash`) and version filtering on the table's version column (`WHERE vid >= min_vid`, `WHERE (vid_e =
    0 OR vid_e >= min_vid)`, or `WHERE commit_id >= min_vid` for `m_bridge_checkpoint`). `min_vid` is a commit
    id (`gp.Old_VID`), so tables without a version column are never filtered (blob ids are a different id
    space).

- **`select_in(table: Table, col_idx: int, values: Sequence[SafeDataType]) -> list[tuple[SafeDataType,
  ...]]`**
  - Every row whose column `col_idx` is one of `values` (`MariaDB`: `IN` queries of 1000 values; `MockDB`:
    filter; base default: one `view_select_multiple()` per value). Used to load the stored symbol items of a
    file's previous tags.

- **`table_size(table: Table, cached_columns: tuple[int, ...] | None = None, min_vid: int | None = None) ->
  int`**
  - Size of the table in bytes. `MariaDB` reads `DATA_LENGTH + INDEX_LENGTH` and `TABLE_ROWS` from
    `information_schema.TABLES` (InnoDB statistics, so an estimate) and returns 0 for a table that is not in
    the catalog; `MockDB` measures the stored values.
  - With `cached_columns` narrower than the table, returns the share those columns are estimated to take
    (`estimate_column_bytes()` per row, capped by the table's own size).
  - With `min_vid`, returns the size of the rows `select_preload()` would return for it, so a version-scoped
    table is sized by its working window and not by its whole history. `MariaDB` counts that window with the
    same version predicate `select_preload()` uses (`_version_predicate()`, an index range scan over the rows
    the caller is about to read anyway) and scales the catalog size to it; the table's bytes and total row
    count are never scanned.
  - The base implementation returns 0, meaning the size is unknown: `TECachedDB.cache_max_bytes` then caches
    the table as configured.
  - Used by `TECachedDB` to leave a cached table above `cache_max_bytes` (`parser.cache_max_table_mb`) out of
    the cache (`table_engine/TE_API.md` §2.2).

### 2.3. Multi-Table Relational Views
- **`view_select(tables: Sequence[Table] | dict[int, Table], joins: JoinsType, columns: tuple[SafeDataType,
  ...]) -> tuple[SafeDataType, ...] | None`**
  - Executes joined single-row query across relational join graph (`JoinsType`) matching non-None column
    filters.
- **`view_select_multiple(tables: Sequence[Table] | dict[int, Table], joins: JoinsType, columns:
  tuple[SafeDataType, ...]) -> list[tuple[SafeDataType, ...]]`**
  - Executes joined multi-row query across relational join graph (`JoinsType`) and fetches all matching rows.
  - **Large Graph Chunking**: Graphs exceeding 50 tables are chunked into slices of <= 30 tables per step.

### 2.4. Batch Insert, Upsert & Parallel Commits
- **`insert(table: Table, data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]) -> None`**
  - Batch executes parameterized `INSERT INTO table VALUES (%s, ...)`. Dynamically sizes batches based on
    table schema: tables with `TEXT`, `LONGTEXT`, or `BLOB` columns (e.g. `m_ast_debug`) are capped at <= 500
    rows and <= 4MB estimated payload, while regular tables use <= 2000 rows (or <= 5000 rows for short
    tables) and <= 8MB. On `OperationalError 1153` (`ER_NET_PACKET_TOO_LARGE` / `max_allowed_packet`),
    recursively bisects chunks into halves until execution succeeds. On duplicate primary key collisions
    (MySQL `errno 1062` / `IntegrityError` in `MariaDB`, or existing PK in `MockDB`), raises `ValueError` to
    preserve driver contract. Tables with `idempotent_pk=True` skip only the rows whose key already exists:
    `MariaDB` appends a no-op `ON DUPLICATE KEY UPDATE pk = pk` (`_insert_sql()`), since a plain multi-row
    INSERT fails as a whole on one duplicate and `INSERT IGNORE` is no longer batched by mysql-connector (one
    round trip per row); `MockDB` skips existing keys row by row. Commits transaction.
- **`update(table: Table, data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]) -> None`**
  - Batch executes upsert `INSERT INTO table VALUES (...) ON DUPLICATE KEY UPDATE col=VALUES(col)` for all
    non-primary key columns. Uses dynamic byte-bounded chunking and recursive bisection on packet limit
    errors. Commits transaction.
- **`commit_tables_parallel(tables_data: Sequence[tuple[Table, Sequence[tuple], Sequence[tuple]]],
  max_workers: int | None = None, dedicated: bool = False) -> list[TableCommitStat]`**
  - Flushes insert and update payloads across multiple tables concurrently using dedicated worker connection
    threads (`MariaDB`); a single table is written on the instance's own connection unless `dedicated=True`
    (set by the TableEngine's background writer thread, which must never touch the owner's connection).
  - Each table is written by its own connection as one `transaction()`. A lost connection rolls that table
    back and it is written again on a new connection (3 attempts); other errors are not retried.
  - Every table runs to completion; if any failed, the first error is raised with `failed_table_ids` (the
    tables that were not committed; all others were). `MockDB` writes tables one after the other and names the
    failing table and the ones after it.
  - Returns one `TableCommitStat(table_name, inserted, updated, seconds)` per written table (exported by
    `db_engine`).
  - `supports_background_commit` (class attribute, `True` for `MariaDB`) tells the TableEngine that
    `dedicated=True` keeps the call off the instance's connection, so it may run on a writer thread.

### 2.5. DDL & Index Management
- **`create_table(tables: Sequence[Table] | Table) -> None`**
  - Generates DDL from `table.init_columns`, `table.init_primary`, `table.init_foreign`, creates tables,
    inserts seed rows (`table.initial_insert`), and commits.
- **`drop_table(tables: Sequence[Table] | Table) -> None`**
  - Temporarily sets `SET FOREIGN_KEY_CHECKS = 0;`, drops tables via `DROP TABLE IF EXISTS`, and restores `SET
    FOREIGN_KEY_CHECKS = 1;`.
- **`create_view(view_name: str, select_sql: str, replace: bool = True) -> None`**
  - Creates or replaces a database view using `select_sql`.
- **`drop_view(view_name: str, if_exists: bool = True) -> None`**
  - Drops a database view if present.
- **`add_column_if_missing(table_name: str, column_name: str, column_def: str) -> bool`**
  - Safely checks database catalog and executes `ALTER TABLE <table_name> ADD COLUMN <column_def>` if missing.
- **`index_exists(index_name: str, table: Table) -> bool`**
  - Queries `SHOW INDEX FROM table WHERE Key_name = %s`.
- **`create_index(index_name: str, table: Table, rows: tuple[PointerType, ...]) -> None`**
  - Creates single-column or composite index if not already present.
- **`remove_index(index_name: str, table: Table) -> None`**
  - Drops index if present via `ALTER TABLE table DROP INDEX index_name`.
- **`create_indexes(indexes: Sequence[tuple[str, Table, tuple[PointerType, ...]]], max_workers: int | None =
  None) -> None`**
  - Creates multiple database indexes in parallel across worker connections.
- **`remove_indexes(indexes: Sequence[tuple[str, Table]], max_workers: int | None = None) -> None`**
  - Removes multiple database indexes in parallel across worker connections.
- **`test_tables(tables: Sequence[Table] | Table) -> list[str] | None`**
  - Checks if all registered schema tables exist in the database catalog; returns list of missing table names
    or `None`.
- **`verify_relational_integrity(tables: Sequence[Table]) -> dict[str, int]`**
  - Verifies relational foreign key integrity across tables and returns any orphan record counts (empty
    dictionary if all constraints valid).

### 2.6. Declarative Query Specification Primitives (`db_engine/spec.py`)
Declarative AST types allowing callers across all subsystems to construct rich relational queries, filters, joins, and aggregates without writing raw SQL strings.

- **Data Types**:
  - `Source`: Table schema object, table/view name, or `(Table | name, alias)` tuple.
  - `Cond(op, value, value2)`: Parameterized operator term.
  - `Col(name)`: Explicit column reference (e.g. for compare-and-set or join terms).
  - `Value(v)`: Explicit literal scalar parameter.
  - `Inc(n)`: Atomic increment / decrement expression (`col = col + n`).
  - `Join(source, on, kind="inner"|"left", joins=())`: Relational join clause with nested join support.
  - `Agg(func, column="*", distinct=False)`: Aggregate function (`COUNT`, `SUM`, `MIN`, `MAX`, `AVG`).
  - `Const(value)`: Constant literal column projection.
  - `Select(source, columns, where=None, joins=(), distinct=False, order_by=(), limit=None, offset=None)`: Pure select specification.
  - `AnyOf(*branches)`: Disjunction (`OR`) combinator across condition branches.
  - `Exists(source, on, where=None, joins=(), negate=False)`: Correlated subquery existence test (`EXISTS` / `NOT EXISTS`).
  - `InSelect(columns, source, select, where=None, joins=(), negate=False)`: Set membership subquery (`col IN (SELECT ...)` or tuple `(a, b) IN (SELECT a, b ...)`).
- **Condition Helpers**:
  - `ne(v)`, `lt(v)`, `le(v)`, `gt(v)`, `ge(v)`: Standard scalar comparisons (`<>`, `<`, `<=`, `>`, `>=`).
  - `between(lo, hi)`: Range inclusion (`BETWEEN lo AND hi`).
  - `not_in(seq)`: Negated set membership (`NOT IN (...)`).
  - `is_null()`, `not_null()`: Nullity predicates (`IS NULL`, `IS NOT NULL`).
  - `like(text, mode="prefix"|"suffix"|"contains"|"exact", ci=False)`: Pattern matching with automatic wildcard escaping and dialect-specific `ESCAPE` clauses.
  - `ieq(v)`: Case-insensitive equality (`LOWER(col) = LOWER(%s)`).
  - `hash_prefix(prefix_bytes)`: Binary hash prefix range scan (`col >= lo AND col < hi`).
  - `len_lt(n)`: Column string length check (`LENGTH(col) < n`).
  - `finished_ref(alias="r")`: Subquery checking absence of active `INGEST_PARSING` tasks.
- **Query Optimization Helpers**:
  - `has_empty_in_term(where)`: Introspects where terms; short-circuits empty queries to `[]` without network round trips.
  - `find_oversize_in_term(where, chunk_size=1000)`: Detects IN clauses exceeding threshold for automatic batch chunking.
  - `replace_in_term(where, term_idx, target_col, sub_list)`: Clones query specification substituting chunked IN terms.
  - `dedup_rows(rows, as_dict)`: Preserves distinct result order across chunked multi-pass queries.

### 2.7. Spec Read Primitives
Common query primitives implemented across `BaseDBEngine`, `MariaDB`, `MockDB`, `SQLiteStore`, and `ReadSession`.

- **`select_rows(source, columns, where=None, *, joins=(), distinct=False, order_by=(), limit=None, offset=None, as_dict=None) -> list[tuple | dict]`**
  - Executes declarative select query. Honors auto-chunking for oversize IN lists (> 1000 items in MariaDB / 500 in SQLite) and returns empty lists immediately for empty IN clauses. Defaults to tuples on drivers and dicts on `ReadSession`.
- **`iter_rows(source, columns, where=None, *, joins=(), distinct=False, order_by=(), limit=None, offset=None, as_dict=None, chunk_size=2000) -> Iterator[tuple | dict]`**
  - Streaming iterator over query results yielding rows in chunks.
- **`select_one(source, columns, where=None, *, joins=(), order_by=(), as_dict=None) -> tuple | dict | None`**
  - Fetches the first matching row or returns `None`.
- **`select_values(source, column, where=None, *, joins=(), distinct=False, order_by=(), limit=None, offset=None) -> list[Any]`**
  - Returns a flat list of scalar values for a single projected column.
- **`select_value(source, column, where=None, default=None, *, joins=(), order_by=()) -> Any`**
  - Returns a single scalar value from the first matching row, or `default` if no rows match.
- **`count_rows(source, where=None, *, joins=()) -> int`**
  - Executes `COUNT(*)` query matching specification filters.
- **`row_exists(source, where=None, *, joins=()) -> bool`**
  - Executes optimized `SELECT 1 ... LIMIT 1` existence probe.
- **`min_value(source, column, where=None, *, joins=()) -> Any`**
  - Computes `MIN(column)` over filtered relation.
- **`max_value(source, column, where=None, *, joins=()) -> Any`**
  - Computes `MAX(column)` over filtered relation.
- **`aggregate(source, aggs, where=None, *, joins=(), group_by=(), as_dict=None) -> list[tuple | dict]`**
  - Computes dictionary of aggregate expressions (`Agg`) grouped by `group_by`.
- **`resolve_relation_name(relation: Source) -> str`**
  - Canonical identifier resolution stripping aliases or prefix qualifications.

### 2.8. Spec Write Primitives
Typed write methods eliminating raw caller SQL during data modification and task queue coordination.

- **`insert_rows(table, columns, rows, *, on_conflict="error"|"ignore"|"update"|"replace") -> int`**
  - Inserts batch of rows. Resolves conflict semantics across MariaDB and SQLite (`ON DUPLICATE KEY UPDATE` / `ON CONFLICT DO UPDATE`). Under OperationalError 1153 on MariaDB, recursively bisects chunks. Returns total affected rows.
- **`get_or_insert_id(table, match, defaults=None, id_col=None) -> int`**
  - Fetches existing primary key matching filter, or inserts a new row with combined `match` and `defaults` and returns the generated sequence ID.
- **`update_rows(table, values, where=None, *, limit=None) -> int`**
  - Updates rows matching filter. Supports atomic expressions (`Inc`, `Col`). Auto-chunks oversize IN lists (> 1000 items) and returns total affected rows.
- **`delete_rows(table, where=None, *, limit=None, all_rows=False) -> int`**
  - Deletes rows matching filter. Requires `all_rows=True` when `where=None` to prevent accidental table wipes. Auto-chunks oversize IN lists. Returns total deleted row count.
- **`copy_rows(target_table, columns, select, *, on_conflict="error"|"ignore") -> int`**
  - Performs server-side `INSERT INTO target (cols) SELECT ...` data migration without round-tripping rows through client memory.

### 2.9. Catalog & Server Introspection
Introspection primitives exposing database metadata and runtime configuration.

- **`existing_tables(candidates: Sequence[str]) -> set[str]`**
  - Queries database catalog for existence of specified table names.
- **`column_exists(table: Table | str, column: str) -> bool`**
  - Checks if a specific column exists in the database table schema.
- **`column_info(table: Table | str, column: str) -> ColumnInfo | None`**
  - Returns metadata dataclass `ColumnInfo(name, data_type, is_nullable, column_default, extra)`.
- **`table_stats(tables: Sequence[Table | str]) -> list[TableStats]`**
  - Returns list of `TableStats(table_name, row_count, data_bytes, index_bytes)`.
- **`server_variable(name: str) -> str | None`**
  - Introspects global server configuration parameter.

### 2.10. Schema Maintenance & View Registry (`db_engine/views.py`)
- **`drop_tables(names: Sequence[str], *, if_exists: bool = True, disable_fk_checks: bool = True) -> None`**
  - Drops multiple tables by name, temporarily disabling foreign key checks.
- **`ensure_columns(table: Table, columns: Sequence[str] | None = None) -> list[str]`**
  - Reflects table schema against database catalog and adds missing columns via `ALTER TABLE`.
- **`ensure_views(views: Sequence[str] | None = None) -> None`**
  - Creates or replaces views registered in `ORDERED_VIEW_NAMES` in topological dependency order.
- **Canonical Views**:
  - `m_v_main`: Canonical release tags view.
  - `m_checkpoint_tree`: Milestone checkpoint tree synthesis.
  - `m_bridge_file`: Active file bridge pointer.
  - `m_file`: Unique file blob registry.
  - `m_bridge_commit_file`: Commit to file bridge relations.
  - `m_symbol_def`: Symbol definitions with bit-packed identifier flags.
  - `m_symbol_ref`: Symbol references.

### 2.11. Pipeline Intent Methods
Specialized high-level database operations driving parser pipeline workers.

- **`new_checkpoint_pairs(commit_id: int, prev_commit_id: int) -> Iterator[NewPairs]`**
  - Context manager computing `(fnid, blob_id)` diff between successive commits using a temporary table or anti-join query. Exposes `query_new_tags(kconfig_ref, include_ref)` for incremental tag discovery.
- **`enqueue_parse_tasks(tasks: Sequence[Mapping[str, Any]]) -> int`**
  - Batch inserts parse tasks with `ON DUPLICATE KEY UPDATE` resetting retry counters on re-enqueue.
- **`record_task_failure(network_id: int, ref_name: str, error_msg: str, max_attempts: int = 3) -> None`**
  - Updates failed task status, incrementing `retry_count` and transitioning to dead-letter status when attempts exceed `max_attempts`.
- **`next_eligible_parse_tasks(network_id: int, worker_id: str, lease_seconds: int = 300, limit: int = 1) -> list[dict[str, Any]]`**
  - Acquires unassigned or expired task leases atomically for distributed worker execution.

### 2.12. Webapp Read Session & Query Primitives (`db_engine/pool.py`)
Read-only query abstraction for web services providing dictionary-mapped results and specialized intent methods.

- **Acquisition**:
  - `get_db_reader() -> ContextManager[ReadSession]`
  - `BaseDBEngine.reader() -> ContextManager[ReadSession]`
- **Domain Intent Methods**:
  - `list_network_commits(network_id: int, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]`
  - `maintainer_sections() -> list[dict[str, Any]]`
  - `maintainer_developer_directory(letter: str) -> list[dict[str, Any]]`
  - `find_maintainer_person(query: str, limit: int = 20) -> list[dict[str, Any]]`
  - `symbol_name_page(prefix: str, limit: int = 100) -> list[dict[str, Any]]`
  - `symbol_refs_of_targets(target_ids: Sequence[int], vid: int | None = None) -> list[dict[str, Any]]`
  - `symbol_def_blobs_of_targets(target_ids: Sequence[int]) -> list[dict[str, Any]]`
  - `checkpoint_find_file(commit_id: int, fnid: int) -> int | None`

### 2.13. Embedded SQLite Store (`db_engine/sqlite_store.py`)
High-performance embedded SQLite database engine for local indexes, caches, and offline testing.

- **Features**:
  - WAL journaling mode with automatic checkpoints on transaction commit.
  - Thread-safe connection cache keyed by thread ID with `isolation_level=None`.
  - Schema management: `create_tables(specs)`, `drop_tables(names)`, `existing_tables(names)`, `column_exists(table, col)`.
  - Key-value metadata storage: `meta_get(key, default=None)` and `meta_set(key, value)`.
  - Multi-database attachment: `session(attach={"alias": "path"})`.
  - Automatic 500-item IN list batch chunking on reads and writes.
  - Declarative spec read and write execution (`select_rows`, `iter_rows`, `insert_rows`, `update_rows`, `delete_rows`, `clear`).

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

Comprehensive unit testing and contract verification across all database engines is implemented in
`unit_test/test_db_engine.py` using the reusable harness in `unit_test/harness.py`.

Key test capabilities:
- **Prefix Isolation**: Dynamically generates `t_*` test schemas from all tables in `core.DBLayout.TABLES`,
  rewriting foreign keys to eliminate collision with production `m_*` data.
- **Automated Cleanup & DB Reload**: Drops `t_*` tables at suite start and tearDown, and automatically reloads
  clean schemas upon test failure.
- **Dual Error Tracing**: On test failure, prints rich diagnostic reports featuring both feature-level error
  context and full Python tracebacks.
- **Dependency Propagation**: Enforces prerequisites (e.g. `depends_on=["insert"]`), automatically labeling
  downstream dependent tests as `UNTESTABLE` if prerequisite features fail.
- **Payload & Packet Resilience**: Tests handled recursive chunk bisection under `OperationalError 1153`
  (`max_allowed_packet`), verifies unhandled exception raising on oversized atomic rows, and exercises
  multi-megabyte byte-bounded chunking on live `LONGTEXT` columns.

