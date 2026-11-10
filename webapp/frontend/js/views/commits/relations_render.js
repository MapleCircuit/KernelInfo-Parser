/**
 * relations_render.js - HTML of the commit relations card, release badges and timeline chips (H1-H3,
 * webapp/API.md §5.12). Pure (strings in, strings out) and escaped throughout
 * (webapp/frontend/tests/relations_render.test.mjs).
 */

export function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** "v3.1-rc1 → v3.1", "v3.1", "v3.0.5 (stable)", "not released yet". */
export function releaseLabel(info) {
  if (!info) return "";
  if (!info.released) return "not released yet";
  if (info.first_tag) {
    return info.first_release && info.first_release !== info.first_tag ? `${info.first_tag} → ${info.first_release}` : info.first_tag;
  }
  return info.first_stable ? `${info.first_stable} (stable)` : "";
}

/** Badge of the first release containing a commit (H3); `data-tag` is what a click selects. */
export function releaseBadgeHtml(info) {
  if (!info) return "";
  if (!info.released) {
    const where = (info.branches || []).slice(0, 3).join(", ");
    return `<span class="release-badge unreleased" title="${escapeHtml(where ? `On ${where}` : "No tag contains this commit yet")}">Not released yet</span>`;
  }
  const tag = info.first_release || info.first_tag || info.first_stable;
  const stable = info.first_stable && info.first_tag ? ` <span class="release-badge stable" data-tag="${escapeHtml(info.first_stable)}" title="First stable release containing it">${escapeHtml(info.first_stable)}</span>` : "";
  return `<span class="release-badge" data-tag="${escapeHtml(tag)}" title="First released in ${escapeHtml(releaseLabel(info))} — click to select it in the header">First released in ${escapeHtml(releaseLabel(info))}</span>${stable}`;
}

function shortSha(sha) {
  return String(sha || "").slice(0, 12);
}

function relRow(item, { prefix = "" } = {}) {
  const sha = item.sha || "";
  const subject = item.subject || "";
  const rel = item.released ? `<span class="rel-release" title="First release containing it">${escapeHtml(releaseLabel(item.released))}</span>` : "";
  const hash = sha
    ? `<span class="commit-hash copy-hash" data-hash="${escapeHtml(sha)}" role="button" tabindex="0" title="Click to copy ${escapeHtml(sha)} - right-click for commit actions">${escapeHtml(shortSha(sha))}</span>`
    : `<span class="commit-hash unresolved" title="This abbreviated id names no commit of the repository (or several)">${escapeHtml(prefix)}</span>`;
  const subj = sha
    ? `<a href="#" class="rel-subject" data-inspect="${escapeHtml(sha)}" title="Inspect this commit (middle-click: new tab)">${escapeHtml(subject || "(no subject)")}</a>`
    : `<span class="rel-subject unresolved">${escapeHtml(subject ? `"${subject}"` : "(unknown commit)")}</span>`;
  const who = item.author ? `<span class="rel-author">${escapeHtml(item.author)}</span>` : "";
  return `<div class="rel-row">${hash}${subj}${who}${rel}</div>`;
}

const SECTIONS = [
  ["fixes", "Fixes", "This commit fixes"],
  ["fixed_by", "Fixed by", "Later commits fixing this one"],
  ["reverts", "Reverts", "This commit reverts"],
  ["reverted_by", "Reverted by", "Later commits reverting this one"],
  ["upstream", "Backport of", "The upstream commit this one was backported from"],
  ["backports", "Backported as", "Stable backports of this commit"],
];

/** The Relations card body (H1, H2); "" when the commit has no relation at all. */
export function relationsHtml(rel) {
  if (!rel) return "";
  const parts = [];
  for (const [key, title, help] of SECTIONS) {
    const items = rel[key] || [];
    if (!items.length) continue;
    parts.push(`<div class="rel-section rel-${key}"><div class="rel-title" title="${escapeHtml(help)}">${escapeHtml(title)} <span>${items.length}</span></div>
      ${items.map((it) => relRow(it, { prefix: it.prefix })).join("")}</div>`);
  }
  if ((rel.stable_cc || []).length) {
    const notes = rel.stable_cc.filter(Boolean);
    parts.push(`<div class="rel-section rel-stable"><div class="rel-title">Cc: stable</div><div class="rel-row">Marked for the stable releases${notes.length ? ` (<code>${escapeHtml(notes.join(", "))}</code>)` : ""}</div></div>`);
  }
  const cves = rel.cves || {};
  if ((cves.fixes || []).length || (cves.introduced || []).length || (cves.fixes_upstream || []).length) {
    const link = (id) => `<a href="#" class="rel-cve" data-cve="${escapeHtml(id)}" title="Open in Insights · Security">${escapeHtml(id)}</a> <a href="https://www.cve.org/CVERecord?id=${encodeURIComponent(id)}" target="_blank" rel="noopener noreferrer" class="rel-ext" title="The record at cve.org">↗</a>`;
    parts.push(`<div class="rel-section rel-cve-section"><div class="rel-title">CVE</div>
      ${(cves.fixes || []).map((id) => `<div class="rel-row">Fixes ${link(id)}</div>`).join("")}
      ${(cves.fixes_upstream || []).map((id) => `<div class="rel-row">Backports the fix of ${link(id)}</div>`).join("")}
      ${(cves.introduced || []).map((id) => `<div class="rel-row">Introduced the bug of ${link(id)}</div>`).join("")}</div>`);
  }
  if ((rel.links || []).length) {
    parts.push(`<div class="rel-section rel-links"><div class="rel-title">Link</div>${rel.links.slice(0, 5).map((m) => `<div class="rel-row"><a href="#" class="rel-mail" data-mid="${escapeHtml(m)}" title="Open the thread in the mailing list tab">&lt;${escapeHtml(m)}&gt;</a></div>`).join("")}</div>`);
  }
  return parts.join("");
}

/** Count of relations shown in the card title. */
export function relationsCount(rel) {
  if (!rel) return 0;
  return SECTIONS.reduce((n, [k]) => n + (rel[k] || []).length, 0) + ((rel.stable_cc || []).length ? 1 : 0);
}

/** Timeline chips of a commit card from batched counts (H1): "fixed later ×2", "reverted", "backported ×3". */
export function relationChipsHtml(counts) {
  if (!counts) return "";
  const chips = [];
  if (counts.fixed_by) chips.push(`<span class="rel-chip fixed" title="${counts.fixed_by} later commit(s) carry Fixes: for this one">fixed later${counts.fixed_by > 1 ? ` ×${counts.fixed_by}` : ""}</span>`);
  if (counts.reverted_by) chips.push('<span class="rel-chip reverted" title="A later commit reverts this one">reverted</span>');
  if (counts.fixes) chips.push(`<span class="rel-chip fixes" title="This commit fixes ${counts.fixes} earlier commit(s)">fix</span>`);
  if (counts.backports) chips.push(`<span class="rel-chip backported" title="Backported to ${counts.backports} stable branch commit(s)">backported${counts.backports > 1 ? ` ×${counts.backports}` : ""}</span>`);
  if (counts.stable_cc && !counts.backports) chips.push('<span class="rel-chip stable" title="Cc: stable">stable</span>');
  return chips.join("");
}
