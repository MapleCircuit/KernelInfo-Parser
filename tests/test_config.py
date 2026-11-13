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
    DEFAULT_BRANCH_FILTER,
    DEFAULT_CONFIG,
    DEFAULT_TAG_VIEWS,
    find_config_path,
    find_repository_branches,
    get_config,
    get_db_config,
    get_parser_config,
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
        import main
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

        args = webapp_main.parse_args(["-c", "custom.json", "--host", "127.0.0.1", "--port", "9099", "--no-reload"])
        self.assertEqual(args.config, "custom.json")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9099)
        self.assertFalse(args.reload)
        self.assertIsNone(webapp_main.parse_args([]).reload)

        # -c reaches core.config (and uvicorn's reloader process) as CONFIG_FILE
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIG_FILE", None)
            webapp_main.apply_config_path(args)
            self.assertEqual(os.environ["CONFIG_FILE"], str(Path("custom.json").resolve()))

    def test_default_continuous_parsing_arguments(self) -> None:
        """Verify pipeline.cli.arg_handling defaults single_version to False for continuous parsing."""
        import main
        import pipeline.cli
        with mock.patch("sys.argv", ["main.py"]):
            args = pipeline.cli.arg_handling()
            self.assertFalse(args.single_version)

    def test_cli_single_version_argument(self) -> None:
        """Verify pipeline.cli.arg_handling recognizes --single-version and --single flags."""
        import main
        import pipeline.cli
        with mock.patch("sys.argv", ["main.py", "--single-version"]):
            args = pipeline.cli.arg_handling()
            self.assertTrue(args.single_version)

        with mock.patch("sys.argv", ["main.py", "--single"]):
            args = pipeline.cli.arg_handling()
            self.assertTrue(args.single_version)

    def test_cli_no_rc_argument(self) -> None:
        """--no-rc / --releases-only disables pre-release discovery; the default includes them."""
        import main
        import pipeline.cli
        with mock.patch("sys.argv", ["main.py"]):
            self.assertTrue(pipeline.cli.arg_handling().include_pre_releases)
        with mock.patch("sys.argv", ["main.py", "--no-rc"]):
            self.assertFalse(pipeline.cli.arg_handling().include_pre_releases)
        with mock.patch("sys.argv", ["main.py", "--releases-only"]):
            self.assertFalse(pipeline.cli.arg_handling().include_pre_releases)

    def test_cli_rc_arguments(self) -> None:
        """Verify pipeline.cli.arg_handling recognizes --rc, --include-rc, and --include-pre-releases flags."""
        import main
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
        """Verify DEFAULT_TAG_VIEWS regex rules partition Linux 2.6.x and 3.x+ tags correctly."""
        import re

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

        # Linux 3.x+ family: base is v<major>.<minor>
        self.assertEqual(parse("v3.0"), ("v3.0", "", False))
        self.assertEqual(parse("v3.0-rc7"), ("v3.0", "-rc7", True))
        self.assertEqual(parse("v3.0.1"), ("v3.0", ".1", True))
        self.assertEqual(parse("v3.0.101"), ("v3.0", ".101", True))
        self.assertEqual(parse("v6.6"), ("v6.6", "", False))
        self.assertEqual(parse("v6.6.1"), ("v6.6", ".1", True))
        self.assertEqual(parse("v6.6-rc1"), ("v6.6", "-rc1", True))



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


if __name__ == "__main__":
    unittest.main()
