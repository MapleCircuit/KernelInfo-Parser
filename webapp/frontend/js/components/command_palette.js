/**
 * command_palette.js - Global Ctrl+K search & command palette.
 *
 * Prefixes select the mode: (none) files, symbols and Kconfig options together; ">" commands;
 * "@" symbols; "#" commits; "?" Kconfig options; ":" go to line in the active code tab.
 */
import { api } from "../api.js";
import { state } from "../state.js";
import { debounce } from "../utils/debounce.js";
import { shortRef } from "./ref_picker.js";

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Wrap the characters of `text` matched by `query` (substring, else subsequence) in <mark>. */
function highlight(text, query) {
  const t = String(text ?? "");
  const q = (query || "").toLowerCase();
  if (!q) return escapeHtml(t);
  const idx = t.toLowerCase().indexOf(q);
  if (idx !== -1) {
    return `${escapeHtml(t.slice(0, idx))}<mark>${escapeHtml(t.slice(idx, idx + q.length))}</mark>${escapeHtml(t.slice(idx + q.length))}`;
  }
  let out = "";
  let qi = 0;
  for (const ch of t) {
    if (qi < q.length && ch.toLowerCase() === q[qi]) {
      out += `<mark>${escapeHtml(ch)}</mark>`;
      qi++;
    } else {
      out += escapeHtml(ch);
    }
  }
  return out;
}

const MODES = {
  ">": { label: "Commands", placeholder: "Run a command…" },
  "@": { label: "Symbols", placeholder: "Search symbol definitions…" },
  "#": { label: "Commits", placeholder: "Search commits by subject, author or hash…" },
  "?": { label: "Kconfig", placeholder: "Search Kconfig options…" },
  ":": { label: "Go to line", placeholder: "Line number in the active file…" },
};

class CommandPalette {
  constructor() {
    this.el = null;
    this.items = [];
    this.highlightIdx = 0;
    this.seq = 0;
    this.commandProvider = () => [];
    this.debouncedSearch = debounce(() => this.search(), 160);
  }

  /** Register the function returning palette commands: [{ id, title, hint, run }]. */
  setCommandProvider(fn) {
    this.commandProvider = fn;
  }

  init() {
    document.addEventListener("keydown", (e) => {
      const key = e.key.toLowerCase();
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (key === "k" || (key === "p" && !e.shiftKey))) {
        e.preventDefault();
        this.open("");
      } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && key === "p") {
        e.preventDefault();
        this.open(">");
      } else if (key === "escape" && this.el) {
        this.close();
      }
    });
  }

  open(initial = "") {
    if (this.el) {
      const input = this.el.querySelector(".cmdp-input");
      input.value = initial;
      input.focus();
      this.search();
      return;
    }
    const backdrop = document.createElement("div");
    backdrop.className = "cmdp-backdrop";
    backdrop.innerHTML = `
      <div class="cmdp" role="dialog" aria-label="Command palette">
        <div class="cmdp-input-row">
          <span class="cmdp-mode"></span>
          <input type="text" class="cmdp-input" spellcheck="false" autocomplete="off" />
        </div>
        <div class="cmdp-results" role="listbox"></div>
        <div class="cmdp-footer">
          <span><kbd>↑↓</kbd> navigate</span><span><kbd>Enter</kbd> open</span><span><kbd>Ctrl+Enter</kbd> new tab</span>
          <span class="cmdp-prefixes"><code>&gt;</code> commands <code>@</code> symbols <code>#</code> commits <code>?</code> kconfig <code>:</code> line</span>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    this.el = backdrop;

    backdrop.addEventListener("mousedown", (e) => {
      if (e.target === backdrop) this.close();
    });
    const input = backdrop.querySelector(".cmdp-input");
    input.value = initial;
    input.addEventListener("input", () => {
      this.updateMode();
      const { mode } = this.parse();
      if (mode === ">" || mode === ":") this.search();
      else this.debouncedSearch();
    });
    input.addEventListener("keydown", (e) => this.onKey(e));
    input.focus();
    this.updateMode();
    this.search();
  }

  close() {
    this.el?.remove();
    this.el = null;
    this.items = [];
    this.seq++;
  }

  parse() {
    const raw = this.el?.querySelector(".cmdp-input").value || "";
    const mode = MODES[raw[0]] ? raw[0] : "";
    return { mode, query: (mode ? raw.slice(1) : raw).trim() };
  }

  updateMode() {
    const { mode } = this.parse();
    const badge = this.el.querySelector(".cmdp-mode");
    const input = this.el.querySelector(".cmdp-input");
    badge.textContent = mode ? MODES[mode].label : "Search";
    input.placeholder = mode ? MODES[mode].placeholder : `Search files, symbols and Kconfig options in ${shortRef(state.currentVersion)}…`;
  }

  async search() {
    if (!this.el) return;
    const seq = ++this.seq;
    const { mode, query } = this.parse();
    const version = state.getActiveTab()?.version || state.currentVersion;
    let items = [];

    if (mode === ">") {
      const q = query.toLowerCase();
      items = this.commandProvider()
        .filter((c) => !q || c.title.toLowerCase().includes(q) || (c.keywords || "").toLowerCase().includes(q))
        .map((c) => ({ section: "Commands", icon: "›", title: c.title, detail: c.hint || "", run: c.run, highlightQuery: query }));
    } else if (mode === ":") {
      const line = parseInt(query, 10);
      const tab = state.getActiveTab();
      if (tab?.type === "code" && line > 0) {
        items = [{ section: "Go to line", icon: "↳", title: `Line ${line}`, detail: tab.path, run: () => this.openCode(tab.path, tab.version, line, false) }];
      } else {
        items = [{ section: "Go to line", icon: "ℹ", title: tab?.type === "code" ? "Type a line number" : "Open a code tab first", detail: "", run: null }];
      }
    } else if (!query) {
      items = this.commandProvider().slice(0, 8).map((c) => ({ section: "Suggested commands", icon: "›", title: c.title, detail: c.hint || "", run: c.run }));
    } else {
      const wantFiles = mode === "";
      const wantSymbols = mode === "" || mode === "@";
      const wantKconfig = mode === "" || mode === "?";
      const wantCommits = mode === "#";
      const [files, symbols, kconfig, commits] = await Promise.allSettled([
        wantFiles ? api.searchFiles(version, query, 25) : null,
        wantSymbols && query.length >= 2 ? api.searchSymbols(version, query, mode === "@" ? 50 : 15) : null,
        wantKconfig && query.length >= 2 ? api.searchKconfig(version, query.replace(/^CONFIG_/i, ""), mode === "?" ? 40 : 8) : null,
        wantCommits ? api.searchCommits(version, query, 40) : null,
      ]);
      if (seq !== this.seq || !this.el) return;

      const val = (r) => (r.status === "fulfilled" ? r.value : null);
      for (const f of val(files)?.files || []) {
        items.push({ section: "Files", icon: "📄", title: f.name, detail: f.path, highlightQuery: query, run: (nt) => this.openCode(f.path, version, 1, nt) });
      }
      for (const s of val(symbols)?.symbols || []) {
        if (!s.file_path) continue;
        items.push({
          section: "Symbols", icon: "ƒ", title: s.name, detail: `${s.type_name || ""} · ${s.file_path}:${s.line_s || 1}`, highlightQuery: query,
          run: (nt) => this.openCode(s.file_path, version, s.line_s || 1, nt),
        });
      }
      for (const k of val(kconfig)?.symbols || []) {
        items.push({
          section: "Kconfig", icon: "⚙", title: `CONFIG_${k.name}`, detail: k.prompt || k.type_name || "", highlightQuery: query,
          run: () => state.openTab({ type: "kconfig", title: "KConfig (x86_64)", arch: "x86_64", version, symbol: k.name }),
        });
      }
      for (const c of val(commits)?.commits || []) {
        items.push({
          section: "Commits", icon: "●", title: c.subject || c.commit_hash, detail: `${c.commit_hash.slice(0, 12)} · ${c.author_name || c.author_email || ""}`, highlightQuery: query,
          run: () => state.setVersion(c.commit_hash),
        });
      }
      if (items.length === 0) {
        items.push({ section: "No results", icon: "ℹ", title: `Nothing matches “${query}” in ${shortRef(version)}`, detail: "", run: null });
      }
    }

    this.items = items;
    this.highlightIdx = items.findIndex((it) => it.run);
    this.render();
  }

  render() {
    const box = this.el?.querySelector(".cmdp-results");
    if (!box) return;
    let lastSection = null;
    box.innerHTML = this.items.map((it, idx) => {
      const header = it.section !== lastSection ? `<div class="cmdp-section">${escapeHtml(it.section)}</div>` : "";
      lastSection = it.section;
      return `${header}<div class="cmdp-item ${idx === this.highlightIdx ? "active" : ""} ${it.run ? "" : "disabled"}" data-idx="${idx}" role="option">
        <span class="cmdp-icon">${escapeHtml(it.icon)}</span>
        <span class="cmdp-title">${highlight(it.title, it.highlightQuery)}</span>
        <span class="cmdp-detail">${escapeHtml(it.detail)}</span>
      </div>`;
    }).join("");
    box.querySelectorAll(".cmdp-item").forEach((row) => {
      row.addEventListener("mousedown", (e) => {
        e.preventDefault();
        this.execute(parseInt(row.dataset.idx, 10), e.ctrlKey || e.metaKey || e.button === 1);
      });
    });
    box.querySelector(".cmdp-item.active")?.scrollIntoView({ block: "nearest" });
  }

  onKey(e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const dir = e.key === "ArrowDown" ? 1 : -1;
      if (!this.items.some((it) => it.run)) return;
      let idx = this.highlightIdx;
      do {
        idx = (idx + dir + this.items.length) % this.items.length;
      } while (!this.items[idx].run);
      this.highlightIdx = idx;
      this.render();
    } else if (e.key === "Enter") {
      e.preventDefault();
      this.execute(this.highlightIdx, e.ctrlKey || e.metaKey);
    } else if (e.key === "Escape") {
      e.preventDefault();
      this.close();
    }
  }

  execute(idx, newTab) {
    const item = this.items[idx];
    if (!item || !item.run) return;
    this.close();
    item.run(newTab);
  }

  openCode(path, version, line, forceNew) {
    state.openTab({ type: "code", title: path.split("/").pop(), path, version, cursorLine: line > 1 ? line : undefined, forceNew: Boolean(forceNew) });
  }
}

export const commandPalette = new CommandPalette();
