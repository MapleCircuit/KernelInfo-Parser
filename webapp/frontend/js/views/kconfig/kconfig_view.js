/**
 * kconfig_view.js - KConfig Explorer, Constraint Inspector & .config Manager.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { KconfigEngine } from "./kconfig_engine.js";
import { KconfigParser } from "./kconfig_parser.js";
import { MenuconfigTui } from "./menuconfig_tui.js";
import { openModal, escapeHtml } from "../../components/modal.js";

export class KconfigView {
  constructor() {
    this.treeNodes = [];
    this.engine = new KconfigEngine();
    this.showUnmet = false; // Clean Linux menuconfig tree by default
    this.currentArch = "x86";
    this.currentVersion = "v3.0";
    this.selectedSymbol = null;
    this.selectedNodeId = null;
    this.expandedNodeIds = new Set();
    this.nodesByParent = new Map();
    this.nodesById = new Map();
    this.nodesBySymbol = new Map();
    this.containerEl = null;
    this.archPresets = [];
  }

  /** The constraint engine switch is global so code views can gate #ifdef dimming on it. */
  get engineActive() {
    return state.kconfigEngineActive;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.currentVersion = tabData.version || state.currentVersion;
    this.currentArch = tabData.arch || "x86";

    containerEl.innerHTML = `
      <div class="kconfig-view-container">
        <!-- 1. Left Explorer Column -->
        <div class="kconfig-explorer-col">
          <div class="kconfig-filter-bar">
            <div class="kconfig-arch-presets">
              <select id="kconfig-arch-select" style="flex:1;">
                <option value="x86" ${this.currentArch === "x86" ? "selected" : ""}>x86 (i386 / x86_64)</option>
                <option value="arm" ${this.currentArch === "arm" ? "selected" : ""}>ARM</option>
                <option value="arm64" ${this.currentArch === "arm64" ? "selected" : ""}>ARM64 (aarch64)</option>
                <option value="mips" ${this.currentArch === "mips" ? "selected" : ""}>MIPS</option>
                <option value="powerpc" ${this.currentArch === "powerpc" ? "selected" : ""}>PowerPC</option>
                <option value="sparc" ${this.currentArch === "sparc" ? "selected" : ""}>SPARC</option>
              </select>
              <button class="code-btn" id="btn-open-tui" title="Open Terminal Menuconfig">TUI</button>
            </div>
            <div class="kconfig-search-box">
              <input type="text" placeholder="Filter symbols..." id="kconfig-search-input" />
            </div>
            <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px;">
              <button class="code-btn ${this.engineActive ? "active" : ""}" id="btn-toggle-engine" style="flex:1;">
                ${this.engineActive ? "✓ Engine Active" : "⚡ Enable Engine"}
              </button>
              <button class="code-btn ${this.showUnmet ? "active" : ""}" id="btn-toggle-unmet" style="flex:1;" title="Toggle visibility of inactive/unmet options">
                ${this.showUnmet ? "✓ Showing Unmet" : "👁 Show Unmet"}
              </button>
            </div>
            <div style="display:flex;gap:6px;">
              <button class="code-btn" id="btn-export-config" style="flex:1;">Export .config</button>
              <button class="code-btn" id="btn-load-defconfig" style="flex:1;">Load Defconfig</button>
            </div>
            <div style="display:flex;gap:6px;margin-top:6px;">
              <button class="code-btn" id="btn-import-config" style="flex:1;" title="Load a .config file">Import .config</button>
              <button class="code-btn" id="btn-validate-config" style="flex:1;" title="Check select/depends constraints">Validate</button>
              <button class="code-btn" id="btn-compare-config" style="flex:1;" title="Compare the current configuration with a defconfig">Compare…</button>
              <input type="file" id="kconfig-import-file" accept=".config,text/plain,*" hidden />
            </div>
          </div>
          <div class="kconfig-tree-scroll" id="kconfig-tree-scroll">
            <div style="padding:16px;color:var(--text-muted);">Loading KConfig tree for ${this.currentArch}...</div>
          </div>
        </div>

        <!-- 2. Right Symbol Detail Column -->
        <div class="kconfig-detail-col" id="kconfig-detail-col">
          <div style="color:var(--text-muted);padding-top:40px;text-align:center;">
            Select a configuration option from the left tree to inspect its dependencies and compiled targets.
          </div>
        </div>
      </div>
    `;

    // Arch change with confirmation prompt if config has selections
    const archSelect = containerEl.querySelector("#kconfig-arch-select");
    archSelect.onchange = (e) => {
      const targetArch = e.target.value;
      if (targetArch === this.currentArch) return;

      const hasCustomAssignments = Object.keys(state.kconfigAssignments || {}).length > 0;
      if (hasCustomAssignments) {
        this.showConfirmModal({
          title: "Switch Architecture?",
          message: `Switching to <strong>${targetArch}</strong> will reset your current configuration selections and load the target architecture defaults.`,
          confirmText: `Switch to ${targetArch}`,
          cancelText: "Keep Current",
          onConfirm: async () => {
            await this.switchArchitecture(targetArch);
          },
          onCancel: () => {
            archSelect.value = this.currentArch;
          }
        });
      } else {
        this.switchArchitecture(targetArch);
      }
    };

    // Engine activation toggle
    const engineBtn = containerEl.querySelector("#btn-toggle-engine");
    engineBtn.onclick = () => {
      state.setKconfigEngineActive(!state.kconfigEngineActive);
      engineBtn.classList.toggle("active", this.engineActive);
      engineBtn.textContent = this.engineActive ? "✓ Engine Active" : "⚡ Enable Engine";
      if (this.engineActive) {
        const resolved = this.engine.propagate(state.kconfigAssignments);
        state.setKconfigAssignments(resolved);
        this.renderTree();
        toast.success("Constraint engine activated: live propagation and #ifdef dimming in code views");
      } else {
        toast.info("Constraint engine dormant (fast browsing mode, #ifdef dimming off)");
      }
    };

    // Unmet / inactive visibility toggle
    const unmetBtn = containerEl.querySelector("#btn-toggle-unmet");
    unmetBtn.onclick = () => {
      this.showUnmet = !this.showUnmet;
      unmetBtn.classList.toggle("active", this.showUnmet);
      unmetBtn.textContent = this.showUnmet ? "✓ Showing Unmet" : "👁 Show Unmet";
      this.renderTree();
      toast.info(this.showUnmet ? "Showing all options including unmet dependencies" : "Showing only active/visible options");
    };

    // TUI Menuconfig trigger
    containerEl.querySelector("#btn-open-tui").onclick = () => {
      const tui = new MenuconfigTui(this.treeNodes, this.engine, {
        getVal: (sym) => {
          const bare = sym.startsWith("CONFIG_") ? sym.substring(7) : sym;
          return state.kconfigAssignments[bare] || state.kconfigAssignments[`CONFIG_${bare}`] || "n";
        },
        setVal: (sym, val) => {
          state.setKconfigAssignment(sym, val);
          if (this.engineActive) {
            const resolved = this.engine.propagate(state.kconfigAssignments);
            state.setKconfigAssignments(resolved);
          }
          this.renderTree();
        },
        save: () => this.exportConfig()
      });
      tui.show();
    };

    // Export .config
    containerEl.querySelector("#btn-export-config").onclick = () => this.exportConfig();

    // Import .config (server-side parsing into assignments)
    const importInput = containerEl.querySelector("#kconfig-import-file");
    containerEl.querySelector("#btn-import-config").onclick = () => importInput.click();
    importInput.onchange = async () => {
      const file = importInput.files?.[0];
      importInput.value = "";
      if (!file) return;
      try {
        const res = await api.importKconfig(this.currentVersion, await file.text());
        const symbols = res.symbols || res.assignments || {};
        const count = Object.keys(symbols).length;
        if (!count) {
          toast.error("No CONFIG_ assignments found in that file");
          return;
        }
        state.setKconfigAssignments(this.engineActive ? this.engine.propagate(symbols) : symbols);
        this.renderTree();
        toast.success(`Imported ${count} symbols from ${file.name}`);
      } catch (err) {
        toast.error(`Import failed: ${err.message}`);
      }
    };

    containerEl.querySelector("#btn-validate-config").onclick = () => this.showValidation();
    containerEl.querySelector("#btn-compare-config").onclick = () => this.showCompareWithDefconfig();

    // Load Defconfig via Selector Modal
    containerEl.querySelector("#btn-load-defconfig").onclick = async () => {
      try {
        const defs = await api.getKconfigDefconfigs(this.currentVersion, this.currentArch);
        const list = (defs && defs.defconfigs) || [];
        if (list.length === 0) {
          toast.info("No architecture defconfig found.");
          return;
        }
        this.showDefconfigModal(list);
      } catch (err) {
        toast.error(`Failed to fetch defconfigs: ${err.message}`);
      }
    };

    // Search filter
    const searchInput = containerEl.querySelector("#kconfig-search-input");
    searchInput.oninput = () => {
      const q = searchInput.value.trim().toLowerCase();
      this.filterTree(q);
    };

    // Fetch architecture presets
    try {
      const presetsRes = await api.getKconfigPresets(this.currentVersion);
      this.archPresets = (presetsRes && (presetsRes.targets || presetsRes.architectures)) || [];
    } catch (err) {
      console.warn("Could not fetch arch presets:", err);
    }

    // Initial load: if state does not have architecture defconfig loaded (less than 50 symbols), load defaults
    const hasAssignments = Object.keys(state.kconfigAssignments || {}).length >= 50;
    if (!hasAssignments) {
      await this.switchArchitecture(this.currentArch);
    } else {
      await this.loadTree();
    }

    if (tabData && tabData.symbol) {
      this.selectAndInspectSymbol(tabData.symbol);
    }
  }

  async switchArchitecture(targetArch) {
    if (this.currentArch !== targetArch) {
      this.expandedNodeIds.clear();
      this.selectedSymbol = null;
      this.selectedNodeId = null;
    }
    this.currentArch = targetArch;
    const archSelect = this.containerEl?.querySelector("#kconfig-arch-select");
    if (archSelect) archSelect.value = targetArch;

    const scrollEl = this.containerEl?.querySelector("#kconfig-tree-scroll");
    if (scrollEl) {
      scrollEl.innerHTML = `<div style="padding:16px;color:var(--text-muted);">Switching architecture to ${targetArch}...</div>`;
    }

    // Find preset target
    if (this.archPresets.length === 0) {
      try {
        const presetsRes = await api.getKconfigPresets(this.currentVersion);
        this.archPresets = (presetsRes && (presetsRes.targets || presetsRes.architectures)) || [];
      } catch (e) {
        console.warn("Error fetching presets:", e);
      }
    }

    let target = this.archPresets.find((p) => p.id === targetArch || p.arch === targetArch);
    if (!target) {
      target = {
        id: targetArch,
        arch: targetArch,
        srcarch: targetArch,
        canonical_defconfig: targetArch === "x86" ? "x86_64_defconfig" : (targetArch === "arm" ? "versatile_defconfig" : "defconfig"),
        symbols: {
          [targetArch.toUpperCase()]: "y",
          "ARCH": targetArch,
          "SRCARCH": targetArch
        }
      };
    }

    let seedConfig = { ...(target.symbols || {}) };

    // Load canonical defconfig for target architecture
    if (target.canonical_defconfig) {
      try {
        const defRes = await api.getKconfigDefconfigContent(this.currentVersion, target.canonical_defconfig, this.currentArch);
        if (defRes && defRes.content) {
          const parsed = KconfigParser.parse(defRes.content);
          seedConfig = { ...seedConfig, ...parsed };
        } else if (defRes && defRes.values) {
          seedConfig = { ...seedConfig, ...defRes.values };
        }
      } catch (err) {
        console.warn(`Could not load ${target.canonical_defconfig}:`, err);
      }
    }

    // Load the target architecture's Kconfig tree
    await this.loadTree();

    // Propagate defaults cleanly through constraint engine
    if (this.engineActive) {
      const resolved = this.engine.propagate(seedConfig);
      state.setKconfigAssignments(resolved);
    } else {
      state.setKconfigAssignments(seedConfig);
    }

    this.renderTree();
    const count = Object.keys(state.kconfigAssignments).length / 2;
    toast.success(`Switched to ${target.label || targetArch} (${Math.round(count)} symbols loaded)`);
  }

  showConfirmModal({ title, message, confirmText = "Confirm", cancelText = "Cancel", onConfirm, onCancel }) {
    const backdrop = document.createElement("div");
    backdrop.className = "palette-backdrop";
    backdrop.onclick = () => {
      backdrop.remove();
      if (typeof onCancel === "function") onCancel();
    };

    const box = document.createElement("div");
    box.className = "palette-box";
    box.style.maxWidth = "420px";
    box.onclick = (e) => e.stopPropagation();

    box.innerHTML = `
      <div style="padding:14px 18px;border-bottom:1px solid var(--border-color);font-weight:600;color:var(--text-primary);font-size:14px;">
        ${title}
      </div>
      <div style="padding:16px 18px;color:var(--text-secondary);font-size:12px;line-height:1.5;">
        ${message}
      </div>
      <div style="padding:12px 18px;border-top:1px solid var(--border-color);display:flex;justify-content:flex-end;gap:8px;">
        <button class="code-btn" id="modal-cancel-btn">${cancelText}</button>
        <button class="code-btn active" id="modal-confirm-btn">${confirmText}</button>
      </div>
    `;

    backdrop.appendChild(box);
    document.body.appendChild(backdrop);

    box.querySelector("#modal-cancel-btn").onclick = () => {
      backdrop.remove();
      if (typeof onCancel === "function") onCancel();
    };

    box.querySelector("#modal-confirm-btn").onclick = () => {
      backdrop.remove();
      if (typeof onConfirm === "function") onConfirm();
    };
  }

  expandAncestors(node) {
    if (!node) return;
    let curr = node;
    while (curr && curr.parent_id) {
      this.expandedNodeIds.add(curr.parent_id);
      curr = this.nodesById?.get(curr.parent_id);
    }
  }

  selectAndInspectSymbol(symbolName) {
    if (!symbolName) return;
    const cleanSym = symbolName.startsWith("CONFIG_") ? symbolName.slice(7) : symbolName;

    const node = (this.nodesBySymbol && this.nodesBySymbol.get(cleanSym)) ||
      this.treeNodes.find((n) => n.symbol_name === cleanSym);

    if (node) {
      if (!this.shouldShowNode(node)) {
        this.showUnmet = true;
        const unmetBtn = this.containerEl?.querySelector("#btn-toggle-unmet");
        if (unmetBtn) {
          unmetBtn.classList.add("active");
          unmetBtn.textContent = "✓ Showing Unmet";
        }
      }

      this.expandAncestors(node);
      this.selectedSymbol = cleanSym;
      this.selectedNodeId = node.tree_id;

      // Clear search filter so full tree is visible with ancestors open
      const searchInput = this.containerEl?.querySelector("#kconfig-search-input");
      if (searchInput && searchInput.value) {
        searchInput.value = "";
      }

      this.renderTree();

      // Highlight row and scroll into view
      const row = this.containerEl?.querySelector(`.kconfig-node-row[data-symbol="${cleanSym}"]`);
      if (row) {
        this.containerEl.querySelectorAll(".kconfig-node-row").forEach((r) => r.classList.remove("active"));
        row.classList.add("active");
        row.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    }

    this.inspectSymbol(cleanSym, node || {});

    // Update active tab state and title
    const activeTab = state.getActiveTab();
    if (activeTab && activeTab.type === "kconfig") {
      activeTab.symbol = cleanSym;
      activeTab.title = `KConfig (${cleanSym})`;
      state.saveState();
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

  async loadTree() {
    const scrollEl = this.containerEl?.querySelector("#kconfig-tree-scroll");
    if (scrollEl) {
      scrollEl.innerHTML = `<div style="padding:16px;color:var(--text-muted);">Loading ${this.currentArch} tree...</div>`;
    }

    try {
      const res = await api.getKconfigTree(this.currentVersion, this.currentArch);
      this.treeNodes = res.nodes || [];
      this.engine.setNodes(this.treeNodes);

      if (this.engineActive) {
        const resolved = this.engine.propagate(state.kconfigAssignments);
        state.setKconfigAssignments(resolved);
      }

      this.renderTree();
    } catch (e) {
      if (scrollEl) {
        scrollEl.innerHTML = `<div style="padding:16px;color:var(--accent-red);">Failed to load tree: ${e.message}</div>`;
      }
    }
  }

  shouldShowNode(node) {
    if (!node) return false;

    const isConfig = Boolean(node.symbol_name);
    const hasPrompt = Boolean(node.prompt && String(node.prompt).trim().length > 0);

    // 1. Promptless configs are internal compilation symbols (e.g. HAVE_AOUT); never show in interactive menu tree
    if (isConfig && !hasPrompt) {
      return false;
    }

    // 2. If showUnmet is enabled, show all prompted nodes regardless of dependency state
    if (this.showUnmet) {
      return true;
    }

    // 3. For configs with prompts: check if symbol dependencies are satisfied
    if (isConfig) {
      return this.engine.isSymbolVisible(node.symbol_name, state.kconfigAssignments);
    }

    // 4. For choices/menus/comments: check direct expression if defined
    if (node.depends_on_expr) {
      return this.engine.evalExpr(node.depends_on_expr, state.kconfigAssignments) > 0;
    }

    return true;
  }

  renderTree() {
    const scrollEl = this.containerEl?.querySelector("#kconfig-tree-scroll");
    if (!scrollEl) return;
    const prevScrollTop = scrollEl.scrollTop;
    scrollEl.innerHTML = "";

    // Build children index and lookups for fast on-demand hierarchical expansion
    this.nodesByParent = new Map();
    this.nodesById = new Map();
    this.nodesBySymbol = new Map();
    this.treeNodes.forEach((node) => {
      if (node.tree_id) this.nodesById.set(node.tree_id, node);
      if (node.symbol_name) this.nodesBySymbol.set(node.symbol_name, node);
      const pid = node.parent_id || 0;
      if (!this.nodesByParent.has(pid)) {
        this.nodesByParent.set(pid, []);
      }
      this.nodesByParent.get(pid).push(node);
    });

    const rootNodes = (this.nodesByParent.get(0) || []).filter((node) => this.shouldShowNode(node));
    if (rootNodes.length === 0) {
      const fallbackNodes = this.treeNodes.filter((node) => this.shouldShowNode(node)).slice(0, 50);
      fallbackNodes.forEach((node) => {
        this.renderTreeNode(scrollEl, node, 0);
      });
    } else {
      rootNodes.forEach((node) => {
        this.renderTreeNode(scrollEl, node, 0);
      });
    }

    // Preserve scroll position
    scrollEl.scrollTop = prevScrollTop;

    // Restore active row highlight if selected
    if (this.selectedSymbol) {
      const activeRow = scrollEl.querySelector(`.kconfig-node-row[data-symbol="${this.selectedSymbol}"]`);
      if (activeRow) activeRow.classList.add("active");
    } else if (this.selectedNodeId) {
      const activeRow = scrollEl.querySelector(`.kconfig-node-row[data-node-id="${this.selectedNodeId}"]`);
      if (activeRow) activeRow.classList.add("active");
    }
  }

  renderTreeNode(container, node, depth) {
    const row = document.createElement("div");
    row.className = "kconfig-node-row";
    row.style.paddingLeft = `${12 + depth * 14}px`;
    row.dataset.symbol = node.symbol_name || "";
    row.dataset.nodeId = String(node.tree_id || "");

    const rawChildList = (this.nodesByParent && this.nodesByParent.get(node.tree_id))
      ? this.nodesByParent.get(node.tree_id)
      : (Array.isArray(node.children) ? node.children : []);
    const childList = rawChildList.filter((c) => this.shouldShowNode(c));
    const hasChildren = childList.length > 0;
    const isConfig = Boolean(node.symbol_name);
    const isChoice = Boolean(node.choice && node.choice.id);

    let stateStr = "   ";
    let val = "n";
    if (isConfig) {
      val = state.kconfigAssignments[node.symbol_name] || state.kconfigAssignments[`CONFIG_${node.symbol_name}`] || "n";
      const isTristate = node.type === 2 || node.type_name === "tristate";

      if (isChoice) {
        stateStr = val === "y" ? "(*)" : "( )";
      } else if (isTristate) {
        stateStr = val === "y" ? "[*]" : (val === "m" ? "<M>" : "[ ]");
      } else {
        stateStr = val === "y" ? "[*]" : "[ ]";
      }
    }

    // Visibility and dependency validation
    const isVis = isConfig ? this.engine.isSymbolVisible(node.symbol_name, state.kconfigAssignments) : true;
    if (!isVis && isConfig) {
      row.classList.add("kconfig-node-disabled");
      const failing = this.engine.getFailingDependencies(node, state.kconfigAssignments);
      if (failing.length > 0) {
        row.title = `Fails dependencies: ${failing.join(" && ")}`;
      }
    }

    if (this.selectedSymbol && node.symbol_name === this.selectedSymbol) {
      row.classList.add("active");
    } else if (!this.selectedSymbol && this.selectedNodeId && node.tree_id === this.selectedNodeId) {
      row.classList.add("active");
    }

    const isExpanded = hasChildren && this.expandedNodeIds.has(node.tree_id);
    const displayName = node.prompt || node.title || node.symbol_name || "Option";

    row.innerHTML = `
      <span class="chevron" style="visibility:${hasChildren ? 'visible' : 'hidden'};">${hasChildren ? (isExpanded ? '▼' : '▶') : '&nbsp;'}</span>
      <span class="kconfig-node-state" title="Click to cycle or select">${stateStr}</span>
      <span class="kconfig-node-name">${displayName}</span>
      ${node.symbol_name ? `<span class="kconfig-node-sym">(${node.symbol_name})</span>` : ""}
    `;

    container.appendChild(row);

    const childContainer = document.createElement("div");
    childContainer.style.display = isExpanded ? "block" : "none";
    container.appendChild(childContainer);

    // If already expanded in set, render children immediately
    if (isExpanded && hasChildren) {
      childList.forEach((c) => this.renderTreeNode(childContainer, c, depth + 1));
    }

    const chevron = row.querySelector(".chevron");
    chevron.onclick = (e) => {
      e.stopPropagation();
      const nextExpanded = !this.expandedNodeIds.has(node.tree_id);
      if (nextExpanded) {
        this.expandedNodeIds.add(node.tree_id);
        chevron.textContent = "▼";
        childContainer.style.display = "block";
        if (childContainer.children.length === 0 && hasChildren) {
          childList.forEach((c) => this.renderTreeNode(childContainer, c, depth + 1));
        }
      } else {
        this.expandedNodeIds.delete(node.tree_id);
        chevron.textContent = "▶";
        childContainer.style.display = "none";
      }
    };

    // State marker click to cycle or select choice member
    const stateEl = row.querySelector(".kconfig-node-state");
    stateEl.onclick = (e) => {
      e.stopPropagation();
      if (!isConfig) return;

      this.selectedSymbol = node.symbol_name;
      this.selectedNodeId = node.tree_id;

      if (isChoice) {
        const updated = this.engine.selectChoiceMember(node.choice.id, node.symbol_name, state.kconfigAssignments);
        state.setKconfigAssignments(updated);
        toast.info(`Selected choice member CONFIG_${node.symbol_name}`);
      } else {
        const nextVal = this.engine.cycleValue(node.symbol_name, val, node, state.kconfigAssignments);
        state.setKconfigAssignment(node.symbol_name, nextVal);
        if (this.engineActive) {
          const resolved = this.engine.propagate(state.kconfigAssignments);
          state.setKconfigAssignments(resolved);
        }
        toast.info(`Set CONFIG_${node.symbol_name}=${nextVal}`);
      }

      this.renderTree();
      this.inspectSymbol(node.symbol_name, node);
    };

    // Row selection
    row.onclick = () => {
      if (hasChildren && !node.symbol_name) {
        // Clicking folder row toggles expansion
        chevron.click();
        return;
      }
      this.containerEl.querySelectorAll(".kconfig-node-row").forEach((r) => r.classList.remove("active"));
      row.classList.add("active");
      this.selectedSymbol = node.symbol_name || null;
      this.selectedNodeId = node.tree_id;
      if (node.symbol_name) {
        this.inspectSymbol(node.symbol_name, node);
      }
    };

    row.ondblclick = () => {
      if (hasChildren) {
        chevron.click();
      }
    };

    row.addEventListener("auxclick", (e) => {
      if (e.button === 1 && node.symbol_name) {
        e.preventDefault();
        e.stopPropagation();
        state.openTab({
          type: "kconfig",
          title: `KConfig (${node.symbol_name})`,
          arch: this.currentArch,
          symbol: node.symbol_name,
          version: this.currentVersion,
          forceNew: true
        });
      }
    });
  }

  filterTree(query) {
    if (!query) {
      this.renderTree();
      return;
    }
    const scrollEl = this.containerEl.querySelector("#kconfig-tree-scroll");
    if (!scrollEl) return;
    scrollEl.innerHTML = "";

    const cleanQ = query.toLowerCase();
    const matches = this.treeNodes.filter((n) => {
      const sym = (n.symbol_name || "").toLowerCase();
      const title = (n.title || "").toLowerCase();
      const prompt = (n.prompt || "").toLowerCase();
      return sym.includes(cleanQ) || title.includes(cleanQ) || prompt.includes(cleanQ);
    });

    if (matches.length === 0) {
      scrollEl.innerHTML = `<div style="padding:16px;color:var(--text-muted);">No symbols matching "${this.escapeHtml(query)}"</div>`;
      return;
    }

    matches.slice(0, 100).forEach((node) => {
      this.renderTreeNode(scrollEl, node, 0);
    });

    if (matches.length > 100) {
      const more = document.createElement("div");
      more.style.padding = "8px 12px";
      more.style.color = "var(--text-muted)";
      more.style.fontSize = "11px";
      more.textContent = `Showing 100 of ${matches.length} matches. Refine filter for more.`;
      scrollEl.appendChild(more);
    }
  }

  async inspectSymbol(symbolName, nodeData = {}) {
    this.selectedSymbol = symbolName;
    if (nodeData && nodeData.tree_id) {
      this.selectedNodeId = nodeData.tree_id;
    }
    const detailCol = this.containerEl.querySelector("#kconfig-detail-col");
    detailCol.innerHTML = `<div style="color:var(--text-muted);">Loading detail for ${symbolName}...</div>`;

    try {
      const detail = await api.getKconfigSymbol(this.currentVersion, symbolName);
      const sym = detail.symbol || detail;

      const currentVal = state.kconfigAssignments[symbolName] || state.kconfigAssignments[`CONFIG_${symbolName}`] || "n";
      const isChoice = Boolean(nodeData.choice && nodeData.choice.id);
      const isTristate = nodeData.type === 2 || nodeData.type_name === "tristate" || sym.type === 2 || sym.type_name === "tristate";
      const typeLabel = isChoice ? "choice member" : (isTristate ? "tristate" : (nodeData.type_name || sym.type_name || "bool"));

      // Direct dependencies (depends_on)
      const rawDeps = (Array.isArray(nodeData.depends_on) && nodeData.depends_on.length > 0)
        ? nodeData.depends_on
        : (sym.depends_on || []);

      const rawRevDeps = (Array.isArray(nodeData.selected_by) && nodeData.selected_by.length > 0)
        ? nodeData.selected_by
        : (sym.selected_by || []);

      const rawSelects = (Array.isArray(nodeData.selects) && nodeData.selects.length > 0)
        ? nodeData.selects
        : (sym.selects || []);

      const rawDefaults = (Array.isArray(nodeData.defaults) && nodeData.defaults.length > 0)
        ? nodeData.defaults
        : (sym.defaults || []);

      // Evaluated dependencies badge renderer
      const renderEvaluatedDeps = (deps) => {
        if (!Array.isArray(deps) || deps.length === 0) {
          return `<span style="color:var(--text-muted);font-size:11px;">Unconditional / None</span>`;
        }
        return deps.map((dep) => {
          const raw = typeof dep === "string" ? dep : (dep.name || dep.symbol || "");
          const clean = raw.replace(/^[!]/, "").replace(/^CONFIG_/, "").trim();
          const evalVal = this.engine.evalExpr(raw, state.kconfigAssignments);
          const statusClass = evalVal === 2 ? "dep-met" : (evalVal === 1 ? "dep-mod" : "dep-unmet");
          const statusPrefix = evalVal === 2 ? "✓ " : (evalVal === 1 ? "~ " : "✗ ");
          const statusSuffix = evalVal === 2 ? " (y)" : (evalVal === 1 ? " (m)" : " (n)");

          return `
            <span class="kconfig-badge kconfig-badge-link ${statusClass}" data-symbol="${this.escapeHtml(clean)}" title="Expression: ${this.escapeHtml(raw)} &rarr; evaluated ${evalVal}">
              ${statusPrefix}${this.escapeHtml(raw)}${statusSuffix}
            </span>
          `;
        }).join("");
      };

      const renderBadges = (list) => {
        if (!Array.isArray(list) || list.length === 0) {
          return `<span style="color:var(--text-muted);font-size:11px;">None</span>`;
        }
        return list.map((item) => {
          const raw = typeof item === "string" ? item : (item.name || item.symbol || "");
          const clean = raw.replace(/^[!]/, "").replace(/^CONFIG_/, "").trim();
          return `<span class="kconfig-badge kconfig-badge-link" data-symbol="${this.escapeHtml(clean)}" title="Jump to CONFIG_${this.escapeHtml(clean)}">${this.escapeHtml(raw)}</span>`;
        }).join("");
      };

      const renderDefaults = (defs) => {
        if (!Array.isArray(defs) || defs.length === 0) {
          return `<span style="color:var(--text-muted);font-size:11px;">Default is 'n'</span>`;
        }
        return defs.map((d) => {
          const val = typeof d === "object" ? (d.value || d.expr || "y") : d;
          const cond = typeof d === "object" ? d.expr || d.cond : null;
          const condActive = cond ? this.engine.evalExpr(cond, state.kconfigAssignments) > 0 : true;

          return `
            <div style="font-family:var(--font-mono);font-size:11px;display:flex;align-items:center;gap:8px;">
              <span style="color:${condActive ? 'var(--accent-green)' : 'var(--text-muted)'};font-weight:600;">default ${this.escapeHtml(val)}</span>
              ${cond ? `<span style="color:var(--text-muted);font-size:10px;">if ${this.escapeHtml(cond)} (${condActive ? 'met' : 'unmet'})</span>` : ""}
            </div>
          `;
        }).join("");
      };

      detailCol.innerHTML = `
        <div class="kconfig-symbol-header">
          <div>
            <div style="display:flex;align-items:center;gap:8px;">
              <div class="kconfig-symbol-title">CONFIG_${sym.name || symbolName}</div>
              <span class="kconfig-type-badge">${typeLabel}</span>
            </div>
            <div class="kconfig-prompt">${sym.prompt || nodeData.prompt || "No prompt available"}</div>
            ${(sym.file_path || nodeData.file_path) ? `
              <div style="font-size:11px;color:var(--text-muted);margin-top:6px;display:flex;align-items:center;gap:6px;">
                <span>📁 Declared in:</span>
                <button class="code-btn" id="btn-jump-kconfig-decl" style="padding:2px 8px;font-size:11px;color:var(--accent-blue);" title="Jump to declaration in Code View">
                  📄 ${this.escapeHtml(sym.file_path || nodeData.file_path)}:${sym.line_s || nodeData.line_s || 1}
                </button>
              </div>
            ` : ""}
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            ${
              rawDeps.length > 0
                ? `<button class="code-btn" id="btn-autosolve-sym" title="Auto-solve prerequisite dependencies">🪄 Auto-Solve</button>`
                : ""
            }
            <button class="code-btn ${currentVal !== 'n' ? 'active' : ''}" id="btn-toggle-sym">
              Value: ${currentVal === 'y' ? 'CONFIG_y' : (currentVal === 'm' ? 'CONFIG_m' : 'Not Set (n)')}
            </button>
          </div>
        </div>

        ${
          isChoice ? `
            <div class="kconfig-choice-box">
              <div style="font-size:11px;font-weight:600;color:var(--accent-blue);text-transform:uppercase;">
                Choice Block: ${this.escapeHtml(nodeData.choice.prompt || nodeData.choice.name || "Mutually Exclusive Choice")}
              </div>
              <div style="font-size:11px;color:var(--text-secondary);">
                Only one member of this choice block can be selected at a time.
              </div>
              <button class="code-btn active" id="btn-select-this-choice" style="align-self:flex-start;margin-top:4px;">
                Select CONFIG_${symbolName}
              </button>
            </div>
          ` : ""
        }

        <!-- Dependencies -->
        <div class="kconfig-prop-card">
          <div class="kconfig-prop-title">Direct Dependencies (depends on)</div>
          <div class="kconfig-badge-list">
            ${renderEvaluatedDeps(rawDeps)}
          </div>
        </div>

        <!-- Defaults -->
        <div class="kconfig-prop-card">
          <div class="kconfig-prop-title">Defaults</div>
          <div style="display:flex;flex-direction:column;gap:4px;">
            ${renderDefaults(rawDefaults)}
          </div>
        </div>

        <!-- Reverse Dependencies -->
        <div class="kconfig-prop-card">
          <div class="kconfig-prop-title">Reverse Dependencies (selected by)</div>
          <div class="kconfig-badge-list">
            ${renderBadges(rawRevDeps)}
          </div>
        </div>

        <!-- Selects -->
        <div class="kconfig-prop-card">
          <div class="kconfig-prop-title">Selects (Forces Active)</div>
          <div class="kconfig-badge-list">
            ${renderBadges(rawSelects)}
          </div>
        </div>

        <!-- Dependency graph -->
        <div class="kconfig-prop-card">
          <div class="kconfig-prop-title" style="display:flex;justify-content:space-between;align-items:center;">
            <span>Dependency Graph</span>
            <button class="code-btn" id="btn-kconfig-graph" type="button">Show graph</button>
          </div>
          <div id="kconfig-dep-graph"></div>
        </div>

        <!-- Compiled Files -->
        <div class="kconfig-prop-card">
          <div class="kconfig-prop-title">Compiled Objects & Source Files</div>
          <div style="display:flex;flex-direction:column;gap:4px;font-family:var(--font-mono);font-size:11px;">
            ${
              Array.isArray(sym.compiled_files) && sym.compiled_files.length > 0
                ? sym.compiled_files.map((cf) => {
                    const filePath = typeof cf === "object" && cf !== null ? (cf.file_path || cf.path || "") : String(cf);
                    const targetObj = typeof cf === "object" && cf !== null && cf.target_obj ? `<span style="color:var(--text-muted);font-size:10px;margin-left:6px;">(${cf.target_obj})</span>` : "";
                    return `
                      <div style="color:var(--accent-blue);cursor:pointer;display:flex;align-items:center;padding:4px 6px;border-radius:3px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);" class="compiled-file-link" data-path="${filePath}" title="Open ${filePath} in editor">
                        <span style="margin-right:6px;">📄</span>
                        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${filePath}</span>
                        ${targetObj}
                      </div>
                    `;
                  }).join("")
                : `<span style="color:var(--text-muted);font-size:11px;">No direct obj-y compiled sources associated.</span>`
            }
          </div>
        </div>
      `;

      const graphBtn = detailCol.querySelector("#btn-kconfig-graph");
      graphBtn.onclick = () => {
        graphBtn.disabled = true;
        this.renderDependencyGraph(symbolName, detailCol.querySelector("#kconfig-dep-graph")).finally(() => {
          graphBtn.disabled = false;
        });
      };

      // Jump to Kconfig declaration in code view
      const declBtn = detailCol.querySelector("#btn-jump-kconfig-decl");
      if (declBtn && (sym.file_path || nodeData.file_path)) {
        const filePath = sym.file_path || nodeData.file_path;
        const line = sym.line_s || nodeData.line_s || 1;
        declBtn.onclick = () => {
          state.openTab({
            type: "code",
            title: filePath.split("/").pop(),
            path: filePath,
            cursorLine: line,
            version: this.currentVersion,
            forceNew: true,
          });
          toast.success(`Opened ${filePath} at line ${line}`);
        };
      }

      // Choice selection button
      const choiceBtn = detailCol.querySelector("#btn-select-this-choice");
      if (choiceBtn && isChoice) {
        choiceBtn.onclick = () => {
          const updated = this.engine.selectChoiceMember(nodeData.choice.id, symbolName, state.kconfigAssignments);
          state.setKconfigAssignments(updated);
          this.inspectSymbol(symbolName, nodeData);
          this.renderTree();
          toast.success(`Selected choice member CONFIG_${symbolName}`);
        };
      }

      // Auto-solve symbol prerequisites
      const autosolveBtn = detailCol.querySelector("#btn-autosolve-sym");
      if (autosolveBtn) {
        autosolveBtn.onclick = async () => {
          try {
            toast.info(`Auto-solving prerequisites for CONFIG_${symbolName}...`);
            const res = await api.autosolveKconfig(this.currentVersion, { target_symbol: symbolName });
            const toggles = (res && res.toggles_needed) || [];
            if (toggles.length > 0) {
              toggles.forEach((t) => {
                state.setKconfigAssignment(t.symbol, t.set_to || "y");
              });
              if (this.engineActive) {
                const resolved = this.engine.propagate(state.kconfigAssignments);
                state.setKconfigAssignments(resolved);
              }
              this.inspectSymbol(symbolName, nodeData);
              this.renderTree();
              toast.success(`Applied ${toggles.length} prerequisite toggles for CONFIG_${symbolName}`);
            } else {
              toast.info(`No missing prerequisites for CONFIG_${symbolName}`);
            }
          } catch (err) {
            toast.error(`Autosolve failed: ${err.message}`);
          }
        };
      }

      // Toggle symbol
      const toggleBtn = detailCol.querySelector("#btn-toggle-sym");
      toggleBtn.onclick = () => {
        if (isChoice) {
          const updated = this.engine.selectChoiceMember(nodeData.choice.id, symbolName, state.kconfigAssignments);
          state.setKconfigAssignments(updated);
        } else {
          const nextVal = this.engine.cycleValue(symbolName, currentVal, nodeData, state.kconfigAssignments);
          state.setKconfigAssignment(symbolName, nextVal);
          if (this.engineActive) {
            const resolved = this.engine.propagate(state.kconfigAssignments);
            state.setKconfigAssignments(resolved);
          }
        }
        this.inspectSymbol(symbolName, nodeData);
        this.renderTree();
      };

      // Jump to dependency/select symbol
      detailCol.querySelectorAll(".kconfig-badge-link").forEach((badge) => {
        badge.onclick = (e) => {
          e.stopPropagation();
          const targetSym = badge.dataset.symbol;
          if (targetSym) {
            this.selectAndInspectSymbol(targetSym);
          }
        };
        badge.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            const targetSym = badge.dataset.symbol;
            if (targetSym) {
              state.openTab({
                type: "kconfig",
                title: `KConfig (${targetSym})`,
                arch: this.currentArch,
                symbol: targetSym,
                version: this.currentVersion,
                forceNew: true
              });
            }
          }
        });
      });

      // Jump to compiled file
      detailCol.querySelectorAll(".compiled-file-link").forEach((link) => {
        link.onclick = () => {
          state.openTab({
            type: "code",
            title: link.dataset.path.split("/").pop(),
            path: link.dataset.path,
            version: this.currentVersion
          });
        };
        link.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            state.openTab({
              type: "code",
              title: link.dataset.path.split("/").pop(),
              path: link.dataset.path,
              version: this.currentVersion,
              forceNew: true
            });
          }
        });
      });
    } catch (e) {
      detailCol.innerHTML = `<div style="color:var(--accent-red);padding:16px;">Failed to fetch detail: ${e.message}</div>`;
    }
  }

  valueOf(symbol) {
    return state.kconfigAssignments[symbol] || state.kconfigAssignments[`CONFIG_${symbol}`] || "n";
  }

  /** Depends-on (left) and selects (right) relations of a symbol as a clickable SVG graph. */
  async renderDependencyGraph(symbolName, container) {
    container.innerHTML = `<div style="color:var(--text-muted);font-size:11px;padding:6px 0;">Loading graph…</div>`;
    let graph;
    try {
      graph = await api.getKconfigGraph(this.currentVersion, this.currentArch, symbolName, 1);
    } catch (err) {
      container.innerHTML = `<div style="color:var(--accent-red);font-size:11px;">${escapeHtml(err.message)}</div>`;
      return;
    }
    const edges = graph.edges || [];
    const left = [...new Set(edges.filter((e) => e.type === "depends_on" && e.from === symbolName).map((e) => e.to))];
    const right = [...new Set(edges.filter((e) => e.type !== "depends_on" && e.from === symbolName).map((e) => e.to))];
    if (!left.length && !right.length) {
      container.innerHTML = `<div style="color:var(--text-muted);font-size:11px;padding:6px 0;">No depends-on or select relations recorded.</div>`;
      return;
    }

    const ROW = 26;
    const NODE_W = 170;
    const W = 620;
    const rows = Math.max(left.length, right.length, 1);
    const H = rows * ROW + 30;
    const rootY = H / 2;
    const colY = (count, i) => (H - count * ROW) / 2 + i * ROW + ROW / 2;
    const fill = (sym) => ({ y: "rgba(63,185,80,0.25)", m: "rgba(210,153,34,0.25)" }[this.valueOf(sym)] || "rgba(110,118,129,0.18)");
    const stroke = (sym) => ({ y: "#3fb950", m: "#d29922" }[this.valueOf(sym)] || "#6e7681");
    const node = (sym, x, y, isRoot = false) => `
      <g class="kgraph-node" data-symbol="${escapeHtml(sym)}" style="cursor:pointer;">
        <rect x="${x}" y="${y - 10}" width="${NODE_W}" height="20" rx="4" fill="${isRoot ? "rgba(56,139,253,0.25)" : fill(sym)}" stroke="${isRoot ? "#58a6ff" : stroke(sym)}" />
        <text x="${x + 8}" y="${y + 4}" font-size="11" font-family="var(--font-mono)" fill="#c9d1d9">${escapeHtml(sym.length > 22 ? `${sym.slice(0, 21)}…` : sym)} = ${escapeHtml(this.valueOf(sym))}</text>
        <title>CONFIG_${escapeHtml(sym)} = ${escapeHtml(this.valueOf(sym))}</title>
      </g>`;
    const rootX = (W - NODE_W) / 2;
    let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;display:block;">
      <text x="10" y="14" font-size="10" fill="#8b949e">depends on</text>
      <text x="${W - 10}" y="14" font-size="10" fill="#8b949e" text-anchor="end">selects</text>`;
    left.forEach((sym, i) => {
      const y = colY(left.length, i);
      svg += `<path d="M ${10 + NODE_W} ${y} C ${rootX - 30} ${y}, ${rootX - 30} ${rootY}, ${rootX} ${rootY}" fill="none" stroke="#6e7681" stroke-opacity="0.6" />`;
    });
    right.forEach((sym, i) => {
      const y = colY(right.length, i);
      svg += `<path d="M ${rootX + NODE_W} ${rootY} C ${W - NODE_W - 40} ${rootY}, ${W - NODE_W - 40} ${y}, ${W - NODE_W - 10} ${y}" fill="none" stroke="#bc8cff" stroke-opacity="0.6" />`;
    });
    left.forEach((sym, i) => { svg += node(sym, 10, colY(left.length, i)); });
    right.forEach((sym, i) => { svg += node(sym, W - NODE_W - 10, colY(right.length, i)); });
    svg += node(symbolName, rootX, rootY, true);
    svg += `</svg>
      <div style="font-size:10px;color:var(--text-muted);margin-top:4px;">Green = y, amber = m, grey = n/unset under the current configuration. Click a node to inspect it.</div>`;
    container.innerHTML = svg;
    container.querySelectorAll(".kgraph-node").forEach((g) => {
      g.addEventListener("click", () => {
        const sym = g.dataset.symbol;
        if (sym && sym !== symbolName) this.selectAndInspectSymbol(sym);
      });
    });
  }

  /** Server-side constraint check of the current assignments. */
  async showValidation() {
    const modal = openModal({ title: "Validate configuration", subtitle: `${this.currentArch} @ ${this.currentVersion}`, width: "640px" });
    modal.body.innerHTML = `<div class="ki-modal-empty">Checking select / depends-on constraints…</div>`;
    try {
      const bare = {};
      for (const [k, v] of Object.entries(state.kconfigAssignments)) if (!k.startsWith("CONFIG_")) bare[k] = v;
      const res = await api.validateKconfig(this.currentVersion, bare);
      const forced = Object.entries(res.forced_symbols || {});
      const adjusted = Object.entries(res.adjusted_symbols || {});
      const conflicts = res.conflicts || [];
      const list = (title, rows, color) => rows.length ? `
        <div class="kconfig-prop-card"><div class="kconfig-prop-title" style="color:${color};">${title} (${rows.length})</div>
          <div style="font-family:var(--font-mono);font-size:11px;display:flex;flex-direction:column;gap:2px;">${rows.join("")}</div></div>` : "";
      modal.body.innerHTML = (forced.length || adjusted.length || conflicts.length)
        ? list("Forced by select", forced.map(([k, v]) => `<div>CONFIG_${escapeHtml(k)} → ${escapeHtml(v)}</div>`), "var(--accent-blue)")
          + list("Adjusted (unmet dependencies)", adjusted.map(([k, v]) => `<div>CONFIG_${escapeHtml(k)} → ${escapeHtml(v)}</div>`), "var(--accent-yellow)")
          + list("Conflicts", conflicts.map((c) => `<div>${escapeHtml(typeof c === "string" ? c : JSON.stringify(c))}</div>`), "var(--accent-red)")
        : `<div class="ki-modal-empty">✓ No constraint violations found for ${Object.keys(bare).length} symbols.</div>`;
      if (forced.length || adjusted.length) {
        modal.footer.insertAdjacentHTML("afterbegin", `<button class="code-btn active" id="btn-apply-validation" type="button">Apply ${forced.length + adjusted.length} fixes</button>`);
        modal.footer.querySelector("#btn-apply-validation").onclick = () => {
          state.setKconfigAssignments({ ...bare, ...Object.fromEntries(forced), ...Object.fromEntries(adjusted) });
          this.renderTree();
          modal.close();
          toast.success("Applied validation fixes");
        };
      }
    } catch (err) {
      modal.body.innerHTML = `<div class="ki-modal-error">${escapeHtml(err.message)}</div>`;
    }
  }

  /** Compare the current configuration with one of the architecture's defconfigs. */
  async showCompareWithDefconfig() {
    const modal = openModal({ title: "Compare with defconfig", subtitle: `${this.currentArch} @ ${this.currentVersion}`, width: "720px" });
    modal.body.innerHTML = `<div class="ki-modal-empty">Loading defconfigs…</div>`;
    let list = [];
    try {
      const defs = await api.getKconfigDefconfigs(this.currentVersion, this.currentArch);
      list = (defs && defs.defconfigs) || [];
    } catch (err) {
      modal.body.innerHTML = `<div class="ki-modal-error">${escapeHtml(err.message)}</div>`;
      return;
    }
    if (!list.length) {
      modal.body.innerHTML = `<div class="ki-modal-empty">No defconfigs found for ${escapeHtml(this.currentArch)}.</div>`;
      return;
    }
    const nameOf = (d) => (typeof d === "string" ? d : d.name || d.file_name || d.path || "");
    modal.body.innerHTML = `
      <div class="ki-form-row"><label>Defconfig
        <select id="cmp-defconfig">${list.map((d) => `<option value="${escapeHtml(nameOf(d))}">${escapeHtml(nameOf(d).split("/").pop())}</option>`).join("")}</select>
      </label></div>
      <div id="cmp-defconfig-result"></div>`;
    const select = modal.body.querySelector("#cmp-defconfig");
    const run = async () => {
      const out = modal.body.querySelector("#cmp-defconfig-result");
      out.innerHTML = `<div class="ki-modal-empty">Comparing…</div>`;
      try {
        const defRes = await api.getKconfigDefconfigContent(this.currentVersion, select.value, this.currentArch);
        const other = defRes?.content ? KconfigParser.parse(defRes.content) : (defRes?.values || {});
        const bareOf = (cfg) => Object.fromEntries(Object.entries(cfg).filter(([k]) => !k.startsWith("CONFIG_")).map(([k, v]) => [k, String(v)]));
        const res = await api.diffKconfigConfigs(this.currentVersion, bareOf(state.kconfigAssignments), bareOf(other));
        const diffs = res.differences || [];
        out.innerHTML = `
          <div style="font-size:12px;color:var(--text-secondary);margin:6px 0 10px;">${res.matching_symbols} matching · <strong>${res.mismatched_symbols}</strong> different</div>
          <table class="kconfig-cmp-table"><thead><tr><th>Option</th><th>Current</th><th>${escapeHtml(select.value.split("/").pop())}</th></tr></thead>
          <tbody>${diffs.map((d) => `<tr data-symbol="${escapeHtml(d.symbol)}"><td>CONFIG_${escapeHtml(d.symbol)}</td><td>${escapeHtml(d.val1 ?? "—")}</td><td>${escapeHtml(d.val2 ?? "—")}</td></tr>`).join("")}</tbody></table>`;
        out.querySelectorAll("tr[data-symbol]").forEach((tr) => {
          tr.onclick = () => {
            modal.close();
            this.selectAndInspectSymbol(tr.dataset.symbol);
          };
        });
      } catch (err) {
        out.innerHTML = `<div class="ki-modal-error">${escapeHtml(err.message)}</div>`;
      }
    };
    select.onchange = run;
    run();
  }

  exportConfig() {
    const text = KconfigParser.serialize(state.kconfigAssignments, this.currentVersion, this.currentArch);
    const blob = new Blob([text], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `.config_${this.currentArch}`;
    a.click();
    toast.success(`Exported .config_${this.currentArch}`);
  }

  showDefconfigModal(defconfigs) {
    const backdrop = document.createElement("div");
    backdrop.className = "palette-backdrop";
    backdrop.onclick = () => backdrop.remove();

    const box = document.createElement("div");
    box.className = "palette-box";
    box.onclick = (e) => e.stopPropagation();

    box.innerHTML = `
      <div style="padding:12px 16px;border-bottom:1px solid var(--border-color);display:flex;justify-content:space-between;align-items:center;">
        <span style="font-weight:bold;color:var(--accent-blue);">Select Architecture Defconfig (${this.currentArch})</span>
        <button id="close-def-btn" style="color:var(--text-muted);font-size:16px;">&times;</button>
      </div>
      <div style="max-height:360px;overflow-y:auto;padding:8px 16px;display:flex;flex-direction:column;gap:6px;">
        ${
          defconfigs.map((d) => `
            <div class="palette-item def-item" style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;">
              <div>
                <div style="font-weight:600;color:var(--text-primary);">${d.name || d.file_path}</div>
                <div class="palette-item-desc">${d.file_path || ""}</div>
              </div>
              <button class="code-btn active btn-load-single-def" data-path="${d.file_path || d.path || d.name}">Load</button>
            </div>
          `).join("")
        }
      </div>
    `;

    backdrop.appendChild(box);
    document.body.appendChild(backdrop);

    box.querySelector("#close-def-btn").onclick = () => backdrop.remove();

    box.querySelectorAll(".btn-load-single-def").forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        const p = btn.dataset.path;
        backdrop.remove();
        toast.info(`Loading defconfig: ${p}...`);
        try {
          const res = await api.getKconfigDefconfigContent(this.currentVersion, p, this.currentArch);
          let parsed = {};
          if (res.content) {
            parsed = KconfigParser.parse(res.content);
          } else if (res.values) {
            parsed = res.values;
          }

          // Preserve preset arch symbols for the active architecture
          let target = this.archPresets.find((pr) => pr.id === this.currentArch || pr.arch === this.currentArch);
          const archSymbols = (target && target.symbols) || {
            [this.currentArch.toUpperCase()]: "y",
            "ARCH": this.currentArch,
            "SRCARCH": this.currentArch
          };

          const merged = { ...archSymbols, ...parsed };

          if (this.engineActive) {
            const resolved = this.engine.propagate(merged);
            state.setKconfigAssignments(resolved);
          } else {
            state.setKconfigAssignments(merged);
          }
          const symCount = Object.keys(parsed).length;
          toast.success(`Loaded defconfig: ${p.split("/").pop()} (${symCount} symbols)`);
          this.renderTree();
        } catch (err) {
          toast.error(`Failed to load defconfig: ${err.message}`);
        }
      };
    });

    box.querySelectorAll(".palette-item.def-item").forEach((itemEl) => {
      itemEl.addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          e.stopPropagation();
          const btn = itemEl.querySelector(".btn-load-single-def");
          const p = btn?.dataset.path;
          if (p) {
            state.openTab({
              type: "kconfig",
              title: `KConfig (${p.split("/").pop()})`,
              arch: this.currentArch,
              version: this.currentVersion,
              forceNew: true
            });
          }
        }
      });
    });
  }
}
