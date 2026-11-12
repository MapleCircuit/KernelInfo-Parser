/**
 * man_view.js - Man pages tab (tab type `man`, webapp/API_FRONTEND.md §5.38) and the rendered man page
 * of a code tab of the man-pages repository (`mountManDocument`).
 *
 * The tab belongs to the man-pages repository (`tab.repo`) at one of its tags (`tab.version`), like every
 * tab, and asks `/api/man/*` with that repository named on every request. Without a page it shows the
 * tag's index (`/api/man/{v}/index`), grouped by section with a filter box. The toolbar holds the tag
 * dropdown (`/api/man/tags`), the Web/Terminal toggle (per tab, default `kip-man-mode` in localStorage)
 * and the links back into the kernel: the syscall in the Kernel API tab and the SYNOPSIS symbols'
 * definitions, opened as tabs of the kernel's repository next to this one.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { escapeHtml } from "../../components/modal.js";
import { openDefinition } from "../../utils/definition_picker.js";
import { isManPagesRepo, manPagesRepo, projects } from "../../utils/projects.js";
import { renderManTerm, renderManWeb } from "./man_render.js";
import { groupIndex, linuxApiTab, linuxSymbolTab, man7Url, manLabel, manTabTitle, pageOfPath } from "./man_model.js";

const MODE_KEY = "kip-man-mode";
const MAX_SYMBOL_LINKS = 16;
let idSeq = 0;

/** Display mode a new man view starts in: the last one picked ("web" unless "term"). */
export function defaultManMode() {
  try {
    return localStorage.getItem(MODE_KEY) === "term" ? "term" : "web";
  } catch {
    return "web";
  }
}

function rememberManMode(mode) {
  try {
    localStorage.setItem(MODE_KEY, mode);
  } catch {
    // not kept: the next view starts in the default mode
  }
}

const modeToggleHtml = (mode) => `<span class="man-mode" role="group" aria-label="Display mode">
  <button type="button" class="code-btn${mode === "web" ? " active" : ""}" data-mode="web" aria-pressed="${mode === "web"}" title="man7.org-like page">Web</button>
  <button type="button" class="code-btn${mode === "term" ? " active" : ""}" data-mode="term" aria-pressed="${mode === "term"}" title="As man(1) shows it in a terminal">Terminal</button>
</span>`;

/** HTML of a page answer in a mode. */
function pageHtml(res, mode) {
  const opts = { name: res.name, section: res.section, idPrefix: `man${++idSeq}-` };
  const alias = res.resolved_path && res.path && res.resolved_path !== res.path
    ? `<div class="man-alias">${escapeHtml(res.path)} is an alias of <code>${escapeHtml(res.resolved_path)}</code></div>`
    : "";
  return alias + (mode === "term" ? renderManTerm(res.doc, opts) : renderManWeb(res.doc, opts));
}

/** Kernel revision and repository the back-links of a page target. */
function linuxTarget(res, kernel = "", kernelRepo = "") {
  const linux = res?.linux || {};
  return { version: kernel || linux.version || "", repo: kernelRepo || linux.repo || "" };
}

/** "Linux vX.Y: syscall read · fd · ssize_t" back-links of a page answer ("" when it has none). */
export function linuxLinksHtml(res, kernel = "", kernelRepo = "") {
  const linux = res?.linux;
  const t = linuxTarget(res, kernel, kernelRepo);
  if (!linux || !t.repo || !t.version) return "";
  const parts = [];
  if (linux.syscall) parts.push(`<a href="#" data-linux-syscall="${escapeHtml(linux.syscall)}" title="${escapeHtml(`The ${linux.syscall} system call in the Kernel API tab`)}">syscall <b>${escapeHtml(linux.syscall)}</b></a>`);
  for (const s of (Array.isArray(linux.symbols) ? linux.symbols : []).slice(0, MAX_SYMBOL_LINKS)) {
    parts.push(`<a href="#" data-linux-symbol="${escapeHtml(s)}" title="${escapeHtml(`Definition of ${s} in ${t.repo} ${t.version}`)}"><code>${escapeHtml(s)}</code></a>`);
  }
  if (!parts.length) return "";
  return `<span class="man-linux-label" title="${escapeHtml(`Opens in ${t.repo} ${t.version}, next to this tab`)}">Linux ${escapeHtml(t.version)}:</span> ${parts.join(" ")}`;
}

/**
 * Follow a back-link (`[data-linux-syscall]` / `[data-linux-symbol]`) of a page answer: a tab of the kernel's
 * repository opens in this workspace, next to the man tab (§5.18).
 */
async function followLinuxLink(a, res, { kernel = "", kernelRepo = "", forceNew = false } = {}) {
  const t = linuxTarget(res, kernel, kernelRepo);
  const syscall = a.dataset.linuxSyscall;
  const symbol = a.dataset.linuxSymbol;
  if (syscall) {
    state.openTab({ ...linuxApiTab({ repo: t.repo, version: t.version, name: syscall }), forceNew });
    return;
  }
  if (!symbol) return;
  try {
    if (await openDefinition(t.version, symbol, { forceNew, repo: t.repo })) return;
  } catch {
    // searched instead, below
  }
  toast.info(`No indexed definition of '${symbol}' at ${t.version}: searching for it`);
  state.openTab({ ...linuxSymbolTab({ repo: t.repo, version: t.version, symbol }), forceNew });
}

/** Scroll to an in-page anchor of a rendered page (the URL hash is the app's address: untouched). */
function scrollToAnchor(root, id) {
  const el = id && root.querySelector(`[id="${CSS.escape(id)}"]`);
  if (el) el.scrollIntoView({ block: "start" });
}

const notFound = (err) => err && (err.status === 404 || /not found/i.test(err.message || ""));

export class ManView {
  constructor() {
    this.seq = 0;
    this.prefetched = null; // {key, res}: the page a cross reference already fetched
    this.res = null;
    this.index = null;
  }

  render(containerEl, tab) {
    this.containerEl = containerEl;
    this.tab = tab;
    this.tabId = tab.id;
    containerEl.innerHTML = `<div class="man-view">
      <div class="man-toolbar">
        <button type="button" class="code-btn" data-act="index" title="Every page of this tag, by section">☰ Index</button>
        <span class="man-title"></span>
        <label class="man-tag-label">Tag <select class="man-tag" title="man-pages release"></select></label>
        ${modeToggleHtml(this.mode())}
        <span class="man-linux"></span>
        <a class="man-ext" hidden target="_blank" rel="noopener noreferrer" title="The same page on man7.org">man7.org ↗</a>
      </div>
      <div class="man-body"><div class="man-status">Loading…</div></div>
    </div>`;
    const root = containerEl.firstElementChild;
    root.addEventListener("click", (e) => this.onClick(e));
    root.querySelector(".man-tag").addEventListener("change", (e) => this.switchTag(e.target.value));
    this.load();
  }

  dispose() {
    this.seq++;
  }

  mode() {
    return this.tab.manMode === "term" || this.tab.manMode === "web" ? this.tab.manMode : defaultManMode();
  }

  /**
   * The man-pages repository shown: the tab's own; a tab of another project (a link that named none) takes
   * the configured man-pages repository once the project list is known.
   */
  repo() {
    const r = this.tab.repo || "";
    if (r && (isManPagesRepo(r) || !projects().length)) return r;
    return manPagesRepo() || r;
  }

  /** The tab's tag: its revision ("" until the newest tag is picked). */
  manVersion() {
    return this.tab.version || "";
  }

  /** Tabs saved with `manRepo` / `manVersion` fields keep them as the tab's own repository and revision. */
  migrateLegacy() {
    if (!this.tab.manRepo && !this.tab.manVersion) return;
    if (this.tab.manRepo) this.tab.repo = this.tab.manRepo;
    if (this.tab.manRepo || this.tab.manVersion) this.tab.version = this.tab.manVersion || "";
    delete this.tab.manRepo;
    delete this.tab.manVersion;
    state.saveState();
  }

  /** Move the tab to its repository / tag; the header follows when it is the active tab. */
  adopt(repo, version) {
    const patch = {};
    if (repo && this.tab.repo !== repo) patch.repo = repo;
    if (version && this.tab.version !== version) patch.version = version;
    if (!Object.keys(patch).length) return;
    this.persist(patch);
    if (state.getActiveTab() === this.tab) {
      state.syncRepoFromActiveTab();
      state.syncVersionFromTab(this.tab.version);
    }
  }

  persist(patch) {
    state.updateTabState(this.tabId, patch, { replace: true });
  }

  live(seq) {
    return seq === this.seq && this.containerEl?.isConnected;
  }

  body() {
    return this.containerEl.querySelector(".man-body");
  }

  status(html) {
    this.body().innerHTML = `<div class="man-status">${html}</div>`;
  }

  async load() {
    const seq = ++this.seq;
    this.migrateLegacy();
    const repo = this.repo();
    if (!repo) {
      this.status("No man-pages repository is configured (a repository whose project is <code>man-pages</code>).");
      return;
    }
    this.adopt(repo, "");
    const tags = this.loadTags(repo, seq);
    let mv = this.manVersion();
    if (!mv) {
      mv = (await tags)[0]?.name || "";
      if (!this.live(seq)) return;
      if (!mv) {
        this.status(`No man-pages release tag found in <code>${escapeHtml(repo)}</code>.`);
        return;
      }
    }
    this.adopt(repo, mv);
    if (this.tab.manPage || this.tab.manPath) await this.showPage(repo, mv, seq);
    else await this.showIndex(repo, mv, seq);
  }

  /** Fill the tag dropdown; resolves to the tags (newest first). */
  async loadTags(repo, seq) {
    let tags = [];
    try {
      tags = (await this.api.getManTags(repo))?.tags || [];
    } catch (err) {
      console.warn("Man-pages tags unavailable:", err);
    }
    if (!this.live(seq)) return tags;
    const sel = this.containerEl.querySelector(".man-tag");
    const cur = this.manVersion() || tags[0]?.name || "";
    const names = tags.map((t) => t.name);
    if (cur && !names.includes(cur)) names.unshift(cur);
    sel.innerHTML = names.map((n) => `<option value="${escapeHtml(n)}"${n === cur ? " selected" : ""}>${escapeHtml(n)}</option>`).join("");
    return tags;
  }

  pageKey(repo, mv, { name = "", section = "", path = "" }) {
    return JSON.stringify([repo, mv, path || "", path ? "" : name, path ? "" : section]);
  }

  async fetchPage(repo, mv, target) {
    const key = this.pageKey(repo, mv, target);
    if (this.prefetched?.key === key) {
      const { res } = this.prefetched;
      this.prefetched = null;
      return res;
    }
    return this.api.getManPage(repo, mv, { ...target, kernel: this.tab.kernelVersion || "", kernelRepo: this.tab.kernelRepo || "" });
  }

  async showPage(repo, mv, seq) {
    const target = { name: this.tab.manPage || "", section: this.tab.manSec || "", path: this.tab.manPath || "" };
    const label = target.path || manLabel(target.name, target.section);
    this.setTitle(label);
    this.status(`Loading ${escapeHtml(label)} at ${escapeHtml(mv)}…`);
    let res;
    try {
      res = await this.fetchPage(repo, mv, target);
    } catch (err) {
      if (!this.live(seq)) return;
      this.res = null;
      this.status(notFound(err)
        ? `<b>${escapeHtml(label)}</b> is not in ${escapeHtml(mv)}. <a href="#" data-act="index">Show the index of ${escapeHtml(mv)}</a>`
        : `Could not load ${escapeHtml(label)}: ${escapeHtml(err.message)}`);
      return;
    }
    if (!this.live(seq)) return;
    this.res = res;
    this.setTitle(manLabel(res.name || target.name, res.section || target.section), res);
    this.renderPage();
  }

  setTitle(text, res = null) {
    const el = this.containerEl.querySelector(".man-title");
    const aliases = (res?.aliases || []).filter((a) => a && a !== res.name);
    el.innerHTML = `<b>${escapeHtml(text)}</b>${aliases.length ? ` <span class="man-muted">also ${escapeHtml(aliases.slice(0, 6).join(", "))}</span>` : ""}`;
    const ext = this.containerEl.querySelector(".man-ext");
    ext.hidden = !res?.name;
    if (res?.name) ext.href = man7Url(res.name, res.section);
    this.containerEl.querySelector(".man-linux").innerHTML = res ? linuxLinksHtml(res, this.tab.kernelVersion, this.tab.kernelRepo) : "";
  }

  renderPage() {
    if (!this.res) return;
    this.body().innerHTML = `<div class="man-page man-mode-${this.mode()}">${pageHtml(this.res, this.mode())}</div>`;
    this.body().scrollTop = 0;
  }

  async showIndex(repo, mv, seq) {
    this.setTitle(`Man pages · ${mv}`);
    this.status(`Loading the index of ${escapeHtml(mv)}…`);
    let res;
    try {
      res = await this.api.getManIndex(repo, mv);
    } catch (err) {
      if (this.live(seq)) this.status(`Could not load the index of ${escapeHtml(mv)}: ${escapeHtml(err.message)}`);
      return;
    }
    if (!this.live(seq)) return;
    this.res = null;
    this.index = res?.pages || [];
    this.body().innerHTML = `<div class="man-index">
      <div class="man-index-head">
        <input type="search" class="man-filter" placeholder="Filter pages by name or summary…" value="${escapeHtml(this.tab.manQuery || "")}" aria-label="Filter man pages" />
        <span class="man-index-count man-muted"></span>
      </div>
      <nav class="man-index-toc"></nav>
      <div class="man-index-groups"></div>
    </div>`;
    const input = this.body().querySelector(".man-filter");
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        this.persist({ manQuery: input.value.trim() });
        this.renderIndexGroups();
      }, 120);
    });
    this.renderIndexGroups();
  }

  renderIndexGroups() {
    const root = this.body().querySelector(".man-index");
    if (!root) return;
    const groups = groupIndex(this.index, this.tab.manQuery || "");
    const total = groups.reduce((n, g) => n + g.pages.length, 0);
    root.querySelector(".man-index-count").textContent = `${total.toLocaleString("en-US")} of ${(this.index || []).length.toLocaleString("en-US")} pages`;
    const prefix = `manidx${++idSeq}-`;
    root.querySelector(".man-index-toc").innerHTML = groups.map((g) =>
      `<a href="#" data-man-anchor="${escapeHtml(prefix + g.section)}" title="${escapeHtml(g.title)}">${escapeHtml(g.section)} <span class="man-muted">${g.pages.length}</span></a>`).join("");
    root.querySelector(".man-index-groups").innerHTML = groups.length ? groups.map((g) => `<section class="man-index-sec">
      <h2 id="${escapeHtml(prefix + g.section)}">${escapeHtml(g.section)} · ${escapeHtml(g.title)}</h2>
      <ul class="man-index-list">${g.pages.map((p) => `<li><a href="#" data-man-ref="${escapeHtml(p.name)}" data-man-sec="${escapeHtml(p.section || "")}" data-man-path="${escapeHtml(p.path || "")}">${escapeHtml(manLabel(p.name, p.section))}</a>${p.so ? ' <span class="man-muted" title="alias page">→</span>' : ""}${p.summary ? ` <span class="man-summary">${escapeHtml(p.summary)}</span>` : ""}</li>`).join("")}</ul>
    </section>`).join("") : `<div class="man-status">No page matches.</div>`;
  }

  onClick(e) {
    const root = this.containerEl.firstElementChild;
    const forceNew = e.ctrlKey || e.metaKey;
    const act = e.target.closest("[data-act]");
    if (act?.dataset.act === "index") {
      e.preventDefault();
      this.navigate({ name: "", section: "", path: "" }, { forceNew });
      return;
    }
    const mode = e.target.closest("[data-mode]");
    if (mode) {
      this.setMode(mode.dataset.mode);
      return;
    }
    const anchor = e.target.closest("[data-man-anchor]");
    if (anchor) {
      e.preventDefault();
      scrollToAnchor(root, anchor.dataset.manAnchor);
      return;
    }
    const ref = e.target.closest("[data-man-ref]");
    if (ref) {
      e.preventDefault();
      this.followRef({ name: ref.dataset.manRef, section: ref.dataset.manSec || "", path: ref.dataset.manPath || "" }, forceNew);
      return;
    }
    const linux = e.target.closest("[data-linux-syscall], [data-linux-symbol]");
    if (linux) {
      e.preventDefault();
      followLinuxLink(linux, this.res, { kernel: this.tab.kernelVersion, kernelRepo: this.tab.kernelRepo, forceNew });
    }
  }

  setMode(mode) {
    if (mode !== "web" && mode !== "term") return;
    rememberManMode(mode);
    this.persist({ manMode: mode });
    for (const b of this.containerEl.querySelectorAll(".man-mode [data-mode]")) {
      b.classList.toggle("active", b.dataset.mode === mode);
      b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
    }
    this.renderPage();
  }

  /** A cross reference: fetched first, so a page missing at this tag leaves the tab where it is. */
  async followRef(target, forceNew) {
    const repo = this.repo();
    const mv = this.manVersion();
    if (!repo || !mv) return;
    if (!forceNew) {
      try {
        const res = await this.api.getManPage(repo, mv, { ...target, kernel: this.tab.kernelVersion || "", kernelRepo: this.tab.kernelRepo || "" });
        this.prefetched = { key: this.pageKey(repo, mv, target), res };
      } catch (err) {
        toast.warning(notFound(err)
          ? `${manLabel(target.name, target.section)} is not in ${mv}`
          : `Could not open ${manLabel(target.name, target.section)}: ${err.message}`);
        return;
      }
    }
    this.navigate(target, { forceNew });
  }

  /** Show another page (or the index) in this tab — a history entry — or in a new tab. */
  navigate({ name = "", section = "", path = "" }, { forceNew = false } = {}) {
    const fields = { manPage: name, manSec: section, manPath: path };
    if (forceNew) {
      state.openTab({
        type: "man", repo: this.repo(), version: this.manVersion(), manMode: this.tab.manMode || "",
        kernelVersion: this.tab.kernelVersion || "", kernelRepo: this.tab.kernelRepo || "", manQuery: "",
        ...fields, title: manTabTitle(fields), forceNew: true,
      });
      return;
    }
    for (const [k, v] of Object.entries(fields)) {
      if (v) this.tab[k] = v;
      else delete this.tab[k];
    }
    this.tab.title = manTabTitle(this.tab);
    state.saveState();
    state.emit("tabs:changed", { paneId: state.activePaneId, activeTabId: this.tabId });
  }

  /** The tag is the tab's revision: the header follows it while the tab is the active one. */
  switchTag(tag) {
    if (!tag || tag === this.manVersion()) return;
    state.setVersion(tag, this.tab);
  }
}

/**
 * Rendered man page of a code tab of the man-pages repository (`opts.repo`): the page at
 * `path` and revision `version`, with the mode toggle, the back-links into the kernel and the
 * rendered/source toggle. Cross references open in the same code tab (`onNavigate(path)`).
 * opts: {repo, version, path, tab, canGoBack, onBack, onShowSource, onNavigate}.
 */
export async function mountManDocument(host, opts) {
  const tab = opts.tab || {};
  const mode = () => (tab.manMode === "term" || tab.manMode === "web" ? tab.manMode : defaultManMode());
  host.innerHTML = `<div class="man-view man-in-code">
    <div class="man-toolbar">
      ${opts.canGoBack ? '<button type="button" class="code-btn" data-act="back" title="Previous page">← Back</button>' : ""}
      <span class="man-title"><b>${escapeHtml(pageOfPath(opts.path) ? manLabel(pageOfPath(opts.path).name, pageOfPath(opts.path).section) : opts.path)}</b></span>
      ${modeToggleHtml(mode())}
      <button type="button" class="code-btn" data-act="source" title="The roff source of this page">&lt;/&gt; Source</button>
      <button type="button" class="code-btn" data-act="tab" title="Open this page in a man pages tab">Man tab</button>
      <span class="man-linux"></span>
    </div>
    <div class="man-body"><div class="man-status">Rendering ${escapeHtml(opts.path)}…</div></div>
  </div>`;
  const root = host.firstElementChild;
  const body = root.querySelector(".man-body");
  let res = null;
  const draw = () => {
    if (res) body.innerHTML = `<div class="man-page man-mode-${mode()}">${pageHtml(res, mode())}</div>`;
  };
  root.addEventListener("click", async (e) => {
    const forceNew = e.ctrlKey || e.metaKey;
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "back") return opts.onBack?.();
    if (act === "source") return opts.onShowSource?.();
    if (act === "tab") {
      const p = pageOfPath(opts.path) || {};
      const fields = { manPage: res?.name || p.name || "", manSec: res?.section || p.section || "", manPath: opts.path };
      state.openTab({ type: "man", repo: opts.repo, version: opts.version, manMode: mode(), ...fields, title: manTabTitle(fields) });
      return;
    }
    const m = e.target.closest("[data-mode]");
    if (m) {
      tab.manMode = m.dataset.mode;
      rememberManMode(tab.manMode);
      state.saveState();
      for (const b of root.querySelectorAll(".man-mode [data-mode]")) b.classList.toggle("active", b.dataset.mode === tab.manMode);
      draw();
      return;
    }
    const anchor = e.target.closest("[data-man-anchor]");
    if (anchor) {
      e.preventDefault();
      scrollToAnchor(root, anchor.dataset.manAnchor);
      return;
    }
    const ref = e.target.closest("[data-man-ref]");
    if (ref) {
      e.preventDefault();
      const label = manLabel(ref.dataset.manRef, ref.dataset.manSec);
      try {
        const r = await api.forRepo(opts.repo).getManPage(opts.repo, opts.version, { name: ref.dataset.manRef, section: ref.dataset.manSec || "" });
        const path = r.path || r.resolved_path;
        if (forceNew) state.openTab({ type: "code", title: path.split("/").pop(), path, repo: opts.repo, version: opts.version, forceNew: true });
        else opts.onNavigate?.(path);
      } catch (err) {
        toast.warning(notFound(err) ? `${label} is not in ${opts.version}` : `Could not open ${label}: ${err.message}`);
      }
      return;
    }
    const linux = e.target.closest("[data-linux-syscall], [data-linux-symbol]");
    if (linux) {
      e.preventDefault();
      followLinuxLink(linux, res, { forceNew });
    }
  });
  try {
    res = await api.forRepo(opts.repo).getManPage(opts.repo, opts.version, { path: opts.path });
  } catch (err) {
    if (root.isConnected) {
      body.innerHTML = `<div class="man-status">Could not render ${escapeHtml(opts.path)}: ${escapeHtml(err.message)} · <a href="#" data-act="source">Show the source</a></div>`;
    }
    return;
  }
  if (!root.isConnected) return;
  root.querySelector(".man-linux").innerHTML = linuxLinksHtml(res);
  draw();
}
