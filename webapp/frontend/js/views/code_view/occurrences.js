/**
 * occurrences.js - Usage highlighting of a local variable, parameter or field in the code view (§5.21).
 *
 * Clicking a token that resolves locally (`GET /symbols/{v}/scope`), or a card's / menu's *Highlight usages*,
 * paints every use of the ScopeInfo with its access (declaration, read, write, read-write, address-of). The
 * ranges are computed once (`occurrenceRanges`, scope_model.js) and re-applied to the rows each time the
 * virtualized lines render, like the find bar's matches. Escape, a click elsewhere in the code and another
 * file or revision clear it. `flash(line)` marks a revealed line for a moment (it survives re-rendering too).
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { rangesToNodePieces } from "./find_model.js";
import { isScopeFile, occurrenceRanges, scopeOf, tokenPosition } from "./scope_model.js";

const FLASH_MS = 1200;

export class OccurrenceHighlighter {
  /** `view`: the code view (currentVersion, currentFilePath, rawLines, fileDiff, updateVisibleLines). */
  constructor(view) {
    this.view = view;
    this.active = null;
    this.seq = 0;
    this.abort = null;
    this.flashState = null;
    this.onKey = (e) => {
      if (e.key === "Escape" && this.active) this.clear();
    };
  }

  key() {
    return `${this.view.currentVersion}|${this.view.currentFilePath}`;
  }

  attach() {
    document.removeEventListener("keydown", this.onKey);
    document.addEventListener("keydown", this.onKey);
  }

  dispose() {
    document.removeEventListener("keydown", this.onKey);
    this.abort?.abort();
    this.active = null;
  }

  /** Drop a highlight made for another file or revision (the view re-rendered onto something else). */
  sync() {
    if (this.active && this.active.key !== this.key()) {
      this.active = null;
      this.flashState = null;
    }
  }

  /** Highlight the uses of `scope` (a ScopeInfo or a payload carrying one); false when it has none. */
  set(scopeOrPayload, name = "") {
    const scope = scopeOf(scopeOrPayload);
    if (!scope?.uses?.length) return false;
    const lines = this.view.rawLines || [];
    const ranges = occurrenceRanges(scope.uses, (l) => lines[l - 1], scope.name || name);
    if (!ranges.size) return false;
    this.active = { key: this.key(), name: scope.name || name, scope, ranges };
    this.view.updateVisibleLines?.();
    return true;
  }

  clear() {
    this.seq++;
    this.abort?.abort();
    if (!this.active) return;
    this.active = null;
    this.view.updateVisibleLines?.();
  }

  /**
   * Resolve a clicked token (C files): highlight its uses when it is a local variable, parameter or field,
   * clear otherwise. Resolves to the ScopeInfo or null.
   */
  async fromToken(span) {
    const seq = ++this.seq;
    const name = (span?.dataset?.name || span?.textContent || "").trim();
    const pos = !this.view.fileDiff && isScopeFile(this.view.currentFilePath) && /^[A-Za-z_]\w*$/.test(name) ? tokenPosition(span) : null;
    if (!pos) {
      this.clear();
      return null;
    }
    this.abort?.abort();
    this.abort = typeof AbortController === "function" ? new AbortController() : null;
    let scope = null;
    try {
      const ctx = state.definitionContext(this.view.engineTabId ?? null);
      const client = this.view.api || api;
      scope = scopeOf(await client.getSymbolScope(this.view.currentVersion, this.view.currentFilePath, pos.line, pos.col, name, ctx.arch, this.abort?.signal));
    } catch (err) {
      if (err?.name !== "AbortError") console.warn("Scope lookup failed:", err);
      scope = null;
    }
    if (seq !== this.seq) return scope;
    if (!scope || !this.set(scope, name)) {
      if (this.active) {
        this.active = null;
        this.view.updateVisibleLines?.();
      }
    }
    return scope;
  }

  /** Mark `line` for a moment (a definition revealed in place). */
  flash(line) {
    this.flashState = { key: this.key(), line, at: performance.now() };
  }

  /** Paint the rendered rows (called after every render of the visible lines). */
  decorate(container) {
    if (!container || this.view.fileDiff) return;
    const flash = this.flashState && this.flashState.key === this.key() ? this.flashState : null;
    const elapsed = flash ? performance.now() - flash.at : Infinity;
    if (flash && elapsed >= FLASH_MS) this.flashState = null;
    const active = this.active && this.active.key === this.key() ? this.active : null;
    if (!active && !(flash && elapsed < FLASH_MS)) return;
    container.querySelectorAll(".code-row[data-line-no]").forEach((rowEl) => {
      const line = parseInt(rowEl.dataset.lineNo, 10);
      if (flash && elapsed < FLASH_MS && line === flash.line) {
        rowEl.classList.add("line-flash");
        rowEl.style.animationDelay = `-${Math.round(elapsed)}ms`;
      }
      const ranges = active?.ranges.get(line);
      const cell = ranges && rowEl.querySelector(".code-text");
      if (cell) decorateCell(cell, ranges);
    });
  }
}

function decorateCell(cell, ranges) {
  const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
  const nodes = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n);
  const pieces = rangesToNodePieces(nodes.map((n) => n.nodeValue.length), ranges.map((r, i) => [r.start, r.end, i]));
  for (const p of pieces) {
    const node = nodes[p.node];
    if (!node || p.end <= p.start) continue;
    if (p.end < node.nodeValue.length) node.splitText(p.end);
    const mid = p.start > 0 ? node.splitText(p.start) : node;
    const mark = document.createElement("mark");
    mark.className = `occ-mark occ-${ranges[p.index].access}`;
    mid.parentNode.insertBefore(mark, mid);
    mark.appendChild(mid);
  }
}
