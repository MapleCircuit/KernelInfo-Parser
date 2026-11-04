/**
 * blame_view.js - Side-by-Side Git Blame Gutter.
 * Displays commit author, hash, and date alongside line numbers.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { showPersonModal } from "../../components/person_modal.js";
import { escapeHtml } from "../../components/modal.js";

const AUTHOR_PALETTE = [
  "#58a6ff", // Blue
  "#3fb950", // Green
  "#f0883e", // Orange
  "#bc8cff", // Purple
  "#39c5bb", // Cyan
  "#e3b341", // Gold
  "#ff7b72", // Coral
  "#79c0ff", // Sky Blue
  "#56d364", // Lime Mint
  "#d2a8ff", // Lavender
  "#ffa657", // Peach
  "#ec65c4", // Pink / Magenta
  "#20c997", // Turquoise
  "#e5c07b", // Warm Yellow
  "#f47067", // Light Salmon
  "#80b5ff", // Periwinkle
];

export class BlameView {
  constructor() {
    this.blameData = null;
    this.blameByLine = new Map();
    this.authorColors = new Map();
    // Revision the blame belongs to: what the profiles and commit tabs it opens follow
    this.version = "";
  }

  async loadBlame(version, filePath) {
    this.blameByLine.clear();
    this.authorColors.clear();
    this.version = version;
    try {
      this.blameData = await api.getFileBlame(version, filePath);
      if (Array.isArray(this.blameData.lines)) {
        this.blameData.lines.forEach((b) => {
          this.blameByLine.set(b.line, b);
          const author = (b.author_name || b.author_email || "Unknown").trim();
          if (author && author !== "Unknown" && !this.authorColors.has(author)) {
            const color = AUTHOR_PALETTE[this.authorColors.size % AUTHOR_PALETTE.length];
            this.authorColors.set(author, color);
          }
        });
      }
    } catch (e) {
      console.warn("Could not load blame for file:", e);
    }
  }

  _getAuthorColor(authorName) {
    if (!authorName || authorName === "Unknown") return "var(--accent-blue)";
    const clean = authorName.trim();
    if (!this.authorColors.has(clean)) {
      const color = AUTHOR_PALETTE[this.authorColors.size % AUTHOR_PALETTE.length];
      this.authorColors.set(clean, color);
    }
    return this.authorColors.get(clean);
  }

  renderGutterCell(lineNo) {
    const b = this.blameByLine.get(lineNo);
    if (!b) return `<div class="gutter-blame" style="border-left:2px solid transparent;">&nbsp;</div>`;

    const hashShort = (b.commit_hash || "0000000").substring(0, 7);
    const author = (b.author_name || b.author_email || "Unknown").trim();
    const dateStr = b.date ? new Date(b.date * 1000).toISOString().split("T")[0] : "";
    const cleanSubject = escapeHtml(b.subject || "");
    const cleanAuthor = escapeHtml(author);
    const cleanCommit = escapeHtml(b.commit_hash || "");
    const authorColor = this._getAuthorColor(author);

    return `
      <div class="gutter-blame" title="${cleanSubject} (${cleanAuthor})" data-commit="${cleanCommit}" style="border-left:2px solid ${authorColor};">
        <span style="color:${authorColor};font-family:var(--font-mono);font-weight:600;">${escapeHtml(hashShort)}</span>
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

    containerEl.addEventListener("auxclick", (e) => {
      if (e.button === 1) {
        const cell = e.target.closest(".gutter-blame");
        if (cell && cell.dataset.commit) {
          e.preventDefault();
          e.stopPropagation();
          state.openTab({
            type: "commits",
            title: `Commit ${cell.dataset.commit.substring(0, 7)}`,
            commitId: cell.dataset.commit,
            listCollapsed: true,
            version: this.version || state.activeVersion(),
            forceNew: true,
          });
        }
      }
    });
  }
}
