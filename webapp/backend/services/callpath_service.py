"""webapp/backend/services/callpath_service.py - Call path finder: how function A reaches function B (N2, webapp/API.md section 4.7).

A bidirectional breadth-first search over the source index of the revision (section 2.19, in memory): forward from every
node named A along callees, backward from every node named B along callers, always expanding the smaller frontier. It
stops at the first level where the frontiers meet, within a depth limit, an expansion budget and a deadline, and returns
up to `k` shortest call chains with the call site of every hop.

Calls made in other architectures' trees, in the other word size's files of the ARCH's own tree (`process_32.c` for
x86_64) and in user-space trees (`tools/`, `scripts/`, `samples/`) are not followed. With `indirect`, a call through a struct member (`->read(...)`) also reaches the functions the
ops tables assign to that field, matched by field name (approximate: every struct with a `read` field); those hops carry
`via: "file_operations.read"`.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Hashable, Iterable

Edge = tuple  # (other node, path, line[, via])


def _keys(value: Any) -> list:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def search_paths(src: Any, dst: Any, callees: Callable[[list], dict[Hashable, list[Edge]]],
                 callers: Callable[[list], dict[Hashable, list[Edge]]], *, max_depth: int = 6, k: int = 5,
                 budget: int = 50_000, deadline: float | None = None, exclude: Iterable[Hashable] = (),
                 progress: Callable | None = None) -> dict[str, Any]:
    """Shortest call chains from any of `src` to any of `dst` (a node or a list of nodes).

    `callees(nodes)` -> {caller: [(callee, path, line[, via])]}, `callers(nodes)` -> {callee: [(caller, path, line[, via])]}
    (the site is always in the caller). Returns {paths, explored, budget_exhausted, timed_out, depth}; a path is
    [{function, path, line[, via]}] where path/line/via describe the call to the next hop."""
    srcs, dsts = _keys(src), _keys(dst)
    skip = set(exclude) - set(srcs) - set(dsts)
    fwd: dict[Hashable, list[tuple]] = {s: [] for s in srcs}   # node -> [(parent, path, line, via)]
    bwd: dict[Hashable, list[tuple]] = {d: [] for d in dsts}   # node -> [(child, path, line, via)]
    f_front, b_front = list(srcs), list(dsts)
    f_depth = b_depth = 0
    explored = 0
    exhausted = timed_out = False
    meet: set = set(fwd) & set(bwd)
    # A side whose frontier runs dry (nothing calls the target, or it calls nothing) leaves the search to the other
    while not meet and (f_front or b_front) and f_depth + b_depth < max_depth:
        if deadline is not None and time.monotonic() > deadline:
            timed_out = True
            break
        if explored >= budget:
            exhausted = True
            break
        forward = bool(f_front) and (not b_front or len(f_front) <= len(b_front))
        front = (f_front if forward else b_front)[: max(0, budget - explored)]
        explored += len(front)
        seen = fwd if forward else bwd
        level: dict[Hashable, list[tuple]] = {}
        step = 2000
        for i in range(0, len(front), step):
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                break
            if progress:
                progress(explored - len(front) + i, budget, f"{'forward' if forward else 'backward'} level {(f_depth if forward else b_depth) + 1}")
            edges = (callees if forward else callers)(front[i:i + step])
            for node, outs in edges.items():
                for edge in outs:
                    other = edge[0]
                    if other in skip or other in seen:
                        continue
                    site = (node, edge[1], edge[2], edge[3] if len(edge) > 3 else "")
                    hops = level.setdefault(other, [])
                    if site not in hops and len(hops) < 8:
                        hops.append(site)
        seen.update(level)
        if forward:
            f_front, f_depth = list(level), f_depth + 1
        else:
            b_front, b_depth = list(level), b_depth + 1
        meet = set(fwd) & set(bwd)
    paths: list[list[dict[str, Any]]] = []
    if meet:
        src_set, dst_set = set(srcs), set(dsts)

        def heads(node: Hashable) -> list[list[tuple]]:
            """Chains src..node as [(function, site path, site line, via)] (the site of the call to the next hop)."""
            if node in src_set:
                return [[(node, None, None, "")]]
            out = []
            for parent, path, line, via in fwd[node]:
                for chain in heads(parent):
                    if len(out) >= k:
                        return out
                    out.append(chain[:-1] + [(chain[-1][0], path, line, via), (node, None, None, "")])
            return out

        def tails(node: Hashable) -> list[list[tuple]]:
            if node in dst_set:
                return [[(node, None, None, "")]]
            out = []
            for child, path, line, via in bwd[node]:
                for chain in tails(child):
                    if len(out) >= k:
                        return out
                    out.append([(node, path, line, via)] + chain)
            return out

        seen_paths = set()
        for m in sorted(meet, key=str):
            for h in heads(m):
                for t in tails(m):
                    chain = h[:-1] + t
                    names = tuple(c[0] for c in chain)
                    if names in seen_paths or len(set(names)) != len(names):
                        continue
                    seen_paths.add(names)
                    paths.append([{"function": f, "path": p, "line": ln, **({"via": via} if via else {})} for f, p, ln, via in chain])
                    if len(paths) >= k:
                        break
                if len(paths) >= k:
                    break
            if len(paths) >= k:
                break
        paths.sort(key=len)
    return {"paths": paths, "explored": explored, "budget_exhausted": exhausted, "timed_out": timed_out,
            "depth": f_depth + b_depth}


# ------------------------------------------------------------------------------------- over the source index

# Trees of user-space programs and examples: their calls are not the kernel's
NOT_KERNEL = ("tools/", "scripts/", "samples/", "Documentation/", "usr/")


def arch_filter(version: str, arch: str | None) -> tuple[str, Callable[[str], bool]]:
    """(ARCH, path test): calls made in a path count unless it belongs to another architecture's tree, to the other
    word size's files of the ARCH's own tree, or to a user-space tree (`tools/`, `scripts/`, ...)."""
    from webapp.backend.services.tree_resolver import ARCH_OTHER, ARCH_OWN, _bits_penalty, definition_arch, path_arch_group
    name, scope, bits = definition_arch(version, arch or None)

    def test(path: str) -> bool:
        if path.startswith(NOT_KERNEL):
            return False
        group = path_arch_group(path, scope)
        if group == ARCH_OTHER:
            return False
        return not (group == ARCH_OWN and _bits_penalty(path, bits))

    return name, test


def find_call_paths(idx: Any, src: str, dst: str, *, arch_name: str = "", allowed_test: Callable[[str], bool] | None = None,
                    max_depth: int = 6, k: int = 5, budget: int = 50_000, timeout: float = 20.0, exclude: Iterable[str] = (),
                    indirect: bool = False, progress: Callable | None = None) -> dict[str, Any]:
    """Search a `source_index.RevisionIndex` by function names."""
    started = time.monotonic()
    allowed = idx.allowed(arch_name, allowed_test) if allowed_test else None
    srcs, dsts = idx.nodes_named(src), idx.nodes_named(dst)
    skip = [n for name in exclude for n in idx.nodes_named(name)]

    def callees(nodes: list[int]) -> dict[int, list[Edge]]:
        return {n: idx.callees(n, allowed, indirect) for n in nodes}

    def callers(nodes: list[int]) -> dict[int, list[Edge]]:
        return {n: idx.callers(n, allowed, indirect) for n in nodes}

    if srcs and dsts:
        res = search_paths(srcs, dsts, callees, callers, max_depth=max_depth, k=k, budget=budget,
                           deadline=started + timeout, exclude=skip, progress=progress)
    else:
        res = {"paths": [], "explored": 0, "budget_exhausted": False, "timed_out": False, "depth": 0}
    for path in res["paths"]:
        for hop in path:
            node = hop["function"]
            hop["function"] = idx.name_of(node)
            if idx.file_of(node):
                hop["file"] = idx.file_of(node)  # a static function or a macro of that file
            if hop["path"] is not None:
                hop["path"] = idx.paths[hop["path"]]
    res.update(found={"from": len(srcs), "to": len(dsts)}, arch=arch_name, indirect=indirect,
               elapsed_ms=int((time.monotonic() - started) * 1000))
    return res


def callpath_state(version: str, src: str, dst: str, *, max_depth: int = 6, k: int = 5, exclude: tuple[str, ...] = (),
                   indirect: bool = False, arch: str = "", wait: float = 2.0) -> dict[str, Any]:
    """The search, once the revision's source index is in memory ({state: "done", result}); the index job's state
    (202 with progress) while it is assembled. The search itself takes milliseconds to a few seconds."""
    from fastapi import HTTPException
    from webapp.backend.security.sql import is_valid_identifier
    from webapp.backend.services.kapi_service import resolve
    from webapp.backend.services.source_index import revision_state
    for name in (src, dst, *exclude):
        if not is_valid_identifier(name):
            raise HTTPException(status_code=400, detail=f"Not a C identifier: {name[:60]!r}")
    if arch and not is_valid_identifier(arch):
        raise HTTPException(status_code=400, detail="Bad ARCH")
    sha = resolve(version)
    state = revision_state(sha, wait=wait)
    if state.get("state") != "done":
        return state
    arch_name, test = arch_filter(version, arch)
    res = find_call_paths(state["index"], src, dst, arch_name=arch_name, allowed_test=test, max_depth=max(1, min(int(max_depth), 10)),
                          k=max(1, min(int(k), 20)), exclude=exclude, indirect=indirect)
    return {"state": "done", "result": {"version": version, "from": src, "to": dst, **res}}
