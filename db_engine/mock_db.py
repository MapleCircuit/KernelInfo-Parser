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

    def commit_tables_parallel(
        self,
        tables_data: Sequence[tuple[Table, Sequence[tuple[SafeDataType, ...]], Sequence[tuple[SafeDataType, ...]]]],
        max_workers: int | None = None,
    ) -> None:
        """Commit inserts and updates for multiple tables in mock database."""
        for table, insert_data, update_data in tables_data:
            if insert_data:
                self.insert(table, insert_data)
            if update_data:
                self.update(table, update_data)

    def view_select(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Joined single-row select with wildcard column matching."""
        res = self.view_select_multiple(tables, joins, columns)
        return res[0] if res else None

    def view_select_multiple(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> list[tuple[SafeDataType, ...]]:
        """Joined multi-row select across join graph with wildcard column matching."""
        if isinstance(tables, (tuple, list)):
            tables_dict = {t.table_id: t for t in tables}
        else:
            tables_dict = tables

        pg = PointerGetter(joins)
        first_table_id = pg.get_first_table_id()
        t1 = tables_dict[first_table_id]
        t1_rows = self.tables_data.get(t1.table_name, {})

        results = []
        for r1 in t1_rows.values():
            match = True
            for i, val in enumerate(columns[:t1.length]):
                if val is not None and i < len(r1) and r1[i] != val:
                    match = False
                    break
            if not match:
                continue

            current_composite_rows = [list(r1)]
            col_offset = t1.length

            for join in joins:
                if len(join) < 2:
                    continue
                from_ptr, to_ptr = join[0], join[1]
                t_target = tables_dict[to_ptr[0]]
                t_target_rows = self.tables_data.get(t_target.table_name, {})

                new_composite = []
                for comp in current_composite_rows:
                    from_val = comp[from_ptr[1]]
                    for r_tgt in t_target_rows.values():
                        if r_tgt[to_ptr[1]] == from_val:
                            tgt_match = True
                            for j, val in enumerate(columns[col_offset : col_offset + t_target.length]):
                                if val is not None and j < len(r_tgt) and r_tgt[j] != val:
                                    tgt_match = False
                                    break
                            if tgt_match:
                                new_composite.append(comp + list(r_tgt))
                current_composite_rows = new_composite
                col_offset += t_target.length

            for comp in current_composite_rows:
                results.append(tuple(comp))

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

    def index_exists(self, index_name: str, table: Table) -> bool:
        """Check if index exists (MockDB no-op)."""
        return False

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
