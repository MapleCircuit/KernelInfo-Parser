/**
 * merged_render.js - "Merged k/N · v3.1" badge and the commit list of a mail thread (P2, webapp/API.md
 * §5.22). Pure and escaped (webapp/frontend/tests/merged_render.test.mjs).
 */
function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** {label, cls, title} of the thread badge. */
export function mergedSummary(res) {
  const commits = res?.commits || [];
  if (!commits.length) {
    return { label: "Not merged", cls: "none", title: res?.source === "trailers"
      ? "No commit links this thread (commit-mail links unavailable: Link: trailers searched)" : "No commit was linked to this thread" };
  }
  const s = res.series || {};
  const count = s.total > 1 ? `${s.merged}/${s.total}` : commits.length > 1 ? `${commits.length} commits` : "";
  const rel = s.first_release ? ` · ${s.first_release}` : " · not released yet";
  return {
    label: `Merged${count ? ` ${count}` : ""}${rel}`,
    cls: s.total > 1 && s.merged < s.total ? "partial" : "merged",
    title: `${commits.length} commit(s) from ${res.source === "db" ? "the parser's commit-mail links" : "Link:/Message-Id: trailers"} — click to list them`,
  };
}

export function mergedListHtml(res) {
  return (res?.commits || []).map((c) => {
    const rel = c.released?.released ? (c.released.first_release || c.released.first_tag) : "not released yet";
    const idx = c.patch_total ? `[${c.patch_idx}/${c.patch_total}] ` : "";
    return `<div class="mail-merged-row"><a href="#" data-commit="${esc(c.hash)}" title="Open in the Commits tab (middle-click: new tab)"><code>${esc(c.hash.slice(0, 12))}</code> ${esc(idx)}${esc(c.subject)}</a> <span class="mail-merged-rel">${esc(rel)}</span></div>`;
  }).join("");
}
