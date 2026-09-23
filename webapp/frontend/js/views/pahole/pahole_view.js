/**
 * pahole_view.js - Pahole Struct Layout & Padding Hole Inspector.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";

export class PaholeView {
  constructor() {
    this.currentVersion = "v3.0";
    this.structName = "task_struct";
    this.containerEl = null;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.currentVersion = tabData.version || state.currentVersion;
    if (tabData.structName) {
      this.structName = tabData.structName;
    }

    containerEl.innerHTML = `
      <div class="tools-view-container">
        <div class="pahole-container">
          <div class="pahole-header">
            <div>
              <div class="pahole-title">Pahole Struct Layout: struct ${this.structName}</div>
              <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                Visualizes 64-byte cacheline boundaries, alignment holes, and packing efficiency.
              </div>
              <div id="pahole-def-link-container"></div>
            </div>
            <div style="display:flex;gap:8px;">
              <input type="text" id="pahole-search-input" value="${this.structName}" placeholder="Struct name..." />
              <button class="code-btn active" id="pahole-search-btn">Analyze</button>
              <button class="code-btn" id="pahole-btn-nodemap" title="Open struct layout in NodeMap">🗺️ Open in NodeMap</button>
            </div>
          </div>

          <div id="pahole-results-container">
            <div style="color:var(--text-muted);">Analyzing struct layout...</div>
          </div>
        </div>
      </div>
    `;

    const btn = containerEl.querySelector("#pahole-search-btn");
    const input = containerEl.querySelector("#pahole-search-input");

    const triggerSearch = () => {
      this.structName = input.value.trim() || "task_struct";
      this.loadLayout();
    };

    btn.onclick = triggerSearch;
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        triggerSearch();
      }
    });

    await this.loadLayout();
  }

  async loadLayout() {
    const titleEl = this.containerEl.querySelector(".pahole-title");
    if (titleEl) {
      titleEl.textContent = `Pahole Struct Layout: struct ${this.structName}`;
    }

    const resultsContainer = this.containerEl.querySelector("#pahole-results-container");
    const defLinkContainer = this.containerEl.querySelector("#pahole-def-link-container");

    try {
      const res = await api.getPaholeLayout(this.currentVersion, this.structName);
      const members = res.members || [];

      // Link to definition in Code View
      if (defLinkContainer && res.file_path) {
        defLinkContainer.innerHTML = `
          <div style="font-size:11px;color:var(--accent-blue);cursor:pointer;margin-top:4px;" id="pahole-open-src">
            📄 Open Definition in Code View (${res.file_path}:${res.line_s || 1})
          </div>
        `;
        defLinkContainer.querySelector("#pahole-open-src").onclick = () => {
          state.openTab({
            type: "code",
            title: res.file_path.split("/").pop(),
            path: res.file_path,
            cursorLine: res.line_s || 1,
            version: this.currentVersion
          });
        };
      }

      // Wire Open in NodeMap button
      const nmBtn = this.containerEl.querySelector("#pahole-btn-nodemap");
      if (nmBtn) {
        nmBtn.onclick = () => {
          const constituents = members.map((m) => ({ name: `${m.type} ${m.name}` }));
          state.openTab({
            type: "nodemap",
            title: `struct ${this.structName}`,
            version: this.currentVersion,
            initialNodes: [
              {
                id: `struct-${this.structName}`,
                title: `struct ${this.structName}`,
                type: "struct",
                x: 240,
                y: 120,
                color: "#58a6ff",
                expanded: true,
                constituents
              }
            ],
            initialEdges: []
          });
        };
      }

      let tableRows = "";
      members.forEach((m) => {
        // If there was alignment padding before this member, show a dedicated hole row
        if (m.padding_before && m.padding_before > 0) {
          tableRows += `
            <tr class="padding-hole-row">
              <td>${m.offset - m.padding_before}</td>
              <td>${m.padding_before}</td>
              <td style="color:var(--accent-orange);font-style:italic;">hole</td>
              <td><span class="padding-hole-badge">/* ${m.padding_before} byte alignment hole */</span></td>
              <td>Line ${Math.floor((m.offset - m.padding_before) / 64)}</td>
            </tr>
          `;
        }

        const isCacheBoundary = m.offset % 64 === 0 && m.offset > 0;
        const rowClass = isCacheBoundary ? "cacheline-boundary" : "";
        const cachelineIdx = Math.floor(m.offset / 64);

        const cleanType = String(m.type || "").trim();
        let typeHtml = cleanType;
        if (cleanType.startsWith("struct ")) {
          const cleanStruct = cleanType.replace(/^struct\s+/, "").replace(/[*&[].*$/, "").trim();
          typeHtml = `<span class="struct-drilldown" data-struct="${cleanStruct}" style="color:var(--accent-blue);cursor:pointer;text-decoration:underline;" title="Drilldown into struct ${cleanStruct}">${cleanType}</span>`;
        }

        tableRows += `
          <tr class="${rowClass}">
            <td>${m.offset}</td>
            <td>${m.size}</td>
            <td>${typeHtml}</td>
            <td style="font-weight:500;">${m.name}</td>
            <td>Line ${cachelineIdx}</td>
          </tr>
        `;
      });

      resultsContainer.innerHTML = `
        <div class="m-grid-2col" style="margin-bottom:16px;">
          <div class="m-card">
            <div class="m-card-title">Layout Metrics</div>
            <div style="font-size:12px;display:flex;flex-direction:column;gap:4px;">
              <div><strong>Total Size:</strong> ${res.total_size || 0} bytes</div>
              <div><strong>Cachelines Spanned:</strong> ${res.cache_lines_used ?? res.cachelines ?? 1} (64-byte lines)</div>
              <div><strong>Padding Holes:</strong> ${res.holes_count || 0}</div>
              <div><strong>Wasted Padding:</strong> ${res.padding_bytes ?? res.padding_wasted ?? 0} bytes</div>
            </div>
          </div>
          <div class="m-card">
            <div class="m-card-title">Optimization Assessment</div>
            <div style="font-size:12px;color:var(--accent-green);">
              ${res.holes_count > 0 ? "Potential cacheline re-ordering identified to eliminate alignment holes." : "Struct packing is optimal."}
            </div>
          </div>
        </div>

        <table class="pahole-table">
          <thead>
            <tr>
              <th>Offset</th>
              <th>Size</th>
              <th>Type</th>
              <th>Member Name</th>
              <th>Cacheline</th>
            </tr>
          </thead>
          <tbody>
            ${tableRows}
          </tbody>
        </table>
      `;

      // Drilldown into nested structs
      resultsContainer.querySelectorAll(".struct-drilldown").forEach((el) => {
        el.onclick = () => {
          this.structName = el.dataset.struct;
          const searchInput = this.containerEl.querySelector("#pahole-search-input");
          if (searchInput) searchInput.value = this.structName;
          this.loadLayout();
        };
      });

    } catch (e) {
      resultsContainer.innerHTML = `<div style="color:var(--accent-red);">Failed: ${e.message}</div>`;
    }
  }
}
