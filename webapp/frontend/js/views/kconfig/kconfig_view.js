/**
 * kconfig_view.js - KConfig explorer: web menuconfig, symbol inspector and .config manager.
 *
 * The configuration model of the selected ARCH comes from GET /api/kconfig/{version}/tree and is
 * evaluated in the browser by KconfigEngine (scripts/kconfig semantics). While the engine is on,
 * every edit goes through kconfig's own setters, so only valid configurations can be produced and
 * exported. With the engine off, values are edited raw (no constraints) for exploration.
 *
 * One configuration is kept per "version|ARCH" profile (state.kconfigProfiles), plus one per named
 * copy ("version|ARCH|name", **Duplicate** / **Rename…**), so several configurations of the same
 * ARCH and revision can be kept and compared side by side. A tab running its engine publishes its
 * resolved configuration under its own tab id (state.kconfigEngines), and the code tabs following
 * that engine dim their #ifdef branches with it.
 *
 * The engine is off by default and belongs to the tab that enables it (state.kconfigEngineTabIds).
 * Several tabs may run their engine at the same time: each one resolves its own configuration and
 * publishes it under its tab id, and a code tab picks which of them it follows (code toolbar, §5.16).
 * Disabling it keeps every value (the tab's profile is untouched; editing just stops being constrained).
 */
import { api } from "../../api.js";
import { state, kconfigProfileKey } from "../../state.js";
import { toast } from "../../components/toast.js";
import { KconfigEngine } from "./kconfig_engine.js";
import { KconfigParser } from "./kconfig_parser.js";
import { MenuconfigTui } from "./menuconfig_tui.js";
import { openModal, escapeHtml, downloadText } from "../../components/modal.js";
import { showKconfigHistoryModal } from "../../components/symbol_history_modal.js";

const DEFAULT_ARCH = "x86_64";
const TRI_CHAR = ["n", "m", "y"];
const SEARCH_LIMIT = 150;

export class KconfigView {
  constructor() {
    this.engine = new KconfigEngine();
    this.showUnmet = false;
    this.currentArch = DEFAULT_ARCH;
    this.currentVersion = "v3.0";
    this.selectedSymbol = null;
    this.selectedNodeId = null;
    // Text typed in the tree filter box; kept on the tab and in the URL so a copied link
    // reopens this tab on the same search (§5.26)
    this.searchQuery = "";
    this.expandedNodeIds = new Set();
    this.containerEl = null;
    this.archPresets = [];
    this.detailToken = 0;
    // Raw assignments (bare names) while the engine is off; resolved values while it is on
    this.rawValues = {};
    // Tab currently rendered by this view, and whether the engine change in flight is its own
    this.tabId = null;
    // Name telling this configuration apart from other copies of the same revision and ARCH
    this.configName = "";
    this.selfEngineChange = false;
    // "version|ARCH|name" this tab's values were last loaded against (a switch reports what moved)
    this.resolvedProfileKey = "";

    // This tab's engine can be switched off elsewhere (the command palette's *Disable every KConfig
    // engine*, a tab being closed): follow it so the toolbar and the tree never claim an engine this
    // tab lost. Engines of other tabs are none of this view's business.
    this.unsubscribeEngine = state.subscribe("kconfig:engine:changed", ({ tabId, remote } = {}) => {
      if (this.selfEngineChange) return;
      if (remote) return; // an engine of another session: this tab neither gained nor lost its own
      if (!this.containerEl || !this.containerEl.isConnected) return;
      if (!state.findTab(this.tabId)) return; // this tab is being closed
      if (tabId && tabId !== this.tabId) return; // another tab's engine
      this.followEngineChange();
    });

    // The command palette asks a tab to take the engine; taking it goes through the same dialog
    this.unsubscribeEngineRequest = state.subscribe("kconfig:engine:requested", (tabId) => {
      if (tabId !== this.tabId) return;
      if (!this.containerEl || !this.containerEl.isConnected) return;
      if (!state.takeKconfigEngineRequest(tabId)) return;
      this.setEngineActive(true);
    });
  }

  /** Called when this tab is closed (TabsComponent.pruneTabViews). */
  dispose() {
    if (this.unsubscribeEngine) this.unsubscribeEngine();
    if (this.unsubscribeEngineRequest) this.unsubscribeEngineRequest();
    this.unsubscribeEngine = null;
    this.unsubscribeEngineRequest = null;
    this.containerEl = null;
  }

  /** This tab's engine was switched on or off by something other than this view. */
  async followEngineChange() {
    // Gaining it resolves this tab's values, so it is confirmed like the toolbar button (§5.0)
    if (this.engineActive && this.engine.loaded && !(await this.confirmEngineResolution())) {
      state.setKconfigEngineEnabled(this.tabId, false);
      toast.info("Engine left off: the configuration is unchanged");
      return;
    }
    this.applyEngineState({ announce: false });
  }

  /** Whether this tab runs its engine (off by default; other tabs may run theirs too). */
  get engineActive() {
    return state.isKconfigEngineOwner(this.tabId);
  }

  get profileKey() {
    return kconfigProfileKey(this.currentVersion, this.currentArch, this.configName);
  }

  /** Tab title: the ARCH, plus the name when this is one of several configurations of it. */
  tabTitle() {
    return `KConfig (${this.currentArch})${this.configName ? ` · ${this.configName}` : ""}`;
  }

  /** Toolchain profile of this configuration (only the fields the user set; server fills defaults). */
  get toolchain() {
    return state.getKconfigToolchain(this.profileKey);
  }

  get nodes() {
    return this.engine.loaded ? this.engine.nodes : [];
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tabId = tabData.id || null;
    this.currentVersion = tabData.version || state.currentVersion;
    this.currentArch = tabData.arch || DEFAULT_ARCH;
    this.configName = tabData.configName || "";
    this.searchQuery = tabData.query || "";
    // A revision or ARCH switch can land on another tab's configuration: take a free name instead
    const ownTab = state.findTab(this.tabId);
    if (ownTab) {
      this.configName = state.ensureUniqueKconfigConfigName(ownTab);
      if (ownTab.title !== this.tabTitle()) {
        ownTab.title = this.tabTitle();
        state.saveState();
      }
    }

    containerEl.innerHTML = `
      <div class="kconfig-view-container">
        <div class="kconfig-explorer-col">
          <div class="kconfig-filter-bar">
            <div class="kconfig-arch-presets">
              <select id="kconfig-arch-select" style="flex:1;" title="ARCH value passed to kconfig (decides which Kconfig files and definitions apply)">
                <option value="${escapeHtml(this.currentArch)}">${escapeHtml(this.currentArch)}</option>
              </select>
            </div>
            <div class="kconfig-btn-row">
              <button class="code-btn" id="btn-toolchain" title="Compiler, linker, environment variables and toolchain checks Kconfig depends on">Toolchain…</button>
              <button class="code-btn" id="btn-open-tui" title="Open terminal-style menuconfig">TUI</button>
            </div>
            <div class="kconfig-btn-row">
              <button class="code-btn" id="btn-rename-config" title="Name this configuration: copies of one ARCH and revision are told apart by their name">✎ ${escapeHtml(this.configName || "Unnamed")}</button>
              <button class="code-btn" id="btn-duplicate-config" title="Open a copy of this configuration in a new tab (named v2, v3, …)">⧉ Duplicate</button>
            </div>
            <div class="kconfig-search-box">
              <input type="text" placeholder="Search symbols or prompts..." id="kconfig-search-input" value="${escapeHtml(this.searchQuery)}" />
            </div>
            <div class="kconfig-btn-row">
              <button class="code-btn ${this.engineActive ? "active" : ""}" id="btn-toggle-engine" title="Constraint engine: only values kconfig accepts can be set (several tabs may run one)">${this.engineActive ? "✓ Engine Active" : "⚡ Enable Engine"}</button>
              <button class="code-btn" id="btn-compare-config" title="Compare this configuration with a defconfig or with another KConfig tab">Compare…</button>
            </div>
            <div class="kconfig-btn-row">
              <button class="code-btn" id="btn-export-config" title="Download the full .config (what make writes)">Export .config</button>
              <button class="code-btn" id="btn-load-defconfig" title="Load one of the architecture's defconfigs">Load Defconfig</button>
            </div>
            <div class="kconfig-btn-row">
              <button class="code-btn" id="btn-import-config" title="Load a .config or defconfig file">Import .config</button>
              <button class="code-btn" id="btn-validate-config" title="Check the configuration against Kconfig constraints">Validate</button>
              <input type="file" id="kconfig-import-file" accept=".config,text/plain,*" hidden />
            </div>
            <div class="kconfig-status" id="kconfig-status"></div>
          </div>
          <div class="kconfig-tree-scroll" id="kconfig-tree-scroll">
            <div class="kconfig-empty">Loading KConfig model for ${escapeHtml(this.currentArch)}...</div>
          </div>
        </div>
        <div class="kconfig-detail-col" id="kconfig-detail-col">
          <div class="kconfig-empty" style="padding-top:40px;text-align:center;">
            Select a configuration option to inspect where it is declared, what it depends on and which files it builds.
          </div>
        </div>
      </div>
    `;
    this.bindToolbar();

    try {
      const presetsRes = await api.getKconfigPresets(this.currentVersion);
      this.archPresets = (presetsRes && (presetsRes.targets || presetsRes.architectures)) || [];
      if (!this.archPresets.some((p) => p.id === this.currentArch)) {
        const fallback = presetsRes?.default_arch || this.archPresets[0]?.id || DEFAULT_ARCH;
        if (this.archPresets.length) toast.info(`ARCH=${this.currentArch} does not exist in ${this.currentVersion}; using ${fallback}`);
        this.currentArch = fallback;
      }
      this.fillArchSelect();
    } catch (err) {
      console.warn("Could not fetch arch presets:", err);
    }

    await this.activateArch(this.currentArch);
    if (tabData && tabData.symbol) this.selectAndInspectSymbol(tabData.symbol, { keepFilter: Boolean(this.searchQuery) });
    // The command palette may have asked this tab to take the engine while it was in the background
    if (state.takeKconfigEngineRequest(this.tabId)) this.setEngineActive(true);
  }

  bindToolbar() {
    const c = this.containerEl;
    const archSelect = c.querySelector("#kconfig-arch-select");
    archSelect.onchange = async (e) => {
      const target = e.target.value;
      if (target !== this.currentArch) await this.activateArch(target);
    };

    const engineBtn = c.querySelector("#btn-toggle-engine");
    engineBtn.onclick = () => this.setEngineActive(!this.engineActive);

    c.querySelector("#btn-rename-config").onclick = () => this.showRenameModal();
    c.querySelector("#btn-duplicate-config").onclick = () => this.duplicateConfig();
    c.querySelector("#btn-toolchain").onclick = () => this.showToolchainModal();
    c.querySelector("#btn-open-tui").onclick = () => {
      if (!this.engine.loaded) return;
      new MenuconfigTui(this).show();
    };
    c.querySelector("#btn-export-config").onclick = () => this.exportConfig();
    c.querySelector("#btn-load-defconfig").onclick = () => this.showDefconfigModal();

    const importInput = c.querySelector("#kconfig-import-file");
    c.querySelector("#btn-import-config").onclick = () => importInput.click();
    importInput.onchange = async () => {
      const file = importInput.files?.[0];
      importInput.value = "";
      if (file) this.loadConfigText(await file.text(), file.name);
    };
    c.querySelector("#btn-validate-config").onclick = () => this.showValidation();
    c.querySelector("#btn-compare-config").onclick = () => this.showCompareModal();

    const searchInput = c.querySelector("#kconfig-search-input");
    searchInput.oninput = () => {
      this.searchQuery = searchInput.value.trim();
      this.filterTree(this.searchQuery);
      this.persistTabState({ query: this.searchQuery });
    };
  }

  fillArchSelect() {
    const select = this.containerEl?.querySelector("#kconfig-arch-select");
    if (!select || !this.archPresets.length) return;
    const groups = new Map();
    for (const p of this.archPresets) {
      if (!groups.has(p.srcarch)) groups.set(p.srcarch, []);
      groups.get(p.srcarch).push(p);
    }
    select.innerHTML = [...groups.entries()].map(([srcarch, list]) => `
      <optgroup label="arch/${escapeHtml(srcarch)}">
        ${list.map((p) => `<option value="${escapeHtml(p.id)}" ${p.id === this.currentArch ? "selected" : ""}>${escapeHtml(p.label || p.id)}</option>`).join("")}
      </optgroup>`).join("");
  }

  // ------------------------------------------------------------------ configuration lifecycle
  /** Load the ARCH's model and restore its saved configuration (or its default defconfig). */
  async activateArch(arch) {
    const archChanged = this.currentArch !== arch;
    if (archChanged) {
      this.expandedNodeIds.clear();
      this.selectedSymbol = null;
      this.selectedNodeId = null;
    }
    this.currentArch = arch;
    // The tab carries the ARCH and the name the profile key is built from, so update it first.
    // The switch goes through the store so the URL follows it (§5.26), and a real switch drops
    // the inspected symbol: it was a symbol of the ARCH being left. Re-activating the same ARCH
    // (what every render does) keeps it, or a deep link's `sym` would be dropped before it is read.
    const ownTab = state.findTab(this.tabId);
    if (ownTab && ownTab.type === "kconfig") {
      this.persistTabState(archChanged ? { arch, symbol: "" } : { arch });
      this.configName = state.ensureUniqueKconfigConfigName(ownTab);
      this.persistTabState({ configName: this.configName, title: this.tabTitle() });
    }
    const select = this.containerEl?.querySelector("#kconfig-arch-select");
    if (select) select.value = arch;
    const scrollEl = this.containerEl?.querySelector("#kconfig-tree-scroll");
    if (scrollEl) scrollEl.innerHTML = `<div class="kconfig-empty">Loading KConfig model for ARCH=${escapeHtml(arch)}...</div>`;

    try {
      const raw = await api.getKconfigTree(this.currentVersion, arch, this.toolchain);
      this.engine.setModel(raw);
    } catch (err) {
      if (scrollEl) scrollEl.innerHTML = `<div class="kconfig-error">Failed to load the Kconfig model: ${escapeHtml(err.message)}</div>`;
      return;
    }

    const saved = state.getKconfigProfile(this.profileKey);
    if (saved && Object.keys(saved).length) {
      // Values carried over from another revision or ARCH (see `requestVersionSwitch`) are loaded
      // against this model; with the engine on, kconfig resolves them and drops what it does not know
      if (this.engineActive) {
        const report = this.engine.loadAssignments(saved);
        this.rawValues = { ...saved };
        this.commit();
        // Only when the configuration actually moved to another model, not on every re-render
        if (this.resolvedProfileKey && this.resolvedProfileKey !== this.profileKey) {
          const dropped = report.unknown.length ? `, ${report.unknown.length} unknown for ARCH=${this.currentArch} dropped` : "";
          toast.info(`${Object.keys(saved).length} values loaded and resolved for ${this.currentVersion}${dropped}`);
        }
      } else {
        this.rawValues = { ...saved };
        this.commit();
      }
    } else {
      await this.loadDefconfig("", { quiet: true });
    }

    this.resolvedProfileKey = this.profileKey;
    this.renderTree();
    if (this.selectedSymbol) this.inspectSymbol(this.selectedSymbol);
  }

  /**
   * Name this configuration. Several configurations of one revision and ARCH are told apart by
   * their name (it is part of the key they are saved under), so copies can be edited and compared
   * independently; the saved values and toolchain profile move with the name.
   */
  showRenameModal() {
    const taken = state.kconfigConfigNames(this.currentVersion, this.currentArch, this.tabId);
    const modal = openModal({
      title: "Name this configuration",
      subtitle: `ARCH=${this.currentArch} @ ${this.currentVersion}`,
      width: "520px",
      footerHtml: `<button class="code-btn active" id="btn-rename-ok" type="button">Rename</button>`,
    });
    modal.body.innerHTML = `
      <div class="ki-form-row"><label>Name
        <input type="text" id="kconfig-rename-input" maxlength="40" placeholder="unnamed" value="${escapeHtml(this.configName)}" />
      </label></div>
      <div class="kconfig-muted">Copies of ARCH=${escapeHtml(this.currentArch)} @ ${escapeHtml(this.currentVersion)} are told apart by this name, and each keeps its own values. Leave it empty for the unnamed configuration.${taken.size ? ` Already used: ${escapeHtml([...taken].map((n) => n || "unnamed").join(", "))}.` : ""}</div>`;
    const input = modal.body.querySelector("#kconfig-rename-input");
    const submit = () => {
      const name = input.value.trim().replace(/\|/g, "-");
      if (name === this.configName) {
        modal.close();
        return;
      }
      if (taken.has(name)) {
        toast.error(`“${name || "unnamed"}” is already used by another configuration of ARCH=${this.currentArch} @ ${this.currentVersion}`);
        return;
      }
      modal.close();
      this.renameConfig(name);
    };
    modal.footer.querySelector("#btn-rename-ok").onclick = submit;
    input.onkeydown = (e) => {
      if (e.key === "Enter") submit();
    };
    input.focus();
    input.select();
  }

  /** Apply a new name: the saved configuration and toolchain profile move to the new key. */
  renameConfig(name) {
    const from = this.profileKey;
    this.configName = name;
    state.moveKconfigProfile(from, this.profileKey);
    const tab = state.findTab(this.tabId);
    if (tab) {
      tab.configName = name;
      tab.title = this.tabTitle();
      state.saveState();
      // Refresh the tab strip (and this view) under the new name
      const pane = state.panes.find((p) => p.tabs.some((t) => t.id === this.tabId));
      state.emit("tabs:changed", { paneId: pane?.id || state.activePaneId, activeTabId: pane?.activeTabId });
    }
    toast.success(`Configuration renamed to ${name || "unnamed"}`);
  }

  /**
   * Open a copy of this configuration in its own tab, named v2, v3, … (the first free one). The
   * copy owns its values from the start, so both tabs can be edited apart and compared (**Compare…**).
   * The engine stays with whichever tab holds it; the copy opens unconstrained.
   */
  duplicateConfig() {
    const name = state.nextKconfigConfigName(this.currentVersion, this.currentArch);
    const key = kconfigProfileKey(this.currentVersion, this.currentArch, name);
    state.saveKconfigProfileValues(key, this.rawValues || {});
    const tc = this.toolchain;
    if (tc && Object.keys(tc).length) state.setKconfigToolchain(key, JSON.parse(JSON.stringify(tc)));
    state.openTab({
      type: "kconfig",
      title: `KConfig (${this.currentArch}) · ${name}`,
      arch: this.currentArch,
      version: this.currentVersion,
      configName: name,
      forceNew: true,
    });
    toast.success(`Copied ${Object.keys(this.rawValues || {}).length} values to ${name}; the engine is off in the copy`);
  }

  /**
   * Save the current configuration under this tab's profile. While this tab runs its engine, the
   * resolved values are also published as *its* engine configuration, which the code tabs following
   * this engine dim with; the engines of other tabs are untouched.
   */
  commit() {
    if (this.engineActive) {
      this.rawValues = this.engine.configValues();
    }
    state.setKconfigAssignments(this.tabId, this.profileKey, this.rawValues || {}, {
      version: this.currentVersion,
      arch: this.currentArch,
      configName: this.configName,
    });
    this.updateStatus();
  }

  /** Refresh tree and inspector after a change. */
  changed() {
    this.commit();
    this.renderTree();
    if (this.selectedSymbol) this.inspectSymbol(this.selectedSymbol, this.selectedNodeId, { keepScroll: true });
  }

  /**
   * Turn this tab's engine on or off. Engines are per tab and independent: enabling one here does
   * not disturb another tab's, and code tabs choose which engine they follow.
   */
  async setEngineActive(active) {
    // Enabling it resolves the values through kconfig, which may change more than nothing (§5.0)
    if (active && this.engine.loaded && !(await this.confirmEngineResolution())) return;
    this.selfEngineChange = true;
    try {
      state.setKconfigEngineEnabled(this.tabId, Boolean(active));
    } finally {
      this.selfEngineChange = false;
    }
    this.applyEngineState({ announce: true });
  }

  /**
   * Bring the toolbar, the values and the tree in line with who holds the engine. Turning it on
   * resolves this tab's values through kconfig; turning it off keeps them as they are (raw editing).
   */
  applyEngineState({ announce = true } = {}) {
    const btn = this.containerEl?.querySelector("#btn-toggle-engine");
    if (btn) {
      btn.classList.toggle("active", this.engineActive);
      btn.textContent = this.engineActive ? "✓ Engine Active" : "⚡ Enable Engine";
    }
    if (this.engineActive && this.engine.loaded) {
      const before = { ...(this.rawValues || {}) };
      const report = this.engine.loadAssignments(before);
      this.commit();
      const after = this.rawValues;
      const adjusted = Object.keys(before).filter((k) => {
        const s = this.engine.syms[k];
        if (!s || s.auto) return false;
        const got = after[k] ?? (s.type === "bool" || s.type === "tristate" ? "n" : "");
        return got !== String(before[k]);
      }).length;
      if (announce) toast.success(`Engine on in ${this.tabTitle()}: configuration resolved by kconfig (${adjusted} values adjusted, ${report.unknown.length} unknown symbols dropped)`);
      this.renderTree();
    } else {
      if (announce) toast.info("Engine off: values are edited without constraints (export will be unvalidated)");
      this.updateStatus();
      if (this.engine.loaded) this.renderTree();
    }
    if (this.selectedSymbol) this.inspectSymbol(this.selectedSymbol, this.selectedNodeId, { keepScroll: true });
  }

  /**
   * Enabling the engine resolves this tab's values through kconfig, which can change values other
   * than the ones the user set (an edited or imported configuration, a raw edit kconfig refuses).
   * Report exactly what would change and apply it only once confirmed; resolve true straight away
   * when the resolution changes nothing.
   */
  confirmEngineResolution() {
    const MAX_ROWS = 300;
    const { adjusted, unknown } = this.resolutionDiff();
    if (!adjusted.length && !unknown.length) return Promise.resolve(true);
    return new Promise((resolve) => {
      let confirmed = false;
      const parts = [];
      if (adjusted.length) parts.push(`${adjusted.length} value${adjusted.length === 1 ? "" : "s"} changed`);
      if (unknown.length) parts.push(`${unknown.length} unknown symbol${unknown.length === 1 ? "" : "s"} dropped`);
      const modal = openModal({
        title: "Enabling the engine changes this configuration",
        subtitle: `kconfig would not keep it as it is: ${parts.join(", ")}`,
        width: "820px",
        footerHtml: `<button class="code-btn active" id="kconfig-confirm-resolve" type="button">Enable engine and apply these changes</button>`,
        onClose: () => resolve(confirmed),
      });
      modal.body.innerHTML = `
        <div class="kconfig-muted" style="margin-bottom:10px;">With the engine on, only configurations kconfig accepts exist, so loading these values resolves them (ARCH=${escapeHtml(this.currentArch)} @ ${escapeHtml(this.currentVersion)}). <strong>Close</strong> leaves the engine off and the values exactly as they are.</div>
        ${adjusted.length ? `<table class="kconfig-cmp-table"><thead><tr><th>Option</th><th>Now</th><th>After</th><th>Why</th></tr></thead>
        <tbody>${adjusted.slice(0, MAX_ROWS).map((a) => `<tr><td>CONFIG_${escapeHtml(a.name)}</td><td>${escapeHtml(a.want || '""')}</td><td>${escapeHtml(a.got || '""')}</td><td class="kconfig-muted">${escapeHtml(a.reason)}</td></tr>`).join("")}</tbody></table>
        ${adjusted.length > MAX_ROWS ? `<div class="kconfig-muted" style="margin-top:8px;">… and ${adjusted.length - MAX_ROWS} more</div>` : ""}` : ""}
        ${unknown.length ? `<div class="kconfig-prop-card" style="margin-top:10px;"><div class="kconfig-prop-title">Unknown for ARCH=${escapeHtml(this.currentArch)} (${unknown.length}, dropped by kconfig)</div><div class="kconfig-muted" style="font-family:var(--font-mono);font-size:11px;">${unknown.slice(0, 200).map(escapeHtml).join(" ")}</div></div>` : ""}`;
      const ok = modal.footer.querySelector("#kconfig-confirm-resolve");
      ok.onclick = () => {
        confirmed = true;
        modal.close();
      };
      ok.focus();
    });
  }

  /**
   * Resolve this tab's values with a throwaway engine (the live one is untouched) and report what
   * kconfig would not keep: `{ probe, raw, resolved, adjusted: [{name, want, got, reason}], unknown }`.
   */
  resolutionDiff() {
    const raw = { ...(this.rawValues || {}) };
    const probe = new KconfigEngine(this.engine.model);
    const report = probe.loadAssignments(raw);
    const resolved = probe.configValues();
    const adjusted = [];
    for (const [name, want] of Object.entries(raw)) {
      const s = probe.syms[name];
      if (!s || s.auto) continue;
      const got = resolved[name] ?? (s.type === "bool" || s.type === "tristate" ? "n" : "");
      if (String(want) === got) continue;
      const reason = probe.revDep(name) > 0 && got !== "n" ? `forced by select: ${probe.exprStr(s.rev_dep, true)}`
        : probe.visibility(name) === 0 ? (s.prompts.length ? `prompt not visible: ${s.prompts.map((pr) => probe.exprStr(pr, true)).join(" || ")}` : "no prompt (value comes from defaults)")
        : "limited by dependencies or range";
      adjusted.push({ name, want, got, reason });
    }
    return { probe, raw, resolved, adjusted, unknown: report.unknown };
  }

  /** Load a defconfig of this ARCH ("" = the default `make defconfig` one). */
  async loadDefconfig(path, { quiet = false } = {}) {
    let res;
    try {
      res = await api.getKconfigDefconfigContent(this.currentVersion, path, this.currentArch, this.toolchain);
    } catch (err) {
      if (!quiet) toast.error(`Failed to load defconfig: ${err.message}`);
      this.engine.loadConfig("");
      this.engine.setAllNewSymbols("default");
      this.rawValues = {};
      this.commit();
      return;
    }
    const report = this.engine.loadConfig(res.content || "");
    this.engine.setAllNewSymbols("default");
    if (this.engineActive) {
      this.commit();
    } else {
      this.rawValues = Object.fromEntries(Object.entries(KconfigParser.parse(res.content)).map(([k, v]) => [k.slice(7), v]));
      this.commit();
    }
    const extra = report.unknown.length ? `, ${report.unknown.length} unknown symbols ignored` : "";
    if (!quiet || report.unknown.length) toast.success(`Loaded ${res.name} for ARCH=${this.currentArch}${extra}`);
    this.renderTree();
  }

  /** Load .config text (import): conf_read semantics when the engine is on. */
  loadConfigText(text, label = "configuration") {
    const parsed = KconfigParser.parse(text);
    if (!Object.keys(parsed).length) {
      toast.error("No CONFIG_ assignments found in that file");
      return;
    }
    if (this.engineActive) {
      const report = this.engine.loadConfig(text);
      this.engine.setAllNewSymbols("default");
      this.commit();
      const notes = [];
      if (report.unknown.length) notes.push(`${report.unknown.length} unknown for ARCH=${this.currentArch}`);
      if (report.invalid.length) notes.push(`${report.invalid.length} invalid values`);
      toast.success(`Imported ${label}${notes.length ? ` (${notes.join(", ")})` : ""}`);
    } else {
      this.rawValues = Object.fromEntries(Object.entries(parsed).map(([k, v]) => [k.slice(7), v]));
      this.commit();
      toast.success(`Imported ${Object.keys(parsed).length} raw assignments from ${label}`);
    }
    this.renderTree();
  }

  updateStatus() {
    const el = this.containerEl?.querySelector("#kconfig-status");
    if (!el || !this.engine.loaded) return;
    const written = Object.keys(this.rawValues || {}).length;
    const env = this.engine.model.env || {};
    const tc = this.engine.model.toolchain;
    const errors = (tc?.messages || []).filter((m) => m.level === "error").length;
    const toolchain = tc?.macros ? ` · <a href="#" class="kconfig-status-link" id="kconfig-status-toolchain">${escapeHtml(tc.summary)}${errors ? ` <span style="color:var(--accent-red);">⚠ ${errors} error(s)</span>` : ""}</a>` : "";
    el.innerHTML = `ARCH=${escapeHtml(env.ARCH || this.currentArch)} SRCARCH=${escapeHtml(env.SRCARCH || "")} · ${escapeHtml(this.currentVersion)}${this.configName ? ` · ${escapeHtml(this.configName)}` : ""} · ${written} symbols${this.engineActive ? " · engine resolved" : " · <span style=\"color:var(--accent-yellow);\">unvalidated (engine off)</span>"}${toolchain}`;
    const link = el.querySelector("#kconfig-status-toolchain");
    if (link) {
      link.onclick = (ev) => {
        ev.preventDefault();
        this.showToolchainModal();
      };
    }
  }

  // ------------------------------------------------------------------ value access & edits
  /** Current value of a symbol (engine-resolved, or raw when the engine is off). */
  valueOf(name) {
    if (this.engineActive) return this.engine.isDefined(name) ? this.engine.value(name) : "n";
    const raw = (this.rawValues || {})[name];
    if (raw !== undefined) return raw;
    const t = this.engine.syms[name]?.type;
    return t === "bool" || t === "tristate" ? "n" : "";
  }

  nodeVisible(node) {
    if (!node || node.prompt === null || node.prompt === undefined) return false;
    if (this.showUnmet || !this.engineActive) return true;
    return this.engine.menuVisible(node.id);
  }

  visibleChildren(node) {
    return node.children.map((id) => this.nodes[id]).filter((n) => this.nodeVisible(n));
  }

  /** menuconfig-style rendering of an entry: { marker, title, submenu, dim }. */
  describe(node) {
    const e = this.engine;
    const hasChildren = this.visibleChildren(node).length > 0;
    if (node.kind === "menu") return { marker: "", title: node.prompt, submenu: true, dim: !e.menuVisible(node.id) };
    if (node.kind === "comment") return { marker: "", title: `*** ${node.prompt} ***`, submenu: false, comment: true, dim: !e.menuVisible(node.id) };
    const sym = node.sym;
    const s = e.syms[sym];
    if (!s) return { marker: "", title: node.prompt || sym || "", submenu: hasChildren };
    const dim = this.engineActive && !e.menuVisible(node.id);
    if (s.is_choice) {
      const sel = this.engineActive ? e.choiceSelection(sym) : (s.members.find((m) => this.valueOf(m) === "y") || null);
      const selPrompt = sel ? (e.nodes[e.syms[sel].menus[0]]?.prompt || sel) : "";
      return { marker: "", title: `${node.prompt}${selPrompt ? ` (${selPrompt})` : ""}`, submenu: true, dim };
    }
    const val = this.valueOf(sym);
    let marker;
    if (s.type === "bool" || s.type === "tristate") {
      const tri = { n: 0, m: 1, y: 2 }[val] ?? 0;
      const choiceY = s.choice && this.engineActive && e.symTri(s.choice) === 2;
      const changeable = !this.engineActive || e.isChangeable(sym);
      const asBool = s.type === "bool" || (this.engineActive && e.getType(sym) === "bool");
      if (choiceY || (s.choice && !this.engineActive)) marker = tri === 2 ? "(X)" : "( )";
      else if (!changeable) marker = tri === 2 ? "-*-" : (tri === 1 ? "-M-" : "---");
      else if (asBool) marker = tri ? "[*]" : "[ ]";
      else marker = tri === 2 ? "<*>" : (tri === 1 ? "<M>" : "< >");
    } else {
      marker = `(${val})`;
    }
    return { marker, title: node.prompt, submenu: node.kind === "menuconfig" || hasChildren, dim };
  }

  /** Change a symbol's value as menuconfig does; returns true when the configuration changed. */
  async changeValue(node, explicit = null) {
    const sym = node?.sym;
    const s = sym && this.engine.syms[sym];
    if (!s || s.is_choice) return false;
    if (s.type !== "bool" && s.type !== "tristate") {
      const current = this.valueOf(sym);
      const range = this.engineActive ? this.engine.activeRange(sym) : null;
      const hint = range ? ` (range ${s.type === "hex" ? `0x${range.lo.toString(16)}..0x${range.hi.toString(16)}` : `${range.lo}..${range.hi}`})` : "";
      const text = explicit ?? window.prompt(`CONFIG_${sym} (${s.type})${hint}`, current);
      if (text === null || text === undefined) return false;
      return this.applyValue(sym, text);
    }
    if (!this.engineActive) {
      const order = s.type === "tristate" ? ["n", "m", "y"] : ["n", "y"];
      const cur = this.valueOf(sym);
      const next = explicit ?? order[(order.indexOf(cur) + 1) % order.length];
      return this.applyValue(sym, next);
    }
    if (explicit !== null) return this.applyValue(sym, explicit);
    if (s.choice && this.engine.symTri(s.choice) === 2) return this.applyValue(sym, "y");
    const before = this.engine.symTri(sym);
    const next = this.engine.toggleTarget(sym);
    if (before === next) {
      toast.info(`CONFIG_${sym} cannot change: ${this.engine.refusalReason(sym, (before + 1) % 3) || "fixed by its dependencies"}`);
      return false;
    }
    return this.applyValue(sym, ["n", "m", "y"][next]);
  }

  /**
   * Set a value (y/m/n or text); with the engine on it is refused unless kconfig accepts it. Lowering a value
   * that other options need (they depend on it or were selected by it) asks first, listing what they lose.
   */
  async applyValue(sym, text) {
    if (!this.engineActive) {
      this.rawValues = { ...(this.rawValues || {}), [sym]: text };
      this.changed();
      return true;
    }
    const s = this.engine.syms[sym];
    const tri = s.type === "bool" || s.type === "tristate" ? { n: 0, m: 1, y: 2 }[String(text).toLowerCase()[0]] : null;
    if (tri !== null && tri !== undefined && tri < this.engine.symTri(sym)) {
      const lowered = this.engine.previewLowered(sym, text);
      if (lowered && lowered.length && !(await this.confirmSideEffects(sym, text, lowered))) return false;
    }
    if (!this.engine.setValue(sym, text)) {
      toast.error(`CONFIG_${sym}=${text} refused: ${this.engine.refusalReason(sym, tri ?? text) || "not accepted by kconfig"}`);
      return false;
    }
    this.changed();
    return true;
  }

  /** Ask before a change that lowers other options; resolves true to go ahead. */
  confirmSideEffects(sym, text, lowered) {
    const MAX_ROWS = 300;
    const value = String(text).toLowerCase()[0];
    return new Promise((resolve) => {
      let confirmed = false;
      const modal = openModal({
        title: `Set CONFIG_${sym}=${value}?`,
        subtitle: `${lowered.length} other option${lowered.length === 1 ? "" : "s"} would change as a side effect`,
        width: "820px",
        footerHtml: `<button class="code-btn active" id="kconfig-confirm-lower" type="button">Set to ${escapeHtml(value)} anyway</button>`,
        onClose: () => resolve(confirmed),
      });
      modal.body.innerHTML = `
        <div class="kconfig-muted" style="margin-bottom:10px;">These options need CONFIG_${escapeHtml(sym)} (they depend on it or are selected through it) and are lowered with it:</div>
        <table class="kconfig-cmp-table"><thead><tr><th>Option</th><th>Now</th><th>After</th><th>Why</th></tr></thead>
        <tbody>${lowered.slice(0, MAX_ROWS).map((r) => `<tr data-symbol="${escapeHtml(r.name)}" title="Inspect CONFIG_${escapeHtml(r.name)} (cancels this change)"><td>CONFIG_${escapeHtml(r.name)}</td><td>${escapeHtml(r.before)}</td><td>${escapeHtml(r.after)}</td><td class="kconfig-muted">${escapeHtml(r.reason)}</td></tr>`).join("")}</tbody></table>
        ${lowered.length > MAX_ROWS ? `<div class="kconfig-muted" style="margin-top:8px;">… and ${lowered.length - MAX_ROWS} more</div>` : ""}`;
      modal.body.querySelectorAll("tr[data-symbol]").forEach((tr) => {
        tr.onclick = () => {
          modal.close();
          this.selectAndInspectSymbol(tr.dataset.symbol);
        };
      });
      const ok = modal.footer.querySelector("#kconfig-confirm-lower");
      ok.onclick = () => {
        confirmed = true;
        modal.close();
      };
      ok.focus();
    });
  }

  // ------------------------------------------------------------------ tree
  expandAncestors(nodeId) {
    let parent = this.nodes[nodeId]?.parent;
    while (parent > 0) {
      this.expandedNodeIds.add(parent);
      parent = this.nodes[parent].parent;
    }
  }

  /** First menu entry of a symbol (the one with a prompt if any). */
  primaryNode(sym) {
    const s = this.engine.syms[sym];
    if (!s || !s.menus.length) return null;
    const ids = s.menus;
    return this.nodes[ids.find((id) => this.nodes[id].prompt) ?? ids[0]];
  }

  selectAndInspectSymbol(symbolName, { keepFilter = false } = {}) {
    if (!symbolName) return;
    const clean = symbolName.startsWith("CONFIG_") ? symbolName.slice(7) : symbolName;
    const node = this.primaryNode(clean);
    if (node) {
      // An entry menuconfig hides is listed anyway so the inspected symbol can be reached
      if (!this.nodeVisible(node) && node.prompt) this.showUnmet = true;
      this.expandAncestors(node.id);
      // Reaching the symbol in the tree means leaving the flat match list, unless the filter is
      // what a deep link asked for: then both the search and the symbol are restored (§5.26)
      const searchInput = this.containerEl?.querySelector("#kconfig-search-input");
      if (searchInput && !keepFilter) {
        searchInput.value = "";
        this.searchQuery = "";
      }
    }
    this.selectedSymbol = clean;
    this.selectedNodeId = node ? node.id : null;
    this.renderTree();
    const row = this.containerEl?.querySelector(`.kconfig-node-row[data-node-id="${node?.id}"]`);
    if (row) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    this.inspectSymbol(clean, node?.id);

    this.persistTabState({
      symbol: clean,
      query: this.searchQuery,
      title: `KConfig (${clean})`
    });
  }

  /**
   * Record on this tab (and so in its URL, §5.26) what this view is showing. A view whose
   * element was replaced by a re-render is stale: its state is nobody's address any more.
   */
  persistTabState(patch) {
    if (!this.containerEl || !this.containerEl.isConnected) return null;
    return state.updateTabState(this.tabId, patch);
  }

  renderTree() {
    const scrollEl = this.containerEl?.querySelector("#kconfig-tree-scroll");
    if (!scrollEl || !this.engine.loaded) return;
    const query = this.containerEl.querySelector("#kconfig-search-input")?.value.trim() ?? this.searchQuery;
    if (query) {
      this.filterTree(query);
      return;
    }
    const prevScrollTop = scrollEl.scrollTop;
    scrollEl.innerHTML = "";
    const roots = this.visibleChildren(this.nodes[0]);
    if (!roots.length) scrollEl.innerHTML = `<div class="kconfig-empty">No visible entries.</div>`;
    const frag = document.createDocumentFragment();
    roots.forEach((node) => this.renderTreeNode(frag, node, 0));
    scrollEl.appendChild(frag);
    scrollEl.scrollTop = prevScrollTop;
  }

  renderTreeNode(container, node, depth, flat = false) {
    const d = this.describe(node);
    const childList = flat ? [] : this.visibleChildren(node);
    const hasChildren = childList.length > 0;
    const row = document.createElement("div");
    row.className = "kconfig-node-row";
    if (d.dim) row.classList.add("kconfig-node-disabled");
    if (d.comment) row.classList.add("kconfig-node-comment");
    if (node.id === this.selectedNodeId || (node.sym && node.sym === this.selectedSymbol && this.selectedNodeId === null)) row.classList.add("active");
    row.style.paddingLeft = `${10 + depth * 14}px`;
    row.dataset.symbol = node.sym || "";
    row.dataset.nodeId = String(node.id);
    const isExpanded = hasChildren && this.expandedNodeIds.has(node.id);
    const location = flat ? this.engine.menuPath(node.id).join(" › ") : "";
    const arrow = d.submenu && (hasChildren || node.kind === "menu" || node.kind === "choice");
    row.innerHTML = `
      <span class="chevron" style="visibility:${hasChildren ? "visible" : "hidden"};">${isExpanded ? "▼" : "▶"}</span>
      <span class="kconfig-node-state" title="Click to change">${escapeHtml(d.marker)}</span>
      <span class="kconfig-node-name">${escapeHtml(d.title || "")}${arrow ? ' <span class="kconfig-submenu">---&gt;</span>' : ""}</span>
      ${node.sym && !this.engine.syms[node.sym]?.is_choice ? `<span class="kconfig-node-sym">${escapeHtml(node.sym)}</span>` : ""}
      ${location ? `<span class="kconfig-node-loc">${escapeHtml(location)}</span>` : ""}
    `;
    if (d.dim && node.sym && this.engine.syms[node.sym]) {
      row.title = `Not visible: depends on ${this.engine.exprStr(node.pvis, true)}`;
    }
    container.appendChild(row);

    const childContainer = document.createElement("div");
    childContainer.style.display = isExpanded ? "block" : "none";
    container.appendChild(childContainer);
    if (isExpanded) childList.forEach((c) => this.renderTreeNode(childContainer, c, depth + 1));

    const chevron = row.querySelector(".chevron");
    const toggleExpand = () => {
      if (!hasChildren) return;
      if (this.expandedNodeIds.has(node.id)) {
        this.expandedNodeIds.delete(node.id);
        chevron.textContent = "▶";
        childContainer.style.display = "none";
      } else {
        this.expandedNodeIds.add(node.id);
        chevron.textContent = "▼";
        childContainer.style.display = "block";
        if (!childContainer.children.length) childList.forEach((c) => this.renderTreeNode(childContainer, c, depth + 1));
      }
    };
    chevron.onclick = (e) => {
      e.stopPropagation();
      toggleExpand();
    };
    row.querySelector(".kconfig-node-state").onclick = (e) => {
      e.stopPropagation();
      if (node.sym && !this.engine.syms[node.sym]?.is_choice) {
        this.selectedSymbol = node.sym;
        this.selectedNodeId = node.id;
        this.changeValue(node);
      } else {
        toggleExpand();
      }
    };
    row.onclick = () => {
      if (!node.sym || this.engine.syms[node.sym]?.is_choice) {
        if (flat) this.selectAndRevealNode(node);
        else toggleExpand();
        return;
      }
      this.containerEl.querySelectorAll(".kconfig-node-row.active").forEach((r) => r.classList.remove("active"));
      row.classList.add("active");
      this.selectedSymbol = node.sym;
      this.selectedNodeId = node.id;
      this.inspectSymbol(node.sym, node.id);
      this.persistTabState({ symbol: node.sym });
    };
    row.ondblclick = () => toggleExpand();
    row.addEventListener("auxclick", (e) => {
      if (e.button === 1 && node.sym) {
        e.preventDefault();
        e.stopPropagation();
        this.openSymbolTab(node.sym);
      }
    });
  }

  selectAndRevealNode(node) {
    const searchInput = this.containerEl?.querySelector("#kconfig-search-input");
    if (searchInput) searchInput.value = "";
    this.searchQuery = "";
    this.persistTabState({ query: "" });
    this.expandAncestors(node.id);
    this.expandedNodeIds.add(node.id);
    this.renderTree();
  }

  openSymbolTab(sym) {
    state.openTab({ type: "kconfig", title: `KConfig (${sym})`, arch: this.currentArch, symbol: sym, version: this.currentVersion, forceNew: true });
  }

  /** Flat list of entries whose symbol or prompt matches, with their menu location. */
  filterTree(query) {
    const scrollEl = this.containerEl?.querySelector("#kconfig-tree-scroll");
    if (!scrollEl || !this.engine.loaded) return;
    if (!query) {
      this.renderTree();
      return;
    }
    const q = query.replace(/^CONFIG_/i, "").toLowerCase();
    const matches = this.nodes.filter((n) => n.prompt && ((n.sym || "").toLowerCase().includes(q) || n.prompt.toLowerCase().includes(q)))
      .sort((a, b) => Number((b.sym || "").toLowerCase() === q) - Number((a.sym || "").toLowerCase() === q));
    scrollEl.innerHTML = "";
    if (!matches.length) {
      scrollEl.innerHTML = `<div class="kconfig-empty">No entries matching "${escapeHtml(query)}" for ARCH=${escapeHtml(this.currentArch)}</div>`;
      return;
    }
    matches.slice(0, SEARCH_LIMIT).forEach((node) => this.renderTreeNode(scrollEl, node, 0, true));
    if (matches.length > SEARCH_LIMIT) {
      scrollEl.insertAdjacentHTML("beforeend", `<div class="kconfig-empty">Showing ${SEARCH_LIMIT} of ${matches.length} matches. Refine the filter for more.</div>`);
    }
  }

  // ------------------------------------------------------------------ inspector
  triBadge(tri) {
    return `<span class="kconfig-tri kconfig-tri-${TRI_CHAR[tri]}">${TRI_CHAR[tri]}</span>`;
  }

  exprHtml(id) {
    if (id === null || id === undefined || id < 0) return `<span class="kconfig-muted">none</span>`;
    const tri = this.engine.exprTri(id);
    return `<code class="kconfig-expr">${escapeHtml(this.engine.exprStr(id, true))}</code> ${this.triBadge(tri)}`;
  }

  symLink(name) {
    const defined = this.engine.isDefined(name);
    const val = defined ? this.engine.value(name) : "undefined";
    const cls = val === "y" ? "dep-met" : (val === "m" ? "dep-mod" : "dep-unmet");
    return `<span class="kconfig-badge kconfig-badge-link ${cls}" data-symbol="${escapeHtml(name)}" title="CONFIG_${escapeHtml(name)} = ${escapeHtml(val)}">${escapeHtml(name)}=${escapeHtml(val)}</span>`;
  }

  fileLink(path, line, label = null) {
    return `<button class="code-btn kconfig-file-link" data-path="${escapeHtml(path)}" data-line="${line || 1}" title="Open ${escapeHtml(path)}:${line || 1}">📄 ${escapeHtml(label || `${path}:${line || 1}`)}</button>`;
  }

  /** Why the symbol has its current value (engine on). */
  explainValue(sym) {
    const e = this.engine;
    const s = e.syms[sym];
    const vis = e.visibility(sym);
    const rev = e.revDep(sym);
    if (s.choice && vis === 2) return `chosen in its choice group (${e.choiceSelection(s.choice) === sym ? "selected" : "another member is selected"})`;
    if (vis !== 0 && e.hasUser(sym)) {
      if ((s.type === "bool" || s.type === "tristate") && rev > ({ n: 0, m: 1, y: 2 }[e.user.get(sym)] ?? 0)) return "raised by select (your value is lower than what selects force)";
      return "set explicitly (loaded from the configuration or changed by you)";
    }
    if (rev > 0 && (s.type === "bool" || s.type === "tristate")) return `forced by select: ${e.exprStr(s.rev_dep, true)}`;
    const def = e.defaultProp(sym);
    if (def) return `default ${e.exprStr(def.valueId)}${s.defaults.length > 1 ? " (first default whose condition holds)" : ""}`;
    if (!s.prompts.length) return "no prompt and no active default: n / empty";
    return vis === 0 ? "dependencies not met" : "no default (n / empty)";
  }

  async inspectSymbol(symbolName, nodeId = null, { keepScroll = false } = {}) {
    const detailCol = this.containerEl?.querySelector("#kconfig-detail-col");
    if (!detailCol || !this.engine.loaded) return;
    const token = ++this.detailToken;
    const sym = symbolName.startsWith("CONFIG_") ? symbolName.slice(7) : symbolName;
    this.selectedSymbol = sym;
    const e = this.engine;
    const s = e.syms[sym];
    const scrollTop = detailCol.scrollTop;

    if (!s) {
      detailCol.innerHTML = `<div class="kconfig-symbol-title">CONFIG_${escapeHtml(sym)}</div>
        <div class="kconfig-prop-card" id="kconfig-detail-remote"><div class="kconfig-muted">Not defined for ARCH=${escapeHtml(this.currentArch)}. Looking up other definitions…</div></div>`;
      this.fillRemoteDetail(sym, token, detailCol);
      return;
    }

    const val = this.valueOf(sym);
    const isTri = s.type === "bool" || s.type === "tristate";
    const typeLabel = s.type === "tristate" && this.engineActive && e.getType(sym) === "bool" ? "tristate (as bool: MODULES=n or y-choice)" : s.type;
    const controls = [];
    if (isTri && !(s.choice && this.engineActive && e.symTri(s.choice) === 2)) {
      for (const v of s.type === "tristate" ? ["n", "m", "y"] : ["n", "y"]) {
        const allowed = !this.engineActive || v === val || e.tristateWithinRange(sym, { n: 0, m: 1, y: 2 }[v]);
        controls.push(`<button class="code-btn kconfig-set-btn ${v === val ? "active" : ""}" data-value="${v}" ${allowed ? "" : "disabled"} title="${allowed ? `Set CONFIG_${sym}=${v}` : escapeHtml(e.refusalReason(sym, { n: 0, m: 1, y: 2 }[v]))}">${v}</button>`);
      }
    } else if (isTri) {
      controls.push(`<button class="code-btn kconfig-set-btn ${val === "y" ? "active" : ""}" data-value="y">${val === "y" ? "(X) selected" : "Select"}</button>`);
    } else {
      controls.push(`<input class="kconfig-value-input" id="kconfig-value-input" value="${escapeHtml(val)}" spellcheck="false" />
        <button class="code-btn" id="kconfig-value-set">Set</button>`);
    }
    if (this.engineActive && isTri && val === "n") controls.push(`<button class="code-btn" id="btn-autosolve-sym" title="Find the legal changes that enable this option">🪄 Auto-solve</button>`);

    const defs = s.menus.map((id) => this.nodes[id]);
    const definitionsHtml = defs.map((n) => `
      <div class="kconfig-def-row">
        ${this.fileLink(n.file, n.line)}
        <span class="kconfig-muted">${escapeHtml(n.prompt ? `"${n.prompt}"` : "(no prompt)")}</span>
        <div class="kconfig-location">Location: ${escapeHtml(["Main menu", ...e.menuPath(n.id)].join(" › "))}</div>
        ${n.prompt ? `<div class="kconfig-muted">Visible if: ${this.exprHtml(n.pvis)}</div>` : ""}
      </div>`).join("");

    const defaultsHtml = s.defaults.length
      ? s.defaults.map(([v, c], i) => {
        const active = this.engineActive && e.exprTri(c) !== 0 && s.defaults.findIndex(([, cc]) => e.exprTri(cc) !== 0) === i;
        return `<div class="kconfig-default ${active ? "active" : ""}"><code>default ${escapeHtml(e.exprStr(v))}</code>${c >= 0 ? ` <span class="kconfig-muted">if</span> ${this.exprHtml(c)}` : ""}${active ? ' <span class="kconfig-tag">active</span>' : ""}</div>`;
      }).join("")
      : `<span class="kconfig-muted">No defaults (n / empty)</span>`;

    const relList = (items) => items.length
      ? items.map(([target, cond]) => `<div class="kconfig-rel-row">${this.symLink(target)} <span class="kconfig-muted">when</span> ${this.exprHtml(cond)}</div>`).join("")
      : `<span class="kconfig-muted">None</span>`;

    const revTerms = e.orTerms(s.rev_dep).filter((id) => !(e.exprs[id][0] === "&" && e.exprs[e.exprs[id][2]]?.[0] === "s" && e.exprs[e.exprs[id][2]][1] === "m"));
    const selectedByHtml = revTerms.length
      ? revTerms.map((id) => `<div class="kconfig-rel-row">${this.exprHtml(id)}</div>`).join("")
      : `<span class="kconfig-muted">Not selected by any option</span>`;
    const impliedByHtml = s.implied >= 0 ? e.orTerms(s.implied).map((id) => `<div class="kconfig-rel-row">${this.exprHtml(id)}</div>`).join("") : "";

    let choiceHtml = "";
    if (s.choice) {
      const cs = e.syms[s.choice];
      const selected = this.engineActive ? e.choiceSelection(s.choice) : null;
      choiceHtml = `
        <div class="kconfig-prop-card">
          <div class="kconfig-prop-title">Choice group${cs.optional ? " (optional)" : ""}</div>
          <div class="kconfig-badge-list">${cs.members.map((m) => `<span class="kconfig-badge kconfig-badge-link ${m === selected || this.valueOf(m) === "y" ? "dep-met" : ""}" data-symbol="${escapeHtml(m)}">${m === selected ? "(X)" : "( )"} ${escapeHtml(m)}</span>`).join("")}</div>
        </div>`;
    }

    const rangesHtml = s.ranges.length
      ? `<div class="kconfig-prop-card"><div class="kconfig-prop-title">Ranges</div>${s.ranges.map(([lo, hi, c]) => `<div><code>range ${escapeHtml(e.exprStr(lo))} ${escapeHtml(e.exprStr(hi))}</code>${c >= 0 ? ` <span class="kconfig-muted">if</span> ${this.exprHtml(c)}` : ""}</div>`).join("")}</div>`
      : "";

    detailCol.innerHTML = `
      <div class="kconfig-symbol-header">
        <div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <div class="kconfig-symbol-title">CONFIG_${escapeHtml(sym)}</div>
            <span class="kconfig-type-badge">${escapeHtml(typeLabel)}</span>
            ${s.env ? `<span class="kconfig-type-badge">env ${escapeHtml(s.env)}</span>` : ""}
          </div>
          <div class="kconfig-prompt">${escapeHtml(defs.find((n) => n.prompt)?.prompt || "No prompt (set only through defaults or select)")}</div>
          <div class="kconfig-value-line">Value: <code>${escapeHtml(val)}</code>${this.engineActive ? ` <span class="kconfig-muted">— ${escapeHtml(this.explainValue(sym))}</span>` : ' <span class="kconfig-muted">— raw (engine off)</span>'}</div>
        </div>
        <div class="kconfig-value-controls">${controls.join("")}</div>
      </div>

      ${this.engineActive ? `
      <div class="kconfig-prop-card">
        <div class="kconfig-prop-title">Dependencies (evaluated for ARCH=${escapeHtml(this.currentArch)})</div>
        <div>Prompt visible: ${this.triBadge(e.visibility(sym))}${!s.prompts.length ? ' <span class="kconfig-muted">(no prompt)</span>' : ""}</div>
        <div>Depends on: ${this.exprHtml(s.dir_dep)}</div>
        <div>Selected by: ${revTerms.length ? "" : '<span class="kconfig-muted">none</span>'}</div>${revTerms.length ? selectedByHtml : ""}
        ${impliedByHtml ? `<div>Implied by:</div>${impliedByHtml}` : ""}
        ${e.revDep(sym) > 0 && e.dirDep(sym) === 0 ? `<div class="kconfig-warning">⚠ Selected while its direct dependencies are unmet (kconfig warns about this, the value is still forced).</div>` : ""}
      </div>` : ""}

      <div class="kconfig-prop-card">
        <div class="kconfig-prop-title">Defined in (${defs.length} definition${defs.length === 1 ? "" : "s"} for ARCH=${escapeHtml(this.currentArch)})</div>
        ${definitionsHtml || '<span class="kconfig-muted">No definition</span>'}
      </div>

      ${choiceHtml}

      <div class="kconfig-prop-card">
        <div class="kconfig-prop-title">Defaults</div>
        ${defaultsHtml}
      </div>
      ${rangesHtml}

      <div class="kconfig-prop-card">
        <div class="kconfig-prop-title">Selects (forces on)</div>
        ${relList(s.selects)}
        ${s.implies.length ? `<div class="kconfig-prop-title" style="margin-top:6px;">Implies (weak default)</div>${relList(s.implies)}` : ""}
      </div>

      <div class="kconfig-prop-card" id="kconfig-detail-remote">
        <div class="kconfig-muted">Loading help and build information…</div>
      </div>

      <div class="kconfig-prop-card">
        <div class="kconfig-prop-title" style="display:flex;justify-content:space-between;align-items:center;">
          <span>Where CONFIG_${escapeHtml(sym)} is used</span>
          <button class="code-btn" id="btn-kconfig-usage" type="button">Find usages</button>
        </div>
        <div id="kconfig-usage"></div>
      </div>

      <div class="kconfig-prop-card">
        <div class="kconfig-prop-title" style="display:flex;justify-content:space-between;align-items:center;">
          <span>History across releases</span>
          <button class="code-btn" id="btn-kconfig-history" type="button" title="When CONFIG_${escapeHtml(sym)} changed and what changed (type, prompt, depends on, select, help…)">🕰 History</button>
        </div>
      </div>

      <div class="kconfig-prop-card">
        <div class="kconfig-prop-title" style="display:flex;justify-content:space-between;align-items:center;">
          <span>Dependency graph</span>
          <button class="code-btn" id="btn-kconfig-graph" type="button">Show graph</button>
        </div>
        <div id="kconfig-dep-graph"></div>
      </div>
    `;
    if (keepScroll) detailCol.scrollTop = scrollTop;
    this.bindDetail(detailCol, sym);
    this.fillRemoteDetail(sym, token, detailCol);
  }

  bindDetail(detailCol, sym) {
    detailCol.querySelectorAll(".kconfig-set-btn").forEach((btn) => {
      btn.onclick = () => this.applyValue(sym, btn.dataset.value);
    });
    const input = detailCol.querySelector("#kconfig-value-input");
    if (input) {
      const submit = () => this.applyValue(sym, input.value);
      detailCol.querySelector("#kconfig-value-set").onclick = submit;
      input.onkeydown = (ev) => {
        if (ev.key === "Enter") submit();
      };
    }
    const autosolveBtn = detailCol.querySelector("#btn-autosolve-sym");
    if (autosolveBtn) autosolveBtn.onclick = () => this.autosolve(sym);
    detailCol.querySelector("#btn-kconfig-history").onclick = () => showKconfigHistoryModal(this.currentVersion, sym);
    detailCol.querySelector("#btn-kconfig-graph").onclick = (ev) => {
      ev.target.disabled = true;
      this.renderDependencyGraph(sym, detailCol.querySelector("#kconfig-dep-graph")).finally(() => {
        ev.target.disabled = false;
      });
    };
    detailCol.querySelector("#btn-kconfig-usage").onclick = (ev) => {
      ev.target.disabled = true;
      this.renderUsage(sym, detailCol.querySelector("#kconfig-usage")).finally(() => {
        ev.target.disabled = false;
      });
    };
    this.bindLinks(detailCol);
  }

  bindLinks(root) {
    root.querySelectorAll(".kconfig-badge-link[data-symbol]").forEach((badge) => {
      if (badge.dataset.bound) return;
      badge.dataset.bound = "1";
      badge.onclick = (ev) => {
        ev.stopPropagation();
        if (badge.dataset.symbol) this.selectAndInspectSymbol(badge.dataset.symbol);
      };
      badge.addEventListener("auxclick", (ev) => {
        if (ev.button === 1 && badge.dataset.symbol) {
          ev.preventDefault();
          this.openSymbolTab(badge.dataset.symbol);
        }
      });
    });
    root.querySelectorAll(".kconfig-file-link").forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      const open = (forceNew) => {
        const path = btn.dataset.path;
        const line = parseInt(btn.dataset.line || "1", 10);
        if (path.endsWith("/")) {
          toast.info(`${path} is a directory (built when the rule applies)`);
          return;
        }
        state.openTab({ type: "code", title: path.split("/").pop(), path, cursorLine: line, version: this.currentVersion, forceNew });
      };
      btn.onclick = () => open(true);
      btn.addEventListener("auxclick", (ev) => {
        if (ev.button === 1) {
          ev.preventDefault();
          open(true);
        }
      });
    });
  }

  /** Help text and compiled files from the server (arch-scoped). */
  async fillRemoteDetail(sym, token, detailCol) {
    let detail;
    try {
      detail = await api.getKconfigSymbol(this.currentVersion, sym, this.currentArch, this.toolchain);
    } catch (err) {
      if (token === this.detailToken) {
        const box = detailCol.querySelector("#kconfig-detail-remote");
        if (box) box.innerHTML = `<div class="kconfig-error">${escapeHtml(err.message)}</div>`;
      }
      return;
    }
    if (token !== this.detailToken) return;
    const box = detailCol.querySelector("#kconfig-detail-remote");
    if (!box) return;
    if (detail.defined_for_arch === false) {
      box.innerHTML = `
        <div class="kconfig-prop-title">Not defined for ARCH=${escapeHtml(this.currentArch)}</div>
        <div class="kconfig-muted">Declared for other architectures in:</div>
        ${(detail.declarations || []).map((d) => `<div>${this.fileLink(d.file_path, d.line_s)}</div>`).join("") || '<span class="kconfig-muted">nowhere</span>'}`;
      this.bindLinks(box);
      return;
    }
    const compiled = detail.compiled_files || [];
    const rules = (detail.kbuild || []).filter((r) => r.kind === "directory" && !r.other_arch);
    const own = compiled.filter((f) => !f.other_arch);
    const other = compiled.filter((f) => f.other_arch);
    const fileRow = (f) => `<div class="kconfig-file-row ${f.other_arch ? "kconfig-other-arch" : ""}">
        ${this.fileLink(f.file_path, 1, f.file_path)}
        <span class="kconfig-muted">${escapeHtml(f.target_obj || "")} · ${escapeHtml(f.compile_mode || "")}</span>
        ${f.makefile ? this.fileLink(f.makefile, f.line, `${f.makefile}:${f.line}`) : ""}
      </div>`;
    box.innerHTML = `
      <div class="kconfig-prop-title">Help</div>
      <pre class="kconfig-help">${escapeHtml(detail.help || "There is no help available for this option.")}</pre>
      <div class="kconfig-prop-title" style="margin-top:8px;">Files built when enabled (${own.length})</div>
      ${own.map(fileRow).join("") || '<span class="kconfig-muted">No Makefile rule names this option directly.</span>'}
      ${rules.length ? `<div class="kconfig-prop-title" style="margin-top:8px;">Directories descended into</div>
        ${rules.map((r) => `<div class="kconfig-file-row ${r.other_arch ? "kconfig-other-arch" : ""}"><code>${escapeHtml(r.path)}</code> ${this.fileLink(r.makefile, r.line, `${r.makefile}:${r.line}`)}</div>`).join("")}` : ""}
      ${other.length ? `<details style="margin-top:6px;"><summary class="kconfig-muted">${other.length} file(s) under other architectures</summary>${other.map(fileRow).join("")}</details>` : ""}
    `;
    this.bindLinks(box);
  }

  async renderUsage(sym, container) {
    container.innerHTML = `<div class="kconfig-muted">Searching ${escapeHtml(this.currentVersion)}…</div>`;
    let usage;
    try {
      usage = await api.getKconfigUsage(this.currentVersion, sym, this.currentArch, this.toolchain);
    } catch (err) {
      container.innerHTML = `<div class="kconfig-error">${escapeHtml(err.message)}</div>`;
      return;
    }
    const byFile = new Map();
    for (const r of usage.source_refs || []) {
      if (!byFile.has(r.file_path)) byFile.set(r.file_path, []);
      byFile.get(r.file_path).push(r);
    }
    const kconfigRefs = usage.kconfig_refs || [];
    const kbuild = (usage.kbuild || []).filter((r) => r.via === "condition" || r.kind === "directory");
    container.innerHTML = `
      <div class="kconfig-muted">${usage.source_ref_count} reference(s) in ${usage.source_file_count} source file(s)${usage.source_ref_count > (usage.source_refs || []).length ? ` (first ${(usage.source_refs || []).length} shown)` : ""}</div>
      ${[...byFile.entries()].map(([file, refs]) => `
        <details class="kconfig-usage-file ${refs[0].other_arch ? "kconfig-other-arch" : ""}">
          <summary>${escapeHtml(file)} <span class="kconfig-muted">(${refs.length})</span></summary>
          ${refs.map((r) => `<div class="kconfig-usage-line">${this.fileLink(r.file_path, r.line, String(r.line))} <code>${escapeHtml(r.text)}</code></div>`).join("")}
        </details>`).join("")}
      ${kbuild.length ? `<div class="kconfig-prop-title" style="margin-top:8px;">Makefile conditions</div>${kbuild.map((r) => `<div>${this.fileLink(r.makefile, r.line, `${r.makefile}:${r.line}`)} <code>${escapeHtml(r.rule)}</code></div>`).join("")}` : ""}
      <div class="kconfig-prop-title" style="margin-top:8px;">Kconfig entries referring to it (${kconfigRefs.length})</div>
      ${kconfigRefs.map((r) => `<div class="kconfig-rel-row">${this.symLink(r.by)} <span class="kconfig-muted">${escapeHtml(r.kind.replace(/_/g, " "))}</span> ${this.fileLink(r.file, r.line)}</div>`).join("") || '<span class="kconfig-muted">None</span>'}
    `;
    this.bindLinks(container);
  }

  async autosolve(sym) {
    toast.info(`Searching prerequisites for CONFIG_${sym}…`);
    let res;
    try {
      res = await api.autosolveKconfig(this.currentVersion, { target_symbol: sym, arch: this.currentArch, toolchain: this.toolchain, current_values: this.engine.configValues() });
    } catch (err) {
      toast.error(`Auto-solve failed: ${err.message}`);
      return;
    }
    const toggles = res.toggles_needed || [];
    let applied = 0;
    for (const t of toggles) {
      if (this.engine.setValue(t.symbol, t.set_to || t.to || "y")) applied++;
    }
    if (applied) this.changed();
    if (this.engine.value(sym) !== "n") toast.success(`CONFIG_${sym}=${this.engine.value(sym)} after ${applied} change(s): ${toggles.map((t) => `${t.symbol}=${t.set_to}`).join(", ")}`);
    else toast.error(`CONFIG_${sym} cannot be enabled: ${(res.blocked_by || []).join("; ") || "no legal path"}`);
  }

  /** Depends-on (left) and selects/implies (right) relations of a symbol as a clickable SVG graph. */
  async renderDependencyGraph(symbolName, container) {
    container.innerHTML = `<div class="kconfig-muted">Loading graph…</div>`;
    let graph;
    try {
      graph = await api.getKconfigGraph(this.currentVersion, this.currentArch, symbolName, 1, this.toolchain);
    } catch (err) {
      container.innerHTML = `<div class="kconfig-error">${escapeHtml(err.message)}</div>`;
      return;
    }
    const edges = graph.edges || [];
    const left = [...new Set(edges.filter((e) => e.type === "depends_on" && e.from === symbolName).map((e) => e.to))];
    const right = [...new Set(edges.filter((e) => e.type !== "depends_on" && e.from === symbolName).map((e) => e.to))];
    if (!left.length && !right.length) {
      container.innerHTML = `<div class="kconfig-muted">No own depends-on, select or imply relations.</div>`;
      return;
    }
    const ROW = 26;
    const NODE_W = 170;
    const W = 620;
    const rows = Math.max(left.length, right.length, 1);
    const H = rows * ROW + 30;
    const rootY = H / 2;
    const colY = (count, i) => (H - count * ROW) / 2 + i * ROW + ROW / 2;
    const val = (sym) => (this.engine.isDefined(sym) ? this.valueOf(sym) : "?");
    const fill = (sym) => ({ y: "rgba(63,185,80,0.25)", m: "rgba(210,153,34,0.25)" }[val(sym)] || "rgba(110,118,129,0.18)");
    const stroke = (sym) => ({ y: "#3fb950", m: "#d29922" }[val(sym)] || "#6e7681");
    const node = (sym, x, y, isRoot = false) => `
      <g class="kgraph-node" data-symbol="${escapeHtml(sym)}" style="cursor:pointer;">
        <rect x="${x}" y="${y - 10}" width="${NODE_W}" height="20" rx="4" fill="${isRoot ? "rgba(56,139,253,0.25)" : fill(sym)}" stroke="${isRoot ? "#58a6ff" : stroke(sym)}" />
        <text x="${x + 8}" y="${y + 4}" font-size="11" font-family="var(--font-mono)" fill="#c9d1d9">${escapeHtml(sym.length > 22 ? `${sym.slice(0, 21)}…` : sym)} = ${escapeHtml(val(sym))}</text>
        <title>CONFIG_${escapeHtml(sym)} = ${escapeHtml(val(sym))}</title>
      </g>`;
    const rootX = (W - NODE_W) / 2;
    let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;display:block;">
      <text x="10" y="14" font-size="10" fill="#8b949e">depends on</text>
      <text x="${W - 10}" y="14" font-size="10" fill="#8b949e" text-anchor="end">selects / implies</text>`;
    left.forEach((_sym, i) => {
      const y = colY(left.length, i);
      svg += `<path d="M ${10 + NODE_W} ${y} C ${rootX - 30} ${y}, ${rootX - 30} ${rootY}, ${rootX} ${rootY}" fill="none" stroke="#6e7681" stroke-opacity="0.6" />`;
    });
    right.forEach((_sym, i) => {
      const y = colY(right.length, i);
      svg += `<path d="M ${rootX + NODE_W} ${rootY} C ${W - NODE_W - 40} ${rootY}, ${W - NODE_W - 40} ${y}, ${W - NODE_W - 10} ${y}" fill="none" stroke="#bc8cff" stroke-opacity="0.6" />`;
    });
    left.forEach((sym, i) => { svg += node(sym, 10, colY(left.length, i)); });
    right.forEach((sym, i) => { svg += node(sym, W - NODE_W - 10, colY(right.length, i)); });
    svg += node(symbolName, rootX, rootY, true);
    svg += `</svg><div class="kconfig-muted" style="font-size:10px;margin-top:4px;">Green = y, amber = m, grey = n under the current configuration. Click a node to inspect it.</div>`;
    container.innerHTML = svg;
    container.querySelectorAll(".kgraph-node").forEach((g) => {
      g.addEventListener("click", () => {
        if (g.dataset.symbol && g.dataset.symbol !== symbolName) this.selectAndInspectSymbol(g.dataset.symbol);
      });
    });
  }

  // ------------------------------------------------------------------ validation / compare / export
  /** Kconfig consistency: engine-on configs are valid by construction, raw ones are resolved. */
  showValidation() {
    if (!this.engine.loaded) return;
    const modal = openModal({ title: "Validate configuration", subtitle: `ARCH=${this.currentArch} @ ${this.currentVersion}`, width: "720px" });
    const { probe, raw, resolved, adjusted, unknown } = this.resolutionDiff();
    const warnings = Object.keys(probe.syms).filter((n) => {
      const s = probe.syms[n];
      return (s.type === "bool" || s.type === "tristate") && !s.choice && probe.revDep(n) > 0 && probe.dirDep(n) === 0;
    });
    const row = (html) => `<div class="kconfig-validate-row">${html}</div>`;
    modal.body.innerHTML = `
      ${adjusted.length
        ? `<div class="kconfig-prop-card"><div class="kconfig-prop-title" style="color:var(--accent-yellow);">Values kconfig would not keep (${adjusted.length})</div>
            ${adjusted.slice(0, 400).map((a) => row(`<code>CONFIG_${escapeHtml(a.name)}</code> ${escapeHtml(a.want)} → <strong>${escapeHtml(a.got || '""')}</strong> <span class="kconfig-muted">${escapeHtml(a.reason)}</span>`)).join("")}</div>`
        : `<div class="ki-modal-empty">✓ Every value is accepted by kconfig for ARCH=${escapeHtml(this.currentArch)}${this.engineActive ? " (the engine only allows valid values)" : ""}.</div>`}
      ${unknown.length ? `<div class="kconfig-prop-card"><div class="kconfig-prop-title">Unknown for ARCH=${escapeHtml(this.currentArch)} (${unknown.length}, dropped by kconfig)</div><div class="kconfig-muted" style="font-family:var(--font-mono);font-size:11px;">${unknown.slice(0, 200).map(escapeHtml).join(" ")}</div></div>` : ""}
      ${warnings.length ? `<div class="kconfig-prop-card"><div class="kconfig-prop-title" style="color:var(--accent-orange);">kconfig warnings: selected with unmet direct dependencies (${warnings.length})</div>
          ${warnings.map((n) => row(`<code>CONFIG_${escapeHtml(n)}</code> <span class="kconfig-muted">selected by ${escapeHtml(probe.exprStr(probe.syms[n].rev_dep, true))}; depends on ${escapeHtml(probe.exprStr(probe.syms[n].dir_dep, true))}</span>`)).join("")}
          <div class="kconfig-muted">The real kconfig prints the same warning and keeps the value; the build may still fail for these options.</div></div>` : ""}
    `;
    if (adjusted.length || unknown.length) {
      modal.footer.insertAdjacentHTML("afterbegin", `<button class="code-btn active" id="btn-apply-validation" type="button">Apply kconfig resolution</button>`);
      modal.footer.querySelector("#btn-apply-validation").onclick = () => {
        this.engine.loadAssignments(raw);
        if (this.engineActive) this.commit();
        else {
          this.rawValues = resolved;
          this.commit();
        }
        this.renderTree();
        modal.close();
        toast.success("Configuration replaced by its kconfig resolution");
      };
    }
  }

  /** Compare this tab's configuration with a defconfig of its ARCH, or with another KConfig tab. */
  async showCompareModal() {
    const modal = openModal({ title: "Compare configurations", subtitle: `ARCH=${this.currentArch} @ ${this.currentVersion}`, width: "760px" });
    modal.body.innerHTML = `<div class="ki-modal-empty">Loading defconfigs…</div>`;
    let list = [];
    let listError = "";
    try {
      const defs = await api.getKconfigDefconfigs(this.currentVersion, this.currentArch);
      list = (defs && defs.defconfigs) || [];
    } catch (err) {
      listError = err.message;
    }
    const otherTabs = state.kconfigTabs().filter((t) => t.id !== this.tabId);
    if (!list.length && !otherTabs.length) {
      modal.body.innerHTML = `<div class="ki-modal-empty">${listError
        ? `Could not load the defconfigs of ARCH=${escapeHtml(this.currentArch)}: ${escapeHtml(listError)}`
        : `No defconfig for ARCH=${escapeHtml(this.currentArch)} and no other KConfig tab open to compare with.`}</div>`;
      return;
    }

    modal.body.innerHTML = `
      <div class="ki-form-row"><label>Compare with
        <select id="cmp-source">
          ${list.length ? `<option value="defconfig">A defconfig of ARCH=${escapeHtml(this.currentArch)}</option>` : ""}
          ${otherTabs.map((t) => `<option value="tab:${escapeHtml(t.id)}">${escapeHtml(t.title)} · ${escapeHtml(t.version)}</option>`).join("")}
        </select>
      </label></div>
      <div class="ki-form-row" id="cmp-defconfig-row"><label>Defconfig
        <select id="cmp-defconfig">${list.map((d) => `<option value="${escapeHtml(d.file_path)}" ${d.is_canonical ? "selected" : ""}>${escapeHtml(d.name)}</option>`).join("")}</select>
      </label></div>
      ${!list.length && listError ? `<div class="kconfig-muted">Defconfigs unavailable: ${escapeHtml(listError)}</div>` : ""}
      <div id="cmp-defconfig-result"></div>`;
    const source = modal.body.querySelector("#cmp-source");
    const defRow = modal.body.querySelector("#cmp-defconfig-row");
    const defSelect = modal.body.querySelector("#cmp-defconfig");
    const run = async () => {
      const out = modal.body.querySelector("#cmp-defconfig-result");
      const useDefconfig = source.value === "defconfig";
      defRow.style.display = useDefconfig ? "" : "none";
      out.innerHTML = `<div class="ki-modal-empty">Comparing…</div>`;
      try {
        const side = useDefconfig
          ? await this.defconfigSide(defSelect.value)
          : this.tabSide(source.value.slice(4));
        this.renderComparison(out, modal, side);
      } catch (err) {
        out.innerHTML = `<div class="ki-modal-error">${escapeHtml(err.message)}</div>`;
      }
    };
    source.onchange = run;
    defSelect.onchange = run;
    run();
  }

  /** Right-hand side of the compare window: a defconfig resolved for this ARCH. */
  async defconfigSide(path) {
    if (!this.engine.loaded) throw new Error("The Kconfig model of this tab is not loaded yet");
    const res = await api.getKconfigDefconfigContent(this.currentVersion, path, this.currentArch, this.toolchain);
    const probe = new KconfigEngine(this.engine.model);
    probe.loadConfig(res.content || "");
    probe.setAllNewSymbols("default");
    return { label: res.name, values: probe.configValues(), notes: [] };
  }

  /**
   * Right-hand side of the compare window: another KConfig tab's configuration, as that tab holds
   * it (`state.kconfigProfiles`, resolved while its engine is on, raw while it is off). Its ARCH and
   * revision may differ from this tab's, so the differences are reported with that caveat.
   */
  tabSide(tabId) {
    const tab = state.findTab(tabId);
    if (!tab || tab.type !== "kconfig") throw new Error("That KConfig tab is no longer open");
    const key = state.kconfigProfileKeyOf(tab);
    const label = `${tab.title} · ${tab.version}`;
    const saved = state.getKconfigProfile(key);
    if (!saved || !Object.keys(saved).length) {
      return { label, values: {}, notes: [`${tab.title} has no configuration yet — open that tab once to load one.`] };
    }
    const notes = [];
    if (tab.arch !== this.currentArch) notes.push(`Different ARCH (${tab.arch} vs ${this.currentArch}): options only one of them defines show “—” on the other side.`);
    if (tab.version !== this.currentVersion) notes.push(`Different revision (${tab.version} vs ${this.currentVersion}).`);
    if (!state.isKconfigEngineOwner(tab.id)) notes.push(`The engine is off in ${tab.title}, so its raw (unresolved) values are compared.`);
    return { label, values: saved, notes };
  }

  /** Table of the options whose value differs between this tab and the chosen side. */
  renderComparison(out, modal, { label, values, notes = [] }) {
    const MAX_ROWS = 2000; // comparing two architectures differs in thousands of options
    const mine = this.rawValues || {};
    const keys = [...new Set([...Object.keys(mine), ...Object.keys(values)])].sort();
    const diffs = keys.filter((k) => (mine[k] ?? "—") !== (values[k] ?? "—"));
    out.innerHTML = `
      ${notes.length ? `<div class="kconfig-muted" style="margin:6px 0;">${notes.map(escapeHtml).join(" ")}</div>` : ""}
      <div class="kconfig-muted" style="margin:6px 0 10px;">${keys.length - diffs.length} matching · <strong>${diffs.length}</strong> different${this.engineActive ? " (resolved values)" : " (this tab's raw values: engine off)"}</div>
      <table class="kconfig-cmp-table"><thead><tr><th>Option</th><th>Current</th><th>${escapeHtml(label)}</th></tr></thead>
      <tbody>${diffs.slice(0, MAX_ROWS).map((k) => `<tr data-symbol="${escapeHtml(k)}" title="Inspect CONFIG_${escapeHtml(k)}"><td>CONFIG_${escapeHtml(k)}</td><td>${escapeHtml(mine[k] ?? "—")}</td><td>${escapeHtml(values[k] ?? "—")}</td></tr>`).join("")}</tbody></table>
      ${diffs.length > MAX_ROWS ? `<div class="kconfig-muted" style="margin-top:8px;">… and ${diffs.length - MAX_ROWS} more</div>` : ""}`;
    out.querySelectorAll("tr[data-symbol]").forEach((tr) => {
      tr.onclick = () => {
        modal.close();
        this.selectAndInspectSymbol(tr.dataset.symbol);
      };
    });
  }

  /** Full .config, byte-identical to what scripts/kconfig writes (engine on). */
  exportConfig() {
    if (!this.engine.loaded) return;
    const text = this.engineActive
      ? this.engine.writeConfig()
      : KconfigParser.serialize(this.rawValues || {}, this.currentVersion, this.currentArch, (n) => this.engine.syms[n]?.type);
    const name = `${this.currentArch}-${this.currentVersion}.config`;
    downloadText(name, text);
    toast.success(this.engineActive
      ? `Exported ${name}: copy it to <kernel tree>/.config and run make ARCH=${this.currentArch}`
      : `Exported ${name} (unvalidated: run make ARCH=${this.currentArch} olddefconfig first)`);
  }

  /** Minimal defconfig (make savedefconfig); loadable with make <name>_defconfig. */
  exportDefconfig() {
    if (!this.engine.loaded) return;
    if (!this.engineActive) {
      toast.error("Enable the engine to compute a minimal defconfig");
      return;
    }
    const name = `${this.currentArch}_custom_defconfig`;
    downloadText(name, this.engine.writeDefconfig());
    toast.success(`Saved ${name}: place it in arch/${this.engine.model.srcarch}/configs/ and run make ARCH=${this.currentArch} ${name}`);
  }

  async showDefconfigModal() {
    let list = [];
    try {
      const defs = await api.getKconfigDefconfigs(this.currentVersion, this.currentArch);
      list = (defs && defs.defconfigs) || [];
    } catch (err) {
      toast.error(`Failed to fetch defconfigs: ${err.message}`);
      return;
    }
    if (!list.length) {
      toast.info(`No defconfig found for ARCH=${this.currentArch}.`);
      return;
    }
    const modal = openModal({ title: `Defconfigs (ARCH=${this.currentArch})`, subtitle: `${list.length} in ${this.currentVersion}`, width: "560px" });
    modal.body.innerHTML = `
      <input type="text" class="kconfig-value-input" id="defconfig-filter" placeholder="Filter…" style="width:100%;margin-bottom:8px;" />
      <div id="defconfig-list" style="max-height:420px;overflow-y:auto;display:flex;flex-direction:column;gap:4px;">
        ${list.map((d) => `
          <div class="palette-item def-item" data-name="${escapeHtml(d.name.toLowerCase())}" style="display:flex;justify-content:space-between;align-items:center;padding:6px 10px;">
            <div>
              <div style="font-weight:600;color:var(--text-primary);">${escapeHtml(d.name)}${d.is_canonical ? ' <span class="kconfig-tag">make defconfig</span>' : ""}</div>
              <div class="palette-item-desc">${escapeHtml(d.file_path)}</div>
            </div>
            <button class="code-btn active btn-load-single-def" data-path="${escapeHtml(d.file_path)}">Load</button>
          </div>`).join("")}
      </div>`;
    const filter = modal.body.querySelector("#defconfig-filter");
    filter.oninput = () => {
      const q = filter.value.trim().toLowerCase();
      modal.body.querySelectorAll(".def-item").forEach((el) => {
        el.style.display = el.dataset.name.includes(q) ? "" : "none";
      });
    };
    modal.body.querySelectorAll(".btn-load-single-def").forEach((btn) => {
      btn.onclick = async () => {
        modal.close();
        await this.loadDefconfig(btn.dataset.path);
        if (this.selectedSymbol) this.inspectSymbol(this.selectedSymbol);
      };
    });
  }

  // ------------------------------------------------------------------ toolchain
  /**
   * Re-fetch the model (new toolchain) and resolve the current values through it. A different model
   * resolves dependencies and defaults differently, so the values it would change are shown first
   * and nothing is touched until they are confirmed (§5.0). Returns the number of values changed,
   * or null when the user declined (model and values left as they were).
   */
  async reloadModel() {
    const before = { ...(this.rawValues || {}) };
    const raw = await api.getKconfigTree(this.currentVersion, this.currentArch, this.toolchain);
    // Resolve on a throwaway engine: the live one keeps the current model until this is accepted
    let changes = [];
    if (this.engineActive) {
      const probe = new KconfigEngine(raw);
      probe.loadAssignments(before);
      const after = probe.configValues();
      const names = new Set([...Object.keys(before), ...Object.keys(after)]);
      changes = [...names]
        .filter((n) => String(before[n] ?? "") !== String(after[n] ?? ""))
        .map((n) => ({ name: n, want: String(before[n] ?? ""), got: String(after[n] ?? "") }));
    }
    if (changes.length && !(await this.confirmModelChanges(changes))) return null;
    this.engine.setModel(raw);
    if (this.engineActive) this.engine.loadAssignments(before);
    this.commit();
    this.renderTree();
    if (this.selectedSymbol) this.inspectSymbol(this.selectedSymbol);
    return changes.length;
  }

  /** Ask before keeping a rebuilt model that resolves the configuration differently. */
  confirmModelChanges(changes) {
    const MAX_ROWS = 300;
    return new Promise((resolve) => {
      let confirmed = false;
      const modal = openModal({
        title: "This toolchain changes the configuration",
        subtitle: `${changes.length} value${changes.length === 1 ? "" : "s"} resolve differently with the rebuilt model`,
        width: "760px",
        footerHtml: `<button class="code-btn active" id="kconfig-confirm-model" type="button">Apply the toolchain and these changes</button>`,
        onClose: () => resolve(confirmed),
      });
      modal.body.innerHTML = `
        <div class="kconfig-muted" style="margin-bottom:10px;">The toolchain answers the <code>$(shell,…)</code> checks and environment variables Kconfig reads while it is parsed, so dependencies, defaults and ranges can differ. <strong>Close</strong> keeps the current toolchain, model and values.</div>
        <table class="kconfig-cmp-table"><thead><tr><th>Option</th><th>Now</th><th>After</th></tr></thead>
        <tbody>${changes.slice(0, MAX_ROWS).map((c) => `<tr><td>CONFIG_${escapeHtml(c.name)}</td><td>${escapeHtml(c.want || '""')}</td><td>${escapeHtml(c.got || '""')}</td></tr>`).join("")}</tbody></table>
        ${changes.length > MAX_ROWS ? `<div class="kconfig-muted" style="margin-top:8px;">… and ${changes.length - MAX_ROWS} more</div>` : ""}`;
      const ok = modal.footer.querySelector("#kconfig-confirm-model");
      ok.onclick = () => {
        confirmed = true;
        modal.close();
      };
      ok.focus();
    });
  }

  /**
   * Toolchain dialog: compiler/linker identity, every environment variable Kconfig read and every
   * toolchain check ($(cc-option,...), version scripts, compile tests) with how it was answered.
   */
  async showToolchainModal() {
    if (!this.engine.loaded) return;
    const report = this.engine.model.toolchain || { macros: false, profile: {}, env_used: [], probes: [], messages: [] };
    const spec = JSON.parse(JSON.stringify(this.toolchain || {}));
    spec.env = spec.env || {};
    spec.probes = spec.probes || {};
    let defaults = null;
    try {
      defaults = await api.getKconfigToolchainDefaults(this.currentVersion, this.currentArch);
    } catch (err) {
      console.warn("Toolchain defaults unavailable:", err);
    }
    const eff = report.profile || {};
    const modal = openModal({ title: "Toolchain & Kconfig environment", subtitle: `ARCH=${this.currentArch} @ ${this.currentVersion}`, width: "980px" });
    const BOOL_KINDS = new Set(["cc-option", "ld-option", "as-instr", "cc-compile", "script", "file", "command"]);
    const field = (key, label, placeholder, hint = "") => `
      <label class="kconfig-tc-field"><span>${escapeHtml(label)}</span>
        <input class="kconfig-value-input" data-key="${key}" value="${escapeHtml(spec[key] ?? "")}" placeholder="${escapeHtml(placeholder ?? "")}" spellcheck="false" />
        ${hint ? `<small class="kconfig-muted">${escapeHtml(hint)}</small>` : ""}</label>`;
    const select = (key, label, options) => `
      <label class="kconfig-tc-field"><span>${escapeHtml(label)}</span>
        <select data-key="${key}">${options.map(([v, t]) => `<option value="${v}" ${String(spec[key] ?? "") === v ? "selected" : ""}>${escapeHtml(t)}</option>`).join("")}</select></label>`;
    const sourceBadge = (src) => `<span class="kconfig-tc-src kconfig-tc-src-${src}">${escapeHtml(src)}</span>`;
    const usedBy = (p) => (p.locations || []).slice(0, 4).map((l) => l.symbol
      ? `<span class="kconfig-badge kconfig-badge-link" data-symbol="${escapeHtml(l.symbol)}">${escapeHtml(l.symbol)}</span>`
      : this.fileLink(l.file, l.line)).join(" ") + ((p.locations || []).length > 4 ? ` <span class="kconfig-muted">+${p.locations.length - 4}</span>` : "");
    const probeRow = (p) => {
      const override = spec.probes[p.key];
      const control = BOOL_KINDS.has(p.kind)
        ? `<select class="kconfig-tc-probe" data-probe="${escapeHtml(p.key)}">
             <option value="" ${override === undefined ? "selected" : ""}>auto</option>
             <option value="y" ${override === "y" ? "selected" : ""}>force y</option>
             <option value="n" ${override === "n" ? "selected" : ""}>force n</option></select>`
        : `<input class="kconfig-value-input kconfig-tc-probe" data-probe="${escapeHtml(p.key)}" value="${escapeHtml(override ?? "")}" placeholder="${escapeHtml(p.result)}" spellcheck="false" />`;
      return `<tr data-filter="${escapeHtml(`${p.label} ${p.key} ${(p.locations || []).map((l) => l.symbol || "").join(" ")}`.toLowerCase())}">
        <td><div title="${escapeHtml(p.command)}">${escapeHtml(p.label)}</div><small class="kconfig-muted">${escapeHtml(p.note || p.kind)}</small></td>
        <td><code>${escapeHtml(p.result === "" ? '""' : p.result)}</code></td>
        <td>${sourceBadge(p.source)}</td>
        <td>${usedBy(p)}</td>
        <td>${control}</td></tr>`;
    };
    const assumed = (report.probes || []).filter((p) => p.source === "assumed").length;
    modal.body.innerHTML = `
      ${(report.messages || []).length ? `<div class="kconfig-prop-card">${report.messages.map((m) => `<div class="${m.level === "error" ? "kconfig-error" : "kconfig-warning"}">${escapeHtml(m.level)}: ${escapeHtml(m.text)} <span class="kconfig-muted">(${escapeHtml(m.file)}:${m.line})</span></div>`).join("")}
        ${report.messages.some((m) => m.level === "error") ? '<div class="kconfig-muted">The real kconfig stops on these errors; the configuration below is what it would compute if it went on.</div>' : ""}</div>` : ""}
      ${report.macros ? "" : `<div class="kconfig-prop-card kconfig-muted">This revision predates the Kconfig macro language (Linux 4.18): the compiler cannot influence the configuration. Only the environment variables below (read through <code>option env=</code>) matter.</div>`}
      <div class="kconfig-prop-card">
        <div class="kconfig-prop-title">Toolchain${report.macros ? "" : " (no effect on this revision)"}</div>
        <div class="kconfig-tc-grid">
          ${select("cc", "Compiler", [["gcc", "GCC"], ["clang", "Clang / LLVM"]])}
          ${field("cc_version", "Compiler version", eff.cc_version)}
          ${select("ld", "Linker", [["", `auto (${eff.ld === "lld" ? "LLD" : "GNU ld"})`], ["bfd", "GNU ld (BFD)"], ["lld", "LLD"]])}
          ${field("ld_version", "Linker version", eff.ld_version)}
          ${select("assembler", "Assembler", [["", `auto (${eff.assembler === "llvm" ? "integrated" : "GNU as"})`], ["gnu", "GNU as"], ["llvm", "LLVM integrated"]])}
          ${field("binutils_version", "Binutils version", eff.binutils_version)}
          ${field("cross_compile", "CROSS_COMPILE", "", "e.g. aarch64-linux-gnu-")}
          ${select("llvm_utils", "LLVM utilities (LLVM=1)", [["", `auto (${eff.llvm_utils ? "yes" : "no"})`], ["true", "yes"], ["false", "no"]])}
          ${field("pahole_version", "pahole version", eff.pahole_version || "not installed", '"none" = not installed')}
          ${field("rustc_version", "rustc version", eff.rustc_version || "not installed", 'e.g. 1.75.0; "none" = no Rust toolchain')}
          ${select("gcc_plugins", "GCC plugin headers", [["", eff.gcc_plugins ? "auto (yes)" : "auto (no)"], ["true", "installed"], ["false", "missing"]])}
          ${field("host_release", "Build host uname -r", eff.host_release)}
        </div>
        ${defaults ? `<div class="kconfig-muted">Defaults for Linux ${escapeHtml(defaults.kernelversion)}: gcc ${escapeHtml(defaults.gcc.cc_version)} + binutils ${escapeHtml(defaults.gcc.binutils_version)}, or clang/LLD ${escapeHtml(defaults.clang.cc_version)}. Empty fields use them.</div>` : ""}
      </div>
      <div class="kconfig-prop-card">
        <div class="kconfig-prop-title">Environment variables read by Kconfig (${(report.env_used || []).length})</div>
        <table class="kconfig-cmp-table kconfig-tc-table"><thead><tr><th>Variable</th><th>Value</th><th>Override</th></tr></thead><tbody>
          ${(report.env_used || []).map((e) => `<tr><td><code>${escapeHtml(e.name)}</code></td><td><code>${escapeHtml(e.value ?? "(unset)")}</code></td>
            <td><input class="kconfig-value-input kconfig-tc-env" data-env="${escapeHtml(e.name)}" value="${escapeHtml(spec.env[e.name] ?? "")}" placeholder="${escapeHtml(e.value ?? "")}" spellcheck="false" /></td></tr>`).join("")}
          ${Object.keys(spec.env).filter((k) => !(report.env_used || []).some((e) => e.name === k)).map((k) => `<tr><td><code>${escapeHtml(k)}</code></td><td class="kconfig-muted">not read</td>
            <td><input class="kconfig-value-input kconfig-tc-env" data-env="${escapeHtml(k)}" value="${escapeHtml(spec.env[k])}" spellcheck="false" /></td></tr>`).join("")}
          <tr><td><input class="kconfig-value-input" id="kconfig-tc-newvar" placeholder="NEW_VARIABLE" spellcheck="false" /></td><td class="kconfig-muted">add any variable</td>
            <td><input class="kconfig-value-input" id="kconfig-tc-newval" placeholder="value" spellcheck="false" /></td></tr>
        </tbody></table>
      </div>
      ${report.macros ? `<div class="kconfig-prop-card">
        <div class="kconfig-prop-title" style="display:flex;justify-content:space-between;align-items:center;">
          <span>Toolchain checks run by Kconfig (${(report.probes || []).length}, ${assumed} assumed)</span>
          <input class="kconfig-value-input" id="kconfig-tc-filter" placeholder="Filter checks or symbols…" spellcheck="false" />
        </div>
        <div class="kconfig-muted">Sources: <b>toolchain</b> = derived from the profile above, <b>knowledge</b> = known first gcc/clang versions, <b>assumed</b> = no data (assumed to succeed), <b>override</b> = set here.</div>
        <div style="max-height:420px;overflow-y:auto;">
        <table class="kconfig-cmp-table kconfig-tc-table"><thead><tr><th>Check</th><th>Result</th><th>Source</th><th>Used by</th><th>Override</th></tr></thead>
          <tbody>${(report.probes || []).map(probeRow).join("")}</tbody></table></div>
      </div>` : ""}
    `;
    this.bindLinks(modal.body);
    modal.body.querySelectorAll(".kconfig-badge-link[data-symbol]").forEach((b) => b.addEventListener("click", () => modal.close()));
    const filter = modal.body.querySelector("#kconfig-tc-filter");
    if (filter) {
      filter.oninput = () => {
        const q = filter.value.trim().toLowerCase();
        modal.body.querySelectorAll("tr[data-filter]").forEach((tr) => {
          tr.style.display = tr.dataset.filter.includes(q) ? "" : "none";
        });
      };
    }
    modal.footer.insertAdjacentHTML("afterbegin", `
      <button class="code-btn" id="btn-tc-reset" type="button">Reset to defaults</button>
      <button class="code-btn active" id="btn-tc-apply" type="button">Apply</button>`);
    const apply = async (next) => {
      const previous = JSON.parse(JSON.stringify(this.toolchain || {}));
      state.setKconfigToolchain(this.profileKey, next);
      modal.close();
      toast.info("Rebuilding the Kconfig model for this toolchain…");
      try {
        const changed = await this.reloadModel();
        if (changed === null) {
          state.setKconfigToolchain(this.profileKey, previous); // declined: nothing changed
          toast.info("Toolchain profile left as it was");
          return;
        }
        const tc = this.engine.model.toolchain;
        toast.success(`Toolchain ${tc?.summary || ""}: ${changed} configuration value(s) changed`);
      } catch (err) {
        state.setKconfigToolchain(this.profileKey, previous);
        toast.error(`Failed to rebuild the model: ${err.message}`);
      }
    };
    modal.footer.querySelector("#btn-tc-reset").onclick = () => apply({});
    modal.footer.querySelector("#btn-tc-apply").onclick = () => {
      const next = {};
      modal.body.querySelectorAll("[data-key]").forEach((el) => {
        const v = el.value.trim();
        if (!v) return;
        next[el.dataset.key] = v === "true" ? true : v === "false" ? false : v;
      });
      if (next.cc && next.cc !== eff.cc) {
        // Switching compiler: versions typed for the other compiler would not make sense
        for (const k of ["cc_version", "ld_version"]) if (next[k] !== undefined && next[k] === this.toolchain[k]) delete next[k];
      }
      const env = {};
      modal.body.querySelectorAll(".kconfig-tc-env").forEach((el) => {
        if (el.value !== "") env[el.dataset.env] = el.value;
      });
      const newVar = modal.body.querySelector("#kconfig-tc-newvar").value.trim();
      if (newVar) env[newVar] = modal.body.querySelector("#kconfig-tc-newval").value;
      if (Object.keys(env).length) next.env = env;
      const probes = {};
      modal.body.querySelectorAll(".kconfig-tc-probe").forEach((el) => {
        if (el.value !== "") probes[el.dataset.probe] = el.value;
      });
      if (Object.keys(probes).length) next.probes = probes;
      apply(next);
    };
  }
}
