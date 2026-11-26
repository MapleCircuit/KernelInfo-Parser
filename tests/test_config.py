"""tests/test_config.py - Unit tests for unified configuration management."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from core.config import (
    DEFAULT_BRANCH_FILTER,
    DEFAULT_CONFIG,
    DEFAULT_TAG_VIEWS,
    find_config_path,
    find_repository_branches,
    get_repository,
    get_webapp_config,
    init_config,
    load_config,
    matches_branch_filter,
    reset_config,
    sync_environ,
    sync_repositories,
    update_repository_branches,
)
from webapp.backend.config import get_backend_db_config


class TestConfigManagement(unittest.TestCase):
    def setUp(self) -> None:
        import copy

        import core.config
        self._orig_cfg = copy.deepcopy(core.config._ACTIVE_CONFIG)
        self._orig_cfg_path = core.config._LOADED_CONFIG_PATH
        reset_config()
        self.original_env = dict(os.environ)

    def tearDown(self) -> None:
        import copy

        import core.config
        reset_config()
        os.environ.clear()
        os.environ.update(self.original_env)
        core.config._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        core.config._LOADED_CONFIG_PATH = self._orig_cfg_path

    def test_default_config_fallback(self) -> None:
        """When no config file or env vars exist, defaults are used."""
        # Clean any relevant env vars
        for k in ["DB_HOST", "MYSQL_HOST", "DB_PORT", "MYSQL_PORT", "HOST", "PORT", "CONFIG_FILE"]:
            os.environ.pop(k, None)

        with patch("core.config.find_config_path", return_value=None):
            cfg = load_config(config_path=None)
            self.assertEqual(cfg["database"]["port"], 3306)
            # No default account: credentials come from the configuration or the connect fails with a clear error
            self.assertEqual((cfg["database"]["user"], cfg["database"]["password"]), ("", ""))
            self.assertEqual(cfg["webapp"]["port"], 8000)
            self.assertEqual(cfg["webapp"]["pool_size"], 10)
            self.assertEqual(cfg["webapp"]["default_version"], "v3.0")
            self.assertEqual(cfg["parser"]["table_engine"], "cached")

    def test_default_config_webapp_keys_and_no_duplicate_dict_literals(self) -> None:
        """DEFAULT_CONFIG['webapp'] has all expected keys (including default_version and pool_size) and zero duplicate literal keys."""
        import ast

        import core.config
        from webapp.backend.config import get_backend_server_config

        wa = DEFAULT_CONFIG["webapp"]
        for expected_key in (
            "host",
            "port",
            "reload",
            "pool_size",
            "default_version",
            "cache",
            "jobs",
            "prewarm_indexes",
            "tools",
            "source_index",
        ):
            self.assertIn(expected_key, wa)
        self.assertEqual(wa["default_version"], "v3.0")
        self.assertEqual(wa["pool_size"], 10)
        self.assertIn("repositories", DEFAULT_CONFIG)

        source_path = Path(core.config.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        default_cfg_dicts: list[ast.Dict] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "DEFAULT_CONFIG":
                if isinstance(node.value, ast.Dict):
                    default_cfg_dicts.append(node.value)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG" and isinstance(node.value, ast.Dict):
                        default_cfg_dicts.append(node.value)
        self.assertEqual(len(default_cfg_dicts), 1)

        for dict_node in ast.walk(default_cfg_dicts[0]):
            if isinstance(dict_node, ast.Dict):
                literal_keys = [
                    k.value for k in dict_node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
                ]
                self.assertEqual(
                    len(literal_keys),
                    len(set(literal_keys)),
                    f"Duplicate literal key in DEFAULT_CONFIG at line {dict_node.lineno}: {literal_keys}",
                )

        with patch("core.config.find_config_path", return_value=None):
            init_config(None)
            srv_cfg = get_backend_server_config()
            self.assertEqual(srv_cfg["default_version"], "v3.0")
            self.assertEqual(srv_cfg["pool_size"], 10)
            self.assertEqual(srv_cfg["port"], 8000)

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
            self.assertEqual(cfg["database"]["user"], "")
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
            "parser": {"table_engine": "cached", "memory_mode": "normal"},
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

    def test_webapp_db_config_reads_config(self) -> None:
        """The webapp's connection pool settings come from get_db_config() (get_backend_db_config)."""
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
            cfg = get_backend_db_config()
            self.assertEqual(cfg["host"], "10.0.0.42")
            self.assertEqual(int(cfg["port"]), 3308)
            self.assertEqual(cfg["user"], "dbmgr_user")
            self.assertEqual(cfg["password"], "mgr_pass")
            self.assertEqual(cfg["database"], "dbmgr_test")
            self.assertEqual(int(cfg["timeout"]), 5)
        finally:
            os.unlink(temp_path)

    def test_main_cli_config_integration(self) -> None:
        """main.py arg_handling correctly parses -c and loads parser configuration."""
        import pipeline.cli
        custom_data = {
            "parser": {
                "table_engine": "direct",
                "memory_mode": "low",
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
                args = pipeline.cli.arg_handling()
                self.assertEqual(args.config, temp_path)
                from core.globalstuff import G
                self.assertEqual(G.MEMORY_MODE, "low")
        finally:
            os.unlink(temp_path)

    def test_webapp_cli_arguments(self) -> None:
        """webapp/main.py parses CLI overrides, and -c becomes the configuration of the server process."""
        from webapp import main as webapp_main

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = Path(tmpdir) / "custom_webapp.json"
            cfg_file.write_text(json.dumps({"webapp": {"port": 9099, "reload": False}}), encoding="utf-8")

            args = webapp_main.parse_args(["-c", str(cfg_file), "--host", "127.0.0.1", "--port", "9099", "--no-reload"])
            self.assertEqual(args.config, str(cfg_file))
            self.assertEqual(args.host, "127.0.0.1")
            self.assertEqual(args.port, 9099)
            self.assertFalse(args.reload)
            self.assertIsNone(webapp_main.parse_args([]).reload)

            # -c reaches core.config (and uvicorn's reloader process) as CONFIG_FILE and KERNELINFO_CONFIG
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CONFIG_FILE", None)
                os.environ.pop("KERNELINFO_CONFIG", None)
                webapp_main.apply_config_path(args)
                self.assertEqual(os.environ["CONFIG_FILE"], str(cfg_file.resolve()))
                self.assertEqual(os.environ["KERNELINFO_CONFIG"], str(cfg_file.resolve()))
                self.assertEqual(get_webapp_config()["port"], 9099)
                self.assertFalse(get_webapp_config()["reload"])

            missing_args = webapp_main.parse_args(["-c", str(Path(tmpdir) / "missing.json")])
            with self.assertRaises(FileNotFoundError):
                webapp_main.apply_config_path(missing_args)

    def test_default_continuous_parsing_arguments(self) -> None:
        """Verify pipeline.cli.arg_handling defaults single_version to False for continuous parsing."""
        import pipeline.cli
        with mock.patch("sys.argv", ["main.py"]):
            args = pipeline.cli.arg_handling()
            self.assertFalse(args.single_version)

    def test_cli_single_version_argument(self) -> None:
        """Verify pipeline.cli.arg_handling recognizes --single-version and --single flags."""
        import pipeline.cli
        with mock.patch("sys.argv", ["main.py", "--single-version"]):
            args = pipeline.cli.arg_handling()
            self.assertTrue(args.single_version)

        with mock.patch("sys.argv", ["main.py", "--single"]):
            args = pipeline.cli.arg_handling()
            self.assertTrue(args.single_version)

    def test_cli_no_rc_argument(self) -> None:
        """--no-rc / --releases-only disables pre-release discovery; the default includes them."""
        import pipeline.cli
        with mock.patch("sys.argv", ["main.py"]):
            self.assertTrue(pipeline.cli.arg_handling().include_pre_releases)
        with mock.patch("sys.argv", ["main.py", "--no-rc"]):
            self.assertFalse(pipeline.cli.arg_handling().include_pre_releases)
        with mock.patch("sys.argv", ["main.py", "--releases-only"]):
            self.assertFalse(pipeline.cli.arg_handling().include_pre_releases)

    def test_cli_rc_arguments(self) -> None:
        """Verify pipeline.cli.arg_handling recognizes --rc, --include-rc, and --include-pre-releases flags."""
        import pipeline.cli
        with mock.patch("sys.argv", ["main.py", "--rc"]):
            args = pipeline.cli.arg_handling()
            self.assertTrue(args.include_pre_releases)

        with mock.patch("sys.argv", ["main.py", "--include-rc"]):
            args = pipeline.cli.arg_handling()
            self.assertTrue(args.include_pre_releases)

        with mock.patch("sys.argv", ["main.py", "--include-pre-releases"]):
            args = pipeline.cli.arg_handling()
            self.assertTrue(args.include_pre_releases)

    def test_master_file_get_version_tags(self) -> None:
        """Verify MasterFile.get_version_tags discovers sequential releases starting at target version."""
        from core.FileHandler import MasterFile
        mf = MasterFile()
        # Test with mock git command output
        # git tag --sort=v:refname with versionsort.suffix=-rc orders candidates before their release
        mock_output = "assoc-array-fixes\nv2.6.11-tree\nv3.0-rc1\nv3.0-rc2\nv3.0\nv3.1-rc1\nv3.1\nv3.2\nv3.3\n"
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout=mock_output)
            # Default: release candidates are included in version order, non-release tags are filtered out
            tags = mf.get_version_tags(start_ref="v3.0")
            self.assertNotIn("assoc-array-fixes", tags)
            self.assertNotIn("v2.6.11-tree", tags)
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

        # Daily next-YYYYMMDD tags follow every release, even unprefixed historical tags
        mock_output = "0.01\n2.4.31\nnext-20260810\nnext-20260811\nv2.5.0\nv2.6.12-rc2\n"
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout=mock_output)
            self.assertEqual(
                mf.get_version_tags(start_ref="0.01"),
                ["0.01", "2.4.31", "v2.5.0", "v2.6.12-rc2", "next-20260810", "next-20260811"],
            )
            self.assertIn("versionsort.suffix=pre", mock_run.call_args.args[0])



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

    def test_start_ref_prefers_the_earliest_history_root(self) -> None:
        from core.config import detect_start_ref
        repo = self.root / "history"
        shas = _make_git_repo(repo, commits=2, tags={0: "v2.6.12-rc2", 1: "v2.6.12"})
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["checkout", "-q", "--orphan", "history/master"], ["commit", "-q", "-m", "0.01"], ["tag", "0.01"],
                    ["commit", "-q", "--allow-empty", "-m", "2.4.0"], ["tag", "2.4.0"], ["checkout", "-q", "main"]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env)
        # Without a history branch the default branch's first commit stays the start
        self.assertEqual(detect_start_ref(repo, shas[0], ["main"]), shas[0])
        # The history root's release tag sorts before the default branch's: the walk starts there, by name
        self.assertEqual(detect_start_ref(repo, shas[0], ["history/master", "main"]), "0.01")

    def test_branch_filtering(self) -> None:
        self.assertTrue(matches_branch_filter("master"))
        self.assertTrue(matches_branch_filter("main"))
        self.assertTrue(matches_branch_filter("stable/linux-6.6.y"))
        self.assertFalse(matches_branch_filter("v6.6-rc1"))
        self.assertFalse(matches_branch_filter("test-rc2"))

        custom_filter = {
            "include": ["stable/*", "main"],
            "exclude": ["*test*"],
        }
        self.assertTrue(matches_branch_filter("stable/linux-5.15.y", custom_filter))
        self.assertTrue(matches_branch_filter("main", custom_filter))
        self.assertFalse(matches_branch_filter("master", custom_filter))
        self.assertFalse(matches_branch_filter("stable/test", custom_filter))

    def test_find_and_sync_repository_branches(self) -> None:
        repo_dir = self.repos_dir / "demo"
        subprocess.run(["git", "-C", str(repo_dir), "branch", "feature-x"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "branch", "feature-rc1"], check=True)
        # Create a simulated remote tracking branch
        subprocess.run(["git", "-C", str(repo_dir), "update-ref", "refs/remotes/stable/linux-1.0.y", "HEAD"], check=True)
        # Create a simulated origin/main remote tracking branch that mirrors local main
        subprocess.run(["git", "-C", str(repo_dir), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)

        branches = find_repository_branches(repo_dir, DEFAULT_BRANCH_FILTER, default_branch="main")
        self.assertIn("main", branches)
        self.assertNotIn("origin/main", branches)
        self.assertIn("feature-x", branches)
        self.assertIn("stable/linux-1.0.y", branches)
        self.assertNotIn("feature-rc1", branches)
        self.assertEqual(branches[0], "main")

        updated = update_repository_branches("demo", write=True)
        self.assertEqual(updated, branches)
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved["repositories"][0]["branches"], branches)
        self.assertEqual(saved["repositories"][0]["branch_filter"], DEFAULT_BRANCH_FILTER)

    def test_tag_views_config_and_regex_rules(self) -> None:
        """Verify DEFAULT_TAG_VIEWS regex rules partition Linux 2.6.x, 3.x+, historical, and next tags."""
        import copy
        import re

        from core.config import (
            _LEGACY_DEFAULT_TAG_VIEWS,
            _normalize_repo_entry_features,
        )

        self.assertGreaterEqual(len(DEFAULT_TAG_VIEWS), 1)
        linux_view = DEFAULT_TAG_VIEWS[0]
        self.assertEqual(linux_view["name"], "— any branch —")
        rules = [re.compile(r) for r in linux_view["regex_rules"]]

        def parse(tag: str) -> tuple[str, str, bool]:
            for r in rules:
                m = r.match(tag)
                if m and m.group(1):
                    suffix = m.group(2) if len(m.groups()) >= 2 and m.group(2) else ""
                    if suffix:
                        return m.group(1), suffix, True
            return tag, "", False

        # Linux 2.6.x family: base is v2.6.<sublevel>
        self.assertEqual(parse("v2.6.12"), ("v2.6.12", "", False))
        self.assertEqual(parse("v2.6.12.1"), ("v2.6.12", ".1", True))
        self.assertEqual(parse("v2.6.16.62"), ("v2.6.16", ".62", True))
        self.assertEqual(parse("v2.6.12-rc2"), ("v2.6.12", "-rc2", True))
        self.assertEqual(parse("v2.6.16.28-rc1"), ("v2.6.16", ".28-rc1", True))
        self.assertEqual(parse("v2.6.0-test1"), ("v2.6.0", "-test1", True))
        self.assertEqual(parse("v2.6.9-final"), ("v2.6.9", "-final", True))

        # Linux 3.x+ family: base is v<major>.<minor>
        self.assertEqual(parse("v3.0"), ("v3.0", "", False))
        self.assertEqual(parse("v3.0-rc7"), ("v3.0", "-rc7", True))
        self.assertEqual(parse("v3.0.1"), ("v3.0", ".1", True))
        self.assertEqual(parse("v3.0.101"), ("v3.0", ".101", True))
        self.assertEqual(parse("v6.6"), ("v6.6", "", False))
        self.assertEqual(parse("v6.6.1"), ("v6.6", ".1", True))
        self.assertEqual(parse("v6.6-rc1"), ("v6.6", "-rc1", True))

        # Historical & non-master branch tags: base is <major>.<minor> or <prefix>
        self.assertEqual(parse("2.4.0"), ("2.4", ".0", True))
        self.assertEqual(parse("2.4.9.15"), ("2.4", ".9.15", True))
        self.assertEqual(parse("2.2.0pre1"), ("2.2", ".0pre1", True))
        self.assertEqual(parse("1.1.95"), ("1.1", ".95", True))
        self.assertEqual(parse("1.0pre1"), ("1.0", "pre1", True))
        self.assertEqual(parse("0.99.14a"), ("0.99", ".14a", True))
        self.assertEqual(parse("0.96a-patch1"), ("0.96", "a-patch1", True))
        self.assertEqual(parse("pre2.0.1"), ("pre2.0", ".1", True))
        self.assertEqual(parse("lia64-v2.5.10"), ("lia64-v2.5", ".10", True))
        self.assertEqual(parse("next-20261110"), ("next", "-20261110", True))

        # Legacy DEFAULT_TAG_VIEWS are upgraded during repository entry normalization
        entry = {"name": "linux", "tag_views": copy.deepcopy(_LEGACY_DEFAULT_TAG_VIEWS)}
        _normalize_repo_entry_features(entry)
        self.assertEqual(entry["tag_views"], DEFAULT_TAG_VIEWS)



class TestManPagesRepository(unittest.TestCase):
    """The Linux man-pages: detection, release tags, project names."""

    def setUp(self) -> None:
        reset_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        reset_config()
        self.tmp.cleanup()

    def _repo(self, name: str, tags: dict[int, str] | None = None) -> tuple[Path, list[str]]:
        path = self.root / name
        shas = _make_git_repo(path, commits=3, tags=tags)
        subprocess.run(["git", "-C", str(path), "branch", "-m", "master"], check=True)
        subprocess.run(["git", "-C", str(path), "branch", "topic"], check=True)
        return path, shas

    def test_detected_by_directory_name(self) -> None:
        from core.config import (
            MANPAGES_BRANCH_FILTER,
            MANPAGES_TAG_VIEWS,
            detect_repository,
        )
        path, shas = self._repo("man-pages", tags={0: "man-pages-1.70", 2: "man-pages-2.00"})
        entry = detect_repository(path)
        self.assertEqual(entry["project"], "man-pages")
        self.assertEqual(entry["default_branch"], "master")
        self.assertEqual(entry["root_commit"], shas[0])
        self.assertEqual(entry["start_ref"], shas[0])
        self.assertEqual(entry["branch_filter"], MANPAGES_BRANCH_FILTER)
        self.assertEqual(entry["branches"], ["master"])
        self.assertEqual(entry["tag_views"], MANPAGES_TAG_VIEWS)

    def test_detected_by_url_or_root_commit(self) -> None:
        from core import config
        path, _shas = self._repo("mirror")
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                        "https://git.kernel.org/pub/scm/docs/man-pages/man-pages.git"], check=True)
        self.assertEqual(config.detect_repository(path)["project"], "man-pages")
        other, _ = self._repo("docs")
        with mock.patch.object(config, "detect_root_commit", return_value=config.MANPAGES_ROOT_COMMIT):
            entry = config.detect_repository(other)
        self.assertEqual((entry["project"], entry["start_ref"]), ("man-pages", config.MANPAGES_ROOT_COMMIT))

    def test_other_repositories_unchanged(self) -> None:
        from core.config import detect_repository
        path, shas = self._repo("linux")
        entry = detect_repository(path)
        self.assertEqual(entry["project"], "linux")
        self.assertEqual(entry["start_ref"], shas[0])
        self.assertEqual(entry["branch_filter"], DEFAULT_BRANCH_FILTER)
        self.assertEqual(entry["tag_views"], DEFAULT_TAG_VIEWS)
        self.assertEqual(sorted(entry["branches"]), ["master", "topic"])

    def test_linux_root_commit_names_the_linux_project(self) -> None:
        from core import config
        other, _shas = self._repo("custom")
        self.assertEqual(config.detect_repository(other)["project"], "custom")
        path, _shas = self._repo("torvalds")
        with mock.patch.object(config, "detect_root_commit", return_value=config.LINUX_ROOT_COMMIT):
            self.assertEqual(config.detect_repository(path)["project"], "linux")

    def test_project_helpers(self) -> None:
        from core.config import (
            is_kernel_project,
            is_manpages_repository,
            project_display_name,
            repository_project,
        )
        self.assertEqual(project_display_name("linux"), "Linux Kernel")
        self.assertEqual(project_display_name(None), "Linux Kernel")
        self.assertEqual(project_display_name("man-pages"), "Linux man-pages")
        self.assertEqual(project_display_name("rt"), "rt")
        self.assertTrue(is_kernel_project("linux"))
        self.assertTrue(is_kernel_project("torvalds"))
        self.assertFalse(is_kernel_project("man-pages"))
        self.assertEqual(repository_project(None), "linux")
        self.assertEqual(repository_project({"project": "man-pages"}), "man-pages")
        self.assertTrue(is_manpages_repository("x", "git://example.com/man-pages.git"))
        self.assertTrue(is_manpages_repository("man-pages"))
        self.assertFalse(is_manpages_repository("linux", "https://git.kernel.org/torvalds/linux.git", "1da177e4"))

    def test_tag_view_groups_by_major(self) -> None:
        import re

        from core.config import MANPAGES_TAG_VIEWS
        rule = re.compile(MANPAGES_TAG_VIEWS[0]["regex_rules"][0])
        self.assertEqual(rule.match("man-pages-6.9.1").groups(), ("man-pages-6", ".9.1"))
        self.assertEqual(rule.match("man-pages-2.00").groups(), ("man-pages-2", ".00"))

    def test_version_tags_accept_manpages_releases(self) -> None:
        from core.FileHandler import MasterFile
        mf = MasterFile()
        mock_output = ("man-pages-1.70\nman-pages-2.00\nman-pages-6.0-rc1\nman-pages-6.00\nman-pages-6.05-a1\n"
                       "man-pages-6.9\nman-pages-6.9.1\nman-pages-6.10\nnofwd-fix\n")
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout=mock_output)
            every = ["man-pages-1.70", "man-pages-2.00", "man-pages-6.0-rc1", "man-pages-6.00", "man-pages-6.9",
                     "man-pages-6.9.1", "man-pages-6.10"]
            # Without a start ref a man-pages repository starts at its first release
            self.assertEqual(mf.get_version_tags(), every)
            self.assertEqual(mf.get_version_tags(start_ref="man-pages-6.9", include_pre_releases=False), every[4:])
            self.assertNotIn("man-pages-6.0-rc1", mf.get_version_tags(include_pre_releases=False))

    def test_version_sequence_from_root_commit_and_tui_listing(self) -> None:
        from core.FileHandler import MasterFile
        from core.tui.git_ops import list_version_tags
        path, shas = self._repo("man-pages", tags={1: "man-pages-6.9", 2: "man-pages-6.10"})
        subprocess.run(["git", "-C", str(path), "tag", "v3.0-tree"], check=True)
        mf = MasterFile()
        mf.set_repo_dir(path)
        self.assertEqual(mf.get_version_tags(start_ref=shas[0]), [shas[0], "man-pages-6.9", "man-pages-6.10"])
        self.assertEqual(list_version_tags(path), ["man-pages-6.9", "man-pages-6.10"])
        self.assertEqual(list_version_tags(path, include_pre_releases=False), ["man-pages-6.9", "man-pages-6.10"])


class TestProjectFeatureConfig(unittest.TestCase):
    """Canonical per-project feature definitions, overrides, pruning, and sparse repo schema."""

    def setUp(self) -> None:
        reset_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        reset_config()
        self.tmp.cleanup()

    def test_feature_defaults_for_linux_and_manpages(self) -> None:
        from core.config import (
            DEFAULT_KERNEL_FEATURES,
            DEFAULT_MANPAGES_FEATURES,
            PROJECT_FEATURE_DEFS,
            PROJECT_FEATURE_KEYS,
            default_project_features,
        )

        self.assertEqual(len(PROJECT_FEATURE_DEFS), 15)
        self.assertEqual(len(PROJECT_FEATURE_KEYS), 15)
        self.assertTrue(all(DEFAULT_KERNEL_FEATURES[k] for k in PROJECT_FEATURE_KEYS))
        self.assertEqual(default_project_features("linux"), DEFAULT_KERNEL_FEATURES)
        self.assertEqual(default_project_features("custom-kernel"), DEFAULT_KERNEL_FEATURES)
        self.assertEqual(default_project_features("man-pages"), DEFAULT_MANPAGES_FEATURES)
        for enabled_key in ("commits", "insights", "man", "patch", "compare"):
            self.assertTrue(DEFAULT_MANPAGES_FEATURES[enabled_key])
        for disabled_key in ("kconfig", "api", "nodemap", "maintainers", "mail", "stable", "cve", "pahole", "callgraph", "docs"):
            self.assertFalse(DEFAULT_MANPAGES_FEATURES[disabled_key])

    def test_normalize_resolve_prune_and_enabled(self) -> None:
        from core.config import (
            normalize_feature_overrides,
            prune_default_feature_overrides,
            repo_feature_enabled,
            resolve_repo_features,
        )

        self.assertEqual(normalize_feature_overrides(None), {})
        self.assertEqual(normalize_feature_overrides(["kconfig"]), {})
        self.assertEqual(
            normalize_feature_overrides({"api": False, "unknown": True, "kconfig": 1, "docs": "yes", "man": True}),
            {"api": False, "man": True},
        )

        linux_entry = {"project": "linux", "features": {"api": False, "bogus": True}}
        resolved_linux = resolve_repo_features(linux_entry)
        self.assertFalse(resolved_linux["api"])
        self.assertTrue(resolved_linux["kconfig"])
        self.assertFalse(repo_feature_enabled(linux_entry, "api"))
        self.assertTrue(repo_feature_enabled(linux_entry, "kconfig"))

        man_entry = {"project": "man-pages", "features": {"kconfig": True}}
        resolved_man = resolve_repo_features(man_entry)
        self.assertTrue(resolved_man["kconfig"])
        self.assertFalse(resolved_man["api"])
        self.assertTrue(repo_feature_enabled(man_entry, "kconfig"))

        self.assertEqual(prune_default_feature_overrides("linux", {"api": False, "kconfig": True}), {"api": False})
        self.assertEqual(prune_default_feature_overrides("man-pages", {"kconfig": True, "man": True}), {"kconfig": True})

    def test_features_sparse_in_detect_and_normalized_in_merge_and_load(self) -> None:
        from core.config import (
            REPOSITORY_KEYS,
            _merge_repositories,
            _missing_repo_keys,
            detect_repository,
            load_config,
        )

        self.assertIn("features", REPOSITORY_KEYS)
        repo_path = self.root / "linux"
        _make_git_repo(repo_path, commits=2)
        entry = detect_repository(repo_path)
        self.assertNotIn("features", entry)
        self.assertEqual(_missing_repo_keys(entry), [])

        merged, _ = _merge_repositories(
            [
                {**entry, "features": {"api": False, "invalid": True, "docs": "no"}},
                {**entry, "path": str(repo_path), "features": {"invalid": True}},
            ],
            [repo_path],
        )
        self.assertEqual(merged[0]["features"], {"api": False})
        self.assertNotIn("features", merged[1])

        cfg_path = self.root / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "repositories": [
                        {"name": "linux", "path": "repos/linux", "project": "linux", "features": {"cve": False, "bad": 1}},
                        {"name": "empty", "path": "repos/empty", "project": "linux", "features": {}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        loaded = load_config(cfg_path)
        self.assertEqual(loaded["repositories"][0]["features"], {"cve": False})
        self.assertNotIn("features", loaded["repositories"][1])


class TestProjectInsightsConfig(unittest.TestCase):
    """Canonical per-project Insights definitions, autodetection, overrides, and pruning."""

    def setUp(self) -> None:
        reset_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        reset_config()
        self.tmp.cleanup()

    def test_insights_defs_and_defaults(self) -> None:
        from core.config import (
            DEFAULT_KERNEL_INSIGHTS,
            DEFAULT_MANPAGES_INSIGHTS,
            PROJECT_INSIGHTS_DEFS,
            PROJECT_INSIGHTS_KEYS,
            default_project_insights,
        )

        self.assertEqual(len(PROJECT_INSIGHTS_DEFS), 10)
        self.assertEqual(len(PROJECT_INSIGHTS_KEYS), 10)
        self.assertTrue(DEFAULT_KERNEL_INSIGHTS["merge_window"])
        self.assertEqual(DEFAULT_KERNEL_INSIGHTS["lead_mergers"], ["Linus Torvalds"])
        self.assertEqual(DEFAULT_KERNEL_INSIGHTS["maintainers_file"], "MAINTAINERS")
        self.assertEqual(DEFAULT_KERNEL_INSIGHTS["catchall_section"], "THE REST")
        self.assertEqual(DEFAULT_KERNEL_INSIGHTS["generic_repo_names"], ["linux", "linux-2.6", "kernel"])
        self.assertEqual(DEFAULT_KERNEL_INSIGHTS["generic_ref_words"], ["linus", "torvalds", "linux", "next"])
        self.assertEqual(DEFAULT_KERNEL_INSIGHTS["milestone_rc"], 1)
        self.assertEqual(DEFAULT_KERNEL_INSIGHTS["orphan_min_commits"], 5)
        self.assertEqual(DEFAULT_KERNEL_INSIGHTS["health_min_commits"], 10)
        self.assertEqual(DEFAULT_KERNEL_INSIGHTS["no_review_threshold"], 0.10)

        self.assertFalse(DEFAULT_MANPAGES_INSIGHTS["merge_window"])
        self.assertEqual(DEFAULT_MANPAGES_INSIGHTS["lead_mergers"], [])
        self.assertEqual(DEFAULT_MANPAGES_INSIGHTS["maintainers_file"], "")
        self.assertEqual(DEFAULT_MANPAGES_INSIGHTS["catchall_section"], "")
        self.assertEqual(DEFAULT_MANPAGES_INSIGHTS["generic_repo_names"], ["man-pages"])
        self.assertEqual(DEFAULT_MANPAGES_INSIGHTS["generic_ref_words"], ["man", "pages", "next"])
        self.assertEqual(DEFAULT_MANPAGES_INSIGHTS["milestone_rc"], 0)

        self.assertEqual(default_project_insights("linux"), DEFAULT_KERNEL_INSIGHTS)
        self.assertEqual(default_project_insights(None), DEFAULT_KERNEL_INSIGHTS)
        self.assertEqual(default_project_insights("man-pages"), DEFAULT_MANPAGES_INSIGHTS)

    def test_custom_project_insights_defaults(self) -> None:
        from core.config import default_project_insights

        custom = default_project_insights(
            "custom-tool",
            {"name": "custom-tool", "git_url": "https://example.org/git/custom-tool.git"},
        )
        self.assertEqual(custom["lead_mergers"], [])
        self.assertEqual(custom["generic_repo_names"], ["custom-tool"])
        self.assertEqual(custom["generic_ref_words"], ["custom-tool", "next"])
        self.assertTrue(custom["merge_window"])
        self.assertEqual(custom["maintainers_file"], "MAINTAINERS")

    def test_normalize_insights_overrides(self) -> None:
        from core.config import normalize_insights_overrides

        self.assertEqual(normalize_insights_overrides(None), {})
        self.assertEqual(normalize_insights_overrides(["lead_mergers"]), {})

        normalized = normalize_insights_overrides({
            "merge_window": "false",
            "lead_mergers": "Alice, Bob",
            "maintainers_file": "  DOCS/MAINTAINERS  ",
            "milestone_rc": "2",
            "orphan_min_commits": -5,  # invalid (< 0) -> dropped
            "health_min_commits": 15,
            "no_review_threshold": "0.25",
            "unknown_key": "ignored",
        })
        self.assertEqual(normalized["merge_window"], False)
        self.assertEqual(normalized["lead_mergers"], ["Alice", "Bob"])
        self.assertEqual(normalized["maintainers_file"], "DOCS/MAINTAINERS")
        self.assertEqual(normalized["milestone_rc"], 2)
        self.assertNotIn("orphan_min_commits", normalized)
        self.assertEqual(normalized["health_min_commits"], 15)
        self.assertEqual(normalized["no_review_threshold"], 0.25)
        self.assertNotIn("unknown_key", normalized)

    def test_resolve_and_prune_insights(self) -> None:
        from core.config import (
            DEFAULT_KERNEL_INSIGHTS,
            prune_default_insights_overrides,
            resolve_repo_insights,
        )

        entry = {"project": "linux", "insights": {"milestone_rc": 2, "merge_window": True}}
        resolved = resolve_repo_insights(entry)
        self.assertEqual(resolved["milestone_rc"], 2)
        self.assertTrue(resolved["merge_window"])
        self.assertEqual(resolved["maintainers_file"], DEFAULT_KERNEL_INSIGHTS["maintainers_file"])

        # Prune keeps only milestone_rc since merge_window=True is the default
        pruned = prune_default_insights_overrides("linux", {"milestone_rc": 2, "merge_window": True})
        self.assertEqual(pruned, {"milestone_rc": 2})

    def test_detect_repository_insights(self) -> None:
        from core.config import detect_repository_insights

        repo_path = self.root / "myrepo"
        _make_git_repo(repo_path, commits=2)

        # Add a MAINTAINERS file with THE REST
        maint = repo_path / "MAINTAINERS"
        maint.write_text("THE REST\nM: Alice <alice@example.org>\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "a@a"}
        subprocess.run(["git", "-C", str(repo_path), "add", "MAINTAINERS"], check=True, env=env)
        subprocess.run(["git", "-C", str(repo_path), "commit", "-q", "-m", "add maintainers"], check=True, env=env)

        # Add a branch and merge it with committer Alice
        subprocess.run(["git", "-C", str(repo_path), "checkout", "-q", "-b", "feature"], check=True, env=env)
        (repo_path / "feat.txt").write_text("feat\n")
        subprocess.run(["git", "-C", str(repo_path), "add", "feat.txt"], check=True, env=env)
        subprocess.run(["git", "-C", str(repo_path), "commit", "-q", "-m", "feature commit"], check=True, env=env)
        subprocess.run(["git", "-C", str(repo_path), "checkout", "-q", "main"], check=True, env=env)
        subprocess.run(["git", "-C", str(repo_path), "merge", "--no-ff", "-q", "feature", "-m", "merge feature"], check=True, env=env)

        # Add a pre-release tag
        subprocess.run(["git", "-C", str(repo_path), "tag", "v1.0-rc1"], check=True, env=env)

        detected = detect_repository_insights(repo_path, project_slug="myrepo", default_branch="main")
        self.assertEqual(detected["maintainers_file"], "MAINTAINERS")
        self.assertEqual(detected["catchall_section"], "THE REST")
        self.assertEqual(detected["milestone_rc"], 1)
        self.assertTrue(detected["merge_window"])
        self.assertEqual(detected["lead_mergers"], ["Alice"])

    def test_insights_sparse_in_detect_and_normalized_in_merge_and_load(self) -> None:
        from core.config import (
            OPTIONAL_REPOSITORY_KEYS,
            REPOSITORY_KEYS,
            _merge_repositories,
            _missing_repo_keys,
            detect_repository,
            load_config,
        )

        self.assertIn("insights", REPOSITORY_KEYS)
        self.assertIn("insights", OPTIONAL_REPOSITORY_KEYS)

        repo_path = self.root / "linux"
        _make_git_repo(repo_path, commits=2)
        entry = detect_repository(repo_path)
        self.assertNotIn("insights", entry)
        self.assertEqual(_missing_repo_keys(entry), [])

        merged, _ = _merge_repositories(
            [
                {**entry, "insights": {"milestone_rc": 3, "invalid": True}},
                {**entry, "path": str(repo_path), "insights": {"invalid": True}},
            ],
            [repo_path],
        )
        self.assertEqual(merged[0]["insights"], {"milestone_rc": 3})
        self.assertNotIn("insights", merged[1])

        cfg_path = self.root / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "repositories": [
                        {"name": "linux", "path": "repos/linux", "project": "linux", "insights": {"milestone_rc": 2}},
                        {"name": "empty", "path": "repos/empty", "project": "linux", "insights": {}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        loaded = load_config(cfg_path)
        self.assertEqual(loaded["repositories"][0]["insights"], {"milestone_rc": 2})
        self.assertNotIn("insights", loaded["repositories"][1])

    def test_preparse_defaults_normalization_and_pruning(self) -> None:
        from core.config import (
            DEFAULT_PREPARSE_SPEC,
            OPTIONAL_REPOSITORY_KEYS,
            PREPARSE_FEATURES,
            PREPARSE_MODES,
            REPOSITORY_KEYS,
            default_repo_preparse,
            normalize_preparse_overrides,
            normalize_preparse_spec,
            prune_default_preparse_overrides,
            resolve_repo_preparse,
        )

        self.assertEqual(PREPARSE_FEATURES, ("insights", "api"))
        self.assertEqual(PREPARSE_MODES, ("all_main", "latest_n_main", "groups", "custom"))
        self.assertIn("preparse", REPOSITORY_KEYS)
        self.assertIn("preparse", OPTIONAL_REPOSITORY_KEYS)

        defaults = default_repo_preparse("linux")
        self.assertEqual(set(defaults), {"insights", "api"})
        self.assertEqual(defaults["insights"], DEFAULT_PREPARSE_SPEC)
        self.assertEqual(defaults["api"], DEFAULT_PREPARSE_SPEC)
        self.assertIsNot(defaults["insights"], defaults["api"])

        self.assertEqual(normalize_preparse_spec(None), DEFAULT_PREPARSE_SPEC)
        spec = normalize_preparse_spec({
            "mode": "LATEST_N_MAIN",
            "latest_n": 0,
            "include_main": "false",
            "include_stable": True,
            "include_rc": "yes",
            "versions": ["v6.6", " ", "v6.6", "v6.7-rc1"],
        })
        self.assertEqual(spec, {
            "mode": "latest_n_main",
            "latest_n": 1,
            "include_main": False,
            "include_stable": True,
            "include_rc": True,
            "versions": ["v6.6", "v6.7-rc1"],
        })

        overrides = normalize_preparse_overrides({
            "API": {"mode": "custom", "versions": ["v6.6"]},
            "unknown": {"mode": "all_main"},
        })
        self.assertEqual(set(overrides), {"api"})
        self.assertEqual(overrides["api"]["mode"], "custom")
        self.assertEqual(overrides["api"]["versions"], ["v6.6"])

        resolved = resolve_repo_preparse({"project": "linux", "preparse": {"api": {"mode": "custom", "versions": ["v6.6"]}}})
        self.assertEqual(resolved["insights"]["mode"], "all_main")
        self.assertEqual(resolved["api"]["mode"], "custom")
        self.assertEqual(resolved["api"]["versions"], ["v6.6"])

        pruned = prune_default_preparse_overrides("linux", {
            "insights": dict(DEFAULT_PREPARSE_SPEC),
            "api": {"mode": "latest_n_main", "latest_n": 5},
        })
        self.assertEqual(set(pruned), {"api"})
        self.assertEqual(pruned["api"]["mode"], "latest_n_main")
        self.assertEqual(pruned["api"]["latest_n"], 5)

    def test_resolve_preparse_tags_modes_and_classification(self) -> None:
        from core.config import is_tag_selected_for_preparse, resolve_preparse_tags

        tags = [
            "v2.6.39",
            "v2.6.39.4",
            "v6.5",
            "v6.6-rc1",
            "v6.6",
            "v6.6.1",
            "v6.7-rc2",
            "v6.7",
            "next-20250101",
            "unrelated-tag",
        ]

        # Default (all_main)
        self.assertEqual(
            resolve_preparse_tags(None, "insights", tags),
            ["v2.6.39", "v6.5", "v6.6", "v6.7"],
        )
        self.assertTrue(is_tag_selected_for_preparse(None, "api", "v6.6", tags))
        self.assertFalse(is_tag_selected_for_preparse(None, "api", "v6.6.1", tags))
        self.assertFalse(is_tag_selected_for_preparse(None, "api", "v6.6-rc1", tags))

        # latest_n_main + explicit version
        entry_latest = {
            "preparse": {
                "api": {"mode": "latest_n_main", "latest_n": 2, "versions": ["v2.6.39.4"]},
            }
        }
        self.assertEqual(
            resolve_preparse_tags(entry_latest, "api", tags),
            ["v2.6.39.4", "v6.6", "v6.7"],
        )
        # Insights remains independent (default all_main)
        self.assertEqual(
            resolve_preparse_tags(entry_latest, "insights", tags),
            ["v2.6.39", "v6.5", "v6.6", "v6.7"],
        )

        # groups: stable + rc + explicit
        entry_groups = {
            "preparse": {
                "insights": {
                    "mode": "groups",
                    "include_main": False,
                    "include_stable": True,
                    "include_rc": True,
                    "versions": ["v6.5"],
                }
            }
        }
        self.assertEqual(
            resolve_preparse_tags(entry_groups, "insights", tags),
            ["v2.6.39.4", "v6.5", "v6.6-rc1", "v6.6.1", "v6.7-rc2"],
        )

        # custom: only explicit versions present in tags
        entry_custom = {
            "preparse": {
                "api": {"mode": "custom", "versions": ["v6.6", "v6.6-rc1", "v99.0"]},
            }
        }
        self.assertEqual(
            resolve_preparse_tags(entry_custom, "api", tags),
            ["v6.6-rc1", "v6.6"],
        )

    def test_preparse_normalized_in_load_config(self) -> None:
        from core.config import load_config

        cfg_path = self.root / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "repositories": [
                        {
                            "name": "linux",
                            "path": "repos/linux",
                            "project": "linux",
                            "preparse": {
                                "api": {"mode": "latest_n_main", "latest_n": 3},
                                "bogus": {"mode": "custom"},
                            },
                        },
                        {
                            "name": "empty",
                            "path": "repos/empty",
                            "project": "linux",
                            "preparse": {},
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        loaded = load_config(cfg_path)
        self.assertEqual(set(loaded["repositories"][0]["preparse"]), {"api"})
        self.assertEqual(loaded["repositories"][0]["preparse"]["api"]["mode"], "latest_n_main")
        self.assertEqual(loaded["repositories"][0]["preparse"]["api"]["latest_n"], 3)
        self.assertNotIn("preparse", loaded["repositories"][1])


class TestServiceConfigIsolation(unittest.TestCase):
    """Standalone service config seeding, pruning, validation, env fallback, and entrypoint wiring."""

    def setUp(self) -> None:
        import copy

        import core.config

        self._orig_cfg = copy.deepcopy(core.config._ACTIVE_CONFIG)
        self._orig_cfg_path = core.config._LOADED_CONFIG_PATH
        reset_config()
        self.original_env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        import copy

        import core.config

        reset_config()
        os.environ.clear()
        os.environ.update(self.original_env)
        core.config._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        core.config._LOADED_CONFIG_PATH = self._orig_cfg_path
        self._tmp.cleanup()

    def test_find_config_path_kernelinfo_config_fallback_and_missing_errors(self) -> None:
        import core.config

        default_cfg = self.root / "config.json"
        default_cfg.write_text("{}", encoding="utf-8")
        ki_cfg = self.root / "ki.json"
        ki_cfg.write_text("{}", encoding="utf-8")
        cf_cfg = self.root / "cf.json"
        cf_cfg.write_text("{}", encoding="utf-8")
        cli_cfg = self.root / "cli.json"
        cli_cfg.write_text("{}", encoding="utf-8")

        for k in ("CONFIG_FILE", "CONFIG_PATH", "KERNELINFO_CONFIG"):
            os.environ.pop(k, None)

        with patch.object(core.config, "REPO_ROOT", self.root):
            # 1. Falls back to REPO_ROOT/config.json when no env/custom path
            self.assertEqual(find_config_path(), default_cfg)

            # 2. KERNELINFO_CONFIG overrides REPO_ROOT/config.json
            os.environ["KERNELINFO_CONFIG"] = str(ki_cfg)
            self.assertEqual(find_config_path(), ki_cfg.resolve())

            # 3. CONFIG_FILE overrides KERNELINFO_CONFIG
            os.environ["CONFIG_FILE"] = str(cf_cfg)
            self.assertEqual(find_config_path(), cf_cfg.resolve())

            # 4. Explicit custom_path overrides everything
            self.assertEqual(find_config_path(cli_cfg), cli_cfg.resolve())

            # 5. Missing explicit custom_path raises FileNotFoundError (never falls back to config.json)
            with self.assertRaises(FileNotFoundError):
                find_config_path(self.root / "does_not_exist.json")

            # 6. Missing KERNELINFO_CONFIG raises FileNotFoundError (never falls back to config.json)
            os.environ.pop("CONFIG_FILE", None)
            os.environ.pop("CONFIG_PATH", None)
            os.environ["KERNELINFO_CONFIG"] = str(self.root / "missing_ki.json")
            with self.assertRaises(FileNotFoundError):
                find_config_path()

    def test_get_service_config_path(self) -> None:
        from core.config import (
            DAEMON_CONFIG_FILENAME,
            REPO_ROOT,
            WEBAPP_CONFIG_FILENAME,
            get_service_config_path,
        )

        self.assertEqual(get_service_config_path("daemon"), REPO_ROOT / DAEMON_CONFIG_FILENAME)
        self.assertEqual(get_service_config_path("webapp"), REPO_ROOT / WEBAPP_CONFIG_FILENAME)
        with self.assertRaises(ValueError):
            get_service_config_path("unknown")

    def test_prune_webapp_config_omits_parser_only_and_keeps_webapp_sections(self) -> None:
        from core.config import prune_webapp_config

        sample = {
            "database": {
                "host": "db.internal",
                "port": 3306,
                "user": "kip",
                "password": "secret",
                "database": "kernelinfo",
                "engine": "mariadb",
                "unix_socket": "auto",
                "ssl_disabled": False,
                "server_tuning": {"innodb_buffer_pool_size": "8G"},
                "resolved_unix_socket": "/run/mysqld/mysqld.sock",
            },
            "ssh_tunnel": {"enabled": True, "host": "bastion.internal"},
            "cache": {"engine": "sqlite", "dir": "/tmp/kip-cache"},
            "webapp": {
                "host": "127.0.0.1",
                "port": 8080,
                "reload": True,
                "cache": {"max_mb": 512},
                "jobs": {"workers": 4},
                "prewarm_indexes": False,
                "tools": {"checkpatch": {"enabled": False, "ref": "latest-release"}, "sandbox": ""},
                "source_index": {"workers": 2, "max_mb": 1024, "revisions": 8},
            },
            "cve": {"repo_dir": "cvelistV5"},
            "parser": {"table_engine": "cached", "memory_mode": "low", "mem_max": 32},
            "mailing_list": {"storage_dir": "/srv/lore", "port": 8192},
            "workspace": {"repos_dir": "repos", "auto_detect_repos": True},
            "logging": {
                "parser": {"enabled": True, "file": "parser.log"},
                "webapp": {"enabled": True, "file": "webapp.log", "request_mode": "errors"},
            },
            "repositories": [
                {
                    "name": "linux",
                    "path": "repos/linux",
                    "features": {"kconfig": True},
                    "insights": {"milestone_rc": 1},
                    "preparse": {"api": {"mode": "latest_n_main", "latest_n": 3}},
                }
            ],
        }
        pruned = prune_webapp_config(sample)

        # Input is untouched
        self.assertIn("parser", sample)
        self.assertIn("server_tuning", sample["database"])
        self.assertIn("preparse", sample["repositories"][0])

        # Parser-only keys removed
        self.assertNotIn("parser", pruned)
        self.assertNotIn("ssh_tunnel", pruned)
        self.assertNotIn("server_tuning", pruned["database"])
        self.assertNotIn("resolved_unix_socket", pruned["database"])
        self.assertNotIn("parser", pruned["logging"])
        self.assertNotIn("preparse", pruned["repositories"][0])

        # Webapp-used sections & migrated tunnel preserved
        self.assertEqual(pruned["database"]["ssh_tunnel"]["host"], "bastion.internal")
        self.assertEqual(pruned["cache"], sample["cache"])
        self.assertEqual(pruned["webapp"], sample["webapp"])
        self.assertEqual(pruned["cve"], sample["cve"])
        self.assertEqual(pruned["mailing_list"], sample["mailing_list"])
        self.assertEqual(pruned["workspace"], sample["workspace"])
        self.assertEqual(pruned["logging"]["webapp"], sample["logging"]["webapp"])
        self.assertEqual(pruned["repositories"][0]["features"], {"kconfig": True})
        self.assertEqual(pruned["repositories"][0]["insights"], {"milestone_rc": 1})

    def test_ensure_standalone_config_bootstrap_only(self) -> None:
        from core.config import ensure_standalone_config

        base_cfg = self.root / "config.json"
        base_cfg.write_text(
            json.dumps(
                {
                    "database": {
                        "host": "10.1.2.3",
                        "user": "reader",
                        "password": "pw",
                        "engine": "mariadb",
                        "server_tuning": {"innodb_buffer_pool_size": "4G"},
                    },
                    "parser": {"mem_max": 24, "distributed": {"enabled": False, "role": "standalone"}},
                    "webapp": {"port": 9001, "reload": True},
                    "cve": {"repo_dir": "vulns"},
                    "mailing_list": {"storage_dir": "/data/lore"},
                    "workspace": {"repos_dir": "trees", "auto_detect_repos": False},
                    "logging": {
                        "parser": {"enabled": True, "file": "p.log"},
                        "webapp": {"enabled": True, "file": "w.log"},
                    },
                    "repositories": [
                        {
                            "name": "linux",
                            "path": "trees/linux",
                            "preparse": {"insights": {"mode": "all_final"}},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        daemon_target = self.root / "config_daemon.json"
        path_d, created_d = ensure_standalone_config("daemon", config_path=daemon_target, base_config_path=base_cfg)
        self.assertEqual(path_d, daemon_target.resolve())
        self.assertTrue(created_d)
        d_data = json.loads(daemon_target.read_text(encoding="utf-8"))
        self.assertEqual(d_data["database"]["host"], "10.1.2.3")
        self.assertEqual(d_data["parser"]["mem_max"], 24)
        self.assertTrue(d_data["parser"]["distributed"]["enabled"])
        self.assertEqual(d_data["parser"]["distributed"]["role"], "daemon")
        self.assertEqual(d_data["cve"]["repo_dir"], "vulns")
        self.assertEqual(d_data["mailing_list"]["storage_dir"], "/data/lore")

        webapp_target = self.root / "config_webapp.json"
        path_w, created_w = ensure_standalone_config("webapp", config_path=webapp_target, base_config_path=base_cfg)
        self.assertEqual(path_w, webapp_target.resolve())
        self.assertTrue(created_w)
        w_data = json.loads(webapp_target.read_text(encoding="utf-8"))
        self.assertEqual(w_data["database"]["host"], "10.1.2.3")
        self.assertNotIn("server_tuning", w_data["database"])
        self.assertNotIn("parser", w_data)
        self.assertNotIn("parser", w_data["logging"])
        self.assertNotIn("preparse", w_data["repositories"][0])
        self.assertEqual(w_data["webapp"]["port"], 9001)
        self.assertFalse(w_data["webapp"]["reload"])

        # Bootstrap-only: mutating base config.json must never overwrite existing standalone configs
        w_data["webapp"]["port"] = 7777
        w_data["webapp"]["reload"] = True
        webapp_target.write_text(json.dumps(w_data), encoding="utf-8")
        base_cfg.write_text(json.dumps({"database": {"host": "overwritten"}, "webapp": {"port": 1111}}), encoding="utf-8")

        path_w2, created_w2 = ensure_standalone_config("webapp", config_path=webapp_target, base_config_path=base_cfg)
        self.assertEqual(path_w2, webapp_target.resolve())
        self.assertFalse(created_w2)
        w_after = json.loads(webapp_target.read_text(encoding="utf-8"))
        self.assertEqual(w_after["webapp"]["port"], 7777)
        self.assertTrue(w_after["webapp"]["reload"])
        self.assertEqual(w_after["database"]["host"], "10.1.2.3")

        # When base_config_path does not exist, seeds cleanly from DEFAULT_CONFIG
        fresh_daemon = self.root / "fresh_daemon.json"
        _, created_fresh = ensure_standalone_config(
            "daemon", config_path=fresh_daemon, base_config_path=self.root / "no_base.json"
        )
        self.assertTrue(created_fresh)
        fd_data = json.loads(fresh_daemon.read_text(encoding="utf-8"))
        self.assertTrue(fd_data["parser"]["distributed"]["enabled"])
        self.assertEqual(fd_data["parser"]["distributed"]["role"], "daemon")

    def test_validate_service_config(self) -> None:
        import core.config
        from core.config import validate_service_config

        # Pre-seed active config to verify validate_service_config does not mutate global state
        active_path = self.root / "active.json"
        active_path.write_text(json.dumps({"database": {"host": "active.db", "engine": "sqlite", "path": ":memory:"}}), encoding="utf-8")
        init_config(active_path)
        before_active = dict(core.config._ACTIVE_CONFIG["database"])
        before_loaded_path = core.config._LOADED_CONFIG_PATH
        before_db_host_env = os.environ.get("DB_HOST")

        valid_wa = self.root / "valid_wa.json"
        valid_wa.write_text(
            json.dumps({"database": {"host": "other.db", "engine": "sqlite", "path": ":memory:"}, "webapp": {"port": 8081}}),
            encoding="utf-8",
        )
        ok, errs, cfg = validate_service_config("webapp", valid_wa)
        self.assertTrue(ok)
        self.assertEqual(errs, [])
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["webapp"]["port"], 8081)

        # Global config state and env are completely unchanged
        self.assertEqual(core.config._ACTIVE_CONFIG["database"]["host"], before_active["host"])
        self.assertEqual(core.config._LOADED_CONFIG_PATH, before_loaded_path)
        self.assertEqual(os.environ.get("DB_HOST"), before_db_host_env)

        # Missing file
        ok, errs, cfg = validate_service_config("webapp", self.root / "missing.json")
        self.assertFalse(ok)
        self.assertTrue(errs)
        self.assertIsNone(cfg)

        # Invalid port & unknown db engine
        bad_wa = self.root / "bad_wa.json"
        bad_wa.write_text(json.dumps({"database": {"engine": "bogusdb"}, "webapp": {"port": 70000}}), encoding="utf-8")
        ok, errs, cfg = validate_service_config("webapp", bad_wa)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(errs), 2)
        self.assertIsNone(cfg)

        # Valid daemon vs invalid daemon
        valid_daemon = self.root / "valid_daemon.json"
        valid_daemon.write_text(
            json.dumps({"database": {"engine": "mock"}, "parser": {"table_engine": "cached", "mem_max": 32, "distributed": {"role": "daemon"}}}),
            encoding="utf-8",
        )
        ok, errs, cfg = validate_service_config("daemon", valid_daemon)
        self.assertTrue(ok)
        self.assertEqual(errs, [])
        self.assertIsNotNone(cfg)

        bad_daemon = self.root / "bad_daemon.json"
        bad_daemon.write_text(
            json.dumps({"database": {"engine": "mock"}, "parser": {"table_engine": "invalid", "distributed": {"role": "bogus"}}}),
            encoding="utf-8",
        )
        ok, errs, cfg = validate_service_config("daemon", bad_daemon)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(errs), 2)
        self.assertIsNone(cfg)

    def test_cli_configure_allows_missing_config_and_sync_writes_to_custom_config(self) -> None:
        import core.config
        import pipeline.cli

        new_cfg = self.root / "new_daemon.json"
        with (
            mock.patch("sys.argv", ["main.py", "--configure", "--config", str(new_cfg)]),
            mock.patch("core.tui.app.run_config_tui_launch", return_value=None) as mock_tui,
            self.assertRaises(SystemExit) as cm,
        ):
            pipeline.cli.arg_handling()
        self.assertEqual(cm.exception.code, 0)
        mock_tui.assert_called_once_with(str(new_cfg))

        # Non-TUI run with missing --config raises FileNotFoundError (no fallback to config.json)
        with (
            mock.patch("sys.argv", ["main.py", "--role", "daemon", "--config", str(new_cfg)]),
            self.assertRaises(FileNotFoundError),
        ):
            pipeline.cli.arg_handling()

        # sync_repositories(write=True) writes to the --config file, not REPO_ROOT/config.json
        repos_dir = self.root / "repos"
        repos_dir.mkdir()
        _make_git_repo(repos_dir / "linux", commits=2, tags={1: "v3.0"})
        default_cfg = self.root / "config.json"
        default_cfg.write_text(json.dumps({"workspace": {"repos_dir": str(repos_dir), "auto_detect_repos": False}}), encoding="utf-8")
        daemon_cfg = self.root / "config_daemon.json"
        daemon_cfg.write_text(
            json.dumps(
                {
                    "database": {"engine": "mock"},
                    "workspace": {"repos_dir": str(repos_dir), "auto_detect_repos": True},
                    "parser": {"distributed": {"enabled": True, "role": "daemon"}},
                }
            ),
            encoding="utf-8",
        )
        with patch.object(core.config, "REPO_ROOT", self.root):
            with mock.patch("sys.argv", ["main.py", "--role", "daemon", "--config", str(daemon_cfg)]):
                pipeline.cli.arg_handling()
            saved_daemon = json.loads(daemon_cfg.read_text(encoding="utf-8"))
            saved_default = json.loads(default_cfg.read_text(encoding="utf-8"))
            self.assertEqual(len(saved_daemon.get("repositories") or []), 1)
            self.assertEqual(saved_daemon["repositories"][0]["name"], "linux")
            self.assertNotIn("repositories", saved_default)

    def test_webapp_backend_config_resolves_after_late_init_config(self) -> None:
        from webapp.backend import app as webapp_app
        from webapp.backend import config as webapp_config

        repo_a = self.root / "repo_a"
        repo_b = self.root / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()
        cfg_a = self.root / "wa_a.json"
        cfg_b = self.root / "wa_b.json"
        cfg_a.write_text(
            json.dumps({"workspace": {"auto_detect_repos": False}, "repositories": [{"name": "a", "path": str(repo_a), "default": True}]}),
            encoding="utf-8",
        )
        cfg_b.write_text(
            json.dumps({"workspace": {"auto_detect_repos": False}, "repositories": [{"name": "b", "path": str(repo_b), "default": True}]}),
            encoding="utf-8",
        )

        init_config(cfg_a)
        webapp_config.refresh_default_repo_dir()
        self.assertEqual(webapp_config.get_repo_dir(), repo_a.resolve())

        init_config(cfg_b)
        self.assertEqual(webapp_config.get_repo_dir(), repo_b.resolve())
        webapp_app.create_app()
        self.assertEqual(webapp_config.LINUX_REPO_DIR, repo_b.resolve())

    def test_setup_memory_limit_skips_systemd_run_without_user_session(self) -> None:
        import core.globalstuff as gs

        orig_initialized = gs._MEMORY_GUARD_INITIALIZED
        try:
            os.environ.pop("XDG_RUNTIME_DIR", None)
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            os.environ.pop("KERNELINFO_CGROUP_ACTIVE", None)

            gs._MEMORY_GUARD_INITIALIZED = False
            with (
                mock.patch.object(gs.sys, "platform", "linux"),
                mock.patch.object(gs, "is_test_environment", return_value=False),
                mock.patch.object(gs.os.path, "exists", return_value=False),
                mock.patch.object(gs.shutil, "which", return_value="/usr/bin/systemd-run"),
                mock.patch.object(gs.os, "execvp") as mock_execvp,
                mock.patch("resource.getrlimit", return_value=(-1, -1)),
                mock.patch("resource.setrlimit") as mock_setrlimit,
            ):
                gs.setup_memory_limit(10.0)
                mock_execvp.assert_not_called()
                mock_setrlimit.assert_called_once()

            # When XDG_RUNTIME_DIR is present, systemd-run --user is attempted
            gs._MEMORY_GUARD_INITIALIZED = False
            os.environ["XDG_RUNTIME_DIR"] = "/run/user/1000"
            with (
                mock.patch.object(gs.sys, "platform", "linux"),
                mock.patch.object(gs, "is_test_environment", return_value=False),
                mock.patch.object(gs.os.path, "exists", return_value=False),
                mock.patch.object(gs.shutil, "which", return_value="/usr/bin/systemd-run"),
                mock.patch.object(gs.os, "execvp") as mock_execvp,
                mock.patch("resource.getrlimit", return_value=(-1, -1)),
                mock.patch("resource.setrlimit"),
            ):
                gs.setup_memory_limit(10.0)
                mock_execvp.assert_called_once()
                self.assertEqual(mock_execvp.call_args.args[0], "systemd-run")
                self.assertIn("--user", mock_execvp.call_args.args[1])
        finally:
            gs._MEMORY_GUARD_INITIALIZED = orig_initialized

    def test_api_config_defaults_file_env_and_cli(self) -> None:
        """`api` section defaults, config.json overrides, env-var precedence, and api/main.py CLI flags."""
        from api import main as api_main
        from api.config import get_api_server_config, normalize_api_keys
        from core.config import get_api_config

        with patch("core.config.find_config_path", return_value=None):
            cfg = load_config(config_path=None)
        api_cfg = cfg["api"]
        self.assertEqual(api_cfg["host"], "127.0.0.1")
        self.assertEqual(api_cfg["port"], 8001)
        self.assertFalse(api_cfg["reload"])
        self.assertEqual(api_cfg["default_version"], "v3.0")
        self.assertEqual(api_cfg["keys"], [])
        self.assertEqual(api_cfg["rate_limit"], {"max_requests": 120, "window_seconds": 60})
        self.assertEqual(
            api_cfg["auth_lockout"],
            {
                "max_failures": 5,
                "failure_window_seconds": 60,
                "base_lockout_seconds": 30,
                "max_lockout_seconds": 3600,
            },
        )
        self.assertEqual(api_cfg["trusted_proxies"], ["127.0.0.1", "::1"])
        self.assertEqual(api_cfg["client_ip_header"], "X-Forwarded-For")

        # Key normalization supports list[str] and {name: key}, rejecting blanks/non-strings
        self.assertEqual(normalize_api_keys([" k1 ", "", 123]), [("key_0", "k1")])
        self.assertEqual(normalize_api_keys({"agent": " s1 ", "empty": "  "}), [("agent", "s1")])
        self.assertEqual(normalize_api_keys(None), [])

        custom_file = self.root / "api_cfg.json"
        custom_file.write_text(
            json.dumps(
                {
                    "api": {
                        "host": "0.0.0.0",
                        "port": 8111,
                        "reload": True,
                        "default_version": "v6.6",
                        "keys": {"ci": "secret-ci"},
                        "rate_limit": {"max_requests": 50, "window_seconds": 30},
                        "auth_lockout": {
                            "max_failures": 3,
                            "failure_window_seconds": 120,
                            "base_lockout_seconds": 10,
                            "max_lockout_seconds": 600,
                        },
                        "trusted_proxies": ["10.0.0.0/8"],
                        "client_ip_header": "X-Real-IP",
                    }
                }
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(
            os.environ,
            {
                "API_PORT": "8222",
                "API_KEYS": "env-key-1, env-key-2",
                "API_CLIENT_IP_HEADER": "CF-Connecting-IP",
            },
            clear=False,
        ):
            init_config(custom_file)
            loaded_api = get_api_config()
            self.assertEqual(loaded_api["host"], "0.0.0.0")
            self.assertEqual(loaded_api["port"], 8222)
            self.assertTrue(loaded_api["reload"])
            self.assertEqual(loaded_api["default_version"], "v6.6")
            self.assertEqual(loaded_api["client_ip_header"], "CF-Connecting-IP")
            from core.config import normalize_api_client_ip_header
            self.assertEqual(normalize_api_client_ip_header("x-real-ip"), "X-Real-IP")
            self.assertEqual(normalize_api_client_ip_header("X-Evil-Header"), "X-Forwarded-For")
            self.assertEqual(normalize_api_client_ip_header(None), "X-Forwarded-For")
            self.assertEqual(loaded_api["trusted_proxies"], ["10.0.0.0/8"])
            srv = get_api_server_config()
            self.assertEqual(srv["normalized_keys"], [("key_0", "env-key-1"), ("key_1", "env-key-2")])

        args = api_main.parse_args(["-c", str(custom_file), "--host", "127.0.0.1", "--port", "8333", "--no-reload"])
        self.assertEqual(args.config, str(custom_file))
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8333)
        self.assertFalse(args.reload)
        self.assertIsNone(api_main.parse_args([]).reload)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIG_FILE", None)
            os.environ.pop("KERNELINFO_CONFIG", None)
            api_main.apply_config_path(args)
            self.assertEqual(os.environ["CONFIG_FILE"], str(custom_file.resolve()))
            self.assertEqual(os.environ["KERNELINFO_CONFIG"], str(custom_file.resolve()))


    def test_api_logging_section_defaults_and_env(self) -> None:
        """`logging.api` defaults, normalization, KIP_API_LOG_* overrides, and get_logging_config("api")."""
        from core.config import LOGGING_TARGETS, get_logging_config

        self.assertEqual(LOGGING_TARGETS, ("parser", "webapp", "api"))
        with patch("core.config.find_config_path", return_value=None):
            cfg = load_config(config_path=None)
        a_log = cfg["logging"]["api"]
        self.assertEqual(
            a_log,
            {"enabled": False, "dir": "", "file": "api.log", "request_mode": "all",
             "error_dir": "", "error_file": "", "level": "INFO"},
        )
        custom = self.root / "log_api.json"
        custom.write_text(json.dumps({"logging": {"api": {"enabled": True, "request_mode": "errors_only", "level": "warn"}}}))
        with mock.patch.dict(os.environ, {"KIP_API_LOG_FILE": "agents.log", "KIP_API_LOG_LEVEL": "debug"}, clear=False):
            init_config(custom)
            loaded = get_logging_config("api")
        self.assertTrue(loaded["enabled"])
        self.assertEqual(loaded["request_mode"], "errors")
        self.assertEqual(loaded["file"], "agents.log")
        self.assertEqual(loaded["level"], "DEBUG")
        with self.assertRaises(ValueError):
            get_logging_config("nope")

    def test_prune_api_config_keeps_only_api_sections(self) -> None:
        from core.config import prune_api_config

        sample = {
            "database": {"host": "db", "server_tuning": {"x": 1}, "resolved_unix_socket": "/s"},
            "ssh_tunnel": {"enabled": True, "host": "bastion"},
            "cache": {"engine": "sqlite"},
            "webapp": {"port": 8000},
            "api": {"port": 8001, "keys": ["k"]},
            "cve": {"repo_dir": "cvelistV5"},
            "parser": {"mem_max": 32},
            "mailing_list": {"storage_dir": "/srv"},
            "workspace": {"repos_dir": "repos"},
            "logging": {"parser": {"enabled": True}, "webapp": {"enabled": True}, "api": {"enabled": True, "file": "a.log"}},
            "repositories": [{"name": "linux", "features": {"kconfig": True}, "preparse": {"api": {"mode": "all_main"}}}],
        }
        pruned = prune_api_config(sample)
        self.assertEqual(sorted(pruned), ["api", "cache", "database", "logging", "repositories", "workspace"])
        self.assertEqual(pruned["logging"], {"api": {"enabled": True, "file": "a.log"}})
        self.assertEqual(pruned["database"]["ssh_tunnel"]["host"], "bastion")
        self.assertNotIn("server_tuning", pruned["database"])
        self.assertNotIn("preparse", pruned["repositories"][0])
        self.assertEqual(pruned["repositories"][0]["features"], {"kconfig": True})
        self.assertEqual(pruned["api"], {"port": 8001, "keys": ["k"]})
        # Input untouched
        self.assertIn("parser", sample)
        self.assertIn("webapp", sample["logging"])

    def test_ensure_standalone_config_api(self) -> None:
        from core.config import API_CONFIG_FILENAME, ensure_standalone_config, get_service_config_path

        self.assertEqual(API_CONFIG_FILENAME, "config_api.json")
        self.assertEqual(get_service_config_path("api").name, "config_api.json")
        base_cfg = self.root / "config.json"
        base_cfg.write_text(json.dumps({
            "database": {"host": "10.9.9.9", "server_tuning": {"a": 1}},
            "parser": {"mem_max": 8},
            "webapp": {"port": 9000},
            "api": {"port": 8123, "reload": True, "keys": {"agent": "s3cret"}},
            "logging": {"parser": {"enabled": True}, "api": {"enabled": True}},
        }))
        target = self.root / "out" / API_CONFIG_FILENAME
        path, created = ensure_standalone_config("api", config_path=target, base_config_path=base_cfg)
        self.assertTrue(created)
        seeded = json.loads(path.read_text())
        self.assertEqual(sorted(seeded), ["api", "cache", "database", "logging", "repositories", "workspace"])
        self.assertEqual(seeded["database"]["host"], "10.9.9.9")
        self.assertNotIn("server_tuning", seeded["database"])
        self.assertEqual(seeded["api"]["port"], 8123)
        self.assertFalse(seeded["api"]["reload"])
        self.assertEqual(seeded["api"]["keys"], {"agent": "s3cret"})
        self.assertEqual(list(seeded["logging"]), ["api"])
        self.assertTrue(seeded["logging"]["api"]["enabled"])
        # Never overwrites an existing file
        path.write_text('{"api": {"port": 1}}')
        _, created_again = ensure_standalone_config("api", config_path=target, base_config_path=base_cfg)
        self.assertFalse(created_again)
        self.assertEqual(json.loads(path.read_text()), {"api": {"port": 1}})


if __name__ == "__main__":
    unittest.main()
