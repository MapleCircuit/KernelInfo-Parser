"""cache_engine/base.py - Abstract cache engine, neutral schema and filter objects (cache_engine/CACHE_API.md).

A *cache engine* keeps rebuildable, locally stored derived data. Its public surface is expressed in cache terms
(spaces, stores, records, keys, conditions, metadata, batches, compaction), not in storage terms (files, tables,
SQL, cursors), so engines that work differently (an in-memory map, a directory of files, a key-value library) can
implement it and be selected by configuration.
"""
from __future__ import annotations

import abc
import enum
import re
from dataclasses import dataclass, field as _dc_field
from typing import Any, ContextManager, Iterable, Iterator, Mapping, Sequence

from .errors import CacheSchemaError

_IDENT_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
_NAMESPACE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
RESERVED_STORE_NAMES = frozenset({"meta"})

Record = tuple
Key = Any  # a scalar for a single-field key, else a tuple of the key fields in `StoreSpec.key` order


# ----------------------------------------------------------------------------------------- schema

class FieldType(enum.Enum):
    INT = "int"
    FLOAT = "float"
    TEXT = "text"
    BYTES = "bytes"


@dataclass(frozen=True)
class Field:
    name: str
    type: FieldType
    nullable: bool = False

    def __post_init__(self) -> None:
        if not _IDENT_RE.fullmatch(self.name):
            raise CacheSchemaError(f"invalid field name: {self.name!r}")


def INT(name: str, null: bool = False) -> Field:
    return Field(name, FieldType.INT, null)


def FLOAT(name: str, null: bool = False) -> Field:
    return Field(name, FieldType.FLOAT, null)


def TEXT(name: str, null: bool = False) -> Field:
    return Field(name, FieldType.TEXT, null)


def BYTES(name: str, null: bool = False) -> Field:
    return Field(name, FieldType.BYTES, null)


@dataclass(frozen=True)
class StoreSpec:
    """Declaration of one store: an ordered set of records of fixed fields.

    - ``key``: field names identifying a record (unique). Empty: a keyless store (an append-only set, deduplicated
      by ``unique`` when given; ``scan`` returns it in insertion order).
    - ``unique``: further groups of fields that must be unique (a violating ``put_many(replace=False)`` record is
      skipped, a ``replace=True`` one replaces the existing record). Groups with nullable fields are enforced by the
      engine's own NULL semantics (SQLite: NULLs never collide), so callers must not rely on them there.
    - ``indexes``: field groups lookups filter on (a hint: engines may ignore it, results never depend on it).
    - ``clustered``: a hint that records should be stored in key order (point and range lookups by key are the
      dominant access); ignored by engines without such a notion.
    """
    name: str
    fields: tuple[Field, ...]
    key: tuple[str, ...] = ()
    unique: tuple[tuple[str, ...], ...] = ()
    indexes: tuple[tuple[str, ...], ...] = ()
    clustered: bool = False

    def __post_init__(self) -> None:
        if not _IDENT_RE.fullmatch(self.name) or self.name in RESERVED_STORE_NAMES:
            raise CacheSchemaError(f"invalid store name: {self.name!r}")
        names = [f.name for f in self.fields]
        if not names or len(set(names)) != len(names):
            raise CacheSchemaError(f"store {self.name!r}: fields must be non-empty and distinct")
        for group in (self.key, *self.unique, *self.indexes):
            for n in group:
                if n not in names:
                    raise CacheSchemaError(f"store {self.name!r}: unknown field {n!r}")
        if self.clustered and not self.key:
            raise CacheSchemaError(f"store {self.name!r}: clustered needs a key")

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise CacheSchemaError(f"store {self.name!r} has no field {name!r}")


# ----------------------------------------------------------------------------------------- conditions

@dataclass(frozen=True)
class Eq:
    field: str
    value: Any


@dataclass(frozen=True)
class In:
    field: str
    values: Sequence[Any]


@dataclass(frozen=True)
class Range:
    """Bounds on a field (any combination of ge/gt/le/lt; byte strings and text compare bytewise/by code point)."""
    field: str
    ge: Any = None
    gt: Any = None
    le: Any = None
    lt: Any = None


@dataclass(frozen=True)
class Prefix:
    """A text or bytes field starting with ``value``."""
    field: str
    value: Any


@dataclass(frozen=True)
class IsNull:
    field: str
    negate: bool = False


Condition = Eq | In | Range | Prefix | IsNull
Where = Mapping[str, Any] | Sequence[Condition] | Condition | None


def as_conditions(where: Where) -> list[Condition]:
    """Normalise ``where`` (all conditions must hold): one condition, a ``{field: value}`` mapping (``None`` means IS NULL; a
    list/tuple/set value means IN) or a sequence of condition objects."""
    if where is None:
        return []
    if isinstance(where, Mapping):
        out: list[Condition] = []
        for name, value in where.items():
            if value is None:
                out.append(IsNull(name))
            elif isinstance(value, (list, tuple, set, frozenset)):
                out.append(In(name, list(value)))
            else:
                out.append(Eq(name, value))
        return out
    if isinstance(where, (Eq, In, Range, Prefix, IsNull)):
        return [where]
    return list(where)


# ----------------------------------------------------------------------------------------- handles

class CacheStore(abc.ABC):
    """One declared store of a space. Records are tuples in ``spec.fields`` order; keys are scalars (one key
    field) or tuples (several). Every call is atomic and thread-safe; use ``CacheSpace.batch()`` to group calls."""

    spec: StoreSpec

    @abc.abstractmethod
    def get(self, key: Key) -> Record | None: ...

    @abc.abstractmethod
    def get_many(self, keys: Iterable[Key]) -> dict[Key, Record]:
        """{key: record} of the keys present (any number of keys; absent ones are omitted)."""

    @abc.abstractmethod
    def put(self, record: Record) -> None:
        """Insert or replace one record."""

    @abc.abstractmethod
    def put_many(self, records: Iterable[Record], *, replace: bool = True) -> int:
        """Store records in one atomic bulk call (``records`` may be a generator). ``replace=True``: insert or
        replace; ``False``: insert only when no existing record collides on the key or a unique group. Returns the
        number of records written."""

    @abc.abstractmethod
    def delete(self, key: Key) -> bool: ...

    @abc.abstractmethod
    def delete_many(self, keys: Iterable[Key]) -> int: ...

    @abc.abstractmethod
    def delete_where(self, where: Where) -> int:
        """Delete the matching records (all records when ``where`` is empty); returns the count."""

    @abc.abstractmethod
    def update_where(self, where: Where, values: Mapping[str, Any]) -> int:
        """Set fields of the matching records (not key fields); returns the count."""

    @abc.abstractmethod
    def scan(self, where: Where = None, *, columns: Sequence[str] | None = None,
             order_by: Sequence[str] = (), limit: int | None = None) -> Iterator[tuple]:
        """Matching records (``columns``: project these fields, in that order). ``order_by``: field names, a leading
        ``-`` for descending. Without it the order is unspecified, except that a keyless store yields insertion
        order. An ``In`` too long for one engine call is split transparently, unless ``order_by`` or ``limit``
        is given (then ``CacheSchemaError``)."""

    @abc.abstractmethod
    def count(self, where: Where = None) -> int: ...

    @abc.abstractmethod
    def sum(self, field: str, where: Where = None) -> int | float:
        """Sum of a numeric field (0 for no records)."""

    @abc.abstractmethod
    def max(self, field: str, where: Where = None) -> Any:
        """Largest value of a field, or None for no records."""

    @abc.abstractmethod
    def clear(self) -> None:
        """Delete every record of this store."""


class CacheSpace(abc.ABC):
    """A named group of stores that share storage, lifetime, metadata and transactions (one ``open()`` of an engine).
    It is the unit of invalidation (``clear()``, ``discard()``) and of maintenance (``compact()``)."""

    namespace: str
    name: str

    @abc.abstractmethod
    def store(self, name: str) -> CacheStore: ...

    @abc.abstractmethod
    def get_meta(self, key: str) -> str | None:
        """A freshness marker / version stamp of the space (built-at time, covered heads ...)."""

    @abc.abstractmethod
    def set_meta(self, key: str, value: str) -> None: ...

    @abc.abstractmethod
    def delete_meta(self, key: str) -> None: ...

    @abc.abstractmethod
    def batch(self) -> ContextManager[None]:
        """``with space.batch():`` makes the calls inside atomic (all stores and metadata of the space); re-entrant
        (the outermost block commits, an exception rolls everything back). Other threads see it only when done."""

    @abc.abstractmethod
    def clear(self) -> None:
        """Delete every record of every store and all metadata (the schema stays)."""

    @abc.abstractmethod
    def compact(self) -> None:
        """Maintenance: reclaim space / fold logs into the main storage. Never changes content."""

    @abc.abstractmethod
    def size_bytes(self) -> int:
        """Storage used by the space (0 when it holds nothing on disk)."""

    @abc.abstractmethod
    def discard(self) -> None:
        """Delete the space's storage entirely (invalidation of a corrupt or obsolete cache); the handle is
        unusable afterwards (``engine.open`` creates a fresh one)."""

    @abc.abstractmethod
    def close(self) -> None: ...

    @property
    @abc.abstractmethod
    def location(self) -> str:
        """Human-readable location (a path, a URL ...), for logs and status pages only."""


class BaseCacheEngine(abc.ABC):
    """Factory of cache spaces. Implementations register under a name (``register_cache_engine``)."""

    name: str = "base"

    @abc.abstractmethod
    def open(self, namespace: str, name: str, stores: Sequence[StoreSpec]) -> CacheSpace:
        """Open (creating when absent) the space ``namespace``/``name`` holding ``stores``. Opening the same space
        again returns the same handle (cheap: services may call it on every use). A held space whose stored schema
        differs from ``stores`` raises ``CacheSchemaError``; version the space ``name`` to change a schema."""

    @abc.abstractmethod
    def exists(self, namespace: str, name: str) -> bool:
        """The space has storage already (checked without creating it)."""

    @abc.abstractmethod
    def location(self, namespace: str, name: str) -> str: ...

    @abc.abstractmethod
    def close(self) -> None:
        """Close every open space."""


def check_namespace(namespace: str) -> str:
    if not _NAMESPACE_RE.fullmatch(namespace or ""):
        raise CacheSchemaError(f"invalid cache namespace: {namespace!r}")
    return namespace
