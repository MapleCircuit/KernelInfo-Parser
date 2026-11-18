/**
 * security_insights.test.mjs - Tests for Insights · Security (`&tab=insights&view=security`),
 * including "Introduced by" first-final-release sv-chips, dynamic CVSS operator + numeric threshold
 * filtering/clamping/debouncing, dynamic summary KPIs/bars, and null/RC-only edge cases.
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
const { tabRoute } = await import("../js/tab_routes.js");
const { state } = await import("../js/state.js");
const {
  clampCvssScore,
  filterRows,
  matchesCvssScore,
  normalizeCvssOp,
  parseChips,
  parseCvssInput,
  sectionParamsFromState,
  sectionStateFromParams,
} = await import("../js/views/sections/section_model.js");
const { InsightsView, insightsSections } = await import("../js/views/insights/insights_view.js");
const {
  buildSecurityChips,
  buildSecuritySummaryHtml,
  cvssFilterParams,
  securityCvesPath,
  targetFinalRelease,
} = await import("../js/views/insights/security_section.js");

const securitySec = () => insightsSections().find((s) => s.id === "security");

function makeInteractiveDom() {
  const mk = (tag = "div") => {
    const el = new FakeElement(tag);
    el.clientHeight = 400;
    el.attrs = {};
    el.setAttribute = (k, v) => { el.attrs[k] = String(v); };
    el.getAttribute = (k) => el.attrs[k] ?? null;
    el.removeAttribute = (k) => { delete el.attrs[k]; };
    el.classList = {
      _set: new Set(),
      add(...xs) { xs.forEach((x) => this._set.add(x)); },
      remove(...xs) { xs.forEach((x) => this._set.delete(x)); },
      toggle(x, on) { if (on === undefined) { if (this._set.has(x)) this._set.delete(x); else this._set.add(x); } else if (on) this._set.add(x); else this._set.delete(x); },
      contains(x) { return this._set.has(x); },
    };
    return el;
  };

  const listEl = mk("div");
  const vlEls = { ".vl-viewport": mk("div"), ".vl-spacer": mk("div"), ".vl-rows": mk("div") };
  listEl.querySelector = (sel) => vlEls[sel] || null;

  const extraEl = mk("div");
  extraEl.isConnected = true;
  const cvssEls = {
    ".sv-cvss-control": mk("span"),
    ".sv-cvss-toggle": mk("button"),
    ".sv-cvss-op": mk("select"),
    ".sv-cvss-input": mk("input"),
    ".sv-cvss-count": mk("span"),
  };
  cvssEls[".sv-cvss-op"].value = "ge";
  cvssEls[".sv-cvss-input"].value = "7";
  cvssEls[".sv-cvss-control"].querySelector = (sel) => cvssEls[sel] || null;
  extraEl.querySelector = (sel) => cvssEls[sel] || null;

  const chipsEl = mk("div");
  const els = {
    ".sv-container": mk("div"),
    ".sv-sections": mk("div"),
    ".sv-detected": mk("span"),
    ".sv-arch": mk("select"),
    ".sv-window": mk("select"),
    ".sv-search": mk("input"),
    ".sv-toggles": mk("span"),
    ".sv-chips": chipsEl,
    ".sv-filter-extra": extraEl,
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
  return { containerEl, els, cvssEls };
}

test("CVSS normalization, clamping, intermediate input parsing, and operator matching", () => {
  assert.equal(normalizeCvssOp("≥"), "ge");
  assert.equal(normalizeCvssOp(">="), "ge");
  assert.equal(normalizeCvssOp(">"), "gt");
  assert.equal(normalizeCvssOp("<"), "lt");
  assert.equal(normalizeCvssOp("="), "eq");
  assert.equal(normalizeCvssOp("bad"), "");

  // Clamping [0.0, 10.0]
  assert.equal(clampCvssScore(-3.4), 0);
  assert.equal(clampCvssScore(12.8), 10);
  assert.equal(clampCvssScore(7.54), 7.5);
  assert.equal(clampCvssScore(null), null);

  // Incomplete intermediate keystrokes return null
  assert.equal(parseCvssInput(""), null);
  assert.equal(parseCvssInput("7."), null);
  assert.equal(parseCvssInput("-"), null);
  assert.equal(parseCvssInput("7.5"), 7.5);
  assert.equal(parseCvssInput("15"), 10);
  assert.equal(parseCvssInput("-1"), 0);

  // Null/unscored CVEs are excluded across ALL operators (especially `<`)
  for (const op of ["lt", "gt", "eq", "ge"]) {
    assert.equal(matchesCvssScore(null, op, 7.5), false);
    assert.equal(matchesCvssScore(undefined, op, 10), false);
    assert.equal(matchesCvssScore(NaN, op, 5), false);
  }

  // Decimal precision & epsilon `< 0.05` / 1-decimal rounding for `=`
  assert.equal(matchesCvssScore(7.5, "eq", 7.5), true);
  assert.equal(matchesCvssScore(7.499999999999999, "eq", 7.5), true);
  assert.equal(matchesCvssScore(9.8, "eq", 9.8), true);
  assert.equal(matchesCvssScore(7.4, "eq", 7.5), false);

  assert.equal(matchesCvssScore(7.4, "lt", 7.5), true);
  assert.equal(matchesCvssScore(7.5, "lt", 7.5), false);
  assert.equal(matchesCvssScore(7.6, "gt", 7.5), true);
  assert.equal(matchesCvssScore(7.5, "gt", 7.5), false);
  assert.equal(matchesCvssScore(7.5, "ge", 7.5), true);
  assert.equal(matchesCvssScore(7.4, "ge", 7.5), false);
});

test("Security section URL state roundtrip and query path construction", () => {
  const route = tabRoute("insights");
  const tab = {
    view: "security",
    query: "net",
    filter: "affected,high,intro:v6.1",
    cvssOp: "lt",
    cvssScore: 7.5,
    sel: "CVE-2024-0001",
    listCollapsed: false,
  };
  const params = new URLSearchParams();
  route.toHash(tab, params);
  assert.equal(params.get("cvss_op"), "lt");
  assert.equal(params.get("cvss"), "7.5");

  const parsed = sectionStateFromParams(params);
  assert.equal(parsed.cvssOp, "lt");
  assert.equal(parsed.cvssScore, 7.5);
  assert.equal(parsed.filter, "affected,high,intro:v6.1");

  const out2 = new URLSearchParams();
  sectionParamsFromState(parsed, out2);
  assert.equal(out2.get("cvss_op"), "lt");
  assert.equal(out2.get("cvss"), "7.5");

  assert.equal(securityCvesPath({}), "cves");
  assert.equal(securityCvesPath({ filter: "high" }), "cves?cvss_op=ge&cvss_score=7");
  assert.equal(securityCvesPath({ filter: "high", cvssOp: "eq", cvssScore: 9.8 }), "cves?cvss_op=eq&cvss_score=9.8");
  assert.deepEqual(cvssFilterParams({}), { active: false, op: "ge", score: 7 });
});

test("buildSecurityChips provides single 'Introduced by' sv-chip matching the selected version's first final release", () => {
  const sec = securitySec();
  const rows = [
    { id: "CVE-2024-0001", title: "bug1", state: "affected", basis: "record", score: 7.5, introduced_release: "v6.1" },
    { id: "CVE-2024-0002", title: "bug2", state: "fixed", basis: "record", score: 5.5, introduced_release: "v5.15", fixed_here: true },
    { id: "CVE-2024-0003", title: "rc-only", state: "affected", basis: "record", score: null, introduced_release: null },
    { id: "CVE-2024-0004", title: "unversioned", state: "possibly", basis: "reference", score: 8.2, introduced_release: null },
  ];
  const ctx61 = { release: "v6.1", resolved: { tag: "v6.1", final: "v6.1" }, view: { tab: {} } };
  const chips = buildSecurityChips(rows, ctx61);
  const introChips = chips.filter((c) => c.id === "introduced");
  assert.equal(introChips.length, 1);
  assert.equal(introChips[0].label, "Introduced by");
  assert.ok(!chips.some((c) => c.id.startsWith("intro:")));

  // Filters out CVEs that did not start in the current selected version (v5.15, null/RC-only excluded)
  const only61 = filterRows(sec, rows, "", parseChips("introduced"), ctx61);
  assert.deepEqual(only61.map((r) => r.id), ["CVE-2024-0001"]);
  const combined = filterRows(sec, rows, "", parseChips("fixed,introduced"), ctx61);
  assert.deepEqual(combined.length, 0);

  // RC tag whose final release exists resolves to v6.1; RC-only without final release matches nothing (never null === null)
  assert.equal(targetFinalRelease({ release: "v6.1-rc2", resolved: { final: "v6.1" } }), "v6.1");
  assert.equal(targetFinalRelease({ release: "v6.2-rc1", resolved: { final: null } }), "");
  const rcOnlyFiltered = filterRows(sec, rows, "", parseChips("introduced"), { release: "v6.2-rc1", resolved: { final: null } });
  assert.deepEqual(rcOnlyFiltered.length, 0);

  // Summary KPIs & severity bars react to filtered rows
  const sumAll = buildSecuritySummaryHtml(rows, rows);
  assert.match(sumAll, /sv-kpi/);
  assert.match(sumAll, /Unscored/);
  const sum61 = buildSecuritySummaryHtml(only61, rows);
  assert.match(sum61, /7\.5/);
  assert.doesNotMatch(sum61, /Unscored/);
});

test("InsightsView security end-to-end: 'Introduced by' sv-chip, CVSS control debounce/clamping, and summary updates", async () => {
  const realUpdateTabState = state.updateTabState;
  const allRows = [
    { id: "CVE-2024-0001", title: "net UAF", state: "affected", basis: "record", score: 7.5, introduced_release: "v6.6", fixes: [], cwes: [] },
    { id: "CVE-2024-0002", title: "fs null", state: "fixed", basis: "record", score: 5.2, introduced_release: "v5.15", fixed_here: true, fixes: [], cwes: [] },
    { id: "CVE-2024-0003", title: "rc-only bug", state: "affected", basis: "record", score: null, introduced_release: null, fixes: [], cwes: [] },
  ];
  const calls = [];
  const { containerEl, els, cvssEls } = makeInteractiveDom();
  const tab = { id: "tab-sec-1", type: "insights", view: "security", version: "v6.6" };
  state.updateTabState = (_id, patch) => { Object.assign(tab, patch); };

  const view = new InsightsView();
  view.api = {
    async resolveInsightsTag(rev) {
      return { rev, sha: "a".repeat(40), tag: "v6.6", exact: true, latest: false, source: "tag", final: "v6.6", series: "v6.6" };
    },
    async getInsights(ver, path) {
      calls.push({ ver, path });
      const u = new URL(`http://localhost/${path}`);
      const op = u.searchParams.get("cvss_op");
      const score = u.searchParams.get("cvss_score");
      let rows = allRows;
      if (op && score !== null) {
        rows = allRows.filter((r) => matchesCvssScore(r.score, op, Number(score)));
      }
      const counts = {
        affected: rows.filter((r) => r.state === "affected").length,
        possibly: rows.filter((r) => r.state === "possibly").length,
        fixed: rows.filter((r) => r.state === "fixed").length,
        fixed_here: rows.filter((r) => r.fixed_here).length,
      };
      return { release: ver, head: "a".repeat(40), layout: "vulns", counts, cves: rows };
    },
  };

  const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
  try {
    await view.render(containerEl, tab);
    await flush();

    // Initial load: unfiltered, single "Introduced by" sv-chip rendered with count 1 (v6.6 only, ignoring v5.15 & null)
    assert.deepEqual(calls.map((c) => c.path), ["cves"]);
    assert.match(els[".sv-chips"].innerHTML, /data-chip="introduced"[^>]*>Introduced by <span>1<\/span>/);
    assert.doesNotMatch(els[".sv-chips"].innerHTML, /data-chip="intro:/);
    assert.match(els[".sv-filter-extra"].innerHTML, /type="number" class="sv-cvss-input" min="0" max="10" step="0\.1"/);
    assert.equal(cvssEls[".sv-cvss-count"].textContent, "1"); // 7.5 >= 7
    assert.match(els[".sv-summary"].innerHTML, /Unscored/);

    // Clicking "Introduced by" sv-chip filters out CVEs that did not start in v6.6 & updates summary KPIs
    els[".sv-chips"].dispatchEvent({
      type: "click",
      target: { closest: (sel) => (sel === "[data-chip]" ? { dataset: { chip: "introduced" } } : null) },
    });
    assert.equal(view.filtered.length, 1);
    assert.equal(view.filtered[0].id, "CVE-2024-0001");
    assert.match(els[".sv-chips"].innerHTML, /class="sv-chip active" data-chip="introduced"/);
    assert.match(els[".sv-status"].innerHTML, /^1 of 3/);

    // Clear "Introduced by" chip, then type incomplete keystrokes ("", "7.") -> must NOT fire API requests
    els[".sv-chips"].dispatchEvent({
      type: "click",
      target: { closest: (sel) => (sel === "[data-chip]" ? { dataset: { chip: "introduced" } } : null) },
    });
    const prevCallCount = calls.length;
    cvssEls[".sv-cvss-input"].value = "";
    cvssEls[".sv-cvss-input"].dispatchEvent({ type: "input" });
    cvssEls[".sv-cvss-input"].value = "7.";
    cvssEls[".sv-cvss-input"].dispatchEvent({ type: "input" });
    await new Promise((r) => realSetTimeout(r, 300));
    assert.equal(calls.length, prevCallCount);

    // Operator `<` with threshold `7.5` -> server query excludes null CVSS (CVE-2024-0003) and returns CVE-2024-0002 (5.2)
    cvssEls[".sv-cvss-op"].value = "lt";
    cvssEls[".sv-cvss-input"].value = "7.5";
    cvssEls[".sv-cvss-op"].dispatchEvent({ type: "change" });
    await flush();
    assert.equal(calls.at(-1).path, "cves?cvss_op=lt&cvss_score=7.5");
    assert.deepEqual(view.filtered.map((r) => r.id), ["CVE-2024-0002"]);
    assert.equal(cvssEls[".sv-cvss-count"].textContent, "1");
    assert.doesNotMatch(els[".sv-summary"].innerHTML, /Unscored/);

    // Out-of-range clamping on change: 15 -> 10.0, operator `=` -> matches none cleanly
    cvssEls[".sv-cvss-op"].value = "eq";
    cvssEls[".sv-cvss-input"].value = "15";
    cvssEls[".sv-cvss-input"].dispatchEvent({ type: "change" });
    await flush();
    assert.equal(cvssEls[".sv-cvss-input"].value, "10");
    assert.equal(calls.at(-1).path, "cves?cvss_op=eq&cvss_score=10");
    assert.equal(view.filtered.length, 0);

    // Unconfigured / uningested response clears chips rather than rendering 0-count chips
    view.api.getInsights = async () => ({ configured: false, ingested: false, total: 0, cves: [] });
    await view.loadList();
    await flush();
    assert.equal(els[".sv-chips"].innerHTML, "");
    assert.equal(els[".sv-filter-extra"].hidden, true);
    assert.match(els[".sv-summary"].innerHTML, /not configured/);
  } finally {
    state.updateTabState = realUpdateTabState;
    view.dispose();
  }
});
