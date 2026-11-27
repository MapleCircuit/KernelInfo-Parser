"""webapp/backend/services/nodemap_nesting.py - Pure helpers of nested NodeMap references (`nodemap` nodes).

A `nodemap` node of a saved canvas references another saved canvas of the same repository by
`data.canvasId`. The reference graph (canvas id -> referenced canvas ids) must stay acyclic, parent
reads carry a bounded static preview of each referenced canvas instead of its document, and grouping
moves a selection of nodes into a new child canvas. Everything here is DOM-, disk- and HTTP-free; the
persistence side (reference index, locks, writes) lives in `nodemap_canvas_service.py`.
"""
from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from webapp.backend.services.nodemap_canvas_schema import (
    SIZE_LIMITS,
    bounds_of,
    group_descendants,
    paint_order,
)

NESTED_NODE_TYPE = "nodemap"
PREVIEW_SCALE = 1000
PREVIEW_MAX_NODES = 200
PREVIEW_MAX_EDGES = 400
HIERARCHY_DEPTH_DEFAULT = 8
HIERARCHY_DEPTH_MAX = 32
HIERARCHY_MAX_ENTRIES = 500
GROUP_ORIGIN = 80
GROUP_FRAME_MAX = {"w": 640, "h": 480}


def nested_nodes(canvas: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The `nodemap` nodes of `canvas` in document order."""
    return [n for n in ((canvas or {}).get("nodes") or []) if n.get("type") == NESTED_NODE_TYPE]


def nested_refs(canvas: Mapping[str, Any] | None) -> list[str]:
    """Sorted distinct canvas ids referenced by the `nodemap` nodes of `canvas` (unset references skipped)."""
    return sorted({str((n.get("data") or {}).get("canvasId") or "") for n in nested_nodes(canvas)} - {""})


def canvas_counts(canvas: Mapping[str, Any] | None) -> dict[str, int]:
    """`{"nodes", "edges", "nested"}` counts of a canvas document (`nested`: its `nodemap` nodes)."""
    c = canvas or {}
    return {
        "nodes": len(c.get("nodes") or []),
        "edges": len(c.get("edges") or []),
        "nested": len(nested_nodes(c)),
    }


def build_static_preview(canvas: Mapping[str, Any] | None) -> dict[str, Any]:
    """Bounded read-only geometry of a canvas: node boxes and center-to-center edge segments.

    Coordinates are integers in a `PREVIEW_SCALE` box keeping the aspect ratio of the node bounds
    (`w`, `h` give the extent). Boxes follow paint order (groups first) and stop at `PREVIEW_MAX_NODES`;
    segments join two previewed boxes and stop at `PREVIEW_MAX_EDGES`. No title, data or code content.
    """
    nodes = list((canvas or {}).get("nodes") or [])
    edges = list((canvas or {}).get("edges") or [])
    b = bounds_of(nodes)
    if not b:
        return {"w": 0, "h": 0, "nodes": [], "edges": [], "truncated": False}
    k = PREVIEW_SCALE / max(b["w"], b["h"], 1)
    ordered = paint_order(nodes)
    shown = ordered[:PREVIEW_MAX_NODES]
    centers: dict[str, tuple[int, int]] = {}
    boxes: list[dict[str, Any]] = []
    for n in shown:
        x = round((n["x"] - b["x"]) * k)
        y = round((n["y"] - b["y"]) * k)
        w = max(1, round(n["w"] * k))
        h = max(1, round(n["h"] * k))
        centers[n["id"]] = (x + w // 2, y + h // 2)
        boxes.append({"x": x, "y": y, "w": w, "h": h, "type": n.get("type") or "", "color": n.get("color") or ""})
    segs: list[dict[str, Any]] = []
    for e in edges:
        a = centers.get(e.get("from"))
        z = centers.get(e.get("to"))
        if a is None or z is None:
            continue
        if len(segs) >= PREVIEW_MAX_EDGES:
            break
        segs.append({"x1": a[0], "y1": a[1], "x2": z[0], "y2": z[1], "color": e.get("color") or "", "style": e.get("style") or "solid"})
    linkable = sum(1 for e in edges if e.get("from") in centers and e.get("to") in centers)
    return {
        "w": round(b["w"] * k),
        "h": round(b["h"] * k),
        "nodes": boxes,
        "edges": segs,
        "truncated": len(shown) < len(nodes) or len(segs) < linkable,
    }


def find_cycle(graph: Mapping[str, Iterable[str]], canvas_id: str, refs: Iterable[str]) -> list[str] | None:
    """The reference path `[canvas_id, ..., canvas_id]` that `canvas_id -> refs` would close, else None.

    `graph` maps every other stored canvas id to the ids it references (`canvas_id`'s own stored entry is
    ignored: `refs` replaces it). Missing targets end a path. Iterative DFS, so depth is unbounded.
    """
    targets = sorted(set(refs))
    if canvas_id in targets:
        return [canvas_id, canvas_id]
    parent: dict[str, str] = {}
    stack: list[str] = []
    for t in targets:
        if t not in parent:
            parent[t] = canvas_id
            stack.append(t)
    while stack:
        cur = stack.pop()
        for r in sorted(set(graph.get(cur) or ())):
            if r == canvas_id:
                path = [cur]
                while parent[path[-1]] != canvas_id:
                    path.append(parent[path[-1]])
                return [canvas_id, *reversed(path), canvas_id]
            if r not in parent:
                parent[r] = cur
                stack.append(r)
    return None


def ancestors_of(graph: Mapping[str, Iterable[str]], canvas_id: str) -> set[str]:
    """Every stored canvas id from which `canvas_id` is reachable through references (excluding itself)."""
    reverse: dict[str, set[str]] = {}
    for src, refs in graph.items():
        for r in refs:
            reverse.setdefault(r, set()).add(src)
    out: set[str] = set()
    stack = [canvas_id]
    while stack:
        cur = stack.pop()
        for p in reverse.get(cur, ()):
            if p not in out and p != canvas_id:
                out.add(p)
                stack.append(p)
    return out


def _clamp(v: float, lo: float, hi: float) -> int:
    return round(min(max(v, lo), hi))


def split_for_group(canvas: dict[str, Any], node_ids: Iterable[str], nested_node_id: str) -> dict[str, Any]:
    """Partition `canvas` for grouping `node_ids` into a child canvas behind node `nested_node_id`.

    The selection grows by the descendants of selected groups. Returns `{"moved_ids", "child_nodes",
    "child_edges", "parent_nodes", "parent_edges", "rewired_edge_ids", "bounds", "group_id"}`:
    child nodes are translated so their bounds start at `GROUP_ORIGIN` (internal edge waypoints too) and
    lose a `groupId` outside the selection; edges with both ends inside move to the child; edges with one
    end inside stay in the parent with that end rewired to `nested_node_id` (its field port and waypoints
    dropped). `group_id` is the group shared by every top-level moved node (the nested node joins it).
    Inputs are not mutated.
    """
    nodes = canvas.get("nodes") or []
    by_id = {n["id"]: n for n in nodes}
    picked = [i for i in dict.fromkeys(str(x) for x in node_ids) if i in by_id]
    moved: set[str] = set(picked)
    for nid in picked:
        if by_id[nid].get("type") == "group":
            moved.update(d["id"] for d in group_descendants(nodes, nid))
    moved_nodes = [copy.deepcopy(n) for n in nodes if n["id"] in moved]
    b = bounds_of(moved_nodes) or {"x": GROUP_ORIGIN, "y": GROUP_ORIGIN, "w": 0, "h": 0}
    dx = GROUP_ORIGIN - b["x"]
    dy = GROUP_ORIGIN - b["y"]
    outer_groups = {n.get("groupId") for n in moved_nodes if n.get("groupId") not in moved}
    for n in moved_nodes:
        n["x"] += dx
        n["y"] += dy
        if n.get("groupId") not in moved:
            n["groupId"] = None
    child_edges: list[dict[str, Any]] = []
    parent_edges: list[dict[str, Any]] = []
    rewired: list[str] = []
    for e in canvas.get("edges") or []:
        src_in = e.get("from") in moved
        dst_in = e.get("to") in moved
        if src_in and dst_in:
            ce = copy.deepcopy(e)
            ce["waypoints"] = [{"x": w["x"] + dx, "y": w["y"] + dy} for w in (ce.get("waypoints") or [])]
            child_edges.append(ce)
        elif src_in or dst_in:
            pe = copy.deepcopy(e)
            if src_in:
                pe["from"] = nested_node_id
                pe["fromField"] = None
            else:
                pe["to"] = nested_node_id
                pe["toField"] = None
            pe["waypoints"] = []
            parent_edges.append(pe)
            rewired.append(pe["id"])
        else:
            parent_edges.append(copy.deepcopy(e))
    shared = next(iter(outer_groups)) if len(outer_groups) == 1 else None
    return {
        "moved_ids": [n["id"] for n in nodes if n["id"] in moved],
        "child_nodes": moved_nodes,
        "child_edges": child_edges,
        "parent_nodes": [copy.deepcopy(n) for n in nodes if n["id"] not in moved],
        "parent_edges": parent_edges,
        "rewired_edge_ids": rewired,
        "bounds": b,
        "group_id": shared,
    }


def nested_node_frame(bounds: Mapping[str, int]) -> dict[str, int]:
    """`{x, y, w, h}` of a nested node replacing a selection with `bounds` (size clamped to `GROUP_FRAME_MAX`)."""
    lim = SIZE_LIMITS[NESTED_NODE_TYPE]
    return {
        "x": int(bounds["x"]),
        "y": int(bounds["y"]),
        "w": _clamp(bounds["w"] or lim["w"], lim["minW"], GROUP_FRAME_MAX["w"]),
        "h": _clamp(bounds["h"] or lim["h"], lim["minH"], GROUP_FRAME_MAX["h"]),
    }


def remap_tours(tours: list[dict[str, Any]], moved: set[str], nested_node_id: str, edge_ids: set[str]) -> list[dict[str, Any]]:
    """Tours with moved node targets replaced by `nested_node_id` (deduplicated) and edge targets kept in `edge_ids`."""
    out = copy.deepcopy(tours or [])
    for t in out:
        for s in t.get("steps") or []:
            nids: list[str] = []
            for nid in s.get("nodeIds") or []:
                v = nested_node_id if nid in moved else nid
                if v not in nids:
                    nids.append(v)
            s["nodeIds"] = nids
            s["edgeIds"] = [e for e in (s.get("edgeIds") or []) if e in edge_ids]
    return out
