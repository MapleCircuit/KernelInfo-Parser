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


@router.get("/kconfig/{version_name}/fragments")
def get_fragments(version_name: str, arch: str = Query(DEFAULT_ARCH)) -> dict[str, Any]:
    """Configuration fragments of the revision (K2): kernel/configs/*.config and arch/$SRCARCH/configs/*.config."""
    return kconfig_service.list_fragments(version_name, arch)


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


# ------------------------------------------------------------------ Kbuild object index (S04)
def _kbuild_job(state: dict[str, Any]) -> Any:
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse
    from webapp.backend.services.jobs import retry_after_ms
    if state.get("state") == "failed":
        raise HTTPException(status_code=500, detail=f"Kbuild index failed: {state.get('error')}")
    if state.get("state") != "done":
        return JSONResponse({"state": "building", "progress": state.get("progress") or {}, "retry_after_ms": retry_after_ms(state)},
                            status_code=202, headers={"Cache-Control": "no-store"})
    return None


@router.get("/kconfig/{version_name}/kbuild/index")
def get_kbuild_index(version_name: str, arch: Annotated[str, Query()] = DEFAULT_ARCH) -> Any:
    """Every object the Kbuild files name for one ARCH (S04): modname, kind, condition (interned), member
    sources. 202 while the index builds; ETag = cache key."""
    from fastapi.responses import JSONResponse
    from webapp.backend.security.git import safe_rev
    from webapp.backend.services.kbuild_index_service import compact, index_state
    safe_rev(version_name)
    st = index_state(version_name, arch, wait=2.0)
    building = _kbuild_job(st)
    if building is not None:
        return building
    return JSONResponse(compact(st["result"]), headers={"ETag": f'W/"{abs(hash(st["cache_key"])):x}"', "Cache-Control": "private, max-age=3600"})


@router.post("/kconfig/{version_name}/kbuild/modules")
def post_kbuild_modules(version_name: str, payload: dict[str, Any] = Body(...)) -> Any:
    """Objects building the named modules (at most 2000; dashes and underscores are the same) with the
    symbols each requires: {modules: {name: {objects, required_symbols}}, unmatched}; `"gates": true` adds
    `module_gates`, every symbol gating a module-capable object (K1 trim candidates)."""
    from fastapi import HTTPException
    from webapp.backend.security.git import safe_rev
    from webapp.backend.services.kbuild_index_service import index_state, module_gates, objects_for_module
    safe_rev(version_name)
    names = payload.get("names") if isinstance(payload, dict) else None
    if not isinstance(names, list) or len(names) > 2000 or not all(isinstance(n, str) and 0 < len(n) <= 64 for n in names):
        raise HTTPException(status_code=400, detail="names must be a list of at most 2000 module names")
    st = index_state(version_name, str(payload.get("arch") or DEFAULT_ARCH), wait=2.0)
    building = _kbuild_job(st)
    if building is not None:
        return building
    out = {"version": version_name, "arch": st["result"]["arch"], **objects_for_module(st["result"], names)}
    if payload.get("gates"):
        out["module_gates"] = module_gates(st["result"])
    return out


@router.get("/kconfig/{version_name}/kbuild/file")
def get_kbuild_file_objects(version_name: str, path: Annotated[str, Query()] = "", arch: Annotated[str, Query()] = DEFAULT_ARCH) -> Any:
    """Objects one source file is part of, with their conditions and rules."""
    from webapp.backend.security.git import safe_rev
    from webapp.backend.services.kbuild_index_service import index_state, objects_for_file
    safe_rev(version_name)
    st = index_state(version_name, arch, wait=2.0)
    building = _kbuild_job(st)
    if building is not None:
        return building
    return {"version": version_name, "arch": st["result"]["arch"], "path": path, "objects": objects_for_file(st["result"], path)}
