/**
 * url_sync.js - Bi-directional URL synchronization.
 *
 * The hash is the address of what the active tab is showing: its kind and revision, and the
 * state inside it a reader would otherwise have to reproduce by hand — the file and line of a
 * code tab, the ARCH, filter and inspected symbol of a KConfig tab, the query, thread and
 * message of a mailing list tab, the query and commit of a commits tab. Copying the URL
 * therefore reopens that same search and that same target (§5.26).
 *
 * Tab and revision changes are announced as `tabs:changed` / `version:changed`; a navigation
 * *inside* a tab is announced as `tab:state` (`state.updateTabState`), because redrawing the
 * pane would discard the view that reported it.
 */
import { state } from "./state.js";

class UrlSynchronizer {
  constructor() {
    this.isUpdatingFromHash = false;
    // Address held while the startup session picker is open (`deferred`). Adopting a session
    // replaces the panes and rewrites the hash, so a link opened in a new browser tab would
    // lose its target if it were applied before that choice is made (§5.26).
    this.deferred = false;
    this.deferredHash = null;

    // Listen to hash changes (e.g. browser back/forward or user paste)
    window.addEventListener("hashchange", () => this.handleHashChange());

    // Listen to state changes
    state.subscribe("tabs:changed", () => this.updateHashFromState());
    state.subscribe("version:changed", () => this.updateHashFromState());
    state.subscribe("tab:state", () => this.updateHashFromState());
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
  }

  handleHashChange() {
    // While held, a hash change only updates what `init` will apply
    if (this.deferred) {
      this.deferredHash = window.location.hash.substring(1) || null;
      return;
    }
    this.applyHash(window.location.hash.substring(1));
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
        state.openTab({
          type: "code",
          title: file.split("/").pop(),
          path: file,
          cursorLine: line,
          version: v || state.currentVersion
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
      } else if (tabType === "commits") {
        state.openTab({
          type: "commits",
          title: "Commits",
          commitId: params.get("cid") || "",
          query,
          version: v || state.currentVersion
        });
      }
    } finally {
      this.isUpdatingFromHash = false;
    }
  }

  updateHashFromState() {
    if (this.isUpdatingFromHash || this.deferred) return;

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
    } else if (activeTab.type === "commits") {
      if (activeTab.commitId) params.set("cid", activeTab.commitId);
      if (activeTab.query) params.set("q", activeTab.query);
    }

    const newHash = "#" + params.toString();
    if (window.location.hash !== newHash) {
      history.replaceState(null, "", newHash);
    }
  }
}

export const urlSync = new UrlSynchronizer();
