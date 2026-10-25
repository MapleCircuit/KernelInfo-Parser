/**
 * diff_view.js - Compare view: file and Kconfig deltas between any two tags or commits.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { refPicker, shortRef } from "../../components/ref_picker.js";

const PAGE_SIZE = 500;
const KCONFIG_TYPES = { 1: "bool", 2: "tristate", 3: "string", 4: "hex", 5: "int" };
const STATUS_LABELS = { added: "A", modified: "M", deleted: "D", renamed: "R", removed: "D", changed: "M" };

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
  }

  async render(containerEl, tabData) {
    this.tab = tabData;
    this.containerEl = containerEl;
    this.versionB = tabData.versionB || state.currentVersion;
    this.versionA = tabData.versionA || await this.defaultBaseFor(this.versionB);
    this.currentMode = tabData.mode || "files";

    containerEl.innerHTML = `
      <div class="tools-view-container compare-view">
        <div class="compare-header">
          <div>
            <div class="compare-title">Compare</div>
            <div class="compare-subtitle">Files and Kconfig changes between any two release tags or commits.</div>
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

        <div class="compare-modes">
          <button class="code-btn" data-mode="files" type="button">Files <span class="compare-mode-count" id="cmp-count-files"></span></button>
          <button class="code-btn" data-mode="kconfig" type="button">Kconfig <span class="compare-mode-count" id="cmp-count-kconfig"></span></button>
        </div>

        <div id="diff-results-container"></div>
      </div>
    `;

    this.persist();
    this.updateRefButtons();
    this.updateModeButtons();

    const pick = (which) => (e) => {
      e.stopPropagation();
      const btn = e.currentTarget;
      refPicker.open(btn, {
        current: which === "a" ? this.versionA : this.versionB,
        onSelect: (ref) => {
          if (which === "a") this.versionA = ref;
          else this.versionB = ref;
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
      this.persist();
      this.updateRefButtons();
      this.loadDiff();
    };
    containerEl.querySelectorAll(".compare-modes .code-btn").forEach((btn) => {
      btn.onclick = () => {
        if (this.currentMode === btn.dataset.mode) return;
        this.currentMode = btn.dataset.mode;
        this.persist();
        this.updateModeButtons();
        this.loadDiff();
      };
    });

    await this.loadDiff();
  }

  /** Default base: the release tag preceding target (or the latest tag when target is a commit). */
  async defaultBaseFor(target) {
    try {
      const res = await api.getVersions(true);
      // Compare a release with the previous release, a release candidate with the previous tag
      const all = (res.versions || []).filter((v) => typeof v === "string" || v.ref_type !== 3);
      const isPre = (name) => /-(rc|pre|alpha|beta)\d*$/.test(name);
      const tags = all.map((v) => (typeof v === "string" ? v : v.vname)).filter((n) => n && (isPre(target) || !isPre(n) || n === target));
      const idx = tags.indexOf(target);
      if (idx > 0) return tags[idx - 1];
      if (idx === -1 && tags.length > 0) return tags[tags.length - 1];
      return tags.find((t) => t !== target) || "v3.0";
    } catch {
      return "v3.0";
    }
  }

  persist() {
    if (!this.tab) return;
    this.tab.versionA = this.versionA;
    this.tab.versionB = this.versionB;
    this.tab.mode = this.currentMode;
    this.tab.title = `Compare ${shortRef(this.versionA)}…${shortRef(this.versionB)}`;
    const titleEl = document.querySelector(`.tab[data-tab-id="${this.tab.id}"] .tab-title`);
    if (titleEl) titleEl.textContent = this.tab.title;
    state.saveState();
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
      if (this.currentMode === "kconfig") {
        const res = await api.getKconfigDiff(this.versionA, this.versionB);
        if (seq !== this.loadSeq) return;
        this.kconfigResult = res;
        this.renderKconfigDiff(resContainer);
      } else {
        const res = await api.getVersionsDiff(this.versionA, this.versionB);
        if (seq !== this.loadSeq) return;
        this.fileChanges = res.changes || [];
        this.fileSummary = res.summary || {};
        this.visibleCount = PAGE_SIZE;
        this.renderFileDiff(resContainer);
      }
    } catch (e) {
      if (seq !== this.loadSeq) return;
      resContainer.innerHTML = `<div class="compare-empty error">Failed: ${escapeHtml(e.message)}</div>`;
    }
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
          <span class="compare-chip-static">${s.unchanged_count ?? 0} unchanged</span>
        </div>
        <input type="text" class="compare-filter" id="cmp-file-filter" placeholder="Filter by path…" value="${escapeHtml(this.fileFilter)}" spellcheck="false" />
      </div>
      <div class="compare-list" id="cmp-file-list"></div>
    `;

    resContainer.querySelectorAll(".compare-chip[data-status]").forEach((btn) => {
      btn.onclick = () => {
        const key = btn.dataset.status;
        if (this.statusFilter.has(key)) this.statusFilter.delete(key);
        else this.statusFilter.add(key);
        btn.classList.toggle("active", this.statusFilter.has(key));
        this.visibleCount = PAGE_SIZE;
        this.renderFileRows();
      };
    });

    const filterInput = resContainer.querySelector("#cmp-file-filter");
    filterInput.oninput = () => {
      this.fileFilter = filterInput.value;
      this.visibleCount = PAGE_SIZE;
      this.renderFileRows();
    };

    this.renderFileRows();
  }

  filteredFileChanges() {
    const q = this.fileFilter.trim().toLowerCase();
    return this.fileChanges.filter((ch) => {
      if (this.statusFilter.size > 0 && !this.statusFilter.has(ch.type)) return false;
      if (q && !(ch.file_path || "").toLowerCase().includes(q) && !(ch.old_file || "").toLowerCase().includes(q)) return false;
      return true;
    });
  }

  renderFileRows() {
    const listEl = this.containerEl.querySelector("#cmp-file-list");
    if (!listEl) return;
    const rows = this.filteredFileChanges();

    if (rows.length === 0) {
      listEl.innerHTML = `<div class="compare-empty">${this.fileChanges.length === 0 ? "No file differences between these revisions." : "No files match the current filter."}</div>`;
      return;
    }

    const shown = rows.slice(0, this.visibleCount);
    listEl.innerHTML = shown.map((ch, idx) => this.fileRowHtml(ch, idx)).join("") + (
      rows.length > shown.length
        ? `<button class="compare-more" id="cmp-more" type="button">Show ${Math.min(PAGE_SIZE, rows.length - shown.length)} more (${rows.length - shown.length} remaining)</button>`
        : ""
    );

    listEl.querySelectorAll(".compare-row").forEach((rowEl) => {
      const ch = shown[parseInt(rowEl.dataset.idx, 10)];
      rowEl.querySelector(".compare-row-main").onclick = () => this.toggleFileDiff(rowEl, ch.file_path, ch.old_file);
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
    const symDelta = (ch.symbols_added || ch.symbols_removed)
      ? `<span class="compare-row-syms" title="Symbol definitions added / removed">+${ch.symbols_added || 0} / −${ch.symbols_removed || 0} sym</span>`
      : "";
    const canOpenBase = type !== "added";
    const canOpenTarget = type !== "deleted";
    return `
      <div class="compare-row" data-idx="${idx}">
        <div class="compare-row-main" title="Click to show the diff · middle-click to open">
          <span class="compare-status ${type}">${STATUS_LABELS[type] || "?"}</span>
          <span class="compare-row-path">${renameFrom}${path}</span>
          ${symDelta}
          <span class="compare-row-actions">
            ${canOpenBase ? `<button class="code-btn" data-open="a" type="button" title="Open at ${escapeHtml(this.versionA)}">${escapeHtml(shortRef(this.versionA))}</button>` : ""}
            ${canOpenTarget ? `<button class="code-btn" data-open="b" type="button" title="Open at ${escapeHtml(this.versionB)}">${escapeHtml(shortRef(this.versionB))}</button>` : ""}
          </span>
        </div>
      </div>`;
  }

  async toggleFileDiff(rowEl, path, oldPath = "") {
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
      const res = await api.getFileDiff(this.versionA, this.versionB, path, oldPath || "");
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
      forceNew
    });
  }

  // ---------------------------------------------------------------------------
  // Kconfig mode
  // ---------------------------------------------------------------------------

  renderKconfigDiff(resContainer) {
    const res = this.kconfigResult || {};
    const s = res.summary || {};
    const files = res.files || [];
    const symbols = res.symbols || (
      [...(res.added || []).map((n) => ({ name: n, change: "added" })),
       ...(res.removed || []).map((n) => ({ name: n, change: "removed" })),
       ...(res.changed || []).map((n) => ({ name: n, change: "changed" }))]
    );
    const unindexed = res.unindexed_files || [];

    const countEl = this.containerEl.querySelector("#cmp-count-kconfig");
    if (countEl) countEl.textContent = symbols.length + files.length;

    resContainer.innerHTML = `
      <div class="compare-toolbar">
        <div class="compare-chips">
          <span class="compare-chip-static"><strong class="kc-added">${s.added ?? 0}</strong> options added</span>
          <span class="compare-chip-static"><strong class="kc-removed">${s.removed ?? 0}</strong> removed</span>
          <span class="compare-chip-static"><strong class="kc-changed">${s.changed ?? 0}</strong> changed</span>
          <span class="compare-chip-sep"></span>
          <span class="compare-chip-static">Kconfig files: ${s.files_added ?? 0} added · ${s.files_removed ?? 0} removed · ${s.files_modified ?? 0} modified</span>
        </div>
        <input type="text" class="compare-filter" id="cmp-kc-filter" placeholder="Filter options or files…" spellcheck="false" />
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

    const filterInput = resContainer.querySelector("#cmp-kc-filter");
    const renderLists = () => {
      const q = filterInput.value.trim().toLowerCase();
      const symRows = symbols.filter((sym) => !q || sym.name.toLowerCase().includes(q) || `config_${sym.name.toLowerCase()}`.includes(q));
      const fileRows = files.filter((f) => !q || f.file.toLowerCase().includes(q));
      resContainer.querySelector("#cmp-kc-sym-count").textContent = symRows.length;
      resContainer.querySelector("#cmp-kc-file-count").textContent = fileRows.length;
      this.renderKconfigSymbols(resContainer.querySelector("#cmp-kc-symbols"), symRows);
      this.renderKconfigFiles(resContainer.querySelector("#cmp-kc-files"), fileRows);
    };
    filterInput.oninput = renderLists;
    renderLists();
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

    const box = document.createElement("div");
    box.className = "compare-sym-detail";
    box.innerHTML = (sym.before || sym.after)
      ? `
        <div class="compare-sym-side"><div class="compare-sym-side-title">${escapeHtml(shortRef(this.versionA))}</div>${defsHtml(sym.before)}</div>
        <div class="compare-sym-side"><div class="compare-sym-side-title">${escapeHtml(shortRef(this.versionB))}</div>${defsHtml(sym.after)}</div>`
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
