/**
 * edge_router.js - NodeMap edge geometry: side anchors, auto side choice, bezier / straight / orthogonal
 * paths, arrow heads and label placement. DOM-free; coordinates are canvas units (the wire layer sits
 * inside the zoomed canvas layer, so the same geometry serves the live view and the SVG/PNG export).
 */

const NORMALS = { top: [0, -1], right: [1, 0], bottom: [0, 1], left: [-1, 0] };

/** Point on side `side` of `rect`; `at` (0..1) slides along the side (0.5 = its middle). */
export function anchorPoint(rect, side, at = 0.5) {
  switch (side) {
    case "top": return { x: rect.x + rect.w * at, y: rect.y };
    case "bottom": return { x: rect.x + rect.w * at, y: rect.y + rect.h };
    case "left": return { x: rect.x, y: rect.y + rect.h * at };
    default: return { x: rect.x + rect.w, y: rect.y + rect.h * at };
  }
}

/** The facing sides of two rectangles (auto routing): horizontal when they are more apart in x than in y. */
export function autoSides(a, b) {
  const ax = a.x + a.w / 2, ay = a.y + a.h / 2;
  const bx = b.x + b.w / 2, by = b.y + b.h / 2;
  const dx = bx - ax, dy = by - ay;
  // Gaps between the boxes decide, not centers alone: side-by-side boxes connect left/right
  const gapX = Math.max(b.x - (a.x + a.w), a.x - (b.x + b.w));
  const gapY = Math.max(b.y - (a.y + a.h), a.y - (b.y + b.h));
  if (gapX >= gapY) return dx >= 0 ? ["right", "left"] : ["left", "right"];
  return dy >= 0 ? ["bottom", "top"] : ["top", "bottom"];
}

/**
 * Resolve the end points of an edge. `from` / `to` are `{rect, side, point?}`: `side` "auto" takes the
 * facing side; a `point` (struct field port) pins the anchor and keeps its side.
 */
export function resolveEnds(from, to) {
  const [sa, sb] = autoSides(from.rect, to.rect);
  const fromSide = from.side && from.side !== "auto" ? from.side : (from.point ? (sa === "left" ? "left" : "right") : sa);
  const toSide = to.side && to.side !== "auto" ? to.side : (to.point ? (sb === "right" ? "right" : "left") : sb);
  return {
    p1: from.point || anchorPoint(from.rect, fromSide),
    p2: to.point || anchorPoint(to.rect, toSide),
    fromSide,
    toSide,
  };
}

function cubicAt(p0, c1, c2, p3, t) {
  const u = 1 - t;
  return {
    x: u * u * u * p0.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t * t * t * p3.x,
    y: u * u * u * p0.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t * t * t * p3.y,
  };
}

const r1 = (v) => Math.round(v * 10) / 10;

/**
 * Path of an edge between p1 (leaving `fromSide`) and p2 (entering `toSide`) in `curve` style.
 * Returns {d, mid, startAngle, endAngle}: `mid` is where the label goes, the angles (radians) are the
 * direction of travel at each end (for arrow heads).
 */
export function edgePath(p1, p2, fromSide = "right", toSide = "left", curve = "bezier") {
  const n1 = NORMALS[fromSide] || NORMALS.right;
  const n2 = NORMALS[toSide] || NORMALS.left;
  if (curve === "straight") {
    const ang = Math.atan2(p2.y - p1.y, p2.x - p1.x);
    return { d: `M ${r1(p1.x)} ${r1(p1.y)} L ${r1(p2.x)} ${r1(p2.y)}`, mid: { x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 }, startAngle: ang, endAngle: ang };
  }
  if (curve === "orthogonal") {
    const stub = 24;
    const a = { x: p1.x + n1[0] * stub, y: p1.y + n1[1] * stub };
    const b = { x: p2.x + n2[0] * stub, y: p2.y + n2[1] * stub };
    const pts = [p1, a];
    const horizFirst = n1[0] !== 0;
    if (horizFirst) {
      const mx = (n2[0] !== 0) ? (a.x + b.x) / 2 : b.x;
      pts.push({ x: mx, y: a.y }, { x: mx, y: b.y });
    } else {
      const my = (n2[1] !== 0) ? (a.y + b.y) / 2 : b.y;
      pts.push({ x: a.x, y: my }, { x: b.x, y: my });
    }
    pts.push(b, p2);
    const clean = pts.filter((p, i) => i === 0 || p.x !== pts[i - 1].x || p.y !== pts[i - 1].y);
    const d = clean.map((p, i) => `${i ? "L" : "M"} ${r1(p.x)} ${r1(p.y)}`).join(" ");
    // Label on the middle segment
    const m = Math.floor((clean.length - 1) / 2);
    const mid = { x: (clean[m].x + clean[m + 1].x) / 2, y: (clean[m].y + clean[m + 1].y) / 2 };
    const last = clean[clean.length - 2] || p1;
    return { d, mid, startAngle: Math.atan2(n1[1], n1[0]), endAngle: Math.atan2(p2.y - last.y, p2.x - last.x) };
  }
  const dist = Math.hypot(p2.x - p1.x, p2.y - p1.y);
  const k = Math.max(40, Math.min(240, dist * 0.45));
  const c1 = { x: p1.x + n1[0] * k, y: p1.y + n1[1] * k };
  const c2 = { x: p2.x + n2[0] * k, y: p2.y + n2[1] * k };
  return {
    d: `M ${r1(p1.x)} ${r1(p1.y)} C ${r1(c1.x)} ${r1(c1.y)}, ${r1(c2.x)} ${r1(c2.y)}, ${r1(p2.x)} ${r1(p2.y)}`,
    mid: cubicAt(p1, c1, c2, p2, 0.5),
    startAngle: Math.atan2(c1.y - p1.y, c1.x - p1.x),
    endAngle: Math.atan2(p2.y - c2.y, p2.x - c2.x),
    controls: [c1, c2],
  };
}

/** Polygon points of an arrow head with its tip at `tip`, pointing along `angle`. */
export function arrowHead(tip, angle, size = 9) {
  const spread = 0.45;
  const a = { x: tip.x - size * Math.cos(angle - spread), y: tip.y - size * Math.sin(angle - spread) };
  const b = { x: tip.x - size * Math.cos(angle + spread), y: tip.y - size * Math.sin(angle + spread) };
  return `${r1(tip.x)},${r1(tip.y)} ${r1(a.x)},${r1(a.y)} ${r1(b.x)},${r1(b.y)}`;
}

/** Arrow heads of an edge: which ends get one for `arrow` none|forward|backward|both. */
export function arrowHeads(geom, p1, p2, arrow) {
  const out = [];
  if (arrow === "forward" || arrow === "both") out.push(arrowHead(p2, geom.endAngle));
  if (arrow === "backward" || arrow === "both") out.push(arrowHead(p1, geom.startAngle + Math.PI));
  return out;
}

/** SVG `stroke-dasharray` of a line style. */
export function dashArray(style) {
  if (style === "dashed") return "8 5";
  if (style === "dotted") return "2 4";
  return "";
}

/** Full geometry of one edge between two node rectangles. */
export function computeEdgeGeometry(from, to, { curve = "bezier", arrow = "forward" } = {}) {
  const ends = resolveEnds(from, to);
  const geom = edgePath(ends.p1, ends.p2, ends.fromSide, ends.toSide, curve);
  return { ...ends, ...geom, heads: arrowHeads(geom, ends.p1, ends.p2, arrow) };
}
