/**
 * blame_view.js - Side-by-Side Git Blame Gutter.
 * Displays commit author, hash, and date alongside line numbers.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { showPersonModal } from "../../components/person_modal.js";

export class BlameView {
  constructor() {
    this.blameData = null;
    this.blameByLine = new Map();
  }

  async loadBlame(version, filePath) {
    this.blameByLine.clear();
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

    return `
      <div class="gutter-blame" title="${b.subject || ''} (${b.author_name})" data-commit="${b.commit_hash}">
        <span style="color:var(--accent-blue);font-family:var(--font-mono);">${hashShort}</span>
        <span class="blame-author-link" style="cursor:pointer;" data-author="${author}" title="Click to view developer profile: ${author}">${author}</span>
        <span style="font-size:9px;color:var(--text-muted);">${dateStr}</span>
      </div>
    `;
  }

  attachListeners(containerEl) {
    containerEl.addEventListener("click", (e) => {
      const authorEl = e.target.closest(".blame-author-link");
      if (authorEl && authorEl.dataset.author) {
        e.stopPropagation();
        showPersonModal(state.currentVersion, authorEl.dataset.author);
        return;
      }

      const cell = e.target.closest(".gutter-blame");
      if (cell && cell.dataset.commit) {
        state.openTab({
          type: "commits",
          title: `Commit ${cell.dataset.commit.substring(0, 7)}`,
          commitId: cell.dataset.commit,
          version: state.currentVersion
        });
      }
    });
  }
}
