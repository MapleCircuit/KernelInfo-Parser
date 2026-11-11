"""Unit tests for core/tui/process_view.py and core/tui/git_ops.py (local git only, no network)."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.tui import git_ops
from core.tui.process_view import _LineBuffer, run_streaming_command


class TestLineBuffer(unittest.TestCase):
    def test_carriage_return_rewrites_current_line(self):
        buf = _LineBuffer()
        buf.feed("Cloning into 'linux'...\nReceiving: 1%\rReceiving: 5")
        self.assertEqual(buf.snapshot(), ["Cloning into 'linux'...", "Receiving: 5"])
        buf.feed("0%\rReceiving: 100%, done.\r\nResolving deltas\n")
        self.assertEqual(buf.snapshot(), ["Cloning into 'linux'...", "Receiving: 100%, done.", "Resolving deltas"])


class TestRunStreamingCommand(unittest.TestCase):
    def setUp(self):
        self.stdscr = MagicMock()
        self.stdscr.getmaxyx.return_value = (24, 80)
        self.stdscr.getch.return_value = -1

    def test_success_captures_merged_output(self):
        code = "import sys; print('out'); print('err', file=sys.stderr)"
        res = run_streaming_command(self.stdscr, [sys.executable, "-c", code], "Test")
        self.assertTrue(res.ok)
        self.assertEqual(sorted(res.lines), ["err", "out"])
        self.stdscr.timeout.assert_called_with(-1)

    def test_failure_exit_code(self):
        res = run_streaming_command(self.stdscr, [sys.executable, "-c", "raise SystemExit(3)"], "Test")
        self.assertFalse(res.ok)
        self.assertEqual(res.returncode, 3)

    def test_missing_binary_reports_error(self):
        res = run_streaming_command(self.stdscr, ["/nonexistent/binary-xyz"], "Test")
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.error)

    def test_cancel_stops_process(self):
        self.stdscr.getch.side_effect = [27] + [-1] * 50
        with patch("core.tui.process_view.ModalDialog.show_dialog", return_value="Stop"):
            res = run_streaming_command(
                self.stdscr, [sys.executable, "-c", "import time; time.sleep(30)"], "Test", wait_for_key=False
            )
        self.assertTrue(res.cancelled)
        self.assertFalse(res.ok)


class TestGitOps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)  # noqa: E731
        run("init", "-q")
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
        for tag in ("v3.0-rc1", "v3.0", "v2.6.39", "v3.1-rc2", "v3.1", "v2.6.11-tree", "topic-fixes"):
            run("tag", tag)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_version_tags_sorted_and_filtered(self):
        self.assertEqual(git_ops.list_version_tags(self.repo), ["v2.6.39", "v3.0-rc1", "v3.0", "v3.1-rc2", "v3.1"])
        self.assertEqual(git_ops.list_version_tags(self.repo, include_pre_releases=False), ["v2.6.39", "v3.0", "v3.1"])
        self.assertEqual(git_ops.list_version_tags(Path(self.tmp.name) / "missing"), [])

    def test_remote_add_list_remove(self):
        ok, _ = git_ops.add_remote(self.repo, "stable", git_ops.STANDARD_REMOTES[0].url)
        self.assertTrue(ok)
        self.assertEqual(git_ops.list_remotes(self.repo), [("stable", git_ops.STANDARD_REMOTES[0].url)])
        ok, msg = git_ops.add_remote(self.repo, "stable", "https://example.org/x.git")
        self.assertFalse(ok)
        self.assertIn("already exists", msg)
        self.assertTrue(git_ops.remove_remote(self.repo, "stable")[0])
        self.assertEqual(git_ops.list_remotes(self.repo), [])

    def test_remote_validation(self):
        for url in ("ssh://host/repo.git", "git@github.com:a/b.git", "http://host/repo.git", "https://", "file:///x"):
            self.assertFalse(git_ops.add_remote(self.repo, "custom", url)[0], url)
        for name in ("", "-x", "a b", "a..b", "x.lock"):
            self.assertFalse(git_ops.is_valid_remote_name(name), name)
        self.assertTrue(git_ops.add_remote(self.repo, "mine", "git://git.example.org/linux.git")[0])

    def test_commands(self):
        self.assertEqual(git_ops.clone_command("https://h/l.git", "/r/linux"),
                         ["git", "clone", "--progress", "https://h/l.git", "/r/linux"])
        self.assertEqual(git_ops.fetch_command("/r/linux", "stable")[-1], "stable")
        self.assertEqual(git_ops.fetch_command("/r/linux")[-1], "--all")
        self.assertEqual(git_ops.git_env()["GIT_TERMINAL_PROMPT"], "0")


if __name__ == "__main__":
    unittest.main()
