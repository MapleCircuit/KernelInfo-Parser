"""Connection lifecycle, session setup, transactions, raw cursors, id blocks and the reader pool of the MariaDB driver."""
from __future__ import annotations

import contextlib
import os
import re
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, Iterator, Sequence, Self
from types import TracebackType
import mysql.connector
from mysql.connector.constants import ClientFlag
from core.globalstuff import (
    SafeDataType,
    JoinsType,
)

from db_engine.base import (
    CAP_CONCURRENT_CONNECTIONS,
    CAP_PERSISTENT,
    CAP_SERVER_TUNING,
    ProbeResult,
)
from db_engine.errors import (
    ConfigurationError,
    ConnectionLostError,
    ReadOnlyViolation,
    UnsupportedOperation,
    translate_mysql_error,
)

if TYPE_CHECKING:
    from core.TableHandling import Table
from db_engine.mariadb_support import _TUNING_FIRST, _TUNING_GROW_ONLY, logger, parse_server_setting

# A connection used within this many seconds is trusted without a COM_PING round trip
IDLE_PING_S = 30.0
# Client errors meaning the connection itself is gone (server gone away, lost during query, lost connection)
_LOST_ERRNOS = frozenset({2006, 2013, 2055})


def _is_connection_lost(exc: BaseException) -> bool:
    """True when `exc` means the connection is gone (a read may then reconnect and run again)."""
    if isinstance(exc, (mysql.connector.InterfaceError, ConnectionError)):
        return True
    if getattr(exc, "errno", None) in _LOST_ERRNOS:
        return True
    return isinstance(exc, OSError)



class MariaDBConnection:
    """Connection lifecycle, session setup, transactions, raw cursors, id blocks and the reader pool of the MariaDB driver."""

    # commit_tables_parallel(dedicated=True) only uses its own connections, so a background thread may run it
    supports_background_commit = True

    _in_txn = False

    # time.monotonic() of the last statement known to have succeeded (0.0: check the connection before use)
    _last_ok = 0.0

    # Process that opened `cnx`; a forked child never sends on the inherited socket and opens its own connection
    _pid: int | None = None

    dialect = "mariadb"

    base_capabilities = frozenset({CAP_PERSISTENT, CAP_SERVER_TUNING, CAP_CONCURRENT_CONNECTIONS})

    default_socket_paths = ("/run/mysqld/mysqld.sock", "/var/run/mysqld/mysqld.sock", "/tmp/mysql.sock")

    read_only = False

    def __init__(self) -> None:
        """Initialize database connection parameters and open session.

        Args:
            None (reads parameters from environment: DB_USER, DB_PASSWORD, DB_NAME, DB_HOST, DB_PORT).

        Process:
            1. Reads environment configuration defaults.
            2. Opens SQL connection socket via `connect_sql()`.
            3. Creates cursor instance handle.
            4. Invokes `_init_session()` to configure session parameters.

        Outputs:
            None.
        """
        self.user = os.getenv("DB_USER", "")
        self.password = os.getenv("DB_PASSWORD", "")
        self.db_name = os.getenv("DB_NAME", "test")
        self.host = os.getenv("DB_HOST")
        self.port = int(os.getenv("DB_PORT", "3306"))
        self.ssl_disabled = os.getenv("DB_SSL_DISABLED", "").lower() in ("true", "1", "yes")
        ssh_tunnel_active = os.getenv("SSH_TUNNEL_ENABLED", "").lower() in ("true", "1", "yes")
        env_socket = os.getenv("DB_SOCKET") or os.getenv("MYSQL_UNIX_PORT") or os.getenv("MYSQL_SOCKET")
        if not ssh_tunnel_active and env_socket not in ("off", "disabled", "none", "false"):
            if env_socket:
                self.unix_socket = env_socket
            elif not self.host or self.host in ("127.0.0.1", "localhost"):
                for cand in self.default_socket_paths:
                    if os.path.exists(cand):
                        self.unix_socket = cand
                        break
                else:
                    self.unix_socket = None
            else:
                self.unix_socket = None
        else:
            self.unix_socket = None
        # Inside transaction(): a dropped connection fails the transaction instead of silently reconnecting
        self._in_txn = False
        self.cnx = self.connect_sql()
        self._cur = self.cnx.cursor()
        self._select_sql_cache: dict[tuple[int, tuple[bool, ...]], str] = {}
        self._preload_sql_cache: dict[tuple[int, tuple[int, ...] | None, bool], str] = {}
        self._view_sql_cache: dict[tuple[JoinsType, tuple[bool, ...]], str] = {}
        self._next_id_sql_cache: dict[int, str] = {}
        self._init_session()
        self._pid = os.getpid()
        self._last_ok = time.monotonic()

    def apply_server_tuning(
        self,
        settings: dict[str, Any] | None = None,
        *,
        tunnel_active: bool | None = None,
        mem_max_gb: float = 0.0,
    ) -> list[str]:
        """Raise global server variables with `SET GLOBAL`, capping the buffer pool of a local server.

        Args:
            settings: `{variable_name: value}` (sizes may carry a K/M/G/T suffix).
            tunnel_active: Whether the server is reached through an SSH tunnel. `None` skips the local-RAM logic;
                `False` with a local host caps `innodb_buffer_pool_size` at a quarter of this machine's RAM.
            mem_max_gb: Parser memory guard (GB); when it plus the effective buffer pool exceeds the RAM of a local
                server a `memory guard` warning note is appended.

        Outputs:
            Notes as of `_apply_server_variables()`; classify with `db_engine.base.tuning_note_is_warning()`.
        """
        settings = dict(settings or {})
        local = False
        ram = 0
        pool: Any = None
        if tunnel_active is not None:
            host = getattr(self, "host", None) or "localhost"
            local = host in ("localhost", "127.0.0.1", "::1") and not tunnel_active
            try:
                ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            except (ValueError, OSError, AttributeError):
                ram = 0
            pool = parse_server_setting(settings["innodb_buffer_pool_size"]) if "innodb_buffer_pool_size" in settings else None
            if local and ram and isinstance(pool, int) and pool > ram // 4:
                pool = (ram // 4) // (128 * 1024 * 1024) * (128 * 1024 * 1024)
                settings["innodb_buffer_pool_size"] = pool
        notes = self._apply_server_variables(settings)
        if local and ram and isinstance(pool, int):
            actual = self.server_variable("innodb_buffer_pool_size")
            if actual is not None and str(actual).isdigit():
                pool = int(actual)
            if mem_max_gb > 0 and mem_max_gb * 1024**3 + pool > ram:
                notes.append(
                    f"memory guard ({mem_max_gb:.0f} GB) plus the InnoDB buffer pool ({pool / 1024**3:.0f} GB) "
                    f"exceed this machine's {ram / 1024**3:.0f} GB of RAM; lower parser.mem_max if the parser ever gets close"
                )
        return notes

    def _apply_server_variables(self, settings: dict[str, Any]) -> list[str]:
        """Raise global server variables to `settings` with `SET GLOBAL` (needs the SUPER privilege).

        Args:
            settings: `{variable_name: value}`; sizes may carry a K/M/G/T suffix. Variables in
                `_TUNING_GROW_ONLY` are never lowered.

        Outputs:
            One note per variable that changed or could not be changed (unchanged variables are omitted).
            A user without the SUPER privilege gets a single `server tuning skipped: ...` note instead.
            Runtime changes last until the server restarts.
        """
        notes: list[str] = []
        self.check_if_connected()
        for name in sorted(settings, key=lambda n: (n not in _TUNING_FIRST, n)):
            if not re.fullmatch(r"[a-z0-9_]+", name):
                notes.append(f"{name}: skipped (not a server variable name)")
                continue
            want = parse_server_setting(settings[name])
            try:
                self._cur.execute(f"SELECT @@GLOBAL.{name}")
                row = self._cur.fetchone()
            except mysql.connector.Error as e:
                notes.append(f"{name}: not available on this server ({getattr(e, 'msg', e)})")
                continue
            current = row[0] if row else None
            if isinstance(current, (bytes, bytearray)):
                current = current.decode()
            if isinstance(current, str) and current.isdigit():
                current = int(current)
            if isinstance(want, str) and isinstance(current, str) and want.lower() == current.lower():
                continue
            if current == want or (
                name in _TUNING_GROW_ONLY and isinstance(current, int) and isinstance(want, int) and current >= want
            ):
                continue
            try:
                self._cur.execute(f"SET GLOBAL {name} = %s", (want,))
                self._cur.execute(f"SELECT @@GLOBAL.{name}")
                row = self._cur.fetchone()
            except mysql.connector.Error as e:
                if getattr(e, "errno", None) in (1227, 1044, 1045):
                    # ER_SPECIFIC_ACCESS_DENIED_ERROR: the user may not SET GLOBAL anything, so stop trying
                    # (e.g. a shared/remote server); the server keeps its own configuration
                    notes.append(
                        f"server tuning skipped: user '{self.user}' lacks the SUPER privilege needed for SET GLOBAL "
                        "(set database.server_tuning values in the server's my.cnf instead)"
                    )
                    break
                notes.append(f"{name}: kept {current} ({getattr(e, 'msg', e)})")
                continue
            now = row[0] if row else None
            if isinstance(now, int) and isinstance(want, int) and now < want:
                # e.g. innodb_buffer_pool_size beyond innodb_buffer_pool_size_max, which only a restart raises
                limit = " (raise it in the server configuration and restart)"
                if name == "innodb_buffer_pool_size":
                    limit = " (innodb_buffer_pool_size_max is fixed at server start: set the size in my.cnf and restart)"
                notes.append(f"{name}: capped at {now} instead of {want}{limit}")
            else:
                notes.append(f"{name}: {current} -> {now if now is not None else want}")
        return notes

    def _init_session(self) -> None:
        """Configure session parameters in a single round trip.

        Process:
            1. Sets `sql_mode = 'NO_AUTO_VALUE_ON_ZERO'`, disables per-session `foreign_key_checks` and
               `unique_checks`, and sets session timeouts (`wait_timeout`, `interactive_timeout`,
               `net_read_timeout`, `net_write_timeout`) in one `SET SESSION` statement (falling back to
               `sql_mode` alone if a restricted server rejects the optional variables).
            2. Commits session configuration.
        """
        try:
            self._cur.execute(
                "SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO', "
                "foreign_key_checks = 0, unique_checks = 0, "
                "wait_timeout = 28800, interactive_timeout = 28800, "
                "net_read_timeout = 3600, net_write_timeout = 3600;"
            )
        except mysql.connector.Error:
            self._cur.execute("SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO';")
        self.cnx.commit()

    def __enter__(self) -> Self:
        """Enter context manager scope (`with G.DB() as db:`).

        Args:
            None.

        Process:
            Verifies active connection via `check_if_connected()`.

        Outputs:
            Self (Active database driver instance).
        """
        self.check_if_connected()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Exit context manager scope, rolling back on error and releasing resources.

        Args:
            exception_type: Type of exception raised within context block (or None).
            exception_value: Exception instance (or None).
            exception_traceback: Exception traceback (or None).

        Process:
            If an unhandled exception occurred, rolls back pending transaction.

        Outputs:
            None.
        """
        if exception_type is not None and getattr(self, "cnx", None):
            try:
                self.cnx.rollback()
            except Exception:
                pass

    def close(self) -> None:
        """Safely close cursor and connection handles.

        Args:
            None.

        Process:
            Closes cursor and connection sockets, suppressing any teardown errors.

        Outputs:
            None.
        """
        if getattr(self, "_cur", None):
            try:
                self._cur.close()
            except Exception:
                pass
            self._cur = None
        if getattr(self, "cnx", None):
            try:
                self.cnx.close()
            except Exception:
                pass
            self.cnx = None

    def connect_sql(self) -> Any:
        """Establish SQL connection with environment detection.

        Args:
            None.

        Process:
            Detects environment (explicit DB_HOST, Docker container files `/.dockerenv`
            or `/proc/self/cgroup`, or localhost fallback) and opens MySQL connection.

        Outputs:
            Native MySQL connection socket handle.
        """
        if not self.user or not self.password:
            raise ConfigurationError(
                "Database credentials are missing: set database.user and database.password in config.json "
                "(or DB_USER / DB_PASSWORD); no default account is assumed - see DB_API.md 'Lifecycle, Session & Health'."
            )
        if self.unix_socket and os.path.exists(self.unix_socket):
            conn_kwargs: dict[str, Any] = {
                "user": self.user,
                "password": self.password,
                "database": self.db_name,
                "autocommit": True,
                "client_flags": [ClientFlag.FOUND_ROWS],  # affected rows = matched rows, like SQLite / MockDB
                "unix_socket": self.unix_socket,
            }
            if self.ssl_disabled:
                conn_kwargs["ssl_disabled"] = True
            try:
                return mysql.connector.connect(**conn_kwargs)
            except mysql.connector.Error as e:
                logger.warning(
                    f"[DB Socket] Failed connecting via Unix socket '{self.unix_socket}' ({e}); "
                    "falling back to TCP..."
                )

        if self.host:
            mysql_host = self.host
        elif os.path.exists("/.dockerenv"):
            mysql_host = "host.docker.internal"
        elif os.path.exists("/proc/self/cgroup"):
            try:
                with open("/proc/self/cgroup", "r", encoding="utf-8") as f:
                    cgroup_content = f.read()
                if "docker" in cgroup_content:
                    mysql_host = "host.docker.internal"
                else:
                    mysql_host = "localhost"
            except (OSError, UnicodeDecodeError):
                mysql_host = "localhost"
        else:
            mysql_host = "localhost"

        conn_kwargs = {
            "host": mysql_host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.db_name,
            "autocommit": True,
            "client_flags": [ClientFlag.FOUND_ROWS],  # affected rows = matched rows, like SQLite / MockDB
        }
        if self.ssl_disabled:
            conn_kwargs["ssl_disabled"] = True

        return mysql.connector.connect(**conn_kwargs)

    def check_if_connected(self) -> None:
        """Make sure the instance has a usable connection, reconnecting if it was dropped.

        Process:
            A connection used successfully within `IDLE_PING_S` seconds is trusted as is (no round trip); an
            idle one is checked with one `ping()`. A missing or dead connection is replaced by `_reconnect()`
            (new connection, new cursor, `_init_session()`; 3 attempts). A forked child drops the connection
            it inherited (without closing it, which would end the parent's session) and opens its own.
            Inside `transaction()` nothing is checked: a dead connection makes the next statement fail, the
            server has rolled the transaction back and the caller retries all of it.

        Raises:
            ConnectionLostError: If no connection can be opened after 3 attempts, or there is none inside a
                transaction.
        """
        pid = getattr(self, "_pid", None)
        if pid is not None and pid != os.getpid():
            self.cnx = None
            self._cur = None
            self._pid = None
        in_txn = getattr(self, "_in_txn", False)
        if getattr(self, "cnx", None) is not None:
            if in_txn:
                return
            if getattr(self, "_cur", None) is not None and time.monotonic() - self._last_ok < IDLE_PING_S:
                return
            try:
                self.cnx.ping(reconnect=False)
                if getattr(self, "_cur", None) is None:
                    self._cur = self.cnx.cursor()
                self._last_ok = time.monotonic()
                return
            except Exception:
                self.close()
        if in_txn:
            raise ConnectionLostError("Database connection lost inside a transaction")
        self._reconnect()

    def _reconnect(self) -> None:
        """Replace the connection with a new one (new cursor, `_init_session()`), up to 3 attempts."""
        self.close()
        for attempt in range(3):
            logger.warning("No SQL connection. Reconnection attempt %d/3...", attempt + 1)
            try:
                self.cnx = self.connect_sql()
                self._cur = self.cnx.cursor()
                self._init_session()
                self._pid = os.getpid()
                self._last_ok = time.monotonic()
                return
            except ConfigurationError:
                raise
            except Exception as e:
                if attempt == 2:
                    raise ConnectionLostError(f"Failed to reconnect to database after 3 attempts: {e}") from e
                time.sleep(0.5 * (attempt + 1))

    def _read(self, fn: Callable[[], Any]) -> Any:
        """Run the read `fn()` (which uses `self.cnx` / `self._cur` when called).

        Outside `transaction()` a read whose connection was lost reconnects and runs once more (reads are
        idempotent); inside one the error propagates like for any statement.
        """
        self.check_if_connected()
        try:
            result = fn()
        except Exception as exc:
            if getattr(self, "_in_txn", False) or not _is_connection_lost(exc):
                raise
            logger.warning("[DB] Connection lost during a read (%s); reconnecting and reading again", exc)
            self._reconnect()
            result = fn()
        self._last_ok = time.monotonic()
        return result

    def _fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """First row of the read `sql` on the shared cursor (with the reconnect-and-retry of `_read()`)."""

        def run() -> Any:
            self._cur.execute(sql, params)
            return self._cur.fetchone()

        return self._read(run)

    def _fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        """All rows of the read `sql` on the shared cursor (with the reconnect-and-retry of `_read()`)."""

        def run() -> list[Any]:
            self._cur.execute(sql, params)
            return self._cur.fetchall()

        return self._read(run)

    def _lease_writer(self) -> Any:
        """A dedicated writer connection (a separate `MariaDB` instance) for one table of `commit_tables_parallel()`."""
        from db_engine.DBHandling import MariaDB  # the composed driver class (mixins cannot import it at load)

        return MariaDB()

    def _release_writer(self, writer: Any, healthy: bool) -> None:
        """Give back a connection of `_lease_writer()`."""
        writer.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Self]:
        """Run the block's `insert()`/`update()` calls as one transaction, committed on exit.

        Inside it the per-chunk reconnect-and-retry is disabled: a lost connection rolls the whole transaction
        back on the server, so it fails (`ConnectionError` or the driver error) and the caller retries all of it.
        """
        self.check_if_connected()
        try:
            self.cnx.autocommit = False
        except mysql.connector.Error as exc:
            self._last_ok = 0.0
            raise (translate_mysql_error(exc) or exc) from exc
        self._in_txn = True
        try:
            yield self
            try:
                self.cnx.commit()
            except mysql.connector.Error as exc:
                raise (translate_mysql_error(exc) or exc) from exc
        except BaseException:
            self._last_ok = 0.0  # the next call checks the connection before using it
            try:
                self.cnx.rollback()
            except Exception:
                pass
            raise
        finally:
            self._in_txn = False
            try:
                self.cnx.autocommit = True
            except Exception:
                self.close()  # a connection that cannot leave the transaction is not reused

    def _require_writable(self) -> None:
        """Raise `ReadOnlyViolation` on a driver that backs a read-only session."""
        if self.read_only:
            raise ReadOnlyViolation(
                "This database session is read-only; write through the parser's writer engine (G.DB) "
                "- see DB_API.md 'Webapp Read Session & Query Primitives'"
            )

    def commit(self) -> None:
        """Commit the pending work of `transaction()`; outside one every statement is already committed (autocommit)."""
        if getattr(self, "_in_txn", False) and getattr(self, "cnx", None):
            self.cnx.commit()

    def rollback(self) -> None:
        """Roll back pending transaction on connection."""
        if hasattr(self, "cnx") and self.cnx:
            try:
                self.cnx.rollback()
            except Exception:
                pass

    def _raw_cursor(self, dictionary: bool = False) -> Any:
        """Lease a cursor handle on the underlying connection."""
        self.check_if_connected()
        return self.cnx.cursor(dictionary=dictionary)

    def _raw_query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> list[dict[str, Any] | tuple[SafeDataType, ...]]:
        """Execute a SQL query and return all matching records."""

        def run() -> list[Any]:
            with contextlib.closing(self.cnx.cursor(dictionary=dictionary)) as cursor:
                cursor.execute(sql, tuple(params or ()))
                rows = cursor.fetchall()
                return list(rows) if rows else []

        return self._read(run)

    def _raw_query_one(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> dict[str, Any] | tuple[SafeDataType, ...] | None:
        """Execute a SQL query and return a single record or None."""

        def run() -> Any:
            with contextlib.closing(self.cnx.cursor(dictionary=dictionary)) as cursor:
                cursor.execute(sql, tuple(params or ()))
                return cursor.fetchone()

        return self._read(run)

    def _raw_query_iter(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
        chunk_size: int = 2000,
    ) -> Iterator[dict[str, Any] | tuple[SafeDataType, ...]]:
        """Stream query result rows in batches of chunk_size (the first batch has the reconnect-and-retry of `_read()`)."""

        def start() -> tuple[Any, list[Any]]:
            cursor = self.cnx.cursor(dictionary=dictionary)
            try:
                cursor.execute(sql, tuple(params or ()))
                return cursor, cursor.fetchmany(chunk_size)
            except BaseException:
                with contextlib.suppress(Exception):
                    cursor.close()
                raise

        cursor, chunk = self._read(start)
        with contextlib.closing(cursor):
            while chunk:
                yield from chunk
                chunk = cursor.fetchmany(chunk_size)

    def _raw_execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Execute a DML or DDL SQL statement (committed by autocommit outside `transaction()`)."""
        self._require_writable()
        self.check_if_connected()
        with contextlib.closing(self.cnx.cursor()) as cursor:
            cursor.execute(sql, tuple(params or ()))
            if getattr(cursor, "with_rows", False):
                try:
                    cursor.fetchall()
                except Exception:
                    pass
            rowcount = cursor.rowcount
        self._last_ok = time.monotonic()
        return max(0, rowcount)

    def _raw_executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
        """Execute a parameterized SQL statement repeatedly (committed by autocommit outside `transaction()`)."""
        if not params_seq:
            return 0
        self._require_writable()
        self.check_if_connected()
        with contextlib.closing(self.cnx.cursor()) as cursor:
            cursor.executemany(sql, [tuple(p) for p in params_seq])
            if getattr(cursor, "with_rows", False):
                try:
                    cursor.fetchall()
                except Exception:
                    pass
            rowcount = cursor.rowcount
        self._last_ok = time.monotonic()
        return max(0, rowcount)

    # -------------------------------------------------------------------------
    # Sequence Reservation Override (§9)
    # -------------------------------------------------------------------------
    def reserve_id_block(
        self,
        table: Table,
        block_size: int,
        commit_id: int = 0,
    ) -> tuple[int, int]:
        """Reserve an atomic block of primary key IDs [start_id, end_id] for target table.

        Process (up to 3 attempts, each on a checked connection; a lost connection is replaced):
            1. Reads `COALESCE(MAX(pk), 0) + 1` of the table with a plain consistent read (autocommit, no locks, so
               it never waits for a commit appending to the same table).
            2. In one transaction: upserts that floor into `m_table_sequence` (`next_id = GREATEST(next_id, floor)`,
               which X-locks the sequence row, so concurrent callers are serialized), reads `next_id`, advances it
               by `block_size` and, with `commit_id > 0`, records the range in `m_ingest_id_range`. A failure of
               that last insert is logged and ignored unless it is a deadlock (1213, InnoDB rolled the whole
               transaction back) or a lost connection, which retry the attempt.

        Raises:
            UnsupportedOperation: Inside `transaction()` (the reservation commits on its own).
        """
        table_id = getattr(table, "table_id", None)
        if table_id is None or not getattr(self, "cnx", None):
            return super().reserve_id_block(table, block_size, commit_id)
        if getattr(self, "_in_txn", False):
            raise UnsupportedOperation(
                "reserve_id_block() commits its own transaction and cannot run inside transaction(); reserve the "
                "ids before entering it - see DB_API.md 'Sequence Tracking & Single-Row CRUD'"
            )
        pk_col = table.init_columns[0][0]
        for attempt in range(3):
            try:
                self.check_if_connected()
                with contextlib.closing(self.cnx.cursor()) as cur:
                    cur.execute(f"SELECT COALESCE(MAX(`{pk_col}`), 0) + 1 FROM `{table.table_name}`")  # noqa: S608
                    row = cur.fetchone()
                    floor = int(row[0]) if row and row[0] is not None else 1
                    self.cnx.autocommit = False
                    try:
                        cur.execute(
                            "INSERT INTO m_table_sequence (seq_table_id, next_id) VALUES (%s, %s) "
                            "ON DUPLICATE KEY UPDATE next_id = GREATEST(next_id, VALUES(next_id))",
                            (table_id, floor),
                        )
                        cur.execute(
                            "SELECT next_id FROM m_table_sequence WHERE seq_table_id = %s FOR UPDATE",
                            (table_id,),
                        )
                        row = cur.fetchone()
                        start_id = int(row[0]) if row and row[0] is not None else floor
                        end_id = start_id + block_size - 1
                        cur.execute(
                            "UPDATE m_table_sequence SET next_id = %s WHERE seq_table_id = %s",
                            (end_id + 1, table_id),
                        )
                        if commit_id > 0:
                            try:
                                cur.execute(
                                    "INSERT INTO m_ingest_id_range (commit_id, marked_table_id, start_id, end_id) "
                                    "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE end_id = GREATEST(end_id, VALUES(end_id))",
                                    (commit_id, table_id, start_id, end_id),
                                )
                            except mysql.connector.Error as e_range:
                                if getattr(e_range, "errno", None) == 1213 or _is_connection_lost(e_range):
                                    raise
                                logger.warning(f"[DB] Error recording ingest ID range for table {table_id}: {e_range}")
                        self.cnx.commit()
                    finally:
                        self.cnx.autocommit = True
                self._last_ok = time.monotonic()
                return (start_id, end_id)
            except Exception as e:
                self._last_ok = 0.0  # the next attempt checks the connection (and replaces a dead one)
                try:
                    self.cnx.rollback()
                except Exception:
                    pass
                if _is_connection_lost(e):
                    self.close()
                logger.warning(f"[DB] reserve_id_block attempt {attempt + 1}/3 failed for {table.table_name}: {e}")
                if attempt == 2:
                    raise
                time.sleep(0.2 * (attempt + 1))
        raise AssertionError("unreachable")

    @classmethod
    def probe_connection(cls, cfg: Mapping[str, Any]) -> ProbeResult:
        """Connect with the `database` section `cfg`, run `SELECT 1` and report server version and latency.

        Honors `unix_socket` ("auto" probes `default_socket_paths` for a local host; "off"/"none"/"disabled"/"false"
        forces TCP; a path is used when it exists) and falls back to TCP `host`:`port` when the socket connection
        fails and a host is configured. Recognized keys: user, password, database, host, port, unix_socket,
        ssl_disabled, timeout (seconds, default 3). Never raises; `ProbeResult.target` names what was connected to.
        """
        user = cfg.get("user")
        db_name = cfg.get("database")
        if not user or not db_name:
            return ProbeResult(False, error=f"Database credentials incomplete (user={user!r}, database={db_name!r})")
        host = cfg.get("host")
        try:
            port = int(cfg.get("port", 3306))
        except (TypeError, ValueError):
            port = 3306
        sock = cfg.get("unix_socket")
        resolved: str | None = None
        if sock == "auto":
            if not host or host in ("127.0.0.1", "localhost"):
                resolved = next((c for c in cls.default_socket_paths if os.path.exists(c)), None)
        elif sock and str(sock).lower() not in ("off", "disabled", "none", "false") and os.path.exists(str(sock)):
            resolved = str(sock)
        kwargs: dict[str, Any] = {
            "user": user,
            "password": cfg.get("password", ""),
            "database": db_name,
            "connection_timeout": float(cfg.get("timeout", 3) or 3),
            "autocommit": True,
        }
        if cfg.get("ssl_disabled"):
            kwargs["ssl_disabled"] = True
        tcp = {"host": host or "127.0.0.1", "port": port}
        t0 = time.perf_counter()
        cnx = None
        target = ""
        try:
            if resolved:
                try:
                    cnx = mysql.connector.connect(**kwargs, unix_socket=resolved)
                    target = resolved
                except mysql.connector.Error:
                    if not host:
                        raise
            if cnx is None:
                cnx = mysql.connector.connect(**kwargs, **tcp)
                target = f"{tcp['host']}:{tcp['port']}"
            cur = cnx.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            version = cnx.get_server_info() or ""
            cur.close()
            cnx.close()
        except Exception as exc:
            return ProbeResult(False, latency_ms=round((time.perf_counter() - t0) * 1000.0, 2), error=str(exc), target=target or f"{tcp['host']}:{tcp['port']}")
        return ProbeResult(True, version, round((time.perf_counter() - t0) * 1000.0, 2), "", target)

    @classmethod
    def reader(cls) -> Any:
        """Context manager yielding a pooled read-only `ReadSession` (needs `db_engine.pool.configure_pool()`)."""
        from db_engine.pool import get_db_reader

        return get_db_reader()

    @classmethod
    def warm_up(cls) -> None:
        """Build the shared reader connection pool (starting the configured SSH tunnel first)."""
        from db_engine.pool import get_db_pool

        get_db_pool()

    @classmethod
    def shutdown(cls) -> None:
        """Close the shared reader connection pool."""
        from db_engine.pool import close_db_pool

        close_db_pool()
