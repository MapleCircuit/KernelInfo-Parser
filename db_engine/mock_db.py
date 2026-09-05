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
    _global_next_id: dict[str, int] = defaultdict(lambda: 1)

    def __init__(self, use_global: bool = True) -> None:
        """Initialize in-memory storage dictionary.
        
        Args:
            use_global: If True, uses the shared class-level store; if False, uses instance-level store.
        """
        self.tables_data = MockDB._global_store if use_global else defaultdict(dict)
        self.tables_next_id = MockDB._global_next_id if use_global else defaultdict(lambda: 1)
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
        """Reset the shared in-memory table store and sequence counters."""
        cls._global_store.clear()
        cls._global_next_id.clear()

    def close(self) -> None:
        """Release driver resources."""
        pass

    def get_next_id(self, table: Table) -> int:
        """Query current maximum primary key value and return next available integer ID."""
        if table.table_name not in self.tables_next_id:
            rows = self.tables_data.get(table.table_name, {})
            if not rows:
                self.tables_next_id[table.table_name] = 1
                return 1
            pk_idx = table.primary[0] if table.primary and len(table.primary) > 0 else 0
            max_pk = 0
            for r in rows.values():
                if pk_idx < len(r) and isinstance(r[pk_idx], int):
                    if r[pk_idx] > max_pk:
                        max_pk = r[pk_idx]
            self.tables_next_id[table.table_name] = max_pk + 1
        return self.tables_next_id[table.table_name]

    def select(self, table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None:
        """Single-row select matching non-None column wildcard values."""
        rows = self.tables_data.get(table.table_name)
        if not rows:
            return None

        # O(1) Primary Key Fast-Path
        if table.primary:
            all_pk_set = True
            for pk_col_idx in table.primary:
                if pk_col_idx >= len(data) or data[pk_col_idx] is None:
                    all_pk_set = False
                    break
            if all_pk_set:
                pk = data[table.primary[0]] if len(table.primary) == 1 else tuple(data[i] for i in table.primary)
                row = rows.get(pk)
                if row is not None:
                    for i, val in enumerate(data):
                        if val is not None and (i >= len(row) or row[i] != val):
                            return None
                    return row
                return None

        # Pre-filtered wildcard linear scan
        filters = [(i, val) for i, val in enumerate(data) if val is not None]
        if not filters:
            return next(iter(rows.values()))

        for row in rows.values():
            match = True
            for i, val in filters:
                if i >= len(row) or row[i] != val:
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
        primaries = table.primary
        curr_next_id = self.tables_next_id.get(table.table_name, 1)

        if primaries:
            if len(primaries) == 1:
                pk_idx = primaries[0]
                for row in data:
                    pk = row[pk_idx]
                    table_dict[pk] = tuple(row)
                    if isinstance(pk, int) and pk >= curr_next_id:
                        curr_next_id = pk + 1
            else:
                pk_0 = primaries[0]
                for row in data:
                    pk = tuple(row[i] for i in primaries)
                    table_dict[pk] = tuple(row)
                    first_val = row[pk_0]
                    if isinstance(first_val, int) and first_val >= curr_next_id:
                        curr_next_id = first_val + 1
        else:
            for row in data:
                pk = row[0] if len(row) > 0 else id(row)
                table_dict[pk] = tuple(row)
                if isinstance(pk, int) and pk >= curr_next_id:
                    curr_next_id = pk + 1

        self.tables_next_id[table.table_name] = curr_next_id

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
        if not t1_rows:
            return []

        t1_filters = [(i, val) for i, val in enumerate(columns[: t1.length]) if val is not None]

        current_composite_rows: list[list[SafeDataType]] = []
        for r1 in t1_rows.values():
            if t1_filters:
                match = True
                for i, val in t1_filters:
                    if i >= len(r1) or r1[i] != val:
                        match = False
                        break
                if not match:
                    continue
            current_composite_rows.append(list(r1))

        if not current_composite_rows:
            return []

        col_offset = t1.length
        for join in joins:
            if len(join) < 2:
                continue
            from_ptr, to_ptr = join[0], join[1]
            t_target = tables_dict[to_ptr[0]]
            t_target_rows = self.tables_data.get(t_target.table_name, {})
            if not t_target_rows:
                return []

            target_filters = [
                (j, val)
                for j, val in enumerate(columns[col_offset : col_offset + t_target.length])
                if val is not None
            ]

            to_col_idx = to_ptr[1]
            is_single_pk = bool(t_target.primary and len(t_target.primary) == 1 and t_target.primary[0] == to_col_idx)

            new_composite = []
            if is_single_pk:
                # O(1) direct dictionary lookup
                for comp in current_composite_rows:
                    from_val = comp[from_ptr[1]]
                    r_tgt = t_target_rows.get(from_val)
                    if r_tgt is not None:
                        if target_filters:
                            tgt_match = True
                            for j, val in target_filters:
                                if j >= len(r_tgt) or r_tgt[j] != val:
                                    tgt_match = False
                                    break
                            if tgt_match:
                                new_composite.append(comp + list(r_tgt))
                        else:
                            new_composite.append(comp + list(r_tgt))
            else:
                # Build hash index for multi-match target column
                target_hash_index: dict[SafeDataType, list[tuple[SafeDataType, ...]]] = defaultdict(list)
                for r_tgt in t_target_rows.values():
                    if target_filters:
                        tgt_match = True
                        for j, val in target_filters:
                            if j >= len(r_tgt) or r_tgt[j] != val:
                                tgt_match = False
                                break
                        if not tgt_match:
                            continue
                    target_hash_index[r_tgt[to_col_idx]].append(r_tgt)

                for comp in current_composite_rows:
                    from_val = comp[from_ptr[1]]
                    matching_target_rows = target_hash_index.get(from_val)
                    if matching_target_rows:
                        for r_tgt in matching_target_rows:
                            new_composite.append(comp + list(r_tgt))

            current_composite_rows = new_composite
            if not current_composite_rows:
                return []
            col_offset += t_target.length

        return [tuple(comp) for comp in current_composite_rows]

    def select_preload(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> list[tuple[SafeDataType, ...]]:
        """Query records for TableEngine startup preloading with column projection and version filtering."""
        rows = self.tables_data.get(table.table_name, {})
        if not rows:
            return []

        col_names = [col[0] for col in table.init_columns]
        has_vids = "vid_s" in col_names and "vid_e" in col_names
        has_vid = "vid" in col_names
        has_vids_only = "vid_s" in col_names and not has_vids

        vid_s_idx = col_names.index("vid_s") if "vid_s" in col_names else None
        vid_e_idx = col_names.index("vid_e") if "vid_e" in col_names else None
        vid_idx = col_names.index("vid") if "vid" in col_names else None

        results = []
        is_partial = cached_columns is not None and len(cached_columns) < table.length

        for row in rows.values():
            # Apply version-scoped filter
            if min_vid is not None and min_vid > 0:
                if has_vids and vid_e_idx is not None:
                    vid_e_val = row[vid_e_idx]
                    if vid_e_val is not None and vid_e_val != 0 and vid_e_val < min_vid:
                        continue
                elif has_vid and vid_idx is not None:
                    vid_val = row[vid_idx]
                    if vid_val is not None and vid_val < min_vid:
                        continue
                elif has_vids_only and vid_s_idx is not None:
                    vid_s_val = row[vid_s_idx]
                    if vid_s_val is not None and vid_s_val < min_vid:
                        continue

            # Slice cached columns if partial
            if is_partial and cached_columns is not None:
                proj = tuple(row[i] for i in cached_columns)
            else:
                proj = tuple(row)
            if any(isinstance(val, (bytearray, memoryview)) for val in proj):
                results.append(tuple(bytes(val) if isinstance(val, (bytearray, memoryview)) else val for val in proj))
            else:
                results.append(proj)
        return results

    def create_table(self, tables: Sequence[Table] | Table) -> None:
        """Register tables in in-memory schema catalog."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        for table in tables:
            self.tables_schema[table.table_id] = table
            if table.table_name not in self.tables_data:
                self.tables_data[table.table_name] = {}
            if table.table_name not in self.tables_next_id:
                self.tables_next_id[table.table_name] = 1
            if table.initial_insert:
                self.insert(table, table.initial_insert)

    def drop_table(self, tables: Sequence[Table] | Table) -> None:
        """Drop tables from in-memory schema catalog."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        for table in tables:
            self.tables_data.pop(table.table_name, None)
            self.tables_next_id.pop(table.table_name, None)
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

    def create_indexes(
        self,
        indexes: Sequence[tuple[str, Table, tuple[PointerType, ...]]],
        max_workers: int | None = None,
    ) -> None:
        """No-op for in-memory mock engine."""
        pass

    def remove_indexes(
        self,
        indexes: Sequence[tuple[str, Table]],
        max_workers: int | None = None,
    ) -> None:
        """No-op for in-memory mock engine."""
        pass

    def test_tables(self, tables: Sequence[Table] | Table) -> list[str] | None:
        """Check registered table existence."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        missing = [t.table_name for t in tables if t.table_name not in self.tables_data]
        return missing if missing else None

    def verify_relational_integrity(self, tables: Sequence[Table]) -> dict[str, int]:
        """Verify relational foreign key integrity across in-memory tables."""
        violations: dict[str, int] = {}
        for tbl in tables:
            if not getattr(tbl, "init_foreign", None):
                continue
            child_rows = self.tables_data.get(tbl.table_name, {})
            for fk in tbl.init_foreign:
                local_col_name, foreign_tbl_name, foreign_col_name = fk
                local_col_idx = [c[0] for c in tbl.init_columns].index(local_col_name)
                parent_data = self.tables_data.get(foreign_tbl_name, {})
                orphans = 0
                for row in child_rows.values():
                    val = row[local_col_idx]
                    if val is not None and val not in parent_data:
                        orphans += 1
                if orphans > 0:
                    violations[f"{tbl.table_name}.{local_col_name} -> {foreign_tbl_name}.{foreign_col_name}"] = orphans
        return violations
