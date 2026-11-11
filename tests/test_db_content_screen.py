"""tests/test_db_content_screen.py - Unit tests for DBContentScreen (DB Content Viewer TUI screen)."""

from __future__ import annotations

import curses
import unittest
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from core.tui.db_content import (
    STATE_DONE,
    STATE_IN_PROGRESS,
    STATE_INTERRUPTED,
    STATE_LEGACY,
    STATE_NEW,
    CleanResult,
    DBSnapshot,
    FeedTable,
    RepoRow,
    TableRow,
    VersionRow,
)
from core.tui.screens import db_content as dbc
from core.tui.screens.db_content import (
    FILTER_ALL,
    FILTER_NEW,
    FILTER_PARSED,
    FILTER_PARTIAL,
    TAB_FEEDS,
    TAB_REPOS,
    TAB_TABLES,
    TAB_VERSIONS,
    TABS,
    DBContentScreen,
    filter_versions,
)
from core.tui.screens.main_menu import MainMenuScreen
from core.tui.widgets import ModalDialog
from tests.test_tui import MockCursesWindow

TAB, ENTER, ESC = 9, 10, 27


class MockApp:
    """App double with an in-memory config; never touches config.json."""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {"database": {"engine": "mariadb", "database": "kip", "host": "db", "port": 3306}}
        self.is_dirty = False
        self.config_path = "config.json"
        self.view_state: dict[str, dict[str, Any]] = {}
        self.popped = 0
        self.pushed: list[Any] = []

    def pop_screen(self) -> None:
        self.popped += 1

    def push_screen(self, screen: Any) -> None:
        self.pushed.append(screen)


def make_snapshot(errors: list[str] | None = None) -> DBSnapshot:
    versions = [
        VersionRow(1, "linux", "v3.0", "tag", 10, STATE_DONE, 1_700_000_000),
        VersionRow(1, "linux", "v3.1", "tag", 11, STATE_INTERRUPTED, 1_700_000_100, "node-a"),
        VersionRow(1, "linux", "v3.2", "tag", 12, STATE_IN_PROGRESS, 1_700_000_200, "node-b"),
        VersionRow(1, "linux", "v3.3", "tag", 13, STATE_LEGACY),
        VersionRow(1, "linux", "v3.4", "tag", 14, STATE_NEW),
        VersionRow(1, "linux", "v3.5", "tag", 15, STATE_INTERRUPTED, 1_700_000_300),
        VersionRow(1, "linux", "master", "branch", 15, STATE_INTERRUPTED, 1_700_000_300),
    ]
    repos = [RepoRow(1, "linux", "linux", "https://example.invalid/linux.git", "master", 6, 1, 0, 1, 4, ["master"])]
    return DBSnapshot(
        target="mariadb 'kip' at db:3306",
        versions=versions,
        repos=repos,
        mailing_list=[FeedTable("m_commit_email", True, True, 1200, 4096), FeedTable("m_commit_link", True, False)],
        cve=[FeedTable("m_cve_source", True, True, 1, 16384), FeedTable("m_cve", False, False)],
        cve_sources=[{"name": "cvelistV5", "layout": "v5", "head": "ab" * 20, "format": 1, "records": 250000, "built_at": 1_700_000_000}],
        tables=[TableRow("m_commit", 1000, 1 << 20, 1 << 18, True), TableRow("old_tmp", 5, 16384, 0, False)],
        missing_tables=["m_cve_file"],
        errors=list(errors or []),
        loaded_at=1_700_000_500,
    )


class Recorder:
    """Injectable session factory / collector / cleaner recording their calls."""

    def __init__(self, snapshots: list[DBSnapshot] | None = None, fail: Exception | None = None) -> None:
        self.snapshots = list(snapshots or [make_snapshot()])
        self.fail = fail
        self.sessions = 0
        self.collected = 0
        self.cleaned: list[list[VersionRow]] = []

    @contextmanager
    def session(self, cfg: dict[str, Any]):
        self.sessions += 1
        if self.fail is not None:
            raise self.fail
        yield object()

    def collect(self, db: Any, target: str = "") -> DBSnapshot:
        self.collected += 1
        return self.snapshots[min(self.collected - 1, len(self.snapshots) - 1)]

    def clean(self, db: Any, versions: list[VersionRow]) -> list[CleanResult]:
        self.cleaned.append(list(versions))
        return [CleanResult(v, True, method="rollback") for v in versions]


def make_prompt(answer: str | None):
    class Prompt:
        calls = 0

        def __init__(self, stdscr: Any, title: str = "", prompt: str = "", **_kw: Any) -> None:
            pass

        def show(self) -> str | None:
            Prompt.calls += 1
            return answer

    return Prompt


class DBContentTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.win = MockCursesWindow(30, 100)
        self.app = MockApp()
        self.rec = Recorder()
        self.dialogs: list[tuple[str, str, list[str]]] = []
        self.answers: list[str | None] = []

        def fake_dialog(stdscr: Any, title: str, message: str, buttons: Any = None, default_button: int = 0) -> Any:
            self.dialogs.append((title, message, list(buttons or ["OK"])))
            return self.answers.pop(0) if self.answers else (buttons or ["OK"])[default_button]

        p = patch.object(ModalDialog, "show_dialog", side_effect=fake_dialog)
        p.start()
        self.addCleanup(p.stop)

    def make_screen(self, prompt_answer: str | None = "CLEAN", rec: Recorder | None = None) -> DBContentScreen:
        rec = rec or self.rec
        screen = DBContentScreen(
            self.win,
            self.app,
            session_factory=rec.session,
            collector=rec.collect,
            cleaner=rec.clean,
            text_prompt=make_prompt(prompt_answer),
        )
        screen.on_enter()
        return screen

    def drawn_text(self) -> str:
        return "\n".join(c[1][2] for c in self.win.calls if c[0] == "addstr")


class TestLoadingAndDrawing(DBContentTestBase):
    def test_loads_on_enter_and_draws_every_tab(self) -> None:
        screen = self.make_screen()
        self.assertEqual(self.rec.collected, 1)
        for i, (key, label) in enumerate(TABS):
            screen.set_tab(i)
            self.win.calls.clear()
            screen.draw()
            text = self.drawn_text()
            self.assertIn(label, text)
            self.assertTrue(screen.lists[key].items, key)
        screen.set_tab(0)
        self.win.calls.clear()
        screen.draw()
        self.assertIn("partial (interrupted)", self.drawn_text())

    def test_reenter_does_not_reload(self) -> None:
        screen = self.make_screen()
        screen.on_enter()
        self.assertEqual(self.rec.collected, 1)

    def test_small_terminal(self) -> None:
        self.win = MockCursesWindow(10, 40)
        screen = self.make_screen()
        screen.draw()
        self.assertIn("too small", self.drawn_text())
        self.win = MockCursesWindow(12, 60)
        screen.stdscr = self.win
        for i in range(len(TABS)):
            screen.set_tab(i)
            screen.draw()

    def test_tables_tab_totals_and_missing(self) -> None:
        screen = self.make_screen()
        screen.set_tab(3)
        screen.draw()
        text = self.drawn_text()
        self.assertIn("2 tables", text)
        self.assertIn("1 missing", text)
        self.assertIn("(not in schema)", text)
        self.assertIn("missing from database", text)

    def test_feeds_tab_rows(self) -> None:
        screen = self.make_screen()
        items = screen.items_for(TAB_FEEDS)
        self.assertEqual(len(items), 5)
        screen.set_tab(2)
        screen.draw()
        text = self.drawn_text()
        self.assertIn("mailing list: present", text)
        self.assertIn("cvelistV5", text)
        self.assertIn("missing", text)

    def test_load_failure_shows_error_and_esc_pops(self) -> None:
        rec = Recorder(fail=ConnectionError("refused"))
        screen = self.make_screen(rec=rec)
        self.assertIsNone(screen.snapshot)
        self.assertIn("refused", screen.load_error or "")
        screen.draw()
        self.assertIn("refused", self.drawn_text())
        self.assertTrue(screen.handle_key(ENTER))  # nothing selected: no crash
        self.assertTrue(screen.handle_key(ESC))
        self.assertEqual(self.app.popped, 1)

    def test_section_errors_listed(self) -> None:
        screen = self.make_screen(rec=Recorder([make_snapshot(errors=["CVE: boom"])]))
        screen.draw()
        self.assertIn("1 error(s)", self.drawn_text())
        screen.handle_key(ord("e"))
        self.assertIn("CVE: boom", self.dialogs[-1][1])

    def test_details_dialog(self) -> None:
        screen = self.make_screen()
        screen.handle_key(curses.KEY_DOWN)
        screen.handle_key(ENTER)
        self.assertEqual(self.dialogs[-1][0], "v3.1")
        screen.set_tab(1)
        screen.handle_key(ENTER)
        self.assertIn("master", self.dialogs[-1][1])


class TestNavigation(DBContentTestBase):
    def test_tab_switching_keys(self) -> None:
        screen = self.make_screen()
        self.assertEqual(screen.tab, TAB_VERSIONS)
        screen.handle_key(TAB)
        self.assertEqual(screen.tab, TAB_REPOS)
        screen.handle_key(curses.KEY_RIGHT)
        self.assertEqual(screen.tab, TAB_FEEDS)
        screen.handle_key(curses.KEY_BTAB)
        self.assertEqual(screen.tab, TAB_REPOS)
        screen.handle_key(curses.KEY_LEFT)
        screen.handle_key(curses.KEY_LEFT)
        self.assertEqual(screen.tab, TAB_TABLES)
        screen.handle_key(ord("1"))
        self.assertEqual(screen.tab, TAB_VERSIONS)
        screen.handle_key(ord("4"))
        self.assertEqual(screen.tab, TAB_TABLES)

    def test_filter_cycles(self) -> None:
        screen = self.make_screen()
        screen.set_tab(3)
        seen = []
        for _ in range(4):
            screen.handle_key(ord("f"))
            seen.append((screen.filter_mode, len(screen.lists[TAB_VERSIONS].items)))
        self.assertEqual(screen.tab, TAB_VERSIONS)
        self.assertEqual(seen, [(FILTER_PARTIAL, 5), (FILTER_PARSED, 1), (FILTER_NEW, 1), (FILTER_ALL, 7)])

    def test_filter_versions_helper(self) -> None:
        versions = make_snapshot().versions
        self.assertEqual({v.state for v in filter_versions(versions, FILTER_PARTIAL)},
                         {STATE_INTERRUPTED, STATE_IN_PROGRESS, STATE_LEGACY})

    def test_view_state_survives_new_instance(self) -> None:
        screen = self.make_screen()
        screen.handle_key(ord("f"))
        screen.handle_key(ord("3"))
        screen.handle_key(ESC)
        again = self.make_screen()
        self.assertEqual((again.tab, again.filter_mode), (TAB_FEEDS, FILTER_PARTIAL))

    def test_list_navigation(self) -> None:
        screen = self.make_screen()
        lst = screen.lists[TAB_VERSIONS]
        screen.handle_key(curses.KEY_END)
        self.assertEqual(lst.selected_index, len(lst.items) - 1)
        screen.handle_key(curses.KEY_HOME)
        self.assertEqual(lst.selected_index, 0)
        screen.handle_key(curses.KEY_NPAGE)
        screen.handle_key(curses.KEY_UP)
        screen.draw()


class TestClean(DBContentTestBase):
    def test_cancel_at_preview(self) -> None:
        screen = self.make_screen()
        screen.set_tab(3)  # not on Versions: no per-version choice
        self.answers = ["Cancel"]
        self.assertFalse(screen.clean_partial())
        self.assertEqual(self.dialogs[-1][0], "Clean Preview")
        self.assertIn("commit_id 11", self.dialogs[-1][1])
        self.assertIn("commit_id 15", self.dialogs[-1][1])
        self.assertNotIn("commit_id 12", self.dialogs[-1][1])  # in progress
        self.assertEqual(self.rec.cleaned, [])

    def test_wrong_confirmation_text(self) -> None:
        screen = self.make_screen(prompt_answer="clean ")
        screen.set_tab(3)
        self.answers = ["Clean"]
        self.assertFalse(screen.clean_partial())
        self.assertEqual(self.dialogs[-1][0], "Clean Aborted")
        self.assertEqual(self.rec.cleaned, [])

    def test_confirmed_clean_runs_and_reloads(self) -> None:
        after = make_snapshot()
        after.versions = [v for v in after.versions if v.state != STATE_INTERRUPTED]
        self.rec.snapshots = [make_snapshot(), after]
        screen = self.make_screen()
        screen.set_tab(3)
        self.answers = ["Clean"]
        self.assertTrue(screen.handle_key(ord("c")))
        self.assertEqual(len(self.rec.cleaned), 1)
        self.assertEqual([v.commit_id for v in self.rec.cleaned[0]], [11, 15])
        self.assertEqual(self.dialogs[-1][0], "Clean Complete")
        self.assertEqual(self.rec.collected, 2)
        self.assertEqual(screen.snapshot, after)

    def test_this_version_choice(self) -> None:
        screen = self.make_screen()
        screen.handle_key(curses.KEY_DOWN)  # v3.1, interrupted
        self.answers = ["This version", "Clean"]
        self.assertTrue(screen.clean_partial())
        self.assertEqual(self.dialogs[0][2], ["This version", "All interrupted (2)", "Cancel"])
        self.assertEqual([v.commit_id for v in self.rec.cleaned[0]], [11])

    def test_choice_cancel(self) -> None:
        screen = self.make_screen()
        screen.handle_key(curses.KEY_DOWN)
        self.answers = ["Cancel"]
        self.assertFalse(screen.clean_partial())
        self.assertEqual(self.rec.cleaned, [])

    def test_nothing_to_clean(self) -> None:
        snap = make_snapshot()
        snap.versions = [v for v in snap.versions if v.state != STATE_INTERRUPTED]
        screen = self.make_screen(rec=Recorder([snap]))
        self.assertFalse(screen.clean_partial())
        self.assertEqual(self.dialogs[-1][0], "Nothing to Clean")
        self.assertIn("never cleaned", self.dialogs[-1][1])

    def test_clean_failure_reported(self) -> None:
        screen = self.make_screen()
        screen.set_tab(3)

        def boom(db: Any, versions: list[VersionRow]) -> list[CleanResult]:
            raise RuntimeError("lock wait timeout")

        screen.cleaner = boom
        self.answers = ["Clean"]
        self.assertFalse(screen.clean_partial())
        self.assertTrue(any(t == "Clean Failed" and "lock wait" in m for t, m, _b in self.dialogs))


class TestMainMenuEntry(unittest.TestCase):
    def test_menu_item_9_opens_viewer(self) -> None:
        win = MockCursesWindow(30, 100)
        app = MockApp()
        menu = MainMenuScreen(win, app)
        self.assertIn("9", menu.HOTKEYS.values())
        self.assertTrue(menu.handle_key(ord("9")))
        self.assertIsInstance(app.pushed[-1], DBContentScreen)

    def test_screen_module_exports(self) -> None:
        import core.tui
        import core.tui.screens

        self.assertIs(core.tui.DBContentScreen, DBContentScreen)
        self.assertIs(core.tui.screens.DBContentScreen, DBContentScreen)
        self.assertIs(dbc.DBContentScreen, DBContentScreen)


if __name__ == "__main__":
    unittest.main()
