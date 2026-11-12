/**
 * security_section.js - Insights · Security (H4, webapp/API.md §5.30): Linux kernel CVEs affecting the tab's
 * release, possibly affecting it, or fixed in it. The parser ingests a local clone of the CVE list (`cvelistV5`) or of
 * the kernel CNA's vulns.git (config `cve.repo_dir`) into the database; this section reads what it stored.
 */
import { api } from "../../api.js";
import { escapeHtml } from "../../components/modal.js";
import { registerInsightsSection } from "./insights_view.js";
import { toTsv } from "../sections/section_model.js";

const SETUP = `<h2>CVE records are not configured</h2>
<p>The Security section reads CVE records from a local git clone, either the CVE Program's list (every CNA; the Linux kernel's records are picked out) or the kernel CNA's own repository:</p>
<pre>git clone https://github.com/CVEProject/cvelistV5.git          # in the project root: found by default
git clone https://git.kernel.org/pub/scm/linux/security/vulns.git /srv/vulns
# config.json (a relative path is under the project root)
"cve": { "repo_dir": "/srv/vulns" }</pre>
<p>Keep the clone current yourself (for example <code>git -C cvelistV5 pull</code> from cron). The parser reads it into the database at the start of every run, only the records changed since its last run; <code>python3 main.py --cve</code> does just that.</p>`;

const NOT_INGESTED = `<h2>The CVE list has not been ingested yet</h2>
<p>The clone is configured, but the database holds no CVE records. The parser ingests the list at the start of every run; to do only that:</p>
<pre>python3 main.py --cve</pre>`;

const STATE_TAG = { affected: "bad", possibly: "warn", fixed: "ok" };
const STATE_LABEL = { affected: "affected", possibly: "possibly", fixed: "fixed" };
const BASIS = {
  record: "the CVE record's commit ranges and versions",
  "fixes-tag": "the commits the fix names in its Fixes: tags",
  reference: "a fixing commit the record links; when the bug appeared is unknown",
  description: "the versions its description gives",
  versions: "the record's product versions",
};
const KIND = {
  cna: "issued by the kernel CNA",
  commit: "another CNA's record linking a kernel commit",
  text: "another CNA's record about the kernel, linking no commit",
};

const release = (r) => (r?.released ? (r.first_release || r.first_tag || r.first_stable) : "not released");
const severityTag = (score, sev) => (score == null ? "" :
  `<span class="sv-tag ${score >= 7 ? "bad" : score >= 4 ? "warn" : "info"}" title="CVSS ${escapeHtml(String(score))}">${escapeHtml(String(score))}${sev ? ` ${escapeHtml(sev.toLowerCase())}` : ""}</span>`);
const commitLink = (sha, subject, rel, cls) => `<a href="#" data-commit="${escapeHtml(sha)}">${escapeHtml(sha.slice(0, 12))}</a> ${escapeHtml(subject || "")} <span class="sv-tag ${cls}">${escapeHtml(release(rel))}</span>`;

function versionItem(v) {
  return `${escapeHtml(v.version || "")}${v.lessThan ? ` ≤ x &lt; ${escapeHtml(v.lessThan)}` : ""}${v.lessThanOrEqual ? ` ≤ x ≤ ${escapeHtml(v.lessThanOrEqual)}` : ""}`;
}

registerInsightsSection({
  id: "security", label: "Security", order: 60, releaseScoped: true, keywords: "cve vulnerability security",
  hint: "Linux kernel CVEs affecting a release, or fixed in it",
  emptyText: "No CVE record affects this release or is fixed in it.",
  async releaseOptions(ctx) {
    // Mainline releases plus the stable updates: a stable tag carries backported fixes
    const res = await (ctx.api || api).getVersionsCached(true);
    return (res?.versions || []).map((v) => v.vname || v).filter((n) => /^v\d+\.\d+(\.\d+)*$/.test(n)).reverse();
  },
  async fetchList(ctx) {
    const res = await (ctx.api || api).getInsights(ctx.release, "cves", ctx);
    if (res?.configured === false || res?.ingested === false) {
      this._setup = true;
      return { rows: [], note: res?.configured === false ? "not configured" : "not ingested", summaryHtml: res?.configured === false ? SETUP : NOT_INGESTED };
    }
    this._setup = false;
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
  detailHtml: (c, ctx) => {
    const st = c.release_state;
    const cvss = c.cvss;
    const ranges = c.ranges || [];
    return `<h2>${escapeHtml(c.id)} ${escapeHtml(c.title || "")}</h2>
    <p><a href="${escapeHtml(c.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(c.id)} at cve.org ↗</a>${c.published ? ` · published ${escapeHtml(c.published)}` : ""}${c.updated ? ` · updated ${escapeHtml(c.updated)}` : ""} · ${escapeHtml(KIND[c.kind] || "")}</p>
    ${st && st.state !== "unknown" ? `<p><span class="sv-tag ${STATE_TAG[st.state] || "info"}">${escapeHtml(st.state)}</span> in ${escapeHtml(st.release)}${BASIS[st.basis] ? `, from ${escapeHtml(BASIS[st.basis])}` : ""}.</p>` : ""}
    ${cvss ? `<p>${severityTag(cvss.score, cvss.severity)} CVSS ${escapeHtml(cvss.version || "")} <code>${escapeHtml(cvss.vector || "")}</code>${cvss.source ? ` (${escapeHtml(cvss.source)})` : ""}</p>` : ""}
    ${(c.cwes || []).length ? `<p><strong>Weakness:</strong> ${c.cwes.map((w) => `<span class="sv-tag info">${escapeHtml(w)}</span>`).join(" ")}</p>` : ""}
    ${c.kev ? `<p><span class="sv-tag bad">KEV</span> In CISA's Known Exploited Vulnerabilities catalog since ${escapeHtml(c.kev)}.</p>` : c.exploitation && c.exploitation !== "none" ? `<p>Exploitation: ${escapeHtml(c.exploitation)} (CISA SSVC).</p>` : ""}
    <pre>${escapeHtml(c.description)}</pre>
    ${(c.files || []).length ? `<h3>Files</h3>${c.files.map((f) => `<div><a href="#" data-open-file="${escapeHtml(f)}"${ctx?.release ? ` data-version="${escapeHtml(ctx.release)}"` : ""}>${escapeHtml(f)}</a></div>`).join("")}` : ""}
    ${ranges.length ? `<h3>Commits</h3>
    <table><tr><th>Introduced by</th><th>Fixed by</th></tr>
    ${ranges.map((r) => `<tr>
      <td>${r.introduced ? commitLink(r.introduced, r.introduced_subject, r.introduced_release, "info")
        : (r.inferred_introduced || []).length ? r.inferred_introduced.map((i) => `${commitLink(i.sha, i.subject, i.release, "info")} <span class="sv-tag warn" title="Named by the fix's Fixes: tag">Fixes: tag</span>`).join("<br>")
        : r.basis === "reference" ? "unknown" : "the beginning of history"}</td>
      <td>${r.fixed ? commitLink(r.fixed, r.fixed_subject, r.fixed_release, "ok") : "not fixed"}</td></tr>`).join("")}</table>` : ""}
    ${(c.entries || []).some((e) => (e.versions || []).length) ? `<h3>Versions</h3>${c.entries.filter((e) => (e.versions || []).length).map((e) => `<table>
      ${e.source === "description" ? "<tr><th colspan=2>From the description</th></tr>" : e.default ? `<tr><th colspan=2>Otherwise ${escapeHtml(e.default)}</th></tr>` : ""}
      ${e.versions.map((v) => `<tr><td>${escapeHtml(v.status || "")}</td><td>${versionItem(v)}</td></tr>`).join("")}</table>`).join("")}` : ""}
    ${(c.references || []).length ? `<h3>References</h3>${c.references.map((u) => `<div><a href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer">${escapeHtml(u)}</a></div>`).join("")}` : ""}`;
  },
  downloads: [{ label: "CVEs", ext: "tsv", build: (rows) => toTsv(rows, [["id", (r) => r.id], ["state", (r) => r.state], ["basis", (r) => r.basis],
    ["cvss", (r) => (r.score == null ? "" : r.score)], ["kev", (r) => r.kev || ""], ["published", (r) => r.published],
    ["fixes", (r) => (r.fixes || []).join(" ")], ["title", (r) => r.title]]) }],
});
