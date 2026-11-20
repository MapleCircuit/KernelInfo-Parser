/**
 * imports_panel.js - Imports panel of the code view (webapp/API_FRONTEND.md §5.21).
 *
 * Lists the symbols the open C / header / assembly file references that headers on its `#include` chain
 * define (GET /api/fs/imports, for the ARCH of the KConfig engine the tab follows, x86_64 without one),
 * in a side panel docked in `.editor-body` with a search box and kind pills. Every entry shows the header
 * and line defining the symbol and the include chain reaching it (compiler-forced `-include` chains are
 * badged); a click opens the definition or a header of the chain (middle-click / Ctrl+click: new tab),
 * the reference line jumps within the open file. The Outline and Imports panels share the side of the
 * editor: opening one closes the other. Fetched only while open; a file or ARCH change refetches.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { escapeHtml } from "../../components/modal.js";
import {
  IMPORT_FILTERS, chainSegments, countByFilter, emptyStateMessage, filterImports, formatChain, groupImports,
  isImportsPath, kindIcon, kindLabel, basename,
} from "./imports_model.js";

const DEFAULT_ARCH = "x86_64";

export class ImportsPanel {
  constructor(editor) {
    this.editor = editor;
    this.payload = null;
    this.loading = false;
    this.failed = false;
    this.loadSeq = 0;
    this.loadedKey = null;
    this.query = "";
    this.kind = "all";
    this.panelEl = null;
  }

  get open() {
    return Boolean(this.editor.tabData?.importsOpen);
  }

  /** ARCH of the KConfig engine this tab follows (x86_64 without one). */
  arch() {
    const id = this.editor.engineTabId;
    const engine = id ? state.kconfigEngineList().find((e) => e.tabId === id) : null;
    return engine?.arch || DEFAULT_ARCH;
  }

  eligible() {
    return isImportsPath(this.editor.currentFilePath || "");
  }

  key() {
    const ed = this.editor;
    return `${ed.currentVersion}|${ed.currentFilePath}|${this.arch()}`;
  }

  /** Called after the editor rebuilt its DOM; the outline keeps the side when both were left open. */
  mount() {
    this.panelEl = null;
    if (this.open && this.editor.outline?.open) this.editor.persistTab({ importsOpen: null });
    if (this.open) this.showPanel();
    this.updateButton();
  }

  /** A new file is loaded: refetch when open, otherwise forget the previous file's answer. */
  fileChanged() {
    this.payload = null;
    this.loadedKey = null;
    this.failed = false;
    if (this.open) this.load();
    else this.updateButton();
  }

  /** The followed engine (or its ARCH) may have changed. */
  archChanged() {
    if (this.open && this.loadedKey !== null && this.loadedKey !== this.key()) this.load();
    this.updateButton();
  }

  async load() {
    const seq = ++this.loadSeq;
    const ed = this.editor;
    const key = this.key();
    this.payload = null;
    this.failed = false;
    this.loadedKey = key;
    if (!this.eligible()) {
      this.loading = false;
      this.renderPanel();
      this.updateButton();
      return;
    }
    this.loading = true;
    this.renderPanel();
    let payload = null;
    let failed = false;
    try {
      payload = await (ed.api || api).getFileImports(ed.currentVersion, ed.currentFilePath, this.arch());
    } catch {
      failed = true;
    }
    if (seq !== this.loadSeq) return;
    this.loading = false;
    this.payload = payload;
    this.failed = failed;
    this.renderPanel();
    this.updateButton();
  }

  updateButton() {
    const btn = this.editor.containerEl?.querySelector("#btn-imports");
    if (!btn) return;
    btn.classList.toggle("active", this.open);
    const eligible = this.eligible();
    btn.disabled = !eligible;
    const n = this.payload?.total;
    btn.title = !eligible ? "Imports are available for C, header and assembly files"
      : `Symbols this file uses from its #include chain (ARCH=${this.arch()})${Number.isFinite(n) ? `: ${n}` : ""}`;
  }

  toggle() {
    if (this.open) this.close();
    else this.show();
  }

  show() {
    this.editor.outline?.close?.();
    this.editor.persistTab({ importsOpen: true });
    this.showPanel();
    if (this.loadedKey !== this.key()) this.load();
    this.updateButton();
  }

  close() {
    if (!this.open && !this.panelEl) return;
    this.editor.persistTab({ importsOpen: null });
    this.hidePanel();
    this.updateButton();
  }

  showPanel() {
    const body = this.editor.containerEl?.querySelector(".editor-body");
    if (!body) return;
    if (!this.panelEl) {
      this.panelEl = document.createElement("aside");
      this.panelEl.className = "code-imports";
      this.panelEl.setAttribute("aria-label", "Imported symbols");
      this.panelEl.innerHTML = `
        <div class="code-imports-head">
          <input type="search" class="code-imports-filter" placeholder="Filter symbols or headers…" spellcheck="false" aria-label="Filter imported symbols or headers" />
          <button type="button" class="code-imports-close" title="Close the imports panel">✕</button>
        </div>
        <div class="code-imports-pills" role="toolbar" aria-label="Kind filter"></div>
        <div class="code-imports-list" role="list"></div>`;
      const minimap = body.querySelector(".editor-minimap");
      body.insertBefore(this.panelEl, minimap || null);
      const filter = this.panelEl.querySelector(".code-imports-filter");
      filter.value = this.query;
      filter.addEventListener("input", () => {
        this.query = filter.value;
        this.renderPanel();
      });
      filter.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          const first = this.panelEl.querySelector(".code-imports-item[data-open]");
          if (first) this.openPath(first.dataset.open, first.dataset.line, e.ctrlKey || e.metaKey);
        } else if (e.key === "Escape") {
          filter.value = "";
          this.query = "";
          this.renderPanel();
        }
      });
      this.panelEl.querySelector(".code-imports-close").onclick = () => this.close();
      this.panelEl.querySelector(".code-imports-pills").addEventListener("click", (e) => {
        const pill = e.target.closest("[data-kind]");
        if (!pill) return;
        this.kind = pill.dataset.kind;
        this.renderPanel();
      });
      const list = this.panelEl.querySelector(".code-imports-list");
      list.addEventListener("click", (e) => this.onListClick(e, e.ctrlKey || e.metaKey));
      list.addEventListener("auxclick", (e) => {
        if (e.button === 1) this.onListClick(e, true);
      });
    }
    this.renderPanel();
    this.editor.renderMinimap?.();
  }

  hidePanel() {
    this.panelEl?.remove();
    this.panelEl = null;
    this.editor.renderMinimap?.();
  }

  onListClick(e, forceNew) {
    const head = e.target.closest(".code-imports-group-head");
    if (head) {
      head.parentElement.classList.toggle("collapsed");
      return;
    }
    const ref = e.target.closest("[data-ref-line]");
    if (ref) {
      e.preventDefault();
      this.editor.scrollToLine(parseInt(ref.dataset.refLine, 10), { select: true });
      return;
    }
    const target = e.target.closest("[data-open]");
    if (!target) return;
    e.preventDefault();
    e.stopPropagation();
    this.openPath(target.dataset.open, target.dataset.line, forceNew);
  }

  openPath(path, line, forceNew = false) {
    if (!path) return;
    state.openTab({
      type: "code", title: basename(path), path, cursorLine: Number(line) || 1, version: this.editor.currentVersion, forceNew,
    });
  }

  renderPills(items) {
    const el = this.panelEl?.querySelector(".code-imports-pills");
    if (!el) return;
    const counts = countByFilter(items, this.query);
    el.innerHTML = IMPORT_FILTERS.map((f) => `
      <button type="button" class="code-imports-pill${f.id === this.kind ? " active" : ""}" data-kind="${escapeHtml(f.id)}" aria-pressed="${f.id === this.kind}"${f.id !== "all" && !counts[f.id] ? " disabled" : ""}>${escapeHtml(f.label)} <span>${Number(counts[f.id]) || 0}</span></button>`).join("");
  }

  renderChain(item) {
    const current = this.editor.currentFilePath;
    const links = chainSegments(item).map((seg) => {
      const cls = `code-imports-hop${seg.isTarget ? " target" : ""}`;
      if (seg.path === current) return `<span class="${cls} current" title="${escapeHtml(seg.path)} (this file)">${escapeHtml(seg.label)}</span>`;
      return `<a href="#" class="${cls}" data-open="${escapeHtml(seg.path)}" title="Open ${escapeHtml(seg.path)}">${escapeHtml(seg.label)}</a>`;
    }).join('<span class="code-imports-arrow">→</span>');
    const context = item.via === "context"
      ? `<span class="code-imports-context" title="${escapeHtml(`Defined by what ${item.context || "an including file"} includes before this file (this file relies on its includer)`)}">context</span>` : "";
    const badge = context + (item.forced ? '<span class="code-imports-forced" title="Reached through a compiler-forced -include header">-include</span>' : "");
    return `<div class="code-imports-chain" title="${escapeHtml(formatChain(item, { full: true }))}">${badge}${links}</div>`;
  }

  renderItem(it) {
    const refs = Array.isArray(it.ref_lines) ? it.ref_lines : [];
    const refTitle = refs.length
      ? `Referenced at line${it.ref_count > 1 ? "s" : ""} ${refs.join(", ")}${it.ref_count > refs.length ? ", …" : ""}` : "";
    const alt = it.alternatives > 0 ? ` · also defined in ${Number(it.alternatives)} other header${it.alternatives > 1 ? "s" : ""}` : "";
    return `
      <div class="code-imports-item kind-${escapeHtml(it.kind)}" role="listitem" data-open="${escapeHtml(it.header_path)}" data-line="${Number(it.line) || 1}"
        title="${escapeHtml(`${kindLabel(it.kind)} ${it.name} · ${it.type_name} · ${it.header_path}:${it.line}${alt}`)}">
        <div class="code-imports-row">
          <span class="code-imports-icon">${escapeHtml(kindIcon(it.kind))}</span>
          <span class="code-imports-name">${escapeHtml(it.name)}</span>
          ${it.ref_line ? `<button type="button" class="code-imports-ref" data-ref-line="${Number(it.ref_line)}" title="${escapeHtml(refTitle)}">L${Number(it.ref_line)}${it.ref_count > 1 ? ` ×${Number(it.ref_count)}` : ""}</button>` : ""}
        </div>
        <div class="code-imports-loc">${escapeHtml(basename(it.header_path))}:${Number(it.line) || 0}</div>
        ${this.renderChain(it)}
      </div>`;
  }

  renderPanel() {
    const list = this.panelEl?.querySelector(".code-imports-list");
    if (!list) return;
    const items = this.payload?.items || [];
    this.renderPills(items);
    const path = this.editor.currentFilePath || "";
    if (this.loading) {
      list.innerHTML = '<div class="code-imports-empty">Resolving #include chain…</div>';
      return;
    }
    if (this.failed) {
      list.innerHTML = '<div class="code-imports-empty">Imports could not be loaded.</div>';
      return;
    }
    const empty = emptyStateMessage(this.payload, { path });
    if (empty || !this.payload) {
      list.innerHTML = `<div class="code-imports-empty">${escapeHtml(empty || "Loading…")}</div>`;
      return;
    }
    const groups = groupImports(filterImports(items, { query: this.query, kind: this.kind }), "kind");
    const note = this.payload.truncated
      ? `<div class="code-imports-note">Showing ${items.length} of ${Number(this.payload.total) || 0} symbols.</div>` : "";
    list.innerHTML = (groups.length ? groups.map((g) => `
      <div class="code-imports-group">
        <div class="code-imports-group-head">${escapeHtml(g.label)} <span>${g.items.length}</span></div>
        ${g.items.map((it) => this.renderItem(it)).join("")}
      </div>`).join("") : '<div class="code-imports-empty">No imported symbol matches.</div>') + note;
  }
}
