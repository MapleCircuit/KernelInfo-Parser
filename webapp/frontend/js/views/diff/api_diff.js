/**
 * api_diff.js - "API" mode of the Compare view (A6, webapp/API.md §5.11): the Kernel API changelog
 * between the base and target revisions (GET /api/kapi/diff). Pure rendering helpers plus a small
 * controller the Compare view mounts.
 */
import { escapeHtml } from "../../components/modal.js";

export const API_KINDS = ["syscalls", "exports", "params", "tracepoints", "modules"];
export const API_STATUSES = ["added", "removed", "changed"];
const KIND_LABEL = { syscalls: "Syscalls", exports: "Exports", params: "Parameters", tracepoints: "Tracepoints", modules: "Modules" };

/** Flat rows [{kind, status, key, detail, path}] of a diff result. */
export function apiDiffRows(result) {
  const rows = [];
  for (const kind of result?.kinds || []) {
    for (const status of API_STATUSES) {
      for (const it of result[kind]?.[status] || []) {
        let detail = "";
        if (status === "changed") {
          detail = kind === "tracepoints"
            ? [it.fields_added?.length ? `+fields ${it.fields_added.join(", ")}` : "", it.fields_removed?.length ? `−fields ${it.fields_removed.join(", ")}` : "", it.printk_changed ? "format changed" : ""].filter(Boolean).join("; ")
            : `${it.field}: ${it.from ?? ""} → ${it.to ?? ""}`;
        } else if (kind === "syscalls") detail = `nr ${it.nr}${it.args ? ` (${it.args})` : ""}`;
        else if (kind === "exports") detail = it.variant !== "plain" ? it.variant : "";
        else if (kind === "modules") detail = it.options ? `CONFIG_${it.options.split(" ").join(", CONFIG_")}` : "";
        else if (kind === "params") detail = it.type || it.kind || "";
        rows.push({ kind, status, key: it.key.replace(/\/$/, ""), detail, path: it.path || "" });
      }
    }
  }
  return rows;
}

export function filterApiRows(rows, kinds, statuses, query) {
  const q = String(query || "").toLowerCase();
  return rows.filter((r) => (!kinds.size || kinds.has(r.kind)) && (!statuses.size || statuses.has(r.status))
    && (!q || `${r.key} ${r.detail} ${r.path}`.toLowerCase().includes(q)));
}

/** Markdown summary for release notes. */
export function apiDiffMarkdown(result, rows) {
  const lines = [`# Kernel API changes ${result.a} → ${result.b} (ARCH=${result.arch})`, ""];
  for (const kind of API_KINDS) {
    const of = rows.filter((r) => r.kind === kind);
    if (!of.length) continue;
    lines.push(`## ${KIND_LABEL[kind]}`, "");
    for (const status of API_STATUSES) {
      const s = of.filter((r) => r.status === status);
      if (!s.length) continue;
      lines.push(`### ${status[0].toUpperCase()}${status.slice(1)} (${s.length})`, "");
      for (const r of s) lines.push(`- \`${r.key}\`${r.detail ? ` — ${r.detail}` : ""}`);
      lines.push("");
    }
  }
  return lines.join("\n");
}

export function apiDiffTsv(rows) {
  const c = (v) => String(v ?? "").replace(/[\t\r\n]+/g, " ");
  return ["kind\tstatus\tname\tdetail\tpath", ...rows.map((r) => [r.kind, r.status, r.key, r.detail, r.path].map(c).join("\t"))].join("\n") + "\n";
}

export function apiDiffHtml(result, rows, { kinds, statuses, query }) {
  const counts = result.counts || {};
  const kindChips = API_KINDS.filter((k) => counts[k]).map((k) => {
    const n = API_STATUSES.reduce((s, st) => s + (counts[k][st] || 0), 0);
    return `<button type="button" class="compare-chip${kinds.has(k) ? " active" : ""}" data-api-kind="${k}">${KIND_LABEL[k]} <span>${n}</span></button>`;
  }).join("");
  const statusChips = API_STATUSES.map((st) => `<button type="button" class="compare-chip status-${st}${statuses.has(st) ? " active" : ""}" data-api-status="${st}">${st}</button>`).join("");
  const shown = filterApiRows(rows, kinds, statuses, query);
  return `<div class="api-diff">
    <div class="compare-toolbar">${kindChips}<span class="api-diff-sep"></span>${statusChips}
      <input type="search" class="compare-filter api-diff-filter" placeholder="Filter names…" value="${escapeHtml(query)}" />
      <button type="button" class="code-btn api-diff-download">⬇ Download (${shown.length})</button></div>
    <div class="api-diff-list">${shown.slice(0, 3000).map((r) => `<div class="api-diff-row status-${r.status}" data-kind="${r.kind}" data-key="${escapeHtml(r.key)}" data-status="${r.status}">
      <span class="api-diff-status">${r.status === "added" ? "+" : r.status === "removed" ? "−" : "~"}</span>
      <span class="api-diff-kind">${escapeHtml(KIND_LABEL[r.kind])}</span>
      <span class="api-diff-key">${escapeHtml(r.key)}</span>
      <span class="api-diff-detail">${escapeHtml(r.detail)}</span></div>`).join("") || '<div class="compare-empty">No API change matches.</div>'}
    ${shown.length > 3000 ? `<div class="compare-empty">… ${shown.length - 3000} more (download the table)</div>` : ""}</div></div>`;
}

export { KIND_LABEL };
