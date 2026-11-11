"""tests/test_tui_testing.py - Unit tests for headless diagnostic testing engine."""

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.tui.testing import (
    DiagnosticResult,
    open_test_tunnel,
    run_all_diagnostics,
    test_cve_catalog,
    test_database_connection,
    test_mailing_list,
    test_repository,
    test_ssh_tunnel,
)


def _fake_tunnel(local_port: int = 40001, exc: Exception | None = None, calls: list | None = None):
    """Stand-in for open_test_tunnel: yields local_port, or raises exc on entry."""

    @contextmanager
    def _cm(ssh_cfg, feature_name="database"):
        if calls is not None:
            calls.append((dict(ssh_cfg), feature_name))
        if exc is not None:
            raise exc
        yield local_port

    return _cm


class TestOpenTestTunnel(unittest.TestCase):
    """Test open_test_tunnel lifecycle."""

    @patch("core.ssh_tunnel.get_active_ssh_tunnel", return_value=None)
    @patch("core.ssh_tunnel.SSHTunnelManager")
    def test_temporary_tunnel_started_and_stopped(self, mock_mgr_cls, _mock_active) -> None:
        mgr = mock_mgr_cls.return_value
        mgr.local_port = 45555
        cfg = {"enabled": True, "host": "gw", "port": 2222, "user": "u", "password": "pw", "local_port": 3308}
        with open_test_tunnel(cfg, "database") as port:
            self.assertEqual(port, 45555)
            mgr.start.assert_called_once()
            mgr.stop.assert_not_called()
        mgr.stop.assert_called_once()
        kwargs = mock_mgr_cls.call_args.kwargs
        self.assertEqual(kwargs["feature"], "tui-test:database")
        self.assertEqual(kwargs["host"], "gw")
        self.assertEqual(kwargs["port"], 2222)
        self.assertEqual(kwargs["password"], "pw")
        self.assertEqual(kwargs["remote_port"], 3306)
        self.assertEqual(kwargs["local_port"], 3308)

    @patch("core.ssh_tunnel.get_active_ssh_tunnel", return_value=None)
    @patch("core.ssh_tunnel.SSHTunnelManager")
    def test_mailing_list_default_remote_port(self, mock_mgr_cls, _mock_active) -> None:
        mock_mgr_cls.return_value.local_port = 1
        with open_test_tunnel({"host": "gw"}, "mailing_list"):
            pass
        self.assertEqual(mock_mgr_cls.call_args.kwargs["remote_port"], 8192)

    @patch("core.ssh_tunnel.SSHTunnelManager")
    @patch("core.ssh_tunnel.get_active_ssh_tunnel")
    def test_reuses_active_tunnel(self, mock_active, mock_mgr_cls) -> None:
        mock_active.return_value = MagicMock(local_port=3310)
        with open_test_tunnel({"host": "gw"}, "database") as port:
            self.assertEqual(port, 3310)
        mock_mgr_cls.assert_not_called()
        mock_active.return_value.stop.assert_not_called()

    @patch("core.ssh_tunnel.get_active_ssh_tunnel", return_value=None)
    @patch("core.ssh_tunnel.SSHTunnelManager")
    def test_start_failure_propagates(self, mock_mgr_cls, _mock_active) -> None:
        mock_mgr_cls.return_value.start.side_effect = RuntimeError("Permission denied (publickey)")
        with self.assertRaises(RuntimeError):
            with open_test_tunnel({"host": "gw"}, "database"):
                self.fail("body must not run")


class TestDiagnosticResult(unittest.TestCase):
    """Test DiagnosticResult dataclass behavior and defaults."""

    def test_dataclass_fields(self) -> None:
        res = DiagnosticResult(
            name="Test Component",
            passed=True,
            latency_ms=12.5,
            message="All systems nominal",
        )
        self.assertEqual(res.name, "Test Component")
        self.assertTrue(res.passed)
        self.assertEqual(res.latency_ms, 12.5)
        self.assertEqual(res.message, "All systems nominal")
        self.assertEqual(res.details, "")

        res_detailed = DiagnosticResult(
            name="Failed Component",
            passed=False,
            latency_ms=45.0,
            message="Connection refused",
            details="Traceback details here",
        )
        self.assertFalse(res_detailed.passed)
        self.assertEqual(res_detailed.details, "Traceback details here")


class TestSSHTunnelDiagnostic(unittest.TestCase):
    """Test test_ssh_tunnel diagnostics."""

    def test_ssh_tunnel_disabled(self) -> None:
        cfg = {"enabled": False}
        res = test_ssh_tunnel(cfg, feature_name="database")
        self.assertTrue(res.passed)
        self.assertEqual(res.name, "SSH Tunnel (database)")
        self.assertIn("disabled", res.message)

    def test_ssh_tunnel_empty_host(self) -> None:
        cfg = {"enabled": True, "host": "", "port": 22}
        res = test_ssh_tunnel(cfg, feature_name="database")
        self.assertFalse(res.passed)
        self.assertIn("host is not configured", res.message)

    def test_ssh_tunnel_invalid_port(self) -> None:
        for bad_port in (-1, 0, 70000, "not_a_port"):
            cfg = {"enabled": True, "host": "127.0.0.1", "port": bad_port}
            res = test_ssh_tunnel(cfg, feature_name="database")
            self.assertFalse(res.passed)
            self.assertIn("Invalid SSH port", res.message)

    def test_ssh_tunnel_missing_key_file(self) -> None:
        cfg = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 22,
            "key_file": "/tmp/non_existent_key_123456789.pem",
        }
        res = test_ssh_tunnel(cfg, feature_name="database")
        self.assertFalse(res.passed)
        self.assertIn("key file not found", res.message)

    @patch("os.access")
    @patch("pathlib.Path.exists")
    def test_ssh_tunnel_unreadable_key_file(self, mock_exists, mock_access) -> None:
        mock_exists.return_value = True
        mock_access.return_value = False
        cfg = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 22,
            "key_file": "/tmp/unreadable_key.pem",
        }
        res = test_ssh_tunnel(cfg, feature_name="database")
        self.assertFalse(res.passed)
        self.assertIn("not readable", res.message)

    @patch("socket.create_connection")
    def test_ssh_tunnel_reachable_no_establish(self, mock_connect) -> None:
        mock_connect.return_value.__enter__.return_value = MagicMock()
        cfg = {"enabled": True, "host": "192.168.1.10", "port": 2222}
        with patch("core.tui.testing.open_test_tunnel") as mock_tunnel:
            res = test_ssh_tunnel(cfg, feature_name="database", establish=False)
        self.assertTrue(res.passed)
        self.assertIn("reachable", res.message)
        mock_tunnel.assert_not_called()

    @patch("socket.create_connection")
    def test_ssh_tunnel_established(self, mock_connect) -> None:
        mock_connect.return_value.__enter__.return_value = MagicMock()
        cfg = {"enabled": True, "host": "192.168.1.10", "port": 2222, "remote_port": 3306}
        calls: list = []
        with patch("core.tui.testing.open_test_tunnel", _fake_tunnel(40123, calls=calls)):
            res = test_ssh_tunnel(cfg, feature_name="database")
        self.assertTrue(res.passed)
        self.assertIn("established", res.message)
        self.assertIn("127.0.0.1:40123 -> 127.0.0.1:3306", res.details)
        self.assertEqual(calls[0][1], "database")

    @patch("socket.create_connection")
    def test_ssh_tunnel_auth_failure(self, mock_connect) -> None:
        mock_connect.return_value.__enter__.return_value = MagicMock()
        cfg = {"enabled": True, "host": "192.168.1.10", "port": 22}
        err = RuntimeError("SSH tunnel process exited prematurely with code 255: Permission denied")
        with patch("core.tui.testing.open_test_tunnel", _fake_tunnel(exc=err)):
            res = test_ssh_tunnel(cfg, feature_name="database")
        self.assertFalse(res.passed)
        self.assertIn("tunnel failed", res.message)
        self.assertIn("Permission denied", res.message)

    @patch("socket.create_connection")
    def test_ssh_tunnel_unreachable(self, mock_connect) -> None:
        mock_connect.side_effect = TimeoutError("Connection timed out")
        cfg = {"enabled": True, "host": "192.168.1.10", "port": 22}
        res = test_ssh_tunnel(cfg, feature_name="mailing_list")
        self.assertFalse(res.passed)
        self.assertEqual(res.name, "SSH Tunnel (mailing_list)")
        self.assertIn("unreachable", res.message)


class TestDatabaseConnectionDiagnostic(unittest.TestCase):
    """Test test_database_connection diagnostics."""

    def test_database_empty_config(self) -> None:
        res = test_database_connection({})
        self.assertFalse(res.passed)
        self.assertIn("missing or empty", res.message)

    def test_database_mock_engine(self) -> None:
        for engine in ("mock", "mockdb", "inmemory"):
            res = test_database_connection({"engine": engine})
            self.assertTrue(res.passed)
            self.assertIn("Mock database configured", res.message)

    def test_database_sqlite_engine(self) -> None:
        res = test_database_connection({"engine": "sqlite", "database": ":memory:"})
        self.assertTrue(res.passed)
        self.assertIn("Connected to SQLite", res.message)

    def test_database_missing_credentials(self) -> None:
        cfg = {"engine": "mariadb", "host": "127.0.0.1", "port": 3306}
        res = test_database_connection(cfg)
        self.assertFalse(res.passed)
        self.assertIn("credentials incomplete", res.message)

    @patch("core.ssh_tunnel.is_ssh_tunnel_active")
    @patch("core.tui.testing._check_local_port_listening")
    @patch("socket.create_connection")
    def test_database_tunnel_required_ssh_reachable(
        self,
        mock_socket,
        mock_port_listening,
        mock_is_active,
    ) -> None:
        mock_is_active.return_value = False
        mock_port_listening.return_value = False
        mock_socket.return_value.__enter__.return_value = MagicMock()

        db_cfg = {
            "engine": "mariadb",
            "user": "root",
            "password": "pwd",
            "database": "test",
            "ssh_tunnel": {
                "enabled": True,
                "host": "tunnel.remote.com",
                "port": 22,
                "local_port": 3308,
            },
        }
        res = test_database_connection(db_cfg, establish_tunnel=False)
        self.assertFalse(res.passed)
        self.assertIn("SSH tunnel required but not running", res.message)
        self.assertIn("tunnel.remote.com:22 is reachable", res.message)

    @patch("mysql.connector.connect")
    @patch("core.ssh_tunnel.is_ssh_tunnel_active", return_value=False)
    @patch("core.tui.testing._check_local_port_listening", return_value=False)
    @patch("socket.create_connection")
    def test_database_through_temporary_tunnel(
        self, mock_socket, _mock_listening, _mock_active, mock_connect
    ) -> None:
        mock_socket.return_value.__enter__.return_value = MagicMock()
        mock_cnx = MagicMock()
        mock_cnx.get_server_info.return_value = "10.11.6-MariaDB"
        mock_connect.return_value = mock_cnx
        db_cfg = {
            "engine": "mariadb",
            "user": "root",
            "password": "pwd",
            "database": "test",
            "host": "127.0.0.1",
            "port": 3308,
            "ssh_tunnel": {"enabled": True, "host": "tunnel.remote.com", "port": 22, "local_port": 3308},
        }
        entered: list = []
        exited: list = []

        @contextmanager
        def tracking_tunnel(ssh_cfg, feature_name="database"):
            entered.append(feature_name)
            try:
                yield 3308
            finally:
                exited.append(feature_name)

        with patch("core.tui.testing.open_test_tunnel", tracking_tunnel):
            res = test_database_connection(db_cfg)
        self.assertTrue(res.passed, res.message)
        self.assertEqual(entered, ["database"])
        self.assertEqual(exited, ["database"])
        kwargs = mock_connect.call_args.kwargs
        self.assertEqual(kwargs["host"], "127.0.0.1")
        self.assertEqual(kwargs["port"], 3308)
        self.assertNotIn("unix_socket", kwargs)

    @patch("mysql.connector.connect", side_effect=OSError("Can't connect to MySQL server on '127.0.0.1:3308'"))
    @patch("core.ssh_tunnel.is_ssh_tunnel_active", return_value=False)
    @patch("core.tui.testing._check_local_port_listening", return_value=False)
    @patch("socket.create_connection")
    def test_database_tunnel_probes_configured_port(
        self, mock_socket, _mock_listening, _mock_active, mock_connect
    ) -> None:
        """The probe uses the database Port, so a forward bound elsewhere fails the test."""
        mock_socket.return_value.__enter__.return_value = MagicMock()
        db_cfg = {
            "engine": "mariadb",
            "user": "root",
            "database": "test",
            "host": "127.0.0.1",
            "port": 3308,
            "ssh_tunnel": {"enabled": True, "host": "tunnel.remote.com", "port": 22, "local_port": 0},
        }
        with patch("core.tui.testing.open_test_tunnel", _fake_tunnel(41000)):
            res = test_database_connection(db_cfg)
        self.assertFalse(res.passed)
        self.assertEqual(mock_connect.call_args.kwargs["port"], 3308)
        self.assertIn("127.0.0.1:41000", res.details)
        self.assertIn("Set Local Port to 3308", res.details)

    @patch("core.ssh_tunnel.is_ssh_tunnel_active", return_value=False)
    @patch("core.tui.testing._check_local_port_listening", return_value=False)
    @patch("socket.create_connection")
    def test_database_temporary_tunnel_failure(self, mock_socket, _mock_listening, _mock_active) -> None:
        mock_socket.return_value.__enter__.return_value = MagicMock()
        db_cfg = {
            "engine": "mariadb",
            "user": "root",
            "database": "test",
            "ssh_tunnel": {"enabled": True, "host": "tunnel.remote.com", "port": 22},
        }
        err = RuntimeError("Permission denied (publickey)")
        with patch("core.tui.testing.open_test_tunnel", _fake_tunnel(exc=err)):
            res = test_database_connection(db_cfg)
        self.assertFalse(res.passed)
        self.assertIn("could not be established", res.message)
        self.assertIn("Permission denied", res.message)

    @patch("core.ssh_tunnel.is_ssh_tunnel_active")
    @patch("core.tui.testing._check_local_port_listening")
    @patch("socket.create_connection")
    def test_database_tunnel_required_ssh_unreachable(
        self,
        mock_socket,
        mock_port_listening,
        mock_is_active,
    ) -> None:
        mock_is_active.return_value = False
        mock_port_listening.return_value = False
        mock_socket.side_effect = OSError("No route to host")

        db_cfg = {
            "engine": "mariadb",
            "user": "root",
            "password": "pwd",
            "database": "test",
            "ssh_tunnel": {
                "enabled": True,
                "host": "tunnel.unreachable.com",
                "port": 22,
                "local_port": 3308,
            },
        }
        res = test_database_connection(db_cfg)
        self.assertFalse(res.passed)
        self.assertIn("SSH host tunnel.unreachable.com:22 unreachable", res.message)

    @patch.dict("sys.modules", {"mysql.connector": None})
    def test_database_missing_mysql_connector(self) -> None:
        cfg = {
            "engine": "mariadb",
            "user": "root",
            "password": "pwd",
            "database": "test",
            "host": "127.0.0.1",
        }
        res = test_database_connection(cfg)
        self.assertFalse(res.passed)
        self.assertIn("mysql-connector is not installed", res.message)

    @patch("mysql.connector.connect")
    def test_database_successful_mariadb_connection(self, mock_connect) -> None:
        mock_cnx = MagicMock()
        mock_cursor = MagicMock()
        mock_cnx.cursor.return_value = mock_cursor
        mock_cnx.get_server_info.return_value = "10.11.6-MariaDB"
        mock_connect.return_value = mock_cnx

        cfg = {
            "engine": "mariadb",
            "user": "root",
            "password": "pwd",
            "database": "test",
            "host": "127.0.0.1",
            "port": 3306,
        }
        res = test_database_connection(cfg)
        self.assertTrue(res.passed)
        self.assertIn("Connected to database successfully", res.message)
        self.assertIn("10.11.6-MariaDB", res.message)
        mock_cursor.execute.assert_called_with("SELECT 1")


class TestRepositoryDiagnostic(unittest.TestCase):
    """Test test_repository diagnostics."""

    def test_repo_empty_path(self) -> None:
        res = test_repository({"name": "empty", "path": ""})
        self.assertFalse(res.passed)
        self.assertIn("Repository path is not configured", res.message)

    def test_repo_non_existent_directory(self) -> None:
        res = test_repository({"name": "ghost", "path": "/path/does/not/exist/ever"})
        self.assertFalse(res.passed)
        self.assertIn("directory does not exist", res.message)

    def test_repo_not_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            res = test_repository({"name": "not_git", "path": tmpdir})
            self.assertFalse(res.passed)
            self.assertIn("Not a valid git repository", res.message)

    def test_repo_valid_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", tmpdir], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "--allow-empty", "-m", "initial commit"], check=True)

            res = test_repository({"name": "temp_repo", "path": tmpdir, "default_branch": "HEAD"})
            self.assertTrue(res.passed)
            self.assertIn("Git repository valid", res.message)

    def test_repo_bad_default_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", tmpdir], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "--allow-empty", "-m", "init"], check=True)

            res = test_repository({"name": "temp_repo", "path": tmpdir, "default_branch": "non_existent_branch_xyz"})
            self.assertFalse(res.passed)
            self.assertIn("Failed to resolve default branch", res.message)


class TestCVECatalogDiagnostic(unittest.TestCase):
    """Test test_cve_catalog diagnostics."""

    def test_cve_catalog_disabled(self) -> None:
        res = test_cve_catalog({"repo_dir": ""})
        self.assertTrue(res.passed)
        self.assertIn("CVE ingestion disabled", res.message)

    def test_cve_catalog_non_existent(self) -> None:
        res = test_cve_catalog({"repo_dir": "/path/to/missing/cve/repo"})
        self.assertFalse(res.passed)
        self.assertIn("CVE directory does not exist", res.message)

    def test_cve_catalog_not_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            res = test_cve_catalog({"repo_dir": tmpdir})
            self.assertFalse(res.passed)
            self.assertIn("not a git repository", res.message)

    def test_cve_catalog_invalid_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", tmpdir], check=True, capture_output=True)
            res = test_cve_catalog({"repo_dir": tmpdir})
            self.assertFalse(res.passed)
            self.assertIn("Unrecognized CVE catalog structure", res.message)

    def test_cve_catalog_valid_cvelistv5(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", tmpdir], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "test@example.com"], check=True)
            os.makedirs(os.path.join(tmpdir, "cves"), exist_ok=True)
            with open(os.path.join(tmpdir, "cves", "dummy.txt"), "w") as f:
                f.write("test")
            subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-m", "cve commit"], check=True)

            res = test_cve_catalog({"repo_dir": tmpdir})
            self.assertTrue(res.passed)
            self.assertIn("CVE catalog valid (cvelistV5", res.message)

    def test_cve_catalog_valid_vulns_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", tmpdir], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "test@example.com"], check=True)
            os.makedirs(os.path.join(tmpdir, "cve", "published"), exist_ok=True)
            with open(os.path.join(tmpdir, "cve", "published", "dummy.txt"), "w") as f:
                f.write("test")
            subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-m", "vulns commit"], check=True)

            res = test_cve_catalog({"repo_dir": tmpdir})
            self.assertTrue(res.passed)
            self.assertIn("CVE catalog valid (kernel vulns.git", res.message)


class TestMailingListDiagnostic(unittest.TestCase):
    """Test test_mailing_list diagnostics."""

    def test_mailing_list_disabled(self) -> None:
        res = test_mailing_list({"enabled": False})
        self.assertTrue(res.passed)
        self.assertIn("Mailing list ingestion is disabled", res.message)

    @patch("urllib.request.urlopen")
    def test_mailing_list_daemon_healthy(self, mock_urlopen) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"status": "ok", "rss_mb": 128}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = test_mailing_list({"port": 8192, "storage_dir": "/nonexistent"})
        self.assertTrue(res.passed)
        self.assertIn("Mailing list daemon responding on port 8192", res.message)

    def test_mailing_list_local_storage_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            res = test_mailing_list({"port": 54321, "storage_dir": tmpdir})
            self.assertTrue(res.passed)
            self.assertIn("Local mailing list store accessible", res.message)

    def test_mailing_list_storage_missing_local(self) -> None:
        res = test_mailing_list({"port": 54321, "storage_dir": "/nonexistent/lore/dir"})
        self.assertFalse(res.passed)
        self.assertIn("Storage directory does not exist", res.message)

    def test_mailing_list_tunnel_missing_host(self) -> None:
        cfg = {
            "port": 54321,
            "storage_dir": "/nonexistent/lore/dir",
            "ssh_tunnel": {"enabled": True},
        }
        with patch("core.tui.testing.open_test_tunnel") as mock_tunnel:
            res = test_mailing_list(cfg)
        self.assertFalse(res.passed)
        self.assertIn("SSH host is not configured", res.message)
        mock_tunnel.assert_not_called()

    @patch("core.tui.testing._probe_ml_health", return_value=(True, "Health: ok"))
    def test_mailing_list_tunnel_daemon_healthy(self, mock_probe) -> None:
        cfg = {"port": 8192, "ssh_tunnel": {"enabled": True, "host": "lore.host"}}
        calls: list = []
        with patch("core.tui.testing.open_test_tunnel", _fake_tunnel(42000, calls=calls)):
            res = test_mailing_list(cfg)
        self.assertTrue(res.passed)
        self.assertIn("responding through SSH tunnel", res.message)
        self.assertEqual(calls[0][1], "mailing_list")
        self.assertEqual(mock_probe.call_args[0][0], 42000)

    @patch("core.tui.testing._probe_ml_health", return_value=(False, ""))
    def test_mailing_list_tunnel_daemon_down(self, _mock_probe) -> None:
        with patch("core.tui.testing.open_test_tunnel", _fake_tunnel(42000)):
            auto = test_mailing_list({"auto_start_daemon": True, "ssh_tunnel": {"enabled": True, "host": "h"}})
            manual = test_mailing_list({"auto_start_daemon": False, "ssh_tunnel": {"enabled": True, "host": "h"}})
        self.assertTrue(auto.passed)
        self.assertIn("auto-started", auto.message)
        self.assertFalse(manual.passed)
        self.assertIn("no daemon is listening", manual.message)

    def test_mailing_list_tunnel_failure(self) -> None:
        err = RuntimeError("Connection refused")
        with patch("core.tui.testing.open_test_tunnel", _fake_tunnel(exc=err)):
            res = test_mailing_list({"ssh_tunnel": {"enabled": True, "host": "h", "port": 22}})
        self.assertFalse(res.passed)
        self.assertIn("could not be established", res.message)


class TestRunAllDiagnostics(unittest.TestCase):
    """Test run_all_diagnostics composite execution."""

    def test_run_all_diagnostics_comprehensive(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            subprocess.run(["git", "init", repo_dir], check=True, capture_output=True)
            subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", repo_dir, "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", repo_dir, "commit", "--allow-empty", "-m", "init"], check=True)

            config = {
                "database": {
                    "engine": "mock",
                    "ssh_tunnel": {"enabled": False},
                },
                "repositories": [
                    {
                        "name": "mock_kernel",
                        "path": repo_dir,
                        "default_branch": "HEAD",
                        "enabled": True,
                    },
                    {
                        "name": "disabled_repo",
                        "path": "/some/disabled/path",
                        "enabled": False,
                    },
                ],
                "cve": {"repo_dir": ""},
                "mailing_list": {"enabled": False},
            }

            results = run_all_diagnostics(config)
            self.assertIsInstance(results, list)
            names = [r.name for r in results]

            self.assertIn("SSH Tunnel (database)", names)
            self.assertIn("Database Connection", names)
            self.assertIn("Repo: mock_kernel", names)
            self.assertNotIn("Repo: disabled_repo", names)
            self.assertIn("CVE Catalog", names)
            self.assertIn("Mailing List", names)

            # All should pass given mock/disabled inputs
            for r in results:
                self.assertTrue(r.passed, f"Failed diagnostic: {r.name} - {r.message}")

    def test_run_all_diagnostics_no_enabled_repos(self) -> None:
        config = {
            "database": {"engine": "mock"},
            "repositories": [],
            "cve": {"repo_dir": ""},
            "mailing_list": {"enabled": False},
        }
        results = run_all_diagnostics(config)
        names = [r.name for r in results]
        self.assertIn("Repositories", names)
        repo_res = [r for r in results if r.name == "Repositories"][0]
        self.assertFalse(repo_res.passed)
        self.assertIn("No enabled repositories configured", repo_res.message)


if __name__ == "__main__":
    unittest.main()
