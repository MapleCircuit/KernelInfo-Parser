/**
 * checkpatch_panel.js - "Run checkpatch" for a generated patch (P4, webapp/API.md §5.17).
 *
 * `POST /api/tools/{v}/checkpatch` runs scripts/checkpatch.pl of a configured release tag on the server
 * (off by default; the panel then names the config key). Results are grouped by level; `onJump(line)`
 * receives the patch line of a result (the patch preview scrolls to it and marks it).
 */
import { api } from "../api.js";
import { escapeHtml } from "./modal.js";

const LEVELS = [["ERROR", "error"], ["WARNING", "warning"], ["CHECK", "check"]];

export async function runCheckpatch(host, version, patchText, { onJump = null } = {}) {
  host.innerHTML = `<div class="cp-run kconfig-muted">Running checkpatch…</div>`;
  let res;
  try {
    res = await api.checkpatch(version, patchText);
  } catch (err) {
    host.innerHTML = `<div class="ki-modal-error">checkpatch failed: ${escapeHtml(err.message)}</div>`;
    return null;
  }
  if (!res.enabled) {
    host.innerHTML = `<div class="cp-disabled">checkpatch is off on this server. It runs <code>scripts/checkpatch.pl</code> taken from the repository, so the operator has to enable it: <code>${escapeHtml(res.config_key || "webapp.tools.checkpatch.enabled")}: true</code> in config.json (the script is then taken from a release tag only, and runs isolated).</div>`;
    return res;
  }
  const total = res.results.length;
  host.innerHTML = `
    <div class="cp-summary">checkpatch (${escapeHtml(res.script_ref)}): ${LEVELS.map(([l, c]) => `<span class="cp-count-${c}">${res.counts[l] || 0} ${l.toLowerCase()}${(res.counts[l] || 0) === 1 ? "" : "s"}</span>`).join(" · ")}${res.timed_out ? " · timed out" : ""}${total ? "" : " — clean"}</div>
    ${LEVELS.map(([l, c]) => {
      const rows = res.results.filter((r) => r.level === l);
      return rows.length ? `<ul class="cp-list cp-${c}">${rows.map((r) => `<li><a href="#" data-line="${r.line}">line ${r.line}</a> <code>${escapeHtml(r.type)}</code> ${escapeHtml(r.message)}</li>`).join("")}</ul>` : "";
    }).join("")}
    ${res.raw ? `<details><summary class="kconfig-muted">Other output</summary><pre class="cp-raw">${escapeHtml(res.raw)}</pre></details>` : ""}`;
  host.querySelectorAll("a[data-line]").forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      if (onJump) onJump(Number(a.dataset.line));
    };
  });
  return res;
}
