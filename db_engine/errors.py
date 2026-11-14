"""db_engine/errors.py - Neutral exception hierarchy raised by `db_engine` drivers.

Callers outside `db_engine/` catch these classes only; each driver maps its native errors to them at its boundary
(`raise NeutralError(...) from native_error`).
"""
from __future__ import annotations

from typing import Any


class DBError(Exception):
    """Base class for every database-abstraction error raised by `db_engine`."""


class UnsupportedOperation(DBError, NotImplementedError):
    """The active backend does not implement the requested operation (for example raw SQL on `MockDB`).

    Messages name the replacement API and the `db_engine/DB_API.md` section.
    """


class SpecError(DBError, ValueError):
    """A query / write specification is malformed (ambiguous condition, unknown alias, cyclic assignments, ...).

    Also a `ValueError`; the message names the fix and the `db_engine/DB_API.md` section that documents the form.
    """


class DuplicateKeyError(DBError, ValueError):
    """A write violated a primary-key or unique-index constraint (also a `ValueError`, the historic insert contract)."""


class MissingTableError(DBError, LookupError):
    """The statement names a table or view that does not exist."""


class ConnectionLostError(DBError, ConnectionError):
    """The connection to the database was lost or could not be acquired (also a `ConnectionError`)."""


class PacketTooLargeError(DBError):
    """A statement exceeded the server's maximum packet size; callers may retry with smaller batches."""


class ConfigurationError(DBError):
    """The connection settings are incomplete (for example no user or password): fix the configuration, retrying cannot help."""


class ReadOnlyViolation(DBError):
    """A write was attempted through a read-only session (the message names the writer to use)."""


class DeadlockError(DBError):
    """A transaction was rolled back by the database due to a lock deadlock or lock wait timeout; callers may retry."""


# MySQL / MariaDB client error numbers
_DUP_ERRNOS = frozenset({1062, 1586})
_MISSING_TABLE_ERRNOS = frozenset({1146, 1051})
_PACKET_ERRNOS = frozenset({1153, 1154})
_DEADLOCK_ERRNOS = frozenset({1213, 1205})
_CONN_ERRNOS = frozenset({2002, 2003, 2006, 2013, 2055, 1927})
_CONN_CLASS_NAMES = frozenset({"InterfaceError", "PoolError"})


def translate_mysql_error(exc: BaseException) -> DBError | None:
    """Map a mysql-connector exception to its neutral `DBError`, or None when it has no neutral equivalent.

    The decision uses the error number first, the message second and the exception class last (duck-typed, so no
    driver import is needed). An `OperationalError` with an unknown number is treated as a lost connection only when
    it carries none of the statement-level numbers above.
    """
    raw_errno = getattr(exc, "errno", None)
    errno = raw_errno if isinstance(raw_errno, int) and raw_errno > 0 else None  # mysql-connector uses -1 for "none"
    text = str(exc)
    low = text.lower()
    if errno in _DUP_ERRNOS or (errno is None and "duplicate entry" in low):
        return DuplicateKeyError(text)
    if errno in _MISSING_TABLE_ERRNOS or (errno is None and "doesn't exist" in low and "table" in low):
        return MissingTableError(f"{text}; create the table or check its name - see DB_API.md 'DDL & Index Management'")
    if errno in _PACKET_ERRNOS or "max_allowed_packet" in low:
        return PacketTooLargeError(text)
    if errno in _DEADLOCK_ERRNOS or (errno is None and ("deadlock found" in low or "lock wait timeout" in low)):
        return DeadlockError(text)
    if errno in _CONN_ERRNOS:
        return ConnectionLostError(text)
    names = {c.__name__ for c in type(exc).__mro__}
    if names & _CONN_CLASS_NAMES:
        return ConnectionLostError(text)
    if "OperationalError" in names and (errno is None or errno >= 2000):
        return ConnectionLostError(text)
    return None


def translate_sqlite_error(exc: BaseException) -> DBError | None:
    """Map a `sqlite3` exception to its neutral `DBError`, or None when it has no neutral equivalent."""
    text = str(exc)
    low = text.lower()
    names = {c.__name__ for c in type(exc).__mro__}
    if "IntegrityError" in names and ("unique constraint" in low or "primary key" in low):
        return DuplicateKeyError(f"Duplicate entry ({text})")
    if "no such table" in low or "no such view" in low:
        return MissingTableError(f"{text}; create the table or check its name - see DB_API.md 'DDL & Index Management'")
    if "too many sql variables" in low or "too big" in low or "statement too long" in low:
        return PacketTooLargeError(text)
    if "readonly database" in low or "read-only" in low:
        return ReadOnlyViolation(f"{text}; open the writer engine instead - see DB_API.md 'Webapp Read Session & Query Primitives'")
    if "closed database" in low or "unable to open database" in low or "disk i/o error" in low:
        return ConnectionLostError(text)
    return None


def raise_translated(exc: BaseException, *types: Any) -> None:
    """Raise the neutral equivalent of `exc` (chained) when it is an instance of one of `types`; else return."""
    if types and not isinstance(exc, types):
        return
    neutral = translate_mysql_error(exc)
    if neutral is not None:
        raise neutral from exc


__all__ = [
    "DBError",
    "UnsupportedOperation",
    "SpecError",
    "DuplicateKeyError",
    "MissingTableError",
    "ConnectionLostError",
    "PacketTooLargeError",
    "ReadOnlyViolation",
    "DeadlockError",
    "translate_mysql_error",
    "translate_sqlite_error",
    "raise_translated",
]
