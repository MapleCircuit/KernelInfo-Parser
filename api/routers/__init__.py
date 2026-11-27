"""api/routers/__init__.py - AI API routers."""
from api.routers.oauth import router as oauth_router
from api.routers.v1 import router as v1_router

__all__ = ["oauth_router", "v1_router"]
