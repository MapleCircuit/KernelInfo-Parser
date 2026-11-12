/**
 * blame_view.js - Side-by-Side Git Blame Gutter.
 * Displays commit author, hash, and date alongside line numbers.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { showPersonModal } from "../../components/person_modal.js";
import { escapeHtml } from "../../components/modal.js";
import { movedFrom } from "./blame_options.js";

const AUTHOR_PALETTE = Array.from({ length: 16 }, (_, k) => `var(--chart-${k + 1})`); // theme tokens (G08)

export class BlameView {
  constructor() {
    this.blameData = null;
    this.blameByLine = new Map();
    this.authorColors = new Map();
    // Revision the blame belongs to: what the profiles and commit tabs it opens follow
    this.version = "";
  }

  async loadBlame(version, filePath, opts = null, client = api) {
    this.blameByLine.clear();
    this.authorColors.clear();
    this.version = version;
    this.filePath = filePath;
    try {
      this.blameData = await client.getFileBlame(version, filePath, opts);
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
    // -M/-C followed this line from another file (H9)
    const from = movedFrom(b, this.filePath);
    const fromTitle = from ? `\nmoved from ${escapeHtml(from)}` : "";

    return `
      <div class="gutter-blame${from ? " blame-moved" : ""}" title="${cleanSubject} (${cleanAuthor})${fromTitle}" data-commit="${cleanCommit}" style="border-left:2px solid ${authorColor};">${from ? `<span class="blame-moved-mark" title="moved from ${escapeHtml(from)}">↪</span>` : ""}
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
