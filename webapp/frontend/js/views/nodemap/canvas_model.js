/**
 * canvas_model.js - NodeMap canvas document: schema, normalization, geometry, groups, selection and layout.
 *
 * DOM-free and dependency-free (unit tested in `webapp/frontend/tests/nodemap_canvas.test.mjs`). The
 * controller owns one canvas document per NodeMap tab; every mutation goes through these helpers so the
 * persisted JSON (localStorage and Export JSON) always has the shape `normalizeCanvas()` produces.
 *
 * Canvas JSON (`schema: "kip-nodemap-canvas"`, `version: 1`):
 *   { schema, version, nodes: [Node], edges: [Edge], viewport: {x, y, zoom}, settings: {snap, grid} }
 *   Node  { id, type, x, y, w, h, title, customTitle, color, groupId, version, repo, data }
 *   Edge  { id, from, to, fromSide, toSide, fromField, toField, label, arrow, style, curve, color }
 */

export const CANVAS_SCHEMA = "kip-nodemap-canvas";
export const CANVAS_VERSION = 1;
export const STORAGE_PREFIX = "kip-nodemap-canvas:";

export const NODE_TYPES = ["struct", "symbol", "source", "man", "tab", "note", "group"];
export const SIDES = ["auto", "top", "right", "bottom", "left"];
export const ARROWS = ["none", "forward", "backward", "both"];
export const LINE_STYLES = ["solid", "dashed", "dotted"];
export const CURVES = ["bezier", "straight", "orthogonal"];
export const SOURCE_MODES = ["file", "range", "symbol"];
export const SYMBOL_KINDS = ["", "struct", "union", "function", "macro", "enum"];
export const ALIGN_MODES = ["left", "center", "right", "top", "middle", "bottom"];

/** Preset accents: ids map to theme tokens, so every theme (dark, light, high contrast) recolors them. */
export const ACCENTS = [
  { id: "", label: "Default" },
  { id: "blue", label: "Blue" },
  { id: "cyan", label: "Cyan" },
  { id: "green", label: "Green" },
  { id: "purple", label: "Purple" },
  { id: "orange", label: "Orange" },
  { id: "red", label: "Red" },
  { id: "yellow", label: "Yellow" },
  { id: "pink", label: "Pink" },
];
const ACCENT_IDS = new Set(ACCENTS.map((a) => a.id));

/** CSS color of an accent id (`""` = the type's default token). */
export function accentVar(id, fallback = "var(--border-color)") {
  return id && ACCENT_IDS.has(id) ? `var(--accent-${id})` : fallback;
}

/** Default and limit geometry per node type (canvas units, zoom-controlled). */
export const SIZE_LIMITS = {
  struct: { w: 300, h: 320, minW: 180, minH: 90, maxW: 1400, maxH: 2400 },
  symbol: { w: 220, h: 120, minW: 140, minH: 60, maxW: 900, maxH: 1600 },
  source: { w: 560, h: 360, minW: 220, minH: 120, maxW: 2400, maxH: 2400 },
  man: { w: 560, h: 420, minW: 260, minH: 160, maxW: 2400, maxH: 2400 },
  tab: { w: 720, h: 480, minW: 280, minH: 200, maxW: 3000, maxH: 2400 },
  note: { w: 240, h: 140, minW: 120, minH: 60, maxW: 1600, maxH: 1600 },
  group: { w: 520, h: 360, minW: 160, minH: 100, maxW: 6000, maxH: 6000 },
};

export const GRID_DEFAULT = 20;
export const ZOOM_MIN = 0.15;
export const ZOOM_MAX = 3;

const ID_RE = /^[\w-]{1,64}$/;
let idCounter = 0;

/** A fresh id with a type prefix (`n-…` nodes, `e-…` edges). */
export function newId(prefix = "n") {
  idCounter = (idCounter + 1) % 1e6;
  return `${prefix}-${Date.now().toString(36)}${idCounter.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const num = (v, d) => (typeof v === "number" && Number.isFinite(v) ? v : d);
const str = (v, d = "", max = 4096) => (typeof v === "string" ? v.slice(0, max) : d);
const pick = (v, list, d) => (list.includes(v) ? v : d);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const posInt = (v) => (Number.isInteger(v) && v > 0 ? v : null);
const boolMap = (m) => {
  const out = {};
  if (!isObj(m)) return out;
  for (const [k, v] of Object.entries(m)) if (typeof v === "boolean" && k.length <= 256) out[k] = v;
  return out;
};

/** Type-specific payload, normalized. */
export function normalizeData(type, raw) {
  const d = isObj(raw) ? raw : {};
  switch (type) {
    case "struct":
      return {
        name: str(d.name, "", 256),
        kind: pick(d.kind, ["struct", "union"], "struct"),
        path: str(d.path, "", 1024),
        // File tried when the revision's index has no definition (unparsed revision): where it was sent from
        hint: str(d.hint, "", 1024),
        line: posInt(d.line),
        showTypes: typeof d.showTypes === "boolean" ? d.showTypes : true,
        showGuards: typeof d.showGuards === "boolean" ? d.showGuards : true,
        typeOverrides: boolMap(d.typeOverrides),
        collapsed: boolMap(d.collapsed),
      };
    case "symbol":
      return {
        name: str(d.name, "", 256),
        kind: str(d.kind, "symbol", 32),
        astId: Number.isInteger(d.astId) ? d.astId : null,
        expanded: Boolean(d.expanded),
        constituents: Array.isArray(d.constituents)
          ? d.constituents.slice(0, 2000).map((c) => ({ name: str(isObj(c) ? c.name : c, "", 512) })).filter((c) => c.name)
          : [],
      };
    case "source": {
      const start = posInt(d.start);
      const end = posInt(d.end);
      return {
        path: str(d.path, "", 1024),
        mode: pick(d.mode, SOURCE_MODES, "file"),
        start,
        end: start && end && end < start ? start : end,
        symbol: str(d.symbol, "", 256),
        symbolKind: pick(d.symbolKind, SYMBOL_KINDS, ""),
        context: clamp(Number.isInteger(d.context) ? d.context : 0, 0, 200),
      };
    }
    case "man":
      return {
        repo: str(d.repo, "", 128),
        version: str(d.version, "", 256),
        path: str(d.path, "", 1024),
        name: str(d.name, "", 128),
        section: str(d.section, "", 16),
        mode: pick(d.mode, ["web", "term", ""], ""),
      };
    case "tab":
      return {
        tabType: str(d.tabType, "", 32),
        hash: str(d.hash, "", 4096).replace(/^#/, ""),
      };
    case "note":
      return { text: str(d.text, "", 20000), markdown: d.markdown !== false };
    default:
      return {};
  }
}

/** Default title of a node from its payload (shown unless `customTitle` is set). */
export function defaultTitle(type, data) {
  switch (type) {
    case "struct":
      return data.name ? `${data.kind} ${data.name}` : "struct";
    case "symbol":
      return data.name ? (data.kind === "func" && !data.name.endsWith("()") ? `${data.name}()` : data.name) : "Symbol";
    case "source": {
      const file = data.path ? data.path.split("/").pop() : "Source";
      if (data.mode === "symbol" && data.symbol) return `${file} · ${data.symbol}`;
      if (data.mode === "range" && data.start) return `${file}:${data.start}${data.end && data.end !== data.start ? `-${data.end}` : ""}`;
      return file;
    }
    case "man":
      return data.name ? `${data.name}(${data.section || "?"})` : (data.path ? data.path.split("/").pop() : "Man page");
    case "tab":
      return data.tabType ? `${data.tabType} tab` : "KernelInfo tab";
    case "note":
      return "Note";
    case "group":
      return "Group";
    default:
      return "Node";
  }
}

/** Shown title: the custom title when set, else the stored/default one. */
export function displayTitle(node) {
  return node.customTitle || node.title || defaultTitle(node.type, node.data || {});
}

/** One node, normalized (null when it cannot be repaired: unknown type). */
export function normalizeNode(raw) {
  if (!isObj(raw) || !NODE_TYPES.includes(raw.type)) return null;
  const type = raw.type;
  const lim = SIZE_LIMITS[type];
  const data = normalizeData(type, raw.data);
  return {
    id: typeof raw.id === "string" && ID_RE.test(raw.id) ? raw.id : newId("n"),
    type,
    x: Math.round(num(raw.x, 80)),
    y: Math.round(num(raw.y, 80)),
    w: Math.round(clamp(num(raw.w, lim.w), lim.minW, lim.maxW)),
    h: Math.round(clamp(num(raw.h, lim.h), lim.minH, lim.maxH)),
    title: str(raw.title, "", 512) || defaultTitle(type, data),
    customTitle: str(raw.customTitle, "", 512),
    color: ACCENT_IDS.has(raw.color) ? raw.color : "",
    groupId: typeof raw.groupId === "string" && ID_RE.test(raw.groupId) ? raw.groupId : null,
    version: str(raw.version, "", 256),
    repo: str(raw.repo, "", 128),
    data,
  };
}

/** A new node of `type` with defaults filled in. */
export function createNode(type, opts = {}) {
  return normalizeNode({ ...opts, type, id: opts.id || newId("n") });
}

/** One edge, normalized (null without both ends). */
export function normalizeEdge(raw, nodeIds = null) {
  if (!isObj(raw)) return null;
  const from = typeof raw.from === "string" ? raw.from : null;
  const to = typeof raw.to === "string" ? raw.to : null;
  if (!from || !to || from === to) return null;
  if (nodeIds && (!nodeIds.has(from) || !nodeIds.has(to))) return null;
  return {
    id: typeof raw.id === "string" && ID_RE.test(raw.id) ? raw.id : newId("e"),
    from,
    to,
    fromSide: pick(raw.fromSide, SIDES, "auto"),
    toSide: pick(raw.toSide, SIDES, "auto"),
    fromField: str(raw.fromField, "", 256) || null,
    toField: str(raw.toField, "", 256) || null,
    label: str(raw.label, "", 512),
    arrow: pick(raw.arrow, ARROWS, "forward"),
    style: pick(raw.style, LINE_STYLES, "solid"),
    curve: pick(raw.curve, CURVES, "bezier"),
    color: ACCENT_IDS.has(raw.color) ? raw.color : "",
  };
}

export function createEdge(opts = {}) {
  return normalizeEdge({ ...opts, id: opts.id || newId("e") });
}

export function emptyCanvas() {
  return { schema: CANVAS_SCHEMA, version: CANVAS_VERSION, nodes: [], edges: [], viewport: { x: 0, y: 0, zoom: 1 }, settings: { snap: false, grid: GRID_DEFAULT } };
}

/**
 * A canvas document repaired into the current schema, or null when `raw` is not one (wrong schema/version,
 * not an object). Invalid nodes and dangling edges are dropped; group references to missing or cyclic
 * groups are cleared.
 */
export function normalizeCanvas(raw) {
  if (!isObj(raw) || raw.schema !== CANVAS_SCHEMA || raw.version !== CANVAS_VERSION || !Array.isArray(raw.nodes)) return null;
  const nodes = [];
  const seen = new Set();
  for (const n of raw.nodes.slice(0, 5000)) {
    const node = normalizeNode(n);
    if (!node || seen.has(node.id)) continue;
    seen.add(node.id);
    nodes.push(node);
  }
  const byId = new Map(nodes.map((n) => [n.id, n]));
  for (const n of nodes) {
    const g = n.groupId ? byId.get(n.groupId) : null;
    if (!g || g.type !== "group" || g.id === n.id) n.groupId = null;
  }
  for (const n of nodes) if (n.groupId && groupCycle(byId, n)) n.groupId = null;
  const edges = [];
  const edgeIds = new Set();
  for (const e of Array.isArray(raw.edges) ? raw.edges.slice(0, 20000) : []) {
    const edge = normalizeEdge(e, seen);
    if (!edge || edgeIds.has(edge.id)) continue;
    edgeIds.add(edge.id);
    edges.push(edge);
  }
  const vp = isObj(raw.viewport) ? raw.viewport : {};
  const st = isObj(raw.settings) ? raw.settings : {};
  return {
    schema: CANVAS_SCHEMA,
    version: CANVAS_VERSION,
    nodes,
    edges,
    viewport: { x: num(vp.x, 0), y: num(vp.y, 0), zoom: clamp(num(vp.zoom, 1), ZOOM_MIN, ZOOM_MAX) },
    settings: { snap: Boolean(st.snap), grid: clamp(Math.round(num(st.grid, GRID_DEFAULT)), 4, 200) },
  };
}

function groupCycle(byId, node) {
  const seen = new Set([node.id]);
  let cur = node;
  while (cur?.groupId) {
    if (seen.has(cur.groupId)) return true;
    seen.add(cur.groupId);
    cur = byId.get(cur.groupId);
  }
  return false;
}

/** JSON-ready deep copy of a canvas (already normalized documents round-trip unchanged). */
export function serializeCanvas(canvas) {
  return JSON.parse(JSON.stringify({
    schema: CANVAS_SCHEMA,
    version: CANVAS_VERSION,
    nodes: canvas.nodes,
    edges: canvas.edges,
    viewport: canvas.viewport,
    settings: canvas.settings,
  }));
}

/** Canvas stored under `key`, or null when absent or unusable (an unusable payload is removed). */
export function loadCanvas(storage, key) {
  let raw = null;
  try {
    raw = storage?.getItem(key);
  } catch {
    return null;
  }
  if (!raw) return null;
  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = null;
  }
  const canvas = normalizeCanvas(parsed);
  if (!canvas) {
    try {
      storage.removeItem(key);
    } catch {
      // nothing to reset
    }
  }
  return canvas;
}

/** Persist a canvas; false when the storage refused it (quota, privacy mode). */
export function saveCanvas(storage, key, canvas) {
  try {
    storage.setItem(key, JSON.stringify(serializeCanvas(canvas)));
    return true;
  } catch {
    return false;
  }
}

// ------------------------------------------------------------------ geometry

export function snapValue(v, grid) {
  return grid > 0 ? Math.round(v / grid) * grid : v;
}

export function rectOf(node) {
  return { x: node.x, y: node.y, w: node.w, h: node.h };
}

/**
 * New geometry of `rect` dragged by resize handle `handle` (n, s, e, w, ne, nw, se, sw) by (dx, dy),
 * kept within `limits` (minW/minH/maxW/maxH); the opposite edge stays put. With `grid` the moving
 * edges land on the grid.
 */
export function resizeRect(rect, handle, dx, dy, limits, grid = 0) {
  let { x, y, w, h } = rect;
  const right = x + w;
  const bottom = y + h;
  const lim = limits || { minW: 40, minH: 40, maxW: Infinity, maxH: Infinity };
  if (handle.includes("e")) {
    let r = right + dx;
    if (grid) r = snapValue(r, grid);
    w = clamp(r - x, lim.minW, lim.maxW);
  }
  if (handle.includes("s")) {
    let b = bottom + dy;
    if (grid) b = snapValue(b, grid);
    h = clamp(b - y, lim.minH, lim.maxH);
  }
  if (handle.includes("w")) {
    let l = x + dx;
    if (grid) l = snapValue(l, grid);
    w = clamp(right - l, lim.minW, lim.maxW);
    x = right - w;
  }
  if (handle.includes("n")) {
    let t = y + dy;
    if (grid) t = snapValue(t, grid);
    h = clamp(bottom - t, lim.minH, lim.maxH);
    y = bottom - h;
  }
  return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
}

/** Normalized rectangle between two corner points (marquee). */
export function rectFromPoints(x1, y1, x2, y2) {
  return { x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1), h: Math.abs(y2 - y1) };
}

export function rectsIntersect(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

export function rectContains(outer, inner) {
  return inner.x >= outer.x && inner.y >= outer.y && inner.x + inner.w <= outer.x + outer.w && inner.y + inner.h <= outer.y + outer.h;
}

export function pointInRect(px, py, r) {
  return px >= r.x && px <= r.x + r.w && py >= r.y && py <= r.y + r.h;
}

/** Ids of the nodes a marquee selects: nodes it touches, groups only when it encloses them. */
export function nodesInMarquee(nodes, rect) {
  return nodes.filter((n) => (n.type === "group" ? rectContains(rect, rectOf(n)) : rectsIntersect(rect, rectOf(n)))).map((n) => n.id);
}

export function boundsOf(nodes) {
  if (!nodes.length) return null;
  let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
  for (const n of nodes) {
    x1 = Math.min(x1, n.x);
    y1 = Math.min(y1, n.y);
    x2 = Math.max(x2, n.x + n.w);
    y2 = Math.max(y2, n.y + n.h);
  }
  return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
}

/** Viewport `{x, y, zoom}` that fits `bounds` into a `width`×`height` view with `pad` margin. */
export function fitViewport(bounds, width, height, pad = 40) {
  if (!bounds || !width || !height) return { x: 0, y: 0, zoom: 1 };
  const zoom = clamp(Math.min((width - pad * 2) / Math.max(bounds.w, 1), (height - pad * 2) / Math.max(bounds.h, 1)), ZOOM_MIN, 1.5);
  return {
    x: Math.round(width / 2 - (bounds.x + bounds.w / 2) * zoom),
    y: Math.round(height / 2 - (bounds.y + bounds.h / 2) * zoom),
    zoom,
  };
}

// ------------------------------------------------------------------ align / distribute

/** Align `nodes` (mutated) on the selection's bounding box: left|center|right|top|middle|bottom. */
export function alignNodes(nodes, mode) {
  const b = boundsOf(nodes);
  if (!b || nodes.length < 2) return nodes;
  for (const n of nodes) {
    if (mode === "left") n.x = b.x;
    else if (mode === "right") n.x = b.x + b.w - n.w;
    else if (mode === "center") n.x = Math.round(b.x + b.w / 2 - n.w / 2);
    else if (mode === "top") n.y = b.y;
    else if (mode === "bottom") n.y = b.y + b.h - n.h;
    else if (mode === "middle") n.y = Math.round(b.y + b.h / 2 - n.h / 2);
  }
  return nodes;
}

/** Equal gaps between `nodes` (mutated) along `axis` ("horizontal" | "vertical"); the outer two stay put. */
export function distributeNodes(nodes, axis) {
  if (nodes.length < 3) return nodes;
  const horiz = axis === "horizontal";
  const pos = horiz ? "x" : "y";
  const size = horiz ? "w" : "h";
  const sorted = [...nodes].sort((a, b) => a[pos] - b[pos]);
  const first = sorted[0];
  const last = sorted[sorted.length - 1];
  const span = last[pos] + last[size] - first[pos];
  const total = sorted.reduce((s, n) => s + n[size], 0);
  const gap = (span - total) / (sorted.length - 1);
  let cur = first[pos] + first[size] + gap;
  for (const n of sorted.slice(1, -1)) {
    n[pos] = Math.round(cur);
    cur += n[size] + gap;
  }
  return nodes;
}

// ------------------------------------------------------------------ groups

/** Every node inside group `groupId`, nested groups' members included. */
export function groupDescendants(nodes, groupId) {
  const out = [];
  const queue = [groupId];
  const seen = new Set(queue);
  while (queue.length) {
    const gid = queue.shift();
    for (const n of nodes) {
      if (n.groupId === gid && !seen.has(n.id)) {
        seen.add(n.id);
        out.push(n);
        if (n.type === "group") queue.push(n.id);
      }
    }
  }
  return out;
}

/**
 * The group a node dropped at its current place belongs to: the smallest group whose frame holds the
 * node's center, other than itself and the groups inside it; null outside every group.
 */
export function containingGroup(nodes, node) {
  const cx = node.x + node.w / 2;
  const cy = node.y + node.h / 2;
  const excluded = new Set([node.id]);
  if (node.type === "group") for (const d of groupDescendants(nodes, node.id)) excluded.add(d.id);
  let best = null;
  for (const g of nodes) {
    if (g.type !== "group" || excluded.has(g.id)) continue;
    if (!pointInRect(cx, cy, rectOf(g))) continue;
    if (node.type === "group" && !rectContains(rectOf(g), rectOf(node))) continue;
    if (!best || g.w * g.h < best.w * best.h) best = g;
  }
  return best ? best.id : null;
}

/** Re-derive membership of `moved` nodes from where they now are; returns the ids that changed group. */
export function reassignGroups(nodes, moved) {
  const changed = [];
  for (const n of moved) {
    const g = containingGroup(nodes, n);
    if (g !== n.groupId) {
      n.groupId = g;
      changed.push(n.id);
    }
  }
  return changed;
}

/** Nodes a drag of `ids` moves: the nodes themselves plus every member of a dragged group. */
export function dragSet(nodes, ids) {
  const out = new Map();
  for (const id of ids) {
    const n = nodes.find((x) => x.id === id);
    if (!n) continue;
    out.set(n.id, n);
    if (n.type === "group") for (const d of groupDescendants(nodes, n.id)) out.set(d.id, d);
  }
  return [...out.values()];
}

/** Paint order: groups first (outer before inner), then the other nodes in document order. */
export function paintOrder(nodes) {
  const depth = new Map();
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const depthOf = (n, guard = 0) => {
    if (depth.has(n.id)) return depth.get(n.id);
    const g = n.groupId ? byId.get(n.groupId) : null;
    const d = g && guard < 64 ? depthOf(g, guard + 1) + 1 : 0;
    depth.set(n.id, d);
    return d;
  };
  const groups = nodes.filter((n) => n.type === "group").sort((a, b) => depthOf(a) - depthOf(b));
  return [...groups, ...nodes.filter((n) => n.type !== "group")];
}

/** Remove nodes `ids` (and their edges); members of a removed group stay, outside any group. */
export function removeNodes(canvas, ids) {
  const drop = new Set(ids);
  canvas.nodes = canvas.nodes.filter((n) => !drop.has(n.id));
  for (const n of canvas.nodes) if (n.groupId && drop.has(n.groupId)) n.groupId = null;
  canvas.edges = canvas.edges.filter((e) => !drop.has(e.from) && !drop.has(e.to));
  return canvas;
}

// ------------------------------------------------------------------ struct toggles

/** Stable key of a struct layout item: its source line and name (survives reloads of the same revision). */
export function structItemKey(item) {
  return `${item.line || 0}:${item.name || item.tag || item.directive || ""}`;
}

/** Whether the type of the item `key` is shown: its own override, else the struct-wide toggle. */
export function typeVisible(structData, key) {
  const o = structData.typeOverrides?.[key];
  return typeof o === "boolean" ? o : Boolean(structData.showTypes);
}

/** Struct-wide toggle: every member follows it again (per-member overrides are cleared). */
export function setAllTypes(structData, show) {
  structData.showTypes = Boolean(show);
  structData.typeOverrides = {};
  return structData;
}

/** Flip one member's type; an aggregate flips with every member inside it. */
export function toggleItemType(structData, item) {
  const key = structItemKey(item);
  const next = !typeVisible(structData, key);
  const apply = (it) => {
    if (it.kind === "directive") return;
    const k = structItemKey(it);
    if (next === Boolean(structData.showTypes)) delete structData.typeOverrides[k];
    else structData.typeOverrides[k] = next;
    for (const c of it.children || []) apply(c);
  };
  apply(item);
  return next;
}

// ------------------------------------------------------------------ embedded tab specs

/** Hash parameters a tab node embeds (`tab=` must name a tab type). */
export function tabTypeOfHash(hash) {
  try {
    return new URLSearchParams(String(hash || "").replace(/^#/, "")).get("tab") || "";
  } catch {
    return "";
  }
}

/** URL of the isolated embed shell for a tab hash: the app with `?embed=1` (no chrome, no session). */
export function embedSrc(hash, base = "/") {
  return `${base}?embed=1#${String(hash || "").replace(/^#/, "")}`;
}
