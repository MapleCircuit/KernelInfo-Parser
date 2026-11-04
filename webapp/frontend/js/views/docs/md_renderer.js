/**
 * md_renderer.js - Beautified Markdown (GFM) → HTML renderer for KernelInfo-Parser.
 *
 * Implements GitHub Flavored Markdown (GFM) parsing and rendering tailored to the kernel IDE:
 * - ATX and Setext headings with auto-slugs, `¶` permalinks, and nested Table of Contents (TOC).
 * - Fenced code blocks with multi-language syntax highlighting (Rust, C, Python, Shell, etc.).
 * - GitHub-style callouts / alerts (> [!NOTE], > [!TIP], > [!IMPORTANT], > [!WARNING], > [!CAUTION]).
 * - GFM tables with column alignment (:---, :---:, ---:).
 * - Task lists with interactive styled checkboxes (- [x], - [ ]).
 * - Inline formatting (bold, italic, strikethrough, code, links, images, kbd).
 * - Kernel automarkup: symbols, functions func(), structs struct foo, CONFIG_* Kconfig links.
 *
 * Designed to seamlessly plug into DocPage and docs.css.
 */

import { escapeHtml, slugify } from "./rst_renderer.js";
import { AstOverlay } from "../code_view/ast_overlay.js";

const OVERLAY_CACHE = new Map();

/**
 * Highlight fenced code blocks using the IDE's multi-language syntax tokenizer.
 *
 * @param {string} code - Raw code content
 * @param {string} [lang=""] - Code language identifier (e.g. "rust", "c", "python", "sh")
 * @returns {string} Escaped and syntax-colored HTML
 */
export function highlightCode(code, lang = "") {
  const cleanLang = (lang || "").trim().toLowerCase();
  if (!cleanLang || cleanLang === "text" || cleanLang === "plain") {
    return escapeHtml(code);
  }

  let sampleFile = "code.txt";
  switch (cleanLang) {
    case "c": case "h": case "i": sampleFile = "code.c"; break;
    case "cpp": case "c++": case "cc": case "cxx": sampleFile = "code.c"; break;
    case "rust": case "rs": sampleFile = "code.rs"; break;
    case "python": case "py": sampleFile = "code.py"; break;
    case "sh": case "bash": case "shell": case "zsh": sampleFile = "code.sh"; break;
    case "perl": case "pl": case "pm": sampleFile = "code.pl"; break;
    case "asm": case "assembly": case "s": sampleFile = "code.S"; break;
    case "make": case "makefile": case "mk": sampleFile = "Makefile"; break;
    case "kconfig": sampleFile = "Kconfig"; break;
    case "dts": case "dtsi": sampleFile = "code.dts"; break;
    case "yaml": case "yml": sampleFile = "code.yaml"; break;
    case "json": sampleFile = "code.json"; break;
    default: sampleFile = `code.${cleanLang}`; break;
  }

  let overlay = OVERLAY_CACHE.get(sampleFile);
  if (!overlay) {
    overlay = new AstOverlay();
    overlay.setFileInfo(null, sampleFile);
    OVERLAY_CACHE.set(sampleFile, overlay);
  }

  const lines = String(code).split(/\r?\n/);
  return lines.map((line) => overlay.renderLineHtml(null, line)).join("\n");
}

/**
 * True if path is a Markdown document.
 *
 * @param {string} path
 * @returns {boolean}
 */
export function isMarkdownPath(path) {
  if (!path) return false;
  const base = path.split("/").pop();
  return /\.(md|markdown)$/i.test(base) || /^README\.md$/i.test(base);
}

/**
 * Resolve relative document links (e.g. `../subsystem/file.md`).
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

export class MarkdownRenderer {
  /**
   * @param {string} source - Raw Markdown text
   * @param {{ path?: string }} [options={}]
   */
  constructor(source, options = {}) {
    this.source = String(source || "");
    this.path = options.path || "";
    this.toc = []; // { level, id, title }
    this.ids = new Set();
    this.title = "";
    this.refLinks = new Map();
    this.collectRefLinks(this.source.split(/\r?\n/));
  }

  /**
   * Pre-scan for Markdown reference link definitions ([label]: url "optional title").
   */
  collectRefLinks(lines) {
    for (const line of lines) {
      const m = /^\s*\[([^\]]+)\]:\s*(<[^>]+>|\S+)(?:\s+(?:"([^"]*)"|'([^']*)'|\(([^)]*)\)))?\s*$/.exec(line);
      if (m) {
        const rawLabel = m[1];
        const url = m[2].startsWith("<") && m[2].endsWith(">") ? m[2].slice(1, -1) : m[2];
        const title = m[3] || m[4] || m[5] || "";
        this.refLinks.set(this.normalizeRefKey(rawLabel), { url, title });
        this.refLinks.set(rawLabel.trim().toLowerCase(), { url, title });
      }
    }
  }

  normalizeRefKey(key) {
    return String(key || "").trim().replace(/`/g, "").replace(/\s+/g, " ").toLowerCase();
  }

  /**
   * Render Markdown text to HTML.
   *
   * @returns {{ html: string, toc: Array<{level:number,id:string,title:string}>, title: string }}
   */
  render() {
    const rawLines = this.source.split(/\r?\n/);
    const html = this.renderBlocks(rawLines);
    return {
      html,
      toc: this.toc,
      title: this.title || this.extractFirstHeading(rawLines) || this.path.split("/").pop() || "Documentation"
    };
  }

  extractFirstHeading(lines) {
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      const atx = /^#{1,6}\s+(.*)$/.exec(line);
      if (atx) return atx[1].replace(/\s+#+\s*$/, "").trim();
      if (i + 1 < lines.length && !line.startsWith("#") && line.length > 0) {
        const next = lines[i + 1].trim();
        if (/^={3,}$/.test(next) || /^-{3,}$/.test(next)) return line;
      }
    }
    return "";
  }

  /**
   * Render an array of Markdown block lines.
   */
  renderBlocks(lines) {
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // 1. Blank lines
      if (!line.trim()) {
        i++;
        continue;
      }

      // 1b. HTML comments (<!-- ... -->)
      if (line.trim().startsWith("<!--")) {
        while (i < lines.length && !lines[i].includes("-->")) {
          i++;
        }
        if (i < lines.length) i++;
        continue;
      }

      // 1c. Link reference definitions ([label]: url "optional title")
      if (/^\s*\[([^\]]+)\]:\s*(<[^>]+>|\S+)/.test(line)) {
        i++;
        continue;
      }

      // 1d. Standalone HTML anchors (<a id="..." ...></a> or <span id="...">)
      const anchorMatch = /^\s*<a\s+(?:id|name)="([^"]+)"[^>]*>\s*<\/a>\s*$/i.exec(line);
      if (anchorMatch) {
        out.push(`<span id="${escapeHtml(anchorMatch[1])}" class="kdoc-anchor"></span>`);
        i++;
        continue;
      }

      // 2. Fenced Code Blocks (``` or ~~~)
      const fenceMatch = /^\s*(```|~~~)\s*([\w+-]*)/.exec(line);
      if (fenceMatch) {
        const fence = fenceMatch[1];
        const lang = fenceMatch[2] || "";
        const codeLines = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) {
          codeLines.push(lines[i]);
          i++;
        }
        if (i < lines.length) i++; // skip closing fence

        const rawCode = codeLines.join("\n");
        const highlighted = highlightCode(rawCode, lang);
        out.push(
          `<div class="highlight-${escapeHtml(lang || "none")} notranslate kdoc-code-block">` +
          `<pre class="literal-block">${highlighted}</pre>` +
          `</div>`
        );
        continue;
      }

      // 3. ATX Headings (# Heading)
      const atxMatch = /^(#{1,6})\s+(.*)$/.exec(line);
      if (atxMatch) {
        const level = atxMatch[1].length;
        const headingRaw = atxMatch[2].replace(/\s+#+\s*$/, "").trim();
        out.push(this.formatHeading(headingRaw, level));
        i++;
        continue;
      }

      // 4. Setext Headings (Heading\n=== or Heading\n---)
      if (i + 1 < lines.length) {
        const nextLine = lines[i + 1].trim();
        if (/^={3,}$/.test(nextLine)) {
          out.push(this.formatHeading(line.trim(), 1));
          i += 2;
          continue;
        }
        if (/^-{3,}$/.test(nextLine) && !line.trim().startsWith("-") && !line.trim().startsWith("*")) {
          out.push(this.formatHeading(line.trim(), 2));
          i += 2;
          continue;
        }
      }

      // 5. Horizontal Rules (---, ***, ___)
      if (/^(\*{3,}|-{3,}|_{3,})\s*$/.test(line.trim())) {
        out.push('<hr class="docutils" />');
        i++;
        continue;
      }

      // 6. GitHub Alerts & Blockquotes (> [!NOTE], > quote)
      if (line.trim().startsWith(">")) {
        const quoteLines = [];
        while (i < lines.length && (lines[i].trim().startsWith(">") || (lines[i].trim() && !this.isBlockStarter(lines[i])))) {
          const l = lines[i].trim();
          if (l.startsWith(">")) {
            quoteLines.push(l.replace(/^>\s?/, ""));
          } else {
            quoteLines.push(l);
          }
          i++;
        }

        // Check if blockquote is a GitHub-style alert callout
        const firstLine = quoteLines[0] ? quoteLines[0].trim() : "";
        const alertMatch = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]$/i.exec(firstLine);

        if (alertMatch) {
          const kind = alertMatch[1].toLowerCase();
          const title = kind.charAt(0).toUpperCase() + kind.slice(1);
          const bodyLines = quoteLines.slice(1);
          const innerHtml = this.renderBlocks(bodyLines);
          out.push(
            `<div class="admonition ${kind}">` +
            `<p class="admonition-title">${title}</p>` +
            `${innerHtml}` +
            `</div>`
          );
        } else {
          out.push(`<blockquote>${this.renderBlocks(quoteLines)}</blockquote>`);
        }
        continue;
      }

      // 7. GFM Tables (| Header | Header |)
      if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-+:?\s*\|/.test(lines[i + 1])) {
        const tableLines = [];
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
          tableLines.push(lines[i].trim());
          i++;
        }
        out.push(this.renderTable(tableLines));
        continue;
      }

      // 8. Lists (Unordered, Ordered, Task Lists)
      const listMatch = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(line);
      if (listMatch) {
        const { html: listHtml, nextIndex } = this.renderList(lines, i);
        out.push(listHtml);
        i = nextIndex;
        continue;
      }

      // 9. Standard Paragraphs
      const paraLines = [];
      while (i < lines.length && lines[i].trim() && !this.isBlockStarter(lines[i])) {
        // Stop if next line is a setext underline
        if (i + 1 < lines.length && /^(=+|-+)$/.test(lines[i + 1].trim())) {
          break;
        }
        paraLines.push(lines[i]);
        i++;
      }

      if (paraLines.length > 0) {
        const paraText = paraLines.join(" ");
        out.push(`<p>${this.inline(paraText)}</p>`);
      }
    }

    return out.join("\n");
  }

  isBlockStarter(line) {
    const t = line.trim();
    if (!t) return false;
    if (t.startsWith("<!--")) return true;
    if (/^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)/.test(t)) return true;
    if (/^<a\s+(?:id|name)=/i.test(t)) return true;
    if (/^#{1,6}\s+/.test(t)) return true;
    if (/^(```|~~~)/.test(t)) return true;
    if (t.startsWith(">")) return true;
    if (/^(\*{3,}|-{3,}|_{3,})\s*$/.test(t)) return true;
    if (t.includes("|") && /\|.*\|/.test(t)) return true;
    if (/^([-*+]|\d+\.)\s+/.test(t)) return true;
    return false;
  }

  formatHeading(text, level) {
    const inlineHtml = this.inline(text);
    const plainText = this.inlineText(text);
    let slug = slugify(text.replace(/`([^`]+)`/g, "$1"));

    // Ensure unique ID
    let finalSlug = slug;
    let counter = 1;
    while (this.ids.has(finalSlug)) {
      finalSlug = `${slug}-${counter++}`;
    }
    this.ids.add(finalSlug);

    if (!this.title && level <= 2) {
      this.title = plainText;
    }

    this.toc.push({
      level,
      id: finalSlug,
      title: inlineHtml
    });

    return (
      `<h${level} id="${finalSlug}">` +
      `${inlineHtml}` +
      `<a class="kdoc-headerlink" href="#${finalSlug}" title="Permalink to this headline">¶</a>` +
      `</h${level}>`
    );
  }

  renderTable(tableLines) {
    if (tableLines.length < 2) return "";

    const splitRow = (row) => {
      let r = row.trim();
      if (r.startsWith("|")) r = r.slice(1);
      if (r.endsWith("|")) r = r.slice(0, -1);
      return r.split("|").map((cell) => cell.trim());
    };

    const headerCells = splitRow(tableLines[0]);
    const alignRow = splitRow(tableLines[1]);
    const alignments = alignRow.map((col) => {
      const left = col.startsWith(":");
      const right = col.endsWith(":");
      if (left && right) return "center";
      if (right) return "right";
      if (left) return "left";
      return "";
    });

    const thead = (
      "<thead><tr>" +
      headerCells.map((cell, idx) => {
        const align = alignments[idx] ? ` style="text-align:${alignments[idx]};"` : "";
        return `<th${align}>${this.inline(cell)}</th>`;
      }).join("") +
      "</tr></thead>"
    );

    const tbodyRows = [];
    for (let r = 2; r < tableLines.length; r++) {
      const rowCells = splitRow(tableLines[r]);
      const tr = (
        "<tr>" +
        rowCells.map((cell, idx) => {
          const align = alignments[idx] ? ` style="text-align:${alignments[idx]};"` : "";
          return `<td${align}>${this.inline(cell)}</td>`;
        }).join("") +
        "</tr>"
      );
      tbodyRows.push(tr);
    }

    const tbody = `<tbody>${tbodyRows.join("")}</tbody>`;
    return `<table class="docutils">${thead}${tbody}</table>`;
  }

  renderList(lines, startIndex) {
    const items = [];
    let i = startIndex;
    const firstLine = lines[i];
    const isOrdered = /^\s*\d+\.\s+/.test(firstLine);
    let isTaskList = false;

    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        i++;
        continue;
      }

      const match = isOrdered
        ? /^(\s*)\d+\.\s+(.*)$/.exec(line)
        : /^(\s*)([-*+])\s+(.*)$/.exec(line);

      if (!match) break;

      let itemText = match[match.length - 1];
      const subLines = [];
      i++;

      // Collect continuation lines of this list item
      while (i < lines.length && lines[i].trim() && !this.isListItemStart(lines[i])) {
        subLines.push(lines[i].trim());
        i++;
      }

      if (subLines.length > 0) {
        itemText += " " + subLines.join(" ");
      }

      // Check for GFM task list checkbox [ ] or [x]
      const taskMatch = /^\[([ xX])\]\s+(.*)$/.exec(itemText);
      if (taskMatch) {
        isTaskList = true;
        const checked = taskMatch[1].toLowerCase() === "x";
        const content = taskMatch[2];
        items.push({
          isTask: true,
          checked,
          html: this.inline(content)
        });
      } else {
        items.push({
          isTask: false,
          checked: false,
          html: this.inline(itemText)
        });
      }
    }

    const tag = isOrdered ? "ol" : "ul";
    const cls = isTaskList ? ' class="task-list"' : "";
    const listItemsHtml = items.map((it) => {
      if (it.isTask) {
        const chk = it.checked ? ' checked=""' : "";
        return `<li class="task-list-item"><input type="checkbox" disabled${chk} /> <span>${it.html}</span></li>`;
      }
      return `<li>${it.html}</li>`;
    }).join("");

    return {
      html: `<${tag}${cls}>${listItemsHtml}</${tag}>`,
      nextIndex: i
    };
  }

  isListItemStart(line) {
    return /^(\s*)([-*+]|\d+\.)\s+/.test(line);
  }

  /**
   * Plain-text strip for TOC entries.
   */
  inlineText(text) {
    return this.inline(text).replace(/<[^>]+>/g, "");
  }

  /**
   * Format image tags, adding badge class if image URL corresponds to a shield/badge.
   */
  formatImage(alt, rawUrl, rawTitle) {
    const titleAttr = rawTitle ? ` title="${escapeHtml(rawTitle)}"` : "";
    const isBadge = /(badge|shields\.io|crates\.io\/crates\/[^\/]+\.svg|deps\.rs)/i.test(rawUrl);
    const badgeClass = isBadge ? " kdoc-badge" : "";
    if (/^[a-z]+:\/\//i.test(rawUrl)) {
      return `<img class="kdoc-img${badgeClass}" src="${escapeHtml(rawUrl)}" alt="${escapeHtml(alt)}"${titleAttr} />`;
    }
    const resolved = resolveRelativePath(rawUrl, this.path);
    return `<img class="kdoc-img${badgeClass}" data-raw="${escapeHtml(resolved)}" alt="${escapeHtml(alt)}"${titleAttr} />`;
  }

  /**
   * Format links to relative documents, sources, external URLs, or internal section anchors.
   */
  formatLink(innerHtml, rawUrl, rawTitle) {
    const titleAttr = rawTitle ? ` title="${escapeHtml(rawTitle)}"` : "";
    if (rawUrl.startsWith("#")) {
      return `<a class="reference internal" href="${escapeHtml(rawUrl)}"${titleAttr}>${innerHtml}</a>`;
    }
    if (/^[a-z]+:\/\//i.test(rawUrl)) {
      return `<a class="reference external" href="${escapeHtml(rawUrl)}" target="_blank" rel="noopener noreferrer"${titleAttr}>${innerHtml}</a>`;
    }
    const resolved = resolveRelativePath(rawUrl, this.path);
    if (/\.(md|markdown|rst|txt)$/i.test(resolved.split("#")[0])) {
      return `<a class="reference internal" data-doc="${escapeHtml(resolved)}" href="#"${titleAttr}>${innerHtml}</a>`;
    }
    return `<a class="reference internal" data-src="${escapeHtml(resolved)}" href="#"${titleAttr}>${innerHtml}</a>`;
  }

  /**
   * Render formatting inside link text without generating nested <a> tags.
   */
  renderInner(src) {
    if (!src) return "";
    const parts = [
      "(?<esc>\\\\([\\\\`*_{}\\[\\]()#+\\-.!>~|]))",
      "(?<lit>`(?<litText>[^`\\n]+)`)",
      "(?<img>!\\[(?<imgAlt>[^\\]]*?)\\]\\((?<imgUrl>[^)\\s]+)(?:\\s+\"(?<imgTitle>[^\"]*)\")?\\))",
      "(?<bolditalic>\\*\\*\\*(?<boldItalicText>[^*]+?)\\*\\*\\*)",
      "(?<strong>(?:\\*\\*(?<strongText1>[^*]+?)\\*\\*|__(?<strongText2>[^_]+?)__))",
      "(?<em>(?:\\*(?<emText1>[^*\\n]+?)\\*|_(?<emText2>[^_\\n]+?)_))",
      "(?<strike>~~(?<strikeText>[^~\\n]+?)~~)"
    ];
    const re = new RegExp(parts.join("|"), "g");
    let out = "";
    let last = 0;
    let m;
    while ((m = re.exec(src)) !== null) {
      const g = m.groups;
      out += escapeHtml(src.slice(last, m.index));
      if (g.esc !== undefined) {
        out += escapeHtml(m[0].slice(1));
      } else if (g.lit !== undefined) {
        out += `<code class="docutils literal notranslate">${escapeHtml(g.litText)}</code>`;
      } else if (g.img !== undefined) {
        out += this.formatImage(g.imgAlt || "", g.imgUrl || "", g.imgTitle || "");
      } else if (g.bolditalic !== undefined) {
        out += `<strong><em>${escapeHtml(g.boldItalicText)}</em></strong>`;
      } else if (g.strong !== undefined) {
        out += `<strong>${escapeHtml(g.strongText1 || g.strongText2)}</strong>`;
      } else if (g.em !== undefined) {
        out += `<em>${escapeHtml(g.emText1 || g.emText2)}</em>`;
      } else if (g.strike !== undefined) {
        out += `<del>${escapeHtml(g.strikeText)}</del>`;
      }
      last = re.lastIndex;
    }
    if (last < src.length) out += escapeHtml(src.slice(last));
    return out;
  }

  /**
   * Inline formatting parser (links, code, bold, italics, strikethrough, automarkup).
   *
   * @param {string} text - Raw inline text
   * @returns {string} Safe HTML with styling and navigation data-attributes
   */
  inline(text) {
    const src = String(text ?? "");
    if (!src) return "";

    const parts = [
      // 1. Escaped characters
      "(?<esc>\\\\([\\\\`*_{}\\[\\]()#+\\-.!>~|]))",
      // 2. Inline code: `code`
      "(?<lit>`(?<litText>[^`\\n]+)`)",
      // 3. Images: ![alt](url "title")
      "(?<img>!\\[(?<imgAlt>[^\\]]*?)\\]\\((?<imgUrl>[^)\\s]+)(?:\\s+\"(?<imgTitle>[^\"]*)\")?\\))",
      // 4. Links: [text](url "title") - supports nested images ![alt](url) and formatted text
      "(?<link>(?<!\\!)(\\[(?<linkText>(?:!\\[[^\\]]*\\]\\([^)]*\\)|`[^`]+`|[^\\[\\]])+)\\]\\((?<linkUrl>[^)\\s]+)(?:\\s+\"(?<linkTitle>[^\"]*)\")?\\)))",
      // 5. Full/collapsed reference links: [text][ref] or [ref][]
      "(?<reflink>(?<!\\!)(\\[(?<refText>(?:!\\[[^\\]]*\\]\\([^)]*\\)|`[^`]+`|[^\\[\\]])+)\\]\\[(?<refLabel>[^\\]]*)\\]))",
      // 6. Shortcut reference links: [ref]
      "(?<shortcut>(?<!\\!)(\\[(?<shortcutText>`[^`]+`|[^\\[\\]]+)\\])(?![\\(\\[:]))",
      // 7. Bold & Italic: ***text***
      "(?<bolditalic>\\*\\*\\*(?<boldItalicText>[^*]+?)\\*\\*\\*)",
      // 8. Bold: **text** or __text__
      "(?<strong>(?:\\*\\*(?<strongText1>[^*]+?)\\*\\*|__(?<strongText2>[^_]+?)__))",
      // 9. Italic: *text* or _text_
      "(?<em>(?:\\*(?<emText1>[^*\\n]+?)\\*|_(?<emText2>[^_\\n]+?)_))",
      // 10. Strikethrough: ~~text~~
      "(?<strike>~~(?<strikeText>[^~\\n]+?)~~)",
      // 11. Autolinks: <https://...>
      "(?<autolink><(?<autoUrl>(?:https?|ftp):\\/\\/[^>]+)>)",
      // 12. Bare URLs: https://...
      "(?<url>\\b(?:https?|ftp):\\/\\/[^\\s<>\"'`)\]]*[^\\s<>\"'`).,;:\\]!?])",
      // 13. Kernel automarkup: CONFIG_FOO
      "(?<config>\\bCONFIG_[A-Z0-9_]+\\b)",
      // 14. Kernel automarkup: func()
      "(?<func>\\b(?<funcName>[A-Za-z_][A-Za-z0-9_]*)\\(\\))",
      // 15. Kernel automarkup: struct / union / enum / typedef
      "(?<cstruct>\\b(?<cKind>struct|union|enum|typedef)\\s+(?<cName>[A-Za-z_][A-Za-z0-9_]*)\\b)"
    ];

    const re = new RegExp(parts.join("|"), "g");
    let out = "";
    let last = 0;
    let m;

    while ((m = re.exec(src)) !== null) {
      const g = m.groups;
      out += escapeHtml(src.slice(last, m.index));

      if (g.esc !== undefined) {
        out += escapeHtml(m[0].slice(1));
      } else if (g.lit !== undefined) {
        out += `<code class="docutils literal notranslate">${escapeHtml(g.litText)}</code>`;
      } else if (g.img !== undefined) {
        out += this.formatImage(g.imgAlt || "", g.imgUrl || "", g.imgTitle || "");
      } else if (g.link !== undefined) {
        out += this.formatLink(this.renderInner(g.linkText), g.linkUrl, g.linkTitle);
      } else if (g.reflink !== undefined) {
        const target = g.refLabel.trim() || g.refText;
        const ref = this.refLinks.get(this.normalizeRefKey(target)) || this.refLinks.get(target.trim().toLowerCase());
        if (ref) {
          out += this.formatLink(this.renderInner(g.refText), ref.url, ref.title);
        } else {
          out += escapeHtml(m[0]);
        }
      } else if (g.shortcut !== undefined) {
        const target = g.shortcutText;
        const ref = this.refLinks.get(this.normalizeRefKey(target)) || this.refLinks.get(target.trim().toLowerCase());
        if (ref) {
          out += this.formatLink(this.renderInner(g.shortcutText), ref.url, ref.title);
        } else {
          out += escapeHtml(m[0]);
        }
      } else if (g.bolditalic !== undefined) {
        out += `<strong><em>${escapeHtml(g.boldItalicText)}</em></strong>`;
      } else if (g.strong !== undefined) {
        out += `<strong>${escapeHtml(g.strongText1 || g.strongText2)}</strong>`;
      } else if (g.em !== undefined) {
        out += `<em>${escapeHtml(g.emText1 || g.emText2)}</em>`;
      } else if (g.strike !== undefined) {
        out += `<del>${escapeHtml(g.strikeText)}</del>`;
      } else if (g.autolink !== undefined) {
        const u = escapeHtml(g.autoUrl);
        out += `<a class="reference external" href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`;
      } else if (g.url !== undefined) {
        const u = escapeHtml(m[0]);
        out += `<a class="reference external" href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`;
      } else if (g.config !== undefined) {
        const sym = m[0];
        out += `<a class="reference internal kdoc-kconfig" data-kconfig="${escapeHtml(sym.replace(/^CONFIG_/, ""))}" href="#"><code class="xref kconfig">${escapeHtml(sym)}</code></a>`;
      } else if (g.func !== undefined) {
        const fn = g.funcName;
        out += `<a class="reference internal kdoc-symbol" data-symbol="${escapeHtml(fn)}" href="#"><code>${escapeHtml(fn)}()</code></a>`;
      } else if (g.cstruct !== undefined) {
        const kind = g.cKind;
        const name = g.cName;
        out += `<a class="reference internal kdoc-symbol" data-symbol="${escapeHtml(name)}" href="#"><code>${escapeHtml(kind)} ${escapeHtml(name)}</code></a>`;
      }

      last = re.lastIndex;
    }

    if (last < src.length) {
      out += escapeHtml(src.slice(last));
    }

    return out;
  }
}

/**
 * Render a Markdown documentation file.
 *
 * @param {string} source - Raw Markdown text
 * @param {string} path - Document repository path
 * @returns {{ html: string, toc: Array<{level:number,id:string,title:string}>, title: string }}
 */
export function renderMarkdownDocument(source, path) {
  return new MarkdownRenderer(source, { path }).render();
}

/**
 * Render a Markdown fragment to HTML.
 *
 * @param {string} source - Markdown text fragment
 * @param {string} path - Document context path
 * @returns {string} Rendered HTML string
 */
export function renderMarkdownFragment(source, path = "") {
  return new MarkdownRenderer(source, { path }).render().html;
}

/**
 * Extract document title from Markdown source.
 *
 * @param {string} source - Raw Markdown text
 * @returns {string} Document title or empty string
 */
export function extractMarkdownTitle(source) {
  const lines = String(source || "").split(/\r?\n/, 50);
  for (let i = 0; i < lines.length; i++) {
    const atx = /^#\s+(.*)$/.exec(lines[i].trim());
    if (atx) return atx[1].replace(/\s+#+\s*$/, "").trim();
    if (i + 1 < lines.length && lines[i].trim() && /^={3,}$/.test(lines[i + 1].trim())) {
      return lines[i].trim();
    }
  }
  return "";
}
