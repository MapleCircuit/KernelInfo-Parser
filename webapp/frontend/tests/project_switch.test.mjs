/**
 * project_switch.test.mjs - Switching the header Project leaves the active tab untouched while
 * updating the header's Branch/Tag choices and the Activity Bar's valid tabs (webapp/API_FRONTEND.md §5.18).
 */
import { FakeElement } from "./helpers/dom_shim.mjs";
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

globalThis.__KIP_SKIP_AUTOSTART__ = true;

const PROJECTS = [
  { repo: "linux", project: "linux", slug: "linux", name: "Linux", available: true, indexed: true, default_branch: "master" },
  { repo: "man-pages", project: "man-pages", slug: "man-pages", name: "man-pages", available: true, indexed: false, default_branch: "master" },
  { repo: "linux-alt", project: "linux", slug: "linux-alt", name: "Linux Alt", available: true, indexed: true, default_branch: "master" },
];

const BRANCHES_BY_REPO = {
  linux: [{ name: "master", is_default: true }, { name: "linux-3.0.y", is_default: false }],
  "man-pages": [{ name: "master", is_default: true }, { name: "man-pages-6.9", is_default: false }],
  "linux-alt": [{ name: "master", is_default: true }, { name: "linux-3.1.y", is_default: false }],
};

const BRANCH_TAGS_BY_REPO = {
  linux: { master: ["v3.0", "v3.1"] },
  "man-pages": { master: ["man-pages-6.8", "man-pages-6.9"] },
  "linux-alt": { master: ["v3.0", "v3.1", "v3.2-rc1", "v3.2"], "linux-3.1.y": ["v3.0", "v3.1", "v3.1.5"] },
};

const flushAsync = () => new Promise((r) => realSetTimeout(r, 20));

const VERSIONS_BY_REPO = {
  linux: {
    g: "linux-g1",
    n: ["v3.0", "v3.1"],
    h: "111111111111222222222222",
    p: [1000, 1001],
    t: [],
  },
  "man-pages": {
    g: "man-g1",
    n: ["man-pages-6.8", "man-pages-6.9"],
    h: "333333333333444444444444",
    p: [null, null],
    t: [],
  },
  "linux-alt": {
    g: "alt-g1",
    n: ["v3.0", "v3.1", "v3.1.5", "v3.2-rc1", "v3.2"],
    h: "111111111111222222222222333333333333444444444444555555555555",
    p: [1000, 1001, 1002, 1003, null],
    t: [],
  },
};

class SelectFake extends FakeElement {
  constructor(tag = "select") {
    super(tag);
    this._value = "";
    this._innerHTML = "";
  }
  get options() {
    const opts = [];
    for (const child of this.children) {
      if (child.tagName === "OPTGROUP") opts.push(...child.children);
      else opts.push(child);
    }
    return opts;
  }
  get value() {
    const sel = this.options.find((o) => o.selected);
    return sel ? sel.value : this._value;
  }
  set value(v) {
    this._value = v;
    for (const o of this.options) o.selected = o.value === v;
  }
  set innerHTML(val) {
    this._innerHTML = val;
    if (val === "") {
      this.children = [];
      return;
    }
    // Parse <option value="..." ...>text</option> emitted by renderBranchSelector / initVersionSelector
    const parsed = [];
    const optRe = /<option\s+value="([^"]*)"([^>]*)>([\s\S]*?)<\/option>/g;
    let m;
    while ((m = optRe.exec(val)) !== null) {
      const opt = new FakeElement("option");
      opt.value = m[1];
      opt.selected = /\bselected\b/.test(m[2]);
      opt.textContent = m[3];
      parsed.push(opt);
    }
    this.children = parsed;
  }
  get innerHTML() {
    return this._innerHTML;
  }
}

function makeActivityBtn(action) {
  const btn = new FakeElement("button");
  btn.dataset.action = action;
  btn.hidden = action === "man";
  return btn;
}

const domIds = {
  "project-select": new SelectFake("select"),
  "branch-picker-btn": new FakeElement("button"),
  "branch-picker-label": new FakeElement("span"),
  "version-select": new SelectFake("select"),
  "rc-select": new SelectFake("select"),
  "rc-select-label": new FakeElement("label"),
  "commit-picker-btn": new FakeElement("button"),
  "commit-picker-label": new FakeElement("span"),
  "version-apply-btn": new FakeElement("button"),
  "version-newtab-btn": new FakeElement("button"),
};

const activityBtns = [
  "explorer", "kconfig", "nodemap", "maintainers", "commits", "mail", "api", "insights", "man", "compare",
].map(makeActivityBtn);

document.getElementById = (id) => domIds[id] || null;
document.querySelector = (sel) => {
  const m = /\.activity-btn\[data-action="([^"]+)"\]/.exec(sel);
  if (m) return activityBtns.find((b) => b.dataset.action === m[1]) || null;
  return null;
};
document.querySelectorAll = (sel) => (sel === ".activity-btn" ? activityBtns : []);

// Mock fetch for projects / branches / versions
globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  const repo = opts.headers?.["X-KI-Repo"] || "linux";
  let body = { ok: true };
  if (u.includes("/webapi/projects")) {
    body = { projects: PROJECTS, active: "linux" };
  } else if (u.includes("/webapi/branch/tags")) {
    const q = new URL(u, "http://localhost").searchParams.get("branch") || "master";
    const tags = BRANCH_TAGS_BY_REPO[repo]?.[q] || [];
    body = { branch: q, head: "aaaaaaaaaaaaaaaaaaaa", total: tags.length, tags };
  } else if (u.includes("/webapi/branches")) {
    body = { branches: BRANCHES_BY_REPO[repo] || BRANCHES_BY_REPO.linux, tag_views: [] };
  } else if (u.includes("/webapi/versions")) {
    body = VERSIONS_BY_REPO[repo] || VERSIONS_BY_REPO.linux;
  }
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
};

const { KernelIdeApp } = await import("../js/app.js");
const { state } = await import("../js/state.js");
const { api } = await import("../js/api.js");
const { setProjects } = await import("../js/utils/projects.js");
const { openVersionInNewTab } = await import("../js/components/version_switch.js");

setProjects(PROJECTS);

function resetWorkspace(tabs, activeTabId = tabs[0]?.id) {
  state._repoManualOverride = false;
  state._repoOverrideTabId = null;
  state.currentRepo = "linux";
  state.currentVersion = "v3.0";
  state.selectedVersion = "v3.0";
  state.selectedBranch = "";
  state.selectedBranchByRepo = {};
  state.selectedVersionByRepo = {};
  state.panes = [{ id: "pane-1", activeTabId, tabs }];
  state.activePaneId = "pane-1";
}

function visibleActivityActions() {
  return activityBtns.filter((b) => !b.hidden).map((b) => b.dataset.action);
}

test("switchProject keeps the active tab unchanged while updating branch, tag and activity bar", async () => {
  resetWorkspace([
    { id: "t1", type: "explorer", title: "Explorer v3.0", path: "kernel", version: "v3.0", repo: "linux" },
    { id: "t2", type: "code", title: "sched.c", path: "kernel/sched.c", version: "v3.0", repo: "linux" },
  ], "t1");

  const app = new KernelIdeApp();
  app.projects = PROJECTS;
  let sidebarLoads = 0;
  app.loadSidebarFileTree = async () => { sidebarLoads += 1; };
  app.tabsComponent = { render() {} };

  // Wire repo:synced the same way app.init() does
  const unsubRepo = state.subscribe("repo:synced", async () => {
    app.updateActivityBar(state.currentRepo || state.activeRepo());
    await app.initVersionSelector();
    if (!state.getActiveTab() || state.activeRepo() === state.currentRepo) {
      await app.loadSidebarFileTree();
    }
  });
  const unsubTabs = state.subscribe("tabs:changed", () => {
    app.updateVersionSelectorDisplay();
    app.updateActivityBar(state.currentRepo || state.activeRepo());
  });

  try {
    await app.initVersionSelector();
    app.setupActivityBar();
    assert.deepEqual(visibleActivityActions(), [
      "explorer", "kconfig", "nodemap", "maintainers", "commits", "mail", "api", "insights", "man", "compare",
    ]);

    // Switch Project to man-pages while explorer tab (t1) in linux is active
    await app.switchProject("man-pages");
    // Wait for async repo:synced initVersionSelector() to finish
    await flushAsync();

    // 1. Current tab MUST remain the exact same (not moved, not closed, no new tab opened)
    assert.equal(state.getActiveTab().id, "t1");
    assert.equal(state.getActiveTab().repo, "linux");
    assert.equal(state.getActiveTab().version, "v3.0");
    assert.equal(state.getActiveTab().path, "kernel");
    assert.equal(state.panes[0].tabs.length, 2);
    assert.equal(state.currentVersion, "v3.0");
    assert.equal(sidebarLoads, 0);

    // 2. Project, Branch and Tag choices MUST update to man-pages
    assert.equal(state.currentRepo, "man-pages");
    assert.equal(state.selectedVersion, "man-pages-6.9");
    assert.equal(domIds["project-select"].value, "man-pages");
    assert.deepEqual(
      (app.branches || []).map((b) => b.name),
      ["master", "man-pages-6.9"],
    );
    assert.equal(domIds["branch-picker-label"].textContent, "— any branch —");
    assert.equal(typeof domIds["branch-picker-btn"].onclick, "function");
    assert.deepEqual(
      domIds["version-select"].options.map((o) => o.value),
      ["man-pages-6.8", "man-pages-6.9"],
    );
    assert.equal(domIds["version-select"].value, "man-pages-6.9");

    // 3. Activity bar MUST update to show only valid tabs for man-pages
    assert.deepEqual(visibleActivityActions(), [
      "explorer", "commits", "insights", "man", "compare",
    ]);

    // 4. Closing a background tab does NOT revert the header Project
    state.closeTab("t2");
    assert.equal(state.getActiveTab().id, "t1");
    assert.equal(state.currentRepo, "man-pages");
    assert.deepEqual(visibleActivityActions(), [
      "explorer", "commits", "insights", "man", "compare",
    ]);

    // 5. Switching Project back to linux works even though active tab is already linux
    await app.switchProject("linux");
    await flushAsync();
    assert.equal(state.currentRepo, "linux");
    assert.equal(state.getActiveTab().id, "t1");
    assert.deepEqual(visibleActivityActions(), [
      "explorer", "kconfig", "nodemap", "maintainers", "commits", "mail", "api", "insights", "man", "compare",
    ]);
  } finally {
    unsubRepo();
    unsubTabs();
  }
});

test("activity bar and New Tab open in the selected header Project, while switching tabs syncs header back", async () => {
  resetWorkspace([
    { id: "t1", type: "code", title: "sched.c", path: "kernel/sched.c", version: "v3.0", repo: "linux" },
    { id: "t2", type: "code", title: "fork.c", path: "kernel/fork.c", version: "v3.0", repo: "linux" },
  ], "t1");

  const app = new KernelIdeApp();
  app.projects = PROJECTS;
  app.loadSidebarFileTree = async () => {};
  app.tabsComponent = { render() {} };
  const unsubRepo = state.subscribe("repo:synced", async () => {
    app.updateActivityBar(state.currentRepo || state.activeRepo());
    await app.initVersionSelector();
  });

  try {
    await app.initVersionSelector();
    // Ensure fresh click listeners on fake buttons
    for (const b of activityBtns) b._l?.clear?.();
    app.setupActivityBar();

    await app.switchProject("man-pages");
    await flushAsync();
    assert.equal(state.getActiveTab().id, "t1");

    // Clicking New Tab opens explorer in man-pages @ man-pages-6.9
    openVersionInNewTab(state.selectedVersion, state.currentRepo || state.activeRepo());
    const expTab = state.getActiveTab();
    assert.equal(expTab.type, "explorer");
    assert.equal(expTab.repo, "man-pages");
    assert.equal(expTab.version, "man-pages-6.9");

    // Focus back to t1 (linux), switch Project to man-pages, then click Commits in activity bar
    state.focusTab("t1");
    await flushAsync();
    assert.equal(state.currentRepo, "linux");

    await app.switchProject("man-pages");
    await flushAsync();
    const commitsBtn = activityBtns.find((b) => b.dataset.action === "commits");
    commitsBtn.dispatchEvent({ type: "click" });
    const commitsTab = state.getActiveTab();
    assert.equal(commitsTab.type, "commits");
    assert.equal(commitsTab.repo, "man-pages");
    assert.equal(commitsTab.version, "man-pages-6.9");

    // Focusing another linux tab syncs Project and activity bar back to linux
    state.focusTab("t2");
    await flushAsync();
    assert.equal(state.currentRepo, "linux");
    assert.deepEqual(visibleActivityActions(), [
      "explorer", "kconfig", "nodemap", "maintainers", "commits", "mail", "api", "insights", "man", "compare",
    ]);

    // Clean up api scoped client state
    assert.equal(api.forRepo("man-pages").repo, "man-pages");
  } finally {
    unsubRepo();
  }
});

test("Apply never corrupts non-movable tabs across projects and moves movable tabs when requested", async () => {
  resetWorkspace([
    { id: "c1", type: "code", title: "sched.c", path: "kernel/sched.c", version: "v3.0", repo: "linux" },
    { id: "e1", type: "explorer", title: "Explorer v3.0", path: "kernel", version: "v3.0", repo: "linux" },
  ], "c1");

  const app = new KernelIdeApp();
  app.projects = PROJECTS;
  app.loadSidebarFileTree = async () => {};
  app.tabsComponent = { render() {} };
  const unsubRepo = state.subscribe("repo:synced", async () => {
    app.updateActivityBar(state.currentRepo || state.activeRepo());
    await app.initVersionSelector();
  });

  try {
    await app.initVersionSelector();
    await app.switchProject("man-pages");
    await flushAsync();

    // Non-movable code tab: clicking Apply leaves c1 untouched on linux @ v3.0
    await domIds["version-apply-btn"].onclick();
    assert.equal(state.getActiveTab().id, "c1");
    assert.equal(state.getActiveTab().repo, "linux");
    assert.equal(state.getActiveTab().version, "v3.0");
    assert.equal(state.currentRepo, "man-pages");

    // Disabled activity button (kconfig on man-pages) does not open a tab
    const kconfigBtn = activityBtns.find((b) => b.dataset.action === "kconfig");
    kconfigBtn.dispatchEvent({ type: "click" });
    assert.equal(state.getActiveTab().id, "c1");

    // Movable explorer tab: switching to e1 syncs header to linux, switching Project to man-pages,
    // and clicking Apply explicitly moves e1 to man-pages @ man-pages-6.9
    state.focusTab("e1");
    await flushAsync();
    assert.equal(state.currentRepo, "linux");

    await app.switchProject("man-pages");
    await flushAsync();
    assert.equal(state.getActiveTab().repo, "linux");

    await domIds["version-apply-btn"].onclick();
    assert.equal(state.getActiveTab().id, "e1");
    assert.equal(state.getActiveTab().repo, "man-pages");
    assert.equal(state.getActiveTab().version, "man-pages-6.9");
  } finally {
    unsubRepo();
  }
});

test("switchProject restores last valid tag used and falls back to latest parsed final tag on master", async () => {
  resetWorkspace([
    { id: "c1", type: "code", title: "sched.c", path: "kernel/sched.c", version: "v3.0", repo: "linux" },
  ], "c1");

  const app = new KernelIdeApp();
  app.projects = PROJECTS;
  app.loadSidebarFileTree = async () => {};
  app.tabsComponent = { render() {} };
  const unsubRepo = state.subscribe("repo:synced", async () => {
    app.updateActivityBar(state.currentRepo || state.activeRepo());
    await app.initVersionSelector();
  });

  try {
    await app.initVersionSelector();
    // Currently in linux on v3.0; picking a branch head should NOT clobber the last valid tag ("v3.0")
    await app.selectBranch("master");
    assert.equal(state.selectedVersion, "master");
    assert.equal(state.selectedVersionByRepo.linux, "v3.0");

    // First switch to linux-alt (never visited): picks latest parsed final tag from master ("v3.1"),
    // ignoring parsed off-master point release ("v3.1.5"), parsed prerelease ("v3.2-rc1"), and unparsed ("v3.2")
    await app.switchProject("linux-alt");
    await flushAsync();
    assert.equal(state.currentRepo, "linux-alt");
    assert.equal(state.selectedVersion, "v3.1");

    // Pick v3.0 in linux-alt, switch to man-pages (unparsed -> newest final on master: man-pages-6.9),
    // pick man-pages-6.8, then switch back to linux -> restores v3.0 (not "master")
    state.setSelectedVersion("v3.0");
    await app.switchProject("man-pages");
    await flushAsync();
    assert.equal(state.selectedVersion, "man-pages-6.9");
    state.setSelectedVersion("man-pages-6.8");

    await app.switchProject("linux");
    await flushAsync();
    assert.equal(state.selectedVersion, "v3.0");

    // Switching back to man-pages restores man-pages-6.8, and back to linux-alt restores v3.0
    await app.switchProject("man-pages");
    await flushAsync();
    assert.equal(state.selectedVersion, "man-pages-6.8");

    await app.switchProject("linux-alt");
    await flushAsync();
    assert.equal(state.selectedVersion, "v3.0");

    // If the remembered tag is no longer valid in the repo, fall back to latest parsed final tag on master
    state.selectedVersionByRepo.linux = "v99.0-deleted";
    await app.switchProject("linux");
    await flushAsync();
    assert.equal(state.selectedVersion, "v3.1");
  } finally {
    unsubRepo();
  }
});
