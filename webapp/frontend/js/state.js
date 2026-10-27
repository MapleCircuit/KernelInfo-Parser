/**
 * state.js - Reactive Global State Store.
 * Manages versions, multi-pane tabs, active tab, kconfig assignments, and event bus.
 */
import { localDB } from "./db.js";

// A resolved configuration (defconfig loaded in the KConfig view) has far more than the default seeds
export const MIN_CONFIG_SYMBOLS = 50;
// ARCH (and what is known of its configuration) when the KConfig engine does not provide one
export const DEFAULT_DEFINITION_ARCH = "x86_64";
const DEFAULT_ARCH_BASELINE = { X86: "y", X86_64: "y", "64BIT": "y", X86_32: "n" };

/** Key a KConfig configuration is saved under: "version|ARCH", plus "|name" for a named copy. */
export function kconfigProfileKey(version, arch, configName = "") {
  const name = (configName || "").trim();
  return `${version}|${arch}${name ? `|${name}` : ""}`;
}

class GlobalState {
  constructor() {
    this.subscribers = new Map();

    // Default State
    this.currentVersion = "v3.0";
    this.currentRepo = ""; // configured repository name ("" = server default)
    // Id of the KConfig tab holding the Kconfig constraint engine ("" = engine off, the default).
    // Only one tab may hold it: its resolved configuration is the published one and gates #ifdef
    // dimming in code views, so two tabs resolving two configurations at once cannot happen.
    this.kconfigEngineTabId = "";
    // KConfig tab asked to take the engine (command palette), until its view claims the request
    this.kconfigEnginePendingTabId = "";
    this.splitMode = "single"; // 'single' | 'horizontal' | 'vertical'
    this.activePaneId = "pane-1";
    this.panes = [
      {
        id: "pane-1",
        activeTabId: "tab-default",
        tabs: [
          {
            id: "tab-default",
            type: "code",
            title: "sched.c",
            version: "v3.0",
            path: "kernel/sched.c",
            cursorLine: 1,
            scrollLine: 1
          }
        ]
      }
    ];

    // Active KConfig configuration (bare and CONFIG_ keys) used for #ifdef dimming. Filled by the
    // KConfig view: the engine's resolved .config values, or raw values while the engine is off.
    this.kconfigAssignments = {};
    // Saved configuration per "version|ARCH" (bare keys), so switching architecture keeps each config
    this.kconfigProfiles = {};
    this.kconfigProfileKey = "";
    // Toolchain profile per "version|ARCH" (compiler, versions, env and probe overrides; see the API docs)
    this.kconfigToolchains = {};

    // Load persisted state if available
    this.loadState();
  }

  subscribe(event, callback) {
    if (!this.subscribers.has(event)) {
      this.subscribers.set(event, new Set());
    }
    this.subscribers.get(event).add(callback);
    return () => this.subscribers.get(event).delete(callback);
  }

  emit(event, data) {
    if (this.subscribers.has(event)) {
      this.subscribers.get(event).forEach((cb) => {
        try {
          cb(data);
        } catch (e) {
          console.error(`Error in state subscriber for ${event}:`, e);
        }
      });
    }
  }

  setVersion(version) {
    const vStr = typeof version === "string" ? version : (version?.vname || version?.name || "v3.0");
    if (!vStr || vStr === "[object Object]" || vStr.includes("object")) return;
    this.currentVersion = vStr;

    // Update active tab's version so it reloads in the newly selected version
    const activeTab = this.getActiveTab();
    if (activeTab && activeTab.version !== vStr) {
      activeTab.version = vStr;
    }

    this.emit("version:changed", vStr);
    this.saveState();
  }

  /** Switch the active repository (project); tabs belong to one repository so the workspace resets. */
  setRepository(name, defaultVersion) {
    if (!name || name === this.currentRepo) return;
    this.currentRepo = name;
    this.currentVersion = defaultVersion || this.currentVersion;
    this.panes = [{ id: "pane-1", activeTabId: null, tabs: [] }];
    this.activePaneId = "pane-1";
    this.splitMode = "single";
    const hadEngine = Boolean(this.kconfigEngineTabId);
    this.kconfigEngineTabId = ""; // the tab that held it is gone
    this.saveState();
    if (hadEngine) this.emit("kconfig:engine:changed", { active: false, tabId: "", previousTabId: "" });
    this.emit("repo:changed", name);
    this.emit("layout:changed", { splitMode: this.splitMode, panes: this.panes });
    this.emit("version:changed", this.currentVersion);
  }

  /**
   * Profile key of a KConfig tab: "version|ARCH" for its first configuration, "version|ARCH|name"
   * for a named copy, so several configurations of one ARCH and revision stay independent (and
   * keys saved before names existed keep working).
   */
  kconfigProfileKeyOf(tab) {
    if (!tab) return "";
    return kconfigProfileKey(tab.version, tab.arch, tab.configName);
  }

  /** Names already used by the KConfig tabs of one revision and ARCH ("" = the unnamed first one). */
  kconfigConfigNames(version, arch, exceptTabId = null) {
    return new Set(this.kconfigTabs()
      .filter((t) => t.id !== exceptTabId && t.version === version && t.arch === arch)
      .map((t) => t.configName || ""));
  }

  /** First free copy name for a revision and ARCH: "v2", "v3", … (the unnamed one counts as v1). */
  nextKconfigConfigName(version, arch, exceptTabId = null) {
    const used = this.kconfigConfigNames(version, arch, exceptTabId);
    for (let n = 2; ; n += 1) {
      if (!used.has(`v${n}`)) return `v${n}`;
    }
  }

  /**
   * Give a KConfig tab a free name when another tab already holds its "version|ARCH|name" (after a
   * revision or ARCH switch), so two tabs never share one configuration. Returns the name it got.
   */
  ensureUniqueKconfigConfigName(tab) {
    if (!tab || tab.type !== "kconfig") return "";
    const used = this.kconfigConfigNames(tab.version, tab.arch, tab.id);
    if (!used.has(tab.configName || "")) return tab.configName || "";
    tab.configName = this.nextKconfigConfigName(tab.version, tab.arch, tab.id);
    this.saveState();
    return tab.configName;
  }

  /** Move a saved configuration and its toolchain profile to another key (rename, ARCH switch). */
  moveKconfigProfile(fromKey, toKey) {
    if (!fromKey || !toKey || fromKey === toKey) return;
    if (this.kconfigProfiles[fromKey]) {
      this.kconfigProfiles[toKey] = this.kconfigProfiles[fromKey];
      delete this.kconfigProfiles[fromKey];
    }
    if (this.kconfigToolchains[fromKey]) {
      this.kconfigToolchains[toKey] = this.kconfigToolchains[fromKey];
      delete this.kconfigToolchains[fromKey];
    }
    if (this.kconfigProfileKey === fromKey) this.kconfigProfileKey = toKey;
    this.saveState();
  }

  /** Whether any KConfig tab holds the constraint engine. */
  get kconfigEngineActive() {
    return Boolean(this.kconfigEngineTabId);
  }

  /** Whether this KConfig tab is the one holding the engine. */
  isKconfigEngineOwner(tabId) {
    return Boolean(tabId) && this.kconfigEngineTabId === tabId;
  }

  /** The tab holding the engine, or null when it is off. */
  kconfigEngineOwnerTab() {
    return this.kconfigEngineTabId ? this.findTab(this.kconfigEngineTabId) : null;
  }

  /** Every open KConfig tab, in pane order. */
  kconfigTabs() {
    return this.panes.flatMap((p) => p.tabs.filter((t) => t.type === "kconfig"));
  }

  /**
   * Ask a KConfig tab to take the engine. It is a request, not a decision: the tab's view asks the
   * user about the values kconfig would change before taking it (webapp/API.md §5.0). A tab in the
   * background is shown first, and claims the request when its view renders.
   */
  requestKconfigEngine(tabId) {
    if (!tabId) return false;
    this.kconfigEnginePendingTabId = tabId;
    this.focusTab(tabId);
    this.emit("kconfig:engine:requested", tabId);
    return true;
  }

  /** Claim a pending engine request for this tab (true once). */
  takeKconfigEngineRequest(tabId) {
    if (!tabId || this.kconfigEnginePendingTabId !== tabId) return false;
    this.kconfigEnginePendingTabId = "";
    return true;
  }

  /**
   * Hand the engine to one KConfig tab (by id) or turn it off (null/""). Handing it over takes it
   * from whichever tab held it, so at most one engine is ever enabled. Configurations are untouched:
   * each one stays in `kconfigProfiles` under its "version|ARCH" key.
   */
  setKconfigEngineOwner(tabId) {
    const next = tabId || "";
    if (next === this.kconfigEngineTabId) return;
    const previousTabId = this.kconfigEngineTabId;
    this.kconfigEngineTabId = next;
    this.emit("kconfig:engine:changed", { active: Boolean(next), tabId: next, previousTabId });
    this.saveState();
  }

  /** Whether the KConfig engine is on with a resolved configuration (gates #ifdef dimming and build banners). */
  kconfigConfigActive() {
    return this.kconfigEngineActive
      && Boolean(this.kconfigProfileKey.split("|")[1])
      && Object.keys(this.kconfigAssignments || {}).length / 2 >= MIN_CONFIG_SYMBOLS;
  }

  /** ARCH of the KConfig view's active profile (x86_64 until one was chosen). */
  kconfigArch() {
    return this.kconfigProfileKey.split("|")[1] || DEFAULT_DEFINITION_ARCH;
  }

  /**
   * ARCH and configuration deciding which of several per-architecture definitions applies.
   * Engine on with a resolved configuration: its ARCH and values (`strict`: absent symbols are "n").
   * Otherwise x86_64 with a baseline (X86, X86_64, 64BIT set, X86_32 not); other symbols are unknown.
   */
  definitionContext() {
    if (this.kconfigConfigActive()) {
      return { arch: this.kconfigProfileKey.split("|")[1], assignments: this.kconfigAssignments, strict: true };
    }
    return { arch: DEFAULT_DEFINITION_ARCH, assignments: DEFAULT_ARCH_BASELINE, strict: false };
  }

  syncVersionFromTab(version) {
    const vStr = typeof version === "string" ? version : (version?.vname || version?.name || "v3.0");
    if (!vStr || vStr === "[object Object]" || vStr.includes("object")) return;
    if (this.currentVersion === vStr) return;
    this.currentVersion = vStr;
    this.emit("version:synced", vStr);
    this.saveState();
  }


  setSplitMode(mode) {
    if (this.splitMode === mode) return;
    this.splitMode = mode;

    if (mode !== "single" && this.panes.length === 1) {
      // Add second pane
      this.panes.push({
        id: "pane-2",
        activeTabId: null,
        tabs: []
      });
    } else if (mode === "single" && this.panes.length > 1) {
      // Merge all tabs into pane-1
      const p2Tabs = this.panes[1].tabs;
      this.panes[0].tabs.push(...p2Tabs);
      this.panes.splice(1, 1);
      this.activePaneId = "pane-1";
    }

    this.emit("layout:changed", { splitMode: this.splitMode, panes: this.panes });
    this.saveState();
  }

  setActivePane(paneId) {
    if (this.activePaneId === paneId) return;
    this.activePaneId = paneId;
    this.emit("pane:activated", paneId);
  }

  getPane(paneId) {
    return this.panes.find((p) => p.id === paneId) || this.panes[0];
  }

  getActivePane() {
    return this.getPane(this.activePaneId);
  }

  getActiveTab() {
    const pane = this.getActivePane();
    if (!pane || !pane.activeTabId) return null;
    return pane.tabs.find((t) => t.id === pane.activeTabId) || null;
  }

  /** Tab with this id in any pane, or null. */
  findTab(tabId) {
    if (!tabId) return null;
    for (const pane of this.panes) {
      const tab = pane.tabs.find((t) => t.id === tabId);
      if (tab) return tab;
    }
    return null;
  }

  /** Show an already open tab (and activate its pane) so its view renders. */
  focusTab(tabId) {
    const pane = this.panes.find((p) => p.tabs.some((t) => t.id === tabId));
    if (!pane) return false;
    const wasShown = pane.activeTabId === tabId && this.activePaneId === pane.id;
    pane.activeTabId = tabId;
    this.activePaneId = pane.id;
    if (!wasShown) {
      this.emit("tabs:changed", { paneId: pane.id, activeTabId: tabId });
      this.saveState();
    }
    return true;
  }

  openTab(tabData, targetPaneId = null) {
    const pane = targetPaneId ? this.getPane(targetPaneId) : this.getActivePane();
    if (!pane) return;

    // Check if duplicate tab exists (bypassed if tabData.forceNew is true)
    const existing = tabData.forceNew
      ? null
      : pane.tabs.find((t) => {
          if (t.type !== tabData.type) return false;
          if (t.type === "code") return t.path === tabData.path && t.version === tabData.version;
          if (t.type === "kconfig") return t.arch === tabData.arch && t.version === tabData.version && (t.configName || "") === (tabData.configName || "");
          if (t.type === "nodemap" && (tabData.initialNodes || tabData.initialNode)) return false;
          if (t.type === "diff") return true;
          return t.title === tabData.title;
        });

    if (existing) {
      pane.activeTabId = existing.id;
      if (tabData.cursorLine) existing.cursorLine = tabData.cursorLine;
      if ("diffMarks" in tabData) existing.diffMarks = tabData.diffMarks;
      if (tabData.symbol) existing.symbol = tabData.symbol;
      if (tabData.title) existing.title = tabData.title;
    } else {
      const id = "tab-" + Math.random().toString(36).substring(2, 9);
      const tabVer = (tabData.version && typeof tabData.version === "string" && !tabData.version.includes("object"))
        ? tabData.version
        : this.currentVersion;
      const newTab = {
        id,
        ...tabData,
        version: tabVer
      };
      pane.tabs.push(newTab);
      pane.activeTabId = id;
    }


    this.emit("tabs:changed", { paneId: pane.id, activeTabId: pane.activeTabId });
    this.saveState();
  }

  closeTab(tabId, paneId = null) {
    const pane = paneId ? this.getPane(paneId) : this.getActivePane();
    if (!pane) return;

    const idx = pane.tabs.findIndex((t) => t.id === tabId);
    if (idx === -1) return;

    pane.tabs.splice(idx, 1);

    // A closed tab cannot hold the engine; its configuration stays in kconfigProfiles
    const releasedEngine = this.kconfigEngineTabId === tabId;
    if (releasedEngine) this.kconfigEngineTabId = "";

    if (pane.activeTabId === tabId) {
      if (pane.tabs.length > 0) {
        const nextTab = pane.tabs[Math.max(0, idx - 1)];
        pane.activeTabId = nextTab.id;
        if (nextTab && nextTab.version && nextTab.version !== this.currentVersion) {
          this.syncVersionFromTab(nextTab.version);
        }
      } else {
        pane.activeTabId = null;
      }
    }

    if (releasedEngine) {
      this.emit("kconfig:engine:changed", { active: false, tabId: "", previousTabId: tabId });
    }
    this.emit("tabs:changed", { paneId: pane.id, activeTabId: pane.activeTabId });
    this.saveState();
  }

  setKconfigAssignment(symbol, value) {
    if (!symbol) return;
    const bare = symbol.startsWith("CONFIG_") ? symbol.substring(7) : symbol;
    this.kconfigAssignments[bare] = value;
    this.kconfigAssignments[`CONFIG_${bare}`] = value;
    this.emit("kconfig:assignment:changed", { symbol: bare, value, assignments: this.kconfigAssignments });
    this.saveState();
  }

  /**
   * Store a configuration under its "version|ARCH" profile. `publish` (the default) also makes it
   * the active configuration used for dimming; a KConfig tab that does not hold the engine passes
   * `publish: false` so it cannot overwrite the engine owner's resolved configuration.
   */
  setKconfigAssignments(newAssignments, profileKey = null, { publish = true } = {}) {
    const normalized = {};
    const bareOnly = {};
    for (const [k, v] of Object.entries(newAssignments || {})) {
      const bare = k.startsWith("CONFIG_") ? k.substring(7) : k;
      normalized[bare] = v;
      normalized[`CONFIG_${bare}`] = v;
      bareOnly[bare] = v;
    }
    if (!publish) {
      if (profileKey) this.kconfigProfiles[profileKey] = bareOnly;
      this.saveState();
      return;
    }
    if (profileKey) this.kconfigProfileKey = profileKey;
    if (this.kconfigProfileKey) this.kconfigProfiles[this.kconfigProfileKey] = bareOnly;
    this.kconfigAssignments = normalized;
    this.emit("kconfig:assignments:updated", this.kconfigAssignments);
    this.saveState();
  }

  /** Saved configuration (bare keys) of a "version|ARCH" profile, or null. */
  getKconfigProfile(profileKey) {
    return this.kconfigProfiles[profileKey] || null;
  }

  getKconfigToolchain(profileKey) {
    return this.kconfigToolchains[profileKey] || {};
  }

  setKconfigToolchain(profileKey, toolchain) {
    if (toolchain && Object.keys(toolchain).length) this.kconfigToolchains[profileKey] = toolchain;
    else delete this.kconfigToolchains[profileKey];
    this.saveState();
  }

  dropKconfigProfile(profileKey) {
    delete this.kconfigProfiles[profileKey];
    this.saveState();
  }

  saveState() {
    try {
      const stateObj = {
        version: this.currentVersion,
        repo: this.currentRepo,
        kconfigEngineTabId: this.kconfigEngineTabId,
        splitMode: this.splitMode,
        activePaneId: this.activePaneId,
        panes: this.panes,
        kconfigAssignments: this.kconfigAssignments,
        kconfigProfiles: this.kconfigProfiles,
        kconfigProfileKey: this.kconfigProfileKey,
        kconfigToolchains: this.kconfigToolchains
      };
      localStorage.setItem("kernel_ide_state", JSON.stringify(stateObj));
      localDB.set("tabs_state", "current_session", stateObj).catch(() => {});
    } catch (e) {
      console.warn("Failed to persist state:", e);
    }
  }

  loadState() {
    try {
      const saved = localStorage.getItem("kernel_ide_state");
      if (saved) {
        const parsed = JSON.parse(saved);
        if (parsed.version && typeof parsed.version === "string" && !parsed.version.includes("object")) {
          this.currentVersion = parsed.version;
        } else {
          this.currentVersion = "v3.0";
        }
        if (typeof parsed.repo === "string") this.currentRepo = parsed.repo;
        if (typeof parsed.kconfigEngineTabId === "string") this.kconfigEngineTabId = parsed.kconfigEngineTabId;
        if (parsed.splitMode) this.splitMode = parsed.splitMode;
        if (parsed.activePaneId) this.activePaneId = parsed.activePaneId;
        if (Array.isArray(parsed.panes) && parsed.panes.length > 0) {
          parsed.panes.forEach((p) => {
            if (Array.isArray(p.tabs)) {
              p.tabs.forEach((t) => {
                if (!t.version || typeof t.version !== "string" || t.version.includes("object")) {
                  t.version = "v3.0";
                }
              });
            }
          });
          this.panes = parsed.panes;
        }
        if (parsed.kconfigAssignments) this.kconfigAssignments = parsed.kconfigAssignments;
        if (parsed.kconfigProfiles && typeof parsed.kconfigProfiles === "object") this.kconfigProfiles = parsed.kconfigProfiles;
        if (typeof parsed.kconfigProfileKey === "string") this.kconfigProfileKey = parsed.kconfigProfileKey;
        if (parsed.kconfigToolchains && typeof parsed.kconfigToolchains === "object") this.kconfigToolchains = parsed.kconfigToolchains;
        // The engine belongs to a live KConfig tab or to none (sessions saved before it was owned
        // by a tab, and tabs closed since, leave it off; their configurations are kept)
        const owner = this.findTab(this.kconfigEngineTabId);
        if (!owner || owner.type !== "kconfig") this.kconfigEngineTabId = "";
      }
    } catch (e) {
      console.warn("Failed to load saved state:", e);
    }
  }

}

export const state = new GlobalState();
