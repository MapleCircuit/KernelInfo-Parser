/**
 * url_sync.js - Bi-directional URL synchronization.
 * Reflects current tab, file, version, and line in URL hash.
 */
import { state } from "./state.js";

class UrlSynchronizer {
  constructor() {
    this.isUpdatingFromHash = false;

    // Listen to hash changes (e.g. browser back/forward or user paste)
    window.addEventListener("hashchange", () => this.handleHashChange());

    // Listen to state changes
    state.subscribe("tabs:changed", () => this.updateHashFromState());
    state.subscribe("version:changed", () => this.updateHashFromState());
  }

  init() {
    if (window.location.hash) {
      this.handleHashChange();
    } else {
      this.updateHashFromState();
    }
  }

  handleHashChange() {
    const hash = window.location.hash.substring(1);
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
          query: params.get("q") || "",
          listMode: params.get("mode") === "tree" ? "tree" : "search",
          version: v || state.currentVersion
        });
      }
    } finally {
      this.isUpdatingFromHash = false;
    }
  }

  updateHashFromState() {
    if (this.isUpdatingFromHash) return;

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
    } else if (activeTab.type === "mail") {
      if (activeTab.threadId) params.set("tid", activeTab.threadId);
      if (activeTab.messageId) params.set("mid", activeTab.messageId);
      if (activeTab.query) params.set("q", activeTab.query);
      if (activeTab.listMode === "tree") params.set("mode", "tree");
    }

    const newHash = "#" + params.toString();
    if (window.location.hash !== newHash) {
      history.replaceState(null, "", newHash);
    }
  }
}

export const urlSync = new UrlSynchronizer();
