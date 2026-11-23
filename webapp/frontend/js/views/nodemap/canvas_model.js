/**
 * canvas_model.js - NodeMap canvas document: schema, normalization, geometry, groups, selection,
 * visibility layers, focus subgraph BFS, sub-map portals, tours, structural-sharing undo history,
 * and async IndexedDB persistence (`webapp/API_FRONTEND.md` §5.3).
 *
 * DOM-free and dependency-free (unit tested in `webapp/frontend/tests/nodemap_canvas.test.mjs`). The
 * controller owns one canvas document per NodeMap tab; every mutation goes through these helpers so the
 * persisted JSON (IndexedDB USER_STORE, localStorage fallback, and Export JSON) always has the shape
 * `normalizeCanvas()` produces.
 */
import { hexOfCssColor } from "../../utils/css_color.js";
import { EMBED_NODE_RE, normalizeEmbedViewState } from "../../utils/embed_mode.js";

export const CANVAS_SCHEMA = "kip-nodemap-canvas";
export const CANVAS_VERSION = 1;
export const STORAGE_PREFIX = "kip-nodemap-canvas:";
export const USER_CANVAS_KIND = "nodemap-canvas";

export const NODE_TYPES = [
  "struct", "symbol", "source", "man", "tab", "note", "group",
  "diff", "mail", "kconfig", "maintainer", "cve", "kapi", "doc", "history", "commit",
];
/** Header badge of each node type (live node and SVG export). */
export const NODE_BADGES = {
  struct: "struct", symbol: "symbol", source: "source", man: "man", tab: "tab", note: "note", group: "group",
  diff: "diff", mail: "mail", kconfig: "kconfig", maintainer: "maint",
  cve: "cve", kapi: "kapi", doc: "doc", history: "hist", commit: "commit",
};
export const SIDES = ["auto", "top", "right", "bottom", "left"];
export const ARROWS = ["none", "forward", "backward", "both"];
export const LINE_STYLES = ["solid", "dashed", "dotted"];
export const CURVES = ["bezier", "straight", "orthogonal"];
export const SOURCE_MODES = ["file", "range", "symbol"];
export const SYMBOL_KINDS = ["", "struct", "union", "function", "macro", "enum"];
export const DIFF_MODES = ["file", "commit"];
export const KAPI_CATALOGS = ["summary", "syscalls", "exports", "params", "tracepoints", "modules", "devices"];
export const DOC_MODES = ["auto", "rst", "kernel-doc"];
export const HISTORY_MODES = ["symbol", "blame"];
export const ALIGN_MODES = ["left", "center", "right", "top", "middle", "bottom"];
export const STRUCT_STYLES = ["list", "table", "layout"];
export const LOCK_KINDS = ["spinlock", "mutex", "rwsem", "rcu", "atomic", "seqlock", "lock-order"];
/** Node types whose content can be measured and scaled (`autoFit`); groups and live tab iframes cannot. */
export const AUTOFIT_TYPES = [
  "struct", "symbol", "note", "source", "man", "diff", "mail", "kconfig", "maintainer",
  "cve", "kapi", "doc", "history", "commit",
];
/** Node types a tab of the workspace can open (Open as tab, Copy to Session). */
export const TAB_OPENABLE_TYPES = [
  "struct", "source", "man", "tab", "symbol", "diff", "mail", "kconfig", "maintainer",
  "cve", "kapi", "doc", "history", "commit",
];
/** Node types whose content scales (`scale`): every type but groups. */
export const scalable = (type) => type !== "group";
export const SCALE_MIN = 0.2;
export const SCALE_MAX = 4;
/** Largest natural size an auto-fitted node takes (canvas units, before scaling). */
export const AUTOFIT_MAX = { w: 4000, h: 8000 };

/** Semantic edge presets (Edge Inspector quick style + label). */
export const EDGE_PRESETS = [
  { id: "", label: "Custom" },
  { id: "calls", label: "calls", arrow: "forward", style: "solid", color: "blue" },
  { id: "points to", label: "points to", arrow: "forward", style: "solid", color: "cyan" },
  { id: "embeds", label: "embeds", arrow: "forward", style: "dashed", color: "purple" },
  { id: "locks", label: "locks / RCU", arrow: "forward", style: "dashed", color: "orange" },
  { id: "depends on", label: "depends on", arrow: "forward", style: "solid", color: "yellow" },
  { id: "fixes", label: "fixes", arrow: "forward", style: "solid", color: "green" },
  { id: "replies to", label: "replies to", arrow: "forward", style: "solid", color: "pink" },
];

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
 * `#rrggbb` of a resolved CSS color (`#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb()` / `rgba()` in comma or space
 * syntax, `color-mix(in srgb, …)` of literals; `utils/css_color.js`), alpha dropped, for a native color
 * input; null when it cannot be read.
 */
export function colorToHex(c) {
  return hexOfCssColor(c);
}

/**
 * Box a node's content lays out in (canvas units, before `scale`): the natural size of an auto-fitted
 * node, else the outer box divided by the content scale. `{w, h, s}`.
 */
export function contentBox(node) {
  const s = node.scale && scalable(node.type) ? node.scale : 1;
  if (node.autoFit) return { w: node.baseW, h: node.baseH, s };
  return { w: node.w / s, h: node.h / s, s };
}

/**
 * Set the content scale of a node (mutated; groups have none): an auto-fitted node grows or shrinks
 * with it (base × scale), any other node keeps its outer box and lays its content out bigger or smaller.
 */
export function setContentScale(node, value) {
  if (!scalable(node.type)) return node;
  const s = Math.round(clamp(num(Number(value), 1), SCALE_MIN, SCALE_MAX) * 1000) / 1000;
  node.scale = s;
  if (node.autoFit) {
    node.w = Math.round(node.baseW * s);
    node.h = Math.round(node.baseH * s);
  }
  return node;
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
  diff: { w: 620, h: 420, minW: 260, minH: 120, maxW: 2400, maxH: 2400 },
  mail: { w: 480, h: 360, minW: 240, minH: 120, maxW: 1800, maxH: 2400 },
  kconfig: { w: 340, h: 300, minW: 200, minH: 100, maxW: 1400, maxH: 2000 },
  maintainer: { w: 380, h: 320, minW: 220, minH: 100, maxW: 1400, maxH: 2000 },
  cve: { w: 420, h: 360, minW: 240, minH: 140, maxW: 1600, maxH: 2400 },
  kapi: { w: 400, h: 340, minW: 240, minH: 120, maxW: 1600, maxH: 2400 },
  doc: { w: 480, h: 380, minW: 260, minH: 140, maxW: 2000, maxH: 2400 },
  history: { w: 440, h: 340, minW: 240, minH: 140, maxW: 1800, maxH: 2400 },
  commit: { w: 460, h: 420, minW: 260, minH: 140, maxW: 1800, maxH: 2400 },
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
const normalizeFieldLocks = (m) => {
  const out = {};
  if (!isObj(m)) return out;
  for (const [k, v] of Object.entries(m).slice(0, 512)) {
    if (!k || k.length > 256) continue;
    if (typeof v === "string" && LOCK_KINDS.includes(v)) {
      out[k] = { kind: v, lockName: "" };
    } else if (isObj(v) && LOCK_KINDS.includes(v.kind)) {
      out[k] = { kind: v.kind, lockName: str(v.lockName, "", 128).trim() };
    }
  }
  return out;
};
const normalizeNodeLocks = (raw) => {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const x of raw.slice(0, 32)) {
    if (!isObj(x) || !LOCK_KINDS.includes(x.kind)) continue;
    out.push({
      kind: x.kind,
      lockName: str(x.lockName, "", 128).trim(),
      note: str(x.note, "", 256).trim(),
    });
  }
  return out;
};

/** Characters kept of one saved mail message body (`fullBody`, `pinnedBodies`). */
export const MAIL_BODY_MAX = 200_000;
/** Saved message bodies of one thread node: at most this many messages and twice `MAIL_BODY_MAX` characters. */
export const MAIL_PIN_MAX = 12;

function pinnedMailBodies(m) {
  const out = {};
  if (!isObj(m)) return out;
  let total = 0;
  let n = 0;
  for (const [k, v] of Object.entries(m)) {
    if (typeof v !== "string" || !k || k.length > 512 || n >= MAIL_PIN_MAX) continue;
    const body = v.slice(0, MAIL_BODY_MAX);
    if (total + body.length > MAIL_BODY_MAX * 2) continue;
    out[k] = body;
    total += body.length;
    n += 1;
  }
  return out;
}

/** Type-specific payload, normalized. */
export function normalizeData(type, raw, depth = 0) {
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
        fieldLocks: normalizeFieldLocks(d.fieldLocks),
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
        // NodeMap embedded session the iframe runs in (`""`: the canvas's first one, `embedSessionOf()`)
        embedSessionId: typeof d.embedSessionId === "string" && ID_RE.test(d.embedSessionId) ? d.embedSessionId : "",
        // Frozen: `hash` / `viewState` stay what they were when it was frozen and the iframe's own changes
        // are ephemeral; else (Keep changes) the iframe's navigation and view state are written back
        frozen: d.frozen === true,
        // view state beyond the hash (`utils/embed_mode.js` normalizeEmbedViewState): scroll positions,
        // view-local fields; sent to the embed shell when it starts
        viewState: normalizeEmbedViewState(d.viewState),
      };
    case "diff": {
      const mode = pick(d.mode, DIFF_MODES, d.commitHash ? "commit" : "file");
      return {
        mode,
        versionA: str(d.versionA, "", 256),
        versionB: str(d.versionB, "", 256),
        path: str(d.path, "", 1024),
        oldPath: str(d.oldPath, "", 1024),
        commitHash: /^[0-9a-f]{4,64}$/i.test(String(d.commitHash || "")) ? String(d.commitHash).toLowerCase() : "",
      };
    }
    case "mail": {
      // One message of a thread on its own ("Expand Thread to Nodes", the ⊕ of a message)
      const single = Boolean(d.single) && Boolean(str(d.mid, "", 512));
      return {
        tid: Number.isInteger(d.tid) && d.tid > 0 ? d.tid : null,
        mid: str(d.mid, "", 512).replace(/^<|>$/g, ""),
        subject: str(d.subject, "", 512),
        // Messages whose body excerpt shows (message id -> true)
        openMids: trueMap(d.openMids),
        // Messages of a thread shown whole instead of their 10-line excerpt
        fullMids: trueMap(d.fullMids),
        // Whole messages saved with the canvas (message id -> body), capped per message and per node
        pinnedBodies: single ? {} : pinnedMailBodies(d.pinnedBodies),
        single,
        // The whole body of a single-message node, saved once it loads (shown without a refetch)
        fullBody: single ? str(d.fullBody, "", MAIL_BODY_MAX) : "",
      };
    }
    case "kconfig":
      return {
        symbol: str(d.symbol, "", 256).replace(/^CONFIG_/, ""),
        arch: str(d.arch, "", 64) || "x86_64",
      };
    case "maintainer":
      return {
        secId: posInt(d.secId),
        title: str(d.title, "", 512),
        query: str(d.query, "", 256),
        path: str(d.path, "", 1024),
      };
    case "cve":
      return {
        cveId: str(d.cveId, "", 64).trim().toUpperCase(),
        release: str(d.release, "", 128).trim(),
        filePath: str(d.filePath, "", 1024).trim(),
      };
    case "kapi":
      return {
        catalog: pick(d.catalog, KAPI_CATALOGS, "summary"),
        query: str(d.query, "", 256).trim(),
        symbol: str(d.symbol, "", 256).trim(),
        arch: str(d.arch, "", 64).trim() || "x86_64",
      };
    case "doc":
      return {
        path: str(d.path, "", 1024).trim(),
        mode: pick(d.mode, DOC_MODES, "auto"),
        functions: str(d.functions, "", 256).trim(),
      };
    case "history":
      return {
        mode: pick(d.mode, HISTORY_MODES, "symbol"),
        path: str(d.path, "", 1024).trim(),
        line: posInt(d.line) || 1,
        name: str(d.name, "", 256).trim(),
        astId: Number.isInteger(d.astId) ? d.astId : null,
      };
    case "commit":
      return {
        commitHash: /^[0-9a-f]{4,64}$/i.test(String(d.commitHash || "")) ? String(d.commitHash).toLowerCase() : "",
        // Subject known where the node was spawned from (a blame row): the title until the commit loads
        subject: str(d.subject, "", 512).trim(),
      };
    case "note":
      return { text: str(d.text, "", 20000), markdown: d.markdown !== false };
    case "group":
      return depth < 8 && isObj(d.subCanvas) ? { subCanvas: normalizeCanvas(d.subCanvas, depth + 1) } : {};
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
      return data.subCanvas ? "Sub-Map" : "Group";
    case "diff":
      if (data.mode === "commit") return data.commitHash ? `${data.path ? `${data.path.split("/").pop()} @ ` : ""}${data.commitHash.slice(0, 12)}` : "Commit diff";
      return data.path ? `${data.path.split("/").pop()} · ${data.versionA || "?"}..${data.versionB || "?"}` : "Diff";
    case "mail":
      return data.subject || (data.mid ? `<${data.mid}>` : data.tid ? `Thread ${data.tid}` : "Mail thread");
    case "kconfig":
      return data.symbol ? `CONFIG_${data.symbol}` : "Kconfig symbol";
    case "maintainer":
      return data.title || data.path || data.query || "Maintainers";
    case "cve":
      return data.cveId || (data.filePath ? `CVEs · ${data.filePath.split("/").pop()}` : "CVE Record");
    case "kapi":
      return data.symbol ? `KAPI · ${data.symbol}` : data.query ? `KAPI · ${data.query}` : `Kernel API (${data.catalog || "summary"})`;
    case "doc":
      return data.path ? data.path.split("/").pop() : "Kernel Docs";
    case "history":
      if (data.mode === "blame") return data.path ? `Blame · ${data.path.split("/").pop()}` : "File Blame";
      return data.name ? `History · ${data.name}` : (data.path ? `History · ${data.path.split("/").pop()}:${data.line || 1}` : "Symbol History");
    case "commit":
      if (!data.commitHash) return "Commit";
      return data.subject ? `${data.commitHash.slice(0, 12)} ${data.subject}` : `Commit ${data.commitHash.slice(0, 12)}`;
    default:
      return "Node";
  }
}

/** Shown title: the custom title when set, else the stored/default one. */
export function displayTitle(node) {
  return node.customTitle || node.title || defaultTitle(node.type, node.data || {});
}

/** One node, normalized (null when it cannot be repaired: unknown type). */
export function normalizeNode(raw, depth = 0) {
  if (!isObj(raw) || !NODE_TYPES.includes(raw.type)) return null;
  const type = raw.type;
  const lim = SIZE_LIMITS[type];
  const data = normalizeData(type, raw.data, depth);
  const autoFit = AUTOFIT_TYPES.includes(type) && raw.autoFit === true;
  let w = Math.round(clamp(num(raw.w, lim.w), lim.minW, lim.maxW));
  let h = Math.round(clamp(num(raw.h, lim.h), lim.minH, lim.maxH));
  const baseW = Math.round(clamp(num(raw.baseW, w), 20, AUTOFIT_MAX.w));
  const baseH = Math.round(clamp(num(raw.baseH, h), 20, AUTOFIT_MAX.h));
  const scale = scalable(type) ? clamp(num(raw.scale, 1), SCALE_MIN, SCALE_MAX) : 1;
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
    layerId: typeof raw.layerId === "string" && ID_RE.test(raw.layerId) ? raw.layerId : "",
    version: str(raw.version, "", 256),
    repo: str(raw.repo, "", 128),
    locks: normalizeNodeLocks(raw.locks),
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

function normalizeWaypoints(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const w of raw.slice(0, 64)) {
    if (!isObj(w)) continue;
    const x = num(w.x, NaN);
    const y = num(w.y, NaN);
    if (Number.isFinite(x) && Number.isFinite(y)) out.push({ x: Math.round(x), y: Math.round(y) });
  }
  return out;
}

function normalizeCallSite(raw) {
  if (!isObj(raw)) return null;
  const path = str(raw.path, "", 1024);
  if (!path) return null;
  const line = posInt(raw.line) || 1;
  return { path, line };
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
    fromField: str(raw.fromField, "", 600) || null,
    toField: str(raw.toField, "", 256) || null,
    label: str(raw.label, "", 512),
    arrow: pick(raw.arrow, ARROWS, "forward"),
    style: pick(raw.style, LINE_STYLES, "solid"),
    curve: pick(raw.curve, CURVES, "bezier"),
    color: normalizeColor(raw.color),
    waypoints: normalizeWaypoints(raw.waypoints),
    layerId: typeof raw.layerId === "string" && ID_RE.test(raw.layerId) ? raw.layerId : "",
    lockKind: pick(raw.lockKind, ["", ...LOCK_KINDS], ""),
    callSite: normalizeCallSite(raw.callSite),
  };
}

export function createEdge(opts = {}) {
  return normalizeEdge({ ...opts, id: opts.id || newId("e") });
}

/** Apply a semantic edge preset (`EDGE_PRESETS`) to an edge object (mutated). */
export function applyEdgePreset(edge, presetId) {
  const p = EDGE_PRESETS.find((x) => x.id === presetId);
  if (!edge || !p || !p.id) return false;
  edge.label = p.label;
  edge.arrow = p.arrow;
  edge.style = p.style;
  edge.color = p.color;
  if (presetId === "locks" && !edge.lockKind) edge.lockKind = "rcu";
  return true;
}

// ------------------------------------------------------------------ locking & concurrency annotations

/** Set or clear a locking annotation (`{ kind, lockName }`) on a struct field key. */
export function setFieldLock(structData, fieldKey, lock) {
  if (!structData || !fieldKey) return null;
  if (!isObj(structData.fieldLocks)) structData.fieldLocks = {};
  if (!lock || !LOCK_KINDS.includes(lock.kind)) {
    delete structData.fieldLocks[fieldKey];
    return null;
  }
  const next = { kind: lock.kind, lockName: str(lock.lockName, "", 128).trim() };
  structData.fieldLocks[fieldKey] = next;
  return next;
}

/** Append a locking annotation (`{ kind, lockName, note }`) to `node.locks`. */
export function addNodeLock(node, lock = {}) {
  if (!node) return null;
  if (!Array.isArray(node.locks)) node.locks = [];
  if (node.locks.length >= 32) return null;
  const entry = {
    kind: pick(lock.kind, LOCK_KINDS, "spinlock"),
    lockName: str(lock.lockName, "", 128).trim(),
    note: str(lock.note, "", 256).trim(),
  };
  node.locks.push(entry);
  return entry;
}

/** Update an entry in `node.locks` by index. */
export function updateNodeLock(node, idx, patch = {}) {
  if (!node || !Array.isArray(node.locks) || idx < 0 || idx >= node.locks.length) return null;
  const cur = node.locks[idx];
  const next = {
    kind: patch.kind !== undefined ? pick(patch.kind, LOCK_KINDS, cur.kind) : cur.kind,
    lockName: patch.lockName !== undefined ? str(patch.lockName, "", 128).trim() : cur.lockName,
    note: patch.note !== undefined ? str(patch.note, "", 256).trim() : cur.note,
  };
  node.locks[idx] = next;
  return next;
}

/** Remove a locking annotation from `node.locks` by index. */
export function removeNodeLock(node, idx) {
  if (!node || !Array.isArray(node.locks) || idx < 0 || idx >= node.locks.length) return false;
  node.locks.splice(idx, 1);
  return true;
}

/** Format a lock annotation as a compact badge label. */
export function formatLockBadge(lock) {
  if (!lock || !lock.kind) return "";
  return lock.lockName ? `🔒 ${lock.kind}: ${lock.lockName}` : `🔒 ${lock.kind}`;
}

// ------------------------------------------------------------------ visibility layers

function normalizeLayers(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  const seen = new Set();
  for (const l of raw.slice(0, 64)) {
    if (!isObj(l) || typeof l.id !== "string" || !ID_RE.test(l.id) || seen.has(l.id)) continue;
    seen.add(l.id);
    out.push({
      id: l.id,
      name: str(l.name, "", 128).trim() || l.id,
      visible: l.visible !== false,
      color: normalizeColor(l.color) || "blue",
    });
  }
  return out;
}

export function addLayer(settings, name = "", color = "blue") {
  if (!settings) return null;
  if (!Array.isArray(settings.layers)) settings.layers = [];
  if (settings.layers.length >= 64) return null;
  const layer = {
    id: newId("lyr"),
    name: str(name, "", 128).trim() || `Layer ${settings.layers.length + 1}`,
    visible: true,
    color: normalizeColor(color) || "blue",
  };
  settings.layers.push(layer);
  return layer;
}

export function toggleLayerVisibility(settings, id) {
  const layer = (settings?.layers || []).find((l) => l.id === id);
  if (!layer) return null;
  layer.visible = !layer.visible;
  return layer.visible;
}

/**
 * Set a layer's visibility explicitly (the Inspector checkbox: its `input` and `change` events both
 * land here, so the state follows the checkbox instead of flipping twice). Null for an unknown layer.
 */
export function setLayerVisibility(settings, id, visible) {
  const layer = (settings?.layers || []).find((l) => l.id === id);
  if (!layer) return null;
  layer.visible = Boolean(visible);
  return layer.visible;
}

export function removeLayer(canvas, id) {
  if (!canvas?.settings?.layers) return false;
  const before = canvas.settings.layers.length;
  canvas.settings.layers = canvas.settings.layers.filter((l) => l.id !== id);
  if (canvas.settings.layers.length === before) return false;
  for (const n of canvas.nodes || []) if (n.layerId === id) n.layerId = "";
  for (const e of canvas.edges || []) if (e.layerId === id) e.layerId = "";
  return true;
}

export function isLayerVisible(settings, layerId) {
  if (!layerId) return true;
  const layer = (settings?.layers || []).find((l) => l.id === layerId);
  return layer ? layer.visible !== false : true;
}

/** Whether edge `e` is visible: its own layer and the layers of both nodes it joins are shown. */
export function isEdgeVisible(settings, e, from = null, to = null) {
  return isLayerVisible(settings, e?.layerId) && isLayerVisible(settings, from?.layerId) && isLayerVisible(settings, to?.layerId);
}

/** Filter canvas nodes & edges to only those visible in active visibility layers. */
export function visibleCanvasSlice(canvas) {
  if (!canvas) return canvas;
  const st = canvas.settings || {};
  if (!st.layers?.length) return canvas;
  const visNodes = canvas.nodes.filter((n) => isLayerVisible(st, n.layerId));
  const visIds = new Set(visNodes.map((n) => n.id));
  const visEdges = canvas.edges.filter((e) => visIds.has(e.from) && visIds.has(e.to) && isLayerVisible(st, e.layerId));
  return { ...canvas, nodes: visNodes, edges: visEdges };
}

// ------------------------------------------------------------------ tours normalization

function normalizeTours(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  const seen = new Set();
  for (const t of raw.slice(0, 32)) {
    if (!isObj(t) || typeof t.id !== "string" || !ID_RE.test(t.id) || seen.has(t.id)) continue;
    seen.add(t.id);
    const steps = [];
    const stepSeen = new Set();
    for (const s of Array.isArray(t.steps) ? t.steps.slice(0, 200) : []) {
      if (!isObj(s)) continue;
      const sid = typeof s.id === "string" && ID_RE.test(s.id) && !stepSeen.has(s.id) ? s.id : newId("step");
      stepSeen.add(sid);
      const vp = isObj(s.viewport) ? s.viewport : {};
      steps.push({
        id: sid,
        title: str(s.title, "", 256) || "Step",
        caption: str(s.caption, "", 2000),
        nodeIds: Array.isArray(s.nodeIds) ? s.nodeIds.filter((x) => typeof x === "string" && ID_RE.test(x)).slice(0, 500) : [],
        edgeIds: Array.isArray(s.edgeIds) ? s.edgeIds.filter((x) => typeof x === "string" && ID_RE.test(x)).slice(0, 500) : [],
        viewport: { x: Math.round(num(vp.x, 0)), y: Math.round(num(vp.y, 0)), zoom: clamp(num(vp.zoom, 1), ZOOM_MIN, ZOOM_MAX) },
      });
    }
    out.push({ id: t.id, title: str(t.title, "", 128).trim() || "Walkthrough", steps });
  }
  return out;
}

export function emptyCanvas() {
  return {
    schema: CANVAS_SCHEMA,
    version: CANVAS_VERSION,
    nodes: [],
    edges: [],
    viewport: { x: 0, y: 0, zoom: 1 },
    settings: normalizeSettings({}),
    tours: [],
  };
}

/**
 * Canvas-wide settings: snap grid; `editMode` (node editing controls shown); `kconfigCollapse` (fold #if
 * branches a Kconfig engine compiles out) and `kconfigEngine` (engine tab id, `""` = the engine of each
 * node's revision); `bgColor` the canvas background (`bgVar()` over `--bg-canvas`); `embedSessions`
 * (1+ `{id, name}`) and `followSession` (`""` or one of their ids, default the first); `layers`
 * (visibility layers `[{id, name, visible, color}]`); `renderQuality` the supersampling of embedded
 * tab iframes (`RENDER_QUALITIES`, `iframeSupersample()`).
 */
export function normalizeSettings(raw) {
  const st = isObj(raw) ? raw : {};
  const embedSessions = normalizeEmbedSessions(st.embedSessions);
  return {
    snap: Boolean(st.snap),
    grid: clamp(Math.round(num(st.grid, GRID_DEFAULT)), 4, 200),
    editMode: st.editMode === true,
    kconfigCollapse: st.kconfigCollapse !== false,
    kconfigEngine: str(st.kconfigEngine, "", 256),
    bgColor: normalizeColor(st.bgColor),
    embedSessions,
    followSession: typeof st.followSession === "string" && (st.followSession === "" || embedSessions.some((x) => x.id === st.followSession))
      ? st.followSession : embedSessions[0].id,
    layers: normalizeLayers(st.layers),
    renderQuality: pick(st.renderQuality, RENDER_QUALITIES, "auto"),
  };
}

// ------------------------------------------------------------------ render quality

/** Tab iframe supersampling: `auto` follows the magnification, the others are fixed factors. */
export const RENDER_QUALITIES = ["auto", "1", "1.5", "2"];
export const SUPERSAMPLE_MAX = 2;

/**
 * Internal resolution factor of an embedded tab iframe: it lays out at `k ×` its box (its document
 * zoomed by `k`) and is drawn scaled by `1 / k`, so the browser rasterizes `k ×` the pixels before the
 * canvas zoom and the node's content scale magnify it. `auto` picks 1, 1.5 or 2 from the magnification
 * `zoom × scale` (nothing to gain at or below 100%).
 */
export function iframeSupersample(quality, zoom = 1, scale = 1) {
  if (quality && quality !== "auto") {
    const k = Number(quality);
    return Number.isFinite(k) ? clamp(k, 1, SUPERSAMPLE_MAX) : 1;
  }
  const m = num(zoom, 1) * num(scale, 1);
  if (m <= 1.05) return 1;
  if (m <= 1.6) return 1.5;
  return SUPERSAMPLE_MAX;
}

/** `v` snapped to the device pixel grid (`dpr` device pixels per CSS pixel): crisp layer offsets. */
export function snapToDevicePixel(v, dpr = 1) {
  const d = num(dpr, 1) > 0 ? num(dpr, 1) : 1;
  return Math.round(num(v, 0) * d) / d;
}

/** Default embedded session of a new canvas. */
export const DEFAULT_EMBED_SESSION = { id: "nms-1", name: "Canvas Session 1" };
export const EMBED_SESSIONS_MAX = 16;

function normalizeEmbedSessions(raw) {
  const out = [];
  const seen = new Set();
  for (const x of Array.isArray(raw) ? raw.slice(0, EMBED_SESSIONS_MAX) : []) {
    if (!isObj(x) || typeof x.id !== "string" || !ID_RE.test(x.id) || seen.has(x.id)) continue;
    seen.add(x.id);
    out.push({ id: x.id, name: str(x.name, "", 128).trim() || x.id });
  }
  return out.length ? out : [{ ...DEFAULT_EMBED_SESSION }];
}

/** The embedded session a tab node runs in: its own when it still exists, else the canvas's first. */
export function embedSessionOf(canvas, node) {
  const list = canvas?.settings?.embedSessions || [DEFAULT_EMBED_SESSION];
  const id = node?.data?.embedSessionId;
  return list.find((x) => x.id === id) || list[0];
}

/** Add an embedded session (mutated settings); returns it, or null at the limit. */
export function addEmbedSession(settings, name = "") {
  const list = settings.embedSessions;
  if (list.length >= EMBED_SESSIONS_MAX) return null;
  let n = list.length + 1;
  while (list.some((x) => x.id === `nms-${n}`)) n += 1;
  const sess = { id: `nms-${n}`, name: str(name, "", 128).trim() || `Canvas Session ${n}` };
  list.push(sess);
  return sess;
}

/** Rename an embedded session; false when it does not exist or the name is empty. */
export function renameEmbedSession(settings, id, name) {
  const sess = settings.embedSessions.find((x) => x.id === id);
  const clean = str(name, "", 128).trim();
  if (!sess || !clean) return false;
  sess.name = clean;
  return true;
}

/**
 * Delete an embedded session (the last one stays): tab nodes that ran in it move to the first remaining
 * one, a canvas following it follows that one. Returns the ids of the moved tab nodes, or null.
 */
export function removeEmbedSession(canvas, id) {
  const st = canvas.settings;
  if (st.embedSessions.length <= 1 || !st.embedSessions.some((x) => x.id === id)) return null;
  st.embedSessions = st.embedSessions.filter((x) => x.id !== id);
  const fallback = st.embedSessions[0].id;
  if (st.followSession === id) st.followSession = fallback;
  const moved = [];
  for (const n of canvas.nodes) {
    if (n.type === "tab" && n.data.embedSessionId === id) {
      n.data.embedSessionId = fallback;
      moved.push(n.id);
    }
  }
  return moved;
}

/**
 * A canvas document repaired into the current schema, or null when `raw` is not one (wrong schema/version,
 * not an object). Invalid nodes and dangling edges are dropped; group references to missing or cyclic
 * groups are cleared.
 */
export function normalizeCanvas(raw, depth = 0) {
  if (!isObj(raw) || raw.schema !== CANVAS_SCHEMA || raw.version !== CANVAS_VERSION || !Array.isArray(raw.nodes)) return null;
  const nodes = [];
  const seen = new Set();
  for (const n of raw.nodes.slice(0, 5000)) {
    const node = normalizeNode(n, depth);
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
    tours: normalizeTours(raw.tours),
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
    tours: canvas.tours || [],
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
    const payload = serializeCanvas(canvas);
    payload.updated_at = Number(canvas?.updated_at) || Date.now();
    storage.setItem(key, JSON.stringify(payload));
    return true;
  } catch {
    return false;
  }
}

/**
 * Persist a canvas asynchronously to IndexedDB `USER_STORE` (`localDB.userPut`, `kind: "nodemap-canvas"`),
 * falling back to synchronous `localStorage` when IndexedDB is unavailable (e.g. unit tests or private mode).
 */
export async function saveCanvasAsync(userDb, key, canvas, meta = {}, fallbackStorage = null) {
  const data = serializeCanvas(canvas);
  if (userDb && typeof userDb.userPut === "function") {
    try {
      const now = Date.now();
      const rec = {
        key,
        kind: USER_CANVAS_KIND,
        repo: String(meta.repo || ""),
        id: String(meta.id || key),
        created_at: Number(meta.created_at) || now,
        updated_at: now,
        data,
      };
      const ok = await userDb.userPut(rec);
      if (ok) return { ok: true, backend: "indexeddb", created_at: rec.created_at };
    } catch {
      // fall back to localStorage
    }
  }
  const ok = saveCanvas(fallbackStorage, key, data);
  return { ok, backend: "localStorage", created_at: meta.created_at || null };
}

/**
 * Load a canvas asynchronously from IndexedDB `USER_STORE` (`localDB.userGet`), falling back to
 * `localStorage` when absent or unavailable.
 */
export async function loadCanvasAsync(userDb, key, fallbackStorage = null) {
  if (userDb && typeof userDb.userGet === "function") {
    try {
      const rec = await userDb.userGet(key);
      if (rec?.data) {
        const canvas = normalizeCanvas(rec.data);
        if (canvas) return { canvas, created_at: rec.created_at || null, backend: "indexeddb" };
      }
    } catch {
      // fall back to localStorage
    }
  }
  const canvas = loadCanvas(fallbackStorage, key);
  return { canvas, created_at: null, backend: "localStorage" };
}

// ------------------------------------------------------------------ Sub-Map Portals

/** Convert a `group` node into a Sub-Map portal (creates an empty nested child canvas if absent). */
export function convertGroupToSubMap(groupNode) {
  if (!groupNode || groupNode.type !== "group") return null;
  if (!isObj(groupNode.data)) groupNode.data = {};
  if (!groupNode.data.subCanvas) {
    groupNode.data.subCanvas = emptyCanvas();
    if (!groupNode.customTitle && groupNode.title === "Group") groupNode.title = "Sub-Map";
  }
  return groupNode.data.subCanvas;
}

/**
 * Resolve a breadcrumb path of `group` portal ids `[g1, g2, ...]` from `rootCanvas`:
 * returns `{ canvas, trail: [{ id, title, groupNode }] }`. Invalid ids stop traversal cleanly.
 */
export function resolveSubMapAtPath(rootCanvas, pathIds = []) {
  let cur = rootCanvas;
  const trail = [];
  for (const id of Array.isArray(pathIds) ? pathIds : []) {
    const grp = cur?.nodes?.find((n) => n.id === id && n.type === "group");
    if (!grp?.data?.subCanvas) break;
    trail.push({ id: grp.id, title: displayTitle(grp), groupNode: grp });
    cur = grp.data.subCanvas;
  }
  return { canvas: cur, trail };
}

// ------------------------------------------------------------------ Focus / Trace BFS

/**
 * Connected subgraph reachable from `seedNodeIds` within `maxHops` undirected edge hops.
 * Returns `{ nodeIds: Set<string>, edgeIds: Set<string>, distances: Map<string, number> }`.
 */
export function connectedSubgraph(canvas, seedNodeIds, maxHops = 1) {
  const limit = maxHops === Infinity || maxHops < 0 ? Infinity : Math.max(0, Math.floor(Number(maxHops) || 0));
  const seeds = [...(seedNodeIds || [])].filter(Boolean);
  const nodeIds = new Set();
  const edgeIds = new Set();
  const distances = new Map();
  if (!canvas || !seeds.length) return { nodeIds, edgeIds, distances };

  const adj = new Map();
  for (const e of canvas.edges || []) {
    if (!adj.has(e.from)) adj.set(e.from, []);
    if (!adj.has(e.to)) adj.set(e.to, []);
    adj.get(e.from).push({ other: e.to, edgeId: e.id });
    adj.get(e.to).push({ other: e.from, edgeId: e.id });
  }

  const validIds = new Set((canvas.nodes || []).map((n) => n.id));
  const queue = [];
  for (const id of seeds) {
    if (!validIds.has(id) || nodeIds.has(id)) continue;
    nodeIds.add(id);
    distances.set(id, 0);
    queue.push(id);
  }

  let head = 0;
  while (head < queue.length) {
    const cur = queue[head++];
    const dist = distances.get(cur);
    if (dist >= limit) continue;
    for (const { other, edgeId } of adj.get(cur) || []) {
      edgeIds.add(edgeId);
      if (!nodeIds.has(other) && validIds.has(other)) {
        nodeIds.add(other);
        distances.set(other, dist + 1);
        queue.push(other);
      }
    }
  }
  // Include edges between already-discovered nodes within hop limit
  for (const e of canvas.edges || []) {
    if (nodeIds.has(e.from) && nodeIds.has(e.to)) {
      const da = distances.get(e.from);
      const db = distances.get(e.to);
      if (da < limit || db < limit || limit === Infinity) edgeIds.add(e.id);
    }
  }
  return { nodeIds, edgeIds, distances };
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

/** Turn auto-fit on or off (mutated); the content scale and the on-screen size stay. */
export function setAutoFit(node, on) {
  if (!AUTOFIT_TYPES.includes(node.type)) return node;
  if (on && !node.autoFit) {
    const s = node.scale || 1;
    Object.assign(node, { autoFit: true, baseW: Math.round(node.w / s), baseH: Math.round(node.h / s) });
  } else if (!on && node.autoFit) {
    const lim = SIZE_LIMITS[node.type];
    // The content keeps its scale: the outer box stays where it is
    Object.assign(node, {
      autoFit: false,
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

/** Space between a group's frame and the members an Auto-Layout arranges in it (canvas units). */
export const GROUP_LAYOUT_PAD = { x: 24, top: 44, bottom: 24 };

/**
 * What an Auto-Layout arranges for the selection `selectedIds`:
 * - one group selected: its direct members other than nested groups (`scope: "group"`, `group`);
 * - several nodes selected: the selected nodes other than groups (`scope: "selection"`);
 * - nothing or one node other than a group: every node but groups (`scope: "canvas"`).
 * `{ scope, nodes, group }`; `nodes` is empty when a group or a selection has nothing to arrange.
 */
export function layoutScope(nodes, selectedIds = []) {
  const ids = new Set(selectedIds || []);
  const sel = nodes.filter((n) => ids.has(n.id));
  if (sel.length === 1 && sel[0].type === "group") {
    const group = sel[0];
    return { scope: "group", group, nodes: nodes.filter((n) => n.groupId === group.id && n.type !== "group") };
  }
  if (sel.length > 1) return { scope: "selection", group: null, nodes: sel.filter((n) => n.type !== "group") };
  return { scope: "canvas", group: null, nodes: nodes.filter((n) => n.type !== "group") };
}

/**
 * Move laid-out `positions` (`[{id, x, y}]`, any origin) so their bounding box starts at `origin`
 * (`{x, y}`): a subset arranged in place keeps the top-left corner it had. Returns new objects.
 */
export function anchorPositions(positions, nodes, origin) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const list = (positions || []).filter((p) => p && byId.has(p.id));
  if (!list.length || !origin) return list.map((p) => ({ ...p }));
  let minX = Infinity, minY = Infinity;
  for (const p of list) {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
  }
  const dx = Math.round(origin.x - minX);
  const dy = Math.round(origin.y - minY);
  return list.map((p) => ({ id: p.id, x: Math.round(p.x + dx), y: Math.round(p.y + dy) }));
}

/**
 * Grow or shrink group `group` (mutated; its top-left corner stays) so `members` fit inside its frame
 * with `GROUP_LAYOUT_PAD` around them, within the group size limits.
 */
export function fitGroupToMembers(group, members, pad = GROUP_LAYOUT_PAD) {
  const b = boundsOf(members);
  if (!group || !b) return group;
  const lim = SIZE_LIMITS.group;
  group.w = Math.round(clamp(b.x + b.w + pad.x - group.x, lim.minW, lim.maxW));
  group.h = Math.round(clamp(b.y + b.h + pad.bottom - group.y, lim.minH, lim.maxH));
  return group;
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

/**
 * URL of the isolated embed shell for a tab hash: the app with `?embed=1` (no chrome, no workspace
 * session), joined to the NodeMap embedded session `embedSession` (`&embedSession=<id>`) and reporting to
 * node `nodeId` (`&embedNode=<id>`) when given.
 */
export function embedSrc(hash, base = "/", embedSession = "", nodeId = "") {
  // Key of the session: `<nodemap tab id>:<session id>` (utils/embed_mode.js EMBED_SESSION_RE)
  const sess = embedSession && /^[\w.:-]{1,160}$/.test(embedSession) ? `&embedSession=${encodeURIComponent(embedSession)}` : "";
  // The node the shell reports its state to (`&embedNode=<id>`, utils/embed_mode.js EMBED_MSG)
  const node = nodeId && EMBED_NODE_RE.test(nodeId) ? `&embedNode=${encodeURIComponent(nodeId)}` : "";
  return `${base}?embed=1${sess}${node}#${String(hash || "").replace(/^#/, "")}`;
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

// ------------------------------------------------------------------ undo / redo (structural sharing)

/** Settings that are view state, not document: undo never flips them. */
const VIEW_SETTINGS = ["editMode", "snap"];

/** Comparable snapshot (JSON text) of what undo restores: nodes, edges, tours and document settings. */
export function historySnapshot(canvas) {
  const settings = { ...(canvas?.settings || {}) };
  for (const k of VIEW_SETTINGS) delete settings[k];
  return JSON.stringify({
    nodes: canvas?.nodes || [],
    edges: canvas?.edges || [],
    tours: canvas?.tours || [],
    settings,
  });
}

/**
 * Lightweight structural snapshot sharing unchanged serialized node/edge entries with `prev` (a prior
 * structural snapshot), eliminating redundant full-canvas JSON duplication across the undo stack.
 */
export function structuralSnapshot(canvas, prev = null) {
  const settings = { ...(canvas?.settings || {}) };
  for (const k of VIEW_SETTINGS) delete settings[k];
  const settingsJson = JSON.stringify(settings);
  const toursJson = JSON.stringify(canvas?.tours || []);

  const prevNodes = prev?.__structural ? prev.nodeMap : null;
  const nodeMap = new Map();
  const nodeOrder = [];
  for (const n of canvas?.nodes || []) {
    const js = JSON.stringify(n);
    const prevJs = prevNodes?.get(n.id);
    // Share the existing immutable string reference whenever the serialized item matches
    nodeMap.set(n.id, prevJs === js ? prevJs : js);
    nodeOrder.push(n.id);
  }

  const prevEdges = prev?.__structural ? prev.edgeMap : null;
  const edgeMap = new Map();
  const edgeOrder = [];
  for (const e of canvas?.edges || []) {
    const js = JSON.stringify(e);
    const prevJs = prevEdges?.get(e.id);
    edgeMap.set(e.id, prevJs === js ? prevJs : js);
    edgeOrder.push(e.id);
  }

  const sameSettings = prev?.__structural && prev.settingsJson === settingsJson && prev.toursJson === toursJson;
  return {
    __structural: true,
    nodeMap,
    nodeOrder,
    edgeMap,
    edgeOrder,
    settingsJson: sameSettings ? prev.settingsJson : settingsJson,
    toursJson: sameSettings ? prev.toursJson : toursJson,
  };
}

/** Materialize a structural (or string) snapshot into `{ nodes, edges, tours, settings }`. */
export function materializeSnapshot(snap) {
  if (!snap) return null;
  if (typeof snap === "string") return JSON.parse(snap);
  if (snap.__structural) {
    return {
      nodes: snap.nodeOrder.map((id) => JSON.parse(snap.nodeMap.get(id))),
      edges: snap.edgeOrder.map((id) => JSON.parse(snap.edgeMap.get(id))),
      tours: JSON.parse(snap.toursJson || "[]"),
      settings: JSON.parse(snap.settingsJson || "{}"),
    };
  }
  return snap;
}

function snapshotsEqual(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a === "string" || typeof b === "string") return false;
  if (!a.__structural || !b.__structural) return false;
  if (a.settingsJson !== b.settingsJson || a.toursJson !== b.toursJson) return false;
  if (a.nodeOrder.length !== b.nodeOrder.length || a.edgeOrder.length !== b.edgeOrder.length) return false;
  for (let i = 0; i < a.nodeOrder.length; i++) {
    const id = a.nodeOrder[i];
    if (id !== b.nodeOrder[i] || a.nodeMap.get(id) !== b.nodeMap.get(id)) return false;
  }
  for (let i = 0; i < a.edgeOrder.length; i++) {
    const id = a.edgeOrder[i];
    if (id !== b.edgeOrder[i] || a.edgeMap.get(id) !== b.edgeMap.get(id)) return false;
  }
  return true;
}

/**
 * Bounded undo / redo history of snapshots (string or structural-sharing). `base` is the document as last
 * seen; `record(snap)` pushes the previous base when the document changed (no-op snapshots are not pushed)
 * and clears the redo stack; consecutive records with the same `coalesce` key fold into one step.
 * `absorb(snap)` takes a non-user change into the base.
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
    if (snapshotsEqual(snap, this.base)) {
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
    if (current !== undefined && current !== null && !snapshotsEqual(current, this.base)) this.record(current);
    if (!this.undoList.length) return null;
    this.redoList.push(this.base);
    this.base = this.undoList.pop();
    this.lastKey = null;
    return this.base;
  }

  /** Snapshot to restore (the step undone last), or null; a change since the undo drops the redo stack. */
  redo(current) {
    if (current !== undefined && current !== null && !snapshotsEqual(current, this.base)) {
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
