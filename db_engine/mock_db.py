"""db_engine/mock_db.py - In-Memory Mock Database Driver.

Provides a fast, zero-dependency in-memory database driver adhering to the
BaseDBEngine specification for isolated unit testing and ChangeSet execution.
"""
from __future__ import annotations

import contextlib
import re
import time
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, Self
from types import TracebackType
from db_engine.base import BaseDBEngine, TableCommitStat, ColumnInfo, TableStats, NewPairs
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
    dedup_rows,
    find_oversize_in_term,
    has_empty_in_term,
    replace_in_term,
)
from db_engine.sql_render import resolve_source, validate_ident
from db_engine.views import ORDERED_VIEW_NAMES
from core.globalstuff import SafeDataType, JoinsType, PointerType, PointerGetter

if TYPE_CHECKING:
    from core.TableHandling import Table


def _sql_like_to_regex(pattern: str) -> str:
    res = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 < n:
                res.append(re.escape(pattern[i + 1]))
                i += 2
            else:
                res.append(re.escape("\\"))
                i += 1
        elif c == "%":
            res.append(".*")
            i += 1
        elif c == "_":
            res.append(".")
            i += 1
        else:
            res.append(re.escape(c))
            i += 1
    res.append("$")
    return "".join(res)


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
        self.tables_by_name: dict[str, Table] = {}

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

        is_idempotent = getattr(table, "idempotent_pk", False)
        if primaries:
            if len(primaries) == 1:
                pk_idx = primaries[0]
                for row in data:
                    pk = row[pk_idx]
                    if pk in table_dict:
                        if is_idempotent:
                            continue
                        raise ValueError(f"Duplicate entry for primary key '{pk}' in table '{table.table_name}'")
                    table_dict[pk] = tuple(row)
                    if isinstance(pk, int) and pk >= curr_next_id:
                        curr_next_id = pk + 1
            else:
                pk_0 = primaries[0]
                for row in data:
                    pk = tuple(row[i] for i in primaries)
                    if pk in table_dict:
                        if is_idempotent:
                            continue
                        raise ValueError(f"Duplicate entry for primary key '{pk}' in table '{table.table_name}'")
                    table_dict[pk] = tuple(row)
                    first_val = row[pk_0]
                    if isinstance(first_val, int) and first_val >= curr_next_id:
                        curr_next_id = first_val + 1
        else:
            for row in data:
                pk = row[0] if len(row) > 0 else id(row)
                if pk in table_dict:
                    raise ValueError(f"Duplicate entry for row in table '{table.table_name}'")
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

    def commit_tables_parallel(
        self,
        tables_data: Sequence[tuple[Table, Sequence[tuple[SafeDataType, ...]], Sequence[tuple[SafeDataType, ...]]]],
        max_workers: int | None = None,
        dedicated: bool = False,
    ) -> list[TableCommitStat]:
        """Commit inserts and updates for multiple tables in mock database, one table after the other.

        A failing table and the tables after it are not committed (`failed_table_ids` on the raised error).
        """
        items = [item for item in tables_data if item[1] or item[2]]
        stats: list[TableCommitStat] = []
        for pos, (table, insert_data, update_data) in enumerate(items):
            t_start = time.perf_counter()
            try:
                if insert_data:
                    self.insert(table, insert_data)
                if update_data:
                    self.update(table, update_data)
            except Exception as e:
                e.failed_table_ids = {t.table_id for t, _, _ in items[pos:]}
                raise
            stats.append(TableCommitStat(table.table_name, len(insert_data), len(update_data), time.perf_counter() - t_start))
        return stats

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

        table_offsets: dict[int, int] = {first_table_id: 0}
        col_offset = t1.length
        for join in joins:
            if len(join) < 2:
                continue
            from_ptr, to_ptr = join[0], join[1]
            from_col_idx = table_offsets.get(from_ptr[0], 0) + from_ptr[1]
            t_target = tables_dict[to_ptr[0]]
            table_offsets[to_ptr[0]] = col_offset
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
                    from_val = comp[from_col_idx]
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
                    from_val = comp[from_col_idx]
                    matching_target_rows = target_hash_index.get(from_val)
                    if matching_target_rows:
                        for r_tgt in matching_target_rows:
                            new_composite.append(comp + list(r_tgt))

            current_composite_rows = new_composite
            if not current_composite_rows:
                return []
            col_offset += t_target.length

        return [tuple(comp) for comp in current_composite_rows]

    def select_in(self, table: Table, col_idx: int, values: Sequence[SafeDataType]) -> list[tuple[SafeDataType, ...]]:
        """Every row whose column `col_idx` is one of `values`."""
        wanted = set(values)
        return [row for row in self.tables_data.get(table.table_name, {}).values() if col_idx < len(row) and row[col_idx] in wanted]

    def table_size(
        self,
        table: Table,
        cached_columns: tuple[int, ...] | None = None,
        min_vid: int | None = None,
    ) -> int:
        """Bytes the rows of `table` take, limited to `cached_columns` and to the window from `min_vid` on.

        The rows live in memory here, so their values are measured one by one instead of estimated; the
        window is the one `select_preload()` returns, so both report the same rows.
        """
        if not self.tables_data.get(table.table_name):
            return 0
        is_partial = bool(cached_columns) and len(cached_columns) < table.length
        rows = self.select_preload(table, cached_columns=cached_columns if is_partial else None, min_vid=min_vid)
        total = 0
        for row in rows:
            for val in row:
                if val is None:
                    continue
                total += len(val) if isinstance(val, (str, bytes, bytearray)) else 8
        return total

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
        has_commit = "commit_id" in col_names

        vid_s_idx = col_names.index("vid_s") if "vid_s" in col_names else None
        vid_e_idx = col_names.index("vid_e") if "vid_e" in col_names else None
        vid_idx = col_names.index("vid") if "vid" in col_names else None
        commit_idx = col_names.index("commit_id") if "commit_id" in col_names else None

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
                elif has_commit and commit_idx is not None:
                    commit_val = row[commit_idx]
                    if commit_val is not None and commit_val < min_vid:
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
            self.tables_by_name[table.table_name] = table
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
            self.tables_by_name.pop(table.table_name, None)

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

    def commit(self) -> None:
        """Commit pending in-memory state."""
        pass

    def rollback(self) -> None:
        """Roll back pending in-memory state."""
        pass

    def cursor(self, dictionary: bool = False) -> Any:
        """Lease a mock cursor."""
        class MockCursor:
            def __init__(self, db: MockDB) -> None:
                self.db = db
                self.rowcount = 0
                self.description = None

            def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
                self.rowcount = self.db.execute(sql, params)

            def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
                self.rowcount = self.db.executemany(sql, params_seq)

            def fetchone(self) -> Any:
                return None

            def fetchall(self) -> list[Any]:
                return []

            def fetchmany(self, size: int = 1) -> list[Any]:
                return []

            def close(self) -> None:
                pass

        return MockCursor(self)


    def query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> list[dict[str, Any] | tuple[SafeDataType, ...]]:
        """Mock SQL query execution."""
        return []

    def query_one(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> dict[str, Any] | tuple[SafeDataType, ...] | None:
        """Mock SQL single-row query execution."""
        res = self.query(sql, params, dictionary=dictionary)
        return res[0] if res else None

    def query_iter(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
        chunk_size: int = 2000,
    ) -> Iterator[dict[str, Any] | tuple[SafeDataType, ...]]:
        """Mock SQL streaming query execution."""
        rows = self.query(sql, params, dictionary=dictionary)
        yield from rows

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Mock SQL statement execution."""
        return 0

    def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
        """Mock SQL batch statement execution."""
        return len(params_seq)

    # -------------------------------------------------------------------------
    # Transaction Management (§3.6)
    # -------------------------------------------------------------------------
    @contextlib.contextmanager
    def transaction(self) -> Iterator[Self]:
        """Run operations within a transaction, rolling back snapshot on unhandled error."""
        snapshot_data = {tbl: dict(rows) for tbl, rows in self.tables_data.items()}
        snapshot_next_id = dict(self.tables_next_id)
        try:
            yield self
        except BaseException:
            self.tables_data.clear()
            for tbl, rows in snapshot_data.items():
                self.tables_data[tbl] = rows
            self.tables_next_id.clear()
            self.tables_next_id.update(snapshot_next_id)
            raise

    # -------------------------------------------------------------------------
    # Schema Introspection Helpers
    # -------------------------------------------------------------------------
    def _get_table_schema(self, table_name: str) -> Table | None:
        """Find Table object from registered schemas or canonical TABLES."""
        if hasattr(self, "tables_by_name") and table_name in self.tables_by_name:
            return self.tables_by_name[table_name]
        for t in self.tables_schema.values():
            if t.table_name == table_name:
                return t
        try:
            import core.DBLayout as dbl
            for t in getattr(dbl, "TABLES", ()):
                if t.table_name == table_name:
                    return t
        except Exception:
            pass
        return None

    def _get_column_names(self, table_name: str) -> list[str]:
        t = self._get_table_schema(table_name)
        if t and getattr(t, "init_columns", None):
            return [col[0] for col in t.init_columns]
        rows = self.tables_data.get(table_name)
        if rows:
            first_row = next(iter(rows.values()))
            return [f"col_{i}" for i in range(len(first_row))]
        return []

    # -------------------------------------------------------------------------
    # Spec Read Primitives (§2)
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
        """Execute spec query against in-memory mock tables."""
        if has_empty_in_term(where):
            return []

        term_idx, target_col, oversize_vals = find_oversize_in_term(where, chunk_size=1000)
        if oversize_vals is not None and target_col is not None:
            if limit is not None or offset is not None:
                raise ValueError("Auto-chunking cannot be combined with limit/offset")
            merged_rows: list[Any] = []
            for i in range(0, len(oversize_vals), 1000):
                chunk = oversize_vals[i : i + 1000]
                chunk_where = replace_in_term(where, term_idx, target_col, chunk)
                sub_rows = self.select_rows(
                    source,
                    columns,
                    where=chunk_where,
                    joins=joins,
                    distinct=distinct,
                    order_by=order_by,
                    as_dict=as_dict,
                )
                merged_rows.extend(sub_rows)
            if distinct:
                merged_rows = dedup_rows(merged_rows, bool(as_dict))
            return merged_rows

        tbl_name, _, alias = resolve_source(source)
        p_alias = alias or tbl_name

        # 1. Gather primary table rows
        if tbl_name == "m_checkpoint_tree" and not self.tables_data.get("m_checkpoint_tree"):
            # Synthesize m_checkpoint_tree from m_bridge_checkpoint and m_checkpoint_base
            raw_tuples = self._synthesize_checkpoint_tree()
            col_names = ["commit_id", "fnid", "blob_id"]
        else:
            raw_tuples = list(self.tables_data.get(tbl_name, {}).values())
            col_names = self._get_column_names(tbl_name)

        curr_rows: list[dict[str, Any]] = []
        for r in raw_tuples:
            r_dict = {c: (r[i] if i < len(r) else None) for i, c in enumerate(col_names)}
            r_dict.update({f"{p_alias}.{c}": (r[i] if i < len(r) else None) for i, c in enumerate(col_names)})
            curr_rows.append(r_dict)

        # 2. Joins
        for j in joins:
            j_name, _, j_alias = resolve_source(j.source)
            j_p_alias = j_alias or j_name
            if j_name == "m_checkpoint_tree" and not self.tables_data.get("m_checkpoint_tree"):
                j_raw_tuples = self._synthesize_checkpoint_tree()
                j_col_names = ["commit_id", "fnid", "blob_id"]
            else:
                j_raw_tuples = list(self.tables_data.get(j_name, {}).values())
                j_col_names = self._get_column_names(j_name)

            new_curr_rows: list[dict[str, Any]] = []
            for curr_r in curr_rows:
                matched: list[dict[str, Any]] = []
                for j_r in j_raw_tuples:
                    j_dict = {c: (j_r[i] if i < len(j_r) else None) for i, c in enumerate(j_col_names)}
                    j_dict.update({f"{j_p_alias}.{c}": (j_r[i] if i < len(j_r) else None) for i, c in enumerate(j_col_names)})
                    comb = dict(curr_r)
                    comb.update(j_dict)
                    if self._eval_join_on(j.on, comb):
                        matched.append(comb)
                if matched:
                    new_curr_rows.extend(matched)
                elif j.kind == "left":
                    empty_j = {c: None for c in j_col_names}
                    empty_j.update({f"{j_p_alias}.{c}": None for c in j_col_names})
                    comb = dict(curr_r)
                    comb.update(empty_j)
                    new_curr_rows.append(comb)
            curr_rows = new_curr_rows

        # 3. Filter WHERE
        filtered_rows = [r for r in curr_rows if self._eval_where(where, r)]

        # 4. Projection and Aggregation
        has_agg = self._has_aggregates(columns)
        if has_agg:
            # We compute single-row aggregate across all filtered rows
            result_rows = [self._compute_aggregates(columns, filtered_rows, bool(as_dict))]
        else:
            result_rows = []
            for r in filtered_rows:
                result_rows.append(self._project_row(columns, r, bool(as_dict)))

        # 5. Distinct
        if distinct:
            result_rows = self._dedup_result(result_rows, bool(as_dict))

        # 6. Order By
        if order_by:
            result_rows = self._sort_result(result_rows, order_by, columns, bool(as_dict))

        # 7. Offset & Limit
        off = offset or 0
        if limit is not None:
            result_rows = result_rows[off : off + limit]
        elif off > 0:
            result_rows = result_rows[off:]

        return result_rows

    def _synthesize_checkpoint_tree(self) -> list[tuple[Any, ...]]:
        """Dynamically compute m_checkpoint_tree view rows in memory."""
        c_rows = list(self.tables_data.get("m_bridge_checkpoint", {}).values())
        b_rows = list(self.tables_data.get("m_checkpoint_base", {}).values())
        base_commit_ids = {b[0] for b in b_rows if len(b) > 0}
        
        # c.commit_id, c.fnid, c.blob_id
        res: list[tuple[Any, ...]] = []
        for r in c_rows:
            cid, fnid, bid = r[0], r[1], r[2]
            if bid != 0 or cid not in base_commit_ids:
                res.append((cid, fnid, bid))

        # Second part: delta inherit
        # SELECT b.commit_id, k.fnid, k.blob_id FROM m_checkpoint_base b
        # JOIN m_bridge_checkpoint k ON k.commit_id = b.base_commit_id
        # WHERE NOT EXISTS (SELECT 1 FROM m_bridge_checkpoint d WHERE d.commit_id = b.commit_id AND d.fnid = k.fnid)
        c_by_commit_fnid = {(r[0], r[1]) for r in c_rows}
        c_by_commit: dict[int, list[tuple[Any, ...]]] = defaultdict(list)
        for r in c_rows:
            c_by_commit[r[0]].append(r)

        for b in b_rows:
            b_cid, base_cid = b[0], b[1]
            for k in c_by_commit.get(base_cid, []):
                k_fnid, k_bid = k[1], k[2]
                if (b_cid, k_fnid) not in c_by_commit_fnid:
                    res.append((b_cid, k_fnid, k_bid))
        return res

    def _eval_join_on(self, on: Mapping[str, Any], comb: dict[str, Any]) -> bool:
        for left_col, right_val in on.items():
            left_v = self._get_val(comb, left_col)
            if isinstance(right_val, Value):
                if left_v != right_val.v:
                    return False
            elif isinstance(right_val, Cond):
                if not self._eval_cond(left_v, right_val):
                    return False
            elif isinstance(right_val, str):
                right_v = self._get_val(comb, right_val)
                if left_v != right_v:
                    return False
            else:
                if left_v != right_val:
                    return False
        return True

    def _get_val(self, row_dict: dict[str, Any], col_name: str) -> Any:
        if col_name in row_dict:
            return row_dict[col_name]
        base_name = col_name.rsplit(".", 1)[-1]
        if base_name in row_dict:
            return row_dict[base_name]
        return None

    def _eval_cond(self, val: Any, cond: Cond) -> bool:
        op = cond.op
        if op == "ne":
            return val != cond.value
        if op == "lt":
            return val is not None and val < cond.value
        if op == "le":
            return val is not None and val <= cond.value
        if op == "gt":
            return val is not None and val > cond.value
        if op == "ge":
            return val is not None and val >= cond.value
        if op == "between":
            return val is not None and cond.value <= val <= cond.value2
        if op == "not_in":
            return val not in cond.value
        if op == "is_null":
            return val is None
        if op == "not_null":
            return val is not None
        if op == "ieq":
            return str(val or "").lower() == str(cond.value or "").lower()
        if op == "like":
            ci = bool(cond.value2)
            pat = _sql_like_to_regex(str(cond.value or ""))
            flags = re.IGNORECASE if ci else 0
            return bool(re.match(pat, str(val or ""), flags))
        if op == "hash_prefix":
            lo, hi = cond.value, cond.value2
            val_bytes = bytes(val) if isinstance(val, (bytes, bytearray, memoryview)) else val
            if hi is not None:
                return val_bytes >= lo and val_bytes < hi
            return val_bytes >= lo
        if op == "len_lt":
            return len(str(val or "")) < cond.value
        return False

    def _eval_where(self, where: Where | None, row_dict: dict[str, Any]) -> bool:
        if where is None:
            return True
        if isinstance(where, AnyOf):
            return any(self._eval_where(branch, row_dict) for branch in where.branches)
        if isinstance(where, (Exists, InSelect)):
            return self._eval_where([where], row_dict)
        if isinstance(where, Mapping):
            for k, v in where.items():
                if v is None:
                    raise TypeError(f"None cannot be used as an equality filter for '{k}'; use is_null()")
                row_val = self._get_val(row_dict, k)
                if isinstance(v, Cond):
                    if not self._eval_cond(row_val, v):
                        return False
                elif isinstance(v, Col):
                    if row_val != self._get_val(row_dict, v.name):
                        return False
                elif isinstance(v, (list, tuple, set, frozenset)):
                    if not v or row_val not in v:
                        return False
                else:
                    if row_val != v:
                        return False
            return True
        if isinstance(where, Sequence):
            for term in where:
                if isinstance(term, Mapping):
                    if not self._eval_where(term, row_dict):
                        return False
                elif isinstance(term, AnyOf):
                    if not any(self._eval_where(branch, row_dict) for branch in term.branches):
                        return False
                elif isinstance(term, Exists):
                    sub_where: list[Any] = []
                    if term.where:
                        if isinstance(term.where, Sequence):
                            sub_where.extend(term.where)
                        else:
                            sub_where.append(term.where)
                    for l_col, r_col in term.on.items():
                        if isinstance(r_col, Value):
                            sub_where.append({l_col: r_col.v})
                        elif isinstance(r_col, Cond):
                            sub_where.append({l_col: r_col})
                        elif isinstance(r_col, str):
                            sub_where.append({l_col: self._get_val(row_dict, r_col)})
                    sub_rows = self.select_rows(term.source, [Const(1)], where=sub_where, joins=term.joins, limit=1)
                    matched = bool(sub_rows)
                    if term.negate:
                        matched = not matched
                    if not matched:
                        return False
                elif isinstance(term, InSelect):
                    sel_cols = [term.select] if isinstance(term.select, str) else list(term.select)
                    sub_rows = self.select_rows(term.source, sel_cols, where=term.where, as_dict=False)
                    if isinstance(term.columns, (tuple, list)):
                        curr_vals = tuple(self._get_val(row_dict, c) for c in term.columns)
                        in_sub = curr_vals in sub_rows
                    else:
                        curr_val = self._get_val(row_dict, term.columns)
                        in_sub = any(r[0] == curr_val for r in sub_rows)
                    if term.negate:
                        in_sub = not in_sub
                    if not in_sub:
                        return False
            return True
        return True

    def _has_aggregates(self, columns: Sequence | Mapping) -> bool:
        items = columns.values() if isinstance(columns, Mapping) else columns
        return any(isinstance(x, Agg) for x in items)

    def _compute_aggregates(self, columns: Sequence | Mapping, filtered_rows: list[dict[str, Any]], as_dict: bool) -> Any:
        res_dict: dict[str, Any] = {}
        res_list: list[Any] = []

        if isinstance(columns, Mapping):
            for alias, item in columns.items():
                val = self._eval_col_item(item, filtered_rows)
                res_dict[alias] = val
            return res_dict if as_dict else tuple(res_dict.values())
        else:
            for idx, item in enumerate(columns):
                val = self._eval_col_item(item, filtered_rows)
                res_dict[f"col_{idx}"] = val
                res_list.append(val)
            return res_dict if as_dict else tuple(res_list)

    def _eval_col_item(self, item: Any, rows: list[dict[str, Any]]) -> Any:
        if isinstance(item, Const):
            return item.value
        if isinstance(item, Agg):
            func = item.func
            col = item.column
            if func == "COUNT":
                if col == "*":
                    return len(rows)
                vals = [self._get_val(r, col) for r in rows if self._get_val(r, col) is not None]
                return len(set(vals)) if item.distinct else len(vals)
            vals = [self._get_val(r, col) for r in rows if self._get_val(r, col) is not None]
            if not vals:
                return None
            if func == "SUM":
                return sum(vals)
            if func == "MAX":
                return max(vals)
            if func == "MIN":
                return min(vals)
            if func == "AVG":
                return sum(vals) / len(vals)
        if isinstance(item, str):
            return self._get_val(rows[0], item) if rows else None
        return None

    def _project_row(self, columns: Sequence | Mapping, row_dict: dict[str, Any], as_dict: bool) -> Any:
        if isinstance(columns, Mapping):
            res_dict: dict[str, Any] = {}
            for alias, item in columns.items():
                if isinstance(item, Const):
                    res_dict[alias] = item.value
                elif isinstance(item, str):
                    res_dict[alias] = self._get_val(row_dict, item)
            return res_dict if as_dict else tuple(res_dict.values())
        else:
            res_list: list[Any] = []
            res_dict: dict[str, Any] = {}
            for idx, item in enumerate(columns):
                if isinstance(item, Const):
                    val = item.value
                    col_name = f"const_{idx}"
                elif isinstance(item, str):
                    val = self._get_val(row_dict, item)
                    col_name = item.rsplit(".", 1)[-1]
                else:
                    val = None
                    col_name = f"col_{idx}"
                res_list.append(val)
                res_dict[col_name] = val
            return res_dict if as_dict else tuple(res_list)

    def _dedup_result(self, rows: list[Any], as_dict: bool) -> list[Any]:
        seen = set()
        deduped = []
        for r in rows:
            if as_dict and isinstance(r, dict):
                k = tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in r.items()))
            else:
                k = r
            if k not in seen:
                seen.add(k)
                deduped.append(r)
        return deduped

    def _sort_result(
        self,
        rows: list[Any],
        order_by: Sequence[str],
        columns: Sequence | Mapping,
        as_dict: bool,
    ) -> list[Any]:
        if not order_by or not rows:
            return rows
        result = list(rows)
        # Determine column names
        if isinstance(columns, Mapping):
            col_names = list(columns.keys())
        else:
            col_names = [c if isinstance(c, str) else f"col_{i}" for i, c in enumerate(columns)]

        for order_spec in reversed(order_by):
            clean = order_spec.strip()
            desc = clean.startswith("-")
            col = clean[1:] if desc else clean
            base_col = col.rsplit(".", 1)[-1]

            def _get_sort_val(r: Any) -> Any:
                if as_dict and isinstance(r, dict):
                    return r.get(col, r.get(base_col))
                else:
                    if col in col_names:
                        idx = col_names.index(col)
                    elif base_col in col_names:
                        idx = col_names.index(base_col)
                    else:
                        return None
                    return r[idx] if idx < len(r) else None

            result.sort(key=lambda r: (_get_sort_val(r) is None, _get_sort_val(r)), reverse=desc)
        return result

    # -------------------------------------------------------------------------
    # Spec Write Primitives (§3)
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
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        table_obj = self._get_table_schema(tbl_name)
        if table_obj and getattr(table_obj, "init_columns", None):
            schema_cols = [c[0] for c in table_obj.init_columns]
            primaries = list(table_obj.primary)
        else:
            schema_cols = list(columns)
            primaries = [0]

        table_dict = self.tables_data[tbl_name]
        curr_next_id = self.tables_next_id.get(tbl_name, 1)
        affected = 0

        for r in rows:
            row_dict = {c: v for c, v in zip(columns, r)}
            full_row_list = []
            for idx, c in enumerate(schema_cols):
                if c in row_dict:
                    full_row_list.append(row_dict[c])
                elif primaries and idx == primaries[0]:
                    full_row_list.append(curr_next_id)
                else:
                    full_row_list.append(None)
            full_row = tuple(full_row_list)

            if len(primaries) == 1:
                pk = full_row[primaries[0]]
            else:
                pk = tuple(full_row[i] for i in primaries)

            if pk in table_dict:
                if on_conflict == "error":
                    raise ValueError(f"Duplicate entry for primary key '{pk}' in table '{tbl_name}'")
                elif on_conflict == "ignore":
                    continue
                elif on_conflict in ("update", "replace"):
                    existing = list(table_dict[pk])
                    up_cols = update_columns if update_columns is not None else columns
                    for c in up_cols:
                        if c in schema_cols:
                            c_idx = schema_cols.index(c)
                            if c in row_dict:
                                existing[c_idx] = row_dict[c]
                    table_dict[pk] = tuple(existing)
                    affected += 2 if on_conflict == "update" else 1
            else:
                table_dict[pk] = full_row
                affected += 1
                if isinstance(pk, int) and pk >= curr_next_id:
                    curr_next_id = pk + 1

        self.tables_next_id[tbl_name] = curr_next_id
        return affected

    def update_rows(
        self,
        table: Table | str,
        values: Mapping[str, Any],
        where: Where | None,
        *,
        limit: int | None = None,
    ) -> int:
        """Update rows matching where filter. Returns affected row count."""
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        table_dict = self.tables_data.get(tbl_name, {})
        col_names = self._get_column_names(tbl_name)

        matched_pks: list[Any] = []
        for pk, row in table_dict.items():
            r_dict = {c: row[i] if i < len(row) else None for i, c in enumerate(col_names)}
            if self._eval_where(where, r_dict):
                matched_pks.append(pk)
                if limit is not None and len(matched_pks) >= limit:
                    break

        for pk in matched_pks:
            curr_row = list(table_dict[pk])
            r_dict = {c: curr_row[i] if i < len(curr_row) else None for i, c in enumerate(col_names)}
            for col, val in values.items():
                if col in col_names:
                    idx = col_names.index(col)
                    if isinstance(val, Inc):
                        curr_row[idx] = (curr_row[idx] or 0) + val.n
                    elif isinstance(val, Col):
                        curr_row[idx] = r_dict.get(val.name)
                    else:
                        curr_row[idx] = val
            table_dict[pk] = tuple(curr_row)

        return len(matched_pks)

    def delete_rows(
        self,
        table: Table | str,
        where: Where | None,
        *,
        limit: int | None = None,
        all_rows: bool = False,
    ) -> int:
        """Delete rows matching where filter. Returns affected row count."""
        if where is None and not all_rows:
            raise ValueError("delete_rows without where requires all_rows=True")
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        table_dict = self.tables_data.get(tbl_name, {})
        col_names = self._get_column_names(tbl_name)

        matched_pks: list[Any] = []
        for pk, row in table_dict.items():
            r_dict = {c: row[i] if i < len(row) else None for i, c in enumerate(col_names)}
            if self._eval_where(where, r_dict):
                matched_pks.append(pk)
                if limit is not None and len(matched_pks) >= limit:
                    break

        for pk in matched_pks:
            table_dict.pop(pk, None)

        return len(matched_pks)

    def copy_rows(
        self,
        table: Table | str,
        columns: Sequence[str],
        select: Select,
        *,
        on_conflict: Literal["error", "ignore"] = "error",
    ) -> int:
        """Evaluate select query and insert rows into table."""
        rows = self.select_rows(
            select.source,
            select.columns,
            select.where,
            joins=select.joins,
            distinct=select.distinct,
            order_by=select.order_by,
            limit=select.limit,
            offset=select.offset,
            as_dict=False,
        )
        return self.insert_rows(table, columns, rows, on_conflict=on_conflict)

    # -------------------------------------------------------------------------
    # Catalog and Schema Maintenance (§4 & §5)
    # -------------------------------------------------------------------------
    def drop_tables(
        self,
        names: Sequence[str],
        *,
        if_exists: bool = True,
        disable_fk_checks: bool = True,
    ) -> None:
        """Drop tables from in-memory catalog."""
        for n in names:
            self.tables_data.pop(n, None)
            self.tables_next_id.pop(n, None)
            self.tables_by_name.pop(n, None)

    def column_exists(self, table: Table | str, column: str) -> bool:
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        return column in self._get_column_names(tbl_name)

    def existing_tables(self, names: Sequence[str], *, include_views: bool = False) -> set[str]:
        res = set()
        for n in names:
            if n in self.tables_data or n in self.tables_by_name or self._get_table_schema(n) is not None:
                res.add(n)
            elif include_views and n in ORDERED_VIEW_NAMES:
                res.add(n)
        return res

    def column_info(self, table: Table | str, column: str) -> ColumnInfo | None:
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        t = self._get_table_schema(tbl_name)
        if t and getattr(t, "init_columns", None):
            for col_def in t.init_columns:
                if col_def[0] == column:
                    col_type = col_def[1]
                    nullable = "NOT NULL" not in col_def
                    octet_len = 16 if "BINARY(16)" in col_type else (32 if "BINARY(32)" in col_type else None)
                    return ColumnInfo(data_type=col_type.split("(")[0].lower(), column_type=col_type, octet_length=octet_len, nullable=nullable)
        if column in self._get_column_names(tbl_name):
            return ColumnInfo(data_type="varchar", column_type="VARCHAR(255)", octet_length=255, nullable=True)
        return None

    def index_names(self, table: Table | str) -> set[str]:
        return set()

    def table_stats(self, names: Sequence[str] | None = None) -> list[TableStats]:
        stats: list[TableStats] = []
        target_names = names if names is not None else list(self.tables_data.keys())
        for name in target_names:
            rows = self.tables_data.get(name, {})
            stats.append(TableStats(name=name, rows=len(rows), data_bytes=len(rows) * 64, index_bytes=0))
        stats.sort(key=lambda s: s.data_bytes, reverse=True)
        return stats

    def server_variable(self, name: str) -> str | None:
        return None

    def apply_server_tuning(self, settings: dict[str, Any] | None = None) -> list[str]:
        return []

    def ensure_columns(self, table: Table, columns: Sequence[str] | None = None) -> list[str]:
        return []

    def ensure_views(self, names: Sequence[str] | None = None) -> dict[str, Exception | None]:
        view_names = names if names is not None else ORDERED_VIEW_NAMES
        return {v: None for v in view_names}

    def resolve_relation_name(self, name: str) -> str:
        return name

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
        c_rows = list(self.tables_data.get("m_bridge_checkpoint", {}).values())
        cur_pairs = {(r[1], r[2]) for r in c_rows if r[0] == commit_id}
        old_pairs = {(r[1], r[2]) for r in c_rows if r[0] == prev_commit_id}
        diff_pairs = cur_pairs - old_pairs
        yield MockNewPairs(self, diff_pairs)

    def enqueue_parse_tasks(self, rows: Sequence[Sequence[Any]]) -> int:
        """Insert or update tasks in m_parse_task in memory."""
        tbl_name = "m_parse_task"
        if tbl_name not in self.tables_data:
            self.tables_data[tbl_name] = {}
        t_dict = self.tables_data[tbl_name]
        curr_next_id = self.tables_next_id.get(tbl_name, 1)

        enqueued = 0
        for r in rows:
            net_id, c_name, c_kind, s_ord, r_name, cid, b_name, b_cid, init_st = r
            # Unique key on m_parse_task: (network_id, chain_name, seq_order, ref_name)
            key = (net_id, c_name, s_ord, r_name)
            found_pk = None
            for pk, existing in t_dict.items():
                if (existing[1], existing[2], existing[4], existing[5]) == key:
                    found_pk = pk
                    break

            if found_pk is not None:
                old = list(t_dict[found_pk])
                old_status = old[9]
                new_status = init_st
                if old_status == 4:  # TASK_FAILED
                    resolved_status = new_status
                elif old_status == 3 and new_status != 3:  # TASK_DONE reopened
                    resolved_status = new_status
                elif old_status in (2, 5):  # LEASED / RECLAIMING
                    resolved_status = 3 if new_status == 3 else old_status
                else:
                    resolved_status = new_status

                reset_retries = (old_status == 4) or (old_status == 3 and new_status != 3)
                old[1] = net_id
                old[2] = c_name
                old[3] = c_kind
                old[4] = s_ord
                old[5] = r_name
                old[6] = cid if cid > 0 else old[6]
                old[7] = b_name
                old[8] = b_cid if b_cid > 0 else old[8]
                old[9] = resolved_status
                if reset_retries:
                    old[11] = 0   # retry_count
                    old[12] = ""  # error_msg
                t_dict[found_pk] = tuple(old)
                enqueued += 1
            else:
                task_id = curr_next_id
                curr_next_id += 1
                row_tuple = (
                    task_id, net_id, c_name, c_kind, s_ord, r_name, cid, b_name, b_cid,
                    init_st, "", 0, 0, ""
                )
                t_dict[task_id] = row_tuple
                enqueued += 1

        self.tables_next_id[tbl_name] = curr_next_id
        return enqueued

    def record_task_failure(
        self,
        task_id: int,
        error_msg: str,
        *,
        worker_id: str | None = None,
        max_attempts: int = 3,
    ) -> int:
        """Record task failure in memory."""
        t_dict = self.tables_data.get("m_parse_task", {})
        if task_id not in t_dict:
            return 0
        row = list(t_dict[task_id])
        status = row[9]
        w_id = row[10]
        if status not in (2, 5):
            return 0
        if worker_id is not None and w_id != worker_id:
            return 0

        retry_count = row[11] + 1
        new_status = 4 if retry_count >= max_attempts else 1
        row[9] = new_status
        row[10] = ""
        row[11] = retry_count
        row[12] = error_msg[:1000]
        t_dict[task_id] = tuple(row)
        return 1

    def next_eligible_parse_tasks(
        self,
        network_id: int,
        status: int,
        *,
        limit: int = 10,
    ) -> list[tuple]:
        """Fetch eligible parse tasks in memory."""
        t_dict = self.tables_data.get("m_parse_task", {})
        candidates = []
        for r in t_dict.values():
            if r[1] == network_id and r[9] == status:
                candidates.append((r[0], r[2], r[3], r[5], r[6], r[7], r[8]))

        # ORDER BY (chain_kind = 'stable') DESC, LENGTH(chain_name) ASC, chain_name ASC, seq_order ASC
        candidates.sort(key=lambda c: (c[2] != "stable", len(c[1]), c[1]))
        return candidates[:limit]

    # -------------------------------------------------------------------------
    # Webapp Read API Proxy (§6)
    # -------------------------------------------------------------------------
    def reader(self) -> MockReadSession:
        """Return a ReadAPI view on this MockDB instance with as_dict=True default."""
        return MockReadSession(self)


class MockNewPairs(NewPairs):
    """In-memory implementation of NewPairs."""

    def __init__(self, db: MockDB, pairs: set[tuple[int, int]]) -> None:
        self.db = db
        self.pairs = pairs

    def delete_rows_of(self, table: Table | str) -> int:
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        tbl_dict = self.db.tables_data.get(tbl_name, {})
        to_del = []
        for pk, row in tbl_dict.items():
            pair = (row[1], row[0]) if len(row) >= 2 else None
            if pair in self.pairs:
                to_del.append(pk)
        for pk in to_del:
            tbl_dict.pop(pk, None)
        return len(to_del)

    def insert_include_references(
        self,
        kconfig_type_ids: Sequence[int] | str,
        kconfig_ref_type: int,
        include_ref_type: int,
    ) -> int:
        return 0


class MockReadSession:
    """MockDB ReadAPI proxy exposing read methods with as_dict=True default."""

    def __init__(self, db: MockDB) -> None:
        self._db = db

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

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
        dict_mode = True if as_dict is None else as_dict
        return self._db.select_rows(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            offset=offset,
            as_dict=dict_mode,
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
    ) -> Iterator[tuple | dict]:
        dict_mode = True if as_dict is None else as_dict
        return self._db.iter_rows(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            offset=offset,
            as_dict=dict_mode,
        )

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
        dict_mode = True if as_dict is None else as_dict
        return self._db.select_one(
            source,
            columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            as_dict=dict_mode,
        )

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
        return self._db.select_values(
            source,
            column,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
        )

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
        return self._db.select_value(
            source,
            column,
            where=where,
            joins=joins,
            order_by=order_by,
            default=default,
        )

    def count_rows(
        self,
        source: Source,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
        distinct: str | None = None,
    ) -> int:
        return self._db.count_rows(source, where=where, joins=joins, distinct=distinct)

    def row_exists(
        self,
        source: Source,
        where: Where | None = None,
        *,
        joins: Sequence[Join] = (),
    ) -> bool:
        return self._db.row_exists(source, where=where, joins=joins)

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
        dict_mode = True if as_dict is None else as_dict
        return self._db.aggregate(
            source,
            aggregates,
            where=where,
            joins=joins,
            group_by=group_by,
            order_by=order_by,
            as_dict=dict_mode,
        )

    def max_value(self, source: Source, column: str, where: Where | None = None, *, default: Any = 0) -> Any:
        return self._db.max_value(source, column, where=where, default=default)

    def min_value(self, source: Source, column: str, where: Where | None = None, *, default: Any = 0) -> Any:
        return self._db.min_value(source, column, where=where, default=default)

    def column_exists(self, table: Table | str, column: str) -> bool:
        return self._db.column_exists(table, column)

    def existing_tables(self, names: Sequence[str], *, include_views: bool = False) -> set[str]:
        return self._db.existing_tables(names, include_views=include_views)

    def column_info(self, table: Table | str, column: str) -> ColumnInfo | None:
        return self._db.column_info(table, column)

    def index_names(self, table: Table | str) -> set[str]:
        return self._db.index_names(table)

    def table_stats(self, names: Sequence[str] | None = None) -> list[TableStats]:
        return self._db.table_stats(names)

    def resolve_relation_name(self, name: str) -> str:
        return self._db.resolve_relation_name(name)

