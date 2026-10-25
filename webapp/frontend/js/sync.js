/**
 * sync.js - Keeps the client in step with the server (tokens of GET /api/sync, see
 * webapp/backend/services/sync_service.py).
 *
 * - db: a rebuilt database clears every cached API response.
 * - parsed / git: version and branch lists are refetched; each version carries a stamp (commit + parse
 *   completion) that is part of its cache keys (api._vk), and entries of versions whose stamp changed
 *   are deleted. Checked when the page opens and whenever an API response's X-KIP-Sync header differs.
 * - frontend: when the page opens and the last check is more than an hour old, a newer build than the
 *   one this page runs (window.KIP_BUILD, see boot.js) is announced and the page reloads into it after
 *   5 seconds ("Later" postpones it to the next check).
 * - checkNow() (Settings "Check for Updates"): all four tokens recomputed by the server, version stamps
 *   refetched and the frontend build compared regardless of the hourly delay.
 */
import { api } from "./api.js";
import { localDB } from "./db.js";
import { state } from "./state.js";

const FRONTEND_CHECK_MS = 60 * 60 * 1000;
const RELOAD_DELAY_S = 5;
const LAST_FRONTEND_CHECK_KEY = "kip-last-frontend-check";
// Bumped when cache keys change shape: entries of the previous layout are dropped once
const CACHE_LAYOUT = 2;

class SyncManager {
  constructor() {
    this.tokens = {};
    this.queue = Promise.resolve();
    this.reloadTimer = null;
  }

  /** Frontend build of the running page. */
  get build() {
    return window.KIP_BUILD || document.querySelector('meta[name="kip-build"]')?.content || "";
  }

  /** Page open: restore what was seen last, compare with the server, then follow response headers. */
  async start() {
    if ((await localDB.get("meta", "cache_layout")) !== CACHE_LAYOUT) {
      await localDB.clearDataStores();
      await localDB.set("meta", "cache_layout", CACHE_LAYOUT);
      await localDB.set("meta", "version_stamps", {});
    }
    api.versionStamps = (await localDB.get("meta", "version_stamps")) || {};
    this.tokens = (await localDB.get("meta", "sync_tokens")) || {};
    api.onSyncHeader = (value) => this.onHeader(value);
    this.pruneOtherBuilds();

    let sync;
    try {
      sync = await api.getSync();
    } catch (err) {
      console.info("[Sync] Server unreachable; using cached data.", err);
      return;
    }
    await this.enqueue(sync, { notify: false });
    this.checkFrontend(sync.frontend);
  }

  onHeader(value) {
    const [db, parsed, git] = String(value).split(".");
    const t = this.tokens;
    if ((db && db !== t.db) || (parsed && parsed !== t.parsed) || (git && git !== t.git)) {
      this.enqueue({ db, parsed, git }, { notify: true });
    }
  }

  enqueue(sync, opts) {
    this.queue = this.queue.then(() => this.apply(sync, opts)).catch((err) => console.warn("[Sync] Update failed:", err));
    return this.queue;
  }

  /** Returns the version changes of refreshVersions() when the versions were refetched, else null. */
  async apply(sync, { notify, force = false }) {
    const prev = this.tokens;
    const next = { ...prev };
    let refresh = force || !Object.keys(api.versionStamps).length;

    if (sync.db && sync.db !== prev.db) {
      if (prev.db) {
        console.info(`[Sync] Database changed (${prev.db} -> ${sync.db}); clearing cached data.`);
        await localDB.clearDataStores();
        api.versionStamps = {};
        document.dispatchEvent(new CustomEvent("app:cache-reset", { detail: { instance_hash: sync.db } }));
      }
      await localDB.set("meta", "db_instance_hash", sync.db);
      next.db = sync.db;
      refresh = true;
    }
    for (const key of ["parsed", "git"]) {
      if (sync[key] && sync[key] !== prev[key]) {
        next[key] = sync[key];
        refresh = refresh || Boolean(prev[key]);
      }
    }
    this.tokens = next;
    await localDB.set("meta", "sync_tokens", next);
    return refresh ? this.refreshVersions({ notify }) : null;
  }

  /**
   * Manual check: server tokens recomputed (`?fresh=true`), version stamps always refetched, frontend build
   * compared without the hourly delay. Resolves to {db, parsed, git, frontend, changed, removed, added,
   * newBuild}: the first four tell which token changed, newBuild is the server build when it differs from
   * the running page's (the reload banner is then shown). Rejects when the server is unreachable.
   */
  async checkNow() {
    const sync = await api.getSync({ fresh: true });
    const prev = { ...this.tokens };
    const run = this.queue.then(() => this.apply(sync, { notify: true, force: true }));
    this.queue = run.catch((err) => console.warn("[Sync] Update failed:", err));
    const versions = (await run) || { changed: [], removed: [], added: [] };
    try {
      localStorage.setItem(LAST_FRONTEND_CHECK_KEY, String(Date.now()));
    } catch { /* storage unavailable */ }
    const newBuild = sync.frontend && this.build && !this.build.startsWith("__") && sync.frontend !== this.build ? sync.frontend : null;
    if (newBuild) this.announceUpdate(newBuild);
    const changedToken = (k) => Boolean(sync[k] && prev[k] && sync[k] !== prev[k]);
    return {
      db: changedToken("db"),
      parsed: changedToken("parsed"),
      git: changedToken("git"),
      frontend: Boolean(newBuild),
      ...versions,
      newBuild,
    };
  }

  /** Refetch version and branch stamps; drop cached data of versions whose stamp changed. */
  async refreshVersions({ notify }) {
    const [vRes, bRes] = await Promise.all([api.getVersions(true), api.getBranches()]);
    const nextStamps = {};
    for (const v of vRes?.versions || []) if (v.vname && v.stamp) nextStamps[v.vname] = v.stamp;
    for (const b of bRes?.branches || []) if (b.name && b.stamp) nextStamps[b.name] = b.stamp;

    const prevStamps = api.versionStamps || {};
    const changed = Object.keys(nextStamps).filter((v) => prevStamps[v] !== undefined && prevStamps[v] !== nextStamps[v]);
    const removed = Object.keys(prevStamps).filter((v) => !(v in nextStamps));
    const added = Object.keys(nextStamps).filter((v) => prevStamps[v] === undefined);
    api.versionStamps = nextStamps;
    await localDB.set("meta", "version_stamps", nextStamps);

    const stale = [...changed, ...removed];
    if (stale.length) {
      const dropped = await localDB.deleteDataWhere((key) => stale.some((v) => isStaleKey(key, v, nextStamps[v])));
      console.info(`[Sync] ${stale.length} version(s) changed (${stale.slice(0, 5).join(", ")}); ${dropped} cached entries dropped.`);
    }
    if (notify && (changed.length || removed.length || added.length)) {
      const newlyIndexed = changed.filter((v) => prevStamps[v].endsWith(".git") && !nextStamps[v].endsWith(".git"));
      state.emit("versions:updated", { changed, removed, added, newlyIndexed });
    }
    return { changed, removed, added };
  }

  // ------------------------------------------------------------------ frontend build
  checkFrontend(serverBuild) {
    let last = 0;
    try {
      last = Number(localStorage.getItem(LAST_FRONTEND_CHECK_KEY) || 0);
    } catch { /* storage unavailable */ }
    if (Date.now() - last < FRONTEND_CHECK_MS) return;
    try {
      localStorage.setItem(LAST_FRONTEND_CHECK_KEY, String(Date.now()));
    } catch { /* storage unavailable */ }
    if (serverBuild && this.build && serverBuild !== this.build && !this.build.startsWith("__")) {
      this.announceUpdate(serverBuild);
    }
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
      bar.remove();
    };
    tick();
  }

  reloadToLatest() {
    clearTimeout(this.reloadTimer);
    if (window.KIP_RELOAD_TO_LATEST) window.KIP_RELOAD_TO_LATEST();
    else location.reload();
  }

  /** Let the service worker drop cached files of other builds. */
  pruneOtherBuilds() {
    const build = this.build;
    if (!build || build.startsWith("__")) return;
    navigator.serviceWorker?.ready?.then((reg) => reg.active?.postMessage({ type: "prune", build })).catch(() => {});
  }
}

/** Whether a cache key holds data of version `v` under a stamp other than `stamp` (see api._vk). */
export function isStaleKey(key, v, stamp) {
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
