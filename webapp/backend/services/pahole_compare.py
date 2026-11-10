"""webapp/backend/services/pahole_compare.py - Struct layout over time (N3, webapp/API.md section 4.7).

Compare a struct's layout between two revisions member by member, and follow its size, holes and cachelines
across the releases where its definition changed (the symbol history's stops), on top of PaholeService.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException

SUMMARY_KEYS = ("total_size", "holes_count", "padding_bytes", "cache_lines_used", "is_estimate")
CHANGE_STATUSES = ("introduced", "added", "modified")


def summary(layout: dict[str, Any]) -> dict[str, Any]:
    return {k: layout.get(k) for k in SUMMARY_KEYS} | {"version": layout.get("version"), "file_path": layout.get("file_path"), "line_s": layout.get("line_s")}


def compare_layouts(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Members matched by name: same | added | removed | moved | resized | type_changed, plus *renamed?* guesses
    (a removed and an added member of the same type at the same offset) and the size / holes / cacheline deltas."""
    am = {m["name"]: m for m in a.get("members") or []}
    bm = {m["name"]: m for m in b.get("members") or []}
    side = lambda m: {"offset": m["offset"], "size": m["size"], "type": m["type"]} if m else None  # noqa: E731
    rows = []
    for name in list(am) + [n for n in bm if n not in am]:
        x, y = am.get(name), bm.get(name)
        if x and not y:
            status = "removed"
        elif y and not x:
            status = "added"
        elif x["type"] != y["type"]:
            status = "type_changed"
        elif x["size"] != y["size"]:
            status = "resized"
        elif x["offset"] != y["offset"]:
            status = "moved"
        else:
            status = "same"
        rows.append({"name": name, "status": status, "a": side(x), "b": side(y)})
    removed = [r for r in rows if r["status"] == "removed"]
    for r in [r for r in rows if r["status"] == "added"]:
        twin = next((x for x in removed if x["a"]["type"] == r["b"]["type"] and x["a"]["offset"] == r["b"]["offset"] and not x.get("renamed_to")), None)
        if twin:
            twin["renamed_to"] = r["name"]
            r["renamed_from"] = twin["name"]
    # Order by the new layout, removed members where they were
    rows.sort(key=lambda r: ((r["b"] or r["a"])["offset"], r["status"] == "removed"))
    delta = {k: (b.get(k) or 0) - (a.get(k) or 0) for k in ("total_size", "holes_count", "padding_bytes", "cache_lines_used")}
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"a": summary(a), "b": summary(b), "members": rows, "delta": delta, "counts": counts,
            "is_estimate": bool(a.get("is_estimate") or b.get("is_estimate"))}


def history_stops(timeline: list[dict[str, Any]]) -> list[str]:
    """The releases where the definition was introduced or changed (cosmetic edits do not move a layout)."""
    return [e["vname"] for e in timeline if e.get("status") in CHANGE_STATUSES]


def layout_history(stops: list[str], layout_of: Callable[[str], dict[str, Any]], progress: Callable | None = None) -> list[dict[str, Any]]:
    out = []
    for i, v in enumerate(stops):
        if progress:
            progress(i, len(stops), f"layout at {v}")
        try:
            out.append(summary(layout_of(v)) | {"version": v, "available": True})
        except HTTPException as exc:
            out.append({"version": v, "available": False, "reason": str(exc.detail)[:200]})
    return out


# ------------------------------------------------------------------------------------------- service
_PAHOLE = None


def _pahole():
    global _PAHOLE
    if _PAHOLE is None:
        from webapp.backend.services.pahole_service import PaholeService
        _PAHOLE = PaholeService()
    return _PAHOLE


def compare(a: str, b: str, struct: str) -> dict[str, Any]:
    from webapp.backend.config import RepoScopedCache
    from webapp.backend.security.git import safe_rev
    from webapp.backend.services.kapi_service import resolve
    global _CMP
    safe_rev(a)
    safe_rev(b)
    key = (resolve(a), resolve(b), struct)
    if _CMP is None:
        _CMP = RepoScopedCache(maxsize=64)
    hit = _CMP.get(key)
    if hit is None:
        hit = compare_layouts(_pahole().get_struct_layout(a, struct), _pahole().get_struct_layout(b, struct))
        hit["struct"] = struct
        _CMP[key] = hit
    return hit


_CMP = None


def history_state(version: str, struct: str, include_rc: bool = False, wait: float = 2.0) -> dict[str, Any]:
    from webapp.backend.config import get_active_repo_name
    from webapp.backend.services.disk_cache import DiskCache
    from webapp.backend.services.jobs import jobs
    from webapp.backend.services.kapi_service import resolve
    sha = resolve(version)
    ck = f"{get_active_repo_name()}|pahole-hist|1|{sha}|{struct}|{int(include_rc)}"

    def build(progress):
        from webapp.backend.services.history_service import history_service
        here = _pahole().get_struct_layout(version, struct)
        progress(0, 1, "release history of the definition")
        hist = history_service.get_symbol_history(version, here["file_path"], line=here["line_s"], name=struct.split()[-1], include_rc=include_rc)
        stops = history_stops(hist.get("timeline") or [])
        return {"struct": here["struct_name"], "version": version, "path": here["file_path"],
                "stops": layout_history(stops, lambda v: _pahole().get_struct_layout(v, struct), progress)}

    global _HDISK
    if _HDISK is None:
        _HDISK = DiskCache("pahole-history", max_bytes=16 << 20)
    state = jobs.submit(f"phist:{ck}", build, cache=_HDISK, cache_key=ck)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(f"phist:{ck}", wait) or state
    return state


_HDISK = None
