"""table_engine/te_direct_db.py - Direct Database Table Engine (TEDirectDB).

===============================================================================
TABLE ENGINE (TEDirectDB) ARCHITECTURAL GUIDE & CONTRACT SPECIFICATION
===============================================================================
This module implements the Direct Database Table Engine (TEDirectDB) for
KernelInfo-Parser. It serves as the stateful caching, sequence coordination,
and batching layer between high-level ChangeSet operations and the lower-level
database driver (e.g., MariaDB or MockDB in db_engine).

KEY RESPONSIBILITIES:
1. SEQUENCE ASSIGNMENT (next_id):
   Maintains local auto-increment primary key counters initialized via
   `db.get_next_id(table)` to eliminate per-row SELECT MAX() database queries.

2. LOCAL STAGING & DEDUPLICATION:
   - `queued_set`: Staging dictionary for single-table inserts. For deduplicated
     tables (no_duplicate=True), maps unique data tuples to assigned sequence IDs.
   - `queued_update`: Staging list of row update tuples.
   - `queued_view`: In-memory cache for relational joins (JoinsType) mapping
     non-None column values to assigned primary view IDs.

3. RELATIONAL VIEW EXPANSION:
   `view_set()` decomposes multi-table joined structures into their constituent
   table rows, correctly slicing data offsets and keying on each table's primary key.

4. TRANSACTION COMMIT:
   `commit()` flushes all staged rows in high-throughput batch operations
   using `db.insert()` and `db.update()`, and purges staged memory buffers.

5. RESOURCE MANAGEMENT:
   Safely manages database connection lifecycles during start, restart, and shutdown.
===============================================================================
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Sequence, Self
from types import TracebackType
from operator import itemgetter
from core.globalstuff import (
    SafeDataType,
    JoinsType,
    PointerGetter,
)

if TYPE_CHECKING:
    from core.TableHandling import Table
    from db_engine.base import BaseDBEngine


class TEDirectDB:
    """Direct Database Table Engine (Unoptimized direct-to-database table engine).

    Acts as a stateful caching, sequence coordination, and batching layer between
    ChangeSet execution in TableHandling and the database backend in db_engine.
    """

    def __init__(self) -> None:
        """Initialize Table Engine internal state structures.

        Note: You must still call `start()` or `start_new_db()` before performing operations.

        Attributes initialized:
            tables: Maps table_id (int) to Table schema instance.
            queued_set: Maps table_id (int) to dictionary of staged insert rows.
            queued_update: Maps table_id (int) to list of staged update rows.
            queued_view: Maps JoinsType tuple to dictionary mapping non-None column tuples to view IDs.
            next_id: Maps table_id (int) to current auto-increment sequence integer ID.
            db: Database driver instance handle (or None until started).
        """
        self.tables: dict[int, Table] = {}
        self.queued_set: dict[int, dict[Any, Any]] = {}
        self.queued_update: dict[int, list[tuple[SafeDataType, ...]]] = {}
        self.queued_view: dict[JoinsType, dict[tuple[SafeDataType, ...], int]] = {}
        self.next_id: dict[int, int] = {}
        self.db: Any | None = None

    def close(self) -> None:
        """Safely close active database connection handle."""
        if getattr(self, "db", None) is not None:
            if hasattr(self.db, "close") and callable(self.db.close):
                try:
                    self.db.close()
                except Exception:
                    pass
            self.db = None

    def __enter__(self) -> Self:
        """Enter context manager scope."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Exit context manager scope and close DB connection."""
        self.close()

    def __del__(self) -> None:
        """Clean up resources upon garbage collection."""
        self.close()

    def start_new_db(self, db: Callable[[], Any] | type[Any]) -> None:
        """Start or restart the database connection handle for Table Engine use.

        Args:
            db: Database class or factory callable (e.g., MariaDB or G.DB).

        Process:
            1. Closes any existing active database connection handle safely.
            2. Instantiates a new database driver instance: `self.db = db()`.

        Outputs:
            None.
        """
        self.close()
        self.db = db()

    def start(self, tables: Sequence[Table] | Table, db: Callable[[], Any] | type[Any]) -> None:
        """Initialize Table Engine with schema tables and connect to database.

        Args:
            tables: Single Table instance or sequence of Table instances (gp.Table_Array).
            db: Database class or factory callable (e.g., MariaDB or G.DB).

        Process:
            1. Normalizes `tables` into a tuple if a single table was provided.
            2. Starts a fresh database connection via `self.start_new_db(db)`.
            3. Resets view deduplication cache `self.queued_view = {}`.
            4. For each registered Table schema:
               - Stores table schema reference in `self.tables[table.table_id]`.
               - Initializes empty insert staging dictionary in `self.queued_set[table.table_id]`.
               - Initializes empty update staging list in `self.queued_update[table.table_id]`.
               - Queries current MAX primary key sequence ID via `self.db.get_next_id(table)`.

        Outputs:
            None.
        """
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)

        self.start_new_db(db)
        self.queued_view = {}

        for table in tables:
            self.tables[table.table_id] = table
            self.queued_set[table.table_id] = {}
            self.queued_update[table.table_id] = []
            self.next_id[table.table_id] = self.db.get_next_id(table)

    def get(
        self,
        table_id: int,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Execute single-row SELECT query with wildcard column filtering.

        Args:
            table_id: Target table identifier integer.
            columns: Tuple of column filter values, where None represents wildcards.

        Process:
            1. Emptiness guard: If `next_id == 1` and `table.initial_insert is None`,
               the table is empty, avoiding unnecessary database queries.
            2. Executes parameterized SELECT query via `self.db.select(table, columns)`.

        Outputs:
            Matching row tuple `tuple[SafeDataType, ...]` or None if no match is found.
        """
        table = self.tables[table_id]
        if table.initial_insert is None and self.next_id[table_id] <= 1:
            return None
        return self.db.select(table, columns)

    def set(
        self,
        table_id: int,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...]:
        """Stage an insert row in local memory, handling deduplication and sequence generation.

        Args:
            table_id: Target table identifier integer.
            columns: Row data tuple matching the table's column schema.

        Process:
            1. If table has `no_duplicate=True`:
               - Checks `self.queued_set[table_id]` using `columns[1:]` as key.
               - If match exists: returns `(cached_id, *columns[1:])`.
               - If new: assigns `self.next_id[table_id]`, increments counter,
                 stores `self.queued_set[table_id][columns[1:]] = assigned_id`,
                 and returns `(assigned_id, *columns[1:])`.
            2. If `columns[0] is None` (auto-increment generation):
               - Assigns `assigned_id = self.next_id[table_id]`, increments counter.
               - Builds `new_row = (assigned_id, *columns[1:])`.
               - Stores `self.queued_set[table_id][assigned_id] = new_row`.
               - Returns `new_row`.
            3. If explicit primary key provided (`columns[0] is not None`):
               - Extracts primary key keying via `itemgetter(*table.primary)(columns)`.
               - Stores `self.queued_set[table_id][pk] = columns`.
               - Returns `columns`.

        Outputs:
            Complete resolved row tuple `tuple[SafeDataType, ...]`.
        """
        table = self.tables[table_id]

        if table.no_duplicate:
            key = columns[1:]
            current_set = self.queued_set[table_id].get(key)
            if current_set is not None:
                return (current_set, *columns[1:])

            assigned_id = self.next_id[table_id]
            self.queued_set[table_id][key] = assigned_id
            self.next_id[table_id] += 1
            return (assigned_id, *columns[1:])

        if columns[0] is None:
            assigned_id = self.next_id[table_id]
            row = (assigned_id, *columns[1:])
            self.queued_set[table_id][assigned_id] = row
            self.next_id[table_id] += 1
            return row

        pk = itemgetter(*table.primary)(columns)
        self.queued_set[table_id][pk] = columns
        return columns

    def view_get(
        self,
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Execute single-row SELECT query across multi-table relational join graph.

        Args:
            joins: Relational join graph tuple (JoinsType).
            columns: Concatenated tuple of column filter values across joined tables.

        Process:
            1. Emptiness guard: Checks if the initial table in the join graph has data,
               safely handling cases where `table.initial_insert is None`.
            2. Executes multi-table join SELECT via `self.db.view_select(self.tables, joins, columns)`.

        Outputs:
            First matching joined row tuple `tuple[SafeDataType, ...]` or None.
        """
        initial_table_id = PointerGetter(joins).get_first_table_id()
        table = self.tables[initial_table_id]
        if table.initial_insert is None and self.next_id[initial_table_id] <= 1:
            return None
        return self.db.view_select(self.tables, joins, columns)

    def view_get_multiple(
        self,
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> list[tuple[SafeDataType, ...]]:
        """Execute multi-row SELECT query across relational join graph, fetching all matches.

        Args:
            joins: Relational join graph tuple (JoinsType).
            columns: Concatenated tuple of column filter values across joined tables.

        Process:
            Executes multi-table join SELECT via `self.db.view_select_multiple(self.tables, joins, columns)`.

        Outputs:
            List of matching joined row tuples `list[tuple[SafeDataType, ...]]`.
        """
        return self.db.view_select_multiple(self.tables, joins, columns)

    def view_set(
        self,
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...]:
        """Stage multi-table relational view, deduplicate in memory, and decompose into table inserts.

        Args:
            joins: Relational join graph tuple (JoinsType).
            columns: Concatenated data tuple across joined tables (None at view ID positions).

        Process:
            1. Extracts non-None column values: `filtered = tuple(x for x in columns if x is not None)`.
            2. Checks `self.queued_view` cache for matching `joins` and `filtered`:
               - If cached: returns `columns` with None positions replaced by cached `view_id`.
            3. If not cached:
               - Assigns `current_view_id = self.next_id[main_table_id]`, increments counter.
               - Caches in `self.queued_view[joins][filtered] = current_view_id`.
               - Builds `result` by replacing None values in `columns` with `current_view_id`.
               - Decomposes `result` across each constituent table in `joins`:
                 * Slices per-table row: `row = result[data_offset : data_offset + table.length]`.
                 * Extracts primary key from `row` slice: `pk = itemgetter(*table.primary)(row)`.
                 * Stores staged row in `self.queued_set[table_id][pk] = row`.
                 * Advances `data_offset += table.length`.

        Outputs:
            Complete joined row tuple `tuple[SafeDataType, ...]`.
        """
        filtered_columns = tuple(val for val in columns if val is not None)

        if joins in self.queued_view:
            current_view_id = self.queued_view[joins].get(filtered_columns)
            if current_view_id is not None:
                return tuple(val if val is not None else current_view_id for val in columns)
        else:
            self.queued_view[joins] = {}

        main_table_id = PointerGetter(joins).get_first_table_id()
        current_view_id = self.next_id[main_table_id]
        self.queued_view[joins][filtered_columns] = current_view_id
        self.next_id[main_table_id] += 1

        result = tuple(val if val is not None else current_view_id for val in columns)

        data_offset = 0
        for repeat, pointer in PointerGetter(joins):
            target_table = self.tables[pointer[0]]
            t_len = target_table.length
            for _ in range(repeat):
                row = result[data_offset : data_offset + t_len]
                pk = itemgetter(*target_table.primary)(row)
                self.queued_set[pointer[0]][pk] = row
                data_offset += t_len

        return result

    def update(
        self,
        table_id: int,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...]:
        """Stage a row update in local memory for batch execution on commit.

        Args:
            table_id: Target table identifier integer.
            columns: Fully resolved row tuple to update.

        Process:
            Appends `columns` to `self.queued_update[table_id]`.

        Outputs:
            The input `columns` tuple.
        """
        self.queued_update[table_id].append(columns)
        return columns

    def commit(self, table_id: int) -> None:
        """Flush staged insert and update operations for target table to database and clear buffers.

        Args:
            table_id: Target table identifier integer.

        Process:
            1. If `self.queued_set[table_id]` is non-empty:
               - For `no_duplicate=True` tables: formats items as `((id_val, *data_key), ...)`.
               - For other tables: formats values as `tuple(self.queued_set[table_id].values())`.
               - Executes batch insert via `self.db.insert(table, payload)`.
               - Clears `self.queued_set[table_id]`.
            2. If `self.queued_update[table_id]` is non-empty:
               - Executes batch upsert via `self.db.update(table, tuple(self.queued_update[table_id]))`.
               - Clears `self.queued_update[table_id]`.

        Outputs:
            None.
        """
        table = self.tables[table_id]

        if self.queued_set[table_id]:
            if table.no_duplicate:
                payload = tuple((item[1], *item[0]) for item in self.queued_set[table_id].items())
            else:
                payload = tuple(self.queued_set[table_id].values())

            self.db.insert(table, payload)
            self.queued_set[table_id].clear()

        if self.queued_update[table_id]:
            self.db.update(table, tuple(self.queued_update[table_id]))
            self.queued_update[table_id].clear()

    def commit_all(self, max_workers: int | None = None) -> None:
        """Flush staged insert and update operations across ALL tables concurrently and clear buffers.

        Args:
            max_workers: Maximum concurrent worker threads.

        Process:
            1. Collects payload tuples `(table, insert_payload, update_payload)` for all non-empty tables.
            2. Dispatches to `self.db.commit_tables_parallel(tables_data, max_workers=max_workers)`.
            3. Clears all queued buffers.

        Outputs:
            None.
        """
        tables_data = []
        for table_id, table in self.tables.items():
            insert_payload = ()
            update_payload = ()

            if self.queued_set[table_id]:
                if table.no_duplicate:
                    insert_payload = tuple((item[1], *item[0]) for item in self.queued_set[table_id].items())
                else:
                    insert_payload = tuple(self.queued_set[table_id].values())

            if self.queued_update[table_id]:
                update_payload = tuple(self.queued_update[table_id])

            if insert_payload or update_payload:
                tables_data.append((table, insert_payload, update_payload))

        if tables_data:
            if hasattr(self.db, "commit_tables_parallel"):
                self.db.commit_tables_parallel(tables_data, max_workers=max_workers)
            else:
                for table, ins_p, upd_p in tables_data:
                    if ins_p:
                        self.db.insert(table, ins_p)
                    if upd_p:
                        self.db.update(table, upd_p)

            for table_id in self.tables:
                self.queued_set[table_id].clear()
                self.queued_update[table_id].clear()
