"""db_engine/base.py - Base Abstract Database Engine Interface Contract.

Defines the core database backend API expected across KernelInfo-Parser,
Table Engine (TEDirectDB), and ChangeSet execution.
"""
from __future__ import annotations

import contextlib
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Generator, Iterator, Sequence
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any, ContextManager, Literal, Mapping, NamedTuple, Self

from core.globalstuff import JoinsType, PointerType, SafeDataType
from db_engine.spec import (
    Agg,
    AnyOf,
    Col,
    Cond,
    Const,
    Exists,
    Inc,
    InSelect,
    Join,
    Select,
    Source,
    Value,
    Where,
)

if TYPE_CHECKING:
    from core.TableHandling import Table

logger = logging.getLogger("kernelinfo.db_engine")


class ColumnInfo(NamedTuple):
    """Metadata describing a catalog column."""

    data_type: str
    column_type: str
    octet_length: int | None
    nullable: bool


class TableStats(NamedTuple):
    """Catalog table size and row count statistics."""

    name: str
    rows: int
    data_bytes: int
    index_bytes: int


class NewPairs(ABC):
    """Interface for temporary or anti-joined new version (fnid, blob_id) pairs."""

    pairs: set[tuple[int, int]]

    @abstractmethod
    def delete_rows_of(self, table: Table | str) -> int:
        """Delete rows from table where (source_fnid, source_blob_id) is one of the new pairs."""

    @abstractmethod
    def insert_include_references(
        self,
        kconfig_type_ids: Sequence[int] | str,
        kconfig_ref_type: int,
        include_ref_type: int,
    ) -> int:
        """Insert include and Kconfig references from new pairs into m_file_reference."""


class TableCommitStat(NamedTuple):
    """Rows one `commit_tables_parallel()` call wrote to one table, and the seconds that table took."""

    table_name: str
    inserted: int
    updated: int
    seconds: float


def estimate_column_bytes(col_def: Sequence[str]) -> int:
    """Stored width in bytes of one column, from its DDL definition (`("hash", "BINARY(32)", "NOT NULL")`).

    Fixed-width types are exact; a `CHAR`/`BINARY` column is taken at its declared length and a
    `TEXT`/`BLOB` column at a nominal average, which is what makes a projected table size an estimate.
    """
    sql_type = str(col_def[1]).upper()
    declared_match = re.search(r"\((\d+)", sql_type)
    declared = int(declared_match.group(1)) if declared_match else 0
    if "TINYINT" in sql_type or "BOOL" in sql_type:
        return 1
    if "SMALLINT" in sql_type:
        return 2
    if "MEDIUMINT" in sql_type:
        return 3
    if "BIGINT" in sql_type or "DOUBLE" in sql_type or "DATETIME" in sql_type or "TIMESTAMP" in sql_type:
        return 8
    if "INT" in sql_type or "FLOAT" in sql_type or "DATE" in sql_type:
        return 4
    if "LONGTEXT" in sql_type or "LONGBLOB" in sql_type:
        return 4096
    if "MEDIUMTEXT" in sql_type or "MEDIUMBLOB" in sql_type:
        return 2048
    if "TEXT" in sql_type or "BLOB" in sql_type:
        return 512
    if "BINARY" in sql_type or "CHAR" in sql_type:
        return max(declared, 1) + (2 if "VAR" in sql_type else 0)
    return max(declared, 8)


class BaseDBEngine(ABC):
    """Abstract Base Class for database backend driver implementations.

    Driver Contract & Architecture:
        - Connection: Drivers manage active connection/cursor handles, auto-reconnecting on dropped sockets.
        - Transactions: DDL and DML operations auto-commit; context manager rolls back on unhandled error.
        - Wildcard Matching: Tuple queries treat `None` values as wildcards (omitted from WHERE filters).
        - Joins: Multi-table join queries map columns in joined order, chunking graphs > 50 tables into slices of <= 30.
        - Background commits: a driver whose `commit_tables_parallel(..., dedicated=True)` uses only connections of
          its own sets `supports_background_commit = True`, so the TableEngine may run it on a writer thread.
    """

    supports_background_commit = False

    @abstractmethod
    def __init__(self) -> None:
        """Initialize database engine parameters and session.

        Args:
            None (reads driver configuration from environment variables or defaults).
        Process:
            Establishes SQL connection handle, creates cursor instance, sets session parameters
            (`sql_mode = 'NO_AUTO_VALUE_ON_ZERO'`, connection timeouts, max allowed packet size).
        Outputs:
            None.
        Raises:
            Exception: If connection or session initialization fails.
        """

    @abstractmethod
    def __enter__(self) -> Self:
        """Enter context manager scope (`with db:`).

        Args:
            None.
        Process:
            Verifies active connection health; auto-reconnects if disconnected.
        Outputs:
            Self: Active database driver instance.
        """

    @abstractmethod
    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Exit context manager scope, rolling back pending transaction on unhandled error.

        Args:
            exception_type: Exception class raised within block, or None.
            exception_value: Exception instance raised within block, or None.
            exception_traceback: Traceback object, or None.
        Process:
            If exception occurred (`exception_type is not None`), executes rollback. Does not suppress exceptions.
        Outputs:
            None.
        """

    @abstractmethod
    def close(self) -> None:
        """Safely release connection and cursor resources.

        Args:
            None.
        Process:
            Closes cursor and connection socket handles, suppressing teardown errors, and resets handles to None.
        Outputs:
            None.
        """

    @abstractmethod
    def get_next_id(self, table: Table) -> int:
        """Query next sequence integer ID for target table.

        Args:
            table (Table): Target Table schema instance (`table.table_name`, `table.init_columns[0][0]`).
        Process:
            Queries `COALESCE(MAX(pk), 0) + 1` on primary key column.
        Outputs:
            int: Next available sequential integer ID (returns 1 if table is empty).
        """

    def reserve_id_block(
        self,
        table: Table,
        block_size: int,
        commit_id: int = 0,
    ) -> tuple[int, int]:
        """Reserve an atomic block of primary key IDs [start_id, end_id] for target table."""
        start_id = self.get_next_id(table)
        return (start_id, start_id + block_size - 1)

    @abstractmethod
    def select(self, table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None:
        """Execute single-row SELECT matching non-None column filter values.

        Args:
            table (Table): Target Table schema instance.
            data (tuple[SafeDataType, ...]): Row filter tuple of length `table.length`. Non-None values
                are exact equality match filters (`col = %s`); None values act as wildcards (ignored).
        Process:
            Executes `SELECT * FROM table WHERE <non_none_cols = %s> LIMIT 1`.
        Outputs:
            tuple[SafeDataType, ...] | None: Matching row tuple of length `table.length`, or None if no match.
        """

    @abstractmethod
    def view_select(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Execute joined single-row SELECT across relational join graph with wildcard column filtering.

        Args:
            tables (Sequence[Table] | dict[int, Table]): Collection of Table schema objects indexed by ID.
            joins (JoinsType): Join graph tuple `(((src_tbl, src_col), (tgt_tbl, tgt_col), repeat_count), ...)`.
            columns (tuple[SafeDataType, ...]): Flat concatenated column filter tuple spanning all joined tables.
                Non-None values are exact equality filters; None values act as wildcards.
        Process:
            Builds multi-table JOIN query (`SELECT * FROM t1 A1 JOIN t2 A2 ON ... WHERE ... LIMIT 1`).
            If joined table count > 50, chunks query into <= 30 tables per step.
        Outputs:
            tuple[SafeDataType, ...] | None: Flat concatenated row tuple across all joined tables, or None if no match.
        """

    @abstractmethod
    def view_select_multiple(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> list[tuple[SafeDataType, ...]]:
        """Execute joined multi-row SELECT across relational join graph with wildcard filtering, returning all matches.

        Args:
            tables (Sequence[Table] | dict[int, Table]): Collection of Table schema objects indexed by ID.
            joins (JoinsType): Join graph tuple `(((src_tbl, src_col), (tgt_tbl, tgt_col), repeat_count), ...)`.
            columns (tuple[SafeDataType, ...]): Flat concatenated column filter tuple spanning all joined tables.
                Non-None values are exact equality filters; None values act as wildcards.
        Process:
            Builds multi-table JOIN query (`SELECT * FROM t1 A1 JOIN t2 A2 ON ... WHERE ...`) and fetches all rows.
            If joined table count > 50, chunks query into <= 30 tables per step.
        Outputs:
            list[tuple[SafeDataType, ...]]: List of flat concatenated row tuples for all matching records.
        """

    def select_in(self, table: Table, col_idx: int, values: Sequence[SafeDataType]) -> list[tuple[SafeDataType, ...]]:
        """Fetch every row of `table` whose column `col_idx` is one of `values`.

        Default implementation: one `view_select_multiple()` per value; drivers override it with an `IN` query.
        """
        rows: list[tuple[SafeDataType, ...]] = []
        joins: JoinsType = (((table.table_id, 0),),)
        for val in dict.fromkeys(values):
            cols = tuple(val if i == col_idx else None for i in range(table.length))
            rows.extend(self.view_select_multiple({table.table_id: table}, joins, cols))
        return rows

    @abstractmethod
    def select_preload(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> list[tuple[SafeDataType, ...]]:
        """Query records for TableEngine startup preloading with column projection and version filtering.

        Args:
            table (Table): Target Table schema instance.
            cached_columns (tuple[int, ...] | None): Optional tuple of 0-indexed column positions to select.
                If None or covering all columns, selects all columns.
            min_vid (int | None): Optional minimum active version ID for version-scoped tables.

        Process:
            Constructs a SELECT query requesting only `cached_columns` (e.g. `SELECT col1, col2 FROM table`)
            and applies a version-scoped predicate (`WHERE vid >= min_vid` or `WHERE (vid_e = 0 OR vid_e >= min_vid)`)
            if `min_vid > 0`.

        Outputs:
            list[tuple[SafeDataType, ...]]: List of row tuples containing the selected columns in requested order.
        """


    def table_size(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> int:
        """Bytes of `table` a caller holding `cached_columns` of the rows from `min_vid` on would be sized by.

        Args:
            table (Table): Target Table schema instance.
            cached_columns (tuple[int, ...] | None): Optional tuple of 0-indexed column positions the caller
                would keep. When it covers fewer columns than the table has, the returned size is the share
                those columns are estimated to take (`estimate_column_bytes()` per row), capped by the
                table's full size; otherwise the table's full size is measured.
            min_vid (int | None): Optional minimum active version ID. Scales the size down to the rows
                `select_preload()` would return for it, so a version-scoped table is sized by its working
                window instead of its whole history.

        Process:
            Reads the size the backend reports for the table (data plus indexes) and scales it to the
            requested column projection and version window. Only the window needs counting; the table's
            bytes and row count come from the backend's own bookkeeping, never from a row scan.

        Outputs:
            int: Size in bytes, or 0 when the backend cannot report one (an unknown size is never a ceiling
                violation, so callers that cap what they cache - `TECachedDB.cache_max_bytes` - cache as configured).

        Note:
            Drivers reporting a size from catalog statistics (`information_schema.TABLES` on InnoDB) return an
            estimate, not an exact byte count.
        """
        return 0

    @abstractmethod
    def insert(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        """Insert row(s) into database table matching schema.

        Args:
            table (Table): Target Table schema instance.
            data (tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]): Single row tuple or batch tuple
                of row tuples matching `table.length` and schema column order.
        Process:
            Constructs `INSERT INTO table VALUES (%s, ...)`. Chunks batches into 1000-row slices, executes
            with auto-retry on dropped socket, and commits.
        Outputs:
            None.
        """

    @abstractmethod
    def update(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        """Update/upsert row(s) in database table using primary key conflict resolution.

        Args:
            table (Table): Target Table schema instance.
            data (tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...]): Single row tuple or batch tuple
                of row tuples matching `table.length` and schema column order.
        Process:
            Constructs `INSERT INTO table VALUES (...) ON DUPLICATE KEY UPDATE col=VALUES(col)` for all non-primary
            key columns. Chunks into 1000-row slices, executes with auto-retry, and commits.
        Outputs:
            None.
        """

    @abstractmethod
    def commit_tables_parallel(
        self,
        tables_data: Sequence[tuple[Table, Sequence[tuple[SafeDataType, ...]], Sequence[tuple[SafeDataType, ...]]]],
        max_workers: int | None = None,
        dedicated: bool = False,
    ) -> list[TableCommitStat]:
        """Commit inserts and updates for multiple tables concurrently across worker connection threads.

        Args:
            tables_data: Sequence of tuples `(table, insert_payload, update_payload)`.
            max_workers: Maximum concurrent database worker threads (defaults to min(len(tables_data), G.CPUS)).
            dedicated: Use only connections opened for this call, never the instance's own (background writers).
        Process:
            Executes table insert/update operations in parallel across isolated database connections and commits,
            each table atomically. On failure the first error is raised after all tables finished, carrying
            `failed_table_ids` (the tables not committed; every other table is).
        Outputs:
            One `TableCommitStat` per written table.
        """

    @abstractmethod
    def create_table(self, tables: Sequence[Table] | Table) -> None:
        """Create database tables from Table schema specifications and seed initial records.

        Args:
            tables (Sequence[Table] | Table): Single Table instance or sequence of Table instances.
        Process:
            Translates `table.init_columns`, `table.init_primary`, `table.init_foreign` into DDL statements,
            executes `CREATE TABLE`, inserts `table.initial_insert` seed rows if present, and commits.
        Outputs:
            None.
        """

    @abstractmethod
    def drop_table(self, tables: Sequence[Table] | Table) -> None:
        """Drop database tables, safely disabling foreign key checks during drop.

        Args:
            tables (Sequence[Table] | Table): Single Table instance or sequence of Table instances.
        Process:
            Disables foreign key checks (`SET FOREIGN_KEY_CHECKS = 0;`), executes `DROP TABLE IF EXISTS <tables>`,
            commits, and restores foreign key checks (`SET FOREIGN_KEY_CHECKS = 1;`) in a finally block.
        Outputs:
            None.
        """

    @abstractmethod
    def index_exists(self, index_name: str, table: Table) -> bool:
        """Check if an index exists on target table in database catalog.

        Args:
            index_name (str): Identifier name for index.
            table (Table): Target Table schema instance.
        Outputs:
            bool: True if index exists, False otherwise.
        """

    @abstractmethod
    def create_index(self, index_name: str, table: Table, rows: tuple[PointerType, ...]) -> None:
        """Create composite or single-column index on target table if not already existing.

        Args:
            index_name (str): Identifier name for index.
            table (Table): Target Table schema instance.
            rows (tuple[PointerType, ...]): Tuple of pointer tuples `(table_id, col_idx)` specifying indexed columns.
        Process:
            Checks for existing index, maps column indices to names, executes `CREATE INDEX <index_name> ON <table> (<cols>)`, and commits.
        Outputs:
            None.
        """

    @abstractmethod
    def remove_index(self, index_name: str, table: Table) -> None:
        """Drop existing index from target table if it exists.

        Args:
            index_name (str): Name of index to remove.
            table (Table): Target Table schema instance.
        Process:
            Checks for existing index, executes `ALTER TABLE <table> DROP INDEX <index_name>`, and commits.
        Outputs:
            None.
        """

    @abstractmethod
    def create_indexes(
        self,
        indexes: Sequence[tuple[str, Table, tuple[PointerType, ...]]],
        max_workers: int | None = None,
    ) -> None:
        """Create multiple database indexes in parallel across dedicated worker connections.

        Args:
            indexes (Sequence[tuple[str, Table, tuple[PointerType, ...]]]): Sequence of index specifications `(index_name, table, rows)`.
            max_workers (int | None): Maximum concurrent worker threads (default 8).
        Outputs:
            None.
        """

    @abstractmethod
    def remove_indexes(
        self,
        indexes: Sequence[tuple[str, Table]],
        max_workers: int | None = None,
    ) -> None:
        """Remove multiple database indexes in parallel across dedicated worker connections.

        Args:
            indexes (Sequence[tuple[str, Table]]): Sequence of index specifications `(index_name, table)`.
            max_workers (int | None): Maximum concurrent worker threads (default 8).
        Outputs:
            None.
        """

    @abstractmethod
    def test_tables(self, tables: Sequence[Table] | Table) -> list[str] | None:
        """Check for existence of registered schema tables in database catalog.

        Args:
            tables (Sequence[Table] | Table): Single Table instance or sequence of Table instances.
        Process:
            Queries catalog (`SHOW TABLES`), compares against registered tables, and identifies missing table names.
        Outputs:
            list[str] | None: List of missing table names, or None if all registered tables exist.
        """

    @abstractmethod
    def verify_relational_integrity(self, tables: Sequence[Table]) -> dict[str, int]:
        """Verify relational foreign key integrity across tables and return any orphan record counts.

        Args:
            tables (Sequence[Table]): Sequence of registered schema tables.
        Outputs:
            dict[str, int]: Dictionary mapping constraint names to orphan record counts (empty if all valid).
        """

    def commit(self) -> None:
        """Commit pending transaction if driver is in transactional mode."""

    def rollback(self) -> None:
        """Roll back pending transaction if driver is in transactional mode."""

    def cursor(self, dictionary: bool = False) -> Any:
        """Lease a cursor handle on the underlying database connection."""
        if hasattr(self, "cnx") and getattr(self, "cnx", None):
            return self.cnx.cursor(dictionary=dictionary)
        raise NotImplementedError("Driver does not provide raw cursor handle")


    def query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> list[dict[str, Any] | tuple[SafeDataType, ...]]:
        """Execute a SQL SELECT/query and return all result rows."""
        raise NotImplementedError(f"{type(self).__name__} does not implement query")

    def query_one(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> dict[str, Any] | tuple[SafeDataType, ...] | None:
        """Execute a SQL SELECT/query and return the first result row, or None if no match."""
        rows = self.query(sql, params, dictionary=dictionary)
        return rows[0] if rows else None

    def query_iter(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
        chunk_size: int = 2000,
    ) -> Iterator[dict[str, Any] | tuple[SafeDataType, ...]]:
        """Stream query result rows in batches of chunk_size."""
        yield from self.query(sql, params, dictionary=dictionary)

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Execute an arbitrary SQL DML/DDL statement."""
        raise NotImplementedError(f"{type(self).__name__} does not implement execute")

    def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
        """Execute a parameterized SQL statement repeatedly across a sequence of parameter tuples."""
        count = 0
        for p in params_seq:
            count += self.execute(sql, p)
        return count

    def delete(
        self,
        table: Table | str,
        where: str,
        params: Sequence[Any] | None = None,
    ) -> int:
        """Delete records matching parameterized WHERE condition."""
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        sql = f"DELETE FROM {tbl_name} WHERE {where}"
        return self.execute(sql, params)

    def table_exists(self, table_name: str) -> bool:
        """Check whether a table or view exists in the current catalog."""
        rows = self.query(
            "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s LIMIT 1",
            (table_name,),
            dictionary=False,
        )
        return bool(rows)

    def create_view(self, view_name: str, select_sql: str, replace: bool = True) -> None:
        """Create or replace a database view safely."""
        stmt = f"CREATE {'OR REPLACE ' if replace else ''}VIEW {view_name} AS {select_sql}"
        self.execute(stmt)

    def drop_view(self, view_name: str, if_exists: bool = True) -> None:
        """Drop a database view."""
        stmt = f"DROP VIEW {'IF EXISTS ' if if_exists else ''}{view_name}"
        self.execute(stmt)

    def add_column_if_missing(self, table_name: str, column_name: str, column_def: str) -> bool:
        """Add column to table if not already present."""
        rows = self.query(
            "SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s LIMIT 1",
            (table_name, column_name),
            dictionary=False,
        )
        if not rows:
            col_spec = column_def if column_def.strip().startswith(column_name) else f"{column_name} {column_def}"
            self.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_spec}")
            return True
        return False

    # -------------------------------------------------------------------------
    # Transaction Management (§3.6)
    # -------------------------------------------------------------------------
    @contextlib.contextmanager
    def transaction(self) -> Iterator[Self]:
        """Run operations within an atomic transaction, committed on exit or rolled back on error."""
        yield self

    # -------------------------------------------------------------------------
    # Read Primitives (§2)
    # -------------------------------------------------------------------------
    def select_rows(
        self,
        source: Source,
        columns: Sequence[str | Agg | Const] | Mapping[str, str | Agg | Const],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        limit: int | None = None,
        offset: int | None = None,
        as_dict: bool | None = None,
    ) -> list[tuple | dict]:
        """Execute parameterized SELECT query from spec and return rows."""
        raise NotImplementedError(f"{type(self).__name__} does not implement select_rows")

    def iter_rows(
        self,
        source: Source,
        columns: Sequence[str | Agg | Const] | Mapping[str, str | Agg | Const],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        limit: int | None = None,
        offset: int | None = None,
        as_dict: bool | None = None,
    ) -> Iterator[tuple | dict]:
        """Stream query result rows."""
        rows = self.select_rows(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            offset=offset,
            as_dict=as_dict,
        )
        yield from rows

    def select_one(
        self,
        source: Source,
        columns: Sequence[str | Agg | Const] | Mapping[str, str | Agg | Const],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        as_dict: bool | None = None,
    ) -> tuple | dict | None:
        """Fetch first matching row or None."""
        rows = self.select_rows(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=1,
            as_dict=as_dict,
        )
        return rows[0] if rows else None

    def select_values(
        self,
        source: Source,
        column: str,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: bool = False,
        order_by: Sequence[str] = (),
        limit: int | None = None,
    ) -> list:
        """Fetch a list of single column values."""
        rows = self.select_rows(
            source,
            [column],
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            as_dict=False,
        )
        return [r[0] for r in rows]

    def select_value(
        self,
        source: Source,
        column: str,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        order_by: Sequence[str] = (),
        default: Any = None,
    ) -> Any:
        """Fetch single scalar value or default."""
        vals = self.select_values(
            source,
            column,
            where=where,
            joins=joins,
            order_by=order_by,
            limit=1,
        )
        return vals[0] if vals else default

    def count_rows(
        self,
        source: Source,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: str | None = None,
    ) -> int:
        """Count matching rows."""
        agg = Agg("COUNT", distinct if distinct else "*", distinct=bool(distinct))
        rows = self.select_rows(
            source,
            [agg],
            where=where,
            joins=joins,
            as_dict=False,
        )
        if rows and rows[0] and rows[0][0] is not None:
            return int(rows[0][0])
        return 0

    def row_exists(
        self,
        source: Source,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
    ) -> bool:
        """Return True if at least one matching row exists."""
        rows = self.select_rows(
            source,
            [Const(1)],
            where=where,
            joins=joins,
            limit=1,
            as_dict=False,
        )
        return bool(rows)

    def aggregate(
        self,
        source: Source,
        aggregates: Mapping[str, Agg],
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        group_by: Sequence[str] = (),
        order_by: Sequence[str] = (),
        as_dict: bool | None = None,
    ) -> list:
        """Compute aggregate calculations, optionally grouped by columns."""
        cols: dict[str, str | Agg | Const] = {col: col for col in group_by}
        cols.update(aggregates)
        return self.select_rows(
            source,
            cols,
            where=where,
            joins=joins,
            order_by=order_by,
            as_dict=as_dict,
        )

    def max_value(
        self,
        source: Source,
        column: str,
        where: Where | None = None,
        *,
        default: Any = 0,
    ) -> Any:
        """Return COALESCE(MAX(column), default)."""
        agg = Agg("MAX", column)
        rows = self.select_rows(source, [agg], where=where, as_dict=False)
        if rows and rows[0] and rows[0][0] is not None:
            return rows[0][0]
        return default

    def min_value(
        self,
        source: Source,
        column: str,
        where: Where | None = None,
        *,
        default: Any = 0,
    ) -> Any:
        """Return COALESCE(MIN(column), default)."""
        agg = Agg("MIN", column)
        rows = self.select_rows(source, [agg], where=where, as_dict=False)
        if rows and rows[0] and rows[0][0] is not None:
            return rows[0][0]
        return default

    # -------------------------------------------------------------------------
    # Write Primitives (§3)
    # -------------------------------------------------------------------------
    def insert_rows(
        self,
        table: Table | str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]] | Any,
        *,
        on_conflict: Literal["error", "ignore", "update", "replace"] = "error",
        update_columns: Sequence[str] | None = None,
        chunk_size: int | None = None,
    ) -> int:
        """Insert row(s) matching columns. Returns total affected rows."""
        raise NotImplementedError(f"{type(self).__name__} does not implement insert_rows")

    def get_or_insert_id(
        self,
        table: Table | str,
        key: Mapping[str, Any],
        values: Mapping[str, Any] | None = None,
    ) -> int:
        """Fetch primary key ID by unique key; if missing, insert key | values and return ID."""
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        pk_col = getattr(table, "init_columns", [("id",)])[0][0] if hasattr(table, "init_columns") else "id"
        row = self.select_one(tbl_name, [pk_col], where=key)
        if row is not None:
            val = row[0] if isinstance(row, tuple) else row[pk_col]
            return int(val)
        row_dict = dict(key)
        if values:
            row_dict.update(values)
        cols = list(row_dict.keys())
        row_vals = [tuple(row_dict[c] for c in cols)]
        self.insert_rows(tbl_name, cols, row_vals, on_conflict="ignore")
        row = self.select_one(tbl_name, [pk_col], where=key)
        if row is not None:
            val = row[0] if isinstance(row, tuple) else row[pk_col]
            return int(val)
        raise RuntimeError(f"Failed to get or insert ID for {tbl_name} with key {key}")

    def update_rows(
        self,
        table: Table | str,
        values: Mapping[str, Any],
        where: Where | None,
        *,
        limit: int | None = None,
    ) -> int:
        """Update rows matching where filter. Returns affected row count."""
        raise NotImplementedError(f"{type(self).__name__} does not implement update_rows")

    def delete_rows(
        self,
        table: Table | str,
        where: Where | None,
        *,
        limit: int | None = None,
        all_rows: bool = False,
    ) -> int:
        """Delete rows matching where filter. Returns affected row count."""
        raise NotImplementedError(f"{type(self).__name__} does not implement delete_rows")

    def copy_rows(
        self,
        table: Table | str,
        columns: Sequence[str],
        select: Select,
        *,
        on_conflict: Literal["error", "ignore"] = "error",
    ) -> int:
        """Insert rows selected by select query into table. Returns affected rows."""
        raise NotImplementedError(f"{type(self).__name__} does not implement copy_rows")

    # -------------------------------------------------------------------------
    # Catalog and Server Introspection (§4)
    # -------------------------------------------------------------------------
    def column_exists(self, table: Table | str, column: str) -> bool:
        """Check if target column exists in table."""
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        info = self.column_info(tbl_name, column)
        return info is not None

    def existing_tables(self, names: Sequence[str], *, include_views: bool = False) -> set[str]:
        """Return subset of table names that exist in the catalog."""
        if not names:
            return set()
        type_filter = "" if include_views else " AND TABLE_TYPE = 'BASE TABLE'"
        placeholders = ", ".join(["%s"] * len(names))
        sql = (
            f"SELECT TABLE_NAME FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders}){type_filter}"
        )
        rows = self.query(sql, list(names), dictionary=False)
        return {str(r[0]) for r in rows if r and r[0]}

    def column_info(self, table: Table | str, column: str) -> ColumnInfo | None:
        """Query column metadata (data_type, column_type, octet_length, nullable)."""
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        sql = (
            "SELECT DATA_TYPE, COLUMN_TYPE, CHARACTER_OCTET_LENGTH, IS_NULLABLE "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s LIMIT 1"
        )
        rows = self.query(sql, (tbl_name, column), dictionary=False)
        if not rows or not rows[0]:
            return None
        r = rows[0]
        data_type = str(r[0]).lower() if r[0] else ""
        col_type = str(r[1]) if r[1] else ""
        octet_len = int(r[2]) if r[2] is not None else None
        nullable = (str(r[3]).upper() == "YES") if r[3] else False
        return ColumnInfo(data_type=data_type, column_type=col_type, octet_length=octet_len, nullable=nullable)

    def index_names(self, table: Table | str) -> set[str]:
        """Return set of index names on target table."""
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        sql = (
            "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
        )
        rows = self.query(sql, (tbl_name,), dictionary=False)
        return {str(r[0]) for r in rows if r and r[0]}

    def table_stats(self, names: Sequence[str] | None = None) -> list[TableStats]:
        """Return table size and row count estimates ordered by size descending."""
        if names:
            placeholders = ", ".join(["%s"] * len(names))
            where = f" AND TABLE_NAME IN ({placeholders})"
            params = list(names)
        else:
            where = ""
            params = []
        sql = (
            f"SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = DATABASE(){where} ORDER BY DATA_LENGTH + INDEX_LENGTH DESC"
        )
        rows = self.query(sql, params, dictionary=False)
        stats = []
        for r in rows:
            name = str(r[0])
            row_count = int(r[1]) if r[1] is not None else 0
            data_bytes = int(r[2]) if r[2] is not None else 0
            idx_bytes = int(r[3]) if r[3] is not None else 0
            stats.append(TableStats(name=name, rows=row_count, data_bytes=data_bytes, index_bytes=idx_bytes))
        return stats

    def server_variable(self, name: str) -> str | None:
        """Return value of server GLOBAL system variable or None."""
        clean = name.strip()
        if not re.match(r"^[A-Za-z0-9_]+$", clean):
            raise ValueError(f"Invalid server variable name: {name}")
        try:
            rows = self.query(f"SELECT @@GLOBAL.{clean}", dictionary=False)
            if rows and rows[0] and rows[0][0] is not None:
                return str(rows[0][0])
        except Exception:
            pass
        return None

    def apply_server_tuning(self, settings: dict[str, Any] | None = None) -> list[str]:
        """Apply server-level engine tuning. Base implementation returns empty list."""
        return []

    # -------------------------------------------------------------------------
    # Schema Maintenance (§5)
    # -------------------------------------------------------------------------
    def drop_tables(
        self,
        names: Sequence[str],
        *,
        if_exists: bool = True,
        disable_fk_checks: bool = True,
    ) -> None:
        """Drop tables by name safely."""
        raise NotImplementedError(f"{type(self).__name__} does not implement drop_tables")

    def ensure_columns(self, table: Table, columns: Sequence[str] | None = None) -> list[str]:
        """Add any init_columns missing from the live table and return their names."""
        added: list[str] = []
        wanted = set(columns) if columns is not None else None
        for col_def in table.init_columns:
            col_name = col_def[0]
            if wanted is not None and col_name not in wanted:
                continue
            if not self.column_exists(table.table_name, col_name):
                col_spec = " ".join(col_def)
                self.execute(f"ALTER TABLE {table.table_name} ADD COLUMN {col_spec}")
                added.append(col_name)
        if added:
            self.commit()
        return added

    def ensure_views(self, names: Sequence[str] | None = None) -> dict[str, Exception | None]:
        """Create or replace registered views in dependency order.
        
        Refuses a name that already exists as a BASE TABLE.
        """
        from db_engine.views import VIEW_DEFINITIONS, extract_select_sql
        wanted = set(names) if names is not None else None
        res: dict[str, Exception | None] = {}
        for vname, ddl in VIEW_DEFINITIONS:
            if wanted is not None and vname not in wanted:
                continue
            try:
                base_tables = self.existing_tables([vname], include_views=False)
                if vname in base_tables:
                    raise RuntimeError(f"Cannot create view '{vname}': already exists as a BASE TABLE")
                select_sql = extract_select_sql(ddl)
                self.create_view(vname, select_sql)
                res[vname] = None
            except Exception as e:
                res[vname] = e
        return res

    def resolve_relation_name(self, name: str) -> str:
        """Resolve logical relation name (e.g. m_checkpoint_tree -> m_bridge_checkpoint if view absent)."""
        if name != "m_checkpoint_tree":
            return name
        now = time.monotonic()
        cached = getattr(self, "_relation_name_cache", None)
        if cached is None:
            cached = {}
            self._relation_name_cache = cached
        hit = cached.get(name)
        if hit is not None:
            val, at = hit
            if val == "m_checkpoint_tree" or (now - at < 300.0):
                return val
        exists = self.table_exists("m_checkpoint_tree")
        res = "m_checkpoint_tree" if exists else "m_bridge_checkpoint"
        cached[name] = (res, now)
        return res

    # -------------------------------------------------------------------------
    # Pipeline Intent Methods (§7.1)
    # -------------------------------------------------------------------------
    @contextlib.contextmanager
    def new_checkpoint_pairs(
        self,
        commit_id: int,
        prev_commit_id: int,
    ) -> Iterator[NewPairs]:
        """Context manager yielding NewPairs for (fnid, blob_id) diff between commits."""
        raise NotImplementedError(f"{type(self).__name__} does not implement new_checkpoint_pairs")
        yield

    def enqueue_parse_tasks(self, rows: Sequence[Sequence[Any]]) -> int:
        """Insert or update tasks in m_parse_task. Returns count of modified/inserted rows."""
        raise NotImplementedError(f"{type(self).__name__} does not implement enqueue_parse_tasks")

    def record_task_failure(
        self,
        task_id: int,
        error_msg: str,
        *,
        worker_id: str | None = None,
        max_attempts: int = 3,
    ) -> int:
        """Record task failure, incrementing retry_count and updating status to FAILED or ELIGIBLE."""
        raise NotImplementedError(f"{type(self).__name__} does not implement record_task_failure")

    def next_eligible_parse_tasks(
        self,
        network_id: int,
        status: int,
        *,
        limit: int = 10,
    ) -> list[tuple]:
        """Fetch next batch of eligible parse tasks ordered by chain preference."""
        raise NotImplementedError(f"{type(self).__name__} does not implement next_eligible_parse_tasks")

