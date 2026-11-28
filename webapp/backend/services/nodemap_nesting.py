"""webapp/backend/services/nodemap_nesting.py - Pure helpers of nested NodeMaps (`nodemap` nodes).

A `nodemap` node embeds its child canvas inline in `data.canvas` (portable format of the public API, MCP
and the webapp JSON import/export; arbitrary depth up to `NESTED_DEPTH_MAX`). Parent edges attach to the
nested node as a whole, never to a node inside it. Responses annotate every nested node with a computed
`data.summary` (title, counts, depth below) and, on request, a bounded static `data.preview`. Pre-existing
server documents may still name another stored canvas by `data.canvasId`; `inline_references` resolves such
references into inline children (read-only). Grouping moves a selection into a new inline child.
Everything here is DOM-, disk- and HTTP-free: invalid inline trees raise `NestingError`.
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

from webapp.backend.services.nodemap_canvas_schema import (
    MAX_SUBCANVAS_DEPTH,
    SIZE_LIMITS,
    bounds_of,
    display_title,
    empty_canvas,
    fit_viewport,
    group_descendants,
    is_valid_id,
    new_id,
    normalize_canvas,
    normalize_node,
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
# Inline nesting bounds: levels below the root canvas (shared with the Sub-Map embedding budget of the
# schema) and nodes across the whole inline tree
NESTED_DEPTH_MAX = MAX_SUBCANVAS_DEPTH
NESTED_TOTAL_NODES_MAX = 20000
_PROBLEMS_MAX = 50
# Node fields a caller may set on a new nested node
_NODE_SPEC_KEYS = ("id", "x", "y", "w", "h", "customTitle", "color", "bgColor", "groupId", "layerId")


class NestingError(ValueError):
    """An invalid inline nesting request; `problems` lists `{"code", "message", "path", ...}` entries."""

    def __init__(self, problems: list[dict[str, Any]]) -> None:
        self.problems = problems[:_PROBLEMS_MAX]
        msgs = [p["message"] for p in self.problems[:5]]
        more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        super().__init__("; ".join(msgs) + more)


def _problem(code: str, message: str, path: Iterable[str], **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "path": list(path), **extra}


def nested_nodes(canvas: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The `nodemap` nodes of `canvas` in document order."""
    return [n for n in ((canvas or {}).get("nodes") or []) if n.get("type") == NESTED_NODE_TYPE]


def nested_refs(canvas: Mapping[str, Any] | None) -> list[str]:
    """Sorted distinct reference ids (`data.canvasId`) of the non-inline `nodemap` nodes of `canvas`."""
    return sorted(
        {str((n.get("data") or {}).get("canvasId") or "") for n in nested_nodes(canvas) if not inline_child(n)} - {""}
    )


def inline_child(node: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The inline child canvas (`data.canvas`) of a `nodemap` node, else None."""
    if not isinstance(node, Mapping) or node.get("type") != NESTED_NODE_TYPE:
        return None
    data = node.get("data")
    child = data.get("canvas") if isinstance(data, Mapping) else None
    return child if isinstance(child, dict) else None


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


# ---------------------------------------------------------------------- inline trees


def inline_problems(
    raw: Any,
    *,
    max_depth: int = NESTED_DEPTH_MAX,
    max_total_nodes: int = NESTED_TOTAL_NODES_MAX,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(errors, warnings)` of a raw (not yet normalized) inline canvas tree.

    Errors: `nested_depth_exceeded` (a child deeper than `max_depth` levels), `nested_cycle` (a canvas
    object inside its own subtree, possible only for in-memory input), `nested_too_large` (more than
    `max_total_nodes` nodes across the tree) and `internal_edge_target` (an edge naming a node that exists
    only inside a nested child of its canvas). Warnings: `unresolved_reference` (a `nodemap` node with a
    `data.canvasId` and no inline canvas). `path` is the chain of nested node ids from the root.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    total = 0
    active: set[int] = set()

    def walk(canvas: Mapping[str, Any], path: tuple[str, ...], depth: int) -> set[str]:
        nonlocal total
        key = id(canvas)
        if key in active:
            errors.append(_problem("nested_cycle", f"Nested NodeMap cycle at {'/'.join(path) or 'root'}.", path))
            return set()
        active.add(key)
        raw_nodes = canvas.get("nodes")
        nodes = [n for n in raw_nodes if isinstance(n, Mapping)] if isinstance(raw_nodes, list) else []
        total += len(nodes)
        own = {str(n.get("id")) for n in nodes if n.get("id") is not None}
        below: set[str] = set()
        for n in nodes:
            if n.get("type") != NESTED_NODE_TYPE:
                continue
            nid = str(n.get("id") or "")
            data = n.get("data") if isinstance(n.get("data"), Mapping) else {}
            child = data.get("canvas")
            if isinstance(child, Mapping):
                if depth + 1 > max_depth:
                    errors.append(_problem(
                        "nested_depth_exceeded",
                        f"Nested NodeMap '{'/'.join((*path, nid))}' exceeds the maximum depth {max_depth}.",
                        (*path, nid),
                    ))
                    continue
                below |= walk(child, (*path, nid), depth + 1)
            elif data.get("canvasId"):
                warnings.append(_problem(
                    "unresolved_reference",
                    f"Nested NodeMap '{nid}' references canvas '{data.get('canvasId')}' without an inline canvas.",
                    path, node_id=nid, canvas_id=str(data.get("canvasId")),
                ))
        raw_edges = canvas.get("edges")
        for e in raw_edges if isinstance(raw_edges, list) else []:
            if not isinstance(e, Mapping):
                continue
            for end in ("from", "to"):
                v = e.get(end)
                if v is not None and str(v) not in own and str(v) in below:
                    errors.append(_problem(
                        "internal_edge_target",
                        f"Edge '{e.get('id')}' {end} '{v}' targets a node inside a nested NodeMap; "
                        "edges attach to the nested node as a whole.",
                        path, edge_id=str(e.get("id") or ""), node_id=str(v),
                    ))
        active.discard(key)
        return own | below

    if isinstance(raw, Mapping):
        walk(raw, (), 0)
    if total > max_total_nodes:
        errors.append(_problem(
            "nested_too_large",
            f"Nested NodeMap tree holds {total} nodes (maximum {max_total_nodes}).",
            (),
        ))
    return errors[:_PROBLEMS_MAX], warnings[:_PROBLEMS_MAX]


def check_inline(raw: Any) -> list[dict[str, Any]]:
    """Warnings of a raw inline canvas tree; raises `NestingError` when it has errors."""
    errors, warnings = inline_problems(raw)
    if errors:
        raise NestingError(errors)
    return warnings


def iter_inline(canvas: Mapping[str, Any]) -> Iterator[tuple[tuple[str, ...], dict[str, Any]]]:
    """`(path, canvas)` of the root and every inline child canvas, pre-order (normalized trees only)."""
    stack: list[tuple[tuple[str, ...], Any]] = [((), canvas)]
    while stack:
        path, cur = stack.pop()
        yield path, cur
        kids = [(n["id"], c) for n in nested_nodes(cur) if (c := inline_child(n)) is not None]
        for nid, child in reversed(kids):
            stack.append(((*path, nid), child))


def tree_stats(canvas: Mapping[str, Any]) -> dict[str, int]:
    """`{"nested_count", "nested_depth", "total_node_count"}` of a normalized inline tree."""
    nested = depth = total = 0
    for path, cur in iter_inline(canvas):
        depth = max(depth, len(path))
        total += len(cur.get("nodes") or [])
        nested += len(nested_nodes(cur))
    return {"nested_count": nested, "nested_depth": depth, "total_node_count": total}


def annotate_nested(canvas: dict[str, Any], *, include_preview: bool = False) -> int:
    """Set `data.summary` (and `data.preview` when `include_preview`) on every nested node, in place.

    `summary` = `{"title", "nodes", "edges", "nested", "depth"}` of the child canvas (`depth`: nested levels
    below it). Reference-only nodes lose stale annotations. Returns the nested depth below `canvas`.
    """
    deepest = 0
    for n in nested_nodes(canvas):
        data = n.setdefault("data", {})
        data.pop("summary", None)
        data.pop("preview", None)
        child = inline_child(n)
        if child is None:
            continue
        below = annotate_nested(child, include_preview=include_preview)
        data["summary"] = {"title": display_title(n), **canvas_counts(child), "depth": below}
        if include_preview:
            data["preview"] = build_static_preview(child)
        deepest = max(deepest, below + 1)
    return deepest


def canvas_at_path(root: dict[str, Any], path: Iterable[Any] | None) -> dict[str, Any]:
    """The inline canvas reached from `root` through the nested node ids of `path` (NestingError if absent)."""
    cur = root
    walked: list[str] = []
    for raw_id in path or []:
        nid = str(raw_id)
        node = next((n for n in cur.get("nodes") or [] if n.get("id") == nid), None)
        child = inline_child(node)
        if child is None:
            raise NestingError([_problem(
                "path_not_found",
                f"Nested NodeMap path '{'/'.join((*walked, nid))}' does not name an inline nested node.",
                (*walked, nid),
            )])
        walked.append(nid)
        cur = child
    return cur


def _node_spec(spec: Any) -> dict[str, Any]:
    return {k: spec[k] for k in _NODE_SPEC_KEYS if isinstance(spec, Mapping) and k in spec}


def attach_inline(
    host: dict[str, Any],
    child: dict[str, Any] | None,
    *,
    spec: Mapping[str, Any] | None = None,
    title: str = "",
    version: str = "v3.0",
    repo: str = "linux",
) -> dict[str, Any]:
    """Append a nested node embedding `child` (an empty canvas when None) to the normalized `host`, in place.

    `spec` sets `id`, `x`, `y`, `w`, `h`, `customTitle`, `color`, `bgColor`, `groupId`, `layerId` of the new
    node (a taken or invalid `id` is a `NestingError`). Returns the normalized new node.
    """
    raw = _node_spec(spec)
    taken = {n["id"] for n in host.get("nodes") or []}
    if "id" in raw and (not is_valid_id(raw["id"]) or raw["id"] in taken):
        raise NestingError([_problem("node_id_taken", f"Node id '{raw['id']}' is invalid or already used.", ())])
    nid = raw.pop("id", None) or new_id("n")
    while nid in taken:
        nid = new_id("n")
    node = normalize_node({
        **raw,
        "id": nid,
        "type": NESTED_NODE_TYPE,
        "title": str(title or "").strip()[:256] or "Nested NodeMap",
        "version": version,
        "repo": repo,
        "data": {"canvas": child if isinstance(child, dict) else empty_canvas()},
    })
    assert node is not None
    host.setdefault("nodes", []).append(node)
    return node


def group_inline(
    host: dict[str, Any],
    node_ids: Iterable[Any],
    *,
    spec: Mapping[str, Any] | None = None,
    title: str = "",
    version: str = "v3.0",
    repo: str = "linux",
) -> dict[str, Any]:
    """Move `node_ids` (plus descendants of selected groups) of the normalized `host` into a new inline child.

    The child receives the moved nodes and the edges between them; the host gets a nested node at the
    selection's place, every edge crossing the selection boundary is rewired to it and tour steps targeting
    moved nodes target it. Mutates `host`; returns `{"node", "moved_ids", "moved_edge_ids",
    "rewired_edge_ids"}` (`NestingError` for an empty or unknown selection).
    """
    ids = [str(x) for x in node_ids or []]
    known = {n["id"] for n in host.get("nodes") or []}
    if not ids:
        raise NestingError([_problem("empty_selection", "node_ids must be a non-empty list of node ids.", ())])
    unknown = sorted(set(ids) - known)
    if unknown:
        raise NestingError([_problem("unknown_nodes", f"Unknown node ids: {', '.join(unknown[:20])}.", (), node_ids=unknown[:50])])
    raw = _node_spec(spec)
    nid = raw.get("id") if is_valid_id(raw.get("id")) and raw.get("id") not in known else new_id("n")
    while nid in known:
        nid = new_id("n")
    split = split_for_group(host, ids, nid)
    child = normalize_canvas({
        **empty_canvas(),
        "nodes": split["child_nodes"],
        "edges": split["child_edges"],
        "settings": copy.deepcopy(host.get("settings") or {}),
        "viewport": fit_viewport(bounds_of(split["child_nodes"])),
    }) or empty_canvas()
    node = normalize_node({
        **{k: raw[k] for k in ("customTitle", "color", "bgColor", "layerId") if k in raw},
        **nested_node_frame(split["bounds"]),
        "id": nid,
        "type": NESTED_NODE_TYPE,
        "title": str(title or "").strip()[:256] or "Grouped NodeMap",
        "groupId": split["group_id"],
        "version": version,
        "repo": repo,
        "data": {"canvas": child},
    })
    assert node is not None
    moved = set(split["moved_ids"])
    host["nodes"] = [*split["parent_nodes"], node]
    host["edges"] = split["parent_edges"]
    host["tours"] = remap_tours(host.get("tours") or [], moved, nid, {e["id"] for e in split["parent_edges"]})
    return {
        "node": node,
        "moved_ids": split["moved_ids"],
        "moved_edge_ids": [e["id"] for e in split["child_edges"]],
        "rewired_edge_ids": split["rewired_edge_ids"],
    }


def inline_references(
    canvas: dict[str, Any],
    resolve: Callable[[str], dict[str, Any] | None],
    *,
    root_id: str | None = None,
    max_depth: int = NESTED_DEPTH_MAX,
    max_total_nodes: int = NESTED_TOTAL_NODES_MAX,
) -> list[dict[str, Any]]:
    """Replace every reference node (`data.canvasId`) of a normalized tree by an inline copy, in place.

    `resolve(canvas_id)` returns the referenced stored canvas (normalized) or None. A missing target, a
    reference closing a cycle (`root_id` and the reference chain above), one beyond `max_depth` or one whose
    copy would push the tree past `max_total_nodes` becomes an unset nested node (`data: {}`, a
    `highQualityPreview` flag kept) and a warning
    `{"code": "missing_reference" | "reference_cycle" | "nested_depth_exceeded" | "nested_too_large",
    "node_id", "canvas_id", "path"}` (`path`: canvas ids from the root).
    """
    warnings: list[dict[str, Any]] = []
    total = tree_stats(canvas)["total_node_count"]
    stack: list[tuple[dict[str, Any], tuple[str, ...], int]] = [(canvas, (root_id,) if root_id else (), 0)]
    while stack:
        cur, chain, depth = stack.pop()
        for n in nested_nodes(cur):
            child = inline_child(n)
            if child is not None:
                stack.append((child, chain, depth + 1))
                continue
            opts = {"highQualityPreview": True} if (n.get("data") or {}).get("highQualityPreview") is True else {}
            cid = str((n.get("data") or {}).get("canvasId") or "")
            if not cid:
                n["data"] = dict(opts)
                continue
            code = ""
            target = None
            if cid in chain:
                code = "reference_cycle"
            elif depth + 1 > max_depth:
                code = "nested_depth_exceeded"
            else:
                target = resolve(cid)
                if target is None:
                    code = "missing_reference"
                else:
                    size = tree_stats(target)["total_node_count"]
                    if total + size > max_total_nodes:
                        code = "nested_too_large"
                    else:
                        total += size
            if code:
                n["data"] = dict(opts)
                warnings.append(_problem(code, f"Nested NodeMap '{n['id']}' -> '{cid}': {code.replace('_', ' ')}.",
                                         chain, node_id=n["id"], canvas_id=cid))
                continue
            inlined = copy.deepcopy(target)
            n["data"] = {"canvas": inlined, **opts}
            stack.append((inlined, (*chain, cid), depth + 1))
    return warnings[:_PROBLEMS_MAX]


def inline_tree(
    canvas: Mapping[str, Any],
    *,
    depth: int = HIERARCHY_DEPTH_DEFAULT,
    max_entries: int = HIERARCHY_MAX_ENTRIES,
) -> dict[str, Any]:
    """Nested tree of a normalized inline canvas without node/edge payloads.

    Returns `{"tree", "entries", "truncated"}`; a tree entry is `{"counts", "depth", "children"}` (children
    add `node_id` and `title`; `children_truncated` marks a cut below `depth` levels or `max_entries`).
    """
    budget = {"left": max_entries, "truncated": False}

    def entry(cur: Mapping[str, Any], levels: int) -> dict[str, Any]:
        budget["left"] -= 1
        out: dict[str, Any] = {"counts": canvas_counts(cur), "depth": 0, "children": []}
        kids = [(n, c) for n in nested_nodes(cur) if (c := inline_child(n)) is not None]
        deepest = 0
        for n, child in kids:
            deepest = max(deepest, annotate_depth(child) + 1)
        out["depth"] = deepest
        if not kids:
            return out
        if levels <= 0:
            out["children_truncated"] = True
            return out
        for n, child in kids:
            if budget["left"] <= 0:
                budget["truncated"] = True
                out["children_truncated"] = True
                break
            out["children"].append({"node_id": n["id"], "title": display_title(n), **entry(child, levels - 1)})
        return out

    tree = entry(canvas, int(depth))
    return {"tree": tree, "entries": max_entries - budget["left"], "truncated": budget["truncated"]}


def annotate_depth(canvas: Mapping[str, Any]) -> int:
    """Nested levels below a normalized inline canvas (0 for a canvas without inline children)."""
    return max((len(path) for path, _ in iter_inline(canvas)), default=0)
