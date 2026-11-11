"""DB Content Viewer: tabs showing what the configured database holds, and cleaning of interrupted runs.

Tabs: version parse states, repositories and their refs, mailing-list / CVE data presence, and table
sizes. Data comes from `core.tui.db_content` (`open_db_session()` + `collect_snapshot()`); the `C` key
removes the rows of interrupted version runs after a preview and a typed `CLEAN` confirmation.
"""

from __future__ import annotations

import curses
import time
from typing import Any, Callable, Optional

from core.tui.db_content import (
    PARTIAL_STATES,
    STATE_DONE,
    STATE_IN_PROGRESS,
    STATE_LABELS,
    STATE_NEW,
    CleanResult,
    DBSnapshot,
    FeedTable,
    RepoRow,
    TableRow,
    VersionRow,
    clean_versions,
    collect_snapshot,
    describe_target,
    format_bytes,
    format_count,
    open_db_session,
)
from core.tui.screens.base import BaseScreen
from core.tui.screens.repos import ModalTextInputDialog
from core.tui.theme import (
    PAIR_ACTIVE,
    PAIR_BORDER,
    PAIR_DEFAULT,
    PAIR_ERROR,
    PAIR_HEADER,
    PAIR_HIGHLIGHT,
    PAIR_MUTED,
    PAIR_SUCCESS,
    get_color,
)
from core.tui.widgets import ModalDialog, ScrollableList, StatusBar

CONFIRM_WORD = "CLEAN"
PREVIEW_LIMIT = 10

TAB_VERSIONS = "versions"
TAB_REPOS = "repos"
TAB_FEEDS = "feeds"
TAB_TABLES = "tables"
TABS = [
    (TAB_VERSIONS, "Versions"),
    (TAB_REPOS, "Repos & Branches"),
    (TAB_FEEDS, "Mailing List & CVE"),
    (TAB_TABLES, "Tables & Size"),
]

FILTER_ALL = "all"
FILTER_PARTIAL = "partial"
FILTER_PARSED = "parsed"
FILTER_NEW = "new"
FILTERS = [
    (FILTER_ALL, "All"),
    (FILTER_PARTIAL, "Partial only"),
    (FILTER_PARSED, "Parsed only"),
    (FILTER_NEW, "Not parsed"),
]

COLUMN_HEADERS = {
    TAB_VERSIONS: f"{'Repository':<14} {'Ref':<24} {'Type':<7} {'State':<22} {'commit_id':>9}  Updated",
    TAB_REPOS: f"{'Repository':<20} {'Network':<12} {'Default':<12} {'Tags':>6} {'Branches':>8} {'Parsed':>7} {'Partial':>7}",
    TAB_FEEDS: f"{'Feed':<6} {'Table / Source':<22} {'Status':<8} {'~Rows':>14} {'Size':>11}",
    TAB_TABLES: f"{'Table':<28} {'~Rows':>14} {'Data':>11} {'Index':>11} {'Total':>11}",
}

VIEW_KEY = "db_content"
MIN_W, MIN_H = 60, 12


def format_time(ts: int | float) -> str:
    """Local `YYYY-MM-DD HH:MM` for a Unix timestamp, `-` for 0."""
    if not ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (OverflowError, OSError, ValueError):
        return str(ts)


def filter_versions(versions: list[VersionRow], mode: str) -> list[VersionRow]:
    """Versions matching the Versions-tab filter `mode`."""
    if mode == FILTER_PARTIAL:
        return [v for v in versions if v.state in PARTIAL_STATES]
    if mode == FILTER_PARSED:
        return [v for v in versions if v.state == STATE_DONE]
    if mode == FILTER_NEW:
        return [v for v in versions if v.state == STATE_NEW]
    return list(versions)


def _feed_status(f: FeedTable) -> str:
    if not f.exists:
        return "missing"
    return "present" if f.has_rows else "empty"


class DBContentScreen(BaseScreen):
    """Tabbed read-only view of the configured database, with cleaning of interrupted version runs.

    `session_factory(cfg)` (a context manager yielding an engine), `collector(db, target)` and
    `cleaner(db, versions)` default to `core.tui.db_content`'s functions and are injectable for tests;
    `text_prompt` is the dialog class asking for the `CLEAN` confirmation.
    """

    def __init__(
        self,
        stdscr: Any,
        app: Any = None,
        session_factory: Optional[Callable[[dict[str, Any]], Any]] = None,
        collector: Optional[Callable[..., DBSnapshot]] = None,
        cleaner: Optional[Callable[[Any, list[VersionRow]], list[CleanResult]]] = None,
        text_prompt: type = ModalTextInputDialog,
    ) -> None:
        super().__init__(stdscr, app)
        self.session_factory = session_factory or open_db_session
        self.collector = collector or collect_snapshot
        self.cleaner = cleaner or clean_versions
        self.text_prompt = text_prompt

        self.snapshot: DBSnapshot | None = None
        self.load_error: str | None = None
        self.loaded = False

        state = self.recall_view(VIEW_KEY)
        tab_keys = [k for k, _ in TABS]
        filter_keys = [k for k, _ in FILTERS]
        self.tab_index = tab_keys.index(state["tab"]) if state.get("tab") in tab_keys else 0
        self.filter_index = filter_keys.index(state["filter"]) if state.get("filter") in filter_keys else 0

        self.lists = {key: ScrollableList(y=4, x=0, height=10, width=MIN_W, title=label) for key, label in TABS}
        self.status_bar = StatusBar([
            ("Tab/1-4", "Tabs"),
            ("Enter", "Details"),
            ("F", "Filter"),
            ("C", "Clean partial"),
            ("R", "Reload"),
            ("E", "Errors"),
            ("Esc", "Back"),
        ])

    # --- State -------------------------------------------------------------------------------

    @property
    def tab(self) -> str:
        return TABS[self.tab_index][0]

    @property
    def filter_mode(self) -> str:
        return FILTERS[self.filter_index][0]

    @property
    def config(self) -> dict[str, Any]:
        cfg = getattr(self.app, "config", None)
        return cfg if isinstance(cfg, dict) else {}

    def _remember(self) -> None:
        self.remember_view(VIEW_KEY, tab=self.tab, filter=self.filter_mode)

    def set_tab(self, index: int) -> None:
        self.tab_index = index % len(TABS)
        self._remember()

    def cycle_filter(self) -> None:
        """Advance the Versions filter (all → partial → parsed → not parsed) and show the Versions tab."""
        self.filter_index = (self.filter_index + 1) % len(FILTERS)
        self.tab_index = 0
        self.lists[TAB_VERSIONS].set_items(self.items_for(TAB_VERSIONS), keep_selection=False)
        self._remember()

    def on_enter(self) -> None:
        if not self.loaded:
            self.reload()

    def on_leave(self) -> None:
        self._remember()

    def reload(self) -> bool:
        """Connect and read a fresh snapshot; a failure is kept in `load_error` and shown on screen."""
        self.loaded = True
        self.show_busy("Connecting to database...")
        cfg = self.config
        target = describe_target(cfg)
        try:
            with self.session_factory(cfg) as db:
                snap = self.collector(db, target)
        except Exception as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            self.snapshot = None
            self._refresh_items()
            return False
        self.snapshot = snap
        self.load_error = None
        self._refresh_items()
        return True

    def _refresh_items(self) -> None:
        for key, _label in TABS:
            self.lists[key].set_items(self.items_for(key))

    def items_for(self, tab: str) -> list[Any]:
        """Rows of `tab` for the current snapshot (VersionRow, RepoRow, TableRow or tagged tuples)."""
        snap = self.snapshot
        if snap is None:
            return []
        if tab == TAB_VERSIONS:
            return filter_versions(snap.versions, self.filter_mode)
        if tab == TAB_REPOS:
            return list(snap.repos)
        if tab == TAB_FEEDS:
            rows: list[Any] = [("feed", "ML", f) for f in snap.mailing_list]
            rows += [("feed", "CVE", f) for f in snap.cve]
            rows += [("cve_source", s) for s in snap.cve_sources]
            return rows
        if tab == TAB_TABLES:
            return list(snap.tables) + [("missing", name) for name in snap.missing_tables]
        return []

    def selected_item(self) -> Any | None:
        return self.lists[self.tab].get_selected()

    # --- Rendering ---------------------------------------------------------------------------

    def render_row(self, item: Any) -> tuple[str, int]:
        """(text, attribute) of one list row."""
        if isinstance(item, VersionRow):
            label = STATE_LABELS.get(item.state, item.state)
            text = (
                f"{item.repo:<14.14} {item.ref_name:<24.24} {item.ref_type:<7.7} {label:<22.22} "
                f"{item.commit_id:>9}  {format_time(item.updated_at)}"
            )
            if item.state == STATE_DONE:
                return text, get_color(PAIR_SUCCESS)
            if item.state == STATE_IN_PROGRESS:
                return text, get_color(PAIR_HIGHLIGHT)
            if item.state in PARTIAL_STATES:
                return text, get_color(PAIR_ERROR)
            return text, get_color(PAIR_MUTED)
        if isinstance(item, RepoRow):
            text = (
                f"{item.name:<20.20} {item.network:<12.12} {item.default_branch:<12.12} {item.tags:>6} "
                f"{item.branches:>8} {item.parsed:>7} {item.partial:>7}"
            )
            return text, get_color(PAIR_ERROR) if item.partial else get_color(PAIR_DEFAULT)
        if isinstance(item, TableRow):
            text = (
                f"{item.name:<28.28} {format_count(item.rows):>14} {format_bytes(item.data_bytes):>11} "
                f"{format_bytes(item.index_bytes):>11} {format_bytes(item.total_bytes):>11}"
            )
            if not item.in_schema:
                return text + "  (not in schema)", get_color(PAIR_MUTED)
            return text, get_color(PAIR_DEFAULT)
        if isinstance(item, tuple) and item and item[0] == "feed":
            _kind, group, f = item
            status = _feed_status(f)
            text = f"{group:<6} {f.name:<22.22} {status:<8} {format_count(f.rows):>14} {format_bytes(f.data_bytes):>11}"
            color = PAIR_SUCCESS if status == "present" else (PAIR_ERROR if status == "missing" else PAIR_MUTED)
            return text, get_color(color)
        if isinstance(item, tuple) and item and item[0] == "cve_source":
            s = item[1]
            text = (
                f"{'CVE':<6} source '{s.get('name', '')}': {format_count(s.get('records', 0))} records, "
                f"head {str(s.get('head', ''))[:12]}, built {format_time(s.get('built_at', 0))}"
            )
            return text, get_color(PAIR_HIGHLIGHT)
        if isinstance(item, tuple) and item and item[0] == "missing":
            return f"{item[1]:<28.28} (missing from database)", get_color(PAIR_ERROR)
        return str(item), get_color(PAIR_DEFAULT)

    def summary_line(self) -> str:
        """Per-tab summary drawn under the title."""
        snap = self.snapshot
        if snap is None:
            return "Not loaded" if self.load_error is None else "Connection failed"
        parts: list[str] = []
        if self.tab == TAB_VERSIONS:
            c = snap.state_counts()
            partial = sum(c.get(s, 0) for s in PARTIAL_STATES)
            parts += [
                f"parsed {c.get(STATE_DONE, 0)}",
                f"partial {partial}",
                f"not parsed {c.get(STATE_NEW, 0)}",
                f"filter: {FILTERS[self.filter_index][1]}",
            ]
        elif self.tab == TAB_REPOS:
            parts += [
                f"{len(snap.repos)} repositories",
                f"{sum(r.branches for r in snap.repos)} branches",
                f"{sum(r.tags for r in snap.repos)} tags",
            ]
        elif self.tab == TAB_FEEDS:
            ml = any(f.has_rows for f in snap.mailing_list)
            cve = any(f.has_rows for f in snap.cve)
            parts += [f"mailing list: {'present' if ml else 'absent'}", f"CVE: {'present' if cve else 'absent'}"]
        else:
            rows, data, index = snap.totals()
            parts += [
                f"{len(snap.tables)} tables",
                f"~{format_count(rows)} rows",
                f"data {format_bytes(data)}",
                f"index {format_bytes(index)}",
                f"total {format_bytes(data + index)}",
            ]
            if snap.missing_tables:
                parts.append(f"{len(snap.missing_tables)} missing")
        if snap.errors:
            parts.append(f"{len(snap.errors)} error(s) [E]")
        return " | ".join(parts) if parts else "-"

    def draw(self) -> None:
        self.clear()
        max_y, max_x = self.get_maxyx()
        if max_y < MIN_H or max_x < MIN_W:
            self.safe_addstr(0, 0, f"Terminal window too small (min {MIN_W}x{MIN_H}).", get_color(PAIR_ERROR))
            self.refresh()
            return

        title = " DB Content Viewer "
        self.safe_addstr(0, 1, title, get_color(PAIR_HEADER) | curses.A_BOLD)
        loaded = ""
        if self.snapshot is not None and self.snapshot.loaded_at:
            loaded = time.strftime("loaded %H:%M:%S", time.localtime(self.snapshot.loaded_at))
        target = self.snapshot.target if self.snapshot is not None else describe_target(self.config)
        room = max(0, max_x - len(title) - len(loaded) - 5)
        self.safe_addstr(0, 1 + len(title) + 1, target[:room], get_color(PAIR_MUTED))
        if loaded:
            self.safe_addstr(0, max_x - len(loaded) - 2, loaded, get_color(PAIR_MUTED))

        summary = self.summary_line()
        has_errors = self.snapshot is not None and bool(self.snapshot.errors)
        self.safe_addstr(1, 2, summary[: max_x - 3], get_color(PAIR_ERROR if has_errors else PAIR_MUTED))

        # Tab bar
        col = 1
        for i, (_key, label) in enumerate(TABS):
            text = f" {i + 1} {label} "
            if col + len(text) >= max_x:
                break
            active = i == self.tab_index
            attr = (get_color(PAIR_ACTIVE) | curses.A_BOLD | curses.A_REVERSE) if active else get_color(PAIR_BORDER)
            self.safe_addstr(2, col, text, attr)
            col += len(text) + 1

        box_y, box_h, box_w = 3, max(4, max_y - 4), max_x
        lst = self.lists[self.tab]
        lst.y, lst.x, lst.height, lst.width = box_y, 0, box_h, box_w
        self.draw_box(box_y, 0, box_h, box_w, title=f" {TABS[self.tab_index][1]} ", color_pair=PAIR_ACTIVE)
        inner_w = box_w - 2
        self.safe_addstr(box_y + 1, 1, ("  " + COLUMN_HEADERS[self.tab])[:inner_w], get_color(PAIR_MUTED) | curses.A_BOLD)

        rows_y = box_y + 2
        rows_h = max(1, box_h - 3)
        if self.load_error is not None:
            msg = [
                "Could not read the database:",
                self.load_error,
                "",
                "Check option [2] Database, SSH Tunnel & Auth, then press [R] to retry or [Esc] to go back.",
            ]
            for i, line in enumerate(msg[:rows_h]):
                self.safe_addstr(rows_y + i, 3, line[: inner_w - 3], get_color(PAIR_ERROR if i < 2 else PAIR_MUTED))
        elif self.snapshot is None:
            self.safe_addstr(rows_y, 3, "Not loaded yet. Press [R] to connect.", get_color(PAIR_MUTED))
        elif not lst.items:
            empty = "No rows match the filter." if self.tab == TAB_VERSIONS and self.filter_mode != FILTER_ALL else "No data."
            self.safe_addstr(rows_y, 3, empty, get_color(PAIR_MUTED))
        else:
            self._draw_rows(lst, rows_y, rows_h, inner_w, box_y, box_h, box_w)

        self.status_bar.y = max_y - 1
        self.status_bar.draw(self.stdscr)
        self.refresh()

    def _draw_rows(self, lst: ScrollableList, rows_y: int, rows_h: int, inner_w: int, box_y: int, box_h: int, box_w: int) -> None:
        # Page keys move by the visible row count
        lst.height = rows_h + 2
        lst.selected_index = max(0, min(lst.selected_index, len(lst.items) - 1))
        if lst.selected_index < lst.scroll_offset:
            lst.scroll_offset = lst.selected_index
        elif lst.selected_index >= lst.scroll_offset + rows_h:
            lst.scroll_offset = lst.selected_index - rows_h + 1
        if lst.scroll_offset > 0:
            self.safe_addstr(box_y, box_w - 3, "↑", get_color(PAIR_ACTIVE) | curses.A_BOLD)
        if lst.scroll_offset + rows_h < len(lst.items):
            self.safe_addstr(box_y + box_h - 1, box_w - 3, "↓", get_color(PAIR_ACTIVE) | curses.A_BOLD)
        for r in range(rows_h):
            idx = lst.scroll_offset + r
            if idx >= len(lst.items):
                break
            text, attr = self.render_row(lst.items[idx])
            selected = idx == lst.selected_index
            line = (("► " if selected else "  ") + text)[:inner_w].ljust(inner_w)
            if selected:
                attr = get_color(PAIR_HIGHLIGHT) | curses.A_BOLD | curses.A_REVERSE
            self.safe_addstr(rows_y + r, 1, line, attr)

    # --- Dialogs -----------------------------------------------------------------------------

    def _dialog(self, title: str, message: str, buttons: list[str] | None = None, default_button: int = 0) -> Optional[str]:
        return ModalDialog.show_dialog(self.stdscr, title, message, buttons or ["OK"], default_button=default_button)

    def details_text(self, item: Any) -> tuple[str, str]:
        """(title, message) of the details dialog for `item`."""
        if isinstance(item, VersionRow):
            lines = [
                f"Repository:  {item.repo}",
                f"Ref:         {item.ref_name} ({item.ref_type})",
                f"commit_id:   {item.commit_id}   network_id: {item.network_id}",
                f"State:       {STATE_LABELS.get(item.state, item.state)}",
                f"Updated:     {format_time(item.updated_at)}",
            ]
            if item.worker_id:
                lines.append(f"Worker:      {item.worker_id}")
            if item.cleanable:
                lines += ["", "Interrupted run: [C] removes its partial rows."]
            elif item.state == STATE_IN_PROGRESS:
                lines += ["", "A worker heartbeated within the last 3 minutes: the run is live and never cleaned."]
            elif item.state in PARTIAL_STATES:
                lines += ["", "Checkpoint rows without a state row: the pipeline checks it (90% tree rule) on its next run."]
            return item.ref_name, "\n".join(lines)
        if isinstance(item, RepoRow):
            branches = ", ".join(item.branch_names[:30]) or "-"
            if len(item.branch_names) > 30:
                branches += f", ... and {len(item.branch_names) - 30} more"
            lines = [
                f"Repository:      {item.name} (repo_id {item.repo_id})",
                f"Network:         {item.network}",
                f"Git URL:         {item.git_url or '-'}",
                f"Default branch:  {item.default_branch}",
                f"Refs:            {item.tags} tags, {item.branches} branches, {item.commits} bare commits",
                f"Parse states:    {item.parsed} parsed, {item.partial} partial",
                "",
                f"Branches: {branches}",
            ]
            return item.name, "\n".join(lines)
        if isinstance(item, TableRow):
            lines = [
                f"Rows (estimate): {format_count(item.rows)}",
                f"Data:            {format_bytes(item.data_bytes)} ({format_count(item.data_bytes)} bytes)",
                f"Index:           {format_bytes(item.index_bytes)} ({format_count(item.index_bytes)} bytes)",
                f"Total:           {format_bytes(item.total_bytes)}",
                f"In schema:       {'yes' if item.in_schema else 'no (not declared in core/DBLayout.py)'}",
            ]
            return item.name, "\n".join(lines)
        if isinstance(item, tuple) and item and item[0] == "feed":
            _k, group, f = item
            lines = [
                f"Feed:            {'Mailing list' if group == 'ML' else 'CVE'}",
                f"Status:          {_feed_status(f)}",
                f"Rows (estimate): {format_count(f.rows)}",
                f"Size:            {format_bytes(f.data_bytes)}",
            ]
            return f.name, "\n".join(lines)
        if isinstance(item, tuple) and item and item[0] == "cve_source":
            s = item[1]
            lines = [f"{k + ':':<10} {v}" for k, v in s.items()]
            lines[-1] = f"{'built_at:':<10} {format_time(s.get('built_at', 0))}"
            return f"CVE source {s.get('name', '')}", "\n".join(lines)
        if isinstance(item, tuple) and item and item[0] == "missing":
            return item[1], "Declared in core/DBLayout.py but missing from the database.\nThe next pipeline run creates it."
        return "Details", str(item)

    def show_details(self) -> Optional[str]:
        item = self.selected_item()
        if item is None:
            return None
        title, message = self.details_text(item)
        return self._dialog(title, message)

    def show_errors(self) -> Optional[str]:
        if self.load_error is not None:
            return self._dialog("Connection Error", self.load_error)
        if self.snapshot is None or not self.snapshot.errors:
            return self._dialog("No Errors", "Every section was read without errors.")
        return self._dialog("Section Errors", "\n".join(self.snapshot.errors))

    # --- Cleaning ----------------------------------------------------------------------------

    def choose_clean_targets(self) -> list[VersionRow] | None:
        """Interrupted versions the user wants cleaned, or None when there is nothing to do / cancelled."""
        if self.snapshot is None:
            self._dialog("Not Loaded", "The database has not been read. Press [R] to connect first.")
            return None
        candidates = self.snapshot.cleanable_versions()
        if not candidates:
            self._dialog(
                "Nothing to Clean",
                "No interrupted version runs were found.\n"
                "Runs in progress on a heartbeating worker are never cleaned; legacy partial versions "
                "are verified and purged by the pipeline when it parses them again.",
            )
            return None
        selected = self.selected_item() if self.tab == TAB_VERSIONS else None
        if isinstance(selected, VersionRow) and selected.cleanable and len(candidates) > 1:
            this_label, all_label = "This version", f"All interrupted ({len(candidates)})"
            choice = self._dialog(
                "Clean Partial Versions",
                f"Clean only {selected.repo} {selected.ref_name}, or every interrupted run?",
                [this_label, all_label, "Cancel"],
                default_button=2,
            )
            if choice == this_label:
                return [selected]
            if choice == all_label:
                return candidates
            return None
        return candidates

    def clean_partial(self) -> bool:
        """Preview, confirm (type CLEAN) and clean interrupted runs, then reload. Returns True when cleaned."""
        targets = self.choose_clean_targets()
        if not targets:
            return False
        target_db = self.snapshot.target if self.snapshot is not None else describe_target(self.config)
        lines = [f"Deletes every row written by {len(targets)} interrupted run(s) in", f"{target_db}:", ""]
        for v in targets[:PREVIEW_LIMIT]:
            lines.append(f"  {v.repo} {v.ref_name} (commit_id {v.commit_id})")
        if len(targets) > PREVIEW_LIMIT:
            lines.append(f"  ... and {len(targets) - PREVIEW_LIMIT} more")
        lines += ["", "The versions then read as not parsed; the next run parses them from scratch."]
        if self._dialog("Clean Preview", "\n".join(lines), ["Clean", "Cancel"], default_button=1) != "Clean":
            return False

        typed = self.text_prompt(
            self.stdscr, title=" Confirm Clean ", prompt=f"Type {CONFIRM_WORD} to delete the partial rows:"
        ).show()
        if (typed or "").strip() != CONFIRM_WORD:
            self._dialog("Clean Aborted", f"Confirmation text did not match '{CONFIRM_WORD}'.\nNo rows were changed.")
            return False

        self.show_busy(f"Cleaning {len(targets)} interrupted run(s)...")
        try:
            with self.session_factory(self.config) as db:
                results = self.cleaner(db, targets)
        except Exception as exc:
            self._dialog("Clean Failed", f"{type(exc).__name__}: {exc}")
            self.reload()
            return False

        ok = [r for r in results if r.ok]
        lines = [f"{len(ok)} of {len(results)} run(s) cleaned.", ""]
        for r in results[:PREVIEW_LIMIT + 5]:
            v = r.version
            outcome = f"ok ({r.method})" if r.ok else f"FAILED: {r.error}"
            lines.append(f"{v.repo} {v.ref_name}: {outcome}")
        if len(results) > PREVIEW_LIMIT + 5:
            lines.append(f"... and {len(results) - PREVIEW_LIMIT - 5} more")
        self._dialog("Clean Complete" if len(ok) == len(results) else "Clean Finished With Errors", "\n".join(lines))
        self.reload()
        return bool(ok)

    # --- Keys --------------------------------------------------------------------------------

    def handle_key(self, ch: int) -> bool:
        lst = self.lists[self.tab]
        if ch in (9, curses.KEY_RIGHT):
            self.set_tab(self.tab_index + 1)
            return True
        if ch in (getattr(curses, "KEY_BTAB", 353), curses.KEY_LEFT):
            self.set_tab(self.tab_index - 1)
            return True
        if ord("1") <= ch <= ord(str(len(TABS))):
            self.set_tab(ch - ord("1"))
            return True
        if ch in (curses.KEY_UP, ord("k")):
            lst.select_prev()
            return True
        if ch in (curses.KEY_DOWN, ord("j")):
            lst.select_next()
            return True
        if ch == curses.KEY_PPAGE:
            lst.page_up()
            return True
        if ch == curses.KEY_NPAGE:
            lst.page_down()
            return True
        if ch == curses.KEY_HOME:
            lst.select_first()
            return True
        if ch == curses.KEY_END:
            lst.select_last()
            return True
        if ch in (10, 13, curses.KEY_ENTER, ord(" ")):
            self.show_details()
            return True
        if ch in (ord("f"), ord("F")):
            self.cycle_filter()
            return True
        if ch in (ord("r"), ord("R")):
            self.reload()
            return True
        if ch in (ord("c"), ord("C")):
            self.clean_partial()
            return True
        if ch in (ord("e"), ord("E")):
            self.show_errors()
            return True
        if ch in (27, ord("q"), ord("Q")):
            self._remember()
            if self.app is not None and callable(getattr(self.app, "pop_screen", None)):
                self.app.pop_screen()
            return True
        return False
