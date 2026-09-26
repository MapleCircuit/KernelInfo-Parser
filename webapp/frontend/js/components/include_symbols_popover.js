/**
 * IncludeSymbolsPopover - Anchored floating popover for inspecting imported symbols from #include directives.
 */
import { state } from "../state.js";
import { toast } from "./toast.js";

export class IncludeSymbolsPopover {
  constructor() {
    this.container = null;
    this.currentData = null;
    this.currentCategory = "All";
    this.currentQuery = "";
    this._handleKeyDown = this._handleKeyDown.bind(this);
    this._handleClickOutside = this._handleClickOutside.bind(this);
  }

  show(x, y, includeInfo, data) {
    this.close();

    this.currentData = data || { symbols: [], header_file: "", include_text: "" };
    this.includeInfo = includeInfo || {};
    this.currentCategory = "All";
    this.currentQuery = "";

    const popover = document.createElement("div");
    popover.className = "include-symbols-popover";
    popover.onclick = (e) => e.stopPropagation();

    // Position clamping
    const width = 450;
    const height = 480;
    const posX = Math.min(window.innerWidth - width - 16, Math.max(16, x + 8));
    const posY = Math.min(window.innerHeight - height - 16, Math.max(16, y + 16));

    popover.style.left = `${posX}px`;
    popover.style.top = `${posY}px`;

    const targetHeader = data.header_file || includeInfo.header || includeInfo.name || "#include";
    const titleText = data.include_text || includeInfo.header || includeInfo.name || "#include";

    popover.innerHTML = `
      <div class="include-popover-header">
        <div class="include-popover-title-group">
          <span class="include-popover-icon">📦</span>
          <span class="include-popover-title" title="${this.escapeHtml(titleText)}">${this.escapeHtml(titleText)}</span>
        </div>
        <div class="include-popover-actions">
          ${
            data.header_file
              ? `<button id="btn-include-open-header" class="btn-include-header" title="Open ${this.escapeHtml(data.header_file)}">
                  <span>📄</span> Open Header
                </button>`
              : ""
          }
          <button id="btn-include-close" class="btn-include-close" title="Close (Esc)">&times;</button>
        </div>
      </div>

      <div class="include-popover-toolbar">
        <div class="include-search-wrapper">
          <span class="include-search-icon">🔍</span>
          <input id="include-symbols-search" type="text" placeholder="Filter imported symbols..." autocomplete="off" spellcheck="false" />
        </div>
        <div id="include-category-pills" class="include-category-pills"></div>
      </div>

      <div id="include-symbols-list" class="include-symbols-list"></div>

      <div class="include-popover-footer">
        <span id="include-footer-stats">0 imported symbols</span>
        <span class="include-footer-hint">Click to jump • Middle-click for new tab</span>
      </div>
    `;

    document.body.appendChild(popover);
    this.container = popover;

    // Attach listeners
    const closeBtn = popover.querySelector("#btn-include-close");
    if (closeBtn) closeBtn.onclick = () => this.close();

    const openHeaderBtn = popover.querySelector("#btn-include-open-header");
    if (openHeaderBtn) {
      openHeaderBtn.onclick = () => {
        const hFile = data.header_file;
        this.close();
        if (hFile) {
          state.openTab({
            type: "code",
            title: hFile.split("/").pop(),
            path: hFile,
            cursorLine: 1,
            version: state.currentVersion,
          });
          toast.success(`Opened header: ${hFile}`);
        }
      };
    }

    const searchInput = popover.querySelector("#include-symbols-search");
    if (searchInput) {
      searchInput.oninput = (e) => {
        this.currentQuery = e.target.value.toLowerCase().trim();
        this.renderList();
      };
      // Auto-focus search input
      setTimeout(() => searchInput.focus(), 50);
    }

    this.renderCategoryPills();
    this.renderList();

    // Global dismiss listeners
    document.addEventListener("keydown", this._handleKeyDown);
    setTimeout(() => {
      document.addEventListener("click", this._handleClickOutside);
    }, 10);
  }

  renderCategoryPills() {
    if (!this.container || !this.currentData) return;
    const pillsContainer = this.container.querySelector("#include-category-pills");
    if (!pillsContainer) return;

    const allSymbols = this.currentData.symbols || [];
    const counts = { All: allSymbols.length };
    allSymbols.forEach((s) => {
      const cat = s.category || "Symbol";
      counts[cat] = (counts[cat] || 0) + 1;
    });

    const categories = ["All", ...Object.keys(counts).filter((c) => c !== "All").sort()];
    pillsContainer.innerHTML = "";

    categories.forEach((cat) => {
      const btn = document.createElement("button");
      btn.className = `include-pill ${cat === this.currentCategory ? "active" : ""}`;
      btn.innerHTML = `${this.escapeHtml(cat)} <span class="pill-count">(${counts[cat] || 0})</span>`;
      btn.onclick = () => {
        this.currentCategory = cat;
        this.renderCategoryPills();
        this.renderList();
      };
      pillsContainer.appendChild(btn);
    });
  }

  renderList() {
    if (!this.container || !this.currentData) return;
    const listEl = this.container.querySelector("#include-symbols-list");
    const statsEl = this.container.querySelector("#include-footer-stats");
    if (!listEl) return;

    const allSymbols = this.currentData.symbols || [];
    const filtered = allSymbols.filter((s) => {
      if (this.currentCategory !== "All" && (s.category || "Symbol") !== this.currentCategory) {
        return false;
      }
      if (this.currentQuery) {
        const nameMatch = (s.name || "").toLowerCase().includes(this.currentQuery);
        const fileMatch = (s.def_file || "").toLowerCase().includes(this.currentQuery);
        return nameMatch || fileMatch;
      }
      return true;
    });

    if (statsEl) {
      statsEl.innerText = `${filtered.length} of ${allSymbols.length} imported symbols`;
    }

    if (filtered.length === 0) {
      listEl.innerHTML = `
        <div class="include-empty-state">
          ${allSymbols.length === 0 ? "No imported symbols directly referenced in this file." : "No matching symbols found."}
        </div>
      `;
      return;
    }

    listEl.innerHTML = "";
    filtered.forEach((sym) => {
      const row = document.createElement("div");
      row.className = "include-symbol-row";

      const catBadge = document.createElement("span");
      catBadge.className = `include-badge include-badge-${(sym.category || "symbol").toLowerCase()}`;
      catBadge.innerText = sym.category || "Symbol";

      const nameSpan = document.createElement("span");
      nameSpan.className = "include-symbol-name";
      nameSpan.innerText = sym.name || "";
      nameSpan.title = sym.name || "";

      const leftGroup = document.createElement("div");
      leftGroup.className = "include-row-left";
      leftGroup.appendChild(catBadge);
      leftGroup.appendChild(nameSpan);

      const rightGroup = document.createElement("div");
      rightGroup.className = "include-row-right";

      if (sym.def_file && sym.def_line_s > 0) {
        const locBadge = document.createElement("span");
        locBadge.className = "include-loc-badge";
        const shortFile = sym.def_file.split("/").slice(-2).join("/");
        locBadge.innerText = `${shortFile}:${sym.def_line_s}`;
        locBadge.title = `${sym.def_file}:${sym.def_line_s}`;
        rightGroup.appendChild(locBadge);
      }

      const arrow = document.createElement("span");
      arrow.className = "include-row-arrow";
      arrow.innerText = "➔";
      rightGroup.appendChild(arrow);

      row.appendChild(leftGroup);
      row.appendChild(rightGroup);

      // Left-click jump to definition
      row.onclick = () => {
        this.openSymbol(sym, false);
      };

      // Middle-click multi-tab queueing
      row.addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          e.stopPropagation();
          this.openSymbol(sym, true);
        }
      });

      listEl.appendChild(row);
    });
  }

  openSymbol(sym, forceNew = false) {
    const targetFile = sym.def_file || this.currentData.header_file;
    const line = sym.def_line_s || 1;

    if (targetFile) {
      state.openTab({
        type: "code",
        title: targetFile.split("/").pop(),
        path: targetFile,
        cursorLine: line,
        version: state.currentVersion,
        forceNew: Boolean(forceNew),
      });
      toast.success(`Jumped to ${sym.name} in ${targetFile}:${line}`);
      if (!forceNew) {
        this.close();
      }
    } else {
      toast.info(`Definition location for ${sym.name} not available`);
    }
  }

  close() {
    if (this.container) {
      this.container.remove();
      this.container = null;
    }
    document.removeEventListener("keydown", this._handleKeyDown);
    document.removeEventListener("click", this._handleClickOutside);
  }

  _handleKeyDown(e) {
    if (e.key === "Escape") {
      this.close();
    }
  }

  _handleClickOutside(e) {
    if (this.container && !this.container.contains(e.target)) {
      this.close();
    }
  }

  escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
}

export const includeSymbolsPopover = new IncludeSymbolsPopover();
