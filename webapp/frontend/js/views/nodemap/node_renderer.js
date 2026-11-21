/**
 * node_renderer.js - Incremental DOM of NodeMap nodes (`webapp/API_FRONTEND.md` §5.3.2).
 *
 * One element per node, kept across updates: moving, resizing, recoloring or retitling a node patches
 * its element, and the body is rebuilt only when the node's content key changes, so a live tab iframe,
 * a man page or a scrolled source slice survives drags. Pointer handling is delegated to the controller
 * (one listener set on the canvas); elements only carry `data-*` hooks:
 *   .nm-node[data-node-id]   .nm-node-head (drag handle)   .nm-title (inline title edit)
 *   .nm-rz[data-rz]          resize handles (n, s, e, w, ne, nw, se, sw)
 *   .nm-port[data-side]      side ports; struct rows add .nm-port-field[data-field]
 *   [data-act]               header and body buttons
 */
import { accentVar, displayTitle, paintOrder } from "./canvas_model.js";
import { esc } from "./struct_view.js";

const HANDLES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
const SIDE_PORTS = ["top", "right", "bottom", "left"];
const BADGES = { struct: "struct", symbol: "symbol", source: "source", man: "man", tab: "tab", note: "note", group: "group" };

/** What makes a node's body stale: its payload and revision, not its geometry, title or color. */
export function contentKey(node) {
  return JSON.stringify([node.type, node.version, node.repo, node.data]);
}

export class NodeRenderer {
  /**
   * @param {HTMLElement} layer  the zoomed canvas layer
   * @param {{renderBody(node, bodyEl), disposeBody?(nodeId), headTools?(node): string}} hooks
   */
  constructor(layer, hooks) {
    this.layer = layer;
    this.hooks = hooks;
    this.els = new Map();
    this.keys = new Map();
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
        <span class="nm-head-tools"></span>
      </div>
      <div class="nm-node-body"></div>
      ${node.type === "group" ? "" : SIDE_PORTS.map((s) => `<span class="nm-port nm-port-side nm-port-${s}" data-side="${s}" title="Drag to connect"></span>`).join("")}
      ${HANDLES.map((h) => `<span class="nm-rz nm-rz-${h}" data-rz="${h}"></span>`).join("")}`;
    return el;
  }

  patch(el, node, isSelected, isActive) {
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;
    el.style.width = `${node.w}px`;
    el.style.height = `${node.h}px`;
    el.classList.toggle("selected", isSelected);
    el.classList.toggle("nm-interacting", isActive);
    el.classList.toggle("nm-has-accent", Boolean(node.color));
    el.style.setProperty("--nm-accent", accentVar(node.color, "var(--accent-blue)"));
    const titleEl = el.querySelector(".nm-title");
    const title = displayTitle(node);
    if (titleEl && !titleEl.querySelector("input") && titleEl.textContent !== title) titleEl.textContent = title;
    const tools = this.hooks.headTools ? this.hooks.headTools(node, isActive) : "";
    const toolsEl = el.querySelector(".nm-head-tools");
    if (toolsEl && toolsEl.dataset.html !== tools) {
      toolsEl.innerHTML = tools;
      toolsEl.dataset.html = tools;
    }
    const key = contentKey(node);
    if (this.keys.get(node.id) !== key) {
      this.keys.set(node.id, key);
      const body = el.querySelector(".nm-node-body");
      this.hooks.disposeBody?.(node.id);
      this.hooks.renderBody(node, body);
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
