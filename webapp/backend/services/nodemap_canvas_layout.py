"""webapp/backend/services/nodemap_canvas_layout.py - Deterministic NodeMap layout and diagram exporters.

Ports `webapp/frontend/js/views/nodemap/nodemap_worker.js` (`"dag"` Sugiyama layered layout and `"force"`
force-directed layout) and `webapp/frontend/js/views/nodemap/nodemap_export_formats.js` (`"json"`,
`"mermaid"`, and `"dot"` exporters) to pure Python.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from webapp.backend.services.nodemap_canvas_schema import (
    GROUP_LAYOUT_PAD,
    NODE_BADGES,
    SIZE_LIMITS,
    _clamp,
    _js_round,
    bounds_of,
    display_title,
    fit_group_to_members,
    format_lock_badge,
    paint_order,
    serialize_canvas,
    visible_canvas_slice,
)

DEFAULT_W = 300
DEFAULT_H = 200
MERMAID_DIRS = frozenset({"LR", "RL", "TB", "TD", "BT"})
DOT_DIRS = frozenset({"LR", "RL", "TB", "BT"})
MAX_FIELD_LOCKS_SHOWN = 12


def _node_width(n: dict[str, Any]) -> float:
    w = n.get("w")
    if not isinstance(w, bool) and isinstance(w, (int, float)) and math.isfinite(w) and w > 0:
        return float(w)
    return float(DEFAULT_W)


def _node_height(n: dict[str, Any]) -> float:
    h = n.get("h")
    if not isinstance(h, bool) and isinstance(h, (int, float)) and math.isfinite(h) and h > 0:
        return float(h)
    return float(DEFAULT_H)


def estimate_node_dimensions(node: dict[str, Any], hydrated_meta: dict[str, Any] | None = None) -> tuple[int, int]:
    """Estimate rendered `(w, h)` for `node` prior to layout using content metadata when available."""
    ntype = str(node.get("type") or "note")
    lim = SIZE_LIMITS.get(ntype, SIZE_LIMITS["note"])
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    meta = hydrated_meta or {}

    w = int(_clamp(int(_node_width(node)), lim["minW"], lim["maxW"]))
    h = int(_clamp(int(_node_height(node)), lim["minH"], lim["maxH"]))

    # Only adjust default height when caller did not set a custom height or when hydration supplied counts
    default_h = lim["h"]
    if ntype == "struct":
        member_count = meta.get("member_count")
        if isinstance(member_count, int) and member_count > 0:
            h = int(_clamp(72 + member_count * 24, lim["minH"], min(900, lim["maxH"])))
    elif ntype == "source":
        line_count = meta.get("line_count")
        if line_count is None and isinstance(data.get("start"), int) and isinstance(data.get("end"), int):
            line_count = max(1, data["end"] - data["start"] + 1)
        if isinstance(line_count, int) and line_count > 0:
            h = int(_clamp(64 + min(line_count, 45) * 18, lim["minH"], min(960, lim["maxH"])))
    elif ntype == "note" and h == default_h:
        text = str(data.get("text") or "")
        if text:
            lines = sum(max(1, math.ceil(len(line) / 36)) for line in text.splitlines()) or 1
            h = int(_clamp(60 + lines * 20, lim["minH"], min(720, lim["maxH"])))
    elif ntype == "symbol" and data.get("expanded") and isinstance(data.get("constituents"), list):
        count = len(data["constituents"])
        if count > 0:
            h = int(_clamp(84 + min(count, 25) * 22, lim["minH"], min(720, lim["maxH"])))

    node["w"] = w
    node["h"] = h
    if not node.get("autoFit"):
        node["baseW"] = w
        node["baseH"] = h
    return w, h


def _collect_directed_edges(active: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[tuple[int, int]]:
    idx_by_id: dict[str, int] = {}
    for i, node in enumerate(active):
        nid = str(node.get("id") or "")
        if nid and nid not in idx_by_id:
            idx_by_id[nid] = i
    out: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for e in edges or []:
        if not isinstance(e, dict):
            continue
        u = idx_by_id.get(str(e.get("from") or ""))
        v = idx_by_id.get(str(e.get("to") or ""))
        if u is None or v is None or u == v:
            continue
        pair = (u, v)
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def layout_dag(
    active: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Sugiyama layered DAG layout (`nodemap_worker.js:layoutDag`)."""
    n = len(active)
    if n == 0:
        return []
    opts = options or {}
    dir_edges = _collect_directed_edges(active, edges)
    raw_out: list[list[int]] = [[] for _ in range(n)]
    for u, v in dir_edges:
        raw_out[u].append(v)

    # 1. Iterative 3-color DFS cycle breaking
    state = [0] * n  # 0=unvisited, 1=visiting, 2=done
    dag_edges: list[tuple[int, int]] = []
    for start in range(n):
        if state[start] != 0:
            continue
        state[start] = 1
        stack: list[list[int]] = [[start, 0]]
        while stack:
            top = stack[-1]
            u, k = top[0], top[1]
            nbrs = raw_out[u]
            if k >= len(nbrs):
                state[u] = 2
                stack.pop()
                continue
            v = nbrs[k]
            top[1] = k + 1
            if state[v] == 1:
                continue  # back-edge
            dag_edges.append((u, v))
            if state[v] == 0:
                state[v] = 1
                stack.append([v, 0])

    # 2. Longest-path layer assignment in topological order
    dag_out: list[list[int]] = [[] for _ in range(n)]
    dag_in: list[list[int]] = [[] for _ in range(n)]
    in_deg = [0] * n
    for u, v in dag_edges:
        dag_out[u].append(v)
        dag_in[v].append(u)
        in_deg[v] += 1

    layer = [0] * n
    queue = [i for i in range(n) if in_deg[i] == 0]
    head = 0
    while head < len(queue):
        u = queue[head]
        head += 1
        next_l = layer[u] + 1
        for v in dag_out[u]:
            if layer[v] < next_l:
                layer[v] = next_l
            in_deg[v] -= 1
            if in_deg[v] == 0:
                queue.append(v)

    # 3. 4 sweeps of barycenter crossing minimization
    max_layer = max(layer, default=0)
    layers: list[list[int]] = [[] for _ in range(max_layer + 1)]
    pos_in_layer = [0] * n
    for i in range(n):
        lvl = layer[i]
        pos_in_layer[i] = len(layers[lvl])
        layers[lvl].append(i)

    bary = [0.0] * n

    def sort_layer(lvl: int, adj_layer: int, use_in: bool) -> None:
        row = layers[lvl]
        if len(row) <= 1:
            return
        for u in row:
            nbrs = dag_in[u] if use_in else dag_out[u]
            total = 0
            cnt = 0
            for v in nbrs:
                if layer[v] == adj_layer:
                    total += pos_in_layer[v]
                    cnt += 1
            bary[u] = (total / cnt) if cnt > 0 else float(pos_in_layer[u])
        row.sort(key=lambda idx: (bary[idx], pos_in_layer[idx]))
        for pos, idx in enumerate(row):
            pos_in_layer[idx] = pos

    for sweep in range(4):
        if sweep % 2 == 0:
            for lvl in range(1, max_layer + 1):
                sort_layer(lvl, lvl - 1, True)
        else:
            for lvl in range(max_layer - 1, -1, -1):
                sort_layer(lvl, lvl + 1, False)

    # 4. Left-to-right column placement starting at (80, 80)
    gap_x = float(opts["gapX"]) if isinstance(opts.get("gapX"), (int, float)) and not isinstance(opts.get("gapX"), bool) else 100.0
    gap_y = float(opts["gapY"]) if isinstance(opts.get("gapY"), (int, float)) and not isinstance(opts.get("gapY"), bool) else 40.0
    pos_map: list[dict[str, Any]] = [{} for _ in range(n)]
    x = 80.0
    for col in layers:
        if not col:
            continue
        y = 80.0
        max_w = 0.0
        for idx in col:
            w = _node_width(active[idx])
            h = _node_height(active[idx])
            pos_map[idx] = {"id": active[idx]["id"], "x": _js_round(x), "y": _js_round(y)}
            y += h + gap_y
            if w > max_w:
                max_w = w
        x += max_w + gap_x
    return pos_map


def _create_spiral_stepper():
    x = 0
    y = 0
    dx = 1
    dy = 0
    seg_len = 1
    passed = 0

    def step() -> tuple[int, int]:
        nonlocal x, y, dx, dy, seg_len, passed
        pt = (x, y)
        x += dx
        y += dy
        passed += 1
        if passed == seg_len:
            passed = 0
            dx, dy = -dy, dx
            if dy == 0:
                seg_len += 1
        return pt

    return step


def layout_force(
    active: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic force-directed layout (`nodemap_worker.js:layoutForce`)."""
    n = len(active)
    if n == 0:
        return []
    opts = options or {}
    gap_x = float(opts["gapX"]) if isinstance(opts.get("gapX"), (int, float)) and not isinstance(opts.get("gapX"), bool) else 100.0
    gap_y = float(opts["gapY"]) if isinstance(opts.get("gapY"), (int, float)) and not isinstance(opts.get("gapY"), bool) else 40.0
    raw_iter = opts.get("iterations")
    iterations = max(0, int(raw_iter)) if isinstance(raw_iter, (int, float)) and not isinstance(raw_iter, bool) else 80

    w = [_node_width(node) for node in active]
    h = [_node_height(node) for node in active]
    hw = [val * 0.5 for val in w]
    hh = [val * 0.5 for val in h]
    avg_w = sum(w) / n
    avg_h = sum(h) / n
    cell_w = avg_w + gap_x
    cell_h = avg_h + gap_y
    k_base = math.hypot(cell_w, cell_h)

    cx = [0.0] * n
    cy = [0.0] * n
    used: set[str] = set()
    next_spiral = _create_spiral_stepper()
    anchor_x = 0.0
    anchor_y = 0.0
    for node in active:
        nx, ny = node.get("x"), node.get("y")
        if (
            not isinstance(nx, bool)
            and not isinstance(ny, bool)
            and isinstance(nx, (int, float))
            and isinstance(ny, (int, float))
            and math.isfinite(nx)
            and math.isfinite(ny)
        ):
            anchor_x = float(nx)
            anchor_y = float(ny)
            break

    for i, node in enumerate(active):
        nx, ny = node.get("x"), node.get("y")
        has_coords = (
            not isinstance(nx, bool)
            and not isinstance(ny, bool)
            and isinstance(nx, (int, float))
            and isinstance(ny, (int, float))
            and math.isfinite(nx)
            and math.isfinite(ny)
        )
        x = float(nx) if has_coords else 0.0
        y = float(ny) if has_coords else 0.0
        key = f"{_js_round(x)},{_js_round(y)}"
        if not has_coords or key in used:
            while True:
                sx, sy = next_spiral()
                x = anchor_x + sx * cell_w
                y = anchor_y + sy * cell_h
                key = f"{_js_round(x)},{_js_round(y)}"
                if key not in used:
                    break
        used.add(key)
        cx[i] = x + hw[i]
        cy[i] = y + hh[i]

    dir_edges = _collect_directed_edges(active, edges)
    undir_edges: list[tuple[int, int]] = []
    seen_undir: set[tuple[int, int]] = set()
    for u, v in dir_edges:
        pair = (u, v) if u < v else (v, u)
        if pair not in seen_undir:
            seen_undir.add(pair)
            undir_edges.append(pair)

    fx = [0.0] * n
    fy = [0.0] * n
    for it in range(iterations):
        t_ratio = 1.0 - it / max(1, iterations)
        max_step = max(4.0, k_base * 0.55 * t_ratio * t_ratio + 4.0 * t_ratio)
        for i in range(n):
            fx[i] = 0.0
            fy[i] = 0.0

        mean_x = sum(cx) / n
        mean_y = sum(cy) / n

        for i in range(n):
            for j in range(i + 1, n):
                dx = cx[j] - cx[i]
                dy = cy[j] - cy[i]
                if dx == 0.0 and dy == 0.0:
                    dx = 1.0 if (i % 2 == 0) else -1.0
                    dy = 1.0
                dist = max(1.0, math.hypot(dx, dy))
                ux = dx / dist
                uy = dy / dist
                pair_k = max(hw[i] + hw[j] + gap_x, hh[i] + hh[j] + gap_y)
                f_rep = (pair_k * pair_k * pair_k) / max(dist * dist, 100.0)
                fx[i] -= ux * f_rep
                fy[i] -= uy * f_rep
                fx[j] += ux * f_rep
                fy[j] += uy * f_rep

        for u, v in undir_edges:
            dx = cx[v] - cx[u]
            dy = cy[v] - cy[u]
            dist = math.hypot(dx, dy)
            if dist <= 0.0:
                continue
            rest_len = max(hw[u] + hw[v] + gap_x, hh[u] + hh[v] + gap_y)
            f_att = 1.2 * (dist - rest_len * 0.85)
            ux = dx / dist
            uy = dy / dist
            fx[u] += ux * f_att
            fy[u] += uy * f_att
            fx[v] -= ux * f_att
            fy[v] -= uy * f_att

        gravity = 0.28
        for i in range(n):
            fx[i] += (mean_x - cx[i]) * gravity
            fy[i] += (mean_y - cy[i]) * gravity
            mag = math.hypot(fx[i], fy[i])
            if mag > 0.0:
                step = min(mag, max_step)
                cx[i] += (fx[i] / mag) * step
                cy[i] += (fy[i] / mag) * step

    pad_x = max(20.0, min(gap_x, 40.0))
    pad_y = max(20.0, min(gap_y, 40.0))
    max_passes = min(120, max(40, n * 3))
    for _ in range(max_passes):
        moved = False
        for i in range(n):
            for j in range(i + 1, n):
                dx = cx[j] - cx[i]
                dy = cy[j] - cy[i]
                ox = hw[i] + hw[j] + pad_x - abs(dx)
                oy = hh[i] + hh[j] + pad_y - abs(dy)
                if ox > 0.0 and oy > 0.0:
                    moved = True
                    if ox <= oy:
                        s = 1.0 if (dx > 0.0 or (dx == 0.0 and i < j)) else -1.0
                        cx[i] -= s * ox * 0.5
                        cx[j] += s * ox * 0.5
                    else:
                        s = 1.0 if (dy > 0.0 or (dy == 0.0 and i < j)) else -1.0
                        cy[i] -= s * oy * 0.5
                        cy[j] += s * oy * 0.5
        if not moved:
            break

    min_x = min(cx[i] - hw[i] for i in range(n))
    min_y = min(cy[i] - hh[i] for i in range(n))
    return [
        {
            "id": active[i]["id"],
            "x": _js_round(cx[i] - hw[i] - min_x + 80.0),
            "y": _js_round(cy[i] - hh[i] - min_y + 80.0),
        }
        for i in range(n)
    ]


def anchor_positions(
    positions: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    origin: dict[str, int] | None,
) -> list[dict[str, Any]]:
    """Shift `positions` so their bounding-box top-left starts at `origin` (`canvas_model.js:anchorPositions`)."""
    by_id = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}
    valid = [dict(p) for p in (positions or []) if isinstance(p, dict) and p.get("id") in by_id]
    if not valid or not origin:
        return valid
    min_x = min(int(p["x"]) for p in valid)
    min_y = min(int(p["y"]) for p in valid)
    dx = _js_round(origin["x"] - min_x)
    dy = _js_round(origin["y"] - min_y)
    return [{"id": p["id"], "x": _js_round(p["x"] + dx), "y": _js_round(p["y"] + dy)} for p in valid]


def layout_graph_job(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    mode: str = "dag",
    options: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Run `"dag"` or `"force"` layout over non-group `nodes` (`nodemap_worker.js:layoutGraphJob`)."""
    active = [
        n
        for n in (nodes or [])
        if isinstance(n, dict) and n.get("id") is not None and n.get("type") != "group"
    ]
    if not active:
        return {"positions": []}
    if mode == "force":
        return {"positions": layout_force(active, edges, options)}
    return {"positions": layout_dag(active, edges, options)}


def apply_canvas_layout(
    canvas: dict[str, Any],
    *,
    mode: str = "dag",
    group_id: str | None = None,
    node_ids: list[str] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply `"dag"` or `"force"` layout to `canvas` (full canvas, one `group_id`, or `node_ids`) and auto-fit groups."""
    nodes: list[dict[str, Any]] = canvas.get("nodes") or []
    edges: list[dict[str, Any]] = canvas.get("edges") or []
    by_id = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}

    if group_id:
        group = by_id.get(group_id)
        if not group or group.get("type") != "group":
            return canvas
        members = [n for n in nodes if n.get("groupId") == group_id and n.get("type") != "group"]
        if not members:
            return canvas
        member_ids = {n["id"] for n in members}
        sub_edges = [e for e in edges if e.get("from") in member_ids and e.get("to") in member_ids]
        raw_pos = layout_graph_job(members, sub_edges, mode=mode, options=options)["positions"]
        origin = {"x": int(group["x"]) + GROUP_LAYOUT_PAD["x"], "y": int(group["y"]) + GROUP_LAYOUT_PAD["top"]}
        anchored = anchor_positions(raw_pos, members, origin)
        for p in anchored:
            target = by_id.get(p["id"])
            if target:
                target["x"] = p["x"]
                target["y"] = p["y"]
        fit_group_to_members(group, members, GROUP_LAYOUT_PAD)
        return canvas

    if node_ids:
        wanted = set(node_ids)
        subset = [n for n in nodes if n["id"] in wanted and n.get("type") != "group"]
        if not subset:
            return canvas
        orig_bounds = bounds_of(subset)
        origin = {"x": orig_bounds["x"], "y": orig_bounds["y"]} if orig_bounds else {"x": 80, "y": 80}
        sub_ids = {n["id"] for n in subset}
        sub_edges = [e for e in edges if e.get("from") in sub_ids and e.get("to") in sub_ids]
        raw_pos = layout_graph_job(subset, sub_edges, mode=mode, options=options)["positions"]
        anchored = anchor_positions(raw_pos, subset, origin)
        for p in anchored:
            target = by_id.get(p["id"])
            if target:
                target["x"] = p["x"]
                target["y"] = p["y"]
        # Re-fit any affected parent groups
        affected_gids = {n.get("groupId") for n in subset if n.get("groupId")}
        for gid in affected_gids:
            grp = by_id.get(gid)
            if grp and grp.get("type") == "group":
                members = [n for n in nodes if n.get("groupId") == gid]
                fit_group_to_members(grp, members, GROUP_LAYOUT_PAD)
        return canvas

    # Full canvas layout:
    groups = [n for n in nodes if n.get("type") == "group"]
    non_groups = [n for n in nodes if n.get("type") != "group"]
    if not groups:
        raw_pos = layout_graph_job(non_groups, edges, mode=mode, options=options)["positions"]
        for p in raw_pos:
            target = by_id.get(p["id"])
            if target:
                target["x"] = p["x"]
                target["y"] = p["y"]
        return canvas

    # When groups exist: lay out members inside each group first, then arrange top-level units (groups + ungrouped nodes)
    for grp in reversed(paint_order(groups)):
        gid = grp["id"]
        direct_non_groups = [n for n in nodes if n.get("groupId") == gid and n.get("type") != "group"]
        if direct_non_groups:
            m_ids = {n["id"] for n in direct_non_groups}
            m_edges = [e for e in edges if e.get("from") in m_ids and e.get("to") in m_ids]
            raw_pos = layout_graph_job(direct_non_groups, m_edges, mode=mode, options=options)["positions"]
            origin = {"x": int(grp["x"]) + GROUP_LAYOUT_PAD["x"], "y": int(grp["y"]) + GROUP_LAYOUT_PAD["top"]}
            for p in anchor_positions(raw_pos, direct_non_groups, origin):
                target = by_id.get(p["id"])
                if target:
                    target["x"] = p["x"]
                    target["y"] = p["y"]
        all_members = [n for n in nodes if n.get("groupId") == gid]
        if all_members:
            fit_group_to_members(grp, all_members, GROUP_LAYOUT_PAD)

    # Top-level units (top-level groups treated as proxy boxes + ungrouped non-group nodes)
    top_groups = [g for g in groups if not g.get("groupId")]
    top_ungrouped = [n for n in non_groups if not n.get("groupId")]
    if top_groups and top_ungrouped or len(top_groups) > 1 or (not top_groups and top_ungrouped):
        proxy_nodes: list[dict[str, Any]] = []
        top_group_Of: dict[str, str] = {}
        for g in top_groups:
            proxy_nodes.append({"id": g["id"], "type": "note", "x": g["x"], "y": g["y"], "w": g["w"], "h": g["h"]})
            top_group_Of[g["id"]] = g["id"]
            for n in nodes:
                cur = n
                seen_g: set[str] = set()
                while cur and cur.get("groupId") and cur["groupId"] not in seen_g:
                    seen_g.add(cur["groupId"])
                    if cur["groupId"] == g["id"]:
                        top_group_Of[n["id"]] = g["id"]
                        break
                    cur = by_id.get(cur["groupId"])
        for u in top_ungrouped:
            proxy_nodes.append({"id": u["id"], "type": u["type"], "x": u["x"], "y": u["y"], "w": u["w"], "h": u["h"]})
            top_group_Of[u["id"]] = u["id"]

        proxy_edges: list[dict[str, str]] = []
        for e in edges:
            pu = top_group_Of.get(str(e.get("from") or ""))
            pv = top_group_Of.get(str(e.get("to") or ""))
            if pu and pv and pu != pv:
                proxy_edges.append({"from": pu, "to": pv})

        top_pos = layout_graph_job(proxy_nodes, proxy_edges, mode=mode, options=options)["positions"]
        for p in top_pos:
            target = by_id.get(p["id"])
            if not target:
                continue
            dx = int(p["x"]) - int(target["x"])
            dy = int(p["y"]) - int(target["y"])
            target["x"] = int(p["x"])
            target["y"] = int(p["y"])
            if target.get("type") == "group" and (dx != 0 or dy != 0):
                # Shift all descendants of this top group by (dx, dy)
                queue = [target["id"]]
                seen_desc = {target["id"]}
                while queue:
                    cur_gid = queue.pop(0)
                    for child in nodes:
                        if child.get("groupId") == cur_gid and child["id"] not in seen_desc:
                            seen_desc.add(child["id"])
                            child["x"] = int(child["x"]) + dx
                            child["y"] = int(child["y"]) + dy
                            if child.get("type") == "group":
                                queue.append(child["id"])
    return canvas


# ------------------------------------------------------------------ Exporters (JSON, Mermaid, DOT)


def _build_safe_id_map(nodes: list[dict[str, Any]]):
    used: set[str] = set()
    mapping: dict[str, str] = {}

    def safe_id_of(raw_id: Any) -> str:
        key = str(raw_id if raw_id is not None else "")
        if key in mapping:
            return mapping[key]
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", key) or "node"
        cand = f"n_{cleaned}"
        if cand in used:
            suffix = 2
            while f"{cand}_{suffix}" in used:
                suffix += 1
            cand = f"{cand}_{suffix}"
        used.add(cand)
        mapping[key] = cand
        return cand

    for n in nodes:
        if isinstance(n, dict) and n.get("id") is not None:
            safe_id_of(n["id"])
    return safe_id_of


def _node_lock_lines(node: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    locks = node.get("locks")
    if isinstance(locks, list):
        for lk in locks:
            badge = format_lock_badge(lk)
            if badge:
                lines.append(badge)
    data = node.get("data")
    field_locks = data.get("fieldLocks") if isinstance(data, dict) else None
    if isinstance(field_locks, dict):
        entries = list(field_locks.items())
        for fk, fl in entries[:MAX_FIELD_LOCKS_SHOWN]:
            badge = format_lock_badge(fl)
            if not badge:
                continue
            field_name = re.sub(r"^\d+:", "", str(fk)) or str(fk)
            lines.append(f"{field_name}: {badge}")
        if len(entries) > MAX_FIELD_LOCKS_SHOWN:
            lines.append(f"+{len(entries) - MAX_FIELD_LOCKS_SHOWN} more field locks")
    return lines


def _format_edge_text(edge: dict[str, Any]) -> str:
    label = str(edge.get("label") or "").strip()
    lock_kind = str(edge.get("lockKind") or "").strip()
    lock_tag = f"🔒 {lock_kind}" if lock_kind else ""
    if label and lock_tag:
        return f"{label} ({lock_tag})"
    return label or lock_tag


def _build_group_hierarchy(nodes: list[dict[str, Any]]):
    ordered = paint_order(nodes)
    by_id = {n["id"]: n for n in ordered}
    visible_group_ids = {n["id"] for n in ordered if n.get("type") == "group"}
    children_of: dict[str, list[dict[str, Any]]] = {}
    top_level: list[dict[str, Any]] = []
    for n in ordered:
        gid = n.get("groupId")
        pid = gid if (gid and gid != n["id"] and gid in visible_group_ids) else None
        if pid:
            children_of.setdefault(pid, []).append(n)
        else:
            top_level.append(n)
    return ordered, by_id, children_of, top_level


def _escape_mermaid(text: Any) -> str:
    s = str(text if text is not None else "")
    s = s.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\r?\n|\r", "<br/>", s)
    return s


def _escape_dot(text: Any) -> str:
    s = str(text if text is not None else "")
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = re.sub(r"\r?\n|\r", "\\\\n", s)
    return s


def _mermaid_arrow(arrow: str | None, style: str | None) -> str:
    dashed = style in ("dotted", "dashed")
    if dashed:
        if arrow == "both":
            return "<-.->"
        if arrow == "none":
            return "-.-"
        if arrow == "backward":
            return "<-.-"
        return "-.->"
    if arrow == "both":
        return "<-->"
    if arrow == "none":
        return "---"
    if arrow == "backward":
        return "<--"
    return "-->"


def canvas_to_mermaid(canvas: dict[str, Any], *, direction: str = "LR") -> str:
    """Export the visible canvas as a Mermaid `flowchart` diagram (`nodemap_export_formats.js:canvasToMermaid`)."""
    vis = visible_canvas_slice(canvas)
    dir_code = direction if direction in MERMAID_DIRS else "LR"
    ordered, by_id, children_of, top_level = _build_group_hierarchy(vis.get("nodes") or [])
    safe_id_of = _build_safe_id_map(ordered)
    lines = [f"flowchart {dir_code}"]
    visited_groups: set[str] = set()

    def emit_node(n: dict[str, Any], indent: str) -> None:
        sid = safe_id_of(n["id"])
        if n.get("type") == "group":
            if n["id"] in visited_groups:
                return
            visited_groups.add(n["id"])
            grp_locks = _node_lock_lines(n)
            grp_title = f"{display_title(n)} ({', '.join(grp_locks)})" if grp_locks else display_title(n)
            lines.append(f'{indent}subgraph {sid} ["{_escape_mermaid(grp_title)}"]')
            kids = children_of.get(n["id"]) or []
            if kids:
                for child in kids:
                    emit_node(child, f"{indent}  ")
            else:
                lines.append(f'{indent}  {sid}_portal["{_escape_mermaid(f"[portal] {display_title(n)}")}"]')
            lines.append(f"{indent}end")
            return
        ntype = str(n.get("type") or "node")
        badge = NODE_BADGES.get(ntype, ntype)
        parts = [f"[{badge}] {display_title(n)}", *_node_lock_lines(n)]
        lines.append(f'{indent}{sid}["{_escape_mermaid(chr(10).join(parts))}"]')

    for n in top_level:
        emit_node(n, "  ")

    for e in vis.get("edges") or []:
        if not isinstance(e, dict) or e.get("from") not in by_id or e.get("to") not in by_id:
            continue
        from_id = safe_id_of(e["from"])
        to_id = safe_id_of(e["to"])
        arrow = _mermaid_arrow(e.get("arrow"), e.get("style"))
        edge_text = _format_edge_text(e)
        if edge_text:
            lines.append(f'  {from_id} {arrow}|"{_escape_mermaid(edge_text)}"| {to_id}')
        else:
            lines.append(f"  {from_id} {arrow} {to_id}")

    return "\n".join(lines)


def canvas_to_dot(canvas: dict[str, Any], *, direction: str = "LR") -> str:
    """Export the visible canvas as a Graphviz DOT `digraph` (`nodemap_export_formats.js:canvasToDot`)."""
    vis = visible_canvas_slice(canvas)
    raw_dir = "TB" if direction == "TD" else direction
    dir_code = raw_dir if raw_dir in DOT_DIRS else "LR"
    ordered, by_id, children_of, top_level = _build_group_hierarchy(vis.get("nodes") or [])
    safe_id_of = _build_safe_id_map(ordered)
    lines = [
        "digraph NodeMap {",
        f"  rankdir={dir_code};",
        "  compound=true;",
        '  graph [fontname="sans-serif"];',
        '  node [shape=box, style="rounded,filled", fontname="monospace"];',
        '  edge [fontname="sans-serif"];',
    ]
    visited_groups: set[str] = set()

    def cluster_anchor_of(group_node: dict[str, Any]) -> str | None:
        queue = list(children_of.get(group_node["id"]) or [])
        seen = {group_node["id"]}
        while queue:
            cur = queue.pop(0)
            if not cur or cur["id"] in seen:
                continue
            seen.add(cur["id"])
            kids = children_of.get(cur["id"]) or []
            if cur.get("type") != "group" or not kids:
                return safe_id_of(cur["id"])
            queue.extend(kids)
        return None

    def emit_node(n: dict[str, Any], indent: str) -> None:
        sid = safe_id_of(n["id"])
        ntype = str(n.get("type") or "node")
        if ntype == "group":
            if n["id"] in visited_groups:
                return
            visited_groups.add(n["id"])
            grp_locks = _node_lock_lines(n)
            grp_label = f"{display_title(n)}\n" + "\n".join(grp_locks) if grp_locks else display_title(n)
            kids = children_of.get(n["id"]) or []
            lines.append(f"{indent}subgraph cluster_{sid} {{")
            lines.append(f'{indent}  label="{_escape_dot(grp_label)}";')
            lines.append(f'{indent}  style="rounded,dashed";')
            if kids:
                for child in kids:
                    emit_node(child, f"{indent}  ")
            else:
                badge = str(NODE_BADGES.get(ntype, ntype)).upper()
                parts = [f"[{badge}] {display_title(n)}", *grp_locks]
                lines.append(f'{indent}  {sid} [label="{_escape_dot(chr(10).join(parts))}"];')
            lines.append(f"{indent}}}")
            return
        badge = str(NODE_BADGES.get(ntype, ntype)).upper()
        parts = [f"[{badge}] {display_title(n)}", *_node_lock_lines(n)]
        lines.append(f'{indent}{sid} [label="{_escape_dot(chr(10).join(parts))}"];')

    for n in top_level:
        emit_node(n, "  ")

    for e in vis.get("edges") or []:
        if not isinstance(e, dict) or e.get("from") not in by_id or e.get("to") not in by_id:
            continue
        from_node = by_id[e["from"]]
        to_node = by_id[e["to"]]
        from_sid = safe_id_of(e["from"])
        to_sid = safe_id_of(e["to"])
        from_anchor = cluster_anchor_of(from_node) if from_node.get("type") == "group" else None
        to_anchor = cluster_anchor_of(to_node) if to_node.get("type") == "group" else None
        from_ep = from_anchor or from_sid
        to_ep = to_anchor or to_sid
        edge_text = _format_edge_text(e)
        style_raw = e.get("style")
        dot_style = "dashed" if style_raw == "dashed" else ("dotted" if style_raw == "dotted" else "solid")
        arrow_raw = e.get("arrow")
        dot_dir = (
            "both"
            if arrow_raw == "both"
            else ("back" if arrow_raw == "backward" else ("none" if arrow_raw == "none" else "forward"))
        )
        attrs: list[str] = []
        if edge_text:
            attrs.append(f'label="{_escape_dot(edge_text)}"')
        attrs.append(f'style="{dot_style}"')
        attrs.append(f'dir="{dot_dir}"')
        if from_anchor:
            attrs.append(f'ltail="cluster_{from_sid}"')
        if to_anchor:
            attrs.append(f'lhead="cluster_{to_sid}"')
        lines.append(f"  {from_ep} -> {to_ep} [{', '.join(attrs)}];")

    lines.append("}")
    return "\n".join(lines)


def export_canvas(canvas: dict[str, Any], fmt: str = "json", *, direction: str = "LR") -> dict[str, Any]:
    """Export `canvas` in `"json"` (canonical), `"mermaid"`, or `"dot"` format."""
    clean_fmt = (fmt or "json").strip().lower()
    canonical = serialize_canvas(canvas)
    if clean_fmt == "mermaid":
        return {"format": "mermaid", "content": canvas_to_mermaid(canonical, direction=direction)}
    if clean_fmt == "dot":
        return {"format": "dot", "content": canvas_to_dot(canonical, direction=direction)}
    return {"format": "json", "canvas": canonical, "content": json.dumps(canonical, indent=2)}
