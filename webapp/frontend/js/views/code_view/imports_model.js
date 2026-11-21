/**
 * imports_model.js - "Imports" panel logic of the code view: symbols a C/header/assembly file uses that are
 * defined in other headers, with the include chain that reaches them. Pure and DOM-free
 * (webapp/frontend/tests/imports_model.test.mjs).
 *
 * Items come from GET /api/fs/imports/{version}?path=&arch= ({name, kind, type_name, header_path, line, chain,
 * forced, via, context, ref_line, ref_lines, ref_count, alternatives}). When `forced` is true, `chain[0]` is the
 * compiler-forced `-include` header; for a context import (`via: "context"`) `chain[0]` is the file including the
 * current one (`context`); otherwise `chain[0]` is the current file. The last element is `header_path`.
 */

/** File extensions (case-sensitive) for which the imports panel is available. */
export const IMPORTS_EXTENSIONS = [".c", ".h", ".S", ".s"];

/** True when `path` ends with an imports-eligible extension and has a non-empty basename stem. */
export function isImportsPath(path) {
  if (!path || typeof path !== "string") return false;
  const base = basename(path);
  return IMPORTS_EXTENSIONS.some((ext) => base.length > ext.length && base.endsWith(ext));
}

/** Ordered pill filters. */
export const IMPORT_FILTERS = [
  { id: "all", label: "All" },
  { id: "function", label: "Functions" },
  { id: "struct", label: "Structs" },
  { id: "type", label: "Types" },
  { id: "macro", label: "Macros" },
  { id: "variable", label: "Variables" },
  { id: "config", label: "Config" },
];

/** Pill id of an item kind ("prototype" folds into "function"; unknown kinds into "variable"). */
export function filterIdOf(kind) {
  if (kind === "prototype") return "function";
  if (kind === "function" || kind === "struct" || kind === "type" || kind === "macro" || kind === "config") return kind;
  return "variable";
}

/** Icon glyph per item kind. */
export const KIND_ICONS = {
  function: "ƒ", prototype: "ƒ", struct: "{}", type: "T", macro: "#", variable: "x", config: "⚙",
};

/** Icon glyph for `kind` ("•" when unknown). */
export function kindIcon(kind) {
  return Object.hasOwn(KIND_ICONS, kind) ? KIND_ICONS[kind] : "•";
}

const KIND_LABELS = {
  function: "Function", prototype: "Prototype", struct: "Struct", type: "Type", macro: "Macro", variable: "Variable", config: "Config",
};

/** Human label for `kind` (the kind string itself when unknown). */
export function kindLabel(kind) {
  return Object.hasOwn(KIND_LABELS, kind) ? KIND_LABELS[kind] : kind;
}

/** Case-insensitive substring match of the trimmed `query` against name, header path or any chain path. */
export function matchesQuery(item, query) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return true;
  if (!item) return false;
  if ((item.name || "").toLowerCase().includes(q)) return true;
  if ((item.header_path || "").toLowerCase().includes(q)) return true;
  return (item.chain || []).some((p) => String(p).toLowerCase().includes(q));
}

/** New array of items passing both the pill filter (`kind`) and the search `query`. */
export function filterImports(items, { query = "", kind = "all" } = {}) {
  return (items || []).filter((it) => it && (kind === "all" || filterIdOf(it.kind) === kind) && matchesQuery(it, query));
}

/** Count per IMPORT_FILTERS id (`all` included) of the items matching `query`. */
export function countByFilter(items, query = "") {
  const counts = {};
  for (const f of IMPORT_FILTERS) counts[f.id] = 0;
  for (const it of items || []) {
    if (!it || !matchesQuery(it, query)) continue;
    counts.all += 1;
    counts[filterIdOf(it.kind)] += 1;
  }
  return counts;
}

const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

function compareItems(a, b) {
  return cmp(a.name || "", b.name || "") || cmp(a.header_path || "", b.header_path || "") || (a.line || 0) - (b.line || 0);
}

/** Groups `items` into `{key, label, items}` by "kind" (pill order) or "header" (chain length, then path). */
export function groupImports(items, by = "kind") {
  const list = (items || []).filter(Boolean);
  const map = new Map();
  const chainLen = new Map();
  for (const it of list) {
    const key = by === "header" ? it.header_path || "" : filterIdOf(it.kind);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(it);
    if (by === "header") {
      const n = (it.chain || []).length;
      chainLen.set(key, Math.min(chainLen.get(key) ?? Infinity, n));
    }
  }
  let groups;
  if (by === "header") {
    groups = [...map.keys()]
      .sort((a, b) => chainLen.get(a) - chainLen.get(b) || cmp(a, b))
      .map((key) => ({ key, label: key, items: map.get(key) }));
  } else {
    groups = IMPORT_FILTERS.filter((f) => f.id !== "all" && map.has(f.id))
      .map((f) => ({ key: f.id, label: f.label, items: map.get(f.id) }));
  }
  for (const g of groups) g.items = [...g.items].sort(compareItems);
  return groups;
}

/** Last path segment. */
export function basename(path) {
  const s = String(path || "");
  return s.slice(s.lastIndexOf("/") + 1);
}

/** One `{path, label, index, isTarget, isRoot}` per chain element (empty when no chain). */
export function chainSegments(item) {
  const chain = (item && item.chain) || [];
  return chain.map((path, index) => ({
    path, label: basename(path), index, isTarget: index === chain.length - 1, isRoot: index === 0,
  }));
}

/** Chain as text (basenames, or full paths when `full`); forced includes get an "-include " prefix, context
 * imports (defined by what the including file includes first) a "context " prefix, and Kconfig symbols
 * a "kconfig " prefix. */
export function formatChain(item, { arrow = " → ", full = false } = {}) {
  const chain = (item && item.chain) || [];
  if (!chain.length) return "";
  const text = chain.map((p) => (full ? p : basename(p))).join(arrow);
  const forced = item.forced ? `-include ${text}` : text;
  if (item.via === "context") return `context ${forced}`;
  if (item.via === "kconfig") return `kconfig ${forced}`;
  return forced;
}

/** Message shown instead of the list, or null when the list should render (or is still loading). */
export function emptyStateMessage(payload, { path } = {}) {
  if (!isImportsPath(path)) return "Imports are available for C, header and assembly files.";
  if (!payload) return null;
  if (payload.indexed === false) return "This revision's file is not indexed; imported symbols are unavailable.";
  if (payload.layout === false && (!payload.items || !payload.items.length)) {
    return `No build layout for ARCH=${payload.arch} at this revision.`;
  }
  if (!payload.items || !payload.items.length) return "No imported symbols referenced in this file.";
  return null;
}
