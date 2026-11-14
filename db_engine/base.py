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
from db_engine.errors import UnsupportedOperation
from db_engine.spec import (
    as_column_spec,
    Agg,
    ColumnSpec,
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
from db_engine.spec import Case, Incoming, Length, Order, Pred, Substr, add, ge, gt, in_select, is_null, ne

if TYPE_CHECKING:
    from core.TableHandling import Table

logger = logging.getLogger("kernelinfo.db_engine")


# Capability flags (`BaseDBEngine.capabilities`). Callers branch on these instead of `isinstance(db, MockDB)`.
CAP_PERSISTENT = "persistent"  # data outlives the process (false for the in-memory mock)
CAP_SERVER_TUNING = "server_tuning"  # `apply_server_tuning()` can change server variables
CAP_BACKGROUND_COMMIT = "background_commit"  # `commit_tables_parallel(dedicated=True)` may run on a writer thread
CAP_CONCURRENT_CONNECTIONS = "concurrent_connections"  # independent connections see one database (workers, reader pool)

_TUNING_WARNING_MARKERS = (" capped at ", ": kept ", "not available", "memory guard")


def tuning_note_is_warning(note: str) -> bool:
    """True for an `apply_server_tuning()` note that a caller should log as a warning (cap, refusal, memory guard)."""
    return any(marker in note for marker in _TUNING_WARNING_MARKERS)


class ProbeResult(NamedTuple):
    """Outcome of `BaseDBEngine.probe_connection()`."""

    ok: bool
    version: str = ""
    latency_ms: float = 0.0
    error: str = ""
    target: str = ""


class _hybridmethod:
    """Descriptor: the wrapped function receives the instance when called on one, else the class."""

    def __init__(self, func: Any) -> None:
        self.func = func
        self.__doc__ = func.__doc__

    def __get__(self, obj: Any, cls: type | None = None) -> Any:
        target = obj if obj is not None else cls
        return lambda *a, **k: self.func(target, *a, **k)


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


class SpecNewPairs(NewPairs):
    """`NewPairs` built from query specs alone: no connection-local state, so every backend can use it.

    `pairs` is read once; the deletes and the include copy re-run the anti-join of the two checkpoints in the
    database (`in_select` / `copy_rows`).
    """

    def __init__(self, db: BaseDBEngine, commit_id: int, prev_commit_id: int) -> None:
        self.db = db
        self.commit_id = commit_id
        self.prev_commit_id = prev_commit_id
        rows = db.select_spec(self._anti_join(["cur_v.fnid", "cur_v.blob_id"]), as_dict=False)
        self.pairs = {(int(f), int(b)) for f, b in rows}

    def _anti_join(self, columns: Sequence[Any], *, hint: Literal["straight_join"] | None = None) -> Select:
        """`(fnid, blob_id)` rows of `commit_id`'s checkpoint that `prev_commit_id`'s lacks."""
        return Select(
            ("m_bridge_checkpoint", "cur_v"),
            columns,
            {"cur_v.commit_id": self.commit_id, "old_v.blob_id": is_null()},
            joins=[Join(
                ("m_bridge_checkpoint", "old_v"),
                {"old_v.commit_id": Value(self.prev_commit_id), "old_v.fnid": "cur_v.fnid", "old_v.blob_id": "cur_v.blob_id"},
                "left",
            )],
            hint=hint,
        )

    def delete_rows_of(self, table: Table | str) -> int:
        name = table.table_name if hasattr(table, "table_name") else str(table)
        return self.db.delete_rows(
            name,
            [in_select(("source_blob_id", "source_fnid"), self._anti_join(["cur_v.blob_id", "cur_v.fnid"]))],
        )

    def insert_include_references(
        self,
        kconfig_type_ids: Sequence[int] | str,
        kconfig_ref_type: int,
        include_ref_type: int,
    ) -> int:
        if isinstance(kconfig_type_ids, str):
            kconfig_type_ids = [int(x) for x in kconfig_type_ids.split(",") if x.strip()]
        kconfig_types = [int(x) for x in kconfig_type_ids]
        return self.db.copy_rows(
            "m_file_reference",
            ["source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details"],
            Select(
                ("m_bridge_checkpoint", "bf"),
                [
                    "bt.blob_id",
                    "bf.fnid",
                    "inc.fnid",
                    Case([({"a.type_id": kconfig_types}, kconfig_ref_type)], include_ref_type),
                    "bt.line_s",
                    Substr("a.name", 1, 255),
                ],
                {"bf.commit_id": self.commit_id, "old_v.blob_id": is_null()},
                joins=[
                    Join(
                        ("m_bridge_checkpoint", "old_v"),
                        {"old_v.commit_id": Value(self.prev_commit_id), "old_v.fnid": "bf.fnid", "old_v.blob_id": "bf.blob_id"},
                        "left",
                    ),
                    Join(("m_bridge_tag", "bt"), {"bt.blob_id": "bf.blob_id"}),
                    Join(("m_tag", "t"), {"t.tag_id": "bt.tag_id"}),
                    Join(("m_ast_include", "inc"), {"inc.ast_id": "t.ast_id"}),
                    Join(("m_ast", "a"), {"a.ast_id": "inc.ast_id"}),
                ],
                distinct=True,
                hint="straight_join",
            ),
            on_conflict="ignore",
        )


class TableCommitStat(NamedTuple):
    """Rows one `commit_tables_parallel()` call wrote to one table, and the seconds that table took."""

    table_name: str
    inserted: int
    updated: int
    seconds: float


def estimate_column_bytes(col_def: Sequence[Any]) -> int:
    """Stored width in bytes of one column, from its definition (`("hash", BIN(32))`, a name + `ColumnSpec`).

    Fixed-width types are exact; a `CHAR`/`BINARY` column is taken at its declared length and a
    `TEXT`/`BLOB` column at a nominal average, which is what makes a projected table size an estimate.
    """
    from db_engine.sql_render import render_column_type

    sql_type = render_column_type(as_column_spec(tuple(col_def[1:]))).upper()
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

    #: Human-readable engine name used in diagnostics ("Connected to <display_name> successfully").
    display_name: str = "database"

    #: SQL dialect that `sql_render` uses for this engine ("mariadb" or "sqlite").
    dialect: Literal["mariadb", "sqlite"] = "mariadb"
    #: Capability flags every instance of the engine has (see the `CAP_*` constants); `capabilities` adds derived ones.
    base_capabilities: frozenset[str] = frozenset()
    #: Candidate local server socket paths an engine of this family listens on (empty for socketless engines).
    default_socket_paths: tuple[str, ...] = ()

    @property
    def capabilities(self) -> frozenset[str]:
        """Capability flags of this engine (`CAP_*`): persistent, server_tuning, background_commit, ..."""
        caps = set(self.base_capabilities)
        if self.supports_background_commit:
            caps.add(CAP_BACKGROUND_COMMIT)
        return frozenset(caps)

    def has_capability(self, name: str) -> bool:
        """True when `name` is in `capabilities`."""
        return name in self.capabilities

    @classmethod
    def probe_connection(cls, cfg: Mapping[str, Any]) -> ProbeResult:
        """Try to connect with the `database` config section `cfg`; never raises. Engines override."""
        return ProbeResult(False, error=f"{cls.__name__} does not implement probe_connection")

    @_hybridmethod
    def reader(self) -> ContextManager[Any]:
        """Context manager yielding a read-only `ReadSession` (callable on the class or an instance)."""
        raise UnsupportedOperation(
            f"{getattr(self, '__name__', type(self).__name__)} does not provide a reader; use an engine with read sessions "
            "(MariaDB, SQLiteDB, MockDB) - see DB_API.md 'Webapp Read Session & Query Primitives'"
        )

    @classmethod
    def warm_up(cls) -> None:
        """Pre-connect the shared reader pool (and any SSH tunnel) at process start; no-op by default."""

    @classmethod
    def shutdown(cls) -> None:
        """Release the shared reader pool at process exit; no-op by default."""

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

    def check_if_connected(self) -> None:
        """Verify the connection and re-establish it when dropped (no-op for engines without a remote connection)."""

    def commit(self) -> None:
        """Commit pending transaction if driver is in transactional mode."""

    def rollback(self) -> None:
        """Roll back pending transaction if driver is in transactional mode."""

    def _raw_cursor(self, dictionary: bool = False) -> Any:
        """Lease a cursor handle on the underlying database connection."""
        if hasattr(self, "cnx") and getattr(self, "cnx", None):
            return self.cnx.cursor(dictionary=dictionary)
        raise UnsupportedOperation(
            "Driver does not provide a raw cursor; use spec reads (select_rows) / writes (insert_rows, update_rows) "
            "- see DB_API.md 'Encapsulation & Query Authority Contract'"
        )


    def _raw_query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> list[dict[str, Any] | tuple[SafeDataType, ...]]:
        """Execute a SQL SELECT/query and return all result rows."""
        raise UnsupportedOperation(
            f"{type(self).__name__} does not run raw queries; use select_rows / select_spec - see DB_API.md 'Spec Read Primitives'"
        )

    def _raw_query_one(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> dict[str, Any] | tuple[SafeDataType, ...] | None:
        """Execute a SQL SELECT/query and return the first result row, or None if no match."""
        rows = self._raw_query(sql, params, dictionary=dictionary)
        return rows[0] if rows else None

    def _raw_query_iter(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
        chunk_size: int = 2000,
    ) -> Iterator[dict[str, Any] | tuple[SafeDataType, ...]]:
        """Stream query result rows in batches of chunk_size."""
        yield from self._raw_query(sql, params, dictionary=dictionary)

    def _raw_execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Execute an arbitrary SQL DML/DDL statement."""
        raise UnsupportedOperation(
            f"{type(self).__name__} does not run raw statements; use insert_rows / update_rows / delete_rows - see DB_API.md 'Spec Write Primitives'"
        )

    def _raw_executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
        """Execute a parameterized SQL statement repeatedly across a sequence of parameter tuples."""
        count = 0
        for p in params_seq:
            count += self._raw_execute(sql, p)
        return count

    def _raw_delete(
        self,
        table: Table | str,
        where: str,
        params: Sequence[Any] | None = None,
    ) -> int:
        """Delete records matching parameterized WHERE condition."""
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        sql = f"DELETE FROM {tbl_name} WHERE {where}"
        return self._raw_execute(sql, params)

    def table_exists(self, table_name: str) -> bool:
        """Check whether a table or view exists in the current catalog."""
        rows = self._raw_query(
            "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s LIMIT 1",
            (table_name,),
            dictionary=False,
        )
        return bool(rows)

    def create_view(self, view_name: str, select: Select | str, replace: bool = True) -> None:
        """Create or replace a database view from a `Select` spec (a SQL string is accepted for legacy callers)."""
        from db_engine.sql_render import render_select_literal, validate_ident

        body = render_select_literal(select, self.dialect) if isinstance(select, Select) else str(select)
        validate_ident(view_name)
        if replace and self.dialect == "sqlite":
            self._raw_execute(f"DROP VIEW IF EXISTS {view_name}")
            self._raw_execute(f"CREATE VIEW {view_name} AS {body}")
            return
        self._raw_execute(f"CREATE {'OR REPLACE ' if replace else ''}VIEW {view_name} AS {body}")

    def drop_view(self, view_name: str, if_exists: bool = True) -> None:
        """Drop a database view."""
        stmt = f"DROP VIEW {'IF EXISTS ' if if_exists else ''}{view_name}"
        self._raw_execute(stmt)

    def add_column_if_missing(self, table_name: str, column_name: str, column_def: str) -> bool:
        """Add column to table if not already present."""
        rows = self._raw_query(
            "SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s LIMIT 1",
            (table_name, column_name),
            dictionary=False,
        )
        if not rows:
            col_spec = column_def if column_def.strip().startswith(column_name) else f"{column_name} {column_def}"
            self._raw_execute(f"ALTER TABLE {table_name} ADD COLUMN {col_spec}")
            return True
        return False

    def add_column(self, table_name: str, column_name: str, spec: ColumnSpec) -> bool:
        """Add a neutrally described column (`ColumnSpec`) when the table lacks it. Returns True when added."""
        from db_engine.sql_render import render_column_ddl, validate_ident

        validate_ident(table_name)
        if self.column_exists(table_name, column_name):
            return False
        self._raw_execute(f"ALTER TABLE {table_name} ADD COLUMN {render_column_ddl(column_name, spec, self.dialect)}")
        self.commit()
        return True

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
        group_by: Sequence[Any] = (),
        having: Where | None = None,
        hint: Literal["straight_join"] | None = None,
    ) -> list[tuple | dict]:
        """Execute parameterized SELECT query from spec and return rows.

        Columns / order_by / group_by items may be column names, `Agg`, `Const` or expressions (`Concat`, `Case`, ...).
        """
        raise UnsupportedOperation(f"{type(self).__name__} does not implement select_rows - see DB_API.md 'Spec Read Primitives'")

    def select_spec(self, select: Select, *, as_dict: bool | None = None) -> list[tuple | dict]:
        """Run a complete `Select` specification (every clause) through `select_rows()`."""
        return self.select_rows(
            select.source,
            select.columns,
            select.where,
            joins=select.joins,
            distinct=select.distinct,
            order_by=select.order_by,
            limit=select.limit,
            offset=select.offset,
            as_dict=as_dict,
            group_by=select.group_by,
            having=select.having,
            hint=select.hint,
        )

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
        group_by: Sequence[Any] = (),
        having: Where | None = None,
        hint: Literal["straight_join"] | None = None,
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
            group_by=group_by,
            having=having,
            hint=hint,
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
        order_by: Sequence[Any] = (),
        as_dict: bool | None = None,
        having: Where | None = None,
    ) -> list:
        """Compute aggregate calculations, optionally grouped (`GROUP BY group_by [HAVING having]`)."""
        cols: dict[str, Any] = {col.rsplit(".", 1)[-1]: col for col in group_by if isinstance(col, str)}
        cols.update(aggregates)
        return self.select_rows(
            source,
            cols,
            where=where,
            joins=joins,
            order_by=order_by,
            as_dict=as_dict,
            group_by=group_by,
            having=having,
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
        update_exprs: Mapping[str, Any] | None = None,
    ) -> int:
        """Insert row(s) matching columns. Returns the number of rows written.

        Count (identical on every backend): `error` / `ignore` count the rows inserted (an ignored duplicate counts 0);
        `update` / `replace` count every offered row (inserted, or overwriting / updating an existing one, even when
        the stored values do not change).

        `on_conflict="update"` assigns the incoming value to `update_columns` (default: every inserted column,
        or none when `update_exprs` is given) and evaluates `update_exprs` (`{column: Expr}`) for the rest:
        `Incoming("col")` is the offered value, a bare column name the STORED (pre-update) value on every backend
        (MariaDB renders the assignments in an order that guarantees it; a read cycle raises `SpecError`),
        `Case(...)` makes the update conditional.
        """
        raise UnsupportedOperation(f"{type(self).__name__} does not implement insert_rows - see DB_API.md 'Spec Write Primitives'")

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
        """Update rows matching where filter. Returns the rows matched (also those whose values do not change).

        Expressions (`Col`, `Case`, ...) read the stored row on every backend.
        """
        raise UnsupportedOperation(f"{type(self).__name__} does not implement update_rows - see DB_API.md 'Spec Write Primitives'")

    def delete_rows(
        self,
        table: Table | str,
        where: Where | None,
        *,
        limit: int | None = None,
        all_rows: bool = False,
    ) -> int:
        """Delete rows matching where filter. Returns the rows deleted."""
        raise UnsupportedOperation(f"{type(self).__name__} does not implement delete_rows - see DB_API.md 'Spec Write Primitives'")

    def copy_rows(
        self,
        table: Table | str,
        columns: Sequence[str],
        select: Select,
        *,
        on_conflict: Literal["error", "ignore"] = "error",
    ) -> int:
        """Insert rows selected by select query into table. Returns the rows inserted (ignored duplicates count 0)."""
        raise UnsupportedOperation(f"{type(self).__name__} does not implement copy_rows - see DB_API.md 'Spec Write Primitives'")

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
        rows = self._raw_query(sql, list(names), dictionary=False)
        return {str(r[0]) for r in rows if r and r[0]}

    def column_info(self, table: Table | str, column: str) -> ColumnInfo | None:
        """Query column metadata (data_type, column_type, octet_length, nullable)."""
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        sql = (
            "SELECT DATA_TYPE, COLUMN_TYPE, CHARACTER_OCTET_LENGTH, IS_NULLABLE "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s LIMIT 1"
        )
        rows = self._raw_query(sql, (tbl_name, column), dictionary=False)
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
        rows = self._raw_query(sql, (tbl_name,), dictionary=False)
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
        rows = self._raw_query(sql, params, dictionary=False)
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
            rows = self._raw_query(f"SELECT @@GLOBAL.{clean}", dictionary=False)
            if rows and rows[0] and rows[0][0] is not None:
                return str(rows[0][0])
        except Exception:
            pass
        return None

    def apply_server_tuning(
        self,
        settings: dict[str, Any] | None = None,
        *,
        tunnel_active: bool | None = None,
        mem_max_gb: float = 0.0,
    ) -> list[str]:
        """Apply server-level engine tuning and return one note per change or problem. Base: no tuning, no notes.

        `tunnel_active` (None = unknown, skip the local-RAM logic) tells whether the server is reached through an
        SSH tunnel; `mem_max_gb` is the parser memory guard used for the RAM-fit warning
        (`tuning_note_is_warning()` classifies the notes).
        """
        return []

    # -------------------------------------------------------------------------
    # Schema Maintenance (§5)
    # -------------------------------------------------------------------------
    def drop_tables(
        self,
        names: Sequence[str],
        *,
        ignore_fk: bool = False,
        if_exists: bool = True,
    ) -> None:
        """Drop tables by name; `ignore_fk=True` drops them although other tables reference them."""
        raise UnsupportedOperation(f"{type(self).__name__} does not implement drop_tables - see DB_API.md 'Schema Maintenance & View Registry'")

    def ensure_columns(self, table: Table, columns: Sequence[str] | None = None) -> list[str]:
        """Add any init_columns missing from the live table and return their names."""
        added: list[str] = []
        wanted = set(columns) if columns is not None else None
        for col_def in table.init_columns:
            col_name = col_def[0]
            if wanted is not None and col_name not in wanted:
                continue
            if not self.column_exists(table.table_name, col_name):
                self.add_column(table.table_name, col_name, col_def[1])
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
        yield SpecNewPairs(self, commit_id, prev_commit_id)

    def enqueue_parse_tasks(self, rows: Sequence[Sequence[Any]]) -> int:
        """Publish tasks to `m_parse_task`; returns how many tasks were written (inserted or updated).

        Each row is `(network_id, chain_name, chain_kind, seq_order, ref_name, commit_id, base_ref_name,
        base_commit_id, status)`; `(network_id, ref_name)` identifies a task. An existing task keeps its progress: a
        FAILED task, or a DONE one that is published as not done, starts over (status as published, `retry_count`
        0, `error_msg` cleared); a LEASED or RECLAIMING task keeps its state unless it is published as DONE; any
        other task takes the published status. A known `commit_id` / `base_commit_id` is never replaced by 0. The
        rows go out as chunked multi-row upserts (`insert_rows(on_conflict="update", update_exprs=...)`) in one
        transaction; rows are applied in order, so a task listed twice ends as if published twice.
        """
        if not rows:
            return 0
        failed, done, leased, reclaiming = 4, 3, 2, 5  # core.distributed TASK_* values
        reopened = AnyOf({"status": failed}, [{"status": done}, Pred(Incoming("status"), ne(done))])
        update_exprs = {
            "commit_id": Case([(Pred(Incoming("commit_id"), gt(0)), Incoming("commit_id"))], "commit_id"),
            "base_commit_id": Case([(Pred(Incoming("base_commit_id"), gt(0)), Incoming("base_commit_id"))], "base_commit_id"),
            # retry_count / error_msg read the stored status (every expression reads the stored row)
            "retry_count": Case([([reopened], 0)], "retry_count"),
            "error_msg": Case([([reopened], Value(""))], "error_msg"),
            "status": Case(
                [
                    ({"status": failed}, Incoming("status")),
                    ([{"status": done}, Pred(Incoming("status"), ne(done))], Incoming("status")),
                    (
                        {"status": [leased, reclaiming]},
                        Case([(Pred(Incoming("status"), done), done)], "status"),
                    ),
                ],
                Incoming("status"),
            ),
        }
        columns = ["network_id", "chain_name", "chain_kind", "seq_order", "ref_name", "commit_id", "base_ref_name",
                   "base_commit_id", "status"]
        with self.transaction():
            return self.insert_rows(
                "m_parse_task", columns, [tuple(row) for row in rows], on_conflict="update",
                update_columns=["chain_name", "chain_kind", "seq_order", "base_ref_name"], update_exprs=update_exprs,
            )

    def record_task_failure(
        self,
        task_id: int,
        error_msg: str,
        *,
        worker_id: str | None = None,
        max_attempts: int = 3,
    ) -> int:
        """Record a failed attempt of a LEASED / RECLAIMING task (of `worker_id` when given); returns the rows updated.

        `retry_count` grows by one; the task becomes FAILED once that reaches `max_attempts`, else ELIGIBLE again,
        and loses its worker. Every expression reads the stored row.
        """
        where: dict[str, Any] = {"task_id": task_id, "status": [2, 5]}
        if worker_id:
            where["worker_id"] = worker_id
        return self.update_rows(
            "m_parse_task",
            {
                "status": Case([(Pred(add("retry_count", 1), ge(max_attempts)), 4)], 1),
                "retry_count": add("retry_count", 1),
                "error_msg": error_msg[:1000],
                "worker_id": "",
            },
            where,
        )

    def next_eligible_parse_tasks(
        self,
        network_id: int,
        status: int,
        *,
        limit: int = 10,
    ) -> list[tuple]:
        """Next `limit` tasks of `status`: stable chains first, then shorter then alphabetical chain names, by `seq_order`.

        Rows are `(task_id, chain_name, chain_kind, ref_name, commit_id, base_ref_name, base_commit_id)`.
        """
        rows = self.select_rows(
            "m_parse_task",
            ["task_id", "chain_name", "chain_kind", "ref_name", "commit_id", "base_ref_name", "base_commit_id"],
            {"network_id": network_id, "status": status},
            order_by=[Order(Case([({"chain_kind": "stable"}, 1)], 0), desc=True), Order(Length("chain_name")),
                      "chain_name", "seq_order"],
            limit=int(limit),
            as_dict=False,
        )
        return [tuple(r) for r in rows]

    def symbol_refs_of_targets(
        self,
        ast_ids: Sequence[int],
        *,
        role: int | None = None,
        with_caller: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """References of the target ASTs: dicts `role, line, char_s, fid, tag_id, ast_id` (+ `caller`).

        Read key first through query specs (`db_engine/symbol_rows.py`) instead of filtering the `m_symbol_ref` view.
        """
        from db_engine import symbol_rows

        return symbol_rows.symbol_refs_of_targets(self, ast_ids, role=role, with_caller=with_caller, limit=limit)

    def symbol_def_blobs_of_targets(self, ast_ids: Sequence[int]) -> set[tuple[int, int]]:
        """Distinct `(ast_id, blob_id)` definitions of the target ASTs (`db_engine/symbol_rows.py`)."""
        from db_engine import symbol_rows

        return symbol_rows.symbol_def_blobs_of_targets(self, ast_ids)
