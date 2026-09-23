/**
 * state.js - Reactive Global State Store.
 * Manages versions, multi-pane tabs, active tab, kconfig assignments, and event bus.
 */
import { localDB } from "./db.js";

class GlobalState {
  constructor() {
    this.subscribers = new Map();

    // Default State
    this.currentVersion = "v3.0";
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

    // Client-side KConfig symbol assignments (for constraint evaluation & #ifdef dimming)
    this.kconfigAssignments = {
      "64BIT": "y",
      "CONFIG_64BIT": "y",
      "SMP": "y",
      "CONFIG_SMP": "y",
      "MODULES": "y",
      "CONFIG_MODULES": "y",
      "X86": "y",
      "CONFIG_X86": "y",
      "PRINTK": "y",
      "CONFIG_PRINTK": "y",
      "MAGIC_SYSRQ": "y",
      "CONFIG_MAGIC_SYSRQ": "y"
    };

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

  openTab(tabData, targetPaneId = null) {
    const pane = targetPaneId ? this.getPane(targetPaneId) : this.getActivePane();
    if (!pane) return;

    // Check if duplicate tab exists (bypassed if tabData.forceNew is true)
    const existing = tabData.forceNew
      ? null
      : pane.tabs.find((t) => {
          if (t.type !== tabData.type) return false;
          if (t.type === "code") return t.path === tabData.path && t.version === tabData.version;
          if (t.type === "kconfig") return t.arch === tabData.arch && t.version === tabData.version;
          if (t.type === "nodemap" && (tabData.initialNodes || tabData.initialNode)) return false;
          return t.title === tabData.title;
        });

    if (existing) {
      pane.activeTabId = existing.id;
      if (tabData.cursorLine) existing.cursorLine = tabData.cursorLine;
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

  setKconfigAssignments(newAssignments) {
    const normalized = {};
    for (const [k, v] of Object.entries(newAssignments || {})) {
      const bare = k.startsWith("CONFIG_") ? k.substring(7) : k;
      normalized[bare] = v;
      normalized[`CONFIG_${bare}`] = v;
    }
    this.kconfigAssignments = normalized;
    this.emit("kconfig:assignments:updated", this.kconfigAssignments);
    this.saveState();
  }

  saveState() {
    try {
      const stateObj = {
        version: this.currentVersion,
        splitMode: this.splitMode,
        activePaneId: this.activePaneId,
        panes: this.panes,
        kconfigAssignments: this.kconfigAssignments
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
      }
    } catch (e) {
      console.warn("Failed to load saved state:", e);
    }
  }

}

export const state = new GlobalState();
