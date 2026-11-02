"""webapp/backend/routers/kconfig.py - Kconfig models, defconfigs, symbol provenance and the auto-solver."""
from __future__ import annotations
from typing import Annotated, Any
from fastapi import APIRouter, Query, Body
from webapp.backend.services.kconfig_service import DEFAULT_ARCH, KconfigService
from webapp.backend.services.history_service import history_service

router = APIRouter(prefix="/api", tags=["kconfig"])
kconfig_service = KconfigService()

ToolchainParam = Annotated[str | None, Query(description="Toolchain profile (JSON) answering Kconfig $(shell,...) calls")]


@router.get("/kconfig/{version_name}/tree")
def get_kconfig_tree(version_name: str, arch: str = Query(DEFAULT_ARCH), toolchain: ToolchainParam = None) -> dict[str, Any]:
    """Finalized Kconfig model (menus, symbols, expressions) for one ARCH and toolchain, evaluated client-side."""
    return kconfig_service.get_tree(version_name, arch, toolchain=toolchain)


@router.get("/kconfig/{version_name}/toolchain")
def get_kconfig_toolchain_defaults(version_name: str, arch: str = Query(DEFAULT_ARCH)) -> dict[str, Any]:
    """Default gcc/clang toolchain profiles of a revision and whether its Kconfig uses $(...) macros."""
    return kconfig_service.toolchain_defaults(version_name, arch)


@router.get("/kconfig/{version_name}/symbol/{symbol_name}")
def get_kconfig_symbol(
    version_name: str,
    symbol_name: str,
    arch: str = Query(DEFAULT_ARCH),
    toolchain: ToolchainParam = None,
) -> dict[str, Any]:
    """Arch-scoped definitions (file:line), dependencies, reverse dependencies, help and compiled files."""
    return kconfig_service.get_symbol_detail(version_name, symbol_name, arch, toolchain)


@router.get("/kconfig/{version_name}/symbol/{symbol_name}/usage")
def get_kconfig_symbol_usage(
    version_name: str,
    symbol_name: str,
    arch: str = Query(DEFAULT_ARCH),
    limit: int = Query(400, ge=1, le=2000),
    toolchain: ToolchainParam = None,
) -> dict[str, Any]:
    """Where CONFIG_<symbol> is used: Kbuild rules, C/ASM/linker sources and Kconfig references."""
    return kconfig_service.get_symbol_usage(version_name, symbol_name, arch, limit, toolchain)


@router.get("/kconfig/{version_name}/build")
def get_build_conditions(
    version_name: str,
    path: list[str] = Query([], description="Source paths (repeatable, up to 64)"),
    arch: str = Query(DEFAULT_ARCH),
) -> dict[str, Any]:
    """Kbuild condition per file: when `make ARCH=<arch>` compiles it, as an expression over CONFIG_* symbols."""
    return kconfig_service.build_conditions(version_name, path, arch)


@router.get("/kconfig/{version_name}/symbol/{symbol_name}/history")
def get_kconfig_symbol_history(
    version_name: str,
    symbol_name: str,
    include_rc: bool = Query(False, description="Also list release candidates"),
    branch: str | None = Query(None, description="Branch the timeline follows (default: chosen for version_name)"),
) -> dict[str, Any]:
    """Release-by-release history of every definition of CONFIG_<symbol>, with property-level changes."""
    return history_service.get_kconfig_history(symbol_name, version_name, include_rc, branch=branch or None)


@router.get("/kconfig/{version_name}/defconfigs")
def get_defconfigs(version_name: str, arch: str = Query(DEFAULT_ARCH)) -> dict[str, Any]:
    """Every defconfig available for an architecture."""
    return kconfig_service.get_defconfigs(version_name, arch)


@router.get("/kconfig/{version_name}/defconfig")
def get_defconfig_content(
    version_name: str,
    file_path: str = Query(""),
    arch: str = Query(DEFAULT_ARCH),
    toolchain: ToolchainParam = None,
) -> dict[str, Any]:
    """Raw defconfig text and its literal assignments (empty name: the arch's default defconfig)."""
    return kconfig_service.get_defconfig_content(version_name, file_path, arch, toolchain=toolchain)


@router.get("/kconfig/{version_name}/graph")
def get_kconfig_graph(
    version_name: str,
    arch: str = Query(DEFAULT_ARCH),
    symbol: str = Query(""),
    toolchain: ToolchainParam = None,
) -> dict[str, Any]:
    """Direct relations of a symbol (own dependencies, selects, implies, selected by)."""
    return kconfig_service.get_graph(version_name, symbol, arch=arch, toolchain=toolchain)


@router.post("/kconfig/{version_name}/autosolve")
def autosolve_kconfig(version_name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Legal user changes that make `target_symbol` reach `value` (default y) from `current_values`."""
    return kconfig_service.autosolve(version_name, payload)


@router.get("/kconfig/{version_name}/presets")
def get_kconfig_presets(version_name: str) -> dict[str, Any]:
    """ARCH values accepted by this revision, with their kconfig environment and default defconfig."""
    return kconfig_service.get_env_presets(version_name)


@router.get("/kconfig/{version_name}/search")
def search_kconfig_symbols(
    version_name: str,
    q: str = Query(""),
    limit: int = Query(25, ge=1, le=500),
    arch: str = Query(DEFAULT_ARCH),
    toolchain: ToolchainParam = None,
) -> dict[str, Any]:
    """Search Kconfig symbols by name or prompt."""
    return kconfig_service.search_symbols(version_name, q=q, limit=limit, arch=arch, toolchain=toolchain)
