/**
 * context_menu.js - Universal Context Menu System.
 * Supports token clicks, right-click code editor actions, file explorer actions,
 * and NodeMap schematic canvas controls.
 */
import { shortcuts } from "../utils/shortcuts.js";
import { api } from "../api.js";
import { state } from "../state.js";
import { sessionManager } from "../session_manager.js";
import { toast } from "./toast.js";
import { shortRef } from "./ref_picker.js";
import { copyToClipboard } from "../utils/clipboard.js";
import { showPersonModal } from "./person_modal.js";
import { includeSymbolsPopover } from "./include_symbols_popover.js";
import { showSymbolHistoryModal, showKconfigHistoryModal } from "./symbol_history_modal.js";
import { showPatchModal } from "./patch_modal.js";
import { showFileHistoryModal } from "./file_history_modal.js";
import { openCodeLocation, openDefinition } from "../utils/definition_picker.js";
import { scopeOf, scopeTypeTarget, scopeXrefTitle, xrefBadge } from "../views/code_view/scope_model.js";
import { blameBeforeTarget } from "../views/code_view/blame_options.js";
import { referenceMenuItems } from "./file_references.js";
import { hasFeature } from "../utils/projects.js";
import { canBeVersionlessTab, isAlwaysVersionlessTab } from "../tab_routes.js";
import { urlSync } from "../url_sync.js";
import { diffSpec, historySpec, nodeSpecsForTab, sendToNodemap, sourceSpec, tokenSpec } from "../views/nodemap/nodemap_bridge.js";
import { generateImpactMap } from "../views/nodemap/impact_map.js";

class ContextMenu {
  constructor() {
    this.menuEl = null;
    this._justOpened = false;
    this._openSeq = 0;

    document.addEventListener("click", () => {
      if (this._justOpened) return;
      this.hide();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this.hide();
    });
    window.addEventListener("resize", () => this.hide());
  }

  hide() {
    this._openSeq++;
    this._justOpened = false;
    if (this._guardTimer) {
      clearTimeout(this._guardTimer);
      this._guardTimer = null;
    }
    if (this.menuEl) {
      this.menuEl.remove();
      this.menuEl = null;
    }
  }

  /**
   * Generic structured menu renderer.
   * @param {number} x Client X coordinate
   * @param {number} y Client Y coordinate
   * @param {Object} menuConfig Configuration { header?: { title, sub }, items: Array<{ label, icon, shortcut, onClick, disabled, danger, separator, when }> }
   * An item with `when` (a Promise) stays hidden until it resolves truthy, and is dropped otherwise.
   */
  renderMenu(x, y, { header, items }) {
    this.hide();
    // Ignore the bubbling click event that opened this menu if a caller did not stopPropagation()
    const seq = ++this._openSeq;
    this._justOpened = true;
    this._guardTimer = setTimeout(() => {
      this._guardTimer = null;
      if (this._openSeq === seq) this._justOpened = false;
    }, 0);
    this._guardTimer?.unref?.();

    this.menuEl = document.createElement("div");
    this.menuEl.className = "context-menu";

    if (header && (header.title || header.sub)) {
      const hEl = document.createElement("div");
      hEl.className = "context-menu-header";
      hEl.innerHTML = `
        <span class="context-menu-header-title">${this.escapeHtml(header.title || "")}</span>
        ${header.sub ? `<span class="context-menu-header-sub">${this.escapeHtml(header.sub)}</span>` : ""}
      `;
      this.menuEl.appendChild(hEl);
    }

    (items || []).forEach((item) => {
      if (item.separator) {
        const sep = document.createElement("div");
        sep.className = "context-menu-sep";
        this.menuEl.appendChild(sep);
        return;
      }

      const row = document.createElement("div");
      row.className = `context-menu-item ${item.disabled ? "disabled" : ""} ${item.danger ? "danger" : ""}`;
      row.innerHTML = `
        <span class="menu-icon">${item.icon || ""}</span>
        <span class="menu-label">${this.escapeHtml(item.label || "")}</span>
        ${item.shortcut ? `<span class="context-menu-shortcut">${this.escapeHtml(item.shortcut)}</span>` : ""}
      `;

      if (item.title) row.title = item.title;
      if (item.when) {
        row.style.display = "none";
        Promise.resolve(item.when).then((ok) => {
          if (!ok) return row.remove();
          if (!row.isConnected) return;
          row.style.display = "";
          this.clampMenu();
        }, () => row.remove());
      }
      if (!item.disabled) {
        row.addEventListener("click", (e) => {
          e.stopPropagation();
          this.hide();
          // Ctrl/Cmd+click on an item that can open a new tab does so (§5.9)
          if ((e.ctrlKey || e.metaKey) && item.onMiddleClick) item.onMiddleClick();
          else if (item.onClick) item.onClick();
        });
        if (item.onMiddleClick) {
          row.addEventListener("auxclick", (e) => {
            if (e.button !== 1) return;
            e.preventDefault();
            e.stopPropagation();
            this.hide();
            item.onMiddleClick();
          });
        }
      }
      this.menuEl.appendChild(row);
    });

    document.body.appendChild(this.menuEl);
    this._menuAnchor = { x, y };
    this.clampMenu();
  }

  /** Keep the open menu inside the viewport (again when an item appears later). */
  clampMenu() {
    if (!this.menuEl || !this._menuAnchor) return;
    const { x, y } = this._menuAnchor;
    // Dynamic viewport boundary clamping
    const rect = this.menuEl.getBoundingClientRect();
    const pad = 8;
    let posX = x;
    let posY = y;

    if (posX + rect.width > window.innerWidth - pad) {
      posX = Math.max(pad, window.innerWidth - rect.width - pad);
    }
    if (posY + rect.height > window.innerHeight - pad) {
      posY = Math.max(pad, window.innerHeight - rect.height - pad);
    }

    this.menuEl.style.left = `${posX}px`;
    this.menuEl.style.top = `${posY}px`;
  }

  /**
   * Determine whether a token represents a Kconfig symbol.
   */
  /** Cross-references of a token in the modal (menu item and Shift+F12). */
  async findReferences(tokenData) {
    if (!tokenData?.name) return;
    const v = tokenData.version || state.currentVersion;
    try {
      const xref = await api.getSymbolXref(v, tokenData.name, 250, undefined, this.positionOpts(tokenData));
      this.showXrefModal(tokenData.name, xref, v);
    } catch (err) {
      toast.error(`XRef lookup failed: ${err.message}`);
    }
  }

  /**
   * Position of a code-view token (`line` + 1-based byte `col`, set for C files only) as the symbol requests
   * take it, with the file it is in: `{fromFile, line, col}`, or `{}` when the token carries no column.
   */
  positionOpts(tokenData) {
    if (!tokenData?.col || !tokenData.filePath) return {};
    return { fromFile: tokenData.filePath, line: tokenData.line, col: tokenData.col };
  }

  /** ScopeInfo of a positioned token (the editor's pending lookup, else one of its own), or null. */
  scopeOfToken(tokenData) {
    if (!tokenData?.col) return Promise.resolve(null);
    if (!tokenData._scope) {
      const v = tokenData.version || state.currentVersion;
      const pending = tokenData.scope || api.getSymbolScope(v, tokenData.filePath, tokenData.line, tokenData.col, tokenData.name);
      tokenData._scope = Promise.resolve(pending).then(scopeOf, () => null);
    }
    return tokenData._scope;
  }

  /**
   * Menu items of a local variable, parameter or field (shown once its scope resolves): *Go to Type
   * Definition* and *Highlight Usages*.
   */
  scopeMenuItems(tokenData, v) {
    if (!tokenData?.col) return [];
    const scope = this.scopeOfToken(tokenData);
    const items = [{
      label: "Go to Type Definition",
      icon: "🧬",
      when: scope.then((sc) => Boolean(scopeTypeTarget(sc))),
      onClick: async () => {
        const t = scopeTypeTarget(await scope);
        if (t?.file) openCodeLocation(v, t.file, t.line);
        else if (t?.name && !(await openDefinition(v, t.name, { fromFile: tokenData.filePath, kind: "type" }))) toast.info(`No definition found for ${t.name}`);
      },
    }];
    if (typeof tokenData.highlight === "function") {
      items.push({
        label: "Highlight Usages",
        icon: "🖍",
        when: scope.then((sc) => Boolean(sc?.uses?.length)),
        onClick: async () => tokenData.highlight(await scope),
      });
    }
    return items;
  }

  /** Go to Definition of a token: a positioned one resolves locally (declaration in its scope) first. */
  async goToDefinition(tokenData, v, { forceNew = false } = {}) {
    const name = tokenData.name;
    const pos = this.positionOpts(tokenData);
    try {
      if (await openDefinition(v, name, { forceNew, fromFile: tokenData.filePath, line: pos.line, col: pos.col })) {
        toast.success(`Jumped to definition of ${name}`);
      } else {
        toast.info(`No definition found for ${name}`);
      }
    } catch (err) {
      toast.error(`Lookup failed: ${err.message}`);
    }
  }

  isKconfigToken(token, filePath = "", ftype = null) {
    if (!token && !filePath) return false;
    const typeId = token?.type_id ?? token?.typeId ?? 0;
    const name = token?.name || "";
    const path = filePath || token?.filePath || "";
    const fileType = ftype ?? token?.ftype;

    // 1. AST type ID in Kconfig range (88 to 118 from m_map_ast)
    if (typeId >= 88 && typeId <= 118) {
      return true;
    }

    // 2. Symbol name starts with CONFIG_ (standard in C and Kconfig files)
    if (name.startsWith("CONFIG_")) {
      return true;
    }

    // 3. File is a Kconfig file (ftype === 2 or path includes Kconfig)
    if (fileType === 2 || (path && (path.includes("Kconfig") || path.startsWith("Kconfig")))) {
      if (name && /^[A-Za-z0-9_]+$/.test(name)) {
        return true;
      }
    }

    return false;
  }

  /**
   * Normalize Kconfig symbol name by stripping CONFIG_ prefix.
   */
  getKconfigSymbolName(name) {
    if (!name) return "";
    return name.startsWith("CONFIG_") ? name.slice(7) : name;
  }

  /**
   * Open Kconfig tab focused on the given symbol.
   */
  openKconfigViewer(symbolName, version = null, options = {}) {
    const cleanSym = this.getKconfigSymbolName(symbolName);
    const v = version || state.currentVersion;
    const arch = state.kconfigArch();
    state.openTab({
      type: "kconfig",
      title: cleanSym ? `KConfig (${cleanSym})` : `KConfig (${arch})`,
      arch,
      symbol: cleanSym,
      version: v,
      forceNew: Boolean(options?.forceNew)
    });
    if (cleanSym) {
      toast.success(`Opened ${cleanSym} in Kconfig Viewer`);
    } else {
      toast.success("Opened Kconfig Viewer");
    }
  }

  /**
   * Jump to a Kconfig symbol's declaration in its Kconfig file, optionally falling back to Kconfig Viewer.
   */
  async jumpToKconfigDeclaration(symbolName, version = null, options = {}) {
    const v = version || state.currentVersion;
    const forceNew = options?.forceNew;
    try {
      const detail = await api.getSymbolDetail(v, symbolName);
      if (detail && detail.file_path) {
        state.openTab({
          type: "code",
          title: detail.file_path.split("/").pop(),
          path: detail.file_path,
          cursorLine: detail.definition?.line_s || detail.line_s || detail.line || 1,
          version: v,
          ...(forceNew !== undefined ? { forceNew: Boolean(forceNew) } : {})
        });
        toast.success(`Jumped to Kconfig declaration of ${symbolName}`);
        return true;
      }
      if (!options?.fallbackToViewer) {
        toast.info(`Could not locate declaration file for ${symbolName}`);
      }
    } catch (err) {
      if (!options?.fallbackToViewer) {
        toast.error(`Lookup failed: ${err.message}`);
      }
    }
    if (options?.fallbackToViewer) {
      this.openKconfigViewer(symbolName, v, options);
    }
    return false;
  }

  isObjectToken(token) {
    return Boolean(token && (token.isObject || token.object || token.typeClass?.includes("ast-object")));
  }

  async openObjectFile(tokenData, version = null, options = {}) {
    const v = version || tokenData?.version || state.currentVersion;
    const objectQuery = tokenData?.object || tokenData?.name || "";
    try {
      const res = await api.resolveObject(v, objectQuery, tokenData?.filePath);
      if (res && res.path) {
        state.openTab({
          type: "code",
          title: res.path.split("/").pop(),
          path: res.path,
          cursorLine: 1,
          version: v,
          ...(options?.forceNew !== undefined ? { forceNew: Boolean(options.forceNew) } : {}),
        });
        toast.success(`Opened source: ${res.path}`);
      } else {
        toast.info(`Could not resolve source for: ${objectQuery}`);
      }
    } catch (err) {
      toast.error(`Object lookup failed: ${err.message}`);
    }
  }

  /**
   * Execute the first/default action for a token directly in a tab (e.g. on middle-click).
   */
  async executeDefaultAction(tokenData, options = { forceNew: true }) {
    if (!tokenData) return;
    const tokenName = tokenData.name || "";
    const isInclude = Boolean(
      tokenData.isInclude ||
      tokenData.type_id === 78 ||
      tokenData.header ||
      tokenName.trim().startsWith("#include") ||
      tokenName.includes(".h")
    );
    const isObject = this.isObjectToken(tokenData);
    const isKconfig = this.isKconfigToken(tokenData, tokenData.filePath, tokenData.ftype);
    const v = tokenData.version || state.currentVersion;

    if (isInclude) {
      try {
        const headerQuery = tokenData.header || tokenName;
        const res = await api.resolveInclude(v, headerQuery, tokenData.ast_id, tokenData.filePath);
        if (res && res.path) {
          state.openTab({
            type: "code",
            title: res.path.split("/").pop(),
            path: res.path,
            cursorLine: 1,
            version: v,
            forceNew: Boolean(options?.forceNew)
          });
          toast.success(`Opened header: ${res.path}`);
        } else {
          toast.info(`Could not resolve header: ${headerQuery}`);
        }
      } catch (err) {
        toast.error(`Header lookup failed: ${err.message}`);
      }
    } else if (isObject) {
      await this.openObjectFile(tokenData, v, { forceNew: Boolean(options?.forceNew) });
    } else if (isKconfig && hasFeature(state.activeRepo(), "kconfig")) {
      await this.jumpToKconfigDeclaration(tokenName, v, {
        forceNew: Boolean(options?.forceNew),
        fallbackToViewer: true
      });
    } else {
      await this.goToDefinition(tokenData, v, { forceNew: Boolean(options?.forceNew) });
    }
  }

  /**
   * Left-click token quick menu (backward compatible with existing virtual_editor clicks).
   */
  show(x, y, tokenData) {
    const tokenName = tokenData.name || "";
    const isInclude = Boolean(
      tokenData.isInclude ||
      tokenData.type_id === 78 ||
      tokenData.header ||
      tokenName.trim().startsWith("#include") ||
      tokenName.includes(".h")
    );
    const isObject = this.isObjectToken(tokenData);
    const isKconfig = this.isKconfigToken(tokenData, tokenData.filePath, tokenData.ftype);
    const v = tokenData.version || state.currentVersion;

    const items = [];
    if (isInclude) {
      items.push({
        label: "Open Header File",
        icon: "📄",
        onClick: async () => {
          try {
            const headerQuery = tokenData.header || tokenName;
            const res = await api.resolveInclude(v, headerQuery, tokenData.ast_id, tokenData.filePath);
            if (res && res.path) {
              state.openTab({
                type: "code",
                title: res.path.split("/").pop(),
                path: res.path,
                cursorLine: 1,
                version: v
              });
              toast.success(`Opened header: ${res.path}`);
            } else {
              toast.info(`Could not resolve header: ${headerQuery}`);
            }
          } catch (err) {
            toast.error(`Header lookup failed: ${err.message}`);
          }
        }
      });
      items.push({
        label: "Inspect Included Symbols",
        icon: "🔍",
        onClick: async () => {
          try {
            const astId = tokenData.ast_id || 0;
            const incData = await api.getIncludeSymbols(v, astId, {
              filePath: tokenData.filePath,
              line: tokenData.line,
              header: tokenData.header || tokenName,
              tagId: tokenData.tag_id,
              arch: state.definitionContext().arch
            });
            includeSymbolsPopover.show(x, y, tokenData, incData);
          } catch (err) {
            toast.error(`Symbols lookup failed: ${err.message}`);
          }
        }
      });
    } else if (isObject) {
      items.push({
        label: "Open C File",
        icon: "📄",
        onClick: () => this.openObjectFile(tokenData, v)
      });
    } else if (isKconfig && hasFeature(state.activeRepo(), "kconfig")) {
      items.push({
        label: "Jump to Kconfig Declaration",
        icon: "📍",
        onClick: () => this.jumpToKconfigDeclaration(tokenName, v)
      });
      items.push({
        label: "Open in Kconfig Viewer",
        icon: "⚙️",
        onClick: () => {
          this.openKconfigViewer(tokenName, v);
        }
      });
    } else {
      items.push({
        label: "Go to Definition",
        icon: "🎯",
        shortcut: shortcuts.label("code.goToDefinition"),
        onClick: () => this.goToDefinition(tokenData, v)
      });
      items.push(...this.scopeMenuItems(tokenData, v));
      items.push({
        label: "Find References / XRef",
        icon: "🔗",
        shortcut: shortcuts.label("code.findReferences"),
        onClick: () => this.findReferences({ ...tokenData, version: v })
      });
      if (/^[A-Za-z_]\w*$/.test(tokenName || "")) {
        items.push({
          label: /(_operations|_ops)$/.test(tokenName) ? `Ops implementations of struct ${tokenName}` : "Ops implementations / tables using this",
          icon: "🧩",
          onClick: async () => {
            const { showOpsModal } = await import("../views/code_view/ops_modal.js");
            showOpsModal({ version: v, name: tokenName, filePath: tokenData.filePath, line: tokenData.line });
          }
        });
      }
      // Bookmarks and tours (G05)
      if (tokenData.filePath && tokenData.line) {
        const bookmark = async (target, label) => {
          const { toggleBookmark } = await import("../views/bookmarks/bookmark_store.js");
          try {
            const r = await toggleBookmark(target, { label });
            toast.success(r.added ? `Bookmarked ${label}` : `Bookmark ${label} removed`);
          } catch (err) {
            toast.error(err.message);
          }
        };
        const lineTarget = async () => {
          const { viewOf } = await import("../utils/active_views.js");
          return viewOf(state.getActiveTab()?.id)?.lineTarget?.(tokenData.line)
            || { type: "line", path: tokenData.filePath, rev: v, line: tokenData.line };
        };
        items.push({
          label: "Bookmark this line",
          icon: "🔖",
          shortcut: shortcuts.label("code.bookmark"),
          onClick: async () => bookmark(await lineTarget(), `${tokenData.filePath.split("/").pop()}:${tokenData.line}`),
        });
        if (/^[A-Za-z_]\w*$/.test(tokenName || "")) {
          items.push({
            label: `Bookmark symbol ${tokenName}`,
            icon: "🔖",
            onClick: () => bookmark({ type: "symbol", name: tokenName, rev: v }, `${tokenName}()`),
          });
        }
        const watch = async (target) => {
          const { toggleWatch } = await import("../views/watch/watch_runtime.js");
          try {
            const r = await toggleWatch(target);
            toast.success(r.added ? `Watching ${r.label}` : `Stopped watching ${r.label}`);
          } catch (err) {
            toast.error(err.message);
          }
        };
        items.push({ label: "Watch this file", icon: "🔔", onClick: () => watch({ type: "file", path: tokenData.filePath }) });
        if (/^[A-Za-z_]\w*$/.test(tokenName || "")) items.push({ label: `Watch symbol ${tokenName}`, icon: "🔔", onClick: () => watch({ type: "symbol", name: tokenName }) });
        items.push({
          label: "Add this line to a tour…",
          icon: "🧭",
          onClick: async () => {
            const { addToTourDialog } = await import("../views/bookmarks/tour_dialog.js");
            addToTourDialog(await lineTarget());
          },
        });
      }
    }

    items.push({ separator: true });

    items.push({
      label: "Inspect AST Node",
      icon: "🌳",
      onClick: async () => {
        try {
          const astId = tokenData.ast_id || 1;
          const astTree = await api.getAstTree(v, astId);
          this.showAstModal(tokenName, astTree);
        } catch (err) {
          toast.error(`AST fetch failed: ${err.message}`);
        }
      }
    });

    if (hasFeature(state.activeRepo(), "nodemap") && !isInclude && !isObject) {
      items.push({
        label: "Send to NodeMap",
        icon: "🗺️",
        onClick: () => sendToNodemap(tokenSpec({ name: tokenName, typeId: tokenData.type_id, astId: tokenData.ast_id, path: tokenData.filePath || "", version: v }), { version: v })
      });
    }

    this.renderMenu(x, y, {
      header: {
        title: tokenData.header || tokenData.object || tokenName,
        sub: isInclude ? "Include Directive" : (isObject ? "Kbuild Object" : (tokenData.filePath || "AST Token"))
      },
      items
    });
  }

  /**
   * Right-click Context Menu for Code Editor.
   * Adaptive based on whether a token, selection, or arbitrary line was clicked.
   */
  showEditorMenu(x, y, context) {
    const { token, filePath, line, selection, version, blame, ftype } = context;
    const v = version || state.currentVersion;
    const fileName = filePath ? filePath.split("/").pop() : "File";
    const headerTitle = token ? (token.header || token.object || token.name) : `${fileName}:${line || 1}`;
    const headerSub = token ? `${fileName}:${line || 1}` : (filePath || "");

    const items = [];

    // 1. Token Navigation Actions
    if (token) {
      const isKconfig = this.isKconfigToken(token, filePath, ftype ?? token.ftype);
      const isObject = this.isObjectToken(token);
      if (token.isInclude) {
        items.push({
          label: "Open Header File",
          icon: "📄",
          onClick: async () => {
            try {
              const headerQuery = token.header || token.name;
              const res = await api.resolveInclude(v, headerQuery, token.ast_id, filePath);
              if (res && res.path) {
                state.openTab({
                  type: "code",
                  title: res.path.split("/").pop(),
                  path: res.path,
                  cursorLine: 1,
                  version: v
                });
                toast.success(`Opened header: ${res.path}`);
              } else {
                toast.info(`Could not resolve header: ${headerQuery}`);
              }
            } catch (err) {
              toast.error(`Header lookup failed: ${err.message}`);
            }
          }
        });
        items.push({
          label: "Inspect Included Symbols",
          icon: "🔍",
          onClick: async () => {
            try {
              const astId = token.ast_id || 0;
              const incData = await api.getIncludeSymbols(v, astId, {
                filePath: token.filePath,
                line: token.line,
                header: token.header || token.name,
                tagId: token.tag_id,
                arch: state.definitionContext().arch
              });
              includeSymbolsPopover.show(x, y, token, incData);
            } catch (err) {
              toast.error(`Symbols lookup failed: ${err.message}`);
            }
          }
        });
      } else if (isObject) {
        items.push({
          label: "Open C File",
          icon: "📄",
          onClick: () => this.openObjectFile({ ...token, filePath: token.filePath || filePath }, v)
        });
      } else if (isKconfig && hasFeature(state.activeRepo(), "kconfig")) {
        items.push({
          label: "Jump to Kconfig Declaration",
          icon: "📍",
          onClick: () => this.jumpToKconfigDeclaration(token.name, v)
        });
        items.push({
          label: "Open in Kconfig Viewer",
          icon: "⚙️",
          onClick: () => {
            this.openKconfigViewer(token.name, v);
          }
        });
        items.push({
          label: "Kconfig History Across Releases",
          icon: "🕰",
          onClick: () => showKconfigHistoryModal(v, this.getKconfigSymbolName(token.name))
        });
      } else {
        items.push({
          label: "Go to Definition",
          icon: "🎯",
          shortcut: shortcuts.label("code.goToDefinition"),
          onClick: () => this.goToDefinition({ ...token, filePath: token.filePath || filePath }, v)
        });
        items.push(...this.scopeMenuItems(token, v));
        items.push({
          label: "Find References / XRef",
          icon: "🔗",
          shortcut: shortcuts.label("code.findReferences"),
          onClick: () => this.findReferences({ ...token, filePath: token.filePath || filePath, version: v })
        });
      }

      items.push({ separator: true });

      // 2. Deep Analysis Tools (Context-adaptive: only show struct for structs, callgraph for functions)
      const typeId = token.type_id;
      const cleanName = token.name ? token.name.replace(/^(struct|union)\s+/, "").trim() : "";
      const lineText = context.lineText || "";
      const isCFile = (ftype === 1) || Boolean(filePath && /\.(c|h|i)$/i.test(filePath));

      const isStruct =
        typeId === 27 || typeId === 28 || typeId === 29 || // C_struct, C_structdecl, C_structnotbind
        typeId === 33 || typeId === 34 || typeId === 35 || // C_union, C_uniondecl, C_unionnotbind
        typeId === 123 || typeId === 125 || typeId === 127 || // Rust_Struct, Rust_Enum, Rust_Union
        token.name?.startsWith("struct ") ||
        token.name?.startsWith("union ") ||
        (token.typeClass && token.typeClass.includes("ast-struct")) ||
        (cleanName && (cleanName.endsWith("_struct") || cleanName.endsWith("_union") || cleanName.endsWith("_s") || cleanName === "task_struct")) ||
        (cleanName && lineText && new RegExp(`\\b(struct|union)\\s+${cleanName}\\b`).test(lineText));

      const isFunction =
        typeId === 20 || typeId === 21 || typeId === 22 || typeId === 63 || typeId === 147 || // C_functionproto, C_functionprotodecl, C_functionprotnotbind, C_CallExpr, C_SyscallDef
        typeId === 128 || // Rust_Fn
        token.name?.endsWith("()") ||
        (token.typeClass && (token.typeClass.includes("ast-func") || token.typeClass.includes("tok-function"))) ||
        (cleanName && lineText && new RegExp(`\\b${cleanName}\\s*\\(`).test(lineText));

      const showStructAction = isStruct || (isCFile && cleanName && /^[a-zA-Z_]\w*$/.test(cleanName) && !isFunction && !token.isInclude && !isObject);

      const repo = state.activeRepo();
      if (showStructAction && cleanName && hasFeature(repo, "pahole")) {
        items.push({
          label: `Show Struct Layout: struct ${cleanName}`,
          icon: "📐",
          onClick: () => {
            state.openTab({
              type: "pahole",
              title: `struct ${cleanName}`,
              structName: cleanName,
              version: v
            });
            toast.success(`Opened struct layout for struct ${cleanName}`);
          }
        });
      }

      if (isFunction && cleanName && hasFeature(repo, "callgraph")) {
        items.push({
          label: `Show Callgraph: ${cleanName}()`,
          icon: "📊",
          onClick: () => {
            state.openTab({
              type: "callgraph",
              title: `Callgraph: ${cleanName}`,
              functionName: cleanName,
              version: v
            });
            toast.success(`Opened callgraph for ${cleanName}`);
          }
        });
      }

      items.push({
        label: "Inspect AST Node",
        icon: "🌳",
        onClick: async () => {
          try {
            const astId = token.ast_id || 1;
            const astTree = await api.getAstTree(v, astId);
            this.showAstModal(token.name, astTree);
          } catch (err) {
            toast.error(`AST fetch failed: ${err.message}`);
          }
        }
      });

      if (hasFeature(repo, "nodemap") && !token.isInclude && !isObject) {
        const isAggregate = token.type_id === 28 || token.type_id === 34 || /\bast-struct\b/.test(token.typeClass || "");
        items.push({
          label: isAggregate ? `Send ${token.type_id === 34 ? "union" : "struct"} ${token.name} to NodeMap` : `Send ${token.name} to NodeMap`,
          icon: "🗺️",
          onClick: () => sendToNodemap(tokenSpec({ name: token.name, typeId: isAggregate && token.type_id !== 34 ? 28 : token.type_id, astId: token.ast_id, path: filePath || "", version: v }), { version: v })
        });
        if (filePath && !isAggregate) {
          items.push({
            label: `Send Definition Slice of ${token.name} to NodeMap`,
            icon: "🗺️",
            onClick: () => sendToNodemap(sourceSpec({ path: "", version: v, symbol: token.name }), { version: v })
          });
        }
      }

      if (!isKconfig) {
        items.push({
          label: "Symbol History Across Releases",
          icon: "🕰",
          onClick: () => showSymbolHistoryModal(v, filePath, line, token.ast_id || null, token.name)
        });
        if (filePath && hasFeature(state.activeRepo(), "nodemap")) {
          items.push({
            label: `Send Symbol History of ${token.name} to NodeMap`,
            icon: "🗺️",
            onClick: () => sendToNodemap(historySpec({ mode: "symbol", path: filePath, line, astId: token.ast_id || null, name: token.name, version: v }), { version: v })
          });
        }
      }

      items.push({ separator: true });
    } else if (selection && this.isKconfigToken({ name: selection }, filePath, ftype) && hasFeature(state.activeRepo(), "kconfig")) {
      items.push({
        label: `Open in Kconfig Viewer (${this.getKconfigSymbolName(selection)})`,
        icon: "⚙️",
        onClick: () => {
          this.openKconfigViewer(selection, v);
        }
      });
      items.push({
        label: `Kconfig History Across Releases (${this.getKconfigSymbolName(selection)})`,
        icon: "🕰",
        onClick: () => showKconfigHistoryModal(v, this.getKconfigSymbolName(selection))
      });
      items.push({ separator: true });
    }

    // 3. Git & History Actions
    if (!token) {
      items.push({
        label: "Symbol History at This Line",
        icon: "🕰",
        onClick: () => showSymbolHistoryModal(v, filePath, line)
      });
    }
    // Full-text search of the revision (§5.34) for the selection, else the token
    const searchText = (selection && !selection.includes("\n") ? selection : token?.name || "").trim().slice(0, 256);
    if (searchText) {
      items.push({
        label: `Search “${searchText.length > 32 ? `${searchText.slice(0, 32)}…` : searchText}” in tree`,
        icon: "🔎",
        onClick: () => state.openTab({ type: "search", title: `Search: ${searchText.slice(0, 24)}`, query: searchText, regex: false, word: Boolean(token && !selection), version: v })
      });
    }
    const repoCtx = state.activeRepo();
    if (hasFeature(repoCtx, "patch")) {
      items.push({
        label: "Create Patch from This File…",
        icon: "✎",
        onClick: () => showPatchModal(v, filePath)
      });
    }
    if (hasFeature(repoCtx, "commits")) {
      items.push({
        label: "View Line Blame & Commit Details",
        icon: "🕒",
        onClick: () => this.showCommitDetailModal(blame, filePath, line, v)
      });
      // H9: re-blame the line at the parent of the commit that last touched it (§5.21)
      if (blame) {
        const before = blameBeforeTarget(blame, filePath);
        items.push(before ? {
          label: `Blame Before This Change (${before.path === filePath ? "" : `${before.path}, `}≈ line ${before.line})`,
          icon: "⏮",
          title: `Open ${before.path} at ${before.version.slice(0, 12)}, the parent of ${String(blame.commit_hash || "").slice(0, 12)}, with blame on (middle-click: new tab)`,
          onClick: () => openBlameBefore(before, false),
          onMiddleClick: () => openBlameBefore(before, true),
        } : { label: "Blame Before This Change (root commit)", icon: "⏮", disabled: true });
      }
    }

    // Send the file or the selected lines to the canvas (§5.3.5)
    if (filePath && hasFeature(state.activeRepo(), "nodemap")) {
      const range = context.lineRange;
      if (range && range.end > range.start) {
        items.push({
          label: `Send Lines ${range.start}–${range.end} to NodeMap`,
          icon: "🗺️",
          onClick: () => sendToNodemap(sourceSpec({ path: filePath, version: v, start: range.start, end: range.end }), { version: v })
        });
      }
      items.push({
        label: `Send ${fileName} to NodeMap`,
        icon: "🗺️",
        onClick: () => sendToNodemap(sourceSpec({ path: filePath, version: v }), { version: v })
      });
      items.push({
        label: `Send ${fileName} Blame Timeline to NodeMap`,
        icon: "🗺️",
        onClick: () => sendToNodemap(historySpec({ mode: "blame", path: filePath, line, version: v }), { version: v })
      });
    }

    items.push({ separator: true });

    // 4. Clipboard Actions
    // Links to the selected lines: permalink pinned to the commit, moving link, Markdown, external (G06)
    if (context.linkItems?.length) {
      items.push(...context.linkItems);
    }
    if (filePath && line) {
      items.push({
        label: `Copy Path (${fileName}:${line})`,
        icon: "📋",
        onClick: async () => {
          const ok = await copyToClipboard(`${filePath}:${line}`);
          if (ok) toast.success(`Copied ${filePath}:${line}`);
          else toast.error("Failed to copy path");
        }
      });
    }
    if (token && token.name) {
      items.push({
        label: `Copy Symbol (${token.name})`,
        icon: "🏷️",
        onClick: async () => {
          const ok = await copyToClipboard(token.name);
          if (ok) toast.success(`Copied "${token.name}"`);
          else toast.error("Failed to copy symbol");
        }
      });
    }
    if (selection) {
      items.push({
        label: "Copy Selected Text",
        icon: "📄",
        onClick: async () => {
          const ok = await copyToClipboard(selection);
          if (ok) toast.success("Copied selected text");
          else toast.error("Failed to copy text");
        }
      });
    }

    this.renderMenu(x, y, {
      header: { title: headerTitle, sub: headerSub },
      items
    });
  }

  /**
   * "Referenced by" of a file (GET /webapi/fs/references, §4.2): grouped entries, each opening the file at
   * its line (middle-click or Ctrl+click: a new tab). Resolves with the response (null on error).
   */
  async showFileReferencesMenu(x, y, version, path) {
    let res;
    try {
      res = await api.getFileReferences(version, path);
    } catch (err) {
      toast.error(`References of ${path}: ${err.message}`);
      return null;
    }
    const open = (p, line, forceNew) => state.openTab({
      type: "code", title: p.split("/").pop(), path: p, version, cursorLine: line, forceNew,
    });
    this.renderMenu(x, y, {
      header: { title: `⇇ Referenced by`, sub: `${path} @ ${shortRef(version)}` },
      items: referenceMenuItems(res, open),
    });
    return res;
  }

  /**
   * Right-click Context Menu for File Explorer Sidebar.
   */
  showFileTreeMenu(x, y, context) {
    const { item, isDir, path, version } = context;
    const v = version || state.currentVersion;
    const items = [];

    if (isDir) {
      items.push({
        label: "Browse Directory in Editor",
        icon: "🗂️",
        onClick: () => state.openTab({
          type: "code",
          title: item.name,
          path: path,
          version: v
        })
      });
      items.push({
        label: "Show Treemap of Folder",
        icon: "▦",
        onClick: () => state.openTab({ type: "treemap", title: `Treemap: ${item.name}`, path, version: v })
      });
      items.push({
        label: "Copy Folder Path",
        icon: "📋",
        onClick: async () => {
          const ok = await copyToClipboard(path);
          if (ok) toast.success(`Copied folder path: ${path}`);
          else toast.error("Failed to copy folder path");
        }
      });
    } else {
      items.push({
        label: "Open File",
        icon: "📄",
        onClick: () => state.openTab({
          type: "code",
          title: item.name,
          path: path,
          version: v
        })
      });
      items.push({
        label: "Copy Relative Path",
        icon: "📋",
        onClick: async () => {
          const ok = await copyToClipboard(path);
          if (ok) toast.success(`Copied path: ${path}`);
          else toast.error("Failed to copy path");
        }
      });
      items.push({
        label: "Copy Filename",
        icon: "🏷️",
        onClick: async () => {
          const ok = await copyToClipboard(item.name);
          if (ok) toast.success(`Copied filename: ${item.name}`);
          else toast.error("Failed to copy filename");
        }
      });
      items.push({
        label: "Create Patch…",
        icon: "✎",
        onClick: () => showPatchModal(v, path)
      });
      if (hasFeature(state.activeRepo(), "nodemap")) {
        items.push({ label: "Send File to NodeMap", icon: "🗺️", onClick: () => sendToNodemap(sourceSpec({ path, version: v }), { version: v }) });
      }
      items.push({ separator: true });
      items.push({
        label: "View File Git History",
        icon: "🕒",
        onClick: () => showFileHistoryModal(v, path)
      });
      items.push({
        label: "Referenced by…",
        icon: "⇇",
        title: "Files including this one, Kconfig sources, Kbuild rules and documents naming it",
        onClick: () => this.showFileReferencesMenu(x, y, v, path)
      });
    }

    this.renderMenu(x, y, {
      header: {
        title: isDir ? `📁 ${item.name}` : `📄 ${item.name}`,
        sub: path
      },
      items
    });
  }

  /**
   * Right-click menu of a commit hash (`.copy-hash`, §5.12). Left-clicking a hash copies it; this
   * menu adds the explicit copy, opening the commit, and making it the header's work commit.
   *
   * @param {Object} ctx `{ hash, version, onView }` - `onView` lets a view that already shows
   *   commits inspect the hash in place instead of opening a tab for it.
   */
  showCommitHashMenu(x, y, { hash, version, onView } = {}) {
    const full = String(hash || "");
    if (!full) return;
    const short = full.slice(0, 12);
    const v = version || state.activeVersion();
    const isWorkCommit = state.selectedVersion === full;

    this.renderMenu(x, y, {
      header: { title: "Commit", sub: short },
      items: [
        {
          label: "Copy Hash",
          icon: "📋",
          shortcut: "click",
          onClick: async () => {
            if (await copyToClipboard(full)) toast.success(`Copied ${short}… to clipboard`);
            else toast.error("Failed to copy the commit hash");
          }
        },
        { separator: true },
        {
          label: "View Commit",
          icon: "🔍",
          onClick: () => {
            if (onView && onView(full)) return;
            state.openTab({
              type: "commits",
              title: `Commit ${full.slice(0, 7)}`,
              commitId: full,
              listCollapsed: true,
              forceNew: true
            });
          }
        },
        {
          label: "Send Diff to NodeMap",
          icon: "🕸",
          onClick: () => sendToNodemap(diffSpec({ commitHash: full, version: v, repo: state.activeRepo() }), { version: v }),
        },
        ...(hasFeature(state.activeRepo(), "nodemap") ? [{
          label: "Generate Commit Impact Map",
          icon: "🗺",
          onClick: async () => {
            try {
              const repo = state.activeRepo();
              const client = typeof api.forRepo === "function" && repo ? api.forRepo(repo) : api;
              const mapCanvas = await generateImpactMap({
                api: client,
                version: v,
                repo,
                commitHash: full,
                title: `Commit Impact: ${full.slice(0, 12)}`,
              });
              state.openTab({
                type: "nodemap",
                title: `Impact: ${full.slice(0, 8)}`,
                repo,
                version: v,
                initialCanvas: mapCanvas,
                forceNew: true,
              });
              toast.success(`Generated Impact Map (${mapCanvas.nodes.length} nodes)`);
            } catch (err) {
              toast.error(`Impact Map failed: ${err.message}`);
            }
          },
        }] : []),
        {
          // Only the header selection moves: no tab changes revision until Apply / New Tab (§5.13)
          label: isWorkCommit ? "Already the Work Commit" : "Set as Work Commit",
          icon: "🎯",
          shortcut: isWorkCommit ? "" : "header",
          disabled: isWorkCommit,
          onClick: () => {
            if (state.setSelectedVersion(full)) {
              toast.success(`Work commit set to ${full.slice(0, 8)} - press Apply or New Tab to open it`);
            }
          }
        }
      ]
    });
  }

  /**
   * Right-click menu of a tab in the tabstrip (§5.24.5). The actions themselves are handed in by
   * `TabsComponent`, which owns tab transfers; this only lays out the menu and the session list.
   */
  showTabMenu(x, y, { tab, onOpenInNewWindow, onPushToSession, onApplyCurrentTag, onGetTabTag, onClearTag }) {
    const sessions = sessionManager
      .getSessionsIndex()
      .filter((s) => s.id !== sessionManager.activeSessionId);

    // Header selection vs. this tab's own revision (§5.24.5): either side can be copied to the other
    const selected = state.selectedVersion || "";
    const alwaysVersionless = isAlwaysVersionlessTab(tab.type);
    const canClear = canBeVersionlessTab(tab.type) && !alwaysVersionless && Boolean(tab.version);
    const tabRef = alwaysVersionless
      ? ""
      : (canBeVersionlessTab(tab.type) ? (tab.version || "") : (tab.version || state.currentVersion || ""));
    const otherRepo = Boolean(state.currentRepo) && state.repoOf(tab) !== state.currentRepo;
    const same = !selected || !tabRef || selected === tabRef;
    const applyDisabled = alwaysVersionless || otherRepo || !selected || (Boolean(tabRef) && selected === tabRef);
    const applyShortcut = alwaysVersionless
      ? "versionless"
      : (otherRepo ? "other project" : (tabRef && selected === tabRef ? "already on it" : ""));
    const getDisabled = alwaysVersionless || !tabRef || selected === tabRef;
    const getShortcut = alwaysVersionless || !tabRef ? "no tag" : (same ? "already selected" : "");

    const items = [
      {
        label: `Apply Current Tag${selected && !alwaysVersionless ? ` (${shortRef(selected)})` : ""}`,
        icon: "⤓",
        shortcut: applyShortcut,
        disabled: applyDisabled,
        onClick: () => onApplyCurrentTag && onApplyCurrentTag()
      },
      {
        label: `Get Tab Tag${tabRef ? ` (${shortRef(tabRef)})` : ""}`,
        icon: "⤒",
        shortcut: getShortcut,
        disabled: getDisabled,
        onClick: () => onGetTabTag && onGetTabTag()
      },
      ...(canClear ? [{
        label: "Clear Tag (Make Tag-less)",
        icon: "🏷️",
        shortcut: shortRef(tabRef),
        onClick: () => onClearTag && onClearTag()
      }] : []),
      ...(tab.type !== "nodemap" && hasFeature(state.repoOf(tab), "nodemap") ? [
        { separator: true },
        ...(nodeSpecsForTab(tab, null).length ? [{
          label: "Send to NodeMap",
          icon: "🗺️",
          onClick: () => sendToNodemap(nodeSpecsForTab(tab, (t) => urlSync.paramsForTab(t).toString()), { repo: state.repoOf(tab), version: tab.version || "" })
        }] : []),
        {
          label: "Embed Live Tab in NodeMap",
          icon: "🗔",
          onClick: () => sendToNodemap(nodeSpecsForTab(tab, (t) => urlSync.paramsForTab(t).toString(), { live: true }), { repo: state.repoOf(tab), version: tab.version || "" })
        },
      ] : []),
      { separator: true },
      {
        label: "Open on New Browser Tab",
        icon: "🗗",
        shortcut: "moves it",
        onClick: () => onOpenInNewWindow && onOpenInNewWindow()
      },
      { separator: true },
      {
        label: sessions.length > 0 ? "Push to Another Session…" : "No Other Session to Push To",
        icon: "↪",
        disabled: sessions.length === 0,
        onClick: () => this.showTabPushMenu(x, y, { tab, sessions, onPushToSession })
      }
    ];

    this.renderMenu(x, y, {
      header: { title: tab.title || "Tab", sub: tab.path || tab.type || "" },
      items
    });
  }

  /** Second step of the tab menu: which session the tab is pushed to (§5.24.5). */
  showTabPushMenu(x, y, { tab, sessions, onPushToSession }) {
    const items = sessions.map((s) => {
      const live = sessionManager.isSessionActiveInOtherWindow(s.id);
      return {
        // A session open in another window takes the tab live; one no window holds gets it written
        // into its stored record, and shows it the next time it is opened.
        label: s.name || s.id,
        icon: live ? "🟢" : "💤",
        shortcut: live ? "open" : "saved",
        onClick: () => onPushToSession && onPushToSession(s)
      };
    });

    this.renderMenu(x, y, {
      header: {
        title: `Push '${tab.title || "tab"}' to…`,
        sub: "The tab leaves this session"
      },
      items
    });
  }

  /**
   * Right-click menu of the NodeMap canvas (§5.3): a node (`node`, optional struct row `fieldKey`), a
   * connection (`edgeId`) or the background (`isCanvas`, canvas point `at`). Acts through `controller`.
   */
  showNodeMapMenu(x, y, context) {
    const { node, fieldKey, edgeId, isCanvas, at, controller } = context;
    if (!controller) return;
    const items = [];
    if (node) {
      const d = node.data || {};
      const name = d.name || "";
      const v = node.version || state.activeVersion();
      const title = node.customTitle || node.title;
      items.push({ label: "Rename…", icon: "✎", onClick: () => controller.beginTitleEdit(node.id) });
      const followType = node.type === "struct" && fieldKey ? controller.followableTypeFor?.(node.id, fieldKey) : null;
      if (followType) items.push({ label: `Follow Type (${followType})`, icon: "↣", onClick: () => controller.followStructType(node.id, fieldKey) });
      if (fieldKey && node.type === "struct" && controller.canvas?.settings?.editMode) {
        items.push({ label: "Set Field Lock Annotation…", icon: "🔒", onClick: () => controller.promptFieldLock?.(node, fieldKey) });
      }
      if (fieldKey) items.push({ label: "Attach Node to This Member…", icon: "🔗", onClick: () => controller.promptAttachNode(node.id, fieldKey) });
      else if (node.type !== "group" && node.type !== "note") items.push({ label: "Attach Connected Node…", icon: "🔗", onClick: () => controller.promptAttachNode(node.id) });
      if (node.type === "symbol") items.push({ label: d.expanded ? "Collapse Members" : "Expand Members", icon: d.expanded ? "🔼" : "🔽", onClick: () => controller.toggleExpandNode(node.id) });
      if (node.type === "symbol" && d.kind === "func" && hasFeature(node.repo || state.activeRepo(), "callgraph")) {
        items.push({ label: "+ Callers to NodeMap", icon: "⤴", onClick: () => controller.expandCallgraph?.(node.id, "callers") });
        items.push({ label: "+ Callees to NodeMap", icon: "⤵", onClick: () => controller.expandCallgraph?.(node.id, "callees") });
        items.push({ label: "Find Call Path to…", icon: "🛣", onClick: () => controller.promptCallPath?.(node.id) });
      }
      if (node.type === "mail" && !d.single) items.push({ label: "Expand Thread to Nodes", icon: "⇶", onClick: () => controller.expandThreadToNodes(node.id) });
      if (node.type === "kconfig") items.push({ label: "Expand Relations", icon: "⇶", onClick: () => controller.expandKconfigRelations(node.id) });
      const typeCount = node.type === "struct" ? controller.expandableTypesOf?.(node.id)?.length || 0 : 0;
      if (typeCount) items.push({ label: `Expand Types (${typeCount})`, icon: "⇶", onClick: () => controller.expandStructTypes(node.id) });
      if (node.type === "nodemap") {
        const target = d.canvasId || "";
        const status = controller.nestedMaps?.[target]?.status;
        items.push({ label: "Open NodeMap", icon: "↘", disabled: !target || !status || status === "missing", onClick: () => controller.openNested?.(node.id) });
        items.push({ label: "Choose Another NodeMap…", icon: "🗺", onClick: () => controller.chooseNestedTarget?.(node.id) });
      }
      if (node.type === "group") {
        items.push({
          label: d.subCanvas ? "Enter Sub-Map Portal" : "Convert Group to Sub-Map Portal",
          icon: "🌀",
          onClick: () => controller.enterSubMap?.(node.id),
        });
      }
      if (["struct", "source", "man", "tab", "symbol", "diff", "mail", "kconfig", "maintainer", "cve", "kapi", "doc", "history"].includes(node.type)) items.push({ label: "Open as Tab", icon: "↗", onClick: () => controller.openNodeAsTab(node.id) });
      if ((node.type === "struct" || node.type === "symbol") && name) {
        items.push({ separator: true });
        items.push({
          label: "Go to Definition",
          icon: "🎯",
          onClick: async () => {
            try {
              if (!(await openDefinition(v, name))) toast.info(`No definition found for ${name}`);
            } catch (err) {
              toast.error(`Lookup failed: ${err.message}`);
            }
          }
        });
        const repo = node.repo || state.activeRepo();
        if (node.type === "struct" && hasFeature(repo, "pahole")) {
          items.push({ label: `Show Struct Layout: ${name}`, icon: "📐", onClick: () => state.openTab({ type: "pahole", title: `struct ${name}`, structName: name, version: v }) });
        }
        if (node.type === "symbol" && d.kind === "func" && hasFeature(repo, "callgraph")) {
          items.push({ label: `Show Callgraph: ${name}()`, icon: "📊", onClick: () => state.openTab({ type: "callgraph", title: `Callgraph: ${name}`, functionName: name, version: v }) });
        }
      }
      items.push({ separator: true });
      if (controller.groupSelectionIntoNested) items.push({ label: "Group Selection into Nested NodeMap…", icon: "🗺", onClick: () => controller.groupSelectionIntoNested() });
      if (controller.selected?.size > 1) items.push({ label: "Group Selection", icon: "▭", onClick: () => controller.addGroup() });
      items.push({ label: controller.selected?.size > 1 ? `Duplicate ${controller.selected.size} Nodes` : "Duplicate", icon: "⧉", shortcut: "Ctrl+D", onClick: () => controller.duplicateNode(node.id) });
      items.push({ label: controller.selected?.size > 1 ? `Copy ${controller.selected.size} Nodes` : "Copy", icon: "📋", shortcut: "Ctrl+C", onClick: () => controller.copySelection() });
      items.push({ label: "Center / Focus Node", icon: "🎯", onClick: () => controller.centerOnNode(node.id) });
      items.push({ label: controller.selected?.size > 1 ? `Remove ${controller.selected.size} Nodes` : "Remove Node", icon: "🗑️", danger: true, onClick: () => controller.deleteSelection() });
      this.renderMenu(x, y, { header: { title: title || "Node", sub: `NodeMap [${node.type}]` }, items });
    } else if (edgeId) {
      const edge = controller.canvas?.edges?.find?.((x) => x.id === edgeId);
      items.push({ label: "Edit Label…", icon: "✎", onClick: () => controller.beginEdgeLabelEdit(edgeId) });
      if (at) items.push({ label: "Add Bend Waypoint Here", icon: "◆", onClick: () => controller.addEdgeWaypointAt?.(edgeId, at) });
      if (edge?.callSite?.path) {
        items.push({
          label: `Open Call Site (${edge.callSite.path.split("/").pop()}:${edge.callSite.line || 1})`,
          icon: "↗",
          onClick: () => controller.openCallSiteSource?.(edge),
        });
      }
      items.push({ label: "Remove Connection", icon: "🗑️", danger: true, onClick: () => controller.removeEdge(edgeId) });
      this.renderMenu(x, y, { header: { title: "Connection", sub: "NodeMap" }, items });
    } else if (isCanvas) {
      items.push({ label: "Add Node…", icon: "➕", onClick: () => controller.showAddMenu(x, y) });
      items.push({ label: "Build Patch / Commit Impact Map…", icon: "🗺", onClick: () => controller.openImpactMapDialog?.() });
      items.push({ label: "Canvas Library & Templates…", icon: "📚", onClick: () => controller.openLibraryDialog?.("library") });
      if (at) items.push({ label: "Add Note Here", icon: "📝", onClick: () => controller.addNoteAt(Math.round(at.x), Math.round(at.y)) });
      items.push({ label: "Add Group", icon: "▭", onClick: () => controller.addGroup() });
      if (controller.insertExistingNodemap) {
        items.push({ label: "Insert Existing NodeMap Here…", icon: "🗺", onClick: () => controller.insertExistingNodemap(at ? { x: Math.round(at.x), y: Math.round(at.y) } : null) });
        items.push({ label: "New Nested NodeMap Here…", icon: "➕", onClick: () => controller.createNestedNodemap(at ? { x: Math.round(at.x), y: Math.round(at.y) } : null) });
      }
      items.push({ label: "Paste Nodes", icon: "📋", shortcut: "Ctrl+V", disabled: !controller.readClipboard?.(), onClick: () => controller.pasteClipboard() });
      items.push({ separator: true });
      items.push({ label: "Fit to View", icon: "⤢", onClick: () => controller.fitView() });
      items.push({ label: "Reset View & Zoom (100%)", icon: "🔄", onClick: () => controller.resetTransform() });
      items.push({ separator: true });
      items.push({
        label: "Clear Entire Canvas…",
        icon: "🧹",
        danger: true,
        disabled: !controller.canvas?.nodes?.length,
        onClick: () => {
          const n = controller.canvas.nodes.length;
          if (confirm(`Remove all ${n} nodes and their connections from this canvas? Export it as JSON first to keep a copy.`)) {
            controller.clearAll();
            toast.info("Cleared NodeMap canvas");
          }
        }
      });
      this.renderMenu(x, y, { header: { title: "NodeMap", sub: "Canvas" }, items });
    }
  }

  /**
   * Commit Detail Quick Popover Modal.
   */
  async showCommitDetailModal(blame, filePath, line, version) {
    const v = version || state.currentVersion;
    let commitHash = blame?.commit_hash;

    if (!commitHash && filePath) {
      try {
        toast.info(`Fetching blame for ${filePath}:${line || 1}...`);
        const fileBlame = await api.getFileBlame(v, filePath);
        if (Array.isArray(fileBlame?.lines)) {
          const targetLine = parseInt(line, 10) || 1;
          const lineBlame = fileBlame.lines.find((b) => b.line === targetLine);
          if (lineBlame) {
            commitHash = lineBlame.commit_hash;
            blame = lineBlame;
          }
        }
      } catch (err) {
        console.warn("Blame lookup error:", err);
      }
    }

    if (!commitHash) {
      toast.info(`No commit history available for ${filePath}:${line || 1}`);
      return;
    }

    let commitDetail = null;
    try {
      commitDetail = await api.getCommitDetail(v, commitHash);
    } catch (err) {
      console.warn("Commit detail fetch error:", err);
      commitDetail = {
        commit_hash: commitHash,
        subject: blame?.subject || `Commit ${commitHash.substring(0, 7)}`,
        author_name: blame?.author_name || "Unknown",
        date: blame?.author_date || blame?.date || ""
      };
    }

    const backdrop = document.createElement("div");
    backdrop.className = "palette-backdrop";
    backdrop.onclick = () => backdrop.remove();

    const box = document.createElement("div");
    box.className = "commit-popover-box";
    box.onclick = (e) => e.stopPropagation();

    const shortHash = commitHash.substring(0, 8);
    const author = commitDetail.author_name || blame?.author_name || "Unknown";
    const dateStr = commitDetail.author_date_iso || commitDetail.author_date || commitDetail.date || (blame?.author_date || blame?.date ? new Date((blame.author_date || blame.date) * 1000).toLocaleString() : "Unknown");
    const subject = commitDetail.subject || blame?.subject || "No commit message";
    const body = commitDetail.message || commitDetail.body || "";
    const files = commitDetail.touched_files || commitDetail.files || [];

    box.innerHTML = `
      <div style="padding:14px 16px;border-bottom:1px solid var(--border-color);display:flex;justify-content:space-between;align-items:flex-start;">
        <div>
          <div style="font-size:var(--fs-11, 11px);color:var(--text-muted);display:flex;align-items:center;gap:8px;">
            <span>Line ${line || 1} modified in:</span>
            <code style="background:var(--bg-tertiary);padding:2px 6px;border-radius:4px;color:var(--accent-blue);font-family:var(--font-mono);">${shortHash}</code>
          </div>
          <div style="font-size:var(--fs-15, 15px);font-weight:600;color:var(--text-primary);margin-top:6px;line-height:1.3;">
            ${this.escapeHtml(subject)}
          </div>
        </div>
        <button id="close-commit-btn" style="color:var(--text-muted);font-size:var(--fs-18, 18px);background:none;border:none;cursor:pointer;padding:0 4px;">&times;</button>
      </div>

      <div style="padding:10px 16px;background:var(--bg-primary);border-bottom:1px solid var(--border-color);display:flex;gap:20px;font-size:var(--fs-11, 11px);color:var(--text-secondary);">
        <div><strong style="color:var(--text-primary);">Author:</strong> <span class="commit-popover-author-link" style="color:var(--accent-blue);cursor:pointer;text-decoration:underline;" title="Click to view developer profile">${this.escapeHtml(author)}</span></div>
        <div><strong style="color:var(--text-primary);">Date:</strong> ${this.escapeHtml(dateStr)}</div>
      </div>

      ${body ? `
        <div style="max-height:140px;overflow-y:auto;padding:12px 16px;font-size:var(--fs-12, 12px);line-height:1.5;color:var(--text-secondary);white-space:pre-wrap;border-bottom:1px solid var(--border-color);background:var(--bg-secondary);">
          ${this.escapeHtml(body)}
        </div>
      ` : ""}

      <div style="padding:12px 16px;display:flex;justify-content:space-between;align-items:center;background:var(--bg-secondary);">
        <div style="font-size:var(--fs-11, 11px);color:var(--text-muted);">
          ${files.length > 0 ? `${files.length} touched files` : `${this.escapeHtml(filePath || '')}`}
        </div>
        <div style="display:flex;gap:8px;">
          <button id="copy-hash-btn" class="code-btn" style="padding:4px 10px;font-size:var(--fs-11, 11px);">Copy Hash</button>
          <button id="open-commit-tab-btn" class="code-btn active" style="padding:4px 12px;font-size:var(--fs-11, 11px);">Open Full Diff in Commits Tab &rarr;</button>
        </div>
      </div>
    `;

    backdrop.appendChild(box);
    document.body.appendChild(backdrop);

    const authorLink = box.querySelector(".commit-popover-author-link");
    if (authorLink) {
      authorLink.onclick = () => {
        showPersonModal(state.activeVersion(), author);
      };
    }

    box.querySelector("#close-commit-btn").onclick = () => backdrop.remove();
    box.querySelector("#copy-hash-btn").onclick = async () => {
      const ok = await copyToClipboard(commitHash);
      if (ok) toast.success(`Copied commit hash: ${shortHash}`);
      else toast.error("Failed to copy commit hash");
    };
    const openCommitBtn = box.querySelector("#open-commit-tab-btn");
    openCommitBtn.onclick = () => {
      state.openTab({
        type: "commits",
        title: `Commit ${shortHash}`,
        commitId: commitHash,
        listCollapsed: true
      });
      backdrop.remove();
    };
    openCommitBtn.addEventListener("auxclick", (e) => {
      if (e.button === 1) {
        e.preventDefault();
        e.stopPropagation();
        state.openTab({
          type: "commits",
          title: `Commit ${shortHash}`,
          commitId: commitHash,
          listCollapsed: true,
          forceNew: true
        });
      }
    });
  }

  formatTypeName(typeName) {
    if (!typeName) return "Definition";
    if (typeName === "C_enumequal" || typeName === "Enum_Equal") return "EnumConstant";
    return typeName;
  }

  showXrefModal(name, xref, version = state.activeVersion()) {
    const backdrop = document.createElement("div");
    backdrop.className = "palette-backdrop";
    backdrop.onclick = () => backdrop.remove();

    const box = document.createElement("div");
    box.className = "palette-box";
    box.style.width = "calc(540px * var(--ui-layout-scale, 1))";
    box.onclick = (e) => e.stopPropagation();

    const allDefs = (xref && (xref.definitions || (xref.definition ? [xref.definition] : []))) || [];
    // A definition outside this revision's tree sits at no path here, so it can be neither shown nor jumped to
    const defs = allDefs.filter((d) => d && d.file);
    const refs = (xref && (xref.references || [])) || [];
    // A local variable, parameter or field (§5.6): its in-scope uses, each with its access
    const scope = scopeOf(xref);
    const title = scope ? scopeXrefTitle(scope, name) : `Cross-References: ${name}`;
    const noDefsNote = xref && xref.definition_unindexed
      ? "Defined in other indexed revisions, but this revision's copy of the file was never parsed &mdash; the usages below still apply."
      : "No direct definition indexed for this symbol.";

    const defsHtml = defs.length > 0
      ? defs.map((d) => `
          <div class="xref-def-card" data-path="${this.escapeHtml(d.file)}" data-line="${d.line_s || 1}">
            <div>
              <div style="font-weight:600;color:var(--text-primary);display:flex;align-items:center;gap:6px;">
                <span>${this.escapeHtml(name)}</span>
                <span class="xref-def-badge">${this.escapeHtml(this.formatTypeName(d.type_name))}</span>
              </div>
              <div style="font-size:var(--fs-11, 11px);color:var(--text-muted);font-family:var(--font-mono);margin-top:3px;">
                ${this.escapeHtml(d.file)}:${d.line_s || 1}
              </div>
            </div>
            <span style="font-size:var(--fs-11, 11px);color:var(--accent-purple);font-weight:500;">Jump &rarr;</span>
          </div>
        `).join("")
      : `<div style="font-size:var(--fs-11, 11px);color:var(--text-muted);margin-bottom:12px;font-style:italic;">${noDefsNote}</div>`;

    const refsHtml = refs.length > 0
      ? refs.map((r) => `
          <div class="palette-item xref-item" data-path="${this.escapeHtml(r.file)}" data-line="${r.line}">
            <div>
              <div style="font-weight:500;color:var(--text-primary);">${this.escapeHtml(r.caller || r.file)}</div>
              <div class="palette-item-desc">${this.escapeHtml(r.file)}:${r.line}${r.char_s ? `:${Number(r.char_s) || ""}` : ""}</div>
            </div>
            <span class="xref-badge ${xrefBadge(r).cls}">${this.escapeHtml(xrefBadge(r).label)}</span>
          </div>
        `).join("")
      : `<div style="color:var(--text-muted);padding:10px 0;">
          <div>No external cross-references indexed.</div>
        </div>`;

    box.innerHTML = `
      <div style="padding:12px 16px;border-bottom:1px solid var(--border-color);display:flex;justify-content:space-between;align-items:center;">
        <span style="font-weight:bold;color:var(--accent-blue);">${this.escapeHtml(title)}</span>
        <button id="close-xref-btn" style="color:var(--text-muted);font-size:var(--fs-18, 18px);background:none;border:none;cursor:pointer;padding:0 4px;">&times;</button>
      </div>

      <div style="max-height:420px;overflow-y:auto;padding:12px 16px;">
        <div class="settings-section-title">${scope ? "Declaration" : `Definition${defs.length > 1 ? `s (${defs.length})` : ""}`}</div>
        ${defsHtml}

        <div class="settings-section-title" style="margin-top:8px;">${scope ? "Uses in scope" : "References &amp; Usages"} (${refs.length})</div>
        <div class="xref-list">
          ${refsHtml}
        </div>
      </div>
    `;

    backdrop.appendChild(box);
    document.body.appendChild(backdrop);

    box.querySelector("#close-xref-btn").onclick = () => backdrop.remove();

    box.querySelectorAll(".xref-def-card, .xref-item").forEach((el) => {
      el.onclick = () => {
        // A line of the active code tab's own file is revealed in place
        openCodeLocation(version, el.dataset.path, parseInt(el.dataset.line || "1", 10));
        backdrop.remove();
      };
      el.addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          e.stopPropagation();
          state.openTab({
            type: "code",
            title: el.dataset.path.split("/").pop(),
            path: el.dataset.path,
            cursorLine: parseInt(el.dataset.line || "1", 10),
            version,
            forceNew: true
          });
          // Do not close backdrop - keeps modal open for queuing multiple tabs
        }
      });
    });
  }

  showAstModal(name, astTree) {
    const backdrop = document.createElement("div");
    backdrop.className = "palette-backdrop";
    backdrop.onclick = () => backdrop.remove();

    const modal = document.createElement("div");
    modal.className = "ast-inspector-modal";
    modal.onclick = (e) => e.stopPropagation();

    modal.innerHTML = `
      <div class="ast-modal-header">
        <span>AST Structure: ${this.escapeHtml(name)}</span>
        <button id="close-ast-btn" style="color:var(--text-muted);font-size:var(--fs-16, 16px);">&times;</button>
      </div>
      <div class="ast-tree-content" id="ast-tree-root"></div>
    `;

    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);

    modal.querySelector("#close-ast-btn").onclick = () => backdrop.remove();

    const rootContainer = modal.querySelector("#ast-tree-root");
    this.renderAstNode(rootContainer, astTree.node || astTree);
  }

  renderAstNode(container, node, depth = 0) {
    if (!node) return;

    const row = document.createElement("div");
    row.className = "ast-node-row";
    row.style.paddingLeft = `${depth * 16}px`;

    const hasChildren = Array.isArray(node.children) && node.children.length > 0;
    const chevron = document.createElement("span");
    chevron.textContent = hasChildren ? "▶ " : "• ";
    chevron.style.color = "var(--text-muted)";
    chevron.style.cursor = hasChildren ? "pointer" : "default";

    const label = document.createElement("span");
    label.textContent = `${node.type_name || node.type || "ASTNode"} [id:${node.ast_id || 0}] ${node.spelling || node.name || ""}`;
    label.style.color = node.type_name === "FunctionDecl" ? "var(--accent-purple)" : "var(--text-primary)";

    row.appendChild(chevron);
    row.appendChild(label);

    if (Array.isArray(node.attributes) && node.attributes.length > 0) {
      const attrSpan = document.createElement("span");
      attrSpan.style.color = "var(--text-muted)";
      attrSpan.style.fontSize = "var(--fs-11, 11px)";
      attrSpan.style.marginLeft = "6px";
      attrSpan.textContent = "{" + node.attributes.map(a => `${a.name}: ${a.val}`).join(", ") + "}";
      row.appendChild(attrSpan);
    }

    container.appendChild(row);

    if (hasChildren) {
      const childContainer = document.createElement("div");
      childContainer.style.display = "none";
      container.appendChild(childContainer);

      let expanded = false;
      const toggle = () => {
        expanded = !expanded;
        chevron.textContent = expanded ? "▼ " : "▶ ";
        childContainer.style.display = expanded ? "block" : "none";
        if (expanded && childContainer.children.length === 0) {
          node.children.forEach((c) => this.renderAstNode(childContainer, c, depth + 1));
        }
      };

      chevron.onclick = toggle;
      label.onclick = toggle;
    }
  }

  escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
}

export const contextMenu = new ContextMenu();

/** Open the file as it was before a blamed change, with blame on (H9, §5.21). */
function openBlameBefore(target, forceNew) {
  state.openTab({
    type: "code",
    title: target.path.split("/").pop(),
    path: target.path,
    version: target.version,
    cursorLine: target.line,
    blame: true,
    forceNew,
  });
}
