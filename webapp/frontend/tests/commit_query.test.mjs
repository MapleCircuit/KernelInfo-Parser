import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

// Module singletons start heartbeats when commits_view.js imports state.js: keep them unreffed
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

const {
  QUERY_EXAMPLES,
  QUERY_HELP_GROUPS,
  QUERY_KEYS,
  completeKey,
  isStructured,
  parseCommitQuery,
  queryHelpHtml,
  queryParams,
  removeToken,
  tokenize,
} = await import("../js/views/commits/commit_query.js");
const { CommitsView } = await import("../js/views/commits/commits_view.js");
const { contextMenu } = await import("../js/components/context_menu.js");
const { state } = await import("../js/state.js");
const { urlSync } = await import("../js/url_sync.js");

test("tokens: quotes, keys, plain words", () => {
  assert.deepEqual(tokenize('path:fs/ext4 reviewer:"Jan Kara" journal').map((t) => [t.key, t.value]),
    [["path", "fs/ext4"], ["reviewer", "Jan Kara"], ["", "journal"]]);
  assert.deepEqual(tokenize('"two words" x').map((t) => t.value), ["two words", "x"]);
});

test("parse: chips, repeated keys, unknown keys stay text", () => {
  const q = parseCommitQuery('path:fs/ext4 path:*.h reviewer:"Jan Kara" since:2011-01-01 in:v3.0 merges:none foo:bar journal');
  assert.deepEqual(q.paths, ["fs/ext4", "*.h"]);
  assert.deepEqual(q.roles, ["reviewer:Jan Kara"]);
  assert.equal(q.since, "2011-01-01");
  assert.equal(q.release, "v3.0");
  assert.equal(q.merges, "none");
  assert.equal(q.text, "foo:bar journal");
  assert.equal(q.chips.length, 6);
  assert.equal(isStructured("just text"), false);
  assert.equal(isStructured("http://x journal"), false);
  assert.equal(isStructured("author:ann"), true);
});

test("request parameters and chip removal", () => {
  const q = parseCommitQuery("path:fs/ext4 in:v3.0 merges:none journal");
  assert.equal(queryParams(q, 2, 50).toString(), "path=fs%2Fext4&release=v3.0&merges=none&text=journal&page=2&limit=50");
  const text = 'path:fs/ext4 reviewer:"Jan Kara" journal';
  const chip = parseCommitQuery(text).chips.find((c) => c.key === "reviewer");
  assert.equal(removeToken(text, chip.index), "path:fs/ext4 journal");
  assert.deepEqual(completeKey("rev"), ["reviewer:"]);
  assert.deepEqual(completeKey("path:x"), []);
});

test("query help metadata covers every query key and valid structured examples", () => {
  const documentedKeys = QUERY_HELP_GROUPS.flatMap((g) => g.items.map((it) => it.key));
  assert.deepEqual([...documentedKeys].sort(), [...QUERY_KEYS].sort());
  for (const ex of QUERY_EXAMPLES) {
    assert.equal(isStructured(ex.query), true, `example should be structured: ${ex.query}`);
  }
});

test("queryHelpHtml renders modes, keys, examples, and escapes selected commit label", () => {
  const closedHtml = queryHelpHtml({ hasSelectedCommit: false });
  assert.match(closedHtml, /id="commit-query-help-panel"/);
  assert.match(closedHtml, /Search Modes/);
  assert.match(closedHtml, /Structured Filter Keys/);
  assert.match(closedHtml, /Example Queries/);
  assert.match(closedHtml, />✕ Close</);
  for (const k of QUERY_KEYS) {
    assert.ok(closedHtml.includes(`data-insert-key="${k}:"`), `missing key button for ${k}`);
  }

  const backHtml = queryHelpHtml({ hasSelectedCommit: true, selectedCommitLabel: "<1234abcd>" });
  assert.match(backHtml, /← Back to commit &lt;1234abcd&gt;/);
});

function makeViewHarness() {
  const mkEl = () => {
    const classes = new Set();
    const attrs = new Map();
    return {
      isConnected: true,
      innerHTML: "",
      value: "",
      hidden: false,
      scrollTop: 0,
      focused: false,
      selection: null,
      classList: {
        toggle(c, force) {
          const on = force === undefined ? !classes.has(c) : Boolean(force);
          if (on) classes.add(c);
          else classes.delete(c);
          return on;
        },
        contains: (c) => classes.has(c),
      },
      setAttribute: (k, v) => attrs.set(k, String(v)),
      getAttribute: (k) => attrs.get(k) ?? null,
      addEventListener() {},
      focus() { this.focused = true; },
      setSelectionRange(a, b) { this.selection = [a, b]; },
    };
  };
  const elements = {
    "#commit-detail-container": mkEl(),
    "#btn-commit-query-help": mkEl(),
    "#commit-search-input": mkEl(),
    "#commit-query-chips": mkEl(),
    "#commit-list-container": mkEl(),
    datalist: mkEl(),
  };
  const view = new CommitsView();
  view.containerEl = {
    isConnected: true,
    querySelector: (sel) => elements[sel] || (sel.startsWith("#commit-query-keys-") ? elements.datalist : null),
  };
  return { view, elements };
}

test("CommitsView toggles in-pane query help, aborts pending commit facts, and supports insert/example", () => {
  const { view, elements } = makeViewHarness();
  let aborted = false;
  view.factsAbort = { abort() { aborted = true; } };
  view._detailToken = 5;

  // Toggle open without selected commit
  view.toggleQueryHelp();
  assert.equal(view.showingQueryHelp, true);
  assert.equal(aborted, true);
  assert.equal(view._detailToken, 6);
  assert.equal(elements["#btn-commit-query-help"].classList.contains("active"), true);
  assert.equal(elements["#btn-commit-query-help"].getAttribute("aria-pressed"), "true");
  assert.match(elements["#commit-detail-container"].innerHTML, /Commit Search Guide/);
  assert.match(elements["#commit-detail-container"].innerHTML, /✕ Close/);

  // Insert a key and run an example
  view.insertQueryKey("path:");
  assert.equal(elements["#commit-search-input"].value, "path:");
  assert.equal(elements["#commit-search-input"].focused, true);

  let loaded = null;
  let persisted = null;
  view.persistTabState = (p) => { persisted = p; };
  view.loadCommits = (q, reset) => { loaded = { q, reset }; };
  view.applyQueryExample("path:fs/ext4 merges:none");
  assert.equal(elements["#commit-search-input"].value, "path:fs/ext4 merges:none");
  assert.deepEqual(persisted, { query: "path:fs/ext4 merges:none" });
  assert.deepEqual(loaded, { q: "path:fs/ext4 merges:none", reset: true });
  assert.equal(elements["#commit-query-chips"].hidden, false);

  // Toggle closed restores empty placeholder
  view.toggleQueryHelp();
  assert.equal(view.showingQueryHelp, false);
  assert.equal(elements["#btn-commit-query-help"].classList.contains("active"), false);
  assert.equal(elements["#btn-commit-query-help"].getAttribute("aria-pressed"), "false");
  assert.match(elements["#commit-detail-container"].innerHTML, /Select a commit from the timeline/);

  // With a selected commit, opening help shows "Back to commit <short>" and closing re-inspects it
  view.selectedCommitId = "deadbeefcafebabe1234";
  view.toggleQueryHelp();
  assert.match(elements["#commit-detail-container"].innerHTML, /← Back to commit deadbeef/);
  let inspected = null;
  view.inspectCommit = (id) => { inspected = id; view.setQueryHelpActive(false); };
  view.hideQueryHelp();
  assert.equal(inspected, "deadbeefcafebabe1234");
  assert.equal(view.showingQueryHelp, false);
});

test("contextMenu.renderMenu survives an opening click bubbling to document", () => {
  contextMenu.renderMenu(10, 20, { header: { title: "Test" }, items: [{ label: "One" }] });
  assert.ok(contextMenu.menuEl, "menu should exist immediately after renderMenu");
  document.dispatchEvent({ type: "click" });
  assert.ok(contextMenu.menuEl, "same-tick bubbling click must not dismiss newly opened menu");
  contextMenu.hide();
});

test("commit query help participates in browser Back/Forward history round-trips", async () => {
  const pushed = [];
  const replaced = [];
  const origPush = globalThis.history.pushState;
  const origReplace = globalThis.history.replaceState;
  globalThis.history.pushState = (st, title, url) => {
    globalThis.history.state = st;
    globalThis.location.hash = url;
    pushed.push({ st, url });
  };
  globalThis.history.replaceState = (st, title, url) => {
    globalThis.history.state = st;
    globalThis.location.hash = url;
    replaced.push({ st, url });
  };

  try {
    state.panes = [{ id: "pane-1", activeTabId: null, tabs: [] }];
    state.activePaneId = "pane-1";
    urlSync.lastCommittedSnapshot = null;
    urlSync.deferred = false;
    urlSync.init();

    const commitHash = "deadbeefcafebabe1234567890abcdef12345678";
    state.openTab({ type: "commits", title: "Commits", version: "v3.0", commitId: commitHash });
    const tab = state.getActiveTab();
    assert.ok(tab);
    assert.match(globalThis.location.hash, new RegExp(`cid=${commitHash}`));
    assert.ok(!globalThis.location.hash.includes("help=1"));

    pushed.length = 0;
    const { view, elements } = makeViewHarness();
    view.tabId = tab.id;
    view.selectedCommitId = commitHash;

    // 1. Opening Search Guide pushes a history entry with help=1
    view.toggleQueryHelp();
    assert.equal(tab.queryHelp, true);
    assert.equal(pushed.length, 1);
    assert.match(pushed[0].url, /help=1/);
    assert.match(pushed[0].url, new RegExp(`cid=${commitHash}`));
    const helpSnapshot = pushed[0].st;

    // Build a snapshot for the prior commit state (before clicking ?)
    urlSync.handlePopState({ state: { ...helpSnapshot, tabs: helpSnapshot.tabs.map((t) => ({ ...t, queryHelp: undefined })), navKey: urlSync.getNavKey({ ...tab, queryHelp: undefined }) } });
    assert.equal(state.getActiveTab().queryHelp, undefined);
    assert.equal(state.getActiveTab().commitId, commitHash);

    // 2. Going Forward restores queryHelp=true and CommitsView.render() renders the help panel
    urlSync.handlePopState({ state: helpSnapshot });
    assert.equal(state.getActiveTab().queryHelp, true);

    const restoredView = new CommitsView();
    let inspectedOnForward = false;
    restoredView.inspectCommit = async () => { inspectedOnForward = true; };
    restoredView.loadCommits = async () => {};
    const forwardContainer = {
      isConnected: true,
      innerHTML: "",
      dataset: {},
      querySelector: (sel) => {
        if (sel === "#commit-detail-container") return elements["#commit-detail-container"];
        if (sel === "#btn-commit-query-help") return elements["#btn-commit-query-help"];
        if (sel === "#commit-search-input") return elements["#commit-search-input"];
        if (sel === "#commit-query-chips") return elements["#commit-query-chips"];
        if (sel === "#commit-list-container") return elements["#commit-list-container"];
        return { onclick: null, addEventListener() {}, value: "" };
      },
      addEventListener() {},
    };
    elements["#commit-detail-container"].innerHTML = "";
    await restoredView.render(forwardContainer, state.getActiveTab());
    assert.equal(inspectedOnForward, false, "restoring help=1 must not overwrite the help panel with commit detail");
    assert.equal(restoredView.showingQueryHelp, true);
    assert.match(elements["#commit-detail-container"].innerHTML, /Commit Search Guide/);

    // 3. Closing Search Guide via hideQueryHelp() pushes a non-help history entry so Back returns to help=1
    pushed.length = 0;
    restoredView.hideQueryHelp();
    assert.equal(state.getActiveTab().queryHelp, undefined);
    assert.equal(pushed.length, 1);
    assert.ok(!pushed[0].url.includes("help=1"));
    assert.equal(inspectedOnForward, true);

    // 4. applyHash parses help=1 and absent help
    urlSync.applyHash(`v=v3.0&tab=commits&cid=${commitHash}&help=1`);
    assert.equal(state.getActiveTab().queryHelp, true);
    urlSync.applyHash(`v=v3.0&tab=commits&cid=${commitHash}`);
    assert.equal(state.getActiveTab().queryHelp, undefined);
  } finally {
    globalThis.history.pushState = origPush;
    globalThis.history.replaceState = origReplace;
  }
});
