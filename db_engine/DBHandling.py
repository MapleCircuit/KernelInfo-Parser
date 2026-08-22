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

import os
from typing import TYPE_CHECKING, Any, Sequence, Self
from types import TracebackType
import mysql.connector
from core.globalstuff import (
    G,
    PointerGetter,
    SafeDataType,
    JoinsType,
    PointerType,
)

from db_engine.base import BaseDBEngine

if TYPE_CHECKING:
    from core.TableHandling import Table

MAX_ALLOWED_PACKET = 1073741824
MAX_JOIN_TABLES = 50
CHUNK_JOIN_SIZE = 30
MAX_CANDIDATE_BATCH = 500


class MariaDB(BaseDBEngine):
    """MariaDB / MySQL implementation of the Database Backend Contract."""

    def __init__(self) -> None:
        """Initialize database connection parameters and open session.

        Args:
            None (reads parameters from environment: DB_USER, DB_PASSWORD, DB_NAME, DB_HOST, DB_PORT).

        Process:
            1. Reads environment configuration defaults.
            2. Opens SQL connection socket via `connect_sql()`.
            3. Creates cursor instance handle.
            4. Invokes `_init_session()` to configure session parameters.

        Outputs:
            None.
        """
        self.user = os.getenv("DB_USER", "root")
        self.password = os.getenv("DB_PASSWORD", "Passe123")  # noqa: S105
        self.db_name = os.getenv("DB_NAME", "test")
        self.host = os.getenv("DB_HOST")
        self.port = int(os.getenv("DB_PORT", "3306"))
        self.cnx = self.connect_sql()
        self.cursor = self.cnx.cursor()
        self._init_session()

    def _init_session(self) -> None:
        """Set session parameters and attempt global packet configuration.

        Args:
            None.

        Process:
            1. Sets `SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO'`.
            2. Configures session timeouts (`wait_timeout`, `interactive_timeout`, `net_read_timeout`, `net_write_timeout`).
            3. Attempts `SET GLOBAL max_allowed_packet = 1GB`, suppressing privilege errors.
            4. Commits session configuration.

        Outputs:
            None.
        """
        self.cursor.execute("SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO';")
        try:
            self.cursor.execute("SET SESSION wait_timeout = 28800;")
            self.cursor.execute("SET SESSION interactive_timeout = 28800;")
            self.cursor.execute("SET SESSION net_read_timeout = 3600;")
            self.cursor.execute("SET SESSION net_write_timeout = 3600;")
        except mysql.connector.Error:
            pass
        try:
            self.cursor.execute(f"SET GLOBAL max_allowed_packet = {MAX_ALLOWED_PACKET};")
        except mysql.connector.Error:
            # Setting global variable may require SUPER/SYSTEM_VARIABLES_ADMIN privileges
            pass
        self.cnx.commit()

    def __enter__(self) -> Self:
        """Enter context manager scope (`with G.DB() as db:`).

        Args:
            None.

        Process:
            Verifies active connection via `check_if_connected()`.

        Outputs:
            Self (Active database driver instance).
        """
        self.check_if_connected()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Exit context manager scope, rolling back on error and releasing resources.

        Args:
            exception_type: Type of exception raised within context block (or None).
            exception_value: Exception instance (or None).
            exception_traceback: Exception traceback (or None).

        Process:
            If an unhandled exception occurred, rolls back pending transaction.

        Outputs:
            None.
        """
        if exception_type is not None and getattr(self, "cnx", None):
            try:
                self.cnx.rollback()
            except Exception:
                pass

    def close(self) -> None:
        """Safely close cursor and connection handles.

        Args:
            None.

        Process:
            Closes cursor and connection sockets, suppressing any teardown errors.

        Outputs:
            None.
        """
        if getattr(self, "cursor", None):
            try:
                self.cursor.close()
            except Exception:
                pass
            self.cursor = None
        if getattr(self, "cnx", None):
            try:
                self.cnx.close()
            except Exception:
                pass
            self.cnx = None

    def connect_sql(self) -> Any:
        """Establish SQL connection with environment detection.

        Args:
            None.

        Process:
            Detects environment (explicit DB_HOST, Docker container files `/.dockerenv`
            or `/proc/self/cgroup`, or localhost fallback) and opens MySQL connection.

        Outputs:
            Native MySQL connection socket handle.
        """
        if self.host:
            mysql_host = self.host
        elif os.path.exists("/.dockerenv"):
            mysql_host = "host.docker.internal"
        elif os.path.exists("/proc/self/cgroup"):
            try:
                with open("/proc/self/cgroup", "r", encoding="utf-8") as f:
                    cgroup_content = f.read()
                if "docker" in cgroup_content:
                    mysql_host = "host.docker.internal"
                else:
                    mysql_host = "localhost"
            except (OSError, UnicodeDecodeError):
                mysql_host = "localhost"
        else:
            mysql_host = "localhost"

        return mysql.connector.connect(
            host=mysql_host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.db_name,
        )

    def check_if_connected(self) -> None:
        """Verify connection health and automatically reconnect if dropped.

        Args:
            None.

        Process:
            Checks active connection via `ping(reconnect=True)`. If disconnected, attempts up to 3 reconnects,
            re-creating the cursor and re-initializing session state.

        Outputs:
            None. Guarantees healthy connection or raises ConnectionError.

        Raises:
            ConnectionError: If connection cannot be re-established after 3 attempts.
        """
        if getattr(self, "cnx", None) is not None:
            try:
                self.cnx.ping(reconnect=True, attempts=3, delay=1)
                if getattr(self, "cursor", None) is None:
                    self.cursor = self.cnx.cursor()
                return
            except Exception:
                pass

        for attempt in range(3):
            print(f"No SQL connection. Reconnection attempt {attempt + 1}/3...")
            try:
                self.cnx = self.connect_sql()
                self.cursor = self.cnx.cursor()
                self._init_session()
                return
            except (mysql.connector.Error, Exception) as e:
                if attempt == 2:
                    raise ConnectionError(f"Failed to reconnect to database after 3 attempts: {e}") from e

    def test_tables(self, tables: Sequence[Table] | Table) -> list[str] | None:
        """Test existence of registered schema tables in database catalog.

        Args:
            tables: Single Table instance or sequence of Table instances.

        Process:
            1. Normalizes tables into a tuple.
            2. Executes `SHOW TABLES` and extracts existing table names.
            3. Identifies any registered tables missing from database catalog.

        Outputs:
            List of missing table name strings or None if all exist.
        """
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)

        self.check_if_connected()
        self.cursor.execute("SHOW TABLES")
        existing_tables = {row[0] for row in self.cursor.fetchall()}

        missing_tables = [table.table_name for table in tables if table.table_name not in existing_tables]

        if missing_tables:
            print(f"We are missing these tables: {missing_tables}")
            return missing_tables
        return None

    def drop_table(self, tables: Sequence[Table] | Table) -> None:
        """Drop target tables, safely disabling foreign key checks.

        Args:
            tables: Single Table instance or sequence of Table instances.

        Process:
            1. Normalizes tables into a tuple.
            2. Disables foreign key checks (`SET FOREIGN_KEY_CHECKS = 0;`).
            3. Executes `DROP TABLE IF EXISTS <table_list>`.
            4. Commits transaction and restores `SET FOREIGN_KEY_CHECKS = 1;` in finally block.

        Outputs:
            None.
        """
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        if not tables:
            return

        self.check_if_connected()
        try:
            self.cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
            sql_drop = f"DROP TABLE IF EXISTS {', '.join(f'`{x.table_name}`' for x in tables)}"
            self.cursor.execute(sql_drop)
            self.cnx.commit()
        except Exception as e:
            self.cnx.rollback()
            print(f"Drop table failed: {e}")
            raise
        finally:
            self.cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")
            self.cnx.commit()

    def create_table(self, tables: Sequence[Table] | Table) -> None:
        """Create database tables and insert initial seed records.

        Args:
            tables: Single Table instance or sequence of Table instances.

        Process:
            1. Normalizes tables into a tuple.
            2. For each Table:
               - Constructs parameterized `CREATE TABLE` query matching columns, primary keys, and foreign keys.
               - Executes query.
               - If `table.initial_insert` is defined, inserts initial seed rows via `insert()`.
            3. Commits transaction.

        Outputs:
            None.
        """
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)

        self.check_if_connected()

        for table in tables:
            sql_table = f"CREATE TABLE {table.table_name} "
            sql_table += f"({', '.join(map(' '.join, table.init_columns))} "
            sql_table += f", PRIMARY KEY ({', '.join(table.init_primary)}) "
            if table.init_foreign:
                sql_table += " ".join(
                    f",FOREIGN KEY ({x[0]}) REFERENCES {x[1]}({x[2]})" for x in table.init_foreign
                )
            sql_table += ")"
            if G.OVERRIDE_TABLE_CREATION_PRINT:
                print(f"Created table:{table.table_name}")
                print(sql_table)
            self.check_if_connected()
            self.cursor.execute(sql_table)
            if table.initial_insert:
                self.insert(table, table.initial_insert)

        self.cnx.commit()

    def get_next_id(self, table: Table) -> int:
        """Query current maximum primary key sequence ID for target table.

        Args:
            table: Target Table schema instance.

        Process:
            Executes `SELECT COALESCE(MAX({pk_col}), 0)+1 FROM {table_name};` and fetches result.

        Outputs:
            Next available sequence integer ID.
        """
        self.check_if_connected()
        self.cursor.execute(
            f"SELECT COALESCE(MAX({table.init_columns[0][0]}), 0)+1 FROM {table.table_name};",  # noqa: S608
        )
        return self.cursor.fetchone()[0]

    def insert(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        """Insert row(s) into database table in chunked batches.

        Args:
            table: Target Table schema instance.
            data: Single row tuple or batch tuple of row tuples.

        Process:
            1. Constructs parameterized `INSERT INTO table VALUES (%s, ...)`.
            2. If `data` is a batch tuple, chunks into 1000-row slices and executes `executemany` with auto-retry.
            3. If `data` is a single row tuple, executes `execute` with auto-retry.
            4. Commits transaction.

        Outputs:
            None.
        """
        if not data:
            return

        sql = f"INSERT INTO `{table.table_name}` VALUES ({','.join(('%s',) * table.length)})"

        if isinstance(data[0], (tuple, list)):
            batch_size = 1000  # Safe High-throughput batch size
            for i in range(0, len(data), batch_size):
                chunk = data[i : i + batch_size]
                for attempt in range(3):
                    self.check_if_connected()
                    try:
                        self.cursor.executemany(sql, chunk)
                        break
                    except (mysql.connector.OperationalError, mysql.connector.InterfaceError, OSError):
                        if attempt == 2:
                            raise
                        self.close()
        else:
            for attempt in range(3):
                self.check_if_connected()
                try:
                    self.cursor.execute(sql, data)
                    break
                except (mysql.connector.OperationalError, mysql.connector.InterfaceError, OSError):
                    if attempt == 2:
                        raise
                    self.close()

        self.check_if_connected()
        self.cnx.commit()

    def update(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        """Update row(s) in database table using ON DUPLICATE KEY UPDATE.

        Args:
            table: Target Table schema instance.
            data: Single row tuple or batch tuple of row tuples.

        Process:
            1. Constructs upsert SQL statement:
               `INSERT INTO table VALUES (...) ON DUPLICATE KEY UPDATE col=VALUES(col)`
               for all non-primary key columns.
            2. If `data` is a batch tuple, chunks into 1000-row slices and executes `executemany` with auto-retry.
            3. If single row, executes `execute` with auto-retry.
            4. Commits transaction.

        Outputs:
            None.
        """
        if not data:
            return

        sql = f"INSERT INTO `{table.table_name}` "
        sql += f"({', '.join(f'`{column[0]}`' for column in table.init_columns)}) VALUES "
        sql += f"({','.join(('%s',) * table.length)}) ON DUPLICATE KEY UPDATE "
        updatable_columns = []
        for x, column in enumerate(table.init_columns):
            if x not in table.primary:
                updatable_columns.append(f"`{column[0]}` = VALUES(`{column[0]}`)")
        sql += ", ".join(updatable_columns)

        if isinstance(data[0], (tuple, list)):
            batch_size = 1000
            for i in range(0, len(data), batch_size):
                chunk = data[i : i + batch_size]
                for attempt in range(3):
                    self.check_if_connected()
                    try:
                        self.cursor.executemany(sql, chunk)
                        break
                    except (mysql.connector.OperationalError, mysql.connector.InterfaceError, OSError):
                        if attempt == 2:
                            raise
                        self.close()
        else:
            for attempt in range(3):
                self.check_if_connected()
                try:
                    self.cursor.execute(sql, data)
                    break
                except (mysql.connector.OperationalError, mysql.connector.InterfaceError, OSError):
                    if attempt == 2:
                        raise
                    self.close()

        self.check_if_connected()
        self.cnx.commit()

    def commit_tables_parallel(
        self,
        tables_data: Sequence[tuple[Table, Sequence[tuple[SafeDataType, ...]], Sequence[tuple[SafeDataType, ...]]]],
        max_workers: int | None = None,
    ) -> None:
        """Commit inserts and updates for multiple tables concurrently across worker connection threads.

        Args:
            tables_data: Sequence of tuples `(table, insert_payload, update_payload)`.
            max_workers: Maximum concurrent database worker threads (defaults to min(len(tables_data), 8)).

        Process:
            1. Filters non-empty table payloads.
            2. If single table, executes directly on current connection.
            3. If multiple tables, dispatches concurrently via ThreadPoolExecutor.
            4. Each worker thread opens an isolated MariaDB connection, disables foreign key and unique checks,
               executes batch operations, commits, and releases resources.
        """
        valid_items = [item for item in tables_data if item[1] or item[2]]
        if not valid_items:
            return

        if len(valid_items) == 1:
            table, insert_data, update_data = valid_items[0]
            if insert_data:
                self.insert(table, insert_data)
            if update_data:
                self.update(table, update_data)
            return

        from concurrent.futures import ThreadPoolExecutor
        workers = min(len(valid_items), max_workers or 8)

        def _worker_commit(item: tuple[Table, Sequence[tuple[SafeDataType, ...]], Sequence[tuple[SafeDataType, ...]]]) -> None:
            table, insert_data, update_data = item
            worker_db = MariaDB()
            try:
                try:
                    worker_db.cursor.execute("SET unique_checks = 0; SET foreign_key_checks = 0; SET autocommit = 0;")
                except Exception:
                    pass

                if insert_data:
                    worker_db.insert(table, insert_data)
                if update_data:
                    worker_db.update(table, update_data)

                try:
                    worker_db.cursor.execute("SET unique_checks = 1; SET foreign_key_checks = 1; SET autocommit = 1;")
                except Exception:
                    pass
                worker_db.cnx.commit()
            finally:
                worker_db.close()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(_worker_commit, valid_items))

    def select(
        self,
        table: Table,
        data: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Execute single-row SELECT query matching non-None column filter values.

        Args:
            table: Target Table schema instance.
            data: Tuple of column filter values (None values act as wildcards).

        Process:
            1. Constructs `SELECT * FROM table WHERE col1=%s AND col2=%s LIMIT 1`
               for non-None positions in `data`.
            2. Executes query with filtered parameter tuple.
            3. Returns first matching row.

        Outputs:
            Matching row tuple `tuple[SafeDataType, ...]` or None.
        """
        self.check_if_connected()

        sql = f"SELECT * FROM {table.table_name}"  # noqa: S608
        where_clauses = []

        for x, val in enumerate(data):
            if val is not None:
                where_clauses.append(f"{table.init_columns[x][0]}=%s")

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        sql += " LIMIT 1"

        params = tuple(val for val in data if val is not None)
        self.cursor.execute(sql, params)
        return self.cursor.fetchone()

    def _build_view_query(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[str, tuple[SafeDataType, ...]]:
        """Build parameterized SQL query and parameters for multi-table relational joins.

        Args:
            tables: Table schema sequence or dictionary keyed by table_id.
            joins: Relational join graph tuple (JoinsType).
            columns: Concatenated tuple of column filter values across joined tables.

        Process:
            1. Identifies initial table pointer and sets primary alias `A1`.
            2. Iterates over joined table pointers, generating `JOIN table A<offset> ON ...`.
            3. Appends WHERE clauses matching non-None column filter positions.
            4. Extracts parameter tuple of non-None values.

        Outputs:
            Tuple `(sql_query_string, parameters_tuple)`.
        """
        initial_pointer = PointerGetter(joins).get_first_pointer()
        sql = f"SELECT * FROM {tables[initial_pointer[0]].table_name} AS A1"  # noqa: S608

        data_offset = 0
        where_clauses = []
        for i, init_column in enumerate(tables[initial_pointer[0]].init_columns):
            if columns[i] is not None:
                where_clauses.append(f"A1.{init_column[0]}=%s")
            data_offset += 1

        # Multi-table joins
        if len(joins[0]) > 1:
            table_id_to_alias_dict = {initial_pointer[0]: 1}
            alias_offset = 1

            for join in joins:
                for x in range(join[2]):
                    alias_offset += 1
                    if x == 0:
                        table_id_to_alias_dict[join[1][0]] = alias_offset
                    sql += f" JOIN {tables[join[1][0]].table_name} A{alias_offset} ON A{table_id_to_alias_dict[join[0][0]]}.{tables[join[0][0]].init_columns[join[0][1]][0]} = A{alias_offset}.{tables[join[1][0]].init_columns[join[1][1]][0]}"

                    for init_column in tables[join[1][0]].init_columns:
                        if data_offset < len(columns) and columns[data_offset] is not None:
                            where_clauses.append(f"A{alias_offset}.{init_column[0]}=%s")
                        data_offset += 1

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        params = tuple(val for val in columns if val is not None)
        return sql, params

    def _chunked_view_select(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
        limit_one: bool = True,
    ) -> list[tuple[SafeDataType, ...]] | tuple[SafeDataType, ...] | None:
        """Execute relational join query across large join graphs by chunking joins into <= 30 tables per step.

        Args:
            tables: Table schema sequence or dictionary keyed by table_id.
            joins: Relational join graph tuple (JoinsType).
            columns: Concatenated tuple of column filter values across joined tables.
            limit_one: If True, returns single matching row tuple (or None); if False, returns list of matching row tuples.

        Process:
            1. Extracts root table and maps all joined table slices with their data offsets.
            2. Partitions joined table slices into chunks of size `CHUNK_JOIN_SIZE` (<= 30).
            3. Filters candidate root primary keys across sequential chunk queries (SELECT DISTINCT A1.pk ...).
            4. Reconstructs exact flat result tuples for surviving candidate root IDs by querying chunk column slices.

        Outputs:
            First matching joined row tuple (or None) if limit_one is True, else list of matching joined row tuples.
        """
        initial_pointer = PointerGetter(joins).get_first_pointer()
        parent_table = tables[initial_pointer[0]]
        parent_pk_col = parent_table.init_columns[initial_pointer[1]][0]

        # Map each individual joined table instance with its target table, join columns, and data offset
        join_slices: list[tuple[Table, str, str, int]] = []
        data_offset = parent_table.length

        for join in joins:
            if len(join) < 2:
                continue
            src_table = tables[join[0][0]]
            src_col_name = src_table.init_columns[join[0][1]][0]
            target_table = tables[join[1][0]]
            target_col_name = target_table.init_columns[join[1][1]][0]
            repeat_count = join[2] if len(join) > 2 else 1

            for _ in range(repeat_count):
                join_slices.append((target_table, src_col_name, target_col_name, data_offset))
                data_offset += target_table.length

        if not join_slices:
            return None if limit_one else []

        # Partition joined tables into chunks of <= CHUNK_JOIN_SIZE
        chunks = [join_slices[i : i + CHUNK_JOIN_SIZE] for i in range(0, len(join_slices), CHUNK_JOIN_SIZE)]
        candidate_ids: list[Any] | None = None

        # Phase 1: Filter candidate root IDs across each chunk sequentially
        for chunk_idx, chunk in enumerate(chunks):
            base_sql = f"SELECT DISTINCT A1.{parent_pk_col} FROM {parent_table.table_name} AS A1"  # noqa: S608
            base_joins = ""
            base_where: list[str] = []
            base_params: list[SafeDataType] = []

            if chunk_idx == 0:
                for i, init_column in enumerate(parent_table.init_columns):
                    if columns[i] is not None:
                        base_where.append(f"A1.{init_column[0]}=%s")
                        base_params.append(columns[i])

            alias_offset = 1
            for target_table, src_col_name, target_col_name, slice_offset in chunk:
                alias_offset += 1
                alias = f"A{alias_offset}"
                base_joins += f" JOIN {target_table.table_name} {alias} ON A1.{src_col_name} = {alias}.{target_col_name}"

                for init_column in target_table.init_columns:
                    if slice_offset < len(columns) and columns[slice_offset] is not None:
                        base_where.append(f"{alias}.{init_column[0]}=%s")
                        base_params.append(columns[slice_offset])
                    slice_offset += 1

            if chunk_idx == 0:
                sql = base_sql + base_joins
                if base_where:
                    sql += " WHERE " + " AND ".join(base_where)
                self.cursor.execute(sql, tuple(base_params))
                rows = self.cursor.fetchall()
                candidate_ids = [row[0] for row in rows]
                if not candidate_ids:
                    return None if limit_one else []
            else:
                if not candidate_ids:
                    return None if limit_one else []

                surviving_ids: list[Any] = []
                # Batch candidate IDs in slices of MAX_CANDIDATE_BATCH to avoid oversized IN clauses
                for i in range(0, len(candidate_ids), MAX_CANDIDATE_BATCH):
                    cand_batch = candidate_ids[i : i + MAX_CANDIDATE_BATCH]
                    placeholders = ", ".join(["%s"] * len(cand_batch))
                    where_clauses = [f"A1.{parent_pk_col} IN ({placeholders})"] + base_where
                    sql = base_sql + base_joins + " WHERE " + " AND ".join(where_clauses)
                    params = list(cand_batch) + base_params

                    self.cursor.execute(sql, tuple(params))
                    rows = self.cursor.fetchall()
                    surviving_ids.extend(row[0] for row in rows)

                    # Short-circuit for limit_one if we already found surviving candidates
                    if limit_one and surviving_ids:
                        break

                candidate_ids = surviving_ids
                if not candidate_ids:
                    return None if limit_one else []

        # Phase 2: Reconstruct exact flat row tuples for surviving candidate root IDs
        results: list[tuple[SafeDataType, ...]] = []
        target_candidates = candidate_ids[:1] if limit_one else candidate_ids

        for cand_id in target_candidates:
            full_row: list[SafeDataType] = []

            for chunk_idx, chunk in enumerate(chunks):
                alias_offset = 1
                select_items: list[str] = []

                if chunk_idx == 0:
                    select_items.append("A1.*")

                for _ in chunk:
                    alias_offset += 1
                    select_items.append(f"A{alias_offset}.*")

                sql = f"SELECT {', '.join(select_items)} FROM {parent_table.table_name} AS A1"  # noqa: S608
                where_clauses = [f"A1.{parent_pk_col} = %s"]
                params = [cand_id]

                alias_offset = 1
                for target_table, src_col_name, target_col_name, slice_offset in chunk:
                    alias_offset += 1
                    alias = f"A{alias_offset}"
                    sql += f" JOIN {target_table.table_name} {alias} ON A1.{src_col_name} = {alias}.{target_col_name}"

                    for init_column in target_table.init_columns:
                        if slice_offset < len(columns) and columns[slice_offset] is not None:
                            where_clauses.append(f"{alias}.{init_column[0]}=%s")
                            params.append(columns[slice_offset])
                        slice_offset += 1

                sql += " WHERE " + " AND ".join(where_clauses) + " LIMIT 1"
                self.cursor.execute(sql, tuple(params))
                chunk_row = self.cursor.fetchone()

                if chunk_row is not None:
                    full_row.extend(chunk_row)
                else:
                    if chunk_idx == 0:
                        full_row.extend([None] * parent_table.length)
                    for target_table, _, _, _ in chunk:
                        full_row.extend([None] * target_table.length)

            results.append(tuple(full_row))

        if limit_one:
            return results[0] if results else None
        return results

    def view_select(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Execute single-row SELECT query across multi-table relational join graph.

        Args:
            tables: Table schema sequence or dictionary keyed by table_id.
            joins: Relational join graph tuple (JoinsType).
            columns: Concatenated tuple of column filter values across joined tables.

        Process:
            1. Checks total table alias count; if > MAX_JOIN_TABLES (50), routes to `_chunked_view_select()`.
            2. Otherwise, generates parameterized SQL via `_build_view_query()`, appends `LIMIT 1`, and fetches first match.

        Outputs:
            First matching joined row tuple `tuple[SafeDataType, ...]` or None.
        """
        self.check_if_connected()
        total_aliases = 1
        if len(joins) > 0 and len(joins[0]) > 1:
            total_aliases += sum(join[2] if len(join) > 2 else 1 for join in joins)

        if total_aliases > MAX_JOIN_TABLES:
            return self._chunked_view_select(tables, joins, columns, limit_one=True)  # type: ignore[return-value]

        sql, params = self._build_view_query(tables, joins, columns)
        sql += " LIMIT 1"
        self.cursor.execute(sql, params)
        return self.cursor.fetchone()

    def view_select_multiple(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> list[tuple[SafeDataType, ...]]:
        """Execute multi-row SELECT query across relational join graph, fetching all matches.

        Args:
            tables: Table schema sequence or dictionary keyed by table_id.
            joins: Relational join graph tuple (JoinsType).
            columns: Concatenated tuple of column filter values across joined tables.

        Process:
            1. Checks total table alias count; if > MAX_JOIN_TABLES (50), routes to `_chunked_view_select()`.
            2. Otherwise, generates parameterized SQL via `_build_view_query()` and fetches all matching rows (`fetchall()`).

        Outputs:
            List of matching joined row tuples `list[tuple[SafeDataType, ...]]`.
        """
        self.check_if_connected()
        total_aliases = 1
        if len(joins) > 0 and len(joins[0]) > 1:
            total_aliases += sum(join[2] if len(join) > 2 else 1 for join in joins)

        if total_aliases > MAX_JOIN_TABLES:
            return self._chunked_view_select(tables, joins, columns, limit_one=False)  # type: ignore[return-value]

        sql, params = self._build_view_query(tables, joins, columns)
        self.cursor.execute(sql, params)
        return self.cursor.fetchall()


    def index_exists(self, index_name: str, table: Table) -> bool:
        """Check if an index exists on the specified table."""
        self.check_if_connected()
        try:
            self.cursor.execute(f"SHOW INDEX FROM {table.table_name} WHERE Key_name = %s", (index_name,))
            return bool(self.cursor.fetchone())
        except Exception:
            return False

    def create_index(
        self,
        index_name: str,
        table: Table,
        rows: tuple[PointerType, ...],
    ) -> None:
        """Create database index on target table columns if not already existing.

        Args:
            index_name: Name identifier for index.
            table: Target Table schema instance.
            rows: Tuple of pointer tuples `(table_id, col_idx)`.

        Process:
            1. Checks if index already exists via `SHOW INDEX`.
            2. If not, executes `CREATE INDEX index_name ON table (cols)`.
            3. Commits transaction and handles duplicate index exceptions safely.

        Outputs:
            None.
        """
        self.check_if_connected()
        try:
            if self.index_exists(index_name, table):
                return
            sql = f"CREATE INDEX {index_name} ON {table.table_name} "
            sql += f"({', '.join(table.init_columns[x[1]][0] for x in rows)})"
            self.cursor.execute(sql)
            self.cnx.commit()
        except mysql.connector.Error as e:
            # 1061: Duplicate key name
            if getattr(e, "errno", None) == 1061 or "Duplicate key name" in str(e):
                return
            raise

    def remove_index(
        self,
        index_name: str,
        table: Table,
    ) -> None:
        """Drop existing index from target table if it exists.

        Args:
            index_name: Name identifier of index to drop.
            table: Target Table schema instance.

        Process:
            1. Checks if index exists via `SHOW INDEX`.
            2. If exists, executes `ALTER TABLE table DROP INDEX index_name`.
            3. Commits transaction and handles missing index exceptions safely.

        Outputs:
            None.
        """
        self.check_if_connected()
        try:
            if not self.index_exists(index_name, table):
                return
            sql = f"ALTER TABLE {table.table_name} DROP INDEX {index_name}"
            self.cursor.execute(sql)
            self.cnx.commit()
        except mysql.connector.Error as e:
            # 1091: Can't DROP 'index'; check that column/key exists
            if getattr(e, "errno", None) == 1091 or "check that column/key exists" in str(e):
                return
            raise
