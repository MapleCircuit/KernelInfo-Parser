/**
 * tmpl_renderer.js - DocBook XML Template (.tmpl) → Beautified HTML renderer for KernelInfo-Parser.
 *
 * Implements parsing and presentation of Linux kernel DocBook XML templates (.tmpl),
 * which served as the authoritative documentation system prior to the Sphinx/rST migration.
 *
 * Capabilities:
 * - Structural hierarchy: <book>, <chapter>, <preface>, <appendix>, <sect1> through <sect4>, <section>.
 * - Nested Table of Contents (TOC) with permalinks (¶) and section anchors matching DocPage sidebar.
 * - Kernel-Doc directive extraction and card generation:
 *     !E<file>               Exported symbols
 *     !I<file>               Internal symbols
 *     !F<file> <functions>   Specific functions
 *     !P<file> <section>     DOC: section comments
 *     !C<file>               All comments
 *   Emitted as <div class="kdoc-kerneldoc"> blocks for live client-side hydration via DocPage.hydrateKernelDoc().
 * - Book metadata: <bookinfo>, <title>, <authorgroup>, <author>, <copyright>, <legalnotice>, <abstract>.
 * - Program listings: <programlisting>, <screen> with multi-language code highlighting via AstOverlay.
 * - Lists & Tables: <itemizedlist>, <orderedlist>, <variablelist>, <table>, <informaltable>, <tgroup>.
 * - Inline semantics & Automarkup: <function>, <structname>, <constant>, <varname>, <filename>, <emphasis>,
 *   <ulink>, <link>, alongside CONFIG_* macros, func() calls, and struct declarations.
 */

import { escapeHtml, slugify } from "./rst_renderer.js";
import { highlightCode } from "./md_renderer.js";

/**
 * Check if the given repository path is a DocBook XML template.
 *
 * @param {string} path
 * @returns {boolean}
 */
export function isTmplPath(path) {
  if (!path) return false;
  return /\.tmpl$/i.test(path);
}

/**
 * Detect if text source looks like a DocBook XML template.
 *
 * @param {string} source
 * @returns {boolean}
 */
export function looksLikeTmpl(source) {
  const sample = String(source || "").slice(0, 4000);
  return /<!DOCTYPE\s+book\b/i.test(sample) || /<book\b[\s\S]*?<chapter\b/i.test(sample) || /<book\b[\s\S]*?<bookinfo\b/i.test(sample);
}

/**
 * Extract document title from DocBook XML template source.
 *
 * @param {string} source
 * @returns {string}
 */
export function extractTmplTitle(source) {
  const sample = String(source || "").slice(0, 6000);
  const m = /<bookinfo>[\s\S]*?<title>(.*?)<\/title>/i.exec(sample) || /<book[^>]*>[\s\S]*?<title>(.*?)<\/title>/i.exec(sample);
  if (m) {
    return m[1].replace(/<[^>]+>/g, "").trim();
  }
  return "";
}

export class TmplRenderer {
  /**
   * @param {string} source - Raw DocBook XML text
   * @param {{ path?: string }} [options={}]
   */
  constructor(source, options = {}) {
    this.source = String(source || "");
    this.path = options.path || "";
    this.toc = []; // { level, id, title }
    this.ids = new Set();
    this.title = "";
  }

  /**
   * Generate unique heading anchor ID.
   */
  uniqueId(base) {
    let slug = slugify(base) || "section";
    let candidate = slug;
    let i = 1;
    while (this.ids.has(candidate)) {
      candidate = `${slug}-${i++}`;
    }
    this.ids.add(candidate);
    return candidate;
  }

  /**
   * Lightweight zero-dependency XML tokenizer and node tree builder.
   * Tolerant of XML entities, CDATA sections, comments, and unescaped directives.
   */
  parseXml(xml) {
    let clean = xml.replace(/<\?xml[^>]*\?>/gi, "");
    clean = clean.replace(/<!DOCTYPE[^>]*(\[[^\]]*\])?>/gi, "");
    clean = clean.replace(/<!--[\s\S]*?-->/g, "");

    const cdata = [];
    clean = clean.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, (m, c) => {
      cdata.push(c);
      return `__CDATA_${cdata.length - 1}__`;
    });

    const root = { tag: "#root", attrs: {}, children: [] };
    const stack = [root];
    const tagRe = /<(\/)?([a-zA-Z0-9_-]+)([^>]*?)(\/)?>|([^<]+)/g;
    let m;

    while ((m = tagRe.exec(clean)) !== null) {
      if (m[5] !== undefined) {
        const text = m[5].replace(/__CDATA_(\d+)__/g, (_, idx) => cdata[Number(idx)]);
        stack[stack.length - 1].children.push({ tag: "#text", text });
      } else {
        const isClose = Boolean(m[1]);
        const tagName = m[2].toLowerCase();
        const rawAttrs = m[3] || "";
        const isSelfClosing = Boolean(m[4]) || ["graphic", "img", "xref", "colspec"].includes(tagName);

        if (isClose) {
          if (stack.length > 1) {
            let idx = stack.length - 1;
            while (idx > 0 && stack[idx].tag !== tagName) idx--;
            if (idx > 0) {
              while (stack.length > idx) stack.pop();
            }
          }
        } else {
          const attrs = {};
          const attrRe = /([a-zA-Z0-9_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))/g;
          let am;
          while ((am = attrRe.exec(rawAttrs)) !== null) {
            attrs[am[1].toLowerCase()] = am[2] !== undefined ? am[2] : (am[3] !== undefined ? am[3] : am[4]);
          }
          const node = { tag: tagName, attrs, children: [] };
          stack[stack.length - 1].children.push(node);
          if (!isSelfClosing) {
            stack.push(node);
          }
        }
      }
    }
    return root;
  }

  /**
   * Render the DocBook template into HTML, TOC, and document title.
   *
   * @returns {{ html: string, toc: Array<{level:number,id:string,title:string}>, title: string }}
   */
  render() {
    const root = this.parseXml(this.source);
    const bodyHtml = this.renderNode(root, { depth: 0 });
    return {
      html: bodyHtml,
      toc: this.toc,
      title: this.title || this.path.split("/").pop() || "DocBook Documentation"
    };
  }

  renderChildren(node, ctx = {}) {
    return (node.children || []).map(c => this.renderNode(c, ctx)).join("");
  }

  extractText(node) {
    if (!node) return "";
    if (node.tag === "#text") return node.text;
    return (node.children || []).map(c => this.extractText(c)).join("");
  }

  /**
   * Translate DocBook kernel-doc directive lines (!E, !I, !F, !P, !C) into
   * hydrating blocks (<div class="kdoc-kerneldoc">) with source fallback links.
   */
  renderKernelDocDirective(line) {
    const trimmed = line.trim();
    const type = trimmed[1];
    const rest = trimmed.slice(2).trim();
    let path = "";
    let opts = {};
    let label = "API documentation extracted from source comments";

    if (type === "E") {
      path = rest;
      opts = { export: true };
      label = "Exported functions extracted from source comments";
    } else if (type === "I") {
      path = rest;
      opts = { internal: true };
      label = "Internal functions extracted from source comments";
    } else if (type === "F") {
      const parts = rest.split(/\s+/);
      path = parts[0];
      const funcs = parts.slice(1).join(" ");
      opts = { functions: funcs };
      label = `Functions: ${funcs}`;
    } else if (type === "P") {
      const firstSpace = rest.indexOf(" ");
      if (firstSpace !== -1) {
        path = rest.slice(0, firstSpace);
        const section = rest.slice(firstSpace + 1).trim();
        opts = { doc: section };
        label = `DOC section: ${section}`;
      } else {
        path = rest;
      }
    } else {
      path = rest;
    }

    const cleanPath = path.replace(/^\//, "");
    const card = `<div class="kdoc-directive-card">` +
      `<div class="kdoc-directive-head">` +
      `<span class="kdoc-directive-name">kernel-doc (!${type})</span>` +
      `<span>${escapeHtml(label)}</span>` +
      `</div>` +
      `<a class="reference internal" data-src="${escapeHtml(cleanPath)}" href="#">${escapeHtml(cleanPath)}</a>` +
      `</div>`;

    return `<div class="kdoc-kerneldoc" data-kdoc-path="${escapeHtml(cleanPath)}" data-kdoc-opts="${escapeHtml(JSON.stringify(opts))}">${card}</div>`;
  }

  /**
   * Parse inline automarkup (CONFIG_* options, func() calls, struct declarations).
   */
  formatInlineProse(text) {
    const src = String(text || "");
    if (!src) return "";
    const parts = [
      "(?<config>\\bCONFIG_[A-Z0-9_]+\\b)",
      "(?<func>\\b(?<funcName>[A-Za-z_][A-Za-z0-9_]*)\\(\\))",
      "(?<cstruct>\\b(?<cKind>struct|union|enum|typedef)\\s+(?<cName>[A-Za-z_][A-Za-z0-9_]*)\\b)"
    ];
    const re = new RegExp(parts.join("|"), "g");
    let out = "";
    let last = 0;
    let m;
    while ((m = re.exec(src)) !== null) {
      const g = m.groups;
      out += escapeHtml(src.slice(last, m.index));
      if (g.config !== undefined) {
        const sym = m[0];
        out += `<a class="reference internal kdoc-kconfig" data-kconfig="${escapeHtml(sym.replace(/^CONFIG_/, ""))}" href="#"><code class="xref kconfig">${escapeHtml(sym)}</code></a>`;
      } else if (g.func !== undefined) {
        out += `<a class="reference internal kdoc-symbol" data-symbol="${escapeHtml(g.funcName)}" href="#"><code>${escapeHtml(g.funcName)}()</code></a>`;
      } else if (g.cstruct !== undefined) {
        out += `<a class="reference internal kdoc-symbol" data-symbol="${escapeHtml(g.cName)}" href="#"><code>${escapeHtml(g.cKind)} ${escapeHtml(g.cName)}</code></a>`;
      }
      last = re.lastIndex;
    }
    if (last < src.length) out += escapeHtml(src.slice(last));
    return out;
  }

  /**
   * Recursive node renderer mapping DocBook AST nodes to HTML elements.
   */
  renderNode(node, ctx = {}) {
    if (!node) return "";

    if (node.tag === "#text") {
      const raw = node.text || "";
      if (ctx.rawCode) return raw;

      // Detect and handle kernel-doc directive lines (!E, !I, !F, !P, !C)
      if (/^![EIFPC]/m.test(raw)) {
        const lines = raw.split(/\r?\n/);
        const out = [];
        let proseLines = [];
        for (const line of lines) {
          if (/^![EIFPC]/.test(line.trim())) {
            if (proseLines.join("").trim()) {
              out.push(`<p>${this.formatInlineProse(proseLines.join(" ").trim())}</p>`);
              proseLines = [];
            }
            out.push(this.renderKernelDocDirective(line));
          } else if (line.trim()) {
            proseLines.push(line);
          }
        }
        if (proseLines.join("").trim()) {
          out.push(`<p>${this.formatInlineProse(proseLines.join(" ").trim())}</p>`);
        }
        return out.join("\n");
      }

      if (ctx.inline) {
        return this.formatInlineProse(raw);
      }
      if (!raw.trim()) return "";
      return this.formatInlineProse(raw);
    }

    switch (node.tag) {
      case "#root":
        return this.renderChildren(node, ctx);

      case "book": {
        const bookId = node.attrs.id || "book";
        return `<div class="kdoc-book" id="${escapeHtml(bookId)}">${this.renderChildren(node, ctx)}</div>`;
      }

      case "bookinfo": {
        const titleNode = node.children.find(c => c.tag === "title");
        let html = "";
        if (titleNode) {
          const titleText = this.extractText(titleNode).trim();
          this.title = titleText;
          html += `<h1 class="kdoc-book-title">${escapeHtml(titleText)}</h1>\n`;
        }
        html += this.renderChildren(node, { ...ctx, handledTitle: true });
        return html;
      }

      case "title": {
        if (ctx.handledTitle) return "";
        return `<strong>${this.renderChildren(node, { inline: true })}</strong>`;
      }

      case "subtitle":
        return `<p class="kdoc-subtitle">${this.renderChildren(node, { inline: true })}</p>`;

      case "legalnotice": {
        const content = this.renderChildren(node, ctx);
        return `<div class="admonition note legalnotice"><p class="admonition-title">Legal Notice</p>${content}</div>`;
      }

      case "authorgroup": {
        return `<div class="kdoc-meta-authors">${this.renderChildren(node, ctx)}</div>`;
      }

      case "author": {
        const first = this.extractText(node.children.find(c => c.tag === "firstname")).trim();
        const sur = this.extractText(node.children.find(c => c.tag === "surname")).trim();
        const email = this.extractText(
          node.children.find(c => c.tag === "email") ||
          (node.children.find(c => c.tag === "affiliation")?.children.find(c => c.tag === "address")?.children.find(c => c.tag === "email"))
        ).trim();
        const name = [first, sur].filter(Boolean).join(" ");
        const emailHtml = email ? ` &lt;<a class="reference external" href="mailto:${escapeHtml(email)}">${escapeHtml(email)}</a>&gt;` : "";
        return `<div class="kdoc-author">Author: <strong>${escapeHtml(name)}</strong>${emailHtml}</div>`;
      }

      case "copyright": {
        const year = this.extractText(node.children.find(c => c.tag === "year")).trim();
        const holder = this.extractText(node.children.find(c => c.tag === "holder")).trim();
        return `<div class="kdoc-copyright"><p>Copyright &copy; ${escapeHtml(year)} ${escapeHtml(holder)}</p></div>`;
      }

      case "abstract": {
        return `<div class="topic abstract"><p class="topic-title">Abstract</p>${this.renderChildren(node, ctx)}</div>`;
      }

      case "toc":
        // DocPage renders left sidebar TOC natively; omit redundant inline <toc> tag
        return "";

      case "chapter":
      case "preface":
      case "appendix":
      case "part": {
        const titleNode = node.children.find(c => c.tag === "title");
        const titleText = titleNode ? this.extractText(titleNode).trim() : (node.tag.charAt(0).toUpperCase() + node.tag.slice(1));
        const id = node.attrs.id || this.uniqueId(titleText);
        this.toc.push({ level: 1, id, title: escapeHtml(titleText) });

        const heading = `<h2 id="${escapeHtml(id)}">${escapeHtml(titleText)}<a class="kdoc-headerlink" href="#${escapeHtml(id)}" title="Permalink to this headline">¶</a></h2>`;
        const children = this.renderChildren(node, { ...ctx, handledTitle: true });
        return `<section class="kdoc-${node.tag}">\n${heading}\n${children}\n</section>`;
      }

      case "sect1":
      case "sect2":
      case "sect3":
      case "sect4":
      case "section": {
        let level = 2;
        if (node.tag === "sect2") level = 3;
        else if (node.tag === "sect3") level = 4;
        else if (node.tag === "sect4") level = 5;
        else if (node.tag === "section") level = (ctx.sectionLevel || 2);

        const hTag = `h${Math.min(level + 1, 6)}`;
        const titleNode = node.children.find(c => c.tag === "title");
        const titleText = titleNode ? this.extractText(titleNode).trim() : "Section";
        const id = node.attrs.id || this.uniqueId(titleText);
        this.toc.push({ level, id, title: escapeHtml(titleText) });

        const heading = `<${hTag} id="${escapeHtml(id)}">${escapeHtml(titleText)}<a class="kdoc-headerlink" href="#${escapeHtml(id)}" title="Permalink to this headline">¶</a></${hTag}>`;
        const children = this.renderChildren(node, { ...ctx, handledTitle: true, sectionLevel: level + 1 });
        return `<section class="kdoc-section">\n${heading}\n${children}\n</section>`;
      }

      case "para":
        return `<p>${this.renderChildren(node, { ...ctx, inline: true })}</p>`;

      case "programlisting":
      case "screen": {
        const rawCode = this.renderChildren(node, { ...ctx, rawCode: true });
        const lang = node.attrs.language || "c";
        const highlighted = highlightCode(rawCode, lang);
        return `<div class="highlight-${escapeHtml(lang)} notranslate kdoc-code-block"><pre class="literal-block">${highlighted}</pre></div>`;
      }

      case "itemizedlist":
        return `<ul class="docutils">${this.renderChildren(node, ctx)}</ul>`;

      case "orderedlist":
        return `<ol class="docutils">${this.renderChildren(node, ctx)}</ol>`;

      case "listitem":
        return `<li>${this.renderChildren(node, ctx)}</li>`;

      case "variablelist":
        return `<dl class="docutils">${this.renderChildren(node, ctx)}</dl>`;

      case "varlistentry": {
        const terms = node.children.filter(c => c.tag === "term").map(t => `<dt>${this.renderChildren(t, { inline: true })}</dt>`).join("");
        const items = node.children.filter(c => c.tag === "listitem").map(l => `<dd>${this.renderChildren(l, ctx)}</dd>`).join("");
        return `${terms}${items}`;
      }

      case "term":
        return `<dt>${this.renderChildren(node, { inline: true })}</dt>`;

      case "table":
      case "informaltable": {
        const titleNode = node.children.find(c => c.tag === "title");
        const titleHtml = titleNode ? `<caption>${escapeHtml(this.extractText(titleNode).trim())}</caption>` : "";
        const tgroup = node.children.find(c => c.tag === "tgroup") || node;
        const thead = tgroup.children.find(c => c.tag === "thead");
        const tbody = tgroup.children.find(c => c.tag === "tbody") || tgroup;

        const renderRows = (container, cellTag) => {
          return (container.children || [])
            .filter(r => r.tag === "row")
            .map(row => {
              const cells = (row.children || [])
                .filter(e => e.tag === "entry")
                .map(entry => `<${cellTag}>${this.renderChildren(entry, { inline: true })}</${cellTag}>`)
                .join("");
              return `<tr>${cells}</tr>`;
            }).join("");
        };

        const theadHtml = thead ? `<thead>${renderRows(thead, "th")}</thead>` : "";
        const tbodyHtml = `<tbody>${renderRows(tbody, "td")}</tbody>`;
        return `<table class="docutils">${titleHtml}${theadHtml}${tbodyHtml}</table>`;
      }

      case "example":
      case "informalexample": {
        const titleNode = node.children.find(c => c.tag === "title");
        const titleText = titleNode ? this.extractText(titleNode).trim() : "Example";
        const content = this.renderChildren(node, { ...ctx, handledTitle: true });
        return `<div class="topic example"><p class="topic-title">${escapeHtml(titleText)}</p>${content}</div>`;
      }

      case "note":
      case "warning":
      case "caution":
      case "important":
      case "tip": {
        const type = node.tag;
        const cap = type.charAt(0).toUpperCase() + type.slice(1);
        return `<div class="admonition ${type}"><p class="admonition-title">${cap}</p>${this.renderChildren(node, ctx)}</div>`;
      }

      case "blockquote":
        return `<blockquote>${this.renderChildren(node, ctx)}</blockquote>`;

      case "function": {
        const rawName = this.extractText(node).trim();
        const cleanName = rawName.replace(/\(\)$/, "");
        return `<a class="reference internal kdoc-symbol" data-symbol="${escapeHtml(cleanName)}" href="#"><code>${escapeHtml(rawName)}</code></a>`;
      }

      case "structname": {
        const rawName = this.extractText(node).trim();
        const cleanName = rawName.replace(/^struct\s+/, "");
        return `<a class="reference internal kdoc-symbol" data-symbol="${escapeHtml(cleanName)}" href="#"><code>struct ${escapeHtml(cleanName)}</code></a>`;
      }

      case "constant":
      case "varname":
      case "parameter":
      case "structfield":
      case "type":
      case "returnvalue":
      case "errorname":
      case "symbol":
      case "literal":
      case "code":
        return `<code class="docutils literal notranslate">${escapeHtml(this.extractText(node).trim())}</code>`;

      case "filename": {
        const p = this.extractText(node).trim();
        return `<a class="reference internal" data-src="${escapeHtml(p)}" href="#"><code>${escapeHtml(p)}</code></a>`;
      }

      case "emphasis": {
        const isBold = (node.attrs.role || "").toLowerCase() === "bold";
        const tag = isBold ? "strong" : "em";
        return `<${tag}>${this.renderChildren(node, { inline: true })}</${tag}>`;
      }

      case "quote":
        return `&ldquo;${this.renderChildren(node, { inline: true })}&rdquo;`;

      case "ulink": {
        const url = node.attrs.url || "#";
        const text = this.renderChildren(node, { inline: true }) || url;
        return `<a class="reference external" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${text}</a>`;
      }

      case "link": {
        const linkend = node.attrs.linkend || "";
        const text = this.renderChildren(node, { inline: true }) || linkend;
        return `<a class="reference internal" href="#${escapeHtml(linkend)}">${text}</a>`;
      }

      case "email": {
        const email = this.extractText(node).trim();
        return `<a class="reference external" href="mailto:${escapeHtml(email)}">${escapeHtml(email)}</a>`;
      }

      case "graphic": {
        const fileref = node.attrs.fileref || "";
        return `<figure class="kdoc-figure"><img class="kdoc-img" data-raw="${escapeHtml(fileref)}" alt="${escapeHtml(fileref)}" /></figure>`;
      }

      case "superscript":
        return `<sup>${this.renderChildren(node, { inline: true })}</sup>`;

      case "subscript":
        return `<sub>${this.renderChildren(node, { inline: true })}</sub>`;

      default:
        return this.renderChildren(node, ctx);
    }
  }
}

/**
 * Render a DocBook XML template (.tmpl) document to HTML.
 *
 * @param {string} source - Raw DocBook XML text
 * @param {string} path - Document repository path
 * @returns {{ html: string, toc: Array<{level:number,id:string,title:string}>, title: string }}
 */
export function renderTmplDocument(source, path) {
  return new TmplRenderer(source, { path }).render();
}

/**
 * Render a DocBook XML fragment to HTML.
 *
 * @param {string} source - DocBook XML fragment
 * @param {string} path - Context path
 * @returns {string}
 */
export function renderTmplFragment(source, path = "") {
  return new TmplRenderer(source, { path }).render().html;
}

