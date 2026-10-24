"""webapp/backend/routers/kconfig.py - Kconfig models, defconfigs, symbol provenance and constraint tools."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query, Body
from webapp.backend.services.kconfig_service import DEFAULT_ARCH, KconfigService

router = APIRouter(prefix="/api", tags=["kconfig"])
kconfig_service = KconfigService()


@router.get("/kconfig/tree")
@router.get("/kconfig/{version_name}/tree")
def get_kconfig_tree(
    version: str = Query("v3.0"),
    arch: str = Query(DEFAULT_ARCH),
    version_name: str | None = None,
    include_tree: bool = Query(False),
    include_relations: bool = Query(False),
    include_nodes: bool = Query(False),
    toolchain: str | None = Query(None, description="Toolchain profile (JSON) answering Kconfig $(shell,...) calls"),
) -> dict[str, Any]:
    """Finalized Kconfig model (menus, symbols, expressions) for one ARCH and toolchain, evaluated client-side."""
    v = version_name or version
    return kconfig_service.get_tree(v, arch, include_tree=include_tree, include_relations=include_relations,
                                    include_nodes=include_nodes, toolchain=toolchain)


@router.get("/kconfig/{version_name}/toolchain")
@router.get("/version/{version_name}/kconfig/toolchain")
def get_kconfig_toolchain_defaults(version_name: str, arch: str = Query(DEFAULT_ARCH)) -> dict[str, Any]:
    """Default gcc/clang toolchain profiles of a revision and whether its Kconfig uses $(...) macros."""
    return kconfig_service.toolchain_defaults(version_name, arch)


@router.get("/kconfig/symbol")
@router.get("/version/{version_name}/kconfig/symbol")
@router.get("/kconfig/{version_name}/symbol/{symbol_name}")
@router.get("/version/{version_name}/kconfig/symbol/{symbol_name}")
def get_kconfig_symbol(
    version: str = Query("v3.0"),
    name: str = Query(None),
    version_name: str | None = None,
    symbol_name: str | None = None,
    arch: str = Query(DEFAULT_ARCH),
    toolchain: str | None = Query(None, description="Toolchain profile (JSON) answering Kconfig $(shell,...) calls"),
) -> dict[str, Any]:
    """Arch-scoped definitions (file:line), dependencies, reverse dependencies, help and compiled files."""
    v = version_name or version
    sym = symbol_name or name
    return kconfig_service.get_symbol_detail(v, sym, arch, toolchain)


@router.get("/kconfig/{version_name}/symbol/{symbol_name}/usage")
@router.get("/version/{version_name}/kconfig/symbol/{symbol_name}/usage")
def get_kconfig_symbol_usage(
    version_name: str,
    symbol_name: str,
    arch: str = Query(DEFAULT_ARCH),
    limit: int = Query(400, ge=1, le=2000),
    toolchain: str | None = Query(None, description="Toolchain profile (JSON) answering Kconfig $(shell,...) calls"),
) -> dict[str, Any]:
    """Where CONFIG_<symbol> is used: Kbuild rules, C/ASM/linker sources and Kconfig references."""
    return kconfig_service.get_symbol_usage(version_name, symbol_name, arch, limit, toolchain)


@router.get("/version/{version_name}/kconfig/defconfigs")
@router.get("/kconfig/defconfigs")
@router.get("/kconfig/{version_name}/defconfigs")
def get_defconfigs(
    version_name: str = "v3.0",
    version: str | None = None,
    arch: str = Query(DEFAULT_ARCH),
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
    arch: str = Query(DEFAULT_ARCH),
    toolchain: str | None = Query(None, description="Toolchain profile (JSON) answering Kconfig $(shell,...) calls"),
) -> dict[str, Any]:
    """Raw defconfig text and its literal assignments (empty name: the arch's default defconfig)."""
    target_ver = version or version_name
    target_p = defconfig or file_path or ""
    return kconfig_service.get_defconfig_content(target_ver, target_p, arch, toolchain=toolchain)


@router.get("/kconfig/graph")
@router.get("/kconfig/{version_name}/graph")
@router.get("/version/{version_name}/kconfig/graph")
@router.get("/version/{version_name}/kconfig/graph/{symbol_name}")
def get_kconfig_graph(
    version: str = Query("v3.0"),
    arch: str = Query(DEFAULT_ARCH),
    version_name: str | None = None,
    symbol: str | None = Query(None),
    symbol_name: str | None = None,
    depth: int = Query(2),
    toolchain: str | None = Query(None, description="Toolchain profile (JSON) answering Kconfig $(shell,...) calls"),
) -> dict[str, Any]:
    """Direct relations of a symbol (own dependencies, selects, implies, selected by)."""
    v = version_name or version
    return kconfig_service.get_graph(v, symbol_name or symbol or "", depth, arch, toolchain)


@router.post("/kconfig/{version_name}/autosolve")
@router.post("/version/{version_name}/kconfig/autosolve")
def autosolve_kconfig(
    payload: dict[str, Any] = Body(...),
    version_name: str = "v3.0",
) -> dict[str, Any]:
    """Legal user changes that make `target_symbol` reach `value` (default y) from `current_values`."""
    return kconfig_service.autosolve(version_name, payload)


@router.get("/kconfig/{version_name}/autosolve/{symbol}")
@router.get("/version/{version_name}/kconfig/autosolve/{symbol}")
def autosolve_kconfig_get(
    symbol: str,
    version_name: str = "v3.0",
    arch: str = Query(DEFAULT_ARCH),
) -> dict[str, Any]:
    """Prerequisite changes for a single symbol starting from an empty (all-default) configuration."""
    return kconfig_service.autosolve(version_name, {"target_symbol": symbol, "arch": arch})


@router.get("/kconfig/{version_name}/presets")
@router.get("/version/{version_name}/kconfig/presets")
@router.get("/version/{version_name}/kconfig/env-presets")
def get_kconfig_presets(version_name: str = "v3.0") -> dict[str, Any]:
    """ARCH values accepted by this revision, with their kconfig environment and default defconfig."""
    return kconfig_service.get_env_presets(version_name)


@router.get("/kconfig/{version_name}/search")
@router.get("/version/{version_name}/kconfig/search")
def search_kconfig_symbols(
    version_name: str = "v3.0",
    q: str = Query(""),
    limit: int = Query(25),
    arch: str = Query(DEFAULT_ARCH),
    toolchain: str | None = Query(None, description="Toolchain profile (JSON) answering Kconfig $(shell,...) calls"),
) -> dict[str, Any]:
    """Search Kconfig symbols by name or prompt."""
    return kconfig_service.search_symbols(version_name, q=q, limit=limit, arch=arch, toolchain=toolchain)


@router.post("/kconfig/{version_name}/import")
@router.post("/version/{version_name}/kconfig/import")
def import_kconfig(
    payload: dict[str, Any] = Body(...),
    version_name: str = "v3.0",
) -> dict[str, Any]:
    """Parse raw configuration text into assignments (with `arch`: unknown/invalid entries reported)."""
    return kconfig_service.import_file(version_name, payload)


@router.post("/kconfig/{version_name}/export")
@router.post("/version/{version_name}/kconfig/export")
def export_kconfig(
    payload: dict[str, Any] = Body(...),
    version_name: str = "v3.0",
) -> dict[str, Any]:
    """Format assignments as .config (with `arch`: resolved by kconfig; `format`: config | defconfig)."""
    return kconfig_service.export_file(version_name, payload)


@router.post("/kconfig/{version_name}/validate")
@router.post("/version/{version_name}/kconfig/validate")
def validate_kconfig(
    payload: dict[str, Any] = Body(...),
    version_name: str = "v3.0",
) -> dict[str, Any]:
    """Resolve assignments through kconfig: forced, adjusted, unknown and unmet-dependency entries."""
    return kconfig_service.validate_assignments(version_name, payload)


@router.post("/kconfig/{version_name}/diff-config")
@router.post("/version/{version_name}/kconfig/diff-config")
def diff_kconfig_configs(
    payload: dict[str, Any] = Body(...),
    version_name: str = "v3.0",
) -> dict[str, Any]:
    """Compare two configurations ({active_config, custom_config})."""
    return kconfig_service.diff_configurations(version_name, payload)
