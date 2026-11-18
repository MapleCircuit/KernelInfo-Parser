/**
 * stable_section.js - Insights · Stable (H2, webapp/API.md §5.30): mainline commits marked for a
 * stable series (Cc: stable covering it, or fixing a commit it contains) with no backport in its tags.
 */
import { api } from "../../api.js";
import { escapeHtml } from "../../components/modal.js";
import { registerInsightsSection } from "./insights_view.js";
import { toTsv } from "../sections/section_model.js";
import { kpiHtml } from "../../utils/svg_charts.js";
import { hasFeature } from "../../utils/projects.js";

const REASON = { "cc-stable": "Cc: stable", fixes: "Fixes a commit of the series" };
const n = (x) => (x == null ? "—" : Number(x).toLocaleString("en-US"));

export function buildStableSummaryHtml(summary) {
  if (!summary || summary.available === false) return "";
  const lat = summary.avg_fix_latency_days == null ? "—" : `${Number(summary.avg_fix_latency_days).toFixed(1)} d`;
  const tiles = [
    [n(summary.commits), "commits", summary.releases != null ? `${n(summary.releases)} stable releases` : ""],
    [n(summary.backports), "backports"],
    [n(summary.fixes), "Fixes"],
    [n(summary.cc_stable), "Cc: stable"],
    [n(summary.cve_commits), "CVE-linked"],
    [`+${n(summary.lines_added)} −${n(summary.lines_removed)}`, "lines"],
    [`${n(summary.same_cycle_fixes)} / ${n(summary.cross_release_fixes)}`, "same / cross fixes", "Same-cycle vs cross-release fixes"],
    [lat, "avg fix latency"],
  ];
  // kpiHtml escapes every tile value, label, and title via esc()
  return kpiHtml(tiles);
}

registerInsightsSection({
  id: "stable", label: "Stable", order: 50, keywords: "stable backport missing cc",
  needsListForDetail: true,
  visible: (repo) => hasFeature(repo, "stable"),
  hint: "Mainline fixes marked for a stable series that it does not carry",
  emptyText: "Every marked commit has a backport in this series.",
  async fetchList(ctx) {
    if (!ctx.release) return { rows: [], emptyText: "No release tag found for this revision." };
    const client = ctx.api || api;
    const verKey = client._vk ? client._vk(ctx.release) : ctx.release;
    const res = await client.getInsights(
      ctx.release,
      `stable/missing?series=${encodeURIComponent(ctx.release)}&limit=20000`,
      { ...ctx, cacheKey: `stable_${verKey}` },
    );
    if (!res?.available) {
      return {
        rows: [],
        emptyText: "Stable backport tracking is not available for this release.",
        note: res?.reason || "no stable history",
      };
    }
    const summaryHtml = res.summary ? buildStableSummaryHtml(res.summary) : "";
    return {
      rows: res.candidates, total: res.total, summaryHtml,
      note: `${res.candidates_checked.toLocaleString("en-US")} marked commits checked against ${res.stable_tags} tags up to ${res.last_stable}`,
    };
  },
  rowKey: (r) => r.sha,
  rowText: (r) => `${r.sha} ${r.subject} ${r.mainline_release} ${r.annotation}`,
  rowHtml: (r) => `<code class="sv-row-sub">${escapeHtml(r.sha.slice(0, 12))}</code><span class="sv-row-main">${escapeHtml(r.subject)}</span>`
    + `${r.reasons.includes("fixes") ? '<span class="sv-tag bad">fixes</span>' : ""}${r.reasons.includes("cc-stable") ? '<span class="sv-tag warn">cc</span>' : ""}`
    + `<span class="sv-row-sub">${escapeHtml(r.mainline_release)}</span>`,
  chips: [
    { id: "cc-stable", label: "Cc: stable", test: (r) => r.reasons.includes("cc-stable") },
    { id: "fixes", label: "Fixes", test: (r) => r.reasons.includes("fixes") },
  ],
  detailHtml: (r, ctx) => `
    <h2>${escapeHtml(r.subject)}</h2>
    <dl class="sv-kv">
      <dt>Commit</dt><dd><a href="#" data-commit="${escapeHtml(r.sha)}">${escapeHtml(r.sha)}</a></dd>
      <dt>First released in</dt><dd>${escapeHtml(r.mainline_release)}</dd>
      <dt>Date</dt><dd>${r.date ? escapeHtml(new Date(r.date * 1000).toISOString().slice(0, 10)) : ""}</dd>
      <dt>Why a candidate</dt><dd>${r.reasons.map((x) => escapeHtml(REASON[x] || x)).join("; ")}${r.annotation ? ` (<code># ${escapeHtml(r.annotation)}</code>)` : ""}</dd>
      ${r.fixes.length ? `<dt>Fixes</dt><dd>${r.fixes.map((f) => `<a href="#" data-commit="${escapeHtml(f.sha)}">${escapeHtml(f.sha.slice(0, 12))}</a> (in ${escapeHtml(f.release)})`).join(", ")}</dd>` : ""}
    </dl>
    <p class="sv-empty">No tag of ${escapeHtml(ctx.release)}.y carries a backport referring to this commit (<code>commit … upstream.</code>). Backports without an upstream reference are not recognized.</p>`,
  downloads: [{ label: "Missing backports", ext: "tsv", build: (rows) => toTsv(rows, [
    ["sha", (r) => r.sha], ["mainline_release", (r) => r.mainline_release], ["reasons", (r) => r.reasons.join(",")],
    ["annotation", (r) => r.annotation], ["subject", (r) => r.subject]]) }],
});
