/**
 * app.js - Main Application Bootstrap & Lifecycle Orchestrator.
 */
import { api } from "./api.js";
import { state } from "./state.js";
import { urlSync } from "./url_sync.js";
import { toast } from "./components/toast.js";
import { TabsComponent } from "./components/tabs.js";
import { contextMenu } from "./components/context_menu.js";
import { localDB } from "./db.js";

// View Renderers
import { VirtualEditor } from "./views/code_view/virtual_editor.js";
import { KconfigView } from "./views/kconfig/kconfig_view.js";
import { NodeMapController } from "./views/nodemap/nodemap_controller.js";
import { MaintainersView } from "./views/maintainers/maintainers_view.js";
import { CommitsView } from "./views/commits/commits_view.js";
import { PaholeView } from "./views/pahole/pahole_view.js";
import { CallgraphView } from "./views/callgraph/callgraph_view.js";
import { DiffView } from "./views/diff/diff_view.js";

export class KernelIdeApp {
  constructor() {
    this.tabsComponent = null;
    this.viewRenderers = {
      code: new VirtualEditor(),
      kconfig: new KconfigView(),
      nodemap: new NodeMapController(),
      maintainers: new MaintainersView(),
      commits: new CommitsView(),
      pahole: new PaholeView(),
      callgraph: new CallgraphView(),
      diff: new DiffView()
    };
  }

  async init() {
    console.info("Initializing KernelInfo Developer IDE...");

    // 1. Register Service Worker for offline PWA
    this.registerServiceWorker();

    // 2. Setup Online/Offline Status Indicator
    this.setupNetworkStatus();

    // 2b. Synchronize with Database Instance Hash
    try {
      await api.checkDbSync();
    } catch (e) {
      console.warn("DB instance sync check error:", e);
    }

    // 3. Initialize Version Selector
    await this.initVersionSelector();

    // 4. Mount Multi-Tab Split-Pane Workspace
    const workspaceEl = document.getElementById("workspace");
    this.tabsComponent = new TabsComponent(workspaceEl, this.viewRenderers);
    this.tabsComponent.render();

    // 5. Setup Activity Bar & Sidebar
    this.setupActivityBar();
    await this.loadSidebarFileTree();

    // 6. Initialize URL Synchronization
    urlSync.init();

    // Re-render tabs and sidebar when global version changes
    state.subscribe("version:changed", async () => {
      await this.loadSidebarFileTree();
      this.tabsComponent.render();
    });

    // Synchronize top selector dropdown when tab with different version is selected
    state.subscribe("version:synced", async (v) => {
      const select = document.getElementById("version-select");
      if (select && select.value !== v) {
        select.value = v;
      }
      await this.loadSidebarFileTree();
    });

    console.info("KernelInfo Developer IDE fully initialized.");
  }

  registerServiceWorker() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").then((reg) => {
        console.info("PWA ServiceWorker registered with scope:", reg.scope);
      }).catch((err) => {
        console.warn("ServiceWorker registration failed:", err);
      });
    }
  }

  setupNetworkStatus() {
    const badge = document.getElementById("offline-badge");
    if (!badge) return;

    badge.style.cursor = "pointer";
    badge.title = "Click to open Network & Local Storage Settings";

    const updateBadge = () => {
      if (navigator.onLine) {
        badge.textContent = "● Online";
        badge.className = "offline-badge";
      } else {
        badge.textContent = "● Offline (Cached)";
        badge.className = "offline-badge offline";
        toast.info("Offline mode active: viewing cached repository data");
      }
    };

    window.addEventListener("online", updateBadge);
    window.addEventListener("offline", updateBadge);
    updateBadge();

    badge.addEventListener("click", (e) => {
      e.stopPropagation();
      this.toggleSettingsPopover(badge);
    });
  }

  async toggleSettingsPopover(anchorEl) {
    const existing = document.getElementById("settings-popover-menu");
    if (existing) {
      existing.remove();
      return;
    }

    const popover = document.createElement("div");
    popover.id = "settings-popover-menu";
    popover.className = "settings-popover";

    const rect = anchorEl.getBoundingClientRect();
    popover.style.top = `${rect.bottom + 8}px`;
    popover.style.right = `${Math.max(8, window.innerWidth - rect.right)}px`;

    const storeCounts = await localDB.getStoreCounts();
    const filesCount = storeCounts.files || 0;
    const symbolsCount = storeCounts.symbols || 0;
    const kconfigsCount = storeCounts.kconfigs || 0;
    const maintainersCount = storeCounts.maintainers || 0;
    const commitsCount = storeCounts.commits || 0;
    const toolsCount = storeCounts.tools || 0;
    const totalCachedItems = filesCount + symbolsCount + kconfigsCount + maintainersCount + commitsCount + toolsCount;

    const tabsCount = Array.isArray(state.tabs)
      ? state.tabs.length
      : Array.isArray(state.openTabs)
      ? state.openTabs.length
      : (state.panes?.reduce((sum, p) => sum + (p.tabs?.length || 0), 0) || 0);
    const isOnline = navigator.onLine;

    // Database instance hash and storage estimate
    const cachedHash = (await localDB.get("meta", "db_instance_hash")) || "Default";
    const shortHash = cachedHash.length > 12 ? cachedHash.substring(0, 12) : cachedHash;
    const storageEst = await localDB.getStorageEstimate();
    const usedMB = storageEst?.usage ? (storageEst.usage / (1024 * 1024)).toFixed(1) : null;

    popover.innerHTML = `
      <div class="settings-popover-header">
        <div class="settings-popover-title">
          <span>⚙️ Settings &amp; Local Storage</span>
        </div>
        <button id="close-settings-btn" style="background:none;border:none;color:var(--text-muted);font-size:16px;cursor:pointer;">&times;</button>
      </div>

      <div class="settings-popover-body">
        <!-- 1. Connection & DB Instance Status -->
        <div>
          <div class="settings-section-title">Database &amp; Network</div>
          <div style="display:flex;flex-direction:column;gap:6px;">
            <div style="display:flex;align-items:center;justify-content:space-between;background:var(--bg-primary);padding:6px 10px;border-radius:var(--radius-md);border:1px solid var(--border-color);">
              <div style="display:flex;align-items:center;gap:6px;">
                <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${isOnline ? 'var(--accent-green)' : 'var(--accent-yellow)'};"></span>
                <span style="font-weight:600;color:var(--text-primary);font-size:11px;">${isOnline ? 'Online (Connected)' : 'Offline (Cached)'}</span>
              </div>
              <span style="font-size:10px;color:var(--text-muted);font-family:var(--font-mono);">${isOnline ? 'REST API Active' : 'Fallback DB'}</span>
            </div>

            <div style="display:flex;align-items:center;justify-content:space-between;background:var(--bg-primary);padding:6px 10px;border-radius:var(--radius-md);border:1px solid var(--border-color);" title="Database Instance: ${cachedHash}">
              <div style="display:flex;align-items:center;gap:6px;">
                <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent-blue);"></span>
                <span style="font-size:11px;color:var(--text-secondary);">DB Instance:</span>
                <code style="font-family:var(--font-mono);font-size:11px;color:var(--accent-blue);">${shortHash}</code>
              </div>
              <span style="font-size:10px;color:var(--accent-green);font-weight:500;">● Synced</span>
            </div>
          </div>
        </div>

        <!-- 2. Local Storage Breakdown -->
        <div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
            <span class="settings-section-title" style="margin-bottom:0;">Cached Data Stores</span>
            <span style="font-size:10px;color:var(--text-muted);">${totalCachedItems} entries ${usedMB ? `(${usedMB} MB)` : ''}</span>
          </div>
          <div class="settings-metric-grid" style="grid-template-columns: repeat(3, 1fr);">
            <div class="settings-metric-card">
              <div class="settings-metric-val">${filesCount}</div>
              <div class="settings-metric-lbl">Files</div>
            </div>
            <div class="settings-metric-card">
              <div class="settings-metric-val">${symbolsCount}</div>
              <div class="settings-metric-lbl">Symbols</div>
            </div>
            <div class="settings-metric-card">
              <div class="settings-metric-val">${kconfigsCount}</div>
              <div class="settings-metric-lbl">Kconfig</div>
            </div>
            <div class="settings-metric-card">
              <div class="settings-metric-val">${maintainersCount}</div>
              <div class="settings-metric-lbl">Maintainers</div>
            </div>
            <div class="settings-metric-card">
              <div class="settings-metric-val">${commitsCount}</div>
              <div class="settings-metric-lbl">Commits</div>
            </div>
            <div class="settings-metric-card">
              <div class="settings-metric-val">${toolsCount}</div>
              <div class="settings-metric-lbl">Tools</div>
            </div>
          </div>
        </div>

        <!-- 3. Granular Store Clear Chips -->
        <div>
          <div class="settings-section-title">Clear Specific Store</div>
          <div class="settings-store-chips">
            <button class="settings-chip-btn" data-store="kconfigs">Clear Kconfig</button>
            <button class="settings-chip-btn" data-store="files">Clear Files</button>
            <button class="settings-chip-btn" data-store="symbols">Clear Symbols</button>
            <button class="settings-chip-btn" data-store="maintainers">Clear Maintainers</button>
            <button class="settings-chip-btn" data-store="commits">Clear Commits</button>
            <button class="settings-chip-btn" data-store="tools">Clear Tools</button>
          </div>
        </div>

        <!-- 4. Storage Actions -->
        <div>
          <div class="settings-section-title">Storage Actions</div>
          <div class="settings-actions">
            <button class="settings-btn" id="btn-clear-cache">
              <span>🧹 Clear All Offline Cache (IndexedDB)</span>
              <span style="font-size:10px;color:var(--text-muted);">&rarr;</span>
            </button>
            <button class="settings-btn" id="btn-reset-tabs">
              <span>📑 Reset Tabs &amp; Workspace State (${tabsCount} open)</span>
              <span style="font-size:10px;color:var(--text-muted);">&rarr;</span>
            </button>
            <button class="settings-btn danger" id="btn-full-reset">
              <span>⚠️ Full Local Reset &amp; Reload</span>
              <span style="font-size:10px;">&rarr;</span>
            </button>
          </div>
        </div>
      </div>
    `;

    document.body.appendChild(popover);

    const outsideClickHandler = (e) => {
      if (!popover.contains(e.target) && e.target !== anchorEl) {
        popover.remove();
        document.removeEventListener("click", outsideClickHandler);
      }
    };
    setTimeout(() => document.addEventListener("click", outsideClickHandler), 10);

    popover.querySelector("#close-settings-btn").onclick = () => {
      popover.remove();
      document.removeEventListener("click", outsideClickHandler);
    };

    popover.querySelectorAll(".settings-chip-btn").forEach((chip) => {
      chip.onclick = async () => {
        const storeName = chip.dataset.store;
        try {
          await localDB.clearStore(storeName);
          toast.success(`Cleared ${storeName} offline cache`);
          popover.remove();
        } catch (err) {
          toast.error(`Failed to clear ${storeName}: ${err.message}`);
        }
      };
    });

    popover.querySelector("#btn-clear-cache").onclick = async () => {
      try {
        await localDB.clearDataStores();
        toast.success("Cleared all offline IndexedDB cache stores");
        popover.remove();
      } catch (err) {
        toast.error("Failed to clear offline cache: " + err.message);
      }
    };

    popover.querySelector("#btn-reset-tabs").onclick = async () => {
      try {
        localStorage.removeItem("kernel_ide_open_tabs");
        localStorage.removeItem("kernel_ide_active_tab_id");
        localStorage.removeItem("kernel_ide_state");
        await localDB.delete("tabs_state", "current_session").catch(() => {});
        state.openTabs = [];
        state.tabs = [];
        state.activeTabId = null;
        state.panes = [
          {
            id: "pane-1",
            activeTabId: null,
            tabs: []
          }
        ];
        state.activePaneId = "pane-1";
        state.splitMode = "single";
        state.emit?.("layout:changed", { splitMode: "single", panes: state.panes });
        state.emit?.("tabs:changed", { paneId: "pane-1", activeTabId: null });
        state.saveState?.();
        this.tabsComponent?.render();
        toast.success("Reset open tabs and workspace session");
        popover.remove();
      } catch (err) {
        toast.error("Failed to reset tabs: " + err.message);
      }
    };

    popover.querySelector("#btn-full-reset").onclick = async () => {
      const confirmed = confirm(
        "Are you sure you want to reset all local data?\\n\\nThis will wipe offline IndexedDB caches, clear tabs and local storage settings, and reload the application."
      );
      if (!confirmed) return;

      try {
        await localDB.clearAllStores();
        localStorage.clear();
        if (window.caches) {
          const cacheKeys = await caches.keys();
          await Promise.all(cacheKeys.map((k) => caches.delete(k)));
        }
        toast.info("Local storage cleared. Reloading...");
        setTimeout(() => window.location.reload(), 300);
      } catch (err) {
        toast.error("Error resetting local data: " + err.message);
      }
    };
  }

  async initVersionSelector() {
    const select = document.getElementById("version-select");
    if (!select) return;

    try {
      const res = await api.getVersions();
      const rawVersions = res.versions || ["v3.0"];
      const versionNames = rawVersions.map((v) =>
        typeof v === "string" ? v : (v.vname || v.name || "v3.0")
      );

      // Validate and sanitize state.currentVersion
      if (!versionNames.includes(state.currentVersion)) {
        state.currentVersion = versionNames.includes("v3.0") ? "v3.0" : (versionNames[0] || "v3.0");
      }

      select.innerHTML = "";
      versionNames.forEach((verName) => {
        const opt = document.createElement("option");
        opt.value = verName;
        opt.textContent = verName;
        if (verName === state.currentVersion) opt.selected = true;
        select.appendChild(opt);
      });

      select.onchange = (e) => {
        const val = e.target.value;
        if (val && !val.includes("object")) {
          state.setVersion(val);
        }
      };
    } catch (e) {
      console.warn("Could not load versions list:", e);
    }
  }

  setupActivityBar() {
    const buttons = document.querySelectorAll(".activity-btn");
    const sidebar = document.getElementById("sidebar");

    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const action = btn.dataset.action;

        if (action === "explorer") {
          sidebar.classList.toggle("collapsed");
          btn.classList.toggle("active", !sidebar.classList.contains("collapsed"));
        } else if (action === "kconfig") {
          state.openTab({ type: "kconfig", title: "KConfig (x86)", arch: "x86" });
        } else if (action === "nodemap") {
          state.openTab({ type: "nodemap", title: "NodeMap Canvas" });
        } else if (action === "maintainers") {
          state.openTab({ type: "maintainers", title: "Maintainers" });
        } else if (action === "commits") {
          state.openTab({ type: "commits", title: "Commits" });
        } else if (action === "tools") {
          state.openTab({ type: "diff", title: "Version Diff" });
        }
      });
    });
  }

  async loadSidebarFileTree() {
    const treeContainer = document.getElementById("sidebar-tree-root");
    if (!treeContainer) return;

    if (!this._explorerSubscribed) {
      this._explorerSubscribed = true;
      state.subscribe("explorer:reveal", async (dirPath) => {
        const sidebar = document.getElementById("sidebar");
        if (sidebar && sidebar.classList.contains("collapsed")) {
          sidebar.classList.remove("collapsed");
          const expBtn = document.querySelector('.activity-btn[data-action="explorer"]');
          if (expBtn) expBtn.classList.add("active");
        }

        if (!dirPath) return;
        const parts = dirPath.split("/").filter(Boolean);
        let curPrefix = "";
        for (const part of parts) {
          curPrefix += (curPrefix ? "/" : "") + part;
          const targetRow = document.querySelector(`.tree-item[data-path="${curPrefix}"]`);
          if (targetRow && !targetRow.classList.contains("expanded")) {
            targetRow.click();
          }
        }
      });
    }

    treeContainer.innerHTML = `<div style="padding:12px;color:var(--text-muted);">Loading files...</div>`;

    try {
      const res = await api.getDirectoryTree(state.currentVersion, "", 2);
      const items = res.entries || res.tree || [];
      treeContainer.innerHTML = "";

      items.forEach((item) => {
        this.renderFileTreeNode(treeContainer, item, 0);
      });
    } catch (e) {
      treeContainer.innerHTML = `<div style="padding:12px;color:var(--accent-red);">Failed: ${e.message}</div>`;
    }
  }

  renderFileTreeNode(container, item, depth) {
    const isDir = item.type === "dir" || Boolean(item.children);
    const row = document.createElement("div");
    row.className = `tree-item ${isDir ? "directory" : "file"}`;
    row.dataset.path = item.path || "";
    row.style.paddingLeft = `${12 + depth * 14}px`;

    row.innerHTML = `
      <span class="chevron">${isDir ? "▶" : ""}</span>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="${isDir ? 'var(--accent-blue)' : 'var(--text-muted)'}">
        ${
          isDir
            ? '<path d="M1.75 1A1.75 1.75 0 0 0 0 2.75v10.5C0 14.216.784 15 1.75 15h12.5A1.75 1.75 0 0 0 16 13.25v-8.5A1.75 1.75 0 0 0 14.25 3H7.5a.25.25 0 0 1-.2-.1l-.9-1.2C6.07 1.26 5.55 1 5 1H1.75z"/>'
            : '<path d="M3.75 1.5a.25.25 0 0 0-.25.25v12.5c0 .138.112.25.25.25h8.5a.25.25 0 0 0 .25-.25V6H9.75A1.75 1.75 0 0 1 8 4.25V1.5H3.75zm5.75.56v2.19c0 .138.112.25.25.25h2.19L9.5 2.06zM2 1.75C2 .784 2.784 0 3.75 0h5.586c.464 0 .909.184 1.237.513l3.414 3.414c.329.328.513.773.513 1.237v9.086A1.75 1.75 0 0 1 12.75 16h-8.5A1.75 1.75 0 0 1 2 14.25V1.75z"/>'
        }
      </svg>
      <span style="overflow:hidden;text-overflow:ellipsis;">${item.name}</span>
    `;

    row.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      contextMenu.showFileTreeMenu(e.clientX, e.clientY, {
        item,
        isDir,
        path: item.path,
        version: state.currentVersion
      });
    });

    container.appendChild(row);

    if (isDir) {
      const childContainer = document.createElement("div");
      childContainer.style.display = "none";
      container.appendChild(childContainer);

      let expanded = false;
      row.onclick = async () => {
        expanded = !expanded;
        row.classList.toggle("expanded", expanded);
        childContainer.style.display = expanded ? "block" : "none";

        if (expanded && childContainer.children.length === 0) {
          if (Array.isArray(item.children) && item.children.length > 0) {
            item.children.forEach((c) => this.renderFileTreeNode(childContainer, c, depth + 1));
          } else {
            try {
              const res = await api.getDirectoryTree(state.currentVersion, item.path, 1);
              const children = res.entries || res.tree || [];
              children.forEach((c) => this.renderFileTreeNode(childContainer, c, depth + 1));
            } catch (err) {
              console.warn("Failed to expand dir:", err);
            }
          }
        }
      };
    } else {
      row.onclick = () => {
        state.openTab({
          type: "code",
          title: item.name,
          path: item.path,
          version: state.currentVersion
        });
      };
      row.addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          e.stopPropagation();
          state.openTab({
            type: "code",
            title: item.name,
            path: item.path,
            version: state.currentVersion,
            forceNew: true
          });
        }
      });
    }
  }
}

// Bootstrap on DOM ready
document.addEventListener("DOMContentLoaded", () => {
  const app = new KernelIdeApp();
  app.init().catch((err) => {
    console.error("Critical failure during IDE initialization:", err);
  });
});
