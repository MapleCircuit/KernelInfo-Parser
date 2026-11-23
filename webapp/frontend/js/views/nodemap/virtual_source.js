/**
 * virtual_source.js - Virtualized viewport renderer for large source/diff nodes and viewport LOD culling
 * (`webapp/API_FRONTEND.md` §5.3).
 *
 * Keeps DOM node counts bounded (< 200 rows) even on 6,000+ line files by rendering only the visible
 * slice plus an overscan buffer inside a fixed-height spacer positioned via a 2D `translate` (no
 * `will-change` compositor layer, so the rows stay crisp at any zoom).
 */
import { AstOverlay } from "../code_view/ast_overlay.js";
import { esc } from "./struct_view.js";

export const VIRTUAL_ROW_H = 16.5;
export const VIRTUAL_OVERSCAN = 20;
export const VIRTUAL_THRESHOLD = 120;
export const LOD_LOW_ZOOM = 0.25;

const DIFF_CLASS = {
  "+": "nm-diff-add",
  "-": "nm-diff-del",
  " ": "nm-diff-ctx",
  hunk: "nm-diff-hunk",
  file: "nm-diff-file",
  note: "nm-diff-note",
};

/**
 * Visible row window `[startIdx, endIdx)` for `totalRows` inside a scroll viewport of `viewportHeight`
 * at `scrollTop`, padded by `overscan` rows above and below.
 */
export function computeVirtualRange(
  totalRows,
  scrollTop = 0,
  viewportHeight = 360,
  rowH = VIRTUAL_ROW_H,
  overscan = VIRTUAL_OVERSCAN,
) {
  const total = Math.max(0, Math.floor(Number(totalRows) || 0));
  const rh = rowH > 0 ? rowH : VIRTUAL_ROW_H;
  const vh = Math.max(rh, Number(viewportHeight) || 360);
  const st = Math.max(0, Number(scrollTop) || 0);
  const totalHeight = Math.round(total * rh * 10) / 10;
  if (total === 0) {
    return { startIdx: 0, endIdx: 0, offsetY: 0, totalHeight: 0, visibleCount: 0 };
  }
  const firstVisible = Math.floor(st / rh);
  const visibleCount = Math.ceil(vh / rh);
  const startIdx = Math.max(0, Math.min(total - 1, firstVisible - overscan));
  const endIdx = Math.min(total, firstVisible + visibleCount + overscan);
  const offsetY = Math.round(startIdx * rh * 10) / 10;
  return { startIdx, endIdx, offsetY, totalHeight, visibleCount };
}

/**
 * Build an `AstOverlay` instance pre-indexed for `lines` so virtualized jumps across a 6,000-line
 * file keep multi-line comment and docstring state intact.
 */
export function createSourceHighlighter(lines, path = "") {
  const ov = new AstOverlay();
  ov.setFileInfo(null, path || "", lines?.[0] || "");
  ov.indexMultiLineSpans(lines || []);
  return ov;
}

/** Render one source slice window `[range.startIdx, range.endIdx)` as HTML inside a fixed-height spacer. */
export function renderVirtualSourceWindow(lines, startLine, path, highlight = null, range = null, htmlRows = null, highlighter = null) {
  const arr = Array.isArray(lines) ? lines : [];
  const baseLine = Number(startLine) || 1;
  const r = range || computeVirtualRange(arr.length, 0, 360);
  const ov = htmlRows ? null : (highlighter || createSourceHighlighter(arr, path));
  let rowsHtml = "";
  for (let i = r.startIdx; i < r.endIdx; i++) {
    const no = baseLine + i;
    const mark = highlight && highlight.has(no) ? " nm-src-hl" : "";
    const codeHtml = htmlRows ? String(htmlRows[i] ?? "") : ov.renderLineHtml(i + 1, arr[i]);
    rowsHtml += `<div class="nm-src-row${mark}"><span class="nm-src-no">${no}</span><span class="nm-src-code">${codeHtml}</span></div>`;
  }
  return `<div class="nm-virt-spacer" style="height:${r.totalHeight}px"><div class="nm-virt-window" style="transform:translate(0,${r.offsetY}px)">${rowsHtml}</div></div>`;
}

/** Render one diff row window `[range.startIdx, range.endIdx)` inside a fixed-height spacer. */
export function renderVirtualDiffWindow(rows, range = null) {
  const arr = Array.isArray(rows) ? rows : [];
  if (!arr.length) return '<div class="nm-empty">No changes</div>';
  const r = range || computeVirtualRange(arr.length, 0, 360);
  let rowsHtml = "";
  for (let i = r.startIdx; i < r.endIdx; i++) {
    const row = arr[i];
    const cls = DIFF_CLASS[row.kind] || "nm-diff-ctx";
    if (row.kind === "file" || row.kind === "hunk" || row.kind === "note") {
      rowsHtml += `<div class="nm-diff-row ${cls}"><span class="nm-diff-code">${esc(row.text)}</span></div>`;
    } else {
      rowsHtml += `<div class="nm-diff-row ${cls}"><span class="nm-diff-no">${esc(row.old ?? "")}</span><span class="nm-diff-no">${esc(row.new ?? "")}</span><span class="nm-diff-sign">${row.kind === " " ? "" : esc(row.kind)}</span><span class="nm-diff-code">${esc(row.text)}</span></div>`;
    }
  }
  return `<div class="nm-virt-spacer" style="height:${r.totalHeight}px"><div class="nm-virt-window" style="transform:translate(0,${r.offsetY}px)">${rowsHtml}</div></div>`;
}

/**
 * Estimate natural `{w, h}` of a large source or diff slice mathematically from line count and max
 * line length so `autoFit` never lays out 6,000 DOM rows synchronously.
 */
export function estimateNaturalSourceSize(lines, startLine = 1, rowH = VIRTUAL_ROW_H, headH = 26) {
  const arr = Array.isArray(lines) ? lines : [];
  const count = Math.max(1, arr.length);
  let maxChars = 24;
  const step = arr.length > 600 ? Math.ceil(arr.length / 300) : 1;
  for (let i = 0; i < arr.length; i += step) {
    const len = String(arr[i] ?? "").replace(/\t/g, "        ").length;
    if (len > maxChars) maxChars = len;
  }
  const gutterChars = Math.max(5, String(startLine + count).length + 2);
  const estW = Math.round((gutterChars + Math.min(maxChars, 140) + 3) * 6.8 + 16);
  const estH = Math.round(headH + count * rowH + 6);
  return { w: Math.max(240, estW), h: Math.max(120, estH) };
}

/**
 * Mount interactive scroll virtualization onto `.nm-src` container. Returns `{ update, setHtmlRows, dispose }`.
 */
export function mountVirtualSource(scrollerEl, { lines, startLine, path, highlight = null, htmlRows = null } = {}) {
  const arr = Array.isArray(lines) ? lines : [];
  const highlighter = htmlRows ? null : createSourceHighlighter(arr, path);
  let preRendered = htmlRows || null;
  let lastKey = "";
  let raf = null;

  const paint = () => {
    raf = null;
    if (!scrollerEl?.isConnected && scrollerEl?.isConnected !== undefined) return;
    const vh = scrollerEl.clientHeight || 360;
    const range = computeVirtualRange(arr.length, scrollerEl.scrollTop || 0, vh);
    const key = `${range.startIdx}:${range.endIdx}:${preRendered ? "w" : "m"}`;
    if (key === lastKey) return;
    lastKey = key;
    scrollerEl.innerHTML = renderVirtualSourceWindow(arr, startLine, path, highlight, range, preRendered, highlighter);
  };

  const onScroll = () => {
    if (raf) return;
    if (typeof requestAnimationFrame === "function") raf = requestAnimationFrame(paint);
    else paint();
  };

  scrollerEl.addEventListener?.("scroll", onScroll, { passive: true });
  paint();

  return {
    update: paint,
    setHtmlRows(nextHtmlRows) {
      preRendered = Array.isArray(nextHtmlRows) ? nextHtmlRows : null;
      lastKey = "";
      paint();
    },
    dispose() {
      if (raf && typeof cancelAnimationFrame === "function") cancelAnimationFrame(raf);
      raf = null;
      scrollerEl.removeEventListener?.("scroll", onScroll);
    },
  };
}

/**
 * Mount interactive scroll virtualization onto `.nm-diff` container. Returns `{ update, dispose }`.
 */
export function mountVirtualDiff(scrollerEl, { rows } = {}) {
  const arr = Array.isArray(rows) ? rows : [];
  let lastKey = "";
  let raf = null;

  const paint = () => {
    raf = null;
    if (!scrollerEl?.isConnected && scrollerEl?.isConnected !== undefined) return;
    const vh = scrollerEl.clientHeight || 360;
    const range = computeVirtualRange(arr.length, scrollerEl.scrollTop || 0, vh);
    const key = `${range.startIdx}:${range.endIdx}`;
    if (key === lastKey) return;
    lastKey = key;
    scrollerEl.innerHTML = renderVirtualDiffWindow(arr, range);
  };

  const onScroll = () => {
    if (raf) return;
    if (typeof requestAnimationFrame === "function") raf = requestAnimationFrame(paint);
    else paint();
  };

  scrollerEl.addEventListener?.("scroll", onScroll, { passive: true });
  paint();

  return {
    update: paint,
    dispose() {
      if (raf && typeof cancelAnimationFrame === "function") cancelAnimationFrame(raf);
      raf = null;
      scrollerEl.removeEventListener?.("scroll", onScroll);
    },
  };
}

// ------------------------------------------------------------------ Viewport LOD culling

/** Visible canvas bounds in canvas units, padded by `margin` for smooth pre-rendering. */
export function visibleCanvasRect(viewport, wrapWidth, wrapHeight, margin = 240) {
  const z = viewport?.zoom > 0 ? viewport.zoom : 1;
  const vx = -(viewport?.x || 0) / z;
  const vy = -(viewport?.y || 0) / z;
  const vw = (wrapWidth || 1200) / z;
  const vh = (wrapHeight || 800) / z;
  const m = margin / z;
  return { x: vx - m, y: vy - m, w: vw + m * 2, h: vh + m * 2 };
}

/** Whether `node` intersects the expanded viewport `visRect`. */
export function isNodeInViewport(node, visRect) {
  if (!node || !visRect) return true;
  return (
    node.x < visRect.x + visRect.w &&
    visRect.x < node.x + node.w &&
    node.y < visRect.y + visRect.h &&
    visRect.y < node.y + node.h
  );
}

/** Level-of-detail tier (`"low"` below 25% zoom, else `"full"`). */
export function lodTierForViewport(zoom) {
  return Number(zoom) > 0 && Number(zoom) < LOD_LOW_ZOOM ? "low" : "full";
}
