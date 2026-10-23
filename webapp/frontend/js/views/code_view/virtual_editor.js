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
import { DirectiveEvaluator } from "../kconfig/directive_evaluator.js";

// Source types whose #if/#ifdef blocks follow C preprocessor semantics
const PREPROCESSED_RE = /\.(c|h|S|lds|dts|dtsi|inc)$/i;
// A resolved configuration (defconfig loaded in the KConfig view) has far more than the default seeds
const MIN_CONFIG_SYMBOLS = 50;

const ROW_HEIGHT = 20; // 20px per line
const BUFFER_LINES = 15; // lines buffered above and below viewport

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
    const refreshDimming = () => {
      if (!this.linesContainer || !this.rawLines.length) return;
      this.computeDimming();
      this.updateVisibleLines();
    };
    state.subscribe("kconfig:engine:changed", refreshDimming);
    state.subscribe("kconfig:assignments:updated", refreshDimming);
    state.subscribe("kconfig:assignment:changed", refreshDimming);
  }

  /** #ifdef dimming is only active while the Kconfig engine is enabled with a resolved configuration. */
  dimmingAvailable() {
    return state.kconfigEngineActive
      && PREPROCESSED_RE.test(this.currentFilePath || "")
      && Object.keys(state.kconfigAssignments || {}).length / 2 >= MIN_CONFIG_SYMBOLS;
  }

  computeDimming() {
    const chip = this.containerEl?.querySelector("#kconfig-dim-chip");
    if (!this.dimmingAvailable()) {
      this.dimmedLines = new Set();
      if (chip) chip.style.display = "none";
      return;
    }
    this.directiveEvaluator.setAssignments(state.kconfigAssignments);
    this.dimmedLines = this.directiveEvaluator.evaluateDimmedLines(this.rawLines);
    if (chip) {
      chip.style.display = "inline-flex";
      chip.textContent = `⚙ #ifdef: ${this.dimmedLines.size} line${this.dimmedLines.size === 1 ? "" : "s"} compiled out`;
      chip.title = "Lines inside #if/#ifdef branches disabled by the KConfig engine's current configuration.\nToggle the engine in the KConfig view to turn dimming off.";
    }
  }

  async render(containerEl, tabData) {
    this.tabData = tabData;
    this.containerEl = containerEl;
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

    const pathParts = (this.currentFilePath || "").split("/").filter(Boolean);
    let breadcrumbsHtml = `<span class="breadcrumb-item">${this.currentVersion}</span>`;
    let accumPath = "";
    pathParts.forEach((part, idx) => {
      accumPath += (accumPath ? "/" : "") + part;
      const isLast = idx === pathParts.length - 1;
      breadcrumbsHtml += `<span class="breadcrumb-sep">&gt;</span>`;
      if (isLast) {
        breadcrumbsHtml += `<span class="breadcrumb-item active" style="font-weight:500;">${part}</span>`;
      } else {
        breadcrumbsHtml += `<span class="breadcrumb-item breadcrumb-dir" data-path="${accumPath}" style="cursor:pointer;color:var(--accent-blue);">${part}</span>`;
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
            <span id="kconfig-dim-chip" class="code-dim-chip" style="display:none;"></span>
            ${this.diffMarks ? `<span class="code-diffmark-chip ${this.diffMarks.kind === "del" ? "del" : "add"}" title="Lines ${this.diffMarks.kind === "del" ? "removed" : "added or changed"} by commit ${this.diffMarks.commit || ""}">
              ± ${(this.diffMarks.commit || "").slice(0, 8)}: ${this.diffMarkedLines.size} line${this.diffMarkedLines.size === 1 ? "" : "s"} ${this.diffMarks.kind === "del" ? "removed" : "changed"}
              <button data-diffnav="-1" title="Previous change">↑</button><button data-diffnav="1" title="Next change">↓</button><button data-diffnav="0" title="Clear change markers">✕</button>
            </span>` : ""}
            <span id="file-last-changed" class="code-last-changed" style="display:none;font-size:11px;color:var(--text-muted);margin-right:8px;padding:3px 8px;background:var(--bg-tertiary);border-radius:4px;border:1px solid var(--border-color);align-items:center;gap:4px;cursor:pointer;" title="Click to view full commit history"></span>
            <button class="code-btn" id="btn-file-history" title="View commit history for this file">📜 File History</button>
            <button class="code-btn" id="btn-toggle-blame">Git Blame</button>
            <button class="code-btn" id="btn-copy-raw">Copy File</button>
            <button class="code-btn" id="btn-create-patch" title="Edit this file and generate a git format-patch">✎ Patch</button>
          </div>
        </div>
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

    // Clickable breadcrumb segments reveal directories in sidebar explorer
    containerEl.querySelectorAll(".breadcrumb-dir").forEach((el) => {
      el.onclick = () => {
        state.emit("explorer:reveal", el.dataset.path);
        toast.info(`Revealing ${el.dataset.path} in file explorer`);
      };
    });

    this.viewportEl = containerEl.querySelector("#editor-viewport");
    this.spacerEl = containerEl.querySelector("#editor-spacer");
    this.linesContainer = containerEl.querySelector("#editor-lines");
    this.minimapCanvas = containerEl.querySelector("#minimap-canvas");

    // Attach 100% copy fidelity handler
    const clipboard = new FidelityClipboard(() => this.rawLines);
    clipboard.attach(this.linesContainer);

    // Toolbar actions
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

    // Viewport scrolling
    this.viewportEl.addEventListener("scroll", () => {
      this.updateVisibleLines();
      this.updateMinimapSlider();
    });

    // Load file content from API
    await this.loadFileContent();
  }

  async loadFileContent() {
    const lastChangedEl = this.viewportEl?.closest(".code-viewer-container")?.querySelector("#file-last-changed");
    if (lastChangedEl) {
      lastChangedEl.style.display = "none";
    }
    this.linesContainer.innerHTML = `<div style="padding:20px;color:var(--text-muted);">Loading ${this.currentFilePath}...</div>`;
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
          lastChangedEl.innerHTML = `🕒 Last Changed: <strong style="color:var(--text-primary);margin-left:3px;">${dateStr}</strong><span style="opacity:0.75;margin-left:4px;">by ${authorStr}</span>`;
          lastChangedEl.title = `Last changed: ${lc.author_date_iso} by ${authorStr}\nCommit: ${lc.commit_hash_short}\nSubject: ${lc.subject}\nClick to view full history`;
        }
      }

      // Load server tokens into AST overlay
      if (Array.isArray(res.tokens)) {
        this.astOverlay.loadServerTokens(res.tokens);
      }

      this.computeDimming();

      if (this.docMode === "rendered") {
        this.renderDocPage(content);
      }

      // Update spacer height for virtual scrolling
      const totalHeight = this.rawLines.length * ROW_HEIGHT;
      this.spacerEl.style.height = `${totalHeight}px`;

      // Jump to target cursor line if specified
      if (this.targetCursorLine > 1) {
        this.viewportEl.scrollTop = Math.max(0, (this.targetCursorLine - 5) * ROW_HEIGHT);
      }

      this.updateVisibleLines();
      this.renderMinimap();
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
      const isDimmed = this.dimmedLines.has(lineNo);
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

  renderMinimap() {
    if (!this.minimapCanvas) return;
    const canvas = this.minimapCanvas;
    const ctx = canvas.getContext("2d");
    const w = canvas.parentElement.clientWidth || 90;
    const h = canvas.parentElement.clientHeight || 600;

    canvas.width = w;
    canvas.height = h;

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "rgba(139, 148, 158, 0.4)";

    const totalLines = this.rawLines.length;
    if (totalLines === 0) return;

    const lineScale = Math.min(1, h / totalLines);

    for (let i = 0; i < totalLines; i++) {
      const len = Math.min(w - 10, (this.rawLines[i] || "").length * 0.8);
      if (len > 0) {
        ctx.fillRect(4, i * lineScale, len, Math.max(1, lineScale));
      }
    }

    if (this.diffMarks) {
      ctx.fillStyle = this.diffMarks.kind === "del" ? "rgba(248, 81, 73, 0.9)" : "rgba(63, 185, 80, 0.9)";
      for (const [a, b] of this.diffMarks.ranges) {
        ctx.fillRect(0, (a - 1) * lineScale, 4, Math.max(2, (b - a + 1) * lineScale));
      }
    }
  }

  updateMinimapSlider() {
    const slider = document.getElementById("minimap-slider");
    if (!slider || !this.viewportEl) return;

    const totalHeight = this.rawLines.length * ROW_HEIGHT;
    if (totalHeight === 0) return;

    const viewportH = this.viewportEl.clientHeight || 600;
    const containerH = slider.parentElement.clientHeight || 600;

    const topPct = this.viewportEl.scrollTop / totalHeight;
    const heightPct = Math.min(1, viewportH / totalHeight);

    slider.style.top = `${topPct * containerH}px`;
    slider.style.height = `${Math.max(12, heightPct * containerH)}px`;
  }
}
