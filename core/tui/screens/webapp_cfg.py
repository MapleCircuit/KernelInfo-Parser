"""core/tui/screens/webapp_cfg.py - Webapp server, cache, tools, and source index configuration screen."""

from __future__ import annotations

import curses
from typing import Any

from cache_engine import (
    DEFAULT_ENGINE,
    UnknownCacheEngine,
    available_cache_engines,
    get_cache_engine,
)
from core.config import _coerce_bool
from core.tui.screens.base import BaseScreen
from core.tui.screens.parser_dist import wrap_help
from core.tui.theme import (
    PAIR_BORDER,
    PAIR_DEFAULT,
    PAIR_HEADER,
    PAIR_MUTED,
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

HELP_TOPICS: dict[str, tuple[str, list[str]]] = {
    "host": (
        "Webapp Bind Host",
        [
            "Network interface address uvicorn binds to (e.g. 0.0.0.0 for all interfaces, 127.0.0.1 for local-only).",
        ],
    ),
    "port": (
        "Webapp Port",
        [
            "TCP port (1-65535) the FastAPI/uvicorn webapp listens on. Defaults to 8000.",
        ],
    ),
    "reload": (
        "Auto-Reload",
        [
            "Enable uvicorn auto-reload on source code changes (for local development only).",
            "Disable this when running the webapp as a systemd service.",
        ],
    ),
    "cache_engine": (
        "Cache Engine",
        [
            "Backend for local rebuildable caches (git-derived indexes). Env KERNELINFO_CACHE_ENGINE overrides it.",
            "- ram: pure-Python in-memory cache; fastest, cleared on process exit.",
            "- sqlite: persistent SQLite files under Cache Directory; survives restarts.",
        ],
    ),
    "cache_dir": (
        "Cache Directory",
        [
            "Base directory for sqlite cache stores and scope key for ram.",
            "Leave empty to use KERNELINFO_CACHE_DIR or ~/.cache/kernelinfo.",
        ],
    ),
    "cache_max_mb": (
        "Webapp Disk Cache Cap (MB)",
        [
            "Maximum disk size in MB for derived webapp caches under Cache Directory (0 = unlimited).",
        ],
    ),
    "jobs_workers": (
        "Background Job Workers",
        [
            "Number of worker threads per process executing asynchronous webapp jobs (>= 1).",
        ],
    ),
    "prewarm_indexes": (
        "Prewarm Indexes at Startup",
        [
            "Build commit relation and Kbuild object indexes eagerly when the webapp starts.",
        ],
    ),
    "checkpatch": (
        "checkpatch.pl Tool",
        [
            "Allow the webapp to run scripts/checkpatch.pl from the selected release ref (never caller revisions).",
            "- Checkpatch Ref: tag or 'latest-release' providing checkpatch.pl.",
            "- Tools Sandbox: optional wrapper command (e.g. bwrap) for tool execution.",
        ],
    ),
    "source_index": (
        "Source Index Workers & Limits",
        [
            "Low-priority background extractor for unparsed git revisions.",
            "- Workers: number of extractor processes (>= 1).",
            "- Max MB: SQLite store cap in MB (0 = unlimited).",
            "- Revisions: maximum assembled revisions kept on disk (0 = none).",
        ],
    ),
    "overview": (
        "Help",
        [
            "Move to a field to see its help here (F1 opens it in a dialog).",
            "- Webapp Server: bind host, port, and dev auto-reload.",
            "- Cache & Jobs: cache_engine backend, disk limits, worker threads, startup prewarm.",
            "- Tools & Source Index: checkpatch.pl sandbox and on-demand source index limits.",
        ],
    ),
}

_FORM_MIN_W = 52
_HELP_MIN_W = 30


def _select_primary_repo(repos: list[Any]) -> dict[str, Any] | None:
    """Return the primary repository dict entry (`default: True`, else first enabled, else first dict)."""
    dicts = [r for r in repos if isinstance(r, dict)]
    if not dicts:
        return None
    for entry in dicts:
        if _coerce_bool(entry.get("default"), False):
            return entry
    for entry in dicts:
        if _coerce_bool(entry.get("enabled"), True):
            return entry
    return dicts[0]


def sync_webapp_repositories(app_config: dict[str, Any] | None, webapp_cfg: dict[str, Any]) -> None:
    """Synchronize scalar webapp settings with `webapp.repositories` and canonical top-level sections.

    Ensures `default_version` and `pool_size` carry valid defaults, and keeps any legacy scalar
    fields (`webapp.repo_dir`, `webapp.lore_archive_db`, `webapp.cve_dir`, `webapp.default_version`)
    synchronized with the primary entry in `webapp.repositories` and top-level `repositories`,
    `cve.repo_dir`, and `mailing_list.storage_dir` so TUI saves never leave conflicting entries.
    """
    default_ver = str(webapp_cfg.get("default_version") or "v3.0").strip() or "v3.0"
    webapp_cfg["default_version"] = default_ver
    try:
        pool_size = max(1, int(webapp_cfg.get("pool_size") or 10))
    except (TypeError, ValueError):
        pool_size = 10
    webapp_cfg["pool_size"] = pool_size

    wa_repos = webapp_cfg.get("repositories")
    wa_primary = _select_primary_repo(wa_repos) if isinstance(wa_repos, list) else None
    top_repos = app_config.get("repositories") if isinstance(app_config, dict) else None
    top_primary = _select_primary_repo(top_repos) if isinstance(top_repos, list) else None

    repo_dir_raw = webapp_cfg.get("repo_dir")
    repo_dir = str(repo_dir_raw).strip() if isinstance(repo_dir_raw, str) else ""
    if repo_dir:
        if wa_primary is not None:
            wa_primary["path"] = repo_dir
            if "repo_dir" in wa_primary:
                wa_primary["repo_dir"] = repo_dir
        if top_primary is not None:
            top_primary["path"] = repo_dir
    elif wa_primary is not None:
        if top_primary is not None and isinstance(top_primary.get("path"), str) and top_primary["path"].strip():
            wa_primary["path"] = top_primary["path"].strip()
            if "repo_dir" in wa_primary:
                wa_primary["repo_dir"] = top_primary["path"].strip()
        if "repo_dir" in webapp_cfg and isinstance(wa_primary.get("path"), str) and wa_primary["path"].strip():
            webapp_cfg["repo_dir"] = wa_primary["path"].strip()

    lore_raw = webapp_cfg.get("lore_archive_db")
    lore_db = str(lore_raw).strip() if isinstance(lore_raw, str) else ""
    if lore_db:
        if wa_primary is not None:
            wa_primary["lore_archive_db"] = lore_db
        if isinstance(app_config, dict):
            ml_sec = app_config.setdefault("mailing_list", {})
            if isinstance(ml_sec, dict):
                ml_sec["storage_dir"] = lore_db
    elif wa_primary is not None:
        ml_dir = (
            str(app_config["mailing_list"].get("storage_dir") or "").strip()
            if isinstance(app_config, dict) and isinstance(app_config.get("mailing_list"), dict)
            else ""
        )
        if ml_dir and "lore_archive_db" in wa_primary:
            wa_primary["lore_archive_db"] = ml_dir
        elif (
            "lore_archive_db" in webapp_cfg
            and isinstance(wa_primary.get("lore_archive_db"), str)
            and wa_primary["lore_archive_db"].strip()
        ):
            webapp_cfg["lore_archive_db"] = wa_primary["lore_archive_db"].strip()

    cve_raw = webapp_cfg.get("cve_dir")
    cve_dir = str(cve_raw).strip() if isinstance(cve_raw, str) else ""
    if cve_dir:
        if wa_primary is not None:
            wa_primary["cve_dir"] = cve_dir
        if isinstance(app_config, dict):
            cve_sec = app_config.setdefault("cve", {})
            if isinstance(cve_sec, dict):
                cve_sec["repo_dir"] = cve_dir
    elif wa_primary is not None:
        top_cve = (
            str(app_config["cve"].get("repo_dir") or "").strip()
            if isinstance(app_config, dict) and isinstance(app_config.get("cve"), dict)
            else ""
        )
        if top_cve and "cve_dir" in wa_primary:
            wa_primary["cve_dir"] = top_cve
        elif (
            "cve_dir" in webapp_cfg
            and isinstance(wa_primary.get("cve_dir"), str)
            and wa_primary["cve_dir"].strip()
        ):
            webapp_cfg["cve_dir"] = wa_primary["cve_dir"].strip()

    if wa_primary is not None:
        wa_primary["default_version"] = default_ver


class WebappSettingsScreen(BaseScreen):
    """Configuration screen for webapp server, cache, tools, and source index settings."""

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)

        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            webapp_cfg = self.app.config.get("webapp")
            if not isinstance(webapp_cfg, dict):
                webapp_cfg = {}
                self.app.config["webapp"] = webapp_cfg
            cache_cfg = self.app.config.get("cache")
            if not isinstance(cache_cfg, dict):
                cache_cfg = {}
                self.app.config["cache"] = cache_cfg
        else:
            webapp_cfg = {}
            cache_cfg = {}

        self.webapp_cfg = webapp_cfg
        self.cache_cfg = cache_cfg

        wa_cache_cfg = webapp_cfg.get("cache") if isinstance(webapp_cfg.get("cache"), dict) else {}
        wa_jobs_cfg = webapp_cfg.get("jobs") if isinstance(webapp_cfg.get("jobs"), dict) else {}
        wa_tools_cfg = webapp_cfg.get("tools") if isinstance(webapp_cfg.get("tools"), dict) else {}
        wa_cp_cfg = wa_tools_cfg.get("checkpatch") if isinstance(wa_tools_cfg.get("checkpatch"), dict) else {}
        wa_si_cfg = webapp_cfg.get("source_index") if isinstance(webapp_cfg.get("source_index"), dict) else {}

        cur_host = str(webapp_cfg.get("host") or "0.0.0.0")
        cur_port = str(webapp_cfg.get("port") if webapp_cfg.get("port") is not None else 8000)
        cur_reload = _coerce_bool(webapp_cfg.get("reload"), False)

        cache_engine_opts = list(available_cache_engines()) or [DEFAULT_ENGINE]
        cur_ce_raw = str(cache_cfg.get("engine") or DEFAULT_ENGINE).strip().lower()
        try:
            cur_ce = get_cache_engine(cur_ce_raw).name.lower()
        except UnknownCacheEngine:
            cur_ce = DEFAULT_ENGINE
        idx_ce = cache_engine_opts.index(cur_ce) if cur_ce in cache_engine_opts else 0
        cur_cache_dir = str(cache_cfg.get("dir") or "")

        cur_cache_max_mb = str(wa_cache_cfg.get("max_mb") if wa_cache_cfg.get("max_mb") is not None else 1024)
        cur_jobs_workers = str(wa_jobs_cfg.get("workers") if wa_jobs_cfg.get("workers") is not None else 2)
        cur_prewarm = _coerce_bool(webapp_cfg.get("prewarm_indexes"), False)

        cur_cp_enabled = _coerce_bool(wa_cp_cfg.get("enabled"), False)
        cp_ref_raw = wa_cp_cfg.get("ref")
        cur_cp_ref = "latest-release" if cp_ref_raw is None else str(cp_ref_raw)
        cur_sandbox = str(wa_tools_cfg.get("sandbox") or "")

        cur_si_workers = str(wa_si_cfg.get("workers") if wa_si_cfg.get("workers") is not None else 2)
        cur_si_max_mb = str(wa_si_cfg.get("max_mb") if wa_si_cfg.get("max_mb") is not None else 1024)
        cur_si_revisions = str(wa_si_cfg.get("revisions") if wa_si_cfg.get("revisions") is not None else 8)

        # Box 1: Webapp Server (y=2, h=5 -> rows 3..5)
        self.inp_host = TextInput(y=3, x=4, width=26, label="Bind Host", value=cur_host, label_width=20)
        self.inp_port = TextInput(y=4, x=4, width=12, label="Listen Port", value=cur_port, label_width=20)
        self.chk_reload = Checkbox(y=5, x=4, label="Enable Uvicorn Auto-Reload", checked=cur_reload)

        # Box 2: Cache & Jobs (y=7, h=7 -> rows 8..12)
        self.sel_cache_engine = SelectList(
            y=8, x=4, width=16, label="Cache Engine", options=cache_engine_opts, selected_index=idx_ce, label_width=20
        )
        self.inp_cache_dir = TextInput(y=9, x=4, width=26, label="Cache Directory", value=cur_cache_dir, label_width=20)
        self.inp_cache_max_mb = TextInput(
            y=10, x=4, width=12, label="Webapp Cache (MB)", value=cur_cache_max_mb, label_width=20
        )
        self.inp_jobs_workers = TextInput(
            y=11, x=4, width=12, label="Job Workers", value=cur_jobs_workers, label_width=20
        )
        self.chk_prewarm_indexes = Checkbox(y=12, x=4, label="Prewarm Indexes at Startup", checked=cur_prewarm)

        # Box 3: Tools & Source Index (y=14, h=8 -> rows 15..20)
        self.chk_checkpatch_enabled = Checkbox(y=15, x=4, label="Enable checkpatch.pl Tool", checked=cur_cp_enabled)
        self.inp_checkpatch_ref = TextInput(
            y=16, x=4, width=24, label="Checkpatch Ref", value=cur_cp_ref, label_width=20
        )
        self.inp_tools_sandbox = TextInput(
            y=17, x=4, width=24, label="Tools Sandbox", value=cur_sandbox, label_width=20
        )
        self.inp_source_index_workers = TextInput(
            y=18, x=4, width=12, label="Src Index Workers", value=cur_si_workers, label_width=20
        )
        self.inp_source_index_max_mb = TextInput(
            y=19, x=4, width=12, label="Src Index Max MB", value=cur_si_max_mb, label_width=20
        )
        self.inp_source_index_revisions = TextInput(
            y=20, x=4, width=12, label="Src Index Revisions", value=cur_si_revisions, label_width=20
        )

        # Buttons (row 22)
        self.btn_save = Button(y=22, x=4, label="Save Settings", on_click=self.save)
        self.btn_cancel = Button(y=22, x=24, label="Cancel", on_click=self.cancel)

        self.status_bar = StatusBar(
            shortcuts=[
                ("Tab/Down", "Next"),
                ("Shift-Tab/Up", "Prev"),
                ("S", "Save"),
                ("F1/?", "Help"),
                ("Esc", "Back"),
            ]
        )

        self.widgets = [
            self.inp_host,
            self.inp_port,
            self.chk_reload,
            self.sel_cache_engine,
            self.inp_cache_dir,
            self.inp_cache_max_mb,
            self.inp_jobs_workers,
            self.chk_prewarm_indexes,
            self.chk_checkpatch_enabled,
            self.inp_checkpatch_ref,
            self.inp_tools_sandbox,
            self.inp_source_index_workers,
            self.inp_source_index_max_mb,
            self.inp_source_index_revisions,
            self.btn_save,
            self.btn_cancel,
        ]

        self.help_topic_for = {
            id(self.inp_host): "host",
            id(self.inp_port): "port",
            id(self.chk_reload): "reload",
            id(self.sel_cache_engine): "cache_engine",
            id(self.inp_cache_dir): "cache_dir",
            id(self.inp_cache_max_mb): "cache_max_mb",
            id(self.inp_jobs_workers): "jobs_workers",
            id(self.chk_prewarm_indexes): "prewarm_indexes",
            id(self.chk_checkpatch_enabled): "checkpatch",
            id(self.inp_checkpatch_ref): "checkpatch",
            id(self.inp_tools_sandbox): "checkpatch",
            id(self.inp_source_index_workers): "source_index",
            id(self.inp_source_index_max_mb): "source_index",
            id(self.inp_source_index_revisions): "source_index",
        }
        self.focus_widget(0)
        self.mark_form_clean()

    def current_help_topic(self) -> str:
        """Return the HELP_TOPICS key for the focused widget ('overview' when it has none)."""
        return self.help_topic_for.get(id(self.get_focused_widget()), "overview")

    def show_help_dialog(self) -> None:
        """Show the focused widget's help topic in a modal dialog."""
        title, paragraphs = HELP_TOPICS[self.current_help_topic()]
        ModalDialog(self.stdscr, title, "\n".join(paragraphs), ["OK"]).show()

    def _layout_widths(self, max_x: int) -> tuple[int, int]:
        """Return (form box width, help panel width); the panel width is 0 when it does not fit."""
        if max_x - 4 - 1 - _FORM_MIN_W < _HELP_MIN_W:
            return max(40, min(76, max_x - 4)), 0
        box_w = min(76, max(_FORM_MIN_W, max_x - 4 - 1 - 40))
        return box_w, max_x - 2 - box_w - 1 - 2

    def _draw_help_panel(self, y: int, x: int, h: int, w: int) -> None:
        """Draw the focused widget's help topic in a bordered panel."""
        title, paragraphs = HELP_TOPICS[self.current_help_topic()]
        draw_box(self.stdscr, y, x, h, w, title=title, color_pair=PAIR_BORDER)
        inner_h = h - 2
        lines = wrap_help(paragraphs, w - 4)
        if len(lines) > inner_h:
            lines = lines[: max(0, inner_h - 1)] + ["... F1: full text"]
        for i, line in enumerate(lines):
            attr = get_color(PAIR_MUTED) if line.startswith("... F1") else get_color(PAIR_DEFAULT)
            safe_addstr(self.stdscr, y + 1 + i, x + 2, line, attr)

    def _validate_fields(self) -> tuple[dict[str, Any], list[str]]:
        """Validate all webapp and cache inputs; return (parsed_values, errors)."""
        values: dict[str, Any] = {}
        errors: list[str] = []

        host = self.inp_host.get_value().strip()
        if not host or "\x00" in host:
            errors.append("Bind Host: must be a non-empty string without NUL bytes")
        else:
            values["host"] = host

        for label, widget in (
            ("Cache Directory", self.inp_cache_dir),
            ("Checkpatch Ref", self.inp_checkpatch_ref),
            ("Tools Sandbox", self.inp_tools_sandbox),
        ):
            if "\x00" in widget.get_value():
                errors.append(f"{label}: path/value cannot contain NUL bytes")

        int_specs = (
            ("port", self.inp_port, 1, 65535, "an integer between 1 and 65535"),
            ("cache_max_mb", self.inp_cache_max_mb, 0, None, "a whole number >= 0"),
            ("jobs_workers", self.inp_jobs_workers, 1, None, "a whole number >= 1"),
            ("si_workers", self.inp_source_index_workers, 1, None, "a whole number >= 1"),
            ("si_max_mb", self.inp_source_index_max_mb, 0, None, "a whole number >= 0"),
            ("si_revisions", self.inp_source_index_revisions, 0, None, "a whole number >= 0"),
        )
        for key, widget, min_val, max_val, desc in int_specs:
            raw = widget.get_value().strip()
            try:
                num = int(raw)
            except (ValueError, TypeError):
                num = None
            if num is None or num < min_val or (max_val is not None and num > max_val):
                errors.append(f"{widget.label}: '{raw}' must be {desc}")
            else:
                values[key] = num

        return values, errors

    def draw(self) -> None:
        """Render the webapp settings screen."""
        self.clear()
        max_y, max_x = self.get_maxyx()

        title = "Webapp Server, Cache & Tools Configuration"
        safe_addstr(self.stdscr, 1, 2, title, get_color(PAIR_HEADER) | curses.A_BOLD)

        box_w, help_w = self._layout_widths(max_x)
        draw_box(self.stdscr, 2, 2, 5, box_w, title="Webapp Server", color_pair=PAIR_BORDER)
        draw_box(self.stdscr, 7, 2, 7, box_w, title="Cache & Jobs", color_pair=PAIR_BORDER)
        draw_box(self.stdscr, 14, 2, 8, box_w, title="Tools & Source Index", color_pair=PAIR_BORDER)

        if help_w:
            self._draw_help_panel(2, 2 + box_w + 1, max(4, max_y - 3), help_w)

        for widget in self.widgets:
            if hasattr(widget, "draw"):
                widget.draw(self.stdscr)

        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Handle navigation, shortcuts, and widget key events."""
        if ch in (9, curses.KEY_DOWN):
            self.focus_next()
            return True
        if ch in (curses.KEY_UP, getattr(curses, "KEY_BTAB", 353), 353):
            self.focus_prev()
            return True
        if ch == 27:
            self.cancel()
            return True
        if ch == 19:
            self.save()
            return True
        if ch == getattr(curses, "KEY_F1", 265):
            self.show_help_dialog()
            return True

        focused = self.get_focused_widget()
        if focused is not None and hasattr(focused, "handle_key") and focused.handle_key(ch):
            return True

        if ch == ord("?"):
            self.show_help_dialog()
            return True
        if ch in (ord("s"), ord("S")):
            self.save()
            return True
        if ch in (10, 13, curses.KEY_ENTER):
            if focused is self.btn_save:
                self.save()
                return True
            if focused is self.btn_cancel:
                self.cancel()
                return True

        return False

    def save(self) -> bool:
        """Validate and persist webapp & cache settings to `app.config`, mark dirty, and pop screen."""
        parsed, errors = self._validate_fields()
        if errors:
            ModalDialog(
                self.stdscr,
                "Invalid Value",
                "Settings were not saved:\n" + "\n".join(errors),
                ["OK"],
            ).show()
            return False

        if self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict):
            webapp_cfg = self.app.config.setdefault("webapp", {})
            if not isinstance(webapp_cfg, dict):
                webapp_cfg = {}
                self.app.config["webapp"] = webapp_cfg
            cache_cfg = self.app.config.setdefault("cache", {})
            if not isinstance(cache_cfg, dict):
                cache_cfg = {}
                self.app.config["cache"] = cache_cfg
        else:
            webapp_cfg = self.webapp_cfg
            cache_cfg = self.cache_cfg

        webapp_cfg["host"] = parsed["host"]
        webapp_cfg["port"] = parsed["port"]
        webapp_cfg["reload"] = self.chk_reload.is_checked()
        webapp_cfg["prewarm_indexes"] = self.chk_prewarm_indexes.is_checked()

        wa_cache = webapp_cfg.get("cache")
        if not isinstance(wa_cache, dict):
            wa_cache = {}
            webapp_cfg["cache"] = wa_cache
        wa_cache["max_mb"] = parsed["cache_max_mb"]

        wa_jobs = webapp_cfg.get("jobs")
        if not isinstance(wa_jobs, dict):
            wa_jobs = {}
            webapp_cfg["jobs"] = wa_jobs
        wa_jobs["workers"] = parsed["jobs_workers"]

        wa_tools = webapp_cfg.get("tools")
        if not isinstance(wa_tools, dict):
            wa_tools = {}
            webapp_cfg["tools"] = wa_tools
        wa_cp = wa_tools.get("checkpatch")
        if not isinstance(wa_cp, dict):
            wa_cp = {}
            wa_tools["checkpatch"] = wa_cp
        wa_cp["enabled"] = self.chk_checkpatch_enabled.is_checked()
        wa_cp["ref"] = self.inp_checkpatch_ref.get_value().strip() or "latest-release"
        wa_tools["sandbox"] = self.inp_tools_sandbox.get_value().strip()

        wa_si = webapp_cfg.get("source_index")
        if not isinstance(wa_si, dict):
            wa_si = {}
            webapp_cfg["source_index"] = wa_si
        wa_si["workers"] = parsed["si_workers"]
        wa_si["max_mb"] = parsed["si_max_mb"]
        wa_si["revisions"] = parsed["si_revisions"]

        cache_cfg["engine"] = self.sel_cache_engine.get_selected()
        cache_cfg["dir"] = self.inp_cache_dir.get_value().strip()

        app_cfg = self.app.config if (self.app is not None and hasattr(self.app, "config") and isinstance(self.app.config, dict)) else None
        sync_webapp_repositories(app_cfg, webapp_cfg)

        self.webapp_cfg = webapp_cfg
        self.cache_cfg = cache_cfg

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
