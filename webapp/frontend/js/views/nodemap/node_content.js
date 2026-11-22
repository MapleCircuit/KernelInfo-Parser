/**
 * node_content.js - HTML of NodeMap content bodies that need no live DOM: note cards (lightweight
 * Markdown) and syntax-highlighted source slices (the code view's lexical highlighter, `AstOverlay`,
 * plus virtualized window support via `virtual_source.js`).
 * DOM-free (unit tested).
 */
import { AstOverlay } from "../code_view/ast_overlay.js";
import { esc } from "./struct_view.js";
import { VIRTUAL_THRESHOLD, renderVirtualSourceWindow } from "./virtual_source.js";

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
 * Above `VIRTUAL_THRESHOLD` lines (unless `opts.virtual === false`), renders a virtualized viewport window.
 */
export function renderSourceRows(lines, startLine, path, highlight = null, opts = {}) {
  const arr = Array.isArray(lines) ? lines : [];
  if (arr.length > VIRTUAL_THRESHOLD && opts.virtual !== false) {
    return renderVirtualSourceWindow(arr, startLine, path, highlight, opts.range || null, opts.htmlRows || null);
  }
  const htmlRows = opts.htmlRows || null;
  let ov = null;
  if (!htmlRows) {
    ov = new AstOverlay();
    ov.setFileInfo(null, path || "", arr[0] || "");
    ov.indexMultiLineSpans(arr);
  }
  let html = "";
  const baseLine = Number(startLine) || 1;
  for (let i = 0; i < arr.length; i++) {
    const no = baseLine + i;
    const mark = highlight && highlight.has(no) ? " nm-src-hl" : "";
    const codeHtml = htmlRows ? String(htmlRows[i] ?? "") : ov.renderLineHtml(i + 1, arr[i]);
    html += `<div class="nm-src-row${mark}"><span class="nm-src-no">${no}</span><span class="nm-src-code">${codeHtml}</span></div>`;
  }
  return html;
}

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
const decodeEntities = (t) => t.replace(/&(?:(amp|lt|gt|quot|apos|nbsp)|#(\d{1,6}));/g, (m, k, n) => (k ? ENTITIES[k] : String.fromCharCode(Number(n))));

/**
 * Lexical highlighting of source lines as plain segments, for the SVG export: one array per line of
 * `{text, cls}` where `cls` is the `tok-*` class the code view colors the run with (`""` plain text).
 */
export function sourceLineSegments(lines, path) {
  const ov = new AstOverlay();
  ov.setFileInfo(null, path || "", lines[0] || "");
  ov.indexMultiLineSpans(lines);
  return lines.map((line, i) => htmlSegments(ov.renderLineHtml(i + 1, String(line ?? ""))));
}

/** `{text, cls}` runs of highlighter HTML (spans only; the innermost `tok-*` class wins). */
export function htmlSegments(html) {
  const out = [];
  const stack = [];
  const re = /<span\b([^>]*)>|<\/span>|<[^>]*>|[^<]+/g;
  let m;
  while ((m = re.exec(String(html || "")))) {
    const tok = m[0];
    if (tok.startsWith("</span")) stack.pop();
    else if (tok.startsWith("<span")) {
      const cls = /\bclass="([^"]*)"/.exec(m[1] || "")?.[1] || "";
      stack.push(cls.split(/\s+/).find((c) => c.startsWith("tok-")) || "");
    }
    else if (!tok.startsWith("<")) {
      const text = decodeEntities(tok);
      const cls = [...stack].reverse().find(Boolean) || "";
      const last = out[out.length - 1];
      if (last && last.cls === cls) last.text += text;
      else out.push({ text, cls });
    }
  }
  return out;
}

/**
 * Compact source reference a node header shows on the right (`.nm-head-meta`).
 */
export function headMetaOf(type, payload) {
  if (!payload) return { text: "", title: "" };
  if (type === "struct" && payload.file_path) {
    const text = `${payload.file_path}:${payload.line_s || ""}`;
    const span = payload.span_source === "db" ? "the index" : "the source text";
    return { text, title: `${payload.file_path}:${payload.line_s || ""}-${payload.line_e || ""} (span from ${span})` };
  }
  if (type === "source" && payload.path) {
    const text = `${payload.path} · ${payload.start_line}–${payload.end_line}`;
    const title = `${text} of ${payload.total_lines}${payload.truncated ? " · truncated" : ""}${payload.symbol ? ` · ${payload.symbol.kind} ${payload.symbol.name}` : ""}`;
    return { text, title };
  }
  if (type === "diff" && payload.files) {
    const add = payload.files.reduce((t, f) => t + (f.additions || 0), 0);
    const del = payload.files.reduce((t, f) => t + (f.deletions || 0), 0);
    const ref = payload.mode === "commit" ? String(payload.commitHash || "").slice(0, 12) : `${payload.versionA || "?"}..${payload.versionB || "?"}`;
    const text = `${payload.path ? `${payload.path} · ` : ""}${ref}`;
    return { text, title: `${text} · +${add} −${del} in ${payload.files.length} file${payload.files.length === 1 ? "" : "s"}` };
  }
  if (type === "mail" && Array.isArray(payload.rows)) {
    const n = payload.rows.length;
    const text = `${n} message${n === 1 ? "" : "s"}${payload.tid ? ` · t/${payload.tid}` : ""}`;
    return { text, title: `${text}${payload.total > n ? ` of ${payload.total}` : ""}` };
  }
  if (type === "kconfig" && payload.name) {
    const text = payload.file ? `${payload.file}${payload.line ? `:${payload.line}` : ""}` : "";
    return { text, title: text ? `CONFIG_${payload.name} defined at ${text}` : "" };
  }
  if (type === "maintainer" && payload.status) {
    return { text: payload.status, title: `S: ${payload.status}` };
  }
  if (type === "cve") {
    if (payload.mode === "file") {
      const text = `${payload.affectedCount || 0}/${payload.total || 0}`;
      return { text, title: `${payload.filePath}: ${text} CVEs` };
    }
    const text = payload.state || (payload.score != null ? `CVSS ${payload.score}` : "");
    return { text, title: `${payload.cveId}${text ? ` · ${text}` : ""}` };
  }
  if (type === "kapi") {
    const text = `${payload.catalog || "summary"}${payload.arch ? ` · ${payload.arch}` : ""}`;
    return { text, title: `${text} (${payload.total || 0} items)` };
  }
  if (type === "doc" && payload.path) {
    const text = payload.mode === "kernel-doc" ? `${payload.total || 0} entries` : `${payload.path}`;
    return { text, title: payload.path };
  }
  if (type === "history") {
    const text = payload.mode === "blame"
      ? `${payload.commits?.length || 0} commits`
      : `${payload.changeCount || 0} changes`;
    return { text, title: `${payload.path || ""} · ${text}` };
  }
  return { text: "", title: "" };
}
