"""core/tui/screens/cve_ml.py - CVE catalog and Mailing List (Lore) configuration screen."""

from __future__ import annotations

import curses
from typing import Any, Optional

from core.config import _coerce_bool
from core.tui.screens.base import BaseScreen
from core.tui.testing import (
    DiagnosticResult,
    test_cve_catalog,
    test_mailing_list as test_ml_service,
)
from core.tui.theme import (
    PAIR_BORDER,
    PAIR_HEADER,
    get_color,
    safe_addstr,
    draw_box,
)
from core.tui.widgets import (
    Button,
    Checkbox,
    ModalDialog,
    StatusBar,
    TextInput,
)


class CVEMailingListScreen(BaseScreen):
    """Configuration and diagnostic test screen for CVE list and Lore mailing list.

    Attributes:
        app: Application controller reference.
    """

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)

        # Retrieve or initialize config sections
        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            self.cve_cfg = self.app.config.setdefault("cve", {})
            self.ml_cfg = self.app.config.setdefault("mailing_list", {})
        else:
            self.cve_cfg = {}
            self.ml_cfg = {}

        if not isinstance(self.cve_cfg, dict):
            self.cve_cfg = {}
        if not isinstance(self.ml_cfg, dict):
            self.ml_cfg = {}

        self.ssh_cfg = self.ml_cfg.setdefault("ssh_tunnel", {})
        if not isinstance(self.ssh_cfg, dict):
            self.ssh_cfg = {}

        # Initial values
        cur_cve_dir = str(self.cve_cfg.get("repo_dir", "cvelistV5"))
        cur_ml_dir = str(self.ml_cfg.get("storage_dir", "/srv/lore"))
        cur_ml_port = str(self.ml_cfg.get("port", 8192))
        cur_auto_daemon = _coerce_bool(self.ml_cfg.get("auto_start_daemon"), True)

        cur_ssh_enabled = _coerce_bool(self.ssh_cfg.get("enabled"), False)
        cur_ssh_host = str(self.ssh_cfg.get("host", ""))
        cur_ssh_port = str(self.ssh_cfg.get("port", 22))
        cur_ssh_user = str(self.ssh_cfg.get("user", ""))
        cur_ssh_key = str(self.ssh_cfg.get("key_file", ""))

        # CVE widgets
        self.inp_cve_repo_dir = TextInput(
            y=4, x=4, width=36, label="CVE Catalog Repo", value=cur_cve_dir, label_width=20
        )
        self.btn_test_cve = Button(y=5, x=4, label="Test CVE Catalog [C]", on_click=self.test_cve)

        # Mailing list widgets
        self.inp_ml_storage_dir = TextInput(
            y=8, x=4, width=36, label="Lore Storage Dir", value=cur_ml_dir, label_width=20
        )
        self.inp_ml_port = TextInput(
            y=9, x=4, width=12, label="Daemon Port", value=cur_ml_port, label_width=20
        )
        self.chk_ml_auto_start = Checkbox(
            y=10, x=4, label="Auto start daemon", checked=cur_auto_daemon
        )

        # SSH Tunnel for ML widgets
        self.chk_ssh_enabled = Checkbox(
            y=11, x=4, label="Enable SSH Tunnel for Mailing List", checked=cur_ssh_enabled
        )
        self.inp_ssh_host = TextInput(
            y=12, x=4, width=28, label="SSH Host", value=cur_ssh_host, label_width=20
        )
        self.inp_ssh_port = TextInput(
            y=13, x=4, width=12, label="SSH Port", value=cur_ssh_port, label_width=20
        )
        self.inp_ssh_user = TextInput(
            y=14, x=4, width=24, label="SSH User", value=cur_ssh_user, label_width=20
        )
        self.inp_ssh_key_file = TextInput(
            y=15, x=4, width=36, label="SSH Key File", value=cur_ssh_key, label_width=20
        )
        self.btn_test_ml = Button(y=16, x=4, label="Test Mailing List [M]", on_click=self.test_mailing_list)

        # Actions
        self.btn_save = Button(y=18, x=4, label="Save Settings", on_click=self.save)
        self.btn_cancel = Button(y=18, x=24, label="Cancel", on_click=self.cancel)

        # Status Bar
        self.status_bar = StatusBar(
            shortcuts=[
                ("Tab/Down", "Next"),
                ("Shift-Tab/Up", "Prev"),
                ("C/T", "Test CVE"),
                ("M", "Test ML"),
                ("S", "Save"),
                ("Esc", "Back"),
            ]
        )

        self.widgets = [
            self.inp_cve_repo_dir,
            self.btn_test_cve,
            self.inp_ml_storage_dir,
            self.inp_ml_port,
            self.chk_ml_auto_start,
            self.chk_ssh_enabled,
            self.inp_ssh_host,
            self.inp_ssh_port,
            self.inp_ssh_user,
            self.inp_ssh_key_file,
            self.btn_test_ml,
            self.btn_save,
            self.btn_cancel,
        ]
        self.focus_widget(0)
        self.mark_form_clean()

    def draw(self) -> None:
        """Render CVE and mailing list configuration screen."""
        self.clear()
        max_y, max_x = self.get_maxyx()

        title = "CVE Catalog & Mailing List Configuration"
        safe_addstr(self.stdscr, 1, 2, title, get_color(PAIR_HEADER) | curses.A_BOLD)

        box_w = max(40, min(76, max_x - 4))

        # Section 1: CVE Catalog
        draw_box(self.stdscr, 3, 2, 4, box_w, title="CVE Catalog Settings", color_pair=PAIR_BORDER)

        # Section 2: Mailing List
        draw_box(self.stdscr, 7, 2, 11, box_w, title="Mailing List (Lore) & SSH Tunnel", color_pair=PAIR_BORDER)

        for widget in self.widgets:
            if hasattr(widget, "draw"):
                widget.draw(self.stdscr)

        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Handle screen navigation, test hotkeys, and widget interactions."""
        # Navigation
        if ch in (9, curses.KEY_DOWN):
            self.focus_next()
            return True

        if ch in (curses.KEY_UP, getattr(curses, "KEY_BTAB", 353), 353):
            self.focus_prev()
            return True

        if ch == 27:
            self.cancel()
            return True

        if ch == 19:  # Ctrl+S
            self.save()
            return True

        focused = self.get_focused_widget()
        if focused is not None and hasattr(focused, "handle_key"):
            if focused.handle_key(ch):
                return True

        # Hotkeys when not actively consumed by focused text input
        if ch in (ord("s"), ord("S")):
            self.save()
            return True

        if ch in (ord("c"), ord("C")):
            self.test_cve()
            return True

        if ch in (ord("t"), ord("T")):
            # If focused on mailing list section, test ML; otherwise test CVE
            ml_widgets = (
                self.inp_ml_storage_dir,
                self.inp_ml_port,
                self.chk_ml_auto_start,
                self.chk_ssh_enabled,
                self.inp_ssh_host,
                self.inp_ssh_port,
                self.inp_ssh_user,
                self.inp_ssh_key_file,
                self.btn_test_ml,
            )
            if focused in ml_widgets:
                self.test_mailing_list()
            else:
                self.test_cve()
            return True

        if ch in (ord("m"), ord("M")):
            self.test_mailing_list()
            return True

        if ch in (10, 13, curses.KEY_ENTER):
            if focused is self.btn_save:
                self.save()
                return True
            elif focused is self.btn_cancel:
                self.cancel()
                return True
            elif focused is self.btn_test_cve:
                self.test_cve()
                return True
            elif focused is self.btn_test_ml:
                self.test_mailing_list()
                return True

        return False

    def test_cve(self) -> DiagnosticResult:
        """Run CVE catalog diagnostic test and display result dialog."""
        cve_cfg = {"repo_dir": self.inp_cve_repo_dir.get_value().strip()}
        res = test_cve_catalog(cve_cfg)
        self.show_result_modal("CVE Catalog Test", res)
        return res

    def test_mailing_list(self) -> DiagnosticResult:
        """Run Mailing List diagnostic test and display result dialog."""
        try:
            port_num = int(self.inp_ml_port.get_value().strip() or "8192")
        except ValueError:
            port_num = 8192

        try:
            ssh_port_num = int(self.inp_ssh_port.get_value().strip() or "22")
        except ValueError:
            ssh_port_num = 22

        # Config-only tunnel fields (password, remote_host/remote_port, local_port, ...) carry
        # over from the stored section so the test opens the same forward the pipeline would.
        ssh_cfg = dict(self.ssh_cfg)
        ssh_cfg.update({
            "enabled": self.chk_ssh_enabled.is_checked(),
            "host": self.inp_ssh_host.get_value().strip(),
            "port": ssh_port_num,
            "user": self.inp_ssh_user.get_value().strip(),
            "key_file": self.inp_ssh_key_file.get_value().strip(),
        })
        ml_cfg = dict(self.ml_cfg)
        ml_cfg.update({
            "storage_dir": self.inp_ml_storage_dir.get_value().strip(),
            "port": port_num,
            "auto_start_daemon": self.chk_ml_auto_start.is_checked(),
            "ssh_tunnel": ssh_cfg,
        })
        if ssh_cfg["enabled"]:
            self.show_busy("Testing mailing list through SSH tunnel (opening port forward)...")
        res = test_ml_service(ml_cfg)
        self.show_result_modal("Mailing List Test", res)
        return res

    def show_result_modal(self, title: str, res: DiagnosticResult) -> Optional[str]:
        """Display outcome of a diagnostic test in a ModalDialog."""
        tag = "PASS" if res.passed else "FAIL"
        modal_title = f"{title} [{tag}]"
        lines = [res.message]
        if res.details:
            lines.append("")
            lines.append("Details:")
            lines.append(res.details)
        msg = "\n".join(lines)
        dialog = ModalDialog(self.stdscr, title=modal_title, message=msg, buttons=["OK"])
        return dialog.show()

    def _parse_port_fields(self) -> tuple[dict[str, int], list[str]]:
        """Parse the port inputs; return ({"ml_port"/"ssh_port": value}, [labels of invalid inputs])."""
        values: dict[str, int] = {}
        errors: list[str] = []
        for key, widget in (("ml_port", self.inp_ml_port), ("ssh_port", self.inp_ssh_port)):
            raw = widget.get_value().strip()
            try:
                num = int(raw)
            except (ValueError, TypeError):
                num = 0
            if not 1 <= num <= 65535:
                errors.append(f"{widget.label}: '{raw}' is not a port number (1-65535)")
            else:
                values[key] = num
        return values, errors

    def save(self) -> bool:
        """Persist current widget values to app.config, mark dirty, and pop screen.

        An invalid port input leaves the configuration untouched, reports the field in a dialog,
        and keeps the screen open. Returns True when the values were saved.
        """
        ports, errors = self._parse_port_fields()
        if errors:
            ModalDialog(
                self.stdscr,
                title="Invalid Value",
                message="Settings were not saved:\n" + "\n".join(errors),
                buttons=["OK"],
            ).show()
            return False

        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            cve_cfg = self.app.config.setdefault("cve", {})
            ml_cfg = self.app.config.setdefault("mailing_list", {})
        else:
            cve_cfg = self.cve_cfg
            ml_cfg = self.ml_cfg

        cve_cfg["repo_dir"] = self.inp_cve_repo_dir.get_value().strip()

        ml_cfg["storage_dir"] = self.inp_ml_storage_dir.get_value().strip()
        ml_cfg["port"] = ports["ml_port"]
        ml_cfg["auto_start_daemon"] = self.chk_ml_auto_start.is_checked()

        ssh_cfg = ml_cfg.setdefault("ssh_tunnel", {})
        ssh_cfg["enabled"] = self.chk_ssh_enabled.is_checked()
        ssh_cfg["host"] = self.inp_ssh_host.get_value().strip()
        ssh_cfg["port"] = ports["ssh_port"]
        ssh_cfg["user"] = self.inp_ssh_user.get_value().strip()
        ssh_cfg["key_file"] = self.inp_ssh_key_file.get_value().strip()

        self.cve_cfg = cve_cfg
        self.ml_cfg = ml_cfg
        self.ssh_cfg = ssh_cfg

        if self.app is not None:
            if hasattr(self.app, "is_dirty"):
                self.app.is_dirty = True
            if hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
                self.app.pop_screen()
        self.mark_form_clean()
        return True

    def cancel(self) -> None:
        """Dismiss screen without persisting changes (confirming first when edits are unsaved)."""
        if not self.confirm_discard():
            return
        if self.app is not None and hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
            self.app.pop_screen()
