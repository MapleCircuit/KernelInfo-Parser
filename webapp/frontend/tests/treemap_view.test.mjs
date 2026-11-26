import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { FakeElement } from "./helpers/dom_shim.mjs";

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

let roCallback = null;
globalThis.ResizeObserver = class {
  constructor(cb) { roCallback = cb; }
  observe() {}
  disconnect() { roCallback = null; }
};

const { TreemapView } = await import("../js/views/treemap/treemap_view.js");
const { state } = await import("../js/state.js");

test("TreemapView caches hover path in onmousemove, coalesces ResizeObserver via rAF, and cleans up in dispose", async () => {
  const mk = (tag = "div") => {
    const el = new FakeElement(tag);
    el.isConnected = true;
    el.clientWidth = 600;
    el.clientHeight = 400;
    return el;
  };
  const viewBox = mk("div");
  let rectCalls = 0;
  viewBox.getBoundingClientRect = () => {
    rectCalls++;
    return { left: 0, top: 0, width: 600, height: 400 };
  };
  const els = {
    ".treemap-view": viewBox,
    ".treemap-crumbs": mk("div"),
    ".treemap-summary": mk("span"),
    ".treemap-legend": mk("div"),
    ".treemap-canvas": mk("div"),
    ".treemap-tooltip": mk("div"),
    ".treemap-build-btn": mk("button"),
  };
  const containerEl = {
    isConnected: true,
    _html: "",
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    querySelector: (sel) => els[sel] || null,
    querySelectorAll: () => [],
  };

  const view = new TreemapView();
  view.api = {
    async getTreemap() {
      return {
        path: "",
        size: 1000,
        file_count: 10,
        children: [{ path: "net", name: "net", type: "dir", size: 1000, file_count: 10, children: [] }],
      };
    },
  };
  await view.render(containerEl, { id: "tm-1", type: "treemap", version: "v3.0" });

  // Hovering the same node repeatedly should not rebuild tooltip HTML or call getBoundingClientRect()
  const target = { closest: () => ({ dataset: { path: "net" } }) };
  els[".treemap-canvas"].onmousemove({ target, clientX: 10, clientY: 10 });
  els[".treemap-canvas"].onmousemove({ target, clientX: 20, clientY: 20 });
  assert.equal(rectCalls, 1);

  // Coalesced ResizeObserver schedules one rAF, cancelled on dispose
  roCallback?.();
  roCallback?.();
  assert.ok(view._rafId !== 0);

  view.engineTabId = "eng-1";
  state.emit("kconfig:assignments:updated", { tabId: "eng-1" });
  assert.ok(view._reloadTimer !== null);

  const prevSeq = view._loadSeq;
  view.dispose();
  assert.equal(view.containerEl, null);
  assert.equal(view._reloadTimer, null);
  assert.equal(view._rafId, 0);
  assert.ok(view._loadSeq > prevSeq);
});
