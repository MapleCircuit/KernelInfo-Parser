/**
 * find_bar.js - Find in file for the code view (webapp/API.md §5.21, Find in file).
 *
 * The code view is virtualized: only the rows on screen exist in the DOM, so the browser's own find
 * misses everything else. This bar searches the whole file (or every row of a diff) with
 * find_model.js, walks the matches, highlights them on the rendered rows by splitting their text
 * nodes (any token structure works: the text nodes hold the raw text), and marks the matching rows
 * on the minimap.
 *
 * The query and the toggles live on the tab (`tab.find = {query, caseSensitive, wholeWord, regex,
 * open}`), so the bar survives re-renders; they are not part of the URL.
 */
import {
  FIND_MATCH_CAP, buildMatcher, countLabel, findAllChunked, findInRows, firstMatchFrom, groupByRow,
  rangesToNodePieces, searchRows, stepMatch,
} from "./find_model.js";

const DEBOUNCE_MS = 120;
const SYNC_ROW_LIMIT = 50000;
const MINIMAP_MATCH_COLOR = "rgba(234, 179, 8, 0.95)";

export class FindBar {
  constructor(editor) {
    this.editor = editor;
    this.el = null;
    this.matches = [];
    this.byRow = new Map();
    this.capped = false;
    this.current = -1;
    this.error = null;
    this.seq = 0;
    this.timer = null;
  }

  get state() {
    const tab = this.editor.tabData;
    if (!tab) return { query: "", caseSensitive: false, wholeWord: false, regex: false, open: false };
    if (!tab.find) tab.find = { query: "", caseSensitive: false, wholeWord: false, regex: false, open: false };
    return tab.find;
  }

  get isOpen() {
    return Boolean(this.el && this.el.isConnected);
  }

  /** Re-attach after the editor re-rendered its DOM (the bar stays open across re-renders). */
  mount() {
    this.el = null;
    this.matches = [];
    this.byRow = new Map();
    this.current = -1;
    if (this.state.open) this.open(null, { focus: false });
  }

  open(prefill = null, { focus = true } = {}) {
    const st = this.state;
    if (typeof prefill === "string" && prefill && !prefill.includes("\n")) {
      st.query = prefill.slice(0, 500);
    }
    st.open = true;
    const body = this.editor.containerEl?.querySelector(".editor-body");
    if (!body) return;
    if (!this.isOpen) {
      this.el = document.createElement("div");
      this.el.className = "code-find-bar";
      this.el.setAttribute("role", "search");
      this.el.innerHTML = `
        <input type="text" class="code-find-input" placeholder="Find in file" spellcheck="false" autocomplete="off" aria-label="Find in file" />
        <button type="button" class="code-find-toggle" data-opt="caseSensitive" title="Match case" aria-pressed="false">Aa</button>
        <button type="button" class="code-find-toggle" data-opt="wholeWord" title="Match whole word" aria-pressed="false">ab|</button>
        <button type="button" class="code-find-toggle" data-opt="regex" title="Regular expression (JavaScript syntax)" aria-pressed="false">.*</button>
        <span class="code-find-count" aria-live="polite"></span>
        <button type="button" class="code-find-nav" data-nav="-1" title="Previous match (Shift+Enter, Shift+F3)">↑</button>
        <button type="button" class="code-find-nav" data-nav="1" title="Next match (Enter, F3)">↓</button>
        <button type="button" class="code-find-close" title="Close (Escape)">✕</button>
        <div class="code-find-error" hidden></div>`;
      body.appendChild(this.el);
      const input = this.el.querySelector(".code-find-input");
      input.addEventListener("input", () => {
        this.state.query = input.value;
        this.schedule();
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          this.step(e.shiftKey ? -1 : 1);
        } else if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          this.close();
        }
      });
      this.el.querySelectorAll(".code-find-toggle").forEach((btn) => {
        btn.onclick = () => {
          const opt = btn.dataset.opt;
          this.state[opt] = !this.state[opt];
          this.syncToggles();
          this.run();
        };
      });
      this.el.querySelectorAll(".code-find-nav").forEach((btn) => {
        btn.onclick = () => this.step(parseInt(btn.dataset.nav, 10));
      });
      this.el.querySelector(".code-find-close").onclick = () => this.close();
    }
    const input = this.el.querySelector(".code-find-input");
    input.value = st.query || "";
    this.syncToggles();
    if (focus) {
      input.focus();
      input.select();
    }
    this.run();
  }

  close() {
    const st = this.state;
    st.open = false;
    this.seq++;
    clearTimeout(this.timer);
    this.el?.remove();
    this.el = null;
    this.matches = [];
    this.byRow = new Map();
    this.current = -1;
    this.editor.updateVisibleLines();
    this.editor.renderMinimap();
    this.editor.viewportEl?.focus?.({ preventScroll: true });
  }

  syncToggles() {
    if (!this.el) return;
    this.el.querySelectorAll(".code-find-toggle").forEach((btn) => {
      const on = Boolean(this.state[btn.dataset.opt]);
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", String(on));
    });
  }

  schedule() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.run(), DEBOUNCE_MS);
  }

  /** The file (or the diff it shows) changed: search again, keeping the bar as it is. */
  refresh() {
    if (this.isOpen) this.run({ scroll: false });
  }

  async run({ scroll = true } = {}) {
    clearTimeout(this.timer);
    const seq = ++this.seq;
    const st = this.state;
    const { re, error } = buildMatcher(st.query || "", st);
    this.error = error;
    this.matches = [];
    this.byRow = new Map();
    this.capped = false;
    this.current = -1;
    if (!re) {
      this.updateCount();
      this.repaint();
      return;
    }
    const rows = searchRows(this.editor.rawLines || [], this.editor.fileDiff);
    let result;
    if (rows.length <= SYNC_ROW_LIMIT) {
      result = findInRows(rows, re, FIND_MATCH_CAP);
    } else {
      result = await findAllChunked(rows, re, {
        isCancelled: () => seq !== this.seq,
        onProgress: (count) => {
          if (seq === this.seq) this.updateCount(`${count}…`);
        },
      });
      if (result.cancelled || seq !== this.seq) return;
    }
    this.matches = result.matches;
    this.capped = result.capped;
    this.byRow = groupByRow(this.matches);
    if (this.matches.length) this.current = firstMatchFrom(this.matches, this.topRow());
    this.updateCount();
    if (this.current >= 0 && scroll) this.reveal(this.current, { onlyIfHidden: true });
    else this.repaint();
  }

  topRow() {
    const rowH = this.editor.rowHeight();
    return Math.floor((this.editor.viewportEl?.scrollTop || 0) / rowH);
  }

  updateCount(text = null) {
    if (!this.el) return;
    const countEl = this.el.querySelector(".code-find-count");
    const errEl = this.el.querySelector(".code-find-error");
    const st = this.state;
    if (this.error) {
      errEl.hidden = false;
      errEl.textContent = this.error;
      countEl.textContent = "";
      this.el.classList.add("has-error");
      return;
    }
    errEl.hidden = true;
    this.el.classList.remove("has-error");
    countEl.textContent = text != null ? text : (st.query ? countLabel(this.current, this.matches.length, this.capped) : "");
    this.el.classList.toggle("no-results", Boolean(st.query) && !this.matches.length && text == null);
  }

  step(dir) {
    if (!this.matches.length) {
      if (this.state.query && this.isOpen) this.run();
      return;
    }
    this.current = stepMatch(this.matches, this.current, dir);
    this.updateCount();
    this.reveal(this.current);
  }

  /** Scroll the match to the vertical center (unless already visible with onlyIfHidden), then repaint. */
  reveal(index, { onlyIfHidden = false } = {}) {
    const m = this.matches[index];
    const vp = this.editor.viewportEl;
    if (!m || !vp) return;
    const rowH = this.editor.rowHeight();
    const top = m.row * rowH;
    const visible = top >= vp.scrollTop && top + rowH <= vp.scrollTop + vp.clientHeight;
    if (!(onlyIfHidden && visible)) {
      vp.scrollTop = Math.max(0, top - vp.clientHeight / 2 + rowH / 2);
    }
    this.editor.updateVisibleLines();
    this.editor.updateMinimapSlider?.();
    this.editor.renderMinimap();
  }

  repaint() {
    this.editor.updateVisibleLines();
    this.editor.renderMinimap();
  }

  /** Wrap the matches of the rendered rows (called after every render of the visible rows). */
  decorate(linesContainer) {
    if (!this.isOpen || !this.byRow.size || !linesContainer) return;
    const currentMatch = this.current >= 0 ? this.matches[this.current] : null;
    linesContainer.querySelectorAll(".code-row[data-row]").forEach((rowEl) => {
      const row = rowEl.dataset.row;
      const cells = rowEl.querySelectorAll(".code-text");
      if (rowEl.classList.contains("diff-split-row")) {
        cells.forEach((cell, i) => this.decorateCell(cell, this.byRow.get(`${row}|${i === 0 ? "l" : "r"}`), currentMatch));
      } else if (cells[0]) {
        this.decorateCell(cells[0], this.byRow.get(`${row}|`), currentMatch);
      }
    });
  }

  decorateCell(cell, ranges, currentMatch) {
    if (!ranges || !ranges.length) return;
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n);
    const pieces = rangesToNodePieces(nodes.map((n) => n.nodeValue.length), ranges);
    const currentIdx = currentMatch ? this.current : -1;
    for (const p of pieces) {
      const node = nodes[p.node];
      if (!node || p.end <= p.start) continue;
      if (p.end < node.nodeValue.length) node.splitText(p.end);
      const mid = p.start > 0 ? node.splitText(p.start) : node;
      const mark = document.createElement("mark");
      mark.className = p.index === currentIdx ? "find-match current" : "find-match";
      mid.parentNode.insertBefore(mark, mid);
      mark.appendChild(mid);
    }
  }

  /** One tick per matching row on the right edge of the minimap. */
  drawMinimap(ctx, m, w) {
    if (!this.isOpen || !this.matches.length || !m) return;
    ctx.fillStyle = MINIMAP_MATCH_COLOR;
    let last = -1;
    for (const match of this.matches) {
      if (match.row === last) continue;
      last = match.row;
      ctx.fillRect(w - 5, match.row * m.lineScale, 5, Math.max(2, m.lineScale));
    }
  }
}

/** Text of the current selection inside `root`, for prefilling (single line only). */
export function selectionWithin(root) {
  const sel = window.getSelection ? window.getSelection() : null;
  if (!sel || !sel.rangeCount || !root || !sel.anchorNode || !root.contains(sel.anchorNode)) return "";
  const text = sel.toString();
  return text && !text.includes("\n") ? text : "";
}

