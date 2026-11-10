/**
 * grep_format.js - Formatting of full-text search results (GET /api/fs/grep, webapp/API.md §4.2, §5.34).
 *
 * Pure and DOM-free (webapp/frontend/tests/grep_format.test.mjs).
 */

export function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * HTML of `text` with `ranges` ([[start, end], ...], character offsets in the raw text) wrapped in
 * <mark>. Each segment is escaped on its own, so offsets always refer to the raw text.
 */
export function highlightRanges(text, ranges, cls = "grep-hit") {
  const t = String(text ?? "");
  const sorted = (ranges || [])
    .map(([a, b]) => [Math.max(0, a | 0), Math.min(t.length, b | 0)])
    .filter(([a, b]) => b > a)
    .sort((x, y) => x[0] - y[0]);
  let out = "";
  let pos = 0;
  for (const [a, b] of sorted) {
    if (a < pos) continue; // overlapping ranges: keep the first
    out += escapeHtml(t.slice(pos, a));
    out += `<mark class="${cls}">${escapeHtml(t.slice(a, b))}</mark>`;
    pos = b;
  }
  return out + escapeHtml(t.slice(pos));
}

/** Matches grouped by file, in result order: [{path, matches}]. */
export function groupByFile(matches) {
  const out = [];
  const byPath = new Map();
  for (const m of matches || []) {
    let g = byPath.get(m.path);
    if (!g) {
      g = { path: m.path, matches: [] };
      byPath.set(m.path, g);
      out.push(g);
    }
    g.matches.push(m);
  }
  return out;
}

/** Plain-text export: one `path:line:text` per match. */
export function grepToText(result) {
  return (result?.matches || []).map((m) => `${m.path}:${m.line}:${m.text}`).join("\n") + "\n";
}

/** Query string of a search: {q, regex, caseSensitive, word, paths, limit}. */
export function grepParams({ q, regex = false, caseSensitive = false, word = false, paths = [], limit = 0, perFile = 0 }) {
  const params = new URLSearchParams({ q: q || "" });
  if (regex) params.set("mode", "regex");
  if (caseSensitive) params.set("case", "1");
  if (word) params.set("word", "1");
  for (const p of paths || []) if (p) params.append("path", p);
  if (limit) params.set("limit", String(limit));
  if (perFile) params.set("per_file", String(perFile));
  return params;
}

/** Split a path-filter text ("drivers/net/, !*.h") into specs. */
export function parsePathFilters(text) {
  return String(text || "").split(/[,\s]+/).map((s) => s.trim()).filter(Boolean).slice(0, 8);
}

/** One-line summary: "123 matches in 45 files (truncated)". */
export function grepSummary(result) {
  if (!result) return "";
  const n = result.total || 0;
  const f = result.files || 0;
  let s = `${n.toLocaleString("en-US")} match${n === 1 ? "" : "es"} in ${f.toLocaleString("en-US")} file${f === 1 ? "" : "s"}`;
  if (result.timed_out) s += " · timed out (partial)";
  else if (result.truncated) s += " · truncated: narrow the search with a path filter";
  return s;
}
