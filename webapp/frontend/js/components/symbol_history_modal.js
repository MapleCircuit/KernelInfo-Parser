/**
 * symbol_history_modal.js - History of one symbol definition across every indexed version.
 */
import { api } from "../api.js";
import { state } from "../state.js";
import { openModal, escapeHtml } from "./modal.js";
import { shortRef } from "./ref_picker.js";

const STATUS_LABEL = {
  introduced: "introduced",
  added: "re-added",
  unchanged: "unchanged",
  modified: "modified",
  removed: "removed",
  absent: "absent",
  unindexed: "not indexed",
};

export async function showSymbolHistoryModal(version, filePath, line, astId = null, name = "") {
  const modal = openModal({
    title: `🕰 Symbol history${name ? `: ${name}` : ""}`,
    subtitle: `${filePath}:${line} @ ${shortRef(version)}`,
    width: "820px",
  });
  modal.body.innerHTML = `<div class="ki-modal-empty">Tracing the definition across indexed versions…</div>`;

  let res;
  try {
    res = await api.getSymbolHistory(version, filePath, line, astId);
  } catch (err) {
    modal.body.innerHTML = `<div class="ki-modal-error">${escapeHtml(err.message)}</div>`;
    return;
  }

  const timeline = res.timeline || [];
  const changes = timeline.filter((t) => t.status === "modified").length;
  modal.el.querySelector(".ki-modal-title").textContent = `🕰 Symbol history: ${res.ast_name || name}`;
  modal.body.innerHTML = `
    <div class="symhist-meta">
      <span>File <code>${escapeHtml(res.path)}</code></span>
      <span>Present in <strong>${res.total_versions}</strong> of ${timeline.length} indexed versions</span>
      <span><strong>${changes}</strong> change${changes === 1 ? "" : "s"}</span>
      ${res.moved_from || res.moved_to ? `<span>Moved: ${res.moved_from ? `from tag ${res.moved_from}` : ""} ${res.moved_to ? `to tag ${res.moved_to}` : ""}</span>` : ""}
    </div>
    <div class="symhist-timeline">
      ${timeline.length === 0 ? `<div class="ki-modal-empty">No indexed versions in this repository yet.</div>` : timeline.map((t, idx) => `
        <div class="symhist-row ${t.status === "modified" ? "changed" : t.status}" data-idx="${idx}">
          <span class="symhist-version">${escapeHtml(t.vname)}</span>
          <span class="symhist-status ${t.status}">${STATUS_LABEL[t.status] || t.status}</span>
          <span>${t.line_s ? `lines ${t.line_s}–${t.line_e}` : ""}${t.status === "modified" ? ` <span class="diff-add">+${t.lines_added}</span> <span class="diff-del">−${t.lines_removed}</span>` : ""}</span>
          <span>${t.line_s ? `<button class="code-btn" data-open="${idx}" type="button">Open</button>` : ""}</span>
          ${t.diff && t.diff.length ? `<div class="symhist-diff diff-box"><div class="diff-content">${t.diff.map((d) => `<div class="${d.type === "add" ? "diff-add" : "diff-del"}">${d.type === "add" ? "+" : "-"}${escapeHtml(d.text)}</div>`).join("")}</div></div>` : ""}
        </div>`).join("")}
    </div>`;

  modal.body.querySelectorAll("[data-open]").forEach((btn) => {
    btn.onclick = () => {
      const t = timeline[parseInt(btn.dataset.open, 10)];
      modal.close();
      state.openTab({ type: "code", title: res.path.split("/").pop(), path: res.path, version: t.vname, cursorLine: t.line_s, forceNew: true });
    };
  });
}
