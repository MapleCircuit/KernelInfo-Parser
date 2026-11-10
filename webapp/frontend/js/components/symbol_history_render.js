/**
 * symbol_history_render.js - Pure fragments of the symbol history modal (webapp/API.md §4.3), escaped
 * (webapp/frontend/tests/symbol_history_render.test.mjs).
 */
function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** "renamed from <old path>" note of the timeline entry where the definition's file was renamed ("" otherwise). */
export function renamedNoteHtml(entry) {
  if (!entry || !entry.renamed_from) return "";
  return `<span class="symhist-status unchanged" title="The file was renamed: ${esc(entry.renamed_from)} → ${esc(entry.path)}">renamed from <code>${esc(entry.renamed_from)}</code></span>`;
}

