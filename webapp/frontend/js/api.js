/**
 * api.js - Centralized API client for KernelInfo-Parser.
 * Handles timeouts, retries, and offline caching via IndexedDB.
 */
import { localDB } from "./db.js";

class ApiClient {
  constructor() {
    this.baseUrl = "";
    this.isOnline = navigator.onLine;
    this.activeRequests = 0;
    this._showTimer = null;

    window.addEventListener("online", () => {
      this.isOnline = true;
      document.dispatchEvent(new CustomEvent("app:online"));
    });
    window.addEventListener("offline", () => {
      this.isOnline = false;
      document.dispatchEvent(new CustomEvent("app:offline"));
    });
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
    document.dispatchEvent(new CustomEvent("app:loading", { detail: { loading: active } }));
  }

  /**
   * Synchronize local cache with the active database instance hash.
   * If the database was dropped and remade, local data caches are purged automatically.
   */
  async checkDbSync() {
    try {
      const serverInst = await this.getDbInstance();
      if (!serverInst || !serverInst.instance_hash) return null;

      const cachedHash = await localDB.get("meta", "db_instance_hash");
      if (cachedHash !== serverInst.instance_hash) {
        console.info(`[ApiClient] DB instance changed (${cachedHash || 'none'} -> ${serverInst.instance_hash}). Invalidating local cache.`);
        await localDB.clearDataStores();
        await localDB.set("meta", "db_instance_hash", serverInst.instance_hash);
        await localDB.set("meta", "db_instance_created_at", serverInst.created_at);
        document.dispatchEvent(new CustomEvent("app:cache-reset", { detail: serverInst }));
      }
      return serverInst;
    } catch (e) {
      console.warn("[ApiClient] DB instance sync check failed:", e);
      return null;
    }
  }

  async getDbInstance() {
    this._onRequestStart();
    try {
      const res = await fetch(`${this.baseUrl}/api/db/instance`, {
        headers: { Accept: "application/json" }
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } finally {
      this._onRequestEnd();
    }
  }

  async request(endpoint, options = {}, cacheStore = null, cacheKey = null) {
    const url = `${this.baseUrl}${endpoint}`;

    // 1. Cache-First: If cache info provided and not a mutating method, return cached immediately
    if (cacheStore && cacheKey && (!options.method || options.method === "GET")) {
      const cached = await localDB.get(cacheStore, cacheKey);
      if (cached !== null && cached !== undefined) {
        return cached;
      }
    }

    // 2. Fetch with 10-second timeout
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    this._onRequestStart();

    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          ...(options.headers || {})
        }
      });
      clearTimeout(timer);

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
        throw new Error(errMsg);
      }

      const data = await response.json();

      // 3. Cache successful response in IndexedDB
      if (cacheStore && cacheKey) {
        localDB.set(cacheStore, cacheKey, data).catch((e) => console.warn("Cache write failed:", e));
      }

      return data;
    } catch (err) {
      clearTimeout(timer);

      // 4. On network failure, fallback to cache if available
      if (cacheStore && cacheKey) {
        const cached = await localDB.get(cacheStore, cacheKey);
        if (cached !== null && cached !== undefined) {
          console.info(`Serving offline cache for ${cacheKey}`);
          return cached;
        }
      }
      throw err;
    } finally {
      this._onRequestEnd();
    }
  }

  _sanitizeVer(v) {
    if (!v || typeof v !== "string" || v.includes("object")) {
      return "v3.0";
    }
    return v;
  }

  // --- Versions ---
  async getVersions() {
    return this.request("/api/versions", {}, "files", "app_versions");
  }

  // --- Filesystem ---
  async getDirectoryTree(version, path = "", depth = 2) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ path, depth }).toString();
    return this.request(`/api/fs/tree/${encodeURIComponent(ver)}?${q}`, {}, "files", `tree_${ver}_${path}_${depth}`);
  }

  async getFileContent(version, path) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ path }).toString();
    return this.request(`/api/fs/file/${encodeURIComponent(ver)}?${q}`, {}, "files", `file_${ver}_${path}`);
  }

  async getFileBlame(version, path) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/commits/${encodeURIComponent(ver)}/blame/${encodeURIComponent(path)}`, {}, "files", `blame_${ver}_${path}`);
  }

  async resolveInclude(version, header, astId = null, currentFile = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({
      version: ver,
      header: header || "",
    });
    if (astId) params.set("ast_id", astId);
    if (currentFile) params.set("current_file", currentFile);
    const cacheKey = `inc_res_${ver}_${header || ''}_${astId || 0}_${currentFile || ''}`;
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
    const cacheKey = `inc_syms_${ver}_${targetId}_${options.filePath || ""}_${options.line || ""}_${options.header || ""}`;
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/include/${targetId}${queryStr}`, {}, "symbols", cacheKey);
  }

  // --- Symbols ---
  async searchSymbols(version, query, limit = 50) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query, limit }).toString();
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/search?${q}`);
  }

  async getSymbolDetail(version, symbolName) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/detail/${encodeURIComponent(symbolName)}`, {}, "symbols", `sym_${ver}_${symbolName}`);
  }

  async getSymbolXref(version, symbolName, limit = null) {
    const ver = this._sanitizeVer(version);
    const q = limit ? `?limit=${limit}` : "";
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/xref/${encodeURIComponent(symbolName)}${q}`, {}, "symbols", `xref_${ver}_${symbolName}_${limit || 'all'}`);
  }

  async getAstTree(version, astId) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/ast/${astId}`, {}, "symbols", `ast_${ver}_${astId}`);
  }

  // --- KConfig ---
  async getKconfigTree(version, arch = "x86") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ arch }).toString();
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/tree?${q}`, {}, "kconfigs", `ktree_${ver}_${arch}`);
  }

  async getKconfigDefconfigs(version, arch = "x86") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ arch }).toString();
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/defconfigs?${q}`, {}, "kconfigs", `defconfigs_${ver}_${arch}`);
  }

  async getKconfigDefconfigContent(version, filePath) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ file_path: filePath }).toString();
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/defconfig?${q}`, {}, "kconfigs", `defconfig_content_${ver}_${filePath}`);
  }

  async getKconfigSymbol(version, symbolName) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/symbol/${encodeURIComponent(symbolName)}`, {}, "kconfigs", `ksym_${ver}_${symbolName}`);
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
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/presets`, {}, "kconfigs", `presets_${ver}`);
  }

  // --- Maintainers ---
  async getMaintainersOverview(version, query = "") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query }).toString();
    const cacheStore = !query ? "maintainers" : null;
    const cacheKey = !query ? `m_overview_${ver}` : null;
    return this.request(`/api/maintainers/${encodeURIComponent(ver)}/overview?${q}`, {}, cacheStore, cacheKey);
  }

  async getMaintainerSection(version, secId) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/maintainers/${encodeURIComponent(ver)}/section/${secId}`, {}, "maintainers", `m_sec_${ver}_${secId}`);
  }

  async getPersonProfile(version, personIdOrEmail) {
    const ver = this._sanitizeVer(version);
    const idStr = String(personIdOrEmail || "").trim();
    if (idStr.includes("@") || idStr.includes("/") || idStr.includes("<")) {
      const q = new URLSearchParams({ email: idStr }).toString();
      return this.request(`/api/maintainers/${encodeURIComponent(ver)}/person?${q}`, {}, "maintainers", `m_person_${ver}_${idStr}`);
    }
    return this.request(`/api/maintainers/${encodeURIComponent(ver)}/person/${encodeURIComponent(idStr)}`, {}, "maintainers", `m_person_${ver}_${idStr}`);
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
    const cacheKey = !query ? `m_credits_${ver}` : null;
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
    const cacheKey = (!query && role === "all" && sort === "activity") ? `m_devs_${ver}` : null;
    return this.request(`/api/maintainers/${encodeURIComponent(ver)}/developers${qs}`, {}, cacheStore, cacheKey);
  }

  // --- Commits ---
  async getCommits(version, page = 1, limit = 50, query = "") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ page, limit, q: query }).toString();
    const cacheStore = (!query && page === 1) ? "commits" : null;
    const cacheKey = (!query && page === 1) ? `commits_${ver}_p1` : null;
    return this.request(`/api/commits/${encodeURIComponent(ver)}/list?${q}`, {}, cacheStore, cacheKey);
  }

  async getCommitDetail(version, commitIdOrHash) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/commits/${encodeURIComponent(ver)}/detail/${encodeURIComponent(commitIdOrHash)}`, {}, "commits", `commit_${ver}_${commitIdOrHash}`);
  }

  // --- Tools ---
  async getPaholeLayout(version, structName) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/tools/${encodeURIComponent(ver)}/pahole/${encodeURIComponent(structName)}`, {}, "tools", `pahole_${ver}_${structName}`);
  }

  async getCallgraph(version, functionName) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/tools/${encodeURIComponent(ver)}/callgraph/${encodeURIComponent(functionName)}`, {}, "tools", `callgraph_${ver}_${functionName}`);
  }

  async getVersionsDiff(versionA, versionB) {
    const va = this._sanitizeVer(versionA);
    const vb = this._sanitizeVer(versionB);
    const q = new URLSearchParams({ version_a: va, version_b: vb }).toString();
    return this.request(`/api/tools/diff/versions?${q}`, {}, "tools", `diff_${va}_${vb}`);
  }
}

export const api = new ApiClient();

