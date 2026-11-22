/**
 * nodemap_gestures.js - NodeMap pointer, drag, resize, marquee, wire, waypoint, smart snap
 * alignment guides, Alt+Drag clone-on-drag, Shift+Drag axis locking, and keyboard shortcuts.
 */
import { api } from "../../api.js";
import { contextMenu } from "../../components/context_menu.js";
import { toast } from "../../components/toast.js";
import * as M from "./canvas_model.js";
import { mailThreadLayout, relationTargets } from "./domain_nodes.js";
import { cveSpec, kapiSpec } from "./nodemap_bridge.js";
import { extractReferencedStruct, findItem, guardAutoCollapsed } from "./struct_view.js";

const DRAG_THRESHOLD = 3;
const ALIGN_SNAP_THRESHOLD = 6;
const EDIT_ACTS = new Set(["toggle-type", "hide-item", "all-types", "guards", "toggle-header", "field-lock"]);
const MAIL_EXPAND_MAX = 60;
const KC_EXPAND_MAX = 40;

const isTyping = (el) => Boolean(el && (el.closest?.("input, textarea, select, [contenteditable='true']")));

/** Whether the document holds a non-empty text selection inside `root` (copying it stays native). */
export function hasTextSelection(root) {
  try {
    const sel = globalThis.getSelection?.();
    if (!sel || !sel.toString()) return false;
    if (!root?.contains || !sel.anchorNode) return true;
    return root.contains(sel.anchorNode) || root.contains(sel.focusNode);
  } catch {
    return false;
  }
}

/**
 * Pure smart-alignment guide calculation (Figma-style edge & center snapping).
 * Compares the bounding box of `movingNodes` against `candidateNodes` within `threshold` canvas units.
 * Returns `{ dx, dy, guides: Array<{ axis: "x"|"y", pos: number, from: number, to: number }> }`.
 */
export function computeAlignmentGuides(movingNodes, candidateNodes, threshold = ALIGN_SNAP_THRESHOLD, { lockX = false, lockY = false } = {}) {
  const moving = (Array.isArray(movingNodes) ? movingNodes : []).filter(Boolean);
  const cands = (Array.isArray(candidateNodes) ? candidateNodes : []).filter(Boolean);
  if (!moving.length || !cands.length) return { dx: 0, dy: 0, guides: [] };

  const box = M.boundsOf(moving);
  if (!box) return { dx: 0, dy: 0, guides: [] };

  const movX = [box.x, box.x + box.w / 2, box.x + box.w];
  const movY = [box.y, box.y + box.h / 2, box.y + box.h];

  let bestDx = 0;
  let minAbsDx = threshold + 1e-6;
  let bestSnapX = null;

  if (!lockX) {
    for (const c of cands) {
      const cxs = [c.x, c.x + c.w / 2, c.x + c.w];
      for (const mx of movX) {
        for (const cx of cxs) {
          const d = cx - mx;
          const ad = Math.abs(d);
          if (ad <= threshold && ad < minAbsDx) {
            minAbsDx = ad;
            bestDx = Math.round(d);
            bestSnapX = cx;
          }
        }
      }
    }
  }

  let bestDy = 0;
  let minAbsDy = threshold + 1e-6;
  let bestSnapY = null;

  if (!lockY) {
    for (const c of cands) {
      const cys = [c.y, c.y + c.h / 2, c.y + c.h];
      for (const my of movY) {
        for (const cy of cys) {
          const d = cy - my;
          const ad = Math.abs(d);
          if (ad <= threshold && ad < minAbsDy) {
            minAbsDy = ad;
            bestDy = Math.round(d);
            bestSnapY = cy;
          }
        }
      }
    }
  }

  const guides = [];
  const snappedBox = { x: box.x + bestDx, y: box.y + bestDy, w: box.w, h: box.h };

  if (bestSnapX !== null) {
    let minY = snappedBox.y;
    let maxY = snappedBox.y + snappedBox.h;
    for (const c of cands) {
      const cxs = [c.x, c.x + c.w / 2, c.x + c.w];
      if (cxs.some((cx) => Math.abs(cx - bestSnapX) <= 0.5)) {
        minY = Math.min(minY, c.y);
        maxY = Math.max(maxY, c.y + c.h);
      }
    }
    guides.push({ axis: "x", pos: Math.round(bestSnapX), from: Math.round(minY - 14), to: Math.round(maxY + 14) });
  }

  if (bestSnapY !== null) {
    let minX = snappedBox.x;
    let maxX = snappedBox.x + snappedBox.w;
    for (const c of cands) {
      const cys = [c.y, c.y + c.h / 2, c.y + c.h];
      if (cys.some((cy) => Math.abs(cy - bestSnapY) <= 0.5)) {
        minX = Math.min(minX, c.x);
        maxX = Math.max(maxX, c.x + c.w);
      }
    }
    guides.push({ axis: "y", pos: Math.round(bestSnapY), from: Math.round(minX - 14), to: Math.round(maxX + 14) });
  }

  return { dx: bestDx, dy: bestDy, guides };
}

export function renderAlignmentGuides(ctrl, guides) {
  const layer = ctrl.layerEl;
  if (!layer || typeof document === "undefined") return;
  if (!guides?.length) {
    clearAlignmentGuides(ctrl);
    return;
  }
  let host = ctrl.guideEl;
  if (!host && typeof document.createElement === "function") {
    host = document.createElement("div");
    host.className = "nm-guides";
    host.setAttribute?.("aria-hidden", "true");
    layer.appendChild?.(host);
    ctrl.guideEl = host;
  }
  if (!host) return;
  host.hidden = false;
  host.innerHTML = guides.map((g) => (g.axis === "x"
    ? `<div class="nm-guide-line nm-guide-x" style="left:${g.pos}px;top:${g.from}px;height:${Math.max(1, g.to - g.from)}px"></div>`
    : `<div class="nm-guide-line nm-guide-y" style="top:${g.pos}px;left:${g.from}px;width:${Math.max(1, g.to - g.from)}px"></div>`)).join("");
}

export function clearAlignmentGuides(ctrl) {
  if (!ctrl?.guideEl) return;
  ctrl.guideEl.innerHTML = "";
  ctrl.guideEl.hidden = true;
}

export function clearTextSelection(ctrl) {
  try {
    const sel = globalThis.getSelection?.();
    if (sel && sel.toString() && ctrl.rootEl?.contains(sel.anchorNode)) sel.removeAllRanges();
  } catch {
    // no selection API
  }
}

export function selectOnly(ctrl, id, e = null) {
  if (e && (e.shiftKey || e.ctrlKey || e.metaKey)) ctrl.selected.add(id);
  else ctrl.selected = new Set([id]);
  ctrl.selectedEdge = null;
  ctrl.commit({ save: false });
}

export function selectEdge(ctrl, id) {
  ctrl.selectedEdge = id;
  ctrl.selected.clear();
  ctrl.commit({ save: false });
}

export function onPointerDown(ctrl, e) {
  const t = e.target;
  if (!t.closest(".nm-inline-edit")) ctrl.rootEl.focus({ preventScroll: true });
  const start = { x: e.clientX, y: e.clientY };
  if (e.button === 1 || (e.button === 0 && ctrl.spaceDown)) {
    e.preventDefault();
    ctrl.gesture = { kind: "pan", start, vp: { ...ctrl.canvas.viewport }, moved: false };
    return;
  }
  if (e.button !== 0 || t.closest(".nm-inline-edit, input, textarea, select")) return;
  const wpEl = t.closest(".nm-edge-wp[data-edge-id]");
  if (wpEl) {
    const edge = ctrl.canvas.edges.find((x) => x.id === wpEl.dataset.edgeId);
    const wpIdx = parseInt(wpEl.dataset.wpIdx, 10);
    if (edge && Number.isInteger(wpIdx) && edge.waypoints?.[wpIdx]) {
      e.preventDefault();
      selectEdge(ctrl, edge.id);
      ctrl.gesture = { kind: "waypoint", start, edgeId: edge.id, wpIdx, moved: false };
      return;
    }
  }
  const edgeEl = t.closest(".nm-edge[data-edge-id]");
  if (edgeEl) {
    selectEdge(ctrl, edgeEl.dataset.edgeId);
    return;
  }
  const nodeEl = t.closest(".nm-node");
  if (!nodeEl || (t.closest(".nm-node-head") && !t.closest("button"))) clearTextSelection(ctrl);
  if (!nodeEl) {
    if (ctrl.active) {
      ctrl.active = null;
      ctrl.commit({ save: false });
    }
    const additive = e.shiftKey || e.ctrlKey || e.metaKey;
    if (ctrl.tool === "select" || additive) {
      const p = ctrl.toCanvas(e.clientX, e.clientY);
      ctrl.gesture = { kind: "marquee", start, origin: p, base: additive ? new Set(ctrl.selected) : new Set(), moved: false };
    } else {
      ctrl.gesture = { kind: "pan", start, vp: { ...ctrl.canvas.viewport }, moved: false, clear: true };
    }
    return;
  }
  const node = ctrl.byId(nodeEl.dataset.nodeId);
  if (!node) return;
  const rz = t.closest(".nm-rz");
  const port = t.closest(".nm-port");
  if (rz) {
    e.preventDefault();
    selectOnly(ctrl, node.id, e);
    ctrl.gesture = { kind: "resize", start, id: node.id, handle: rz.dataset.rz, rect: M.rectOf(node), moved: false };
  } else if (port) {
    e.preventDefault();
    e.stopPropagation();
    ctrl.gesture = { kind: "wire", start, from: node.id, side: port.dataset.side || "auto", field: port.dataset.field || null, cur: ctrl.toCanvas(e.clientX, e.clientY), moved: false };
    ctrl.rootEl?.classList.add("nm-wiring");
  } else if (t.closest(".nm-node-head") && !t.closest("button")) {
    e.preventDefault();
    const altClone = Boolean(e.altKey);
    const toggle = !altClone && (e.shiftKey || e.ctrlKey || e.metaKey);
    let shiftTogglePending = null;
    if (toggle) {
      if (ctrl.selected.has(node.id)) {
        if (e.shiftKey && !e.ctrlKey && !e.metaKey) {
          // Defer deselection to pointerup without movement so Shift+Drag can axis-lock a selected node
          shiftTogglePending = node.id;
        } else {
          ctrl.selected.delete(node.id);
          ctrl.selectedEdge = null;
          ctrl.commit({ save: false });
          return;
        }
      } else {
        ctrl.selected.add(node.id);
        ctrl.selectedEdge = null;
        ctrl.commit({ save: false });
      }
    } else if (!ctrl.selected.has(node.id)) {
      selectOnly(ctrl, node.id);
    }
    const moving = M.dragSet(ctrl.canvas.nodes, [...ctrl.selected]);
    ctrl.gesture = {
      kind: "drag",
      start,
      primary: node.id,
      nodes: moving.map((n) => ({ n, x: n.x, y: n.y })),
      altClone,
      cloned: false,
      shiftTogglePending,
      moved: false,
    };
  } else if (!t.closest("button, a")) {
    if (!ctrl.selected.has(node.id) || ctrl.selected.size > 1) {
      if (e.shiftKey || e.ctrlKey || e.metaKey) ctrl.selected.add(node.id);
      else selectOnly(ctrl, node.id);
      ctrl.commit({ save: false });
    }
  }
}

function materializeAltCloneDrag(ctrl, g) {
  const ids = [...ctrl.selected];
  const clones = M.cloneSubgraph(ctrl.canvas, ids, { x: 0, y: 0 });
  if (!clones.nodes.length) {
    g.cloned = true;
    return;
  }
  const idSet = new Set(clones.nodes.map((n) => n.id));
  ctrl.canvas.nodes.push(...clones.nodes);
  ctrl.canvas.edges.push(...clones.edges);
  // Reset original nodes back to their start coordinates in case any moved
  for (const m of g.nodes) {
    m.n.x = m.x;
    m.n.y = m.y;
  }
  const origPrimary = g.nodes.find((m) => m.n.id === g.primary);
  const clonedNodes = clones.nodes.map((n) => ({ n, x: n.x, y: n.y }));
  const nextPrimary = (origPrimary && clones.nodes.find((c) => c.type === origPrimary.n.type && c.x === origPrimary.x && c.y === origPrimary.y)) || clones.nodes[0];
  g.nodes = clonedNodes;
  g.primary = nextPrimary.id;
  g.cloned = true;
  ctrl.selected = new Set(clones.nodes.filter((n) => !(n.groupId && idSet.has(n.groupId))).map((n) => n.id));
  ctrl.selectedEdge = null;
  ctrl.renderer?.sync?.(ctrl.canvas.nodes, ctrl.selected, ctrl.active, ctrl.canvas.settings);
}

export function onPointerMove(ctrl, e) {
  const g = ctrl.gesture;
  if (!g) return;
  if (e.buttons === 0) return onPointerUp(ctrl, e);
  const dxs = e.clientX - g.start.x;
  const dys = e.clientY - g.start.y;
  if (!g.moved && Math.hypot(dxs, dys) < DRAG_THRESHOLD) return;
  g.moved = true;
  const z = ctrl.zoom;
  const snap = ctrl.canvas.settings.snap ? ctrl.canvas.settings.grid : 0;
  if (g.kind === "pan") {
    ctrl.wrapEl.classList.add("nm-panning");
    ctrl.rootEl?.classList?.add?.("nm-dragging");
    ctrl.canvas.viewport.x = g.vp.x + dxs;
    ctrl.canvas.viewport.y = g.vp.y + dys;
    ctrl.applyTransform();
  } else if (g.kind === "drag") {
    ctrl.rootEl?.classList?.add?.("nm-dragging");
    if (g.altClone && !g.cloned) materializeAltCloneDrag(ctrl, g);
    let dx = dxs / z;
    let dy = dys / z;
    let lockX = false;
    let lockY = false;
    if (e.shiftKey) {
      if (Math.abs(dx) >= Math.abs(dy)) {
        dy = 0;
        lockY = true;
      } else {
        dx = 0;
        lockX = true;
      }
    }
    const prim = g.nodes.find((m) => m.n.id === g.primary);
    if (snap && prim) {
      if (!lockX) dx = M.snapValue(prim.x + dx, snap) - prim.x;
      if (!lockY) dy = M.snapValue(prim.y + dy, snap) - prim.y;
    }
    if (!snap) {
      const movingIds = new Set(g.nodes.map((m) => m.n.id));
      const prospective = g.nodes.map((m) => ({ x: Math.round(m.x + dx), y: Math.round(m.y + dy), w: m.n.w, h: m.n.h }));
      const candidates = (ctrl.canvas.nodes || []).filter((n) => !movingIds.has(n.id) && ctrl.isNodeVisibleInLayer(n));
      const align = computeAlignmentGuides(prospective, candidates, ALIGN_SNAP_THRESHOLD, { lockX, lockY });
      dx += align.dx;
      dy += align.dy;
      renderAlignmentGuides(ctrl, align.guides);
    } else {
      clearAlignmentGuides(ctrl);
    }

    const movedIds = new Set();
    for (const m of g.nodes) {
      m.n.x = Math.round(m.x + dx);
      m.n.y = Math.round(m.y + dy);
      movedIds.add(m.n.id);
      const el = ctrl.renderer.element(m.n.id);
      if (el && ctrl.renderer.place) ctrl.renderer.place(el, m.n);
    }
    ctrl.scheduleWires(movedIds);
  } else if (g.kind === "resize") {
    ctrl.rootEl?.classList?.add?.("nm-dragging");
    const node = ctrl.byId(g.id);
    if (!node) return;
    if (node.autoFit) Object.assign(node, M.scaleResize(g.rect, g.handle, dxs / z, dys / z, node));
    else Object.assign(node, M.resizeRect(g.rect, g.handle, dxs / z, dys / z, M.SIZE_LIMITS[node.type], snap));
    ctrl.portOffsets.delete(node.id);
    const el = ctrl.renderer.element(node.id);
    if (el) ctrl.renderer.place(el, node);
    ctrl.scheduleWires(new Set([node.id]));
  } else if (g.kind === "waypoint") {
    const edge = ctrl.canvas.edges.find((x) => x.id === g.edgeId);
    if (edge?.waypoints?.[g.wpIdx]) {
      const p = ctrl.toCanvas(e.clientX, e.clientY);
      edge.waypoints[g.wpIdx] = {
        x: Math.round(snap ? M.snapValue(p.x, snap) : p.x),
        y: Math.round(snap ? M.snapValue(p.y, snap) : p.y),
      };
      ctrl.scheduleWires(new Set([edge.from, edge.to]));
    }
  } else if (g.kind === "wire") {
    g.cur = ctrl.toCanvas(e.clientX, e.clientY);
    ctrl.scheduleWires(new Set([g.from]));
  } else if (g.kind === "marquee") {
    const p = ctrl.toCanvas(e.clientX, e.clientY);
    const r = M.rectFromPoints(g.origin.x, g.origin.y, p.x, p.y);
    Object.assign(ctrl.marqueeEl.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
    ctrl.marqueeEl.hidden = false;
    ctrl.selected = new Set([...g.base, ...M.nodesInMarquee(ctrl.canvas.nodes, r)]);
    ctrl.selectedEdge = null;
    ctrl.renderer.sync(ctrl.canvas.nodes, ctrl.selected, ctrl.active, ctrl.canvas.settings);
  }
}

export function onPointerUp(ctrl, e) {
  const g = ctrl.gesture;
  if (!g) return;
  ctrl.gesture = null;
  ctrl.wrapEl?.classList?.remove?.("nm-panning");
  ctrl.rootEl?.classList?.remove?.("nm-dragging");
  clearAlignmentGuides(ctrl);
  if (e.type === "pointercancel" && g.kind === "wire") g.moved = false;
  if (g.kind === "pan") {
    if (!g.moved && g.clear) {
      ctrl.selected.clear();
      ctrl.selectedEdge = null;
      ctrl.commit({ save: false });
    } else if (g.moved) ctrl.scheduleSave();
  } else if (g.kind === "drag") {
    if (!g.moved && g.shiftTogglePending) {
      ctrl.selected.delete(g.shiftTogglePending);
      ctrl.selectedEdge = null;
      ctrl.commit({ save: false });
      return;
    }
    if (g.moved) {
      const movedIds = new Set(g.nodes.map((m) => m.n.id));
      const roots = g.nodes.map((m) => m.n).filter((n) => !(n.groupId && movedIds.has(n.groupId)));
      M.reassignGroups(ctrl.canvas.nodes, roots);
      ctrl.commit();
    }
  } else if (g.kind === "resize" && g.moved) {
    const node = ctrl.byId(g.id);
    if (node?.type === "group") M.reassignGroups(ctrl.canvas.nodes, ctrl.canvas.nodes.filter((n) => n.id !== node.id));
    ctrl.commit();
  } else if (g.kind === "waypoint" && g.moved) {
    ctrl.commit();
  } else if (g.kind === "wire") {
    try {
      ctrl.finishWire(g, e);
    } finally {
      ctrl.rootEl?.classList.remove("nm-wiring");
    }
  } else if (g.kind === "marquee") {
    ctrl.marqueeEl.hidden = true;
    if (!g.moved && !g.base.size) ctrl.selected.clear();
    ctrl.commit({ save: false });
  }
}

export function finishWire(ctrl, g, e) {
  const hit = document.elementFromPoint(e.clientX, e.clientY);
  const nodeEl = hit?.closest?.(".nm-node");
  const port = hit?.closest?.(".nm-port");
  const toId = nodeEl?.dataset.nodeId;
  if (!g.moved || !toId || toId === g.from || !ctrl.byId(toId)) {
    ctrl.renderWires();
    return;
  }
  const edge = M.createEdge({
    from: g.from,
    to: toId,
    fromSide: g.field ? (g.side === "left" ? "left" : "right") : g.side,
    fromField: g.field,
    toSide: port?.dataset.side || "auto",
    toField: port?.dataset.field || null,
  });
  if (!edge) {
    ctrl.renderWires();
    return;
  }
  ctrl.canvas.edges.push(edge);
  ctrl.selectedEdge = edge.id;
  ctrl.selected.clear();
  ctrl.commit();
}

export function onDblClick(ctrl, e) {
  if (isTyping(e.target)) return;
  const wpEl = e.target.closest?.(".nm-edge-wp[data-edge-id]");
  if (wpEl) {
    ctrl.removeEdgeWaypoint(wpEl.dataset.edgeId, parseInt(wpEl.dataset.wpIdx, 10));
    return;
  }
  const titleEl = e.target.closest(".nm-title");
  const nodeEl = e.target.closest(".nm-node");
  const edgeEl = e.target.closest(".nm-edge[data-edge-id]");
  if (nodeEl && !ctrl.canvas.settings.editMode) {
    if ((titleEl || e.target.closest(".nm-type-note .nm-node-body")) && !ctrl.hintedEditMode) {
      ctrl.hintedEditMode = true;
      toast.info("Turn on Edit Mode in the Inspector to rename nodes and edit notes in place");
    }
    return;
  }
  if (titleEl && nodeEl) return ctrl.beginTitleEdit(nodeEl.dataset.nodeId);
  if (nodeEl) {
    const node = ctrl.byId(nodeEl.dataset.nodeId);
    if (node?.type === "note" && e.target.closest(".nm-node-body")) return ctrl.beginNoteEdit(node.id);
    if (node?.type === "group" && e.target.closest(".nm-node-head")) return ctrl.beginTitleEdit(node.id);
    return;
  }
  if (edgeEl) {
    const eid = edgeEl.dataset.edgeId;
    if (e.shiftKey) return ctrl.addEdgeWaypointAt(eid, ctrl.toCanvas(e.clientX, e.clientY));
    const edge = ctrl.canvas.edges.find((x) => x.id === eid);
    if (e.altKey && edge?.callSite?.path) return ctrl.openCallSiteSource(edge);
    return ctrl.beginEdgeLabelEdit(eid);
  }
  const p = ctrl.toCanvas(e.clientX, e.clientY);
  ctrl.addNoteAt(Math.round(p.x - 110), Math.round(p.y - 20));
}

export function onWheel(ctrl, e) {
  const inContent = e.target.closest(".nm-scroll");
  if (inContent && !e.ctrlKey && !e.metaKey) return;
  e.preventDefault();
  const factor = Math.exp(-Math.max(-60, Math.min(60, e.deltaY)) * (e.ctrlKey ? 0.01 : 0.0025));
  ctrl.adjustZoom(factor, e.clientX, e.clientY);
}

export function onKeyDown(ctrl, e) {
  if (isTyping(e.target)) return;
  if (e.target.closest?.("button") && [" ", "Enter", "Delete", "Backspace"].includes(e.key)) return;
  if (ctrl.tourPlayer?.active && ctrl.tourPlayer.handleKeyDown?.(e)) return;
  const mod = e.ctrlKey || e.metaKey;
  const key = String(e.key || "").toLowerCase();
  if (mod && !e.altKey && key === "f") {
    e.preventDefault();
    ctrl.searchBar?.open();
  } else if (mod && !e.altKey && !e.shiftKey && e.key === "0") {
    e.preventDefault();
    ctrl.resetTransform();
  } else if (mod && !e.altKey && !e.shiftKey && e.key === "1") {
    e.preventDefault();
    ctrl.fitView();
  } else if (!mod && !e.altKey && e.key === "F2") {
    if (ctrl.selected.size === 1) {
      e.preventDefault();
      ctrl.beginTitleEdit([...ctrl.selected][0]);
    }
  } else if (!mod && !e.altKey && e.key === "?") {
    e.preventDefault();
    ctrl.openShortcutsDialog();
  } else if (mod && !e.altKey && (key === "z" || key === "y")) {
    e.preventDefault();
    if (key === "y" || e.shiftKey) ctrl.redo();
    else ctrl.undo();
  } else if (e.key === " ") {
    ctrl.spaceDown = true;
    e.preventDefault();
  } else if (e.key === "Delete" || e.key === "Backspace") {
    e.preventDefault();
    ctrl.deleteSelection();
  } else if (mod && (e.key === "c" || e.key === "C") && !e.shiftKey && !e.altKey) {
    if (hasTextSelection(ctrl.rootEl) || !ctrl.selected.size) return;
    e.preventDefault();
    ctrl.copySelection();
  } else if (mod && (e.key === "v" || e.key === "V") && !e.shiftKey && !e.altKey) {
    if (hasTextSelection(ctrl.rootEl)) return;
    if (ctrl.pasteClipboard()) e.preventDefault();
  } else if (mod && (e.key === "d" || e.key === "D") && !e.shiftKey && !e.altKey) {
    if (!ctrl.selected.size) return;
    e.preventDefault();
    ctrl.duplicateSelection();
  } else if (mod && (e.key === "a" || e.key === "A")) {
    e.preventDefault();
    ctrl.selected = new Set(ctrl.canvas.nodes.map((n) => n.id));
    ctrl.selectedEdge = null;
    ctrl.commit({ save: false });
  } else if (e.key === "Escape") {
    if (ctrl.searchBar?.isOpen?.()) {
      ctrl.searchBar.close();
      return;
    }
    ctrl.active = null;
    ctrl.selected.clear();
    ctrl.selectedEdge = null;
    ctrl.commit({ save: false });
  } else if (e.key.startsWith("Arrow") && ctrl.selected.size) {
    e.preventDefault();
    const step = e.shiftKey ? (ctrl.canvas.settings.grid || 10) : 1;
    const dx = e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0;
    const dy = e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0;
    const moved = M.dragSet(ctrl.canvas.nodes, [...ctrl.selected]);
    for (const n of moved) {
      n.x += dx;
      n.y += dy;
    }
    const ids = new Set(moved.map((n) => n.id));
    M.reassignGroups(ctrl.canvas.nodes, moved.filter((n) => !(n.groupId && ids.has(n.groupId))));
    ctrl.commit({ coalesce: `arrow:${[...ctrl.selected].join(",")}` });
  }
}

export function onContextMenu(ctrl, e) {
  e.preventDefault();
  e.stopPropagation();
  const nodeEl = e.target.closest(".nm-node");
  const edgeEl = e.target.closest(".nm-edge[data-edge-id]");
  const at = ctrl.toCanvas(e.clientX, e.clientY);
  if (nodeEl) {
    const node = ctrl.byId(nodeEl.dataset.nodeId);
    if (!node) return;
    if (!ctrl.selected.has(node.id)) selectOnly(ctrl, node.id);
    const row = e.target.closest(".nm-sf[data-key]");
    contextMenu.showNodeMapMenu(e.clientX, e.clientY, { node, fieldKey: row?.dataset.key || null, controller: ctrl });
  } else if (edgeEl) {
    selectEdge(ctrl, edgeEl.dataset.edgeId);
    contextMenu.showNodeMapMenu(e.clientX, e.clientY, { edgeId: edgeEl.dataset.edgeId, at, controller: ctrl });
  } else {
    contextMenu.showNodeMapMenu(e.clientX, e.clientY, { isCanvas: true, at, controller: ctrl });
  }
}

export function attachCanvasEvents(ctrl) {
  const wrap = ctrl.wrapEl;
  wrap.addEventListener("pointerdown", (e) => ctrl.onPointerDown(e));
  wrap.addEventListener("click", (e) => ctrl.onClick(e));
  wrap.addEventListener("dblclick", (e) => ctrl.onDblClick(e));
  wrap.addEventListener("wheel", (e) => ctrl.onWheel(e), { passive: false });
  wrap.addEventListener("contextmenu", (e) => ctrl.onContextMenu(e));
  wrap.addEventListener("scroll", (ev) => {
    const nEl = ev.target?.closest?.(".nm-node");
    const id = nEl?.dataset?.nodeId;
    if (id) {
      ctrl.refreshPortOffsets(id);
      ctrl.scheduleWires(new Set([id]));
    } else {
      ctrl.scheduleWires();
    }
  }, true);
  if (ctrl.breadcrumbsEl) {
    ctrl.breadcrumbsEl.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-bc-depth]");
      if (!btn) return;
      ctrl.exitSubMapTo(parseInt(btn.dataset.bcDepth, 10));
    });
  }
  ctrl.rootEl.addEventListener("keydown", (e) => ctrl.onKeyDown(e));
  ctrl.rootEl.addEventListener("keyup", (e) => {
    if (e.key === " ") ctrl.spaceDown = false;
    if (String(e.key).startsWith("Arrow")) ctrl.history.close();
  });
  ctrl.inspectorEl.addEventListener("change", (e) => ctrl.onInspectorInput(e, true));
  ctrl.inspectorEl.addEventListener("input", (e) => ctrl.onInspectorInput(e, false));
  ctrl.inspectorEl.addEventListener("click", (e) => ctrl.onInspectorClick(e));
  if (!ctrl.onWinMove) {
    ctrl.onWinMove = (e) => ctrl.onPointerMove(e);
    ctrl.onWinUp = (e) => ctrl.onPointerUp(e);
    window.addEventListener("pointermove", ctrl.onWinMove);
    window.addEventListener("pointerup", ctrl.onWinUp);
    window.addEventListener("pointercancel", ctrl.onWinUp);
    ctrl.onMessage = (e) => {
      if (e.origin !== location.origin || e.data?.type !== "kip-embed-escape" || !ctrl.active) return;
      const frame = ctrl.renderer?.element(ctrl.active)?.querySelector("iframe");
      if (frame && e.source === frame.contentWindow) {
        ctrl.active = null;
        ctrl.rootEl?.focus({ preventScroll: true });
        ctrl.commit({ save: false });
      }
    };
    window.addEventListener("message", ctrl.onMessage);
  }
}

// ------------------------------------------------------------------ inline editing

export function inlineInput(ctrl, host, value, { multiline = false, onCommit, onDone = null }) {
  const input = document.createElement(multiline ? "textarea" : "input");
  input.className = `nm-inline-edit${multiline ? " nm-inline-multi" : ""}`;
  input.value = value;
  host.appendChild(input);
  input.focus();
  if (!multiline) input.select();
  let done = false;
  const finish = (commit, refocus = false) => {
    if (done) return;
    done = true;
    const v = input.value;
    input.remove();
    onDone?.();
    if (commit) onCommit(v);
    else ctrl.commit({ save: false });
    if (refocus) ctrl.rootEl?.focus({ preventScroll: true });
  };
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape") finish(multiline, true);
    else if (e.key === "Enter" && (!multiline || e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      finish(true, true);
    }
  });
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("pointerdown", (e) => e.stopPropagation());
  return input;
}

export function beginTitleEdit(ctrl, id) {
  const node = ctrl.byId(id);
  const titleEl = ctrl.renderer?.element(id)?.querySelector(".nm-title");
  if (!node || !titleEl) return;
  titleEl.textContent = "";
  inlineInput(ctrl, titleEl, M.displayTitle(node), {
    onCommit: (v) => ctrl.setTitle(id, v),
  });
}

export function setTitle(ctrl, id, value) {
  const node = ctrl.byId(id);
  if (!node) return;
  const v = String(value || "").trim().slice(0, 512);
  node.customTitle = v && v !== node.title ? v : "";
  const el = ctrl.renderer.element(id)?.querySelector(".nm-title");
  if (el) el.textContent = M.displayTitle(node);
  ctrl.commit();
}

export function beginNoteEdit(ctrl, id) {
  const node = ctrl.byId(id);
  const body = ctrl.renderer?.element(id)?.querySelector(".nm-node-body");
  if (!node || !body) return;
  body.innerHTML = "";
  inlineInput(ctrl, body, node.data.text || "", {
    multiline: true,
    onCommit: (v) => {
      node.data.text = v.slice(0, 20000);
      ctrl.renderer.invalidate(id);
      ctrl.commit();
    },
  });
}

export function beginEdgeLabelEdit(ctrl, id) {
  const edge = ctrl.canvas.edges.find((x) => x.id === id);
  const g = ctrl.edgeGeoms?.get(id);
  if (!edge || !g) return;
  const host = document.createElement("div");
  host.className = "nm-edge-edit";
  Object.assign(host.style, { left: `${g.mid.x}px`, top: `${g.mid.y}px` });
  ctrl.layerEl.appendChild(host);
  inlineInput(ctrl, host, edge.label, {
    onDone: () => host.remove(),
    onCommit: (v) => {
      edge.label = String(v || "").trim().slice(0, 512);
      ctrl.commit();
    },
  });
}

// ------------------------------------------------------------------ node click, spawn & graph expansion actions

export function spawnLinkedNode(ctrl, from, spec, label = "") {
  if (!from || !spec) return null;
  const siblingCount = ctrl.canvas.edges.filter((e) => e.from === from.id).length;
  const node = ctrl.nodeFromSpec(spec, { x: from.x + from.w + 100, y: from.y + siblingCount * 110 });
  if (!node) return null;
  ctrl.canvas.nodes.push(node);
  M.reassignGroups(ctrl.canvas.nodes, [node]);
  const edge = M.createEdge({ from: from.id, to: node.id, label });
  if (edge) ctrl.canvas.edges.push(edge);
  ctrl.selected = new Set([node.id]);
  ctrl.selectedEdge = null;
  ctrl.commit();
  return node;
}

export function spawnFromCveDiff(ctrl, node, key) {
  const raw = String(key || "");
  const idx = raw.lastIndexOf(":");
  const sha = idx > 0 ? raw.slice(0, idx) : raw;
  const kind = idx > 0 ? raw.slice(idx + 1) : "fixes";
  if (!sha) return null;
  return spawnLinkedNode(
    ctrl,
    node,
    { type: "diff", version: node.version, repo: node.repo, data: { mode: "commit", commitHash: sha, path: "" } },
    kind === "intro" ? "introduced" : "fixes",
  );
}

export function onClick(ctrl, e) {
  const btn = e.target.closest("[data-act]");
  const nodeEl = e.target.closest(".nm-node");
  if (!btn || !nodeEl) return;
  const node = ctrl.byId(nodeEl.dataset.nodeId);
  if (!node) return;
  e.stopPropagation();
  const act = btn.dataset.act;
  if (EDIT_ACTS.has(act) && !ctrl.canvas.settings.editMode) return;
  if (act === "interact") {
    ctrl.active = ctrl.active === node.id ? null : node.id;
    ctrl.selected = new Set([node.id]);
    ctrl.commit({ save: false });
    if (ctrl.active) {
      const frame = ctrl.renderer?.element?.(node.id)?.querySelector?.("iframe");
      try {
        frame?.focus?.();
        frame?.contentWindow?.focus?.();
      } catch {
        // a frame still loading
      }
    }
  } else if (act === "toggle-header") ctrl.setHeaderHidden(node.id, !node.hideHeader);
  else if (act === "open-tab") ctrl.openNodeAsTab(node.id);
  else if (act === "refresh") ctrl.refreshNode(node.id);
  else if (act === "expand") ctrl.toggleExpandNode(node.id);
  else if (act === "field-lock") ctrl.promptFieldLock(node, btn.dataset.key);
  else if (act === "mail-open") ctrl.toggleMailMessage(node.id, btn.dataset.key);
  else if (act === "mail-spawn") ctrl.spawnMailMessages(node.id, [btn.dataset.key]);
  else if (act === "mail-expand") ctrl.expandThreadToNodes(node.id);
  else if (act === "kc-rel") ctrl.expandKconfigRelations(node.id, btn.dataset.key);
  else if (act === "kc-expand") ctrl.expandKconfigRelations(node.id);
  else if (act === "follow-type") ctrl.followStructType(node.id, btn.dataset.key);
  else if (act === "cve-spawn-diff") spawnFromCveDiff(ctrl, node, btn.dataset.key);
  else if (act === "cve-spawn-source") spawnLinkedNode(ctrl, node, { type: "source", version: node.version, data: { path: btn.dataset.key, mode: "file" } }, "affects");
  else if (act === "cve-spawn-cve") spawnLinkedNode(ctrl, node, cveSpec({ cveId: btn.dataset.key, version: node.version, repo: node.repo }), "related");
  else if (act === "kapi-catalog") spawnLinkedNode(ctrl, node, kapiSpec({ catalog: btn.dataset.key, arch: node.data.arch, version: node.version, repo: node.repo }), "contains");
  else if (act === "kapi-spawn-symbol") spawnLinkedNode(ctrl, node, { type: "symbol", version: node.version, repo: node.repo, data: { name: btn.dataset.key, kind: "func" } }, "exposes");
  else if (act === "kapi-spawn-man") {
    const idx = String(btn.dataset.key || "").lastIndexOf(":");
    const name = idx >= 0 ? btn.dataset.key.slice(0, idx) : btn.dataset.key;
    const section = idx >= 0 ? btn.dataset.key.slice(idx + 1) : "2";
    spawnLinkedNode(ctrl, node, { type: "man", version: node.version, repo: node.repo, data: { name, section } }, "documented by");
  } else if (act === "kapi-spawn-source" || act === "doc-spawn-source") {
    const raw = String(btn.dataset.key || "");
    const idx = raw.lastIndexOf(":");
    const path = idx > 0 ? raw.slice(0, idx) : raw;
    const line = idx > 0 ? Math.max(1, parseInt(raw.slice(idx + 1), 10) || 1) : 1;
    spawnLinkedNode(ctrl, node, { type: "source", version: node.version, repo: node.repo, data: { path, mode: "range", start: line, end: line + 30 } }, act === "doc-spawn-source" ? "documents" : "defined in");
  } else if (act === "hist-spawn-commit") {
    spawnLinkedNode(ctrl, node, { type: "diff", version: node.version, repo: node.repo, data: { mode: "commit", commitHash: btn.dataset.key, path: "" } }, "blame commit");
  } else if (act === "hist-spawn-diff") {
    const raw = String(btn.dataset.key || "");
    const sep = raw.indexOf(":");
    const revRange = sep >= 0 ? raw.slice(0, sep) : raw;
    const path = sep >= 0 ? raw.slice(sep + 1) : node.data.path;
    const [versionA, versionB] = revRange.split("..");
    spawnLinkedNode(ctrl, node, { type: "diff", version: versionB || node.version, repo: node.repo, data: { mode: "file", versionA, versionB, path } }, "changed in");
  } else if (node.type === "struct") ctrl.onStructAction(node, act, btn.dataset.key);
}

export async function toggleMailMessage(ctrl, id, mid) {
  const node = ctrl.byId(id);
  if (!node || node.type !== "mail" || !mid) return;
  const open = { ...node.data.openMids };
  if (open[mid]) delete open[mid];
  else open[mid] = true;
  node.data.openMids = open;
  if (node.autoFit) ctrl.fitQueue.add(id);
  ctrl.commit();
  if (!open[mid] || ctrl.mailBodies.has(mid) || mid.startsWith("#")) return;
  try {
    const res = await (ctrl.api || api).getMailMessage(mid);
    ctrl.mailBodies.set(mid, String(res?.body || res?.text || ""));
  } catch (err) {
    ctrl.mailBodies.set(mid, `(${err.message})`);
  }
  ctrl.mailEpoch = (ctrl.mailEpoch || 0) + 1;
  for (const n of ctrl.canvas.nodes) if (n.type === "mail" && n.autoFit) ctrl.fitQueue.add(n.id);
  if (ctrl.containerEl?.isConnected || !ctrl.containerEl) ctrl.commit({ save: false, inspector: false });
}

export function mailNodeOf(ctrl, mid) {
  return ctrl.canvas.nodes.find((n) => n.type === "mail" && n.data.mid === mid && n.data.single) || null;
}

export function spawnMailMessages(ctrl, id, mids, { select = true, layout = null } = {}) {
  const thread = ctrl.byId(id);
  const model = thread ? ctrl.modelOf(thread) : null;
  if (!model) return [];
  const rows = new Map(model.rows.map((r) => [r.mid, r]));
  const created = [];
  const place = layout ? new Map(layout.map((p) => [p.mid, p])) : null;
  let k = 0;
  for (const mid of mids) {
    const r = rows.get(mid);
    if (!r) continue;
    let n = ctrl.mailNodeOf(mid);
    if (!n) {
      const at = place?.get(mid) || { x: thread.x + thread.w + 80, y: thread.y + k * 140 };
      n = M.createNode("mail", {
        x: at.x, y: at.y, w: 320, h: 150, version: thread.version, repo: thread.repo,
        data: { tid: thread.data.tid, mid, subject: r.subject, single: true },
      });
      ctrl.canvas.nodes.push(n);
      created.push(n);
      k += 1;
    }
    const parentNode = (r.parent && ctrl.mailNodeOf(r.parent)) || thread;
    if (parentNode.id !== n.id && !ctrl.canvas.edges.some((e) => e.from === parentNode.id && e.to === n.id)) {
      const edge = M.createEdge({ from: parentNode.id, to: n.id, label: "reply" });
      if (edge) ctrl.canvas.edges.push(edge);
    }
  }
  if (created.length) M.reassignGroups(ctrl.canvas.nodes, created);
  if (select && created.length) {
    ctrl.selected = new Set(created.map((x) => x.id));
    ctrl.selectedEdge = null;
  }
  ctrl.commit();
  return created;
}

export function expandThreadToNodes(ctrl, id) {
  const thread = ctrl.byId(id);
  const model = thread ? ctrl.modelOf(thread) : null;
  if (!model?.rows.length) return toast.info("The thread has not loaded yet"), [];
  const rows = model.rows.slice(0, MAIL_EXPAND_MAX);
  const layout = mailThreadLayout({ rows }, { x: thread.x + thread.w + 80, y: thread.y }, { w: 320, h: 150 });
  const created = ctrl.spawnMailMessages(id, rows.map((r) => r.mid), { layout });
  if (model.rows.length > rows.length) toast.info(`Added the first ${rows.length} of ${model.rows.length} messages`);
  return created;
}

export function expandKconfigRelations(ctrl, id, only = "") {
  const node = ctrl.byId(id);
  const model = node ? ctrl.modelOf(node) : null;
  if (!model) return toast.info("The symbol has not loaded yet"), [];
  const targets = relationTargets(model, only).slice(0, only ? 1 : KC_EXPAND_MAX);
  const created = [];
  const count = { out: 0, in: 0 };
  for (const t of targets) {
    let other = ctrl.canvas.nodes.find((n) => n.type === "kconfig" && n.data.symbol === t.name && n.data.arch === node.data.arch && n.version === node.version);
    if (!other) {
      const k = count[t.dir]++;
      const lim = M.SIZE_LIMITS.kconfig;
      other = M.createNode("kconfig", {
        x: t.dir === "out" ? node.x + node.w + 120 : node.x - lim.w - 120,
        y: node.y + k * (lim.h * 0.6 + 24),
        h: Math.round(lim.h * 0.6),
        version: node.version,
        repo: node.repo,
        data: { symbol: t.name, arch: node.data.arch },
      });
      ctrl.canvas.nodes.push(other);
      created.push(other);
    }
    const [from, to] = t.dir === "out" ? [node.id, other.id] : [other.id, node.id];
    if (!ctrl.canvas.edges.some((e) => e.from === from && e.to === to && e.label === t.edge)) {
      const edge = M.createEdge({ from, to, label: t.edge, style: t.edge === "depends on" ? "solid" : "dashed" });
      if (edge) ctrl.canvas.edges.push(edge);
    }
  }
  if (created.length) {
    M.reassignGroups(ctrl.canvas.nodes, created);
    ctrl.selected = new Set(created.map((x) => x.id));
    ctrl.selectedEdge = null;
  }
  ctrl.commit();
  return created;
}

export function onStructAction(ctrl, node, act, key) {
  const d = node.data;
  if (act === "all-types") M.setAllTypes(d, !d.showTypes);
  else if (act === "guards") d.showGuards = !d.showGuards;
  else if (act === "collapse" && key) {
    if (d.collapsed[key]) delete d.collapsed[key];
    else d.collapsed[key] = true;
  } else if (act === "toggle-type" && key) {
    const layout = ctrl.content.get(ctrl.fetchKeyOf(node))?.value;
    const item = layout ? findItem(layout.items, key) : null;
    if (item) M.toggleItemType(d, item);
  } else if (act === "guard-fold" && key) {
    const layout = ctrl.content.get(ctrl.fetchKeyOf(node))?.value;
    M.toggleGuardCollapse(d, key, guardAutoCollapsed(layout, key, ctrl.evaluatorFor(node)));
  } else if (act === "hide-item" && key) {
    M.toggleHidden(d, key);
  } else if (act === "show-hidden") {
    M.showAllHidden(d);
  } else if (act === "style") {
    const idx = M.STRUCT_STYLES.indexOf(d.style);
    d.style = M.STRUCT_STYLES[(idx + 1) % M.STRUCT_STYLES.length];
  } else return;
  if (node.autoFit) ctrl.fitQueue.add(node.id);
  ctrl.commit();
}

export async function toggleExpandNode(ctrl, id) {
  const node = ctrl.byId(id);
  if (!node || node.type !== "symbol" || ctrl.expanding?.has(id)) return;
  const d = node.data;
  d.expanded = !d.expanded;
  ctrl.expanding = ctrl.expanding || new Set();
  ctrl.expanding.add(id);
  if (d.expanded && !d.constituents?.length) {
    const client = ctrl.clientFor(node);
    try {
      let astId = d.astId;
      if (!astId) {
        const detail = await client.getSymbolDetail(node.version, d.name);
        astId = detail?.definition?.ast_id || null;
      }
      if (astId) {
        const res = await client.getAstTree(node.version, astId);
        const children = res.node?.children || res.children || [];
        d.constituents = children.map((c) => ({ name: c.spelling || c.name || c.type_name || "" })).filter((c) => c.name);
      }
    } catch (err) {
      toast.warning(`No members for ${d.name}: ${err.message}`);
    }
  }
  ctrl.expanding.delete(id);
  ctrl.commit();
}

export function followableTypeFor(ctrl, nodeId, fieldKey) {
  const node = ctrl.byId(nodeId);
  if (!node || node.type !== "struct" || !fieldKey) return null;
  const layout = ctrl.content.get(ctrl.fetchKeyOf(node))?.value;
  const item = layout ? findItem(layout.items, fieldKey) : null;
  const ref = item ? extractReferencedStruct(item.type || item.type_name || "") : null;
  return ref ? `${ref.kind} ${ref.name}` : null;
}

export function followStructType(ctrl, nodeId, fieldKey) {
  const node = ctrl.byId(nodeId);
  if (!node || node.type !== "struct" || !fieldKey) return null;
  const layout = ctrl.content.get(ctrl.fetchKeyOf(node))?.value;
  const item = layout ? findItem(layout.items, fieldKey) : null;
  const ref = item ? extractReferencedStruct(item.type || item.type_name || "") : null;
  if (!ref) return null;
  let target = ctrl.canvas.nodes.find((n) => n.type === "struct" && n.data.name === ref.name && n.data.kind === ref.kind && n.version === node.version);
  if (!target) {
    const siblingCount = ctrl.canvas.edges.filter((e) => e.from === node.id).length;
    target = M.createNode("struct", {
      x: node.x + node.w + 110, y: node.y + siblingCount * 120, version: node.version, repo: node.repo,
      data: { name: ref.name, kind: ref.kind },
    });
    ctrl.canvas.nodes.push(target);
    M.reassignGroups(ctrl.canvas.nodes, [target]);
  }
  if (!ctrl.canvas.edges.some((e) => e.from === node.id && e.to === target.id && e.fromField === fieldKey)) {
    const edge = M.createEdge({ from: node.id, to: target.id, fromSide: "right", fromField: fieldKey, toSide: "left", label: ref.edgeLabel });
    if (edge) ctrl.canvas.edges.push(edge);
  }
  ctrl.selected = new Set([target.id]);
  ctrl.selectedEdge = null;
  ctrl.commit();
  return target;
}

export async function expandCallgraph(ctrl, id, direction = "both") {
  const node = ctrl.byId(id);
  if (!node || node.type !== "symbol" || !node.data?.name) return [];
  const client = ctrl.clientFor(node);
  const ver = node.version || ctrl.currentVersion;
  let res;
  try {
    res = await client.getCallgraph(ver, node.data.name);
  } catch (err) {
    toast.error(`Callgraph failed: ${err.message}`);
    return [];
  }
  const created = [];
  const addSide = (entries, isCaller) => {
    const list = Array.isArray(entries) ? entries.slice(0, 30) : [];
    const lim = M.SIZE_LIMITS.symbol;
    list.forEach((raw, idx) => {
      const fnName = typeof raw === "string" ? raw : (raw?.name || raw?.function || raw?.caller || raw?.callee || "");
      if (!fnName) return;
      let other = ctrl.canvas.nodes.find((n) => n.type === "symbol" && n.data.name === fnName && n.version === ver);
      if (!other) {
        other = M.createNode("symbol", {
          x: isCaller ? node.x - lim.w - 110 : node.x + node.w + 110,
          y: node.y + idx * (lim.h + 24),
          version: ver,
          repo: node.repo,
          data: { name: fnName, kind: "func", astId: raw?.ast_id || null },
        });
        ctrl.canvas.nodes.push(other);
        created.push(other);
      }
      const [from, to] = isCaller ? [other.id, node.id] : [node.id, other.id];
      if (!ctrl.canvas.edges.some((e) => e.from === from && e.to === to && e.label === "calls")) {
        const sitePath = typeof raw === "object" ? (raw?.call_site_path || raw?.file_path || raw?.path || "") : "";
        const siteLine = typeof raw === "object" ? (raw?.call_site_line || raw?.line || null) : null;
        const edge = M.createEdge({ from, to, label: "calls", callSite: sitePath ? { path: sitePath, line: siteLine } : null });
        if (edge) ctrl.canvas.edges.push(edge);
      }
    });
  };
  if (direction === "callers" || direction === "both") addSide(res?.callers, true);
  if (direction === "callees" || direction === "both") addSide(res?.callees, false);
  if (created.length) {
    M.reassignGroups(ctrl.canvas.nodes, created);
    ctrl.selected = new Set(created.map((x) => x.id));
  }
  ctrl.commit();
  return created;
}

export async function expandCallPath(ctrl, fromId, targetFn, { maxDepth = 6, k = 3 } = {}) {
  const node = ctrl.byId(fromId);
  if (!node || !targetFn) return [];
  const client = ctrl.clientFor(node);
  const ver = node.version || ctrl.currentVersion;
  let res;
  try {
    res = await client.getCallpath(ver, { from: node.data?.name || "", to: targetFn, maxDepth, k });
  } catch (err) {
    toast.error(`Call path failed: ${err.message}`);
    return [];
  }
  const paths = Array.isArray(res?.paths) ? res.paths : [];
  const created = [];
  const lim = M.SIZE_LIMITS.symbol;
  paths.forEach((chain, pIdx) => {
    const steps = Array.isArray(chain) ? chain : (chain?.nodes || []);
    let prev = node;
    steps.forEach((step, sIdx) => {
      const fnName = typeof step === "string" ? step : (step?.name || step?.function || "");
      if (!fnName || (sIdx === 0 && fnName === node.data?.name)) return;
      let cur = ctrl.canvas.nodes.find((n) => n.type === "symbol" && n.data.name === fnName && n.version === ver);
      if (!cur) {
        cur = M.createNode("symbol", {
          x: node.x + (sIdx + 1) * (lim.w + 90), y: node.y + pIdx * (lim.h + 40), version: ver, repo: node.repo,
          data: { name: fnName, kind: "func", astId: step?.ast_id || null },
        });
        ctrl.canvas.nodes.push(cur);
        created.push(cur);
      }
      if (prev.id !== cur.id && !ctrl.canvas.edges.some((e) => e.from === prev.id && e.to === cur.id && e.label === "calls")) {
        const sitePath = typeof step === "object" ? (step?.call_site_path || step?.file_path || step?.path || "") : "";
        const siteLine = typeof step === "object" ? (step?.call_site_line || step?.line || null) : null;
        const edge = M.createEdge({ from: prev.id, to: cur.id, label: "calls", callSite: sitePath ? { path: sitePath, line: siteLine } : null });
        if (edge) ctrl.canvas.edges.push(edge);
      }
      prev = cur;
    });
  });
  if (created.length) {
    M.reassignGroups(ctrl.canvas.nodes, created);
    ctrl.selected = new Set(created.map((x) => x.id));
  }
  ctrl.commit();
  return created;
}
