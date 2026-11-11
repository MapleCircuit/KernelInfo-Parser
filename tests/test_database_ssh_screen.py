"""tests/test_database_ssh_screen.py - Unit tests for DatabaseSSHScreen."""

import curses
import unittest
from unittest.mock import MagicMock, patch

from core.tui.screens.database_ssh import DatabaseSSHScreen
from core.tui.testing import DiagnosticResult
from core.tui.widgets import ModalDialog, TextInput, SelectList, Checkbox, Button
from tests.test_tui import MockCursesWindow


class MockApp:
    """Mock application controller managing config, screens, and dirty state."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config if config is not None else {}
        self.is_dirty = False
        self.popped = False

    def pop_screen(self) -> None:
        self.popped = True


class TestDatabaseSSHScreen(unittest.TestCase):
    """Test suite for DatabaseSSHScreen configuration screen."""

    def setUp(self) -> None:
        self.win = MockCursesWindow(24, 80)
        self.initial_config = {
            "database": {
                "engine": "mariadb",
                "host": "192.168.1.50",
                "port": 3308,
                "user": "parser_user",
                "password": "SecretPassword123",
                "database": "kernel_db",
                "unix_socket": "/run/mysqld/mysqld.sock",
                "timeout": 15,
                "server_tuning": {"innodb_buffer_pool_size": "4G"},
                "ssh_tunnel": {
                    "enabled": True,
                    "host": "10.0.0.1",
                    "port": 2222,
                    "user": "ssh_admin",
                    "key_file": "~/.ssh/id_rsa",
                    "password": "ssh_pass_masked",
                    "remote_host": "127.0.0.1",
                    "remote_port": 3306,
                    "local_port": 3308,
                },
            }
        }
        self.app = MockApp(self.initial_config)

    def test_screen_init_reads_config(self) -> None:
        """Screen properly reads and populates all widgets from app.config."""
        screen = DatabaseSSHScreen(self.win, self.app)

        # Database fields
        self.assertEqual(screen.engine.get_selected(), "mariadb")
        self.assertEqual(screen.host.get_value(), "192.168.1.50")
        self.assertEqual(screen.port.get_value(), "3308")
        self.assertEqual(screen.user.get_value(), "parser_user")
        self.assertEqual(screen.password.get_value(), "SecretPassword123")
        self.assertTrue(screen.password.is_password)
        self.assertEqual(screen.database.get_value(), "kernel_db")
        self.assertEqual(screen.unix_socket.get_value(), "/run/mysqld/mysqld.sock")

        # SSH fields
        self.assertTrue(screen.ssh_enabled.is_checked())
        self.assertEqual(screen.ssh_host.get_value(), "10.0.0.1")
        self.assertEqual(screen.ssh_port.get_value(), "2222")
        self.assertEqual(screen.ssh_user.get_value(), "ssh_admin")
        self.assertEqual(screen.ssh_key_file.get_value(), "~/.ssh/id_rsa")
        self.assertEqual(screen.ssh_password.get_value(), "ssh_pass_masked")
        self.assertTrue(screen.ssh_password.is_password)
        self.assertEqual(screen.ssh_remote_host.get_value(), "127.0.0.1")
        self.assertEqual(screen.ssh_remote_port.get_value(), "3306")
        self.assertEqual(screen.ssh_local_port.get_value(), "3308")

        # Initial focus and dirty state
        self.assertEqual(screen.focused_widget_index, 0)
        self.assertIs(screen.get_focused_widget(), screen.engine)
        self.assertFalse(screen.has_unsaved_changes())

    def test_screen_init_defaults_when_empty_config(self) -> None:
        """Screen provides sensible defaults when app.config is empty."""
        app = MockApp({})
        screen = DatabaseSSHScreen(self.win, app)

        self.assertEqual(screen.engine.get_selected(), "mariadb")
        self.assertEqual(screen.host.get_value(), "127.0.0.1")
        self.assertEqual(screen.port.get_value(), "3306")
        self.assertEqual(screen.user.get_value(), "root")
        self.assertEqual(screen.password.get_value(), "")
        self.assertEqual(screen.database.get_value(), "test")
        self.assertEqual(screen.unix_socket.get_value(), "auto")

        self.assertFalse(screen.ssh_enabled.is_checked())
        self.assertEqual(screen.ssh_host.get_value(), "")
        self.assertEqual(screen.ssh_port.get_value(), "22")
        self.assertEqual(screen.ssh_remote_host.get_value(), "127.0.0.1")
        self.assertEqual(screen.ssh_remote_port.get_value(), "3306")
        self.assertEqual(screen.ssh_local_port.get_value(), "0")

    def test_has_unsaved_changes(self) -> None:
        """Dirty check accurately detects when form values differ from initial values."""
        screen = DatabaseSSHScreen(self.win, self.app)
        self.assertFalse(screen.has_unsaved_changes())

        # Modify a database field
        screen.host.set_value("10.10.10.10")
        self.assertTrue(screen.has_unsaved_changes())

        # Restore value
        screen.host.set_value("192.168.1.50")
        self.assertFalse(screen.has_unsaved_changes())

        # Modify SSH checkbox
        screen.ssh_enabled.set_checked(False)
        self.assertTrue(screen.has_unsaved_changes())
        screen.ssh_enabled.set_checked(True)
        self.assertFalse(screen.has_unsaved_changes())

        # Modify engine select list
        screen.engine.set_selected(1)  # sqlite
        self.assertTrue(screen.has_unsaved_changes())

    def test_navigation_focus_cycling(self) -> None:
        """Tab, Down, Shift-Tab, and Up cycle focus across all widgets."""
        screen = DatabaseSSHScreen(self.win, self.app)
        total_widgets = len(screen.widgets)
        self.assertEqual(screen.focused_widget_index, 0)

        # Tab moves forward
        screen.handle_key(9)
        self.assertEqual(screen.focused_widget_index, 1)

        # Down arrow moves forward
        screen.handle_key(curses.KEY_DOWN)
        self.assertEqual(screen.focused_widget_index, 2)

        # Up arrow moves backward
        screen.handle_key(curses.KEY_UP)
        self.assertEqual(screen.focused_widget_index, 1)

        # Shift-Tab moves backward
        screen.handle_key(curses.KEY_BTAB)
        self.assertEqual(screen.focused_widget_index, 0)

        # Shift-Tab wraps around to last widget (Cancel button)
        screen.handle_key(curses.KEY_BTAB)
        self.assertEqual(screen.focused_widget_index, total_widgets - 1)
        self.assertIs(screen.get_focused_widget(), screen.btn_cancel)

        # Tab wraps around to first widget (Engine select)
        screen.handle_key(9)
        self.assertEqual(screen.focused_widget_index, 0)
        self.assertIs(screen.get_focused_widget(), screen.engine)

    def test_text_input_typing_and_enter_navigation(self) -> None:
        """Typing in a focused TextInput edits text; Enter advances to next widget."""
        screen = DatabaseSSHScreen(self.win, self.app)
        # Focus on host TextInput (index 1)
        screen.focus_widget(1)
        self.assertIs(screen.get_focused_widget(), screen.host)

        # Typing 't' or 's' in a text input appends the character instead of triggering hotkeys
        screen.host.set_value("tes")
        screen.host.cursor_pos = len(screen.host.get_value())
        screen.handle_key(ord("t"))
        self.assertEqual(screen.host.get_value(), "test")

        # Enter advances to the next widget (port)
        screen.handle_key(10)
        self.assertEqual(screen.focused_widget_index, 2)
        self.assertIs(screen.get_focused_widget(), screen.port)

    def test_save_action_via_hotkey_and_button(self) -> None:
        """Save commits changes to app.config, preserves extra keys, sets is_dirty, and pops screen."""
        screen = DatabaseSSHScreen(self.win, self.app)

        # Change some fields
        screen.host.set_value("db.internal.net")
        screen.port.set_value("3307")
        screen.ssh_host.set_value("gateway.internal.net")

        # Focus is on engine (non-TextInput), press 's' hotkey; 3307 != local_port 3308 asks first
        self.assertEqual(screen.focused_widget_index, 0)
        with patch("core.tui.widgets.ModalDialog.show_dialog", return_value="Save Anyway") as dlg:
            res = screen.handle_key(ord("s"))
        self.assertTrue(res)
        self.assertEqual(dlg.call_args.kwargs["title"], "Port Mismatch")

        # Verify app.config["database"] updated
        saved_db = self.app.config["database"]
        self.assertEqual(saved_db["host"], "db.internal.net")
        self.assertEqual(saved_db["port"], 3307)
        self.assertEqual(saved_db["ssh_tunnel"]["host"], "gateway.internal.net")
        # Extra keys preserved
        self.assertEqual(saved_db["timeout"], 15)
        self.assertEqual(saved_db["server_tuning"]["innodb_buffer_pool_size"], "4G")

        self.assertTrue(self.app.is_dirty)
        self.assertTrue(self.app.popped)
        self.assertFalse(screen.has_unsaved_changes())

    def test_port_mismatch_cancel_keeps_screen_unsaved(self) -> None:
        """Declining the port-mismatch confirmation commits nothing and stays on the screen."""
        screen = DatabaseSSHScreen(self.win, self.app)
        screen.port.set_value("3306")
        with patch("core.tui.widgets.ModalDialog.show_dialog", return_value="Cancel") as dlg:
            self.assertFalse(screen.save())
        dlg.assert_called_once()
        self.assertIn("3306", dlg.call_args.kwargs["message"])
        self.assertIn("3308", dlg.call_args.kwargs["message"])
        self.assertEqual(self.app.config["database"]["port"], 3308)
        self.assertFalse(self.app.popped)
        self.assertFalse(self.app.is_dirty)
        self.assertTrue(screen.has_unsaved_changes())

    def test_no_port_confirmation_when_ports_match_or_ssh_disabled(self) -> None:
        """Matching ports, or a disabled tunnel, save without a confirmation dialog."""
        screen = DatabaseSSHScreen(self.win, self.app)
        with patch("core.tui.widgets.ModalDialog.show_dialog") as dlg:
            self.assertTrue(screen.save())
        dlg.assert_not_called()

        screen = DatabaseSSHScreen(self.win, self.app)
        screen.ssh_enabled.set_checked(False)
        screen.port.set_value("3306")
        with patch("core.tui.widgets.ModalDialog.show_dialog") as dlg:
            self.assertTrue(screen.save())
        dlg.assert_not_called()
        self.assertEqual(self.app.config["database"]["port"], 3306)

    def test_port_mismatch_message_for_auto_local_port(self) -> None:
        """Local Port 0 (auto) with SSH enabled is reported as a mismatch."""
        screen = DatabaseSSHScreen(self.win, self.app)
        screen.ssh_local_port.set_value("0")
        msg = screen.port_mismatch_message(screen.get_form_config()["database"])
        self.assertIsNotNone(msg)
        self.assertIn("picked at runtime", msg)

    def test_invalid_port_blocks_save(self) -> None:
        """A non-numeric or empty port reports the field and commits nothing."""
        screen = DatabaseSSHScreen(self.win, self.app)
        screen.port.set_value("33o6")
        screen.ssh_local_port.set_value("")
        with patch("core.tui.widgets.ModalDialog.show_dialog", return_value="OK") as dlg:
            self.assertFalse(screen.save())
        self.assertEqual(dlg.call_args.kwargs["title"], "Invalid Value")
        self.assertIn("Port: '33o6'", dlg.call_args.kwargs["message"])
        self.assertIn("Local Port: ''", dlg.call_args.kwargs["message"])
        self.assertEqual(self.app.config["database"]["port"], 3308)
        self.assertFalse(self.app.popped)

    def test_string_boolean_enabled_reads_false(self) -> None:
        """A text "false" in ssh_tunnel.enabled shows the tunnel as disabled."""
        self.app.config["database"]["ssh_tunnel"]["enabled"] = "false"
        screen = DatabaseSSHScreen(self.win, self.app)
        self.assertFalse(screen.ssh_enabled.is_checked())

    def test_save_via_button_enter(self) -> None:
        """Focusing Save button and pressing Enter executes save."""
        screen = DatabaseSSHScreen(self.win, self.app)
        screen.user.set_value("new_admin")

        save_idx = screen.widgets.index(screen.btn_save)
        screen.focus_widget(save_idx)

        screen.handle_key(curses.KEY_ENTER)
        self.assertTrue(self.app.popped)
        self.assertEqual(self.app.config["database"]["user"], "new_admin")

    def test_save_via_ctrl_s(self) -> None:
        """Ctrl+S (19) triggers save even when focused on a TextInput."""
        screen = DatabaseSSHScreen(self.win, self.app)
        screen.focus_widget(1)  # host TextInput
        screen.host.set_value("cluster-node-1")

        screen.handle_key(19)  # Ctrl+S
        self.assertTrue(self.app.popped)
        self.assertEqual(self.app.config["database"]["host"], "cluster-node-1")

    @patch("core.tui.screens.database_ssh.test_database_connection")
    @patch("core.tui.screens.database_ssh.test_ssh_tunnel")
    @patch("core.tui.widgets.ModalDialog.show_dialog")
    def test_connection_test_with_ssh_enabled(
        self, mock_dialog, mock_ssh_test, mock_db_test
    ) -> None:
        """In-place test action tests SSH tunnel first, then database connection."""
        mock_ssh_test.return_value = DiagnosticResult(
            name="SSH Tunnel (database)",
            passed=True,
            latency_ms=14.2,
            message="SSH host reachable",
            details="Host: 10.0.0.1:2222",
        )
        mock_db_test.return_value = DiagnosticResult(
            name="Database Connection",
            passed=True,
            latency_ms=8.5,
            message="Connected to MariaDB 10.11.6",
            details="Server Version: 10.11.6\nHost: 192.168.1.50:3306",
        )

        screen = DatabaseSSHScreen(self.win, self.app)

        # Trigger via 't' hotkey (focused on engine select)
        self.assertEqual(screen.focused_widget_index, 0)
        res = screen.handle_key(ord("t"))
        self.assertTrue(res)

        # Verified diagnostics were called
        mock_ssh_test.assert_called_once()
        mock_db_test.assert_called_once()
        mock_dialog.assert_called_once()

        dialog_args = mock_dialog.call_args[0]
        title = dialog_args[1]
        msg = dialog_args[2]

        self.assertIn("Passed", title)
        self.assertIn("SSH Tunnel: OK", msg)
        self.assertIn("Connected to MariaDB 10.11.6", msg)

    @patch("core.tui.screens.database_ssh.test_database_connection")
    @patch("core.tui.screens.database_ssh.test_ssh_tunnel")
    @patch("core.tui.widgets.ModalDialog.show_dialog")
    def test_connection_test_skips_db_when_tunnel_fails(
        self, mock_dialog, mock_ssh_test, mock_db_test
    ) -> None:
        """A failed tunnel reports the database probe as skipped instead of retrying the forward."""
        mock_ssh_test.return_value = DiagnosticResult(
            name="SSH Tunnel (database)",
            passed=False,
            latency_ms=30.0,
            message="SSH host 10.0.0.1:2222 reachable but tunnel failed: Permission denied",
        )
        screen = DatabaseSSHScreen(self.win, self.app)
        db_res, ssh_res = screen.test_connection()

        mock_db_test.assert_not_called()
        self.assertFalse(db_res.passed)
        self.assertIn("Skipped", db_res.message)
        self.assertFalse(ssh_res.passed)
        title, msg = mock_dialog.call_args[0][1], mock_dialog.call_args[0][2]
        self.assertIn("Failed", title)
        self.assertIn("SSH Tunnel: FAILED", msg)
        self.assertIn("Database: FAILED - Skipped", msg)

    @patch("core.tui.screens.database_ssh.test_database_connection")
    @patch("core.tui.screens.database_ssh.test_ssh_tunnel")
    @patch("core.tui.widgets.ModalDialog.show_dialog")
    def test_connection_test_ssh_disabled(
        self, mock_dialog, mock_ssh_test, mock_db_test
    ) -> None:
        """When SSH tunnel is disabled, only database connection is tested."""
        screen = DatabaseSSHScreen(self.win, self.app)
        screen.ssh_enabled.set_checked(False)

        mock_db_test.return_value = DiagnosticResult(
            name="Database Connection",
            passed=True,
            latency_ms=4.1,
            message="Connected to MariaDB",
            details="OK",
        )

        # Trigger via Test Connection button
        test_btn_idx = screen.widgets.index(screen.btn_test)
        screen.focus_widget(test_btn_idx)
        screen.handle_key(10)  # Enter on Test button

        mock_ssh_test.assert_not_called()
        mock_db_test.assert_called_once()
        mock_dialog.assert_called_once()

        dialog_msg = mock_dialog.call_args[0][2]
        self.assertNotIn("SSH Tunnel:", dialog_msg)
        self.assertIn("Connected to MariaDB", dialog_msg)

    @patch("core.tui.screens.database_ssh.test_database_connection")
    @patch("core.tui.screens.database_ssh.test_ssh_tunnel")
    @patch("core.tui.widgets.ModalDialog.show_dialog")
    def test_connection_test_failure_formats_dialog(
        self, mock_dialog, mock_ssh_test, mock_db_test
    ) -> None:
        """Failures in diagnostics present formatted error messages in ModalDialog."""
        mock_ssh_test.return_value = DiagnosticResult(
            name="SSH Tunnel (database)",
            passed=False,
            latency_ms=2500.0,
            message="Connection refused to 10.0.0.1:2222",
            details="socket.timeout",
        )
        mock_db_test.return_value = DiagnosticResult(
            name="Database Connection",
            passed=False,
            latency_ms=0.0,
            message="Cannot connect through tunnel",
            details="Access denied",
        )

        screen = DatabaseSSHScreen(self.win, self.app)
        screen.test_connection()

        mock_dialog.assert_called_once()
        dialog_title = mock_dialog.call_args[0][1]
        dialog_msg = mock_dialog.call_args[0][2]

        self.assertIn("Failed", dialog_title)
        self.assertIn("SSH Tunnel: FAILED", dialog_msg)
        self.assertIn("Connection refused", dialog_msg)
        self.assertIn("Database: FAILED", dialog_msg)

    def test_esc_without_changes_pops_screen(self) -> None:
        """Pressing Esc when no changes were made immediately returns without prompt."""
        screen = DatabaseSSHScreen(self.win, self.app)
        self.assertFalse(screen.has_unsaved_changes())

        screen.handle_key(27)  # Esc
        self.assertTrue(self.app.popped)

    @patch("core.tui.widgets.ModalDialog.show_dialog")
    def test_esc_with_changes_prompts_discard(self, mock_dialog) -> None:
        """Pressing Esc with unsaved changes asks user whether to discard changes."""
        screen = DatabaseSSHScreen(self.win, self.app)
        screen.host.set_value("new.db.host")
        self.assertTrue(screen.has_unsaved_changes())

        # User chooses Cancel on prompt -> do not pop screen
        mock_dialog.return_value = "Cancel"
        screen.handle_key(27)
        self.assertFalse(self.app.popped)

        # User chooses Discard -> pops screen
        mock_dialog.return_value = "Discard"
        screen.handle_key(27)
        self.assertTrue(self.app.popped)

    def test_cancel_button_triggers_esc_behavior(self) -> None:
        """Activating Cancel button behaves like Esc key."""
        screen = DatabaseSSHScreen(self.win, self.app)
        cancel_idx = screen.widgets.index(screen.btn_cancel)
        screen.focus_widget(cancel_idx)

        # When no changes made, Enter on cancel pops screen
        screen.handle_key(10)
        self.assertTrue(self.app.popped)

    def test_draw_renders_layout(self) -> None:
        """Screen draw renders title, section headers, widgets, and status bar without error."""
        screen = DatabaseSSHScreen(self.win, self.app)
        screen.draw()

        rendered_texts = [c[1][2] for c in self.win.calls if c[0] == "addstr"]
        self.assertTrue(any("DATABASE & SSH TUNNEL CONFIGURATION" in t for t in rendered_texts))
        self.assertTrue(any("Database Credentials & Connection" in t for t in rendered_texts))
        self.assertTrue(any("SSH Tunnel (Feature \"database\")" in t for t in rendered_texts))
        self.assertTrue(any("Save" in t for t in rendered_texts))
        self.assertTrue(any("Test Connection" in t for t in rendered_texts))
        self.assertTrue(any("Cancel" in t for t in rendered_texts))


if __name__ == "__main__":
    unittest.main()
