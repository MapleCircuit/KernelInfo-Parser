/**
 * blame_view.js - Side-by-Side Git Blame Gutter.
 * Displays commit author, hash, and date alongside line numbers.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { showPersonModal } from "../../components/person_modal.js";
import { escapeHtml } from "../../components/modal.js";

export class BlameView {
  constructor() {
    this.blameData = null;
    this.blameByLine = new Map();
    // Revision the blame belongs to: what the profiles and commit tabs it opens follow
    this.version = "";
  }

  async loadBlame(version, filePath) {
    this.blameByLine.clear();
    this.version = version;
    try {
      this.blameData = await api.getFileBlame(version, filePath);
      if (Array.isArray(this.blameData.lines)) {
        this.blameData.lines.forEach((b) => {
          this.blameByLine.set(b.line, b);
        });
      }
    } catch (e) {
      console.warn("Could not load blame for file:", e);
    }
  }

  renderGutterCell(lineNo) {
    const b = this.blameByLine.get(lineNo);
    if (!b) return `<div class="gutter-blame">&nbsp;</div>`;

    const hashShort = (b.commit_hash || "0000000").substring(0, 7);
    const author = b.author_name || "Unknown";
    const dateStr = b.date ? new Date(b.date * 1000).toISOString().split("T")[0] : "";
    const cleanSubject = escapeHtml(b.subject || "");
    const cleanAuthor = escapeHtml(author);
    const cleanCommit = escapeHtml(b.commit_hash || "");

    return `
      <div class="gutter-blame" title="${cleanSubject} (${cleanAuthor})" data-commit="${cleanCommit}">
        <span style="color:var(--accent-blue);font-family:var(--font-mono);">${escapeHtml(hashShort)}</span>
        <span class="blame-author-link" style="cursor:pointer;" data-author="${cleanAuthor}" title="Click to view developer profile: ${cleanAuthor}">${cleanAuthor}</span>
        <span style="font-size:9px;color:var(--text-muted);">${escapeHtml(dateStr)}</span>
      </div>
    `;
  }

  attachListeners(containerEl) {
    containerEl.addEventListener("click", (e) => {
      const authorEl = e.target.closest(".blame-author-link");
      if (authorEl && authorEl.dataset.author) {
        e.stopPropagation();
        showPersonModal(this.version || state.activeVersion(), authorEl.dataset.author);
        return;
      }

      const cell = e.target.closest(".gutter-blame");
      if (cell && cell.dataset.commit) {
        state.openTab({
          type: "commits",
          title: `Commit ${cell.dataset.commit.substring(0, 7)}`,
          commitId: cell.dataset.commit,
          listCollapsed: true,
          version: this.version || state.activeVersion()
        });
      }
    });
  }
}
