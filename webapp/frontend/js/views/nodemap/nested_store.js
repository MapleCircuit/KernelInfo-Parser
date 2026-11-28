/**
 * nested_store.js - Browser-local storage and pure transforms of nested NodeMaps.
 *
 * Working format: a `nodemap` node is `data: {canvasId, summary, preview}`; the child canvas lives in the
 * named-canvas library (`nodemap_library.js`, key `kip-nodemap-named:<canvasId>`, record flag `nested: true`
 * so the library list hides it). Portable format (JSON import/export, server canvases): the child canvas
 * inline in `data.canvas`. `unpackDocument` turns the portable form into the working form, `inlineDocument`
 * the reverse. Nothing here talks to the server. DOM-free; storage is `{userDb, storage}`.
 */
import * as M from "./canvas_model.js";
import { NAMED_STORAGE_PREFIX, listNamedCanvases, loadNamedCanvas, saveNamedCanvas } from "./nodemap_library.js";

export const PREVIEW_SCALE = 1000;
export const PREVIEW_MAX_NODES = 200;
export const PREVIEW_MAX_EDGES = 400;
export const GROUP_ORIGIN = 80;
export const GROUP_FRAME_MAX = { w: 640, h: 480 };

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const clone = (v) => JSON.parse(JSON.stringify(v));

/** The display options of a nested node that travel with it in every form (`{highQualityPreview: true}` or `{}`). */
export function nestedOptions(data) {
  return data?.highQualityPreview === true ? { highQualityPreview: true } : {};
}

/** Storage handle of a controller (`ctrl.userDb` + `localStorage`). */
export function storeOf(ctrl) {
  return { userDb: ctrl?.userDb || null, storage: globalThis.localStorage };
}

export const childKey = (id) => `${NAMED_STORAGE_PREFIX}${id}`;

// ---------------------------------------------------------------- child records

/** Save a child canvas under its local id (a nested library record). */
export async function saveChild(store, id, canvas, { name = "", repo = "" } = {}) {
  const key = childKey(id);
  const res = await saveNamedCanvas(store.userDb, { key, name: name || "Nested NodeMap", repo, canvas, nested: true }, store.storage);
  return res.ok;
}

/** `{id, name, canvas}` of a stored child (or library) canvas, `null` when absent. */
export async function loadChild(store, id) {
  if (!id) return null;
  const rec = await loadNamedCanvas(store.userDb, childKey(id), store.storage);
  return rec ? { id: String(id), name: rec.name, canvas: rec.canvas, nested: rec.nested } : null;
}

// ---------------------------------------------------------------- canvas walking

/** Every `nodemap` node of `canvas`, descending into inline Sub-Maps (`group.data.subCanvas`). */
export function nestedNodesOf(canvas, depth = 0) {
  const out = [];
  if (!canvas || depth > M.MAX_SUBCANVAS_DEPTH) return out;
  for (const n of canvas.nodes || []) {
    if (n.type === "nodemap") out.push(n);
    else if (n.type === "group" && isObj(n.data?.subCanvas)) out.push(...nestedNodesOf(n.data.subCanvas, depth + 1));
  }
  return out;
}

/** Local child ids a canvas references (Sub-Maps included). */
export function referencedIds(canvas) {
  return [...new Set(nestedNodesOf(canvas).map((n) => n.data?.canvasId || "").filter(Boolean))];
}

/** Node `id` anywhere in the tree of `canvas` (Sub-Maps included). */
export function findNodeDeep(canvas, id, depth = 0) {
  if (!canvas || depth > M.MAX_SUBCANVAS_DEPTH) return null;
  for (const n of canvas.nodes || []) {
    if (n.id === id) return n;
    if (n.type === "group" && isObj(n.data?.subCanvas)) {
      const hit = findNodeDeep(n.data.subCanvas, id, depth + 1);
      if (hit) return hit;
    }
  }
  return null;
}

// ---------------------------------------------------------------- summary / preview

/** Card summary of a child canvas: counts of the child only, `depth` = nested levels below it. */
export function summarizeCanvas(canvas, title = "", depthBelow = 0) {
  const c = canvas || {};
  return {
    title: String(title || "").slice(0, 256),
    nodes: (c.nodes || []).length,
    edges: (c.edges || []).length,
    nested: (c.nodes || []).filter((n) => n.type === "nodemap").length,
    depth: depthBelow,
  };
}

/** Levels of nested nodes below `canvas` according to their cached summaries (0 = leaf). */
export function cachedDepth(canvas) {
  let d = 0;
  for (const n of nestedNodesOf(canvas)) {
    if (n.data?.canvasId || n.data?.canvas) d = Math.max(d, (n.data.summary?.depth ?? 0) + 1);
  }
  return d;
}

/** Bounded read-only geometry of a canvas in a 1000-unit box (mirrors Python `build_static_preview`). */
export function buildStaticPreview(canvas) {
  const nodes = [...(canvas?.nodes || [])];
  const edges = canvas?.edges || [];
  const b = M.boundsOf(nodes);
  if (!b) return { w: 0, h: 0, nodes: [], edges: [], truncated: false };
  const k = PREVIEW_SCALE / Math.max(b.w, b.h, 1);
  const shown = M.paintOrder(nodes).slice(0, PREVIEW_MAX_NODES);
  const centers = new Map();
  const boxes = shown.map((n) => {
    const x = Math.round((n.x - b.x) * k);
    const y = Math.round((n.y - b.y) * k);
    const w = Math.max(1, Math.round(n.w * k));
    const h = Math.max(1, Math.round(n.h * k));
    centers.set(n.id, [x + Math.floor(w / 2), y + Math.floor(h / 2)]);
    return { x, y, w, h, type: n.type || "", color: n.color || "" };
  });
  const segs = [];
  for (const e of edges) {
    const a = centers.get(e.from);
    const z = centers.get(e.to);
    if (!a || !z) continue;
    if (segs.length >= PREVIEW_MAX_EDGES) break;
    segs.push({ x1: a[0], y1: a[1], x2: z[0], y2: z[1], color: e.color || "", style: e.style || "solid" });
  }
  const linkable = edges.filter((e) => centers.has(e.from) && centers.has(e.to)).length;
  return {
    w: Math.round(b.w * k),
    h: Math.round(b.h * k),
    nodes: boxes,
    edges: segs,
    truncated: shown.length < nodes.length || segs.length < linkable,
  };
}

/** Write the cached `summary` + `preview` of `child` into nested `node`. Returns whether anything changed. */
export function refreshNodeCache(node, child, title = "") {
  if (!node || node.type !== "nodemap" || !node.data?.canvasId) return false;
  const summary = summarizeCanvas(child, title || node.data.summary?.title || M.displayTitle(node), cachedDepth(child));
  const preview = buildStaticPreview(child);
  const same = JSON.stringify([node.data.summary, node.data.preview]) === JSON.stringify([summary, preview]);
  if (!same) node.data = { ...node.data, summary, preview };
  return !same;
}

/** Card the node renders from its cached working data (`status`: `ok` / `missing` / `unset`). */
export function cardOf(node, status = "ok") {
  const id = node?.data?.canvasId || "";
  if (!id) return null;
  if (status === "missing") return { canvas_id: id, status: "missing" };
  const s = node.data.summary || {};
  return {
    canvas_id: id, status: "ok", name: s.title || "",
    counts: { nodes: s.nodes || 0, edges: s.edges || 0, nested: s.nested || 0 },
    preview: node.data.preview || null,
  };
}

// ---------------------------------------------------------------- import: inline -> local

/**
 * Unpack a normalized canvas in place: every inline `data.canvas` (also inside Sub-Maps) is stored as a
 * local child and the node becomes `{canvasId, summary, preview}`. Depth is bounded by the model
 * normalization (`MAX_SUBCANVAS_DEPTH`); `seen` guards identity cycles. Returns the canvas.
 */
export async function unpackDocument(store, canvas, { repo = "", seen = new Set(), depth = 0 } = {}) {
  if (!canvas || seen.has(canvas) || depth > M.MAX_SUBCANVAS_DEPTH + 1) return canvas;
  seen.add(canvas);
  for (const n of canvas.nodes || []) {
    if (n.type === "group" && isObj(n.data?.subCanvas)) {
      await unpackDocument(store, n.data.subCanvas, { repo, seen, depth: depth + 1 });
    } else if (n.type === "nodemap" && isObj(n.data?.canvas)) {
      const child = n.data.canvas;
      const opts = nestedOptions(n.data);
      if (seen.has(child)) {
        n.data = { ...opts };
        continue;
      }
      await unpackDocument(store, child, { repo, seen, depth: depth + 1 });
      const id = M.newId("nm");
      const title = M.displayTitle(n);
      const ok = await saveChild(store, id, child, { name: title, repo });
      if (!ok) {
        n.data = { ...opts };
        continue;
      }
      const data = { canvasId: id, ...opts, summary: summarizeCanvas(child, title, cachedDepth(child)), preview: buildStaticPreview(child) };
      n.data = data;
    }
  }
  return canvas;
}

/** Normalize raw JSON (inline form) and unpack it into local storage; `null` when it is not a canvas. */
export async function importDocument(store, raw, opts = {}) {
  const canvas = M.normalizeCanvas(raw);
  if (!canvas) return null;
  await unpackDocument(store, canvas, opts);
  return M.normalizeCanvas(canvas);
}

// ---------------------------------------------------------------- export: local -> inline

/**
 * Self-contained deep copy of `canvas`: every `canvasId` is re-inlined from local storage into
 * `data.canvas` (`canvasId`, `summary`, `preview` dropped; `highQualityPreview` kept). A reference cycle, a missing local target or
 * a tree deeper than `MAX_SUBCANVAS_DEPTH` becomes an unset nested node (`data: {}`) plus a warning
 * `{code: "reference_cycle" | "missing_reference" | "nested_depth_exceeded", node_id, canvas_id}`.
 * Resolves `{canvas, warnings}`.
 */
export async function inlineDocument(store, canvas, { chain = [] } = {}) {
  const warnings = [];
  const cache = new Map();
  const loadOnce = async (id) => {
    if (!cache.has(id)) cache.set(id, await loadChild(store, id));
    return cache.get(id);
  };
  const walk = async (cv, path, depth) => {
    const out = clone(M.serializeCanvas(cv));
    const visit = async (doc, d) => {
      for (const n of doc.nodes) {
        if (n.type === "group" && isObj(n.data?.subCanvas)) {
          await visit(n.data.subCanvas, d + 1);
        } else if (n.type === "nodemap") {
          const id = n.data?.canvasId || "";
          const opts = nestedOptions(n.data);
          if (!id) {
            if (!isObj(n.data?.canvas)) n.data = { ...opts };
            continue;
          }
          let code = "";
          let child = null;
          if (path.includes(id)) code = "reference_cycle";
          else if (d + 1 > M.MAX_SUBCANVAS_DEPTH) code = "nested_depth_exceeded";
          else {
            child = await loadOnce(id);
            if (!child) code = "missing_reference";
          }
          if (code) {
            warnings.push({ code, node_id: n.id, canvas_id: id });
            n.data = { ...opts };
            continue;
          }
          n.data = { canvas: await walk(child.canvas, [...path, id], d + 1), ...opts };
        }
      }
    };
    await visit(out, depth);
    return out;
  };
  const doc = await walk(canvas, [...chain], 0);
  return { canvas: doc, warnings };
}

// ---------------------------------------------------------------- reference graph / cycles

/** `Map<localId, Set<referencedId>>` of every stored local canvas (library maps and nested children). */
export async function referenceGraph(store) {
  const graph = new Map();
  for (const rec of await listNamedCanvases(store.userDb, { nested: "all" }, store.storage)) {
    const child = await loadChild(store, rec.id);
    graph.set(rec.id, new Set(child ? referencedIds(child.canvas) : []));
  }
  return graph;
}

/**
 * Local canvas ids that cannot be inserted into the canvas `currentId`: the canvas itself, every id in
 * `ancestors` (the open drill-down chain) and every canvas from which `currentId` or an ancestor is
 * reachable through references (inserting it would create a cycle).
 */
export function cycleBlocked(graph, currentId, ancestors = []) {
  const targets = new Set([currentId, ...ancestors].filter(Boolean));
  const blocked = new Set(targets);
  let grew = true;
  while (grew) {
    grew = false;
    for (const [id, refs] of graph) {
      if (blocked.has(id)) continue;
      for (const r of refs) {
        if (blocked.has(r)) {
          blocked.add(id);
          grew = true;
          break;
        }
      }
    }
  }
  return blocked;
}

// ---------------------------------------------------------------- group selection

/**
 * Partition `canvas` for grouping `nodeIds` behind the nested node `nestedId` (port of Python
 * `split_for_group`): the selection plus the members of selected groups moves, edges inside move, edges
 * crossing the boundary stay with that end rewired to `nestedId` (port and waypoints dropped). Pure.
 */
export function splitForGroup(canvas, nodeIds, nestedId) {
  const nodes = canvas.nodes || [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const picked = [...new Set(nodeIds.map(String))].filter((i) => byId.has(i));
  const moved = new Set(picked);
  for (const id of picked) if (byId.get(id).type === "group") for (const d of M.groupDescendants(nodes, id)) moved.add(d.id);
  const movedNodes = nodes.filter((n) => moved.has(n.id)).map(clone);
  const b = M.boundsOf(movedNodes) || { x: GROUP_ORIGIN, y: GROUP_ORIGIN, w: 0, h: 0 };
  const dx = GROUP_ORIGIN - b.x;
  const dy = GROUP_ORIGIN - b.y;
  const outer = new Set(movedNodes.filter((n) => !moved.has(n.groupId)).map((n) => n.groupId || null));
  for (const n of movedNodes) {
    n.x += dx;
    n.y += dy;
    if (!moved.has(n.groupId)) n.groupId = null;
  }
  const childEdges = [];
  const parentEdges = [];
  const rewired = [];
  for (const e of canvas.edges || []) {
    const src = moved.has(e.from);
    const dst = moved.has(e.to);
    const ce = clone(e);
    if (src && dst) {
      ce.waypoints = (ce.waypoints || []).map((w) => ({ x: w.x + dx, y: w.y + dy }));
      childEdges.push(ce);
    } else if (src || dst) {
      if (src) {
        ce.from = nestedId;
        ce.fromField = null;
      } else {
        ce.to = nestedId;
        ce.toField = null;
      }
      ce.waypoints = [];
      parentEdges.push(ce);
      rewired.push(ce.id);
    } else {
      parentEdges.push(ce);
    }
  }
  const shared = outer.size === 1 ? [...outer][0] : null;
  return {
    movedIds: nodes.filter((n) => moved.has(n.id)).map((n) => n.id),
    childNodes: movedNodes,
    childEdges,
    parentNodes: nodes.filter((n) => !moved.has(n.id)),
    parentEdges,
    rewiredEdgeIds: rewired,
    bounds: b,
    groupId: shared,
  };
}

/** Frame of the nested node replacing a selection with `bounds` (size clamped to `GROUP_FRAME_MAX`). */
export function nestedNodeFrame(bounds) {
  const lim = M.SIZE_LIMITS.nodemap;
  const clamp = (v, lo, hi) => Math.round(Math.min(Math.max(v, lo), hi));
  return { x: Math.round(bounds.x), y: Math.round(bounds.y), w: clamp(bounds.w || lim.w, lim.minW, GROUP_FRAME_MAX.w), h: clamp(bounds.h || lim.h, lim.minH, GROUP_FRAME_MAX.h) };
}

/** Tours with moved node targets replaced by `nestedId` (deduplicated); edge targets kept in `edgeIds`. */
export function remapTours(tours, moved, nestedId, edgeIds) {
  const out = clone(tours || []);
  for (const t of out) {
    for (const s of t.steps || []) {
      const ids = [];
      for (const id of s.nodeIds || []) {
        const v = moved.has(id) ? nestedId : id;
        if (!ids.includes(v)) ids.push(v);
      }
      s.nodeIds = ids;
      s.edgeIds = (s.edgeIds || []).filter((e) => edgeIds.has(e));
    }
  }
  return out;
}

/**
 * Group `nodeIds` of the normalized `host` canvas into a new child canvas behind a new nested node
 * (port of Python `group_inline`). Mutates `host`; returns `{node, childId, child, movedIds, rewiredEdgeIds}`
 * where `child` still has to be stored under `childId` by the caller. `null` for an empty selection.
 */
export function groupIntoNested(host, nodeIds, { title = "Grouped NodeMap", childId = M.newId("nm"), version = "" } = {}) {
  const known = new Set((host.nodes || []).map((n) => n.id));
  const ids = nodeIds.filter((i) => known.has(i));
  if (!ids.length) return null;
  let nid = M.newId("n");
  while (known.has(nid)) nid = M.newId("n");
  const split = splitForGroup(host, ids, nid);
  const child = M.normalizeCanvas({
    ...M.emptyCanvas(),
    nodes: split.childNodes,
    edges: split.childEdges,
    settings: clone(host.settings || {}),
    viewport: M.fitViewport(M.boundsOf(split.childNodes), 1280, 800),
  }) || M.emptyCanvas();
  const label = String(title || "").trim().slice(0, 256) || "Grouped NodeMap";
  const node = M.createNode("nodemap", {
    ...nestedNodeFrame(split.bounds),
    id: nid,
    title: label,
    groupId: split.groupId,
    ...(version ? { version } : {}),
    data: { canvasId: childId, summary: summarizeCanvas(child, label, cachedDepth(child)), preview: buildStaticPreview(child) },
  });
  const edgeIds = new Set(split.parentEdges.map((e) => e.id));
  host.nodes = [...split.parentNodes, node];
  host.edges = split.parentEdges;
  host.tours = remapTours(host.tours || [], new Set(split.movedIds), nid, edgeIds);
  return { node, childId, child, movedIds: split.movedIds, rewiredEdgeIds: split.rewiredEdgeIds, movedEdgeCount: split.childEdges.length };
}
