/**
 * nodemap_export.js - SVG (and, through it, PNG) of a whole NodeMap canvas: the bounding box of every
 * node and wire plus a margin, never the viewport. Nodes are drawn with what they show, as vectors:
 * group frames, note text, symbol member lists, source slices (code lines), struct members in the node's
 * `list` or `table` style (types, `#if` lines indented per nesting level with their scope bars, the
 * table's `#ifdef` cell, folded branches, hidden items and the outer conditions banner as the node shows
 * them); headers with their source reference (left out for `hideHeader` nodes in View Mode), node and
 * canvas backgrounds, accents (preset or custom), auto-fit scaling and edges anchored at the struct rows
 * they leave from. Man pages and live tab iframes are hybrid: a DOM snapshot of their rendered content
 * (`captureNodeForeignObject()`, XHTML with inlined computed styles in a `<foreignObject>`), or a
 * placeholder card when nothing is mounted. Theme tokens are resolved to literal colors so the file looks
 * the same outside the app. `canvasToSvg()` and the size helpers are DOM-free (unit tested);
 * `captureNodeForeignObject()` reads the live DOM.
 */
import { BG_TINT, accentVar, boundsOf, colorToHex, displayTitle, isCustomColor, paintOrder } from "./canvas_model.js";
import { arrowHeads, dashArray, edgePath, resolveEnds } from "./edge_router.js";
import { headMetaOf } from "./node_content.js";
import { countDirectives, esc as escHtml, structRows } from "./struct_view.js";

const PAD = 40;
// Canvas-unit metrics of the drawn node chrome (they follow nodemap.css)
const HEAD_H = 26;
const BAR_H = 22;
const OUTER_H = 16;
const ROW_H = 16; // list rows
const TABLE_ROW_H = 18; // table rows: 17 units and their bottom border
const SRC_ROW_H = 16.5;
const PP_INDENT = 10; // per enclosing #if block (nodemap.css .nm-pp-indent)
const MONO_CH = 6.6; // width of one 11-unit monospace character
const SANS_CH = 6.2;
const TRACK_W = 6;
const BADGES = { struct: "struct", symbol: "symbol", source: "source", man: "man", tab: "tab", note: "note", group: "group" };

const r1 = (v) => Math.round(v * 10) / 10;
// Characters XML 1.0 does not allow (form feeds in kernel sources, NULs): dropped so the document parses
const XML_ILLEGAL = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\uFFFE\uFFFF]/g;
const esc = (s) => escHtml(String(s ?? "").replace(XML_ILLEGAL, ""));

/** `#rrggbb` mix of two resolved colors (`t` of `a`), null when either cannot be read. */
function mixColors(a, b, t) {
  const ha = colorToHex(a);
  const hb = colorToHex(b);
  if (!ha || !hb) return null;
  const ch = (h, i) => parseInt(h.slice(1 + i * 2, 3 + i * 2), 16);
  return `#${[0, 1, 2].map((i) => Math.round(ch(ha, i) * t + ch(hb, i) * (1 - t)).toString(16).padStart(2, "0")).join("")}`;
}

/** Box the content of a node is laid out in (natural size for auto-fitted nodes) and its scale. */
function layoutBox(n) {
  return n.autoFit ? { w: n.baseW, h: n.baseH, s: n.scale || 1 } : { w: n.w, h: n.h, s: 1 };
}

/** Text cut to `max` characters with an ellipsis. */
function fit(text, max) {
  const t = String(text ?? "");
  return max > 1 && t.length > max ? `${t.slice(0, Math.max(1, max - 1))}…` : t;
}

/** Word-wrapped lines of `text` at `cols` characters (longer words are cut). */
function wrap(text, cols) {
  const out = [];
  for (const para of String(text || "").split(/\r?\n/)) {
    let line = "";
    for (let word of para.split(/\s+/).filter(Boolean)) {
      while (word.length > cols) {
        if (line) out.push(line);
        line = "";
        out.push(word.slice(0, cols));
        word = word.slice(cols);
      }
      if (line && line.length + 1 + word.length > cols) {
        out.push(line);
        line = "";
      }
      line = line ? `${line} ${word}` : word;
    }
    out.push(line);
  }
  return out;
}

/** Markdown markers a note shows as structure, left out of the plain text the SVG draws. */
function plainNoteLine(line) {
  return line.replace(/^#{1,3}\s+/, "").replace(/^\s*[-*]\s+\[( |x|X)\]\s+/, (m, x) => (x === " " ? "☐ " : "☑ ")).replace(/^\s*[-*]\s+/, "• ")
    .replace(/^>\s?/, "│ ").replace(/\*\*([^*]+)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1").replace(/~~([^~]+)~~/g, "$1");
}

/**
 * Standalone SVG document of the canvas.
 *   resolve(cssColor)   turns `var(--x)` into a literal color
 *   opts.content(node)  loaded payload of a struct / source node (null while not loaded)
 *   opts.structOpts(node)  `{editMode, evaluator}` the node's struct rows are drawn with
 *   opts.editMode       Edit Mode (struct toolbar chips, hidden items and headers dimmed instead of left out)
 *   opts.foreign(node)  `{html}` DOM snapshot of a man / tab node (`captureNodeForeignObject()`), or null
 *   opts.transparent    no canvas background and no canvas-colored halo behind edge labels
 *   opts.pixelScale     the document's width/height are this many times its viewBox (PNG rasterizing)
 */
export function canvasToSvg(canvas, resolve = (c) => c, opts = {}) {
  const content = typeof opts.content === "function" ? opts.content : () => null;
  const foreignOf = typeof opts.foreign === "function" ? opts.foreign : () => null;
  const editMode = Boolean(opts.editMode);
  const structOptsOf = typeof opts.structOpts === "function" ? opts.structOpts : () => ({ editMode });
  /** Background of a node or the canvas: theme token, custom color, or a preset tinted into the token. */
  const bgOf = (id, base) => {
    const b = resolve(base);
    if (!id) return b;
    if (isCustomColor(id)) return id.toLowerCase();
    const acc = resolve(accentVar(id));
    return mixColors(acc, b, BG_TINT) || acc;
  };
  const C = {
    bg: bgOf(canvas.settings?.bgColor || "", "var(--bg-canvas)"),
    panel: resolve("var(--bg-secondary)"),
    head: resolve("var(--bg-tertiary)"),
    code: resolve("var(--bg-primary)"),
    text: resolve("var(--text-primary)"),
    muted: resolve("var(--text-secondary)"),
    faint: resolve("var(--text-muted)"),
    border: resolve("var(--border-color)"),
    rule: resolve("var(--border-muted)"),
    type: resolve("var(--syn-type)"),
    keyword: resolve("var(--syn-keyword)"),
    preproc: resolve("var(--syn-preproc)"),
    number: resolve("var(--syn-number)"),
    note: resolve("var(--accent-yellow)"),
  };
  const anchors = new Map(); // node id -> Map(field key -> canvas y)
  let defs = "";
  let clipN = 0;
  const clip = (x, y, w, h) => {
    const id = `nmclip${clipN++}`;
    defs += `<clipPath id="${id}"><rect x="${r1(x)}" y="${r1(y)}" width="${r1(Math.max(0, w))}" height="${r1(Math.max(0, h))}"/></clipPath>`;
    return id;
  };

  /* ui-scale-exempt-start (NodeMap generated SVG: canvas units, zoom-controlled) */
  const txt = (x, y, s, { fill = C.text, size = 11, family = "sans-serif", weight = "", anchor = "", italic = false, opacity = 1 } = {}) =>
    `<text x="${r1(x)}" y="${r1(y)}" fill="${fill}" font-family="${family}" font-size="${size}"${weight ? ` font-weight="${weight}"` : ""}${anchor ? ` text-anchor="${anchor}"` : ""}${italic ? ' font-style="italic"' : ""}${opacity !== 1 ? ` opacity="${opacity}"` : ""} xml:space="preserve">${s}</text>`;

  /** Struct body rows (local coordinates); records field anchors for the edges. */
  const structBody = (n, box, top, accent) => {
    const layout = content(n);
    const d = n.data;
    if (!layout) return txt(10, top + 16, esc(`${d.kind} ${d.name}`), { fill: C.faint });
    const so = structOptsOf(n) || {};
    const editMode = Boolean(so.editMode);
    const model = structRows(layout, d, so);
    const table = d.style === "table";
    let s = "";
    let y = top;
    // Toolbar (Edit Mode only): the struct-wide chips and the hidden count; the source is in the header
    if (editMode) {
      s += `<line x1="0" y1="${y + BAR_H}" x2="${box.w}" y2="${y + BAR_H}" stroke="${C.rule}"/>`;
      let cx = 8;
      const directives = countDirectives(layout.items);
      for (const [label, on, enabled] of [["Types", d.showTypes, true], [directives ? `#ifdef ${directives}` : "#ifdef", d.showGuards, directives > 0]]) {
        const w = label.length * 6 + 12;
        const chip = `<rect x="${cx}" y="${y + 4}" width="${w}" height="14" rx="7" fill="${on ? accent : "none"}" fill-opacity="${on ? 0.14 : 0}" stroke="${on ? accent : C.border}"/>` +
          txt(cx + w / 2, y + 14.5, esc(label), { fill: on ? C.text : C.muted, size: 10, family: "monospace", anchor: "middle" });
        s += enabled ? chip : `<g opacity="0.45">${chip}</g>`;
        cx += w + 4;
      }
      if (model.hidden) {
        const label = `${model.hidden} hidden`;
        const w = label.length * 6 + 12;
        s += `<rect x="${cx}" y="${y + 4}" width="${w}" height="14" rx="7" fill="none" stroke="${C.border}" stroke-dasharray="3 2"/>` +
          txt(cx + w / 2, y + 14.5, esc(label), { fill: C.faint, size: 10, family: "monospace", anchor: "middle" });
      }
      y += BAR_H;
    }
    // Outer conditions: left out when hidden in View Mode; in Edit Mode a hidden banner dims (rows do not)
    if (layout.outer_conds?.length && (!model.outerHidden || editMode)) {
      const banner = txt(8, y + 12, esc(fit(layout.outer_conds.join(" && "), Math.floor((box.w - 16) / 6))), { fill: C.preproc, size: 10, family: "monospace" });
      s += model.outerHidden ? `<g opacity="0.45">${banner}</g>` : banner;
      s += `<line x1="0" y1="${y + OUTER_H}" x2="${box.w}" y2="${y + OUTER_H}" stroke="${C.rule}"/>`;
      y += OUTER_H;
    }
    const rowsTop = y + 2;
    const rowsBottom = box.h - 2;
    const id = clip(0, rowsTop, box.w, rowsBottom - rowsTop);
    const gutter = model.tracks ? model.tracks * TRACK_W + TRACK_W : 0;
    // Table style with tracks: the scope bars have their own cell, closed by a column divider
    const cellRight = table && model.tracks ? gutter + 10 : 0;
    const ctlW = table ? (editMode ? 30 : 14) : 0;
    const typeW = model.typeCh * MONO_CH;
    const rowH = table ? TABLE_ROW_H : ROW_H;
    const fieldY = new Map();
    s += `<g clip-path="url(#${id})">`;
    if (!model.rows.length) s += txt(10, rowsTop + 12, "No members", { fill: C.faint, italic: true });
    if (cellRight && model.rows.length) s += `<line x1="${cellRight}" y1="${rowsTop}" x2="${cellRight}" y2="${r1(rowsTop + model.rows.length * rowH)}" stroke="${C.rule}"/>`;
    model.rows.forEach((r, i) => {
      const ry = rowsTop + i * rowH;
      const mid = ry + rowH / 2;
      const base = ry + rowH / 2 + 4;
      const op = r.dim ? 0.45 : r.dead ? 0.6 : 1;
      let row = "";
      for (const [l, part] of r.bars) {
        const bx = 6 + l * TRACK_W + 1;
        const y1 = part === "start" ? mid : ry;
        const y2 = part === "end" ? mid : ry + rowH;
        const alpha = [0.9, 0.7, 0.55, 0.4][l % 4];
        row += `<line x1="${bx}" y1="${r1(y1)}" x2="${bx}" y2="${r1(y2)}" stroke="${C.preproc}" stroke-opacity="${alpha}" stroke-width="2"/>`;
        if (part === "start" || part === "end" || part === "mid") row += `<line x1="${bx}" y1="${r1(mid)}" x2="${bx + 4}" y2="${r1(mid)}" stroke="${C.preproc}" stroke-opacity="${alpha}" stroke-width="2"/>`;
      }
      if (table) row += `<line x1="0" y1="${ry + rowH}" x2="${box.w}" y2="${ry + rowH}" stroke="${C.rule}"/>`;
      const x0 = cellRight ? cellRight + 4 : 6 + gutter + (table ? 0 : r.depth * 14);
      const lineNo = r.line ? txt(box.w - 8, base, esc(r.line), { fill: C.faint, size: 9, family: "monospace", anchor: "end" }) : "";
      if (r.kind === "pp" || r.kind === "summary") {
        const chev = r.count ? (r.folded ? "▸ " : "▾ ") : "";
        const extra = r.kind === "summary" ? ` · ${r.count} member${r.count === 1 ? "" : "s"}${r.auto ? " · compiled out" : " · folded"}` : "";
        const px = (table ? x0 + ctlW + 4 : x0) + (r.ppLevel || 0) * PP_INDENT;
        row += txt(px, base, `${esc(chev)}${esc(fit(r.text + extra, Math.floor((box.w - px - 40) / MONO_CH)))}`, { fill: C.preproc, family: "monospace", italic: true });
      } else {
        fieldY.set(r.key, mid);
        const label = r.kind === "agg" ? `${r.name}${r.folded ? ` {${r.count}}` : ""}` : `${r.name}${r.suffix || ""}`;
        const lead = r.kind === "agg" ? (r.folded ? "▸ " : "▾ ") : "";
        if (table) {
          const tx = x0 + ctlW;
          row += txt(x0, base, esc(lead), { fill: C.faint, family: "monospace" });
          const typeText = r.kind === "agg" ? `${r.tag}${r.typeText ? ` ${r.typeText}` : ""}` : r.type;
          if (typeText) row += txt(tx + 4 + r.depth * 2 * MONO_CH, base, esc(fit(typeText, model.typeCh - r.depth * 2)), { fill: r.kind === "agg" ? C.keyword : C.type, family: "monospace" });
          row += `<line x1="${r1(tx + typeW + 8)}" y1="${ry}" x2="${r1(tx + typeW + 8)}" y2="${ry + rowH}" stroke="${C.rule}"/>`;
          const nx = tx + typeW + 12;
          row += txt(nx, base, `<tspan fill="${C.text}">${esc(r.kind === "agg" ? label : r.name)}</tspan>${r.kind === "field" && r.suffix ? `<tspan fill="${C.number}">${esc(r.suffix)}</tspan>` : ""}`, { family: "monospace", weight: r.kind === "agg" ? "bold" : "" });
        } else {
          let spans = lead ? `<tspan fill="${C.faint}">${esc(lead)}</tspan>` : "";
          if (r.kind === "agg") {
            spans += `<tspan fill="${C.keyword}">${esc(r.tag)} </tspan>`;
            if (r.typeText) spans += `<tspan fill="${C.type}">${esc(r.typeText)} </tspan>`;
            spans += `<tspan fill="${C.text}" font-weight="bold">${esc(label)}</tspan>`;
          } else {
            if (r.showType && r.type) spans += `<tspan fill="${C.type}">${esc(r.type)} </tspan>`;
            spans += `<tspan fill="${C.text}">${esc(r.name)}</tspan>`;
            if (r.suffix) spans += `<tspan fill="${C.number}">${esc(r.suffix)}</tspan>`;
          }
          if (r.conds.length && !d.showGuards) spans += `<tspan fill="${C.preproc}" font-size="9"> #</tspan>`;
          row += txt(x0, base, spans, { family: "monospace" });
        }
      }
      row += lineNo;
      s += op !== 1 ? `<g opacity="${op}">${row}</g>` : row;
    });
    s += "</g>";
    // Field anchors: the row's middle, kept inside the visible rows (as the live view does)
    const ys = new Map();
    for (const [k, my] of fieldY) ys.set(k, Math.min(Math.max(my, rowsTop + 4), rowsBottom - 4));
    anchors.set(n.id, ys);
    return s;
  };

  const sourceBody = (n, box, top) => {
    const r = content(n);
    const d = n.data;
    if (!r) return txt(10, top + 16, esc(d.path || "Source"), { fill: C.faint });
    // The code area keeps the theme's code background unless the node has its own (as in nodemap.css)
    let s = n.bgColor ? "" : `<rect x="0" y="${top}" width="${box.w}" height="${box.h - top}" fill="${C.code}"/>`;
    const y0 = top;
    const lines = Array.isArray(r.lines) ? r.lines : [];
    const max = Math.max(0, Math.ceil((box.h - y0) / SRC_ROW_H));
    const noW = Math.max(5, String(r.start_line + lines.length).length + 1) * MONO_CH;
    const hl = r.symbol && (r.symbol.line_s > r.start_line || r.symbol.line_e < r.end_line) ? r.symbol : null;
    const id = clip(0, y0, box.w, box.h - y0);
    s += `<g clip-path="url(#${id})">`;
    for (let i = 0; i < Math.min(lines.length, max); i++) {
      const no = r.start_line + i;
      const ly = y0 + i * SRC_ROW_H;
      if (hl && no >= hl.line_s && no <= hl.line_e) s += `<rect x="0" y="${r1(ly)}" width="${box.w}" height="${SRC_ROW_H}" fill="${C.note}" fill-opacity="0.12"/>`;
      s += txt(noW, ly + 12, esc(no), { fill: C.faint, family: "monospace", anchor: "end" });
      s += txt(noW + 8, ly + 12, esc(String(lines[i]).replace(/\t/g, "        ")), { family: "monospace" });
    }
    s += "</g>";
    return s;
  };

  const noteBody = (n, box, top) => {
    const cols = Math.max(8, Math.floor((box.w - 20) / SANS_CH));
    const lines = wrap(String(n.data.text || "").split(/\r?\n/).filter((l) => !/^```/.test(l.trim())).map(plainNoteLine).join("\n"), cols);
    const max = Math.floor((box.h - top - 8) / 16);
    if (!lines.length || (lines.length === 1 && !lines[0])) return txt(10, top + 18, "(empty note)", { fill: C.faint, italic: true });
    let s = "";
    lines.slice(0, max).forEach((l, i) => {
      s += txt(10, top + 18 + i * 16, esc(l), { fill: C.text });
    });
    return s;
  };

  const symbolBody = (n, box, top) => {
    const d = n.data;
    if (!d.expanded) return txt(10, top + 18, esc(`${d.kind === "func" ? "Function" : "Symbol"} · ${n.version}`), { fill: C.faint });
    const list = d.constituents || [];
    if (!list.length) return txt(10, top + 18, "No members found", { fill: C.faint, italic: true });
    const max = Math.floor((box.h - top - 4) / ROW_H);
    let s = "";
    list.slice(0, max).forEach((c, i) => {
      s += txt(8, top + 14 + i * ROW_H, esc(fit(c.name, Math.floor((box.w - 16) / MONO_CH))), { family: "monospace" });
    });
    return s;
  };

  /** Placeholder card body: what a live view shows cannot be vectorized. */
  const cardBody = (box, top, lines) => {
    let s = "";
    lines.forEach(([t, o], i) => {
      s += txt(box.w / 2, top + (box.h - top) / 2 - ((lines.length - 1) * 16) / 2 + i * 16, esc(fit(t, Math.floor((box.w - 20) / 6))), { fill: o?.fill || C.muted, size: o?.size || 11, family: o?.family || "sans-serif", anchor: "middle", italic: o?.italic });
    });
    return s;
  };

  const nodeSvg = (n) => {
    const accent = resolve(accentVar(n.color, n.type === "group" ? "var(--border-color)" : n.type === "note" ? "var(--accent-yellow)" : "var(--accent-blue)"));
    if (n.type === "group") {
      return `<g><rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="8" fill="${accent}" fill-opacity="0.06" stroke="${accent}" stroke-dasharray="6 4"/>` +
        `${txt(n.x + 10, n.y + 18, esc(displayTitle(n)), { weight: "bold", size: 12 })}</g>\n`;
    }
    const box = layoutBox(n);
    const tf = box.s !== 1 ? `translate(${n.x} ${n.y}) scale(${box.s})` : `translate(${n.x} ${n.y})`;
    let s = `<g transform="${tf}">`;
    const panel = n.bgColor ? bgOf(n.bgColor, "var(--bg-secondary)") : C.panel;
    s += `<rect x="0" y="0" width="${box.w}" height="${box.h}" rx="6" fill="${panel}" stroke="${n.color ? accent : C.border}" stroke-width="1"/>`;
    if (n.type === "note" && !n.bgColor) s += `<rect x="0" y="0" width="${box.w}" height="${box.h}" rx="6" fill="${accent}" fill-opacity="0.08"/>`;
    // Header (accent strip, type badge, title, source reference): none for a hidden header in View
    // Mode, dimmed in Edit Mode
    const headShown = !n.hideHeader || editMode;
    if (headShown) {
      let h = "";
      const hid = clip(0, 0, box.w, HEAD_H);
      h += `<g clip-path="url(#${hid})"><rect x="0" y="0" width="${box.w}" height="${HEAD_H + 6}" rx="6" fill="${C.head}"/>` +
        `<rect x="0" y="0" width="${box.w}" height="3" fill="${n.color ? accent : C.rule}"/></g>`;
      h += `<line x1="0" y1="${HEAD_H}" x2="${box.w}" y2="${HEAD_H}" stroke="${C.border}"/>`;
      const badge = BADGES[n.type] || n.type;
      const bw = badge.length * 5.6 + 8;
      h += `<rect x="8" y="8" width="${r1(bw)}" height="13" rx="3" fill="${accent}" fill-opacity="0.16"/>`;
      h += txt(8 + bw / 2, 17.5, esc(badge.toUpperCase()), { fill: accent, size: 9, anchor: "middle" });
      const meta = headMetaOf(n.type, ["struct", "source"].includes(n.type) ? content(n) : null).text;
      // The reference takes at most 55% of the header (as .nm-head-meta), keeping its end (line numbers)
      const metaCols = meta ? Math.min(meta.length, Math.floor((box.w * 0.55) / 6)) : 0;
      if (meta) {
        const shown = meta.length > metaCols ? `…${meta.slice(meta.length - metaCols + 1)}` : meta;
        h += txt(box.w - 8, 17.5, esc(shown), { fill: C.faint, size: 10, family: "monospace", anchor: "end" });
      }
      h += txt(16 + bw, 18, esc(fit(displayTitle(n), Math.floor((box.w - bw - 28 - (metaCols ? metaCols * 6 + 8 : 0)) / 6.6))), { weight: "bold", size: 12 });
      s += n.hideHeader ? `<g opacity="0.45">${h}</g>` : h;
    }
    const top = headShown ? HEAD_H : 0;
    const bid = clip(0, top, box.w, box.h - top);
    let body = "";
    if (n.type === "struct") body = structBody(n, box, top, accent);
    else if (n.type === "source") body = sourceBody(n, box, top);
    else if (n.type === "note") body = noteBody(n, box, top);
    else if (n.type === "symbol") body = symbolBody(n, box, top);
    const fo = n.type === "tab" || n.type === "man" ? foreignOf(n) : null;
    if (fo?.html) {
      // Live rendered content: XHTML snapshot, clipped to the body like the node shows it
      const bh = Math.max(0, box.h - top);
      body = `<foreignObject x="0" y="${top}" width="${box.w}" height="${bh}"><div xmlns="http://www.w3.org/1999/xhtml" style="width:${box.w}px;height:${bh}px;overflow:hidden">${String(fo.html).replace(XML_ILLEGAL, "")}</div></foreignObject>`;
    } else if (n.type === "tab") {
      const hash = String(n.data.hash || "");
      body = cardBody(box, top, [[`Live ${n.data.tabType || "KernelInfo"} tab`, { fill: C.text, size: 12 }], [`#${hash}`, { fill: C.faint, size: 10, family: "monospace" }], ["(embedded view: not part of the image)", { fill: C.faint, size: 10, italic: true }]]);
    } else if (n.type === "man") {
      const d = n.data;
      body = cardBody(box, top, [[d.name ? `${d.name}(${d.section || "?"})` : (d.path || "Man page"), { fill: C.text, size: 12 }], [d.path ? `${d.repo ? `${d.repo} · ` : ""}${d.path}` : "", { fill: C.faint, size: 10, family: "monospace" }]]);
    }
    s += `<g clip-path="url(#${bid})">${body}</g>`;
    s += `<rect x="0" y="0" width="${box.w}" height="${box.h}" rx="6" fill="none" stroke="${n.color ? accent : C.border}" stroke-width="1.5"/>`;
    return `${s}</g>\n`;
  };

  // Nodes first (struct rows record field anchors), painted groups < edges < nodes
  const ordered = paintOrder(canvas.nodes);
  const groupsSvg = ordered.filter((x) => x.type === "group").map(nodeSvg).join("");
  const nodesSvg = ordered.filter((x) => x.type !== "group").map(nodeSvg).join("");
  const byId = new Map(canvas.nodes.map((n) => [n.id, n]));
  const pointOf = (n, field, side) => {
    const y = field ? anchors.get(n.id)?.get(field) : undefined;
    if (y === undefined) return null;
    const s = n.autoFit ? n.scale || 1 : 1;
    return { x: side === "left" ? n.x : n.x + n.w, y: n.y + y * s };
  };
  let edgesSvg = "";
  const extra = [];
  for (const e of canvas.edges) {
    const a = byId.get(e.from);
    const b = byId.get(e.to);
    if (!a || !b) continue;
    const fs = e.fromSide === "left" ? "left" : "right";
    const ts = e.toSide === "left" ? "left" : "right";
    const p1 = pointOf(a, e.fromField, fs);
    const p2 = pointOf(b, e.toField, ts);
    const ends = resolveEnds({ rect: a, side: p1 ? fs : e.fromSide, point: p1 }, { rect: b, side: p2 ? ts : e.toSide, point: p2 });
    const g = edgePath(ends.p1, ends.p2, ends.fromSide, ends.toSide, e.curve);
    const heads = arrowHeads(g, ends.p1, ends.p2, e.arrow);
    extra.push(ends.p1, ends.p2, g.mid, ...(g.controls || []));
    if (e.label) {
      // The label's extent (11-unit sans text centered above the midpoint)
      const half = (e.label.length * SANS_CH) / 2 + 4;
      extra.push({ x: g.mid.x - half, y: g.mid.y - 20 }, { x: g.mid.x + half, y: g.mid.y });
    }
    const c = resolve(accentVar(e.color, "var(--accent-blue)"));
    const dash = dashArray(e.style);
    edgesSvg += `<path d="${g.d}" fill="none" stroke="${c}" stroke-width="2"${dash ? ` stroke-dasharray="${dash}"` : ""}/>\n`;
    for (const h of heads) edgesSvg += `<polygon points="${h}" fill="${c}"/>\n`;
    // The canvas-colored halo keeps a label readable over wires; a transparent image has no canvas color
    const halo = opts.transparent ? "" : ` stroke="${C.bg}" stroke-width="4" paint-order="stroke"`;
    if (e.label) edgesSvg += `<text x="${r1(g.mid.x)}" y="${r1(g.mid.y - 6)}" text-anchor="middle" fill="${C.text}"${halo} font-family="sans-serif" font-size="11">${esc(e.label)}</text>\n`;
  }
  /* ui-scale-exempt-end */

  const nb = boundsOf(canvas.nodes) || { x: 0, y: 0, w: 400, h: 300 };
  let x1 = nb.x, y1 = nb.y, x2 = nb.x + nb.w, y2 = nb.y + nb.h;
  for (const p of extra) {
    x1 = Math.min(x1, p.x);
    y1 = Math.min(y1, p.y);
    x2 = Math.max(x2, p.x);
    y2 = Math.max(y2, p.y);
  }
  const vx = Math.floor(x1 - PAD), vy = Math.floor(y1 - PAD);
  const vw = Math.ceil(x2 - x1 + PAD * 2), vh = Math.ceil(y2 - y1 + PAD * 2);
  const ps = Number(opts.pixelScale) > 0 ? Number(opts.pixelScale) : 1;
  // Same rounding as pngSize(): the rasterized document is exactly the PNG's size
  const pw = ps === 1 ? vw : Math.max(1, Math.floor(vw * ps)), ph = ps === 1 ? vh : Math.max(1, Math.floor(vh * ps));
  let s = `<svg xmlns="http://www.w3.org/2000/svg" width="${pw}" height="${ph}" viewBox="${vx} ${vy} ${vw} ${vh}">\n`;
  if (defs) s += `<defs>${defs}</defs>\n`;
  if (!opts.transparent) s += `<rect x="${vx}" y="${vy}" width="${vw}" height="${vh}" fill="${C.bg}"/>\n`;
  s += groupsSvg + edgesSvg + nodesSvg;
  return `${s}</svg>`;
}

// ------------------------------------------------------------------ image sizes

/** PNG scale factors the export dialog offers (default `PNG_DEFAULT_SCALE`). */
export const PNG_SCALES = [1, 2, 3, 4];
export const PNG_DEFAULT_SCALE = 3;
/** Largest PNG side and area browsers rasterize into a `<canvas>` reliably. */
export const PNG_MAX_SIDE = 16384;
export const PNG_MAX_AREA = 268435456;

/** `{w, h}` of an SVG document's root `width` / `height` (0 when absent). */
export function svgSize(svg) {
  const m = /<svg\b[^>]*?\bwidth="([\d.]+)"[^>]*?\bheight="([\d.]+)"/.exec(String(svg || ""));
  return m ? { w: Number(m[1]), h: Number(m[2]) } : { w: 0, h: 0 };
}

/**
 * Output size of a `w`×`h` image upscaled by `scale`, clamped so neither side exceeds `maxSide` and the
 * area stays under `maxArea`: `{w, h, scale, clamped}` (`scale` is the factor actually used).
 */
export function pngSize(w, h, scale = PNG_DEFAULT_SCALE, maxSide = PNG_MAX_SIDE, maxArea = PNG_MAX_AREA) {
  const want = Number(scale) > 0 ? Number(scale) : 1;
  if (!(w > 0 && h > 0)) return { w: 0, h: 0, scale: want, clamped: false };
  const s = Math.min(want, maxSide / w, maxSide / h, Math.sqrt(maxArea / (w * h)));
  return { w: Math.max(1, Math.floor(w * s)), h: Math.max(1, Math.floor(h * s)), scale: s, clamped: s < want };
}

// ------------------------------------------------------------------ DOM snapshots (man pages, tabs)

// Computed properties inlined onto every cloned element: the snapshot needs no stylesheet
const FO_PROPS = [
  "display", "position", "top", "right", "bottom", "left", "box-sizing", "width", "min-width", "max-width",
  "margin-top", "margin-right", "margin-bottom", "margin-left", "padding-top", "padding-right", "padding-bottom", "padding-left",
  "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
  "border-top-style", "border-right-style", "border-bottom-style", "border-left-style",
  "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
  "border-top-left-radius", "border-top-right-radius", "border-bottom-right-radius", "border-bottom-left-radius",
  "background-color", "background-image", "color", "font-family", "font-size", "font-weight", "font-style",
  "line-height", "letter-spacing", "text-align", "text-decoration-line", "text-decoration-color", "text-transform",
  "text-indent", "text-overflow", "white-space", "word-break", "overflow-wrap", "vertical-align", "opacity",
  "visibility", "flex-direction", "flex-wrap", "flex-grow", "flex-shrink", "flex-basis", "justify-content",
  "align-items", "align-self", "row-gap", "column-gap", "grid-template-columns", "grid-template-rows",
  "grid-column-start", "grid-column-end", "grid-row-start", "grid-row-end", "list-style-type",
  "list-style-position", "table-layout", "border-collapse", "border-spacing", "tab-size",
  "fill", "stroke", "stroke-width", "overflow-x", "overflow-y",
];
// Never part of a snapshot: scripts and resources, nested frames, transient overlays and controls
const FO_DROP_TAGS = new Set(["script", "noscript", "style", "link", "meta", "template", "iframe", "frame", "object", "embed", "audio", "video"]);
const FO_DROP_SEL = ".nm-shield, .context-menu, .tooltip, .toast, .toast-container, [role='tooltip'], .loading-overlay, .spinner";
// Larger subtrees are not snapshotted (placeholder card): inlining styles is main-thread work per element
const FO_MAX_ELEMENTS = 8000;
// Plain names only: a prefixed (`xlink:href`, `xml:lang`) or `xmlns` attribute of an HTML element would
// serialize with an unbound prefix and break the whole document
const ATTR_NAME_RE = /^(?!xmlns)[A-Za-z_][\w.-]*$/i;

/** Data URL of a same-origin image or canvas (null when it is cross-origin or not loaded). */
function dataUrlOf(src, doc) {
  try {
    if (src.localName === "canvas") return src.toDataURL("image/png");
    const url = src.currentSrc || src.src || "";
    if (url.startsWith("data:")) return url;
    if (!src.complete || !src.naturalWidth) return null;
    const c = doc.createElement("canvas");
    c.width = src.naturalWidth;
    c.height = src.naturalHeight;
    c.getContext("2d").drawImage(src, 0, 0);
    return c.toDataURL("image/png");
  } catch {
    return null; // tainted (cross-origin): left out, the export must not taint the PNG canvas
  }
}

/** Clone of `src` (an element of `win`'s document) with computed styles inlined; null to leave it out. */
function cloneStyled(src, win, budget) {
  if (src.nodeType === 3) return src.cloneNode(false);
  if (src.nodeType !== 1 || budget.n-- <= 0) return null;
  const tag = String(src.localName || "").toLowerCase();
  if (FO_DROP_TAGS.has(tag)) return null;
  try {
    if (src.matches?.(FO_DROP_SEL)) return null;
  } catch {
    // selector unsupported: keep the element
  }
  const cs = win.getComputedStyle(src);
  if (cs.getPropertyValue("display") === "none") return null;
  const doc = src.ownerDocument;
  let el;
  if (tag === "canvas" || tag === "img") {
    const url = dataUrlOf(src, doc);
    if (!url) return null;
    el = doc.createElement("img");
    el.setAttribute("src", url);
    el.setAttribute("alt", "");
    el.setAttribute("width", String(src.width || src.clientWidth || 0));
    el.setAttribute("height", String(src.height || src.clientHeight || 0));
  } else {
    el = src.cloneNode(false);
    for (const a of [...(el.attributes || [])]) {
      const name = a.name.toLowerCase();
      // Event handlers, ids (they would collide across snapshots), resources and invalid XML names go
      if (name.startsWith("on") || name === "id" || name === "style" || name === "srcset" || !ATTR_NAME_RE.test(a.name)) el.removeAttribute(a.name);
      else if ((name === "src" || name === "poster") && !String(a.value).startsWith("data:")) el.removeAttribute(a.name);
    }
    if (tag === "input" || tag === "textarea") el.setAttribute("value", String(src.value ?? ""));
  }
  let style = "";
  for (const p of FO_PROPS) {
    let v = cs.getPropertyValue(p);
    if (!v) continue;
    if (p === "background-image" && /url\(/i.test(v)) continue; // external images: not same-origin data
    if ((p === "overflow-x" || p === "overflow-y") && (v === "auto" || v === "scroll")) v = "hidden";
    style += `${p}:${v};`;
  }
  if (style) el.setAttribute("style", style);
  if (tag === "canvas" || tag === "img" || tag === "textarea") {
    if (tag === "textarea") el.textContent = String(src.value ?? "");
    return el;
  }
  let host = el;
  // A scrolled box shows what it scrolled to: its content moves up / left inside it
  const st = src.scrollTop || 0;
  const sl = src.scrollLeft || 0;
  if ((st || sl) && !/^(flex|inline-flex|grid|inline-grid|table)/.test(cs.getPropertyValue("display"))) {
    host = doc.createElement("div");
    host.setAttribute("style", `transform:translate(${-sl}px,${-st}px)`);
    el.appendChild(host);
  }
  for (const child of src.childNodes) {
    const c = cloneStyled(child, win, budget);
    if (c) host.appendChild(c);
  }
  return el;
}

/**
 * The live rendered content of a man (`.nm-man`) or tab node (the same-origin `?embed=1` iframe's
 * `#workspace`) as an XHTML string for `canvasToSvg`'s `opts.foreign`: a clone with computed styles
 * inlined, scripts / frames / overlays left out, canvases and same-origin images as data URLs, sized to
 * the node body. `{html, w, h}` (body size, layout units), or null when nothing is mounted (an unloaded
 * node, a cross-origin or not yet loaded frame, no DOM).
 */
export function captureNodeForeignObject(node, el) {
  try {
    const body = el?.querySelector?.(".nm-node-body");
    if (!body || typeof XMLSerializer === "undefined") return null;
    let root = null;
    let win = null;
    if (node.type === "man") {
      root = body.querySelector(".nm-man");
      win = el.ownerDocument?.defaultView || globalThis;
    } else if (node.type === "tab") {
      const frame = body.querySelector("iframe");
      const doc = frame?.contentDocument;
      win = frame?.contentWindow;
      if (!doc || !win || doc.readyState === "loading") return null;
      root = doc.querySelector("#workspace") || doc.body;
    }
    if (!root || !win?.getComputedStyle) return null;
    const budget = { n: FO_MAX_ELEMENTS };
    const clone = cloneStyled(root, win, budget);
    // A truncated snapshot would misstate the content: the placeholder card stands in for it
    if (!clone || budget.n < 0) return null;
    const w = body.offsetWidth || root.offsetWidth || 0;
    const h = body.offsetHeight || root.offsetHeight || 0;
    let bg = win.getComputedStyle(root).getPropertyValue("background-color");
    if (!bg || bg === "transparent" || /rgba\([^)]*,\s*0\)$/.test(bg)) bg = win.getComputedStyle(root.ownerDocument.body).getPropertyValue("background-color");
    const base = clone.getAttribute("style") || "";
    clone.setAttribute("style", `${base}margin:0;width:${w}px;height:${h}px;max-width:none;overflow:hidden;box-sizing:border-box;${bg ? `background-color:${bg};` : ""}`);
    const html = new XMLSerializer().serializeToString(clone).replace(XML_ILLEGAL, "");
    return { html, w, h };
  } catch {
    return null;
  }
}
