"""db_engine/spec.py - Declarative Query Specification Primitives.

Pure data types and condition constructors defining caller queries, filters,
projections, and joins without raw SQL generation.
"""
from __future__ import annotations

import enum
import re
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


def eq(v: Any) -> Cond:
    """col = %s (`eq(None)` is `col IS NULL`); for a second condition on the same column: `{"a": [eq(1), gt(0)]}`."""
    return Cond("eq", v)


def ne(v: Any) -> Cond:
    """col != %s (`ne(None)` is `col IS NOT NULL`)."""
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


def all_of(*terms: Where) -> list[WhereTerm]:
    """AND of several Where terms (mappings, `AnyOf`, subqueries); list arguments are flattened.

    Use it, or a list of conditions as the value (`{"a": [gt(0), lt(10)]}`), to put several conditions on one column.
    """
    out: list[WhereTerm] = []
    for term in terms:
        if term is None:
            continue
        if isinstance(term, list):
            out.extend(term)
        else:
            out.append(term)
    return out


def any_of(*terms: Where) -> AnyOf:
    """OR of several Where terms (same as `AnyOf(*terms)`)."""
    return AnyOf(*terms)


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
    """Select statement specification used for copy_rows(), subqueries and views.

    `columns` items (or mapping values) are a column name, `Agg`, `Const` or any `Expr`; `order_by` / `group_by`
    items are a column name (`"-col"` = descending), an `Expr` or an `Order`; `having` is a Where over aggregates
    (`Pred(Agg(...), cond)`) or output aliases. `hint="straight_join"` asks MariaDB to join in the written order
    (ignored by other dialects).
    """

    source: Source
    columns: Sequence[Any] | Mapping[str, Any]
    where: Where | None = None
    joins: Sequence[Join] = ()
    distinct: bool = False
    order_by: Sequence[Any] = ()
    limit: int | None = None
    offset: int | None = None
    group_by: Sequence[Any] = ()
    having: Where | None = None
    hint: Literal["straight_join"] | None = None


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


# -----------------------------------------------------------------------------
# Expressions
# -----------------------------------------------------------------------------
# Operand rule inside every expression: `str` = column reference, `Value(v)` / `Const(v)` = bound literal,
# `Expr` / `Agg` = nested expression, any other scalar (int, float, bytes, bool, None) = bound literal.
@dataclass(frozen=True)
class Expr:
    """Marker base class of scalar expressions usable in select columns, conditions, order_by and update values."""


def _varargs(obj: Any, name: str, items: tuple[Any, ...]) -> None:
    object.__setattr__(obj, name, items)


@dataclass(frozen=True, init=False)
class Concat(Expr):
    """String concatenation (MariaDB `CONCAT`, SQLite `||`)."""

    parts: tuple[Any, ...]

    def __init__(self, *parts: Any) -> None:
        _varargs(self, "parts", parts)


@dataclass(frozen=True)
class Length(Expr):
    """Length in characters (MariaDB `CHAR_LENGTH`, SQLite `LENGTH`)."""

    operand: Any


@dataclass(frozen=True)
class Substr(Expr):
    """`SUBSTR(operand, start[, length])`; `start` is 1-based. Use `Substr(x, 1, n)` for `LEFT(x, n)`."""

    operand: Any
    start: Any = 1
    length: Any = None


@dataclass(frozen=True, init=False)
class Coalesce(Expr):
    """First non-NULL operand."""

    parts: tuple[Any, ...]

    def __init__(self, *parts: Any) -> None:
        _varargs(self, "parts", parts)


@dataclass(frozen=True)
class NullIf(Expr):
    """NULL when both operands are equal, else the first."""

    left: Any
    right: Any


@dataclass(frozen=True)
class Arith(Expr):
    """Binary arithmetic: op is one of `+ - *` (division differs between dialects and is not offered)."""

    op: Literal["+", "-", "*"]
    left: Any
    right: Any


def add(left: Any, right: Any) -> Arith:
    """left + right"""
    return Arith("+", left, right)


def sub(left: Any, right: Any) -> Arith:
    """left - right"""
    return Arith("-", left, right)


def mul(left: Any, right: Any) -> Arith:
    """left * right"""
    return Arith("*", left, right)


@dataclass(frozen=True)
class Hex(Expr):
    """Upper-case hexadecimal text of a binary operand (`HEX` / `hex`)."""

    operand: Any


@dataclass(frozen=True, init=False)
class Case(Expr):
    """`CASE WHEN <where> THEN <value> ... ELSE <otherwise> END`; each `when` is `(Where, operand)`."""

    whens: tuple[tuple[Any, Any], ...]
    otherwise: Any

    def __init__(self, whens: Sequence[tuple[Any, Any]], otherwise: Any = None) -> None:
        object.__setattr__(self, "whens", tuple((w, v) for w, v in whens))
        object.__setattr__(self, "otherwise", otherwise)


@dataclass(frozen=True)
class Incoming(Expr):
    """The value a conditional upsert tried to insert for `column` (`VALUES(col)` / `excluded.col`)."""

    column: str


class Order(NamedTuple):
    """`ORDER BY` / `GROUP BY` item over an expression."""

    expr: Any
    desc: bool = False


class Pred(NamedTuple):
    """Where term testing an expression: `Pred(Add("n", 1), ge(3))` or `Pred(Length("name"), 5)` (equality)."""

    expr: Any
    test: Any


class InQuery(NamedTuple):
    """`(a[, b]) [NOT] IN (<Select>)` over a full `Select` (joins, distinct, expressions allowed)."""

    columns: str | tuple[str, ...]
    select: Select
    negate: bool = False


class ExistsQuery(NamedTuple):
    """`[NOT] EXISTS (<Select>)`; the select may reference outer columns with `Col("alias.col")` values."""

    select: Select
    negate: bool = False


def in_select(columns: str | Sequence[str], select: Select, *, negate: bool = False) -> InQuery:
    """`col IN (SELECT ...)` / `(a, b) IN (SELECT a, b ...)` term."""
    cols: str | tuple[str, ...] = columns if isinstance(columns, str) else tuple(columns)
    return InQuery(cols, select, negate)


def exists_select(select: Select, *, negate: bool = False) -> ExistsQuery:
    """`EXISTS (SELECT ...)` term; `negate=True` gives `NOT EXISTS`."""
    return ExistsQuery(select, negate)


# -----------------------------------------------------------------------------
# Neutral column descriptions
# -----------------------------------------------------------------------------
class ColumnType(str, enum.Enum):
    """Backend-neutral column types."""

    INT = "INT"
    BIGINT = "BIGINT"
    MEDIUMINT = "MEDIUMINT"
    SMALLINT = "SMALLINT"
    TINYINT = "TINYINT"
    BOOL = "BOOL"
    TEXT = "TEXT"
    MEDIUMTEXT = "MEDIUMTEXT"
    LONGTEXT = "LONGTEXT"
    CHAR = "CHAR"
    VARCHAR = "VARCHAR"
    BINARY = "BINARY"
    VARBINARY = "VARBINARY"
    BLOB = "BLOB"
    MEDIUMBLOB = "MEDIUMBLOB"
    LONGBLOB = "LONGBLOB"
    DATETIME = "DATETIME"
    TIMESTAMP = "TIMESTAMP"
    FLOAT = "FLOAT"
    DOUBLE = "DOUBLE"
    JSON = "JSON"


LENGTH_TYPES = frozenset({ColumnType.CHAR, ColumnType.VARCHAR, ColumnType.BINARY, ColumnType.VARBINARY})
BINARY_TYPES = frozenset({ColumnType.BINARY, ColumnType.VARBINARY, ColumnType.BLOB, ColumnType.MEDIUMBLOB, ColumnType.LONGBLOB})
LARGE_TYPES = frozenset({ColumnType.TEXT, ColumnType.MEDIUMTEXT, ColumnType.LONGTEXT, ColumnType.BLOB, ColumnType.MEDIUMBLOB, ColumnType.LONGBLOB})


class _Sentinel:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


NO_DEFAULT = _Sentinel("NO_DEFAULT")
CURRENT_TIMESTAMP = _Sentinel("CURRENT_TIMESTAMP")


@dataclass(frozen=True)
class ColumnSpec:
    """Neutral column description used by `add_column` (and, from the neutral DBLayout, `create_table`).

    `default` is `NO_DEFAULT` (no DEFAULT clause), `None` (DEFAULT NULL), `CURRENT_TIMESTAMP`, or a literal.
    `length` is required for VARCHAR / BINARY / VARBINARY.
    """

    type: ColumnType
    length: int | None = None
    unsigned: bool = False
    nullable: bool = True
    default: Any = NO_DEFAULT
    auto_increment: bool = False
    bin_collation: bool = False
    explicit_null: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.type, ColumnType):
            object.__setattr__(self, "type", ColumnType(str(self.type).upper()))
        if self.type in LENGTH_TYPES and not self.length:
            raise ValueError(f"{self.type.value} requires a length")

    @property
    def is_binary(self) -> bool:
        """True for BINARY / VARBINARY / BLOB-family columns (values are `bytes`)."""
        return self.type in BINARY_TYPES

    @property
    def is_large(self) -> bool:
        """True for TEXT / BLOB-family columns (rows are written in smaller batches)."""
        return self.type in LARGE_TYPES

    @property
    def is_integer(self) -> bool:
        return self.type in (ColumnType.INT, ColumnType.BIGINT, ColumnType.MEDIUMINT, ColumnType.SMALLINT, ColumnType.TINYINT)


# -- Compact constructors used by the schema (`core/DBLayout.py`) -------------------------------------------
# Columns are NOT NULL unless `null` says otherwise: `null=True` is a nullable column whose DDL spells out
# `NULL`; `null=None` is nullable with no nullability keyword at all (both identical for every backend, the
# distinction only keeps the rendered MariaDB DDL byte-identical). `bin_collation=True` asks for a
# case-sensitive binary collation on text columns (utf8mb4_bin on MariaDB, SQLite compares text binary anyway).
def _make(ctype: ColumnType, length: int | None = None, *, unsigned: bool = False, null: bool | None = False,
          default: Any = NO_DEFAULT, auto: bool = False, bin_collation: bool = False) -> ColumnSpec:
    return ColumnSpec(ctype, length, unsigned=unsigned, nullable=null is not False, default=default,
                      auto_increment=auto, bin_collation=bin_collation, explicit_null=null is True)


def INT(**kw: Any) -> ColumnSpec:
    return _make(ColumnType.INT, **kw)


def BIGINT(**kw: Any) -> ColumnSpec:
    return _make(ColumnType.BIGINT, **kw)


def MEDIUMINT(**kw: Any) -> ColumnSpec:
    return _make(ColumnType.MEDIUMINT, **kw)


def SMALLINT(**kw: Any) -> ColumnSpec:
    return _make(ColumnType.SMALLINT, **kw)


def TINYINT(**kw: Any) -> ColumnSpec:
    return _make(ColumnType.TINYINT, **kw)


def TEXT(**kw: Any) -> ColumnSpec:
    return _make(ColumnType.TEXT, **kw)


def MEDIUMTEXT(**kw: Any) -> ColumnSpec:
    return _make(ColumnType.MEDIUMTEXT, **kw)


def MEDIUMBLOB(**kw: Any) -> ColumnSpec:
    return _make(ColumnType.MEDIUMBLOB, **kw)


def CHAR(length: int, **kw: Any) -> ColumnSpec:
    return _make(ColumnType.CHAR, length, **kw)


def VARCHAR(length: int, **kw: Any) -> ColumnSpec:
    return _make(ColumnType.VARCHAR, length, **kw)


def BIN(length: int, **kw: Any) -> ColumnSpec:
    """Fixed-width binary column (`BINARY(length)` on MariaDB, BLOB on SQLite)."""
    return _make(ColumnType.BINARY, length, **kw)


_FRAGMENT_TYPE = re.compile(r"^([A-Za-z]+)\s*(?:\((\d+)\))?$")


def parse_column_fragment(*fragments: str) -> ColumnSpec:
    """Convert legacy MariaDB DDL fragments (`"INT", "UNSIGNED", "NOT NULL", "DEFAULT 0"`) into a `ColumnSpec`.

    Compatibility input for ad-hoc `Table` definitions; the schema itself uses the neutral constructors.
    """
    words = [str(f).strip() for f in fragments if str(f).strip()]
    if not words:
        raise ValueError("empty column definition")
    tokens: list[str] = []
    for w in words:
        tokens.extend([w] if w.upper().startswith(("DEFAULT ", "COLLATE ")) else w.split())
    match = _FRAGMENT_TYPE.match(tokens[0])
    if not match:
        raise ValueError(f"Unrecognised column type {tokens[0]!r}")
    ctype = ColumnType(match.group(1).upper())
    length = int(match.group(2)) if match.group(2) else None
    unsigned, nullable, explicit_null, auto, bin_coll, default = False, True, False, False, False, NO_DEFAULT
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        up = tok.upper()
        if up == "UNSIGNED":
            unsigned = True
        elif up == "NOT" and i + 1 < len(tokens) and tokens[i + 1].upper() == "NULL":
            nullable = False
            i += 1
        elif up == "NULL":
            explicit_null = True
        elif up == "AUTO_INCREMENT":
            auto = True
        elif up.startswith("COLLATE"):
            bin_coll = True
        elif up.startswith("DEFAULT"):
            default = _parse_default(tok[len("DEFAULT"):].strip())
        else:
            raise ValueError(f"Unrecognised column modifier {tok!r}")
        i += 1
    return ColumnSpec(ctype, length, unsigned=unsigned, nullable=nullable, default=default,
                      auto_increment=auto, bin_collation=bin_coll, explicit_null=explicit_null)


def _parse_default(text: str) -> Any:
    if text.upper() == "NULL":
        return None
    if text.upper() == "CURRENT_TIMESTAMP":
        return CURRENT_TIMESTAMP
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    try:
        return int(text)
    except ValueError:
        return float(text)


def as_column_spec(definition: Any) -> ColumnSpec:
    """`ColumnSpec` from a `ColumnSpec` or legacy fragment(s) (a string or a sequence of strings)."""
    if isinstance(definition, ColumnSpec):
        return definition
    if isinstance(definition, str):
        return parse_column_fragment(definition)
    if len(definition) == 1 and isinstance(definition[0], ColumnSpec):
        return definition[0]
    return parse_column_fragment(*definition)
