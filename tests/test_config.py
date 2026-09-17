"""tests/test_config.py - Unit tests for unified configuration management."""
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from core.config import (
    DEFAULT_CONFIG,
    find_config_path,
    get_config,
    get_db_config,
    get_parser_config,
    get_webapp_config,
    init_config,
    load_config,
    reset_config,
    sync_environ,
)
from webapp.main import DatabaseManager


class TestConfigManagement(unittest.TestCase):
    def setUp(self) -> None:
        reset_config()
        self.original_env = dict(os.environ)

    def tearDown(self) -> None:
        reset_config()
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_default_config_fallback(self) -> None:
        """When no config file or env vars exist, defaults are used."""
        # Clean any relevant env vars
        for k in ["DB_HOST", "MYSQL_HOST", "DB_PORT", "MYSQL_PORT", "HOST", "PORT", "CONFIG_FILE"]:
            os.environ.pop(k, None)

        cfg = load_config(config_path=None)
        self.assertEqual(cfg["database"]["port"], 3306)
        self.assertEqual(cfg["database"]["user"], "root")
        self.assertEqual(cfg["webapp"]["port"], 8000)
        self.assertEqual(cfg["parser"]["table_engine"], "cached")

    def test_load_custom_config_file(self) -> None:
        """Config file values override defaults."""
        custom_data = {
            "database": {
                "host": "192.168.1.50",
                "port": 3307,
                "database": "custom_kernel",
            },
            "webapp": {
                "port": 9090,
                "reload": False,
            },
            "parser": {
                "table_engine": "direct",
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(custom_data, f)
            temp_path = f.name

        try:
            cfg = load_config(temp_path)
            self.assertEqual(cfg["database"]["host"], "192.168.1.50")
            self.assertEqual(cfg["database"]["port"], 3307)
            self.assertEqual(cfg["database"]["database"], "custom_kernel")
            # Unspecified keys retain defaults
            self.assertEqual(cfg["database"]["user"], "root")
            self.assertEqual(cfg["webapp"]["port"], 9090)
            self.assertFalse(cfg["webapp"]["reload"])
            self.assertEqual(cfg["parser"]["table_engine"], "direct")
        finally:
            os.unlink(temp_path)

    def test_environment_variable_precedence_over_file(self) -> None:
        """Environment variables take precedence over config.json values."""
        custom_data = {
            "database": {
                "host": "192.168.1.50",
                "port": 3307,
            },
            "webapp": {
                "port": 9090,
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(custom_data, f)
            temp_path = f.name

        try:
            # Set environment variable override
            os.environ["MYSQL_HOST"] = "env-db-host.internal"
            os.environ["PORT"] = "9999"

            cfg = load_config(temp_path)
            self.assertEqual(cfg["database"]["host"], "env-db-host.internal")
            self.assertEqual(cfg["database"]["port"], 3307)  # from file
            self.assertEqual(cfg["webapp"]["port"], 9999)  # from env var
        finally:
            os.unlink(temp_path)

    def test_env_synchronization(self) -> None:
        """sync_environ ensures both DB_* and MYSQL_* keys are exported."""
        cfg = {
            "database": {
                "host": "sync-host.internal",
                "port": 3309,
                "user": "sync_user",
                "password": "sync_pass",
                "database": "sync_db",
                "timeout": 15,
            },
            "webapp": {"host": "0.0.0.0", "port": 8000, "reload": True},
            "parser": {"table_engine": "cached", "memory_mode": "normal", "fidelity": True},
        }
        sync_environ(cfg)

        self.assertEqual(os.environ.get("DB_HOST"), "sync-host.internal")
        self.assertEqual(os.environ.get("MYSQL_HOST"), "sync-host.internal")
        self.assertEqual(os.environ.get("DB_PORT"), "3309")
        self.assertEqual(os.environ.get("MYSQL_PORT"), "3309")
        self.assertEqual(os.environ.get("DB_USER"), "sync_user")
        self.assertEqual(os.environ.get("MYSQL_USER"), "sync_user")
        self.assertEqual(os.environ.get("DB_NAME"), "sync_db")
        self.assertEqual(os.environ.get("MYSQL_DATABASE"), "sync_db")

    def test_malformed_json_raises_runtime_error(self) -> None:
        """Malformed JSON raises a descriptive RuntimeError."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{ invalid json : true, ")
            temp_path = f.name

        try:
            with self.assertRaises(RuntimeError) as ctx:
                load_config(temp_path)
            self.assertIn("Failed to parse JSON configuration file", str(ctx.exception))
        finally:
            os.unlink(temp_path)

    def test_database_manager_reads_config(self) -> None:
        """DatabaseManager correctly initializes using get_db_config()."""
        custom_data = {
            "database": {
                "host": "10.0.0.42",
                "port": 3308,
                "user": "dbmgr_user",
                "password": "mgr_pass",
                "database": "dbmgr_test",
                "timeout": 5,
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(custom_data, f)
            temp_path = f.name

        try:
            init_config(temp_path)
            with unittest.mock.patch.object(DatabaseManager, "_init_pool"):
                mgr = DatabaseManager()
                self.assertEqual(mgr.host, "10.0.0.42")
                self.assertEqual(mgr.port, 3308)
                self.assertEqual(mgr.user, "dbmgr_user")
                self.assertEqual(mgr.password, "mgr_pass")
                self.assertEqual(mgr.database, "dbmgr_test")
                self.assertEqual(mgr.timeout, 5)
        finally:
            os.unlink(temp_path)

    def test_main_cli_config_integration(self) -> None:
        """main.py arg_handling correctly parses -c and loads parser configuration."""
        import main
        custom_data = {
            "parser": {
                "table_engine": "direct",
                "memory_mode": "low",
                "fidelity": False,
            },
            "database": {
                "engine": "mysql",
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(custom_data, f)
            temp_path = f.name

        try:
            with mock.patch("sys.argv", ["main.py", "-c", temp_path]):
                args = main.arg_handling()
                self.assertEqual(args.config, temp_path)
                self.assertFalse(args.fidelity)
                from core.globalstuff import G
                self.assertEqual(G.MEMORY_MODE, "low")
        finally:
            os.unlink(temp_path)

    def test_webapp_cli_arguments(self) -> None:
        """webapp/main.py parses CLI overrides on top of config."""
        import argparse
        parser = argparse.ArgumentParser(description="KernelInfo-Parser Web Application Server")
        parser.add_argument("-c", "--config", dest="config_path", default=None)
        parser.add_argument("--host", dest="host", default=None)
        parser.add_argument("--port", dest="port", type=int, default=None)
        parser.add_argument("--reload", dest="reload", action="store_true", default=None)
        parser.add_argument("--no-reload", dest="reload", action="store_false")

        args = parser.parse_args(["-c", "custom.json", "--host", "127.0.0.1", "--port", "9099", "--no-reload"])
        self.assertEqual(args.config_path, "custom.json")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9099)
        self.assertFalse(args.reload)


if __name__ == "__main__":
    unittest.main()
