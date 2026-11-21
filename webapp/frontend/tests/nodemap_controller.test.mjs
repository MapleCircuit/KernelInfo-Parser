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
