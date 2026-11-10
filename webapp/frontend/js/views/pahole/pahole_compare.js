/**
 * pahole_compare.js - Struct layout over time (N3, webapp/API.md §4.7): the two-revision comparison table and the
 * size history chart of the Pahole view. DOM-free (HTML strings), tested.
 */
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const signed = (n) => (n > 0 ? `+${n}` : String(n));

/** "+8 bytes, −1 hole, cachelines 3 → 4" */
export function deltaText(res) {
  const d = res.delta || {};
  const parts = [`${signed(d.total_size || 0)} bytes`];
  if (d.holes_count) parts.push(`${signed(d.holes_count)} hole${Math.abs(d.holes_count) === 1 ? "" : "s"}`);
  if (d.cache_lines_used) parts.push(`cachelines ${res.a.cache_lines_used} → ${res.b.cache_lines_used}`);
  return parts.join(", ").replace(/-/g, "−");
}

export function compareHtml(res, a, b) {
  const cell = (s) => (s ? `<td>${s.offset}</td><td>${s.size}</td><td><code>${esc(s.type)}</code></td>` : `<td></td><td></td><td></td>`);
  const rows = res.members.map((m) => `<tr class="pcmp-${m.status}"><td><code>${esc(m.name)}</code>${m.renamed_to ? ` <span class="kconfig-muted">renamed? → ${esc(m.renamed_to)}</span>` : ""}${m.renamed_from ? ` <span class="kconfig-muted">renamed? ← ${esc(m.renamed_from)}</span>` : ""}</td>
    <td class="pcmp-status">${esc(m.status.replace("_", " "))}</td>${cell(m.a)}${cell(m.b)}</tr>`).join("");
  const counts = Object.entries(res.counts || {}).filter(([k]) => k !== "same").map(([k, n]) => `${n} ${k.replace("_", " ")}`).join(", ") || "no member changed";
  return `<div class="pcmp-summary"><strong>${esc(a)}</strong> ${res.a.total_size} bytes → <strong>${esc(b)}</strong> ${res.b.total_size} bytes: ${esc(deltaText(res))} · ${esc(counts)}${res.is_estimate ? ' · <span class="kconfig-muted">estimated (some members lack compiler attributes)</span>' : ""}</div>
    <div class="pcmp-scroll"><table class="pcmp-table"><thead><tr><th>Member</th><th>Status</th><th colspan="3">${esc(a)}: offset · size · type</th><th colspan="3">${esc(b)}: offset · size · type</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

/** Line chart of total_size over the history stops (holes as bars, estimates hollow, unavailable stops as gaps). */
export function historySvg(stops, { width = 640, height = 170 } = {}) {
  const pts = stops.map((s, i) => ({ ...s, i })).filter((s) => s.available);
  if (!pts.length) return "";
  const max = Math.max(...pts.map((p) => p.total_size), 1);
  const maxHoles = Math.max(...pts.map((p) => p.holes_count || 0), 1);
  const padL = 44;
  const padB = 30;
  const w = width - padL - 10;
  const h = height - padB - 10;
  const x = (i) => padL + (stops.length === 1 ? w / 2 : (i / (stops.length - 1)) * w);
  const y = (v) => 10 + h - (v / max) * h;
  const holes = pts.map((p) => {
    const bh = ((p.holes_count || 0) / maxHoles) * (h * 0.3);
    return `<rect x="${(x(p.i) - 3).toFixed(1)}" y="${(10 + h - bh).toFixed(1)}" width="6" height="${bh.toFixed(1)}" fill="var(--accent-yellow)" opacity="0.5"><title>${esc(p.version)}: ${p.holes_count} holes</title></rect>`;
  }).join("");
  const line = pts.map((p, k) => `${k ? "L" : "M"}${x(p.i).toFixed(1)},${y(p.total_size).toFixed(1)}`).join(" ");
  const dots = pts.map((p) => `<circle data-version="${esc(p.version)}" cx="${x(p.i).toFixed(1)}" cy="${y(p.total_size).toFixed(1)}" r="4" fill="${p.is_estimate ? "var(--bg-primary)" : "var(--accent-blue)"}" stroke="var(--accent-blue)" stroke-width="2" style="cursor:pointer"><title>${esc(p.version)}: ${p.total_size} bytes, ${p.holes_count} holes, ${p.cache_lines_used} cachelines${p.is_estimate ? " (estimate)" : ""}</title></circle>`).join("");
  const labels = stops.map((s, i) => `<text x="${x(i).toFixed(1)}" y="${height - 8}" font-size="9" fill="${s.available ? "var(--text-muted)" : "var(--accent-red)"}" text-anchor="middle">${esc(s.version)}</text>`).join("");
  const axis = `<line x1="${padL}" x2="${padL + w}" y1="${10 + h}" y2="${10 + h}" stroke="var(--border-color)"/><text x="${padL - 4}" y="16" font-size="10" fill="var(--text-muted)" text-anchor="end">${max} B</text>`;
  return `<svg class="pcmp-chart" viewBox="0 0 ${width} ${height}" width="100%" role="img" aria-label="Size history">${axis}${holes}<path d="${line}" fill="none" stroke="var(--accent-blue)" stroke-width="2"/>${dots}${labels}</svg>`;
}
