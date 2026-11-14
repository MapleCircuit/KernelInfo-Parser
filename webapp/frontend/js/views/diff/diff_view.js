/**
 * diff_view.js - Compare view: file and Kconfig deltas between any two tags or commits.
 */
import { API_KINDS, API_STATUSES, KIND_LABEL, apiDiffHtml, apiDiffMarkdown, apiDiffRows, apiDiffTsv, filterApiRows } from "./api_diff.js";
import {
  MAN_DOWNLOAD_FORMATS, MAN_STATUS_LABELS, MAN_STATUSES,
  filterManEntries, manDiffBadges, manDiffMarkdown, manDiffSections, manDiffTsv,
} from "./man_diff.js";
import { progressLabel } from "../../utils/job_poll.js";
import { api } from "../../api.js";
import { state } from "../../state.js";
import { refPicker, shortRef } from "../../components/ref_picker.js";
import { downloadText } from "../../components/modal.js";
import { contextMenu } from "../../components/context_menu.js";
import { debounce } from "../../utils/debounce.js";
import { isManPagesRepo, manPagesRepo } from "../../utils/projects.js";
import { manLabel, manTabFields, manTabTitle } from "../man/man_model.js";
import { previousTag } from "../code_view/file_diff.js";

const PAGE_SIZE = 500;
const KCONFIG_TYPES = { 1: "bool", 2: "tristate", 3: "string", 4: "hex", 5: "int" };
const STATUS_LABELS = { added: "A", modified: "M", deleted: "D", renamed: "R", unchanged: "U", removed: "D", changed: "M" };
const FILE_STATUSES = ["added", "modified", "deleted", "renamed", "unchanged"];
// Kconfig mode chips: option changes (added/changed/removed) and Kconfig file changes
// (added/modified/removed) both map onto these
const KCONFIG_STATUSES = ["added", "modified", "deleted"];
const KCONFIG_STATUS_OF = { added: "added", changed: "modified", modified: "modified", removed: "deleted", deleted: "deleted" };
const KCONFIG_DOWNLOAD_FORMATS = [
  { key: "options", label: "Options (.txt)", title: "One CONFIG_ option per line" },
  { key: "files", label: "Kconfig files (.txt)", title: "One Kconfig file path per line" },
  { key: "tsv", label: "Table (.tsv)", title: "Kind, status, name, defining files and changed fields per option and file" },
  { key: "json", label: "JSON (.json)", title: "Revisions, filters, every option with its definitions and every Kconfig file" },
];
const DOWNLOAD_FORMATS = [
  { key: "txt", label: "Paths (.txt)", title: "One path per line (the target path; the base path for deleted files)" },
  { key: "tsv", label: "Table (.tsv)", title: "Status, similarity, path and old path per file" },
  { key: "json", label: "JSON (.json)", title: "Revisions, filters and every file with its details" },
];

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export class DiffView {
  constructor() {
    this.tab = null;
    this.containerEl = null;
    this.versionA = "v3.0";
    this.versionB = "v3.0";
    this.currentMode = "files"; // "files" | "kconfig"
    this.fileChanges = [];
    this.fileFilter = "";
    this.statusFilter = new Set();
    this.visibleCount = PAGE_SIZE;
    this.kconfigResult = null;
    this.loadSeq = 0;
    // Rename search (shown while the renamed chip is active): the quick pass of the diff, the last search
    this.renameInfo = null;
    this.renameSimilarity = 50;
    this.renameScope = "";
    this.renameSearch = null; // { busy, seq, message, error }
    // Rename searches applied to this comparison, in order ([{ sim, dir }]); kept on the tab and in
    // the URL (§5.26) and re-applied whenever the diff is loaded again
    this.renameSearches = [];
    this.kconfigFilter = "";
    this.kconfigStatusFilter = new Set();
    this.manResult = null;
    this.manStatuses = new Set();
    this.manSection = "";
    this.manQuery = "";
    this.manVisibleCount = PAGE_SIZE;
    // Unchanged files, fetched when the unchanged chip is first selected
    this.unchangedFiles = null;
    this.unchangedLoading = null;
    this.unchangedError = "";
  }

  repo() {
    return this.tab?.repo || state.activeRepo();
  }

  isManRepo() {
    return isManPagesRepo(this.repo());
  }

  async render(containerEl, tabData) {
    this.tab = tabData;
    this.containerEl = containerEl;
    if (tabData.mode === "man" && !this.isManRepo()) {
      const mrepo = manPagesRepo();
      if (mrepo) {
        tabData.repo = mrepo;
        state.updateTabState(tabData.id, { repo: mrepo }, { replace: true });
      }
    }
    const isMan = this.isManRepo();
    const hasMan = isMan || Boolean(manPagesRepo());

    this.versionB = tabData.versionB || tabData.version || await this.defaultTarget();
    this.versionA = tabData.versionA || await this.defaultBaseFor(this.versionB);
    if (isMan) {
      this.currentMode = tabData.mode === "files" ? "files" : "man";
    } else {
      this.currentMode = ["kconfig", "api"].includes(tabData.mode) ? tabData.mode : "files";
    }
    // API mode (A6): kinds, statuses, filter and ARCH, recorded on the tab
    this.apiKinds = new Set(String(tabData.apiKinds || "").split(",").filter((k) => API_KINDS.includes(k)));
    this.apiStatuses = new Set(String(tabData.apiStatuses || "").split(",").filter((k) => API_STATUSES.includes(k)));
    this.apiQuery = tabData.apiQuery || "";
    this.apiArch = tabData.apiArch || state.kconfigArch() || "x86_64";
    // Man pages mode: status chips, section and text filter, recorded on the tab
    this.manStatuses = new Set(String(tabData.manStatuses || "").split(",").filter((k) => MAN_STATUSES.includes(k)));
    this.manSection = tabData.manSection || "";
    this.manQuery = tabData.manQuery || "";
    this.manVisibleCount = PAGE_SIZE;
    // View state recorded on the tab (and so in the URL, §5.26): chips, filters, rename searches
    this.statusFilter = new Set(String(tabData.statuses || "").split(",").filter((k) => FILE_STATUSES.includes(k)));
    this.fileFilter = tabData.query || "";
    this.kconfigFilter = tabData.kconfigQuery || "";
    this.kconfigStatusFilter = new Set(String(tabData.kconfigStatuses || "").split(",").filter((k) => KCONFIG_STATUSES.includes(k)));
    this.renameSearches = Array.isArray(tabData.renameSearches)
      ? tabData.renameSearches.filter((r) => r && Number.isFinite(r.sim)).map((r) => ({ sim: r.sim, dir: r.dir || "" }))
      : [];
    const lastSearch = this.renameSearches[this.renameSearches.length - 1];
    if (lastSearch) {
      this.renameSimilarity = lastSearch.sim;
      this.renameScope = lastSearch.dir;
    }

    const subtitle = isMan
      ? "Man entries and files changed between any two man-pages release tags or commits."
      : "Files, Kconfig and API changes between any two release tags or commits.";
    const modeButtons = isMan
      ? `<button class="code-btn" data-mode="man" type="button" title="Man entries added, modified or deleted">Man pages <span class="compare-mode-count" id="cmp-count-man"></span></button>
         <button class="code-btn" data-mode="files" type="button">Files <span class="compare-mode-count" id="cmp-count-files"></span></button>`
      : `<button class="code-btn" data-mode="files" type="button">Files <span class="compare-mode-count" id="cmp-count-files"></span></button>
         <button class="code-btn" data-mode="kconfig" type="button">Kconfig <span class="compare-mode-count" id="cmp-count-kconfig"></span></button>
         <button class="code-btn" data-mode="api" type="button" title="Kernel API changes: syscalls, exports, parameters, tracepoints, modules">API <span class="compare-mode-count" id="cmp-count-api"></span></button>
         ${hasMan ? `<button class="code-btn" data-mode="man" type="button" title="Compare man entries in the man-pages repository">Man pages <span class="compare-mode-count" id="cmp-count-man"></span></button>` : ""}`;

    containerEl.innerHTML = `
      <div class="tools-view-container compare-view">
        <div class="compare-header">
          <div>
            <div class="compare-title">Compare</div>
            <div class="compare-subtitle">${subtitle}</div>
          </div>
          <div class="compare-refs">
            <button class="compare-ref-btn" id="cmp-ref-a" type="button" title="Base revision">
              <span class="compare-ref-role">Base</span><span class="compare-ref-value"></span>
            </button>
            <button class="code-btn" id="cmp-swap" type="button" title="Swap base and target">⇄</button>
            <button class="compare-ref-btn" id="cmp-ref-b" type="button" title="Target revision">
              <span class="compare-ref-role">Target</span><span class="compare-ref-value"></span>
            </button>
          </div>
        </div>

        <div class="compare-modes">${modeButtons}</div>

        <div id="diff-results-container"></div>
      </div>
    `;

    this.persist({ rendering: true });
    this.updateRefButtons();
    this.updateModeButtons();

    const pick = (which) => (e) => {
      e.stopPropagation();
      const btn = e.currentTarget;
      refPicker.open(btn, {
        current: which === "a" ? this.versionA : this.versionB,
        client: this.api,
        onSelect: (ref) => {
          if (which === "a") this.versionA = ref;
          else this.versionB = ref;
          this.renameSearches = []; // they paired files of the previous revisions
          this.persist();
          this.updateRefButtons();
          this.loadDiff();
        }
      });
    };
    containerEl.querySelector("#cmp-ref-a").onclick = pick("a");
    containerEl.querySelector("#cmp-ref-b").onclick = pick("b");
    containerEl.querySelector("#cmp-swap").onclick = () => {
      [this.versionA, this.versionB] = [this.versionB, this.versionA];
      this.renameSearches = [];
      this.persist();
      this.updateRefButtons();
      this.loadDiff();
    };
    containerEl.querySelectorAll(".compare-modes .code-btn").forEach((btn) => {
      btn.onclick = () => {
        if (btn.dataset.mode === "man" && !this.isManRepo()) {
          const mrepo = manPagesRepo();
          if (mrepo) state.openTab({ type: "diff", title: "Compare", repo: mrepo, mode: "man" });
          return;
        }
        if (this.currentMode === btn.dataset.mode) return;
        this.currentMode = btn.dataset.mode;
        this.persist();
        this.updateModeButtons();
        this.loadDiff();
      };
    });

    await this.loadDiff();
  }

  /** Default target: the newest release tag of the tab's repository, else state.currentVersion. */
  async defaultTarget() {
    try {
      const res = await this.api.getVersionsCached(true);
      const all = (res.versions || []).filter((v) => typeof v === "string" || v.ref_type !== 3);
      const tags = all.map((v) => (typeof v === "string" ? v : v.vname)).filter(Boolean);
      const isPre = (name) => /-(rc|pre|alpha|beta)\d*$/.test(name);
      const finals = tags.filter((n) => !isPre(n));
      if (finals.length) return finals[finals.length - 1];
      if (tags.length) return tags[tags.length - 1];
    } catch {
      // fall back below
    }
    return state.currentVersion || "HEAD";
  }

  /** Default base: the release tag preceding target (or the latest tag when target is a commit). */
  async defaultBaseFor(target) {
    return previousTag(target, this.api);
  }

  /**
   * Record on this tab (and so in its URL, §5.26) what this view is showing. A view whose element
   * was replaced by a re-render is stale: its state is nobody's address any more.
   */
  persistTabState(patch, { rendering = false } = {}) {
    // render() runs before the pane attaches its element, so the render itself is never stale
    if (!this.tab || !this.containerEl || (!rendering && !this.containerEl.isConnected)) return;
    state.updateTabState(this.tab.id, patch, { replace: Boolean(rendering) });
  }

  persist(options = {}) {
    if (!this.tab) return;
    const title = `Compare ${shortRef(this.versionA)}…${shortRef(this.versionB)}`;
    const mode = this.currentMode === "files" && !this.isManRepo() ? "" : this.currentMode;
    this.persistTabState({
      versionA: this.versionA,
      versionB: this.versionB,
      mode,
      apiKinds: API_KINDS.filter((k) => this.apiKinds?.has(k)).join(","),
      apiStatuses: API_STATUSES.filter((k) => this.apiStatuses?.has(k)).join(","),
      apiQuery: this.apiQuery || "",
      apiArch: this.currentMode === "api" ? this.apiArch : "",
      manStatuses: MAN_STATUSES.filter((k) => this.manStatuses?.has(k)).join(","),
      manSection: this.manSection || "",
      manQuery: this.manQuery || "",
      statuses: FILE_STATUSES.filter((k) => this.statusFilter.has(k)).join(","),
      query: this.fileFilter,
      kconfigQuery: this.kconfigFilter,
      kconfigStatuses: KCONFIG_STATUSES.filter((k) => this.kconfigStatusFilter.has(k)).join(","),
      renameSearches: this.renameSearches.length ? this.renameSearches : null,
      title,
    }, options);
    const titleEl = document.querySelector(`.tab[data-tab-id="${this.tab.id}"] .tab-title`);
    if (titleEl) titleEl.textContent = title;
  }

  updateRefButtons() {
    const a = this.containerEl.querySelector("#cmp-ref-a .compare-ref-value");
    const b = this.containerEl.querySelector("#cmp-ref-b .compare-ref-value");
    if (a) a.textContent = shortRef(this.versionA);
    if (b) b.textContent = shortRef(this.versionB);
  }

  updateModeButtons() {
    this.containerEl.querySelectorAll(".compare-modes .code-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === this.currentMode);
    });
  }

  async loadDiff() {
    const resContainer = this.containerEl.querySelector("#diff-results-container");
    const seq = ++this.loadSeq;

    if (this.versionA === this.versionB) {
      resContainer.innerHTML = `<div class="compare-empty">Base and target are the same revision. Pick two different tags or commits to compare.</div>`;
      return;
    }

    resContainer.innerHTML = `<div class="compare-empty">Comparing ${escapeHtml(shortRef(this.versionA))} → ${escapeHtml(shortRef(this.versionB))}…</div>`;

    try {
      if (this.currentMode === "man") {
        const res = await this.api.getManDiff(this.repo(), this.versionA, this.versionB);
        if (seq !== this.loadSeq) return;
        this.manResult = res;
        this.manVisibleCount = PAGE_SIZE;
        this.renderManDiff(resContainer);
      } else if (this.currentMode === "api") {
        this.apiAbort?.abort();
        this.apiAbort = new AbortController();
        const res = await this.api.requestJob(`/api/kapi/diff?a=${encodeURIComponent(this.versionA)}&b=${encodeURIComponent(this.versionB)}&arch=${encodeURIComponent(this.apiArch)}`, {
          signal: this.apiAbort.signal, timeoutMs: 60000,
          onProgress: (p) => {
            if (seq === this.loadSeq) resContainer.innerHTML = `<div class="compare-empty">Building the Kernel API catalogs… ${escapeHtml(progressLabel(p))}</div>`;
          },
        });
        if (seq !== this.loadSeq) return;
        this.apiResult = res;
        this.renderApiDiff(resContainer);
      } else if (this.currentMode === "kconfig") {
        const res = await this.api.getKconfigDiff(this.versionA, this.versionB);
        if (seq !== this.loadSeq) return;
        this.kconfigResult = res;
        this.renderKconfigDiff(resContainer);
      } else {
        const res = await this.api.getVersionsDiff(this.versionA, this.versionB);
        if (seq !== this.loadSeq) return;
        this.fileChanges = res.changes || [];
        this.fileSummary = res.summary || {};
        this.renameInfo = res.renames || null;
        this.renameSearch = null;
        this.unchangedFiles = null;
        this.unchangedLoading = null;
        this.unchangedError = "";
        this.visibleCount = PAGE_SIZE;
        this.renderFileDiff(resContainer);
        await this.reapplyRenameSearches(seq);
      }
    } catch (e) {
      if (seq !== this.loadSeq) return;
      resContainer.innerHTML = `<div class="compare-empty error">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  // ---------------------------------------------------------------------------
  // API mode (A6)
  // ---------------------------------------------------------------------------
  renderApiDiff(container) {
    const res = this.apiResult;
    const rows = apiDiffRows(res);
    const count = this.containerEl.querySelector("#cmp-count-api");
    if (count) count.textContent = rows.length ? `(${rows.length})` : "";
    container.innerHTML = `<div class="compare-api-arch">ARCH <select class="api-diff-arch"><option value="${escapeHtml(this.apiArch)}">${escapeHtml(this.apiArch)}</option></select>
      <span class="compare-subtitle">Renames are not inferred: a renamed symbol shows as removed + added.</span></div>`
      + apiDiffHtml(res, rows, { kinds: this.apiKinds, statuses: this.apiStatuses, query: this.apiQuery });
    const archSel = container.querySelector(".api-diff-arch");
    this.api.getKconfigPresets(this.versionB).then((p) => {
      archSel.innerHTML = (p?.targets || []).map((t) => `<option value="${escapeHtml(t.id)}" ${t.id === this.apiArch ? "selected" : ""}>${escapeHtml(t.label || t.id)}</option>`).join("") || archSel.innerHTML;
    }).catch(() => {});
    archSel.onchange = () => {
      this.apiArch = archSel.value;
      this.persist();
      this.loadDiff();
    };
    const rerender = () => {
      this.persist();
      this.renderApiDiff(container);
    };
    container.querySelectorAll("[data-api-kind]").forEach((b) => {
      b.onclick = () => {
        const k = b.dataset.apiKind;
        if (this.apiKinds.has(k)) this.apiKinds.delete(k);
        else this.apiKinds.add(k);
        rerender();
      };
    });
    container.querySelectorAll("[data-api-status]").forEach((b) => {
      b.onclick = () => {
        const k = b.dataset.apiStatus;
        if (this.apiStatuses.has(k)) this.apiStatuses.delete(k);
        else this.apiStatuses.add(k);
        rerender();
      };
    });
    const filter = container.querySelector(".api-diff-filter");
    let t = null;
    filter.oninput = () => {
      clearTimeout(t);
      t = setTimeout(() => {
        this.apiQuery = filter.value.trim();
        this.persist();
        const shown = filterApiRows(rows, this.apiKinds, this.apiStatuses, this.apiQuery);
        const dlBtn = container.querySelector(".api-diff-download");
        if (dlBtn) dlBtn.textContent = `⬇ Download (${shown.length})`;
        const listEl = container.querySelector(".api-diff-list");
        if (listEl) {
          listEl.innerHTML = shown.slice(0, 3000).map((r) => `<div class="api-diff-row status-${r.status}" data-kind="${r.kind}" data-key="${escapeHtml(r.key)}" data-status="${r.status}">
            <span class="api-diff-status">${r.status === "added" ? "+" : r.status === "removed" ? "−" : "~"}</span>
            <span class="api-diff-kind">${escapeHtml(KIND_LABEL[r.kind] || r.kind)}</span>
            <span class="api-diff-key">${escapeHtml(r.key)}</span>
            <span class="api-diff-detail">${escapeHtml(r.detail)}</span></div>`).join("") || '<div class="compare-empty">No API change matches.</div>'
            + (shown.length > 3000 ? `<div class="compare-empty">… ${shown.length - 3000} more (download the table)</div>` : "");
        }
      }, 200);
    };
    container.querySelector(".api-diff-download").onclick = (e) => {
      const shown = filterApiRows(rows, this.apiKinds, this.apiStatuses, this.apiQuery);
      const r = e.currentTarget.getBoundingClientRect();
      const base = `api-${String(this.versionA).replace(/[^A-Za-z0-9._-]/g, "_")}-${String(this.versionB).replace(/[^A-Za-z0-9._-]/g, "_")}`;
      contextMenu.renderMenu(r.left, r.bottom + 4, { header: { title: "Download", sub: `${shown.length} changes` }, items: [
        { label: "Summary (.md)", icon: "⬇", onClick: () => downloadText(`${base}.md`, apiDiffMarkdown(res, shown), "text/markdown") },
        { label: "Table (.tsv)", icon: "⬇", onClick: () => downloadText(`${base}.tsv`, apiDiffTsv(shown)) },
        { label: "JSON", icon: "⬇", onClick: () => downloadText(`${base}.json`, JSON.stringify(res, null, 1), "application/json") },
      ] });
    };
    const open = (row, forceNew) => {
      const removed = row.dataset.status === "removed";
      state.openTab({ type: "api", title: "Kernel API", view: row.dataset.kind, sel: "", query: row.dataset.key,
        arch: this.apiArch, version: removed ? this.versionA : this.versionB, forceNew });
    };
    container.querySelector(".api-diff-list").addEventListener("click", (e) => {
      const row = e.target.closest(".api-diff-row");
      if (row) open(row, e.ctrlKey || e.metaKey);
    });
    container.querySelector(".api-diff-list").addEventListener("auxclick", (e) => {
      const row = e.target.closest(".api-diff-row");
      if (row && e.button === 1) {
        e.preventDefault();
        open(row, true);
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Files mode
  // ---------------------------------------------------------------------------

  renderFileDiff(resContainer) {
    const s = this.fileSummary;
    const countEl = this.containerEl.querySelector("#cmp-count-files");
    if (countEl) countEl.textContent = this.fileChanges.length;

    const chip = (key, label, count) => `
      <button class="compare-chip ${key} ${this.statusFilter.has(key) ? "active" : ""}" data-status="${key}" type="button">
        <span class="compare-chip-count">${count ?? 0}</span> ${label}
      </button>`;

    resContainer.innerHTML = `
      <div class="compare-toolbar">
        <div class="compare-chips">
          ${chip("added", "added", s.added_count)}
          ${chip("modified", "modified", s.modified_count)}
          ${chip("deleted", "deleted", s.removed_count)}
          ${chip("renamed", "renamed", s.renamed_count)}
          ${chip("unchanged", "unchanged", s.unchanged_count)}
        </div>
        <div class="compare-toolbar-right">
          <input type="text" class="compare-filter" id="cmp-file-filter" placeholder="Filter by path…" value="${escapeHtml(this.fileFilter)}" spellcheck="false" />
          ${this.downloadMenuHtml(DOWNLOAD_FORMATS, "Download every file matching the current filters (all pages)")}
        </div>
      </div>
      <div id="cmp-rename-search"></div>
      <div class="compare-list" id="cmp-file-list"></div>
    `;

    resContainer.querySelectorAll(".compare-chip[data-status]").forEach((btn) => {
      btn.onclick = () => {
        const key = btn.dataset.status;
        if (this.statusFilter.has(key)) this.statusFilter.delete(key);
        else this.statusFilter.add(key);
        btn.classList.toggle("active", this.statusFilter.has(key));
        this.visibleCount = PAGE_SIZE;
        if (key === "renamed") this.renderRenameSearch();
        if (key === "unchanged") this.unchangedError = ""; // a reselected chip retries a failed fetch
        this.persist();
        this.renderFileRows();
      };
    });

    const filterInput = resContainer.querySelector("#cmp-file-filter");
    const applyFileFilter = debounce(() => {
      this.visibleCount = PAGE_SIZE;
      this.renderFileRows();
      this.persist();
    }, 120);
    filterInput.oninput = () => {
      this.fileFilter = filterInput.value;
      applyFileFilter();
    };

    this.wireDownloadMenu(resContainer, (format) => this.downloadFileRows(format));

    this.renderRenameSearch();
    this.renderFileRows();
  }

  /** The ⬇ Download button and its format menu (both modes). */
  downloadMenuHtml(formats, title) {
    return `
      <div class="compare-download">
        <button class="code-btn" id="cmp-download" type="button" title="${escapeHtml(title)}">⬇ Download <span id="cmp-download-count"></span></button>
        <div class="compare-download-menu" id="cmp-download-menu" hidden>
          ${formats.map((f) => `<button type="button" data-format="${f.key}" title="${escapeHtml(f.title)}">${escapeHtml(f.label)}</button>`).join("")}
        </div>
      </div>`;
  }

  wireDownloadMenu(resContainer, onPick) {
    const menu = resContainer.querySelector("#cmp-download-menu");
    const closeMenu = (e) => {
      if (e && menu.parentElement.contains(e.target)) return;
      menu.hidden = true;
      document.removeEventListener("mousedown", closeMenu, true);
    };
    resContainer.querySelector("#cmp-download").onclick = () => {
      if (!menu.hidden) return closeMenu();
      menu.hidden = false;
      document.addEventListener("mousedown", closeMenu, true);
    };
    menu.querySelectorAll("[data-format]").forEach((btn) => {
      btn.onclick = () => {
        closeMenu();
        onPick(btn.dataset.format);
      };
    });
  }

  /** File name stem shared by both download kinds: `<prefix>_<base>..<target>[_<statuses>][_<filter>]`. */
  downloadName(prefix, statuses, filter) {
    const safe = (v) => String(v).replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "");
    return [prefix, `${safe(shortRef(this.versionA))}..${safe(shortRef(this.versionB))}`,
      statuses.length ? statuses.join("-") : "", filter ? safe(filter).slice(0, 40) : ""].filter(Boolean).join("_");
  }

  // ---------------------------------------------------------------------------
  // Rename search: pairs Git's quick pass left as added + deleted
  // ---------------------------------------------------------------------------

  renderRenameSearch() {
    const box = this.containerEl.querySelector("#cmp-rename-search");
    if (!box) return;
    if (!this.statusFilter.has("renamed")) {
      box.innerHTML = "";
      return;
    }
    const info = this.renameInfo;
    const search = this.renameSearch;
    let note;
    if (!info || info.source !== "git") {
      note = "Only renames with identical content are detected for these revisions (no Git trees).";
    } else if (info.exhaustive) {
      note = `Renames are files at least ${info.similarity}% alike; every deleted/added pair was compared.`;
    } else {
      note = `Renames are files at least ${info.similarity}% alike, but there were too many added and deleted files to compare every pair: `
        + "only identical files and files that kept their name were paired"
        + (info.limit_needed ? ` (Git would need a rename limit of ${Number(info.limit_needed).toLocaleString()})` : "")
        + ". Search to compare every pair (narrow it to a directory for large ranges).";
    }
    // Directories holding unpaired added or deleted files, for the scope suggestions
    const dirs = new Map();
    for (const ch of this.fileChanges) {
      if (ch.type !== "added" && ch.type !== "deleted") continue;
      const parts = (ch.file_path || "").split("/");
      for (let depth = 1; depth <= Math.min(3, parts.length - 1); depth++) {
        const d = parts.slice(0, depth).join("/");
        dirs.set(d, (dirs.get(d) || 0) + 1);
      }
    }
    const dirOptions = [...dirs.entries()].sort((a, b) => b[1] - a[1]).slice(0, 200)
      .map(([d, n]) => `<option value="${escapeHtml(d)}">${n} unpaired</option>`).join("");
    const canSearch = info?.source === "git";

    box.innerHTML = `
      <div class="compare-rename-search">
        <div class="compare-rename-note">${escapeHtml(note)}</div>
        ${canSearch ? `
        <form class="compare-rename-form" id="cmp-rename-form">
          <label>Similarity ≥ <input type="number" id="cmp-rename-sim" min="1" max="100" step="1" value="${this.renameSimilarity}" />%</label>
          <label>in <input type="text" id="cmp-rename-scope" list="cmp-rename-dirs" placeholder="whole tree" value="${escapeHtml(this.renameScope)}" spellcheck="false" /></label>
          <datalist id="cmp-rename-dirs">${dirOptions}</datalist>
          <button class="code-btn" type="submit" ${search?.busy ? "disabled" : ""}>${search?.busy ? "Searching…" : "Search renames"}</button>
        </form>` : ""}
        ${search?.message ? `<div class="compare-rename-status ${search.error ? "error" : ""}">${escapeHtml(search.message)}</div>` : ""}
      </div>`;

    const form = box.querySelector("#cmp-rename-form");
    if (form) {
      form.onsubmit = (e) => {
        e.preventDefault();
        const sim = parseInt(box.querySelector("#cmp-rename-sim").value, 10);
        this.renameSimilarity = Number.isFinite(sim) ? Math.max(1, Math.min(100, sim)) : 50;
        this.renameScope = box.querySelector("#cmp-rename-scope").value.trim().replace(/^\/+|\/+$/g, "");
        this.runRenameSearch();
      };
    }
  }

  /** Re-apply the rename searches recorded on the tab (from the URL or before a reload); cached, so fast. */
  async reapplyRenameSearches(seq) {
    const searches = this.renameSearches.slice();
    for (const r of searches) {
      if (seq !== this.loadSeq) return;
      this.renameSimilarity = r.sim;
      this.renameScope = r.dir;
      await this.runRenameSearch({ record: false });
    }
  }

  /** Record an applied search; a search re-pairs its whole directory, so it supersedes those inside it. */
  recordRenameSearch(sim, dir) {
    const inside = (d) => !dir || d === dir || d.startsWith(`${dir}/`);
    this.renameSearches = [...this.renameSearches.filter((r) => !inside(r.dir)), { sim, dir }];
    this.persist();
  }

  async runRenameSearch({ record = true } = {}) {
    const seq = this.loadSeq;
    const scope = this.renameScope;
    const similarity = this.renameSimilarity;
    const where = scope ? `under ${scope}/` : "in the whole tree";
    this.renameSearch = { busy: true, message: `Comparing every deleted with every added file ${where}…` };
    this.renderRenameSearch();
    const started = performance.now();
    try {
      const res = await this.api.searchRenames(this.versionA, this.versionB, scope, similarity);
      if (seq !== this.loadSeq) return;
      const c = res.candidates || {};
      if (res.reason === "too_many") {
        const pairs = (c.pairs || 0).toLocaleString();
        this.renameSearch = {
          error: true,
          message: `${(c.deleted || 0).toLocaleString()} deleted × ${(c.added || 0).toLocaleString()} added files ${where} = ${pairs} pairs, `
            + `above the limit of ${(res.max_pairs || 0).toLocaleString()}. Pick a directory to narrow the search.`
        };
      } else if (res.reason === "failed") {
        this.renameSearch = { error: true, message: "The rename search failed (Git did not answer in time)." };
      } else {
        const { found, added } = this.applyRenameSearch(res);
        if (record) this.recordRenameSearch(res.similarity, res.path || "");
        const secs = ((performance.now() - started) / 1000).toFixed(1);
        this.renameSearch = {
          message: `${found} rename${found === 1 ? "" : "s"} at ≥${res.similarity}% ${where} `
            + `(${added >= 0 ? "+" : ""}${added} vs. before; ${(c.pairs || 0).toLocaleString()} pairs compared, ${secs}s).`
        };
        this.renderFileDiff(this.containerEl.querySelector("#diff-results-container"));
        return;
      }
    } catch (e) {
      if (seq !== this.loadSeq) return;
      this.renameSearch = { error: true, message: `Rename search failed: ${e.message}` };
    }
    this.renderRenameSearch();
  }

  /**
   * Replace the pairing of the search's scope with the search result. Renames with both ends in scope
   * go back to added + deleted and the search pairs them again (a higher threshold drops pairs, a lower
   * one adds them); renames crossing the scope boundary are kept, since the search did not see them.
   */
  applyRenameSearch(res) {
    const scope = res.path || "";
    const inScope = (p) => !scope || p === scope || (p || "").startsWith(`${scope}/`);
    const rows = [];
    const added = new Map();
    const deleted = new Map();
    let before = 0;
    const row = (type, path, fid) => ({ type, file: path, file_path: path, name: path, fid: fid || 0 });
    for (const ch of this.fileChanges) {
      if (ch.type === "renamed" && inScope(ch.file_path) && inScope(ch.old_file)) {
        before++;
        added.set(ch.file_path, row("added", ch.file_path, ch.fid));
        deleted.set(ch.old_file, row("deleted", ch.old_file, 0));
      } else if (ch.type === "added" && inScope(ch.file_path)) {
        added.set(ch.file_path, ch);
      } else if (ch.type === "deleted" && inScope(ch.file_path)) {
        deleted.set(ch.file_path, ch);
      } else {
        rows.push(ch);
      }
    }
    let found = 0;
    for (const r of res.renames || []) {
      if (!added.has(r.file) || !deleted.has(r.old_file)) continue;
      added.delete(r.file);
      deleted.delete(r.old_file);
      rows.push({ ...row("renamed", r.file, r.fid), old_file: r.old_file, similarity: r.similarity });
      found++;
    }
    rows.push(...added.values(), ...deleted.values());
    rows.sort((a, b) => (a.file_path < b.file_path ? -1 : a.file_path > b.file_path ? 1 : 0));
    this.fileChanges = rows;
    const count = (t) => rows.filter((ch) => ch.type === t).length;
    this.fileSummary = {
      ...this.fileSummary,
      added_count: count("added"),
      removed_count: count("deleted"),
      renamed_count: count("renamed"),
    };
    this.visibleCount = PAGE_SIZE;
    return { found, added: found - before };
  }

  /** Fetch the unchanged files once per comparison (the unchanged chip); re-renders the rows when done. */
  ensureUnchangedFiles() {
    if (this.unchangedFiles || this.unchangedLoading) return this.unchangedLoading || Promise.resolve();
    const seq = this.loadSeq;
    this.unchangedError = "";
    this.unchangedLoading = this.api.getUnchangedFiles(this.versionA, this.versionB).then((res) => {
      if (seq !== this.loadSeq) return;
      this.unchangedFiles = (res.files || []).map((p) => ({ type: "unchanged", file: p, file_path: p, name: p }));
    }).catch((e) => {
      if (seq !== this.loadSeq) return;
      this.unchangedError = e.message || String(e);
    }).finally(() => {
      if (seq !== this.loadSeq) return;
      this.unchangedLoading = null;
      this.renderFileRows();
    });
    return this.unchangedLoading;
  }

  /**
   * Rows matching the chips and the path filter. No chip = every change (unchanged files only appear
   * when their chip is selected); unchanged rows are merged in path order.
   */
  filteredFileChanges() {
    const q = this.fileFilter.trim().toLowerCase();
    let source = this.fileChanges;
    if (this.statusFilter.has("unchanged") && this.unchangedFiles) {
      source = this.statusFilter.size === 1
        ? this.unchangedFiles
        : [...this.fileChanges, ...this.unchangedFiles].sort((a, b) => (a.file_path < b.file_path ? -1 : a.file_path > b.file_path ? 1 : 0));
    }
    return source.filter((ch) => {
      if (this.statusFilter.size > 0 && !this.statusFilter.has(ch.type)) return false;
      if (q && !(ch.file_path || "").toLowerCase().includes(q) && !(ch.old_file || "").toLowerCase().includes(q)) return false;
      return true;
    });
  }

  renderFileRows() {
    const listEl = this.containerEl.querySelector("#cmp-file-list");
    if (!listEl) return;
    const wantsUnchanged = this.statusFilter.has("unchanged");
    if (wantsUnchanged && !this.unchangedFiles && !this.unchangedError) this.ensureUnchangedFiles();
    const rows = this.filteredFileChanges();
    const pending = wantsUnchanged && !this.unchangedFiles;
    const countEl = this.containerEl.querySelector("#cmp-download-count");
    if (countEl) countEl.textContent = pending ? "" : `(${rows.length.toLocaleString()})`;
    const dlBtn = this.containerEl.querySelector("#cmp-download");
    if (dlBtn) dlBtn.disabled = pending || rows.length === 0;
    const note = pending
      ? `<div class="compare-empty ${this.unchangedError ? "error" : ""}">${this.unchangedError
        ? `Failed to load the unchanged files: ${escapeHtml(this.unchangedError)}`
        : "Loading unchanged files…"}</div>`
      : "";

    if (rows.length === 0) {
      listEl.innerHTML = note || `<div class="compare-empty">${this.fileChanges.length === 0 && !wantsUnchanged ? "No file differences between these revisions." : "No files match the current filter."}</div>`;
      return;
    }

    const shown = rows.slice(0, this.visibleCount);
    listEl.innerHTML = note + shown.map((ch, idx) => this.fileRowHtml(ch, idx)).join("") + (
      rows.length > shown.length
        ? `<button class="compare-more" id="cmp-more" type="button">Show ${Math.min(PAGE_SIZE, rows.length - shown.length)} more (${rows.length - shown.length} remaining)</button>`
        : ""
    );

    listEl.querySelectorAll(".compare-row").forEach((rowEl) => {
      const ch = shown[parseInt(rowEl.dataset.idx, 10)];
      // An unchanged file has no diff: a click opens it at the target revision
      rowEl.querySelector(".compare-row-main").onclick = ch.type === "unchanged"
        ? () => this.openFile(this.versionB, ch.file_path)
        : () => this.toggleFileDiff(rowEl, ch.file_path, ch.old_file, ch.similarity);
      rowEl.querySelector(".compare-row-main").addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          this.openFile(ch.type === "deleted" ? this.versionA : this.versionB, ch.file_path, true);
        }
      });
      rowEl.querySelectorAll("[data-open]").forEach((btn) => {
        btn.onclick = (e) => {
          e.stopPropagation();
          const isBase = btn.dataset.open === "a";
          this.openFile(isBase ? this.versionA : this.versionB, isBase ? (ch.old_file || ch.file_path) : ch.file_path);
        };
      });
    });

    const moreBtn = listEl.querySelector("#cmp-more");
    if (moreBtn) {
      moreBtn.onclick = () => {
        this.visibleCount += PAGE_SIZE;
        this.renderFileRows();
      };
    }
  }

  fileRowHtml(ch, idx) {
    const type = ch.type || "modified";
    const path = escapeHtml(ch.file_path);
    const renameFrom = type === "renamed" ? `<span class="compare-row-old">${escapeHtml(ch.old_file)} →</span>` : "";
    const similarity = type === "renamed" && ch.similarity != null && ch.similarity < 100
      ? `<span class="compare-row-sim" title="Content similarity of the renamed file">${Number(ch.similarity)}%</span>`
      : "";
    const symDelta = (ch.symbols_added || ch.symbols_removed)
      ? `<span class="compare-row-syms" title="Symbol definitions added / removed">+${ch.symbols_added || 0} / −${ch.symbols_removed || 0} sym</span>`
      : "";
    const canOpenBase = type !== "added";
    const canOpenTarget = type !== "deleted";
    return `
      <div class="compare-row" data-idx="${idx}">
        <div class="compare-row-main" title="${type === "unchanged" ? "Identical at both revisions · click to open" : "Click to show the diff · middle-click to open"}">
          <span class="compare-status ${type}">${STATUS_LABELS[type] || "?"}</span>
          <span class="compare-row-path">${renameFrom}${path}</span>
          ${similarity}
          ${symDelta}
          <span class="compare-row-actions">
            ${canOpenBase ? `<button class="code-btn" data-open="a" type="button" title="Open at ${escapeHtml(this.versionA)}">${escapeHtml(shortRef(this.versionA))}</button>` : ""}
            ${canOpenTarget ? `<button class="code-btn" data-open="b" type="button" title="Open at ${escapeHtml(this.versionB)}">${escapeHtml(shortRef(this.versionB))}</button>` : ""}
          </span>
        </div>
      </div>`;
  }

  /** Download every row matching the chips and the path filter (all pages, not only the rendered ones). */
  async downloadFileRows(format) {
    if (this.statusFilter.has("unchanged")) await this.ensureUnchangedFiles();
    const rows = this.filteredFileChanges();
    if (rows.length === 0) return;
    const statuses = [...this.statusFilter].sort();
    const filter = this.fileFilter.trim();
    const name = this.downloadName("compare", statuses, filter);
    // A deleted file only exists at the base; every other row is named by its target path
    const pathOf = (ch) => ch.file_path;

    if (format === "txt") {
      downloadText(`${name}.txt`, rows.map(pathOf).join("\n") + "\n");
      return;
    }
    if (format === "tsv") {
      const cell = (v) => String(v ?? "").replace(/[\t\r\n]/g, " ");
      const lines = [["status", "similarity", "path", "old_path"].join("\t")];
      for (const ch of rows) {
        lines.push([
          STATUS_LABELS[ch.type] || "?",
          ch.type === "renamed" ? (ch.similarity ?? "") : "",
          pathOf(ch),
          ch.type === "renamed" ? ch.old_file : "",
        ].map(cell).join("\t"));
      }
      downloadText(`${name}.tsv`, lines.join("\n") + "\n", "text/tab-separated-values");
      return;
    }
    const data = {
      base: this.versionA,
      target: this.versionB,
      generated: new Date().toISOString(),
      filters: {
        statuses: statuses.length ? statuses : ["added", "modified", "deleted", "renamed"],
        path_contains: filter,
        rename_search: this.renameSearch && !this.renameSearch.error && !this.renameSearch.busy
          ? { similarity: this.renameSimilarity, directory: this.renameScope } : null,
      },
      renames: this.renameInfo,
      count: rows.length,
      files: rows.map((ch) => {
        const out = { status: ch.type, path: pathOf(ch) };
        if (ch.type === "renamed") Object.assign(out, { old_path: ch.old_file, similarity: ch.similarity ?? null });
        return out;
      }),
    };
    downloadText(`${name}.json`, JSON.stringify(data, null, 2) + "\n", "application/json");
  }

  async toggleFileDiff(rowEl, path, oldPath = "", similarity = 50) {
    const existing = rowEl.querySelector(".compare-row-diff");
    if (existing) {
      existing.remove();
      rowEl.classList.remove("expanded");
      return;
    }
    rowEl.classList.add("expanded");
    const box = document.createElement("div");
    box.className = "compare-row-diff diff-box";
    box.innerHTML = `<div class="diff-content compare-diff-loading">Loading diff…</div>`;
    rowEl.appendChild(box);

    try {
      const res = await this.api.getFileDiff(this.versionA, this.versionB, path, oldPath || "", similarity ?? 50);
      if (!rowEl.contains(box)) return;
      if (res.is_binary) {
        box.innerHTML = `<div class="diff-content">Binary file changed.</div>`;
        return;
      }
      const lines = (res.diff || "").split("\n");
      const body = lines.map((line) => {
        let cls = "";
        if (line.startsWith("@@")) cls = "diff-hunk";
        else if (line.startsWith("+") && !line.startsWith("+++")) cls = "diff-add";
        else if (line.startsWith("-") && !line.startsWith("---")) cls = "diff-del";
        return `<div class="${cls}">${escapeHtml(line) || " "}</div>`;
      }).join("");
      box.innerHTML = `
        <div class="diff-header">
          <span class="diff-add">+${res.additions}</span> <span class="diff-del">−${res.deletions}</span>
          ${res.truncated ? `<span class="compare-diff-note">(diff truncated)</span>` : ""}
        </div>
        <div class="diff-content">${body || "No textual changes."}</div>`;
    } catch (e) {
      if (rowEl.contains(box)) {
        box.innerHTML = `<div class="diff-content compare-diff-error">Failed to load diff: ${escapeHtml(e.message)}</div>`;
      }
    }
  }

  openFile(version, path, forceNew = false) {
    if (!path) return;
    state.openTab({
      type: "code",
      title: path.split("/").pop(),
      path,
      version,
      repo: this.repo(),
      forceNew
    });
  }

  // ---------------------------------------------------------------------------
  // Man pages mode
  // ---------------------------------------------------------------------------

  renderManDiff(resContainer) {
    const res = this.manResult || {};
    const s = res.summary || {};
    const entries = res.entries || [];
    const sections = manDiffSections(entries);
    if (this.manSection && !sections.some((sec) => sec.section === this.manSection)) {
      this.manSection = "";
    }

    const countEl = this.containerEl.querySelector("#cmp-count-man");
    if (countEl) countEl.textContent = entries.length;

    const chip = (key, label, count) => `
      <button class="compare-chip ${key} ${this.manStatuses.has(key) ? "active" : ""}" data-man-status="${key}" type="button">
        <span class="compare-chip-count">${count ?? 0}</span> ${label}
      </button>`;

    const secOptions = sections.map((sec) => `<option value="${escapeHtml(sec.section)}" ${sec.section === this.manSection ? "selected" : ""}>${escapeHtml(`${sec.section} — ${sec.title} (${sec.count})`)}</option>`).join("");

    resContainer.innerHTML = `
      <div class="compare-toolbar">
        <div class="compare-chips">
          ${chip("added", "added", s.added)}
          ${chip("modified", "modified", s.modified)}
          ${chip("deleted", "deleted", s.deleted)}
        </div>
        <div class="compare-toolbar-right">
          <select class="compare-man-section" id="cmp-man-section" title="Filter by man section">
            <option value="">All sections (${entries.length})</option>
            ${secOptions}
          </select>
          <input type="text" class="compare-filter" id="cmp-man-filter" placeholder="Filter entries, summaries, symbols…" value="${escapeHtml(this.manQuery)}" spellcheck="false" />
          ${this.downloadMenuHtml(MAN_DOWNLOAD_FORMATS, "Download every man entry matching the current filters")}
        </div>
      </div>
      <div class="compare-man-summary-bar">
        ${entries.length.toLocaleString()} changed · ${(s.unchanged ?? 0).toLocaleString()} unchanged · ${(s.total_b ?? 0).toLocaleString()} entries in ${escapeHtml(shortRef(this.versionB))}${res.source ? ` · source: ${escapeHtml(res.source)}` : ""}
      </div>
      <div class="compare-list" id="cmp-man-list"></div>
    `;

    resContainer.querySelectorAll(".compare-chip[data-man-status]").forEach((btn) => {
      btn.onclick = () => {
        const key = btn.dataset.manStatus;
        if (this.manStatuses.has(key)) this.manStatuses.delete(key);
        else this.manStatuses.add(key);
        btn.classList.toggle("active", this.manStatuses.has(key));
        this.manVisibleCount = PAGE_SIZE;
        this.persist();
        this.renderManRows();
      };
    });

    const secSelect = resContainer.querySelector("#cmp-man-section");
    secSelect.onchange = () => {
      this.manSection = secSelect.value;
      this.manVisibleCount = PAGE_SIZE;
      this.persist();
      this.renderManRows();
    };

    const filterInput = resContainer.querySelector("#cmp-man-filter");
    const applyManFilter = debounce(() => {
      this.manVisibleCount = PAGE_SIZE;
      this.renderManRows();
      this.persist();
    }, 120);
    filterInput.oninput = () => {
      this.manQuery = filterInput.value;
      applyManFilter();
    };

    this.wireDownloadMenu(resContainer, (format) => this.downloadManRows(format));
    this.renderManRows();
  }

  filteredManEntries() {
    return filterManEntries(this.manResult?.entries || [], this.manStatuses, this.manSection, this.manQuery);
  }

  renderManRows() {
    const listEl = this.containerEl.querySelector("#cmp-man-list");
    if (!listEl) return;
    const rows = this.filteredManEntries();
    const countEl = this.containerEl.querySelector("#cmp-download-count");
    if (countEl) countEl.textContent = `(${rows.length.toLocaleString()})`;
    const dlBtn = this.containerEl.querySelector("#cmp-download");
    if (dlBtn) dlBtn.disabled = rows.length === 0;

    if (rows.length === 0) {
      const total = (this.manResult?.entries || []).length;
      listEl.innerHTML = `<div class="compare-empty">${total === 0 ? "No man entry differences between these revisions." : "No man entries match the current filter."}</div>`;
      return;
    }

    const shown = rows.slice(0, this.manVisibleCount);
    listEl.innerHTML = shown.map((entry, idx) => this.manRowHtml(entry, idx)).join("") + (
      rows.length > shown.length
        ? `<button class="compare-more" id="cmp-man-more" type="button">Show ${Math.min(PAGE_SIZE, rows.length - shown.length)} more (${rows.length - shown.length} remaining)</button>`
        : ""
    );

    listEl.querySelectorAll(".compare-row").forEach((rowEl) => {
      const entry = shown[parseInt(rowEl.dataset.idx, 10)];
      rowEl.querySelector(".compare-row-main").onclick = () => {
        const targetPath = entry.path_b || entry.path_a || entry.path;
        const oldPath = entry.status === "modified" && entry.path_a && entry.path_b && entry.path_a !== entry.path_b ? entry.path_a : "";
        this.toggleFileDiff(rowEl, targetPath, oldPath, oldPath ? 1 : 50);
      };
      rowEl.querySelector(".compare-row-main").addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          this.openManEntry(entry.status === "deleted" ? this.versionA : this.versionB, entry, true);
        }
      });
      rowEl.querySelectorAll("[data-man-open]").forEach((btn) => {
        btn.onclick = (e) => {
          e.stopPropagation();
          const isBase = btn.dataset.manOpen === "a";
          this.openManEntry(isBase ? this.versionA : this.versionB, entry, e.ctrlKey || e.metaKey);
        };
      });
    });

    const moreBtn = listEl.querySelector("#cmp-man-more");
    if (moreBtn) {
      moreBtn.onclick = () => {
        this.manVisibleCount += PAGE_SIZE;
        this.renderManRows();
      };
    }
  }

  manRowHtml(entry, idx) {
    const status = entry.status || "modified";
    const badges = manDiffBadges(entry).map((b) => `<span class="compare-man-badge kind-${escapeHtml(b.kind)}" title="${escapeHtml(b.title)}">${escapeHtml(b.text)}</span>`).join("");
    const canOpenBase = status !== "added";
    const canOpenTarget = status !== "deleted";
    return `
      <div class="compare-row" data-idx="${idx}">
        <div class="compare-row-main" title="Click to show the diff · middle-click to open man page">
          <span class="compare-status ${status}">${MAN_STATUS_LABELS[status] || "?"}</span>
          <span class="compare-man-name">${escapeHtml(manLabel(entry.name, entry.section))}</span>
          <span class="compare-man-summary" title="${escapeHtml(entry.summary || "")}">${escapeHtml(entry.summary || "")}</span>
          ${badges}
          <span class="compare-row-actions">
            ${canOpenBase ? `<button class="code-btn" data-man-open="a" type="button" title="Open man page at ${escapeHtml(this.versionA)}">${escapeHtml(shortRef(this.versionA))}</button>` : ""}
            ${canOpenTarget ? `<button class="code-btn" data-man-open="b" type="button" title="Open man page at ${escapeHtml(this.versionB)}">${escapeHtml(shortRef(this.versionB))}</button>` : ""}
          </span>
        </div>
      </div>`;
  }

  openManEntry(version, entry, forceNew = false) {
    if (!entry) return;
    const path = version === this.versionA ? (entry.path_a || "") : (entry.path_b || "");
    state.openTab({
      type: "man",
      title: manTabTitle({ manPage: entry.name, manSec: entry.section }),
      ...manTabFields({
        repo: this.repo(),
        version,
        name: entry.name,
        section: entry.section,
        path,
      }),
      forceNew,
    });
  }

  downloadManRows(format) {
    const rows = this.filteredManEntries();
    if (rows.length === 0) return;
    const statuses = MAN_STATUSES.filter((k) => this.manStatuses.has(k));
    const filter = [this.manSection, this.manQuery.trim()].filter(Boolean).join("_");
    const name = this.downloadName("compare-man", statuses, filter);

    if (format === "txt") {
      downloadText(`${name}.txt`, rows.map((e) => manLabel(e.name, e.section)).join("\n") + "\n");
      return;
    }
    if (format === "md") {
      downloadText(`${name}.md`, manDiffMarkdown(this.manResult, rows), "text/markdown");
      return;
    }
    if (format === "tsv") {
      downloadText(`${name}.tsv`, manDiffTsv(rows), "text/tab-separated-values");
      return;
    }
    const data = {
      base: this.versionA,
      target: this.versionB,
      repo: this.repo(),
      generated: new Date().toISOString(),
      filters: {
        statuses: statuses.length ? statuses : MAN_STATUSES.slice(),
        section: this.manSection,
        query: this.manQuery.trim(),
      },
      summary: this.manResult?.summary || {},
      count: rows.length,
      entries: rows,
    };
    downloadText(`${name}.json`, JSON.stringify(data, null, 2) + "\n", "application/json");
  }

  // ---------------------------------------------------------------------------
  // Kconfig mode
  // ---------------------------------------------------------------------------

  /** Every option and Kconfig file of the Kconfig diff, each with its chip status (added/modified/deleted). */
  kconfigEntries() {
    const res = this.kconfigResult || {};
    const symbols = (res.symbols || (
      [...(res.added || []).map((n) => ({ name: n, change: "added" })),
       ...(res.removed || []).map((n) => ({ name: n, change: "removed" })),
       ...(res.changed || []).map((n) => ({ name: n, change: "changed" }))]
    )).map((sym) => ({ ...sym, status: KCONFIG_STATUS_OF[sym.change] || "modified" }));
    const files = (res.files || []).map((f) => ({ ...f, status: KCONFIG_STATUS_OF[f.change] || "modified" }));
    return { symbols, files };
  }

  /** Options and Kconfig files matching the chips (none selected = all) and the text filter. */
  filteredKconfigEntries() {
    const { symbols, files } = this.kconfigEntries();
    const q = this.kconfigFilter.trim().toLowerCase();
    const chips = this.kconfigStatusFilter;
    const byChip = (e) => chips.size === 0 || chips.has(e.status);
    return {
      symbols: symbols.filter((sym) => byChip(sym)
        && (!q || sym.name.toLowerCase().includes(q) || `config_${sym.name.toLowerCase()}`.includes(q))),
      files: files.filter((f) => byChip(f) && (!q || f.file.toLowerCase().includes(q))),
    };
  }

  renderKconfigDiff(resContainer) {
    const res = this.kconfigResult || {};
    const s = res.summary || {};
    const { symbols, files } = this.kconfigEntries();
    const unindexed = res.unindexed_files || [];

    const countEl = this.containerEl.querySelector("#cmp-count-kconfig");
    if (countEl) countEl.textContent = symbols.length + files.length;

    const fileCount = { added: s.files_added ?? 0, modified: s.files_modified ?? 0, deleted: s.files_removed ?? 0 };
    const optionCount = { added: s.added ?? 0, modified: s.changed ?? 0, deleted: s.removed ?? 0 };
    const chip = (key) => `
      <button class="compare-chip ${key} ${this.kconfigStatusFilter.has(key) ? "active" : ""}" data-kc-status="${key}" type="button"
              title="${optionCount[key]} option${optionCount[key] === 1 ? "" : "s"} and ${fileCount[key]} Kconfig file${fileCount[key] === 1 ? "" : "s"} ${key}">
        <span class="compare-chip-count">${optionCount[key]}</span> ${key}
        <span class="compare-chip-sub">· ${fileCount[key]} file${fileCount[key] === 1 ? "" : "s"}</span>
      </button>`;

    resContainer.innerHTML = `
      <div class="compare-toolbar">
        <div class="compare-chips">
          ${KCONFIG_STATUSES.map(chip).join("")}
        </div>
        <div class="compare-toolbar-right">
          <input type="text" class="compare-filter" id="cmp-kc-filter" placeholder="Filter options or files…" value="${escapeHtml(this.kconfigFilter)}" spellcheck="false" />
          ${this.downloadMenuHtml(KCONFIG_DOWNLOAD_FORMATS, "Download every option and Kconfig file matching the current filters")}
        </div>
      </div>
      ${unindexed.length > 0 ? `
        <div class="compare-warning" title="${escapeHtml(unindexed.slice(0, 40).join("\n"))}${unindexed.length > 40 ? "\n…" : ""}">
          ${unindexed.length} Kconfig file${unindexed.length === 1 ? "" : "s"} at these revisions ${unindexed.length === 1 ? "is" : "are"} not indexed in the database, so option-level changes inside ${unindexed.length === 1 ? "it" : "them"} are not listed. File-level changes below are complete.
        </div>` : ""}
      <div class="compare-columns">
        <div class="m-card compare-card">
          <div class="m-card-title">Kconfig Options (<span id="cmp-kc-sym-count">${symbols.length}</span>)</div>
          <div class="compare-list" id="cmp-kc-symbols"></div>
        </div>
        <div class="m-card compare-card">
          <div class="m-card-title">Kconfig Files (<span id="cmp-kc-file-count">${files.length}</span>)</div>
          <div class="compare-list" id="cmp-kc-files"></div>
        </div>
      </div>
    `;

    const renderLists = () => {
      const { symbols: symRows, files: fileRows } = this.filteredKconfigEntries();
      resContainer.querySelector("#cmp-kc-sym-count").textContent = symRows.length;
      resContainer.querySelector("#cmp-kc-file-count").textContent = fileRows.length;
      const total = symRows.length + fileRows.length;
      resContainer.querySelector("#cmp-download-count").textContent = `(${total.toLocaleString()})`;
      resContainer.querySelector("#cmp-download").disabled = total === 0;
      this.renderKconfigSymbols(resContainer.querySelector("#cmp-kc-symbols"), symRows);
      this.renderKconfigFiles(resContainer.querySelector("#cmp-kc-files"), fileRows);
    };

    resContainer.querySelectorAll(".compare-chip[data-kc-status]").forEach((btn) => {
      btn.onclick = () => {
        const key = btn.dataset.kcStatus;
        if (this.kconfigStatusFilter.has(key)) this.kconfigStatusFilter.delete(key);
        else this.kconfigStatusFilter.add(key);
        btn.classList.toggle("active", this.kconfigStatusFilter.has(key));
        renderLists();
        this.persist();
      };
    });

    const filterInput = resContainer.querySelector("#cmp-kc-filter");
    filterInput.oninput = () => {
      this.kconfigFilter = filterInput.value;
      renderLists();
      this.persist();
    };

    this.wireDownloadMenu(resContainer, (format) => this.downloadKconfigRows(format));
    renderLists();
  }

  /** Download every option and Kconfig file matching the chips and the filter (not only the 2000 rendered). */
  downloadKconfigRows(format) {
    const { symbols, files } = this.filteredKconfigEntries();
    if (symbols.length + files.length === 0) return;
    const statuses = KCONFIG_STATUSES.filter((k) => this.kconfigStatusFilter.has(k));
    const filter = this.kconfigFilter.trim();
    const name = this.downloadName("compare-kconfig", statuses, filter);
    // Files defining an option, on either side
    const defFiles = (sym) => [...new Set([...(sym.before || []), ...(sym.after || [])].map((d) => d.file).filter(Boolean))];

    if (format === "options") {
      downloadText(`${name}_options.txt`, symbols.map((sym) => `CONFIG_${sym.name}`).join("\n") + "\n");
      return;
    }
    if (format === "files") {
      downloadText(`${name}_files.txt`, files.map((f) => f.file).join("\n") + "\n");
      return;
    }
    if (format === "tsv") {
      const cell = (v) => String(v ?? "").replace(/[\t\r\n]/g, " ");
      const lines = [["kind", "status", "name", "files", "changed_fields"].join("\t")];
      for (const sym of symbols) {
        lines.push(["option", STATUS_LABELS[sym.status], `CONFIG_${sym.name}`, defFiles(sym).join(","),
          (sym.changes || []).map((c) => c.field).join(",")].map(cell).join("\t"));
      }
      for (const f of files) lines.push(["file", STATUS_LABELS[f.status], f.file, "", ""].map(cell).join("\t"));
      downloadText(`${name}.tsv`, lines.join("\n") + "\n", "text/tab-separated-values");
      return;
    }
    const data = {
      base: this.versionA,
      target: this.versionB,
      generated: new Date().toISOString(),
      filters: { statuses: statuses.length ? statuses : KCONFIG_STATUSES.slice(), contains: filter },
      summary: this.kconfigResult?.summary || {},
      count: { options: symbols.length, files: files.length },
      options: symbols.map((sym) => {
        const out = { name: sym.name, status: sym.status, files: defFiles(sym) };
        if (sym.before) out.before = sym.before;
        if (sym.after) out.after = sym.after;
        if (sym.changes) out.changes = sym.changes;
        return out;
      }),
      files: files.map((f) => ({ status: f.status, path: f.file })),
    };
    downloadText(`${name}.json`, JSON.stringify(data, null, 2) + "\n", "application/json");
  }

  renderKconfigSymbols(listEl, symbols) {
    if (symbols.length === 0) {
      listEl.innerHTML = `<div class="compare-empty">No option-level differences found.</div>`;
      return;
    }
    const shown = symbols.slice(0, 2000);
    listEl.innerHTML = shown.map((sym, idx) => `
      <div class="compare-row" data-idx="${idx}">
        <div class="compare-row-main" title="Click to show definitions">
          <span class="compare-status ${sym.change}">${STATUS_LABELS[sym.change] || "?"}</span>
          <span class="compare-row-path">CONFIG_${escapeHtml(sym.name)}</span>
          <span class="compare-row-actions">
            <button class="code-btn" data-open-kconfig type="button" title="Inspect in KConfig at ${escapeHtml(sym.change === "removed" ? this.versionA : this.versionB)}">Inspect</button>
          </span>
        </div>
      </div>`).join("") + (symbols.length > shown.length ? `<div class="compare-empty">${symbols.length - shown.length} more — refine the filter.</div>` : "");

    listEl.querySelectorAll(".compare-row").forEach((rowEl) => {
      const sym = shown[parseInt(rowEl.dataset.idx, 10)];
      rowEl.querySelector(".compare-row-main").onclick = () => this.toggleSymbolDetail(rowEl, sym);
      rowEl.querySelector("[data-open-kconfig]").onclick = (e) => {
        e.stopPropagation();
        state.openTab({
          type: "kconfig",
          title: `KConfig (${state.kconfigArch()})`,
          arch: state.kconfigArch(),
          version: sym.change === "removed" ? this.versionA : this.versionB,
          symbol: sym.name
        });
      };
    });
  }

  toggleSymbolDetail(rowEl, sym) {
    const existing = rowEl.querySelector(".compare-sym-detail");
    if (existing) {
      existing.remove();
      rowEl.classList.remove("expanded");
      return;
    }
    rowEl.classList.add("expanded");

    const defsHtml = (defs) => (defs && defs.length > 0)
      ? defs.map((d) => `
          <div class="compare-sym-def">
            <span class="compare-sym-file" data-path="${escapeHtml(d.file)}">${escapeHtml(d.file)}</span>
            <span class="compare-sym-type">${KCONFIG_TYPES[d.type] || "—"}</span>
            ${d.prompt ? `<span class="compare-sym-prompt">"${escapeHtml(d.prompt)}"</span>` : ""}
            ${d.def_val ? `<span class="compare-sym-default">default ${escapeHtml(d.def_val)}</span>` : ""}
          </div>`).join("")
      : `<div class="compare-sym-none">not defined</div>`;

    // Property-level changes (one definition on each side): what actually differs, e.g. a new `depends on`
    const show = (v) => (typeof v === "string" ? v : JSON.stringify(v));
    const changesHtml = (sym.changes || []).map((c) => {
      if (Array.isArray(c.added) || Array.isArray(c.removed)) {
        const items = [...(c.removed || []).map((x) => `<code class="diff-del">− ${escapeHtml(show(x))}</code>`),
          ...(c.added || []).map((x) => `<code class="diff-add">+ ${escapeHtml(show(x))}</code>`)];
        return `<div class="compare-sym-change"><span class="compare-sym-field">${escapeHtml(c.field)}</span> ${items.join(" ") || "reordered"}</div>`;
      }
      if (c.field === "help") return `<div class="compare-sym-change"><span class="compare-sym-field">help</span> text changed</div>`;
      return `<div class="compare-sym-change"><span class="compare-sym-field">${escapeHtml(c.field)}</span> `
        + `<code class="diff-del">− ${escapeHtml(show(c.before ?? ""))}</code> <code class="diff-add">+ ${escapeHtml(show(c.after ?? ""))}</code></div>`;
    }).join("");

    const box = document.createElement("div");
    box.className = "compare-sym-detail";
    box.innerHTML = (sym.before || sym.after)
      ? `
        <div class="compare-sym-side"><div class="compare-sym-side-title">${escapeHtml(shortRef(this.versionA))}</div>${defsHtml(sym.before)}</div>
        <div class="compare-sym-side"><div class="compare-sym-side-title">${escapeHtml(shortRef(this.versionB))}</div>${defsHtml(sym.after)}</div>
        ${changesHtml ? `<div class="compare-sym-changes">${changesHtml}</div>` : ""}`
      : `<div class="compare-sym-none">No definition details available.</div>`;
    rowEl.appendChild(box);

    box.querySelectorAll(".compare-sym-file").forEach((el) => {
      el.onclick = () => {
        const inBase = el.closest(".compare-sym-side") === box.firstElementChild;
        this.openFile(inBase ? this.versionA : this.versionB, el.dataset.path);
      };
    });
  }

  renderKconfigFiles(listEl, files) {
    if (files.length === 0) {
      listEl.innerHTML = `<div class="compare-empty">No Kconfig files changed.</div>`;
      return;
    }
    const changes = files.map((f) => ({ type: f.change === "removed" ? "deleted" : f.change, file_path: f.file }));
    listEl.innerHTML = changes.map((ch, idx) => this.fileRowHtml(ch, idx)).join("");
    listEl.querySelectorAll(".compare-row").forEach((rowEl) => {
      const ch = changes[parseInt(rowEl.dataset.idx, 10)];
      rowEl.querySelector(".compare-row-main").onclick = () => this.toggleFileDiff(rowEl, ch.file_path);
      rowEl.querySelectorAll("[data-open]").forEach((btn) => {
        btn.onclick = (e) => {
          e.stopPropagation();
          this.openFile(btn.dataset.open === "a" ? this.versionA : this.versionB, ch.file_path);
        };
      });
    });
  }
}
