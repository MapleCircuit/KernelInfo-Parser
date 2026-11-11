"""Curses TUI (Text User Interface) foundation package."""

from core.tui.app import TUIApp, run_config_tui, run_config_tui_launch
from core.tui.screens.base import BaseScreen
from core.tui.screens.cve_ml import CVEMailingListScreen
from core.tui.screens.database_ssh import DatabaseSSHScreen
from core.tui.screens.db_content import DBContentScreen
from core.tui.screens.diagnostics import DiagnosticsScreen
from core.tui.screens.git_manager import GitManagerScreen
from core.tui.screens.main_menu import MainMenuScreen
from core.tui.screens.parser_dist import ParserDistributedScreen
from core.tui.screens.repos import RepoDetailScreen, RepositoriesScreen
from core.tui.screens.version_parse import VersionParseScreen
from core.tui.testing import (
    DiagnosticResult,
    run_all_diagnostics,
    test_cve_catalog,
    test_database_connection,
    test_mailing_list,
    test_repository,
    test_ssh_tunnel,
)
from core.tui.theme import (
    PAIR_ACTIVE,
    PAIR_BORDER,
    PAIR_DEFAULT,
    PAIR_ERROR,
    PAIR_HEADER,
    PAIR_HIGHLIGHT,
    PAIR_MUTED,
    PAIR_SUCCESS,
    draw_box,
    get_color,
    init_theme,
    safe_addstr,
)
from core.tui.widgets import (
    Checkbox,
    ModalDialog,
    ScrollableList,
    SelectList,
    StatusBar,
    TextInput,
)

__all__ = [
    # Application Controller & Entrypoint
    "TUIApp",
    "run_config_tui",
    "run_config_tui_launch",
    # Color Pair Identifiers
    "PAIR_DEFAULT",
    "PAIR_HEADER",
    "PAIR_ACTIVE",
    "PAIR_ERROR",
    "PAIR_SUCCESS",
    "PAIR_MUTED",
    "PAIR_HIGHLIGHT",
    "PAIR_BORDER",
    # Theme & Drawing Primitives
    "init_theme",
    "get_color",
    "safe_addstr",
    "draw_box",
    # Screens
    "BaseScreen",
    "MainMenuScreen",
    "DatabaseSSHScreen",
    "DBContentScreen",
    "RepositoriesScreen",
    "RepoDetailScreen",
    "ParserDistributedScreen",
    "CVEMailingListScreen",
    "DiagnosticsScreen",
    "GitManagerScreen",
    "VersionParseScreen",
    # Widgets
    "TextInput",
    "Checkbox",
    "SelectList",
    "ScrollableList",
    "StatusBar",
    "ModalDialog",
    # Diagnostic Testing Engine
    "DiagnosticResult",
    "test_ssh_tunnel",
    "test_database_connection",
    "test_repository",
    "test_cve_catalog",
    "test_mailing_list",
    "run_all_diagnostics",
]
