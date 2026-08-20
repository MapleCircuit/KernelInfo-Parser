"""db_engine/mock_db.py - In-Memory Mock Database Driver.

Provides a fast, zero-dependency in-memory database driver adhering to the
BaseDBEngine specification for isolated unit testing and ChangeSet execution.
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any, Sequence, Self
from types import TracebackType
from db_engine.base import BaseDBEngine
from core.globalstuff import SafeDataType, JoinsType, PointerType, PointerGetter

if TYPE_CHECKING:
    from core.TableHandling import Table


class MockDB(BaseDBEngine):
    """In-memory mock database driver implementation for ultra-fast, isolated testing."""

    _global_store: dict[str, dict[Any, tuple[SafeDataType, ...]]] = defaultdict(dict)

    def __init__(self, use_global: bool = True) -> None:
        """Initialize in-memory storage dictionary.
        
        Args:
            use_global: If True, uses the shared class-level store; if False, uses instance-level store.
        """
        self.tables_data = MockDB._global_store if use_global else defaultdict(dict)
        self.tables_schema: dict[int, Table] = {}

    def __enter__(self) -> Self:
        """Enter context manager scope."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Exit context manager scope."""
        pass

    @classmethod
    def reset(cls) -> None:
        """Reset the shared in-memory table store."""
        cls._global_store.clear()

    def close(self) -> None:
        """Release driver resources."""
        pass

    def get_next_id(self, table: Table) -> int:
        """Query current maximum primary key value and return next available integer ID."""
        rows = self.tables_data.get(table.table_name, {})
        if not rows:
            return 1
        pks = []
        for r in rows.values():
            if table.primary and len(table.primary) > 0:
                pk_val = r[table.primary[0]]
                if isinstance(pk_val, int):
                    pks.append(pk_val)
        return max(pks, default=0) + 1

    def select(self, table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None:
        """Single-row select matching non-None column wildcard values."""
        rows = self.tables_data.get(table.table_name, {})
        for row in rows.values():
            match = True
            for i, val in enumerate(data):
                if val is not None and i < len(row) and row[i] != val:
                    match = False
                    break
            if match:
                return row
        return None

    def insert(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        """Batch insert rows into in-memory table store."""
        if not data:
            return
        if not isinstance(data[0], (tuple, list)):
            data = (data,)  # type: ignore[assignment]

        table_dict = self.tables_data[table.table_name]
        for row in data:
            if table.primary:
                if len(table.primary) == 1:
                    pk = row[table.primary[0]]
                else:
                    pk = tuple(row[i] for i in table.primary)
            else:
                pk = row[0] if len(row) > 0 else id(row)
            table_dict[pk] = tuple(row)

    def update(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        """Batch upsert/update rows into in-memory table store."""
        self.insert(table, data)

    def view_select(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Joined single-row select with wildcard column matching."""
        if isinstance(tables, (tuple, list)):
            tables_dict = {t.table_id: t for t in tables}
        else:
            tables_dict = tables

        first_table_id = joins[0][0][0] if isinstance(joins[0], (tuple, list)) else joins[0][0]
        table = tables_dict[first_table_id]
        return self.select(table, columns[:table.length])

    def view_select_multiple(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> list[tuple[SafeDataType, ...]]:
        """Joined multi-row select with wildcard column matching."""
        if isinstance(tables, (tuple, list)):
            tables_dict = {t.table_id: t for t in tables}
        else:
            tables_dict = tables

        first_table_id = joins[0][0][0] if isinstance(joins[0], (tuple, list)) else joins[0][0]
        table = tables_dict[first_table_id]
        rows = self.tables_data.get(table.table_name, {})
        results = []
        for row in rows.values():
            match = True
            for i, val in enumerate(columns[:table.length]):
                if val is not None and i < len(row) and row[i] != val:
                    match = False
                    break
            if match:
                results.append(row)
        return results

    def create_table(self, tables: Sequence[Table] | Table) -> None:
        """Register tables in in-memory schema catalog."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        for table in tables:
            self.tables_schema[table.table_id] = table
            if table.table_name not in self.tables_data:
                self.tables_data[table.table_name] = {}
            if table.initial_insert:
                self.insert(table, table.initial_insert)

    def drop_table(self, tables: Sequence[Table] | Table) -> None:
        """Drop tables from in-memory schema catalog."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        for table in tables:
            self.tables_data.pop(table.table_name, None)
            self.tables_schema.pop(table.table_id, None)

    def create_index(self, index_name: str, table: Table, rows: tuple[PointerType, ...]) -> None:
        """No-op for in-memory mock engine."""
        pass

    def remove_index(self, index_name: str, table: Table) -> None:
        """No-op for in-memory mock engine."""
        pass

    def test_tables(self, tables: Sequence[Table] | Table) -> list[str] | None:
        """Check registered table existence."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        missing = [t.table_name for t in tables if t.table_name not in self.tables_data]
        return missing if missing else None
