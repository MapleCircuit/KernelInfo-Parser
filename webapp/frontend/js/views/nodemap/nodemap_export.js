/**
 * nodemap_export.js - SVG (and, through it, PNG) of a whole NodeMap canvas (`webapp/API_FRONTEND.md` §5.3).
 *
 * Respects visibility layers (`visibleCanvasSlice`), edge waypoints, struct `layout` (pahole
 * cache-line offsets/holes), and all 16 node types including `cve`, `kapi`, `doc`, `history` and `commit`.
 * Every color is the live CSS rule's token (`nodemap.css`, `codeview.css`) resolved through `resolve`;
 * `color-mix()` tints are evaluated in sRGB (`utils/css_color.js`) and translucent paints are written as
 * SVG 1.1 `#rrggbb` + `fill-opacity` / `stroke-opacity`, so the file is portable to vector tools.
 * `canvasToSvg()` and the size helpers are DOM-free (unit tested).
 */
import { BG_TINT, NODE_BADGES, accentVar, boundsOf, contentBox, displayTitle, formatLockBadge, isCustomColor, paintOrder, visibleCanvasSlice } from "./canvas_model.js";
import { arrowHeads, dashArray, edgePath, resolveEnds } from "./edge_router.js";
import { headMetaOf, sourceLineSegments } from "./node_content.js";
import { commitRowKey, diffRows, kconfigValue, mailBadges, mailLineClass, mailRowKey, maintainerLines, releaseRowKey, statusClass, valueClass } from "./domain_nodes.js";
import { collectExpandableTypes, countDirectives, esc as escHtml, paholeRows, structRows } from "./struct_view.js";
import { evalCssColor, formatCssColor, mixCssColors, svgPaint } from "../../utils/css_color.js";
import { nestedExportLines } from "./nested_nodes.js";

export const EXPORT_METRICS = {
  PAD: 40,
  HEAD_H: 33,
  BAR_H: 22,
  OUTER_H: 16,
  ROW_H: 16,
  TABLE_ROW_H: 18,
  LAYOUT_ROW_H: 21,
  LAYOUT_SUM_H: 24,
  SRC_ROW_H: 16.5,
  DIFF_ROW_H: 16.5,
  PP_INDENT: 10,
  ROW_BORDER: 2,
  MONO_CH: 6.6,
  SANS_CH: 5.8,
  TRACK_W: 6,
  MAIL_ROW_H: 18,
  MAIL_META_H: 14,
  MAIL_BODY_BOX_H: 320,
  MAIL_LINE_H: 14.5,
  KC_ROW_H: 17,
  MT_ROW_H: 16,
  GUIDE_W: 12,
  LOCK_BADGE_H: 16.5,
  LOCK_PAD: 4,
  NOTE_LINE_H: 16,
  HIST_ROW_H: 15,
  ROW_GAP: 5,
};

const {
  PAD, HEAD_H, BAR_H, OUTER_H, ROW_H, TABLE_ROW_H, LAYOUT_ROW_H, LAYOUT_SUM_H, SRC_ROW_H,
  PP_INDENT, ROW_BORDER, MONO_CH, SANS_CH, TRACK_W, DIFF_ROW_H, MAIL_ROW_H, MAIL_META_H, MAIL_BODY_BOX_H, MAIL_LINE_H,
  KC_ROW_H, MT_ROW_H, GUIDE_W, LOCK_BADGE_H, LOCK_PAD, NOTE_LINE_H, HIST_ROW_H, ROW_GAP,
} = EXPORT_METRICS;

/** Type-default accents (nodemap.css): groups are blue like every node, notes yellow. */
export function defaultAccent(type) {
  return type === "note" ? "var(--accent-yellow)" : "var(--accent-blue)";
}

/** Lock-kind colors of `.nm-lock-<kind>` badges (nodemap.css). */
export const LOCK_COLORS = {
  spinlock: "var(--accent-orange)", mutex: "var(--accent-blue)", rwsem: "var(--accent-purple)", rcu: "var(--accent-cyan)",
  atomic: "var(--accent-green)", seqlock: "var(--accent-yellow)", "lock-order": "var(--accent-red)",
};

/** `tok-*` classes of the code view's highlighter (`codeview.css`): color token, weight, italic. */
export const TOKEN_STYLES = {
  "tok-keyword": ["var(--syn-keyword)", 500], "tok-type": ["var(--syn-type)"], "tok-string": ["var(--syn-string)"],
  "tok-number": ["var(--syn-number)"], "tok-comment": ["var(--syn-comment)", 0, true], "tok-preproc": ["var(--syn-preproc)", 500],
  "tok-macro": ["var(--syn-preproc)", 500], "tok-include": ["var(--syn-string)"], "tok-punct": ["var(--syn-punct)"],
  "tok-function": ["var(--syn-function)", 500], "tok-identifier": ["var(--syn-identifier)"], "tok-variable": ["var(--syn-variable)"],
  "tok-attribute": ["var(--syn-attribute)"], "tok-lifetime": ["var(--syn-variable)", 0, true],
};

const r1 = (v) => Math.round(v * 10) / 10;
const XML_ILLEGAL = /[\u0000-\u0008\u000B\u000C\u000E-\u001F￾￿]/g;
const esc = (s) => escHtml(String(s ?? "").replace(XML_ILLEGAL, ""));
const attrSafe = (s) => String(s || "").replace(/"/g, "'").replace(/[<>&]/g, "");

function layoutBox(n) {
  return contentBox(n);
}

function fit(text, max) {
  const t = String(text ?? "");
  return max > 1 && t.length > max ? `${t.slice(0, Math.max(1, max - 1))}…` : t;
}

/** Word wrap at `cols` characters (long words are broken). */
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

/** `white-space: pre-wrap` lines: spaces kept, a line longer than `cols` continues on the next. */
function wrapPre(text, cols) {
  const out = [];
  const c = Math.max(1, cols);
  for (const raw of String(text ?? "").split(/\r?\n/)) {
    const line = raw.replace(/\t/g, "        ");
    if (!line.length) out.push("");
    for (let i = 0; i < line.length; i += c) out.push(line.slice(i, i + c));
  }
  return out;
}

/**
 * Text measurer of the export in progress (`opts.measure(text, size, mono, weight)`: the live fonts through a
 * canvas 2D context), or null: `canvasToSvg()` is synchronous, so it is set for one call.
 */
let MEASURE = null;

/**
 * Advance of `s` (canvas units): the live font's when a measurer is set, else an estimate (mono 0.6em,
 * sans `SANS_CH` per 11 units, emoji 1.2em).
 */
function textWidth(s, size = 11, mono = false, weight = 400) {
  if (MEASURE) {
    try {
      const m = Number(MEASURE(String(s ?? ""), size, mono, weight));
      if (m >= 0) return m;
    } catch {
      // estimate below
    }
  }
  let w = 0;
  for (const ch of String(s ?? "")) {
    const cp = ch.codePointAt(0);
    if (cp >= 0x1f000 || (cp >= 0x2600 && cp <= 0x27bf)) w += size * 1.2;
    else if (cp >= 0x2e80 && cp <= 0xffdc) w += size;
    else w += mono ? size * 0.6 : size * (SANS_CH / 11);
  }
  return w;
}

/** Word wrap at `width` canvas units (measured or estimated; long words are broken). */
function wrapW(text, width, size = 11, mono = false, weight = 400) {
  if (!MEASURE) return wrap(text, Math.max(4, Math.floor(width / (mono ? size * 0.6 : size * (SANS_CH / 11)))));
  const out = [];
  const tw = (t) => textWidth(t, size, mono, weight);
  for (const para of String(text || "").split(/\r?\n/)) {
    let line = "";
    for (let word of para.split(/\s+/).filter(Boolean)) {
      while (tw(word) > width && word.length > 1) {
        if (line) out.push(line);
        line = "";
        let k = word.length - 1;
        while (k > 1 && tw(word.slice(0, k)) > width) k--;
        out.push(word.slice(0, k));
        word = word.slice(k);
      }
      if (line && tw(`${line} ${word}`) > width) {
        out.push(line);
        line = "";
      }
      line = line ? `${line} ${word}` : word;
    }
    out.push(line);
  }
  return out;
}

/** `white-space: pre-wrap` lines at `width` canvas units: spaces kept, breaking at the last space when one fits. */
function wrapPreW(text, width, size = 11, mono = true) {
  if (!MEASURE || mono) return wrapPre(text, Math.max(1, Math.floor(width / (mono ? textWidth("0", size, true) : size * (SANS_CH / 11)))));
  const out = [];
  for (const raw of String(text ?? "").split(/\r?\n/)) {
    let rest = raw.replace(/\t/g, "        ");
    if (!rest.length) out.push("");
    while (rest.length) {
      if (textWidth(rest, size, false) <= width) {
        out.push(rest);
        break;
      }
      let k = rest.length - 1;
      while (k > 1 && textWidth(rest.slice(0, k), size, false) > width) k--;
      const sp = rest.lastIndexOf(" ", k);
      const cut = Math.max(1, sp > 0 ? sp + 1 : k); // always progress, even on a measurer gone wrong
      out.push(rest.slice(0, cut));
      rest = rest.slice(cut);
    }
  }
  return out;
}

/** Inline Markdown runs of a note line (`node_content.js` `inline()`): code spans, **bold**, *italic*, ~~strike~~. */
function noteRuns(text) {
  const runs = [];
  for (const part of String(text).split(/(`[^`]+`)/g)) {
    if (!part) continue;
    if (/^`[^`]+`$/.test(part)) {
      runs.push({ t: part.slice(1, -1), code: true });
      continue;
    }
    const re = /\*\*([^*]+)\*\*|~~([^~]+)~~|(^|[^*])\*([^*\s][^*]*)\*/g;
    let at = 0;
    let m;
    while ((m = re.exec(part))) {
      const lead = m[3] || "";
      const start = m.index + lead.length;
      if (start > at) runs.push({ t: part.slice(at, start) });
      else if (lead) runs.push({ t: lead });
      if (m[1] !== undefined) runs.push({ t: m[1], b: true });
      else if (m[2] !== undefined) runs.push({ t: m[2], s: true });
      else runs.push({ t: m[4], i: true });
      at = re.lastIndex;
    }
    if (at < part.length) runs.push({ t: part.slice(at) });
  }
  return runs.filter((r) => r.t);
}

/** Greedy wrap of styled runs to `width` canvas units; lines of runs. */
function wrapRuns(runs, width, size) {
  const lines = [[]];
  let w = 0;
  const push = (r, text) => {
    const line = lines[lines.length - 1];
    const last = line[line.length - 1];
    if (last && last.b === r.b && last.i === r.i && last.s === r.s && last.code === r.code) last.t += text;
    else line.push({ ...r, t: text });
  };
  for (const r of runs) {
    for (const word of r.t.split(/(\s+)/)) {
      if (!word) continue;
      const ww = textWidth(word, size, Boolean(r.code));
      if (/^\s+$/.test(word)) {
        if (w > 0) {
          push(r, " ");
          w += textWidth(" ", size, Boolean(r.code));
        }
        continue;
      }
      if (w > 0 && w + ww > width) {
        const line = lines[lines.length - 1];
        const last = line[line.length - 1];
        if (last) last.t = last.t.replace(/\s+$/, "");
        lines.push([]);
        w = 0;
      }
      push(r, word);
      w += ww;
    }
  }
  return lines;
}

/**
 * Note text as layout blocks (`renderNoteMarkdown()` of `node_content.js`): headings, list / task items,
 * quotes, fenced code and paragraphs.
 */
function noteBlocks(text) {
  const out = [];
  let fence = null;
  let list = null;
  let num = 0;
  const lines = String(text || "").split(/\r?\n/);
  const endList = () => {
    if (list && out.length) out[out.length - 1].listEnd = true;
    list = null;
  };
  for (const line of lines) {
    if (fence !== null) {
      if (/^```/.test(line.trim())) {
        out.push({ kind: "code", text: fence.join("\n") });
        fence = null;
      } else fence.push(line);
      continue;
    }
    if (/^```/.test(line.trim())) {
      endList();
      fence = [];
      continue;
    }
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    if (h) {
      endList();
      out.push({ kind: "h", level: h[1].length, text: h[2] });
      continue;
    }
    const task = /^\s*[-*]\s+\[( |x|X)\]\s+(.*)$/.exec(line);
    const ul = /^\s*[-*]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (task || ul || ol) {
      const want = ol && !ul ? "ol" : "ul";
      if (list !== want) {
        endList();
        list = want;
        num = 0;
        out.push({ kind: "liststart" });
      }
      num += 1;
      if (task) out.push({ kind: "li", list: want, n: num, task: task[1] === " " ? "open" : "done", text: task[2] });
      else out.push({ kind: "li", list: want, n: num, text: (ul || ol)[1] });
      continue;
    }
    endList();
    const q = /^>\s?(.*)$/.exec(line);
    if (q) out.push({ kind: "quote", text: q[1] });
    else if (line.trim()) out.push({ kind: "p", text: line });
  }
  if (fence !== null) out.push({ kind: "code", text: fence.join("\n") });
  endList();
  return out;
}

/** SVG filter of a CSS `box-shadow` token (`0 1px 3px rgba(…)`), or "". */
function shadowFilter(value, id) {
  const m = /^\s*(-?[\d.]+)(?:px)?\s+(-?[\d.]+)(?:px)?\s+([\d.]+)(?:px)?\s+(.+?)\s*$/.exec(String(value || "").split(/,(?![^(]*\))/)[0] || "");
  if (!m) return "";
  const p = svgPaint(m[4]);
  if (!/^#[0-9a-f]{6}$/.test(p.color)) return "";
  return `<filter id="${id}" x="-10%" y="-10%" width="120%" height="120%"><feDropShadow dx="${m[1]}" dy="${m[2]}" stdDeviation="${r1(Number(m[3]) / 2)}" flood-color="${p.color}" flood-opacity="${p.opacity}"/></filter>`;
}

/**
 * Standalone SVG document of the canvas.
 *
 * `resolve(cssColor)` turns a token (`var(--x)`) into a color; `opts`: `content(node)` (payload / model),
 * `foreign(node)` (DOM snapshot `{html}` of tab / man / doc), `editMode`, `structOpts(node)`,
 * `rowHeights(node)` (measured struct rows), `kconfigValues(node)`, `onCanvas(node)` (Set of Kconfig
 * symbols already on the canvas), `scroll(node)` (`{top, boxes}` live scroll offsets), `fonts`
 * (`{sans, mono}` font stacks), `transparent`, `pixelScale`.
 */
export function canvasToSvg(canvas, resolve = (c) => c, opts = {}) {
  const prevMeasure = MEASURE;
  MEASURE = typeof opts.measure === "function" ? opts.measure : null;
  try {
    return buildSvg(canvas, resolve, opts);
  } finally {
    MEASURE = prevMeasure;
  }
}

function buildSvg(canvas, resolve, opts) {
  const visCanvas = visibleCanvasSlice(canvas);
  const content = typeof opts.content === "function" ? opts.content : () => null;
  const nestedCardOf = typeof opts.nested === "function" ? opts.nested : () => null;
  const foreignOf = typeof opts.foreign === "function" ? opts.foreign : () => null;
  // per-node rasters of live DOM bodies (PNG export, `nodemap_png.js`): `{ href: data:image/png…, w, h }`
  const rasterOf = typeof opts.rasters === "function" ? opts.rasters : () => null;
  const editMode = Boolean(opts.editMode);
  const structOptsOf = typeof opts.structOpts === "function" ? opts.structOpts : () => ({ editMode });
  const rowHeightsOf = typeof opts.rowHeights === "function" ? opts.rowHeights : () => null;
  const kcValuesOf = typeof opts.kconfigValues === "function" ? opts.kconfigValues : () => null;
  const onCanvasOf = typeof opts.onCanvas === "function" ? opts.onCanvas : () => null;
  const scrollOf = typeof opts.scroll === "function" ? opts.scroll : () => null;
  const FONT = { sans: attrSafe(opts.fonts?.sans) || "sans-serif", mono: attrSafe(opts.fonts?.mono) || "monospace" };
  const cache = new Map();
  const R = (v) => {
    if (!cache.has(v)) cache.set(v, resolve(v));
    return cache.get(v);
  };
  /** `color-mix(in srgb, a pct%, b)` of two resolved colors (the CSS expression when they cannot be read). */
  const mix = (a, b, pct) => mixCssColors(a, b, pct) || `color-mix(in srgb, ${a} ${pct}%, ${b})`;
  const bgOf = (id, base) => {
    const b = R(base);
    if (!id) return b;
    if (isCustomColor(id)) return id.toLowerCase();
    return mix(R(accentVar(id)), b, Math.round(BG_TINT * 100));
  };
  const C = {
    bg: bgOf(visCanvas.settings?.bgColor || "", "var(--bg-canvas)"),
    canvas: R("var(--bg-canvas)"),
    panel: R("var(--bg-secondary)"),
    head: R("var(--bg-tertiary)"),
    code: R("var(--bg-primary)"),
    text: R("var(--text-primary)"),
    muted: R("var(--text-secondary)"),
    faint: R("var(--text-muted)"),
    border: R("var(--border-color)"),
    rule: R("var(--border-muted)"),
    type: R("var(--syn-type)"),
    keyword: R("var(--syn-keyword)"),
    preproc: R("var(--syn-preproc)"),
    number: R("var(--syn-number)"),
    note: R("var(--accent-yellow)"),
    add: R("var(--diff-add-fg)"),
    del: R("var(--diff-del-fg)"),
    green: R("var(--accent-green)"),
    red: R("var(--accent-red)"),
    orange: R("var(--accent-orange)"),
    blue: R("var(--accent-blue)"),
    cyan: R("var(--accent-cyan)"),
    purple: R("var(--accent-purple)"),
    yellow: R("var(--accent-yellow)"),
  };
  const lockColor = (kind) => R(LOCK_COLORS[kind] || LOCK_COLORS.spinlock);
  const tokStyle = (cls) => {
    const t = TOKEN_STYLES[cls];
    return t ? { fill: R(t[0]), weight: t[1] || 0, italic: Boolean(t[2]) } : null;
  };
  let used = 0;
  const anchors = new Map();
  let defs = "";
  let clipN = 0;
  const clip = (x, y, w, h, rx = 0) => {
    const id = `nmclip${clipN++}`;
    defs += `<clipPath id="${id}"><rect x="${r1(x)}" y="${r1(y)}" width="${r1(Math.max(0, w))}" height="${r1(Math.max(0, h))}"${rx ? ` rx="${rx}"` : ""}/></clipPath>`;
    return id;
  };
  const clipRound = (w, h) => clip(0, 0, w, h, 6);
  const shadowId = "nmshadow";
  const shadowDef = shadowFilter(R("var(--shadow-sm)"), shadowId);
  let shadowUsed = false;

  /* ui-scale-exempt-start (NodeMap generated SVG: canvas units, zoom-controlled) */
  /** SVG 1.1 paint attribute: a translucent color becomes `#rrggbb` plus `<attr>-opacity`. */
  const paint = (attr, color, op = 1) => {
    // "none" stays; a token missing from the theme (empty) paints nothing rather than an invalid value
    if (color === "none" || !String(color ?? "").trim()) return ` ${attr}="none"`;
    const p = svgPaint(color);
    const o = Math.round(op * p.opacity * 1000) / 1000;
    return ` ${attr}="${p.color}"${o < 1 ? ` ${attr}-opacity="${o}"` : ""}`;
  };
  const F = (c, op) => paint("fill", c, op);
  const S = (c, op) => paint("stroke", c, op);
  const fontAttrs = ({ size, mono, family, weight, italic, deco, ls }) =>
    `${family !== null ? ` font-family="${family || (mono ? FONT.mono : FONT.sans)}"` : ""}${size ? ` font-size="${size}"` : ""}${weight ? ` font-weight="${weight}"` : ""}${italic ? ' font-style="italic"' : ""}${deco ? ` text-decoration="${deco}"` : ""}${ls ? ` letter-spacing="${ls}"` : ""}`;
  const txt = (x, y, s, { fill = C.text, op = 1, size = 11, mono = false, family = "", weight = "", anchor = "", italic = false, opacity = 1, deco = "", ls = 0 } = {}) =>
    `<text x="${r1(x)}" y="${r1(y)}"${F(fill, op)}${fontAttrs({ size, mono, family, weight, italic, deco, ls })}${anchor ? ` text-anchor="${anchor}"` : ""}${opacity !== 1 ? ` opacity="${opacity}"` : ""} xml:space="preserve">${s}</text>`;
  const tspan = (s, { fill, op = 1, weight = "", italic = false, deco = "", size = 0, mono = null, x = null } = {}) =>
    `<tspan${x !== null ? ` x="${r1(x)}"` : ""}${fill ? F(fill, op) : ""}${mono !== null ? ` font-family="${mono ? FONT.mono : FONT.sans}"` : ""}${size ? ` font-size="${size}"` : ""}${weight ? ` font-weight="${weight}"` : ""}${italic ? ' font-style="italic"' : ""}${deco ? ` text-decoration="${deco}"` : ""}>${s}</tspan>`;
  const rect = (x, y, w, h, { fill = "none", fop = 1, stroke = "", sop = 1, sw = 1, rx = 0, dash = "" } = {}) =>
    `<rect x="${r1(x)}" y="${r1(y)}" width="${r1(Math.max(0, w))}" height="${r1(Math.max(0, h))}"${rx ? ` rx="${rx}"` : ""}${F(fill, fop)}${stroke ? `${S(stroke, sop)}${sw !== 1 ? ` stroke-width="${sw}"` : ""}` : ""}${dash ? ` stroke-dasharray="${dash}"` : ""}/>`;
  const hline = (x1, x2, y, color, op = 1, dash = "", sw = 1) =>
    `<line x1="${r1(x1)}" y1="${r1(y)}" x2="${r1(x2)}" y2="${r1(y)}"${S(color, op)}${sw !== 1 ? ` stroke-width="${sw}"` : ""}${dash ? ` stroke-dasharray="${dash}"` : ""}/>`;
  const vline = (x, y1, y2, color, op = 1, sw = 1) =>
    `<line x1="${r1(x)}" y1="${r1(y1)}" x2="${r1(x)}" y2="${r1(y2)}"${S(color, op)}${sw !== 1 ? ` stroke-width="${sw}"` : ""}/>`;
  /**
   * A pill (badge, chip, tag, button): `{s, w, h}`; `y` is its top. `fg` text color, `stroke` border,
   * `fill` + `fop` background.
   */
  const pill = (x, y, label, { size = 9, mono = true, padX = 5, h = 0, rx = 6, fg = C.text, stroke = "", sop = 1, fill = "none", fop = 1, dash = "", weight = "", italic = false, ls = 0, upper = false } = {}) => {
    const text = upper ? String(label).toUpperCase() : String(label);
    const ph = h || Math.round(size * 1.35 + 2);
    const w = textWidth(text, size, mono, weight || 400) + (ls ? ls * text.length : 0) + padX * 2 + (stroke ? 2 : 0);
    const s = rect(x, y, w, ph, { fill, fop, stroke, sop, rx, dash }) +
      txt(x + w / 2, y + ph / 2 + size * 0.35, esc(text), { fill: fg, size, mono, anchor: "middle", weight, italic, ls });
    return { s, w, h: ph };
  };
  /** A `.nm-*-btn` action button (blue mono 9 on a border-color outline, radius 4). */
  const btn = (x, y, label) => pill(x, y, label, { size: 9, padX: 5, rx: 4, fg: C.blue, stroke: C.border });
  /** Buttons laid out right to left ending at `right`; `{s, x}` (`x` their left edge). */
  const btnsRight = (right, y, labels) => {
    let s = "";
    let x = right;
    for (const l of [...labels].reverse()) {
      const w = textWidth(l, 9, true) + 12;
      x -= w;
      s += btn(x, y, l).s;
      x -= 3;
    }
    return { s, x: labels.length ? x : right };
  };
  /** Lock badge of `.nm-lock-badge.nm-lock-<kind>` (mono 10, kind-colored border and text on `--bg-tertiary`). */
  const lockBadge = (x, y, lock, h = LOCK_BADGE_H) => pill(x, y, formatLockBadge(lock), { size: 10, padX: 5, rx: 4, h, fg: lockColor(lock.kind), stroke: lockColor(lock.kind), fill: C.head });
  const lockBadgeW = (lock) => textWidth(formatLockBadge(lock), 10, true) + 12;

  /** Clamp row anchors to the visible rows area (scrolled by `sc`) and record them for the edges (`fromField`). */
  const setRowAnchors = (n, box, top, rowY, end, sc = 0) => {
    const lo = top + 4;
    const hi = (n.autoFit ? Math.min(box.h, end) : box.h) - 4;
    const ys = new Map();
    for (const [k, y] of rowY) ys.set(k, Math.min(Math.max(y - sc, lo), Math.max(lo, hi)));
    anchors.set(n.id, ys);
  };

  // ---------------------------------------------------------------- struct

  /** `.nm-ver-diff-badge`: Version Time-Travel banner (`version_diff.js` `renderStructDiffBadge`). */
  const versionDiffBanner = (so, box, y) => {
    const diff = so.versionDiff;
    if (!diff) return { s: "", h: 0 };
    const [va, vb] = Array.isArray(so.versionDiffRange) ? so.versionDiffRange : ["", ""];
    const chips = diff.hasChanges ? [
      ...(diff.addedFields || []).slice(0, 6).map((f) => [`+${f}`, C.green]),
      ...(diff.removedFields || []).slice(0, 6).map((f) => [`-${f}`, C.red]),
      ...(diff.changedTypes || []).slice(0, 4).map((c) => [`~${c.name}`, C.orange]),
      ...(diff.changedGuards || []).slice(0, 4).map((c) => [`#${c.name}`, C.preproc]),
    ] : [];
    let inner = "";
    const headY = y + 4 + 10;
    const head = diff.hasChanges ? `${va} → ${vb}: ${diff.summaryText}` : `${va} → ${vb}: unchanged`;
    inner += txt(8, headY, esc(fit(head, Math.floor((box.w - 16) / 6))), { fill: diff.hasChanges ? C.text : C.green, size: 10, mono: true, weight: diff.hasChanges ? 600 : "" });
    let cy = headY + 4;
    let cx = 8;
    if (chips.length) {
      cy += 3;
      for (const [label, col] of chips) {
        const w = textWidth(label, 9, true) + 10;
        if (cx + w > box.w - 8 && cx > 8) {
          cx = 8;
          cy += 15;
        }
        inner += pill(cx, cy, label, { size: 9, padX: 5, rx: 6, h: 12, fg: col, fill: col, fop: 0.14 }).s;
        cx += w + 3;
      }
      cy += 12;
    }
    const h = cy + 4 - y + 1;
    return { s: rect(0, y, box.w, h, { fill: mix(C.purple, C.head, 10) }) + hline(0, box.w, y + h - 0.5, C.rule) + inner, h };
  };

  const structLayoutBody = (n, box, top, so, sc) => {
    const pahole = so.pahole || content(n)?.pahole || null;
    if (!pahole || pahole.unavailable) {
      used = top + 34;
      const reason = pahole?.error ? `: ${pahole.error}` : "";
      const lines = wrapW(`Memory layout (pahole) unavailable for this revision${reason}. Switch to list or table style to inspect AST fields.`, box.w - 20, 11);
      used = top + 8 + lines.length * 16 + 8;
      return lines.map((l, i) => txt(10, top + 20 + i * 16, esc(l), { fill: C.faint })).join("");
    }
    const m = paholeRows(pahole, n.data);
    let s = "";
    let y = top;
    const vd = versionDiffBanner(so, box, y);
    s += vd.s;
    y += vd.h;
    s += rect(0, y, box.w, LAYOUT_SUM_H, { fill: C.head });
    let sx = 8;
    const parts = [[`${m.totalSize}B`, C.muted], [`align ${m.alignment}B`, C.muted], [`${m.cacheLines} cache line${m.cacheLines === 1 ? "" : "s"}`, C.muted]];
    if (m.paddingBytes) parts.push([`${m.paddingBytes}B pad (${m.holesCount} hole${m.holesCount === 1 ? "" : "s"})`, C.orange]);
    if (m.isEstimate) parts.push(["est.", C.faint, true]);
    for (const [t, col, it] of parts) {
      if (sx > box.w - 8) break;
      s += txt(sx, y + 15.5, esc(t), { fill: col, size: 10, mono: true, italic: Boolean(it) });
      sx += textWidth(t, 10, true) + 8;
    }
    s += hline(0, box.w, y + LAYOUT_SUM_H - 0.5, C.rule);
    y += LAYOUT_SUM_H;
    const rowsTop = y + 2; // .nm-struct-rows padding
    const measured = rowHeightsOf(n);
    const hs = Array.isArray(measured) && measured.length === m.rows.length && measured.every((v) => v > 0) ? measured : null;
    const rowsBottom = box.h;
    const fieldY = new Map();
    let inner = "";
    let ry = rowsTop;
    // `.nm-sf-layout`: 2-unit border + 6 padding, offset right-aligned in 4ch, size in 3.5ch (10-unit mono), 4-unit gaps
    const ch10 = textWidth("0", 10, true);
    const offR = ROW_BORDER + 6 + 4 * ch10;
    const szR = offR + 4 + 3.5 * ch10;
    const tx = szR + 4;
    m.rows.forEach((r, i) => {
      const RH = hs ? hs[i] : r.kind === "cacheline" ? LAYOUT_ROW_H + 2 : LAYOUT_ROW_H;
      const mid = ry + RH / 2;
      const base = ry + RH / 2 + 4;
      if (r.kind === "cacheline") {
        inner += rect(0, ry, box.w, RH, { fill: C.blue, fop: 0.08 });
        inner += hline(0, box.w, ry + 0.5, C.blue, 0.45, "4 3");
        inner += txt(ROW_BORDER + 8, base, esc(`Cache line ${r.cl} · +${r.offset}B`), { fill: C.blue, size: 10, mono: true, weight: 600 });
      } else if (r.kind === "hole") {
        inner += rect(0, ry, box.w, RH, { fill: C.orange, fop: 0.1 });
        inner += hline(0, box.w, ry + RH - 0.5, C.rule);
        inner += txt(offR, base, esc(`+${r.offset}`), { fill: C.faint, size: 10, mono: true, anchor: "end", italic: true });
        inner += txt(szR, base, esc(`${r.size}B`), { fill: C.number, size: 10, mono: true, anchor: "end", italic: true });
        inner += txt(tx, base, esc(`/* ${r.size} byte padding hole */`), { fill: C.orange, size: 10, mono: true, italic: true });
      } else {
        fieldY.set(r.key, mid);
        if (r.crosses) inner += rect(0, ry, box.w, RH, { fill: C.red, fop: 0.07 });
        inner += hline(0, box.w, ry + RH - 0.5, C.rule);
        inner += txt(offR, base, esc(`+${r.offset}`), { fill: C.faint, size: 10, mono: true, anchor: "end" });
        inner += txt(szR, base, esc(`${r.size}B`), { fill: C.number, size: 10, mono: true, anchor: "end" });
        if (r.type) inner += txt(tx, base, esc(r.type), { fill: C.type, mono: true });
        const nx = tx + (r.type ? textWidth(r.type, 11, true) + 4 : 0);
        const label = `${r.name}${r.bitfield || ""}`;
        inner += txt(nx, base, `${tspan(esc(r.name), { fill: C.text })}${r.bitfield ? tspan(esc(r.bitfield), { fill: C.number }) : ""}`, { mono: true });
        let bx = nx + textWidth(label, 11, true) + 4;
        if (r.lock?.kind) {
          inner += lockBadge(bx, ry + 1.5, r.lock, 15).s;
          bx += lockBadgeW(r.lock) + 4;
        }
        if (r.crosses) inner += txt(box.w - 8, base - 1, "⇄CL", { fill: C.red, size: 9, mono: true, anchor: "end" });
      }
      ry += RH;
    });
    const rowsEnd = ry;
    if (!m.rows.length) inner += txt(10, rowsTop + 16, "No members", { fill: C.faint, italic: true });
    used = m.rows.length ? rowsEnd : rowsTop + 24;
    const off = Math.min(sc, Math.max(0, rowsEnd - rowsBottom));
    const visBottom = n.autoFit ? Math.min(rowsBottom, rowsEnd) : rowsBottom;
    const ys = new Map();
    for (const [k, my] of fieldY) ys.set(k, Math.min(Math.max(my - off, rowsTop + 4), visBottom - 4));
    anchors.set(n.id, ys);
    const cid = clip(0, rowsTop, box.w, rowsBottom - rowsTop);
    return `${s}<g clip-path="url(#${cid})">${off ? `<g transform="translate(0 ${r1(-off)})">${inner}</g>` : inner}</g>`;
  };

  /** Struct body rows (local coordinates); records field anchors for the edges. */
  const structBody = (n, box, top, accent, sc) => {
    const layout = content(n);
    const d = n.data;
    const so = structOptsOf(n) || {};
    if (d.style === "layout") return structLayoutBody(n, box, top, so, sc);
    if (!layout) return txt(10, top + 16, esc(`${d.kind} ${d.name}`), { fill: C.faint });
    const sEdit = Boolean(so.editMode);
    const model = so.model || structRows(layout, d, so);
    const table = d.style === "table";
    let s = "";
    let y = top;
    if (sEdit) {
      s += hline(0, box.w, y + BAR_H, C.rule);
      let cx = 8;
      const directives = countDirectives(layout.items);
      const chip = (label, { on = false, enabled = true, note = false } = {}) => {
        const w = label.length * 6 + 12;
        const c = rect(cx, y + 4, w, 14, { fill: on ? C.blue : "none", fop: 0.14, stroke: on ? C.blue : C.border, rx: 7, dash: note ? "3 2" : "" }) +
          txt(cx + w / 2, y + 14.5, esc(label), { fill: on ? C.text : note ? C.faint : C.muted, size: 10, mono: true, anchor: "middle" });
        s += enabled ? c : `<g opacity="0.45">${c}</g>`;
        cx += w + 4;
      };
      chip("Types", { on: Boolean(d.showTypes) });
      chip(directives ? `#ifdef ${directives}` : "#ifdef", { on: Boolean(d.showGuards), enabled: directives > 0 });
      let typeCount = 0;
      try {
        typeCount = collectExpandableTypes(layout, d, so).length;
      } catch {
        typeCount = 0;
      }
      if (typeCount) chip(`⇶ Types ${typeCount}`);
      if (model.hidden) chip(`${model.hidden} hidden`, { note: true });
      y += BAR_H;
    }
    const vd = versionDiffBanner(so, box, y);
    s += vd.s;
    y += vd.h;
    if (layout.outer_conds?.length && (!model.outerHidden || sEdit)) {
      const banner = txt(8, y + 12, esc(fit(layout.outer_conds.join(" && "), Math.floor((box.w - 16) / 6))), { fill: C.preproc, size: 10, mono: true, deco: model.outerHidden ? "line-through" : "" });
      s += model.outerHidden ? `<g opacity="0.45">${banner}</g>` : banner;
      s += hline(0, box.w, y + OUTER_H, C.rule);
      y += OUTER_H;
    }
    const rowsTop = y + 2;
    const rowsBottom = box.h - 2;
    const id = clip(0, rowsTop, box.w, rowsBottom - rowsTop);
    // Same geometry as nodemap.css: rows start after a 2px left border; the #if cell spans
    // [2, gutter + 6) with a 1px right border and a 4px flex gap before the controls column
    const gutter = model.tracks ? model.tracks * TRACK_W + TRACK_W : 0;
    const cellRight = table && model.tracks ? gutter + 5.5 : 0;
    const ctlW = table ? (sEdit ? 30 : 14) : 0;
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
    const off = n.autoFit ? 0 : Math.min(sc, Math.max(0, rowsEnd - rowsBottom));
    const fieldY = new Map();
    let inner = "";
    if (!model.rows.length) inner += txt(10, rowsTop + 12, "No members", { fill: C.faint, italic: true });
    if (cellRight && model.rows.length) inner += vline(cellRight, rowsTop, rowsEnd, C.rule);
    model.rows.forEach((r, i) => {
      const ry = rowTops[i];
      const rh = hs ? hs[i] : rowH;
      if (ry - off > rowsBottom || ry + rh - off < rowsTop) {
        if (r.kind !== "pp" && r.kind !== "summary") fieldY.set(r.key, ry + rh / 2);
        return;
      }
      const mid = ry + rh / 2;
      const base = ry + rh / 2 + 4;
      const deco = r.hidden ? "line-through" : "";
      let row = "";
      // row tints: version diff, folded #if summary, guarded left border
      if (r.diff === "add" || r.diff === "added") row += rect(0, ry, box.w, rh, { fill: C.add, fop: 0.12 });
      else if (r.diff === "type" || r.diff === "guard" || r.diff === "changed") row += rect(0, ry, box.w, rh, { fill: C.orange, fop: 0.1 });
      if (r.kind === "summary") row += rect(0, ry, box.w, rh, { fill: C.preproc, fop: 0.07 });
      const guarded = (r.kind === "field" || r.kind === "agg") && r.conds.length && !d.showGuards;
      if (guarded) row += rect(0, ry, ROW_BORDER, rh, { fill: C.preproc, fop: 0.55 });
      for (const [l, part] of r.bars) {
        const bx = ROW_BORDER + 7 + l * TRACK_W;
        const y1 = part === "start" ? mid : ry;
        const y2 = part === "end" ? mid : ry + rh;
        const alpha = [0.9, 0.7, 0.55, 0.4][l % 4];
        row += `<line x1="${bx}" y1="${r1(y1)}" x2="${bx}" y2="${r1(y2)}"${S(C.preproc, alpha)} stroke-width="2"/>`;
        if (part === "start" || part === "end" || part === "mid") row += `<line x1="${bx - 1}" y1="${r1(mid)}" x2="${bx + 4}" y2="${r1(mid)}"${S(C.preproc, alpha)} stroke-width="2"/>`;
      }
      if (table) row += hline(0, box.w, ry + rh, C.rule);
      const x0 = cellRight ? cellRight + 4.5 : ROW_BORDER + 4 + gutter + (table ? 0 : r.depth * 14);
      const lineNo = r.line ? txt(box.w - 8, base, esc(r.line), { fill: C.faint, size: 9, mono: true, anchor: "end" }) : "";
      if (r.kind === "pp" || r.kind === "summary") {
        const chev = r.count ? (r.folded ? "▸ " : "▾ ") : "";
        const ix = x0 + (r.ppLevel || 0) * PP_INDENT;
        const ppFalse = r.truth === "false";
        const ppText = { fill: C.preproc, mono: true, italic: true, deco: ppFalse ? "line-through" : deco, opacity: ppFalse ? 0.7 : 1 };
        let px = ix;
        if (table) {
          // chevron in the controls column, text after it (the indent steps both in, like the live row)
          px = ix + ctlW + 6;
          if (chev) row += txt(ix, base, esc(chev), { fill: C.faint, mono: true });
          row += txt(px, base, esc(fit(r.text, Math.floor((box.w - px - 40) / MONO_CH))), ppText);
        } else {
          row += txt(ix, base, `${esc(chev)}${esc(fit(r.text, Math.floor((box.w - ix - 40) / MONO_CH)))}`, ppText);
          px = ix + (chev ? 2 * MONO_CH : 0);
        }
        if (r.kind === "summary") {
          const label = `${r.count} member${r.count === 1 ? "" : "s"}${r.auto ? " · compiled out" : " · folded"}`;
          const lx = px + textWidth(fit(r.text, Math.floor((box.w - px - 40) / MONO_CH)), 11, true) + 6;
          row += pill(lx, ry + (rh - 12) / 2, label, { size: 9, mono: true, padX: 5, h: 12, rx: 6, fg: C.faint, stroke: C.preproc, sop: 0.45 }).s;
        }
      } else {
        fieldY.set(r.key, mid);
        const label = r.kind === "agg" ? `${r.name}${r.folded ? ` {${r.count}}` : ""}` : `${r.name}${r.suffix || ""}`;
        const lead = r.kind === "agg" ? (r.folded ? "▸ " : "▾ ") : "";
        const dead = r.dead ? { op: 0.65, italic: true } : {};
        const deadSpan = (s2, fill, extra = {}) => tspan(s2, { fill, op: dead.op || 1, italic: dead.italic, ...extra });
        let endX;
        if (table) {
          const tx = x0 + ctlW;
          row += txt(x0, base, esc(lead), { fill: C.faint, mono: true });
          const typeText = r.kind === "agg" ? `${r.tag}${r.typeText ? ` ${r.typeText}` : ""}` : r.type;
          if (typeText) row += txt(tx + 4 + r.depth * 2 * MONO_CH, base, r.kind === "agg" ? `${tspan(esc(r.tag), { fill: C.keyword })}${r.typeText ? tspan(esc(` ${fit(r.typeText, model.typeCh - r.depth * 2 - r.tag.length - 1)}`), { fill: C.type }) : ""}` : esc(fit(typeText, model.typeCh - r.depth * 2)), { fill: C.type, mono: true, deco, weight: r.kind === "agg" ? 600 : "", opacity: dead.op || 1, italic: dead.italic });
          row += vline(tx + typeW + 10.5, ry, ry + rh, C.rule);
          const nx = tx + typeW + 17;
          const nameSpans = r.kind === "agg"
            ? `${deadSpan(esc(r.name), C.text)}${r.folded ? tspan(esc(` {${r.count}}`), { fill: C.faint, weight: 400 }) : ""}`
            : `${deadSpan(esc(r.name), C.text)}${r.suffix ? tspan(esc(r.suffix), { fill: C.number }) : ""}`;
          row += txt(nx, base, nameSpans, { mono: true, weight: r.kind === "agg" ? 600 : "", deco });
          endX = nx + textWidth(label, 11, true);
        } else {
          let spans = lead ? tspan(esc(lead), { fill: C.faint }) : "";
          let plain = lead;
          if (r.kind === "agg") {
            spans += tspan(`${esc(r.tag)} `, { fill: C.keyword });
            plain += `${r.tag} `;
            if (r.typeText) {
              spans += tspan(`${esc(r.typeText)} `, { fill: C.type });
              plain += `${r.typeText} `;
            }
            spans += tspan(esc(r.name), { fill: C.text });
            if (r.folded) spans += tspan(esc(` {${r.count}}`), { fill: C.faint, weight: 400 });
            plain += label;
          } else {
            if (r.showType && r.type) {
              spans += deadSpan(`${esc(r.type)} `, C.type);
              plain += `${r.type} `;
            }
            spans += deadSpan(esc(r.name), C.text);
            if (r.suffix) spans += tspan(esc(r.suffix), { fill: C.number });
            plain += label;
          }
          row += txt(x0, base, spans, { mono: true, weight: r.kind === "agg" ? 600 : "", deco });
          endX = x0 + textWidth(plain, 11, true);
        }
        let bx = endX + 4;
        if (r.lock?.kind) {
          row += lockBadge(bx, ry + (rh - 14) / 2, r.lock, 14).s;
          bx += lockBadgeW(r.lock) + 4;
        }
        if (guarded) row += txt(bx, base, "#", { fill: C.preproc, size: 9, mono: true });
      }
      row += lineNo;
      inner += r.dim ? `<g opacity="0.45">${row}</g>` : row;
    });
    s += `<g clip-path="url(#${id})">${off ? `<g transform="translate(0 ${r1(-off)})">${inner}</g>` : inner}</g>`;
    const ys = new Map();
    const visBottom = n.autoFit ? Math.min(rowsBottom, rowsEnd) : rowsBottom;
    for (const [k, my] of fieldY) ys.set(k, Math.min(Math.max(my - off, rowsTop + 4), visBottom - 4));
    anchors.set(n.id, ys);
    return s;
  };

  // ---------------------------------------------------------------- code (source, diff)

  const sourceBody = (n, box, top, ctx) => {
    const r = content(n);
    const d = n.data;
    if (!r) return txt(10, top + 16, esc(d.path || "Source"), { fill: C.faint });
    let s = n.bgColor ? "" : rect(0, top, box.w, box.h - top, { fill: C.code });
    const lines = Array.isArray(r.lines) ? r.lines : [];
    const total = lines.length * SRC_ROW_H + 2;
    const sc = Math.min(ctx.sc, Math.max(0, total - (box.h - top)));
    ctx.sc = sc;
    const first = Math.max(0, Math.floor(sc / SRC_ROW_H));
    const shown = Math.min(lines.length, first + Math.ceil((box.h - top) / SRC_ROW_H) + 1);
    const noW = Math.max(5, String(r.start_line + lines.length).length) * MONO_CH;
    const hl = r.symbol && (r.symbol.line_s > r.start_line || r.symbol.line_e < r.end_line) ? r.symbol : null;
    const segs = sourceLineSegments(lines.slice(0, shown).map((l) => String(l ?? "")), r.path || d.path);
    for (let i = first; i < shown; i++) {
      const no = r.start_line + i;
      const ly = top + i * SRC_ROW_H;
      if (hl && no >= hl.line_s && no <= hl.line_e) s += rect(0, ly, box.w, SRC_ROW_H, { fill: C.note, fop: 0.12 });
      s += txt(4 + noW, ly + 12, esc(no), { fill: C.faint, mono: true, anchor: "end" });
      const spans = (segs[i] || []).map((g) => {
        const t = esc(g.text.replace(/\t/g, "        "));
        if (!t) return "";
        const st = g.cls ? tokStyle(g.cls) : null;
        return st ? tspan(t, { fill: st.fill, weight: st.weight || "", italic: st.italic }) : t;
      }).join("");
      s += txt(4 + noW + 8, ly + 12, spans, { mono: true });
    }
    used = top + lines.length * SRC_ROW_H + 2;
    return s;
  };

  const diffBody = (n, box, top, ctx) => {
    const p = content(n);
    const d = n.data;
    if (!p) return txt(10, top + 16, esc(d.path || (d.commitHash ? d.commitHash.slice(0, 12) : "Diff")), { fill: C.faint });
    const rows = diffRows(p);
    const total = Math.max(1, rows.length) * DIFF_ROW_H + 2;
    const sc = Math.min(ctx.sc, Math.max(0, total - (box.h - top)));
    ctx.sc = sc;
    const first = Math.max(0, Math.floor(sc / DIFF_ROW_H));
    const shown = Math.min(rows.length, first + Math.ceil((box.h - top) / DIFF_ROW_H) + 1);
    let maxNo = 1;
    for (const r of rows) maxNo = Math.max(maxNo, r.old || 0, r.new || 0);
    const noW = Math.max(4, String(maxNo).length) * 6 + 8;
    let s = n.bgColor ? "" : rect(0, top, box.w, box.h - top, { fill: C.code });
    for (let i = first; i < shown; i++) {
      const r = rows[i];
      const ly = top + i * DIFF_ROW_H;
      const base = ly + 12;
      if (r.kind === "file") {
        s += rect(0, ly, box.w, DIFF_ROW_H, { fill: C.head }) + txt(8, base, esc(r.text), { fill: C.text, mono: true, weight: 600 });
      } else if (r.kind === "hunk") {
        s += rect(0, ly, box.w, DIFF_ROW_H, { fill: C.blue, fop: 0.08 }) + txt(8, base, esc(r.text), { fill: C.preproc, mono: true });
      } else if (r.kind === "note") {
        s += txt(8, base, esc(r.text), { fill: C.faint, mono: true, italic: true });
      } else {
        const color = r.kind === "+" ? C.add : r.kind === "-" ? C.del : C.text;
        if (r.kind !== " ") s += rect(0, ly, box.w, DIFF_ROW_H, { fill: color, fop: 0.12 });
        s += txt(noW - 4, base, esc(r.old ?? ""), { fill: C.faint, mono: true, anchor: "end", size: 10 });
        s += txt(noW * 2 - 4, base, esc(r.new ?? ""), { fill: C.faint, mono: true, anchor: "end", size: 10 });
        if (r.kind !== " ") s += txt(noW * 2 + 0.75 * MONO_CH, base, esc(r.kind), { fill: color, mono: true, anchor: "middle" });
        s += txt(noW * 2 + 1.5 * MONO_CH + 2, base, esc(String(r.text).replace(/\t/g, "        ")), { fill: color, mono: true });
      }
    }
    used = top + Math.max(1, rows.length) * DIFF_ROW_H + 2;
    return s;
  };

  // ---------------------------------------------------------------- note

  const noteBody = (n, box, top, ctx) => {
    const text = String(n.data.text || "");
    if (!text.trim()) {
      used = top + 6 + 6 + 16 + 6;
      return txt(20, top + 6 + 6 + 12, esc(editMode ? "Double-click to write a note" : "Empty note (Edit Mode edits it)"), { fill: C.faint, italic: true });
    }
    const x0 = 10;
    const width = box.w - 20;
    const lim = box.h + ctx.sc + 20;
    let y = top + 6;
    let s = "";
    const emitRuns = (x, baseline, runs, base = {}) => {
      let spans = "";
      let cx = x;
      let needX = false;
      for (const r of runs) {
        const t = esc(r.t);
        const size = base.size || 11;
        const w = textWidth(r.t, size, Boolean(r.code));
        if (r.code) {
          s += rect(cx - 1, baseline - size * 0.9, w + 2, size * 1.25, { fill: C.head, rx: 3 });
          spans += tspan(t, { mono: true, x: cx });
          needX = true;
        } else {
          spans += r.b || r.i || r.s || needX ? tspan(t, { weight: r.b ? 700 : "", italic: r.i, deco: r.s ? "line-through" : "", x: needX ? cx : null }) : t;
          needX = false;
        }
        cx += w;
      }
      s += txt(x, baseline, spans, { fill: base.fill || C.text, size: base.size || 11, weight: base.weight || "", deco: base.deco || "", op: base.op || 1 });
    };
    if (n.data.markdown === false) {
      for (const l of wrapPreW(text, width, 11, false)) {
        if (y <= lim) s += txt(x0, y + 12, esc(l), { fill: C.text });
        y += NOTE_LINE_H;
      }
    } else {
      for (const b of noteBlocks(text)) {
        if (y > lim) break;
        if (b.kind === "liststart") {
          y += 2;
          continue;
        }
        if (b.kind === "h") {
          const size = { 1: 14, 2: 13, 3: 12 }[b.level];
          const lh = Math.round(size * 1.45);
          y += 2;
          for (const line of wrapRuns(noteRuns(b.text), width, size)) {
            emitRuns(x0, y + lh * 0.75, line, { size, weight: 700 });
            y += lh;
          }
          y += 4;
        } else if (b.kind === "code") {
          const lines = wrapPreW(b.text, width - 12, 11, true);
          y += 2;
          const h = lines.length * NOTE_LINE_H + 8;
          s += rect(x0, y, width, h, { fill: C.head, rx: 3 });
          lines.forEach((l, i) => {
            s += txt(x0 + 6, y + 4 + 12 + i * NOTE_LINE_H, esc(l), { fill: C.text, mono: true });
          });
          y += h + 4;
        } else if (b.kind === "quote") {
          const lines = wrapRuns(noteRuns(b.text), width - 10, 11);
          y += 2;
          s += rect(x0, y, 2, lines.length * NOTE_LINE_H, { fill: C.border });
          for (const line of lines) {
            emitRuns(x0 + 10, y + 12, line, { fill: C.muted });
            y += NOTE_LINE_H;
          }
          y += 2;
        } else if (b.kind === "li") {
          const lines = wrapRuns(b.task ? [{ t: `${b.task === "done" ? "☑" : "☐"} ` }, ...noteRuns(b.text)] : noteRuns(b.text), width - 18, 11);
          const done = b.task === "done";
          s += txt(x0 + (b.list === "ol" ? 14 : 8), y + 12, b.list === "ol" ? `${b.n}.` : "•", { fill: done ? C.faint : C.text, anchor: b.list === "ol" ? "end" : "middle" });
          for (const line of lines) {
            emitRuns(x0 + 18, y + 12, line, done ? { fill: C.faint, deco: "line-through" } : {});
            y += NOTE_LINE_H;
          }
          if (b.listEnd) y += 4;
        } else {
          for (const line of wrapRuns(noteRuns(b.text), width, 11)) {
            emitRuns(x0, y + 12, line);
            y += NOTE_LINE_H;
          }
          y += 4;
        }
      }
    }
    used = y + 6;
    return s;
  };

  // ---------------------------------------------------------------- mail thread

  const mailBody = (n, box, top, ctx) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.subject || n.data.mid || "Mail thread"), { fill: C.faint });
    if (!model.rows.length) {
      used = top + 30;
      return txt(10, top + 18, "No messages", { fill: C.faint, italic: true });
    }
    let s = "";
    let y = top + 4;
    const lim = box.h + ctx.sc + 40;
    const rowY = new Map();
    const boxes = Array.isArray(ctx.boxes) ? ctx.boxes : [];
    let boxN = 0;
    const lineColor = { "nm-ml-add": C.add, "nm-ml-del": C.del, "nm-ml-hunk": C.cyan, "nm-ml-sig": C.faint };
    for (const r of model.rows) {
      const y0 = y;
      const depth = r.guides.length;
      const indent = 8 + depth * 12 + 18;
      const selfMsg = model.single && r.focus;
      rowY.set(mailRowKey(r.mid), y0 + MAIL_ROW_H / 2);
      let inner = "";
      let x = 8 + depth * GUIDE_W;
      inner += txt(x + 2, y0 + 13, r.open ? "▾" : "▸", { fill: C.faint, size: 10 });
      x += 14;
      for (const b of mailBadges(r)) {
        const p = pill(x, y0 + 3, b, { size: 9, mono: false, padX: 4, h: 12, rx: 3, fg: C.blue, fill: C.blue, fop: 0.16 });
        inner += p.s;
        x += p.w + 4;
      }
      inner += txt(x, y0 + 13, esc(fit(r.subject, Math.max(4, Math.floor((box.w - x - 10) / SANS_CH)))), { fill: C.text });
      y += MAIL_ROW_H;
      const meta = `${r.author}${r.date ? ` · ${r.date}` : ""}`;
      if (meta) {
        inner += txt(indent, y + 10, esc(fit(meta, Math.max(4, Math.floor((box.w - indent - 8) / (10 * SANS_CH / 11))))), { fill: C.faint, size: 10 });
        y += MAIL_META_H; // an empty .nm-mail-meta has no height
      }
      if (r.open && !selfMsg) {
        y += 2;
        const a = pill(indent, y, r.full ? "⤡ Excerpt" : "⤢ Show full message", { size: 9, mono: false, padX: 5, h: 13, rx: 3, fg: C.faint, stroke: C.border });
        const pinOn = Boolean(r.pinned);
        const b = pill(indent + a.w + 4, y, `📌 ${pinOn ? "Pinned" : "Pin"}`, { size: 9, mono: false, padX: 5, h: 13, rx: 3, fg: pinOn ? C.text : C.faint, stroke: pinOn ? C.yellow : C.border, fill: pinOn ? C.yellow : "none", fop: 0.14 });
        inner += a.s + b.s;
        y += 13;
      }
      if (r.open) {
        const bx = selfMsg ? 8 : indent;
        const bw = box.w - 8 - bx;
        y += 2;
        if (r.full) {
          const state = { patch: false, sig: false };
          const lines = [];
          for (const l of r.loaded ? String(r.body || "").split("\n") : ["Loading…"]) {
            const cls = r.loaded ? mailLineClass(l, state) : "";
            for (const part of wrapPreW(l, bw - 14, 10, true)) lines.push([part, cls]);
          }
          const contentH = lines.length * MAIL_LINE_H + 8;
          const h = selfMsg ? contentH : Math.min(contentH, MAIL_BODY_BOX_H);
          const off = selfMsg ? 0 : Math.min(Math.max(0, boxes[boxN++] || 0), Math.max(0, contentH - h));
          inner += rect(bx, y, bw, h, { fill: C.code }) + rect(bx, y, 2, h, { fill: selfMsg ? C.border : C.blue });
          let body = "";
          lines.forEach(([l, cls], k) => {
            const ly = y + 4 + 11 + k * MAIL_LINE_H - off;
            if (ly < y - MAIL_LINE_H || ly > y + h + MAIL_LINE_H || ly > lim) return;
            const q = /nm-ml-q(\d)/.exec(cls);
            const fill = q ? [C.green, C.green, C.cyan, C.purple][Number(q[1])] : lineColor[cls] || C.text;
            body += txt(bx + 8, ly, esc(l), { fill, size: 10, mono: true, weight: cls === "nm-ml-file" ? 700 : "" });
          });
          const cid = clip(bx, y, bw, h);
          inner += `<g clip-path="url(#${cid})">${body}</g>`;
          y += h + 4;
        } else {
          const raw = r.excerpt.length ? r.excerpt.join("\n") : r.loaded ? "(empty message)" : "Loading…";
          const lines = wrapPreW(raw, bw - 14, 10, true);
          const h = lines.length * MAIL_LINE_H + 8;
          inner += rect(bx, y, bw, h, { fill: C.code }) + rect(bx, y, 2, h, { fill: C.border });
          lines.forEach((l, k) => {
            const ly = y + 4 + 11 + k * MAIL_LINE_H;
            if (ly <= lim) inner += txt(bx + 8, ly, esc(l), { fill: C.muted, size: 10, mono: true });
          });
          y += h + 4;
        }
      }
      const rowEnd = y;
      if (y0 > lim) continue;
      let guides = "";
      r.guides.forEach((g, k) => {
        const gx = 8 + k * GUIDE_W + 6;
        const mid = y0 + MAIL_ROW_H / 2;
        if (g === "pipe" || g === "tee") guides += vline(gx, y0, rowEnd, C.border);
        if (g === "end") guides += vline(gx, y0, mid, C.border);
        if (g === "tee" || g === "end") guides += hline(gx, gx + 6, mid, C.border);
      });
      if (r.focus) s += rect(0, y0, box.w, rowEnd - y0, { fill: C.blue, fop: 0.1 });
      s += guides + inner;
    }
    if (model.truncated || model.total > model.rows.length) {
      s += txt(10, y + 6 + 11, esc(`${model.rows.length} of ${model.total} messages`), { fill: C.faint, italic: true });
      y += 28;
    }
    used = y + 4;
    setRowAnchors(n, box, top, rowY, used, ctx.sc);
    return s;
  };

  // ---------------------------------------------------------------- Kconfig, MAINTAINERS

  const kconfigBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(`CONFIG_${n.data.symbol || "?"}`), { fill: C.faint });
    const values = kcValuesOf(n);
    const value = values ? kconfigValue(values, model.name) : null;
    const onCanvas = onCanvasOf(n);
    let s = "";
    let y = top + 6;
    s += txt(8, y + 12, esc(`CONFIG_${model.name}`), { fill: C.text, mono: true, weight: 700 });
    let x = 8 + textWidth(`CONFIG_${model.name}`, 11, true) + 6;
    if (model.type) {
      s += txt(x, y + 12, esc(model.type), { fill: C.type, mono: true, size: 10 });
      x += textWidth(model.type, 10, true) + 6;
    }
    const vText = value === null ? "no engine" : value || "unset";
    const vCls = value === null ? "none" : valueClass(value);
    const vCol = { y: C.green, m: C.orange, n: C.red, val: C.blue, unset: C.faint, none: C.faint }[vCls];
    s += pill(x, y + 2, vText, { size: 10, padX: 6, h: 14, rx: 7, fg: vCol, stroke: vCol, fill: vCol, fop: 0.14 }).s;
    y += KC_ROW_H + 6; // the head row and the 6-unit flex gap
    if (model.prompt) {
      for (const l of wrapW(model.prompt, box.w - 16, 11)) {
        s += txt(8, y + 12, esc(l), { fill: C.muted, italic: true });
        y += 16.5;
      }
      y += 6;
    }
    if (model.defined === false) {
      s += txt(18, y + 6 + 11, "Not defined for this ARCH", { fill: C.faint, italic: true });
      y += 28 + 6;
    }
    for (const g of model.groups) {
      s += txt(8, y + 11, esc(g.label), { fill: C.faint, size: 10, weight: 600 });
      y += 15 + 3;
      let cx = 8;
      for (const name of g.names) {
        const cw = textWidth(name, 10, true) + 14;
        if (cx + cw > box.w - 8 && cx > 8) {
          cx = 8;
          y += 15 + 4;
        }
        const on = Boolean(onCanvas?.has?.(name));
        s += pill(cx, y, name, { size: 10, padX: 6, h: 15, rx: 7.5, fg: C.text, stroke: on ? C.blue : C.border, fill: on ? C.blue : "none", fop: 0.14 }).s;
        cx += cw + 4;
      }
      y += 15 + 6;
    }
    if (!model.groups.length) {
      s += txt(18, y + 6 + 11, "No relations", { fill: C.faint, italic: true });
      y += 28;
    }
    used = y;
    return s;
  };

  const maintainerBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.title || n.data.path || n.data.query || "Maintainers"), { fill: C.faint });
    let s = "";
    let y = top + 6;
    let right = box.w - 8;
    if (model.status) {
      const col = { supported: C.green, maintained: C.blue, odd: C.orange, orphan: C.red }[statusClass(model.status)] || C.faint;
      const sw = textWidth(model.status, 10) + 14;
      right -= sw;
      s += pill(right, y + 1, model.status, { size: 10, mono: false, padX: 6, h: 14, rx: 7, fg: col, stroke: col }).s;
      right -= 6;
    }
    s += txt(8, y + 12, esc(fit(model.title || "(section)", Math.floor((right - 8) / SANS_CH))), { fill: C.text, weight: 700 });
    y += 17 + 4;
    const tagColor = { M: C.blue, R: C.type, L: C.green, T: C.preproc, F: C.number, X: C.red };
    const lines = maintainerLines(model);
    if (!lines.length) {
      s += txt(18, y + 6 + 11, "No entries", { fill: C.faint, italic: true });
      y += 28;
    }
    for (const [tag, v] of lines) {
      if (y > box.h + 40) break;
      s += txt(8, y + 12, `${tag}:`, { fill: tagColor[tag] || C.faint, mono: true, weight: 700 });
      s += txt(8 + 2 * MONO_CH + 6, y + 12, esc(fit(v, Math.floor((box.w - 22 - 2 * MONO_CH) / MONO_CH))), { fill: C.text, mono: true });
      y += MT_ROW_H;
    }
    used = y + 6;
    return s;
  };

  // ---------------------------------------------------------------- CVE, Kernel API, docs, history, commit

  /** A `.nm-*-head` row's bottom rule (border-muted, 4 below the content). */
  const headRule = (box, y) => hline(8, box.w - 8, y, C.rule);

  const cvePill = (x, y, label, col) => pill(x, y, label, { size: 9, padX: 5, h: 13, rx: 6, fg: col || C.text, stroke: col || C.border });
  const cveStateCol = (st) => ({ affected: C.red, possibly: C.orange, fixed: C.green }[st] || null);

  const cveBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.cveId || n.data.filePath || "CVE"), { fill: C.faint });
    let s = "";
    let y = top + 6;
    const lim = box.h + 400;
    const right = box.w - 8;
    if (model.mode === "file") {
      s += txt(8, y + 12, esc(fit(model.filePath || "File CVEs", Math.floor((box.w - 120) / MONO_CH))), { fill: C.text, mono: true, weight: 700 });
      s += cvePill(8 + textWidth(fit(model.filePath || "File CVEs", Math.floor((box.w - 120) / MONO_CH)), 11, true) + 6, y + 1, `${model.affectedCount} affected / ${model.total}`).s;
      y += 16 + 4;
      s += headRule(box, y);
      y += 1 + 5;
      if (!model.cves.length) {
        s += txt(18, y + 6 + 11, "No CVEs recorded for this file", { fill: C.faint, italic: true });
        y += 28;
      }
      for (const c of model.cves) {
        if (y > lim) break;
        let x = 8;
        const st = cvePill(x, y + 1, c.state, cveStateCol(c.state));
        s += st.s;
        x += st.w + 5;
        const id = btn(x, y + 1, c.cveId);
        s += id.s;
        x += id.w + 5;
        if (c.score != null) {
          const sc = cvePill(x, y + 1, String(c.score));
          s += sc.s;
          x += sc.w + 5;
        }
        s += txt(x, y + 11, esc(fit(c.title, Math.floor((right - x) / 6))), { fill: C.muted, size: 10, mono: true });
        y += 15 + 5;
      }
    } else {
      let x = 8;
      s += txt(x, y + 12, esc(model.cveId), { fill: C.text, mono: true, weight: 700 });
      x += textWidth(model.cveId, 11, true) + 6;
      const pills = [];
      if (model.score != null) pills.push([`${model.score}${model.severity ? ` ${model.severity}` : ""}`, model.score >= 7 ? C.red : model.score >= 4 ? C.orange : null]);
      if (model.state && model.state !== "unknown") pills.push([`${model.state}${model.release ? ` (${model.release})` : ""}`, cveStateCol(model.state)]);
      if (model.kev) pills.push(["KEV", C.red]);
      for (const [label, col] of pills) {
        const w = textWidth(label, 9, true) + 12;
        if (x + w > right && x > 8) {
          x = 8;
          y += 18;
        }
        s += cvePill(x, y + 1, label, col).s;
        x += w + 6;
      }
      y += 16 + 4;
      s += headRule(box, y);
      y += 1 + 5;
      if (model.title) {
        for (const l of wrapW(model.title, box.w - 16, 11, false, 600)) {
          s += txt(8, y + 12, esc(l), { fill: C.text, weight: 600 });
          y += 16;
        }
        y += 5;
      }
      if (model.versionSummary) {
        for (const l of wrapW(model.versionSummary, box.w - 16, 10)) {
          s += txt(8, y + 11, esc(l), { fill: C.muted, size: 10 });
          y += 14;
        }
        y += 5;
      }
      const label = (t) => {
        s += txt(8, y + 11, esc(t), { fill: C.faint, size: 10, weight: 600 });
        y += 14 + 3;
      };
      if (model.ranges.length) {
        label("Fix Chain");
        for (const rg of model.ranges) {
          const intro = rg.introduced || rg.inferred[0]?.sha || "";
          const introSubj = rg.introducedSubject || rg.inferred[0]?.subject || "";
          for (const [tag, sha, subj, col] of [["intro", intro, introSubj, C.orange], ["fix", rg.fixed, rg.fixedSubject, C.green]]) {
            if (!sha || y > lim) continue;
            let cx = 8;
            const t = cvePill(cx, y + 1, tag, col);
            s += t.s;
            cx += t.w + 5;
            s += txt(cx, y + 11, esc(sha.slice(0, 10)), { fill: C.text, size: 10, mono: true });
            cx += textWidth(sha.slice(0, 10), 10, true) + 5;
            const b = btnsRight(right, y + 1, ["⊕ diff"]);
            s += b.s;
            s += txt(cx, y + 11, esc(fit(subj || "", Math.floor((b.x - cx - 4) / 6))), { fill: C.muted, size: 10, mono: true });
            y += 15 + 3;
          }
        }
        y += 2;
      }
      if (model.files.length) {
        label("Affected Files");
        for (const f of model.files) {
          if (y > lim) break;
          const b = btnsRight(right, y + 1, ["⊕ src"]);
          s += b.s + txt(8, y + 11, esc(fit(f, Math.floor((b.x - 12) / 6))), { fill: C.muted, size: 10, mono: true });
          y += 15 + 3;
        }
        y += 2;
      }
      if (model.description) {
        const desc = `${model.description.slice(0, 600)}${model.description.length > 600 ? "…" : ""}`;
        for (const l of wrapW(desc, box.w - 16, 10)) {
          if (y > lim) break;
          s += txt(8, y + 11, esc(l), { fill: C.muted, size: 10 });
          y += 14;
        }
      }
    }
    used = y + 6;
    return s;
  };

  const kapiBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.symbol || n.data.catalog || "Kernel API"), { fill: C.faint });
    let s = "";
    let y = top + 6;
    const right = box.w - 8;
    let x = 8;
    const cat = cvePill(x, y + 1, model.catalog);
    s += cat.s;
    x += cat.w + 6;
    if (model.arch) s += cvePill(x, y + 1, model.arch).s;
    s += txt(right, y + 11, esc(`${model.total} entries`), { fill: C.faint, size: 10, anchor: "end" });
    y += 16 + 4;
    s += headRule(box, y);
    y += 1 + 5;
    if (!model.items.length) {
      s += txt(18, y + 6 + 11, "No matching Kernel API entries", { fill: C.faint, italic: true });
      y += 28;
    }
    for (const it of model.items) {
      if (y > box.h + 400) break;
      let cx = 8;
      if (it.kind === "catalog") {
        const c = btn(cx, y + 1, it.name);
        s += c.s;
        cx += c.w + 5;
        s += txt(cx, y + 11, esc(fit(it.subtitle || "", Math.floor((right - cx) / 6))), { fill: C.muted, size: 10, mono: true });
      } else {
        s += txt(cx, y + 11, esc(it.kind), { fill: C.blue, size: 9, mono: true });
        cx += textWidth(it.kind, 9, true) + 5;
        s += txt(cx, y + 11, esc(it.name), { fill: C.text, size: 10, mono: true, weight: 600 });
        cx += textWidth(it.name, 10, true) + 5;
        const b = btnsRight(right, y + 1, [it.symbol ? "⊕ fn" : "", it.manName ? "⊕ man" : "", it.path ? "⊕ src" : ""].filter(Boolean));
        s += b.s;
        if (it.subtitle) s += txt(cx, y + 11, esc(fit(it.subtitle, Math.floor((b.x - cx - 4) / 6))), { fill: C.muted, size: 10, mono: true });
      }
      y += 15 + 5;
    }
    used = y + 6;
    return s;
  };

  /** Vector fallback of a doc node (the live page is snapshotted when mounted): every entry and section, no caps. */
  const docBody = (n, box, top) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.path || "Kernel Docs"), { fill: C.faint });
    let s = "";
    let y = top + 8;
    const lim = box.h + 400;
    if (model.mode === "kernel-doc") {
      if (!model.entries.length) {
        s += txt(18, y + 11, "No kernel-doc comments found", { fill: C.faint, italic: true });
        y += 24;
      }
      for (const e of model.entries) {
        if (y > lim) break;
        let x = 12;
        const k = cvePill(x, y + 1, e.kind);
        s += k.s;
        x += k.w + 6;
        s += txt(x, y + 12, esc(fit(e.name, Math.floor((box.w - x - 12) / SANS_CH))), { fill: C.text, weight: 600 });
        y += 18;
        if (e.brief) {
          for (const l of wrapW(e.brief, box.w - 24, 11)) {
            s += txt(12, y + 12, esc(l), { fill: C.muted });
            y += 16;
          }
        }
        if (e.signature) {
          const lines = wrapPreW(e.signature, box.w - 36, 10, true);
          const h = lines.length * 14 + 8;
          s += rect(12, y + 2, box.w - 24, h, { fill: C.code, rx: 4 });
          lines.forEach((l, i) => {
            s += txt(18, y + 2 + 4 + 11 + i * 14, esc(l), { fill: C.text, size: 10, mono: true });
          });
          y += h + 5;
        }
        for (const p of e.params || []) {
          for (const l of wrapW(`${p.name}: ${p.desc}`, box.w - 24, 10)) {
            s += txt(12, y + 11, esc(l), { fill: C.muted, size: 10 });
            y += 14;
          }
        }
        for (const sec of e.sections || []) {
          if (sec.title) {
            s += txt(12, y + 12, esc(sec.title), { fill: C.text, weight: 700 });
            y += 16;
          }
          for (const l of wrapW(sec.body, box.w - 24, 11)) {
            s += txt(12, y + 12, esc(l), { fill: C.text });
            y += 16;
          }
        }
        y += 5;
        s += hline(12, box.w - 12, y, C.rule);
        y += 6;
      }
    } else {
      for (const sec of model.sections) {
        if (y > lim) break;
        const size = sec.level <= 1 ? 18 : sec.level === 2 ? 15 : 13;
        for (const l of wrapW(sec.heading, box.w - 24, size, false, 700)) {
          s += txt(12, y + size, esc(l), { fill: C.text, size, weight: 700 });
          y += Math.round(size * 1.4);
        }
        y += 4;
        for (const l of wrapW(sec.text, box.w - 24, 12)) {
          if (y > lim) break;
          s += txt(12, y + 13, esc(l), { fill: C.text, size: 12 });
          y += 18;
        }
        y += 6;
      }
    }
    used = y + 6;
    return s;
  };

  const historyBody = (n, box, top, ctx) => {
    const model = content(n);
    if (!model) return txt(10, top + 16, esc(n.data.name || n.data.path || "History"), { fill: C.faint });
    let s = "";
    let y = top + 6;
    const right = box.w - 8;
    const rowY = new Map();
    const lim = box.h + ctx.sc + 40;
    const blame = model.mode === "blame";
    let hr = right;
    if (blame && model.commits.length) {
      const b = btnsRight(right, y + 1, ["⇶ commits", "⇶ diffs"]);
      s += b.s;
      hr = b.x - 3;
    }
    const cnt = blame ? `${model.commits.length} commits · ${model.totalLines} lines` : `${model.changeCount} changes / ${model.totalVersions} releases`;
    s += txt(hr, y + 11, esc(cnt), { fill: C.faint, size: 10, anchor: "end" });
    const headText = blame ? model.path || "Blame" : model.name || model.path || "History";
    s += txt(8, y + 12, esc(fit(headText, Math.max(4, Math.floor((hr - textWidth(cnt, 10) - 14) / SANS_CH)))), { fill: C.text });
    y += 16 + 4;
    s += headRule(box, y);
    y += 1 + 5;
    const empty = blame ? (!model.commits.length && "No blame lines") : (!model.rows.length && "No history stops found");
    if (empty) {
      s += txt(18, y + 6 + 11, empty, { fill: C.faint, italic: true });
      y += 28;
    }
    if (blame) {
      for (const c of model.commits) {
        rowY.set(commitRowKey(c.commitHash), y + HIST_ROW_H / 2);
        if (y <= lim) {
          let x = 8;
          s += txt(x, y + 11, esc(c.commitHash.slice(0, 8)), { fill: C.text, size: 10, mono: true, weight: 600 });
          x += 8 * 6 + 5;
          const author = fit(c.author, Math.floor(110 / 6));
          s += txt(x, y + 11, esc(author), { fill: C.cyan, size: 10, mono: true });
          x += textWidth(author, 10, true) + 5;
          const b = btnsRight(right, y + 1, ["⊕ commit", "⊕ diff"]);
          s += b.s;
          const lines = `L${c.lineStart}${c.lineEnd !== c.lineStart ? `–${c.lineEnd}` : ""} (${c.count})`;
          s += txt(b.x - 2, y + 11, esc(lines), { fill: C.faint, size: 10, mono: true, anchor: "end" });
          const subjMax = Math.floor((b.x - 2 - textWidth(lines, 10, true) - 5 - x) / 6);
          if (subjMax > 1) s += txt(x, y + 11, esc(fit(c.subject, subjMax)), { fill: C.muted, size: 10, mono: true });
        }
        y += HIST_ROW_H + ROW_GAP;
      }
    } else {
      const stCol = { removed: C.red, modified: C.orange, introduced: C.green, added: C.green };
      for (const r of model.rows) {
        rowY.set(releaseRowKey(r.vname), y + HIST_ROW_H / 2);
        if (y <= lim) {
          let x = 8;
          s += txt(x, y + 11, esc(r.vname), { fill: C.text, size: 10, mono: true, weight: 600 });
          x += textWidth(r.vname, 10, true) + 5;
          s += cvePill(x, y + 1, r.status, stCol[r.status] || null).s;
          const b = btnsRight(right, y + 1, r.prevVname && r.path ? ["⊕ diff"] : []);
          s += b.s;
          const delta = r.added || r.removed ? `+${r.added} −${r.removed}` : (r.lineS ? `L${r.lineS}` : "");
          if (delta) s += txt(b.x - 2, y + 11, esc(delta), { fill: C.faint, size: 10, mono: true, anchor: "end" });
        }
        y += HIST_ROW_H + ROW_GAP;
      }
    }
    used = y + 6 - ROW_GAP;
    setRowAnchors(n, box, top, rowY, used, ctx.sc);
    return s;
  };

  /** A nested NodeMap node is a titled box with its name and counts (the preview is a live-view feature). */
  const nodemapBody = (n, box, top) => {
    let s = "";
    let y = top + 6;
    for (const line of nestedExportLines(n, nestedCardOf(n))) {
      for (const l of wrapW(line, box.w - 16, 12, false, 400)) {
        s += txt(8, y + 12, esc(l), { fill: C.text, size: 12 });
        y += 16.2;
      }
    }
    used = Math.max(used, y);
    return s;
  };

  const commitBody = (n, box, top) => {
    const model = content(n);
    if (!model?.hash) return txt(10, top + 16, esc(n.data.commitHash ? `Commit ${n.data.commitHash.slice(0, 12)}` : "Commit"), { fill: C.faint });
    let s = "";
    let y = top + 6;
    const right = box.w - 8;
    const lim = box.h + 400;
    const GAP = 4;
    for (const l of wrapW(model.subject || "(no subject)", box.w - 16, 12, false, 700)) {
      s += txt(8, y + 12, esc(l), { fill: C.text, size: 12, weight: 700 });
      y += 16.2;
    }
    y += GAP;
    const kv = (k) => {
      s += txt(8, y + 11, esc(k), { fill: C.faint, size: 10, mono: true });
      return 8 + textWidth(k, 10, true) + 6;
    };
    const vx = kv("commit");
    for (const l of wrapPreW(model.hash, right - vx, 10, true)) {
      s += txt(vx, y + 11, esc(l), { fill: C.blue, size: 10, mono: true });
      y += 15;
    }
    y += GAP;
    const person = (label, p) => {
      if (!p || !(p.name || p.email)) return;
      const x = kv(label);
      const who = `${p.name}${p.email ? ` <${p.email}>` : ""}`;
      const whoW = textWidth(who, 11);
      const avail = right - x;
      s += txt(x, y + 11, esc(fit(who, Math.floor(avail / SANS_CH))), { fill: C.muted });
      if (p.date) {
        if (whoW + 6 + textWidth(p.date, 10) > avail) {
          y += 15;
          s += txt(x, y + 11, esc(p.date), { fill: C.faint, size: 10 });
        } else s += txt(x + whoW + 6, y + 11, esc(p.date), { fill: C.faint, size: 10 });
      }
      y += 16.5 + GAP;
    };
    person("Author", model.author);
    if (model.committer) person("Committer", model.committer);
    if (model.parents.length) {
      let x = kv(model.parents.length > 1 ? "Parents" : "Parent");
      for (const p of model.parents) {
        const w = textWidth(`⊕ ${p.slice(0, 12)}`, 9, true) + 12;
        if (x + w > right && x > 60) {
          x = 60;
          y += 17;
        }
        s += btn(x, y + 1.5, `⊕ ${p.slice(0, 12)}`).s;
        x += w + 4;
      }
      y += 16.5 + GAP;
    }
    const od = btn(8, y, "± Open diff");
    s += od.s;
    if (model.isMerge) s += txt(8 + od.w + 6, y + 10, "merge (diff against the first parent)", { fill: C.faint, size: 10 });
    y += 14 + GAP;
    if (model.body) {
      const lines = wrapPreW(model.body, box.w - 28, 10, true);
      const h = lines.length * 14 + 8;
      s += rect(8, y, box.w - 16, h, { fill: C.code, rx: 4 });
      lines.forEach((l, i) => {
        const ly = y + 4 + 11 + i * 14;
        if (ly <= lim) s += txt(14, ly, esc(l), { fill: C.text, size: 10, mono: true });
      });
      y += h + GAP;
    }
    if (model.trailers.length) {
      for (const t of model.trailers) {
        if (y > lim) break;
        const k = `${t.key}:`;
        s += txt(8, y + 11, esc(k), { fill: C.green, size: 10, mono: true });
        const x = 8 + textWidth(k, 10, true) + 6;
        const v = fit(t.value, Math.floor((right - x - (t.sha ? 24 : 0)) / textWidth("0", 10, true)));
        s += txt(x, y + 11, esc(v), { fill: C.muted, size: 10, mono: true });
        if (t.sha) s += btn(x + textWidth(v, 10, true) + 6, y + 1, "⊕").s;
        y += 15 + 2;
      }
      y += GAP - 2;
    }
    const filesHead = `Files (${model.files.length}${model.filesTruncated ? "+" : ""})`;
    s += txt(8, y + 12, esc(filesHead), { fill: C.text });
    if (model.fileSummary) s += txt(right, y + 11, esc(fit(model.fileSummary, Math.floor((right - 8 - textWidth(filesHead, 11) - 8) / 6))), { fill: C.faint, size: 10, anchor: "end" });
    y += 16 + 4;
    s += headRule(box, y);
    y += 1 + 2;
    const stCol = { A: C.green, D: C.red, R: C.blue, C: C.blue };
    for (const f of model.files) {
      if (y > lim) break;
      s += txt(8, y + 11, esc(f.status), { fill: stCol[f.status] || C.orange, size: 10, mono: true, weight: 700 });
      const b = btnsRight(right, y + 1, ["⊕ diff"]);
      s += b.s + txt(8 + textWidth(f.status, 10, true) + 5, y + 11, esc(fit(f.path, Math.floor((b.x - 20) / 6))), { fill: C.muted, size: 10, mono: true });
      y += 15 + 2;
    }
    if (!model.files.length) {
      s += txt(18, y + 6 + 11, "No changed files recorded", { fill: C.faint, italic: true });
      y += 28;
    }
    used = y + 6;
    return s;
  };

  const symbolBody = (n, box, top) => {
    const d = n.data;
    if (!d.expanded) return txt(10, top + 20, esc(`${d.kind === "func" ? "Function" : "Symbol"} · ${n.version}`), { fill: C.faint });
    const list = d.constituents || [];
    if (!list.length) return txt(10, top + 20, "No members found", { fill: C.faint, italic: true });
    let s = "";
    // `.nm-sym-list .nm-sf` rows: the live rows' measured heights, else 1 + 16.5 + 1 units
    const measured = rowHeightsOf(n);
    const hs = Array.isArray(measured) && measured.length === list.length && measured.every((v) => v > 0) ? measured : null;
    let y = top;
    list.forEach((c, i) => {
      const rh = hs ? hs[i] : 18.5;
      if (y <= box.h + 20) s += txt(ROW_BORDER + 8, y + rh / 2 + 4, esc(c.name), { fill: C.text });
      y += rh;
    });
    used = y;
    return s;
  };

  const cardBody = (box, top, lines) => {
    let s = "";
    lines.forEach(([t, o], i) => {
      s += txt(box.w / 2, top + (box.h - top) / 2 - ((lines.length - 1) * 16) / 2 + i * 16, esc(fit(t, Math.floor((box.w - 20) / 6))), { fill: o?.fill || C.muted, size: o?.size || 11, mono: Boolean(o?.mono), anchor: "middle", italic: o?.italic });
    });
    return s;
  };

  // ---------------------------------------------------------------- frames

  /** Lock badges of a node's `.nm-node-locks` strip, wrapped like the flex row: `{s, h}`. */
  const lockStrip = (locks, w, y) => {
    if (!locks.length) return { s: "", h: 0 };
    let inner = "";
    let lx = 8;
    let ly = y + LOCK_PAD;
    for (const lk of locks) {
      const lw = lockBadgeW(lk);
      if (lx + lw > w - 8 && lx > 8) {
        lx = 8;
        ly += LOCK_BADGE_H + 4;
      }
      inner += lockBadge(lx, ly, lk).s;
      lx += lw + 4;
    }
    const h = ly + LOCK_BADGE_H + LOCK_PAD + 1 - y;
    return { s: rect(0, y, w, h, { fill: C.code }) + hline(0, w, y + h - 0.5, C.rule) + inner, h };
  };

  const rawHl = opts.tourHighlights;
  const hlNodeIds = rawHl?.nodeIds instanceof Set ? rawHl.nodeIds : new Set(Array.isArray(rawHl?.nodeIds) ? rawHl.nodeIds : []);
  const hlEdgeIds = rawHl?.edgeIds instanceof Set ? rawHl.edgeIds : new Set(Array.isArray(rawHl?.edgeIds) ? rawHl.edgeIds : []);
  const hlActive = Boolean(rawHl && (rawHl.active ?? (hlNodeIds.size > 0 || hlEdgeIds.size > 0)));
  const rects = new Map();
  const nodeSvg = (n) => {
    const accent = R(accentVar(n.color, defaultAccent(n.type)));
    const locks = Array.isArray(n.locks) ? n.locks.filter((l) => l?.kind) : [];
    const focused = hlActive && hlNodeIds.has(n.id);
    const dimmed = hlActive && !focused;
    if (n.type === "group") {
      rects.set(n.id, { x: n.x, y: n.y, w: n.w, h: n.h });
      const fill = n.bgColor ? bgOf(n.bgColor, "var(--bg-secondary)") : accent;
      let g = `<g transform="translate(${n.x} ${n.y})">` +
        rect(0.75, 0.75, n.w - 1.5, n.h - 1.5, { fill, fop: n.bgColor ? 1 : 0.05, stroke: mix(accent, C.border, 60), sw: 1.5, rx: 6, dash: "6 4" });
      const GH = 29;
      const gb = pill(9.5, 8.5, NODE_BADGES.group || "group", { size: 9, mono: false, padX: 4, h: 13, rx: 3, fg: accent, fill: accent, fop: 0.16, upper: true, ls: 0.27 });
      const gid = clip(gb.w + 15, 0, Math.max(0, n.w - gb.w - 24), GH);
      g += gb.s + `<g clip-path="url(#${gid})">${txt(gb.w + 15.5, 19.5, esc(displayTitle(n)), { fill: C.text, weight: 600, size: 12 })}</g>`;
      if (n.data?.subCanvas) g += hline(1, n.w - 1, GH, C.border, 1, "4 3");
      const ls = lockStrip(locks, n.w, GH);
      if (ls.h) {
        const cid = clip(0, 0, n.w, n.h, 6);
        g += `<g clip-path="url(#${cid})">${ls.s}</g>`;
      }
      if (focused) g += rect(-3, -3, n.w + 6, n.h + 6, { stroke: C.yellow, sw: 2, rx: 8 });
      const out = `${g}</g>\n`;
      return dimmed ? `<g opacity="0.2">${out}</g>\n` : out;
    }
    let box = layoutBox(n);
    const headShown = !n.hideHeader || editMode;
    const strip = lockStrip(locks, box.w, headShown ? HEAD_H : 0);
    const top = (headShown ? HEAD_H : 0) + strip.h;
    const scr = scrollOf(n);
    const ctx = { accent, sc: n.autoFit ? 0 : Math.max(0, Number(scr?.top) || 0), boxes: scr?.boxes || [] };
    const selfScroll = n.type === "struct";
    used = 0;
    let body = "";
    if (n.type === "struct") body = structBody(n, box, top, accent, ctx.sc);
    else if (n.type === "source") body = sourceBody(n, box, top, ctx);
    else if (n.type === "note") body = noteBody(n, box, top, ctx);
    else if (n.type === "symbol") body = symbolBody(n, box, top);
    else if (n.type === "diff") body = diffBody(n, box, top, ctx);
    else if (n.type === "mail") body = mailBody(n, box, top, ctx);
    else if (n.type === "kconfig") body = kconfigBody(n, box, top);
    else if (n.type === "maintainer") body = maintainerBody(n, box, top);
    else if (n.type === "cve") body = cveBody(n, box, top);
    else if (n.type === "kapi") body = kapiBody(n, box, top);
    else if (n.type === "doc") body = docBody(n, box, top);
    else if (n.type === "history") body = historyBody(n, box, top, ctx);
    else if (n.type === "commit") body = commitBody(n, box, top);
    else if (n.type === "nodemap") body = nodemapBody(n, box, top);
    if (n.autoFit && used > top && used < box.h) box = { ...box, h: Math.ceil(used) };
    // a scrolled body shows the same window as the live node (clamped to its content)
    const sc = selfScroll ? 0 : Math.min(ctx.sc, Math.max(0, used - box.h));
    rects.set(n.id, { x: n.x, y: n.y, w: n.w, h: Math.round(box.h * box.s) });
    const tf = box.s !== 1 ? `translate(${n.x} ${n.y}) scale(${box.s})` : `translate(${n.x} ${n.y})`;
    let s = `<g transform="${tf}">`;
    const noteTint = n.type === "note" && !n.bgColor;
    const panel = n.bgColor ? bgOf(n.bgColor, "var(--bg-secondary)") : noteTint ? mix(accent, C.panel, n.color ? 10 : 8) : C.panel;
    // the panel is painted once: with its --shadow-sm filter on opaque images, inside the clip otherwise
    const shadowed = Boolean(shadowDef) && !opts.transparent;
    if (shadowed) {
      shadowUsed = true;
      s += `<rect x="0" y="0" width="${r1(box.w)}" height="${r1(box.h)}" rx="6"${F(panel)} filter="url(#${shadowId})"/>`;
    }
    const rid = clipRound(box.w, box.h);
    s += `<g clip-path="url(#${rid})">`;
    if (!shadowed) s += rect(0, 0, box.w, box.h, { fill: panel });
    if (headShown) {
      const dashed = n.hideHeader;
      // live geometry: 1-unit node border, 3-unit top border, 5-unit padding, an 18-unit line, 1-unit bottom border
      let h = rect(0, 0, box.w, HEAD_H, { fill: C.head });
      h += dashed ? hline(0, box.w, 2.5, n.color ? accent : C.rule, 1, "6 4", 3) : rect(0, 1, box.w, 3, { fill: n.color ? accent : C.rule });
      h += hline(0, box.w, HEAD_H - 0.5, C.border, 1, dashed ? "4 3" : "");
      const badge = NODE_BADGES[n.type] || n.type;
      const bp = pill(9, 11.5, badge, { size: 9, mono: false, padX: 4, h: 13, rx: 3, fg: accent, fill: accent, fop: 0.16, upper: true, ls: 0.27 });
      h += bp.s;
      const meta = headMetaOf(n.type, n.type === "symbol" || n.type === "note" || n.type === "tab" || n.type === "man" ? null : content(n)).text;
      const metaCols = meta ? Math.min(meta.length, Math.floor((box.w * 0.55) / 6)) : 0;
      if (meta) {
        const shown = meta.length > metaCols ? `…${meta.slice(meta.length - metaCols + 1)}` : meta;
        h += txt(box.w - 9, 21.5, esc(shown), { fill: C.faint, size: 10, mono: true, anchor: "end" });
      }
      // `.nm-title` clips (overflow hidden, no ellipsis) where the meta starts
      const tx = 9 + bp.w + 6;
      const tid = clip(tx, 0, Math.max(0, box.w - 9 - (metaCols ? metaCols * 6 + 6 : 0) - tx), HEAD_H);
      h += `<g clip-path="url(#${tid})">${txt(tx, 22.5, esc(displayTitle(n)), { fill: C.text, weight: 600, size: 12 })}</g>`;
      s += dashed ? `<g opacity="0.45">${h}</g>` : h;
    }
    s += strip.s;
    const bid = clip(0, top, box.w, box.h - top);
    const live = n.type === "tab" || n.type === "man" || n.type === "doc";
    const raster = live ? rasterOf(n) : null;
    const rasterOk = Boolean(raster?.href) && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(raster.href) && raster.w > 0 && raster.h > 0;
    const fo = live && !rasterOk ? foreignOf(n) : null;
    if (rasterOk) {
      body = `<image x="0" y="${top}" width="${r1(raster.w)}" height="${r1(raster.h)}" preserveAspectRatio="none" href="${raster.href}"/>`;
    } else if (fo?.html) {
      const bh = Math.max(0, box.h - top);
      body = `<foreignObject x="0" y="${top}" width="${box.w}" height="${bh}"><div xmlns="http://www.w3.org/1999/xhtml" style="width:${box.w}px;height:${bh}px;overflow:hidden">${String(fo.html).replace(XML_ILLEGAL, "")}</div></foreignObject>`;
    } else if (n.type === "tab") {
      const hash = String(n.data.hash || "");
      body = cardBody(box, top, [[`Live ${n.data.tabType || "KernelInfo"} tab`, { fill: C.text, size: 12 }], [`#${hash}`, { fill: C.faint, size: 10, mono: true }], ["(embedded view: not part of the image)", { fill: C.faint, size: 10, italic: true }]]);
    } else if (n.type === "man") {
      const d = n.data;
      body = cardBody(box, top, [[d.name ? `${d.name}(${d.section || "?"})` : (d.path || "Man page"), { fill: C.text, size: 12 }], [d.path ? `${d.repo ? `${d.repo} · ` : ""}${d.path}` : "", { fill: C.faint, size: 10, mono: true }]]);
    } else if (sc > 0) body = `<g transform="translate(0 ${r1(-sc)})">${body}</g>`;
    s += `<g clip-path="url(#${bid})">${body}</g>`;
    s += "</g>";
    s += rect(0, 0, box.w, box.h, { stroke: n.color ? mix(accent, C.border, 55) : C.border, rx: 6 });
    if (focused) {
      const inv = 1 / (box.s || 1);
      s += rect(-3 * inv, -3 * inv, box.w + 6 * inv, box.h + 6 * inv, { stroke: C.yellow, sw: 2 * inv, rx: 8 });
    }
    const out = `${s}</g>\n`;
    return dimmed ? `<g opacity="0.2">${out}</g>\n` : out;
  };

  const ordered = paintOrder(visCanvas.nodes);
  const groupNodes = ordered.filter((x) => x.type === "group");
  const leafNodes = ordered.filter((x) => x.type !== "group");
  const orderedGroups = hlActive ? [...groupNodes.filter((x) => !hlNodeIds.has(x.id)), ...groupNodes.filter((x) => hlNodeIds.has(x.id))] : groupNodes;
  const orderedLeaves = hlActive ? [...leafNodes.filter((x) => !hlNodeIds.has(x.id)), ...leafNodes.filter((x) => hlNodeIds.has(x.id))] : leafNodes;
  const groupsSvg = orderedGroups.map(nodeSvg).join("");
  const nodesSvg = orderedLeaves.map(nodeSvg).join("");
  const byId = new Map(visCanvas.nodes.map((n) => [n.id, { ...n, ...(rects.get(n.id) || {}) }]));
  const pointOf = (n, field, side) => {
    const y = field ? anchors.get(n.id)?.get(field) : undefined;
    if (y === undefined) return null;
    const s = layoutBox(n).s;
    return { x: side === "left" ? n.x : n.x + n.w, y: n.y + y * s };
  };
  let edgesNormalSvg = "";
  let edgesFocusSvg = "";
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
    // the live label (`_edgeInnerSvg`): the lock kind joins the text in parentheses unless it names it
    const hasLockInLabel = e.lockKind && String(e.label || "").toLowerCase().includes(String(e.lockKind).toLowerCase());
    const edgeLabel = [e.label, e.lockKind && !hasLockInLabel ? `(🔒 ${e.lockKind})` : ""].filter(Boolean).join(" ");
    extra.push(ends.p1, ends.p2, g.mid, ...(g.controls || []), ...(e.waypoints || []));
    if (edgeLabel) {
      const half = textWidth(edgeLabel, 11) / 2 + 4;
      extra.push({ x: g.mid.x - half, y: g.mid.y - 20 }, { x: g.mid.x + half, y: g.mid.y });
    }
    const c = R(accentVar(e.color, "var(--accent-blue)"));
    const dash = dashArray(e.style);
    const eFocused = hlActive && hlEdgeIds.has(e.id);
    const eDimmed = hlActive && !eFocused;
    let eSvg = `<path d="${g.d}" fill="none"${S(c, eFocused ? 1 : 0.9)} stroke-width="${eFocused ? 3.5 : 2}"${dash ? ` stroke-dasharray="${dash}"` : ""}/>\n`;
    for (const h of heads) eSvg += `<polygon points="${h}"${F(c)}/>\n`;
    const halo = opts.transparent ? "" : `${S(C.bg)} stroke-width="4" paint-order="stroke"`;
    if (edgeLabel) eSvg += `<text x="${r1(g.mid.x)}" y="${r1(g.mid.y - 6)}" text-anchor="middle"${F(C.text)}${halo} font-family="${FONT.sans}" font-size="11">${esc(edgeLabel)}</text>\n`;
    if (eDimmed) edgesNormalSvg += `<g opacity="0.2">${eSvg}</g>\n`;
    else if (eFocused) edgesFocusSvg += eSvg;
    else edgesNormalSvg += eSvg;
  }
  const edgesSvg = edgesNormalSvg + edgesFocusSvg;

  const nb = boundsOf([...byId.values()]) || { x: 0, y: 0, w: 400, h: 300 };
  let x1 = nb.x, y1 = nb.y, x2 = nb.x + nb.w, y2 = nb.y + nb.h;
  for (const p of extra) {
    x1 = Math.min(x1, p.x);
    y1 = Math.min(y1, p.y);
    x2 = Math.max(x2, p.x);
    y2 = Math.max(y2, p.y);
  }
  const vb = opts.viewportBox;
  const hasVb = Boolean(vb && Number.isFinite(Number(vb.x)) && Number.isFinite(Number(vb.y)) && Number(vb.w) > 0 && Number(vb.h) > 0 && Number(vb.width) > 0 && Number(vb.height) > 0);
  const vx = hasVb ? Number(vb.x) : Math.floor(x1 - PAD);
  const vy = hasVb ? Number(vb.y) : Math.floor(y1 - PAD);
  const vw = hasVb ? Number(vb.w) : Math.ceil(x2 - x1 + PAD * 2);
  const vh = hasVb ? Number(vb.h) : Math.ceil(y2 - y1 + PAD * 2);
  const ps = Number(opts.pixelScale) > 0 ? Number(opts.pixelScale) : 1;
  const pw = hasVb ? Math.max(1, Math.round(Number(vb.width))) : (ps === 1 ? vw : Math.max(1, Math.floor(vw * ps)));
  const ph = hasVb ? Math.max(1, Math.round(Number(vb.height))) : (ps === 1 ? vh : Math.max(1, Math.floor(vh * ps)));
  const vbAttr = hasVb ? `${+vx.toFixed(4)} ${+vy.toFixed(4)} ${+vw.toFixed(4)} ${+vh.toFixed(4)}` : `${vx} ${vy} ${vw} ${vh}`;
  // Snap grid (Inspector → Canvas → Snap): the live canvas draws border-color dots every `grid` units
  const grid = !opts.transparent && visCanvas.settings?.snap && Number(visCanvas.settings?.grid) >= 4 ? Number(visCanvas.settings.grid) : 0;
  if (grid) defs += `<pattern id="nmgrid" x="0" y="0" width="${grid}" height="${grid}" patternUnits="userSpaceOnUse"><circle cx="${grid / 2}" cy="${grid / 2}" r="1"${F(C.border)}/></pattern>`;
  if (shadowUsed) defs += shadowDef;
  let s = `<svg xmlns="http://www.w3.org/2000/svg" width="${pw}" height="${ph}" viewBox="${vbAttr}"${hasVb ? ' preserveAspectRatio="xMidYMid meet"' : ""}>\n`;
  if (defs) s += `<defs>${defs}</defs>\n`;
  if (!opts.transparent) s += `<rect x="${vx}" y="${vy}" width="${vw}" height="${vh}"${F(C.bg)}/>\n`;
  if (grid) s += `<rect x="${vx}" y="${vy}" width="${vw}" height="${vh}" fill="url(#nmgrid)"/>\n`;
  /* ui-scale-exempt-end */
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
  "display", "position", "top", "right", "bottom", "left", "z-index", "box-sizing", "width", "min-width", "max-width",
  "height", "min-height", "max-height", "aspect-ratio", "object-fit", "object-position", "float", "clear",
  "margin-top", "margin-right", "margin-bottom", "margin-left", "padding-top", "padding-right", "padding-bottom", "padding-left",
  "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
  "border-top-style", "border-right-style", "border-bottom-style", "border-left-style",
  "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
  "border-top-left-radius", "border-top-right-radius", "border-bottom-right-radius", "border-bottom-left-radius",
  "background-color", "background-image", "background-size", "background-position", "background-repeat", "background-clip",
  "box-shadow", "outline-width", "outline-style", "outline-color", "outline-offset",
  "color", "font-family", "font-size", "font-weight", "font-style", "font-variant", "font-stretch",
  "font-feature-settings", "font-variant-numeric", "font-variant-ligatures",
  "line-height", "letter-spacing", "word-spacing", "text-align", "text-decoration-line", "text-decoration-color",
  "text-decoration-style", "text-decoration-thickness", "text-underline-offset", "text-shadow", "text-transform",
  "text-indent", "text-overflow", "white-space", "word-break", "overflow-wrap", "hyphens", "direction", "unicode-bidi",
  "writing-mode", "vertical-align", "opacity", "transform", "transform-origin",
  "visibility", "flex-direction", "flex-wrap", "flex-grow", "flex-shrink", "flex-basis", "justify-content",
  "align-items", "align-self", "align-content", "justify-items", "justify-self", "order", "row-gap", "column-gap",
  "grid-template-columns", "grid-template-rows", "grid-auto-flow", "grid-auto-columns", "grid-auto-rows",
  "grid-column-start", "grid-column-end", "grid-row-start", "grid-row-end", "column-count", "list-style-type",
  "list-style-position", "list-style-image", "table-layout", "border-collapse", "border-spacing", "tab-size",
  "clip-path", "mask-image", "-webkit-mask-image", "filter",
  "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-dasharray", "stroke-linecap",
  "stroke-linejoin", "stop-color", "stop-opacity", "text-anchor", "dominant-baseline", "overflow-x", "overflow-y",
];
const FO_DROP_TAGS = new Set(["script", "noscript", "style", "link", "meta", "template", "iframe", "frame", "object", "embed", "audio", "video", "source", "track", "base"]);
const FO_DROP_SEL = ".nm-shield, .context-menu, .tooltip, .toast, .toast-container, [role='tooltip'], .loading-overlay, .spinner, .nm-tour-export-ui, [data-tour-export-ui]";
/** Elements cloned per snapshot; a larger subtree keeps its first ones in document order (the rest is cut). */
const FO_MAX_ELEMENTS = 20000;
const ATTR_NAME_RE = /^(?!xmlns)[A-Za-z_][\w.-]*$/i;
const SVG_NS = "http://www.w3.org/2000/svg";
// Attributes that would load something or name a resource of the live document
const FO_DROP_ATTRS = new Set(["id", "style", "srcset", "sizes", "background", "ping", "integrity", "crossorigin", "loading"]);
// Properties whose value is a list of layers (a `url()` layer is dropped, gradients stay)
const LAYERED_PROPS = new Set(["background-image", "mask-image", "-webkit-mask-image"]);
// SVG properties whose `url(#id)` may point at a gradient / clip / mask / filter of the same snapshot
const SVG_REF_PROPS = new Set(["fill", "stroke", "clip-path", "mask-image", "-webkit-mask-image", "filter"]);
const COLOR_PROPS = new Set([
  "color", "background-color", "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
  "outline-color", "text-decoration-color", "stop-color",
]);
let foSeq = 0;

/** Split a CSS value at its top-level commas (not inside parentheses or quotes). */
function splitTopLevel(value) {
  const out = [];
  let depth = 0;
  let quote = "";
  let cur = "";
  for (const ch of String(value)) {
    if (quote) {
      if (ch === quote) quote = "";
    } else if (ch === "\"" || ch === "'") quote = ch;
    else if (ch === "(") depth += 1;
    else if (ch === ")") depth = Math.max(0, depth - 1);
    else if (ch === "," && depth === 0) {
      out.push(cur.trim());
      cur = "";
      continue;
    }
    cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

const URL_RE = /url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*?))\s*\)/gi;

/**
 * A computed value with nothing that could taint the snapshot or point outside it: `data:` URLs stay
 * (inlined assets), a `url(#id)` of an SVG paint / clip / mask / filter is renamed to the snapshot's copy
 * of `id` (`ids`), any other `url()` drops its layer (layered properties) or the whole value (`null`).
 * `var()` / `color-mix()` left in a color are evaluated (`utils/css_color.js`).
 */
export function sanitizeCssValue(prop, value, { ids = null, read = null } = {}) {
  let v = String(value ?? "");
  if (!v) return null;
  if (/url\(/i.test(v)) {
    const fix = (part) => {
      let ok = true;
      const out = part.replace(URL_RE, (m, a, b, c) => {
        const url = String(a ?? b ?? c ?? "").trim();
        if (/^data:/i.test(url)) return `url("${url.replace(/"/g, "%22")}")`;
        if (url.startsWith("#") && SVG_REF_PROPS.has(prop) && ids?.has(url.slice(1))) return `url("#${ids.get(url.slice(1))}")`;
        ok = false;
        return m;
      });
      return ok ? out : null;
    };
    if (LAYERED_PROPS.has(prop)) {
      const layers = splitTopLevel(v).map(fix).filter(Boolean);
      if (!layers.length) return null;
      v = layers.join(", ");
    } else {
      const fixed = fix(v);
      if (fixed === null) {
        // an SVG paint keeps its fallback color (`url(#x) rgb(…)`), anything else is left out
        if (prop === "fill" || prop === "stroke") {
          const fallback = v.replace(URL_RE, "").trim();
          return fallback && fallback !== "none" ? fallback : null;
        }
        return null;
      }
      v = fixed;
    }
  }
  if (COLOR_PROPS.has(prop) || prop === "fill" || prop === "stroke") {
    if (/var\(|color-mix\(/i.test(v)) {
      const c = evalCssColor(v, read);
      return c ? formatCssColor(c) : null;
    }
  }
  return v;
}

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
    // throws for an image of another origin: the snapshot leaves it out instead of tainting the export
    return c.toDataURL("image/png");
  } catch {
    return null;
  }
}

/** An empty box of `src`'s size (an image that cannot be inlined keeps its place in the layout). */
function blankBox(src, doc) {
  const box = doc.createElement("span");
  const w = src.clientWidth || src.width || 0;
  const h = src.clientHeight || src.height || 0;
  box.setAttribute("style", `display:inline-block;width:${w}px;height:${h}px;`);
  return box;
}

/** Copy of `<use href="#x">`'s target (a `<symbol>` as a nested `<svg>`), for a target outside the snapshot. */
function inlineUse(src, win, ctx) {
  const ref = String(src.getAttribute?.("href") || src.getAttribute?.("xlink:href") || "");
  if (!ref.startsWith("#")) return null;
  const doc = src.ownerDocument;
  const target = doc.getElementById?.(ref.slice(1));
  if (!target || ctx.root?.contains?.(target) || ctx.inlining > 4) return null;
  const isSymbol = String(target.localName || "").toLowerCase() === "symbol";
  const out = doc.createElementNS(SVG_NS, isSymbol ? "svg" : "g");
  const num = (a) => Number(src.getAttribute?.(a)) || 0;
  if (isSymbol) {
    for (const a of ["x", "y", "width", "height"]) if (src.getAttribute?.(a)) out.setAttribute(a, String(num(a)));
    if (!src.getAttribute?.("width")) {
      const r = src.getBoundingClientRect?.();
      if (r?.width) out.setAttribute("width", String(Math.round(r.width)));
      if (r?.height) out.setAttribute("height", String(Math.round(r.height)));
    }
    const vb = target.getAttribute?.("viewBox");
    if (vb) out.setAttribute("viewBox", vb);
  } else if (num("x") || num("y")) out.setAttribute("transform", `translate(${num("x")} ${num("y")})`);
  ctx.inlining += 1;
  try {
    for (const child of (isSymbol ? target.childNodes : [target])) {
      const c = cloneStyled(child, win, ctx);
      if (c) out.appendChild(c);
    }
  } finally {
    ctx.inlining -= 1;
  }
  return out;
}

/** Ids of the SVG elements inside `root` renamed for one snapshot (`fo<n>-<id>`): id -> new id. */
function svgIdsOf(root) {
  const ids = new Map();
  const prefix = `fo${(foSeq = (foSeq + 1) % 1e6)}-`;
  for (const el of root.querySelectorAll?.("svg [id]") || []) {
    if (el.namespaceURI === SVG_NS && !ids.has(el.id)) ids.set(el.id, `${prefix}${el.id}`.replace(/[^\w.-]/g, "_"));
  }
  return ids;
}

function cloneStyled(src, win, ctx) {
  if (src.nodeType === 3) return src.cloneNode(false);
  if (src.nodeType !== 1 || ctx.n-- <= 0) return null;
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
  const isSvg = src.namespaceURI === SVG_NS;
  if (isSvg && tag === "use") {
    const inlined = inlineUse(src, win, ctx);
    if (inlined) return inlined;
  }
  let el;
  if (tag === "canvas" || (tag === "img" && !isSvg)) {
    const url = dataUrlOf(src, doc);
    if (!url) return blankBox(src, doc);
    el = doc.createElement("img");
    el.setAttribute("src", url);
    el.setAttribute("alt", "");
    el.setAttribute("width", String(src.width || src.clientWidth || 0));
    el.setAttribute("height", String(src.height || src.clientHeight || 0));
  } else {
    el = src.cloneNode(false);
    const xlink = el.getAttribute?.("xlink:href");
    for (const a of [...(el.attributes || [])]) {
      const name = a.name.toLowerCase();
      const value = String(a.value);
      if (isSvg && name === "id" && ctx.ids.has(value)) {
        el.setAttribute("id", ctx.ids.get(value));
        continue;
      }
      if (name.startsWith("on") || FO_DROP_ATTRS.has(name) || !ATTR_NAME_RE.test(a.name) || /var\(/i.test(value)) {
        el.removeAttribute(a.name);
      } else if (name === "src" || name === "poster" || (name === "href" && isSvg)) {
        // a resource reference: inlined data only, or (SVG) an element of this snapshot
        if (/^data:/i.test(value)) continue;
        if (name === "href" && value.startsWith("#") && ctx.ids.has(value.slice(1))) el.setAttribute("href", `#${ctx.ids.get(value.slice(1))}`);
        else el.removeAttribute(a.name);
      }
    }
    if (xlink && isSvg && !el.getAttribute("href")) {
      if (xlink.startsWith("#") && ctx.ids.has(xlink.slice(1))) el.setAttribute("href", `#${ctx.ids.get(xlink.slice(1))}`);
      else if (/^data:/i.test(xlink)) el.setAttribute("href", xlink);
    }
    if (tag === "input" || tag === "textarea") el.setAttribute("value", String(src.value ?? ""));
    // an inline SVG keeps explicit dimensions (its CSS size may come from a stylesheet left behind)
    if (tag === "svg" && isSvg) {
      const r = src.getBoundingClientRect?.();
      if (r?.width && !el.getAttribute("width")) el.setAttribute("width", String(Math.round(r.width * 100) / 100));
      if (r?.height && !el.getAttribute("height")) el.setAttribute("height", String(Math.round(r.height * 100) / 100));
    }
  }
  const read = (name) => cs.getPropertyValue(name);
  let style = "";
  for (const p of FO_PROPS) {
    let v = cs.getPropertyValue(p);
    if (!v) continue;
    if ((p === "overflow-x" || p === "overflow-y") && (v === "auto" || v === "scroll")) v = "hidden";
    v = sanitizeCssValue(p, v, { ids: ctx.ids, read });
    if (v === null) continue;
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
  // a scrolled box shows its scroll position: a block box through a shifted wrapper, a flex / grid /
  // table box (a wrapper would break its layout) by shifting each item
  const itemShift = (st || sl) && /^(flex|inline-flex|grid|inline-grid|table)/.test(cs.getPropertyValue("display"));
  if ((st || sl) && !itemShift && !isSvg) {
    host = doc.createElement("div");
    host.setAttribute("style", `transform:translate(${-sl}px,${-st}px)`);
    el.appendChild(host);
  }
  for (const child of src.childNodes) {
    let c = cloneStyled(child, win, ctx);
    if (!c) continue;
    if (itemShift && !isSvg) {
      if (c.nodeType === 3) {
        if (!String(c.nodeValue || "").trim()) continue;
        const span = doc.createElement("span");
        span.appendChild(c);
        c = span;
      }
      const base = c.getAttribute?.("style") || "";
      const tf = /(?:^|;)transform:([^;]*)/.exec(base)?.[1]?.trim();
      const own = tf && tf !== "none" ? ` ${tf}` : "";
      c.setAttribute?.("style", `${base.replace(/(?:^|;)transform:[^;]*/, "")};transform:translate(${-sl}px,${-st}px)${own};`);
    }
    host.appendChild(c);
  }
  return el;
}

/**
 * Standalone markup of one element (an inline `<svg>` for the PNG paint fallback, `nodemap_png.js`): a
 * styled, taint-free copy (`cloneStyled()`), or "" when it cannot be made.
 */
export function snapshotElementMarkup(src, win) {
  try {
    if (typeof XMLSerializer === "undefined" || !win?.getComputedStyle) return "";
    const clone = cloneStyled(src, win, { n: FO_MAX_ELEMENTS, ids: svgIdsOf(src), root: src, inlining: 0 });
    if (!clone) return "";
    const markup = new XMLSerializer().serializeToString(clone).replace(XML_ILLEGAL, "");
    return isWellFormedXml(markup) ? markup : "";
  } catch {
    return "";
  }
}

/** True when `markup` (an XHTML fragment) parses as XML (no `DOMParser`: assumed so). */
export function isWellFormedXml(markup) {
  if (typeof DOMParser === "undefined") return true;
  try {
    const doc = new DOMParser().parseFromString(`<root xmlns="http://www.w3.org/1999/xhtml">${markup}</root>`, "application/xml");
    return !doc.getElementsByTagName("parsererror").length;
  } catch {
    return false;
  }
}

/**
 * The body element and window a live DOM node (`tab`, `man`, `doc`) is captured from: `{ body, root, win }`,
 * or null when nothing is mounted or loaded (a tab iframe still loading or of another origin).
 */
export function liveNodeRoot(node, el) {
  const body = el?.querySelector?.(".nm-node-body");
  if (!body) return null;
  if (node.type === "man" || node.type === "doc") {
    // the live page (a beautified doc node keeps the Kernel Docs layout and styles)
    const root = body.querySelector(node.type === "man" ? ".nm-man" : ".nm-doc");
    return root ? { body, root, win: el.ownerDocument?.defaultView || globalThis, frame: null } : null;
  }
  if (node.type !== "tab") return null;
  const frame = body.querySelector("iframe");
  let doc = null;
  let win = null;
  try {
    doc = frame?.contentDocument;
    win = frame?.contentWindow;
  } catch {
    return null;
  }
  if (!doc || !win || doc.readyState === "loading") return null;
  const root = doc.querySelector("#workspace") || doc.body;
  return root ? { body, root, win, frame } : null;
}

/**
 * Taint-free XHTML snapshot of a standalone DOM element (used for compositing visible HUD overlays
 * when exporting tour frames with Hide HUD disabled): `{ html, w, h, bg }` or null.
 */
export function captureElementForeignObject(el) {
  try {
    if (!el || typeof XMLSerializer === "undefined") return null;
    const win = el.ownerDocument?.defaultView || globalThis;
    if (!win?.getComputedStyle) return null;
    const rect = el.getBoundingClientRect?.();
    const w = Math.ceil(el.offsetWidth || rect?.width || 0);
    const h = Math.ceil(el.offsetHeight || rect?.height || 0);
    if (!(w > 0 && h > 0)) return null;
    const ctx = { n: FO_MAX_ELEMENTS, ids: svgIdsOf(el), root: el, inlining: 0 };
    const clone = cloneStyled(el, win, ctx);
    if (!clone) return null;
    const base = clone.getAttribute("style") || "";
    clone.setAttribute("style", `${base}position:static;margin:0;left:auto;top:auto;right:auto;bottom:auto;transform:none;width:${w}px;height:${h}px;box-sizing:border-box;`);
    const html = new XMLSerializer().serializeToString(clone).replace(XML_ILLEGAL, "");
    if (!isWellFormedXml(html)) return null;
    return { html, w, h, bg: "" };
  } catch {
    return null;
  }
}

/**
 * Snapshot of a live DOM node's body (`tab` iframe document, `man` / `doc` page) as an XHTML fragment with
 * every computed style inlined: `{ html, w, h }` in content units, or null (nothing laid out, nothing
 * loaded, or markup that would not parse as XML). Nothing in it loads a resource: images and canvases are
 * inlined as `data:` URLs (one of another origin is left out as an empty box), other `url()`s are dropped,
 * SVG ids are renamed per snapshot (their `url(#id)` / `href` references follow) and `<use>` of a sprite
 * outside the snapshot is inlined. A supersampled tab iframe is read at 1×.
 */
export function captureNodeForeignObject(node, el) {
  try {
    if (typeof XMLSerializer === "undefined") return null;
    const live = liveNodeRoot(node, el);
    if (!live?.win?.getComputedStyle) return null;
    const { body, root, win } = live;
    const html0 = node.type === "tab" ? root.ownerDocument?.documentElement : null;
    const zoom0 = html0?.style?.zoom || "";
    if (zoom0) html0.style.zoom = "";
    const ctx = { n: FO_MAX_ELEMENTS, ids: svgIdsOf(root), root, inlining: 0 };
    let clone;
    try {
      clone = cloneStyled(root, win, ctx);
    } finally {
      if (zoom0) html0.style.zoom = zoom0;
    }
    const w = body.offsetWidth || root.offsetWidth || 0;
    const h = body.offsetHeight || root.offsetHeight || 0;
    // nothing laid out (a culled or LOD-hidden node outside `withExportLayout()`): keep the vector fallback
    if (!clone || !(w > 0 && h > 0)) return null;
    let bg = win.getComputedStyle(root).getPropertyValue("background-color");
    if (!bg || bg === "transparent" || /rgba\([^)]*,\s*0\)$/.test(bg)) bg = win.getComputedStyle(root.ownerDocument.body).getPropertyValue("background-color");
    const base = clone.getAttribute("style") || "";
    clone.setAttribute("style", `${base}margin:0;width:${w}px;height:${h}px;max-width:none;overflow:hidden;box-sizing:border-box;${bg ? `background-color:${bg};` : ""}`);
    const html = new XMLSerializer().serializeToString(clone).replace(XML_ILLEGAL, "");
    if (!isWellFormedXml(html)) return null;
    return { html, w, h, bg: bg || "" };
  } catch {
    return null;
  }
}
