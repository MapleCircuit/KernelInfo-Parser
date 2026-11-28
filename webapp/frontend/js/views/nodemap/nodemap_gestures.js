/**
 * nodemap_gestures.js - NodeMap pointer, drag, resize, marquee, wire, waypoint, smart snap
 * alignment guides, Alt+Drag clone-on-drag, Shift+Drag axis locking, and keyboard shortcuts.
 */
import { api } from "../../api.js";
import { contextMenu } from "../../components/context_menu.js";
import { toast } from "../../components/toast.js";
import { EMBED_MSG } from "../../utils/embed_mode.js";
import * as M from "./canvas_model.js";
import { commitRowKey, fullMailBody, mailRowKey, mailThreadLayout, relationTargets, releaseRowKey } from "./domain_nodes.js";
import { cveSpec, kapiSpec } from "./nodemap_bridge.js";
import { collectExpandableTypes, findItem, guardAutoCollapsed, typeRefOfField } from "./struct_view.js";

const DRAG_THRESHOLD = 3;
const ALIGN_SNAP_THRESHOLD = 6;
const EDIT_ACTS = new Set(["toggle-type", "hide-item", "all-types", "guards", "toggle-header", "field-lock"]);
const MAIL_EXPAND_MAX = 60;
const KC_EXPAND_MAX = 40;
const TYPE_EXPAND_MAX = 40;
/** A bulk Blame expand that would add more new nodes than this shows a confirmation first. */
export const BLAME_CONFIRM_ABOVE = 20;

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
  if (ctrl.tourExporting) return;
  const t = e.target;
  if (t.closest?.(".nm-breadcrumbs")) return;
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
  if (ctrl.tourExporting) return;
  if (e.target.closest?.(".nm-breadcrumbs")) return;
  if (isTyping(e.target)) return;
  const wpEl = e.target.closest?.(".nm-edge-wp[data-edge-id]");
  if (wpEl) {
    ctrl.removeEdgeWaypoint(wpEl.dataset.edgeId, parseInt(wpEl.dataset.wpIdx, 10));
    return;
  }
  const titleEl = e.target.closest(".nm-title");
  const nodeEl = e.target.closest(".nm-node");
  const edgeEl = e.target.closest(".nm-edge[data-edge-id]");
  const nestedNode = nodeEl ? ctrl.byId(nodeEl.dataset.nodeId) : null;
  if (nestedNode?.type === "nodemap" && !titleEl && !e.target.closest("button")) return ctrl.openNested(nestedNode.id);
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
  if (ctrl.tourExporting) {
    e.preventDefault?.();
    return;
  }
  const inContent = e.target.closest(".nm-scroll");
  if (inContent && !e.ctrlKey && !e.metaKey) return;
  e.preventDefault();
  const factor = Math.exp(-Math.max(-60, Math.min(60, e.deltaY)) * (e.ctrlKey ? 0.01 : 0.0025));
  ctrl.adjustZoom(factor, e.clientX, e.clientY);
}

export function onKeyDown(ctrl, e) {
  if (ctrl.tourExporting) {
    if (e.key === "Escape") {
      e.preventDefault?.();
      ctrl.cancelTourFrameExport?.();
    }
    return;
  }
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
  if (ctrl.tourExporting) {
    e.preventDefault?.();
    return;
  }
  if (e.target.closest?.(".nm-breadcrumbs")) return;
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
    ctrl.breadcrumbsEl.addEventListener("pointerdown", (e) => e.stopPropagation());
    ctrl.breadcrumbsEl.addEventListener("dblclick", (e) => e.stopPropagation());
    ctrl.breadcrumbsEl.addEventListener("contextmenu", (e) => e.stopPropagation());
    ctrl.breadcrumbsEl.addEventListener("click", (e) => {
      if (ctrl.tourExporting) return;
      const nested = e.target.closest("[data-nm-crumb]");
      if (nested) {
        ctrl.goToCrumb(parseInt(nested.dataset.nmCrumb, 10));
        return;
      }
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
      if (e.origin !== location.origin) return;
      // embed shells reporting their state (validated against the node's own iframe there)
      if (e.data?.type === EMBED_MSG.hello || e.data?.type === EMBED_MSG.state) {
        ctrl.onTabFrameMessage?.(e);
        return;
      }
      if (e.data?.type !== EMBED_MSG.escape || !ctrl.active) return;
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

/**
 * Add the node of `spec` to the right of `from`, connected by an edge labeled `label`. `opts.fromField`
 * anchors the edge to one row of `from` (a struct member, a mail message `mail:<mid>`, a history row
 * `commit:<sha>` / `ver:<release>`).
 */
export function spawnLinkedNode(ctrl, from, spec, label = "", { fromField = null } = {}) {
  if (!from || !spec) return null;
  const siblingCount = ctrl.canvas.edges.filter((e) => e.from === from.id).length;
  const node = ctrl.nodeFromSpec(spec, { x: from.x + from.w + 100, y: from.y + siblingCount * 110 });
  if (!node) return null;
  ctrl.canvas.nodes.push(node);
  M.reassignGroups(ctrl.canvas.nodes, [node]);
  const edge = M.createEdge(fromField
    ? { from: from.id, to: node.id, label, fromSide: "right", fromField, toSide: "left" }
    : { from: from.id, to: node.id, label });
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
  if (ctrl.tourExporting) return;
  const btn = e.target.closest("[data-act]");
  const nodeEl = e.target.closest(".nm-node");
  if (!btn && nodeEl && e.target.closest(".nm-doc-page")) return onDocLink(ctrl, e, nodeEl);
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
  } else if (act === "nested-open" && node.type === "nodemap") ctrl.openNested(node.id);
  else if (act === "nested-choose" && node.type === "nodemap") ctrl.chooseNestedTarget(node.id);
  else if (act === "nested-remove" && node.type === "nodemap") ctrl.removeNode(node.id);
  else if (act === "tab-freeze" && node.type === "tab") ctrl.setTabsFrozen([node.id], !node.data.frozen);
  else if (act === "tab-reset" && node.type === "tab") ctrl.resetTabNode(node.id);
  else if (act === "toggle-header") ctrl.setHeaderHidden(node.id, !node.hideHeader);
  else if (act === "open-tab") ctrl.openNodeAsTab(node.id);
  else if (act === "refresh") ctrl.refreshNode(node.id);
  else if (act === "expand") ctrl.toggleExpandNode(node.id);
  else if (act === "field-lock") ctrl.promptFieldLock(node, btn.dataset.key);
  else if (act === "mail-open") ctrl.toggleMailMessage(node.id, btn.dataset.key);
  else if (act === "mail-full") ctrl.toggleMailMessage(node.id, btn.dataset.key, "full");
  else if (act === "mail-pin") ctrl.toggleMailMessage(node.id, btn.dataset.key, "pin");
  else if (act === "mail-spawn") ctrl.spawnMailMessages(node.id, [btn.dataset.key]);
  else if (act === "mail-expand") ctrl.expandThreadToNodes(node.id);
  else if (act === "kc-rel") ctrl.expandKconfigRelations(node.id, btn.dataset.key);
  else if (act === "kc-expand") ctrl.expandKconfigRelations(node.id);
  else if (act === "follow-type") ctrl.followStructType(node.id, btn.dataset.key);
  else if (act === "expand-types") ctrl.expandStructTypes(node.id);
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
  } else if (act === "hist-spawn-commit" || act === "hist-spawn-commit-detail") {
    const c = ctrl.modelOf(node)?.commits?.find((x) => x.commitHash === btn.dataset.key) || { commitHash: btn.dataset.key, subject: "" };
    const target = linkBlameCommit(ctrl, node, c, act === "hist-spawn-commit" ? "diff" : "commit");
    if (target) {
      ctrl.selected = new Set([target.id]);
      ctrl.selectedEdge = null;
      ctrl.commit();
    }
  } else if (act === "blame-expand-commits") ctrl.expandBlameToNodes(node.id, "commit");
  else if (act === "blame-expand-diffs") ctrl.expandBlameToNodes(node.id, "diff");
  else if (act === "commit-spawn-diff") ctrl.spawnCommitDiff(node.id);
  else if (act === "commit-spawn-parent") spawnCommitLink(ctrl, node, { type: "commit", hash: btn.dataset.key, label: "parent" });
  else if (act === "commit-spawn-ref") {
    const raw = String(btn.dataset.key || "");
    const idx = raw.indexOf(":");
    spawnCommitLink(ctrl, node, { type: "commit", hash: idx > 0 ? raw.slice(0, idx) : raw, label: idx > 0 ? raw.slice(idx + 1) : "refers to" });
  } else if (act === "commit-spawn-file") spawnCommitLink(ctrl, node, { type: "diff", hash: node.data.commitHash, path: btn.dataset.key, label: "changes" });
  else if (act === "hist-spawn-diff") {
    const raw = String(btn.dataset.key || "");
    const sep = raw.indexOf(":");
    const revRange = sep >= 0 ? raw.slice(0, sep) : raw;
    const path = sep >= 0 ? raw.slice(sep + 1) : node.data.path;
    const [versionA, versionB] = revRange.split("..");
    spawnLinkedNode(ctrl, node, { type: "diff", version: versionB || node.version, repo: node.repo, data: { mode: "file", versionA, versionB, path } }, "changed in", { fromField: versionB ? releaseRowKey(versionB) : null });
  } else if (node.type === "struct") ctrl.onStructAction(node, act, btn.dataset.key);
}

/**
 * A click on a link of a rendered doc node: documentation links (`data-doc`) and kernel-doc directives
 * add a connected doc node, `data-src` a source slice, `data-symbol` a symbol node, `data-kconfig` a
 * Kconfig node; `#anchor` links scroll inside the node; external links open in a new browser tab.
 */
export function onDocLink(ctrl, e, nodeEl) {
  const node = ctrl.byId(nodeEl.dataset.nodeId);
  if (!node) return;
  const kd = e.target.closest(".kdoc-kerneldoc[data-kdoc-path]");
  const a = e.target.closest("a");
  if (!a && !kd) return;
  if (a && /^(https?|mailto):/i.test(a.getAttribute("href") || "")) return; // external: the browser opens it (target=_blank)
  e.preventDefault();
  e.stopPropagation();
  const base = { version: node.version, repo: node.repo };
  if (kd && !a) {
    spawnLinkedNode(ctrl, node, { type: "doc", ...base, data: { path: kd.dataset.kdocPath, mode: "kernel-doc" } }, "includes");
  } else if (a.dataset.doc) {
    const path = String(a.dataset.doc).split("#")[0];
    if (path) spawnLinkedNode(ctrl, node, { type: "doc", ...base, data: { path } }, "links to");
  } else if (a.dataset.src) {
    const line = Math.max(1, parseInt(a.dataset.line, 10) || 1);
    spawnLinkedNode(ctrl, node, { type: "source", ...base, data: { path: a.dataset.src, mode: "range", start: line, end: line + 30 } }, "documents");
  } else if (a.dataset.symbol) {
    spawnLinkedNode(ctrl, node, { type: "symbol", ...base, data: { name: a.dataset.symbol } }, "mentions");
  } else if (a.dataset.kconfig) {
    spawnLinkedNode(ctrl, node, { type: "kconfig", ...base, data: { symbol: a.dataset.kconfig } }, "mentions");
  } else {
    const href = a.getAttribute("href") || "";
    if (href.startsWith("#") && href.length > 1) {
      const page = nodeEl.querySelector(".nm-doc-page");
      let id = href.slice(1);
      try {
        id = decodeURIComponent(id);
      } catch {
        // a malformed escape: match the raw fragment
      }
      const target = [...(page?.querySelectorAll?.("[id]") || [])].find((x) => x.id === id);
      const sc = target?.closest?.(".nm-scroll");
      if (target && sc) sc.scrollTop += target.getBoundingClientRect().top - sc.getBoundingClientRect().top;
    }
  }
}

/**
 * Show or hide a message of a mail node. `mode` "open" toggles its excerpt, "full" the whole message,
 * "pin" saves the whole message with the canvas (`pinnedBodies`, capped by `canvas_model.js`).
 */
export async function toggleMailMessage(ctrl, id, mid, mode = "open") {
  let node = ctrl.byId(id);
  if (!node || node.type !== "mail" || !mid) return;
  let pinBody = null;
  if (mode === "pin" && node.data.pinnedBodies?.[mid] === undefined) {
    if (Object.keys(node.data.pinnedBodies || {}).length >= M.MAIL_PIN_MAX) return toast.info(`At most ${M.MAIL_PIN_MAX} messages are saved per thread node`);
    const body = await loadMailBody(ctrl, mid);
    if (body === null) return toast.warning("Could not load the message to pin it");
    pinBody = fullMailBody(body);
    // the node (and its state) as it is once the message has loaded
    node = ctrl.byId(id);
    if (!node || node.type !== "mail") return;
  }
  const d = node.data;
  const open = { ...d.openMids };
  const full = { ...d.fullMids };
  const pins = { ...d.pinnedBodies };
  if (mode === "pin") {
    if (pinBody === null) delete pins[mid];
    else {
      pins[mid] = pinBody;
      open[mid] = true;
      full[mid] = true;
      if (M.normalizeData("mail", { ...d, pinnedBodies: pins }).pinnedBodies[mid] === undefined) {
        return toast.info("This thread node keeps no more saved message text: unpin another message first");
      }
    }
  } else if (mode === "full") {
    // the full view only: a pin stays until it is unpinned
    if (full[mid]) delete full[mid];
    else {
      full[mid] = true;
      open[mid] = true;
    }
  } else if (open[mid]) {
    delete open[mid];
    delete full[mid];
  } else open[mid] = true;
  d.openMids = open;
  d.fullMids = full;
  d.pinnedBodies = M.normalizeData("mail", { ...d, pinnedBodies: pins }).pinnedBodies;
  if (node.autoFit) ctrl.fitQueue.add(id);
  ctrl.commit();
  if (!open[mid] || pins[mid] !== undefined || (ctrl.mailBodies.has(mid) && !ctrl.mailBodyErrors?.has(mid)) || mid.startsWith("#")) return;
  await loadMailBody(ctrl, mid);
  ctrl.mailEpoch = (ctrl.mailEpoch || 0) + 1;
  for (const n of ctrl.canvas.nodes) if (n.type === "mail" && n.autoFit) ctrl.fitQueue.add(n.id);
  if (ctrl.containerEl?.isConnected || !ctrl.containerEl) ctrl.commit({ save: false, inspector: false });
}

/**
 * Body of message `mid` (cached in `ctrl.mailBodies`), or null for a message without an id or a failed
 * fetch (whose error text shows in the row until the next attempt retries it, never pinned).
 */
async function loadMailBody(ctrl, mid) {
  ctrl.mailBodyErrors = ctrl.mailBodyErrors || new Set();
  if (ctrl.mailBodies.has(mid) && !ctrl.mailBodyErrors.has(mid)) return ctrl.mailBodies.get(mid);
  if (mid.startsWith("#")) return null;
  try {
    const res = await (ctrl.api || api).getMailMessage(mid);
    ctrl.mailBodies.set(mid, String(res?.body || res?.text || ""));
    ctrl.mailBodyErrors.delete(mid);
    return ctrl.mailBodies.get(mid);
  } catch (err) {
    ctrl.mailBodies.set(mid, `(${err.message})`);
    ctrl.mailBodyErrors.add(mid);
    return null;
  }
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
    // a reply wires from its parent's own node, else from its row inside the thread node
    const parentNode = (r.parent && ctrl.mailNodeOf(r.parent)) || thread;
    if (parentNode.id !== n.id && !ctrl.canvas.edges.some((e) => e.from === parentNode.id && e.to === n.id)) {
      const edge = M.createEdge(parentNode === thread && !thread.data.single
        ? { from: thread.id, to: n.id, label: "reply", fromSide: "right", fromField: mailRowKey(mid), toSide: "left" }
        : { from: parentNode.id, to: n.id, label: "reply" });
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

// ------------------------------------------------------------------ commits (Blame rows, commit nodes)

/** Whether two commit hashes name the same commit (one may be an abbreviation of the other). */
export function sameCommit(a, b) {
  const x = String(a || "").toLowerCase();
  const y = String(b || "").toLowerCase();
  if (!x || !y) return false;
  return x.length <= y.length ? x.length >= 7 && y.startsWith(x) : y.length >= 7 && x.startsWith(y);
}

/**
 * The node of `kind` already showing commit `hash` in `repo`: a `commit` details node, or a `diff`
 * node of the whole commit (`path` empty) or of one of its files (`path`). Null when there is none.
 */
export function commitNodeOf(ctrl, kind, hash, repo = "", path = "") {
  // A node without a repository is in the NodeMap's own (`nodeFromSpec()` fills it in)
  const scope = (r) => r || ctrl.repo || "";
  return ctrl.canvas.nodes.find((n) => scope(n.repo) === scope(repo) && (kind === "commit"
    ? n.type === "commit" && sameCommit(n.data.commitHash, hash)
    : n.type === "diff" && n.data.mode === "commit" && sameCommit(n.data.commitHash, hash) && (n.data.path || "") === (path || ""))) || null;
}

/** Spec of the node of `kind` (`commit` | `diff`) for commit `hash`, in the scope of node `from`. */
function commitSpec(from, kind, hash, { subject = "", path = "" } = {}) {
  return kind === "commit"
    ? { type: "commit", version: from.version, repo: from.repo, data: { commitHash: hash, subject } }
    : { type: "diff", version: from.version, repo: from.repo, data: { mode: "commit", commitHash: hash, path } };
}

/** Add an edge `from` → `to` unless one with the same anchor row already joins them. */
function linkOnce(ctrl, from, to, label, fromField = null) {
  if (ctrl.canvas.edges.some((e) => e.from === from.id && e.to === to.id && (e.fromField || null) === (fromField || null))) return null;
  const edge = M.createEdge(fromField
    ? { from: from.id, to: to.id, label, fromSide: "right", fromField, toSide: "left" }
    : { from: from.id, to: to.id, label });
  if (edge) ctrl.canvas.edges.push(edge);
  return edge;
}

/**
 * Connect Blame row `c` of history node `node` to the `commit` details or the commit `diff` node of its
 * commit, adding that node (to the right of the Blame node) when the canvas has none. No commit here:
 * the caller commits once. Returns the target node.
 */
export function linkBlameCommit(ctrl, node, c, kind, at = null) {
  if (!node || !c?.commitHash) return null;
  let target = commitNodeOf(ctrl, kind, c.commitHash, node.repo);
  if (!target) {
    const siblingCount = ctrl.canvas.edges.filter((e) => e.from === node.id).length;
    target = ctrl.nodeFromSpec(commitSpec(node, kind, c.commitHash, { subject: c.subject }), at || { x: node.x + node.w + 100, y: node.y + siblingCount * 110 });
    if (!target) return null;
    ctrl.canvas.nodes.push(target);
    M.reassignGroups(ctrl.canvas.nodes, [target]);
  }
  linkOnce(ctrl, node, target, kind === "commit" ? "blame commit" : "blame diff", commitRowKey(c.commitHash));
  return target;
}

/**
 * From commit node `node`: add (or reuse) the `commit` node of `hash` (a parent, a `Fixes:` target) or
 * the commit `diff` node of `hash` (the whole commit, or one file `path`), connected with `label`.
 */
export function spawnCommitLink(ctrl, node, { type, hash, path = "", label = "" }) {
  if (!node || !hash) return null;
  let target = commitNodeOf(ctrl, type, hash, node.repo, path);
  if (!target) return spawnLinkedNode(ctrl, node, commitSpec(node, type, hash, { path }), label);
  linkOnce(ctrl, node, target, label);
  ctrl.selected = new Set([target.id]);
  ctrl.selectedEdge = null;
  ctrl.commit();
  return target;
}

/** The commit diff node of commit node `id` (added or reused), connected with a `diff` edge. */
export function spawnCommitDiff(ctrl, id) {
  const node = ctrl.byId(id);
  if (!node || node.type !== "commit" || !node.data.commitHash) return null;
  return spawnCommitLink(ctrl, node, { type: "diff", hash: node.data.commitHash, label: "diff" });
}

/** Geometry of the nodes a bulk Blame expand adds, in columns of `perColumn` beside the Blame node. */
export function blameExpandLayout(node, count, kind, { perColumn = 12, gapX = 60, gapY = 24, offsetX = 120 } = {}) {
  const lim = M.SIZE_LIMITS[kind];
  const w = kind === "commit" ? 420 : 520;
  const h = Math.max(lim.minH, kind === "commit" ? 260 : 300);
  return Array.from({ length: count }, (_, i) => ({
    x: node.x + node.w + offsetX + Math.floor(i / perColumn) * (w + gapX),
    y: node.y + (i % perColumn) * (h + gapY),
    w,
    h,
  }));
}

/**
 * "Expand Commits to Nodes" (`kind` `commit`) / "Expand Diffs to Nodes" (`diff`) of Blame node `id`:
 * one node per blamed commit (all of them, no cap), existing ones reused, each wired from its commit's
 * row, the new ones in columns beside the Blame node; one undo step. Adding more than
 * `BLAME_CONFIRM_ABOVE` new nodes asks first (`ctrl.confirmBulkSpawn()`); dismissing changes nothing.
 * Resolves to the created nodes.
 */
export async function expandBlameToNodes(ctrl, id, kind = "commit") {
  const what = kind === "diff" ? "diff" : "commit";
  const node = ctrl.byId(id);
  if (!node || node.type !== "history" || node.data.mode !== "blame") return [];
  let model = ctrl.modelOf(node);
  if (!model) return toast.info("The blame has not loaded yet"), [];
  if (!model.commits?.length) return toast.info("No blamed commits to expand"), [];
  let fresh = model.commits.filter((c) => !commitNodeOf(ctrl, what, c.commitHash, node.repo));
  if (fresh.length > BLAME_CONFIRM_ABOVE) {
    const ok = await ctrl.confirmBulkSpawn({
      title: `Spawn ${fresh.length} ${what} nodes?`,
      summary: `Spawn ${fresh.length} ${what} nodes from this Blame node?`,
      intro: `${fresh.length} new ${what === "diff" ? "commit diff" : "commit details"} nodes will be added beside "${M.displayTitle(node)}" and wired from their rows${model.commits.length > fresh.length ? `; ${model.commits.length - fresh.length} commits already have one and are connected to it` : ""}. One undo step removes them all.`,
      sections: [{
        label: `New ${what} nodes`,
        prefix: "",
        fields: ["subject", "author"],
        columns: ["Commit", "Subject", "Author"],
        rows: fresh.map((c) => ({ name: c.commitHash.slice(0, 12), subject: c.subject, author: c.author })),
      }],
      confirmLabel: `Spawn ${fresh.length} nodes`,
    });
    // The canvas may have changed (or switched) while the dialog was open: plan again from what it holds now
    if (!ok || ctrl.byId(id) !== node) return [];
    model = ctrl.modelOf(node);
    if (!model?.commits?.length) return [];
    fresh = model.commits.filter((c) => !commitNodeOf(ctrl, what, c.commitHash, node.repo));
  }
  const freshSet = new Set(fresh.map((c) => c.commitHash));
  const slots = blameExpandLayout(node, freshSet.size, what);
  const created = [];
  for (const c of model.commits) {
    const existed = !freshSet.has(c.commitHash) || commitNodeOf(ctrl, what, c.commitHash, node.repo);
    const slot = existed ? null : slots[created.length];
    const target = linkBlameCommit(ctrl, node, c, what, slot ? { x: slot.x, y: slot.y } : null);
    if (target && !existed) {
      target.w = slot.w;
      target.h = slot.h;
      created.push(target);
    }
  }
  if (created.length) {
    M.reassignGroups(ctrl.canvas.nodes, created);
    ctrl.selected = new Set(created.map((x) => x.id));
    ctrl.selectedEdge = null;
  }
  ctrl.commit();
  const reused = model.commits.length - created.length;
  toast.success(`Added ${created.length} ${what} node${created.length === 1 ? "" : "s"}${reused ? ` (${reused} already on the canvas, connected)` : ""}`);
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

/** The non-standard type member `fieldKey` of struct node `node` references (`struct_view.js` `typeRefOfField`). */
function fieldTypeRef(ctrl, node, fieldKey) {
  const layout = ctrl.content.get(ctrl.fetchKeyOf(node))?.value;
  const pahole = String(fieldKey).startsWith("po:") ? ctrl.paholeFor?.(node) : null;
  return typeRefOfField(layout, fieldKey, pahole);
}

export function followableTypeFor(ctrl, nodeId, fieldKey) {
  const node = ctrl.byId(nodeId);
  if (!node || node.type !== "struct" || !fieldKey) return null;
  const ref = fieldTypeRef(ctrl, node, fieldKey);
  return ref ? `${ref.kind} ${ref.name}` : null;
}

/** The canvas node showing type `ref` at `version` (struct node for a struct / union, else symbol node). */
function typeNodeOf(ctrl, ref, version, repo = "") {
  return ctrl.canvas.nodes.find((n) => n.version === version && (n.repo || "") === (repo || "") && (ref.kind === "struct" || ref.kind === "union"
    ? n.type === "struct" && n.data.name === ref.name && n.data.kind === ref.kind
    : (n.type === "symbol" && n.data.name === ref.name) || (n.type === "struct" && n.data.name === ref.name))) || null;
}

/**
 * A new node for type `ref`: a struct node for a `struct` / `union` (`GET /webapi/nodemap/{v}/struct/{name}`),
 * a symbol node for an `enum` or typedef (its definition resolves through `getSymbolDetail` and
 * its members expand like any symbol node).
 */
function createTypeNode(from, ref, at) {
  if (ref.kind === "struct" || ref.kind === "union") {
    return M.createNode("struct", { ...at, version: from.version, repo: from.repo, data: { name: ref.name, kind: ref.kind } });
  }
  return M.createNode("symbol", { ...at, version: from.version, repo: from.repo, data: { name: ref.name, kind: ref.kind } });
}

/** Connect member `fieldKey` of `node` to `target` (once per member and target). */
function linkTypeEdge(ctrl, node, target, fieldKey, ref) {
  if (ctrl.canvas.edges.some((e) => e.from === node.id && e.to === target.id && e.fromField === fieldKey)) return;
  const edge = M.createEdge({ from: node.id, to: target.id, fromSide: "right", fromField: fieldKey, toSide: "left", label: ref.edgeLabel });
  if (edge) ctrl.canvas.edges.push(edge);
}

export function followStructType(ctrl, nodeId, fieldKey) {
  const node = ctrl.byId(nodeId);
  if (!node || node.type !== "struct" || !fieldKey) return null;
  const ref = fieldTypeRef(ctrl, node, fieldKey);
  if (!ref) return null;
  let target = typeNodeOf(ctrl, ref, node.version, node.repo);
  if (!target) {
    const siblingCount = ctrl.canvas.edges.filter((e) => e.from === node.id).length;
    target = createTypeNode(node, ref, { x: node.x + node.w + 110, y: node.y + siblingCount * 120 });
    ctrl.canvas.nodes.push(target);
    M.reassignGroups(ctrl.canvas.nodes, [target]);
  }
  linkTypeEdge(ctrl, node, target, fieldKey, ref);
  ctrl.selected = new Set([target.id]);
  ctrl.selectedEdge = null;
  ctrl.commit();
  return target;
}

/** The unique non-standard types of the visible members of struct node `id` (`[{ key, ref }]`). */
export function expandableTypesOf(ctrl, id) {
  const node = ctrl.byId(id);
  if (!node || node.type !== "struct") return [];
  const entry = ctrl.content.get(ctrl.fetchKeyOf(node));
  if (entry?.status !== "ok") return [];
  const so = ctrl.structOpts ? ctrl.structOpts(node) : {};
  return collectExpandableTypes(entry.value, node.data, { evaluator: so.evaluator || null, pahole: so.pahole || null });
}

/**
 * "Expand Types": one node per unique non-standard type the visible members of struct node `id`
 * reference (existing nodes of the type are reused), stacked to the right of the struct and wired from
 * the first referencing member (`fromField`) with a `points to` / `embeds` / `type` edge. One undo step.
 */
export function expandStructTypes(ctrl, id) {
  const node = ctrl.byId(id);
  if (!node || node.type !== "struct") return [];
  if (ctrl.content.get(ctrl.fetchKeyOf(node))?.status !== "ok") return toast.info("The struct has not loaded yet"), [];
  const all = expandableTypesOf(ctrl, id);
  if (!all.length) return toast.info("No non-standard member types to expand"), [];
  const list = all.slice(0, TYPE_EXPAND_MAX);
  const created = [];
  let y = node.y;
  for (const { key, ref } of list) {
    let target = typeNodeOf(ctrl, ref, node.version, node.repo);
    if (!target) {
      target = createTypeNode(node, ref, { x: node.x + node.w + 120, y });
      const lim = M.SIZE_LIMITS[target.type];
      if (target.type === "struct") target.h = Math.round(lim.h * 0.6);
      y += target.h + 24;
      ctrl.canvas.nodes.push(target);
      created.push(target);
    }
    linkTypeEdge(ctrl, node, target, key, ref);
  }
  if (created.length) {
    M.reassignGroups(ctrl.canvas.nodes, created);
    ctrl.selected = new Set(created.map((x) => x.id));
    ctrl.selectedEdge = null;
  }
  if (all.length > list.length) toast.info(`Added the first ${list.length} of ${all.length} types`);
  ctrl.commit();
  return created;
}

export async function expandCallgraph(ctrl, id, direction = "both") {
  const node = ctrl.byId(id);
  if (!node || node.type !== "symbol" || !node.data?.name) return [];
  const client = ctrl.clientFor(node);
  const ver = node.version || ctrl.currentVersion;
  const gen = ctrl._disposeGen;
  const canvas = ctrl.canvas;
  const canvasId = ctrl.canvas?.id;
  let res;
  try {
    res = await client.getCallgraph(ver, node.data.name);
  } catch (err) {
    if (ctrl._disposeGen !== gen || !ctrl.containerEl?.isConnected) return [];
    toast.error(`Callgraph failed: ${err.message}`);
    return [];
  }
  if (ctrl._disposeGen !== gen || !ctrl.containerEl?.isConnected || ctrl.canvas !== canvas || ctrl.canvas?.id !== canvasId || !ctrl.canvas?.nodes?.some((n) => n.id === node.id)) return [];
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
  const gen = ctrl._disposeGen;
  const canvas = ctrl.canvas;
  const canvasId = ctrl.canvas?.id;
  let res;
  try {
    res = await client.getCallpath(ver, { from: node.data?.name || "", to: targetFn, maxDepth, k });
  } catch (err) {
    if (ctrl._disposeGen !== gen || !ctrl.containerEl?.isConnected) return [];
    toast.error(`Call path failed: ${err.message}`);
    return [];
  }
  if (ctrl._disposeGen !== gen || !ctrl.containerEl?.isConnected || ctrl.canvas !== canvas || ctrl.canvas?.id !== canvasId || !ctrl.canvas?.nodes?.some((n) => n.id === node.id)) return [];
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
