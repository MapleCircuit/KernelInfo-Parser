"""core/ssh_tunnel.py - SSH Port Forwarding Tunnel Manager for KernelInfo-Parser.

Manages background OpenSSH subprocess tunnels to securely forward remote MariaDB
ports to local interfaces. Automatically updates active configuration and os.environ
so that TableEngine, MariaDB drivers, and multicore worker processes route queries
transparently through the encrypted tunnel.
"""
from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

from core.config import get_config, get_ssh_tunnel_config, sync_environ

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    """Find and return an available ephemeral TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return int(s.getsockname()[1])


def _is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check if target host:port is currently accepting TCP connections."""
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except (OSError, ConnectionRefusedError):
        return False


class SSHTunnelManager:
    """Manages the lifecycle of an OpenSSH port forwarding subprocess."""

    def __init__(
        self,
        host: str,
        port: int = 22,
        user: str = "",
        password: str = "",
        key_file: str = "",
        remote_host: str = "127.0.0.1",
        remote_port: int = 3306,
        local_port: int = 0,
        strict_host_key_checking: str = "accept-new",
        connect_timeout: int = 10,
    ) -> None:
        self.host = host.strip()
        self.port = int(port or 22)
        self.user = user.strip()
        self.password = password
        self.key_file = key_file.strip()
        self.remote_host = remote_host.strip() or "127.0.0.1"
        self.remote_port = int(remote_port or 3306)
        self.configured_local_port = int(local_port or 0)
        self.local_port: int = 0
        self.strict_host_key_checking = strict_host_key_checking.strip() or "accept-new"
        self.connect_timeout = max(1, int(connect_timeout or 10))

        self.proc: subprocess.Popen[str] | None = None
        self._askpass_path: Path | None = None
        self._is_active = False

    def is_active(self) -> bool:
        """Return True if the SSH tunnel subprocess is currently alive and active."""
        if not self._is_active or self.proc is None:
            return False
        return self.proc.poll() is None

    def _setup_askpass(self, env: dict[str, str]) -> None:
        """Create a temporary executable script to supply password non-interactively via SSH_ASKPASS."""
        if not self.password:
            return

        # Write a secure python script that prints the password to stdout
        tf = tempfile.NamedTemporaryFile(
            mode="w",
            prefix="ssh_askpass_",
            suffix=".py",
            delete=False,
            encoding="utf-8",
        )
        tf.write(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.stdout.write({repr(self.password)} + '\\n')\n"
        )
        tf.flush()
        tf.close()

        askpass_path = Path(tf.name).resolve()
        # Set execute permission for current user only (0700)
        os.chmod(askpass_path, stat.S_IRWXU)
        self._askpass_path = askpass_path

        env["SSH_ASKPASS"] = str(askpass_path)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        # OpenSSH requires DISPLAY to be set if SSH_ASKPASS is invoked in some environments
        if "DISPLAY" not in env:
            env["DISPLAY"] = ":0"

    def _cleanup_askpass(self) -> None:
        """Remove any temporary askpass script."""
        if self._askpass_path and self._askpass_path.exists():
            try:
                self._askpass_path.unlink()
            except OSError as exc:
                logger.debug("Failed to remove askpass script %s: %s", self._askpass_path, exc)
            finally:
                self._askpass_path = None

    def build_command(self, local_port: int) -> tuple[list[str], dict[str, str]]:
        """Construct the SSH command line and environment dictionary."""
        ssh_bin = shutil.which("ssh") or "/usr/bin/ssh"
        cmd = [ssh_bin]

        # Options for reliable, non-blocking port forwarding
        cmd.extend([
            "-N",  # Do not execute a remote command; only forward ports
            "-L", f"{local_port}:{self.remote_host}:{self.remote_port}",
            "-p", str(self.port),
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", f"StrictHostKeyChecking={self.strict_host_key_checking}",
            "-o", "BatchMode=yes" if not self.password else "BatchMode=no",
        ])

        # SSH key file authentication
        if self.key_file:
            expanded_key = Path(os.path.expanduser(self.key_file)).resolve()
            if not expanded_key.is_file():
                logger.warning("Specified SSH key file does not exist: %s", expanded_key)
            cmd.extend([
                "-i", str(expanded_key),
                "-o", "IdentitiesOnly=yes",
            ])

        # Target host specification
        target = f"{self.user}@{self.host}" if self.user else self.host
        cmd.append(target)

        env = os.environ.copy()

        # Handle password authentication
        if self.password:
            # Check if sshpass is available
            sshpass_bin = shutil.which("sshpass")
            if sshpass_bin:
                cmd = [sshpass_bin, "-p", self.password] + cmd
            else:
                self._setup_askpass(env)

        return cmd, env

    def start(self) -> int:
        """Spawn the SSH tunnel subprocess, wait for port forward readiness, and update config.

        Returns:
            int: The local port bound to the remote MariaDB server.

        Raises:
            RuntimeError: If tunnel fails to establish or process terminates with error.
        """
        if self.is_active():
            logger.info("SSH tunnel already active on local port %d", self.local_port)
            return self.local_port

        if not self.host:
            raise ValueError("Cannot start SSH tunnel: 'host' is not configured.")

        # Determine local port: use configured_local_port if available, otherwise find free ephemeral port
        if self.configured_local_port > 0 and not _is_port_in_use(self.configured_local_port):
            self.local_port = self.configured_local_port
        else:
            if self.configured_local_port > 0:
                logger.warning(
                    "Configured local port %d is already in use; allocating ephemeral port.",
                    self.configured_local_port,
                )
            self.local_port = _find_free_port()

        cmd, env = self.build_command(self.local_port)
        logger.info(
            "Establishing SSH tunnel: 127.0.0.1:%d -> %s:%d (via %s:%d)",
            self.local_port,
            self.remote_host,
            self.remote_port,
            self.host,
            self.port,
        )

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                env=env,
            )
        except Exception as exc:
            self._cleanup_askpass()
            raise RuntimeError(f"Failed to spawn SSH process: {exc}") from exc

        # Wait for tunnel readiness: probe local port until listening or process dies
        start_time = time.time()
        ready = False
        while time.time() - start_time < self.connect_timeout:
            # Check if process died early
            if self.proc.poll() is not None:
                stderr = self.proc.stderr.read() if self.proc.stderr else ""
                self._cleanup_askpass()
                raise RuntimeError(
                    f"SSH tunnel process exited prematurely with code {self.proc.returncode}: {stderr.strip()}"
                )

            # Test connection to the local forwarded port
            if _is_port_in_use(self.local_port):
                ready = True
                break

            time.sleep(0.1)

        if not ready:
            self.stop()
            raise RuntimeError(
                f"SSH tunnel failed to establish forward on 127.0.0.1:{self.local_port} "
                f"within {self.connect_timeout}s timeout."
            )

        self._is_active = True

        # Clean up temporary askpass script now that auth is complete
        self._cleanup_askpass()

        # Update active database configuration and synchronize environment variables
        active_cfg = get_config()
        active_db = active_cfg.get("database", {})
        active_db["host"] = "127.0.0.1"
        active_db["port"] = self.local_port
        sync_environ(active_cfg)

        logger.info(
            "SSH tunnel active! Rerouted database connection to 127.0.0.1:%d",
            self.local_port,
        )
        return self.local_port

    def stop(self) -> None:
        """Safely terminate the SSH tunnel process and clean up resources."""
        self._cleanup_askpass()
        if self.proc is not None:
            if self.proc.poll() is None:
                logger.info("Stopping SSH tunnel (PID %d)...", self.proc.pid)
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=2.0)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        self.proc.kill()
                    except OSError:
                        pass
            self.proc = None
        self._is_active = False


_GLOBAL_TUNNEL: SSHTunnelManager | None = None


def start_ssh_tunnel(cfg: dict[str, Any] | None = None) -> SSHTunnelManager:
    """Initialize and start the global SSH tunnel singleton based on active configuration."""
    global _GLOBAL_TUNNEL
    if _GLOBAL_TUNNEL is not None and _GLOBAL_TUNNEL.is_active():
        return _GLOBAL_TUNNEL

    tunnel_cfg = cfg or get_ssh_tunnel_config()
    manager = SSHTunnelManager(
        host=tunnel_cfg.get("host", ""),
        port=tunnel_cfg.get("port", 22),
        user=tunnel_cfg.get("user", ""),
        password=tunnel_cfg.get("password", ""),
        key_file=tunnel_cfg.get("key_file", ""),
        remote_host=tunnel_cfg.get("remote_host", "127.0.0.1"),
        remote_port=tunnel_cfg.get("remote_port", 3306),
        local_port=tunnel_cfg.get("local_port", 0),
        strict_host_key_checking=tunnel_cfg.get("strict_host_key_checking", "accept-new"),
        connect_timeout=tunnel_cfg.get("connect_timeout", 10),
    )
    manager.start()
    _GLOBAL_TUNNEL = manager
    return manager


def stop_ssh_tunnel() -> None:
    """Terminate the global SSH tunnel singleton if active."""
    global _GLOBAL_TUNNEL
    if _GLOBAL_TUNNEL is not None:
        try:
            _GLOBAL_TUNNEL.stop()
        finally:
            _GLOBAL_TUNNEL = None


def is_ssh_tunnel_active() -> bool:
    """Check if the global SSH tunnel is currently active."""
    return _GLOBAL_TUNNEL is not None and _GLOBAL_TUNNEL.is_active()


# Automatically ensure tunnel teardown on interpreter exit
atexit.register(stop_ssh_tunnel)
