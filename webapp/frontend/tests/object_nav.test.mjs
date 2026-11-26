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

let fetchCalls = [];
let fetchResponder = async () => ({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({}) });

globalThis.fetch = async (url, opts = {}) => {
  fetchCalls.push({ url: String(url), repo: opts.headers?.["X-KI-Repo"] || "" });
  return fetchResponder(String(url), opts);
};

const { api } = await import("../js/api.js");
const { state } = await import("../js/state.js");
const { localDB } = await import("../js/db.js");
const { contextMenu } = await import("../js/components/context_menu.js");
const { toast } = await import("../js/components/toast.js");

const fakeStore = new Map();
localDB.get = async (store, key) => fakeStore.get(`${store}:${key}`) ?? null;
localDB.set = async (store, key, value) => { fakeStore.set(`${store}:${key}`, value); };

test("api.resolveObject caches per version, repo, currentFile, and objectPath", async () => {
  fakeStore.clear();
  fetchCalls = [];
  state.currentRepo = "linux";
  state.panes = [{ id: "pane-1", activeTabId: null, tabs: [] }];

  fetchResponder = async () => ({
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => ({ object: "drivers/net/sub/foo-bar.o", path: "drivers/net/sub/foo-bar.c", resolved: true }),
  });

  const first = await api.resolveObject("v3.0", "sub/foo-bar.o", "drivers/net/Makefile");
  assert.equal(first.path, "drivers/net/sub/foo-bar.c");
  assert.equal(fetchCalls.length, 1);
  assert.ok(fetchCalls[0].url.includes("/webapi/fs/resolve_object?"));
  assert.ok(fetchCalls[0].url.includes("obj=sub%2Ffoo-bar.o"));
  assert.ok(fetchCalls[0].url.includes("current_file=drivers%2Fnet%2FMakefile"));

  // Same args hit cache
  const cached = await api.resolveObject("v3.0", "sub/foo-bar.o", "drivers/net/Makefile");
  assert.equal(cached.path, "drivers/net/sub/foo-bar.c");
  assert.equal(fetchCalls.length, 1);

  // Different currentFile misses cache
  await api.resolveObject("v3.0", "sub/foo-bar.o", "drivers/usb/Makefile");
  assert.equal(fetchCalls.length, 2);

  // Different repo misses cache
  state.currentRepo = "linux-next";
  await api.resolveObject("v3.0", "sub/foo-bar.o", "drivers/net/Makefile");
  assert.equal(fetchCalls.length, 3);
  assert.equal(fetchCalls[2].repo, "linux-next");
});

test("contextMenu handles object tokens in show(), showEditorMenu(), and executeDefaultAction()", async () => {
  fakeStore.clear();
  fetchCalls = [];
  state.currentRepo = "linux";
  state.currentVersion = "v3.0";
  state.panes = [{ id: "pane-1", activeTabId: null, tabs: [] }];

  const openedTabs = [];
  const origOpenTab = state.openTab.bind(state);
  state.openTab = (cfg) => { openedTabs.push(cfg); };

  const toasts = [];
  const origSuccess = toast.success.bind(toast);
  const origInfo = toast.info.bind(toast);
  const origError = toast.error.bind(toast);
  toast.success = (m) => toasts.push(["success", m]);
  toast.info = (m) => toasts.push(["info", m]);
  toast.error = (m) => toasts.push(["error", m]);

  const renderedMenus = [];
  const origRenderMenu = contextMenu.renderMenu.bind(contextMenu);
  contextMenu.renderMenu = (_x, _y, cfg) => { renderedMenus.push(cfg); };

  try {
    fetchResponder = async () => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => ({ object: "kernel/fork.o", path: "kernel/fork.c", resolved: true }),
    });

    const tok = {
      name: "fork.o",
      object: "fork.o",
      isObject: true,
      isInclude: false,
      version: "v3.0",
      filePath: "kernel/Makefile",
      line: 12,
    };

    // 1. Left-click quick menu show()
    contextMenu.show(10, 20, tok);
    const leftMenu = renderedMenus.pop();
    assert.equal(leftMenu.header.sub, "Kbuild Object");
    const openItem = leftMenu.items.find((i) => i.label === "Open C File");
    assert.ok(openItem, "left-click menu must include Open C File");
    await openItem.onClick();
    assert.equal(openedTabs.length, 1);
    assert.deepEqual(openedTabs[0], {
      type: "code",
      title: "fork.c",
      path: "kernel/fork.c",
      cursorLine: 1,
      version: "v3.0",
    });
    assert.equal(toasts[0][0], "success");

    // 2. Right-click editor menu showEditorMenu()
    contextMenu.showEditorMenu(10, 20, { token: tok, filePath: "kernel/Makefile", line: 12, version: "v3.0" });
    const rightMenu = renderedMenus.pop();
    const rightOpenItem = rightMenu.items.find((i) => i.label === "Open C File");
    assert.ok(rightOpenItem, "right-click menu must include Open C File");
    await rightOpenItem.onClick();
    assert.equal(openedTabs.length, 2);

    // 3. executeDefaultAction() respects forceNew (middle-click vs gd)
    await contextMenu.executeDefaultAction(tok, { forceNew: true });
    assert.equal(openedTabs[2].forceNew, true);
    await contextMenu.executeDefaultAction(tok, { forceNew: false });
    assert.equal(openedTabs[3].forceNew, false);

    // 4. Unresolved object shows info toast without opening a tab
    fakeStore.clear();
    fetchResponder = async () => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => ({ object: "kernel/my_mod.o", path: null, resolved: false }),
    });
    const beforeCount = openedTabs.length;
    await contextMenu.executeDefaultAction({ ...tok, name: "my_mod.o", object: "my_mod.o" }, { forceNew: true });
    assert.equal(openedTabs.length, beforeCount);
    assert.equal(toasts[toasts.length - 1][0], "info");

    // 5. Failed request shows error toast
    fakeStore.clear();
    fetchResponder = async () => ({
      ok: false,
      status: 400,
      headers: { get: () => null },
      text: async () => JSON.stringify({ detail: "Path traversal attempt detected." }),
    });
    await contextMenu.executeDefaultAction({ ...tok, name: "bad.o", object: "bad.o" }, { forceNew: true });
    assert.equal(toasts[toasts.length - 1][0], "error");
  } finally {
    state.openTab = origOpenTab;
    toast.success = origSuccess;
    toast.info = origInfo;
    toast.error = origError;
    contextMenu.renderMenu = origRenderMenu;
  }
});
