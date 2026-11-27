"""api/routers/kapi.py - Kernel API catalogs: summary, syscalls, exports, params, tracepoints, modules, and devices."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from api.config import (
    get_active_repo_name,
    paginate_items,
    require_feature,
    resolve_version,
)
from api.routers.common import DEFAULT_ARCH, _with_repo
from webapp.backend.routers.insights import job_answer
from webapp.backend.services import device_index_service, kapi_service

router = APIRouter()


def _filter_words(rows: list[dict[str, Any]], q: str, text_fn) -> list[dict[str, Any]]:
    words = q.strip().lower().split()
    if not words:
        return rows
    return [r for r in rows if all(w in text_fn(r).lower() for w in words)]


@router.get("/kapi/summary", dependencies=[Depends(require_feature("kapi"))])
def get_kapi_summary(
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
) -> dict[str, Any]:
    """Per-release Kernel API summary and catalog counts."""
    ver = resolve_version(version)
    return {"repo": get_active_repo_name(), **kapi_service.summary(ver, arch)}


@router.get("/kapi/syscalls", dependencies=[Depends(require_feature("kapi"))])
def list_kapi_syscalls(
    q: Annotated[str, Query(max_length=256)] = "",
    abi: Annotated[str, Query(max_length=64)] = "",
    status: Annotated[str, Query(max_length=64)] = "",
    include_unnumbered: bool = False,
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """Paginated system calls of `arch` at `version`: number, ABI, name, entry point, status, and `SYSCALL_DEFINE` signature."""
    ver = resolve_version(version)
    abi_clean = abi.strip()
    status_clean = status.strip()

    def _shape(r: dict[str, Any]) -> dict[str, Any]:
        rows = [
            s
            for s in (r.get("syscalls") or [])
            if (not abi_clean or s.get("abi") == abi_clean)
            and (not status_clean or s.get("status") == status_clean)
        ]
        rows = _filter_words(
            rows,
            q,
            lambda s: f"{s.get('nr', '')} {s.get('abi', '')} {s.get('name', '')} {s.get('entry', '')}",
        )
        page, total, truncated = paginate_items(rows, limit, offset)
        unnumbered = r.get("unnumbered") or []
        out: dict[str, Any] = {
            "repo": get_active_repo_name(),
            "version": ver,
            "arch": r.get("arch", arch),
            "srcarch": r.get("srcarch"),
            "sources": r.get("sources") or [],
            "counts": r.get("counts"),
            "unnumbered_total": len(unnumbered),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "syscalls": page,
        }
        if include_unnumbered:
            out["unnumbered"] = unnumbered
        return out

    return _with_repo(job_answer(kapi_service.syscalls(ver, arch, wait=5.0), _shape))


@router.get("/kapi/exports", dependencies=[Depends(require_feature("kapi"))])
def lookup_kapi_exports(
    name: Annotated[str, Query(max_length=128)] = "",
    q: Annotated[str, Query(max_length=256)] = "",
    variant: Annotated[str, Query(max_length=64)] = "",
    ns: Annotated[str, Query(max_length=128)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """Paginated `EXPORT_SYMBOL*` catalog, or single-symbol export sites with Kbuild module ownership when `name` is set."""
    ver = resolve_version(version)
    sym = name.strip()
    if sym:
        res = kapi_service.export_detail(ver, sym, arch)
        return {"repo": get_active_repo_name(), "mode": "detail", **res}

    var_clean = variant.strip()
    ns_clean = ns.strip()

    def _shape(r: dict[str, Any]) -> dict[str, Any]:
        rows = [
            e
            for e in (r.get("exports") or [])
            if (not var_clean or e.get("variant") == var_clean)
            and (not ns_clean or e.get("namespace") == ns_clean)
        ]
        rows = _filter_words(
            rows,
            q,
            lambda e: f"{e.get('symbol', '')} {e.get('namespace', '')} {e.get('path', '')}",
        )
        page, total, truncated = paginate_items(rows, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "list",
            "version": ver,
            "counts": r.get("counts") or {},
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "exports": page,
        }

    return _with_repo(job_answer(kapi_service.exports(ver, wait=5.0), _shape))


@router.get("/kapi/params", dependencies=[Depends(require_feature("kapi"))])
def list_kapi_params(
    q: Annotated[str, Query(max_length=256)] = "",
    kind: Annotated[str, Query(max_length=64)] = "",
    doc_line: Annotated[int | None, Query(ge=1)] = None,
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """Paginated boot and module parameters, or the `kernel-parameters.txt` entry at `doc_line`."""
    ver = resolve_version(version)
    if doc_line is not None:
        res = kapi_service.param_doc(ver, doc_line)
        return {"repo": get_active_repo_name(), "mode": "doc", "version": ver, **res}

    kind_clean = kind.strip()

    def _shape(r: dict[str, Any]) -> dict[str, Any]:
        rows = [
            p
            for p in (r.get("params") or [])
            if not kind_clean or p.get("kind") == kind_clean
        ]
        rows = _filter_words(
            rows,
            q,
            lambda p: f"{p.get('full_name', '')} {p.get('name', '')} {p.get('path', '')} {p.get('description', '')}",
        )
        page, total, truncated = paginate_items(rows, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "list",
            "version": ver,
            "arch": r.get("arch", arch),
            "doc_path": r.get("doc_path"),
            "counts": r.get("counts"),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "params": page,
        }

    return _with_repo(job_answer(kapi_service.params(ver, arch, wait=5.0), _shape))


@router.get("/kapi/tracepoints", dependencies=[Depends(require_feature("kapi"))])
def lookup_kapi_tracepoints(
    name: Annotated[str, Query(max_length=128)] = "",
    q: Annotated[str, Query(max_length=256)] = "",
    system: Annotated[str, Query(max_length=128)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """Paginated tracepoints catalog, or single-tracepoint definition and call sites when `name` is provided."""
    ver = resolve_version(version)
    tp_name = name.strip()
    if tp_name:
        res = kapi_service.tracepoint_callsites(ver, tp_name)
        tp_meta = None
        st = kapi_service.tracepoints(ver, wait=2.0)
        if st.get("state") == "done":
            tp_meta = next(
                (t for t in (st.get("result") or {}).get("tracepoints", []) if t.get("name") == tp_name),
                None,
            )
        sites = res.get("call_sites") or []
        page, total, truncated = paginate_items(
            sites, limit, offset, already_truncated=bool(res.get("truncated"))
        )
        return {
            "repo": get_active_repo_name(),
            "mode": "detail",
            "version": ver,
            "name": tp_name,
            "tracepoint": tp_meta,
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "call_sites": page,
        }

    sys_clean = system.strip()

    def _shape(r: dict[str, Any]) -> dict[str, Any]:
        rows = [
            t
            for t in (r.get("tracepoints") or [])
            if not sys_clean or t.get("system") == sys_clean
        ]
        rows = _filter_words(
            rows,
            q,
            lambda t: f"{t.get('name', '')} {t.get('system', '')} {t.get('path', '')}",
        )
        page, total, truncated = paginate_items(rows, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "list",
            "version": ver,
            "counts": r.get("counts"),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "tracepoints": page,
        }

    return _with_repo(job_answer(kapi_service.tracepoints(ver, wait=5.0), _shape))


@router.get("/kapi/modules", dependencies=[Depends(require_feature("kapi"))])
def lookup_kapi_modules(
    name: Annotated[str, Query(max_length=128)] = "",
    q: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """Paginated loadable kernel modules for `arch`, or single-module detail (params, exports, maintainers) when `name` is set."""
    ver = resolve_version(version)
    mod_name = name.strip()
    if mod_name:
        res = kapi_service.module_detail(ver, arch, mod_name)
        return {"repo": get_active_repo_name(), "mode": "detail", "version": ver, "arch": arch, **res}

    def _shape(r: dict[str, Any]) -> dict[str, Any]:
        rows = _filter_words(
            list(r.get("modules") or []),
            q,
            lambda m: f"{m.get('modname', '')} {m.get('target', '')} {m.get('description', '')} {' '.join(m.get('required_symbols') or [])}",
        )
        page, total, truncated = paginate_items(rows, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "list",
            "version": ver,
            "arch": r.get("arch", arch),
            "counts": r.get("counts"),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "modules": page,
        }

    return _with_repo(job_answer(kapi_service.modules(ver, arch, wait=5.0), _shape))


@router.get("/kapi/devices", dependencies=[Depends(require_feature("kapi"))])
def lookup_kapi_devices(
    bus: Annotated[str, Query(max_length=64)] = "",
    q: Annotated[str, Query(max_length=256)] = "",
    match: Annotated[str, Query(max_length=16384)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_ARCH,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """Paginated `MODULE_DEVICE_TABLE` entries, or hardware-ID / `lspci -nn` / `lsusb` / modalias driver match when `match` is set."""
    ver = resolve_version(version)
    if match.strip():
        res = device_index_service.match_text(ver, match, arch)
        return {"repo": get_active_repo_name(), "mode": "match", "version": ver, "arch": arch, **res}

    bus_clean = bus.strip()

    def _shape(r: dict[str, Any]) -> dict[str, Any]:
        rows = [
            {
                "bus": e["bus"],
                "id": device_index_service.fmt_id(e),
                "table": e["table"],
                "path": e["path"],
                "line": e["line"],
                "text": e["text"],
            }
            for e in (r.get("entries") or [])
            if not bus_clean or e.get("bus") == bus_clean
        ]
        rows = _filter_words(
            rows,
            q,
            lambda e: f"{e['id']} {e['table']} {e['path']} {e['bus']}",
        )
        page, total, truncated = paginate_items(rows, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "mode": "list",
            "version": ver,
            "tables": r.get("tables", 0),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "entries": page,
        }

    return _with_repo(job_answer(device_index_service.index_state(ver, wait=5.0), _shape))
