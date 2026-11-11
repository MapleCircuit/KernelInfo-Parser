"""tests/test_tui_output_capture.py - Output diverted away from the terminal while the TUI runs."""

import io
import logging
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from core.tui.app import TUIApp
from core.tui.output_capture import TerminalOutputCapture
from tests.test_tui import MockCursesWindow


class TestTerminalOutputCapture(unittest.TestCase):
    """TerminalOutputCapture diverts logging, print() and fd 2, then restores them."""

    def setUp(self) -> None:
        self.logger = logging.getLogger("tests.tui_capture")
        self.logger.setLevel(logging.INFO)
        self.terminal = io.StringIO()
        self.terminal.name = "<stderr>"
        self.handler = logging.StreamHandler(self.terminal)
        self.logger.addHandler(self.handler)
        self.logger.propagate = False

    def tearDown(self) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.propagate = True

    def test_log_records_are_held_and_replayed(self) -> None:
        cap = TerminalOutputCapture(redirect_fd=False)
        with cap:
            self.assertNotIn(self.handler, self.logger.handlers)
            self.logger.info("Establishing SSH tunnel: 127.0.0.1:3308")
        self.assertEqual(self.terminal.getvalue(), "")
        self.assertIn(self.handler, self.logger.handlers)
        self.assertEqual(len(cap.records), 1)
        self.assertGreater(cap.output_count, 0)

        cap.replay()
        self.assertIn("Establishing SSH tunnel", self.terminal.getvalue())
        self.assertEqual(cap.records, [])

    def test_file_handlers_keep_logging(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fh = logging.FileHandler(os.path.join(tmp, "log.txt"))
            self.logger.addHandler(fh)
            try:
                with TerminalOutputCapture(redirect_fd=False):
                    self.assertIn(fh, self.logger.handlers)
                    self.logger.warning("to file")
                fh.flush()
                with open(os.path.join(tmp, "log.txt"), encoding="utf-8") as f:
                    self.assertIn("to file", f.read())
            finally:
                self.logger.removeHandler(fh)
                fh.close()

    def test_print_and_stderr_writes_are_captured(self) -> None:
        orig_out, orig_err = sys.stdout, sys.stderr
        cap = TerminalOutputCapture(redirect_fd=False)
        with cap:
            print("stray print")
            sys.stderr.write("stray stderr\n")
        self.assertIs(sys.stdout, orig_out)
        self.assertIs(sys.stderr, orig_err)
        self.assertIn("stray print", "".join(cap.text))
        self.assertIn("stray stderr", "".join(cap.text))

    def test_child_process_stderr_is_captured(self) -> None:
        cap = TerminalOutputCapture(redirect_fd=True)
        with cap:
            subprocess.run([sys.executable, "-c", "import sys; sys.stderr.write('ssh: warning\\n')"], check=True)
            self.assertGreater(cap.fd_output_size(), 0)
        self.assertIn("ssh: warning", "".join(cap.text))

    def test_last_resort_is_restored(self) -> None:
        saved = logging.lastResort
        with TerminalOutputCapture(redirect_fd=False):
            self.assertIsNot(logging.lastResort, saved)
        self.assertIs(logging.lastResort, saved)


class TestTUIAppRepaintsAfterLeakedOutput(unittest.TestCase):
    """The event loop forces a full repaint once output was captured."""

    def test_clearok_after_output(self) -> None:
        win = MockCursesWindow(24, 80)
        clearok_calls: list[bool] = []
        win.clearok = lambda flag: clearok_calls.append(flag)  # type: ignore[attr-defined]
        app = TUIApp("/nonexistent/kip-test-config.json")
        app.output_capture = TerminalOutputCapture(redirect_fd=False)

        keys = iter([ord("x"), ord("q")])

        def getch() -> int:
            key = next(keys)
            if key == ord("x"):
                logging.getLogger("core.ssh_tunnel").warning("leaked tunnel message")
            return key

        win.getch = getch  # type: ignore[assignment]
        with patch("curses.curs_set"), patch("core.tui.app.init_theme"):
            app.run(win)
        self.assertIn(True, clearok_calls)
        self.assertTrue(any("leaked tunnel message" in r.getMessage() for r in app.output_capture.records))


if __name__ == "__main__":
    unittest.main()
