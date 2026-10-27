/**
 * app.js - Main Application Bootstrap & Lifecycle Orchestrator.
 */
import { api } from "./api.js";
import { syncManager } from "./sync.js";
import { state } from "./state.js";
import { urlSync } from "./url_sync.js";
import { toast } from "./components/toast.js";
import { TabsComponent } from "./components/tabs.js";
import { contextMenu } from "./components/context_menu.js";
import { refPicker, shortRef, isCommitRef } from "./components/ref_picker.js";
import { requestVersionSwitch } from "./components/version_switch.js";
import { commandPalette } from "./components/command_palette.js";
import { showPatchModal } from "./components/patch_modal.js";
import { showSymbolHistoryModal } from "./components/symbol_history_modal.js";
import { localDB } from "./db.js";
import { escapeHtml } from "./components/modal.js";

// View Renderers
import { VirtualEditor } from "./views/code_view/virtual_editor.js";
import { KconfigView } from "./views/kconfig/kconfig_view.js";
import { NodeMapController } from "./views/nodemap/nodemap_controller.js";
import { MaintainersView } from "./views/maintainers/maintainers_view.js";
import { CommitsView } from "./views/commits/commits_view.js";
import { PaholeView } from "./views/pahole/pahole_view.js";
import { CallgraphView } from "./views/callgraph/callgraph_view.js";
import { DiffView } from "./views/diff/diff_view.js";
import { TreemapView } from "./views/treemap/treemap_view.js";

export class KernelIdeApp {
  constructor() {
    this.tabsComponent = null;
    this.viewRenderers = {
      // One view per tab (code and kconfig): each keeps its own file, dimming and engine state
      code: () => new VirtualEditor(),
      kconfig: () => new KconfigView(),
      nodemap: new NodeMapController(),
      maintainers: new MaintainersView(),
      commits: new CommitsView(),
      pahole: new PaholeView(),
      callgraph: new CallgraphView(),
      diff: new DiffView(),
      treemap: new TreemapView()
    };
  }

  async init() {
    console.info("Initializing KernelInfo Developer IDE...");

    // 1. Register Service Worker for offline PWA
    this.registerServiceWorker();

    // 2. Setup Online/Offline Status Indicator
    this.setupNetworkStatus();

    // 2a. Repository selected in a previous session scopes every API request
    api.setRepository(state.currentRepo);

    // 2b. Synchronize with the server's change tokens (database, parsed versions, Git refs, frontend build)
    try {
      await syncManager.start();
    } catch (e) {
      console.warn("Server sync check error:", e);
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

    // 7. Command palette (Ctrl+K / Ctrl+P search, Ctrl+Shift+P commands)
    commandPalette.setCommandProvider(() => this.paletteCommands());
    commandPalette.init();
    document.getElementById("global-search-btn")?.addEventListener("click", () => commandPalette.open(""));

    // Re-render tabs and sidebar when global version changes
    state.subscribe("version:changed", async () => {
      this.updateVersionSelectorDisplay();
      await this.loadSidebarFileTree();
      this.tabsComponent.render();
    });

    // A version finished parsing (or was rolled back), a branch moved or tags were fetched
    state.subscribe("versions:updated", async ({ changed, removed, newlyIndexed }) => {
      await this.initVersionSelector();
      if (newlyIndexed.length) {
        toast.success(`Now indexed: ${newlyIndexed.slice(0, 3).join(", ")}${newlyIndexed.length > 3 ? ` and ${newlyIndexed.length - 3} more` : ""}`);
      }
      const shown = new Set([state.currentVersion, ...state.panes.flatMap((p) => p.tabs.map((t) => t.version))]);
      if ([...changed, ...removed].some((v) => shown.has(v))) {
        await this.loadSidebarFileTree();
        this.tabsComponent.render();
      }
    });

    // Synchronize top selectors when a tab with a different version is selected
    state.subscribe("version:synced", async () => {
      this.updateVersionSelectorDisplay();
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

            <button class="settings-btn" id="btn-check-updates" ${isOnline ? "" : "disabled"} title="Check the frontend build, database, parsed versions and Git refs against the server">
              <span>🔄 Check for Updates</span>
              <span id="check-updates-status" style="font-size:10px;color:var(--text-muted);">${isOnline ? "&rarr;" : "Offline"}</span>
            </button>
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

    popover.querySelector("#btn-check-updates").onclick = (e) => this.checkForUpdates(e.currentTarget);

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

  /** Settings "Check for Updates": manual sync of the frontend build, database, parsed versions and Git refs. */
  async checkForUpdates(btn) {
    const status = btn.querySelector("#check-updates-status");
    btn.disabled = true;
    status.textContent = "Checking…";
    try {
      const r = await syncManager.checkNow();
      const parts = [];
      if (r.frontend) parts.push(`new frontend build ${r.newBuild}`);
      if (r.db) parts.push("database rebuilt (cache cleared)");
      if (r.changed.length) parts.push(`${r.changed.length} version(s) re-parsed or moved`);
      if (r.added.length && !r.db) parts.push(`${r.added.length} new version(s)/branch(es)`);
      if (r.removed.length) parts.push(`${r.removed.length} removed`);
      if (!parts.length && (r.parsed || r.git)) parts.push("version lists refreshed");
      if (parts.length) {
        status.textContent = "Updated";
        toast.success(`Updates found: ${parts.join(", ")}`);
      } else {
        status.textContent = "Up to date";
        toast.info("Everything is up to date (frontend, database, parsed versions, Git)");
      }
    } catch (err) {
      status.textContent = "Failed";
      toast.error("Update check failed: " + err.message);
    } finally {
      btn.disabled = false;
    }
  }

  async initVersionSelector() {
    const select = document.getElementById("version-select");
    const projSelect = document.getElementById("project-select");
    const branchSelect = document.getElementById("branch-select");

    // 1. Projects (configured repositories; selection is sent as X-KI-Repo with every request)
    if (projSelect) {
      try {
        const pRes = await api.getProjects();
        this.projects = pRes.projects || [];
        if (!state.currentRepo || !this.projects.some((p) => p.repo === state.currentRepo)) {
          state.currentRepo = pRes.active || this.projects[0]?.repo || "";
          api.setRepository(state.currentRepo);
          state.saveState();
        }
        projSelect.innerHTML = this.projects.map((p) => {
          const label = `${p.name}${p.indexed ? "" : " (git only)"}${p.available ? "" : " — missing"}`;
          return `<option value="${escapeAttr(p.repo)}" ${p.repo === state.currentRepo ? "selected" : ""} ${p.available ? "" : "disabled"}>${escapeAttr(label)}</option>`;
        }).join("");
        projSelect.title = "Repository (projects detected under workspace.repos_dir)";
        projSelect.onchange = () => this.switchProject(projSelect.value);
      } catch (err) {
        console.warn("Could not load projects:", err);
      }
    }

    // 2. Branches
    if (branchSelect) {
      try {
        const bRes = await api.getBranches();
        const branches = bRes.branches || [{ name: "master" }];
        branchSelect.innerHTML = branches.map(b => `<option value="${b.name}" ${b.is_default ? "selected" : ""}>${b.name}</option>`).join("");
        branchSelect.onchange = (e) => {
          const val = e.target.value;
          if (val) requestVersionSwitch(val);
        };
      } catch (err) {
        console.warn("Could not load branches:", err);
      }
    }

    // 3. Versions (release tags)
    if (!select) return;
    try {
      // Every tag including release candidates; releases fill the Tag selector, -rc# the RC selector
      const res = await api.getVersions(true);
      this.buildVersionCatalog(res.versions || ["v3.0"]);
      const branchNames = Array.from(branchSelect?.options || []).map((o) => o.value);

      // Validate and sanitize state.currentVersion (commit SHAs and branch names are valid targets too)
      const cur = state.currentVersion;
      if (!this.versionByName.has(cur) && !isCommitRef(cur) && !branchNames.includes(cur)) {
        const indexed = this.tagVersions.filter((t) => t.indexed && !t.rcOnly);
        state.currentVersion = (indexed[indexed.length - 1] || this.tagVersions[this.tagVersions.length - 1] || { vname: "v3.0" }).vname;
      }

      // Picking a release selects its final tag (or its latest -rc when it was never released)
      select.onchange = (e) => {
        const base = e.target.value;
        if (!base || base.includes("object")) return;
        const target = this.versionByName.has(base) ? base : (this.rcsByBase.get(base) || []).slice(-1)[0]?.vname;
        if (target) requestVersionSwitch(target);
      };

      const rcSelect = document.getElementById("rc-select");
      if (rcSelect) {
        rcSelect.onchange = (e) => {
          const val = e.target.value;
          if (val) requestVersionSwitch(val);
        };
      }
    } catch (e) {
      console.warn("Could not load versions list:", e);
    }

    // 4. Commit picker
    const commitBtn = document.getElementById("commit-picker-btn");
    if (commitBtn) {
      commitBtn.onclick = (e) => {
        e.stopPropagation();
        refPicker.open(commitBtn, {
          current: state.currentVersion,
          onSelect: (ref) => {
            if (ref) requestVersionSwitch(ref);
          }
        });
      };
    }

    this.updateVersionSelectorDisplay();
  }

  /** Reflect state.currentVersion in the Tag dropdown, Branch dropdown and Commit picker button. */
  /**
   * Split the version catalog into releases (Tag selector) and pre-releases grouped under the release
   * they lead to (RC selector): v3.1-rc1..rc10 belong to v3.1. A release that only has candidates so
   * far (e.g. v7.3-rc1 before v7.3 is tagged) gets an "rc only" entry in the Tag selector.
   */
  buildVersionCatalog(rawVersions) {
    const entries = rawVersions
      .map((v) => (typeof v === "string"
        ? { vname: v, commit_hash: "", indexed: true, ref_type: 1, is_prerelease: /-(rc|pre|alpha|beta)\d*$/.test(v) }
        : { vname: v.vname || v.name, commit_hash: v.commit_hash || "", indexed: v.indexed !== false, ref_type: v.ref_type ?? 1, is_prerelease: Boolean(v.is_prerelease) }))
      .filter((v) => v.vname);
    this.versionByName = new Map(entries.map((v) => [v.vname, v]));
    this.rcsByBase = new Map();
    const releases = [];
    const seenBases = new Set();
    for (const v of entries) {
      if (v.ref_type === 2 || v.ref_type === 3) continue; // branches and bare commits are not releases
      const base = this.releaseOf(v.vname);
      if (base !== v.vname) {
        if (!this.rcsByBase.has(base)) this.rcsByBase.set(base, []);
        this.rcsByBase.get(base).push(v);
        if (!this.versionByName.has(base) && !seenBases.has(base)) {
          seenBases.add(base);
          releases.push({ vname: base, commit_hash: "", indexed: v.indexed, rcOnly: true });
        }
      } else if (!seenBases.has(v.vname)) {
        seenBases.add(v.vname);
        releases.push(v);
      }
    }
    // A release counts as parsed when its final tag, or any of its candidates, was parsed
    for (const r of releases) {
      if (r.rcOnly) r.indexed = (this.rcsByBase.get(r.vname) || []).some((rc) => rc.indexed);
    }
    this.tagVersions = releases;
  }

  /** Release a tag belongs to: "v3.1-rc4" → "v3.1"; releases map to themselves. */
  releaseOf(vname) {
    const m = /^(.*?)-(rc|pre|alpha|beta)\d*$/.exec(vname || "");
    return m ? m[1] : vname;
  }

  updateVersionSelectorDisplay() {
    const cur = state.currentVersion;
    const tags = this.tagVersions || [];
    const tag = this.versionByName?.get(cur);
    const base = tag ? this.releaseOf(cur) : null;

    const select = document.getElementById("version-select");
    if (select) {
      select.innerHTML = "";
      if (!tag) {
        // Detached target (commit SHA or branch): keep a visible placeholder so the dropdown never lies
        const opt = document.createElement("option");
        opt.value = cur;
        opt.textContent = "— none —";
        opt.selected = true;
        select.appendChild(opt);
      }
      // Parsed versions first; tags only present in Git stay browsable (tree, files, diffs)
      const groups = [
        { label: "Parsed", items: tags.filter((t) => t.indexed) },
        { label: "Git only (not parsed)", items: tags.filter((t) => !t.indexed) },
      ].filter((g) => g.items.length);
      groups.forEach((g) => {
        const parent = groups.length > 1 ? document.createElement("optgroup") : select;
        if (parent !== select) parent.label = g.label;
        g.items.forEach((t) => {
          const opt = document.createElement("option");
          opt.value = t.vname;
          opt.textContent = t.rcOnly ? `${t.vname} (rc only)` : t.vname;
          if (t.vname === base) opt.selected = true;
          parent.appendChild(opt);
        });
        if (parent !== select) select.appendChild(parent);
      });
    }

    // RC selector: the selected release's candidates, plus its final tag when it exists
    const rcSelect = document.getElementById("rc-select");
    const rcLabel = document.getElementById("rc-select-label");
    const rcs = base ? this.rcsByBase?.get(base) || [] : [];
    if (rcSelect) {
      const show = rcs.length > 0;
      rcSelect.hidden = !show;
      if (rcLabel) rcLabel.hidden = !show;
      rcSelect.innerHTML = "";
      if (show) {
        const finalTag = this.versionByName.get(base);
        const options = [
          ...(finalTag ? [{ value: base, text: "final", indexed: finalTag.indexed }] : []),
          ...rcs.slice().reverse().map((rc) => ({ value: rc.vname, text: rc.vname.slice(base.length + 1), indexed: rc.indexed })),
        ];
        for (const o of options) {
          const opt = document.createElement("option");
          opt.value = o.value;
          opt.textContent = o.indexed ? o.text : `${o.text} (git only)`;
          if (o.value === cur) opt.selected = true;
          rcSelect.appendChild(opt);
        }
        rcSelect.title = `${rcs.length} release candidate${rcs.length === 1 ? "" : "s"} of ${base}`;
      }
    }

    const branchSelect = document.getElementById("branch-select");
    if (branchSelect && Array.from(branchSelect.options).some((o) => o.value === cur)) {
      branchSelect.value = cur;
    }

    const label = document.getElementById("commit-picker-label");
    const commitBtn = document.getElementById("commit-picker-btn");
    if (label && commitBtn) {
      if (tag) {
        label.textContent = tag.commit_hash ? shortRef(tag.commit_hash) : "tag head";
        commitBtn.classList.remove("detached");
        commitBtn.title = `Tag ${tag.vname}${tag.commit_hash ? ` → ${tag.commit_hash}` : ""}\nClick to select a specific commit`;
      } else {
        label.textContent = shortRef(cur);
        commitBtn.classList.add("detached");
        commitBtn.title = `Exploring ${cur}\nClick to select another commit`;
      }
    }
  }

  /** Switch the active repository: resets the workspace and reloads the version catalog. */
  async switchProject(repo) {
    const projSelect = document.getElementById("project-select");
    if (!repo || repo === state.currentRepo) return;
    const openTabs = state.panes.reduce((n, p) => n + p.tabs.length, 0);
    if (openTabs > 0 && !confirm(`Switch project to '${repo}'? Open tabs (${openTabs}) belong to the current repository and will be closed.`)) {
      if (projSelect) projSelect.value = state.currentRepo;
      return;
    }
    api.setRepository(repo);
    let defaultVersion = "";
    try {
      const res = await api.getVersions(true);
      const versions = (res.versions || []).filter((v) => !v.is_prerelease);
      const indexed = versions.filter((v) => v.indexed !== false && v.ref_type !== 3);
      defaultVersion = (indexed[indexed.length - 1] || versions[versions.length - 1] || {}).vname || "";
    } catch (err) {
      console.warn("Could not load versions for project:", err);
    }
    if (!defaultVersion) {
      const project = (this.projects || []).find((p) => p.repo === repo);
      defaultVersion = project?.default_branch || "HEAD";
    }
    state.setRepository(repo, defaultVersion);
    await this.initVersionSelector();
    toast.success(`Switched to ${repo} @ ${shortRef(defaultVersion)}`);
  }

  /** Commands listed in the palette (">" prefix). */
  paletteCommands() {
    const v = () => state.getActiveTab()?.version || state.currentVersion;
    const activeCode = () => {
      const tab = state.getActiveTab();
      return tab && tab.type === "code" && tab.path ? tab : null;
    };
    const commands = [
      { title: "Compare versions or commits", hint: "Files & Kconfig changes", keywords: "diff compare", run: () => state.openTab({ type: "diff", title: "Compare" }) },
      { title: "Select commit…", hint: "Tags and commits", keywords: "version checkout ref", run: () => document.getElementById("commit-picker-btn")?.click() },
      { title: "Open source treemap", hint: "Bytes / files per directory", keywords: "size disk usage", run: () => state.openTab({ type: "treemap", title: "Treemap: root", path: "", version: v() }) },
      {
        title: "Export compile_commands.json", hint: "clang compilation database (x86)", keywords: "clangd lsp download",
        run: () => {
          window.location.href = api.getCompileCommandsDownloadUrl(v(), "x86");
          toast.info("Generating compile_commands.json…");
        }
      },
      { title: "Open KConfig engine", hint: "Menuconfig, defconfigs, dependencies", keywords: "config", run: () => state.openTab({ type: "kconfig", title: "KConfig (x86_64)", arch: "x86_64" }) },
      {
        title: `${state.kconfigEngineActive ? "Disable" : "Enable"} KConfig engine (#ifdef dimming)`, hint: "Dims compiled-out preprocessor branches", keywords: "ifdef dim preprocessor",
        run: () => {
          if (state.kconfigEngineActive) {
            const owner = state.kconfigEngineOwnerTab();
            state.setKconfigEngineOwner(null);
            toast.info(`KConfig engine disabled${owner ? ` in ${owner.title}` : ""} (its configuration is kept)`);
            return;
          }
          // The engine resolves the configuration of one KConfig tab, so it needs one to belong to
          const active = state.getActiveTab();
          const target = active && active.type === "kconfig" ? active : state.kconfigTabs()[0];
          if (!target) {
            toast.info("Open a KConfig tab to enable the engine");
            return;
          }
          // The tab shows what resolving its values changes before it takes the engine (§5.0)
          state.requestKconfigEngine(target.id);
        }
      },
      { title: "Open Git commits timeline", hint: "History of the version", keywords: "log", run: () => state.openTab({ type: "commits", title: "Commits" }) },
      { title: "Open subsystems & maintainers", hint: "MAINTAINERS, CREDITS", keywords: "people", run: () => state.openTab({ type: "maintainers", title: "Maintainers" }) },
      { title: "Open NodeMap canvas", hint: "Schematic view", keywords: "graph", run: () => state.openTab({ type: "nodemap", title: "NodeMap Canvas" }) },
      { title: "Open kernel documentation", hint: "Documentation/index.rst", keywords: "docs rst", run: () => state.openTab({ type: "code", title: "index.rst", path: "Documentation/index.rst", version: v() }) },
      { title: "Toggle file explorer", hint: "Sidebar", keywords: "sidebar tree", run: () => document.querySelector('.activity-btn[data-action="explorer"]')?.click() },
    ];
    const tab = activeCode();
    if (tab) {
      commands.unshift(
        { title: `Create patch from ${tab.path.split("/").pop()}…`, hint: "Edit & git format-patch", keywords: "patch edit", run: () => showPatchModal(tab.version || v(), tab.path) },
        { title: "Symbol history at cursor line", hint: `${tab.path}:${tab.cursorLine || 1}`, keywords: "timeline versions", run: () => showSymbolHistoryModal(tab.version || v(), tab.path, tab.cursorLine || 1) },
      );
    }
    if ((this.projects || []).length > 1) {
      for (const p of this.projects) {
        if (p.repo !== state.currentRepo && p.available) {
          commands.push({ title: `Switch project: ${p.name}`, hint: p.path, keywords: "repository project", run: () => this.switchProject(p.repo) });
        }
      }
    }
    return commands;
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
          state.openTab({ type: "kconfig", title: "KConfig (x86_64)", arch: "x86_64" });
        } else if (action === "nodemap") {
          state.openTab({ type: "nodemap", title: "NodeMap Canvas" });
        } else if (action === "maintainers") {
          state.openTab({ type: "maintainers", title: "Maintainers" });
        } else if (action === "commits") {
          state.openTab({ type: "commits", title: "Commits" });
        } else if (action === "compare" || action === "tools") {
          state.openTab({ type: "diff", title: "Compare" });
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

    const headerLabel = document.querySelector("#sidebar .sidebar-header span");
    if (headerLabel) headerLabel.textContent = `Repository Explorer · ${shortRef(state.currentVersion)}`;

    treeContainer.innerHTML = `<div style="padding:12px;color:var(--text-muted);">Loading files...</div>`;

    try {
      const res = await api.getDirectoryTree(state.currentVersion, "", 2);
      const items = res.entries || res.tree || [];
      treeContainer.innerHTML = "";

      items.forEach((item) => {
        this.renderFileTreeNode(treeContainer, item, 0);
      });
    } catch (e) {
      treeContainer.innerHTML = `<div style="padding:12px;color:var(--accent-red);">Failed: ${escapeHtml(e.message)}</div>`;
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

function escapeAttr(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// Bootstrap on DOM ready (boot.js imports this module, possibly after DOMContentLoaded fired)
function startApp() {
  const app = new KernelIdeApp();
  app.init().catch((err) => {
    console.error("Critical failure during IDE initialization:", err);
  });
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", startApp);
} else {
  startApp();
}
