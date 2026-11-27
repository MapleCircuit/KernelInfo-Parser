"""api/routers/kconfig.py - Kconfig search, symbol detail, build conditions, evaluator, graph, solver, defconfigs, history, and Kbuild endpoints."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.config import (
    get_active_repo_name,
    require_feature,
    resolve_version,
)
from api.routers.common import (
    DEFAULT_ARCH,
    _MAX_SEARCH_OFFSET,
    _kconfig_service,
    _validate_identifier,
    _validate_rel_path,
    _validate_search_offset,
    _with_repo,
)
from webapp.backend.routers.insights import job_answer
from webapp.backend.security.git import safe_rev
from webapp.backend.services import kbuild_index_service
from webapp.backend.services.history_service import history_service

router = APIRouter()


def _parse_assignments(assign: list[str], *, max_items: int = 128) -> dict[str, str]:
    if len(assign) > max_items:
        raise HTTPException(status_code=400, detail=f"At most {max_items} assignments allowed.")
    assignments: dict[str, str] = {}
    for item in assign:
        name, sep, value = item.partition("=")
        if not sep or len(value) > 256:
            raise HTTPException(status_code=400, detail=f"Assignment must be NAME=value: {item[:64]!r}")
        assignments[_validate_identifier(name.strip().removeprefix("CONFIG_"), "kconfig symbol")] = value.strip()
    return assignments


@router.get("/kconfig/search", dependencies=[Depends(require_feature("kconfig"))])
def search_kconfig(
    q: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Search Kconfig symbols by name or prompt."""
    _validate_search_offset(offset, _MAX_SEARCH_OFFSET)
    ver = resolve_version(version)
    fetch_cap = min(_MAX_SEARCH_OFFSET, offset + limit)
    res = _kconfig_service.search_symbols(ver, q=q, limit=fetch_cap, arch=arch)
    items = res.get("results") or res.get("symbols") or []
    total = int(res.get("total") or len(items))
    sliced = items[offset : offset + limit]
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "arch": res.get("arch", arch),
        "query": q,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": (offset + len(sliced)) < total,
        "symbols": sliced,
    }


@router.get("/kconfig/symbol/{symbol_name}", dependencies=[Depends(require_feature("kconfig"))])
def get_kconfig_symbol(
    symbol_name: str,
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
    include_usage: bool = False,
    usage_limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Kconfig symbol definition, dependencies, help, and optional usage."""
    ver = resolve_version(version)
    ident = _validate_identifier(symbol_name, "kconfig symbol")
    detail = _kconfig_service.get_symbol_detail(ver, ident, arch)
    out: dict[str, Any] = {"repo": get_active_repo_name(), **detail}
    if include_usage:
        out["usage"] = _kconfig_service.get_symbol_usage(ver, ident, arch, limit=usage_limit)
    return out


@router.get("/kconfig/build", dependencies=[Depends(require_feature("kconfig"))])
def get_kconfig_build_conditions(
    path: Annotated[list[str], Query(min_length=1)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
) -> dict[str, Any]:
    """Kbuild compilation condition expressions for up to 64 source paths."""
    if len(path) > 64:
        raise HTTPException(status_code=400, detail="At most 64 paths allowed.")
    ver = resolve_version(version)
    clean_paths = [_validate_rel_path(p, allow_empty=False) for p in path]
    res = _kconfig_service.build_conditions(ver, clean_paths, arch)
    return {"repo": get_active_repo_name(), **res}


@router.get("/kconfig/eval", dependencies=[Depends(require_feature("kconfig"))])
def eval_kconfig(
    symbol: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI query list default
    path: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI query list default
    assign: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI query list default
    defconfig: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
) -> dict[str, Any]:
    """Evaluate `symbol` values and `path` Kbuild conditions under `defconfig` plus `assign` (`NAME=value`)."""
    if not symbol and not path:
        raise HTTPException(status_code=400, detail="Give at least one `symbol` or `path`.")
    if len(symbol) > 128 or len(path) > 64 or len(assign) > 128:
        raise HTTPException(status_code=400, detail="At most 128 symbols, 64 paths, and 128 assignments.")
    ver = resolve_version(version)
    names = [_validate_identifier(s.strip().removeprefix("CONFIG_"), "kconfig symbol") for s in symbol]
    clean_paths = [_validate_rel_path(p, allow_empty=False) for p in path]
    assignments = _parse_assignments(assign, max_items=128)
    res = _kconfig_service.evaluate(
        ver, arch, defconfig=defconfig.strip(), assignments=assignments, symbols=names, paths=clean_paths
    )
    return {"repo": get_active_repo_name(), **res}


@router.get("/kconfig/graph", dependencies=[Depends(require_feature("kconfig"))])
def get_kconfig_graph(
    symbol: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
) -> dict[str, Any]:
    """Direct dependency, select, imply, and reverse-dependency graph of a Kconfig `symbol`."""
    ver = resolve_version(version)
    ident = _validate_identifier(symbol.strip().removeprefix("CONFIG_"), "kconfig symbol")
    res = _kconfig_service.get_graph(ver, ident, arch=arch)
    return {"repo": get_active_repo_name(), **res}


@router.get("/kconfig/solve", dependencies=[Depends(require_feature("kconfig"))])
def solve_kconfig_symbol(
    symbol: Annotated[str, Query(min_length=1, max_length=256)],
    value: Annotated[str, Query(pattern="^(y|m|n)$")] = "y",
    assign: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI query list default
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
) -> dict[str, Any]:
    """Compute legal Kconfig symbol assignments to make `symbol` reach `value` (`y`, `m`, or `n`) from `assign` (`NAME=value`)."""
    ver = resolve_version(version)
    target = _validate_identifier(symbol.strip().removeprefix("CONFIG_"), "kconfig symbol")
    current_values = _parse_assignments(assign, max_items=128)
    res = _kconfig_service.autosolve(
        ver,
        {"target_symbol": target, "value": value, "arch": arch, "current_values": current_values},
    )
    return {"repo": get_active_repo_name(), **res}


@router.get("/kconfig/defconfigs", dependencies=[Depends(require_feature("kconfig"))])
def lookup_kconfig_defconfigs(
    file_path: Annotated[str, Query(max_length=512)] = "",
    include_fragments: bool = True,
    presets: bool = False,
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
) -> dict[str, Any]:
    """List architecture defconfigs and fragments, list ARCH presets (`presets=true`), or read a defconfig (`file_path`)."""
    ver = resolve_version(version)
    if file_path.strip():
        clean_path = _validate_rel_path(file_path, allow_empty=False)
        res = _kconfig_service.get_defconfig_content(ver, clean_path, arch)
        return {"repo": get_active_repo_name(), "mode": "content", **res}
    if presets:
        res = _kconfig_service.get_env_presets(ver)
        return {"repo": get_active_repo_name(), "mode": "presets", **res}
    defs = _kconfig_service.get_defconfigs(ver, arch)
    out: dict[str, Any] = {"repo": get_active_repo_name(), "mode": "list", **defs}
    if include_fragments:
        frags = _kconfig_service.list_fragments(ver, arch)
        out["fragments"] = frags.get("fragments") if isinstance(frags, dict) else frags
    return out


@router.get("/kconfig/history", dependencies=[Depends(require_feature("kconfig"))])
def get_kconfig_symbol_history(
    symbol: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    include_rc: bool = False,
    branch: Annotated[str | None, Query(max_length=128)] = None,
) -> dict[str, Any]:
    """Release-by-release history of every definition of `CONFIG_<symbol>`, with property-level changes."""
    ver = resolve_version(version)
    ident = _validate_identifier(symbol.strip().removeprefix("CONFIG_"), "kconfig symbol")
    clean_branch = safe_rev(branch, "branch") if branch else None
    res = history_service.get_kconfig_history(ident, ver, include_rc, branch=clean_branch)
    return {"repo": get_active_repo_name(), **res}


@router.get("/kconfig/kbuild", dependencies=[Depends(require_feature("kconfig"))])
def lookup_kbuild_objects(
    path: Annotated[str, Query(max_length=512)] = "",
    module: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI query list default
    gates: bool = False,
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
) -> Any:
    """Lookup Kbuild objects owning source `path` or building named kernel `module`s."""
    raw_path = path.strip()
    clean_mods = [m.strip() for m in module if m and m.strip()]
    if raw_path and clean_mods:
        raise HTTPException(status_code=400, detail="Specify either `path` or `module`, not both.")
    if not raw_path and not clean_mods:
        raise HTTPException(status_code=400, detail="Provide `path` or at least one `module` name.")
    ver = resolve_version(version)

    if raw_path:
        clean_path = _validate_rel_path(raw_path, allow_empty=False)
        st = kbuild_index_service.index_state(ver, arch, wait=2.0)
        return _with_repo(
            job_answer(
                st,
                lambda r: {
                    "repo": get_active_repo_name(),
                    "mode": "file",
                    "version": ver,
                    "arch": r.get("arch", arch),
                    "path": clean_path,
                    "objects": kbuild_index_service.objects_for_file(r, clean_path),
                },
            )
        )

    if len(clean_mods) > 200 or not all(0 < len(m) <= 64 for m in clean_mods):
        raise HTTPException(status_code=400, detail="Provide 1..200 module names (each 1..64 characters).")

    def _shape_mods(r: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "repo": get_active_repo_name(),
            "mode": "modules",
            "version": ver,
            "arch": r.get("arch", arch),
            **kbuild_index_service.objects_for_module(r, clean_mods),
        }
        if gates:
            out["module_gates"] = kbuild_index_service.module_gates(r)
        return out

    st = kbuild_index_service.index_state(ver, arch, wait=2.0)
    return _with_repo(job_answer(st, _shape_mods))
