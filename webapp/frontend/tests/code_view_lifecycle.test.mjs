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

const { VirtualEditor } = await import("../js/views/code_view/virtual_editor.js");
const { showDiffPicker } = await import("../js/views/code_view/file_diff.js");
const registry = await import("../js/views/code_view/syntax/registry.js");
const { state } = await import("../js/state.js");

test("syntax registry does not export unused ALL_GRAMMARS", () => {
  assert.equal("ALL_GRAMMARS" in registry, false);
});

test("showDiffPicker aborts cleanly if anchorEl is disconnected while awaiting previousTag", async () => {
  const appended = [];
  const origAppend = document.body.appendChild.bind(document.body);
  document.body.appendChild = (el) => { appended.push(el); return origAppend(el); };
  try {
    const anchor = { isConnected: true };
    const p = showDiffPicker(anchor, {
      tabId: "t1",
      version: "v3.0",
      path: "init/main.c",
      onPick() {},
      client: {
        getVersionsCached: async () => {
          anchor.isConnected = false;
          return { versions: [{ vname: "v2.6.39", ref_type: 1 }, { vname: "v3.0", ref_type: 1 }] };
        },
      },
    });
    await p;
    assert.equal(appended.length, 0);
  } finally {
    document.body.appendChild = origAppend;
  }
});

function makeEditorContainer() {
  const makeNode = (tag = "div") => ({
    tagName: tag,
    isConnected: true,
    innerHTML: "",
    textContent: "",
    hidden: false,
    style: {},
    dataset: {},
    clientWidth: 800,
    clientHeight: 600,
    offsetWidth: 80,
    scrollWidth: 100,
    scrollTop: 0,
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    setAttribute() {},
    addEventListener() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    closest() { return null; },
    parentElement: {
      clientWidth: 90,
      clientHeight: 600,
      addEventListener() {},
      getBoundingClientRect: () => ({ top: 0, left: 0, width: 90, height: 600 }),
    },
    getBoundingClientRect: () => ({ top: 0, left: 0, right: 100, bottom: 20, width: 100, height: 20 }),
  });

  const nodes = {
    "#editor-viewport": makeNode("div"),
    "#editor-spacer": makeNode("div"),
    "#editor-lines": makeNode("div"),
    "#minimap-canvas": { ...makeNode("canvas"), getContext: () => ({ clearRect() {}, fillRect() {} }) },
    "#kconfig-dim-chip": makeNode("button"),
    "#btn-kconfig-engine": makeNode("button"),
    "#btn-file-diff": makeNode("button"),
    "#btn-toggle-blame": makeNode("button"),
    "#btn-outline": makeNode("button"),
    "#btn-imports": makeNode("button"),
    "#btn-file-refs": makeNode("button"),
    "#btn-copy-link": makeNode("button"),
    "#btn-blame-opts": makeNode("button"),
    "#btn-copy-raw": makeNode("button"),
    "#btn-create-patch": makeNode("button"),
    "#btn-file-history": makeNode("button"),
    "#btn-code-overflow": makeNode("button"),
    "#file-last-changed": makeNode("span"),
    "#file-security-badge": makeNode("span"),
    "#file-subsystem": makeNode("span"),
    ".code-toolbar": makeNode("div"),
    ".code-actions": makeNode("div"),
    ".code-breadcrumbs": makeNode("div"),
    ".editor-body": makeNode("div"),
    "#doc-viewport": makeNode("div"),
  };
  nodes["#editor-viewport"].closest = () => container;
  const container = {
    isConnected: true,
    innerHTML: "",
    querySelector: (sel) => nodes[sel] || null,
    querySelectorAll: () => [],
  };
  return { container, nodes };
}

test("VirtualEditor escapes diffMarks.commit, tracks font-changed, and guards stale load/blame after dispose", async () => {
  const ed = new VirtualEditor();
  const { container, nodes } = makeEditorContainer();

  let resolveContent;
  let resolveBlame;
  let resolveProjects;
  ed.api = {
    repo: "linux",
    versionStamps: {},
    getFileContent: () => new Promise((r) => { resolveContent = r; }),
    getProjects: () => new Promise((r) => { resolveProjects = r; }),
    getFileCves: async () => ({ total: 0 }),
    matchMaintainers: async () => ({ subsystems: [] }),
    getOutline: async () => ({ items: [], indexed: false }),
    getBuildConditions: async () => ({ files: {} }),
  };
  ed.blameView = {
    attachListeners() {},
    loadBlame: () => new Promise((r) => { resolveBlame = r; }),
  };

  // Start render with XSS commit string in diffMarks
  const renderP = ed.render(container, {
    id: "tab-ve-1",
    path: "init/main.c",
    version: "v3.0",
    diffMarks: { kind: "add", commit: 'abc"><img src=x onerror=alert(1)>', ranges: [[1, 1]] },
  });

  // Verify diffMarks.commit was escaped in toolbar HTML
  assert.ok(!container.innerHTML.includes("<img src=x"), "diffMarks.commit must be escaped");
  assert.ok(container.innerHTML.includes("&lt;img src=x"), "escaped diffMarks.commit present");

  // Verify codeview:font-changed is tracked in unsubscribers (4 subscriptions: font-changed + 3 kconfig/dim)
  assert.equal(ed.unsubscribers.length, 4);

  // Trigger setBlame while disposed mid-flight
  const blameP = ed.setBlame(true, { persist: false });
  const linesEl = nodes["#editor-lines"];
  linesEl.innerHTML = "SENTINEL";

  ed.dispose();
  assert.equal(ed.viewportEl, null);
  assert.equal(ed.linesContainer, null);
  assert.equal(ed.unsubscribers.length, 0);

  // Resolve in-flight promises after dispose: must not overwrite or throw
  await Promise.resolve();
  resolveProjects?.({ projects: [{ repo: "linux", git_url: "https://git.kernel.org" }] });
  resolveContent?.({ content: "int main(void) { return 0; }", ftype: 1 });
  resolveBlame?.();
  await Promise.all([renderP, blameP]);

  assert.equal(linesEl.innerHTML, "SENTINEL");
  assert.equal(ed.repoGitUrl, "");

  // Also test font-changed emission after dispose does nothing
  state.emit("codeview:font-changed");
});
