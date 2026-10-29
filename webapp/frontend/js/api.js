/**
 * api.js - Centralized API client for KernelInfo-Parser.
 * Handles timeouts, retries, and offline caching via IndexedDB.
 */
import { localDB } from "./db.js";
import { state } from "./state.js";

class ApiClient {
  constructor() {
    this.baseUrl = "";
    this.isOnline = navigator.onLine;
    this.activeRequests = 0;
    // Version name -> stamp (commit + parse completion) from /api/versions and /api/branches; part of every
    // cache key of version data, so a re-parsed version or a moved branch never serves older cached data
    this.versionStamps = {};
    // Called with the X-KIP-Sync header of every API response (js/sync.js)
    this.onSyncHeader = null;
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

  /** Repository selected in the header; sent with every request and namespacing offline cache keys. */
  setRepository(name) {
    this.repo = name || "";
  }

  async request(endpoint, options = {}, cacheStore = null, cacheKey = null) {
    const url = `${this.baseUrl}${endpoint}`;
    if (cacheKey && this.repo) cacheKey = `${this.repo}::${cacheKey}`;

    // 1. Cache-First: If cache info provided and not a mutating method, return cached immediately
    //    (catalog endpoints pass networkFirst so newly parsed versions/repositories show up)
    const { networkFirst, ...fetchOptions } = options;
    options = fetchOptions;
    if (!networkFirst && cacheStore && cacheKey && (!options.method || options.method === "GET")) {
      const cached = await localDB.get(cacheStore, cacheKey);
      if (cached !== null && cached !== undefined) {
        // If cached mail item is failed or missing body/raw, bypass stale cache to fetch fresh
        if (cacheStore === "mail" && cached && (cached.available === false || cached.error || (!cached.raw && !cached.body && !cached.messages && !cached.results && !cached.versions))) {
          // Stale or incomplete mail entry, bypass cache
        } else {
          return cached;
        }
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
          ...(this.repo ? { "X-KI-Repo": this.repo } : {}),
          ...(options.headers || {})
        }
      });
      clearTimeout(timer);
      const syncHeader = response.headers.get("X-KIP-Sync");
      if (syncHeader && this.onSyncHeader) this.onSyncHeader(syncHeader);

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
        // Do not cache failed or empty mail items in IndexedDB
        if (cacheStore !== "mail" || (data && data.available !== false && !data.error && (data.raw || data.body || data.messages || data.results || data.versions))) {
          localDB.set(cacheStore, cacheKey, data).catch((e) => console.warn("Cache write failed:", e));
        }
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

  /** Cache-key form of a version: "<name>@<stamp>~" once its stamp is known (see versionStamps). */
  _vk(v) {
    const stamp = this.versionStamps[v];
    return stamp ? `${v}@${stamp}~` : v;
  }

  /** Server change tokens {db, frontend, parsed, git, repo}; never cached. `fresh` makes the server recompute them. */
  async getSync({ fresh = false } = {}) {
    const res = await fetch(`${this.baseUrl}/api/sync${fresh ? "?fresh=true" : ""}`, {
      headers: { Accept: "application/json", ...(this.repo ? { "X-KI-Repo": this.repo } : {}) },
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  _sanitizeVer(v) {
    if (!v || typeof v !== "string" || v.includes("object")) {
      return "v3.0";
    }
    return v;
  }

  // --- Versions & Projects ---
  async getProjects() {
    return this.request("/api/projects", { networkFirst: true }, "files", "app_projects2");
  }

  async getBranches() {
    return this.request("/api/branches", { networkFirst: true }, "files", "app_branches");
  }

  async getVersions(includeRc = true) {
    const q = `?include_rc=${includeRc ? "true" : "false"}`;
    return this.request(`/api/versions${q}`, { networkFirst: true }, "files", `app_versions_${includeRc ? "rc" : "rel"}`);
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

  /** Release-by-release history of the definition at path:line (`name`: symbol under the cursor). */
  async getSymbolHistory(version, path, line, astId = null, { name = "", includeRc = false } = {}) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ path, line, arch: state.definitionContext().arch });
    if (astId) params.set("ast_id", astId);
    if (name) params.set("name", name);
    if (includeRc) params.set("include_rc", "true");
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/history?${params.toString()}`);
  }

  /** Release-by-release history of every definition of CONFIG_<name>, with property-level changes. */
  async getKconfigHistory(version, name, { includeRc = false } = {}) {
    const ver = this._sanitizeVer(version);
    const q = includeRc ? "?include_rc=true" : "";
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/symbol/${encodeURIComponent(name)}/history${q}`);
  }

  /** Commits of from..to that touched lines lineS..lineE (at `to`) of path. */
  async getHistoryCommits({ path, to, from = null, lineS = null, lineE = null }) {
    const params = new URLSearchParams({ path, to });
    if (from) params.set("from", from);
    if (lineS && lineE) {
      params.set("line_s", lineS);
      params.set("line_e", lineE);
    }
    return this.request(`/api/history/commits?${params.toString()}`, {}, "files", `histcommits_${params.toString()}`);
  }

  async searchKconfig(version, query, limit = 25, arch = "x86_64") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query, limit, arch }).toString();
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/search?${q}`);
  }

  async importKconfig(version, content, arch = null) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/import`, { method: "POST", body: JSON.stringify({ content, ...(arch ? { arch } : {}) }) });
  }

  async validateKconfig(version, symbols, arch = null) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/validate`, { method: "POST", body: JSON.stringify({ symbols, ...(arch ? { arch } : {}) }) });
  }

  async diffKconfigConfigs(version, activeConfig, customConfig) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/diff-config`, {
      method: "POST",
      body: JSON.stringify({ active_config: activeConfig, custom_config: customConfig })
    });
  }

  // --- Filesystem ---
  async getDirectoryTree(version, path = "", depth = 2) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ path, depth }).toString();
    return this.request(`/api/fs/tree/${encodeURIComponent(ver)}?${q}`, {}, "files", `tree2_${this._vk(ver)}_${path}_${depth}`);
  }

  async getFileContent(version, path) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ path }).toString();
    return this.request(`/api/fs/file/${encodeURIComponent(ver)}?${q}`, {}, "files", `file_${this._vk(ver)}_${path}`);
  }

  async getFileBlame(version, path) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/commits/${encodeURIComponent(ver)}/blame/${encodeURIComponent(path)}`, {}, "files", `blame_${this._vk(ver)}_${path}`);
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

  async getCompileCommands(version, arch = "x86") {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ arch }).toString();
    return this.request(`/api/version/${encodeURIComponent(ver)}/compile_commands?${q}`);
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

  async lookupSymbols(version, query, limit = 20) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query, limit }).toString();
    return this.request(`/api/symbols/${encodeURIComponent(ver)}/lookup?${q}`);
  }

  async getTagTimeline(tagId, { includeRc = false } = {}) {
    return this.request(`/api/tag/${encodeURIComponent(tagId)}/timeline${includeRc ? "?include_rc=true" : ""}`);
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

  async getKconfigGraph(version, arch = "x86_64", symbol = null, depth = 2, toolchain = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ arch, depth });
    if (symbol) params.set("symbol", symbol);
    const suffix = this._toolchainParams(params, toolchain);
    return this.request(`/api/kconfig/${encodeURIComponent(ver)}/graph?${params.toString()}`, {}, "kconfigs", `kgraph3_${this._vk(ver)}_${arch}_${symbol || 'all'}_${depth}${suffix}`);
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
    return this.request(`/api/commits/${encodeURIComponent(ver)}/list?${q}`, {}, cacheStore, cacheKey);
  }

  async getCommitDetail(version, commitIdOrHash) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/commits/${encodeURIComponent(ver)}/detail/${encodeURIComponent(commitIdOrHash)}`, {}, "commits", `commit_${this._vk(ver)}_${commitIdOrHash}`);
  }

  async getCommitDiff(version, commitHash, context = 3) {
    const ver = this._sanitizeVer(version);
    return this.request(`/api/commits/${encodeURIComponent(ver)}/diff/${encodeURIComponent(commitHash)}?context=${context}`, {}, "commits", `cdiff_${commitHash}_${context}`);
  }

  async getCommitTimeline(version, limit = 100, direction = "past", cursorId = null) {
    const ver = this._sanitizeVer(version);
    const params = new URLSearchParams({ limit, direction });
    if (cursorId !== null && cursorId !== undefined) params.set("cursor_id", cursorId);
    return this.request(`/api/commits/${encodeURIComponent(ver)}/timeline?${params.toString()}`);
  }

  async searchCommits(version, query, limit = 25) {
    const ver = this._sanitizeVer(version);
    const q = new URLSearchParams({ q: query, limit }).toString();
    return this.request(`/api/commits/${encodeURIComponent(ver)}/search?${q}`);
  }

  async getFileHistory(version, filePath, limit = 50) {
    const ver = this._sanitizeVer(version);
    const cleanPath = filePath.replace(/^\/+/, "");
    return this.request(`/api/commits/${encodeURIComponent(ver)}/history/${encodeURIComponent(cleanPath)}?limit=${limit}`);
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
    return this.request(`/api/tools/${encodeURIComponent(ver)}/callgraph/${encodeURIComponent(functionName)}`, {}, "tools", `callgraph_${this._vk(ver)}_${functionName}`);
  }

  async getVersionsDiff(versionA, versionB) {
    const va = this._sanitizeVer(versionA);
    const vb = this._sanitizeVer(versionB);
    const q = new URLSearchParams({ version_a: va, version_b: vb }).toString();
    return this.request(`/api/tools/diff/versions?${q}`, {}, "tools", `diff2_${this._vk(va)}_${this._vk(vb)}`);
  }

  async getFileDiff(versionA, versionB, path, oldPath = "") {
    const va = this._sanitizeVer(versionA);
    const vb = this._sanitizeVer(versionB);
    const params = new URLSearchParams({ version_a: va, version_b: vb, path });
    if (oldPath && oldPath !== path) params.set("old_path", oldPath);
    return this.request(`/api/tools/diff/file?${params.toString()}`, {}, "tools", `fdiff_${this._vk(va)}_${this._vk(vb)}_${oldPath || ""}_${path}`);
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

  async searchMailingList(query = "", { page = 1, limit = 50, filterType = "all", networkFirst = false } = {}) {
    const params = new URLSearchParams({
      q: query,
      page,
      limit,
      filter: filterType,
    });
    return this.request(
      `/api/mail/search?${params.toString()}`,
      { networkFirst },
      "mail",
      `search_${query}_${page}_${limit}_${filterType}`
    );
  }

  async resolveCommitMail(version, commitIdOrHash) {
    const ver = this._sanitizeVer(version);
    const cid = encodeURIComponent(String(commitIdOrHash).trim());
    return this.request(`/api/commits/${encodeURIComponent(ver)}/resolve_mail/${cid}`, {
      method: "POST",
    });
  }
}

export const api = new ApiClient();


