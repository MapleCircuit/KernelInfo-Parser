"""webapp/backend/services/kapi_diff_service.py - Kernel API changelog between two revisions (A6,
webapp/API.md section 4.9).

Compares the A1-A5 catalogs of both sides (each cached per commit): what was added, removed or changed
among syscalls, exports, parameters, tracepoints and modules. Renames are not inferred (a renamed
export is removed + added). While a catalog is still building the answer is its job state (202).
"""
from __future__ import annotations

from typing import Any

from webapp.backend.config import RepoScopedCache
from webapp.backend.services import kapi_service as k

KINDS = ("syscalls", "exports", "params", "tracepoints", "modules")
_CACHE: RepoScopedCache = RepoScopedCache(maxsize=16)


def _catalog(kind: str, version: str, arch: str) -> dict[str, Any]:
    if kind == "syscalls":
        return k.syscalls(version, arch)
    if kind == "exports":
        return k.exports(version)
    if kind == "params":
        return k.params(version, arch)
    if kind == "tracepoints":
        return k.tracepoints(version)
    return k.modules(version, arch)


def _keyed(kind: str, result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if kind == "syscalls":
        return {f"{r['name']}/{r['abi']}": {"nr": r["nr"], "abi": r["abi"], "entry": r["entry"],
                                           "args": ", ".join(f"{a['type']} {a['name']}" for a in (r.get("definition") or {}).get("args", [])),
                                           "path": (r.get("definition") or {}).get("path", "")} for r in result["syscalls"]}
    if kind == "exports":
        return {e["symbol"]: {"variant": e["variant"], "namespace": e["namespace"], "path": e["path"]} for e in result["exports"]}
    if kind == "params":
        return {p["full_name"]: {"kind": p["kind"], "type": p.get("type", ""), "perm": p.get("perm", ""), "path": p["path"]} for p in result["params"]}
    if kind == "tracepoints":
        return {t["name"]: {"kind": t["kind"], "fields": [f["name"] for f in t["fields"]], "printk": t["printk"], "path": t["path"],
                            "system": t["system"]} for t in result["tracepoints"]}
    return {m["modname"]: {"options": " ".join(m["options"]), "license": m["license"], "target": m["target"],
                           "path": m["members"][0] if m["members"] else ""} for m in result["modules"]}


def compare(kind: str, a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """{added, removed, changed} of two keyed catalogs (pure)."""
    added = [{"key": key, **b[key]} for key in sorted(b.keys() - a.keys())]
    removed = [{"key": key, **a[key]} for key in sorted(a.keys() - b.keys())]
    changed = []
    for key in sorted(a.keys() & b.keys()):
        x, y = a[key], b[key]
        if kind == "tracepoints":
            fa, fb = set(x["fields"]), set(y["fields"])
            if fa != fb or x["printk"] != y["printk"]:
                changed.append({"key": key, "fields_added": sorted(fb - fa), "fields_removed": sorted(fa - fb),
                                "printk_changed": x["printk"] != y["printk"], "path": y["path"]})
            continue
        for field in [f for f in x if f != "path"]:
            if x.get(field) != y.get(field):
                changed.append({"key": key, "field": field, "from": x.get(field), "to": y.get(field), "path": y.get("path", "")})
    return {"added": added, "removed": removed, "changed": changed}


def diff(a: str, b: str, arch: str, kinds: list[str]) -> dict[str, Any]:
    """{state: "done", result} or the state of a catalog still building."""
    sha_a, sha_b = k.resolve(a), k.resolve(b)
    kinds = [x for x in KINDS if x in kinds] or list(KINDS)
    ck = (sha_a, sha_b, arch, tuple(kinds))
    hit = _CACHE.get(ck)
    if hit is not None:
        return {"state": "done", "result": hit}
    results: dict[str, dict[str, Any]] = {}
    for kind in kinds:
        for side, version in (("a", a), ("b", b)):
            st = _catalog(kind, version, arch)
            if st.get("state") == "failed":
                return st
            if st.get("state") != "done":
                p = dict(st.get("progress") or {})
                p["note"] = f"{kind} of {version}" + (f": {p.get('note')}" if p.get("note") else "")
                return {**st, "progress": p}
            results[f"{kind}/{side}"] = st["result"]
    out: dict[str, Any] = {"a": a, "b": b, "arch": arch, "kinds": kinds}
    for kind in kinds:
        out[kind] = compare(kind, _keyed(kind, results[f"{kind}/a"]), _keyed(kind, results[f"{kind}/b"]))
    out["counts"] = {kind: {s: len(out[kind][s]) for s in ("added", "removed", "changed")} for kind in kinds}
    _CACHE[ck] = out
    return {"state": "done", "result": out}
