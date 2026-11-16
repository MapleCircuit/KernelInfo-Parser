/**
 * security_section.js - Insights · Security (H4, webapp/API.md §5.30): Linux kernel CVEs affecting the tab's
 * release, possibly affecting it, or fixed in it. The parser ingests a local clone of the CVE list (`cvelistV5`) or of
 * the kernel CNA's vulns.git (config `cve.repo_dir`) into the database; this section reads what it stored.
 */
import { api } from "../../api.js";
import { escapeHtml } from "../../components/modal.js";
import { registerInsightsSection } from "./insights_view.js";
import { toTsv } from "../sections/section_model.js";
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

registerInsightsSection({
  id: "security", label: "Security", order: 60, keywords: "cve vulnerability security",
  visible: (repo) => hasFeature(repo, "cve"),
  hint: "Linux kernel CVEs affecting a release, or fixed in it",
  emptyText: "No CVE record affects this release or is fixed in it.",
  async fetchList(ctx) {
    if (!ctx.release) return { rows: [], emptyText: "No release tag found for this revision." };
    const res = await (ctx.api || api).getInsights(ctx.release, "cves", ctx);
    if (res?.configured === false || res?.ingested === false) {
      return { rows: [], note: res?.configured === false ? "not configured" : "not ingested", summaryHtml: res?.configured === false ? SETUP_HTML : NOT_INGESTED_HTML };
    }
    const c = res.counts || {};
    const source = res.layout === "vulns" ? "vulns.git" : "CVE list";
    return {
      rows: res.cves || [],
      note: `${c.affected ?? 0} affecting, ${c.possibly ?? 0} possibly, ${c.fixed ?? 0} fixed · ${source} ${String(res.head || "").slice(0, 12)}`,
    };
  },
  rowKey: (r) => r.id,
  rowText: (r) => `${r.id} ${r.title} ${r.severity || ""} ${(r.cwes || []).join(" ")}`,
  rowHtml: (r) => `<span class="sv-tag ${STATE_TAG[r.state] || "info"}" title="${escapeHtml(BASIS[r.basis] || "")}">${escapeHtml(STATE_LABEL[r.state] || r.state)}</span>${severityTag(r.score, "")}${r.kev ? '<span class="sv-tag bad" title="In CISA\'s Known Exploited Vulnerabilities catalog">KEV</span>' : ""}<code class="sv-row-sub">${escapeHtml(r.id)}</code><span class="sv-row-main">${escapeHtml(r.title)}</span>`,
  chips: [
    { id: "affected", label: "affected", title: "The record's data puts the bug in this release, without its fix", test: (r) => r.state === "affected" },
    { id: "possibly", label: "possibly", title: "The fix is not in this release, but when the bug appeared is unknown (or only the description gives versions)", test: (r) => r.state === "possibly" },
    { id: "fixed", label: "fixed", title: "This release carries the fix", test: (r) => r.state === "fixed" },
    { id: "new", label: "fixed here", title: "Fixed by a commit first released in this release (its -rc cycle, or this stable update)", test: (r) => !!r.fixed_here },
    { id: "high", label: "CVSS ≥ 7", title: "High or critical CVSS base score", test: (r) => r.score != null && r.score >= 7 },
    { id: "kev", label: "exploited", title: "Listed in CISA's Known Exploited Vulnerabilities catalog", test: (r) => !!r.kev },
  ],
  fetchDetail: (ctx, key) => (ctx.api || api).request(`/api/cve/${encodeURIComponent(key)}${ctx.release ? `?release=${encodeURIComponent(ctx.release)}` : ""}`),
  detailHtml: (c, ctx) => renderCveDetailHtml(c, { release: ctx?.release }),
  downloads: [{ label: "CVEs", ext: "tsv", build: (rows) => toTsv(rows, [["id", (r) => r.id], ["state", (r) => r.state], ["basis", (r) => r.basis],
    ["cvss", (r) => (r.score == null ? "" : r.score)], ["kev", (r) => r.kev || ""], ["published", (r) => r.published],
    ["fixes", (r) => (r.fixes || []).join(" ")], ["title", (r) => r.title]]) }],
});
