/**
 * virtual_list.js - Fixed-row-height windowed list (S05, webapp/API.md §5.29).
 *
 * Only the rows in view (plus an overscan margin) exist in the DOM, so lists of 10⁵ rows scroll
 * smoothly. Rows are rendered by a callback; ↑/↓/PageUp/PageDown/Home/End move the selection, Enter
 * activates it; the active row is announced through `aria-activedescendant`. The viewport carries
 * `data-scroll-restore="off"` (§5.28): the owner keeps the position itself (`scrollTop`).
 */

/** [start, end) of the rows to render for a viewport. Pure (webapp/frontend/tests/virtual_list.test.mjs). */
export function visibleRange(scrollTop, viewportHeight, rowHeight, total, overscan = 8) {
  if (!total || rowHeight <= 0) return [0, 0];
  const first = Math.floor(Math.max(0, scrollTop) / rowHeight);
  const count = Math.ceil(Math.max(0, viewportHeight) / rowHeight) + 1;
  const start = Math.max(0, first - overscan);
  const end = Math.min(total, first + count + overscan);
  return [start, Math.max(start, end)];
}

/** Next selected index for a key (or null when the key is not a navigation key). */
export function stepIndex(key, index, total, pageRows = 10) {
  if (!total) return null;
  const cur = index < 0 ? -1 : index;
  switch (key) {
    case "ArrowDown": return Math.min(total - 1, cur + 1);
    case "ArrowUp": return Math.max(0, cur < 0 ? 0 : cur - 1);
    case "PageDown": return Math.min(total - 1, Math.max(0, cur) + pageRows);
    case "PageUp": return Math.max(0, Math.max(0, cur) - pageRows);
    case "Home": return 0;
    case "End": return total - 1;
    default: return null;
  }
}

/** scrollTop that brings row `index` into view (unchanged when already visible). */
export function scrollToShow(index, scrollTop, viewportHeight, rowHeight) {
  const top = index * rowHeight;
  if (top < scrollTop) return top;
  if (top + rowHeight > scrollTop + viewportHeight) return top + rowHeight - viewportHeight;
  return scrollTop;
}

let listSeq = 0;

export class VirtualList {
  constructor(hostEl, { rowHeight = 28, renderRow, rowKey = (r, i) => String(i), onSelect = null, onActivate = null, label = "List" }) {
    this.host = hostEl;
    this.rowHeight = rowHeight;
    this.renderRow = renderRow;
    this.rowKey = rowKey;
    this.onSelect = onSelect;
    this.onActivate = onActivate;
    this.rows = [];
    this.selected = -1;
    this.id = `vl${++listSeq}`;
    hostEl.innerHTML = `<div class="vl-viewport" tabindex="0" role="listbox" aria-label="${label.replace(/"/g, "&quot;")}" data-scroll-restore="off">
      <div class="vl-spacer"></div><div class="vl-rows"></div></div>`;
    this.viewport = hostEl.querySelector(".vl-viewport");
    this.spacer = hostEl.querySelector(".vl-spacer");
    this.rowsEl = hostEl.querySelector(".vl-rows");
    this.viewport.addEventListener("scroll", () => this.paint());
    this.viewport.addEventListener("click", (e) => this.onClick(e, false));
    this.viewport.addEventListener("auxclick", (e) => {
      if (e.button === 1) this.onClick(e, true);
    });
    this.viewport.addEventListener("keydown", (e) => this.onKey(e));
    if (typeof ResizeObserver === "function") {
      this.ro = new ResizeObserver(() => this.paint());
      this.ro.observe(this.viewport);
    }
  }

  setRows(rows, { keepScroll = false, selectKey = null } = {}) {
    this.rows = rows || [];
    this.spacer.style.height = `${this.rows.length * this.rowHeight}px`;
    this.selected = selectKey != null ? this.rows.findIndex((r, i) => this.rowKey(r, i) === selectKey) : -1;
    if (!keepScroll) this.viewport.scrollTop = 0;
    if (this.selected >= 0) this.reveal(this.selected, true);
    this.paint();
  }

  paint() {
    const [start, end] = visibleRange(this.viewport.scrollTop, this.viewport.clientHeight || 400, this.rowHeight, this.rows.length);
    this.rowsEl.style.transform = `translateY(${start * this.rowHeight}px)`;
    let html = "";
    for (let i = start; i < end; i++) {
      const sel = i === this.selected;
      html += `<div class="vl-row${sel ? " selected" : ""}" role="option" id="${this.id}-${i}" data-index="${i}" aria-selected="${sel}" style="height:${this.rowHeight}px">${this.renderRow(this.rows[i], i, sel)}</div>`;
    }
    this.rowsEl.innerHTML = html;
    if (this.selected >= 0) this.viewport.setAttribute("aria-activedescendant", `${this.id}-${this.selected}`);
    else this.viewport.removeAttribute("aria-activedescendant");
  }

  reveal(index, center = false) {
    const vh = this.viewport.clientHeight || 400;
    this.viewport.scrollTop = center
      ? Math.max(0, index * this.rowHeight - vh / 2)
      : scrollToShow(index, this.viewport.scrollTop, vh, this.rowHeight);
  }

  select(index, { notify = true, event = null, forceNew = false } = {}) {
    if (index < 0 || index >= this.rows.length) return;
    this.selected = index;
    this.reveal(index);
    this.paint();
    if (notify && this.onSelect) this.onSelect(this.rows[index], index, { event, forceNew });
  }

  onClick(e, forceNew) {
    const row = e.target.closest(".vl-row");
    if (!row || e.target.closest("a[href]:not([href='#']), button")) return;
    if (forceNew) e.preventDefault();
    this.select(parseInt(row.dataset.index, 10), { event: e, forceNew: forceNew || e.ctrlKey || e.metaKey });
  }

  onKey(e) {
    if (e.key === "Enter" && this.selected >= 0) {
      e.preventDefault();
      if (this.onActivate) this.onActivate(this.rows[this.selected], this.selected, { event: e });
      return;
    }
    const next = stepIndex(e.key, this.selected, this.rows.length, Math.max(1, Math.floor((this.viewport.clientHeight || 400) / this.rowHeight) - 1));
    if (next === null) return;
    e.preventDefault();
    this.select(next);
  }

  dispose() {
    this.ro?.disconnect();
  }
}
