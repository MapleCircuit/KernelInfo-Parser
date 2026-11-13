"""db_engine/sql_render.py - SQL Renderer for Declarative Spec Objects.

Translates Spec objects (Select, Where, Cond, Join, etc.) into parameterized
(sql, params) tuples across 'mariadb' and 'sqlite' dialects.
"""
from __future__ import annotations

import contextlib
import re
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence

from db_engine.errors import SpecError
from db_engine.spec import (
    NO_DEFAULT,
    CURRENT_TIMESTAMP,
    Agg,
    AnyOf,
    Arith,
    Case,
    Coalesce,
    Col,
    ColumnSpec,
    ColumnType,
    Concat,
    Cond,
    Const,
    Exists,
    ExistsQuery,
    Expr,
    Hex,
    InQuery,
    Inc,
    InSelect,
    Incoming,
    Join,
    Length,
    NullIf,
    Order,
    Pred,
    Select,
    Source,
    Substr,
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


_SCOPE = threading.local()


@contextlib.contextmanager
def _incoming_scope(active: bool) -> Iterator[None]:
    """Mark conditional-upsert rendering so `Incoming()` stays legal inside nested `Case` conditions."""
    previous = getattr(_SCOPE, "incoming", False)
    _SCOPE.incoming = previous or active
    try:
        yield
    finally:
        _SCOPE.incoming = previous


def _is_operand_expr(x: Any) -> bool:
    return isinstance(x, (Expr, Agg))


def render_operand(
    x: Any,
    params: list[Any],
    dialect: Dialect = "mariadb",
    *,
    incoming: bool = False,
) -> str:
    """Render an expression operand: column name, `Value`/`Const`/scalar literal, `Expr` or `Agg`.

    `incoming=True` renders `Incoming(col)` (only valid inside conditional-upsert `update_exprs`).
    """
    ph = placeholder(dialect)
    if isinstance(x, str):
        return validate_ident(x)
    if isinstance(x, (Value, Const)):
        params.append(x.v if isinstance(x, Value) else x.value)
        return ph
    if isinstance(x, Col):
        return validate_ident(x.name)
    if isinstance(x, Agg):
        dist = "DISTINCT " if x.distinct else ""
        col_val = "*" if x.column == "*" else validate_ident(x.column)
        return f"{x.func}({dist}{col_val})"
    if isinstance(x, Expr):
        return render_expr(x, params, dialect, incoming=incoming)
    params.append(x)
    return ph


def render_expr(
    expr: Expr,
    params: list[Any],
    dialect: Dialect = "mariadb",
    *,
    incoming: bool = False,
) -> str:
    """Render a scalar expression node to SQL (parameters appended to `params` in textual order)."""

    def op(x: Any) -> str:
        return render_operand(x, params, dialect, incoming=incoming)

    if isinstance(expr, Concat):
        if not expr.parts:
            raise ValueError("Concat requires at least one part")
        if dialect == "sqlite":
            return "(" + " || ".join(op(p) for p in expr.parts) + ")"
        return "CONCAT(" + ", ".join(op(p) for p in expr.parts) + ")"
    if isinstance(expr, Length):
        fn = "LENGTH" if dialect == "sqlite" else "CHAR_LENGTH"
        return f"{fn}({op(expr.operand)})"
    if isinstance(expr, Substr):
        args = [op(expr.operand), op(expr.start)]
        if expr.length is not None:
            args.append(op(expr.length))
        return f"SUBSTR({', '.join(args)})"
    if isinstance(expr, Coalesce):
        if not expr.parts:
            raise ValueError("Coalesce requires at least one part")
        return "COALESCE(" + ", ".join(op(p) for p in expr.parts) + ")"
    if isinstance(expr, NullIf):
        return f"NULLIF({op(expr.left)}, {op(expr.right)})"
    if isinstance(expr, Arith):
        if expr.op not in ("+", "-", "*"):
            raise ValueError(f"Unsupported arithmetic operator '{expr.op}'")
        return f"({op(expr.left)} {expr.op} {op(expr.right)})"
    if isinstance(expr, Hex):
        return f"HEX({op(expr.operand)})"
    if isinstance(expr, Case):
        if not expr.whens:
            raise ValueError("Case requires at least one WHEN branch")
        parts = ["CASE"]
        for cond, value in expr.whens:
            with _incoming_scope(incoming):
                cond_sql = render_where(cond, params, dialect=dialect)
            parts.append(f"WHEN {cond_sql or '1 = 1'} THEN {op(value)}")
        if expr.otherwise is not None:
            parts.append(f"ELSE {op(expr.otherwise)}")
        parts.append("END")
        return " ".join(parts)
    if isinstance(expr, Incoming):
        if not (incoming or getattr(_SCOPE, "incoming", False)):
            raise ValueError("Incoming() is only valid in update_exprs of insert_rows(on_conflict='update')")
        col = validate_ident(expr.column)
        return f"VALUES({col})" if dialect == "mariadb" else f"excluded.{col}"
    raise TypeError(f"Unsupported expression type: {type(expr)}")


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
            elif isinstance(right_val, Expr):
                on_parts.append(f"{left} = {render_expr(right_val, sub_params, dialect)}")
            elif isinstance(right_val, str):
                right = validate_ident(right_val)
                on_parts.append(f"{left} = {right}")
            elif right_val is None:
                on_parts.append(f"{left} IS NULL")
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

    if isinstance(term, Pred):
        expr_sql = render_operand(term.expr, params, dialect)
        if isinstance(term.test, Cond):
            return render_cond(expr_sql, term.test, params, dialect)
        if _is_operand_expr(term.test) or isinstance(term.test, (Col, Value)):
            return f"{expr_sql} = {render_operand(term.test, params, dialect)}"
        if term.test is None:
            return f"{expr_sql} IS NULL"
        params.append(term.test)
        return f"{expr_sql} = {ph}"

    if isinstance(term, InQuery):
        cols = term.columns
        cols_sql = f"({', '.join(validate_ident(c) for c in cols)})" if isinstance(cols, tuple) else validate_ident(cols)
        sel_sql, sel_params, _ = render_select(term.select, dialect=dialect, check=False)
        params.extend(sel_params)
        neg = "NOT " if term.negate else ""
        return f"{cols_sql} {neg}IN ({sel_sql})"

    if isinstance(term, ExistsQuery):
        sel_sql, sel_params, _ = render_select(term.select, dialect=dialect, check=False)
        params.extend(sel_params)
        neg = "NOT " if term.negate else ""
        return f"{neg}EXISTS ({sel_sql})"

    if isinstance(term, Mapping):
        parts: list[str] = []
        for col_name, val in term.items():
            col = validate_ident(col_name)
            parts.extend(_render_column_filter(col, val, params, dialect))
        return " AND ".join(parts) if parts else ""

    raise TypeError(f"Unsupported Where term type: {type(term)}")


_DOC_WHERE = "see DB_API.md 'Declarative Query Specification Primitives'"


def _render_column_filter(col: str, val: Any, params: list[Any], dialect: Dialect) -> list[str]:
    """SQL conditions (ANDed by the caller) of one `{column: value}` entry of a Where mapping."""
    ph = placeholder(dialect)
    if val is None:
        return [f"{col} IS NULL"]
    if isinstance(val, Expr):
        return [f"{col} = {render_expr(val, params, dialect)}"]
    if isinstance(val, Cond):
        return [render_cond(col, val, params, dialect)]
    if isinstance(val, Col):
        return [f"{col} = {validate_ident(val.name)}"]
    if isinstance(val, Mapping):
        raise SpecError(f"Where value for '{col}' is a mapping; use a scalar, a list (IN), a Cond or [Cond, ...] - {_DOC_WHERE}")
    if isinstance(val, (list, tuple, set, frozenset)):
        items = list(val)
        if items and any(isinstance(x, Cond) for x in items):
            if not all(isinstance(x, Cond) for x in items):
                raise SpecError(
                    f"Where value for '{col}' mixes conditions and plain values; a list is either IN values or "
                    f"[Cond, ...] (all ANDed) - {_DOC_WHERE}"
                )
            return [render_cond(col, c, params, dialect) for c in items]
        if not items:
            return ["1 = 0"]  # empty IN matches nothing (documented SQL semantics)
        params.extend(items)
        return [f"{col} IN ({', '.join([ph] * len(items))})"]
    params.append(val)
    return [f"{col} = {ph}"]


_NULL_OPS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "between", "ieq"})


def render_cond(col: str, cond: Cond, params: list[Any], dialect: Dialect) -> str:
    """Render a Cond operator."""
    ph = placeholder(dialect)
    op = cond.op
    if cond.value is None and op in _NULL_OPS:
        if op == "eq":
            return f"{col} IS NULL"
        if op == "ne":
            return f"{col} IS NOT NULL"
        raise SpecError(
            f"{op}(None) compares with NULL and never matches; use is_null() / not_null() (or eq(None) / ne(None)) - {_DOC_WHERE}"
        )
    if op == "eq":
        params.append(cond.value)
        return f"{col} = {ph}"
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
        if cond.value2 is None:
            raise SpecError(f"between(lo, None) compares with NULL and never matches; use ge(lo) - {_DOC_WHERE}")
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
    if isinstance(where, (Mapping, AnyOf, Exists, InSelect, Pred, InQuery, ExistsQuery)):
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
        elif isinstance(right_val, Expr):
            on_parts.append(f"{left} = {render_expr(right_val, params, dialect)}")
        elif isinstance(right_val, str):
            right = validate_ident(right_val)
            on_parts.append(f"{left} = {right}")
        elif right_val is None:
            on_parts.append(f"{left} IS NULL")
        else:
            on_parts.append(f"{left} = {ph}")
            params.append(right_val)
    on_sql = f" ON {' AND '.join(on_parts)}" if on_parts else ""
    return f"{kind_sql} {src_from}{on_sql}"


def _render_column_item(item: Any, params: list[Any], dialect: Dialect) -> str:
    """Render one projection item (column name, Agg, Const or Expr) without alias."""
    if isinstance(item, Const):
        params.append(item.value)
        return placeholder(dialect)
    if isinstance(item, str):
        return validate_ident(item)
    if isinstance(item, (Agg, Expr)):
        return render_operand(item, params, dialect)
    raise TypeError(f"Unsupported column projection type: {type(item)}")


def render_columns(
    columns: Sequence[Any] | Mapping[str, Any],
    params: list[Any],
    dialect: Dialect = "mariadb",
) -> tuple[str, list[str]]:
    """Render columns projection, returning (columns_sql, list_of_column_names_or_aliases)."""
    col_sqls = []
    col_names = []

    if isinstance(columns, Mapping):
        for alias_name, item in columns.items():
            alias = validate_ident(alias_name)
            col_names.append(alias)
            col_sqls.append(f"{_render_column_item(item, params, dialect)} AS {alias}")
    elif isinstance(columns, Sequence):
        for idx, item in enumerate(columns):
            col_sqls.append(_render_column_item(item, params, dialect))
            if isinstance(item, str):
                col_names.append(validate_ident(item).rsplit(".", 1)[-1])
            elif isinstance(item, Const):
                col_names.append(f"const_{idx}")
            elif isinstance(item, Agg):
                col_names.append(f"{item.func.lower()}_{idx}")
            else:
                col_names.append(f"expr_{idx}")
    else:
        raise TypeError(f"Columns must be a Sequence or Mapping, got {type(columns)}")

    return ", ".join(col_sqls), col_names


def render_order_by(
    order_by: Sequence[Any],
    params: list[Any] | None = None,
    dialect: Dialect = "mariadb",
    *,
    keyword: str = "ORDER BY",
) -> str:
    """Render ORDER BY items: `"col"`, `"-col"` (descending), an `Expr`/`Agg`, or `Order(expr, desc)`."""
    if not order_by:
        return ""
    if params is None:
        params = []
    order_parts = []
    for item in order_by:
        if isinstance(item, Order):
            order_parts.append(f"{render_operand(item.expr, params, dialect)} {'DESC' if item.desc else 'ASC'}")
        elif isinstance(item, str):
            clean = item.strip()
            if clean.startswith("-"):
                order_parts.append(f"{validate_ident(clean[1:])} DESC")
            else:
                order_parts.append(f"{validate_ident(clean)} ASC")
        else:
            order_parts.append(f"{render_operand(item, params, dialect)} ASC")
    return f" {keyword} {', '.join(order_parts)}"


def _render_group_by(group_by: Sequence[Any], params: list[Any], dialect: Dialect) -> str:
    if not group_by:
        return ""
    parts = [
        render_operand(g.expr if isinstance(g, Order) else g, params, dialect) for g in group_by
    ]
    return f" GROUP BY {', '.join(parts)}"


def render_select(
    select: Select,
    dialect: Dialect = "mariadb",
    *,
    check: bool = True,
) -> tuple[str, list[Any], list[str]]:
    """Render Select spec into (sql, params, col_names). Parameters follow the SQL text order.

    `check=True` validates the `alias.column` references first (`check_select_aliases`); subqueries are covered by
    the check of their outermost select.
    """
    if check:
        check_select_aliases(select)
    params: list[Any] = []
    _, src_from, _ = resolve_source(select.source)
    if select.hint not in (None, "straight_join"):
        raise ValueError(f"Unknown select hint: {select.hint!r}")

    cols_sql, col_names = render_columns(select.columns, params, dialect=dialect)
    hint_sql = "STRAIGHT_JOIN " if (select.hint == "straight_join" and dialect == "mariadb") else ""
    distinct_sql = "DISTINCT " if select.distinct else ""

    join_sqls = []
    if select.joins:
        for j in select.joins:
            join_sqls.append(render_join(j, params, dialect=dialect))
    joins_str = (" " + " ".join(join_sqls)) if join_sqls else ""

    w_sql = render_where(select.where, params, dialect=dialect)
    where_str = f" WHERE {w_sql}" if w_sql else ""

    group_str = _render_group_by(select.group_by, params, dialect)
    having_sql = render_where(select.having, params, dialect=dialect) if select.having is not None else ""
    having_str = f" HAVING {having_sql}" if having_sql else ""

    order_str = render_order_by(select.order_by, params, dialect)

    limit_str = ""
    if select.limit is not None:
        limit_str = f" LIMIT {int(select.limit)}"
        if select.offset is not None:
            limit_str += f" OFFSET {int(select.offset)}"
    elif select.offset is not None:
        # MariaDB and SQLite both require LIMIT before OFFSET
        limit_str = f" LIMIT 18446744073709551615 OFFSET {int(select.offset)}" if dialect == "mariadb" else (
            f" LIMIT -1 OFFSET {int(select.offset)}"
        )

    sql = (
        f"SELECT {hint_sql}{distinct_sql}{cols_sql} FROM {src_from}{joins_str}{where_str}"
        f"{group_str}{having_str}{order_str}{limit_str}"
    )
    return sql, params, col_names


# -----------------------------------------------------------------------------
# Column reads and assignment order (stored-row semantics on MariaDB)
# -----------------------------------------------------------------------------
def expr_columns(x: Any, out: set[str] | None = None) -> set[str]:
    """Names of the columns an expression operand reads (`Incoming` and literals read none; subqueries are not entered)."""
    out = set() if out is None else out
    if isinstance(x, str):
        out.add(x)
    elif isinstance(x, Col):
        out.add(x.name)
    elif isinstance(x, Agg):
        if x.column != "*":
            out.add(x.column)
    elif isinstance(x, (Value, Const)):
        pass
    elif isinstance(x, (Concat, Coalesce)):
        for part in x.parts:
            expr_columns(part, out)
    elif isinstance(x, (Length, Hex)):
        expr_columns(x.operand, out)
    elif isinstance(x, Substr):
        for part in (x.operand, x.start, x.length):
            expr_columns(part, out)
    elif isinstance(x, (NullIf, Arith)):
        expr_columns(x.left, out)
        expr_columns(x.right, out)
    elif isinstance(x, Case):
        for cond, value in x.whens:
            where_columns(cond, out)
            expr_columns(value, out)
        expr_columns(x.otherwise, out)
    return out


def where_columns(where: Any, out: set[str] | None = None) -> set[str]:
    """Names of the columns a Where reads (subquery terms contribute their outer-side columns only)."""
    out = set() if out is None else out
    if where is None:
        return out
    if isinstance(where, Mapping):
        for key, val in where.items():
            out.add(key)
            if isinstance(val, (Expr, Col)):
                expr_columns(val, out)
    elif isinstance(where, AnyOf):
        for branch in where.branches:
            where_columns(branch, out)
    elif isinstance(where, Pred):
        expr_columns(where.expr, out)
        if isinstance(where.test, (Expr, Agg, Col)):
            expr_columns(where.test, out)
    elif isinstance(where, (InSelect, InQuery)):
        cols = where.columns
        out.update([cols] if isinstance(cols, str) else cols)
    elif isinstance(where, (Exists, ExistsQuery)):
        pass
    elif isinstance(where, Sequence) and not isinstance(where, str):
        for term in where:
            where_columns(term, out)
    return out


def order_assignments(assignments: Sequence[tuple[str, set[str]]], what: str) -> list[int]:
    """Indexes of `assignments` (`(column, columns read)`) in an order where every expression sees the STORED row.

    MariaDB evaluates `SET` / `ON DUPLICATE KEY UPDATE` assignments left to right on the already-updated row, so an
    assignment that reads column B must come before the assignment that writes B (reading its own column is fine).
    The written order is kept wherever it already satisfies that; a cycle raises `SpecError`.
    """
    n = len(assignments)
    cols = [c for c, _ in assignments]
    # before[j] = assignments that must be placed before j (they read column j)
    before: list[set[int]] = [set() for _ in range(n)]
    for i, (_, reads) in enumerate(assignments):
        for j in range(n):
            if j != i and cols[j] in reads:
                before[j].add(i)
    placed: list[int] = []
    left = list(range(n))
    while left:
        pick = next((j for j in left if all(i in placed for i in before[j])), None)
        if pick is None:
            names = ", ".join(sorted(cols[j] for j in left))
            raise SpecError(
                f"{what}: assignments to {names} read each other's stored values, so no order gives every expression "
                f"the stored row on MariaDB; split it into two statements - see DB_API.md 'Spec Write Primitives'"
            )
        placed.append(pick)
        left.remove(pick)
    return placed


# -----------------------------------------------------------------------------
# Alias validation
# -----------------------------------------------------------------------------
def _source_names(*sources: Any) -> set[str]:
    names: set[str] = set()
    for src in sources:
        tbl, _, alias = resolve_source(src)
        names.add(alias or tbl)
        if not alias and "." in tbl:
            names.add(tbl.rsplit(".", 1)[-1])
    return names


class _AliasCheck:
    """Collects `alias.column` references whose alias is not a source / join name of the enclosing selects."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def ref(self, name: Any, scope: frozenset[str], where: str) -> None:
        if isinstance(name, str) and "." in name:
            qual = name.rsplit(".", 1)[0]
            if qual not in scope:
                self.problems.append(f"'{name}' in {where}")

    def refs(self, names: Any, scope: frozenset[str], where: str) -> None:
        for n in (names or ()):
            self.ref(n, scope, where)

    def operand(self, x: Any, scope: frozenset[str], where: str) -> None:
        self.refs(expr_columns(x), scope, where)

    def where_clause(self, where: Any, scope: frozenset[str], label: str) -> None:
        if where is None:
            return
        if isinstance(where, (Exists, ExistsQuery, InSelect, InQuery)):
            self.subquery(where, scope, label)
        elif isinstance(where, AnyOf):
            for branch in where.branches:
                self.where_clause(branch, scope, label)
        elif isinstance(where, Pred):
            self.operand(where.expr, scope, label)
            if isinstance(where.test, (Expr, Agg, Col)):
                self.operand(where.test, scope, label)
        elif isinstance(where, Mapping):
            for key, val in where.items():
                self.ref(key, scope, label)
                if isinstance(val, (Expr, Col)):
                    self.operand(val, scope, label)
        elif isinstance(where, Sequence) and not isinstance(where, str):
            for term in where:
                self.where_clause(term, scope, label)

    def on_clause(self, on: Mapping[str, Any], scope: frozenset[str], label: str) -> None:
        for left, right in on.items():
            self.ref(left, scope, label)
            if isinstance(right, (str, Col, Expr)):
                self.operand(right, scope, label)

    def subquery(self, term: Any, scope: frozenset[str], label: str) -> None:
        if isinstance(term, Exists):
            inner = scope | _source_names(term.source, *[j.source for j in term.joins])
            self.on_clause(term.on, inner, label)
            self.where_clause(term.where, inner, label)
            for j in term.joins:
                self.on_clause(j.on, inner, label)
        elif isinstance(term, InSelect):
            self.refs([term.columns] if isinstance(term.columns, str) else term.columns, scope, label)
            inner = scope | _source_names(term.source)
            self.refs([term.select] if isinstance(term.select, str) else term.select, inner, label)
            self.where_clause(term.where, inner, label)
        elif isinstance(term, InQuery):
            self.refs([term.columns] if isinstance(term.columns, str) else term.columns, scope, label)
            self.select(term.select, scope)
        elif isinstance(term, ExistsQuery):
            self.select(term.select, scope)

    def select(self, sel: Select, outer: frozenset[str] = frozenset()) -> None:
        scope = outer | _source_names(sel.source, *[j.source for j in sel.joins])
        cols = sel.columns.values() if isinstance(sel.columns, Mapping) else sel.columns
        for item in cols:
            if not isinstance(item, Const):
                self.operand(item, scope, "columns")
        for j in sel.joins:
            self.on_clause(j.on, scope, "join on")
        self.where_clause(sel.where, scope, "where")
        for item in sel.group_by:
            self.operand(item.expr if isinstance(item, Order) else item, scope, "group_by")
        self.where_clause(sel.having, scope, "having")
        for item in sel.order_by:
            if isinstance(item, Order):
                self.operand(item.expr, scope, "order_by")
            elif isinstance(item, str):
                self.ref(item.strip().lstrip("-"), scope, "order_by")
            else:
                self.operand(item, scope, "order_by")


def check_select_aliases(select: Select) -> None:
    """Raise `SpecError` when a `alias.column` reference names an alias that is not a source or join of the select.

    Correlated subqueries (`exists_select`, `in_select`, `Exists`, `finished_ref`) may use the aliases of every
    enclosing select. The message lists the known aliases.
    """
    chk = _AliasCheck()
    chk.select(select)
    if chk.problems:
        known = sorted(_source_names(select.source, *[j.source for j in select.joins]))
        raise SpecError(
            f"Unknown alias in {'; '.join(chk.problems[:4])}; known aliases of this select: {', '.join(known)} "
            f"(a subquery alias such as finished_ref('r') must equal the outer alias) - "
            f"see DB_API.md 'Declarative Query Specification Primitives'"
        )


def render_insert_with_params(
    table: Table | str,
    columns: Sequence[str],
    n_rows: int,
    on_conflict: Literal["error", "ignore", "update", "replace"] = "error",
    update_columns: Sequence[str] | None = None,
    dialect: Dialect = "mariadb",
    update_exprs: Mapping[str, Any] | None = None,
) -> tuple[str, list[Any]]:
    """Render an INSERT template with `n_rows` value groups; returns `(sql, trailing_params)`.

    `trailing_params` are the bound values of `update_exprs` literals; they follow the row values in the final
    parameter list. `update_exprs` maps a column to an expression (`Incoming("col")` = the offered value, a bare
    column name = the existing value) and is only valid with `on_conflict="update"`; columns in `update_columns`
    and not in `update_exprs` are assigned their incoming value.
    """
    tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
    validate_ident(tbl_name)
    col_names = [validate_ident(c) for c in columns]
    cols_sql = ", ".join(col_names)
    ph = placeholder(dialect)
    row_ph = f"({', '.join([ph] * len(col_names))})"
    values_sql = ", ".join([row_ph] * n_rows)
    trailing: list[Any] = []

    if update_exprs and on_conflict != "update":
        raise ValueError("update_exprs requires on_conflict='update'")

    def _update_parts() -> list[str]:
        up_cols = list(update_columns) if update_columns is not None else (
            [] if update_exprs else list(col_names)
        )
        exprs = dict(update_exprs or {})
        # (column, columns read, plain incoming assignment?, expression)
        items: list[tuple[str, set[str], bool, Any]] = []
        for c in up_cols:
            if c not in exprs:
                items.append((validate_ident(c), set(), True, None))
        for c, e in exprs.items():
            items.append((validate_ident(c), expr_columns(e), False, e))
        if not items:
            raise ValueError("on_conflict='update' has nothing to update")
        order = order_assignments([(c, reads) for c, reads, _, _ in items], "insert_rows(update_exprs=...)")
        if dialect != "mariadb":
            order = list(range(len(items)))  # other dialects read the stored row whatever the order
        parts: list[str] = []
        for idx in order:
            col, _, plain, e = items[idx]
            if plain:
                parts.append(f"{col} = VALUES({col})" if dialect == "mariadb" else f"{col} = excluded.{col}")
            else:
                parts.append(f"{col} = {render_operand(e, trailing, dialect, incoming=True)}")
        return parts

    if dialect == "mariadb":
        if on_conflict == "error":
            prefix, suffix = "INSERT INTO", ""
        elif on_conflict == "ignore":
            prefix, suffix = "INSERT IGNORE INTO", ""
        elif on_conflict == "replace":
            prefix, suffix = "REPLACE INTO", ""
        elif on_conflict == "update":
            prefix = "INSERT INTO"
            suffix = f" ON DUPLICATE KEY UPDATE {', '.join(_update_parts())}"
        else:
            raise ValueError(f"Unknown on_conflict: {on_conflict}")
    else:  # sqlite
        if on_conflict == "error":
            prefix, suffix = "INSERT INTO", ""
        elif on_conflict == "ignore":
            prefix, suffix = "INSERT OR IGNORE INTO", ""
        elif on_conflict == "replace":
            prefix, suffix = "INSERT OR REPLACE INTO", ""
        elif on_conflict == "update":
            prefix = "INSERT INTO"
            suffix = f" ON CONFLICT DO UPDATE SET {', '.join(_update_parts())}"
        else:
            raise ValueError(f"Unknown on_conflict: {on_conflict}")

    return f"{prefix} {tbl_name} ({cols_sql}) VALUES {values_sql}{suffix}", trailing


def render_insert(
    table: Table | str,
    columns: Sequence[str],
    n_rows: int,
    on_conflict: Literal["error", "ignore", "update", "replace"] = "error",
    update_columns: Sequence[str] | None = None,
    dialect: Dialect = "mariadb",
) -> str:
    """Render INSERT statement template with n_rows value groups (no `update_exprs`)."""
    return render_insert_with_params(table, columns, n_rows, on_conflict, update_columns, dialect)[0]


def render_update(
    table: Table | str,
    values: Mapping[str, Any],
    where: Where | None,
    limit: int | None = None,
    dialect: Dialect = "mariadb",
) -> tuple[str, list[Any]]:
    """Render UPDATE statement: {col: value | Inc(n) | Col("other") | Expr}.

    Assignments are written in the given order. MariaDB evaluates them left to right on the already-updated row,
    SQLite on the original row; an expression that reads a column should therefore come before the assignment
    that changes that column.
    """
    tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
    validate_ident(tbl_name)
    ph = placeholder(dialect)
    params: list[Any] = []

    assigns = [(validate_ident(c), v) for c, v in values.items()]
    reads = [
        {c} if isinstance(v, Inc) else (expr_columns(v) if isinstance(v, (Col, Expr)) else set())
        for c, v in assigns
    ]
    order = order_assignments([(c, r) for (c, _), r in zip(assigns, reads)], "update_rows(values=...)")
    if dialect == "mariadb":  # other dialects read the stored row whatever the order
        assigns = [assigns[i] for i in order]
    set_parts = []
    for col, val in assigns:
        if isinstance(val, Inc):
            set_parts.append(f"{col} = {col} + {ph}")
            params.append(val.n)
        elif isinstance(val, Col):
            set_parts.append(f"{col} = {validate_ident(val.name)}")
        elif isinstance(val, Expr):
            set_parts.append(f"{col} = {render_expr(val, params, dialect)}")
        else:
            set_parts.append(f"{col} = {ph}")
            params.append(val)

    if not set_parts:
        raise ValueError("update_rows requires at least one value to set")

    w_sql = render_where(where, params, dialect=dialect)
    where_str = f" WHERE {w_sql}" if w_sql else ""
    if limit is not None and dialect == "sqlite":
        # SQLite has no UPDATE ... LIMIT: restrict by rowid
        sub = f"SELECT rowid FROM {tbl_name}{where_str} LIMIT {int(limit)}"
        sql = f"UPDATE {tbl_name} SET {', '.join(set_parts)} WHERE rowid IN ({sub})"
        # the WHERE parameters now come after the SET parameters, as before
        return sql, params
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
    """Render DELETE statement (`LIMIT` becomes a rowid subquery on SQLite)."""
    tbl_name = table.table_name if hasattr(table, "table_name") else str(table)
    validate_ident(tbl_name)
    if where is None and not all_rows:
        raise ValueError("delete_rows without where requires all_rows=True")

    params: list[Any] = []
    w_sql = render_where(where, params, dialect=dialect)
    where_str = f" WHERE {w_sql}" if w_sql else ""
    if limit is not None and dialect == "sqlite":
        return (
            f"DELETE FROM {tbl_name} WHERE rowid IN (SELECT rowid FROM {tbl_name}{where_str} LIMIT {int(limit)})",
            params,
        )
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
    """Render INSERT [IGNORE] INTO ... SELECT ... statement (the select may join, be DISTINCT and use expressions)."""
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


def render_drop_tables(names: Sequence[str], *, if_exists: bool = True, dialect: Dialect = "mariadb") -> list[str]:
    """Render DROP TABLE statements (one statement for MariaDB, one per table for SQLite)."""
    clean = [validate_ident(n) for n in names]
    if not clean:
        return []
    ie = "IF EXISTS " if if_exists else ""
    if dialect == "mariadb":
        return [f"DROP TABLE {ie}{', '.join(clean)}"]
    return [f"DROP TABLE {ie}{n}" for n in clean]


_MARIADB_TYPES: dict[ColumnType, str] = {
    ColumnType.INT: "INT",
    ColumnType.BIGINT: "BIGINT",
    ColumnType.MEDIUMINT: "MEDIUMINT",
    ColumnType.SMALLINT: "SMALLINT",
    ColumnType.TINYINT: "TINYINT",
    ColumnType.BOOL: "TINYINT(1)",
    ColumnType.TEXT: "TEXT",
    ColumnType.MEDIUMTEXT: "MEDIUMTEXT",
    ColumnType.LONGTEXT: "LONGTEXT",
    ColumnType.BLOB: "BLOB",
    ColumnType.MEDIUMBLOB: "MEDIUMBLOB",
    ColumnType.LONGBLOB: "LONGBLOB",
    ColumnType.DATETIME: "DATETIME",
    ColumnType.TIMESTAMP: "TIMESTAMP",
    ColumnType.FLOAT: "FLOAT",
    ColumnType.DOUBLE: "DOUBLE",
    ColumnType.JSON: "JSON",
}
_SQLITE_TYPES: dict[ColumnType, str] = {
    ColumnType.INT: "INTEGER",
    ColumnType.BIGINT: "INTEGER",
    ColumnType.MEDIUMINT: "INTEGER",
    ColumnType.SMALLINT: "INTEGER",
    ColumnType.TINYINT: "INTEGER",
    ColumnType.BOOL: "INTEGER",
    ColumnType.TEXT: "TEXT",
    ColumnType.MEDIUMTEXT: "TEXT",
    ColumnType.LONGTEXT: "TEXT",
    ColumnType.CHAR: "TEXT",
    ColumnType.VARCHAR: "TEXT",
    ColumnType.BINARY: "BLOB",
    ColumnType.VARBINARY: "BLOB",
    ColumnType.BLOB: "BLOB",
    ColumnType.MEDIUMBLOB: "BLOB",
    ColumnType.LONGBLOB: "BLOB",
    ColumnType.DATETIME: "TEXT",
    ColumnType.TIMESTAMP: "TEXT",
    ColumnType.FLOAT: "REAL",
    ColumnType.DOUBLE: "REAL",
    ColumnType.JSON: "TEXT",
}


def render_column_type(spec: ColumnSpec, dialect: Dialect = "mariadb") -> str:
    """Render the SQL type (with UNSIGNED where the dialect has it) of a neutral column."""
    if dialect == "sqlite":
        return _SQLITE_TYPES[spec.type]
    if spec.type in (ColumnType.CHAR, ColumnType.VARCHAR, ColumnType.BINARY, ColumnType.VARBINARY):
        base = f"{spec.type.value}({int(spec.length or 0)})"
    else:
        base = _MARIADB_TYPES[spec.type]
    return f"{base} UNSIGNED" if spec.unsigned else base


def render_default(default: Any, dialect: Dialect = "mariadb") -> str:
    """Render the literal after `DEFAULT` for a `ColumnSpec.default` (not `NO_DEFAULT`)."""
    if default is CURRENT_TIMESTAMP:
        return "CURRENT_TIMESTAMP"
    return sql_literal(default, dialect)


def render_column_ddl(name: str, spec: ColumnSpec, dialect: Dialect = "mariadb") -> str:
    """Render `name TYPE [NOT NULL] [DEFAULT x] [AUTO_INCREMENT]` for ALTER/CREATE TABLE.

    On SQLite `auto_increment` renders `PRIMARY KEY AUTOINCREMENT` (valid for one INTEGER primary-key column).
    """
    parts = [validate_ident(name), render_column_type(spec, dialect)]
    if not spec.nullable:
        parts.append("NOT NULL")
    elif spec.explicit_null:
        parts.append("NULL")
    if spec.default is not NO_DEFAULT:
        parts.append(f"DEFAULT {render_default(spec.default, dialect)}")
    if spec.bin_collation and dialect == "mariadb" and spec.type in _TEXT_TYPES:
        parts.append("COLLATE utf8mb4_bin")
    if spec.auto_increment:
        parts.append("AUTO_INCREMENT" if dialect == "mariadb" else "PRIMARY KEY AUTOINCREMENT")
    return " ".join(parts)


_TEXT_TYPES = frozenset({ColumnType.CHAR, ColumnType.VARCHAR, ColumnType.TEXT, ColumnType.MEDIUMTEXT, ColumnType.LONGTEXT})


def render_create_table_statements(table: Any, dialect: Dialect = "mariadb") -> list[str]:
    """`CREATE TABLE` (and, where the dialect needs them, `CREATE INDEX`) statements for a `Table` schema.

    `table` supplies `table_name`, `init_columns` (`(name, ColumnSpec)` pairs), `init_primary`, `unique_keys`,
    `indexes` and `init_foreign`. MariaDB gets one statement with the keys inline; SQLite gets the table plus
    one `CREATE INDEX` per secondary index (an AUTO_INCREMENT column becomes `INTEGER PRIMARY KEY AUTOINCREMENT`).
    """
    columns = [(c[0], c[1]) for c in table.init_columns]
    keys = list(table.unique_keys or ())
    indexes = list(table.indexes or ())
    foreign = list(table.init_foreign or ())
    if dialect == "mariadb":
        sql = f"CREATE TABLE {table.table_name} "
        sql += f"({', '.join(render_column_ddl(n, s, dialect) for n, s in columns)} "
        sql += f", PRIMARY KEY ({', '.join(table.init_primary)}) "
        for uk in keys:
            sql += f", UNIQUE KEY ({', '.join(uk)}) "
        for idx_name, idx_cols in indexes:
            sql += f", INDEX {idx_name} ({', '.join(idx_cols)}) "
        if foreign:
            sql += " ".join(f",FOREIGN KEY ({x[0]}) REFERENCES {x[1]}({x[2]})" for x in foreign)
        return [sql + ")"]
    inline_pk = any(s.auto_increment for _, s in columns)
    defs = [render_column_ddl(n, s, dialect) for n, s in columns]
    if not inline_pk:
        defs.append(f"PRIMARY KEY ({', '.join(table.init_primary)})")
    defs.extend(f"UNIQUE ({', '.join(uk)})" for uk in keys)
    defs.extend(f"FOREIGN KEY ({x[0]}) REFERENCES {x[1]}({x[2]})" for x in foreign)
    out = [f"CREATE TABLE {table.table_name} ({', '.join(defs)})"]
    out.extend(f"CREATE INDEX {n} ON {table.table_name} ({', '.join(c)})" for n, c in indexes)
    return out


def render_create_table(table: Any, dialect: Dialect = "mariadb") -> str:
    """The `CREATE TABLE` statement of `table` (see `render_create_table_statements` for the index statements)."""
    return render_create_table_statements(table, dialect)[0]


def sql_literal(value: Any, dialect: Dialect = "mariadb") -> str:
    """Render a Python value as an inline SQL literal (used only for DDL such as views and defaults)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex() + "'"
    text = str(value).replace("'", "''")
    if dialect == "mariadb":
        text = text.replace("\\", "\\\\")
    return f"'{text}'"


def inline_params(sql: str, params: Sequence[Any], dialect: Dialect = "mariadb") -> str:
    """Substitute the bind placeholders of rendered `sql` with literals (for view definitions)."""
    token = placeholder(dialect)
    pieces = sql.split(token)
    if len(pieces) - 1 != len(params):
        raise ValueError(f"Placeholder count {len(pieces) - 1} does not match {len(params)} parameters")
    out = [pieces[0]]
    for value, piece in zip(params, pieces[1:]):
        out.append(sql_literal(value, dialect))
        out.append(piece)
    return "".join(out)


def render_select_literal(select: Select, dialect: Dialect = "mariadb") -> str:
    """Render a Select with its parameters inlined as literals (the body of `CREATE VIEW`)."""
    sql, params, _ = render_select(select, dialect=dialect)
    return inline_params(sql, params, dialect)
