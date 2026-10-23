/**
 * ref_picker.js - Git reference picker popover (release tags + searchable commits).
 * Shared by the header version selector and the Compare view.
 */
import { api } from "../api.js";
import { debounce } from "../utils/debounce.js";

const FULL_SHA_RE = /^[0-9a-f]{40}$/i;
const SHA_PREFIX_RE = /^[0-9a-f]{7,40}$/i;

/** Human-readable label for a version or commit ref (commit SHAs are shortened). */
export function shortRef(ref) {
  if (!ref) return "";
  return FULL_SHA_RE.test(ref) ? ref.slice(0, 12) : ref;
}

export function isCommitRef(ref) {
  return Boolean(ref) && SHA_PREFIX_RE.test(ref);
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatDate(ts) {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toISOString().slice(0, 10);
  } catch {
    return "";
  }
}

class RefPicker {
  constructor() {
    this.popover = null;
    this.items = [];
    this.highlight = -1;
    this.onSelect = null;
    this.searchSeq = 0;
    this._outsideHandler = null;
  }

  /**
   * Open the picker under anchorEl.
   * @param {HTMLElement} anchorEl
   * @param {{ current?: string, onSelect: (ref: string) => void }} options
   */
  async open(anchorEl, { current = "", onSelect }) {
    if (this.popover && this.anchorEl === anchorEl) {
      this.close();
      return;
    }
    this.close();
    this.anchorEl = anchorEl;
    this.onSelect = onSelect;
    this.current = current;

    const rect = anchorEl.getBoundingClientRect();
    const pop = document.createElement("div");
    pop.className = "ref-picker";
    pop.style.top = `${rect.bottom + 6}px`;
    pop.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 448))}px`;
    pop.innerHTML = `
      <input type="text" class="ref-picker-input" placeholder="Search tag, commit hash, subject or author…" spellcheck="false" autocomplete="off" />
      <div class="ref-picker-list"><div class="ref-picker-empty">Loading…</div></div>
      <div class="ref-picker-footer">↑↓ navigate · Enter select · paste any SHA or ref name</div>
    `;
    document.body.appendChild(pop);
    this.popover = pop;

    const input = pop.querySelector(".ref-picker-input");
    input.focus();

    const runSearch = debounce(() => this.search(input.value.trim()), 200);
    input.addEventListener("input", runSearch);
    input.addEventListener("keydown", (e) => this.handleKey(e, input));

    this._outsideHandler = (e) => {
      if (this.popover && !this.popover.contains(e.target) && !anchorEl.contains(e.target)) {
        this.close();
      }
    };
    setTimeout(() => document.addEventListener("mousedown", this._outsideHandler), 0);

    await this.search("");
  }

  close() {
    if (this._outsideHandler) {
      document.removeEventListener("mousedown", this._outsideHandler);
      this._outsideHandler = null;
    }
    this.popover?.remove();
    this.popover = null;
    this.anchorEl = null;
    this.items = [];
    this.highlight = -1;
  }

  async search(query) {
    const seq = ++this.searchSeq;
    const q = query.toLowerCase();

    const [versionsRes, commitsRes] = await Promise.allSettled([
      api.getVersions(true),
      api.getCommits(this.current || "v3.0", 1, 50, query)
    ]);
    if (seq !== this.searchSeq || !this.popover) return;

    const tags = (versionsRes.status === "fulfilled" ? versionsRes.value.versions || [] : [])
      .map((v) => (typeof v === "string" ? { vname: v } : v))
      .filter((v) => v.vname && (!q || v.vname.toLowerCase().includes(q)));
    const commits = commitsRes.status === "fulfilled" ? commitsRes.value.commits || [] : [];

    this.items = [];
    tags.forEach((t) => this.items.push({ kind: "tag", ref: t.vname, label: t.vname, meta: t.is_prerelease ? "pre-release" : "release" }));
    commits.forEach((c) =>
      this.items.push({
        kind: "commit",
        ref: c.commit_hash,
        label: c.subject || "(no subject)",
        sha: c.commit_hash,
        meta: `${c.author_name || ""} · ${formatDate(c.author_date)}`
      })
    );

    // A pasted SHA or ref name that the lists don't contain is still selectable (resolved server-side)
    if (query && !this.items.some((it) => it.ref === query)) {
      this.items.push({ kind: "raw", ref: query, label: `Use “${query}”`, meta: isCommitRef(query) ? "commit SHA" : "git revision" });
    }

    this.highlight = this.items.findIndex((it) => it.ref === this.current);
    if (this.highlight < 0 && query) this.highlight = 0;
    this.renderList();
  }

  renderList() {
    const list = this.popover?.querySelector(".ref-picker-list");
    if (!list) return;
    if (this.items.length === 0) {
      list.innerHTML = `<div class="ref-picker-empty">No matching tags or commits.</div>`;
      return;
    }

    let lastKind = null;
    const sectionTitle = { tag: "Release Tags", commit: "Commits", raw: "Revision" };
    list.innerHTML = this.items
      .map((it, idx) => {
        const header = it.kind !== lastKind ? `<div class="ref-picker-section">${sectionTitle[it.kind]}</div>` : "";
        lastKind = it.kind;
        const classes = ["ref-picker-item", idx === this.highlight ? "highlight" : "", it.ref === this.current ? "current" : ""].join(" ");
        const sha = it.sha ? `<code class="ref-picker-sha">${escapeHtml(it.sha.slice(0, 12))}</code>` : "";
        return `${header}
          <div class="${classes}" data-idx="${idx}" title="${escapeHtml(it.ref)}">
            ${sha}
            <span class="ref-picker-label">${escapeHtml(it.label)}</span>
            <span class="ref-picker-meta">${escapeHtml(it.meta || "")}</span>
          </div>`;
      })
      .join("");

    list.querySelectorAll(".ref-picker-item").forEach((el) => {
      el.addEventListener("mousedown", (e) => {
        e.preventDefault();
        this.select(parseInt(el.dataset.idx, 10));
      });
    });
    list.querySelector(".ref-picker-item.highlight")?.scrollIntoView({ block: "nearest" });
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
      this.renderList();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (this.highlight >= 0) {
        this.select(this.highlight);
      } else if (input.value.trim()) {
        this.selectRef(input.value.trim());
      }
    }
  }

  select(idx) {
    const item = this.items[idx];
    if (item) this.selectRef(item.ref);
  }

  selectRef(ref) {
    const cb = this.onSelect;
    this.close();
    if (cb) cb(ref);
  }
}

export const refPicker = new RefPicker();
