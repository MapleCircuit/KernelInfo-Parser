/**
 * pahole_view.js - Pahole Struct Layout & Padding Hole Inspector.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { escapeHtml } from "../../components/modal.js";
import { refPicker } from "../../components/ref_picker.js";
import { compareHtml, historySvg } from "./pahole_compare.js";

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
              <button class="code-btn" id="pahole-btn-compare" title="Compare this layout with another revision, member by member (N3)">⇄ Compare with…</button>
              <button class="code-btn" id="pahole-btn-history" title="Size, holes and cachelines at each release where the definition changed (N3)">📈 History</button>
            </div>
          </div>

          <div id="pahole-extra" class="pcmp-panel" hidden></div>
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

    this.bindOverTime(tabData);
    await this.loadLayout();
  }

  /** Compare / History panels (N3), restored from the tab (`cmp`, `hist`). */
  bindOverTime(tabData) {
    const c = this.containerEl;
    c.querySelector("#pahole-btn-compare").onclick = (e) => refPicker.open(e.currentTarget, {
      current: tabData.cmp || "",
      onSelect: (ref) => this.showCompare(ref),
    });
    c.querySelector("#pahole-btn-history").onclick = () => this.showHistory(!state.findTab(this.tabId)?.hist);
    if (tabData.cmp) this.showCompare(tabData.cmp);
    else if (tabData.hist) this.showHistory(true);
  }

  async showCompare(other) {
    const panel = this.containerEl.querySelector("#pahole-extra");
    state.updateTabState(this.tabId, { cmp: other, hist: false });
    panel.hidden = false;
    panel.innerHTML = `<div class="kconfig-muted">Comparing struct ${escapeHtml(this.structName)} at ${escapeHtml(this.currentVersion)} and ${escapeHtml(other)}…</div>`;
    try {
      const res = await this.api.request(`/api/tools/pahole/compare?${new URLSearchParams({ a: this.currentVersion, b: other, struct: this.structName })}`, { timeoutMs: 60000 });
      panel.innerHTML = `<div class="pcmp-head"><strong>Layout ${escapeHtml(this.currentVersion)} → ${escapeHtml(other)}</strong><button class="code-btn" type="button" data-close>✕</button></div>${compareHtml(res, this.currentVersion, other)}`;
    } catch (err) {
      panel.innerHTML = `<div class="pcmp-head"><span style="color:var(--accent-red);">Compare failed: ${escapeHtml(err.message)}</span><button class="code-btn" type="button" data-close>✕</button></div>`;
    }
    this.bindPanelClose(panel);
  }

  async showHistory(on) {
    const panel = this.containerEl.querySelector("#pahole-extra");
    state.updateTabState(this.tabId, { hist: on, cmp: "" });
    if (!on) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    panel.innerHTML = `<div class="kconfig-muted pcmp-progress">Computing the layout at each release where struct ${escapeHtml(this.structName)} changed…</div>`;
    try {
      const res = await this.api.requestJob(`/api/tools/${encodeURIComponent(this.currentVersion)}/pahole/${encodeURIComponent(this.structName)}/history`, {
        timeoutMs: 120000,
        onProgress: (p) => { const el = panel.querySelector(".pcmp-progress"); if (el) el.textContent = `Computing ${p?.note || "…"} (${p?.done ?? 0}/${p?.total ?? "?"})…`; },
      });
      const missing = res.stops.filter((s) => !s.available).length;
      panel.innerHTML = `<div class="pcmp-head"><strong>${escapeHtml(res.struct)} over time</strong> <span class="kconfig-muted">${res.stops.length} release${res.stops.length === 1 ? "" : "s"} where the definition changed${missing ? `, ${missing} not indexed` : ""} · bars: holes · hollow points: estimates · click a point to open that layout</span><button class="code-btn" type="button" data-close>✕</button></div>
        ${historySvg(res.stops) || '<div class="kconfig-muted">No indexed release has this definition.</div>'}`;
      panel.querySelectorAll("circle[data-version]").forEach((dot) => {
        dot.addEventListener("click", (e) => state.openTab({ type: "pahole", title: `struct ${this.structName}`, structName: this.structName, version: dot.dataset.version, forceNew: true }));
      });
    } catch (err) {
      panel.innerHTML = `<div class="pcmp-head"><span style="color:var(--accent-red);">History failed: ${escapeHtml(err.message)}</span><button class="code-btn" type="button" data-close>✕</button></div>`;
    }
    this.bindPanelClose(panel);
  }

  bindPanelClose(panel) {
    const btn = panel.querySelector("[data-close]");
    if (btn) btn.onclick = () => {
      panel.hidden = true;
      state.updateTabState(this.tabId, { cmp: "", hist: false });
    };
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
      const res = await this.api.getPaholeLayout(this.currentVersion, this.structName);
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
                color: "var(--accent-blue)",
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
