/**
 * nodemap_tracks.test.mjs - Unit tests for all 5 NodeMap refactor & feature tracks:
 *   1. Controller decomposition & delegation parity
 *   2. Named Canvas Library (USER_STORE + localStorage), orphan pruning & 4 kernel starter templates
 *   3. Recursive sub-map search, smart snap alignment guides & power-user shortcuts
 *   4. Interactive HTML (XSS-safe offline pan/zoom + Code Tour), Mermaid (.mmd) & Graphviz (.dot)
 *   5. Automated Patch/Commit Impact Maps & first-class Locking/Concurrency annotations
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

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
const { NodeMapController } = await import("../js/views/nodemap/nodemap_controller.js");
const { pickerValue } = await import("../js/views/nodemap/nodemap_inspector.js");
const { computeAlignmentGuides, hasTextSelection } = await import("../js/views/nodemap/nodemap_gestures.js");
const { SHORTCUT_SECTIONS } = await import("../js/views/nodemap/nodemap_dialogs.js");
const { hasIndexedDbBackend, pruneOrphanedCanvases } = await import("../js/views/nodemap/nodemap_persistence.js");
const {
  KERNEL_TEMPLATES, buildKernelTemplate, collectReferencedTabIds, deleteNamedCanvas,
  duplicateNamedCanvas, findOrphanedTabCanvases, listNamedCanvases, loadNamedCanvas,
  pruneOrphanedTabCanvases, renameNamedCanvas, saveNamedCanvas,
} = await import("../js/views/nodemap/nodemap_library.js");
const {
  canvasToDot, canvasToInteractiveHtml, canvasToMermaid, escapeJsonForScript,
} = await import("../js/views/nodemap/nodemap_export_formats.js");
const { buildImpactMap, extractImpactFromDiff, generateImpactMap } = await import("../js/views/nodemap/impact_map.js");
const { NodeMapSearch, searchCanvasNodes } = await import("../js/views/nodemap/minimap_search.js");
const { NodeRenderer } = await import("../js/views/nodemap/node_renderer.js");
const { renderPaholeBody, renderStructBody } = await import("../js/views/nodemap/struct_view.js");
const { canvasToSvg } = await import("../js/views/nodemap/nodemap_export.js");

function makeMemoryStorage() {
  const m = new Map();
  return {
    _m: m,
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(String(k), String(v)),
    removeItem: (k) => m.delete(String(k)),
    key: (i) => [...m.keys()][i] ?? null,
    get length() { return m.size; },
    clear: () => m.clear(),
  };
}

function makeMockUserDb() {
  const store = new Map();
  return {
    _store: store,
    async userPut(rec) {
      store.set(rec.key, structuredClone(rec));
      return true;
    },
    async userGet(key) {
      return store.has(key) ? structuredClone(store.get(key)) : null;
    },
    async userList(kind = null, repo = null) {
      return [...store.values()]
        .filter((r) => (!kind || r.kind === kind) && (repo === null || repo === undefined || r.repo === repo))
        .map((r) => structuredClone(r));
    },
    async userDelete(key) {
      return store.delete(key);
    },
  };
}

function makeFakeElement(tag = "div") {
  const cls = new Set();
  const attrs = new Map();
  const listeners = new Map();
  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    style: {
      _props: new Map(),
      setProperty(k, v) { this._props.set(k, v); },
      getPropertyValue(k) { return this._props.get(k) || ""; },
      removeProperty(k) { this._props.delete(k); },
    },
    dataset: {},
    hidden: false,
    _innerHTML: "",
    textContent: "",
    isConnected: true,
    classList: {
      add: (...ks) => ks.forEach((k) => cls.add(k)),
      remove: (...ks) => ks.forEach((k) => cls.delete(k)),
      toggle: (k, on) => (on === undefined ? (cls.has(k) ? cls.delete(k) : cls.add(k)) : (on ? cls.add(k) : cls.delete(k))),
      contains: (k) => cls.has(k),
    },
    get className() { return [...cls].join(" "); },
    set className(v) { cls.clear(); String(v || "").split(/\s+/).filter(Boolean).forEach((k) => cls.add(k)); },
    setAttribute(k, v) {
      if (k === "class") this.className = v;
      else attrs.set(k, String(v));
    },
    getAttribute(k) { return k === "class" ? this.className : (attrs.get(k) ?? null); },
    appendChild(c) { this.children.push(c); return c; },
    remove() {},
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    dispatch(type, ev = {}) {
      for (const fn of listeners.get(type) || []) fn(ev);
    },
    querySelector(sel) {
      if (sel === ".nm-guides") return this.children.find((c) => c.classList.contains("nm-guides")) || null;
      return null;
    },
    querySelectorAll() { return []; },
    closest() { return null; },
    focus() {},
    select() {},
  };
  return el;
}

function makeController(tab = { id: "trk-tab-1", type: "nodemap", version: "v6.6", repo: "linux" }) {
  const c = new NodeMapController();
  c.tab = tab;
  c.currentVersion = tab.version;
  c.repo = tab.repo;
  c.storageKey = `${M.STORAGE_PREFIX}${tab.id}`;
  c.canvas = M.emptyCanvas();
  c.rootCanvas = c.canvas;
  c.history.reset(M.structuralSnapshot(c.canvas));
  c.rootEl = makeFakeElement("div");
  c.wrapEl = { ...makeFakeElement("div"), getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 700 }) };
  c.layerEl = makeFakeElement("div");
  c.wireSvg = { style: {}, setAttribute() {}, innerHTML: "" };
  c.marqueeEl = makeFakeElement("div");
  c.inspectorEl = makeFakeElement("div");
  c.elements = new Map();
  c.renderer = {
    sync(nodes) {
      for (const n of nodes) {
        if (!c.elements.has(n.id)) c.elements.set(n.id, makeFakeElement("div"));
      }
    },
    place() {},
    invalidate() {},
    element: (id) => c.elements.get(id) || null,
    clear() {},
  };
  return c;
}

// ------------------------------------------------------------------ Track 1: Decomposition & Exports

test("Track 1: extracted modules export expected APIs and controller delegates cleanly", () => {
  assert.equal(typeof pickerValue, "function");
  assert.equal(pickerValue("#abcdef"), "#abcdef");
  assert.equal(typeof computeAlignmentGuides, "function");
  assert.equal(typeof hasTextSelection, "function");
  assert.ok(Array.isArray(SHORTCUT_SECTIONS) && SHORTCUT_SECTIONS.length >= 3);
  assert.equal(typeof hasIndexedDbBackend, "function");
  assert.equal(typeof pruneOrphanedCanvases, "function");

  const c = makeController();
  c.userDb = makeMockUserDb();
  assert.equal(c._hasIndexedDbBackend(), true);
  assert.equal(c.embedKey("nms-1"), "trk-tab-1:nms-1");
  assert.equal(hasIndexedDbBackend({ userDb: null }), false);

  const downloads = [];
  c.download = (name, blob) => downloads.push({ name, blob });
  c.addNode({ type: "note", data: { text: "Hello" } });
  c.exportMermaid();
  c.exportDot();
  c.exportInteractiveHTML({ title: "Test" });
  assert.deepEqual(downloads.map((d) => d.name), ["nodemap_canvas.mmd", "nodemap_canvas.dot", "nodemap_canvas.html"]);
});

// ------------------------------------------------------------------ Track 2: Library, Orphan Pruning & 4 Templates

test("Track 2: Named Canvas Library CRUD works in IndexedDB USER_STORE and localStorage fallback", async () => {
  const userDb = makeMockUserDb();
  const ls = makeMemoryStorage();
  const doc = M.emptyCanvas();
  doc.nodes.push(M.createNode("note", { data: { text: "Saved" } }));

  const saved = await saveNamedCanvas(userDb, { name: "VFS Locking", repo: "linux", canvas: doc }, ls);
  assert.equal(saved.ok, true);
  assert.equal(saved.backend, "indexeddb");
  assert.ok(saved.key.startsWith("kip-nodemap-named:"));

  const list1 = await listNamedCanvases(userDb, { repo: "linux" }, ls);
  assert.equal(list1.length, 1);
  assert.equal(list1[0].name, "VFS Locking");
  assert.equal(list1[0].nodeCount, 1);

  const loaded = await loadNamedCanvas(userDb, saved.key, ls);
  assert.equal(loaded.canvas.nodes.length, 1);

  const renamed = await renameNamedCanvas(userDb, saved.key, "Renamed VFS Map", ls);
  assert.equal(renamed.name, "Renamed VFS Map");

  const dup = await duplicateNamedCanvas(userDb, saved.key, "", ls);
  assert.ok(dup.key !== saved.key);
  assert.equal(dup.name, "Renamed VFS Map (Copy)");
  assert.equal((await listNamedCanvases(userDb, { repo: "linux" }, ls)).length, 2);

  assert.equal(await deleteNamedCanvas(userDb, saved.key, ls), true);
  assert.equal((await listNamedCanvases(userDb, { repo: "linux" }, ls)).length, 1);

  // Fallback when userDb is unavailable
  const fbSaved = await saveNamedCanvas(null, { name: "LS Fallback", repo: "linux", canvas: doc }, ls);
  assert.equal(fbSaved.ok, true);
  assert.equal(fbSaved.backend, "localStorage");
  const fbList = await listNamedCanvases(null, { repo: "linux" }, ls);
  assert.equal(fbList.some((r) => r.key === fbSaved.key), true);
});

test("Track 2: orphan pruning preserves open/session tabs and recent caches, deletes stale orphans", async () => {
  const userDb = makeMockUserDb();
  const ls = makeMemoryStorage();
  const now = 1_800_000_000_000;
  const oldTs = now - 20 * 24 * 60 * 60 * 1000;
  const recentTs = now - 2 * 24 * 60 * 60 * 1000;

  await userDb.userPut({ key: `${M.STORAGE_PREFIX}open-tab`, kind: M.USER_CANVAS_KIND, updated_at: oldTs });
  await userDb.userPut({ key: `${M.STORAGE_PREFIX}saved-sess-tab`, kind: M.USER_CANVAS_KIND, updated_at: oldTs });
  await userDb.userPut({ key: `${M.STORAGE_PREFIX}recent-orphan`, kind: M.USER_CANVAS_KIND, updated_at: recentTs });
  await userDb.userPut({ key: `${M.STORAGE_PREFIX}stale-orphan`, kind: M.USER_CANVAS_KIND, updated_at: oldTs });
  ls.setItem(`${M.STORAGE_PREFIX}stale-ls-orphan`, JSON.stringify({ updated_at: oldTs }));

  const activeIds = collectReferencedTabIds({
    openPanes: [{ id: "p1", tabs: [{ id: "open-tab" }] }],
    sessionsIndex: [{ id: "s1" }],
    readSession: (sid) => (sid === "s1" ? { panes: [{ id: "p2", tabs: [{ id: "saved-sess-tab" }] }] } : null),
  });
  assert.deepEqual([...activeIds].sort(), ["open-tab", "saved-sess-tab"]);

  const orphans = await findOrphanedTabCanvases(userDb, { activeTabIds: activeIds, now, fallbackStorage: ls });
  assert.deepEqual(orphans.map((o) => o.tabId).sort(), ["stale-ls-orphan", "stale-orphan"]);

  const res = await pruneOrphanedTabCanvases(userDb, { activeTabIds: activeIds, now, fallbackStorage: ls });
  assert.equal(res.prunedCount, 2);
  assert.ok(await userDb.userGet(`${M.STORAGE_PREFIX}open-tab`));
  assert.ok(await userDb.userGet(`${M.STORAGE_PREFIX}saved-sess-tab`));
  assert.ok(await userDb.userGet(`${M.STORAGE_PREFIX}recent-orphan`));
  assert.equal(await userDb.userGet(`${M.STORAGE_PREFIX}stale-orphan`), null);
  assert.equal(ls.getItem(`${M.STORAGE_PREFIX}stale-ls-orphan`), null);
});

test("Track 2: 4 kernel starter templates build valid canvases and loadCanvasDocument is undoable", () => {
  assert.equal(KERNEL_TEMPLATES.length, 4);
  for (const meta of KERNEL_TEMPLATES) {
    const tpl = buildKernelTemplate(meta.id, { version: "v6.8", repo: "linux" });
    assert.ok(tpl, `template ${meta.id} builds`);
    assert.equal(tpl.schema, M.CANVAS_SCHEMA);
    assert.ok(tpl.nodes.length >= 4);
    assert.ok(tpl.edges.length >= 3);
    assert.equal(tpl.tours.length, 1);
    assert.equal(tpl.tours[0].steps.length, 3);
  }
  const lockTpl = buildKernelTemplate("struct-locking");
  assert.ok(lockTpl.nodes.some((n) => n.locks?.length > 0));
  assert.ok(lockTpl.nodes.some((n) => Object.keys(n.data?.fieldLocks || {}).length > 0));
  assert.ok(lockTpl.edges.some((e) => Boolean(e.lockKind)));

  const subTpl = buildKernelTemplate("subsystem-overview");
  assert.ok(subTpl.nodes.some((n) => n.type === "group" && n.data?.subCanvas?.nodes?.length > 0));
  assert.ok(subTpl.settings.layers.length >= 2);
  assert.equal(buildKernelTemplate("non-existent"), null);

  // Undoable loadCanvasDocument
  const c = makeController();
  const orig = c.addNode({ type: "note", data: { text: "Original" } });
  c.loadCanvasDocument(lockTpl);
  assert.equal(c.canvas.nodes.length, lockTpl.nodes.length);
  assert.equal(c.undo(), true);
  assert.equal(c.canvas.nodes.length, 1);
  assert.equal(c.canvas.nodes[0].id, orig.id);
});

// ------------------------------------------------------------------ Track 3: UX & Authoring Polish

test("Track 3: recursive sub-map Ctrl+F search returns breadcrumbs and navigates into sub-maps", () => {
  const deepLeaf = M.createNode("struct", { data: { name: "dentry", kind: "struct" }, locks: [{ kind: "rcu", lockName: "d_lock" }] });
  const innerGroup = M.createNode("group", {
    customTitle: "dentry cache",
    data: { subCanvas: { ...M.emptyCanvas(), nodes: [deepLeaf] } },
  });
  const outerGroup = M.createNode("group", {
    customTitle: "VFS",
    data: { subCanvas: { ...M.emptyCanvas(), nodes: [innerGroup] } },
  });
  const root = M.normalizeCanvas({ ...M.emptyCanvas(), nodes: [outerGroup] });

  const hits = searchCanvasNodes(root.nodes, "d_lock");
  assert.equal(hits.length, 1);
  assert.equal(hits[0].nodeId, deepLeaf.id);
  assert.deepEqual(hits[0].subMapPath, [outerGroup.id, innerGroup.id]);
  assert.deepEqual(hits[0].trailTitles, ["VFS", "dentry cache"]);
  assert.equal(hits[0].breadcrumb, "VFS › dentry cache › struct dentry");

  const c = makeController();
  c.rootCanvas = root;
  c.canvas = root;
  const search = new NodeMapSearch(c);
  search.el = makeFakeElement("div");
  search.inputEl = { value: "dentry", focus() {}, select() {} };
  search.countEl = makeFakeElement("span");
  search.crumbEl = makeFakeElement("span");

  const results = search.update();
  assert.ok(results.length >= 2);
  // Step to deepLeaf match inside VFS › dentry cache
  let steppedToDeep = null;
  for (let i = 0; i < results.length; i++) {
    const hit = search.step(1);
    if (hit.nodeId === deepLeaf.id) { steppedToDeep = hit; break; }
  }
  assert.ok(steppedToDeep);
  assert.deepEqual(c.subMapPath, [outerGroup.id, innerGroup.id]);
  assert.equal(c.byId(deepLeaf.id)?.data?.name, "dentry");
  assert.equal(search.crumbEl.textContent, "VFS › dentry cache");
});

test("Track 3: smart snap alignment guides snap within threshold and clear on pointerup", () => {
  const moving = [{ x: 103, y: 204, w: 100, h: 60 }];
  const other = [{ x: 100, y: 400, w: 120, h: 80 }, { x: 400, y: 200, w: 100, h: 60 }];
  const res = computeAlignmentGuides(moving, other, 6);
  assert.equal(res.dx, -3);
  assert.equal(res.dy, -4);
  assert.equal(res.guides.length, 2);

  const locked = computeAlignmentGuides(moving, other, 6, { lockX: true, lockY: false });
  assert.equal(locked.dx, 0);
  assert.equal(locked.dy, -4);

  // Gesture integration renders .nm-guide-line into ctrl.guideEl then clears on pointerup
  const c = makeController();
  c.canvas.settings.snap = false;
  const n1 = c.addNode({ type: "note", x: 100, y: 300, w: 100, h: 60, data: { text: "Anchor" } });
  const n2 = c.addNode({ type: "note", x: 104, y: 100, w: 100, h: 60, data: { text: "Mover" } });
  Object.assign(n1, { x: 100, y: 300, w: 100, h: 60 });
  Object.assign(n2, { x: 104, y: 100, w: 100, h: 60 });
  const head = { closest: (s) => (s === ".nm-node-head" ? {} : s === ".nm-node" ? { dataset: { nodeId: n2.id } } : null) };
  c.onPointerDown({ button: 0, clientX: 0, clientY: 0, preventDefault() {}, target: head });
  c.onPointerMove({ buttons: 1, clientX: 0, clientY: 20 });
  assert.equal(n2.x, 100, "snapped left edge to n1.x = 100");
  assert.ok(c.guideEl && !c.guideEl.hidden && c.guideEl.innerHTML.includes("nm-guide-line"));
  c.onPointerUp({ type: "pointerup" });
  assert.equal(c.guideEl.hidden, true);
  assert.equal(c.guideEl.innerHTML, "");

  // When grid snap is active + Shift+Drag locks axis, locked axis does NOT snap to grid and guide lines stay hidden
  c.canvas.settings.snap = true;
  c.canvas.settings.grid = 16;
  Object.assign(n2, { x: 103, y: 107 });
  c.onPointerDown({ button: 0, shiftKey: true, clientX: 0, clientY: 0, preventDefault() {}, target: head });
  c.onPointerMove({ buttons: 1, shiftKey: true, clientX: 40, clientY: 6 });
  assert.equal(n2.y, 107, "Shift+Drag locked axis stays at 107 even with grid snap active");
  assert.equal(c.guideEl.hidden, true, "alignment guides stay cleared when grid snap is active");
  c.onPointerUp({ type: "pointerup" });
});

test("Track 3: power-user shortcuts (Alt+Drag, Shift+Drag, F2, Ctrl+0, Ctrl+1, ?)", () => {
  const c = makeController();
  c.canvas.settings.snap = false;
  const n = c.addNode({ type: "note", x: 100, y: 100, w: 160, h: 80, data: { text: "Orig" } });
  Object.assign(n, { x: 100, y: 100, w: 160, h: 80 });

  // Shift+Drag locks dominant axis
  const targetFor = (id) => ({
    closest: (s) => (s === ".nm-node-head" ? {} : s === ".nm-node" ? { dataset: { nodeId: id } } : null),
  });
  c.selectOnly(n.id);
  c.onPointerDown({ button: 0, shiftKey: true, clientX: 0, clientY: 0, preventDefault() {}, target: targetFor(n.id) });
  c.onPointerMove({ buttons: 1, shiftKey: true, clientX: 40, clientY: 8 });
  assert.equal(n.x, 140);
  assert.equal(n.y, 100, "vertical delta locked to 0 when horizontal is dominant");
  c.onPointerUp({ type: "pointerup" });

  // Alt+Drag clones selection on first drag past threshold and leaves original intact
  c.onPointerDown({ button: 0, altKey: true, clientX: 0, clientY: 0, preventDefault() {}, target: targetFor(n.id) });
  c.onPointerMove({ buttons: 1, altKey: true, clientX: 50, clientY: 20 });
  c.onPointerUp({ type: "pointerup" });
  assert.equal(c.canvas.nodes.length, 2);
  assert.equal(n.x, 140, "original stayed in place");
  const clone = c.canvas.nodes.find((x) => x.id !== n.id);
  assert.equal(clone.x, 190);
  assert.equal(clone.y, 120);

  // Ctrl+0 resets zoom, Ctrl+1 fits view
  c.canvas.viewport = { x: 200, y: -50, zoom: 2.5 };
  let prevented = 0;
  const ev = (key, extra = {}) => ({
    key,
    preventDefault() { prevented += 1; },
    target: { closest: () => null },
    ...extra,
  });
  c.onKeyDown(ev("0", { ctrlKey: true }));
  assert.deepEqual(c.canvas.viewport, { x: 0, y: 0, zoom: 1 });
  c.onKeyDown(ev("1", { ctrlKey: true }));
  assert.notEqual(c.canvas.viewport.zoom, 1);

  // F2 triggers inline rename even when Edit Mode is off; ? opens shortcuts modal
  c.canvas.settings.editMode = false;
  c.selected = new Set([n.id]);
  let renamedId = null;
  c.beginTitleEdit = (id) => { renamedId = id; };
  c.onKeyDown(ev("F2"));
  assert.equal(renamedId, n.id);

  let openedModalTitle = "";
  c.openModalFn = ({ title }) => {
    openedModalTitle = title;
    return { body: makeFakeElement("div"), footer: makeFakeElement("div"), close() {} };
  };
  c.onKeyDown(ev("?"));
  assert.match(openedModalTitle, /Shortcuts/i);
  assert.ok(prevented >= 4);
});

// ------------------------------------------------------------------ Track 4: Multi-Format Export

test("Track 4: Interactive HTML, Mermaid (.mmd), and Graphviz (.dot) exports handle XSS, layers, groups & locks", () => {
  const visLayer = { id: "l-vis", name: "Visible", visible: true, color: "blue" };
  const hidLayer = { id: "l-hid", name: "Hidden", visible: false, color: "red" };
  const grp = M.createNode("group", { customTitle: 'Core "Subgraph"', layerId: visLayer.id });
  const a = M.createNode("struct", {
    x: 40, y: 40, groupId: grp.id, layerId: visLayer.id,
    locks: [{ kind: "spinlock", lockName: "task_lock" }],
    data: { name: "task_struct", kind: "struct", fieldLocks: { "1:pids": { kind: "rcu", lockName: "rcu_read_lock" } } },
  });
  a.id = "node-a";
  const b = M.createNode("symbol", { x: 400, y: 40, layerId: visLayer.id, data: { name: "schedule", kind: "func" } });
  b.id = "node_a"; // Deliberate sanitized-ID collision with node-a
  const hiddenNode = M.createNode("note", { x: 100, y: 300, layerId: hidLayer.id, data: { text: "SECRET_HIDDEN" } });
  const xssNote = M.createNode("note", {
    x: 40, y: 260, layerId: visLayer.id,
    customTitle: '</script><script>alert("xss")</script>',
    data: { text: "Safe note" },
  });
  const e1 = M.createEdge({ from: a.id, to: b.id, label: "calls", lockKind: "rcu", style: "dashed", arrow: "both", layerId: visLayer.id });
  const eHid = M.createEdge({ from: a.id, to: hiddenNode.id, label: "hidden_edge", layerId: hidLayer.id });

  const canvas = M.normalizeCanvas({
    ...M.emptyCanvas(),
    nodes: [grp, a, b, hiddenNode, xssNote],
    edges: [e1, eHid],
    settings: { ...M.emptyCanvas().settings, layers: [visLayer, hidLayer] },
    tours: [{
      id: "tour_1",
      title: 'Walk </script><script>alert(2)</script>',
      steps: [{ id: "step_1", title: "Step 1", caption: "Check lock", nodeIds: [a.id], edgeIds: [e1.id], viewport: { x: 0, y: 0, zoom: 1 } }],
    }],
  });

  const escJson = escapeJsonForScript({ bad: "</script>\u2028\u2029&<>" });
  assert.ok(!escJson.includes("</script>"));
  assert.ok(escJson.includes("\\u003c/script\\u003e"));

  const html = canvasToInteractiveHtml(canvas, "", { title: "<Title & Test>" });
  const nonNsHtml = html.replace(/http:\/\/www\.w3\.org\/(?:2000\/svg|1999\/xhtml|1999\/xlink)/g, "");
  assert.match(html, /<!DOCTYPE html>/);
  assert.ok(!nonNsHtml.includes("https://") && !nonNsHtml.includes("http://"));
  assert.ok(!/\beval\(|new Function\(/.test(html));
  assert.ok(!html.includes('</script><script>alert'));
  assert.ok(!html.includes("SECRET_HIDDEN"));
  assert.match(html, /id="nm-tour-bar"/);
  assert.match(html, /nm-zoom-in/);

  const mmd = canvasToMermaid(canvas, { direction: "TB" });
  assert.match(mmd, /^flowchart TB/m);
  assert.match(mmd, /subgraph /);
  assert.match(mmd, /n_node_a\[/);
  assert.match(mmd, /n_node_a_2\[/);
  assert.match(mmd, /task_lock/);
  assert.match(mmd, /rcu_read_lock/);
  assert.match(mmd, /<-\.->\|"calls \(🔒 rcu\)"\|/);
  assert.ok(!mmd.includes("SECRET_HIDDEN"));

  const dot = canvasToDot(canvas, { direction: "LR" });
  assert.match(dot, /^digraph NodeMap \{/m);
  assert.match(dot, /rankdir=LR;/);
  assert.match(dot, /subgraph cluster_/);
  assert.match(dot, /Core \\"Subgraph\\"/);
  assert.match(dot, /style="dashed", dir="both"/);
  assert.ok(!dot.includes("SECRET_HIDDEN"));
});

// ------------------------------------------------------------------ Track 5: Impact Maps & Locking Annotations

test("Track 5: Impact Map extracts diff entities, builds 3-column canvas + tour, and preserves via consumeIncoming", async () => {
  const diffText = [
    "diff --git a/kernel/fork.c b/kernel/fork.c",
    "--- a/kernel/fork.c",
    "+++ b/kernel/fork.c",
    "@@ -100,5 +100,7 @@ struct task_struct *copy_process(unsigned long clone_flags)",
    "-  struct mm_struct *mm = oldmm;",
    "+  struct mm_struct *mm = dup_mm(tsk, current->mm);",
    "+int helper_fn(int flags)",
  ].join("\n");

  const extracted = extractImpactFromDiff({ diffText });
  assert.equal(extracted.files.length, 1);
  assert.ok(extracted.structs.includes("task_struct"));
  assert.ok(extracted.structs.includes("mm_struct"));
  assert.ok(extracted.functions.includes("copy_process"));

  const mockApi = {
    async getCommitDiff() {
      return { files: [{ path: "kernel/fork.c", status: "M", additions: 2, deletions: 1, hunks: [] }] };
    },
    async getCallgraph(_v, fn) {
      return fn === "copy_process"
        ? { callers: [{ caller: "kernel_clone", call_site_path: "kernel/fork.c", call_site_line: 2600 }] }
        : { callers: [] };
    },
    async matchMaintainers() {
      return { subsystems: ["PROCESS SCHEDULING"], files: [{ subsystems: [{ name: "PROCESS SCHEDULING", sec_id: 42 }] }] };
    },
  };

  const impactCanvas = await generateImpactMap({
    api: mockApi,
    version: "v6.8",
    repo: "linux",
    diffText,
    commitHash: "a1b2c3d4e5f6",
  });
  assert.equal(impactCanvas.schema, M.CANVAS_SCHEMA);
  const groupTitles = impactCanvas.nodes.filter((n) => n.type === "group").map((n) => M.displayTitle(n));
  assert.deepEqual(groupTitles, ["Patch / Commit Changes", "Touched Data & Symbols", "Blast Radius & Ownership"]);
  assert.ok(impactCanvas.nodes.some((n) => n.type === "symbol" && n.data.name === "kernel_clone"));
  assert.ok(impactCanvas.nodes.some((n) => n.type === "maintainer" && n.data.secId === 42));
  assert.equal(impactCanvas.tours[0].title, "Impact Walkthrough");

  // Controller consumes initialCanvas cleanly preserving groups and tour
  const c = makeController();
  const added = c.consumeIncoming({ id: "t-imp", initialCanvas: impactCanvas });
  assert.equal(added.length, impactCanvas.nodes.length);
  assert.equal(c.canvas.tours.length, 1);

  const built = buildImpactMap({ files: ["mm/slub.c"], structs: ["kmem_cache"], functions: ["kfree"], callers: { kfree: ["do_free"] }, maintainers: ["SLAB ALLOCATOR"] });
  assert.ok(built.nodes.some((n) => n.type === "struct" && n.data.name === "kmem_cache"));
});

test("Track 5: Locking annotations integrate across canvas_model, struct_view, NodeRenderer, SVG export & Inspector", () => {
  assert.ok(M.LOCK_KINDS.includes("spinlock") && M.LOCK_KINDS.includes("rcu") && M.LOCK_KINDS.includes("lock-order"));

  const st = M.createNode("struct", {
    x: 40, y: 40, w: 320, h: 240,
    locks: [{ kind: "mutex", lockName: "inode_lock", note: "Serializes writes" }],
    data: { name: "inode", kind: "struct", style: "list" },
  });
  const lockRes = M.setFieldLock(st.data, "10:i_size", { kind: "seqlock", lockName: "i_size_seqcount" });
  assert.deepEqual(lockRes, { kind: "seqlock", lockName: "i_size_seqcount" });
  assert.equal(st.data.fieldLocks["10:i_size"].kind, "seqlock");
  const addedLock = M.addNodeLock(st, { kind: "spinlock", lockName: "i_lock" });
  assert.deepEqual(addedLock, { kind: "spinlock", lockName: "i_lock", note: "" });
  const updatedLock = M.updateNodeLock(st, 1, { note: "Innermost" });
  assert.equal(updatedLock.note, "Innermost");
  assert.equal(st.locks[1].note, "Innermost");
  assert.equal(M.removeNodeLock(st, 1), true);
  assert.equal(st.locks.length, 1);

  const layout = {
    file_path: "include/linux/fs.h",
    line_s: 1,
    line_e: 50,
    items: [{ kind: "field", line: 10, name: "i_size", type: "loff_t" }],
  };
  const listHtml = renderStructBody(layout, st.data, { editMode: true });
  assert.match(listHtml, /nm-lock-seqlock/);
  assert.match(listHtml, /seqlock: i_size_seqcount/);
  assert.match(listHtml, /data-act="field-lock"/);

  st.data.style = "table";
  const tableHtml = renderStructBody(layout, st.data, { editMode: false });
  assert.match(tableHtml, /seqlock: i_size_seqcount/);
  assert.ok(!tableHtml.includes('data-act="field-lock"'), "Edit-mode field lock button hidden in View Mode");

  const paholeHtml = renderPaholeBody({
    total_size: 16, cache_lines_used: 1, holes_count: 0,
    members: [{ name: "i_size", type: "loff_t", offset: 0, size: 8, cacheline_idx: 0 }],
  }, st.data);
  assert.match(paholeHtml, /seqlock: i_size_seqcount/);

  // NodeRenderer renders .nm-node-locks cleanly
  const layer = makeFakeElement("div");
  const locksEl = makeFakeElement("div");
  locksEl.className = "nm-node-locks";
  const subEls = new Map([
    [".nm-node-locks", locksEl],
    [".nm-badge", makeFakeElement("span")],
    [".nm-title", makeFakeElement("span")],
    [".nm-head-meta", makeFakeElement("span")],
    [".nm-head-tools", makeFakeElement("div")],
    [".nm-node-body", makeFakeElement("div")],
  ]);
  const nodeEl = {
    ...makeFakeElement("div"),
    querySelector: (s) => subEls.get(s) || null,
    style: { setProperty() {}, removeProperty() {} },
  };
  const nr = new NodeRenderer(layer, { renderBody() {}, disposeBody() {}, headTools: () => "", headMeta: () => null });
  nr.patch(nodeEl, st, new Set(), null, M.emptyCanvas().settings);
  assert.equal(locksEl.hidden, false);
  assert.match(locksEl.innerHTML, /mutex: inode_lock/);

  // SVG export includes lock badge strip and edge lockKind
  const other = M.createNode("symbol", { x: 460, y: 40, data: { name: "vfs_write", kind: "func" } });
  const edge = M.createEdge({ from: st.id, to: other.id, fromField: "10:i_size" });
  M.applyEdgePreset(edge, "locks");
  assert.equal(edge.lockKind, "rcu");
  const edge2 = M.createEdge({ from: st.id, to: other.id, label: "protects", lockKind: "spinlock" });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [st, other], edges: [edge, edge2] }, (c) => c, {
    content: (n) => (n.id === st.id ? layout : null),
  });
  assert.match(svg, /mutex: inode_lock/);
  assert.match(svg, /seqlock: i_size_seqcount/);
  assert.match(svg, /locks \/ RCU/);
  assert.match(svg, /protects \(🔒 spinlock\)/);

  // Inspector adds/deletes node lock and clears field lock
  const c = makeController();
  c.canvas.nodes.push(st);
  c.selected = new Set([st.id]);
  c.onInspectorClick({ target: { closest: (s) => (s === "button[data-insp]" ? { dataset: { insp: "node-lock-add" } } : null) } });
  assert.equal(st.locks.length, 2);
  c.onInspectorClick({ target: { closest: (s) => (s === "button[data-insp]" ? { dataset: { insp: "field-lock-del", key: "10:i_size" } } : null) } });
  assert.equal(st.data.fieldLocks["10:i_size"], undefined);

  // Live SVG edge lock label matches export format without duplicating when label already names the lock kind
  const liveEdgeSvg1 = c._edgeInnerSvg(edge, { heads: [], d: "M 0 0 L 10 0", mid: { x: 5, y: 0 } });
  const liveEdgeSvg2 = c._edgeInnerSvg(edge2, { heads: [], d: "M 0 0 L 10 0", mid: { x: 5, y: 0 } });
  assert.match(liveEdgeSvg1, />locks \/ RCU</);
  assert.match(liveEdgeSvg2, />protects \(🔒 spinlock\)</);
});

test("Regression fixes: toggleExpandNode, sub-map loadCanvasDocument undo, recent saveCanvas timestamp, and group/cluster DOT+Mermaid edges", async () => {
  // 1. toggleExpandNode expands symbol members via getSymbolDetail + getAstTree
  const c = makeController();
  c.api = {
    forRepo() { return this; },
    async getSymbolDetail(_ver, name) {
      return name === "schedule" ? { definition: { ast_id: 99 } } : null;
    },
    async getAstTree(_ver, astId) {
      return astId === 99 ? { node: { children: [{ spelling: "pick_next_task" }, { name: "context_switch" }] } } : {};
    },
  };
  const sym = c.addNode({ type: "symbol", x: 40, y: 40, data: { name: "schedule", kind: "func" } });
  await c.toggleExpandNode(sym.id);
  assert.equal(sym.data.expanded, true);
  assert.deepEqual(sym.data.constituents.map((x) => x.name), ["pick_next_task", "context_switch"]);

  // 2. loadCanvasDocument inside a sub-map portal undoes back to previous root canvas
  const grp = c.addNode({ type: "group", x: 10, y: 10, w: 300, h: 200, customTitle: "Portal" });
  assert.equal(c.enterSubMap(grp.id), true);
  assert.equal(c.subMapPath.length, 1);
  const tpl = buildKernelTemplate("syscall-flow", { version: "v6.8", repo: "linux" });
  assert.equal(c.loadCanvasDocument(tpl), true);
  assert.equal(c.subMapPath.length, 0);
  assert.equal(c.canvas.nodes.length, tpl.nodes.length);
  assert.equal(c.undo(), true);
  assert.ok(c.canvas.nodes.some((n) => n.id === grp.id), "undo restores prior root canvas instead of stale sub-map");

  // 3. saveCanvas stamps updated_at so fresh localStorage-only tab caches are not treated as expired orphans
  const ls = makeMemoryStorage();
  const freshCanvas = M.emptyCanvas();
  M.saveCanvas(ls, `${M.STORAGE_PREFIX}fresh-ls-tab`, freshCanvas);
  const orphansAfterSave = await findOrphanedTabCanvases(null, { fallbackStorage: ls, now: Date.now() });
  assert.equal(orphansAfterSave.some((o) => o.tabId === "fresh-ls-tab"), false);

  // 4. Group/cluster edges in DOT and Mermaid (both non-empty group with ltail/lhead and empty sub-map portal group)
  const subTpl = buildKernelTemplate("subsystem-overview", { version: "v6.8", repo: "linux" });
  const portalGrp = subTpl.nodes.find((n) => n.type === "group" && n.data?.subCanvas);
  assert.ok(portalGrp);
  const dot = canvasToDot(subTpl);
  assert.match(dot, /compound=true;/);
  const portalDotId = new RegExp(`n_${portalGrp.id.replace(/[^A-Za-z0-9_]/g, "_")} \\[label=`).test(dot);
  assert.equal(portalDotId, true, "empty portal cluster declares an internal node so incoming edge does not create an orphan external node");

  const impTpl = buildImpactMap({ files: ["kernel/fork.c"], structs: ["task_struct"], functions: ["copy_process"], maintainers: ["FORK"] });
  const impDot = canvasToDot(impTpl);
  assert.match(impDot, /ltail="cluster_n_/);
  const subMmd = canvasToMermaid(subTpl);
  assert.match(subMmd, /_portal\["\[portal\] Core Internals \(Sub-Map Portal\)"\]/);
});
