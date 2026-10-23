/**
 * doc_page.js - kernel.org-style documentation page (dark theme) for Documentation/ files.
 *
 * Layout mirrors https://www.kernel.org/doc/html/latest/: a left sidebar with the project
 * title, version, quick search, page contents and "This Page" links, and a serif body.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { shortRef } from "../../components/ref_picker.js";
import { renderDocument, renderRstFragment, extractTitle, escapeHtml, highlightCode, slugify } from "./rst_renderer.js";

const TITLE_FETCH_LIMIT = 80;
const TITLE_FETCH_CONCURRENCY = 4;
const KERNEL_DOC_CONCURRENCY = 3;

/** kernel-doc inline markup → reStructuredText (Documentation/doc-guide/kernel-doc.rst "Highlights"). */
function kernelDocToRst(text) {
  return String(text || "")
    .replace(/&(struct|union|enum|typedef)\s+([A-Za-z_]\w*)(->\w+|\.\w+)?/g, (_, kind, name, member) => `:c:type:\`${kind} ${name}${member || ""} <${name}>\``)
    .replace(/(^|[^\w`&])&([A-Za-z_]\w*)\b(?!\()/g, "$1:c:type:`$2`")
    .replace(/(^|[^\w`])%([A-Za-z_][\w]*)/g, "$1``$2``")
    .replace(/(^|[^\w`@.])@([A-Za-z_][\w.]*)/g, "$1**$2**");
}

export class DocPage {
  /**
   * @param {{ path: string, version: string, onNavigate: (path: string, opts?: object) => void,
   *           onShowSource: () => void, canGoBack: boolean, onBack: () => void, anchor?: string }} opts
   */
  constructor(opts) {
    this.opts = opts;
    this.root = null;
    this.searchMarks = [];
    this.searchIdx = -1;
    this._scrollRaf = null;
  }

  mount(containerEl, source) {
    const { path, version } = this.opts;
    const doc = renderDocument(source, path);
    const title = doc.title || path.split("/").pop();
    const docIndex = path.startsWith("Documentation/") ? "Documentation/index.rst" : null;

    containerEl.innerHTML = `
      <div class="kdoc">
        <aside class="kdoc-sidebar">
          <div class="kdoc-brand">
            <a class="kdoc-logo" ${docIndex ? `data-doc="${docIndex}"` : ""} href="#">The Linux Kernel</a>
            <div class="kdoc-version" title="${escapeHtml(version)}">${escapeHtml(shortRef(version).replace(/^v/, ""))}</div>
          </div>
          <div class="kdoc-search">
            <h3>Quick search</h3>
            <form class="kdoc-search-form">
              <input type="text" class="kdoc-search-input" placeholder="Search this page" spellcheck="false" autocomplete="off" />
              <button type="submit" class="kdoc-search-btn">Go</button>
            </form>
            <div class="kdoc-search-status"></div>
          </div>
          ${this.opts.canGoBack ? `<div class="kdoc-nav-links"><a href="#" class="kdoc-back">← Back</a></div>` : ""}
          <div class="kdoc-sidebar-section">
            <h3>Contents</h3>
            <div class="kdoc-localtoc">${this.renderToc(doc.toc, 4) || `<p class="kdoc-muted">No sections</p>`}</div>
          </div>
          <div class="kdoc-sidebar-section kdoc-related"></div>
          <div class="kdoc-sidebar-section">
            <h3>This Page</h3>
            <ul class="kdoc-this-page">
              <li><a href="#" class="kdoc-show-source">Show Source</a></li>
              <li><a href="#" class="kdoc-copy-link">Copy page path</a></li>
            </ul>
          </div>
        </aside>
        <main class="kdoc-main">
          <div class="kdoc-body" role="main">${doc.html}</div>
          <footer class="kdoc-footer">
            ©The kernel development community. | Rendered from <code>${escapeHtml(path)}</code> at <code>${escapeHtml(shortRef(version))}</code> |
            <a href="#" class="kdoc-show-source">Page source</a>
          </footer>
        </main>
      </div>
    `;
    this.root = containerEl.querySelector(".kdoc");
    this.scroller = this.root.querySelector(".kdoc-main");

    // .. contents:: directive placeholders receive the local table of contents
    this.root.querySelectorAll(".contents.local").forEach((el) => {
      const depth = parseInt(el.dataset.localToc || "", 10) || 3;
      el.insertAdjacentHTML("beforeend", this.renderToc(doc.toc.filter((t) => t.level > 1), depth + 1) || "");
    });

    this.bindLinks();
    this.bindSearch();
    this.bindScrollSpy();
    this.fillToctreeTitles();
    this.renderRelated();
    this.hydrateImages();
    this.hydrateKernelDoc();
    this.resolveCrossDocRefs();

    if (this.opts.anchor) this.scrollToId(this.opts.anchor, false);
    return { title };
  }

  renderToc(toc, maxLevel) {
    const entries = toc.filter((t) => t.level <= maxLevel);
    if (entries.length === 0) return "";
    // Build nested lists from the flat heading sequence; stack holds the level of each open <ul>
    let html = "";
    const stack = [];
    for (const t of entries) {
      while (stack.length && t.level < stack[stack.length - 1]) {
        html += `</li></ul>`;
        stack.pop();
      }
      if (!stack.length || t.level > stack[stack.length - 1]) {
        html += `<ul>`;
        stack.push(t.level);
      } else {
        html += `</li>`;
      }
      html += `<li><a class="reference internal" href="#${escapeHtml(t.id)}" data-toc-id="${escapeHtml(t.id)}">${t.title}</a>`; // title is already escaped inline HTML text
    }
    while (stack.length) {
      html += `</li></ul>`;
      stack.pop();
    }
    return html;
  }

  scrollToId(id, smooth = true) {
    const target = id && this.root.querySelector(`#${CSS.escape(id)}`);
    if (!target) return;
    target.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
    target.classList.add("kdoc-target-flash");
    setTimeout(() => target.classList.remove("kdoc-target-flash"), 1200);
  }

  bindLinks() {
    this.root.addEventListener("click", async (e) => {
      const a = e.target.closest("a");
      if (!a || !this.root.contains(a)) return;
      const newTab = e.ctrlKey || e.metaKey;

      if (a.classList.contains("kdoc-show-source")) {
        e.preventDefault();
        this.opts.onShowSource();
      } else if (a.classList.contains("kdoc-copy-link")) {
        e.preventDefault();
        try {
          await navigator.clipboard.writeText(this.opts.path);
          toast.success("Copied document path");
        } catch {
          toast.error("Clipboard unavailable");
        }
      } else if (a.classList.contains("kdoc-back")) {
        e.preventDefault();
        this.opts.onBack();
      } else if (a.dataset.doc) {
        e.preventDefault();
        this.navigate(a.dataset.doc, newTab);
      } else if (a.dataset.src) {
        e.preventDefault();
        this.openSource(a.dataset.src);
      } else if (a.dataset.symbol) {
        e.preventDefault();
        await this.openSymbol(a.dataset.symbol, newTab);
      } else if (a.dataset.kconfig) {
        e.preventDefault();
        state.openTab({ type: "kconfig", title: "KConfig (x86)", arch: "x86", version: this.opts.version, symbol: a.dataset.kconfig });
      } else if ((a.getAttribute("href") || "").startsWith("#")) {
        // In-page anchors must not touch location.hash (it drives the app router)
        e.preventDefault();
        this.scrollToId(decodeURIComponent(a.getAttribute("href").slice(1)));
      }
    });

    this.root.addEventListener("auxclick", (e) => {
      if (e.button !== 1) return;
      const a = e.target.closest("a[data-doc], a[data-src], a[data-symbol]");
      if (!a) return;
      e.preventDefault();
      if (a.dataset.doc) this.navigate(a.dataset.doc, true);
      else if (a.dataset.src) this.openSource(a.dataset.src, true);
      else if (a.dataset.symbol) this.openSymbol(a.dataset.symbol, true);
    });
  }

  navigate(path, newTab = false) {
    if (newTab) {
      const [docPath, anchor] = path.split("#");
      state.openTab({ type: "code", title: docPath.split("/").pop(), path: docPath, docAnchor: anchor || "", version: this.opts.version, forceNew: true });
    } else {
      this.opts.onNavigate(path);
    }
  }

  openSource(path, forceNew = false) {
    state.openTab({ type: "code", title: path.split("/").pop(), path, version: this.opts.version, docMode: "source", forceNew });
  }

  async openSymbol(name, forceNew) {
    try {
      const detail = await api.getSymbolDetail(this.opts.version, name);
      if (detail && detail.file_path) {
        state.openTab({
          type: "code",
          title: detail.file_path.split("/").pop(),
          path: detail.file_path,
          version: this.opts.version,
          cursorLine: detail.definition?.line_s || detail.line_s || detail.line || 1,
          forceNew,
        });
        return;
      }
    } catch {
      // fall through to the toast below
    }
    toast.info(`No indexed definition found for '${name}' at ${shortRef(this.opts.version)}`);
  }

  async fillToctreeTitles() {
    const links = [...this.root.querySelectorAll("a[data-fetch-title]")].slice(0, TITLE_FETCH_LIMIT);
    let next = 0;
    const worker = async () => {
      while (next < links.length) {
        const a = links[next++];
        try {
          const res = await api.getFileContent(this.opts.version, a.dataset.doc);
          const title = res && !res.is_binary && res.content ? extractTitle(res.content) : "";
          if (title && a.isConnected) {
            const span = a.querySelector(".doc") || a;
            span.textContent = title.replace(/\*\*|``|\\/g, "");
          }
          if (!res || (!res.content && !res.is_directory)) a.classList.add("kdoc-missing");
        } catch {
          if (a.isConnected) {
            a.classList.add("kdoc-missing");
            a.title = "Document not present at this version";
          }
        }
      }
    };
    await Promise.all(Array.from({ length: TITLE_FETCH_CONCURRENCY }, worker));
  }

  /** Figures and images are served from the Git tree through /api/fs/raw. */
  hydrateImages() {
    this.root.querySelectorAll("img.kdoc-img[data-raw]").forEach((img) => {
      img.addEventListener("error", () => {
        const path = img.dataset.raw;
        img.replaceWith(Object.assign(document.createElement("div"), {
          className: "kdoc-figure-placeholder",
          innerHTML: `🖼 <a class="reference internal" data-src="${escapeHtml(path)}" href="#">${escapeHtml(path)}</a> <span class="kdoc-muted">(image not renderable here)</span>`,
        }));
      }, { once: true });
      img.src = api.getRawFileUrl(this.opts.version, img.dataset.raw);
    });
  }

  /** Replace `.. kernel-doc::` cards with the API documentation extracted from the source comments. */
  async hydrateKernelDoc() {
    const blocks = [...this.root.querySelectorAll(".kdoc-kerneldoc[data-kdoc-path]")];
    let next = 0;
    const worker = async () => {
      while (next < blocks.length) {
        const el = blocks[next++];
        let opts = {};
        try {
          opts = JSON.parse(el.dataset.kdocOpts || "{}");
        } catch {
          opts = {};
        }
        const query = {
          export: "export" in opts,
          internal: "internal" in opts,
          functions: opts.functions || opts.identifiers || "",
          doc: opts.doc || "",
          no_identifiers: opts["no-identifiers"] || "",
        };
        try {
          const res = await api.getKernelDoc(this.opts.version, el.dataset.kdocPath, query);
          if (!el.isConnected) return;
          if (!res.entries?.length) {
            el.querySelector(".kdoc-directive-card")?.insertAdjacentHTML("beforeend", `<div class="kdoc-muted">No matching kernel-doc comments.</div>`);
            continue;
          }
          el.innerHTML = res.entries.map((e) => this.renderKernelDocEntry(e, el.dataset.kdocPath, Boolean(query.doc))).join("");
          this.bindKernelDocSources(el);
        } catch (err) {
          el.querySelector(".kdoc-directive-card")?.insertAdjacentHTML("beforeend", `<div class="kdoc-muted">kernel-doc unavailable: ${escapeHtml(err.message)}</div>`);
        }
      }
    };
    await Promise.all(Array.from({ length: KERNEL_DOC_CONCURRENCY }, worker));
  }

  renderKernelDocEntry(e, srcPath, docOnly) {
    const path = this.opts.path;
    const body = (text) => renderRstFragment(kernelDocToRst(text), path);
    if (e.kind === "doc") {
      return docOnly ? body(e.sections[0]?.body || "") : `<p class="rubric">${escapeHtml(e.name)}</p>${body(e.sections[0]?.body || "")}`;
    }
    const id = `c.${e.name}`;
    const isFunc = e.kind === "function" || e.kind === "macro";
    const sigText = isFunc ? (e.signature || `${e.name}()`).replace(/\s+/g, " ").replace(/\s*;\s*$/, "") : `${e.kind} ${e.name}`;
    const params = e.params.length
      ? `<p class="rubric">${isFunc ? "Parameters" : "Members"}</p><dl class="simple">${e.params.map((p) => `<dt><code class="docutils literal notranslate">${escapeHtml(p.name)}</code></dt><dd>${body(p.desc)}</dd>`).join("")}</dl>`
      : "";
    const definition = !isFunc && e.signature
      ? `<p class="rubric">Definition</p><div class="highlight-c notranslate"><pre class="literal-block">${highlightCode(e.signature, "c")}</pre></div>`
      : "";
    const sections = e.sections.map((sec) => `<p class="rubric">${escapeHtml(sec.title)}</p>${body(sec.body)}`).join("");
    return `
      <dl class="c ${escapeHtml(e.kind)}">
        <dt class="sig sig-object c" id="${escapeHtml(id)}">
          <code class="sig-name">${isFunc ? highlightCode(sigText, "c") : `<span class="hl-k">${escapeHtml(e.kind)}</span> ${escapeHtml(e.name)}`}</code>
          <a class="kdoc-headerlink" href="#${escapeHtml(id)}" title="Link to this definition">¶</a>
          <a class="kdoc-src-link" href="#" data-src="${escapeHtml(srcPath)}" data-line="${e.line}" title="Open ${escapeHtml(srcPath)}:${e.line}">[source]</a>
        </dt>
        <dd>${e.brief ? `<p>${body(e.brief).replace(/^<p>|<\/p>$/g, "")}</p>` : ""}${definition}${params}${sections}</dd>
      </dl>`;
  }

  bindKernelDocSources(el) {
    el.querySelectorAll(".kdoc-src-link[data-line]").forEach((a) => {
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        state.openTab({ type: "code", title: a.dataset.src.split("/").pop(), path: a.dataset.src, version: this.opts.version, cursorLine: parseInt(a.dataset.line, 10) || 1, docMode: "source", forceNew: ev.ctrlKey || ev.metaKey });
      });
    });
  }

  /** `:ref:` targets defined in other documents, resolved through the Documentation/ label index. */
  async resolveCrossDocRefs() {
    const refs = [...this.root.querySelectorAll(".kdoc-unresolved[data-ref]")];
    if (!refs.length) return;
    let index;
    try {
      index = (await api.getDocLabels(this.opts.version)).labels || {};
    } catch {
      return;
    }
    for (const span of refs) {
      const entry = index[span.dataset.ref];
      if (!entry || !span.isConnected) continue;
      const a = document.createElement("a");
      a.className = "reference internal";
      a.href = "#";
      a.dataset.doc = `${entry.path}#${slugify(span.dataset.ref)}`;
      a.textContent = span.dataset.refExplicit ? span.textContent : (entry.title || span.textContent);
      a.title = `${entry.path}${entry.title ? ` — ${entry.title}` : ""}`;
      span.replaceWith(a);
    }
  }

  /** Sidebar "Navigation": parent index of the current directory and the documentation root. */
  async renderRelated() {
    const { path } = this.opts;
    if (!path.startsWith("Documentation/")) return;
    const box = this.root.querySelector(".kdoc-related");
    const dir = path.slice(0, path.lastIndexOf("/"));
    const links = [];
    try {
      const listing = await api.getDirectoryTree(this.opts.version, dir, 1);
      const names = new Set((listing.entries || []).map((e) => e.name));
      const base = path.split("/").pop();
      for (const idx of ["index.rst", "00-INDEX"]) {
        if (names.has(idx) && base !== idx) {
          links.push(`<li><a href="#" data-doc="${escapeHtml(`${dir}/${idx}`)}">${escapeHtml(dir.replace(/^Documentation\/?/, "") || "Documentation")} index</a></li>`);
          break;
        }
      }
    } catch {
      // Directory listing unavailable: show only the root link
    }
    if (path !== "Documentation/index.rst") {
      links.push(`<li><a href="#" data-doc="Documentation/index.rst">Documentation home</a></li>`);
    }
    if (links.length && box) box.innerHTML = `<h3>Navigation</h3><ul class="kdoc-this-page">${links.join("")}</ul>`;
  }

  bindSearch() {
    const form = this.root.querySelector(".kdoc-search-form");
    const input = this.root.querySelector(".kdoc-search-input");
    const status = this.root.querySelector(".kdoc-search-status");
    let lastQuery = "";
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (!q) {
        this.clearSearch();
        status.textContent = "";
        return;
      }
      if (q !== lastQuery) {
        lastQuery = q;
        this.highlight(q);
        this.searchIdx = -1;
      }
      if (this.searchMarks.length === 0) {
        status.textContent = "No matches on this page.";
        return;
      }
      this.searchMarks[this.searchIdx]?.classList.remove("current");
      this.searchIdx = (this.searchIdx + 1) % this.searchMarks.length;
      const mark = this.searchMarks[this.searchIdx];
      mark.classList.add("current");
      mark.scrollIntoView({ behavior: "smooth", block: "center" });
      status.textContent = `Match ${this.searchIdx + 1} of ${this.searchMarks.length} — Enter for next`;
    });
  }

  clearSearch() {
    for (const mark of this.searchMarks) {
      const parent = mark.parentNode;
      if (!parent) continue;
      parent.replaceChild(document.createTextNode(mark.textContent), mark);
      parent.normalize();
    }
    this.searchMarks = [];
  }

  highlight(query) {
    this.clearSearch();
    const body = this.root.querySelector(".kdoc-body");
    const needle = query.toLowerCase();
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.nodeValue.toLowerCase().includes(needle) && !n.parentElement.closest(".kdoc-headerlink") ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      let text = node.nodeValue;
      const frag = document.createDocumentFragment();
      let idx;
      while ((idx = text.toLowerCase().indexOf(needle)) !== -1) {
        if (idx > 0) frag.appendChild(document.createTextNode(text.slice(0, idx)));
        const mark = document.createElement("mark");
        mark.className = "kdoc-hl";
        mark.textContent = text.slice(idx, idx + needle.length);
        frag.appendChild(mark);
        this.searchMarks.push(mark);
        text = text.slice(idx + needle.length);
      }
      if (text) frag.appendChild(document.createTextNode(text));
      node.parentNode.replaceChild(frag, node);
    }
  }

  /** Highlight the sidebar entry of the section currently at the top of the viewport. */
  bindScrollSpy() {
    const headings = [...this.root.querySelectorAll(".kdoc-body h1[id], .kdoc-body h2[id], .kdoc-body h3[id], .kdoc-body h4[id]")];
    const tocLinks = new Map([...this.root.querySelectorAll(".kdoc-localtoc a[data-toc-id]")].map((a) => [a.dataset.tocId, a]));
    if (headings.length === 0) return;
    let active = null;
    const update = () => {
      this._scrollRaf = null;
      const top = this.scroller.getBoundingClientRect().top + 24;
      let current = headings[0];
      for (const h of headings) {
        if (h.getBoundingClientRect().top <= top) current = h;
        else break;
      }
      const link = tocLinks.get(current.id);
      if (link !== active) {
        active?.classList.remove("current");
        link?.classList.add("current");
        active = link;
      }
    };
    this.scroller.addEventListener("scroll", () => {
      if (!this._scrollRaf) this._scrollRaf = requestAnimationFrame(update);
    }, { passive: true });
    update();
  }
}
