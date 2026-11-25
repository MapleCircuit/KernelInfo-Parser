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

const { state } = await import("../js/state.js");
const { localDB } = await import("../js/db.js");
const { contextMenu } = await import("../js/components/context_menu.js");
const { toast } = await import("../js/components/toast.js");

const fakeStore = new Map();
localDB.get = async (store, key) => fakeStore.get(`${store}:${key}`) ?? null;
localDB.set = async (store, key, value) => { fakeStore.set(`${store}:${key}`, value); };

test("contextMenu identifies Makefile CONFIG_* tokens and provides declaration jump in show(), showEditorMenu(), and executeDefaultAction()", async () => {
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
    assert.equal(contextMenu.isKconfigToken({ name: "CONFIG_FOO" }, "drivers/net/Makefile"), true);
    assert.equal(contextMenu.isKconfigToken({ name: "CONFIG_64BIT" }, "arch/x86/Kbuild"), true);
    assert.equal(contextMenu.isKconfigToken({ name: "CONFIG_SMP" }, "scripts/Makefile.build"), true);
    assert.equal(contextMenu.isKconfigToken({ name: "srctree" }, "Makefile"), false);

    fetchResponder = async () => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => ({
        name: "CONFIG_8139TOO",
        file_path: "drivers/net/ethernet/realtek/Kconfig",
        line_s: 42
      })
    });

    const tok = {
      name: "CONFIG_8139TOO",
      header: "",
      object: "",
      isInclude: false,
      isObject: false,
      ast_id: 0,
      type_id: 0,
      version: "v3.0",
      filePath: "drivers/net/ethernet/realtek/Makefile",
      line: 7
    };

    // 1. Left-click menu show()
    contextMenu.show(10, 20, tok);
    const leftMenu = renderedMenus.pop();
    const leftJump = leftMenu.items.find((i) => i.label === "Jump to Kconfig Declaration");
    const leftViewer = leftMenu.items.find((i) => i.label === "Open in Kconfig Viewer");
    assert.ok(leftJump, "left-click menu must include Jump to Kconfig Declaration");
    assert.ok(leftViewer, "left-click menu must include Open in Kconfig Viewer");
    await leftJump.onClick();
    assert.equal(openedTabs.length, 1);
    assert.deepEqual(openedTabs[0], {
      type: "code",
      title: "Kconfig",
      path: "drivers/net/ethernet/realtek/Kconfig",
      cursorLine: 42,
      version: "v3.0"
    });
    assert.ok(fetchCalls.some((c) => c.url.includes("/webapi/symbols/v3.0/detail/CONFIG_8139TOO")));
    assert.equal(toasts[toasts.length - 1][0], "success");

    // 2. Right-click editor menu showEditorMenu()
    contextMenu.showEditorMenu(10, 20, { token: tok, filePath: tok.filePath, line: 7, version: "v3.0" });
    const rightMenu = renderedMenus.pop();
    const rightJump = rightMenu.items.find((i) => i.label === "Jump to Kconfig Declaration");
    const rightViewer = rightMenu.items.find((i) => i.label === "Open in Kconfig Viewer");
    const rightHistory = rightMenu.items.find((i) => i.label === "Kconfig History Across Releases");
    assert.ok(rightJump, "right-click menu must include Jump to Kconfig Declaration");
    assert.ok(rightViewer, "right-click menu must include Open in Kconfig Viewer");
    assert.ok(rightHistory, "right-click menu must include Kconfig History Across Releases");
    await rightJump.onClick();
    assert.equal(openedTabs.length, 2);
    assert.equal(openedTabs[1].path, "drivers/net/ethernet/realtek/Kconfig");
    assert.equal(openedTabs[1].cursorLine, 42);

    // 3. executeDefaultAction() respects forceNew and falls back to Kconfig Viewer when declaration is missing
    await contextMenu.executeDefaultAction(tok, { forceNew: true });
    assert.equal(openedTabs[2].type, "code");
    assert.equal(openedTabs[2].forceNew, true);

    await contextMenu.executeDefaultAction(tok, { forceNew: false });
    assert.equal(openedTabs[3].type, "code");
    assert.equal(openedTabs[3].forceNew, false);

    fakeStore.clear();
    fetchResponder = async () => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => ({ name: "CONFIG_MISSING", file_path: null })
    });

    // Right-click menu shows info toast when declaration cannot be located
    contextMenu.showEditorMenu(10, 20, { token: { ...tok, name: "CONFIG_MISSING" }, filePath: tok.filePath, line: 8, version: "v3.0" });
    const missingMenu = renderedMenus.pop();
    await missingMenu.items.find((i) => i.label === "Jump to Kconfig Declaration").onClick();
    assert.equal(toasts[toasts.length - 1][0], "info");

    // executeDefaultAction falls back to Kconfig Viewer when declaration cannot be located
    await contextMenu.executeDefaultAction({ ...tok, name: "CONFIG_MISSING" }, { forceNew: true });
    const fallbackTab = openedTabs[openedTabs.length - 1];
    assert.equal(fallbackTab.type, "kconfig");
    assert.equal(fallbackTab.symbol, "MISSING");
    assert.equal(fallbackTab.forceNew, true);
  } finally {
    state.openTab = origOpenTab;
    toast.success = origSuccess;
    toast.info = origInfo;
    toast.error = origError;
    contextMenu.renderMenu = origRenderMenu;
  }
});
