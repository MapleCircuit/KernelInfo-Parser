/**
 * outline_panel.js - Outline panel, sticky scroll and the breadcrumb's current definition (G03,
 * webapp/API.md §5.21).
 *
 * The outline lists the open file's definitions (GET /api/fs/outline; Kconfig files are outlined from
 * their text) in a side panel with a filter, grouped by kind; a click scrolls to the definition and
 * the one containing the top visible line is highlighted. Sticky scroll pins the first line of the
 * definitions the viewport is inside (two levels at most) over the top of the viewport.
 */
import { api } from "../../api.js";
import { escapeHtml } from "../../components/modal.js";
import {
  KIND_ICONS, currentDefinition, filterOutline, groupOutline, isKconfigPath, kconfigOutline, stickyLines, stickyRanges,
} from "./outline_model.js";

export const STICKY_KEY = "kip-sticky-scroll";

export function stickyEnabled() {
  try {
    return localStorage.getItem(STICKY_KEY) !== "off";
  } catch {
    return true;
  }
}

export class OutlinePanel {
  constructor(editor) {
    this.editor = editor;
    this.items = [];
    this.ranges = [];
    this.indexed = false;
    this.loaded = false;
    this.loadSeq = 0;
    this.filter = "";
    this.panelEl = null;
    this.stickyEl = null;
    this.lastCurrent = null;
  }

  get open() {
    return Boolean(this.editor.tabData?.outlineOpen);
  }

  /** Called after the editor rebuilt its DOM. */
  mount() {
    this.panelEl = null;
    this.stickyEl = null;
    this.lastCurrent = null;
    const body = this.editor.containerEl?.querySelector(".editor-body");
    if (!body) return;
    this.stickyEl = document.createElement("div");
    this.stickyEl.className = "code-sticky";
    this.stickyEl.hidden = true;
    this.stickyEl.addEventListener("click", (e) => {
      const row = e.target.closest("[data-line]");
      if (row) this.editor.scrollToLine(parseInt(row.dataset.line, 10), { select: true });
    });
    // Inside the scrolling viewport, in flow and `position: sticky`: it stays at the top while the
    // absolutely positioned rows scroll under it, and scrolls sideways with them
    const viewport = body.querySelector(".virtual-scroll-viewport");
    if (viewport) viewport.prepend(this.stickyEl);
    if (this.open) this.showPanel();
  }

  /** Fetch (or compute) the outline of the file now loaded in the editor. */
  async load() {
    const seq = ++this.loadSeq;
    const ed = this.editor;
    this.loaded = false;
    this.items = [];
    this.ranges = [];
    let items = [];
    let indexed = false;
    if (isKconfigPath(ed.currentFilePath)) {
      items = kconfigOutline(ed.rawLines);
      indexed = true;
    } else {
      try {
        const res = await (ed.api || api).getOutline(ed.currentVersion, ed.currentFilePath);
        items = res?.items || [];
        indexed = Boolean(res?.indexed);
      } catch {
        indexed = false;
      }
    }
    if (seq !== this.loadSeq) return;
    this.items = items;
    this.indexed = indexed;
    this.ranges = stickyRanges(items);
    this.loaded = true;
    this.updateButton();
    if (this.open) this.renderPanel();
    this.update();
  }

  updateButton() {
    const btn = this.editor.containerEl?.querySelector("#btn-outline");
    if (!btn) return;
    btn.classList.toggle("active", this.open);
    const usable = this.indexed && this.items.length > 0;
    btn.disabled = this.loaded && !usable;
    btn.title = !this.loaded ? "Outline of this file's definitions"
      : usable ? "Outline of this file's definitions (filter, jump, current definition)"
        : this.indexed ? "No definitions in this file" : "Outline needs an indexed file";
  }

  toggle() {
    const next = !this.open;
    this.editor.persistTab({ outlineOpen: next || null });
    if (next) this.showPanel();
    else this.hidePanel();
    this.updateButton();
  }

  showPanel() {
    const body = this.editor.containerEl?.querySelector(".editor-body");
    if (!body) return;
    if (!this.panelEl) {
      this.panelEl = document.createElement("aside");
      this.panelEl.className = "code-outline";
      this.panelEl.innerHTML = `
        <div class="code-outline-head">
          <input type="search" class="code-outline-filter" placeholder="Filter definitions…" spellcheck="false" aria-label="Filter definitions" />
          <button type="button" class="code-outline-close" title="Close the outline">✕</button>
        </div>
        <div class="code-outline-list" role="tree"></div>`;
      const minimap = body.querySelector(".editor-minimap");
      body.insertBefore(this.panelEl, minimap || null);
      const filter = this.panelEl.querySelector(".code-outline-filter");
      filter.value = this.filter;
      filter.addEventListener("input", () => {
        this.filter = filter.value;
        this.renderPanel();
      });
      filter.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          const first = this.panelEl.querySelector(".code-outline-item[data-line]");
          if (first) this.editor.scrollToLine(parseInt(first.dataset.line, 10), { select: true });
        } else if (e.key === "Escape") {
          filter.value = "";
          this.filter = "";
          this.renderPanel();
        }
      });
      this.panelEl.querySelector(".code-outline-close").onclick = () => this.toggle();
      this.panelEl.querySelector(".code-outline-list").addEventListener("click", (e) => {
        const head = e.target.closest(".code-outline-group-head");
        if (head) {
          head.parentElement.classList.toggle("collapsed");
          return;
        }
        const item = e.target.closest(".code-outline-item[data-line]");
        if (item) this.editor.scrollToLine(parseInt(item.dataset.line, 10), { select: true });
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

  renderPanel() {
    const list = this.panelEl?.querySelector(".code-outline-list");
    if (!list) return;
    if (!this.loaded) {
      list.innerHTML = '<div class="code-outline-empty">Loading…</div>';
      return;
    }
    if (!this.indexed || !this.items.length) {
      list.innerHTML = `<div class="code-outline-empty">${this.indexed ? "No definitions in this file." : "Outline needs an indexed file."}</div>`;
      return;
    }
    const items = filterOutline(this.items, this.filter);
    const renderItems = (arr, depth) => arr.map((it) => `
      <div class="code-outline-item kind-${escapeHtml(it.kind)}" data-line="${it.line_s}" data-end="${it.line_e}" style="padding-left:${8 + depth * 14}px" title="${escapeHtml(`${it.type_name} · line ${it.line_s}${it.line_e > it.line_s ? `–${it.line_e}` : ""}`)}">
        <span class="code-outline-icon">${escapeHtml(KIND_ICONS[it.kind] || "•")}</span><span class="code-outline-name">${escapeHtml(it.name)}</span><span class="code-outline-line">${it.line_s}</span>
      </div>${renderItems(it.children || [], depth + 1)}`).join("");
    const groups = groupOutline(items);
    list.innerHTML = groups.length ? groups.map((g) => `
      <div class="code-outline-group">
        <div class="code-outline-group-head">${escapeHtml(g.title)} <span>${g.items.length}</span></div>
        ${renderItems(g.items, 0)}
      </div>`).join("") : '<div class="code-outline-empty">No definition matches.</div>';
    this.lastCurrent = null;
    this.highlightCurrent(this.topLine());
  }

  topLine() {
    const ed = this.editor;
    if (!ed.viewportEl) return 1;
    return ed.fileDiff ? ed.topShownLine() : Math.floor(ed.viewportEl.scrollTop / ed.rowHeight()) + 1;
  }

  /** Scrolled or re-rendered: refresh the sticky rows, the outline highlight and the breadcrumb. */
  update() {
    if (!this.loaded) return;
    const top = this.topLine();
    this.updateSticky(top);
    this.highlightCurrent(top);
  }

  highlightCurrent(line) {
    const cur = currentDefinition(this.ranges, line);
    const key = cur ? `${cur.line_s}:${cur.name}` : "";
    if (key === this.lastCurrent) return;
    this.lastCurrent = key;
    // Breadcrumb: "› copy_process" for the definition the viewport is in
    const crumbs = this.editor.containerEl?.querySelector("#code-breadcrumbs");
    if (crumbs) {
      let el = crumbs.querySelector(".breadcrumb-symbol");
      if (!cur) {
        el?.remove();
      } else {
        if (!el) {
          el = document.createElement("span");
          el.className = "breadcrumb-symbol";
          el.addEventListener("click", () => this.editor.scrollToLine(parseInt(el.dataset.line, 10), { select: true }));
          crumbs.appendChild(el);
        }
        el.dataset.line = String(cur.line_s);
        el.textContent = `› ${cur.name}`;
        el.title = `Go to the start of ${cur.name} (line ${cur.line_s})`;
      }
    }
    if (!this.panelEl) return;
    this.panelEl.querySelectorAll(".code-outline-item.current").forEach((x) => x.classList.remove("current"));
    if (cur) {
      const el = this.panelEl.querySelector(`.code-outline-item[data-line="${cur.line_s}"][data-end="${cur.line_e}"]`);
      if (el) {
        el.classList.add("current");
        el.scrollIntoView({ block: "nearest" });
      }
    }
  }

  updateSticky(top) {
    const el = this.stickyEl;
    if (!el) return;
    const ed = this.editor;
    if (!stickyEnabled() || !this.ranges.length || ed.docMode === "rendered") {
      el.hidden = true;
      return;
    }
    // In a diff, only rows of the tab's own file carry line numbers the outline knows
    if (ed.fileDiff) {
      const rowTop = Math.floor((ed.viewportEl?.scrollTop || 0) / ed.rowHeight());
      if (!ed.diffShownLineAt(rowTop)) {
        el.hidden = true;
        return;
      }
    }
    const pinned = stickyLines(this.ranges, top, 2);
    if (!pinned.length) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    el.innerHTML = pinned.map((r) => {
      const text = ed.rawLines[r.line_s - 1] ?? "";
      return `<div class="code-row code-sticky-row" data-line="${r.line_s}" title="${escapeHtml(r.name)} (line ${r.line_s})">
        <div class="gutter-line-no">${r.line_s}</div><div class="code-text">${ed.astOverlay.renderLineHtml(r.line_s, text, false)}</div></div>`;
    }).join("");
  }
}
