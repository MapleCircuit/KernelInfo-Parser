"""core/tui/screens/database_ssh.py - TUI Screen for Database and SSH Tunnel configuration."""

from __future__ import annotations

import copy
import curses
from typing import Any, Optional

from core.config import _coerce_bool
from core.tui.screens.base import BaseScreen
from core.tui.theme import (
    PAIR_ACTIVE,
    PAIR_BORDER,
    PAIR_HEADER,
    PAIR_MUTED,
    get_color,
    safe_addstr,
)
from core.tui.widgets import (
    Button,
    Checkbox,
    ModalDialog,
    SelectList,
    StatusBar,
    TextInput,
)
from core.tui.testing import (
    DiagnosticResult,
    test_database_connection,
    test_ssh_tunnel,
)


class DatabaseSSHScreen(BaseScreen):
    """Screen for configuring Database connectivity and SSH Port Forwarding tunnel.

    Attributes:
        stdscr: Curses screen handle.
        app: Application or controller reference.
        widgets: List of interactive focusable widgets.
    """

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)

        # 1. Retrieve or initialize configuration section
        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            self.db_cfg = self.app.config.setdefault("database", {})
        else:
            self.db_cfg = {}

        if not isinstance(self.db_cfg, dict):
            self.db_cfg = {}

        self.ssh_cfg = self.db_cfg.setdefault("ssh_tunnel", {})
        if not isinstance(self.ssh_cfg, dict):
            self.ssh_cfg = {}

        # Store baseline copies for preserving extra fields (e.g. server_tuning)
        self._orig_db_cfg = copy.deepcopy(self.db_cfg)
        self._orig_ssh_cfg = copy.deepcopy(self.ssh_cfg)

        # 2. Section 1: Database Credentials & Connection
        engine_opts = ["mariadb", "sqlite"]
        curr_engine = str(self.db_cfg.get("engine", "mariadb")).lower()
        sel_engine_idx = 1 if curr_engine == "sqlite" else 0

        self.engine = SelectList(
            y=2,
            x=2,
            width=22,
            label="Engine",
            options=engine_opts,
            selected_index=sel_engine_idx,
            label_width=14,
        )
        self.host = TextInput(
            y=3,
            x=2,
            width=30,
            label="Host",
            value=str(self.db_cfg.get("host", "127.0.0.1")),
            label_width=14,
        )
        self.port = TextInput(
            y=4,
            x=2,
            width=12,
            label="Port",
            value=str(self.db_cfg.get("port", 3306)),
            label_width=14,
        )
        self.user = TextInput(
            y=5,
            x=2,
            width=30,
            label="User",
            value=str(self.db_cfg.get("user", "root")),
            label_width=14,
        )
        self.password = TextInput(
            y=6,
            x=2,
            width=30,
            label="Password",
            value=str(self.db_cfg.get("password", "")),
            is_password=True,
            label_width=14,
        )
        self.database = TextInput(
            y=7,
            x=2,
            width=30,
            label="Database",
            value=str(self.db_cfg.get("database", "test")),
            label_width=14,
        )
        self.unix_socket = TextInput(
            y=8,
            x=2,
            width=30,
            label="Unix Socket",
            value=str(self.db_cfg.get("unix_socket", "auto")),
            label_width=14,
        )

        # 3. Section 2: SSH Tunnel (Feature "database")
        ssh_enabled_val = _coerce_bool(self.ssh_cfg.get("enabled"), False)
        self.ssh_enabled = Checkbox(
            y=10,
            x=2,
            label="Enable SSH Port Forwarding",
            checked=ssh_enabled_val,
        )
        self.ssh_host = TextInput(
            y=11,
            x=2,
            width=30,
            label="SSH Host",
            value=str(self.ssh_cfg.get("host", "")),
            label_width=14,
        )
        self.ssh_port = TextInput(
            y=12,
            x=2,
            width=12,
            label="SSH Port",
            value=str(self.ssh_cfg.get("port", 22)),
            label_width=14,
        )
        self.ssh_user = TextInput(
            y=13,
            x=2,
            width=30,
            label="SSH User",
            value=str(self.ssh_cfg.get("user", "")),
            label_width=14,
        )
        self.ssh_key_file = TextInput(
            y=14,
            x=2,
            width=30,
            label="Key File",
            value=str(self.ssh_cfg.get("key_file", "")),
            label_width=14,
        )
        self.ssh_password = TextInput(
            y=15,
            x=2,
            width=30,
            label="SSH Password",
            value=str(self.ssh_cfg.get("password", "")),
            is_password=True,
            label_width=14,
        )
        self.ssh_remote_host = TextInput(
            y=16,
            x=2,
            width=30,
            label="DB Host @SSH",
            value=str(self.ssh_cfg.get("remote_host", "127.0.0.1")),
            label_width=14,
        )
        self.ssh_remote_port = TextInput(
            y=17,
            x=2,
            width=12,
            label="DB Port @SSH",
            value=str(self.ssh_cfg.get("remote_port", 3306)),
            label_width=14,
        )
        self.ssh_local_port = TextInput(
            y=18,
            x=2,
            width=12,
            label="Local Port",
            value=str(self.ssh_cfg.get("local_port", 0)),
            label_width=14,
        )

        # 4. Action Buttons
        self.btn_save = Button(
            y=20,
            x=4,
            label="Save",
            on_click=self.save,
        )
        self.btn_test = Button(
            y=20,
            x=16,
            label="Test Connection",
            on_click=self.test_connection,
        )
        self.btn_cancel = Button(
            y=20,
            x=39,
            label="Cancel",
            on_click=self.on_esc,
        )

        # 5. Bottom Status Bar
        self.status_bar = StatusBar(
            shortcuts=[
                ("Tab/↓", "Next"),
                ("Shift-Tab/↑", "Prev"),
                ("T", "Test"),
                ("S", "Save"),
                ("Esc", "Back"),
            ]
        )

        # Convenient widget aliases
        self.engine_widget = self.engine
        self.host_widget = self.host
        self.port_widget = self.port
        self.user_widget = self.user
        self.password_widget = self.password
        self.database_widget = self.database
        self.unix_socket_widget = self.unix_socket

        self.ssh_enabled_widget = self.ssh_enabled
        self.ssh_host_widget = self.ssh_host
        self.ssh_port_widget = self.ssh_port
        self.ssh_user_widget = self.ssh_user
        self.ssh_key_file_widget = self.ssh_key_file
        self.ssh_password_widget = self.ssh_password
        self.ssh_remote_host_widget = self.ssh_remote_host
        self.ssh_remote_port_widget = self.ssh_remote_port
        self.ssh_local_port_widget = self.ssh_local_port

        self.save_button = self.btn_save
        self.test_button = self.btn_test
        self.cancel_button = self.btn_cancel

        # Full widget traversal order
        self.widgets = [
            self.engine,
            self.host,
            self.port,
            self.user,
            self.password,
            self.database,
            self.unix_socket,
            self.ssh_enabled,
            self.ssh_host,
            self.ssh_port,
            self.ssh_user,
            self.ssh_key_file,
            self.ssh_password,
            self.ssh_remote_host,
            self.ssh_remote_port,
            self.ssh_local_port,
            self.btn_save,
            self.btn_test,
            self.btn_cancel,
        ]

        self.focus_widget(0)
        self._initial_snapshot = self._get_form_snapshot()
        self.last_test_results: Optional[tuple[DiagnosticResult, Optional[DiagnosticResult]]] = None

    def _get_form_snapshot(self) -> dict[str, Any]:
        """Capture raw values of all form widgets for change detection."""
        return {
            "engine": self.engine.get_selected(),
            "host": self.host.get_value(),
            "port": self.port.get_value(),
            "user": self.user.get_value(),
            "password": self.password.get_value(),
            "database": self.database.get_value(),
            "unix_socket": self.unix_socket.get_value(),
            "ssh_enabled": self.ssh_enabled.is_checked(),
            "ssh_host": self.ssh_host.get_value(),
            "ssh_port": self.ssh_port.get_value(),
            "ssh_user": self.ssh_user.get_value(),
            "ssh_key_file": self.ssh_key_file.get_value(),
            "ssh_password": self.ssh_password.get_value(),
            "ssh_remote_host": self.ssh_remote_host.get_value(),
            "ssh_remote_port": self.ssh_remote_port.get_value(),
            "ssh_local_port": self.ssh_local_port.get_value(),
        }

    def has_unsaved_changes(self) -> bool:
        """Return True if form inputs differ from initial snapshot."""
        return self._get_form_snapshot() != self._initial_snapshot

    def get_form_config(self) -> dict[str, Any]:
        """Parse current widget values into merged database configuration dictionary."""
        try:
            port = int(self.port.get_value().strip() or "3306")
        except (ValueError, TypeError):
            port = 3306

        try:
            ssh_port = int(self.ssh_port.get_value().strip() or "22")
        except (ValueError, TypeError):
            ssh_port = 22

        try:
            remote_port = int(self.ssh_remote_port.get_value().strip() or "3306")
        except (ValueError, TypeError):
            remote_port = 3306

        try:
            local_port = int(self.ssh_local_port.get_value().strip() or "0")
        except (ValueError, TypeError):
            local_port = 0

        ssh_cfg = dict(self._orig_ssh_cfg)
        ssh_cfg.update({
            "enabled": self.ssh_enabled.is_checked(),
            "host": self.ssh_host.get_value().strip(),
            "port": ssh_port,
            "user": self.ssh_user.get_value().strip(),
            "key_file": self.ssh_key_file.get_value().strip(),
            "password": self.ssh_password.get_value(),
            "remote_host": self.ssh_remote_host.get_value().strip() or "127.0.0.1",
            "remote_port": remote_port,
            "local_port": local_port,
        })

        db_cfg = dict(self._orig_db_cfg)
        db_cfg.update({
            "engine": self.engine.get_selected(),
            "host": self.host.get_value().strip(),
            "port": port,
            "user": self.user.get_value().strip(),
            "password": self.password.get_value(),
            "database": self.database.get_value().strip(),
            "unix_socket": self.unix_socket.get_value().strip(),
            "ssh_tunnel": ssh_cfg,
        })

        return {"database": db_cfg}

    def port_mismatch_message(self, db_cfg: dict[str, Any]) -> Optional[str]:
        """Describe an enabled SSH tunnel whose Local Port differs from the database Port, else None."""
        ssh_cfg = db_cfg.get("ssh_tunnel") or {}
        if not ssh_cfg.get("enabled", False):
            return None
        db_port, local_port = db_cfg.get("port"), ssh_cfg.get("local_port")
        if db_port == local_port:
            return None
        local_desc = f"{local_port}" if local_port else "0 (a free port is picked at runtime)"
        return (
            f"SSH tunnel is enabled but the database Port ({db_port})\n"
            f"differs from the SSH Local Port ({local_desc}).\n\n"
            "The tunnel listens on 127.0.0.1:<Local Port>; for the\n"
            "configuration to work out of the box both ports should\n"
            "be the same.\n\n"
            "Save this configuration anyway?"
        )

    def invalid_port_fields(self) -> list[str]:
        """Describe each port input that is not a whole number in range (Local Port allows 0 = auto)."""
        errors: list[str] = []
        fields = (
            ("Port", self.port, 1),
            ("SSH Port", self.ssh_port, 1),
            ("DB Port @SSH", self.ssh_remote_port, 1),
            ("Local Port", self.ssh_local_port, 0),
        )
        for label, widget, low in fields:
            raw = widget.get_value().strip()
            try:
                num = int(raw)
            except (ValueError, TypeError):
                num = -1
            if not low <= num <= 65535:
                errors.append(f"{label}: '{raw}' is not a port number ({low}-65535)")
        return errors

    def save(self) -> bool:
        """Commit changes to app.config["database"], mark app dirty, and pop screen.

        A port input that is not a number in range shows an "Invalid Value" dialog and saves
        nothing. With the SSH tunnel enabled and the database Port different from the SSH Local
        Port, a confirmation dialog runs first; declining it keeps the screen open unsaved.

        Returns:
            bool: True when the configuration was committed.
        """
        errors = self.invalid_port_fields()
        if errors:
            ModalDialog.show_dialog(
                self.stdscr,
                title="Invalid Value",
                message="Settings were not saved:\n" + "\n".join(errors),
                buttons=["OK"],
            )
            return False

        cfg = self.get_form_config()
        db_cfg = cfg["database"]

        mismatch = self.port_mismatch_message(db_cfg)
        if mismatch is not None:
            choice = ModalDialog.show_dialog(
                self.stdscr,
                title="Port Mismatch",
                message=mismatch,
                buttons=["Save Anyway", "Cancel"],
                default_button=1,
            )
            if choice != "Save Anyway":
                return False

        if self.app is not None:
            if hasattr(self.app, "config") and isinstance(self.app.config, dict):
                orig = self.app.config.setdefault("database", {})
                if isinstance(orig, dict):
                    orig.update(db_cfg)
                    self.app.config["database"] = orig
                else:
                    self.app.config["database"] = db_cfg
            self.app.is_dirty = True
            if hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
                self.app.pop_screen()

        self._initial_snapshot = self._get_form_snapshot()
        return True

    def test_connection(self) -> tuple[DiagnosticResult, Optional[DiagnosticResult]]:
        """Run connectivity diagnostics for database and optional SSH tunnel.

        Returns:
            Tuple of (database_result, ssh_result).
        """
        form_data = self.get_form_config()
        db_cfg = form_data["database"]
        ssh_cfg = db_cfg.get("ssh_tunnel", {})

        ssh_result: Optional[DiagnosticResult] = None
        if ssh_cfg.get("enabled", False):
            self.show_busy("Testing SSH tunnel (opening port forward)...")
            ssh_result = test_ssh_tunnel(ssh_cfg, feature_name="database")

        if ssh_result is not None and not ssh_result.passed:
            db_result = DiagnosticResult(
                name="Database Connection",
                passed=False,
                latency_ms=0.0,
                message="Skipped: the SSH tunnel could not be established",
            )
        else:
            self.show_busy("Testing database connection...")
            db_result = test_database_connection(db_cfg, ssh_tunnel_cfg=ssh_cfg)
        self.last_test_results = (db_result, ssh_result)

        # Format diagnostic outcome into modal message
        lines: list[str] = []
        if ssh_result is not None:
            if ssh_result.passed:
                lines.append(f"SSH Tunnel: OK ({ssh_result.latency_ms:.1f}ms) - {ssh_result.message}")
            else:
                lines.append(f"SSH Tunnel: FAILED - {ssh_result.message}")
            if ssh_result.details:
                for d_line in ssh_result.details.strip().splitlines()[:4]:
                    lines.append(f"  {d_line}")
            lines.append("")

        if db_result.passed:
            lines.append(f"Database: OK ({db_result.latency_ms:.1f}ms) - {db_result.message}")
        else:
            lines.append(f"Database: FAILED - {db_result.message}")
        if db_result.details:
            for d_line in db_result.details.strip().splitlines()[:6]:
                lines.append(f"  {d_line}")

        all_passed = db_result.passed and (ssh_result is None or ssh_result.passed)
        dialog_title = "Connection Test Passed" if all_passed else "Connection Test Failed"
        dialog_msg = "\n".join(lines).strip()

        ModalDialog.show_dialog(self.stdscr, dialog_title, dialog_msg, ["OK"])
        return (db_result, ssh_result)

    def on_esc(self) -> bool:
        """Handle Esc key: prompt if changes were made or return to previous screen."""
        if self.has_unsaved_changes():
            choice = ModalDialog.show_dialog(
                self.stdscr,
                title="Unsaved Changes",
                message="You have unsaved changes. Discard changes and return?",
                buttons=["Discard", "Cancel"],
                default_button=1,
            )
            if choice == "Discard":
                if self.app is not None and hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
                    self.app.pop_screen()
                return True
            return True
        else:
            if self.app is not None and hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
                self.app.pop_screen()
            return True

    def handle_key(self, ch: int) -> bool:
        """Handle navigation, shortcuts, and widget keys."""
        # 1. Tab / Down -> focus next widget
        if ch in (9, ord("\t"), curses.KEY_DOWN):
            self.focus_next()
            return True

        # 2. Shift-Tab / Up -> focus previous widget
        if ch in (curses.KEY_UP, getattr(curses, "KEY_BTAB", 353), 353):
            self.focus_prev()
            return True

        # 3. Esc -> exit / prompt
        if ch == 27:
            return self.on_esc()

        # 4. Global control shortcuts
        if ch == 20:  # Ctrl+T
            self.test_connection()
            return True
        if ch == 19:  # Ctrl+S
            self.save()
            return True

        focused = self.get_focused_widget()

        # 5. Focused TextInput gets priority for typing and navigation
        if isinstance(focused, TextInput):
            if focused.handle_key(ch):
                return True
            # Enter on TextInput advances to next input
            if ch in (10, 13, curses.KEY_ENTER):
                self.focus_next()
                return True
            return False

        # 6. Non-TextInput hotkeys
        if ch in (ord("t"), ord("T")):
            self.test_connection()
            return True
        if ch in (ord("s"), ord("S")):
            self.save()
            return True

        # 7. Pass remaining keys to focused widget (Button, Checkbox, SelectList)
        if focused is not None and hasattr(focused, "handle_key"):
            if focused.handle_key(ch):
                return True

        return False

    def draw(self) -> None:
        """Render the complete screen layout, headers, widgets, and status bar."""
        max_y, max_x = self.get_maxyx()
        self.clear()

        # Title Banner
        title = " DATABASE & SSH TUNNEL CONFIGURATION "
        title_x = max(0, (max_x - len(title)) // 2)
        safe_addstr(self.stdscr, 0, title_x, title, get_color(PAIR_HEADER) | curses.A_BOLD)

        # Section 1 Header
        sec1_hdr = "── Database Credentials & Connection "
        sec1_hdr_full = sec1_hdr + "─" * max(0, max_x - len(sec1_hdr) - 4)
        safe_addstr(self.stdscr, 1, 2, sec1_hdr_full[: max_x - 4], get_color(PAIR_BORDER) | curses.A_BOLD)

        # Section 2 Header
        sec2_hdr = "── SSH Tunnel (Feature \"database\") "
        sec2_hdr_full = sec2_hdr + "─" * max(0, max_x - len(sec2_hdr) - 4)
        safe_addstr(self.stdscr, 9, 2, sec2_hdr_full[: max_x - 4], get_color(PAIR_BORDER) | curses.A_BOLD)

        # Draw all interactive widgets
        for widget in self.widgets:
            if hasattr(widget, "y") and widget.y < max_y - 1:
                widget.draw(self.stdscr)

        # The forward destination is resolved by the SSH server, not by this machine
        hints = (
            (self.ssh_remote_host, "where the SSH server reaches the DB (127.0.0.1 = DB on SSH host)"),
            (self.ssh_local_port, "127.0.0.1 port on this machine; keep equal to the DB Port above"),
        )
        for widget, text in hints:
            hint_x = widget.x + len(widget._get_label_str()) + widget.width + 1
            if widget.y < max_y - 1 and max_x - hint_x > 20:
                safe_addstr(self.stdscr, widget.y, hint_x, text[: max_x - hint_x - 1], get_color(PAIR_MUTED))

        # Bottom Status Bar
        self.status_bar.y = max_y - 1
        self.status_bar.draw(self.stdscr)
