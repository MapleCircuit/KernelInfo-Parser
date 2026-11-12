/**
 * virtual_editor.js - Virtual Scrolling IDE Code Viewer.
 * High-performance virtualized line renderer with 100% copy fidelity,
 * AST overlay tokens, context menu dispatch, and minimap.
 */
import { shortcuts } from "../../utils/shortcuts.js";
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
import { isManPagePath } from "../man/man_model.js";
import { mountManDocument } from "../man/man_view.js";
import { isManPagesRepo } from "../../utils/projects.js";

/** A page file of the man-pages project renders as a man page (§5.38). */
const isManDocument = (path, repo) => isManPagesRepo(repo) && isManPagePath(path);
import { DirectiveEvaluator, PREPROCESSED_RE, archMacros } from "../kconfig/directive_evaluator.js";
import { configLookup, evalBuild, explainBuild } from "../kconfig/build_condition.js";
import { escapeHtml } from "../../components/modal.js";
import { scheduleScrollSave } from "../../components/scroll_memory.js";
import { FindBar, selectionWithin } from "./find_bar.js";
import { OutlinePanel } from "./outline_panel.js";
import { registerView, unregisterView } from "../../utils/active_views.js";
import { formatBlameOpts, parseBlameOpts } from "./blame_options.js";
import { centeredScrollTop, codeHash, externalLink, fenceLanguage, formatLineSpec, markdownSnippet, pinnedSha } from "./permalink.js";
import { isTextTarget } from "../../utils/shortcuts.js";
import { buildDiffRows, pairDiffRows, showDiffPicker, diffSideLabel } from "./file_diff.js";
import { patchedFileDiff } from "../mail/patch_try.js";
import { HoverCard } from "./hover_card.js";
import { localDB } from "../../db.js";
import { relocate } from "../bookmarks/bookmark_model.js";
import { referenceCount } from "../../components/file_references.js";

// Files the build banner reports on (sources Kbuild compiles and the headers/DTs they use)
const BUILD_TRACKED_RE = /\.(c|h|S|rs|lds|dts|dtsi|inc)$/i;

export function getCodeRowHeight() {
  const custom = parseInt(document.documentElement.style.getPropertyValue("--code-row-height"), 10);
  if (custom && Number.isFinite(custom) && custom >= 16) return custom;
  const fontSize = parseInt(document.documentElement.style.getPropertyValue("--code-font-size"), 10) || 12;
  return Math.max(16, Math.round(fontSize * 1.667));
}

const BUFFER_LINES = 15; // lines buffered above and below viewport

// Compiled-out lines shown undimmed: one choice shared by every code tab (not persisted)
let dimSuspendedAll = false;

export class VirtualEditor {
  constructor() {
    this.rawLines = [];
    this.astOverlay = new AstOverlay();
    this.blameView = new BlameView();
    // Find in file (§5.21): searches the whole buffer, not just the rendered rows
    this.findBar = new FindBar(this);
    // Outline, sticky scroll and the breadcrumb's current definition (G03, §5.21)
    this.outline = new OutlinePanel(this);
    this.showBlame = false;
    this.currentFilePath = "";
    this.currentVersion = "";
    this.currentFtype = null;
    this.targetCursorLine = 1;
    // Lines changed by a commit (opened from the commit diff): Set of line numbers + ordered blocks
    this.diffMarks = null;
    this.diffMarkedLines = new Set();
    // Full-file diff against another file/revision (toolbar "⇄ Diff"; null = the plain file)
    this.fileDiff = null;
    this.fileDiffToken = 0;

    state.subscribe("codeview:font-changed", () => {
      if (this.viewportEl && this.rawLines.length) {
        const rowH = getCodeRowHeight();
        if (this.spacerEl) this.spacerEl.style.height = `${this.rowCount() * rowH}px`;
        this.updateVisibleLines();
        this.renderMinimap();
      }
    });

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
    btn._kipNaturalWidth = 0;
    this.updateToolbarOverflow();
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

  /**
   * Adjusts code-toolbar items dynamically according to available width:
   * 1. Hides informational #file-last-changed when width < 640px or space is constrained.
   * 2. Moves action buttons into the "▾" overflow dropdown in right-to-left priority order.
   * 3. Pinned interactive chips (dimming, commit diffmarks, file diff) always remain on the bar.
   */
  updateToolbarOverflow() {
    const toolbarEl = this.containerEl?.querySelector(".code-toolbar");
    const actionsEl = this.containerEl?.querySelector(".code-actions");
    const breadcrumbsEl = this.containerEl?.querySelector(".code-breadcrumbs");
    const overflowBtn = this.containerEl?.querySelector("#btn-code-overflow");
    if (!toolbarEl || !actionsEl || !breadcrumbsEl || !overflowBtn) return;

    const toolbarWidth = toolbarEl.clientWidth;
    if (toolbarWidth <= 0) return;

    // Auto-hide the informational "last-changed" chip on narrow viewports
    const lastChangedEl = actionsEl.querySelector("#file-last-changed");
    if (lastChangedEl) {
      if (this.hasLastChanged && toolbarWidth >= 640) {
        lastChangedEl.style.display = "inline-flex";
      } else {
        lastChangedEl.style.display = "none";
      }
    }

    // Action buttons in priority order: first item is first to hide/overflow (right-to-left)
    const priorityButtons = [
      { id: "btn-copy-link", label: "Copy Link to Lines…", icon: "🔗" },
      { id: "btn-outline", label: () => (this.outline.open ? "Hide Outline" : "Show Outline"), icon: "☰" },
      { id: "btn-file-refs", label: "Referenced by…", icon: "⇇" },
      { id: "btn-create-patch", label: "Create Patch", icon: "✎" },
      { id: "btn-copy-raw", label: "Copy File", icon: "📋" },
      { id: "btn-file-history", label: "File History", icon: "📜" },
      {
        id: "btn-toggle-blame",
        label: () => (this.showBlame ? "Hide Blame Annotations" : "Show Git Blame"),
        icon: () => (this.showBlame ? "✓" : "👁"),
      },
      { id: "btn-file-diff", label: "Diff File...", icon: "⇄" },
      {
        id: "btn-kconfig-engine",
        label: () => {
          const e = state.kconfigEngineList().find((x) => x.tabId === this.engineTabId);
          return e ? `KConfig Engine (${e.arch})` : "KConfig Engine...";
        },
        icon: "⚙",
      },
    ];

    // Measure natural widths when visible
    for (const item of priorityButtons) {
      const el = actionsEl.querySelector(`#${item.id}`);
      if (el) {
        if (el.style.display !== "none" && el.offsetWidth > 0) {
          el._kipNaturalWidth = el.offsetWidth;
        } else if (!el._kipNaturalWidth) {
          el._kipNaturalWidth = 72;
        }
      }
    }

    // Measure pinned chips
    const gap = 6;
    let pinnedWidth = 0;
    actionsEl.querySelectorAll(".code-dim-chip, .code-diffmark-chip, .code-filediff-chip, .code-last-changed, #btn-doc-toggle, .code-blame-opts").forEach((chip) => {
      if (chip.style.display !== "none" && !chip.hidden && chip.offsetWidth > 0) {
        pinnedWidth += chip.offsetWidth + gap;
      }
    });

    const toolbarPadding = 24; // 12px padding left + right
    const breadcrumbsScrollWidth = breadcrumbsEl.scrollWidth || 120;
    // Keep breadcrumbs comfortably readable (up to 35% of toolbar or full length, min 120px)
    const desiredBreadcrumbs = Math.max(120, Math.min(breadcrumbsScrollWidth, toolbarWidth * 0.35));
    const availableForActions = toolbarWidth - toolbarPadding - desiredBreadcrumbs - pinnedWidth - gap;

    let totalPriorityWidth = 0;
    for (const item of priorityButtons) {
      const el = actionsEl.querySelector(`#${item.id}`);
      if (el) {
        totalPriorityWidth += (el._kipNaturalWidth || 72) + gap;
      }
    }

    const overflowBtnWidth = 26 + gap;
    const hiddenItems = [];

    if (totalPriorityWidth > availableForActions) {
      let currentWidth = totalPriorityWidth + overflowBtnWidth;
      for (const item of priorityButtons) {
        const el = actionsEl.querySelector(`#${item.id}`);
        if (!el) continue;
        if (currentWidth > availableForActions) {
          el.style.display = "none";
          hiddenItems.push(item);
          currentWidth -= ((el._kipNaturalWidth || 72) + gap);
        } else {
          el.style.display = "";
        }
      }
      overflowBtn.style.display = "inline-flex";
    } else {
      for (const item of priorityButtons) {
        const el = actionsEl.querySelector(`#${item.id}`);
        if (el) el.style.display = "";
      }
      overflowBtn.style.display = "none";
    }

    this._hiddenActionItems = hiddenItems;
    if (hiddenItems.length === 0) {
      overflowBtn.style.display = "none";
    }
  }

  /**
   * Opens the dropdown context menu listing actions currently hidden by overflow.
   * @param {HTMLElement} anchorEl The "▾" overflow button
   */
  showOverflowMenu(anchorEl) {
    if (contextMenu.menuEl && contextMenu.menuEl.dataset.origin === "code-overflow") {
      contextMenu.hide();
      return;
    }
    const hidden = this._hiddenActionItems || [];
    if (!hidden.length) return;

    const items = hidden.map((item) => {
      const btn = this.containerEl?.querySelector(`#${item.id}`);
      const icon = typeof item.icon === "function" ? item.icon() : item.icon;
      const label = typeof item.label === "function" ? item.label() : item.label;
      return {
        label,
        icon,
        disabled: btn?.disabled,
        onClick: () => {
          btn?.click();
        },
      };
    });

    const rect = anchorEl.getBoundingClientRect();
    contextMenu.renderMenu(rect.right - 180, rect.bottom + 4, {
      header: { title: "More Actions" },
      items,
    });
    if (contextMenu.menuEl) {
      contextMenu.menuEl.dataset.origin = "code-overflow";
    }
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
  rowHeight() {
    return getCodeRowHeight();
  }

  /** Whether `line` is in the selected range (cursorLine..lineEnd). */
  isLineSelected(line) {
    const end = Math.max(this.targetCursorLine, this.targetLineEnd || 0);
    return line >= this.targetCursorLine && line <= end;
  }

  /**
   * Select lines a..b (in either order). `persist` records them on the tab (cursorLine, lineEnd) and so
   * in its URL (line=a-b), replacing the history entry (G06, §5.26).
   */
  selectLines(a, b, { persist = true } = {}) {
    const start = Math.max(1, Math.min(a, b));
    const end = Math.max(a, b);
    this.targetCursorLine = start;
    this.targetLineEnd = end;
    // Only the final selection is recorded (updateTabState writes the tab and announces the change, so the
    // URL follows); a drag in progress only repaints
    if (persist && this.tabId) {
      state.updateTabState(this.tabId, { cursorLine: start, lineEnd: end > start ? end : null }, { replace: true });
    }
    this.updateVisibleLines();
  }

  /** Lines the link actions address: the selection when `line` is in it, else `line` alone. */
  linkRange(line = null) {
    if (line && !this.isLineSelected(line)) return { start: line, end: line };
    return { start: this.targetCursorLine, end: Math.max(this.targetCursorLine, this.targetLineEnd || 0) };
  }

  /** SHA a permalink pins: the tab's revision when it is one, else the SHA of its catalog stamp. */
  async pinnedRevision() {
    let sha = pinnedSha(this.currentVersion, this.api.versionStamps[this.currentVersion]);
    if (!sha) {
      try {
        await this.api.getBranches();
        sha = pinnedSha(this.currentVersion, this.api.versionStamps[this.currentVersion]);
      } catch {
        sha = "";
      }
    }
    return sha;
  }

  absoluteLink(hash) {
    return `${window.location.origin}${window.location.pathname}${window.location.search}${hash}`;
  }

  /** Copy-link actions of the context menu and the toolbar's 🔗 button (G06, §5.21). */
  linkMenuItems(line = null) {
    const { start, end } = this.linkRange(line);
    const spec = formatLineSpec(start, end);
    const copy = async (text, what) => {
      if (await copyToClipboard(text)) toast.success(`Copied ${what}`);
      else toast.error(`Could not copy the ${what}`);
    };
    const hashFor = (rev) => codeHash({
      repo: this.api.repo, rev, path: this.currentFilePath, start, end,
      diffWith: this.fileDiff ? this.tabData?.diffWith : null, diffLayout: this.tabData?.diffLayout || "",
    });
    const items = [
      {
        label: `Copy Permalink (lines ${spec}, pinned to the commit)`, icon: "🔗",
        onClick: async () => {
          const sha = await this.pinnedRevision();
          if (!sha) toast.warning("The commit of this revision is unknown: the link follows the revision name");
          await copy(this.absoluteLink(hashFor(sha || this.currentVersion)), `permalink to ${this.currentFilePath}:${spec}`);
        },
      },
      {
        label: "Copy Link (follows the revision)", icon: "🔗",
        onClick: () => copy(this.absoluteLink(hashFor(this.currentVersion)), `link to ${this.currentFilePath}:${spec}`),
      },
      {
        label: "Copy as Markdown", icon: "Ⓜ",
        onClick: async () => {
          const sha = await this.pinnedRevision();
          const text = markdownSnippet({
            path: this.currentFilePath, start, end, revLabel: this.currentVersion, sha,
            link: this.absoluteLink(hashFor(sha || this.currentVersion)),
            lines: this.rawLines.slice(start - 1, end), lang: fenceLanguage(this.astOverlay?.grammar?.id),
          });
          await copy(text, `Markdown snippet of ${this.currentFilePath}:${spec}`);
        },
      },
    ];
    const gitUrl = this.repoGitUrl || "";
    if (gitUrl && externalLink(gitUrl, this.currentFilePath, "0".repeat(40), start, end)) {
      items.push({
        label: `Copy External Link (${new URL(gitUrl.replace(/^git@([^:]+):/, "https://$1/")).hostname})`, icon: "↗",
        onClick: async () => {
          const sha = await this.pinnedRevision();
          if (!sha) {
            toast.error("The commit of this revision is unknown");
            return;
          }
          // The web viewers need the full SHA; resolve the short one through the commit catalog when possible
          const full = sha.length === 40 ? sha : (await resolveFullSha(this.currentVersion, sha, this.api));
          await copy(externalLink(gitUrl, this.currentFilePath, full, start, end), "external link");
        },
      });
    }
    return items;
  }

  /**
   * Blame annotations on or off (H9, §5.21). The choice is kept on the tab (`blame`, `bl=1` in its
   * URL), with the options (`blameOpts`, `bo=mcw`), so a reopened tab or a link shows blame again.
   */
  async setBlame(on, { persist = true } = {}) {
    this.showBlame = Boolean(on);
    this.containerEl?.querySelector("#btn-toggle-blame")?.classList.toggle("active", this.showBlame);
    const optsBtn = this.containerEl?.querySelector("#btn-blame-opts");
    if (optsBtn) optsBtn.style.display = this.showBlame ? "" : "none";
    if (persist && this.tabId) state.updateTabState(this.tabId, { blame: this.showBlame || null }, { replace: true });
    if (this.showBlame) {
      const opts = this.tabData?.blameOpts || "";
      await this.blameView.loadBlame(this.currentVersion, this.currentFilePath, opts, this.api);
      if (this.blameView.blameData?.slow) {
        toast.info(`Blame took ${Math.round((this.blameView.blameData.elapsed_ms || 0) / 1000)} s (following copies compares every file of each commit)`);
      }
    }
    if (!this.isLive()) return;
    this.updateVisibleLines();
    this.updateToolbarOverflow();
  }

  /** Popover of the blame options: follow moves (-M), follow copies (-C, slow), ignore whitespace (-w). */
  showBlameOptions(anchorEl) {
    document.querySelector(".blame-opts-popover")?.remove();
    const current = parseBlameOpts(this.tabData?.blameOpts || "");
    const pop = document.createElement("div");
    pop.className = "blame-opts-popover";
    pop.innerHTML = `
      <div class="blame-opts-title">Blame options</div>
      <label><input type="checkbox" data-opt="move" ${current.move ? "checked" : ""}/> Follow lines moved within the file <code>-M</code></label>
      <label><input type="checkbox" data-opt="copy" ${current.copy ? "checked" : ""}/> Follow lines copied from other files <code>-C</code> <span class="blame-opts-slow">slow</span></label>
      <label><input type="checkbox" data-opt="ws" ${current.ws ? "checked" : ""}/> Ignore whitespace changes <code>-w</code></label>`;
    document.body.appendChild(pop);
    const r = anchorEl.getBoundingClientRect();
    pop.style.top = `${r.bottom + 4}px`;
    pop.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8))}px`;
    pop.addEventListener("change", async () => {
      const next = {};
      pop.querySelectorAll("input[data-opt]").forEach((cb) => { next[cb.dataset.opt] = cb.checked; });
      const letters = formatBlameOpts(next);
      state.updateTabState(this.tabId, { blameOpts: letters || null }, { replace: true });
      if (this.tabData) {
        if (letters) this.tabData.blameOpts = letters;
        else delete this.tabData.blameOpts;
      }
      if (this.showBlame) await this.setBlame(true, { persist: false });
    });
    const close = (e) => {
      if (e && pop.contains(e.target)) return;
      pop.remove();
      document.removeEventListener("mousedown", close, true);
      document.removeEventListener("keydown", onKey, true);
    };
    const onKey = (e) => {
      if (e.key === "Escape") close();
    };
    setTimeout(() => {
      document.addEventListener("mousedown", close, true);
      document.addEventListener("keydown", onKey, true);
    }, 0);
  }

  /** Mod+F: the find bar over the source (prefilled with the selection), or the rendered page's quick search. */
  openFind() {
    if (this.docMode === "rendered") {
      const input = this.containerEl?.querySelector(".kdoc-search-input");
      if (input) {
        input.focus();
        input.select();
      }
      return;
    }
    this.findBar.open(selectionWithin(this.linesContainer));
  }

  /**
   * Token the keyboard shortcuts act on (F12, Shift+F12): an identifier selected in this view, else
   * the token under the pointer. Null when there is none, or it is a comment or a directive.
   */
  shortcutToken() {
    if (!this.linesContainer || !this.linesContainer.isConnected) return null;
    const sel = window.getSelection ? window.getSelection() : null;
    const text = sel ? sel.toString().trim() : "";
    if (text && sel.anchorNode && this.linesContainer.contains(sel.anchorNode) && /^(struct\s+|union\s+)?[a-zA-Z_]\w*$/.test(text)) {
      const row = sel.anchorNode.parentElement?.closest(".code-row");
      return {
        name: text.replace(/^(struct|union)\s+/, "").trim(), header: "", isInclude: false, ast_id: 0,
        type_id: text.startsWith("struct ") ? 28 : (text.startsWith("union ") ? 34 : 0),
        version: this.currentVersion, filePath: this.currentFilePath, ftype: this.currentFtype,
        line: row ? (parseInt(row.dataset.lineNo, 10) || 1) : 1,
      };
    }
    const span = this.hoverTokenEl;
    if (!span || !span.isConnected || !this.linesContainer.contains(span)) return null;
    if (span.classList.contains("tok-comment") || ["2", "87", "142", "143"].includes(span.dataset.typeId)) return null;
    const row = span.closest(".code-row");
    const typeId = parseInt(span.dataset.typeId || "0", 10);
    const name = (span.dataset.name || span.textContent || "").trim();
    return {
      name, header: span.dataset.header || "",
      isInclude: span.classList.contains("ast-include") || typeId === 78 || name.includes(".h"),
      ast_id: parseInt(span.dataset.astId || "0", 10), type_id: typeId, typeClass: span.className || "",
      version: this.currentVersion, filePath: this.currentFilePath, ftype: this.currentFtype,
      line: row ? (parseInt(row.dataset.lineNo, 10) || 1) : 1,
    };
  }

  /** Record view state on the tab without a history entry. */
  persistTab(patch) {
    if (!this.tabId) return;
    state.updateTabState(this.tabId, patch, { replace: true });
  }

  /** Scroll so `line` is near the top (5 rows of context) and optionally select it (outline, sticky rows). */
  scrollToLine(line, { select = false } = {}) {
    if (!this.viewportEl || !line) return;
    const rowH = getCodeRowHeight();
    const row = this.fileDiff ? this.diffRowOfLine(line) : line - 1;
    this.viewportEl.scrollTop = Math.max(0, (row - 4) * rowH);
    if (select) this.selectLines(line, line);
    else this.updateVisibleLines();
  }

  /**
   * Bookmarked lines of this file (G05): at this revision exactly, or found again by their snippet within
   * ±200 lines when the bookmark was made at another revision (dimmed, *moved?*). Notes show as the tooltip.
   */
  async loadBookmarkMarks() {
    const path = this.currentFilePath;
    if (!path || !this.rawLines.length) return;
    const [marks, notes] = await Promise.all([localDB.userList("bookmark", this.api.repo || ""), localDB.userList("note", this.api.repo || "")]);
    const noteOf = new Map(notes.map((n) => [n.data.bookmark_key, n.data.markdown]));
    const sha = pinnedSha(this.currentVersion, this.api.versionStamps[this.currentVersion]);
    const out = new Map();
    for (const m of marks) {
      const t = m.data.target;
      if (t?.type !== "line" || t.path !== path) continue;
      const here = (t.sha && sha && t.sha.startsWith(sha.slice(0, 12))) || (!t.sha && t.rev === this.currentVersion) || t.rev === this.currentVersion;
      const at = here ? { line: t.line, moved: false } : relocate(this.rawLines, t.line, t.snippet);
      if (!at) continue;
      const note = noteOf.get(m.key) || "";
      out.set(at.line, { moved: !here, note: Boolean(note), title: `🔖 ${m.data.label}${here ? "" : ` (bookmarked at ${t.rev || t.sha}, moved?)`}${note ? `\n${note.slice(0, 300)}` : ""}` });
    }
    if (this.currentFilePath !== path) return;
    this.bookmarkMarks = out;
    this.updateVisibleLines?.();
  }

  /** The bookmark target of a line of this tab (pinned to the commit shown). */
  lineTarget(line = this.targetCursorLine) {
    const sha = pinnedSha(this.currentVersion, this.api.versionStamps[this.currentVersion]);
    return { type: "line", path: this.currentFilePath, rev: this.currentVersion, ...(sha ? { sha } : {}), line, snippet: (this.rawLines[line - 1] || "").trim().slice(0, 300) };
  }

  dispose() {
    unregisterView(this.tabId, this);
    this.hoverCard?.dispose();
    this.hoverCard = null;
    this.unsubBookmarks?.();
    this.unsubBookmarks = null;
    (this.unsubscribers || []).forEach((off) => off());
    this.unsubscribers = [];
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
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
      this.updateToolbarOverflow();
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
    this.updateToolbarOverflow();
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
        cond = (await this.api.getBuildConditions(this.currentVersion, [path], ctx.arch))?.files?.[path] || null;
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

    // Extract module name, composite membership, and compiler flags from cond/rules
    let moduleName = cond?.module || cond?.modname || "";
    let compositeName = cond?.composite || "";
    let compilerFlags = cond?.compiler_flags || cond?.cflags || cond?.flags || "";
    const rules = cond?.rules || [];
    const baseName = path.split("/").pop().replace(/\.[cCsS]$/, "");

    for (const r of rules) {
      const text = r.text || "";
      const compMatch = text.match(/^([\w-]+)-(?:objs|y)\s*[:+]?=/);
      if (compMatch && !compositeName) {
        compositeName = `${compMatch[1]}.o`;
        if (!moduleName) moduleName = `${compMatch[1]}.ko`;
      }
      const objMatch = text.match(/^obj-(?:\$\([A-Z0-9_]+\)|[ym])\s*[:+]?=\s*([\w-]+)\.o\b/);
      if (objMatch && !moduleName) {
        moduleName = `${objMatch[1]}.ko`;
      }
      const flagMatch = text.match(/(?:ccflags|asflags|CFLAGS_[\w-]+|EXTRA_CFLAGS)\s*[:+]?=\s*(.+)/);
      if (flagMatch && !compilerFlags) {
        compilerFlags = flagMatch[1].trim();
      }
    }
    if (!moduleName && baseName) {
      moduleName = `${baseName}.ko`;
    }

    const extraParts = [];
    if (compositeName) {
      extraParts.push(`<span>Part of composite <code>${escapeHtml(compositeName)}</code></span>`);
    }
    if (compilerFlags) {
      extraParts.push(`<span>Flags: <code>${escapeHtml(compilerFlags)}</code></span>`);
    }
    const extraHtml = extraParts.length ? `<span class="code-build-details" style="opacity:0.85;margin-left:6px;">(${extraParts.join(" · ")})</span>` : "";

    if (result === "n") {
      let why = escapeHtml(cond.reason || "");
      if (cond.status === "rule") {
        const { symbols, more } = explainBuild(cond.expr, lookup);
        why = `Needs ${symbols.map((s) => `<code>${escapeHtml(s.replace(/=.*$/, ""))}</code>`).join(" + ") || "its Kbuild condition"}`
          + ` (currently ${symbols.map((s) => escapeHtml(s.replace(/^CONFIG_/, ""))).join(", ")})`
          + (more ? `, or one of ${more} other rule${more === 1 ? "" : "s"}` : "");
      }
      banner.className = "code-build-banner not-built";
      banner.innerHTML = `<span class="code-build-icon">⛔</span><span><strong>Not compiled</strong> for ARCH=${arch} with the current KConfig configuration. ${why}</span>${extraHtml}${ruleHtml}`;
    } else if (result === "m") {
      banner.className = "code-build-banner module";
      const modLabel = moduleName ? ` as <code>${escapeHtml(moduleName)}</code>` : "";
      banner.innerHTML = `<span class="code-build-icon">📦</span><span>Built as a <strong>module</strong>${modLabel} for ARCH=${arch} with the current KConfig configuration.</span>${extraHtml}${ruleHtml}`;
    } else if (result === "y") {
      banner.className = "code-build-banner builtin";
      banner.innerHTML = `<span class="code-build-icon">✅</span><span>Built as <strong>built-in (y)</strong> for ARCH=${arch} with the current KConfig configuration.</span>${extraHtml}${ruleHtml}`;
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
    registerView(this.tabId, this);
    // Which KConfig engine this file follows (the first one of this revision by default, else none)
    this.resolveEngineSelection();
    this.currentFilePath = tabData.path;
    this.currentVersion = tabData.version || state.currentVersion;
    this.targetCursorLine = tabData.cursorLine || 1;
    // Selected line range (G06): cursorLine..lineEnd, highlighted and addressed by line=a-b
    this.targetLineEnd = Math.max(this.targetCursorLine, Number(tabData.lineEnd) || 0);
    // Blame comes back from the tab once the file is loaded (tab.blame, H9)
    this.showBlame = false;
    this.diffMarks = tabData.diffMarks?.ranges?.length ? tabData.diffMarks : null;
    // A re-render starts from the plain file; loadFileContent() re-enters the diff the tab records
    this.fileDiff = null;
    this.fileDiffToken++;
    this.diffMarkedLines = new Set();
    for (const [a, b] of this.diffMarks?.ranges || []) {
      for (let n = a; n <= b; n++) this.diffMarkedLines.add(n);
    }
    // Documentation renders kernel.org-style by default unless a specific source line was requested
    this.isMan = isManDocument(this.currentFilePath, this.api.repo);
    this.isDoc = this.isMan || isDocumentationPath(this.currentFilePath);
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
            <span id="code-filediff-chip" class="code-filediff-chip" hidden></span>
            <span id="file-security-badge" class="code-security-badge" style="display:none;" title="Security Insights"></span>
            <span id="file-subsystem" class="code-subsystem-chip" style="display:none;" title="Subsystem"></span>
            <span id="file-last-changed" class="code-last-changed" style="display:none;" title="Click to view full commit history"></span>
            <button class="code-btn" id="btn-kconfig-engine" title="KConfig engine this file follows"></button>
            <button class="code-btn" id="btn-file-diff" title="Diff this file with another revision or another file">⇄ Diff</button>
            <button class="code-btn" id="btn-file-history" title="View commit history for this file">📜 History</button>
            <button class="code-btn" id="btn-toggle-blame" title="Annotate every line with the commit that last changed it">Blame</button>
            <button type="button" class="code-btn code-blame-opts" id="btn-blame-opts" title="Blame options: follow moves and copies, ignore whitespace" style="display:none;" aria-haspopup="true">⚙</button>
            <button class="code-btn" id="btn-copy-raw">Copy File</button>
            <button type="button" class="code-btn" id="btn-outline" title="Outline of this file's definitions">☰ Outline</button>
            <button type="button" class="code-btn" id="btn-file-refs" title="Files including this one, Kconfig sources, Kbuild rules and documents naming it">⇇ Refs<span class="code-btn-badge" id="file-refs-count"></span></button>
            <button type="button" class="code-btn" id="btn-copy-link" title="Copy a link to the selected lines (click line numbers to select; Shift+click extends)">🔗 Link</button>
            <button class="code-btn" id="btn-create-patch" title="Edit this file and generate a git format-patch">✎ Patch</button>
            <button type="button" class="code-btn code-overflow-btn" id="btn-code-overflow" title="More actions" style="display:none;" aria-haspopup="true" aria-expanded="false">▾</button>
          </div>
        </div>
        <div class="code-build-banner" id="code-build-banner" hidden></div>
        <div class="editor-body">
          <div class="virtual-scroll-viewport" id="editor-viewport" data-scroll-restore="off">
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
    this.findBar.mount();
    this.outline.mount();

    // Attach 100% copy fidelity handler
    // A diff mixes both files' lines: its selections are copied as the browser shows them
    const clipboard = new FidelityClipboard(() => (this.fileDiff ? [] : this.rawLines));
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

    const fileDiffBtn = containerEl.querySelector("#btn-file-diff");
    fileDiffBtn.onclick = (e) => {
      e.stopPropagation();
      if (!this.rawLines.length) {
        toast.info("This file has no text lines to diff");
        return;
      }
      showDiffPicker(fileDiffBtn, {
        tabId: this.tabId,
        version: this.currentVersion,
        path: this.currentFilePath,
        current: this.fileDiff?.target || this.tabData.diffWith || null,
        onPick: (target) => this.enterFileDiff(target),
        client: this.api,
      });
    };

    const blameBtn = containerEl.querySelector("#btn-toggle-blame");
    blameBtn.onclick = async () => {
      if (this.fileDiff) return;
      if (this.docMode === "rendered") this.setDocMode("source");
      await this.setBlame(!this.showBlame);
    };
    containerEl.querySelector("#btn-outline").onclick = () => this.outline.toggle();
    const refsBtn = containerEl.querySelector("#btn-file-refs");
    refsBtn.onclick = async (e) => {
      e.stopPropagation();
      const r = (refsBtn.offsetWidth ? refsBtn : containerEl.querySelector("#btn-code-overflow") || refsBtn).getBoundingClientRect();
      const res = await contextMenu.showFileReferencesMenu(Math.max(8, r.right - 360), r.bottom + 4, this.currentVersion, this.currentFilePath);
      const badge = containerEl.querySelector("#file-refs-count");
      if (badge && res) badge.textContent = ` ${referenceCount(res)}`;
    };
    const linkBtn = containerEl.querySelector("#btn-copy-link");
    linkBtn.onclick = (e) => {
      e.stopPropagation();
      const r = (linkBtn.offsetWidth ? linkBtn : containerEl.querySelector("#btn-code-overflow") || linkBtn).getBoundingClientRect();
      const { start, end } = this.linkRange();
      contextMenu.renderMenu(Math.max(8, r.right - 300), r.bottom + 4, {
        header: { title: `${this.currentFilePath}:${formatLineSpec(start, end)}`, sub: "Click line numbers to select; Shift+click extends" },
        items: this.linkMenuItems(),
      });
    };
    // The repository's web viewer (G06 "Copy External Link"), from the project catalog
    this.repoGitUrl = "";
    this.api.getProjects().then((res) => {
      const projects = res?.projects || [];
      const p = projects.find((x) => x.repo === this.api.repo) || projects.find((x) => x.active) || projects[0];
      this.repoGitUrl = p?.git_url || "";
    }).catch(() => {});
    const blameOptsBtn = containerEl.querySelector("#btn-blame-opts");
    blameOptsBtn.onclick = (e) => {
      e.stopPropagation();
      this.showBlameOptions(blameOptsBtn);
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
    const overflowBtn = containerEl.querySelector("#btn-code-overflow");
    if (overflowBtn) {
      overflowBtn.onclick = (e) => {
        e.stopPropagation();
        this.showOverflowMenu(overflowBtn);
      };
    }

    this.blameView.attachListeners(this.linesContainer);

    // Token click listener for context menu
    this.linesContainer.addEventListener("click", (e) => {
      const tokenSpan = e.target.closest(".ast-token");
      if (tokenSpan) {
        // Prevent clicks on comments or structural directives
        if (tokenSpan.classList.contains("tok-comment") || tokenSpan.dataset.typeId === "2" || tokenSpan.dataset.typeId === "87" || tokenSpan.dataset.typeId === "142" || tokenSpan.dataset.typeId === "143") {
          return;
        }

        e.stopPropagation();
        const row = tokenSpan.closest(".code-row");
        const lineNo = row ? (parseInt(row.dataset.lineNo, 10) || 1) : 1;
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
        if (tokenSpan.classList.contains("tok-comment") || tokenSpan.dataset.typeId === "2" || tokenSpan.dataset.typeId === "87" || tokenSpan.dataset.typeId === "142" || tokenSpan.dataset.typeId === "143") {
          return;
        }

        e.preventDefault();
        e.stopPropagation();
        const row = tokenSpan.closest(".code-row");
        const lineNo = row ? (parseInt(row.dataset.lineNo, 10) || 1) : 1;
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

    // Line-number gutter: click selects a line, Shift+click extends, dragging selects a range (G06)
    this.linesContainer.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      const gutter = e.target.closest(".gutter-line-no");
      if (!gutter) return;
      const row = gutter.closest(".code-row");
      const line = parseInt(row?.dataset.lineNo || "", 10);
      if (!line) return;
      e.preventDefault();
      const anchor = e.shiftKey ? this.targetCursorLine : line;
      this.selectLines(anchor, line, { persist: false });
      const onMove = (ev) => {
        const r = document.elementFromPoint(ev.clientX, ev.clientY)?.closest?.(".code-row");
        const l = parseInt(r?.dataset.lineNo || "", 10);
        if (l && this.linesContainer.contains(r)) this.selectLines(anchor, l, { persist: false });
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        this.selectLines(this.targetCursorLine, this.targetLineEnd, { persist: true, anchor });
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    // Bookmark gutter markers (G05) follow edits made anywhere, this window or another
    this.unsubBookmarks?.();
    this.unsubBookmarks = localDB.onUserChanged((d) => {
      if (!d.kind || d.kind === "bookmark" || d.kind === "note" || d.kind === "*") this.loadBookmarkMarks();
    });

    // Hover cards (G04): signature, definition and kernel-doc of the token the pointer rests on
    this.hoverCard?.dispose();
    this.hoverCard = new HoverCard(this);
    this.hoverCard.attach(this.linesContainer, this.viewportEl);

    // Token under the pointer: what F12 / Shift+F12 act on when nothing is selected (§5.31)
    this.linesContainer.addEventListener("mouseover", (e) => {
      const span = e.target.closest?.(".ast-token");
      if (span) this.hoverTokenEl = span;
    });
    this.linesContainer.addEventListener("mouseleave", () => {
      this.hoverTokenEl = null;
    });

    // Right-click context menu listener for editor lines, tokens, and selections
    this.linesContainer.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();

      const row = e.target.closest(".code-row");
      const lineNo = row ? (parseInt(row.dataset.lineNo, 10) || 1) : 1;
      const lineText = (this.rawLines && this.rawLines[lineNo - 1]) || "";
      const selection = window.getSelection() ? window.getSelection().toString().trim() : "";

      let tokenData = null;
      let tokenSpan = e.target.closest(".ast-token");
      // If user right-clicked on 'struct' or 'union' keyword, look ahead to the struct name token
      if (!tokenSpan && e.target.classList.contains("tok-keyword")) {
        const kw = e.target.textContent.trim();
        if ((kw === "struct" || kw === "union") && e.target.nextElementSibling) {
          tokenSpan = e.target.nextElementSibling.closest(".ast-token") || e.target.nextElementSibling;
        }
      }

      if (tokenSpan && !tokenSpan.classList.contains("tok-comment") && tokenSpan.dataset.typeId !== "2" && tokenSpan.dataset.typeId !== "87" && tokenSpan.dataset.typeId !== "142" && tokenSpan.dataset.typeId !== "143") {
        const astId = parseInt(tokenSpan.dataset.astId || "0", 10);
        const typeId = parseInt(tokenSpan.dataset.typeId || "0", 10);
        const name = (tokenSpan.dataset.name || tokenSpan.textContent || "").trim();
        const header = tokenSpan.dataset.header || "";
        const isInclude = tokenSpan.classList.contains("ast-include") || typeId === 78 || name.includes(".h");
        tokenData = {
          name,
          header,
          isInclude,
          ast_id: astId,
          type_id: typeId,
          typeClass: tokenSpan.className || "",
          version: this.currentVersion,
          filePath: this.currentFilePath,
          ftype: this.currentFtype,
          line: lineNo
        };
      }

      // If no AST token span was clicked, but user has an active text selection of a symbol
      if (!tokenData && selection && /^(struct\s+|union\s+)?[a-zA-Z_]\w*$/.test(selection)) {
        const cleanName = selection.replace(/^(struct|union)\s+/, "").trim();
        tokenData = {
          name: cleanName,
          header: "",
          isInclude: false,
          ast_id: 0,
          type_id: selection.startsWith("struct ") ? 28 : (selection.startsWith("union ") ? 34 : 0),
          typeClass: selection.startsWith("struct ") ? "ast-struct tok-type" : (selection.startsWith("union ") ? "ast-struct tok-type" : "tok-identifier"),
          version: this.currentVersion,
          filePath: this.currentFilePath,
          ftype: this.currentFtype,
          line: lineNo
        };
      }

      const blame = this.blameView?.blameByLine?.get(lineNo) || null;

      contextMenu.showEditorMenu(e.clientX, e.clientY, {
        token: tokenData,
        lineText,
        filePath: this.currentFilePath,
        ftype: this.currentFtype,
        line: lineNo,
        selection,
        version: this.currentVersion,
        blame,
        // Links to the selected lines, or to this line when it is outside the selection (G06)
        linkItems: row?.dataset.lineNo ? this.linkMenuItems(lineNo) : [],
      });
    });

    // Viewport scrolling: the position is kept on the tab, so coming back lands where it was left
    this.viewportEl.addEventListener("scroll", () => {
      this.updateVisibleLines();
      this.updateMinimapSlider();
      this.rememberScrollPosition();
      this.outline.update();
    });

    // The minimap is to scale with the document, so clicking or dragging it scrolls there
    this.attachMinimapScrubbing();

    // A split, a resized window or a toggled sidebar changes the minimap's scale (one observer per
    // render: the previous one watched the element this render just replaced)
    this.resizeObserver?.disconnect();
    if (typeof ResizeObserver === "function") {
      this.resizeObserver = new ResizeObserver(() => {
        if (!this.isLive()) return;
        this.updateToolbarOverflow();
        if (!this.rawLines.length) return;
        this.renderMinimap();
        this.updateMinimapSlider();
        this.updateVisibleLines();
      });
      const editorBody = containerEl.querySelector(".editor-body");
      if (editorBody) this.resizeObserver.observe(editorBody);
      const toolbar = containerEl.querySelector(".code-toolbar");
      if (toolbar) this.resizeObserver.observe(toolbar);
    }

    this.updateToolbarOverflow();

    // Load file content from API
    await this.loadFileContent();
  }

  async loadFileContent() {
    this.hasLastChanged = false;
    const lastChangedEl = this.viewportEl?.closest(".code-viewer-container")?.querySelector("#file-last-changed");
    if (lastChangedEl) {
      lastChangedEl.style.display = "none";
    }
    const secBadgeEl = this.viewportEl?.closest(".code-viewer-container")?.querySelector("#file-security-badge");
    if (secBadgeEl) {
      secBadgeEl.style.display = "none";
    }
    const subChipEl = this.viewportEl?.closest(".code-viewer-container")?.querySelector("#file-subsystem");
    if (subChipEl) {
      subChipEl.style.display = "none";
    }
    this.linesContainer.innerHTML = `<div style="padding:20px;color:var(--text-muted);">Loading ${escapeHtml(this.currentFilePath)}...</div>`;
    try {
      const res = await this.api.getFileContent(this.currentVersion, this.currentFilePath);
      if (!res) {
        throw new Error("No response received from server for file content");
      }
      if (res.is_directory || res.type === "dir") {
        state.emit("explorer:reveal", this.currentFilePath);
        const entries = res.entries || res.tree || [];
        const dirListHtml = entries.map((item) => {
          const isItemDir = item.type === "dir";
          const icon = isItemDir ? "📁" : "📄";
          const statBadge = item.s_stat_label ? `<span class="badge" style="font-size:10px;padding:1px 6px;border-radius:3px;background:var(--bg-tertiary);">${escapeHtml(item.s_stat_label)}</span>` : "";
          return `
            <div class="dir-entry-row" data-path="${escapeHtml(item.path)}" data-type="${escapeHtml(item.type)}" style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid var(--border-color);cursor:pointer;transition:background 0.15s;" onmouseover="this.style.background='var(--bg-hover)'" onmouseout="this.style.background='transparent'">
              <div style="display:flex;align-items:center;gap:10px;">
                <span style="font-size:16px;">${icon}</span>
                <span style="font-weight:${isItemDir ? '600' : '400'};color:${isItemDir ? 'var(--accent-blue)' : 'var(--text-primary)'};">${escapeHtml(item.name)}</span>
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
                <h3 style="margin:0;font-size:18px;display:flex;align-items:center;gap:8px;">📁 ${escapeHtml(this.currentFilePath || "Kernel Root")}</h3>
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

      // Binary and oversized (> 50 MB) files come without content: say why instead of an empty editor
      if (res.is_binary || res.is_oversized) {
        this.rawLines = [];
        this.linesContainer.innerHTML = `<div style="padding:20px;color:var(--text-muted);">${escapeHtml(res.message || "This file cannot be displayed.")}</div>`;
        return;
      }

      const content = res.content || "";
      this.rawLines = content.split(/\r?\n/);
      this.currentFtype = res.ftype;
      this.astOverlay.setFileInfo(res.ftype, this.currentFilePath, this.rawLines[0] || "");
      this.astOverlay.indexMultiLineSpans(this.rawLines);

      // Update last changed metadata chip in toolbar if available
      if (res.file_info && res.file_info.last_changed) {
        const lc = res.file_info.last_changed;
        const lastChangedEl = this.viewportEl?.closest(".code-viewer-container")?.querySelector("#file-last-changed");
        if (lastChangedEl) {
          const dateStr = lc.author_date_iso ? lc.author_date_iso.split(" ")[0] : "";
          const authorStr = lc.author_name || "";
          this.hasLastChanged = true;
          lastChangedEl.innerHTML = `🕒 Last Changed: <strong style="color:var(--text-primary);margin-left:3px;">${escapeHtml(dateStr)}</strong><span style="opacity:0.75;margin-left:4px;">by ${escapeHtml(authorStr)}</span>`;
          lastChangedEl.title = `Last changed: ${lc.author_date_iso} by ${authorStr}\nCommit: ${lc.commit_hash_short}\nSubject: ${lc.subject}\nClick to view full history`;
          this.updateToolbarOverflow();
        }
      }

      // Fetch CVE security badge for this file
      this.api.getFileCves(this.currentVersion, this.currentFilePath).then((cveRes) => {
        const badge = this.viewportEl?.closest(".code-viewer-container")?.querySelector("#file-security-badge");
        if (!badge) return;
        const total = cveRes?.total ?? (Array.isArray(cveRes?.cves) ? cveRes.cves.length : 0);
        if (total > 0) {
          badge.style.display = "inline-flex";
          badge.innerHTML = `🛡️ ${total} CVE${total === 1 ? "" : "s"}`;
          badge.title = `${total} CVE${total === 1 ? "" : "s"} associated with ${this.currentFilePath} (click to view Security Insights)`;
          badge.onclick = () => {
            state.openTab({
              type: "insights",
              title: "Insights · Security",
              view: "security",
              version: this.currentVersion,
            });
          };
          this.updateToolbarOverflow();
        } else {
          badge.style.display = "none";
        }
      }).catch((err) => console.warn("CVE lookup failed:", err));

      // Fetch Subsystem indicator for this file
      this.api.matchMaintainers(this.currentVersion, "", [this.currentFilePath]).then((matchRes) => {
        const subChip = this.viewportEl?.closest(".code-viewer-container")?.querySelector("#file-subsystem");
        if (!subChip) return;
        const subs = matchRes?.subsystems || [];
        const subName = subs[0] || matchRes?.files?.[0]?.subsystems?.[0]?.name || "";
        if (subName) {
          subChip.style.display = "inline-flex";
          subChip.innerHTML = `🏷️ ${escapeHtml(subName)}`;
          subChip.title = `Subsystem: ${subName} (click to view Maintainers)`;
          subChip.onclick = () => {
            state.openTab({
              type: "maintainers",
              title: "Maintainers",
              section: "subsystems",
              query: subName,
              version: this.currentVersion,
            });
          };
          this.updateToolbarOverflow();
        } else {
          subChip.style.display = "none";
        }
      }).catch((err) => console.warn("Subsystem lookup failed:", err));

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
      const rowH = getCodeRowHeight();
      const totalHeight = this.rawLines.length * rowH;
      this.spacerEl.style.height = `${totalHeight}px`;

      // Come back where the tab was left, unless a specific line was requested (navigation drops
      // the saved position, see state.openTab)
      const savedTop = Number(this.tabData?.scrollTop);
      if (Number.isFinite(savedTop) && savedTop > 0) {
        this.viewportEl.scrollTop = Math.min(savedTop, Math.max(0, totalHeight - this.viewportEl.clientHeight));
      } else if (this.targetLineEnd > this.targetCursorLine) {
        // A line range (permalink, G06): centered
        this.viewportEl.scrollTop = centeredScrollTop(this.targetCursorLine, this.targetLineEnd, rowH, this.viewportEl.clientHeight || 600);
      } else if (this.targetCursorLine > 1) {
        this.viewportEl.scrollTop = Math.max(0, (this.targetCursorLine - 5) * rowH);
      }

      this.updateVisibleLines();
      this.renderMinimap();
      this.updateMinimapSlider();
      this.findBar.refresh();
      this.outline.load();
      this.loadBookmarkMarks();
      // The tab was left with blame on (or a link / "blame before this change" asks for it, H9)
      if (this.tabData.blame && this.docMode === "source" && !this.showBlame) {
        this.setBlame(true, { persist: false });
      }

      // The tab was left showing a diff (or a deep link asks for one): show it again
      if ((this.tabData.diffWith?.version && this.tabData.diffWith?.path) || this.tabData.diffWith?.kind === "patch") {
        await this.enterFileDiff(this.tabData.diffWith, { restoring: true });
      }
    } catch (err) {
      this.linesContainer.innerHTML = `
        <div style="padding:20px;color:var(--accent-red);">
          Failed to load file: ${escapeHtml(err.message)}
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
    if (this.isMan) {
      // Rendered from the page's document model (`/api/man/{v}/page?path=`), not from the roff text
      mountManDocument(docViewport, {
        repo: this.api.repo,
        version: this.currentVersion,
        path: this.currentFilePath,
        tab: this.tabData,
        canGoBack: history.length > 0,
        onShowSource: () => this.setDocMode("source"),
        onNavigate: (path) => this.navigateDoc(path, true),
        onBack: () => {
          const prev = (this.tabData.docHistory || []).pop();
          if (prev) this.navigateDoc(prev, false);
        },
      });
      return;
    }
    const page = new DocPage({
      path: this.currentFilePath,
      version: this.currentVersion,
      api: this.api,
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
    this.tabData.docMode = isDocumentationPath(docPath) || isManDocument(docPath) ? "rendered" : "source";
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
    const rowH = getCodeRowHeight();
    this.viewportEl.scrollTop = Math.max(0, (target[0] - 5) * rowH);
    this.updateVisibleLines();
    this.rememberScrollPosition();
  }

  /** Rows the viewport scrolls through: the file's lines, or the rows of the diff it shows. */
  rowCount() {
    const fd = this.fileDiff;
    if (!fd) return this.rawLines.length;
    return fd.layout === "split" ? fd.pairs.length : fd.rows.length;
  }

  /** Line of the tab's own file at diff row `i` (0 when that row only exists in the other file). */
  diffShownLineAt(i) {
    const fd = this.fileDiff;
    if (fd.layout !== "split") return fd.rows[i]?.s || 0;
    const p = fd.pairs[i];
    return (p?.r?.s || p?.l?.s) || 0;
  }

  /** First line of the own file shown at or below the top of the viewport. */
  topShownLine() {
    const rowH = getCodeRowHeight();
    const top = Math.floor((this.viewportEl?.scrollTop || 0) / rowH);
    if (!this.fileDiff) return top + 1;
    const total = this.rowCount();
    for (let i = top; i < total; i++) {
      const s = this.diffShownLineAt(i);
      if (s) return s;
    }
    return Math.max(1, this.rawLines.length);
  }

  /** Diff row showing own-file line `line` (or the first one after it). */
  diffRowOfLine(line) {
    const total = this.rowCount();
    for (let i = 0; i < total; i++) {
      if (this.diffShownLineAt(i) >= line) return i;
    }
    return Math.max(0, total - 1);
  }

  /**
   * Show this file diffed with `target` ({ version, path, swap }): by default the target is the base
   * and this tab's file the new side; `swap` reverses them. Only the changed lines are fetched, the
   * rest is this tab's own text (`buildDiffRows`). Records the choice on the tab (and so in its URL).
   */
  async enterFileDiff(target, { restoring = false } = {}) {
    if (!this.rawLines.length || !this.isLive()) return;
    const token = ++this.fileDiffToken;
    // Diff source kind `patch` (P1): this tab's file against the text mailing-list patches make of it
    const isPatch = target.kind === "patch";
    // The tab's own file is the base of a patch diff: the layout of a swapped blob diff (own file on the left)
    const swap = isPatch ? true : Boolean(target.swap);
    const pick = isPatch
      ? { kind: "patch", mids: [...(target.mids || [])], revision: target.revision || this.currentVersion, path: this.currentFilePath, applyId: target.applyId || "", swap: true }
      : { version: target.version, path: String(target.path || "").replace(/^\/+|\/+$/g, ""), swap };
    const label = isPatch ? `patched (${pick.mids.length} message${pick.mids.length === 1 ? "" : "s"})` : diffSideLabel(pick.version, pick.path, this.currentFilePath);
    const chip = this.containerEl.querySelector("#code-filediff-chip");
    chip.hidden = false;
    chip.className = "code-filediff-chip loading";
    chip.textContent = `⇄ Loading diff with ${label}…`;
    const [va, pa, vb, pb] = swap
      ? [this.currentVersion, this.currentFilePath, pick.version, pick.path]
      : [pick.version, pick.path, this.currentVersion, this.currentFilePath];
    let res;
    try {
      res = isPatch ? await patchedFileDiff(pick, this.api) : await this.api.getBlobDiff(va, pa, vb, pb);
    } catch (err) {
      if (token !== this.fileDiffToken || !this.isLive()) return;
      toast.error(`Diff with ${label} failed: ${err.message}`);
      // A diff that cannot be shown is not kept on the tab; the one already on screen stays
      if (this.fileDiff) this.updateFileDiffChip();
      else this.exitFileDiff();
      return;
    }
    if (token !== this.fileDiffToken || !this.isLive()) return;
    if (!res || res.binary) {
      toast.warning("Binary files differ: there is no line diff to show");
      if (this.fileDiff) this.updateFileDiffChip();
      else this.exitFileDiff();
      return;
    }

    if (this.docMode === "rendered") this.setDocMode("source");
    const topLine = this.topShownLine();
    const { rows, blocks } = buildDiffRows(res.hunks, this.rawLines, !swap, res.truncated);
    const { pairs, blocks: pairBlocks } = pairDiffRows(rows);
    this.fileDiff = { target: pick, res, rows, blocks, pairs, pairBlocks, layout: this.tabData.diffLayout === "split" ? "split" : "unified", lastJump: null };
    if (JSON.stringify(this.tabData.diffWith || null) !== JSON.stringify(pick)) {
      state.updateTabState(this.tabId, { diffWith: pick }, { replace: true });
      this.tabData.diffWith = pick;
    }

    // Blame and commit change markers annotate the plain file: hidden while the diff is shown
    if (this.showBlame) {
      this.showBlame = false;
      this.containerEl.querySelector("#btn-toggle-blame")?.classList.remove("active");
      const optsBtn = this.containerEl.querySelector("#btn-blame-opts");
      if (optsBtn) optsBtn.style.display = "none";
    }
    this.setPlainOnlyControls(false);

    const rowH = getCodeRowHeight();
    this.spacerEl.style.height = `${this.rowCount() * rowH}px`;
    const savedTop = Number(this.tabData?.scrollTop);
    if (restoring && Number.isFinite(savedTop) && savedTop > 0) {
      this.viewportEl.scrollTop = savedTop;
    } else {
      this.viewportEl.scrollTop = Math.max(0, this.diffRowOfLine(topLine) * rowH);
    }
    this.updateFileDiffChip();
    this.updateVisibleLines();
    this.renderMinimap();
    this.updateMinimapSlider();
    this.rememberScrollPosition();
    if (!restoring) {
      if (res.identical) toast.info("The two files are identical");
      else if (!blocks.length) toast.info("No line differs between the two files");
    }
    if (res.truncated) toast.warning("The diff is too large: it stops after the last change that could be loaded");
    this.findBar.refresh();
  }

  /** Back to the plain file, at the line the diff was showing. */
  exitFileDiff() {
    this.fileDiffToken++;
    const topLine = this.fileDiff ? this.topShownLine() : null;
    this.fileDiff = null;
    state.updateTabState(this.tabId, { diffWith: null }, { replace: true });
    delete this.tabData.diffWith;
    this.setPlainOnlyControls(true);
    const chip = this.containerEl?.querySelector("#code-filediff-chip");
    if (chip) chip.hidden = true;
    this.updateToolbarOverflow();
    if (!this.viewportEl || !this.rawLines.length) return;
    const rowH = getCodeRowHeight();
    this.spacerEl.style.height = `${this.rawLines.length * rowH}px`;
    if (topLine) this.viewportEl.scrollTop = (topLine - 1) * rowH;
    this.updateVisibleLines();
    this.renderMinimap();
    this.updateMinimapSlider();
    this.rememberScrollPosition();
    this.findBar.refresh();
  }

  /** Blame and the commit change-marker chip belong to the plain file view. */
  setPlainOnlyControls(enabled) {
    const blameBtn = this.containerEl?.querySelector("#btn-toggle-blame");
    if (blameBtn) {
      blameBtn.disabled = !enabled;
      blameBtn.title = enabled ? "Annotate every line with the commit that last changed it" : "Blame annotates the plain file: close the diff (✕) to use it";
    }
    const markChip = this.containerEl?.querySelector(".code-diffmark-chip");
    if (markChip) markChip.style.display = enabled ? "" : "none";
    this.containerEl?.querySelector("#btn-file-diff")?.classList.toggle("active", !enabled);
  }

  /** Unified rows (old/new gutters) or side-by-side pairs; toggled from the diff chip. */
  setDiffLayout(layout) {
    const fd = this.fileDiff;
    if (!fd || fd.layout === layout) return;
    const topLine = this.topShownLine();
    fd.layout = layout;
    fd.lastJump = null;
    state.updateTabState(this.tabId, { diffLayout: layout === "split" ? "split" : "" }, { replace: true });
    if (layout === "split") this.tabData.diffLayout = "split";
    else delete this.tabData.diffLayout;
    const rowH = getCodeRowHeight();
    this.spacerEl.style.height = `${this.rowCount() * rowH}px`;
    this.viewportEl.scrollTop = Math.max(0, this.diffRowOfLine(topLine) * rowH);
    this.updateFileDiffChip();
    this.updateVisibleLines();
    this.renderMinimap();
    this.updateMinimapSlider();
    this.rememberScrollPosition();
    this.findBar.refresh();
  }

  /** Scroll to the previous/next change block of the diff. */
  jumpToDiffHunk(dir) {
    const fd = this.fileDiff;
    const blocks = fd?.layout === "split" ? fd.pairBlocks : fd?.blocks;
    if (!blocks?.length) return;
    const rowH = getCodeRowHeight();
    const top = this.viewportEl.scrollTop;
    let idx;
    if (fd.lastJump && Math.abs(fd.lastJump.top - top) < 1) {
      idx = fd.lastJump.idx + dir;
    } else {
      const cur = Math.floor(top / rowH) + 5;
      idx = dir > 0 ? blocks.findIndex(([a]) => a > cur) : blocks.findLastIndex(([a]) => a < cur);
    }
    if (idx < 0 || idx >= blocks.length) idx = dir > 0 ? 0 : blocks.length - 1;
    this.viewportEl.scrollTop = Math.max(0, (blocks[idx][0] - 5) * rowH);
    fd.lastJump = { idx, top: this.viewportEl.scrollTop };
    this.updateFileDiffChip();
    this.updateVisibleLines();
  }

  /** `⇄ base → target  +A −D` with the layout toggle, ↑/↓, swap and close. */
  updateFileDiffChip() {
    const chip = this.containerEl?.querySelector("#code-filediff-chip");
    const fd = this.fileDiff;
    if (!chip) return;
    if (!fd) {
      chip.hidden = true;
      return;
    }
    const own = diffSideLabel(this.currentVersion, this.currentFilePath, this.currentFilePath);
    const isPatch = fd.target.kind === "patch";
    const other = isPatch ? `patched (${fd.target.mids.length} message${fd.target.mids.length === 1 ? "" : "s"})` : diffSideLabel(fd.target.version, fd.target.path, this.currentFilePath);
    const [base, tgt] = fd.target.swap ? [own, other] : [other, own];
    const r = fd.res;
    const blocks = fd.layout === "split" ? fd.pairBlocks : fd.blocks;
    // "–/N" before the first jump: the same width as "n/N", so the chip never shifts while navigating
    const at = fd.lastJump ? `${fd.lastJump.idx + 1}/${blocks.length}` : `–/${blocks.length}`;
    const split = fd.layout === "split";
    chip.hidden = false;
    chip.className = "code-filediff-chip";
    chip.title = isPatch
      ? `Base: ${this.currentFilePath} @ ${this.currentVersion}\nTarget: the same file with the mailing-list patches applied (in memory)\nClick ⇄ Diff to pick another file or revision`
      : `Base: ${fd.target.swap ? `${this.currentFilePath} @ ${this.currentVersion}` : `${fd.target.path} @ ${fd.target.version}`}\n`
      + `Target: ${fd.target.swap ? `${fd.target.path} @ ${fd.target.version}` : `${this.currentFilePath} @ ${this.currentVersion}`}\n`
      + "Click ⇄ Diff to pick another file or revision";
    chip.innerHTML = `
      <span class="code-filediff-label">⇄ ${escapeHtml(base)} → ${escapeHtml(tgt)}</span>
      ${r.identical ? `<span class="code-filediff-muted">identical</span>` : `<span class="code-filediff-add">+${r.additions}</span><span class="code-filediff-del">−${r.deletions}</span>`}
      ${r.truncated ? `<span class="code-filediff-warn" title="The diff was cut at the server's size limit">⚠ truncated</span>` : ""}
      ${blocks.length ? `<span class="code-filediff-muted code-filediff-pos" style="width:${String(blocks.length).length * 2 + 1}ch" title="Change blocks">${at}</span>` : ""}
      <button data-fd="layout" title="${split ? "Unified view (one column, old and new line numbers)" : "Side-by-side view (base left, target right)"}">${split ? "☰" : "◫"}</button>
      <button data-fd="-1" title="Previous change" ${blocks.length ? "" : "disabled"}>↑</button><button data-fd="1" title="Next change" ${blocks.length ? "" : "disabled"}>↓</button>
      ${isPatch ? "" : `<button data-fd="swap" title="Swap base and target">⇆</button>`}<button data-fd="close" title="Close the diff">✕</button>`;
    chip.querySelectorAll("[data-fd]").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const act = btn.dataset.fd;
        if (act === "layout") this.setDiffLayout(split ? "unified" : "split");
        else if (act === "swap") this.enterFileDiff({ ...fd.target, swap: !fd.target.swap });
        else if (act === "close") this.exitFileDiff();
        else this.jumpToDiffHunk(parseInt(act, 10));
      };
    });
    this.updateToolbarOverflow();
  }

  /** Text of one diff row: syntax-highlighted when it is a line of this tab's file, plain otherwise. */
  diffRowHtml(row, asShown) {
    if (asShown && row.s) {
      const isDimmed = !this.dimSuspended && this.dimmedLines.has(row.s);
      return this.astOverlay.renderLineHtml(row.s, this.rawLines[row.s - 1] ?? row.t, isDimmed);
    }
    return escapeHtml(row.t);
  }

  renderDiffLines() {
    const fd = this.fileDiff;
    const rowH = getCodeRowHeight();
    const scrollTop = this.viewportEl.scrollTop;
    const viewportHeight = this.viewportEl.clientHeight || 600;
    const total = this.rowCount();
    const startIdx = Math.max(0, Math.floor(scrollTop / rowH) - BUFFER_LINES);
    const endIdx = Math.min(total - 1, Math.ceil((scrollTop + viewportHeight) / rowH) + BUFFER_LINES);
    this.linesContainer.style.transform = `translateY(${startIdx * rowH}px)`;
    const KIND = { " ": "diff-ctx", "+": "diff-add", "-": "diff-del" };
    const SIGN = { " ": " ", "+": "+", "-": "−" };
    let html = "";
    if (fd.layout === "split") {
      // The tab's own file is the target (right) unless swapped; context rows are the same row on both sides
      const shownSide = fd.target.swap ? "l" : "r";
      const half = (row, side) => {
        if (!row) return `<div class="diff-half diff-filler"><div class="gutter-line-no diff-ln"></div><div class="diff-sign"></div><div class="code-text"></div></div>`;
        const ln = side === "l" ? row.o : row.n;
        return `<div class="diff-half ${KIND[row.k]}"><div class="gutter-line-no diff-ln">${ln ?? ""}</div><div class="diff-sign">${SIGN[row.k]}</div><div class="code-text">${this.diffRowHtml(row, row.k !== " " || side === shownSide)}</div></div>`;
      };
      for (let i = startIdx; i <= endIdx; i++) {
        const p = fd.pairs[i];
        const s = this.diffShownLineAt(i);
        const hl = s && this.isLineSelected(s) ? " highlighted-line" : "";
        html += `<div class="code-row diff-split-row${hl}" data-line-no="${s || ""}" data-row="${i}">${half(p.l, "l")}${half(p.r, "r")}</div>`;
      }
    } else {
      for (let i = startIdx; i <= endIdx; i++) {
        const r = fd.rows[i];
        const hl = r.s && this.isLineSelected(r.s) ? " highlighted-line" : "";
        html += `<div class="code-row diff-row ${KIND[r.k]}${hl}" data-line-no="${r.s || ""}" data-row="${i}">`
          + `<div class="gutter-line-no diff-ln">${r.o ?? ""}</div><div class="gutter-line-no diff-ln">${r.n ?? ""}</div>`
          + `<div class="diff-sign">${SIGN[r.k]}</div><div class="code-text">${this.diffRowHtml(r, true)}</div></div>`;
      }
    }
    this.linesContainer.innerHTML = html;
    this.findBar.decorate(this.linesContainer);
  }

  /** Minimap of the diff rows: text lengths, and a red/green (amber: changed pair) bar per change. */
  renderDiffMinimap(ctx, m, w) {
    const fd = this.fileDiff;
    const split = fd.layout === "split";
    const h = Math.max(1, m.lineScale);
    for (let i = 0; i < m.totalLines; i++) {
      const p = split ? fd.pairs[i] : null;
      const text = split ? (p.r || p.l).t : fd.rows[i].t;
      const len = Math.min(w - 10, (text || "").length * 0.8);
      if (len > 0) ctx.fillRect(6, i * m.lineScale, len, h);
    }
    const colors = { add: "rgba(63, 185, 80, 0.9)", del: "rgba(248, 81, 73, 0.9)", mod: "rgba(210, 153, 34, 0.9)" };
    for (let i = 0; i < m.totalLines; i++) {
      let kind = "";
      if (split) {
        const p = fd.pairs[i];
        const del = p.l?.k === "-";
        const add = p.r?.k === "+";
        kind = del && add ? "mod" : del ? "del" : add ? "add" : "";
      } else {
        const k = fd.rows[i].k;
        kind = k === "+" ? "add" : k === "-" ? "del" : "";
      }
      if (!kind) continue;
      ctx.fillStyle = colors[kind];
      ctx.fillRect(0, i * m.lineScale, 4, Math.max(2, m.lineScale));
    }
    ctx.fillStyle = "rgba(139, 148, 158, 0.4)";
  }

  updateVisibleLines() {
    if (!this.viewportEl || this.rawLines.length === 0) return;
    if (this.fileDiff) {
      this.renderDiffLines();
      return;
    }

    const rowH = getCodeRowHeight();
    const scrollTop = this.viewportEl.scrollTop;
    const viewportHeight = this.viewportEl.clientHeight || 600;

    const startIdx = Math.max(0, Math.floor(scrollTop / rowH) - BUFFER_LINES);
    const endIdx = Math.min(this.rawLines.length - 1, Math.ceil((scrollTop + viewportHeight) / rowH) + BUFFER_LINES);

    this.linesContainer.style.transform = `translateY(${startIdx * rowH}px)`;

    let html = "";
    for (let i = startIdx; i <= endIdx; i++) {
      const lineNo = i + 1;
      const rawText = this.rawLines[i];
      const isHighlighted = this.isLineSelected(lineNo);
      const isDimmed = !this.dimSuspended && this.dimmedLines.has(lineNo);
      const diffCls = this.diffMarkedLines.has(lineNo) ? (this.diffMarks.kind === "del" ? " diff-mark-del" : " diff-mark-add") : "";

      const blameHtml = this.showBlame ? this.blameView.renderGutterCell(lineNo) : "";
      const mark = this.bookmarkMarks?.get(lineNo);
      const markCls = mark ? ` bm-gutter${mark.moved ? " bm-gutter-moved" : ""}${mark.note ? " bm-gutter-note" : ""}` : "";
      const markTitle = mark ? ` title="${escapeHtml(mark.title)}"` : "";
      const textHtml = this.astOverlay.renderLineHtml(lineNo, rawText, isDimmed);

      html += `
        <div class="code-row ${isHighlighted ? "highlighted-line" : ""}${diffCls}" data-line-no="${lineNo}" data-row="${i}">
          ${blameHtml}
          <div class="gutter-line-no${markCls}"${markTitle}>${lineNo}</div>
          <div class="code-text">${textHtml}</div>
        </div>
      `;
    }

    this.linesContainer.innerHTML = html;
    this.findBar.decorate(this.linesContainer);
  }

  /** Remember where this tab is scrolled to, so selecting it again comes back to that position. */
  rememberScrollPosition() {
    if (!this.tabData || !this.viewportEl) return;
    const top = Math.round(this.viewportEl.scrollTop);
    if (this.tabData.scrollTop === top) return;
    this.tabData.scrollTop = top;
    // The position is written on every scroll, so persisting is debounced (shared with §5.28)
    scheduleScrollSave();
  }

  /** Document height the minimap maps, and the height it is drawn at (never taller than its track). */
  minimapMetrics() {
    const totalLines = this.rowCount();
    const trackH = (this.minimapCanvas?.parentElement?.clientHeight) || 0;
    if (!totalLines || !trackH) return null;
    // One pixel per line at most: the drawing is exactly as deep as the file is long
    const lineScale = Math.min(1, trackH / totalLines);
    return {
      totalLines,
      trackH,
      lineScale,
      mapH: Math.min(trackH, totalLines * lineScale),
      docH: totalLines * getCodeRowHeight(),
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

    if (this.fileDiff) {
      this.renderDiffMinimap(ctx, m, w);
    } else {
      for (let i = 0; i < m.totalLines; i++) {
        const len = Math.min(w - 10, (this.rawLines[i] || "").length * 0.8);
        if (len > 0) {
          ctx.fillRect(4, i * m.lineScale, len, Math.max(1, m.lineScale));
        }
      }
    }

    if (this.diffMarks && !this.fileDiff) {
      ctx.fillStyle = this.diffMarks.kind === "del" ? "rgba(248, 81, 73, 0.9)" : "rgba(63, 185, 80, 0.9)";
      for (const [a, b] of this.diffMarks.ranges) {
        ctx.fillRect(0, (a - 1) * m.lineScale, 4, Math.max(2, (b - a + 1) * m.lineScale));
      }
    }

    this.findBar.drawMinimap(ctx, m, w);

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

/** Full 40-hex SHA of a revision for external viewers (the catalog only keeps 12 hex characters). */
async function resolveFullSha(version, sha12, client = api) {
  try {
    const res = await client.searchCommits(version, sha12, 1);
    const hit = (res?.commits || []).find((c) => String(c.commit_hash || "").startsWith(sha12));
    if (hit?.commit_hash?.length === 40) return hit.commit_hash;
  } catch {
    // fall back to the short SHA, which cgit and GitHub accept for unambiguous prefixes
  }
  return sha12;
}

// Code view keys (webapp/API.md §5.31): act on the selected identifier or the token under the pointer
const findAvailable = (ctx) => {
  const v = ctx.view;
  if (!v?.containerEl?.isConnected || typeof v.openFind !== "function") return false;
  // A text field elsewhere (the palette, a picker) keeps the browser's own key
  const active = document.activeElement;
  return !isTextTarget(active) || Boolean(active.closest?.(".code-find-bar, .kdoc-search"));
};
const findOpen = (ctx) => Boolean(ctx.view?.findBar?.isOpen);
shortcuts.registerAll([
  { id: "code.bookmark", keys: ["Mod+Alt+B"], scope: "code", description: "Bookmark the current line (again: remove it)",
    when: (ctx) => Boolean(ctx.view?.currentFilePath && ctx.view.rawLines?.length),
    handler: async (e, ctx) => {
      const { toggleBookmark } = await import("../bookmarks/bookmark_store.js");
      const t = ctx.view.lineTarget();
      try {
        const r = await toggleBookmark(t, { label: `${t.path.split("/").pop()}:${t.line}` });
        toast.success(r.added ? `Bookmarked ${t.path}:${t.line}` : `Bookmark on ${t.path}:${t.line} removed`);
      } catch (err) {
        toast.error(err.message);
      }
    } },
  { id: "code.find", keys: ["Mod+F"], scope: "code", allowInInputs: true, description: "Find in file",
    when: findAvailable, handler: (e, ctx) => { ctx.view.openFind(); } },
  { id: "code.findNext", keys: ["F3"], scope: "code", allowInInputs: true, description: "Next match",
    when: findOpen, handler: (e, ctx) => { ctx.view.findBar.step(1); } },
  { id: "code.findPrevious", keys: ["Shift+F3"], scope: "code", allowInInputs: true, description: "Previous match",
    when: findOpen, handler: (e, ctx) => { ctx.view.findBar.step(-1); } },
  { id: "find.enter", keys: ["Enter", "Shift+Enter"], scope: "code-find", docOnly: true, description: "Next / previous match (in the find box)" },
  { id: "find.close", keys: ["Escape"], scope: "code-find", docOnly: true, description: "Close the find bar" },
  { id: "code.goToDefinition", keys: ["F12"], scope: "code", description: "Go to the definition of the token under the pointer",
    when: (ctx) => Boolean(ctx.view?.shortcutToken?.()),
    handler: (e, ctx) => { contextMenu.executeDefaultAction(ctx.view.shortcutToken(), { forceNew: false }); } },
  { id: "code.findReferences", keys: ["Shift+F12"], scope: "code", description: "Find references of the token under the pointer",
    when: (ctx) => Boolean(ctx.view?.shortcutToken?.()),
    handler: (e, ctx) => { contextMenu.findReferences(ctx.view.shortcutToken()); } },
]);
