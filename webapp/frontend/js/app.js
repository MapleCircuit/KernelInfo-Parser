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
import { requestVersionSwitch, openVersionInNewTab } from "./components/version_switch.js";
import { commandPalette } from "./components/command_palette.js";
import { shortcuts } from "./utils/shortcuts.js";
import { showShortcutsOverlay } from "./components/shortcuts_overlay.js";
import { showPatchModal } from "./components/patch_modal.js";
import { showSymbolHistoryModal } from "./components/symbol_history_modal.js";
import { localDB } from "./db.js";
import { escapeHtml } from "./components/modal.js";
import { sessionManager } from "./session_manager.js";
import { showSessionPickerModal } from "./components/session_modal.js";

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
import { ExplorerView } from "./views/explorer/explorer_view.js";
import { MailView } from "./views/mail/mail_view.js";
import { SearchView } from "./views/search/search_view.js";
import { StatusView } from "./views/status/status_view.js";
import { PatchSeriesView } from "./views/patch/patch_series_view.js";
import { BookmarksView } from "./views/bookmarks/bookmarks_view.js";
import { WatchView } from "./views/watch/watch_view.js";
import { initWatchRuntime } from "./views/watch/watch_runtime.js";
import { KernelApiView, apiSections } from "./views/kernel_api/kernel_api_view.js";
import { InsightsView, insightsSections } from "./views/insights/insights_view.js";
import { ManView } from "./views/man/man_view.js";
import { newestTag } from "./views/man/man_model.js";
import { KERNEL_FAMILY, familyOf, prefixedFinals, prefixedReleaseOf } from "./views/sections/release_order.js";
import { PROJECT_MOVABLE_TABS, isManPagesRepo, manPagesRepo, setProjects } from "./utils/projects.js";
import "./views/sections/all_sections.js";
import { statusLine } from "./views/status/status_model.js";
import { STICKY_KEY, stickyEnabled } from "./views/code_view/outline_panel.js";
import "./routes.js";
import { exportDocument, importRecords, mergeImport } from "./utils/user_records.js";
import { THEMES, applyTheme, savedTheme } from "./utils/theme.js";

/** Settings → Export User Data (S06): every user record as a `kip-user` JSON document. */
async function exportUserData() {
  const records = await localDB.userList();
  const blob = new Blob([JSON.stringify(exportDocument(records), null, 1)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `kernelinfo-user-data-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast.success(`Exported ${records.length} user record${records.length === 1 ? "" : "s"}`);
}

/** Settings → Import User Data: adds the records of an export; records already here are kept. */
function importUserData() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/json,.json";
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      const incoming = importRecords(JSON.parse(await file.text()));
      const existing = new Map((await localDB.userList()).map((r) => [r.key, r]));
      const { put, skipped } = mergeImport(existing, incoming, "skip-existing");
      for (const r of put) await localDB.userPut(r);
      toast.success(`Imported ${put.length} user record${put.length === 1 ? "" : "s"}${skipped ? ` (${skipped} already here, kept)` : ""}`);
    } catch (err) {
      toast.error(`Import failed: ${err.message}`);
    }
  };
  input.click();
}

/** Per-browser UI preference: whether the left explorer sidebar is collapsed. */
const SIDEBAR_COLLAPSED_KEY = "kernel_ide_sidebar_collapsed";

class KernelIdeApp {
  constructor() {
    this.tabsComponent = null;
    this.viewRenderers = {
      // One view per tab (code, kconfig, explorer, mail, diff, commits): each keeps its own file, dimming,
      // engine selection, browsed directory, mail search/thread, comparison (chips, filters, rename
      // searches) or commit search and selection
      code: () => new VirtualEditor(),
      kconfig: () => new KconfigView(),
      explorer: () => new ExplorerView(),
      mail: () => new MailView(),
      nodemap: () => new NodeMapController(),
      maintainers: () => new MaintainersView(),
      commits: () => new CommitsView(),
      pahole: () => new PaholeView(),
      callgraph: () => new CallgraphView(),
      diff: () => new DiffView(),
      treemap: () => new TreemapView(),
      search: () => new SearchView(),
      status: () => new StatusView(),
      patch: () => new PatchSeriesView(),
      bookmarks: () => new BookmarksView(),
      watch: () => new WatchView(),
      api: () => new KernelApiView(),
      insights: () => new InsightsView(),
      man: () => new ManView()
    };
  }

  async init() {
    console.info("Initializing KernelInfo Developer IDE...");

    // 0. Apply persistent font scaling and typography preferences
    applyTypographySettings();

    // 1. Register Service Worker for offline PWA
    this.registerServiceWorker();

    // 2. Setup Online/Offline Status Indicator
    this.setupNetworkStatus();

    // 2b. Synchronize with the server's change tokens (database, parsed versions, Git refs, frontend build)
    try {
      await syncManager.start();
    } catch (e) {
      console.warn("Server sync check error:", e);
    }
    // Restarted by Settings "Check for Updates": say what it found (syncManager.restart())
    const restartNote = syncManager.takeRestartNote();
    if (restartNote !== null) toast.success(restartNote ? `Updates applied: ${restartNote}` : "Updates applied");

    // 3. Initialize Version Selector
    await this.initVersionSelector();

    // 4. Mount Multi-Tab Split-Pane Workspace
    const workspaceEl = document.getElementById("workspace");
    this.tabsComponent = new TabsComponent(workspaceEl, this.viewRenderers);
    // Watch list (G10): bell count and checks when a watched branch moves
    initWatchRuntime();
    this.tabsComponent.render();

    // 4a. Initialize Multi-Window Workspace Sessions
    this.setupSessionControls();
    if (!state.currentSessionId) {
      // The hash is the address this window was opened to show, but adopting a session replaces
      // the panes with that session's tabs. So the address is held and applied once a session is
      // chosen, and the tab a link asked for opens inside it (§5.26).
      urlSync.defer();
      showSessionPickerModal({ allowClose: false, onSelect: () => { if (urlSync.deferred) urlSync.init(); } });
    }

    // 5. Setup Activity Bar & Sidebar
    this.setupActivityBar();
    this.setupSidebarCollapse();
    await this.loadSidebarFileTree();

    // 6. Initialize URL Synchronization (held above while the startup session picker is open).
    // An address naming another project (`repo=`, §5.26) opens its tab in that project, next to the
    // open tabs; a repository not configured here opens in the active tab's project instead.
    urlSync.setRepoHandler((repo) => {
      const known = this.projects || [];
      const project = known.find((p) => p.repo === repo);
      if (known.length && (!project || project.available === false)) {
        toast.warning(`The link names project '${repo}', which is not configured here: opened in ${state.activeRepo() || "the current project"}`);
        return { repo: "", version: "" };
      }
      return { repo, version: project?.default_branch || "" };
    });
    if (!urlSync.deferred) urlSync.init();

    // 7. Command palette (Ctrl+K / Ctrl+P search, Ctrl+Shift+P commands)
    commandPalette.setCommandProvider(() => this.paletteCommands());
    commandPalette.init();

    // Keyboard shortcuts (§5.31): one capture listener; the scope is the open dialog or the active tab's type
    shortcuts.setContextProvider(() => {
      const tab = state.getActiveTab();
      return {
        tab,
        tabType: tab?.type || null,
        view: tab ? this.tabsComponent?.perTabViews?.get(tab.id) || null : null,
        modal: Boolean(document.querySelector(".ki-modal-backdrop")),
      };
    });
    shortcuts.install(document);
    document.getElementById("global-search-btn")?.addEventListener("click", () => commandPalette.open(""));

    // Re-render tabs and sidebar when global version changes
    state.subscribe("version:changed", async () => {
      this.updateVersionSelectorDisplay();
      await this.loadSidebarFileTree();
      this.tabsComponent.render();
    });

    // A repository was added, removed, parsed for the first time or went missing (sync `projects` token)
    state.subscribe("projects:updated", () => this.initVersionSelector());

    // The active tab belongs to another project: the header lists that project's branches and revisions
    state.subscribe("repo:synced", async () => {
      await this.initVersionSelector();
      await this.loadSidebarFileTree();
    });

    // A version finished parsing (or was rolled back), a branch moved or tags were fetched
    // (of one repository: `repo`; the header lists the active tab's project)
    state.subscribe("versions:updated", async ({ repo = state.currentRepo, changed, removed, newlyIndexed }) => {
      const headerRepo = repo === state.currentRepo;
      if (headerRepo) await this.initVersionSelector();
      if (newlyIndexed.length) {
        toast.success(`Now indexed${headerRepo ? "" : ` in ${repo}`}: ${newlyIndexed.slice(0, 3).join(", ")}${newlyIndexed.length > 3 ? ` and ${newlyIndexed.length - 3} more` : ""}`);
      }
      const shown = new Set([...(headerRepo ? [state.currentVersion] : []),
        ...state.panes.flatMap((p) => p.tabs.filter((t) => state.repoOf(t) === repo).map((t) => t.version))]);
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

    // Picking a revision in the header only moves the selection (Apply / New Tab act on it)
    state.subscribe("version:selected", () => this.updateVersionSelectorDisplay());

    // The active tab decides what Apply would switch, so the buttons follow tab changes
    state.subscribe("tabs:changed", () => this.updateVersionSelectorDisplay());

    console.info("KernelInfo Developer IDE fully initialized.");
  }

  registerServiceWorker() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.getRegistration().then((reg) => {
        if (!reg || reg.updateViaCache !== "all") {
          navigator.serviceWorker.register("/sw.js", { updateViaCache: "all" }).then((r) => {
            console.info("PWA ServiceWorker registered with scope:", r.scope);
          }).catch((err) => {
            console.warn("ServiceWorker registration failed:", err);
          });
        }
      }).catch((err) => {
        console.warn("ServiceWorker registration check failed:", err);
      });
    }
  }

  setupNetworkStatus() {
    const badge = document.getElementById("offline-badge");
    if (!badge) return;

    badge.style.cursor = "pointer";
    badge.title = "Click to open Network & Local Storage Settings";

    // Three states: the link is down, the link is up but the backend does not answer (api.serverUp,
    // fed by every request and by the heartbeat), or both are fine.
    let shown = null;
    const updateBadge = () => {
      const status = !navigator.onLine ? "offline" : api.serverUp ? "online" : "unreachable";
      if (status === shown) return;
      const first = shown === null;
      shown = status;
      const setBadgeContent = (text) => {
        let textSpan = badge.querySelector(".badge-text");
        let dotSpan = badge.querySelector(".badge-dot");
        if (!textSpan || !dotSpan) {
          badge.innerHTML = `<span class="badge-dot">●</span><span class="badge-text">${escapeHtml(text)}</span>`;
        } else {
          textSpan.textContent = text;
        }
      };

      if (status === "online") {
        setBadgeContent("Online");
        badge.className = "offline-badge";
        badge.title = "Online: Connected to backend server. Click to open Network & Local Storage Settings";
        if (!first) toast.success("Server reachable again");
      } else if (status === "unreachable") {
        setBadgeContent("Server Unreachable");
        badge.className = "offline-badge unreachable";
        badge.title = "Server Unreachable: Viewing cached data. Click to open Network & Local Storage Settings";
        toast.warning("Server not responding: viewing cached repository data");
      } else {
        setBadgeContent("Offline (Cached)");
        badge.className = "offline-badge offline";
        badge.title = "Offline: Viewing cached data. Click to open Network & Local Storage Settings";
        toast.info("Offline mode active: viewing cached repository data");
      }
      // Keep an open settings popover in step with the state it reports
      const openPopover = document.getElementById("settings-popover-menu");
      if (openPopover) {
        if (openPopover._closePopover) openPopover._closePopover();
        else openPopover.remove();
        this.toggleSettingsPopover(badge);
      }
    };

    window.addEventListener("online", updateBadge);
    window.addEventListener("offline", updateBadge);
    document.addEventListener("app:server-status", updateBadge);
    updateBadge();
    api.startHeartbeat();

    badge.addEventListener("click", (e) => {
      e.stopPropagation();
      this.toggleSettingsPopover(badge);
    });
  }

  setupSessionControls() {
    const btn = document.getElementById("session-picker-btn");
    const container = document.getElementById("session-selector-container");
    const labelEl = document.getElementById("session-picker-label");

    const updateLabel = () => {
      if (labelEl) {
        labelEl.textContent = state.currentSessionName || "Session";
      }
      if (btn) {
        btn.title = `Workspace Session: ${state.currentSessionName || "Session"} (${state.currentSessionId || "none"})`;
      }
    };
    updateLabel();

    state.subscribe("session:adopted", async () => {
      updateLabel();
      // The adopted session's active tab may belong to another project: the header lists its revisions
      if (this.headerRepo !== undefined && this.headerRepo !== state.currentRepo) await this.initVersionSelector();
      if (typeof this.loadBranchScope === "function") {
        await this.loadBranchScope(state.selectedBranch);
      }
      if (typeof this.loadSidebarFileTree === "function") {
        await this.loadSidebarFileTree();
      }
      this.tabsComponent?.render();
      this.updateVersionSelectorDisplay();
    });

    sessionManager.subscribe("session:renamed", ({ id, name }) => {
      if (id === state.currentSessionId) {
        state.currentSessionName = name;
        updateLabel();
      }
    });

    state.subscribe("session:taken_over", () => {
      updateLabel();
      toast.info("This session was opened in another window; it continues there. Choose a session to carry on here.");
      showSessionPickerModal({ allowClose: false });
    });

    state.subscribe("session:closed", (closedSid) => {
      if (closedSid === state.currentSessionId || !state.currentSessionId) {
        updateLabel();
        const index = sessionManager.getSessionsIndex();
        if (index.length === 1 && !sessionManager.isSessionActiveInOtherWindow(index[0].id)) {
          const remaining = sessionManager.loadSession(index[0].id);
          if (remaining) {
            state.adoptSession(remaining);
            toast.info(`Switched to "${remaining.name || "Session"}"`);
            return;
          }
        }
        showSessionPickerModal({ allowClose: false });
      }
    });

    if (!btn || !container) return;

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const existingMenu = container.querySelector(".session-dropdown-menu");
      if (existingMenu) {
        if (typeof existingMenu._closeMenu === "function") existingMenu._closeMenu();
        else existingMenu.remove();
        return;
      }

      const menu = document.createElement("div");
      menu.className = "session-dropdown-menu";
      menu.innerHTML = `
        <div class="session-dropdown-header">
          <div style="font-weight: 600; color: var(--text-primary); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
            ${escapeHtml(state.currentSessionName || "Current Session")}
          </div>
          <div style="font-size: 10px; color: var(--text-muted); font-family: var(--font-mono); user-select: all; word-break: break-all;" title="Session ID: ${escapeHtml(state.currentSessionId || '')}">
            ID: <span style="color: var(--accent-blue);">${escapeHtml(state.currentSessionId || "none")}</span>
          </div>
        </div>
        <div class="session-menu-divider"></div>
        <button type="button" class="session-menu-item" id="session-action-switch">
          <span>🗂</span> Switch Session…
        </button>
        <button type="button" class="session-menu-item" id="session-action-new">
          <span>➕</span> New Session…
        </button>
        <button type="button" class="session-menu-item" id="session-action-duplicate">
          <span>⧉</span> Duplicate Session…
        </button>
        <button type="button" class="session-menu-item" id="session-action-rename">
          <span>✏️</span> Rename Session…
        </button>
        <div class="session-menu-divider"></div>
        <button type="button" class="session-menu-item danger" id="session-action-close">
          <span>🗑</span> Close Session
        </button>
      `;

      container.appendChild(menu);

      const closeMenu = () => {
        menu.remove();
        document.removeEventListener("click", outsideClick);
      };
      menu._closeMenu = closeMenu;

      const outsideClick = (ev) => {
        if (!menu.contains(ev.target) && ev.target !== btn) {
          closeMenu();
        }
      };
      setTimeout(() => document.addEventListener("click", outsideClick), 10);

      menu.querySelector("#session-action-switch")?.addEventListener("click", () => {
        closeMenu();
        showSessionPickerModal({ allowClose: true });
      });

      menu.querySelector("#session-action-new")?.addEventListener("click", () => {
        closeMenu();
        const count = sessionManager.getSessionsIndex().length;
        const name = prompt("Enter a name for the new session:", "Session " + (count + 1));
        if (name === null) return;
        const newSession = sessionManager.createSession({
          name: name.trim() || ("Session " + (count + 1)),
          version: state.currentVersion || "v3.0",
          repo: state.currentRepo || "",
          pin: false
        });
        state.adoptSession(newSession);
        toast.success("Created session '" + newSession.name + "'");
      });

      menu.querySelector("#session-action-duplicate")?.addEventListener("click", () => {
        closeMenu();
        if (!state.currentSessionId) return;
        const dup = sessionManager.duplicateSession(state.currentSessionId);
        if (dup) {
          state.adoptSession(dup);
          toast.success("Duplicated into '" + dup.name + "'");
        }
      });

      menu.querySelector("#session-action-rename")?.addEventListener("click", () => {
        closeMenu();
        const currentName = state.currentSessionName || "Session";
        const newName = prompt("Rename session:", currentName);
        if (!newName || newName.trim() === currentName) return;
        sessionManager.renameSession(state.currentSessionId, newName.trim());
        state.currentSessionName = newName.trim();
        updateLabel();
        toast.success("Renamed session to '" + newName.trim() + "'");
      });

      menu.querySelector("#session-action-close")?.addEventListener("click", () => {
        closeMenu();
        const name = state.currentSessionName || "this session";
        if (confirm("Are you sure you want to close '" + name + "'? The session will be deleted from your saved workspaces.")) {
          const sid = state.currentSessionId;
          sessionManager.deleteSession(sid); // unpins this window from it
          state.currentSessionId = null;
          state.currentSessionName = "";
          updateLabel();
          const index = sessionManager.getSessionsIndex();
          if (index.length === 1 && !sessionManager.isSessionActiveInOtherWindow(index[0].id)) {
            const remaining = sessionManager.loadSession(index[0].id);
            if (remaining) {
              state.adoptSession(remaining);
              toast.info(`Switched to "${remaining.name || "Session"}"`);
              return;
            }
          }
          showSessionPickerModal({ allowClose: false });
        }
      });
    });
  }

  async toggleSettingsPopover(anchorEl) {
    const existing = document.getElementById("settings-popover-menu");
    if (existing) {
      if (typeof existing._closePopover === "function") existing._closePopover();
      else existing.remove();
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
    // User data (S06): never pruned, never part of the cache clears
    const userCount = storeCounts.user || 0;

    const tabsCount = Array.isArray(state.tabs)
      ? state.tabs.length
      : Array.isArray(state.openTabs)
        ? state.openTabs.length
        : (state.panes?.reduce((sum, p) => sum + (p.tabs?.length || 0), 0) || 0);
    // Same three states as the header badge: link down, backend not answering, or connected.
    const isOnline = api.isConnected;
    const linkDown = !api.isOnline;
    const connLabel = isOnline ? "Online (Connected)" : linkDown ? "Offline (Cached)" : "Server Unreachable";
    const connDetail = isOnline ? "REST API Active" : linkDown ? "Fallback DB" : "No response from backend";
    const connColor = isOnline ? "var(--accent-green)" : linkDown ? "var(--accent-yellow)" : "var(--accent-red)";

    // Database instance hash and storage estimate
    const cachedHash = (await localDB.get("meta", "db_instance_hash")) || "Default";
    const shortHash = cachedHash.length > 12 ? cachedHash.substring(0, 12) : cachedHash;
    const storageEst = await localDB.getStorageEstimate();
    const usedMB = storageEst?.usage ? (storageEst.usage / (1024 * 1024)).toFixed(1) : null;

    const curCodeFontSize = parseInt(localStorage.getItem("kip-code-font-size") || "12", 10);
    const curUiFontScale = parseInt(localStorage.getItem("kip-ui-font-scale") || "100", 10);

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
                <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${connColor};"></span>
                <span style="font-weight:600;color:var(--text-primary);font-size:11px;">${connLabel}</span>
              </div>
              <span style="font-size:10px;color:var(--text-muted);font-family:var(--font-mono);">${connDetail}</span>
            </div>

            <div style="display:flex;align-items:center;justify-content:space-between;background:var(--bg-primary);padding:6px 10px;border-radius:var(--radius-md);border:1px solid var(--border-color);" title="Database Instance: ${cachedHash}">
              <div style="display:flex;align-items:center;gap:6px;">
                <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent-blue);"></span>
                <span style="font-size:11px;color:var(--text-secondary);">DB Instance:</span>
                <code style="font-family:var(--font-mono);font-size:11px;color:var(--accent-blue);">${shortHash}</code>
              </div>
              <span style="font-size:10px;color:var(--accent-green);font-weight:500;">● Synced</span>
            </div>

            <button class="settings-btn" id="btn-parser-status" title="Open the parser status page (G11)">
              <span id="settings-parser-line" style="font-size:11px;">⛏ Parser: …</span>
              <span style="font-size:10px;color:var(--text-muted);">status page &rarr;</span>
            </button>

            <button class="settings-btn" id="btn-keyboard-shortcuts" title="List every keyboard shortcut and rebind them">
              <span>⌨ Keyboard Shortcuts</span>
              <span style="font-size:10px;color:var(--text-muted);">${escapeHtml(shortcuts.label("help.shortcuts") || "?")}</span>
            </button>

            <button class="settings-btn" id="btn-check-updates" ${isOnline ? "" : "disabled"} title="Sync with the server now (frontend build, database, parsed versions, Git refs, repositories) and restart the page when an update is found">
              <span>🔄 Check for Updates</span>
              <span id="check-updates-status" style="font-size:10px;color:var(--text-muted);">${isOnline ? "&rarr;" : linkDown ? "Offline" : "Unreachable"}</span>
            </button>
          </div>
        </div>

        <!-- Theme (G08) -->
        <div>
          <div class="settings-section-title">Theme</div>
          <select id="setting-theme" style="width:100%;background:var(--bg-primary);color:var(--text-primary);border:1px solid var(--border-color);border-radius:var(--radius-sm);padding:4px 6px;">
            ${THEMES.map(([id, label]) => `<option value="${id}" ${savedTheme() === id ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </div>

        <!-- Hover cards (G04) -->
        <div>
          <div class="settings-section-title">Hover Cards</div>
          <div style="display:flex;flex-direction:column;gap:8px;background:var(--bg-primary);padding:10px 12px;border-radius:var(--radius-md);border:1px solid var(--border-color);">
            <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text-secondary);cursor:pointer;">
              <input type="checkbox" id="setting-hover-enabled" ${localStorage.getItem("kip-hover-cards") === "0" ? "" : "checked"} /> Show a card when the pointer rests on a symbol
            </label>
            <div>
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                <label for="setting-hover-delay" style="font-size:11px;font-weight:600;color:var(--text-secondary);">Delay</label>
                <span id="setting-hover-delay-val" style="font-size:11px;font-family:var(--font-mono);color:var(--accent-blue);">${parseInt(localStorage.getItem("kip-hover-delay") || "450", 10)} ms</span>
              </div>
              <input type="range" id="setting-hover-delay" min="300" max="1000" step="50" value="${parseInt(localStorage.getItem("kip-hover-delay") || "450", 10)}" style="width:100%;cursor:pointer;" />
            </div>
          </div>
        </div>

        <!-- 2. Typography & Font Sizes -->
        <div>
          <div class="settings-section-title">Typography &amp; Font Sizes</div>
          <div style="display:flex;flex-direction:column;gap:10px;background:var(--bg-primary);padding:10px 12px;border-radius:var(--radius-md);border:1px solid var(--border-color);">
            <!-- Code View Font Size -->
            <div>
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                <label for="setting-code-font-slider" style="font-size:11px;font-weight:600;color:var(--text-secondary);">Code View Font Size</label>
                <span id="setting-code-font-val" style="font-size:11px;font-family:var(--font-mono);color:var(--accent-blue);font-weight:600;">${curCodeFontSize}px</span>
              </div>
              <input type="range" id="setting-code-font-slider" min="10" max="26" step="1" value="${curCodeFontSize}" style="width:100%;cursor:pointer;" />
              <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--text-muted);margin-top:2px;">
                <span>10px</span>
                <span>Default: 12px</span>
                <span>26px</span>
              </div>
            </div>

            <!-- UI Relative Scale -->
            <div>
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                <label for="setting-ui-font-slider" style="font-size:11px;font-weight:600;color:var(--text-secondary);">UI Scale</label>
                <span id="setting-ui-font-val" style="font-size:11px;font-family:var(--font-mono);color:var(--accent-blue);font-weight:600;">${curUiFontScale}%</span>
              </div>
              <input type="range" id="setting-ui-font-slider" min="80" max="160" step="5" value="${curUiFontScale}" style="width:100%;cursor:pointer;" />
              <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--text-muted);margin-top:2px;">
                <span>80%</span>
                <span>Default: 100%</span>
                <span>160%</span>
              </div>
            </div>

            <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text-secondary);cursor:pointer;" title="Pin the first line of the function or struct being read at the top of the code view (G03)">
              <input type="checkbox" id="setting-sticky-scroll" ${stickyEnabled() ? "checked" : ""} /> Sticky scroll (pin the enclosing definition)
            </label>

            <button class="settings-reset-font-btn" id="btn-reset-fonts" style="align-self:flex-start;padding:3px 8px;font-size:10px;cursor:pointer;border:1px solid var(--border-color);background:var(--bg-tertiary);color:var(--text-secondary);border-radius:var(--radius-sm);transition:var(--transition-fast);">Reset to Defaults</button>
          </div>
        </div>

        <!-- 3. Local Storage Breakdown -->
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
            <div class="settings-metric-card" title="Bookmarks, notes, tours and watches you made: not part of the offline cache, never pruned">
              <div class="settings-metric-val">${userCount}</div>
              <div class="settings-metric-lbl">User data</div>
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
            <button class="settings-chip-btn" id="btn-clear-user" ${userCount ? "" : "disabled"} title="Delete your bookmarks, notes, tours and watches (asks first)">Clear User Data</button>
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
            <button class="settings-btn" id="btn-export-user" title="Download your bookmarks, notes, tours and watches as JSON">
              <span>⬇ Export User Data (${userCount})</span>
              <span style="font-size:10px;color:var(--text-muted);">&rarr;</span>
            </button>
            <button class="settings-btn" id="btn-import-user" title="Add bookmarks, notes, tours and watches from an export (existing ones are kept)">
              <span>⬆ Import User Data…</span>
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

    const closePopover = () => {
      popover.remove();
      document.removeEventListener("click", outsideClickHandler);
    };
    popover._closePopover = closePopover;

    const outsideClickHandler = (e) => {
      if (!popover.contains(e.target) && e.target !== anchorEl) {
        closePopover();
      }
    };
    setTimeout(() => document.addEventListener("click", outsideClickHandler), 10);

    popover.querySelector("#close-settings-btn").onclick = closePopover;

    popover.querySelector("#btn-check-updates").onclick = (e) => this.checkForUpdates(e.currentTarget);
    popover.querySelector("#setting-sticky-scroll").onchange = (e) => {
      try {
        if (e.target.checked) localStorage.removeItem(STICKY_KEY);
        else localStorage.setItem(STICKY_KEY, "off");
      } catch {
        // storage unavailable: the choice lasts for this page
      }
      document.querySelectorAll(".code-sticky").forEach((el) => { if (!e.target.checked) el.hidden = true; });
      document.dispatchEvent(new CustomEvent("code:sticky-changed"));
    };
    popover.querySelector("#btn-parser-status").onclick = () => {
      closePopover();
      state.openTab({ type: "status", title: "Parser status" });
    };
    // One line of ingestion status (G11), filled when the server answers
    if (isOnline) {
      api.getIngestStatus().then((st) => {
        const el = popover.querySelector("#settings-parser-line");
        if (el && popover.isConnected) el.textContent = `⛏ ${statusLine(st)}`;
      }).catch(() => {
        const el = popover.querySelector("#settings-parser-line");
        if (el) el.textContent = "⛏ Parser status unavailable";
      });
    }
    popover.querySelector("#btn-keyboard-shortcuts").onclick = () => {
      closePopover();
      showShortcutsOverlay();
    };

    const codeSlider = popover.querySelector("#setting-code-font-slider");
    const codeVal = popover.querySelector("#setting-code-font-val");
    if (codeSlider) {
      codeSlider.oninput = (e) => {
        const val = parseInt(e.target.value, 10);
        if (codeVal) codeVal.textContent = `${val}px`;
        applyTypographySettings(val, null);
      };
    }

    const themeSelect = popover.querySelector("#setting-theme");
    if (themeSelect) themeSelect.onchange = () => applyTheme(themeSelect.value);
    const hoverToggle = popover.querySelector("#setting-hover-enabled");
    const hoverDelay = popover.querySelector("#setting-hover-delay");
    if (hoverToggle) hoverToggle.onchange = () => { try { localStorage.setItem("kip-hover-cards", hoverToggle.checked ? "1" : "0"); } catch { /* not kept */ } };
    if (hoverDelay) hoverDelay.oninput = () => {
      popover.querySelector("#setting-hover-delay-val").textContent = `${hoverDelay.value} ms`;
      try { localStorage.setItem("kip-hover-delay", hoverDelay.value); } catch { /* not kept */ }
    };
    const uiSlider = popover.querySelector("#setting-ui-font-slider");
    const uiVal = popover.querySelector("#setting-ui-font-val");
    if (uiSlider) {
      uiSlider.oninput = (e) => {
        const val = parseInt(e.target.value, 10);
        if (uiVal) uiVal.textContent = `${val}%`;
        applyTypographySettings(null, val);
      };
    }

    const resetFontsBtn = popover.querySelector("#btn-reset-fonts");
    if (resetFontsBtn) {
      resetFontsBtn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (codeSlider) codeSlider.value = "12";
        if (codeVal) codeVal.textContent = "12px";
        if (uiSlider) uiSlider.value = "100";
        if (uiVal) uiVal.textContent = "100%";
        applyTypographySettings(12, 100);
        toast.info("Font sizes reset to defaults");
      };
    }

    popover.querySelectorAll(".settings-store-chips .settings-chip-btn").forEach((chip) => {
      chip.onclick = async () => {
        const storeName = chip.dataset.store;
        if (!storeName) return;
        try {
          await localDB.clearStore(storeName);
          toast.success(`Cleared ${storeName} offline cache`);
          closePopover();
        } catch (err) {
          toast.error(`Failed to clear ${storeName}: ${err.message}`);
        }
      };
    });

    popover.querySelector("#btn-clear-cache").onclick = async () => {
      try {
        await localDB.clearDataStores();
        if ("caches" in window) {
          await caches.delete("kip-api");
        }
        toast.success("Cleared all offline IndexedDB and Service Worker API caches");
        closePopover();
      } catch (err) {
        toast.error("Failed to clear offline cache: " + err.message);
      }
    };

    popover.querySelector("#btn-reset-tabs").onclick = async () => {
      try {
        localStorage.removeItem("kernel_ide_open_tabs");
        localStorage.removeItem("kernel_ide_active_tab_id");
        localStorage.removeItem("kernel_ide_state");
        state.cancelPendingSave();
        await localDB.delete("tabs_state", "current_session").catch(() => { });
        state.openTabs = [];
        state.tabs = [];
        state.kconfigEngines = {};
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
        closePopover();
      } catch (err) {
        toast.error("Failed to reset tabs: " + err.message);
      }
    };

    popover.querySelector("#btn-export-user").onclick = () => exportUserData();
    popover.querySelector("#btn-import-user").onclick = () => importUserData();
    popover.querySelector("#btn-clear-user").onclick = async () => {
      if (!confirm(`Delete your ${userCount} bookmarks, notes, tours and watches?\n\nThis cannot be undone (use Export User Data first to keep a copy).`)) return;
      await localDB.userClear();
      toast.success("User data deleted");
      closePopover();
    };

    popover.querySelector("#btn-full-reset").onclick = async () => {
      const confirmed = confirm(
        "Are you sure you want to reset all local data?\n\nThis will wipe offline IndexedDB caches, clear tabs and local storage settings, and reload the application."
        + (userCount ? `\n\nIt also deletes your ${userCount} bookmarks, notes, tours and watches. Cancel and use Export User Data first to keep them.` : "")
      );
      if (!confirmed) return;

      try {
        // Nothing may be written back after the wipe (a pending save would be flushed on reload)
        state.suspendPersistence();
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

  /**
   * Settings "Check for Updates": the 60 s sync run now (syncManager.checkNow()). When it found an update the page
   * restarts at once (into the new build when there is one) and the restarted page toasts what was found.
   */
  async checkForUpdates(btn) {
    const status = btn.querySelector("#check-updates-status");
    btn.disabled = true;
    status.textContent = "Checking…";
    let restarting = false;
    try {
      const r = await syncManager.checkNow();
      if (r.updated) {
        restarting = true;
        status.textContent = "Restarting…";
        syncManager.restart(r.newBuild, describeUpdates(r));
        return;
      }
      status.textContent = "Up to date";
      toast.info("Everything is up to date (frontend, database, parsed versions, Git, repositories)");
    } catch (err) {
      status.textContent = "Failed";
      toast.error("Update check failed: " + err.message);
    } finally {
      if (!restarting) btn.disabled = false;
    }
  }

  async initVersionSelector() {
    const select = document.getElementById("version-select");
    const projSelect = document.getElementById("project-select");
    const branchSelect = document.getElementById("branch-select");
    // The header shows the active tab's project; a later call (another tab activated) supersedes this one
    const seq = (this._headerSeq = (this._headerSeq || 0) + 1);
    const stale = () => seq !== this._headerSeq;

    // 1. Projects (configured repositories; each tab's requests name its own, §5.18)
    if (projSelect) {
      try {
        const pRes = await api.forRepo(state.currentRepo).getProjects();
        if (stale()) return;
        this.projects = pRes.projects || [];
        setProjects(this.projects);
        if (!state.currentRepo || !this.projects.some((p) => p.repo === state.currentRepo)) {
          state.currentRepo = pRes.active || this.projects[0]?.repo || "";
          state.saveState();
        }
        // Tabs saved before tabs carried their repository belong to this project
        state.assignTabRepos(state.currentRepo);
        projSelect.innerHTML = this.projects.map((p) => {
          const label = `${p.name}${p.indexed ? "" : " (git only)"}${p.available ? "" : " — missing"}`;
          return `<option value="${escapeAttr(p.repo)}" ${p.repo === state.currentRepo ? "selected" : ""} ${p.available ? "" : "disabled"}>${escapeAttr(label)}</option>`;
        }).join("");
        projSelect.title = "Repository (projects detected under workspace.repos_dir)";
        // The Man activity button (§5.38) exists while a man-pages repository is configured
        const manBtn = document.querySelector('.activity-btn[data-action="man"]');
        if (manBtn) manBtn.hidden = !manPagesRepo();
        projSelect.onchange = () => this.switchProject(projSelect.value);
      } catch (err) {
        console.warn("Could not load projects:", err);
      }
    }

    this.headerRepo = state.currentRepo;
    const client = api.forRepo(state.currentRepo);

    // 2. Branches: the selected one scopes the Tag and RC lists to the tags reachable from it
    if (branchSelect) {
      try {
        const bRes = await client.getBranches();
        if (stale()) return;
        this.branches = bRes.branches || [{ name: "master" }];
        this.tagViews = bRes.tag_views || (this.projects || []).find((p) => p.repo === state.currentRepo)?.tag_views || [];
        const isTagView = (scope) => !scope || scope.startsWith("view:") || (this.tagViews || []).some((v) => v.name === scope);
        if (state.selectedBranch && !isTagView(state.selectedBranch) && !this.branches.some((b) => b.name === state.selectedBranch)) {
          state.setSelectedBranch(""); // the stored scope is not a branch of this repository
        }
        this.renderBranchSelector();
        branchSelect.onchange = () => this.selectBranch(branchSelect.value);
      } catch (err) {
        console.warn("Could not load branches:", err);
      }
    }

    // 3. Versions (release tags)
    if (!select) return;
    try {
      // Every tag including release candidates; releases fill the Tag selector, -rc# the RC selector
      const res = await client.getVersions(true);
      if (stale()) return;
      this.rawVersions = res.versions || ["v3.0"];
      this.buildVersionCatalog(this.rawVersions);
      const branchNames = (this.branches || []).map((b) => b.name);
      // The branch scope only narrows what the dropdowns offer, so it is read in the background:
      // the lists render complete and narrow once the walk of that branch's history comes back
      this.loadBranchScope(state.selectedBranch).then(() => this.updateVersionSelectorDisplay());

      // Validate and sanitize the explored and the selected revision (commit SHAs and branch names
      // are valid targets too)
      const valid = (ref) => this.versionByName.has(ref) || isCommitRef(ref) || branchNames.includes(ref);
      const fallback = () => {
        // The man-pages project (§5.38): its newest tag, parsed or not (its pages render from Git too)
        if (isManPagesRepo(state.currentRepo)) {
          const newest = newestTag(this.tagVersions.map((t) => t.vname));
          if (newest) return newest;
        }
        const indexed = this.tagVersions.filter((t) => t.indexed && !t.rcOnly);
        return (indexed[indexed.length - 1] || this.tagVersions[this.tagVersions.length - 1] || { vname: "v3.0" }).vname;
      };
      if (!valid(state.currentVersion)) state.currentVersion = fallback();
      if (!valid(state.selectedVersion)) state.selectedVersion = state.currentVersion;

      // Picking a release selects its final tag (or its latest -rc when it was never released).
      // Selecting changes nothing on its own: **Apply** or **New Tab** act on the selection.
      select.onchange = (e) => {
        const base = e.target.value;
        if (!base || base.includes("object")) return;
        let target = this.versionByName.has(base) ? base : null;
        if (!target) {
          const subs = this.rcsByBase.get(base) || [];
          target = subs[0]?.vname || base;
        }
        if (target) state.setSelectedVersion(target);
      };

      const rcSelect = document.getElementById("rc-select");
      if (rcSelect) {
        rcSelect.onchange = (e) => {
          const val = e.target.value;
          if (val) state.setSelectedVersion(val);
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
          current: state.selectedVersion,
          scoped: true, // its tag search offers the releases of the selected branch (§5.13)
          onSelect: (ref) => {
            if (ref) state.setSelectedVersion(ref);
          }
        });
      };
    }

    // 5. Apply (switch the current tab) and New Tab (repository explorer on the selection)
    const applyBtn = document.getElementById("version-apply-btn");
    if (applyBtn) {
      applyBtn.onclick = async () => {
        const target = state.selectedVersion;
        const tab = state.getActiveTab();
        if (!tab) {
          openVersionInNewTab(target);
          toast.info(`No tab to switch: opened a repository explorer on ${shortRef(target)}`);
          return;
        }
        if (tab.version === target) {
          toast.info(`${tab.title} is already on ${shortRef(target)}`);
          return;
        }
        if (await requestVersionSwitch(target)) toast.success(`${tab.title} switched to ${shortRef(target)}`);
      };
    }
    const newTabBtn = document.getElementById("version-newtab-btn");
    if (newTabBtn) {
      newTabBtn.onclick = () => openVersionInNewTab(state.selectedVersion);
    }

    this.updateVersionSelectorDisplay();
  }

  /** Active tag_view if current branch selection matches one, otherwise null for real Git branches. */
  getActiveTagView() {
    const views = (this.tagViews && this.tagViews.length) ? this.tagViews : [
      {
        name: "\u2014 any branch \u2014",
        regex_rules: [
          "^(v2\\.6\\.\\d+)((?:-(?:rc|pre|alpha|beta)\\d*|\\.\\d+(?:-(?:rc|pre|alpha|beta)\\d*)?).*)$",
          "^(v(?!2\\.6\\.)\\d+\\.\\d+)((?:-(?:rc|pre|alpha|beta)\\d*|\\.\\d+(?:-(?:rc|pre|alpha|beta)\\d*)?).*)$"
        ]
      }
    ];
    const branch = state.selectedBranch;
    if (branch === "" || branch === "view:0") return views[0];
    if (branch.startsWith("view:")) {
      const idx = parseInt(branch.slice(5), 10);
      return (!isNaN(idx) && views[idx]) ? views[idx] : null;
    }
    const namedView = views.find((v) => v.name === branch);
    if (namedView) return namedView;
    return null;
  }

  /** Natural version comparison helper (e.g. 3.0.101 > 3.0.2, -rc7 > -rc1, .28 > .28-rc1). */
  static naturalVersionCompare(a, b) {
    if (a === b) return 0;
    const ax = [], bx = [];
    (a || "").replace(/(\d+)|(\D+)/g, (_, $1, $2) => { ax.push([$1 ? 0 : 1, $1 ? parseInt($1, 10) : $2]); });
    (b || "").replace(/(\d+)|(\D+)/g, (_, $1, $2) => { bx.push([$1 ? 0 : 1, $1 ? parseInt($1, 10) : $2]); });
    for (let i = 0; i < Math.max(ax.length, bx.length); i++) {
      const ai = ax[i], bi = bx[i];
      if (ai === undefined) {
        if (bi && typeof bi[1] === "string" && bi[1].startsWith("-")) return 1;
        return -1;
      }
      if (bi === undefined) {
        if (ai && typeof ai[1] === "string" && ai[1].startsWith("-")) return -1;
        return 1;
      }
      if (ai[0] !== bi[0]) return ai[0] - bi[0];
      if (ai[1] !== bi[1]) return ai[1] < bi[1] ? -1 : 1;
    }
    return 0;
  }

  /** Branch dropdown: tag_views (e.g. "any branch") plus the repository's branches, the scope selected. */
  renderBranchSelector() {
    const branchSelect = document.getElementById("branch-select");
    if (!branchSelect) return;
    const scope = state.selectedBranch;
    const views = (this.tagViews && this.tagViews.length) ? this.tagViews : [
      {
        name: "\u2014 any branch \u2014",
        regex_rules: [
          "^(v2\\.6\\.\\d+)((?:-(?:rc|pre|alpha|beta)\\d*|\\.\\d+(?:-(?:rc|pre|alpha|beta)\\d*)?).*)$",
          "^(v(?!2\\.6\\.)\\d+\\.\\d+)((?:-(?:rc|pre|alpha|beta)\\d*|\\.\\d+(?:-(?:rc|pre|alpha|beta)\\d*)?).*)$"
        ]
      }
    ];
    const options = [
      ...views.map((v, i) => ({ value: i === 0 ? "" : `view:${i}`, label: v.name })),
      ...(this.branches || []).map((b) => ({ value: b.name, label: b.is_default ? `${b.name} (default)` : b.name })),
    ];
    branchSelect.innerHTML = options.map((o) =>
      `<option value="${escapeAttr(o.value)}" ${o.value === scope ? "selected" : ""}>${escapeAttr(o.label)}</option>`
    ).join("");
    const activeView = this.getActiveTagView();
    branchSelect.title = activeView
      ? `Tag layout: ${activeView.name} (regex grouped)`
      : (scope ? `Tag and Release lists hold the releases of ${scope} only` : "Every tag of the repository; pick a branch to list only the releases on it");
  }

  /**
   * Pick the branch or custom tag_view: custom tag_views lay out tags via regex rules, while real
   * Git branches scope to tags reachable from them and preserve standard Git pre-release grouping.
   */
  async selectBranch(name) {
    const branch = typeof name === "string" ? name.trim() : "";
    state.setSelectedBranch(branch);
    if (branch && !branch.startsWith("view:")) {
      state.setSelectedVersion(branch);
    }
    this.renderBranchSelector();
    await this.loadBranchScope(branch);
    this.buildVersionCatalog(this.rawVersions);
    this.updateVersionSelectorDisplay();
  }

  /**
   * Read tags reachable from `branch` into `state.branchTags`. If a custom tag_view is selected,
   * no reachability filtering is applied (branchTags = null).
   */
  async loadBranchScope(branch) {
    const b = typeof branch === "string" ? branch.trim() : "";
    if (!b || b.startsWith("view:")) {
      state.setBranchTags("", null);
      return;
    }
    if (state.branchTagsFor === b) return; // already in hand for this branch
    const select = document.getElementById("version-select");
    if (select) select.title = `Reading the releases of ${b}\u2026`;
    try {
      const repo = state.currentRepo;
      const res = await api.forRepo(repo).getBranchTags(b);
      if (state.currentRepo !== repo) return; // the header moved to another project meanwhile
      state.setBranchTags(b, res?.tags || []);
    } catch (err) {
      console.warn(`Could not list the tags of ${b}:`, err);
      if (state.selectedBranch !== b) return;
      state.setBranchTags("", null);
      toast.warning(`Could not list the tags of ${b}: every tag stays listed`);
    }
  }

  /** True when a release belongs to the selected branch (itself or any of its candidates reachable). */
  releaseOnBranch(release) {
    const scope = state.branchTags;
    if (!scope || !release) return true;
    if (scope.has(release.vname)) return true;
    return (this.rcsByBase?.get(release.vname) || []).some((rc) => scope.has(rc.vname));
  }

  /**
   * Parse a tag into base release and sub-version/RC suffix according to active tag_view regex rules,
   * or standard Git pre-release pattern if on a real Git branch.
   */
  parseTagVersion(vname) {
    const activeView = this.getActiveTagView();
    if (activeView && Array.isArray(activeView.regex_rules) && activeView.regex_rules.length > 0) {
      if (!this._regexCache) this._regexCache = new Map();
      for (const rule of activeView.regex_rules) {
        try {
          let re = this._regexCache.get(rule);
          if (!re) {
            re = new RegExp(rule);
            this._regexCache.set(rule, re);
          }
          const m = re.exec(vname || "");
          if (m && m[1]) {
            const base = m[1];
            const suffix = m[2] || "";
            if (suffix) {
              return { base, suffix, isSub: true };
            }
          }
        } catch (e) {
          console.warn(`Invalid tag_view regex rule '${rule}':`, e);
        }
      }
      return this.prefixedRelease(vname) || { base: vname, suffix: "", isSub: false };
    }
    const prefixed = this.prefixedRelease(vname);
    if (prefixed) return prefixed;
    // Real Git branch: standard Git pre-release grouping (only -rc/-pre/etc are sub-versions)
    const m = /^(.*?)(-(?:rc|pre|alpha|beta)\d*)$/.exec(vname || "");
    if (m && m[1] && m[2]) {
      return { base: m[1], suffix: m[2], isSub: true };
    }
    return { base: vname, suffix: "", isSub: false };
  }

  /**
   * Release of a prefixed release tag (`<prefix>X.Y[.Z][-rcN]`, release_order.js) in a repository without
   * kernel-scheme tags: point releases and candidates group under their series' final tag
   * (`man-pages-6.9.1` → `man-pages-6.9`). Null in a kernel repository (its tags keep the tag_view rules).
   */
  prefixedRelease(vname) {
    return this._prefixedFinals ? prefixedReleaseOf(vname, this._prefixedFinals) : null;
  }

  /** Release a tag belongs to: "v3.1-rc4" → "v3.1", "v3.0.1" → "v3.0" (in tag_view), etc. */
  releaseOf(vname) {
    return this.parseTagVersion(vname).base;
  }

  /**
   * Split the version catalog into releases (Tag selector) and sub-versions/pre-releases grouped
   * under the release they belong to (Release selector).
   */
  buildVersionCatalog(rawVersions) {
    if (rawVersions) this.rawVersions = rawVersions;
    const versions = this.rawVersions || rawVersions || ["v3.0"];
    const entries = versions
      .map((v) => (typeof v === "string"
        ? { vname: v, commit_hash: "", indexed: true, ref_type: 1, is_prerelease: /-(rc|pre|alpha|beta)\d*$/.test(v) }
        : { vname: v.vname || v.name, commit_hash: v.commit_hash || "", indexed: v.indexed !== false, ref_type: v.ref_type ?? 1, is_prerelease: Boolean(v.is_prerelease) }))
      .filter((v) => v.vname);
    this.versionByName = new Map(entries.map((v) => [v.vname, v]));
    // Prefixed release tags group by series only where no kernel-scheme tag exists (release_tag_names rule)
    const tagNames = entries.filter((v) => v.ref_type !== 2 && v.ref_type !== 3).map((v) => v.vname);
    this._prefixedFinals = tagNames.some((n) => familyOf(n) === KERNEL_FAMILY) ? null : prefixedFinals(tagNames);
    this.rcsByBase = new Map();
    const releases = [];
    const seenBases = new Set();
    for (const v of entries) {
      if (v.ref_type === 2 || v.ref_type === 3) continue; // branches and bare commits are not releases
      const parsed = this.parseTagVersion(v.vname);
      const base = parsed.base;
      if (parsed.isSub) {
        if (!this.rcsByBase.has(base)) this.rcsByBase.set(base, []);
        this.rcsByBase.get(base).push({ ...v, suffix: parsed.suffix });
        if (!this.versionByName.has(base) && !seenBases.has(base)) {
          seenBases.add(base);
          releases.push({ vname: base, commit_hash: "", indexed: v.indexed, rcOnly: true });
        }
      } else if (!seenBases.has(v.vname)) {
        seenBases.add(v.vname);
        releases.push(v);
      }
    }
    // Sort sub-versions: stable point releases descending first, then RCs descending
    const isRc = (s) => /^-(rc|pre|alpha|beta)/.test(s || "");
    for (const subs of this.rcsByBase.values()) {
      subs.sort((a, b) => {
        const aIsRc = isRc(a.suffix);
        const bIsRc = isRc(b.suffix);
        if (!aIsRc && bIsRc) return -1;
        if (aIsRc && !bIsRc) return 1;
        return KernelIdeApp.naturalVersionCompare(b.vname, a.vname);
      });
    }
    // A release counts as parsed when its final tag, or any of its candidates, was parsed
    for (const r of releases) {
      if (r.rcOnly) r.indexed = (this.rcsByBase.get(r.vname) || []).some((rc) => rc.indexed);
    }
    this.tagVersions = releases;
  }

  /** Reflect the selected revision and branch scope in the Tag, Release and Branch controls. */
  updateVersionSelectorDisplay() {
    const cur = state.selectedVersion;
    const scope = state.branchTags; // tags of the selected branch, null while no branch scopes them
    const branch = state.selectedBranch;
    const tags = (this.tagVersions || []).filter((t) => this.releaseOnBranch(t));
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
      // A release the branch scope excludes is still listed while it is the selection, under a group
      // saying so, so the dropdown never hides what it is showing
      const offBranch = (base && !tags.some((t) => t.vname === base))
        ? [this.tagVersions.find((t) => t.vname === base) || { vname: base, indexed: tag.indexed }]
        : [];
      // Parsed versions first; tags only present in Git stay browsable (tree, files, diffs)
      const groups = [
        { label: "Parsed", items: tags.filter((t) => t.indexed) },
        { label: "Git only (not parsed)", items: tags.filter((t) => !t.indexed) },
        { label: `Not on ${branch}`, items: offBranch },
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
      const activeView = this.getActiveTagView();
      select.title = activeView
        ? `${tags.length} base release${tags.length === 1 ? "" : "s"} (${activeView.name})`
        : (branch && scope
          ? `${tags.length} release${tags.length === 1 ? "" : "s"} on ${branch}`
          : "Every release of the repository");
    }

    // Release selector: sub-versions (stable point releases first descending, final, then RCs descending)
    const rcSelect = document.getElementById("rc-select");
    const rcLabel = document.getElementById("rc-select-label");
    const rcs = base
      ? (this.rcsByBase?.get(base) || []).filter((rc) => !scope || scope.has(rc.vname) || rc.vname === cur)
      : [];
    if (rcSelect) {
      const show = rcs.length > 0;
      rcSelect.hidden = !show;
      if (rcLabel) {
        rcLabel.hidden = !show;
        rcLabel.textContent = "Release:";
      }
      rcSelect.innerHTML = "";
      if (show) {
        const finalTag = this.versionByName.get(base);
        const finalListed = finalTag && (!scope || scope.has(base) || base === cur);

        const isRc = (s) => /^-(rc|pre|alpha|beta)/.test(s || "");
        const stables = rcs.filter((r) => !isRc(r.suffix));
        const prereleases = rcs.filter((r) => isRc(r.suffix));
        const formatSuffix = (suffix) => {
          if (!suffix) return "";
          if (suffix.startsWith("-rc") || suffix.startsWith("-pre")) {
            return suffix.slice(1);
          }
          return suffix;
        };

        const options = [
          ...stables.map((s) => ({ value: s.vname, text: formatSuffix(s.suffix), indexed: s.indexed })),
          ...(finalListed ? [{ value: base, text: "final", indexed: finalTag.indexed }] : []),
          ...prereleases.map((rc) => ({ value: rc.vname, text: formatSuffix(rc.suffix), indexed: rc.indexed })),
        ];

        for (const o of options) {
          const opt = document.createElement("option");
          opt.value = o.value;
          opt.textContent = o.indexed ? o.text : `${o.text} (git only)`;
          if (o.value === cur) opt.selected = true;
          rcSelect.appendChild(opt);
        }
        rcSelect.title = `${rcs.length} release/candidate${rcs.length === 1 ? "" : "s"} of ${base}`;
      }
    }

    // The Branch dropdown shows what scopes these lists, not the explored revision: picking a branch
    // narrows them (and selects that branch head), picking a tag from them leaves the scope alone.
    const branchSelect = document.getElementById("branch-select");
    if (branchSelect && Array.from(branchSelect.options).some((o) => o.value === branch)) {
      branchSelect.value = branch;
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
        commitBtn.title = `Selected ${cur}\nClick to select another commit`;
      }
    }

    // Apply stands out while the selection is not what the current tab shows
    const applyBtn = document.getElementById("version-apply-btn");
    const activeTab = state.getActiveTab();
    if (applyBtn) {
      const pending = Boolean(activeTab) && activeTab.version !== cur;
      applyBtn.classList.toggle("pending", pending);
      applyBtn.title = activeTab
        ? (pending
          ? `Switch ${activeTab.title} from ${shortRef(activeTab.version)} to ${shortRef(cur)}`
          : `${activeTab.title} is already on ${shortRef(cur)}`)
        : `Open ${shortRef(cur)} in a new tab`;
    }
    const newTabBtn = document.getElementById("version-newtab-btn");
    if (newTabBtn) newTabBtn.title = `Open a repository explorer on ${shortRef(cur)} in a new tab`;
  }

  /**
   * Default revision of a project: its latest parsed release (else its newest tag, else its default
   * branch); the man-pages project's newest release tag, parsed or not (§5.38).
   */
  async defaultVersionOf(repo) {
    let version = "";
    try {
      const res = await api.forRepo(repo).getVersions(true);
      const versions = (res.versions || []).filter((v) => !v.is_prerelease);
      const indexed = versions.filter((v) => v.indexed !== false && v.ref_type !== 3);
      version = (indexed[indexed.length - 1] || versions[versions.length - 1] || {}).vname || "";
      if (isManPagesRepo(repo)) version = newestTag(versions.filter((v) => v.ref_type !== 2 && v.ref_type !== 3).map((v) => v.vname)) || version;
    } catch (err) {
      console.warn("Could not load versions for project:", err);
    }
    return version || (this.projects || []).find((p) => p.repo === repo)?.default_branch || "HEAD";
  }

  /**
   * The header's Project picked `repo` (§5.18): no tab is closed and nothing is asked. A tab whose address
   * holds in any repository (explorer, commits, search, treemap, status) moves to `repo` at its default
   * revision, like Apply moves it to another revision; any other active tab stays and the project's
   * default view opens next to it (the man index in the man-pages project, else a repository explorer).
   * Without an open tab it only becomes the project new tabs open in.
   */
  async switchProject(repo) {
    const projSelect = document.getElementById("project-select");
    const tab = state.getActiveTab();
    if (!repo || repo === state.activeRepo()) return;
    const version = await this.defaultVersionOf(repo);
    const label = (this.projects || []).find((p) => p.repo === repo)?.name || repo;
    if (!tab) {
      state.setDefaultRepository(repo, version);
    } else if (PROJECT_MOVABLE_TABS[tab.type]) {
      const patch = PROJECT_MOVABLE_TABS[tab.type](tab, label);
      state.moveTabToRepository(tab, repo, version, patch);
    } else if (isManPagesRepo(repo)) {
      state.openTab({ type: "man", title: "Man pages", manPage: "", manSec: "", manPath: "", manQuery: "", repo, version, kernelVersion: "", kernelRepo: "" });
    } else {
      state.openTab({ type: "explorer", title: `Explorer ${shortRef(version)}`, path: "", repo, version, forceNew: true });
    }
    if (projSelect) projSelect.value = state.currentRepo;
    toast.success(`Project ${label} @ ${shortRef(version)}`);
  }

  /** Commands listed in the palette (">" prefix). */
  paletteCommands() {
    const v = () => state.getActiveTab()?.version || state.currentVersion;
    const activeCode = () => {
      const tab = state.getActiveTab();
      return tab && tab.type === "code" && tab.path ? tab : null;
    };
    const commands = [
      { title: "Switch workspace session…", hint: "Choose or open a saved session", keywords: "session workspace window", run: () => showSessionPickerModal({ allowClose: true }) },
      {
        title: "New workspace session…", hint: "Create a fresh session in this window", keywords: "session new workspace",
        run: () => {
          const count = sessionManager.getSessionsIndex().length;
          const name = prompt("Enter a name for the new session:", `Session ${count + 1}`);
          if (name === null) return;
          const newSession = sessionManager.createSession({
            name: name.trim() || `Session ${count + 1}`,
            version: state.currentVersion || "v3.0",
            repo: state.currentRepo || "",
            pin: false
          });
          state.adoptSession(newSession);
          toast.success(`Created session '${newSession.name}'`);
        }
      },
      {
        title: "Duplicate workspace session…", hint: "Clone current session into a new session", keywords: "session duplicate clone workspace",
        run: () => {
          if (!state.currentSessionId) return;
          const dup = sessionManager.duplicateSession(state.currentSessionId);
          if (dup) {
            state.adoptSession(dup);
            toast.success(`Duplicated into '${dup.name}'`);
          }
        }
      },
      { title: "Compare versions or commits", hint: "Files, Kconfig & man-pages changes", keywords: "diff compare", run: () => state.openTab({ type: "diff", title: "Compare" }) },
      { title: "Compare kernel APIs", hint: "Syscalls, exports, parameters, tracepoints and modules added, removed or changed between two revisions", keywords: "api changelog diff exports compare", run: () => state.openTab({ type: "diff", title: "Compare", mode: "api", versionB: v() }) },
      { title: "Compare man pages", hint: "Man entries added, modified or deleted between two man-pages revisions", keywords: "man manual manpage man-pages diff compare", run: () => {
        const repo = manPagesRepo();
        if (!repo) { toast.info("No man-pages repository is configured (a repository whose project is man-pages)"); return; }
        state.openTab({ type: "diff", title: "Compare", repo, mode: "man" });
      } },
      { title: "Open man pages", hint: "Linux man-pages index (sections, filter), Web or Terminal rendering", keywords: "man manual manpage man-pages syscall", run: () => this.openManIndex() },
      { title: "Open Kernel API", hint: "Syscalls, exports, parameters, tracepoints, modules, device tables", keywords: "kapi api catalog", run: () => state.openTab({ type: "api", title: "Kernel API", version: v() }) },
      ...apiSections().map((sec) => ({ title: `Kernel API: ${sec.label}`, hint: sec.hint || "", keywords: `kapi ${sec.keywords || ""}`,
        run: () => state.openTab({ type: "api", title: `Kernel API · ${sec.label}`, view: sec.id, version: v() }) })),
      { title: "Open Insights", hint: "Release stats, merge window, subsystem health, stable, security", keywords: "insights report statistics", run: () => state.openTab({ type: "insights", title: "Insights", version: v() }) },
      ...insightsSections().map((sec) => ({ title: `Insights: ${sec.label}`, hint: sec.hint || "", keywords: `insights ${sec.keywords || ""}`,
        run: () => state.openTab({ type: "insights", title: `Insights · ${sec.label}`, view: sec.id, version: v() }) })),
      { title: "Show watch list", hint: "Files, symbols, Kconfig options and MAINTAINERS sections you follow", keywords: "watch follow notify bell changes", run: () => state.openTab({ type: "watch", title: "Watch list" }) },
      { title: "Watch this tab", hint: "The file of a code tab, the directory of an explorer, a Kconfig option, a MAINTAINERS section", keywords: "watch follow notify", run: async () => {
        const { toggleWatch } = await import("./views/watch/watch_runtime.js");
        const tab = state.getActiveTab();
        const t = tab?.type === "code" && tab.path ? { type: "file", path: tab.path }
          : tab?.type === "explorer" && tab.path ? { type: "dir", path: tab.path.replace(/\/+$/, "") }
          : tab?.type === "kconfig" && tab.symbol ? { type: "kconfig", name: tab.symbol, arch: tab.arch }
          : tab?.type === "maintainers" && tab.section ? { type: "section", name: tab.section } : null;
        if (!t) {
          toast.info("This tab has nothing to watch");
          return;
        }
        try {
          const r = await toggleWatch(t);
          toast.success(r.added ? `Watching ${r.label}` : `Stopped watching ${r.label}`);
        } catch (err) {
          toast.error(err.message);
        }
      } },
      { title: "Show bookmarks & tours", hint: "Bookmarks, notes and code tours (Ctrl+Alt+B bookmarks a line)", keywords: "bookmark note tour favorites saved", run: () => state.openTab({ type: "bookmarks", title: "Bookmarks" }) },
      { title: "Bookmark this tab", hint: "A commit, mail thread or Kconfig option tab (code lines: Ctrl+Alt+B)", keywords: "bookmark save favorite", run: async () => {
        const { targetOfTab, toggleBookmark } = await import("./views/bookmarks/bookmark_store.js");
        const t = targetOfTab(state.getActiveTab());
        if (!t) {
          toast.info("This tab has nothing to bookmark (in a code tab, press Ctrl+Alt+B on a line)");
          return;
        }
        const r = await toggleBookmark(t);
        toast.success(r.added ? `Bookmarked ${r.record.data.label}` : `Bookmark ${r.record.data.label} removed`);
      } },
      { title: "Open patch series", hint: "Files staged with ✎ Create patch → Add to series: patches, cover letter, recipients, mbox", keywords: "patch series format-patch mbox cover letter send", run: () => state.openTab({ type: "patch", title: "Patch series", version: state.getPatchDrafts()?.base || state.currentVersion }) },
      { title: "Show parser status", hint: "Versions parsed and being parsed, database size, server caches", keywords: "ingest ingestion parser progress database status", run: () => state.openTab({ type: "status", title: "Parser status" }) },
      { title: "Keyboard shortcuts", hint: `List and rebind them (${shortcuts.label("help.shortcuts") || "?"})`, keywords: "keys keybindings hotkeys help rebind", run: () => showShortcutsOverlay() },
      { title: "Search text in files…", hint: "git grep over the revision (or type % in the palette)", keywords: "grep find code full-text search", run: () => state.openTab({ type: "search", title: "Search", version: v() }) },
      { title: "Select commit…", hint: "Tags and commits", keywords: "version checkout ref", run: () => document.getElementById("commit-picker-btn")?.click() },
      { title: "Open source treemap", hint: "Bytes / files per directory", keywords: "size disk usage", run: () => state.openTab({ type: "treemap", title: "Treemap: root", path: "", version: v() }) },
      {
        title: "Export compile_commands.json", hint: "clang compilation database (x86)", keywords: "clangd lsp download",
        run: () => {
          window.location.href = api.getCompileCommandsDownloadUrl(v(), "x86");
          toast.info("Generating compile_commands.json…");
        }
      },
      { title: "Open KConfig engine", hint: "Menuconfig, defconfigs, dependencies", keywords: "config", run: () => { const a = state.kconfigArch(); state.openTab({ type: "kconfig", title: `KConfig (${a})`, arch: a }); } },
      {
        title: "Enable KConfig engine in a tab (#ifdef dimming)", hint: "Dims compiled-out preprocessor branches", keywords: "ifdef dim preprocessor config engine",
        run: () => {
          // An engine resolves the configuration of one KConfig tab, so it needs one to belong to
          const active = state.getActiveTab();
          const target = active && active.type === "kconfig" && !state.isKconfigEngineOwner(active.id)
            ? active
            : state.kconfigTabs().find((t) => !state.isKconfigEngineOwner(t.id));
          if (!target) {
            toast.info(state.kconfigTabs().length ? "Every open KConfig tab already runs its engine" : "Open a KConfig tab to enable an engine");
            return;
          }
          // The tab shows what resolving its values changes before it takes the engine (§5.0)
          state.requestKconfigEngine(target.id);
        }
      },
      {
        title: "Disable every KConfig engine", hint: `${state.kconfigEngineTabIds.length} running`, keywords: "ifdef dim config engine off",
        run: () => {
          const had = state.disableAllKconfigEngines();
          toast.info(had.length ? `${had.length} KConfig engine(s) disabled (their configurations are kept)` : "No KConfig engine is running");
        }
      },
      { title: "Open repository explorer tab", hint: "Browse the tree of this revision", keywords: "files tree browse", run: () => state.openTab({ type: "explorer", title: `Explorer ${v()}`, path: "", version: v(), forceNew: true }) },
      { title: "Open Git commits timeline", hint: "History of the version", keywords: "log", run: () => state.openTab({ type: "commits", title: "Commits" }) },
      { title: "Open Mailing List & LKML search", hint: "Search patches and threads", keywords: "mail lkml lore patch thread", run: () => state.openTab({ type: "mail", title: "Mailing List" }) },
      { title: "Open subsystems & maintainers", hint: "MAINTAINERS, CREDITS", keywords: "people", run: () => state.openTab({ type: "maintainers", title: "Maintainers" }) },
      { title: "Open NodeMap canvas", hint: "Schematic view", keywords: "graph", run: () => state.openTab({ type: "nodemap", title: "NodeMap Canvas" }) },
      { title: "Open Pahole struct layout inspector", hint: "Memory layout, cachelines & padding holes", keywords: "pahole struct layout memory cacheline hole padding", run: () => state.openTab({ type: "pahole", title: "struct task_struct", structName: "task_struct" }) },
      { title: "Open Callgraph function explorer", hint: "Bidirectional callers and callees", keywords: "callgraph callers callees function graph", run: () => state.openTab({ type: "callgraph", title: "Callgraph: schedule", functionName: "schedule" }) },
      { title: "Open kernel documentation", hint: "Documentation/index.rst", keywords: "docs rst", run: () => state.openTab({ type: "code", title: "index.rst", path: "Documentation/index.rst", version: v() }) },
      { title: "Toggle file explorer", hint: "Sidebar", keywords: "sidebar tree collapse", run: () => this.toggleSidebar() },
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
          commands.push({ title: `Switch project: ${p.name}`, hint: p.path, keywords: "repository project tab", run: () => this.switchProject(p.repo) });
        }
      }
    }
    return commands;
  }

  /**
   * Single owner of the sidebar's collapsed state: the activity-bar Explorer button, the
   * sidebar header's collapse button, the command palette and `explorer:reveal` all route
   * here so the class, both buttons and the stored preference never disagree.
   */
  setSidebarCollapsed(collapsed) {
    const sidebar = document.getElementById("sidebar");
    if (!sidebar) return;

    sidebar.classList.toggle("collapsed", collapsed);

    const expBtn = document.querySelector('.activity-btn[data-action="explorer"]');
    if (expBtn) expBtn.classList.toggle("active", !collapsed);

    const collapseBtn = document.getElementById("sidebar-collapse");
    if (collapseBtn) collapseBtn.setAttribute("aria-expanded", String(!collapsed));

    try {
      localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
    } catch {
      /* private mode or blocked storage: the sidebar still toggles, it just will not be remembered */
    }
  }

  toggleSidebar() {
    const sidebar = document.getElementById("sidebar");
    if (!sidebar) return;
    this.setSidebarCollapsed(!sidebar.classList.contains("collapsed"));
  }

  setupSidebarCollapse() {
    const collapseBtn = document.getElementById("sidebar-collapse");
    if (collapseBtn) collapseBtn.addEventListener("click", () => this.setSidebarCollapsed(true));

    let collapsed = false;
    try {
      collapsed = localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1";
    } catch {
      /* ignore: default to expanded */
    }
    this.setSidebarCollapsed(collapsed);
  }

  /**
   * Man activity button (§5.38): the man pages index. In the man-pages project, at the explored tag;
   * from the kernel, at the man-pages tag released on or before the explored kernel revision
   * (`/api/man/resolve`), else the newest tag.
   */
  async openManIndex() {
    const index = { manPage: "", manSec: "", manPath: "", manQuery: "", title: "Man pages" };
    if (isManPagesRepo(state.activeRepo())) {
      state.openTab({ type: "man", ...index, repo: state.activeRepo(), version: state.activeVersion(), kernelVersion: "", kernelRepo: "" });
      return;
    }
    const repo = manPagesRepo();
    if (!repo) {
      toast.info("No man-pages repository is configured (a repository whose project is man-pages)");
      return;
    }
    const kernel = state.activeVersion();
    const kernelRepo = state.activeRepo();
    let mv = "";
    try {
      const res = await api.forRepo(kernelRepo).resolveMan("intro", "1", kernel);
      if (res?.available && res.repo === repo) mv = res.version;
    } catch (err) {
      console.warn("Man-pages tag lookup failed:", err);
    }
    // Without a resolved tag the man tab opens on the newest one (man_view.js)
    state.openTab({ type: "man", ...index, repo, version: mv, kernelVersion: kernel, kernelRepo });
  }

  setupActivityBar() {
    const buttons = document.querySelectorAll(".activity-btn");

    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const action = btn.dataset.action;

        if (action === "explorer") {
          this.toggleSidebar();
        } else if (action === "kconfig") {
          const a = state.kconfigArch();
          state.openTab({ type: "kconfig", title: `KConfig (${a})`, arch: a });
        } else if (action === "nodemap") {
          state.openTab({ type: "nodemap", title: "NodeMap Canvas" });
        } else if (action === "maintainers") {
          state.openTab({ type: "maintainers", title: "Maintainers" });
        } else if (action === "commits") {
          state.openTab({ type: "commits", title: "Commits" });
        } else if (action === "mail") {
          state.openTab({ type: "mail", title: "Mailing List" });
        } else if (action === "api") {
          state.openTab({ type: "api", title: "Kernel API" });
        } else if (action === "insights") {
          state.openTab({ type: "insights", title: "Insights" });
        } else if (action === "man") {
          this.openManIndex();
        } else if (action === "compare" || action === "tools") {
          state.openTab({ type: "diff", title: "Compare" });
        }
      });
    });
  }

  async expandTreeItem(path) {
    const treeContainer = document.getElementById("sidebar-tree-root");
    if (!treeContainer || !path) return null;
    const selector = `.tree-item[data-path="${CSS.escape(path)}"]`;
    const targetRow = treeContainer.querySelector(selector);
    if (targetRow && !targetRow.classList.contains("expanded") && typeof targetRow._expandDir === "function") {
      await targetRow._expandDir();
    }
    return targetRow;
  }

  async loadSidebarFileTree() {
    const treeContainer = document.getElementById("sidebar-tree-root");
    if (!treeContainer) return;

    if (!this._explorerSubscribed) {
      this._explorerSubscribed = true;
      state.subscribe("explorer:reveal", async (dirPath) => {
        this.setSidebarCollapsed(false);

        if (!dirPath) return;
        const parts = dirPath.split("/").filter(Boolean);
        let curPrefix = "";
        for (const part of parts) {
          curPrefix += (curPrefix ? "/" : "") + part;
          await this.expandTreeItem(curPrefix);
        }
      });
    }

    const headerLabel = document.querySelector("#sidebar .sidebar-header span");
    if (headerLabel) headerLabel.textContent = `Repository Explorer · ${shortRef(state.currentVersion)}`;

    treeContainer.innerHTML = `<div style="padding:12px;color:var(--text-muted);">Loading files...</div>`;

    try {
      const res = await api.getDirectoryTree(state.currentVersion, "");
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
        ${isDir
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
      const doExpand = async () => {
        if (row.dataset.loading === "true") return;
        expanded = !expanded;
        row.classList.toggle("expanded", expanded);
        childContainer.style.display = expanded ? "block" : "none";

        if (expanded && childContainer.children.length === 0) {
          row.dataset.loading = "true";
          try {
            if (Array.isArray(item.children) && item.children.length > 0) {
              item.children.forEach((c) => this.renderFileTreeNode(childContainer, c, depth + 1));
            } else {
              const res = await api.getDirectoryTree(state.currentVersion, item.path);
              const children = res.entries || res.tree || [];
              children.forEach((c) => this.renderFileTreeNode(childContainer, c, depth + 1));
            }
          } catch (err) {
            console.warn("Failed to expand dir:", err);
          } finally {
            row.dataset.loading = "false";
          }
        }
      };
      row._expandDir = doExpand;
      row.onclick = doExpand;
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

export function applyTypographySettings(codeFontSize = null, uiFontScale = null) {
  let cfs = codeFontSize;
  if (cfs === null) {
    const raw = localStorage.getItem("kip-code-font-size");
    cfs = raw ? parseInt(raw, 10) : 12;
  }
  cfs = Math.max(10, Math.min(26, Number.isFinite(cfs) ? cfs : 12));

  let ufs = uiFontScale;
  if (ufs === null) {
    const raw = localStorage.getItem("kip-ui-font-scale");
    ufs = raw ? parseInt(raw, 10) : 100;
  }
  ufs = Math.max(80, Math.min(160, Number.isFinite(ufs) ? ufs : 100));

  localStorage.setItem("kip-code-font-size", String(cfs));
  localStorage.setItem("kip-ui-font-scale", String(ufs));

  const rowHeight = Math.max(16, Math.round(cfs * 1.667));
  const fontScale = ufs / 100;
  const layoutScale = 1 + (fontScale - 1) * 0.5;
  document.documentElement.style.setProperty("--code-font-size", `${cfs}px`);
  document.documentElement.style.setProperty("--code-row-height", `${rowHeight}px`);
  document.documentElement.style.setProperty("--ui-font-scale", fontScale.toString());
  document.documentElement.style.setProperty("--ui-layout-scale", layoutScale.toString());

  state.emit("codeview:font-changed", { fontSize: cfs, rowHeight });
}

function escapeAttr(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** What "Check for Updates" found (syncManager.checkNow()), as the restarted page toasts it. */
function describeUpdates(r) {
  const parts = [];
  if (r.frontend) parts.push(`new frontend build ${r.newBuild}`);
  if (r.db) parts.push("database rebuilt (cache cleared)");
  if (r.changed.length) parts.push(`${r.changed.length} version(s) re-parsed or moved`);
  if (r.added.length && !r.db) parts.push(`${r.added.length} new version(s)/branch(es)`);
  if (r.removed.length) parts.push(`${r.removed.length} removed`);
  if (r.projects && !r.db) parts.push("repository list updated");
  if (!parts.length) parts.push("version lists refreshed");
  return parts.join(", ");
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
