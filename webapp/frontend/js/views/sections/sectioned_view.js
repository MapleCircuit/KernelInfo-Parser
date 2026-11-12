/**
 * sectioned_view.js - Base of the list-and-detail tabs hosting catalogs and reports (S05,
 * webapp/API.md §5.29 Kernel API, §5.30 Insights).
 *
 * A tab shows one section at a time: section pills, an optional release or ARCH picker, a search box,
 * filter chips with counts, a windowed list (VirtualList) and a detail pane, with the collapsible list
 * rail of the other list views (`lc=1`) and a download menu. Each feature only provides a section:
 *
 *   { id, label, releaseScoped, archScoped,
 *     fetchList(ctx) -> {rows, total?, note?, truncated?, summaryHtml?},   // ctx: {version, release, arch, api, signal, onProgress}
 *     rowKey(row), rowHtml(row), rowText?(row), rowHeight?,
 *     chips?: [{id, label, test(row)}],
 *     detailHtml?(row, ctx) | fetchDetail?(ctx, key, row) + detailHtml(detail, ctx),
 *     onDetailClick?(event, ctx) -> bool, downloads?: [{label, ext, build(rows, ctx)}],
 *     releaseOptions?(ctx) -> [names], emptyText? }
 *
 * The section, its release / ARCH, the search, the chips, the selection and the folded rail are the
 * tab's state and its address (`#tab=api&view=…&q=…&f=…&sel=…`, §5.26). Lists answered 202 by a
 * background job show its progress (§2.15).
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { escapeHtml, downloadText } from "../../components/modal.js";
import { toast } from "../../components/toast.js";
import { contextMenu } from "../../components/context_menu.js";
import { VirtualList } from "../../components/virtual_list.js";
import { COLLAPSE_ICON } from "../../utils/icons.js";
import { progressFraction, progressLabel } from "../../utils/job_poll.js";
import { chipCounts, filterRows, findRowBySel, formatChips, parseChips } from "./section_model.js";
import { releaseKey, isFinalRelease } from "./release_order.js";

export class SectionedView {
  constructor({ type, title, sections }) {
    this.type = type;
    this.title = title;
    this.sections = sections;
    this.tabId = null;
    this.tab = null;
    this.containerEl = null;
    this.list = null;
    this.rows = [];
    this.filtered = [];
    this.listSeq = 0;
    this.detailSeq = 0;
    this.abort = null;
    this.result = null;
  }

  get section() {
    return this.sections.find((s) => s.id === this.tab?.view) || this.sections[0];
  }

  ctx(extra = {}) {
    return {
      version: this.version, release: this.tab.release || "", arch: this.tab.arch || "",
      window: this.tab.window || this.section?.windowDefault || 0,
      // The client of this tab's repository: every request a section makes names it (§5.18)
      view: this, api: this.api, signal: this.abort?.signal, ...extra,
    };
  }

  persist(patch) {
    if (!this.containerEl?.isConnected) return;
    // updateTabState writes the tab and announces the change (the URL follows, §5.26); assigning first
    // would make it see nothing new
    state.updateTabState(this.tabId, patch, { replace: !("view" in patch || "sel" in patch || "release" in patch || "arch" in patch) });
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === undefined || v === "" || v === false) delete this.tab[k];
      else this.tab[k] = v;
    }
  }

  /** A tab field corrected in place (the address it was reached by stays the history entry). */
  persistReplace(patch) {
    if (!this.containerEl?.isConnected) return;
    state.updateTabState(this.tabId, patch, { replace: true });
    for (const [k, v] of Object.entries(patch)) this.tab[k] = v;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tabId = tabData.id;
    this.tab = tabData;
    this.version = tabData.version || state.currentVersion;
    const sec = this.section;
    containerEl.innerHTML = `
      <div class="sv-container${tabData.listCollapsed ? " list-collapsed" : ""}">
        <button type="button" class="sv-rail" title="Show the list" aria-label="Show the list">
          <span class="collapse-btn expand">${COLLAPSE_ICON}</span><span class="sv-rail-label">${escapeHtml(sec.label)}</span>
        </button>
        <div class="sv-list-col">
          <div class="sv-head">
            <div class="sv-sections" role="tablist">${this.sections.map((s) => `<button type="button" role="tab" class="sv-pill${s.id === sec.id ? " active" : ""}" data-section="${escapeHtml(s.id)}" aria-selected="${s.id === sec.id}">${escapeHtml(s.label)}</button>`).join("")}</div>
            <div class="sv-controls">
              <select class="sv-release" title="Release" hidden></select>
              <select class="sv-arch" title="ARCH" hidden></select>
              <select class="sv-window" title="Window" hidden></select>
              <input type="search" class="sv-search" placeholder="Filter…" spellcheck="false" aria-label="Filter the list" />
              <button type="button" class="code-btn sv-download" title="Download the filtered list">⬇</button>
              <button type="button" class="collapse-btn sv-collapse" title="Collapse the list" aria-label="Collapse the list">${COLLAPSE_ICON}</button>
            </div>
            <div class="sv-chips"></div>
            <div class="sv-status" aria-live="polite"></div>
            <div class="sv-summary" hidden></div>
          </div>
          <div class="sv-list"></div>
        </div>
        <div class="sv-detail-col"><div class="sv-detail"><div class="sv-empty">Select an entry.</div></div></div>
      </div>`;
    const q = containerEl.querySelector(".sv-search");
    q.value = tabData.query || "";
    let qTimer = null;
    q.addEventListener("input", () => {
      clearTimeout(qTimer);
      qTimer = setTimeout(() => {
        this.persist({ query: q.value.trim() });
        this.applyFilter();
      }, 150);
    });
    containerEl.querySelector(".sv-sections").addEventListener("click", (e) => {
      const b = e.target.closest("[data-section]");
      if (b && b.dataset.section !== this.section.id) this.switchSection(b.dataset.section);
    });
    containerEl.querySelector(".sv-chips").addEventListener("click", (e) => {
      const b = e.target.closest("[data-chip]");
      if (!b) return;
      const chips = parseChips(this.tab.filter);
      if (chips.has(b.dataset.chip)) chips.delete(b.dataset.chip);
      else chips.add(b.dataset.chip);
      this.persist({ filter: formatChips(chips) });
      this.applyFilter();
    });
    containerEl.querySelector(".sv-download").onclick = (e) => this.showDownloads(e.currentTarget);
    containerEl.querySelector(".sv-collapse").onclick = () => this.setCollapsed(true);
    containerEl.querySelector(".sv-rail").onclick = () => this.setCollapsed(false);
    const detail = containerEl.querySelector(".sv-detail");
    detail.addEventListener("click", (e) => this.onDetailClick(e, false));
    detail.addEventListener("auxclick", (e) => {
      if (e.button === 1) this.onDetailClick(e, true);
    });
    this.list = new VirtualList(containerEl.querySelector(".sv-list"), {
      rowHeight: sec.rowHeight || 30,
      renderRow: (row) => sec.rowHtml(row),
      rowKey: (row) => String(sec.rowKey(row)),
      label: sec.label,
      onSelect: (row, i, { forceNew } = {}) => this.selectRow(row, forceNew),
    });
    await this.setupPickers();
    if (tabData.listCollapsed && tabData.sel && !sec.needsListForDetail) {
      // A folded list is not fetched: only the selection's detail is shown
      this.showDetail(null, tabData.sel);
    } else {
      this.loadList();
    }
  }

  async setupPickers() {
    const sec = this.section;
    const relSel = this.containerEl.querySelector(".sv-release");
    const archSel = this.containerEl.querySelector(".sv-arch");
    relSel.hidden = !sec.releaseScoped;
    const winSel = this.containerEl.querySelector(".sv-window");
    winSel.hidden = !sec.windowOptions;
    if (sec.windowOptions) {
      const w = this.tab.window || sec.windowDefault || sec.windowOptions[0];
      winSel.innerHTML = sec.windowOptions.map((n) => `<option value="${n}" ${n === w ? "selected" : ""}>${escapeHtml(sec.windowLabel ? sec.windowLabel(n) : String(n))}</option>`).join("");
      winSel.onchange = () => {
        const n = parseInt(winSel.value, 10);
        this.persist({ window: n === (sec.windowDefault || sec.windowOptions[0]) ? null : n, sel: "" });
        this.loadList();
      };
    }
    archSel.hidden = !sec.archScoped;
    if (sec.releaseScoped) {
      let names = [];
      try {
        names = sec.releaseOptions ? await sec.releaseOptions(this.ctx()) : await this.defaultReleases();
      } catch {
        names = [];
      }
      if (!this.tab.release || !names.includes(this.tab.release)) {
        const own = names.includes(this.version) ? this.version : names[0];
        if (own) this.tab.release = own;
      }
      relSel.innerHTML = names.map((n) => `<option value="${escapeHtml(n)}" ${n === this.tab.release ? "selected" : ""}>${escapeHtml(n)}</option>`).join("");
      relSel.onchange = () => {
        this.persist({ release: relSel.value, sel: "" });
        this.loadList();
      };
    }
    if (sec.archScoped) {
      let targets = [];
      try {
        targets = (await this.api.getKconfigPresets(this.version))?.targets || [];
      } catch {
        targets = [];
      }
      if (!this.tab.arch) this.tab.arch = state.kconfigArch() || "x86_64";
      if (targets.length && !targets.some((t) => t.id === this.tab.arch)) targets.unshift({ id: this.tab.arch, label: this.tab.arch });
      archSel.innerHTML = (targets.length ? targets : [{ id: this.tab.arch, label: this.tab.arch }])
        .map((t) => `<option value="${escapeHtml(t.id)}" ${t.id === this.tab.arch ? "selected" : ""}>${escapeHtml(t.label || t.id)}</option>`).join("");
      archSel.onchange = () => {
        this.persist({ arch: archSel.value, sel: "" });
        this.loadList();
      };
    }
  }

  /** Final mainline releases of the repository, newest first. */
  async defaultReleases() {
    const res = await this.api.getVersionsCached(true);
    const names = (res?.versions || []).map((v) => v.vname || v).filter((n) => typeof n === "string" && isFinalRelease(n));
    return [...new Set(names)].sort((a, b) => (releaseKey(b) > releaseKey(a) ? 1 : releaseKey(b) < releaseKey(a) ? -1 : 0));
  }

  async switchSection(id) {
    this.abort?.abort();
    this.persist({ view: id, sel: "", filter: "", title: `${this.title} · ${this.sections.find((s) => s.id === id)?.label || id}` });
    this.render(this.containerEl, this.tab);
  }

  setCollapsed(on) {
    this.containerEl.querySelector(".sv-container").classList.toggle("list-collapsed", on);
    this.persist({ listCollapsed: on || null });
    if (!on && !this.rows.length) this.loadList();
  }

  setStatus(text, progress = null) {
    const el = this.containerEl?.querySelector(".sv-status");
    if (!el) return;
    const frac = progressFraction(progress);
    el.innerHTML = `${escapeHtml(text)}${frac !== null ? `<div class="sv-progress"><div style="width:${Math.round(frac * 100)}%"></div></div>` : ""}`;
  }

  async loadList() {
    const seq = ++this.listSeq;
    this.abort?.abort();
    this.abort = new AbortController();
    const sec = this.section;
    this.setStatus("Loading…");
    let res;
    try {
      res = await sec.fetchList(this.ctx({
        signal: this.abort.signal,
        onProgress: (p) => {
          if (seq === this.listSeq) this.setStatus(`Building… ${progressLabel(p)}`, p);
        },
      }));
    } catch (err) {
      if (err?.name === "AbortError" || seq !== this.listSeq) return;
      this.rows = [];
      this.applyFilter();
      this.setStatus(`Could not load: ${err.message}`);
      return;
    }
    if (seq !== this.listSeq || !this.containerEl?.isConnected) return;
    this.result = res || {};
    this.rows = this.result.rows || [];
    const summary = this.containerEl.querySelector(".sv-summary");
    summary.hidden = !this.result.summaryHtml;
    summary.innerHTML = this.result.summaryHtml || "";
    // A `sel` naming only the last key segment (a syscall name, §5.29) selects that row under its full key
    const selRow = this.tab.sel ? findRowBySel(this.rows, sec.rowKey, this.tab.sel) : null;
    if (selRow && String(sec.rowKey(selRow)) !== this.tab.sel) this.persistReplace({ sel: String(sec.rowKey(selRow)) });
    this.applyFilter({ keepSelection: true });
    if (this.tab.sel) this.showDetail(selRow, this.tab.sel);
  }

  applyFilter({ keepSelection = false } = {}) {
    const sec = this.section;
    const chips = parseChips(this.tab.filter);
    this.filtered = filterRows(sec, this.rows, this.tab.query, chips);
    const counts = chipCounts(sec, this.rows);
    this.containerEl.querySelector(".sv-chips").innerHTML = (sec.chips || []).map((c) =>
      `<button type="button" class="sv-chip${chips.has(c.id) ? " active" : ""}" data-chip="${escapeHtml(c.id)}" aria-pressed="${chips.has(c.id)}" title="${escapeHtml(c.title || "")}">${escapeHtml(c.label)}${counts[c.id] != null ? ` <span>${counts[c.id]}</span>` : ""}</button>`).join("");
    this.list.setRows(this.filtered, { keepScroll: keepSelection, selectKey: this.tab.sel || null });
    const r = this.result || {};
    const total = r.total ?? this.rows.length;
    const shown = this.filtered.length === this.rows.length ? `${this.rows.length.toLocaleString("en-US")}` : `${this.filtered.length.toLocaleString("en-US")} of ${this.rows.length.toLocaleString("en-US")}`;
    this.setStatus(this.rows.length || total ? `${shown}${total > this.rows.length ? ` (of ${total.toLocaleString("en-US")})` : ""}${r.truncated ? " · truncated" : ""}${r.note ? ` · ${r.note}` : ""}` : (sec.emptyText || "Nothing to show."));
  }

  selectRow(row, forceNew = false) {
    const key = String(this.section.rowKey(row));
    if (forceNew) {
      state.openTab({ type: this.type, title: this.tab.title, view: this.section.id, sel: key, release: this.tab.release, arch: this.tab.arch, version: this.version, forceNew: true });
      return;
    }
    this.persist({ sel: key });
    this.showDetail(row, key);
  }

  async showDetail(row, key) {
    const sec = this.section;
    const el = this.containerEl.querySelector(".sv-detail");
    const seq = ++this.detailSeq;
    if (!row && !sec.fetchDetail) {
      el.innerHTML = `<div class="sv-empty">${escapeHtml(key ? `${key} is not in this list.` : "Select an entry.")}</div>`;
      return;
    }
    let detail = row;
    if (sec.fetchDetail) {
      el.innerHTML = '<div class="sv-empty">Loading…</div>';
      try {
        detail = await sec.fetchDetail(this.ctx(), key, row);
      } catch (err) {
        if (seq === this.detailSeq) el.innerHTML = `<div class="sv-empty">Could not load: ${escapeHtml(err.message)}</div>`;
        return;
      }
      if (seq !== this.detailSeq) return;
    }
    el.innerHTML = sec.detailHtml ? sec.detailHtml(detail, this.ctx()) : `<pre>${escapeHtml(JSON.stringify(detail, null, 2))}</pre>`;
    sec.afterDetail?.(el, detail, this.ctx());
  }

  /** Links shared by every section's detail: data-open-file / data-line, data-commit, data-symbol. */
  onDetailClick(e, forceNew) {
    if (this.section.onDetailClick?.(e, this.ctx({ forceNew }))) return;
    const file = e.target.closest("[data-open-file]");
    const commit = e.target.closest("[data-commit]");
    const symbol = e.target.closest("[data-symbol]");
    const nt = forceNew || e.ctrlKey || e.metaKey;
    if (file) {
      e.preventDefault();
      const path = file.dataset.openFile;
      state.openTab({ type: "code", title: path.split("/").pop(), path, version: file.dataset.version || this.version,
        cursorLine: parseInt(file.dataset.line || "1", 10) || 1, forceNew: nt });
    } else if (commit) {
      e.preventDefault();
      const hash = commit.dataset.commit;
      state.openTab({ type: "commits", title: `Commit ${hash.slice(0, 7)}`, commitId: hash, listCollapsed: true, version: this.version, forceNew: nt });
    } else if (symbol) {
      e.preventDefault();
      state.openTab({ type: "search", title: `Search: ${symbol.dataset.symbol}`, query: symbol.dataset.symbol, word: true, version: this.version, forceNew: nt });
    }
  }

  showDownloads(anchor) {
    const sec = this.section;
    const items = (sec.downloads || []).map((d) => ({
      label: `${d.label} (.${d.ext})`, icon: "⬇",
      onClick: () => {
        const text = d.build(this.filtered, this.ctx());
        downloadText(`${this.type}-${sec.id}-${String(this.tab.release || this.version).replace(/[^A-Za-z0-9._-]/g, "_")}.${d.ext}`, text,
          d.ext === "json" ? "application/json" : "text/plain");
        toast.success(`Downloaded ${this.filtered.length} rows`);
      },
    }));
    if (!items.length) {
      toast.info("This section has nothing to download");
      return;
    }
    const r = anchor.getBoundingClientRect();
    contextMenu.renderMenu(r.left, r.bottom + 4, { header: { title: "Download", sub: `${this.filtered.length} rows (the filtered list)` }, items });
  }

  dispose() {
    this.abort?.abort();
    this.list?.dispose();
    this.containerEl = null;
  }
}
