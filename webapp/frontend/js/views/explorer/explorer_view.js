/**
 * explorer_view.js - Full-tab repository explorer (tab type `explorer`).
 *
 * Browses one revision's Git tree in a whole tab instead of the sidebar: clicking the path in a code
 * view opens the file's directory here, and the header's **New Tab** button opens the root of the
 * selected revision. Navigation stays inside the tab (the tab remembers its directory, its filter
 * and its scroll position) and everything it opens keeps the tab's revision, so an explorer on
 * v3.1 never hands a file to a tab on another revision.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { contextMenu } from "../../components/context_menu.js";
import { shortRef } from "../../components/ref_picker.js";
import { escapeHtml } from "../../components/modal.js";

// Extension -> icon of the entry list (directories and plain files have their own)
const FILE_ICONS = [
  [/\.(c|cc|cpp)$/i, "🅒"],
  [/\.h$/i, "🅗"],
  [/\.(S|s|asm)$/i, "⚙"],
  [/\.rs$/i, "🦀"],
  [/^(Kconfig|Kconfig\..*)$/i, "🧩"],
  [/^(Makefile|Kbuild|.*\.mk)$/i, "🛠"],
  [/\.(rst|txt|md)$/i, "📘"],
  [/\.(dts|dtsi)$/i, "🌲"],
  [/\.(sh|pl|py)$/i, "📜"],
];

function iconFor(entry) {
  if (entry.type === "dir") return "📁";
  for (const [re, icon] of FILE_ICONS) {
    if (re.test(entry.name)) return icon;
  }
  return "📄";
}

export class ExplorerView {
  constructor() {
    this.tabData = null;
    this.containerEl = null;
    this.version = "";
    this.path = "";
    this.entries = [];
    this.filter = "";
    this.loadToken = 0;
  }

  async render(containerEl, tabData) {
    this.tabData = tabData;
    this.containerEl = containerEl;
    this.version = tabData.version || state.currentVersion;
    this.path = (tabData.path || "").replace(/^\/+|\/+$/g, "");
    this.filter = tabData.filter || "";

    containerEl.innerHTML = `
      <div class="explorer-container">
        <div class="explorer-toolbar">
          <div class="explorer-breadcrumbs" id="explorer-breadcrumbs"></div>
          <div class="explorer-actions">
            <input type="text" class="explorer-filter" id="explorer-filter" placeholder="Filter this directory…" spellcheck="false" />
            <button class="code-btn" id="explorer-up" title="Go to the parent directory">↑ Up</button>
            <button class="code-btn" id="explorer-reveal" title="Reveal this directory in the sidebar explorer">Reveal</button>
            <button class="code-btn" id="explorer-treemap" title="Open the treemap of this directory">Treemap</button>
          </div>
        </div>
        <div class="explorer-listing" id="explorer-listing">
          <div class="explorer-empty">Loading ${escapeHtml(this.path || "repository root")}…</div>
        </div>
        <div class="explorer-status" id="explorer-status"></div>
      </div>
    `;

    const filterInput = containerEl.querySelector("#explorer-filter");
    filterInput.value = this.filter;
    filterInput.oninput = () => {
      this.filter = filterInput.value.trim();
      this.tabData.filter = this.filter;
      state.saveState();
      this.renderListing();
    };

    containerEl.querySelector("#explorer-up").onclick = () => {
      if (!this.path) return;
      this.navigate(this.path.split("/").slice(0, -1).join("/"));
    };
    containerEl.querySelector("#explorer-reveal").onclick = () => {
      state.emit("explorer:reveal", this.path);
    };
    containerEl.querySelector("#explorer-treemap").onclick = () => {
      state.openTab({
        type: "treemap",
        title: `Treemap: ${this.path.split("/").pop() || "root"}`,
        path: this.path,
        version: this.version,
      });
    };

    const listing = containerEl.querySelector("#explorer-listing");
    listing.addEventListener("scroll", () => {
      this.tabData.scrollTop = listing.scrollTop;
    });

    this.renderBreadcrumbs();
    await this.load();
  }

  /** Move to another directory of the same revision, in this tab. */
  navigate(path) {
    this.path = (path || "").replace(/^\/+|\/+$/g, "");
    this.tabData.path = this.path;
    this.tabData.title = this.path ? this.path.split("/").pop() : `Explorer ${shortRef(this.version)}`;
    this.tabData.scrollTop = 0;
    state.saveState();
    // Re-renders the tab strip (new directory name) and this view, which lists the new directory
    state.emit("tabs:changed", { paneId: state.activePaneId, activeTabId: this.tabData.id });
  }

  renderBreadcrumbs() {
    const el = this.containerEl?.querySelector("#explorer-breadcrumbs");
    if (!el) return;
    const parts = this.path.split("/").filter(Boolean);
    let accum = "";
    let html = `<span class="explorer-crumb-ref" title="Revision this explorer browses">${escapeHtml(shortRef(this.version))}</span>`
      + `<span class="breadcrumb-sep">&gt;</span><span class="explorer-crumb" data-path="">root</span>`;
    parts.forEach((part, idx) => {
      accum += (accum ? "/" : "") + part;
      const isLast = idx === parts.length - 1;
      html += `<span class="breadcrumb-sep">&gt;</span>`;
      html += isLast
        ? `<span class="explorer-crumb active">${escapeHtml(part)}</span>`
        : `<span class="explorer-crumb" data-path="${escapeHtml(accum)}">${escapeHtml(part)}</span>`;
    });
    el.innerHTML = html;
    el.querySelectorAll(".explorer-crumb[data-path]").forEach((crumb) => {
      crumb.onclick = () => this.navigate(crumb.dataset.path);
    });
  }

  async load() {
    const listing = this.containerEl?.querySelector("#explorer-listing");
    if (!listing) return;
    const token = ++this.loadToken;
    listing.innerHTML = `<div class="explorer-empty">Loading ${escapeHtml(this.path || "repository root")}…</div>`;
    try {
      const res = await api.getDirectoryTree(this.version, this.path, 1);
      if (token !== this.loadToken) return;
      this.entries = res.entries || res.tree || [];
      this.renderListing();
    } catch (err) {
      if (token !== this.loadToken) return;
      listing.innerHTML = `<div class="explorer-error">Failed to list ${escapeHtml(this.path || "the repository root")} at ${escapeHtml(this.version)}: ${escapeHtml(err.message)}</div>`;
    }
  }

  renderListing() {
    const listing = this.containerEl?.querySelector("#explorer-listing");
    const status = this.containerEl?.querySelector("#explorer-status");
    if (!listing) return;
    const q = this.filter.toLowerCase();
    const shown = q ? this.entries.filter((e) => e.name.toLowerCase().includes(q)) : this.entries;
    const dirs = shown.filter((e) => e.type === "dir").length;

    if (!shown.length) {
      listing.innerHTML = `<div class="explorer-empty">${this.entries.length ? "No entry matches the filter." : "This directory is empty."}</div>`;
    } else {
      listing.innerHTML = shown.map((entry, idx) => `
        <div class="explorer-row" data-idx="${idx}" data-type="${entry.type}" title="${escapeHtml(entry.path)}">
          <span class="explorer-icon">${iconFor(entry)}</span>
          <span class="explorer-name ${entry.type === "dir" ? "dir" : ""}">${escapeHtml(entry.name)}</span>
          <span class="explorer-meta">${entry.type === "dir" ? "directory" : escapeHtml(entry.s_stat_label || "file")}</span>
        </div>`).join("");

      listing.querySelectorAll(".explorer-row").forEach((row) => {
        const entry = shown[parseInt(row.dataset.idx, 10)];
        row.onclick = () => this.open(entry, false);
        row.addEventListener("auxclick", (e) => {
          if (e.button !== 1) return;
          e.preventDefault();
          e.stopPropagation();
          this.open(entry, true);
        });
        row.addEventListener("contextmenu", (e) => {
          e.preventDefault();
          e.stopPropagation();
          contextMenu.showFileTreeMenu(e.clientX, e.clientY, {
            item: entry,
            isDir: entry.type === "dir",
            path: entry.path,
            version: this.version,
          });
        });
      });
      listing.scrollTop = this.tabData.scrollTop || 0;
    }

    if (status) {
      status.innerHTML = `${shown.length} entr${shown.length === 1 ? "y" : "ies"} · ${dirs} director${dirs === 1 ? "y" : "ies"}`
        + `${q ? ` (filtered from ${this.entries.length})` : ""} · <code>${escapeHtml(this.path || "/")}</code> @ ${escapeHtml(this.version)}`;
    }
  }

  /** Directories open in this tab (or a new explorer tab); files open in a code tab, same revision. */
  open(entry, forceNew) {
    if (!entry) return;
    if (entry.type === "dir") {
      if (forceNew) {
        state.openTab({ type: "explorer", title: entry.name, path: entry.path, version: this.version, forceNew: true });
      } else {
        this.navigate(entry.path);
      }
      return;
    }
    state.openTab({
      type: "code",
      title: entry.name,
      path: entry.path,
      version: this.version,
      forceNew: Boolean(forceNew),
    });
  }
}
