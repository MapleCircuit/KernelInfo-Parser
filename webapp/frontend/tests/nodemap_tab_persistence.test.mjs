/**
 * nodemap_tab_persistence.test.mjs - Persistent embedded `tab` nodes: the `frozen` / `viewState` fields,
 * the embed shell bridge (`components/embed_bridge.js`: handshake, restore, debounced reports, origin and
 * source checks), the NodeMap side (`nodemap_tab_sync.js`: Keep changes written back without reloading
 * the frame or recording an undo step, Frozen ignoring reports, freeze snapshot, Reset, undo), the header
 * / inspector / multi-select toggles, and `components/tabs.js` keeping a NodeMap's DOM mounted across tab
 * switches (disposed on close and on a project switch).
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

const E = await import("../js/utils/embed_mode.js");
const { EmbedBridge } = await import("../js/components/embed_bridge.js");
const M = await import("../js/views/nodemap/canvas_model.js");
const S = await import("../js/views/nodemap/nodemap_tab_sync.js");
const { NodeMapController } = await import("../js/views/nodemap/nodemap_controller.js");
const { contentKey } = await import("../js/views/nodemap/node_renderer.js");
const { onInspectorClick, onInspectorInput, renderInspector } = await import("../js/views/nodemap/nodemap_inspector.js");
const { onClick } = await import("../js/views/nodemap/nodemap_gestures.js");
const { state } = await import("../js/state.js");
const { TabsComponent } = await import("../js/components/tabs.js");

const ORIGIN = globalThis.location.origin;
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------ model

test("tab nodes: frozen defaults to Keep changes; viewState is bounded and sanitized", () => {
  const n = M.createNode("tab", { data: { tabType: "code", hash: "#tab=code&path=a.c" } });
  assert.equal(n.data.frozen, false, "Keep changes by default");
  assert.equal(n.data.viewState, null);
  assert.equal(n.data.hash, "tab=code&path=a.c");
  const d = M.normalizeData("tab", {
    tabType: "code", hash: "tab=code", frozen: true,
    viewState: { scroll: { "|div.a:0": [120.4, 0], bad: [-1, 2], zero: [0, 0], "|x": ["a", 1] }, fields: { query: "fork", id: "x", initialNodes: 1, sel: [1, "a", true], obj: { a: 1 }, "bad key": 1 } },
  });
  assert.equal(d.frozen, true);
  assert.deepEqual(d.viewState, { scroll: { "|div.a:0": [120, 0] }, fields: { query: "fork", sel: [1, "a", true] } });
  assert.equal(M.normalizeData("tab", { frozen: "yes" }).frozen, false, "only true freezes");
  const big = { fields: Object.fromEntries(Array.from({ length: 40 }, (_, i) => [`f${i}`, "x".repeat(1999)])), scroll: { "|a": [1, 1] } };
  assert.deepEqual(E.normalizeEmbedViewState(big), { scroll: { "|a": [1, 1] } }, "over the cap: fields go first");
  assert.equal(E.normalizeEmbedViewState({ scroll: {} }), null);
  assert.equal(E.normalizeEmbedViewState([1]), null);
});

test("embedSrc names the node the shell reports to; JSON round trip keeps the tab fields", () => {
  assert.equal(M.embedSrc("tab=code", "/", "t-1:s", "n-abc"), "/?embed=1&embedSession=t-1%3As&embedNode=n-abc#tab=code");
  assert.equal(M.embedSrc("tab=code", "/", "", "bad id!"), "/?embed=1#tab=code", "an invalid node id is left out");
  assert.equal(E.embedNodeToken({ search: "?embed=1&embedNode=n-abc" }), "n-abc");
  assert.equal(E.embedNodeToken({ search: "?embedNode=n-abc" }), "", "only an embed shell has one");
  const c = M.emptyCanvas();
  c.nodes.push(M.createNode("tab", { id: "t1", data: { tabType: "code", hash: "tab=code", frozen: true, viewState: { scroll: { "|a": [5, 0] } } } }));
  const back = M.normalizeCanvas(M.serializeCanvas(c));
  assert.deepEqual(back.nodes[0].data, c.nodes[0].data);
});

// ------------------------------------------------------------------ embed shell bridge

function fakeWindow() {
  const listeners = new Map();
  const posted = [];
  const parent = { postMessage: (msg, origin) => posted.push({ msg, origin }) };
  const win = {
    parent,
    addEventListener: (t, fn) => (listeners.get(t) || listeners.set(t, new Set()).get(t)).add(fn),
    removeEventListener: (t, fn) => listeners.get(t)?.delete(fn),
    fire: (t, ev) => [...(listeners.get(t) || [])].forEach((fn) => fn(ev)),
    count: (t) => listeners.get(t)?.size || 0,
  };
  return { win, parent, posted };
}

function fakeState(tab) {
  const subs = new Map();
  return {
    getActiveTab: () => tab,
    subscribe: (ev, fn) => {
      (subs.get(ev) || subs.set(ev, new Set()).get(ev)).add(fn);
      return () => subs.get(ev)?.delete(fn);
    },
    emit: (ev) => [...(subs.get(ev) || [])].forEach((fn) => fn()),
  };
}

test("embed bridge: hello → restore from the parent only; the view state lands on the tab", async () => {
  const { win, parent, posted } = fakeWindow();
  const tab = { id: "e1", type: "code", title: "a.c" };
  let renders = 0;
  const loc = { search: "?embed=1&embedNode=n-1", hash: "#tab=code", origin: ORIGIN };
  const b = new EmbedBridge({ win, loc, doc: null, state: fakeState(tab), render: () => renders++, helloTimeout: 200 });
  assert.equal(b.enabled, true);
  const p = b.handshake();
  assert.deepEqual(posted[0], { msg: { type: E.EMBED_MSG.hello, node: "n-1" }, origin: ORIGIN }, "addressed to this origin only");
  const vs = { scroll: { "|div.x:0": [40, 0] }, fields: { query: "fork" } };
  win.fire("message", { origin: "https://evil.example", source: parent, data: { type: E.EMBED_MSG.restore, node: "n-1", viewState: { fields: { query: "x" } } } });
  win.fire("message", { origin: ORIGIN, source: {}, data: { type: E.EMBED_MSG.restore, node: "n-1", viewState: { fields: { query: "x" } } } });
  win.fire("message", { origin: ORIGIN, source: parent, data: { type: E.EMBED_MSG.restore, node: "n-2", viewState: { fields: { query: "x" } } } });
  win.fire("message", { origin: ORIGIN, source: parent, data: { type: E.EMBED_MSG.restore, node: "n-1", viewState: vs } });
  assert.deepEqual(await p, vs, "only the parent's answer for this node counts");
  assert.equal(win.count("message"), 0, "the handshake listener is gone");
  assert.equal(b.applyRestore(await p), true);
  assert.equal(tab.query, "fork");
  assert.deepEqual(tab.scrollPositions, { "|div.x:0": [40, 0] });
  assert.equal(renders, 1);
});

test("embed bridge: no answer within the timeout, or no NodeMap around, means nothing to restore", async () => {
  const { win } = fakeWindow();
  const b = new EmbedBridge({ win, loc: { search: "?embed=1&embedNode=n-1", origin: ORIGIN }, state: fakeState(null), helloTimeout: 20 });
  assert.equal(await b.handshake(), null);
  const top = { parent: null };
  top.parent = top;
  const alone = new EmbedBridge({ win: top, loc: { search: "?embed=1&embedNode=n-1", origin: ORIGIN }, state: fakeState(null) });
  assert.equal(alone.enabled, false);
  assert.equal(await alone.handshake(), null);
});

test("embed bridge: reports are debounced, deduplicated and carry hash, title and view state", async () => {
  const { win, posted } = fakeWindow();
  const tab = { id: "e1", type: "code", title: "a.c", query: "q" };
  const st = fakeState(tab);
  const loc = { search: "?embed=1&embedNode=n-1", hash: "#tab=code", origin: ORIGIN };
  const b = new EmbedBridge({ win, loc, doc: null, state: st, delay: 10 });
  b.start();
  await tick(30);
  assert.equal(posted.length, 1, "the opening state is reported once");
  assert.deepEqual(posted[0].msg, { type: E.EMBED_MSG.state, node: "n-1", hash: "tab=code", title: "a.c", viewState: { fields: { query: "q" } } });
  st.emit("tab:state");
  st.emit("tab:state");
  await tick(30);
  assert.equal(posted.length, 1, "nothing changed: nothing sent");
  loc.hash = "#tab=code&path=b.c";
  tab.scrollPositions = { "|pre:0": [300, 0] };
  win.fire("hashchange");
  st.emit("tab:state");
  await tick(30);
  assert.equal(posted.length, 2, "two triggers within the delay: one report");
  assert.equal(posted[1].msg.hash, "tab=code&path=b.c");
  assert.deepEqual(posted[1].msg.viewState.scroll, { "|pre:0": [300, 0] });
  b.dispose();
  win.fire("hashchange");
  await tick(30);
  assert.equal(posted.length, 2, "disposed: silent");
});

// ------------------------------------------------------------------ NodeMap side

/** A controller with a stub renderer whose tab node has a frame with a fake window. */
function tabController({ frozen = false } = {}) {
  const c = new NodeMapController();
  c.tab = { id: "nm-1", type: "nodemap", version: "v3.0", repo: "" };
  c.canvas = M.emptyCanvas();
  const node = M.createNode("tab", { id: "t1", title: "code tab", data: { tabType: "code", hash: "tab=code&path=a.c", frozen } });
  c.canvas.nodes.push(node);
  const frameWin = { posted: [], postMessage(msg, origin) { this.posted.push({ msg, origin }); } };
  const frame = { contentWindow: frameWin, dataset: {} };
  const titleEl = { textContent: "", querySelector: () => null };
  const nodeEl = { querySelector: (sel) => (sel === "iframe.nm-iframe" ? frame : sel === ".nm-title" ? titleEl : null) };
  const calls = { commits: [], saves: 0, invalidated: [], inspector: 0 };
  c.renderer = { element: (id) => (id === "t1" ? nodeEl : null), invalidate: (id) => calls.invalidated.push(id), sync() {}, clear() {} };
  c.commit = (opts = {}) => calls.commits.push(opts);
  c.scheduleSave = () => { calls.saves += 1; };
  c.renderInspector = () => { calls.inspector += 1; };
  S.frameMounted(c, node);
  return { c, node, frame, frameWin, titleEl, calls };
}

const stateMsg = (frameWin, data) => ({ origin: ORIGIN, source: frameWin, data: { type: E.EMBED_MSG.state, node: "t1", ...data } });

test("tab sync: hello is answered with the node's saved view state, to its own frame only", () => {
  const { c, node, frameWin } = tabController();
  node.data.viewState = { scroll: { "|a": [9, 0] } };
  assert.equal(S.onTabFrameMessage(c, { origin: ORIGIN, source: frameWin, data: { type: E.EMBED_MSG.hello, node: "t1" } }), true);
  assert.deepEqual(frameWin.posted, [{ msg: { type: E.EMBED_MSG.restore, node: "t1", viewState: { scroll: { "|a": [9, 0] } } }, origin: ORIGIN }]);
  assert.equal(S.onTabFrameMessage(c, { origin: "https://evil.example", source: frameWin, data: { type: E.EMBED_MSG.hello, node: "t1" } }), false, "another origin");
  assert.equal(S.onTabFrameMessage(c, { origin: ORIGIN, source: {}, data: { type: E.EMBED_MSG.hello, node: "t1" } }), false, "another frame");
  assert.equal(S.onTabFrameMessage(c, { origin: ORIGIN, source: frameWin, data: { type: E.EMBED_MSG.hello, node: "nope" } }), false, "unknown node");
  assert.equal(S.onTabFrameMessage(c, { origin: ORIGIN, source: frameWin, data: { type: E.EMBED_MSG.hello, node: "<x>" } }), false, "malformed token");
  assert.equal(frameWin.posted.length, 1);
});

test("tab sync, Keep changes: reports are written back and saved, without an undo step or a frame reload", () => {
  const { c, node, frameWin, titleEl, calls } = tabController();
  const key0 = contentKey(node, "", S.tabKeyData(c, node));
  S.onTabFrameMessage(c, stateMsg(frameWin, { hash: "#tab=code&path=b.c", title: "b.c", viewState: { scroll: { "|pre:0": [200, 0] } } }));
  assert.equal(node.data.hash, "tab=code&path=b.c");
  assert.deepEqual(node.data.viewState, { scroll: { "|pre:0": [200, 0] } });
  assert.equal(node.title, "b.c", "the auto title follows the tab");
  assert.equal(titleEl.textContent, "b.c");
  assert.equal(calls.saves, 1, "the canvas is saved");
  assert.equal(calls.commits.length, 0, "no commit: no undo step, no re-render");
  assert.equal(contentKey(node, "", S.tabKeyData(c, node)), key0, "the frame's own navigation never reloads it");
  S.onTabFrameMessage(c, stateMsg(frameWin, { hash: "tab=code&path=b.c", title: "b.c", viewState: { scroll: { "|pre:0": [200, 0] } } }));
  assert.equal(calls.saves, 1, "an unchanged report saves nothing");
  node.customTitle = "Mine";
  S.onTabFrameMessage(c, stateMsg(frameWin, { hash: "tab=code&path=c.c", title: "c.c" }));
  assert.equal(node.title, "b.c", "a renamed node keeps its name");
  node.data.hash = "tab=code&path=z.c"; // an edit / import / undo from outside the frame
  assert.notEqual(contentKey(node, "", S.tabKeyData(c, node)), key0, "any other address reloads the frame");
});

test("tab sync: while the stored canvas loads, reports are ignored and hellos wait for its view state", () => {
  const { c, node, frameWin, calls } = tabController();
  c._storeLoading = true;
  S.onTabFrameMessage(c, stateMsg(frameWin, { hash: "tab=code&path=b.c" }));
  assert.equal(node.data.hash, "tab=code&path=a.c", "a report now would be replaced by the stored canvas");
  assert.equal(calls.saves, 0);
  assert.equal(S.onTabFrameMessage(c, { origin: ORIGIN, source: frameWin, data: { type: E.EMBED_MSG.hello, node: "t1" } }), true);
  assert.equal(frameWin.posted.length, 0, "not answered yet");
  node.data.viewState = { fields: { query: "stored" } }; // the stored canvas arrived
  c._storeLoading = false;
  S.flushPendingHellos(c);
  assert.deepEqual(frameWin.posted.map((p) => p.msg.viewState), [{ fields: { query: "stored" } }]);
});

test("view state: prototype machinery names are never fields; restore never overwrites tab methods", () => {
  const raw = JSON.parse('{"fields":{"__proto__":[1],"constructor":"x","toString":"y","__x":1,"query":"ok"}}');
  assert.deepEqual(E.normalizeEmbedViewState(raw), { fields: { query: "ok" } });
  const { win } = fakeWindow();
  const tab = { id: "e1", title: "t", render() { return 1; } };
  const b = new EmbedBridge({ win, loc: { search: "?embed=1&embedNode=n-1", origin: ORIGIN }, state: fakeState(tab) });
  b.applyRestore({ fields: { render: "x", query: "q" } });
  assert.equal(typeof tab.render, "function");
  assert.equal(tab.query, "q");
  assert.equal(Object.getPrototypeOf(tab), Object.prototype);
});

test("tab sync: a torn-down frame forgets its reports (a later address change reloads)", () => {
  const { c, node, frameWin } = tabController();
  S.onTabFrameMessage(c, stateMsg(frameWin, { hash: "tab=code&path=b.c" }));
  const epoch = S.liveOf(c, "t1").epoch;
  S.frameDisposed(c, "t1");
  assert.equal(S.liveOf(c, "t1").reported, false);
  assert.equal(S.liveOf(c, "t1").epoch, epoch, "the Reset count survives");
  assert.equal(S.tabKeyData(c, node).hash, node.data.hash);
});

test("tab sync, Frozen: reports leave the node alone and mark it dirty; Reset reloads it", () => {
  const { c, node, frameWin, calls } = tabController({ frozen: true });
  const key0 = contentKey(node, "", S.tabKeyData(c, node));
  S.onTabFrameMessage(c, stateMsg(frameWin, { hash: "tab=code&path=b.c", viewState: { scroll: { "|a": [3, 0] } } }));
  assert.equal(node.data.hash, "tab=code&path=a.c", "frozen: the hash stays");
  assert.equal(node.data.viewState, null);
  assert.equal(calls.saves, 0);
  assert.equal(S.isTabDirty(c, "t1"), true);
  assert.equal(calls.commits.length, 1, "the header shows the dirty state");
  assert.equal(contentKey(node, "", S.tabKeyData(c, node)), key0, "no reload while frozen either");
  assert.equal(S.resetDirtyFrozenTabs(c), 1);
  assert.deepEqual(calls.invalidated, ["t1"]);
  assert.notEqual(contentKey(node, "", S.tabKeyData(c, node)), key0, "Reset keys a new frame");
  assert.equal(S.isTabDirty(c, "t1"), false);
  assert.equal(S.resetDirtyFrozenTabs(c), 0, "clean: nothing to reset");
});

test("tab sync: freezing snapshots what the frame shows; unfreezing keeps it from there", () => {
  const { c, node, frameWin } = tabController();
  c.byId = (id) => c.canvas.nodes.find((n) => n.id === id) || null;
  // what the frame shows now, not yet written into the node
  Object.assign(S.liveOf(c, "t1"), { hash: "tab=code&path=b.c", title: "b.c", viewState: { fields: { query: "q" } }, reported: true });
  assert.equal(S.setTabsFrozen(c, ["t1"], true), 1);
  assert.equal(node.data.frozen, true);
  assert.equal(node.data.hash, "tab=code&path=b.c", "the live state is what gets frozen");
  assert.deepEqual(node.data.viewState, { fields: { query: "q" } });
  assert.equal(S.setTabsFrozen(c, ["t1"], true), 0, "already frozen");
  S.onTabFrameMessage(c, stateMsg(frameWin, { hash: "tab=code&path=c.c" }));
  assert.equal(node.data.hash, "tab=code&path=b.c");
  S.setTabsFrozen(c, ["t1"], false);
  assert.equal(node.data.frozen, false);
  assert.equal(node.data.hash, "tab=code&path=c.c", "unfreezing keeps what the tab shows now");
});

test("tab sync: undo keeps a Keep-changes tab's live address, a frozen snapshot restores its own", () => {
  const { c, node, frameWin } = tabController();
  S.onTabFrameMessage(c, stateMsg(frameWin, { hash: "tab=code&path=b.c" }));
  const old = { ...node, data: { ...node.data, hash: "tab=code&path=a.c" } };
  S.keepLiveOnRestore(c, node, old);
  assert.equal(old.data.hash, "tab=code&path=b.c");
  const frozenSnap = { ...node, data: { ...node.data, hash: "tab=code&path=a.c", frozen: true } };
  S.keepLiveOnRestore(c, node, frozenSnap);
  assert.equal(frozenSnap.data.hash, "tab=code&path=a.c");
});

// ------------------------------------------------------------------ toggles (header, inspector, multi-select)

function uiController(nodes) {
  const c = new NodeMapController();
  c.tab = { id: "nm-1", type: "nodemap", version: "v3.0", repo: "" };
  c.canvas = M.emptyCanvas();
  c.canvas.nodes.push(...nodes);
  c.renderer = { element: () => null, invalidate() {}, sync() {}, clear() {} };
  c.inspectorEl = { innerHTML: "", querySelectorAll: () => [], querySelector: () => null, contains: () => false };
  c.renderWires = () => {};
  c.scheduleSave = () => {};
  return c;
}

test("header: the tab mode toggle (aria-pressed) and Reset when frozen; clicks switch it", () => {
  const n = M.createNode("tab", { id: "t1", data: { tabType: "code", hash: "tab=code" } });
  const c = uiController([n]);
  let html = c.headTools(n, false);
  assert.match(html, /data-act="tab-freeze" aria-pressed="false"/);
  assert.doesNotMatch(html, /tab-reset/);
  const btn = (act) => ({ dataset: { act } });
  const nodeEl = { dataset: { nodeId: "t1" } };
  const ev = (act) => ({ target: { closest: (sel) => (sel === "[data-act]" ? btn(act) : sel === ".nm-node" ? nodeEl : null) }, stopPropagation() {} });
  let frozenCalls = [];
  c.setTabsFrozen = (ids, f) => frozenCalls.push([ids, f]);
  onClick(c, ev("tab-freeze"));
  assert.deepEqual(frozenCalls, [[["t1"], true]]);
  n.data.frozen = true;
  html = c.headTools(n, false);
  assert.match(html, /data-act="tab-freeze" aria-pressed="true"/);
  assert.match(html, /data-act="tab-reset"/);
  let resets = [];
  c.resetTabNode = (id) => resets.push(id);
  onClick(c, ev("tab-reset"));
  assert.deepEqual(resets, ["t1"]);
});

test("inspector: single and multi-select tab mode controls (mixed state) apply to the selection", () => {
  const a = M.createNode("tab", { id: "ta", data: { tabType: "code", hash: "tab=code" } });
  const b = M.createNode("tab", { id: "tb", data: { tabType: "commits", hash: "tab=commits", frozen: true } });
  const s = M.createNode("struct", { id: "s1", data: { name: "page" } });
  const c = uiController([a, b, s]);
  c.selected = new Set(["ta"]);
  renderInspector(c);
  assert.match(c.inspectorEl.innerHTML, /data-insp="tab-keep" aria-pressed="true"/);
  assert.match(c.inspectorEl.innerHTML, /data-insp="tab-freeze" aria-pressed="false"/);
  c.selected = new Set(["ta", "tb", "s1"]);
  renderInspector(c);
  assert.match(c.inspectorEl.innerHTML, /Tab changes \(2, mixed\)/);
  assert.match(c.inspectorEl.innerHTML, /data-insp="tab-keep" aria-pressed="false"/);
  assert.match(c.inspectorEl.innerHTML, /data-insp="tab-freeze" aria-pressed="false"/);
  assert.match(c.inspectorEl.innerHTML, /data-insp="tab-reset"/, "some are frozen: Reset is offered");
  const commits = [];
  c.commit = (o) => commits.push(o);
  const click = (insp) => onInspectorClick(c, { target: { closest: () => ({ dataset: { insp } }) } });
  click("tab-freeze");
  assert.equal(a.data.frozen, true, "every selected tab node freezes");
  assert.equal(b.data.frozen, true);
  assert.equal(commits.length, 1, "one undo step");
  click("tab-keep");
  assert.equal(a.data.frozen, false);
  assert.equal(b.data.frozen, false);
  b.data.frozen = true;
  const resets = [];
  c.resetTabNode = (id) => resets.push(id);
  c.renderInspector = () => {};
  click("tab-reset");
  assert.deepEqual(resets, ["tb"], "Reset reloads the frozen ones");
  assert.equal(typeof onInspectorInput, "function");
});

// ------------------------------------------------------------------ TabsComponent keep-alive

/** A small DOM: real parent / child links, class lists and simple `.class` queries. */
class El {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.localName = tag;
    this.childNodes = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this._cls = new Set();
    this.attrs = {};
    this.textContent = "";
    this._html = "";
    this.inserts = 0;
    const self = this;
    this.classList = {
      add: (...c) => c.forEach((x) => self._cls.add(x)),
      remove: (...c) => c.forEach((x) => self._cls.delete(x)),
      contains: (c) => self._cls.has(c),
      toggle: (c, on) => ((on ?? !self._cls.has(c)) ? self._cls.add(c) : self._cls.delete(c)),
    };
  }
  get className() { return [...this._cls].join(" "); }
  set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    this._html = String(v);
  }
  get children() { return this.childNodes; }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() {
    const sib = this.parentNode?.childNodes;
    return sib ? sib[sib.indexOf(this) + 1] || null : null;
  }
  get isConnected() {
    let cur = this;
    while (cur.parentNode) cur = cur.parentNode;
    return Boolean(cur.isRoot);
  }
  appendChild(c) { return this.insertBefore(c, null); }
  insertBefore(c, ref) {
    c.parentNode?.removeChild(c);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(c);
    else this.childNodes.splice(i, 0, c);
    c.parentNode = this;
    c.inserts += 1;
    return c;
  }
  removeChild(c) {
    const i = this.childNodes.indexOf(c);
    if (i >= 0) this.childNodes.splice(i, 1);
    c.parentNode = null;
    return c;
  }
  remove() { this.parentNode?.removeChild(this); }
  addEventListener() {}
  removeEventListener() {}
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  contains(o) {
    for (let cur = o; cur; cur = cur.parentNode) if (cur === this) return true;
    return false;
  }
  closest(sel) {
    const cls = sel.startsWith(".") ? sel.slice(1) : null;
    for (let cur = this; cur; cur = cur.parentNode) if (cls && cur._cls?.has(cls)) return cur;
    return null;
  }
  querySelectorAll(sel) {
    const cls = sel.startsWith(".") ? sel.slice(1).split(/[\s:]/)[0] : null;
    const out = [];
    const walk = (el) => el.childNodes.forEach((c) => {
      if (cls && c._cls?.has(cls)) out.push(c);
      walk(c);
    });
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 }; }
}

function keepAliveHarness() {
  const prevCreate = globalThis.document.createElement;
  globalThis.document.createElement = (tag) => new El(tag);
  const container = new El("div");
  const root = new El("div");
  root.isRoot = true;
  root.appendChild(container);
  const views = [];
  const factory = () => {
    const v = {
      keepAlive: true, renders: [], hidden: 0, disposed: 0, rootEl: null,
      render(el, tab) {
        this.renders.push(el);
        if (!this.rootEl) {
          this.rootEl = new El("div");
          this.rootEl.className = "nodemap-container";
          this.frame = new El("iframe");
          this.rootEl.appendChild(this.frame);
        }
        if (this.rootEl.parentNode !== el) el.appendChild(this.rootEl);
        this.tab = tab;
      },
      onHidden() { this.hidden += 1; },
      dispose() { this.disposed += 1; this.rootEl?.remove(); },
    };
    views.push(v);
    return v;
  };
  const code = { renders: 0, render(el) { this.renders += 1; el.innerHTML = "<pre>code</pre>"; } };
  const comp = new TabsComponent(container, { nodemap: factory, code });
  return { comp, container, views, code, restore: () => { globalThis.document.createElement = prevCreate; } };
}

function resetPanes(panes, split = "single") {
  state.currentRepo = "linux";
  state.currentVersion = "v3.0";
  state.splitMode = split;
  state.panes = panes;
  state.activePaneId = panes[0].id;
}

test("tabs: a NodeMap's DOM and iframes stay mounted across tab switches (parked, then shown again)", () => {
  const { comp, views, code, restore } = keepAliveHarness();
  try {
    const nm = { id: "nm", type: "nodemap", title: "Map", version: "v3.0", repo: "linux" };
    const cf = { id: "cf", type: "code", title: "a.c", path: "a.c", version: "v3.0", repo: "linux" };
    resetPanes([{ id: "p1", activeTabId: "nm", tabs: [nm, cf] }]);
    comp.render();
    assert.equal(views.length, 1);
    const v = views[0];
    const host = v.renders[0];
    assert.equal(host.classList.contains("tab-keepalive-host"), true);
    assert.equal(v.frame.inserts, 1);
    const hostInserts = host.inserts;
    const rootInserts = v.rootEl.inserts;

    state.panes[0].activeTabId = "cf";
    comp.render();
    assert.equal(code.renders, 1);
    assert.equal(host.classList.contains("is-parked"), true, "parked, not torn down");
    assert.equal(host.inert, true);
    assert.equal(v.hidden, 1);
    assert.equal(host.isConnected, true, "still in the document");

    state.panes[0].activeTabId = "nm";
    comp.render();
    assert.equal(v.renders.at(-1), host, "rendered again into the same host");
    assert.equal(host.classList.contains("is-parked"), false);
    assert.equal(host.inserts, hostInserts, "the host never moved");
    assert.equal(v.rootEl.inserts, rootInserts, "the NodeMap root never moved (its iframes never reload)");
    assert.equal(v.frame.inserts, 1);
    assert.equal(views.length, 1, "the same view instance");
    assert.equal(v.disposed, 0);
  } finally {
    restore();
  }
});

test("tabs: split panes keep the host; a tab moved to the other pane takes it along; close and project switch dispose", () => {
  const { comp, container, views, restore } = keepAliveHarness();
  try {
    const nm = { id: "nm", type: "nodemap", title: "Map", version: "v3.0", repo: "linux" };
    const cf = { id: "cf", type: "code", title: "a.c", path: "a.c", version: "v3.0", repo: "linux" };
    resetPanes([{ id: "p1", activeTabId: "nm", tabs: [nm] }, { id: "p2", activeTabId: "cf", tabs: [cf] }], "horizontal");
    comp.render();
    const v = views[0];
    const host = v.renders[0];
    const p1 = comp.paneEls.get("p1");
    assert.equal(host.parentNode, p1);
    assert.deepEqual(container.childNodes.map((x) => x.className), ["pane active-pane", "pane-divider", "pane"]);
    comp.render();
    assert.equal(comp.paneEls.get("p1"), p1, "panes persist across renders");
    assert.equal(v.frame.inserts, 1);

    // drag the NodeMap tab into the other pane: its host goes with it
    state.panes[0].tabs = [];
    state.panes[0].activeTabId = null;
    state.panes[1].tabs = [cf, nm];
    state.panes[1].activeTabId = "nm";
    comp.render();
    assert.equal(host.parentNode, comp.paneEls.get("p2"));
    assert.equal(host.dataset.paneId, "p2");
    assert.equal(views.length, 1, "same view");

    // the project of the tab changes: a fresh view, the old one disposed with its host
    nm.repo = "man-pages";
    comp.render();
    assert.equal(v.disposed, 1);
    assert.equal(host.parentNode, null, "old host removed");
    assert.equal(views.length, 2);
    const host2 = views[1].renders[0];
    assert.notEqual(host2, host);

    // close it
    state.panes[1].tabs = [cf];
    state.panes[1].activeTabId = "cf";
    comp.pruneTabViews();
    comp.render();
    assert.equal(views[1].disposed, 1);
    assert.equal(host2.parentNode, null, "no detached DOM left in the panes");
    assert.equal(comp.keepAliveHosts.size, 0);
    assert.equal(comp.perTabViews.size, 0);

    // closing the first pane (no moveBefore in this DOM): the surviving pane never moves
    resetPanes([{ id: "p1", activeTabId: "cf", tabs: [cf] }, { id: "p2", activeTabId: "nm2", tabs: [{ id: "nm2", type: "nodemap", title: "M2", version: "v3.0", repo: "linux" }] }], "horizontal");
    comp.render();
    const p2 = comp.paneEls.get("p2");
    const p2Inserts = p2.inserts;
    resetPanes([{ id: "p2", activeTabId: "nm2", tabs: state.panes[1].tabs }]);
    comp.render();
    assert.equal(p2.inserts, p2Inserts, "a stale pane is dropped before the order is fixed");
    assert.deepEqual(container.childNodes.map((x) => x.dataset.paneId), ["p2"]);

    // back to one pane: the second pane element goes
    resetPanes([{ id: "p1", activeTabId: "cf", tabs: [cf] }]);
    comp.render();
    assert.equal(comp.paneEls.has("p2"), false);
    assert.deepEqual(container.childNodes.map((x) => x.dataset.paneId), ["p1"]);
  } finally {
    restore();
  }
});

test("NodeMapController: hidden while parked; shown again it keeps its root and resets dirty frozen tabs", () => {
  const c = new NodeMapController();
  const host = new El("div");
  const rootEl = new El("div");
  host.appendChild(rootEl);
  c.rootEl = rootEl;
  c.canvas = M.emptyCanvas();
  c.rootCanvas = c.canvas;
  const n = M.createNode("tab", { id: "t1", data: { tabType: "code", hash: "tab=code", frozen: true } });
  c.canvas.nodes.push(n);
  c.renderer = { element: () => null, invalidate() {}, sync() {}, clear() {} };
  const commits = [];
  c.commit = (o) => commits.push(o);
  c.subscribe = () => {};
  c.applyTransform = () => {};
  c.renderBreadcrumbs = () => {};
  c.consumeIncoming = () => [];
  let flushed = 0;
  c.flushSave = () => { flushed += 1; };
  c.onHidden();
  assert.equal(c.hidden, true);
  assert.equal(flushed, 1, "saved when parked");
  S.liveOf(c, "t1").dirty = true;
  const epoch = S.liveOf(c, "t1").epoch;
  c.render(host, { id: "nm-1", type: "nodemap", version: "v3.0" });
  assert.equal(c.hidden, false);
  assert.equal(rootEl.parentNode, host);
  assert.equal(rootEl.inserts, 1, "not re-appended");
  assert.equal(S.liveOf(c, "t1").epoch, epoch + 1, "the dirty frozen tab was reset");
  c.dispose();
  assert.equal(c.tabLive.size, 0);
});
