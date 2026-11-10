"""webapp/backend/services/kbuild_index_service.py - Kbuild object index (S04, webapp/API.md section 4.4).

Every object the Kbuild files of one revision and ARCH name (`KbuildConditionResolver.objects()`,
parser/kconfig_ast/KCONFIG_API.md section 6.4): its module name, kind, build condition (directory chain
included) and member sources, plus a file -> objects map. Conditions are interned (many objects share a
directory chain). Built in a background job (section 2.15, ~5 s for v3.0 x86_64), cached on disk per
(repository, commit, ARCH, format) and in an LRU of 4 indexes in memory.

Helpers used by the Kernel API catalogs (A2, A3, A5), localmodconfig (K1), the config-aware treemap
(K4) and hardware -> driver (N4): `objects_for_module`, `objects_for_file`, `required_symbols`.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from fastapi import HTTPException

from parser.kbuild_conditions import M, N, UNKNOWN, Y, evaluate, is_other_arch_path
from webapp.backend.config import get_active_repo_name
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.jobs import jobs

logger = logging.getLogger(__name__)

INDEX_FORMAT = 1
_DISK = DiskCache("kbuild-index", max_bytes=512 << 20, max_entries=64)
_MEM: OrderedDict[str, dict[str, Any]] = OrderedDict()
_MEM_LOCK = threading.Lock()
MEM_SIZE = 4


def _kconfig():
    from webapp.backend.services.kconfig_service import KconfigService
    return KconfigService()


def build_index(version: str, arch: str, progress: Callable | None = None) -> dict[str, Any]:
    svc = _kconfig()
    resolver, info = svc._build_resolver(version, arch)
    started = time.monotonic()
    exprs: list[Any] = []
    intern: dict[str, int] = {}

    def ix(expr: Any) -> int:
        key = json.dumps(expr, sort_keys=True, separators=(",", ":"))
        if key not in intern:
            intern[key] = len(exprs)
            exprs.append(expr)
        return intern[key]

    objects = []
    files: dict[str, list[int]] = {}
    from webapp.backend.services.kconfig_service import _BUILD_LOCK
    with _BUILD_LOCK:  # the resolver's caches are not thread safe (KconfigService shares it)
        for i, obj in enumerate(resolver.objects()):
            if progress and i % 500 == 0:
                progress(i, None, f"{i} objects")
            members = [{"path": p, "expr": ix(c)} for p, c in obj.members]
            objects.append({
                "target": obj.target, "modname": obj.modname, "kind": obj.kind, "expr": ix(obj.condition),
                "composite": obj.composite, "members": members,
                "rules": [r.as_dict() for r in obj.rules],
            })
            for m in members:
                files.setdefault(m["path"], []).append(len(objects) - 1)
    logger.info("Kbuild index of %s ARCH=%s: %d objects, %d conditions in %.1f s", version, info["arch"], len(objects),
                len(exprs), time.monotonic() - started)
    return {"sha": info.get("commit") or info.get("rev"), "arch": info["arch"], "srcarch": info["srcarch"],
            "exprs": exprs, "objects": objects, "files": files}


def _key(version: str, arch: str) -> tuple[str, str]:
    from webapp.backend.services.release_service import resolve_sha
    info = _kconfig().resolve_arch(version, arch)
    sha = resolve_sha(info["rev"]) or info["rev"]
    return f"{get_active_repo_name()}|{INDEX_FORMAT}|{sha}|{info['arch']}", info["arch"]


def index_state(version: str, arch: str, wait: float = 0.0) -> dict[str, Any]:
    """Job state of the index ({state: "done", result} when ready)."""
    ck, real_arch = _key(version, arch)
    with _MEM_LOCK:
        hit = _MEM.get(ck)
        if hit is not None:
            _MEM.move_to_end(ck)
            return {"state": "done", "result": hit, "cache_key": ck}
    state = jobs.submit(f"kbuild-index:{ck}", lambda p: build_index(version, real_arch, p), cache=_DISK, cache_key=ck)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(f"kbuild-index:{ck}", wait) or state
    if state.get("state") == "done":
        with _MEM_LOCK:
            _MEM[ck] = state["result"]
            while len(_MEM) > MEM_SIZE:
                _MEM.popitem(last=False)
    return {**state, "cache_key": ck}


def index_now(version: str, arch: str, timeout: float = 120.0) -> dict[str, Any]:
    """The index, waiting for its job (for other jobs and in-process callers)."""
    st = index_state(version, arch, wait=timeout)
    if st.get("state") != "done":
        raise HTTPException(status_code=503, detail="The Kbuild index is still being built, retry")
    return st["result"]


def required_symbols(expr: Any) -> list[str]:
    """Symbols whose value n makes the expression n (all others y): what must be enabled for it."""
    syms: set[str] = set()

    def walk(e: Any) -> None:
        if isinstance(e, dict):
            for k, v in e.items():
                if k in ("sym", "def", "val") and isinstance(v, str):
                    syms.add(v)
                else:
                    walk(v)
        elif isinstance(e, list):
            for x in e:
                walk(x)

    walk(expr)
    out = []
    for s in sorted(syms):
        if evaluate(expr, lambda name, s=s: N if name == s else Y) == N:
            out.append(s)
    return out


def module_capable(expr: Any) -> bool:
    """Whether the condition can be "m" (a tristate symbol gates it)."""
    syms: set[str] = set()

    def walk(e: Any) -> None:
        if isinstance(e, dict):
            if "sym" in e and isinstance(e["sym"], str):
                syms.add(e["sym"])
            for v in e.values():
                walk(v)
        elif isinstance(e, list):
            for x in e:
                walk(x)

    walk(expr)
    return any(evaluate(expr, lambda name, s=s: M if name == s else Y) == M for s in syms)


def objects_for_module(idx: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """modname (dashes or underscores) -> objects building it, with required symbols."""
    by_name: dict[str, list[int]] = {}
    for i, o in enumerate(idx["objects"]):
        by_name.setdefault(o["modname"], []).append(i)
    out: dict[str, Any] = {}
    unmatched = []
    for raw in names:
        name = str(raw).strip().replace("-", "_")
        hits = by_name.get(name)
        if not hits:
            unmatched.append(raw)
            continue
        objs = [idx["objects"][i] for i in hits]
        out[raw] = {"objects": [{"target": o["target"], "expr": idx["exprs"][o["expr"]], "members": [m["path"] for m in o["members"]],
                                 "module_capable": module_capable(idx["exprs"][o["expr"]])} for o in objs],
                    "required_symbols": sorted({s for o in objs for s in required_symbols(idx["exprs"][o["expr"]])})}
    return {"modules": out, "unmatched": unmatched}


def module_gates(idx: dict[str, Any]) -> list[str]:
    """Symbols gating at least one module-capable object: the options `localmodconfig` may turn off (K1)."""
    out: set[str] = set()
    for o in idx["objects"]:
        expr = idx["exprs"][o["expr"]]
        if module_capable(expr):
            out.update(required_symbols(expr))
    return sorted(out)


def objects_for_file(idx: dict[str, Any], path: str) -> list[dict[str, Any]]:
    out = []
    for i in idx["files"].get(path.strip("/"), []):
        o = idx["objects"][i]
        member = next((m for m in o["members"] if m["path"] == path.strip("/")), None)
        out.append({"target": o["target"], "modname": o["modname"], "kind": o["kind"], "expr": idx["exprs"][o["expr"]],
                    "member_expr": idx["exprs"][member["expr"]] if member else Y, "composite": o["composite"],
                    "members": [m["path"] for m in o["members"]], "rules": o["rules"]})
    return out


def compact(idx: dict[str, Any]) -> dict[str, Any]:
    """The index as served to clients: conditions interned, rules left out (the file endpoint has them)."""
    return {"sha": idx["sha"], "arch": idx["arch"], "srcarch": idx["srcarch"], "exprs": idx["exprs"],
            "objects": [{"t": o["target"], "n": o["modname"], "k": o["kind"], "e": o["expr"],
                         "m": [[m["path"], m["expr"]] for m in o["members"]]} for o in idx["objects"]]}


__all__ = ["index_state", "index_now", "required_symbols", "module_capable", "objects_for_module", "objects_for_file",
           "compact", "Y", "M", "N", "UNKNOWN"]


# ------------------------------------------------------------------ config-aware treemap (K4)
_RANK = {N: 0, UNKNOWN: 1, M: 2, Y: 3}
MAX_ASSIGNMENTS = 30000
_ASSIGN_KEY = __import__("re").compile(r"^[A-Za-z0-9_]{1,100}$")


def validate_assignments(raw: Any) -> dict[str, str]:
    """{SYM: "y"|"m"|"n"|string|number} from a client (CONFIG_ prefixes allowed); HTTP 400 otherwise."""
    if not isinstance(raw, dict) or len(raw) > MAX_ASSIGNMENTS:
        raise HTTPException(status_code=400, detail=f"assignments must be an object of at most {MAX_ASSIGNMENTS} entries")
    out = {}
    for k, v in raw.items():
        key = str(k)[7:] if str(k).startswith("CONFIG_") else str(k)
        if not _ASSIGN_KEY.match(key) or not isinstance(v, (str, int, float, bool)) or len(str(v)) > 1000:
            raise HTTPException(status_code=400, detail=f"Invalid assignment: {str(k)[:40]}")
        out[key] = str(v)
    return out


def classify_files(idx: dict[str, Any], assignments: dict[str, str]) -> dict[str, str]:
    """source path -> "y" (built in), "m" (module), "n" (not built) or "?" under a configuration.

    A resolved configuration (an engine's, at least 50 symbols) lists what is set: a symbol it leaves out
    is unset ("n"); a smaller, partial one leaves the others unknown."""
    full = len(assignments) >= 50
    lookup = (lambda s: assignments.get(s, "n")) if full else (lambda s: assignments.get(s))  # noqa: E731
    values = [evaluate(e, lookup) for e in idx["exprs"]]
    out: dict[str, str] = {}
    for o in idx["objects"]:
        ov = values[o["expr"]]
        for m in o["members"]:
            mv = values[m["expr"]]
            v = min((ov, mv), key=lambda x: _RANK.get(x, 1)) if UNKNOWN not in (ov, mv) else (N if N in (ov, mv) else UNKNOWN)
            cur = out.get(m["path"])
            if cur is None or _RANK.get(v, 1) > _RANK.get(cur, 1):
                out[m["path"]] = v
    return out


_TREEMAP: Any = None


def build_treemap(version: str, arch: str, assignments: dict[str, str], path: str = "", max_depth: int = 2) -> dict[str, Any] | dict:
    """The treemap of a subtree (filesystem_service shape) with per node built_y / built_m / not_built /
    unknown bytes and files_y / files_m / files_n / files_unknown under the configuration."""
    import hashlib
    from webapp.backend.config import RepoScopedCache
    from webapp.backend.services.tree_resolver import git_tree_resolver
    global _TREEMAP
    if _TREEMAP is None:
        _TREEMAP = RepoScopedCache(maxsize=32)
    st = index_state(version, arch, wait=2.0)
    if st.get("state") != "done":
        return st
    idx = st["result"]
    akey = hashlib.sha1(json.dumps(assignments, sort_keys=True).encode()).hexdigest()
    norm = path.strip().strip("/")
    max_depth = max(1, min(int(max_depth or 2), 6))
    ck = (st["cache_key"], akey, norm, max_depth)
    hit = _TREEMAP.get(ck)
    if hit is not None:
        return {"state": "done", "result": hit}
    sizes = git_tree_resolver.list_tree_sizes(version) or {}
    klass = classify_files(idx, assignments)
    bucket = {Y: ("built_y", "files_y"), M: ("built_m", "files_m"), N: ("not_built", "files_n")}
    prefix = f"{norm}/" if norm else ""

    def new(name: str, p: str, kind: str) -> dict[str, Any]:
        return {"name": name, "path": p, "type": kind, "size": 0, "file_count": 0, "built_y": 0, "built_m": 0, "not_built": 0,
                "unknown": 0, "files_y": 0, "files_m": 0, "files_n": 0, "files_unknown": 0, "children": {}}

    root = new(norm.split("/")[-1] if norm else "root", norm, "dir")
    for f, size in sizes.items():
        if prefix and not f.startswith(prefix):
            continue
        k = klass.get(f)
        if k is None and f.endswith((".c", ".S")) and is_other_arch_path(f, idx["srcarch"]):
            k = N  # another architecture's tree: never built for this ARCH
        b, c = bucket.get(k or UNKNOWN, ("unknown", "files_unknown"))
        parts = f[len(prefix):].split("/")
        node = root
        chain = [root]
        for depth, part in enumerate(parts[:max_depth]):
            child = node["children"].get(part)
            if child is None:
                child = new(part, f"{node['path']}/{part}" if node["path"] else part, "file" if depth == len(parts) - 1 else "dir")
                node["children"][part] = child
            chain.append(child)
            node = child
        for n in chain:
            n["size"] += size
            n["file_count"] += 1
            n[b] += size
            n[c] += 1

    def ser(n: dict[str, Any]) -> dict[str, Any]:
        kids = sorted(n["children"].values(), key=lambda x: x["size"], reverse=True)
        return {**{k: v for k, v in n.items() if k != "children"}, "children": [ser(k) for k in kids]}

    result = {"version": version, "arch": idx["arch"], "max_depth": max_depth, **ser(root)}
    _TREEMAP[ck] = result
    return {"state": "done", "result": result}
