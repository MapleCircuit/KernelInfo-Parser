/**
 * canvas_model.js - NodeMap canvas document: schema, normalization, geometry, groups, selection and layout.
 *
 * DOM-free and dependency-free (unit tested in `webapp/frontend/tests/nodemap_canvas.test.mjs`). The
 * controller owns one canvas document per NodeMap tab; every mutation goes through these helpers so the
 * persisted JSON (localStorage and Export JSON) always has the shape `normalizeCanvas()` produces.
 *
 * Canvas JSON (`schema: "kip-nodemap-canvas"`, `version: 1`):
 *   { schema, version, nodes: [Node], edges: [Edge], viewport: {x, y, zoom},
 *     settings: {snap, grid, editMode, kconfigCollapse, kconfigEngine, bgColor} }
 *   Node  { id, type, x, y, w, h, title, customTitle, color, bgColor, hideHeader, groupId, version, repo,
 *           data, autoFit, baseW, baseH, scale }
 *   Edge  { id, from, to, fromSide, toSide, fromField, toField, label, arrow, style, curve, color }
 * `color` (accent) and `bgColor` (node / canvas background, `bgVar()`) are a preset accent id (`ACCENTS`)
 * or a custom `#rrggbb`; `""` is the theme default. `hideHeader` leaves the node header out in View Mode
 * (Edit Mode shows it dimmed). With `autoFit` the node is sized to its content: `baseW`×`baseH` is the
 * natural (unscaled) size and `w`/`h` = base × `scale`.
 *
 * `UndoStack` / `historySnapshot()` hold the controller's bounded undo / redo history of the document
 * (nodes, edges, settings; viewport, Edit Mode and snap are view state and stay out of it).
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
export const STRUCT_STYLES = ["list", "table"];
/** Node types whose content can be measured and scaled (`autoFit`); groups and live tab iframes cannot. */
export const AUTOFIT_TYPES = ["struct", "symbol", "note", "source", "man"];
export const SCALE_MIN = 0.2;
export const SCALE_MAX = 4;
/** Largest natural size an auto-fitted node takes (canvas units, before scaling). */
export const AUTOFIT_MAX = { w: 4000, h: 8000 };

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
const HEX_COLOR_RE = /^#[0-9a-f]{6}$/i;

/** Whether `c` is a custom `#rrggbb` color (preset ids are not). */
export function isCustomColor(c) {
  return typeof c === "string" && HEX_COLOR_RE.test(c);
}

/** A stored color: a preset accent id or a custom `#rrggbb` (lower case); anything else is `""`. */
export function normalizeColor(c) {
  if (isCustomColor(c)) return c.toLowerCase();
  return typeof c === "string" && ACCENT_IDS.has(c) ? c : "";
}

/** CSS color of an accent id or custom `#rrggbb` (`""` = the type's default token). */
export function accentVar(id, fallback = "var(--border-color)") {
  if (isCustomColor(id)) return id.toLowerCase();
  return id && ACCENT_IDS.has(id) ? `var(--accent-${id})` : fallback;
}

/** Share of a preset accent in a background tint (the rest is the theme background it is mixed into). */
export const BG_TINT = 0.22;

/**
 * CSS background of a node or the canvas: `""` the theme `base` token, a custom `#rrggbb` as it is, a
 * preset accent tinted into `base` (accents are foreground colors; text stays readable on the tint).
 */
export function bgVar(id, base = "var(--bg-secondary)") {
  if (isCustomColor(id)) return id.toLowerCase();
  return id && ACCENT_IDS.has(id) ? `color-mix(in srgb, var(--accent-${id}) ${Math.round(BG_TINT * 100)}%, ${base})` : base;
}

/**
 * `#rrggbb` of a resolved CSS color (`#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb()` / `rgba()`), for a native
 * color input; null when it cannot be read.
 */
export function colorToHex(c) {
  const s = String(c || "").trim().toLowerCase();
  let m = /^#([0-9a-f]{3})$/.exec(s);
  if (m) return `#${[...m[1]].map((x) => x + x).join("")}`;
  m = /^#([0-9a-f]{6})(?:[0-9a-f]{2})?$/.exec(s);
  if (m) return `#${m[1]}`;
  m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/.exec(s);
  if (m) return `#${m.slice(1, 4).map((v) => Math.max(0, Math.min(255, Math.round(Number(v)))).toString(16).padStart(2, "0")).join("")}`;
  return null;
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
const trueMap = (m) => {
  const out = {};
  for (const [k, v] of Object.entries(boolMap(m))) if (v) out[k] = true;
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
        style: pick(d.style, STRUCT_STYLES, "list"),
        // #if branches folded (true) or unfolded (false) by hand, overriding the Kconfig auto-collapse
        collapsedGuards: boolMap(d.collapsedGuards),
        // Members, aggregates (with their subtree) and #if blocks hidden in Edit Mode
        hiddenItems: trueMap(d.hiddenItems),
        disableKconfigEngine: Boolean(d.disableKconfigEngine),
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
  const autoFit = AUTOFIT_TYPES.includes(type) && raw.autoFit === true;
  let w = Math.round(clamp(num(raw.w, lim.w), lim.minW, lim.maxW));
  let h = Math.round(clamp(num(raw.h, lim.h), lim.minH, lim.maxH));
  const baseW = Math.round(clamp(num(raw.baseW, w), 20, AUTOFIT_MAX.w));
  const baseH = Math.round(clamp(num(raw.baseH, h), 20, AUTOFIT_MAX.h));
  const scale = autoFit ? clamp(num(raw.scale, 1), SCALE_MIN, SCALE_MAX) : 1;
  if (autoFit) {
    // Content-sized: the per-type limits do not apply, the geometry follows base × scale
    w = Math.round(baseW * scale);
    h = Math.round(baseH * scale);
  }
  return {
    id: typeof raw.id === "string" && ID_RE.test(raw.id) ? raw.id : newId("n"),
    type,
    x: Math.round(num(raw.x, 80)),
    y: Math.round(num(raw.y, 80)),
    w,
    h,
    title: str(raw.title, "", 512) || defaultTitle(type, data),
    customTitle: str(raw.customTitle, "", 512),
    color: normalizeColor(raw.color),
    bgColor: normalizeColor(raw.bgColor),
    hideHeader: raw.hideHeader === true,
    groupId: typeof raw.groupId === "string" && ID_RE.test(raw.groupId) ? raw.groupId : null,
    version: str(raw.version, "", 256),
    repo: str(raw.repo, "", 128),
    data,
    autoFit,
    baseW: autoFit ? baseW : w,
    baseH: autoFit ? baseH : h,
    scale: Math.round(scale * 1000) / 1000,
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
    color: normalizeColor(raw.color),
  };
}

export function createEdge(opts = {}) {
  return normalizeEdge({ ...opts, id: opts.id || newId("e") });
}

export function emptyCanvas() {
  return { schema: CANVAS_SCHEMA, version: CANVAS_VERSION, nodes: [], edges: [], viewport: { x: 0, y: 0, zoom: 1 }, settings: normalizeSettings({}) };
}

/**
 * Canvas-wide settings: snap grid; `editMode` (node editing controls shown); `kconfigCollapse` (fold #if
 * branches a Kconfig engine compiles out) and `kconfigEngine` (engine tab id, `""` = the engine of each
 * node's revision); `bgColor` the canvas background (`bgVar()` over `--bg-canvas`).
 */
export function normalizeSettings(raw) {
  const st = isObj(raw) ? raw : {};
  return {
    snap: Boolean(st.snap),
    grid: clamp(Math.round(num(st.grid, GRID_DEFAULT)), 4, 200),
    editMode: st.editMode === true,
    kconfigCollapse: st.kconfigCollapse !== false,
    kconfigEngine: str(st.kconfigEngine, "", 256),
    bgColor: normalizeColor(st.bgColor),
  };
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
  return {
    schema: CANVAS_SCHEMA,
    version: CANVAS_VERSION,
    nodes,
    edges,
    viewport: { x: num(vp.x, 0), y: num(vp.y, 0), zoom: clamp(num(vp.zoom, 1), ZOOM_MIN, ZOOM_MAX) },
    settings: normalizeSettings(raw.settings),
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

/**
 * Aspect-locked resize of an auto-fitted node: handle `handle` dragged by (dx, dy) changes the content
 * scale, never the content box. `rect` is the geometry at drag start, `base` its natural `{baseW, baseH}`.
 * Edge handles scale by their axis, corners by the axis that moved more; the opposite edge stays put.
 * Returns `{x, y, w, h, scale}` with `w`/`h` = base × scale.
 */
export function scaleResize(rect, handle, dx, dy, base) {
  const bw = Math.max(1, base.baseW);
  const bh = Math.max(1, base.baseH);
  const cur = rect.w / bw;
  const right = rect.x + rect.w;
  const bottom = rect.y + rect.h;
  const horiz = handle.includes("e") || handle.includes("w");
  const vert = handle.includes("n") || handle.includes("s");
  const nw = rect.w + (handle.includes("e") ? dx : handle.includes("w") ? -dx : 0);
  const nh = rect.h + (handle.includes("s") ? dy : handle.includes("n") ? -dy : 0);
  const sw = nw / bw;
  const sh = nh / bh;
  let s = cur;
  if (horiz && vert) s = Math.abs(sw - cur) >= Math.abs(sh - cur) ? sw : sh;
  else if (horiz) s = sw;
  else if (vert) s = sh;
  s = clamp(s, SCALE_MIN, SCALE_MAX);
  const w = Math.round(bw * s);
  const h = Math.round(bh * s);
  return {
    x: Math.round(handle.includes("w") ? right - w : rect.x),
    y: Math.round(handle.includes("n") ? bottom - h : rect.y),
    w,
    h,
    scale: Math.round(s * 1000) / 1000,
  };
}

/**
 * Apply a measured natural content size to an auto-fitted node (mutated): `baseW`/`baseH` become the
 * size (bounded by `AUTOFIT_MAX` and the type's minimum), `w`/`h` follow base × scale. True when the
 * geometry changed.
 */
export function applyNaturalSize(node, natW, natH) {
  if (!node.autoFit || !(natW > 0) || !(natH > 0)) return false;
  const lim = SIZE_LIMITS[node.type];
  const baseW = Math.round(clamp(natW, Math.min(lim.minW, AUTOFIT_MAX.w), AUTOFIT_MAX.w));
  const baseH = Math.round(clamp(natH, Math.min(lim.minH, AUTOFIT_MAX.h), AUTOFIT_MAX.h));
  const w = Math.round(baseW * node.scale);
  const h = Math.round(baseH * node.scale);
  const changed = baseW !== node.baseW || baseH !== node.baseH || w !== node.w || h !== node.h;
  Object.assign(node, { baseW, baseH, w, h });
  return changed;
}

/** Turn auto-fit on or off (mutated); off keeps the current on-screen size at scale 1. */
export function setAutoFit(node, on) {
  if (!AUTOFIT_TYPES.includes(node.type)) return node;
  if (on && !node.autoFit) {
    Object.assign(node, { autoFit: true, scale: 1, baseW: node.w, baseH: node.h });
  } else if (!on && node.autoFit) {
    const lim = SIZE_LIMITS[node.type];
    Object.assign(node, {
      autoFit: false,
      scale: 1,
      w: Math.round(clamp(node.w, lim.minW, lim.maxW)),
      h: Math.round(clamp(node.h, lim.minH, lim.maxH)),
    });
    node.baseW = node.w;
    node.baseH = node.h;
  }
  return node;
}

/** Set the on-screen width or height of an auto-fitted node: the other axis follows the aspect ratio. */
export function setScaledSize(node, axis, value) {
  const s = clamp(axis === "w" ? value / Math.max(1, node.baseW) : value / Math.max(1, node.baseH), SCALE_MIN, SCALE_MAX);
  node.scale = Math.round(s * 1000) / 1000;
  node.w = Math.round(node.baseW * node.scale);
  node.h = Math.round(node.baseH * node.scale);
  return node;
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

// ------------------------------------------------------------------ duplicate / clipboard

/**
 * Deep copy of nodes `nodeIds` (a group brings every member inside it) with fresh ids, moved by `offset`
 * `{x, y}`: members of a copied group stay in the copy of it (others leave any group), and every edge
 * whose two ends are both copied is copied too. Returns `{nodes, edges}`; `canvas` is not changed.
 */
export function cloneSubgraph(canvas, nodeIds, offset = { x: 0, y: 0 }) {
  const picked = dragSet(canvas.nodes, nodeIds);
  const idMap = new Map(picked.map((n) => [n.id, newId("n")]));
  const dx = Math.round(num(offset?.x, 0));
  const dy = Math.round(num(offset?.y, 0));
  // Document order keeps the paint order of the copies
  const nodes = canvas.nodes.filter((n) => idMap.has(n.id)).map((n) => {
    const copy = JSON.parse(JSON.stringify(n));
    copy.id = idMap.get(n.id);
    copy.x = n.x + dx;
    copy.y = n.y + dy;
    copy.groupId = n.groupId && idMap.has(n.groupId) ? idMap.get(n.groupId) : null;
    return copy;
  });
  const edges = canvas.edges.filter((e) => idMap.has(e.from) && idMap.has(e.to)).map((e) => ({
    ...JSON.parse(JSON.stringify(e)),
    id: newId("e"),
    from: idMap.get(e.from),
    to: idMap.get(e.to),
  }));
  return { nodes, edges };
}

export const CLIPBOARD_KEY = "kip-nodemap-clipboard";
export const CLIPBOARD_SCHEMA = "kip-nodemap-clip";

/** Clipboard payload of a selection (shared by every NodeMap tab through localStorage). */
export function clipboardPayload(canvas, nodeIds, origin = "") {
  const { nodes, edges } = cloneSubgraph(canvas, nodeIds);
  return { schema: CLIPBOARD_SCHEMA, version: CANVAS_VERSION, origin: str(origin, "", 256), stamp: newId("c"), nodes, edges };
}

/** A clipboard payload repaired (null when it is not one): nodes normalized, edges kept between them. */
export function normalizeClipboard(raw) {
  if (!isObj(raw) || raw.schema !== CLIPBOARD_SCHEMA || !Array.isArray(raw.nodes)) return null;
  const nodes = [];
  const seen = new Set();
  for (const n of raw.nodes.slice(0, 5000)) {
    const node = normalizeNode(n);
    if (!node || seen.has(node.id)) continue;
    seen.add(node.id);
    nodes.push(node);
  }
  if (!nodes.length) return null;
  for (const n of nodes) if (n.groupId && !seen.has(n.groupId)) n.groupId = null;
  const edges = (Array.isArray(raw.edges) ? raw.edges : []).map((e) => normalizeEdge(e, seen)).filter(Boolean);
  return { schema: CLIPBOARD_SCHEMA, version: CANVAS_VERSION, origin: str(raw.origin, "", 256), stamp: str(raw.stamp, "", 128), nodes, edges };
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

/** `hiddenItems` key of the struct-wide outer conditions banner (`layout.outer_conds`). */
export const OUTER_KEY = "__outer__";

/** Hide or restore one struct item (member, aggregate with its subtree, or `#if` block by its opening line). */
export function toggleHidden(structData, key) {
  if (!key) return false;
  if (structData.hiddenItems[key]) delete structData.hiddenItems[key];
  else structData.hiddenItems[key] = true;
  return Boolean(structData.hiddenItems[key]);
}

/** Restore every hidden struct item; returns how many there were. */
export function showAllHidden(structData) {
  const n = Object.keys(structData.hiddenItems || {}).length;
  structData.hiddenItems = {};
  return n;
}

/**
 * Fold or unfold one `#if` branch by hand. `autoCollapsed` is what the Kconfig engine decides for it:
 * a manual state equal to it is dropped, so the branch follows the engine again.
 */
export function toggleGuardCollapse(structData, key, autoCollapsed = false) {
  const manual = structData.collapsedGuards[key];
  const now = typeof manual === "boolean" ? manual : Boolean(autoCollapsed);
  const next = !now;
  if (next === Boolean(autoCollapsed)) delete structData.collapsedGuards[key];
  else structData.collapsedGuards[key] = next;
  return next;
}

// ------------------------------------------------------------------ undo / redo

/** Settings that are view state, not document: undo never flips them. */
const VIEW_SETTINGS = ["editMode", "snap"];

/** Comparable snapshot (JSON text) of what undo restores: nodes, edges and the document settings. */
export function historySnapshot(canvas) {
  const settings = { ...(canvas?.settings || {}) };
  for (const k of VIEW_SETTINGS) delete settings[k];
  return JSON.stringify({ nodes: canvas?.nodes || [], edges: canvas?.edges || [], settings });
}

/**
 * Bounded undo / redo history of snapshots. `base` is the document as last seen; `record(snap)` pushes the
 * previous base when the document changed (no-op snapshots are not pushed) and clears the redo stack;
 * consecutive records with the same `coalesce` key (live color drags, typing) fold into one step, up to
 * and including the record that has `end` set (the final change event of that edit).
 * `absorb(snap)` takes a change that is not the user's (auto-fit sizes, a resolved man page) into the base.
 */
export class UndoStack {
  constructor(limit = 80) {
    this.limit = limit;
    this.undoList = [];
    this.redoList = [];
    this.base = null;
    this.lastKey = null;
  }

  reset(snap = null) {
    this.undoList = [];
    this.redoList = [];
    this.base = snap;
    this.lastKey = null;
  }

  get canUndo() {
    return this.undoList.length > 0;
  }

  get canRedo() {
    return this.redoList.length > 0;
  }

  absorb(snap) {
    this.base = snap;
    this.lastKey = null;
  }

  /** End a coalescing run (the next record is a step of its own). */
  close() {
    this.lastKey = null;
  }

  /** True when a new step was pushed. */
  record(snap, coalesce = null, end = false) {
    if (this.base === null) {
      this.base = snap;
      return false;
    }
    if (snap === this.base) {
      // A no-op of another kind, or the `end` of the run, ends a coalescing run
      if (end || coalesce !== this.lastKey) this.lastKey = null;
      return false;
    }
    const merge = Boolean(coalesce) && coalesce === this.lastKey && this.undoList.length > 0;
    this.lastKey = end ? null : coalesce || null;
    this.redoList = [];
    if (merge) {
      this.base = snap;
      return false;
    }
    this.undoList.push(this.base);
    if (this.undoList.length > this.limit) this.undoList.shift();
    this.base = snap;
    return true;
  }

  /** Snapshot to restore (the step before `current`), or null when there is none. */
  undo(current) {
    if (current !== undefined && current !== null && current !== this.base) this.record(current);
    if (!this.undoList.length) return null;
    this.redoList.push(this.base);
    this.base = this.undoList.pop();
    this.lastKey = null;
    return this.base;
  }

  /** Snapshot to restore (the step undone last), or null; a change since the undo drops the redo stack. */
  redo(current) {
    if (current !== undefined && current !== null && current !== this.base) {
      this.record(current);
      return null;
    }
    if (!this.redoList.length) return null;
    this.undoList.push(this.base);
    this.base = this.redoList.pop();
    this.lastKey = null;
    return this.base;
  }
}
