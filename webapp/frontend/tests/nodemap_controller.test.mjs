/**
 * nodemap_controller.test.mjs - NodeMap entry points and isolation: "Send to NodeMap" specs for source,
 * man, struct and live tab nodes, target-tab routing, the `?embed=1` shell switch, and the controller's
 * handover, form, struct-toggle and selection logic (no rendering: the renderer is a stub).
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

// Module singletons start heartbeats when the controller imports state.js: keep them unreffed
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

const { isEmbedMode } = await import("../js/utils/embed_mode.js");
const { nodeSpecsForTab, sourceSpec, targetNodemapTab, tokenSpec } = await import("../js/views/nodemap/nodemap_bridge.js");
const { NodeMapController } = await import("../js/views/nodemap/nodemap_controller.js");
const M = await import("../js/views/nodemap/canvas_model.js");
const { state } = await import("../js/state.js");

const hashOf = (t) => `v=${t.version}&tab=${t.type}`;

test("embed shell is opted into by ?embed=1 only", () => {
  assert.equal(isEmbedMode({ search: "?embed=1" }), true);
  assert.equal(isEmbedMode({ search: "?embed=0" }), false);
  assert.equal(isEmbedMode({ search: "" }), false);
  assert.equal(isEmbedMode(null), false);
  assert.equal(isEmbedMode(), false, "the test page itself is not an embed shell");
});

test("tabs become native nodes where one exists, live tab nodes otherwise", () => {
  const code = nodeSpecsForTab({ type: "code", path: "kernel/fork.c", version: "v3.0", repo: "linux" }, hashOf);
  assert.deepEqual(code, [{ version: "v3.0", repo: "linux", type: "source", data: { path: "kernel/fork.c", mode: "file" } }]);
  const range = nodeSpecsForTab({ type: "code", path: "kernel/fork.c", version: "v3.0", cursorLine: 10, lineEnd: 20 }, hashOf);
  assert.deepEqual(range[0].data, { path: "kernel/fork.c", mode: "range", start: 10, end: 20 });
  const man = nodeSpecsForTab({ type: "man", repo: "man-pages", version: "man-pages-6.9", manPath: "man2/open.2", manPage: "open", manSec: "2" }, hashOf);
  assert.equal(man[0].type, "man");
  assert.equal(man[0].data.path, "man2/open.2");
  assert.equal(nodeSpecsForTab({ type: "pahole", structName: "page", version: "v3.0" }, hashOf)[0].type, "struct");
  const commits = nodeSpecsForTab({ type: "commits", version: "v3.0", title: "Commits" }, hashOf);
  assert.deepEqual(commits, [{ version: "v3.0", repo: "", type: "tab", title: "Commits", data: { tabType: "commits", hash: "v=v3.0&tab=commits" } }]);
  const live = nodeSpecsForTab({ type: "code", path: "kernel/fork.c", version: "v3.0" }, hashOf, { live: true });
  assert.equal(live[0].type, "tab", "a live embed of a code tab is a tab node");
  assert.deepEqual(nodeSpecsForTab({ type: "nodemap", version: "v3.0" }, hashOf), [], "a canvas never embeds a canvas");
  assert.deepEqual(nodeSpecsForTab({ type: "commits" }, null), [], "no address, no tab node");
});

test("source and token specs", () => {
  assert.equal(sourceSpec({ path: "", version: "v3.0" }), null);
  assert.deepEqual(sourceSpec({ path: "a.c", version: "v3.0", start: 5, end: 9 }).data, { path: "a.c", mode: "range", start: 5, end: 9 });
  assert.deepEqual(sourceSpec({ path: "", version: "v3.0", symbol: "do_fork" }).data, { path: "", mode: "symbol", symbol: "do_fork", symbolKind: "" });
  const st = tokenSpec({ name: "struct page", typeId: 0, path: "mm/page.c", version: "v3.0" });
  assert.deepEqual(st.data, { name: "page", kind: "struct", hint: "mm/page.c" });
  assert.equal(tokenSpec({ name: "u", typeId: 34 }).data.kind, "union");
  assert.deepEqual(tokenSpec({ name: "schedule()", typeId: 5, astId: 7 }).data, { name: "schedule", kind: "func", astId: 7 });
  assert.equal(tokenSpec({ name: "" }), null);
});

test("Send to NodeMap targets a canvas of the same project, active pane first", () => {
  const panes = [
    { id: "p1", tabs: [{ id: "a", type: "nodemap", repo: "other" }, { id: "b", type: "code", repo: "linux" }] },
    { id: "p2", tabs: [{ id: "c", type: "nodemap", repo: "linux" }] },
    { id: "p3", tabs: [{ id: "d", type: "nodemap", repo: "linux" }] },
  ];
  const repoOf = (t) => t.repo;
  assert.equal(targetNodemapTab(panes, "p3", repoOf, "linux").id, "d");
  assert.equal(targetNodemapTab(panes, "p1", repoOf, "linux").id, "c");
  assert.equal(targetNodemapTab(panes, "p1", repoOf, "none"), null);
});

function controller(tab = { id: "tab-1", type: "nodemap", version: "v3.0", repo: "" }) {
  const c = new NodeMapController();
  c.tab = tab;
  c.currentVersion = tab.version;
  c.repo = tab.repo;
  c.storageKey = `${M.STORAGE_PREFIX}${tab.id}`;
  c.canvas = M.emptyCanvas();
  const synced = [];
  c.renderer = { sync: (nodes, sel) => synced.push([nodes.length, [...sel]]), invalidate() {}, element: () => null, clear() {} };
  c.renderInspector = () => {};
  c.renderWires = () => {};
  return { c, synced };
}

test("handover: initial diagrams, context-menu tokens and queued nodes join the canvas once", () => {
  const { c } = controller();
  const tab = {
    id: "tab-1",
    initialNodes: [{ id: "x", type: "symbol", x: 10, y: 20, data: { name: "f", kind: "func" } }, { id: "y", type: "struct", x: 400, y: 20, data: { name: "s" } }],
    initialEdges: [{ from: "x", to: "y", label: "uses" }],
  };
  c.consumeIncoming(tab);
  assert.equal(c.canvas.nodes.length, 2);
  assert.equal(c.canvas.edges.length, 1);
  assert.equal(c.canvas.edges[0].from, c.canvas.nodes[0].id, "spec ids map onto the new node ids");
  assert.equal(c.canvas.nodes[1].version, "v3.0", "nodes follow the canvas revision by default");
  assert.ok(!("initialNodes" in tab), "consumed once");

  c.consumeIncoming({ id: "tab-1", initialNode: { name: "struct task_struct", type: 28 }, pendingNodes: [{ type: "source", version: "v2.6.39", data: { path: "kernel/fork.c" } }, { type: "bogus" }] });
  assert.equal(c.canvas.nodes.length, 4);
  const ts = c.canvas.nodes[2];
  assert.equal(ts.type, "struct");
  assert.equal(ts.data.name, "task_struct");
  assert.equal(c.canvas.nodes[3].version, "v2.6.39", "a node keeps the revision it was sent with");
  assert.deepEqual([...c.selected], [ts.id, c.canvas.nodes[3].id]);
});

test("add-node forms build specs; tab nodes address their own revision and project", async () => {
  const { c } = controller({ id: "t", type: "nodemap", version: "v3.0", repo: "linux" });
  assert.equal(await c.specFromForm("struct", { name: "bad name" }), null);
  assert.deepEqual(await c.specFromForm("struct", { version: "v3.0", name: "union foo", kind: "union", path: " include/x.h " }), { type: "struct", version: "v3.0", data: { name: "foo", kind: "union", path: "include/x.h" } });
  const src = await c.specFromForm("source", { version: "v3.0", path: "a.c", mode: "range", start: "3", end: "9", symbol: "", symbolKind: "", context: "0" });
  assert.deepEqual(src.data, { path: "a.c", mode: "range", start: 3, end: 9, symbol: "", symbolKind: "", context: 0 });
  const tab = await c.specFromForm("tab", { version: "v3.0", tabType: "kconfig", extra: "arch=x86", openTab: "" });
  const p = new URLSearchParams(tab.data.hash);
  assert.equal(p.get("tab"), "kconfig");
  assert.equal(p.get("v"), "v3.0");
  assert.equal(p.get("repo"), "linux");
  assert.equal(p.get("arch"), "x86");
  const mail = await c.specFromForm("tab", { version: "v3.0", tabType: "mail", extra: "", openTab: "" });
  assert.equal(new URLSearchParams(mail.data.hash).get("v"), null, "versionless tabs carry no revision");
});

test("struct toggles persist on the node; selection, align, colors and deletion", () => {
  const { c } = controller();
  const s = c.addNode({ type: "struct", data: { name: "foo" } });
  c.content.set(c.fetchKeyOf(s), { status: "ok", value: { items: [{ kind: "field", name: "a", type: "int", line: 3, conds: [] }] } });
  c.onStructAction(s, "toggle-type", "3:a");
  assert.equal(s.data.typeOverrides["3:a"], false);
  c.onStructAction(s, "guards");
  assert.equal(s.data.showGuards, false);
  c.onStructAction(s, "all-types");
  assert.equal(s.data.showTypes, false);
  assert.deepEqual(s.data.typeOverrides, {});
  c.onStructAction(s, "collapse", "9:u");
  assert.equal(s.data.collapsed["9:u"], true);
  const back = M.normalizeCanvas(M.serializeCanvas(c.canvas));
  assert.equal(back.nodes[0].data.showGuards, false, "toggle state is part of the canvas JSON");

  const n1 = c.addNode({ type: "note", data: { text: "1" } });
  const n2 = c.addNode({ type: "note", data: { text: "2" } });
  n1.x = 0;
  n2.x = 300;
  c.selected = new Set([n1.id, n2.id]);
  c.align("left");
  assert.equal(n2.x, 0);
  c.setColor("purple");
  assert.equal(n1.color, "purple");
  assert.equal(n2.color, "purple");
  const g = c.addGroup();
  assert.equal(n1.groupId, g.id, "grouping the selection encloses it");
  assert.equal(n2.groupId, g.id);
  c.canvas.edges.push(M.createEdge({ from: n1.id, to: s.id }));
  c.selected = new Set([n1.id]);
  c.deleteSelection();
  assert.equal(c.byId(n1.id), null);
  assert.equal(c.canvas.edges.length, 0, "a removed node takes its edges");
  c.setTitle(n2.id, "Renamed");
  assert.equal(M.displayTitle(n2), "Renamed");
  c.setTitle(n2.id, "");
  assert.equal(n2.customTitle, "");
});

test("canvas persists per tab in localStorage", async () => {
  const { c } = controller({ id: "persist-1", type: "nodemap", version: "v3.0", repo: "" });
  c.addNode({ type: "note", data: { text: "keep me" } });
  c.flushSave();
  const stored = M.loadCanvas(localStorage, `${M.STORAGE_PREFIX}persist-1`);
  assert.equal(stored.nodes[0].data.text, "keep me");
  assert.equal(M.loadCanvas(localStorage, `${M.STORAGE_PREFIX}other-tab`), null, "other tabs have their own canvas");
  // The workspace session is not where it goes
  assert.equal(state._persistenceSuspended ?? false, false);
});

function withViewport(c) {
  const classes = new Set();
  c.wrapEl = { classList: { add: (k) => classes.add(k), remove: (k) => classes.delete(k), toggle() {} }, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }), style: { setProperty() {} } };
  c.layerEl = { style: {} };
  c.marqueeEl = { style: {}, hidden: true };
  c.applyTransform = () => {};
  c.scheduleWires = () => {};
  return classes;
}

test("gestures: drag snaps and re-derives groups, a release the window missed ends it, a cancelled wire adds nothing", () => {
  const { c } = controller();
  withViewport(c);
  c.canvas.settings = { snap: true, grid: 20 };
  const g = c.addNode({ type: "group", data: {} });
  Object.assign(g, { x: 0, y: 0, w: 600, h: 400 });
  const n = c.addNode({ type: "note", data: {} });
  Object.assign(n, { x: 1000, y: 1000, groupId: null });
  c.gesture = { kind: "drag", start: { x: 0, y: 0 }, primary: n.id, nodes: [{ n, x: n.x, y: n.y }], moved: false };
  c.onPointerMove({ clientX: -893, clientY: -907, buttons: 1 });
  assert.deepEqual([n.x, n.y], [100, 100], "snapped to the grid");
  c.onPointerMove({ clientX: -893, clientY: -907, buttons: 0 });
  assert.equal(c.gesture, null, "a move without buttons finishes the gesture");
  assert.equal(n.groupId, g.id, "dropped into the frame");

  c.gesture = { kind: "wire", start: { x: 0, y: 0 }, from: n.id, side: "right", field: null, cur: { x: 0, y: 0 }, moved: true };
  globalThis.document.elementFromPoint = () => null;
  c.onPointerUp({ type: "pointercancel", clientX: 5, clientY: 5 });
  assert.equal(c.canvas.edges.length, 0);
  assert.equal(c.gesture, null);
});

test("marquee and pan gestures", () => {
  const { c } = controller();
  const classes = withViewport(c);
  const a = c.addNode({ type: "note", data: {} });
  const b = c.addNode({ type: "note", data: {} });
  Object.assign(a, { x: 0, y: 0 });
  Object.assign(b, { x: 2000, y: 2000 });
  c.selected.clear();
  c.gesture = { kind: "marquee", start: { x: 0, y: 0 }, origin: { x: -10, y: -10 }, base: new Set(), moved: false };
  c.onPointerMove({ clientX: 300, clientY: 300, buttons: 1 });
  c.onPointerUp({ type: "pointerup" });
  assert.deepEqual([...c.selected], [a.id]);
  c.gesture = { kind: "pan", start: { x: 0, y: 0 }, vp: { x: 0, y: 0, zoom: 1 }, moved: false, clear: true };
  c.onPointerMove({ clientX: 40, clientY: -20, buttons: 1 });
  assert.ok(classes.has("nm-panning"));
  c.onPointerUp({ type: "pointerup" });
  assert.deepEqual([c.canvas.viewport.x, c.canvas.viewport.y], [40, -20]);
  assert.ok(!classes.has("nm-panning"));
  assert.deepEqual([...c.selected], [a.id], "a pan keeps the selection");
});

test("embed shell writes no shared KConfig profile", async () => {
  const { sessionManager } = await import("../js/session_manager.js");
  const before = localStorage.getItem("kip_shared_kconfig_profiles");
  sessionManager.embedded = true;
  try {
    sessionManager.updateSharedKconfigProfile("probe", { A: "y" });
    assert.equal(localStorage.getItem("kip_shared_kconfig_profiles"), before);
    for (let i = 0; i < localStorage.length; i++) assert.ok(!String(localStorage.getItem(localStorage.key(i))).includes('"probe"'));
  } finally {
    sessionManager.embedded = false;
  }
});
