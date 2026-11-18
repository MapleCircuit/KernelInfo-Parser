/**
 * insights_stable_subsystems.test.mjs - Tests for Insights Stable & Subsystem health fixes,
 * AbortController propagation, sync invalidation, and drilldown/UI safeguards.
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

await import("../js/routes.js");
const { api } = await import("../js/api.js");
const { isInsightsCacheKey } = await import("../js/sync.js");
const { InsightsView, insightsSections } = await import("../js/views/insights/insights_view.js");
const { buildStableSummaryHtml } = await import("../js/views/insights/stable_section.js");
await import("../js/views/insights/release_sections.js");

const secById = (id) => insightsSections().find((s) => s.id === id);

function makeDom() {
  const mk = (tag = "div") => {
    const el = new FakeElement(tag);
    el.clientHeight = 400;
    el.attrs = {};
    el.setAttribute = (k, v) => { el.attrs[k] = String(v); };
    el.getAttribute = (k) => el.attrs[k] ?? null;
    el.removeAttribute = (k) => { delete el.attrs[k]; };
    return el;
  };
  const listEl = mk("div");
  const vlEls = { ".vl-viewport": mk("div"), ".vl-spacer": mk("div"), ".vl-rows": mk("div") };
  listEl.querySelector = (sel) => vlEls[sel] || null;
  const els = {
    ".sv-container": mk("div"),
    ".sv-sections": mk("div"),
    ".sv-detected": mk("span"),
    ".sv-arch": mk("select"),
    ".sv-window": mk("select"),
    ".sv-search": mk("input"),
    ".sv-toggles": mk("span"),
    ".sv-chips": mk("div"),
    ".sv-filter-extra": mk("div"),
    ".sv-download": mk("button"),
    ".sv-collapse": mk("button"),
    ".sv-rail": mk("button"),
    ".sv-detail": mk("div"),
    ".sv-list": listEl,
    ".sv-status": mk("div"),
    ".sv-summary": mk("div"),
  };
  const containerEl = {
    isConnected: true,
    _html: "",
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    querySelector: (sel) => els[sel] || null,
    querySelectorAll: () => [],
  };
  return { containerEl, els };
}

test("stable section passes ctx.release (avoiding slash-branch 404), sets needsListForDetail, and renders summary KPIs", async () => {
  const sec = secById("stable");
  assert.equal(sec.needsListForDetail, true);

  let calledVer = null;
  let calledPath = null;
  const res = await sec.fetchList({
    version: "origin/master",
    release: "v6.6",
    api: {
      _vk: (v) => `${v}@stamp~`,
      async getInsights(ver, path) {
        calledVer = ver;
        calledPath = path;
        return {
          available: true,
          candidates: [],
          total: 0,
          candidates_checked: 42,
          stable_tags: 5,
          last_stable: "v6.6.5",
          summary: {
            available: true,
            releases: 5,
            commits: 120,
            backports: 110,
            fixes: 80,
            cc_stable: 30,
            cve_commits: 12,
            lines_added: 1500,
            lines_removed: 400,
            same_cycle_fixes: 25,
            cross_release_fixes: 55,
            avg_fix_latency_days: 14.25,
          },
        };
      },
    },
  });

  assert.equal(calledVer, "v6.6");
  assert.ok(calledPath.includes("series=v6.6"));
  assert.ok(res.summaryHtml.includes("120"));
  assert.ok(res.summaryHtml.includes("110"));
  assert.ok(res.summaryHtml.includes("80"));
  assert.ok(res.summaryHtml.includes("30"));
  assert.ok(res.summaryHtml.includes("12"));
  assert.ok(res.summaryHtml.includes("+1,500 −400"));
  assert.ok(res.summaryHtml.includes("25 / 55"));
  assert.ok(res.summaryHtml.includes("14.3 d"));

  const xssHtml = buildStableSummaryHtml({
    available: true,
    releases: "<script>alert(1)</script>",
    commits: 1,
    backports: 0,
    fixes: 0,
    cc_stable: 0,
    cve_commits: 0,
    lines_added: 0,
    lines_removed: 0,
    same_cycle_fixes: 0,
    cross_release_fixes: 0,
    avg_fix_latency_days: null,
  });
  assert.ok(!xssHtml.includes("<script>"));
});

test("subsystems section sets needsListForDetail, uses _vk cacheKey, handles missing F: patterns, unknown flags, and single-entry latest window", async () => {
  for (const id of ["release-stats", "merge-window", "subsystems"]) {
    assert.equal(secById(id).needsListForDetail, true, id);
  }
  const sub = secById("subsystems");
  let seenOpts = null;
  const fakeView = { containerEl: null };
  const listRes = await sub.fetchList({
    release: "latest",
    window: 4,
    view: fakeView,
    api: {
      _vk: (v) => `${v}@s1~`,
      async getInsights(_ver, _path, opts) {
        seenOpts = opts;
        return {
          release: "latest",
          branch: "origin/master",
          head: "a".repeat(40),
          window: ["latest"],
          source: "git",
          sections: [
            {
              sec_id: 1,
              name: "NET",
              status: "Maintained",
              commits: 0,
              per_release: [0],
              authors: 0,
              bus_factor: 0,
              review_coverage: 0,
              flags: ["no_activity", "custom_flag"],
              maintainers: [],
              top_authors: [],
              has_file_patterns: false,
            },
          ],
        };
      },
    },
  });

  assert.equal(seenOpts.cacheKey, "health_latest@s1~_4");
  assert.ok(listRes.note.startsWith("origin/master (unreleased) · "));
  assert.ok(!listRes.note.includes("latest.."));

  const rowHtml = sub.rowHtml(listRes.rows[0]);
  assert.ok(!rowHtml.includes("undefined"));
  assert.ok(rowHtml.includes("custom_flag"));

  const detailHtml = sub.detailHtml(listRes.rows[0], { release: "latest", view: fakeView });
  assert.ok(!detailHtml.includes("<h3>Top authors</h3>"));
  assert.ok(!detailHtml.includes("data-query="));
  assert.ok(detailHtml.includes("no F: patterns"));
});

test("InsightsView passes AbortSignal to resolveInsightsTag, preserves _resolveError on applyFilter, and switchSection clears window", async () => {
  const { containerEl, els } = makeDom();
  const view = new InsightsView();
  let seenSignal = null;
  view.api = {
    async resolveInsightsTag(_ver, opts) {
      seenSignal = opts?.signal || null;
      throw new Error("resolve boom");
    },
  };
  await view.render(containerEl, { id: "t1", type: "insights", view: "subsystems", version: "badref", window: 6 });
  assert.ok(seenSignal instanceof AbortSignal);
  assert.ok(els[".sv-status"].innerHTML.includes("resolve boom"));

  // Filtering / chip click must preserve _resolveError instead of resetting status to "Nothing to show."
  view.applyFilter();
  assert.ok(els[".sv-status"].innerHTML.includes("resolve boom"));

  // switchSection clears window: null so &w= does not leak across sections
  const persisted = [];
  const origPersist = view.persist.bind(view);
  view.persist = (patch) => {
    persisted.push(patch);
    origPersist(patch);
  };
  view.render = () => {};
  await view.switchSection("stable");
  assert.equal(persisted[0].window, null);
  view.dispose();
});

test("api.clearInsightsResolveMemo and sync.isInsightsCacheKey scope invalidation per repo", () => {
  api._insightsResolveMemo.set("linux|v6.6", { tag: "v6.6" });
  api._insightsResolveMemo.set("man-pages|man-pages-6.9", { tag: "man-pages-6.9" });
  api.clearInsightsResolveMemo("linux");
  assert.equal(api._insightsResolveMemo.has("linux|v6.6"), false);
  assert.equal(api._insightsResolveMemo.has("man-pages|man-pages-6.9"), true);
  api.clearInsightsResolveMemo();
  assert.equal(api._insightsResolveMemo.size, 0);

  assert.equal(isInsightsCacheKey("linux::health_v6.6_4", "linux"), true);
  assert.equal(isInsightsCacheKey("linux::stable_v6.6", "linux"), true);
  assert.equal(isInsightsCacheKey("linux::rstats2_v6.6", "linux"), true);
  assert.equal(isInsightsCacheKey("linux::mwin_v6.6", "linux"), true);
  assert.equal(isInsightsCacheKey("man-pages::health_v6.6_4", "linux"), false);
  assert.equal(isInsightsCacheKey("linux::file_v6.6_a.c", "linux"), false);
  assert.equal(isInsightsCacheKey("health_v6.6_4", ""), true);
  assert.equal(isInsightsCacheKey("other::health_v6.6_4", ""), false);
});
