/**
 * multi_repo.test.mjs - One session, tabs of several projects (webapp/API_FRONTEND.md §5.18, §5.26):
 * per-tab repository routing of the API client, the hash always naming the tab's repository, and a link
 * into another project opening its tab next to the open ones.
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

// Module singletons start heartbeats: they must not keep the test process alive
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

// Every request the client makes: {url, repo (X-KI-Repo)}
const requests = [];
globalThis.fetch = async (url, opts = {}) => {
  requests.push({ url: String(url), repo: opts.headers?.["X-KI-Repo"] || "" });
  return {
    ok: true, status: 200,
    headers: { get: () => null },
    json: async () => ({ ok: true }),
    text: async () => "",
  };
};
// Hashes url_sync writes
const hashes = [];
globalThis.history.replaceState = (_s, _t, h) => hashes.push(h);
globalThis.history.pushState = (_s, _t, h) => hashes.push(h);

const { api, bindViewApi } = await import("../js/api.js");
const { state } = await import("../js/state.js");
const { urlSync } = await import("../js/url_sync.js");
await import("../js/routes.js");
const { remapSessionRepos } = await import("../js/utils/session_transfer.js");
const { PROJECT_MOVABLE_TABS, projectBadge, setProjects } = await import("../js/utils/projects.js");

setProjects([
  { repo: "linux", project: "linux", name: "Linux", available: true },
  { repo: "man-pages", project: "man-pages", name: "man-pages", available: true },
]);

function workspace(tabs, activeTabId = tabs[0]?.id) {
  state.currentRepo = "linux";
  state.currentVersion = "v3.0";
  state.selectedVersion = "v3.0";
  state.splitMode = tabs.some((t) => t.pane === "pane-2") ? "horizontal" : "single";
  const panes = [{ id: "pane-1", activeTabId, tabs: tabs.filter((t) => (t.pane || "pane-1") === "pane-1") }];
  const second = tabs.filter((t) => t.pane === "pane-2");
  if (second.length) panes.push({ id: "pane-2", activeTabId: second[0].id, tabs: second });
  state.panes = panes;
  state.activePaneId = "pane-1";
}

const lastHash = () => new URLSearchParams(String(hashes[hashes.length - 1] || "").replace(/^#/, ""));
const allTabs = () => state.panes.flatMap((p) => p.tabs);

test("each tab's requests name its own repository, whichever tab is active", async () => {
  workspace([
    { id: "k1", type: "code", path: "init/main.c", version: "v3.0", repo: "linux" },
    { id: "m1", type: "man", version: "man-pages-6.9", repo: "man-pages", pane: "pane-2" },
  ]);
  const kernelView = bindViewApi({}, "k1");
  const manView = bindViewApi({}, "m1");
  requests.length = 0;
  await kernelView.api.getSync();
  await manView.api.getSync();
  await api.getSync(); // the shared client: the active tab's (pane-1: k1)
  assert.deepEqual(requests.map((r) => r.repo), ["linux", "man-pages", "linux"]);

  // The other pane becomes the active one: background views keep their repository
  state.setActivePane("pane-2");
  assert.equal(state.currentRepo, "man-pages");
  requests.length = 0;
  await kernelView.api.getSync();
  await api.getSync();
  assert.deepEqual(requests.map((r) => r.repo), ["linux", "man-pages"]);
  // URLs that cannot carry headers name the repository too
  assert.match(kernelView.api.withRepo("/api/fs/raw/v3.0?path=a"), /[?&]repo=linux$/);
  assert.match(manView.api.withRepo("/x"), /\?repo=man-pages$/);
});

test("scoped clients share connection state and keep stamps per repository", () => {
  workspace([{ id: "k1", type: "code", path: "a.c", version: "v3.0", repo: "linux" }]);
  const man = api.forRepo("man-pages");
  assert.equal(api.forRepo("man-pages"), man);
  assert.equal(api.forRepo(""), api);
  man.activeRequests = 3;
  assert.equal(api.activeRequests, 3);
  api.activeRequests = 0;
  man.versionStamps = { "man-pages-6.9": "abc.git" };
  api.forRepo("linux").versionStamps = { "v3.0": "def.1" };
  assert.equal(man._vk("man-pages-6.9"), "man-pages-6.9@abc.git~");
  assert.equal(api._vk("v3.0"), "v3.0@def.1~");
  assert.equal(api._vk("man-pages-6.9"), "man-pages-6.9");
  man.repo = "linux"; // a scoped client never changes repository
  assert.equal(man.repo, "man-pages");
});

test("a view follows its tab when the tab moves to another project", () => {
  workspace([{ id: "e1", type: "explorer", path: "kernel", version: "v3.0", repo: "linux" }]);
  const view = bindViewApi({}, "e1");
  assert.equal(view.api.repo, "linux");
  const tab = state.findTab("e1");
  state.moveTabToRepository(tab, "man-pages", "man-pages-6.9", PROJECT_MOVABLE_TABS.explorer(tab, "man-pages"));
  assert.equal(view.api.repo, "man-pages");
  assert.equal(tab.version, "man-pages-6.9");
  assert.equal(tab.path, undefined);
  assert.equal(state.currentRepo, "man-pages");
  assert.equal(state.currentVersion, "man-pages-6.9");
  assert.equal(allTabs().length, 1);
});

test("new tabs inherit the active tab's repository; a tab of another project is never reused", () => {
  workspace([{ id: "k1", type: "code", path: "init/main.c", version: "v3.0", repo: "linux" }]);
  state.openTab({ type: "search", title: "Search", query: "x" });
  assert.equal(state.getActiveTab().repo, "linux");
  state.openTab({ type: "search", title: "Search", query: "y", repo: "man-pages", version: "man-pages-6.9" });
  const tabs = allTabs();
  assert.equal(tabs.length, 3);
  assert.deepEqual(tabs.map((t) => t.repo), ["linux", "linux", "man-pages"]);
  assert.equal(state.currentRepo, "man-pages");
  // Same type, same repository: reused
  state.openTab({ type: "search", title: "Search", query: "z", repo: "man-pages", version: "man-pages-6.9" });
  assert.equal(allTabs().length, 3);
});

test("every hash names the active tab's repository", () => {
  workspace([
    { id: "k1", type: "code", path: "init/main.c", version: "v3.0", repo: "linux" },
    { id: "m1", type: "man", manPage: "read", manSec: "2", version: "man-pages-6.9", repo: "man-pages" },
  ]);
  urlSync.updateHashFromState({ replace: true });
  assert.equal(lastHash().get("repo"), "linux");
  assert.equal(lastHash().get("file"), "init/main.c");
  state.focusTab("m1");
  const h = lastHash();
  assert.equal(h.get("repo"), "man-pages");
  assert.equal(h.get("v"), "man-pages-6.9");
  assert.equal(h.get("tab"), "man");
  assert.equal(h.get("page"), "read");
  assert.equal(h.get("mrepo"), null);
  // A tab saved without one takes the header's project
  workspace([{ id: "old", type: "explorer", path: "", version: "v3.0" }]);
  urlSync.updateHashFromState({ replace: true });
  assert.equal(lastHash().get("repo"), "linux");
});

test("a link into another project opens its tab next to the open ones", () => {
  workspace([
    { id: "k1", type: "code", path: "init/main.c", version: "v3.0", repo: "linux" },
    { id: "k2", type: "kconfig", arch: "x86_64", version: "v3.0", repo: "linux" },
  ]);
  const known = { "man-pages": "man-pages-6.9", linux: "master" };
  urlSync.setRepoHandler((repo) => (repo in known ? { repo, version: known[repo] } : { repo: "", version: "" }));
  urlSync.applyHash("repo=man-pages&tab=man&page=read&sec=2");
  const tabs = allTabs();
  assert.equal(tabs.length, 3);
  assert.deepEqual(tabs.slice(0, 2).map((t) => t.id), ["k1", "k2"]);
  const man = state.getActiveTab();
  assert.equal(man.type, "man");
  assert.equal(man.repo, "man-pages");
  assert.equal(man.version, "man-pages-6.9"); // the project's default revision: the link had no v=
  assert.equal(state.currentRepo, "man-pages");
  assert.equal(lastHash().get("repo"), "man-pages");

  // Back to the kernel with an explicit revision: a new code tab, the man tab stays
  urlSync.applyHash("repo=linux&v=v3.1&tab=code&file=kernel%2Ffork.c");
  assert.equal(allTabs().length, 4);
  assert.equal(state.getActiveTab().repo, "linux");
  assert.equal(state.getActiveTab().version, "v3.1");
  assert.equal(state.currentRepo, "linux");

  // A repository not configured here opens in the active tab's project
  urlSync.applyHash("repo=elsewhere&v=v3.0&tab=explorer&dir=mm");
  assert.equal(state.getActiveTab().repo, "linux");
  assert.equal(allTabs().length, 5);
});

test("legacy man links (mrepo/mv) open the man-pages tab they address", () => {
  workspace([{ id: "k1", type: "code", path: "a.c", version: "v6.9", repo: "linux" }]);
  urlSync.setRepoHandler((repo) => ({ repo, version: "" }));
  urlSync.applyHash("repo=linux&v=v6.9&tab=man&page=read&sec=2&mrepo=man-pages&mv=man-pages-6.9&kv=v6.9&krepo=linux");
  const man = state.getActiveTab();
  assert.equal(man.repo, "man-pages");
  assert.equal(man.version, "man-pages-6.9");
  assert.equal(man.kernelRepo, "linux");
  assert.equal(allTabs().length, 2);
});

test("session import keeps each tab's project", () => {
  const panes = [{ id: "pane-1", tabs: [{ id: "a", type: "code", repo: "man-pages" }, { id: "b", type: "code" }, { id: "c", type: "code", repo: "gone" }] }];
  const { panes: out, missing } = remapSessionRepos(panes, { fileRepo: "linux", known: ["linux", "man-pages"], fallback: "linux" });
  assert.deepEqual(out[0].tabs.map((t) => t.repo), ["man-pages", "linux", "linux"]);
  assert.deepEqual(missing, ["gone"]);
  assert.equal(panes[0].tabs[1].repo, undefined); // input untouched
  // Projects unknown (offline): names are kept
  assert.deepEqual(remapSessionRepos(panes, { fileRepo: "linux" }).panes[0].tabs.map((t) => t.repo), ["man-pages", "linux", "gone"]);
});

test("project badge only for tabs outside the linux kernel", () => {
  assert.equal(projectBadge("linux"), "");
  assert.equal(projectBadge(""), "");
  assert.equal(projectBadge("man-pages"), "man-pages");
  assert.equal(projectBadge("other-repo"), "other-repo");
});

test("compare man-pages mode deep links and reused man tab version update", () => {
  workspace([
    { id: "m1", type: "man", manPage: "read", manSec: "2", version: "man-pages-6.9", repo: "man-pages" },
  ]);
  urlSync.setRepoHandler((repo) => ({ repo, version: "" }));
  // Reopening an existing man tab at another revision updates its version
  state.openTab({ type: "man", repo: "man-pages", version: "man-pages-6.10", manPage: "newcall", manSec: "2" });
  assert.equal(state.getActiveTab().version, "man-pages-6.10");
  assert.equal(state.getActiveTab().manPage, "newcall");

  // Deep link to Compare in man-pages mode round-trips cleanly
  urlSync.applyHash("repo=man-pages&tab=diff&a=man-pages-6.9&b=man-pages-6.10&mode=man&st=added,modified&sec=2&q=read");
  const diff = state.getActiveTab();
  assert.equal(diff.type, "diff");
  assert.equal(diff.repo, "man-pages");
  assert.equal(diff.mode, "man");
  assert.equal(diff.versionA, "man-pages-6.9");
  assert.equal(diff.versionB, "man-pages-6.10");
  assert.equal(diff.manStatuses, "added,modified");
  assert.equal(diff.manSection, "2");
  assert.equal(diff.manQuery, "read");
  const h = lastHash();
  assert.equal(h.get("repo"), "man-pages");
  assert.equal(h.get("mode"), "man");
  assert.equal(h.get("st"), "added,modified");
  assert.equal(h.get("sec"), "2");
  assert.equal(h.get("q"), "read");
});
