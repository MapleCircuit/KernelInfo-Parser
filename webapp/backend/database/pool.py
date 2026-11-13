"""webapp/backend/database/pool.py - Encapsulated Pool Bridge to db_engine."""
from __future__ import annotations

from typing import Any, ContextManager

from db_engine import configure_pool

from webapp.backend.config import get_backend_db_config

# The pool receives its connection configuration from the webapp, never the reverse.
configure_pool(get_backend_db_config)


def _reader_engine() -> Any:
    """The engine whose `reader()` serves requests: the configured `database.engine`.

    This is the one engine-selection seam; tests replace it with `mock.patch` (`tests/db_fixtures.py: patch_reader`)."""
    from db_engine import get_db_engine

    return get_db_engine(get_backend_db_config().get("engine"))


def get_reader() -> ContextManager[Any]:
    """Read-only session of the configured database engine: the one entry point of every webapp service.

    `with get_reader() as reader:` yields an object with the `db_engine` read API (`select_rows`, `select_one`,
    `select_values`, `select_value`, `count_rows`, `row_exists`, `aggregate`, catalog reads and the intent reads of
    `db_engine/read_intents.py`); rows are dicts by default and writes raise `ReadOnlyViolation`."""
    return _reader_engine().reader()


__all__ = ["get_reader"]
