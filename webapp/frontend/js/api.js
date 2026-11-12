/**
 * api.js - Centralized API client for KernelInfo-Parser.
 * Handles timeouts, retries, and offline caching via IndexedDB.
 */
import { localDB } from "./db.js";
import { state } from "./state.js";
import { VERSION_CATALOG_FORMAT, catalogFromBody, isCatalog, versionsFromCatalog } from "./utils/version_catalog.js";
import { pollJob } from "./utils/job_poll.js";
import { grepParams } from "./utils/grep_format.js";
import { blameQuery, formatBlameOpts } from "./views/code_view/blame_options.js";

// Heartbeat probing GET /api/sync (cheap, server-cached): slow while the backend answers, fast while it
// does not, so the badge recovers quickly once the server is back.
const HEARTBEAT_UP_MS = 60000;
const HEARTBEAT_DOWN_MS = 5000;
const HEARTBEAT_TIMEOUT_MS = 5000;
// Request timeout; `timeoutMs` in a request's options raises it for the few endpoints that compute
// (branch reachability walks the whole history of a branch).
const REQUEST_TIMEOUT_MS = 10000;
const BRANCH_TAGS_TIMEOUT_MS = 180000;
// Commits: timeline seek, multi-page searches, and Git diff/fallback on large repositories
const COMMITS_TIMEOUT_MS = 45000;
// Compare: the revision diff runs Git's rename detection, the rename search compares every candidate pair
const VERSIONS_DIFF_TIMEOUT_MS = 45000;
const RENAME_SEARCH_TIMEOUT_MS = 120000;
// Callgraph: the callers of a widely used function are looked up across every indexed blob (~25 s for schedule)
const CALLGRAPH_TIMEOUT_MS = 60000;

class ApiClient {
  constructor() {
    this.baseUrl = "";
    this.isOnline = navigator.onLine;
    // Whether the backend itself answered last time we talked to it. navigator.onLine only reports the
    // browser's link state, so it stays true when the server is down; this is what the badge follows.
    this.serverUp = navigator.onLine;
    this.activeRequests = 0;
    // Repository -> {version name -> stamp (commit + parse completion)} from /api/versions and /api/branches;
    // part of every cache key of version data, so a re-parsed version or a moved branch never serves older
    // cached data. `versionStamps` is the map of the client's repository.
    this._stampsByRepo = {};
    // Repository-scoped clients (forRepo), one per repository name
    this._scoped = new Map();
    this._versionsInFlight = new Map();
    this._versionsMemo = new Map();
    // Called with (X-KIP-Sync header, repository of the request) of every API response (js/sync.js)
    this.onSyncHeader = null;
    // Called with the parsed JSON body of GET /api/sync (js/sync.js)
    this.onSyncData = null;
    this._showTimer = null;
    this._heartbeatTimer = null;

    window.addEventListener("online", () => {
      this.isOnline = true;
      this.pingServer();
    });
    window.addEventListener("offline", () => {
      this.isOnline = false;
      this._setServerUp(false);
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) this.pingServer();
    });
  }

  /** True once the connection is known to be usable: the link is up and the backend answers. */
  get isConnected() {
    return this.isOnline && this.serverUp;
  }

  /**
   * Records whether the backend answered. Any HTTP response counts as reachable (a 500 still proves the
   * server is there); only transport failures and timeouts count as unreachable. Emits
   * `app:server-status` ({up}) on a change and re-paces the heartbeat.
   */
  _setServerUp(up) {
    up = Boolean(up);
    if (this.serverUp === up) return;
    this.serverUp = up;
    document.dispatchEvent(new CustomEvent("app:server-status", { detail: { up } }));
    if (this._heartbeatTimer) this.startHeartbeat();
  }

  /** One heartbeat request; resolves to the reachability it observed. Never throws, never cached. */
  async pingServer() {
    if (!navigator.onLine) {
      this._setServerUp(false);
      return false;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), HEARTBEAT_TIMEOUT_MS);
    try {
      const res = await fetch(`${this.baseUrl}/api/sync`, {
        method: "GET",
        signal: controller.signal,
        headers: { Accept: "application/json", ...(this.repo ? { "X-KI-Repo": this.repo } : {}) },
        cache: "no-store",
      });
      const ok = res.ok;
      this._setServerUp(ok);
      if (ok) {
        try {
          const syncData = await res.json();
          if (this.onSyncData) {
            this.onSyncData(syncData);
          }
        } catch {
          // Ignore JSON parse failure
        }
        const syncHeader = res.headers.get("X-KIP-Sync");
        if (syncHeader && this.onSyncHeader && !this.onSyncData) {
          this.onSyncHeader(syncHeader, this.repo || "");
        }
      }
      return ok;
    } catch {
      this._setServerUp(false);
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  /** Starts (or re-paces) the heartbeat. Idempotent; the interval follows the current server state. */
  startHeartbeat() {
    this.stopHeartbeat();
    const period = this.serverUp ? HEARTBEAT_UP_MS : HEARTBEAT_DOWN_MS;
    this._heartbeatTimer = setInterval(() => {
      // A hidden tab cannot show the badge anyway; visibilitychange probes as soon as it comes back.
      if (!document.hidden) this.pingServer();
    }, period);
  }

  stopHeartbeat() {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
  }

  _onRequestStart() {
    this.activeRequests++;
    if (this.activeRequests === 1) {
      // 150ms debounce before displaying to avoid flicker on fast sub-150ms responses
      if (this._showTimer) clearTimeout(this._showTimer);
      this._showTimer = setTimeout(() => {
        if (this.activeRequests > 0) {
          this._setSpinnerActive(true);
        }
      }, 150);
    }
  }

  _onRequestEnd() {
    this.activeRequests = Math.max(0, this.activeRequests - 1);
    if (this.activeRequests === 0) {
      if (this._showTimer) {
        clearTimeout(this._showTimer);
        this._showTimer = null;
      }
      this._setSpinnerActive(false);
    }
  }

  _setSpinnerActive(active) {
    const el = document.getElementById("global-loading-spinner");
    if (el) {
      if (active) {
        el.classList.add("active");
      } else {
        el.classList.remove("active");
      }
    }
  }

  /**
   * Repository this client addresses: sent with every request (X-KI-Repo), namespacing offline cache keys and
   * version stamps. The shared client follows the active tab's repository (else the header's Project); a
   * client of `forRepo()` / `forTab()` always names its own (§5.18).
   */
  get repo() {
    return state.getActiveTab()?.repo || state.currentRepo || "";
  }

  /** Version stamps of this client's repository (see _stampsByRepo). */
  get versionStamps() {
    const r = this.repo || "";
    return this._stampsByRepo[r] || (this._stampsByRepo[r] = {});
  }

  set versionStamps(stamps) {
    this._stampsByRepo[this.repo || ""] = stamps && typeof stamps === "object" ? stamps : {};
  }

  /**
   * Client bound to one repository: the same methods, every request (headers, `?repo=` URLs, cache keys,
   * stamps) naming `repo`. Connection state (reachability, spinner, heartbeat, memos) stays shared: writes
   * go to the shared client. "" gives the shared client itself.
   */
  forRepo(repo) {
    const root = this._root || this;
    const name = typeof repo === "string" ? repo : "";
    if (!name) return root;
    let client = root._scoped.get(name);
    if (!client) {
      client = new Proxy(root, {
        get: (target, prop, receiver) => (prop === "repo" ? name : Reflect.get(target, prop, receiver)),
        set: (target, prop, value, receiver) => (prop === "repo" ? true : Reflect.set(target, prop, value, receiver)),
      });
      root._scoped.set(name, client);
    }
    return client;
  }

  /** Client of a tab's repository (the shared client for a tab without one). */
  forTab(tab) {
    return this.forRepo(tab?.repo || "");
  }

  /**
   * One request: the timeout (10 s unless `timeoutMs`), the loading indicator, the repository header and the
   * reachability / X-KIP-Sync bookkeeping. Resolves to {response, data}: data is the JSON body, null for a
   * 304 (only requests that send If-None-Match themselves get one). Rejects on a transport failure, the
   * timeout, or an HTTP error status (with the server's detail).
   */
  async _fetchJson(url, options = {}, timeoutMs = 0) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || REQUEST_TIMEOUT_MS);
    // A caller's own signal (a hover card the pointer left) cancels the request too
    const callerSignal = options.signal || null;
    if (callerSignal) {
      if (callerSignal.aborted) controller.abort();
      else callerSignal.addEventListener("abort", () => controller.abort(), { once: true });
    }
    this._onRequestStart();
    let answered = false;

    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          ...(this.repo ? { "X-KI-Repo": this.repo } : {}),
          ...(options.headers || {})
        }
      });
      clearTimeout(timer);
      // The Service Worker answers from its own cache when the request failed (sw.js, networkFirst);
      // such a reply carries X-KIP-Offline and says nothing about the server being up.
      answered = !response.headers.get("X-KIP-Offline");
      this._setServerUp(answered);
      const syncHeader = response.headers.get("X-KIP-Sync");
      if (syncHeader && this.onSyncHeader) this.onSyncHeader(syncHeader, this.repo || "");

      if (response.status === 304) return { response, data: null };
      if (!response.ok) {
        const errText = await response.text();
        let errMsg = `Request failed: ${response.status}`;
        try {
          const errObj = JSON.parse(errText);
          if (errObj.detail) errMsg = errObj.detail;
        } catch {
          if (errText) errMsg = errText;
        }
        console.error(`API Error [${response.status}] for ${url}: ${errMsg}`);
        const error = new Error(errMsg);
        error.status = response.status;
        throw error;
      }
      return { response, data: await response.json() };
    } catch (err) {
      clearTimeout(timer);
      // Only a transport failure or the timeout means the server is unreachable; an HTTP error status
      // threw above but already proved it answered.
      // A request the caller cancelled says nothing about the server
      if (!answered && !callerSignal?.aborted) this._setServerUp(false);
      if (callerSignal?.aborted && err?.name !== "AbortError") {
        const abortErr = new Error("Aborted");
        abortErr.name = "AbortError";
        throw abortErr;
      }
      throw err;
    } finally {
      this._onRequestEnd();
    }
  }

  async request(endpoint, options = {}, cacheStore = null, cacheKey = null) {
    const url = `${this.baseUrl}${endpoint}`;
    if (cacheKey && this.repo) cacheKey = `${this.repo}::${cacheKey}`;

    // 1. Cache-First: If cache info provided and not a mutating method, return cached immediately
    //    (catalog endpoints pass networkFirst so newly parsed versions/repositories show up)
    const { networkFirst, timeoutMs, ...fetchOptions } = options;
    options = fetchOptions;
    if (!networkFirst && cacheStore && cacheKey && (!options.method || options.method === "GET")) {
      const cached = await localDB.get(cacheStore, cacheKey);
      if (cached !== null && cached !== undefined) return cached;
    }

    try {
      // 2. Fetch with the request timeout (10 s unless the endpoint asked for more)
      const { response, data } = await this._fetchJson(url, options, timeoutMs);

      // 3. Cache successful response in IndexedDB (skip if served from Service Worker offline cache)
      if (cacheStore && cacheKey && !response.headers.get("X-KIP-Offline")) {
        // Do not cache failed or empty mail items in IndexedDB
        if (cacheStore !== "mail" || (data && data.available !== false && !data.error && (data.raw || data.body || data.messages || data.results || data.versions))) {
          localDB.set(cacheStore, cacheKey, data).catch((e) => console.warn("Cache write failed:", e));
        }
      }

      return data;
    } catch (err) {
      // 4. On network failure, fallback to cache if available
      if (cacheStore && cacheKey) {
        const cached = await localDB.get(cacheStore, cacheKey);
        if (cached !== null && cached !== undefined) {
          console.info(`Serving offline cache for ${cacheKey}`);
          return cached;
        }
      }
      throw err;
    }
  }

  /**
   * A background-job endpoint (webapp/API.md §2.15). While the server answers 202 Accepted (body
   * {state: "building", progress, retry_after_ms}) the request is repeated with a backoff from 500 ms to
   * 5 s and `onProgress({done, total, note})` is called; the final 200 body is returned. `signal` (the
   * view's AbortController) stops polling with an AbortError. Only final bodies are stored in IndexedDB
   * (cacheStore/cacheKey, read first like `request`); 202 bodies never are.
   */
  async requestJob(endpoint, { onProgress = null, signal = null, cacheStore = null, cacheKey = null,
    method = "GET", body = undefined, timeoutMs = 0, networkFirst = false } = {}) {
    const url = `${this.baseUrl}${endpoint}`;
    if (cacheKey && this.repo) cacheKey = `${this.repo}::${cacheKey}`;
    if (!networkFirst && cacheStore && cacheKey && method === "GET") {
      const cached = await localDB.get(cacheStore, cacheKey);
      if (cached !== null && cached !== undefined) return cached;
    }
    const options = { method };
    if (body !== undefined) options.body = typeof body === "string" ? body : JSON.stringify(body);
    let offline = false;
    try {
      const data = await pollJob(async () => {
        const { response, data: answer } = await this._fetchJson(url, options, timeoutMs);
        offline = Boolean(response.headers.get("X-KIP-Offline"));
        return { status: response.status, data: answer };
      }, { onProgress, signal });
      if (cacheStore && cacheKey && !offline && data != null) {
        localDB.set(cacheStore, cacheKey, data).catch((e) => console.warn("Cache write failed:", e));
      }
      return data;
    } catch (err) {
      if (err?.name !== "AbortError" && cacheStore && cacheKey) {
        const cached = await localDB.get(cacheStore, cacheKey);
        if (cached !== null && cached !== undefined) return cached;
      }
      throw err;
    }
  }

  /**
   * First tag and release containing a commit (`GET /api/commits/{v}/released/{hash}`, H3). A released
   * answer never changes and is kept in the `commits` store; "not released yet" is asked again later.
   * Polls while the release index is first built (202, §2.15).
   */
  async getCommitRelease(version, hash, { onProgress = null, signal = null } = {}) {
    const ver = this._sanitizeVer(version);
    const key = this.repo ? `${this.repo}::rel_${hash}` : `rel_${hash}`;
    const cached = await localDB.get("commits", key);
    if (cached) return cached;
    const data = await this.requestJob(`/api/commits/${encodeURIComponent(ver)}/released/${encodeURIComponent(hash)}`, { onProgress, signal });
    if (data?.released) localDB.set("commits", key, data).catch(() => {});
    return data;
  }

  /**
   * Relations of a commit (`GET /api/commits/{v}/relations/{hash}`, H1/H2): fixes, fixed by, reverts,
   * backports, Cc: stable, mail links, each enriched with its first release. Polls while the index is
   * first built; never cached (later commits add to "fixed by").
   */
  async getCommitRelations(version, hash, { onProgress = null, signal = null } = {}) {
    const ver = this._sanitizeVer(version);
    return this.requestJob(`/api/commits/${encodeURIComponent(ver)}/relations/${encodeURIComponent(hash)}`, { onProgress, signal, timeoutMs: 30000 });
  }

  /** Relation counts of up to 200 commits (timeline chips): {results: {hash: {fixes, fixed_by, reverted_by, backports, stable_cc}}}. */
  async getRelationCounts(version, hashes, { signal = null } = {}) {
    const ver = this._sanitizeVer(version);
    return this.requestJob(`/api/commits/${encodeURIComponent(ver)}/relations`, { method: "POST", body: { hashes: hashes.slice(0, 200) }, signal, timeoutMs: 30000 });
  }

  /** Structured commit query (`GET /api/commits/{v}/query`, H8); `params` from commit_query.queryParams(). Not cached. */
  async queryCommits(version, params) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/commits/${encodeURIComponent(ver)}/query?${params}`, { timeoutMs: COMMITS_TIMEOUT_MS });
  }

  /** Commits a mail thread or patch became (`GET /api/mail/merged`, P2); kept in the `mail` store under the `parsed` token. */
  async getMergedCommits({ mid = "", tid = 0 } = {}) {
    const params = new URLSearchParams();
    if (mid) params.set("mid", mid);
    if (tid) params.set("tid", String(tid));
    // The parser may link more commits later: the cache key follows the `parsed` token (§2.11)
    const token = ((await localDB.get("meta", "sync_tokens").catch(() => null)) || {}).parsed || "";
    return this.request(`/api/mail/merged?${params}`, { timeoutMs: 30000 }, "tools", `merged_${token}_${params}`);
  }

  /** CVE records for a file at a version (`GET /api/cve/file?version={v}&file_path={p}`). */
  async getFileCves(version, filePath) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ version: ver, file_path: filePath || "" }).toString();
    return this.request(`/api/cve/file?${q}`, {}, "tools", `cve_file_${this._vk(ver)}_${filePath}`);
  }

  /**
   * An Insights report (`GET /api/insights/{v}/<path>`, §4.10): polls while its background job runs
   * (202, §2.15). `cacheKey` (optional) keeps the final body in the `tools` store.
   */
  async getInsights(version, path, { onProgress = null, signal = null, cacheKey = null } = {}) {
    const ver = this._sanitizeVer(version);
    return this.requestJob(`/api/insights/${encodeURIComponent(ver)}/${path}`, { onProgress, signal, timeoutMs: 60000,
      cacheStore: cacheKey ? "tools" : null, cacheKey });
  }

  /** A Kernel API catalog (`GET /api/kapi/{v}/<path>`, §4.9), polling while its job runs. */
  async getKapi(version, path, { onProgress = null, signal = null, cacheKey = null } = {}) {
    const ver = this._sanitizeVer(version);
    return this.requestJob(`/api/kapi/${encodeURIComponent(ver)}/${path}`, { onProgress, signal, timeoutMs: 60000,
      cacheStore: cacheKey ? "tools" : null, cacheKey: cacheKey ? `${cacheKey}_${this._vk(ver)}` : null });
  }

  /** Cache-key form of a version: "<name>@<stamp>~" once its stamp is known (see versionStamps). */
  _vk(v) {
    const stamp = this.versionStamps[v];
    return stamp ? `${v}@${stamp}~` : v;
  }

  /**
   * Ingestion status of the active repository (`GET /api/system/ingest`, §4.1, G11): versions parsed and
   * being parsed, recent completions, database size, the server's tokens, jobs and caches. Always asked
   * of the server (cached 10 s there); the last answer is kept for offline display.
   */
  async getIngestStatus() {
    return this.request("/api/system/ingest", { networkFirst: true }, "tools", "ingest_status");
  }

  /** Server change tokens {db, frontend, parsed, git, repo}; never cached. `fresh` makes the server recompute them. */
  async getSync({ fresh = false } = {}) {
    let res;
    try {
      res = await fetch(`${this.baseUrl}/api/sync${fresh ? "?fresh=true" : ""}`, {
        headers: { Accept: "application/json", ...(this.repo ? { "X-KI-Repo": this.repo } : {}) },
        cache: "no-store",
      });
    } catch (err) {
      this._setServerUp(false);
      throw err;
    }
    this._setServerUp(true);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  _sanitizeVer(v) {
    // A version object stringified by mistake ("[object Object]"); a branch may well contain "object"
    if (!v || typeof v !== "string" || v.startsWith("[object ")) {
      return "v3.0";
    }
    return v;
  }

  // --- Versions & Projects ---
  async getProjects(options = {}) {
    return this.request("/api/projects", options, "files", "app_projects2");
  }

  async getBranches(options = {}) {
    return this.request("/api/branches", options, "files", "app_branches");
  }

  /**
   * Tag names reachable from a branch (`/api/branch/tags`): what the header's Branch selector scopes
   * the Tag and RC lists with (§5.13). The server walks that branch's history for it, so the request
   * gets a long timeout and the answer is cached under the branch's stamp — a branch that moved gets
   * a new key and is recomputed, an unchanged one is read from IndexedDB.
   */
  async getBranchTags(branch) {
    const b = typeof branch === "string" ? branch.trim() : "";
    if (!b) return { branch: "", total: 0, tags: [] };
    return this.request(
      `/api/branch/tags?branch=${encodeURIComponent(b)}`,
      { timeoutMs: BRANCH_TAGS_TIMEOUT_MS },
      "files",
      `branch_tags_${this._vk(b)}`,
    );
  }

  /**
   * Version catalog of the repository (webapp/API.md §4.1) as {total, versions, g}, entries {vname,
   * commit_hash (12 hex), is_prerelease, ref_type, indexed, stamp}. Read from IndexedDB unless
   * `networkFirst` (sync refresh, §2.11). The network request asks for the compact form and names the
   * generation this client has (If-None-Match): the server answers 304, the rows that changed since, or
   * the whole catalog. Concurrent calls of the same kind share one request.
   */
  getVersions(includeRc = true, { networkFirst = false } = {}) {
    const key = `${this.repo || ""}|${includeRc}|${networkFirst ? "net" : "cache"}`;
    let pending = this._versionsInFlight.get(key);
    if (!pending) {
      pending = this._loadVersions(includeRc, networkFirst).finally(() => this._versionsInFlight.delete(key));
      this._versionsInFlight.set(key, pending);
    }
    return pending;
  }

  async _loadVersions(includeRc, networkFirst) {
    const memoKey = `${this.repo || ""}|${includeRc}`;
    const cacheKey = `${this.repo ? `${this.repo}::` : ""}app_versions_${VERSION_CATALOG_FORMAT}_${includeRc ? "rc" : "rel"}`;
    let base = await localDB.get("files", cacheKey);
    if (!isCatalog(base)) base = null;
    if (base && !networkFirst) return this._rememberVersions(memoKey, base);

    const url = `${this.baseUrl}/api/versions?include_rc=${includeRc ? "true" : "false"}&compact=1`;
    let catalog = null;
    try {
      const headers = base ? { "If-None-Match": `W/"${VERSION_CATALOG_FORMAT}-${base.g}"` } : {};
      const { response, data } = await this._fetchJson(url, { headers });
      if (response.status === 304) {
        catalog = base;
      } else if (response.headers.get("X-KIP-Offline")) {
        catalog = base || catalogFromBody(data, null); // the Service Worker's copy is never newer than ours
      } else {
        catalog = catalogFromBody(data, base);
        // A delta from a generation this client does not have: ask for the whole catalog
        if (!catalog && base) catalog = catalogFromBody((await this._fetchJson(url)).data, null);
      }
    } catch (err) {
      if (!base) throw err;
      catalog = base; // offline or failing: the catalog this client has
    }
    if (!catalog) throw new Error("Unexpected version catalog response");
    if (catalog !== base) localDB.set("files", cacheKey, catalog).catch((e) => console.warn("Cache write failed:", e));
    return this._rememberVersions(memoKey, catalog);
  }

  /** The decoded catalog, kept per repository (getVersionsCached) and decoded once per generation. */
  _rememberVersions(memoKey, catalog) {
    const memo = this._versionsMemo.get(memoKey);
    if (memo && memo.g === catalog.g) return memo;
    const versions = versionsFromCatalog(catalog);
    const res = { total: versions.length, versions, g: catalog.g };
    this._versionsMemo.set(memoKey, res);
    return res;
  }

  /**
   * The version catalog as last fetched in this page (startup and every sync refresh go through
   * getVersions), fetched only when nothing was yet. For lookups that must not wait on the
   * network: ⇄ Diff's default revision, the ref picker's tag list, the Compare view's default base.
   */
  async getVersionsCached(includeRc = true) {
    const hit = this._versionsMemo?.get(`${this.repo || ""}|${includeRc}`);
    return hit || this.getVersions(includeRc);
  }

  /** URL with ?repo= for requests that cannot carry headers (img src, downloads). */
  withRepo(path) {
    if (!this.repo) return path;
    return `${path}${path.includes("?") ? "&" : "?"}repo=${encodeURIComponent(this.repo)}`;
  }

  getRawFileUrl(version, path) {
    const ver = this._sanitizeVer(version);
    return this.withRepo(`/api/fs/raw/${encodeURIComponent(ver)}?path=${encodeURIComponent(path)}`);
  }

  getCompileCommandsDownloadUrl(version, arch = "x86") {
    const ver = this._sanitizeVer(version);
    return this.withRepo(`/api/fs/compile_commands/${encodeURIComponent(ver)}/export?arch=${encodeURIComponent(arch)}`);
  }

  /**
   * Full-text search of a revision (`GET /api/fs/grep`, §4.2, §2.1.3). `opts`: {q, regex, caseSensitive,
   * word, paths, limit}. Answers are kept in the `tools` store under the version's stamp (a commit's
   * content never changes); a timed-out, partial answer is never stored.
   */
  async grepCode(version, opts = {}) {
    const ver = this._sanitizeVer(version);
    const params = grepParams(opts);
    const cacheKey = `grep_${this._vk(ver)}_${params.toString()}`;
    const scopedKey = this.repo ? `${this.repo}::${cacheKey}` : cacheKey;
    const cached = await localDB.get("tools", scopedKey);
    if (cached) return cached;
    const data = await this.request(`/api/fs/grep/${encodeURIComponent(ver)}?${params}`, { timeoutMs: 30000 });
    if (data && !data.timed_out) localDB.set("tools", scopedKey, data).catch(() => {});
    return data;
  }

  /** What refers to a file at a revision (`GET /api/fs/references`, §4.2): includers, Kconfig sources, Kbuild rules, documents. */
  async getFileReferences(version, path) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/fs/references/${encodeURIComponent(ver)}?path=${encodeURIComponent(path)}`, {}, "files", `fref_${this._vk(ver)}_${path}`);
  }

  /** Definitions of one file for the outline and sticky scroll (`GET /api/fs/outline`, §4.2, G03). */
  async getOutline(version, path) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/fs/outline/${encodeURIComponent(ver)}?path=${encodeURIComponent(path)}`, {}, "files", `outline_${this._vk(ver)}_${path}`);
  }

  async searchFiles(version, query, limit = 50) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query, limit }).toString();
    return this.request(`/api/fs/search/${encodeURIComponent(ver)}?${q}`);
  }

  async getDocLabels(version) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/docs/${encodeURIComponent(ver)}/labels`, {}, "files", `doclabels_${this._vk(ver)}`);
  }

  async getKernelDoc(version, path, options = {}) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ path });
    for (const key of ["export", "internal"]) if (options[key]) params.set(key, "true");
    for (const key of ["functions", "doc", "no_identifiers"]) if (options[key]) params.set(key, options[key]);
    return this.request(`/api/docs/${encodeURIComponent(ver)}/kernel-doc?${params.toString()}`, {}, "files", `kdoc_${this._vk(ver)}_${params.toString()}`);
  }

  // --- Man pages (§5.38): the man-pages repository is named per request (`repo`), so a man tab works
  // while another project is the active one. The header names it too, since X-KI-Repo wins over ?repo=.
  _manRequest(repo, endpoint, params, cacheKey = null) {
    const q = new URLSearchParams(params || {});
    if (repo) q.set("repo", repo);
    const qs = q.toString();
    return this.request(`${endpoint}${qs ? `?${qs}` : ""}`, { networkFirst: true, ...(repo ? { headers: { "X-KI-Repo": repo } } : {}) },
      cacheKey ? "files" : null, cacheKey ? `man_${repo}_${cacheKey}` : null);
  }

  /** `GET /api/man/{v}/index`: every page of a man-pages tag. */
  async getManIndex(repo, version) {
    const ver = this._sanitizeVer(version);
    return this._manRequest(repo, `/api/man/${encodeURIComponent(ver)}/index`, null, `index_${ver}`);
  }

  /** `GET /api/man/{v}/page`: a page by `path`, or by `name` (+ `section`); `kernel`/`kernelRepo` name where the reader came from. */
  async getManPage(repo, version, { path = "", name = "", section = "", kernel = "", kernelRepo = "" } = {}) {
    const ver = this._sanitizeVer(version);
    const params = {};
    if (path) params.path = path;
    else {
      params.name = name;
      if (section) params.section = section;
    }
    if (kernel) params.kernel = kernel;
    if (kernelRepo) params.kernel_repo = kernelRepo;
    return this._manRequest(repo, `/api/man/${encodeURIComponent(ver)}/page`, params, `page_${ver}_${new URLSearchParams(params).toString()}`);
  }

  /** `GET /api/man/tags`: the man-pages release tags, newest first. */
  async getManTags(repo) {
    return this._manRequest(repo, "/api/man/tags", null, "tags");
  }

  /** `GET /api/man/resolve` (current repository = the kernel): our copy of name(section) for `kernel`, or the man7.org URL. */
  async resolveMan(name, section = "", kernel = "") {
    const params = new URLSearchParams({ name });
    if (section) params.set("section", section);
    if (kernel) params.set("kernel", kernel);
    return this.request(`/api/man/resolve?${params.toString()}`, { networkFirst: true });
  }

  /** Release-by-release history of the definition at path:line (`name`: symbol under the cursor). */
  async getSymbolHistory(version, path, line, astId = null, { name = "", includeRc = false, branch = "" } = {}) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ path, line, arch: state.definitionContext().arch });
    if (astId) params.set("ast_id", astId);
    if (name) params.set("name", name);
    if (includeRc) params.set("include_rc", "true");
    if (branch) params.set("branch", branch);
    // The first history of a branch walks its tags (~18 s for master when not pre-warmed)
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/history?${params.toString()}`, { timeoutMs: BRANCH_TAGS_TIMEOUT_MS });
  }

  /** Release-by-release history of every definition of CONFIG_<name>, with property-level changes. */
  async getKconfigHistory(version, name, { includeRc = false, branch = "" } = {}) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams();
    if (includeRc) params.set("include_rc", "true");
    if (branch) params.set("branch", branch);
    const q = params.toString() ? `?${params.toString()}` : "";
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/symbol/${encodeURIComponent(name)}/history${q}`,
      { timeoutMs: BRANCH_TAGS_TIMEOUT_MS });
  }

  /** Commits of from..to that touched lines lineS..lineE (at `to`) of path. */
  async getHistoryCommits({ path, to, from = null, lineS = null, lineE = null }) {
    const params = new URLSearchParams({ path, to });
    if (from) params.set("from", from);
    if (lineS && lineE) {
      params.set("line_s", lineS);
      params.set("line_e", lineE);
    }
    return this.request(`/api/history/commits?${params.toString()}`, { timeoutMs: COMMITS_TIMEOUT_MS }, "files", `histcommits_${params.toString()}`);
  }

  async searchKconfig(version, query, limit = 25, arch = "x86_64") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query, limit, arch }).toString();
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/search?${q}`);
  }

  // --- Filesystem ---
  async getDirectoryTree(version, path = "") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ path }).toString();
    return this.request(`/api/fs/tree/${encodeURIComponent(ver)}?${q}`, {}, "files", `tree3_${this._vk(ver)}_${path}`);
  }

  async getFileContent(version, path) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ path }).toString();
    return this.request(`/api/fs/file/${encodeURIComponent(ver)}?${q}`, {}, "files", `file_${this._vk(ver)}_${path}`);
  }

  /** `git blame` of a file (§4.6); `opts` = {move, copy, ws} or their letters ("mcw", H9). */
  async getFileBlame(version, path, opts = null) {
    const ver = this._sanitizeVer(version);
    const query = blameQuery(opts);
    const letters = typeof opts === "string" ? opts : formatBlameOpts(opts);
    return this.request(
      `/api/commits/${encodeURIComponent(ver)}/blame/${encodeURIComponent(path)}${query ? `?${query}` : ""}`,
      { timeoutMs: opts && (opts.copy || String(opts).includes("c")) ? 45000 : 0 },
      "files",
      `blame2_${this._vk(ver)}_${letters}_${path}`,
    );
  }

  async resolveInclude(version, header, astId = null, currentFile = null, arch = state.definitionContext().arch) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({
      version: ver,
      header: header || "",
      arch,
    });
    if (astId) params.set("ast_id", astId);
    if (currentFile) params.set("current_file", currentFile);
    const cacheKey = `inc_res2_${this._vk(ver)}_${arch}_${header || ''}_${astId || 0}_${currentFile || ''}`;
    return this.request(`/api/fs/resolve_include?${params.toString()}`, {}, "files", cacheKey);
  }

  async getIncludeSymbols(version, astId, options = {}) {
    const ver = this._sanitizeVer(version);
    const targetId = parseInt(astId || "0", 10);
    const params = new URLSearchParams();
    if (options.tagId) params.append("tag_id", options.tagId);
    if (options.filePath) params.append("file_path", options.filePath);
    if (options.line) params.append("line", options.line);
    if (options.header) params.append("header", options.header);

    const queryStr = params.toString() ? `?${params.toString()}` : "";
    const cacheKey = `inc_syms_${this._vk(ver)}_${targetId}_${options.filePath || ""}_${options.line || ""}_${options.header || ""}`;
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/include/${targetId}${queryStr}`, {}, "symbols", cacheKey);
  }

  async getTreemap(version, maxDepth = 3, path = "") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ max_depth: maxDepth, path }).toString();
    return this.request(`/api/version/${encodeURIComponent(ver)}/treemap?${q}`, {}, "files", `treemap_${this._vk(ver)}_${path}_${maxDepth}`);
  }

  // --- Symbols ---
  async searchSymbols(version, query, limit = 50) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query, limit }).toString();
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/search?${q}`);
  }

  /** Definition of a symbol; per-architecture definitions are ranked for `arch` (the KConfig engine's ARCH). */
  /** `fromFile`: the file the symbol is used in; definitions its #include chain reaches win. */
  async getSymbolDetail(version, symbolName, arch = state.definitionContext().arch, fromFile = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ arch });
    if (fromFile) params.set("from_file", fromFile);
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/detail/${encodeURIComponent(symbolName)}?${params}`, {}, "symbols", `sym2_${this._vk(ver)}_${arch}_${symbolName}${fromFile ? `_from_${fromFile}` : ""}`);
  }

  /** Hover card of a symbol (G04): kind, signature, definition, kernel-doc or Kconfig summary; cached like detail. */
  async getSymbolHover(version, symbolName, arch, fromFile = null, signal = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ arch });
    if (fromFile) params.set("from_file", fromFile);
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/hover/${encodeURIComponent(symbolName)}?${params}`, signal ? { signal } : {}, "symbols", `hover1_${this._vk(ver)}_${arch}_${symbolName}${fromFile ? `_from_${fromFile}` : ""}`);
  }

  async getSymbolXref(version, symbolName, limit = null, arch = state.definitionContext().arch) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ arch });
    if (limit) params.set("limit", limit);
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/xref/${encodeURIComponent(symbolName)}?${params}`, {}, "symbols", `xref2_${this._vk(ver)}_${arch}_${symbolName}_${limit || 'all'}`);
  }

  /** Kbuild build condition of each path for an ARCH (expressions evaluated by build_condition.js). */
  async getBuildConditions(version, paths, arch = state.definitionContext().arch) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ arch });
    for (const p of paths) params.append("path", p);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/build?${params}`, {}, "kconfigs", `build1_${this._vk(ver)}_${arch}_${paths.join("|")}`);
  }

  async getAstTree(version, astId) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/ast/${astId}`, {}, "symbols", `ast_${this._vk(ver)}_${astId}`);
  }

  // --- KConfig ---
  /** Query parameters + cache-key suffix for an optional Kconfig toolchain profile. */
  _toolchainParams(params, toolchain) {
    const json = toolchain && Object.keys(toolchain).length ? JSON.stringify(toolchain) : "";
    if (json) params.set("toolchain", json);
    let h = 0;
    for (let i = 0; i < json.length; i++) h = (h * 31 + json.charCodeAt(i)) | 0;
    return json ? `_tc${(h >>> 0).toString(36)}` : "";
  }

  /** Finalized Kconfig model for one ARCH value and toolchain (format 2: decoded by kconfig_engine.js). */
  async getKconfigTree(version, arch = "x86_64", toolchain = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ arch });
    const suffix = this._toolchainParams(params, toolchain);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/tree?${params}`, {}, "kconfigs", `kmodel3_${this._vk(ver)}_${arch}${suffix}`);
  }

  /** Default gcc/clang toolchain profiles of a revision (and whether its Kconfig has $(...) macros). */
  async getKconfigToolchainDefaults(version, arch = "x86_64") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ arch }).toString();
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/toolchain?${q}`, {}, "kconfigs", `ktoolchain_${this._vk(ver)}_${arch}`);
  }

  async getKconfigDefconfigs(version, arch = "x86_64") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ arch }).toString();
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/defconfigs?${q}`, {}, "kconfigs", `defconfigs_${this._vk(ver)}_${arch}`);
  }

  /** Run checkpatch.pl of the configured release tag on a patch (P4; {enabled: false} when the operator has not enabled it). */
  async checkpatch(version, patch) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/tools/${encodeURIComponent(ver)}/checkpatch`, { method: "POST", body: JSON.stringify({ patch }), timeoutMs: 60000 });
  }

  /** Format a multi-file patch series (P3): {patches: [{name, subject, text}], mbox, diffstat, base, check}. */
  async formatPatchSeries(version, payload) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/version/${encodeURIComponent(ver)}/patch/series`, { method: "POST", body: JSON.stringify(payload), timeoutMs: 60000 });
  }

  /** Apply archive messages (series order) to a revision in memory (P1): results per patch, file and hunk. */
  async applyMailPatches(revision, messageIds) {
    return this.request(`/api/mail/apply`, { method: "POST", body: JSON.stringify({ revision, message_ids: messageIds }), timeoutMs: 60000 });
  }

  /** Changed lines of one patched file (blob-diff hunk shape); a 410 means the result was evicted: apply again. */
  async getAppliedFile(applyId, path) {
    return this.request(`/api/mail/apply/${encodeURIComponent(applyId)}/file?${new URLSearchParams({ path })}`);
  }

  /** Configuration fragments of a revision (K2): kernel/configs/*.config and arch/$SRCARCH/configs/*.config. */
  async getKconfigFragments(version, arch = "x86_64") {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/fragments?${new URLSearchParams({ arch })}`, {}, "kconfigs", `fragments_${this._vk(ver)}_${arch}`);
  }

  /** Objects and required symbols of module names (K1; `gates` adds every symbol gating a module-capable object). */
  async getKbuildModules(version, arch, names, { gates = false, onProgress = null, signal = null } = {}) {
    const ver = this._sanitizeVer(version);
    return this.requestJob(`/api/kconfig/${encodeURIComponent(ver)}/kbuild/modules`, { method: "POST", body: { arch, names: names.slice(0, 2000), gates }, onProgress, signal, timeoutMs: 120000 });
  }

  /** Defconfig text (empty filePath: the arch's default, i.e. what `make ARCH=<arch> defconfig` loads). */
  async getKconfigDefconfigContent(version, filePath, arch = "x86_64", toolchain = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ file_path: filePath || "", arch });
    const suffix = this._toolchainParams(params, toolchain);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/defconfig?${params}`, {}, "kconfigs", `defconfig3_${this._vk(ver)}_${arch}_${filePath || "default"}${suffix}`);
  }

  async getKconfigSymbol(version, symbolName, arch = "x86_64", toolchain = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ arch });
    const suffix = this._toolchainParams(params, toolchain);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/symbol/${encodeURIComponent(symbolName)}?${params}`, {}, "kconfigs", `ksym3_${this._vk(ver)}_${arch}_${symbolName}${suffix}`);
  }

  /** Kbuild rules, source references and Kconfig references of CONFIG_<symbolName>. */
  async getKconfigUsage(version, symbolName, arch = "x86_64", toolchain = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ arch });
    const suffix = this._toolchainParams(params, toolchain);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/symbol/${encodeURIComponent(symbolName)}/usage?${params}`, {}, "kconfigs", `kusage2_${this._vk(ver)}_${arch}_${symbolName}${suffix}`);
  }

  async autosolveKconfig(version, payload) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/autosolve`, {
      method: "POST",
      body: JSON.stringify(payload)
    });
  }

  async getKconfigPresets(version) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/presets`, {}, "kconfigs", `presets2_${this._vk(ver)}`);
  }

  async getKconfigGraph(version, arch = "x86_64", symbol = null, toolchain = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ arch });
    if (symbol) params.set("symbol", symbol);
    const suffix = this._toolchainParams(params, toolchain);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/graph?${params.toString()}`, {}, "kconfigs", `kgraph4_${this._vk(ver)}_${arch}_${symbol || 'all'}${suffix}`);
  }

  // --- Maintainers ---
  async getMaintainersOverview(version, query = "") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query }).toString();
    const cacheStore = !query ? "maintainers" : null;
    const cacheKey = !query ? `m_overview_${this._vk(ver)}` : null;
    return this.request(`/api/maintainers/${encodeURIComponent(ver)}/overview?${q}`, {}, cacheStore, cacheKey);
  }

  async getMaintainerSection(version, secId) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/maintainers/${encodeURIComponent(ver)}/section/${secId}`, {}, "maintainers", `m_sec_${this._vk(ver)}_${secId}`);
  }

  async getPersonProfile(version, personIdOrEmail) {
    const ver = this._sanitizeVer(version);
    const idStr = String(personIdOrEmail || "").trim();
    if (idStr.includes("@") || idStr.includes("/") || idStr.includes("<")) {
      const q = new URLSearchParams({ email: idStr }).toString();
      return this.request(`/api/maintainers/${encodeURIComponent(ver)}/person?${q}`, {}, "maintainers", `m_person_${this._vk(ver)}_${idStr}`);
    }
    return this.request(`/api/maintainers/${encodeURIComponent(ver)}/person/${encodeURIComponent(idStr)}`, {}, "maintainers", `m_person_${this._vk(ver)}_${idStr}`);
  }

  async matchMaintainers(version, patchText, touchedFiles = []) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/maintainers/${encodeURIComponent(ver)}/match`, {
      method: "POST",
      body: JSON.stringify({ patch: patchText, touched_files: touchedFiles })
    });
  }

  async getCredits(version, query = "") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query }).toString();
    const cacheStore = !query ? "maintainers" : null;
    const cacheKey = !query ? `m_credits_${this._vk(ver)}` : null;
    return this.request(`/api/maintainers/${encodeURIComponent(ver)}/credits?${q}`, {}, cacheStore, cacheKey);
  }

  async getDevelopers(version, options = {}) {
    const ver = this._sanitizeVer(version);
    const query = options.query || options.q || "";
    const role = options.role || "all";
    const sort = options.sort || "activity";
    const params = new URLSearchParams();
    if (query) params.set("q", query);
    if (role && role !== "all") params.set("role", role);
    if (sort) params.set("sort", sort);
    const qs = params.toString() ? `?${params.toString()}` : "";
    const cacheStore = (!query && role === "all" && sort === "activity") ? "maintainers" : null;
    const cacheKey = (!query && role === "all" && sort === "activity") ? `m_devs_${this._vk(ver)}` : null;
    return this.request(`/api/maintainers/${encodeURIComponent(ver)}/developers${qs}`, {}, cacheStore, cacheKey);
  }

  // --- Commits ---
  async getCommits(version, page = 1, limit = 50, query = "") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ page, limit, q: query }).toString();
    const cacheStore = (!query && page === 1) ? "commits" : null;
    const cacheKey = (!query && page === 1) ? `commits_${this._vk(ver)}_p1` : null;
    return this.request(`/api/commits/${encodeURIComponent(ver)}/list?${q}`, { timeoutMs: COMMITS_TIMEOUT_MS }, cacheStore, cacheKey);
  }

  async getCommitDetail(version, commitIdOrHash) {
    const ver = this._sanitizeVer(version);
    // High fetch priority: the commit on screen goes ahead of the list/search requested beside it
    return this.request(`/api/commits/${encodeURIComponent(ver)}/detail/${encodeURIComponent(commitIdOrHash)}`, { priority: "high", timeoutMs: COMMITS_TIMEOUT_MS }, "commits", `commit_${this._vk(ver)}_${commitIdOrHash}`);
  }

  async getCommitDiff(version, commitHash, context = 3) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/commits/${encodeURIComponent(ver)}/diff/${encodeURIComponent(commitHash)}?context=${context}`, { timeoutMs: COMMITS_TIMEOUT_MS }, "commits", `cdiff_${commitHash}_${context}`);
  }

  async getCommitTimeline(version, limit = 100, direction = "past", cursorId = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ limit, direction });
    if (cursorId !== null && cursorId !== undefined) params.set("cursor_id", cursorId);
    return this.request(`/api/commits/${encodeURIComponent(ver)}/timeline?${params.toString()}`, { timeoutMs: COMMITS_TIMEOUT_MS });
  }

  async searchCommits(version, query, limit = 25) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query, limit }).toString();
    return this.request(`/api/commits/${encodeURIComponent(ver)}/search?${q}`, { timeoutMs: COMMITS_TIMEOUT_MS });
  }

  async getFileHistory(version, filePath, limit = 50) {
    const ver = this._sanitizeVer(version);
    const cleanPath = filePath.replace(/^\/+/, "");
    return this.request(`/api/commits/${encodeURIComponent(ver)}/history/${encodeURIComponent(cleanPath)}?limit=${limit}`, { timeoutMs: COMMITS_TIMEOUT_MS });
  }

  async formatPatch(version, payload) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/version/${encodeURIComponent(ver)}/patch/format`, {
      method: "POST",
      body: JSON.stringify(payload)
    });
  }

  // --- Tools ---
  async getPaholeLayout(version, structName) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/tools/${encodeURIComponent(ver)}/pahole/${encodeURIComponent(structName)}`, {}, "tools", `pahole_${this._vk(ver)}_${structName}`);
  }

  async getCallgraph(version, functionName) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/tools/${encodeURIComponent(ver)}/callgraph/${encodeURIComponent(functionName)}`, { timeoutMs: CALLGRAPH_TIMEOUT_MS }, "tools", `callgraph_${this._vk(ver)}_${functionName}`);
  }

  async getVersionsDiff(versionA, versionB) {
    const va = this._sanitizeVer(versionA);
    const vb = this._sanitizeVer(versionB);
    const q = new URLSearchParams({ version_a: va, version_b: vb }).toString();
    return this.request(`/api/tools/diff/versions?${q}`, { timeoutMs: VERSIONS_DIFF_TIMEOUT_MS }, "tools", `diff3_${this._vk(va)}_${this._vk(vb)}`);
  }

  async getFileDiff(versionA, versionB, path, oldPath = "", similarity = 50) {
    const va = this._sanitizeVer(versionA);
    const vb = this._sanitizeVer(versionB);
    const params = new URLSearchParams({ version_a: va, version_b: vb, path });
    if (oldPath && oldPath !== path) params.set("old_path", oldPath);
    // A rename below Git's default 50% only shows as one diff when git diff -M uses its score
    const sim = Math.max(1, Math.min(50, Math.round(similarity) || 50));
    if (sim !== 50) params.set("similarity", String(sim));
    return this.request(`/api/tools/diff/file?${params.toString()}`, {}, "tools", `fdiff_${this._vk(va)}_${this._vk(vb)}_${sim}_${oldPath || ""}_${path}`);
  }

  /** Changed lines (-U0 hunks) between any two files at any two revisions (code view "⇄ Diff"). */
  async getBlobDiff(versionA, pathA, versionB, pathB) {
    const va = this._sanitizeVer(versionA);
    const vb = this._sanitizeVer(versionB);
    const params = new URLSearchParams({ version_a: va, path_a: pathA, version_b: vb, path_b: pathB });
    return this.request(`/api/tools/diff/blobs?${params.toString()}`, { timeoutMs: 45000 }, "tools", `bdiff_${this._vk(va)}_${this._vk(vb)}_${pathA}_${pathB}`);
  }

  /** Paths with the same content at both revisions (the diff's unchanged count; ~29k at v3.0…v3.1). */
  async getUnchangedFiles(versionA, versionB) {
    const va = this._sanitizeVer(versionA);
    const vb = this._sanitizeVer(versionB);
    const q = new URLSearchParams({ version_a: va, version_b: vb }).toString();
    return this.request(`/api/tools/diff/unchanged?${q}`, { timeoutMs: VERSIONS_DIFF_TIMEOUT_MS }, "tools", `unchanged_${this._vk(va)}_${this._vk(vb)}`);
  }

  /** Exhaustive rename search (every deleted x added pair under `path`); can take tens of seconds. */
  async searchRenames(versionA, versionB, path = "", similarity = 50) {
    const va = this._sanitizeVer(versionA);
    const vb = this._sanitizeVer(versionB);
    const params = new URLSearchParams({ version_a: va, version_b: vb, path, similarity: String(similarity) });
    return this.request(`/api/tools/diff/renames?${params.toString()}`, { timeoutMs: RENAME_SEARCH_TIMEOUT_MS }, "tools",
      `renames_${this._vk(va)}_${this._vk(vb)}_${similarity}_${path}`);
  }

  async getKconfigDiff(versionA, versionB) {
    const va = this._sanitizeVer(versionA);
    const vb = this._sanitizeVer(versionB);
    return this.request(`/api/diff/kconfig/${encodeURIComponent(va)}/${encodeURIComponent(vb)}`, {}, "tools", `kdiff2_${this._vk(va)}_${this._vk(vb)}`);
  }

  // --- Mailing List & LKML ---
  async getMailStatus({ force = false } = {}) {
    const now = Date.now();
    if (!force && this._mailStatusCache && (now - (this._mailStatusCacheTime || 0) < 15000)) {
      return this._mailStatusCache;
    }
    const data = await this.request("/api/mail/status", { networkFirst: true }, "mail", "daemon_status");
    this._mailStatusCache = data;
    this._mailStatusCacheTime = now;
    return data;
  }

  async getMailMessage(midOrNum, { networkFirst = true } = {}) {
    const target = encodeURIComponent(String(midOrNum).trim());
    return this.request(
      `/api/mail/message?mid=${target}`,
      { networkFirst },
      "mail",
      `msg_${target}`
    );
  }

  async getMailThread(tid, mid = "", { networkFirst = true } = {}) {
    const params = new URLSearchParams();
    if (tid) params.set("tid", tid);
    if (mid) params.set("mid", mid);
    const key = `thread_${tid || 0}_${mid || ""}`;
    return this.request(
      `/api/mail/thread?${params.toString()}`,
      { networkFirst },
      "mail",
      key
    );
  }

  async getMailThreadVersions(tid, mid = "", { networkFirst = true } = {}) {
    const params = new URLSearchParams();
    if (tid) params.set("tid", tid);
    if (mid) params.set("mid", mid);
    const key = `versions_${tid || 0}_${mid || ""}`;
    return this.request(
      `/api/mail/versions?${params.toString()}`,
      { networkFirst },
      "mail",
      key
    );
  }

  /**
   * Mailing list search. The archive grows while it is fetched, so the server is always asked first;
   * only the browse listing (no query) is kept for offline use, not every query typed in the palette.
   */
  async searchMailingList(query = "", { page = 1, limit = 50, filterType = "all" } = {}) {
    const params = new URLSearchParams({
      q: query,
      page,
      limit,
      filter: filterType,
    });
    return this.request(
      `/api/mail/search?${params.toString()}`,
      { networkFirst: true },
      query ? null : "mail",
      query ? null : `search__${page}_${limit}_${filterType}`
    );
  }

  async resolveCommitMail(version, commitIdOrHash) {
    const ver = this._sanitizeVer(version);
    const cid = encodeURIComponent(String(commitIdOrHash).trim());
    return this.request(`/api/commits/${encodeURIComponent(ver)}/resolve_mail/${cid}`, {
      method: "POST",
      timeoutMs: COMMITS_TIMEOUT_MS,
    });
  }
}

export const api = new ApiClient();
api._root = api;

/**
 * Give a tab's view its repository-scoped client: `view.api` resolves the tab by id at every call, so a
 * view keeps addressing its tab's repository after a project switch moved it, and a view of an inactive
 * tab or pane never follows the active one (tabs.js binds every per-tab view, §5.18).
 */
export function bindViewApi(view, tabId, fallbackTab = null) {
  if (!view || Object.prototype.hasOwnProperty.call(view, "api")) return view;
  Object.defineProperty(view, "api", {
    configurable: true,
    get: () => api.forTab(state.findTab(tabId) || fallbackTab),
  });
  return view;
}


