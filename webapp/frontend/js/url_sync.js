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
 * Every address names the repository of its tab (`repo=`, always written): one session holds tabs of
 * several projects, and a link into another project opens its tab next to the open ones (§5.26).
 *
 * Tab and revision changes are announced as `tabs:changed` / `version:changed`; a navigation
 * *inside* a tab is announced as `tab:state` (`state.updateTabState`), because redrawing the
 * pane would discard the view that reported it.
 */
import { state } from "./state.js";
import { hashRepo, routeKeys, tabRoute } from "./tab_routes.js";

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
    // `repo=` of an address naming another project than the active tab's (§5.26): app.js says which
    // repository the tab opens in (that one when configured, else "" = the active tab's) and its default
    // revision for an address without `v=`. No tab is closed and nothing is asked.
    this.repoHandler = null;

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

  /**
   * Install the project resolver: (repo) -> {repo, version}: the repository a link naming `repo` opens in
   * ("" = the active tab's, when `repo` is not configured here) and the revision used when the link has no `v=`.
   */
  setRepoHandler(fn) {
    this.repoHandler = typeof fn === "function" ? fn : null;
  }

  /** Apply an address (or, without one, write the active tab's): a tab of the project its `repo=` names. */
  applyAddress(hash, done = null) {
    if (hash) this.applyHash(hash);
    else this.updateHashFromState();
    done?.();
  }

  /** Repository and fallback revision of an address: {repo ("" = the active tab's), version}. */
  resolveRepo(params) {
    const named = hashRepo(params);
    if (!named || named === state.activeRepo()) return { repo: named, version: "" };
    let answer = { repo: named, version: "" };
    try {
      if (this.repoHandler) answer = this.repoHandler(named) || answer;
    } catch (err) {
      console.warn(`Could not resolve project ${named}:`, err);
    }
    return { repo: typeof answer.repo === "string" ? answer.repo : "", version: typeof answer.version === "string" ? answer.version : "" };
  }

  init() {
    this.deferred = false;
    const hash = this.deferredHash || window.location.hash.substring(1);
    this.deferredHash = null;
    this.applyAddress(hash, () => this.commitInitialSnapshot());
  }

  commitInitialSnapshot() {
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
    this.applyAddress(window.location.hash.substring(1));
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
      "title", "path", "cursorLine", "lineEnd", "blame", "blameOpts", "structName", "functionName",
      "query", "filter", "symbol", "arch", "configName", "commitId",
      "listCollapsed", "threadId", "messageId", "listMode", "section",
      "secId", "person", "role", "sort", "versionA", "versionB", "mode",
      "statuses", "kconfigQuery", "kconfigStatuses", "renameSearches", "apiKinds", "apiStatuses", "apiQuery", "apiArch",
      ...routeKeys()
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
        if (savedTab.repo) liveTab.repo = savedTab.repo;
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
    const v = `${state.repoOf(tab)}@${tab.version || state.currentVersion || ""}`;
    const route = tabRoute(tab.type);
    if (route?.navKey) return `${v}:${tab.type}:${route.navKey(tab)}`;
    switch (tab.type) {
      case "code":
        return `${v}:${tab.type}:${tab.path || ""}`;
      case "pahole":
        return `${v}:${tab.type}:${tab.structName || ""}:${tab.cmp || ""}:${tab.hist ? 1 : 0}`;
      case "callgraph":
        return `${v}:${tab.type}:${tab.functionName || ""}:${tab.pathTo || ""}`;
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
          repo: t.repo,
          version: t.version,
          path: t.path,
          cursorLine: t.cursorLine,
          lineEnd: t.lineEnd,
          blame: t.blame,
          blameOpts: t.blameOpts,
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
          apiKinds: t.apiKinds,
          apiStatuses: t.apiStatuses,
          apiQuery: t.apiQuery,
          apiArch: t.apiArch,
          manStatuses: t.manStatuses,
          manSection: t.manSection,
          manQuery: t.manQuery,
          renameSearches: t.renameSearches ? JSON.parse(JSON.stringify(t.renameSearches)) : undefined,
          ...Object.fromEntries((tabRoute(t.type)?.keys || []).filter((k) => t[k] !== undefined)
            .map((k) => [k, typeof t[k] === "object" && t[k] !== null ? JSON.parse(JSON.stringify(t[k])) : t[k]]))
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
      // line=<a> or line=<a>-<b> (a selected range, G06); a reversed range is normalized
      const lineSpec = /^(\d+)(?:-(\d+))?$/.exec(params.get("line") || "");
      let line = lineSpec ? parseInt(lineSpec[1], 10) : 1;
      let lineEnd = lineSpec && lineSpec[2] ? parseInt(lineSpec[2], 10) : 0;
      if (lineEnd && lineEnd < line) [line, lineEnd] = [lineEnd, line];
      if (lineEnd === line) lineEnd = 0;
      const arch = params.get("arch") || "x86_64";
      const dir = params.get("dir") || "";
      const cfg = params.get("cfg") || ""; // named copy of that ARCH's configuration, if any
      const query = params.get("q") || ""; // search typed in the tab's own filter box
      // The tab opens in the project the address names, next to the open tabs (§5.26)
      const { repo, version: repoDefault } = this.resolveRepo(params);
      const foreign = Boolean(repo) && repo !== state.activeRepo();
      const ver = v || (foreign ? repoDefault || "HEAD" : state.currentVersion);
      const open = (data) => state.openTab({ ...(repo ? { repo } : {}), ...data });

      // Restoring a revision from the hash selects it in the header; the tab opened below carries it
      if (v) state.setSelectedVersion(v);

      const route = tabRoute(tabType);
      if (route?.fromHash) {
        const fields = route.fromHash(params, ver);
        open({ type: tabType, ...fields, version: fields.version || ver });
      } else if (tabType === "code" && file) {
        // "⇄ Diff" of the file (§5.21): dv/dp = the other file, ds=1 swapped sides, dm=split layout.
        // A link without dv addresses the plain file, so a reused tab drops its diff.
        const dv = params.get("dv") || "";
        // dk=patch&dmid=<message ids>: the file against what those archive patches make of it (P1)
        const dmid = params.get("dk") === "patch" ? (params.get("dmid") || "").split(",").map((m) => m.trim()).filter(Boolean).slice(0, 50) : [];
        const diffWith = dmid.length ? { kind: "patch", mids: dmid, revision: ver, path: file, applyId: "" }
          : dv ? { version: dv, path: params.get("dp") || file, swap: params.get("ds") === "1" } : null;
        open({
          type: "code",
          title: file.split("/").pop(),
          path: file,
          cursorLine: line,
          lineEnd,
          version: ver,
          diffWith,
          diffLayout: (dv || dmid.length) && params.get("dm") === "split" ? "split" : "",
          // Blame shown, and its options (H9, §5.21): bl=1, bo=<m|c|w>
          blame: params.get("bl") === "1",
          blameOpts: params.get("bl") === "1" ? (params.get("bo") || "").replace(/[^mcw]/g, "").slice(0, 3) : "",
        });
      } else if (tabType === "kconfig") {
        open({
          type: "kconfig",
          title: `KConfig (${arch})${cfg ? ` · ${cfg}` : ""}`,
          arch,
          configName: cfg,
          query,
          symbol: params.get("sym") || "",
          listCollapsed: params.get("lc") === "1",
          version: ver
        });
      } else if (tabType === "explorer") {
        open({
          type: "explorer",
          title: dir ? dir.split("/").pop() : `Explorer ${ver}`,
          path: dir,
          version: ver
        });
      } else if (tabType === "nodemap") {
        open({
          type: "nodemap",
          title: "NodeMap Canvas",
          version: ver
        });
      } else if (tabType === "mail") {
        open({
          type: "mail",
          title: "Mailing List",
          threadId: parseInt(params.get("tid") || "0", 10),
          messageId: params.get("mid") || "",
          query,
          filter: params.get("f") || "all",
          listMode: params.get("mode") === "tree" ? "tree" : "search",
          listCollapsed: params.get("lc") === "1",
          version: ver
        });
      } else if (tabType === "diff" && params.get("mode") === "api") {
        // API mode (A6): k = kinds, st = statuses, q = filter, arch
        open({
          type: "diff", title: "Compare",
          versionA: params.get("a") || undefined, versionB: params.get("b") || undefined, mode: "api",
          apiKinds: params.get("k") || "", apiStatuses: params.get("st") || "", apiQuery: query, apiArch: params.get("arch") || "",
          version: ver,
        });
      } else if (tabType === "diff" && params.get("mode") === "man") {
        // Man pages mode (§5.11): st = statuses, sec = section, q = filter
        open({
          type: "diff", title: "Compare",
          versionA: params.get("a") || undefined, versionB: params.get("b") || undefined, mode: "man",
          manStatuses: params.get("st") || "", manSection: params.get("sec") || "", manQuery: query,
          version: ver,
        });
      } else if (tabType === "diff") {
        const rawMode = params.get("mode") || "";
        const kconfigMode = rawMode === "kconfig";
        open({
          type: "diff",
          title: "Compare",
          // Without a/b the tab keeps its revisions (or picks its defaults)
          versionA: params.get("a") || undefined,
          versionB: params.get("b") || undefined,
          mode: kconfigMode ? "kconfig" : (rawMode === "files" ? "files" : ""),
          statuses: kconfigMode ? undefined : (params.get("st") || ""),
          query: kconfigMode ? undefined : query,
          kconfigQuery: kconfigMode ? query : undefined,
          kconfigStatuses: kconfigMode ? (params.get("st") || "") : undefined,
          renameSearches: kconfigMode ? undefined : parseRenameSearches(params.getAll("rn")),
          version: ver
        });
      } else if (tabType === "commits") {
        open({
          type: "commits",
          title: "Commits",
          commitId: params.get("cid") || "",
          listCollapsed: params.get("lc") === "1",
          query,
          version: ver
        });
      } else if (tabType === "maintainers") {
        // Every field is given (empty when absent), so a reused tab takes exactly what the link addresses
        const secId = parseInt(params.get("sec") || "0", 10) || 0;
        open({
          type: "maintainers",
          title: "Maintainers",
          section: params.get("view") || "",
          secId,
          person: secId ? "" : (params.get("person") || ""),
          role: params.get("role") || "",
          sort: params.get("sort") === "alpha" ? "alpha" : "",
          query,
          listCollapsed: params.get("lc") === "1",
          version: ver
        });
      } else if (tabType === "pahole") {
        const structName = params.get("struct") || "task_struct";
        open({
          type: "pahole",
          title: `struct ${structName}`,
          structName,
          // Layout over time (N3): cmp=<rev> compares with another revision, hist=1 shows the size history
          cmp: params.get("cmp") || "",
          hist: params.get("hist") === "1",
          version: ver
        });
      } else if (tabType === "callgraph") {
        const funcName = params.get("fn") || "schedule";
        open({
          type: "callgraph",
          title: `Callgraph: ${funcName}`,
          functionName: funcName,
          // Call path search target (N2): navigation, like the function
          pathTo: params.get("to") || "",
          version: ver
        });
      } else if (tabType === "treemap") {
        open({
          type: "treemap",
          title: "Architecture Treemap",
          version: ver
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
    // The project the tab belongs to, always: a shared link opens in it (§5.26)
    const repo = state.repoOf(activeTab);
    if (repo) params.set("repo", repo);
    params.set("v", activeTab.version || state.currentVersion);
    params.set("tab", activeTab.type);

    if (activeTab.type === "code" && activeTab.path) {
      params.set("file", activeTab.path);
      if (activeTab.lineEnd && activeTab.lineEnd > (activeTab.cursorLine || 1)) {
        params.set("line", `${activeTab.cursorLine || 1}-${activeTab.lineEnd}`);
      } else if (activeTab.cursorLine && activeTab.cursorLine > 1) {
        params.set("line", activeTab.cursorLine);
      }
      if (activeTab.diffWith?.kind === "patch") {
        params.set("dk", "patch");
        params.set("dmid", (activeTab.diffWith.mids || []).join(","));
        if (activeTab.diffLayout === "split") params.set("dm", "split");
      } else if (activeTab.diffWith?.version) {
        params.set("dv", activeTab.diffWith.version);
        if (activeTab.diffWith.path && activeTab.diffWith.path !== activeTab.path) params.set("dp", activeTab.diffWith.path);
        if (activeTab.diffWith.swap) params.set("ds", "1");
        if (activeTab.diffLayout === "split") params.set("dm", "split");
      }
      if (activeTab.blame) {
        params.set("bl", "1");
        if (activeTab.blameOpts) params.set("bo", activeTab.blameOpts);
      }
    } else if (activeTab.type === "explorer") {
      if (activeTab.path) params.set("dir", activeTab.path);
    } else if (activeTab.type === "kconfig" && activeTab.arch) {
      params.set("arch", activeTab.arch);
      if (activeTab.configName) params.set("cfg", activeTab.configName);
      if (activeTab.symbol) params.set("sym", activeTab.symbol);
      if (activeTab.query) params.set("q", activeTab.query);
      if (activeTab.listCollapsed) params.set("lc", "1");
    } else if (activeTab.type === "mail") {
      if (activeTab.threadId) params.set("tid", activeTab.threadId);
      if (activeTab.messageId) params.set("mid", activeTab.messageId);
      if (activeTab.query) params.set("q", activeTab.query);
      if (activeTab.filter && activeTab.filter !== "all") params.set("f", activeTab.filter);
      if (activeTab.listMode === "tree") params.set("mode", "tree");
      if (activeTab.listCollapsed) params.set("lc", "1");
    } else if (activeTab.type === "diff") {
      if (activeTab.versionA) params.set("a", activeTab.versionA);
      if (activeTab.versionB) params.set("b", activeTab.versionB);
      if (activeTab.mode === "api") {
        params.set("mode", "api");
        if (activeTab.apiKinds) params.set("k", activeTab.apiKinds);
        if (activeTab.apiStatuses) params.set("st", activeTab.apiStatuses);
        if (activeTab.apiQuery) params.set("q", activeTab.apiQuery);
        if (activeTab.apiArch) params.set("arch", activeTab.apiArch);
      } else if (activeTab.mode === "kconfig") {
        params.set("mode", "kconfig");
        if (activeTab.kconfigStatuses) params.set("st", activeTab.kconfigStatuses);
        if (activeTab.kconfigQuery) params.set("q", activeTab.kconfigQuery);
      } else if (activeTab.mode === "man") {
        params.set("mode", "man");
        if (activeTab.manStatuses) params.set("st", activeTab.manStatuses);
        if (activeTab.manSection) params.set("sec", activeTab.manSection);
        if (activeTab.manQuery) params.set("q", activeTab.manQuery);
      } else {
        if (activeTab.mode === "files") params.set("mode", "files");
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
      if (activeTab.listCollapsed) params.set("lc", "1");
    } else if (tabRoute(activeTab.type)?.toHash) {
      tabRoute(activeTab.type).toHash(activeTab, params);
    } else if (activeTab.type === "pahole") {
      if (activeTab.structName) params.set("struct", activeTab.structName);
      if (activeTab.cmp) params.set("cmp", activeTab.cmp);
      if (activeTab.hist) params.set("hist", "1");
    } else if (activeTab.type === "callgraph") {
      if (activeTab.functionName) params.set("fn", activeTab.functionName);
      if (activeTab.pathTo) params.set("to", activeTab.pathTo);
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
