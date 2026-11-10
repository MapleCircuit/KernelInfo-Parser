/**
 * svg_charts.js - Hand-built SVG charts drawn with the design tokens (no chart library; webapp/API.md
 * §5.30). Strings in, SVG strings out (webapp/frontend/tests/svg_charts.test.mjs).
 */
function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/**
 * Vertical bars of `points` [{x, value, label?}] over an x domain [x0, x1]; `markers` [{x, label}] draw
 * dashed vertical lines (the -rc1 of a merge window).
 */
export function barChartSvg(points, { width = 560, height = 140, x0 = null, x1 = null, markers = [], unit = "" } = {}) {
  if (!points.length) return "";
  const xs = points.map((p) => p.x);
  const lo = x0 ?? Math.min(...xs);
  const hi = x1 ?? Math.max(...xs);
  const span = Math.max(1, hi - lo + 1);
  const max = Math.max(1, ...points.map((p) => p.value));
  const padL = 34;
  const padB = 18;
  const w = width - padL - 6;
  const h = height - padB - 6;
  const bw = Math.max(1, w / span - 1);
  const bars = points.map((p) => {
    const bh = (p.value / max) * h;
    const x = padL + ((p.x - lo) / span) * w;
    return `<rect x="${x.toFixed(1)}" y="${(6 + h - bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" fill="var(--accent-blue)"><title>${esc(p.label ?? p.x)}: ${p.value}${esc(unit)}</title></rect>`;
  }).join("");
  const marks = markers.map((m) => {
    const x = padL + ((m.x - lo) / span) * w;
    return `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="4" y2="${6 + h}" stroke="var(--accent-orange)" stroke-dasharray="3 3"/><text x="${(x + 3).toFixed(1)}" y="14" fill="var(--accent-orange)" font-size="10">${esc(m.label)}</text>`;
  }).join("");
  const axis = `<line x1="${padL}" x2="${padL + w}" y1="${6 + h}" y2="${6 + h}" stroke="var(--border-color)"/>`
    + `<text x="${padL - 4}" y="14" fill="var(--text-muted)" font-size="10" text-anchor="end">${max}</text>`
    + `<text x="${padL}" y="${height - 4}" fill="var(--text-muted)" font-size="10">${esc(lo)}</text>`
    + `<text x="${padL + w}" y="${height - 4}" fill="var(--text-muted)" font-size="10" text-anchor="end">${esc(hi)}</text>`;
  return `<svg class="sv-chart" viewBox="0 0 ${width} ${height}" width="100%" role="img" preserveAspectRatio="none">${axis}${bars}${marks}</svg>`;
}

/** Sparkline of `values` (a polyline), with the last value as a dot. */
export function sparklineSvg(values, { width = 120, height = 26, title = "" } = {}) {
  const v = values.map((x) => Number(x) || 0);
  if (!v.length) return "";
  const max = Math.max(1, ...v);
  const step = v.length > 1 ? (width - 4) / (v.length - 1) : 0;
  const pts = v.map((x, i) => `${(2 + i * step).toFixed(1)},${(height - 2 - (x / max) * (height - 4)).toFixed(1)}`);
  const [lx, ly] = pts[pts.length - 1].split(",");
  return `<svg class="sv-spark" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img"><title>${esc(title || v.join(", "))}</title>`
    + `<polyline points="${pts.join(" ")}" fill="none" stroke="var(--accent-blue)" stroke-width="1.5"/><circle cx="${lx}" cy="${ly}" r="2" fill="var(--accent-blue)"/></svg>`;
}

/** Horizontal bars (HTML, `sv-bar` rows) of [{label, value, title?}]. */
export function hbarsHtml(items, { max = null, format = (n) => String(n) } = {}) {
  const top = max ?? Math.max(1, ...items.map((i) => i.value));
  return `<div class="sv-bars">${items.map((i) => `<div class="sv-bar" title="${esc(i.title || "")}"><span>${esc(i.label)}</span><div class="sv-bar-track"><div class="sv-bar-fill" style="width:${Math.max(0.5, (i.value / top) * 100).toFixed(1)}%"></div></div><span>${esc(format(i.value))}</span></div>`).join("")}</div>`;
}

/** KPI tiles. */
export function kpiHtml(tiles) {
  return `<div class="sv-kpis">${tiles.map(([value, label, title]) => `<div class="sv-kpi" title="${esc(title || "")}"><div class="sv-kpi-val">${esc(value)}</div><div class="sv-kpi-lbl">${esc(label)}</div></div>`).join("")}</div>`;
}
