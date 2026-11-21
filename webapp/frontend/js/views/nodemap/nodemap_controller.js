/**
 * nodemap_controller.js - NodeMap spatial canvas (`webapp/API_FRONTEND.md` §5.3).
 *
 * One controller per NodeMap tab. It owns the tab's canvas document (canvas_model.js), persisted in
 * localStorage under `kip-nodemap-canvas:<tab id>` and exported/imported as JSON, and drives:
 *   - pan (drag the background, middle button, Space+drag), zoom (wheel, Ctrl+wheel over node content),
 *     marquee selection (Select tool, or Shift/Ctrl+drag), Shift/Ctrl-click multi-select;
 *   - node drag (header; dragging a group moves its members, dropping a node in or out of a group frame
 *     changes its membership), resize from 8 handles with per-type limits, optional snap-to-grid;
 *   - Edit Mode (canvas setting, Inspector toggle): node editing controls (struct type / guard / hide
 *     toggles, inline title and note editing) show only in it; folds and header tools always work;
 *   - inline titles (double-click), notes, groups, accent colors and node / canvas backgrounds (presets
 *     or a custom color), hideable node headers, align / distribute, duplicate and copy / paste (Ctrl+D,
 *     Ctrl+C, Ctrl+V; a localStorage clipboard shared by every NodeMap tab), auto-fit nodes whose content
 *     scales with aspect-locked resizes;
 *   - undo / redo (Ctrl+Z, Ctrl+Y / Ctrl+Shift+Z, toolbar ↶ ↷) of every document change: a bounded
 *     snapshot history (`M.UndoStack`) recorded by `commit()`;
 *   - edges from side ports or struct field ports: anchor sides (or auto), label, arrows, line style, curve;
 *     ports show only when connected (`.nm-port-connected`), in Edit Mode, or while a wire is dragged;
 *   - image export (SVG / PNG dialog: transparent background, PNG upscale) with man pages and live tabs
 *     snapshotted into `<foreignObject>`s (`captureNodeForeignObject`);
 *   - content nodes: struct layouts (`list` / `table` style) with type toggles, `#if` guards and their
 *     scope bars, Kconfig-engine folding of compiled-out branches, hidden items
 *     (`/api/nodemap/{v}/struct/{name}`),
 *     source files / line ranges / symbol slices (`/api/nodemap/{v}/source`), man pages
 *     (`mountManDocument`), diffs, mailing-list threads (reply tree, Expand Thread to Nodes), Kconfig
 *     symbols (live value, relation chips that spawn or link nodes), MAINTAINERS sections
 *     (`domain_nodes.js`), and live KernelInfo tabs in an isolated `?embed=1` iframe;
 *   - NodeMap embedded sessions (`settings.embedSessions`): the tab iframes run in one of them
 *     (`&embedSession=`), and the canvas follows the Kconfig engines of one (`settings.followSession`)
 *     over its scoped channel, so editing Kconfig in an embedded tab refolds struct `#ifdef`s and
 *     updates Kconfig node values; Copy to Session hands a node as a tab to another workspace session;
 *   - tabs dragged from a tab strip onto the canvas are copied in as nodes (native, Shift: a live tab);
 *   - a content scale for every non-group node (Inspector), drawn into the image export too.
 * Element lookups stay inside the tab's own container (no document ids), so several NodeMap tabs and
 * panes never collide.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { sessionManager } from "../../session_manager.js";
import { urlSync } from "../../url_sync.js";
import { toast } from "../../components/toast.js";
import { contextMenu } from "../../components/context_menu.js";
import { escapeHtml, openModal } from "../../components/modal.js";
import { resolveColor } from "../../utils/theme.js";
import { onUiScaleChanged } from "../../utils/ui_scale.js";
import { isTabFeatureEnabled } from "../../utils/projects.js";
import { isAlwaysVersionlessTab } from "../../tab_routes.js";
import { embedSessionChannel } from "../../utils/embed_mode.js";
import { mayCarryTab, tabDragPayload } from "../../utils/tab_drag.js";
import { mountManDocument } from "../man/man_view.js";
import { DirectiveEvaluator, archMacros } from "../kconfig/directive_evaluator.js";
import * as M from "./canvas_model.js";
import { computeEdgeGeometry, dashArray } from "./edge_router.js";
import { NodeRenderer } from "./node_renderer.js";
import { findItem, guardAutoCollapsed, renderStructBody, structRows } from "./struct_view.js";
import { headMetaOf, renderNoteMarkdown, renderSourceRows } from "./node_content.js";
import {
  diffPayload, kconfigModel, kconfigValue, mailThreadLayout, mailThreadModel, maintainerModel, relationTargets,
  renderDiffBody, renderKconfigBody, renderMailBody, renderMaintainerBody,
} from "./domain_nodes.js";
import { nodeSpecsForTab, tabForNode } from "./nodemap_bridge.js";
import { PNG_DEFAULT_SCALE, PNG_SCALES, canvasToSvg, captureNodeForeignObject, pngSize, svgSize } from "./nodemap_export.js";

/** Tab types a KernelInfo tab node offers (feature-gated per project). */
export const EMBED_TAB_TYPES = [
  ["explorer", "Repository Explorer"], ["code", "Source Editor"], ["kconfig", "Kconfig"], ["commits", "Commits"],
  ["maintainers", "Maintainers"], ["mail", "Mailing List"], ["pahole", "Struct Layout"], ["callgraph", "Callgraph"],
  ["diff", "Compare"], ["treemap", "Treemap"], ["search", "Search"], ["api", "Kernel API"], ["insights", "Insights"],
  ["cve", "CVE Records"], ["man", "Man Pages"], ["watch", "Watch List"], ["bookmarks", "Bookmarks"], ["status", "Status"],
];

const ADD_MENU = [
  ["struct", "Struct / Union…", "🧱"], ["source", "Source File / Slice…", "📄"], ["diff", "Diff…", "±"],
  ["mail", "Mailing List Thread…", "✉"], ["kconfig", "Kconfig Symbol…", "⚙"], ["maintainer", "Maintainers…", "👥"],
  ["man", "Man Page…", "📘"], ["tab", "KernelInfo Tab…", "🗔"], ["symbol", "Symbol…", "🔣"], ["note", "Note Card", "📝"], ["group", "Group", "▭"],
];

const SAVE_DELAY_MS = 300;
const DRAG_THRESHOLD = 3;

const isTyping = (el) => Boolean(el && (el.closest?.("input, textarea, select, [contenteditable='true']")));
// Body buttons that edit a node: they work in Edit Mode only (folds and header tools always do)
const EDIT_ACTS = new Set(["toggle-type", "hide-item", "all-types", "guards", "toggle-header"]);
const PASTE_STEP = 24;
// A copy pushed to another session is offered to the window holding it first (components/tabs.js)
const PUSH_ACK_GRACE_MS = 400;
// Messages a thread spawns as nodes at once ("Expand Thread to Nodes")
const MAIL_EXPAND_MAX = 60;
const KC_EXPAND_MAX = 40;
const UNDO_LIMIT = 80;
const cssEsc = (s) => (globalThis.CSS?.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&"));
// Clipboard of this window when localStorage refuses it (shared by the NodeMap tabs of the window)
let memoryClipboard = null;

/** Whether the document holds a non-empty text selection inside `root` (copying it stays native). */
function hasTextSelection(root) {
  try {
    const sel = globalThis.getSelection?.();
    if (!sel || !sel.toString()) return false;
    // A selection elsewhere on the page (sidebar, another pane) does not keep the canvas from copying nodes
    if (!root?.contains || !sel.anchorNode) return true;
    return root.contains(sel.anchorNode) || root.contains(sel.focusNode);
  } catch {
    return false;
  }
}

export class NodeMapController {
  constructor() {
    this.canvas = null;
    this.selected = new Set();
    this.selectedEdge = null;
    this.active = null; // node whose embedded content takes the pointer (tab iframes)
    this.tool = "pan";
    this.content = new Map(); // fetch key -> {status, value, error}
    this.mounts = new Map(); // node id -> mounted body state
    this.gesture = null;
    this.saveTimer = null;
    this.unsubs = [];
    this.containerEl = null;
    this.evalCache = new Map(); // Kconfig engine tab id -> DirectiveEvaluator
    this.kconfigEpoch = 0;
    this.fitQueue = new Set(); // auto-fit nodes to measure on the next commit
    this.pasteStamp = "";
    this.pasteCount = 0;
    this.history = new M.UndoStack(UNDO_LIMIT); // undo / redo of the document (commit() records)
    this.connectedPorts = new Set(); // port elements tagged .nm-port-connected
    this.mailBodies = new Map(); // message id -> body text (expanded mail excerpts)
    this.embedEngines = new Map(); // Kconfig engines of the followed embedded session: tabId -> engine
    this.followChannel = null; // BroadcastChannel of the followed embedded session
    this.followKey = "";
    // Where the followed embedded session's channel comes from (tests swap it)
    this.channelFactory = (name) => (typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(name) : null);
    // Where Kconfig engines come from (tests swap it)
    this.kconfigSource = {
      list: () => (typeof state.kconfigEngineList === "function" ? state.kconfigEngineList() : []),
      context: (id) => (typeof state.definitionContext === "function" ? state.definitionContext(id) : null),
    };
  }

  // ------------------------------------------------------------------ lifecycle

  render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tab = tabData;
    this.currentVersion = tabData?.version || state.currentVersion;
    this.repo = tabData?.repo || state.activeRepo();
    this.storageKey = `${M.STORAGE_PREFIX}${tabData?.id || "default"}`;

    if (!this.canvas) {
      this.canvas = M.loadCanvas(globalThis.localStorage, this.storageKey) || M.emptyCanvas();
      this.history.reset(M.historySnapshot(this.canvas));
    }
    // Re-activation of the tab: the canvas DOM built before moves into the new container as it is (loaded
    // struct, source and man bodies, scroll positions and listeners kept; browsers reload moved iframes)
    if (this.rootEl) {
      containerEl.innerHTML = "";
      containerEl.appendChild(this.rootEl);
      this.subscribe();
      this.consumeIncoming(tabData);
      this.applyTransform();
      // Content that changed while the tab was hidden (folds, engines) could not be measured then
      for (const n of this.canvas.nodes) if (n.autoFit) this.fitQueue.add(n.id);
      this.commit({ save: false, record: true });
      return;
    }
    this.renderer?.clear();
    this.mounts.clear();

    containerEl.innerHTML = this.chromeHtml();
    const q = (s) => containerEl.querySelector(s);
    this.rootEl = q(".nodemap-container");
    this.wrapEl = q(".nodemap-canvas-wrapper");
    this.layerEl = q(".nodemap-overlay-layer");
    this.wireSvg = q(".nm-wire-layer");
    this.marqueeEl = q(".nm-marquee");
    this.inspectorEl = q(".nm-inspector-content");
    this.zoomLabel = q(".nm-zoom-label");
    this.hintEl = q(".nm-empty-hint");

    this.renderer = new NodeRenderer(this.layerEl, {
      renderBody: (node, body) => this.renderBody(node, body),
      disposeBody: (id) => this.disposeBody(id),
      headTools: (node, active) => this.headTools(node, active),
      headMeta: (node) => this.headMeta(node),
      bodyKey: (node) => this.bodyKey(node),
    });

    this.attachToolbar();
    this.attachCanvasEvents();
    this.attachTabDrop();
    this.subscribe();
    this.consumeIncoming(tabData);
    this.applyTransform();
    // Nodes handed over by another view are an undoable change of the canvas
    this.commit({ save: false, record: true });
  }

  dispose() {
    this.flushSave();
    for (const u of this.unsubs) u();
    this.unsubs = [];
    this.renderer?.clear();
    this.mounts.clear();
    if (this.onWinMove) window.removeEventListener("pointermove", this.onWinMove);
    if (this.onWinUp) {
      window.removeEventListener("pointerup", this.onWinUp);
      window.removeEventListener("pointercancel", this.onWinUp);
    }
    if (this.onMessage) window.removeEventListener("message", this.onMessage);
    this.onWinMove = this.onWinUp = this.onMessage = null;
    this.closeFollowChannel();
    this.connectedPorts = new Set();
    this.rootEl = null;
  }

  subscribe() {
    for (const u of this.unsubs) u();
    this.unsubs = [];
    const tabId = this.tab?.id;
    const offPending = state.subscribe("nodemap:pending", ({ tabId: id } = {}) => {
      if (id === tabId && this.containerEl?.isConnected) {
        this.consumeIncoming(state.findTab(tabId) || this.tab);
        this.commit();
      }
    });
    this.unsubs.push(typeof offPending === "function" ? offPending : () => {});
    const offScale = onUiScaleChanged(() => this.renderWires());
    this.unsubs.push(typeof offScale === "function" ? offScale : () => {});
    const onTheme = () => this.renderWires();
    window.addEventListener("kip:theme-changed", onTheme);
    this.unsubs.push(() => window.removeEventListener("kip:theme-changed", onTheme));
    // Kconfig engines opened, closed or re-resolved: struct nodes fold their branches again
    for (const ev of ["kconfig:engine:changed", "kconfig:assignments:updated"]) {
      const off = state.subscribe(ev, () => this.onKconfigChanged());
      this.unsubs.push(typeof off === "function" ? off : () => {});
    }
    this.syncFollowChannel();
  }

  onKconfigChanged() {
    this.kconfigEpoch += 1;
    this.evalCache.clear();
    // Folded or unfolded branches change the height of auto-fitted struct nodes
    for (const n of this.canvas?.nodes || []) if (n.autoFit && (n.type === "struct" || n.type === "kconfig")) this.fitQueue.add(n.id);
    clearTimeout(this.kconfigTimer);
    this.kconfigTimer = setTimeout(() => {
      if (this.containerEl?.isConnected) this.commit({ save: false });
    }, 120);
  }

  // ------------------------------------------------------------------ Kconfig engine

  /**
   * The Kconfig engine a struct node folds its branches by: none when folding is off for the canvas or
   * the node; the canvas's chosen engine, else the engine of the node's revision. Only engines with a
   * resolved configuration count.
   */
  kconfigEngineFor(node) {
    const st = this.canvas?.settings;
    if (!st?.kconfigCollapse || node?.type !== "struct" || node.data.disableKconfigEngine) return null;
    return this.pickEngine(node);
  }

  /**
   * Engines the canvas follows: with `followSession`, the ones running in that NodeMap embedded
   * session's tab iframes; the host workspace session's (this window and the sessions it sees) when
   * the host is followed or the embedded session runs none. Only engines with a resolved configuration
   * count.
   */
  followedEngines() {
    try {
      if (this.canvas?.settings.followSession) {
        const embedded = [...this.embedEngines.values()].filter((e) => e && e.resolved);
        if (embedded.length) return embedded;
      }
      return (this.kconfigSource.list() || []).filter((e) => e && e.resolved);
    } catch {
      return [];
    }
  }

  /** Engine context (`{engineTabId, assignments, arch, strict}`) of a followed engine. */
  engineContext(tabId) {
    const e = this.embedEngines.get(tabId);
    // An announced engine carries its whole resolved configuration: a symbol it leaves out is off
    if (e) return { engineTabId: tabId, assignments: e.assignments, arch: e.arch, strict: true };
    return this.kconfigSource.context(tabId);
  }

  /**
   * The followed engine a node reads: the canvas's chosen engine, else the engine of the node's revision;
   * an embedded session (followed explicitly) falls back to its only engine.
   */
  pickEngine(node) {
    const st = this.canvas?.settings;
    const list = this.followedEngines();
    if (st?.kconfigEngine) return list.find((e) => e.tabId === st.kconfigEngine) || null;
    const hit = list.find((e) => e.version && e.version === node.version);
    if (hit) return hit;
    // An embedded session's engine is followed by choice: its only engine serves every revision
    return list.length === 1 && list[0].isEmbedded ? list[0] : null;
  }

  /** Assignments a Kconfig node shows its value from (null: no engine followed for it). */
  kconfigValuesFor(node) {
    const eng = this.pickEngine(node);
    if (!eng) return null;
    try {
      return this.engineContext(eng.tabId)?.assignments || null;
    } catch {
      return null;
    }
  }

  /** `DirectiveEvaluator` of the node's engine (cached per engine until its values change). */
  evaluatorFor(node) {
    const eng = this.kconfigEngineFor(node);
    if (!eng) return null;
    if (this.evalCache.has(eng.tabId)) return this.evalCache.get(eng.tabId);
    let ev = null;
    try {
      const ctx = this.engineContext(eng.tabId);
      if (ctx && ctx.engineTabId === eng.tabId && ctx.assignments) {
        ev = new DirectiveEvaluator(ctx.assignments, { partial: !ctx.strict, macros: archMacros(ctx.arch, ctx.assignments) });
      }
    } catch {
      ev = null;
    }
    this.evalCache.set(eng.tabId, ev);
    return ev;
  }

  /** Options a struct node's rows are drawn with (live body and SVG export alike). */
  structOpts(node) {
    return { editMode: Boolean(this.canvas?.settings.editMode), evaluator: this.evaluatorFor(node) };
  }

  /** View state outside a node that its body depends on (Edit Mode, the Kconfig engine). */
  bodyKey(node) {
    if (node.type === "struct") return JSON.stringify([Boolean(this.canvas.settings.editMode), this.kconfigEngineFor(node)?.tabId || "", this.kconfigEpoch]);
    if (node.type === "note") return this.canvas.settings.editMode ? "edit" : "";
    if (node.type === "kconfig") {
      // Live value and which relations are already on the canvas
      const values = this.kconfigValuesFor(node);
      const names = this.canvas.nodes.filter((n) => n.type === "kconfig").map((n) => n.data.symbol).sort().join(",");
      return JSON.stringify([values ? kconfigValue(values, node.data.symbol) : null, names, this.kconfigEpoch]);
    }
    if (node.type === "mail") return String(this.mailEpoch || 0);
    if (node.type === "tab") return this.embedKey(M.embedSessionOf(this.canvas, node).id);
    return "";
  }

  // ------------------------------------------------------------------ NodeMap embedded sessions

  /** Key of one of this canvas's embedded sessions (unique across NodeMap tabs: tab id + session id). */
  embedKey(sessionId) {
    return `${this.tab?.id || "nodemap"}:${sessionId}`;
  }

  /** Open (or switch) the channel of the followed embedded session; none when the host is followed. */
  syncFollowChannel() {
    const id = this.canvas?.settings.followSession || "";
    const key = id ? this.embedKey(id) : "";
    if (key === this.followKey && (this.followChannel || !key)) return;
    this.closeFollowChannel();
    this.followKey = key;
    if (!key) return;
    let ch = null;
    try {
      ch = this.channelFactory(embedSessionChannel(key));
    } catch {
      ch = null;
    }
    if (!ch) return;
    ch.unref?.(); // Node test runs: an open channel never keeps the process alive
    this.followChannel = ch;
    ch.onmessage = (ev) => this.onEmbedMessage(ev?.data);
    // The tab iframes of the session answer with their running engines
    try {
      ch.postMessage({ type: "kconfig:request_engines", sessionId: `nodemap:${key}` });
    } catch {
      // closed meanwhile
    }
  }

  closeFollowChannel() {
    try {
      this.followChannel?.close?.();
    } catch {
      // already closed
    }
    const had = this.embedEngines.size > 0;
    this.followChannel = null;
    this.followKey = "";
    this.embedEngines.clear();
    if (had && this.canvas) this.onKconfigChanged();
  }

  /** Kconfig engine traffic of the followed embedded session (announce, assignments, revoke). */
  onEmbedMessage(msg) {
    if (msg?.type === "window:unloading") {
      // A shell went away: the engines it announced go with it
      let dropped = false;
      for (const [id, e] of this.embedEngines) {
        if (msg.winId && e.winId === msg.winId) {
          this.embedEngines.delete(id);
          dropped = true;
        }
      }
      if (dropped) this.onKconfigChanged();
      return;
    }
    if (!msg?.type || !msg.tabId) return;
    const resolvedOf = (a) => Math.floor(Object.keys(a || {}).length / 2) >= 1;
    if (msg.type === "kconfig:engine:announced") {
      this.embedEngines.set(msg.tabId, {
        tabId: msg.tabId, winId: msg.winId || "", title: msg.title || `KConfig (${msg.arch || "?"})`, version: msg.version || "",
        arch: msg.arch || "", configName: msg.configName || "", assignments: msg.assignments || {}, resolved: resolvedOf(msg.assignments), isEmbedded: true,
      });
    } else if (msg.type === "kconfig:assignments:broadcast") {
      const eng = this.embedEngines.get(msg.tabId);
      if (!eng) {
        try {
          this.followChannel?.postMessage({ type: "kconfig:request_engines", sessionId: `nodemap:${this.followKey}` });
        } catch {
          // closed
        }
        return;
      }
      eng.assignments = msg.assignments || {};
      eng.resolved = resolvedOf(eng.assignments);
      for (const k of ["title", "version", "arch"]) if (msg[k]) eng[k] = msg[k];
      if (typeof msg.configName === "string") eng.configName = msg.configName;
    } else if (msg.type === "kconfig:engine:revoked") {
      const eng = this.embedEngines.get(msg.tabId);
      if (!eng || (msg.winId && eng.winId && eng.winId !== msg.winId)) return;
      this.embedEngines.delete(msg.tabId);
    } else return;
    this.onKconfigChanged();
  }

  /** Nodes handed over by other views: `initialNodes`/`initialEdges`, `initialNode`, `pendingNodes`. */
  consumeIncoming(tab) {
    if (!tab) return;
    const added = [];
    const seeded = Array.isArray(tab.initialNodes);
    if (seeded) {
      this.canvas = M.emptyCanvas();
      const idMap = new Map();
      for (const spec of tab.initialNodes) {
        const node = this.nodeFromSpec(spec, { x: spec.x ?? 120, y: spec.y ?? 120 });
        if (!node) continue;
        if (spec.id) idMap.set(spec.id, node.id);
        this.canvas.nodes.push(node);
        added.push(node);
      }
      for (const e of tab.initialEdges || []) {
        const edge = M.createEdge({ ...e, from: idMap.get(e.from) || e.from, to: idMap.get(e.to) || e.to });
        if (edge && this.byId(edge.from) && this.byId(edge.to)) this.canvas.edges.push(edge);
      }
    }
    if (tab.initialNode) {
      const n = this.nodeFromLegacySymbol(tab.initialNode);
      if (n) added.push(this.placeNew(n));
    }
    if (Array.isArray(tab.pendingNodes)) {
      for (const spec of tab.pendingNodes) {
        const n = this.nodeFromSpec(spec);
        if (n) added.push(this.placeNew(n));
      }
    }
    const had = ["initialNodes", "initialEdges", "initialNode", "pendingNodes"].some((k) => k in tab);
    if (had) {
      state.updateTabState(tab.id, { initialNodes: null, initialEdges: null, initialNode: null, pendingNodes: null });
      for (const k of ["initialNodes", "initialEdges", "initialNode", "pendingNodes"]) delete tab[k];
    }
    if (added.length) {
      this.selected = new Set(added.map((n) => n.id));
      this.selectedEdge = null;
      if (seeded || added.length > 1) this.fitView(false);
      this.scheduleSave();
    }
  }

  // ------------------------------------------------------------------ node creation

  nodeFromSpec(spec, pos = null) {
    if (!spec || !M.NODE_TYPES.includes(spec.type)) return null;
    const data = spec.type === "tab" && !spec.data?.embedSessionId
      // A new tab node runs in the canvas's first embedded session
      ? { ...(spec.data || {}), embedSessionId: this.canvas?.settings.embedSessions?.[0]?.id || "" }
      : spec.data;
    return M.createNode(spec.type, {
      x: pos?.x ?? 0,
      y: pos?.y ?? 0,
      w: spec.w,
      h: spec.h,
      title: spec.title,
      customTitle: spec.customTitle,
      color: spec.color,
      scale: spec.scale,
      version: spec.version || this.currentVersion,
      repo: spec.repo || this.repo,
      data,
    });
  }

  /** Context-menu tokens (`{name, type, astId}`): struct/union tokens become struct nodes. */
  nodeFromLegacySymbol(d) {
    const name = String(d?.name || "").trim();
    if (!name) return null;
    const t = d.type;
    if (t === 28 || t === 34 || t === "struct" || /^(struct|union)\s+/.test(name)) {
      const kind = t === 34 || /^union\s/.test(name) ? "union" : "struct";
      return this.nodeFromSpec({ type: "struct", data: { name: name.replace(/^(struct|union)\s+/, ""), kind, hint: d.path || "" } });
    }
    const func = t === 5 || t === 6 || t === "func";
    return this.nodeFromSpec({ type: "symbol", data: { name: name.replace(/\(\)$/, ""), kind: func ? "func" : String(t || "symbol"), astId: Number.isInteger(d.astId) ? d.astId : null } });
  }

  /** Put a new node at the viewport center (cascading so several do not stack) and add it. */
  placeNew(node) {
    const c = this.viewCenter();
    const k = this.canvas.nodes.length % 8;
    node.x = Math.round(c.x - node.w / 2 + k * 24);
    node.y = Math.round(c.y - node.h / 2 + k * 24);
    if (this.canvas.settings.snap) {
      node.x = M.snapValue(node.x, this.canvas.settings.grid);
      node.y = M.snapValue(node.y, this.canvas.settings.grid);
    }
    this.canvas.nodes.push(node);
    if (node.type !== "group") M.reassignGroups(this.canvas.nodes, [node]);
    return node;
  }

  addNode(spec) {
    const node = spec?.type ? this.nodeFromSpec(spec) : this.nodeFromLegacySymbol(spec);
    if (!node) return null;
    this.placeNew(node);
    this.selected = new Set([node.id]);
    this.selectedEdge = null;
    this.commit();
    return node;
  }

  /** A group around the selection (or an empty one at the center). */
  addGroup() {
    const members = this.selectedNodes().filter((n) => n.type !== "group");
    const node = M.createNode("group", { version: this.currentVersion, repo: this.repo });
    if (members.length) {
      const b = M.boundsOf(members);
      Object.assign(node, { x: b.x - 24, y: b.y - 48, w: Math.max(M.SIZE_LIMITS.group.minW, b.w + 48), h: Math.max(M.SIZE_LIMITS.group.minH, b.h + 72) });
      this.canvas.nodes.push(node);
      M.reassignGroups(this.canvas.nodes, members);
    } else {
      this.placeNew(node);
    }
    this.selected = new Set([node.id]);
    this.commit();
    return node;
  }

  addNoteAt(x, y) {
    const node = M.createNode("note", { x, y, version: this.currentVersion, repo: this.repo });
    this.canvas.nodes.push(node);
    M.reassignGroups(this.canvas.nodes, [node]);
    this.selected = new Set([node.id]);
    this.commit();
    this.beginNoteEdit(node.id);
  }

  // ------------------------------------------------------------------ chrome

  chromeHtml() {
    return `
      <div class="nodemap-container" tabindex="0" aria-label="NodeMap canvas">
        <div class="nodemap-toolbar" role="toolbar" aria-label="NodeMap tools">
          <button type="button" class="nodemap-btn" data-tb="add" aria-haspopup="menu" title="Add a struct, source, man page, tab, symbol, note or group">+ Add Node</button>
          <button type="button" class="nodemap-btn" data-tb="tool" aria-pressed="${this.tool === "select"}" title="Select tool: drag on the canvas draws a selection box (Shift/Ctrl+drag does it with either tool)">▭ Select</button>
          <button type="button" class="nodemap-btn" data-tb="snap" aria-pressed="${this.canvas.settings.snap}" title="Snap positions and sizes to the grid">Snap</button>
          <div class="nodemap-sep"></div>
          <button type="button" class="nodemap-btn" data-tb="undo" title="Undo (Ctrl+Z)" aria-label="Undo" disabled>↶</button>
          <button type="button" class="nodemap-btn" data-tb="redo" title="Redo (Ctrl+Y / Ctrl+Shift+Z)" aria-label="Redo" disabled>↷</button>
          <div class="nodemap-sep"></div>
          <button type="button" class="nodemap-btn" data-tb="zoom-out" title="Zoom out">−</button>
          <span class="nm-zoom-label" title="Zoom">100%</span>
          <button type="button" class="nodemap-btn" data-tb="zoom-in" title="Zoom in">+</button>
          <button type="button" class="nodemap-btn" data-tb="fit" title="Fit everything into view">Fit</button>
          <button type="button" class="nodemap-btn" data-tb="reset" title="Reset pan and zoom (100%)">1:1</button>
          <div class="nodemap-sep"></div>
          <button type="button" class="nodemap-btn" data-tb="layout" title="Arrange nodes by their connections">Auto-Layout</button>
          <div class="nodemap-sep"></div>
          <button type="button" class="nodemap-btn" data-tb="export" aria-haspopup="menu">Export ▾</button>
          <button type="button" class="nodemap-btn" data-tb="import" title="Replace this canvas with an exported JSON file">Import</button>
          <input type="file" class="nm-file-input" accept=".json,application/json" hidden />
        </div>
        <div class="nodemap-canvas-wrapper">
          <div class="nodemap-overlay-layer">
            <svg class="nm-wire-layer" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"></svg>
            <div class="nm-marquee" hidden></div>
          </div>
          <div class="nm-empty-hint" hidden>
            <b>Empty canvas</b>
            <span>+ Add Node, double-click to write a note, or use “Send to NodeMap” in source, man page and tab menus.</span>
          </div>
        </div>
        <aside class="nodemap-inspector" aria-label="Inspector">
          <div class="inspector-header">Inspector</div>
          <div class="nm-inspector-content"></div>
        </aside>
      </div>`;
  }

  attachToolbar() {
    const tb = this.rootEl.querySelector(".nodemap-toolbar");
    const fileInput = this.rootEl.querySelector(".nm-file-input");
    tb.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-tb]");
      if (!btn) return;
      const act = btn.dataset.tb;
      const r = btn.getBoundingClientRect();
      if (act === "add") this.showAddMenu(r.left, r.bottom + 4);
      else if (act === "tool") {
        this.tool = this.tool === "select" ? "pan" : "select";
        btn.setAttribute("aria-pressed", String(this.tool === "select"));
        this.wrapEl.classList.toggle("nm-tool-select", this.tool === "select");
      } else if (act === "snap") {
        this.canvas.settings.snap = !this.canvas.settings.snap;
        btn.setAttribute("aria-pressed", String(this.canvas.settings.snap));
        this.applyTransform();
        this.commit();
      } else if (act === "zoom-in") this.adjustZoom(1.2);
      else if (act === "zoom-out") this.adjustZoom(1 / 1.2);
      else if (act === "fit") this.fitView();
      else if (act === "undo") this.undo();
      else if (act === "redo") this.redo();
      else if (act === "reset") this.resetTransform();
      else if (act === "layout") this.autoLayoutDAG();
      else if (act === "export") this.showExportMenu(r.left, r.bottom + 4);
      else if (act === "import") fileInput.click();
    });
    fileInput.onchange = (e) => {
      this.importJSON(e.target.files?.[0]);
      fileInput.value = "";
    };
    this.wrapEl.classList.toggle("nm-tool-select", this.tool === "select");
  }

  showAddMenu(x, y) {
    contextMenu.renderMenu(x, y, {
      header: { title: "Add Node", sub: "Placed at the center of the view" },
      items: ADD_MENU.map(([type, label, icon]) => ({
        label,
        icon,
        onClick: () => {
          if (type === "note") this.addNode({ type: "note", data: { text: "" } });
          else if (type === "group") this.addGroup();
          else this.openNodeDialog(type);
        },
      })),
    });
  }

  showExportMenu(x, y) {
    contextMenu.renderMenu(x, y, {
      header: { title: "Export", sub: "This canvas" },
      items: [
        { label: "Canvas JSON (re-importable)", icon: "{}", onClick: () => this.exportJSON() },
        { label: "SVG image…", icon: "🖼", onClick: () => this.openImageExport("svg") },
        { label: "PNG image…", icon: "🖼", onClick: () => this.openImageExport("png") },
      ],
    });
  }

  // ------------------------------------------------------------------ add-node dialogs

  openNodeDialog(type, existing = null) {
    const v = existing?.version || this.currentVersion || "";
    const field = (name, label, value = "", extra = "") =>
      `<label class="nm-form-row"><span>${escapeHtml(label)}</span><input name="${name}" value="${escapeHtml(value ?? "")}" ${extra}/></label>`;
    let body = field("version", "Revision", v, 'spellcheck="false"');
    let title = "";
    if (type === "struct") {
      title = "Add Struct / Union";
      body += field("name", "Name", "", 'placeholder="task_struct" spellcheck="false" required');
      body += `<label class="nm-form-row"><span>Kind</span><select name="kind"><option value="struct">struct</option><option value="union">union</option></select></label>`;
      body += field("path", "Defining file (needed for unparsed revisions)", "", 'placeholder="include/linux/sched.h" spellcheck="false"');
    } else if (type === "source") {
      title = "Add Source File / Slice";
      body += field("path", "File", "", 'placeholder="kernel/fork.c" spellcheck="false" required');
      body += `<label class="nm-form-row"><span>Show</span><select name="mode"><option value="file">Whole file</option><option value="range">Line range</option><option value="symbol">Symbol definition</option></select></label>`;
      body += `<div class="nm-form-pair">${field("start", "From line", "", 'type="number" min="1"')}${field("end", "To line", "", 'type="number" min="1"')}</div>`;
      body += `<div class="nm-form-pair">${field("symbol", "Symbol", "", 'placeholder="copy_process" spellcheck="false"')}
        <label class="nm-form-row"><span>Kind</span><select name="symbolKind">${M.SYMBOL_KINDS.map((k) => `<option value="${k}">${k || "any"}</option>`).join("")}</select></label></div>`;
      body += field("context", "Context lines around a symbol", "0", 'type="number" min="0" max="200"');
    } else if (type === "man") {
      title = "Add Man Page";
      body += field("name", "Page", "", 'placeholder="open" spellcheck="false" required');
      body += field("section", "Section", "", 'placeholder="2" spellcheck="false"');
    } else if (type === "symbol") {
      title = "Add Symbol";
      body += field("name", "Symbol", "", 'placeholder="schedule" spellcheck="false" required');
      body += `<label class="nm-form-row"><span>Kind</span><select name="kind"><option value="func">function</option><option value="symbol">other</option></select></label>`;
    } else if (type === "diff") {
      title = "Add Diff";
      body += `<label class="nm-form-row"><span>Show</span><select name="mode"><option value="file">One file between two revisions</option><option value="commit">A commit</option></select></label>`;
      body += `<div class="nm-form-pair">${field("versionA", "From revision (file diff)", "", 'placeholder="v2.6.39" spellcheck="false"')}${field("versionB", "To revision (file diff)", v, 'spellcheck="false"')}</div>`;
      body += field("path", "File (all files of a commit when empty)", "", 'placeholder="kernel/fork.c" spellcheck="false"');
      body += field("oldPath", "Old path (renamed file)", "", 'spellcheck="false"');
      body += field("commitHash", "Commit (commit diff)", "", 'placeholder="a1b2c3d4" spellcheck="false"');
    } else if (type === "mail") {
      title = "Add Mailing List Thread";
      body += field("tid", "Thread id", "", 'type="number" min="1"');
      body += field("mid", "Message-ID (focused message, or the thread it starts)", "", 'placeholder="20110101.12345@example.org" spellcheck="false"');
    } else if (type === "kconfig") {
      title = "Add Kconfig Symbol";
      body += field("symbol", "Symbol", "", 'placeholder="SMP" spellcheck="false" required');
      body += field("arch", "ARCH", "x86_64", 'spellcheck="false"');
    } else if (type === "maintainer") {
      title = "Add Maintainers Section";
      body += field("query", "Section name", "", 'placeholder="SCHEDULER" spellcheck="false"');
      body += field("path", "Or the section of a file", "", 'placeholder="kernel/sched/core.c" spellcheck="false"');
      body += field("secId", "Or a section id", "", 'type="number" min="1"');
    } else if (type === "tab") {
      title = "Add KernelInfo Tab";
      const repo = this.repo;
      const types = EMBED_TAB_TYPES.filter(([t]) => isTabFeatureEnabled(repo, t));
      const open = state.panes.flatMap((p) => p.tabs).filter((t) => t.type !== "nodemap");
      body += `<label class="nm-form-row"><span>Open tab (embeds what it shows)</span><select name="openTab"><option value="">— a new view instead —</option>${open.map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(`${t.title || t.type} · ${t.type}`)}</option>`).join("")}</select></label>`;
      body += `<label class="nm-form-row"><span>New view</span><select name="tabType">${types.map(([t, l]) => `<option value="${t}">${escapeHtml(l)}</option>`).join("")}</select></label>`;
      body += field("extra", "Address parameters (optional)", "", 'placeholder="file=kernel/fork.c&amp;line=120" spellcheck="false"');
    }
    const modal = openModal({ title, subtitle: "The node is placed at the center of the view", width: "480px", footerHtml: '<button type="button" class="code-btn primary" data-act="add">Add</button>' });
    modal.body.innerHTML = `<form class="nm-form">${body}</form>`;
    const form = modal.body.querySelector("form");
    form.querySelector("input[name='name'], input[name='path'], input[name='symbol'], input[name='tid'], input[name='query']")?.focus();
    const submit = async () => {
      const f = Object.fromEntries(new FormData(form).entries());
      const spec = await this.specFromForm(type, f);
      if (!spec) return;
      modal.close();
      this.addNode(spec);
    };
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      submit();
    });
    modal.footer.querySelector("[data-act='add']").onclick = submit;
  }

  async specFromForm(type, f) {
    const version = String(f.version || this.currentVersion || "").trim();
    const int = (v) => (/^\d+$/.test(String(v || "").trim()) ? parseInt(v, 10) : null);
    if (type === "struct") {
      const name = String(f.name || "").trim().replace(/^(struct|union)\s+/, "");
      if (!/^[A-Za-z_]\w*$/.test(name)) return toast.warning("Enter a struct or union name"), null;
      return { type, version, data: { name, kind: f.kind, path: String(f.path || "").trim() } };
    }
    if (type === "source") {
      const path = String(f.path || "").trim();
      if (!path) return toast.warning("Enter a file path"), null;
      return { type, version, data: { path, mode: f.mode, start: int(f.start), end: int(f.end), symbol: String(f.symbol || "").trim(), symbolKind: f.symbolKind, context: int(f.context) || 0 } };
    }
    if (type === "symbol") {
      const name = String(f.name || "").trim();
      if (!name) return toast.warning("Enter a symbol name"), null;
      return { type, version, data: { name, kind: f.kind } };
    }
    if (type === "man") {
      const name = String(f.name || "").trim();
      if (!name) return toast.warning("Enter a man page name"), null;
      return { type, version, data: { name, section: String(f.section || "").trim() } };
    }
    if (type === "diff") {
      const commitHash = String(f.commitHash || "").trim();
      const path = String(f.path || "").trim();
      if (f.mode === "commit") {
        if (!/^[0-9a-f]{4,64}$/i.test(commitHash)) return toast.warning("Enter a commit hash"), null;
        return { type, version, data: { mode: "commit", commitHash, path } };
      }
      const versionA = String(f.versionA || "").trim();
      const versionB = String(f.versionB || version).trim();
      if (!path || !versionA || !versionB) return toast.warning("Enter a file and both revisions"), null;
      return { type, version: versionB, data: { mode: "file", versionA, versionB, path, oldPath: String(f.oldPath || "").trim() } };
    }
    if (type === "mail") {
      const tid = int(f.tid);
      const mid = String(f.mid || "").trim().replace(/^<|>$/g, "");
      if (!tid && !mid) return toast.warning("Enter a thread id or a Message-ID"), null;
      return { type, version, data: { tid, mid } };
    }
    if (type === "kconfig") {
      const symbol = String(f.symbol || "").trim().replace(/^CONFIG_/, "");
      if (!/^[A-Za-z0-9_]+$/.test(symbol)) return toast.warning("Enter a Kconfig symbol"), null;
      return { type, version, data: { symbol, arch: String(f.arch || "").trim() || "x86_64" } };
    }
    if (type === "maintainer") {
      const secId = int(f.secId);
      const query = String(f.query || "").trim();
      const path = String(f.path || "").trim();
      if (!secId && !query && !path) return toast.warning("Enter a section name, a file or a section id"), null;
      return { type, version, data: { secId, query, path } };
    }
    if (type === "tab") {
      if (f.openTab) {
        const t = state.findTab(f.openTab);
        if (!t) return null;
        return { type, version: t.version || version, repo: t.repo || this.repo, title: t.title, data: { tabType: t.type, hash: urlSync.paramsForTab(t).toString() } };
      }
      const params = new URLSearchParams(String(f.extra || "").replace(/^[#?]/, ""));
      if (this.repo) params.set("repo", this.repo);
      if (!isAlwaysVersionlessTab(f.tabType) && version) params.set("v", version);
      params.set("tab", f.tabType);
      return { type, version, data: { tabType: f.tabType, hash: params.toString() } };
    }
    return null;
  }

  // ------------------------------------------------------------------ bodies

  fetchKeyOf(node) {
    const d = node.data;
    if (node.type === "struct") return JSON.stringify(["struct", node.repo, node.version, d.name, d.kind, d.path, d.hint, d.line]);
    if (node.type === "source") return JSON.stringify(["source", node.repo, node.version, d.path, d.mode, d.start, d.end, d.symbol, d.symbolKind, d.context]);
    if (node.type === "diff") return JSON.stringify(["diff", node.repo, node.version, d.mode, d.versionA, d.versionB, d.path, d.oldPath, d.commitHash]);
    if (node.type === "mail") return JSON.stringify(["mail", d.tid, d.mid, d.single]);
    if (node.type === "kconfig") return JSON.stringify(["kconfig", node.repo, node.version, d.symbol, d.arch]);
    if (node.type === "maintainer") return JSON.stringify(["maintainer", node.repo, node.version, d.secId, d.query, d.path]);
    return null;
  }

  /**
   * What a domain node shows, from its loaded payload: the diff payload, the mail reply tree, the Kconfig
   * and MAINTAINERS models (`domain_nodes.js`); the raw payload for struct / source nodes. Null while not
   * loaded.
   */
  modelOf(node) {
    const entry = this.content.get(this.fetchKeyOf(node));
    if (entry?.status !== "ok") return null;
    const v = entry.value;
    if (node.type === "mail") return mailThreadModel(v, node.data, this.mailBodies);
    if (node.type === "kconfig") return kconfigModel(v?.detail, v?.graph, node.data.symbol);
    if (node.type === "maintainer") return maintainerModel(v);
    return v;
  }

  /** Fetch a node's content once per fetch key; the body re-renders when it lands. */
  ensureContent(node, load) {
    const key = this.fetchKeyOf(node);
    let entry = this.content.get(key);
    if (!entry) {
      entry = { status: "loading" };
      this.content.set(key, entry);
      load().then((value) => {
        entry.status = "ok";
        entry.value = value;
      }, (err) => {
        entry.status = "error";
        entry.error = err;
      }).finally(() => {
        for (const n of this.canvas.nodes) if (this.fetchKeyOf(n) === key) this.renderer?.invalidate(n.id);
        if (this.containerEl?.isConnected) this.commit({ save: false, inspector: false });
      });
    }
    return entry;
  }

  clientFor(node) {
    const base = this.api || api;
    return typeof base.forRepo === "function" ? base.forRepo(node.repo || this.repo || "") : base;
  }

  renderBody(node, body) {
    const status = (msg, err = false) => `<div class="nm-status${err ? " nm-error" : ""}">${escapeHtml(msg)}</div>`;
    if (node.type === "group") {
      body.innerHTML = "";
    } else if (node.type === "note") {
      const empty = this.canvas.settings.editMode ? "Double-click to write a note" : "Empty note (Edit Mode edits it)";
      let html = node.data.markdown ? renderNoteMarkdown(node.data.text) : `<pre class="nm-note-plain">${escapeHtml(node.data.text || empty)}</pre>`;
      if (!String(node.data.text || "").trim()) html = `<p class="nm-empty">${escapeHtml(empty)}</p>`;
      body.innerHTML = `<div class="nm-note nm-scroll">${html}</div>`;
    } else if (node.type === "struct") {
      const prevScroll = body.querySelector(".nm-struct-rows")?.scrollTop || 0;
      const entry = this.ensureContent(node, () => this.loadStruct(node));
      if (entry.status === "loading") body.innerHTML = status(`Loading ${node.data.kind} ${node.data.name}…`);
      else if (entry.status === "error") {
        body.innerHTML = `${status(entry.error?.message || "Not found", true)}<div class="nm-status">Set the defining file in the inspector to read it from the source (unparsed revisions).</div>`;
      } else {
        body.innerHTML = renderStructBody(entry.value, node.data, this.structOpts(node));
        const rows = body.querySelector(".nm-struct-rows");
        if (rows) rows.scrollTop = prevScroll;
      }
    } else if (node.type === "source") {
      const prevScroll = body.querySelector(".nm-src")?.scrollTop || 0;
      if (!node.data.path && !(node.data.mode === "symbol" && node.data.symbol)) {
        body.innerHTML = status("Set a file in the inspector");
        return;
      }
      const entry = this.ensureContent(node, () => this.loadSource(node));
      if (entry.status === "loading") body.innerHTML = status(`Loading ${node.data.path}…`);
      else if (entry.status === "error") {
        body.innerHTML = status(entry.error?.message || "Could not load the file", true) +
          (node.data.path ? "" : status("Set the defining file in the inspector to read it from the source (unparsed revisions)."));
      }
      else {
        const r = entry.value;
        const hl = r.symbol && (r.symbol.line_s > r.start_line || r.symbol.line_e < r.end_line)
          ? new Set(Array.from({ length: r.symbol.line_e - r.symbol.line_s + 1 }, (_, i) => r.symbol.line_s + i)) : null;
        const lines = Array.isArray(r.lines) ? r.lines : [];
        // The slice's path and lines show in the header (headMeta)
        body.innerHTML = `<div class="nm-src nm-scroll">${renderSourceRows(lines, r.start_line, r.path, hl)}</div>`;
        const src = body.querySelector(".nm-src");
        if (src) src.scrollTop = prevScroll;
      }
    } else if (node.type === "man") {
      this.mountMan(node, body);
    } else if (node.type === "tab") {
      if (!node.data.hash) {
        body.innerHTML = status("No tab address", true);
        return;
      }
      const sess = M.embedSessionOf(this.canvas, node);
      const src = M.embedSrc(node.data.hash, typeof location !== "undefined" ? location.pathname : "/", this.embedKey(sess.id));
      body.innerHTML = `<div class="nm-tabframe">
          <iframe class="nm-iframe" src="${escapeHtml(src)}" title="${escapeHtml(M.displayTitle(node))}" loading="lazy" referrerpolicy="same-origin"></iframe>
          <button type="button" class="nm-shield" data-act="interact" title="Click to interact with this tab (Esc or a click on the canvas leaves it)">Click to interact</button>
        </div>`;
    } else if (["diff", "mail", "kconfig", "maintainer"].includes(node.type)) {
      const prevScroll = body.querySelector(".nm-scroll")?.scrollTop || 0;
      const load = { diff: () => this.loadDiff(node), mail: () => this.loadMail(node), kconfig: () => this.loadKconfig(node), maintainer: () => this.loadMaintainer(node) }[node.type];
      const entry = this.ensureContent(node, load);
      if (entry.status === "loading") body.innerHTML = status(`Loading ${M.displayTitle(node)}…`);
      else if (entry.status === "error") body.innerHTML = status(entry.error?.message || "Could not load it", true);
      else {
        const model = this.modelOf(node);
        if (node.type === "diff") body.innerHTML = renderDiffBody(model);
        else if (node.type === "mail") body.innerHTML = renderMailBody(model);
        else if (node.type === "maintainer") body.innerHTML = renderMaintainerBody(model);
        else {
          const values = this.kconfigValuesFor(node);
          const onCanvas = new Set(this.canvas.nodes.filter((n) => n.type === "kconfig" && n.id !== node.id).map((n) => n.data.symbol));
          body.innerHTML = renderKconfigBody(model, values ? kconfigValue(values, model.name) : null, onCanvas);
        }
        const sc = body.querySelector(".nm-scroll");
        if (sc) sc.scrollTop = prevScroll;
      }
    } else if (node.type === "symbol") {
      const list = node.data.constituents || [];
      body.innerHTML = node.data.expanded
        ? `<div class="nm-sym-list nm-scroll">${list.length ? list.map((c, i) => `<div class="nm-sf nm-sf-field" data-key="${i}:${escapeHtml(c.name)}"><span class="nm-sf-name">${escapeHtml(c.name)}</span></div>`).join("") : '<div class="nm-empty">No members found</div>'}</div>`
        : `<div class="nm-status">${escapeHtml(node.data.kind === "func" ? "Function" : "Symbol")} · ${escapeHtml(node.version)}</div>`;
    }
  }

  disposeBody(id) {
    const m = this.mounts.get(id);
    if (m?.dispose) m.dispose();
    this.mounts.delete(id);
  }

  async loadStruct(node) {
    const d = node.data;
    const client = this.clientFor(node);
    const opts = { path: d.path, kind: d.kind, line: d.line, fresh: this.takeFresh(node.id) };
    try {
      return await client.getNodemapStruct(node.version, d.name, opts);
    } catch (err) {
      // Unparsed revision without an explicit file: try where the node was sent from
      if (!d.path && d.hint) return client.getNodemapStruct(node.version, d.name, { ...opts, path: d.hint });
      throw err;
    }
  }

  async loadSource(node) {
    const d = node.data;
    const client = this.clientFor(node);
    const fresh = this.takeFresh(node.id);
    if (d.mode === "symbol" && d.symbol) return client.getNodemapSource(node.version, { path: d.path, symbol: d.symbol, kind: d.symbolKind, context: d.context, fresh });
    if (d.mode === "range" && d.start) return client.getNodemapSource(node.version, { path: d.path, start: d.start, end: d.end || d.start, fresh });
    return client.getNodemapSource(node.version, { path: d.path, fresh });
  }

  async loadDiff(node) {
    const d = node.data;
    const client = this.clientFor(node);
    if (d.mode === "commit") {
      if (!d.commitHash) throw new Error("Set a commit in the inspector");
      return diffPayload(d, await client.getCommitDiff(node.version || this.currentVersion, d.commitHash));
    }
    if (!d.path || !d.versionA || !d.versionB) throw new Error("Set a file and both revisions in the inspector");
    return diffPayload(d, await client.getFileDiff(d.versionA, d.versionB, d.path, d.oldPath || ""));
  }

  async loadMail(node) {
    const d = node.data;
    const client = this.api || api;
    const oldKey = this.fetchKeyOf(node);
    // A message node of its own shows that message (with its body); a thread node the whole thread
    const res = d.single ? await client.getMailMessage(d.mid) : await client.getMailThread(d.tid || 0, d.mid || "");
    if (d.single && res?.body && d.mid) this.mailBodies.set(d.mid, String(res.body));
    if (res && res.available === false) throw new Error(res.error || "Not in the mailing-list archive");
    // A thread found from its message: the node keeps the thread id and subject (not a user change)
    const tid = res?.tid || res?.messages?.[0]?.tid || null;
    const subject = res?.subject || res?.messages?.[0]?.subject || "";
    if ((tid && !d.tid) || (subject && !d.subject)) {
      setTimeout(() => {
        if (tid && !node.data.tid) node.data.tid = tid;
        if (subject && !node.data.subject) node.data.subject = subject;
        // The fetch key follows tid: the loaded thread is kept under the new key
        const entry = this.content.get(oldKey);
        if (entry) this.content.set(this.fetchKeyOf(node), entry);
        if (!node.customTitle) node.title = M.defaultTitle(node.type, node.data);
        if (this.containerEl?.isConnected) this.commit({ absorb: true });
      }, 0);
    }
    return res;
  }

  async loadKconfig(node) {
    const d = node.data;
    const client = this.clientFor(node);
    const version = node.version || this.currentVersion;
    const [detail, graph] = await Promise.all([
      client.getKconfigSymbol(version, d.symbol, d.arch),
      client.getKconfigGraph(version, d.arch, d.symbol).catch(() => ({ edges: [] })),
    ]);
    return { detail, graph };
  }

  async loadMaintainer(node) {
    const d = node.data;
    const client = this.clientFor(node);
    const version = node.version || this.currentVersion;
    let secId = d.secId;
    if (!secId && d.path) {
      const m = await client.matchMaintainers(version, "", [d.path]);
      const name = m?.subsystems?.[0];
      if (!name) throw new Error(`No MAINTAINERS section covers ${d.path}`);
      const ov = await client.getMaintainersOverview(version, name);
      secId = (ov?.sections || []).find((x) => x.name === name)?.sec_id || ov?.sections?.[0]?.sec_id || null;
    } else if (!secId && d.query) {
      const ov = await client.getMaintainersOverview(version, d.query);
      const list = ov?.sections || [];
      const q = d.query.toLowerCase();
      secId = (list.find((x) => String(x.name).toLowerCase() === q) || list[0])?.sec_id || null;
    }
    if (!secId) throw new Error("No matching MAINTAINERS section");
    return client.getMaintainerSection(version, secId);
  }

  /** Whether ⟳ asked for this node's content from the server (past the local API cache); asked once. */
  takeFresh(id) {
    const hit = Boolean(this.fresh?.has(id));
    this.fresh?.delete(id);
    return hit;
  }

  async mountMan(node, body) {
    const d = node.data;
    const token = {};
    this.mounts.set(node.id, token);
    if (!d.path || !d.repo || !d.version) {
      body.innerHTML = `<div class="nm-status">Looking up ${escapeHtml(d.name || "the page")}…</div>`;
      try {
        const r = await (this.api || api).resolveMan(d.name, d.section, node.version);
        if (this.mounts.get(node.id) !== token) return;
        if (!r?.available) {
          body.innerHTML = `<div class="nm-status nm-error">${escapeHtml(d.name)}(${escapeHtml(d.section || "?")}) is not in a configured man-pages repository</div>`;
          return;
        }
        // Filling in where the page lives changes the content key: the body mounts again with it (not a
        // user change: undo history takes it as the baseline)
        Object.assign(node.data, { repo: r.repo, version: r.version, path: r.path, name: r.name || d.name, section: r.section || d.section });
        this.commit({ absorb: true });
      } catch (err) {
        if (this.mounts.get(node.id) === token) body.innerHTML = `<div class="nm-status nm-error">${escapeHtml(err.message)}</div>`;
      }
      return;
    }
    body.innerHTML = '<div class="nm-man nm-scroll"></div>';
    // A node-local tab object: the mode and width shown stay with this node, not the workspace's man tab
    // (choosing one still updates the remembered man page preference, as in a man tab)
    const local = { manMode: d.mode || undefined };
    await mountManDocument(body.firstElementChild, {
      repo: d.repo,
      version: d.version,
      path: d.path,
      tab: local,
      canGoBack: false,
      onNavigate: (path) => {
        Object.assign(node.data, { path, name: "", section: "" });
        this.commit();
      },
      onShowSource: () => state.openTab({ type: "code", title: d.path.split("/").pop(), path: d.path, repo: d.repo, version: d.version }),
    }).catch((err) => {
      if (this.mounts.get(node.id) === token) body.innerHTML = `<div class="nm-status nm-error">${escapeHtml(err.message)}</div>`;
    });
    body.querySelector(".man-body")?.classList.add("nm-scroll");
    // The page is in: an auto-fitted node takes its size now
    if (node.autoFit && this.mounts.get(node.id) === token) {
      this.fitQueue.add(node.id);
      this.commit({ save: false, inspector: false });
    }
  }

  headTools(node, active) {
    const b = (act, label, title) => `<button type="button" class="nm-head-btn" data-act="${act}" title="${escapeHtml(title)}">${label}</button>`;
    let s = "";
    if (this.canvas?.settings.editMode) {
      s += node.hideHeader
        ? b("toggle-header", "◫", "Header hidden in View Mode: click to show it again")
        : b("toggle-header", "▔", "Hide this header in View Mode (Edit Mode keeps it, dimmed)");
    }
    if (node.type === "tab") s += active ? b("interact", "✓", "Done interacting (Esc)") : b("interact", "⤢", "Interact with the embedded tab");
    if (node.type === "symbol") s += b("expand", node.data.expanded ? "−" : "+", node.data.expanded ? "Collapse members" : "Expand members");
    if (node.type === "mail") s += b("mail-expand", "⇶", "Expand Thread to Nodes: one connected node per message");
    if (node.type === "kconfig") s += b("kc-expand", "⇶", "Expand Relations: add or connect every related symbol");
    if (["struct", "source", "man", "diff", "mail", "kconfig", "maintainer"].includes(node.type)) s += b("refresh", "⟳", "Load again");
    if (M.TAB_OPENABLE_TYPES.includes(node.type)) s += b("open-tab", "↗", "Open as a tab");
    return s;
  }

  /** Source reference shown at the right of a struct / source header (from its loaded content). */
  headMeta(node) {
    if (!["struct", "source", "diff", "mail", "kconfig", "maintainer"].includes(node.type)) return null;
    return headMetaOf(node.type, this.modelOf(node));
  }

  // ------------------------------------------------------------------ view / commit

  byId(id) {
    return this.canvas.nodes.find((n) => n.id === id) || null;
  }

  selectedNodes() {
    return this.canvas.nodes.filter((n) => this.selected.has(n.id));
  }

  /**
   * Re-render nodes, wires and (unless asked not to) the inspector; persist unless `save` is false.
   * Undo history: a `record`ed commit (by default every saving one) pushes the document as it was before
   * when it changed; `coalesce` folds consecutive commits of one live edit (a color drag, typing) into one
   * step, `coalesceEnd` closes it (the final change event); `absorb` takes a change that is not the
   * user's (a resolved man page, Edit Mode re-fits, an undo) as the new baseline, as do the auto-fit
   * size changes of unrecorded commits.
   */
  commit({ save = true, inspector = true, record = save, coalesce = null, coalesceEnd = false, absorb = false } = {}) {
    if (!this.renderer) return;
    const ids = new Set(this.canvas.nodes.map((n) => n.id));
    for (const id of [...this.selected]) if (!ids.has(id)) this.selected.delete(id);
    if (this.selectedEdge && !this.canvas.edges.some((e) => e.id === this.selectedEdge)) this.selectedEdge = null;
    if (this.active && !ids.has(this.active)) this.active = null;
    this.rootEl?.classList.toggle("nm-edit", Boolean(this.canvas.settings.editMode));
    this.renderer.sync(this.canvas.nodes, this.selected, this.active);
    const fitted = this.autoFitPass();
    this.applyBackground();
    this.renderWires();
    if (this.hintEl) this.hintEl.hidden = this.canvas.nodes.length > 0;
    this.trackHistory({ record, coalesce, coalesceEnd, absorb: absorb || (fitted && !record) });
    if (inspector) this.renderInspector();
    if (save || fitted) this.scheduleSave();
  }

  // ------------------------------------------------------------------ undo / redo

  trackHistory({ record, coalesce, coalesceEnd = false, absorb }) {
    const h = this.history;
    if (!h || !this.canvas) return;
    if (h.base === null) h.absorb(M.historySnapshot(this.canvas));
    // A gesture in progress keeps the baseline from before it: its end records the whole gesture
    else if (absorb) {
      if (!this.gesture) h.absorb(M.historySnapshot(this.canvas));
    } else if (record) h.record(M.historySnapshot(this.canvas), coalesce, coalesceEnd);
    this.updateHistoryButtons();
  }

  updateHistoryButtons() {
    const tb = this.rootEl?.querySelector?.(".nodemap-toolbar");
    if (!tb) return;
    const u = tb.querySelector("[data-tb='undo']");
    const r = tb.querySelector("[data-tb='redo']");
    if (u) u.disabled = !this.history.canUndo;
    if (r) r.disabled = !this.history.canRedo;
  }

  /** Undo the last document change (false when there is none or a gesture is in progress). */
  undo() {
    if (this.gesture) return false;
    const snap = this.history.undo(M.historySnapshot(this.canvas));
    if (!snap) return false;
    this.restoreSnapshot(snap);
    return true;
  }

  /** Redo the change undone last (false when there is none). */
  redo() {
    if (this.gesture) return false;
    const snap = this.history.redo(M.historySnapshot(this.canvas));
    if (!snap) return false;
    this.restoreSnapshot(snap);
    return true;
  }

  /**
   * Put a history snapshot back: nodes, edges and document settings (view state, Edit Mode and snap
   * stay). Unchanged nodes keep their elements and bodies (live iframes survive); the selection keeps
   * the nodes that still exist.
   */
  restoreSnapshot(snap) {
    const doc = JSON.parse(snap);
    const st = this.canvas.settings || {};
    // Nodes and edges that still exist keep their object (bodies, man page callbacks and open editors hold
    // it): their fields are replaced in place
    const reuse = (list, fresh) => {
      const byId = new Map(list.map((x) => [x.id, x]));
      return fresh.map((x) => {
        const cur = byId.get(x.id);
        if (!cur) return x;
        for (const k of Object.keys(cur)) if (!(k in x)) delete cur[k];
        return Object.assign(cur, x);
      });
    };
    this.canvas.nodes = reuse(this.canvas.nodes, doc.nodes);
    this.canvas.edges = reuse(this.canvas.edges, doc.edges);
    this.canvas.settings = { ...doc.settings, editMode: st.editMode, snap: st.snap };
    for (const n of this.canvas.nodes) if (n.autoFit) this.fitQueue.add(n.id);
    // The followed embedded session may be another one now
    this.syncFollowChannel();
    this.applyTransform();
    this.commit({ absorb: true });
  }

  /** Canvas background (`settings.bgColor`) on the canvas wrapper. */
  applyBackground() {
    const style = this.wrapEl?.style;
    if (!style) return;
    const c = this.canvas.settings.bgColor;
    if (c) style.setProperty("--nm-canvas-bg", M.bgVar(c, "var(--bg-canvas)"));
    else style.removeProperty?.("--nm-canvas-bg");
  }

  // ------------------------------------------------------------------ auto-fit

  /**
   * Measure the auto-fitted nodes whose body was rebuilt (or that asked for it) and size them to their
   * content; true when a geometry changed.
   */
  autoFitPass() {
    const ids = new Set([...(this.renderer.takeRebuilt?.() || []), ...this.fitQueue]);
    this.fitQueue.clear();
    let changed = false;
    for (const id of ids) {
      const node = this.byId(id);
      if (!node?.autoFit) continue;
      const nat = this.measureNatural(node);
      if (nat && M.applyNaturalSize(node, nat.w, nat.h)) {
        changed = true;
        const el = this.renderer.element(id);
        if (el && this.renderer.place) this.renderer.place(el, node);
      }
    }
    return changed;
  }

  /**
   * Natural (unscaled, unclipped) size of a node's content: its element laid out at max-content (notes
   * and man pages wrap, so they keep their width and grow in height). Null without a live element.
   */
  measureNatural(node) {
    const el = this.renderer?.element(node.id);
    if (!el?.isConnected || typeof el.offsetWidth !== "number") return null;
    const wraps = node.type === "note" || node.type === "man";
    el.style.setProperty("--nm-measure-w", `${node.baseW}px`);
    el.classList.add("nm-measuring");
    if (wraps) el.classList.add("nm-measure-wrap");
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    el.classList.remove("nm-measuring", "nm-measure-wrap");
    if (!w || !h) return null;
    // A little slack so rounding never brings scrollbars back; a wrapping node keeps its width exactly
    // (it was measured at that width: slack there would widen it on every rebuild)
    return { w: wraps ? node.baseW : Math.ceil(w) + 2, h: Math.ceil(h) + 2 };
  }

  // Kept for the context menu and other views
  updateView() {
    this.commit({ save: false });
  }

  scheduleSave() {
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => this.flushSave(), SAVE_DELAY_MS);
  }

  flushSave() {
    if (!this.saveTimer || !this.canvas) return;
    clearTimeout(this.saveTimer);
    this.saveTimer = null;
    if (!M.saveCanvas(globalThis.localStorage, this.storageKey, this.canvas) && !this.warnedQuota) {
      this.warnedQuota = true;
      toast.warning("NodeMap could not save this canvas in the browser storage (full?): export it as JSON to keep it");
    }
  }

  applyTransform() {
    if (!this.layerEl) return;
    const { x, y, zoom } = this.canvas.viewport;
    this.layerEl.style.transform = `translate(${x}px, ${y}px) scale(${zoom})`;
    if (this.zoomLabel) this.zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
    const g = this.canvas.settings.grid * zoom;
    this.wrapEl.classList.toggle("nm-grid-on", this.canvas.settings.snap);
    this.wrapEl.style.setProperty("--nm-grid", `${g}px`);
    this.wrapEl.style.setProperty("--nm-grid-x", `${x}px`);
    this.wrapEl.style.setProperty("--nm-grid-y", `${y}px`);
  }

  get zoom() {
    return this.canvas?.viewport.zoom || 1;
  }

  /** Client point -> canvas units. */
  toCanvas(clientX, clientY) {
    const r = this.wrapEl.getBoundingClientRect();
    const { x, y, zoom } = this.canvas.viewport;
    return { x: (clientX - r.left - x) / zoom, y: (clientY - r.top - y) / zoom };
  }

  viewCenter() {
    if (!this.wrapEl) return { x: 200, y: 160 };
    const r = this.wrapEl.getBoundingClientRect();
    return this.toCanvas(r.left + (r.width || 800) / 2, r.top + (r.height || 600) / 2);
  }

  adjustZoom(factor, clientX = null, clientY = null) {
    const vp = this.canvas.viewport;
    const next = Math.max(M.ZOOM_MIN, Math.min(M.ZOOM_MAX, vp.zoom * factor));
    const r = this.wrapEl.getBoundingClientRect();
    const mx = clientX === null ? r.width / 2 : clientX - r.left;
    const my = clientY === null ? r.height / 2 : clientY - r.top;
    vp.x = mx - (mx - vp.x) * (next / vp.zoom);
    vp.y = my - (my - vp.y) * (next / vp.zoom);
    vp.zoom = next;
    this.applyTransform();
    this.renderWires();
    this.scheduleSave();
  }

  resetTransform() {
    this.canvas.viewport = { x: 0, y: 0, zoom: 1 };
    this.applyTransform();
    this.scheduleSave();
  }

  fitView(save = true) {
    const nodes = this.selected.size > 1 ? this.selectedNodes() : this.canvas.nodes;
    const r = this.wrapEl?.getBoundingClientRect();
    if (!nodes.length || !r?.width) return;
    this.canvas.viewport = M.fitViewport(M.boundsOf(nodes), r.width, r.height);
    this.applyTransform();
    if (save) this.scheduleSave();
  }

  centerOnNode(id) {
    const n = this.byId(id);
    const r = this.wrapEl?.getBoundingClientRect();
    if (!n || !r) return;
    const vp = this.canvas.viewport;
    vp.x = Math.round(r.width / 2 - (n.x + n.w / 2) * vp.zoom);
    vp.y = Math.round(r.height / 2 - (n.y + Math.min(n.h, 200) / 2) * vp.zoom);
    this.applyTransform();
    this.scheduleSave();
  }

  // ------------------------------------------------------------------ wires

  /** Canvas point of a struct field row's port, kept inside the node's visible rows. */
  fieldPoint(node, key, side) {
    const el = this.renderer?.element(node.id);
    const row = el?.querySelector(`.nm-sf[data-key="${cssEsc(key)}"]`);
    if (!row) return null;
    const z = this.zoom;
    const nr = el.getBoundingClientRect();
    const rr = row.getBoundingClientRect();
    const rows = row.closest(".nm-scroll")?.getBoundingClientRect() || nr;
    const cy = Math.min(Math.max(rr.top + rr.height / 2, rows.top + 4), rows.bottom - 4);
    return { x: side === "left" ? node.x : node.x + node.w, y: node.y + (cy - nr.top) / z };
  }

  endOf(node, side, field) {
    const point = field ? this.fieldPoint(node, field, side === "left" ? "left" : "right") : null;
    return { rect: M.rectOf(node), side: point ? (side === "left" ? "left" : "right") : side, point };
  }

  renderWires() {
    if (!this.wireSvg || !this.canvas) return;
    let s = "";
    const byId = new Map(this.canvas.nodes.map((n) => [n.id, n]));
    this.edgeGeoms = new Map();
    const ends = [];
    for (const e of this.canvas.edges) {
      const a = byId.get(e.from);
      const b = byId.get(e.to);
      if (!a || !b) continue;
      const fromEnd = this.endOf(a, e.fromSide, e.fromField);
      const toEnd = this.endOf(b, e.toSide, e.toField);
      const g = computeEdgeGeometry(fromEnd, toEnd, { curve: e.curve, arrow: e.arrow });
      this.edgeGeoms.set(e.id, g);
      ends.push([a.id, fromEnd.point ? e.fromField : null, g.fromSide], [b.id, toEnd.point ? e.toField : null, g.toSide]);
      const dash = dashArray(e.style);
      const sel = e.id === this.selectedEdge ? " selected" : "";
      s += `<g class="nm-edge${sel}" data-edge-id="${escapeHtml(e.id)}" style="--nm-edge:${M.accentVar(e.color, "var(--accent-blue)")}">
        <path class="nm-edge-hit" d="${g.d}"/>
        <path class="nm-edge-line" d="${g.d}"${dash ? ` stroke-dasharray="${dash}"` : ""}/>
        ${g.heads.map((h) => `<polygon class="nm-edge-head" points="${h}"/>`).join("")}
        ${e.label ? `<text class="nm-edge-label" x="${g.mid.x}" y="${g.mid.y - 6}" text-anchor="middle">${escapeHtml(e.label)}</text>` : ""}
      </g>`;
    }
    const p = this.gesture?.kind === "wire" ? this.gesture : null;
    // The layer is as large as what it draws (hit testing does not reach outside an svg's box)
    const b = M.boundsOf(this.canvas.nodes) || { x: 0, y: 0, w: 0, h: 0 };
    const pts = p ? [p.cur] : [];
    const x1 = Math.min(b.x, ...pts.map((q) => q.x)) - 400;
    const y1 = Math.min(b.y, ...pts.map((q) => q.y)) - 400;
    const x2 = Math.max(b.x + b.w, ...pts.map((q) => q.x)) + 400;
    const y2 = Math.max(b.y + b.h, ...pts.map((q) => q.y)) + 400;
    Object.assign(this.wireSvg.style, { left: `${x1}px`, top: `${y1}px`, width: `${x2 - x1}px`, height: `${y2 - y1}px` });
    this.wireSvg.setAttribute("viewBox", `${x1} ${y1} ${x2 - x1} ${y2 - y1}`);
    if (p) {
      const from = byId.get(p.from);
      if (from) {
        const end = { rect: { x: p.cur.x, y: p.cur.y, w: 0, h: 0 }, side: "auto", point: p.cur };
        const g = computeEdgeGeometry(this.endOf(from, p.side, p.field), end, { curve: "bezier", arrow: "forward" });
        s += `<g class="nm-edge nm-edge-pending"><path class="nm-edge-line" d="${g.d}" stroke-dasharray="5 4"/></g>`;
      }
    }
    this.wireSvg.innerHTML = s;
    this.tagConnectedPorts(ends);
  }

  /**
   * Mark the ports wires are attached to (`.nm-port-connected`: the only ports View Mode shows). `ends` is
   * `[nodeId, fieldKey|null, side]` per wire end: a struct field port, else the node's side port.
   */
  tagConnectedPorts(ends) {
    const next = new Set();
    for (const [id, field, side] of ends) {
      const el = this.renderer?.element?.(id);
      if (!el?.querySelector) continue;
      let port = null;
      if (field) port = el.querySelector(`.nm-port-field[data-field="${cssEsc(field)}"][data-side="${side === "left" ? "left" : "right"}"]`);
      if (!port) port = el.querySelector(`.nm-port-side[data-side="${cssEsc(side)}"]`);
      if (port) next.add(port);
    }
    for (const p of this.connectedPorts) if (!next.has(p)) p.classList?.remove("nm-port-connected");
    for (const p of next) p.classList?.add("nm-port-connected");
    this.connectedPorts = next;
  }

  // ------------------------------------------------------------------ pointer & keyboard

  attachCanvasEvents() {
    const wrap = this.wrapEl;
    wrap.addEventListener("pointerdown", (e) => this.onPointerDown(e));
    wrap.addEventListener("click", (e) => this.onClick(e));
    wrap.addEventListener("dblclick", (e) => this.onDblClick(e));
    wrap.addEventListener("wheel", (e) => this.onWheel(e), { passive: false });
    wrap.addEventListener("contextmenu", (e) => this.onContextMenu(e));
    // Scrolled struct rows move their field ports
    wrap.addEventListener("scroll", () => this.scheduleWires(), true);
    this.rootEl.addEventListener("keydown", (e) => this.onKeyDown(e));
    this.rootEl.addEventListener("keyup", (e) => {
      if (e.key === " ") this.spaceDown = false;
      if (String(e.key).startsWith("Arrow")) this.history.close();
    });
    this.inspectorEl.addEventListener("change", (e) => this.onInspectorInput(e, true));
    this.inspectorEl.addEventListener("input", (e) => this.onInspectorInput(e, false));
    this.inspectorEl.addEventListener("click", (e) => this.onInspectorClick(e));
    if (!this.onWinMove) {
      this.onWinMove = (e) => this.onPointerMove(e);
      this.onWinUp = (e) => this.onPointerUp(e);
      window.addEventListener("pointermove", this.onWinMove);
      window.addEventListener("pointerup", this.onWinUp);
      window.addEventListener("pointercancel", this.onWinUp);
      // Esc pressed inside an embedded tab (embed shell, same origin) gives the pointer back
      this.onMessage = (e) => {
        if (e.origin !== location.origin || e.data?.type !== "kip-embed-escape" || !this.active) return;
        const frame = this.renderer?.element(this.active)?.querySelector("iframe");
        if (frame && e.source === frame.contentWindow) {
          this.active = null;
          this.rootEl?.focus({ preventScroll: true });
          this.commit({ save: false });
        }
      };
      window.addEventListener("message", this.onMessage);
    }
  }

  scheduleWires() {
    if (this.wireFrame) return;
    this.wireFrame = requestAnimationFrame(() => {
      this.wireFrame = null;
      this.renderWires();
    });
  }

  onPointerDown(e) {
    const t = e.target;
    if (!t.closest(".nm-inline-edit")) this.rootEl.focus({ preventScroll: true });
    const start = { x: e.clientX, y: e.clientY };
    if (e.button === 1 || (e.button === 0 && this.spaceDown)) {
      e.preventDefault();
      this.gesture = { kind: "pan", start, vp: { ...this.canvas.viewport }, moved: false };
      return;
    }
    if (e.button !== 0 || t.closest(".nm-inline-edit, input, textarea, select")) return;
    const edgeEl = t.closest(".nm-edge[data-edge-id]");
    if (edgeEl) {
      this.selectEdge(edgeEl.dataset.edgeId);
      return;
    }
    const nodeEl = t.closest(".nm-node");
    // Dragging nodes or the canvas leaves no stale text selection behind (Ctrl+C copies nodes then)
    if (!nodeEl || (t.closest(".nm-node-head") && !t.closest("button"))) this.clearTextSelection();
    if (!nodeEl) {
      if (this.active) {
        this.active = null;
        this.commit({ save: false });
      }
      const additive = e.shiftKey || e.ctrlKey || e.metaKey;
      if (this.tool === "select" || additive) {
        const p = this.toCanvas(e.clientX, e.clientY);
        this.gesture = { kind: "marquee", start, origin: p, base: additive ? new Set(this.selected) : new Set(), moved: false };
      } else {
        this.gesture = { kind: "pan", start, vp: { ...this.canvas.viewport }, moved: false, clear: true };
      }
      return;
    }
    const node = this.byId(nodeEl.dataset.nodeId);
    if (!node) return;
    const rz = t.closest(".nm-rz");
    const port = t.closest(".nm-port");
    if (rz) {
      e.preventDefault();
      this.selectOnly(node.id, e);
      this.gesture = { kind: "resize", start, id: node.id, handle: rz.dataset.rz, rect: M.rectOf(node), moved: false };
    } else if (port) {
      e.preventDefault();
      e.stopPropagation();
      this.gesture = { kind: "wire", start, from: node.id, side: port.dataset.side || "auto", field: port.dataset.field || null, cur: this.toCanvas(e.clientX, e.clientY), moved: false };
      // Every port a wire can end on shows while it is dragged
      this.rootEl?.classList.add("nm-wiring");
    } else if (t.closest(".nm-node-head") && !t.closest("button")) {
      e.preventDefault();
      const toggle = e.shiftKey || e.ctrlKey || e.metaKey;
      if (toggle) {
        if (this.selected.has(node.id)) this.selected.delete(node.id);
        else this.selected.add(node.id);
        this.selectedEdge = null;
        this.commit({ save: false });
        if (!this.selected.has(node.id)) return;
      } else if (!this.selected.has(node.id)) {
        this.selectOnly(node.id);
      }
      const moving = M.dragSet(this.canvas.nodes, [...this.selected]);
      this.gesture = { kind: "drag", start, primary: node.id, nodes: moving.map((n) => ({ n, x: n.x, y: n.y })), moved: false };
    } else if (!t.closest("button, a")) {
      // Body: select, keep native text selection and scrolling
      if (!this.selected.has(node.id) || this.selected.size > 1) {
        if (e.shiftKey || e.ctrlKey || e.metaKey) this.selected.add(node.id);
        else this.selectOnly(node.id);
        this.commit({ save: false });
      }
    }
  }

  clearTextSelection() {
    try {
      const sel = globalThis.getSelection?.();
      if (sel && sel.toString() && this.rootEl?.contains(sel.anchorNode)) sel.removeAllRanges();
    } catch {
      // no selection API
    }
  }

  selectOnly(id, e = null) {
    if (e && (e.shiftKey || e.ctrlKey || e.metaKey)) this.selected.add(id);
    else this.selected = new Set([id]);
    this.selectedEdge = null;
    this.commit({ save: false });
  }

  selectEdge(id) {
    this.selectedEdge = id;
    this.selected.clear();
    this.commit({ save: false });
  }

  onPointerMove(e) {
    const g = this.gesture;
    if (!g) return;
    // The button came up where this window did not see it (over an iframe, outside the window)
    if (e.buttons === 0) return this.onPointerUp(e);
    const dxs = e.clientX - g.start.x;
    const dys = e.clientY - g.start.y;
    if (!g.moved && Math.hypot(dxs, dys) < DRAG_THRESHOLD) return;
    g.moved = true;
    const z = this.zoom;
    const snap = this.canvas.settings.snap ? this.canvas.settings.grid : 0;
    if (g.kind === "pan") {
      this.wrapEl.classList.add("nm-panning");
      this.canvas.viewport.x = g.vp.x + dxs;
      this.canvas.viewport.y = g.vp.y + dys;
      this.applyTransform();
    } else if (g.kind === "drag") {
      let dx = dxs / z;
      let dy = dys / z;
      const prim = g.nodes.find((m) => m.n.id === g.primary);
      if (snap && prim) {
        dx = M.snapValue(prim.x + dx, snap) - prim.x;
        dy = M.snapValue(prim.y + dy, snap) - prim.y;
      }
      for (const m of g.nodes) {
        m.n.x = Math.round(m.x + dx);
        m.n.y = Math.round(m.y + dy);
        const el = this.renderer.element(m.n.id);
        if (el) {
          el.style.left = `${m.n.x}px`;
          el.style.top = `${m.n.y}px`;
        }
      }
      this.scheduleWires();
    } else if (g.kind === "resize") {
      const node = this.byId(g.id);
      if (!node) return;
      // Auto-fitted nodes keep their aspect ratio: the drag scales their content
      if (node.autoFit) Object.assign(node, M.scaleResize(g.rect, g.handle, dxs / z, dys / z, node));
      else Object.assign(node, M.resizeRect(g.rect, g.handle, dxs / z, dys / z, M.SIZE_LIMITS[node.type], snap));
      const el = this.renderer.element(node.id);
      if (el) this.renderer.place(el, node);
      this.scheduleWires();
    } else if (g.kind === "wire") {
      g.cur = this.toCanvas(e.clientX, e.clientY);
      this.scheduleWires();
    } else if (g.kind === "marquee") {
      const p = this.toCanvas(e.clientX, e.clientY);
      const r = M.rectFromPoints(g.origin.x, g.origin.y, p.x, p.y);
      Object.assign(this.marqueeEl.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
      this.marqueeEl.hidden = false;
      this.selected = new Set([...g.base, ...M.nodesInMarquee(this.canvas.nodes, r)]);
      this.selectedEdge = null;
      this.renderer.sync(this.canvas.nodes, this.selected, this.active);
    }
  }

  onPointerUp(e) {
    const g = this.gesture;
    if (!g) return;
    this.gesture = null;
    this.wrapEl?.classList.remove("nm-panning");
    // A cancelled wire creates nothing; other gestures keep what they did so far
    if (e.type === "pointercancel" && g.kind === "wire") g.moved = false;
    if (g.kind === "pan") {
      if (!g.moved && g.clear) {
        this.selected.clear();
        this.selectedEdge = null;
        this.commit({ save: false });
      } else if (g.moved) this.scheduleSave();
    } else if (g.kind === "drag" && g.moved) {
      const movedIds = new Set(g.nodes.map((m) => m.n.id));
      const roots = g.nodes.map((m) => m.n).filter((n) => !(n.groupId && movedIds.has(n.groupId)));
      M.reassignGroups(this.canvas.nodes, roots);
      this.commit();
    } else if (g.kind === "resize" && g.moved) {
      const node = this.byId(g.id);
      if (node?.type === "group") M.reassignGroups(this.canvas.nodes, this.canvas.nodes.filter((n) => n.id !== node.id));
      this.commit();
    } else if (g.kind === "wire") {
      // Target ports stay hit-testable (.nm-wiring) until the drop point is read
      try {
        this.finishWire(g, e);
      } finally {
        this.rootEl?.classList.remove("nm-wiring");
      }
    } else if (g.kind === "marquee") {
      this.marqueeEl.hidden = true;
      if (!g.moved && !g.base.size) this.selected.clear();
      this.commit({ save: false });
    }
  }

  finishWire(g, e) {
    const hit = document.elementFromPoint(e.clientX, e.clientY);
    const nodeEl = hit?.closest?.(".nm-node");
    const port = hit?.closest?.(".nm-port");
    const toId = nodeEl?.dataset.nodeId;
    if (!g.moved || !toId || toId === g.from || !this.byId(toId)) {
      this.renderWires();
      return;
    }
    const edge = M.createEdge({
      from: g.from,
      to: toId,
      fromSide: g.field ? (g.side === "left" ? "left" : "right") : g.side,
      fromField: g.field,
      toSide: port?.dataset.side || "auto",
      toField: port?.dataset.field || null,
    });
    if (!edge) {
      this.renderWires();
      return;
    }
    this.canvas.edges.push(edge);
    this.selectedEdge = edge.id;
    this.selected.clear();
    this.commit();
  }

  onClick(e) {
    const btn = e.target.closest("[data-act]");
    const nodeEl = e.target.closest(".nm-node");
    if (!btn || !nodeEl) return;
    const node = this.byId(nodeEl.dataset.nodeId);
    if (!node) return;
    e.stopPropagation();
    const act = btn.dataset.act;
    if (EDIT_ACTS.has(act) && !this.canvas.settings.editMode) return;
    if (act === "interact") {
      this.active = this.active === node.id ? null : node.id;
      this.selected = new Set([node.id]);
      this.commit({ save: false });
      // The embedded tab takes the keyboard at once (Esc inside it gives the pointer back)
      if (this.active) {
        const frame = this.renderer?.element?.(node.id)?.querySelector?.("iframe");
        try {
          frame?.focus?.();
          frame?.contentWindow?.focus?.();
        } catch {
          // a frame still loading
        }
      }
    } else if (act === "toggle-header") this.setHeaderHidden(node.id, !node.hideHeader);
    else if (act === "open-tab") this.openNodeAsTab(node.id);
    else if (act === "refresh") this.refreshNode(node.id);
    else if (act === "expand") this.toggleExpandNode(node.id);
    else if (act === "mail-open") this.toggleMailMessage(node.id, btn.dataset.key);
    else if (act === "mail-spawn") this.spawnMailMessages(node.id, [btn.dataset.key]);
    else if (act === "mail-expand") this.expandThreadToNodes(node.id);
    else if (act === "kc-rel") this.expandKconfigRelations(node.id, btn.dataset.key);
    else if (act === "kc-expand") this.expandKconfigRelations(node.id);
    else if (node.type === "struct") this.onStructAction(node, act, btn.dataset.key);
  }

  // ------------------------------------------------------------------ mail and Kconfig nodes

  /** Show or hide a message's body excerpt in a mail node (the body is fetched once). */
  async toggleMailMessage(id, mid) {
    const node = this.byId(id);
    if (!node || node.type !== "mail" || !mid) return;
    const open = { ...node.data.openMids };
    if (open[mid]) delete open[mid];
    else open[mid] = true;
    node.data.openMids = open;
    if (node.autoFit) this.fitQueue.add(id);
    this.commit();
    if (!open[mid] || this.mailBodies.has(mid) || mid.startsWith("#")) return;
    try {
      const res = await (this.api || api).getMailMessage(mid);
      this.mailBodies.set(mid, String(res?.body || res?.text || ""));
    } catch (err) {
      this.mailBodies.set(mid, `(${err.message})`);
    }
    this.mailEpoch = (this.mailEpoch || 0) + 1;
    for (const n of this.canvas.nodes) if (n.type === "mail" && n.autoFit) this.fitQueue.add(n.id);
    if (this.containerEl?.isConnected || !this.containerEl) this.commit({ save: false, inspector: false });
  }

  /** The canvas node showing message `mid` on its own (a mail node focused on it), or null. */
  mailNodeOf(mid) {
    return this.canvas.nodes.find((n) => n.type === "mail" && n.data.mid === mid && n.data.single) || null;
  }

  /**
   * Message nodes for `mids` of thread node `id`, each connected to its parent message's node (or to the
   * thread node for a root) by a `reply` edge; messages already on the canvas are connected, not copied.
   * Returns the nodes created.
   */
  spawnMailMessages(id, mids, { select = true, layout = null } = {}) {
    const thread = this.byId(id);
    const model = thread ? this.modelOf(thread) : null;
    if (!model) return [];
    const rows = new Map(model.rows.map((r) => [r.mid, r]));
    const created = [];
    const place = layout ? new Map(layout.map((p) => [p.mid, p])) : null;
    let k = 0;
    for (const mid of mids) {
      const r = rows.get(mid);
      if (!r) continue;
      let n = this.mailNodeOf(mid);
      if (!n) {
        const at = place?.get(mid) || { x: thread.x + thread.w + 80, y: thread.y + k * 140 };
        n = M.createNode("mail", {
          x: at.x, y: at.y, w: 320, h: 150, version: thread.version, repo: thread.repo,
          data: { tid: thread.data.tid, mid, subject: r.subject, single: true },
        });
        this.canvas.nodes.push(n);
        created.push(n);
        k += 1;
      }
      const parentNode = (r.parent && this.mailNodeOf(r.parent)) || thread;
      if (parentNode.id !== n.id && !this.canvas.edges.some((e) => e.from === parentNode.id && e.to === n.id)) {
        const edge = M.createEdge({ from: parentNode.id, to: n.id, label: "reply" });
        if (edge) this.canvas.edges.push(edge);
      }
    }
    if (created.length) M.reassignGroups(this.canvas.nodes, created);
    if (select && created.length) {
      this.selected = new Set(created.map((x) => x.id));
      this.selectedEdge = null;
    }
    this.commit();
    return created;
  }

  /** "Expand Thread to Nodes": every message of a thread node as a connected node, laid out as the reply tree. */
  expandThreadToNodes(id) {
    const thread = this.byId(id);
    const model = thread ? this.modelOf(thread) : null;
    if (!model?.rows.length) return toast.info("The thread has not loaded yet"), [];
    const rows = model.rows.slice(0, MAIL_EXPAND_MAX);
    const layout = mailThreadLayout({ rows }, { x: thread.x + thread.w + 80, y: thread.y }, { w: 320, h: 150 });
    const created = this.spawnMailMessages(id, rows.map((r) => r.mid), { layout });
    if (model.rows.length > rows.length) toast.info(`Added the first ${rows.length} of ${model.rows.length} messages`);
    return created;
  }

  /**
   * Link a Kconfig node to its related symbols (`only` = `rel:NAME` for one chip): a symbol already on
   * the canvas is connected, any other gets a node of its own (outgoing relations to the right, incoming
   * to the left); each edge is labeled with the relation and drawn once.
   */
  expandKconfigRelations(id, only = "") {
    const node = this.byId(id);
    const model = node ? this.modelOf(node) : null;
    if (!model) return toast.info("The symbol has not loaded yet"), [];
    const targets = relationTargets(model, only).slice(0, only ? 1 : KC_EXPAND_MAX);
    const created = [];
    const count = { out: 0, in: 0 };
    for (const t of targets) {
      let other = this.canvas.nodes.find((n) => n.type === "kconfig" && n.data.symbol === t.name && n.data.arch === node.data.arch && n.version === node.version);
      if (!other) {
        const k = count[t.dir]++;
        const lim = M.SIZE_LIMITS.kconfig;
        other = M.createNode("kconfig", {
          x: t.dir === "out" ? node.x + node.w + 120 : node.x - lim.w - 120,
          y: node.y + k * (lim.h * 0.6 + 24),
          h: Math.round(lim.h * 0.6),
          version: node.version,
          repo: node.repo,
          data: { symbol: t.name, arch: node.data.arch },
        });
        this.canvas.nodes.push(other);
        created.push(other);
      }
      const [from, to] = t.dir === "out" ? [node.id, other.id] : [other.id, node.id];
      if (!this.canvas.edges.some((e) => e.from === from && e.to === to && e.label === t.edge)) {
        const edge = M.createEdge({ from, to, label: t.edge, style: t.edge === "depends on" ? "solid" : "dashed" });
        if (edge) this.canvas.edges.push(edge);
      }
    }
    if (created.length) {
      M.reassignGroups(this.canvas.nodes, created);
      this.selected = new Set(created.map((x) => x.id));
      this.selectedEdge = null;
    }
    this.commit();
    return created;
  }

  onStructAction(node, act, key) {
    const d = node.data;
    if (act === "all-types") M.setAllTypes(d, !d.showTypes);
    else if (act === "guards") d.showGuards = !d.showGuards;
    else if (act === "collapse" && key) {
      if (d.collapsed[key]) delete d.collapsed[key];
      else d.collapsed[key] = true;
    } else if (act === "toggle-type" && key) {
      const layout = this.content.get(this.fetchKeyOf(node))?.value;
      const item = layout ? findItem(layout.items, key) : null;
      if (item) M.toggleItemType(d, item);
    } else if (act === "guard-fold" && key) {
      const layout = this.content.get(this.fetchKeyOf(node))?.value;
      M.toggleGuardCollapse(d, key, guardAutoCollapsed(layout, key, this.evaluatorFor(node)));
    } else if (act === "hide-item" && key) {
      M.toggleHidden(d, key);
    } else if (act === "show-hidden") {
      M.showAllHidden(d);
    } else if (act === "style") {
      d.style = d.style === "table" ? "list" : "table";
    } else return;
    // Folding, showing or hiding rows changes an auto-fitted node's height: it measures again
    if (node.autoFit) this.fitQueue.add(node.id);
    this.commit();
  }

  setEditMode(on) {
    this.canvas.settings.editMode = Boolean(on);
    // Headers hidden in View Mode change the size of auto-fitted nodes: they measure again (view state,
    // not an undo step)
    for (const n of this.canvas.nodes) if (n.autoFit && n.hideHeader) this.fitQueue.add(n.id);
    this.commit({ absorb: true });
  }

  /** Hide (View Mode) or restore a node's header. */
  setHeaderHidden(id, hidden) {
    const node = this.byId(id);
    if (!node) return;
    node.hideHeader = Boolean(hidden);
    if (node.autoFit) this.fitQueue.add(id);
    this.commit();
  }

  onDblClick(e) {
    if (isTyping(e.target)) return;
    const titleEl = e.target.closest(".nm-title");
    const nodeEl = e.target.closest(".nm-node");
    const edgeEl = e.target.closest(".nm-edge[data-edge-id]");
    if (nodeEl && !this.canvas.settings.editMode) {
      if ((titleEl || e.target.closest(".nm-type-note .nm-node-body")) && !this.hintedEditMode) {
        this.hintedEditMode = true;
        toast.info("Turn on Edit Mode in the Inspector to rename nodes and edit notes in place");
      }
      return;
    }
    if (titleEl && nodeEl) return this.beginTitleEdit(nodeEl.dataset.nodeId);
    if (nodeEl) {
      const node = this.byId(nodeEl.dataset.nodeId);
      if (node?.type === "note" && e.target.closest(".nm-node-body")) return this.beginNoteEdit(node.id);
      if (node?.type === "group" && e.target.closest(".nm-node-head")) return this.beginTitleEdit(node.id);
      return;
    }
    if (edgeEl) return this.beginEdgeLabelEdit(edgeEl.dataset.edgeId);
    const p = this.toCanvas(e.clientX, e.clientY);
    this.addNoteAt(Math.round(p.x - 110), Math.round(p.y - 20));
  }

  onWheel(e) {
    const inContent = e.target.closest(".nm-scroll");
    if (inContent && !e.ctrlKey && !e.metaKey) return; // native scroll inside node content
    e.preventDefault();
    const factor = Math.exp(-Math.max(-60, Math.min(60, e.deltaY)) * (e.ctrlKey ? 0.01 : 0.0025));
    this.adjustZoom(factor, e.clientX, e.clientY);
  }

  onKeyDown(e) {
    if (isTyping(e.target)) return;
    // A focused button keeps Space/Enter, and Delete there is not meant for the selection
    if (e.target.closest?.("button") && [" ", "Enter", "Delete", "Backspace"].includes(e.key)) return;
    const mod = e.ctrlKey || e.metaKey;
    const key = String(e.key || "").toLowerCase();
    if (mod && !e.altKey && (key === "z" || key === "y")) {
      // Text fields keep their own undo (isTyping above returned already)
      e.preventDefault();
      if (key === "y" || e.shiftKey) this.redo();
      else this.undo();
    } else if (e.key === " ") {
      this.spaceDown = true;
      e.preventDefault();
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      this.deleteSelection();
    } else if (mod && (e.key === "c" || e.key === "C") && !e.shiftKey && !e.altKey) {
      // Selected text (code, a note, a struct row) copies natively
      if (hasTextSelection(this.rootEl) || !this.selected.size) return;
      e.preventDefault();
      this.copySelection();
    } else if (mod && (e.key === "v" || e.key === "V") && !e.shiftKey && !e.altKey) {
      if (hasTextSelection(this.rootEl)) return;
      if (this.pasteClipboard()) e.preventDefault();
    } else if (mod && (e.key === "d" || e.key === "D") && !e.shiftKey && !e.altKey) {
      if (!this.selected.size) return;
      e.preventDefault();
      this.duplicateSelection();
    } else if (mod && (e.key === "a" || e.key === "A")) {
      e.preventDefault();
      this.selected = new Set(this.canvas.nodes.map((n) => n.id));
      this.selectedEdge = null;
      this.commit({ save: false });
    } else if (e.key === "Escape") {
      this.active = null;
      this.selected.clear();
      this.selectedEdge = null;
      this.commit({ save: false });
    } else if (e.key.startsWith("Arrow") && this.selected.size) {
      e.preventDefault();
      const step = e.shiftKey ? (this.canvas.settings.grid || 10) : 1;
      const dx = e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0;
      const dy = e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0;
      const moved = M.dragSet(this.canvas.nodes, [...this.selected]);
      for (const n of moved) {
        n.x += dx;
        n.y += dy;
      }
      const ids = new Set(moved.map((n) => n.id));
      M.reassignGroups(this.canvas.nodes, moved.filter((n) => !(n.groupId && ids.has(n.groupId))));
      // Held or repeated arrow keys are one undo step (keyup closes it)
      this.commit({ coalesce: `arrow:${[...this.selected].join(",")}` });
    }
  }

  onContextMenu(e) {
    e.preventDefault();
    e.stopPropagation();
    const nodeEl = e.target.closest(".nm-node");
    const edgeEl = e.target.closest(".nm-edge[data-edge-id]");
    if (nodeEl) {
      const node = this.byId(nodeEl.dataset.nodeId);
      if (!node) return;
      if (!this.selected.has(node.id)) this.selectOnly(node.id);
      const row = e.target.closest(".nm-sf[data-key]");
      contextMenu.showNodeMapMenu(e.clientX, e.clientY, { node, fieldKey: row?.dataset.key || null, controller: this });
    } else if (edgeEl) {
      this.selectEdge(edgeEl.dataset.edgeId);
      contextMenu.showNodeMapMenu(e.clientX, e.clientY, { edgeId: edgeEl.dataset.edgeId, controller: this });
    } else {
      const at = this.toCanvas(e.clientX, e.clientY);
      contextMenu.showNodeMapMenu(e.clientX, e.clientY, { isCanvas: true, at, controller: this });
    }
  }

  // ------------------------------------------------------------------ inline editing

  inlineInput(host, value, { multiline = false, onCommit, onDone = null }) {
    const input = document.createElement(multiline ? "textarea" : "input");
    input.className = `nm-inline-edit${multiline ? " nm-inline-multi" : ""}`;
    input.value = value;
    host.appendChild(input);
    input.focus();
    if (!multiline) input.select();
    let done = false;
    const finish = (commit, refocus = false) => {
      if (done) return;
      done = true;
      const v = input.value;
      input.remove();
      onDone?.();
      if (commit) onCommit(v);
      else this.commit({ save: false });
      // Only a keyboard finish takes focus back: a blur went where the user clicked
      if (refocus) this.rootEl?.focus({ preventScroll: true });
    };
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Escape") finish(multiline, true);
      else if (e.key === "Enter" && (!multiline || e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        finish(true, true);
      }
    });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("pointerdown", (e) => e.stopPropagation());
    return input;
  }

  /** Inline title edit; an empty title resets to the default one. */
  beginTitleEdit(id) {
    const node = this.byId(id);
    const titleEl = this.renderer?.element(id)?.querySelector(".nm-title");
    if (!node || !titleEl) return;
    titleEl.textContent = "";
    this.inlineInput(titleEl, M.displayTitle(node), {
      onCommit: (v) => this.setTitle(id, v),
    });
  }

  setTitle(id, value) {
    const node = this.byId(id);
    if (!node) return;
    const v = String(value || "").trim().slice(0, 512);
    node.customTitle = v && v !== node.title ? v : "";
    const el = this.renderer.element(id)?.querySelector(".nm-title");
    if (el) el.textContent = M.displayTitle(node);
    this.commit();
  }

  beginNoteEdit(id) {
    const node = this.byId(id);
    const body = this.renderer?.element(id)?.querySelector(".nm-node-body");
    if (!node || !body) return;
    body.innerHTML = "";
    this.inlineInput(body, node.data.text || "", {
      multiline: true,
      onCommit: (v) => {
        node.data.text = v.slice(0, 20000);
        this.renderer.invalidate(id);
        this.commit();
      },
    });
  }

  beginEdgeLabelEdit(id) {
    const edge = this.canvas.edges.find((x) => x.id === id);
    const g = this.edgeGeoms?.get(id);
    if (!edge || !g) return;
    const host = document.createElement("div");
    host.className = "nm-edge-edit";
    Object.assign(host.style, { left: `${g.mid.x}px`, top: `${g.mid.y}px` });
    this.layerEl.appendChild(host);
    this.inlineInput(host, edge.label, {
      onDone: () => host.remove(),
      onCommit: (v) => {
        edge.label = String(v || "").trim().slice(0, 512);
        this.commit();
      },
    });
  }

  // ------------------------------------------------------------------ actions

  deleteSelection() {
    if (this.selectedEdge) {
      this.canvas.edges = this.canvas.edges.filter((e) => e.id !== this.selectedEdge);
      this.selectedEdge = null;
    } else if (this.selected.size) {
      M.removeNodes(this.canvas, [...this.selected]);
      this.selected.clear();
    } else return;
    this.commit();
  }

  removeNode(id) {
    M.removeNodes(this.canvas, [id]);
    this.selected.delete(id);
    this.commit();
  }

  removeEdge(id) {
    this.canvas.edges = this.canvas.edges.filter((e) => e.id !== id);
    this.commit();
  }

  clearAll() {
    this.canvas.nodes = [];
    this.canvas.edges = [];
    this.selected.clear();
    this.selectedEdge = null;
    this.commit();
  }

  /** Accent of the selected edge or nodes: a preset id or a custom `#rrggbb`. */
  setColor(color, { inspector = true, coalesce = null, coalesceEnd = false } = {}) {
    const c = M.normalizeColor(color);
    if (this.selectedEdge) {
      const e = this.canvas.edges.find((x) => x.id === this.selectedEdge);
      if (e) e.color = c;
    } else for (const n of this.selectedNodes()) n.color = c;
    this.commit({ inspector, coalesce, coalesceEnd });
  }

  /** Background of the selected nodes: `""` (theme), a preset id or a custom `#rrggbb`. */
  setBackground(color, { inspector = true, coalesce = null, coalesceEnd = false } = {}) {
    const c = M.normalizeColor(color);
    for (const n of this.selectedNodes()) n.bgColor = c;
    this.commit({ inspector, coalesce, coalesceEnd });
  }

  /** Canvas background (`settings.bgColor`). */
  setCanvasBackground(color, { inspector = true, coalesce = null, coalesceEnd = false } = {}) {
    this.canvas.settings.bgColor = M.normalizeColor(color);
    this.commit({ inspector, coalesce, coalesceEnd });
  }

  /** Add cloned nodes and edges (cloneSubgraph output), re-derive their groups and select them. */
  insertClones({ nodes, edges }) {
    if (!nodes.length) return [];
    const ids = new Set(nodes.map((n) => n.id));
    this.canvas.nodes.push(...nodes);
    this.canvas.edges.push(...edges);
    // Copies outside a copied group join whatever group they land in
    M.reassignGroups(this.canvas.nodes, nodes.filter((n) => !(n.groupId && ids.has(n.groupId))));
    this.selected = new Set(nodes.filter((n) => !(n.groupId && ids.has(n.groupId))).map((n) => n.id));
    this.selectedEdge = null;
    this.commit();
    return nodes;
  }

  /** Duplicate the selection next to itself (groups with their members, edges between copied nodes). */
  duplicateSelection(ids = [...this.selected]) {
    if (!ids.length) return [];
    return this.insertClones(M.cloneSubgraph(this.canvas, ids, { x: PASTE_STEP, y: PASTE_STEP }));
  }

  duplicateNode(id) {
    if (!this.selected.has(id)) this.selected = new Set([id]);
    return this.duplicateSelection();
  }

  /** Copy the selection to the NodeMap clipboard (localStorage, shared by every NodeMap tab). */
  copySelection() {
    const ids = [...this.selected];
    if (!ids.length) return null;
    const payload = M.clipboardPayload(this.canvas, ids, this.tab?.id || "");
    memoryClipboard = payload;
    try {
      globalThis.localStorage.setItem(M.CLIPBOARD_KEY, JSON.stringify(payload));
    } catch {
      // Storage refused it: this window's memory copy is the clipboard, an older stored one must not win
      try {
        globalThis.localStorage?.removeItem(M.CLIPBOARD_KEY);
      } catch {
        // nothing stored
      }
    }
    this.pasteStamp = payload.stamp;
    this.pasteCount = 0;
    toast.info(`Copied ${payload.nodes.length} node${payload.nodes.length === 1 ? "" : "s"} (Ctrl+V pastes them in any NodeMap tab)`);
    return payload;
  }

  readClipboard() {
    let raw = null;
    try {
      const stored = globalThis.localStorage?.getItem(M.CLIPBOARD_KEY);
      raw = stored ? JSON.parse(stored) : null;
    } catch {
      raw = null;
    }
    return M.normalizeClipboard(raw) || M.normalizeClipboard(memoryClipboard);
  }

  /**
   * Paste the clipboard: copied from this canvas, offset from the originals (cascading on every paste);
   * from another tab, centered in the view. False when the clipboard is empty.
   */
  pasteClipboard() {
    const clip = this.readClipboard();
    if (!clip) return false;
    if (clip.stamp !== this.pasteStamp) {
      this.pasteStamp = clip.stamp;
      this.pasteCount = 0;
    }
    this.pasteCount += 1;
    const step = PASTE_STEP * this.pasteCount;
    let offset = { x: step, y: step };
    if (clip.origin !== (this.tab?.id || "")) {
      const b = M.boundsOf(clip.nodes);
      const c = this.viewCenter();
      offset = { x: Math.round(c.x - (b.x + b.w / 2)) + step - PASTE_STEP, y: Math.round(c.y - (b.y + b.h / 2)) + step - PASTE_STEP };
    }
    const clones = M.cloneSubgraph({ nodes: clip.nodes, edges: clip.edges }, clip.nodes.map((n) => n.id), offset);
    this.insertClones(clones);
    return true;
  }

  align(mode) {
    M.alignNodes(this.selectedNodes(), mode);
    this.commit();
  }

  distribute(axis) {
    M.distributeNodes(this.selectedNodes(), axis);
    this.commit();
  }

  refreshNode(id) {
    const node = this.byId(id);
    if (!node) return;
    const key = this.fetchKeyOf(node);
    if (key) this.content.delete(key);
    this.fresh = new Set([...(this.fresh || []), id]);
    this.renderer.invalidate(id);
    this.commit({ save: false });
  }

  /** The workspace tab a node opens as (`tabForNode()` with its loaded content). */
  tabOfNode(node) {
    const entry = this.content.get(this.fetchKeyOf(node));
    return tabForNode(node, entry?.status === "ok" ? entry.value : null, { repo: this.repo, version: this.currentVersion });
  }

  openNodeAsTab(id) {
    const node = this.byId(id);
    if (!node) return;
    // A tab node opens its own address (every field of the embedded view)
    if (node.type === "tab" && node.data.hash) return urlSync.applyAddress(node.data.hash);
    const tab = this.tabOfNode(node);
    if (tab) state.openTab(tab);
  }

  /** Workspace sessions a node can be copied to: every one but the session this window holds. */
  copyTargets() {
    let list = [];
    try {
      list = (this.sessions || sessionManager).getSessionsIndex() || [];
    } catch {
      list = [];
    }
    const current = state.currentSessionId || sessionManager.activeSessionId || "";
    return (Array.isArray(list) ? list : []).filter((x) => x && x.id && x.id !== current);
  }

  /**
   * Copy a node as a tab into another workspace session (the node stays): the window holding that
   * session takes it live; when none answers, the tab is written into the session's stored record.
   */
  copyNodeToSession(id, sessionId) {
    const node = this.byId(id);
    const target = this.copyTargets().find((x) => x.id === sessionId);
    if (!node || !target) return false;
    const tab = this.tabOfNode(node);
    if (!tab) {
      toast.warning("This node has no tab to copy");
      return false;
    }
    const sm = this.sessions || sessionManager;
    const tabData = { ...tab, id: `tab-${Math.random().toString(36).slice(2, 9)}` };
    const payload = {
      tabId: tabData.id,
      sourcePaneId: null,
      sourceWinId: sm.getWindowId?.() || "",
      sourceSessionId: state.currentSessionId || "",
      sourceSessionName: state.currentSessionName || "",
      tabData,
    };
    const name = target.name || target.id;
    let taken = false;
    const off = sm.subscribe?.("tab:transfer:accepted", (msg) => {
      if (msg?.tabId === tabData.id) taken = true;
    });
    sm.broadcastTabPush?.(payload, target.id);
    setTimeout(() => {
      if (typeof off === "function") off();
      if (taken) return toast.success(`Copied '${M.displayTitle(node)}' to session '${name}'`);
      const stored = sm.pushTabToStoredSession?.(target.id, payload);
      if (stored) toast.success(`Copied '${M.displayTitle(node)}' to session '${name}' (opens there with it)`);
      else toast.error(`'${M.displayTitle(node)}' could not be copied to session '${name}'`);
    }, PUSH_ACK_GRACE_MS);
    return true;
  }

  // ------------------------------------------------------------------ tabs dragged in

  /** Whether a drag over the canvas may carry a tab of a tab strip (`utils/tab_drag.js`). */
  acceptsTabDrag(e) {
    return mayCarryTab(e?.dataTransfer, sessionManager.getIncomingTabDrag?.() || null);
  }

  /**
   * A tab dropped on the canvas is copied in (the tab stays open where it was): the node that shows it
   * natively, or with Shift a live tab node, centered on the drop point. Returns the nodes added.
   */
  dropTab(payload, clientX, clientY, { live = false } = {}) {
    const tab = payload?.tabData;
    if (!tab) return [];
    const specs = nodeSpecsForTab(tab, (t) => urlSync.paramsForTab(t).toString(), { live });
    if (!specs.length) {
      toast.info(tab.type === "nodemap" ? "A NodeMap canvas cannot hold another canvas" : "This tab cannot be shown on the canvas");
      return [];
    }
    const p = this.toCanvas(clientX, clientY);
    const added = [];
    specs.forEach((spec, i) => {
      const node = this.nodeFromSpec(spec);
      if (!node) return;
      node.x = Math.round(p.x - node.w / 2 + i * PASTE_STEP);
      node.y = Math.round(p.y - node.h / 2 + i * PASTE_STEP);
      if (this.canvas.settings.snap) {
        node.x = M.snapValue(node.x, this.canvas.settings.grid);
        node.y = M.snapValue(node.y, this.canvas.settings.grid);
      }
      this.canvas.nodes.push(node);
      added.push(node);
    });
    if (!added.length) return [];
    M.reassignGroups(this.canvas.nodes, added.filter((n) => n.type !== "group"));
    this.selected = new Set(added.map((n) => n.id));
    this.selectedEdge = null;
    this.commit();
    // A drag from another window: that window keeps its tab (no drag-out elevation there)
    if (payload.sourceWinId) sessionManager.broadcastTabDropCopied?.({ tabId: payload.tabId || tab.id, sourceWinId: payload.sourceWinId });
    toast.success(added[0].type === "tab" ? `Embedded '${tab.title || tab.type}' as a live tab` : `Copied '${tab.title || tab.type}' onto the canvas`);
    return added;
  }

  attachTabDrop() {
    const wrap = this.wrapEl;
    const off = () => wrap.classList.remove("nm-drop-target");
    wrap.addEventListener("dragover", (e) => {
      if (!this.acceptsTabDrag(e)) return;
      e.preventDefault();
      // The pane under the canvas must not take this as a tab move into its strip
      e.stopPropagation();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      wrap.classList.add("nm-drop-target");
    });
    wrap.addEventListener("dragleave", (e) => {
      if (!wrap.contains(e.relatedTarget)) off();
    });
    wrap.addEventListener("drop", (e) => {
      off();
      const payload = tabDragPayload(e.dataTransfer, sessionManager.getIncomingTabDrag?.() || null);
      if (!payload) return;
      e.preventDefault();
      e.stopPropagation();
      this.dropTab(payload, e.clientX, e.clientY, { live: e.shiftKey });
    });
  }

  async toggleExpandNode(id) {
    const node = this.byId(id);
    if (!node || node.type !== "symbol" || this.expanding?.has(id)) return;
    const d = node.data;
    d.expanded = !d.expanded;
    this.expanding = this.expanding || new Set();
    this.expanding.add(id);
    if (d.expanded && !d.constituents.length) {
      const client = this.clientFor(node);
      try {
        let astId = d.astId;
        if (!astId) {
          const detail = await client.getSymbolDetail(node.version, d.name);
          astId = detail?.definition?.ast_id || null;
        }
        if (astId) {
          const res = await client.getAstTree(node.version, astId);
          const children = res.node?.children || res.children || [];
          d.constituents = children.map((c) => ({ name: c.spelling || c.name || c.type_name || "" })).filter((c) => c.name);
        }
      } catch (err) {
        toast.warning(`No members for ${d.name}: ${err.message}`);
      }
    }
    this.expanding.delete(id);
    this.commit();
  }

  /** A node attached to `fromId` (optionally from a struct field), placed to its right. */
  promptAttachNode(fromId, fieldKey = null) {
    const from = this.byId(fromId);
    if (!from) return;
    const name = prompt(`Symbol or "struct name" to attach to "${M.displayTitle(from)}":`, "");
    if (!name || !name.trim()) return;
    const n = this.nodeFromLegacySymbol({ name: name.trim(), type: /^(struct|union)\s/.test(name.trim()) ? "struct" : "symbol" });
    if (!n) return;
    n.x = from.x + from.w + 100;
    n.y = from.y;
    this.canvas.nodes.push(n);
    const edge = M.createEdge({ from: from.id, to: n.id, fromField: fieldKey, fromSide: fieldKey ? "right" : "auto" });
    if (edge) this.canvas.edges.push(edge);
    this.selected = new Set([n.id]);
    this.commit();
  }

  autoLayoutDAG() {
    const nodes = this.canvas.nodes.filter((n) => n.type !== "group");
    if (!nodes.length) return;
    const layer = new Map(nodes.map((n) => [n.id, 0]));
    for (let iter = 0; iter < nodes.length; iter++) {
      let changed = false;
      for (const e of this.canvas.edges) {
        if (!layer.has(e.from) || !layer.has(e.to)) continue;
        if (layer.get(e.to) <= layer.get(e.from) && layer.get(e.from) < nodes.length) {
          layer.set(e.to, layer.get(e.from) + 1);
          changed = true;
        }
      }
      if (!changed) break;
    }
    const cols = new Map();
    for (const n of nodes) {
      const l = layer.get(n.id);
      if (!cols.has(l)) cols.set(l, []);
      cols.get(l).push(n);
    }
    let x = 80;
    for (const l of [...cols.keys()].sort((a, b) => a - b)) {
      let y = 80;
      const col = cols.get(l);
      for (const n of col) {
        n.x = x;
        n.y = y;
        y += n.h + 40;
      }
      x += Math.max(...col.map((n) => n.w)) + 100;
    }
    M.reassignGroups(this.canvas.nodes, nodes);
    this.commit();
    this.fitView();
    toast.success(`Arranged ${nodes.length} nodes by their connections`);
  }

  // ------------------------------------------------------------------ inspector

  /** `#rrggbb` a color input starts at: the custom color, else what the accent resolves to. */
  pickerValue(color, fallback = "var(--accent-blue)") {
    if (M.isCustomColor(color)) return color.toLowerCase();
    let hex = null;
    try {
      hex = M.colorToHex(resolveColor(M.accentVar(color, fallback)));
      if (!hex && typeof document !== "undefined") {
        // Named or modern color syntax: let the 2D context normalize it
        const ctx = document.createElement("canvas").getContext?.("2d");
        if (ctx) {
          ctx.fillStyle = resolveColor(M.accentVar(color, fallback));
          hex = M.colorToHex(ctx.fillStyle);
        }
      }
    } catch {
      hex = null;
    }
    return hex || `#${"0".repeat(6)}`;
  }

  renderInspector() {
    if (!this.inspectorEl) return;
    // Accent swatches (`act` "color"), or background swatches over the theme token `bg` (`act` "bg" for
    // the selected nodes, "canvas-bg" for the canvas): presets show as the tint they apply
    const swatches = (current, fallback = "var(--accent-blue)", { act = "color", bg = null } = {}) => {
      const custom = M.isCustomColor(current);
      const label = bg ? "Background" : "Accent";
      return `<div class="nm-swatches" role="group" aria-label="${label}">${M.ACCENTS.map((a) =>
        `<button type="button" class="nm-swatch${a.id === current ? " on" : ""}" data-insp="${act}" data-value="${a.id}" title="${a.id || !bg ? a.label : "Theme default"}" style="--nm-sw:${bg ? M.bgVar(a.id, bg) : M.accentVar(a.id, "var(--bg-tertiary)")}"></button>`).join("")}` +
        `<label class="nm-swatch nm-swatch-custom${custom ? " on" : ""}" title="Custom color…" style="--nm-sw:${custom ? escapeHtml(current) : "var(--bg-tertiary)"}"><input type="color" data-insp="${act}-custom" value="${escapeHtml(this.pickerValue(current, fallback))}" aria-label="Custom ${label.toLowerCase()} color"/></label></div>`;
    };
    const bgRow = (label, current, base, act) => `<div class="inspector-field"><span class="inspector-label">${escapeHtml(label)}</span><div class="nm-insp-inline">${swatches(current, base, { act, bg: base })}${current ? `<button type="button" class="nodemap-btn" data-insp="${act}-reset" title="Back to the theme background">Reset</button>` : ""}</div></div>`;
    const sel = (name, value, options, labels = null) => `<select data-insp="${name}">${options.map((o, i) => `<option value="${escapeHtml(o)}"${o === value ? " selected" : ""}>${escapeHtml(labels ? labels[i] : (o || "any"))}</option>`).join("")}</select>`;
    const row = (label, inner) => `<label class="inspector-field"><span class="inspector-label">${escapeHtml(label)}</span>${inner}</label>`;
    const input = (name, value, extra = "") => `<input data-insp="${name}" value="${escapeHtml(value ?? "")}" ${extra}/>`;
    const check = (name, on, label, title = "") => `<label class="inspector-field nm-insp-check"${title ? ` title="${escapeHtml(title)}"` : ""}><input type="checkbox" data-insp="${name}" ${on ? "checked" : ""}/> <span>${escapeHtml(label)}</span></label>`;
    const st = this.canvas.settings;
    const em = Boolean(st.editMode);
    // Content scale of the given non-group nodes: slider, percent and a reset to 100%
    const scaleRow = (list) => {
      const sc = new Set(list.map((x) => x.scale || 1));
      const cur = sc.size === 1 ? [...sc][0] : 1;
      const pct = Math.round(cur * 100);
      const one = list.length === 1 ? list[0] : null;
      const note = one?.autoFit ? ` of ${one.baseW}×${one.baseH}` : sc.size > 1 ? " (mixed)" : "";
      return `<div class="inspector-field"><span class="inspector-label">Content scale${list.length > 1 ? ` (${list.length})` : ""}</span><div class="nm-insp-inline nm-insp-scale">` +
        `<input type="range" data-insp="content-scale" min="${M.SCALE_MIN * 100}" max="${M.SCALE_MAX * 100}" step="5" value="${pct}" aria-label="Content scale"/>` +
        `<input type="number" data-insp="content-scale" min="${M.SCALE_MIN * 100}" max="${M.SCALE_MAX * 100}" step="5" value="${pct}" aria-label="Content scale in percent"/><span class="inspector-val">%${escapeHtml(note)}</span>` +
        `${sc.size > 1 || cur !== 1 ? '<button type="button" class="nodemap-btn" data-insp="scale-reset" title="Back to 100%">100%</button>' : ""}</div></div>`;
    };
    let html = `<div class="nm-insp-mode"><button type="button" class="nodemap-btn nm-mode-toggle" data-insp="edit-mode" aria-pressed="${em}" title="${em ? "Hide the node editing controls" : "Show the node editing controls: struct type, #ifdef and hide toggles, inline title and note editing"}">✎ Edit Mode: ${em ? "On" : "Off"}</button></div>`;
    const edge = this.selectedEdge ? this.canvas.edges.find((e) => e.id === this.selectedEdge) : null;
    const nodes = this.selectedNodes();
    if (edge) {
      const a = this.byId(edge.from);
      const b = this.byId(edge.to);
      html += `<div class="inspector-field"><span class="inspector-label">Connection</span><span class="inspector-val">${escapeHtml(a ? M.displayTitle(a) : "?")} → ${escapeHtml(b ? M.displayTitle(b) : "?")}</span></div>`;
      html += row("Label", input("edge-label", edge.label, 'placeholder="(none)"'));
      html += `<div class="nm-insp-pair">${row("From side", sel("edge-fromSide", edge.fromSide, M.SIDES))}${row("To side", sel("edge-toSide", edge.toSide, M.SIDES))}</div>`;
      html += `<div class="nm-insp-pair">${row("Arrows", sel("edge-arrow", edge.arrow, M.ARROWS))}${row("Line", sel("edge-style", edge.style, M.LINE_STYLES))}</div>`;
      html += row("Route", sel("edge-curve", edge.curve, M.CURVES));
      html += `<div class="inspector-field"><span class="inspector-label">Color</span>${swatches(edge.color)}</div>`;
      html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="edge-reverse">⇄ Reverse</button><button type="button" class="nodemap-btn nm-danger" data-insp="delete">Delete</button></div>`;
    } else if (nodes.length > 1) {
      html += `<div class="inspector-field"><span class="inspector-label">Selection</span><span class="inspector-val">${nodes.length} nodes</span></div>`;
      html += `<div class="inspector-field"><span class="inspector-label">Align</span><div class="nm-insp-grid">${M.ALIGN_MODES.map((m) => `<button type="button" class="nodemap-btn" data-insp="align" data-value="${m}">${m}</button>`).join("")}</div></div>`;
      html += `<div class="inspector-field"><span class="inspector-label">Distribute</span><div class="nm-insp-grid"><button type="button" class="nodemap-btn" data-insp="distribute" data-value="horizontal" ${nodes.length < 3 ? "disabled" : ""}>horizontally</button><button type="button" class="nodemap-btn" data-insp="distribute" data-value="vertical" ${nodes.length < 3 ? "disabled" : ""}>vertically</button></div></div>`;
      const scalables = nodes.filter((n) => M.scalable(n.type));
      if (scalables.length) html += scaleRow(scalables);
      const structs = nodes.filter((n) => n.type === "struct");
      if (structs.length) {
        const styles = new Set(structs.map((n) => n.data.style));
        const cur = styles.size === 1 ? [...styles][0] : "";
        html += row(`Struct style (${structs.length})`, sel("multi-style", cur, cur ? M.STRUCT_STYLES : ["", ...M.STRUCT_STYLES], cur ? null : ["mixed", ...M.STRUCT_STYLES]));
      }
      html += `<div class="inspector-field"><span class="inspector-label">Color</span>${swatches("")}</div>`;
      const bgs = new Set(nodes.map((x) => x.bgColor));
      html += bgRow("Background", bgs.size === 1 ? [...bgs][0] : "", "var(--bg-secondary)", "bg");
      html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="duplicate" title="Duplicate the selection (Ctrl+D)">⧉ Duplicate</button><button type="button" class="nodemap-btn" data-insp="group">▭ Group</button><button type="button" class="nodemap-btn" data-insp="fit">Fit</button><button type="button" class="nodemap-btn nm-danger" data-insp="delete">Delete</button></div>`;
    } else if (nodes.length === 1) {
      const n = nodes[0];
      const d = n.data;
      html += row("Title", `<div class="nm-insp-inline">${input("title", M.displayTitle(n))}${n.customTitle ? '<button type="button" class="nodemap-btn" data-insp="reset-title" title="Back to the default title">Reset</button>' : ""}</div>`);
      html += `<div class="nm-insp-pair">${row("Width", input("w", n.w, 'type="number"'))}${row("Height", input("h", n.h, 'type="number"'))}</div>`;
      if (M.AUTOFIT_TYPES.includes(n.type)) {
        html += check("autofit", n.autoFit, "Auto-fit & Scale", "Size the node to all of its content; resizing then keeps the aspect ratio and scales the content");
      }
      if (M.scalable(n.type)) html += scaleRow([n]);
      html += `<div class="inspector-field"><span class="inspector-label">Color</span>${swatches(n.color, n.type === "note" ? "var(--accent-yellow)" : "var(--accent-blue)")}</div>`;
      html += bgRow("Background", n.bgColor, "var(--bg-secondary)", "bg");
      html += check("hide-header", n.hideHeader, "Hide header in View Mode", "View Mode and exports leave this node's header out; Edit Mode shows it dimmed (drag, rename, restore)");
      if (["struct", "source", "symbol", "kconfig", "maintainer"].includes(n.type) || (n.type === "diff" && n.data.mode === "commit")) html += row("Revision", input("version", n.version, 'spellcheck="false"'));
      if (n.type === "struct") {
        html += row("Name", input("d-name", d.name, 'spellcheck="false"'));
        html += `<div class="nm-insp-pair">${row("Kind", sel("d-kind", d.kind, ["struct", "union"]))}${row("Style", sel("d-style", d.style, M.STRUCT_STYLES))}</div>`;
        html += row("Defining file", input("d-path", d.path, `spellcheck="false" placeholder="${escapeHtml(d.hint || "from the index")}"`));
        html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="struct-types" aria-pressed="${d.showTypes}">Types: ${d.showTypes ? "shown" : "hidden"}</button><button type="button" class="nodemap-btn" data-insp="struct-guards" aria-pressed="${d.showGuards}">#ifdef: ${d.showGuards ? "shown" : "hidden"}</button></div>`;
        html += check("d-disableKconfigEngine", d.disableKconfigEngine, "Disable Kconfig engine", "This node ignores the Kconfig engine: no #ifdef branch folds on its own");
        const eng = this.kconfigEngineFor(n);
        const why = d.disableKconfigEngine ? "off for this node" : !st.kconfigCollapse ? "off for the canvas" : eng ? `${eng.title || eng.tabId} · ${eng.version || "?"}${eng.arch ? ` · ${eng.arch}` : ""}: compiled-out branches fold` : `no resolved engine for ${n.version || "this revision"}`;
        html += `<div class="inspector-field"><span class="inspector-label">Kconfig folding</span><span class="inspector-val">${escapeHtml(why)}</span></div>`;
        const loaded = this.content.get(this.fetchKeyOf(n));
        // Items of this revision's layout only (keys of moved lines hide nothing)
        const hidden = loaded?.status === "ok" ? structRows(loaded.value, d, { editMode: true }).hidden : Object.keys(d.hiddenItems || {}).length;
        if (hidden) html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="show-hidden" title="Restore every hidden member, aggregate, #ifdef block and the outer conditions">Show all hidden (${hidden})</button></div>`;
        const layout = this.content.get(this.fetchKeyOf(n))?.value;
        if (layout) html += `<div class="inspector-field"><span class="inspector-label">Definition</span><span class="inspector-val">${escapeHtml(`${layout.file_path}:${layout.line_s}-${layout.line_e}`)} · span from ${layout.span_source === "db" ? "the index" : "the source text"}; guards read from the source</span></div>`;
      } else if (n.type === "source") {
        html += row("File", input("d-path", d.path, 'spellcheck="false"'));
        html += row("Show", sel("d-mode", d.mode, M.SOURCE_MODES));
        if (d.mode === "range") html += `<div class="nm-insp-pair">${row("From", input("d-start", d.start || "", 'type="number" min="1"'))}${row("To", input("d-end", d.end || "", 'type="number" min="1"'))}</div>`;
        if (d.mode === "symbol") html += `<div class="nm-insp-pair">${row("Symbol", input("d-symbol", d.symbol, 'spellcheck="false"'))}${row("Kind", sel("d-symbolKind", d.symbolKind, M.SYMBOL_KINDS))}</div>${row("Context lines", input("d-context", d.context, 'type="number" min="0" max="200"'))}`;
      } else if (n.type === "man") {
        html += `<div class="inspector-field"><span class="inspector-label">Page</span><span class="inspector-val">${escapeHtml(d.path || `${d.name}(${d.section})`)} · ${escapeHtml(d.version || "")}</span></div>`;
      } else if (n.type === "tab") {
        html += `<div class="inspector-field"><span class="inspector-label">Embedded ${escapeHtml(d.tabType)} tab</span><span class="inspector-val">#${escapeHtml(d.hash)}</span></div>`;
        const sess = M.embedSessionOf(this.canvas, n);
        html += row("Embedded session", sel("d-embedSessionId", sess.id, st.embedSessions.map((x) => x.id), st.embedSessions.map((x) => x.name)));
        html += `<div class="inspector-field"><span class="inspector-label">Runs isolated in this NodeMap's session "${escapeHtml(sess.name)}": tabs of one session share their Kconfig engines; nothing is saved and no workspace session sees it. Click it to interact; Esc or a click on the canvas gives the pointer back.</span></div>`;
      } else if (n.type === "diff") {
        html += row("Show", sel("d-mode", d.mode, M.DIFF_MODES, ["one file between two revisions", "a commit"]));
        if (d.mode === "commit") html += row("Commit", input("d-commitHash", d.commitHash, 'spellcheck="false"'));
        else html += `<div class="nm-insp-pair">${row("From", input("d-versionA", d.versionA, 'spellcheck="false"'))}${row("To", input("d-versionB", d.versionB, 'spellcheck="false"'))}</div>`;
        html += row(d.mode === "commit" ? "File (all when empty)" : "File", input("d-path", d.path, 'spellcheck="false"'));
        if (d.mode === "file") html += row("Old path (rename)", input("d-oldPath", d.oldPath, 'spellcheck="false"'));
      } else if (n.type === "mail") {
        html += `<div class="nm-insp-pair">${row("Thread id", input("d-tid", d.tid || "", 'type="number" min="1"'))}${row("Message-ID", input("d-mid", d.mid, 'spellcheck="false"'))}</div>`;
        if (!d.single) html += '<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="mail-expand" title="One node per message, connected by reply edges as the thread replies">⇶ Expand Thread to Nodes</button></div>';
      } else if (n.type === "kconfig") {
        html += `<div class="nm-insp-pair">${row("Symbol", input("d-symbol", d.symbol, 'spellcheck="false"'))}${row("ARCH", input("d-arch", d.arch, 'spellcheck="false"'))}</div>`;
        const values = this.kconfigValuesFor(n);
        const eng = this.pickEngine(n);
        const why = values ? `${kconfigValue(values, d.symbol) || "unset"} in ${eng?.title || eng?.tabId || "the engine"}` : "no Kconfig engine followed for this revision";
        html += `<div class="inspector-field"><span class="inspector-label">Value</span><span class="inspector-val">${escapeHtml(why)}</span></div>`;
        html += '<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="kc-expand" title="Add or connect every related symbol with labeled edges">⇶ Expand Relations</button></div>';
      } else if (n.type === "maintainer") {
        html += row("Section name", input("d-query", d.query || d.title, 'spellcheck="false"'));
        html += `<div class="nm-insp-pair">${row("Or a file", input("d-path", d.path, 'spellcheck="false"'))}${row("Section id", input("d-secId", d.secId || "", 'type="number" min="1"'))}</div>`;
      } else if (n.type === "note") {
        html += check("d-markdown", d.markdown, "Render Markdown");
      } else if (n.type === "group") {
        const members = M.groupDescendants(this.canvas.nodes, n.id).length;
        html += `<div class="inspector-field"><span class="inspector-label">Members</span><span class="inspector-val">${members} (drag nodes in or out of the frame)</span></div>`;
      }
      html += `<div class="nm-insp-actions">${M.TAB_OPENABLE_TYPES.includes(n.type) ? '<button type="button" class="nodemap-btn" data-insp="open-tab">↗ Open as tab</button>' : ""}<button type="button" class="nodemap-btn" data-insp="duplicate" title="Duplicate (Ctrl+D)">⧉ Duplicate</button><button type="button" class="nodemap-btn nm-danger" data-insp="delete">Delete</button></div>`;
      if (M.TAB_OPENABLE_TYPES.includes(n.type)) {
        const targets = this.copyTargets();
        if (!targets.some((x) => x.id === this.copyTarget)) this.copyTarget = targets[0]?.id || "";
        html += targets.length
          ? `<div class="inspector-field"><span class="inspector-label">Copy to Session</span><div class="nm-insp-inline">${sel("copy-target", this.copyTarget, targets.map((x) => x.id), targets.map((x) => x.name || x.id))}<button type="button" class="nodemap-btn" data-insp="copy-session" title="Open this node as a tab in that workspace session (the node stays)">Copy…</button></div></div>`
          : '<div class="inspector-field"><span class="inspector-label">Copy to Session</span><span class="inspector-val nm-hint">No other workspace session: create one with Switch Session</span></div>';
      }
    } else {
      html += `<div class="inspector-field"><span class="inspector-label">Canvas</span><span class="inspector-val">${this.canvas.nodes.length} nodes · ${this.canvas.edges.length} connections</span></div>`;
      html += `<div class="nm-insp-pair">${row("Grid", input("grid", st.grid, 'type="number" min="4" max="200"'))}${check("snap", st.snap, "Snap")}</div>`;
      html += bgRow("Canvas background", st.bgColor || "", "var(--bg-canvas)", "canvas-bg");
      const engines = this.followedEngines();
      // NodeMap embedded sessions: what the tab iframes run in, and the one the canvas follows
      html += `<div class="inspector-field"><span class="inspector-label">NodeMap sessions</span><div class="nm-es-list">${st.embedSessions.map((x) =>
        `<div class="nm-es-row"><input data-insp="es-name" data-id="${escapeHtml(x.id)}" value="${escapeHtml(x.name)}" aria-label="Session name" spellcheck="false"/><span class="nm-es-count" title="Tab nodes running in it">${this.canvas.nodes.filter((n) => n.type === "tab" && M.embedSessionOf(this.canvas, n).id === x.id).length}</span>` +
        `<button type="button" class="nodemap-btn nm-danger" data-insp="es-delete" data-id="${escapeHtml(x.id)}" title="Delete (its tab nodes move to the first session)"${st.embedSessions.length <= 1 ? " disabled" : ""}>×</button></div>`).join("")}` +
        `<button type="button" class="nodemap-btn" data-insp="es-add"${st.embedSessions.length >= M.EMBED_SESSIONS_MAX ? " disabled" : ""}>+ New Session</button></div></div>`;
      html += row("Follow session", sel("follow-session", st.followSession, ["", ...st.embedSessions.map((x) => x.id)], ["Host workspace", ...st.embedSessions.map((x) => `NodeMap: ${x.name}`)]));
      html += check("kconfig-collapse", st.kconfigCollapse, "Fold #ifdef branches the Kconfig engine compiles out", "Struct nodes fold the branches of #if / #ifdef blocks that the Kconfig engine's configuration leaves out");
      if (engines.length > 1 || (st.kconfigEngine && !engines.some((e) => e.tabId === st.kconfigEngine))) {
        const ids = ["", ...engines.map((e) => e.tabId)];
        const labels = ["Auto: the engine of each node's revision", ...engines.map((e) => `${e.title || e.tabId} · ${e.version || "?"}${e.arch ? ` · ${e.arch}` : ""}`)];
        if (st.kconfigEngine && !ids.includes(st.kconfigEngine)) {
          ids.push(st.kconfigEngine);
          labels.push("(closed engine)");
        }
        html += row("Kconfig engine", sel("kconfig-engine", st.kconfigEngine, ids, labels));
      } else {
        const e = engines[0];
        const none = st.followSession ? "None in this NodeMap session: embed a Kconfig tab in it and turn its engine on" : "None with a resolved configuration: open a Kconfig tab and turn its engine on";
        html += `<div class="inspector-field"><span class="inspector-label">Kconfig engine</span><span class="inspector-val">${escapeHtml(e ? `${e.title || e.tabId} · ${e.version || "?"}${e.arch ? ` · ${e.arch}` : ""} (nodes of ${e.version || "that revision"})` : none)}</span></div>`;
      }
      html += `<ul class="nm-help">
        <li>Drag the background to pan; wheel to zoom (Ctrl+wheel over node content)</li>
        <li>Shift/Ctrl+drag or the Select tool draws a selection box; Shift/Ctrl+click adds to it</li>
        <li>Drag a header to move, a corner or edge to resize; Edit Mode: double-click a title to rename</li>
        <li>Drag from a side dot (or a struct field dot) onto another node to connect</li>
        <li>Ctrl+C / Ctrl+V copy and paste nodes (across NodeMap tabs), Ctrl+D duplicates</li>
        <li>Ctrl+Z undoes, Ctrl+Y or Ctrl+Shift+Z redoes</li>
        <li>Double-click the background for a note; Delete removes, arrows nudge</li>
        <li>Drag a tab from a tab strip onto the canvas to copy it in (Shift: as a live tab)</li>
      </ul>`;
    }
    this.inspectorEl.innerHTML = html;
  }

  onInspectorInput(e, final) {
    const el = e.target.closest("[data-insp]");
    if (!el || el.tagName === "BUTTON") return;
    const k = el.dataset.insp;
    const val = el.type === "checkbox" ? el.checked : el.value;
    const edge = this.selectedEdge ? this.canvas.edges.find((x) => x.id === this.selectedEdge) : null;
    const node = this.selected.size === 1 ? this.byId([...this.selected][0]) : null;
    const int = (v) => (/^\d+$/.test(String(v).trim()) ? parseInt(v, 10) : null);
    // Live edits (picker drags, typing, then the final change event) are one undo step per field and target
    const coalesce = `insp:${k}:${edge?.id || node?.id || (this.selected.size ? [...this.selected].join(",") : "canvas")}`;
    const coalesceEnd = Boolean(final);
    if (k === "color-custom" || k === "bg-custom" || k === "canvas-bg-custom") {
      // Live while the picker is open; the inspector redraws once it is closed
      if (!M.isCustomColor(val)) return;
      const opts = { inspector: final, coalesce, coalesceEnd };
      if (k === "bg-custom") return this.setBackground(val, opts);
      if (k === "canvas-bg-custom") return this.setCanvasBackground(val, opts);
      return this.setColor(val, opts);
    }
    if (k === "copy-target") {
      this.copyTarget = String(val || "");
      return;
    }
    if (k === "content-scale") {
      const pct = Number(val);
      if (!Number.isFinite(pct) || pct <= 0) return;
      const list = this.selectedNodes().filter((x) => M.scalable(x.type));
      if (!list.length) return;
      for (const x of list) M.setContentScale(x, pct / 100);
      // Live while dragging the slider: the inspector redraws (with the new value) once it is let go
      return this.commit({ inspector: final, coalesce: `insp:content-scale:${list.map((x) => x.id).join(",")}`, coalesceEnd: Boolean(final) });
    }
    if (k === "es-name") {
      if (!final) return;
      if (!M.renameEmbedSession(this.canvas.settings, el.dataset.id, val)) return this.renderInspector();
    } else if (k === "follow-session") {
      this.canvas.settings.followSession = String(val || "");
      this.syncFollowChannel();
      this.onKconfigChanged();
    } else if (k === "kconfig-collapse") {
      this.canvas.settings.kconfigCollapse = Boolean(val);
    } else if (k === "kconfig-engine") {
      this.canvas.settings.kconfigEngine = String(val || "");
    } else if (k === "multi-style") {
      if (!M.STRUCT_STYLES.includes(val)) return;
      for (const n of this.selectedNodes()) if (n.type === "struct") n.data.style = val;
    } else if (node && k === "hide-header") {
      node.hideHeader = Boolean(val);
      if (node.autoFit) this.fitQueue.add(node.id);
    } else if (node && k === "autofit") {
      M.setAutoFit(node, Boolean(val));
      if (node.autoFit) this.fitQueue.add(node.id);
    } else if (k === "grid" && final) {
      this.canvas.settings.grid = Math.max(4, Math.min(200, int(val) || M.GRID_DEFAULT));
      this.applyTransform();
    } else if (k === "snap") {
      this.canvas.settings.snap = Boolean(val);
      this.rootEl.querySelector("[data-tb='snap']")?.setAttribute("aria-pressed", String(this.canvas.settings.snap));
      this.applyTransform();
    } else if (edge && k.startsWith("edge-")) {
      const f = k.slice(5);
      if (f === "label") edge.label = String(val).slice(0, 512);
      else Object.assign(edge, M.normalizeEdge({ ...edge, [f]: val }));
    } else if (node && k === "title") {
      if (!final) return;
      return this.setTitle(node.id, val);
    } else if (node && (k === "w" || k === "h") && final) {
      const lim = M.SIZE_LIMITS[node.type];
      const v = int(val);
      // An auto-fitted node keeps its aspect ratio: the size sets the content scale
      if (v && node.autoFit) M.setScaledSize(node, k, v);
      else if (v) node[k] = k === "w" ? Math.max(lim.minW, Math.min(lim.maxW, v)) : Math.max(lim.minH, Math.min(lim.maxH, v));
    } else if (node && k === "version" && final) {
      node.version = String(val).trim();
    } else if (node && k.startsWith("d-")) {
      if (!final && el.tagName !== "SELECT") return;
      const f = k.slice(2);
      const raw = ["start", "end", "context", "tid", "secId"].includes(f) ? int(val) : val;
      // A maintainer node looked up by name or file again forgets the section it resolved before
      const reset = node.type === "maintainer" && (f === "query" || f === "path") ? { secId: null, title: "" } : {};
      node.data = M.normalizeData(node.type, { ...node.data, ...reset, [f]: typeof raw === "string" ? raw.trim() : raw });
      if (!node.customTitle) node.title = M.defaultTitle(node.type, node.data);
    } else return;
    // Typing in a field keeps the inspector (and the caret) as it is
    this.commit({ inspector: final && (el.tagName === "SELECT" || el.type === "checkbox" || k.startsWith("d-") || k === "w" || k === "h" || k === "es-name"), coalesce, coalesceEnd });
  }

  onInspectorClick(e) {
    const btn = e.target.closest("button[data-insp]");
    if (!btn) return;
    const k = btn.dataset.insp;
    const node = this.selected.size === 1 ? this.byId([...this.selected][0]) : null;
    if (k === "edit-mode") this.setEditMode(!this.canvas.settings.editMode);
    else if (k === "color") this.setColor(btn.dataset.value || "");
    else if (k === "bg" || k === "bg-reset") this.setBackground(k === "bg" ? btn.dataset.value || "" : "");
    else if (k === "canvas-bg" || k === "canvas-bg-reset") this.setCanvasBackground(k === "canvas-bg" ? btn.dataset.value || "" : "");
    else if (k === "duplicate") this.duplicateSelection();
    else if (k === "show-hidden" && node) this.onStructAction(node, "show-hidden");
    else if (k === "scale-reset") {
      const list = this.selectedNodes().filter((x) => M.scalable(x.type));
      for (const x of list) M.setContentScale(x, 1);
      if (list.length) this.commit();
    } else if (k === "es-add") {
      if (M.addEmbedSession(this.canvas.settings)) this.commit();
    } else if (k === "es-delete") {
      if (M.removeEmbedSession(this.canvas, btn.dataset.id)) {
        this.syncFollowChannel();
        this.commit();
      }
    } else if (k === "copy-session" && node) this.copyNodeToSession(node.id, this.copyTarget);
    else if (k === "mail-expand" && node) this.expandThreadToNodes(node.id);
    else if (k === "kc-expand" && node) this.expandKconfigRelations(node.id);
    else if (k === "align") this.align(btn.dataset.value);
    else if (k === "distribute") this.distribute(btn.dataset.value);
    else if (k === "group") this.addGroup();
    else if (k === "fit") this.fitView();
    else if (k === "delete") this.deleteSelection();
    else if (k === "open-tab" && node) this.openNodeAsTab(node.id);
    else if (k === "reset-title" && node) this.setTitle(node.id, "");
    else if (k === "struct-types" && node) this.onStructAction(node, "all-types");
    else if (k === "struct-guards" && node) this.onStructAction(node, "guards");
    else if (k === "edge-reverse" && this.selectedEdge) {
      const ed = this.canvas.edges.find((x) => x.id === this.selectedEdge);
      if (ed) {
        [ed.from, ed.to] = [ed.to, ed.from];
        [ed.fromSide, ed.toSide] = [ed.toSide, ed.fromSide];
        [ed.fromField, ed.toField] = [ed.toField, ed.fromField];
        this.commit();
      }
    }
  }

  // ------------------------------------------------------------------ import / export

  download(name, blob) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  exportJSON() {
    this.download("nodemap_canvas.json", new Blob([JSON.stringify(M.serializeCanvas(this.canvas), null, 2)], { type: "application/json" }));
    toast.success("Exported the canvas as JSON");
  }

  /**
   * Options of the export: loaded struct / source payloads, the struct rows' view state and (`foreign`)
   * DOM snapshots of mounted man pages and live tabs.
   */
  svgOptions({ foreign = true } = {}) {
    return {
      editMode: Boolean(this.canvas.settings.editMode),
      content: (n) => this.modelOf(n),
      structOpts: (n) => this.structOpts(n),
      rowHeights: (n) => this.structRowHeights(n),
      kconfigValues: (n) => this.kconfigValuesFor(n),
      foreign: foreign ? (n) => captureNodeForeignObject(n, this.renderer?.element?.(n.id)) : null,
    };
  }

  /** Heights of a struct node's live rows (content units, one per drawn row), or null without a DOM. */
  structRowHeights(node) {
    if (node.type !== "struct") return null;
    const rows = this.renderer?.element?.(node.id)?.querySelector?.(".nm-struct-rows");
    if (!rows?.children) return null;
    const hs = [...rows.children].filter((el) => el.classList?.contains("nm-sf")).map((el) => el.offsetHeight || 0);
    return hs.length && hs.every((h) => h > 0) ? hs : null;
  }

  /** The whole canvas as a standalone SVG document (`opts`: transparent, foreign, pixelScale). */
  canvasSvg(opts = {}) {
    return canvasToSvg(this.canvas, (c) => resolveColor(c), {
      ...this.svgOptions({ foreign: opts.foreign !== false }),
      transparent: Boolean(opts.transparent),
      pixelScale: opts.pixelScale || 1,
    });
  }

  /**
   * Image export dialog (Export ▾ → SVG / PNG image): transparent background (both), upscale factor with
   * the output size in pixels (PNG). Export runs `exportSVG()` / `exportPNG()` with the choices.
   */
  openImageExport(kind) {
    const png = kind === "png";
    // Size probe without DOM snapshots (they do not change the bounds)
    const { w, h } = svgSize(this.canvasSvg({ foreign: false }));
    const open = this.openModalFn || openModal;
    const modal = open({
      title: png ? "Export PNG Image" : "Export SVG Image",
      subtitle: `The whole canvas: ${w} × ${h} canvas units`,
      width: "420px",
      footerHtml: '<button type="button" class="code-btn primary" data-act="export">Export</button>',
    });
    const scales = PNG_SCALES.map((s) => `<option value="${s}"${s === PNG_DEFAULT_SCALE ? " selected" : ""}>${s}×</option>`).join("");
    modal.body.innerHTML = `<form class="nm-form">
        <label class="nm-insp-check"><input type="checkbox" name="transparent"/> <span>Transparent background</span></label>
        ${png ? `<label class="nm-form-row"><span>Scale (sharper text and lines at higher factors)</span><select name="scale">${scales}</select></label><div class="nm-export-size" aria-live="polite"></div>` : ""}
      </form>`;
    const q = (sel) => modal.body.querySelector(sel);
    const transEl = q("[name='transparent']");
    const scaleEl = q("[name='scale']");
    const sizeEl = q(".nm-export-size");
    const update = () => {
      if (!sizeEl) return;
      const z = pngSize(w, h, Number(scaleEl?.value) || PNG_DEFAULT_SCALE);
      sizeEl.textContent = `Output: ${z.w} × ${z.h} px${z.clamped ? " (reduced to the browser's canvas limit)" : ""}`;
    };
    scaleEl?.addEventListener?.("change", update);
    update();
    const submit = () => {
      const opts = { transparent: Boolean(transEl?.checked), scale: Number(scaleEl?.value) || PNG_DEFAULT_SCALE };
      modal.close();
      if (png) this.exportPNG(opts);
      else this.exportSVG(opts);
    };
    q("form")?.addEventListener?.("submit", (e) => {
      e.preventDefault();
      submit();
    });
    const btn = modal.footer?.querySelector?.("[data-act='export']");
    if (btn) btn.onclick = submit;
    return modal;
  }

  exportSVG({ transparent = false } = {}) {
    const svg = this.canvasSvg({ transparent });
    this.download("nodemap_canvas.svg", new Blob([svg], { type: "image/svg+xml" }));
    toast.success("Exported SVG image");
    return svg;
  }

  /**
   * PNG of the canvas at `scale` (clamped by `pngSize`): the SVG is rasterized at the output size (its
   * width/height carry the scale), so text and lines stay sharp. A browser that refuses to rasterize the
   * man page / tab snapshots (a tainted canvas) gets the image with their placeholder cards.
   */
  exportPNG({ transparent = false, scale = PNG_DEFAULT_SCALE } = {}) {
    const { w, h } = svgSize(this.canvasSvg({ transparent, foreign: false }));
    const size = pngSize(w, h, scale);
    const rasterize = (foreign) => {
      const svg = this.canvasSvg({ transparent, foreign, pixelScale: size.scale });
      const img = new Image();
      const fallback = (err) => {
        if (foreign) {
          toast.warning("This browser cannot rasterize the live man page / tab contents: the PNG shows placeholders for them");
          rasterize(false);
        } else toast.error(`PNG export failed${err?.message ? `: ${err.message}` : ""}`);
      };
      img.onload = () => {
        try {
          const c = document.createElement("canvas");
          c.width = size.w;
          c.height = size.h;
          const ctx = c.getContext("2d");
          ctx.imageSmoothingEnabled = true;
          ctx.imageSmoothingQuality = "high";
          ctx.drawImage(img, 0, 0, size.w, size.h);
          c.toBlob((blob) => {
            if (!blob) return toast.error("PNG export failed");
            this.download("nodemap_canvas.png", blob);
            toast.success(`Exported PNG image (${size.w} × ${size.h} px)`);
          }, "image/png");
        } catch (err) {
          fallback(err);
        }
      };
      img.onerror = () => fallback(null);
      // A data URL (not a blob URL) keeps a foreignObject SVG from tainting the canvas where browsers allow it
      img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    };
    rasterize(true);
    return size;
  }

  /** Replace the canvas with an exported JSON document (rejected when it is not a NodeMap canvas). */
  importJSON(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (e) => {
      let parsed = null;
      try {
        parsed = JSON.parse(e.target.result);
      } catch (err) {
        toast.error(`Import failed: ${err.message}`);
        return;
      }
      const canvas = M.normalizeCanvas(parsed);
      if (!canvas) {
        toast.error(`Import failed: not a NodeMap canvas (schema "${M.CANVAS_SCHEMA}" version ${M.CANVAS_VERSION})`);
        return;
      }
      this.canvas = canvas;
      this.selected.clear();
      this.selectedEdge = null;
      this.active = null;
      this.syncFollowChannel();
      this.applyTransform();
      this.rootEl.querySelector("[data-tb='snap']")?.setAttribute("aria-pressed", String(canvas.settings.snap));
      this.commit();
      toast.success(`Imported ${canvas.nodes.length} nodes and ${canvas.edges.length} connections`);
    };
    reader.readAsText(file);
  }
}
