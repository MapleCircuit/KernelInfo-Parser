/**
 * nodemap_export.js - SVG and PNG snapshots of a NodeMap canvas (groups, nodes with their accents and
 * titles, edges with their route, line style, arrows and labels). Theme tokens are resolved to literal
 * colors so the files look the same outside the app.
 */
import { accentVar, boundsOf, displayTitle, paintOrder } from "./canvas_model.js";
import { arrowHeads, dashArray, edgePath, resolveEnds } from "./edge_router.js";
import { esc } from "./struct_view.js";

const PAD = 40;

/** Edge geometry for export: side anchors only (field rows are not drawn in the snapshot). */
function exportEdges(canvas) {
  const byId = new Map(canvas.nodes.map((n) => [n.id, n]));
  const out = [];
  for (const e of canvas.edges) {
    const a = byId.get(e.from);
    const b = byId.get(e.to);
    if (!a || !b) continue;
    const ends = resolveEnds({ rect: a, side: e.fromSide }, { rect: b, side: e.toSide });
    const g = edgePath(ends.p1, ends.p2, ends.fromSide, ends.toSide, e.curve);
    out.push({ edge: e, geom: g, heads: arrowHeads(g, ends.p1, ends.p2, e.arrow) });
  }
  return out;
}

/** Standalone SVG document of the canvas; `resolve(cssColor)` turns `var(--x)` into a literal color. */
export function canvasToSvg(canvas, resolve = (c) => c) {
  const b = boundsOf(canvas.nodes) || { x: 0, y: 0, w: 400, h: 300 };
  const vx = b.x - PAD, vy = b.y - PAD, vw = b.w + PAD * 2, vh = b.h + PAD * 2;
  const bg = resolve("var(--bg-canvas)");
  const panel = resolve("var(--bg-secondary)");
  const text = resolve("var(--text-primary)");
  const muted = resolve("var(--text-secondary)");
  let s = `<svg xmlns="http://www.w3.org/2000/svg" width="${Math.round(vw)}" height="${Math.round(vh)}" viewBox="${vx} ${vy} ${vw} ${vh}">\n`;
  s += `<rect x="${vx}" y="${vy}" width="${vw}" height="${vh}" fill="${bg}"/>\n`;
  /* ui-scale-exempt-start (NodeMap generated SVG: canvas units, zoom-controlled) */
  const nodeSvg = (n) => {
    const stroke = resolve(accentVar(n.color, n.type === "group" ? "var(--border-color)" : "var(--accent-blue)"));
    if (n.type === "group") {
      return `<g><rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="8" fill="${stroke}" fill-opacity="0.06" stroke="${stroke}" stroke-dasharray="6 4"/>` +
        `<text x="${n.x + 10}" y="${n.y + 18}" fill="${text}" font-family="sans-serif" font-weight="bold" font-size="12">${esc(displayTitle(n))}</text></g>\n`;
    }
    const note = n.type === "note" ? `<text x="${n.x + 10}" y="${n.y + 44}" fill="${muted}" font-family="sans-serif" font-size="11">${esc((n.data.text || "").split("\n")[0].slice(0, 80))}</text>` : "";
    return `<g><rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="6" fill="${panel}" stroke="${stroke}" stroke-width="1.5"/>` +
      `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="3" fill="${stroke}"/>` +
      `<text x="${n.x + 10}" y="${n.y + 22}" fill="${text}" font-family="sans-serif" font-weight="bold" font-size="12">${esc(displayTitle(n))}</text>` +
      `<text x="${n.x + n.w - 10}" y="${n.y + 22}" text-anchor="end" fill="${muted}" font-family="monospace" font-size="10">${esc(n.type)}</text>${note}</g>\n`;
  };
  const ordered = paintOrder(canvas.nodes);
  for (const n of ordered.filter((x) => x.type === "group")) s += nodeSvg(n);
  for (const { edge, geom, heads } of exportEdges(canvas)) {
    const c = resolve(accentVar(edge.color, "var(--accent-blue)"));
    const dash = dashArray(edge.style);
    s += `<path d="${geom.d}" fill="none" stroke="${c}" stroke-width="2"${dash ? ` stroke-dasharray="${dash}"` : ""}/>\n`;
    for (const h of heads) s += `<polygon points="${h}" fill="${c}"/>\n`;
    if (edge.label) s += `<text x="${geom.mid.x}" y="${geom.mid.y - 6}" text-anchor="middle" fill="${text}" stroke="${bg}" stroke-width="4" paint-order="stroke" font-family="sans-serif" font-size="11">${esc(edge.label)}</text>\n`;
  }
  for (const n of ordered.filter((x) => x.type !== "group")) s += nodeSvg(n);
  /* ui-scale-exempt-end */
  return `${s}</svg>`;
}
