"""webapp/backend/routers/kconfig.py - KConfig Architecture Defconfigs & Dependency Graph."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query, Body
from webapp.backend.services.kconfig_service import KconfigService

router = APIRouter(prefix="/api", tags=["kconfig"])
kconfig_service = KconfigService()


@router.get("/kconfig/tree")
@router.get("/kconfig/{version_name}/tree")
def get_kconfig_tree(
    version: str = Query("v3.0"),
    arch: str = Query("x86"),
    version_name: str | None = None,
    include_tree: bool = Query(False),
    include_relations: bool = Query(False),
) -> dict[str, Any]:
    """Retrieve hierarchical Menuconfig tree scoped by architecture."""
    v = version_name or version
    return kconfig_service.get_tree(v, arch, include_tree=include_tree, include_relations=include_relations)


@router.get("/kconfig/symbol")
@router.get("/kconfig/{version_name}/symbol/{symbol_name}")
def get_kconfig_symbol(version: str = Query("v3.0"), name: str = Query(None), version_name: str | None = None, symbol_name: str | None = None) -> dict[str, Any]:
    """Retrieve symbol details, depends_on expressions, and reverse selects."""
    v = version_name or version
    sym = symbol_name or name
    return kconfig_service.get_symbol_detail(v, sym)


@router.get("/version/{version_name}/kconfig/defconfigs")
@router.get("/kconfig/defconfigs")
@router.get("/kconfig/{version_name}/defconfigs")
def get_defconfigs(
    version_name: str = "v3.0",
    version: str | None = None,
    arch: str = Query("x86"),
) -> dict[str, Any]:
    """Retrieve all available defconfigs for an architecture."""
    target_ver = version or version_name
    return kconfig_service.get_defconfigs(target_ver, arch)


@router.get("/version/{version_name}/kconfig/defconfig/content")
@router.get("/kconfig/defconfig/content")
@router.get("/kconfig/{version_name}/defconfig")
def get_defconfig_content(
    version_name: str = "v3.0",
    version: str | None = None,
    defconfig: str | None = None,
    file_path: str | None = None,
    arch: str = Query("x86"),
) -> dict[str, Any]:
    """Parse defconfig into key-value pairs."""
    target_ver = version or version_name
    target_p = defconfig or file_path or "x86_64_defconfig"
    return kconfig_service.get_defconfig_content(target_ver, target_p, arch)


@router.get("/kconfig/graph")
@router.get("/kconfig/{version_name}/graph")
def get_kconfig_graph(
    version: str = Query("v3.0"),
    arch: str = Query("x86"),
    version_name: str | None = None,
    symbol: str | None = Query(None),
    depth: int = Query(2),
) -> dict[str, Any]:
    """Extract full architecture dependency graph for client-side engine caching."""
    v = version_name or version
    sym_or_arch = symbol or arch
    return kconfig_service.get_graph(v, sym_or_arch, depth)


@router.post("/kconfig/{version_name}/autosolve")
@router.post("/version/{version_name}/kconfig/autosolve")
def autosolve_kconfig(
    payload: dict[str, Any] = Body(...),
    version_name: str = "v3.0",
) -> dict[str, Any]:
    """Evaluate prerequisite dependency trees and compute minimal symbol toggles."""
    return kconfig_service.autosolve(version_name, payload)


@router.get("/kconfig/{version_name}/autosolve/{symbol}")
@router.get("/version/{version_name}/kconfig/autosolve/{symbol}")
def autosolve_kconfig_get(
    symbol: str,
    version_name: str = "v3.0",
) -> dict[str, Any]:
    """Evaluate prerequisite dependency trees and compute minimal symbol toggles for a single symbol."""
    return kconfig_service.autosolve(version_name, {"target_symbol": symbol})


@router.post("/kconfig/{version_name}/diff")
@router.post("/version/{version_name}/kconfig/diff")
def diff_kconfig(
    payload: dict[str, Any] = Body(...),
    version_name: str = "v3.0",
) -> dict[str, Any]:
    """Compare active vs target Kconfig symbol values."""
    return kconfig_service.diff_configurations(version_name, payload)


@router.get("/kconfig/{version_name}/presets")
@router.get("/version/{version_name}/kconfig/presets")
def get_kconfig_presets(version_name: str = "v3.0") -> dict[str, Any]:
    """Retrieve preconfigured environment target presets (e.g. Tiny, Server, Security)."""
    return kconfig_service.get_env_presets(version_name)
