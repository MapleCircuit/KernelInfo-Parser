/**
 * command_palette.js - Global Ctrl+K search & command palette.
 *
 * Prefixes select the mode: (none) files, symbols and Kconfig options together; "/" commands;
 * "$" symbols; "#" commits; "?" Kconfig options; ":" go to line in the active code tab; "@" mailing list;
 * "%" text in every file of the revision (git grep, §5.34).
 */
import { api } from "../api.js";
import { state } from "../state.js";
import { debounce } from "../utils/debounce.js";
import { shortRef } from "./ref_picker.js";
import { shortcuts } from "../utils/shortcuts.js";
import { groupByFile, grepSummary, highlightRanges } from "../utils/grep_format.js";
import { viewOf } from "../utils/active_views.js";
import { KIND_ICONS, matchSymbols } from "../views/code_view/outline_model.js";

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
  "/": { label: "Commands", placeholder: "Run a command…" },
  "$": { label: "Symbols", placeholder: "Search symbol definitions…" },
  "#": { label: "Commits", placeholder: "Search commits by subject, author or hash…" },
  "?": { label: "Kconfig", placeholder: "Search Kconfig options…" },
  ":": { label: "Go to line", placeholder: "Line number, or a definition name, in the active file…" },
  "@": { label: "Mailing List", placeholder: "Search LKML emails, patches, and threads…" },
  "%": { label: "Code", placeholder: "Search text in every file of this revision…" },
};
const GREP_PALETTE_LIMIT = 30;

class CommandPalette {
  constructor() {
    this.el = null;
    this.items = [];
    this.highlightIdx = 0;
    this.seq = 0;
    // Input text the rendered results were computed for, and an Enter pressed before they caught up.
    this.resultsFor = null;
    this.pendingEnter = null;
    this.commandProvider = () => [];
    this.debouncedSearch = debounce(() => this.search(), 160);
    // Text search runs git grep on the server: wait for a pause in typing
    this.debouncedGrep = debounce(() => this.search(), 400);
  }

  /** Register the function returning palette commands: [{ id, title, hint, run }]. */
  setCommandProvider(fn) {
    this.commandProvider = fn;
  }

  init() {
    // Global keys go through the shortcut registry (webapp/API.md §5.31), so they can be rebound
    shortcuts.registerAll([
      { id: "palette.open", keys: ["Mod+K", "Mod+P"], scope: "global", allowInInputs: true,
        description: "Search files, symbols and Kconfig options", handler: () => this.open("") },
      { id: "palette.commands", keys: ["Mod+Shift+P"], scope: "global", allowInInputs: true,
        description: "Run a command", handler: () => this.open("/") },
      { id: "palette.navigate", keys: ["ArrowUp", "ArrowDown"], scope: "palette", docOnly: true, description: "Move through the results" },
      { id: "palette.run", keys: ["Enter"], scope: "palette", docOnly: true, description: "Open the highlighted result" },
      { id: "palette.runNewTab", keys: ["Mod+Enter"], scope: "palette", docOnly: true, description: "Open the highlighted result in a new tab" },
      { id: "palette.close", keys: ["Escape"], scope: "palette", docOnly: true, description: "Close the palette" },
    ]);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.el) this.close();
    });
  }

  /** Footer hint of the palette's own keys, following the user's bindings. */
  keyHint() {
    const open = shortcuts.label("palette.open");
    const cmds = shortcuts.label("palette.commands");
    return [open && `<span><kbd>${escapeHtml(open)}</kbd> search</span>`, cmds && `<span><kbd>${escapeHtml(cmds)}</kbd> commands</span>`]
      .filter(Boolean).join("");
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
          <span><kbd>↑↓</kbd> navigate</span><span><kbd>Enter</kbd> open</span><span><kbd>Ctrl+Enter</kbd> new tab</span>${this.keyHint()}
          <span class="cmdp-prefixes"><code>/</code> commands <code>$</code> symbols <code>#</code> commits <code>?</code> kconfig <code>:</code> line <code>@</code> mail <code>%</code> code</span>
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
      this.pendingEnter = null;
      this.updateMode();
      const { mode } = this.parse();
      if (mode === "/" || mode === ":") this.search();
      else if (mode === "%") this.debouncedGrep();
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
    this.resultsFor = null;
    this.pendingEnter = null;
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
    const raw = this.el.querySelector(".cmdp-input").value;
    const { mode, query } = this.parse();
    const version = state.getActiveTab()?.version || state.currentVersion;
    let items = [];

    if (mode === "/") {
      const q = query.toLowerCase();
      items = this.commandProvider()
        .filter((c) => !q || c.title.toLowerCase().includes(q) || (c.keywords || "").toLowerCase().includes(q))
        .map((c) => ({ section: "Commands", icon: "›", title: c.title, detail: c.hint || "", run: c.run, highlightQuery: query }));
    } else if (mode === ":") {
      const tab = state.getActiveTab();
      const view = tab?.type === "code" ? viewOf(tab.id) : null;
      if (query && !/^\d+$/.test(query) && tab?.type === "code") {
        // ":name" — definitions of the active file (its outline, G03)
        const outline = view?.outline;
        if (!outline?.loaded) {
          items = [{ section: "Go to symbol", icon: "ℹ", title: "The outline of this file is still loading", detail: "", run: null }];
        } else if (!outline.indexed || !outline.items.length) {
          items = [{ section: "Go to symbol", icon: "ℹ", title: "Outline needs an indexed file", detail: tab.path, run: null }];
        } else {
          items = matchSymbols(outline.items, query).map((it) => ({
            section: "Go to symbol", icon: KIND_ICONS[it.kind] || "•", title: it.name, highlightQuery: query,
            detail: `${it.type_name} · line ${it.line_s}`,
            run: (nt) => (nt ? this.openCode(tab.path, tab.version, it.line_s, true) : view.scrollToLine(it.line_s, { select: true })),
          }));
          if (!items.length) items = [{ section: "Go to symbol", icon: "ℹ", title: `No definition matches “${query}”`, detail: tab.path, run: null }];
        }
      } else {
        const line = parseInt(query, 10);
        if (tab?.type === "code" && line > 0) {
          items = [{ section: "Go to line", icon: "↳", title: `Line ${line}`, detail: tab.path, run: () => this.openCode(tab.path, tab.version, line, false) }];
        } else {
          items = [{ section: "Go to line", icon: "ℹ", title: tab?.type === "code" ? "Type a line number, or a name to go to a definition" : "Open a code tab first", detail: "", run: null }];
        }
      }
    } else if (mode === "%") {
      // Full-text search (§5.34): the first matches, grouped by file; the Search tab has them all
      if (query.length < 2) {
        items = [{ section: "Code", icon: "ℹ", title: "Type at least 2 characters to search the files' text", detail: "", run: null }];
      } else {
        let res = null;
        let error = null;
        try {
          res = await api.grepCode(version, { q: query, limit: GREP_PALETTE_LIMIT, perFile: 10 });
        } catch (err) {
          error = err;
        }
        if (seq !== this.seq || !this.el) return;
        if (error) {
          items = [{ section: "Code", icon: "⚠", title: `Search failed: ${error.message}`, detail: "", run: null }];
        } else {
          for (const g of groupByFile(res?.matches || [])) {
            for (const m of g.matches) {
              items.push({
                section: g.path, icon: "≡", titleHtml: highlightRanges(m.text.trim() ? m.text : "(blank line)", m.text.trim() ? m.ranges : []),
                detail: `:${m.line}`, run: (nt) => this.openCode(m.path, version, m.line, nt),
              });
            }
          }
          if (!items.length) items.push({ section: "Code", icon: "ℹ", title: `No file of ${shortRef(version)} contains “${query}”`, detail: "", run: null });
          items.push({
            section: "Search tab", icon: "🔎", title: `Open all results in the Search tab`, detail: res?.truncated ? "more than shown here" : grepSummary(res),
            run: () => state.openTab({ type: "search", title: `Search: ${query.slice(0, 24)}`, query, version }),
          });
        }
      }
    } else if (!query) {
      items = this.commandProvider().slice(0, 8).map((c) => ({ section: "Suggested commands", icon: "›", title: c.title, detail: c.hint || "", run: c.run }));
    } else {
      const wantFiles = mode === "";
      const wantSymbols = mode === "" || mode === "$";
      const wantKconfig = mode === "" || mode === "?";
      const wantCommits = mode === "#";
      const wantMail = mode === "@" || (mode === "" && query.length >= 3);
      const [files, symbols, kconfig, commits, mail] = await Promise.allSettled([
        wantFiles ? api.searchFiles(version, query, 25) : null,
        wantSymbols && query.length >= 2 ? api.searchSymbols(version, query, mode === "$" ? 50 : 15) : null,
        wantKconfig && query.length >= 2 ? api.searchKconfig(version, query.replace(/^CONFIG_/i, ""), mode === "?" ? 40 : 8) : null,
        wantCommits ? api.searchCommits(version, query, 40) : null,
        wantMail ? api.searchMailingList(query, { limit: mode === "@" ? 30 : 6 }) : null,
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
        const arch = state.kconfigArch();
        items.push({
          section: "Kconfig", icon: "⚙", title: `CONFIG_${k.name}`, detail: k.prompt || k.type_name || "", highlightQuery: query,
          run: () => state.openTab({ type: "kconfig", title: `KConfig (${arch})`, arch, version, symbol: k.name }),
        });
      }
      for (const c of val(commits)?.commits || []) {
        items.push({
          section: "Commits", icon: "●", title: c.subject || c.commit_hash, detail: `${c.commit_hash.slice(0, 12)} · ${c.author_name || c.author_email || ""}`, highlightQuery: query,
          run: () => {
            state.setSelectedVersion(c.commit_hash);
            state.openTab({ type: "explorer", title: `Explorer ${c.commit_hash.slice(0, 8)}`, path: "", version: c.commit_hash, forceNew: true });
          },
        });
      }
      for (const m of val(mail)?.results || []) {
        const shortSubj = m.subject || m.message_id || "Thread";
        items.push({
          section: "Mailing List",
          icon: m.is_patch ? "✉" : "💬",
          title: shortSubj,
          detail: `${m.author || "Unknown"} · ${m.date_iso || ""}`,
          highlightQuery: query,
          run: (nt) => state.openTab({
            type: "mail",
            title: `LKML: ${shortSubj.slice(0, 32)}`,
            threadId: m.tid,
            messageId: m.message_id,
            forceNew: Boolean(nt),
          }),
        });
      }
      if (items.length === 0) {
        items.push({ section: "No results", icon: "ℹ", title: `Nothing matches “${query}” in ${shortRef(version)}`, detail: "", run: null });
      }
    }

    this.items = items;
    this.resultsFor = raw;
    this.highlightIdx = items.findIndex((it) => it.run);
    this.render();
    if (this.pendingEnter) {
      const { newTab } = this.pendingEnter;
      this.pendingEnter = null;
      this.execute(this.highlightIdx, newTab);
    }
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
        <span class="cmdp-title">${it.titleHtml ?? highlight(it.title, it.highlightQuery)}</span>
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
      const newTab = e.ctrlKey || e.metaKey;
      // Results still belong to an earlier input (debounced search pending or in flight):
      // search the current text now and open its top result instead of a stale row.
      if (e.target.value !== this.resultsFor) {
        this.pendingEnter = { newTab };
        this.search();
        return;
      }
      this.execute(this.highlightIdx, newTab);
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
