"""core/config.py - Unified Configuration Management for KernelInfo-Parser.

Loads, merges, and synchronizes configuration options from JSON config files,
environment variables, and built-in defaults across both parser orchestration
and web application services with strict precedence:
    CLI Arguments > Environment Variables > config.json > Built-in Defaults
"""
from __future__ import annotations

import contextlib
import copy
import fnmatch
import json
import logging
import os
import re
import stat
import subprocess
from pathlib import Path
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SSH_TUNNEL_CONFIG: dict[str, Any] = {
    "enabled": False,
    "host": "",
    "port": 22,
    "user": "",
    "password": "",
    "key_file": "",
    "remote_host": "127.0.0.1",
    "remote_port": 0,
    "local_port": 0,
    "strict_host_key_checking": "accept-new",
    "connect_timeout": 10,
}



def resolve_unix_socket(socket_path: str | None = None, host: str | None = None) -> str | None:
    """Resolve an existing Unix domain socket path for local MariaDB connections.

    - If explicit socket_path is provided and not in ('auto', ''):
      Checks if the path exists and is a Unix domain socket (stat.S_ISSOCK). Returns it if valid.
    - If socket_path is disabled ('off', 'disabled', 'none', 'false', False):
      Returns None (forces TCP connection).
    - If socket_path is 'auto' (or empty/None) and host is local ('localhost', '127.0.0.1', ''):
      Probes candidate standard sockets: /run/mysqld/mysqld.sock, /var/run/mysqld/mysqld.sock, /tmp/mysql.sock.
      Returns the first valid socket path found, or None if none exist.
    """
    if socket_path is False or str(socket_path).strip().lower() in ("off", "disabled", "none", "false"):
        return None

    if socket_path and str(socket_path).strip().lower() not in ("auto", ""):
        candidate = Path(str(socket_path).strip()).resolve()
        try:
            if candidate.exists() and stat.S_ISSOCK(candidate.stat().st_mode):
                return str(candidate)
        except OSError:
            pass
        return None

    normalized_host = (host or "").strip().lower()
    if normalized_host in ("localhost", "127.0.0.1", ""):
        from db_engine import default_socket_paths

        for candidate_str in default_socket_paths():
            candidate = Path(candidate_str)
            try:
                if candidate.exists() and stat.S_ISSOCK(candidate.stat().st_mode):
                    return str(candidate)
            except OSError:
                continue

    return None

DEFAULT_CONFIG: dict[str, Any] = {
    "database": {
        "host": "127.0.0.1",
        "port": 3306,
        # No default account: a missing user or password is reported when the connection is made
        "user": "",
        "password": "",
        "database": "test",
        # SQLite engine only: database file (":memory:" for a private in-memory database); required for a file database (a bare "database" name is not a path)
        "path": "",
        "timeout": 10,
        "engine": "mariadb",
        "unix_socket": "auto",
        "ssl_disabled": False,
        # Raised once per pipeline run with SET GLOBAL (needs SUPER; lasts until the server restarts; sizes are
        # never lowered). The buffer pool is capped at a quarter of RAM when the server runs on this machine.
        # An empty object disables it.
        "server_tuning": {
            "innodb_buffer_pool_size": "8G",
            "innodb_log_file_size": "2G",
            "innodb_flush_log_at_trx_commit": 2,
            "innodb_io_capacity": 1000,
        },
        "ssh_tunnel": {
            "enabled": False,
            "host": "",
            "port": 22,
            "user": "",
            "password": "",
            "key_file": "",
            "remote_host": "127.0.0.1",
            "remote_port": 3306,
            "local_port": 0,
            "strict_host_key_checking": "accept-new",
            "connect_timeout": 10,
        },
    },
    # Local caches of derived data (cache_engine/CACHE_API.md): engine name ("ram" or "sqlite") and base
    # directory (empty: the KERNELINFO_CACHE_DIR environment variable, else ~/.cache/kernelinfo)
    "cache": {"engine": "ram", "dir": ""},
    "webapp": {
        "host": "0.0.0.0",
        "port": 8000,
        "reload": True,
        # Disk cache of derived data under KERNELINFO_CACHE_DIR (webapp/API.md section 2.14)
        "cache": {"max_mb": 1024},
        # Background jobs: worker threads per process (webapp/API.md section 2.15)
        "jobs": {"workers": 2},
        # Build the commit relation and Kbuild object indexes at startup (webapp/API.md section 2.12)
        "prewarm_indexes": False,
        # Repository scripts the webapp may run (webapp/API.md section 2.18): off by default. checkpatch.pl
        # is taken only from `ref` ("latest-release" or a release tag), never from a caller's revision;
        # `sandbox` optionally names a wrapper command (e.g. "bwrap") the run goes through.
        "tools": {"checkpatch": {"enabled": False, "ref": "latest-release"}, "sandbox": ""},
        # Source index (webapp/API.md section 2.19): the facts of ingested blobs come from the database (written by
        # the parser, Step 6.9); these low-priority processes only extract blobs it lacks (revisions the parser never
        # ingested), into a SQLite store capped at max_mb (least recently used blobs dropped past it). Assembled
        # revisions are kept on disk (at most `revisions`), so a restart does not assemble them again.
        "source_index": {"workers": 2, "max_mb": 1024, "revisions": 8},
    },
    # CVE records: a local clone of the CVE Program's list (cvelistV5) or of the kernel CNA's vulns.git, kept
    # current by the operator (git pull). The parser ingests it into m_cve / m_cve_commit when its HEAD changed
    # (at the start of every run, or alone with --cve; core/API.md section 9); the webapp reads those tables
    # (webapp/API.md section 4.11). A relative path is under the project root; empty, or no clone there: off.
    "cve": {
        "repo_dir": "cvelistV5",
    },
    "parser": {
        "table_engine": "cached",
        "memory_mode": "normal",
        "mem_max": 60,
        "hugepages": "auto",
        # MB of preloaded rows held on the heap before they are packed into a shared-memory segment
        "preload_flush_mb": 512,
        # Ceiling (MB) on the size of one TE-cached table; a bigger one is read straight from the
        # database instead of being cached (0: no ceiling)
        "cache_max_table_mb": 0,
        # "tag": symbol definitions/references stored once per tag (core/SymbolRows.py); "blob": per blob
        "symbol_layout": "tag",
        # A version's checkpoint is kept as a delta of an earlier full one once the next version is ingested,
        # when it differs from it in at most this share of its files (core/checkpoints.py); 0 keeps every one full
        "checkpoint_delta_ratio": 0.3,
        # Processes extracting the functions/calls/ops tables of new C blobs (Step 6.9, m_blob_facts)
        "facts_workers": 4,
        "single_version": False,
        "include_pre_releases": True,
    },
    "mailing_list": {
        "storage_dir": "/srv/lore",
        "port": 8192,
        "batch_size": 50,
        "request_timeout": 180,
        "daemon_max_rss_mb": 1024,
        "daemon_max_concurrent": 4,
        "auto_start_daemon": True,
        "ssh_tunnel": {
            "enabled": False,
            "host": "",
            "port": 22,
            "user": "",
            "password": "",
            "key_file": "",
            "remote_host": "127.0.0.1",
            "remote_port": 8192,
            "local_port": 0,
            "strict_host_key_checking": "accept-new",
            "connect_timeout": 10,
        },
    },
    "ssh_tunnel": {
        "enabled": False,
        "host": "",
        "port": 22,
        "user": "",
        "password": "",
        "key_file": "",
        "remote_host": "127.0.0.1",
        "remote_port": 3306,
        "local_port": 0,
        "strict_host_key_checking": "accept-new",
        "connect_timeout": 10,
    },
    "workspace": {
        "repos_dir": "repos",
        "auto_detect_repos": True,
    },
    # Populated by sync_repositories() from Git repositories found under workspace.repos_dir
    "repositories": [],
}

# Keys every repositories[] entry carries once detected (see detect_repository())
REPOSITORY_KEYS = (
    "name",
    "path",
    "project",
    "default_branch",
    "git_url",
    "root_commit",
    "start_ref",
    "enabled",
    "branch_filter",
    "branches",
    "tag_views",
)

_ACTIVE_CONFIG: dict[str, dict[str, Any]] | None = None
_LOADED_CONFIG_PATH: Path | None = None
_EXPORTED_ENV_VARS: dict[str, str] = {}


def _get_user_env(key: str) -> str | None:
    """Return environment variable value only if set by user/shell, not by sync_environ."""
    val = os.getenv(key)
    if val is None:
        return None
    if key in _EXPORTED_ENV_VARS and val == _EXPORTED_ENV_VARS[key]:
        return None
    return val


def find_config_path(custom_path: str | Path | None = None) -> Path | None:
    """Resolve the path to the configuration file based on priority.

    1. Explicit custom_path argument (CLI flag -c / --config)
    2. Environment variable CONFIG_FILE or CONFIG_PATH
    3. Default repo-root config.json
    """
    if custom_path:
        p = Path(custom_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Specified configuration file not found: {p}")
        return p

    env_path = _get_user_env("CONFIG_FILE") or _get_user_env("CONFIG_PATH")
    if env_path:
        p = Path(env_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Configuration file from environment variable not found: {p}")
        return p

    default_file = REPO_ROOT / "config.json"
    if default_file.is_file():
        return default_file

    return None


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge source dictionary into target dictionary."""
    for key, value in source.items():
        if isinstance(value, dict) and key in target and isinstance(target[key], dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def _coerce_int(val: Any, default: int) -> int:
    """Safely coerce value to integer."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


MEMORY_MODES = ("normal", "low", "very_low")
_MEMORY_MODE_ALIASES = {"compact": "very_low", "very-low": "very_low", "verylow": "very_low"}


def normalize_memory_mode(val: Any) -> str:
    """Return the canonical memory mode for `val`: one of MEMORY_MODES.

    Accepts the aliases "compact" and "very-low" for "very_low"; any other unknown value is "normal".
    """
    mode = str(val or "").strip().lower()
    mode = _MEMORY_MODE_ALIASES.get(mode, mode)
    return mode if mode in MEMORY_MODES else "normal"


def _coerce_bool(val: Any, default: bool) -> bool:
    """Safely coerce value or string to boolean."""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    str_val = str(val).strip().lower()
    if str_val in ("true", "1", "yes", "on"):
        return True
    if str_val in ("false", "0", "no", "off"):
        return False
    return default


def sync_environ(cfg: dict[str, dict[str, Any]]) -> None:
    """Synchronize resolved database and server settings into os.environ.

    Ensures that low-level DB driver layers (which check DB_*),
    legacy webapp connection logic (which checks MYSQL_*), and spawned
    multiprocessing worker processes all observe the exact same configuration.
    """
    global _EXPORTED_ENV_VARS
    db_cfg = cfg.get("database", {})

    host = str(db_cfg.get("host") or "127.0.0.1")
    port = str(db_cfg.get("port") or 3306)
    user = str(db_cfg.get("user") or "")
    password = str(db_cfg.get("password") or "")
    database = str(db_cfg.get("database") or "test")
    timeout = str(db_cfg.get("timeout") or 10)

    # Resolve Unix domain socket for local connections when SSH tunnel is disabled
    ssh_tunnel_active = (db_cfg.get("ssh_tunnel") or cfg.get("ssh_tunnel", {})).get("enabled", False)
    raw_socket = db_cfg.get("unix_socket", "auto")
    resolved_socket = ""
    if not ssh_tunnel_active and raw_socket not in ("disabled", "none", "false", False):
        resolved_socket = resolve_unix_socket(raw_socket, host) or ""

    db_cfg["resolved_unix_socket"] = resolved_socket

    # Export both DB_* and MYSQL_* keys for full subsystem consistency
    vars_to_sync = {
        "DB_HOST": host,
        "MYSQL_HOST": host,
        "DB_PORT": port,
        "MYSQL_PORT": port,
        "DB_USER": user,
        "MYSQL_USER": user,
        "DB_PASSWORD": password,
        "MYSQL_PASSWORD": password,
        "DB_NAME": database,
        "MYSQL_DATABASE": database,
        "DB_PATH": str(db_cfg.get("path") or ""),
        "DB_TIMEOUT": timeout,
        "MYSQL_TIMEOUT": timeout,
        "DB_SOCKET": resolved_socket,
        "MYSQL_UNIX_PORT": resolved_socket,
        "MYSQL_SOCKET": resolved_socket,
        "DB_SSL_DISABLED": "true" if db_cfg.get("ssl_disabled") else "false",
        "MYSQL_SSL_DISABLED": "true" if db_cfg.get("ssl_disabled") else "false",
        "TE_HUGEPAGES": str(cfg.get("parser", {}).get("hugepages", "auto")),
        "SSH_TUNNEL_ENABLED": "true" if ssh_tunnel_active else "false",
    }
    for k, v in vars_to_sync.items():
        os.environ[k] = v
        _EXPORTED_ENV_VARS[k] = v


@contextlib.contextmanager
def scoped_environ(cfg: dict[str, dict[str, Any]]) -> Iterator[None]:
    """Export `cfg` with `sync_environ()` for the duration of the block, then restore the previous variables.

    Lets a caller open a database engine against a configuration other than the active one (the TUI's
    DB Content Viewer) without changing what the rest of the process reads afterwards.
    """
    global _EXPORTED_ENV_VARS
    saved_env = dict(os.environ)
    saved_exported = dict(_EXPORTED_ENV_VARS)
    sync_environ(copy.deepcopy(cfg))
    changed = [key for key in _EXPORTED_ENV_VARS if os.environ.get(key) != saved_env.get(key)]
    try:
        yield
    finally:
        for key in changed:
            if key in saved_env:
                os.environ[key] = saved_env[key]
            else:
                os.environ.pop(key, None)
        _EXPORTED_ENV_VARS = saved_exported


def load_config(config_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load configuration from JSON file, merge with defaults, and apply ENV overrides.

    Precedence:
        Environment Variables > config.json > Built-in Defaults
    """
    global _LOADED_CONFIG_PATH
    resolved_path = find_config_path(config_path)
    _LOADED_CONFIG_PATH = resolved_path

    merged: dict[str, dict[str, Any]] = copy.deepcopy(DEFAULT_CONFIG)

    # 1. Load and merge JSON file if present
    if resolved_path:
        try:
            with open(resolved_path, "r", encoding="utf-8") as f:
                file_data = json.load(f)
            if not isinstance(file_data, dict):
                raise ValueError(f"Root JSON element must be an object, got {type(file_data).__name__}")
            # Backward compatibility: migrate legacy top-level ssh_tunnel to database.ssh_tunnel if needed
            if "ssh_tunnel" in file_data and isinstance(file_data["ssh_tunnel"], dict):
                db_data = file_data.setdefault("database", {})
                if isinstance(db_data, dict) and "ssh_tunnel" not in db_data:
                    db_data["ssh_tunnel"] = copy.deepcopy(file_data["ssh_tunnel"])
            _deep_merge(merged, file_data)
            logger.info("Loaded configuration from %s", resolved_path)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to parse JSON configuration file at '{resolved_path}': {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Error reading configuration file at '{resolved_path}': {exc}"
            ) from exc

    # 2. Apply Docker default host if running inside a container and host was not explicitly set
    if os.path.exists("/.dockerenv") and not resolved_path:
        merged["database"]["host"] = "host.docker.internal"

    # 3. Apply Environment Variable overrides (ENV > JSON file)
    db_sec = merged["database"]

    if env_db_host := (_get_user_env("DB_HOST") or _get_user_env("MYSQL_HOST")):
        db_sec["host"] = env_db_host
    if env_db_port := (_get_user_env("DB_PORT") or _get_user_env("MYSQL_PORT")):
        db_sec["port"] = _coerce_int(env_db_port, db_sec["port"])
    else:
        db_sec["port"] = _coerce_int(db_sec.get("port"), 3306)

    if env_db_user := (_get_user_env("DB_USER") or _get_user_env("MYSQL_USER")):
        db_sec["user"] = env_db_user
    if env_db_pass := (_get_user_env("DB_PASSWORD") or _get_user_env("MYSQL_PASSWORD")):
        db_sec["password"] = env_db_pass
    if env_db_name := (_get_user_env("DB_NAME") or _get_user_env("MYSQL_DATABASE")):
        db_sec["database"] = env_db_name
    if env_db_timeout := (_get_user_env("DB_TIMEOUT") or _get_user_env("MYSQL_TIMEOUT")):
        db_sec["timeout"] = _coerce_int(env_db_timeout, db_sec["timeout"])
    else:
        db_sec["timeout"] = _coerce_int(db_sec.get("timeout"), 10)

    if env_db_engine := _get_user_env("DB_ENGINE"):
        db_sec["engine"] = env_db_engine

    if env_db_socket := (_get_user_env("DB_SOCKET") or _get_user_env("MYSQL_UNIX_PORT") or _get_user_env("MYSQL_SOCKET")):
        db_sec["unix_socket"] = env_db_socket

    if env_db_ssl_disabled := (_get_user_env("DB_SSL_DISABLED") or _get_user_env("MYSQL_SSL_DISABLED")):
        db_sec["ssl_disabled"] = _coerce_bool(env_db_ssl_disabled, db_sec.get("ssl_disabled", False))
    else:
        db_sec["ssl_disabled"] = _coerce_bool(db_sec.get("ssl_disabled"), False)

    webapp_sec = merged["webapp"]
    if env_wa_host := (_get_user_env("HOST") or _get_user_env("WEBAPP_HOST")):
        webapp_sec["host"] = env_wa_host
    if env_wa_port := (_get_user_env("PORT") or _get_user_env("WEBAPP_PORT")):
        webapp_sec["port"] = _coerce_int(env_wa_port, webapp_sec["port"])
    else:
        webapp_sec["port"] = _coerce_int(webapp_sec.get("port"), 8000)

    if env_wa_reload := (_get_user_env("RELOAD") or _get_user_env("WEBAPP_RELOAD")):
        webapp_sec["reload"] = _coerce_bool(env_wa_reload, webapp_sec["reload"])
    else:
        webapp_sec["reload"] = _coerce_bool(webapp_sec.get("reload"), True)

    parser_sec = merged["parser"]
    if env_p_te := _get_user_env("TABLE_ENGINE"):
        parser_sec["table_engine"] = env_p_te
    if env_p_mem := _get_user_env("MEMORY_MODE"):
        parser_sec["memory_mode"] = env_p_mem
    parser_sec["memory_mode"] = normalize_memory_mode(parser_sec.get("memory_mode"))
    if env_p_hp := (_get_user_env("HUGEPAGES") or _get_user_env("TE_HUGEPAGES")):
        parser_sec["hugepages"] = env_p_hp.lower().strip()
    else:
        parser_sec["hugepages"] = str(parser_sec.get("hugepages", "auto")).lower().strip()

    if env_p_flush := _get_user_env("PRELOAD_FLUSH_MB"):
        parser_sec["preload_flush_mb"] = _coerce_int(env_p_flush, 512)
    else:
        parser_sec["preload_flush_mb"] = _coerce_int(parser_sec.get("preload_flush_mb"), 512)

    if env_p_cache_max := _get_user_env("CACHE_MAX_TABLE_MB"):
        parser_sec["cache_max_table_mb"] = _coerce_int(env_p_cache_max, 0)
    else:
        parser_sec["cache_max_table_mb"] = _coerce_int(parser_sec.get("cache_max_table_mb"), 0)

    db_sec = merged.setdefault("database", copy.deepcopy(DEFAULT_CONFIG["database"]))
    if "ssh_tunnel" not in db_sec or not isinstance(db_sec["ssh_tunnel"], dict):
        db_sec["ssh_tunnel"] = copy.deepcopy(DEFAULT_CONFIG["database"]["ssh_tunnel"])
    ssh_sec = db_sec["ssh_tunnel"]

    # Keep top-level ssh_tunnel synchronized for backward compatibility.
    # Legacy file-level top-level sections were already migrated into
    # database.ssh_tunnel at load time; merging merged["ssh_tunnel"] here would
    # overwrite the user's database.ssh_tunnel with the built-in defaults.
    merged["ssh_tunnel"] = ssh_sec

    if env_ssh_enabled := (_get_user_env("SSH_TUNNEL_ENABLED") or _get_user_env("SSH_TUNNEL")):
        ssh_sec["enabled"] = _coerce_bool(env_ssh_enabled, ssh_sec.get("enabled", False))
    else:
        ssh_sec["enabled"] = _coerce_bool(ssh_sec.get("enabled"), False)

    if env_ssh_host := _get_user_env("SSH_HOST"):
        ssh_sec["host"] = env_ssh_host
    if env_ssh_port := _get_user_env("SSH_PORT"):
        ssh_sec["port"] = _coerce_int(env_ssh_port, ssh_sec.get("port", 22))
    else:
        ssh_sec["port"] = _coerce_int(ssh_sec.get("port"), 22)

    if env_ssh_user := _get_user_env("SSH_USER"):
        ssh_sec["user"] = env_ssh_user
    if env_ssh_pass := _get_user_env("SSH_PASSWORD"):
        ssh_sec["password"] = env_ssh_pass
    if env_ssh_key := _get_user_env("SSH_KEY_FILE"):
        ssh_sec["key_file"] = env_ssh_key

    if env_ssh_remote_host := _get_user_env("SSH_REMOTE_HOST"):
        ssh_sec["remote_host"] = env_ssh_remote_host
    if env_ssh_remote_port := _get_user_env("SSH_REMOTE_PORT"):
        ssh_sec["remote_port"] = _coerce_int(env_ssh_remote_port, ssh_sec.get("remote_port", 3306))
    else:
        ssh_sec["remote_port"] = _coerce_int(ssh_sec.get("remote_port"), 3306)

    if env_ssh_local_port := _get_user_env("SSH_LOCAL_PORT"):
        ssh_sec["local_port"] = _coerce_int(env_ssh_local_port, ssh_sec.get("local_port", 0))
    else:
        ssh_sec["local_port"] = _coerce_int(ssh_sec.get("local_port"), 0)

    if env_ssh_host_check := _get_user_env("SSH_STRICT_HOST_KEY_CHECKING"):
        ssh_sec["strict_host_key_checking"] = env_ssh_host_check
    if env_ssh_conn_timeout := _get_user_env("SSH_CONNECT_TIMEOUT"):
        ssh_sec["connect_timeout"] = _coerce_int(env_ssh_conn_timeout, ssh_sec.get("connect_timeout", 10))
    else:
        ssh_sec["connect_timeout"] = _coerce_int(ssh_sec.get("connect_timeout"), 10)

    if "mailing_list" not in merged or not isinstance(merged["mailing_list"], dict):
        merged["mailing_list"] = copy.deepcopy(DEFAULT_CONFIG["mailing_list"])
    ml_sec = merged["mailing_list"]
    ml_sec.setdefault("storage_dir", "/srv/lore")
    ml_sec.setdefault("port", 8192)
    ml_sec.setdefault("batch_size", 50)
    ml_sec.setdefault("request_timeout", 180)
    ml_sec.setdefault("daemon_max_rss_mb", 1024)
    ml_sec.setdefault("daemon_max_concurrent", 4)
    ml_sec.setdefault("auto_start_daemon", True)
    if "ssh_tunnel" not in ml_sec or not isinstance(ml_sec["ssh_tunnel"], dict):
        ml_sec["ssh_tunnel"] = copy.deepcopy(DEFAULT_CONFIG["mailing_list"]["ssh_tunnel"])
    else:
        if not ml_sec["ssh_tunnel"].get("remote_port"):
            ml_sec["ssh_tunnel"]["remote_port"] = 8192

    # 4. Synchronize into os.environ for low-level and child worker compatibility
    sync_environ(merged)

    return merged


def init_config(custom_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Initialize or re-initialize the active global configuration."""
    global _ACTIVE_CONFIG
    if custom_path is None and _ACTIVE_CONFIG is not None:
        return _ACTIVE_CONFIG
    _ACTIVE_CONFIG = load_config(custom_path)
    return _ACTIVE_CONFIG


def get_config() -> dict[str, dict[str, Any]]:
    """Retrieve the cached active configuration, auto-initializing if necessary."""
    global _ACTIVE_CONFIG
    if _ACTIVE_CONFIG is None:
        _ACTIVE_CONFIG = load_config()
    return _ACTIVE_CONFIG


def get_db_config() -> dict[str, Any]:
    """Retrieve active database configuration section."""
    return get_config().get("database", copy.deepcopy(DEFAULT_CONFIG["database"]))


def get_webapp_config() -> dict[str, Any]:
    """Retrieve active webapp configuration section."""
    return get_config().get("webapp", copy.deepcopy(DEFAULT_CONFIG["webapp"]))


def get_parser_config() -> dict[str, Any]:
    """Retrieve active parser configuration section."""
    return get_config().get("parser", copy.deepcopy(DEFAULT_CONFIG["parser"]))


def get_ssh_tunnel_config(feature: str = "database") -> dict[str, Any]:
    """Retrieve active SSH tunnel configuration section for a specific feature."""
    cfg = get_config()
    feature_sec = cfg.get(feature)
    if isinstance(feature_sec, dict) and "ssh_tunnel" in feature_sec and isinstance(feature_sec["ssh_tunnel"], dict):
        return feature_sec["ssh_tunnel"]
    if feature == "database" and "ssh_tunnel" in cfg and isinstance(cfg["ssh_tunnel"], dict):
        return cfg["ssh_tunnel"]
    default_tunnel = copy.deepcopy(DEFAULT_SSH_TUNNEL_CONFIG)
    if feature == "database":
        default_tunnel["remote_port"] = 3306
    elif feature == "mailing_list":
        default_tunnel["remote_port"] = 8192
    return default_tunnel


def get_mailing_list_config() -> dict[str, Any]:
    """Retrieve active mailing list configuration section."""
    return get_config().get("mailing_list", copy.deepcopy(DEFAULT_CONFIG.get("mailing_list", {})))


def get_workspace_config() -> dict[str, Any]:
    """Retrieve active workspace configuration section."""
    return get_config().get("workspace", copy.deepcopy(DEFAULT_CONFIG["workspace"]))


def get_repos_dir() -> Path:
    """Absolute directory scanned for Git repositories (workspace.repos_dir, relative to REPO_ROOT)."""
    raw = os.path.expanduser(str(get_workspace_config().get("repos_dir") or "repos"))
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def resolve_repo_path(entry: dict[str, Any]) -> Path:
    """Absolute path of a repositories[] entry (relative paths resolve against REPO_ROOT)."""
    path = Path(os.path.expanduser(str(entry.get("path") or "")))
    return path if path.is_absolute() else REPO_ROOT / path


def _git_out(repo: Path, *args: str, timeout: float = 120.0) -> str:
    """Run a read-only git command in repo, returning stripped stdout ('' on failure)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def is_git_repository(path: Path) -> bool:
    """True for a work tree (.git dir or gitfile) or a bare repository."""
    if (path / ".git").exists():
        return True
    return (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()


def detect_default_branch(repo: Path) -> str:
    """Branch the repository tracks: checked-out branch, else origin/HEAD, else master/main."""
    branch = _git_out(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch:
        return branch
    remote_head = _git_out(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if remote_head:
        return remote_head.split("/", 1)[-1]
    for candidate in ("master", "main"):
        if _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"):
            return candidate
    return "HEAD"


def detect_root_commit(repo: Path, rev: str = "HEAD") -> str:
    """First commit of rev's history: the parentless commit reached by following first parents."""
    roots = _git_out(repo, "rev-list", "--max-parents=0", "--first-parent", rev).splitlines()
    return roots[-1] if roots else ""


def detect_start_ref(repo: Path, root_commit: str, branches: list[str]) -> str:
    """First version to parse: the earliest release tag on a root of the default or a history/* branch.

    A history remote (e.g. bootlin's linux-history next to torvalds/linux) carries roots older than the
    default branch's (Linux 0.01 vs. 2.6.12-rc2); the start is the root whose release tag sorts first, named
    by that tag so the release walk crosses into the later, unconnected histories. Without one, it is
    root_commit."""
    from core.FileHandler import _VERSION_TAG_RE, _VERSIONSORT_SUFFIX_ARGS

    roots = {root_commit} if root_commit else set()
    for b in branches:
        if b.startswith("history/") and b != "history/HEAD":
            lines = _git_out(repo, "rev-list", "--max-parents=0", "--first-parent", b).splitlines()
            if lines:
                roots.add(lines[-1])
    tags = [
        t for r in sorted(roots) for t in _git_out(repo, "tag", "--points-at", r).splitlines()
        if not t.endswith("-tree") and _VERSION_TAG_RE.match(t) and not t.startswith("next-")
    ]
    if len(roots) < 2 or not tags:
        return root_commit
    ordered = _git_out(repo, *_VERSIONSORT_SUFFIX_ARGS, "tag", "-l", "--sort=v:refname", *tags).splitlines()
    return ordered[0] if ordered else root_commit


_DETECTED_REPO_CACHE: dict[str, dict[str, Any]] = {}


DEFAULT_BRANCH_FILTER: dict[str, list[str]] = {
    "include": ["*"],
    "exclude": ["*rc*"],
}

DEFAULT_TAG_VIEWS: list[dict[str, Any]] = [
    {
        "name": "— any branch —",
        "regex_rules": [
            r"^(v2\.6\.\d+)((?:-(?:rc|pre|alpha|beta)\d*|\.\d+(?:-(?:rc|pre|alpha|beta)\d*)?).*)$",
            r"^(v(?!2\.6\.)\d+\.\d+)((?:-(?:rc|pre|alpha|beta)\d*|\.\d+(?:-(?:rc|pre|alpha|beta)\d*)?).*)$",
        ],
    }
]


# Projects the parser knows by slug (repositories[].project, m_project.slug) and their display names
LINUX_PROJECT = "linux"
MANPAGES_PROJECT = "man-pages"
PROJECT_DISPLAY_NAMES: dict[str, str] = {LINUX_PROJECT: "Linux Kernel", MANPAGES_PROJECT: "Linux man-pages"}
# Projects whose trees are not kernel trees: the Linux-only per-version steps (Kbuild, ARCH layouts, CVE
# commit resolution) skip them
NON_KERNEL_PROJECTS = frozenset({MANPAGES_PROJECT})

# The Linux man-pages repository (https://git.kernel.org/pub/scm/docs/man-pages/man-pages.git): every
# release tag (man-pages-X.Y[.Z]) descends from this root commit
MANPAGES_ROOT_COMMIT = "fea681dafb1363a154b7fc6d59baa83d2a9ebc5c"
# Root commit of torvalds/linux (v2.6.12-rc2): a clone rooted there is the `linux` project whatever its directory name
LINUX_ROOT_COMMIT = "1da177e4c3f41524e886b7f1b8a0c1fc7321cac2"
MANPAGES_DEFAULT_BRANCH = "master"
MANPAGES_BRANCH_FILTER: dict[str, list[str]] = {
    "include": ["master"],
    "exclude": [],
}
MANPAGES_TAG_VIEWS: list[dict[str, Any]] = [
    {
        "name": "— man-pages —",
        "regex_rules": [r"^(man-pages-\d+)(\..*)$"],
    }
]


def project_display_name(slug: str | None) -> str:
    """Display name of a project slug: "Linux Kernel" for `linux`, "Linux man-pages" for `man-pages`, else the slug."""
    slug = str(slug or "").strip() or LINUX_PROJECT
    return PROJECT_DISPLAY_NAMES.get(slug, slug)


def is_kernel_project(slug: str | None) -> bool:
    """False for projects known not to be kernel trees (`man-pages`); unknown slugs count as kernel trees."""
    return str(slug or "").strip() not in NON_KERNEL_PROJECTS


def repository_project(entry: dict[str, Any] | None) -> str:
    """Project slug of a repositories[] entry (`project`, else `linux`)."""
    return str((entry or {}).get("project") or "").strip() or LINUX_PROJECT


def is_manpages_repository(name: str, git_url: str = "", root_commit: str = "") -> bool:
    """True for a clone of the Linux man-pages: its root commit, a `man-pages` directory or a `man-pages.git` URL."""
    url = (git_url or "").strip().rstrip("/")
    return (
        root_commit == MANPAGES_ROOT_COMMIT
        or name == MANPAGES_PROJECT
        or url.endswith(("/man-pages.git", "/man-pages"))
    )


def matches_branch_filter(branch: str, branch_filter: dict[str, Any] | None = None) -> bool:
    """True if branch matches include patterns and does not match exclude patterns."""
    bf = branch_filter if isinstance(branch_filter, dict) else DEFAULT_BRANCH_FILTER
    includes = bf.get("include") or ["*"]
    excludes = bf.get("exclude") or []
    if isinstance(includes, str):
        includes = [includes]
    if isinstance(excludes, str):
        excludes = [excludes]

    if not any(fnmatch.fnmatch(branch, pat) for pat in includes):
        return False
    if any(fnmatch.fnmatch(branch, pat) for pat in excludes):
        return False
    return True


def find_repository_branches(
    repo: Path,
    branch_filter: dict[str, Any] | None = None,
    default_branch: str | None = None,
) -> list[str]:
    """Discover local and remote-tracking Git branches matching branch_filter.

    Symbolic refs (e.g. origin/HEAD) are omitted. When a branch exists locally
    (e.g. 'master'), its remote-tracking counterpart on origin (e.g. 'origin/master')
    is deduplicated and omitted in favor of the local branch.

    The default branch (or main/master) is placed first if present, followed by
    remaining branches in alphabetical order.
    """
    raw_lines = _git_out(
        repo,
        "for-each-ref",
        "--format=%(refname) %(refname:short) %(symref)",
        "refs/heads/",
        "refs/remotes/",
    ).splitlines()

    local_branches: set[str] = set()
    ref_entries: list[tuple[str, str]] = []  # (refname, short_name)

    for line in raw_lines:
        parts = line.strip().split()
        if not parts:
            continue
        refname = parts[0]
        short_name = parts[1] if len(parts) > 1 else refname
        symref = parts[2] if len(parts) > 2 and parts[2] != parts[-1] else ""

        # Skip symbolic refs (e.g. refs/remotes/origin/HEAD)
        if symref or refname.endswith("/HEAD"):
            continue

        if refname.startswith("refs/heads/"):
            bname = refname[len("refs/heads/"):]
            local_branches.add(bname)
            local_branches.add(short_name)

        ref_entries.append((refname, short_name))

    candidates: list[str] = []
    seen: set[str] = set()

    for refname, short_name in ref_entries:
        if refname.startswith("refs/remotes/"):
            rel = refname[len("refs/remotes/"):]
            remote_name, _, remote_branch = rel.partition("/")
            # Deduplicate remote tracking branches on origin when a local branch exists with the same name
            if remote_name == "origin" and remote_branch in local_branches:
                continue

        if short_name and short_name not in seen:
            seen.add(short_name)
            if matches_branch_filter(short_name, branch_filter):
                candidates.append(short_name)

    def_b = default_branch or ("main" if "main" in candidates else ("master" if "master" in candidates else ""))

    def branch_sort_key(b: str) -> tuple[int, tuple, str]:
        # Tier 0: History trunk
        if b in ("history/master", "history/HEAD"):
            return (0, (), b)
        # Tier 1: Secondary history branches
        if b.startswith("history/"):
            return (1, (), b)
        # Tier 2: Mainline default branch or main/master
        if def_b and b == def_b:
            return (2, (0,), b)
        if b in ("master", "main", "origin/master", "origin/main"):
            return (2, (1,), b)
        # Tier 3: Stable series sorted by dotted version numbers
        if b.startswith("stable/") or (b.startswith("linux-") and not b.startswith("linux-next/")):
            nums = tuple(int(n) for n in re.findall(r"\d+", b))
            return (3, nums if nums else (999,), b)
        # Tier 4: Linux-next integration branches (linux-next/master first)
        if b in ("linux-next/master", "linux-next/HEAD"):
            return (4, (0,), b)
        if b.startswith("linux-next/") or b.startswith("next/"):
            return (4, (1,), b)
        # Tier 5: Remaining topic branches
        return (5, (), b)

    candidates.sort(key=branch_sort_key)
    return candidates


def detect_repository(path: Path) -> dict[str, Any]:
    """Build a repositories[] entry for the Git repository at path (cached per path for the process)."""
    # abspath keeps symlinked repositories addressed through repos_dir instead of their target
    path = Path(os.path.abspath(path))
    if str(path) in _DETECTED_REPO_CACHE:
        return dict(_DETECTED_REPO_CACHE[str(path)])
    try:
        rel_path = str(path.relative_to(REPO_ROOT))
    except ValueError:
        rel_path = str(path)
    branch = detect_default_branch(path)
    root_commit = detect_root_commit(path, branch)
    git_url = _git_out(path, "config", "--get", "remote.origin.url")
    if is_manpages_repository(path.name, git_url, root_commit):
        # The man-pages: its own project, the master branch only, parsed from its root commit, tags by major
        if branch == "HEAD" or not branch:
            branch = MANPAGES_DEFAULT_BRANCH
        branch_filter = copy.deepcopy(MANPAGES_BRANCH_FILTER)
        branches = find_repository_branches(path, branch_filter, default_branch=branch)
        project, start_ref, tag_views = MANPAGES_PROJECT, root_commit, copy.deepcopy(MANPAGES_TAG_VIEWS)
    else:
        branch_filter = copy.deepcopy(DEFAULT_BRANCH_FILTER)
        branches = find_repository_branches(path, branch_filter, default_branch=branch)
        # Parsing starts from the first version of the repository (history/* roots included) unless overridden
        project = LINUX_PROJECT if root_commit == LINUX_ROOT_COMMIT else path.name
        start_ref, tag_views = detect_start_ref(path, root_commit, branches), copy.deepcopy(DEFAULT_TAG_VIEWS)
    entry = {
        "name": path.name,
        "path": rel_path,
        "project": project,
        "default_branch": branch,
        "git_url": git_url,
        "root_commit": root_commit,
        "start_ref": start_ref,
        "enabled": True,
        "branch_filter": branch_filter,
        "branches": branches,
        "tag_views": tag_views,
    }
    _DETECTED_REPO_CACHE[str(path)] = entry
    return dict(entry)


def find_repository_dirs(repos_dir: Path | None = None) -> list[Path]:
    """Git repositories directly under repos_dir (symlinked repositories included)."""
    base = repos_dir or get_repos_dir()
    if not base.is_dir():
        return []
    return [child for child in sorted(base.iterdir()) if child.is_dir() and is_git_repository(child)]


def _missing_repo_keys(entry: dict[str, Any]) -> list[str]:
    return [k for k in REPOSITORY_KEYS if k not in entry]


def _merge_repositories(
    existing: list[dict[str, Any]], repo_dirs: list[Path]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Add repositories found in repo_dirs and fill keys missing from known ones; never overwrite user values.

    Git metadata is only computed for repositories that are new or incomplete.
    """
    merged = [dict(e) for e in existing if isinstance(e, dict)]
    known = {resolve_repo_path(e).resolve(): e for e in merged if e.get("path")}
    changes: list[str] = []
    for repo_dir in repo_dirs:
        entry = known.get(repo_dir.resolve())
        if entry is None:
            repo = detect_repository(repo_dir)
            merged.append(repo)
            known[repo_dir.resolve()] = repo
            changes.append(f"added '{repo['name']}' ({repo['path']})")
            continue
        missing = _missing_repo_keys(entry)
        if not missing:
            continue
        repo = detect_repository(repo_dir)
        for k in missing:
            entry[k] = repo[k]
        changes.append(f"completed '{entry.get('name', repo['name'])}' ({', '.join(missing)})")
    return merged, changes


def sync_repositories(write: bool = True) -> list[dict[str, Any]]:
    """Detect repositories under workspace.repos_dir and register them in the active config.

    New repositories are appended to "repositories" and missing keys of known entries are filled.
    With write=True the result is persisted to the loaded config file (config.json under REPO_ROOT
    when none was loaded), preserving every other key of that file.
    """
    cfg = get_config()
    merged, changes = _merge_repositories(cfg.get("repositories") or [], find_repository_dirs())
    cfg["repositories"] = merged
    if not changes:
        return merged
    for change in changes:
        logger.info("Repository auto-detection: %s", change)

    if write:
        target = _LOADED_CONFIG_PATH or REPO_ROOT / "config.json"
        file_data: dict[str, Any] = {}
        if target.is_file():
            with open(target, "r", encoding="utf-8") as f:
                file_data = json.load(f)
        file_data["repositories"] = merged
        file_data.setdefault("workspace", copy.deepcopy(cfg.get("workspace", DEFAULT_CONFIG["workspace"])))
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(file_data, f, indent=2)
            f.write("\n")
        os.replace(tmp, target)
        logger.info("Saved %d repository entr%s to %s", len(merged), "y" if len(merged) == 1 else "ies", target)
    return merged


def get_repositories_config() -> list[dict[str, Any]]:
    """Retrieve configured repositories, merged in memory with repositories detected under repos_dir."""
    repos = get_config().get("repositories") or []
    if _coerce_bool(get_workspace_config().get("auto_detect_repos"), True):
        repos, _ = _merge_repositories(repos, find_repository_dirs())
    return [r for r in repos if _coerce_bool(r.get("enabled"), True)]


def get_repository(name_or_path: str | None = None) -> dict[str, Any] | None:
    """Look up a repository by name or path; without an argument return the default one.

    The default is the entry flagged "default": true, else the first enabled repository.
    """
    repos = get_repositories_config()
    if name_or_path:
        for r in repos:
            if r.get("name") == name_or_path or r.get("path") == name_or_path:
                return r
        candidate = Path(os.path.expanduser(name_or_path))
        candidate = candidate if candidate.is_absolute() else REPO_ROOT / candidate
        for r in repos:
            if resolve_repo_path(r).resolve() == candidate.resolve():
                return r
        return None
    for r in repos:
        if _coerce_bool(r.get("default"), False):
            return r
    return repos[0] if repos else None


def get_default_repo_path() -> Path:
    """Absolute path of the default repository (falls back to <repos_dir>/linux)."""
    repo = get_repository()
    return resolve_repo_path(repo) if repo else get_repos_dir() / "linux"


def update_repository_branches(name_or_path: str | None = None, write: bool = True) -> list[str]:
    """Refresh the 'branches' list for the repository matching name_or_path and optionally save to config."""
    cfg = get_config()
    repo = get_repository(name_or_path)
    if not repo:
        return []
    repo_path = resolve_repo_path(repo)
    bf = repo.get("branch_filter") or copy.deepcopy(DEFAULT_BRANCH_FILTER)
    branches = find_repository_branches(repo_path, bf, default_branch=repo.get("default_branch"))
    repo["branches"] = branches
    repo.setdefault("branch_filter", copy.deepcopy(bf))

    # Also update in cfg['repositories']
    for r in cfg.get("repositories", []):
        if r.get("name") == repo.get("name") or resolve_repo_path(r).resolve() == repo_path.resolve():
            r["branches"] = branches
            r.setdefault("branch_filter", copy.deepcopy(bf))
            break

    if write:
        target = _LOADED_CONFIG_PATH or REPO_ROOT / "config.json"
        if target.is_file():
            file_data: dict[str, Any] = {}
            with open(target, "r", encoding="utf-8") as f:
                file_data = json.load(f)
            file_repos = file_data.setdefault("repositories", copy.deepcopy(cfg.get("repositories", [])))
            found = False
            for r in file_repos:
                if r.get("name") == repo.get("name") or resolve_repo_path(r).resolve() == repo_path.resolve():
                    r["branches"] = branches
                    r.setdefault("branch_filter", copy.deepcopy(bf))
                    found = True
                    break
            if not found:
                file_repos.append(copy.deepcopy(repo))
            tmp = target.with_suffix(target.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(file_data, f, indent=2)
                f.write("\n")
            os.replace(tmp, target)
            logger.info("Updated %d branches for repository '%s' in %s", len(branches), repo.get("name"), target)
    return branches


def reset_config() -> None:
    """Reset the active configuration to uninitialized state (useful for tests)."""
    global _ACTIVE_CONFIG, _LOADED_CONFIG_PATH, _EXPORTED_ENV_VARS
    _ACTIVE_CONFIG = None
    _LOADED_CONFIG_PATH = None
    _DETECTED_REPO_CACHE.clear()
    for k in list(_EXPORTED_ENV_VARS.keys()):
        if os.environ.get(k) == _EXPORTED_ENV_VARS[k]:
            os.environ.pop(k, None)
    _EXPORTED_ENV_VARS.clear()
