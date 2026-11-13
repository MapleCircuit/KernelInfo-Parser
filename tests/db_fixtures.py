"""tests/db_fixtures.py - The one home of the database helpers tests use.

Everything a test needs to get a database, fill it, and serve it to the code under test lives here:

- `make_db(kind="mock"|"sqlite", *, tables=None, rows=None)`: a private in-memory database (`MockDB`, or
  `SQLiteDB(":memory:")` without FOREIGN KEY enforcement), optionally with tables created and rows inserted.
  `MockDB` creates canonical and `Table`-object tables on first use; `SQLiteDB` needs `tables` (or the canonical
  tables named in `rows`, which `make_db` creates).
- `patch_reader(db)`: makes the webapp `get_reader()` serve `db.reader()` (a context manager, and `start()` / `stop()`
  for patch lists; nests, stops in any order). `webapp/backend/database/pool.py` has no test hook; this `mock.patch`es
  its engine-selection seam `_reader_engine`.
- `DistDB`: in-memory SQLite with the distributed-parsing coordination tables and row helpers
  (`add_tasks`, `add_nodes`, `add_git_refs`, `tasks`, `nodes`, `task`).

- `requires_live_db(reason)` / `require_live_db(reason)`: mark (decorator on a test, test class or `setUpClass`) or
  guard (call) tests that read the real configured MariaDB. They skip with one line when `KIP_NO_LIVE_DB=1`
  (`python3 main.py -u --no-live-db`) or when the database does not answer (probed once per process).

The `MockDB` / `SQLiteDB` / `MariaDB` classes themselves stay importable from `db_engine` in tests
(`tests/test_db_boundary.py`); production code never imports them.
"""
from __future__ import annotations

import functools
import os
import time
import unittest
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from unittest import mock

from core import distributed
from core.DBLayout import TABLES, m_checkpoint_base, m_bridge_checkpoint, m_commit, m_git_ref, m_ingest_state, m_parse_node, m_parse_task, m_repository
from db_engine import MockDB, SQLiteDB
from db_engine.base import BaseDBEngine

_READER_SEAM = "webapp.backend.database.pool._reader_engine"


def make_db(
    kind: Literal["mock", "sqlite"] = "mock",
    *,
    tables: Sequence[Any] | None = None,
    rows: Mapping[str, Sequence[Mapping[str, Any]] | tuple[Sequence[str], Sequence[Sequence[Any]]]] | None = None,
) -> BaseDBEngine:
    """A fresh private in-memory database.

    `tables`: `Table` objects to create up front. `rows`: `{table name: [{column: value}, ...]}` or
    `{table name: (columns, row tuples)}`, inserted in mapping order (a name that is a canonical table of
    `core/DBLayout.py` is created first on `sqlite`)."""
    if kind == "mock":
        db: BaseDBEngine = MockDB(use_global=False)
    elif kind == "sqlite":
        db = SQLiteDB(":memory:", foreign_keys=False)
    else:
        raise ValueError(f"make_db kind must be 'mock' or 'sqlite', not {kind!r}")
    canonical = {t.table_name: t for t in TABLES}
    wanted = list(tables or ())
    if kind == "sqlite":
        wanted += [canonical[n] for n in (rows or {}) if n in canonical and canonical[n] not in wanted]
    if wanted:
        db.create_table(wanted)
    for name, spec in (rows or {}).items():
        if isinstance(spec, tuple) and len(spec) == 2 and not isinstance(spec[0], Mapping):
            columns, data = list(spec[0]), [tuple(r) for r in spec[1]]
            db.insert_rows(name, columns, data)
            continue
        by_columns: dict[tuple[str, ...], list[tuple[Any, ...]]] = {}
        for row in spec:
            by_columns.setdefault(tuple(row), []).append(tuple(row.values()))
        for columns, data in by_columns.items():
            db.insert_rows(name, list(columns), data)
    return db


class _ReaderPatch:
    """Context manager / `start()` + `stop()` pair serving `get_reader()` from one engine. Patches nest and may stop in
    any order: the most recently started engine that has not stopped serves, and the seam is restored once none is
    left."""

    _stack: list[Any] = []
    _patcher: Any = None

    def __init__(self, db: Any) -> None:
        self._db = db
        self._token: object | None = None

    def start(self) -> None:
        cls = _ReaderPatch
        self._token = (self._db,)
        cls._stack.append(self._token)
        if cls._patcher is None:
            cls._patcher = mock.patch(_READER_SEAM, side_effect=lambda: cls._stack[-1][0])
            cls._patcher.start()

    def stop(self) -> None:
        cls = _ReaderPatch
        if self._token is not None and self._token in cls._stack:
            cls._stack.remove(self._token)
        self._token = None
        if not cls._stack and cls._patcher is not None:
            cls._patcher.stop()
            cls._patcher = None

    def __enter__(self) -> _ReaderPatch:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def patch_reader(db: Any) -> _ReaderPatch:
    """Serve the webapp `get_reader()` from `db` (an engine instance or class) until stopped.

    Use as `with patch_reader(db):`, or `p = patch_reader(db); p.start(); self.addCleanup(p.stop)`. Patches nest and
    may stop in any order."""
    return _ReaderPatch(db)


DIST_TABLES = (m_parse_task, m_parse_node, m_ingest_state, m_git_ref, m_repository, m_commit, m_bridge_checkpoint, m_checkpoint_base)
_TASK_COLUMNS = [c[0] for c in m_parse_task.init_columns]


class DistDB(SQLiteDB):
    """In-memory SQLite database with the coordination tables; `tasks` / `nodes` read them back as dicts."""

    def __init__(self, full_schema: bool = False) -> None:
        super().__init__(":memory:", foreign_keys=False)
        self.create_table(list(TABLES if full_schema else DIST_TABLES))

    def add_tasks(self, *tasks: dict) -> DistDB:
        for t in tasks:
            row = {"network_id": 1, "chain_name": "", "chain_kind": "", "seq_order": 0, "ref_name": f"ref-{t['task_id']}",
                   "commit_id": 0, "base_ref_name": "", "base_commit_id": 0, "status": distributed.TASK_PENDING,
                   "worker_id": "", "lease_expires_at": 0, "retry_count": 0, "error_msg": "", **t}
            self.insert_rows("m_parse_task", list(row), [tuple(row.values())])
        return self

    def add_nodes(self, *nodes: dict) -> DistDB:
        for n in nodes:
            row = {"network_id": 1, "role": "worker", "hostname": "h", "pid": 1, "active_commit_id": 0,
                   "heartbeat_at": int(time.time()), **n}
            self.insert_rows("m_parse_node", list(row), [tuple(row.values())])
        return self

    def add_git_refs(self, *refs: dict) -> DistDB:
        """Refs of `network_id` (default 1); each network gets its repository row (repo_id = network_id)."""
        for r in refs:
            net = r.get("network_id", 1)
            self.insert_rows("m_repository", ["repo_id", "network_id", "name", "git_url"], [(net, net, f"repo{net}", "")],
                             on_conflict="ignore")
            self.insert_rows("m_git_ref", ["repo_id", "ref_type", "ref_name", "commit_id"],
                             [(net, r.get("ref_type", 1), r["ref_name"], r["commit_id"])])
        return self

    @property
    def tasks(self) -> list[dict]:
        return self.select_rows("m_parse_task", _TASK_COLUMNS, order_by=["task_id"], as_dict=True)

    @property
    def nodes(self) -> list[dict]:
        return self.select_rows("m_parse_node", ["node_id", "role", "heartbeat_at"], order_by=["node_id"], as_dict=True)

    def task(self, task_id: int) -> dict:
        return next(t for t in self.tasks if t["task_id"] == task_id)


NO_LIVE_DB_ENV = "KIP_NO_LIVE_DB"
_LIVE_DB_STATE: list[str | None] = []  # [] = not probed; [None] = reachable; [message] = unreachable


def require_live_db(reason: str = "reads the live MariaDB") -> None:
    """Skip (raise `unittest.SkipTest`) unless the configured database answers; always skips under `KIP_NO_LIVE_DB=1`."""
    if os.environ.get(NO_LIVE_DB_ENV) == "1":
        raise unittest.SkipTest(f"live MariaDB disabled ({NO_LIVE_DB_ENV}=1): {reason}")
    if not _LIVE_DB_STATE:
        try:
            from webapp.backend.database.pool import get_reader

            with get_reader() as reader:
                reader.select_one("m_repository", ["repo_id"])
            _LIVE_DB_STATE.append(None)
        except Exception as exc:
            _LIVE_DB_STATE.append(f"{type(exc).__name__}: {exc}"[:160])
    if _LIVE_DB_STATE[0] is not None:
        raise unittest.SkipTest(f"live MariaDB unreachable ({_LIVE_DB_STATE[0]}): {reason}")


def requires_live_db(reason: str = "reads the live MariaDB"):
    """Decorator for a test method or a test class (guards `setUpClass`)."""

    def wrap(obj):
        if isinstance(obj, type):
            original = obj.__dict__.get("setUpClass")
            inner = original.__func__ if original is not None else None

            @classmethod
            def setUpClass(cls):
                require_live_db(reason)
                if inner is not None:
                    inner(cls)
                else:
                    super(obj, cls).setUpClass()

            obj.setUpClass = setUpClass
            return obj

        @functools.wraps(obj)
        def guarded(*args, **kwargs):
            require_live_db(reason)
            return obj(*args, **kwargs)

        return guarded

    return wrap
