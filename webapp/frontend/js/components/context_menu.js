/**
 * context_menu.js - Universal Context Menu System.
 * Supports token clicks, right-click code editor actions, file explorer actions,
 * and NodeMap schematic canvas controls.
 */
import { api } from "../api.js";
import { state } from "../state.js";
import { toast } from "./toast.js";
import { copyToClipboard } from "../utils/clipboard.js";
import { showPersonModal } from "./person_modal.js";
import { includeSymbolsPopover } from "./include_symbols_popover.js";

class ContextMenu {
  constructor() {
    this.menuEl = null;
    this.activeToken = null;

    document.addEventListener("click", () => this.hide());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this.hide();
    });
    window.addEventListener("resize", () => this.hide());
  }

  hide() {
    if (this.menuEl) {
      this.menuEl.remove();
      this.menuEl = null;
    }
  }

  /**
   * Generic structured menu renderer.
   * @param {number} x Client X coordinate
   * @param {number} y Client Y coordinate
   * @param {Object} menuConfig Configuration { header?: { title, sub }, items: Array<{ label, icon, shortcut, onClick, disabled, danger, separator }> }
   */
  renderMenu(x, y, { header, items }) {
    this.hide();

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

      if (!item.disabled) {
        row.addEventListener("click", (e) => {
          e.stopPropagation();
          this.hide();
          if (item.onClick) item.onClick();
        });
      }
      this.menuEl.appendChild(row);
    });

    document.body.appendChild(this.menuEl);

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
    state.openTab({
      type: "kconfig",
      title: cleanSym ? `KConfig (${cleanSym})` : "KConfig (x86)",
      arch: "x86",
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
    } else if (isKconfig) {
      this.openKconfigViewer(tokenName, v, options);
    } else {
      try {
        const detail = await api.getSymbolDetail(v, tokenName);
        if (detail && detail.file_path) {
          state.openTab({
            type: "code",
            title: detail.file_path.split("/").pop(),
            path: detail.file_path,
            cursorLine: detail.definition?.line_s || detail.line_s || detail.line || 1,
            version: v,
            forceNew: Boolean(options?.forceNew)
          });
          toast.success(`Jumped to definition of ${tokenName}`);
        } else {
          toast.info(`No definition found for ${tokenName}`);
        }
      } catch (err) {
        toast.error(`Lookup failed: ${err.message}`);
      }
    }
  }

  /**
   * Left-click token quick menu (backward compatible with existing virtual_editor clicks).
   */
  show(x, y, tokenData) {
    this.activeToken = tokenData;
    const tokenName = tokenData.name || "";
    const isInclude = Boolean(
      tokenData.isInclude ||
      tokenData.type_id === 78 ||
      tokenData.header ||
      tokenName.trim().startsWith("#include") ||
      tokenName.includes(".h")
    );
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
              tagId: tokenData.tag_id
            });
            includeSymbolsPopover.show(x, y, tokenData, incData);
          } catch (err) {
            toast.error(`Symbols lookup failed: ${err.message}`);
          }
        }
      });
    } else if (isKconfig) {
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
        shortcut: "F12",
        onClick: async () => {
          try {
            const detail = await api.getSymbolDetail(v, tokenName);
            if (detail && detail.file_path) {
              state.openTab({
                type: "code",
                title: detail.file_path.split("/").pop(),
                path: detail.file_path,
                cursorLine: detail.definition?.line_s || detail.line_s || detail.line || 1,
                version: v
              });
              toast.success(`Jumped to definition of ${tokenName}`);
            } else {
              toast.info(`No definition found for ${tokenName}`);
            }
          } catch (err) {
            toast.error(`Lookup failed: ${err.message}`);
          }
        }
      });
      items.push({
        label: "Find References / XRef",
        icon: "🔗",
        shortcut: "Shift+F12",
        onClick: async () => {
          try {
            const xref = await api.getSymbolXref(v, tokenName, 250);
            this.showXrefModal(tokenName, xref);
          } catch (err) {
            toast.error(`XRef lookup failed: ${err.message}`);
          }
        }
      });
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

    items.push({
      label: "Open in NodeMap",
      icon: "🗺️",
      onClick: () => {
        state.openTab({
          type: "nodemap",
          title: "NodeMap Canvas",
          version: v,
          initialNode: {
            name: tokenData.header || tokenName,
            type: isInclude ? "include" : (tokenData.type_id || "symbol"),
            astId: tokenData.ast_id
          }
        });
        toast.success(`Opened ${tokenName} in NodeMap`);
      }
    });

    this.renderMenu(x, y, {
      header: {
        title: tokenData.header || tokenName,
        sub: isInclude ? "Include Directive" : (tokenData.filePath || "AST Token")
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
    const headerTitle = token ? (token.header || token.name) : `${fileName}:${line || 1}`;
    const headerSub = token ? `${fileName}:${line || 1}` : (filePath || "");

    const items = [];

    // 1. Token Navigation Actions
    if (token) {
      const isKconfig = this.isKconfigToken(token, filePath, ftype ?? token.ftype);
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
                tagId: token.tag_id
              });
              includeSymbolsPopover.show(x, y, token, incData);
            } catch (err) {
              toast.error(`Symbols lookup failed: ${err.message}`);
            }
          }
        });
      } else if (isKconfig) {
        items.push({
          label: "Open in Kconfig Viewer",
          icon: "⚙️",
          onClick: () => {
            this.openKconfigViewer(token.name, v);
          }
        });
      } else {
        items.push({
          label: "Go to Definition",
          icon: "🎯",
          shortcut: "F12",
          onClick: async () => {
            try {
              const detail = await api.getSymbolDetail(v, token.name);
              if (detail && detail.file_path) {
                state.openTab({
                  type: "code",
                  title: detail.file_path.split("/").pop(),
                  path: detail.file_path,
                  cursorLine: detail.definition?.line_s || detail.line_s || detail.line || 1,
                  version: v
                });
                toast.success(`Jumped to definition of ${token.name}`);
              } else {
                toast.info(`No definition found for ${token.name}`);
              }
            } catch (err) {
              toast.error(`Lookup failed: ${err.message}`);
            }
          }
        });
        items.push({
          label: "Find References / XRef",
          icon: "🔗",
          shortcut: "Shift+F12",
          onClick: async () => {
            try {
              const xref = await api.getSymbolXref(v, token.name, 250);
              this.showXrefModal(token.name, xref);
            } catch (err) {
              toast.error(`XRef lookup failed: ${err.message}`);
            }
          }
        });
      }

      items.push({ separator: true });

      // 2. Deep Analysis Tools (Context-adaptive: only show struct for structs, callgraph for functions)
      const typeId = token.type_id;
      const isStruct = typeId === 6 || typeId === 7 || typeId === 8 || token.name?.startsWith("struct ") || token.name?.startsWith("union ") || (token.typeClass && token.typeClass.includes("ast-struct"));
      if (isStruct) {
        items.push({
          label: `Show Struct Layout: ${token.name}`,
          icon: "📐",
          onClick: () => {
            state.openTab({
              type: "pahole",
              title: `struct ${token.name}`,
              structName: token.name,
              version: v
            });
            toast.success(`Opened struct layout for ${token.name}`);
          }
        });
      }

      const isFunction = typeId === 5 || typeId === 21 || typeId === 10 || typeId === 74 || (token.typeClass && (token.typeClass.includes("ast-function") || token.typeClass.includes("tok-function")));
      if (isFunction) {
        items.push({
          label: `Show Callgraph: ${token.name}()`,
          icon: "📊",
          onClick: () => {
            state.openTab({
              type: "callgraph",
              title: `Callgraph: ${token.name}`,
              functionName: token.name,
              version: v
            });
            toast.success(`Opened callgraph for ${token.name}`);
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

      items.push({
        label: "Open in NodeMap",
        icon: "🗺️",
        onClick: () => {
          state.openTab({
            type: "nodemap",
            title: "NodeMap Canvas",
            version: v,
            initialNode: {
              name: token.header || token.name,
              type: token.isInclude ? "include" : (token.type_id || "symbol"),
              astId: token.ast_id
            }
          });
          toast.success(`Opened ${token.name} in NodeMap`);
        }
      });

      items.push({ separator: true });
    } else if (selection && this.isKconfigToken({ name: selection }, filePath, ftype)) {
      items.push({
        label: `Open in Kconfig Viewer (${this.getKconfigSymbolName(selection)})`,
        icon: "⚙️",
        onClick: () => {
          this.openKconfigViewer(selection, v);
        }
      });
      items.push({ separator: true });
    }

    // 3. Git & History Actions
    items.push({
      label: "View Line Blame & Commit Details",
      icon: "🕒",
      onClick: () => this.showCommitDetailModal(blame, filePath, line, v)
    });

    items.push({ separator: true });

    // 4. Clipboard Actions
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
      items.push({ separator: true });
      items.push({
        label: "View File Git History",
        icon: "🕒",
        onClick: () => state.openTab({
          type: "commits",
          title: `History: ${item.name}`,
          filePath: path,
          version: v
        })
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
   * Right-click Context Menu for NodeMap Schematic Canvas.
   */
  showNodeMapMenu(x, y, context) {
    const { node, isCanvas, controller, constituent, constituentIndex } = context;
    const v = state.currentVersion;
    const items = [];

    if (node) {
      const nodeName = node.name || node.label || "Symbol";

      // If clicked specifically on a constituent member
      if (constituent) {
        const cName = constituent.name || constituent.type || "member";
        items.push({
          label: `Attach Node for "${cName}"`,
          icon: "🔗",
          onClick: () => {
            if (controller && typeof controller.attachNodeToConstituent === "function") {
              controller.attachNodeToConstituent(node.id, cName, constituentIndex);
            }
          }
        });
        items.push({ separator: true });
      }

      // Expand / Collapse Constituents
      items.push({
        label: node.expanded ? "Collapse Constituents" : "Expand Constituents",
        icon: node.expanded ? "🔼" : "🔽",
        onClick: async () => {
          if (controller && typeof controller.toggleExpandNode === "function") {
            await controller.toggleExpandNode(node.id);
          }
        }
      });

      // Attach Connected Node
      items.push({
        label: "Attach Connected Node...",
        icon: "🔗",
        onClick: () => {
          if (controller && typeof controller.promptAttachNode === "function") {
            controller.promptAttachNode(node.id);
          }
        }
      });

      items.push({ separator: true });

      items.push({
        label: "Go to Definition",
        icon: "🎯",
        onClick: async () => {
          try {
            const detail = await api.getSymbolDetail(v, nodeName);
            if (detail && detail.file_path) {
              state.openTab({
                type: "code",
                title: detail.file_path.split("/").pop(),
                path: detail.file_path,
                cursorLine: detail.definition?.line_s || detail.line_s || detail.line || 1,
                version: v
              });
              toast.success(`Jumped to definition of ${nodeName}`);
            } else {
              toast.info(`No definition found for ${nodeName}`);
            }
          } catch (err) {
            toast.error(`Lookup failed: ${err.message}`);
          }
        }
      });
      items.push({
        label: "Inspect AST Structure",
        icon: "🌳",
        onClick: async () => {
          try {
            const astId = node.astId || node.ast_id || 1;
            const astTree = await api.getAstTree(v, astId);
            this.showAstModal(nodeName, astTree);
          } catch (err) {
            toast.error(`AST fetch failed: ${err.message}`);
          }
        }
      });

      // Filter Struct Layout / Callgraph per Issue 4
      const isStruct = node.type === "struct" || nodeName.startsWith("struct ");
      const isFunction = node.type === "func" || node.type === "function" || nodeName.endsWith("()");

      if (isStruct || isFunction) {
        items.push({ separator: true });
      }
      if (isStruct) {
        items.push({
          label: `Show Struct Layout: ${nodeName}`,
          icon: "📐",
          onClick: () => {
            state.openTab({
              type: "pahole",
              title: `struct ${nodeName.replace(/^struct\s+/, "")}`,
              structName: nodeName.replace(/^struct\s+/, ""),
              version: v
            });
          }
        });
      }
      if (isFunction) {
        items.push({
          label: `Show Callgraph: ${nodeName}`,
          icon: "📊",
          onClick: () => {
            state.openTab({
              type: "callgraph",
              title: `Callgraph: ${nodeName}`,
              functionName: nodeName.replace(/\(\)$/, ""),
              version: v
            });
          }
        });
      }
      items.push({ separator: true });
      items.push({
        label: "Center / Focus Node",
        icon: "🎯",
        onClick: () => {
          if (controller && typeof controller.centerOnNode === "function") {
            controller.centerOnNode(node.id);
          }
        }
      });
      items.push({
        label: "Remove Node from Canvas",
        icon: "🗑️",
        danger: true,
        onClick: () => {
          if (controller && typeof controller.removeNode === "function") {
            controller.removeNode(node.id);
            toast.info(`Removed ${nodeName} from canvas`);
          }
        }
      });

      this.renderMenu(x, y, {
        header: {
          title: nodeName,
          sub: `NodeMap [${node.type || "symbol"}]`
        },
        items
      });
    } else if (isCanvas) {
      items.push({
        label: "Add Symbol / Node to Canvas...",
        icon: "➕",
        onClick: () => {
          const sym = prompt("Enter symbol or function name to add to canvas:");
          if (sym && sym.trim() && controller && typeof controller.addNode === "function") {
            controller.addNode({ name: sym.trim(), type: "symbol" });
          }
        }
      });
      items.push({
        label: "Reset View & Zoom (100%)",
        icon: "🔄",
        onClick: () => {
          if (controller && typeof controller.resetTransform === "function") {
            controller.resetTransform();
          }
        }
      });
      items.push({ separator: true });
      items.push({
        label: "Clear Entire Canvas",
        icon: "🧹",
        danger: true,
        onClick: () => {
          if (controller && typeof controller.clearAll === "function") {
            controller.clearAll();
            toast.info("Cleared NodeMap canvas");
          }
        }
      });

      this.renderMenu(x, y, {
        header: {
          title: "NodeMap Schematic",
          sub: "Canvas Controls"
        },
        items
      });
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
          const lineBlame = fileBlame.lines.find((b) => b.line === line);
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
        date: blame?.date || ""
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
    const dateStr = commitDetail.date || (blame?.date ? new Date(blame.date * 1000).toLocaleString() : "Unknown");
    const subject = commitDetail.subject || blame?.subject || "No commit message";
    const body = commitDetail.body || "";
    const files = commitDetail.touched_files || commitDetail.files || [];

    box.innerHTML = `
      <div style="padding:14px 16px;border-bottom:1px solid var(--border-color);display:flex;justify-content:space-between;align-items:flex-start;">
        <div>
          <div style="font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:8px;">
            <span>Line ${line || 1} modified in:</span>
            <code style="background:var(--bg-tertiary);padding:2px 6px;border-radius:4px;color:var(--accent-blue);font-family:var(--font-mono);">${shortHash}</code>
          </div>
          <div style="font-size:15px;font-weight:600;color:var(--text-primary);margin-top:6px;line-height:1.3;">
            ${this.escapeHtml(subject)}
          </div>
        </div>
        <button id="close-commit-btn" style="color:var(--text-muted);font-size:18px;background:none;border:none;cursor:pointer;padding:0 4px;">&times;</button>
      </div>

      <div style="padding:10px 16px;background:var(--bg-primary);border-bottom:1px solid var(--border-color);display:flex;gap:20px;font-size:11px;color:var(--text-secondary);">
        <div><strong style="color:var(--text-primary);">Author:</strong> <span class="commit-popover-author-link" style="color:var(--accent-blue);cursor:pointer;text-decoration:underline;" title="Click to view developer profile">${this.escapeHtml(author)}</span></div>
        <div><strong style="color:var(--text-primary);">Date:</strong> ${this.escapeHtml(dateStr)}</div>
      </div>

      ${body ? `
        <div style="max-height:140px;overflow-y:auto;padding:12px 16px;font-size:12px;line-height:1.5;color:var(--text-secondary);white-space:pre-wrap;border-bottom:1px solid var(--border-color);background:var(--bg-secondary);">
          ${this.escapeHtml(body)}
        </div>
      ` : ""}

      <div style="padding:12px 16px;display:flex;justify-content:space-between;align-items:center;background:var(--bg-secondary);">
        <div style="font-size:11px;color:var(--text-muted);">
          ${files.length > 0 ? `${files.length} touched files` : `${filePath || ''}`}
        </div>
        <div style="display:flex;gap:8px;">
          <button id="copy-hash-btn" class="code-btn" style="padding:4px 10px;font-size:11px;">Copy Hash</button>
          <button id="open-commit-tab-btn" class="code-btn active" style="padding:4px 12px;font-size:11px;">Open Full Diff in Commits Tab &rarr;</button>
        </div>
      </div>
    `;

    backdrop.appendChild(box);
    document.body.appendChild(backdrop);

    const authorLink = box.querySelector(".commit-popover-author-link");
    if (authorLink) {
      authorLink.onclick = () => {
        showPersonModal(state.currentVersion, author);
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
        version: v
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
          version: v,
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

  showXrefModal(name, xref) {
    const backdrop = document.createElement("div");
    backdrop.className = "palette-backdrop";
    backdrop.onclick = () => backdrop.remove();

    const box = document.createElement("div");
    box.className = "palette-box";
    box.style.width = "540px";
    box.onclick = (e) => e.stopPropagation();

    const defs = (xref && (xref.definitions || (xref.definition ? [xref.definition] : []))) || [];
    const refs = (xref && (xref.references || [])) || [];

    const defsHtml = defs.length > 0
      ? defs.map((d) => `
          <div class="xref-def-card" data-path="${this.escapeHtml(d.file)}" data-line="${d.line_s || 1}">
            <div>
              <div style="font-weight:600;color:var(--text-primary);display:flex;align-items:center;gap:6px;">
                <span>${this.escapeHtml(name)}</span>
                <span class="xref-def-badge">${this.escapeHtml(this.formatTypeName(d.type_name))}</span>
              </div>
              <div style="font-size:11px;color:var(--text-muted);font-family:var(--font-mono);margin-top:3px;">
                ${this.escapeHtml(d.file)}:${d.line_s || 1}
              </div>
            </div>
            <span style="font-size:11px;color:var(--accent-purple);font-weight:500;">Jump &rarr;</span>
          </div>
        `).join("")
      : `<div style="font-size:11px;color:var(--text-muted);margin-bottom:12px;font-style:italic;">No direct definition indexed for this symbol.</div>`;

    const refsHtml = refs.length > 0
      ? refs.map((r) => `
          <div class="palette-item xref-item" data-path="${this.escapeHtml(r.file)}" data-line="${r.line}">
            <div>
              <div style="font-weight:500;color:var(--text-primary);">${this.escapeHtml(r.caller || r.file)}</div>
              <div class="palette-item-desc">${this.escapeHtml(r.file)}:${r.line}</div>
            </div>
            <span style="font-size:10px;background:var(--bg-tertiary);padding:1px 6px;border-radius:3px;font-family:var(--font-mono);color:var(--accent-blue);">${this.escapeHtml(r.type || "Call")}</span>
          </div>
        `).join("")
      : `<div style="color:var(--text-muted);padding:10px 0;">
          <div>No external cross-references indexed.</div>
        </div>`;

    box.innerHTML = `
      <div style="padding:12px 16px;border-bottom:1px solid var(--border-color);display:flex;justify-content:space-between;align-items:center;">
        <span style="font-weight:bold;color:var(--accent-blue);">Cross-References: ${this.escapeHtml(name)}</span>
        <button id="close-xref-btn" style="color:var(--text-muted);font-size:18px;background:none;border:none;cursor:pointer;padding:0 4px;">&times;</button>
      </div>

      <div style="max-height:420px;overflow-y:auto;padding:12px 16px;">
        <div class="settings-section-title">Definition${defs.length > 1 ? `s (${defs.length})` : ""}</div>
        ${defsHtml}

        <div class="settings-section-title" style="margin-top:8px;">References &amp; Usages (${refs.length})</div>
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
        state.openTab({
          type: "code",
          title: el.dataset.path.split("/").pop(),
          path: el.dataset.path,
          cursorLine: parseInt(el.dataset.line || "1", 10),
          version: state.currentVersion
        });
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
            version: state.currentVersion,
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
        <span>AST Structure: ${name}</span>
        <button id="close-ast-btn" style="color:var(--text-muted);font-size:16px;">&times;</button>
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

  showIncludeSymbolsModal(name, incData, x, y, includeInfo) {
    const info = includeInfo || { header: name, name: name, version: state.currentVersion };
    includeSymbolsPopover.show(
      x || Math.max(16, window.innerWidth / 2 - 225),
      y || Math.max(16, window.innerHeight / 4),
      info,
      incData
    );
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
