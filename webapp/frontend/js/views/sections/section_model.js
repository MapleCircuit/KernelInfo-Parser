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

/** Text a row is searched by: the section's rowText, else its string fields. */
export function rowSearchText(section, row) {
  if (section.rowText) return String(section.rowText(row) || "").toLowerCase();
  return Object.values(row || {}).filter((v) => typeof v === "string" || typeof v === "number").join(" ").toLowerCase();
}

/**
 * Rows matching the query (every word, case-insensitive) and every active chip. Chips are
 * {id, label, test(row)}; a chip without `test` is a server-side filter and is not applied here.
 */
export function filterRows(section, rows, query, chips) {
  const words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  const active = (section.chips || []).filter((c) => chips?.has(c.id) && typeof c.test === "function");
  if (!words.length && !active.length) return rows;
  return rows.filter((r) => {
    if (active.some((c) => !c.test(r))) return false;
    if (!words.length) return true;
    const hay = rowSearchText(section, r);
    return words.every((w) => hay.includes(w));
  });
}

/** Counts per chip over `rows` (what each chip would leave). */
export function chipCounts(section, rows) {
  const out = {};
  for (const c of section.chips || []) {
    if (typeof c.test === "function") out[c.id] = rows.reduce((n, r) => n + (c.test(r) ? 1 : 0), 0);
  }
  return out;
}

/** Tab fields of a sectioned tab from hash parameters, and back (routes.js). */
export function sectionStateFromParams(params) {
  return {
    view: params.get("view") || "",
    query: params.get("q") || "",
    filter: params.get("f") || "",
    sel: params.get("sel") || "",
    arch: params.get("arch") || "",
    release: params.get("r") || "",
    window: parseInt(params.get("w") || "0", 10) || 0,
    listCollapsed: params.get("lc") === "1",
  };
}

export function sectionParamsFromState(tab, params) {
  if (tab.view) params.set("view", tab.view);
  if (tab.release) params.set("r", tab.release);
  if (tab.arch) params.set("arch", tab.arch);
  if (tab.window) params.set("w", String(tab.window));
  if (tab.query) params.set("q", tab.query);
  if (tab.filter) params.set("f", tab.filter);
  if (tab.sel) params.set("sel", tab.sel);
  if (tab.listCollapsed) params.set("lc", "1");
}

/** TSV export of rows for given columns [[header, row => value]]. */
export function toTsv(rows, columns) {
  const clean = (v) => String(v ?? "").replace(/[\t\r\n]+/g, " ");
  return [columns.map(([h]) => h).join("\t"), ...rows.map((r) => columns.map(([, f]) => clean(f(r))).join("\t"))].join("\n") + "\n";
}
