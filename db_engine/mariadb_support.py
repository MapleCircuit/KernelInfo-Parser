"""Shared pieces of the MariaDB driver: connector compatibility shims, tuning constants, row/binary helpers and the driver-error wrapper."""
from __future__ import annotations

import functools
import inspect
import logging
import re
import ssl
from typing import TYPE_CHECKING, Any, Iterator, Sequence
import mysql.connector

# Python 3.12 compatibility shim: mysql.connector calls ssl.wrap_socket which was removed in 3.12
if not hasattr(ssl, "wrap_socket"):
    def _compat_wrap_socket(
        sock: Any,
        keyfile: str | None = None,
        certfile: str | None = None,
        server_side: bool = False,
        cert_reqs: int = ssl.CERT_NONE,
        ssl_version: Any = None,
        ca_certs: str | None = None,
        do_handshake_on_connect: bool = True,
        suppress_ragged_eofs: bool = True,
        ciphers: str | None = None,
    ) -> Any:
        context = ssl.create_default_context() if not server_side else ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.check_hostname = False
        context.verify_mode = cert_reqs
        if ca_certs:
            context.load_verify_locations(ca_certs)
        if certfile:
            context.load_cert_chain(certfile, keyfile)
        if ciphers:
            context.set_ciphers(ciphers)
        return context.wrap_socket(
            sock,
            server_side=server_side,
            do_handshake_on_connect=do_handshake_on_connect,
            suppress_ragged_eofs=suppress_ragged_eofs,
        )

    ssl.wrap_socket = _compat_wrap_socket


def _patch_connector_statement_splitter() -> None:
    """Send every statement to the server unchanged (mysql-connector 9.x pure-Python cursor).

    `MySQLCursor.execute()` (and `executemany()`, which runs the built multi-row INSERT through it) passes the
    final SQL to `split_multi_statement()`. Whenever the text matches its `DELIMITER` pattern anywhere -
    including inside an escaped string value such as the kernel comment `/* Transmit delimiter */` - the
    splitter strips "comments" with a literal tracker that ignores backslash escapes: after a value holding
    `\\'` it treats the next `#`, `--` or `/*` inside a later value as a comment and cuts the statement, so
    the server answers 1064 (`... near ''//' at line 1`). This driver never sends DELIMITER scripts or uses
    `map_results`, so the statement is passed through as one unmodified partition.
    """
    try:
        import mysql.connector.cursor as _connector_cursor
        from mysql.connector._scripting import MySQLScriptPartition
    except ImportError:
        return
    original = getattr(_connector_cursor, "split_multi_statement", None)
    if original is None or getattr(original, "_kip_passthrough", False):
        return
    from collections import deque

    def _passthrough_split(sql_code: bytes, map_results: bool = False) -> Iterator[Any]:
        if map_results:
            yield from original(sql_code=sql_code, map_results=map_results)
            return
        yield MySQLScriptPartition(single_stmts=deque([]), mappable_stmt=sql_code)

    _passthrough_split._kip_passthrough = True  # type: ignore[attr-defined]
    _connector_cursor.split_multi_statement = _passthrough_split


_patch_connector_statement_splitter()

from db_engine.errors import (
    translate_mysql_error,
)
from db_engine.spec import (
    ColumnSpec,
)

if TYPE_CHECKING:
    pass

MAX_ALLOWED_PACKET = 1073741824
MAX_JOIN_TABLES = 50
CHUNK_JOIN_SIZE = 30
MAX_CANDIDATE_BATCH = 500

# apply_server_tuning(): the I/O ceiling goes first (innodb_io_capacity may not exceed it); sizes and I/O
# capacities are only ever raised, never lowered below what the server already runs with
_TUNING_FIRST = ("innodb_io_capacity_max",)
_TUNING_GROW_ONLY = frozenset({"innodb_buffer_pool_size", "innodb_log_file_size", "innodb_io_capacity", "innodb_io_capacity_max"})
_SIZE_SUFFIXES = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_server_setting(value: Any) -> int | str:
    """Server variable value from config: an int, a size with a K/M/G/T suffix (`"8G"`), or a plain string."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    m = re.fullmatch(r"(\d+)\s*([KMGTkmgt])?", text)
    if m:
        return int(m.group(1)) * _SIZE_SUFFIXES.get((m.group(2) or "").upper(), 1)
    return text

logger = logging.getLogger("db_engine.DBHandling")  # the name the driver logged under before the split

_BINARY_TYPE_WORDS = ("BINARY", "BLOB")
_TABLE_BY_NAME: dict[str, Any] | None = None


def _coerce_bytes(val: Any) -> Any:
    """bytes for a binary column value: bytearray/memoryview, or the str mysql-connector decoded from valid UTF-8."""
    if isinstance(val, (bytearray, memoryview)):
        return bytes(val)
    if isinstance(val, str):
        return val.encode("utf-8")
    return val


def _is_binary_type(sql_type: Any) -> bool:
    if isinstance(sql_type, ColumnSpec):
        return sql_type.is_binary
    text = str(sql_type).upper()
    return any(word in text for word in _BINARY_TYPE_WORDS)


def _table_for_name(name: str) -> Any:
    """Canonical `Table` of `name` from `core.DBLayout.TABLES` (None for unknown names and views)."""
    global _TABLE_BY_NAME
    if _TABLE_BY_NAME is None:
        try:
            import core.DBLayout as dbl

            _TABLE_BY_NAME = {t.table_name: t for t in getattr(dbl, "TABLES", ())}
        except Exception:
            _TABLE_BY_NAME = {}
    return _TABLE_BY_NAME.get(name)


def _binary_names_of(sources: Sequence[Any]) -> frozenset[str]:
    """Names of the binary columns (BINARY / VARBINARY / BLOB family) of the tables behind `sources`."""
    names: set[str] = set()
    for src in sources:
        if isinstance(src, tuple):
            src = src[0]
        table = src if hasattr(src, "init_columns") else _table_for_name(str(src))
        for col in getattr(table, "init_columns", ()) or ():
            if _is_binary_type(col[1]):
                names.add(col[0])
    return frozenset(names)


def _wrap_driver_errors(fn: Any) -> Any:
    """Translate mysql-connector errors raised by `fn` to the neutral `db_engine.errors` classes."""
    if inspect.isgeneratorfunction(fn):

        @functools.wraps(fn)
        def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                yield from fn(*args, **kwargs)
            except mysql.connector.Error as exc:
                neutral = translate_mysql_error(exc)
                if neutral is None:
                    raise
                raise neutral from exc

        return gen_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except mysql.connector.Error as exc:
            neutral = translate_mysql_error(exc)
            if neutral is None:
                raise
            raise neutral from exc

    return wrapper




def _sort_rows(
    rows: list[Any],
    order_by: Sequence[str],
    col_names: list[str],
    as_dict: bool,
) -> list[Any]:
    """Sort rows in Python across chunks."""
    if not order_by or not rows:
        return rows
    result = list(rows)
    for order_spec in reversed(order_by):
        clean = order_spec.strip()
        desc = clean.startswith("-")
        col = clean[1:] if desc else clean
        base_col = col.rsplit(".", 1)[-1]

        def _get_val(r: Any) -> Any:
            if as_dict and isinstance(r, dict):
                return r.get(col, r.get(base_col))
            else:
                if col in col_names:
                    idx = col_names.index(col)
                elif base_col in col_names:
                    idx = col_names.index(base_col)
                else:
                    return None
                return r[idx] if idx < len(r) else None

        result.sort(key=lambda r: (_get_val(r) is None, _get_val(r)), reverse=desc)
    return result


def _normalize_binary(val: Any) -> Any:
    """Normalize bytearray or memoryview to bytes."""
    if isinstance(val, (bytearray, memoryview)):
        return bytes(val)
    return val


def _normalize_row(
    row: Any,
    as_dict: bool,
    binary: frozenset[str] = frozenset(),
    col_names: Sequence[str] = (),
) -> Any:
    """Normalize BINARY fields of a row: `bytes` for bytearray/memoryview and for `binary` columns decoded to str."""
    if as_dict and isinstance(row, dict):
        return {k: (_coerce_bytes(v) if k in binary else _normalize_binary(v)) for k, v in row.items()}
    if isinstance(row, (tuple, list)):
        if binary and col_names:
            return tuple(
                _coerce_bytes(v) if (i < len(col_names) and col_names[i] in binary) else _normalize_binary(v)
                for i, v in enumerate(row)
            )
        return tuple(_normalize_binary(v) for v in row)
    return row


