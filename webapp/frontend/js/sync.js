/**
 * sync.js - Keeps the client in step with the server (tokens of GET /api/sync, see
 * webapp/backend/services/sync_service.py).
 *
 * - db: a rebuilt database clears every cached API response.
 * - parsed / git: tokens of one repository (each response's X-KIP-Sync header is compared with the tokens
 *   of the repository that request named, so tabs of several projects never mistake each other's tokens
 *   for a change): that repository's version and branch lists are refetched; each version carries a stamp
 *   (commit + parse completion) that is part of its cache keys (api._vk, stamps kept per repository), and
 *   that repository's entries of versions whose stamp changed are deleted.
 * - projects: the repository list is refetched (and the selectors re-rendered) only when it changed.
 * - frontend: compared on every sync (page open, opening a new tab, restoring a session, and periodic
 *   60s sync). When a newer build than the running page (window.KIP_BUILD, see boot.js) is detected,
 *   it is announced and the page reloads after 5 seconds ("Later" postpones it for the session).
 * - checkNow() (Settings "Check for Updates"): that same sync, run at once; when it finds an update (a newer
 *   frontend build, a changed token or changed versions) the page restarts right away (restart()) instead of
 *   re-rendering in place or announcing the build.
 */
import { api } from "./api.js";
import { localDB } from "./db.js";
import { state } from "./state.js";

const RELOAD_DELAY_S = 5;
const LAST_FRONTEND_CHECK_KEY = "kip-last-frontend-check";
// What a manual check found, left by restart() for the page it reloads (sessionStorage: this browser tab only)
const RESTART_NOTE_KEY = "kip-restart-note";
// Cached API data older than this is dropped (checked at most daily, when the page opens)
const CACHE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
const LAST_PRUNE_KEY = "kip-last-cache-prune";
// Bumped when cache keys or cached responses change shape: entries of the previous layout are dropped once
// (3: symbol detail, file, callgraph, Pahole, blame, version diff and maintainers responses changed)
const CACHE_LAYOUT = 3;

/** IndexedDB `meta` key of a repository's version stamps. */
const stampsKey = (repo) => `version_stamps::${repo || ""}`;

class SyncManager {
  constructor() {
    // Server-wide tokens {db, frontend, projects}; `repos` holds {parsed, git} per repository
    this.tokens = {};
    this._stampsLoaded = new Set();
    this.queue = Promise.resolve();
    this.reloadTimer = null;
    this.postponedBuild = null;
    this._lastSyncTime = 0;
    this._inFlightSync = null;

    // Synchronize on opening a new tab and restoring a session
    state.subscribe("tab:opened", () => this.sync({ notify: true }));
    state.subscribe("session:adopted", () => this.sync({ notify: true }));
    state.subscribe("session:restored", () => this.sync({ notify: true }));
  }

  /** Frontend build of the running page. */
  get build() {
    return window.KIP_BUILD || document.querySelector('meta[name="kip-build"]')?.content || "";
  }

  /** Page open: restore what was seen last, compare with the server, then follow response headers. */
  async start() {
    if ((await localDB.get("meta", "cache_layout")) !== CACHE_LAYOUT) {
      await localDB.clearDataStores();
      if ("caches" in window) {
        await caches.delete("kip-api");
      }
      await localDB.set("meta", "cache_layout", CACHE_LAYOUT);
    }
    this.pruneOldEntries();
    for (const repo of state.openRepos()) await this.loadStamps(repo);
    this.tokens = (await localDB.get("meta", "sync_tokens")) || {};
    if (!this.tokens.repos || typeof this.tokens.repos !== "object") this.tokens.repos = {};
    api.onSyncHeader = (value) => this.onHeader(value);
    api.onSyncData = (data) => this.onSyncData(data);
    this.pruneOtherBuilds();

    await this.sync({ notify: false });
  }

  /**
   * Sync with the server (`GET /api/sync`): checks db, parsed, git, and frontend tokens.
   * In-flight calls share the promise; rapid calls within 1 s are coalesced.
   */
  async sync({ notify = true, fresh = false, force = false } = {}) {
    const now = Date.now();
    if (!force && !fresh && this._lastSyncTime && (now - this._lastSyncTime < 1000)) {
      return this.queue;
    }
    if (this._inFlightSync) {
      return this._inFlightSync;
    }
    this._inFlightSync = (async () => {
      try {
        const sync = await api.getSync({ fresh });
        this._lastSyncTime = Date.now();
        return await this.enqueue(sync, { notify, force });
      } catch (err) {
        console.info("[Sync] Server unreachable; using cached data.", err);
        return null;
      } finally {
        this._inFlightSync = null;
      }
    })();
    return this._inFlightSync;
  }

  /** Stamps of a repository as last saved (once per page); a repository's client keeps them in memory. */
  async loadStamps(repo) {
    const r = repo || "";
    if (this._stampsLoaded.has(r)) return;
    this._stampsLoaded.add(r);
    const saved = await localDB.get("meta", stampsKey(r)).catch(() => null);
    const client = api.forRepo(r);
    if (saved && typeof saved === "object" && !Object.keys(client.versionStamps).length) client.versionStamps = saved;
  }

  /** Tokens {parsed, git} last seen for a repository. */
  repoTokens(repo) {
    return (this.tokens.repos || {})[repo || ""] || {};
  }

  /** X-KIP-Sync of a response to a request naming `repo` ("db.parsed.git"). */
  onHeader(value, repo = api.repo) {
    const [db, parsed, git] = String(value).split(".");
    const t = this.repoTokens(repo);
    if ((db && db !== this.tokens.db) || (parsed && parsed !== t.parsed) || (git && git !== t.git)) {
      this.enqueue({ db, parsed, git, repo: repo || "" }, { notify: true });
    }
  }

  onSyncData(sync) {
    if (!sync) return;
    this.enqueue(sync, { notify: true });
  }

  enqueue(sync, opts) {
    this.queue = this.queue.then(() => this.apply(sync, opts)).catch((err) => console.warn("[Sync] Update failed:", err));
    return this.queue;
  }

  /**
   * Returns the version changes of refreshVersions() when the versions were refetched, else null. `announce`
   * false leaves a newer frontend build to the caller (checkNow(): the page restarts instead of showing the banner).
   */
  async apply(sync, { notify, force = false, announce = true }) {
    if (!sync) return null;
    // The repository these tokens belong to (`repo` of /api/sync, else the request's)
    const repo = typeof sync.repo === "string" ? sync.repo : (api.repo || "");
    await this.loadStamps(repo);
    const prev = this.tokens;
    const prevRepo = this.repoTokens(repo);
    const next = { ...prev, repos: { ...(prev.repos || {}) } };
    const nextRepo = { ...prevRepo };
    let refresh = force || !Object.keys(api.forRepo(repo).versionStamps).length;
    // The repository list has its own token: refetched when it changed, not with every parsed version
    let refreshProjects = force;

    // Check for frontend updates on every sync
    if (sync.frontend) {
      next.frontend = sync.frontend;
      if (announce) this.checkFrontend(sync.frontend);
    }

    if (sync.db && sync.db !== prev.db) {
      if (prev.db) {
        console.info(`[Sync] Database changed (${prev.db} -> ${sync.db}); clearing cached data.`);
        await localDB.clearDataStores();
        if ("caches" in window) {
          await caches.delete("kip-api");
        }
        // Every repository's stamps belonged to the old database
        api._stampsByRepo = {};
      }
      await localDB.set("meta", "db_instance_hash", sync.db);
      next.db = sync.db;
      refresh = true;
    }
    for (const key of ["parsed", "git"]) {
      if (sync[key] && sync[key] !== prevRepo[key]) {
        nextRepo[key] = sync[key];
        refresh = refresh || Boolean(prevRepo[key]);
      }
    }
    next.repos[repo] = nextRepo;
    if (sync.projects && sync.projects !== prev.projects) {
      next.projects = sync.projects;
      refreshProjects = refreshProjects || Boolean(prev.projects);
    }
    delete next.parsed;
    delete next.git;
    delete next.repo;
    this.tokens = next;
    await localDB.set("meta", "sync_tokens", next);
    if (refreshProjects) await this.refreshProjects({ notify });
    return refresh ? this.refreshVersions({ notify, repo }) : null;
  }

  /**
   * Settings "Check for Updates": the sync of the 60 s heartbeat (`GET /api/sync` with the server's cached
   * tokens, applied by apply(): a purged cache, refetched catalogs and repository list), run at once. Nothing is
   * re-rendered and a newer build is not announced: when `updated`, the caller restarts the page (restart()).
   * Resolves to {db, parsed, git, projects, frontend, newBuild, changed, removed, added, updated}: the first four
   * tell which token changed since the last sync, frontend / newBuild whether (and to what) the server's build
   * differs from the running page's, changed / removed / added the versions whose stamp changed, updated whether
   * any of them did. Rejects when the server is unreachable or the update could not be applied.
   */
  async checkNow() {
    const prev = { ...this.tokens, ...this.repoTokens(api.repo) };
    const sync = await api.getSync();
    const run = this.queue.then(() => this.apply(sync, { notify: false, announce: false }));
    this.queue = run.catch((err) => console.warn("[Sync] Update failed:", err));
    const versions = (await run) || { changed: [], removed: [], added: [] };
    // The other projects this session has tabs of are checked too
    for (const repo of state.openRepos()) {
      if (repo === (sync.repo || api.repo)) continue;
      try {
        const other = await api.forRepo(repo).getSync();
        const res = await this.queue.then(() => this.apply({ ...other, repo }, { notify: false, announce: false }));
        if (res) for (const k of ["changed", "removed", "added"]) versions[k].push(...res[k]);
      } catch (err) {
        console.warn(`[Sync] Could not check ${repo}:`, err);
      }
    }
    const newBuild = sync.frontend && this.build && !this.build.startsWith("__") && sync.frontend !== this.build ? sync.frontend : null;
    const changedToken = (k) => Boolean(sync[k] && prev[k] && sync[k] !== prev[k]);
    const found = {
      db: changedToken("db"),
      parsed: changedToken("parsed"),
      git: changedToken("git"),
      projects: changedToken("projects"),
      frontend: Boolean(newBuild),
    };
    const versionsChanged = versions.changed.length + versions.removed.length + versions.added.length > 0;
    return { ...found, newBuild, ...versions, updated: versionsChanged || Object.values(found).some(Boolean) };
  }

  /**
   * Restart the page after checkNow() found updates: into the new frontend build when there is one
   * (reloadToLatest(): cached shell dropped), else a plain reload, the shell and modules still served by the
   * Service Worker. `note` (what was found) is for the restarted page (takeRestartNote()).
   */
  restart(newBuild, note = "") {
    try {
      sessionStorage.setItem(RESTART_NOTE_KEY, note);
    } catch { /* storage unavailable */ }
    if (newBuild) this.reloadToLatest();
    else location.reload();
  }

  /** The note restart() left for this page, once; null when the page was not restarted by an update check. */
  takeRestartNote() {
    try {
      const note = sessionStorage.getItem(RESTART_NOTE_KEY);
      if (note !== null) sessionStorage.removeItem(RESTART_NOTE_KEY);
      return note;
    } catch {
      return null;
    }
  }

  /**
   * Refetch the repository list (GET /api/projects) after its `projects` token changed or on a manual check;
   * app.js re-renders the selectors on `projects:updated`. A failure keeps the cached list.
   */
  async refreshProjects({ notify }) {
    try {
      const res = await api.getProjects({ networkFirst: true });
      if (notify) state.emit("projects:updated", res);
    } catch (err) {
      console.warn("[Sync] Could not refresh the repository list:", err);
    }
  }

  /** Refetch a repository's version and branch stamps; drop its cached data of versions whose stamp changed. */
  async refreshVersions({ notify, repo = api.repo }) {
    const client = api.forRepo(repo);
    const [vRes, bRes] = await Promise.all([
      client.getVersions(true, { networkFirst: true }),
      client.getBranches({ networkFirst: true }),
    ]);
    const nextStamps = {};
    for (const v of vRes?.versions || []) if (v.vname && v.stamp) nextStamps[v.vname] = v.stamp;
    for (const b of bRes?.branches || []) if (b.name && b.stamp) nextStamps[b.name] = b.stamp;

    const prevStamps = client.versionStamps || {};
    const changed = Object.keys(nextStamps).filter((v) => prevStamps[v] !== undefined && prevStamps[v] !== nextStamps[v]);
    const removed = Object.keys(prevStamps).filter((v) => !(v in nextStamps));
    const added = Object.keys(nextStamps).filter((v) => prevStamps[v] === undefined);
    client.versionStamps = nextStamps;
    await localDB.set("meta", stampsKey(repo), nextStamps);

    const stale = [...changed, ...removed];
    if (stale.length) {
      // Only this repository's entries (cache keys are prefixed "<repo>::")
      const prefix = repo ? `${repo}::` : "";
      const dropped = await localDB.deleteDataWhere((key) => (!prefix || String(key).startsWith(prefix))
        && stale.some((v) => isStaleKey(key, v, nextStamps[v])));
      console.info(`[Sync] ${stale.length} version(s) changed (${stale.slice(0, 5).join(", ")}); ${dropped} cached entries dropped.`);
    }
    if (notify && (changed.length || removed.length || added.length)) {
      const newlyIndexed = changed.filter((v) => prevStamps[v].endsWith(".git") && !nextStamps[v].endsWith(".git"));
      state.emit("versions:updated", { repo: repo || "", changed, removed, added, newlyIndexed });
    }
    return { changed, removed, added };
  }

  // ------------------------------------------------------------------ frontend build
  checkFrontend(serverBuild) {
    if (!serverBuild || !this.build || this.build.startsWith("__")) return;
    if (serverBuild === this.build) return;
    this.updateServiceWorker();
    if (this.postponedBuild === serverBuild) return;
    try {
      localStorage.setItem(LAST_FRONTEND_CHECK_KEY, String(Date.now()));
    } catch { /* storage unavailable */ }
    this.announceUpdate(serverBuild);
  }

  /** Banner "KernelInfo was updated", reloading after RELOAD_DELAY_S (waits while a dialog is open). */
  announceUpdate(serverBuild) {
    if (document.getElementById("kip-update-banner")) return;
    const bar = document.createElement("div");
    bar.id = "kip-update-banner";
    bar.className = "kip-update-banner";
    bar.setAttribute("role", "status");
    bar.innerHTML = `
      <span class="kip-update-text"></span>
      <button type="button" class="kip-update-now">Reload now</button>
      <button type="button" class="kip-update-later">Later</button>`;
    document.body.appendChild(bar);
    const text = bar.querySelector(".kip-update-text");
    let left = RELOAD_DELAY_S;
    const tick = () => {
      if (document.querySelector(".ki-modal-backdrop")) {
        text.textContent = "KernelInfo was updated. It will reload once the open dialog is closed.";
        this.reloadTimer = setTimeout(tick, 1000);
        return;
      }
      if (left <= 0) {
        this.reloadToLatest();
        return;
      }
      text.textContent = `KernelInfo was updated (build ${serverBuild}). Reloading in ${left} s…`;
      left -= 1;
      this.reloadTimer = setTimeout(tick, 1000);
    };
    bar.querySelector(".kip-update-now").onclick = () => this.reloadToLatest();
    bar.querySelector(".kip-update-later").onclick = () => {
      clearTimeout(this.reloadTimer);
      this.postponedBuild = serverBuild;
      bar.remove();
    };
    tick();
  }

  /**
   * Refetch and update the Service Worker only when sync confirms a frontend build change.
   * Uses cache: 'reload' to bypass the HTTP cache and update it with the new /sw.js.
   */
  async updateServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      if (!reg) return;
      await fetch("/sw.js", { cache: "reload" });
      await reg.update();
    } catch (e) {
      console.warn("[Sync] ServiceWorker update failed:", e);
    }
  }

  reloadToLatest() {
    clearTimeout(this.reloadTimer);
    this.updateServiceWorker();
    if (window.KIP_RELOAD_TO_LATEST) window.KIP_RELOAD_TO_LATEST();
    else location.reload();
  }

  /** At most daily: drop cached API data older than CACHE_MAX_AGE_MS (runs in the background). */
  pruneOldEntries() {
    let last = 0;
    try {
      last = Number(localStorage.getItem(LAST_PRUNE_KEY) || 0);
    } catch { /* storage unavailable */ }
    if (Date.now() - last < 24 * 60 * 60 * 1000) return;
    try {
      localStorage.setItem(LAST_PRUNE_KEY, String(Date.now()));
    } catch { /* storage unavailable */ }
    localDB.pruneOlderThan(CACHE_MAX_AGE_MS)
      .then((n) => { if (n) console.info(`[Sync] ${n} cached entries older than 30 days dropped.`); })
      .catch(() => {});
  }

  /** Let the service worker drop cached files of other builds. */
  pruneOtherBuilds() {
    const build = this.build;
    if (!build || build.startsWith("__")) return;
    navigator.serviceWorker?.ready?.then((reg) => reg.active?.postMessage({ type: "prune", build })).catch(() => {});
  }
}

/** Whether a cache key holds data of version `v` under a stamp other than `stamp` (see api._vk). */
function isStaleKey(key, v, stamp) {
  let i = key.indexOf(`${v}@`);
  while (i >= 0) {
    const before = i === 0 ? "" : key[i - 1];
    if (!before || before === "_" || before === ":") {
      return !(stamp && key.startsWith(`${v}@${stamp}~`, i));
    }
    i = key.indexOf(`${v}@`, i + 1);
  }
  return false;
}

export const syncManager = new SyncManager();
