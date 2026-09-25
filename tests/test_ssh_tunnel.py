"""tests/test_ssh_tunnel.py - Unit Tests for SSH Tunnel Port Forwarding & Configuration.

Tests configuration parsing, environment variable overrides, command-line argument building,
ephemeral port selection, lifecycle management, and error handling for the SSH tunnel manager.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.config import (
    DEFAULT_CONFIG,
    get_config,
    get_db_config,
    get_ssh_tunnel_config,
    init_config,
    reset_config,
)
from core.ssh_tunnel import (
    SSHTunnelManager,
    _find_free_port,
    _is_port_in_use,
    is_ssh_tunnel_active,
    start_ssh_tunnel,
    stop_ssh_tunnel,
)


class TestSSHTunnelConfig(unittest.TestCase):
    """Test configuration loading and precedence rules for SSH tunnel."""

    def setUp(self) -> None:
        reset_config()

    def tearDown(self) -> None:
        reset_config()

    def test_default_config_disabled(self) -> None:
        """SSH tunnel must be disabled by default in built-in configuration."""
        with patch("core.config.find_config_path", return_value=None):
            reset_config()
            cfg = get_ssh_tunnel_config()
            self.assertFalse(cfg["enabled"])
            self.assertEqual(cfg["port"], 22)
            self.assertEqual(cfg["remote_host"], "127.0.0.1")
            self.assertEqual(cfg["remote_port"], 3306)
            self.assertEqual(cfg["local_port"], 0)
            self.assertEqual(cfg["strict_host_key_checking"], "accept-new")

    def test_env_var_overrides(self) -> None:
        """Environment variables must override default configuration."""
        env_overrides = {
            "SSH_TUNNEL_ENABLED": "true",
            "SSH_HOST": "remote.mariadb.org",
            "SSH_PORT": "2222",
            "SSH_USER": "testuser",
            "SSH_PASSWORD": "secretpassword",
            "SSH_KEY_FILE": "/tmp/id_rsa",
            "SSH_REMOTE_HOST": "db.internal",
            "SSH_REMOTE_PORT": "3307",
            "SSH_LOCAL_PORT": "3308",
            "SSH_STRICT_HOST_KEY_CHECKING": "no",
            "SSH_CONNECT_TIMEOUT": "15",
        }
        with patch.dict(os.environ, env_overrides, clear=False):
            reset_config()
            cfg = get_ssh_tunnel_config()
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["host"], "remote.mariadb.org")
            self.assertEqual(cfg["port"], 2222)
            self.assertEqual(cfg["user"], "testuser")
            self.assertEqual(cfg["password"], "secretpassword")
            self.assertEqual(cfg["key_file"], "/tmp/id_rsa")
            self.assertEqual(cfg["remote_host"], "db.internal")
            self.assertEqual(cfg["remote_port"], 3307)
            self.assertEqual(cfg["local_port"], 3308)
            self.assertEqual(cfg["strict_host_key_checking"], "no")
            self.assertEqual(cfg["connect_timeout"], 15)


class TestSSHTunnelManager(unittest.TestCase):
    """Test SSH command generation and lifecycle execution."""

    def setUp(self) -> None:
        reset_config()
        stop_ssh_tunnel()

    def tearDown(self) -> None:
        stop_ssh_tunnel()
        reset_config()

    def test_find_free_port(self) -> None:
        """_find_free_port should return a valid unbound ephemeral port."""
        port = _find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 1024)

    def test_build_command_basic(self) -> None:
        """Verify command construction with host, port, user."""
        manager = SSHTunnelManager(
            host="192.168.1.100",
            port=2222,
            user="dbadmin",
            remote_host="127.0.0.1",
            remote_port=3306,
        )
        cmd, env = manager.build_command(local_port=44444)
        cmd_str = " ".join(cmd)

        self.assertIn("-N", cmd)
        self.assertIn("-L 44444:127.0.0.1:3306", cmd_str)
        self.assertIn("-p 2222", cmd_str)
        self.assertIn("-o ExitOnForwardFailure=yes", cmd_str)
        self.assertIn("-o ServerAliveInterval=15", cmd_str)
        self.assertIn("-o ServerAliveCountMax=3", cmd_str)
        self.assertIn("-o StrictHostKeyChecking=accept-new", cmd_str)
        self.assertTrue(cmd[-1].endswith("dbadmin@192.168.1.100"))

    def test_build_command_key_file(self) -> None:
        """Verify command includes key file argument."""
        with tempfile.NamedTemporaryFile() as tf:
            manager = SSHTunnelManager(
                host="192.168.1.100",
                key_file=tf.name,
            )
            cmd, _ = manager.build_command(local_port=44444)
            cmd_str = " ".join(cmd)
            self.assertIn(f"-i {tf.name}", cmd_str)
            self.assertIn("-o IdentitiesOnly=yes", cmd_str)

    def test_build_command_password_askpass(self) -> None:
        """Verify password authentication configures SSH_ASKPASS when sshpass is missing."""
        manager = SSHTunnelManager(
            host="192.168.1.100",
            password="supersecretpass",
        )
        with patch("shutil.which", return_value=None):
            cmd, env = manager.build_command(local_port=44444)
            self.assertIn("SSH_ASKPASS", env)
            self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
            askpass_file = Path(env["SSH_ASKPASS"])
            self.assertTrue(askpass_file.exists())
            # Check script has execute permission
            file_stat = os.stat(askpass_file)
            self.assertTrue(bool(file_stat.st_mode & stat.S_IXUSR))

            # Clean up askpass
            manager._cleanup_askpass()
            self.assertFalse(askpass_file.exists())

    @patch("subprocess.Popen")
    @patch("core.ssh_tunnel._is_port_in_use")
    def test_start_and_stop_success(self, mock_port_in_use: MagicMock, mock_popen: MagicMock) -> None:
        """Test successful tunnel start, config redirection, and stop."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc

        # Port appears ready on second check
        mock_port_in_use.side_effect = [False, True, True]

        manager = SSHTunnelManager(
            host="db.remote.com",
            port=22,
            local_port=39999,
            connect_timeout=2,
        )

        bound_port = manager.start()
        self.assertEqual(bound_port, 39999)
        self.assertTrue(manager.is_active())

        # Verify database config was rerouted
        db_cfg = get_db_config()
        self.assertEqual(db_cfg["host"], "127.0.0.1")
        self.assertEqual(db_cfg["port"], 39999)
        self.assertEqual(os.environ.get("DB_HOST"), "127.0.0.1")
        self.assertEqual(os.environ.get("DB_PORT"), "39999")

        # Stop tunnel
        manager.stop()
        mock_proc.terminate.assert_called_once()
        self.assertFalse(manager.is_active())

    @patch("subprocess.Popen")
    def test_start_premature_exit(self, mock_popen: MagicMock) -> None:
        """Test error raised when SSH process terminates immediately."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 255
        mock_proc.returncode = 255
        mock_proc.stderr.read.return_value = "Permission denied (publickey)."
        mock_popen.return_value = mock_proc

        manager = SSHTunnelManager(
            host="db.remote.com",
            local_port=39999,
            connect_timeout=2,
        )

        with self.assertRaises(RuntimeError) as ctx:
            manager.start()
        self.assertIn("Permission denied", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
