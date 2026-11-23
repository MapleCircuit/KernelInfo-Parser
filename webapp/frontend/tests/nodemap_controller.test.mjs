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
const { diffSpec, kconfigSpec, mailSpec, maintainerSpec, nodeSpecsForTab, sourceSpec, tabForNode, targetNodemapTab, tokenSpec } = await import("../js/views/nodemap/nodemap_bridge.js");
const { NodeMapController } = await import("../js/views/nodemap/nodemap_controller.js");
const M = await import("../js/views/nodemap/canvas_model.js");
const { state } = await import("../js/state.js");
const { canvasToSvg, pngSize, svgSize } = await import("../js/views/nodemap/nodemap_export.js");
const { structRows } = await import("../js/views/nodemap/struct_view.js");

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

// ------------------------------------------------------------------ NodeMap upgrade: Edit Mode, Kconfig, clipboard, auto-fit

const LAYOUT = {
  file_path: "include/n.h", line_s: 1, line_e: 9, outer_conds: [],
  items: [
    { kind: "field", name: "a", type: "int", line: 2, conds: [] },
    { kind: "directive", directive: "ifdef", expr: "CONFIG_A", text: "#ifdef CONFIG_A", line: 3, depth: 0 },
    { kind: "field", name: "b", type: "long", line: 4, conds: ["defined(CONFIG_A)"] },
    { kind: "directive", directive: "endif", expr: "", text: "#endif", line: 5, depth: 0 },
  ],
};

/** A body click on `[data-act=act]` of node `id`. */
function clickAct(c, id, act, key = "") {
  const btn = { dataset: { act, key } };
  const nodeEl = { dataset: { nodeId: id } };
  c.onClick({ target: { closest: (s) => (s === "[data-act]" ? btn : s === ".nm-node" ? nodeEl : null) }, stopPropagation() {} });
}

const keyEvent = (key, extra = {}) => {
  let prevented = false;
  return { ev: { key, ctrlKey: true, target: { closest: () => null }, preventDefault: () => { prevented = true; }, ...extra }, prevented: () => prevented };
};

test("Edit Mode gates node editing actions; folds and the body key follow it", () => {
  const { c } = controller();
  const s = c.addNode({ type: "struct", data: { name: "n" } });
  c.content.set(c.fetchKeyOf(s), { status: "ok", value: LAYOUT });
  assert.equal(c.canvas.settings.editMode, false, "View Mode by default");
  clickAct(c, s.id, "hide-item", "2:a");
  clickAct(c, s.id, "toggle-type", "2:a");
  clickAct(c, s.id, "all-types");
  assert.deepEqual(s.data.hiddenItems, {}, "no hiding in View Mode");
  assert.deepEqual(s.data.typeOverrides, {});
  assert.equal(s.data.showTypes, true);
  clickAct(c, s.id, "guard-fold", "3:ifdef");
  assert.deepEqual(s.data.collapsedGuards, { "3:ifdef": true }, "branch folds work in View Mode");
  const key = c.bodyKey(s);
  c.setEditMode(true);
  assert.notEqual(c.bodyKey(s), key, "struct bodies redraw with Edit Mode");
  clickAct(c, s.id, "hide-item", "2:a");
  assert.deepEqual(s.data.hiddenItems, { "2:a": true });
  c.onStructAction(s, "show-hidden");
  assert.deepEqual(s.data.hiddenItems, {});
  const tab = c.addNode({ type: "tab", data: { tabType: "commits", hash: "tab=commits" } });
  const tk = c.bodyKey(tab);
  c.setEditMode(false);
  assert.equal(c.bodyKey(tab), tk, "a live tab iframe is not rebuilt by Edit Mode");
  assert.equal(M.normalizeCanvas(M.serializeCanvas(c.canvas)).settings.editMode, false);
});

test("Kconfig engine per node: revision match, canvas choice, per-node and canvas off; folds follow the engine", () => {
  const { c } = controller();
  const engines = [
    { tabId: "k1", title: "Kconfig A", version: "v3.0", arch: "x86_64", resolved: true },
    { tabId: "k2", title: "Kconfig B", version: "v4.0", arch: "arm64", resolved: true },
    { tabId: "k3", title: "Unresolved", version: "v3.0", resolved: false },
  ];
  c.kconfigSource = {
    list: () => engines,
    context: (id) => ({ engineTabId: id, arch: "x86_64", strict: true, assignments: id === "k1" ? { CONFIG_A: "y", A: "y" } : {} }),
  };
  const s = c.addNode({ type: "struct", version: "v3.0", data: { name: "n" } });
  c.content.set(c.fetchKeyOf(s), { status: "ok", value: LAYOUT });
  assert.equal(c.kconfigEngineFor(s).tabId, "k1", "the resolved engine of the node's revision");
  assert.ok(c.evaluatorFor(s), "an evaluator over its values");
  c.canvas.settings.kconfigEngine = "k2";
  c.evalCache.clear();
  assert.equal(c.kconfigEngineFor(s).tabId, "k2", "the canvas's chosen engine wins");
  // k2 has no CONFIG_A: the #ifdef branch is compiled out and folds on its own
  assert.equal(structRows(LAYOUT, s.data, c.structOpts(s)).rows.find((r) => r.key === "3:ifdef").kind, "summary");
  clickAct(c, s.id, "guard-fold", "3:ifdef");
  assert.deepEqual(s.data.collapsedGuards, { "3:ifdef": false }, "a manual unfold overrides the engine");
  clickAct(c, s.id, "guard-fold", "3:ifdef");
  assert.deepEqual(s.data.collapsedGuards, {}, "folding it again follows the engine");
  s.data.disableKconfigEngine = true;
  assert.equal(c.kconfigEngineFor(s), null, "Disable Kconfig engine");
  assert.equal(structRows(LAYOUT, s.data, c.structOpts(s)).rows.find((r) => r.key === "3:ifdef").kind, "pp");
  s.data.disableKconfigEngine = false;
  c.canvas.settings.kconfigCollapse = false;
  assert.equal(c.kconfigEngineFor(s), null, "folding off for the canvas");
  c.canvas.settings.kconfigCollapse = true;
  c.canvas.settings.kconfigEngine = "";
  s.version = "v9.9";
  assert.equal(c.kconfigEngineFor(s), null, "no engine of another revision is guessed");
  const epoch = c.kconfigEpoch;
  c.onKconfigChanged();
  assert.equal(c.kconfigEpoch, epoch + 1, "engine events redraw struct bodies");
  assert.equal(c.evalCache.size, 0);
});


test("copy / paste / duplicate: fresh ids, internal edges, groups, across tabs, native text copy kept", () => {
  localStorage.removeItem(M.CLIPBOARD_KEY);
  const { c } = controller({ id: "tab-a", type: "nodemap", version: "v3.0", repo: "" });
  withViewport(c);
  const a = c.addNode({ type: "note", data: { text: "a" } });
  const b = c.addNode({ type: "struct", data: { name: "b" } });
  Object.assign(a, { x: 0, y: 0 });
  Object.assign(b, { x: 400, y: 0 });
  c.canvas.edges.push(M.createEdge({ from: a.id, to: b.id, label: "uses" }));
  c.selected = new Set([a.id, b.id]);

  // Selected text copies natively: the canvas does not take Ctrl+C or Ctrl+V
  globalThis.getSelection = () => ({ toString: () => "int x;" });
  let k = keyEvent("c");
  c.onKeyDown(k.ev);
  assert.equal(k.prevented(), false);
  assert.equal(localStorage.getItem(M.CLIPBOARD_KEY), null);
  k = keyEvent("v");
  c.onKeyDown(k.ev);
  assert.equal(c.canvas.nodes.length, 2);
  globalThis.getSelection = () => ({ toString: () => "" });
  // Focus in an input keeps the key too
  k = keyEvent("c", { target: { closest: (s) => (s.includes("input") ? {} : null) } });
  c.onKeyDown(k.ev);
  assert.equal(localStorage.getItem(M.CLIPBOARD_KEY), null);

  k = keyEvent("c");
  c.onKeyDown(k.ev);
  assert.equal(k.prevented(), true);
  assert.ok(localStorage.getItem(M.CLIPBOARD_KEY), "the clipboard is shared through localStorage");
  k = keyEvent("v");
  c.onKeyDown(k.ev);
  assert.equal(c.canvas.nodes.length, 4);
  const pasted = c.canvas.nodes.slice(2);
  assert.deepEqual([pasted[0].x, pasted[0].y], [24, 24], "offset from the originals");
  assert.deepEqual([...c.selected].sort(), pasted.map((n) => n.id).sort(), "the pasted nodes are selected");
  assert.equal(c.canvas.edges.length, 2, "the edge between them is pasted too");
  const pe = c.canvas.edges[1];
  assert.ok(pasted.some((n) => n.id === pe.from) && pasted.some((n) => n.id === pe.to));
  c.pasteClipboard();
  assert.deepEqual([c.canvas.nodes[4].x, c.canvas.nodes[4].y], [48, 48], "each paste cascades");

  // Another NodeMap tab pastes the same clipboard, centered in its view
  const { c: other } = controller({ id: "tab-b", type: "nodemap", version: "v3.0", repo: "" });
  withViewport(other);
  assert.equal(other.pasteClipboard(), true);
  assert.equal(other.canvas.nodes.length, 2);
  assert.equal(other.canvas.edges.length, 1);
  const bb = M.boundsOf(other.canvas.nodes);
  assert.equal(Math.round(bb.x + bb.w / 2), 400, "centered on the view (800 wide)");

  // Duplicate (Ctrl+D) keeps groups: a copied group brings its members, in the copy of it
  const g = other.addGroup();
  other.selected = new Set([g.id]);
  k = keyEvent("d");
  other.onKeyDown(k.ev);
  assert.equal(k.prevented(), true);
  const groups = other.canvas.nodes.filter((n) => n.type === "group");
  assert.equal(groups.length, 2);
  const copyGroup = groups[1];
  assert.equal(M.groupDescendants(other.canvas.nodes, copyGroup.id).length, 2, "members are copied into the copied group");
  assert.deepEqual([...other.selected], [copyGroup.id]);
  assert.equal(other.canvas.edges.length, 2);

  // No storage: the window's memory copy still pastes
  const real = localStorage.setItem;
  localStorage.setItem = () => {
    throw new Error("quota");
  };
  try {
    other.selected = new Set([other.canvas.nodes[0].id]);
    other.copySelection();
    assert.equal(localStorage.getItem(M.CLIPBOARD_KEY), null, "a stale stored clipboard is dropped");
    const before = other.canvas.nodes.length;
    assert.equal(other.pasteClipboard(), true);
    assert.equal(other.canvas.nodes.length, before + 1);
  } finally {
    localStorage.setItem = real;
  }
});

test("auto-fit: measured content sizes the node, resize handles keep the aspect ratio and scale", () => {
  const { c } = controller();
  withViewport(c);
  const s = c.addNode({ type: "struct", data: { name: "n" } });
  c.measureNatural = () => ({ w: 400, h: 200 });
  c.selected = new Set([s.id]);
  c.onInspectorInput({ target: { closest: () => ({ dataset: { insp: "autofit" }, type: "checkbox", checked: true, tagName: "INPUT" }) } }, true);
  assert.equal(s.autoFit, true);
  assert.deepEqual([s.baseW, s.baseH, s.w, s.h], [400, 200, 400, 200], "sized to its content on the next commit");
  c.gesture = { kind: "resize", start: { x: 0, y: 0 }, id: s.id, handle: "se", rect: M.rectOf(s), moved: false };
  c.onPointerMove({ clientX: 200, clientY: 10, buttons: 1 });
  assert.deepEqual([s.w, s.h, s.scale], [600, 300, 1.5], "the corner scales by the axis that moved more");
  c.onPointerUp({ type: "pointerup" });
  c.measureNatural = () => ({ w: 500, h: 100 });
  c.fitQueue.add(s.id);
  c.commit();
  assert.deepEqual([s.baseW, s.baseH, s.w, s.h], [500, 100, 750, 150], "changed content refits at the same scale");
  c.onInspectorInput({ target: { closest: () => ({ dataset: { insp: "w" }, value: "250", tagName: "INPUT" }) } }, true);
  assert.deepEqual([s.scale, s.w, s.h], [0.5, 250, 50], "a width in the inspector sets the scale");
  const t = c.addNode({ type: "tab", data: { tabType: "commits", hash: "tab=commits" } });
  M.setAutoFit(t, true);
  assert.equal(t.autoFit, false, "live tab iframes are never fitted");
});

test("custom colors, Duplicate in the node menu, and the SVG export of the controller's content", async () => {
  const { c } = controller();
  withViewport(c);
  const s = c.addNode({ type: "struct", data: { name: "n" } });
  c.content.set(c.fetchKeyOf(s), { status: "ok", value: LAYOUT });
  c.setColor("#12AB34");
  assert.equal(s.color, "#12ab34");
  c.setColor("not-a-color");
  assert.equal(s.color, "");
  assert.equal(c.pickerValue("#a0b0c0"), "#a0b0c0");
  assert.match(c.pickerValue("blue"), /^#[0-9a-f]{6}$/, "presets start the picker at a #rrggbb");
  const svg = canvasToSvg(c.canvas, (x) => x, c.svgOptions());
  assert.match(svg, /<tspan fill="var\(--text-primary\)">b<\/tspan>/, "struct members from the loaded layout");

  const { contextMenu } = await import("../js/components/context_menu.js");
  const realRender = contextMenu.renderMenu;
  let menu = null;
  contextMenu.renderMenu = (x, y, cfg) => {
    menu = cfg;
  };
  try {
    contextMenu.showNodeMapMenu(0, 0, { node: s, controller: c });
    const dup = menu.items.find((i) => i.label === "Duplicate");
    assert.ok(dup, "Duplicate is in the node menu");
    dup.onClick();
    assert.equal(c.canvas.nodes.filter((n) => n.type === "struct").length, 2);
  } finally {
    contextMenu.renderMenu = realRender;
  }
});

test("wrapping auto-fit nodes keep their width across rebuilds; a text selection outside the canvas does not block Ctrl+C", () => {
  const { c } = controller();
  withViewport(c);
  const n = c.addNode({ type: "note", data: { text: "x" } });
  M.setAutoFit(n, true);
  const el = { isConnected: true, offsetWidth: 0, offsetHeight: 0, style: { setProperty() {} }, classList: { add() {}, remove() {} } };
  Object.defineProperty(el, "offsetWidth", { get: () => n.baseW });
  Object.defineProperty(el, "offsetHeight", { get: () => 120 });
  c.renderer.element = () => el;
  const w = n.baseW;
  for (let i = 0; i < 3; i++) {
    c.fitQueue.add(n.id);
    c.commit();
  }
  assert.equal(n.baseW, w, "no slack added to a wrapping node's width");
  assert.equal(n.baseH, 122);
  c.renderer.element = () => null;

  localStorage.removeItem(M.CLIPBOARD_KEY);
  c.rootEl = { contains: (x) => x === "inside", classList: { toggle() {} } };
  c.selected = new Set([n.id]);
  globalThis.getSelection = () => ({ toString: () => "sidebar text", anchorNode: "outside", focusNode: "outside" });
  let k = keyEvent("c");
  c.onKeyDown(k.ev);
  assert.equal(k.prevented(), true, "the nodes are copied");
  localStorage.removeItem(M.CLIPBOARD_KEY);
  globalThis.getSelection = () => ({ toString: () => "node text", anchorNode: "inside", focusNode: "inside" });
  k = keyEvent("c");
  c.onKeyDown(k.ev);
  assert.equal(k.prevented(), false, "text selected inside the canvas copies natively");
  globalThis.getSelection = () => ({ toString: () => "" });
  c.rootEl = null;
});

test("undo / redo: Ctrl+Z, Ctrl+Y and Ctrl+Shift+Z over adds, deletes, paste, duplicate and property edits", () => {
  localStorage.removeItem(M.CLIPBOARD_KEY);
  const { c } = controller({ id: "tab-u", type: "nodemap", version: "v3.0", repo: "" });
  withViewport(c);
  c.commit({ save: false }); // baseline: the empty canvas
  const press = (key, extra = {}) => {
    const k = keyEvent(key, extra);
    c.onKeyDown(k.ev);
    return k.prevented();
  };
  const a = c.addNode({ type: "note", data: { text: "a" } });
  const b = c.addNode({ type: "note", data: { text: "b" } });
  assert.equal(c.canvas.nodes.length, 2);
  assert.equal(press("z"), true);
  assert.deepEqual(c.canvas.nodes.map((n) => n.id), [a.id], "Ctrl+Z removes the last added node");
  assert.equal(press("y"), true);
  assert.deepEqual(c.canvas.nodes.map((n) => n.id), [a.id, b.id], "Ctrl+Y brings it back");
  press("z");
  press("Z", { shiftKey: true });
  assert.equal(c.canvas.nodes.length, 2, "Ctrl+Shift+Z redoes too");

  // Selection changes are not steps; deleting is
  c.selected = new Set([a.id]);
  c.commit({ save: false });
  c.deleteSelection();
  assert.equal(c.byId(a.id), null);
  assert.equal(c.undo(), true);
  assert.ok(c.byId(a.id), "the deleted node is back");
  assert.ok(!c.selected.has("gone"), "selection keeps existing ids only");

  // Property edits, one step per live picker drag
  c.selected = new Set([a.id]);
  const insp = (k, value, final) => c.onInspectorInput({ target: { closest: () => ({ dataset: { insp: k }, value, tagName: "INPUT", type: "color" }) } }, final);
  insp("bg-custom", "#101010", false);
  insp("bg-custom", "#202020", false);
  insp("bg-custom", "#303030", true);
  assert.equal(c.byId(a.id).bgColor, "#303030");
  c.undo();
  assert.equal(c.byId(a.id).bgColor, "", "a whole color drag is one step");
  c.redo();
  assert.equal(c.byId(a.id).bgColor, "#303030");

  // Duplicate and paste
  c.selected = new Set([a.id]);
  press("d");
  assert.equal(c.canvas.nodes.length, 3);
  press("z");
  assert.equal(c.canvas.nodes.length, 2, "duplicate undone");
  c.selected = new Set([a.id]);
  press("c");
  press("v");
  assert.equal(c.canvas.nodes.length, 3);
  press("z");
  assert.equal(c.canvas.nodes.length, 2, "paste undone");
  assert.deepEqual([...c.selected].filter((id) => !c.byId(id)), [], "pasted ids left the selection");

  // A drag is one step from where it started
  const before = [a.x, a.y];
  const live = c.byId(a.id);
  c.gesture = { kind: "drag", start: { x: 0, y: 0 }, primary: live.id, nodes: [{ n: live, x: live.x, y: live.y }], moved: false };
  c.onPointerMove({ clientX: 50, clientY: 40, buttons: 1 });
  c.onPointerMove({ clientX: 90, clientY: 80, buttons: 1 });
  c.onPointerUp({ type: "pointerup" });
  c.undo();
  assert.deepEqual([c.byId(a.id).x, c.byId(a.id).y], before);
  assert.equal(c.byId(a.id), live, "an undo keeps the object of a node that still exists (bodies hold it)");

  // Held arrow keys are one step
  c.selected = new Set([a.id]);
  const x0 = live.x;
  for (let i = 0; i < 5; i++) c.onKeyDown({ key: "ArrowRight", target: { closest: () => null }, preventDefault() {} });
  c.history.close();
  c.undo();
  assert.equal(live.x, x0);

  // Nothing pushed for no-ops; a new change clears redo; Edit Mode is view state
  const depth = c.history.undoList.length;
  c.commit();
  assert.equal(c.history.undoList.length, depth, "no-op commits push nothing");
  c.undo();
  c.addNode({ type: "note", data: {} });
  assert.equal(c.history.canRedo, false);
  c.setEditMode(true);
  c.undo();
  assert.equal(c.canvas.settings.editMode, true, "undo never flips Edit Mode");

  // Typing in a field keeps its native undo
  const n = c.canvas.nodes.length;
  const k = keyEvent("z", { target: { closest: (s) => (s.includes("textarea") ? {} : null) } });
  c.onKeyDown(k.ev);
  assert.equal(k.prevented(), false);
  assert.equal(c.canvas.nodes.length, n);
});

test("undo covers struct toggles, the outer #ifdef banner, header hiding and backgrounds", () => {
  const { c } = controller();
  withViewport(c);
  c.commit({ save: false });
  c.setEditMode(true);
  const s = c.addNode({ type: "struct", data: { name: "foo" } });
  c.content.set(c.fetchKeyOf(s), { status: "ok", value: { ...LAYOUT, outer_conds: ["defined(CONFIG_X)"] } });
  clickAct(c, s.id, "hide-item", M.OUTER_KEY);
  assert.equal(c.byId(s.id).data.hiddenItems[M.OUTER_KEY], true, "the outer banner hides");
  clickAct(c, s.id, "hide-item", M.OUTER_KEY);
  assert.equal(c.byId(s.id).data.hiddenItems[M.OUTER_KEY], undefined, "and is restored");
  clickAct(c, s.id, "hide-item", M.OUTER_KEY);
  c.onStructAction(c.byId(s.id), "show-hidden");
  assert.deepEqual(c.byId(s.id).data.hiddenItems, {}, "Show all hidden restores it");
  c.undo();
  assert.equal(c.byId(s.id).data.hiddenItems[M.OUTER_KEY], true, "undo brings the hidden state back");
  clickAct(c, s.id, "toggle-header");
  assert.equal(c.byId(s.id).hideHeader, true);
  c.undo();
  assert.equal(c.byId(s.id).hideHeader, false);
  c.setEditMode(false);
  clickAct(c, s.id, "toggle-header");
  assert.equal(c.byId(s.id).hideHeader, false, "header toggles are Edit Mode controls");
  assert.doesNotMatch(c.headTools(c.byId(s.id), false), /toggle-header/);
  c.setEditMode(true);
  assert.match(c.headTools(c.byId(s.id), false), /data-act="toggle-header"/);
  c.setCanvasBackground("green");
  assert.equal(c.canvas.settings.bgColor, "green");
  c.undo();
  assert.equal(c.canvas.settings.bgColor, "");
  c.selected = new Set([s.id]);
  c.onInspectorClick({ target: { closest: () => ({ dataset: { insp: "bg", value: "purple" } }) } });
  assert.equal(c.byId(s.id).bgColor, "purple");
  c.onInspectorClick({ target: { closest: () => ({ dataset: { insp: "bg-reset" } }) } });
  assert.equal(c.byId(s.id).bgColor, "", "Reset is the theme default");
});

test("ports: connected ports are tagged as wires render; wiring and Edit Mode classes on the container", () => {
  const { c } = controller();
  withViewport(c);
  const rootClasses = new Set();
  c.rootEl = { classList: { add: (k) => rootClasses.add(k), remove: (k) => rootClasses.delete(k), toggle: (k, on) => (on ? rootClasses.add(k) : rootClasses.delete(k)) }, focus() {} };
  const port = (sel) => {
    const cls = new Set();
    return { sel, cls, classList: { add: (k) => cls.add(k), remove: (k) => cls.delete(k) } };
  };
  const ports = new Map();
  const elFor = (id) => ({
    querySelector: (sel) => {
      if (!/nm-port-(side|field)/.test(sel)) return null;
      const key = `${id} ${sel}`;
      if (!ports.has(key)) ports.set(key, port(sel));
      return ports.get(key);
    },
  });
  c.renderer.element = (id) => elFor(id);
  c.wireSvg = { style: {}, setAttribute() {}, innerHTML: "" };
  c.renderWires = Object.getPrototypeOf(c).renderWires;
  c.fieldPoint = () => null;
  const a = c.addNode({ type: "note", data: {} });
  const b = c.addNode({ type: "note", data: {} });
  Object.assign(a, { x: 0, y: 0 });
  Object.assign(b, { x: 600, y: 0 });
  c.canvas.edges.push(M.createEdge({ from: a.id, to: b.id }));
  c.renderWires();
  const tagged = [...ports.values()].filter((p) => p.cls.has("nm-port-connected")).map((p) => p.sel);
  assert.deepEqual(tagged.sort(), ['.nm-port-side[data-side="left"]', '.nm-port-side[data-side="right"]'], "the facing side ports of both ends");
  c.canvas.edges = [];
  c.renderWires();
  assert.equal([...ports.values()].filter((p) => p.cls.has("nm-port-connected")).length, 0, "untagged when the wire goes");

  c.commit({ save: false });
  assert.equal(rootClasses.has("nm-edit"), false);
  c.setEditMode(true);
  assert.equal(rootClasses.has("nm-edit"), true, "Edit Mode shows every port");
  const portEl = { dataset: { side: "right" } };
  const nodeEl = { dataset: { nodeId: a.id } };
  c.onPointerDown({ button: 0, clientX: 0, clientY: 0, preventDefault() {}, stopPropagation() {}, target: { closest: (s) => (s === ".nm-port" ? portEl : s === ".nm-node" ? nodeEl : null) } });
  assert.equal(rootClasses.has("nm-wiring"), true, "dragging a wire shows the target ports");
  let wiringAtDrop = null;
  c.finishWire = () => {
    wiringAtDrop = rootClasses.has("nm-wiring");
  };
  c.onPointerUp({ type: "pointerup" });
  assert.equal(wiringAtDrop, true, "target ports stay hit-testable while the drop is read");
  assert.equal(rootClasses.has("nm-wiring"), false);
});

test("image export dialog: transparent background and PNG upscale with the output size", () => {
  const { c } = controller();
  withViewport(c);
  c.addNode({ type: "note", data: { text: "x" } });
  c.canvas.settings.bgColor = "#334455";
  const fake = () => {
    const els = {
      transparent: { checked: false },
      scale: { value: "3", listeners: [], addEventListener(ev, fn) { this.listeners.push(fn); } },
      size: { textContent: "" },
      form: { addEventListener() {} },
      btn: {},
    };
    const modal = {
      closed: false,
      close() { this.closed = true; },
      body: { innerHTML: "", querySelector: (s) => (s.includes("transparent") ? els.transparent : s.includes("scale") ? (modal.body.innerHTML.includes('name="scale"') ? els.scale : null) : s.includes("export-size") ? (modal.body.innerHTML.includes("nm-export-size") ? els.size : null) : s === "form" ? els.form : null) },
      footer: { querySelector: () => els.btn },
    };
    return { modal, els };
  };
  let made = null;
  c.openModalFn = (cfg) => {
    made = { cfg, ...fake() };
    return made.modal;
  };
  const downloads = [];
  c.download = (name, blob) => downloads.push(name);
  let svgOpts = null;
  c.exportSVG = function (opts) {
    svgOpts = opts;
    return Object.getPrototypeOf(this).exportSVG.call(this, opts);
  };
  c.openImageExport("svg");
  assert.match(made.cfg.title, /SVG/);
  assert.doesNotMatch(made.modal.body.innerHTML, /name="scale"/, "SVG has no scale");
  made.els.transparent.checked = true;
  made.els.btn.onclick();
  assert.equal(made.modal.closed, true);
  assert.deepEqual(svgOpts, { transparent: true, scale: 3 });
  assert.deepEqual(downloads, ["nodemap_canvas.svg"]);
  assert.doesNotMatch(c.canvasSvg({ transparent: true }), /fill="#334455"/, "a transparent image has no background");
  assert.match(c.canvasSvg(), /fill="#334455"/);

  let pngOpts = null;
  c.exportPNG = (opts) => {
    pngOpts = opts;
  };
  c.openImageExport("png");
  const { w, h } = svgSize(c.canvasSvg({ foreign: false }));
  assert.equal(made.els.size.textContent, `Output: ${w * 3} × ${h * 3} px`, "3× by default");
  made.els.scale.value = "2";
  made.els.scale.listeners.forEach((fn) => fn());
  assert.equal(made.els.size.textContent, `Output: ${w * 2} × ${h * 2} px`);
  made.els.btn.onclick();
  assert.deepEqual(pngOpts, { transparent: false, scale: 2 });
  assert.deepEqual(pngSize(w, h, 4), { w: w * 4, h: h * 4, scale: 4, clamped: false });
});

// ------------------------------------------------------------------ tab drag-in, embedded sessions, copy to session, domain nodes

const flush = (ms = 0) => new Promise((r) => realSetTimeout(r, ms));

test("Send to NodeMap specs of the domain views; tabs convert to native diff / mail / kconfig / maintainer nodes", () => {
  assert.deepEqual(diffSpec({ versionA: "v3.0", versionB: "v3.1", path: "a.c" }).data, { mode: "file", versionA: "v3.0", versionB: "v3.1", path: "a.c", oldPath: "" });
  assert.equal(diffSpec({ versionA: "v3.0", versionB: "v3.1" }), null, "a file diff needs its file");
  assert.equal(diffSpec({ commitHash: "abc123", version: "v3.0" }).data.mode, "commit");
  assert.equal(mailSpec({}), null);
  assert.equal(mailSpec({ tid: "5", mid: "m@x" }).data.tid, 5);
  assert.equal(kconfigSpec({ symbol: "CONFIG_SMP" }).data.symbol, "SMP");
  assert.equal(kconfigSpec({ symbol: "a b" }), null);
  assert.equal(maintainerSpec({ secId: "3" }).data.secId, 3);
  const T = (tab) => nodeSpecsForTab({ version: "v3.0", ...tab }, hashOf);
  assert.equal(T({ type: "diff", versionA: "v3.0", versionB: "v3.1", path: "a.c" })[0].type, "diff");
  assert.equal(T({ type: "diff", versionA: "v3.0", versionB: "v3.1" })[0].type, "tab", "a whole-revision compare stays a live tab");
  const commit = T({ type: "commits", commitId: "abc123" })[0];
  assert.equal(commit.type, "commit", "a commit of the Commits tab becomes a commit details node");
  assert.deepEqual(commit.data, { commitHash: "abc123" });
  assert.equal(T({ type: "commits", commitId: "not-a-hash" })[0].type, "tab", "anything but a hash stays a live tab");
  assert.deepEqual(T({ type: "mail", threadId: "9", messageId: "m@x" })[0].data, { tid: 9, mid: "m@x", subject: "" });
  assert.deepEqual(T({ type: "kconfig", symbol: "SMP", arch: "arm64" })[0].data, { symbol: "SMP", arch: "arm64" });
  assert.equal(T({ type: "kconfig", arch: "arm64" })[0].type, "tab", "a Kconfig tab without a symbol embeds live");
  assert.equal(T({ type: "maintainers", secId: 4, section: "SCHED" })[0].type, "maintainer");
  assert.equal(nodeSpecsForTab({ type: "kconfig", symbol: "SMP", version: "v3.0" }, hashOf, { live: true })[0].type, "tab");
  // Nodes open as the matching workspace tab (Open as tab, Copy to Session)
  const n = (type, data, extra = {}) => M.createNode(type, { version: "v3.0", data, ...extra });
  assert.deepEqual(tabForNode(n("kconfig", { symbol: "SMP" })), { type: "kconfig", title: "KConfig (x86_64)", symbol: "SMP", arch: "x86_64", repo: "", version: "v3.0" });
  assert.equal(tabForNode(n("diff", { commitHash: "abcdef1" })).commitId, "abcdef1");
  assert.deepEqual([tabForNode(n("mail", { tid: 5, mid: "m" })).threadId, tabForNode(n("mail", { tid: 5, mid: "m" })).messageId], [5, "m"]);
  assert.equal(tabForNode(n("maintainer", { secId: 3, title: "S" })).secId, 3);
  const t = tabForNode(n("tab", { tabType: "code", hash: "v=v3.1&tab=code&file=a.c&line=4-9" }));
  assert.deepEqual([t.type, t.path, t.cursorLine, t.lineEnd, t.version], ["code", "a.c", 4, 9, "v3.1"], "a tab node's address as tab fields");
  assert.equal(tabForNode(n("note", { text: "x" })), null);
});

test("tabs dropped on the canvas are copied in at the drop point: native nodes, Shift for a live tab", async () => {
  const { sessionManager } = await import("../js/session_manager.js");
  const { c } = controller();
  withViewport(c);
  c.canvas.viewport = { x: 100, y: 50, zoom: 2 };
  const copied = [];
  const real = sessionManager.broadcastTabDropCopied;
  sessionManager.broadcastTabDropCopied = (m) => copied.push(m);
  try {
    const tab = { id: "t-code", type: "code", path: "kernel/fork.c", version: "v3.0", title: "fork.c" };
    const [src] = c.dropTab({ tabId: tab.id, tabData: tab }, 500, 450);
    assert.equal(src.type, "source");
    assert.deepEqual([src.x + src.w / 2, src.y + src.h / 2], [200, 200], "centered on the drop point in canvas units");
    assert.deepEqual([...c.selected], [src.id]);
    const [live] = c.dropTab({ tabId: tab.id, tabData: tab, sourceWinId: "other-win" }, 500, 450, { live: true });
    assert.equal(live.type, "tab", "Shift drops a live tab node");
    assert.equal(live.data.embedSessionId, "nms-1", "running in the canvas's first embedded session");
    assert.deepEqual(copied, [{ tabId: "t-code", sourceWinId: "other-win" }], "the window it came from keeps its tab");
    assert.deepEqual(c.dropTab({ tabData: { type: "nodemap", id: "x" } }, 0, 0), [], "a canvas is never dropped into a canvas");
    assert.equal(c.canvas.nodes.length, 2);
    const ev = (types) => ({ dataTransfer: { types } });
    assert.equal(c.acceptsTabDrag(ev(["application/kip-tab"])), true);
    assert.equal(c.acceptsTabDrag(ev(["Files"])), false);
  } finally {
    sessionManager.broadcastTabDropCopied = real;
  }
});

/** A stand-in BroadcastChannel factory: posted messages and closes are recorded. */
function fakeChannels() {
  const made = [];
  const factory = (name) => {
    const ch = { name, posted: [], closed: false, onmessage: null, postMessage(m) { this.posted.push(m); }, close() { this.closed = true; } };
    made.push(ch);
    return ch;
  };
  return { made, factory };
}

test("embedded sessions: tab iframes join one; the canvas follows its Kconfig engines over the scoped channel", () => {
  const { c } = controller();
  withViewport(c);
  const { made, factory } = fakeChannels();
  c.channelFactory = factory;
  c.kconfigSource = { list: () => [], context: () => null };
  const before = localStorage.getItem("kip_sessions_index");
  const t = c.addNode({ type: "tab", data: { tabType: "kconfig", hash: "tab=kconfig&v=v3.0" } });
  assert.equal(t.data.embedSessionId, "nms-1");
  const body = { innerHTML: "", querySelector: () => null };
  c.renderBody(t, body);
  assert.match(body.innerHTML, /\?embed=1&amp;embedSession=tab-1%3Anms-1#tab=kconfig/, "the iframe joins the canvas-scoped session");
  // Inspector: a second session, the tab moved to it, the canvas following it
  c.selected = new Set();
  c.onInspectorClick({ target: { closest: () => ({ dataset: { insp: "es-add" } }) } });
  assert.equal(c.canvas.settings.embedSessions.length, 2);
  c.selected = new Set([t.id]);
  c.onInspectorInput({ target: { closest: () => ({ dataset: { insp: "d-embedSessionId" }, value: "nms-2", tagName: "SELECT" }) } }, true);
  assert.equal(t.data.embedSessionId, "nms-2");
  assert.match(c.bodyKey(t), /tab-1:nms-2/, "a new session rebuilds the iframe");
  c.onInspectorInput({ target: { closest: () => ({ dataset: { insp: "follow-session" }, value: "nms-2", tagName: "SELECT" }) } }, true);
  const ch = made.at(-1);
  assert.equal(ch.name, "kip_nodemap_session:tab-1:nms-2");
  assert.deepEqual(ch.posted.map((m) => m.type), ["kconfig:request_engines"], "the session's iframes are asked for their engines");
  // An embedded Kconfig tab announces its engine, then edits a value
  const s = c.addNode({ type: "struct", version: "v3.0", data: { name: "n" } });
  c.content.set(c.fetchKeyOf(s), { status: "ok", value: LAYOUT });
  const k = c.addNode({ type: "kconfig", version: "v3.0", data: { symbol: "A" } });
  c.content.set(c.fetchKeyOf(k), { status: "ok", value: { detail: { name: "A" }, graph: { edges: [] } } });
  assert.equal(c.kconfigValuesFor(k), null, "nothing announced yet");
  const epoch = c.kconfigEpoch;
  ch.onmessage({ data: { type: "kconfig:engine:announced", tabId: "kt", winId: "w1", version: "v3.0", arch: "x86_64", assignments: { CONFIG_A: "y", A: "y" } } });
  assert.equal(c.kconfigEpoch, epoch + 1, "an engine announcement redraws");
  assert.equal(c.kconfigEngineFor(s).tabId, "kt");
  assert.equal(structRows(LAYOUT, s.data, c.structOpts(s)).rows.find((r) => r.key === "3:ifdef").kind, "pp", "CONFIG_A=y keeps the branch open");
  assert.match(c.bodyKey(k), /^\["y"/, "the Kconfig node shows y");
  ch.onmessage({ data: { type: "kconfig:assignments:broadcast", tabId: "kt", assignments: { CONFIG_B: "y", B: "y" } } });
  c.evalCache.clear();
  assert.equal(structRows(LAYOUT, s.data, c.structOpts(s)).rows.find((r) => r.key === "3:ifdef").kind, "summary", "an edit in the embedded tab folds the struct's #ifdef");
  assert.match(c.bodyKey(k), /^\["n"|^\[""/, "and the Kconfig node's value follows");
  ch.onmessage({ data: { type: "kconfig:assignments:broadcast", tabId: "unknown", assignments: {} } });
  assert.equal(ch.posted.at(-1).type, "kconfig:request_engines", "an unknown engine is asked to announce itself");
  ch.onmessage({ data: { type: "kconfig:engine:revoked", tabId: "kt", winId: "other" } });
  assert.ok(c.embedEngines.has("kt"), "only the announcing window revokes");
  ch.onmessage({ data: { type: "kconfig:engine:revoked", tabId: "kt", winId: "w1" } });
  assert.equal(c.embedEngines.size, 0);
  // Back to the host workspace: the scoped channel closes; deleting a session keeps one
  c.selected = new Set();
  c.onInspectorInput({ target: { closest: () => ({ dataset: { insp: "follow-session" }, value: "", tagName: "SELECT" }) } }, true);
  assert.ok(ch.closed);
  c.onInspectorClick({ target: { closest: () => ({ dataset: { insp: "es-delete", id: "nms-2" } }) } });
  assert.deepEqual(c.canvas.settings.embedSessions.map((x) => x.id), ["nms-1"]);
  assert.equal(t.data.embedSessionId, "nms-1");
  assert.equal(localStorage.getItem("kip_sessions_index"), before, "embedded sessions never reach the workspace session index");
  c.canvas.settings.followSession = "nms-1";
  c.syncFollowChannel();
  const last = made.at(-1);
  c.dispose();
  assert.ok(last.closed, "dispose closes the followed channel");
});

test("embed shells write no workspace session: index, records and pins stay untouched", async () => {
  const { sessionManager } = await import("../js/session_manager.js");
  const keys = () => JSON.stringify(Object.keys(localStorage).sort().map((k) => [k, localStorage.getItem(k)]));
  const before = keys();
  sessionManager.embedded = true;
  try {
    assert.deepEqual(sessionManager.getSessionsIndex(), [], "Switch Session lists nothing in an embed shell");
    sessionManager.saveSessionsIndex([{ id: "x" }]);
    sessionManager.saveActiveSession({ panes: [] });
    assert.equal(sessionManager.createSession({ name: "n" }), null);
    assert.equal(sessionManager.pushTabToStoredSession("x", { tabData: { id: "t" } }), null);
    sessionManager.pinSessionToWindow("x");
    assert.equal(keys(), before);
  } finally {
    sessionManager.embedded = false;
  }
});

test("Copy to Session: targets every other workspace session and hands the node over as a tab", async () => {
  const { c } = controller();
  withViewport(c);
  const pushed = [];
  const stored = [];
  c.sessions = {
    getSessionsIndex: () => [{ id: "cur", name: "Current" }, { id: "s2", name: "Other" }],
    getWindowId: () => "w-1",
    subscribe: () => () => {},
    broadcastTabPush: (p, id) => pushed.push([p, id]),
    pushTabToStoredSession: (id, p) => (stored.push([id, p]), p.tabData),
  };
  const prev = state.currentSessionId;
  state.currentSessionId = "cur";
  try {
    assert.deepEqual(c.copyTargets().map((x) => x.id), ["s2"], "never the session this window holds");
    const k = c.addNode({ type: "kconfig", version: "v3.0", data: { symbol: "SMP", arch: "arm64" } });
    assert.ok(c.copyNodeToSession(k.id, "s2"));
    assert.equal(pushed.length, 1);
    assert.equal(pushed[0][1], "s2");
    assert.deepEqual([pushed[0][0].tabData.type, pushed[0][0].tabData.symbol, pushed[0][0].tabData.arch], ["kconfig", "SMP", "arm64"]);
    assert.ok(c.byId(k.id), "the node stays on the canvas");
    await flush(450);
    assert.equal(stored.length, 1, "no window took it live: written into the session's stored record");
    assert.equal(c.copyNodeToSession(k.id, "cur"), false, "not to the current session");
    c.sessions.getSessionsIndex = () => [{ id: "cur" }];
    assert.deepEqual(c.copyTargets(), []);
  } finally {
    state.currentSessionId = prev;
  }
});

test("content scale in the inspector: any non-group node, batch over a selection, independent of auto-fit", () => {
  const { c } = controller();
  withViewport(c);
  const a = c.addNode({ type: "source", data: { path: "a.c" } });
  const b = c.addNode({ type: "tab", data: { tabType: "commits", hash: "tab=commits" } });
  const g = c.addGroup();
  const [w, h] = [a.w, a.h];
  c.selected = new Set([a.id, b.id, g.id]);
  c.onInspectorInput({ target: { closest: () => ({ dataset: { insp: "content-scale" }, value: "150", tagName: "INPUT", type: "range" }) } }, true);
  assert.deepEqual([a.scale, b.scale, g.scale], [1.5, 1.5, 1], "groups have none");
  assert.deepEqual([a.w, a.h], [w, h], "the outer box stays");
  c.onInspectorClick({ target: { closest: () => ({ dataset: { insp: "scale-reset" } }) } });
  assert.deepEqual([a.scale, b.scale], [1, 1]);
  c.selected = new Set([a.id]);
  c.measureNatural = () => ({ w: 300, h: 100 });
  c.onInspectorInput({ target: { closest: () => ({ dataset: { insp: "autofit" }, type: "checkbox", checked: true, tagName: "INPUT" }) } }, true);
  c.onInspectorInput({ target: { closest: () => ({ dataset: { insp: "content-scale" }, value: "200", tagName: "INPUT", type: "number" }) } }, true);
  assert.deepEqual([a.scale, a.w, a.h], [2, a.baseW * 2, a.baseH * 2], "an auto-fitted node grows with its scale");
  const svg = canvasToSvg(c.canvas, (x) => x, c.svgOptions());
  assert.match(svg, /scale\(2\)/);
});

test("collapsing a guard of an auto-fitted struct measures it again", () => {
  const { c } = controller();
  withViewport(c);
  const s = c.addNode({ type: "struct", data: { name: "n" } });
  c.content.set(c.fetchKeyOf(s), { status: "ok", value: LAYOUT });
  let measured = 0;
  c.measureNatural = () => (measured++, { w: 300, h: 200 });
  M.setAutoFit(s, true);
  c.canvas.settings.editMode = true;
  measured = 0;
  clickAct(c, s.id, "guard-fold", "3:ifdef");
  assert.ok(measured > 0, "a fold re-fits");
  measured = 0;
  c.onKconfigChanged();
  assert.ok(c.fitQueue.has(s.id), "an engine change re-fits auto-fitted structs");
});

const THREAD = {
  tid: 5, total: 3, messages: [
    { message_id: "root@x", subject: "[PATCH 0/1] t", timestamp: 1, references: "" },
    { message_id: "p1@x", subject: "[PATCH 1/1] one", timestamp: 2, references: "<root@x>" },
    { message_id: "r1@x", subject: "Re: one", timestamp: 3, references: "<root@x> <p1@x>" },
  ],
};

test("mail nodes: Expand Thread to Nodes lays out the reply tree with reply edges, never twice", () => {
  const { c } = controller();
  withViewport(c);
  const th = c.addNode({ type: "mail", data: { tid: 5 } });
  c.content.set(c.fetchKeyOf(th), { status: "ok", value: THREAD });
  const created = c.expandThreadToNodes(th.id);
  assert.equal(created.length, 3);
  const byMid = new Map(created.map((n) => [n.data.mid, n]));
  assert.ok(created.every((n) => n.data.single && n.data.tid === 5));
  const edge = (a, b) => c.canvas.edges.find((e) => e.from === a.id && e.to === b.id);
  assert.equal(edge(th, byMid.get("root@x")).label, "reply", "the root hangs off the thread node");
  assert.ok(edge(byMid.get("root@x"), byMid.get("p1@x")));
  assert.ok(edge(byMid.get("p1@x"), byMid.get("r1@x")), "a reply hangs off the message it answers");
  assert.ok(byMid.get("r1@x").x > byMid.get("p1@x").x, "one column per reply depth");
  assert.deepEqual(c.expandThreadToNodes(th.id), [], "messages already on the canvas are not copied");
  assert.equal(c.canvas.edges.length, 3, "nor connected twice");
  assert.equal(c.tabOfNode(byMid.get("p1@x")).messageId, "p1@x");
});

test("kconfig nodes: relation chips and Expand Relations spawn or link nodes with labeled edges; live value", () => {
  const { c } = controller();
  withViewport(c);
  c.canvas.settings.followSession = "";
  c.kconfigSource = {
    list: () => [{ tabId: "k1", version: "v3.0", resolved: true }],
    context: (id) => ({ engineTabId: id, assignments: { CONFIG_SMP: "y", SMP: "y" }, arch: "x86_64", strict: true }),
  };
  const k = c.addNode({ type: "kconfig", version: "v3.0", data: { symbol: "SMP" } });
  const numa = c.addNode({ type: "kconfig", version: "v3.0", data: { symbol: "NUMA" } });
  c.content.set(c.fetchKeyOf(k), { status: "ok", value: { detail: { name: "SMP", depends_on: ["X86_LOCAL_APIC"], selects: [] }, graph: { edges: [{ from: "NUMA", to: "SMP", type: "depends_on" }, { from: "X86_64_SMP", to: "SMP", type: "selects" }] } } });
  assert.equal(kconfigValueOf(c, k), "y", "the followed engine's value");
  const one = c.expandKconfigRelations(k.id, "depends_on:X86_LOCAL_APIC");
  assert.equal(one.length, 1);
  assert.equal(one[0].data.symbol, "X86_LOCAL_APIC");
  assert.ok(one[0].x > k.x, "a dependency is placed to the right");
  const rest = c.expandKconfigRelations(k.id);
  assert.deepEqual(rest.map((n) => n.data.symbol), ["X86_64_SMP"], "NUMA is already on the canvas: linked, not copied");
  const labels = c.canvas.edges.map((e) => [c.byId(e.from).data.symbol, e.label, c.byId(e.to).data.symbol]);
  assert.deepEqual(labels.sort(), [["NUMA", "depends on", "SMP"], ["SMP", "depends on", "X86_LOCAL_APIC"], ["X86_64_SMP", "selects", "SMP"]]);
  assert.ok(numa.x !== undefined);
  c.expandKconfigRelations(k.id);
  assert.equal(c.canvas.edges.length, 3, "edges are drawn once");
});

function kconfigValueOf(c, node) {
  return JSON.parse(c.bodyKey(node))[0];
}

test("maintainer nodes resolve their section from a file or a name", async () => {
  const { c } = controller();
  const calls = [];
  c.api = {
    matchMaintainers: async (v, patch, files) => (calls.push(["match", files]), { subsystems: ["SCHEDULER"] }),
    getMaintainersOverview: async (v, q) => (calls.push(["overview", q]), { sections: [{ sec_id: 7, name: "SCHEDULER" }, { sec_id: 8, name: "SCHEDULER FOO" }] }),
    getMaintainerSection: async (v, id) => (calls.push(["section", id]), { section: { sec_id: id, name: "SCHEDULER", status: "Maintained" } }),
  };
  const byPath = c.addNode({ type: "maintainer", version: "v3.0", data: { path: "kernel/sched/core.c" } });
  const res = await c.loadMaintainer(byPath);
  assert.equal(res.section.sec_id, 7);
  assert.deepEqual(calls.map((x) => x[0]), ["match", "overview", "section"]);
  calls.length = 0;
  const byName = c.addNode({ type: "maintainer", version: "v3.0", data: { query: "scheduler foo" } });
  assert.equal((await c.loadMaintainer(byName)).section.sec_id, 8, "the exact name wins");
  c.content.set(c.fetchKeyOf(byName), { status: "ok", value: { section: { sec_id: 8, name: "SCHEDULER FOO", status: "Supported" } } });
  assert.equal(c.headMeta(byName).text, "Supported");
});

test("a departed embed shell retires its engines; a thread found by Message-ID keeps its loaded content", async () => {
  const { c } = controller();
  withViewport(c);
  const { made, factory } = fakeChannels();
  c.channelFactory = factory;
  c.kconfigSource = { list: () => [], context: () => null };
  c.syncFollowChannel();
  const ch = made.at(-1);
  ch.onmessage({ data: { type: "kconfig:engine:announced", tabId: "kt", winId: "w9", version: "v3.0", assignments: { A: "y", CONFIG_A: "y" } } });
  assert.equal(c.followedEngines().length, 1);
  ch.onmessage({ data: { type: "window:unloading", winId: "w9" } });
  assert.equal(c.followedEngines().length, 0, "the shell's engine left with it");
  let fetches = 0;
  c.api = { getMailThread: async () => (fetches++, { tid: 42, subject: "Subj", messages: [{ message_id: "m@x", tid: 42, subject: "Subj" }] }) };
  const n = c.addNode({ type: "mail", data: { mid: "m@x" } });
  const body = { innerHTML: "", querySelector: () => null };
  c.renderBody(n, body);
  await flush(5);
  assert.equal(n.data.tid, 42);
  assert.equal(c.content.get(c.fetchKeyOf(n))?.status, "ok", "re-keyed under the thread id: no second fetch");
  c.renderBody(n, body);
  await flush(5);
  assert.equal(fetches, 1);
  c.dispose();
});

test("commit diff lines from the server are coerced before they reach the HTML", async () => {
  const D = await import("../js/views/nodemap/domain_nodes.js");
  const p = D.diffPayload({ mode: "commit", commitHash: "abc", path: "" }, { files: [{ path: "a", hunks: [{ old_start: 1, new_start: 1, lines: [["<b>", "<i>", 2, "x"]] }] }] });
  assert.deepEqual(p.files[0].hunks[0].lines[0], [" ", null, 2, "x"]);
  assert.doesNotMatch(D.renderDiffBody(p), /<b>|<i>/);
});

test("header Apply moves the selected versioned nodes in one undo step, else leaves them to the tab switch", async () => {
  const VS = await import("../js/components/version_switch.js");
  const tab = { id: "tab-apply", type: "nodemap", version: "v6.12", repo: "" };
  const { c } = controller(tab);
  const unregister = VS.registerSelectionVersionApplier(tab.id, (ref) => c.applyVersionToSelection(ref));
  const s = c.addNode({ type: "struct", version: "v3.0", data: { name: "task_struct" } });
  const src = c.addNode({ type: "source", version: "v6.12", data: { path: "kernel/fork.c" } });
  const d = c.addNode({ type: "diff", version: "v6.1", data: { mode: "file", versionA: "v6.1", versionB: "v6.6", path: "mm/slub.c" } });
  const t = c.addNode({ type: "tab", data: { tabType: "commits", hash: "v=v3.0&tab=commits" } });
  const note = c.addNode({ type: "note", data: { text: "n" } });
  const g = c.addNode({ type: "group", data: {} });

  c.selected = new Set();
  c.commit({ save: false });
  assert.equal(VS.applyVersionToSelection("v6.12", tab), null, "no selection: the tab's default revision switches");
  assert.equal(s.version, "v3.0");

  // The tab is already on v6.12, the selected struct is not: Apply still moves it
  c.selected = new Set([s.id, src.id, d.id, t.id, note.id, g.id]);
  c.commit({ save: false });
  const steps = c.history.undoList.length;
  const res = VS.applyVersionToSelection("v6.12", tab);
  assert.equal(res.level, "success");
  assert.deepEqual(new Set(res.changedIds), new Set([s.id, d.id, t.id]));
  assert.equal(res.unchanged, 1, "the source node is already on it");
  assert.equal(res.skipped, 2, "note and group have no revision");
  assert.match(res.message, /^Updated 3 selected nodes to v6\.12/);
  assert.equal(s.version, "v6.12");
  assert.equal(d.data.versionB, "v6.12", "a file diff moves its To side");
  assert.equal(d.data.versionA, "v6.1");
  assert.equal(new URLSearchParams(t.data.hash).get("v"), "v6.12", "a tab node moves its embedded address");
  assert.equal(c.history.undoList.length, steps + 1, "one undo step");
  c.undo();
  assert.equal(c.byId(s.id).version, "v3.0", "undo restores every node at once");
  assert.equal(c.byId(d.id).data.versionB, "v6.6");
  assert.equal(new URLSearchParams(c.byId(t.id).data.hash).get("v"), "v3.0");

  c.selected = new Set([note.id, g.id]);
  const none = VS.applyVersionToSelection("v6.12", tab);
  assert.equal(none.level, "info", "only versionless nodes: nothing changes");
  assert.deepEqual(none.changedIds, []);

  // CVE release checks move; nodes of another repository keep their refs
  const cve = c.addNode({ type: "cve", version: "v6.1", data: { cveId: "CVE-2024-1", release: "v6.1" } });
  const other = c.addNode({ type: "struct", version: "v6.1", repo: "other-repo", data: { name: "x" } });
  c.selected = new Set([cve.id, other.id]);
  const mixed = VS.applyVersionToSelection("v6.12", tab);
  assert.deepEqual(mixed.changedIds, [cve.id]);
  assert.equal(cve.data.release, "v6.12");
  assert.equal(other.version, "v6.1");
  assert.equal(mixed.otherRepo, 1);
  assert.match(mixed.message, /1 of another repository skipped/);

  unregister();
  c.selected = new Set([c.byId(s.id).id]);
  assert.equal(VS.applyVersionToSelection("v6.12", tab), null, "an unregistered view leaves Apply to the tab");
});

test("inspector pickers commit through the input handler: revision popover, multi-selection revision, struct pick", async () => {
  const I = await import("../js/views/nodemap/nodemap_inspector.js");
  const { c } = controller();
  const s = c.addNode({ type: "struct", version: "v3.0", data: { name: "foo" } });
  const opened = [];
  c.refPicker = { open: (anchor, opts) => opened.push({ anchor, opts }) };
  c.commit({ save: false });
  const steps = c.history.undoList.length;
  I.onInspectorClick(c, { target: { closest: () => ({ dataset: { insp: "pick", pick: "version", for: "version" } }) } });
  assert.equal(opened.length, 1);
  assert.equal(opened[0].opts.current, "v3.0");
  assert.equal(opened[0].opts.scoped, false, "every tag and commit, as in Compare");
  opened[0].opts.onSelect("v6.6");
  assert.equal(s.version, "v6.6");
  assert.equal(c.history.undoList.length, steps + 1);

  // A pick that arrives after the selection moved to another node is dropped
  const other = c.addNode({ type: "struct", version: "v3.0", data: { name: "other" } });
  c.selected = new Set([s.id]);
  I.onInspectorClick(c, { target: { closest: () => ({ dataset: { insp: "pick", pick: "version", for: "version" } }) } });
  c.selected = new Set([other.id]);
  opened.at(-1).opts.onSelect("v5.0");
  assert.equal(other.version, "v3.0");
  assert.equal(s.version, "v6.6");
  c.selected = new Set([s.id]);
  opened.splice(1);

  // Diff From / To browse their own side
  const d = c.addNode({ type: "diff", version: "v3.0", data: { mode: "file", versionA: "v2.6.39", versionB: "v3.0", path: "" } });
  I.onInspectorClick(c, { target: { closest: () => ({ dataset: { insp: "pick", pick: "version", for: "d-versionA" } }) } });
  opened[1].opts.onSelect("v2.6.38");
  assert.equal(d.data.versionA, "v2.6.38");

  // Multi-selection Revision field
  const n = c.addNode({ type: "note", data: { text: "x" } });
  c.selected = new Set([s.id, d.id, n.id]);
  I.onInspectorInput(c, { target: { closest() { return this; }, dataset: { insp: "multi-version" }, value: "v6.1", tagName: "INPUT", type: "text" } }, true);
  assert.equal(s.version, "v6.1");
  assert.equal(d.data.versionB, "v6.1");

  // Struct autocomplete pick fills name, defining file and kind in one step
  c.selected = new Set([s.id]);
  c.commit({ save: false });
  const before = c.history.undoList.length;
  I.commitStructPick(c, { name: "bar", kind: "C_union", path: "include/linux/bar.h" });
  assert.equal(s.data.name, "bar");
  assert.equal(s.data.path, "include/linux/bar.h");
  assert.equal(s.data.kind, "union");
  assert.equal(c.history.undoList.length, before + 1);
});

test("dialog pickers: revision popover and autocomplete fill the form inputs", async () => {
  const P = await import("../js/views/nodemap/nodemap_pickers.js");
  const { FakeElement } = await import("./helpers/dom_shim.mjs");
  const mk = (name, value = "") => Object.assign(new FakeElement("input"), { name, value });
  const inputs = { version: mk("version", "v3.0"), name: mk("name"), path: mk("path"), kind: mk("kind", "struct") };
  inputs.name.dataset.ac = "struct";
  const vBtn = new FakeElement("button");
  vBtn.dataset = { pick: "version", for: "version" };
  const fBtn = new FakeElement("button");
  fBtn.dataset = { pick: "file", for: "path" };
  const form = {
    querySelector: (s) => inputs[/name="(\w+)"/.exec(s)?.[1]] || null,
    querySelectorAll: (s) => (s === "[data-pick]" ? [vBtn, fBtn] : s === "input[data-ac]" ? [inputs.name] : []),
  };
  const opened = [];
  const modals = [];
  P.wireFormPickers(form, {
    client: { getDirectoryTree: async () => ({ entries: [] }) },
    version: () => inputs.version.value,
    picker: { open: (a, o) => opened.push(o) },
    openModalFn: (cfg) => {
      const m = { cfg, body: { innerHTML: "", querySelector: () => null }, footer: { querySelector: () => null }, close() {} };
      modals.push(m);
      return m;
    },
  });
  vBtn.dispatchEvent({ type: "click" });
  opened[0].onSelect("v6.6");
  assert.equal(inputs.version.value, "v6.6");
  inputs.path.value = "include/linux/sched.h";
  fBtn.dispatchEvent({ type: "click" });
  assert.equal(modals.length, 1, "Browse… opens the File Explorer");
  assert.match(modals[0].cfg.subtitle, /v6\.6/, "scoped to the form's revision");
  assert.ok(inputs.name._nmAutocomplete, "the struct name autocompletes");
});
