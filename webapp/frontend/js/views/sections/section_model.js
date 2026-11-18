/**
 * section_model.js - Pure helpers of the sectioned tabs (Kernel API, Insights; S05, webapp/API.md §5.29).
 */

/** "a,b" <-> Set of chip ids. */
export function parseChips(text) {
  return new Set(String(text || "").split(",").map((s) => s.trim()).filter(Boolean));
}
export function formatChips(set) {
  return [...(set || [])].sort().join(",");
}

export const CVSS_OPS = ["ge", "gt", "lt", "eq"];
export const CVSS_OP_LABEL = { ge: "≥", gt: ">", lt: "<", eq: "=" };
const _CVSS_OP_ALIASES = {
  "<": "lt", lt: "lt",
  ">": "gt", gt: "gt",
  "=": "eq", "==": "eq", eq: "eq",
  ">=": "ge", "≥": "ge", ge: "ge",
};

/** Normalize a CVSS comparison operator; "" for empty/unknown. */
export function normalizeCvssOp(op) {
  const raw = String(op ?? "").trim().toLowerCase();
  return _CVSS_OP_ALIASES[raw] || "";
}

/** Clamp a finite numeric CVSS score into [0.0, 10.0] at 1-decimal precision; null if invalid. */
export function clampCvssScore(val) {
  if (val === null || val === undefined || val === "" || typeof val === "boolean") return null;
  const num = Number(val);
  if (!Number.isFinite(num)) return null;
  return Math.round(Math.min(10, Math.max(0, num)) * 10) / 10;
}

/** Parse a CVSS text input, returning null for incomplete keystrokes (e.g. "", "7.", "-"). */
export function parseCvssInput(raw) {
  const s = String(raw ?? "").trim();
  if (!s || s === "-" || s === "+" || s.endsWith(".") || !/^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(s)) return null;
  return clampCvssScore(Number(s));
}

/** True when `score` matches `op` against `threshold`; excludes null/unscored CVEs across all operators. */
export function matchesCvssScore(score, op, threshold) {
  if (score === null || score === undefined || typeof score === "boolean") return false;
  const s = Number(score);
  if (!Number.isFinite(s)) return false;
  const normOp = normalizeCvssOp(op) || "ge";
  const t = clampCvssScore(threshold);
  if (t === null) return true;
  if (normOp === "eq") return Math.abs(Math.round(s * 10) / 10 - t) < 0.05;
  const sRounded = Math.round(s * 10000) / 10000;
  if (normOp === "lt") return sRounded < t - 1e-9;
  if (normOp === "gt") return sRounded > t + 1e-9;
  return sRounded >= t - 1e-9;
}

/** Resolve a section's chip list (static array or dynamic `getChips(rows, ctx)`). */
export function resolveChips(section, rows = [], ctx = null) {
  if (typeof section?.getChips === "function") return section.getChips(rows, ctx) || [];
  return section?.chips || [];
}

/** Text a row is searched by: the section's rowText, else its string fields. */
export function rowSearchText(section, row) {
  if (section.rowText) return String(section.rowText(row) || "").toLowerCase();
  return Object.values(row || {}).filter((v) => typeof v === "string" || typeof v === "number").join(" ").toLowerCase();
}

/**
 * Rows matching the query (every word, case-insensitive) and every active chip. Chips are
 * {id, label, test(row, ctx), group?}; chips sharing a `group` are ORed together, while distinct
 * groups and ungrouped chips are ANDed. A chip without `test` is a server-side filter.
 */
export function filterRows(section, rows, query, chips, ctx = null) {
  const words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  const active = resolveChips(section, rows, ctx).filter((c) => chips?.has(c.id) && typeof c.test === "function");
  if (!words.length && !active.length) return rows;
  const ungrouped = active.filter((c) => !c.group);
  const grouped = new Map();
  for (const c of active) {
    if (!c.group) continue;
    if (!grouped.has(c.group)) grouped.set(c.group, []);
    grouped.get(c.group).push(c);
  }
  return rows.filter((r) => {
    if (ungrouped.some((c) => !c.test(r, ctx))) return false;
    for (const grp of grouped.values()) {
      if (!grp.some((c) => c.test(r, ctx))) return false;
    }
    if (!words.length) return true;
    const hay = rowSearchText(section, r);
    return words.every((w) => hay.includes(w));
  });
}

/** Counts per chip over `rows` (what each chip would leave). */
export function chipCounts(section, rows, ctx = null) {
  const out = {};
  for (const c of resolveChips(section, rows, ctx)) {
    if (typeof c.test === "function") out[c.id] = rows.reduce((n, r) => n + (c.test(r, ctx) ? 1 : 0), 0);
  }
  return out;
}

/** Tab fields of a sectioned tab from hash parameters, and back (routes.js). */
export function sectionStateFromParams(params) {
  const cvssOp = normalizeCvssOp(params.get("cvss_op") || "");
  const cvssScore = parseCvssInput(params.get("cvss") || "");
  return {
    view: params.get("view") || "",
    query: params.get("q") || "",
    filter: params.get("f") || "",
    sel: params.get("sel") || "",
    arch: params.get("arch") || "",
    window: parseInt(params.get("w") || "0", 10) || 0,
    listCollapsed: params.get("lc") === "1",
    ...(cvssOp ? { cvssOp } : {}),
    ...(cvssScore != null ? { cvssScore } : {}),
  };
}

export function sectionParamsFromState(tab, params) {
  if (tab.view) params.set("view", tab.view);
  if (tab.arch) params.set("arch", tab.arch);
  if (tab.window) params.set("w", String(tab.window));
  if (tab.query) params.set("q", tab.query);
  if (tab.filter) params.set("f", tab.filter);
  if (tab.cvssOp) params.set("cvss_op", tab.cvssOp);
  if (tab.cvssScore != null) params.set("cvss", String(tab.cvssScore));
  if (tab.sel) params.set("sel", tab.sel);
  if (tab.listCollapsed) params.set("lc", "1");
}

/**
 * Row a `sel` value addresses: the row whose key is `sel`, else the first whose key's last
 * `:`-segment is `sel` — so a link naming a syscall (`sel=read`) selects `common:0:read` or `:0:read`
 * without knowing its ABI and number. Null when none matches.
 */
export function findRowBySel(rows, rowKey, sel) {
  const want = String(sel ?? "");
  if (!want || !Array.isArray(rows)) return null;
  const keyOf = (r) => String(rowKey(r));
  const exact = rows.find((r) => keyOf(r) === want);
  if (exact) return exact;
  if (want.includes(":")) return null;
  return rows.find((r) => {
    const k = keyOf(r);
    return k.includes(":") && k.slice(k.lastIndexOf(":") + 1) === want;
  }) || null;
}

/** TSV export of rows for given columns [[header, row => value]]. */
export function toTsv(rows, columns) {
  const clean = (v) => String(v ?? "").replace(/[\t\r\n]+/g, " ");
  return [columns.map(([h]) => h).join("\t"), ...rows.map((r) => columns.map(([, f]) => clean(f(r))).join("\t"))].join("\n") + "\n";
}
