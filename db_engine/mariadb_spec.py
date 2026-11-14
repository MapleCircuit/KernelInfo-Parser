"""Spec-based reads/writes, view creation and pipeline intents of the MariaDB driver."""
from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Iterator, Sequence
import mysql.connector

from db_engine.base import (
    NewPairs,
)
from db_engine.spec import (
    Agg,
    Const,
    Join,
    Select,
    Source,
    Where,
    dedup_rows as _dedup_rows,
    find_oversize_in_term as _find_oversize_in_term,
    has_empty_in_term as _has_empty_in_term,
    replace_in_term as _replace_in_term,
)
from db_engine.sql_render import (
    render_drop_tables,
    render_select_literal,
    render_insert_with_params,
    render_select,
    render_update,
    render_delete,
    render_copy,
    validate_ident,
)

if TYPE_CHECKING:
    from db_engine.DBHandling import MariaDB
    from core.TableHandling import Table
from db_engine.mariadb_support import _binary_names_of, _normalize_row, _sort_rows, logger



class MariaDBSpec:
    """Spec-based reads/writes, view creation and pipeline intents of the MariaDB driver."""

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
        group_by: Sequence[Any] = (),
        having: Where | None = None,
        hint: Literal["straight_join"] | None = None,
    ) -> list[tuple | dict]:
        """Execute parameterized SELECT query from spec and return rows (BINARY columns come back as `bytes`)."""
        # 1. Empty IN check: returns [] without sending any query
        if _has_empty_in_term(where):
            return []

        # 2. Check for oversize IN list chunking (> 1000 values); grouped/aggregated reads are never split
        term_idx, target_col, oversize_vals = _find_oversize_in_term(where, chunk_size=1000)
        if oversize_vals is not None and target_col is not None and not group_by and having is None:
            if limit is not None or offset is not None:
                raise ValueError("Auto-chunking cannot be combined with limit/offset")
            merged_rows: list[Any] = []
            col_names_cached: list[str] = []
            for i in range(0, len(oversize_vals), 1000):
                chunk = oversize_vals[i : i + 1000]
                chunk_where = _replace_in_term(where, term_idx, target_col, chunk)
                sub_rows = self.select_rows(
                    source,
                    columns,
                    where=chunk_where,
                    joins=joins,
                    distinct=distinct,
                    order_by=order_by,
                    as_dict=as_dict,
                    hint=hint,
                )
                merged_rows.extend(sub_rows)

            if distinct:
                merged_rows = _dedup_rows(merged_rows, bool(as_dict))

            if order_by:
                # Introspect col_names from spec for sorting
                _, _, col_names = render_select(
                    Select(source, columns, None, joins, distinct, order_by),
                    dialect="mariadb",
                )
                merged_rows = _sort_rows(merged_rows, order_by, col_names, bool(as_dict))

            return merged_rows

        # 3. Direct execution
        select_spec = Select(
            source=source,
            columns=columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            offset=offset,
            group_by=group_by,
            having=having,
            hint=hint,
        )
        sql, params, col_names = render_select(select_spec, dialect=self.dialect)
        rows = self._raw_query(sql, params, dictionary=bool(as_dict))
        binary = self._binary_output_columns(source, joins, columns, col_names)
        return [_normalize_row(r, bool(as_dict), binary, col_names) for r in rows]

    @staticmethod
    def _binary_output_columns(
        source: Source,
        joins: Sequence[Join],
        columns: Any,
        col_names: Sequence[str],
    ) -> frozenset[str]:
        """Output column names (aliases) of a spec read that project a BINARY / VARBINARY / BLOB column as-is."""
        bin_src = _binary_names_of([source, *(j.source for j in joins)])
        if not bin_src:
            return frozenset()
        out: set[str] = set()
        items = list(columns.items()) if isinstance(columns, Mapping) else [(n, c) for n, c in zip(col_names, columns)]
        for out_name, item in items:
            if isinstance(item, str) and item.rsplit(".", 1)[-1] in bin_src:
                out.add(out_name)
        return frozenset(out)

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
        """Stream query result rows using cursor streaming."""
        if _has_empty_in_term(where):
            return
        term_idx, target_col, oversize_vals = _find_oversize_in_term(where, chunk_size=1000)
        if oversize_vals is not None and not group_by and having is None:
            # Fall back to iterating merged rows for chunked queries
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
                hint=hint,
            )
            yield from rows
            return

        select_spec = Select(
            source=source,
            columns=columns,
            where=where,
            joins=joins,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            offset=offset,
            group_by=group_by,
            having=having,
            hint=hint,
        )
        sql, params, col_names = render_select(select_spec, dialect=self.dialect)
        binary = self._binary_output_columns(source, joins, columns, col_names)
        for r in self._raw_query_iter(sql, params, dictionary=bool(as_dict)):
            yield _normalize_row(r, bool(as_dict), binary, col_names)

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
        update_exprs: Mapping[str, Any] | None = None,
    ) -> int:
        """Insert row(s) matching columns with adaptive bisection. Returns the rows written (`BaseDBEngine.insert_rows`).

        See `BaseDBEngine.insert_rows()` for `update_columns` / `update_exprs` (conditional upsert).
        """
        self._require_writable()
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        clean_cols = [validate_ident(c) for c in columns]
        if not clean_cols:
            raise ValueError("insert_rows requires at least one column")

        max_batch_rows = chunk_size or 5000
        max_batch_bytes = 4 * 1024 * 1024

        def _estimate_row_bytes(row: Sequence[Any]) -> int:
            return sum(len(x) if isinstance(x, (str, bytes, bytearray, memoryview)) else 8 for x in row)

        def _exec_batch(batch: list[tuple[Any, ...]]) -> int:
            if not batch:
                return 0
            sql, trailing = render_insert_with_params(
                tbl_name,
                clean_cols,
                len(batch),
                on_conflict=on_conflict,
                update_columns=update_columns,
                dialect=self.dialect,
                update_exprs=update_exprs,
            )
            flat_params = [v for r in batch for v in r] + trailing
            for attempt in range(3):
                self.check_if_connected()
                try:
                    import contextlib
                    with contextlib.closing(self.cnx.cursor()) as cur:
                        cur.execute(sql, flat_params)
                        rc = cur.rowcount
                        self._last_ok = time.monotonic()  # outside transaction() autocommit has committed it
                        if on_conflict in ("update", "replace"):
                            return len(batch)  # server counts 1 or 2 per row; every offered row is written
                        return max(0, rc)
                except mysql.connector.Error as err:
                    if (getattr(err, "errno", None) == 1153 or "max_allowed_packet" in str(err).lower()) and len(batch) > 1:
                        mid = len(batch) // 2
                        logger.warning(
                            f"Packet exceeded max_allowed_packet inserting into '{tbl_name}' with {len(batch)} rows. "
                            f"Bisecting into {mid} and {len(batch) - mid} rows..."
                        )
                        return _exec_batch(batch[:mid]) + _exec_batch(batch[mid:])
                    if isinstance(err, (mysql.connector.OperationalError, mysql.connector.InterfaceError)):
                        if self._in_txn:
                            raise
                        self.close()
                        if attempt == 2:
                            raise
                        time.sleep(0.5 * (attempt + 1))
                    else:
                        raise
            return 0

        total_affected = 0
        current_batch: list[tuple[Any, ...]] = []
        current_bytes = 0

        for r in rows:
            r_tuple = tuple(r)
            r_bytes = _estimate_row_bytes(r_tuple)
            if current_batch and (len(current_batch) >= max_batch_rows or current_bytes + r_bytes > max_batch_bytes):
                total_affected += _exec_batch(current_batch)
                current_batch = [r_tuple]
                current_bytes = r_bytes
            else:
                current_batch.append(r_tuple)
                current_bytes += r_bytes

        if current_batch:
            total_affected += _exec_batch(current_batch)

        return total_affected

    def update_rows(
        self,
        table: Table | str,
        values: Mapping[str, Any],
        where: Where | None,
        *,
        limit: int | None = None,
    ) -> int:
        """Update rows matching where filter. Returns the rows matched (`BaseDBEngine.update_rows`)."""
        if _has_empty_in_term(where):
            return 0

        term_idx, target_col, oversize_vals = _find_oversize_in_term(where, chunk_size=1000)
        if oversize_vals is not None and target_col is not None:
            if limit is not None:
                raise ValueError("Auto-chunking cannot be combined with limit")
            total_rc = 0
            for i in range(0, len(oversize_vals), 1000):
                chunk = oversize_vals[i : i + 1000]
                chunk_where = _replace_in_term(where, term_idx, target_col, chunk)
                total_rc += self.update_rows(table, values, chunk_where)
            return total_rc

        sql, params = render_update(table, values, where, limit=limit, dialect=self.dialect)
        return self._raw_execute(sql, params)

    def delete_rows(
        self,
        table: Table | str,
        where: Where | None,
        *,
        limit: int | None = None,
        all_rows: bool = False,
    ) -> int:
        """Delete rows matching where filter. Returns the rows deleted."""
        if where is None and not all_rows:
            raise ValueError("delete_rows without where requires all_rows=True")
        if _has_empty_in_term(where):
            return 0

        term_idx, target_col, oversize_vals = _find_oversize_in_term(where, chunk_size=1000)
        if oversize_vals is not None and target_col is not None:
            if limit is not None:
                raise ValueError("Auto-chunking cannot be combined with limit")
            total_rc = 0
            for i in range(0, len(oversize_vals), 1000):
                chunk = oversize_vals[i : i + 1000]
                chunk_where = _replace_in_term(where, term_idx, target_col, chunk)
                total_rc += self.delete_rows(table, chunk_where, all_rows=all_rows)
            return total_rc

        sql, params = render_delete(table, where, limit=limit, all_rows=all_rows, dialect=self.dialect)
        return self._raw_execute(sql, params)

    def copy_rows(
        self,
        table: Table | str,
        columns: Sequence[str],
        select: Select,
        *,
        on_conflict: Literal["error", "ignore"] = "error",
    ) -> int:
        """Insert rows selected by select query into table. Returns the rows inserted."""
        sql, params = render_copy(table, columns, select, on_conflict=on_conflict, dialect=self.dialect)
        return self._raw_execute(sql, params)

    def drop_tables(
        self,
        names: Sequence[str],
        *,
        ignore_fk: bool = False,
        if_exists: bool = True,
    ) -> None:
        """Drop tables by name; `ignore_fk=True` suspends foreign-key checks for the statement (and restores them)."""
        if not names:
            return
        self.check_if_connected()
        statements = render_drop_tables(names, if_exists=if_exists, dialect=self.dialect)
        previous = None
        if ignore_fk:
            self._cur.execute("SELECT @@SESSION.foreign_key_checks")
            row = self._cur.fetchone()
            previous = int(row[0]) if row and row[0] is not None else 1
            self._cur.execute("SET foreign_key_checks = 0")
        try:
            for stmt in statements:
                self._cur.execute(stmt)
            if not self._in_txn:
                self.commit()
        finally:
            if previous is not None:
                try:
                    self._cur.execute(f"SET foreign_key_checks = {previous}")
                except Exception:
                    pass

    def create_view(self, view_name: str, select: Select | str, replace: bool = True) -> None:
        """Create or replace a view from a `Select` spec (rendered with inline literals) or a legacy SQL string."""
        validate_ident(view_name)
        body = render_select_literal(select, self.dialect) if isinstance(select, Select) else str(select)
        self._raw_execute(f"CREATE {'OR REPLACE ' if replace else ''}VIEW {view_name} AS {body}")

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
        self.check_if_connected()
        has_tmp = False
        pairs: set[tuple[int, int]] = set()
        try:
            self._raw_execute("DROP TEMPORARY TABLE IF EXISTS tmp_new_pairs")
            self._raw_execute(
                "CREATE TEMPORARY TABLE tmp_new_pairs (fnid INT NOT NULL, blob_id INT NOT NULL, PRIMARY KEY (fnid, blob_id))"
            )
            self._raw_execute(
                "INSERT INTO tmp_new_pairs (fnid, blob_id) "
                "SELECT cur_v.fnid, cur_v.blob_id FROM m_bridge_checkpoint cur_v "
                "LEFT JOIN m_bridge_checkpoint old_v "
                "       ON old_v.commit_id = %s AND old_v.fnid = cur_v.fnid AND old_v.blob_id = cur_v.blob_id "
                "WHERE cur_v.commit_id = %s AND old_v.blob_id IS NULL",
                (prev_commit_id, commit_id),
            )
            rows = self._raw_query("SELECT fnid, blob_id FROM tmp_new_pairs", dictionary=False)
            pairs = {(int(f), int(b)) for f, b in rows}
            has_tmp = True
        except Exception as e:
            logger.debug(f"Temporary table tmp_new_pairs unavailable ({e}); falling back to inline anti-join.")
            rows = self._raw_query(
                "SELECT cur_v.fnid, cur_v.blob_id FROM m_bridge_checkpoint cur_v "
                "LEFT JOIN m_bridge_checkpoint old_v "
                "       ON old_v.commit_id = %s AND old_v.fnid = cur_v.fnid AND old_v.blob_id = cur_v.blob_id "
                "WHERE cur_v.commit_id = %s AND old_v.blob_id IS NULL",
                (prev_commit_id, commit_id),
                dictionary=False,
            )
            pairs = {(int(f), int(b)) for f, b in rows}
            has_tmp = False

        yield MariaDBNewPairs(self, commit_id, prev_commit_id, has_tmp, pairs)

        if has_tmp:
            try:
                self._raw_execute("DROP TEMPORARY TABLE IF EXISTS tmp_new_pairs")
                if not self._in_txn:
                    self.commit()
            except Exception:
                pass


class MariaDBNewPairs(NewPairs):
    """MariaDB implementation of NewPairs."""

    def __init__(
        self,
        db: MariaDB,
        commit_id: int,
        prev_commit_id: int,
        has_tmp: bool,
        pairs: set[tuple[int, int]],
    ) -> None:
        self.db = db
        self.commit_id = commit_id
        self.prev_commit_id = prev_commit_id
        self.has_tmp = has_tmp
        self.pairs = pairs

    def delete_rows_of(self, table: Table | str) -> int:
        tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
        validate_ident(tbl_name)
        if self.has_tmp:
            sql = f"DELETE r FROM tmp_new_pairs p JOIN {tbl_name} r ON r.source_blob_id = p.blob_id AND r.source_fnid = p.fnid"
            return self.db._raw_execute(sql)
        sql = (
            f"DELETE r FROM m_bridge_checkpoint cur_v "
            f"LEFT JOIN m_bridge_checkpoint old_v "
            f"       ON old_v.commit_id = %s AND old_v.fnid = cur_v.fnid AND old_v.blob_id = cur_v.blob_id "
            f"JOIN {tbl_name} r ON r.source_blob_id = cur_v.blob_id AND r.source_fnid = cur_v.fnid "
            f"WHERE cur_v.commit_id = %s AND old_v.blob_id IS NULL"
        )
        return self.db._raw_execute(sql, (self.prev_commit_id, self.commit_id))

    def insert_include_references(
        self,
        kconfig_type_ids: Sequence[int] | str,
        kconfig_ref_type: int,
        include_ref_type: int,
    ) -> int:
        if isinstance(kconfig_type_ids, (tuple, list, set)):
            kconfig_types = ", ".join(str(int(x)) for x in kconfig_type_ids)
        else:
            kconfig_types = str(kconfig_type_ids)
        if self.has_tmp:
            sql = f"""
                INSERT IGNORE INTO m_file_reference (source_blob_id, source_fnid, target_fnid, ref_type, line_no, details)
                SELECT STRAIGHT_JOIN DISTINCT bt.blob_id, p.fnid, inc.fnid,
                       CASE WHEN a.type_id IN ({kconfig_types}) THEN %s ELSE %s END AS ref_type,
                       bt.line_s,
                       LEFT(a.name, 255)
                FROM tmp_new_pairs p
                JOIN m_bridge_tag bt ON p.blob_id = bt.blob_id
                JOIN m_tag t ON bt.tag_id = t.tag_id
                JOIN m_ast_include inc ON t.ast_id = inc.ast_id
                JOIN m_ast a ON inc.ast_id = a.ast_id;
            """
            return self.db._raw_execute(sql, (kconfig_ref_type, include_ref_type))
        sql = f"""
            INSERT IGNORE INTO m_file_reference (source_blob_id, source_fnid, target_fnid, ref_type, line_no, details)
            SELECT STRAIGHT_JOIN DISTINCT bt.blob_id, bf.fnid, inc.fnid,
                   CASE WHEN a.type_id IN ({kconfig_types}) THEN %s ELSE %s END AS ref_type,
                   bt.line_s,
                   LEFT(a.name, 255)
            FROM m_bridge_checkpoint bf
            LEFT JOIN m_bridge_checkpoint old_v
                   ON old_v.commit_id = %s AND old_v.fnid = bf.fnid AND old_v.blob_id = bf.blob_id
            JOIN m_bridge_tag bt ON bf.blob_id = bt.blob_id
            JOIN m_tag t ON bt.tag_id = t.tag_id
            JOIN m_ast_include inc ON t.ast_id = inc.ast_id
            JOIN m_ast a ON inc.ast_id = a.ast_id
            WHERE bf.commit_id = %s AND old_v.blob_id IS NULL;
        """
        return self.db._raw_execute(sql, (kconfig_ref_type, include_ref_type, self.prev_commit_id, self.commit_id))
