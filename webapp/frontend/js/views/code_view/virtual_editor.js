/**
 * virtual_editor.js - Virtual Scrolling IDE Code Viewer.
 * High-performance virtualized line renderer with 100% copy fidelity,
 * AST overlay tokens, context menu dispatch, and minimap.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { contextMenu } from "../../components/context_menu.js";
import { AstOverlay } from "./ast_overlay.js";
import { FidelityClipboard } from "./fidelity_clipboard.js";
import { BlameView } from "./blame_view.js";
import { copyToClipboard } from "../../utils/clipboard.js";
import { showFileHistoryModal } from "../../components/file_history_modal.js";
import { DocPage } from "../docs/doc_page.js";
import { showPatchModal } from "../../components/patch_modal.js";
import { isDocumentationPath } from "../docs/rst_renderer.js";
import { DirectiveEvaluator, PREPROCESSED_RE, archMacros } from "../kconfig/directive_evaluator.js";
import { configLookup, evalBuild, explainBuild } from "../kconfig/build_condition.js";
import { escapeHtml } from "../../components/modal.js";

// Files the build banner reports on (sources Kbuild compiles and the headers/DTs they use)
const BUILD_TRACKED_RE = /\.(c|h|S|rs|lds|dts|dtsi|inc)$/i;

const ROW_HEIGHT = 20; // 20px per line
const BUFFER_LINES = 15; // lines buffered above and below viewport

// Compiled-out lines shown undimmed: one choice shared by every code tab (not persisted)
let dimSuspendedAll = false;

export class VirtualEditor {
  constructor() {
    this.rawLines = [];
    this.astOverlay = new AstOverlay();
    this.blameView = new BlameView();
    this.showBlame = false;
    this.currentFilePath = "";
    this.currentVersion = "";
    this.currentFtype = null;
    this.targetCursorLine = 1;
    // Lines changed by a commit (opened from the commit diff): Set of line numbers + ordered blocks
    this.diffMarks = null;
    this.diffMarkedLines = new Set();

    this.viewportEl = null;
    this.spacerEl = null;
    this.linesContainer = null;
    this.minimapCanvas = null;

    this.directiveEvaluator = new DirectiveEvaluator();
    this.dimmedLines = new Set();
    // Build condition of the open file: { key: "version|arch|path", cond } (re-evaluated on config edits)
    this.buildCond = null;
    this.buildToken = 0;
    // Tab this view renders (one view per code tab, built by TabsComponent)
    this.tabId = null;
    // KConfig engine this tab follows ("" = none); picked in the toolbar, kept on the tab
    this.engineTabId = "";

    // Every change to the KConfig engine this tab follows, or to its configuration, changes which
    // lines are compiled out and whether the file is compiled at all, so this tab recomputes both.
    // A tab that is not on screen does nothing here: showing it re-renders it, which recomputes
    // them from scratch.
    const refreshDimming = () => {
      if (!this.isLive() || !this.rawLines.length) return;
      this.computeDimming();
      this.updateVisibleLines();
      this.refreshBuildBanner();
    };
    this.unsubscribers = [
      state.subscribe("kconfig:engine:changed", () => {
        if (!this.isLive()) return;
        // An engine this tab followed may be gone, and a new one may now match its revision
        this.resolveEngineSelection();
        this.updateEngineButton();
        refreshDimming();
      }),
      state.subscribe("kconfig:assignments:updated", ({ tabId } = {}) => {
        if (tabId && tabId !== this.engineTabId) return;
        this.updateEngineButton();
        refreshDimming();
      }),
      state.subscribe("code:dim-suspended", () => {
        if (!this.isLive()) return;
        const chip = this.containerEl?.querySelector("#kconfig-dim-chip");
        if (chip && this.dimmingAvailable()) this.updateDimChip(chip);
        this.updateVisibleLines();
      }),
    ];
  }

  /**
   * Which engine this tab follows. A tab the user never set one on follows the first engine of its
   * own revision, and none when no engine runs on that revision (it is then set by hand in the
   * toolbar). A tab whose engine was picked by hand keeps that choice until that engine stops
   * running, at which point it falls back to the same default.
   */
  resolveEngineSelection() {
    const tab = state.findTab(this.tabId) || this.tabData;
    const picked = tab ? tab.kconfigEngineTabId : undefined;
    if (tab?.kconfigEngineUserPicked && (picked === "" || state.hasEngine(picked))) {
      this.engineTabId = picked;
      return this.engineTabId;
    }
    this.engineTabId = state.defaultEngineTabIdFor(tab);
    if (tab) {
      tab.kconfigEngineTabId = this.engineTabId;
      delete tab.kconfigEngineUserPicked; // the engine it was set to is gone
      state.saveState();
    }
    return this.engineTabId;
  }

  /** Follow another engine (or none) for this file only. */
  setEngineSelection(engineTabId) {
    const next = state.hasEngine(engineTabId) ? engineTabId : "";
    this.engineTabId = next;
    const tab = state.findTab(this.tabId) || this.tabData;
    if (tab) {
      tab.kconfigEngineTabId = next;
      tab.kconfigEngineUserPicked = true; // a choice by hand outlives new engines appearing
      state.saveState();
    }
    this.buildCond = null; // its ARCH decides the Kbuild condition
    this.updateEngineButton();
    if (this.rawLines.length) {
      this.computeDimming();
      this.updateVisibleLines();
      this.refreshBuildBanner();
    }
    const engine = state.kconfigEngineList().find((e) => e.tabId === next);
    toast.info(engine
      ? `This file follows ${engine.title} (ARCH=${engine.arch} @ ${engine.version})`
      : "This file follows no KConfig engine: no #ifdef dimming or build banner");
  }

  /** Label and tooltip of the toolbar's engine selector. */
  updateEngineButton() {
    const btn = this.containerEl?.querySelector("#btn-kconfig-engine");
    if (!btn) return;
    const engines = state.kconfigEngineList();
    const engine = engines.find((e) => e.tabId === this.engineTabId);
    btn.classList.toggle("active", Boolean(engine));
    btn.textContent = engine ? `⚙ ${engine.arch}${engine.configName ? ` · ${engine.configName}` : ""} ▾` : "⚙ No engine ▾";
    btn.title = engine
      ? `This file follows ${engine.title} (ARCH=${engine.arch} @ ${engine.version}, ${engine.symbols} symbols${engine.resolved ? "" : " — not a resolved configuration yet"}).\nClick to follow another KConfig engine.`
      : `${engines.length ? `${engines.length} KConfig engine(s) running` : "No KConfig engine is running"}: this file is shown without #ifdef dimming or build banner.\nClick to pick one.`;
  }

  /** Popover listing the running KConfig engines (plus "No engine") for this file. */
  showEnginePicker(anchorEl) {
    const existing = document.getElementById("code-engine-picker");
    if (existing) {
      existing.remove();
      return;
    }
    const engines = state.kconfigEngineList();
    const pop = document.createElement("div");
    pop.id = "code-engine-picker";
    pop.className = "engine-picker";
    const rect = anchorEl.getBoundingClientRect();
    pop.style.top = `${rect.bottom + 6}px`;
    pop.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 328))}px`;
    const rows = engines.map((e) => `
      <div class="engine-picker-item ${e.tabId === this.engineTabId ? "current" : ""}" data-engine="${escapeHtml(e.tabId)}">
        <span class="engine-picker-title">${escapeHtml(e.title)}</span>
        <span class="engine-picker-meta">ARCH=${escapeHtml(e.arch)} · ${escapeHtml(e.version)}${e.version === this.currentVersion ? "" : " (other revision)"} · ${e.symbols} symbols${e.resolved ? "" : " · unresolved"}</span>
      </div>`).join("");
    pop.innerHTML = `
      <div class="engine-picker-header">KConfig engine for this file</div>
      <div class="engine-picker-list">
        <div class="engine-picker-item ${this.engineTabId ? "" : "current"}" data-engine="">
          <span class="engine-picker-title">No engine</span>
          <span class="engine-picker-meta">No #ifdef dimming, no build banner</span>
        </div>
        ${rows || `<div class="engine-picker-empty">No KConfig tab runs its engine. Open a KConfig tab and press <strong>⚡ Enable Engine</strong>.</div>`}
      </div>`;
    document.body.appendChild(pop);

    const close = () => {
      pop.remove();
      document.removeEventListener("mousedown", outside);
    };
    const outside = (e) => {
      if (!pop.contains(e.target) && !anchorEl.contains(e.target)) close();
    };
    setTimeout(() => document.addEventListener("mousedown", outside), 0);
    pop.querySelectorAll(".engine-picker-item").forEach((row) => {
      row.onclick = () => {
        close();
        this.setEngineSelection(row.dataset.engine);
      };
    });
  }

  /** Compiled-out lines shown undimmed; the choice is shared by every code tab. */
  get dimSuspended() {
    return dimSuspendedAll;
  }

  set dimSuspended(value) {
    dimSuspendedAll = Boolean(value);
  }

  /** Whether this view's lines are the ones on screen (its tab may have been re-rendered or closed). */
  isLive() {
    return Boolean(this.linesContainer && this.linesContainer.isConnected);
  }

  /** Called when this tab is closed (TabsComponent.pruneTabViews). */
  dispose() {
    (this.unsubscribers || []).forEach((off) => off());
    this.unsubscribers = [];
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    clearTimeout(this._saveScrollTimer);
    this._saveScrollTimer = null;
    this.containerEl = null;
    this.linesContainer = null;
    this.rawLines = [];
  }

  /** #ifdef dimming is only active while the Kconfig engine is enabled with a resolved configuration. */
  dimmingAvailable() {
    return state.kconfigConfigActive(this.engineTabId) && PREPROCESSED_RE.test(this.currentFilePath || "");
  }

  computeDimming() {
    const chip = this.containerEl?.querySelector("#kconfig-dim-chip");
    if (!this.dimmingAvailable()) {
      this.dimmedLines = new Set();
      if (chip) chip.style.display = "none";
      return;
    }
    const ctx = state.definitionContext(this.engineTabId);
    this.directiveEvaluator.setAssignments(ctx.assignments);
    this.directiveEvaluator.setMacros(archMacros(ctx.arch, ctx.assignments));
    this.dimmedLines = this.directiveEvaluator.evaluateDimmedLines(this.rawLines);
    if (chip) this.updateDimChip(chip);
  }

  updateDimChip(chip) {
    const count = `${this.dimmedLines.size} line${this.dimmedLines.size === 1 ? "" : "s"} compiled out`;
    chip.style.display = "inline-flex";
    chip.classList.toggle("suspended", this.dimSuspended);
    chip.setAttribute("aria-pressed", String(this.dimSuspended));
    chip.textContent = this.dimSuspended ? `⚙ #ifdef: ${count} (not dimmed)` : `⚙ #ifdef: ${count}`;
    chip.title = this.dimSuspended
      ? "Compiled-out lines are shown normally. Click to dim them again."
      : "Lines inside #if/#ifdef branches disabled by the KConfig engine's current configuration.\nClick to stop dimming them for now (toggle the engine in the KConfig view to turn dimming off).";
  }

  /** Temporarily show compiled-out lines undimmed (or dim them again); not persisted. */
  toggleDimSuspended() {
    this.dimSuspended = !this.dimSuspended;
    const chip = this.containerEl?.querySelector("#kconfig-dim-chip");
    if (chip && this.dimmingAvailable()) this.updateDimChip(chip);
    this.updateVisibleLines();
    state.emit("code:dim-suspended", this.dimSuspended); // the choice applies to every code tab
  }

  /**
   * Banner at the top of the file when the KConfig engine's configuration does not compile it (Kbuild
   * condition "n", e.g. obj-$(CONFIG_FOO) += foo.o with FOO=n, or another architecture's tree), and a
   * note when it is only built as a module.
   */
  async refreshBuildBanner() {
    const banner = this.containerEl?.querySelector("#code-build-banner");
    if (!banner) return;
    const path = this.currentFilePath || "";
    if (!state.kconfigConfigActive(this.engineTabId) || this.isDoc || !BUILD_TRACKED_RE.test(path)) {
      banner.hidden = true;
      return;
    }
    const ctx = state.definitionContext(this.engineTabId);
    const key = `${this.currentVersion}|${ctx.arch}|${path}`;
    if (this.buildCond?.key !== key) {
      const token = ++this.buildToken;
      let cond = null;
      try {
        cond = (await api.getBuildConditions(this.currentVersion, [path], ctx.arch))?.files?.[path] || null;
      } catch (err) {
        console.warn("Build condition unavailable:", err);
      }
      if (token !== this.buildToken || path !== this.currentFilePath) return;
      this.buildCond = { key, cond };
    }
    const cond = this.buildCond.cond;
    const lookup = configLookup(ctx);
    const result = cond ? evalBuild(cond.expr, lookup) : "?";
    const arch = escapeHtml(ctx.arch);
    const rule = cond?.rules?.[0];
    const ruleHtml = rule
      ? `<button class="code-build-rule" data-mk="${escapeHtml(rule.makefile)}" data-line="${rule.line}" title="Open the Kbuild rule">${escapeHtml(rule.makefile)}:${rule.line} <code>${escapeHtml(rule.text)}</code></button>`
      : "";
    if (result === "n") {
      let why = escapeHtml(cond.reason || "");
      if (cond.status === "rule") {
        const { symbols, more } = explainBuild(cond.expr, lookup);
        why = `Needs ${symbols.map((s) => `<code>${escapeHtml(s.replace(/=.*$/, ""))}</code>`).join(" + ") || "its Kbuild condition"}`
          + ` (currently ${symbols.map((s) => escapeHtml(s.replace(/^CONFIG_/, ""))).join(", ")})`
          + (more ? `, or one of ${more} other rule${more === 1 ? "" : "s"}` : "");
      }
      banner.className = "code-build-banner not-built";
      banner.innerHTML = `<span class="code-build-icon">⛔</span><span><strong>Not compiled</strong> for ARCH=${escapeHtml(arch)} with the current KConfig configuration. ${why}</span>${ruleHtml}`;
    } else if (result === "m") {
      banner.className = "code-build-banner module";
      banner.innerHTML = `<span class="code-build-icon">📦</span><span>Built as a <strong>module</strong> for ARCH=${escapeHtml(arch)} with the current KConfig configuration.</span>${ruleHtml}`;
    } else {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    banner.querySelector(".code-build-rule")?.addEventListener("click", (e) => {
      const btn = e.currentTarget;
      state.openTab({ type: "code", title: btn.dataset.mk.split("/").pop(), path: btn.dataset.mk, cursorLine: Number(btn.dataset.line) || 1, version: this.currentVersion });
    });
  }

  async render(containerEl, tabData) {
    this.tabData = tabData;
    this.tabId = tabData.id || null;
    this.containerEl = containerEl;
    // Which KConfig engine this file follows (the first one of this revision by default, else none)
    this.resolveEngineSelection();
    this.currentFilePath = tabData.path;
    this.currentVersion = tabData.version || state.currentVersion;
    this.targetCursorLine = tabData.cursorLine || 1;
    this.diffMarks = tabData.diffMarks?.ranges?.length ? tabData.diffMarks : null;
    this.diffMarkedLines = new Set();
    for (const [a, b] of this.diffMarks?.ranges || []) {
      for (let n = a; n <= b; n++) this.diffMarkedLines.add(n);
    }
    // Documentation renders kernel.org-style by default unless a specific source line was requested
    this.isDoc = isDocumentationPath(this.currentFilePath);
    if (this.isDoc && !tabData.docMode) {
      tabData.docMode = this.targetCursorLine > 1 ? "source" : "rendered";
    }
    this.docMode = this.isDoc ? tabData.docMode : "source";

    // Every segment of the path opens the repository explorer of this revision on that directory
    const pathParts = (this.currentFilePath || "").split("/").filter(Boolean);
    let breadcrumbsHtml = `<span class="breadcrumb-item breadcrumb-dir" data-path="" title="Browse ${escapeHtml(this.currentVersion)} in a repository explorer">${escapeHtml(this.currentVersion)}</span>`;
    let accumPath = "";
    pathParts.forEach((part, idx) => {
      accumPath += (accumPath ? "/" : "") + part;
      const isLast = idx === pathParts.length - 1;
      breadcrumbsHtml += `<span class="breadcrumb-sep">&gt;</span>`;
      if (isLast) {
        breadcrumbsHtml += `<span class="breadcrumb-item active" style="font-weight:500;">${escapeHtml(part)}</span>`;
      } else {
        breadcrumbsHtml += `<span class="breadcrumb-item breadcrumb-dir" data-path="${escapeHtml(accumPath)}" title="Browse ${escapeHtml(accumPath)} in a repository explorer" style="cursor:pointer;color:var(--accent-blue);">${escapeHtml(part)}</span>`;
      }
    });

    containerEl.innerHTML = `
      <div class="code-viewer-container">
        <div class="code-toolbar">
          <div class="code-breadcrumbs" id="code-breadcrumbs">
            ${breadcrumbsHtml}
          </div>
          <div class="code-actions">
            ${this.isDoc ? `<button class="code-btn ${this.docMode === "rendered" ? "active" : ""}" id="btn-doc-toggle" title="Toggle rendered documentation / raw source">${this.docMode === "rendered" ? "📖 Rendered" : "&lt;/&gt; Source"}</button>` : ""}
            <button type="button" id="kconfig-dim-chip" class="code-dim-chip" style="display:none;" aria-pressed="false"></button>
            ${this.diffMarks ? `<span class="code-diffmark-chip ${this.diffMarks.kind === "del" ? "del" : "add"}" title="Lines ${this.diffMarks.kind === "del" ? "removed" : "added or changed"} by commit ${this.diffMarks.commit || ""}">
              ± ${(this.diffMarks.commit || "").slice(0, 8)}: ${this.diffMarkedLines.size} line${this.diffMarkedLines.size === 1 ? "" : "s"} ${this.diffMarks.kind === "del" ? "removed" : "changed"}
              <button data-diffnav="-1" title="Previous change">↑</button><button data-diffnav="1" title="Next change">↓</button><button data-diffnav="0" title="Clear change markers">✕</button>
            </span>` : ""}
            <span id="file-last-changed" class="code-last-changed" style="display:none;font-size:11px;color:var(--text-muted);margin-right:8px;padding:3px 8px;background:var(--bg-tertiary);border-radius:4px;border:1px solid var(--border-color);align-items:center;gap:4px;cursor:pointer;" title="Click to view full commit history"></span>
            <button class="code-btn" id="btn-kconfig-engine" title="KConfig engine this file follows"></button>
            <button class="code-btn" id="btn-file-history" title="View commit history for this file">📜 History</button>
            <button class="code-btn" id="btn-toggle-blame" title="Annotate every line with the commit that last changed it">Blame</button>
            <button class="code-btn" id="btn-copy-raw">Copy File</button>
            <button class="code-btn" id="btn-create-patch" title="Edit this file and generate a git format-patch">✎ Patch</button>
          </div>
        </div>
        <div class="code-build-banner" id="code-build-banner" hidden></div>
        <div class="editor-body">
          <div class="virtual-scroll-viewport" id="editor-viewport">
            <div class="virtual-scroll-spacer" id="editor-spacer"></div>
            <div class="virtual-lines-container" id="editor-lines"></div>
          </div>
          <div class="editor-minimap">
            <canvas class="minimap-canvas" id="minimap-canvas"></canvas>
            <div class="minimap-slider" id="minimap-slider"></div>
          </div>
        </div>
        <div class="kdoc-viewport" id="doc-viewport" hidden></div>
      </div>
    `;

    // Clicking the path opens a full-tab repository explorer on that directory, same revision
    containerEl.querySelectorAll(".breadcrumb-dir").forEach((el) => {
      const open = (forceNew) => state.openTab({
        type: "explorer",
        title: el.dataset.path ? el.dataset.path.split("/").pop() : `Explorer ${this.currentVersion}`,
        path: el.dataset.path,
        version: this.currentVersion,
        forceNew,
      });
      el.onclick = () => open(false);
      el.addEventListener("auxclick", (e) => {
        if (e.button !== 1) return;
        e.preventDefault();
        e.stopPropagation();
        open(true);
      });
    });

    this.viewportEl = containerEl.querySelector("#editor-viewport");
    this.spacerEl = containerEl.querySelector("#editor-spacer");
    this.linesContainer = containerEl.querySelector("#editor-lines");
    this.minimapCanvas = containerEl.querySelector("#minimap-canvas");

    // Attach 100% copy fidelity handler
    const clipboard = new FidelityClipboard(() => this.rawLines);
    clipboard.attach(this.linesContainer);

    // Toolbar actions
    containerEl.querySelector("#kconfig-dim-chip").onclick = () => this.toggleDimSuspended();

    const engineBtn = containerEl.querySelector("#btn-kconfig-engine");
    this.updateEngineButton();
    engineBtn.onclick = (e) => {
      e.stopPropagation();
      this.showEnginePicker(engineBtn);
    };

    const docToggle = containerEl.querySelector("#btn-doc-toggle");
    if (docToggle) {
      docToggle.onclick = () => this.setDocMode(this.docMode === "rendered" ? "source" : "rendered");
    }

    containerEl.querySelectorAll("[data-diffnav]").forEach((btn) => {
      btn.onclick = () => {
        const dir = parseInt(btn.dataset.diffnav, 10);
        if (dir === 0) {
          delete this.tabData.diffMarks;
          state.saveState();
          containerEl.querySelector(".code-diffmark-chip")?.remove();
          this.diffMarks = null;
          this.diffMarkedLines = new Set();
          this.updateVisibleLines();
          this.renderMinimap();
        } else {
          this.jumpToDiffBlock(dir);
        }
      };
    });

    const blameBtn = containerEl.querySelector("#btn-toggle-blame");
    blameBtn.onclick = async () => {
      if (this.docMode === "rendered") this.setDocMode("source");
      this.showBlame = !this.showBlame;
      blameBtn.classList.toggle("active", this.showBlame);
      if (this.showBlame) {
        await this.blameView.loadBlame(this.currentVersion, this.currentFilePath);
      }
      this.updateVisibleLines();
    };

    const copyBtn = containerEl.querySelector("#btn-copy-raw");
    copyBtn.onclick = async () => {
      const ok = await copyToClipboard(this.rawLines.join("\n"));
      if (ok) {
        toast.success("Copied entire file to clipboard (100% fidelity)");
      } else {
        toast.error("Failed to copy file to clipboard");
      }
    };

    containerEl.querySelector("#btn-create-patch").onclick = () => {
      showPatchModal(this.currentVersion, this.currentFilePath, this.rawLines.length ? this.rawLines.join("\n") : null);
    };

    const historyBtn = containerEl.querySelector("#btn-file-history");
    if (historyBtn) {
      historyBtn.onclick = () => {
        showFileHistoryModal(this.currentVersion, this.currentFilePath);
      };
    }
    const lastChangedEl = containerEl.querySelector("#file-last-changed");
    if (lastChangedEl) {
      lastChangedEl.onclick = () => {
        showFileHistoryModal(this.currentVersion, this.currentFilePath);
      };
    }

    this.blameView.attachListeners(this.linesContainer);

    // Token click listener for context menu
    this.linesContainer.addEventListener("click", (e) => {
      const tokenSpan = e.target.closest(".ast-token");
      if (tokenSpan) {
        // Prevent clicks on comments or structural directives
        if (tokenSpan.classList.contains("tok-comment") || tokenSpan.dataset.typeId === "2") {
          return;
        }

        e.stopPropagation();
        const row = tokenSpan.closest(".code-row");
        const lineNo = row ? parseInt(row.dataset.lineNo, 10) : 1;
        const astId = parseInt(tokenSpan.dataset.astId || "0", 10);
        const typeId = parseInt(tokenSpan.dataset.typeId || "0", 10);
        const name = tokenSpan.dataset.name || tokenSpan.textContent;
        const header = tokenSpan.dataset.header || "";
        const isInclude = tokenSpan.classList.contains("ast-include") || typeId === 78 || name.includes(".h");

        contextMenu.show(e.clientX, e.clientY, {
          name,
          header,
          isInclude,
          ast_id: astId,
          type_id: typeId,
          version: this.currentVersion,
          filePath: this.currentFilePath,
          ftype: this.currentFtype,
          line: lineNo
        });
      }
    });

    // Middle-click token listener to execute default action directly in a new tab
    this.linesContainer.addEventListener("auxclick", (e) => {
      if (e.button !== 1) return;
      const tokenSpan = e.target.closest(".ast-token");
      if (tokenSpan) {
        // Prevent clicks on comments or structural directives
        if (tokenSpan.classList.contains("tok-comment") || tokenSpan.dataset.typeId === "2") {
          return;
        }

        e.preventDefault();
        e.stopPropagation();
        const row = tokenSpan.closest(".code-row");
        const lineNo = row ? parseInt(row.dataset.lineNo, 10) : 1;
        const astId = parseInt(tokenSpan.dataset.astId || "0", 10);
        const typeId = parseInt(tokenSpan.dataset.typeId || "0", 10);
        const name = tokenSpan.dataset.name || tokenSpan.textContent;
        const header = tokenSpan.dataset.header || "";
        const isInclude = tokenSpan.classList.contains("ast-include") || typeId === 78 || name.includes(".h");

        contextMenu.executeDefaultAction({
          name,
          header,
          isInclude,
          ast_id: astId,
          type_id: typeId,
          version: this.currentVersion,
          filePath: this.currentFilePath,
          ftype: this.currentFtype,
          line: lineNo
        }, { forceNew: true });
      }
    });

    // Right-click context menu listener for editor lines, tokens, and selections
    this.linesContainer.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();

      const row = e.target.closest(".code-row");
      const lineNo = row ? parseInt(row.dataset.lineNo, 10) : 1;

      let tokenData = null;
      const tokenSpan = e.target.closest(".ast-token");
      if (tokenSpan && !tokenSpan.classList.contains("tok-comment") && tokenSpan.dataset.typeId !== "2") {
        const astId = parseInt(tokenSpan.dataset.astId || "0", 10);
        const typeId = parseInt(tokenSpan.dataset.typeId || "0", 10);
        const name = tokenSpan.dataset.name || tokenSpan.textContent;
        const header = tokenSpan.dataset.header || "";
        const isInclude = tokenSpan.classList.contains("ast-include") || typeId === 78 || name.includes(".h");
        tokenData = {
          name,
          header,
          isInclude,
          ast_id: astId,
          type_id: typeId,
          version: this.currentVersion,
          filePath: this.currentFilePath,
          ftype: this.currentFtype,
          line: lineNo
        };
      }

      const selection = window.getSelection() ? window.getSelection().toString().trim() : "";
      const blame = this.blameView?.blameByLine?.get(lineNo) || null;

      contextMenu.showEditorMenu(e.clientX, e.clientY, {
        token: tokenData,
        filePath: this.currentFilePath,
        ftype: this.currentFtype,
        line: lineNo,
        selection,
        version: this.currentVersion,
        blame
      });
    });

    // Viewport scrolling: the position is kept on the tab, so coming back lands where it was left
    this.viewportEl.addEventListener("scroll", () => {
      this.updateVisibleLines();
      this.updateMinimapSlider();
      this.rememberScrollPosition();
    });

    // The minimap is to scale with the document, so clicking or dragging it scrolls there
    this.attachMinimapScrubbing();

    // A split, a resized window or a toggled sidebar changes the minimap's scale (one observer per
    // render: the previous one watched the element this render just replaced)
    this.resizeObserver?.disconnect();
    if (typeof ResizeObserver === "function") {
      this.resizeObserver = new ResizeObserver(() => {
        if (!this.isLive() || !this.rawLines.length) return;
        this.renderMinimap();
        this.updateMinimapSlider();
        this.updateVisibleLines();
      });
      this.resizeObserver.observe(containerEl.querySelector(".editor-body"));
    }

    // Load file content from API
    await this.loadFileContent();
  }

  async loadFileContent() {
    const lastChangedEl = this.viewportEl?.closest(".code-viewer-container")?.querySelector("#file-last-changed");
    if (lastChangedEl) {
      lastChangedEl.style.display = "none";
    }
    this.linesContainer.innerHTML = `<div style="padding:20px;color:var(--text-muted);">Loading ${escapeHtml(this.currentFilePath)}...</div>`;
    try {
      const res = await api.getFileContent(this.currentVersion, this.currentFilePath);
      if (!res) {
        throw new Error("No response received from server for file content");
      }
      if (res.is_directory || res.type === "dir") {
        state.emit("explorer:reveal", this.currentFilePath);
        const entries = res.entries || res.tree || [];
        const dirListHtml = entries.map((item) => {
          const isItemDir = item.type === "dir";
          const icon = isItemDir ? "📁" : "📄";
          const statBadge = item.s_stat_label ? `<span class="badge" style="font-size:10px;padding:1px 6px;border-radius:3px;background:var(--bg-tertiary);">${item.s_stat_label}</span>` : "";
          return `
            <div class="dir-entry-row" data-path="${item.path}" data-type="${item.type}" style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid var(--border-color);cursor:pointer;transition:background 0.15s;" onmouseover="this.style.background='var(--bg-hover)'" onmouseout="this.style.background='transparent'">
              <div style="display:flex;align-items:center;gap:10px;">
                <span style="font-size:16px;">${icon}</span>
                <span style="font-weight:${isItemDir ? '600' : '400'};color:${isItemDir ? 'var(--accent-blue)' : 'var(--text-primary)'};">${item.name}</span>
              </div>
              <div style="display:flex;align-items:center;gap:12px;font-size:11px;color:var(--text-muted);">
                ${statBadge}
                <span>${item.type === "dir" ? "Directory" : "File"}</span>
              </div>
            </div>
          `;
        }).join("");

        this.linesContainer.innerHTML = `
          <div class="directory-browser-container" style="max-width:860px;margin:0 auto;padding:24px 16px;">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;border-bottom:1px solid var(--border-color);padding-bottom:12px;">
              <div>
                <h3 style="margin:0;font-size:18px;display:flex;align-items:center;gap:8px;">📁 ${this.currentFilePath || "Kernel Root"}</h3>
                <span style="font-size:12px;color:var(--text-muted);">${entries.length} items in this directory</span>
              </div>
            </div>
            <div class="dir-entries-card" style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:6px;overflow:hidden;">
              ${entries.length > 0 ? dirListHtml : '<div style="padding:24px;text-align:center;color:var(--text-muted);">Directory is empty.</div>'}
            </div>
          </div>
        `;

        this.linesContainer.querySelectorAll(".dir-entry-row").forEach((row) => {
          row.onclick = () => {
            const path = row.dataset.path;
            const name = path.split("/").pop();
            state.openTab({
              type: "code",
              title: name,
              path: path,
              version: this.currentVersion
            });
          };
        });
        return;
      }

      const content = res.content || "";
      this.rawLines = content.split(/\r?\n/);
      this.currentFtype = res.ftype;
      this.astOverlay.setFileInfo(res.ftype, this.currentFilePath);

      // Update last changed metadata chip in toolbar if available
      if (res.file_info && res.file_info.last_changed) {
        const lc = res.file_info.last_changed;
        const lastChangedEl = this.viewportEl?.closest(".code-viewer-container")?.querySelector("#file-last-changed");
        if (lastChangedEl) {
          const dateStr = lc.author_date_iso ? lc.author_date_iso.split(" ")[0] : "";
          const authorStr = lc.author_name || "";
          lastChangedEl.style.display = "inline-flex";
          lastChangedEl.innerHTML = `🕒 Last Changed: <strong style="color:var(--text-primary);margin-left:3px;">${escapeHtml(dateStr)}</strong><span style="opacity:0.75;margin-left:4px;">by ${escapeHtml(authorStr)}</span>`;
          lastChangedEl.title = `Last changed: ${lc.author_date_iso} by ${authorStr}\nCommit: ${lc.commit_hash_short}\nSubject: ${lc.subject}\nClick to view full history`;
        }
      }

      // Load server tokens into AST overlay
      if (Array.isArray(res.tokens)) {
        this.astOverlay.loadServerTokens(res.tokens);
      }

      this.computeDimming();
      this.refreshBuildBanner();

      if (this.docMode === "rendered") {
        this.renderDocPage(content);
      }

      // Update spacer height for virtual scrolling
      const totalHeight = this.rawLines.length * ROW_HEIGHT;
      this.spacerEl.style.height = `${totalHeight}px`;

      // Come back where the tab was left, unless a specific line was requested (navigation drops
      // the saved position, see state.openTab)
      const savedTop = Number(this.tabData?.scrollTop);
      if (Number.isFinite(savedTop) && savedTop > 0) {
        this.viewportEl.scrollTop = Math.min(savedTop, Math.max(0, totalHeight - this.viewportEl.clientHeight));
      } else if (this.targetCursorLine > 1) {
        this.viewportEl.scrollTop = Math.max(0, (this.targetCursorLine - 5) * ROW_HEIGHT);
      }

      this.updateVisibleLines();
      this.renderMinimap();
      this.updateMinimapSlider();
    } catch (err) {
      this.linesContainer.innerHTML = `
        <div style="padding:20px;color:var(--accent-red);">
          Failed to load file: ${err.message}
        </div>
      `;
    }
  }

  /** Switch a documentation tab between the rendered page and the raw source editor. */
  setDocMode(mode) {
    if (!this.isDoc || mode === this.docMode) return;
    this.docMode = mode;
    this.tabData.docMode = mode;
    state.saveState();
    const btn = this.containerEl.querySelector("#btn-doc-toggle");
    if (btn) {
      btn.classList.toggle("active", mode === "rendered");
      btn.innerHTML = mode === "rendered" ? "📖 Rendered" : "&lt;/&gt; Source";
    }
    if (mode === "rendered") {
      this.renderDocPage(this.rawLines.join("\n"));
    } else {
      this.containerEl.querySelector(".editor-body").hidden = false;
      this.containerEl.querySelector("#doc-viewport").hidden = true;
      this.updateVisibleLines();
      this.renderMinimap();
    }
  }

  renderDocPage(content) {
    const docViewport = this.containerEl.querySelector("#doc-viewport");
    const editorBody = this.containerEl.querySelector(".editor-body");
    if (!docViewport || !editorBody) return;
    editorBody.hidden = true;
    docViewport.hidden = false;
    const history = this.tabData.docHistory || [];
    const page = new DocPage({
      path: this.currentFilePath,
      version: this.currentVersion,
      anchor: this.tabData.docAnchor,
      canGoBack: history.length > 0,
      onShowSource: () => this.setDocMode("source"),
      onNavigate: (path) => this.navigateDoc(path, true),
      onBack: () => {
        const prev = (this.tabData.docHistory || []).pop();
        if (prev) this.navigateDoc(prev, false);
      },
    });
    page.mount(docViewport, content);
  }

  /** Follow a documentation link inside the same tab, keeping a back-history stack. */
  navigateDoc(path, pushHistory) {
    const [docPath, anchor] = path.split("#");
    if (pushHistory) {
      this.tabData.docHistory = [...(this.tabData.docHistory || []), this.currentFilePath].slice(-50);
    }
    this.tabData.path = docPath;
    this.tabData.title = docPath.split("/").pop();
    this.tabData.cursorLine = 1;
    this.tabData.docAnchor = anchor || "";
    this.tabData.docMode = isDocumentationPath(docPath) ? "rendered" : "source";
    state.saveState();
    state.emit("tabs:changed", { paneId: state.activePaneId, activeTabId: this.tabData.id });
  }

  /** Scroll to the previous/next block of commit-changed lines relative to the current cursor line. */
  jumpToDiffBlock(dir) {
    const blocks = this.diffMarks?.ranges || [];
    if (!blocks.length || !this.viewportEl) return;
    const cur = this.targetCursorLine;
    let target = dir > 0 ? blocks.find(([a]) => a > cur) : [...blocks].reverse().find(([, b]) => b < cur);
    if (!target) target = dir > 0 ? blocks[0] : blocks[blocks.length - 1];
    this.targetCursorLine = target[0];
    this.tabData.cursorLine = target[0];
    this.viewportEl.scrollTop = Math.max(0, (target[0] - 5) * ROW_HEIGHT);
    this.updateVisibleLines();
    this.rememberScrollPosition();
  }

  updateVisibleLines() {
    if (!this.viewportEl || this.rawLines.length === 0) return;

    const scrollTop = this.viewportEl.scrollTop;
    const viewportHeight = this.viewportEl.clientHeight || 600;

    const startIdx = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - BUFFER_LINES);
    const endIdx = Math.min(this.rawLines.length - 1, Math.ceil((scrollTop + viewportHeight) / ROW_HEIGHT) + BUFFER_LINES);

    this.linesContainer.style.transform = `translateY(${startIdx * ROW_HEIGHT}px)`;

    let html = "";
    for (let i = startIdx; i <= endIdx; i++) {
      const lineNo = i + 1;
      const rawText = this.rawLines[i];
      const isHighlighted = lineNo === this.targetCursorLine;
      const isDimmed = !this.dimSuspended && this.dimmedLines.has(lineNo);
      const diffCls = this.diffMarkedLines.has(lineNo) ? (this.diffMarks.kind === "del" ? " diff-mark-del" : " diff-mark-add") : "";

      const blameHtml = this.showBlame ? this.blameView.renderGutterCell(lineNo) : "";
      const textHtml = this.astOverlay.renderLineHtml(lineNo, rawText, isDimmed);

      html += `
        <div class="code-row ${isHighlighted ? "highlighted-line" : ""}${diffCls}" data-line-no="${lineNo}">
          ${blameHtml}
          <div class="gutter-line-no">${lineNo}</div>
          <div class="code-text">${textHtml}</div>
        </div>
      `;
    }

    this.linesContainer.innerHTML = html;
  }

  /** Remember where this tab is scrolled to, so selecting it again comes back to that position. */
  rememberScrollPosition() {
    if (!this.tabData || !this.viewportEl) return;
    const top = Math.round(this.viewportEl.scrollTop);
    if (this.tabData.scrollTop === top) return;
    this.tabData.scrollTop = top;
    // The position is written on every scroll, so persisting is throttled to one write per idle frame
    if (this._saveScrollTimer) return;
    this._saveScrollTimer = setTimeout(() => {
      this._saveScrollTimer = null;
      state.saveState();
    }, 400);
  }

  /** Document height the minimap maps, and the height it is drawn at (never taller than its track). */
  minimapMetrics() {
    const totalLines = this.rawLines.length;
    const trackH = (this.minimapCanvas?.parentElement?.clientHeight) || 0;
    if (!totalLines || !trackH) return null;
    // One pixel per line at most: the drawing is exactly as deep as the file is long
    const lineScale = Math.min(1, trackH / totalLines);
    return {
      totalLines,
      trackH,
      lineScale,
      mapH: Math.min(trackH, totalLines * lineScale),
      docH: totalLines * ROW_HEIGHT,
      viewportH: this.viewportEl?.clientHeight || 0,
    };
  }

  renderMinimap() {
    if (!this.minimapCanvas) return;
    const canvas = this.minimapCanvas;
    const m = this.minimapMetrics();
    const w = canvas.parentElement.clientWidth || 90;
    const h = canvas.parentElement.clientHeight || 600;

    canvas.width = w;
    canvas.height = h;

    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    if (!m) return;
    ctx.fillStyle = "rgba(139, 148, 158, 0.4)";

    for (let i = 0; i < m.totalLines; i++) {
      const len = Math.min(w - 10, (this.rawLines[i] || "").length * 0.8);
      if (len > 0) {
        ctx.fillRect(4, i * m.lineScale, len, Math.max(1, m.lineScale));
      }
    }

    if (this.diffMarks) {
      ctx.fillStyle = this.diffMarks.kind === "del" ? "rgba(248, 81, 73, 0.9)" : "rgba(63, 185, 80, 0.9)";
      for (const [a, b] of this.diffMarks.ranges) {
        ctx.fillRect(0, (a - 1) * m.lineScale, 4, Math.max(2, (b - a + 1) * m.lineScale));
      }
    }

    // Below the last line the minimap shows nothing, so the track is marked as not part of the file
    if (m.mapH < h) {
      ctx.fillStyle = "rgba(0, 0, 0, 0.18)";
      ctx.fillRect(0, m.mapH, w, h - m.mapH);
    }
  }

  /**
   * The slider covers the lines the viewport shows, on the same scale as the drawing: at the bottom
   * of the file its lower edge meets the last line instead of running past it.
   */
  updateMinimapSlider() {
    const slider = this.containerEl?.querySelector("#minimap-slider");
    const m = this.minimapMetrics();
    if (!slider || !m) return;

    const maxScroll = Math.max(0, m.docH - m.viewportH);
    const sliderH = Math.max(12, Math.min(m.mapH, (m.viewportH / m.docH) * m.mapH));
    const progress = maxScroll > 0 ? Math.min(1, this.viewportEl.scrollTop / maxScroll) : 0;

    slider.style.height = `${sliderH}px`;
    slider.style.top = `${progress * (m.mapH - sliderH)}px`;
  }

  /** Click or drag the minimap to scroll the file (the y position maps back to the document). */
  attachMinimapScrubbing() {
    const track = this.minimapCanvas?.parentElement;
    if (!track) return;
    const scrollToY = (clientY) => {
      const m = this.minimapMetrics();
      if (!m) return;
      const rect = track.getBoundingClientRect();
      const sliderH = Math.max(12, Math.min(m.mapH, (m.viewportH / m.docH) * m.mapH));
      const span = Math.max(1, m.mapH - sliderH);
      const y = Math.min(Math.max(clientY - rect.top - sliderH / 2, 0), span);
      this.viewportEl.scrollTop = (y / span) * Math.max(0, m.docH - m.viewportH);
    };
    const onMove = (e) => scrollToY(e.clientY);
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    track.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      scrollToY(e.clientY);
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }
}
