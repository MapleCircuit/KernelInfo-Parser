/**
 * url_sync.js - Bi-directional URL synchronization.
 *
 * The hash is the address of what the active tab is showing: its kind and revision, and the
 * state inside it a reader would otherwise have to reproduce by hand — the file and line of a
 * code tab, the ARCH, filter and inspected symbol of a KConfig tab, the query, thread and
 * message of a mailing list tab, the query and commit of a commits tab, the revisions, chips,
 * filter and rename searches of a Compare tab, the section, search, developer filters and
 * inspected subsystem or developer of a Maintainers tab. Copying the URL
 * therefore reopens that same search and that same target (§5.26).
 *
 * Tab and revision changes are announced as `tabs:changed` / `version:changed`; a navigation
 * *inside* a tab is announced as `tab:state` (`state.updateTabState`), because redrawing the
 * pane would discard the view that reported it.
 */
import { state } from "./state.js";

/** Compare rename searches from `rn=<similarity>:<directory>` entries (applied in order; empty dir = whole tree). */
function parseRenameSearches(values) {
  const out = [];
  for (const value of values) {
    const sep = value.indexOf(":");
    const sim = parseInt(sep === -1 ? value : value.slice(0, sep), 10);
    if (!Number.isFinite(sim) || sim < 1 || sim > 100) continue;
    out.push({ sim, dir: sep === -1 ? "" : value.slice(sep + 1).replace(/^\/+|\/+$/g, "") });
  }
  return out.length ? out : null;
}

class UrlSynchronizer {
  constructor() {
    this.isUpdatingFromHash = false;
    this.isNavigatingHistory = false;
    this.justPoppedState = false;
    this.lastCommittedSnapshot = null;
    // Address held while the startup session picker is open (`deferred`). Adopting a session
    // replaces the panes and rewrites the hash, so a link opened in a new browser tab would
    // lose its target if it were applied before that choice is made (§5.26).
    this.deferred = false;
    this.deferredHash = null;

    // Listen to browser popstate (Back/Forward navigation)
    window.addEventListener("popstate", (e) => this.handlePopState(e));

    // Listen to hash changes (e.g. user editing address bar directly)
    window.addEventListener("hashchange", () => this.handleHashChange());

    // Listen to state changes
    state.subscribe("tabs:changed", () => this.updateHashFromState());
    state.subscribe("version:changed", () => this.updateHashFromState());
    state.subscribe("tab:state", (msg) => this.updateHashFromState(msg));
    // Whichever way a session is finally adopted, this window now has its tabs: the held
    // address is applied on top of them, so the tab a link asked for opens inside that session.
    state.subscribe("session:adopted", () => {
      if (this.deferred) this.init();
    });
  }

  /**
   * Keep this window's address aside until `init`, because the session picker is open and the
   * tabs this window will show are not loaded yet. The hash is neither read nor overwritten
   * while held, so the tab a link asked for survives the session being adopted.
   */
  defer() {
    this.deferredHash = window.location.hash.substring(1) || null;
    this.deferred = true;
  }

  init() {
    this.deferred = false;
    const hash = this.deferredHash || window.location.hash.substring(1);
    this.deferredHash = null;
    if (hash) {
      this.applyHash(hash);
    } else {
      this.updateHashFromState();
    }

    const initialSnapshot = this.createSnapshot(window.location.hash);
    if (initialSnapshot) {
      this.lastCommittedSnapshot = initialSnapshot;
      if (!history.state || !history.state.kipSnapshot) {
        history.replaceState(initialSnapshot, "", window.location.hash || "#");
      }
    }
  }

  handleHashChange() {
    // While held, a hash change only updates what `init` will apply
    if (this.deferred) {
      this.deferredHash = window.location.hash.substring(1) || null;
      return;
    }
    // If popstate already handled history restoration, skip redundant hashchange processing
    if (this.justPoppedState) {
      this.justPoppedState = false;
      return;
    }
    this.applyHash(window.location.hash.substring(1));
  }

  handlePopState(e) {
    if (this.deferred) {
      this.deferredHash = window.location.hash.substring(1) || null;
      return;
    }

    const targetSnapshot = e.state;
    if (!targetSnapshot || !targetSnapshot.kipSnapshot) {
      this.justPoppedState = true;
      const hash = window.location.hash.substring(1);
      if (hash) this.applyHash(hash);
      return;
    }

    this.isNavigatingHistory = true;
    this.justPoppedState = true;

    try {
      this.restoreSnapshot(targetSnapshot);
    } catch (err) {
      console.warn("Failed to restore history snapshot:", err);
      const hash = window.location.hash.substring(1);
      if (hash) this.applyHash(hash);
    } finally {
      this.isNavigatingHistory = false;
      this.lastCommittedSnapshot = targetSnapshot;
    }
  }

  /**
   * Reconcile current workspace tabs with a target history snapshot:
   * - Closes tabs created after this snapshot (Option B: redacting newly opened tabs).
   * - Reopens closed tabs recorded in this snapshot.
   * - Rewinds in-tab replacement properties (e.g. Pahole struct drilldown, Callgraph hop).
   * - Restores active pane and tab selections.
   */
  restoreSnapshot(targetSnapshot) {
    if (!targetSnapshot) return;

    const targetOpenIds = new Set(targetSnapshot.openTabIds || []);

    // 1. Redact / close tabs that were created after this snapshot
    for (const pane of state.panes) {
      const tabsToClose = pane.tabs.filter((t) => !targetOpenIds.has(t.id));
      for (const t of tabsToClose) {
        state.closeTab(t.id, pane.id);
      }
    }

    // 2. Reopen tabs that existed in this snapshot, or restore in-tab properties
    const NAV_PROPS = [
      "title", "path", "cursorLine", "structName", "functionName",
      "query", "filter", "symbol", "arch", "configName", "commitId",
      "listCollapsed", "threadId", "messageId", "listMode", "section",
      "secId", "person", "role", "sort", "versionA", "versionB", "mode",
      "statuses", "kconfigQuery", "kconfigStatuses", "renameSearches"
    ];

    for (const savedTab of (targetSnapshot.tabs || [])) {
      const liveTab = state.findTab(savedTab.id);
      if (!liveTab) {
        const pane = state.getPane(savedTab.paneId) || state.getActivePane();
        if (pane) {
          pane.tabs.push({ ...savedTab });
        }
      } else {
        for (const prop of NAV_PROPS) {
          if (savedTab[prop] !== undefined) {
            liveTab[prop] = savedTab[prop];
          } else {
            delete liveTab[prop];
          }
        }
        if (savedTab.title) liveTab.title = savedTab.title;
        if (savedTab.version) liveTab.version = savedTab.version;
      }
    }

    // 3. Restore active pane and active tab per pane
    if (targetSnapshot.activePaneId && state.panes.some((p) => p.id === targetSnapshot.activePaneId)) {
      state.activePaneId = targetSnapshot.activePaneId;
    }

    if (targetSnapshot.activeTabIdPerPane) {
      for (const pane of state.panes) {
        const activeId = targetSnapshot.activeTabIdPerPane[pane.id];
        if (activeId && pane.tabs.some((t) => t.id === activeId)) {
          pane.activeTabId = activeId;
        } else if (pane.tabs.length > 0) {
          pane.activeTabId = pane.tabs[0].id;
        } else {
          pane.activeTabId = null;
        }
      }
    } else if (targetSnapshot.activeTabId) {
      const activePane = state.getActivePane();
      if (activePane) activePane.activeTabId = targetSnapshot.activeTabId;
    }

    // 4. Sync version if active tab version differs
    const activeTab = state.getActiveTab();
    if (activeTab && activeTab.version && activeTab.version !== state.currentVersion) {
      state.syncVersionFromTab(activeTab.version);
    }

    // 5. Emit tabs:changed and save state to re-render DOM
    state.emit("tabs:changed", {
      paneId: state.activePaneId,
      activeTabId: state.getActiveTab()?.id
    });
    state.saveState();
  }

  /**
   * Primary navigation key distinguishing discrete targets of a tab.
   * Changing this key triggers a history push; changing non-nav keys (e.g. query typing) replaces.
   */
  getNavKey(tab) {
    if (!tab) return "";
    const v = tab.version || state.currentVersion || "";
    switch (tab.type) {
      case "code":
        return `${v}:${tab.type}:${tab.path || ""}`;
      case "pahole":
        return `${v}:${tab.type}:${tab.structName || ""}`;
      case "callgraph":
        return `${v}:${tab.type}:${tab.functionName || ""}`;
      case "explorer":
        return `${v}:${tab.type}:${tab.path || ""}`;
      case "commits":
        return `${v}:${tab.type}:${tab.commitId || ""}`;
      case "mail":
        return `${v}:${tab.type}:${tab.threadId || 0}:${tab.messageId || ""}`;
      case "kconfig":
        return `${v}:${tab.type}:${tab.arch || ""}:${tab.configName || ""}:${tab.symbol || ""}`;
      case "maintainers":
        return `${v}:${tab.type}:${tab.section || ""}:${tab.secId || 0}:${tab.person || ""}`;
      case "diff":
        return `${v}:${tab.type}:${tab.versionA || ""}:${tab.versionB || ""}:${tab.mode || "files"}`;
      case "nodemap":
        return `${v}:${tab.type}`;
      case "treemap":
        return `${v}:${tab.type}:${tab.path || ""}`;
      default:
        return `${v}:${tab.type}:${tab.title || ""}`;
    }
  }

  /** Lightweight snapshot of workspace tab state serializable into history.state. */
  createSnapshot(hash = window.location.hash) {
    const activeTab = state.getActiveTab();
    if (!activeTab) return null;

    const activeTabIdPerPane = {};
    for (const pane of state.panes) {
      activeTabIdPerPane[pane.id] = pane.activeTabId;
    }

    const openTabIds = [];
    const tabs = [];

    for (const pane of state.panes) {
      for (const t of pane.tabs) {
        openTabIds.push(t.id);
        tabs.push({
          id: t.id,
          paneId: pane.id,
          type: t.type,
          title: t.title,
          version: t.version,
          path: t.path,
          cursorLine: t.cursorLine,
          structName: t.structName,
          functionName: t.functionName,
          query: t.query,
          filter: t.filter,
          symbol: t.symbol,
          arch: t.arch,
          configName: t.configName,
          commitId: t.commitId,
          listCollapsed: t.listCollapsed,
          threadId: t.threadId,
          messageId: t.messageId,
          listMode: t.listMode,
          section: t.section,
          secId: t.secId,
          person: t.person,
          role: t.role,
          sort: t.sort,
          versionA: t.versionA,
          versionB: t.versionB,
          mode: t.mode,
          statuses: t.statuses,
          kconfigQuery: t.kconfigQuery,
          kconfigStatuses: t.kconfigStatuses,
          renameSearches: t.renameSearches ? JSON.parse(JSON.stringify(t.renameSearches)) : undefined
        });
      }
    }

    return {
      kipSnapshot: true,
      activePaneId: state.activePaneId,
      activeTabId: activeTab.id,
      activeTabIdPerPane,
      openTabIds,
      tabs,
      hash,
      navKey: this.getNavKey(activeTab)
    };
  }

  applyHash(hash) {
    if (!hash) return;

    this.isUpdatingFromHash = true;
    try {
      const params = new URLSearchParams(hash);
      const v = params.get("v");
      const tabType = params.get("tab") || "code";
      const file = params.get("file");
      const line = parseInt(params.get("line") || "1", 10);
      const arch = params.get("arch") || "x86_64";
      const dir = params.get("dir") || "";
      const cfg = params.get("cfg") || ""; // named copy of that ARCH's configuration, if any
      const query = params.get("q") || ""; // search typed in the tab's own filter box

      // Restoring a revision from the hash selects it in the header; the tab opened below carries it
      if (v) state.setSelectedVersion(v);

      if (tabType === "code" && file) {
        // "⇄ Diff" of the file (§5.21): dv/dp = the other file, ds=1 swapped sides, dm=split layout.
        // A link without dv addresses the plain file, so a reused tab drops its diff.
        const dv = params.get("dv") || "";
        const diffWith = dv ? { version: dv, path: params.get("dp") || file, swap: params.get("ds") === "1" } : null;
        state.openTab({
          type: "code",
          title: file.split("/").pop(),
          path: file,
          cursorLine: line,
          version: v || state.currentVersion,
          diffWith,
          diffLayout: dv && params.get("dm") === "split" ? "split" : "",
        });
      } else if (tabType === "kconfig") {
        state.openTab({
          type: "kconfig",
          title: `KConfig (${arch})${cfg ? ` · ${cfg}` : ""}`,
          arch,
          configName: cfg,
          query,
          symbol: params.get("sym") || "",
          version: v || state.currentVersion
        });
      } else if (tabType === "explorer") {
        state.openTab({
          type: "explorer",
          title: dir ? dir.split("/").pop() : `Explorer ${v || state.currentVersion}`,
          path: dir,
          version: v || state.currentVersion
        });
      } else if (tabType === "nodemap") {
        state.openTab({
          type: "nodemap",
          title: "NodeMap Canvas",
          version: v || state.currentVersion
        });
      } else if (tabType === "mail") {
        state.openTab({
          type: "mail",
          title: "Mailing List",
          threadId: parseInt(params.get("tid") || "0", 10),
          messageId: params.get("mid") || "",
          query,
          filter: params.get("f") || "all",
          listMode: params.get("mode") === "tree" ? "tree" : "search",
          version: v || state.currentVersion
        });
      } else if (tabType === "diff") {
        const kconfigMode = params.get("mode") === "kconfig";
        state.openTab({
          type: "diff",
          title: "Compare",
          // Without a/b the tab keeps its revisions (or picks its defaults)
          versionA: params.get("a") || undefined,
          versionB: params.get("b") || undefined,
          mode: kconfigMode ? "kconfig" : "files",
          statuses: kconfigMode ? undefined : (params.get("st") || ""),
          query: kconfigMode ? undefined : query,
          kconfigQuery: kconfigMode ? query : undefined,
          kconfigStatuses: kconfigMode ? (params.get("st") || "") : undefined,
          renameSearches: kconfigMode ? undefined : parseRenameSearches(params.getAll("rn")),
          version: v || state.currentVersion
        });
      } else if (tabType === "commits") {
        state.openTab({
          type: "commits",
          title: "Commits",
          commitId: params.get("cid") || "",
          listCollapsed: params.get("lc") === "1",
          query,
          version: v || state.currentVersion
        });
      } else if (tabType === "maintainers") {
        // Every field is given (empty when absent), so a reused tab takes exactly what the link addresses
        const secId = parseInt(params.get("sec") || "0", 10) || 0;
        state.openTab({
          type: "maintainers",
          title: "Maintainers",
          section: params.get("view") || "",
          secId,
          person: secId ? "" : (params.get("person") || ""),
          role: params.get("role") || "",
          sort: params.get("sort") === "alpha" ? "alpha" : "",
          query,
          version: v || state.currentVersion
        });
      } else if (tabType === "pahole") {
        const structName = params.get("struct") || "task_struct";
        state.openTab({
          type: "pahole",
          title: `struct ${structName}`,
          structName,
          version: v || state.currentVersion
        });
      } else if (tabType === "callgraph") {
        const funcName = params.get("fn") || "schedule";
        state.openTab({
          type: "callgraph",
          title: `Callgraph: ${funcName}`,
          functionName: funcName,
          version: v || state.currentVersion
        });
      } else if (tabType === "treemap") {
        state.openTab({
          type: "treemap",
          title: "Architecture Treemap",
          version: v || state.currentVersion
        });
      }
    } finally {
      this.isUpdatingFromHash = false;
    }
    this.updateHashFromState();
  }

  updateHashFromState(options = {}) {
    if (this.isUpdatingFromHash || this.isNavigatingHistory || this.deferred) return;

    const activeTab = state.getActiveTab();
    if (!activeTab) return;

    const params = new URLSearchParams();
    params.set("v", activeTab.version || state.currentVersion);
    params.set("tab", activeTab.type);

    if (activeTab.type === "code" && activeTab.path) {
      params.set("file", activeTab.path);
      if (activeTab.cursorLine && activeTab.cursorLine > 1) {
        params.set("line", activeTab.cursorLine);
      }
      if (activeTab.diffWith?.version) {
        params.set("dv", activeTab.diffWith.version);
        if (activeTab.diffWith.path && activeTab.diffWith.path !== activeTab.path) params.set("dp", activeTab.diffWith.path);
        if (activeTab.diffWith.swap) params.set("ds", "1");
        if (activeTab.diffLayout === "split") params.set("dm", "split");
      }
    } else if (activeTab.type === "explorer") {
      if (activeTab.path) params.set("dir", activeTab.path);
    } else if (activeTab.type === "kconfig" && activeTab.arch) {
      params.set("arch", activeTab.arch);
      if (activeTab.configName) params.set("cfg", activeTab.configName);
      if (activeTab.symbol) params.set("sym", activeTab.symbol);
      if (activeTab.query) params.set("q", activeTab.query);
    } else if (activeTab.type === "mail") {
      if (activeTab.threadId) params.set("tid", activeTab.threadId);
      if (activeTab.messageId) params.set("mid", activeTab.messageId);
      if (activeTab.query) params.set("q", activeTab.query);
      if (activeTab.filter && activeTab.filter !== "all") params.set("f", activeTab.filter);
      if (activeTab.listMode === "tree") params.set("mode", "tree");
    } else if (activeTab.type === "diff") {
      if (activeTab.versionA) params.set("a", activeTab.versionA);
      if (activeTab.versionB) params.set("b", activeTab.versionB);
      if (activeTab.mode === "kconfig") {
        params.set("mode", "kconfig");
        if (activeTab.kconfigStatuses) params.set("st", activeTab.kconfigStatuses);
        if (activeTab.kconfigQuery) params.set("q", activeTab.kconfigQuery);
      } else {
        if (activeTab.statuses) params.set("st", activeTab.statuses);
        if (activeTab.query) params.set("q", activeTab.query);
        for (const r of activeTab.renameSearches || []) params.append("rn", `${r.sim}:${r.dir || ""}`);
      }
    } else if (activeTab.type === "commits") {
      if (activeTab.commitId) params.set("cid", activeTab.commitId);
      if (activeTab.query) params.set("q", activeTab.query);
      if (activeTab.listCollapsed) params.set("lc", "1");
    } else if (activeTab.type === "maintainers") {
      if (activeTab.section && activeTab.section !== "subsystems") params.set("view", activeTab.section);
      if (activeTab.secId) params.set("sec", activeTab.secId);
      else if (activeTab.person) params.set("person", activeTab.person);
      if (activeTab.query) params.set("q", activeTab.query);
      if (activeTab.role && activeTab.role !== "all") params.set("role", activeTab.role);
      if (activeTab.sort === "alpha") params.set("sort", "alpha");
    } else if (activeTab.type === "pahole") {
      if (activeTab.structName) params.set("struct", activeTab.structName);
    } else if (activeTab.type === "callgraph") {
      if (activeTab.functionName) params.set("fn", activeTab.functionName);
    }

    const newHash = "#" + params.toString();
    const currentNavKey = this.getNavKey(activeTab);
    const currentTabId = activeTab.id;

    // Check if this transition represents a discrete navigation that should push history
    let shouldPush = false;
    if (options && options.replace) {
      shouldPush = false;
    } else if (!this.lastCommittedSnapshot) {
      shouldPush = false;
    } else {
      const lastTabId = this.lastCommittedSnapshot.activeTabId;
      const lastNavKey = this.lastCommittedSnapshot.navKey;

      if (currentTabId !== lastTabId) {
        // Tab switch, new tab opened, or tab closed
        shouldPush = true;
      } else if (currentNavKey !== lastNavKey) {
        // Discrete in-tab navigation (e.g. Pahole drilldown, Callgraph hop, directory navigation).
        // If the previous snapshot for this tab was still uninitialized (e.g. Compare opened before
        // default versions loaded), completing initial hydration must replace rather than push.
        const prevTab = this.lastCommittedSnapshot.tabs?.find((t) => t.id === currentTabId);
        if (activeTab.type === "diff" && (!prevTab?.versionA || !prevTab?.versionB)) {
          shouldPush = false;
        } else if (activeTab.type === "pahole" && !prevTab?.structName) {
          shouldPush = false;
        } else if (activeTab.type === "callgraph" && !prevTab?.functionName) {
          shouldPush = false;
        } else {
          shouldPush = true;
        }
      }
    }

    const snapshot = this.createSnapshot(newHash);
    this.lastCommittedSnapshot = snapshot;

    if (shouldPush) {
      history.pushState(snapshot, "", newHash);
    } else {
      if (window.location.hash !== newHash || !history.state || !history.state.kipSnapshot) {
        history.replaceState(snapshot, "", newHash);
      }
    }
  }
}

export const urlSync = new UrlSynchronizer();
