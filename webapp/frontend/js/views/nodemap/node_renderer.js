/**
 * node_renderer.js - Incremental DOM of NodeMap nodes (`webapp/API_FRONTEND.md` §5.3.2).
 *
 * One element per node, kept across updates: moving, resizing, recoloring or retitling a node patches
 * its element, and the body is rebuilt only when the node's content key changes, so a live tab iframe,
 * a man page or a scrolled source slice survives drags. Pointer handling is delegated to the controller
 * (one listener set on the canvas); elements only carry `data-*` hooks:
 *   .nm-node[data-node-id]   .nm-node-head (drag handle)   .nm-title (inline title edit)
 *   .nm-head-meta            source reference (`headMeta` hook) before the header tools
 *   .nm-rz[data-rz]          resize handles (n, s, e, w, ne, nw, se, sw)
 *   .nm-port[data-side]      side ports; struct rows add .nm-port-field[data-field]
 *   [data-act]               header and body buttons
 * Every non-group node has a content scale (`scale`): an auto-fitted node (`autoFit`) lays out at its
 * natural `baseW`×`baseH`, any other at `w/scale`×`h/scale` (`contentBox()`), and is drawn scaled by
 * `scale` (transform origin top left), so its on-screen box is `w`×`h` and content grows and shrinks
 * with it; field rows scale along (wires still meet them), side ports and resize handles are
 * counter-scaled (`--nm-scale`) so they keep their on-screen size.
 * `hideHeader` sets `.nm-head-hidden` (nodemap.css: dimmed header in Edit Mode, none in View Mode);
 * `bgColor` sets `--nm-node-bg` (`.nm-has-bg`).
 */
import { NODE_BADGES, accentVar, bgVar, contentBox, displayTitle, paintOrder } from "./canvas_model.js";
import { esc } from "./struct_view.js";

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

/** CSS box and transform of a node element: its content box, scaled by the content scale. */
export function nodeBox(node) {
  const box = contentBox(node);
  const r = (v) => Math.round(v * 100) / 100;
  return { width: r(box.w), height: r(box.h), transform: box.s !== 1 ? `scale(${box.s})` : "" };
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
  sync(nodes, selected, active = null) {
    const live = new Set(nodes.map((n) => n.id));
    for (const [id, el] of this.els) {
      if (!live.has(id)) {
        this.hooks.disposeBody?.(id);
        el.remove();
        this.els.delete(id);
        this.keys.delete(id);
      }
    }
    // Paint order inside each z tier (nodemap.css: groups < wires < nodes); elements already in place
    // are not moved, since moving an iframe reloads it
    let prev = null;
    for (const node of paintOrder(nodes)) {
      let el = this.els.get(node.id);
      if (!el) {
        el = this.create(node);
        this.els.set(node.id, el);
      }
      this.patch(el, node, selected.has(node.id), active === node.id);
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
      <div class="nm-node-body"></div>
      ${node.type === "group" ? "" : SIDE_PORTS.map((s) => `<span class="nm-port nm-port-side nm-port-${s}" data-side="${s}" title="Drag to connect"></span>`).join("")}
      ${HANDLES.map((h) => `<span class="nm-rz nm-rz-${h}" data-rz="${h}"></span>`).join("")}`;
    return el;
  }

  patch(el, node, isSelected, isActive) {
    this.place(el, node);
    el.classList.toggle("selected", isSelected);
    el.classList.toggle("nm-interacting", isActive);
    el.classList.toggle("nm-has-accent", Boolean(node.color));
    el.classList.toggle("nm-autofit", Boolean(node.autoFit));
    el.classList.toggle("nm-scaled", node.type !== "group" && Boolean(node.scale) && node.scale !== 1);
    el.style.setProperty("--nm-accent", accentVar(node.color, "var(--accent-blue)"));
    el.classList.toggle("nm-head-hidden", Boolean(node.hideHeader));
    el.classList.toggle("nm-has-bg", Boolean(node.bgColor));
    if (node.bgColor) el.style.setProperty("--nm-node-bg", bgVar(node.bgColor, "var(--bg-secondary)"));
    else el.style.removeProperty?.("--nm-node-bg");
    const meta = this.hooks.headMeta ? this.hooks.headMeta(node) : null;
    const metaEl = el.querySelector(".nm-head-meta");
    if (metaEl && metaEl.dataset.text !== (meta?.text || "")) {
      metaEl.dataset.text = meta?.text || "";
      // An isolated LTR run inside the RTL box: a long path loses its start, never the line numbers
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
    const key = contentKey(node, this.hooks.bodyKey ? this.hooks.bodyKey(node) : "");
    if (this.keys.get(node.id) !== key) {
      this.keys.set(node.id, key);
      const body = el.querySelector(".nm-node-body");
      this.hooks.disposeBody?.(node.id);
      this.hooks.renderBody(node, body);
      this.rebuilt.add(node.id);
    }
  }

  /** Position and size an element (live drags and resizes call this directly). */
  place(el, node) {
    const box = nodeBox(node);
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;
    el.style.width = `${box.width}px`;
    el.style.height = `${box.height}px`;
    el.style.transform = box.transform;
    // Handles and side ports are counter-scaled (nodemap.css) so they stay grabbable at any content scale
    el.style.setProperty("--nm-scale", String(node.type !== "group" && node.scale ? node.scale : 1));
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
