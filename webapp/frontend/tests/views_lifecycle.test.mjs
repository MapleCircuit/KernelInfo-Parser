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

const { DiffView } = await import("../js/views/diff/diff_view.js");
const { MailView } = await import("../js/views/mail/mail_view.js");
const { MaintainersView } = await import("../js/views/maintainers/maintainers_view.js");
const { ExplorerView } = await import("../js/views/explorer/explorer_view.js");
const { CallgraphView } = await import("../js/views/callgraph/callgraph_view.js");
const { PatchSeriesView } = await import("../js/views/patch/patch_series_view.js");
const { PaholeView } = await import("../js/views/pahole/pahole_view.js");
const { BookmarksView } = await import("../js/views/bookmarks/bookmarks_view.js");

function makeBox(extra = {}) {
  return {
    isConnected: true,
    innerHTML: "",
    hidden: false,
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {},
    remove() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    ...extra,
  };
}

test("DiffView dispose aborts, cancels debouncers, removes download listener, and toggleFileDiff caps at 800 lines", async () => {
  const view = new DiffView();
  let aborted = false;
  let filterCancelled = false;
  view.apiAbort = { abort() { aborted = true; } };
  view._debouncedFilter = { cancel() { filterCancelled = true; } };
  view.containerEl = makeBox();

  // wireDownloadMenu replaces previous mousedown listener and dispose removes it
  const listeners = new Set();
  const origAdd = document.addEventListener.bind(document);
  const origRem = document.removeEventListener.bind(document);
  document.addEventListener = (t, fn, c) => { if (t === "mousedown") listeners.add(fn); origAdd(t, fn, c); };
  document.removeEventListener = (t, fn, c) => { if (t === "mousedown") listeners.delete(fn); origRem(t, fn, c); };
  try {
    const dlMenu = makeBox({ hidden: true, parentElement: makeBox() });
    const dlBtn = makeBox();
    const resContainer = makeBox({
      querySelector: (s) => (s === "#cmp-download-menu" ? dlMenu : s === "#cmp-download" ? dlBtn : null),
    });
    view.wireDownloadMenu(resContainer, () => {});
    dlBtn.onclick();
    assert.equal(listeners.size, 1);
    view.wireDownloadMenu(resContainer, () => {});
    assert.equal(listeners.size, 0);
    dlBtn.onclick();
    assert.equal(listeners.size, 1);

    // toggleFileDiff caps at 800 lines with Show all button
    const bigDiff = Array.from({ length: 900 }, (_, i) => `+line ${i}`).join("\n");
    view.api = { getFileDiff: async () => ({ additions: 900, deletions: 0, diff: bigDiff }) };
    let appendedBox = null;
    const contentNode = makeBox();
    const showAllBtn = makeBox();
    const origCreate = document.createElement.bind(document);
    document.createElement = (tag) => {
      const el = origCreate(tag);
      el.querySelector = (s) => {
        if (s === ".compare-diff-show-all") return showAllBtn;
        if (s === ".diff-content") return contentNode;
        return null;
      };
      return el;
    };
    try {
      const rowEl = makeBox({
        appendChild(b) { appendedBox = b; },
        contains(b) { return b === appendedBox; },
      });
      await view.toggleFileDiff(rowEl, "fs/test.c");
      assert.ok(appendedBox.innerHTML.includes("Show all 900 lines"));
      assert.ok(!appendedBox.innerHTML.includes("+line 850"));
      showAllBtn.onclick({ stopPropagation() {} });
      assert.ok(contentNode.innerHTML.includes("+line 850"));
    } finally {
      document.createElement = origCreate;
    }

    const prevSeq = view.loadSeq;
    view.dispose();
    assert.equal(aborted, true);
    assert.equal(filterCancelled, true);
    assert.equal(listeners.size, 0);
    assert.equal(view.loadSeq, prevSeq + 1);
    assert.equal(view.containerEl, null);
  } finally {
    document.addEventListener = origAdd;
    document.removeEventListener = origRem;
  }
});

test("MailView dispose, safeHttpUrl lore links, and stale loadSearch guard", async () => {
  const view = new MailView();
  let cancelled = false;
  view.debouncedSearch = { cancel() { cancelled = true; } };
  const listBox = makeBox();
  view.containerEl = makeBox({
    querySelector: (s) => (s === "#mail-results-container" ? listBox : null),
  });
  let resolveSearch;
  view.api = {
    searchMailingList: () => new Promise((r) => { resolveSearch = r; }),
  };

  const p = view.loadSearch(true);
  listBox.innerHTML = "KEEP";
  view.dispose();
  assert.equal(cancelled, true);
  assert.equal(view.containerEl, null);
  resolveSearch({ total: 1, results: [{ subject: "hi" }] });
  await p;
  assert.equal(listBox.innerHTML, "KEEP");

  // renderThreadView & renderCardContent sanitize lore_url
  const view2 = new MailView();
  const threadBox = makeBox();
  view2.renderThreadView(threadBox, { tid: 1, subject: "T", lore_url: "javascript:alert(1)", messages: [] });
  assert.ok(!threadBox.innerHTML.includes("javascript:"));

  const cardBody = makeBox();
  view2.renderCardContent(cardBody, { available: false, lore_url: "javascript:alert(1)" }, false);
  assert.ok(!cardBody.innerHTML.includes("javascript:"));
});

test("MaintainersView safeHttpUrl, dispose, and stale async guards", async () => {
  const view = new MaintainersView();
  let cancelled = false;
  view.debouncedSearch = { cancel() { cancelled = true; } };
  const detailBox = makeBox();
  view.containerEl = makeBox({
    querySelector: (s) => (s === "#m-detail-container" ? detailBox : null),
  });
  view.api = {
    getMaintainerSection: async () => ({
      section: {
        name: "TEST",
        web_page: "javascript:alert(1)",
        patchwork: "https://patchwork.kernel.org/x/",
        bug_url: "data:text/html,bad",
        files: [{ fname: "kernel/<script>.c" }],
      },
    }),
  };
  await view.inspectSection(1);
  assert.ok(!detailBox.innerHTML.includes('href="javascript:'));
  assert.ok(!detailBox.innerHTML.includes('href="data:'));
  assert.match(detailBox.innerHTML, /href="https:\/\/patchwork\.kernel\.org\/x\/"[^>]*rel="noopener noreferrer"/);
  assert.ok(detailBox.innerHTML.includes("kernel/&lt;script&gt;.c"));

  view.dispose();
  assert.equal(cancelled, true);
  assert.equal(view.containerEl, null);
});

test("ExplorerView, CallgraphView, PatchSeriesView, PaholeView, and BookmarksView dispose and guard stale updates", async () => {
  // ExplorerView
  const ex = new ExplorerView();
  const listing = makeBox();
  ex.containerEl = makeBox({ querySelector: (s) => (s === "#explorer-listing" ? listing : null) });
  let resolveTree;
  ex.api = { getDirectoryTree: () => new Promise((r) => { resolveTree = r; }) };
  const exP = ex.load();
  listing.innerHTML = "KEEP";
  ex.dispose();
  resolveTree({ entries: [{ name: "a.c", type: "file" }] });
  await exP;
  assert.equal(listing.innerHTML, "KEEP");
  assert.equal(ex.containerEl, null);

  // CallgraphView
  const cg = new CallgraphView();
  const cgRes = makeBox();
  cg.containerEl = makeBox({ querySelector: (s) => (s === "#callgraph-results" ? cgRes : null) });
  let resolveCg;
  cg.api = { getCallgraph: () => new Promise((r) => { resolveCg = r; }) };
  const cgP = cg.loadCallgraph();
  cgRes.innerHTML = "KEEP";
  cg.dispose();
  resolveCg({ callers: [{ name: "x" }], callees: [] });
  await cgP;
  assert.equal(cgRes.innerHTML, "KEEP");
  assert.equal(cg.containerEl, null);

  // PatchSeriesView
  const ps = new PatchSeriesView();
  let unsubbed = false;
  ps.unsub = () => { unsubbed = true; };
  ps.containerEl = makeBox();
  const seqBefore = ps._seq;
  ps.dispose();
  assert.equal(unsubbed, true);
  assert.equal(ps._seq, seqBefore + 1);
  assert.equal(ps.containerEl, null);

  // PaholeView
  const pv = new PaholeView();
  const pvRes = makeBox();
  pv.containerEl = makeBox({ querySelector: (s) => (s === "#pahole-results-container" ? pvRes : null) });
  let resolvePv;
  pv.api = { getPaholeLayout: () => new Promise((r) => { resolvePv = r; }) };
  const pvP = pv.loadLayout();
  pvRes.innerHTML = "KEEP";
  pv.dispose();
  resolvePv({ members: [] });
  await pvP;
  assert.equal(pvRes.innerHTML, "KEEP");
  assert.equal(pv.containerEl, null);

  // BookmarksView
  const bv = new BookmarksView();
  let bmUnsubbed = false;
  bv.unsub = () => { bmUnsubbed = true; };
  bv.containerEl = makeBox();
  const bvSeq = bv._seq;
  bv.dispose();
  assert.equal(bmUnsubbed, true);
  assert.equal(bv._seq, bvSeq + 1);
  assert.equal(bv.containerEl, null);
});
