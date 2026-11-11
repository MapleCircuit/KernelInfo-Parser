"""core/tui/testing.py - Headless diagnostic testing engine for KernelInfo-Parser configuration.

Provides diagnostic tests for verifying database connectivity, SSH tunnel reachability,
Git repository integrity, CVE catalog valid status, and mailing list storage/daemon health.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import time
import traceback
from typing import Any, Iterator
import urllib.error
import urllib.request

from core.config import (
    DEFAULT_BRANCH_FILTER,
    REPO_ROOT,
    find_repository_branches,
    is_git_repository,
    matches_branch_filter,
    resolve_repo_path,
)

logger = logging.getLogger(__name__)


@dataclass
class DiagnosticResult:
    """Diagnostic outcome for a single configuration subsystem or component."""

    name: str
    passed: bool
    latency_ms: float
    message: str
    details: str = ""


_DEFAULT_REMOTE_PORTS = {"database": 3306, "mailing_list": 8192}


def _tunnel_remote_port(ssh_cfg: dict[str, Any], feature_name: str) -> int:
    """Remote port forwarded by a feature's tunnel (config value or the feature default)."""
    try:
        port = int(ssh_cfg.get("remote_port") or 0)
    except (ValueError, TypeError):
        port = 0
    return port or _DEFAULT_REMOTE_PORTS.get(feature_name, 0)


@contextmanager
def open_test_tunnel(ssh_cfg: dict[str, Any], feature_name: str = "database") -> Iterator[int]:
    """Yield the local port of a usable SSH port forward for `feature_name`.

    Reuses this process's active tunnel for the feature when one is running; otherwise spawns
    a temporary `ssh -L` forward from `ssh_cfg` and tears it down on exit. The temporary
    manager runs under a private feature key (`tui-test:<feature>`), so establishing it never
    rewrites the active configuration's host/port the way a pipeline tunnel does.

    Raises:
        RuntimeError / ValueError: The forward could not be established (auth, host, timeout).
    """
    from core.ssh_tunnel import SSHTunnelManager, get_active_ssh_tunnel

    active = get_active_ssh_tunnel(feature_name)
    if active is not None and active.local_port:
        yield active.local_port
        return

    try:
        connect_timeout = int(float(ssh_cfg.get("connect_timeout", 10) or 10))
    except (ValueError, TypeError):
        connect_timeout = 10
    mgr = SSHTunnelManager(
        host=str(ssh_cfg.get("host", "") or ""),
        port=int(ssh_cfg.get("port", 22) or 22),
        user=str(ssh_cfg.get("user", "") or ""),
        password=str(ssh_cfg.get("password", "") or ""),
        key_file=str(ssh_cfg.get("key_file", "") or ""),
        remote_host=str(ssh_cfg.get("remote_host", "") or "127.0.0.1"),
        remote_port=_tunnel_remote_port(ssh_cfg, feature_name),
        local_port=int(ssh_cfg.get("local_port", 0) or 0),
        strict_host_key_checking=str(ssh_cfg.get("strict_host_key_checking", "") or "accept-new"),
        connect_timeout=connect_timeout,
        feature=f"tui-test:{feature_name}",
        detach_tty=True,
    )
    mgr.start()
    try:
        yield mgr.local_port
    finally:
        mgr.stop()


def test_ssh_tunnel(
    ssh_cfg: dict[str, Any],
    feature_name: str = "database",
    establish: bool = True,
) -> DiagnosticResult:
    """Verify SSH tunnel configuration, key accessibility, host reachability, and port forwarding.

    Args:
        ssh_cfg: SSH tunnel configuration dictionary.
        feature_name: Logical feature identifier (e.g. 'database', 'mailing_list').
        establish: When True, also authenticates and opens the `ssh -L` forward
            (`open_test_tunnel`), then closes it; False limits the check to TCP reachability.

    Returns:
        DiagnosticResult with connection latency and verification details.
    """
    test_name = f"SSH Tunnel ({feature_name})"
    try:
        if not ssh_cfg.get("enabled", False):
            return DiagnosticResult(
                name=test_name,
                passed=True,
                latency_ms=0.0,
                message="SSH tunnel is disabled",
                details="",
            )

        host = str(ssh_cfg.get("host", "")).strip()
        if not host:
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message="SSH host is not configured",
                details="Configuration field 'host' is empty.",
            )

        raw_port = ssh_cfg.get("port", 22)
        try:
            port = int(raw_port)
            if port <= 0 or port > 65535:
                raise ValueError(f"Port {port} out of range (1-65535)")
        except (ValueError, TypeError) as exc:
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message=f"Invalid SSH port: {raw_port}",
                details=str(exc),
            )

        key_file = str(ssh_cfg.get("key_file", "")).strip()
        key_details = ""
        if key_file:
            key_path = Path(os.path.expanduser(key_file))
            if not key_path.exists():
                return DiagnosticResult(
                    name=test_name,
                    passed=False,
                    latency_ms=0.0,
                    message=f"SSH private key file not found: {key_file}",
                    details=f"Resolved key path does not exist: {key_path}",
                )
            if not os.access(key_path, os.R_OK):
                return DiagnosticResult(
                    name=test_name,
                    passed=False,
                    latency_ms=0.0,
                    message=f"SSH private key file is not readable: {key_file}",
                    details=f"Read permission denied for: {key_path}",
                )
            key_details = f"Key file: {key_path} (accessible)"

        raw_timeout = ssh_cfg.get("connect_timeout", 3.0)
        try:
            timeout = float(raw_timeout)
            if timeout <= 0.0 or timeout > 10.0:
                timeout = 3.0
        except (ValueError, TypeError):
            timeout = 3.0

        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                latency_ms = (time.perf_counter() - t0) * 1000.0
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=round(latency_ms, 2),
                message=f"SSH host {host}:{port} is unreachable: {exc}",
                details=traceback.format_exc(),
            )

        details_lines = [f"Host: {host}:{port}", f"Socket latency: {latency_ms:.1f}ms"]
        if key_details:
            details_lines.append(key_details)

        if not establish:
            return DiagnosticResult(
                name=test_name,
                passed=True,
                latency_ms=round(latency_ms, 2),
                message=f"SSH host {host}:{port} reachable",
                details="\n".join(details_lines),
            )

        remote_host = str(ssh_cfg.get("remote_host", "") or "127.0.0.1")
        remote_port = _tunnel_remote_port(ssh_cfg, feature_name)
        t1 = time.perf_counter()
        try:
            with open_test_tunnel(ssh_cfg, feature_name) as local_port:
                tunnel_ms = (time.perf_counter() - t1) * 1000.0
        except Exception as exc:
            tunnel_ms = (time.perf_counter() - t1) * 1000.0
            details_lines.append(f"Forward: {remote_host}:{remote_port} (not established)")
            details_lines.append(str(exc))
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=round(tunnel_ms, 2),
                message=f"SSH host {host}:{port} reachable but tunnel failed: {exc}",
                details="\n".join(details_lines),
            )

        details_lines.append(f"Forward: 127.0.0.1:{local_port} -> {remote_host}:{remote_port}")
        details_lines.append(f"Tunnel setup: {tunnel_ms:.1f}ms")
        return DiagnosticResult(
            name=test_name,
            passed=True,
            latency_ms=round(tunnel_ms, 2),
            message=f"SSH tunnel via {host}:{port} established",
            details="\n".join(details_lines),
        )
    except Exception as exc:
        return DiagnosticResult(
            name=test_name,
            passed=False,
            latency_ms=0.0,
            message=f"SSH tunnel check failed: {exc}",
            details=traceback.format_exc(),
        )


def _check_local_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """Test if a local TCP port is actively listening and accepting connections."""
    if port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def test_database_connection(
    db_cfg: dict[str, Any],
    ssh_tunnel_cfg: dict[str, Any] | None = None,
    establish_tunnel: bool = True,
) -> DiagnosticResult:
    """Test database connectivity, host/socket resolution, and simple query execution.

    Args:
        db_cfg: Database configuration mapping (host, port, user, database, unix_socket, engine, etc.).
        ssh_tunnel_cfg: Optional SSH tunnel configuration overriding db_cfg['ssh_tunnel'].
        establish_tunnel: When the SSH tunnel is enabled but no forward is running, open a
            temporary one (`open_test_tunnel`) for the duration of the test.

    Returns:
        DiagnosticResult with connection latency and server details.
    """
    test_name = "Database Connection"
    tunnel_stack = ExitStack()
    try:
        if not db_cfg:
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message="Database configuration is missing or empty",
                details="Configuration section 'database' was not provided.",
            )

        engine = str(db_cfg.get("engine", "mariadb")).lower()

        # In-memory mock engine support
        if engine in ("mock", "mockdb", "inmemory"):
            return DiagnosticResult(
                name=test_name,
                passed=True,
                latency_ms=0.0,
                message=f"Mock database configured ({engine})",
                details=f"Engine '{engine}' does not require an external SQL server.",
            )

        # SQLite database engine support
        if engine in ("sqlite", "sqlite3"):
            db_path = str(db_cfg.get("database", ":memory:"))
            t0 = time.perf_counter()
            import sqlite3

            try:
                conn = sqlite3.connect(db_path, timeout=float(db_cfg.get("timeout", 3.0)))
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchall()
                cur.execute("SELECT sqlite_version()")
                ver_row = cur.fetchone()
                ver_str = ver_row[0] if ver_row else "unknown"
                latency_ms = (time.perf_counter() - t0) * 1000.0
                conn.close()
                return DiagnosticResult(
                    name=test_name,
                    passed=True,
                    latency_ms=round(latency_ms, 2),
                    message=f"Connected to SQLite ({ver_str}) successfully",
                    details=f"Path: {db_path}\nSQLite Version: {ver_str}\nLatency: {latency_ms:.1f}ms",
                )
            except Exception as exc:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                return DiagnosticResult(
                    name=test_name,
                    passed=False,
                    latency_ms=round(latency_ms, 2),
                    message=f"SQLite connection failed: {exc}",
                    details=traceback.format_exc(),
                )

        # MariaDB / MySQL validation
        user = db_cfg.get("user")
        db_name = db_cfg.get("database")
        if not user or not db_name:
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message="Database credentials incomplete (user and database required)",
                details=f"Configured: user={user!r}, database={db_name!r}",
            )

        password = db_cfg.get("password", "")
        host = db_cfg.get("host")
        raw_port = db_cfg.get("port", 3306)
        try:
            port = int(raw_port)
        except (ValueError, TypeError):
            port = 3306

        unix_socket = db_cfg.get("unix_socket")
        ssl_disabled = bool(db_cfg.get("ssl_disabled", False))

        # Check SSH Tunnel status if enabled
        ssh_cfg = ssh_tunnel_cfg if ssh_tunnel_cfg is not None else db_cfg.get("ssh_tunnel", {})
        tunnel_enabled = bool(isinstance(ssh_cfg, dict) and ssh_cfg.get("enabled", False))

        if tunnel_enabled:
            from core.ssh_tunnel import get_active_ssh_tunnel, is_ssh_tunnel_active

            active = is_ssh_tunnel_active("database")
            mgr = get_active_ssh_tunnel("database") if active else None
            configured_local_port = int(ssh_cfg.get("local_port", 0))
            effective_local_port = mgr.local_port if mgr and mgr.local_port else configured_local_port

            port_listening = _check_local_port_listening(effective_local_port)

            if not active and not port_listening:
                # Tunnel is inactive: perform a safe diagnostic socket test on SSH endpoint
                ssh_host = str(ssh_cfg.get("host", "")).strip()
                ssh_port = int(ssh_cfg.get("port", 22))
                t_ssh_start = time.perf_counter()
                ssh_reachable = False
                ssh_err = ""
                if ssh_host and ssh_port > 0:
                    try:
                        with socket.create_connection((ssh_host, ssh_port), timeout=3.0):
                            ssh_reachable = True
                    except Exception as err:
                        ssh_err = str(err)
                else:
                    ssh_err = "SSH host/port not configured"
                ssh_latency = (time.perf_counter() - t_ssh_start) * 1000.0

                if ssh_reachable and establish_tunnel:
                    try:
                        effective_local_port = tunnel_stack.enter_context(open_test_tunnel(ssh_cfg, "database"))
                    except Exception as tun_exc:
                        return DiagnosticResult(
                            name=test_name,
                            passed=False,
                            latency_ms=round((time.perf_counter() - t_ssh_start) * 1000.0, 2),
                            message=f"SSH tunnel via {ssh_host}:{ssh_port} could not be established: {tun_exc}",
                            details=(
                                f"SSH server {ssh_host}:{ssh_port} is reachable, but the port forward to "
                                f"{ssh_cfg.get('remote_host') or '127.0.0.1'}:{_tunnel_remote_port(ssh_cfg, 'database')} "
                                f"failed.\n{tun_exc}"
                            ),
                        )
                elif ssh_reachable:
                    return DiagnosticResult(
                        name=test_name,
                        passed=False,
                        latency_ms=round(ssh_latency, 2),
                        message=f"SSH tunnel required but not running (SSH server {ssh_host}:{ssh_port} is reachable)",
                        details=(
                            f"Database uses SSH port forwarding ({ssh_host}:{ssh_port}), "
                            f"but local tunnel port {effective_local_port or 'dynamic'} is not active.\n"
                            f"SSH host was reached in {ssh_latency:.1f}ms. Start the tunnel before connecting."
                        ),
                    )
                else:
                    return DiagnosticResult(
                        name=test_name,
                        passed=False,
                        latency_ms=round(ssh_latency, 2),
                        message=f"SSH tunnel required but not running and SSH host {ssh_host}:{ssh_port} unreachable: {ssh_err}",
                        details=(
                            f"Database requires an SSH tunnel, but SSH endpoint {ssh_host}:{ssh_port} "
                            f"is unreachable ({ssh_err}). Check network or tunnel configuration."
                        ),
                    )

            # A forward is listening on `effective_local_port`. The probe still targets the
            # configured database Host/Port, so a Port that does not reach the forward fails here.
            host = host or "127.0.0.1"
            forward_port = effective_local_port
            unix_socket = None
        else:
            forward_port = 0

        # Resolve Unix domain socket candidates when not tunneled
        resolved_socket = None
        if not tunnel_enabled and unix_socket:
            if unix_socket == "auto":
                if not host or host in ("127.0.0.1", "localhost"):
                    for candidate in ("/run/mysqld/mysqld.sock", "/var/run/mysqld/mysqld.sock", "/tmp/mysql.sock"):
                        if os.path.exists(candidate):
                            resolved_socket = candidate
                            break
            elif unix_socket not in ("off", "disabled", "none", "false") and os.path.exists(unix_socket):
                resolved_socket = unix_socket

        try:
            import mysql.connector
        except ImportError:
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message="mysql-connector is not installed",
                details="Please install mysql-connector-python: pip install mysql-connector-python",
            )

        conn_kwargs: dict[str, Any] = {
            "user": user,
            "password": password,
            "database": db_name,
            "connection_timeout": 3,
            "autocommit": True,
        }
        if ssl_disabled:
            conn_kwargs["ssl_disabled"] = True

        if resolved_socket:
            conn_kwargs["unix_socket"] = resolved_socket
        else:
            conn_kwargs["host"] = host or "127.0.0.1"
            conn_kwargs["port"] = port

        t0 = time.perf_counter()
        try:
            cnx = mysql.connector.connect(**conn_kwargs)
        except Exception as exc:
            # If Unix socket was attempted, attempt TCP fallback if host is present
            if resolved_socket and host:
                try:
                    conn_kwargs.pop("unix_socket", None)
                    conn_kwargs["host"] = host
                    conn_kwargs["port"] = port
                    cnx = mysql.connector.connect(**conn_kwargs)
                except Exception as fallback_exc:
                    latency_ms = (time.perf_counter() - t0) * 1000.0
                    return DiagnosticResult(
                        name=test_name,
                        passed=False,
                        latency_ms=round(latency_ms, 2),
                        message=f"Database connection failed: {fallback_exc}",
                        details=traceback.format_exc(),
                    )
            else:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                details = traceback.format_exc()
                if forward_port and forward_port != port:
                    details = (
                        f"Target: {conn_kwargs['host']}:{port}\n"
                        f"The SSH forward listens on 127.0.0.1:{forward_port}, not on Port {port}. "
                        f"Set Local Port to {port} (or Port to the Local Port).\n{details}"
                    )
                return DiagnosticResult(
                    name=test_name,
                    passed=False,
                    latency_ms=round(latency_ms, 2),
                    message=f"Database connection failed: {exc}",
                    details=details,
                )

        server_info = ""
        try:
            cur = cnx.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            server_info = cnx.get_server_info() or ""
            cur.close()
            cnx.close()
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=round(latency_ms, 2),
                message=f"Query failed on connected database: {exc}",
                details=traceback.format_exc(),
            )

        latency_ms = (time.perf_counter() - t0) * 1000.0
        server_str = f" ({server_info})" if server_info else ""
        target_str = resolved_socket if resolved_socket else f"{conn_kwargs.get('host')}:{conn_kwargs.get('port')}"

        return DiagnosticResult(
            name=test_name,
            passed=True,
            latency_ms=round(latency_ms, 2),
            message=f"Connected to database successfully{server_str}",
            details=(
                f"Target: {target_str}\n"
                f"Database: {db_name}\n"
                f"User: {user}\n"
                f"Server: {server_info or 'Unknown'}\n"
                f"Latency: {latency_ms:.1f}ms"
            ),
        )
    except Exception as exc:
        return DiagnosticResult(
            name=test_name,
            passed=False,
            latency_ms=0.0,
            message=f"Database diagnostic error: {exc}",
            details=traceback.format_exc(),
        )
    finally:
        tunnel_stack.close()


def test_repository(
    repo_entry: dict[str, Any],
    repos_dir: str | None = None,
) -> DiagnosticResult:
    """Verify Git repository directory, branches, and ref resolution.

    Args:
        repo_entry: Repository definition mapping (name, path, default_branch, start_ref, branch_filter, etc.).
        repos_dir: Optional base directory override for relative repository paths.

    Returns:
        DiagnosticResult with branch statistics and ref resolution info.
    """
    repo_label = repo_entry.get("name") or repo_entry.get("path", "unknown")
    test_name = f"Repo: {repo_label}"
    try:
        raw_path = str(repo_entry.get("path", "")).strip()
        if not raw_path:
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message="Repository path is not configured",
                details="Configuration field 'path' is missing or empty.",
            )

        p = Path(raw_path)
        if p.is_absolute():
            repo_path = p
        elif repos_dir and (Path(repos_dir) / p).exists():
            repo_path = Path(repos_dir) / p
        else:
            repo_path = resolve_repo_path(repo_entry)

        if not repo_path.exists():
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message=f"Repository directory does not exist: {repo_path}",
                details=f"Path '{repo_path}' was not found on filesystem.",
            )

        if not is_git_repository(repo_path):
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message=f"Not a valid git repository: {repo_path}",
                details=f"Directory '{repo_path}' lacks .git directory or bare repository structure.",
            )

        t0 = time.perf_counter()
        default_branch = str(repo_entry.get("default_branch") or "HEAD")

        try:
            res_def = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "--verify", default_branch],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                message=f"Git rev-parse timed out for default branch '{default_branch}'",
                details=f"Timeout after 8.0s resolving ref '{default_branch}' in {repo_path}",
            )

        if res_def.returncode != 0:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=round(latency_ms, 2),
                message=f"Failed to resolve default branch '{default_branch}'",
                details=res_def.stderr.strip() or f"git rev-parse exited with code {res_def.returncode}",
            )

        default_sha = res_def.stdout.strip()

        start_ref = repo_entry.get("start_ref")
        start_sha = ""
        if start_ref:
            try:
                res_start = subprocess.run(
                    ["git", "-C", str(repo_path), "rev-parse", "--verify", str(start_ref)],
                    capture_output=True,
                    text=True,
                    timeout=8.0,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return DiagnosticResult(
                    name=test_name,
                    passed=False,
                    latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                    message=f"Git rev-parse timed out for start ref '{start_ref}'",
                    details=f"Timeout after 8.0s resolving start ref '{start_ref}' in {repo_path}",
                )

            if res_start.returncode != 0:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                return DiagnosticResult(
                    name=test_name,
                    passed=False,
                    latency_ms=round(latency_ms, 2),
                    message=f"Failed to resolve start ref '{start_ref}'",
                    details=res_start.stderr.strip() or f"git rev-parse exited with code {res_start.returncode}",
                )
            start_sha = res_start.stdout.strip()

        branch_filter = repo_entry.get("branch_filter")
        matching_branches = find_repository_branches(
            repo_path,
            branch_filter=branch_filter,
            default_branch=default_branch,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0

        branch_count = len(matching_branches)
        sample = ", ".join(matching_branches[:5])
        if branch_count > 5:
            sample += f", ... (+{branch_count - 5} more)"

        details_lines = [
            f"Path: {repo_path}",
            f"Default branch: {default_branch} ({default_sha[:10]})",
        ]
        if start_ref:
            details_lines.append(f"Start ref: {start_ref} ({start_sha[:10]})")
        details_lines.append(f"Matching branches ({branch_count}): {sample or 'none'}")

        return DiagnosticResult(
            name=test_name,
            passed=True,
            latency_ms=round(latency_ms, 2),
            message=f"Git repository valid ({branch_count} matching branch{'es' if branch_count != 1 else ''})",
            details="\n".join(details_lines),
        )
    except Exception as exc:
        return DiagnosticResult(
            name=test_name,
            passed=False,
            latency_ms=0.0,
            message=f"Repository diagnostic error: {exc}",
            details=traceback.format_exc(),
        )


def test_cve_catalog(cve_cfg: dict[str, Any]) -> DiagnosticResult:
    """Verify local CVE catalog clone format and git commit state.

    Args:
        cve_cfg: CVE configuration mapping (repo_dir, etc.).

    Returns:
        DiagnosticResult with catalog format and latest commit metadata.
    """
    test_name = "CVE Catalog"
    try:
        repo_dir = str(cve_cfg.get("repo_dir", "")).strip()
        if not repo_dir:
            return DiagnosticResult(
                name=test_name,
                passed=True,
                latency_ms=0.0,
                message="CVE ingestion disabled",
                details="",
            )

        p = Path(repo_dir)
        repo_path = p if p.is_absolute() else REPO_ROOT / p

        if not repo_path.exists():
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message=f"CVE directory does not exist: {repo_path}",
                details="Clone cvelistV5 or linux-vulns repository to this path or update config.json.",
            )

        if not is_git_repository(repo_path):
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message=f"CVE directory is not a git repository: {repo_path}",
                details="The CVE directory must be a valid git clone.",
            )

        # Identify format: cvelistV5 vs kernel vulns.git
        if (repo_path / "cves").is_dir():
            format_name = "cvelistV5"
        elif (repo_path / "cve" / "published").is_dir() or (repo_path / "published").is_dir():
            format_name = "kernel vulns.git"
        else:
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=0.0,
                message=f"Unrecognized CVE catalog structure in {repo_path}",
                details="Expected 'cves/' (cvelistV5 format) or 'cve/published/' (kernel vulns.git format).",
            )

        t0 = time.perf_counter()
        try:
            res = subprocess.run(
                ["git", "-C", str(repo_path), "log", "-1", "--format=%H%x09%cd%x09%s", "--date=short"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                message=f"Git log timed out inspecting CVE repository in {repo_path}",
                details="Timeout after 5.0s running git log",
            )

        latency_ms = (time.perf_counter() - t0) * 1000.0
        if res.returncode != 0:
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=round(latency_ms, 2),
                message=f"Failed to read CVE git history in {repo_path}",
                details=res.stderr.strip() or f"git log exited with code {res.returncode}",
            )

        parts = res.stdout.strip().split("\t")
        commit_hash = parts[0] if len(parts) > 0 else ""
        commit_date = parts[1] if len(parts) > 1 else ""
        commit_subject = parts[2] if len(parts) > 2 else ""

        return DiagnosticResult(
            name=test_name,
            passed=True,
            latency_ms=round(latency_ms, 2),
            message=f"CVE catalog valid ({format_name}, updated {commit_date or commit_hash[:10]})",
            details=(
                f"Path: {repo_path}\n"
                f"Format: {format_name}\n"
                f"Latest commit: {commit_hash[:10]} ({commit_date})\n"
                f"Subject: {commit_subject}"
            ),
        )
    except Exception as exc:
        return DiagnosticResult(
            name=test_name,
            passed=False,
            latency_ms=0.0,
            message=f"CVE catalog diagnostic error: {exc}",
            details=traceback.format_exc(),
        )


def _probe_ml_health(port: int, timeout: float = 1.5) -> tuple[bool, str]:
    """Probe a mailing list daemon on 127.0.0.1:<port>: GET /health, then a bare TCP connect."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8")
                try:
                    data = json.loads(body)
                    return True, f"Daemon URL: http://127.0.0.1:{port}/health\nHealth: {data}"
                except Exception:
                    return True, f"Daemon URL: http://127.0.0.1:{port}/health\nBody: {body}"
    except Exception:
        pass
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True, f"Daemon TCP port {port} is open and accepting connections."
    except OSError:
        return False, ""


def _test_mailing_list_tunnel(ml_cfg: dict[str, Any], ssh_cfg: dict[str, Any], test_name: str) -> DiagnosticResult:
    """Remote mode: open the mailing list SSH forward and probe the daemon through it."""
    t0 = time.perf_counter()
    ssh_host = str(ssh_cfg.get("host", "") or "").strip()
    ssh_port = ssh_cfg.get("port", 22)
    if not ssh_host:
        return DiagnosticResult(
            name=test_name,
            passed=False,
            latency_ms=0.0,
            message="Mailing list SSH tunnel is enabled but SSH host is not configured",
            details="Configuration field 'mailing_list.ssh_tunnel.host' is empty.",
        )

    remote_host = str(ssh_cfg.get("remote_host", "") or "127.0.0.1")
    remote_port = _tunnel_remote_port(ssh_cfg, "mailing_list")
    try:
        with open_test_tunnel(ssh_cfg, "mailing_list") as local_port:
            healthy, health_details = _probe_ml_health(local_port, timeout=3.0)
    except Exception as exc:
        return DiagnosticResult(
            name=test_name,
            passed=False,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            message=f"SSH tunnel via {ssh_host}:{ssh_port} could not be established: {exc}",
            details=f"Forward target: {remote_host}:{remote_port}\n{exc}",
        )

    latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    forward = f"Forward: 127.0.0.1:{local_port} -> {remote_host}:{remote_port} via {ssh_host}:{ssh_port}"
    if healthy:
        return DiagnosticResult(
            name=test_name,
            passed=True,
            latency_ms=latency_ms,
            message=f"Remote mailing list daemon responding through SSH tunnel ({ssh_host})",
            details=f"{forward}\n{health_details}",
        )
    if ml_cfg.get("auto_start_daemon", True):
        return DiagnosticResult(
            name=test_name,
            passed=True,
            latency_ms=latency_ms,
            message=f"SSH tunnel to {ssh_host} established; remote daemon is not running yet (auto-started on ingest)",
            details=f"{forward}\nNo daemon answered on remote port {remote_port}; auto_start_daemon launches it over SSH.",
        )
    return DiagnosticResult(
        name=test_name,
        passed=False,
        latency_ms=latency_ms,
        message=f"SSH tunnel to {ssh_host} established but no daemon is listening on remote port {remote_port}",
        details=f"{forward}\nStart the daemon on the remote host or enable 'auto_start_daemon'.",
    )


def test_mailing_list(ml_cfg: dict[str, Any]) -> DiagnosticResult:
    """Verify the mailing list backend: remote daemon through its SSH tunnel, or local daemon/storage.

    With `ssh_tunnel.enabled`, opens the forward (`open_test_tunnel`) and probes the remote
    daemon's /health through it. Otherwise probes a local daemon on 127.0.0.1:<port> and
    falls back to checking `storage_dir` readability.

    Args:
        ml_cfg: Mailing list configuration mapping (storage_dir, port, ssh_tunnel, etc.).

    Returns:
        DiagnosticResult with reachability and storage status.
    """
    test_name = "Mailing List"
    try:
        if ml_cfg.get("enabled") is False:
            return DiagnosticResult(
                name=test_name,
                passed=True,
                latency_ms=0.0,
                message="Mailing list ingestion is disabled",
                details="",
            )

        ssh_cfg = ml_cfg.get("ssh_tunnel") or {}
        if isinstance(ssh_cfg, dict) and ssh_cfg.get("enabled"):
            return _test_mailing_list_tunnel(ml_cfg, ssh_cfg, test_name)

        t0 = time.perf_counter()
        port = int(ml_cfg.get("port", 8192))
        storage_dir = str(ml_cfg.get("storage_dir", "")).strip()

        # Check if local daemon is actively responding on 127.0.0.1:<port>
        daemon_healthy, daemon_details = _probe_ml_health(port)

        if daemon_healthy:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return DiagnosticResult(
                name=test_name,
                passed=True,
                latency_ms=round(latency_ms, 2),
                message=f"Mailing list daemon responding on port {port}",
                details=daemon_details,
            )

        # Daemon is not active; inspect local storage_dir
        if not storage_dir:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=round(latency_ms, 2),
                message="No storage directory configured and daemon not responding",
                details=f"Daemon is not listening on port {port} and 'storage_dir' is empty.",
            )

        p = Path(storage_dir)
        if not p.exists():
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=round(latency_ms, 2),
                message=f"Storage directory does not exist: {storage_dir}",
                details=f"Path '{p}' was not found on local filesystem.",
            )

        if not os.access(p, os.R_OK):
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return DiagnosticResult(
                name=test_name,
                passed=False,
                latency_ms=round(latency_ms, 2),
                message=f"Storage directory is not readable: {storage_dir}",
                details=f"Read permission denied for '{p}'.",
            )

        latency_ms = (time.perf_counter() - t0) * 1000.0
        return DiagnosticResult(
            name=test_name,
            passed=True,
            latency_ms=round(latency_ms, 2),
            message=f"Local mailing list store accessible ({storage_dir})",
            details=f"Storage directory: {p.resolve()}\nReadable: True",
        )
    except Exception as exc:
        return DiagnosticResult(
            name=test_name,
            passed=False,
            latency_ms=0.0,
            message=f"Mailing list diagnostic error: {exc}",
            details=traceback.format_exc(),
        )


def run_all_diagnostics(config_data: dict[str, Any]) -> list[DiagnosticResult]:
    """Run diagnostics across Database, SSH tunnel(s), Repositories, CVE catalog, and Mailing List.

    Args:
        config_data: Full configuration mapping (e.g. loaded from config.json).

    Returns:
        List of DiagnosticResult objects for all evaluated components.
    """
    results: list[DiagnosticResult] = []

    # 1. SSH Tunnels
    db_cfg = config_data.get("database", {})
    ssh_tunnel_db = db_cfg.get("ssh_tunnel") or config_data.get("ssh_tunnel")
    if ssh_tunnel_db is not None:
        try:
            results.append(test_ssh_tunnel(ssh_tunnel_db, feature_name="database"))
        except Exception as exc:
            results.append(
                DiagnosticResult(
                    name="SSH Tunnel (database)",
                    passed=False,
                    latency_ms=0.0,
                    message=f"Unexpected error in SSH tunnel diagnostic: {exc}",
                    details=traceback.format_exc(),
                )
            )

    ml_cfg = config_data.get("mailing_list", {})
    ssh_tunnel_ml = ml_cfg.get("ssh_tunnel")
    if ssh_tunnel_ml and ssh_tunnel_ml.get("enabled"):
        try:
            results.append(test_ssh_tunnel(ssh_tunnel_ml, feature_name="mailing_list"))
        except Exception as exc:
            results.append(
                DiagnosticResult(
                    name="SSH Tunnel (mailing_list)",
                    passed=False,
                    latency_ms=0.0,
                    message=f"Unexpected error in SSH tunnel diagnostic: {exc}",
                    details=traceback.format_exc(),
                )
            )

    # 2. Database Connection
    try:
        results.append(test_database_connection(db_cfg, ssh_tunnel_cfg=ssh_tunnel_db))
    except Exception as exc:
        results.append(
            DiagnosticResult(
                name="Database Connection",
                passed=False,
                latency_ms=0.0,
                message=f"Unexpected error in database diagnostic: {exc}",
                details=traceback.format_exc(),
            )
        )

    # 3. Repositories
    repos = config_data.get("repositories", [])
    repos_dir = (config_data.get("workspace") or {}).get("repos_dir")
    enabled_repos = [r for r in repos if isinstance(r, dict) and r.get("enabled", True)]

    if not enabled_repos:
        results.append(
            DiagnosticResult(
                name="Repositories",
                passed=False,
                latency_ms=0.0,
                message="No enabled repositories configured",
                details="Configure at least one repository with 'enabled': true under 'repositories'.",
            )
        )
    else:
        for repo in enabled_repos:
            try:
                results.append(test_repository(repo, repos_dir=repos_dir))
            except Exception as exc:
                repo_name = f"Repo: {repo.get('name') or repo.get('path', 'unknown')}"
                results.append(
                    DiagnosticResult(
                        name=repo_name,
                        passed=False,
                        latency_ms=0.0,
                        message=f"Unexpected error testing repository: {exc}",
                        details=traceback.format_exc(),
                    )
                )

    # 4. CVE Catalog
    cve_cfg = config_data.get("cve", {})
    try:
        results.append(test_cve_catalog(cve_cfg))
    except Exception as exc:
        results.append(
            DiagnosticResult(
                name="CVE Catalog",
                passed=False,
                latency_ms=0.0,
                message=f"Unexpected error testing CVE catalog: {exc}",
                details=traceback.format_exc(),
            )
        )

    # 5. Mailing List
    try:
        results.append(test_mailing_list(ml_cfg))
    except Exception as exc:
        results.append(
            DiagnosticResult(
                name="Mailing List",
                passed=False,
                latency_ms=0.0,
                message=f"Unexpected error testing mailing list: {exc}",
                details=traceback.format_exc(),
            )
        )

    return results
