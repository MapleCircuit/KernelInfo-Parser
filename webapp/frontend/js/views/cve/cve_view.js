/**
 * cve_view.js - Independent CVE tab (`type: "cve"`): release-independent catalog of all ingested
 * Linux kernel CVEs, with version filtering, fast deep-linked detail lookup, and on-demand release checking.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { escapeHtml, downloadText } from "../../components/modal.js";
import { toast } from "../../components/toast.js";
import { VirtualList } from "../../components/virtual_list.js";
import { COLLAPSE_ICON } from "../../utils/icons.js";
import { copyToClipboard } from "../../utils/clipboard.js";
import { buildDeepLink } from "../../tab_routes.js";
import { chipCounts, filterRows, formatChips, parseChips, toTsv } from "../sections/section_model.js";
import { hasFeature } from "../../utils/projects.js";
import { cveSpec, sendToNodemap } from "../nodemap/nodemap_bridge.js";
import { NOT_INGESTED_HTML, SETUP_HTML, renderCveDetailHtml, severityTag } from "./cve_render.js";

export const CVE_CHIPS = [
  { id: "cna", label: "Kernel CNA", title: "Issued by the Linux kernel CNA", test: (r) => r.kind === "cna" },
  { id: "commit", label: "Commit ref", title: "Another CNA's record linking a kernel commit", test: (r) => r.kind === "commit" },
  { id: "text", label: "Text only", title: "Another CNA's record about the kernel, linking no commit", test: (r) => r.kind === "text" },
  { id: "unfixed", label: "Unfixed", title: "No fix recorded", test: (r) => !!r.unfixed },
  { id: "high", label: "CVSS ≥ 7", title: "High or critical CVSS base score", test: (r) => r.score != null && r.score >= 7 },
  { id: "kev", label: "Exploited (KEV)", title: "Listed in CISA's Known Exploited Vulnerabilities catalog", test: (r) => !!r.kev },
];

export const CVE_SECTION_MODEL = {
  chips: CVE_CHIPS,
  rowText: (r) => `${r.id} ${r.title} ${r.severity || ""} ${(r.cwes || []).join(" ")} ${r.versions || ""} ${(r.files || []).join(" ")}`,
};

export function cveRowHtml(r) {
  return `${severityTag(r.score, "")}${r.kev ? '<span class="sv-tag bad" title="In CISA\'s Known Exploited Vulnerabilities catalog">KEV</span>' : ""}${r.unfixed ? '<span class="sv-tag warn" title="No fix recorded">unfixed</span>' : ""}<code class="sv-row-sub">${escapeHtml(r.id)}</code><span class="sv-row-main">${escapeHtml(r.title || "")}</span>${r.versions ? `<span class="sv-row-sub cve-ver-badge" title="${escapeHtml(r.versions)}">${escapeHtml(r.versions)}</span>` : ""}`;
}

export function cveTsv(rows) {
  return toTsv(rows, [
    ["id", (r) => r.id],
    ["kind", (r) => r.kind || ""],
    ["cvss", (r) => (r.score == null ? "" : r.score)],
    ["kev", (r) => r.kev || ""],
    ["published", (r) => r.published || ""],
    ["versions", (r) => r.versions || ""],
    ["fixed", (r) => (Array.isArray(r.fixed_versions) ? r.fixed_versions.join(" ") : (r.fixed || ""))],
    ["unfixed", (r) => (r.unfixed ? "yes" : "")],
    ["title", (r) => r.title || ""],
  ]);
}

export class CveView {
  constructor() {
    this.tabId = null;
    this.tab = null;
    this.containerEl = null;
    this.list = null;
    this.rows = [];
    this.filtered = [];
    this.listSeq = 0;
    this.detailSeq = 0;
    this.abort = null;
    this.detailAbort = null;
    this.result = null;
    this.queryTimer = null;
    this.versionTimer = null;
    this.checkedRelease = "";
  }

  persist(patch) {
    if (!this.containerEl?.isConnected) return;
    state.updateTabState(this.tabId, patch, { replace: !("sel" in patch || "cveVersion" in patch) });
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === undefined || v === "" || v === false) delete this.tab[k];
      else this.tab[k] = v;
    }
  }

  render(containerEl, tabData) {
    clearTimeout(this.queryTimer);
    clearTimeout(this.versionTimer);
    this.queryTimer = null;
    this.versionTimer = null;
    this.list?.dispose();
    this.list = null;
    this.containerEl = containerEl;
    this.tabId = tabData.id;
    this.tab = tabData;
    this.version = tabData.version || "";
    this.checkedRelease = "";

    containerEl.innerHTML = `
      <div class="sv-container${tabData.listCollapsed ? " list-collapsed" : ""}">
        <button type="button" class="sv-rail" title="Show the list" aria-label="Show the list">
          <span class="collapse-btn expand">${COLLAPSE_ICON}</span><span class="sv-rail-label">CVEs</span>
        </button>
        <div class="sv-list-col">
          <div class="sv-head">
            <div class="sv-controls">
              <input type="search" class="sv-search" placeholder="Filter…" spellcheck="false" aria-label="Filter CVEs" />
              <input type="search" class="cve-version-input" placeholder="Version (e.g. 6.6.15)" spellcheck="false" aria-label="Filter by kernel version" />
              <button type="button" class="code-btn sv-download" title="Download the filtered list (TSV)">⬇</button>
              <button type="button" class="collapse-btn sv-collapse" title="Collapse the list" aria-label="Collapse the list">${COLLAPSE_ICON}</button>
            </div>
            <div class="sv-chips"></div>
            <div class="sv-status" aria-live="polite"></div>
            <div class="sv-summary" hidden></div>
          </div>
          <div class="sv-list"></div>
        </div>
        <div class="sv-detail-col"><div class="sv-detail"><div class="sv-empty">Select a CVE.</div></div></div>
      </div>`;

    const q = containerEl.querySelector(".sv-search");
    q.value = tabData.query || "";
    q.addEventListener("input", () => {
      clearTimeout(this.queryTimer);
      this.queryTimer = setTimeout(() => {
        this.persist({ query: q.value.trim() });
        this.applyFilter();
      }, 150);
    });

    const verInput = containerEl.querySelector(".cve-version-input");
    verInput.value = tabData.cveVersion || "";
    verInput.addEventListener("input", () => {
      clearTimeout(this.versionTimer);
      this.versionTimer = setTimeout(() => {
        const nextVer = verInput.value.trim();
        this.persist({ cveVersion: nextVer });
        this.loadList();
      }, 250);
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

    containerEl.querySelector(".sv-download").onclick = () => {
      const text = cveTsv(this.filtered);
      const verTag = this.tab.cveVersion ? `-${this.tab.cveVersion.replace(/[^A-Za-z0-9._-]/g, "_")}` : "";
      downloadText(`cves${verTag}.tsv`, text, "text/plain");
      toast.success(`Downloaded ${this.filtered.length} CVEs`);
    };

    containerEl.querySelector(".sv-collapse").onclick = () => this.setCollapsed(true);
    containerEl.querySelector(".sv-rail").onclick = () => this.setCollapsed(false);

    const detail = containerEl.querySelector(".sv-detail");
    detail.addEventListener("click", (e) => this.onDetailClick(e, false));
    detail.addEventListener("auxclick", (e) => {
      if (e.button === 1) this.onDetailClick(e, true);
    });
    detail.addEventListener("submit", (e) => {
      const form = e.target.closest("[data-check-release-form]");
      if (!form) return;
      e.preventDefault();
      const cveId = form.dataset.checkReleaseForm;
      const rel = form.querySelector(".cve-check-release-input")?.value?.trim() || "";
      this.checkedRelease = rel;
      if (cveId) this.showDetail(cveId, rel);
    });

    this.list = new VirtualList(containerEl.querySelector(".sv-list"), {
      rowHeight: 30,
      renderRow: (row) => cveRowHtml(row),
      rowKey: (row) => String(row.id),
      label: "CVEs",
      onSelect: (row, i, { forceNew } = {}) => this.selectRow(row, forceNew),
    });

    if (tabData.sel) {
      this.showDetail(tabData.sel, this.checkedRelease || tabData.cveVersion || "");
    }
    if (!tabData.listCollapsed) {
      this.loadList();
    }
  }

  setCollapsed(on) {
    this.containerEl?.querySelector(".sv-container")?.classList.toggle("list-collapsed", on);
    this.persist({ listCollapsed: on || null });
    if (!on && !this.rows.length) this.loadList();
  }

  setStatus(text) {
    const el = this.containerEl?.querySelector(".sv-status");
    if (el) el.textContent = text;
  }

  async loadList() {
    const seq = ++this.listSeq;
    this.abort?.abort();
    this.abort = new AbortController();
    this.setStatus("Loading…");
    let res;
    try {
      res = await (this.api || api).getCveList(
        { version: this.tab.cveVersion || "" },
        { signal: this.abort.signal },
      );
    } catch (err) {
      if (err?.name === "AbortError" || seq !== this.listSeq) return;
      this.rows = [];
      this.applyFilter();
      this.setStatus(`Could not load: ${err.message}`);
      return;
    }
    if (seq !== this.listSeq || !this.containerEl?.isConnected) return;
    this.result = res || {};
    const summary = this.containerEl.querySelector(".sv-summary");
    if (res?.configured === false || res?.ingested === false) {
      this.rows = [];
      summary.hidden = false;
      summary.innerHTML = res.configured === false ? SETUP_HTML : NOT_INGESTED_HTML;
      this.applyFilter();
      this.setStatus(res.configured === false ? "not configured" : "not ingested");
      return;
    }
    summary.hidden = true;
    summary.innerHTML = "";
    this.rows = res.cves || [];
    this.applyFilter({ keepSelection: true });
  }

  applyFilter({ keepSelection = false } = {}) {
    const chips = parseChips(this.tab.filter);
    this.filtered = filterRows(CVE_SECTION_MODEL, this.rows, this.tab.query, chips);
    const counts = chipCounts(CVE_SECTION_MODEL, this.rows);
    const chipsEl = this.containerEl?.querySelector(".sv-chips");
    if (chipsEl) {
      chipsEl.innerHTML = CVE_CHIPS.map((c) =>
        `<button type="button" class="sv-chip${chips.has(c.id) ? " active" : ""}" data-chip="${escapeHtml(c.id)}" aria-pressed="${chips.has(c.id)}" title="${escapeHtml(c.title || "")}">${escapeHtml(c.label)}${counts[c.id] != null ? ` <span>${counts[c.id]}</span>` : ""}</button>`).join("");
    }
    this.list?.setRows(this.filtered, { keepScroll: keepSelection, selectKey: this.tab.sel || null });
    const r = this.result || {};
    if (r.configured === false || r.ingested === false) return;
    const total = r.total ?? this.rows.length;
    const shown = this.filtered.length === this.rows.length
      ? `${this.rows.length.toLocaleString("en-US")}`
      : `${this.filtered.length.toLocaleString("en-US")} of ${this.rows.length.toLocaleString("en-US")}`;
    const source = r.layout === "vulns" ? "vulns.git" : "CVE list";
    const verNote = this.tab.cveVersion ? ` · version ${this.tab.cveVersion}` : "";
    this.setStatus(this.rows.length || total
      ? `${shown}${total > this.rows.length ? ` (of ${total.toLocaleString("en-US")})` : ""}${verNote} · ${source}${r.head ? ` ${String(r.head).slice(0, 12)}` : ""}`
      : `No CVE records match.${verNote}`);
  }

  selectRow(row, forceNew = false) {
    const key = String(row.id);
    if (forceNew) {
      state.openTab({
        type: "cve",
        title: `CVE · ${key}`,
        sel: key,
        cveVersion: this.tab.cveVersion || "",
        forceNew: true,
      });
      return;
    }
    this.persist({ sel: key, title: `CVE · ${key}` });
    this.showDetail(key, this.checkedRelease || this.tab.cveVersion || "");
  }

  async showDetail(cveId, release = "") {
    const el = this.containerEl?.querySelector(".sv-detail");
    if (!el) return;
    const seq = ++this.detailSeq;
    this.detailAbort?.abort();
    this.detailAbort = new AbortController();
    el.innerHTML = '<div class="sv-empty">Loading…</div>';
    let detail;
    try {
      detail = await (this.api || api).getCveDetail(cveId, release, { signal: this.detailAbort.signal });
    } catch (err) {
      if (err?.name === "AbortError" || seq !== this.detailSeq) return;
      el.innerHTML = `<div class="sv-empty">Could not load: ${escapeHtml(err.message)}</div>`;
      return;
    }
    if (seq !== this.detailSeq || !this.containerEl?.isConnected) return;
    const repo = this.tab?.repo || state.repoOf(this.tab) || state.activeRepo();
    el.innerHTML = renderCveDetailHtml(detail, {
      release: detail?.release_state?.release || "",
      showCopyLink: true,
      showNodemap: hasFeature(repo, "nodemap"),
      showCheckRelease: true,
      checkedRelease: release,
    });
  }

  onDetailClick(e, forceNew) {
    const nmBtn = e.target.closest("[data-nodemap-cve]");
    if (nmBtn) {
      e.preventDefault();
      const repo = this.tab?.repo || state.repoOf(this.tab) || state.activeRepo();
      const ver = nmBtn.dataset.nodemapRelease || this.version || state.activeVersion();
      sendToNodemap(cveSpec({ cveId: nmBtn.dataset.nodemapCve, version: ver, repo }), { repo, version: ver });
      return;
    }
    const copyBtn = e.target.closest("[data-copy-cve]");
    if (copyBtn) {
      e.preventDefault();
      const cveId = copyBtn.dataset.copyCve;
      const p = new URLSearchParams();
      const repo = this.tab?.repo || state.repoOf(this.tab) || state.activeRepo();
      if (repo) p.set("repo", repo);
      p.set("tab", "cve");
      p.set("id", cveId);
      copyToClipboard(buildDeepLink(p)).then((ok) => {
        if (ok) toast.success(`Copied link to ${cveId}`);
        else toast.error("Failed to copy link to clipboard");
      });
      return;
    }

    const file = e.target.closest("[data-open-file]");
    const commit = e.target.closest("[data-commit]");
    const symbol = e.target.closest("[data-symbol]");
    const nt = forceNew || e.ctrlKey || e.metaKey;
    if (file) {
      e.preventDefault();
      const path = file.dataset.openFile;
      state.openTab({
        type: "code",
        title: path.split("/").pop(),
        path,
        version: file.dataset.version || this.version || state.activeVersion(),
        cursorLine: parseInt(file.dataset.line || "1", 10) || 1,
        forceNew: nt,
      });
    } else if (commit) {
      e.preventDefault();
      const hash = commit.dataset.commit;
      state.openTab({
        type: "commits",
        title: `Commit ${hash.slice(0, 7)}`,
        commitId: hash,
        listCollapsed: true,
        forceNew: nt,
      });
    } else if (symbol) {
      e.preventDefault();
      state.openTab({
        type: "search",
        title: `Search: ${symbol.dataset.symbol}`,
        query: symbol.dataset.symbol,
        word: true,
        version: this.version || state.activeVersion(),
        forceNew: nt,
      });
    }
  }

  dispose() {
    this.listSeq++;
    this.detailSeq++;
    clearTimeout(this.queryTimer);
    clearTimeout(this.versionTimer);
    this.abort?.abort();
    this.detailAbort?.abort();
    this.list?.dispose();
    this.containerEl = null;
  }
}
