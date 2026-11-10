/**
 * html_renderer.js - HTML (.html, .htm, .xhtml, .shtml) → Beautified HTML renderer for KernelInfo-Parser.
 *
 * Implements parsing and presentation of HTML documents found across the Linux kernel repository
 * (e.g. Documentation/sound/alsa/seq_oss.html, Documentation/video4linux/API.html).
 *
 * Capabilities:
 * - Security hardening: strips active scripts (<script>, <noscript>), embedded objects (<object>, <embed>,
 *   <applet>, <iframe>, <frame>, <frameset>), unauthenticated styles (<style>, <link>), inline event handlers
 *   (onload=, onclick=, etc.), and javascript: / data: URIs in strict compliance with CSP.
 * - Structural hierarchy & TOC: extracts headings (<h1> through <h6>) with slugified IDs, permalink ¶ symbols,
 *   and nested local Table of Contents (TOC) matching DocPage sidebar. Filters pseudo-headings containing only <hr>.
 * - Legacy Netscape/HTML4 normalization: translates <center> to .kdoc-align-center, <tt> to .docutils.literal,
 *   normalizes <hr> to .docutils, and ensures legacy <a name="..."> anchors are addressable via id="...".
 * - Code syntax highlighting: converts <pre> and <pre><code> listings into styled blocks via AstOverlay and highlightCode.
 * - Table enhancement: ensures all <table> elements receive .docutils classes for IDE dark theme styling.
 * - Link resolution: maps document links (.rst, .md, .tmpl, .html) to data-doc, code files to data-src,
 *   in-page anchors to internal references, and web URLs to external new tabs.
 * - Image resolution: hydrates relative images with data-raw for git tree fetching and styles inline badges.
 * - Kernel automarkup: links CONFIG_* macros and kernel functions func() within prose text nodes.
 */

import { escapeHtml, slugify } from "./rst_renderer.js";
import { highlightCode } from "./md_renderer.js";

/**
 * Decode common HTML entities from raw source.
 *
 * @param {string} str
 * @returns {string}
 */
export function unescapeHtml(str) {
  return String(str || "")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)))
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCharCode(parseInt(h, 16)));
}

/**
 * Check if the given repository path is an HTML document.
 *
 * @param {string} path
 * @returns {boolean}
 */
export function isHtmlPath(path) {
  if (!path) return false;
  return /\.(html|htm|xhtml|shtml)$/i.test(path);
}

/**
 * Detect if text source looks like an HTML document when extension is ambiguous.
 *
 * @param {string} source
 * @returns {boolean}
 */
export function looksLikeHtml(source) {
  const sample = String(source || "").slice(0, 4000);
  return /<!DOCTYPE\s+html\b/i.test(sample) ||
         /<html\b[\s\S]*?<body\b/i.test(sample) ||
         /<html\b[\s\S]*?<head\b/i.test(sample) ||
         /<head\b[\s\S]*?<title\b/i.test(sample) ||
         /<body\b[\s\S]*?<\/body>/i.test(sample);
}

/**
 * Extract document title from HTML source (<title> or first heading).
 *
 * @param {string} source
 * @returns {string}
 */
export function extractHtmlTitle(source) {
  if (!source) return "";
  const s = String(source);
  const titleMatch = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(s);
  if (titleMatch) {
    const raw = titleMatch[1].replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
    if (raw) return unescapeHtml(raw);
  }
  const headingMatch = /<h[12][^>]*>([\s\S]*?)<\/h[12]>/gi;
  let m;
  while ((m = headingMatch.exec(s)) !== null) {
    const plain = m[1].replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
    if (plain) return unescapeHtml(plain);
  }
  return "";
}

/**
 * Resolve a relative file or document target against the current document's path.
 *
 * @param {string} target
 * @param {string} currentPath
 * @returns {string}
 */
function resolveRelativePath(target, currentPath) {
  let t = String(target || "").trim();
  if (!t || t.startsWith("/") || /^[a-z]+:/i.test(t)) return t;

  const [targetPath, hash] = t.split("#");
  const dir = currentPath.includes("/") ? currentPath.slice(0, currentPath.lastIndexOf("/")) : "";
  const parts = (dir ? dir.split("/") : []).concat(targetPath.split("/"));
  const out = [];

  for (const p of parts) {
    if (p === "..") {
      out.pop();
    } else if (p && p !== ".") {
      out.push(p);
    }
  }

  const res = out.join("/");
  return hash ? `${res}#${hash}` : res;
}

/**
 * Repair legacy unclosed <pre> tags (common in early kernel HTML docs).
 *
 * @param {string} str
 * @returns {string}
 */
function fixPreTags(str) {
  return str.replace(/<pre\b([^>]*)>([\s\S]*?)(?=<pre\b|<h[1-6]\b|<\/body>|$)/gi, (match, attrs, content) => {
    if (content.toLowerCase().includes("</pre>")) {
      return match;
    }
    const pIdx = content.search(/<p\b/i);
    if (pIdx !== -1) {
      return `<pre${attrs}>${content.slice(0, pIdx)}</pre>${content.slice(pIdx)}`;
    }
    return `<pre${attrs}>${content}</pre>`;
  });
}

/**
 * Safely append a CSS class or set of classes to an existing HTML attribute string.
 *
 * @param {string} attrsStr
 * @param {string} newClass
 * @returns {string}
 */
function addClass(attrsStr, newClass) {
  let s = String(attrsStr || "").trim();
  const classesToAdd = newClass.split(/\s+/).filter(Boolean);
  if (/\bclass\s*=\s*(["'])([^"']*)\1/i.test(s)) {
    return s.replace(/\bclass\s*=\s*(["'])([^"']*)\1/i, (m, q, cls) => {
      const existing = cls.split(/\s+/).filter(Boolean);
      for (const c of classesToAdd) {
        if (!existing.includes(c)) existing.push(c);
      }
      return `class=${q}${existing.join(" ")}${q}`;
    });
  }
  return s ? `class="${newClass}" ${s}` : `class="${newClass}"`;
}

/**
 * Heuristically detect language of code snippet when no explicit class is specified.
 *
 * @param {string} code
 * @returns {string}
 */
function detectLanguage(code) {
  const c = String(code || "");
  if (/#include\s+[<"]|\b(?:int|void|char|static|struct|enum|typedef|const|unsigned|volatile|sizeof)\b/.test(c)) {
    return "c";
  }
  if (/^#!|\b(?:modprobe|insmod|rmmod|make|cat|grep|echo|cd|mkdir)\b|^\s*[\$#]\s+/m.test(c)) {
    return "sh";
  }
  if (/^import\s+\w+|^from\s+\w+\s+import|\bdef\s+\w+\(/m.test(c)) {
    return "py";
  }
  if (/\bfn\s+\w+\(|\buse\s+(?:core|alloc|std|kernel)::|\blet\s+(?:mut\s+)?\w+/m.test(c)) {
    return "rust";
  }
  if (/(\+-+\+|\| {3,}\||\+===\+)/.test(c)) {
    return "text";
  }
  return "c";
}

export class HtmlRenderer {
  /**
   * @param {string} source - Raw HTML text
   * @param {{ path?: string }} [options={}]
   */
  constructor(source, options = {}) {
    this.source = String(source || "");
    this.path = options.path || "";
    this.toc = [];
    this.ids = new Set();
    this.title = "";
  }

  /**
   * Produce a collision-free section ID slug.
   *
   * @param {string} base
   * @returns {string}
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
   * Parse and render HTML source into beautified, secure HTML.
   *
   * @returns {{ html: string, toc: Array<{level:number,id:string,title:string}>, title: string }}
   */
  render() {
    let html = this.source;

    // 1. Extract document title
    this.title = extractHtmlTitle(html);

    // 2. Fix unclosed <pre> blocks in legacy HTML
    html = fixPreTags(html);

    // 3. Extract <body> content if document is a full HTML page
    const bodyMatch = /<body\b[^>]*>([\s\S]*?)<\/body>/i.exec(html);
    if (bodyMatch) {
      html = bodyMatch[1];
    } else {
      // Strip <!DOCTYPE ...>, <html>, <head>...</head>
      html = html.replace(/<!DOCTYPE[^>]*>/gi, "");
      html = html.replace(/<\/?html\b[^>]*>/gi, "");
      html = html.replace(/<head\b[^>]*>[\s\S]*?<\/head>/gi, "");
    }

    // 4. Security sanitization: strip active scripts, dangerous objects, inline event handlers
    html = html.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, "");
    html = html.replace(/<noscript\b[^<]*(?:(?!<\/noscript>)<[^<]*)*<\/noscript>/gi, "");
    html = html.replace(/<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>/gi, "");
    html = html.replace(/<(?:link|meta|base)\b[^>]*>/gi, "");
    html = html.replace(/<(?:object|embed|applet|iframe|frame|frameset)\b[^>]*>([\s\S]*?<\/(?:object|embed|applet|iframe|frame|frameset)>)?/gi, "");

    // Strip inline event handlers (onload, onclick, etc.), including slash-separated (<div/onclick=...) and unquoted
    html = html.replace(/[\s/]on[a-z0-9_-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "");

    // Sanitize javascript: and vbscript: URIs in href and src (quoted or unquoted, with optional slash separation)
    html = html.replace(/([\s/]href\s*=\s*)(?:"\s*(?:javascript|vbscript):[^"]*"|'\s*(?:javascript|vbscript):[^']*'|(?:javascript|vbscript):[^\s>]*)/gi, '$1"#"');
    html = html.replace(/([\s/]src\s*=\s*)(?:"\s*(?:javascript|vbscript):[^"]*"|'\s*(?:javascript|vbscript):[^']*'|(?:javascript|vbscript):[^\s>]*)/gi, '$1""');

    // Sanitize data: URIs in href (all data: links neutralized to "#", handling parametric/unquoted/slash-separated)
    html = html.replace(/([\s/]href\s*=\s*)(?:"\s*data:[^"]*"|'\s*data:[^']*'|data:[^\s>]*)/gi, '$1"#"');

    // Sanitize dangerous/executable data: URIs in src (e.g. text/html, javascript, svg+xml, xhtml, with any parameters/charsets)
    html = html.replace(/([\s/]src\s*=\s*)(?:"\s*data:\s*(?:text\/(?:html|xml)|application\/(?:javascript|x-javascript|xhtml\+xml|xml)|image\/svg\+xml)[^"]*"|'\s*data:\s*(?:text\/(?:html|xml)|application\/(?:javascript|x-javascript|xhtml\+xml|xml)|image\/svg\+xml)[^']*'|data:\s*(?:text\/(?:html|xml)|application\/(?:javascript|x-javascript|xhtml\+xml|xml)|image\/svg\+xml)[^\s>]*)/gi, '$1""');

    // 5. Transform legacy presentational elements into clean HTML5 / CSS
    html = html.replace(/<center\b[^>]*>([\s\S]*?)<\/center>/gi, '<div class="kdoc-align-center">$1</div>');
    html = html.replace(/<tt\b[^>]*>([\s\S]*?)<\/tt>/gi, '<code class="docutils literal notranslate">$1</code>');
    html = html.replace(/<hr\b[^>]*>/gi, '<hr class="docutils" />');

    // 6. Ensure legacy <a name="..."> anchors are addressable via id="..." for smooth scrolling
    html = html.replace(/<a\b([^>]*)>/gi, (tag, attrs) => {
      const nameMatch = /\bname\s*=\s*(["'])([^"']+)\1/i.exec(attrs);
      const idMatch = /\bid\s*=\s*(["'])([^"']+)\1/i.exec(attrs);
      if (nameMatch && !idMatch) {
        const idVal = nameMatch[2];
        this.ids.add(idVal);
        return `<a id="${escapeHtml(idVal)}"${attrs}>`;
      }
      if (idMatch) {
        this.ids.add(idMatch[2]);
      }
      return tag;
    });

    // 7. Transform headings and collect TOC
    html = html.replace(/<h([1-6])([^>]*)>([\s\S]*?)<\/h\1>/gi, (match, levelStr, rawAttrs, inner) => {
      const level = parseInt(levelStr, 10);
      const plainText = inner.replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();

      // Skip pseudo-headings that only contain <hr> or whitespace
      if (!plainText) {
        return inner.toLowerCase().includes("<hr") ? '<hr class="docutils" />' : "";
      }

      if (!this.title && level <= 2) {
        this.title = unescapeHtml(plainText);
      }

      const idMatch = /\bid\s*=\s*(["'])([^"']+)\1/i.exec(rawAttrs);
      const innerNameMatch = /<a\b[^>]*\b(?:name|id)\s*=\s*(["'])([^"']+)\1/i.exec(inner);
      let id = "";
      if (idMatch) {
        id = idMatch[2];
        this.ids.add(id);
      } else if (innerNameMatch) {
        id = innerNameMatch[2];
        this.ids.add(id);
      } else {
        id = this.uniqueId(plainText);
      }

      this.toc.push({
        level,
        id,
        title: escapeHtml(unescapeHtml(plainText))
      });

      const cleanAttrs = rawAttrs.replace(/\s*\bid\s*=\s*(["'])[^"']*\1/gi, "");
      return `<h${level} id="${escapeHtml(id)}"${cleanAttrs}>${inner}<a class="kdoc-headerlink" href="#${escapeHtml(id)}" title="Permalink to this headline">¶</a></h${level}>`;
    });

    // 8. Syntax highlight <pre> and <pre><code> blocks
    html = html.replace(/<pre\b([^>]*)>([\s\S]*?)<\/pre>/gi, (match, preAttrs, preInner) => {
      let lang = "";
      let rawCode = preInner;

      const codeMatch = /<code\b([^>]*)>([\s\S]*?)<\/code>/i.exec(preInner);
      if (codeMatch) {
        const codeAttrs = codeMatch[1];
        rawCode = codeMatch[2];
        const langM = /class=["'][^"']*(?:lang(?:uage)?-|brush:\s*)([\w+-]+)[^"']*["']/i.exec(codeAttrs);
        if (langM) lang = langM[1];
      }

      if (!lang) {
        const preLangM = /class=["'][^"']*(?:lang(?:uage)?-|brush:\s*)([\w+-]+)[^"']*["']/i.exec(preAttrs);
        if (preLangM) lang = preLangM[1];
      }

      const unescaped = unescapeHtml(rawCode);

      if (!lang) {
        lang = detectLanguage(unescaped);
      }

      const highlighted = highlightCode(unescaped, lang);
      return `<div class="highlight-${escapeHtml(lang)} notranslate kdoc-code-block"><pre class="literal-block">${highlighted}</pre></div>`;
    });

    // 9. Ensure tables have .docutils class
    html = html.replace(/<table\b([^>]*)>/gi, (match, attrs) => {
      if (/\bclass\s*=\s*(["'])([^"']*)\1/i.test(attrs)) {
        return match.replace(/\bclass\s*=\s*(["'])([^"']*)\1/i, (m, q, cls) => {
          if (cls.includes("docutils")) return `class=${q}${cls}${q}`;
          return `class=${q}${cls} docutils${q}`;
        });
      }
      return `<table class="docutils"${attrs}>`;
    });

    // 10. Transform links (handling quoted, unquoted, and slash-separated href attributes)
    html = html.replace(/<a\b([^>]*?)[\s/]href\s*=\s*(?:(["'])([\s\S]*?)\2|([^\s>]+))([^>]*)>([\s\S]*?)<\/a>/gi, (match, before, quote, quotedHref, unquotedHref, after, linkText) => {
      const rawHref = quote ? quotedHref : unquotedHref;
      const href = (rawHref || "").trim();
      const attrs = `${before} ${after}`.replace(/\s+/g, " ").trim();

      // Don't re-transform internal headerlinks
      if (/\bkdoc-headerlink\b/.test(attrs)) {
        return match;
      }

      if (href.startsWith("#")) {
        const fullAttrs = addClass(attrs, "reference internal");
        return `<a ${fullAttrs} href="${escapeHtml(href)}">${linkText}</a>`;
      }
      if (/^[a-z]+:\/\//i.test(href) || href.startsWith("mailto:") || href.startsWith("ftp:")) {
        let fullAttrs = addClass(attrs, "reference external");
        if (!/\btarget\s*=/i.test(fullAttrs)) fullAttrs += ' target="_blank"';
        if (!/\brel\s*=/i.test(fullAttrs)) fullAttrs += ' rel="noopener noreferrer"';
        return `<a ${fullAttrs} href="${escapeHtml(href)}">${linkText}</a>`;
      }

      const resolved = resolveRelativePath(href, this.path);
      const [cleanPath, hash] = resolved.split("#");

      if (cleanPath === this.path) {
        const targetHash = hash ? `#${hash}` : "#";
        const fullAttrs = addClass(attrs, "reference internal");
        return `<a ${fullAttrs} href="${escapeHtml(targetHash)}">${linkText}</a>`;
      }

      if (/\.(rst|txt|md|markdown|tmpl|html|htm|xhtml|shtml)$/i.test(cleanPath)) {
        const fullAttrs = addClass(attrs, "reference internal");
        return `<a ${fullAttrs} data-doc="${escapeHtml(resolved)}" href="#">${linkText}</a>`;
      }

      const fullAttrs = addClass(attrs, "reference internal");
      return `<a ${fullAttrs} data-src="${escapeHtml(resolved)}" href="#">${linkText}</a>`;
    });

    // 11. Transform images (handling quoted, unquoted, and slash-separated src attributes)
    html = html.replace(/<img\b([^>]*?)[\s/]src\s*=\s*(?:(["'])([\s\S]*?)\2|([^\s>]+))([^>]*)>/gi, (match, before, quote, quotedSrc, unquotedSrc, after) => {
      const rawSrc = quote ? quotedSrc : unquotedSrc;
      const src = (rawSrc || "").trim();
      const attrs = `${before} ${after}`.replace(/\s+/g, " ").trim();
      const isBadge = /(badge|shields\.io|crates\.io\/crates\/[^\/]+\.svg|deps\.rs)/i.test(src);
      const badgeClass = isBadge ? " kdoc-badge" : "";

      if (/^[a-z]+:\/\//i.test(src)) {
        return `<img class="kdoc-img${badgeClass}" src="${escapeHtml(src)}"${attrs ? " " + attrs : ""} />`;
      }
      const resolved = resolveRelativePath(src, this.path);
      return `<img class="kdoc-img${badgeClass}" data-raw="${escapeHtml(resolved)}"${attrs ? " " + attrs : ""} />`;
    });

    // 12. Kernel Automarkup for CONFIG_* macros and func() calls in text nodes
    let linkDepth = 0;
    let codeDepth = 0;

    html = html.replace(/(<[^>]+>)|([^<]+)/g, (match, tag, text) => {
      if (tag) {
        if (/^<a\b/i.test(tag)) linkDepth++;
        else if (/^<\/a>/i.test(tag)) linkDepth = Math.max(0, linkDepth - 1);
        else if (/^<(?:pre|code|kbd|samp)\b/i.test(tag)) codeDepth++;
        else if (/^<\/(?:pre|code|kbd|samp)>/i.test(tag)) codeDepth = Math.max(0, codeDepth - 1);
        return tag;
      }

      if (linkDepth > 0 || codeDepth > 0) {
        return text;
      }

      // Automarkup CONFIG_* symbols
      let marked = text.replace(/\bCONFIG_[A-Z0-9_]+\b/g, (sym) => {
        const configName = sym.replace(/^CONFIG_/, "");
        return `<a class="reference internal kdoc-kconfig" data-kconfig="${escapeHtml(configName)}" href="#"><code class="xref kconfig">${escapeHtml(sym)}</code></a>`;
      });

      // Automarkup function calls
      marked = marked.replace(/\b([a-zA-Z_][a-zA-Z0-9_]{2,})\(\)/g, (funcCall, funcName) => {
        if (/^(if|for|while|switch|sizeof|return|defined)$/.test(funcName)) return funcCall;
        return `<a class="reference internal" data-symbol="${escapeHtml(funcName)}" href="#"><code class="xref c-func">${escapeHtml(funcCall)}</code></a>`;
      });

      return marked;
    });

    return {
      html,
      toc: this.toc,
      title: this.title || this.path.split("/").pop() || "HTML Documentation"
    };
  }
}

/**
 * Render an HTML document to beautified HTML with TOC.
 *
 * @param {string} source - Raw HTML text
 * @param {string} path - Document repository path
 * @returns {{ html: string, toc: Array<{level:number,id:string,title:string}>, title: string }}
 */
export function renderHtmlDocument(source, path) {
  return new HtmlRenderer(source, { path }).render();
}

/**
 * Render an HTML fragment to beautified HTML.
 *
 * @param {string} source - HTML fragment
 * @param {string} path - Context path
 * @returns {string}
 */
export function renderHtmlFragment(source, path = "") {
  return new HtmlRenderer(source, { path }).render().html;
}
