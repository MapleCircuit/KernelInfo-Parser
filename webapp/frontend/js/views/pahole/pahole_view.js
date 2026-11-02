/**
 * pahole_view.js - Pahole Struct Layout & Padding Hole Inspector.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { escapeHtml } from "../../components/modal.js";

export class PaholeView {
  constructor() {
    this.currentVersion = "v3.0";
    this.structName = "task_struct";
    this.containerEl = null;
    this.tabId = null;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tabId = tabData.id;
    this.currentVersion = tabData.version || state.currentVersion;
    if (tabData.structName) {
      this.structName = tabData.structName.replace(/^(struct|union)\s+/, "").trim();
    }

    containerEl.innerHTML = `
      <div class="tools-view-container">
        <div class="pahole-container">
          <div class="pahole-header">
            <div>
              <div class="pahole-title">Pahole Struct Layout: struct ${escapeHtml(this.structName)}</div>
              <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                Visualizes 64-byte cacheline boundaries, alignment holes, and packing efficiency.
              </div>
              <div id="pahole-def-link-container"></div>
            </div>
            <div style="display:flex;gap:8px;">
              <input type="text" id="pahole-search-input" value="${escapeHtml(this.structName)}" placeholder="Struct name..." />
              <button class="code-btn active" id="pahole-search-btn">Analyze</button>
              <button class="code-btn" id="pahole-btn-nodemap" title="Open struct layout in NodeMap">🗺️ Open in NodeMap</button>
            </div>
          </div>

          <div id="pahole-results-container">
            <div style="color:var(--text-muted);">Analyzing struct layout: struct ${escapeHtml(this.structName)}...</div>
          </div>
        </div>
      </div>
    `;

    const btn = containerEl.querySelector("#pahole-search-btn");
    const input = containerEl.querySelector("#pahole-search-input");

    const triggerSearch = (forceNew = false) => {
      const val = input.value.trim().replace(/^(struct|union)\s+/, "") || "task_struct";
      if (forceNew) {
        state.openTab({
          type: "pahole",
          title: `struct ${val}`,
          structName: val,
          version: this.currentVersion,
          forceNew: true
        });
        return;
      }
      this.structName = val;
      this.loadLayout();
    };

    btn.onclick = (e) => {
      triggerSearch(Boolean(e && (e.ctrlKey || e.metaKey)));
    };
    btn.addEventListener("auxclick", (e) => {
      if (e.button === 1) {
        e.preventDefault();
        e.stopPropagation();
        triggerSearch(true);
      }
    });

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        triggerSearch(Boolean(e.ctrlKey || e.metaKey));
      }
    });

    await this.loadLayout();
  }

  async loadLayout() {
    const titleEl = this.containerEl.querySelector(".pahole-title");
    if (titleEl) {
      titleEl.textContent = `Pahole Struct Layout: struct ${this.structName}`;
    }

    const tab = (this.tabId ? state.findTab(this.tabId) : null) || state.getActiveTab();
    if (tab && tab.type === "pahole") {
      state.updateTabState(tab.id, {
        structName: this.structName,
        title: `struct ${this.structName}`
      });
    }

    const resultsContainer = this.containerEl.querySelector("#pahole-results-container");
    const defLinkContainer = this.containerEl.querySelector("#pahole-def-link-container");
    if (defLinkContainer) {
      defLinkContainer.innerHTML = "";
    }
    if (resultsContainer) {
      resultsContainer.innerHTML = `<div style="color:var(--text-muted);">Analyzing struct layout: struct ${escapeHtml(this.structName)}...</div>`;
    }

    try {
      const res = await api.getPaholeLayout(this.currentVersion, this.structName);
      const members = res.members || [];

      // Link to definition in Code View
      if (defLinkContainer && res.file_path) {
        defLinkContainer.innerHTML = `
          <div style="font-size:11px;color:var(--accent-blue);cursor:pointer;margin-top:4px;" id="pahole-open-src">
            📄 Open Definition in Code View (${escapeHtml(res.file_path)}:${res.line_s || 1})
          </div>
        `;
        const openSrcLink = defLinkContainer.querySelector("#pahole-open-src");
        const openSrc = (forceNew = false) => {
          state.openTab({
            type: "code",
            title: res.file_path.split("/").pop(),
            path: res.file_path,
            cursorLine: res.line_s || 1,
            version: this.currentVersion,
            forceNew: Boolean(forceNew)
          });
        };
        openSrcLink.onclick = (e) => {
          openSrc(Boolean(e && (e.ctrlKey || e.metaKey)));
        };
        openSrcLink.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            openSrc(true);
          }
        });
      }

      // Wire Open in NodeMap button
      const nmBtn = this.containerEl.querySelector("#pahole-btn-nodemap");
      if (nmBtn) {
        const openNodeMap = (forceNew = false) => {
          const constituents = members.map((m) => ({ name: `${m.type} ${m.name}` }));
          state.openTab({
            type: "nodemap",
            title: `struct ${this.structName}`,
            version: this.currentVersion,
            forceNew: Boolean(forceNew),
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
        nmBtn.onclick = (e) => {
          openNodeMap(Boolean(e && (e.ctrlKey || e.metaKey)));
        };
        nmBtn.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            openNodeMap(true);
          }
        });
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
        let typeHtml = escapeHtml(cleanType);
        if (cleanType.startsWith("struct ") || cleanType.startsWith("union ")) {
          const cleanStruct = cleanType.replace(/^(struct|union)\s+/, "").replace(/[*&[].*$/, "").trim();
          typeHtml = `<span class="struct-drilldown" data-struct="${escapeHtml(cleanStruct)}" style="color:var(--accent-blue);cursor:pointer;text-decoration:underline;" title="Drilldown into ${escapeHtml(cleanType)}">${escapeHtml(cleanType)}</span>`;
        }

        tableRows += `
          <tr class="${rowClass}">
            <td>${m.offset}</td>
            <td${m.estimated ? ' title="Estimated: the index records only the type category of this member"' : ""}>${m.estimated ? "~" : ""}${m.size}</td>
            <td>${typeHtml}</td>
            <td style="font-weight:500;">${escapeHtml(m.name || "")}</td>
            <td>Line ${cachelineIdx}</td>
          </tr>
        `;
      });

      const estimateNote = res.is_estimate
        ? `<div class="m-card" style="margin-bottom:12px;font-size:12px;color:var(--accent-yellow);">
            Estimated layout: ${res.estimated_members} of ${members.length} members (typedefs, arrays, embedded structs, unsigned types) are counted as 8 bytes, because the index records only their type category. Offsets after the first estimated member are approximate.
          </div>`
        : "";
      resultsContainer.innerHTML = `
        ${estimateNote}
        <div class="m-grid-2col" style="margin-bottom:16px;">
          <div class="m-card">
            <div class="m-card-title">Layout Metrics</div>
            <div style="font-size:12px;display:flex;flex-direction:column;gap:4px;">
              <div><strong>Total Size:</strong> ${res.is_estimate ? "~" : ""}${res.total_size || 0} bytes</div>
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

      // Drilldown into nested structs / unions
      resultsContainer.querySelectorAll(".struct-drilldown").forEach((el) => {
        const cleanStruct = (el.dataset.struct || "").replace(/^(struct|union)\s+/, "").trim();
        const openPaholeTab = (forceNew = true) => {
          state.openTab({
            type: "pahole",
            title: `struct ${cleanStruct}`,
            structName: cleanStruct,
            version: this.currentVersion,
            forceNew: Boolean(forceNew)
          });
        };

        el.onclick = (e) => {
          if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            e.stopPropagation();
            openPaholeTab(true);
            return;
          }
          this.structName = cleanStruct;
          const searchInput = this.containerEl.querySelector("#pahole-search-input");
          if (searchInput) searchInput.value = this.structName;
          this.loadLayout();
        };

        el.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            openPaholeTab(true);
          }
        });
      });

    } catch (e) {
      resultsContainer.innerHTML = `<div style="color:var(--accent-red);padding:12px 0;">Failed to load layout: ${escapeHtml(e.message || "Unknown error")}</div>`;
    }
  }
}
