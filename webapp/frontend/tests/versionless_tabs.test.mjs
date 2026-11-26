/**
 * versionless_tabs.test.mjs - Tests for tag-less / version-less tabs and versionless deep links
 * for commits, mailing list, CVEs, watch, bookmarks, and parser status.
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
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (...args) => {
  const t = realSetTimeout(...args);
  t?.unref?.();
  return t;
};

const requests = [];
globalThis.fetch = async (url, opts = {}) => {
  requests.push({ url: String(url), method: opts.method || "GET", repo: opts.headers?.["X-KI-Repo"] || "" });
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => ({ ok: true, commits: [], timeline: [] }),
    text: async () => "",
  };
};

const hashes = [];
globalThis.history.replaceState = (st, _t, h) => {
  globalThis.history.state = st;
  globalThis.location.hash = h;
  hashes.push(h);
};
globalThis.history.pushState = (st, _t, h) => {
  globalThis.history.state = st;
  globalThis.location.hash = h;
  hashes.push(h);
};

await import("../js/routes.js");
const { isAlwaysVersionlessTab, canBeVersionlessTab, buildDeepLink } = await import("../js/tab_routes.js");
const { state } = await import("../js/state.js");
const { urlSync } = await import("../js/url_sync.js");
const { api } = await import("../js/api.js");
const { setProjects } = await import("../js/utils/projects.js");
const { requestVersionSwitch } = await import("../js/components/version_switch.js");
const { TabsComponent } = await import("../js/components/tabs.js");
const { CommitsView } = await import("../js/views/commits/commits_view.js");
const { CveView } = await import("../js/views/cve/cve_view.js");

setProjects([
  { repo: "linux", project: "linux", name: "Linux", available: true },
  { repo: "man-pages", project: "man-pages", name: "man-pages", available: true },
]);

function resetWorkspace(tabs = [], activeTabId = tabs[0]?.id || null) {
  state.currentRepo = "linux";
  state.currentVersion = "v3.0";
  state.selectedVersion = "v3.0";
  state.splitMode = "single";
  state.panes = [{ id: "pane-1", activeTabId, tabs }];
  state.activePaneId = "pane-1";
  urlSync.lastCommittedSnapshot = null;
  urlSync.deferred = false;
  urlSync.setRepoHandler((repo) => ({ repo: repo || "linux", version: "v3.0" }));
}

const lastParams = () => new URLSearchParams(String(hashes[hashes.length - 1] || "").replace(/^#/, ""));

test("tab versionlessness classification covers always- and optionally-versionless types", () => {
  for (const t of ["mail", "cve", "watch", "bookmarks", "status"]) {
    assert.equal(isAlwaysVersionlessTab(t), true, `${t} should be always versionless`);
    assert.equal(canBeVersionlessTab(t), true, `${t} should be able to be versionless`);
  }
  assert.equal(isAlwaysVersionlessTab("commits"), false);
  assert.equal(canBeVersionlessTab("commits"), true);
  for (const t of ["code", "explorer", "kconfig", "diff", "search", "api", "insights", "man", "maintainers"]) {
    assert.equal(isAlwaysVersionlessTab(t), false);
    assert.equal(canBeVersionlessTab(t), false);
  }
});

test("state.openTab, setVersion, clearTabVersion, acceptTransferredTab, and adoptSession preserve versionless tabs", () => {
  resetWorkspace([{ id: "c1", type: "code", path: "init/main.c", version: "v3.0", repo: "linux" }]);

  // Always-versionless tabs drop any supplied version
  state.openTab({ type: "mail", title: "Mailing List", version: "v3.0", threadId: 42, messageId: "m@x" });
  const mailTab = state.getActiveTab();
  assert.equal(mailTab.version, "");

  // requestVersionSwitch and setVersion are no-ops for always-versionless tabs
  state.setVersion("v3.1", mailTab);
  assert.equal(mailTab.version, "");

  state.openTab({ type: "cve", title: "CVEs", version: "v3.0", sel: "CVE-2024-1234" });
  assert.equal(state.getActiveTab().version, "");

  // Commits opened without a version stays tag-less; commits opened with a version keeps it
  state.openTab({ type: "commits", title: "Commit abc1234", commitId: "abc1234567890", listCollapsed: true });
  const taglessCommit = state.getActiveTab();
  assert.equal(taglessCommit.version, "");

  state.openTab({ type: "commits", title: "Commits v3.0", version: "v3.0", forceNew: true });
  const versionedCommit = state.getActiveTab();
  assert.equal(versionedCommit.version, "v3.0");

  // clearTabVersion clears a commits tab's tag, but refuses a code tab
  assert.equal(state.clearTabVersion(versionedCommit), true);
  assert.equal(versionedCommit.version, "");
  const codeTab = state.findTab("c1");
  assert.equal(state.clearTabVersion(codeTab), false);
  assert.equal(codeTab.version, "v3.0");

  // acceptTransferredTab preserves versionless tabs without backfilling currentVersion
  const transferredMail = state.acceptTransferredTab({
    tabData: { id: "m-xfer", type: "mail", title: "Mail", repo: "linux", version: "" },
  });
  assert.equal(transferredMail.version, "");
  const transferredCommit = state.acceptTransferredTab({
    tabData: { id: "c-xfer", type: "commits", title: "Commit", repo: "linux", commitId: "deadbeef", version: "" },
  });
  assert.equal(transferredCommit.version, "");
  const transferredCode = state.acceptTransferredTab({
    tabData: { id: "f-xfer", type: "code", title: "a.c", path: "a.c", repo: "linux" },
  });
  assert.equal(transferredCode.version, "v3.0");

  // adoptSession preserves versionless tabs without backfilling "v3.0"
  state.adoptSession({
    id: "sess-vl",
    name: "VL",
    version: "v3.0",
    repo: "linux",
    panes: [{
      id: "pane-1",
      activeTabId: "t-mail",
      tabs: [
        { id: "t-mail", type: "mail", title: "Mailing List" },
        { id: "t-cve", type: "cve", title: "CVEs", version: "v3.0" },
        { id: "t-commit", type: "commits", title: "Commit", commitId: "deadbeef" },
        { id: "t-code", type: "code", title: "main.c", path: "init/main.c" },
      ],
    }],
  });
  assert.equal(state.findTab("t-mail").version, "");
  assert.equal(state.findTab("t-cve").version, "");
  assert.equal(state.findTab("t-commit").version, "");
  assert.equal(state.findTab("t-code").version, "v3.0");
});

test("requestVersionSwitch refuses always-versionless tabs and switches versionless commits tabs", async () => {
  resetWorkspace([
    { id: "m1", type: "mail", title: "Mailing List", repo: "linux", version: "" },
    { id: "cm1", type: "commits", title: "Commits", repo: "linux", version: "" },
  ], "m1");
  const mailTab = state.findTab("m1");
  const commitsTab = state.findTab("cm1");

  assert.equal(await requestVersionSwitch("v3.1", mailTab), false);
  assert.equal(mailTab.version, "");

  assert.equal(await requestVersionSwitch("v3.1", commitsTab), true);
  assert.equal(commitsTab.version, "v3.1");
});

test("urlSync omits v= for versionless commit, mail, CVE, watch, bookmarks, and status links", () => {
  resetWorkspace([{ id: "c1", type: "code", path: "init/main.c", version: "v3.0", repo: "linux" }]);
  urlSync.init();

  const sha = "0123456789abcdef0123456789abcdef01234567";

  // 1. Commit without version
  urlSync.applyHash(`repo=linux&tab=commits&cid=${sha}&lc=1`);
  let tab = state.getActiveTab();
  assert.equal(tab.type, "commits");
  assert.equal(tab.commitId, sha);
  assert.equal(tab.version, "");
  let p = lastParams();
  assert.equal(p.get("v"), null);
  assert.equal(p.get("tab"), "commits");
  assert.equal(p.get("cid"), sha);

  // 2. Commit WITH explicit version still keeps v=
  urlSync.applyHash(`repo=linux&v=v3.0&tab=commits&cid=${sha}`);
  tab = state.getActiveTab();
  assert.equal(tab.version, "v3.0");
  p = lastParams();
  assert.equal(p.get("v"), "v3.0");

  // Re-applying versionless commit link over existing "Commits" tab clears its version back to ""
  urlSync.applyHash(`repo=linux&tab=commits&cid=${sha}`);
  assert.equal(state.getActiveTab().version, "");
  assert.equal(lastParams().get("v"), null);

  // 3. Mailing list link without version (and legacy link with v= drops v=)
  urlSync.applyHash("repo=linux&tab=mail&tid=77&mid=patch%40kernel.org");
  tab = state.getActiveTab();
  assert.equal(tab.type, "mail");
  assert.equal(tab.version, "");
  assert.equal(tab.threadId, 77);
  assert.equal(tab.messageId, "patch@kernel.org");
  p = lastParams();
  assert.equal(p.get("v"), null);
  assert.equal(p.get("tid"), "77");
  assert.equal(p.get("mid"), "patch@kernel.org");

  urlSync.applyHash("repo=linux&v=v3.0&tab=mail&tid=78&mid=msg%40kernel.org");
  assert.equal(state.getActiveTab().version, "");
  assert.equal(lastParams().get("v"), null);

  // 4. CVE link without version (and legacy link with v= drops v=)
  urlSync.applyHash("repo=linux&tab=cve&id=CVE-2024-9999");
  tab = state.getActiveTab();
  assert.equal(tab.type, "cve");
  assert.equal(tab.version, "");
  assert.equal(tab.sel, "CVE-2024-9999");
  assert.equal(lastParams().get("v"), null);
  assert.equal(lastParams().get("id"), "CVE-2024-9999");

  urlSync.applyHash("repo=linux&v=v3.0&tab=cve&id=CVE-2024-8888");
  assert.equal(state.getActiveTab().version, "");
  assert.equal(lastParams().get("v"), null);

  // 5. Watch, bookmarks, status hashes never carry v=
  for (const t of ["watch", "bookmarks", "status"]) {
    urlSync.applyHash(`repo=linux&tab=${t}`);
    assert.equal(state.getActiveTab().version, "");
    assert.equal(lastParams().get("v"), null, `${t} hash should omit v=`);
  }
});

test("urlSync.getNavKey ignores currentVersion for versionless tabs and restoreSnapshot rewinds version to ''", () => {
  resetWorkspace([
    { id: "cm", type: "commits", title: "Commits", commitId: "deadbeef", repo: "linux", version: "" },
    { id: "ml", type: "mail", title: "Mailing List", threadId: 12, repo: "linux", version: "" },
  ], "cm");
  urlSync.init();

  state.currentVersion = "v3.0";
  const k1 = urlSync.getNavKey(state.findTab("ml"));
  const c1 = urlSync.getNavKey(state.findTab("cm"));
  state.currentVersion = "v6.1";
  assert.equal(urlSync.getNavKey(state.findTab("ml")), k1);
  assert.equal(urlSync.getNavKey(state.findTab("cm")), c1);

  // Snapshot before applying a version to versionless commits tab
  state.focusTab("cm");
  urlSync.updateHashFromState({ replace: true });
  const beforeApplySnap = urlSync.createSnapshot(globalThis.location.hash);
  state.setVersion("v3.0", state.findTab("cm"));
  assert.equal(state.findTab("cm").version, "v3.0");

  urlSync.restoreSnapshot(beforeApplySnap);
  assert.equal(state.findTab("cm").version, "");
});

test("api commit endpoints route without version segment when version is empty", async () => {
  requests.length = 0;
  await api.getCommitDetail("", "deadbeef");
  await api.getCommitDiff("", "deadbeef", 3);
  await api.getCommitTimeline("", 50, "past", null);
  await api.getCommits("", 1, 50, "ext4");
  await api.queryCommits("", "author=linus");
  await api.getCommitRelations("", "deadbeef");
  await api.getCommitRelease("", "deadbeef");
  await api.resolveCommitMail("", "deadbeef");

  const urls = requests.map((r) => r.url);
  assert.equal(urls[0], "/webapi/commits/detail/deadbeef");
  assert.equal(urls[1], "/webapi/commits/diff/deadbeef?context=3");
  assert.equal(urls[2], "/webapi/commits/timeline?limit=50&direction=past");
  assert.equal(urls[3], "/webapi/commits/list?page=1&limit=50&q=ext4");
  assert.equal(urls[4], "/webapi/commits/query?author=linus");
  assert.equal(urls[5], "/webapi/commits/relations/deadbeef");
  assert.equal(urls[6], "/webapi/commits/released/deadbeef");
  assert.equal(urls[7], "/webapi/commits/resolve_mail/deadbeef");

  // With a version, routes keep the version path segment
  requests.length = 0;
  await api.getCommitDetail("v3.0", "deadbeef");
  assert.equal(requests[0].url, "/webapi/commits/v3.0/detail/deadbeef");
});

test("TabsComponent omits .tab-version badge on versionless tabs", () => {
  resetWorkspace([
    { id: "t-code", type: "code", title: "main.c", path: "init/main.c", version: "v3.0", repo: "linux" },
    { id: "t-mail", type: "mail", title: "Mailing List", version: "", repo: "linux" },
    { id: "t-cve", type: "cve", title: "CVEs", version: "", repo: "linux" },
    { id: "t-cm-vl", type: "commits", title: "Commit deadbee", commitId: "deadbeef", version: "", repo: "linux" },
    { id: "t-cm-v", type: "commits", title: "Commits", version: "v3.0", repo: "linux" },
  ], "t-code");

  const container = document.createElement("div");
  const tabsComp = new TabsComponent(container, {});
  // Stub renderActiveTabView so only tabstrips are inspected
  tabsComp.renderActiveTabView = () => {};
  tabsComp.render();

  const findBadges = (el, out = []) => {
    if (el.dataset?.tabId) {
      const hasVer = el.children.some((c) => c.className === "tab-version");
      out.push({ id: el.dataset.tabId, hasVer });
    }
    for (const c of el.children || []) findBadges(c, out);
    return out;
  };
  const badges = Object.fromEntries(findBadges(container).map((x) => [x.id, x.hasVer]));
  assert.deepEqual(badges, {
    "t-code": true,
    "t-mail": false,
    "t-cve": false,
    "t-cm-vl": false,
    "t-cm-v": true,
  });
});

test("CommitsView and CveView keep versionless tabs independent of state.currentVersion", async () => {
  resetWorkspace([{ id: "cm", type: "commits", title: "Commit", commitId: "deadbeef1234", listCollapsed: true, version: "" }], "cm");
  state.currentVersion = "v3.0";

  const detailCalls = [];
  const view = new CommitsView();
  view.api = {
    repo: "linux",
    async getCommitDetail(ver, cid) {
      detailCalls.push({ ver, cid });
      return { commit_hash: "deadbeef1234567890", subject: "test", author_name: "A", author_email: "a@x", files: [] };
    },
    async getCommitRelease() { return { released: false }; },
    async getCommitRelations() { return { state: "done", sha: "deadbeef1234567890", cves: {} }; },
  };
  const mk = (tag = "div") => {
    const el = document.createElement(tag);
    el.isConnected = true;
    return el;
  };
  const els = {
    "#btn-commits-expand": mk("button"),
    "#btn-commits-collapse": mk("button"),
    "#commit-search-input": mk("input"),
    "#btn-commit-query-help": mk("button"),
    "#commit-query-chips": mk("div"),
    "#commit-detail-container": mk("div"),
    "#commit-list-container": mk("div"),
  };
  const host = {
    isConnected: true,
    dataset: {},
    innerHTML: "",
    querySelector: (s) => els[s] || null,
    querySelectorAll: () => [],
    addEventListener() {},
  };
  await view.render(host, state.findTab("cm"));
  assert.equal(view.currentVersion, "");
  assert.deepEqual(detailCalls, [{ ver: "", cid: "deadbeef1234" }]);
  view.dispose();

  // buildDeepLink builds versionless URL cleanly
  const p = new URLSearchParams({ repo: "linux", tab: "commits", cid: "deadbeef", lc: "1" });
  assert.equal(buildDeepLink(p), "http://localhost/#repo=linux&tab=commits&cid=deadbeef&lc=1");

  // CveView does not inherit state.currentVersion
  const cveView = new CveView();
  cveView.api = {
    async getCveList() { return { configured: true, ingested: true, total: 0, cves: [] }; },
    async getCveDetail() { return { id: "CVE-2024-1234", title: "t", files: ["net/a.c"], ranges: [] }; },
  };
  const cveEls = {
    ".sv-container": mk("div"),
    ".sv-search": mk("input"),
    ".cve-version-input": mk("input"),
    ".sv-chips": mk("div"),
    ".sv-download": mk("button"),
    ".sv-collapse": mk("button"),
    ".sv-rail": mk("button"),
    ".sv-detail": mk("div"),
    ".sv-list": Object.assign(mk("div"), { querySelector: () => mk("div") }),
    ".sv-status": mk("div"),
    ".sv-summary": mk("div"),
  };
  const cveHost = {
    isConnected: true,
    innerHTML: "",
    querySelector: (s) => cveEls[s] || null,
    querySelectorAll: () => [],
  };
  cveView.render(cveHost, { id: "cve1", type: "cve", sel: "CVE-2024-1234", listCollapsed: true, version: "" });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(cveView.version, "");
  assert.doesNotMatch(cveEls[".sv-detail"].innerHTML, /data-version="v3\.0"/);
  cveView.dispose();
});
