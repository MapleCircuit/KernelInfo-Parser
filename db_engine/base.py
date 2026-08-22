"""db_engine/base.py - Base Abstract Database Engine Interface Contract.

Defines the core database backend API expected across KernelInfo-Parser,
Table Engine (TEDirectDB), and ChangeSet execution.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Self

from core.globalstuff import JoinsType, PointerType, SafeDataType

if TYPE_CHECKING:
    from core.TableHandling import Table


class BaseDBEngine(ABC):
    """Abstract Base Class for database backend driver implementations.

    Driver Contract & Architecture:
        - Connection: Drivers manage active connection/cursor handles, auto-reconnecting on dropped sockets.
        - Transactions: DDL and DML operations auto-commit; context manager rolls back on unhandled error.
        - Wildcard Matching: Tuple queries treat `None` values as wildcards (omitted from WHERE filters).
        - Joins: Multi-table join queries map columns in joined order, chunking graphs > 50 tables into slices of <= 30.
    """

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
    ) -> None:
        """Commit inserts and updates for multiple tables concurrently across worker connection threads.

        Args:
            tables_data: Sequence of tuples `(table, insert_payload, update_payload)`.
            max_workers: Maximum concurrent database worker threads (defaults to min(len(tables_data), G.CPUS)).
        Process:
            Executes table insert/update operations in parallel across isolated database connections and commits.
        Outputs:
            None.
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
    def create_index(self, index_name: str, table: Table, rows: tuple[PointerType, ...]) -> None:
        """Create composite or single-column index on target table.

        Args:
            index_name (str): Identifier name for index.
            table (Table): Target Table schema instance.
            rows (tuple[PointerType, ...]): Tuple of pointer tuples `(table_id, col_idx)` specifying indexed columns.
        Process:
            Maps column indices to names, executes `CREATE INDEX <index_name> ON <table> (<cols>)`, and commits.
        Outputs:
            None.
        """

    @abstractmethod
    def remove_index(self, index_name: str, table: Table) -> None:
        """Drop existing index from target table.

        Args:
            index_name (str): Name of index to remove.
            table (Table): Target Table schema instance.
        Process:
            Executes `ALTER TABLE <table> DROP INDEX <index_name>`, and commits.
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
