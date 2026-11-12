/**
 * man_render.js - Renderers of the man page document model (format 1, `parser/man_ast/MAN_API.md`,
 * webapp/API_FRONTEND.md §5.38).
 *
 *   renderManWeb(doc, opts)  -> HTML of a man7.org-like page: a NAME(S) title bar, a section TOC,
 *                               h2 section headings, indented bodies, tag lists, tables and examples.
 *   renderManTerm(doc, opts) -> HTML of a terminal man(1) screen: header/footer lines, text filled and
 *                               justified to `opts.width` columns (80) with a 7-column body indent,
 *                               bold as bright bold and italic as underline.
 *
 * Pure and DOM-free (unit tested by webapp/frontend/tests/man_render.test.mjs). Every text of the
 * document is escaped; cross references become `<a data-man-ref="name" data-man-sec="S">` (the view
 * follows them), URLs become links only for http(s) and mailto targets.
 */

export function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Manual title of a section, as groff's an.tmac names it (the .TH fourth argument overrides it). */
const MANUALS = {
  1: "General Commands Manual", 2: "System Calls Manual", 3: "Library Functions Manual",
  4: "Kernel Interfaces Manual", 5: "File Formats Manual", 6: "Games Manual",
  7: "Miscellaneous Information Manual", 8: "System Manager's Manual", 9: "Kernel Developer's Manual",
};

/** A link target the page may open: http(s) or mailto only; anything else is dropped. */
export function safeUrl(url) {
  const u = String(url || "").trim();
  return /^(https?:\/\/|mailto:)[^\s"'<>]+$/i.test(u) ? u : "";
}

const runs = (c) => (Array.isArray(c) ? c : []);
const sectionsOf = (doc) => (doc && Array.isArray(doc.sections) ? doc.sections : []);

/** Title line parts of a page: "READ(2)", the manual, the source and the date. */
export function manHeader(doc, opts = {}) {
  const th = (doc && doc.th) || {};
  const title = String(th.title || opts.name || "").trim();
  const section = String(th.section || opts.section || "").trim();
  const tag = title ? `${title.toUpperCase()}${section ? `(${section})` : ""}` : "";
  const manual = String(th.manual || "").trim() || MANUALS[section.charAt(0)] || "";
  return { tag, manual, source: String(th.source || "").trim(), date: String(th.date || "").trim(), title, section };
}

const ANCHOR_RE = /[^a-z0-9_-]+/g;
const anchorOf = (prefix, id) => `${prefix}${String(id || "").toLowerCase().replace(ANCHOR_RE, "-")}`;

// ------------------------------------------------------------------------------------------- web

function fontWrap(f, html) {
  if (f === "B") return `<b>${html}</b>`;
  if (f === "I") return `<i>${html}</i>`;
  if (f === "BI" || f === "IB") return `<b><i>${html}</i></b>`;
  if (f === "CR" || f === "C" || f === "CW") return `<code>${html}</code>`;
  return html;
}

function refAttrs(ref) {
  const [name, sec] = Array.isArray(ref) ? ref : [];
  return `data-man-ref="${escapeHtml(name || "")}" data-man-sec="${escapeHtml(sec || "")}"`;
}

function webInline(c) {
  let out = "";
  for (const r of runs(c)) {
    if (!r || typeof r !== "object") continue;
    if (r.br) {
      out += "<br>";
      continue;
    }
    let html = fontWrap(r.f, escapeHtml(r.x));
    if (Array.isArray(r.ref) && r.ref[0]) {
      html = `<a href="#" class="man-ref" ${refAttrs(r.ref)} title="${escapeHtml(`${r.ref[0]}(${r.ref[1] || ""})`)}">${html}</a>`;
    } else if (r.url !== undefined) {
      const href = safeUrl(r.url);
      if (href) html = `<a href="${escapeHtml(href)}" class="man-url" target="_blank" rel="noopener noreferrer">${html || escapeHtml(href)}</a>`;
    }
    out += html;
  }
  return out;
}

function webBlocks(blocks, prefix) {
  let out = "";
  for (const b of Array.isArray(blocks) ? blocks : []) {
    if (!b || typeof b !== "object") continue;
    switch (b.t) {
      case "p":
        out += `<p>${webInline(b.c)}</p>`;
        break;
      case "ss":
        out += `<h3 class="man-ss" id="${escapeHtml(anchorOf(prefix, b.id || b.title))}">${escapeHtml(b.title)}</h3>`;
        break;
      case "tp":
        out += `<dl class="man-tp">${(Array.isArray(b.tags) ? b.tags : []).map((t) => `<dt>${webInline(t)}</dt>`).join("")}<dd>${webBlocks(b.body, prefix)}</dd></dl>`;
        break;
      case "ip":
        out += `<div class="man-ip">${webBlocks(b.body, prefix)}</div>`;
        break;
      case "rs":
        out += `<div class="man-rs">${webBlocks(b.body, prefix)}</div>`;
        break;
      case "pre":
        out += `<pre class="man-pre${b.ex ? " man-ex" : ""}">${(Array.isArray(b.lines) ? b.lines : []).map(webInline).join("\n")}</pre>`;
        break;
      case "table":
        out += webTable(b);
        break;
      default:
        break;
    }
  }
  return out;
}

const ALIGN = { l: "left", c: "center", r: "right", n: "right" };

function webTable(b) {
  const rows = Array.isArray(b.rows) ? b.rows : [];
  const align = Array.isArray(b.align) ? b.align : [];
  const header = Math.max(0, Number(b.header) || 0);
  const cell = (tag, c, i) => {
    const a = ALIGN[String(align[i] || "l").charAt(0).toLowerCase()] || "left";
    return `<${tag} class="man-al-${a}">${webInline(c)}</${tag}>`;
  };
  const head = rows.slice(0, header).map((r) => `<tr>${(r || []).map((c, i) => cell("th", c, i)).join("")}</tr>`).join("");
  const body = rows.slice(header).map((r) => `<tr>${(r || []).map((c, i) => cell("td", c, i)).join("")}</tr>`).join("");
  return `<div class="man-tbl-wrap"><table class="man-tbl${b.box ? " man-box" : ""}">${head ? `<thead>${head}</thead>` : ""}<tbody>${body}</tbody></table></div>`;
}

/**
 * man7.org-like HTML of a document. opts: {idPrefix: anchor prefix ("man-"), name, section: fallbacks
 * for a page without .TH}. In-page TOC links carry `data-man-anchor` (the view scrolls; the hash is
 * the app's address and stays untouched).
 */
export function renderManWeb(doc, opts = {}) {
  const prefix = opts.idPrefix || "man-";
  const h = manHeader(doc, opts);
  const secs = sectionsOf(doc);
  const toc = secs.length > 1
    ? `<nav class="man-web-toc" aria-label="Sections">${secs.map((s) => {
      const id = anchorOf(prefix, s.id || s.title);
      return `<a href="#" data-man-anchor="${escapeHtml(id)}">${escapeHtml(s.title)}</a>`;
    }).join(" <span class=\"man-toc-sep\">|</span> ")}</nav>`
    : "";
  const body = secs.length
    ? secs.map((s) => `<section class="man-web-sec"><h2 id="${escapeHtml(anchorOf(prefix, s.id || s.title))}">${escapeHtml(s.title)}</h2><div class="man-web-body">${webBlocks(s.blocks, prefix)}</div></section>`).join("")
    : `<p class="man-empty">This page has no content.</p>`;
  return `<article class="man-web">
  <header class="man-web-head"><span class="man-web-tag">${escapeHtml(h.tag)}</span><span class="man-web-manual">${escapeHtml(h.manual)}</span><span class="man-web-tag">${escapeHtml(h.tag)}</span></header>
  ${toc}
  ${body}
  <footer class="man-web-foot"><span>${escapeHtml(h.source)}</span><span>${escapeHtml(h.date)}</span><span>${escapeHtml(h.tag)}</span></footer>
</article>`;
}

// ------------------------------------------------------------------------------------------- terminal

const TERM_INDENT = 7;
const SS_INDENT = 3;
const SPACE_RE = /[ \t\n]+/;

/** Opening/closing HTML of a run's style in the terminal (bold, underline, links). */
function termStyle(r) {
  let open = "";
  let close = "";
  if (r.f === "B" || r.f === "BI" || r.f === "IB") {
    open += '<span class="mt-b">';
    close = "</span>" + close;
  }
  if (r.f === "I" || r.f === "BI" || r.f === "IB") {
    open += '<span class="mt-u">';
    close = "</span>" + close;
  }
  if (Array.isArray(r.ref) && r.ref[0]) {
    open = `<a href="#" class="man-ref" ${refAttrs(r.ref)}>` + open;
    close += "</a>";
  } else if (r.href) {
    open = `<a href="${escapeHtml(r.href)}" class="man-url" target="_blank" rel="noopener noreferrer">` + open;
    close += "</a>";
  }
  return [open, close];
}

/** A word: pieces [{x, style}] with no breakable space between them. */
function wordLen(w) {
  return w.reduce((n, p) => n + p.x.length, 0);
}
function wordHtml(w) {
  return w.map((p) => {
    const [o, c] = termStyle(p.r);
    return `${o}${escapeHtml(p.x)}${c}`;
  }).join("");
}

/**
 * Words of a run list, and forced breaks ("\n" entries). A URL whose text is not the URL itself is
 * followed by "<url>", as groff prints .UR in a terminal.
 */
function termWords(c) {
  const out = [];
  let cur = [];
  const flush = () => {
    if (cur.length) out.push(cur);
    cur = [];
  };
  for (const r0 of runs(c)) {
    if (!r0 || typeof r0 !== "object") continue;
    if (r0.br) {
      flush();
      out.push("\n");
      continue;
    }
    let r = r0;
    let text = String(r0.x ?? "");
    if (r0.url !== undefined) {
      const href = safeUrl(r0.url);
      r = { ...r0, href };
      if (href && text.trim() && text.trim() !== href && text.trim() !== href.replace(/^mailto:/i, "")) text += ` <${href.replace(/^mailto:/i, "")}>`;
      else if (!text.trim()) text = href.replace(/^mailto:/i, "");
    }
    const parts = text.split(SPACE_RE);
    parts.forEach((part, i) => {
      if (i > 0) flush();
      if (part) cur.push({ x: part, r });
    });
  }
  flush();
  return out;
}

/** Lines (HTML) of filled, justified text: `indent` columns of margin, `width` total. */
function fill(c, indent, width) {
  const avail = Math.max(10, width - indent);
  const pad = " ".repeat(indent);
  const lines = [];
  let line = [];
  let len = 0;
  const emit = (justify) => {
    if (!line.length) {
      lines.push("");
      return;
    }
    const gaps = line.length - 1;
    let extra = justify && gaps > 0 ? avail - len : 0;
    let html = "";
    line.forEach((w, i) => {
      html += wordHtml(w);
      if (i < gaps) {
        // Extra spaces go to the leftmost gaps first
        const add = extra > 0 ? Math.ceil(extra / (gaps - i)) : 0;
        extra -= add;
        html += " ".repeat(1 + add);
      }
    });
    lines.push(pad + html);
    line = [];
    len = 0;
  };
  for (const w of termWords(c)) {
    if (w === "\n") {
      emit(false);
      continue;
    }
    const wl = wordLen(w);
    if (line.length && len + 1 + wl > avail) emit(true);
    len += (line.length ? 1 : 0) + wl;
    line.push(w);
  }
  if (line.length) emit(false);
  return lines;
}

function plainOf(c) {
  return runs(c).map((r) => (r && r.br ? " " : String(r?.x ?? ""))).join("");
}

function termTable(b, indent) {
  const rows = (Array.isArray(b.rows) ? b.rows : []).map((r) => (Array.isArray(r) ? r : []));
  const ncol = rows.reduce((n, r) => Math.max(n, r.length), 0);
  if (!ncol) return [];
  const align = Array.isArray(b.align) ? b.align : [];
  const widths = Array.from({ length: ncol }, (_, i) => rows.reduce((n, r) => Math.max(n, plainOf(r[i]).length), 0));
  const header = Math.max(0, Number(b.header) || 0);
  const pad = " ".repeat(indent);
  const cellHtml = (c, i) => {
    const text = plainOf(c);
    const room = widths[i] - text.length;
    const a = String(align[i] || "l").charAt(0).toLowerCase();
    const left = a === "r" || a === "n" ? room : a === "c" ? Math.floor(room / 2) : 0;
    const words = termWords(c).filter((w) => w !== "\n");
    return " ".repeat(left) + words.map(wordHtml).join(" ") + " ".repeat(room - left);
  };
  const rule = (l, m, r) => pad + l + widths.map((w) => "─".repeat(w + 2)).join(m) + r;
  const out = [];
  if (b.box) out.push(rule("┌", "┬", "┐"));
  rows.forEach((r, ri) => {
    const cells = Array.from({ length: ncol }, (_, i) => cellHtml(r[i], i));
    out.push(b.box ? `${pad}│ ${cells.join(" │ ")} │` : pad + cells.join("   ").replace(/\s+$/, ""));
    if (header && ri === header - 1 && ri < rows.length - 1) out.push(b.box ? rule("├", "┼", "┤") : pad + widths.map((w) => "─".repeat(w)).join("   "));
  });
  if (b.box) out.push(rule("└", "┴", "┘"));
  return out;
}

function termBlocks(blocks, indent, width, out) {
  let first = true;
  const gap = () => {
    if (!first && out.length && out[out.length - 1] !== "") out.push("");
    first = false;
  };
  for (const b of Array.isArray(blocks) ? blocks : []) {
    if (!b || typeof b !== "object") continue;
    switch (b.t) {
      case "p":
        gap();
        out.push(...fill(b.c, indent, width));
        break;
      case "ss":
        gap();
        out.push(" ".repeat(Math.max(0, indent - TERM_INDENT + SS_INDENT)) + `<span class="mt-b">${escapeHtml(b.title)}</span>`);
        first = true; // the subsection's text follows its heading directly
        break;
      case "tp": {
        gap();
        const tags = Array.isArray(b.tags) ? b.tags : [];
        const inner = indent + TERM_INDENT;
        const body = [];
        termBlocks(b.body, inner, width, body);
        tags.forEach((t, i) => {
          const tagWords = termWords(t).filter((w) => w !== "\n");
          const tagLen = tagWords.reduce((n, w) => n + wordLen(w), 0) + Math.max(0, tagWords.length - 1);
          const tagHtml = " ".repeat(indent) + tagWords.map(wordHtml).join(" ");
          // A short last tag (a bullet, "-a") shares its line with the body, like nroff does
          const firstBody = body.find((l) => l !== "");
          if (i === tags.length - 1 && firstBody !== undefined && tagLen < TERM_INDENT - 1 && body[0] === firstBody) {
            body[0] = tagHtml + firstBody.slice(indent + tagLen);
          } else {
            out.push(tagHtml);
          }
        });
        out.push(...body);
        break;
      }
      case "ip":
      case "rs": {
        gap();
        const body = [];
        termBlocks(b.body, indent + TERM_INDENT, width, body);
        out.push(...body);
        break;
      }
      case "pre":
        gap();
        for (const l of Array.isArray(b.lines) ? b.lines : []) {
          // A no-fill line keeps its own spacing: runs are printed as they are
          const html = runs(l).map((r) => {
            if (!r || typeof r !== "object" || r.br) return "";
            const href = r.url !== undefined ? safeUrl(r.url) : "";
            const [o, c] = termStyle({ ...r, href });
            return `${o}${escapeHtml(r.x)}${c}`;
          }).join("");
          out.push(html ? " ".repeat(indent) + html : "");
        }
        break;
      case "table":
        gap();
        out.push(...termTable(b, indent));
        break;
      default:
        break;
    }
  }
}

/** One line of `width` columns: left, centered middle and right parts (escaped plain text). */
function threePart(left, mid, right, width) {
  const total = left.length + mid.length + right.length;
  if (total + 2 > width) return escapeHtml([left, mid, right].filter(Boolean).join("  "));
  const midStart = Math.max(left.length + 1, Math.floor((width - mid.length) / 2));
  const midEnd = midStart + mid.length;
  const rightStart = Math.max(midEnd + 1, width - right.length);
  return escapeHtml(left + " ".repeat(midStart - left.length) + mid + " ".repeat(rightStart - midEnd) + right);
}

/**
 * Terminal man(1) HTML of a document. opts: {width: columns (80), name, section: fallbacks for a page
 * without .TH}. Returns a `<pre class="man-term">` whose lines are plain text with style spans.
 */
export function renderManTerm(doc, opts = {}) {
  const width = Math.max(40, Math.min(200, Number(opts.width) || 80));
  const h = manHeader(doc, opts);
  const lines = [threePart(h.tag, h.manual, h.tag, width), ""];
  const secs = sectionsOf(doc);
  for (const s of secs) {
    lines.push(`<span class="mt-b">${escapeHtml(s.title)}</span>`);
    termBlocks(s.blocks, TERM_INDENT, width, lines);
    if (lines[lines.length - 1] !== "") lines.push("");
  }
  if (!secs.length) lines.push(" ".repeat(TERM_INDENT) + "This page has no content.", "");
  lines.push(threePart(h.source, h.date, h.tag, width));
  return `<pre class="man-term" style="--man-cols:${width}">${lines.join("\n")}</pre>`;
}
