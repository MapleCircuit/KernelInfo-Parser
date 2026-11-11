"""TUI screens package."""

from core.tui.screens.base import BaseScreen
from core.tui.screens.cve_ml import CVEMailingListScreen
from core.tui.screens.database_ssh import DatabaseSSHScreen
from core.tui.screens.db_content import DBContentScreen
from core.tui.screens.diagnostics import DiagnosticsScreen
from core.tui.screens.git_manager import GitManagerScreen
from core.tui.screens.git_select import GitRepoSelectScreen
from core.tui.screens.main_menu import MainMenuScreen
from core.tui.screens.parser_dist import ParserDistributedScreen
from core.tui.screens.repos import RepoDetailScreen, RepositoriesScreen
from core.tui.screens.version_parse import VersionParseScreen

__all__ = [
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
    "GitRepoSelectScreen",
    "VersionParseScreen",
]
