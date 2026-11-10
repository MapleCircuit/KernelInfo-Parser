"""webapp/backend/routers/kapi.py - Kernel API catalogs (A1-A5, webapp/API.md section 4.9)."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query

from webapp.backend.routers.insights import job_answer
from webapp.backend.services import kapi_service as k
from webapp.backend.services.kconfig_service import DEFAULT_ARCH

router = APIRouter(prefix="/api/kapi", tags=["kapi"])

Arch = Annotated[str, Query(description="ARCH value (x86_64, i386, arm, ...)")]


def _page(rows: list[dict[str, Any]], q: str, offset: int, limit: int, text) -> tuple[list[dict[str, Any]], int]:
    if q:
        words = q.lower().split()
        rows = [r for r in rows if all(w in text(r).lower() for w in words)]
    return rows[offset:offset + limit], len(rows)


@router.get("/diff")
def get_api_diff(
    a: Annotated[str, Query(description="Base revision")],
    b: Annotated[str, Query(description="Target revision")],
    arch: Arch = DEFAULT_ARCH,
    kinds: Annotated[str, Query(description="Comma-separated: syscalls,exports,params,tracepoints,modules")] = "",
) -> Any:
    """Kernel API changelog between two revisions (A6): per kind {added, removed, changed} and counts.
    202 while a catalog of either side is still being built."""
    from webapp.backend.security.git import safe_rev
    from webapp.backend.services.kapi_diff_service import diff
    safe_rev(a, "base revision")
    safe_rev(b, "target revision")
    return job_answer(diff(a, b, arch, [x.strip() for x in kinds.split(",") if x.strip()]))


@router.get("/{version_name}/syscalls")
def get_syscalls(version_name: str, arch: Arch = DEFAULT_ARCH) -> Any:
    """System calls of an ARCH (A1): number, ABI, name, entry point, status (defined / ni_syscall /
    no_definition) and the SYSCALL_DEFINE with its arguments; plus definitions with no number."""
    return job_answer(k.syscalls(version_name, arch, wait=5.0), lambda r: {"version": version_name, **r})


@router.get("/{version_name}/exports")
def get_exports(
    version_name: str,
    q: Annotated[str, Query()] = "",
    variant: Annotated[str, Query()] = "",
    ns: Annotated[str, Query()] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100000)] = 100000,
) -> Any:
    """EXPORT_SYMBOL* sites of a revision (A2): {symbol, variant, namespace, path, line}, filtered and paged."""
    def shape(r: dict[str, Any]) -> dict[str, Any]:
        rows = [e for e in r["exports"] if (not variant or e["variant"] == variant) and (not ns or e["namespace"] == ns)]
        page, total = _page(rows, q, offset, limit, lambda e: f"{e['symbol']} {e['namespace']} {e['path']}")
        return {"version": version_name, "total": total, "counts": r["counts"], "exports": page}
    return job_answer(k.exports(version_name, wait=5.0), shape)


@router.get("/{version_name}/exports/{symbol}")
def get_export(version_name: str, symbol: str, arch: Arch = DEFAULT_ARCH) -> dict[str, Any]:
    """Every export site of a symbol, with the object that contains it (built-in or a module, S04)."""
    return k.export_detail(version_name, symbol, arch)


@router.get("/{version_name}/params")
def get_params(version_name: str, arch: Arch = DEFAULT_ARCH) -> Any:
    """Boot (__setup, early_param, core_param) and module parameters (A3), with their documentation
    entry (kernel-parameters.txt), undocumented boot parameters and stale documentation entries."""
    return job_answer(k.params(version_name, arch, wait=5.0), lambda r: {"version": version_name, **r})


@router.get("/{version_name}/params/doc")
def get_param_doc(version_name: str, line: Annotated[int, Query(ge=1)] = 1) -> dict[str, Any]:
    """The kernel-parameters.txt entry starting at a line."""
    return k.param_doc(version_name, line)


@router.get("/{version_name}/tracepoints")
def get_tracepoints(version_name: str, q: Annotated[str, Query()] = "") -> Any:
    """Tracepoints (A4): events, classes and raw tracepoints with system, prototype, fields and format."""
    def shape(r: dict[str, Any]) -> dict[str, Any]:
        rows, total = _page(r["tracepoints"], q, 0, 100000, lambda t: f"{t['name']} {t['system']}")
        return {"version": version_name, "total": total, "tracepoints": rows}
    return job_answer(k.tracepoints(version_name, wait=5.0), shape)


@router.get("/{version_name}/tracepoints/{name}")
def get_tracepoint(version_name: str, name: str) -> dict[str, Any]:
    """One tracepoint's call sites (`trace_<name>(`, at most 500)."""
    return k.tracepoint_callsites(version_name, name)


@router.get("/{version_name}/modules")
def get_modules(version_name: str, arch: Arch = DEFAULT_ARCH) -> Any:
    """Loadable modules of an ARCH (A5): objects whose condition can be m (S04), with Kconfig options,
    license, authors, description, aliases, firmware, device tables, init/exit functions."""
    return job_answer(k.modules(version_name, arch, wait=5.0), lambda r: {"version": version_name, **r})


@router.get("/{version_name}/modules/{modname}")
def get_module(version_name: str, modname: str, arch: Arch = DEFAULT_ARCH) -> dict[str, Any]:
    """One module with its parameters (A3), exports (A2) and maintainers."""
    return k.module_detail(version_name, arch, modname)


@router.get("/{version_name}/devices")
def get_devices(
    version_name: str,
    bus: Annotated[str, Query()] = "",
    q: Annotated[str, Query()] = "",
    arch: Arch = DEFAULT_ARCH,
) -> Any:
    """Device table entries (N4): {bus, id, table, path, line, text}; `q` filters by id, table or path."""
    from webapp.backend.services.device_index_service import fmt_id, index_state

    def shape(r: dict[str, Any]) -> dict[str, Any]:
        rows = [{"bus": e["bus"], "id": fmt_id(e), "table": e["table"], "path": e["path"], "line": e["line"], "text": e["text"]}
                for e in r["entries"] if not bus or e["bus"] == bus]
        page, total = _page(rows, q, 0, 200000, lambda e: f"{e['id']} {e['table']} {e['path']} {e['bus']}")
        return {"version": version_name, "tables": r["tables"], "total": total, "entries": page}

    return job_answer(index_state(version_name, wait=5.0), shape)


@router.post("/{version_name}/devices/match")
def post_devices_match(version_name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Drivers for pasted hardware ids (N4): lspci -nn, lsusb, modalias, vendor:device, OF compatible or
    ACPI ids, one per line (at most 64 KB): {lines: [{input, ids, matches: [{module, options, path, line,
    table, match_kind}]}], unmatched}."""
    from fastapi import HTTPException
    from webapp.backend.services.device_index_service import match_text
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise HTTPException(status_code=400, detail='Body must be {"text": "...", "arch": "..."}')
    return match_text(version_name, payload["text"], str(payload.get("arch") or DEFAULT_ARCH))
