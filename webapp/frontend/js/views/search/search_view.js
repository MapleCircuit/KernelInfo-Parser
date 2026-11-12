/**
 * search_view.js - Full-text code search tab (tab type `search`, webapp/API.md §5.34).
 *
 * Searches the text of every file of the tab's revision (GET /api/fs/grep, git grep over its commit,
 * §2.1.3): fixed text or POSIX ERE, match case, whole words, path filters (`drivers/net/`, `*.c`,
 * `!*.h`). Results are grouped by file with their matches highlighted; a click opens the file at the
 * line (middle-click or Ctrl/Cmd+click: a new tab, §5.9). The search is the tab's address
 * (`#tab=search&q=…&re=1&cs=1&w=1&p=…`, §5.26).
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { shortRef } from "../../components/ref_picker.js";
import { SEARCH_LIMITS } from "../../routes.js";
import {
  escapeHtml, groupByFile, grepSummary, grepToText, highlightRanges, parsePathFilters,
} from "../../utils/grep_format.js";

const LIMITS = SEARCH_LIMITS;

export class SearchView {
  constructor() {
    this.tabId = null;
    this.tabData = null;
    this.containerEl = null;
    this.version = "";
    this.result = null;
    this.seq = 0;
    this.collapsed = new Set();
  }

  async render(containerEl, tabData) {
    this.tabId = tabData.id;
    this.tabData = tabData;
    this.containerEl = containerEl;
    this.version = tabData.version || state.currentVersion;
    const t = tabData;
    containerEl.innerHTML = `
      <div class="search-view">
        <form class="search-toolbar" autocomplete="off">
          <input type="text" class="search-query" placeholder="Search text in ${escapeHtml(shortRef(this.version))}…" spellcheck="false" aria-label="Search text" />
          <button type="button" class="search-toggle" data-opt="caseSensitive" title="Match case" aria-pressed="false">Aa</button>
          <button type="button" class="search-toggle" data-opt="word" title="Whole words" aria-pressed="false">ab|</button>
          <button type="button" class="search-toggle" data-opt="regex" title="POSIX extended regular expression (git grep -E)" aria-pressed="false">.*</button>
          <input type="text" class="search-paths" placeholder="Paths: drivers/net/, *.c, !*.h" spellcheck="false" aria-label="Path filters" />
          <select class="search-limit" title="Most matches returned">${LIMITS.map((n) => `<option value="${n}">${n.toLocaleString("en-US")}</option>`).join("")}</select>
          <button type="submit" class="code-btn search-go">Search</button>
          <button type="button" class="code-btn search-download" title="Download the results as path:line:text" disabled>⬇ .txt</button>
        </form>
        <div class="search-summary" aria-live="polite"></div>
        <div class="search-results"></div>
      </div>`;
    const form = containerEl.querySelector(".search-toolbar");
    const qInput = form.querySelector(".search-query");
    const pInput = form.querySelector(".search-paths");
    const limitSel = form.querySelector(".search-limit");
    qInput.value = t.query || "";
    pInput.value = (t.paths || []).join(", ");
    limitSel.value = String(t.limit || 2000);
    this.syncToggles();
    form.querySelectorAll(".search-toggle").forEach((btn) => {
      btn.onclick = () => {
        const opt = btn.dataset.opt;
        this.submit({ [opt]: !this.tabData[opt] });
      };
    });
    form.onsubmit = (e) => {
      e.preventDefault();
      this.submit({});
    };
    limitSel.onchange = () => this.submit({});
    form.querySelector(".search-download").onclick = () => this.download();
    const resultsEl = containerEl.querySelector(".search-results");
    resultsEl.addEventListener("click", (e) => this.onResultClick(e, false));
    resultsEl.addEventListener("auxclick", (e) => {
      if (e.button === 1) this.onResultClick(e, true);
    });
    if (t.query) this.run();
    else {
      containerEl.querySelector(".search-summary").textContent = "Type a text or a regular expression and press Enter.";
      qInput.focus();
    }
  }

  syncToggles() {
    this.containerEl?.querySelectorAll(".search-toggle").forEach((btn) => {
      const on = Boolean(this.tabData[btn.dataset.opt]);
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", String(on));
    });
  }

  /** Record the search on the tab (its URL, §5.26) and run it. */
  submit(patch) {
    if (!this.containerEl?.isConnected) return;
    const form = this.containerEl.querySelector(".search-toolbar");
    const query = form.querySelector(".search-query").value;
    const paths = parsePathFilters(form.querySelector(".search-paths").value);
    const limit = Number(form.querySelector(".search-limit").value) || 2000;
    const next = {
      query,
      regex: this.tabData.regex || false,
      caseSensitive: this.tabData.caseSensitive || false,
      word: this.tabData.word || false,
      paths,
      limit: limit === 2000 ? 0 : limit,
      ...patch,
    };
    state.updateTabState(this.tabId, {
      ...next,
      paths: next.paths.length ? next.paths : null,
      title: query ? `Search: ${query.slice(0, 24)}` : "Search",
    });
    Object.assign(this.tabData, next);
    this.syncToggles();
    this.collapsed.clear();
    if (query) this.run();
  }

  async run() {
    const seq = ++this.seq;
    const t = this.tabData;
    const summary = this.containerEl.querySelector(".search-summary");
    const resultsEl = this.containerEl.querySelector(".search-results");
    summary.textContent = `Searching ${shortRef(this.version)}…`;
    resultsEl.classList.add("loading");
    let res;
    try {
      res = await this.api.grepCode(this.version, {
        q: t.query, regex: t.regex, caseSensitive: t.caseSensitive, word: t.word, paths: t.paths || [],
        limit: t.limit || 2000, perFile: 200,
      });
    } catch (err) {
      if (seq !== this.seq || !this.containerEl?.isConnected) return;
      resultsEl.classList.remove("loading");
      summary.innerHTML = `<span class="search-error">${escapeHtml(err.message)}</span>`;
      resultsEl.innerHTML = "";
      return;
    }
    if (seq !== this.seq || !this.containerEl?.isConnected) return;
    resultsEl.classList.remove("loading");
    this.result = res;
    this.containerEl.querySelector(".search-download").disabled = !res?.total;
    summary.textContent = res?.total ? `${grepSummary(res)} · ${res.elapsed_ms} ms` : `No file of ${shortRef(this.version)} matches.`;
    summary.classList.toggle("warn", Boolean(res?.truncated || res?.timed_out));
    this.renderResults();
  }

  renderResults() {
    const resultsEl = this.containerEl.querySelector(".search-results");
    const groups = groupByFile(this.result?.matches || []);
    resultsEl.innerHTML = groups.map((g, gi) => {
      const folded = this.collapsed.has(g.path);
      return `<section class="search-file${folded ? " collapsed" : ""}" data-gi="${gi}">
        <header class="search-file-head" data-path="${escapeHtml(g.path)}">
          <button type="button" class="search-fold" aria-expanded="${!folded}" title="Fold">${folded ? "▸" : "▾"}</button>
          <a class="search-file-path" href="#" data-path="${escapeHtml(g.path)}" data-line="1">${escapeHtml(g.path)}</a>
          <span class="search-file-count">${g.matches.length}</span>
        </header>
        ${folded ? "" : `<div class="search-lines">${g.matches.map((m) => `
          <a class="search-line" href="#" data-path="${escapeHtml(m.path)}" data-line="${m.line}">
            <span class="search-ln">${m.line}</span><code class="search-text">${highlightRanges(m.text, m.ranges)}${m.clipped ? '<span class="search-clipped" title="Long line, clipped around the match">…</span>' : ""}</code>
          </a>`).join("")}</div>`}
      </section>`;
    }).join("");
  }

  onResultClick(e, forceNew) {
    const fold = e.target.closest(".search-fold");
    if (fold) {
      e.preventDefault();
      const path = fold.closest(".search-file-head").dataset.path;
      if (this.collapsed.has(path)) this.collapsed.delete(path);
      else this.collapsed.add(path);
      this.renderResults();
      return;
    }
    const link = e.target.closest("[data-line]");
    if (!link) return;
    e.preventDefault();
    const path = link.dataset.path;
    const line = parseInt(link.dataset.line, 10) || 1;
    state.openTab({
      type: "code", title: path.split("/").pop(), path, version: this.version,
      cursorLine: line, forceNew: forceNew || e.ctrlKey || e.metaKey,
    });
  }

  download() {
    if (!this.result?.total) return;
    const blob = new Blob([grepToText(this.result)], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `grep-${String(this.version).replace(/[^A-Za-z0-9._-]/g, "_")}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast.success(`Downloaded ${this.result.total} matches`);
  }

  dispose() {
    this.seq++;
    this.containerEl = null;
  }
}
