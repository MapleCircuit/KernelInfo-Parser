/**
 * nested_snapshot.js - High-quality pictures of nested NodeMaps (`webapp/API_FRONTEND.md` §5.3.9).
 *
 * A `nodemap` node with `data.highQualityPreview` shows a static picture of its child canvas instead of the
 * schematic rectangles. The picture is the child rendered once by the export renderer (`canvasToSvg()`,
 * `nodemap_export.js`) into a standalone SVG string; the live card shows it as an `<img>` (no child DOM) and
 * the SVG / PNG / tour-frame exports embed the same string as a nested `<svg>` with scoped ids.
 *
 * Snapshots are memory-only (never part of node data, JSON exports or server payloads), keyed by
 * `(canvasId, contentHash, theme, variant)`, coalesced while in flight and evicted least-recently-used under
 * an entry and a byte budget (pictures no card shows first; object URLs revoked on eviction). DOM-free apart from the optional object URL.
 */
import * as M from "./canvas_model.js";

/** Renderer variant of a snapshot (part of the key: a renderer change invalidates every picture). */
export const SNAPSHOT_VARIANT = "svg1";
export const SNAPSHOT_MAX_ENTRIES = 64;
export const SNAPSHOT_MAX_BYTES = 32 * 1024 * 1024;
/** Id prefix the child render uses; each embedding replaces it with an id prefix of its own. */
export const SNAPSHOT_ID_TOKEN = "nmsnapq7-";
const PREFIX_RE = /^[A-Za-z_][\w-]{0,63}$/;
const THEME_TOKENS = ["--bg-canvas", "--bg-secondary", "--bg-tertiary", "--text-primary", "--text-secondary", "--border-color", "--accent-blue", "--accent-yellow", "--syn-keyword"];

/** 53-bit string hash (cyrb53) as hex. */
export function hashString(str) {
  const s = String(str ?? "");
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let i = 0; i < s.length; i++) {
    const ch = s.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(16);
}

/** Serialized child canvas without its viewport (panning the child does not change its picture). */
function snapshotDoc(canvas) {
  const doc = M.serializeCanvas(canvas);
  delete doc.viewport;
  return doc;
}

/** Revision of a child canvas: the hash of its serialized content (viewport excluded) and of `extra`. */
export function contentHash(canvas, extra = "") {
  return hashString(`${JSON.stringify(snapshotDoc(canvas))}\u0000${extra}`);
}

/**
 * Theme part of a key: the theme name plus a fingerprint of resolved design tokens (`auto` follows the
 * system) and of `extra` (the font stacks the text is measured with).
 */
export function themeKey(resolve = (c) => c, name = "", extra = "") {
  let tokens = "";
  try {
    tokens = THEME_TOKENS.map((t) => String(resolve(`var(${t})`) ?? "")).join("|");
  } catch {
    tokens = "";
  }
  return `${String(name || "")}:${hashString(`${tokens}\u0000${extra}`)}`;
}

export function snapshotKey({ canvasId, hash, theme = "", variant = SNAPSHOT_VARIANT }) {
  return JSON.stringify([String(canvasId || ""), String(hash || ""), String(theme || ""), String(variant || "")]);
}

const slotOf = (canvasId, theme, variant) => JSON.stringify([String(canvasId || ""), String(theme || ""), String(variant || "")]);

/** `{width, height, viewBox: {x, y, w, h}}` of a standalone SVG string, `null` when it is not one. */
export function parseSnapshotSvg(svg) {
  const open = /^<svg\b([^>]*)>/.exec(String(svg || ""));
  if (!open || !String(svg).trimEnd().endsWith("</svg>")) return null;
  const attr = (name) => new RegExp(`\\b${name}="([^"]*)"`).exec(open[1])?.[1];
  const vb = String(attr("viewBox") || "").trim().split(/[\s,]+/).map(Number);
  const width = Number(attr("width"));
  const height = Number(attr("height"));
  if (vb.length !== 4 || !vb.every(Number.isFinite) || !(vb[2] > 0 && vb[3] > 0)) return null;
  return { width: width > 0 ? width : vb[2], height: height > 0 ? height : vb[3], viewBox: { x: vb[0], y: vb[1], w: vb[2], h: vb[3] } };
}

/** `data:` URL of an SVG string (an `<img>` source that never needs releasing). */
export function snapshotDataUrl(svg) {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(String(svg || ""))}`;
}

/** Image source helpers: object URLs when the platform has them (released on eviction), `data:` URLs otherwise. */
export function defaultSnapshotUrls() {
  const U = globalThis.URL;
  if (typeof U?.createObjectURL === "function" && typeof U?.revokeObjectURL === "function" && typeof globalThis.Blob === "function") {
    return {
      create: (svg) => U.createObjectURL(new globalThis.Blob([svg], { type: "image/svg+xml" })),
      revoke: (url) => { if (String(url).startsWith("blob:")) U.revokeObjectURL(url); },
    };
  }
  return { create: snapshotDataUrl, revoke: () => {} };
}

/** An `<img>` source this module produced (`blob:` or an SVG `data:` URL). */
export function isSnapshotUrl(url) {
  return /^blob:[\w:/.%[\]-]+$/i.test(String(url || "")) || String(url || "").startsWith("data:image/svg+xml;charset=utf-8,");
}

/**
 * The snapshot as a nested `<svg>` fitted (aspect preserved, centered) into the `{x, y, w, h}` box of the
 * parent document; its ids (clip paths, filters, patterns) get `prefix`, so they never collide with the
 * parent's or another embedding's. `""` for an empty or unusable snapshot.
 */
export function embedSnapshotSvg(entry, box, prefix) {
  if (!entry || entry.empty || !entry.svg || !entry.viewBox) return "";
  if (!(box?.w > 0 && box?.h > 0)) return "";
  const p = PREFIX_RE.test(String(prefix || "")) ? String(prefix) : "nmsnap-";
  // memoized per entry and prefix (a tour embeds the same picture under the same prefix in every frame);
  // only attribute values are rewritten (`id="…"`, `="url(#…)"`), never text content
  const memo = (entry._embedded ||= new Map());
  let inner = memo.get(p);
  if (inner === undefined) {
    const svg = entry.svg;
    inner = svg.slice(svg.indexOf(">") + 1, svg.lastIndexOf("</svg>"))
      .split(` id="${SNAPSHOT_ID_TOKEN}`).join(` id="${p}`)
      .split(`="url(#${SNAPSHOT_ID_TOKEN}`).join(`="url(#${p}`);
    if (memo.size >= 8) memo.clear();
    memo.set(p, inner);
  }
  const n = (v) => Math.round(Number(v) * 10) / 10;
  const vb = entry.viewBox;
  return `<svg x="${n(box.x)}" y="${n(box.y)}" width="${n(box.w)}" height="${n(box.h)}" viewBox="${vb.x} ${vb.y} ${vb.w} ${vb.h}" preserveAspectRatio="xMidYMid meet" overflow="hidden">${inner}</svg>`;
}

const defaultDefer = () => new Promise((resolve) => setTimeout(resolve, 0));

/**
 * Snapshot cache. `render(doc, {canvasId, theme, variant})` returns the standalone SVG of a serialized child
 * canvas (sync or async). Entries: `{key, canvasId, hash, theme, variant, svg, width, height, viewBox, url,
 * empty}`; an empty child is an `empty` entry without rendering. `onEvict(entry)` runs when the budget
 * evicts a picture a card shows (the card falls back to its schematic). A slot (child, theme, variant) whose
 * render failed or whose shown picture was evicted is `settled`: background requests skip it until an
 * explicit request of new content (`request`) or `invalidate`.
 */
export class NestedSnapshotCache {
  constructor({ render, maxEntries = SNAPSHOT_MAX_ENTRIES, maxBytes = SNAPSHOT_MAX_BYTES, urls = defaultSnapshotUrls(), defer = defaultDefer, onEvict = null } = {}) {
    this.render = typeof render === "function" ? render : () => "";
    this.onEvict = typeof onEvict === "function" ? onEvict : null;
    this.settledSlots = new Set(); // slots not re-requested in the background (failed / evicted)
    this.maxEntries = Math.max(1, maxEntries);
    this.maxBytes = Math.max(1, maxBytes);
    this.urls = urls;
    this.defer = defer;
    this.entries = new Map(); // key -> entry, least recently used first
    this.inflight = new Map(); // key -> Promise<entry|null>
    this.latestKeys = new Map(); // slot (canvasId, theme, variant) -> key shown by the cards
    this.wanted = new Map(); // slot -> key of the newest request
    this.failed = new Set(); // keys whose render failed (not retried until invalidated)
    this.bytes = 0;
    this.renders = 0;
    this.disposed = false;
  }

  get size() {
    return this.entries.size;
  }

  /** The cached entry of `key` (no LRU touch), or `null`. */
  peek(key) {
    return this.entries.get(key) || null;
  }

  /** The snapshot the cards of `canvasId` show in `theme` (the last one completed for its newest content). */
  latest(canvasId, theme = "", variant = SNAPSHOT_VARIANT) {
    const key = this.latestKeys.get(slotOf(canvasId, theme, variant));
    return (key && this.entries.get(key)) || null;
  }

  /** Whether background requests skip `canvasId` in `theme` (its render failed or its picture was evicted). */
  settled(canvasId, theme = "", variant = SNAPSHOT_VARIANT) {
    return this.settledSlots.has(slotOf(canvasId, theme, variant));
  }

  /** Whether a render of `canvasId` in `theme` is in flight. */
  pending(canvasId, theme = "", variant = SNAPSHOT_VARIANT) {
    const key = this.wanted.get(slotOf(canvasId, theme, variant));
    return Boolean(key && this.inflight.has(key));
  }

  /**
   * The snapshot of `canvas` (the child `canvasId`) in `theme`: a cached entry, the in-flight render of the
   * same key, or a new render (deferred so the canvas paints first). A different revision of the same child
   * replaces the picture the cards show (the stale one is released at once). `extra` is folded into the
   * content hash (what else the render depends on, e.g. which data payloads are loaded). Resolves `null` on
   * failure.
   */
  request({ canvasId, canvas, theme = "", variant = SNAPSHOT_VARIANT, extra = "" }) {
    if (this.disposed || !canvasId || !canvas || typeof canvas !== "object") return Promise.resolve(null);
    const doc = snapshotDoc(canvas);
    const hash = hashString(`${JSON.stringify(doc)}\u0000${extra}`);
    const key = snapshotKey({ canvasId, hash, theme, variant });
    const slot = slotOf(canvasId, theme, variant);
    this.wanted.set(slot, key);
    this.settledSlots.delete(slot);
    const hit = this.entries.get(key);
    if (hit) {
      this.entries.delete(key);
      this.entries.set(key, hit);
      this.latestKeys.set(slot, key);
      return Promise.resolve(hit);
    }
    const running = this.inflight.get(key);
    if (running) return running;
    const shown = this.latestKeys.get(slot);
    if (shown && shown !== key) this.drop(shown);
    if (this.failed.has(key)) {
      this.settledSlots.add(slot);
      return Promise.resolve(null);
    }
    const base = { key, canvasId: String(canvasId), hash, theme: String(theme || ""), variant: String(variant || "") };
    const job = (async () => {
      try {
        let entry;
        if (!(doc.nodes || []).length) {
          entry = { ...base, svg: "", width: 0, height: 0, viewBox: null, url: "", empty: true };
        } else {
          await this.defer();
          if (this.disposed) return null;
          this.renders += 1;
          const svg = String((await this.render(doc, { canvasId: base.canvasId, theme: base.theme, variant: base.variant })) || "");
          const geom = parseSnapshotSvg(svg);
          if (!geom || this.disposed) {
            if (!this.disposed) this.fail(key, slot);
            return null;
          }
          entry = { ...base, svg, ...geom, url: this.urls.create(svg), empty: false };
        }
        if (this.wanted.get(slot) !== key) {
          // superseded by a newer revision while rendering: handed to this caller only
          if (entry.url) this.urls.revoke(entry.url);
          return { ...entry, url: "" };
        }
        this.store(entry);
        this.latestKeys.set(slot, key);
        return entry;
      } catch {
        if (!this.disposed) this.fail(key, slot);
        return null;
      } finally {
        this.inflight.delete(key);
      }
    })();
    this.inflight.set(key, job);
    return job;
  }

  fail(key, slot) {
    this.failed.add(key);
    this.settledSlots.add(slot);
    if (this.failed.size > this.maxEntries * 4) this.failed.delete(this.failed.values().next().value);
  }

  store(entry) {
    this.entries.set(entry.key, entry);
    this.bytes += entry.svg.length;
    const over = () => this.entries.size > this.maxEntries || this.bytes > this.maxBytes;
    const shown = new Set(this.latestKeys.values());
    // least recently used first, pictures no card shows before shown ones
    for (const pass of [false, true]) {
      for (const key of [...this.entries.keys()]) {
        if (!over()) return;
        if (key !== entry.key && shown.has(key) === pass) this.drop(key, { evicted: true });
      }
    }
  }

  /** Remove one entry (its object URL revoked, every card pointer to it cleared). */
  drop(key, { evicted = false } = {}) {
    const entry = this.entries.get(key);
    if (!entry) return;
    this.entries.delete(key);
    this.bytes -= entry.svg.length;
    if (entry.url) this.urls.revoke(entry.url);
    let wasShown = false;
    for (const [slot, k] of [...this.latestKeys]) {
      if (k !== key) continue;
      this.latestKeys.delete(slot);
      wasShown = true;
      if (evicted) this.settledSlots.add(slot);
    }
    if (evicted && wasShown) this.onEvict?.(entry);
  }

  /** Forget every snapshot of `canvasId` (all themes and revisions), failures included. */
  invalidate(canvasId) {
    const id = String(canvasId || "");
    for (const [key, e] of [...this.entries]) if (e.canvasId === id) this.drop(key);
    for (const key of [...this.failed]) if (JSON.parse(key)[0] === id) this.failed.delete(key);
    for (const slot of [...this.settledSlots]) if (JSON.parse(slot)[0] === id) this.settledSlots.delete(slot);
  }

  /** Forget everything (object URLs revoked); `dispose` also refuses later requests. */
  clear({ dispose = false } = {}) {
    for (const key of [...this.entries.keys()]) this.drop(key);
    this.latestKeys.clear();
    this.wanted.clear();
    this.failed.clear();
    this.settledSlots.clear();
    if (dispose) this.disposed = true;
  }
}
