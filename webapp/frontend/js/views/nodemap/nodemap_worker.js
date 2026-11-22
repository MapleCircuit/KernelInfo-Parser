/**
 * nodemap_worker.js - Web Worker & pure job handlers for heavy NodeMap computation:
 *   - tokenize: AstOverlay lexical highlighting + htmlSegments extraction for large source nodes
 *   - kconfig:  DirectiveEvaluator + batch structRows evaluation across struct nodes
 *   - layout:   Sugiyama layered DAG layout (cycle breaking, longest path, barycenter) & force layout
 *   - svg:      standalone SVG document assembly via canvasToSvg
 *   - png:      OffscreenCanvas + createImageBitmap PNG rasterization (when supported by browser)
 *
 * Pure functions are exported directly so Node.js unit tests and main-thread fallback callers
 * execute the exact same code path with zero browser globals required.
 */
import { AstOverlay } from "../code_view/ast_overlay.js";
import { DirectiveEvaluator, archMacros } from "../kconfig/directive_evaluator.js";
import { structRows } from "./struct_view.js";
import { htmlSegments } from "./node_content.js";
import { canvasToSvg } from "./nodemap_export.js";

const DEFAULT_W = 300;
const DEFAULT_H = 200;

const nodeWidth = (n) => (typeof n?.w === "number" && Number.isFinite(n.w) && n.w > 0 ? n.w : DEFAULT_W);
const nodeHeight = (n) => (typeof n?.h === "number" && Number.isFinite(n.h) && n.h > 0 ? n.h : DEFAULT_H);

/**
 * Tokenize source lines with AstOverlay and extract highlight segments for SVG export.
 *
 * @param {object} [opts]
 * @param {string[]} [opts.lines]
 * @param {number} [opts.startLine=1]
 * @param {string} [opts.path=""]
 * @returns {{ startLine: number, htmlRows: string[], segments: Array<Array<{ text: string, cls: string }>> }}
 */
export function tokenizeSourceJob(opts = {}) {
  const { lines = [], startLine = 1, path = "" } = opts || {};
  const safeLines = Array.isArray(lines) ? lines.map((l) => String(l ?? "")) : [];
  const baseLine = startLine ?? 1;
  const ov = new AstOverlay();
  ov.setFileInfo(null, path || "", safeLines[0] || "");
  ov.indexMultiLineSpans(safeLines);

  const htmlRows = new Array(safeLines.length);
  const segments = new Array(safeLines.length);
  for (let i = 0; i < safeLines.length; i++) {
    const html = ov.renderLineHtml(i + 1, safeLines[i]);
    htmlRows[i] = html;
    segments[i] = htmlSegments(html);
  }
  return { startLine: baseLine, htmlRows, segments };
}

/**
 * Evaluate Kconfig #if/#ifdef visibility and build structRows models for a batch of struct nodes.
 *
 * @param {object} [opts]
 * @param {object} [opts.assignments]
 * @param {string} [opts.arch="x86_64"]
 * @param {boolean} [opts.strict=true]
 * @param {Array<{ nodeId: string, layout: object, data: object, editMode?: boolean }>} [opts.structs]
 * @returns {{ results: Array<{ nodeId: string, model: object }> }}
 */
export function batchKconfigJob(opts = {}) {
  const { assignments = {}, arch = "x86_64", strict = true, structs = [] } = opts || {};
  const safeAssignments = assignments && typeof assignments === "object" ? assignments : {};
  const safeArch = arch ?? "x86_64";
  const evaluator = new DirectiveEvaluator(safeAssignments, {
    partial: !strict,
    macros: archMacros(safeArch, safeAssignments),
  });
  const list = Array.isArray(structs) ? structs : [];
  const results = [];
  for (const item of list) {
    if (!item) continue;
    const { nodeId, layout, data, editMode } = item;
    const model = structRows(layout, data || {}, { editMode: Boolean(editMode), evaluator });
    results.push({ nodeId, model });
  }
  return { results };
}

/** Deduplicated directed edge index pairs `[u, v]` (no self-loops) among `active` nodes. */
function collectDirectedEdges(active, edges) {
  const idxById = new Map();
  for (let i = 0; i < active.length; i++) {
    if (!idxById.has(active[i].id)) idxById.set(active[i].id, i);
  }
  const out = [];
  const seen = new Set();
  for (const e of Array.isArray(edges) ? edges : []) {
    if (!e) continue;
    const u = idxById.get(e.from);
    const v = idxById.get(e.to);
    if (u === undefined || v === undefined || u === v) continue;
    const k = `${u}:${v}`;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push([u, v]);
  }
  return out;
}

/** Sugiyama layered DAG layout: DFS cycle breaking, longest-path layering, 4-sweep barycenter ordering. */
function layoutDag(active, edges, options) {
  const n = active.length;
  const dirEdges = collectDirectedEdges(active, edges);
  const rawOut = Array.from({ length: n }, () => []);
  for (const [u, v] of dirEdges) rawOut[u].push(v);

  // 1. Iterative 3-color DFS back-edge removal so cyclic graphs become strict DAGs
  const state = new Uint8Array(n); // 0=unvisited, 1=visiting, 2=done
  const dagEdges = [];
  for (let start = 0; start < n; start++) {
    if (state[start] !== 0) continue;
    state[start] = 1;
    const stack = [{ u: start, k: 0 }];
    while (stack.length) {
      const top = stack[stack.length - 1];
      const nbrs = rawOut[top.u];
      if (top.k >= nbrs.length) {
        state[top.u] = 2;
        stack.pop();
        continue;
      }
      const v = nbrs[top.k++];
      if (state[v] === 1) continue; // back-edge creates a cycle
      dagEdges.push([top.u, v]);
      if (state[v] === 0) {
        state[v] = 1;
        stack.push({ u: v, k: 0 });
      }
    }
  }

  // 2. Longest-path layer assignment in topological order (sources at 0, targets at max(pred + 1))
  const dagOut = Array.from({ length: n }, () => []);
  const dagIn = Array.from({ length: n }, () => []);
  const inDeg = new Int32Array(n);
  for (const [u, v] of dagEdges) {
    dagOut[u].push(v);
    dagIn[v].push(u);
    inDeg[v]++;
  }
  const layer = new Int32Array(n);
  const queue = [];
  for (let i = 0; i < n; i++) {
    if (inDeg[i] === 0) queue.push(i);
  }
  let head = 0;
  while (head < queue.length) {
    const u = queue[head++];
    const nextL = layer[u] + 1;
    for (const v of dagOut[u]) {
      if (layer[v] < nextL) layer[v] = nextL;
      if (--inDeg[v] === 0) queue.push(v);
    }
  }

  // 3. 4 sweeps of barycenter crossing minimization (alternating forward and backward passes)
  let maxLayer = 0;
  for (let i = 0; i < n; i++) {
    if (layer[i] > maxLayer) maxLayer = layer[i];
  }
  const layers = Array.from({ length: maxLayer + 1 }, () => []);
  const posInLayer = new Int32Array(n);
  for (let i = 0; i < n; i++) {
    const l = layer[i];
    posInLayer[i] = layers[l].length;
    layers[l].push(i);
  }

  const bary = new Float64Array(n);
  const sortLayer = (l, adjLayer, adjOf) => {
    const row = layers[l];
    if (row.length <= 1) return;
    for (const u of row) {
      let sum = 0;
      let cnt = 0;
      for (const v of adjOf(u)) {
        if (layer[v] === adjLayer) {
          sum += posInLayer[v];
          cnt++;
        }
      }
      bary[u] = cnt > 0 ? sum / cnt : posInLayer[u];
    }
    row.sort((a, b) => (bary[a] - bary[b]) || (posInLayer[a] - posInLayer[b]));
    for (let idx = 0; idx < row.length; idx++) posInLayer[row[idx]] = idx;
  };

  for (let sweep = 0; sweep < 4; sweep++) {
    if (sweep % 2 === 0) {
      for (let l = 1; l <= maxLayer; l++) sortLayer(l, l - 1, (u) => dagIn[u]);
    } else {
      for (let l = maxLayer - 1; l >= 0; l--) sortLayer(l, l + 1, (u) => dagOut[u]);
    }
  }

  // 4. Place columns left-to-right starting at (80, 80)
  const gapX = Number.isFinite(Number(options?.gapX)) ? Number(options.gapX) : 100;
  const gapY = Number.isFinite(Number(options?.gapY)) ? Number(options.gapY) : 40;
  const posMap = new Array(n);
  let x = 80;
  for (let l = 0; l < layers.length; l++) {
    const col = layers[l];
    if (!col.length) continue;
    let y = 80;
    let maxW = 0;
    for (const idx of col) {
      const w = nodeWidth(active[idx]);
      const h = nodeHeight(active[idx]);
      posMap[idx] = { id: active[idx].id, x: Math.round(x), y: Math.round(y) };
      y += h + gapY;
      if (w > maxW) maxW = w;
    }
    x += maxW + gapX;
  }
  return { positions: posMap };
}

/** Deterministic integer square-spiral generator around (0, 0). */
function createSpiralStepper() {
  let x = 0;
  let y = 0;
  let dx = 1;
  let dy = 0;
  let segLen = 1;
  let passed = 0;
  return () => {
    const pt = [x, y];
    x += dx;
    y += dy;
    if (++passed === segLen) {
      passed = 0;
      [dx, dy] = [-dy, dx];
      if (dy === 0) segLen++;
    }
    return pt;
  };
}

/** Deterministic force-directed layout with Coulomb repulsion, Hooke springs, gravity, and AABB separation. */
function layoutForce(active, edges, options) {
  const n = active.length;
  const gapX = Number.isFinite(Number(options?.gapX)) ? Number(options.gapX) : 100;
  const gapY = Number.isFinite(Number(options?.gapY)) ? Number(options.gapY) : 40;
  const iterations = Number.isFinite(Number(options?.iterations)) ? Math.max(0, Math.floor(Number(options.iterations))) : 80;

  const w = new Float64Array(n);
  const h = new Float64Array(n);
  const hw = new Float64Array(n);
  const hh = new Float64Array(n);
  let sumW = 0;
  let sumH = 0;
  for (let i = 0; i < n; i++) {
    w[i] = nodeWidth(active[i]);
    h[i] = nodeHeight(active[i]);
    hw[i] = w[i] * 0.5;
    hh[i] = h[i] * 0.5;
    sumW += w[i];
    sumH += h[i];
  }
  const avgW = sumW / n;
  const avgH = sumH / n;
  const cellW = avgW + gapX;
  const cellH = avgH + gapY;
  const kBase = Math.hypot(cellW, cellH);

  // 1. Initialize positions deterministically (keep distinct finite coords; spiral fallback for missing/duplicate)
  const cx = new Float64Array(n);
  const cy = new Float64Array(n);
  const used = new Set();
  const nextSpiral = createSpiralStepper();
  let anchorX = 0;
  let anchorY = 0;
  for (let i = 0; i < n; i++) {
    if (typeof active[i].x === "number" && Number.isFinite(active[i].x) &&
        typeof active[i].y === "number" && Number.isFinite(active[i].y)) {
      anchorX = active[i].x;
      anchorY = active[i].y;
      break;
    }
  }
  for (let i = 0; i < n; i++) {
    const node = active[i];
    const hasCoords = typeof node.x === "number" && Number.isFinite(node.x) &&
      typeof node.y === "number" && Number.isFinite(node.y);
    let x = hasCoords ? node.x : 0;
    let y = hasCoords ? node.y : 0;
    let key = `${Math.round(x)},${Math.round(y)}`;
    if (!hasCoords || used.has(key)) {
      do {
        const [sx, sy] = nextSpiral();
        x = anchorX + sx * cellW;
        y = anchorY + sy * cellH;
        key = `${Math.round(x)},${Math.round(y)}`;
      } while (used.has(key));
    }
    used.add(key);
    cx[i] = x + hw[i];
    cy[i] = y + hh[i];
  }

  // Build unique undirected edge pairs for Hooke spring attraction
  const dirEdges = collectDirectedEdges(active, edges);
  const undirEdges = [];
  const seenUndir = new Set();
  for (const [u, v] of dirEdges) {
    const a = u < v ? u : v;
    const b = u < v ? v : u;
    const k = `${a}:${b}`;
    if (seenUndir.has(k)) continue;
    seenUndir.add(k);
    undirEdges.push([a, b]);
  }

  // 2. Simulation: Coulomb repulsion + Hooke springs + gentle central gravity with cooling
  const fx = new Float64Array(n);
  const fy = new Float64Array(n);
  for (let it = 0; it < iterations; it++) {
    const tRatio = 1 - it / Math.max(1, iterations);
    const maxStep = Math.max(4, kBase * 0.55 * tRatio * tRatio + 4 * tRatio);
    fx.fill(0);
    fy.fill(0);

    let meanX = 0;
    let meanY = 0;
    for (let i = 0; i < n; i++) {
      meanX += cx[i];
      meanY += cy[i];
    }
    meanX /= n;
    meanY /= n;

    // Pairwise Coulomb repulsion
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = cx[j] - cx[i];
        let dy = cy[j] - cy[i];
        if (dx === 0 && dy === 0) {
          dx = i % 2 === 0 ? 1 : -1;
          dy = 1;
        }
        const dist = Math.max(1, Math.hypot(dx, dy));
        const ux = dx / dist;
        const uy = dy / dist;
        const pairK = Math.max(hw[i] + hw[j] + gapX, hh[i] + hh[j] + gapY);
        const fRep = (pairK * pairK * pairK) / Math.max(dist * dist, 100);
        fx[i] -= ux * fRep;
        fy[i] -= uy * fRep;
        fx[j] += ux * fRep;
        fy[j] += uy * fRep;
      }
    }

    // Hooke edge spring attraction
    for (const [u, v] of undirEdges) {
      const dx = cx[v] - cx[u];
      const dy = cy[v] - cy[u];
      const dist = Math.hypot(dx, dy);
      if (dist <= 0) continue;
      const restLen = Math.max(hw[u] + hw[v] + gapX, hh[u] + hh[v] + gapY);
      const fAtt = 1.2 * (dist - restLen * 0.85);
      const ux = dx / dist;
      const uy = dy / dist;
      fx[u] += ux * fAtt;
      fy[u] += uy * fAtt;
      fx[v] -= ux * fAtt;
      fy[v] -= uy * fAtt;
    }

    // Weak central gravity keeps disconnected components bounded
    const gravity = 0.28;
    for (let i = 0; i < n; i++) {
      fx[i] += (meanX - cx[i]) * gravity;
      fy[i] += (meanY - cy[i]) * gravity;
      const mag = Math.hypot(fx[i], fy[i]);
      if (mag > 0) {
        const step = Math.min(mag, maxStep);
        cx[i] += (fx[i] / mag) * step;
        cy[i] += (fy[i] / mag) * step;
      }
    }
  }

  // Pairwise AABB separation pass to eliminate any remaining node overlap
  const padX = Math.max(20, Math.min(gapX, 40));
  const padY = Math.max(20, Math.min(gapY, 40));
  const maxPasses = Math.min(120, Math.max(40, n * 3));
  for (let pass = 0; pass < maxPasses; pass++) {
    let moved = false;
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = cx[j] - cx[i];
        const dy = cy[j] - cy[i];
        const ox = hw[i] + hw[j] + padX - Math.abs(dx);
        const oy = hh[i] + hh[j] + padY - Math.abs(dy);
        if (ox > 0 && oy > 0) {
          moved = true;
          if (ox <= oy) {
            const s = dx > 0 || (dx === 0 && i < j) ? 1 : -1;
            cx[i] -= s * ox * 0.5;
            cx[j] += s * ox * 0.5;
          } else {
            const s = dy > 0 || (dy === 0 && i < j) ? 1 : -1;
            cy[i] -= s * oy * 0.5;
            cy[j] += s * oy * 0.5;
          }
        }
      }
    }
    if (!moved) break;
  }

  // 3. Normalize top-left bounding box to (80, 80) and round coordinates to integers
  let minX = Infinity;
  let minY = Infinity;
  for (let i = 0; i < n; i++) {
    const x = cx[i] - hw[i];
    const y = cy[i] - hh[i];
    if (x < minX) minX = x;
    if (y < minY) minY = y;
  }
  const positions = active.map((node, i) => ({
    id: node.id,
    x: Math.round(cx[i] - hw[i] - minX + 80),
    y: Math.round(cy[i] - hh[i] - minY + 80),
  }));
  return { positions };
}

/**
 * Compute node positions for non-group nodes using `"dag"` (Sugiyama) or `"force"` layout.
 *
 * @param {object} [opts]
 * @param {Array<object>} [opts.nodes]
 * @param {Array<object>} [opts.edges]
 * @param {"dag"|"force"} [opts.mode="dag"]
 * @param {object} [opts.options]
 * @returns {{ positions: Array<{ id: string, x: number, y: number }> }}
 */
export function layoutGraphJob(opts = {}) {
  const { nodes = [], edges = [], mode = "dag", options = {} } = opts || {};
  const active = (Array.isArray(nodes) ? nodes : []).filter((n) => n && n.id !== undefined && n.id !== null && n.type !== "group");
  if (!active.length) return { positions: [] };
  const layoutOpts = options || {};
  if (mode === "force") return layoutForce(active, edges, layoutOpts);
  return layoutDag(active, edges, layoutOpts);
}

/** Rehydrate a structured-cloned or declarative evaluator descriptor for `structRows()`. */
function normalizeStructOpts(entry, defaultEditMode) {
  if (!entry) return { editMode: defaultEditMode };
  const ev = entry.evaluator;
  if (ev && typeof ev.isDefined !== "function" && ev.assignments) {
    return {
      ...entry,
      evaluator: new DirectiveEvaluator(ev.assignments, { partial: Boolean(ev.partial), macros: ev.macros }),
    };
  }
  if (!ev && entry.kconfig && typeof entry.kconfig === "object") {
    const kc = entry.kconfig;
    const assign = kc.assignments && typeof kc.assignments === "object" ? kc.assignments : {};
    return {
      ...entry,
      evaluator: new DirectiveEvaluator(assign, {
        partial: kc.strict === false,
        macros: archMacros(kc.arch ?? "x86_64", assign),
      }),
    };
  }
  return entry;
}

/**
 * Assemble a standalone SVG document for a NodeMap canvas using pre-resolved colors and payloads.
 *
 * @param {object} [opts]
 * @returns {{ svg: string }}
 */
export function assembleSvgJob(opts = {}) {
  const {
    canvas,
    resolvedColors = {},
    contentMap = {},
    structOptsMap = {},
    rowHeightsMap = {},
    kconfigValuesMap = {},
    foreignMap = {},
    editMode = false,
    transparent = false,
    pixelScale = 1,
  } = opts || {};

  const safeCanvas = {
    ...(canvas || {}),
    nodes: Array.isArray(canvas?.nodes) ? canvas.nodes : [],
    edges: Array.isArray(canvas?.edges) ? canvas.edges : [],
    settings: canvas?.settings || {},
  };
  const colors = resolvedColors || {};
  const content = contentMap || {};
  const sOpts = structOptsMap || {};
  const rHeights = rowHeightsMap || {};
  const kcVals = kconfigValuesMap || {};
  const foreign = foreignMap || {};

  const resolve = (c) => (colors[c] !== undefined ? colors[c] : c);
  const svg = canvasToSvg(safeCanvas, resolve, {
    editMode,
    transparent,
    pixelScale,
    content: (n) => content[n.id] || null,
    structOpts: (n) => normalizeStructOpts(sOpts[n.id], editMode),
    rowHeights: (n) => rHeights[n.id] || null,
    kconfigValues: (n) => kcVals[n.id] || null,
    foreign: (n) => foreign[n.id] || null,
  });
  return { svg };
}

/**
 * Rasterize an SVG document to a PNG Blob via OffscreenCanvas + createImageBitmap when supported.
 *
 * @param {object} [opts]
 * @param {string} [opts.svg]
 * @param {number} [opts.width]
 * @param {number} [opts.height]
 * @returns {Promise<{ blob: Blob } | { unsupported: true, error?: string }>}
 */
export async function rasterizePngJob(opts = {}) {
  const { svg, width, height } = opts || {};
  if (
    typeof OffscreenCanvas === "undefined" ||
    typeof createImageBitmap !== "function" ||
    !svg ||
    !(Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0)
  ) {
    return { unsupported: true };
  }
  try {
    const blob = new Blob([svg], { type: "image/svg+xml;charset=utf-8" });
    const bmp = await createImageBitmap(blob);
    try {
      const c = new OffscreenCanvas(width, height);
      const ctx = c.getContext("2d");
      if (!ctx) return { unsupported: true, error: "2D context unavailable" };
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(bmp, 0, 0, width, height);
      const pngBlob = await c.convertToBlob({ type: "image/png" });
      return { blob: pngBlob };
    } finally {
      bmp?.close?.();
    }
  } catch (err) {
    return { unsupported: true, error: err?.message || "OffscreenCanvas failed" };
  }
}

/**
 * Dispatch a NodeMap worker job by type.
 *
 * @param {"tokenize"|"kconfig"|"layout"|"svg"|"png"} type
 * @param {object} [payload]
 * @returns {Promise<object>}
 */
export async function runWorkerJob(type, payload) {
  switch (type) {
    case "tokenize": return tokenizeSourceJob(payload);
    case "kconfig": return batchKconfigJob(payload);
    case "layout": return layoutGraphJob(payload);
    case "svg": return assembleSvgJob(payload);
    case "png": return rasterizePngJob(payload);
    default: throw new Error(`Unknown worker job: ${type}`);
  }
}

const inWorker =
  typeof self !== "undefined" &&
  typeof self.postMessage === "function" &&
  typeof window === "undefined" &&
  typeof process === "undefined";

if (inWorker) {
  self.onmessage = async (ev) => {
    const { id, type, payload } = ev?.data || {};
    try {
      const result = await runWorkerJob(type, payload);
      self.postMessage({ id, ok: true, result });
    } catch (err) {
      self.postMessage({ id, ok: false, error: err?.message || String(err) });
    }
  };
}
