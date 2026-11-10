"""webapp/backend/routers - FastAPI Router Registry."""
from webapp.backend.routers.versions import router as versions_router
from webapp.backend.routers.filesystem import router as filesystem_router
from webapp.backend.routers.symbols import router as symbols_router
from webapp.backend.routers.kconfig import router as kconfig_router
from webapp.backend.routers.maintainers import router as maintainers_router
from webapp.backend.routers.commits import router as commits_router
from webapp.backend.routers.tools import router as tools_router
from webapp.backend.routers.system import sync_router
from webapp.backend.routers.docs import router as docs_router
from webapp.backend.routers.mail import router as mail_router
from webapp.backend.routers.insights import router as insights_router
from webapp.backend.routers.cve import router as cve_router
from webapp.backend.routers.kapi import router as kapi_router
from webapp.backend.routers.watch import router as watch_router

__all__ = [
    "versions_router",
    "filesystem_router",
    "symbols_router",
    "kconfig_router",
    "maintainers_router",
    "commits_router",
    "tools_router",
    "sync_router",
    "docs_router",
    "mail_router",
    "insights_router",
    "cve_router",
    "kapi_router",
    "watch_router",
]

