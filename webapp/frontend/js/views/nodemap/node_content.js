/**
 * node_content.js - HTML of NodeMap content bodies that need no live DOM: note cards (lightweight
 * Markdown) and syntax-highlighted source slices (the code view's lexical highlighter, `AstOverlay`).
 * DOM-free (unit tested).
 */
import { AstOverlay } from "../code_view/ast_overlay.js";
import { esc } from "./struct_view.js";

function inline(text) {
  // Code spans first (their content stays literal), then emphasis on the escaped remainder
  const parts = String(text).split(/(`[^`]+`)/g);
  return parts.map((p) => {
    if (/^`[^`]+`$/.test(p)) return `<code>${esc(p.slice(1, -1))}</code>`;
    return esc(p)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>");
  }).join("");
}

/**
 * Note text as HTML: `#`..`###` headings, `-`/`*`/`1.` lists, `- [ ]`/`- [x]` task items, `>` quotes,
 * fenced code, `**bold**`, `*italic*`, `` `code` ``, `~~strike~~`; everything else is escaped text.
 * Links stay plain text (nothing in a note fetches or navigates).
 */
export function renderNoteMarkdown(text) {
  const lines = String(text || "").split(/\r?\n/);
  let html = "";
  let list = null;
  let fence = null;
  const closeList = () => {
    if (list) html += `</${list}>`;
    list = null;
  };
  for (const line of lines) {
    if (fence !== null) {
      if (/^```/.test(line.trim())) {
        html += `<pre class="nm-note-code">${esc(fence.join("\n"))}</pre>`;
        fence = null;
      } else fence.push(line);
      continue;
    }
    if (/^```/.test(line.trim())) {
      closeList();
      fence = [];
      continue;
    }
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    if (h) {
      closeList();
      html += `<div class="nm-note-h nm-note-h${h[1].length}">${inline(h[2])}</div>`;
      continue;
    }
    const task = /^\s*[-*]\s+\[( |x|X)\]\s+(.*)$/.exec(line);
    const ul = /^\s*[-*]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (task || ul || ol) {
      const want = ol && !ul ? "ol" : "ul";
      if (list !== want) {
        closeList();
        html += `<${want}>`;
        list = want;
      }
      if (task) html += `<li class="nm-note-task${task[1] === " " ? "" : " done"}"><span class="nm-note-box">${task[1] === " " ? "☐" : "☑"}</span> ${inline(task[2])}</li>`;
      else html += `<li>${inline((ul || ol)[1])}</li>`;
      continue;
    }
    closeList();
    const q = /^>\s?(.*)$/.exec(line);
    if (q) html += `<blockquote>${inline(q[1])}</blockquote>`;
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  if (fence !== null) html += `<pre class="nm-note-code">${esc(fence.join("\n"))}</pre>`;
  closeList();
  return html || '<p class="nm-empty">Double-click to write a note</p>';
}

/**
 * Source lines as highlighted rows with their file line numbers. `lines` is the slice, `startLine` the
 * file line of its first entry; `highlight` (Set of file lines) marks the symbol's own span.
 */
export function renderSourceRows(lines, startLine, path, highlight = null) {
  const ov = new AstOverlay();
  ov.setFileInfo(null, path || "", lines[0] || "");
  ov.indexMultiLineSpans(lines);
  let html = "";
  for (let i = 0; i < lines.length; i++) {
    const no = startLine + i;
    const mark = highlight && highlight.has(no) ? " nm-src-hl" : "";
    html += `<div class="nm-src-row${mark}"><span class="nm-src-no">${no}</span><span class="nm-src-code">${ov.renderLineHtml(i + 1, lines[i])}</span></div>`;
  }
  return html;
}
