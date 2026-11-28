/**
 * nodemap_nested.test.mjs - Nested NodeMap nodes: model normalization, static card rendering, breadcrumb
 * stack, insert-picker rows, SVG export of the node and the api.js client shapes (pure modules).
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import "./helpers/unref_timers.mjs";
import * as M from "../js/views/nodemap/canvas_model.js";
import * as N from "../js/views/nodemap/nested_nodes.js";
import * as S from "../js/views/nodemap/nested_store.js";
import * as Nested from "../js/views/nodemap/nodemap_nested.js";
import { canvasToSvg } from "../js/views/nodemap/nodemap_export.js";

// Browser-local storage stub: no IndexedDB, a Map-backed localStorage
const mkStorage = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), key: (i) => [...m.keys()][i] ?? null, get length() { return m.size; }, _m: m };
};
const mkStore = () => ({ userDb: null, storage: mkStorage() });
const note = (id, x = 0, y = 0, text = "t") => M.createNode("note", { id, x, y, data: { text } });
const doc = (nodes, edges = [], extra = {}) => ({ ...M.emptyCanvas(), nodes, edges, ...extra });
const nest = (id, child, title = id) => M.createNode("nodemap", { id, title, x: 10, y: 10, data: { canvas: child } });
const inlineABC = () => {
  const c = doc([note("c1"), note("c2", 300, 0)], [{ id: "ec", from: "c1", to: "c2" }]);
  const b = doc([note("b1"), nest("nc", c, "C")]);
  return doc([note("a1"), nest("nb", b, "B")]);
};

const card = (over = {}) => ({
  canvas_id: "child", status: "ok", name: "Child <b>map</b>", description: "d", version: "v3.0", revision: 2,
  counts: { nodes: 3, edges: 1, nested: 1 },
  preview: { w: 1000, h: 500, nodes: [{ x: 0, y: 0, w: 100, h: 50, type: "note", color: "" }, { x: 200, y: 100, w: 100, h: 50, type: "struct", color: "red" }], edges: [{ x1: 50, y1: 25, x2: 250, y2: 125, color: "", style: "dashed" }], truncated: false },
  ...over,
});

test("nodemap nodes normalize: canvasId validated, field ports cleared", () => {
  assert.ok(M.NODE_TYPES.includes("nodemap"));
  assert.equal(M.createNode("nodemap", { data: { canvasId: "abc-1_x" } }).data.canvasId, "abc-1_x");
  assert.equal(M.createNode("nodemap", { data: { canvasId: "bad id/../" } }).data.canvasId, "");
  assert.equal(M.createNode("nodemap", {}).title, "Nested NodeMap");
  const a = M.createNode("note", { data: { text: "x" } });
  const b = M.createNode("nodemap", { data: { canvasId: "child" } });
  const c = M.normalizeCanvas({
    ...M.emptyCanvas(), nodes: [a, b],
    edges: [{ id: "e1", from: a.id, to: b.id, fromField: "f", toField: "g" }],
  });
  assert.equal(c.edges.length, 1);
  assert.ok(!c.edges[0].toField, "no field port on the nested end");
  assert.equal(c.edges[0].fromField, "f", "the other end keeps its field");
});

test("nested body shows name, counts, escaped text and an SVG preview without child DOM", () => {
  const node = M.createNode("nodemap", { data: { canvasId: "child" } });
  const html = N.nestedBodyHtml(node, card());
  assert.match(html, /<svg class="nm-nested-svg" viewBox="0 0 1000 500"/);
  assert.match(html, /3 nodes · 1 edge · 1 sub-map/);
  assert.ok(!html.includes("<b>map</b>"));
  assert.match(html, /Child &lt;b&gt;map&lt;\/b&gt;/);
  assert.match(html, /data-act="nested-open"/);
  assert.match(html, /stroke-dasharray/);
  assert.ok(!/nm-node\b/.test(html));
});

test("missing and unset targets render broken / choose states", () => {
  const node = M.createNode("nodemap", { data: { canvasId: "gone" } });
  assert.equal(N.nestedState(node, { status: "missing" }), "missing");
  assert.equal(N.nestedState(node, null), "missing");
  const html = N.nestedBodyHtml(node, { canvas_id: "gone", status: "missing" });
  assert.match(html, /Broken reference/);
  assert.match(html, /data-act="nested-remove"/);
  assert.ok(!/data-act="nested-open"/.test(html));
  const unset = M.createNode("nodemap", {});
  assert.equal(N.nestedState(unset, null), "unset");
  assert.match(N.nestedBodyHtml(unset, null), /Choose NodeMap/);
  const evil = M.createNode("nodemap", { data: { canvasId: "ok" }, customTitle: "<img onerror=x>" });
  assert.ok(!N.nestedBodyHtml(evil, { status: "missing" }).includes("<img"));
});

test("breadcrumb stack pushes, de-duplicates and truncates", () => {
  let st = [];
  st = N.pushCrumb(st, N.crumb("a", "A", "n1"));
  st = N.pushCrumb(st, N.crumb("b", "B", "n2"));
  st = N.pushCrumb(st, N.crumb("c", "C", "n3"));
  assert.deepEqual(st.map((c) => c.canvasId), ["a", "b", "c"]);
  assert.deepEqual(N.truncateCrumbs(st, 1).map((c) => c.canvasId), ["a"]);
  assert.deepEqual(N.truncateCrumbs(st, 0), []);
  assert.equal(N.truncateCrumbs(st, 99).length, 3);
  assert.deepEqual(N.pushCrumb(st, N.crumb("b", "B", "n9")).map((c) => c.nodeId), ["n1", "n9"]);
  const html = N.crumbsHtml(N.crumb("root", "Root <x>"), st);
  assert.match(html, /data-nm-crumb="0"/);
  assert.match(html, /data-nm-crumb="2"/);
  assert.ok(!/data-nm-crumb="3"/.test(html), "the current level is not a button");
  assert.match(html, /aria-current="page"/);
  assert.ok(!html.includes("<x>"));
  assert.equal(N.crumbsHtml(N.crumb("root", "R"), []), "");
});

test("SVG export renders a nested node as a titled box", () => {
  const n = M.createNode("nodemap", { x: 10, y: 10, data: { canvasId: "child" } });
  const c = M.normalizeCanvas({ ...M.emptyCanvas(), nodes: [n] });
  const svg = canvasToSvg(c, (v) => v, { nested: () => card() });
  assert.match(svg, /<svg/);
  assert.match(svg, /3 nodes/);
  assert.doesNotThrow(() => canvasToSvg(c, (v) => v));
});


test("model keeps inline children recursively, depth-capped like Sub-Maps", () => {
  const a = M.normalizeCanvas(JSON.parse(JSON.stringify(inlineABC())));
  const nb = a.nodes.find((n) => n.id === "nb");
  assert.equal(nb.data.canvas.schema, M.CANVAS_SCHEMA);
  assert.ok(nb.data.canvas.nodes.find((n) => n.id === "nc").data.canvas.nodes.length === 2);
  assert.equal(nb.data.canvasId, undefined);
  let deep = doc([]);
  for (let i = 0; i < 12; i++) deep = doc([nest(`n${i}`, deep)]);
  let cur = M.normalizeCanvas(deep);
  let levels = 0;
  for (;;) {
    const n = cur.nodes[0];
    if (!n?.data?.canvas) break;
    cur = n.data.canvas;
    levels++;
  }
  assert.equal(levels, M.MAX_SUBCANVAS_DEPTH);
});

test("model sanitizes the cached summary and preview of a canvasId node", () => {
  const n = M.createNode("nodemap", { data: { canvasId: "c1", summary: { title: "T", nodes: 3, edges: -4, nested: 1.6, depth: 99999, x: 1 }, preview: { w: 1000, h: 500, truncated: 1, nodes: Array.from({ length: 300 }, () => ({ x: 1.4, y: 2, w: 3, h: 4, type: "note", color: "red", evil: 1 })), edges: [{ x1: 0, y1: 0, x2: 5, y2: 5, style: "bogus" }] } } });
  assert.deepEqual(n.data.summary, { title: "T", nodes: 3, edges: 0, nested: 2, depth: 64 });
  assert.equal(n.data.preview.nodes.length, 200);
  assert.equal(n.data.preview.nodes[0].evil, undefined);
  assert.equal(n.data.preview.edges[0].style, "solid");
  assert.equal(n.data.preview.truncated, true);
  assert.equal(M.createNode("nodemap", { data: { summary: { title: "x" } } }).data.summary, undefined, "no cache without a target");
});

test("static preview mirrors build_static_preview (1000 box, bounds, caps)", () => {
  const c = M.normalizeCanvas(doc([note("a", 0, 0), note("b", 480, 0)], [{ id: "e", from: "a", to: "b" }]));
  const p = S.buildStaticPreview(c);
  assert.equal(p.nodes.length, 2);
  assert.equal(p.w, 1000);
  assert.equal(p.edges.length, 1);
  assert.equal(p.truncated, false);
  const many = M.normalizeCanvas(doc(Array.from({ length: 250 }, (_, i) => note(`n${i}`, i * 10, 0))));
  const q = S.buildStaticPreview(many);
  assert.equal(q.nodes.length, 200);
  assert.equal(q.truncated, true);
  assert.deepEqual(S.buildStaticPreview(M.emptyCanvas()), { w: 0, h: 0, nodes: [], edges: [], truncated: false });
});

test("import unpacks inline A -> B -> C into local storage without any fetch", async () => {
  const store = mkStore();
  globalThis.fetch = () => { throw new Error("no network allowed"); };
  const a = await S.importDocument(store, JSON.parse(JSON.stringify(inlineABC())));
  const nb = a.nodes.find((n) => n.id === "nb");
  assert.match(nb.data.canvasId, /^nm-/);
  assert.equal(nb.data.canvas, undefined);
  assert.deepEqual(nb.data.summary, { title: "B", nodes: 2, edges: 0, nested: 1, depth: 1 });
  assert.ok(nb.data.preview.nodes.length === 2);
  const b = await S.loadChild(store, nb.data.canvasId);
  assert.equal(b.nested, true);
  const nc = b.canvas.nodes.find((n) => n.id === "nc");
  assert.deepEqual(nc.data.summary, { title: "C", nodes: 2, edges: 1, nested: 0, depth: 0 });
  const c = await S.loadChild(store, nc.data.canvasId);
  assert.equal(c.canvas.nodes.length, 2);
  assert.equal(c.canvas.edges.length, 1);
  // children do not clutter the library list
  const lib = await import("../js/views/nodemap/nodemap_library.js");
  assert.equal((await lib.listNamedCanvases(null, {}, store.storage)).length, 0);
  assert.equal((await lib.listNamedCanvases(null, { nested: "all" }, store.storage)).length, 2);
  assert.equal(await S.importDocument(store, { nope: 1 }), null);
});

test("export re-inlines every level; cycles and missing targets become unset nodes with warnings", async () => {
  const store = mkStore();
  const a = await S.importDocument(store, JSON.parse(JSON.stringify(inlineABC())));
  const out = await S.inlineDocument(store, a);
  assert.deepEqual(out.warnings, []);
  const nb = out.canvas.nodes.find((n) => n.id === "nb");
  assert.equal(nb.data.canvasId, undefined);
  assert.equal(nb.data.summary, undefined);
  assert.equal(nb.data.canvas.nodes.find((n) => n.id === "nc").data.canvas.nodes.length, 2);
  // the export re-imports cleanly into a fresh store
  const store2 = mkStore();
  const again = await S.importDocument(store2, out.canvas);
  assert.ok(again.nodes.find((n) => n.id === "nb").data.canvasId);
  // missing target
  const ghost = M.normalizeCanvas(doc([M.createNode("nodemap", { id: "g", data: { canvasId: "gone" } })]));
  const r = await S.inlineDocument(store, ghost);
  assert.deepEqual(r.canvas.nodes[0].data, {});
  assert.equal(r.warnings[0].code, "missing_reference");
  // cycle: X contains Y contains X
  await S.saveChild(store, "X", doc([M.createNode("nodemap", { id: "ny", data: { canvasId: "Y" } })]));
  await S.saveChild(store, "Y", doc([M.createNode("nodemap", { id: "nx", data: { canvasId: "X" } })]));
  const root = M.normalizeCanvas(doc([M.createNode("nodemap", { id: "r", data: { canvasId: "X" } })]));
  const cyc = await S.inlineDocument(store, root);
  assert.equal(cyc.warnings.length, 1);
  assert.equal(cyc.warnings[0].code, "reference_cycle");
  assert.equal(cyc.canvas.nodes[0].data.canvas.nodes[0].data.canvas.nodes[0].data.constructor, Object);
  assert.deepEqual(cyc.canvas.nodes[0].data.canvas.nodes[0].data.canvas.nodes[0].data, {});
});

test("cycle-blocked rows: the open canvas, its ancestors and every canvas containing them", async () => {
  const store = mkStore();
  await S.saveChild(store, "top", doc([M.createNode("nodemap", { id: "n1", data: { canvasId: "mid" } })]));
  await S.saveChild(store, "mid", doc([M.createNode("nodemap", { id: "n2", data: { canvasId: "cur" } })]));
  await S.saveChild(store, "cur", doc([]));
  await S.saveChild(store, "other", doc([]));
  const blocked = S.cycleBlocked(await S.referenceGraph(store), "cur", []);
  assert.deepEqual([...blocked].sort(), ["cur", "mid", "top"]);
  const lib = await import("../js/views/nodemap/nodemap_library.js");
  const items = await lib.listNamedCanvases(null, { nested: "all" }, store.storage);
  const rows = N.pickerRows(items, { currentId: "cur", blocked });
  assert.deepEqual(rows.map((r) => [r.id, r.insertable]).sort(), [["mid", false], ["other", true], ["top", false]]);
  assert.equal(rows.find((r) => r.id === "mid").hint, "would create a cycle");
  const html = N.pickerHtml(rows);
  assert.match(html, /data-pick="mid"[^>]*disabled/);
  assert.ok(!html.includes("<i>"));
});

// ---- controller-level flows against a fake controller (local storage stub, request recorder)

const mkCtrl = (canvas, over = {}) => {
  const requests = [];
  const ctrl = {
    userDb: null, repo: "linux", currentVersion: "v3.0", tab: { id: "tab1", title: "Root map" },
    rootCanvas: canvas, canvas, subMapPath: [], selected: new Set(), selectedEdge: null, saveTimer: null,
    history: { reset() {} }, portOffsets: new Map(),
    byId(id) { return this.canvas.nodes.find((n) => n.id === id); },
    commit() { this.commits = (this.commits || 0) + 1; },
    viewCenter: () => ({ x: 500, y: 400 }),
    renderBreadcrumbs() {},
    flushSave() { this.flushed = (this.flushed || 0) + 1; },
    loadCanvasDocument(d) {
      const c = M.normalizeCanvas(d);
      if (!c) return false;
      this.rootCanvas = c;
      this.canvas = c;
      this.subMapPath = [];
      return true;
    },
    api: {
      calls: requests,
      async getNodemapCanvases(o) { requests.push(["GET", "list", o]); return { items: [{ id: "srv", name: "Server map", node_count: 2, edge_count: 0 }] }; },
      async getNodemapCanvas(id) { requests.push(["GET", id]); return { id, name: "Server map", canvas: JSON.parse(JSON.stringify(inlineABC())), warnings: [] }; },
    },
    ...over,
  };
  Nested.initNested(ctrl);
  ctrl.nav = { root: { canvasId: "tab1", name: "Root map", nodeId: "" }, stack: [] };
  return ctrl;
};
const useStore = () => {
  const ls = mkStorage();
  Object.defineProperty(globalThis, "localStorage", { value: ls, configurable: true, writable: true });
  return ls;
};
const writes = (ctrl) => ctrl.api.calls.filter((c) => c[0] !== "GET");

test("create a new nested NodeMap locally, drill in, edit, and the parent card refreshes on the way back", async () => {
  useStore();
  const ctrl = mkCtrl(M.normalizeCanvas(doc([note("p1")])), { promptNameFn: async () => "Kid" });
  assert.equal(await Nested.createNestedNodemap(ctrl, { x: 40, y: 50 }), true);
  const node = ctrl.canvas.nodes.find((n) => n.type === "nodemap");
  assert.match(node.data.canvasId, /^nm-/);
  assert.deepEqual(node.data.summary, { title: "Kid", nodes: 0, edges: 0, nested: 0, depth: 0 });
  assert.match(N.nestedBodyHtml(node, Nested.nestedCard(ctrl, node)), /data-act="nested-open"/);
  // drill down
  const rootDoc = ctrl.rootCanvas;
  assert.equal(await Nested.openNested(ctrl, node.id), true);
  assert.deepEqual(ctrl.nav.stack.map((c) => [c.canvasId, c.name, c.nodeId]), [[node.data.canvasId, "Kid", node.id]]);
  assert.equal(ctrl.canvas.nodes.length, 0);
  ctrl.canvas.nodes.push(note("k1", 0, 0), note("k2", 200, 0));
  ctrl.canvas.edges.push(M.createEdge({ id: "ke", from: "k1", to: "k2" }));
  // second level
  ctrl.promptNameFn = async () => "Grand";
  await Nested.createNestedNodemap(ctrl);
  const gnode = ctrl.canvas.nodes.find((n) => n.type === "nodemap");
  await Nested.openNested(ctrl, gnode.id);
  assert.equal(ctrl.nav.stack.length, 2);
  ctrl.canvas.nodes.push(note("g1"));
  // jump straight to the root: every parent card refreshed
  assert.equal(await Nested.goToCrumb(ctrl, 0), true);
  assert.equal(ctrl.nav.stack.length, 0);
  const again = ctrl.canvas.nodes.find((n) => n.id === node.id);
  assert.deepEqual(again.data.summary, { title: "Kid", nodes: 3, edges: 1, nested: 1, depth: 1 });
  const kid = await S.loadChild(mkStoreOf(), node.data.canvasId);
  assert.equal(kid.canvas.nodes.find((n) => n.id === gnode.id).data.summary.nodes, 1);
  assert.ok(rootDoc);
  assert.deepEqual(writes(ctrl), []);
});
const mkStoreOf = () => ({ userDb: null, storage: globalThis.localStorage });

test("a missing child renders the broken-reference state and cannot be opened", async () => {
  useStore();
  const n = M.createNodeStub?.() || M.createNode("nodemap", { id: "bn", data: { canvasId: "gone", summary: { title: "G", nodes: 1, edges: 0, nested: 0, depth: 0 } } });
  const ctrl = mkCtrl(M.normalizeCanvas(doc([n])));
  assert.equal(Nested.nestedCard(ctrl, ctrl.canvas.nodes[0]).status, "ok");
  assert.equal(await Nested.probeStatuses(ctrl), true);
  assert.equal(Nested.nestedCard(ctrl, ctrl.canvas.nodes[0]).status, "missing");
  assert.match(N.nestedBodyHtml(ctrl.canvas.nodes[0], Nested.nestedCard(ctrl, ctrl.canvas.nodes[0])), /Broken reference/);
  assert.equal(await Nested.openNested(ctrl, "bn"), false);
  assert.equal(ctrl.nav.stack.length, 0);
});

test("insert an existing local canvas or a read-only server canvas (as a local copy); zero writes", async () => {
  useStore();
  const store = mkStoreOf();
  await S.saveChild(store, "lib1", doc([note("x"), note("y", 300, 0)]), { name: "Local one" });
  const ctrl = mkCtrl(M.normalizeCanvas(doc([])));
  ctrl.nestedPickerFn = async () => ({ kind: "local", id: "lib1" });
  assert.equal(await Nested.insertExistingNodemap(ctrl, { x: 5, y: 6 }), true);
  const n1 = ctrl.canvas.nodes[0];
  assert.equal(n1.data.canvasId, "lib1");
  assert.equal(n1.data.summary.nodes, 2);
  assert.equal(n1.x, 5);
  ctrl.nestedPickerFn = async () => ({ kind: "server", id: "srv" });
  assert.equal(await Nested.insertExistingNodemap(ctrl), true);
  const n2 = ctrl.canvas.nodes[1];
  assert.notEqual(n2.data.canvasId, "srv");
  assert.equal(n2.data.summary.title, "Server map");
  const copy = await S.loadChild(store, n2.data.canvasId);
  assert.ok(copy.canvas.nodes.find((n) => n.id === "nb").data.canvasId, "nested levels of the server canvas are unpacked too");
  assert.deepEqual(ctrl.api.calls.map((c) => c[0]), ["GET"]);
  assert.deepEqual(writes(ctrl), []);
  // choose another for an unset node
  const unset = M.createNode("nodemap", { id: "u" });
  ctrl.canvas.nodes.push(unset);
  ctrl.nestedPickerFn = async () => ({ kind: "local", id: "lib1" });
  assert.equal(await Nested.chooseNestedTarget(ctrl, "u"), true);
  assert.equal(unset.data.canvasId, "lib1");
});

test("picker data lists local rows (cycle-blocked disabled) and server rows via GET only", async () => {
  useStore();
  const store = mkStoreOf();
  await S.saveChild(store, "par", doc([M.createNode("nodemap", { id: "k", data: { canvasId: "me" } })]), { name: "Parent" });
  await S.saveChild(store, "me", doc([]), { name: "Me" });
  await S.saveChild(store, "free", doc([]), { name: "Free" });
  const ctrl = mkCtrl(M.normalizeCanvas(doc([])));
  ctrl.nav = { root: ctrl.nav.root, stack: [{ canvasId: "me", name: "Me", nodeId: "k" }] };
  const data = await Nested.pickerData(ctrl, {});
  assert.deepEqual(data.local.map((r) => [r.id, r.insertable]).sort(), [["free", true], ["par", false]]);
  assert.equal(data.server[0].id, "srv");
  assert.equal(data.server[0].insertable, true);
  assert.deepEqual(writes(ctrl), []);
});

test("group selection moves nodes, rewires boundary edges, retargets tours; zero writes", async () => {
  useStore();
  const canvas = M.normalizeCanvas(doc(
    [note("a", 0, 0), note("b", 300, 0), note("c", 600, 0), M.createNode("group", { id: "gr", x: 0, y: 200, w: 400, h: 300 }), M.createNode("note", { id: "m", x: 20, y: 220, data: { text: "m" }, groupId: "gr" })],
    [
      { id: "e_ab", from: "a", to: "b", waypoints: [{ x: 150, y: 10 }] },
      { id: "e_bc", from: "b", to: "c", fromField: "f", waypoints: [{ x: 450, y: 5 }] },
      { id: "e_ca", from: "c", to: "a" },
      { id: "e_ma", from: "m", to: "c" },
    ],
    { tours: [{ id: "t1", title: "T", steps: [{ id: "s1", nodeIds: ["a", "b", "c"], edgeIds: ["e_ab", "e_bc"], title: "s" }] }] },
  ));
  let preview = null;
  const ctrl = mkCtrl(canvas, { promptNameFn: async () => "Pair", confirmFn: async (o) => { preview = o; return true; } });
  ctrl.selected = new Set(["a", "b", "gr"]);
  assert.equal(await Nested.groupSelectionIntoNested(ctrl), true);
  assert.ok(preview.notes[0].startsWith("3 edges"), preview.notes[0]);
  const nested = ctrl.canvas.nodes.find((n) => n.type === "nodemap");
  assert.deepEqual(ctrl.canvas.nodes.map((n) => n.id).sort(), [nested.id, "c"].sort());
  const edges = Object.fromEntries(ctrl.canvas.edges.map((e) => [e.id, e]));
  assert.deepEqual(Object.keys(edges).sort(), ["e_bc", "e_ca", "e_ma"]);
  assert.equal(edges.e_bc.from, nested.id);
  assert.equal(edges.e_bc.fromField, null);
  assert.deepEqual(edges.e_bc.waypoints, []);
  assert.equal(edges.e_ca.to, nested.id);
  assert.equal(edges.e_ma.from, nested.id);
  const step = ctrl.canvas.tours[0].steps[0];
  assert.deepEqual(step.nodeIds, [nested.id, "c"]);
  assert.deepEqual(step.edgeIds, ["e_bc"]);
  const kid = await S.loadChild(mkStoreOf(), nested.data.canvasId);
  assert.deepEqual(kid.canvas.nodes.map((n) => n.id).sort(), ["a", "b", "gr", "m"]);
  assert.deepEqual(kid.canvas.edges.map((e) => e.id).sort(), ["e_ab", ]);
  const ab = kid.canvas.nodes.find((n) => n.id === "a");
  assert.equal(ab.x, 80);
  assert.equal(nested.data.summary.nodes, 4);
  assert.deepEqual([...ctrl.selected], [nested.id]);
  assert.deepEqual(writes(ctrl), []);
});

test("declining the group preview leaves the canvas untouched", async () => {
  useStore();
  const ctrl = mkCtrl(M.normalizeCanvas(doc([note("a"), note("b", 300, 0)], [{ id: "e", from: "a", to: "b" }])), { promptNameFn: async () => "X", confirmFn: async () => false });
  ctrl.selected = new Set(["a"]);
  assert.equal(await Nested.groupSelectionIntoNested(ctrl), false);
  assert.equal(ctrl.canvas.nodes.length, 2);
});

test("loading a server canvas makes a local copy and the deep link is detached", async () => {
  useStore();
  const ctrl = mkCtrl(M.normalizeCanvas(doc([])));
  ctrl.tab.canvasId = "srv";
  assert.equal(await Nested.loadServerCanvas(ctrl, "srv"), true);
  assert.equal(ctrl.tab.canvasId, "");
  assert.equal(ctrl.tab.title, "Server map");
  const nb = ctrl.canvas.nodes.find((n) => n.id === "nb");
  assert.match(nb.data.canvasId, /^nm-/);
  assert.equal(nb.data.canvas, undefined);
  assert.deepEqual(ctrl.api.calls.map((c) => c.join(":")), ["GET:srv"]);
  const exported = await Nested.exportDocument(ctrl);
  assert.equal(exported.canvas.nodes.find((n) => n.id === "nb").data.canvas.nodes.length, 2);
});

test("flushSave of a drilled-down level writes the child record, not the tab canvas", async () => {
  useStore();
  const { flushSave } = await import("../js/views/nodemap/nodemap_persistence.js");
  const ctrl = mkCtrl(M.normalizeCanvas(doc([note("p")])), { promptNameFn: async () => "Kid" });
  await Nested.createNestedNodemap(ctrl);
  const node = ctrl.canvas.nodes.find((n) => n.type === "nodemap");
  await Nested.openNested(ctrl, node.id);
  ctrl.canvas.nodes.push(note("inside"));
  ctrl.saveTimer = setTimeout(() => {}, 1000);
  flushSave(ctrl);
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
  const kid = await S.loadChild(mkStoreOf(), node.data.canvasId);
  assert.deepEqual(kid.canvas.nodes.map((n) => n.id), ["inside"]);
  assert.equal(globalThis.localStorage.getItem(`kip-nodemap-canvas:${ctrl.tab.id}`), null);
});

test("Sub-Maps stay untouched: convertGroupToSubMap and resolveSubMapAtPath", () => {
  const g = M.createNode("group", { id: "g" });
  const sub = M.convertGroupToSubMap(g);
  sub.nodes.push(note("inner"));
  const root = M.normalizeCanvas(doc([g]));
  const r = M.resolveSubMapAtPath(root, ["g"]);
  assert.equal(r.canvas.nodes[0].id, "inner");
  assert.equal(r.trail.length, 1);
});

test("api client exposes read-only nodemap canvas methods and issues only GETs", async () => {
  const { api } = await import("../js/api.js");
  const proto = Object.getPrototypeOf(api);
  for (const name of ["createNodemapCanvas", "updateNodemapCanvas", "deleteNodemapCanvas", "applyNodemapOps", "insertNodemapNested", "groupNodemapNodes", "getNodemapHierarchy"]) {
    assert.equal(typeof proto[name], "undefined", name);
  }
  const calls = [];
  const client = Object.create(proto);
  client.request = async (url, opts) => { calls.push([url, opts]); return {}; };
  await client.getNodemapCanvases({ q: "x", insertableInto: "cur" });
  assert.equal(calls.at(-1)[0], "/webapi/nodemap/canvases?q=x&limit=100");
  await client.getNodemapCanvas("a b");
  assert.equal(calls.at(-1)[0], "/webapi/nodemap/canvases/a%20b");
  assert.ok(calls.every(([, o]) => !o?.method || o.method === "GET"));
});

test("breadcrumb click returns to parent nodemap when editMode is off without starting a canvas gesture", async () => {
  const { onPointerDown, onDblClick, attachCanvasEvents } = await import("../js/views/nodemap/nodemap_gestures.js");
  const handlers = new Map();
  const makeEl = (tag = "div") => {
    const el = {
      tagName: tag.toUpperCase(),
      dataset: {},
      classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
      addEventListener(ev, fn) { handlers.set(`${tag}:${ev}`, fn); },
      closest(sel) {
        if (sel === ".nm-breadcrumbs") return el._inBreadcrumbs ? el : null;
        if (sel === "[data-nm-crumb]") return el.dataset.nmCrumb !== undefined ? el : null;
        return null;
      },
      focus() {},
      contains: () => false,
    };
    return el;
  };
  const wrapEl = makeEl("wrap");
  const breadcrumbsEl = makeEl("breadcrumbs");
  breadcrumbsEl._inBreadcrumbs = true;
  const crumbBtn = makeEl("button");
  crumbBtn._inBreadcrumbs = true;
  crumbBtn.dataset.nmCrumb = "0";
  const went = [];
  let addedNote = false;
  const ctrl = {
    canvas: M.normalizeCanvas(doc([note("n1")])),
    wrapEl,
    breadcrumbsEl,
    rootEl: makeEl("root"),
    inspectorEl: makeEl("inspector"),
    selected: new Set(),
    gesture: null,
    tool: "pan",
    tourExporting: false,
    byId: () => null,
    goToCrumb: (d) => { went.push(d); return true; },
    exitSubMapTo: () => false,
    addNoteAt: () => { addedNote = true; },
    onPointerDown: (e) => onPointerDown(ctrl, e),
    onDblClick: (e) => onDblClick(ctrl, e),
    onClick: () => {},
    onWheel: () => {},
    onContextMenu: () => {},
    onKeyDown: () => {},
    onPointerMove: () => {},
    onPointerUp: () => {},
    onInspectorInput: () => {},
    onInspectorClick: () => {},
    scheduleWires: () => {},
    history: { close() {} },
  };
  assert.equal(ctrl.canvas.settings.editMode, false);
  const prevWin = globalThis.window;
  globalThis.window = { addEventListener() {}, removeEventListener() {} };
  try {
    attachCanvasEvents(ctrl);
  } finally {
    if (prevWin === undefined) delete globalThis.window;
    else globalThis.window = prevWin;
  }

  let stopped = false;
  handlers.get("breadcrumbs:pointerdown")({ target: crumbBtn, button: 0, stopPropagation: () => { stopped = true; } });
  assert.equal(stopped, true);
  onPointerDown(ctrl, { target: crumbBtn, button: 0, clientX: 10, clientY: 10 });
  assert.equal(ctrl.gesture, null);

  onDblClick(ctrl, { target: crumbBtn, clientX: 10, clientY: 10 });
  assert.equal(addedNote, false);

  handlers.get("breadcrumbs:click")({ target: crumbBtn });
  assert.deepEqual(went, [0]);
});
