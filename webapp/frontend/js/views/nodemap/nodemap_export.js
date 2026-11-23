/**
 * nodemap_export.js - SVG (and, through it, PNG) of a whole NodeMap canvas (`webapp/API_FRONTEND.md` §5.3).
 *
 * Respects visibility layers (`visibleCanvasSlice`), edge waypoints, struct `layout` (pahole
 * cache-line offsets/holes), and all 16 node types including `cve`, `kapi`, `doc`, `history` and `commit`.
 * `canvasToSvg()` and the size helpers are DOM-free (unit tested).
 */
import { BG_TINT, NODE_BADGES, accentVar, boundsOf, colorToHex, contentBox, displayTitle, formatLockBadge, isCustomColor, paintOrder, visibleCanvasSlice } from "./canvas_model.js";
import { arrowHeads, dashArray, edgePath, resolveEnds } from "./edge_router.js";
import { headMetaOf, sourceLineSegments } from "./node_content.js";
import { commitRowKey, diffRows, kconfigValue, mailBadges, mailLineClass, mailRowKey, maintainerLines, releaseRowKey, statusClass, valueClass } from "./domain_nodes.js";
import { countDirectives, esc as escHtml, paholeRows, structRows } from "./struct_view.js";

export const EXPORT_METRICS = {
  PAD: 40,
  HEAD_H: 26,
  BAR_H: 22,
  OUTER_H: 16,
  ROW_H: 16,
  TABLE_ROW_H: 18,
  LAYOUT_ROW_H: 18,
  SRC_ROW_H: 16.5,
  DIFF_ROW_H: 16.5,
  PP_INDENT: 10,
  ROW_BORDER: 2,
  MONO_CH: 6.6,
  SANS_CH: 6.2,
  TRACK_W: 6,
  MAIL_ROW_H: 18,
  MAIL_META_H: 14,
  MAIL_BODY_BOX_H: 320,
  KC_ROW_H: 18,
  MT_ROW_H: 16,
  GUIDE_W: 12,
};

const {
  PAD, HEAD_H, BAR_H, OUTER_H, ROW_H, TABLE_ROW_H, LAYOUT_ROW_H, SRC_ROW_H,
  PP_INDENT, ROW_BORDER, MONO_CH, SANS_CH, TRACK_W, DIFF_ROW_H, MAIL_ROW_H, MAIL_META_H, MAIL_BODY_BOX_H,
  KC_ROW_H, MT_ROW_H, GUIDE_W,
} = EXPORT_METRICS;

/** Type-default accents (nodemap.css): groups are blue like every node, notes yellow. */
export function defaultAccent(type) {
  return type === "note" ? "var(--accent-yellow)" : "var(--accent-blue)";
}

const r1 = (v) => Math.round(v * 10) / 10;
const XML_ILLEGAL = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\uFFFE\uFFFF]/g;
const esc = (s) => escHtml(String(s ?? "").replace(XML_ILLEGAL, ""));

function mixColors(a, b, t) {
  const ha = colorToHex(a);
  const hb = colorToHex(b);
  if (!ha || !hb) return null;
  const ch = (h, i) => parseInt(h.slice(1 + i * 2, 3 + i * 2), 16);
  return `#${[0, 1, 2].map((i) => Math.round(ch(ha, i) * t + ch(hb, i) * (1 - t)).toString(16).padStart(2, "0")).join("")}`;
}

function layoutBox(n) {
  return contentBox(n);
}

function fit(text, max) {
  const t = String(text ?? "");
  return max > 1 && t.length > max ? `${t.slice(0, Math.max(1, max - 1))}…` : t;
}

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

function plainNoteLine(line) {
  return line.replace(/^#{1,3}\s+/, "").replace(/^\s*[-*]\s+\[( |x|X)\]\s+/, (m, x) => (x === " " ? "☐ " : "☑ ")).replace(/^\s*[-*]\s+/, "• ")
    .replace(/^>\s?/, "│ ").replace(/\*\*([^*]+)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1").replace(/~~([^~]+)~~/g, "$1");
}

/**
 * Standalone SVG document of the canvas.
 */
export function canvasToSvg(canvas, resolve = (c) => c, opts = {}) {
  const visCanvas = visibleCanvasSlice(canvas);
  const content = typeof opts.content === "function" ? opts.content : () => null;
  const foreignOf = typeof opts.foreign === "function" ? opts.foreign : () => null;
  const editMode = Boolean(opts.editMode);
  const structOptsOf = typeof opts.structOpts === "function" ? opts.structOpts : () => ({ editMode });
  const rowHeightsOf = typeof opts.rowHeights === "function" ? opts.rowHeights : () => null;
  const kcValuesOf = typeof opts.kconfigValues === "function" ? opts.kconfigValues : () => null;
  const bgOf = (id, base) => {
    const b = resolve(base);
    if (!id) return b;
    if (isCustomColor(id)) return id.toLowerCase();
    const acc = resolve(accentVar(id));
    return mixColors(acc, b, BG_TINT) || acc;
  };
  const C = {
    bg: bgOf(visCanvas.settings?.bgColor || "", "var(--bg-canvas)"),
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
    add: resolve("var(--diff-add-fg)"),
    del: resolve("var(--diff-del-fg)"),
    green: resolve("var(--accent-green)"),
    red: resolve("var(--accent-red)"),
    orange: resolve("var(--accent-orange)"),
    blue: resolve("var(--accent-blue)"),
    cyan: resolve("var(--accent-cyan)"),
    purple: resolve("var(--accent-purple)"),
  };
  const TOK = {
    "tok-keyword": "var(--syn-keyword)", "tok-type": "var(--syn-type)", "tok-string": "var(--syn-string)",
    "tok-number": "var(--syn-number)", "tok-comment": "var(--syn-comment)", "tok-preproc": "var(--syn-preproc)",
    "tok-macro": "var(--syn-preproc)", "tok-include": "var(--syn-string)", "tok-punct": "var(--syn-punct)",
    "tok-function": "var(--syn-function)", "tok-identifier": "var(--syn-identifier)", "tok-variable": "var(--syn-variable)",
    "tok-attribute": "var(--syn-attribute)", "tok-lifetime": "var(--syn-variable)",
  };
  const tokColor = new Map();
  const colorOfTok = (cls) => {
    if (!TOK[cls]) return C.text;
    if (!tokColor.has(cls)) tokColor.set(cls, resolve(TOK[cls]));
    return tokColor.get(cls);
  };
  let used = 0;
  const anchors = new Map();
  let defs = "";
  let clipN = 0;
  const clip = (x, y, w, h) => {
    const id = `nmclip${clipN++}`;
    defs += `<clipPath id="${id}"><rect x="${r1(x)}" y="${r1(y)}" width="${r1(Math.max(0, w))}" height="${r1(Math.max(0, h))}"/></clipPath>`;
    return id;
  };
  const clipRound = (w, h) => {
    const id = `nmclip${clipN++}`;
    defs += `<clipPath id="${id}"><rect x="0" y="0" width="${r1(Math.max(0, w))}" height="${r1(Math.max(0, h))}" rx="6"/></clipPath>`;
    return id;
  };

  /* ui-scale-exempt-start (NodeMap generated SVG: canvas units, zoom-controlled) */
  const txt = (x, y, s, { fill = C.text, size = 11, family = "sans-serif", weight = "", anchor = "", italic = false, opacity = 1 } = {}) =>
    `<text x="${r1(x)}" y="${r1(y)}" fill="${fill}" font-family="${family}" font-size="${size}"${weight ? ` font-weight="${weight}"` : ""}${anchor ? ` text-anchor="${anchor}"` : ""}${italic ? ' font-style="italic"' : ""}${opacity !== 1 ? ` opacity="${opacity}"` : ""} xml:space="preserve">${s}</text>`;

  const rowsClip = (box, top, inner) => {
    const id = clip(0, top, box.w, box.h - top);
    return `<g clip-path="url(#${id})">${inner}</g>`;
  };

  const structLayoutBody = (n, box, top, so) => {
    const pahole = so.pahole || content(n)?.pahole || null;
    if (!pahole || pahole.unavailable) {
      used = top + 24;
      return txt(10, top + 16, "Memory layout (pahole) unavailable", { fill: C.faint, italic: true });
    }
    const m = paholeRows(pahole, n.data);
    let s = "";
    let y = top;
    const sum = `${m.totalSize}B · align ${m.alignment}B · ${m.cacheLines} CL${m.paddingBytes ? ` · ${m.paddingBytes}B pad (${m.holesCount} holes)` : ""}`;
    s += `<rect x="0" y="${y}" width="${box.w}" height="${BAR_H}" fill="${C.head}"/>`;
    s += txt(8, y + 14.5, esc(fit(sum, Math.floor((box.w - 16) / 6))), { fill: C.muted, size: 10, family: "monospace" });
    s += `<line x1="0" y1="${y + BAR_H}" x2="${box.w}" y2="${y + BAR_H}" stroke="${C.rule}"/>`;
    y += BAR_H;
    const rowsTop = y + 2;
    const rowsBottom = box.h - 2;
    const fieldY = new Map();
    let inner = "";
    let ry = rowsTop;
    for (const r of m.rows) {
      const mid = ry + LAYOUT_ROW_H / 2;
      const base = ry + LAYOUT_ROW_H / 2 + 4;
      if (r.kind === "cacheline") {
        inner += `<rect x="0" y="${r1(ry)}" width="${box.w}" height="${LAYOUT_ROW_H}" fill="${C.blue}" fill-opacity="0.08"/>`;
        inner += `<line x1="0" y1="${r1(ry)}" x2="${box.w}" y2="${r1(ry)}" stroke="${C.blue}" stroke-opacity="0.4" stroke-dasharray="4 3"/>`;
        inner += txt(8, base, esc(`Cache line ${r.cl} · +${r.offset}B`), { fill: C.blue, size: 10, family: "monospace", weight: "bold" });
      } else if (r.kind === "hole") {
        inner += `<rect x="0" y="${r1(ry)}" width="${box.w}" height="${LAYOUT_ROW_H}" fill="${C.orange}" fill-opacity="0.1"/>`;
        inner += txt(8, base, esc(`+${r.offset}`), { fill: C.orange, size: 10, family: "monospace" });
        inner += txt(52, base, esc(`${r.size}B`), { fill: C.orange, size: 10, family: "monospace" });
        inner += txt(92, base, esc(`/* ${r.size}B padding hole */`), { fill: C.orange, size: 10, family: "monospace", italic: true });
      } else {
        fieldY.set(r.key, mid);
        inner += `<line x1="0" y1="${r1(ry + LAYOUT_ROW_H)}" x2="${box.w}" y2="${r1(ry + LAYOUT_ROW_H)}" stroke="${C.rule}"/>`;
        inner += txt(8, base, esc(`+${r.offset}`), { fill: C.faint, size: 10, family: "monospace" });
        inner += txt(52, base, esc(`${r.size}B`), { fill: C.number, size: 10, family: "monospace" });
        const tx = 92;
        if (r.type) inner += txt(tx, base, esc(fit(r.type, m.typeCh)), { fill: C.type, family: "monospace" });
        const nx = tx + (r.type ? m.typeCh * MONO_CH + 6 : 0);
        const lkSpan = r.lock?.kind ? `<tspan fill="${C.orange}"> [${esc(formatLockBadge(r.lock))}]</tspan>` : "";
        inner += txt(nx, base, `${esc(`${r.name}${r.bitfield || ""}`)}${lkSpan}`, { fill: C.text, family: "monospace" });
      }
      ry += LAYOUT_ROW_H;
    }
    const visBottom = n.autoFit ? Math.min(rowsBottom, ry) : rowsBottom;
    const ys = new Map();
    for (const [k, my] of fieldY) ys.set(k, Math.min(Math.max(my, rowsTop + 4), visBottom - 4));
    anchors.set(n.id, ys);
    used = m.rows.length ? ry + 2 : rowsTop + 18;
    const cid = clip(0, rowsTop, box.w, rowsBottom - rowsTop);
    return `${s}<g clip-path="url(#${cid})">${inner}</g>`;
  };

  /** Struct body rows (local coordinates); records field anchors for the edges. */
  const structBody = (n, box, top, accent) => {
    const layout = content(n);
    const d = n.data;
    const so = structOptsOf(n) || {};
    if (d.style === "layout") return structLayoutBody(n, box, top, so);
    if (!layout) return txt(10, top + 16, esc(`${d.kind} ${d.name}`), { fill: C.faint });
    const editMode = Boolean(so.editMode);
    const model = structRows(layout, d, so);
    const table = d.style === "table";
    let s = "";
    let y = top;
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
    if (layout.outer_conds?.length && (!model.outerHidden || editMode)) {
      const banner = txt(8, y + 12, esc(fit(layout.outer_conds.join(" && "), Math.floor((box.w - 16) / 6))), { fill: C.preproc, size: 10, family: "monospace" });
      s += model.outerHidden ? `<g opacity="0.45">${banner}</g>` : banner;
      s += `<line x1="0" y1="${y + OUTER_H}" x2="${box.w}" y2="${y + OUTER_H}" stroke="${C.rule}"/>`;
      y += OUTER_H;
    }
    const rowsTop = y + 2;
    const rowsBottom = box.h - 2;
    const id = clip(0, rowsTop, box.w, rowsBottom - rowsTop);
    // Same geometry as nodemap.css: rows start after a 2px left border; the #if cell spans
    // [2, gutter + 6) with a 1px right border and a 4px flex gap before the controls column
    const gutter = model.tracks ? model.tracks * TRACK_W + TRACK_W : 0;
    const cellRight = table && model.tracks ? gutter + 5.5 : 0;
    const ctlW = table ? (editMode ? 30 : 14) : 0;
    const typeW = model.typeCh * MONO_CH;
    const rowH = table ? TABLE_ROW_H : ROW_H;
    const measured = rowHeightsOf(n);
    const hs = Array.isArray(measured) && measured.length === model.rows.length && measured.every((v) => v > 0) ? measured : null;
    const rowTops = [];
    let acc = rowsTop;
    for (let i = 0; i < model.rows.length; i++) {
      rowTops.push(acc);
      acc += hs ? hs[i] : rowH;
    }
    const rowsEnd = acc;
    used = model.rows.length ? rowsEnd + 2 : rowsTop + 18;
    const fieldY = new Map();
    s += `<g clip-path="url(#${id})">`;
    if (!model.rows.length) s += txt(10, rowsTop + 12, "No members", { fill: C.faint, italic: true });
    if (cellRight && model.rows.length) s += `<line x1="${cellRight}" y1="${rowsTop}" x2="${cellRight}" y2="${r1(rowsEnd)}" stroke="${C.rule}"/>`;
    model.rows.forEach((r, i) => {
      const ry = rowTops[i];
      const rh = hs ? hs[i] : rowH;
      const mid = ry + rh / 2;
      const base = ry + rh / 2 + 4;
      const op = r.dim ? 0.45 : r.dead ? 0.6 : 1;
      let row = "";
      for (const [l, part] of r.bars) {
        const bx = ROW_BORDER + 7 + l * TRACK_W;
        const y1 = part === "start" ? mid : ry;
        const y2 = part === "end" ? mid : ry + rh;
        const alpha = [0.9, 0.7, 0.55, 0.4][l % 4];
        row += `<line x1="${bx}" y1="${r1(y1)}" x2="${bx}" y2="${r1(y2)}" stroke="${C.preproc}" stroke-opacity="${alpha}" stroke-width="2"/>`;
        if (part === "start" || part === "end" || part === "mid") row += `<line x1="${bx - 1}" y1="${r1(mid)}" x2="${bx + 4}" y2="${r1(mid)}" stroke="${C.preproc}" stroke-opacity="${alpha}" stroke-width="2"/>`;
      }
      if (table) row += `<line x1="0" y1="${r1(ry + rh)}" x2="${box.w}" y2="${r1(ry + rh)}" stroke="${C.rule}"/>`;
      const x0 = cellRight ? cellRight + 4.5 : ROW_BORDER + 4 + gutter + (table ? 0 : r.depth * 14);
      const lineNo = r.line ? txt(box.w - 8, base, esc(r.line), { fill: C.faint, size: 9, family: "monospace", anchor: "end" }) : "";
      if (r.kind === "pp" || r.kind === "summary") {
        const chev = r.count ? (r.folded ? "▸ " : "▾ ") : "";
        const extra = r.kind === "summary" ? ` · ${r.count} member${r.count === 1 ? "" : "s"}${r.auto ? " · compiled out" : " · folded"}` : "";
        const ix = x0 + (r.ppLevel || 0) * PP_INDENT;
        if (table) {
          // chevron in the controls column, text after it (the indent steps both in, like the live row)
          const px = ix + ctlW + 6;
          if (chev) row += txt(ix, base, esc(chev), { fill: C.faint, family: "monospace" });
          row += txt(px, base, esc(fit(r.text + extra, Math.floor((box.w - px - 40) / MONO_CH))), { fill: C.preproc, family: "monospace", italic: true });
        } else row += txt(ix, base, `${esc(chev)}${esc(fit(r.text + extra, Math.floor((box.w - ix - 40) / MONO_CH)))}`, { fill: C.preproc, family: "monospace", italic: true });
      } else {
        fieldY.set(r.key, mid);
        const label = r.kind === "agg" ? `${r.name}${r.folded ? ` {${r.count}}` : ""}` : `${r.name}${r.suffix || ""}`;
        const lead = r.kind === "agg" ? (r.folded ? "▸ " : "▾ ") : "";
        if (table) {
          const tx = x0 + ctlW;
          row += txt(x0, base, esc(lead), { fill: C.faint, family: "monospace" });
          const typeText = r.kind === "agg" ? `${r.tag}${r.typeText ? ` ${r.typeText}` : ""}` : r.type;
          if (typeText) row += txt(tx + 4 + r.depth * 2 * MONO_CH, base, esc(fit(typeText, model.typeCh - r.depth * 2)), { fill: r.kind === "agg" ? C.keyword : C.type, family: "monospace" });
          row += `<line x1="${r1(tx + typeW + 10.5)}" y1="${r1(ry)}" x2="${r1(tx + typeW + 10.5)}" y2="${r1(ry + rh)}" stroke="${C.rule}"/>`;
          const nx = tx + typeW + 17;
          const lkSpan = r.lock?.kind ? `<tspan fill="${C.orange}"> [${esc(formatLockBadge(r.lock))}]</tspan>` : "";
          row += txt(nx, base, `<tspan fill="${C.text}">${esc(r.kind === "agg" ? label : r.name)}</tspan>${r.kind === "field" && r.suffix ? `<tspan fill="${C.number}">${esc(r.suffix)}</tspan>` : ""}${lkSpan}`, { family: "monospace", weight: r.kind === "agg" ? "bold" : "" });
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
          if (r.lock?.kind) spans += `<tspan fill="${C.orange}"> [${esc(formatLockBadge(r.lock))}]</tspan>`;
          if (r.conds.length && !d.showGuards) spans += `<tspan fill="${C.preproc}" font-size="9"> #</tspan>`;
          row += txt(x0, base, spans, { family: "monospace" });
        }
      }
      row += lineNo;
      s += op !== 1 ? `<g opacity="${op}">${row}</g>` : row;
    });
    s += "</g>";
    const ys = new Map();
    const visBottom = n.autoFit ? Math.min(rowsBottom, rowsEnd) : rowsBottom;
    for (const [k, my] of fieldY) ys.set(k, Math.min(Math.max(my, rowsTop + 4), visBottom - 4));
    anchors.set(n.id, ys);
    return s;
  };

  const sourceBody = (n, box, top) => {
    const r = content(n);
    const d = n.data;
    if (!r) return txt(10, top + 16, esc(d.path || "Source"), { fill: C.faint });
    let s = n.bgColor ? "" : `<rect x="0" y="${top}" width="${box.w}" height="${box.h - top}" fill="${C.code}"/>`;
    const y0 = top;
    const lines = Array.isArray(r.lines) ? r.lines : [];
    const max = Math.max(0, Math.ceil((box.h - y0) / SRC_ROW_H));
    const noW = Math.max(5, String(r.start_line + lines.length).length + 1) * MONO_CH;
    const hl = r.symbol && (r.symbol.line_s > r.start_line || r.symbol.line_e < r.end_line) ? r.symbol : null;
    const id = clip(0, y0, box.w, box.h - y0);
    s += `<g clip-path="url(#${id})">`;
    const shown = Math.min(lines.length, max);
    const segs = sourceLineSegments(lines.slice(0, shown).map((l) => String(l ?? "")), r.path || d.path);
    for (let i = 0; i < shown; i++) {
      const no = r.start_line + i;
      const ly = y0 + i * SRC_ROW_H;
      if (hl && no >= hl.line_s && no <= hl.line_e) s += `<rect x="0" y="${r1(ly)}" width="${box.w}" height="${SRC_ROW_H}" fill="${C.note}" fill-opacity="0.12"/>`;
      s += txt(noW, ly + 12, esc(no), { fill: C.faint, family: "monospace", anchor: "end" });
      const spans = (segs[i] || []).map((g) => {
        const t = esc(g.text.replace(/\t/g, "        "));
        if (!t) return "";
        const italic = g.cls === "tok-comment" || g.cls === "tok-lifetime" ? ' font-style="italic"' : "";
        return g.cls ? `<tspan fill="${colorOfTok(g.cls)}"${italic}>${t}</tspan>` : t;
      }).join("");
      s += txt(noW + 8, ly + 12, spans, { family: "monospace" });
    }
    s += "</g>";
    used = y0 + shown * SRC_ROW_H + 2;
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

  const diffBody = (n, box, top) => {
    const p = content(n);
    const d = n.data;
    if (!p) return txt(10, top + 16, esc(d.path || (d.commitHash ? d.commitHash.slice(0, 12) : "Diff")), { fill: C.faint });
    const rows = diffRows(p);
    const max = Math.max(0, Math.ceil((box.h - top) / DIFF_ROW_H));
    let maxNo = 1;
    for (const r of rows) maxNo = Math.max(maxNo, r.old || 0, r.new || 0);
    const noW = Math.max(3, String(maxNo).length) * MONO_CH + 8;
    let s = n.bgColor ? "" : `<rect x="0" y="${top}" width="${box.w}" height="${box.h - top}" fill="${C.code}"/>`;
    let inner = "";
    const shown = Math.min(rows.length, max);
    for (let i = 0; i < shown; i++) {
      const r = rows[i];
      const ly = top + i * DIFF_ROW_H;
      const base = ly + 12;
      if (r.kind === "file") {
        inner += `<rect x="0" y="${r1(ly)}" width="${box.w}" height="${DIFF_ROW_H}" fill="${C.head}"/>` + txt(8, base, esc(r.text), { fill: C.text, family: "monospace", weight: "bold" });
      } else if (r.kind === "hunk") {
        inner += `<rect x="0" y="${r1(ly)}" width="${box.w}" height="${DIFF_ROW_H}" fill="${C.blue}" fill-opacity="0.08"/>` + txt(8, base, esc(r.text), { fill: C.preproc, family: "monospace" });
      } else if (r.kind === "note") {
        inner += txt(8, base, esc(r.text), { fill: C.faint, family: "monospace", italic: true });
      } else {
        const color = r.kind === "+" ? C.add : r.kind === "-" ? C.del : C.text;
        if (r.kind !== " ") inner += `<rect x="0" y="${r1(ly)}" width="${box.w}" height="${DIFF_ROW_H}" fill="${color}" fill-opacity="0.12"/>`;
        inner += txt(noW - 4, base, esc(r.old ?? ""), { fill: C.faint, family: "monospace", anchor: "end", size: 10 });
        inner += txt(noW * 2 - 4, base, esc(r.new ?? ""), { fill: C.faint, family: "monospace", anchor: "end", size: 10 });
        inner += txt(noW * 2 + 4, base, `${esc(r.kind === " " ? " " : r.kind)} ${esc(String(r.text).replace(/\t/g, "        "))}`, { fill: color, family: "monospace" });
      }
    }
    s += rowsClip(box, top, inner);
    used = top + Math.max(1, shown) * DIFF_ROW_H + 2;
    return s;
  };

  /** Clamp row anchors to the visible rows area and record them for the edges (`fromField`). */
  const setRowAnchors = (n, box, top, rowY, end) => {
    const lo = top + 4;
    const hi = (n.autoFit ? Math.min(box.h, end) : box.h) - 4;
    const ys = new Map();
    for (const [k, y] of rowY) ys.set(k, Math.min(Math.max(y, lo), Math.max(lo, hi)));
    anchors.set(n.id, ys);
  };

  const mailBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.subject || n.data.mid || "Mail thread"), { fill: C.faint });
    if (!model.rows.length) return txt(10, top + 16, "No messages", { fill: C.faint, italic: true });
    let inner = "";
    let y = top + 4;
    const cols = (x) => Math.max(4, Math.floor((box.w - x - 10) / SANS_CH));
    const lineColor = { "nm-ml-add": C.add, "nm-ml-del": C.del, "nm-ml-hunk": C.cyan, "nm-ml-file": C.text, "nm-ml-sig": C.faint };
    const rowY = new Map();
    for (const r of model.rows) {
      const gx = 8;
      const mid = y + MAIL_ROW_H / 2;
      rowY.set(mailRowKey(r.mid), mid);
      // a thread scrolls each whole message in a box of MAIL_BODY_BOX_H (nodemap.css), a single message grows
      const bodyLines = r.full && r.loaded ? r.body.split("\n") : null;
      const boxed = bodyLines && !(model.single && r.focus) ? bodyLines.slice(0, Math.floor(MAIL_BODY_BOX_H / 14)) : bodyLines;
      const lines = !r.open ? [] : r.full ? (boxed || ["Loading…"]) : (r.excerpt.length ? r.excerpt : ["…"]);
      const rowEnd = y + MAIL_ROW_H + MAIL_META_H + (r.open ? Math.max(1, lines.length) * 14 + 4 : 0);
      if (y > box.h) {
        y = rowEnd;
        continue;
      }
      r.guides.forEach((g, k) => {
        const x = gx + k * GUIDE_W + GUIDE_W / 2;
        if (g === "pipe" || g === "tee") inner += `<line x1="${x}" y1="${r1(y)}" x2="${x}" y2="${r1(rowEnd)}" stroke="${C.border}"/>`;
        if (g === "end") inner += `<line x1="${x}" y1="${r1(y)}" x2="${x}" y2="${r1(mid)}" stroke="${C.border}"/>`;
        if (g === "tee" || g === "end") inner += `<line x1="${x}" y1="${r1(mid)}" x2="${x + GUIDE_W / 2}" y2="${r1(mid)}" stroke="${C.border}"/>`;
      });
      let x = gx + r.guides.length * GUIDE_W;
      if (r.focus) inner += `<rect x="0" y="${r1(y)}" width="${box.w}" height="${MAIL_ROW_H + MAIL_META_H}" fill="${C.blue}" fill-opacity="0.1"/>`;
      for (const b of mailBadges(r)) {
        const bw = b.length * 5.6 + 8;
        const col = b === "cover" ? C.orange : b === "PATCH" ? C.green : C.blue;
        inner += `<rect x="${r1(x)}" y="${r1(y + 3)}" width="${r1(bw)}" height="12" rx="3" fill="${col}" fill-opacity="0.16"/>` + txt(x + bw / 2, y + 12, esc(b), { fill: col, size: 9, anchor: "middle" });
        x += bw + 4;
      }
      inner += txt(x, y + 13, esc(fit(r.subject, cols(x))), { fill: C.text, weight: r.depth === 0 ? "bold" : "" });
      const mx = gx + r.guides.length * GUIDE_W;
      inner += txt(mx, y + MAIL_ROW_H + 9, esc(fit(`${r.author}${r.date ? ` · ${r.date}` : ""}`, cols(mx))), { fill: C.faint, size: 10 });
      if (r.open) {
        const state = { patch: false, sig: false };
        const w = Math.floor((box.w - mx - 16) / MONO_CH);
        lines.forEach((l, k) => {
          const ly = y + MAIL_ROW_H + MAIL_META_H + 12 + k * 14;
          const cls = r.full ? mailLineClass(l, state) : "";
          if (ly > box.h + 14) return;
          const fill = cls.startsWith("nm-ml-q") ? C.green : lineColor[cls] || C.muted;
          inner += txt(mx + 6, ly, esc(fit(l, w)), { fill, size: 10, family: "monospace", italic: cls.startsWith("nm-ml-q") });
        });
      }
      y = rowEnd;
    }
    used = y + 4;
    setRowAnchors(n, box, top, rowY, used);
    return rowsClip(box, top, inner);
  };

  const kconfigBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(`CONFIG_${n.data.symbol || "?"}`), { fill: C.faint });
    const values = kcValuesOf(n);
    const value = values ? kconfigValue(values, model.name) : null;
    let inner = "";
    let y = top + 4;
    inner += txt(8, y + 13, esc(`CONFIG_${model.name}`), { fill: C.text, family: "monospace", weight: "bold" });
    let x = 8 + (model.name.length + 7) * MONO_CH + 8;
    if (model.type) {
      inner += txt(x, y + 13, esc(model.type), { fill: C.type, family: "monospace", size: 10 });
      x += model.type.length * 6 + 10;
    }
    const vText = value === null ? "no engine" : value || "unset";
    const vCls = value === null ? "none" : valueClass(value);
    const vCol = { y: C.green, m: C.orange, n: C.red, val: C.blue, unset: C.faint, none: C.faint }[vCls];
    const vw = vText.length * 6 + 12;
    inner += `<rect x="${r1(x)}" y="${y + 3}" width="${r1(vw)}" height="14" rx="7" fill="${vCol}" fill-opacity="0.18" stroke="${vCol}"/>` + txt(x + vw / 2, y + 13.5, esc(vText), { fill: vCol, size: 10, family: "monospace", anchor: "middle" });
    y += KC_ROW_H + 2;
    if (model.prompt) {
      inner += txt(8, y + 12, esc(fit(model.prompt, Math.floor((box.w - 16) / SANS_CH))), { fill: C.muted, italic: true });
      y += KC_ROW_H;
    }
    for (const g of model.groups) {
      inner += txt(8, y + 12, esc(g.label), { fill: C.faint, size: 10, weight: "bold" });
      y += 16;
      let cx = 8;
      for (const name of g.names) {
        const cw = name.length * 6 + 12;
        if (cx + cw > box.w - 8 && cx > 8) {
          cx = 8;
          y += KC_ROW_H;
        }
        inner += `<rect x="${r1(cx)}" y="${r1(y + 1)}" width="${r1(cw)}" height="15" rx="7" fill="none" stroke="${C.border}"/>` + txt(cx + cw / 2, y + 12, esc(name), { fill: C.text, size: 10, family: "monospace", anchor: "middle" });
        cx += cw + 4;
      }
      y += KC_ROW_H + 2;
    }
    if (!model.groups.length) {
      inner += txt(8, y + 12, "No relations", { fill: C.faint, italic: true });
      y += KC_ROW_H;
    }
    used = y + 4;
    return rowsClip(box, top, inner);
  };

  const maintainerBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.title || n.data.path || n.data.query || "Maintainers"), { fill: C.faint });
    let inner = "";
    let y = top + 4;
    inner += txt(8, y + 13, esc(fit(model.title || "(section)", Math.floor((box.w - 120) / 6.6))), { fill: C.text, weight: "bold", size: 12 });
    if (model.status) {
      const col = { supported: C.green, maintained: C.blue, odd: C.orange, orphan: C.red, other: C.faint }[statusClass(model.status)];
      const sw = model.status.length * 5.8 + 12;
      inner += `<rect x="${r1(box.w - 8 - sw)}" y="${y + 3}" width="${r1(sw)}" height="14" rx="7" fill="${col}" fill-opacity="0.16" stroke="${col}"/>` + txt(box.w - 8 - sw / 2, y + 13.5, esc(model.status), { fill: col, size: 10, anchor: "middle" });
    }
    y += KC_ROW_H + 4;
    const tagColor = { M: C.blue, R: C.type, L: C.green, T: C.preproc, W: C.muted, F: C.number, X: C.red };
    for (const [tag, v] of maintainerLines(model)) {
      if (y > box.h) break;
      inner += txt(8, y + 12, `${tag}:`, { fill: tagColor[tag] || C.faint, family: "monospace", weight: "bold" });
      inner += txt(8 + 3 * MONO_CH, y + 12, esc(fit(v, Math.floor((box.w - 16 - 3 * MONO_CH) / MONO_CH))), { fill: C.text, family: "monospace" });
      y += MT_ROW_H;
    }
    used = y + 4;
    return rowsClip(box, top, inner);
  };

  const cveBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.cveId || n.data.filePath || "CVE"), { fill: C.faint });
    let inner = "";
    let y = top + 6;
    const cols = Math.floor((box.w - 16) / SANS_CH);
    if (model.mode === "file") {
      inner += txt(8, y + 12, esc(fit(`${model.filePath} (${model.affectedCount}/${model.total})`, cols)), { fill: C.text, weight: "bold" });
      y += 18;
      for (const c of model.cves) {
        if (y > box.h) break;
        const sc = c.state === "affected" ? C.red : c.state === "fixed" ? C.green : C.orange;
        inner += txt(8, y + 12, esc(c.state), { fill: sc, size: 10, family: "monospace" });
        inner += txt(66, y + 12, esc(fit(`${c.cveId} ${c.title}`, cols - 10)), { fill: C.text, size: 10 });
        y += 16;
      }
    } else {
      inner += txt(8, y + 12, esc(`${model.cveId}${model.score != null ? ` · CVSS ${model.score}` : ""}${model.state ? ` · ${model.state}` : ""}`), { fill: model.state === "affected" ? C.red : C.text, weight: "bold" });
      y += 18;
      if (model.title) {
        for (const l of wrap(model.title, cols).slice(0, 3)) {
          inner += txt(8, y + 12, esc(l), { fill: C.muted });
          y += 15;
        }
      }
      for (const rg of model.ranges) {
        if (y > box.h) break;
        const intro = rg.introduced || rg.inferred[0]?.sha || "";
        if (intro) {
          inner += txt(8, y + 12, esc(`intro ${intro.slice(0, 10)} ${fit(rg.introducedSubject || "", cols - 18)}`), { fill: C.orange, size: 10, family: "monospace" });
          y += 16;
        }
        if (rg.fixed) {
          inner += txt(8, y + 12, esc(`fix   ${rg.fixed.slice(0, 10)} ${fit(rg.fixedSubject || "", cols - 18)}`), { fill: C.green, size: 10, family: "monospace" });
          y += 16;
        }
      }
      for (const f of model.files.slice(0, 8)) {
        if (y > box.h) break;
        inner += txt(8, y + 12, esc(fit(`• ${f}`, cols)), { fill: C.faint, size: 10, family: "monospace" });
        y += 15;
      }
    }
    used = y + 4;
    return rowsClip(box, top, inner);
  };

  const kapiBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.symbol || n.data.catalog || "Kernel API"), { fill: C.faint });
    let inner = "";
    let y = top + 6;
    const cols = Math.floor((box.w - 16) / MONO_CH);
    inner += txt(8, y + 12, esc(`${model.catalog}${model.arch ? ` (${model.arch})` : ""} · ${model.total} entries`), { fill: C.blue, weight: "bold", family: "monospace" });
    y += 18;
    for (const it of model.items) {
      if (y > box.h) break;
      inner += txt(8, y + 12, esc(fit(`[${it.kind}] ${it.name}${it.subtitle ? ` — ${it.subtitle}` : ""}`, cols)), { fill: C.text, size: 10, family: "monospace" });
      y += 16;
    }
    used = y + 4;
    return rowsClip(box, top, inner);
  };

  const docBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.path || "Kernel Docs"), { fill: C.faint });
    let inner = "";
    let y = top + 6;
    const cols = Math.floor((box.w - 16) / SANS_CH);
    if (model.mode === "kernel-doc") {
      for (const e of model.entries) {
        if (y > box.h) break;
        inner += txt(8, y + 12, esc(fit(`${e.kind} ${e.name}${e.brief ? ` — ${e.brief}` : ""}`, cols)), { fill: C.text, weight: "bold" });
        y += 16;
        if (e.signature) {
          inner += txt(12, y + 12, esc(fit(e.signature, cols)), { fill: C.type, size: 10, family: "monospace" });
          y += 15;
        }
      }
    } else {
      for (const sec of model.sections) {
        if (y > box.h) break;
        inner += txt(8, y + 12, esc(fit(sec.heading, cols)), { fill: C.text, weight: "bold" });
        y += 17;
        for (const l of wrap(sec.text, cols).slice(0, 4)) {
          if (y > box.h) break;
          inner += txt(8, y + 12, esc(l), { fill: C.muted, size: 10 });
          y += 14;
        }
      }
    }
    used = y + 4;
    return rowsClip(box, top, inner);
  };

  const historyBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.name || n.data.path || "History"), { fill: C.faint });
    let inner = "";
    let y = top + 6;
    const cols = Math.floor((box.w - 16) / MONO_CH);
    const rowY = new Map();
    inner += txt(8, y + 12, esc(fit(model.mode === "blame" ? `${model.path || "Blame"} · ${model.commits.length} commits` : `${model.name || model.path || "History"} · ${model.changeCount} changes`, cols)), { fill: C.muted, size: 10, weight: "bold" });
    if (model.mode === "blame") {
      y += 20; // the path / count head line
      for (const c of model.commits) {
        rowY.set(commitRowKey(c.commitHash), y + 8);
        if (y <= box.h) inner += txt(8, y + 12, esc(fit(`${c.commitHash.slice(0, 8)} ${c.author}: ${c.subject}`, cols)), { fill: C.text, size: 10, family: "monospace" });
        y += 16;
      }
    } else {
      y += 20;
      for (const r of model.rows) {
        rowY.set(releaseRowKey(r.vname), y + 8);
        const col = r.status === "modified" ? C.orange : r.status === "introduced" || r.status === "added" ? C.green : r.status === "removed" ? C.red : C.muted;
        if (y <= box.h) inner += txt(8, y + 12, esc(fit(`${r.vname} [${r.status}]${r.added || r.removed ? ` +${r.added} -${r.removed}` : ""}`, cols)), { fill: col, size: 10, family: "monospace" });
        y += 16;
      }
    }
    used = y + 4;
    setRowAnchors(n, box, top, rowY, used);
    return rowsClip(box, top, inner);
  };

  const commitBody = (n, box, top) => {
    const model = content(n);
    if (!model?.hash) return txt(10, top + 16, esc(n.data.commitHash ? `Commit ${n.data.commitHash.slice(0, 12)}` : "Commit"), { fill: C.faint });
    let inner = "";
    let y = top + 6;
    const cols = Math.floor((box.w - 16) / SANS_CH);
    const mcols = Math.floor((box.w - 16) / MONO_CH);
    const line = (s, o = {}) => {
      if (y <= box.h) inner += txt(8, y + 12, esc(s), o);
      y += o.step || 15;
    };
    for (const l of wrap(model.subject || "(no subject)", cols).slice(0, 3)) line(l, { weight: "bold", step: 17 });
    line(fit(`commit ${model.hash}`, mcols), { fill: C.blue, size: 10, family: "monospace" });
    const who = (label, p) => p && (p.name || p.email) && line(fit(`${label} ${p.name}${p.email ? ` <${p.email}>` : ""}${p.date ? ` · ${p.date}` : ""}`, mcols), { fill: C.muted, size: 10, family: "monospace" });
    who("Author:", model.author);
    who("Commit:", model.committer);
    if (model.parents.length) line(fit(`Parent${model.parents.length > 1 ? "s" : ""}: ${model.parents.map((p) => p.slice(0, 12)).join(" ")}`, mcols), { fill: C.muted, size: 10, family: "monospace" });
    y += 4;
    for (const l of String(model.body || "").split("\n").slice(0, 40)) {
      if (y > box.h) break;
      for (const w of wrap(l, mcols).slice(0, 4)) line(w || " ", { fill: C.text, size: 10, family: "monospace", step: 14 });
    }
    for (const t of model.trailers) line(fit(`${t.key}: ${t.value}`, mcols), { fill: C.green, size: 10, family: "monospace", step: 14 });
    y += 4;
    line(fit(`Files (${model.files.length}${model.filesTruncated ? "+" : ""})${model.fileSummary ? ` · ${model.fileSummary}` : ""}`, cols), { fill: C.muted, size: 10, weight: "bold" });
    for (const f of model.files) {
      if (y > box.h) break;
      line(fit(`[${f.status}] ${f.path}`, mcols), { fill: f.status === "A" ? C.green : f.status === "D" ? C.red : C.text, size: 10, family: "monospace", step: 14 });
    }
    used = y + 4;
    return rowsClip(box, top, inner);
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

  const cardBody = (box, top, lines) => {
    let s = "";
    lines.forEach(([t, o], i) => {
      s += txt(box.w / 2, top + (box.h - top) / 2 - ((lines.length - 1) * 16) / 2 + i * 16, esc(fit(t, Math.floor((box.w - 20) / 6))), { fill: o?.fill || C.muted, size: o?.size || 11, family: o?.family || "sans-serif", anchor: "middle", italic: o?.italic });
    });
    return s;
  };

  const rects = new Map();
  const nodeSvg = (n) => {
    const accent = resolve(accentVar(n.color, defaultAccent(n.type)));
    if (n.type === "group") {
      rects.set(n.id, { x: n.x, y: n.y, w: n.w, h: n.h });
      const grpLocks = Array.isArray(n.locks) && n.locks.length
        ? ` · ${n.locks.map((l) => formatLockBadge(l)).join(" · ")}`
        : "";
      return `<g><rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="8" fill="${accent}" fill-opacity="0.06" stroke="${accent}" stroke-dasharray="${n.data?.subCanvas ? "3 2" : "6 4"}"/>` +
        `${txt(n.x + 10, n.y + 18, esc(`${displayTitle(n)}${grpLocks}`), { weight: "bold", size: 12 })}</g>\n`;
    }
    let box = layoutBox(n);
    const headShown = !n.hideHeader || editMode;
    const nodeLocks = Array.isArray(n.locks) ? n.locks : [];
    const lockStripH = nodeLocks.length ? 18 : 0;
    const top = (headShown ? HEAD_H : 0) + lockStripH;
    used = 0;
    let body = "";
    if (n.type === "struct") body = structBody(n, box, top, accent);
    else if (n.type === "source") body = sourceBody(n, box, top);
    else if (n.type === "note") body = noteBody(n, box, top);
    else if (n.type === "symbol") body = symbolBody(n, box, top);
    else if (n.type === "diff") body = diffBody(n, box, top);
    else if (n.type === "mail") body = mailBody(n, box, top);
    else if (n.type === "kconfig") body = kconfigBody(n, box, top);
    else if (n.type === "maintainer") body = maintainerBody(n, box, top);
    else if (n.type === "cve") body = cveBody(n, box, top);
    else if (n.type === "kapi") body = kapiBody(n, box, top);
    else if (n.type === "doc") body = docBody(n, box, top);
    else if (n.type === "history") body = historyBody(n, box, top);
    else if (n.type === "commit") body = commitBody(n, box, top);
    if (n.autoFit && used > top && used < box.h) box = { ...box, h: Math.ceil(used) };
    rects.set(n.id, { x: n.x, y: n.y, w: n.w, h: Math.round(box.h * box.s) });
    const tf = box.s !== 1 ? `translate(${n.x} ${n.y}) scale(${box.s})` : `translate(${n.x} ${n.y})`;
    let s = `<g transform="${tf}">`;
    const panel = n.bgColor ? bgOf(n.bgColor, "var(--bg-secondary)") : C.panel;
    const rid = clipRound(box.w, box.h);
    s += `<g clip-path="url(#${rid})">`;
    s += `<rect x="0" y="0" width="${box.w}" height="${box.h}" fill="${panel}"/>`;
    if (n.type === "note" && !n.bgColor) s += `<rect x="0" y="0" width="${box.w}" height="${box.h}" fill="${accent}" fill-opacity="0.08"/>`;
    if (headShown) {
      let h = `<rect x="0" y="0" width="${box.w}" height="${HEAD_H}" fill="${C.head}"/>` +
        `<rect x="0" y="0" width="${box.w}" height="3" fill="${n.color ? accent : C.rule}"/>`;
      h += `<line x1="0" y1="${HEAD_H}" x2="${box.w}" y2="${HEAD_H}" stroke="${C.border}"/>`;
      const badge = NODE_BADGES[n.type] || n.type;
      const bw = badge.length * 5.6 + 8;
      h += `<rect x="8" y="8" width="${r1(bw)}" height="13" rx="3" fill="${accent}" fill-opacity="0.16"/>`;
      h += txt(8 + bw / 2, 17.5, esc(badge.toUpperCase()), { fill: accent, size: 9, anchor: "middle" });
      const meta = headMetaOf(n.type, n.type === "symbol" || n.type === "note" || n.type === "tab" || n.type === "man" ? null : content(n)).text;
      const metaCols = meta ? Math.min(meta.length, Math.floor((box.w * 0.55) / 6)) : 0;
      if (meta) {
        const shown = meta.length > metaCols ? `…${meta.slice(meta.length - metaCols + 1)}` : meta;
        h += txt(box.w - 8, 17.5, esc(shown), { fill: C.faint, size: 10, family: "monospace", anchor: "end" });
      }
      h += txt(16 + bw, 18, esc(fit(displayTitle(n), Math.floor((box.w - bw - 28 - (metaCols ? metaCols * 6 + 8 : 0)) / 6.6))), { weight: "bold", size: 12 });
      s += n.hideHeader ? `<g opacity="0.45">${h}</g>` : h;
    }
    if (lockStripH) {
      const ly = headShown ? HEAD_H : 0;
      s += `<rect x="0" y="${ly}" width="${box.w}" height="${lockStripH}" fill="${C.head}"/>`;
      s += `<line x1="0" y1="${ly + lockStripH}" x2="${box.w}" y2="${ly + lockStripH}" stroke="${C.rule}"/>`;
      let lx = 8;
      for (const lk of nodeLocks) {
        const label = formatLockBadge(lk);
        const lw = label.length * 5.8 + 10;
        if (lx + lw > box.w - 4 && lx > 8) break;
        s += `<rect x="${r1(lx)}" y="${ly + 2}" width="${r1(lw)}" height="14" rx="7" fill="${C.orange}" fill-opacity="0.16" stroke="${C.orange}"/>`;
        s += txt(lx + lw / 2, ly + 12, esc(label), { fill: C.orange, size: 9, family: "monospace", anchor: "middle" });
        lx += lw + 4;
      }
    }
    const bid = clip(0, top, box.w, box.h - top);
    const fo = n.type === "tab" || n.type === "man" || n.type === "doc" ? foreignOf(n) : null;
    if (fo?.html) {
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
    s += "</g>";
    s += `<rect x="0" y="0" width="${box.w}" height="${box.h}" rx="6" fill="none" stroke="${n.color ? accent : C.border}" stroke-width="1.5"/>`;
    return `${s}</g>\n`;
  };

  const ordered = paintOrder(visCanvas.nodes);
  const groupsSvg = ordered.filter((x) => x.type === "group").map(nodeSvg).join("");
  const nodesSvg = ordered.filter((x) => x.type !== "group").map(nodeSvg).join("");
  const byId = new Map(visCanvas.nodes.map((n) => [n.id, { ...n, ...(rects.get(n.id) || {}) }]));
  const pointOf = (n, field, side) => {
    const y = field ? anchors.get(n.id)?.get(field) : undefined;
    if (y === undefined) return null;
    const s = layoutBox(n).s;
    return { x: side === "left" ? n.x : n.x + n.w, y: n.y + y * s };
  };
  let edgesSvg = "";
  const extra = [];
  for (const e of visCanvas.edges) {
    const a = byId.get(e.from);
    const b = byId.get(e.to);
    if (!a || !b) continue;
    const fs = e.fromSide === "left" ? "left" : "right";
    const ts = e.toSide === "left" ? "left" : "right";
    const p1 = pointOf(a, e.fromField, fs);
    const p2 = pointOf(b, e.toField, ts);
    const ends = resolveEnds({ rect: a, side: p1 ? fs : e.fromSide, point: p1 }, { rect: b, side: p2 ? ts : e.toSide, point: p2 });
    const g = edgePath(ends.p1, ends.p2, ends.fromSide, ends.toSide, e.curve, e.waypoints);
    const heads = arrowHeads(g, ends.p1, ends.p2, e.arrow);
    const lockTag = e.lockKind ? `🔒 ${e.lockKind}` : "";
    const edgeLabel = e.label && lockTag && !e.label.toLowerCase().includes(e.lockKind.toLowerCase())
      ? `${e.label} (${lockTag})`
      : (e.label || lockTag);
    extra.push(ends.p1, ends.p2, g.mid, ...(g.controls || []), ...(e.waypoints || []));
    if (edgeLabel) {
      const half = (edgeLabel.length * SANS_CH) / 2 + 4;
      extra.push({ x: g.mid.x - half, y: g.mid.y - 20 }, { x: g.mid.x + half, y: g.mid.y });
    }
    const c = resolve(accentVar(e.color, "var(--accent-blue)"));
    const dash = dashArray(e.style);
    edgesSvg += `<path d="${g.d}" fill="none" stroke="${c}" stroke-width="2"${dash ? ` stroke-dasharray="${dash}"` : ""}/>\n`;
    for (const h of heads) edgesSvg += `<polygon points="${h}" fill="${c}"/>\n`;
    const halo = opts.transparent ? "" : ` stroke="${C.bg}" stroke-width="4" paint-order="stroke"`;
    if (edgeLabel) edgesSvg += `<text x="${r1(g.mid.x)}" y="${r1(g.mid.y - 6)}" text-anchor="middle" fill="${C.text}"${halo} font-family="sans-serif" font-size="11">${esc(edgeLabel)}</text>\n`;
  }
  /* ui-scale-exempt-end */

  const nb = boundsOf([...byId.values()]) || { x: 0, y: 0, w: 400, h: 300 };
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
  const pw = ps === 1 ? vw : Math.max(1, Math.floor(vw * ps)), ph = ps === 1 ? vh : Math.max(1, Math.floor(vh * ps));
  let s = `<svg xmlns="http://www.w3.org/2000/svg" width="${pw}" height="${ph}" viewBox="${vx} ${vy} ${vw} ${vh}">\n`;
  if (defs) s += `<defs>${defs}</defs>\n`;
  if (!opts.transparent) s += `<rect x="${vx}" y="${vy}" width="${vw}" height="${vh}" fill="${C.bg}"/>\n`;
  s += groupsSvg + edgesSvg + nodesSvg;
  return `${s}</svg>`;
}

// ------------------------------------------------------------------ image sizes

export const PNG_SCALES = [1, 2, 3, 4];
export const PNG_DEFAULT_SCALE = 3;
export const PNG_MAX_SIDE = 16384;
export const PNG_MAX_AREA = 268435456;

export function svgSize(svg) {
  const m = /<svg\b[^>]*?\bwidth="([\d.]+)"[^>]*?\bheight="([\d.]+)"/.exec(String(svg || ""));
  return m ? { w: Number(m[1]), h: Number(m[2]) } : { w: 0, h: 0 };
}

export function pngSize(w, h, scale = PNG_DEFAULT_SCALE, maxSide = PNG_MAX_SIDE, maxArea = PNG_MAX_AREA) {
  const want = Number(scale) > 0 ? Number(scale) : 1;
  if (!(w > 0 && h > 0)) return { w: 0, h: 0, scale: want, clamped: false };
  const s = Math.min(want, maxSide / w, maxSide / h, Math.sqrt(maxArea / (w * h)));
  return { w: Math.max(1, Math.floor(w * s)), h: Math.max(1, Math.floor(h * s)), scale: s, clamped: s < want };
}

// ------------------------------------------------------------------ DOM snapshots (man pages, tabs)

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
const FO_DROP_TAGS = new Set(["script", "noscript", "style", "link", "meta", "template", "iframe", "frame", "object", "embed", "audio", "video"]);
const FO_DROP_SEL = ".nm-shield, .context-menu, .tooltip, .toast, .toast-container, [role='tooltip'], .loading-overlay, .spinner";
const FO_MAX_ELEMENTS = 8000;
const ATTR_NAME_RE = /^(?!xmlns)[A-Za-z_][\w.-]*$/i;

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
    return null;
  }
}

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
      if (name.startsWith("on") || name === "id" || name === "style" || name === "srcset" || !ATTR_NAME_RE.test(a.name)) el.removeAttribute(a.name);
      else if ((name === "src" || name === "poster") && !String(a.value).startsWith("data:")) el.removeAttribute(a.name);
    }
    if (tag === "input" || tag === "textarea") el.setAttribute("value", String(src.value ?? ""));
  }
  let style = "";
  for (const p of FO_PROPS) {
    let v = cs.getPropertyValue(p);
    if (!v) continue;
    if (p === "background-image" && /url\(/i.test(v)) continue;
    if ((p === "overflow-x" || p === "overflow-y") && (v === "auto" || v === "scroll")) v = "hidden";
    style += `${p}:${v};`;
  }
  if (style) el.setAttribute("style", style);
  if (tag === "canvas" || tag === "img" || tag === "textarea") {
    if (tag === "textarea") el.textContent = String(src.value ?? "");
    return el;
  }
  let host = el;
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

export function captureNodeForeignObject(node, el) {
  try {
    const body = el?.querySelector?.(".nm-node-body");
    if (!body || typeof XMLSerializer === "undefined") return null;
    let root = null;
    let win = null;
    if (node.type === "man" || node.type === "doc") {
      // the live page (a beautified doc node keeps the Kernel Docs layout and styles)
      root = body.querySelector(node.type === "man" ? ".nm-man" : ".nm-doc");
      win = el.ownerDocument?.defaultView || globalThis;
    } else if (node.type === "tab") {
      const frame = body.querySelector("iframe");
      const doc = frame?.contentDocument;
      win = frame?.contentWindow;
      if (!doc || !win || doc.readyState === "loading") return null;
      root = doc.querySelector("#workspace") || doc.body;
    }
    if (!root || !win?.getComputedStyle) return null;
    // A supersampled tab iframe (its document zoomed, `applyFrameSupersample()`) is read at 1×
    const html0 = node.type === "tab" ? root.ownerDocument?.documentElement : null;
    const zoom0 = html0?.style?.zoom || "";
    if (zoom0) html0.style.zoom = "";
    const budget = { n: FO_MAX_ELEMENTS };
    let clone;
    try {
      clone = cloneStyled(root, win, budget);
    } finally {
      if (zoom0) html0.style.zoom = zoom0;
    }
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
