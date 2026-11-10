"""db_engine/spec.py - Declarative Query Specification Primitives.

Pure data types and condition constructors defining caller queries, filters,
projections, and joins without raw SQL generation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Mapping, NamedTuple, Sequence, Union

if TYPE_CHECKING:
    from core.TableHandling import Table

# Table schema object, table/view name, or (Table|name, alias). "schema.table" is supported for SQLite ATTACH.
Source = Union["Table", str, tuple[Union["Table", str], str]]


class Cond(NamedTuple):
    """Operator specification for Where clauses."""

    op: str
    value: Any = None
    value2: Any = None


def ne(v: Any) -> Cond:
    """col != %s"""
    return Cond("ne", v)


def lt(v: Any) -> Cond:
    """col < %s"""
    return Cond("lt", v)


def le(v: Any) -> Cond:
    """col <= %s"""
    return Cond("le", v)


def gt(v: Any) -> Cond:
    """col > %s"""
    return Cond("gt", v)


def ge(v: Any) -> Cond:
    """col >= %s"""
    return Cond("ge", v)


def between(lo: Any, hi: Any) -> Cond:
    """col BETWEEN %s AND %s"""
    return Cond("between", lo, hi)


def not_in(values: Any) -> Cond:
    """col NOT IN (%s, ...)"""
    return Cond("not_in", tuple(values))


def is_null() -> Cond:
    """col IS NULL"""
    return Cond("is_null")


def not_null() -> Cond:
    """col IS NOT NULL"""
    return Cond("not_null")


def like(
    text: str,
    mode: Literal["prefix", "suffix", "contains", "exact"] = "contains",
    *,
    ci: bool = False,
) -> Cond:
    """LIKE clause with automatic % _ \\ escaping and ESCAPE '\\'.
    
    If ci=True: LOWER(col) LIKE LOWER(%s).
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if mode == "prefix":
        pattern = f"{escaped}%"
    elif mode == "suffix":
        pattern = f"%{escaped}"
    elif mode == "contains":
        pattern = f"%{escaped}%"
    elif mode == "exact":
        pattern = escaped
    else:
        raise ValueError(f"Unknown like mode: {mode}")
    return Cond("like", pattern, ci)


def ieq(v: Any) -> Cond:
    """Case-insensitive equality: LOWER(col) = LOWER(%s)."""
    return Cond("ieq", v)


def hash_prefix(hex_prefix: str | bytes) -> Cond:
    """BINARY prefix range [lo, hi); odd length bounds last nibble; all-'f' bounds >= lo."""
    if isinstance(hex_prefix, bytes):
        hex_prefix = hex_prefix.hex()
    clean = hex_prefix.strip().lower()
    if not clean:
        raise ValueError("hash_prefix cannot be empty")
    lo_hex = clean + ("0" if len(clean) % 2 else "")
    hi_hex = clean + ("f" if len(clean) % 2 else "")
    nxt = int(hi_hex, 16) + 1
    hi = nxt.to_bytes(len(hi_hex) // 2, "big") if nxt < 16 ** len(hi_hex) else None
    return Cond("hash_prefix", bytes.fromhex(lo_hex), hi)


def len_lt(n: int) -> Cond:
    """CHAR_LENGTH(col) < n"""
    return Cond("len_lt", n)


class Col(NamedTuple):
    """Reference to another column name: col = other_col."""

    name: str


class Value(NamedTuple):
    """Literal constant value inside Join.on or expressions."""

    v: Any


class Inc(NamedTuple):
    """Increment expression for update_rows: col = col + n."""

    n: Any = 1


class Join(NamedTuple):
    """Join clause specification."""

    source: Source
    on: Mapping[str, Any]
    kind: Literal["inner", "left"] = "inner"


# Forward declaration for recursive Where types
WhereTerm = Any  # Mapping[str, Any] | AnyOf | Exists | InSelect
Where = Union[Mapping[str, Any], Sequence[WhereTerm]]


@dataclass(frozen=True)
class AnyOf:
    """OR-combination of multiple Where filter groups."""

    branches: tuple[Where, ...]

    def __init__(self, *branches: Where) -> None:
        if len(branches) == 1 and isinstance(branches[0], tuple):
            object.__setattr__(self, "branches", branches[0])
        else:
            object.__setattr__(self, "branches", tuple(branches))


class Exists(NamedTuple):
    """Correlated [NOT] EXISTS subquery specification."""

    source: Source
    on: Mapping[str, str]
    where: Where | None = None
    joins: Sequence[Join] = ()
    negate: bool = False


def NotExists(
    source: Source,
    on: Mapping[str, str],
    where: Where | None = None,
    joins: Sequence[Join] = (),
) -> Exists:
    """Construct a NOT EXISTS subquery term."""
    return Exists(source=source, on=on, where=where, joins=joins, negate=True)


class InSelect(NamedTuple):
    """(a[, b]) [NOT] IN (SELECT x[, y] FROM ... WHERE ...) subquery."""

    columns: str | tuple[str, ...]
    source: Source
    select: str | tuple[str, ...]
    where: Where | None = None
    negate: bool = False


def finished_ref(alias: str = "r") -> Exists:
    """NOT EXISTS (m_ingest_state, status = INGEST_PARSING); replaces finished_ref_sql()."""
    return NotExists(
        source=("m_ingest_state", "s"),
        on={"s.commit_id": f"{alias}.commit_id"},
        where={"s.status": 1},
    )


class Agg(NamedTuple):
    """Aggregate function expression: COUNT, SUM, MIN, MAX, AVG."""

    func: Literal["COUNT", "SUM", "MIN", "MAX", "AVG"]
    column: str = "*"
    distinct: bool = False


class Const(NamedTuple):
    """Literal constant column value: '0' AS s_stat, 1 AS same_name."""

    value: Any


@dataclass(frozen=True)
class Select:
    """Select statement specification used for copy_rows() and subqueries."""

    source: Source
    columns: Sequence[str | Agg | Const] | Mapping[str, str | Agg | Const]
    where: Where | None = None
    joins: Sequence[Join] = ()
    distinct: bool = False
    order_by: Sequence[str] = ()
    limit: int | None = None
    offset: int | None = None


def has_empty_in_term(where: Where | None) -> bool:
    """Return True if any top-level AND term is an empty IN list."""
    if where is None:
        return False
    if isinstance(where, Mapping):
        for val in where.values():
            if isinstance(val, (list, tuple, set, frozenset)) and len(val) == 0:
                return True
    elif isinstance(where, Sequence):
        for term in where:
            if isinstance(term, Mapping):
                for val in term.values():
                    if isinstance(val, (list, tuple, set, frozenset)) and len(val) == 0:
                        return True
    return False


def find_oversize_in_term(
    where: Where | None,
    chunk_size: int = 1000,
) -> tuple[int | None, str | None, list[Any] | None]:
    """Find single top-level AND term with an IN list > chunk_size."""
    if where is None:
        return None, None, None
    oversize: list[tuple[int | None, str, list[Any]]] = []
    if isinstance(where, Mapping):
        for col, val in where.items():
            if isinstance(val, (list, tuple, set, frozenset)) and len(val) > chunk_size:
                oversize.append((None, col, list(val)))
    elif isinstance(where, Sequence):
        for idx, term in enumerate(where):
            if isinstance(term, Mapping):
                for col, val in term.items():
                    if isinstance(val, (list, tuple, set, frozenset)) and len(val) > chunk_size:
                        oversize.append((idx, col, list(val)))
    if len(oversize) > 1:
        raise ValueError(f"Auto-chunking supports only one oversize IN list, found {len(oversize)}")
    if len(oversize) == 1:
        return oversize[0]
    return None, None, None


def replace_in_term(
    where: Where | None,
    target_term_idx: int | None,
    target_col: str,
    sub_list: list[Any],
) -> Where:
    """Return a copy of where replacing target_col with sub_list."""
    if where is None:
        return {target_col: sub_list}
    if isinstance(where, Mapping):
        new_w = dict(where)
        new_w[target_col] = sub_list
        return new_w
    new_terms = []
    for idx, term in enumerate(where):
        if idx == target_term_idx and isinstance(term, Mapping):
            new_t = dict(term)
            new_t[target_col] = sub_list
            new_terms.append(new_t)
        else:
            new_terms.append(term)
    return new_terms


def dedup_rows(rows: list[Any], as_dict: bool) -> list[Any]:
    """Deduplicate rows preserving insertion order."""
    seen = set()
    deduped = []
    for r in rows:
        key = tuple(sorted(r.items())) if as_dict else tuple(r)
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped
