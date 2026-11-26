"""webapp/backend/routers - FastAPI Router Registry."""
from webapp.backend.routers.commits import router as commits_router
from webapp.backend.routers.cve import router as cve_router
from webapp.backend.routers.docs import router as docs_router
from webapp.backend.routers.filesystem import router as filesystem_router
from webapp.backend.routers.insights import router as insights_router
from webapp.backend.routers.kapi import router as kapi_router
from webapp.backend.routers.kconfig import router as kconfig_router
from webapp.backend.routers.mail import router as mail_router
from webapp.backend.routers.maintainers import router as maintainers_router
from webapp.backend.routers.man import router as man_router
from webapp.backend.routers.nodemap import router as nodemap_router
from webapp.backend.routers.symbols import router as symbols_router
from webapp.backend.routers.system import sync_router
from webapp.backend.routers.tools import router as tools_router
from webapp.backend.routers.versions import router as versions_router
from webapp.backend.routers.watch import router as watch_router

__all__ = [
    "commits_router",
    "cve_router",
    "docs_router",
    "filesystem_router",
    "insights_router",
    "kapi_router",
    "kconfig_router",
    "mail_router",
    "maintainers_router",
    "man_router",
    "nodemap_router",
    "symbols_router",
    "sync_router",
    "tools_router",
    "versions_router",
    "watch_router",
]

