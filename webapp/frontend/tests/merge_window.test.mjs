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

const { InsightsView, insightsSections, resolveSectionRelease } = await import("../js/views/insights/insights_view.js");
const { isGenericRef, mergeRowTag, repoNameFromUrl } = await import("../js/views/insights/release_sections.js");
await import("../js/views/insights/stable_section.js");
await import("../js/views/insights/security_section.js");
const { setProjects } = await import("../js/utils/projects.js");

const mwSection = () => insightsSections().find((s) => s.id === "merge-window");

test("generic refs fall back to the git repository name", () => {
  for (const ref of ["for-linus", "for_linus", "misc", "linux-next", "fixes-for-linus", "master", "main", "HEAD", "urgent", "for-v7.3-rc3", "2026-02-01-fixes"]) {
    assert.equal(isGenericRef(ref), true, ref);
  }
  for (const ref of ["edac_urgent_for_v7.3_rc3", "net-3.1", "kbuild-fixes", "akpm", "staging-linus"]) {
    assert.equal(isGenericRef(ref), false, ref);
  }
  assert.equal(repoNameFromUrl("git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6"), "kbuild-2.6");
  assert.equal(repoNameFromUrl("git://git.kernel.org/pub/scm/linux/kernel/git/davem/net-next.git/"), "net-next");
  assert.equal(repoNameFromUrl("git://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"), "torvalds/linux");
  assert.equal(mergeRowTag({
    ref: "for-linus",
    source_url: "git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6",
  }), "kbuild-2.6");
  assert.equal(mergeRowTag({
    ref: "edac_urgent_for_v7.3_rc3",
    source_url: "git://git.kernel.org/pub/scm/linux/kernel/git/ras/ras",
  }), "edac_urgent_for_v7.3_rc3");
});

test("merge-window rowHtml shows merge tag or git repo name and toggles sender via ctx.showSender", () => {
  const sec = mwSection();
  const edacPull = {
    day: 4.2,
    sender: "Borislav Petkov",
    ref: "edac_urgent_for_v7.3_rc3",
    subject: "Merge tag 'edac_urgent_for_v7.3_rc3'",
    section: { sec_id: 12, name: "EDAC" },
    commits: 3,
  };
  const hiddenSender = sec.rowHtml(edacPull, { showSender: false });
  assert.ok(!hiddenSender.includes("Borislav Petkov"));
  assert.ok(hiddenSender.includes("edac_urgent_for_v7.3_rc3"));
  assert.ok(hiddenSender.includes("EDAC"));

  const shownSender = sec.rowHtml(edacPull, { showSender: true });
  assert.ok(shownSender.includes("Borislav Petkov"));
  assert.ok(shownSender.includes("edac_urgent_for_v7.3_rc3"));
  assert.ok(shownSender.includes("EDAC"));

  const genericRef = sec.rowHtml({
    day: 6.0,
    sender: "Michal Marek",
    ref: "for-linus",
    source_url: "git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6",
    subject: "Merge branch 'for-linus' of git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6",
    section: null,
    commits: 8,
  });
  assert.ok(!genericRef.includes("Michal Marek"));
  assert.ok(genericRef.includes("kbuild-2.6"));
  assert.ok(!genericRef.includes("for-linus<"));

  const withoutRef = sec.rowHtml({
    day: 5,
    sender: "Linus Torvalds",
    ref: "",
    subject: "Merge branch 'misc-fixes'",
    section: null,
    commits: 1,
  }, { showSender: true });
  assert.ok(withoutRef.includes("Linus Torvalds"));
  assert.ok(withoutRef.includes("Merge branch 'misc-fixes'"));
});

test("merge-window afterDetail highlights selected pull in sv-chart", async () => {
  const sec = mwSection();
  const pulls = [
    { merge: "a".repeat(40), parents: ["1".repeat(40), "2".repeat(40)], day: 2.5, after_rc1: false, commits: 10, sender: "A", ref: "a", subject: "Merge A", section: null },
    { merge: "b".repeat(40), parents: ["1".repeat(40), "3".repeat(40)], day: 18.4, after_rc1: true, commits: 25, sender: "B", ref: "b", subject: "Merge B", section: { sec_id: null, name: "EDAC" } },
  ];
  const fakeApi = {
    async getInsights() {
      return {
        release: "v7.3",
        prev: "v7.2",
        rc1: "v7.3-rc1",
        rc1_day: 14.0,
        pulls,
        totals: { pulls: 2, commits: 35, before_rc1: 1, by_day: [{ day: 2, commits: 10 }, { day: 18, commits: 25 }] },
      };
    },
  };
  const wrapEl = { innerHTML: "" };
  const fakeView = {
    repo: () => "linux",
    containerEl: { querySelector: (sel) => (sel === ".sv-chart-wrap" ? wrapEl : null) },
  };
  const listRes = await sec.fetchList({ release: "v7.3", api: fakeApi, view: fakeView });
  assert.ok(listRes.summaryHtml.includes('class="sv-chart-wrap"'));
  assert.ok(!listRes.summaryHtml.includes("sv-bar-active"));

  sec.afterDetail(null, pulls[1], { release: "v7.3", view: fakeView });
  assert.ok(wrapEl.innerHTML.includes('data-x="18" class="sv-bar-active"'));
  assert.ok(wrapEl.innerHTML.includes("d18.4"));

  const detailHtml = sec.detailHtml(pulls[1], { release: "v7.3", view: fakeView });
  assert.ok(detailHtml.includes("<dt>Subsystem</dt><dd>EDAC</dd>"));
  assert.ok(!detailHtml.includes("data-section"));
});

test("insights sections resolve header tags, commits, and latest without sv-release", async () => {
  const { state } = await import("../js/state.js");
  const realOpenTab = state.openTab;
  const opened = [];
  state.openTab = (tab) => opened.push(tab);
  try {
    for (const id of ["release-stats", "merge-window", "subsystems", "stable", "security"]) {
      const sec = insightsSections().find((s) => s.id === id);
      assert.equal(Boolean(sec.releaseScoped), false, `${id} should not set releaseScoped`);
      assert.equal(sec.releaseOptions, undefined, `${id} should not define releaseOptions`);
    }

    // resolveSectionRelease across tags, commits, -rc, stable updates, and latest
    const exactFinal = { rev: "v7.3", tag: "v7.3", exact: true, latest: false, final: "v7.3", series: "v7.3" };
    assert.equal(resolveSectionRelease("release-stats", exactFinal), "v7.3");
    assert.equal(resolveSectionRelease("merge-window", exactFinal), "v7.3");
    assert.equal(resolveSectionRelease("stable", exactFinal), "v7.3");
    assert.equal(resolveSectionRelease("security", exactFinal), "v7.3");

    const stableTag = { rev: "v3.0.5", tag: "v3.0.5", exact: true, latest: false, final: "v3.0", series: "v3.0" };
    assert.equal(resolveSectionRelease("release-stats", stableTag), "v3.0.5");
    assert.equal(resolveSectionRelease("merge-window", stableTag), "v3.0");
    assert.equal(resolveSectionRelease("stable", stableTag), "v3.0");
    assert.equal(resolveSectionRelease("security", stableTag), "v3.0.5");

    const unreleasedRc = { rev: "v7.4-rc2", tag: "v7.4-rc2", exact: true, latest: true, final: null, series: "v7.4" };
    assert.equal(resolveSectionRelease("release-stats", unreleasedRc), "v7.4-rc2");
    assert.equal(resolveSectionRelease("merge-window", unreleasedRc), "latest");
    assert.equal(resolveSectionRelease("subsystems", unreleasedRc), "v7.4-rc2");
    assert.equal(resolveSectionRelease("security", unreleasedRc), "v7.4-rc2");

    const commitInRelease = { rev: "a".repeat(40), tag: "v3.0", exact: false, latest: false, final: "v3.0", series: "v3.0" };
    for (const id of ["release-stats", "merge-window", "subsystems", "stable", "security"]) {
      assert.equal(resolveSectionRelease(id, commitInRelease), "v3.0", id);
    }

    const unreleasedCommit = { rev: "b".repeat(40), tag: "v7.3", exact: false, latest: true, final: "v7.3", series: "v7.3" };
    assert.equal(resolveSectionRelease("release-stats", unreleasedCommit), "latest");
    assert.equal(resolveSectionRelease("merge-window", unreleasedCommit), "latest");
    assert.equal(resolveSectionRelease("subsystems", unreleasedCommit), "latest");
    assert.equal(resolveSectionRelease("stable", unreleasedCommit), "v7.3");
    assert.equal(resolveSectionRelease("security", unreleasedCommit), "v7.3");

    const statsSec = insightsSections().find((s) => s.id === "release-stats");
    let capturedOpts = null;
    const seriesUrls = [];
    const latestView = { version: "master", defaultReleases: async () => ["v7.3", "v7.2"] };
    const statsRes = await statsSec.fetchList({
      release: "latest",
      view: latestView,
      api: {
        async getInsights(_ver, _path, opts) {
          capturedOpts = opts;
          return {
            release: "latest", prev: "v7.3", branch: "master", head: "a".repeat(40),
            source: "git", commits: 5, merges: 1, authors: 2, first_time_authors: 1,
            files_changed: 3, lines_added: 10, lines_removed: 2, days: 4, rc_count: 0,
            top: { authors: [{ person: "Alice", email: "a@example.com", count: 5, share: 1 }] },
            first_timers: [], employers: [],
          };
        },
        async request(url) {
          seriesUrls.push(url);
          return { series: [{ release: "v7.2", commits: 100 }, { release: "v7.3", commits: 120 }] };
        },
      },
    });
    assert.equal(capturedOpts.networkFirst, true);
    assert.ok(statsRes.note.includes("v7.3..master (unreleased)"));
    assert.ok(seriesUrls[0].includes("tag=v7.2") && seriesUrls[0].includes("tag=v7.3"));

    // Trend sparkline for a non-final tag (v3.0.5) in another view does not overwrite latestView's targetRev
    const oldSeriesUrls = [];
    await statsSec.fetchList({
      release: "v3.0.5",
      view: { version: "v3.0.5", defaultReleases: async () => ["v7.3", "v7.2", "v3.1", "v3.0", "v2.6.39"] },
      api: {
        async getInsights() {
          return { release: "v3.0.5", prev: "v3.0.4", source: "git", commits: 3, merges: 0, authors: 1, first_time_authors: null, files_changed: 1, lines_added: 1, lines_removed: 0, days: 2, rc_count: 0, top: { authors: [] }, first_timers: [], employers: [] };
        },
        async request(url) {
          oldSeriesUrls.push(url);
          return { series: [{ release: "v2.6.39", commits: 50 }, { release: "v3.0", commits: 80 }] };
        },
      },
    });
    assert.ok(oldSeriesUrls[0].includes("tag=v3.0") && !oldSeriesUrls[0].includes("tag=v7.3"));

    statsSec.onDetailClick({
      preventDefault() {},
      target: { closest: (sel) => (sel === "[data-query]" ? { dataset: { query: "author:a@example.com in:latest" } } : null) },
    }, { release: "latest", view: latestView });
    assert.equal(opened.at(-1).version, "master");
    assert.equal(opened.at(-1).query, "author:a@example.com in:latest");

    const mwSec = insightsSections().find((s) => s.id === "merge-window");
    const mwRes = await mwSec.fetchList({
      release: "latest",
      api: {
        async getInsights(_ver, _path, opts) {
          assert.equal(opts.networkFirst, true);
          return { release: "latest", prev: "v7.3", branch: "master", head: "b".repeat(40), rc1: null, rc1_day: null, pulls: [], totals: { pulls: 0, commits: 0, before_rc1: 0, by_day: [] } };
        },
      },
    });
    assert.equal(mwRes.note, "v7.3..master (unreleased) · unreleased cycle · computed from Git");

    const mwUnavailable = await mwSec.fetchList({ release: "", resolved: { tag: "v3.1-rc1" } });
    assert.deepEqual(mwUnavailable.rows, []);
    assert.ok(mwUnavailable.emptyText.includes("final release"));

    const stableSec = insightsSections().find((s) => s.id === "stable");
    const stableUnavail = await stableSec.fetchList({
      version: "v3.1",
      release: "v3.1",
      api: { async getInsights() { return { available: false, reason: "The repository has no stable tags of v3.1.y", candidates: [] }; } },
    });
    assert.ok(stableUnavail.emptyText.includes("not available"));

    const subSec = insightsSections().find((s) => s.id === "subsystems");
    assert.equal(subSec.rowKey({ sec_id: null, name: "ALPHA" }), "ALPHA");
    assert.equal(subSec.rowKey({ sec_id: 7, name: "ALPHA" }), "7");
    const subDetail = subSec.detailHtml({
      sec_id: null, name: "ALPHA DRIVERS", status: "Supported", commits: 2, per_release: [1, 1],
      authors: 1, bus_factor: 1, review_coverage: 0.5, flags: [], maintainers: [], top_authors: [],
    }, { release: "latest" });
    assert.ok(!subDetail.includes("data-section"));
    assert.ok(subDetail.includes('section:&quot;ALPHA DRIVERS&quot; in:latest'));

    // InsightsView renders without .sv-release and autodetects tag when version is a commit
    const mk = (tag = "div") => {
      const el = document.createElement(tag);
      el.clientHeight = 400;
      el.setAttribute ??= () => {};
      el.removeAttribute ??= () => {};
      return el;
    };
    const listEl = mk("div");
    const vlEls = { ".vl-viewport": mk("div"), ".vl-spacer": mk("div"), ".vl-rows": mk("div") };
    listEl.querySelector = (sel) => vlEls[sel] || null;
    const els = {
      ".sv-container": mk("div"), ".sv-sections": mk("div"), ".sv-detected": mk("span"),
      ".sv-arch": mk("select"), ".sv-window": mk("select"), ".sv-search": mk("input"),
      ".sv-toggles": mk("span"), ".sv-chips": mk("div"), ".sv-download": mk("button"),
      ".sv-collapse": mk("button"), ".sv-rail": mk("button"), ".sv-detail": mk("div"),
      ".sv-list": listEl, ".sv-status": mk("div"), ".sv-summary": mk("div"),
    };
    const containerEl = {
      isConnected: true,
      _html: "",
      set innerHTML(v) { this._html = v; },
      get innerHTML() { return this._html; },
      querySelector: (sel) => els[sel] || null,
      querySelectorAll: () => [],
    };
    const calls = { resolved: [], insights: [] };
    const view = new InsightsView();
    view.api = {
      async resolveInsightsTag(rev) {
        calls.resolved.push(rev);
        if (rev === "abcdef123456") {
          return { rev, sha: "a".repeat(40), tag: "v3.0", exact: false, latest: false, source: "release_index", final: "v3.0", series: "v3.0" };
        }
        return { rev, sha: "b".repeat(40), tag: rev, exact: true, latest: false, source: "tag", final: rev, series: rev };
      },
      async getInsights(ver, path) {
        calls.insights.push({ ver, path });
        return { release: ver, prev: "v2.6.39", source: "database", commits: 10, merges: 2, authors: 3, first_time_authors: 1, files_changed: 5, lines_added: 20, lines_removed: 4, days: 60, rc_count: 7, top: { authors: [] }, first_timers: [], employers: [] };
      },
      async getVersionsCached() { return { versions: ["v3.0", "v2.6.39"] }; },
      async request() { return { series: [] }; },
    };
    const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
    await view.render(containerEl, { id: "tab-ins-1", type: "insights", view: "release-stats", version: "abcdef123456" });
    await flush();
    assert.ok(!containerEl.innerHTML.includes("sv-release"));
    assert.deepEqual(calls.resolved, ["abcdef123456"]);
    assert.equal(calls.insights[0].ver, "v3.0");
    assert.equal(els[".sv-detected"].hidden, false);
    assert.equal(els[".sv-detected"].textContent, "tag: v3.0");

    await view.render(containerEl, { id: "tab-ins-1", type: "insights", view: "release-stats", version: "v3.0" });
    await flush();
    assert.equal(els[".sv-detected"].hidden, true);
    view.dispose();
  } finally {
    state.openTab = realOpenTab;
  }
});

test("project-specific isGenericRef, repoNameFromUrl, and mergeRowTag respect repoInsights configuration", () => {
  setProjects([
    {
      repo: "my-project",
      project: "my-project",
      available: true,
      insights: {
        generic_ref_words: ["customref", "mysync"],
        generic_repo_names: ["custom-repo", "custom-core"],
      },
    },
  ]);

  // Project-specific generic ref words
  assert.equal(isGenericRef("customref", "my-project"), true);
  assert.equal(isGenericRef("mysync", "my-project"), true);
  assert.equal(isGenericRef("customref", "linux"), false);

  // Project-specific generic repo names
  assert.equal(
    repoNameFromUrl("https://git.example.org/team/custom-repo.git", "my-project"),
    "team/custom-repo"
  );
  assert.equal(
    repoNameFromUrl("https://git.example.org/team/custom-core.git", "my-project"),
    "team/custom-core"
  );
  assert.equal(
    repoNameFromUrl("https://git.example.org/team/custom-repo.git", "linux"),
    "custom-repo"
  );

  // mergeRowTag with project-specific settings
  assert.equal(
    mergeRowTag(
      {
        ref: "customref",
        source_url: "https://git.example.org/team/custom-repo.git",
      },
      "my-project"
    ),
    "team/custom-repo"
  );
  assert.equal(
    mergeRowTag(
      {
        ref: "customref",
        source_url: "https://git.example.org/team/custom-repo.git",
      },
      "linux"
    ),
    "customref"
  );
});
