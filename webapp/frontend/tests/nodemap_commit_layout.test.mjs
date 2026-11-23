/**
 * nodemap_commit_layout.test.mjs - NodeMap multi-selection Inspector controls (Auto-fit & Scale, Hide
 * header, Revision Apply, selection-scoped Version Time-Travel), selection / group scoped Auto-Layout,
 * visibility layer checkboxes, crisp rendering (2D transforms, device pixel snapping, tab iframe
 * supersampling), the native `commit` node and the Blame node's commit / diff spawning and bulk
 * expands with their confirmation gate.
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

const M = await import("../js/views/nodemap/canvas_model.js");
const { NodeMapController, applyFrameSupersample } = await import("../js/views/nodemap/nodemap_controller.js");
const I = await import("../js/views/nodemap/nodemap_inspector.js");
const G = await import("../js/views/nodemap/nodemap_gestures.js");
const D = await import("../js/views/nodemap/domain_nodes.js");
const { tabForNode, commitSpec } = await import("../js/views/nodemap/nodemap_bridge.js");
const { specFromForm } = await import("../js/views/nodemap/nodemap_dialogs.js");
const { canvasToSvg } = await import("../js/views/nodemap/nodemap_export.js");
const { headMetaOf } = await import("../js/views/nodemap/node_content.js");
const { nodeBox } = await import("../js/views/nodemap/node_renderer.js");
const { renderVirtualSourceWindow, computeVirtualRange } = await import("../js/views/nodemap/virtual_source.js");

const SHA = (i) => i.toString(16).padStart(40, "a").slice(-40);

function controller(tab = { id: "cl-tab", type: "nodemap", version: "v3.0", repo: "" }) {
  const c = new NodeMapController();
  c.tab = tab;
  c.currentVersion = tab.version;
  c.repo = tab.repo;
  c.storageKey = `${M.STORAGE_PREFIX}${tab.id}`;
  c.canvas = M.emptyCanvas();
  c.renderer = { sync() {}, invalidate() {}, element: () => null, clear() {} };
  c.renderInspector = () => {};
  c.renderWires = () => {};
  c.workerPool = { workerSupported: false };
  c.rootEl = { querySelector: () => null, classList: { toggle() {} } };
  c.commit({ save: false });
  return c;
}

/** The Inspector element of a checkbox `k` (`input` / `change` both reach `onInspectorInput`). */
const checkbox = (k, checked, dataset = {}) => {
  const el = { dataset: { insp: k, ...dataset }, type: "checkbox", checked, tagName: "INPUT" };
  return { target: { closest: () => el } };
};
/** One click of a checkbox: its `input` then its `change` event. */
const clickCheckbox = (c, k, checked, dataset = {}) => {
  I.onInspectorInput(c, checkbox(k, checked, dataset), false);
  I.onInspectorInput(c, checkbox(k, checked, dataset), true);
};
const clickButton = (c, k, dataset = {}) => I.onInspectorClick(c, { target: { closest: () => ({ dataset: { insp: k, ...dataset } }) } });
/** Inspector HTML of the current selection (rendered into a stub element). */
const inspectorHtml = (c, inputs = {}) => {
  const el = { innerHTML: "", querySelectorAll: () => [], querySelector: (s) => inputs[/data-insp='([\w-]+)'/.exec(s)?.[1]] || null };
  c.inspectorEl = el;
  I.renderInspector(c);
  return el.innerHTML;
};
/** A Set that remembers every id ever queued (the auto-fit pass empties the live queue). */
class SpySet extends Set {
  constructor() { super(); this.seen = []; }
  add(v) { this.seen.push(v); return super.add(v); }
}

// ------------------------------------------------------------------ 1. multi-select Inspector controls

test("multi-select Auto-fit & Scale and Hide header: tri-state, all applicable nodes, one undo step", () => {
  const c = controller();
  const s = c.addNode({ type: "struct", data: { name: "page" }, autoFit: true });
  const src = c.addNode({ type: "source", data: { path: "a.c" } });
  const g = c.addNode({ type: "group" });
  c.selected = new Set([s.id, src.id, g.id]);
  c.commit({ save: false });

  let html = inspectorHtml(c);
  assert.match(html, /data-insp="multi-autofit" data-tri="mixed"/, "struct on, source off: mixed");
  assert.match(html, /Auto-fit &amp; Scale \(2\)/, "only the auto-fit types count (the group does not)");
  assert.match(html, /data-insp="multi-hide-header" data-tri="off"/);
  assert.doesNotMatch(html, /data-insp="multi-autofit" data-tri="\w+" checked/);

  c.fitQueue = new SpySet();
  const steps = c.history.undoList.length;
  clickCheckbox(c, "multi-autofit", true);
  assert.equal(s.autoFit, true);
  assert.equal(src.autoFit, true);
  assert.equal(g.autoFit, false, "a group never auto-fits");
  assert.deepEqual(c.fitQueue.seen.sort(), [s.id, src.id].sort(), "the enabled nodes are measured");
  assert.equal(c.history.undoList.length, steps + 1, "input + change: one undo step");
  assert.match(inspectorHtml(c), /data-insp="multi-autofit" data-tri="on" checked/);

  clickCheckbox(c, "multi-hide-header", true);
  assert.ok([s, src, g].every((n) => n.hideHeader));
  assert.equal(c.history.undoList.length, steps + 2);
  html = inspectorHtml(c);
  assert.match(html, /data-insp="multi-hide-header" data-tri="on" checked/);

  src.hideHeader = false;
  assert.match(inspectorHtml(c), /data-insp="multi-hide-header" data-tri="mixed"/);

  clickCheckbox(c, "multi-autofit", false);
  assert.equal(s.autoFit || src.autoFit, false);
  c.undo();
  assert.equal(c.byId(s.id).autoFit, true, "undo restores the whole selection at once");
  assert.equal(c.byId(src.id).autoFit, true);
});

test("indeterminate is set on mixed multi-select checkboxes after rendering", () => {
  const c = controller();
  const a = c.addNode({ type: "note", data: { text: "a" }, hideHeader: true });
  const b = c.addNode({ type: "note", data: { text: "b" } });
  c.selected = new Set([a.id, b.id]);
  const mixed = { indeterminate: false };
  c.inspectorEl = { innerHTML: "", querySelector: () => null, querySelectorAll: (s) => (s === 'input[data-tri="mixed"]' ? [mixed] : []) };
  I.renderInspector(c);
  assert.equal(mixed.indeterminate, true);
});

test("multi-select Revision: explicit Apply button, versioned types only, one undo step and Revert", () => {
  const c = controller();
  const s = c.addNode({ type: "struct", version: "v3.0", data: { name: "page" } });
  const d = c.addNode({ type: "diff", version: "v3.0", data: { mode: "file", versionA: "v2.6.39", versionB: "v3.0", path: "a.c" } });
  const cm = c.addNode({ type: "commit", version: "v3.0", data: { commitHash: SHA(1) } });
  const n = c.addNode({ type: "note", data: { text: "x" } });
  c.selected = new Set([s.id, d.id, cm.id, n.id]);
  c.commit({ save: false });
  const html = inspectorHtml(c);
  assert.match(html, /data-insp="multi-version-apply"/, "an explicit Apply button");
  assert.match(html, /Revision \(2 of 4\)/, "commit and note nodes have no revision");

  const steps = c.history.undoList.length;
  c.inspectorEl = { innerHTML: "", querySelectorAll: () => [], querySelector: (q) => (q.includes("multi-version") ? { value: "v6.1" } : null) };
  clickButton(c, "multi-version-apply");
  assert.equal(s.version, "v6.1");
  assert.equal(d.data.versionB, "v6.1");
  assert.equal(cm.version, "v3.0", "a commit hash pins the commit");
  assert.equal(c.history.undoList.length, steps + 1);
  assert.equal(c.selected.size, 4, "the selection stays");
  assert.ok(c.versionRevertMap, "Revert Versions undoes it");
  c.revertTargetVersion();
  assert.equal(s.version, "v3.0");
  assert.equal(d.data.versionB, "v3.0", "the moved payload field comes back too");
});

test("selection-scoped Version Time-Travel: Compare, Apply Revision, Revert, Clear", () => {
  const c = controller();
  const a = c.addNode({ type: "struct", version: "v3.0", data: { name: "a" } });
  const b = c.addNode({ type: "source", version: "v3.0", data: { path: "b.c" } });
  const out = c.addNode({ type: "struct", version: "v3.0", data: { name: "out" } });
  c.selected = new Set([a.id, b.id]);
  c.commit({ save: false });
  assert.match(inspectorHtml(c), /Version Time-Travel \(2 selected\)/);

  const target = { value: "v6.6" };
  c.inspectorEl = { innerHTML: "", querySelectorAll: () => [], querySelector: (q) => (q.includes("ver-target") ? target : null) };
  clickButton(c, "ver-compare");
  assert.equal(c.versionDiffTarget, "v6.6");
  assert.deepEqual([...c.versionDiffScope].sort(), [a.id, b.id].sort());
  assert.equal(c.versionDiffTargetOf(a), "v6.6");
  assert.equal(c.versionDiffTargetOf(out), "", "nodes outside the selection are not compared");
  assert.match(inspectorHtml(c, { "ver-target": target }), /comparing 2 nodes with v6\.6/);

  clickButton(c, "ver-apply");
  assert.equal(a.version, "v6.6");
  assert.equal(b.version, "v6.6");
  assert.equal(out.version, "v3.0", "unselected nodes keep their revision");
  assert.equal(c.selected.size, 2);
  clickButton(c, "ver-revert");
  assert.equal(a.version, "v3.0");
  assert.equal(b.version, "v3.0");
  clickButton(c, "ver-clear");
  assert.equal(c.versionDiffTarget, "");
  assert.equal(c.versionDiffScope, null);

  // Canvas-wide (nothing selected) Compare covers every node
  c.selected = new Set();
  clickButton(c, "ver-compare");
  assert.equal(c.versionDiffScope, null);
  assert.equal(c.versionDiffTargetOf(out), "v6.6");
});

// ------------------------------------------------------------------ 2. selection / group Auto-Layout

test("layoutScope: group members, selected nodes, else the whole canvas", () => {
  const nodes = [
    { id: "g", type: "group" }, { id: "inner", type: "group", groupId: "g" },
    { id: "m1", type: "note", groupId: "g" }, { id: "m2", type: "note", groupId: "g" },
    { id: "x", type: "note" }, { id: "y", type: "note", groupId: "inner" },
  ];
  const ids = (r) => r.nodes.map((n) => n.id);
  assert.deepEqual(ids(M.layoutScope(nodes, ["g"])), ["m1", "m2"], "direct members, nested groups left out");
  assert.equal(M.layoutScope(nodes, ["g"]).scope, "group");
  assert.deepEqual(ids(M.layoutScope(nodes, ["x", "m1", "g"])), ["m1", "x"]);
  assert.equal(M.layoutScope(nodes, ["x", "m1"]).scope, "selection");
  assert.equal(M.layoutScope(nodes, ["x"]).scope, "canvas", "one node other than a group: whole canvas");
  assert.equal(M.layoutScope(nodes, []).nodes.length, 4);
  const anchored = M.anchorPositions([{ id: "a", x: 80, y: 80 }, { id: "b", x: 480, y: 200 }], [{ id: "a" }, { id: "b" }], { x: 1000, y: -50 });
  assert.deepEqual(anchored, [{ id: "a", x: 1000, y: -50 }, { id: "b", x: 1400, y: 70 }]);
});

for (const mode of ["dag", "force"]) {
  test(`Auto-Layout (${mode}) of a selection arranges only it, anchored, one undo step, no fit`, () => {
    const c = controller();
    const a = c.addNode({ type: "note", x: 900, y: 700, data: { text: "a" } });
    const b = c.addNode({ type: "note", x: 400, y: 1300, data: { text: "b" } });
    const d = c.addNode({ type: "note", x: 1600, y: 500, data: { text: "c" } });
    const fixed = c.addNode({ type: "note", x: -500, y: -400, data: { text: "fixed" } });
    for (const [f, t] of [[a, b], [b, d], [fixed, a]]) c.canvas.edges.push(M.createEdge({ from: f.id, to: t.id }));
    const sub = [a, b, d];
    Object.assign(a, { x: 900, y: 700 });
    Object.assign(b, { x: 400, y: 1300 });
    Object.assign(d, { x: 1600, y: 500 });
    Object.assign(fixed, { x: -500, y: -400 });
    c.selected = new Set(sub.map((n) => n.id));
    c.commit({ save: false });
    const before = M.boundsOf(sub);
    const steps = c.history.undoList.length;
    let fitted = 0;
    c.fitView = () => { fitted += 1; };
    c.autoLayoutDAG(mode);
    const after = M.boundsOf(sub);
    assert.deepEqual([after.x, after.y], [before.x, before.y], "the subset keeps its top-left corner");
    assert.deepEqual([fixed.x, fixed.y], [-500, -400], "unselected nodes stay where they are");
    assert.equal(c.history.undoList.length, steps + 1);
    assert.equal(fitted, 0, "the view does not jump to the whole canvas");
    if (mode === "dag") assert.ok(a.x < b.x && b.x < d.x, "layered left to right along a → b → c");
  });

  test(`Auto-Layout (${mode}) of a selected group arranges its members inside it`, () => {
    const c = controller();
    const g = c.addNode({ type: "group", x: 1000, y: 1000, w: 300, h: 200 });
    const members = [0, 1, 2].map((i) => c.addNode({ type: "note", x: 1010 + i * 5, y: 1050, data: { text: `m${i}` } }));
    // addNode() places nodes at the view center: put them where the test wants them
    Object.assign(g, { x: 1000, y: 1000 });
    members.forEach((m, i) => Object.assign(m, { x: 1010 + i * 5, y: 1050, groupId: g.id }));
    const outsider = c.addNode({ type: "note", data: { text: "out" } });
    Object.assign(outsider, { x: 0, y: 0, groupId: null });
    c.canvas.edges.push(M.createEdge({ from: members[0].id, to: members[1].id }), M.createEdge({ from: members[1].id, to: members[2].id }));
    c.selected = new Set([g.id]);
    c.commit({ save: false });
    const steps = c.history.undoList.length;
    c.autoLayoutDAG(mode);
    const b = M.boundsOf(members);
    assert.equal(b.x, g.x + M.GROUP_LAYOUT_PAD.x);
    assert.equal(b.y, g.y + M.GROUP_LAYOUT_PAD.top);
    assert.ok(members.every((m) => M.rectContains(M.rectOf(g), M.rectOf(m))), "the group frame holds every member");
    assert.ok(members.every((m) => m.groupId === g.id));
    assert.deepEqual([g.x, g.y], [1000, 1000], "the group stays put, its size follows its members");
    assert.deepEqual([outsider.x, outsider.y], [0, 0]);
    assert.equal(c.history.undoList.length, steps + 1);
  });
}

test("Auto-Layout with nothing selected arranges the canvas and fits the view", () => {
  const c = controller();
  c.addNode({ type: "note", x: 500, y: 500, data: { text: "a" } });
  c.addNode({ type: "note", x: 900, y: 100, data: { text: "b" } });
  c.selected = new Set();
  let fitted = 0;
  c.fitView = () => { fitted += 1; };
  c.autoLayoutDAG("dag");
  assert.equal(fitted, 1);
  assert.equal(Math.min(...c.canvas.nodes.map((n) => n.x)), 80, "full-canvas layout starts at its origin");
});

// ------------------------------------------------------------------ 3. visibility layer checkboxes

test("layer checkbox: input + change set visibility once; hidden layer hides its nodes and their edges", () => {
  const c = controller();
  const layer = M.addLayer(c.canvas.settings, "L1");
  const a = c.addNode({ type: "note", data: { text: "a" } });
  const b = c.addNode({ type: "note", data: { text: "b" } });
  a.layerId = layer.id;
  const e = M.createEdge({ from: a.id, to: b.id });
  c.canvas.edges.push(e);
  c.commit({ save: false });
  const steps = c.history.undoList.length;
  clickCheckbox(c, "layer-toggle", false, { id: layer.id });
  assert.equal(layer.visible, false, "unchecking stays unchecked");
  assert.equal(M.isLayerVisible(c.canvas.settings, a.layerId), false);
  assert.equal(M.isEdgeVisible(c.canvas.settings, e, a, b), false, "an edge to a hidden node hides too");
  assert.equal(c.history.undoList.length, steps + 1);
  clickCheckbox(c, "layer-toggle", true, { id: layer.id });
  assert.equal(layer.visible, true);
  assert.equal(M.isEdgeVisible(c.canvas.settings, e, a, b), true);
  assert.equal(c.history.undoList.length, steps + 2);
  assert.equal(M.setLayerVisibility(c.canvas.settings, "nope", false), null);
  const sl = M.visibleCanvasSlice({ ...c.canvas, settings: { ...c.canvas.settings, layers: [{ ...layer, visible: false }] } });
  assert.deepEqual(sl.nodes.map((n) => n.id), [b.id]);
});

test("checkbox inputs apply on change only (no double toggle, no duplicate undo steps)", () => {
  const c = controller();
  const n = c.addNode({ type: "struct", data: { name: "s" } });
  c.selected = new Set([n.id]);
  c.commit({ save: false });
  const steps = c.history.undoList.length;
  I.onInspectorInput(c, checkbox("snap", true), false);
  assert.equal(c.canvas.settings.snap, false, "the input event alone changes nothing");
  I.onInspectorInput(c, checkbox("snap", true), true);
  assert.equal(c.canvas.settings.snap, true);
  for (const k of ["hide-header", "autofit", "d-disableKconfigEngine"]) clickCheckbox(c, k, true);
  clickCheckbox(c, "kconfig-collapse", false);
  assert.equal(n.hideHeader, true);
  assert.equal(n.autoFit, true);
  assert.equal(n.data.disableKconfigEngine, true);
  assert.equal(c.canvas.settings.kconfigCollapse, false);
  assert.equal(c.history.undoList.length, steps + 4, "one step per click (Snap is a view setting, never recorded)");
});

// ------------------------------------------------------------------ 4. crisp rendering

test("crisp rendering: 2D node transforms, device pixel snapping, no forced 3D layers", () => {
  const n = M.createNode("source", { x: 100, y: 50, data: { path: "a.c" } });
  assert.equal(nodeBox(n).transform, "translate(100px, 50px)");
  M.setContentScale(n, 1.5);
  assert.equal(nodeBox(n).transform, "translate(100px, 50px) scale(1.5)");
  const lines = Array.from({ length: 3000 }, (_, i) => `x${i}`);
  assert.doesNotMatch(renderVirtualSourceWindow(lines, 1, "a.c", null, computeVirtualRange(lines.length, 900, 300)), /translate3d|will-change/);
  assert.equal(M.snapToDevicePixel(10.3, 2), 10.5);
  assert.equal(M.snapToDevicePixel(10.3, 1), 10);
  assert.equal(M.snapToDevicePixel(-7.76, 1.5), -8);

  const c = controller();
  const classes = new Set();
  c.layerEl = { style: {} };
  c.wrapEl = { classList: { add: (k) => classes.add(k), remove: (k) => classes.delete(k), toggle() {} }, style: { setProperty() {} }, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) };
  const prevDpr = globalThis.window.devicePixelRatio;
  globalThis.window.devicePixelRatio = 2;
  c.canvas.viewport = { x: 10.3, y: -4.12, zoom: 1.7 };
  c.applyTransform();
  globalThis.window.devicePixelRatio = prevDpr;
  assert.equal(c.layerEl.style.transform, "translate(10.5px, -4px) scale(1.7)");
  assert.ok(classes.has("nm-moving"), "promoted to a compositor layer only while the view moves");
  const p = c.toCanvas(410.3, 300);
  assert.ok(Math.abs(p.x - 400 / 1.7) < 1e-9, "pointer mapping uses the exact viewport, not the snapped offset");
});

test("tab iframe supersampling: auto factor, in-place application, settings and Inspector", () => {
  assert.equal(M.iframeSupersample("auto", 1, 1), 1);
  assert.equal(M.iframeSupersample("auto", 0.5, 1), 1);
  assert.equal(M.iframeSupersample("auto", 1.3, 1), 1.5);
  assert.equal(M.iframeSupersample("auto", 1.5, 2), 2);
  assert.equal(M.iframeSupersample("1.5", 0.2, 1), 1.5);
  assert.equal(M.iframeSupersample("2", 3, 4), M.SUPERSAMPLE_MAX);
  assert.equal(M.normalizeSettings({}).renderQuality, "auto");
  assert.equal(M.normalizeSettings({ renderQuality: "2" }).renderQuality, "2");
  assert.equal(M.normalizeSettings({ renderQuality: "8" }).renderQuality, "auto");

  const cls = new Set();
  const props = new Map();
  const root = { style: { zoom: "" } };
  const frame = {
    dataset: {},
    classList: { toggle: (k, on) => (on ? cls.add(k) : cls.delete(k)) },
    style: { setProperty: (k, v) => props.set(k, v), removeProperty: (k) => props.delete(k) },
    contentDocument: { documentElement: root },
  };
  assert.equal(applyFrameSupersample(frame, 2), 2);
  assert.ok(cls.has("nm-ss"));
  assert.equal(props.get("--nm-ss"), "2");
  assert.equal(root.style.zoom, "2", "the embedded document lays out at 2× and is drawn at 1/2");
  root.style.zoom = "x";
  applyFrameSupersample(frame, 2);
  assert.equal(root.style.zoom, "x", "unchanged factor: nothing is touched");
  applyFrameSupersample(frame, 2, { force: true });
  assert.equal(root.style.zoom, "2", "a new document takes the factor again");
  applyFrameSupersample(frame, 1);
  assert.equal(cls.has("nm-ss"), false);
  assert.equal(props.has("--nm-ss"), false);
  assert.equal(root.style.zoom, "");

  const c = controller();
  let applied = 0;
  c.applyRenderQuality = () => { applied += 1; };
  I.onInspectorInput(c, { target: { closest: () => ({ dataset: { insp: "render-quality" }, value: "1.5", tagName: "SELECT" }) } }, true);
  assert.equal(c.canvas.settings.renderQuality, "1.5");
  assert.ok(applied >= 1);
  assert.match(inspectorHtml(c), /data-insp="render-quality"/);
});

// ------------------------------------------------------------------ 5. native commit node

const DETAIL = {
  commit_hash: SHA(7),
  subject: "mm: fix <b>races</b>",
  message: "mm: fix <b>races</b>\n\nLonger explanation.\n\nFixes: 0123456789ab (\"mm: old\")\nReviewed-by: R <r@x.org>\nSigned-off-by: A <a@x.org>",
  author: { name: "A", email: "a@x.org" },
  author_date: 1700000000,
  committer: { name: "C", email: "c@x.org" },
  committer_date: 1700000100,
  parents: [SHA(6).toUpperCase()],
  files: [{ change_type: "M", file: "mm/a.c" }, { change_type: "A", file: "mm/b.c" }, { change_type: "M", file: "mm/c.c" }],
  is_merge: false,
};

test("commit node: normalization, registries, title, model, escaped body, bridge and add-node form", async () => {
  for (const list of [M.NODE_TYPES, M.AUTOFIT_TYPES, M.TAB_OPENABLE_TYPES]) assert.ok(list.includes("commit"));
  assert.equal(M.NODE_BADGES.commit, "commit");
  assert.ok(M.SIZE_LIMITS.commit);
  const n = M.normalizeNode({ type: "commit", data: { commitHash: "ABCDEF1234", subject: "x: y", junk: 1 } });
  assert.deepEqual(n.data, { commitHash: "abcdef1234", subject: "x: y" });
  assert.equal(M.displayTitle(n), "abcdef1234 x: y");
  assert.equal(M.normalizeNode({ type: "commit", data: { commitHash: "zz" } }).data.commitHash, "");
  assert.equal(M.defaultTitle("commit", { commitHash: SHA(3), subject: "" }), `Commit ${SHA(3).slice(0, 12)}`);

  const m = D.commitModel(DETAIL, n.data);
  assert.equal(m.hash, SHA(7));
  assert.equal(m.body, "Longer explanation.");
  assert.deepEqual(m.trailers.map((t) => t.key), ["Fixes", "Reviewed-by", "Signed-off-by"]);
  assert.equal(m.trailers[0].sha, "0123456789ab");
  assert.deepEqual(m.parents, [SHA(6)]);
  assert.equal(m.committer.name, "C");
  assert.equal(m.fileSummary, "2 modified · 1 added");
  const html = D.renderCommitBody(m);
  assert.doesNotMatch(html, /<b>races/, "the subject is escaped");
  assert.match(html, /&lt;b&gt;races/);
  assert.match(html, /data-act="commit-spawn-diff"/);
  assert.match(html, new RegExp(`data-act="commit-spawn-parent" data-key="${SHA(6)}"`));
  assert.match(html, /data-act="commit-spawn-ref" data-key="0123456789ab:fixes"/);
  assert.equal((html.match(/data-act="commit-spawn-file"/g) || []).length, 3);
  assert.match(headMetaOf("commit", m).text, new RegExp(`^${SHA(7).slice(0, 12)} · 2023-11-14`));
  // Contributors stand in for trailers when the message has none
  assert.equal(D.commitModel({ ...DETAIL, message: "s", trailers: [{ role_name: "Signed-off-by", name: "Z", email: "z@x" }] }).trailers[0].value, "Z <z@x>");

  assert.deepEqual(tabForNode({ ...n, version: "v3.0" }), { type: "commits", title: "Commit abcdef1234", commitId: "abcdef1234", repo: "", version: "v3.0" });
  assert.equal(commitSpec({ commitHash: "nothex" }), null);
  const c = controller();
  assert.deepEqual(await specFromForm(c, "commit", { version: "v3.0", commitHash: " ABC1234 " }), { type: "commit", version: "v3.0", repo: "", data: { commitHash: "abc1234", subject: "" } });

  // Loading, model and SVG export
  c.api = { getCommitDetail: async (v, h) => ({ ...DETAIL, commit_hash: h }) };
  const node = c.addNode({ type: "commit", data: { commitHash: SHA(7) } });
  assert.equal((await c.loadCommit(node)).commit_hash, SHA(7));
  c.content.set(c.fetchKeyOf(node), { status: "ok", value: DETAIL });
  assert.equal(c.modelOf(node).subject, DETAIL.subject);
  assert.match(c.headTools(node, false), /data-act="commit-spawn-diff"/);
  const svg = canvasToSvg(c.canvas, (x) => x, { content: (x) => c.modelOf(x) });
  assert.match(svg, /mm: fix &lt;b&gt;races/);
  assert.match(svg, /Signed-off-by: A/);
});

function blameController(count) {
  const c = controller();
  const lines = Array.from({ length: count }, (_, i) => ({ commit_hash: SHA(i + 1), author_name: `dev${i}`, subject: `change ${i}`, line: i + 1, date: 1700000000 }));
  const blame = c.addNode({ type: "history", data: { mode: "blame", path: "kernel/fork.c" } });
  c.content.set(c.fetchKeyOf(blame), { status: "ok", value: { path: "kernel/fork.c", lines } });
  c.commit({ save: false });
  return { c, blame };
}

const clickAct = (c, node, act, key) => {
  const btn = { dataset: { act, key } };
  const nodeEl = { dataset: { nodeId: node.id } };
  G.onClick(c, { stopPropagation() {}, target: { closest: (s) => (s === "[data-act]" ? btn : s === ".nm-node" ? nodeEl : null) } });
};

test("Blame rows offer ⊕ commit and ⊕ diff; both spawn linked nodes from the row port and reuse them", () => {
  const { c, blame } = blameController(3);
  const html = D.renderHistoryBody(c.modelOf(blame));
  assert.equal((html.match(/data-act="hist-spawn-commit-detail"/g) || []).length, 3);
  assert.equal((html.match(/data-act="hist-spawn-commit"/g) || []).length, 3);
  assert.match(html, /data-act="blame-expand-commits"/);
  assert.match(html, /data-act="blame-expand-diffs"/);
  assert.match(c.headTools(blame, false), /blame-expand-commits/);

  clickAct(c, blame, "hist-spawn-commit-detail", SHA(2));
  const cn = c.canvas.nodes.find((n) => n.type === "commit");
  assert.equal(cn.data.commitHash, SHA(2));
  assert.equal(cn.data.subject, "change 1", "the row's subject titles the node until it loads");
  const edge = c.canvas.edges.find((e) => e.to === cn.id);
  assert.equal(edge.fromField, D.commitRowKey(SHA(2)));
  assert.deepEqual([...c.selected], [cn.id]);

  clickAct(c, blame, "hist-spawn-commit", SHA(2));
  const dn = c.canvas.nodes.find((n) => n.type === "diff");
  assert.deepEqual(dn.data, { mode: "commit", versionA: "", versionB: "", path: "", oldPath: "", commitHash: SHA(2) });
  assert.equal(c.canvas.edges.find((e) => e.to === dn.id).fromField, D.commitRowKey(SHA(2)));

  const counts = [c.canvas.nodes.length, c.canvas.edges.length];
  clickAct(c, blame, "hist-spawn-commit-detail", SHA(2));
  assert.deepEqual([c.canvas.nodes.length, c.canvas.edges.length], counts, "an existing commit node is reused, not duplicated");

  // Commit node actions: diff, parent, Fixes target, one file
  c.content.set(c.fetchKeyOf(cn), { status: "ok", value: { ...DETAIL, commit_hash: SHA(2) } });
  clickAct(c, cn, "commit-spawn-diff", SHA(2));
  assert.equal(c.canvas.nodes.filter((n) => n.type === "diff").length, 1, "the commit's diff node already exists: connected");
  assert.ok(c.canvas.edges.some((e) => e.from === cn.id && e.to === dn.id && e.label === "diff"));
  clickAct(c, cn, "commit-spawn-parent", SHA(6));
  assert.ok(c.canvas.nodes.some((n) => n.type === "commit" && n.data.commitHash === SHA(6)));
  clickAct(c, cn, "commit-spawn-ref", "0123456789ab:fixes");
  assert.ok(c.canvas.edges.some((e) => e.label === "fixes"));
  clickAct(c, cn, "commit-spawn-file", "mm/a.c");
  assert.ok(c.canvas.nodes.some((n) => n.type === "diff" && n.data.path === "mm/a.c" && n.data.commitHash === SHA(2)));
});

test("Blame bulk expand ≤ 20 new nodes: no confirmation, all wired from their rows, one undo step", async () => {
  const { c, blame } = blameController(5);
  let asked = 0;
  c.confirmFn = async () => { asked += 1; return true; };
  const steps = c.history.undoList.length;
  const created = await c.expandBlameToNodes(blame.id, "commit");
  assert.equal(asked, 0);
  assert.equal(created.length, 5);
  assert.ok(created.every((n) => n.type === "commit"));
  assert.equal(c.history.undoList.length, steps + 1);
  const fields = c.canvas.edges.filter((e) => e.from === blame.id).map((e) => e.fromField).sort();
  assert.deepEqual(fields, [1, 2, 3, 4, 5].map((i) => D.commitRowKey(SHA(i))).sort());
  assert.ok(created.every((n) => n.x > blame.x + blame.w), "arranged beside the Blame node");
  assert.equal(new Set(created.map((n) => `${n.x},${n.y}`)).size, 5, "no two nodes stacked on each other");

  const again = await c.expandBlameToNodes(blame.id, "commit");
  assert.equal(again.length, 0, "a second expand reuses every node");
  assert.equal(c.canvas.edges.filter((e) => e.from === blame.id).length, 5, "and adds no duplicate edge");
});

test("Blame bulk expand > 20 new nodes asks with the exact count; cancel leaves the canvas untouched", async () => {
  const { c, blame } = blameController(47);
  const asks = [];
  c.confirmFn = async (opts) => { asks.push(opts); return false; };
  const before = [c.canvas.nodes.length, c.canvas.edges.length, c.history.undoList.length];
  const created = await c.expandBlameToNodes(blame.id, "commit");
  assert.equal(created.length, 0);
  assert.equal(asks.length, 1);
  assert.equal(asks[0].summary, "Spawn 47 commit nodes from this Blame node?");
  assert.equal(asks[0].sections[0].rows.length, 47);
  assert.deepEqual([c.canvas.nodes.length, c.canvas.edges.length, c.history.undoList.length], before);
});

test("Blame bulk expand of diffs has no hard cap (all 120 commits) once confirmed", async () => {
  const { c, blame } = blameController(120);
  assert.equal(c.modelOf(blame).commits.length, 120, "the blame model keeps every commit");
  const asks = [];
  c.confirmFn = async (opts) => { asks.push(opts.summary); return true; };
  const steps = c.history.undoList.length;
  const created = await c.expandBlameToNodes(blame.id, "diff");
  assert.deepEqual(asks, ["Spawn 120 diff nodes from this Blame node?"]);
  assert.equal(created.length, 120);
  assert.ok(created.every((n) => n.type === "diff" && n.data.mode === "commit"));
  assert.equal(c.history.undoList.length, steps + 1);
});

test("nodes added while the confirmation is open are reused, not duplicated", async () => {
  const { c, blame } = blameController(30);
  c.confirmFn = async () => {
    c.canvas.nodes.push(M.createNode("commit", { data: { commitHash: SHA(1) } }));
    return true;
  };
  const created = await c.expandBlameToNodes(blame.id, "commit");
  assert.equal(created.length, 29);
  assert.equal(c.canvas.nodes.filter((n) => n.type === "commit" && n.data.commitHash === SHA(1)).length, 1);
  assert.equal(D.commitModel({ commit_hash: "<script>" }).hash, "", "a hash that is not hex is dropped");
});

test("an Auto-Layout result for nodes deleted meanwhile is dropped", async () => {
  const c = controller();
  const a = c.addNode({ type: "note", data: { text: "a" } });
  const b = c.addNode({ type: "note", data: { text: "b" } });
  Object.assign(a, { x: 300, y: 300 });
  let resolve;
  c.workerPool = { workerSupported: true, computeLayout: () => new Promise((r) => { resolve = r; }) };
  c.containerEl = { isConnected: true };
  c.selected = new Set([a.id, b.id]);
  const done = c.autoLayoutDAG("dag");
  c.canvas.nodes = c.canvas.nodes.filter((n) => n.id !== b.id);
  resolve({ positions: [{ id: a.id, x: 80, y: 80 }, { id: b.id, x: 500, y: 80 }] });
  await done;
  assert.deepEqual([a.x, a.y], [300, 300]);
});

test("the confirmation counts only new nodes: existing linked nodes are excluded", async () => {
  const { c, blame } = blameController(24);
  for (let i = 1; i <= 4; i++) c.canvas.nodes.push(M.createNode("commit", { repo: c.repo, data: { commitHash: SHA(i) } }));
  let asked = 0;
  c.confirmFn = async () => { asked += 1; return true; };
  const created = await c.expandBlameToNodes(blame.id, "commit");
  assert.equal(asked, 0, "24 commits, 4 already on the canvas: 20 new nodes spawn at once");
  assert.equal(created.length, 20);
  assert.equal(c.canvas.edges.filter((e) => e.from === blame.id).length, 24, "the existing nodes are connected too");

  const { c: c2, blame: b2 } = blameController(24);
  for (let i = 1; i <= 3; i++) c2.canvas.nodes.push(M.createNode("commit", { data: { commitHash: SHA(i) } }));
  const asks = [];
  c2.confirmFn = async (o) => { asks.push(o.summary); return true; };
  await c2.expandBlameToNodes(b2.id, "commit");
  assert.deepEqual(asks, ["Spawn 21 commit nodes from this Blame node?"]);
});

test("Inspector of a Blame node and of a commit node", () => {
  const { c, blame } = blameController(3);
  c.selected = new Set([blame.id]);
  const html = inspectorHtml(c);
  assert.match(html, /data-insp="blame-expand-commits"[^>]*>⇶ Expand Commits to Nodes \(3\)/);
  assert.match(html, /data-insp="blame-expand-diffs"[^>]*>⇶ Expand Diffs to Nodes \(3\)/);
  let called = null;
  c.expandBlameToNodes = (id, kind) => { called = [id, kind]; };
  clickButton(c, "blame-expand-diffs");
  assert.deepEqual(called, [blame.id, "diff"]);

  const cn = c.addNode({ type: "commit", data: { commitHash: SHA(9) } });
  c.selected = new Set([cn.id]);
  const ch = inspectorHtml(c);
  assert.match(ch, /data-insp="d-commitHash"/);
  assert.match(ch, /data-insp="commit-spawn-diff"/);
  assert.match(ch, /data-insp="open-tab"/, "a commit opens as a Commits tab");
  clickButton(c, "commit-spawn-diff");
  assert.ok(c.canvas.nodes.some((n) => n.type === "diff" && n.data.commitHash === SHA(9)));
});
