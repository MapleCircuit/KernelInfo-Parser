/**
 * menuconfig_tui.js - Terminal-style menuconfig (mconf look and keys) over the KConfig view's engine.
 *
 * Menus follow mconf: `menu`, `menuconfig` and `choice` entries open submenus ("--->"), other
 * entries that own children (implicit submenus: options depending on the previous option) list
 * them indented in the same menu. Values change only through the view, so the engine's rules apply.
 *
 * Keys: Up/Down/PgUp/PgDn/Home/End move, Enter opens a submenu or changes the value, Space cycles,
 * Y/M/N set a value, Esc or Left goes back, / searches, ? or H shows help, Z toggles "show all",
 * S saves .config, D saves a minimal defconfig.
 */
import { toast } from "../../components/toast.js";
import { escapeHtml } from "../../components/modal.js";
import { api } from "../../api.js";

export class MenuconfigTui {
  constructor(view) {
    this.view = view;
    this.engine = view.engine;
    this.stack = [{ nodeId: 0, title: this.engine.model.mainmenu || "Linux Kernel Configuration", selectedIndex: 0 }];
    this.searchResults = null;
    this.modalEl = null;
    this.keyHandler = null;
    this.helpOpen = false;
  }

  get current() {
    return this.stack[this.stack.length - 1];
  }

  show() {
    this.modalEl = document.createElement("div");
    this.modalEl.className = "menuconfig-tui-modal";
    document.body.appendChild(this.modalEl);
    this.render();
    this.attachKeyboard();
  }

  hide() {
    if (this.keyHandler) window.removeEventListener("keydown", this.keyHandler, true);
    this.keyHandler = null;
    this.modalEl?.remove();
    this.modalEl = null;
    this.view.renderTree();
  }

  /** Entries of the current menu: [{ node, depth }], implicit children inlined like mconf. */
  items() {
    if (this.current.search) return this.current.search.map((node) => ({ node, depth: 0 }));
    const out = [];
    const walk = (node, depth) => {
      for (const child of this.view.visibleChildren(node)) {
        out.push({ node: child, depth });
        if (!this.opensSubmenu(child)) walk(child, depth + 1);
      }
    };
    walk(this.view.nodes[this.current.nodeId], 0);
    return out;
  }

  opensSubmenu(node) {
    return node.kind === "menu" || node.kind === "menuconfig" || node.kind === "choice";
  }

  line(item) {
    const { node, depth } = item;
    const d = this.view.describe(node);
    const marker = d.marker ? `${d.marker.padEnd(3, " ")} ` : "    ";
    const indent = "  ".repeat(depth);
    const sub = this.opensSubmenu(node) && (node.kind !== "menuconfig" || this.view.visibleChildren(node).length) ? "  --->" : "";
    const name = node.sym && !this.engine.syms[node.sym]?.is_choice ? ` <span class="tui-sym">${escapeHtml(node.sym)}</span>` : "";
    return `${escapeHtml(marker)}${indent}${escapeHtml(d.title || "")}${escapeHtml(sub)}${name}`;
  }

  render() {
    if (!this.modalEl) return;
    const items = this.items();
    const cur = this.current;
    cur.selectedIndex = Math.min(cur.selectedIndex, Math.max(0, items.length - 1));
    const env = this.engine.model.env || {};
    this.modalEl.innerHTML = `
      <div class="tui-header">.config - ${escapeHtml(this.engine.model.mainmenu || "")}${this.view.engineActive ? "" : " — ENGINE OFF (unvalidated)"}</div>
      <div class="tui-body">
        <div class="tui-window">
          <div class="tui-window-title">${escapeHtml(cur.title)}</div>
          <div class="tui-instructions">
            Arrow keys navigate the menu. &lt;Enter&gt; selects submenus ---&gt; (or changes the value). Highlighted letters are hotkeys.
            Pressing &lt;Y&gt; includes, &lt;N&gt; excludes, &lt;M&gt; modularizes features. &lt;Esc&gt; goes back, &lt;/&gt; searches, &lt;?&gt; help,
            &lt;Z&gt; ${this.view.showUnmet ? "hides" : "shows"} invisible entries. Legend: [*] built-in [ ] excluded &lt;M&gt; module -*- forced by select.
            ARCH=${escapeHtml(env.ARCH || "")}
          </div>
          <div class="tui-menu-list" id="tui-menu-list">
            ${items.length
              ? items.map((item, idx) => `<div class="tui-menu-item ${idx === cur.selectedIndex ? "selected" : ""} ${this.engine.menuVisible(item.node.id) ? "" : "tui-invisible"}" data-index="${idx}">${this.line(item)}</div>`).join("")
              : `<div style="padding:10px;">(no visible entries)</div>`}
          </div>
          <div class="tui-footer">
            <button class="tui-footer-btn" id="tui-btn-select">&lt;Select&gt;</button>
            <button class="tui-footer-btn" id="tui-btn-exit">&lt; Exit &gt;</button>
            <button class="tui-footer-btn" id="tui-btn-help">&lt; Help &gt;</button>
            <button class="tui-footer-btn" id="tui-btn-save">&lt; Save &gt;</button>
            <button class="tui-footer-btn" id="tui-btn-defconfig">&lt;Save defconfig&gt;</button>
          </div>
        </div>
      </div>
      <div class="tui-help" id="tui-help" style="display:none;"></div>
    `;
    this.modalEl.querySelector(".tui-menu-item.selected")?.scrollIntoView({ block: "nearest" });
    this.modalEl.querySelectorAll(".tui-menu-item").forEach((el) => {
      el.onclick = () => {
        cur.selectedIndex = parseInt(el.dataset.index, 10);
        this.render();
      };
      el.ondblclick = () => {
        cur.selectedIndex = parseInt(el.dataset.index, 10);
        this.handleEnter();
      };
    });
    this.modalEl.querySelector("#tui-btn-select").onclick = () => this.handleEnter();
    this.modalEl.querySelector("#tui-btn-exit").onclick = () => this.handleBack();
    this.modalEl.querySelector("#tui-btn-help").onclick = () => this.showHelp();
    this.modalEl.querySelector("#tui-btn-save").onclick = () => this.view.exportConfig();
    this.modalEl.querySelector("#tui-btn-defconfig").onclick = () => this.view.exportDefconfig();
  }

  selectedItem() {
    return this.items()[this.current.selectedIndex];
  }

  attachKeyboard() {
    this.keyHandler = (e) => {
      if (!this.modalEl) return;
      if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return;
      // A dialog over the TUI (e.g. confirming a change that lowers other options) gets the keys
      if (document.querySelector(".ki-modal-backdrop")) return;
      if (this.helpOpen) {
        if (["Escape", "Enter", " ", "q", "?", "h"].includes(e.key)) {
          e.preventDefault();
          e.stopPropagation();
          this.closeHelp();
        }
        return;
      }
      const items = this.items();
      const cur = this.current;
      const key = e.key;
      const move = (delta) => {
        if (!items.length) return;
        cur.selectedIndex = Math.max(0, Math.min(items.length - 1, cur.selectedIndex + delta));
        this.render();
      };
      let handled = true;
      switch (key) {
        case "ArrowDown": move(1); break;
        case "ArrowUp": move(-1); break;
        case "PageDown": move(15); break;
        case "PageUp": move(-15); break;
        case "Home": move(-items.length); break;
        case "End": move(items.length); break;
        case "Enter": this.handleEnter(); break;
        case "ArrowRight": this.handleEnter(true); break;
        case " ": this.handleToggle(); break;
        case "Escape":
        case "ArrowLeft": this.handleBack(); break;
        case "/": this.handleSearch(); break;
        case "?": this.showHelp(); break;
        default: {
          const k = key.toLowerCase();
          if (k === "y" || k === "m" || k === "n") this.handleSet(k);
          else if (k === "h") this.showHelp();
          else if (k === "z") {
            this.view.showUnmet = !this.view.showUnmet;
            this.render();
          } else if (k === "s") this.view.exportConfig();
          else if (k === "d") this.view.exportDefconfig();
          else handled = false;
        }
      }
      if (handled) {
        e.preventDefault();
        e.stopPropagation();
      }
    };
    window.addEventListener("keydown", this.keyHandler, true);
  }

  enter(node) {
    const d = this.view.describe(node);
    this.stack.push({ nodeId: node.id, title: d.title || node.prompt || node.sym || "", selectedIndex: 0 });
    this.render();
  }

  handleEnter(submenuOnly = false) {
    const item = this.selectedItem();
    if (!item) return;
    const { node } = item;
    if (this.opensSubmenu(node) && this.view.visibleChildren(node).length) {
      this.enter(node);
      return;
    }
    if (!submenuOnly) this.handleToggle();
  }

  async handleToggle() {
    const item = this.selectedItem();
    if (!item || !item.node.sym || this.engine.syms[item.node.sym]?.is_choice) return;
    await this.view.changeValue(item.node);
    this.render();
  }

  async handleSet(value) {
    const item = this.selectedItem();
    const sym = item?.node.sym;
    const s = sym && this.engine.syms[sym];
    if (!s || (s.type !== "bool" && s.type !== "tristate") || s.is_choice) return;
    await this.view.applyValue(sym, value);
    this.render();
  }

  handleBack() {
    if (this.stack.length > 1) {
      this.stack.pop();
      this.render();
    } else {
      this.hide();
    }
  }

  handleSearch() {
    const q = window.prompt("Search for CONFIG_ symbol or prompt text:");
    if (!q || !q.trim()) return;
    const needle = q.trim().replace(/^CONFIG_/i, "").toLowerCase();
    const results = this.view.nodes.filter((n) => n.prompt && ((n.sym || "").toLowerCase().includes(needle) || n.prompt.toLowerCase().includes(needle)));
    if (!results.length) {
      toast.info(`No match for "${q}"`);
      return;
    }
    this.stack.push({ nodeId: 0, title: `Search results for "${q}" (${results.length})`, selectedIndex: 0, search: results.slice(0, 300) });
    this.render();
  }

  closeHelp() {
    this.helpOpen = false;
    const el = this.modalEl?.querySelector("#tui-help");
    if (el) el.style.display = "none";
  }

  /** mconf-style help: help text, then Symbol/Type/Prompt/Defined at/Depends on/Location/Selects/Selected by. */
  async showHelp() {
    const item = this.selectedItem();
    if (!item) return;
    const { node } = item;
    const e = this.engine;
    const el = this.modalEl.querySelector("#tui-help");
    this.helpOpen = true;
    el.style.display = "block";
    const sym = node.sym && !e.syms[node.sym]?.is_choice ? node.sym : null;
    const lines = [];
    if (sym) {
      const s = e.syms[sym];
      lines.push(`CONFIG_${sym}:`, "", "(loading help…)", "");
      lines.push(`Symbol: ${sym} [=${this.view.valueOf(sym)}]`);
      lines.push(`Type  : ${s.type}`);
      for (const id of s.menus) {
        const n = e.nodes[id];
        if (n.prompt) lines.push(`Prompt: ${n.prompt}`);
        lines.push(`  Defined at ${n.file}:${n.line}`);
        if (n.dep >= 0) lines.push(`  Depends on: ${e.exprStr(n.dep, true)}`);
        lines.push("  Location:");
        ["Main menu", ...e.menuPath(id)].forEach((p, i) => lines.push(`${"  ".repeat(i + 2)}-> ${p}`));
      }
      if (s.selects.length) lines.push(`  Selects: ${s.selects.map(([t]) => `${t} [=${e.isDefined(t) ? e.value(t) : "n"}]`).join(" && ")}`);
      const rev = e.orTerms(s.rev_dep).map((id) => e.exprStr(id, true)).filter((t) => !t.endsWith("&& m"));
      if (rev.length) lines.push(`  Selected by: ${rev.join(" || ")}`);
    } else {
      lines.push(node.prompt || "", "", "There is no help available for this menu.");
    }
    const paint = (help) => {
      if (!this.helpOpen) return;
      const body = sym ? lines.map((l) => (l === "(loading help…)" ? help : l)) : lines;
      el.innerHTML = `<div class="tui-help-window"><div class="tui-window-title">${escapeHtml(node.prompt || sym || "Help")}</div>
        <pre>${escapeHtml(body.join("\n"))}</pre><div class="tui-footer"><button class="tui-footer-btn" id="tui-help-exit">&lt; Exit &gt;</button></div></div>`;
      el.querySelector("#tui-help-exit").onclick = () => this.closeHelp();
    };
    paint(sym ? "(loading help…)" : "");
    if (sym) {
      try {
        const detail = await api.getKconfigSymbol(this.view.currentVersion, sym, this.view.currentArch, this.view.toolchain);
        paint(detail.help || "There is no help available for this option.");
      } catch {
        paint("(help unavailable)");
      }
    }
  }
}
