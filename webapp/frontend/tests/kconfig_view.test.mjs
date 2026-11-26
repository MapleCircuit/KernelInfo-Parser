import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

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

import { FakeElement } from "./helpers/dom_shim.mjs";
const { state } = await import("../js/state.js");
const { KconfigView } = await import("../js/views/kconfig/kconfig_view.js");
const { MenuconfigTui } = await import("../js/views/kconfig/menuconfig_tui.js");

const RAW = JSON.parse(fs.readFileSync(new URL("./fixtures/kconfig_tiny.json", import.meta.url), "utf8"));

function makeRichEl(tag = "div") {
  const el = new FakeElement(tag);
  el.isConnected = true;
  el.value = "";
  el.scrollTop = 0;
  el.scrollIntoView = () => {};
  el.focus = () => {};
  el.insertAdjacentHTML = () => {};
  el.classList = {
    _set: new Set(),
    add(...xs) { xs.forEach((x) => this._set.add(x)); },
    remove(...xs) { xs.forEach((x) => this._set.delete(x)); },
    toggle(x, on) { if (on === undefined) { if (this._set.has(x)) this._set.delete(x); else this._set.add(x); } else if (on) this._set.add(x); else this._set.delete(x); },
    contains(x) { return this._set.has(x); },
  };
  el._sub = new Map();
  el.querySelector = (sel) => {
    if (!el._sub.has(sel)) el._sub.set(sel, makeRichEl("span"));
    return el._sub.get(sel);
  };
  return el;
}

function installDomFactory() {
  const origCreate = document.createElement;
  const origFrag = document.createDocumentFragment;
  document.createElement = (tag) => makeRichEl(tag);
  document.createDocumentFragment = () => makeRichEl("fragment");
  return () => {
    document.createElement = origCreate;
    document.createDocumentFragment = origFrag;
  };
}

function makeFakeKconfigHost() {
  const mk = makeRichEl;

  const els = {
    ".kconfig-view-container": mk("div"),
    "#kconfig-arch-select": mk("select"),
    "#btn-toggle-engine": mk("button"),
    "#btn-rename-config": mk("button"),
    "#btn-duplicate-config": mk("button"),
    "#btn-toolchain": mk("button"),
    "#btn-open-tui": mk("button"),
    "#btn-export-config": mk("button"),
    "#btn-load-defconfig": mk("button"),
    "#kconfig-import-file": mk("input"),
    "#btn-import-config": mk("button"),
    "#btn-validate-config": mk("button"),
    "#btn-compare-config": mk("button"),
    "#btn-kconfig-fragments": mk("button"),
    "#btn-kconfig-hardware": mk("button"),
    "#btn-kconfig-newopts": mk("button"),
    "#kconfig-search-input": mk("input"),
    "#btn-kconfig-expand": mk("button"),
    "#btn-kconfig-collapse": mk("button"),
    "#kconfig-status": mk("div"),
    "#kconfig-new-banner": mk("div"),
    "#kconfig-tree-scroll": mk("div"),
    "#kconfig-detail-col": mk("div"),
  };
  const containerEl = {
    isConnected: true,
    _html: "",
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    querySelector: (sel) => els[sel] || null,
    querySelectorAll: () => [],
  };
  return { containerEl, els };
}

test("KconfigView.autosolve routes multi-symbol mutations through previewOrApply and leaves engine untouched on cancel", async () => {
  const restoreDom = installDomFactory();
  const { containerEl } = makeFakeKconfigHost();
  const view = new KconfigView();
  view.containerEl = containerEl;
  view.tabId = "tab-kc-1";
  view.currentVersion = "v3.0";
  view.currentArch = "x86_64";
  state.panes = [{ id: "pane-1", activeTabId: "tab-kc-1", tabs: [{ id: "tab-kc-1", type: "kconfig", arch: "x86_64", version: "v3.0" }] }];
  state.setKconfigEngineEnabled("tab-kc-1", true);
  view.engine.setModel(RAW);
  view.engine.loadConfig("CONFIG_MODULES=y\n# CONFIG_VIRT is not set\n");

  view.api = {
    async autosolveKconfig() {
      return {
        toggles_needed: [
          { symbol: "VIRT", set_to: "y" },
          { symbol: "KVM", set_to: "y" },
        ],
      };
    },
  };

  let capturedPreview = null;
  let confirm = false;
  view.previewOrApply = async (opts) => {
    capturedPreview = opts;
    if (confirm) {
      await opts.apply();
      return { applied: true, shown: true };
    }
    return { applied: false, shown: true };
  };

  try {
    // 1. User cancels preview -> engine remains unchanged
    await view.autosolve("KVM");
    assert.ok(capturedPreview, "previewOrApply must be called");
    assert.ok(capturedPreview.sections.length >= 1);
    const allNames = capturedPreview.sections.flatMap((s) => s.rows.map((r) => r.name));
    assert.ok(allNames.includes("KVM"));
    assert.ok(allNames.includes("VIRT"));
    assert.equal(view.engine.value("VIRT"), "n");
    assert.equal(view.engine.value("KVM"), "n");

    // 2. User confirms preview -> apply callback mutates engine
    confirm = true;
    await view.autosolve("KVM");
    assert.equal(view.engine.value("VIRT"), "y");
    assert.equal(view.engine.value("KVM"), "y");
  } finally {
    state.setKconfigEngineEnabled("tab-kc-1", false);
    view.dispose();
    restoreDom();
  }
});

test("KconfigView.dispose hides MenuconfigTui (removing window keydown listener), cancels timers, and bumps seq tokens", async () => {
  const restoreDom = installDomFactory();
  const { containerEl, els } = makeFakeKconfigHost();
  const listeners = new Set();
  const origAdd = window.addEventListener;
  const origRemove = window.removeEventListener;
  window.addEventListener = (type, fn, opts) => {
    if (type === "keydown") listeners.add({ fn, opts });
    return origAdd?.call(window, type, fn, opts);
  };
  window.removeEventListener = (type, fn, opts) => {
    if (type === "keydown") {
      for (const entry of listeners) {
        if (entry.fn === fn && Boolean(entry.opts) === Boolean(opts)) listeners.delete(entry);
      }
    }
    return origRemove?.call(window, type, fn, opts);
  };
  const fireKeydown = (ev) => {
    for (const entry of [...listeners]) entry.fn(ev);
  };
  const view = new KconfigView();
  view.api = {
    async getKconfigPresets() {
      return { targets: [{ id: "x86_64", srcarch: "x86", label: "x86_64" }], default_arch: "x86_64" };
    },
    async getKconfigTree() {
      return RAW;
    },
    async getKconfigDefconfigContent() {
      return { name: "defconfig", content: "CONFIG_NET=y\n" };
    },
  };
  try {
    await view.render(containerEl, { id: "tab-kc-2", type: "kconfig", arch: "x86_64", version: "v3.0" });

    // Open TUI via button
    els["#btn-open-tui"].onclick();
    assert.ok(view.tui instanceof MenuconfigTui);
    const tui = view.tui;
    assert.ok(typeof tui.keyHandler === "function");
    assert.equal(listeners.size, 1);
    tui.helpOpen = true;

    // Trigger debounced search timer
    els["#kconfig-search-input"].value = "NET";
    els["#kconfig-search-input"].oninput();
    assert.ok(view._searchTimer !== null);

    let enterCalls = 0;
    tui.handleEnter = () => { enterCalls++; };
    tui.helpOpen = false;
    fireKeydown({ type: "keydown", key: "Enter", preventDefault() {}, stopPropagation() {} });
    assert.equal(enterCalls, 1);

    const prevArchSeq = view._archSeq;
    const prevDetailToken = view.detailToken;
    view.dispose();

    assert.equal(view.tui, null);
    assert.equal(view.containerEl, null);
    assert.equal(view._searchTimer, null);
    assert.equal(tui.keyHandler, null);
    assert.equal(tui.helpOpen, false);
    assert.equal(listeners.size, 0);
    assert.ok(view._archSeq > prevArchSeq);
    assert.ok(view.detailToken > prevDetailToken);

    // Keydown after dispose must not reach the closed TUI
    fireKeydown({ type: "keydown", key: "Enter", preventDefault() {}, stopPropagation() {} });
    assert.equal(enterCalls, 1);
  } finally {
    window.addEventListener = origAdd;
    window.removeEventListener = origRemove;
    view.dispose();
    restoreDom();
  }
});

test("KconfigView.render fallback arch preserves archChanged transition and fillRemoteDetail escapes implied_by", async () => {
  const restoreDom = installDomFactory();
  const { containerEl } = makeFakeKconfigHost();
  const view = new KconfigView();
  view.expandedNodeIds.add(1);
  view.selectedSymbol = "OLD_SYM";

  let seenArchChanged = null;
  const origActivate = view.activateArch.bind(view);
  view.activateArch = async (arch) => {
    seenArchChanged = view.currentArch !== arch;
    return origActivate(arch);
  };
  view.api = {
    async getKconfigPresets() {
      return { targets: [{ id: "arm64", srcarch: "arm64", label: "arm64" }], default_arch: "arm64" };
    },
    async getKconfigTree() {
      return RAW;
    },
    async getKconfigDefconfigContent() {
      return { name: "defconfig", content: "" };
    },
    async getKconfigSymbol() {
      return {
        defined_for_arch: true,
        help: "Help text",
        compiled_files: [],
        kbuild: [],
        implied_by: [
          "NET",
          { symbol: "BAD<script>", cond: "FOO<img onerror=alert(1)>" },
        ],
      };
    },
  };

  try {
    await view.render(containerEl, { id: "tab-kc-3", type: "kconfig", arch: "x86_64", version: "v3.0" });
    assert.equal(seenArchChanged, true);
    assert.equal(view.currentArch, "arm64");
    assert.equal(view.expandedNodeIds.size, 0);

    const box = makeRichEl("div");
    const detailCol = { querySelector: (sel) => (sel === "#kconfig-detail-remote" ? box : null) };
    await view.fillRemoteDetail("INET", view.detailToken, detailCol);
    assert.match(box.innerHTML, /Implied by \(2\)/);
    assert.match(box.innerHTML, /NET/);
    assert.doesNotMatch(box.innerHTML, /<script>/);
    assert.doesNotMatch(box.innerHTML, /<img /);
  } finally {
    view.dispose();
    restoreDom();
  }
});

test("MenuconfigTui.showHelp guards against disconnected modalEl after await", async () => {
  const restoreDom = installDomFactory();
  const { containerEl } = makeFakeKconfigHost();
  const view = new KconfigView();
  view.containerEl = containerEl;
  view.engine.setModel(RAW);
  let resolveHelp;
  view.api = {
    getKconfigSymbol: () => new Promise((r) => { resolveHelp = r; }),
  };

  const tui = new MenuconfigTui(view);
  try {
    tui.show();
    const helpEl = tui.modalEl.querySelector("#tui-help");
    const p = tui.showHelp();
    tui.modalEl.isConnected = false;
    resolveHelp({ help: "Should not paint" });
    await p;
    assert.doesNotMatch(helpEl.innerHTML, /Should not paint/);
  } finally {
    tui.hide();
    view.dispose();
    restoreDom();
  }
});
