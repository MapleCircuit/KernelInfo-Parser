"""Table DDL and tuple-oriented reads/writes (insert, update, select, preload) of the MariaDB driver."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Sequence
import mysql.connector
from core.globalstuff import (
    G,
    SafeDataType,
)

from db_engine.base import (
    TableCommitStat,
    estimate_column_bytes,
)
from db_engine.errors import (
    DuplicateKeyError,
)
from db_engine.sql_render import (
    render_create_table,
)

if TYPE_CHECKING:
    from core.TableHandling import Table
from db_engine.mariadb_support import _coerce_bytes, _is_binary_type, logger



class MariaDBTables:
    """Table DDL and tuple-oriented reads/writes (insert, update, select, preload) of the MariaDB driver."""

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
        self._cur.execute("SHOW TABLES")
        existing_tables = {row[0] for row in self._cur.fetchall()}

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
            self._cur.execute("SET FOREIGN_KEY_CHECKS = 0;")
            sql_drop = f"DROP TABLE IF EXISTS {', '.join(f'`{x.table_name}`' for x in tables)}"
            self._cur.execute(sql_drop)
            self.cnx.commit()
        except Exception as e:
            self.cnx.rollback()
            print(f"Drop table failed: {e}")
            raise
        finally:
            self._cur.execute("SET FOREIGN_KEY_CHECKS = 1;")
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
        self._cur.execute("SET foreign_key_checks = 0;")
        try:
            for table in tables:
                sql_table = render_create_table(table, "mariadb")
                if G.OVERRIDE_TABLE_CREATION_PRINT:
                    print(f"Created table:{table.table_name}")
                    print(sql_table)
                self.check_if_connected()
                self._cur.execute(sql_table)
                if table.initial_insert:
                    self.insert(table, table.initial_insert)
            self.cnx.commit()
        finally:
            self._cur.execute("SET foreign_key_checks = 1;")
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
        if table.table_id not in self._next_id_sql_cache:
            self._next_id_sql_cache[table.table_id] = (
                f"SELECT COALESCE(MAX(`{table.init_columns[0][0]}`), 0)+1 FROM `{table.table_name}`;"
            )
        self._cur.execute(self._next_id_sql_cache[table.table_id])
        return self._cur.fetchone()[0]

    @staticmethod
    def _insert_sql(table: Table) -> str:
        """Build the parameterized INSERT statement for `table`.

        For `idempotent_pk` tables, rows whose primary key already exists are skipped individually via a
        no-op `ON DUPLICATE KEY UPDATE`: a plain multi-row INSERT fails as a whole on one duplicate, which
        would drop every other row of the chunk. (`INSERT IGNORE` would skip the same rows, but
        mysql-connector stops batching it into multi-row statements and sends one round trip per row.)
        """
        sql = f"INSERT INTO `{table.table_name}` VALUES ({','.join(('%s',) * table.length)})"
        if getattr(table, "idempotent_pk", False) and table.init_primary:
            pk_col = table.init_primary[0]
            sql += f" ON DUPLICATE KEY UPDATE `{pk_col}` = `{pk_col}`"
        return sql

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
            1. Constructs parameterized `INSERT INTO table VALUES (%s, ...)` (`_insert_sql()`; `idempotent_pk`
               tables add a no-op `ON DUPLICATE KEY UPDATE` so only already-present rows are skipped).
            2. If `data` is a batch tuple, chunks into 1000-row slices and executes `executemany` with auto-retry.
            3. If `data` is a single row tuple, executes `execute` with auto-retry.
            4. Commits transaction.

        Outputs:
            None.
        """
        if not data:
            return

        sql = self._insert_sql(table)

        has_large_cols = any(
            col[1].is_large
            for col in table.init_columns
        ) or table.table_name in ("m_tag_code", "m_commit", "m_credits_entry")

        max_rows = 500 if has_large_cols else (5000 if table.length <= 4 else 2000)
        max_bytes = 4 * 1024 * 1024 if has_large_cols else 8 * 1024 * 1024

        def _estimate_row_bytes(row: Sequence[Any]) -> int:
            return sum(len(x) if isinstance(x, (str, bytes, bytearray, memoryview)) else 8 for x in row)

        def _exec_chunk(chunk_data: Sequence[tuple[SafeDataType, ...]]) -> None:
            if not chunk_data:
                return
            for attempt in range(3):
                self.check_if_connected()
                try:
                    self._cur.executemany(sql, chunk_data)
                    return
                except mysql.connector.Error as err:
                    if getattr(err, "errno", None) == 1062 or "duplicate entry" in str(err).lower():
                        if getattr(table, "idempotent_pk", False):
                            return
                        raise DuplicateKeyError(f"Duplicate entry for primary key in table '{table.table_name}': {err}") from err
                    # Adaptive bisection on packet too large (errno 1153)
                    if (getattr(err, "errno", None) == 1153 or "max_allowed_packet" in str(err).lower()) and len(chunk_data) > 1:
                        mid = len(chunk_data) // 2
                        logger.warning(
                            f"Packet exceeded max_allowed_packet inserting into '{table.table_name}' with {len(chunk_data)} rows. "
                            f"Bisecting into chunks of {mid} and {len(chunk_data) - mid} rows..."
                        )
                        _exec_chunk(chunk_data[:mid])
                        _exec_chunk(chunk_data[mid:])
                        return
                    if isinstance(err, (mysql.connector.OperationalError, mysql.connector.InterfaceError)):
                        if self._in_txn:
                            raise
                        self.close()
                        if attempt == 2:
                            raise
                        time.sleep(0.2 * (attempt + 1))
                        continue
                    logger.error(
                        f"Database error executing executemany on table '{table.table_name}': {err}. "
                        f"Batch size: {len(chunk_data)}, total data: {len(data)}. "
                        f"First sample row: {chunk_data[0] if chunk_data else None}"
                    )
                    raise
                except (OSError, AttributeError):
                    if self._in_txn:
                        raise
                    self.close()
                    if attempt == 2:
                        raise
                    time.sleep(0.2 * (attempt + 1))

        if isinstance(data[0], (tuple, list)):
            current_chunk = []
            current_bytes = 0
            for row in data:
                r_bytes = _estimate_row_bytes(row)
                if current_chunk and (len(current_chunk) >= max_rows or (current_bytes + r_bytes > max_bytes)):
                    _exec_chunk(current_chunk)
                    current_chunk = [row]
                    current_bytes = r_bytes
                else:
                    current_chunk.append(row)
                    current_bytes += r_bytes
            if current_chunk:
                _exec_chunk(current_chunk)
        else:
            for attempt in range(3):
                self.check_if_connected()
                try:
                    self._cur.execute(sql, data)
                    break
                except mysql.connector.Error as err:
                    if getattr(err, "errno", None) == 1062 or "duplicate entry" in str(err).lower():
                        raise DuplicateKeyError(f"Duplicate entry for primary key in table '{table.table_name}': {err}") from err
                    if isinstance(err, (mysql.connector.OperationalError, mysql.connector.InterfaceError)):
                        if self._in_txn:
                            raise
                        self.close()
                        if attempt == 2:
                            raise
                        time.sleep(0.2 * (attempt + 1))
                        continue
                    logger.error(
                        f"Database error executing execute on table '{table.table_name}': {err}. "
                        f"Row data: {data}"
                    )
                    raise
                except (OSError, AttributeError):
                    if self._in_txn:
                        raise
                    self.close()
                    if attempt == 2:
                        raise
                    time.sleep(0.2 * (attempt + 1))

        if self._in_txn:
            return
        self.check_if_connected()
        try:
            self.cnx.commit()
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) == 1062 or "duplicate entry" in str(err).lower():
                raise DuplicateKeyError(f"Duplicate entry for primary key in table '{table.table_name}': {err}") from err
            raise

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
            2. If `data` is a batch tuple, dynamically chunks rows by count and byte size.
            3. On OperationalError 1153 (max_allowed_packet), bisects chunks into halves recursively.
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

        has_large_cols = any(
            col[1].is_large
            for col in table.init_columns
        ) or table.table_name in ("m_tag_code", "m_commit", "m_credits_entry")

        max_rows = 500 if has_large_cols else 1000
        max_bytes = 4 * 1024 * 1024 if has_large_cols else 8 * 1024 * 1024

        def _estimate_row_bytes(row: Sequence[Any]) -> int:
            return sum(len(x) if isinstance(x, (str, bytes, bytearray, memoryview)) else 8 for x in row)

        def _exec_update_chunk(chunk_data: Sequence[tuple[SafeDataType, ...]]) -> None:
            if not chunk_data:
                return
            for attempt in range(3):
                self.check_if_connected()
                try:
                    self._cur.executemany(sql, chunk_data)
                    return
                except mysql.connector.Error as err:
                    if (getattr(err, "errno", None) == 1153 or "max_allowed_packet" in str(err).lower()) and len(chunk_data) > 1:
                        mid = len(chunk_data) // 2
                        logger.warning(
                            f"Packet exceeded max_allowed_packet updating '{table.table_name}' with {len(chunk_data)} rows. "
                            f"Bisecting into chunks of {mid} and {len(chunk_data) - mid} rows..."
                        )
                        _exec_update_chunk(chunk_data[:mid])
                        _exec_update_chunk(chunk_data[mid:])
                        return
                    if isinstance(err, (mysql.connector.OperationalError, mysql.connector.InterfaceError)):
                        if self._in_txn:
                            raise
                        self.close()
                        if attempt == 2:
                            raise
                        time.sleep(0.2 * (attempt + 1))
                        continue
                    logger.error(
                        f"Database error executing executemany update on table '{table.table_name}': {err}. "
                        f"Batch size: {len(chunk_data)}. First sample row: {chunk_data[0] if chunk_data else None}"
                    )
                    raise
                except (OSError, AttributeError):
                    if self._in_txn:
                        raise
                    self.close()
                    if attempt == 2:
                        raise
                    time.sleep(0.2 * (attempt + 1))

        if isinstance(data[0], (tuple, list)):
            current_chunk = []
            current_bytes = 0
            for row in data:
                r_bytes = _estimate_row_bytes(row)
                if current_chunk and (len(current_chunk) >= max_rows or (current_bytes + r_bytes > max_bytes)):
                    _exec_update_chunk(current_chunk)
                    current_chunk = [row]
                    current_bytes = r_bytes
                else:
                    current_chunk.append(row)
                    current_bytes += r_bytes
            if current_chunk:
                _exec_update_chunk(current_chunk)
        else:
            for attempt in range(3):
                self.check_if_connected()
                try:
                    self._cur.execute(sql, data)
                    break
                except mysql.connector.Error as err:
                    if isinstance(err, (mysql.connector.OperationalError, mysql.connector.InterfaceError)):
                        if self._in_txn:
                            raise
                        self.close()
                        if attempt == 2:
                            raise
                        time.sleep(0.2 * (attempt + 1))
                        continue
                    logger.error(
                        f"Database error executing execute update on table '{table.table_name}': {err}. "
                        f"Row data: {data}"
                    )
                    raise
                except (OSError, AttributeError):
                    if self._in_txn:
                        raise
                    self.close()
                    if attempt == 2:
                        raise
                    time.sleep(0.2 * (attempt + 1))

        if not self._in_txn:
            self.check_if_connected()
            self.cnx.commit()

    def commit_tables_parallel(
        self,
        tables_data: Sequence[tuple[Table, Sequence[tuple[SafeDataType, ...]], Sequence[tuple[SafeDataType, ...]]]],
        max_workers: int | None = None,
        dedicated: bool = False,
    ) -> list[TableCommitStat]:
        """Commit inserts and updates for multiple tables concurrently across worker connection threads.

        Args:
            tables_data: Sequence of tuples `(table, insert_payload, update_payload)`.
            max_workers: Maximum concurrent database worker threads (defaults to min(len(tables_data), 8)).
            dedicated: Never use this instance's connection, even for a single table (set when the call runs on a
                background writer thread while the owner keeps using its connection).

        Process:
            1. Filters non-empty table payloads.
            2. If single table (and not `dedicated`), executes directly on current connection.
            3. Otherwise dispatches concurrently via ThreadPoolExecutor. Each table is written by its own MariaDB
               connection as one transaction (`transaction()`); a lost connection rolls that table back and the
               table is written again on a new connection (3 attempts).
            4. If any table fails, the first error is raised once every table has finished, with
               `failed_table_ids` naming all tables that were not committed (the others are).

        Outputs:
            One `TableCommitStat` per written table (rows inserted/updated, seconds).
        """
        valid_items = [item for item in tables_data if item[1] or item[2]]
        if not valid_items:
            return []

        if len(valid_items) == 1 and not dedicated:
            table, insert_data, update_data = valid_items[0]
            t_start = time.perf_counter()
            try:
                if insert_data:
                    self.insert(table, insert_data)
                if update_data:
                    self.update(table, update_data)
            except Exception as e:
                e.failed_table_ids = {table.table_id}
                raise
            return [TableCommitStat(table.table_name, len(insert_data), len(update_data), time.perf_counter() - t_start)]

        from concurrent.futures import ThreadPoolExecutor
        workers = min(len(valid_items), max_workers or 8)
        transient = (mysql.connector.OperationalError, mysql.connector.InterfaceError, ConnectionError, OSError)

        def _commit_table(
            item: tuple[Table, Sequence[tuple[SafeDataType, ...]], Sequence[tuple[SafeDataType, ...]]],
        ) -> TableCommitStat:
            table, insert_data, update_data = item
            t_start = time.perf_counter()
            for attempt in range(3):
                worker_db = None
                try:
                    from db_engine.DBHandling import MariaDB  # the composed driver class (mixins cannot import it at load)
                    worker_db = MariaDB()
                    with worker_db.transaction():
                        if insert_data:
                            worker_db.insert(table, insert_data)
                        if update_data:
                            worker_db.update(table, update_data)
                    break
                except transient as e:
                    if attempt == 2:
                        raise
                    logger.warning(
                        f"Parallel commit of '{table.table_name}' lost its connection ({e}); "
                        f"rolled back, writing it again (attempt {attempt + 2}/3)..."
                    )
                    time.sleep(0.5 * (attempt + 1))
                finally:
                    if worker_db is not None:
                        worker_db.close()
            return TableCommitStat(table.table_name, len(insert_data), len(update_data), time.perf_counter() - t_start)

        stats: list[TableCommitStat] = []
        failed: list[tuple[Table, BaseException]] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [(item[0], executor.submit(_commit_table, item)) for item in valid_items]
            for table, future in futures:
                try:
                    stats.append(future.result())
                except Exception as e:
                    failed.append((table, e))
        if failed:
            for table, e in failed:
                logger.error(f"Failed parallel commit on table '{table.table_name}': {e}")
            first = failed[0][1]
            first.failed_table_ids = {table.table_id for table, _ in failed}
            raise first
        return stats

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
            1. Checks template cache for parameterized SELECT query matching non-None mask.
            2. Executes query with filtered parameter tuple.
            3. Returns first matching row.

        Outputs:
            Matching row tuple `tuple[SafeDataType, ...]` or None.
        """
        self.check_if_connected()
        mask = tuple(val is not None for val in data)
        cache_key = (table.table_id, mask)
        sql = self._select_sql_cache.get(cache_key)
        if sql is None:
            where_clauses = [f"`{table.init_columns[x][0]}`=%s" for x, is_set in enumerate(mask) if is_set]
            sql = f"SELECT * FROM `{table.table_name}`"
            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)
            sql += " LIMIT 1"
            self._select_sql_cache[cache_key] = sql

        params = tuple(val for val in data if val is not None)
        self._cur.execute(sql, params)
        res = self._cur.fetchone()
        if res is None:
            return None
        return self._normalize_table_row(table, res)

    @staticmethod
    def _normalize_table_row(table: Table, row: Sequence[Any]) -> tuple[SafeDataType, ...]:
        """`row` of `table` with every BINARY / VARBINARY / BLOB value as `bytes`."""
        bin_idx = [i for i, col in enumerate(table.init_columns) if _is_binary_type(col[1])]
        if not bin_idx and not any(isinstance(v, (bytearray, memoryview)) for v in row):
            return tuple(row)
        out = list(row)
        for i in bin_idx:
            if i < len(out) and out[i] is not None:
                out[i] = _coerce_bytes(out[i])
        return tuple(bytes(v) if isinstance(v, (bytearray, memoryview)) else v for v in out)

    def select_in(self, table: Table, col_idx: int, values: Sequence[SafeDataType]) -> list[tuple[SafeDataType, ...]]:
        """Fetch every row of `table` whose column `col_idx` is one of `values` (`IN` queries of 1000 values)."""
        uniq = list(dict.fromkeys(v for v in values if v is not None))
        if not uniq:
            return []
        col = table.init_columns[col_idx][0]
        rows: list[tuple[SafeDataType, ...]] = []
        self.check_if_connected()
        for start in range(0, len(uniq), 1000):
            chunk = uniq[start:start + 1000]
            self._cur.execute(
                f"SELECT * FROM `{table.table_name}` WHERE `{col}` IN ({','.join(['%s'] * len(chunk))})",  # noqa: S608
                tuple(chunk),
            )
            rows.extend(self._normalize_table_row(table, r) for r in self._cur.fetchall())
        return rows

    @staticmethod
    def _version_predicate(table: Table) -> str | None:
        """The `WHERE` clause restricting `table` to the versions from one `min_vid` on, or None if it has none.

        `min_vid` is a commit id, so blob ids (another id space) are never compared with it.
        """
        col_names = [col[0] for col in table.init_columns]
        if "vid_s" in col_names and "vid_e" in col_names:
            return "(`vid_e` = 0 OR `vid_e` >= %s)"
        if "vid" in col_names:
            return "`vid` >= %s"
        if "vid_s" in col_names:
            return "`vid_s` >= %s"
        if "commit_id" in col_names:
            return "`commit_id` >= %s"
        return None

    def table_size(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> int:
        """Size of `table` in bytes, from the server's catalog statistics, scaled to a projection and window.

        Args:
            table: Target Table schema instance.
            cached_columns: Optional 0-indexed column positions the caller would keep. A projection narrower
                than the table returns the share those columns are estimated to take
                (`estimate_column_bytes()` per row), never more than the table's own size.
            min_vid: Optional minimum active version ID. The size is then that of the rows
                `select_preload()` would return for it, counted over the table's version column.

        Process:
            Reads `DATA_LENGTH + INDEX_LENGTH` and `TABLE_ROWS` for the table from `information_schema.TABLES`
            (both InnoDB estimates, so the result is an approximation). With `min_vid` the rows of the window
            are counted (`SELECT COUNT(*) ... WHERE <version predicate>`, an index range scan over the window
            the caller is about to read anyway) and the size is scaled to them.

        Outputs:
            Size in bytes, or 0 if the table is not in the catalog (not created yet).
        """
        self.check_if_connected()
        self._cur.execute(
            "SELECT COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0), COALESCE(TABLE_ROWS, 0) "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s;",
            (table.table_name,),
        )
        catalog = self._cur.fetchone()
        if not catalog:
            return 0
        stored_bytes, row_count = int(catalog[0] or 0), int(catalog[1] or 0)
        if not stored_bytes:
            return 0

        predicate = self._version_predicate(table) if (min_vid is not None and min_vid > 0) else None
        if predicate is not None and row_count > 0:
            self._cur.execute(
                f"SELECT COUNT(*) FROM `{table.table_name}` WHERE {predicate};",  # noqa: S608
                (min_vid,),
            )
            window = self._cur.fetchone()
            # A window wider than the catalog's row estimate is the estimate being stale, not a bigger table
            row_count = min(row_count, int(window[0] or 0)) if window else row_count
            stored_bytes = min(stored_bytes, int(stored_bytes * row_count / max(int(catalog[1] or 0), 1)))

        if not cached_columns or len(cached_columns) >= table.length:
            return stored_bytes
        projected = row_count * sum(estimate_column_bytes(table.init_columns[i]) for i in cached_columns)
        return min(stored_bytes, projected)

    def select_preload(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> list[tuple[SafeDataType, ...]]:
        """Query records for TableEngine startup preloading with column projection and version filtering.

        Args:
            table: Target Table schema instance.
            cached_columns: Optional tuple of 0-indexed column positions to select.
            min_vid: Optional minimum active version ID for version-scoped tables.

        Outputs:
            List of row tuples containing selected columns in requested order.
        """
        self.check_if_connected()
        has_min_vid = min_vid is not None and min_vid > 0
        cache_key = (table.table_id, cached_columns, has_min_vid)
        sql = self._preload_sql_cache.get(cache_key)
        if sql is None:
            # 1. Determine SELECT column clause
            if cached_columns is not None and len(cached_columns) < table.length:
                cols_clause = ", ".join(f"`{table.init_columns[i][0]}`" for i in cached_columns)
            else:
                cols_clause = "*"

            sql = f"SELECT {cols_clause} FROM `{table.table_name}`"  # noqa: S608
            where_clauses = []

            # 2. Determine version filter WHERE clause
            if has_min_vid:
                predicate = self._version_predicate(table)
                if predicate is not None:
                    where_clauses.append(predicate)

            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)
            self._preload_sql_cache[cache_key] = sql

        params = (min_vid,) if has_min_vid else ()
        self._cur.execute(sql, params)
        rows = self._cur.fetchall()
        if not rows:
            return []

        # Identify binary columns in projected output
        target_cols = [table.init_columns[i] for i in cached_columns] if cached_columns is not None else table.init_columns
        binary_indices = {
            i for i, col in enumerate(target_cols)
            if _is_binary_type(col[1])
        }

        _normalize_binary_val = _coerce_bytes

        # Optimized fast-path for single-column binary projection (e.g. m_tag_code)
        if len(target_cols) == 1 and 0 in binary_indices:
            return [
                (_normalize_binary_val(row[0]),)
                if row[0] is not None else (None,)
                for row in rows
            ]

        if binary_indices or any(isinstance(val, (bytearray, memoryview)) for r in rows[:min(len(rows), 10)] for val in r):
            return [
                tuple(
                    _normalize_binary_val(val)
                    if (idx in binary_indices or isinstance(val, (bytearray, memoryview))) and val is not None
                    else val
                    for idx, val in enumerate(row)
                )
                for row in rows
            ]
        return [tuple(row) for row in rows]
