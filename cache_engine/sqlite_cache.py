"""cache_engine/sqlite_cache.py - SQLite implementation of the cache engine (cache_engine/CACHE_API.md).

One database file per space under ``<cache root>/<namespace>/<name>.sqlite3``, WAL journal, one connection per
thread (concurrent readers) and one write lock per space; other processes are serialised by SQLite itself. This is
the only module of the cache engine that imports ``sqlite3``.
"""
from __future__ import annotations

import itertools
import contextlib
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .base import (BaseCacheEngine, CacheSpace, CacheStore, Condition, Eq, FieldType, In, IsNull, Key, Prefix, Range,
                   Record, StoreSpec, Where, as_conditions, check_namespace)
from .errors import (CacheBusyError, CacheCorruptError, CacheError, CacheSchemaError, CacheUnavailableError)

logger = logging.getLogger(__name__)

_SQL_TYPES = {FieldType.INT: "INTEGER", FieldType.FLOAT: "REAL", FieldType.TEXT: "TEXT", FieldType.BYTES: "BLOB"}
IN_CHUNK = 500          # values per IN list (statements stay far below SQLite's variable limit)
FETCH = 2000
BUSY_TIMEOUT_S = 60


def _q(name: str) -> str:
    return '"' + name + '"'


def translate_error(exc: BaseException) -> CacheError:
    """Map a ``sqlite3`` / ``OSError`` failure to the neutral exception classes."""
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, OSError):
        return CacheUnavailableError(msg)
    if isinstance(exc, sqlite3.OperationalError):
        if "locked" in low or "busy" in low:
            return CacheBusyError(msg)
        if "unable to open" in low or "readonly" in low or "disk" in low or "full" in low:
            return CacheUnavailableError(msg)
        if "malformed" in low or "corrupt" in low:
            return CacheCorruptError(msg)
    if isinstance(exc, sqlite3.DatabaseError) and not isinstance(exc, (sqlite3.OperationalError, sqlite3.IntegrityError,
                                                                      sqlite3.ProgrammingError)):
        return CacheCorruptError(msg)  # "file is not a database", "malformed disk image"
    if isinstance(exc, sqlite3.ProgrammingError):
        return CacheSchemaError(msg)
    return CacheError(msg)


@contextlib.contextmanager
def _translated() -> Iterator[None]:
    try:
        yield
    except CacheError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise translate_error(exc) from exc


def _prefix_bounds(value: Any) -> tuple[Any, Any]:
    """(lower, exclusive upper or None) of the values starting with ``value``."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        lo = bytes(value)
        n = int.from_bytes(lo, "big") + 1
        hi = n.to_bytes(len(lo), "big") if lo and n < 1 << (8 * len(lo)) else None
        return lo, hi
    if isinstance(value, str):
        return value, value + "\U0010ffff"
    raise CacheSchemaError(f"Prefix needs a text or bytes value, got {type(value).__name__}")


class _SQLiteStore(CacheStore):
    def __init__(self, space: "_SQLiteSpace", spec: StoreSpec) -> None:
        self.space = space
        self.spec = spec
        self._cols = ", ".join(_q(n) for n in spec.field_names)
        self._marks = ", ".join("?" * len(spec.fields))
        self._key = spec.key

    # ------------------------------------------------------------------ SQL helpers
    def _check_field(self, name: str) -> str:
        self.spec.field(name)  # raises CacheSchemaError
        return _q(name)

    def _where(self, conds: list[Condition], chunk: tuple[int, Sequence[Any]] | None = None) -> tuple[str, list]:
        parts: list[str] = []
        params: list[Any] = []
        for i, c in enumerate(conds):
            col = self._check_field(c.field)
            if isinstance(c, Eq):
                if c.value is None:
                    parts.append(f"{col} IS NULL")
                else:
                    parts.append(f"{col} = ?")
                    params.append(c.value)
            elif isinstance(c, In):
                values = chunk[1] if chunk is not None and chunk[0] == i else list(c.values)
                if not values:
                    parts.append("0")
                else:
                    parts.append(f"{col} IN ({','.join('?' * len(values))})")
                    params.extend(values)
            elif isinstance(c, Range):
                for op, v in ((">=", c.ge), (">", c.gt), ("<=", c.le), ("<", c.lt)):
                    if v is not None:
                        parts.append(f"{col} {op} ?")
                        params.append(v)
            elif isinstance(c, Prefix):
                lo, hi = _prefix_bounds(c.value)
                parts.append(f"{col} >= ?")
                params.append(lo)
                if hi is not None:
                    parts.append(f"{col} < ?")
                    params.append(hi)
            elif isinstance(c, IsNull):
                parts.append(f"{col} IS NOT NULL" if c.negate else f"{col} IS NULL")
            else:
                raise CacheSchemaError(f"unknown condition: {c!r}")
        return (" WHERE " + " AND ".join(parts)) if parts else "", params

    def _chunks(self, conds: list[Condition], single_call: bool) -> Iterator[tuple[int, Sequence[Any]] | None]:
        """Chunking plan of the (at most one) oversized IN list."""
        big = [i for i, c in enumerate(conds) if isinstance(c, In) and len(c.values) > IN_CHUNK]
        if not big:
            yield None
            return
        if len(big) > 1 or single_call:
            raise CacheSchemaError("an In list longer than %d cannot be combined with order_by/limit or another long In" % IN_CHUNK)
        i = big[0]
        values = list(conds[i].values)
        for j in range(0, len(values), IN_CHUNK):
            yield i, values[j:j + IN_CHUNK]

    def _key_tuple(self, key: Key) -> tuple:
        if not self._key:
            raise CacheSchemaError(f"store {self.spec.name!r} has no key")
        if len(self._key) == 1:
            return (key,)
        if not isinstance(key, tuple) or len(key) != len(self._key):
            raise CacheSchemaError(f"store {self.spec.name!r}: key must be a tuple of {len(self._key)} values")
        return key

    def _key_where(self) -> str:
        return " AND ".join(f"{_q(k)} = ?" for k in self._key)

    def _key_in(self, n: int) -> str:
        if len(self._key) == 1:
            return f"{_q(self._key[0])} IN ({','.join('?' * n)})"
        row = "(" + ",".join("?" * len(self._key)) + ")"
        return f"({', '.join(_q(k) for k in self._key)}) IN (VALUES {','.join([row] * n)})"

    def _key_chunks(self, keys: Iterable[Key]) -> Iterator[tuple[list[Key], list]]:
        width = len(self._key) or 1
        step = max(1, IN_CHUNK // width)
        it = iter(keys)
        while True:
            batch = list(itertools.islice(it, step))
            if not batch:
                return
            if width == 1:
                if not self._key:
                    raise CacheSchemaError(f"store {self.spec.name!r} has no key")
                params = batch
            else:
                if not all(isinstance(k, tuple) and len(k) == width for k in batch):
                    raise CacheSchemaError(f"store {self.spec.name!r}: key must be a tuple of {width} values")
                params = list(itertools.chain.from_iterable(batch))
            yield batch, params

    # ------------------------------------------------------------------ reads
    def get(self, key: Key) -> Record | None:
        with _translated():
            row = self.space._read().execute(
                f"SELECT {self._cols} FROM {_q(self.spec.name)} WHERE {self._key_where()}", self._key_tuple(key)).fetchone()
        return tuple(row) if row else None

    def get_many(self, keys: Iterable[Key]) -> dict[Key, Record]:
        out: dict[Key, Record] = {}
        positions = [self.spec.field_names.index(k) for k in self._key]
        with _translated():
            conn = self.space._read()
            for batch, params in self._key_chunks(keys):
                sql = f"SELECT {self._cols} FROM {_q(self.spec.name)} WHERE {self._key_in(len(batch))}"
                for row in conn.execute(sql, params):
                    rec = tuple(row)
                    out[rec[positions[0]] if len(positions) == 1 else tuple(rec[p] for p in positions)] = rec
        return out

    def scan(self, where: Where = None, *, columns: Sequence[str] | None = None,
             order_by: Sequence[str] = (), limit: int | None = None) -> Iterator[tuple]:
        conds = as_conditions(where)
        cols = ", ".join(self._check_field(c) for c in columns) if columns else self._cols
        order = ""
        if order_by:
            order = " ORDER BY " + ", ".join(
                (self._check_field(o[1:]) + " DESC") if o.startswith("-") else self._check_field(o) for o in order_by)
        elif not self.spec.key:
            order = " ORDER BY rowid"
        tail = order + (f" LIMIT {int(limit)}" if limit is not None else "")
        plan = list(self._chunks(conds, single_call=bool(order_by) or limit is not None))
        return self._scan(cols, conds, plan, tail)

    def _scan(self, cols: str, conds: list[Condition], plan: list, tail: str) -> Iterator[tuple]:
        for chunk in plan:
            where_sql, params = self._where(conds, chunk)
            with _translated():
                cur = self.space._read().execute(f"SELECT {cols} FROM {_q(self.spec.name)}{where_sql}{tail}", params)
            while True:
                with _translated():
                    rows = cur.fetchmany(FETCH)
                if not rows:
                    break
                yield from (tuple(r) for r in rows)

    def count(self, where: Where = None) -> int:
        return int(self._aggregate("COUNT(*)", where) or 0)

    def sum(self, field: str, where: Where = None) -> int | float:
        return self._aggregate(f"COALESCE(SUM({self._check_field(field)}), 0)", where) or 0

    def max(self, field: str, where: Where = None) -> Any:
        return self._aggregate(f"MAX({self._check_field(field)})", where)

    def _aggregate(self, expr: str, where: Where) -> Any:
        conds = as_conditions(where)
        plan = list(self._chunks(conds, single_call=False))
        total: Any = None
        for chunk in plan:
            where_sql, params = self._where(conds, chunk)
            with _translated():
                row = self.space._read().execute(f"SELECT {expr} FROM {_q(self.spec.name)}{where_sql}", params).fetchone()
            value = row[0] if row else None
            if len(plan) == 1:
                return value
            if value is not None:
                # chunked IN: only COUNT/SUM combine (MAX is not chunked meaningfully but also folds by max)
                total = value if total is None else (max(total, value) if expr.startswith("MAX") else total + value)
        return total

    # ------------------------------------------------------------------ writes
    def put(self, record: Record) -> None:
        self.put_many([record])

    def put_many(self, records: Iterable[Record], *, replace: bool = True) -> int:
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        sql = f"{verb} INTO {_q(self.spec.name)} ({self._cols}) VALUES ({self._marks})"
        with _translated(), self.space._write() as conn:
            before = conn.total_changes
            conn.executemany(sql, records)
            return conn.total_changes - before

    def delete(self, key: Key) -> bool:
        with _translated(), self.space._write() as conn:
            return conn.execute(f"DELETE FROM {_q(self.spec.name)} WHERE {self._key_where()}", self._key_tuple(key)).rowcount > 0

    def delete_many(self, keys: Iterable[Key]) -> int:
        n = 0
        with _translated(), self.space._write() as conn:
            for batch, params in self._key_chunks(keys):
                n += conn.execute(f"DELETE FROM {_q(self.spec.name)} WHERE {self._key_in(len(batch))}", params).rowcount
        return n

    def delete_where(self, where: Where) -> int:
        conds = as_conditions(where)
        n = 0
        with _translated(), self.space._write() as conn:
            for chunk in self._chunks(conds, single_call=False):
                where_sql, params = self._where(conds, chunk)
                n += conn.execute(f"DELETE FROM {_q(self.spec.name)}{where_sql}", params).rowcount
        return n

    def update_where(self, where: Where, values: Mapping[str, Any]) -> int:
        if not values:
            return 0
        for name in values:
            self._check_field(name)
            if name in self._key:
                raise CacheSchemaError(f"cannot update key field {name!r}")
        conds = as_conditions(where)
        sets = ", ".join(f"{_q(n)} = ?" for n in values)
        n = 0
        with _translated(), self.space._write() as conn:
            for chunk in self._chunks(conds, single_call=False):
                where_sql, params = self._where(conds, chunk)
                n += conn.execute(f"UPDATE {_q(self.spec.name)} SET {sets}{where_sql}", [*values.values(), *params]).rowcount
        return n

    def clear(self) -> None:
        with _translated(), self.space._write() as conn:
            conn.execute(f"DELETE FROM {_q(self.spec.name)}")


def _create_sql(spec: StoreSpec) -> list[str]:
    cols = [f"{_q(f.name)} {_SQL_TYPES[f.type]}{'' if f.nullable else ' NOT NULL'}" for f in spec.fields]
    cols_sql = ", ".join(cols)
    if spec.key:
        cols_sql += ", PRIMARY KEY (" + ", ".join(_q(k) for k in spec.key) + ")"
    for group in spec.unique:
        cols_sql += ", UNIQUE (" + ", ".join(_q(g) for g in group) + ")"
    tail = " WITHOUT ROWID" if spec.clustered else ""
    out = [f"CREATE TABLE IF NOT EXISTS {_q(spec.name)} ({cols_sql}){tail}"]
    for group in spec.indexes:
        out.append(f"CREATE INDEX IF NOT EXISTS {_q(spec.name + '_' + '_'.join(group))} ON {_q(spec.name)} ("
                   + ", ".join(_q(g) for g in group) + ")")
    return out


class _SQLiteSpace(CacheSpace):
    def __init__(self, engine: "SQLiteCacheEngine", namespace: str, name: str, path: Path, stores: Sequence[StoreSpec]) -> None:
        self.engine = engine
        self.namespace, self.name = namespace, name
        self.path = path
        self.specs = tuple(stores)
        self._tls = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._wlock = threading.RLock()
        self._closed = False
        self._stores = {s.name: _SQLiteStore(self, s) for s in stores}
        with _translated():
            conn = self._read()
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            for spec in stores:
                for sql in _create_sql(spec):
                    conn.execute(sql)
                have = [r[1] for r in conn.execute(f"PRAGMA table_info({_q(spec.name)})")]
                if have != list(spec.field_names):
                    raise CacheSchemaError(
                        f"{path}: store {spec.name!r} holds fields {have}, declared {list(spec.field_names)}; "
                        "version the space name or discard it")

    # ------------------------------------------------------------------ connections / transactions
    def _read(self) -> sqlite3.Connection:
        if self._closed:
            raise CacheError(f"cache space {self.namespace}/{self.name} is closed")
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_S, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._tls.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """The thread's connection inside a write transaction (joined when ``batch()`` is active)."""
        conn = self._read()
        with self._wlock:
            if getattr(self._tls, "depth", 0) > 0:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextlib.contextmanager
    def batch(self) -> Iterator[None]:
        with _translated(), self._write():
            self._tls.depth = getattr(self._tls, "depth", 0) + 1
            try:
                yield
            finally:
                self._tls.depth -= 1

    # ------------------------------------------------------------------ CacheSpace
    def store(self, name: str) -> CacheStore:
        try:
            return self._stores[name]
        except KeyError:
            raise CacheSchemaError(f"cache space {self.namespace}/{self.name} declares no store {name!r}") from None

    def get_meta(self, key: str) -> str | None:
        with _translated():
            row = self._read().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with _translated(), self._write() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    def delete_meta(self, key: str) -> None:
        with _translated(), self._write() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    def clear(self) -> None:
        with _translated(), self._write() as conn:
            for name in self._stores:
                conn.execute(f"DELETE FROM {_q(name)}")
            conn.execute("DELETE FROM meta")

    def compact(self) -> None:
        with _translated(), self._wlock:
            self._read().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(str(self.path) + suffix).stat().st_size
            except OSError:
                continue
        return total

    def close(self) -> None:
        with self._conns_lock:
            conns, self._conns = self._conns, []
        self._closed = True
        for conn in conns:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self.engine._forget(self)

    def discard(self) -> None:
        self.close()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(str(self.path) + suffix).unlink()

    @property
    def location(self) -> str:
        return str(self.path)


class SQLiteCacheEngine(BaseCacheEngine):
    """``cache.engine = "sqlite"``. ``root`` fixes the cache directory; by default it is resolved on every open
    (``cache_engine.cache_root()``: ``KERNELINFO_CACHE_DIR`` / ``cache.dir`` / ``~/.cache/kernelinfo``)."""

    name = "sqlite"

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root).expanduser() if root is not None else None
        self._spaces: dict[str, _SQLiteSpace] = {}
        self._lock = threading.Lock()

    def _path(self, namespace: str, name: str) -> Path:
        check_namespace(namespace)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "index"
        if self._root is not None:
            root = self._root
        else:
            from . import cache_root
            root = cache_root()
        return root / namespace / f"{safe}.sqlite3"

    def open(self, namespace: str, name: str, stores: Sequence[StoreSpec]) -> CacheSpace:
        path = self._path(namespace, name)
        key = str(path)
        with self._lock:
            space = self._spaces.get(key)
            if space is not None:
                if space.specs != tuple(stores):
                    raise CacheSchemaError(f"cache space {namespace}/{name} is already open with another schema")
                return space
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CacheUnavailableError(str(exc)) from exc
            space = _SQLiteSpace(self, namespace, name, path, stores)
            self._spaces[key] = space
            return space

    def exists(self, namespace: str, name: str) -> bool:
        try:
            return self._path(namespace, name).exists()
        except OSError:
            return False

    def location(self, namespace: str, name: str) -> str:
        return str(self._path(namespace, name))

    def _forget(self, space: _SQLiteSpace) -> None:
        with self._lock:
            if self._spaces.get(str(space.path)) is space:
                del self._spaces[str(space.path)]

    def close(self) -> None:
        with self._lock:
            spaces = list(self._spaces.values())
        for space in spaces:
            space.close()
