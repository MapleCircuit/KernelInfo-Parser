/**
 * minimap_search.js - NodeMap canvas minimap overlay and node/content search bar.
 *
 * Pure layout/search helpers (`computeMinimapLayout`, `searchCanvasNodes`) are DOM-free and unit tested;
 * `NodeMapMinimap` and `NodeMapSearch` mount inside `controller.wrapEl` and delegate viewport panning,
 * selection, and centering back to the `NodeMapController`.
 */
import { accentVar, displayTitle } from "./canvas_model.js";
import { esc } from "./struct_view.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const DATA_SEARCH_FIELDS = [
  "name",
  "path",
  "symbol",
  "text",
  "cveId",
  "query",
  "catalog",
  "commitHash",
  "subject",
  "mid",
  "title",
];
const GENERIC_TEXT_KEYS = [
  "title",
  "summary",
  "versionSummary",
  "description",
  "text",
  "body",
  "brief",
  "markdown",
  "content",
  "message",
  "subject",
  "introducedSubject",
  "fixedSubject",
  "signature",
  "subtitle",
  "doc",
  "help",
  "cveId",
  "cve_id",
  "id",
  "name",
  "symbol",
  "author",
  "heading",
  "vname",
  "status",
  "commit",
  "commitHash",
  "hash",
  "path",
];

const r1 = (v) => (Number.isFinite(v) ? Math.round(v * 10) / 10 : 0);

function makeSvg(tag) {
  return typeof document.createElementNS === "function"
    ? document.createElementNS(SVG_NS, tag)
    : document.createElement(tag);
}

function makeSnippet(text, q, maxLen = 80) {
  const raw = String(text ?? "");
  const lines = raw.split(/\r?\n/);
  const matchLine = lines.find((l) => l.toLowerCase().includes(q)) ?? raw;
  const trimmed = matchLine.trim().replace(/\s+/g, " ");
  if (trimmed.length <= maxLen) return trimmed;
  const idx = trimmed.toLowerCase().indexOf(q);
  if (idx < 0) return `${trimmed.slice(0, maxLen - 1)}…`;
  const lead = Math.max(0, idx - Math.floor((maxLen - q.length) / 3));
  const tail = Math.min(trimmed.length, lead + maxLen);
  const prefix = lead > 0 ? "…" : "";
  const suffix = tail < trimmed.length ? "…" : "";
  return `${prefix}${trimmed.slice(lead, tail)}${suffix}`;
}

/**
 * Compute world bounds, uniform scale, mapped node rectangles, viewport indicator rectangle,
 * and `toCanvas(mx, my)` coordinate mapper for the minimap.
 */
export function computeMinimapLayout(
  nodes,
  viewport,
  wrapWidth,
  wrapHeight,
  mapW = 180,
  mapH = 120,
  pad = 40,
) {
  const mw = Number(mapW) > 0 ? Number(mapW) : 180;
  const mh = Number(mapH) > 0 ? Number(mapH) : 120;
  const p = Number.isFinite(Number(pad)) ? Number(pad) : 40;
  const z = Number(viewport?.zoom) > 0 ? Number(viewport.zoom) : 1;
  const vx = -(Number(viewport?.x) || 0) / z;
  const vy = -(Number(viewport?.y) || 0) / z;
  const hasVp = Number(wrapWidth) > 0 && Number(wrapHeight) > 0;
  const vw = hasVp ? Number(wrapWidth) / z : 0;
  const vh = hasVp ? Number(wrapHeight) / z : 0;

  const list = Array.isArray(nodes) ? nodes : [];
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  for (const n of list) {
    if (!n) continue;
    const nx = Number(n.x) || 0;
    const ny = Number(n.y) || 0;
    const nw = Math.max(0, Number(n.w) || 0);
    const nh = Math.max(0, Number(n.h) || 0);
    minX = Math.min(minX, nx);
    minY = Math.min(minY, ny);
    maxX = Math.max(maxX, nx + nw);
    maxY = Math.max(maxY, ny + nh);
  }

  if (hasVp) {
    minX = Math.min(minX, vx);
    minY = Math.min(minY, vy);
    maxX = Math.max(maxX, vx + vw);
    maxY = Math.max(maxY, vy + vh);
  }

  if (!Number.isFinite(minX) || !Number.isFinite(minY)) {
    minX = vx;
    minY = vy;
    maxX = vx + 800;
    maxY = vy + 600;
  }

  minX -= p;
  minY -= p;
  maxX += p;
  maxY += p;

  const worldW = Math.max(1, maxX - minX);
  const worldH = Math.max(1, maxY - minY);
  const worldBounds = { x: minX, y: minY, w: worldW, h: worldH };

  const scale = Math.min(
    Math.max(1, mw - 8) / worldW,
    Math.max(1, mh - 8) / worldH,
  );
  const offX = (mw - worldW * scale) / 2;
  const offY = (mh - worldH * scale) / 2;

  const toMapX = (wx) => offX + (wx - minX) * scale;
  const toMapY = (wy) => offY + (wy - minY) * scale;

  const nodeRects = list.filter(Boolean).map((n) => ({
    id: n.id,
    type: n.type,
    isGroup: n.type === "group",
    x: toMapX(Number(n.x) || 0),
    y: toMapY(Number(n.y) || 0),
    w: Math.max(0, Number(n.w) || 0) * scale,
    h: Math.max(0, Number(n.h) || 0) * scale,
    color: accentVar(n.color, n.type === "note" ? "var(--accent-yellow)" : "var(--accent-blue)"),
  }));

  const viewRect = {
    x: toMapX(vx),
    y: toMapY(vy),
    w: Math.max(0, vw * scale),
    h: Math.max(0, vh * scale),
  };

  return {
    mapW: mw,
    mapH: mh,
    scale,
    worldBounds,
    nodeRects,
    viewRect,
    toCanvas(mx, my) {
      return {
        x: minX + ((Number(mx) || 0) - offX) / scale,
        y: minY + ((Number(my) || 0) - offY) / scale,
      };
    },
  };
}

function searchStructItems(items, q) {
  for (const it of items || []) {
    if (!it || typeof it !== "object") continue;
    if (it.kind === "directive") {
      const text = it.text || (it.expr ? `#${it.directive || "if"} ${it.expr}` : "");
      if (text && String(text).toLowerCase().includes(q)) {
        return { matchField: "macro", snippet: makeSnippet(text, q) };
      }
      if (it.expr && String(it.expr).toLowerCase().includes(q)) {
        return { matchField: "macro", snippet: makeSnippet(it.expr, q) };
      }
    } else {
      for (const val of [it.name, it.type, it.type_name]) {
        if (val && String(val).toLowerCase().includes(q)) {
          const label = [it.type || it.type_name, it.name].filter(Boolean).join(" ");
          return { matchField: "member", snippet: makeSnippet(label || val, q) };
        }
      }
      if (Array.isArray(it.children)) {
        const sub = searchStructItems(it.children, q);
        if (sub) return sub;
      }
    }
  }
  return null;
}

function searchGenericText(val, q, depth = 0, seen = new Set()) {
  if (val == null || depth > 4) return null;
  if (typeof val === "string") {
    if (val.toLowerCase().includes(q)) return makeSnippet(val, q);
    return null;
  }
  if (typeof val !== "object") return null;
  if (seen.has(val)) return null;
  seen.add(val);
  if (Array.isArray(val)) {
    for (const item of val) {
      const hit = searchGenericText(item, q, depth + 1, seen);
      if (hit) return hit;
    }
    return null;
  }
  for (const key of GENERIC_TEXT_KEYS) {
    if (val[key] != null) {
      const hit = searchGenericText(val[key], q, depth + 1, seen);
      if (hit) return hit;
    }
  }
  for (const [key, child] of Object.entries(val)) {
    if (GENERIC_TEXT_KEYS.includes(key)) continue;
    const hit = searchGenericText(child, q, depth + 1, seen);
    if (hit) return hit;
  }
  return null;
}

function matchNodeContent(n, content, q) {
  if (content == null) return null;
  if (typeof content === "string") {
    return content.toLowerCase().includes(q)
      ? { matchField: "content", snippet: makeSnippet(content, q) }
      : null;
  }
  if (typeof content !== "object") return null;

  const isKapi = n.type === "kapi" || (typeof content.catalog === "string" && Array.isArray(content.items));

  // Struct layouts: members, nested aggregates, and #if/#ifdef directives
  if (!isKapi && (n.type === "struct" || Array.isArray(content.items)) && Array.isArray(content.items)) {
    const itemHit = searchStructItems(content.items, q);
    if (itemHit) return itemHit;
    if (Array.isArray(content.outer_conds)) {
      for (const cond of content.outer_conds) {
        if (cond && String(cond).toLowerCase().includes(q)) {
          return { matchField: "macro", snippet: makeSnippet(cond, q) };
        }
      }
    }
  }

  // Source slice lines
  if (Array.isArray(content.lines)) {
    for (let i = 0; i < content.lines.length; i++) {
      const line = String(content.lines[i] ?? "");
      if (line.toLowerCase().includes(q)) {
        return { matchField: "source", snippet: makeSnippet(line, q) };
      }
    }
  }

  // Kconfig model (or raw detail/graph payload)
  if (n.type === "kconfig" || Array.isArray(content.groups)) {
    if (content.name && (`CONFIG_${content.name}`).toLowerCase().includes(q)) {
      return { matchField: "kconfig", snippet: `CONFIG_${content.name}` };
    }
    if (content.prompt && String(content.prompt).toLowerCase().includes(q)) {
      return { matchField: "kconfig", snippet: makeSnippet(content.prompt, q) };
    }
    for (const g of content.groups || []) {
      for (const sym of g?.names || []) {
        const s = String(sym);
        if (s.toLowerCase().includes(q) || (`config_${s}`).includes(q)) {
          return { matchField: "kconfig", snippet: g.label ? `${g.label}: ${s}` : s };
        }
      }
    }
    if (content.help && String(content.help).toLowerCase().includes(q)) {
      return { matchField: "kconfig", snippet: makeSnippet(content.help, q) };
    }
    if (content.detail && typeof content.detail === "object") {
      const d = content.detail;
      for (const field of [d.name, d.prompt, d.help]) {
        if (field && String(field).toLowerCase().includes(q)) {
          return { matchField: "kconfig", snippet: makeSnippet(field, q) };
        }
      }
      for (const rel of ["depends_on", "selects", "implies"]) {
        for (const item of Array.isArray(d[rel]) ? d[rel] : []) {
          const sym = typeof item === "string" ? item : item?.name || item?.symbol || "";
          if (sym && String(sym).toLowerCase().includes(q)) {
            return { matchField: "kconfig", snippet: String(sym) };
          }
        }
      }
    }
  }

  // Maintainer section model
  if (n.type === "maintainer" || Array.isArray(content.maintainers) || Array.isArray(content.reviewers)) {
    for (const list of [content.maintainers, content.reviewers, content.files, content.lists, content.trees, content.excludes]) {
      for (const entry of Array.isArray(list) ? list : []) {
        const txt = typeof entry === "string" ? entry : entry?.name || entry?.email || entry?.pattern || entry?.fname || "";
        if (txt && String(txt).toLowerCase().includes(q)) {
          return { matchField: "maintainer", snippet: makeSnippet(txt, q) };
        }
      }
    }
    if (content.title && String(content.title).toLowerCase().includes(q)) {
      return { matchField: "maintainer", snippet: makeSnippet(content.title, q) };
    }
  }

  // Diff payload
  if (n.type === "diff" || (Array.isArray(content.files) && content.files.some((f) => f && Array.isArray(f.hunks)))) {
    for (const f of content.files || []) {
      for (const p of [f?.path, f?.oldPath]) {
        if (p && String(p).toLowerCase().includes(q)) {
          return { matchField: "content", snippet: makeSnippet(p, q) };
        }
      }
      for (const h of f?.hunks || []) {
        if (h?.header && String(h.header).toLowerCase().includes(q)) {
          return { matchField: "content", snippet: makeSnippet(h.header, q) };
        }
        for (const line of h?.lines || []) {
          const text = Array.isArray(line) ? line[3] : line;
          if (text != null && String(text).toLowerCase().includes(q)) {
            return { matchField: "content", snippet: makeSnippet(text, q) };
          }
        }
      }
    }
  }

  // Mail thread model or raw payload
  if (n.type === "mail" || Array.isArray(content.messages) || (n.type !== "history" && Array.isArray(content.rows))) {
    for (const r of content.rows || content.messages || []) {
      for (const field of [r?.subject, r?.author, r?.from, r?.mid, r?.message_id]) {
        if (field && String(field).toLowerCase().includes(q)) {
          return { matchField: "content", snippet: makeSnippet(field, q) };
        }
      }
      for (const ex of Array.isArray(r?.excerpt) ? r.excerpt : r?.body ? [r.body] : []) {
        if (ex && String(ex).toLowerCase().includes(q)) {
          return { matchField: "content", snippet: makeSnippet(ex, q) };
        }
      }
    }
  }

  // CVE / KAPI / Doc / History / generic loaded content
  const generic = searchGenericText(content, q);
  if (generic) return { matchField: "content", snippet: generic };
  return null;
}

/**
 * Search canvas nodes (and recursively nested `group.data.subCanvas` trees) for `query`.
 * Returns at most one match `{ nodeId, title, type, matchField, snippet, subMapPath, trailTitles, breadcrumb }`
 * per matching node.
 */
export function searchCanvasNodes(nodes, query, contentOf = () => null, opts = {}) {
  const trimmed = String(query ?? "").trim();
  if (!trimmed) return [];
  const q = trimmed.toLowerCase();
  const recursive = opts.recursive !== false;
  const maxDepth = Number.isInteger(opts.maxDepth) ? opts.maxDepth : 8;
  const hits = [];

  const matchNode = (n, title) => {
    if (String(title).toLowerCase().includes(q)) {
      return { matchField: "title", snippet: title };
    }
    for (const meta of [n.type, n.version, n.layerId]) {
      if (meta && String(meta).toLowerCase().includes(q)) {
        return { matchField: "meta", snippet: String(meta) };
      }
    }
    for (const lk of Array.isArray(n.locks) ? n.locks : []) {
      for (const lv of [lk?.kind, lk?.lockName, lk?.note]) {
        if (lv && String(lv).toLowerCase().includes(q)) {
          return { matchField: "lock", snippet: makeSnippet([lk.kind, lk.lockName, lk.note].filter(Boolean).join(" "), q) };
        }
      }
    }
    const data = n.data && typeof n.data === "object" ? n.data : null;
    if (data) {
      for (const key of DATA_SEARCH_FIELDS) {
        const val = data[key];
        if (val != null && val !== "" && String(val).toLowerCase().includes(q)) {
          return { matchField: "data", snippet: makeSnippet(String(val), q) };
        }
      }
      if (data.fieldLocks && typeof data.fieldLocks === "object") {
        for (const [fk, fl] of Object.entries(data.fieldLocks)) {
          const text = [fk, fl?.kind, fl?.lockName].filter(Boolean).join(" ");
          if (text.toLowerCase().includes(q)) {
            return { matchField: "lock", snippet: makeSnippet(text, q) };
          }
        }
      }
      if (Array.isArray(data.constituents)) {
        for (const c of data.constituents) {
          const name = typeof c === "string" ? c : c?.name || "";
          if (name && String(name).toLowerCase().includes(q)) {
            return { matchField: "member", snippet: makeSnippet(name, q) };
          }
        }
      }
    }
    let content = null;
    try {
      content = typeof contentOf === "function" ? contentOf(n) : null;
    } catch {
      content = null;
    }
    return matchNodeContent(n, content, q);
  };

  const walk = (list, subMapPath, trailTitles, depth) => {
    for (const n of Array.isArray(list) ? list : []) {
      if (!n) continue;
      const title = displayTitle(n);
      const hit = matchNode(n, title);
      if (hit) {
        const path = [...subMapPath];
        const trail = [...trailTitles];
        hits.push({
          nodeId: n.id,
          title,
          type: n.type,
          ...hit,
          subMapPath: path,
          trailTitles: trail,
          breadcrumb: trail.length ? `${trail.join(" › ")} › ${title}` : title,
        });
      }
      if (recursive && depth < maxDepth && n.type === "group" && Array.isArray(n.data?.subCanvas?.nodes)) {
        walk(n.data.subCanvas.nodes, [...subMapPath, n.id], [...trailTitles, title], depth + 1);
      }
    }
  };

  walk(nodes, [], [], 0);
  return hits;
}

/**
 * Interactive SVG minimap mounted inside `controller.wrapEl`.
 */
export class NodeMapMinimap {
  constructor(controller, { mapW = 180, mapH = 120 } = {}) {
    this.controller = controller;
    this.mapW = Number(mapW) > 0 ? Number(mapW) : 180;
    this.mapH = Number(mapH) > 0 ? Number(mapH) : 120;
    this.visible = true;
    this.el = null;
    this.wrapEl = null;
    this.toggleBtn = null;
    this.svgEl = null;
    this.vpEl = null;
    this.layout = null;
    this.dragging = false;
    this._dragLayout = null;
    this._lastMoveEvent = null;
    this._lastUpEvent = null;
    this._lastPointerPan = null;
    this._unsubs = [];
  }

  mount(wrapEl) {
    this.dispose();
    if (!wrapEl) return null;
    this.wrapEl = wrapEl;

    const el = document.createElement("div");
    el.className = "nm-minimap";
    el.classList?.toggle?.("nm-minimap-collapsed", !this.visible);

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "nm-minimap-toggle";
    toggleBtn.title = "Toggle minimap";
    toggleBtn.setAttribute?.("aria-label", "Toggle minimap");
    toggleBtn.setAttribute?.("aria-expanded", String(this.visible));
    toggleBtn.textContent = "Map";

    const svgEl = makeSvg("svg");
    svgEl.setAttribute?.("class", "nm-minimap-svg");
    svgEl.classList?.add?.("nm-minimap-svg");
    svgEl.setAttribute?.("viewBox", `0 0 ${this.mapW} ${this.mapH}`);
    svgEl.setAttribute?.("width", String(this.mapW));
    svgEl.setAttribute?.("height", String(this.mapH));
    svgEl.setAttribute?.("aria-label", "Canvas minimap");
    svgEl.innerHTML = '<rect class="nm-minimap-vp" x="0" y="0" width="0" height="0" />';

    el.appendChild(toggleBtn);
    el.appendChild(svgEl);
    wrapEl.appendChild?.(el);

    this.el = el;
    this.toggleBtn = toggleBtn;
    this.svgEl = svgEl;
    this.vpEl = svgEl.querySelector?.(".nm-minimap-vp") || null;

    const listen = (target, type, fn) => {
      if (!target?.addEventListener) return;
      target.addEventListener(type, fn);
      this._unsubs.push(() => target.removeEventListener?.(type, fn));
    };

    listen(el, "pointerdown", (e) => e.stopPropagation?.());
    listen(el, "dblclick", (e) => e.stopPropagation?.());
    listen(toggleBtn, "click", (e) => {
      e.stopPropagation?.();
      this.toggle();
    });

    listen(svgEl, "pointerdown", (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      e.preventDefault?.();
      e.stopPropagation?.();
      const layout = this.layout || this._computeLayout();
      this._dragLayout = layout;
      this.dragging = true;
      this._panFromEvent(e, layout);
      this._lastPointerPan = { x: e.clientX, y: e.clientY, t: Date.now() };
    });

    const onMove = (e) => {
      if (!this.dragging || e === this._lastMoveEvent) return;
      this._lastMoveEvent = e;
      if (e.buttons === 0) {
        onUp(e);
        return;
      }
      e.preventDefault?.();
      this._panFromEvent(e, this._dragLayout || this.layout || this._computeLayout());
      this._lastPointerPan = { x: e.clientX, y: e.clientY, t: Date.now() };
    };

    const onUp = (e) => {
      if (!this.dragging || e === this._lastUpEvent) return;
      this._lastUpEvent = e;
      this.dragging = false;
      this._dragLayout = null;
      this._lastPointerPan = { x: e.clientX, y: e.clientY, t: Date.now() };
      this.render();
    };

    listen(svgEl, "pointermove", onMove);
    listen(svgEl, "pointerup", onUp);
    listen(svgEl, "pointercancel", onUp);
    if (typeof window !== "undefined") {
      listen(window, "pointermove", onMove);
      listen(window, "pointerup", onUp);
      listen(window, "pointercancel", onUp);
    }

    listen(svgEl, "click", (e) => {
      e.stopPropagation?.();
      const lp = this._lastPointerPan;
      if (lp && lp.x === e.clientX && lp.y === e.clientY && Date.now() - lp.t < 400) {
        this._lastPointerPan = null;
        return;
      }
      this._panFromEvent(e, this.layout || this._computeLayout());
    });

    this.render();
    return el;
  }

  _wrapSize() {
    const r = this.controller?.wrapEl?.getBoundingClientRect?.()
      || this.wrapEl?.getBoundingClientRect?.()
      || { width: 800, height: 600 };
    return {
      w: r.width > 0 ? r.width : 800,
      h: r.height > 0 ? r.height : 600,
    };
  }

  _visibleNodes() {
    const all = this.controller?.canvas?.nodes || [];
    return all.filter((n) => this.controller?.isNodeVisibleInLayer?.(n) ?? true);
  }

  _computeLayout() {
    const { w, h } = this._wrapSize();
    return computeMinimapLayout(
      this._visibleNodes(),
      this.controller?.canvas?.viewport,
      w,
      h,
      this.mapW,
      this.mapH,
    );
  }

  _panFromEvent(e, layout) {
    if (!this.controller?.canvas?.viewport || !layout) return;
    const rect = this.svgEl?.getBoundingClientRect?.() || { left: 0, top: 0, width: this.mapW, height: this.mapH };
    const rw = rect.width > 0 ? rect.width : this.mapW;
    const rh = rect.height > 0 ? rect.height : this.mapH;
    const mx = ((Number(e.clientX) || 0) - (rect.left || 0)) * (this.mapW / rw);
    const my = ((Number(e.clientY) || 0) - (rect.top || 0)) * (this.mapH / rh);
    const pt = layout.toCanvas(mx, my);
    const { w: wrapW, h: wrapH } = this._wrapSize();
    const vp = this.controller.canvas.viewport;
    const z = Number(vp.zoom) > 0 ? Number(vp.zoom) : 1;
    vp.x = Math.round(wrapW / 2 - pt.x * z);
    vp.y = Math.round(wrapH / 2 - pt.y * z);
    this.controller.applyTransform?.();
    this.controller.scheduleWires?.();
    this.controller.scheduleSave?.();
    this.render();
  }

  render() {
    if (!this.svgEl || !this.controller?.canvas) return;
    const layout = this._computeLayout();
    this.layout = layout;
    const selected = this.controller.selected;
    const ordered = [
      ...layout.nodeRects.filter((r) => r.isGroup),
      ...layout.nodeRects.filter((r) => !r.isGroup),
    ];

    const rectsHtml = ordered.map((r) => {
      const sel = selected?.has?.(r.id) ? " selected" : "";
      const grp = r.isGroup ? " is-group" : "";
      const rw = Math.max(2, r1(r.w));
      const rh = Math.max(2, r1(r.h));
      const fill = r.isGroup
        ? 'fill="none" stroke-dasharray="2 2"'
        : `fill="${esc(r.color)}" fill-opacity="0.35"`;
      return `<rect class="nm-minimap-node${grp}${sel}" data-node-id="${esc(r.id)}" x="${r1(r.x)}" y="${r1(r.y)}" width="${rw}" height="${rh}" rx="1.5" ${fill} stroke="${esc(r.color)}" stroke-width="1" />`;
    }).join("");

    const vr = layout.viewRect;
    const vpHtml = `<rect class="nm-minimap-vp" x="${r1(vr.x)}" y="${r1(vr.y)}" width="${Math.max(4, r1(vr.w))}" height="${Math.max(4, r1(vr.h))}" rx="1.5" fill="var(--accent-blue)" fill-opacity="0.08" stroke="var(--accent-blue)" stroke-width="1.5" />`;

    this.svgEl.innerHTML = `${rectsHtml}${vpHtml}`;
    this.vpEl = this.svgEl.querySelector?.(".nm-minimap-vp") || null;
  }

  toggle(force) {
    this.visible = force === undefined ? !this.visible : Boolean(force);
    this.el?.classList?.toggle?.("nm-minimap-collapsed", !this.visible);
    this.toggleBtn?.setAttribute?.("aria-expanded", String(this.visible));
    if (this.visible) this.render();
    return this.visible;
  }

  dispose() {
    for (const u of this._unsubs) u();
    this._unsubs = [];
    this.dragging = false;
    this._dragLayout = null;
    this._lastMoveEvent = null;
    this._lastUpEvent = null;
    this._lastPointerPan = null;
    this.layout = null;
    this.el?.remove?.();
    if (Array.isArray(this.wrapEl?.children)) {
      const idx = this.wrapEl.children.indexOf(this.el);
      if (idx >= 0) this.wrapEl.children.splice(idx, 1);
    }
    this.el = null;
    this.toggleBtn = null;
    this.svgEl = null;
    this.vpEl = null;
    this.wrapEl = null;
  }
}

/**
 * Canvas search bar overlay mounted inside `controller.wrapEl`.
 */
export class NodeMapSearch {
  constructor(controller) {
    this.controller = controller;
    this.el = null;
    this.wrapEl = null;
    this.inputEl = null;
    this.countEl = null;
    this.crumbEl = null;
    this.prevBtn = null;
    this.nextBtn = null;
    this.closeBtn = null;
    this.matches = [];
    this.index = -1;
    this._lastQuery = "";
    this._highlightedEls = new Set();
    this._unsubs = [];
  }

  mount(wrapEl) {
    this.dispose();
    if (!wrapEl) return null;
    this.wrapEl = wrapEl;

    const el = document.createElement("div");
    el.className = "nm-search-bar";
    el.hidden = true;
    el.setAttribute?.("role", "search");
    el.setAttribute?.("aria-label", "Search canvas");

    const inputEl = document.createElement("input");
    inputEl.type = "search";
    inputEl.className = "nm-search-input";
    inputEl.placeholder = "Search canvas (title, field, #ifdef, note, tag)…";
    inputEl.setAttribute?.("aria-label", "Search canvas");
    inputEl.value = "";

    const countEl = document.createElement("span");
    countEl.className = "nm-search-count";
    countEl.setAttribute?.("aria-live", "polite");
    countEl.textContent = "";

    const crumbEl = document.createElement("span");
    crumbEl.className = "nm-search-crumb";
    crumbEl.hidden = true;
    crumbEl.textContent = "";

    const prevBtn = document.createElement("button");
    prevBtn.type = "button";
    prevBtn.className = "nodemap-btn nm-search-prev";
    prevBtn.dataset.search = "prev";
    prevBtn.title = "Previous match (Shift+Enter)";
    prevBtn.setAttribute?.("aria-label", "Previous match");
    prevBtn.textContent = "↑";

    const nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "nodemap-btn nm-search-next";
    nextBtn.dataset.search = "next";
    nextBtn.title = "Next match (Enter)";
    nextBtn.setAttribute?.("aria-label", "Next match");
    nextBtn.textContent = "↓";

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "nodemap-btn nm-search-close";
    closeBtn.dataset.search = "close";
    closeBtn.title = "Close search (Escape)";
    closeBtn.setAttribute?.("aria-label", "Close search");
    closeBtn.textContent = "×";

    el.appendChild(inputEl);
    el.appendChild(countEl);
    el.appendChild(crumbEl);
    el.appendChild(prevBtn);
    el.appendChild(nextBtn);
    el.appendChild(closeBtn);
    wrapEl.appendChild?.(el);

    this.el = el;
    this.inputEl = inputEl;
    this.countEl = countEl;
    this.crumbEl = crumbEl;
    this.prevBtn = prevBtn;
    this.nextBtn = nextBtn;
    this.closeBtn = closeBtn;

    const listen = (target, type, fn) => {
      if (!target?.addEventListener) return;
      target.addEventListener(type, fn);
      this._unsubs.push(() => target.removeEventListener?.(type, fn));
    };

    listen(el, "pointerdown", (e) => e.stopPropagation?.());
    listen(el, "click", (e) => e.stopPropagation?.());
    listen(el, "dblclick", (e) => e.stopPropagation?.());

    listen(inputEl, "input", () => this.update());
    listen(inputEl, "keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault?.();
        e.stopPropagation?.();
        this.step(e.shiftKey ? -1 : 1);
      } else if (e.key === "Escape") {
        e.preventDefault?.();
        e.stopPropagation?.();
        this.close();
      }
    });

    listen(prevBtn, "click", (e) => {
      e.stopPropagation?.();
      this.step(-1);
    });
    listen(nextBtn, "click", (e) => {
      e.stopPropagation?.();
      this.step(1);
    });
    listen(closeBtn, "click", (e) => {
      e.stopPropagation?.();
      this.close();
    });

    return el;
  }

  isOpen() {
    return Boolean(this.el && !this.el.hidden);
  }

  open() {
    if (!this.el) return;
    this.el.hidden = false;
    this.inputEl?.focus?.();
    this.inputEl?.select?.();
    this.update();
  }

  close() {
    if (!this.el) return;
    this.el.hidden = true;
    this._clearHighlights();
    this.matches = [];
    this.index = -1;
    this._lastQuery = "";
    if (this.countEl) this.countEl.textContent = "";
    if (this.crumbEl) {
      this.crumbEl.textContent = "";
      this.crumbEl.hidden = true;
    }
    this.controller?.rootEl?.focus?.({ preventScroll: true });
  }

  _clearHighlights() {
    for (const nodeEl of this._highlightedEls) {
      nodeEl?.classList?.remove?.("nm-search-hit", "nm-search-current");
    }
    this._highlightedEls.clear();
  }

  _sameSubMapPath(hitPath) {
    const a = Array.isArray(hitPath) ? hitPath : [];
    const b = Array.isArray(this.controller?.subMapPath) ? this.controller.subMapPath : [];
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  _applyHighlights() {
    this._clearHighlights();
    const renderer = this.controller?.renderer;
    if (!renderer?.element) return;
    for (let i = 0; i < this.matches.length; i++) {
      const hit = this.matches[i];
      if (!this._sameSubMapPath(hit.subMapPath)) continue;
      const nodeEl = renderer.element(hit.nodeId);
      if (!nodeEl?.classList) continue;
      nodeEl.classList.add("nm-search-hit");
      if (i === this.index) nodeEl.classList.add("nm-search-current");
      this._highlightedEls.add(nodeEl);
    }
  }

  _updateCrumb(hit) {
    if (!this.crumbEl) return;
    const trail = hit?.trailTitles;
    if (Array.isArray(trail) && trail.length) {
      this.crumbEl.textContent = trail.join(" › ");
      this.crumbEl.title = hit.breadcrumb || trail.join(" › ");
      this.crumbEl.hidden = false;
    } else {
      this.crumbEl.textContent = "";
      this.crumbEl.hidden = true;
    }
  }

  update() {
    const query = this.inputEl?.value ?? "";
    const trimmed = String(query).trim();
    if (!trimmed) {
      this._lastQuery = "";
      this.matches = [];
      this.index = -1;
      if (this.countEl) this.countEl.textContent = "";
      this._updateCrumb(null);
      this._clearHighlights();
      return this.matches;
    }

    const sameQuery = trimmed === this._lastQuery;
    this._lastQuery = trimmed;
    const searchRoot = this.controller?.rootCanvas || this.controller?.canvas;
    this.matches = searchCanvasNodes(
      searchRoot?.nodes || [],
      query,
      (n) => this.controller?.rawContentOf?.(n) || this.controller?.modelOf?.(n),
    );

    if (!this.matches.length) {
      this.index = -1;
      if (this.countEl) this.countEl.textContent = "0 matches";
      this._updateCrumb(null);
      this._clearHighlights();
      return this.matches;
    }

    if (!sameQuery || this.index < 0 || this.index >= this.matches.length) {
      this.index = 0;
    }

    if (this.countEl) this.countEl.textContent = `${this.index + 1} / ${this.matches.length}`;
    this._updateCrumb(this.matches[this.index]);
    this._applyHighlights();
    return this.matches;
  }

  step(delta = 1) {
    if (!this.matches.length) return null;
    const len = this.matches.length;
    const d = Number.isFinite(Number(delta)) ? Math.trunc(Number(delta)) : 1;
    if (this.index < 0) {
      this.index = d >= 0 ? 0 : len - 1;
    } else {
      this.index = ((this.index + d) % len + len) % len;
    }
    if (this.countEl) this.countEl.textContent = `${this.index + 1} / ${len}`;
    const hit = this.matches[this.index];
    this._updateCrumb(hit);
    if (!this._sameSubMapPath(hit.subMapPath) && typeof this.controller?.navigateToSubMapPath === "function") {
      this.controller.navigateToSubMapPath(hit.subMapPath || []);
    }
    this._applyHighlights();
    this.controller?.selectOnly?.(hit.nodeId);
    this.controller?.centerOnNode?.(hit.nodeId);
    return hit;
  }

  dispose() {
    this._clearHighlights();
    for (const u of this._unsubs) u();
    this._unsubs = [];
    this.el?.remove?.();
    if (Array.isArray(this.wrapEl?.children)) {
      const idx = this.wrapEl.children.indexOf(this.el);
      if (idx >= 0) this.wrapEl.children.splice(idx, 1);
    }
    this.el = null;
    this.inputEl = null;
    this.countEl = null;
    this.crumbEl = null;
    this.prevBtn = null;
    this.nextBtn = null;
    this.closeBtn = null;
    this.wrapEl = null;
    this.matches = [];
    this.index = -1;
  }
}
