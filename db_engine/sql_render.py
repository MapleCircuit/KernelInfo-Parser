"""db_engine/sql_render.py - SQL Renderer for Declarative Spec Objects.

Translates Spec objects (Select, Where, Cond, Join, etc.) into parameterized
(sql, params) tuples across 'mariadb' and 'sqlite' dialects.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence

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

Dialect = Literal["mariadb", "sqlite"]

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def validate_ident(ident: str) -> str:
    """Validate SQL identifier against injection: [A-Za-z_][A-Za-z0-9_]* with optional dot prefixes."""
    clean = ident.strip()
    if clean == "*" or clean.endswith(".*"):
        prefix = clean[:-2]
        if prefix and not IDENT_RE.match(prefix):
            raise ValueError(f"Invalid identifier prefix: '{ident}'")
        return clean
    if not IDENT_RE.match(clean):
        raise ValueError(f"Invalid SQL identifier: '{ident}'")
    return clean


def resolve_source(source: Source) -> tuple[str, str, str | None]:
    """Resolve Source specification to (table_name, from_fragment, alias)."""
    alias: str | None = None
    if isinstance(source, tuple):
        src, alias_str = source
        alias = validate_ident(alias_str)
        tbl_name = src.table_name if hasattr(src, "table_name") else str(src)
        validate_ident(tbl_name)
        return tbl_name, f"{tbl_name} {alias}", alias
    tbl_name = source.table_name if hasattr(source, "table_name") else str(source)
    validate_ident(tbl_name)
    return tbl_name, tbl_name, None


def placeholder(dialect: Dialect) -> str:
    """Return bind parameter placeholder for the given dialect (%s or ?)."""
    return "?" if dialect == "sqlite" else "%s"


def render_where_term(
    term: Any,
    params: list[Any],
    dialect: Dialect = "mariadb",
) -> str:
    """Render a single Where term: Mapping, AnyOf, Exists, or InSelect."""
    ph = placeholder(dialect)
    if isinstance(term, AnyOf):
        if not term.branches:
            return "1 = 1"
        branch_sqls = []
        for branch in term.branches:
            b_sql = render_where(branch, params, dialect=dialect)
            if b_sql:
                branch_sqls.append(b_sql)
        if not branch_sqls:
            return "1 = 1"
        return f"({' OR '.join(f'({b})' for b in branch_sqls)})"

    if isinstance(term, Exists):
        src_name, src_from, _ = resolve_source(term.source)
        sub_params: list[Any] = []
        on_parts: list[str] = []
        for left_col, right_val in term.on.items():
            left = validate_ident(left_col)
            if isinstance(right_val, Value):
                on_parts.append(f"{left} = {ph}")
                sub_params.append(right_val.v)
            elif isinstance(right_val, Cond):
                on_parts.append(render_cond(left, right_val, sub_params, dialect))
            elif isinstance(right_val, str):
                right = validate_ident(right_val)
                on_parts.append(f"{left} = {right}")
            else:
                on_parts.append(f"{left} = {ph}")
                sub_params.append(right_val)

        join_parts: list[str] = []
        if term.joins:
            for j in term.joins:
                join_parts.append(render_join(j, sub_params, dialect=dialect))

        where_parts = list(on_parts)
        if term.where is not None:
            w_sql = render_where(term.where, sub_params, dialect=dialect)
            if w_sql:
                where_parts.append(w_sql)

        joins_str = (" " + " ".join(join_parts)) if join_parts else ""
        where_str = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
        neg = "NOT " if term.negate else ""
        params.extend(sub_params)
        return f"{neg}EXISTS (SELECT 1 FROM {src_from}{joins_str}{where_str})"

    if isinstance(term, InSelect):
        if isinstance(term.columns, (tuple, list)):
            cols_sql = f"({', '.join(validate_ident(c) for c in term.columns)})"
        else:
            cols_sql = validate_ident(term.columns)

        if isinstance(term.select, (tuple, list)):
            sel_sql = ", ".join(validate_ident(s) for s in term.select)
        else:
            sel_sql = validate_ident(term.select)

        _, src_from, _ = resolve_source(term.source)
        sub_params = []
        w_sql = render_where(term.where, sub_params, dialect=dialect) if term.where is not None else ""
        where_str = f" WHERE {w_sql}" if w_sql else ""
        neg = "NOT " if term.negate else ""
        params.extend(sub_params)
        return f"{cols_sql} {neg}IN (SELECT {sel_sql} FROM {src_from}{where_str})"

    if isinstance(term, Mapping):
        parts: list[str] = []
        for col_name, val in term.items():
            col = validate_ident(col_name)
            if val is None:
                raise TypeError(
                    f"None cannot be used as an equality filter for '{col_name}'; use is_null()"
                )
            if isinstance(val, Cond):
                parts.append(render_cond(col, val, params, dialect))
            elif isinstance(val, Col):
                parts.append(f"{col} = {validate_ident(val.name)}")
            elif isinstance(val, (list, tuple, set, frozenset)):
                val_list = list(val)
                if not val_list:
                    parts.append("1 = 0")
                else:
                    placeholders = ", ".join([ph] * len(val_list))
                    parts.append(f"{col} IN ({placeholders})")
                    params.extend(val_list)
            else:
                parts.append(f"{col} = {ph}")
                params.append(val)
        return " AND ".join(parts) if parts else ""

    raise TypeError(f"Unsupported Where term type: {type(term)}")


def render_cond(col: str, cond: Cond, params: list[Any], dialect: Dialect) -> str:
    """Render a Cond operator."""
    ph = placeholder(dialect)
    op = cond.op
    if op == "ne":
        params.append(cond.value)
        return f"{col} != {ph}"
    if op == "lt":
        params.append(cond.value)
        return f"{col} < {ph}"
    if op == "le":
        params.append(cond.value)
        return f"{col} <= {ph}"
    if op == "gt":
        params.append(cond.value)
        return f"{col} > {ph}"
    if op == "ge":
        params.append(cond.value)
        return f"{col} >= {ph}"
    if op == "between":
        params.extend([cond.value, cond.value2])
        return f"{col} BETWEEN {ph} AND {ph}"
    if op == "not_in":
        v_list = list(cond.value)
        if not v_list:
            return "1 = 1"
        placeholders = ", ".join([ph] * len(v_list))
        params.extend(v_list)
        return f"{col} NOT IN ({placeholders})"
    if op == "is_null":
        return f"{col} IS NULL"
    if op == "not_null":
        return f"{col} IS NOT NULL"
    if op == "ieq":
        params.append(cond.value)
        return f"LOWER({col}) = LOWER({ph})"
    if op == "like":
        params.append(cond.value)
        ci = bool(cond.value2)
        esc_sql = "ESCAPE '\\\\'" if dialect == "mariadb" else "ESCAPE '\\'"
        if ci:
            return f"LOWER({col}) LIKE LOWER({ph}) {esc_sql}"
        return f"{col} LIKE {ph} {esc_sql}"
    if op == "hash_prefix":
        lo, hi = cond.value, cond.value2
        if hi is not None:
            params.extend([lo, hi])
            return f"({col} >= {ph} AND {col} < {ph})"
        params.append(lo)
        return f"{col} >= {ph}"
    if op == "len_lt":
        params.append(cond.value)
        fn = "LENGTH" if dialect == "sqlite" else "CHAR_LENGTH"
        return f"{fn}({col}) < {ph}"
    raise ValueError(f"Unknown Cond operator '{op}'")


def render_where(
    where: Where | None,
    params: list[Any],
    dialect: Dialect = "mariadb",
) -> str:
    """Render a Where mapping or sequence of Where terms."""
    if where is None:
        return ""
    if isinstance(where, (Mapping, AnyOf, Exists, InSelect)):
        return render_where_term(where, params, dialect=dialect)
    if isinstance(where, Sequence):
        clauses = []
        for term in where:
            term_sql = render_where_term(term, params, dialect=dialect)
            if term_sql:
                clauses.append(term_sql)
        return " AND ".join(clauses)
    raise TypeError(f"Invalid where type: {type(where)}")


def render_join(join: Join, params: list[Any], dialect: Dialect = "mariadb") -> str:
    """Render a single Join clause."""
    ph = placeholder(dialect)
    _, src_from, _ = resolve_source(join.source)
    kind_sql = "LEFT JOIN" if join.kind == "left" else "JOIN"
    on_parts = []
    for left_col, right_val in join.on.items():
        left = validate_ident(left_col)
        if isinstance(right_val, Value):
            on_parts.append(f"{left} = {ph}")
            params.append(right_val.v)
        elif isinstance(right_val, Cond):
            on_parts.append(render_cond(left, right_val, params, dialect))
        elif isinstance(right_val, str):
            right = validate_ident(right_val)
            on_parts.append(f"{left} = {right}")
        else:
            on_parts.append(f"{left} = {ph}")
            params.append(right_val)
    on_sql = f" ON {' AND '.join(on_parts)}" if on_parts else ""
    return f"{kind_sql} {src_from}{on_sql}"


def render_columns(
    columns: Sequence[str | Agg | Const] | Mapping[str, str | Agg | Const],
    params: list[Any],
    dialect: Dialect = "mariadb",
) -> tuple[str, list[str]]:
    """Render columns projection, returning (columns_sql, list_of_column_names_or_aliases)."""
    ph = placeholder(dialect)
    col_sqls = []
    col_names = []

    if isinstance(columns, Mapping):
        for alias_name, item in columns.items():
            alias = validate_ident(alias_name)
            col_names.append(alias)
            if isinstance(item, str):
                col_sqls.append(f"{validate_ident(item)} AS {alias}")
            elif isinstance(item, Const):
                col_sqls.append(f"{ph} AS {alias}")
                params.append(item.value)
            elif isinstance(item, Agg):
                dist = "DISTINCT " if item.distinct else ""
                col_val = "*" if item.column == "*" else validate_ident(item.column)
                col_sqls.append(f"{item.func}({dist}{col_val}) AS {alias}")
            else:
                raise TypeError(f"Unsupported column projection type in mapping: {type(item)}")
    elif isinstance(columns, Sequence):
        for idx, item in enumerate(columns):
            if isinstance(item, str):
                clean_col = validate_ident(item)
                col_sqls.append(clean_col)
                # Base column name without prefix
                base_name = clean_col.rsplit(".", 1)[-1]
                col_names.append(base_name)
            elif isinstance(item, Const):
                col_sqls.append(f"{ph}")
                params.append(item.value)
                col_names.append(f"const_{idx}")
            elif isinstance(item, Agg):
                dist = "DISTINCT " if item.distinct else ""
                col_val = "*" if item.column == "*" else validate_ident(item.column)
                col_sqls.append(f"{item.func}({dist}{col_val})")
                col_names.append(f"{item.func.lower()}_{idx}")
            else:
                raise TypeError(f"Unsupported column projection type in sequence: {type(item)}")
    else:
        raise TypeError(f"Columns must be a Sequence or Mapping, got {type(columns)}")

    return ", ".join(col_sqls), col_names


def render_order_by(order_by: Sequence[str]) -> str:
    """Render ORDER BY clause items."""
    if not order_by:
        return ""
    order_parts = []
    for item in order_by:
        clean = item.strip()
        if clean.startswith("-"):
            col = validate_ident(clean[1:])
            order_parts.append(f"{col} DESC")
        else:
            col = validate_ident(clean)
            order_parts.append(f"{col} ASC")
    return f" ORDER BY {', '.join(order_parts)}"


def render_select(
    select: Select,
    dialect: Dialect = "mariadb",
) -> tuple[str, list[Any], list[str]]:
    """Render Select spec into (sql, params, col_names)."""
    params: list[Any] = []
    _, src_from, _ = resolve_source(select.source)

    cols_sql, col_names = render_columns(select.columns, params, dialect=dialect)
    distinct_sql = "DISTINCT " if select.distinct else ""

    join_sqls = []
    if select.joins:
        for j in select.joins:
            join_sqls.append(render_join(j, params, dialect=dialect))
    joins_str = (" " + " ".join(join_sqls)) if join_sqls else ""

    w_sql = render_where(select.where, params, dialect=dialect)
    where_str = f" WHERE {w_sql}" if w_sql else ""

    order_str = render_order_by(select.order_by)

    limit_str = ""
    if select.limit is not None:
        limit_str = f" LIMIT {int(select.limit)}"
        if select.offset is not None:
            limit_str += f" OFFSET {int(select.offset)}"

    sql = f"SELECT {distinct_sql}{cols_sql} FROM {src_from}{joins_str}{where_str}{order_str}{limit_str}"
    return sql, params, col_names


def render_insert(
    table: Table | str,
    columns: Sequence[str],
    n_rows: int,
    on_conflict: Literal["error", "ignore", "update", "replace"] = "error",
    update_columns: Sequence[str] | None = None,
    dialect: Dialect = "mariadb",
) -> str:
    """Render INSERT statement template with n_rows value groups."""
    tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
    validate_ident(tbl_name)
    col_names = [validate_ident(c) for c in columns]
    cols_sql = ", ".join(col_names)
    ph = placeholder(dialect)
    row_ph = f"({', '.join([ph] * len(col_names))})"
    values_sql = ", ".join([row_ph] * n_rows)

    if dialect == "mariadb":
        if on_conflict == "error":
            prefix = "INSERT INTO"
            suffix = ""
        elif on_conflict == "ignore":
            prefix = "INSERT IGNORE INTO"
            suffix = ""
        elif on_conflict == "replace":
            prefix = "REPLACE INTO"
            suffix = ""
        elif on_conflict == "update":
            prefix = "INSERT INTO"
            up_cols = update_columns if update_columns is not None else col_names
            up_parts = [f"{validate_ident(c)} = VALUES({validate_ident(c)})" for c in up_cols]
            suffix = f" ON DUPLICATE KEY UPDATE {', '.join(up_parts)}"
        else:
            raise ValueError(f"Unknown on_conflict: {on_conflict}")
    else:  # sqlite
        if on_conflict == "error":
            prefix = "INSERT INTO"
            suffix = ""
        elif on_conflict == "ignore":
            prefix = "INSERT OR IGNORE INTO"
            suffix = ""
        elif on_conflict == "replace":
            prefix = "INSERT OR REPLACE INTO"
            suffix = ""
        elif on_conflict == "update":
            prefix = "INSERT INTO"
            up_cols = update_columns if update_columns is not None else col_names
            up_parts = [f"{validate_ident(c)} = excluded.{validate_ident(c)}" for c in up_cols]
            suffix = f" ON CONFLICT DO UPDATE SET {', '.join(up_parts)}"
        else:
            raise ValueError(f"Unknown on_conflict: {on_conflict}")

    return f"{prefix} {tbl_name} ({cols_sql}) VALUES {values_sql}{suffix}"


def render_update(
    table: Table | str,
    values: Mapping[str, Any],
    where: Where | None,
    limit: int | None = None,
    dialect: Dialect = "mariadb",
) -> tuple[str, list[Any]]:
    """Render UPDATE statement: {col: value | Inc(n) | Col("other")}."""
    tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
    validate_ident(tbl_name)
    ph = placeholder(dialect)
    params: list[Any] = []

    set_parts = []
    for col_name, val in values.items():
        col = validate_ident(col_name)
        if isinstance(val, Inc):
            set_parts.append(f"{col} = {col} + {ph}")
            params.append(val.n)
        elif isinstance(val, Col):
            set_parts.append(f"{col} = {validate_ident(val.name)}")
        else:
            set_parts.append(f"{col} = {ph}")
            params.append(val)

    if not set_parts:
        raise ValueError("update_rows requires at least one value to set")

    w_sql = render_where(where, params, dialect=dialect)
    where_str = f" WHERE {w_sql}" if w_sql else ""
    limit_str = f" LIMIT {int(limit)}" if limit is not None else ""

    sql = f"UPDATE {tbl_name} SET {', '.join(set_parts)}{where_str}{limit_str}"
    return sql, params


def render_delete(
    table: Table | str,
    where: Where | None,
    limit: int | None = None,
    all_rows: bool = False,
    dialect: Dialect = "mariadb",
) -> tuple[str, list[Any]]:
    """Render DELETE statement."""
    tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
    validate_ident(tbl_name)
    if where is None and not all_rows:
        raise ValueError("delete_rows without where requires all_rows=True")

    params: list[Any] = []
    w_sql = render_where(where, params, dialect=dialect)
    where_str = f" WHERE {w_sql}" if w_sql else ""
    limit_str = f" LIMIT {int(limit)}" if limit is not None else ""

    sql = f"DELETE FROM {tbl_name}{where_str}{limit_str}"
    return sql, params


def render_copy(
    table: Table | str,
    columns: Sequence[str],
    select: Select,
    on_conflict: Literal["error", "ignore"] = "error",
    dialect: Dialect = "mariadb",
) -> tuple[str, list[Any]]:
    """Render INSERT [IGNORE] INTO ... SELECT ... statement."""
    tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
    validate_ident(tbl_name)
    cols_sql = ", ".join(validate_ident(c) for c in columns)

    if dialect == "mariadb":
        prefix = "INSERT IGNORE INTO" if on_conflict == "ignore" else "INSERT INTO"
    else:
        prefix = "INSERT OR IGNORE INTO" if on_conflict == "ignore" else "INSERT INTO"

    sel_sql, params, _ = render_select(select, dialect=dialect)
    sql = f"{prefix} {tbl_name} ({cols_sql}) {sel_sql}"
    return sql, params
