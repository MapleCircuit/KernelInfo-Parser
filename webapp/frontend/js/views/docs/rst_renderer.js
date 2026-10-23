/**
 * rst_renderer.js - reStructuredText → HTML renderer for kernel Documentation/.
 *
 * Covers the subset of reStructuredText and Sphinx used by the kernel docs (sections, lists,
 * literal blocks, admonitions, toctree, kernel-doc, grid/simple/list tables, field and
 * definition lists, inline roles) plus a plain-text mode for legacy Documentation/*.txt files.
 * All source text is HTML-escaped; links are emitted as data attributes resolved by the caller.
 */

const ADORNMENT_CHARS = "=-`:.'\"~^_*+#<>!$%&,;?@[]\\/|{}()";
const ADMONITIONS = new Set([
  "note", "warning", "attention", "caution", "danger", "error", "hint", "important", "tip", "seealso", "todo"
]);
const C_KEYWORDS = new Set([
  "auto", "break", "case", "char", "const", "continue", "default", "do", "double", "else", "enum", "extern",
  "float", "for", "goto", "if", "inline", "int", "long", "register", "restrict", "return", "short", "signed",
  "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned", "void", "volatile", "while", "bool",
  "true", "false", "NULL", "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64", "size_t", "__u8", "__u16",
  "__u32", "__u64", "__le16", "__le32", "__le64", "__be16", "__be32", "__be64"
]);

export function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** True for files rendered as documentation instead of source code. */
export function isDocumentationPath(path) {
  if (!path) return false;
  const base = path.split("/").pop();
  if (/\.rst$/i.test(base)) return true;
  if (/^README(\.(txt|md|rst))?$/i.test(base)) return true;
  if (path.startsWith("Documentation/")) {
    if (/\.txt$/i.test(base)) return true;
    // Legacy extension-less docs (e.g. Documentation/CodingStyle, 00-INDEX), but not build files
    if (!base.includes(".") && !/^(Makefile|Kconfig|Kbuild)/.test(base)) return true;
  }
  return false;
}

/** Resolve a :doc:/toctree target relative to the current document (Sphinx semantics). */
export function resolveDocPath(target, currentPath) {
  let t = String(target || "").trim();
  if (!t) return "";
  t = t.replace(/#.*$/, "");
  let resolved;
  if (t.startsWith("/")) {
    resolved = `Documentation${t}`;
  } else {
    const dir = currentPath.includes("/") ? currentPath.slice(0, currentPath.lastIndexOf("/")) : "";
    const parts = (dir ? dir.split("/") : []).concat(t.split("/"));
    const out = [];
    for (const p of parts) {
      if (p === "..") out.pop();
      else if (p && p !== ".") out.push(p);
    }
    resolved = out.join("/");
  }
  if (!/\.(rst|txt)$/i.test(resolved)) resolved += ".rst";
  return resolved;
}

function slugify(text) {
  return String(text)
    .toLowerCase()
    .replace(/<[^>]+>/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "section";
}

function expandTabs(line) {
  if (!line.includes("\t")) return line;
  let out = "";
  for (const ch of line) {
    if (ch === "\t") out += " ".repeat(8 - (out.length % 8));
    else out += ch;
  }
  return out;
}

function indentOf(line) {
  const m = /^ */.exec(line);
  return m ? m[0].length : 0;
}

function isBlank(line) {
  return line === undefined || line.trim() === "";
}

function isAdornment(line) {
  const t = (line || "").trimEnd();
  if (t.length < 3 || t !== line.trimEnd() || /^\s/.test(line)) return false;
  const ch = t[0];
  return ADORNMENT_CHARS.includes(ch) && [...t].every((c) => c === ch);
}

function dedent(lines) {
  const indents = lines.filter((l) => !isBlank(l)).map(indentOf);
  const min = indents.length ? Math.min(...indents) : 0;
  return lines.map((l) => (isBlank(l) ? "" : l.slice(min)));
}

function trimBlankEdges(lines) {
  let s = 0;
  let e = lines.length;
  while (s < e && isBlank(lines[s])) s++;
  while (e > s && isBlank(lines[e - 1])) e--;
  return lines.slice(s, e);
}

/** Lightweight syntax highlighter for C and shell literal blocks. */
export function highlightCode(code, lang) {
  const l = (lang || "").toLowerCase();
  if (["c", "c++", "cpp", "h"].includes(l)) {
    const re = /(\/\*[\s\S]*?\*\/|\/\/[^\n]*)|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|(^[ \t]*#[ \t]*\w+)|(\b0x[0-9a-fA-F]+\b|\b\d+[uUlL]*\b)|(\b[A-Za-z_]\w*\b)/gm;
    let out = "";
    let last = 0;
    let m;
    while ((m = re.exec(code)) !== null) {
      out += escapeHtml(code.slice(last, m.index));
      const [tok, comment, str, pre, num, ident] = m;
      if (comment) out += `<span class="hl-c">${escapeHtml(tok)}</span>`;
      else if (str) out += `<span class="hl-s">${escapeHtml(tok)}</span>`;
      else if (pre) out += `<span class="hl-p">${escapeHtml(tok)}</span>`;
      else if (num) out += `<span class="hl-n">${escapeHtml(tok)}</span>`;
      else if (ident && C_KEYWORDS.has(ident)) out += `<span class="hl-k">${escapeHtml(tok)}</span>`;
      else out += escapeHtml(tok);
      last = re.lastIndex;
    }
    return out + escapeHtml(code.slice(last));
  }
  if (["sh", "shell", "bash", "console", "shell-session"].includes(l)) {
    return code.split("\n").map((line) => {
      const cm = /^(\s*)(#.*)$/.exec(line);
      if (cm && l !== "console") return `${cm[1]}<span class="hl-c">${escapeHtml(cm[2])}</span>`;
      const pm = /^(\s*[$#]\s)(.*)$/.exec(line);
      if (pm) return `<span class="hl-p">${escapeHtml(pm[1])}</span>${escapeHtml(pm[2])}`;
      return escapeHtml(line);
    }).join("\n");
  }
  return escapeHtml(code);
}

class RstRenderer {
  /**
   * @param {string} source - Raw document text.
   * @param {{ path: string, plainText?: boolean }} options
   */
  constructor(source, options) {
    this.path = options.path || "";
    this.plain = Boolean(options.plainText);
    this.lines = String(source || "").split(/\r?\n/).map(expandTabs);
    this.styles = []; // section adornment styles in order of appearance
    this.toc = []; // { level, id, title }
    this.ids = new Set();
    this.labels = new Map(); // label -> { id, title }
    this.targets = new Map(); // external target name -> url
    this.subs = new Map(); // substitution -> html
    this.pendingLabels = [];
    this.highlightLang = "none";
    this.title = "";
    this.footnoteCount = 0;
  }

  render() {
    this.collectTargets();
    const html = this.renderBlocks(this.lines, 0);
    return { html, toc: this.toc, title: this.title, labels: this.labels };
  }

  /** Pre-pass: hyperlink targets, labels and substitutions are referenced before their definition. */
  collectTargets() {
    for (let i = 0; i < this.lines.length; i++) {
      const line = this.lines[i];
      let m = /^\s*\.\.\s+_([^:]+|`[^`]+`):\s*(\S.*)?$/.exec(line);
      if (m) {
        const name = m[1].replace(/^`|`$/g, "").trim().toLowerCase();
        if (m[2] && /^(https?|ftp|mailto):/.test(m[2].trim())) this.targets.set(name, m[2].trim());
        else if (!m[2]) this.labels.set(name, { id: slugify(name), title: "" });
        continue;
      }
      m = /^\s*\.\.\s+\|([^|]+)\|\s+(replace|unicode)::\s*(.*)$/.exec(line);
      if (m) this.subs.set(m[1], m[2] === "replace" ? m[3] : m[3].replace(/U\+([0-9A-Fa-f]+)|0x([0-9A-Fa-f]+)/g, (_, a, b) => String.fromCodePoint(parseInt(a || b, 16))));
    }
  }

  uniqueId(base) {
    let id = base;
    let n = 1;
    while (this.ids.has(id)) id = `${base}-${n++}`;
    this.ids.add(id);
    return id;
  }

  sectionLevel(style) {
    let idx = this.styles.indexOf(style);
    if (idx === -1) {
      this.styles.push(style);
      idx = this.styles.length - 1;
    }
    return Math.min(6, idx + 1);
  }

  emitSection(titleText, style) {
    const level = this.sectionLevel(style);
    const title = titleText.trim();
    const id = this.uniqueId(slugify(title));
    if (!this.title) this.title = title;
    const extraAnchors = this.pendingLabels.map((label) => {
      const entry = this.labels.get(label) || { id: slugify(label) };
      entry.title = title;
      entry.id = id;
      this.labels.set(label, entry);
      return "";
    }).join("");
    this.pendingLabels = [];
    this.toc.push({ level, id, title: this.inlineText(title) });
    return `${extraAnchors}<h${level} id="${id}">${this.inline(title)}<a class="kdoc-headerlink" href="#${id}" title="Link to this heading">¶</a></h${level}>`;
  }

  /** Render a list of (already relatively indented) lines as block content. */
  renderBlocks(lines, depth) {
    let out = "";
    let i = 0;
    const n = lines.length;

    while (i < n) {
      const line = lines[i];
      if (isBlank(line)) { i++; continue; }
      const ind = indentOf(line);

      // Section with overline: ===\nTitle\n===
      if (depth === 0 && isAdornment(line) && i + 2 < n && !isBlank(lines[i + 1]) && isAdornment(lines[i + 2])
        && lines[i + 2].trim()[0] === line.trim()[0] && lines[i + 1].trim().length <= line.trim().length + 2) {
        out += this.emitSection(lines[i + 1], `o${line.trim()[0]}`);
        i += 3;
        continue;
      }

      // Section with underline: Title\n=====
      if (depth === 0 && ind === 0 && i + 1 < n && isAdornment(lines[i + 1]) && !isAdornment(line)
        && lines[i + 1].trim().length >= Math.max(3, line.trim().length - 2)
        && (i === 0 || isBlank(lines[i - 1]) || isAdornment(lines[i - 1]))) {
        out += this.emitSection(line, `u${lines[i + 1].trim()[0]}`);
        i += 2;
        continue;
      }

      // Transition
      if (isAdornment(line) && line.trim().length >= 4 && (i === 0 || isBlank(lines[i - 1])) && isBlank(lines[i + 1])) {
        out += `<hr class="docutils" />`;
        i++;
        continue;
      }

      // Legacy plain text centers its titles with tabs: an isolated, deeply indented single line
      if (this.plain && depth === 0 && ind >= 8 && (i === 0 || isBlank(lines[i - 1])) && isBlank(lines[i + 1])
        && line.trim().length <= 72 && !/[.,;:]$/.test(line.trim()) && /[A-Za-z]/.test(line)) {
        out += this.emitSection(line, this.toc.length === 0 ? "c1" : "c2");
        i++;
        continue;
      }

      // Indented block: block quote (rST) or preformatted (legacy plain text)
      if (ind > 0) {
        const block = this.takeIndented(lines, i, 1);
        i = block.end;
        const body = dedent(block.lines);
        if (this.plain) out += `<pre class="literal-block">${escapeHtml(trimBlankEdges(body).join("\n"))}</pre>`;
        else out += `<blockquote>${this.renderBlocks(body, depth + 1)}</blockquote>`;
        continue;
      }

      // Explicit markup: directives, comments, targets, footnotes, substitutions
      if (/^\.\.(\s|$)/.test(line) && !this.plain) {
        const res = this.renderExplicit(lines, i, depth);
        out += res.html;
        i = res.end;
        continue;
      }

      // Grid table
      if (/^\+[-=+]+\+\s*$/.test(line)) {
        const res = this.renderGridTable(lines, i);
        if (res) { out += res.html; i = res.end; continue; }
      }

      // Simple table
      if (/^=+(\s+=+)+\s*$/.test(line)) {
        const res = this.renderSimpleTable(lines, i);
        if (res) { out += res.html; i = res.end; continue; }
      }

      // Bullet list
      if (/^[-*+•]\s+\S/.test(line) || /^[-*+•]\s*$/.test(line) && !isBlank(lines[i + 1]) && indentOf(lines[i + 1]) > 0) {
        const res = this.renderList(lines, i, depth, "ul");
        out += res.html;
        i = res.end;
        continue;
      }

      // Enumerated list
      if (/^(\d+|[a-zA-Z]|#|[ivxlcdmIVXLCDM]+)[.)]\s+\S/.test(line) || /^\((\d+|[a-zA-Z]|#)\)\s+\S/.test(line)) {
        const res = this.renderList(lines, i, depth, "ol");
        if (res) { out += res.html; i = res.end; continue; }
      }

      // Field list
      if (/^:[^:\s][^:]*:(\s|$)/.test(line) && !this.plain) {
        const res = this.renderFieldList(lines, i, depth);
        out += res.html;
        i = res.end;
        continue;
      }

      // Line block
      if (/^\|(\s|$)/.test(line) && !this.plain) {
        let j = i;
        const rows = [];
        while (j < n && /^\|(\s|$)/.test(lines[j])) rows.push(lines[j++].replace(/^\|\s?/, ""));
        out += `<div class="line-block">${rows.map((r) => `<div class="line">${this.inline(r) || "&nbsp;"}</div>`).join("")}</div>`;
        i = j;
        continue;
      }

      // Doctest block
      if (/^>>>\s/.test(line)) {
        let j = i;
        while (j < n && !isBlank(lines[j])) j++;
        out += `<pre class="literal-block doctest">${escapeHtml(lines.slice(i, j).join("\n"))}</pre>`;
        i = j;
        continue;
      }

      // Definition list: term followed directly by an indented definition
      if (i + 1 < n && !isBlank(lines[i + 1]) && indentOf(lines[i + 1]) > 0 && !this.plain) {
        const res = this.renderDefinitionList(lines, i, depth);
        out += res.html;
        i = res.end;
        continue;
      }

      // Paragraph
      let j = i;
      const para = [];
      while (j < n && !isBlank(lines[j]) && indentOf(lines[j]) === 0) para.push(lines[j++]);
      i = j;
      out += this.renderParagraph(para, lines, i, depth, (consumed) => { i = consumed; });
    }
    return out;
  }

  renderParagraph(para, lines, nextIdx, depth, setNext) {
    let text = para.join("\n");
    let literal = false;
    if (!this.plain && /::\s*$/.test(text)) {
      literal = true;
      text = /\s::\s*$/.test(text) ? text.replace(/\s*::\s*$/, "") : /^::\s*$/.test(text.trim()) ? "" : text.replace(/::\s*$/, ":");
    }

    // Plain text: aligned columns and ASCII art keep their layout
    if (this.plain && para.some((l) => /\S {3,}\S/.test(l) || /[|]{1}.*[|]/.test(l) || /^[\s\-+|=*]+$/.test(l))) {
      return `<pre class="literal-block plain-text">${escapeHtml(para.join("\n"))}</pre>`;
    }

    let html = text.trim() ? `<p>${this.inline(text)}</p>` : "";
    if (literal) {
      let j = nextIdx;
      while (j < lines.length && isBlank(lines[j])) j++;
      if (j < lines.length && indentOf(lines[j]) > 0) {
        const block = this.takeIndented(lines, j, 1);
        setNext(block.end);
        const code = trimBlankEdges(dedent(block.lines)).join("\n");
        html += `<pre class="literal-block">${highlightCode(code, this.highlightLang)}</pre>`;
      } else if (j < lines.length && /^[!-/:-@[-`{-~]/.test(lines[j])) {
        // Quoted literal block (unindented, every line starts with the same punctuation)
        const q = lines[j][0];
        let k = j;
        while (k < lines.length && lines[k].startsWith(q)) k++;
        setNext(k);
        html += `<pre class="literal-block">${escapeHtml(lines.slice(j, k).join("\n"))}</pre>`;
      }
    }
    return html;
  }

  /** Collect lines from start while they are blank or indented by at least minIndent. */
  takeIndented(lines, start, minIndent) {
    let j = start;
    const block = [];
    while (j < lines.length) {
      const l = lines[j];
      if (isBlank(l)) {
        let k = j;
        while (k < lines.length && isBlank(lines[k])) k++;
        if (k < lines.length && indentOf(lines[k]) >= minIndent) {
          for (; j < k; j++) block.push("");
          continue;
        }
        break;
      }
      if (indentOf(l) < minIndent) break;
      block.push(l);
      j++;
    }
    return { lines: block, end: j };
  }

  renderList(lines, start, depth, tag) {
    const bulletRe = /^([-*+•])(\s+|$)/;
    const enumRe = /^(\(?)(\d+|[a-zA-Z]|#|[ivxlcdmIVXLCDM]+)([.)])(\s+)/;
    const first = lines[start];
    let marker;
    let startNum = 1;
    let listType = "";
    if (tag === "ul") {
      marker = bulletRe.exec(first)[1];
    } else {
      const m = enumRe.exec(first);
      if (!m) return null;
      marker = `${m[1]}${m[3]}`;
      if (/^\d+$/.test(m[2])) startNum = parseInt(m[2], 10);
      else if (/^[a-z]$/.test(m[2]) && m[2] !== "i") { listType = "a"; startNum = m[2].charCodeAt(0) - 96; }
      else if (/^[A-Z]$/.test(m[2]) && m[2] !== "I") { listType = "A"; startNum = m[2].charCodeAt(0) - 64; }
      else if (/^[ivxlcdm]+$/.test(m[2])) listType = "i";
      else if (/^[IVXLCDM]+$/.test(m[2])) listType = "I";
    }

    const items = [];
    let i = start;
    while (i < lines.length) {
      const line = lines[i];
      let markerLen;
      if (tag === "ul") {
        const m = bulletRe.exec(line);
        if (!m || m[1] !== marker) break;
        markerLen = m[0].length || 2;
      } else {
        const m = enumRe.exec(line);
        if (!m || `${m[1]}${m[3]}` !== marker) break;
        markerLen = m[0].length;
      }
      const block = this.takeIndented(lines, i + 1, Math.max(1, Math.min(markerLen, 2)));
      // Continuation lines are relative to the item's text column
      const cont = block.lines.map((l) => (isBlank(l) ? "" : l.slice(Math.min(indentOf(l), markerLen))));
      items.push([line.slice(markerLen), ...cont]);
      i = block.end;
      // Allow blank lines between items
      let k = i;
      while (k < lines.length && isBlank(lines[k])) k++;
      if (k < lines.length && (tag === "ul" ? bulletRe.test(lines[k]) : enumRe.test(lines[k]))) i = k;
      else break;
    }

    const body = items.map((itemLines) => {
      const trimmed = trimBlankEdges(itemLines);
      // Single-paragraph items render inline; anything structured is parsed as blocks
      const simple = trimmed.every((l) => !isBlank(l) && indentOf(l) === 0)
        && !trimmed.some((l, idx) => idx > 0 && (/^([-*+•]|\d+[.)])\s/.test(l) || /::\s*$/.test(trimmed[idx - 1])))
        && !/::\s*$/.test(trimmed[trimmed.length - 1] || "");
      if (simple) return `<li>${this.inline(trimmed.join("\n"))}</li>`;
      return `<li>${this.renderBlocks(trimmed, depth + 1)}</li>`;
    }).join("");
    const attrs = tag === "ol" ? `${startNum !== 1 ? ` start="${startNum}"` : ""}${listType ? ` type="${listType}"` : ""}` : "";
    return { html: `<${tag} class="simple"${attrs}>${body}</${tag}>`, end: i };
  }

  renderFieldList(lines, start, depth) {
    let i = start;
    let html = "";
    while (i < lines.length) {
      const m = /^:([^:\s][^:]*):(\s+(.*)|\s*$)/.exec(lines[i]);
      if (!m) break;
      const block = this.takeIndented(lines, i + 1, 1);
      const body = [m[3] || "", ...dedent(block.lines)];
      html += `<dt class="field">${this.inline(m[1])}</dt><dd>${this.renderBlocks(trimBlankEdges(body), depth + 1)}</dd>`;
      i = block.end;
      while (i < lines.length && isBlank(lines[i]) && /^:[^:\s][^:]*:/.test(lines[i + 1] || "")) i++;
    }
    return { html: `<dl class="field-list simple">${html}</dl>`, end: i };
  }

  renderDefinitionList(lines, start, depth) {
    let i = start;
    let html = "";
    while (i < lines.length && !isBlank(lines[i]) && indentOf(lines[i]) === 0
      && i + 1 < lines.length && !isBlank(lines[i + 1]) && indentOf(lines[i + 1]) > 0) {
      const termParts = lines[i].split(/\s+:\s+/);
      const block = this.takeIndented(lines, i + 1, 1);
      const classifiers = termParts.slice(1).map((c) => ` <span class="classifier">${this.inline(c)}</span>`).join("");
      html += `<dt>${this.inline(termParts[0])}${classifiers}</dt><dd>${this.renderBlocks(trimBlankEdges(dedent(block.lines)), depth + 1)}</dd>`;
      i = block.end;
      let k = i;
      while (k < lines.length && isBlank(lines[k])) k++;
      if (k < lines.length && indentOf(lines[k]) === 0 && k + 1 < lines.length && !isBlank(lines[k + 1]) && indentOf(lines[k + 1]) > 0
        && !/^\.\.\s/.test(lines[k]) && !isAdornment(lines[k + 1])) i = k;
      else break;
    }
    return { html: `<dl class="simple">${html}</dl>`, end: i };
  }

  renderGridTable(lines, start) {
    let end = start;
    while (end < lines.length && /^[+|]/.test(lines[end])) end++;
    const tableLines = lines.slice(start, end).map((l) => l.trimEnd());
    const border = tableLines[0];
    const cols = [];
    for (let c = 0; c < border.length; c++) if (border[c] === "+") cols.push(c);
    if (cols.length < 2) return null;

    const head = [];
    const body = [];
    let current = null;
    let sawHeaderSep = tableLines.some((l) => /^\+=[=+]*\+$/.test(l));
    let inHeader = sawHeaderSep;
    for (let r = 1; r < tableLines.length; r++) {
      const l = tableLines[r];
      if (/^\+[-=+]+\+$/.test(l)) {
        if (current) (inHeader ? head : body).push(current);
        current = null;
        if (/^\+=/.test(l)) inHeader = false;
        continue;
      }
      if (!current) current = cols.slice(0, -1).map(() => []);
      for (let c = 0; c < cols.length - 1; c++) {
        current[c].push(l.slice(cols[c] + 1, cols[c + 1]).replace(/\|\s*$/, ""));
      }
    }
    if (current) body.push(current);
    const cell = (tagName, parts) => {
      const content = trimBlankEdges(dedent(parts));
      const inner = content.length > 1 && content.some(isBlank) ? this.renderBlocks(content, 1) : this.inline(content.join(" "));
      return `<${tagName}>${inner}</${tagName}>`;
    };
    const rowsHtml = (rows, tagName) => rows.map((row) => `<tr>${row.map((c) => cell(tagName, c)).join("")}</tr>`).join("");
    if (!sawHeaderSep) inHeader = false;
    return {
      html: `<table class="docutils">${head.length ? `<thead>${rowsHtml(head, "th")}</thead>` : ""}<tbody>${rowsHtml(body, "td")}</tbody></table>`,
      end,
    };
  }

  renderSimpleTable(lines, start) {
    const border = lines[start];
    const ranges = [];
    const re = /=+/g;
    let m;
    while ((m = re.exec(border)) !== null) ranges.push([m.index, m.index + m[0].length]);
    if (ranges.length < 2) return null;

    const rows = [];
    let headerRows = 0;
    let borders = 1;
    let i = start + 1;
    while (i < lines.length) {
      const l = lines[i];
      if (/^=+(\s+=+)*\s*$/.test(l)) {
        borders++;
        i++;
        if (borders === 2 && i < lines.length && !isBlank(lines[i]) && !/^=+(\s+=+)*\s*$/.test(lines[i])) {
          headerRows = rows.length;
          continue;
        }
        if (borders >= 2 && (i >= lines.length || isBlank(lines[i]) || borders === 3)) break;
        continue;
      }
      if (/^-+(\s+-+)*\s*$/.test(l)) { i++; continue; }
      if (isBlank(l)) { i++; continue; }
      const cells = ranges.map(([s], idx) => {
        const e = idx === ranges.length - 1 ? l.length : ranges[idx + 1][0];
        return l.slice(s, e).trim();
      });
      if (cells[0] === "" && rows.length) {
        rows[rows.length - 1] = rows[rows.length - 1].map((c, idx) => (cells[idx] ? `${c} ${cells[idx]}` : c));
      } else {
        rows.push(cells);
      }
      i++;
    }
    if (borders < 2) return null;
    const head = rows.slice(0, headerRows);
    const body = rows.slice(headerRows);
    const tr = (r, t) => `<tr>${r.map((c) => `<${t}>${this.inline(c)}</${t}>`).join("")}</tr>`;
    return {
      html: `<table class="docutils">${head.length ? `<thead>${head.map((r) => tr(r, "th")).join("")}</thead>` : ""}<tbody>${body.map((r) => tr(r, "td")).join("")}</tbody></table>`,
      end: i,
    };
  }

  /** Explicit markup starting with "..". */
  renderExplicit(lines, start, depth) {
    const line = lines[start];
    const block = this.takeIndented(lines, start + 1, 1);
    const end = block.end;
    const body = dedent(block.lines);

    // Hyperlink target / label
    let m = /^\.\.\s+_([^:]+|`[^`]+`):\s*(.*)$/.exec(line);
    if (m) {
      const name = m[1].replace(/^`|`$/g, "").trim().toLowerCase();
      if (!m[2]) {
        this.pendingLabels.push(name);
        const id = slugify(name);
        this.ids.add(id);
        return { html: `<span id="${id}"></span>`, end };
      }
      return { html: "", end };
    }

    // Substitution definition (collected in pre-pass)
    if (/^\.\.\s+\|[^|]+\|/.test(line)) return { html: "", end };

    // Footnote / citation
    m = /^\.\.\s+\[([^\]]+)\]\s*(.*)$/.exec(line);
    if (m) {
      const label = m[1] === "#" || m[1] === "*" ? String(++this.footnoteCount) : m[1].replace(/^#/, "");
      const content = this.renderBlocks(trimBlankEdges([m[2], ...body]), depth + 1);
      return { html: `<aside class="footnote" id="fn-${slugify(label)}"><span class="footnote-label">[${escapeHtml(label)}]</span>${content}</aside>`, end };
    }

    // Directive
    m = /^\.\.\s+([\w:.+-]+)::\s*(.*)$/.exec(line);
    if (m) {
      return { html: this.renderDirective(m[1].toLowerCase(), m[2].trim(), body, depth), end };
    }

    // Comment
    return { html: "", end };
  }

  splitDirectiveBody(body) {
    const options = {};
    let i = 0;
    while (i < body.length && /^:[\w-]+:/.test(body[i])) {
      const om = /^:([\w-]+):\s*(.*)$/.exec(body[i]);
      options[om[1].toLowerCase()] = om[2];
      i++;
    }
    return { options, content: trimBlankEdges(body.slice(i)) };
  }

  renderDirective(name, arg, body, depth) {
    // Arguments may continue on following lines until a blank line or options
    let argText = arg;
    let rest = body;
    if (!["code-block", "code", "sourcecode", "literalinclude", "math"].includes(name)) {
      let k = 0;
      while (k < rest.length && !isBlank(rest[k]) && !/^:[\w-]+:/.test(rest[k])) k++;
      if (k > 0 && ADMONITIONS.has(name)) {
        // Admonition text may start on the directive line and continue
      } else if (k > 0 && !["toctree", "list-table", "flat-table", "csv-table", "table", "only", "rubric", "kernel-doc"].includes(name)) {
        argText = [arg, ...rest.slice(0, k)].join(" ").trim();
        rest = rest.slice(k);
      }
    }
    const { options, content } = this.splitDirectiveBody(rest);

    if (ADMONITIONS.has(name) || name === "admonition") {
      const titleMap = { seealso: "See also", todo: "Todo" };
      const title = name === "admonition" ? argText : (titleMap[name] || name.charAt(0).toUpperCase() + name.slice(1));
      const inner = name === "admonition" ? content : trimBlankEdges([argText, ...content]);
      const kind = name === "admonition" ? "note" : name;
      return `<div class="admonition ${kind}"><p class="admonition-title">${this.inline(title)}</p>${this.renderBlocks(inner, depth + 1)}</div>`;
    }

    switch (name) {
      case "code-block":
      case "code":
      case "sourcecode": {
        const lang = arg || this.highlightLang;
        return `<div class="highlight-${escapeHtml(lang || "none")} notranslate"><pre class="literal-block">${highlightCode(content.join("\n"), lang)}</pre></div>`;
      }
      case "highlight":
        this.highlightLang = arg || "none";
        return "";
      case "math":
        return `<div class="math"><pre>${escapeHtml([arg, ...content].join("\n").trim())}</pre></div>`;
      case "toctree":
        return this.renderToctree(options, content);
      case "contents":
        return `<div class="contents local topic" data-local-toc="${escapeHtml(options.depth || "")}"><p class="topic-title">${escapeHtml(argText || "Contents")}</p></div>`;
      case "kernel-doc":
      case "kernel-abi":
      case "kernel-feat":
      case "maintainers-include":
      case "kernel-include":
      case "include":
      case "literalinclude": {
        // kernel-doc and $srctree paths are relative to the source tree; others to the document
        const path = argText.replace(/^\$\w+\//, "").trim();
        let resolved;
        if (name === "kernel-doc" || /^(include|kernel|fs|drivers|arch|mm|net|lib|tools|scripts|Documentation|block|crypto|security|sound|virt|init|ipc|samples|usr|rust|io_uring)\//.test(path)) {
          resolved = path.replace(/^\//, "");
        } else {
          resolved = resolveDocPath(path, this.path);
          if (!/\.(rst|txt)$/i.test(path)) resolved = resolved.replace(/\.rst$/, "");
        }
        const optsHtml = Object.entries(options).map(([k, v]) => `<span class="kdoc-opt">:${escapeHtml(k)}: ${escapeHtml(v)}</span>`).join("");
        const labels = {
          "kernel-doc": "API documentation extracted from source comments",
          include: "Included document",
          "kernel-include": "Included document",
          literalinclude: "Included source",
          "kernel-abi": "ABI documentation",
          "kernel-feat": "Feature matrix",
          "maintainers-include": "MAINTAINERS file",
        };
        const card = `<div class="kdoc-directive-card"><div class="kdoc-directive-head"><span class="kdoc-directive-name">${escapeHtml(name)}</span><span>${labels[name]}</span></div>
          <a class="reference internal" data-src="${escapeHtml(resolved)}" href="#">${escapeHtml(resolved || argText)}</a>${optsHtml ? `<div class="kdoc-opts">${optsHtml}</div>` : ""}</div>`;
        if (name === "kernel-doc") {
          // Filled with the extracted API documentation by DocPage; the card remains as fallback
          return `<div class="kdoc-kerneldoc" data-kdoc-path="${escapeHtml(resolved)}" data-kdoc-opts="${escapeHtml(JSON.stringify(options))}">${card}</div>`;
        }
        return card;
      }
      case "image":
      case "figure":
      case "kernel-figure":
      case "kernel-image": {
        const caption = trimBlankEdges(content);
        let imgPath = resolveDocPath(argText, this.path);
        if (!/\.(rst|txt)$/i.test(argText)) imgPath = imgPath.replace(/\.rst$/, "");
        const width = /^\d+(\.\d+)?(px|%|em)?$/.test(options.width || "") ? options.width : "";
        const align = ["left", "center", "right"].includes(options.align) ? ` kdoc-align-${options.align}` : "";
        const alt = options.alt || argText.split("/").pop();
        return `<figure class="kdoc-figure${align}"><img class="kdoc-img" data-raw="${escapeHtml(imgPath)}" alt="${escapeHtml(alt)}"${width ? ` style="width:${escapeHtml(/^\d+(\.\d+)?$/.test(width) ? `${width}px` : width)}"` : ""} loading="lazy" />${caption.length && name !== "image" ? `<figcaption>${this.renderBlocks(caption, depth + 1)}</figcaption>` : ""}</figure>`;
      }
      case "only":
      case "container":
      case "cssclass":
      case "class":
      case "epigraph":
      case "highlights":
      case "pull-quote":
      case "compound":
        return this.renderBlocks(content, depth + 1);
      case "rubric":
        return `<p class="rubric">${this.inline(argText)}</p>`;
      case "topic":
      case "sidebar":
        return `<div class="topic"><p class="topic-title">${this.inline(argText)}</p>${this.renderBlocks(content, depth + 1)}</div>`;
      case "list-table":
      case "flat-table":
        return this.renderListTable(argText, options, content, name === "flat-table");
      case "csv-table":
        return this.renderCsvTable(argText, options, content);
      case "table":
        return `${argText ? `<p class="caption">${this.inline(argText)}</p>` : ""}${this.renderBlocks(content, depth + 1)}`;
      case "raw":
      case "sectionauthor":
      case "codeauthor":
      case "index":
      case "default-domain":
      case "c:namespace":
      case "c:namespace-push":
      case "c:namespace-pop":
      case "tabularcolumns":
      case "sectnum":
      case "meta":
      case "title":
        return "";
      default: {
        // Domain object descriptions: c:function, c:type, c:macro, c:struct, function, ...
        if (/^(c:|cpp:)?(function|type|macro|struct|union|enum|member|var|data|enumerator|var)$/.test(name)) {
          return `<dl class="c ${escapeHtml(name.replace(/^c(pp)?:/, ""))}"><dt class="sig"><code class="sig-name">${escapeHtml(argText)}</code></dt><dd>${this.renderBlocks(content, depth + 1)}</dd></dl>`;
        }
        if (/^(versionadded|versionchanged|deprecated)$/.test(name)) {
          const labelMap = { versionadded: "New in version", versionchanged: "Changed in version", deprecated: "Deprecated since version" };
          return `<div class="versionmodified"><span class="versionmodified-label">${labelMap[name]} ${escapeHtml(arg)}:</span> ${this.renderBlocks(content, depth + 1)}</div>`;
        }
        return `<div class="kdoc-directive-card"><div class="kdoc-directive-head"><span class="kdoc-directive-name">${escapeHtml(name)}</span><span>${escapeHtml(argText)}</span></div>${content.length ? `<pre class="literal-block">${escapeHtml(content.join("\n"))}</pre>` : ""}</div>`;
      }
    }
  }

  renderToctree(options, content) {
    const entries = content.filter((l) => !isBlank(l)).map((l) => l.trim());
    const items = entries.map((entry) => {
      const m = /^(.*?)\s*<([^>]+)>$/.exec(entry);
      const title = m ? m[1] : "";
      const target = m ? m[2] : entry;
      if (/^https?:/.test(target)) {
        return `<li class="toctree-l1"><a class="reference external" href="${escapeHtml(target)}" target="_blank" rel="noopener noreferrer">${escapeHtml(title || target)}</a></li>`;
      }
      if (target.includes("*")) {
        return `<li class="toctree-l1"><span class="kdoc-muted">${escapeHtml(target)} (glob)</span></li>`;
      }
      const path = resolveDocPath(target, this.path);
      const fallback = title || target.split("/").filter(Boolean).pop().replace(/[-_]/g, " ");
      return `<li class="toctree-l1"><a class="reference internal" data-doc="${escapeHtml(path)}" ${title ? "" : "data-fetch-title=\"1\""} href="#">${escapeHtml(fallback)}</a></li>`;
    }).join("");
    const caption = options.caption ? `<p class="caption" role="heading"><span class="caption-text">${this.inline(options.caption)}</span></p>` : "";
    return `<div class="toctree-wrapper compound">${caption}<ul>${items}</ul></div>`;
  }

  renderListTable(title, options, content, flat) {
    // Rows are top-level bullets; cells are second-level bullets
    const rows = [];
    let i = 0;
    while (i < content.length) {
      const l = content[i];
      if (isBlank(l)) { i++; continue; }
      const rm = /^([-*])\s+(.*)$/.exec(l);
      if (!rm) { i++; continue; }
      const block = this.takeIndented(content, i + 1, 1);
      const rowLines = dedent([rm[2], ...block.lines.map((x) => (isBlank(x) ? "" : x.slice(Math.min(indentOf(x), 2))))]);
      const cells = [];
      let k = 0;
      while (k < rowLines.length) {
        const cm = /^([-*])\s*(.*)$/.exec(rowLines[k]);
        if (!cm) { k++; continue; }
        const cblock = this.takeIndented(rowLines, k + 1, 1);
        cells.push(trimBlankEdges([cm[2], ...dedent(cblock.lines)]));
        k = cblock.end;
      }
      rows.push(cells);
      i = block.end;
    }
    const headerRows = parseInt(options["header-rows"] || "0", 10) || 0;
    const stubCols = parseInt(options["stub-columns"] || "0", 10) || 0;
    const cellHtml = (cell, tag) => {
      const text = cell.join("\n").replace(/^:(cspan|rspan):`\d+`\s*/, "");
      const lines = text.split("\n");
      const inner = lines.length > 1 && lines.some(isBlank) ? this.renderBlocks(lines, 1) : this.inline(text);
      return `<${tag}>${inner}</${tag}>`;
    };
    const tr = (cells, isHead) => `<tr>${cells.map((c, idx) => cellHtml(c, isHead || idx < stubCols ? "th" : "td")).join("")}</tr>`;
    return `<table class="docutils${flat ? " flat-table" : ""}">${title ? `<caption>${this.inline(title)}</caption>` : ""}${headerRows ? `<thead>${rows.slice(0, headerRows).map((r) => tr(r, true)).join("")}</thead>` : ""}<tbody>${rows.slice(headerRows).map((r) => tr(r, false)).join("")}</tbody></table>`;
  }

  renderCsvTable(title, options, content) {
    const parseCsv = (line) => {
      const cells = [];
      let cur = "";
      let q = false;
      for (let i = 0; i < line.length; i++) {
        const c = line[i];
        if (c === '"') {
          if (q && line[i + 1] === '"') { cur += '"'; i++; } else q = !q;
        } else if (c === "," && !q) {
          cells.push(cur.trim());
          cur = "";
        } else cur += c;
      }
      cells.push(cur.trim());
      return cells;
    };
    const header = options.header ? [parseCsv(options.header)] : [];
    const rows = content.filter((l) => !isBlank(l)).map(parseCsv);
    const headerRows = parseInt(options["header-rows"] || "0", 10) || 0;
    const head = header.concat(rows.slice(0, headerRows));
    const body = rows.slice(headerRows);
    const tr = (r, t) => `<tr>${r.map((c) => `<${t}>${this.inline(c)}</${t}>`).join("")}</tr>`;
    return `<table class="docutils">${title ? `<caption>${this.inline(title)}</caption>` : ""}${head.length ? `<thead>${head.map((r) => tr(r, "th")).join("")}</thead>` : ""}<tbody>${body.map((r) => tr(r, "td")).join("")}</tbody></table>`;
  }

  /** Inline markup → plain text (for TOC entries). */
  inlineText(text) {
    return this.inline(text).replace(/<[^>]+>/g, "");
  }

  /** Inline markup → HTML. Every non-markup character is escaped. */
  inline(text) {
    const src = String(text ?? "");
    if (!src) return "";
    const parts = [
      this.plain ? null : String.raw`(?<esc>\\([^\sA-Za-z0-9]))`,
      String.raw`(?<lit>\`\`(?<litText>[^\`]+?(?:\`(?!\`)[^\`]*?)*)\`\`)`,
      this.plain ? null : String.raw`(?<role>:(?<roleName>[A-Za-z0-9_.:+-]+):\`(?<roleText>[^\`]+)\`)`,
      this.plain ? null : String.raw`(?<link>\`(?<linkText>[^\`<]*?)\s*<(?<linkUri>[^>\`]+)>\`__?)`,
      this.plain ? null : String.raw`(?<named>\`(?<namedText>[^\`]+)\`__?)`,
      this.plain ? null : String.raw`(?<cite>\`(?<citeText>[^\`]+)\`)`,
      String.raw`(?<strong>(?<![\w*])\*\*(?=\S)(?<strongText>[^*]+?)(?<=\S)\*\*(?![\w*]))`,
      String.raw`(?<em>(?<![\w*])\*(?=[^\s*])(?<emText>[^*\n]+?)(?<=[^\s*])\*(?![\w*]))`,
      this.plain ? null : String.raw`(?<sub>\|(?<subName>[^|\s][^|]*?)\|(?=[^\w]|$))`,
      this.plain ? null : String.raw`(?<fnref>\[(?<fnLabel>#?[\w-]*|\*)\]_)`,
      String.raw`(?<url>\b(?:https?|ftp):\/\/[^\s<>"'\`)\]]*[^\s<>"'\`).,;:\]!?])`,
      String.raw`(?<docpath>\bDocumentation\/[\w./+-]*\w\.(?:rst|txt)\b)`,
      String.raw`(?<wordref>\b(?<wordRefName>[A-Za-z0-9][\w.-]*)_(?![\w]))`,
      String.raw`(?<func>\b(?<funcName>[A-Za-z_][A-Za-z0-9_]*)\(\))`,
      String.raw`(?<cstruct>\b(?<cKind>struct|union|enum|typedef)\s+(?<cName>[A-Za-z_][A-Za-z0-9_]*)\b)`,
      String.raw`(?<config>\bCONFIG_[A-Z0-9_]+\b)`,
    ].filter(Boolean);
    const re = new RegExp(parts.join("|"), "g");

    let out = "";
    let last = 0;
    let m;
    while ((m = re.exec(src)) !== null) {
      const g = m.groups;
      const before = src.slice(last, m.index);
      let rendered = null;

      if (g.esc !== undefined) rendered = escapeHtml(m[0].slice(1));
      else if (g.lit !== undefined) rendered = `<code class="docutils literal notranslate">${escapeHtml(g.litText)}</code>`;
      else if (g.role !== undefined) rendered = this.renderRole(g.roleName, g.roleText);
      else if (g.link !== undefined) {
        const uri = g.linkUri.trim();
        const label = g.linkText.trim() || uri;
        if (/^(https?|ftp|mailto):/.test(uri)) rendered = `<a class="reference external" href="${escapeHtml(uri)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
        else if (uri.endsWith("_")) rendered = this.renderNamedRef(label, uri.slice(0, -1));
        else rendered = `<a class="reference internal" data-doc="${escapeHtml(resolveDocPath(uri, this.path))}" href="#">${escapeHtml(label)}</a>`;
      } else if (g.named !== undefined) rendered = this.renderNamedRef(g.namedText, g.namedText);
      else if (g.cite !== undefined) rendered = `<cite>${escapeHtml(g.citeText)}</cite>`;
      else if (g.strong !== undefined) rendered = `<strong>${escapeHtml(g.strongText)}</strong>`;
      else if (g.em !== undefined) rendered = `<em>${escapeHtml(g.emText)}</em>`;
      else if (g.sub !== undefined) {
        rendered = this.subs.has(g.subName) ? this.inline(this.subs.get(g.subName)) : null;
      } else if (g.fnref !== undefined) {
        const label = g.fnLabel.replace(/^#/, "") || "*";
        rendered = `<sup class="footnote-reference"><a href="#fn-${slugify(label)}">[${escapeHtml(label)}]</a></sup>`;
      } else if (g.url !== undefined) rendered = `<a class="reference external" href="${escapeHtml(m[0])}" target="_blank" rel="noopener noreferrer">${escapeHtml(m[0])}</a>`;
      else if (g.docpath !== undefined) rendered = `<a class="reference internal" data-doc="${escapeHtml(m[0])}" href="#">${escapeHtml(m[0])}</a>`;
      else if (g.wordref !== undefined) {
        const key = g.wordRefName.toLowerCase();
        rendered = this.targets.has(key) || this.labels.has(key) ? this.renderNamedRef(g.wordRefName, g.wordRefName) : null;
      } else if (g.func !== undefined) rendered = `<a class="reference internal kdoc-symbol" data-symbol="${escapeHtml(g.funcName)}" href="#"><code class="xref c c-func">${escapeHtml(m[0])}</code></a>`;
      else if (g.cstruct !== undefined) rendered = `<a class="reference internal kdoc-symbol" data-symbol="${escapeHtml(g.cName)}" href="#"><code class="xref c c-type">${escapeHtml(m[0])}</code></a>`;
      else if (g.config !== undefined) rendered = `<a class="reference internal kdoc-kconfig" data-kconfig="${escapeHtml(m[0].slice(7))}" href="#"><code class="xref kconfig">${escapeHtml(m[0])}</code></a>`;

      if (rendered === null) {
        // Not markup after all: emit the first character literally and continue scanning after it
        out += escapeHtml(before + src[m.index]);
        last = m.index + 1;
        re.lastIndex = last;
        continue;
      }
      out += escapeHtml(before) + rendered;
      last = re.lastIndex;
    }
    out += escapeHtml(src.slice(last));
    return out.replace(/\n/g, " ");
  }

  renderNamedRef(text, name) {
    const key = name.trim().toLowerCase();
    if (this.targets.has(key)) {
      return `<a class="reference external" href="${escapeHtml(this.targets.get(key))}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`;
    }
    const label = this.labels.get(key);
    const id = label ? label.id : slugify(name);
    return `<a class="reference internal" href="#${escapeHtml(id)}">${escapeHtml(text)}</a>`;
  }

  renderRole(role, text) {
    const r = role.toLowerCase();
    const em = /^(.*?)\s*<([^>]+)>$/.exec(text);
    const shown = em ? em[1] : text.replace(/^~/, "");
    const target = em ? em[2] : text.replace(/^[~!]/, "");

    if (r === "doc") {
      const path = resolveDocPath(target, this.path);
      const label = em ? shown : target.split("/").filter(Boolean).pop().replace(/[-_]/g, " ");
      return `<a class="reference internal" data-doc="${escapeHtml(path)}" ${em ? "" : "data-fetch-title=\"1\""} href="#"><span class="doc">${escapeHtml(label)}</span></a>`;
    }
    if (r === "ref" || r === "numref") {
      const key = target.toLowerCase();
      const label = this.labels.get(key);
      if (label) {
        return `<a class="reference internal" href="#${escapeHtml(label.id)}"><span class="std std-ref">${escapeHtml(em ? shown : (label.title || shown))}</span></a>`;
      }
      return `<span class="xref std std-ref kdoc-unresolved" data-ref="${escapeHtml(key)}" data-ref-explicit="${em ? "1" : ""}" title="Reference '${escapeHtml(target)}' is defined in another document">${escapeHtml(em ? shown : shown.replace(/[-_]/g, " "))}</span>`;
    }
    if (/^(c:)?(func|type|macro|data|member|struct|union|enum|enumerator|var|expr|texpr|id)$/.test(r) || /^(cpp|c):/.test(r)) {
      const sym = target.replace(/\(\)$/, "").replace(/^(struct|union|enum)\s+/, "").replace(/^.*\./, "");
      const shownText = /func$/.test(r) && !/\)$/.test(shown) ? `${shown}()` : shown;
      return `<a class="reference internal kdoc-symbol" data-symbol="${escapeHtml(sym)}" href="#"><code class="xref c">${escapeHtml(shownText)}</code></a>`;
    }
    if (r === "manpage") {
      const mm = /^([\w.+-]+)\((\w+)\)$/.exec(target);
      const href = mm ? `https://man7.org/linux/man-pages/man${mm[2][0]}/${mm[1]}.${mm[2]}.html` : null;
      return href
        ? `<a class="reference external manpage" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer"><em class="manpage">${escapeHtml(shown)}</em></a>`
        : `<em class="manpage">${escapeHtml(shown)}</em>`;
    }
    if (r === "kbd") return shown.split(/\s+|(?<=\w)-(?=\w)/).map((k) => `<kbd class="kbd">${escapeHtml(k)}</kbd>`).join("+");
    if (r === "abbr") return `<abbr title="${escapeHtml(em ? em[2] : "")}">${escapeHtml(shown)}</abbr>`;
    if (r === "menuselection" || r === "guilabel") return `<span class="menuselection">${escapeHtml(shown.replace(/-->/g, "→"))}</span>`;
    if (r === "sub" || r === "subscript") return `<sub>${escapeHtml(shown)}</sub>`;
    if (r === "sup" || r === "superscript") return `<sup>${escapeHtml(shown)}</sup>`;
    if (r === "emphasis") return `<em>${escapeHtml(shown)}</em>`;
    if (r === "strong") return `<strong>${escapeHtml(shown)}</strong>`;
    if (r === "math") return `<span class="math">${escapeHtml(shown)}</span>`;
    if (r === "term") return `<span class="xref std std-term">${escapeHtml(shown)}</span>`;
    if (r === "rfc") return `<a class="reference external" href="https://datatracker.ietf.org/doc/html/rfc${escapeHtml(target)}" target="_blank" rel="noopener noreferrer">RFC ${escapeHtml(shown)}</a>`;
    if (r === "kconfig" || r === "config") return `<a class="reference internal kdoc-kconfig" data-kconfig="${escapeHtml(target.replace(/^CONFIG_/, ""))}" href="#"><code class="xref kconfig">${escapeHtml(shown)}</code></a>`;
    // file, envvar, option, command, program, samp, literal, code, ...
    return `<code class="docutils literal notranslate ${escapeHtml(r.replace(/[^a-z-]/g, "-"))}">${escapeHtml(shown)}</code>`;
  }
}

/**
 * Render a documentation file.
 * @returns {{ html: string, toc: Array<{level:number,id:string,title:string}>, title: string }}
 */
export function renderDocument(source, path) {
  const plainText = !/\.rst$/i.test(path) && !looksLikeRst(source);
  return new RstRenderer(source, { path, plainText }).render();
}

/** Render a reStructuredText fragment (e.g. kernel-doc descriptions) in a document's context. */
export function renderRstFragment(source, path) {
  return new RstRenderer(source, { path, plainText: false }).render().html;
}

/** Export the slug function so cross-document anchors match generated heading ids. */
export { slugify };

/** First section title of a document (cheap scan used for toctree link labels). */
export function extractTitle(source) {
  const lines = String(source || "").split(/\r?\n/, 400).map(expandTabs);
  for (let i = 0; i + 1 < lines.length; i++) {
    const cur = lines[i];
    const next = lines[i + 1];
    if (isAdornment(cur) && i + 2 < lines.length && !isBlank(next) && isAdornment(lines[i + 2])) return next.trim();
    if (!isBlank(cur) && indentOf(cur) === 0 && !isAdornment(cur) && !/^\.\./.test(cur) && isAdornment(next)
      && next.trim().length >= Math.max(3, cur.trim().length - 2)) return cur.trim();
  }
  return "";
}

/** Legacy .txt documents that were already written in reStructuredText. */
function looksLikeRst(source) {
  const sample = String(source || "").slice(0, 20000);
  return /^\.\.\s+(\w[\w-]*::|_[^:]+:)/m.test(sample) || /:(ref|doc|c:func):`/.test(sample);
}
