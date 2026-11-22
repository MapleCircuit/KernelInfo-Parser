/**
 * node_renderer.js - Incremental DOM of NodeMap nodes (`webapp/API_FRONTEND.md` §5.3.2).
 *
 * Uses GPU `translate3d(x, y, 0)` transforms for layout-free dragging, viewport LOD culling
 * (`content-visibility: hidden` off-screen and low-detail mode below 25% zoom), and visibility
 * layer filtering.
 */
import { NODE_BADGES, accentVar, bgVar, contentBox, displayTitle, formatLockBadge, isLayerVisible, paintOrder } from "./canvas_model.js";
import { esc } from "./struct_view.js";
import { isNodeInViewport, lodTierForViewport } from "./virtual_source.js";

const HANDLES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
const SIDE_PORTS = ["top", "right", "bottom", "left"];
const BADGES = NODE_BADGES;

/**
 * What makes a node's body stale: its payload and revision (plus `extra`, view state from outside the
 * node such as Edit Mode or a Kconfig engine), not its geometry, title or color.
 */
export function contentKey(node, extra = "") {
  return JSON.stringify([node.type, node.version, node.repo, node.data, extra]);
}

/** CSS box and GPU transform of a node element: positioned with `translate3d(x, y, 0)` + content scale. */
export function nodeBox(node) {
  const box = contentBox(node);
  const r = (v) => Math.round(v * 100) / 100;
  const tx = Math.round(node.x || 0);
  const ty = Math.round(node.y || 0);
  return {
    width: r(box.w),
    height: r(box.h),
    transform: box.s !== 1 ? `translate3d(${tx}px, ${ty}px, 0) scale(${box.s})` : `translate3d(${tx}px, ${ty}px, 0)`,
  };
}

export class NodeRenderer {
  /**
   * @param {HTMLElement} layer  the zoomed canvas layer
   * @param {{renderBody(node, bodyEl), disposeBody?(nodeId), headTools?(node): string,
   *          headMeta?(node): {text, title}, bodyKey?(node): string}} hooks
   */
  constructor(layer, hooks) {
    this.layer = layer;
    this.hooks = hooks;
    this.els = new Map();
    this.keys = new Map();
    this.rebuilt = new Set(); // ids whose body was rebuilt since the caller last took them
  }

  /** Ids of the nodes whose body was rebuilt since the last call (auto-fit measures them). */
  takeRebuilt() {
    const out = this.rebuilt;
    this.rebuilt = new Set();
    return out;
  }

  element(id) {
    return this.els.get(id) || null;
  }

  /** Bring the DOM in line with `nodes`; `selected` (Set) and `active` (interacting node id) set classes. */
  sync(nodes, selected, active = null, settings = null) {
    const live = new Set(nodes.map((n) => n.id));
    for (const [id, el] of this.els) {
      if (!live.has(id)) {
        this.hooks.disposeBody?.(id);
        el.remove();
        this.els.delete(id);
        this.keys.delete(id);
      }
    }
    let prev = null;
    for (const node of paintOrder(nodes)) {
      let el = this.els.get(node.id);
      if (!el) {
        el = this.create(node);
        this.els.set(node.id, el);
      }
      this.patch(el, node, selected.has(node.id), active === node.id, settings);
      if (!el.isConnected || (prev && el.previousElementSibling !== prev && !this.orderedAfter(prev, el))) {
        if (prev) prev.after(el);
        else this.layer.appendChild(el);
      }
      prev = el;
    }
  }

  /** True when `el` already comes after `prev` among the layer's children. */
  orderedAfter(prev, el) {
    return Boolean(prev.compareDocumentPosition(el) & 4);
  }

  create(node) {
    const el = document.createElement("div");
    el.className = `nm-node nm-type-${node.type}`;
    el.dataset.nodeId = node.id;
    el.dataset.nodeType = node.type;
    el.innerHTML = `
      <div class="nm-node-head">
        <span class="nm-badge">${esc(BADGES[node.type] || node.type)}</span>
        <span class="nm-title" title="Double-click to rename"></span>
        <span class="nm-head-meta"></span>
        <span class="nm-head-tools"></span>
      </div>
      <div class="nm-node-locks" hidden></div>
      <div class="nm-node-body"></div>
      ${node.type === "group" ? "" : SIDE_PORTS.map((s) => `<span class="nm-port nm-port-side nm-port-${s}" data-side="${s}" title="Drag to connect"></span>`).join("")}
      ${HANDLES.map((h) => `<span class="nm-rz nm-rz-${h}" data-rz="${h}"></span>`).join("")}`;
    return el;
  }

  patch(el, node, isSelected, isActive, settings = null) {
    this.place(el, node);
    el.classList.toggle("selected", isSelected);
    el.classList.toggle("nm-interacting", isActive);
    el.classList.toggle("nm-has-accent", Boolean(node.color));
    el.classList.toggle("nm-autofit", Boolean(node.autoFit));
    el.classList.toggle("nm-scaled", node.type !== "group" && Boolean(node.scale) && node.scale !== 1);
    el.classList.toggle("nm-submap-portal", node.type === "group" && Boolean(node.data?.subCanvas));
    el.classList.toggle("nm-layer-hidden", settings ? !isLayerVisible(settings, node.layerId) : false);
    el.style?.setProperty?.("--nm-accent", accentVar(node.color, "var(--accent-blue)"));
    el.classList.toggle("nm-head-hidden", Boolean(node.hideHeader));
    el.classList.toggle("nm-has-bg", Boolean(node.bgColor));
    if (node.bgColor) el.style?.setProperty?.("--nm-node-bg", bgVar(node.bgColor, "var(--bg-secondary)"));
    else el.style?.removeProperty?.("--nm-node-bg");
    const meta = this.hooks.headMeta ? this.hooks.headMeta(node) : null;
    const metaEl = el.querySelector(".nm-head-meta");
    if (metaEl && metaEl.dataset.text !== (meta?.text || "")) {
      metaEl.dataset.text = meta?.text || "";
      metaEl.innerHTML = meta?.text ? `<bdi>${esc(meta.text)}</bdi>` : "";
      metaEl.title = meta?.title || "";
      metaEl.hidden = !meta?.text;
    }
    const titleEl = el.querySelector(".nm-title");
    const title = displayTitle(node);
    if (titleEl && !titleEl.querySelector("input") && titleEl.textContent !== title) titleEl.textContent = title;
    const tools = this.hooks.headTools ? this.hooks.headTools(node, isActive) : "";
    const toolsEl = el.querySelector(".nm-head-tools");
    if (toolsEl && toolsEl.dataset.html !== tools) {
      toolsEl.innerHTML = tools;
      toolsEl.dataset.html = tools;
    }
    const locksEl = el.querySelector(".nm-node-locks");
    if (locksEl) {
      const locks = Array.isArray(node.locks) ? node.locks : [];
      const lockHtml = locks.map((l) =>
        `<span class="nm-lock-badge nm-lock-${esc(l.kind)}" title="${esc(l.note || `${l.kind}${l.lockName ? `: ${l.lockName}` : ""}`)}">${esc(formatLockBadge(l))}</span>`
      ).join("");
      if (locksEl.dataset.html !== lockHtml) {
        locksEl.innerHTML = lockHtml;
        locksEl.dataset.html = lockHtml;
      }
      locksEl.hidden = locks.length === 0;
    }
    const key = contentKey(node, this.hooks.bodyKey ? this.hooks.bodyKey(node) : "");
    if (this.keys.get(node.id) !== key) {
      this.keys.set(node.id, key);
      const body = el.querySelector(".nm-node-body");
      this.hooks.disposeBody?.(node.id);
      this.hooks.renderBody(node, body);
      this.rebuilt.add(node.id);
    }
  }

  /** Position and size an element via GPU `translate3d` (zero layout thrashing during drags). */
  place(el, node) {
    const box = nodeBox(node);
    el.style.left = "0px";
    el.style.top = "0px";
    el.style.width = `${box.width}px`;
    el.style.height = `${box.height}px`;
    el.style.transform = box.transform;
    el.style?.setProperty?.("--nm-scale", String(node.type !== "group" && node.scale ? node.scale : 1));
  }

  /** Update viewport LOD culling (`nm-culled` outside `visRect`, `nm-lod-low` below 25% zoom). */
  updateViewportCulling(nodes, visRect, zoom, activeId = null) {
    const low = lodTierForViewport(zoom) === "low";
    this.layer?.classList?.toggle?.("nm-lod-low", low);
    for (const n of nodes) {
      const el = this.els.get(n.id);
      if (!el) continue;
      // Keep active tab iframes and groups unculled so iframes/frames don't pop awkwardly
      const keep = n.id === activeId || n.type === "tab" || isNodeInViewport(n, visRect);
      el.classList?.toggle?.("nm-culled", !keep);
    }
  }

  /** Force a body rebuild on the next sync (content fetched again, toggles flipped). */
  invalidate(id) {
    this.keys.delete(id);
  }

  clear() {
    for (const id of this.els.keys()) this.hooks.disposeBody?.(id);
    this.els.clear();
    this.keys.clear();
  }
}
