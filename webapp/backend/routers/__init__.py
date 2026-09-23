"""webapp/backend/routers - FastAPI Router Registry."""
from webapp.backend.routers.versions import router as versions_router
from webapp.backend.routers.filesystem import router as filesystem_router
from webapp.backend.routers.symbols import router as symbols_router
from webapp.backend.routers.kconfig import router as kconfig_router
from webapp.backend.routers.maintainers import router as maintainers_router
from webapp.backend.routers.commits import router as commits_router
from webapp.backend.routers.tools import router as tools_router
from webapp.backend.routers.system import router as system_router

__all__ = [
    "versions_router",
    "filesystem_router",
    "symbols_router",
    "kconfig_router",
    "maintainers_router",
    "commits_router",
    "tools_router",
    "system_router",
]
