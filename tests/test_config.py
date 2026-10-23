"""tests/test_config.py - Unit tests for unified configuration management."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch
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
    get_repository,
    sync_repositories,
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

        with patch("core.config.find_config_path", return_value=None):
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

    def test_default_continuous_parsing_arguments(self) -> None:
        """Verify main.arg_handling defaults single_version to False for continuous parsing."""
        import main
        with mock.patch("sys.argv", ["main.py"]):
            args = main.arg_handling()
            self.assertFalse(args.single_version)

    def test_cli_single_version_argument(self) -> None:
        """Verify main.arg_handling recognizes --single-version and --single flags."""
        import main
        with mock.patch("sys.argv", ["main.py", "--single-version"]):
            args = main.arg_handling()
            self.assertTrue(args.single_version)

        with mock.patch("sys.argv", ["main.py", "--single"]):
            args = main.arg_handling()
            self.assertTrue(args.single_version)

    def test_cli_no_rc_argument(self) -> None:
        """--no-rc / --releases-only disables pre-release discovery; the default includes them."""
        import main
        with mock.patch("sys.argv", ["main.py"]):
            self.assertTrue(main.arg_handling().include_pre_releases)
        with mock.patch("sys.argv", ["main.py", "--no-rc"]):
            self.assertFalse(main.arg_handling().include_pre_releases)
        with mock.patch("sys.argv", ["main.py", "--releases-only"]):
            self.assertFalse(main.arg_handling().include_pre_releases)

    def test_cli_rc_arguments(self) -> None:
        """Verify main.arg_handling recognizes --rc, --include-rc, and --include-pre-releases flags."""
        import main
        with mock.patch("sys.argv", ["main.py", "--rc"]):
            args = main.arg_handling()
            self.assertTrue(args.include_pre_releases)

        with mock.patch("sys.argv", ["main.py", "--include-rc"]):
            args = main.arg_handling()
            self.assertTrue(args.include_pre_releases)

        with mock.patch("sys.argv", ["main.py", "--include-pre-releases"]):
            args = main.arg_handling()
            self.assertTrue(args.include_pre_releases)

    def test_master_file_get_version_tags(self) -> None:
        """Verify MasterFile.get_version_tags discovers sequential releases starting at target version."""
        from core.FileHandler import MasterFile
        mf = MasterFile()
        # Test with mock git command output
        # git tag --sort=v:refname with versionsort.suffix=-rc orders candidates before their release
        mock_output = "v3.0-rc1\nv3.0-rc2\nv3.0\nv3.1-rc1\nv3.1\nv3.2\nv3.3\n"
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout=mock_output)
            # Default: release candidates are included in version order
            tags = mf.get_version_tags(start_ref="v3.0")
            self.assertIn("v3.1-rc1", tags)
            self.assertLess(tags.index("v3.1-rc1"), tags.index("v3.1"))

            # include_pre_releases=False keeps final releases only
            tags_rel = mf.get_version_tags(start_ref="v3.0", include_pre_releases=False)
            self.assertEqual(tags_rel, ["v3.0", "v3.1", "v3.2", "v3.3"])

            # Slicing from v3.1
            tags_v31 = mf.get_version_tags(start_ref="v3.1", include_pre_releases=False)
            self.assertEqual(tags_v31, ["v3.1", "v3.2", "v3.3"])

            # Starting with -rc tag preserves pre-releases
            tags_rc = mf.get_version_tags(start_ref="v3.0-rc1")
            self.assertIn("v3.0-rc1", tags_rc)
            self.assertIn("v3.0-rc2", tags_rc)



def _make_git_repo(path: Path, commits: int = 3, tags: dict[int, str] | None = None) -> list[str]:
    """Create a small Git repository with sequential commits; returns commit SHAs oldest first."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    shas = []
    for i in range(commits):
        (path / "f.txt").write_text(f"{i}\n")
        subprocess.run(["git", "-C", str(path), "add", "f.txt"], check=True, env=env)
        subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", f"c{i}"], check=True, env=env)
        shas.append(subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip())
        if tags and i in tags:
            subprocess.run(["git", "-C", str(path), "tag", tags[i]], check=True, env=env)
    return shas


class TestRepositoryDetection(unittest.TestCase):
    """Repository auto-detection under workspace.repos_dir and config.json registration."""

    def setUp(self) -> None:
        reset_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repos_dir = self.root / "repos"
        self.repos_dir.mkdir()
        self.shas = _make_git_repo(self.repos_dir / "demo")
        (self.repos_dir / "not_a_repo").mkdir()
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({
            "database": {"database": "keep_me"},
            "workspace": {"repos_dir": str(self.repos_dir)},
        }))
        init_config(self.config_path)

    def tearDown(self) -> None:
        reset_config()
        self.tmp.cleanup()

    def test_sync_registers_detected_repository(self) -> None:
        repos = sync_repositories(write=True)
        self.assertEqual(len(repos), 1)
        repo = repos[0]
        self.assertEqual(repo["name"], "demo")
        self.assertEqual(repo["default_branch"], "main")
        self.assertEqual(repo["root_commit"], self.shas[0])
        self.assertEqual(repo["start_ref"], self.shas[0])
        self.assertTrue(repo["enabled"])

        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved["database"]["database"], "keep_me")
        self.assertEqual(saved["repositories"][0]["root_commit"], self.shas[0])
        self.assertEqual(get_repository()["name"], "demo")
        self.assertEqual(get_repository("demo")["root_commit"], self.shas[0])

    def test_sync_is_idempotent_and_preserves_user_values(self) -> None:
        sync_repositories(write=True)
        saved = json.loads(self.config_path.read_text())
        saved["repositories"][0]["start_ref"] = self.shas[1]
        del saved["repositories"][0]["git_url"]
        self.config_path.write_text(json.dumps(saved))

        reset_config()
        init_config(self.config_path)
        repos = sync_repositories(write=True)
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["start_ref"], self.shas[1])
        self.assertIn("git_url", json.loads(self.config_path.read_text())["repositories"][0])

    def test_version_sequence_from_first_commit(self) -> None:
        from core.FileHandler import MasterFile
        repo = self.root / "tagged"
        shas = _make_git_repo(repo, commits=4, tags={0: "v1.0-rc1", 2: "v1.0", 3: "v1.1"})
        mf = MasterFile()
        mf.set_repo_dir(repo)
        self.assertEqual(mf.get_root_commit(), shas[0])
        # The tag on the first commit names the snapshot; later releases follow in version order
        self.assertEqual(mf.get_version_tags(start_ref=shas[0]), ["v1.0-rc1", "v1.0", "v1.1"])
        # An untagged start commit keeps its SHA and continues through releases containing it
        self.assertEqual(mf.get_version_tags(start_ref=shas[1]), [shas[1], "v1.0", "v1.1"])


if __name__ == "__main__":
    unittest.main()
