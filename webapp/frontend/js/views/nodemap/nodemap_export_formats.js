/**
 * nodemap_export_formats.js - Interactive HTML, Mermaid, and Graphviz DOT exporters for NodeMap.
 *
 * All exports respect visibility layers via `visibleCanvasSlice`, produce self-contained output
 * (offline interactive HTML with pan/zoom and embedded Code Tours, zero external URLs), and escape
 * all user-controlled strings safely for JSON-in-HTML, HTML attributes/text, Mermaid, and DOT.
 */
import { NODE_BADGES, boundsOf, displayTitle, formatLockBadge, paintOrder, visibleCanvasSlice } from "./canvas_model.js";
import { canvasToSvg } from "./nodemap_export.js";
import { esc } from "./struct_view.js";

const MERMAID_DIRS = new Set(["LR", "RL", "TB", "TD", "BT"]);
const DOT_DIRS = new Set(["LR", "RL", "TB", "BT"]);
const MAX_FIELD_LOCKS_SHOWN = 12;

function normalizeVisible(canvas) {
  const base = canvas && typeof canvas === "object" ? canvas : {};
  const safe = {
    ...base,
    nodes: Array.isArray(base.nodes) ? base.nodes : [],
    edges: Array.isArray(base.edges) ? base.edges : [],
    tours: Array.isArray(base.tours) ? base.tours : [],
    settings: base.settings && typeof base.settings === "object" ? base.settings : {},
  };
  return visibleCanvasSlice(safe);
}

function buildSafeIdMap(nodes) {
  const used = new Set();
  const map = new Map();
  const safeIdOf = (rawId) => {
    const key = String(rawId ?? "");
    if (map.has(key)) return map.get(key);
    const cleaned = key.replace(/[^A-Za-z0-9_]/g, "_") || "node";
    let cand = `n_${cleaned}`;
    if (used.has(cand)) {
      let suffix = 2;
      while (used.has(`${cand}_${suffix}`)) suffix += 1;
      cand = `${cand}_${suffix}`;
    }
    used.add(cand);
    map.set(key, cand);
    return cand;
  };
  for (const n of nodes) {
    if (n && n.id !== undefined) safeIdOf(n.id);
  }
  return safeIdOf;
}

function nodeLockLines(node) {
  const lines = [];
  if (Array.isArray(node?.locks)) {
    for (const lk of node.locks) {
      const badge = formatLockBadge(lk);
      if (badge) lines.push(badge);
    }
  }
  const fieldLocks = node?.data?.fieldLocks;
  if (fieldLocks && typeof fieldLocks === "object") {
    const entries = Object.entries(fieldLocks);
    const shown = entries.slice(0, MAX_FIELD_LOCKS_SHOWN);
    for (const [fk, fl] of shown) {
      const badge = formatLockBadge(fl);
      if (!badge) continue;
      const fieldName = String(fk).replace(/^\d+:/, "") || String(fk);
      lines.push(`${fieldName}: ${badge}`);
    }
    if (entries.length > MAX_FIELD_LOCKS_SHOWN) {
      lines.push(`+${entries.length - MAX_FIELD_LOCKS_SHOWN} more field locks`);
    }
  }
  return lines;
}

function formatEdgeText(edge) {
  const label = String(edge?.label || "").trim();
  const lockTag = edge?.lockKind ? `🔒 ${edge.lockKind}` : "";
  if (label && lockTag) return `${label} (${lockTag})`;
  return label || lockTag;
}

function buildGroupHierarchy(nodes) {
  const ordered = paintOrder(nodes);
  const byId = new Map(ordered.map((n) => [n.id, n]));
  const visibleGroupIds = new Set(ordered.filter((n) => n.type === "group").map((n) => n.id));
  const childrenOf = new Map();
  const topLevel = [];
  for (const n of ordered) {
    const pid = n.groupId && n.groupId !== n.id && visibleGroupIds.has(n.groupId) ? n.groupId : null;
    if (pid) {
      if (!childrenOf.has(pid)) childrenOf.set(pid, []);
      childrenOf.get(pid).push(n);
    } else {
      topLevel.push(n);
    }
  }
  return { ordered, byId, childrenOf, topLevel };
}

function escapeMermaid(text) {
  return String(text ?? "")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\r?\n/g, "<br/>")
    .replace(/\r/g, "<br/>");
}

function escapeDot(text) {
  return String(text ?? "")
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/\r?\n/g, "\\n")
    .replace(/\r/g, "\\n");
}

function mermaidArrow(arrow, style) {
  const dashed = style === "dotted" || style === "dashed";
  if (dashed) {
    if (arrow === "both") return "<-.->";
    if (arrow === "none") return "-.-";
    if (arrow === "backward") return "<-.-";
    return "-.->";
  }
  if (arrow === "both") return "<-->";
  if (arrow === "none") return "---";
  if (arrow === "backward") return "<--";
  return "-->";
}

function parseSvgViewBox(svg) {
  const openTag = /<svg\b([^>]*)>/i.exec(String(svg || ""))?.[1] || "";
  const vbMatch = /\bviewBox\s*=\s*["']\s*([^"']+)\s*["']/i.exec(openTag);
  if (vbMatch) {
    const parts = vbMatch[1].trim().split(/[\s,]+/).map(Number);
    if (parts.length === 4 && parts.every(Number.isFinite) && parts[2] > 0 && parts[3] > 0) {
      return { x: parts[0], y: parts[1], w: parts[2], h: parts[3] };
    }
  }
  const wMatch = /\bwidth\s*=\s*["']\s*([\d.]+)/i.exec(openTag);
  const hMatch = /\bheight\s*=\s*["']\s*([\d.]+)/i.exec(openTag);
  const w = Number(wMatch?.[1]);
  const h = Number(hMatch?.[1]);
  if (w > 0 && h > 0) return { x: 0, y: 0, w, h };
  return null;
}

function injectTourHighlightLayer(svg) {
  const str = String(svg || "");
  if (/\bid\s*=\s*["']nm-tour-hl["']/i.test(str)) return str;
  const idx = str.toLowerCase().lastIndexOf("</svg>");
  if (idx < 0) return str;
  return `${str.slice(0, idx)}<g id="nm-tour-hl" aria-hidden="true"></g>${str.slice(idx)}`;
}

/**
 * Serialize a JSON value safely for embedding inside `<script type="application/json">` or inline JS.
 * Escapes `<`, `>`, `&`, U+2028, and U+2029 so `</script>` breakout and JS line-terminator issues
 * cannot occur.
 */
export function escapeJsonForScript(value) {
  const raw = JSON.stringify(value);
  const json = raw === undefined ? "null" : raw;
  return json
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

/**
 * Export the visible canvas as a standalone, offline-capable interactive HTML viewer with vector
 * pan/zoom, keyboard shortcuts, and an embedded Code Tour player when tours with steps exist.
 */
export function canvasToInteractiveHtml(canvas, svgString = "", opts = {}) {
  const vis = normalizeVisible(canvas);
  const title = String(opts.title || "NodeMap Interactive Export");
  const resolve = typeof opts.resolve === "function" ? opts.resolve : (c) => c;
  let svg = typeof svgString === "string" && svgString.trim() ? svgString : "";
  if (!svg) {
    svg = canvasToSvg(vis, resolve, { transparent: Boolean(opts.transparent) });
  }
  svg = injectTourHighlightLayer(svg);

  const nb = boundsOf(vis.nodes);
  const pad = 40;
  const baseViewBox = parseSvgViewBox(svg) || (nb
    ? {
        x: Math.floor(nb.x - pad),
        y: Math.floor(nb.y - pad),
        w: Math.max(1, Math.ceil(nb.w + pad * 2)),
        h: Math.max(1, Math.ceil(nb.h + pad * 2)),
      }
    : { x: 0, y: 0, w: 800, h: 600 });

  const activeTours = (Array.isArray(vis.tours) ? vis.tours : [])
    .filter((t) => t && Array.isArray(t.steps) && t.steps.length > 0)
    .map((t) => ({
      id: String(t.id || ""),
      title: String(t.title || "Walkthrough"),
      steps: t.steps.map((s) => ({
        id: String(s?.id || ""),
        title: String(s?.title || "Step"),
        caption: String(s?.caption || ""),
        nodeIds: Array.isArray(s?.nodeIds) ? s.nodeIds.map(String) : [],
        edgeIds: Array.isArray(s?.edgeIds) ? s.edgeIds.map(String) : [],
        viewport: {
          x: Number(s?.viewport?.x) || 0,
          y: Number(s?.viewport?.y) || 0,
          zoom: Number(s?.viewport?.zoom) > 0 ? Number(s.viewport.zoom) : 1,
        },
      })),
    }));

  const payload = {
    nodes: vis.nodes.map((n) => ({
      id: String(n.id),
      x: Number(n.x) || 0,
      y: Number(n.y) || 0,
      w: Number(n.w) || 0,
      h: Number(n.h) || 0,
      title: displayTitle(n),
    })),
    tours: activeTours,
    baseViewBox,
  };

  const tourSelectHtml = activeTours.length > 1
    ? `<select id="nm-tour-select" class="nm-exp-select" aria-label="Select tour">${
        activeTours.map((t, i) => `<option value="${esc(t.id)}" data-index="${i}">${esc(t.title)} (${t.steps.length})</option>`).join("")
      }</select>`
    : "";

  const tourBarHtml = activeTours.length > 0
    ? `<div id="nm-tour-bar" class="nm-exp-tour" role="region" aria-label="Code Tour">
  <div class="nm-exp-tour-head">
    ${tourSelectHtml}
    <span id="nm-tour-counter" class="nm-exp-counter">1 / ${activeTours[0].steps.length}</span>
    <span id="nm-tour-title" class="nm-exp-tour-title"></span>
    <div class="nm-exp-tour-btns">
      <button type="button" id="nm-tour-prev" class="nm-exp-btn" title="Previous step (Left Arrow)">&#8592; Prev</button>
      <button type="button" id="nm-tour-next" class="nm-exp-btn" title="Next step (Right Arrow)">Next &#8594;</button>
    </div>
  </div>
  <div id="nm-tour-caption" class="nm-exp-tour-caption" hidden></div>
</div>`
    : "";

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)}</title>
<style>
:root {
  --bg-canvas: var(--_bg-canvas, #0a0d12);
  --bg-primary: var(--_bg-primary, #0d1117);
  --bg-secondary: var(--_bg-secondary, #161b22);
  --bg-tertiary: var(--_bg-tertiary, #21262d);
  --bg-hover: var(--_bg-hover, #30363d);
  --text-primary: var(--_text-primary, #e6edf3);
  --text-secondary: var(--_text-secondary, #9da7b3);
  --text-muted: var(--_text-muted, #7d8590);
  --border-color: var(--_border-color, #30363d);
  --border-muted: var(--_border-muted, #21262d);
  --border-focus: var(--_border-focus, #58a6ff);
  --accent-blue: var(--_accent-blue, #58a6ff);
  --accent-cyan: var(--_accent-cyan, #39c5bb);
  --accent-green: var(--_accent-green, #3fb950);
  --accent-purple: var(--_accent-purple, #bc8cff);
  --accent-orange: var(--_accent-orange, #f0883e);
  --accent-red: var(--_accent-red, #f85149);
  --accent-yellow: var(--_accent-yellow, #d29922);
  --accent-pink: var(--_accent-pink, #f778ba);
  --syn-type: var(--_syn-type, #79c0ff);
  --syn-keyword: var(--_syn-keyword, #ff7b72);
  --syn-preproc: var(--_syn-preproc, #d2a8ff);
  --syn-number: var(--_syn-number, #79c0ff);
  --syn-string: var(--_syn-string, #a5d6ff);
  --syn-comment: var(--_syn-comment, #8b949e);
  --syn-punct: var(--_syn-punct, #8b949e);
  --syn-function: var(--_syn-function, #d2a8ff);
  --syn-identifier: var(--_syn-identifier, #c9d1d9);
  --syn-variable: var(--_syn-variable, #ffa657);
  --syn-attribute: var(--_syn-attribute, #7ee787);
  --diff-add-fg: var(--_diff-add-fg, #7ee787);
  --diff-del-fg: var(--_diff-del-fg, #ff7b72);
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: var(--bg-canvas, #0a0d12);
  color: var(--text-primary, #e6edf3);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: var(--fs-12, 12px);
}
.nm-exp-root {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
}
#nm-stage {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  cursor: grab;
  touch-action: none;
  user-select: none;
}
#nm-stage.is-panning { cursor: grabbing; }
#nm-stage > svg {
  width: 100%;
  height: 100%;
  display: block;
}
.nm-exp-toolbar {
  position: fixed;
  top: 12px;
  left: 12px;
  z-index: 20;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  max-width: calc(100vw - 24px);
  padding: 5px 8px;
  background: var(--bg-secondary, #161b22);
  border: 1px solid var(--border-color, #30363d);
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
}
.nm-exp-title {
  font-size: var(--fs-12, 12px);
  font-weight: 600;
  color: var(--text-primary, #e6edf3);
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--bg-tertiary, #21262d);
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.nm-exp-sep {
  width: 1px;
  height: 18px;
  background: var(--border-color, #30363d);
  margin: 0 2px;
}
.nm-exp-btn {
  appearance: none;
  border: 1px solid var(--border-color, #30363d);
  background: var(--bg-tertiary, #21262d);
  color: var(--text-primary, #e6edf3);
  border-radius: 5px;
  padding: 4px 9px;
  font-size: var(--fs-11, 11px);
  line-height: 1.25;
  cursor: pointer;
}
.nm-exp-btn:hover:not(:disabled) {
  background: var(--bg-hover, #30363d);
  border-color: var(--accent-blue, #58a6ff);
}
.nm-exp-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
.nm-exp-btn:focus-visible, .nm-exp-select:focus-visible {
  outline: 2px solid var(--border-focus, #58a6ff);
  outline-offset: 1px;
}
.nm-exp-readout {
  min-width: 44px;
  text-align: center;
  font-size: var(--fs-11, 11px);
  color: var(--text-secondary, #9da7b3);
  font-variant-numeric: tabular-nums;
}
.nm-exp-hint {
  font-size: var(--fs-11, 11px);
  color: var(--text-muted, #7d8590);
  padding-left: 4px;
  white-space: nowrap;
}
.nm-exp-tour {
  position: fixed;
  left: 50%;
  bottom: 14px;
  transform: translateX(-50%);
  z-index: 20;
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: min(360px, calc(100vw - 24px));
  max-width: min(680px, calc(100vw - 24px));
  padding: 8px 12px;
  background: var(--bg-secondary, #161b22);
  border: 1px solid var(--accent-blue, #58a6ff);
  border-radius: 8px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.5);
}
.nm-exp-tour-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.nm-exp-select {
  background: var(--bg-primary, #0d1117);
  color: var(--text-primary, #e6edf3);
  border: 1px solid var(--border-color, #30363d);
  border-radius: 5px;
  padding: 3px 6px;
  font-size: var(--fs-11, 11px);
  max-width: 180px;
}
.nm-exp-counter {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: var(--fs-10, 10px);
  color: var(--text-muted, #7d8590);
  flex-shrink: 0;
}
.nm-exp-tour-title {
  font-size: var(--fs-13, 13px);
  font-weight: 600;
  color: var(--text-primary, #e6edf3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.nm-exp-tour-btns {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
  flex-shrink: 0;
}
.nm-exp-tour-caption {
  font-size: var(--fs-11, 11px);
  color: var(--text-secondary, #9da7b3);
  line-height: 1.45;
  white-space: pre-wrap;
}
.nm-exp-tour-caption[hidden] { display: none; }
.nm-tour-hl-rect {
  fill: var(--accent-yellow, #d29922);
  fill-opacity: 0.12;
  stroke: var(--accent-yellow, #d29922);
  stroke-width: 3;
  vector-effect: non-scaling-stroke;
}
</style>
</head>
<body>
<div class="nm-exp-root">
  <div class="nm-exp-toolbar" role="toolbar" aria-label="Canvas navigation">
    <span class="nm-exp-title" title="${esc(title)}">${esc(title)}</span>
    <span class="nm-exp-sep" aria-hidden="true"></span>
    <button type="button" id="nm-zoom-in" class="nm-exp-btn" title="Zoom In (+)" aria-label="Zoom In">+</button>
    <span id="nm-zoom-readout" class="nm-exp-readout">100%</span>
    <button type="button" id="nm-zoom-out" class="nm-exp-btn" title="Zoom Out (-)" aria-label="Zoom Out">&#8722;</button>
    <button type="button" id="nm-zoom-fit" class="nm-exp-btn" title="Fit Canvas (F / 1)">Fit</button>
    <button type="button" id="nm-zoom-reset" class="nm-exp-btn" title="Reset 100% (0)">100%</button>
    <span class="nm-exp-sep" aria-hidden="true"></span>
    <span class="nm-exp-hint">Drag to pan &#183; Wheel to zoom &#183; &#8592;/&#8594; Tour</span>
  </div>
  <div id="nm-stage">${svg}</div>
  ${tourBarHtml}
</div>
<script id="nm-export-data" type="application/json">${escapeJsonForScript(payload)}</script>
<script>
(function () {
  var SVG_NS = "http://www.w3.org/2000/svg";
  var MIN_ZOOM = 0.05;
  var MAX_ZOOM = 6;
  var dataEl = document.getElementById("nm-export-data");
  var data = { nodes: [], tours: [], baseViewBox: { x: 0, y: 0, w: 800, h: 600 } };
  try {
    if (dataEl && dataEl.textContent) data = JSON.parse(dataEl.textContent);
  } catch (err) {
    data = { nodes: [], tours: [], baseViewBox: { x: 0, y: 0, w: 800, h: 600 } };
  }
  var base = data.baseViewBox || { x: 0, y: 0, w: 800, h: 600 };
  var stage = document.getElementById("nm-stage");
  var svg = stage ? stage.querySelector("svg") : null;
  if (!stage || !svg) return;

  svg.style.width = "100%";
  svg.style.height = "100%";
  svg.style.display = "block";
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  var hlGroup = svg.querySelector("#nm-tour-hl");
  if (!hlGroup) {
    hlGroup = document.createElementNS(SVG_NS, "g");
    hlGroup.setAttribute("id", "nm-tour-hl");
    hlGroup.setAttribute("aria-hidden", "true");
    svg.appendChild(hlGroup);
  }

  var readoutEl = document.getElementById("nm-zoom-readout");
  var zoomInBtn = document.getElementById("nm-zoom-in");
  var zoomOutBtn = document.getElementById("nm-zoom-out");
  var fitBtn = document.getElementById("nm-zoom-fit");
  var resetBtn = document.getElementById("nm-zoom-reset");
  var tourSelect = document.getElementById("nm-tour-select");
  var tourCounter = document.getElementById("nm-tour-counter");
  var tourTitle = document.getElementById("nm-tour-title");
  var tourCaption = document.getElementById("nm-tour-caption");
  var tourPrevBtn = document.getElementById("nm-tour-prev");
  var tourNextBtn = document.getElementById("nm-tour-next");

  var nodeById = Object.create(null);
  for (var i = 0; i < (data.nodes || []).length; i++) {
    var nd = data.nodes[i];
    if (nd && nd.id) nodeById[nd.id] = nd;
  }

  var view = { cx: base.x + base.w / 2, cy: base.y + base.h / 2, zoom: 1 };
  var animId = 0;

  function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, v));
  }

  function stageSize() {
    var r = stage.getBoundingClientRect ? stage.getBoundingClientRect() : null;
    if (r && r.width > 10 && r.height > 10) return { w: r.width, h: r.height };
    return { w: Math.max(1, base.w), h: Math.max(1, base.h) };
  }

  function applyView() {
    var s = stageSize();
    var vbW = s.w / view.zoom;
    var vbH = s.h / view.zoom;
    var vbX = view.cx - vbW / 2;
    var vbY = view.cy - vbH / 2;
    svg.setAttribute("viewBox", vbX + " " + vbY + " " + vbW + " " + vbH);
    if (readoutEl) readoutEl.textContent = Math.round(view.zoom * 100) + "%";
  }

  function stopAnim() {
    if (animId && typeof cancelAnimationFrame === "function") {
      cancelAnimationFrame(animId);
    }
    animId = 0;
  }

  function moveTo(target, animate) {
    stopAnim();
    var reduced = typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!animate || reduced || typeof requestAnimationFrame !== "function") {
      view.cx = target.cx;
      view.cy = target.cy;
      view.zoom = clamp(target.zoom, MIN_ZOOM, MAX_ZOOM);
      applyView();
      return;
    }
    var fromCx = view.cx;
    var fromCy = view.cy;
    var fromLogZ = Math.log(Math.max(MIN_ZOOM, view.zoom));
    var toLogZ = Math.log(clamp(target.zoom, MIN_ZOOM, MAX_ZOOM));
    var start = performance.now();
    var dur = 240;
    function frame(now) {
      var t = clamp((now - start) / dur, 0, 1);
      var ease = 1 - Math.pow(1 - t, 3);
      view.cx = fromCx + (target.cx - fromCx) * ease;
      view.cy = fromCy + (target.cy - fromCy) * ease;
      view.zoom = Math.exp(fromLogZ + (toLogZ - fromLogZ) * ease);
      applyView();
      if (t < 1) {
        animId = requestAnimationFrame(frame);
      } else {
        animId = 0;
      }
    }
    animId = requestAnimationFrame(frame);
  }

  function fitTarget(bounds, maxZoom) {
    var b = bounds || base;
    var s = stageSize();
    var pad = Math.min(60, Math.min(s.w, s.h) * 0.1);
    var availW = Math.max(20, s.w - pad * 2);
    var availH = Math.max(20, s.h - pad * 2);
    var z = Math.min(availW / Math.max(b.w, 1), availH / Math.max(b.h, 1));
    return {
      cx: b.x + b.w / 2,
      cy: b.y + b.h / 2,
      zoom: clamp(z, MIN_ZOOM, maxZoom || 2),
    };
  }

  function fitView(animate) {
    moveTo(fitTarget(base, 2), animate);
  }

  function resetView(animate) {
    moveTo({ cx: base.x + base.w / 2, cy: base.y + base.h / 2, zoom: 1 }, animate);
  }

  function zoomBy(factor, clientX, clientY) {
    stopAnim();
    var nextZoom = clamp(view.zoom * factor, MIN_ZOOM, MAX_ZOOM);
    if (clientX !== undefined && clientY !== undefined && stage.getBoundingClientRect) {
      var rect = stage.getBoundingClientRect();
      var sx = clientX - rect.left - rect.width / 2;
      var sy = clientY - rect.top - rect.height / 2;
      var wx = view.cx + sx / view.zoom;
      var wy = view.cy + sy / view.zoom;
      view.cx = wx - sx / nextZoom;
      view.cy = wy - sy / nextZoom;
    }
    view.zoom = nextZoom;
    applyView();
  }

  var drag = null;
  stage.addEventListener("pointerdown", function (e) {
    if (e.button !== 0) return;
    stopAnim();
    drag = { x: e.clientX, y: e.clientY, cx: view.cx, cy: view.cy, pid: e.pointerId };
    stage.classList.add("is-panning");
    if (stage.setPointerCapture) {
      try { stage.setPointerCapture(e.pointerId); } catch (err) {}
    }
  });
  stage.addEventListener("pointermove", function (e) {
    if (!drag) return;
    view.cx = drag.cx - (e.clientX - drag.x) / view.zoom;
    view.cy = drag.cy - (e.clientY - drag.y) / view.zoom;
    applyView();
  });
  function endDrag(e) {
    if (!drag) return;
    if (stage.releasePointerCapture && drag.pid !== undefined) {
      try { stage.releasePointerCapture(drag.pid); } catch (err) {}
    }
    drag = null;
    stage.classList.remove("is-panning");
  }
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);

  stage.addEventListener("wheel", function (e) {
    e.preventDefault();
    var factor = clamp(Math.pow(1.0015, -e.deltaY), 0.5, 2);
    zoomBy(factor, e.clientX, e.clientY);
  }, { passive: false });

  if (zoomInBtn) zoomInBtn.addEventListener("click", function () { zoomBy(1.25); });
  if (zoomOutBtn) zoomOutBtn.addEventListener("click", function () { zoomBy(1 / 1.25); });
  if (fitBtn) fitBtn.addEventListener("click", function () { fitView(true); });
  if (resetBtn) resetBtn.addEventListener("click", function () { resetView(true); });

  var tours = Array.isArray(data.tours) ? data.tours : [];
  var tourIdx = 0;
  var stepIdx = 0;

  function renderTourHighlights(nodeIds) {
    while (hlGroup.firstChild) hlGroup.removeChild(hlGroup.firstChild);
    for (var j = 0; j < nodeIds.length; j++) {
      var n = nodeById[nodeIds[j]];
      if (!n) continue;
      var rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("class", "nm-tour-hl-rect");
      rect.setAttribute("x", String(n.x - 4));
      rect.setAttribute("y", String(n.y - 4));
      rect.setAttribute("width", String(Math.max(1, n.w + 8)));
      rect.setAttribute("height", String(Math.max(1, n.h + 8)));
      rect.setAttribute("rx", "8");
      hlGroup.appendChild(rect);
    }
  }

  function boundsForIds(ids) {
    var x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    var count = 0;
    for (var j = 0; j < ids.length; j++) {
      var n = nodeById[ids[j]];
      if (!n) continue;
      x1 = Math.min(x1, n.x);
      y1 = Math.min(y1, n.y);
      x2 = Math.max(x2, n.x + n.w);
      y2 = Math.max(y2, n.y + n.h);
      count += 1;
    }
    if (!count) return null;
    return { x: x1, y: y1, w: Math.max(1, x2 - x1), h: Math.max(1, y2 - y1) };
  }

  function applyTourStep(animate) {
    var tour = tours[tourIdx];
    if (!tour || !tour.steps || !tour.steps.length) return;
    stepIdx = clamp(stepIdx, 0, tour.steps.length - 1);
    var step = tour.steps[stepIdx];
    var total = tour.steps.length;
    if (tourCounter) tourCounter.textContent = (stepIdx + 1) + " / " + total;
    if (tourTitle) tourTitle.textContent = step.title || tour.title || "";
    if (tourCaption) {
      var cap = step.caption || "";
      tourCaption.textContent = cap;
      tourCaption.hidden = !cap;
    }
    if (tourPrevBtn) tourPrevBtn.disabled = stepIdx <= 0;
    if (tourNextBtn) tourNextBtn.disabled = stepIdx >= total - 1;

    var ids = Array.isArray(step.nodeIds) ? step.nodeIds : [];
    renderTourHighlights(ids);
    var nb = boundsForIds(ids);
    if (nb) {
      moveTo(fitTarget(nb, 1.5), animate);
      return;
    }
    var vp = step.viewport;
    if (vp && (vp.x !== 0 || vp.y !== 0 || (vp.zoom && vp.zoom !== 1))) {
      var z = clamp(Number(vp.zoom) || 1, MIN_ZOOM, MAX_ZOOM);
      var s = stageSize();
      moveTo({ cx: (-vp.x + s.w / 2) / z, cy: (-vp.y + s.h / 2) / z, zoom: z }, animate);
      return;
    }
    fitView(animate);
  }

  if (tourPrevBtn) {
    tourPrevBtn.addEventListener("click", function () {
      if (stepIdx > 0) { stepIdx -= 1; applyTourStep(true); }
    });
  }
  if (tourNextBtn) {
    tourNextBtn.addEventListener("click", function () {
      var tour = tours[tourIdx];
      if (tour && stepIdx < tour.steps.length - 1) { stepIdx += 1; applyTourStep(true); }
    });
  }
  if (tourSelect) {
    tourSelect.addEventListener("change", function () {
      var idx = tourSelect.selectedIndex;
      if (idx >= 0 && idx < tours.length) {
        tourIdx = idx;
        stepIdx = 0;
        applyTourStep(true);
      }
    });
  }

  document.addEventListener("keydown", function (e) {
    if (!e || e.ctrlKey || e.metaKey || e.altKey) return;
    var tag = String(e.target && e.target.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target && e.target.isContentEditable)) return;
    if (e.key === "+" || e.key === "=") {
      e.preventDefault();
      zoomBy(1.25);
    } else if (e.key === "-" || e.key === "_") {
      e.preventDefault();
      zoomBy(1 / 1.25);
    } else if (e.key === "0") {
      e.preventDefault();
      resetView(true);
    } else if (e.key === "1" || e.key === "f" || e.key === "F") {
      e.preventDefault();
      fitView(true);
    } else if (e.key === "ArrowLeft" && tours.length) {
      e.preventDefault();
      if (stepIdx > 0) { stepIdx -= 1; applyTourStep(true); }
    } else if (e.key === "ArrowRight" && tours.length) {
      e.preventDefault();
      var t = tours[tourIdx];
      if (t && stepIdx < t.steps.length - 1) { stepIdx += 1; applyTourStep(true); }
    }
  });

  window.addEventListener("resize", applyView);
  if (tours.length > 0) {
    applyTourStep(false);
  } else {
    fitView(false);
  }
})();
</script>
</body>
</html>`;
}

/**
 * Export the visible canvas as a Mermaid `flowchart` diagram, preserving nested groups,
 * lock badges, struct field locks, and edge styles/arrows.
 */
export function canvasToMermaid(canvas, opts = {}) {
  const vis = normalizeVisible(canvas);
  const dir = MERMAID_DIRS.has(opts.direction) ? opts.direction : "LR";
  const { ordered, byId, childrenOf, topLevel } = buildGroupHierarchy(vis.nodes);
  const safeIdOf = buildSafeIdMap(ordered);
  const lines = [`flowchart ${dir}`];
  const visitedGroups = new Set();

  const emitNode = (n, indent) => {
    const sid = safeIdOf(n.id);
    if (n.type === "group") {
      if (visitedGroups.has(n.id)) return;
      visitedGroups.add(n.id);
      const grpLocks = nodeLockLines(n);
      const grpTitle = grpLocks.length ? `${displayTitle(n)} (${grpLocks.join(", ")})` : displayTitle(n);
      lines.push(`${indent}subgraph ${sid} ["${escapeMermaid(grpTitle)}"]`);
      const kids = childrenOf.get(n.id) || [];
      if (kids.length) {
        for (const child of kids) {
          emitNode(child, `${indent}  `);
        }
      } else {
        lines.push(`${indent}  ${sid}_portal["${escapeMermaid(`[portal] ${displayTitle(n)}`)}"]`);
      }
      lines.push(`${indent}end`);
      return;
    }
    const badge = NODE_BADGES[n.type] || n.type;
    const parts = [`[${badge}] ${displayTitle(n)}`, ...nodeLockLines(n)];
    lines.push(`${indent}${sid}["${escapeMermaid(parts.join("\n"))}"]`);
  };

  for (const n of topLevel) {
    emitNode(n, "  ");
  }

  for (const e of vis.edges) {
    if (!e || !byId.has(e.from) || !byId.has(e.to)) continue;
    const fromId = safeIdOf(e.from);
    const toId = safeIdOf(e.to);
    const arrow = mermaidArrow(e.arrow, e.style);
    const edgeText = formatEdgeText(e);
    if (edgeText) {
      lines.push(`  ${fromId} ${arrow}|"${escapeMermaid(edgeText)}"| ${toId}`);
    } else {
      lines.push(`  ${fromId} ${arrow} ${toId}`);
    }
  }

  return lines.join("\n");
}

/**
 * Export the visible canvas as a Graphviz DOT `digraph`, preserving nested groups as
 * `cluster_*` subgraphs, lock badges, struct field locks, and edge styles/directions.
 */
export function canvasToDot(canvas, opts = {}) {
  const vis = normalizeVisible(canvas);
  const rawDir = opts.direction === "TD" ? "TB" : opts.direction;
  const dir = DOT_DIRS.has(rawDir) ? rawDir : "LR";
  const { ordered, byId, childrenOf, topLevel } = buildGroupHierarchy(vis.nodes);
  const safeIdOf = buildSafeIdMap(ordered);
  const lines = [
    "digraph NodeMap {",
    `  rankdir=${dir};`,
    "  compound=true;",
    '  graph [fontname="sans-serif"];',
    '  node [shape=box, style="rounded,filled", fontname="monospace"];',
    '  edge [fontname="sans-serif"];',
  ];
  const visitedGroups = new Set();

  const clusterAnchorOf = (groupNode) => {
    const queue = [...(childrenOf.get(groupNode.id) || [])];
    const seen = new Set([groupNode.id]);
    while (queue.length) {
      const cur = queue.shift();
      if (!cur || seen.has(cur.id)) continue;
      seen.add(cur.id);
      const kids = childrenOf.get(cur.id) || [];
      if (cur.type !== "group" || kids.length === 0) return safeIdOf(cur.id);
      queue.push(...kids);
    }
    return null;
  };

  const emitNode = (n, indent) => {
    const sid = safeIdOf(n.id);
    if (n.type === "group") {
      if (visitedGroups.has(n.id)) return;
      visitedGroups.add(n.id);
      const grpLocks = nodeLockLines(n);
      const grpLabel = grpLocks.length ? `${displayTitle(n)}\n${grpLocks.join("\n")}` : displayTitle(n);
      const kids = childrenOf.get(n.id) || [];
      lines.push(`${indent}subgraph cluster_${sid} {`);
      lines.push(`${indent}  label="${escapeDot(grpLabel)}";`);
      lines.push(`${indent}  style="rounded,dashed";`);
      if (kids.length) {
        for (const child of kids) {
          emitNode(child, `${indent}  `);
        }
      } else {
        const badge = String(NODE_BADGES[n.type] || n.type).toUpperCase();
        const parts = [`[${badge}] ${displayTitle(n)}`, ...grpLocks];
        lines.push(`${indent}  ${sid} [label="${escapeDot(parts.join("\n"))}"];`);
      }
      lines.push(`${indent}}`);
      return;
    }
    const badge = String(NODE_BADGES[n.type] || n.type).toUpperCase();
    const parts = [`[${badge}] ${displayTitle(n)}`, ...nodeLockLines(n)];
    lines.push(`${indent}${sid} [label="${escapeDot(parts.join("\n"))}"];`);
  };

  for (const n of topLevel) {
    emitNode(n, "  ");
  }

  for (const e of vis.edges) {
    if (!e || !byId.has(e.from) || !byId.has(e.to)) continue;
    const fromNode = byId.get(e.from);
    const toNode = byId.get(e.to);
    const fromSid = safeIdOf(e.from);
    const toSid = safeIdOf(e.to);
    const fromAnchor = fromNode?.type === "group" ? clusterAnchorOf(fromNode) : null;
    const toAnchor = toNode?.type === "group" ? clusterAnchorOf(toNode) : null;
    const fromEndpoint = fromAnchor || fromSid;
    const toEndpoint = toAnchor || toSid;
    const edgeText = formatEdgeText(e);
    const dotStyle = e.style === "dashed" ? "dashed" : e.style === "dotted" ? "dotted" : "solid";
    const dotDir = e.arrow === "both" ? "both" : e.arrow === "backward" ? "back" : e.arrow === "none" ? "none" : "forward";
    const attrs = [];
    if (edgeText) attrs.push(`label="${escapeDot(edgeText)}"`);
    attrs.push(`style="${dotStyle}"`);
    attrs.push(`dir="${dotDir}"`);
    if (fromAnchor) attrs.push(`ltail="cluster_${fromSid}"`);
    if (toAnchor) attrs.push(`lhead="cluster_${toSid}"`);
    lines.push(`  ${fromEndpoint} -> ${toEndpoint} [${attrs.join(", ")}];`);
  }

  lines.push("}");
  return lines.join("\n");
}
