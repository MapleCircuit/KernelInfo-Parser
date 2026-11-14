"""DBHandling.py - Database Backend API Contract & MariaDB Implementation.

===============================================================================
DATABASE BACKEND API SPECIFICATION & CONTRACT GUIDE
===============================================================================
This module defines the Database Driver Interface expected by KernelInfo-Parser.
Any database driver class (e.g., MariaDB, PostgreSQL, SQLite, DuckDB) assigned to
`G.DB` must implement the following API methods:

1. __init__() -> None
   - Input: None (reads driver connection config/environment variables).
   - Process: Establishes connection instance, creates cursor, sets session parameters
     (sql_mode = 'NO_AUTO_VALUE_ON_ZERO', max_allowed_packet expansion).
   - Output: None.

2. __enter__() -> Self
   - Input: None.
   - Process: Context manager entry (`with G.DB() as db:`). Verifies connection handle.
   - Output: Self (Active database driver instance).

3. __exit__(exception_type, exception_value, exception_traceback) -> None
   - Input: Standard Python exception context parameters.
   - Process: Context manager exit (`with G.DB() as db:`). Rolls back on error and cleans up.
   - Output: None.

4. close() -> None
   - Input: None.
   - Process: Safely closes cursor and connection handles.
   - Output: None.

5. connect_sql() -> object
   - Input: None.
   - Process: Environment detection (Docker vs host) and opens SQL connection socket.
   - Output: Native database connection object handle.

6. check_if_connected() -> None
   - Input: None.
   - Process: Connection health guard; auto-reconnects up to 3 attempts if dropped
     and resets session parameters.
   - Output: None. Guarantees active connection handle upon return.

7. test_tables(tables: Sequence[Table] | Table) -> list[str] | None
   - Input: Single Table schema object or sequence of Table objects.
   - Process: Queries database catalog (SHOW TABLES), compares against registered tables,
     and logs missing table names.
   - Output: None (or list of missing table names).

8. create_table(tables: Sequence[Table] | Table) -> None
   - Input: Single Table schema object or sequence of Table objects.
   - Process: Translates Table metadata (init_columns, init_primary, init_foreign)
     to CREATE TABLE queries, executes SQL statements, seeds initial_insert data if present, and commits.
   - Output: None.

9. drop_table(tables: Sequence[Table] | Table) -> None
   - Input: Single Table schema object or sequence of Table objects.
   - Process: Disables foreign key checks temporarily, executes DROP TABLE IF EXISTS query, and commits.
   - Output: None.

10. get_next_id(table: Table) -> int
    - Input: Table schema object.
    - Process: Queries current max value of primary key column: COALESCE(MAX(pk), 0) + 1.
    - Output: int (Next available sequence integer ID).

11. insert(table: Table, data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]) -> None
    - Input: Table schema object, row tuple or batch of row tuples matching table columns.
    - Process: Constructs parameterized INSERT INTO statement. Handles payload chunking
      (1000 rows/batch), executes batch insert, and commits.
    - Output: None.

12. update(table: Table, data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]) -> None
    - Input: Table schema object, batch of row tuples matching table columns.
    - Process: Constructs upsert query (INSERT ... ON DUPLICATE KEY UPDATE col=VALUES(col))
      for all non-primary key columns, executes batch, and commits.
    - Output: None.

13. select(table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None
    - Input: Table schema object, query tuple of column filter values (None acts as wildcard).
    - Process: Builds SELECT * FROM table WHERE col1=%s AND col2=%s LIMIT 1 for non-None
      positions, executes query, and returns first match.
    - Output: tuple[SafeDataType, ...] (Matching row tuple) or None.

14. view_select(tables: Sequence[Table] | dict[int, Table], joins: JoinsType, columns: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None
    - Input: Collection of all Table objects, JoinsType relational graph tuple, query columns tuple (None acts as wildcard).
    - Process: Dynamically builds multi-table JOIN query SELECT * FROM t1 A1 JOIN t2 A2 ON... WHERE... LIMIT 1 for non-None columns.
    - Output: tuple[SafeDataType, ...] (First matching joined row tuple) or None.

15. view_select_multiple(tables: Sequence[Table] | dict[int, Table], joins: JoinsType, columns: tuple[SafeDataType, ...]) -> list[tuple[SafeDataType, ...]]
    - Input: Collection of all Table objects, JoinsType relational graph tuple, query columns tuple (None acts as wildcard).
    - Process: Dynamically builds multi-table JOIN query SELECT * FROM t1 A1 JOIN t2 A2 ON... WHERE... matching non-None columns, and fetches all rows (fetchall()).
    - Output: list[tuple[SafeDataType, ...]] (List of matching joined row tuples).

16. create_index(index_name: str, table: Table, rows: tuple[PointerType, ...]) -> None
    - Input: Index name string, target Table object, tuple of (table_id, col_idx) pointers.
    - Process: Maps column indices to names, executes CREATE INDEX query, and commits.
    - Output: None.

17. remove_index(index_name: str, table: Table) -> None
    - Input: Index name string, target Table object.
    - Process: Executes ALTER TABLE table DROP INDEX index_name (or native drop index query), and commits.
    - Output: None.
===============================================================================
"""
from __future__ import annotations

from db_engine.base import BaseDBEngine
from db_engine.mariadb_connection import MariaDBConnection
from db_engine.mariadb_indexes import MariaDBIndexes
from db_engine.mariadb_spec import MariaDBSpec, MariaDBNewPairs  # noqa: F401 - MariaDBNewPairs: returned by new_checkpoint_pairs
from db_engine.mariadb_support import _wrap_driver_errors
from db_engine.mariadb_tables import MariaDBTables
from db_engine.mariadb_views import MariaDBViews


class MariaDB(MariaDBConnection, MariaDBTables, MariaDBIndexes, MariaDBViews, MariaDBSpec, BaseDBEngine):
    """MariaDB / MySQL implementation of the Database Backend Contract.

    The behaviour is split over the mixins (`db_engine/mariadb_*.py`); this class composes them over `BaseDBEngine`."""




# Public entry points whose mysql-connector errors surface to callers as neutral `db_engine.errors` classes
# (`raise Neutral(...) from native_error`). Internal catch sites run inside these methods against raw cursors.
for _name in (
    "_raw_query", "_raw_query_one", "_raw_query_iter", "_raw_execute", "_raw_executemany",
    "select", "select_in", "select_preload", "view_select", "view_select_multiple", "table_size",
    "insert", "update", "get_next_id", "reserve_id_block", "commit", "commit_tables_parallel",
    "select_rows", "iter_rows", "insert_rows", "update_rows", "delete_rows", "copy_rows",
    "drop_tables", "drop_table", "create_table", "create_view", "create_index", "remove_index",
):
    setattr(MariaDB, _name, _wrap_driver_errors(getattr(MariaDB, _name)))
del _name
