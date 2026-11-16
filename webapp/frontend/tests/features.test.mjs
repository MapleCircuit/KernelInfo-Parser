/**
 * features.test.mjs - Per-project feature toggles, Compare mode filtering, Insights section visibility,
 * and URL deep-link feature guarding.
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

const {
  DEFAULT_KERNEL_FEATURES,
  DEFAULT_KERNEL_INSIGHTS,
  DEFAULT_MANPAGES_FEATURES,
  DEFAULT_MANPAGES_INSIGHTS,
  hasFeature,
  isTabFeatureEnabled,
  manPagesRepo,
  repoFeatures,
  repoInsights,
  setProjects,
  tabTypeFeature,
} = await import("../js/utils/projects.js");
const { compareModesForRepo, compareSubtitleForModes, resolveCompareMode } = await import("../js/views/diff/api_diff.js");
const { insightsSections } = await import("../js/views/insights/insights_view.js");
await import("../js/views/insights/release_sections.js");
await import("../js/views/insights/stable_section.js");
await import("../js/views/insights/security_section.js");
const { relationsHtml } = await import("../js/views/commits/relations_render.js");
const { state } = await import("../js/state.js");
const { urlSync } = await import("../js/url_sync.js");
await import("../js/routes.js");

test("repoFeatures and hasFeature merge per-project overrides over defaults", () => {
  setProjects([
    { repo: "linux", project: "linux", available: true, features: { kconfig: false, cve: false } },
    { repo: "man-pages", project: "man-pages", available: true },
  ]);

  assert.equal(DEFAULT_KERNEL_FEATURES.kconfig, true);
  assert.equal(DEFAULT_MANPAGES_FEATURES.kconfig, false);
  assert.equal(DEFAULT_MANPAGES_FEATURES.man, true);

  const linuxFeats = repoFeatures("linux");
  assert.equal(linuxFeats.kconfig, false);
  assert.equal(linuxFeats.cve, false);
  assert.equal(linuxFeats.api, true);

  assert.equal(hasFeature("linux", "kconfig"), false);
  assert.equal(hasFeature("linux", "api"), true);
  assert.equal(hasFeature("linux", ["api", "commits"]), true);
  assert.equal(hasFeature("linux", ["api", "kconfig"]), false);
  assert.equal(hasFeature("man-pages", "api"), false);
  assert.equal(hasFeature("man-pages", "man"), true);
  assert.equal(hasFeature("unknown-repo", "kconfig"), true);
});

test("repoInsights merges per-project overrides and custom project defaults", () => {
  setProjects([
    { repo: "linux", project: "linux", available: true, insights: { milestone_rc: 2 } },
    { repo: "man-pages", project: "man-pages", available: true },
    { repo: "custom-tool", project: "custom-tool", available: true, git_url: "https://example.org/git/custom-tool.git" },
  ]);

  assert.equal(DEFAULT_KERNEL_INSIGHTS.merge_window, true);
  assert.equal(DEFAULT_KERNEL_INSIGHTS.milestone_rc, 1);
  assert.deepEqual(DEFAULT_KERNEL_INSIGHTS.lead_mergers, ["Linus Torvalds"]);
  assert.equal(DEFAULT_MANPAGES_INSIGHTS.merge_window, false);
  assert.equal(DEFAULT_MANPAGES_INSIGHTS.milestone_rc, 0);

  const linuxIns = repoInsights("linux");
  assert.equal(linuxIns.milestone_rc, 2);
  assert.equal(linuxIns.merge_window, true);
  assert.deepEqual(linuxIns.lead_mergers, ["Linus Torvalds"]);

  const manIns = repoInsights("man-pages");
  assert.equal(manIns.merge_window, false);
  assert.equal(manIns.milestone_rc, 0);
  assert.deepEqual(manIns.lead_mergers, []);

  const customIns = repoInsights("custom-tool");
  assert.equal(customIns.merge_window, true);
  assert.deepEqual(customIns.lead_mergers, []);
  assert.deepEqual(customIns.generic_repo_names, ["custom-tool"]);
  assert.deepEqual(customIns.generic_ref_words, ["custom-tool", "next"]);
});

test("manPagesRepo only selects a man-pages repo with man feature enabled", () => {
  setProjects([
    { repo: "linux", project: "linux", available: true },
    { repo: "man-disabled", project: "man-pages", available: true, features: { man: false } },
    { repo: "man-enabled", project: "man-pages", available: true, features: { man: true } },
  ]);
  assert.equal(manPagesRepo("man-disabled"), "man-enabled");
  assert.equal(manPagesRepo(), "man-enabled");

  setProjects([
    { repo: "linux", project: "linux", available: true },
    { repo: "man-disabled", project: "man-pages", available: true, features: { man: false } },
  ]);
  assert.equal(manPagesRepo(), "");
});

test("isTabFeatureEnabled enforces tab feature mapping and man cross-repo rule", () => {
  setProjects([
    { repo: "linux", project: "linux", available: true, features: { api: true, man: true, kconfig: false } },
    { repo: "man-pages", project: "man-pages", available: true, features: { man: true } },
  ]);
  assert.equal(tabTypeFeature("diff"), "compare");
  assert.equal(isTabFeatureEnabled("linux", "explorer"), true);
  assert.equal(isTabFeatureEnabled("linux", "api"), true);
  assert.equal(isTabFeatureEnabled("linux", "kconfig"), false);
  assert.equal(isTabFeatureEnabled("linux", "man"), true);
  assert.equal(isTabFeatureEnabled("linux", "cve"), true);
  assert.equal(isTabFeatureEnabled("man-pages", "api"), false);
  assert.equal(isTabFeatureEnabled("man-pages", "cve"), false);
  assert.equal(isTabFeatureEnabled("man-pages", "man"), true);

  // Without a man-pages repo, man tab is disabled on linux even if linux.features.man is true
  setProjects([{ repo: "linux", project: "linux", available: true, features: { man: true } }]);
  assert.equal(isTabFeatureEnabled("linux", "man"), false);
});

test("Compare mode filtering, subtitle generation, and disabled mode fallback", () => {
  setProjects([
    { repo: "linux-full", project: "linux", available: true },
    { repo: "linux-files-only", project: "linux", available: true, features: { kconfig: false, api: false, man: false } },
    { repo: "man-pages", project: "man-pages", available: true },
  ]);

  const fullModes = compareModesForRepo("linux-full");
  assert.deepEqual(fullModes, ["files", "kconfig", "api"]);
  assert.equal(resolveCompareMode("api", fullModes, false), "api");
  assert.equal(resolveCompareMode("man", fullModes, false), "files");

  const filesOnlyModes = compareModesForRepo("linux-files-only");
  assert.deepEqual(filesOnlyModes, ["files"]);
  assert.equal(compareSubtitleForModes(filesOnlyModes, false), "Files changed between any two release tags or commits.");
  assert.equal(resolveCompareMode("kconfig", filesOnlyModes, false), "files");

  const manModes = compareModesForRepo("man-pages");
  assert.deepEqual(manModes, ["man", "files"]);
  assert.equal(resolveCompareMode("api", manModes, true), "man");
  assert.equal(resolveCompareMode("files", manModes, true), "files");
});

test("Insights sections filter visibility by repo features", () => {
  setProjects([
    { repo: "linux", project: "linux", available: true, features: { commits: true, maintainers: false, stable: false, cve: true, kconfig: false, mail: false } },
    { repo: "man-pages", project: "man-pages", available: true },
  ]);

  const visibleFor = (repo) => insightsSections().filter((s) => !s.visible || s.visible(repo)).map((s) => s.id);
  assert.deepEqual(visibleFor("linux"), ["release-stats", "security"]);
  assert.deepEqual(visibleFor("man-pages"), ["release-stats"]);
});

test("relationsHtml hides CVE and Mail sections when disabled", () => {
  const rel = {
    cves: { fixes: ["CVE-2024-0001"] },
    links: ["msg-1@example.org"],
  };
  const both = relationsHtml(rel);
  assert.match(both, /rel-cve/);
  assert.match(both, /rel-mail/);

  const noCveNoMail = relationsHtml(rel, { showCve: false, showMail: false });
  assert.doesNotMatch(noCveNoMail, /rel-cve/);
  assert.doesNotMatch(noCveNoMail, /rel-mail/);
});

test("urlSync.applyHash falls back to explorer with notice when tab feature is disabled", () => {
  setProjects([
    { repo: "man-pages", project: "man-pages", available: true },
  ]);
  const notices = [];
  urlSync.setNoticeHandler((msg) => notices.push(msg));
  urlSync.setRepoHandler((repo) => ({ repo, version: "man-pages-6.9" }));
  urlSync.applyHash("repo=man-pages&tab=api");

  const active = state.getActiveTab();
  assert.equal(active.type, "explorer");
  assert.equal(active.repo, "man-pages");
  assert.equal(notices.length, 1);
  assert.match(notices[0], /disabled/i);
});
