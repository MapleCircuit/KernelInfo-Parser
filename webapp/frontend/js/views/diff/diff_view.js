/**
 * diff_view.js - Cross-Version Semantic Diff Viewer.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";

export class DiffView {
  constructor() {
    this.versionA = "v3.0";
    this.versionB = "v3.0";
    this.containerEl = null;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.versionA = tabData.versionA || "v3.0";
    this.versionB = tabData.versionB || state.currentVersion;

    containerEl.innerHTML = `
      <div class="tools-view-container">
        <div style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border-color);padding-bottom:12px;">
          <div>
            <div style="font-size:18px;font-weight:600;color:var(--accent-orange);">
              Cross-Version Semantic Diff
            </div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
              Analyzes symbol declarations, definitions, and struct alterations between kernel releases.
            </div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <label style="font-size:11px;color:var(--text-muted);">From:</label>
            <input type="text" id="diff-va-input" value="${this.versionA}" style="width:80px;" />
            <label style="font-size:11px;color:var(--text-muted);">To:</label>
            <input type="text" id="diff-vb-input" value="${this.versionB}" style="width:80px;" />
            <button class="code-btn active" id="diff-compare-btn">Compare</button>
          </div>
        </div>

        <div id="diff-results-container">
          <div style="color:var(--text-muted);">Running semantic comparison...</div>
        </div>
      </div>
    `;

    const btn = containerEl.querySelector("#diff-compare-btn");
    btn.onclick = () => {
      this.versionA = containerEl.querySelector("#diff-va-input").value.trim();
      this.versionB = containerEl.querySelector("#diff-vb-input").value.trim();
      this.loadDiff();
    };

    await this.loadDiff();
  }

  async loadDiff() {
    const resContainer = this.containerEl.querySelector("#diff-results-container");
    try {
      const res = await api.getVersionsDiff(this.versionA, this.versionB);
      let changes = res.changes || [];
      if (changes.length === 0) {
        const added = (res.added || []).map((x) => ({ type: "added", file_path: x.file || x, name: x.file || x }));
        const deleted = (res.deleted || []).map((x) => ({ type: "deleted", file_path: x.file || x, name: x.file || x }));
        const modified = (res.modified || []).map((x) => ({ type: "modified", file_path: x.file || x, name: x.file || x }));
        changes = [...added, ...deleted, ...modified];
      }

      resContainer.innerHTML = `
        <div class="m-card" style="margin-bottom:16px;">
          <div class="m-card-title">Version Comparison Overview</div>
          <div style="font-size:12px;display:flex;gap:20px;">
            <div><strong>Base:</strong> ${this.versionA}</div>
            <div><strong>Target:</strong> ${this.versionB}</div>
            <div><strong>Total Changes:</strong> ${changes.length}</div>
          </div>
        </div>

        <div class="m-card">
          <div class="m-card-title">Altered Files & Definitions (${changes.length})</div>
          <div style="display:flex;flex-direction:column;gap:4px;font-family:var(--font-mono);font-size:11px;max-height:500px;overflow-y:auto;">
            ${
              changes.length > 0
                ? changes.map((ch) => `
                    <div class="diff-file-row" data-path="${ch.file_path || ch.file || ch.name || ''}" style="display:flex;gap:12px;padding:5px 8px;border-bottom:1px solid var(--border-muted);cursor:pointer;border-radius:3px;">
                      <span style="font-weight:bold;color:${ch.type === 'added' ? 'var(--accent-green)' : (ch.type === 'deleted' ? 'var(--accent-red)' : 'var(--accent-yellow)')};">
                        [${(ch.type || 'MODIFIED').toUpperCase()}]
                      </span>
                      <span style="color:var(--text-primary);font-weight:500;">${ch.file_path || ch.file || ch.symbol || ch.name}</span>
                    </div>
                  `).join("")
                : `<div style="color:var(--text-muted);padding:12px;">Identical versions or no alterations found.</div>`
            }
          </div>
        </div>
      `;

      resContainer.querySelectorAll(".diff-file-row").forEach((el) => {
        el.onclick = () => {
          const p = el.dataset.path;
          if (p) {
            state.openTab({
              type: "code",
              title: p.split("/").pop(),
              path: p,
              version: this.versionB
            });
          }
        };
        el.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            const p = el.dataset.path;
            if (p) {
              state.openTab({
                type: "code",
                title: p.split("/").pop(),
                path: p,
                version: this.versionB,
                forceNew: true
              });
            }
          }
        });
      });
    } catch (e) {
      resContainer.innerHTML = `<div style="color:var(--accent-red);">Failed: ${e.message}</div>`;
    }
  }
}
