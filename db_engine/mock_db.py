"""db_engine/mock_db.py - In-Memory Mock Database Driver.

`MockDB` is `SQLiteDB` over an in-memory SQLite database (one private database per instance, or one process-wide
shared database for `use_global=True`), so every spec feature, registry view and neutral error is implemented once
per real dialect (MariaDB, SQLite) and the mock evaluates them with SQLite. What it adds for isolated tests:

- tables are created on first use: a canonical table (`core/DBLayout.py`) by its name, any other `Table` from the
  `Table` object a call passed, and a registry view (`db_engine/views.py`) by its name; `drop_tables()` makes the name
  missing until `create_table()` / `create_view()` brings it back;
- FOREIGN KEY constraints are not enforced;
- the engine is not persistent (`capabilities` is empty) and never runs raw SQL on its public surface;
- `store` shows the rows of every table as `{primary key: row tuple}` dictionaries for test assertions.
"""
from __future__ import annotations

import contextlib
import dataclasses
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from db_engine.base import ColumnInfo, ProbeResult, _hybridmethod
from db_engine.errors import MissingTableError, UnsupportedOperation
from db_engine.sqlite_db import MEMORY, SHARED_MEMORY, SQLiteDB, SQLiteReadSession
from db_engine.spec import NO_DEFAULT, ColumnSpec, ColumnType
from db_engine.sql_render import render_column_type, render_create_table_statements
from db_engine.views import ORDERED_VIEW_NAMES

if TYPE_CHECKING:
    from core.TableHandling import Table

#: The read session of a `MockDB` (the read-only SQLite session).
MockReadSession = SQLiteReadSession

_MISSING_RE = re.compile(r"no such (?:table|view): (?:\w+\.)?(\w+)")
_CANONICAL: dict[str, Any] | None = None

# Public methods whose listed positional arguments name tables (a `Table` the mock may need to create on first use).
_NOTING_ARGS: dict[str, tuple[int, ...]] = {
    "select": (0,), "insert": (0,), "update": (0,), "select_in": (0,), "select_preload": (0,), "get_next_id": (0,),
    "reserve_id_block": (0,), "table_size": (0,), "view_select": (0,), "view_select_multiple": (0,),
    "select_rows": (0,), "insert_rows": (0,), "update_rows": (0,), "delete_rows": (0,), "copy_rows": (0, 2),
    "index_exists": (1,), "create_index": (1,), "remove_index": (1,), "column_exists": (0,), "column_info": (0,),
    "index_names": (0,), "verify_relational_integrity": (0,), "test_tables": (0,),
}
# Public methods that reach the raw SQL entry points internally (allowed; direct raw calls are not).
_INTERNAL_SQL = (
    "create_view", "drop_view", "add_column_if_missing", "column_exists", "ensure_columns", "ensure_views",
    "resolve_relation_name",
)


def _canonical_tables() -> dict[str, Any]:
    """Canonical `Table` objects of `core/DBLayout.py` by table name."""
    global _CANONICAL
    if _CANONICAL is None:
        import core.DBLayout as dbl

        _CANONICAL = {t.table_name: t for t in dbl.TABLES}
    return _CANONICAL


def _is_table(obj: Any) -> bool:
    return hasattr(obj, "table_name") and hasattr(obj, "init_columns")


class _Lenient:
    """A `Table` whose non-key columns accept NULL (`MockDB` does not enforce NOT NULL, so a test inserts only the
    columns it cares about); everything else reads through to the table."""

    def __init__(self, table: Any) -> None:
        self._table = table
        keys = set(table.init_primary)
        self.init_columns = [
            (n, spec if n in keys or spec.auto_increment or spec.nullable else dataclasses.replace(spec, nullable=True))
            for n, spec in table.init_columns
        ]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._table, name)


class _State:
    """Catalog state of one in-memory database: tables seen by name, names dropped explicitly, declared specs of
    columns added later."""

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}
        self.dropped: set[str] = set()
        self.added: dict[tuple[str, str], ColumnSpec] = {}


_SHARED_STATE = _State()


class _TableStore(Mapping):
    """Rows of every non-empty table as `{primary key: row tuple}` dictionaries (an unknown table reads as `{}`)."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner  # a MockDB, or a callable returning the shared MockDB

    def _db(self) -> MockDB:
        return self._owner() if callable(self._owner) else self._owner

    def _names(self) -> list[str]:
        db = self._db()
        rows, _, _ = db._run("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        return [r[0] for r in rows]

    def __getitem__(self, name: str) -> dict[Any, tuple[Any, ...]]:
        return self._db().table_rows(name)

    def get(self, name: str, default: Any = None) -> Any:
        rows = self._db().table_rows(name)
        return rows if rows else default

    def __iter__(self) -> Iterator[str]:
        return iter([n for n in self._names() if self._db().table_rows(n)])

    def __len__(self) -> int:
        return len(list(iter(self)))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and bool(self._db().table_rows(name))

    def clear(self) -> None:
        """Remove every table and view (the whole in-memory database)."""
        self._db().wipe()


class _StoreAccessor:
    """`MockDB.store` on the class is the shared database's store, `db.store` the instance's."""

    def __get__(self, obj: MockDB | None, cls: type[MockDB]) -> _TableStore:
        return _TableStore(obj if obj is not None else cls._shared)


class MockDB(SQLiteDB):
    """`SQLiteDB` over an in-memory database for isolated, fast tests (see the module docstring)."""

    display_name = "MockDB"
    base_capabilities = frozenset()
    store = _StoreAccessor()
    _shared_db: MockDB | None = None

    def __init__(self, use_global: bool = True) -> None:
        """Open an in-memory database: the process-wide shared one (`use_global=True`, visible to every such
        instance and to `MockDB.reader()`), or one private to this instance."""
        super().__init__(SHARED_MEMORY if use_global else MEMORY, foreign_keys=False)
        self._use_global = use_global
        self._state = _SHARED_STATE if use_global else _State()
        self._raw_depth = 0
        if use_global:
            with contextlib.suppress(Exception):
                self._run("PRAGMA read_uncommitted = ON")  # shared cache: reads see a sibling instance's open transaction

    # ------------------------------------------------------------------ shared database
    @classmethod
    def _shared(cls) -> MockDB:
        if MockDB._shared_db is None or MockDB._shared_db._conn is None:
            MockDB._shared_db = MockDB(use_global=True)
        return MockDB._shared_db

    @classmethod
    def reset(cls) -> None:
        """Remove every table and view of the shared in-memory database."""
        cls._shared().wipe()

    def wipe(self) -> None:
        """Remove every table and view of this database and forget what was dropped or seen."""
        with self._lock:
            rows = self._run("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND type IN ('view', 'table')")[0]
            for kind, name in sorted(rows, key=lambda r: r[0] != "view"):
                self._run(f"DROP {kind.upper()} IF EXISTS {name}")
            self._state.seen.clear()
            self._state.dropped.clear()
            self._state.added.clear()

    def close(self) -> None:
        """No-op: the in-memory database lives as long as its instance (or the process, when shared), so a closed
        `MockDB` can still be used, like a database a test hands to an engine that closes it."""

    def __del__(self) -> None:
        conn, self._conn = getattr(self, "_conn", None), None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    @classmethod
    def warm_up(cls) -> None:
        """Nothing to open (the in-memory database exists once instantiated)."""

    @classmethod
    def shutdown(cls) -> None:
        """Nothing to close."""

    @classmethod
    def probe_connection(cls, cfg: Mapping[str, Any]) -> ProbeResult:
        """The in-memory engine is always reachable."""
        return ProbeResult(True, "mockdb", 0.0, "", "memory")

    @_hybridmethod
    def reader(self) -> MockReadSession:
        """Read-only session of this instance, or of the shared in-memory database when called on the class."""
        return SQLiteReadSession(self if isinstance(self, MockDB) else MockDB._shared(), owned=False)

    # ------------------------------------------------------------------ test conveniences
    def table_rows(self, name: str) -> dict[Any, tuple[Any, ...]]:
        """Stored rows of table `name` as `{primary key: row tuple}` (`{}` for a missing or view name); the key is the
        primary-key value (a tuple for a composite key). Never creates the table."""
        if name.startswith("sqlite_"):
            return {}
        found = self._run("SELECT type FROM sqlite_master WHERE name = ?", (name,))[0]
        if not found or found[0][0] != "table":
            return {}
        table = self._state.seen.get(name) or _canonical_tables().get(name)
        pk = tuple(table.primary) if table is not None and getattr(table, "primary", None) else (0,)
        out: dict[Any, tuple[Any, ...]] = {}
        for row in self._run(f"SELECT * FROM {name}")[0]:
            out[row[pk[0]] if len(pk) == 1 else tuple(row[i] for i in pk)] = row
        return out

    # ------------------------------------------------------------------ raw SQL gate
    @contextlib.contextmanager
    def _internal_sql(self) -> Iterator[None]:
        self._raw_depth += 1
        try:
            yield
        finally:
            self._raw_depth -= 1

    def _gate(self, op: str) -> None:
        if self._raw_depth == 0:
            raise UnsupportedOperation(
                f"MockDB does not execute raw SQL ({op}); use select_rows / insert_rows / update_rows / delete_rows or a named "
                "intent method - see DB_API.md 'Spec Write Primitives'"
            )

    def _raw_cursor(self, dictionary: bool = False) -> Any:
        self._gate("cursor()")
        return super()._raw_cursor(dictionary)

    def _raw_query(self, sql: str, params: Sequence[Any] | None = None, dictionary: bool = True) -> list[Any]:
        self._gate("query()")
        return super()._raw_query(sql, params, dictionary)

    def _raw_query_one(self, sql: str, params: Sequence[Any] | None = None, dictionary: bool = True) -> Any:
        self._gate("query_one()")
        return super()._raw_query_one(sql, params, dictionary)

    def _raw_query_iter(self, sql: str, params: Sequence[Any] | None = None, dictionary: bool = True, chunk_size: int = 2000) -> Iterator[Any]:
        self._gate("query_iter()")
        return super()._raw_query_iter(sql, params, dictionary, chunk_size)

    def _raw_execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        self._gate("execute()")
        return super()._raw_execute(sql, params)

    def _raw_executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
        self._gate("executemany()")
        return super()._raw_executemany(sql, params_seq)

    def _raw_delete(self, table: Any, where: str, params: Sequence[Any] | None = None) -> int:
        self._gate("delete()")
        return super()._raw_delete(table, where, params)

    # ------------------------------------------------------------------ creation on first use
    def _note(self, obj: Any, depth: int = 0) -> None:
        """Remember every `Table` inside a call argument (a table, a `(table, alias)` source, a sequence, a select)."""
        if obj is None or isinstance(obj, (str, bytes, int, float)):
            return
        if _is_table(obj):
            self._state.seen[obj.table_name] = obj
        elif depth > 2:
            return
        elif isinstance(obj, tuple) and len(obj) == 2 and isinstance(obj[1], str):
            self._note(obj[0], depth + 1)
        elif isinstance(obj, Mapping):
            for v in obj.values():
                self._note(v, depth + 1)
        elif isinstance(obj, (list, tuple)) and len(obj) <= 64:
            for v in obj:
                self._note(v, depth + 1)
        elif hasattr(obj, "source"):
            self._note(obj.source, depth + 1)
            self._note(getattr(obj, "joins", ()), depth + 1)

    def _materialize(self, name: str) -> bool:
        """Create the missing table or registry view `name` from what is known about it; False when nothing is."""
        if name in self._state.dropped:
            return False
        table = self._state.seen.get(name) or _canonical_tables().get(name)
        if table is not None:
            self._create_lenient(table)
            return True
        if name in ORDERED_VIEW_NAMES:
            with self._internal_sql():
                return self.ensure_views([name]).get(name, RuntimeError("no view")) is None
        return False

    def _run(self, sql: str, params: Sequence[Any] = (), *, many: bool = False) -> tuple[list[tuple[Any, ...]], int, list[str]]:
        for _ in range(24):
            try:
                return super()._run(sql, params, many=many)
            except MissingTableError as exc:
                found = _MISSING_RE.search(str(exc))
                if found is None or not self._materialize(found.group(1)):
                    raise
        return super()._run(sql, params, many=many)

    def _create_lenient(self, table: Any) -> None:
        for stmt in render_create_table_statements(_Lenient(table), "sqlite"):
            SQLiteDB._run(self, stmt)

    def create_table(self, tables: Sequence[Table] | Table) -> None:
        """Create tables that do not exist yet (an existing one is kept); the declared initial rows go into a table
        that is empty."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)
        for table in tables:
            self._state.dropped.discard(table.table_name)
            self._state.seen[table.table_name] = table
            if self._created(table.table_name):
                if table.initial_insert and not self._scalar(f"SELECT COUNT(*) FROM {table.table_name}"):
                    self.insert(table, table.initial_insert)
                continue
            self._create_lenient(table)
            if table.initial_insert:
                self.insert(table, table.initial_insert)

    def _created(self, name: str) -> bool:
        """True when table or view `name` exists in the SQLite catalog (not merely known)."""
        return bool(SQLiteDB._run(self, "SELECT 1 FROM sqlite_master WHERE name = ?", (name,))[0])

    def drop_tables(self, names: Sequence[str], *, ignore_fk: bool = False, if_exists: bool = True) -> None:
        """Drop tables; each name stays missing until `create_table()` brings it back."""
        for n in names:
            if not if_exists and n not in self.existing_tables([n]):
                raise MissingTableError(
                    f"Unknown table '{n}'; check the name against core/DBLayout.py - see DB_API.md 'DDL & Index Management'"
                )
        super().drop_tables(names, ignore_fk=ignore_fk, if_exists=True)
        self._state.dropped.update(names)

    def drop_view(self, view_name: str, if_exists: bool = True) -> None:
        with self._internal_sql():
            SQLiteDB.drop_view(self, view_name, if_exists)
        self._state.dropped.add(view_name)

    def create_view(self, view_name: str, select: Any, replace: bool = True) -> None:
        self._state.dropped.discard(view_name)
        with self._internal_sql():
            SQLiteDB.create_view(self, view_name, select, replace)

    def existing_tables(self, names: Sequence[str], *, include_views: bool = False) -> set[str]:
        """Created tables plus the canonical tables (and, with `include_views`, registry views) not dropped."""
        found = set(super().existing_tables(names, include_views=include_views))
        for n in names:
            if n in self._state.dropped or n in found:
                continue
            if n in _canonical_tables() or n in self._state.seen or (include_views and n in ORDERED_VIEW_NAMES):
                found.add(n)
        return found

    def table_stats(self, names: Sequence[str] | None = None) -> list[Any]:
        if names is not None:
            for n in names:
                self._ensure(n)
        return super().table_stats(names)

    def _ensure(self, name: str) -> None:
        """Create canonical / seen table `name` when it does not exist yet."""
        if not self._created(name):
            self._materialize(name)

    def column_exists(self, table: Table | str, column: str) -> bool:
        self._note(table)
        self._ensure(table.table_name if _is_table(table) else str(table))
        with self._internal_sql():
            return SQLiteDB.column_exists(self, table, column)

    def column_info(self, table: Table | str, column: str) -> Any:
        """Column description with the nullability the schema declares (the mock stores NOT NULL columns as nullable)."""
        self._note(table)
        name = table.table_name if _is_table(table) else str(table)
        self._ensure(name)
        info = super().column_info(table, column)
        if info is None:
            return None
        spec = self._state.added.get((name, column))
        if spec is None:
            schema = self._state.seen.get(name) or _canonical_tables().get(name)
            spec = dict(schema.init_columns).get(column) if schema is not None else None
        if spec is None:
            return info
        col_type = render_column_type(spec, "mariadb")
        octet = int(spec.length) if spec.type in (ColumnType.BINARY, ColumnType.VARBINARY) and spec.length else None
        return ColumnInfo(
            data_type=col_type.split("(")[0].split()[0].lower(), column_type=col_type, octet_length=octet, nullable=spec.nullable
        )

    def add_column(self, table_name: str, column_name: str, spec: ColumnSpec) -> bool:
        """Add a column (a NOT NULL column without default is stored nullable, like every non-key column)."""
        self._ensure(table_name)
        stored = dataclasses.replace(spec, nullable=True) if not spec.nullable and spec.default is NO_DEFAULT else spec
        with self._internal_sql():
            added = SQLiteDB.add_column(self, table_name, column_name, stored)
        if added:
            self._state.added[(table_name, column_name)] = spec
        return added

    def index_names(self, table: Table | str) -> set[str]:
        self._note(table)
        self._ensure(table.table_name if _is_table(table) else str(table))
        return super().index_names(table)

    def apply_server_tuning(
        self, settings: dict[str, Any] | None = None, *, tunnel_active: bool | None = None, mem_max_gb: float = 0.0
    ) -> list[str]:
        return []


def _wrap_noting(name: str, positions: tuple[int, ...]) -> Any:
    base = getattr(MockDB, name)

    def method(self: MockDB, *args: Any, **kwargs: Any) -> Any:
        for i in positions:
            if i < len(args):
                self._note(args[i])
        if "joins" in kwargs:
            self._note(kwargs["joins"])
        if "select" in kwargs:
            self._note(kwargs["select"])
        return base(self, *args, **kwargs)

    method.__name__ = name
    method.__doc__ = base.__doc__
    return method


for _name, _positions in _NOTING_ARGS.items():
    if _name not in ("column_exists", "column_info", "index_names"):  # these notice their table themselves
        setattr(MockDB, _name, _wrap_noting(_name, _positions))


def _wrap_internal(name: str) -> Any:
    base = getattr(MockDB, name)

    def method(self: MockDB, *args: Any, **kwargs: Any) -> Any:
        with self._internal_sql():
            return base(self, *args, **kwargs)

    method.__name__ = name
    method.__doc__ = base.__doc__
    return method


for _name in _INTERNAL_SQL:
    if _name not in ("create_view", "drop_view", "column_exists"):  # these open the internal scope themselves
        setattr(MockDB, _name, _wrap_internal(_name))

__all__ = ["MockDB", "MockReadSession"]
