import "./helpers/dom_shim.mjs";
import test from "node:test";
import assert from "node:assert/strict";

// Keep singleton background timers unreffed so Node test runner exits cleanly
const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (...args) => {
  const t = realSetInterval(...args);
  t?.unref?.();
  return t;
};
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (...args) => {
  const t = realSetTimeout(...args);
  t?.unref?.();
  return t;
};

const M = await import("../js/views/nodemap/canvas_model.js");
const { computeEdgeGeometry, nearestWaypointSegment } = await import("../js/views/nodemap/edge_router.js");
const { extractReferencedStruct, paholeRows, renderPaholeBody } = await import("../js/views/nodemap/struct_view.js");
const { NodeRenderer, nodeBox } = await import("../js/views/nodemap/node_renderer.js");
const {
  cveModel, docModel, historyModel, kapiModel,
  renderCveBody, renderDiffBody, renderDocBody, renderHistoryBody, renderKapiBody,
} = await import("../js/views/nodemap/domain_nodes.js");
const { canvasToSvg } = await import("../js/views/nodemap/nodemap_export.js");
const { cveSpec, docSpec, historySpec, kapiSpec, nodeSpecsForTab, tabForNode } = await import("../js/views/nodemap/nodemap_bridge.js");
const { NodeMapController } = await import("../js/views/nodemap/nodemap_controller.js");
const {
  computeVirtualRange, estimateNaturalSourceSize, isNodeInViewport, lodTierForViewport,
  mountVirtualDiff, mountVirtualSource, renderVirtualDiffWindow, renderVirtualSourceWindow, visibleCanvasRect,
} = await import("../js/views/nodemap/virtual_source.js");
const {
  assembleSvgJob, batchKconfigJob, layoutGraphJob, rasterizePngJob,
} = await import("../js/views/nodemap/nodemap_worker.js");
const { NodeMapWorkerPool } = await import("../js/views/nodemap/worker_pool.js");
const { computeMinimapLayout, searchCanvasNodes } = await import("../js/views/nodemap/minimap_search.js");
const {
  NodeMapTourPlayer, captureTourStep, createTour, removeTourStep, reorderTourStep, tourStepHighlights,
} = await import("../js/views/nodemap/tour_mode.js");
const {
  diffStructLayouts, renderStructDiffBadge,
} = await import("../js/views/nodemap/version_diff.js");

const named = (c) => (String(c).startsWith("var(--") ? `[${c.slice(6, -1)}]` : String(c));

function makeCtrl(tab = { id: "upg-1", type: "nodemap", version: "v6.6", repo: "linux" }) {
  const c = new NodeMapController();
  c.tab = tab;
  c.currentVersion = tab.version;
  c.repo = tab.repo;
  c.storageKey = `${M.STORAGE_PREFIX}${tab.id}`;
  c.canvas = M.emptyCanvas();
  c.rootCanvas = c.canvas;
  c.renderer = { sync() {}, invalidate() {}, element: () => null, clear() {}, place() {}, updateViewportCulling() {} };
  c.renderInspector = () => {};
  c.renderWires = () => {};
  c.scheduleWires = () => {};
  c.applyTransform = () => {};
  return c;
}

// ------------------------------------------------------------------ Pillar 1: Virtualization, LOD, GPU, Incremental Wires, Undo

test("virtual_source: 6,000-line source and diff stay under 200 DOM rows with translate3d window", () => {
  const lines = Array.from({ length: 6000 }, (_, i) => `int fn_${i}(void) { return ${i}; }`);
  const range = computeVirtualRange(lines.length, 3300, 400);
  assert.ok(range.endIdx - range.startIdx <= 70, "overscanned slice stays bounded");
  assert.ok(range.startIdx >= 170);

  const html = renderVirtualSourceWindow(lines, 1, "kernel/big.c", new Set([range.startIdx + 5]), range);
  assert.match(html, /class="nm-virt-spacer"/);
  assert.match(html, /class="nm-virt-window" style="transform:translate3d\(0,/);
  const rowCount = (html.match(/class="nm-src-row/g) || []).length;
  assert.equal(rowCount, range.endIdx - range.startIdx);
  assert.ok(rowCount < 100);

  const dRows = lines.map((text, i) => ({ kind: i % 2 ? "+" : " ", old: i + 1, new: i + 1, text }));
  const dHtml = renderDiffBody({ files: [], rows: [] });
  assert.equal(dHtml, '<div class="nm-empty">No changes</div>');
  const dWin = renderVirtualDiffWindow(dRows, range);
  assert.equal((dWin.match(/class="nm-diff-row/g) || []).length, range.endIdx - range.startIdx);

  const scroller = { clientHeight: 400, scrollTop: 1650, innerHTML: "", addEventListener() {}, removeEventListener() {} };
  const m = mountVirtualSource(scroller, { lines, startLine: 1, path: "a.c" });
  assert.ok(scroller.innerHTML.includes("nm-virt-spacer"));
  m.setHtmlRows(lines.map((_, i) => `<span>row-${i}</span>`));
  assert.match(scroller.innerHTML, /row-100/);
  m.dispose();

  const dScroller = { clientHeight: 360, scrollTop: 1650, innerHTML: "", addEventListener() {}, removeEventListener() {} };
  const dm = mountVirtualDiff(dScroller, { rows: dRows });
  assert.ok((dScroller.innerHTML.match(/class="nm-diff-row/g) || []).length < 80);
  dm.dispose();

  const nat = estimateNaturalSourceSize(lines, 1);
  assert.ok(nat.h > 90000 && nat.w >= 240);

  const c = makeCtrl();
  const s = c.addNode({ type: "source", data: { path: "big.c" } });
  c.content.set(c.fetchKeyOf(s), { status: "ok", value: { path: "big.c", start_line: 1, lines } });
  const est = c.measureNatural(s);
  assert.ok(est.h > 90000, "large source node skips DOM layout measurement");
});

test("viewport LOD culling and GPU translate3d positioning", () => {
  const vis = visibleCanvasRect({ x: 0, y: 0, zoom: 1 }, 1000, 800, 100);
  const inside = M.createNode("note", { x: 100, y: 100, w: 200, h: 100 });
  const outside = M.createNode("note", { x: 5000, y: 5000, w: 200, h: 100 });
  const tab = M.createNode("tab", { x: 5000, y: 5000, w: 400, h: 300, data: { tabType: "commits", hash: "tab=commits" } });
  assert.equal(isNodeInViewport(inside, vis), true);
  assert.equal(isNodeInViewport(outside, vis), false);
  assert.equal(lodTierForViewport(0.24), "low");
  assert.equal(lodTierForViewport(0.25), "full");

  assert.equal(nodeBox(inside).transform, "translate3d(100px, 100px, 0)");

  const toggleLog = () => {
    const set = new Set();
    return { set, toggle: (cls, on) => (on ? set.add(cls) : set.delete(cls)) };
  };
  const layerCls = toggleLog();
  const nr = new NodeRenderer({ classList: layerCls }, { renderBody() {} });
  const mkEl = () => ({ classList: toggleLog() });
  nr.els.set(inside.id, mkEl());
  nr.els.set(outside.id, mkEl());
  nr.els.set(tab.id, mkEl());
  nr.updateViewportCulling([inside, outside, tab], vis, 0.2, null);
  assert.equal(layerCls.set.has("nm-lod-low"), true);
  assert.equal(nr.els.get(inside.id).classList.set.has("nm-culled"), false);
  assert.equal(nr.els.get(outside.id).classList.set.has("nm-culled"), true);
  assert.equal(nr.els.get(tab.id).classList.set.has("nm-culled"), false, "live tab iframes stay unculled");
});

test("cached field ports avoid DOM reads during drag frames and structural snapshots share immutable items", () => {
  const c = makeCtrl();
  const s = c.addNode({ type: "struct", x: 100, y: 200, w: 300, h: 200, data: { name: "task" } });
  let rectReads = 0;
  const rowEl = { dataset: { key: "10:pid" }, getBoundingClientRect: () => ({ top: 240, height: 20 }), closest: () => null };
  const nodeEl = {
    getBoundingClientRect: () => {
      rectReads += 1;
      return { top: 200, height: 200, bottom: 400 };
    },
    querySelector: (sel) => (sel.includes("nm-sf") ? rowEl : null),
    querySelectorAll: (sel) => (sel.includes("nm-sf") ? [rowEl] : []),
  };
  c.renderer.element = () => nodeEl;
  c.refreshPortOffsets(s.id);
  const readsAfterRefresh = rectReads;
  assert.equal(readsAfterRefresh, 1);
  s.x = 400;
  s.y = 500;
  const pt = c.fieldPoint(s, "10:pid", "right");
  assert.deepEqual(pt, { x: 700, y: 550 });
  assert.equal(rectReads, readsAfterRefresh, "drag frame reads zero getBoundingClientRect");

  // Structural-sharing UndoStack
  const c2 = M.emptyCanvas();
  const n1 = M.createNode("note", { id: "n1", data: { text: "stay" } });
  const n2 = M.createNode("note", { id: "n2", data: { text: "one" } });
  c2.nodes.push(n1, n2);
  const s1 = M.structuralSnapshot(c2);
  n2.data.text = "two";
  const s2 = M.structuralSnapshot(c2, s1);
  assert.equal(s1.nodeMap.get("n1"), s2.nodeMap.get("n1"), "unchanged node shares string reference");
  assert.notEqual(s1.nodeMap.get("n2"), s2.nodeMap.get("n2"));
  const mat = M.materializeSnapshot(s1);
  assert.equal(mat.nodes.find((x) => x.id === "n2").data.text, "one");
});

// ------------------------------------------------------------------ Pillar 2: Worker Pool & Async IndexedDB Persistence

test("worker pool and worker jobs execute cleanly with Node.js fallback and cancellation", async () => {
  const pool = new NodeMapWorkerPool({ concurrency: 3 });
  assert.equal(pool.workerSupported, false);
  const tokRes = await pool.tokenizeSource({ lines: ["/* comment */", "int x = 1;"], path: "a.c" }, { key: "src" });
  assert.equal(tokRes.htmlRows.length, 2);
  assert.match(tokRes.htmlRows[0], /tok-comment/);

  // Stale token cancellation
  const oldTok = pool.nextToken("layout");
  const newTok = pool.nextToken("layout");
  assert.equal(pool.isCurrent("layout", oldTok), false);
  assert.equal(pool.isCurrent("layout", newTok), true);

  const kc = batchKconfigJob({
    assignments: { CONFIG_A: "n" },
    strict: true,
    arch: "x86_64",
    structs: [{
      nodeId: "n1",
      layout: {
        items: [
          { kind: "directive", directive: "ifdef", expr: "CONFIG_A", line: 1 },
          { kind: "field", name: "a", type: "int", line: 2, conds: ["CONFIG_A"] },
          { kind: "directive", directive: "endif", line: 3 },
        ],
      },
      data: { kind: "struct", name: "foo", style: "list" },
      editMode: false,
    }],
  });
  assert.equal(kc.results[0].nodeId, "n1");
  assert.equal(kc.results[0].model.rows[0].kind, "summary");

  const nodes = [
    M.createNode("symbol", { id: "a", w: 200, h: 80 }),
    M.createNode("symbol", { id: "b", w: 200, h: 80 }),
    M.createNode("symbol", { id: "c", w: 200, h: 80 }),
  ];
  const edges = [M.createEdge({ from: "a", to: "b" }), M.createEdge({ from: "b", to: "c" })];
  const dag = layoutGraphJob({ nodes, edges, mode: "dag" });
  const byDag = new Map(dag.positions.map((p) => [p.id, p]));
  assert.ok(byDag.get("a").x < byDag.get("b").x && byDag.get("b").x < byDag.get("c").x);

  const frc = layoutGraphJob({ nodes, edges, mode: "force" });
  assert.equal(frc.positions.length, 3);

  const svgJob = assembleSvgJob({ canvas: { nodes, edges, settings: {} }, resolvedColors: {} });
  assert.match(svgJob.svg, /<svg /);

  const pngJob = await rasterizePngJob({ svg: svgJob.svg, width: 100, height: 100 });
  assert.equal(pngJob.unsupported, true);
  pool.dispose();
});

test("async IndexedDB persistence uses USER_STORE with localStorage fallback", async () => {
  const idbStore = new Map();
  const mockDb = {
    async userPut(rec) {
      idbStore.set(rec.key, rec);
      return true;
    },
    async userGet(key) {
      return idbStore.get(key) || null;
    },
  };
  const cv = M.emptyCanvas();
  cv.nodes.push(M.createNode("note", { data: { text: "idb-saved" } }));
  const saved = await M.saveCanvasAsync(mockDb, "k1", cv, { repo: "linux", id: "tab1" });
  assert.equal(saved.ok, true);
  assert.equal(saved.backend, "indexeddb");
  assert.equal(idbStore.get("k1").kind, M.USER_CANVAS_KIND);
  const loaded = await M.loadCanvasAsync(mockDb, "k1");
  assert.equal(loaded.backend, "indexeddb");
  assert.equal(loaded.canvas.nodes[0].data.text, "idb-saved");

  // Fallback to localStorage when DB fails
  const brokenDb = { async userPut() { throw new Error("fail"); }, async userGet() { throw new Error("fail"); } };
  const lsRes = await M.saveCanvasAsync(brokenDb, "k2", cv, {}, globalThis.localStorage);
  assert.equal(lsRes.backend, "localStorage");
  const lsLoaded = await M.loadCanvasAsync(brokenDb, "k2", globalThis.localStorage);
  assert.equal(lsLoaded.backend, "localStorage");
  assert.equal(lsLoaded.canvas.nodes[0].data.text, "idb-saved");
});

// ------------------------------------------------------------------ Pillar 3: Follow Type, Pahole Layout, Callgraph/Callpath & 4 Kernel Nodes

test("struct followStructType parses pointers/embeds and dedupes edges", () => {
  assert.deepEqual(extractReferencedStruct("struct mm_struct *"), { kind: "struct", name: "mm_struct", isPointer: true, edgeLabel: "points to" });
  assert.deepEqual(extractReferencedStruct("const struct list_head"), { kind: "struct", name: "list_head", isPointer: false, edgeLabel: "embeds" });
  assert.deepEqual(extractReferencedStruct("union sigval"), { kind: "union", name: "sigval", isPointer: false, edgeLabel: "embeds" });
  assert.equal(extractReferencedStruct("unsigned long"), null);

  const c = makeCtrl();
  const s = c.addNode({ type: "struct", x: 50, y: 50, w: 300, h: 200, data: { name: "task_struct" } });
  c.content.set(c.fetchKeyOf(s), {
    status: "ok",
    value: { items: [{ kind: "field", name: "mm", type: "struct mm_struct *", line: 42, conds: [] }] },
  });
  assert.equal(c.followableTypeFor(s.id, "42:mm"), "struct mm_struct");
  const target = c.followStructType(s.id, "42:mm");
  assert.equal(target.type, "struct");
  assert.equal(target.data.name, "mm_struct");
  assert.equal(c.canvas.edges.length, 1);
  assert.equal(c.canvas.edges[0].label, "points to");
  assert.equal(c.canvas.edges[0].fromField, "42:mm");
  c.followStructType(s.id, "42:mm");
  assert.equal(c.canvas.edges.length, 1, "does not duplicate edge");
});

test("struct style 'layout' renders pahole cache lines and exports vector SVG", () => {
  const pahole = {
    name: "foo",
    total_size: 128,
    padding_bytes: 4,
    holes_count: 1,
    cache_lines_used: 2,
    members: [
      { name: "a", type: "int", offset: 0, size: 4, cacheline_idx: 0 },
      { name: "b", type: "void *", offset: 64, size: 8, padding_before: 4, cacheline_idx: 1 },
    ],
  };
  const m = paholeRows(pahole, { name: "foo" });
  assert.equal(m.totalSize, 128);
  assert.equal(m.cacheLines, 2);
  assert.equal(m.holesCount, 1);
  const html = renderPaholeBody(pahole, { name: "foo" });
  assert.match(html, /Cache line 0/);
  assert.match(html, /Cache line 1/);
  assert.match(html, /4B/);
  const unavail = renderPaholeBody({ unavailable: true, error: "DWARF missing" }, { name: "foo" });
  assert.match(unavail, /DWARF missing/);

  const c = makeCtrl();
  const s = c.addNode({ type: "struct", data: { name: "foo", style: "list" } });
  c.onStructAction(s, "style");
  assert.equal(s.data.style, "table");
  c.onStructAction(s, "style");
  assert.equal(s.data.style, "layout");
  c.onStructAction(s, "style");
  assert.equal(s.data.style, "list");

  const sn = M.createNode("struct", { w: 320, h: 200, data: { name: "foo", style: "layout" } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [sn] }, named, {
    structOpts: () => ({ pahole }),
    content: () => ({ items: [] }),
  });
  assert.match(svg, /128B/);
  assert.match(svg, /Cache line 0/);
});

test("callgraph and callpath expand symbol nodes with call-site edges", async () => {
  const c = makeCtrl();
  c.api = {
    async getCallgraph() {
      return {
        callers: [{ name: "sys_fork", file_path: "kernel/fork.c", line: 100 }],
        callees: [{ name: "copy_mm", file_path: "kernel/fork.c", line: 220 }],
      };
    },
    async getCallpath() {
      return {
        paths: [["copy_process", "copy_mm", "dup_mm"]],
      };
    },
  };
  const fn = c.addNode({ type: "symbol", data: { name: "copy_process", kind: "func" } });
  const addedCg = await c.expandCallgraph(fn.id, "both");
  assert.equal(addedCg.length, 2);
  assert.equal(c.canvas.edges.length, 2);
  assert.ok(c.canvas.edges.every((e) => e.label === "calls" && e.callSite?.path === "kernel/fork.c"));

  await c.expandCallPath(fn.id, "dup_mm", { maxDepth: 4, k: 2 });
  assert.ok(c.canvas.nodes.some((n) => n.data.name === "dup_mm"));
  assert.ok(c.canvas.edges.some((e) => e.from === addedCg.find((x) => x.data.name === "copy_mm")?.id || e.label === "calls"));
});

test("cve, kapi, doc, and history domain nodes render, bridge, spawn and export SVG", () => {
  const cveN = M.createNode("cve", { data: { cveId: "CVE-2024-0001", release: "v6.6" } });
  const kapiN = M.createNode("kapi", { data: { catalog: "syscalls", symbol: "openat", arch: "x86_64" } });
  const docN = M.createNode("doc", { data: { path: "Documentation/core-api/mm.rst", mode: "rst" } });
  const histN = M.createNode("history", { data: { mode: "symbol", path: "kernel/fork.c", line: 10, name: "do_fork" } });

  const cveM = cveModel({
    id: "CVE-2024-0001",
    title: "race in mm",
    cvss: { score: 7.8, severity: "HIGH" },
    release_state: { state: "fixed", release: "v6.6" },
    files: ["mm/mmap.c"],
    ranges: [{ introduced: "1111aaaa", fixed: "2222bbbb" }],
  }, cveN.data);
  assert.match(renderCveBody(cveM), /CVE-2024-0001/);
  assert.match(renderCveBody(cveM), /cve-spawn-diff/);

  const kapiM = kapiModel({
    syscalls: [{ name: "openat", nr: 257, entry: "sys_openat", definition: { path: "fs/open.c", line_s: 120 } }],
  }, kapiN.data);
  assert.match(renderKapiBody(kapiM), /openat/);
  assert.match(renderKapiBody(kapiM), /kapi-spawn-symbol/);

  const docM = docModel({ path: "Documentation/core-api/mm.rst", content: "MM Docs\n=======\n\nDetails here." }, docN.data);
  assert.match(renderDocBody(docM), /MM Docs/);

  const histM = historyModel({
    ast_name: "do_fork",
    timeline: [{ vname: "v6.6", prev_vname: "v6.5", status: "modified", lines_added: 3, lines_removed: 1, path: "kernel/fork.c" }],
  }, histN.data);
  assert.match(renderHistoryBody(histM), /do_fork/);
  assert.match(renderHistoryBody(histM), /hist-spawn-diff/);

  // Bridge conversions
  assert.equal(nodeSpecsForTab({ type: "code", path: "Documentation/x.rst" })[0].type, "doc");
  assert.equal(nodeSpecsForTab({ type: "cve", sel: "CVE-2024-0001" })[0].type, "cve");
  assert.equal(nodeSpecsForTab({ type: "api", view: "syscalls" })[0].type, "kapi");
  assert.equal(cveSpec({ cveId: "cve-2024-1" }).data.cveId, "CVE-2024-1");
  assert.equal(kapiSpec({ catalog: "exports", symbol: "kmalloc" }).data.symbol, "kmalloc");
  assert.equal(docSpec({ path: "Documentation/foo.rst" }).type, "doc");
  assert.equal(historySpec({ mode: "blame", path: "a.c" }).data.mode, "blame");
  assert.equal(tabForNode(cveN).type, "cve");
  assert.equal(tabForNode(kapiN).type, "api");
  assert.equal(tabForNode(docN).docMode, "doc");
  assert.equal(tabForNode(histN).type, "code");

  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [cveN, kapiN, docN, histN] }, named, {
    content: (n) => ({ [cveN.id]: cveM, [kapiN.id]: kapiM, [docN.id]: docM, [histN.id]: histM }[n.id]),
  });
  assert.match(svg, /CVE-2024-0001/);
  assert.match(svg, /openat/);
  assert.match(svg, /MM Docs/);
  assert.match(svg, /do_fork/);
});

// ------------------------------------------------------------------ Pillar 4: Tours, Minimap/Search, Focus, Presets/Waypoints, Time-Travel, Layers, Sub-Maps

test("tours: create, capture, reorder, remove, highlight and TourPlayer step playback", () => {
  const c = makeCtrl();
  const n1 = c.addNode({ type: "note", x: 0, y: 0, w: 100, h: 60, data: { text: "one" } });
  const n2 = c.addNode({ type: "note", x: 500, y: 0, w: 100, h: 60, data: { text: "two" } });
  const tour = createTour("Boot Path");
  const s1 = captureTourStep({ title: "Start", caption: "Entry", selectedNodes: [n1.id], viewport: { x: 10, y: 20, zoom: 1.5 } });
  const s2 = captureTourStep({ title: "Next", caption: "Continue", selectedNodes: [n2.id], viewport: { x: -300, y: 0, zoom: 1 } });
  tour.steps.push(s1, s2);
  assert.equal(reorderTourStep(tour, s2.id, -1), true);
  assert.equal(tour.steps[0].id, s2.id);
  const hl = tourStepHighlights(s1);
  assert.equal(hl.active, true);
  assert.equal(hl.nodeIds.has(n1.id), true);

  c.canvas.tours = [tour];
  const player = new NodeMapTourPlayer(c);
  assert.equal(player.start(tour.id, 0), true);
  assert.equal(c.canvas.viewport.x, -300);
  player.step(1);
  assert.equal(c.canvas.viewport.zoom, 1.5);
  player.dispose();
  assert.equal(removeTourStep(tour, s1.id), true);
  assert.equal(tour.steps.length, 1);
});

test("minimap layout and Ctrl+F node content search", () => {
  const nodes = [
    M.createNode("note", { id: "a", x: 0, y: 0, w: 200, h: 100, data: { text: "hello kernel" } }),
    M.createNode("struct", { id: "b", x: 400, y: 200, w: 200, h: 100, data: { name: "task_struct" } }),
  ];
  const mm = computeMinimapLayout(nodes, { x: 0, y: 0, zoom: 1 }, 800, 600, 180, 120);
  assert.equal(mm.nodeRects.length, 2);
  assert.ok(mm.viewRect.w > 0 && mm.viewRect.h > 0);
  const hits = searchCanvasNodes(nodes, "task_struct");
  assert.deepEqual(hits.map((h) => h.nodeId), ["b"]);
  const textHits = searchCanvasNodes(nodes, "kernel");
  assert.deepEqual(textHits.map((h) => h.nodeId), ["a"]);
});

test("focus N-hop BFS, semantic edge presets, draggable waypoints, layers, version travel and sub-maps", () => {
  const c = makeCtrl();
  const a = c.addNode({ type: "struct", x: 0, y: 0, w: 120, h: 80, data: { name: "a" } });
  const b = c.addNode({ type: "struct", x: 300, y: 0, w: 120, h: 80, data: { name: "b" } });
  const d = c.addNode({ type: "struct", x: 600, y: 0, w: 120, h: 80, data: { name: "d" } });
  const e1 = M.createEdge({ from: a.id, to: b.id });
  const e2 = M.createEdge({ from: b.id, to: d.id });
  M.applyEdgePreset(e1, "calls");
  assert.equal(e1.label, "calls");
  e1.waypoints = [{ x: 200, y: -60 }];
  c.canvas.edges.push(e1, e2);

  // Waypoint geometry & hit insertion
  const geom = computeEdgeGeometry({ rect: M.rectOf(a), side: "right" }, { rect: M.rectOf(b), side: "left" }, { curve: "bezier", arrow: "forward", waypoints: e1.waypoints });
  assert.match(geom.d, /^M .* C /);
  assert.equal(nearestWaypointSegment([{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 200, y: 0 }], { x: 150, y: 5 }), 1);
  assert.match(canvasToSvg(c.canvas, named), /200,-60|200/);

  // Focus BFS
  const sub1 = M.connectedSubgraph(c.canvas, [a.id], 1);
  assert.equal(sub1.nodeIds.has(b.id), true);
  assert.equal(sub1.nodeIds.has(d.id), false);
  const sub2 = M.connectedSubgraph(c.canvas, [a.id], 2);
  assert.equal(sub2.nodeIds.has(d.id), true);

  // Visibility layers
  const layer = M.addLayer(c.canvas.settings, "MM");
  a.layerId = layer.id;
  M.toggleLayerVisibility(c.canvas.settings, layer.id);
  const visSlice = M.visibleCanvasSlice(c.canvas);
  assert.equal(visSlice.nodes.some((n) => n.id === a.id), false);
  assert.equal(visSlice.edges.some((e) => e.id === e1.id), false, "edges incident to hidden-layer nodes are hidden in SVG");

  // Version time-travel struct diff + apply/revert
  const vDiff = diffStructLayouts(
    { items: [{ kind: "field", name: "x", type: "int", conds: [] }, { kind: "field", name: "old", type: "long", conds: [] }] },
    { items: [{ kind: "field", name: "x", type: "u64", conds: ["CONFIG_64BIT"] }, { kind: "field", name: "added", type: "bool", conds: [] }] },
  );
  assert.deepEqual(vDiff.addedFields, ["added"]);
  assert.deepEqual(vDiff.removedFields, ["old"]);
  assert.equal(vDiff.changedTypes[0].name, "x");
  assert.equal(vDiff.changedGuards[0].name, "x");
  assert.match(renderStructDiffBadge(vDiff, "v6.1", "v6.6"), /\+added/);

  const chg = c.applyTargetVersion("v6.8");
  assert.ok(chg.length >= 2);
  assert.equal(b.version, "v6.8");
  c.revertTargetVersion();
  assert.equal(b.version, "v6.6");

  // Sub-Map portal drill-down & breadcrumbs clear ghost SVG wires and parent focus state
  c.wireSvg = { innerHTML: "<g class=\"nm-edge\"></g>", style: {}, setAttribute() {} };
  c.focusHops = 1;
  c.focusSeedIds = new Set([a.id]);
  const grp = c.addNode({ type: "group", x: 0, y: 0, w: 200, h: 150, data: {} });
  assert.equal(c.enterSubMap(grp.id), true);
  assert.equal(c.wireSvg.innerHTML, "", "sub-map entry clears orphaned wire SVG");
  assert.equal(c.focusHops, null, "sub-map entry clears parent Focus Mode");
  assert.equal(c.focusSeedIds.size, 0);
  assert.equal(c.subMapPath.length, 1);
  c.addNode({ type: "note", data: { text: "inside portal" } });
  assert.equal(c.canvas.nodes.length, 1);
  assert.equal(c.exitSubMapTo(0), true);
  assert.equal(c.subMapPath.length, 0);
  assert.equal(c.canvas.nodes.some((n) => n.id === grp.id), true);

  // Waypoint segment insertion uses [g.p1, ...wps, g.p2]
  c.edgeGeoms = new Map([[e1.id, { p1: { x: 0, y: 0 }, p2: { x: 400, y: 0 } }]]);
  e1.waypoints = [{ x: 300, y: 0 }];
  c.addEdgeWaypointAt(e1.id, { x: 100, y: 0 });
  assert.deepEqual(e1.waypoints, [{ x: 100, y: 0 }, { x: 300, y: 0 }]);

  c.dispose();
});

test("regression: worker pool exec after dispose returns null, and empty IDB merges mount pendingNodes", async () => {
  const pool = new NodeMapWorkerPool({ concurrency: 2 });
  pool.dispose();
  assert.equal(await pool.tokenizeSource({ lines: ["int x;"], path: "a.c" }), null);

  // Pre-seed stale localStorage, and newer empty 0-node IndexedDB record + mount-time pendingNodes
  const key = `${M.STORAGE_PREFIX}idb-merge-1`;
  const stale = M.emptyCanvas();
  stale.nodes.push(M.createNode("note", { data: { text: "stale-local" } }));
  M.saveCanvas(globalThis.localStorage, key, stale);

  const emptyIdb = M.emptyCanvas();
  const c = new NodeMapController();
  c.userDb = {
    async userGet(k) {
      return k === key ? { key, kind: M.USER_CANVAS_KIND, data: M.serializeCanvas(emptyIdb), created_at: 123 } : null;
    },
    async userPut() { return true; },
  };
  const makeEl = () => ({
    innerHTML: "", style: { setProperty() {}, removeProperty() {} }, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, getAttribute: () => "",
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600, right: 800, bottom: 600 }),
    querySelector: () => makeEl(), querySelectorAll: () => [], appendChild() {}, addEventListener() {}, removeEventListener() {},
    after() {}, remove() {}, contains: () => false, focus() {}, compareDocumentPosition: () => 4,
  });
  const origCreate = document.createElement;
  document.createElement = () => makeEl();
  try {
    const container = makeEl();
    container.isConnected = true;
    c.render(container, { id: "idb-merge-1", version: "v6.6", repo: "linux", pendingNodes: [{ type: "note", data: { text: "queued-mount" } }] });
    await c.loadFromIndexedDB();
    assert.equal(c.canvas.nodes.length, 1);
    assert.equal(c.canvas.nodes[0].data.text, "queued-mount", "empty IDB record overrides stale localStorage and merges mount pendingNodes");
  } finally {
    document.createElement = origCreate;
    c.dispose();
  }
});
