/**
 * security_section.js - Insights · Security (H4, webapp/API.md §5.30): Linux kernel CVEs affecting the tab's
 * release, possibly affecting it, or fixed in it. The parser ingests a local clone of the CVE list (`cvelistV5`) or of
 * the kernel CNA's vulns.git (config `cve.repo_dir`) into the database; this section reads what it stored.
 */
import { api } from "../../api.js";
import { escapeHtml } from "../../components/modal.js";
import { registerInsightsSection } from "./insights_view.js";
import {
  clampCvssScore,
  formatChips,
  matchesCvssScore,
  normalizeCvssOp,
  parseChips,
  parseCvssInput,
  toTsv,
} from "../sections/section_model.js";
import { isFinalRelease, parseTag } from "../sections/release_order.js";
import { hbarsHtml, kpiHtml } from "../../utils/svg_charts.js";
import { hasFeature } from "../../utils/projects.js";
import {
  BASIS,
  NOT_INGESTED_HTML,
  SETUP_HTML,
  STATE_LABEL,
  STATE_TAG,
  renderCveDetailHtml,
  severityTag,
} from "../cve/cve_render.js";

const n = (x) => Number(x || 0).toLocaleString("en-US");

export function targetFinalRelease(ctx = null) {
  const rel = String(ctx?.release || "");
  if (!rel) return "";
  if (isFinalRelease(rel)) return rel;
  const p = parseTag(rel);
  if (p?.pre && ctx?.resolved?.final) return ctx.resolved.final;
  return "";
}

export function cvssFilterParams(tab = {}) {
  const chips = parseChips(tab?.filter);
  const op = normalizeCvssOp(tab?.cvssOp);
  const score = clampCvssScore(tab?.cvssScore);
  const active = chips.has("high") || Boolean(op) || score != null;
  return {
    active,
    op: op || "ge",
    score: score ?? 7,
  };
}

export function securityCvesPath(tab = {}) {
  const { active, op, score } = cvssFilterParams(tab);
  if (!active) return "cves";
  return `cves?cvss_op=${encodeURIComponent(op)}&cvss_score=${encodeURIComponent(String(score))}`;
}

export function buildSecurityChips(rows = [], ctx = null) {
  const { op, score } = cvssFilterParams(ctx?.view?.tab);
  return [
    { id: "affected", label: "affected", title: "The record's data puts the bug in this release, without its fix", test: (r) => r.state === "affected" },
    { id: "possibly", label: "possibly", title: "The fix is not in this release, but when the bug appeared is unknown (or only the description gives versions)", test: (r) => r.state === "possibly" },
    { id: "fixed", label: "fixed", title: "This release carries the fix", test: (r) => r.state === "fixed" },
    { id: "new", label: "fixed here", title: "Fixed by a commit first released in this release (its -rc cycle, or this stable update)", test: (r) => !!r.fixed_here },
    {
      id: "introduced",
      label: "Introduced by",
      title: "Introduced by a commit whose first final release is this version",
      test: (r, c) => {
        const rel = targetFinalRelease(c || ctx);
        return Boolean(rel && r.introduced_release && r.introduced_release === rel);
      },
    },
    { id: "high", label: "CVSS", hidden: true, title: "Filter by CVSS base score", test: (r) => matchesCvssScore(r.score, op, score) },
    { id: "kev", label: "exploited", title: "Listed in CISA's Known Exploited Vulnerabilities catalog", test: (r) => !!r.kev },
  ];
}

export function buildSecuritySummaryHtml(filtered = [], allRows = []) {
  if (!allRows.length) return "";
  let affected = 0;
  let fixed = 0;
  let fixedHere = 0;
  let kev = 0;
  let scored = 0;
  let scoreSum = 0;
  let critical = 0;
  let high = 0;
  let medium = 0;
  let low = 0;
  let unscored = 0;
  for (const r of filtered) {
    if (r.state === "affected") affected++;
    else if (r.state === "fixed") fixed++;
    if (r.fixed_here) fixedHere++;
    if (r.kev) kev++;
    if (r.score != null && typeof r.score !== "boolean" && Number.isFinite(Number(r.score))) {
      const s = Number(r.score);
      scored++;
      scoreSum += s;
      if (s >= 9) critical++;
      else if (s >= 7) high++;
      else if (s >= 4) medium++;
      else low++;
    } else {
      unscored++;
    }
  }
  const avg = scored ? (scoreSum / scored).toFixed(1) : "—";
  const kpis = kpiHtml([
    [n(filtered.length), "CVEs", `${n(allRows.length)} loaded`],
    [n(affected), "affected"],
    [n(fixed), "fixed", `${n(fixedHere)} fixed here`],
    [n(kev), "exploited"],
    [avg, "avg CVSS", `${n(scored)} scored`],
  ]);
  const bars = [
    { label: "Critical (9–10)", value: critical, title: `${n(critical)} CVEs` },
    { label: "High (7–8.9)", value: high, title: `${n(high)} CVEs` },
    { label: "Medium (4–6.9)", value: medium, title: `${n(medium)} CVEs` },
    { label: "Low (< 4)", value: low, title: `${n(low)} CVEs` },
    ...(unscored ? [{ label: "Unscored", value: unscored, title: `${n(unscored)} CVEs without CVSS` }] : []),
  ];
  return kpis + hbarsHtml(bars, { max: Math.max(1, ...bars.map((b) => b.value)), format: (v) => n(v) });
}

function mountCvssControl(extraEl, view) {
  const { active, op, score } = cvssFilterParams(view.tab);
  extraEl.innerHTML = `<span class="sv-chip sv-cvss-control${active ? " active" : ""}">`
    + `<button type="button" class="sv-cvss-toggle" aria-pressed="${active}" title="Toggle CVSS severity filter">CVSS</button>`
    + `<select class="sv-cvss-op" aria-label="CVSS comparison operator">`
    + `<option value="ge"${op === "ge" ? " selected" : ""}>≥</option>`
    + `<option value="gt"${op === "gt" ? " selected" : ""}>&gt;</option>`
    + `<option value="lt"${op === "lt" ? " selected" : ""}>&lt;</option>`
    + `<option value="eq"${op === "eq" ? " selected" : ""}>=</option>`
    + `</select>`
    + `<input type="number" class="sv-cvss-input" min="0" max="10" step="0.1" value="${escapeHtml(String(score))}" aria-label="CVSS threshold score" />`
    + `<span class="sv-cvss-count">0</span>`
    + `</span>`;

  const ctrl = extraEl.querySelector(".sv-cvss-control");
  const toggleBtn = extraEl.querySelector(".sv-cvss-toggle");
  const opSel = extraEl.querySelector(".sv-cvss-op");
  const numInput = extraEl.querySelector(".sv-cvss-input");
  if (!ctrl || !toggleBtn || !opSel || !numInput) return;

  let debounceTimer = null;
  const commit = (nextActive, nextOp, nextScore) => {
    clearTimeout(debounceTimer);
    const chips = parseChips(view.tab.filter);
    if (nextActive) chips.add("high");
    else chips.delete("high");
    const normOp = normalizeCvssOp(nextOp) || "ge";
    const clampedScore = clampCvssScore(nextScore) ?? 7;
    view.persist({
      filter: formatChips(chips),
      cvssOp: nextActive ? normOp : "",
      cvssScore: nextActive ? clampedScore : null,
    });
    view.loadList();
  };

  toggleBtn.addEventListener("click", () => {
    const cur = cvssFilterParams(view.tab);
    const parsed = parseCvssInput(numInput.value);
    const nextScore = parsed ?? cur.score;
    numInput.value = String(nextScore);
    commit(!cur.active, opSel.value || cur.op, nextScore);
  });

  opSel.addEventListener("change", () => {
    const cur = cvssFilterParams(view.tab);
    const parsed = parseCvssInput(numInput.value);
    const nextScore = parsed ?? cur.score;
    numInput.value = String(nextScore);
    commit(true, opSel.value, nextScore);
  });

  numInput.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    const parsed = parseCvssInput(numInput.value);
    if (parsed === null) return;
    debounceTimer = setTimeout(() => {
      if (extraEl.isConnected === false) return;
      commit(true, opSel.value, parsed);
    }, 250);
  });

  const finalizeInput = () => {
    clearTimeout(debounceTimer);
    const parsed = parseCvssInput(numInput.value);
    const cur = cvssFilterParams(view.tab);
    if (parsed === null) {
      numInput.value = String(cur.score);
      return;
    }
    numInput.value = String(parsed);
    if (!cur.active || parsed !== cur.score || (opSel.value || "ge") !== cur.op) {
      commit(true, opSel.value, parsed);
    }
  };
  numInput.addEventListener("change", finalizeInput);
  numInput.addEventListener("blur", finalizeInput);
}

function updateCvssControl(extraEl, view) {
  const ctrl = extraEl?.querySelector(".sv-cvss-control");
  if (!ctrl) return;
  const { active, op, score } = cvssFilterParams(view.tab);
  ctrl.classList.toggle("active", active);
  const toggleBtn = ctrl.querySelector(".sv-cvss-toggle");
  const opSel = ctrl.querySelector(".sv-cvss-op");
  const numInput = ctrl.querySelector(".sv-cvss-input");
  const countEl = ctrl.querySelector(".sv-cvss-count");
  toggleBtn?.setAttribute("aria-pressed", String(active));
  if (opSel && opSel.value !== op) opSel.value = op;
  if (numInput && document.activeElement !== numInput) numInput.value = String(score);
  if (countEl) {
    const matchCount = (view.rows || []).reduce((cnt, r) => cnt + (matchesCvssScore(r.score, op, score) ? 1 : 0), 0);
    countEl.textContent = String(matchCount);
  }
}

registerInsightsSection({
  id: "security", label: "Security", order: 60, keywords: "cve vulnerability security",
  visible: (repo) => hasFeature(repo, "cve"),
  hint: "Linux kernel CVEs affecting a release, or fixed in it",
  emptyText: "No CVE record affects this release or is fixed in it.",
  async fetchList(ctx) {
    if (!ctx.release) return { rows: [], emptyText: "No release tag found for this revision." };
    const path = securityCvesPath(ctx.view?.tab);
    const res = await (ctx.api || api).getInsights(ctx.release, path, ctx);
    if (res?.configured === false || res?.ingested === false) {
      return {
        rows: [],
        configured: res?.configured,
        ingested: res?.ingested,
        note: res?.configured === false ? "not configured" : "not ingested",
        summaryHtml: res?.configured === false ? SETUP_HTML : NOT_INGESTED_HTML,
      };
    }
    const c = res.counts || {};
    const source = res.layout === "vulns" ? "vulns.git" : "CVE list";
    return {
      rows: res.cves || [],
      note: `${c.affected ?? 0} affecting, ${c.possibly ?? 0} possibly, ${c.fixed ?? 0} fixed · ${source} ${String(res.head || "").slice(0, 12)}`,
    };
  },
  rowKey: (r) => r.id,
  rowText: (r) => `${r.id} ${r.title} ${r.state || ""} ${r.severity || ""} ${r.introduced_release || ""} ${(r.fixes || []).join(" ")} ${(r.cwes || []).join(" ")}`,
  rowHtml: (r) => `<span class="sv-tag ${STATE_TAG[r.state] || "info"}" title="${escapeHtml(BASIS[r.basis] || "")}">${escapeHtml(STATE_LABEL[r.state] || r.state)}</span>${severityTag(r.score, "")}${r.introduced_release ? `<span class="sv-tag info" title="First final release containing the introducing commit">${escapeHtml(r.introduced_release)}</span>` : ""}${r.kev ? '<span class="sv-tag bad" title="In CISA\'s Known Exploited Vulnerabilities catalog">KEV</span>' : ""}<code class="sv-row-sub">${escapeHtml(r.id)}</code><span class="sv-row-main">${escapeHtml(r.title)}</span>`,
  getChips: buildSecurityChips,
  mountExtraChips: mountCvssControl,
  updateExtraChips: updateCvssControl,
  summaryHtml: buildSecuritySummaryHtml,
  fetchDetail: (ctx, key) => (ctx.api || api).request(`/webapi/cve/${encodeURIComponent(key)}${ctx.release ? `?release=${encodeURIComponent(ctx.release)}` : ""}`),
  detailHtml: (c, ctx) => renderCveDetailHtml(c, { release: ctx?.release }),
  downloads: [{ label: "CVEs", ext: "tsv", build: (rows) => toTsv(rows, [["id", (r) => r.id], ["state", (r) => r.state], ["basis", (r) => r.basis],
    ["introduced_release", (r) => r.introduced_release || ""], ["cvss", (r) => (r.score == null ? "" : r.score)], ["kev", (r) => r.kev || ""], ["published", (r) => r.published],
    ["fixes", (r) => (r.fixes || []).join(" ")], ["title", (r) => r.title]]) }],
});
