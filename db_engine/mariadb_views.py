"""Joined view reads (single, chunked, multiple) of the MariaDB driver."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence
from core.globalstuff import (
    PointerGetter,
    SafeDataType,
    JoinsType,
)
from db_engine.mariadb_support import CHUNK_JOIN_SIZE, MAX_CANDIDATE_BATCH, MAX_JOIN_TABLES, _coerce_bytes, _is_binary_type


if TYPE_CHECKING:
    from core.TableHandling import Table



class MariaDBViews:
    """Joined view reads (single, chunked, multiple) of the MariaDB driver."""

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
            1. Checks template cache for join graph and column mask.
            2. Generates JOIN query if not cached.
            3. Extracts parameter tuple of non-None values.

        Outputs:
            Tuple `(sql_query_string, parameters_tuple)`.
        """
        mask = tuple(val is not None for val in columns)
        cache_key = (joins, mask)
        sql = self._view_sql_cache.get(cache_key)
        if sql is None:
            initial_pointer = PointerGetter(joins).get_first_pointer()
            sql = f"SELECT * FROM `{tables[initial_pointer[0]].table_name}` AS A1"

            data_offset = 0
            where_clauses = []
            for i, init_column in enumerate(tables[initial_pointer[0]].init_columns):
                if columns[i] is not None:
                    where_clauses.append(f"A1.`{init_column[0]}`=%s")
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
                        sql += f" JOIN `{tables[join[1][0]].table_name}` A{alias_offset} ON A{table_id_to_alias_dict[join[0][0]]}.`{tables[join[0][0]].init_columns[join[0][1]][0]}` = A{alias_offset}.`{tables[join[1][0]].init_columns[join[1][1]][0]}`"

                        for init_column in tables[join[1][0]].init_columns:
                            if data_offset < len(columns) and columns[data_offset] is not None:
                                where_clauses.append(f"A{alias_offset}.`{init_column[0]}`=%s")
                            data_offset += 1

            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)
            self._view_sql_cache[cache_key] = sql

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
                self._cur.execute(sql, tuple(base_params))
                rows = self._cur.fetchall()
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

                    self._cur.execute(sql, tuple(params))
                    rows = self._cur.fetchall()
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
                self._cur.execute(sql, tuple(params))
                chunk_row = self._cur.fetchone()

                if chunk_row is not None:
                    full_row.extend(bytes(val) if isinstance(val, (bytearray, memoryview)) else val for val in chunk_row)
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
        self._cur.execute(sql, params)
        res = self._cur.fetchone()
        if res is None:
            return None
        return self._normalize_view_row(self._view_binary_indices(tables, joins), res)

    @staticmethod
    def _view_binary_indices(tables: Sequence[Table] | dict[int, Table], joins: JoinsType) -> frozenset[int]:
        """Positions of the BINARY / VARBINARY / BLOB columns in a `SELECT *` over the join graph `joins`."""
        initial = PointerGetter(joins).get_first_pointer()
        cols = list(tables[initial[0]].init_columns)
        if len(joins) > 0 and len(joins[0]) > 1:
            for join in joins:
                for _ in range(join[2]):
                    cols.extend(tables[join[1][0]].init_columns)
        return frozenset(i for i, col in enumerate(cols) if _is_binary_type(col[1]))

    @staticmethod
    def _normalize_view_row(binary: frozenset[int], row: Sequence[Any]) -> tuple[SafeDataType, ...]:
        """`row` with the columns at `binary` positions (and any bytearray/memoryview) as `bytes`."""
        return tuple(
            _coerce_bytes(v) if (i in binary and v is not None) else (bytes(v) if isinstance(v, (bytearray, memoryview)) else v)
            for i, v in enumerate(row)
        )

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
        self._cur.execute(sql, params)
        rows = self._cur.fetchall()
        if not rows:
            return []
        binary = self._view_binary_indices(tables, joins)
        if binary or any(isinstance(val, (bytearray, memoryview)) for r in rows[:min(len(rows), 10)] for val in r):
            return [self._normalize_view_row(binary, row) for row in rows]
        return [tuple(row) for row in rows]
