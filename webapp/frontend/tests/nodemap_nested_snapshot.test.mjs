/**
 * nodemap_nested_snapshot.test.mjs - High quality preview of nested NodeMap nodes: the opt-in
 * `data.highQualityPreview` flag (model, Inspector, undo, storage, JSON round trip), the snapshot cache
 * (`nested_snapshot.js`: keys, reuse, coalescing, invalidation, eviction, failures), the bounded recursion of
 * a snapshot render, the live card picture and the SVG / PNG export embedding (scoped ids, self-contained).
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import "./helpers/unref_timers.mjs";
import * as M from "../js/views/nodemap/canvas_model.js";
import * as N from "../js/views/nodemap/nested_nodes.js";
import * as S from "../js/views/nodemap/nested_store.js";
import * as Q from "../js/views/nodemap/nested_snapshot.js";
import * as Nested from "../js/views/nodemap/nodemap_nested.js";
import { canvasToSvg } from "../js/views/nodemap/nodemap_export.js";
import { NodeMapController } from "../js/views/nodemap/nodemap_controller.js";
import { renderInspector } from "../js/views/nodemap/nodemap_inspector.js";
import { exportPNG, exportSVG } from "../js/views/nodemap/nodemap_persistence.js";

const mkStorage = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), key: (i) => [...m.keys()][i] ?? null, get length() { return m.size; }, _m: m };
};
const useStore = () => {
  const ls = mkStorage();
  Object.defineProperty(globalThis, "localStorage", { value: ls, configurable: true, writable: true });
  return { userDb: null, storage: ls };
};
const note = (id, x = 0, y = 0, text = "t") => M.createNode("note", { id, x, y, data: { text } });
const doc = (nodes, edges = []) => M.normalizeCanvas({ ...M.emptyCanvas(), nodes, edges });
const ref = (id, canvasId, extra = {}) => M.createNode("nodemap", { id, title: id, x: 10, y: 10, w: 420, h: 320, data: { canvasId, ...extra } });
const idsOf = (svg) => [...String(svg).matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
const fakeUrls = () => {
  const made = [];
  const revoked = [];
  return { made, revoked, create: (svg) => (made.push(svg), `blob:snap-${made.length}`), revoke: (u) => revoked.push(u) };
};
const instant = () => Promise.resolve();
const tick = async () => {
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
};

/** Fake controller for the glue flows (local storage, counting renderer, injectable theme). */
const mkCtrl = (canvas, over = {}) => {
  const ctrl = {
    userDb: null, rootCanvas: canvas, canvas, subMapPath: [], selected: new Set(), _disposeGen: 0, commits: 0,
    theme: "dark", renders: 0,
    themeKeyFn() { return this.theme; },
    snapshotRenderFn(c, d) { c.renders += 1; return canvasToSvg(d, (x) => x, { idPrefix: Q.SNAPSHOT_ID_TOKEN, nested: (n) => S.cardOf(n, "ok") }); },
    snapshotCacheOpts: { urls: fakeUrls(), defer: instant },
    commit() { this.commits += 1; },
    ...over,
  };
  Nested.initNested(ctrl);
  return ctrl;
};

// ---------------------------------------------------------------- node data contract

test("highQualityPreview defaults off, is kept only when true and survives both node forms", () => {
  const plain = M.createNode("nodemap", { id: "a", data: { canvasId: "kid" } });
  assert.equal("highQualityPreview" in plain.data, false, "opt-in: absent by default");
  assert.equal(N.wantsHighQuality(plain), false);
  assert.deepEqual(M.normalizeData("nodemap", { canvasId: "kid", highQualityPreview: true }), { canvasId: "kid", highQualityPreview: true });
  for (const v of [false, "true", 1, null]) assert.deepEqual(M.normalizeData("nodemap", { canvasId: "kid", highQualityPreview: v }), { canvasId: "kid" });
  const inline = M.normalizeData("nodemap", { canvas: doc([note("x")]), highQualityPreview: true });
  assert.equal(inline.highQualityPreview, true);
  assert.equal(inline.canvas.nodes.length, 1);
  assert.deepEqual(M.normalizeData("nodemap", { highQualityPreview: true, preview: { w: 1, h: 1, nodes: [], edges: [] }, png: "data:image/png;base64,AA" }), { canvasId: "", highQualityPreview: true }, "no picture payload is ever kept");
});

test("the flag survives local save / load, JSON export (no picture data) and import", async () => {
  const store = useStore();
  const child = doc([note("c1"), note("c2", 300, 0)]);
  assert.equal(await S.saveChild(store, "kid", child, { name: "Kid" }), true);
  const parent = doc([ref("nb", "kid", { highQualityPreview: true }), ref("np", "kid")]);
  // local storage round trip
  M.saveCanvas(store.storage, "kip-test-canvas", parent);
  const loaded = M.loadCanvas(store.storage, "kip-test-canvas");
  assert.equal(loaded.nodes.find((n) => n.id === "nb").data.highQualityPreview, true);
  assert.equal("highQualityPreview" in loaded.nodes.find((n) => n.id === "np").data, false);
  // a picture rendered for it stays in memory only
  const ctrl = mkCtrl(parent);
  await Nested.refreshSnapshots(ctrl);
  assert.ok(Nested.nestedSnapshot(ctrl, parent.nodes[0]), "picture cached");
  const { canvas: out, warnings } = await S.inlineDocument(store, parent);
  assert.deepEqual(warnings, []);
  const json = JSON.stringify(out);
  assert.equal(out.nodes[0].data.highQualityPreview, true);
  assert.equal(out.nodes[0].data.canvas.nodes.length, 2);
  assert.equal("highQualityPreview" in out.nodes[1].data, false);
  assert.doesNotMatch(json, /data:image|blob:|<svg|nmsnap/, "no snapshot payload in the JSON export");
  const back = await S.importDocument(store, JSON.parse(json));
  assert.equal(back.nodes[0].data.highQualityPreview, true);
  assert.match(back.nodes[0].data.canvasId, /^nm-/);
  assert.equal("highQualityPreview" in back.nodes[1].data, false);
});

test("Inspector toggle: unchecked by default, sets data.highQualityPreview, undo restores, disabled without target", () => {
  useStore();
  const c = new NodeMapController();
  c.tab = { id: "t", type: "nodemap", version: "v3.0", repo: "" };
  c.renderer = { sync() {}, invalidate() {}, element: () => null, clear() {} };
  c.renderWires = () => {};
  c.canvas = doc([ref("nb", "kid"), M.createNode("nodemap", { id: "nu", data: {} })]);
  c.rootCanvas = c.canvas;
  c.history.reset(M.structuralSnapshot(c.canvas));
  c.inspectorEl = { innerHTML: "", querySelector: () => null, querySelectorAll: () => [] };
  c.selected = new Set(["nb"]);
  renderInspector(c);
  const box = /<input type="checkbox" data-insp="d-highQualityPreview"[^>]*>/.exec(c.inspectorEl.innerHTML)?.[0] || "";
  assert.ok(box, "the toggle is shown for a nodemap node");
  assert.doesNotMatch(box, /checked|disabled/);
  assert.match(c.inspectorEl.innerHTML, /High quality preview/);
  c.onInspectorInput({ target: { closest: () => ({ dataset: { insp: "d-highQualityPreview" }, type: "checkbox", checked: true, tagName: "INPUT" }) } }, true);
  assert.equal(c.byId("nb").data.highQualityPreview, true);
  assert.equal(c.byId("nb").data.canvasId, "kid", "the reference is untouched");
  c.renderInspector = (() => renderInspector(c));
  renderInspector(c);
  assert.match(/<input type="checkbox" data-insp="d-highQualityPreview"[^>]*>/.exec(c.inspectorEl.innerHTML)[0], /checked/);
  assert.equal(c.undo(), true);
  assert.equal("highQualityPreview" in c.byId("nb").data, false, "undo turns it back off");
  c.redo();
  assert.equal(c.byId("nb").data.highQualityPreview, true, "redo turns it on again");
  // unset and broken targets: shown disabled
  c.selected = new Set(["nu"]);
  renderInspector(c);
  assert.match(/<input type="checkbox" data-insp="d-highQualityPreview"[^>]*>/.exec(c.inspectorEl.innerHTML)[0], /disabled/);
  c.nestedStatus.kid = "missing";
  c.selected = new Set(["nb"]);
  renderInspector(c);
  assert.match(/<input type="checkbox" data-insp="d-highQualityPreview"[^>]*>/.exec(c.inspectorEl.innerHTML)[0], /disabled/);
  c.dispose();
});

// ---------------------------------------------------------------- snapshot cache

test("cache: one render per (child, content, theme), cached repeats, coalesced concurrent requests", async () => {
  let renders = 0;
  const urls = fakeUrls();
  const cache = new Q.NestedSnapshotCache({ render: (d) => (renders++, canvasToSvg(d, (x) => x, { idPrefix: Q.SNAPSHOT_ID_TOKEN })), urls, defer: instant });
  const child = doc([note("c1"), note("c2", 400, 200)]);
  const [a, b, c] = await Promise.all([1, 2, 3].map(() => cache.request({ canvasId: "kid", canvas: child, theme: "dark" })));
  assert.equal(renders, 1, "concurrent requests share one render");
  assert.ok(a === b && b === c);
  assert.equal(a.key, Q.snapshotKey({ canvasId: "kid", hash: Q.contentHash(child), theme: "dark", variant: Q.SNAPSHOT_VARIANT }));
  assert.equal(await cache.request({ canvasId: "kid", canvas: child, theme: "dark" }), a, "repeat request is a cache hit");
  assert.equal(renders, 1);
  assert.equal(cache.latest("kid", "dark"), a);
  // the viewport of the child is not content
  child.viewport = { x: 999, y: 5, zoom: 2 };
  assert.equal(await cache.request({ canvasId: "kid", canvas: child, theme: "dark" }), a);
  assert.equal(renders, 1);
  // theme switch: a new picture, the old theme's one stays cached for switching back
  const light = await cache.request({ canvasId: "kid", canvas: child, theme: "light" });
  assert.equal(renders, 2);
  assert.notEqual(light.key, a.key);
  assert.equal(await cache.request({ canvasId: "kid", canvas: child, theme: "dark" }), a);
  assert.equal(renders, 2);
  // content edit: re-rendered, the stale picture released at once
  child.nodes.push(note("c3", 0, 400));
  const edited = await cache.request({ canvasId: "kid", canvas: child, theme: "dark" });
  assert.equal(renders, 3);
  assert.notEqual(edited.hash, a.hash);
  assert.equal(cache.peek(a.key), null);
  assert.ok(urls.revoked.includes(a.url), "stale object URL revoked");
  assert.equal(cache.latest("kid", "dark"), edited);
  // explicit invalidation (re-pointed / re-imported child)
  cache.invalidate("kid");
  assert.equal(cache.latest("kid", "dark"), null);
  assert.equal(cache.size, 0);
  assert.ok(urls.revoked.includes(edited.url) && urls.revoked.includes(light.url));
  await cache.request({ canvasId: "kid", canvas: child, theme: "dark" });
  assert.equal(renders, 4);
  cache.clear({ dispose: true });
  assert.equal(await cache.request({ canvasId: "kid", canvas: child, theme: "dark" }), null, "a disposed cache renders nothing");
  assert.equal(urls.made.length, urls.revoked.length, "every object URL released");
});

test("cache: bounded (LRU eviction revokes), empty children need no render, failures fall back and are not retried", async () => {
  let renders = 0;
  const urls = fakeUrls();
  const cache = new Q.NestedSnapshotCache({ render: (d) => (renders++, canvasToSvg(d, (x) => x)), urls, defer: instant, maxEntries: 2 });
  const kids = ["k1", "k2", "k3"].map((id, i) => [id, doc([note(`${id}n`, i * 10, 0)])]);
  for (const [id, cv] of kids) await cache.request({ canvasId: id, canvas: cv, theme: "t" });
  assert.equal(cache.size, 2);
  assert.equal(cache.latest("k1", "t"), null, "least recently used evicted");
  assert.deepEqual(urls.revoked, ["blob:snap-1"]);
  // empty child
  const empty = await cache.request({ canvasId: "e", canvas: doc([]), theme: "t" });
  assert.equal(empty.empty, true);
  assert.equal(empty.url, "");
  assert.equal(renders, 3);
  assert.equal(N.nestedBodyHtml(ref("x", "e", { highQualityPreview: true }), { status: "ok", name: "E", counts: {}, preview: null }, empty).includes("Empty NodeMap"), true);
  // failing renderer
  let fails = 0;
  const bad = new Q.NestedSnapshotCache({ render: () => { fails++; throw new Error("boom"); }, urls, defer: instant });
  assert.equal(await bad.request({ canvasId: "k", canvas: kids[0][1], theme: "t" }), null);
  assert.equal(await bad.request({ canvasId: "k", canvas: kids[0][1], theme: "t" }), null);
  assert.equal(fails, 1, "a failed key is not retried until invalidated");
  bad.invalidate("k");
  await bad.request({ canvasId: "k", canvas: kids[0][1], theme: "t" });
  assert.equal(fails, 2);
  const junk = new Q.NestedSnapshotCache({ render: () => "<div>not svg</div>", urls, defer: instant });
  assert.equal(await junk.request({ canvasId: "k", canvas: kids[0][1], theme: "t" }), null, "a non-SVG result is a failure");
});

test("cache: a request superseded by a newer revision never becomes the shown picture", async () => {
  const urls = fakeUrls();
  const gates = [];
  const cache = new Q.NestedSnapshotCache({ render: (d) => canvasToSvg(d, (x) => x), urls, defer: () => new Promise((r) => gates.push(r)) });
  const v1 = doc([note("a")]);
  const v2 = doc([note("a"), note("b", 300, 0)]);
  const p1 = cache.request({ canvasId: "kid", canvas: v1, theme: "t" });
  const p2 = cache.request({ canvasId: "kid", canvas: v2, theme: "t" });
  assert.equal(cache.pending("kid", "t"), true);
  gates[1]();
  const e2 = await p2;
  gates[0]();
  const e1 = await p1;
  assert.equal(cache.latest("kid", "t"), e2);
  assert.equal(e1.url, "", "the superseded render is handed back without a live URL");
  assert.equal(cache.size, 1);
  assert.equal(urls.made.length - urls.revoked.length, 1);
});

// ---------------------------------------------------------------- rendering

test("snapshot render is bounded: a nested node inside the pictured child stays a schematic card", async () => {
  const store = useStore();
  const grand = doc([note("g1", 0, 0, "grand text")]);
  await S.saveChild(store, "kidc", grand, { name: "C" });
  const childB = doc([note("b1", 0, 0, "B note"), ref("nc", "kidc", { highQualityPreview: true, summary: { title: "C map", nodes: 1, edges: 0, nested: 0, depth: 0 } })]);
  await S.saveChild(store, "kidb", childB, { name: "B" });
  const parent = doc([ref("nb", "kidb", { highQualityPreview: true })]);
  const ctrl = mkCtrl(parent, { snapshotRenderFn: undefined, snapshotCacheOpts: { urls: fakeUrls(), defer: instant } });
  delete ctrl.snapshotRenderFn;
  let calls = 0;
  const real = Nested.renderChildSnapshot;
  ctrl.snapshotRenderFn = (c, d) => (calls++, real(c, d));
  await Nested.refreshSnapshots(ctrl);
  assert.equal(calls, 1, "only B is rendered: C is never rasterized");
  const snap = Nested.nestedSnapshot(ctrl, parent.nodes[0]);
  assert.match(snap.svg, /^<svg /);
  assert.equal((snap.svg.match(/<svg\b/g) || []).length, 1, "no picture inside the picture");
  assert.match(snap.svg, /C map/, "C drawn as its titled schematic card");
  assert.match(snap.svg, /1 node · 0 edges · 0 sub-maps/);
  assert.doesNotMatch(snap.svg, /grand text/);
  assert.ok(idsOf(snap.svg).every((id) => id.startsWith(Q.SNAPSHOT_ID_TOKEN)), "every id carries the embedding token");
  Nested.disposeSnapshots(ctrl);
});

test("fit and aspect: the embedded picture keeps its viewBox and meets the card box", () => {
  const wide = { svg: '<svg xmlns="http://www.w3.org/2000/svg" width="4000" height="40" viewBox="-20 -20 4000 40"><rect id="nmsnapq7-nmclip0"/><g clip-path="url(#nmsnapq7-nmclip0)"/></svg>' };
  const entry = { ...wide, ...Q.parseSnapshotSvg(wide.svg), empty: false };
  assert.deepEqual(entry.viewBox, { x: -20, y: -20, w: 4000, h: 40 });
  const out = Q.embedSnapshotSvg(entry, { x: 8, y: 30, w: 300, h: 200 }, "nq0-");
  assert.match(out, /^<svg x="8" y="30" width="300" height="200" viewBox="-20 -20 4000 40" preserveAspectRatio="xMidYMid meet" overflow="hidden">/);
  assert.match(out, /id="nq0-nmclip0"/);
  assert.match(out, /url\(#nq0-nmclip0\)/);
  assert.doesNotMatch(out, /nmsnapq7/);
  assert.equal(Q.embedSnapshotSvg(entry, { x: 0, y: 0, w: 0, h: 10 }, "p-"), "");
  assert.equal(Q.embedSnapshotSvg({ empty: true }, { x: 0, y: 0, w: 10, h: 10 }, "p-"), "");
  assert.equal(Q.parseSnapshotSvg("<svg></svg>"), null);
});

test("live card: schematic by default, pending while rendering, then a static picture keeping header, counts and Open", async () => {
  const store = useStore();
  await S.saveChild(store, "kid", doc([note("c1"), note("c2", 300, 0)]), { name: "Kid" });
  const off = ref("off", "kid", { summary: { title: "Kid", nodes: 2, edges: 0, nested: 0, depth: 0 }, preview: S.buildStaticPreview(doc([note("c1")])) });
  const on = ref("on", "kid", { highQualityPreview: true, summary: { title: "Kid", nodes: 2, edges: 0, nested: 0, depth: 0 }, preview: S.buildStaticPreview(doc([note("c1")])) });
  const parent = doc([off, on]);
  const ctrl = mkCtrl(parent);
  const html = (n) => N.nestedBodyHtml(n, Nested.nestedCard(ctrl, n), Nested.nestedSnapshot(ctrl, n));
  assert.doesNotMatch(html(off), /nm-nested-img|nm-nested-pending/);
  assert.match(html(off), /svg class="nm-nested-svg"/);
  assert.match(html(on), /nm-nested-pending/, "schematic shown immediately while the picture renders");
  const keyBefore = Nested.nestedBodyKey(ctrl, on);
  assert.equal(Nested.scheduleSnapshot(ctrl, on), true);
  assert.equal(Nested.scheduleSnapshot(ctrl, on), false, "one request per child and theme");
  assert.equal(Nested.scheduleSnapshot(ctrl, off), false, "never for a node that did not opt in");
  await tick();
  assert.equal(ctrl.renders, 1);
  assert.ok(ctrl.commits >= 1, "the canvas re-renders once the picture is ready");
  assert.notEqual(Nested.nestedBodyKey(ctrl, on), keyBefore, "body key follows the picture");
  const pic = html(on);
  assert.match(pic, /<img class="nm-nested-img" src="blob:snap-1" alt="" draggable="false"/);
  assert.match(pic, /nm-nested-title">Kid</);
  assert.match(pic, /2 nodes · 0 edges · 0 sub-maps/);
  assert.match(pic, /data-act="nested-open"/);
  assert.doesNotMatch(pic, /nm-nested-svg|contenteditable|<textarea|<iframe/);
  assert.doesNotMatch(html(off), /<img/, "the other node keeps its schematic");
  // broken target: the broken view, no picture
  ctrl.nestedStatus.kid = "missing";
  assert.match(html(on), /nm-nested-broken/);
  assert.equal(Nested.nestedSnapshot(ctrl, on), null);
  // a URL that is not ours is never rendered
  delete ctrl.nestedStatus.kid;
  assert.doesNotMatch(N.nestedBodyHtml(on, Nested.nestedCard(ctrl, on), { url: "javascript:alert(1)", empty: false }), /<img/);
  Nested.disposeSnapshots(ctrl);
});

test("invalidation: child edit (drill down and back), theme change and a new target regenerate the picture", async () => {
  const store = useStore();
  await S.saveChild(store, "kid", doc([note("c1")]), { name: "Kid" });
  await S.saveChild(store, "kid2", doc([note("d1"), note("d2", 200, 0)]), { name: "Other" });
  const node = ref("nb", "kid", { highQualityPreview: true });
  const ctrl = mkCtrl(doc([node]));
  await Nested.probeStatuses(ctrl);
  const first = Nested.nestedSnapshot(ctrl, ctrl.canvas.nodes[0]);
  assert.ok(first);
  assert.equal(ctrl.renders, 1);
  await Nested.probeStatuses(ctrl);
  assert.equal(ctrl.renders, 1, "unchanged child: cached picture reused");
  // the child is edited (as saved by a drill-down level) and the parent level is probed again
  const kid = await S.loadChild(store, "kid");
  kid.canvas.nodes.push(note("c9", 500, 300));
  await S.saveChild(store, "kid", kid.canvas, { name: "Kid" });
  await Nested.probeStatuses(ctrl);
  const second = Nested.nestedSnapshot(ctrl, ctrl.canvas.nodes[0]);
  assert.equal(ctrl.renders, 2);
  assert.notEqual(second.hash, first.hash);
  assert.ok(ctrl.snapshotCacheOpts.urls.revoked.includes(first.url));
  // theme change: new key, card re-requests
  ctrl.theme = "light";
  const commits = ctrl.commits;
  Nested.onNestedThemeChanged(ctrl);
  assert.equal(ctrl.commits, commits + 1, "high-quality cards re-render on a theme change");
  assert.equal(Nested.nestedSnapshot(ctrl, ctrl.canvas.nodes[0]), null);
  assert.equal(Nested.scheduleSnapshot(ctrl, ctrl.canvas.nodes[0]), true);
  await tick();
  assert.equal(ctrl.renders, 3);
  assert.equal(Nested.nestedSnapshot(ctrl, ctrl.canvas.nodes[0]).theme, "light");
  // re-pointed to another child: flag kept, picture of the new child
  ctrl.byId = (id) => ctrl.canvas.nodes.find((n) => n.id === id);
  ctrl.nestedPickerFn = async () => ({ kind: "local", id: "kid2" });
  assert.equal(await Nested.chooseNestedTarget(ctrl, "nb"), true);
  const moved = ctrl.canvas.nodes[0];
  assert.equal(moved.data.canvasId, "kid2");
  assert.equal(moved.data.highQualityPreview, true);
  assert.equal(Nested.nestedSnapshot(ctrl, moved), null);
  Nested.scheduleSnapshot(ctrl, moved);
  await tick();
  assert.equal(Nested.nestedSnapshot(ctrl, moved).canvasId, "kid2");
  Nested.disposeSnapshots(ctrl);
  assert.equal(ctrl.nestedSnapshots, null);
});

// ---------------------------------------------------------------- exports

test("SVG export: high-quality nodes embed the snapshot with scoped, unique ids; others stay schematic", async () => {
  const store = useStore();
  await S.saveChild(store, "kid", doc([note("c1", 0, 0, "inside A"), note("c2", 300, 0)]), { name: "Kid" });
  const parent = doc([ref("h1", "kid", { highQualityPreview: true }), { ...ref("h2", "kid", { highQualityPreview: true }), x: 600 }, { ...ref("p", "kid"), x: 1200 }]);
  const ctrl = mkCtrl(parent);
  await Nested.prepareSnapshots(ctrl);
  assert.equal(ctrl.renders, 1, "one render for two nodes of the same child");
  const opts = { nested: (n) => Nested.nestedCard(ctrl, n), nestedSnapshot: (n) => Nested.nestedSnapshot(ctrl, n) };
  const svg = canvasToSvg(parent, (x) => x, opts);
  assert.equal((svg.match(/<svg\b/g) || []).length, 3, "the document plus one picture per high-quality node");
  assert.match(svg, /<svg x="8" y="[\d.]+" width="404" /);
  assert.equal((svg.match(/inside A/g) || []).length, 2);
  const ids = idsOf(svg);
  assert.equal(new Set(ids).size, ids.length, "no id collides with the parent or the other embedding");
  assert.ok(ids.some((id) => id.startsWith("nq0-")) && ids.some((id) => id.startsWith("nq1-")));
  for (const [, target] of svg.matchAll(/url\(#([^)]+)\)/g)) assert.ok(ids.includes(target), `reference #${target} resolves`);
  assert.doesNotMatch(svg, /nmsnapq7|blob:|<image|<foreignObject/, "self-contained vector output");
  // without the option: unchanged schematic card
  const plain = canvasToSvg(parent, (x) => x, { nested: opts.nested });
  assert.equal((plain.match(/<svg\b/g) || []).length, 1);
  // a parent itself rendered for embedding prefixes its own ids too
  const scoped = canvasToSvg(parent, (x) => x, { ...opts, idPrefix: "outer-" });
  assert.ok(idsOf(scoped).every((id) => id.startsWith("outer-")));
  Nested.disposeSnapshots(ctrl);
});

test("exportSVG / exportPNG prepare the pictures once and embed them; no high-quality node keeps the sync path", async () => {
  useStore();
  await S.saveChild({ userDb: null, storage: globalThis.localStorage }, "kid", doc([note("c1", 0, 0, "picture text")]), { name: "Kid" });
  const c = new NodeMapController();
  c.tab = { id: "t", type: "nodemap", version: "v3.0", repo: "" };
  c.renderer = { sync() {}, invalidate() {}, element: () => null, clear() {} };
  c.renderInspector = () => {};
  c.renderWires = () => {};
  c.canvas = doc([ref("h", "kid", { highQualityPreview: true })]);
  c.rootCanvas = c.canvas;
  let renders = 0;
  c.snapshotRenderFn = (ctrl, d) => (renders++, Nested.renderChildSnapshot(ctrl, d));
  c.snapshotCacheOpts = { urls: fakeUrls(), defer: instant };
  const downloads = [];
  c.download = (name, blob) => downloads.push(name);
  const out = exportSVG(c);
  assert.equal(typeof out.then, "function", "async when pictures may need rendering");
  const svg = await out;
  assert.equal(renders, 1);
  assert.match(svg, /picture text/);
  assert.match(svg, /<svg x="8"/);
  assert.deepEqual(downloads, ["nodemap_canvas.svg"]);
  // PNG: the cached picture is reused (vector, rasterized at the output size)
  const svgs = [];
  const prevCanvasSvg = c.canvasSvg.bind(c);
  c.canvasSvg = (o) => {
    const s = prevCanvasSvg(o);
    svgs.push(s);
    return s;
  };
  const prev = { Image: globalThis.Image, create: globalThis.document.createElement, URL: globalThis.URL };
  globalThis.Image = class { set src(v) { queueMicrotask(() => this.onload?.()); } };
  globalThis.document.createElement = (tag) => (tag === "canvas" ? { width: 0, height: 0, getContext: () => ({ drawImage() {} }), toBlob: (cb) => queueMicrotask(() => cb({ size: 1 })) } : prev.create.call(globalThis.document, tag));
  globalThis.URL = Object.assign(Object.create(prev.URL), { createObjectURL: () => "blob:x", revokeObjectURL() {} });
  try {
    await exportPNG(c, { scale: 2 });
  } finally {
    Object.assign(globalThis, { Image: prev.Image, URL: prev.URL });
    globalThis.document.createElement = prev.create;
  }
  assert.equal(renders, 1, "PNG export reuses the cached snapshot");
  assert.ok(svgs.length >= 2 && svgs.every((s) => /picture text/.test(s)));
  // a canvas without high-quality nodes keeps the synchronous SVG export
  c.canvas = doc([ref("p", "kid")]);
  c.rootCanvas = c.canvas;
  c.canvasSvg = prevCanvasSvg;
  assert.equal(c.hasHighQualityNested(), false);
  assert.equal(typeof exportSVG(c), "string");
  c.dispose();
  assert.equal(c.nestedSnapshots, null, "dispose releases the cache");
});

// ---------------------------------------------------------------- review edge cases

test("eviction under budget pressure: unshown pictures go first, a shown one re-renders its card once and is not re-requested", async () => {
  const urls = fakeUrls();
  const evicted = [];
  const cache = new Q.NestedSnapshotCache({ render: (d) => canvasToSvg(d, (x) => x), urls, defer: instant, maxEntries: 2, onEvict: (e) => evicted.push(e.canvasId) });
  const a = doc([note("a")]);
  await cache.request({ canvasId: "a", canvas: a, theme: "dark" });
  await cache.request({ canvasId: "a", canvas: a, theme: "light" }); // shown in light
  await cache.request({ canvasId: "a", canvas: a, theme: "dark" }); // dark shown again, both are shown
  await cache.request({ canvasId: "b", canvas: doc([note("b")]), theme: "dark" });
  assert.equal(cache.size, 2);
  assert.deepEqual(evicted, ["a"], "the shown light picture of a (least recently used) was evicted and reported");
  assert.equal(cache.settled("a", "light"), true);
  assert.equal(cache.latest("a", "dark") !== null && cache.latest("b", "dark") !== null, true);
  // the glue never re-requests a settled slot in the background (no eviction ping-pong)
  const store = useStore();
  await S.saveChild(store, "kid", doc([note("k")]), { name: "Kid" });
  const node = ref("n", "kid", { highQualityPreview: true });
  const ctrl = mkCtrl(doc([node]), { snapshotRenderFn: () => { throw new Error("render failed"); } });
  await Nested.refreshSnapshots(ctrl);
  assert.equal(Nested.nestedSnapshot(ctrl, node), null, "failure falls back to the schematic");
  assert.equal(ctrl.nestedSnapshots.settled("kid", "dark"), true);
  assert.equal(Nested.scheduleSnapshot(ctrl, node), false, "a failed slot is not retried by every re-render");
  assert.match(N.nestedBodyHtml(node, Nested.nestedCard(ctrl, node), null), /nm-nested-empty|nm-nested-svg|Empty NodeMap/);
  Nested.disposeSnapshots(ctrl);
  assert.equal(Nested.snapshotCacheOf(ctrl), null, "no cache is created after dispose");
  assert.equal(Nested.scheduleSnapshot(ctrl, node), false);
});

test("embedding rewrites attribute ids only: user text with the id token is kept verbatim", async () => {
  const text = 'see url(#nmsnapq7-nmclip0) and id="nmsnapq7-x"';
  const child = doc([note("c1", 0, 0, text)]);
  const cache = new Q.NestedSnapshotCache({ render: (d) => canvasToSvg(d, (x) => x, { idPrefix: Q.SNAPSHOT_ID_TOKEN }), urls: fakeUrls(), defer: instant });
  const entry = await cache.request({ canvasId: "k", canvas: child, theme: "t" });
  const out = Q.embedSnapshotSvg(entry, { x: 0, y: 0, w: 100, h: 100 }, "nq0-");
  assert.match(out, /see url\(#nmsnapq7-nmclip0\)/, "text content untouched");
  assert.match(out, /id=&quot;nmsnapq7-x&quot;/);
  assert.ok(idsOf(out).every((id) => id.startsWith("nq0-")), "every real id rewritten");
  for (const [, target] of out.matchAll(/="url\(#([^)]+)\)/g)) assert.ok(target.startsWith("nq0-"));
  assert.equal(Q.embedSnapshotSvg(entry, { x: 0, y: 0, w: 100, h: 100 }, "nq0-"), out, "memoized per prefix");
});

test("disposal mid-render releases the picture; data payload readiness is part of the content key", async () => {
  const urls = fakeUrls();
  let release;
  const cache = new Q.NestedSnapshotCache({ render: (d) => canvasToSvg(d, (x) => x), urls, defer: () => new Promise((r) => { release = r; }) });
  const p = cache.request({ canvasId: "k", canvas: doc([note("a")]), theme: "t" });
  cache.clear({ dispose: true });
  release();
  assert.equal(await p, null);
  assert.equal(urls.made.length, 0, "nothing rendered after disposal");
  // a payload arriving later changes the key (the placeholder picture is not reused)
  const ctrl = { modelOf: (n) => (n.id === "s" && ctrl.loaded ? { name: "x" } : null), loaded: false };
  const cv = doc([note("s")]);
  const before = Nested.snapshotContentSignature(ctrl, cv);
  ctrl.loaded = true;
  assert.notEqual(Nested.snapshotContentSignature(ctrl, cv), before);
  let renders = 0;
  const c2 = new Q.NestedSnapshotCache({ render: (d) => (renders++, canvasToSvg(d, (x) => x)), urls: fakeUrls(), defer: instant });
  await c2.request({ canvasId: "k", canvas: cv, theme: "t", extra: before });
  await c2.request({ canvasId: "k", canvas: cv, theme: "t", extra: Nested.snapshotContentSignature(ctrl, cv) });
  assert.equal(renders, 2);
});
