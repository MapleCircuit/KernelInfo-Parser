"""core/tui/screens/systemd_services.py - Systemd service management screen for daemon, webapp and API."""

from __future__ import annotations

import contextlib
import curses
import subprocess
from pathlib import Path
from typing import Any, Iterator

from core.config import (
    API_CONFIG_FILENAME,
    DAEMON_CONFIG_FILENAME,
    REPO_ROOT,
    WEBAPP_CONFIG_FILENAME,
    ensure_standalone_config,
    get_service_config_path,
)
from core.systemd_manager import (
    API_UNIT_NAME,
    DAEMON_UNIT_NAME,
    SYSTEMD_UNIT_DIR,
    WEBAPP_UNIT_NAME,
    CommandResult,
    RunnerFn,
    ServiceStatus,
    ServiceUnitConfig,
    control_service,
    default_user_group,
    find_tmux_binary,
    get_attach_command,
    get_service_status,
    install_service,
    preflight_service_install,
    read_service_logs,
    render_unit_file,
    uninstall_service,
)
from core.tui.screens.base import BaseScreen
from core.tui.screens.scoped_config import ScopedConfigMenuScreen
from core.tui.theme import (
    PAIR_ACTIVE,
    PAIR_BORDER,
    PAIR_DEFAULT,
    PAIR_ERROR,
    PAIR_HEADER,
    PAIR_MUTED,
    PAIR_SUCCESS,
    draw_box,
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

SERVICES: tuple[str, str, str] = ("daemon", "webapp", "api")
_UNIT_NAMES: dict[str, str] = {"daemon": DAEMON_UNIT_NAME, "webapp": WEBAPP_UNIT_NAME, "api": API_UNIT_NAME}
_CONFIG_FILENAMES: dict[str, str] = {
    "daemon": DAEMON_CONFIG_FILENAME,
    "webapp": WEBAPP_CONFIG_FILENAME,
    "api": API_CONFIG_FILENAME,
}


def _default_privileged_runner(command: list[str], *, input_text: str | None = None) -> CommandResult:
    """Execute a privileged command via subprocess, capturing stdout/stderr for TUI reporting."""
    proc = subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(
        ok=(proc.returncode == 0),
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        command=list(command),
    )


class ScrollableTextViewer:
    """Centered scrollable modal text viewer for systemd unit previews and journalctl logs."""

    def __init__(self, stdscr: Any, title: str, text: str) -> None:
        self.stdscr = stdscr
        self.title = title
        self.lines: list[str] = (text or "").splitlines() or ["(empty)"]
        self.scroll_offset: int = 0

    def _geometry(self) -> tuple[int, int, int, int]:
        try:
            max_y, max_x = self.stdscr.getmaxyx()
        except Exception:
            max_y, max_x = 24, 80
        box_w = max(36, min(max_x - 4, 110))
        box_h = max(10, min(max_y - 2, max(12, len(self.lines) + 4)))
        start_y = max(0, (max_y - box_h) // 2)
        start_x = max(0, (max_x - box_w) // 2)
        return start_y, start_x, box_h, box_w

    def draw(self) -> None:
        y, x, h, w = self._geometry()
        inner_h = max(1, h - 4)
        inner_w = max(10, w - 4)
        max_scroll = max(0, len(self.lines) - inner_h)
        self.scroll_offset = max(0, min(self.scroll_offset, max_scroll))

        # Clear interior area
        blank = " " * max(1, w - 2)
        for row in range(y + 1, y + h - 1):
            safe_addstr(self.stdscr, row, x + 1, blank, get_color(PAIR_DEFAULT))

        draw_box(self.stdscr, y, x, h, w, title=f" {self.title} ", color_pair=PAIR_HEADER)

        visible = self.lines[self.scroll_offset : self.scroll_offset + inner_h]
        for idx, line in enumerate(visible):
            safe_addstr(self.stdscr, y + 1 + idx, x + 2, line[:inner_w], get_color(PAIR_DEFAULT))

        if max_scroll > 0:
            pct = f" {self.scroll_offset + 1}-{min(len(self.lines), self.scroll_offset + inner_h)}/{len(self.lines)} "
            safe_addstr(self.stdscr, y, max(x + 2, x + w - len(pct) - 2), pct, get_color(PAIR_MUTED))

        footer = " [Up/Down/PgUp/PgDn] Scroll   [Esc/Enter/Q] Close "
        safe_addstr(self.stdscr, y + h - 2, x + 2, footer[:inner_w], get_color(PAIR_MUTED))

    def handle_key(self, ch: int) -> bool:
        """Handle scroll and close keys; return True when the viewer should close."""
        _, _, h, _ = self._geometry()
        inner_h = max(1, h - 4)
        max_scroll = max(0, len(self.lines) - inner_h)

        if ch in (27, 10, 13, curses.KEY_ENTER, ord(" "), ord("q"), ord("Q")):
            return True
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            self.scroll_offset = max(0, self.scroll_offset - 1)
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            self.scroll_offset = min(max_scroll, self.scroll_offset + 1)
        elif ch == curses.KEY_PPAGE:
            self.scroll_offset = max(0, self.scroll_offset - inner_h)
        elif ch == curses.KEY_NPAGE:
            self.scroll_offset = min(max_scroll, self.scroll_offset + inner_h)
        elif ch == curses.KEY_HOME:
            self.scroll_offset = 0
        elif ch == curses.KEY_END:
            self.scroll_offset = max_scroll
        return False

    def show(self) -> None:
        """Run modal viewer loop until closed (exits immediately on non-blocking -1)."""
        try:
            self.stdscr.keypad(True)
        except Exception:
            pass

        while True:
            self.draw()
            try:
                self.stdscr.refresh()
            except Exception:
                pass
            try:
                ch = self.stdscr.getch()
            except Exception:
                return
            if ch == -1:
                return
            if self.handle_key(ch):
                return


class SystemdServicesScreen(BaseScreen):
    """Screen for installing, configuring, and controlling daemon, webapp and API systemd units."""

    def __init__(
        self,
        stdscr: Any,
        app: Any = None,
        *,
        unit_dir: Path = SYSTEMD_UNIT_DIR,
        runner: RunnerFn | None = None,
        privileged_runner: RunnerFn | None = None,
        project_root: Path | None = None,
    ) -> None:
        super().__init__(stdscr, app)
        self.unit_dir = Path(unit_dir)
        self._runner = runner
        self._privileged_runner: RunnerFn = privileged_runner or _default_privileged_runner
        self._explicit_project_root = Path(project_root).resolve() if project_root is not None else None
        self.project_root = self._explicit_project_root or REPO_ROOT

        def_user, def_group = default_user_group()
        self.selected_service: str = "daemon"
        self.statuses: dict[str, ServiceStatus] = {}
        self._drafts: dict[str, dict[str, Any]] = {
            "daemon": {"user": def_user, "group": def_group, "use_tmux": False},
            "webapp": {"user": def_user, "group": def_group, "use_tmux": False},
            "api": {"user": def_user, "group": def_group, "use_tmux": False},
        }

        self.sel_service = SelectList(
            y=9,
            x=4,
            width=18,
            label="Target Service",
            options=list(SERVICES),
            selected_index=0,
            label_width=18,
            on_change=self._on_service_select_changed,
        )
        self.inp_user = TextInput(y=10, x=4, width=26, label="User=", value=def_user, label_width=18)
        self.inp_group = TextInput(y=11, x=4, width=26, label="Group=", value=def_group, label_width=18)
        self.chk_tmux = Checkbox(y=12, x=4, label="tmux Multiplexer Mode", checked=False)

        self.btn_configure = Button(y=15, x=4, label="Configure Config (C)", on_click=self.configure_selected_service)
        self.btn_preview = Button(y=15, x=30, label="Preview Unit (V)", on_click=self.preview_unit)
        self.btn_install = Button(y=15, x=52, label="Install / Update (I)", on_click=self.install_selected_service)

        self.btn_start = Button(y=16, x=4, label="Start (T)", on_click=lambda: self.control_selected_service("start"))
        self.btn_stop = Button(y=16, x=19, label="Stop (O)", on_click=lambda: self.control_selected_service("stop"))
        self.btn_restart = Button(y=16, x=33, label="Restart (X)", on_click=lambda: self.control_selected_service("restart"))
        self.btn_enable = Button(y=16, x=50, label="Enable (E)", on_click=lambda: self.control_selected_service("enable"))
        self.btn_disable = Button(y=16, x=66, label="Disable (D)", on_click=lambda: self.control_selected_service("disable"))

        self.btn_logs = Button(y=17, x=4, label="View Logs (L)", on_click=self.view_logs)
        self.btn_uninstall = Button(y=17, x=23, label="Uninstall (U)", on_click=self.uninstall_selected_service)
        self.btn_refresh = Button(y=17, x=42, label="Refresh (R)", on_click=lambda: self.refresh_status(sync_fields=False))
        self.btn_back = Button(y=17, x=59, label="Back (Q)", on_click=self.back)

        self.status_bar = StatusBar([
            ("Tab/Up/Down", "Navigate"),
            ("Left/Right", "Switch Svc"),
            ("C", "Config"),
            ("I", "Install"),
            ("R/F5", "Refresh"),
            ("Esc/Q", "Back"),
        ])

        self.widgets = [
            self.sel_service,
            self.inp_user,
            self.inp_group,
            self.chk_tmux,
            self.btn_configure,
            self.btn_preview,
            self.btn_install,
            self.btn_start,
            self.btn_stop,
            self.btn_restart,
            self.btn_enable,
            self.btn_disable,
            self.btn_logs,
            self.btn_uninstall,
            self.btn_refresh,
            self.btn_back,
        ]
        self.focus_widget(0)
        self.refresh_status(sync_fields=True)

    # --- Helpers --------------------------------------------------------------------

    def _service_config_path(self, service: str) -> Path:
        if self._explicit_project_root is not None:
            fname = _CONFIG_FILENAMES[service]
            return (self._explicit_project_root / fname).resolve()
        return get_service_config_path(service).resolve()

    def _base_config_path(self) -> Path | None:
        if self.app is not None:
            base = getattr(self.app, "base_config_path", None) or getattr(self.app, "config_path", None)
            if base is not None:
                return Path(base)
        return self.project_root / "config.json"

    @contextlib.contextmanager
    def _suspend_context(self) -> Iterator[None]:
        suspend_fn = getattr(self.app, "suspend", None) if self.app is not None else None
        if callable(suspend_fn):
            with suspend_fn():
                yield
        else:
            yield

    def _save_current_draft(self) -> None:
        self._drafts[self.selected_service] = {
            "user": self.inp_user.get_value().strip(),
            "group": self.inp_group.get_value().strip(),
            "use_tmux": self.chk_tmux.is_checked(),
        }

    def _load_draft_into_widgets(self, service: str) -> None:
        draft = self._drafts[service]
        self.inp_user.set_value(draft["user"])
        self.inp_group.set_value(draft["group"])
        self.chk_tmux.set_checked(bool(draft["use_tmux"]))

    def _on_service_select_changed(self, _idx: int, value: str) -> None:
        if value in SERVICES and value != self.selected_service:
            self._save_current_draft()
            self.selected_service = value
            self._load_draft_into_widgets(value)

    def select_service(self, service: str) -> None:
        """Switch the active service tab (`'daemon'`, `'webapp'` or `'api'`)."""
        svc = str(service).strip().lower()
        if svc not in SERVICES:
            raise ValueError(f"Unknown service {service!r}")
        if svc == self.selected_service:
            return
        self._save_current_draft()
        self.selected_service = svc
        self.sel_service.set_selected(SERVICES.index(svc))
        self._load_draft_into_widgets(svc)

    def _build_unit_config(self, service: str | None = None) -> ServiceUnitConfig:
        svc = service or self.selected_service
        if svc == self.selected_service:
            self._save_current_draft()
        draft = self._drafts[svc]
        return ServiceUnitConfig(
            service=svc,  # type: ignore[arg-type]
            user=draft["user"],
            group=draft["group"],
            use_tmux=bool(draft["use_tmux"]),
            project_root=self.project_root,
            config_path=self._service_config_path(svc),
        )

    def refresh_status(self, *, sync_fields: bool = False) -> dict[str, ServiceStatus]:
        """Query live systemd status for daemon, webapp and API units."""
        def_user, def_group = default_user_group()
        for svc in SERVICES:
            st = get_service_status(svc, unit_dir=self.unit_dir, runner=self._runner)
            self.statuses[svc] = st
            if sync_fields:
                if st.installed:
                    self._drafts[svc] = {
                        "user": st.user or def_user,
                        "group": st.group or def_group,
                        "use_tmux": (st.mode == "tmux"),
                    }
                else:
                    self._drafts[svc] = {
                        "user": def_user,
                        "group": def_group,
                        "use_tmux": False,
                    }
        if sync_fields:
            self._load_draft_into_widgets(self.selected_service)
        return dict(self.statuses)

    def on_enter(self) -> None:
        """Refresh service status whenever returning to this screen."""
        self.refresh_status(sync_fields=False)

    # --- Actions --------------------------------------------------------------------

    def _verify_tmux_available(self) -> bool:
        if find_tmux_binary() is not None:
            return True
        self.chk_tmux.set_checked(False)
        self._save_current_draft()
        ModalDialog(
            self.stdscr,
            "tmux Not Found",
            "tmux executable was not found in PATH.\nInstall tmux before enabling tmux multiplexer mode.",
            ["OK"],
        ).show()
        return False

    def configure_selected_service(self, service: str | None = None) -> bool:
        """Push `ScopedConfigMenuScreen` for the selected (or given) service."""
        svc = service or self.selected_service
        if self.app is None or not hasattr(self.app, "push_screen"):
            return False
        cfg_path = self._service_config_path(svc)
        self.app.push_screen(ScopedConfigMenuScreen(self.stdscr, self.app, svc, config_path=cfg_path))
        return True

    def preview_unit(self) -> str:
        """Render the unit file for the selected service and show it in a scrollable viewer."""
        cfg = self._build_unit_config(self.selected_service)
        unit_text = render_unit_file(cfg)
        unit_name = _UNIT_NAMES[self.selected_service]
        ScrollableTextViewer(self.stdscr, f"Preview: {unit_name}", unit_text).show()
        return unit_text

    def install_selected_service(self) -> bool:
        """Validate config & permissions, confirm warnings if any, and install the unit via sudo."""
        svc = self.selected_service
        self._save_current_draft()
        if self._drafts[svc]["use_tmux"] and not self._verify_tmux_available():
            return False

        cfg_path = self._service_config_path(svc)
        if not cfg_path.exists():
            try:
                ensure_standalone_config(
                    svc,
                    config_path=cfg_path,
                    base_config_path=self._base_config_path(),
                )
            except Exception as exc:
                ModalDialog(
                    self.stdscr,
                    "Config Seed Failed",
                    f"Could not create standalone configuration '{cfg_path}':\n{exc}",
                    ["OK"],
                ).show()
                return False

        cfg = self._build_unit_config(svc)
        ok, errors, warnings = preflight_service_install(cfg)
        if not ok:
            ModalDialog(
                self.stdscr,
                "Preflight Check Failed",
                "Cannot install service:\n\n" + "\n".join(f"- {e}" for e in errors),
                ["OK"],
            ).show()
            return False

        if warnings:
            choice = ModalDialog(
                self.stdscr,
                "Preflight Warnings",
                "Warnings detected:\n\n"
                + "\n".join(f"- {w}" for w in warnings)
                + "\n\nContinue with installation?",
                ["Install Anyway", "Cancel"],
                default_button=1,
            ).show()
            if choice != "Install Anyway":
                return False

        with self._suspend_context():
            inst_ok, msg = install_service(
                cfg,
                unit_dir=self.unit_dir,
                privileged_runner=self._privileged_runner,
            )

        self.refresh_status(sync_fields=inst_ok)
        ModalDialog(
            self.stdscr,
            "Service Installed" if inst_ok else "Install Failed",
            msg,
            ["OK"],
        ).show()
        return inst_ok

    def control_selected_service(self, action: str) -> bool:
        """Run `start`/`stop`/`restart`/`enable`/`disable` via sudo under `app.suspend()`."""
        svc = self.selected_service
        unit_name = _UNIT_NAMES[svc]
        with self._suspend_context():
            res = control_service(svc, action, privileged_runner=self._privileged_runner)

        self.refresh_status(sync_fields=False)
        if not res.ok:
            detail = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
            self.status_bar.set_message(f"{unit_name}: {action} failed", PAIR_ERROR)
            ModalDialog(self.stdscr, f"{action.title()} Failed", f"{unit_name} ({action}):\n{detail}", ["OK"]).show()
            return False

        self.status_bar.set_message(f"{unit_name}: {action} succeeded", PAIR_SUCCESS)
        return True

    def view_logs(self, lines: int = 100) -> str:
        """Fetch recent `journalctl` logs for the selected service and display them."""
        svc = self.selected_service
        unit_name = _UNIT_NAMES[svc]
        res = read_service_logs(svc, lines=lines, runner=self._runner)
        if not res.ok:
            detail = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
            ModalDialog(self.stdscr, "Log Read Failed", f"Failed to read logs for {unit_name}:\n{detail}", ["OK"]).show()
            return ""
        text = res.stdout or "(no log entries)"
        ScrollableTextViewer(self.stdscr, f"journalctl: {unit_name}", text).show()
        return text

    def uninstall_selected_service(self) -> bool:
        """Confirm and uninstall the selected systemd unit via sudo under `app.suspend()`."""
        svc = self.selected_service
        unit_name = _UNIT_NAMES[svc]
        choice = ModalDialog(
            self.stdscr,
            "Confirm Uninstall",
            f"Stop, disable, and remove {unit_name}?\nStandalone config files are preserved.",
            ["Uninstall", "Cancel"],
            default_button=1,
        ).show()
        if choice != "Uninstall":
            return False

        with self._suspend_context():
            ok, msg = uninstall_service(
                svc,
                unit_dir=self.unit_dir,
                privileged_runner=self._privileged_runner,
                runner=self._runner,
            )

        self.refresh_status(sync_fields=ok)
        ModalDialog(
            self.stdscr,
            "Service Uninstalled" if ok else "Uninstall Failed",
            msg,
            ["OK"],
        ).show()
        return ok

    def back(self) -> None:
        """Return to the main menu screen."""
        if self.app is not None and hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
            self.app.pop_screen()

    # --- Rendering & Keys -----------------------------------------------------------

    def _layout_buttons(self, max_x: int) -> None:
        """Arrange action buttons cleanly for narrow or wide terminals."""
        self.btn_configure.label = f"Configure {self.selected_service.title()} (C)"
        row1 = [self.btn_configure, self.btn_preview, self.btn_install]
        row2 = [self.btn_start, self.btn_stop, self.btn_restart, self.btn_enable, self.btn_disable]
        row3 = [self.btn_logs, self.btn_uninstall, self.btn_refresh, self.btn_back]

        for row_y, btns in ((15, row1), (16, row2), (17, row3)):
            curr_x = 4
            for btn in btns:
                btn.y = row_y
                btn.x = curr_x
                curr_x += len(btn.label) + 4 + 2
                if curr_x > max_x - 4:
                    curr_x = max(4, max_x - len(btn.label) - 5)

    def _effective_attach_command(self, svc: str) -> str | None:
        st = self.statuses.get(svc)
        if svc == self.selected_service and self.chk_tmux.is_checked():
            user = self.inp_user.get_value().strip() or (st.user if st else "") or default_user_group()[0]
            return get_attach_command(svc, user)
        if st is not None and st.mode == "tmux":
            if st.attach_command:
                return st.attach_command
            user = st.user or default_user_group()[0]
            return get_attach_command(svc, user)
        return None

    def draw(self) -> None:
        """Render the systemd service management screen."""
        self.clear()
        max_y, max_x = self.get_maxyx()
        if max_y < 20 or max_x < 50:
            self.safe_addstr(0, 0, "Terminal too small for Systemd Services (min 50x20).", get_color(PAIR_ERROR))
            self.refresh()
            return

        self._layout_buttons(max_x)
        box_w = max(46, max_x - 2)

        self.safe_addstr(0, 2, "Systemd Services (Daemon, Webapp & API)", get_color(PAIR_HEADER) | curses.A_BOLD)

        # Status Overview Box (y=1, h=7 -> rows 2..6): one summary line per service, then the selection's config and attach lines
        draw_box(self.stdscr, 1, 1, 7, box_w, title=" Service Status & Attach Commands ", color_pair=PAIR_BORDER)
        inner_w = max(20, box_w - 4)

        row_y = 2
        for svc in SERVICES:
            st = self.statuses.get(svc)
            inst_txt = "Installed" if (st and st.installed) else "Not Installed"
            en_txt = st.enabled if st else "unknown"
            act_txt = st.active if st else "unknown"
            mode_txt = "tmux" if (st and st.mode == "tmux") else "Direct"
            ug_txt = f"{st.user}:{st.group}" if (st and st.user and st.group) else (st.user or "-") if st else "-"
            marker = ">" if svc == self.selected_service else " "

            summary = (
                f"{marker} {svc:<6} | {inst_txt:<13} | En: {en_txt:<8} | "
                f"Act: {act_txt:<8} | Mode: {mode_txt:<6} | {ug_txt}"
            )
            if svc == self.selected_service:
                line_attr = get_color(PAIR_ACTIVE) | curses.A_BOLD
            elif st and st.active == "active":
                line_attr = get_color(PAIR_SUCCESS)
            else:
                line_attr = get_color(PAIR_DEFAULT)
            self.safe_addstr(row_y, 3, summary[:inner_w], line_attr)
            row_y += 1

        sel_cfg_path = self._service_config_path(self.selected_service)
        cfg_state = "exists" if sel_cfg_path.is_file() else "not created yet"
        sel_attach = self._effective_attach_command(self.selected_service)
        sel_status = self.statuses.get(self.selected_service)
        self.safe_addstr(5, 3, f"Config: {sel_cfg_path.name} [{cfg_state}]"[:inner_w], get_color(PAIR_MUTED))
        if sel_attach:
            self.safe_addstr(6, 3, f"Attach: {sel_attach}"[:inner_w], get_color(PAIR_SUCCESS))
        elif sel_status and sel_status.error and not sel_status.installed:
            self.safe_addstr(6, 3, f"Unit not installed ({sel_status.unit_name})"[:inner_w], get_color(PAIR_MUTED))
        elif sel_status and sel_status.error:
            self.safe_addstr(6, 3, f"Status note: {sel_status.error}"[:inner_w], get_color(PAIR_MUTED))

        # Unit Settings Box (y=8, h=6 -> rows 9..12)
        draw_box(self.stdscr, 8, 1, 6, box_w, title=" Unit Settings ", color_pair=PAIR_BORDER)

        # Actions Box (y=14, h=8 -> rows 15..20)
        draw_box(self.stdscr, 14, 1, 8, box_w, title=" Actions ", color_pair=PAIR_BORDER)
        sel_unit_path = self.unit_dir / _UNIT_NAMES[self.selected_service]
        self.safe_addstr(19, 4, f"Config Path: {sel_cfg_path}"[:inner_w], get_color(PAIR_MUTED))
        self.safe_addstr(20, 4, f"Unit Path:   {sel_unit_path}"[:inner_w], get_color(PAIR_MUTED))

        for widget in self.widgets:
            if hasattr(widget, "draw"):
                widget.draw(self.stdscr)

        self.status_bar.y = max_y - 1
        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Handle navigation, tmux validation, action hotkeys, and widget focus."""
        if ch in (9, curses.KEY_DOWN):
            self.focus_next()
            return True
        if ch in (curses.KEY_UP, getattr(curses, "KEY_BTAB", 353), 353):
            self.focus_prev()
            return True
        if ch == 27:
            self.back()
            return True
        if ch == getattr(curses, "KEY_F5", 269):
            self.refresh_status(sync_fields=False)
            return True

        focused = self.get_focused_widget()
        if focused is self.chk_tmux and ch in (ord(" "), 10, 13, curses.KEY_ENTER):
            was_checked = self.chk_tmux.is_checked()
            self.chk_tmux.handle_key(ch)
            if not was_checked and self.chk_tmux.is_checked():
                self._verify_tmux_available()
            else:
                self._save_current_draft()
            return True

        if focused is not None and hasattr(focused, "handle_key"):
            if focused.handle_key(ch):
                if focused in (self.inp_user, self.inp_group, self.chk_tmux):
                    self._save_current_draft()
                return True

        if 0 <= ch < 256:
            key = chr(ch).lower()
            action_map = {
                "c": self.configure_selected_service,
                "v": self.preview_unit,
                "i": self.install_selected_service,
                "t": lambda: self.control_selected_service("start"),
                "o": lambda: self.control_selected_service("stop"),
                "x": lambda: self.control_selected_service("restart"),
                "e": lambda: self.control_selected_service("enable"),
                "d": lambda: self.control_selected_service("disable"),
                "l": self.view_logs,
                "u": self.uninstall_selected_service,
                "r": lambda: self.refresh_status(sync_fields=False),
                "q": self.back,
            }
            if key in action_map:
                action_map[key]()
                return True

        return False
