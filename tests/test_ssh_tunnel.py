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
    get_mailing_list_config,
    get_ssh_tunnel_config,
    init_config,
    reset_config,
)
from core.ssh_tunnel import (
    SSHTunnelManager,
    _find_free_port,
    _is_port_in_use,
    get_active_ssh_tunnel,
    is_ssh_tunnel_active,
    start_ssh_tunnel,
    stop_ssh_tunnel,
)


class TestSSHTunnelConfig(unittest.TestCase):
    """Test configuration loading and precedence rules for SSH tunnel."""

    def setUp(self) -> None:
        import copy
        import core.config
        self._orig_cfg = copy.deepcopy(core.config._ACTIVE_CONFIG)
        self._orig_cfg_path = core.config._LOADED_CONFIG_PATH
        reset_config()

    def tearDown(self) -> None:
        import copy
        import core.config
        reset_config()
        core.config._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        core.config._LOADED_CONFIG_PATH = self._orig_cfg_path

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

    def test_per_feature_config(self) -> None:
        """Database and mailing list features have distinct SSH tunnel configurations."""
        with patch("core.config.find_config_path", return_value=None):
            reset_config()
            db_tunnel = get_ssh_tunnel_config("database")
            ml_tunnel = get_ssh_tunnel_config("mailing_list")
            self.assertEqual(db_tunnel["remote_port"], 3306)
            self.assertEqual(ml_tunnel["remote_port"], 8192)
            self.assertFalse(db_tunnel["enabled"])
            self.assertFalse(ml_tunnel["enabled"])

            ml_cfg = get_mailing_list_config()
            self.assertIn("ssh_tunnel", ml_cfg)
            self.assertEqual(ml_cfg["ssh_tunnel"]["remote_port"], 8192)

    def test_legacy_flat_ssh_tunnel_backward_compatibility(self) -> None:
        """Legacy flat top-level ssh_tunnel in config files must be migrated to database.ssh_tunnel."""
        import json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            json.dump({
                "ssh_tunnel": {
                    "enabled": True,
                    "host": "legacy.remote.org",
                    "port": 2200,
                    "remote_port": 3306,
                }
            }, tf)
            tf_path = Path(tf.name)

        try:
            with patch("core.config.find_config_path", return_value=tf_path):
                reset_config()
                cfg_db = get_ssh_tunnel_config("database")
                cfg_default = get_ssh_tunnel_config()
                self.assertTrue(cfg_db["enabled"])
                self.assertEqual(cfg_db["host"], "legacy.remote.org")
                self.assertEqual(cfg_db["port"], 2200)
                self.assertEqual(cfg_default["host"], "legacy.remote.org")
        finally:
            tf_path.unlink(missing_ok=True)


class TestSSHTunnelManager(unittest.TestCase):
    """Test SSH command generation and lifecycle execution."""

    def setUp(self) -> None:
        import copy
        import core.config
        import core.ssh_tunnel
        self._orig_env = dict(os.environ)
        self._orig_cfg = copy.deepcopy(core.config._ACTIVE_CONFIG)
        self._orig_cfg_path = core.config._LOADED_CONFIG_PATH
        self._orig_tunnels = dict(core.ssh_tunnel._TUNNELS)
        self._orig_global = core.ssh_tunnel._GLOBAL_TUNNEL
        core.ssh_tunnel._TUNNELS = {}
        core.ssh_tunnel._GLOBAL_TUNNEL = None
        reset_config()

    def tearDown(self) -> None:
        import copy
        import core.config
        import core.ssh_tunnel
        stop_ssh_tunnel()
        core.ssh_tunnel._TUNNELS.clear()
        core.ssh_tunnel._TUNNELS.update(self._orig_tunnels)
        core.ssh_tunnel._GLOBAL_TUNNEL = self._orig_global
        reset_config()
        os.environ.clear()
        os.environ.update(self.original_env if hasattr(self, "original_env") else self._orig_env)
        core.config._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        core.config._LOADED_CONFIG_PATH = self._orig_cfg_path

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

    @patch("subprocess.Popen")
    @patch("core.ssh_tunnel._is_port_in_use")
    def test_per_feature_multi_tunnel_lifecycle(self, mock_port_in_use: MagicMock, mock_popen: MagicMock) -> None:
        """Verify multiple feature tunnels can be started, queried, and stopped independently."""
        mock_proc1 = MagicMock()
        mock_proc1.poll.return_value = None
        mock_proc1.pid = 11111

        mock_proc2 = MagicMock()
        mock_proc2.poll.return_value = None
        mock_proc2.pid = 22222

        mock_popen.side_effect = [mock_proc1, mock_proc2]
        mock_port_in_use.side_effect = [False, True, False, True]

        # Start mailing list tunnel
        ml_cfg = {
            "enabled": True,
            "host": "mail.server.org",
            "port": 22,
            "remote_host": "127.0.0.1",
            "remote_port": 993,
            "local_port": 40001,
            "connect_timeout": 2,
        }
        start_ssh_tunnel(ml_cfg, feature="mailing_list")

        self.assertTrue(is_ssh_tunnel_active("mailing_list"))
        self.assertFalse(is_ssh_tunnel_active("database"))

        active_cfg = get_config()
        self.assertEqual(active_cfg.get("mailing_list", {}).get("host"), "127.0.0.1")
        self.assertEqual(active_cfg.get("mailing_list", {}).get("port"), 40001)

        # Database config should remain untouched
        db_cfg = get_db_config()
        self.assertNotEqual(db_cfg.get("port"), 40001)

        # Start database tunnel
        db_tunnel_cfg = {
            "enabled": True,
            "host": "db.server.org",
            "port": 22,
            "remote_host": "127.0.0.1",
            "remote_port": 3306,
            "local_port": 40002,
            "connect_timeout": 2,
        }
        start_ssh_tunnel(db_tunnel_cfg, feature="database")

        self.assertTrue(is_ssh_tunnel_active("mailing_list"))
        self.assertTrue(is_ssh_tunnel_active("database"))
        self.assertEqual(get_db_config()["port"], 40002)

        # Stop mailing list tunnel specifically
        stop_ssh_tunnel("mailing_list")
        mock_proc1.terminate.assert_called_once()
        self.assertFalse(is_ssh_tunnel_active("mailing_list"))
        self.assertTrue(is_ssh_tunnel_active("database"))

        # Stop all tunnels
        stop_ssh_tunnel()
        mock_proc2.terminate.assert_called_once()
        self.assertFalse(is_ssh_tunnel_active("database"))


if __name__ == "__main__":
    unittest.main()
