/**
 * nodemap_png.js - Live DOM nodes of a NodeMap PNG export (`webapp/API_FRONTEND.md` §5.3.6).
 *
 * Before the canvas is rasterized, every live node body (`tab` iframes, `man` pages, `doc` pages) gets its
 * own raster at the export's pixel density (`pixelScale × content scale`), embedded in the vector canvas
 * as a `data:image/png` `<image>` in place of the body (`canvasToSvg()` `rasters`), so paint order, clips
 * and borders stay the vector canvas's. One node failing never degrades another:
 *
 * 1. `prepareTabFrames()`: lazy tab iframes load now; each is awaited (load + content) within a bounded
 *    timeout, and supersampling (`applyFrameSupersample()`) is taken off while capturing (restored after).
 * 2. `rasterizeSnapshot()`: the node's taint-free DOM snapshot (`captureNodeForeignObject()`) in an SVG of
 *    its own (`<foreignObject>`, a `data:` URL), drawn on a canvas of the node's size; a browser that
 *    refuses it (load error, tainted canvas) only loses that node's snapshot.
 * 3. `collectPaintOps()` + `paintOpsToDataUrl()`: the fallback, a direct 2D-canvas paint of the live DOM
 *    (background boxes, borders, same-origin images, inline SVGs, text at its laid-out line boxes).
 * 4. Nothing readable (document inaccessible or empty): the vector placeholder card.
 */
import { contentBox, visibleCanvasSlice } from "./canvas_model.js";
import { captureNodeForeignObject, liveNodeRoot, snapshotElementMarkup } from "./nodemap_export.js";
import { evalCssColor, formatCssColor, parseCssColor } from "../../utils/css_color.js";

/** Node types whose body is live DOM (rasterized per node). */
export const LIVE_NODE_TYPES = ["tab", "man", "doc"];
/** Longest wait for the tab iframes of one export (load + content). */
export const FRAME_READY_TIMEOUT_MS = 5000;
const FRAME_POLL_MS = 50;
const FRAME_SETTLE_MAX_MS = 250;
const PAINT_MAX_ELEMENTS = 20000;
const PAINT_MAX_WORDS = 40000;
const MIN_NODE_SCALE = 0.5;
const SVG_NS = "http://www.w3.org/2000/svg";
const PAINT_SKIP_TAGS = new Set(["script", "noscript", "style", "link", "meta", "template", "iframe", "frame", "object", "embed", "audio", "video", "source", "track", "head", "title"]);
const PAINT_SKIP_SEL = ".nm-shield, .context-menu, .tooltip, .toast, .toast-container, [role='tooltip'], .loading-overlay, .spinner, .nm-tour-export-ui, [data-tour-export-ui]";

/** Browser primitives of the per-node rasterization (tests pass fakes). */
export function browserPngEnv() {
  return {
    loadImage(svg) {
      return new Promise((resolve, reject) => {
        const img = new Image();
        img.decoding = "sync";
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error("The browser could not load the snapshot image"));
        // a data: URL (a Blob URL of a `<foreignObject>` SVG taints the canvas in some browsers)
        img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
      });
    },
    createCanvas(w, h) {
      const c = document.createElement("canvas");
      c.width = w;
      c.height = h;
      return c;
    },
    toDataURL(c) {
      return c.toDataURL("image/png");
    },
  };
}

// ------------------------------------------------------------------ 1. tab iframes ready for capture

/**
 * Whether a tab iframe shows a loaded embed shell with content (`#workspace` or the body has children).
 * A frame whose document cannot be read counts as ready: there is nothing to wait for.
 */
export function frameReady(frame) {
  try {
    const win = frame.contentWindow;
    const doc = frame.contentDocument;
    if (!win) return false;
    if (!doc) return true;
    const href = String(win.location?.href || "");
    if (!href || href === "about:blank" || doc.readyState !== "complete") return false;
    const root = doc.querySelector?.("#workspace") || doc.body;
    return Boolean(root && root.childElementCount > 0);
  } catch {
    return true;
  }
}

function waitFrameReady(frame, deadline, pollMs) {
  return new Promise((resolve) => {
    if (frameReady(frame)) {
      resolve(true);
      return;
    }
    let done = false;
    let iv = null;
    let to = null;
    const finish = (ok) => {
      if (done) return;
      done = true;
      clearInterval(iv);
      clearTimeout(to);
      frame.removeEventListener?.("load", onLoad);
      resolve(ok);
    };
    const onLoad = () => {
      if (frameReady(frame)) finish(true);
    };
    frame.addEventListener?.("load", onLoad);
    iv = setInterval(() => {
      if (frameReady(frame)) finish(true);
    }, pollMs);
    to = setTimeout(() => finish(frameReady(frame)), Math.max(0, deadline - Date.now()));
  });
}

/** Two animation frames of the iframe's own window (its layout after a change), bounded by `maxMs`. */
function settleFrame(frame, maxMs) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      clearTimeout(t);
      resolve();
    };
    const t = setTimeout(finish, maxMs);
    try {
      const win = frame.contentWindow;
      const raf = win?.requestAnimationFrame;
      if (typeof raf !== "function") finish();
      else raf.call(win, () => raf.call(win, finish));
    } catch {
      finish();
    }
  });
}

/**
 * Get the tab iframes `frames` (`[{ id, frame }]`) ready for capture: a lazy frame loads now (`loading`
 * eager), each is awaited until it shows content or `timeoutMs` passes for the whole export, and a
 * supersampled frame is laid out at 1× (`setSupersample(frame, k)`) so its document measures at its box.
 * Returns `{ timedOut: [ids], restore() }`; `restore()` puts the supersampling back.
 */
export async function prepareTabFrames(frames, { timeoutMs = FRAME_READY_TIMEOUT_MS, setSupersample = null, pollMs = FRAME_POLL_MS } = {}) {
  const deadline = Date.now() + Math.max(0, timeoutMs);
  const restores = [];
  const timedOut = [];
  const restore = () => {
    for (const r of restores.splice(0)) {
      try {
        r();
      } catch {
        // the frame went away
      }
    }
  };
  try {
    await Promise.all((frames || []).map(async ({ id, frame }) => {
    if (!frame) return;
    if (frame.getAttribute?.("loading") === "lazy" || frame.loading === "lazy") {
      frame.setAttribute?.("loading", "eager");
      try {
        frame.loading = "eager";
      } catch {
        // attribute only
      }
    }
    const ok = await waitFrameReady(frame, deadline, pollMs);
    if (!ok) timedOut.push(id);
    const ss = Number(frame.dataset?.ss) || 1;
    if (ss > 1 && typeof setSupersample === "function") {
      setSupersample(frame, 1);
      restores.push(() => setSupersample(frame, ss));
    }
    if (ok) await settleFrame(frame, Math.max(0, Math.min(FRAME_SETTLE_MAX_MS, deadline - Date.now())));
    }));
  } catch (err) {
    restore();
    throw err;
  }
  return { timedOut, restore };
}

// ------------------------------------------------------------------ 2. one node's DOM snapshot

/** A PNG data URL a canvas really produced (`toDataURL()` answers `"data:,"` for a canvas it could not make). */
const isPngUrl = (url) => typeof url === "string" && url.startsWith("data:image/png;base64,") && url.length > 30;

/** Pixel size of `w × h` content units at density `k` (at least 1 px). */
const px = (v, k) => Math.max(1, Math.round(v * k));

/** Standalone SVG of a snapshot (`captureNodeForeignObject()`), sized `pw × ph` pixels. */
export function snapshotSvg(fo, pw, ph) {
  return `<svg xmlns="${SVG_NS}" width="${pw}" height="${ph}" viewBox="0 0 ${fo.w} ${fo.h}"><foreignObject x="0" y="0" width="${fo.w}" height="${fo.h}"><div xmlns="http://www.w3.org/1999/xhtml" style="width:${fo.w}px;height:${fo.h}px;overflow:hidden">${fo.html}</div></foreignObject></svg>`;
}

/**
 * `data:image/png` of snapshot `fo` at density `k` (halved while the browser cannot allocate the canvas,
 * down to 0.5): null when the browser refuses it (image error, tainted canvas, no canvas).
 */
export async function rasterizeSnapshot(fo, k, env = browserPngEnv()) {
  if (!fo?.html || !(fo.w > 0 && fo.h > 0)) return null;
  for (let kk = k; kk >= MIN_NODE_SCALE; kk /= 2) {
    const pw = px(fo.w, kk);
    const ph = px(fo.h, kk);
    let c = null;
    let ctx = null;
    try {
      c = env.createCanvas(pw, ph);
      ctx = c?.getContext?.("2d");
    } catch {
      ctx = null;
    }
    if (!ctx) continue;
    let url;
    try {
      const img = await env.loadImage(snapshotSvg(fo, pw, ph), pw, ph);
      if (fo.bg) {
        ctx.fillStyle = fo.bg;
        ctx.fillRect(0, 0, pw, ph);
      }
      ctx.drawImage(img, 0, 0, pw, ph);
      url = env.toDataURL(c);
    } catch {
      // refused (image error, tainted canvas): this node's snapshot is out
      return null;
    }
    if (isPngUrl(url)) return url;
    // a blank answer: the canvas was too large after all
  }
  return null;
}

// ------------------------------------------------------------------ 3. 2D paint fallback

/** A CSS color the 2D context can take (`var()` / `color-mix()` evaluated), or null when invisible. */
function paintColor(value, read) {
  let v = String(value || "").trim();
  if (!v || v === "transparent" || v === "none") return null;
  if (/var\(|color-mix\(/i.test(v)) {
    const c = evalCssColor(v, read);
    if (!c) return null;
    v = formatCssColor(c);
  }
  const c = parseCssColor(v);
  if (c && c.a === 0) return null;
  return v;
}

const intersect = (a, b) => {
  const x = Math.max(a.x, b.x);
  const y = Math.max(a.y, b.y);
  return { x, y, w: Math.max(0, Math.min(a.x + a.w, b.x + b.w) - x), h: Math.max(0, Math.min(a.y + a.h, b.y + b.h) - y) };
};
const overlaps = (r, c) => r.w > 0 && r.h > 0 && r.x < c.x + c.w && r.y < c.y + c.h && r.x + r.w > c.x && r.y + r.h > c.y;

/**
 * Paint operations of a live node body (`liveNodeRoot()`), read from its laid-out DOM: `{ w, h, bg, clips,
 * ops }` in content units, where each op (`rect`, `text`, `image`, `svg`) carries its clip (the overflow
 * boxes around it) and alpha. Text is placed at its own line boxes (a wrapped text node word by word).
 * Null when the body has no size.
 */
export function collectPaintOps(live, { maxElements = PAINT_MAX_ELEMENTS, maxWords = PAINT_MAX_WORDS } = {}) {
  const { body, root, win } = live || {};
  if (!body || !root || !win?.getComputedStyle) return null;
  const w = body.offsetWidth || 0;
  const h = body.offsetHeight || 0;
  if (!(w > 0 && h > 0)) return null;
  const doc = root.ownerDocument;
  // a tab iframe's viewport is the body; a man / doc page is read through the canvas zoom and node scale
  let ox = 0;
  let oy = 0;
  let f = 1;
  if (!live.frame) {
    const br = body.getBoundingClientRect();
    ox = br.left;
    oy = br.top;
    f = br.width > 0 ? br.width / w : 1;
  }
  const rel = (r) => ({ x: (r.left - ox) / f, y: (r.top - oy) / f, w: r.width / f, h: r.height / f });
  const clips = [{ x: 0, y: 0, w, h }];
  const ops = [];
  let budget = maxElements;
  let words = maxWords;
  const range = doc.createRange?.() || null;
  let bg = paintColor(win.getComputedStyle(root).getPropertyValue("background-color"));
  if (!bg && doc.body) bg = paintColor(win.getComputedStyle(doc.body).getPropertyValue("background-color"));

  const textOps = (tn, cs, clip, alpha) => {
    const text = String(tn.nodeValue || "");
    if (!text.trim() || !range) return;
    const fill = paintColor(cs.getPropertyValue("color"), (n) => cs.getPropertyValue(n));
    if (!fill) return;
    const font = `${cs.getPropertyValue("font-style") || "normal"} ${cs.getPropertyValue("font-weight") || "400"} ${cs.getPropertyValue("font-size") || "12px"} ${cs.getPropertyValue("font-family") || "sans-serif"}`;
    const pre = /^(pre|pre-wrap|break-spaces)$/.test(cs.getPropertyValue("white-space"));
    range.selectNodeContents(tn);
    const rects = range.getClientRects?.() || [];
    if (!rects.length) return;
    const push = (str, r) => {
      const b = rel(r);
      if (!overlaps(b, clips[clip]) || words-- <= 0) return;
      ops.push({ k: "text", text: str, x: b.x, y: b.y, w: b.w, h: b.h, font, fill, clip, alpha });
    };
    if (rects.length === 1) {
      push(pre ? text.replace(/\t/g, "    ").replace(/\n+$/, "") : text.replace(/\s+/g, " ").trim(), rects[0]);
      return;
    }
    for (const m of text.matchAll(/\S+/g)) {
      if (words <= 0) return;
      range.setStart(tn, m.index);
      range.setEnd(tn, m.index + m[0].length);
      const r = range.getClientRects?.()[0] || range.getBoundingClientRect?.();
      if (r) push(m[0], r);
    }
  };

  const walk = (el, clip, alpha) => {
    if (budget-- <= 0) return;
    const tag = String(el.localName || "").toLowerCase();
    if (PAINT_SKIP_TAGS.has(tag)) return;
    try {
      if (el.matches?.(PAINT_SKIP_SEL)) return;
    } catch {
      // selector unsupported
    }
    const cs = win.getComputedStyle(el);
    if (cs.getPropertyValue("display") === "none") return;
    const opacity = parseFloat(cs.getPropertyValue("opacity"));
    const a = alpha * (Number.isFinite(opacity) ? opacity : 1);
    if (a <= 0.01) return;
    const visible = !/^(hidden|collapse)$/.test(cs.getPropertyValue("visibility"));
    const read = (n) => cs.getPropertyValue(n);
    const r = rel(el.getBoundingClientRect());
    const isSvg = el.namespaceURI === SVG_NS;
    if (visible && overlaps(r, clips[clip])) {
      const fill = paintColor(cs.getPropertyValue("background-color"), read);
      if (fill) ops.push({ k: "rect", x: r.x, y: r.y, w: r.w, h: r.h, fill, radius: parseFloat(cs.getPropertyValue("border-top-left-radius")) || 0, clip, alpha: a });
      for (const side of ["top", "right", "bottom", "left"]) {
        const bw = parseFloat(cs.getPropertyValue(`border-${side}-width`)) || 0;
        const style = cs.getPropertyValue(`border-${side}-style`);
        const color = bw > 0 && style && style !== "none" && style !== "hidden" ? paintColor(cs.getPropertyValue(`border-${side}-color`), read) : null;
        if (!color) continue;
        const b = bw / f;
        const strip = side === "top" ? { x: r.x, y: r.y, w: r.w, h: b }
          : side === "bottom" ? { x: r.x, y: r.y + r.h - b, w: r.w, h: b }
            : side === "left" ? { x: r.x, y: r.y, w: b, h: r.h } : { x: r.x + r.w - b, y: r.y, w: b, h: r.h };
        ops.push({ k: "rect", ...strip, fill: color, radius: 0, clip, alpha: a });
      }
      if (tag === "img" || tag === "canvas") {
        ops.push({ k: "image", el, x: r.x, y: r.y, w: r.w, h: r.h, clip, alpha: a });
        return;
      }
      if (isSvg && tag === "svg") {
        const markup = snapshotElementMarkup(el, win);
        if (markup) ops.push({ k: "svg", markup, x: r.x, y: r.y, w: r.w, h: r.h, clip, alpha: a });
        return;
      }
    }
    if (isSvg) return;
    let inner = clip;
    const ovx = cs.getPropertyValue("overflow-x");
    const ovy = cs.getPropertyValue("overflow-y");
    if ((ovx && ovx !== "visible") || (ovy && ovy !== "visible")) {
      clips.push(intersect(clips[clip], r));
      inner = clips.length - 1;
    }
    for (const child of el.childNodes || []) {
      if (child.nodeType === 3) {
        if (visible) textOps(child, cs, inner, a);
      } else if (child.nodeType === 1) walk(child, inner, a);
    }
  };
  walk(root, 0, 1);
  return { w, h, bg, clips, ops };
}

/**
 * `data:image/png` of paint operations (`collectPaintOps()`) at density `k`. Images go first; a canvas
 * they taint is painted again without them. Null without a canvas.
 */
export async function paintOpsToDataUrl(paint, k, env = browserPngEnv()) {
  const draw = async (withImages) => {
    const pw = px(paint.w, k);
    const ph = px(paint.h, k);
    const c = env.createCanvas(pw, ph);
    const ctx = c?.getContext?.("2d");
    if (!ctx) return null;
    ctx.scale(k, k);
    if (paint.bg) {
      ctx.fillStyle = paint.bg;
      ctx.fillRect(0, 0, paint.w, paint.h);
    }
    let cur = -1;
    for (const op of paint.ops) {
      if (op.clip !== cur) {
        if (cur !== -1) ctx.restore();
        ctx.save();
        const cr = paint.clips[op.clip] || paint.clips[0];
        ctx.beginPath();
        ctx.rect(cr.x, cr.y, cr.w, cr.h);
        ctx.clip();
        cur = op.clip;
      }
      ctx.globalAlpha = op.alpha ?? 1;
      if (op.k === "rect") {
        ctx.fillStyle = op.fill;
        if (op.radius > 0 && typeof ctx.roundRect === "function") {
          ctx.beginPath();
          ctx.roundRect(op.x, op.y, op.w, op.h, Math.min(op.radius, op.w / 2, op.h / 2));
          ctx.fill();
        } else ctx.fillRect(op.x, op.y, op.w, op.h);
      } else if (op.k === "text") {
        ctx.font = op.font;
        ctx.fillStyle = op.fill;
        ctx.textBaseline = "middle";
        ctx.fillText(op.text, op.x, op.y + op.h / 2);
      } else if (op.k === "image" && withImages) {
        try {
          ctx.drawImage(op.el, op.x, op.y, op.w, op.h);
        } catch {
          // not decodable
        }
      } else if (op.k === "svg") {
        try {
          // vector: rasterized at the destination size (the inline SVG keeps explicit dimensions)
          const img = await env.loadImage(op.markup, px(op.w, k), px(op.h, k));
          ctx.drawImage(img, op.x, op.y, op.w, op.h);
        } catch {
          // left out
        }
      }
    }
    if (cur !== -1) ctx.restore();
    const url = env.toDataURL(c);
    return isPngUrl(url) ? url : null;
  };
  try {
    return await draw(true);
  } catch {
    // an image tainted the canvas: everything else is still readable
    try {
      return await draw(false);
    } catch {
      return null;
    }
  }
}

// ------------------------------------------------------------------ 4. every live node of an export

/**
 * Rasters of the live nodes of `ctrl`'s visible canvas at `pixelScale`: `{ rasters: Map(id -> { href, w,
 * h, via } | null), report: { dom, painted, placeholder } }` (node ids by how each was drawn). Each node is
 * captured inside `ctrl.withExportLayout()` (culled bodies materialized) and rasterized on its own.
 */
export async function liveNodeRasters(ctrl, pixelScale, env = browserPngEnv()) {
  const rasters = new Map();
  const report = { dom: [], painted: [], placeholder: [] };
  const canvas = ctrl.canvas ? visibleCanvasSlice(ctrl.canvas) : null;
  const layout = (fn) => (typeof ctrl.withExportLayout === "function" ? ctrl.withExportLayout(fn) : fn());
  for (const n of canvas?.nodes || []) {
    if (!LIVE_NODE_TYPES.includes(n.type)) continue;
    const el = ctrl.renderer?.element?.(n.id) || null;
    const k = (Number(pixelScale) > 0 ? Number(pixelScale) : 1) * (contentBox(n).s || 1);
    let res = null;
    try {
      const fo = layout(() => captureNodeForeignObject(n, el));
      const href = fo ? await rasterizeSnapshot(fo, k, env) : null;
      if (href) res = { href, w: fo.w, h: fo.h, via: "dom" };
    } catch {
      res = null;
    }
    if (!res) {
      try {
        const paint = layout(() => {
          const live = liveNodeRoot(n, el);
          return live ? collectPaintOps(live) : null;
        });
        if (paint?.ops.some((op) => op.k !== "rect")) {
          const href = await paintOpsToDataUrl(paint, k, env);
          if (href) res = { href, w: paint.w, h: paint.h, via: "paint" };
        }
      } catch {
        res = null;
      }
    }
    rasters.set(n.id, res);
    if (!res) report.placeholder.push(n.id);
    else if (res.via === "dom") report.dom.push(n.id);
    else report.painted.push(n.id);
  }
  return { rasters, report };
}
