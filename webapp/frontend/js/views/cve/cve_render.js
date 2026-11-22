/**
 * cve_render.js - Shared CVE detail rendering helpers (used by both CveView and Insights · Security).
 */
import { escapeHtml } from "../../components/modal.js";

export const SETUP_HTML = `<h2>CVE records are not configured</h2>
<p>The CVE view reads CVE records from a local git clone, either the CVE Program's list (every CNA; the Linux kernel's records are picked out) or the kernel CNA's own repository:</p>
<pre>git clone https://github.com/CVEProject/cvelistV5.git          # in the project root: found by default
git clone https://git.kernel.org/pub/scm/linux/security/vulns.git /srv/vulns
# config.json (a relative path is under the project root)
"cve": { "repo_dir": "/srv/vulns" }</pre>
<p>Keep the clone current yourself (for example <code>git -C cvelistV5 pull</code> from cron). The parser reads it into the database at the start of every run, only the records changed since its last run; <code>python3 main.py --cve</code> does just that.</p>`;

export const NOT_INGESTED_HTML = `<h2>The CVE list has not been ingested yet</h2>
<p>The clone is configured, but the database holds no CVE records. The parser ingests the list at the start of every run; to do only that:</p>
<pre>python3 main.py --cve</pre>`;

export const STATE_TAG = { affected: "bad", possibly: "warn", fixed: "ok" };
export const STATE_LABEL = { affected: "affected", possibly: "possibly", fixed: "fixed" };
export const BASIS = {
  record: "the CVE record's commit ranges and versions",
  "fixes-tag": "the commits the fix names in its Fixes: tags",
  reference: "a fixing commit the record links; when the bug appeared is unknown",
  description: "the versions its description gives",
  versions: "the record's product versions",
};
export const KIND = {
  cna: "issued by the kernel CNA",
  commit: "another CNA's record linking a kernel commit",
  text: "another CNA's record about the kernel, linking no commit",
};

const release = (r) => (r?.released ? (r.first_release || r.first_tag || r.first_stable || "released") : "not released");
const safeHttpUrl = (u) => {
  const s = String(u || "").trim();
  return /^https?:\/\//i.test(s) ? s : "";
};

export const severityTag = (score, sev) => (score == null ? "" :
  `<span class="sv-tag ${score >= 7 ? "bad" : score >= 4 ? "warn" : "info"}" title="CVSS ${escapeHtml(String(score))}">${escapeHtml(String(score))}${sev ? ` ${escapeHtml(sev.toLowerCase())}` : ""}</span>`);

const commitLink = (sha, subject, rel, cls) =>
  `<a href="#" data-commit="${escapeHtml(sha)}">${escapeHtml(String(sha || "").slice(0, 12))}</a> ${escapeHtml(subject || "")} <span class="sv-tag ${cls}">${escapeHtml(release(rel))}</span>`;

function versionItem(v) {
  return `${escapeHtml(v.version || "")}${v.lessThan ? ` ≤ x &lt; ${escapeHtml(v.lessThan)}` : ""}${v.lessThanOrEqual ? ` ≤ x ≤ ${escapeHtml(v.lessThanOrEqual)}` : ""}`;
}

function formatVersionList(val) {
  if (Array.isArray(val)) return val.filter(Boolean).join(", ");
  return val ? String(val) : "";
}

function renderVersionSummaryHtml(vs) {
  if (!vs || typeof vs !== "object") return "";
  const affected = formatVersionList(vs.affected);
  const fixed = formatVersionList(vs.fixed);
  const text = vs.text ? String(vs.text) : "";
  if (!text && !affected && !fixed && !vs.unfixed) return "";
  return `<div class="cve-version-summary">
    <h3>Applies to Versions ${vs.unfixed ? '<span class="sv-tag bad">unfixed</span>' : ""}</h3>
    ${text ? `<p class="cve-version-summary-text">${escapeHtml(text)}</p>` : ""}
    ${affected || fixed ? `<dl class="sv-kv">
      ${affected ? `<dt>Affected</dt><dd>${escapeHtml(affected)}</dd>` : ""}
      ${fixed ? `<dt>Fixed</dt><dd>${escapeHtml(fixed)}</dd>` : ""}
    </dl>` : ""}
  </div>`;
}

export function renderCveDetailHtml(c, opts = {}) {
  if (!c) return '<div class="sv-empty">No CVE data.</div>';
  const st = c.release_state;
  const cvss = c.cvss;
  const ranges = c.ranges || [];
  const cveUrl = safeHttpUrl(c.url) || `https://www.cve.org/CVERecord?id=${encodeURIComponent(c.id || "")}`;
  const refs = (c.references || []).map(safeHttpUrl).filter(Boolean);
  const kindText = KIND[c.kind] || "";

  return `<h2>${escapeHtml(c.id)} ${escapeHtml(c.title || "")}</h2>
  <p class="cve-meta-line"><a href="${escapeHtml(cveUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(c.id)} at cve.org ↗</a>${opts.showCopyLink ? ` <button type="button" class="cve-copy-btn" data-copy-cve="${escapeHtml(c.id)}" title="Copy deep link to this CVE">Copy link</button>` : ""}${opts.showNodemap ? ` <button type="button" class="cve-copy-btn" data-nodemap-cve="${escapeHtml(c.id)}" data-nodemap-release="${escapeHtml(opts.release || opts.checkedRelease || "")}" title="Send CVE &amp; fix chain to NodeMap">🗺 NodeMap</button>` : ""}${c.published ? ` · published ${escapeHtml(c.published)}` : ""}${c.updated ? ` · updated ${escapeHtml(c.updated)}` : ""}${kindText ? ` · ${escapeHtml(kindText)}` : ""}</p>
  ${st && st.state !== "unknown" ? `<p><span class="sv-tag ${STATE_TAG[st.state] || "info"}">${escapeHtml(st.state)}</span> in ${escapeHtml(st.release)}${BASIS[st.basis] ? `, from ${escapeHtml(BASIS[st.basis])}` : ""}.</p>` : ""}
  ${renderVersionSummaryHtml(c.version_summary)}
  ${opts.showCheckRelease ? `<form class="cve-check-release-form" data-check-release-form="${escapeHtml(c.id)}">
    <input type="text" class="cve-check-release-input" placeholder="Check release (e.g. v6.6.15)…" value="${escapeHtml(opts.checkedRelease || "")}" spellcheck="false" aria-label="Check release" />
    <button type="submit" class="cve-check-release-btn" data-check-release="${escapeHtml(c.id)}">Check release…</button>
  </form>` : ""}
  ${cvss && (cvss.score != null || cvss.vector || cvss.severity) ? `<p>${severityTag(cvss.score, cvss.severity)} CVSS ${escapeHtml(cvss.version || "")} <code>${escapeHtml(cvss.vector || "")}</code>${cvss.source ? ` (${escapeHtml(cvss.source)})` : ""}</p>` : ""}
  ${(c.cwes || []).length ? `<p><strong>Weakness:</strong> ${c.cwes.map((w) => `<span class="sv-tag info">${escapeHtml(w)}</span>`).join(" ")}</p>` : ""}
  ${c.kev ? `<p><span class="sv-tag bad">KEV</span> In CISA's Known Exploited Vulnerabilities catalog since ${escapeHtml(c.kev)}.</p>` : c.exploitation && c.exploitation !== "none" ? `<p>Exploitation: ${escapeHtml(c.exploitation)} (CISA SSVC).</p>` : ""}
  <pre>${escapeHtml(c.description || "")}</pre>
  ${(c.files || []).length ? `<h3>Files</h3>${c.files.map((f) => `<div><a href="#" data-open-file="${escapeHtml(f)}"${opts.release ? ` data-version="${escapeHtml(opts.release)}"` : ""}>${escapeHtml(f)}</a></div>`).join("")}` : ""}
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
  ${refs.length ? `<h3>References</h3>${refs.map((u) => `<div><a href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer">${escapeHtml(u)}</a></div>`).join("")}` : ""}`;
}
