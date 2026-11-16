/**
 * cve.test.mjs - Tests for the independent CVE tab route, feature gating, cve_render.js, and CveView.
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

await import("../js/routes.js");
const { tabRoute } = await import("../js/tab_routes.js");
const { isTabFeatureEnabled, setProjects } = await import("../js/utils/projects.js");
const {
  SETUP_HTML,
  NOT_INGESTED_HTML,
  STATE_TAG,
  STATE_LABEL,
  BASIS,
  KIND,
  severityTag,
  renderCveDetailHtml,
} = await import("../js/views/cve/cve_render.js");
const {
  CVE_CHIPS,
  CVE_SECTION_MODEL,
  CveView,
  cveRowHtml,
  cveTsv,
} = await import("../js/views/cve/cve_view.js");
const { filterRows, parseChips } = await import("../js/views/sections/section_model.js");

test("tabRoute('cve') roundtrips id/sel/cve aliases, ver, q, f, lc and navKey", () => {
  const route = tabRoute("cve");
  assert.ok(route);
  assert.equal(route.badge, "[V]");
  assert.deepEqual(route.keys, ["sel", "query", "filter", "cveVersion", "listCollapsed"]);

  const fromId = route.fromHash(new URLSearchParams("id=CVE-2024-12345&ver=6.6.15&q=net&f=kev,high&lc=1"));
  assert.equal(fromId.sel, "CVE-2024-12345");
  assert.equal(fromId.cveVersion, "6.6.15");
  assert.equal(fromId.query, "net");
  assert.equal(fromId.filter, "kev,high");
  assert.equal(fromId.listCollapsed, true);
  assert.equal(fromId.title, "CVE · CVE-2024-12345");

  const fromSel = route.fromHash(new URLSearchParams("sel=CVE-2024-99999"));
  assert.equal(fromSel.sel, "CVE-2024-99999");

  const fromCve = route.fromHash(new URLSearchParams("cve=CVE-2023-0001"));
  assert.equal(fromCve.sel, "CVE-2023-0001");

  const empty = route.fromHash(new URLSearchParams(""));
  assert.equal(empty.sel, "");
  assert.equal(empty.title, "CVEs");

  const out = new URLSearchParams();
  route.toHash(fromId, out);
  assert.equal(out.get("id"), "CVE-2024-12345");
  assert.equal(out.get("ver"), "6.6.15");
  assert.equal(out.get("q"), "net");
  assert.equal(out.get("f"), "kev,high");
  assert.equal(out.get("lc"), "1");

  assert.equal(route.navKey(fromId), "CVE-2024-12345:6.6.15");
});

test("isTabFeatureEnabled gates 'cve' per project", () => {
  setProjects([
    { repo: "linux", project: "linux", available: true },
    { repo: "linux-nocve", project: "linux", available: true, features: { cve: false } },
    { repo: "man-pages", project: "man-pages", available: true },
  ]);

  assert.equal(isTabFeatureEnabled("linux", "cve"), true);
  assert.equal(isTabFeatureEnabled("linux-nocve", "cve"), false);
  assert.equal(isTabFeatureEnabled("man-pages", "cve"), false);
});

test("cve_render.js exports constants, escapes HTML, and renders version_summary and check-release form", () => {
  assert.ok(SETUP_HTML.includes("cvelistV5"));
  assert.ok(NOT_INGESTED_HTML.includes("--cve"));
  assert.equal(STATE_TAG.affected, "bad");
  assert.equal(STATE_LABEL.fixed, "fixed");
  assert.ok(BASIS.record);
  assert.ok(KIND.cna);
  assert.match(severityTag(9.8, "CRITICAL"), /9\.8 critical/);

  const html = renderCveDetailHtml({
    id: "CVE-2024-12345",
    title: "Use after free <script>alert(1)</script>",
    url: "javascript:alert(1)",
    kind: "cna",
    published: "2024-02-01",
    description: "Details <b>bold</b>",
    cwes: ["CWE-416<img onerror=alert(1)>"],
    files: ["net/core/dev.c"],
    references: ["https://lore.kernel.org/r/123", "javascript:alert(2)", "data:text/html,<script>"],
    version_summary: {
      text: "Affects 6.1 through 6.6; fixed in 6.6.15 & 6.7",
      affected: "6.1 - 6.6.14",
      fixed: "6.6.15, 6.7",
      unfixed: false,
    },
    release_state: { state: "affected", release: "v6.6.10", basis: "record" },
  }, {
    showCopyLink: true,
    showCheckRelease: true,
    checkedRelease: "v6.6.10",
  });

  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(html, /<img /);
  assert.doesNotMatch(html, /<b>bold<\/b>/);
  assert.doesNotMatch(html, /javascript:/i);
  assert.doesNotMatch(html, /data:text\/html/i);
  assert.match(html, /https:\/\/www\.cve\.org\/CVERecord\?id=CVE-2024-12345/);
  assert.match(html, /https:\/\/lore\.kernel\.org\/r\/123/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /Applies to Versions/);
  assert.match(html, /6\.1 - 6\.6\.14/);
  assert.match(html, /6\.6\.15, 6\.7/);
  assert.match(html, /data-copy-cve="CVE-2024-12345"/);
  assert.match(html, /data-check-release="CVE-2024-12345"/);
  assert.match(html, /value="v6\.6\.10"/);

  const unfixedHtml = renderCveDetailHtml({
    id: "CVE-2024-00002",
    title: "Unfixed issue",
    version_summary: { text: "No fix yet", unfixed: true },
  });
  assert.match(unfixedHtml, /<span class="sv-tag bad">unfixed<\/span>/);
});

test("CveView row filtering, rowHtml, and TSV export", () => {
  const rows = [
    {
      id: "CVE-2024-1111",
      title: "net: fix skb leak",
      kind: "cna",
      score: 7.8,
      kev: "2024-03-01",
      unfixed: false,
      versions: "6.1..6.6.14",
      fixed_versions: ["6.6.15"],
      cwes: ["CWE-401"],
      files: ["net/core/skbuff.c"],
      published: "2024-02-15",
    },
    {
      id: "CVE-2024-2222",
      title: "ext4: null deref",
      kind: "text",
      score: 4.5,
      kev: "",
      unfixed: true,
      versions: "6.8",
      fixed_versions: [],
      cwes: ["CWE-476"],
      files: ["fs/ext4/inode.c"],
      published: "2024-04-01",
    },
  ];

  assert.equal(CVE_CHIPS.length, 6);
  const kevOnly = filterRows(CVE_SECTION_MODEL, rows, "", parseChips("kev"));
  assert.equal(kevOnly.length, 1);
  assert.equal(kevOnly[0].id, "CVE-2024-1111");

  const unfixedOnly = filterRows(CVE_SECTION_MODEL, rows, "", parseChips("unfixed"));
  assert.equal(unfixedOnly.length, 1);
  assert.equal(unfixedOnly[0].id, "CVE-2024-2222");

  const byFile = filterRows(CVE_SECTION_MODEL, rows, "skbuff.c", new Set());
  assert.equal(byFile.length, 1);
  assert.equal(byFile[0].id, "CVE-2024-1111");

  const rowMarkup = cveRowHtml(rows[1]);
  assert.match(rowMarkup, /unfixed/);
  assert.match(rowMarkup, /cve-ver-badge/);
  assert.match(rowMarkup, /CVE-2024-2222/);

  const tsv = cveTsv(rows);
  assert.match(tsv, /^id\tkind\tcvss\tkev\tpublished\tversions\tfixed\tunfixed\ttitle\n/);
  assert.match(tsv, /CVE-2024-1111\tcna\t7\.8\t2024-03-01\t2024-02-15\t6\.1\.\.6\.6\.14\t6\.6\.15\t\tnet: fix skb leak/);
  assert.match(tsv, /CVE-2024-2222\ttext\t4\.5\t\t2024-04-01\t6\.8\t\tyes\text4: null deref/);
});

function makeFakeCveHost() {
  const mk = (tag = "div") => {
    const el = document.createElement(tag);
    el.clientHeight = 400;
    return el;
  };
  const listEl = mk("div");
  const vlEls = {
    ".vl-viewport": mk("div"),
    ".vl-spacer": mk("div"),
    ".vl-rows": mk("div"),
  };
  listEl.querySelector = (sel) => vlEls[sel] || null;
  const els = {
    ".sv-container": mk("div"),
    ".sv-search": mk("input"),
    ".cve-version-input": mk("input"),
    ".sv-chips": mk("div"),
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
  return { containerEl, detailEl: els[".sv-detail"] };
}

test("CveView fetches detail immediately when sel is set and skips list fetch when listCollapsed", async () => {
  const { containerEl, detailEl } = makeFakeCveHost();

  const calls = { list: 0, detail: [] };
  const view = new CveView();
  view.api = {
    async getCveList() {
      calls.list++;
      return { configured: true, ingested: true, total: 0, cves: [] };
    },
    async getCveDetail(cveId, release) {
      calls.detail.push({ cveId, release });
      return { id: cveId, title: "Fast detail", description: "Loaded directly" };
    },
  };

  view.render(containerEl, {
    id: "tab-cve-1",
    type: "cve",
    sel: "CVE-2024-55555",
    listCollapsed: true,
    version: "v6.6",
  });

  await Promise.resolve();
  await Promise.resolve();
  assert.equal(calls.list, 0);
  assert.deepEqual(calls.detail, [{ cveId: "CVE-2024-55555", release: "" }]);
  assert.match(detailEl.innerHTML, /Fast detail/);
  view.dispose();
});
