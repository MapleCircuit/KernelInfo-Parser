/**
 * state.js - Reactive Global State Store.
 * Manages versions, multi-pane tabs, active tab, kconfig engines and the event bus.
 */
import { localDB } from "./db.js";

// A resolved configuration (defconfig loaded in the KConfig view) has far more than the default seeds
export const MIN_CONFIG_SYMBOLS = 50;
// ARCH (and what is known of its configuration) when no KConfig engine provides one
export const DEFAULT_DEFINITION_ARCH = "x86_64";
const DEFAULT_ARCH_BASELINE = { X86: "y", X86_64: "y", "64BIT": "y", X86_32: "n" };

/** Key a KConfig configuration is saved under: "version|ARCH", plus "|name" for a named copy. */
export function kconfigProfileKey(version, arch, configName = "") {
  const name = (configName || "").trim();
  return `${version}|${arch}${name ? `|${name}` : ""}`;
}

/** Bare and CONFIG_ keys of a configuration ({FOO: "y"} -> {FOO: "y", CONFIG_FOO: "y"}). */
function normalizeAssignments(values) {
  const normalized = {};
  for (const [k, v] of Object.entries(values || {})) {
    const bare = k.startsWith("CONFIG_") ? k.substring(7) : k;
    normalized[bare] = v;
    normalized[`CONFIG_${bare}`] = v;
  }
  return normalized;
}

/** Bare keys only, the form configurations are saved in. */
function bareAssignments(values) {
  const bare = {};
  for (const [k, v] of Object.entries(values || {})) {
    bare[k.startsWith("CONFIG_") ? k.substring(7) : k] = v;
  }
  return bare;
}

class GlobalState {
  constructor() {
    this.subscribers = new Map();

    // Default State
    // Revision of the active tab: what links opened from it follow
    this.currentVersion = "v3.0";
    // Revision picked in the header (Tag / RC / Branch / Commit). It changes nothing on its own:
    // **Apply** moves the active tab to it, **New Tab** opens a repository explorer on it.
    this.selectedVersion = "v3.0";
    this.currentRepo = ""; // configured repository name ("" = server default)
    // KConfig tabs holding the Kconfig constraint engine. Several may hold it at once: each one
    // resolves its own configuration, and a code tab picks which of them it follows.
    this.kconfigEngineTabIds = [];
    // Published configuration per engine tab: { tabId, version, arch, configName, profileKey, assignments }
    // (assignments carry bare and CONFIG_ keys; they are rehydrated from kconfigProfiles on load)
    this.kconfigEngines = {};
    // KConfig tab asked to take an engine (command palette), until its view claims the request
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

    // Saved configuration per "version|ARCH" (bare keys), so switching architecture keeps each config
    this.kconfigProfiles = {};
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

  /** Normalize whatever a selector or the URL hash hands over into a revision string. */
  static refOf(version) {
    const vStr = typeof version === "string" ? version : (version?.vname || version?.name || "");
    if (!vStr || vStr === "[object Object]" || vStr.includes("object")) return "";
    return vStr;
  }

  /**
   * Remember the revision picked in the header. Nothing is switched: **Apply** moves the active tab
   * to it (`setVersion`) and **New Tab** opens a repository explorer on it, so a tab's revision only
   * ever changes when the user asks for it.
   */
  setSelectedVersion(version) {
    const vStr = GlobalState.refOf(version);
    if (!vStr || vStr === this.selectedVersion) return false;
    this.selectedVersion = vStr;
    this.emit("version:selected", vStr);
    this.saveState();
    return true;
  }

  /** Move the active tab (and the explored revision) to `version`; see `requestVersionSwitch`. */
  setVersion(version) {
    const vStr = GlobalState.refOf(version);
    if (!vStr) return;
    this.currentVersion = vStr;
    this.selectedVersion = vStr;

    // Update active tab's version so it reloads in the newly selected version
    const activeTab = this.getActiveTab();
    if (activeTab && activeTab.version !== vStr) {
      activeTab.version = vStr;
      // A code tab re-opens at the top of the file: its saved position belongs to the old revision
      delete activeTab.scrollTop;
    }

    this.emit("version:changed", vStr);
    this.saveState();
  }

  /** Switch the active repository (project); tabs belong to one repository so the workspace resets. */
  setRepository(name, defaultVersion) {
    if (!name || name === this.currentRepo) return;
    this.currentRepo = name;
    this.currentVersion = defaultVersion || this.currentVersion;
    this.selectedVersion = this.currentVersion;
    this.panes = [{ id: "pane-1", activeTabId: null, tabs: [] }];
    this.activePaneId = "pane-1";
    this.splitMode = "single";
    const hadEngines = this.kconfigEngineTabIds.length > 0;
    this.kconfigEngineTabIds = []; // the tabs that held them are gone
    this.kconfigEngines = {};
    this.saveState();
    if (hadEngines) this.emit("kconfig:engine:changed", { tabId: "", active: false, engineTabIds: [] });
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
    for (const engine of Object.values(this.kconfigEngines)) {
      if (engine.profileKey === fromKey) engine.profileKey = toKey;
    }
    this.saveState();
  }

  // ------------------------------------------------------------------ KConfig engines
  /** Whether any KConfig tab holds an engine. */
  get kconfigEngineActive() {
    return this.kconfigEngineTabIds.length > 0;
  }

  /** Whether this KConfig tab holds an engine. */
  isKconfigEngineOwner(tabId) {
    return Boolean(tabId) && this.kconfigEngineTabIds.includes(tabId);
  }

  /** Every open KConfig tab, in pane order. */
  kconfigTabs() {
    return this.panes.flatMap((p) => p.tabs.filter((t) => t.type === "kconfig"));
  }

  /** The KConfig tabs holding an engine, in pane order. */
  kconfigEngineTabs() {
    return this.kconfigTabs().filter((t) => this.isKconfigEngineOwner(t.id));
  }

  /**
   * Engines a code tab can follow, in pane order:
   * `{ tabId, title, version, arch, configName, symbols, resolved }`.
   */
  kconfigEngineList() {
    return this.kconfigEngineTabs().map((tab) => {
      const engine = this.kconfigEngines[tab.id] || {};
      const symbols = Math.floor(Object.keys(engine.assignments || {}).length / 2);
      return {
        tabId: tab.id,
        title: tab.title,
        version: engine.version || tab.version,
        arch: engine.arch || tab.arch,
        configName: engine.configName || tab.configName || "",
        symbols,
        resolved: symbols >= MIN_CONFIG_SYMBOLS,
      };
    });
  }

  /**
   * Ask a KConfig tab to take an engine. It is a request, not a decision: the tab's view asks the
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
   * Turn the engine of one KConfig tab on or off. Engines are independent: every tab that holds one
   * resolves its own configuration, and switching one off keeps that tab's values (they stay in
   * `kconfigProfiles` under their "version|ARCH" key) — only the published resolution goes away.
   */
  setKconfigEngineEnabled(tabId, enabled) {
    if (!tabId) return;
    const held = this.isKconfigEngineOwner(tabId);
    if (held === Boolean(enabled)) return;
    if (enabled) {
      this.kconfigEngineTabIds = [...this.kconfigEngineTabIds, tabId];
    } else {
      this.kconfigEngineTabIds = this.kconfigEngineTabIds.filter((id) => id !== tabId);
      delete this.kconfigEngines[tabId];
    }
    this.emit("kconfig:engine:changed", { tabId, active: Boolean(enabled), engineTabIds: [...this.kconfigEngineTabIds] });
    this.saveState();
  }

  /** Switch every engine off (command palette); configurations are kept. */
  disableAllKconfigEngines() {
    const had = [...this.kconfigEngineTabIds];
    if (!had.length) return had;
    this.kconfigEngineTabIds = [];
    this.kconfigEngines = {};
    this.emit("kconfig:engine:changed", { tabId: "", active: false, engineTabIds: [] });
    this.saveState();
    return had;
  }

  /** Save a configuration under its profile key without publishing it (**⧉ Duplicate**). */
  saveKconfigProfileValues(profileKey, values) {
    if (!profileKey) return;
    this.kconfigProfiles[profileKey] = bareAssignments(values);
    this.saveState();
  }

  /**
   * Store a KConfig tab's configuration under its profile key and, when that tab holds the engine,
   * publish it as the engine's resolved configuration (what the code tabs following it dim with).
   */
  setKconfigAssignments(tabId, profileKey, values, { version = "", arch = "", configName = "" } = {}) {
    if (profileKey) this.kconfigProfiles[profileKey] = bareAssignments(values);
    if (tabId && this.isKconfigEngineOwner(tabId)) {
      const assignments = normalizeAssignments(values);
      this.kconfigEngines[tabId] = { tabId, version, arch, configName, profileKey, assignments };
      this.emit("kconfig:assignments:updated", { tabId, assignments });
    }
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

  /**
   * Engine a tab follows: the tab's own engine for a KConfig tab, the engine a code tab selected
   * (`tab.kconfigEngineTabId`), or "" when it follows none.
   */
  engineTabIdOf(tab) {
    if (!tab) return "";
    if (tab.type === "kconfig") return this.isKconfigEngineOwner(tab.id) ? tab.id : "";
    const picked = tab.kconfigEngineTabId;
    return picked && this.isKconfigEngineOwner(picked) ? picked : "";
  }

  /**
   * Engine a code tab should follow when it has not picked one: the first engine of the tab's own
   * revision, or none (the user picks one in the code toolbar themselves).
   */
  defaultEngineTabIdFor(tab) {
    if (!tab) return "";
    const version = tab.version || this.currentVersion;
    return this.kconfigEngineList().find((e) => e.version === version)?.tabId || "";
  }

  /** Engine the active tab follows, or "". */
  activeEngineTabId() {
    return this.engineTabIdOf(this.getActiveTab());
  }

  /** Whether that engine (the active tab's by default) is on with a resolved configuration. */
  kconfigConfigActive(engineTabId = null) {
    const id = engineTabId === null ? this.activeEngineTabId() : engineTabId;
    const engine = id ? this.kconfigEngines[id] : null;
    if (!engine || !engine.arch) return false;
    return Object.keys(engine.assignments || {}).length / 2 >= MIN_CONFIG_SYMBOLS;
  }

  /**
   * Engine a view that cannot follow one of its own (Compare, documentation, NodeMap) should go by:
   * the active tab's when that tab can follow an engine, else any running one.
   */
  fallbackEngineTabId() {
    const tab = this.getActiveTab();
    if (tab && (tab.type === "code" || tab.type === "kconfig")) return this.engineTabIdOf(tab);
    return this.kconfigEngineTabs()[0]?.id || "";
  }

  /**
   * ARCH a new KConfig tab should open on (x86_64 when no engine applies). Unlike
   * `definitionContext`, a tab that cannot follow an engine falls back to any running one, so
   * opening a Kconfig symbol from Compare or the documentation lands on the ARCH being configured.
   */
  kconfigArch(engineTabId = null) {
    const id = engineTabId === null ? this.fallbackEngineTabId() : engineTabId;
    return (id && this.kconfigEngines[id]?.arch) || DEFAULT_DEFINITION_ARCH;
  }

  /**
   * ARCH and configuration deciding which of several per-architecture definitions applies.
   * An engine with a resolved configuration (the active tab's by default, or the one passed):
   * its ARCH and values (`strict`: absent symbols are "n"). Otherwise x86_64 with a baseline
   * (X86, X86_64, 64BIT set, X86_32 not); other symbols are unknown.
   */
  definitionContext(engineTabId = null) {
    const id = engineTabId === null ? this.activeEngineTabId() : engineTabId;
    if (this.kconfigConfigActive(id)) {
      const engine = this.kconfigEngines[id];
      return { arch: engine.arch, assignments: engine.assignments, strict: true, engineTabId: id };
    }
    return { arch: DEFAULT_DEFINITION_ARCH, assignments: DEFAULT_ARCH_BASELINE, strict: false, engineTabId: "" };
  }

  syncVersionFromTab(version) {
    const vStr = GlobalState.refOf(version);
    if (!vStr || this.currentVersion === vStr) return;
    this.currentVersion = vStr;
    this.selectedVersion = vStr;
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

  /** Revision links opened from the active tab follow (its own, not the header selection). */
  activeVersion() {
    return this.getActiveTab()?.version || this.currentVersion;
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
          if (t.type === "explorer") return (t.path || "") === (tabData.path || "") && t.version === tabData.version;
          if (t.type === "kconfig") return t.arch === tabData.arch && t.version === tabData.version && (t.configName || "") === (tabData.configName || "");
          if (t.type === "nodemap" && (tabData.initialNodes || tabData.initialNode)) return false;
          if (t.type === "mail") {
            if (tabData.threadId || tabData.messageId) {
              return t.threadId === tabData.threadId && t.messageId === tabData.messageId;
            }
            return t.title === tabData.title;
          }
          if (t.type === "diff") return true;
          return t.title === tabData.title;
        });

    if (existing) {
      pane.activeTabId = existing.id;
      if (tabData.cursorLine) {
        existing.cursorLine = tabData.cursorLine;
        // Navigating to a line overrides the position this tab was left at
        delete existing.scrollTop;
      }
      if ("diffMarks" in tabData) existing.diffMarks = tabData.diffMarks;
      if (tabData.symbol) existing.symbol = tabData.symbol;
      if (tabData.title) existing.title = tabData.title;
      if (tabData.threadId !== undefined) existing.threadId = tabData.threadId;
      if (tabData.messageId !== undefined) existing.messageId = tabData.messageId;
      if (tabData.query !== undefined) existing.query = tabData.query;
    } else {
      const id = "tab-" + Math.random().toString(36).substring(2, 9);
      // A tab opened from another one inherits that tab's revision, never the header selection
      const tabVer = GlobalState.refOf(tabData.version) || this.activeVersion();
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

    // A closed tab cannot hold an engine; its configuration stays in kconfigProfiles
    const releasedEngine = this.isKconfigEngineOwner(tabId);
    if (releasedEngine) {
      this.kconfigEngineTabIds = this.kconfigEngineTabIds.filter((id) => id !== tabId);
      delete this.kconfigEngines[tabId];
    }

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
      this.emit("kconfig:engine:changed", { tabId, active: false, engineTabIds: [...this.kconfigEngineTabIds] });
    }
    this.emit("tabs:changed", { paneId: pane.id, activeTabId: pane.activeTabId });
    this.saveState();
  }

  saveState() {
    try {
      const stateObj = {
        version: this.currentVersion,
        selectedVersion: this.selectedVersion,
        repo: this.currentRepo,
        kconfigEngineTabIds: this.kconfigEngineTabIds,
        // Only the descriptors: each engine's values live in kconfigProfiles under profileKey
        kconfigEngineProfiles: Object.fromEntries(Object.entries(this.kconfigEngines).map(([id, e]) => [
          id,
          { version: e.version, arch: e.arch, configName: e.configName, profileKey: e.profileKey },
        ])),
        splitMode: this.splitMode,
        activePaneId: this.activePaneId,
        panes: this.panes,
        kconfigProfiles: this.kconfigProfiles,
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
        this.currentVersion = GlobalState.refOf(parsed.version) || "v3.0";
        this.selectedVersion = GlobalState.refOf(parsed.selectedVersion) || this.currentVersion;
        if (typeof parsed.repo === "string") this.currentRepo = parsed.repo;
        if (parsed.splitMode) this.splitMode = parsed.splitMode;
        if (parsed.activePaneId) this.activePaneId = parsed.activePaneId;
        if (Array.isArray(parsed.panes) && parsed.panes.length > 0) {
          parsed.panes.forEach((p) => {
            if (Array.isArray(p.tabs)) {
              p.tabs.forEach((t) => {
                if (!GlobalState.refOf(t.version)) t.version = "v3.0";
              });
            }
          });
          this.panes = parsed.panes;
        }
        if (parsed.kconfigProfiles && typeof parsed.kconfigProfiles === "object") this.kconfigProfiles = parsed.kconfigProfiles;
        if (parsed.kconfigToolchains && typeof parsed.kconfigToolchains === "object") this.kconfigToolchains = parsed.kconfigToolchains;
        // Engines belong to live KConfig tabs (sessions saved before engines were owned by tabs, and
        // tabs closed since, come back with their engine off; their configurations are kept)
        const engineIds = Array.isArray(parsed.kconfigEngineTabIds)
          ? parsed.kconfigEngineTabIds
          : (typeof parsed.kconfigEngineTabId === "string" && parsed.kconfigEngineTabId ? [parsed.kconfigEngineTabId] : []);
        this.kconfigEngineTabIds = engineIds.filter((id) => this.findTab(id)?.type === "kconfig");
        const descriptors = parsed.kconfigEngineProfiles || {};
        for (const tabId of this.kconfigEngineTabIds) {
          const tab = this.findTab(tabId);
          const d = descriptors[tabId] || {};
          const profileKey = d.profileKey || this.kconfigProfileKeyOf(tab);
          this.kconfigEngines[tabId] = {
            tabId,
            version: d.version || tab.version,
            arch: d.arch || tab.arch,
            configName: d.configName || tab.configName || "",
            profileKey,
            assignments: normalizeAssignments(this.kconfigProfiles[profileKey] || {}),
          };
        }
      }
    } catch (e) {
      console.warn("Failed to load saved state:", e);
    }
  }

}

export const state = new GlobalState();
