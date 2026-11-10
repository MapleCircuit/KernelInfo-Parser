/**
 * outline_model.js - Outline, sticky scroll and "go to symbol" logic of the code view (G03,
 * webapp/API.md §5.21). Pure and DOM-free (webapp/frontend/tests/outline_model.test.mjs).
 *
 * Items come from GET /api/fs/outline ({name, kind, type_name, line_s, line_e, linkage, children}),
 * or, for Kconfig files (whose entries are not symbol definitions), from `kconfigOutline()`.
 */

export const OUTLINE_GROUPS = [
  ["function", "Functions"], ["type", "Types"], ["macro", "Macros"], ["variable", "Variables"],
  ["api", "Kernel API"], ["kconfig", "Kconfig"], ["constant", "Constants"], ["prototype", "Declarations"],
  ["label", "Labels"], ["other", "Other"],
];

export const KIND_ICONS = {
  function: "ƒ", prototype: "ƒ", type: "𝐓", macro: "#", variable: "𝑥", api: "⇪", kconfig: "⚙",
  constant: "≡", label: "⌖", other: "•",
};

/** Depth-first list of items (children after their parent), each with its `depth`. */
export function flattenOutline(items, depth = 0, out = []) {
  for (const it of items || []) {
    out.push({ ...it, depth });
    flattenOutline(it.children, depth + 1, out);
  }
  return out;
}

/** Items whose name contains `query` (case-insensitive), with their ancestors so the tree reads. */
export function filterOutline(items, query) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return items || [];
  const walk = (list) => {
    const out = [];
    for (const it of list || []) {
      const kids = walk(it.children);
      if (it.name.toLowerCase().includes(q) || kids.length) out.push({ ...it, children: kids });
    }
    return out;
  };
  return walk(items);
}

/** Items grouped for display: [{kind, title, items}] in OUTLINE_GROUPS order (top-level items only). */
export function groupOutline(items) {
  const byKind = new Map();
  for (const it of items || []) {
    if (!byKind.has(it.kind)) byKind.set(it.kind, []);
    byKind.get(it.kind).push(it);
  }
  return OUTLINE_GROUPS.filter(([k]) => byKind.has(k)).map(([kind, title]) => ({ kind, title, items: byKind.get(kind) }));
}

/** Ranges that can pin a sticky header: definitions spanning several lines, sorted by start. */
export function stickyRanges(items) {
  return flattenOutline(items)
    .filter((it) => it.line_e > it.line_s && ["function", "type", "kconfig"].includes(it.kind))
    .map((it) => ({ name: it.name, kind: it.kind, line_s: it.line_s, line_e: it.line_e, depth: it.depth }))
    .sort((a, b) => a.line_s - b.line_s || b.line_e - a.line_e);
}

/**
 * Definitions containing `line` (outermost first): the scopes a reader of that line is inside.
 * `ranges` sorted by line_s (stickyRanges); binary search for the last start <= line, then a scan back.
 */
export function containingRanges(ranges, line) {
  let lo = 0;
  let hi = ranges.length - 1;
  let last = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (ranges[mid].line_s <= line) {
      last = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  const out = [];
  for (let i = last; i >= 0; i--) {
    const r = ranges[i];
    if (r.line_s <= line && line <= r.line_e) out.push(r);
  }
  return out.sort((a, b) => a.line_s - b.line_s || b.line_e - a.line_e);
}

/**
 * Sticky header rows for a viewport whose first visible line is `topLine`: the definitions that
 * contain it and start above it, innermost `maxLevels` of them. Each pinned row hides the line under
 * it, so the containment test uses the line just below the pinned rows.
 */
export function stickyLines(ranges, topLine, maxLevels = 2) {
  let pinned = [];
  for (let level = 0; level <= maxLevels; level++) {
    const probe = topLine + pinned.length;
    const scopes = containingRanges(ranges, probe).filter((r) => r.line_s < probe);
    const next = scopes.slice(-maxLevels);
    if (next.length === pinned.length && next.every((r, i) => r === pinned[i])) break;
    pinned = next;
  }
  return pinned;
}

/** Innermost definition containing `line` (for the breadcrumb and the outline highlight), or null. */
export function currentDefinition(ranges, line) {
  const scopes = containingRanges(ranges, line);
  return scopes.length ? scopes[scopes.length - 1] : null;
}

/** Best matches of `query` among the outline's items for the palette's ":name" mode. */
export function matchSymbols(items, query, limit = 30) {
  const q = String(query || "").trim().toLowerCase();
  const all = flattenOutline(items).filter((it) => it.kind !== "prototype" || !q);
  if (!q) return all.slice(0, limit);
  const scored = [];
  for (const it of all) {
    const n = it.name.toLowerCase();
    let score = -1;
    if (n === q) score = 0;
    else if (n.startsWith(q)) score = 1;
    else if (n.includes(q)) score = 2;
    if (score >= 0) scored.push([score, n.length, it.line_s, it]);
  }
  scored.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]);
  return scored.slice(0, limit).map((s) => s[3]);
}

/**
 * Outline of a Kconfig file from its lines: config / menuconfig / choice entries, nested in their
 * menu ... endmenu, choice ... endchoice and if ... endif blocks (line ranges included).
 */
export function kconfigOutline(lines) {
  const roots = [];
  const stack = [];
  const push = (item) => (stack.length ? stack[stack.length - 1].children : roots).push(item);
  let lastEntry = null;
  (lines || []).forEach((raw, idx) => {
    const line = idx + 1;
    const text = raw.replace(/#.*$/, "").trim();
    let m;
    if ((m = /^(menuconfig|config)\s+([A-Za-z0-9_]+)/.exec(text))) {
      lastEntry = { name: m[2], kind: "kconfig", type_name: m[1] === "config" ? "Kconfig_Config" : "Kconfig_Menuconfig", line_s: line, line_e: line, linkage: null, children: [] };
      push(lastEntry);
    } else if ((m = /^(menu|mainmenu)\s+"(.*)"/.exec(text))) {
      const item = { name: m[2], kind: "kconfig", type_name: m[1] === "menu" ? "Kconfig_Menu" : "Kconfig_Mainmenu", line_s: line, line_e: line, linkage: null, children: [] };
      push(item);
      if (m[1] === "menu") stack.push(item);
      lastEntry = null;
    } else if (/^choice\b/.test(text)) {
      const item = { name: "choice", kind: "kconfig", type_name: "Kconfig_Choice", line_s: line, line_e: line, linkage: null, children: [] };
      push(item);
      stack.push(item);
      lastEntry = item;
    } else if ((m = /^if\s+(.+)$/.exec(text))) {
      const item = { name: `if ${m[1]}`, kind: "kconfig", type_name: "Kconfig_If", line_s: line, line_e: line, linkage: null, children: [] };
      push(item);
      stack.push(item);
      lastEntry = null;
    } else if (/^(endmenu|endchoice|endif)\b/.test(text)) {
      const kind = { endmenu: "Kconfig_Menu", endchoice: "Kconfig_Choice", endif: "Kconfig_If" }[text.split(/\s/)[0]];
      for (let i = stack.length - 1; i >= 0; i--) {
        if (stack[i].type_name === kind) {
          stack[i].line_e = line;
          stack.splice(i);
          break;
        }
      }
      lastEntry = null;
    } else if (/^prompt\s+"(.*)"/.test(text) && lastEntry?.type_name === "Kconfig_Choice") {
      lastEntry.name = `choice: ${/^prompt\s+"(.*)"/.exec(text)[1]}`;
    } else if (lastEntry && text) {
      lastEntry.line_e = line;
    }
  });
  return roots;
}

export function isKconfigPath(path) {
  return /(^|\/)Kconfig[^/]*$/.test(String(path || ""));
}
