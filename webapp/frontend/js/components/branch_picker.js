/**
 * branch_picker.js - Hierarchical branch & tag-view picker popover for the header.
 *
 * Groups Git branches by source (local branches at top, then remote/prefixed sources such as
 * history, linux-next, origin, stable...) so large repositories with 100+ stable branches stay
 * scannable. Selecting a source drills into the branches within it; typing in the filter box
 * searches across all branches and sources immediately.
 */
import { refPicker, shortRef } from "./ref_picker.js";
import { escapeHtml } from "./modal.js";
import {
  SOURCE_SCOPE_PREFIX,
  formatBranchScopeLabel,
  isSourceScope,
  sourceNameOfScope,
  sourceScopeValue,
} from "../state.js";

export {
  SOURCE_SCOPE_PREFIX,
  formatBranchScopeLabel,
  isSourceScope,
  sourceNameOfScope,
  sourceScopeValue,
};

const LOCAL_SOURCE = "local";
const LOCAL_DIRECT_LIMIT = 4;

/** Split a branch name into `{ source, shortName }`. Branches without `/` belong to `local`. */
export function branchSourceOf(name) {
  const s = String(name || "");
  const slash = s.indexOf("/");
  if (slash <= 0) return { source: LOCAL_SOURCE, shortName: s };
  return { source: s.slice(0, slash), shortName: s.slice(slash + 1) };
}

function tokenizeNatural(str) {
  const parts = [];
  String(str || "").replace(/(\d+)|(\D+)/g, (_, num, text) => {
    parts.push(num !== undefined ? [0, parseInt(num, 10)] : [1, text.toLowerCase()]);
  });
  return parts;
}

function compareNaturalDesc(a, b) {
  const ax = tokenizeNatural(a);
  const bx = tokenizeNatural(b);
  const len = Math.max(ax.length, bx.length);
  for (let i = 0; i < len; i++) {
    const ai = ax[i];
    const bi = bx[i];
    if (!ai) return 1;
    if (!bi) return -1;
    if (ai[0] !== bi[0]) return ai[0] - bi[0];
    if (ai[1] !== bi[1]) return ai[1] > bi[1] ? -1 : 1;
  }
  return 0;
}

function isMainlineShortName(shortName) {
  return shortName === "master" || shortName === "main";
}

/** Sort branches inside a source: default first, master/main next, newest versioned branches first, else alphabetical. */
export function compareBranchesInSource(a, b) {
  if (Boolean(a.is_default) !== Boolean(b.is_default)) return a.is_default ? -1 : 1;
  const sa = branchSourceOf(a.name).shortName;
  const sb = branchSourceOf(b.name).shortName;
  if (isMainlineShortName(sa) !== isMainlineShortName(sb)) return isMainlineShortName(sa) ? -1 : 1;
  if (/\d/.test(sa) && /\d/.test(sb)) {
    const cmp = compareNaturalDesc(sa, sb);
    if (cmp !== 0) return cmp;
  }
  const la = sa.toLowerCase();
  const lb = sb.toLowerCase();
  if (la !== lb) return la < lb ? -1 : 1;
  return a.name < b.name ? -1 : (a.name > b.name ? 1 : 0);
}

/**
 * Group branch entries into `{ local, sources }`.
 * `local` contains branches without a `/` prefix; `sources` is sorted with any source holding the
 * default branch first, then alphabetically by source name.
 */
export function groupBranchesBySource(branches) {
  const local = [];
  const bySource = new Map();

  for (const b of branches || []) {
    if (!b || !b.name) continue;
    const { source } = branchSourceOf(b.name);
    if (source === LOCAL_SOURCE) {
      local.push(b);
    } else {
      if (!bySource.has(source)) bySource.set(source, []);
      bySource.get(source).push(b);
    }
  }

  local.sort(compareBranchesInSource);
  const sources = [];
  for (const [source, list] of bySource.entries()) {
    list.sort(compareBranchesInSource);
    const hasDefault = list.some((b) => Boolean(b.is_default));
    sources.push({ source, branches: list, hasDefault });
  }
  sources.sort((a, b) => {
    if (a.hasDefault !== b.hasDefault) return a.hasDefault ? -1 : 1;
    const la = a.source.toLowerCase();
    const lb = b.source.toLowerCase();
    if (la !== lb) return la < lb ? -1 : 1;
    return a.source < b.source ? -1 : (a.source > b.source ? 1 : 0);
  });

  return { local, sources };
}

function branchMeta(b) {
  if (b.is_default) return "default";
  if (Number(b.commit_id) > 0) return "indexed";
  return "git";
}

function isTagViewSelected(current, views, index, view) {
  if (index === 0 && (current === "" || current === "view:0")) return true;
  if (current === `view:${index}`) return true;
  return Boolean(view?.name && current === view.name);
}

export class BranchPicker {
  constructor() {
    this.popover = null;
    this.anchorEl = null;
    this.onSelect = null;
    this.current = "";
    this.branches = [];
    this.tagViews = [];
    this.grouped = { local: [], sources: [] };
    this.activeSource = null;
    this.query = "";
    this.items = [];
    this.highlight = -1;
    this._outsideHandler = null;
    this._outsideTimer = null;
  }

  /**
   * Open the branch picker popover under `anchorEl`.
   * @param {HTMLElement} anchorEl
   * @param {{ current?: string, branches?: Array, tagViews?: Array, onSelect: (value: string) => void }} options
   */
  open(anchorEl, { current = "", branches = [], tagViews = [], onSelect }) {
    if (this.popover && this.anchorEl === anchorEl) {
      this.close();
      return;
    }
    refPicker.close();
    this.close();

    this.anchorEl = anchorEl;
    this.onSelect = onSelect;
    this.current = typeof current === "string" ? current : "";
    this.branches = Array.isArray(branches) ? branches : [];
    this.tagViews = (Array.isArray(tagViews) && tagViews.length)
      ? tagViews
      : [{ name: "— any branch —", regex_rules: [] }];
    this.grouped = groupBranchesBySource(this.branches);
    this.activeSource = null;
    this.query = "";

    const rect = anchorEl.getBoundingClientRect();
    const pop = document.createElement("div");
    pop.className = "ref-picker branch-picker";
    const winW = typeof window !== "undefined" ? window.innerWidth : 1280;
    pop.style.top = `${rect.bottom + 6}px`;
    pop.style.left = `${Math.max(8, Math.min(rect.left, winW - 448))}px`;
    pop.innerHTML = `
      <input type="text" class="ref-picker-input" placeholder="Search branches or sources…" spellcheck="false" autocomplete="off" />
      <div class="branch-picker-breadcrumb" hidden></div>
      <div class="ref-picker-list"></div>
      <div class="ref-picker-footer"></div>
    `;
    document.body.appendChild(pop);
    this.popover = pop;

    // Keep internal mousedowns (including re-renders on source drill-down / back) from reaching document._outsideHandler
    pop.addEventListener("mousedown", (e) => e.stopPropagation());

    const input = pop.querySelector(".ref-picker-input");
    input?.focus?.();
    input?.addEventListener("input", () => {
      this.query = input.value.trim();
      this.rebuildItems(true);
    });
    input?.addEventListener("keydown", (e) => this.handleKey(e, input));

    const handler = (e) => {
      if (this.popover && !this.popover.contains(e.target) && !anchorEl.contains(e.target)) {
        this.close();
      }
    };
    this._outsideHandler = handler;
    this._outsideTimer = setTimeout(() => {
      this._outsideTimer = null;
      if (this._outsideHandler === handler) document.addEventListener("mousedown", handler);
    }, 0);

    this.rebuildItems(false);
  }

  close() {
    if (this._outsideTimer) {
      clearTimeout(this._outsideTimer);
      this._outsideTimer = null;
    }
    if (this._outsideHandler) {
      document.removeEventListener("mousedown", this._outsideHandler);
      this._outsideHandler = null;
    }
    this.popover?.remove();
    this.popover = null;
    this.anchorEl = null;
    this.activeSource = null;
    this.items = [];
    this.highlight = -1;
  }

  enterSource(sourceName) {
    this.activeSource = sourceName;
    this.query = "";
    const input = this.popover?.querySelector(".ref-picker-input");
    if (input) {
      input.value = "";
      input.placeholder = `Filter branches in ${sourceName}…`;
      input.focus?.();
    }
    this.rebuildItems(false);
  }

  leaveSource(focusSource = null) {
    const prevSource = focusSource || this.activeSource;
    this.activeSource = null;
    this.query = "";
    const input = this.popover?.querySelector(".ref-picker-input");
    if (input) {
      input.value = "";
      input.placeholder = "Search branches or sources…";
      input.focus?.();
    }
    this.rebuildItems(false, prevSource);
  }

  _branchesForSource(sourceName) {
    if (sourceName === LOCAL_SOURCE) return this.grouped.local;
    return this.grouped.sources.find((s) => s.source === sourceName)?.branches || [];
  }

  _allSourcesForRoot() {
    const list = [];
    if (this.grouped.local.length > LOCAL_DIRECT_LIMIT) {
      list.push({
        source: LOCAL_SOURCE,
        branches: this.grouped.local,
        hasDefault: this.grouped.local.some((b) => b.is_default),
      });
    }
    list.push(...this.grouped.sources);
    return list;
  }

  _makeSourceScopeItem(sourceName, branchCount, section) {
    const value = sourceScopeValue(sourceName);
    const countLabel = `${branchCount} ${branchCount === 1 ? "branch" : "branches"}`;
    return {
      kind: "source-scope",
      section,
      value,
      sha: "",
      prefix: sourceName === LOCAL_SOURCE ? "" : `${sourceName}/`,
      label: sourceName === LOCAL_SOURCE ? "* (all local)" : "*",
      meta: `all tags · ${countLabel}`,
      isCurrent: this.current === value,
      title: `All tags across ${sourceName} (${countLabel})`,
    };
  }

  rebuildItems(fromSearch, focusSource = null) {
    const q = this.query.toLowerCase();
    this.items = [];

    if (this.activeSource !== null) {
      const sourceBranches = this._branchesForSource(this.activeSource);
      const scopeHint = `${this.activeSource}/* ${this.activeSource} all tags`.toLowerCase();
      if (!q || scopeHint.includes(q)) {
        this.items.push(this._makeSourceScopeItem(this.activeSource, sourceBranches.length, this.activeSource));
      }
      const matching = sourceBranches.filter((b) => {
        if (!q) return true;
        return b.name.toLowerCase().includes(q) || (b.commit_hash || "").toLowerCase().includes(q);
      });
      for (const b of matching) {
        const { shortName } = branchSourceOf(b.name);
        this.items.push({
          kind: "branch",
          section: this.activeSource,
          value: b.name,
          sha: b.commit_hash || "",
          prefix: b.name.includes("/") ? `${this.activeSource}/` : "",
          label: shortName,
          meta: branchMeta(b),
          isCurrent: b.name === this.current,
          title: b.name,
        });
      }
      const curIdx = this.items.findIndex((it) => it.isCurrent);
      this.highlight = curIdx >= 0 ? curIdx : (this.items.length ? 0 : -1);
      this.render(sourceBranches.length);
      return;
    }

    // Root view: without a query, show Tag Views, small Local list, and drillable Branch Sources
    if (!q) {
      this.tagViews.forEach((v, i) => {
        this.items.push({
          kind: "view",
          section: "Tag Views",
          value: i === 0 ? "" : `view:${i}`,
          label: v.name,
          meta: i === 0 ? "all tags" : "regex layout",
          isCurrent: isTagViewSelected(this.current, this.tagViews, i, v),
          title: `Tag layout: ${v.name}`,
        });
      });

      if (this.grouped.local.length > 0 && this.grouped.local.length <= LOCAL_DIRECT_LIMIT) {
        for (const b of this.grouped.local) {
          this.items.push({
            kind: "branch",
            section: "Local",
            value: b.name,
            sha: b.commit_hash || "",
            prefix: "",
            label: b.name,
            meta: branchMeta(b),
            isCurrent: b.name === this.current,
            title: b.name,
          });
        }
      }

      for (const src of this._allSourcesForRoot()) {
        const scopeVal = sourceScopeValue(src.source);
        const scopeActive = this.current === scopeVal;
        const activeInSource = src.branches.find((b) => b.name === this.current);
        const countLabel = `${src.branches.length} ${src.branches.length === 1 ? "branch" : "branches"}`;
        const shortActive = activeInSource ? branchSourceOf(activeInSource.name).shortName : "";
        const activeTag = scopeActive ? "all tags" : shortActive;
        this.items.push({
          kind: "source",
          section: "Branch Sources",
          source: src.source,
          label: src.source,
          meta: activeTag ? `${activeTag} · ${countLabel}` : countLabel,
          isCurrent: scopeActive || Boolean(activeInSource),
          title: `Open ${src.source} (${countLabel})`,
        });
      }
    } else {
      // Search mode across Tag Views, matching Sources, Source Scopes, and all Branches
      this.tagViews.forEach((v, i) => {
        if ((v.name || "").toLowerCase().includes(q)) {
          this.items.push({
            kind: "view",
            section: "Tag Views",
            value: i === 0 ? "" : `view:${i}`,
            label: v.name,
            meta: i === 0 ? "all tags" : "regex layout",
            isCurrent: isTagViewSelected(this.current, this.tagViews, i, v),
            title: `Tag layout: ${v.name}`,
          });
        }
      });

      const allSources = this._allSourcesForRoot();
      for (const src of allSources) {
        if (src.source.toLowerCase().includes(q)) {
          const countLabel = `${src.branches.length} ${src.branches.length === 1 ? "branch" : "branches"}`;
          const scopeActive = this.current === sourceScopeValue(src.source);
          this.items.push({
            kind: "source",
            section: "Sources",
            source: src.source,
            label: src.source,
            meta: scopeActive ? `all tags · ${countLabel}` : countLabel,
            isCurrent: scopeActive || src.branches.some((b) => b.name === this.current),
            title: `Open ${src.source} branches (${countLabel})`,
          });
        }
      }

      for (const src of allSources) {
        if (`${src.source}/*`.toLowerCase().includes(q) || src.source.toLowerCase().includes(q)) {
          this.items.push(this._makeSourceScopeItem(src.source, src.branches.length, "Branches"));
        }
      }

      const allSorted = [
        ...this.grouped.local,
        ...this.grouped.sources.flatMap((s) => s.branches),
      ];
      for (const b of allSorted) {
        if (b.name.toLowerCase().includes(q) || (b.commit_hash || "").toLowerCase().includes(q)) {
          const { source, shortName } = branchSourceOf(b.name);
          this.items.push({
            kind: "branch",
            section: "Branches",
            value: b.name,
            sha: b.commit_hash || "",
            prefix: b.name.includes("/") ? `${source}/` : "",
            label: shortName,
            meta: branchMeta(b),
            isCurrent: b.name === this.current,
            title: b.name,
          });
        }
      }
    }

    if (focusSource) {
      const srcIdx = this.items.findIndex((it) => it.kind === "source" && it.source === focusSource);
      this.highlight = srcIdx >= 0 ? srcIdx : 0;
    } else if (fromSearch) {
      this.highlight = this.items.length ? 0 : -1;
    } else {
      const curIdx = this.items.findIndex((it) => it.isCurrent);
      this.highlight = curIdx >= 0 ? curIdx : (this.items.length ? 0 : -1);
    }
    this.render();
  }

  render(sourceTotal = 0) {
    if (!this.popover) return;
    const crumb = this.popover.querySelector(".branch-picker-breadcrumb");
    const list = this.popover.querySelector(".ref-picker-list");
    const footer = this.popover.querySelector(".ref-picker-footer");

    if (crumb) {
      if (this.activeSource !== null) {
        crumb.hidden = false;
        const branchMatches = this.items.filter((it) => it.kind === "branch").length;
        const countText = this.query
          ? `${branchMatches} / ${sourceTotal}`
          : `${sourceTotal} ${sourceTotal === 1 ? "branch" : "branches"}`;
        crumb.innerHTML = `
          <button type="button" class="branch-picker-back" title="Back to all branch sources (← / Backspace)">← All sources</button>
          <span class="branch-picker-crumb">${escapeHtml(this.activeSource)}</span>
          <span class="branch-picker-count">${escapeHtml(countText)}</span>
        `;
        const backBtn = crumb.querySelector(".branch-picker-back");
        if (backBtn) {
          backBtn.onmousedown = (e) => {
            e.preventDefault();
            e.stopPropagation();
            this.leaveSource();
          };
        }
      } else {
        crumb.hidden = true;
        crumb.innerHTML = "";
      }
    }

    if (footer) {
      footer.textContent = this.activeSource !== null
        ? "↑↓ navigate · Enter select · ←/Backspace back · Esc close"
        : "↑↓ navigate · Enter/→ open or select · Esc close";
    }

    if (!list) return;
    if (this.items.length === 0) {
      list.innerHTML = `<div class="ref-picker-empty">No matching branches or sources.</div>`;
      return;
    }

    let lastSection = null;
    list.innerHTML = this.items
      .map((it, idx) => {
        const header = (this.activeSource === null && it.section !== lastSection)
          ? `<div class="ref-picker-section">${escapeHtml(it.section)}</div>`
          : "";
        lastSection = it.section;
        const classes = [
          "ref-picker-item",
          it.kind === "source" ? "branch-picker-source" : "",
          it.kind === "source-scope" ? "branch-picker-scope" : "",
          idx === this.highlight ? "highlight" : "",
          it.isCurrent ? "current" : "",
        ].filter(Boolean).join(" ");
        const sha = it.sha ? `<code class="ref-picker-sha">${escapeHtml(shortRef(it.sha))}</code>` : "";
        const labelHtml = it.prefix
          ? `<span class="branch-picker-prefix">${escapeHtml(it.prefix)}</span>${escapeHtml(it.label)}`
          : escapeHtml(it.label);
        const chevron = it.kind === "source" ? `<span class="branch-picker-chevron">›</span>` : "";
        return `${header}
          <div class="${classes}" data-idx="${idx}" title="${escapeHtml(it.title || it.label)}">
            ${sha}
            <span class="ref-picker-label">${labelHtml}</span>
            <span class="ref-picker-meta">${escapeHtml(it.meta || "")}</span>
            ${chevron}
          </div>`;
      })
      .join("");

    list.querySelectorAll(".ref-picker-item").forEach((el) => {
      el.addEventListener("mousedown", (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.activate(parseInt(el.dataset.idx, 10));
      });
    });
    list.querySelector(".ref-picker-item.highlight")?.scrollIntoView?.({ block: "nearest" });
  }

  handleKey(e, input) {
    if (e.key === "Escape") {
      e.preventDefault();
      this.close();
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (this.items.length === 0) return;
      const delta = e.key === "ArrowDown" ? 1 : -1;
      this.highlight = (this.highlight + delta + this.items.length) % this.items.length;
      this.render(this.activeSource !== null ? this._branchesForSource(this.activeSource).length : 0);
    } else if (e.key === "ArrowRight") {
      const item = this.items[this.highlight];
      if (item?.kind === "source" && (!input?.value || input.selectionStart === input.value.length)) {
        e.preventDefault();
        this.enterSource(item.source);
      }
    } else if ((e.key === "ArrowLeft" || e.key === "Backspace") && this.activeSource !== null && !input?.value) {
      e.preventDefault();
      this.leaveSource();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (this.highlight >= 0 && this.items[this.highlight]) {
        this.activate(this.highlight);
      }
    }
  }

  activate(idx) {
    const item = this.items[idx];
    if (!item) return;
    if (item.kind === "source") {
      this.enterSource(item.source);
      return;
    }
    const cb = this.onSelect;
    this.close();
    if (cb) cb(item.value);
  }
}

export const branchPicker = new BranchPicker();
