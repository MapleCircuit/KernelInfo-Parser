/**
 * menuconfig_tui.js - Authentic Terminal-Style Curses Menuconfig TUI.
 * Provides keyboard-driven navigation, symbol toggling, and .config saving.
 */
import { toast } from "../../components/toast.js";
import { state } from "../../state.js";

export class MenuconfigTui {
  constructor(treeNodes, engine, onSave) {
    this.treeNodes = treeNodes || [];
    this.engine = engine;
    this.onSave = onSave;

    // Index nodes by parent_id for authentic hierarchical submenu navigation
    this.nodesByParent = new Map();
    this.treeNodes.forEach((node) => {
      const pid = node.parent_id || 0;
      if (!this.nodesByParent.has(pid)) {
        this.nodesByParent.set(pid, []);
      }
      this.nodesByParent.get(pid).push(node);
    });

    const filterPrompted = (nodes) => (nodes || []).filter((node) => {
      const isConfig = Boolean(node.symbol_name);
      if (isConfig && (!node.prompt || !String(node.prompt).trim())) return false;
      return true;
    });
    this.filterPrompted = filterPrompted;

    const rootNodes = filterPrompted(this.nodesByParent.get(0));
    this.currentMenu = { title: "Linux Kernel Configuration", children: rootNodes };
    this.menuStack = [];
    this.selectedIndex = 0;
    this.modalEl = null;
    this.keyHandler = null;
  }

  show() {
    this.modalEl = document.createElement("div");
    this.modalEl.className = "menuconfig-tui-modal";
    document.body.appendChild(this.modalEl);

    this.render();
    this.attachKeyboard();
  }

  hide() {
    if (this.keyHandler) {
      window.removeEventListener("keydown", this.keyHandler);
      this.keyHandler = null;
    }
    if (this.modalEl) {
      this.modalEl.remove();
      this.modalEl = null;
    }
  }

  render() {
    if (!this.modalEl) return;

    const items = this.currentMenu.children || [];
    this.selectedIndex = Math.min(this.selectedIndex, Math.max(0, items.length - 1));

    this.modalEl.innerHTML = `
      <div class="tui-header">Linux Kernel Configuration (Menuconfig TUI)</div>
      <div class="tui-body">
        <div class="tui-window">
          <div class="tui-window-title">${this.currentMenu.title}</div>
          <div class="tui-instructions">
            Arrow keys navigate. &lt;Enter&gt; enters submenus ---&gt;. &lt;Space&gt; cycles [*]/&lt;M&gt;/[ ] or selects (*). Press &lt;Esc&gt; Back, &lt;S&gt; Save.
          </div>
          <div class="tui-menu-list" id="tui-menu-list">
            ${
              items.length > 0
                ? items.map((item, idx) => {
                    const isSelected = idx === this.selectedIndex;
                    const childList = this.nodesByParent.get(item.tree_id) || [];
                    const hasSubmenu = childList.length > 0;
                    const sym = item.symbol_name;
                    const isConfig = item.node_type === 3 || item.node_type === 4 || Boolean(sym);
                    const isChoice = Boolean(item.choice && item.choice.id);
                    const isTristate = item.type === 2 || item.type_name === "tristate";

                    let stateMarker = "   ";
                    if (isConfig && sym) {
                      const val = this.onSave?.getVal ? this.onSave.getVal(sym) : "n";
                      if (isChoice) {
                        stateMarker = val === "y" ? "(*)" : "( )";
                      } else if (isTristate) {
                        stateMarker = val === "y" ? "[*]" : (val === "m" ? "<M>" : "[ ]");
                      } else {
                        stateMarker = val === "y" ? "[*]" : "[ ]";
                      }
                    }

                    const title = item.prompt || item.title || sym || "Item";
                    const suffix = hasSubmenu ? "  --->" : "";

                    return `
                      <div class="tui-menu-item ${isSelected ? "selected" : ""}" data-index="${idx}">
                        ${stateMarker} ${title} ${suffix}
                      </div>
                    `;
                  }).join("")
                : `<div style="padding:10px;">(Empty Menu)</div>`
            }
          </div>
          <div class="tui-footer">
            <button class="tui-footer-btn" id="tui-btn-select">&lt; Select &gt;</button>
            <button class="tui-footer-btn" id="tui-btn-exit">&lt; Exit &gt;</button>
            <button class="tui-footer-btn" id="tui-btn-help">&lt; Help &gt;</button>
            <button class="tui-footer-btn" id="tui-btn-save">&lt; Save &gt;</button>
          </div>
        </div>
      </div>
    `;

    // Scroll active item into view
    const selectedEl = this.modalEl.querySelector(".tui-menu-item.selected");
    if (selectedEl) selectedEl.scrollIntoView({ block: "nearest" });

    // Item click & double-click bindings
    this.modalEl.querySelectorAll(".tui-menu-item").forEach((itemEl) => {
      itemEl.onclick = () => {
        const idx = parseInt(itemEl.dataset.index, 10);
        if (!isNaN(idx)) {
          this.selectedIndex = idx;
          this.render();
        }
      };
      itemEl.ondblclick = () => {
        const idx = parseInt(itemEl.dataset.index, 10);
        if (!isNaN(idx)) {
          this.selectedIndex = idx;
          this.handleSelect();
        }
      };
    });

    // Button clicks
    this.modalEl.querySelector("#tui-btn-select").onclick = () => this.handleSelect();
    this.modalEl.querySelector("#tui-btn-exit").onclick = () => this.handleBack();
    this.modalEl.querySelector("#tui-btn-help").onclick = () => this.showHelp();
    this.modalEl.querySelector("#tui-btn-save").onclick = () => this.handleSave();
  }

  attachKeyboard() {
    this.keyHandler = (e) => {
      if (!this.modalEl) {
        if (this.keyHandler) {
          window.removeEventListener("keydown", this.keyHandler);
          this.keyHandler = null;
        }
        return;
      }

      if (e.key === "ArrowDown") {
        e.preventDefault();
        const items = this.currentMenu.children || [];
        if (items.length > 0) {
          this.selectedIndex = (this.selectedIndex + 1) % items.length;
          this.render();
        }
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        const items = this.currentMenu.children || [];
        if (items.length > 0) {
          this.selectedIndex = (this.selectedIndex - 1 + items.length) % items.length;
          this.render();
        }
      } else if (e.key === "Enter") {
        e.preventDefault();
        this.handleSelect();
      } else if (e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        this.handleToggle();
      } else if (e.key === "Escape") {
        e.preventDefault();
        this.handleBack();
      } else if (e.key.toLowerCase() === "s") {
        e.preventDefault();
        this.handleSave();
      } else if (e.key === "?") {
        e.preventDefault();
        this.showHelp();
      }
    };

    window.addEventListener("keydown", this.keyHandler);
  }

  handleSelect() {
    const items = this.currentMenu.children || [];
    const item = items[this.selectedIndex];
    if (!item) return;

    const rawChildList = this.nodesByParent.get(item.tree_id) || [];
    const childList = this.filterPrompted ? this.filterPrompted(rawChildList) : rawChildList;
    if (childList.length > 0) {
      this.menuStack.push({ menu: this.currentMenu, selectedIndex: this.selectedIndex });
      this.currentMenu = { title: item.prompt || item.title || item.symbol_name || "Submenu", children: childList };
      this.selectedIndex = 0;
      this.render();
    } else {
      this.handleToggle();
    }
  }

  handleToggle() {
    const items = this.currentMenu.children || [];
    const item = items[this.selectedIndex];
    if (!item || !item.symbol_name) return;

    const sym = item.symbol_name;
    const isChoice = Boolean(item.choice && item.choice.id);

    if (isChoice) {
      if (this.engine && typeof this.engine.selectChoiceMember === "function") {
        const updated = this.engine.selectChoiceMember(item.choice.id, sym, state.kconfigAssignments);
        state.setKconfigAssignments(updated);
        toast.info(`Selected choice member ${sym}`);
      } else if (this.onSave && typeof this.onSave.setVal === "function") {
        this.onSave.setVal(sym, "y");
      }
    } else {
      const current = this.onSave?.getVal ? this.onSave.getVal(sym) : (state.kconfigAssignments[sym] || "n");
      let nextVal = "y";
      if (this.engine && typeof this.engine.cycleValue === "function") {
        nextVal = this.engine.cycleValue(sym, current, item, state.kconfigAssignments);
      } else {
        nextVal = current === "y" ? "n" : "y";
      }

      if (this.onSave && typeof this.onSave.setVal === "function") {
        this.onSave.setVal(sym, nextVal);
        toast.info(`Set ${sym}=${nextVal}`);
      }
    }
    this.render();
  }

  handleBack() {
    if (this.menuStack.length > 0) {
      const prev = this.menuStack.pop();
      this.currentMenu = prev.menu;
      this.selectedIndex = prev.selectedIndex;
      this.render();
    } else {
      this.hide();
    }
  }

  handleSave() {
    if (this.onSave && typeof this.onSave.save === "function") {
      this.onSave.save();
    }
    toast.success("Saved Linux kernel configuration");
  }

  showHelp() {
    const items = this.currentMenu.children || [];
    const item = items[this.selectedIndex];
    if (!item) return;

    const sym = item.symbol_name || "Option";
    const prompt = item.prompt || item.title || "";
    const help = item.help || "No inline help text documented in Kconfig.";

    alert(`HELP: ${sym}\n\n${prompt}\n\n${help}`);
  }
}
