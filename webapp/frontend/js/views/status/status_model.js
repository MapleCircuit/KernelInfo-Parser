/**
 * status_model.js - Formatting for the parser status tab and the settings line (G11, webapp/API.md §5.37).
 * Pure and DOM-free (webapp/frontend/tests/status_model.test.mjs).
 */

/** "2 min ago", "3 h ago", "just now" for a unix time (seconds), relative to `now` (ms). */
export function ago(unixSeconds, now = Date.now()) {
  if (!unixSeconds) return "";
  const s = Math.max(0, Math.round(now / 1000 - unixSeconds));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/** "41.3 GB" / "512 MB" from megabytes. */
export function formatMb(mb) {
  const v = Number(mb) || 0;
  if (v >= 1024) return `${(v / 1024).toFixed(1)} GB`;
  return `${v.toFixed(v >= 10 ? 0 : 1)} MB`;
}

/** "1 h 02 min", "3 min", "45 s" from seconds. */
export function formatDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${String(m % 60).padStart(2, "0")} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

/** One-line summary for the settings popover: "312 parsed · 1 parsing (v3.2-rc4, 2 min ago) · DB 41.3 GB". */
export function statusLine(status, now = Date.now()) {
  if (!status) return "";
  if (status.available === false) return "Database unavailable";
  const c = status.catalog || {};
  const parts = [`${(c.parsed || 0).toLocaleString("en-US")} parsed`];
  const parsing = status.parsing || [];
  if (parsing.length) {
    const first = parsing[0];
    const stale = parsing.filter((p) => p.stale).length;
    parts.push(`${parsing.length} parsing (${first.version}, ${ago(first.updated_at, now)}${stale ? `, ${stale} stale` : ""})`);
  } else {
    parts.push("idle");
  }
  if (status.db?.total_mb) parts.push(`DB ${formatMb(status.db.total_mb)}`);
  return parts.join(" · ");
}

/** Table rows sorted by a column ("name", "rows_estimate", "data_mb", "index_mb", "total"), descending for numbers. */
export function sortTables(tables, key = "total", dir = "desc") {
  const val = (t) => (key === "total" ? (t.data_mb || 0) + (t.index_mb || 0) : t[key]);
  const out = [...(tables || [])];
  out.sort((a, b) => {
    const x = val(a);
    const y = val(b);
    const cmp = typeof x === "string" ? String(x).localeCompare(String(y)) : (x || 0) - (y || 0);
    return dir === "asc" ? cmp : -cmp;
  });
  return out;
}
