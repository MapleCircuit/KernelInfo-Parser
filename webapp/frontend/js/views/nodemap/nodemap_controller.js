/**
 * nodemap_controller.js - NodeMap spatial canvas coordinator (`webapp/API_FRONTEND.md` §5.3).
 *
 * One controller per NodeMap tab. Owns the tab's canvas document (`canvas_model.js`) and
 * delegates specialized concerns to focused modules:
 *   - `nodemap_inspector.js`: Inspector panel rendering and input/click actions
 *   - `nodemap_gestures.js`: Pointer, drag/resize/wire/marquee/inline/expansion actions, snap guides & shortcuts
 *   - `nodemap_dialogs.js`: + Add Node, Export, Call Path, Field Lock, Library, Impact Map & Shortcuts modals
 *   - `nodemap_persistence.js`: Async IndexedDB / localStorage sync, clipboard, file import/export & tab pruning
 */
import { api } from "../../api.js";
import { localDB } from "../../db.js";
import { state } from "../../state.js";
import { urlSync } from "../../url_sync.js";
import { toast } from "../../components/toast.js";
import { escapeHtml } from "../../components/modal.js";
import { shortRef } from "../../components/ref_picker.js";
import { registerSelectionVersionApplier } from "../../components/version_switch.js";
import { resolveColor } from "../../utils/theme.js";
import { onUiScaleChanged } from "../../utils/ui_scale.js";
import { isDocumentationPath } from "../docs/rst_renderer.js";
import { DirectiveEvaluator, archMacros } from "../kconfig/directive_evaluator.js";
import { mountManDocument } from "../man/man_view.js";
import * as M from "./canvas_model.js";
import {
  cveModel, diffPayload, diffRows, docModel, fullMailBody, historyModel, kapiModel, kconfigModel, kconfigValue,
  mailThreadModel, maintainerModel,
  renderCveBody, renderDiffBody, renderDocBody, renderHistoryBody, renderKapiBody, renderKconfigBody,
  renderMailBody, renderMaintainerBody,
} from "./domain_nodes.js";
import { computeEdgeGeometry, dashArray, nearestWaypointSegment } from "./edge_router.js";
import { NodeMapMinimap, NodeMapSearch } from "./minimap_search.js";
import { headMetaOf, renderNoteMarkdown, renderSourceRows } from "./node_content.js";
import { NodeRenderer } from "./node_renderer.js";
import { tabForNode } from "./nodemap_bridge.js";
import {
  EMBED_TAB_TYPES, openFormatExportModal, openImageExport, openImpactMapDialog, openLibraryDialog,
  openNodeDialog, openShortcutsDialog, promptAttachNode, promptCallPath, promptFieldLock,
  showAddMenu, showExportMenu, specFromForm,
} from "./nodemap_dialogs.js";
import { canvasToSvg, captureNodeForeignObject } from "./nodemap_export.js";
import {
  attachCanvasEvents, beginEdgeLabelEdit, beginNoteEdit, beginTitleEdit, clearAlignmentGuides,
  clearTextSelection, expandCallPath, expandCallgraph, expandKconfigRelations, expandStructTypes, expandThreadToNodes, expandableTypesOf,
  finishWire, followStructType, followableTypeFor, inlineInput, mailNodeOf, onClick, onContextMenu,
  onDblClick, onKeyDown, onPointerDown, onPointerMove, onPointerUp, onStructAction, onWheel,
  selectEdge, selectOnly, setTitle, spawnFromCveDiff, spawnLinkedNode, spawnMailMessages,
  toggleExpandNode, toggleMailMessage,
} from "./nodemap_gestures.js";
import { onInspectorClick, onInspectorInput, pickerValue, renderInspector } from "./nodemap_inspector.js";
import {
  acceptsTabDrag, attachTabDrop, closeFollowChannel, copyNodeToSession, copySelection, copyTargets,
  download, dropTab, embedKey, exportDot, exportInteractiveHTML, exportJSON, exportMermaid, exportPNG,
  exportSVG, flushSave, hasIndexedDbBackend, importJSON, loadFromIndexedDB, onEmbedMessage,
  pasteClipboard, pruneOrphanedCanvases, readClipboard, scheduleSave, syncFollowChannel,
} from "./nodemap_persistence.js";
import { layoutGraphJob } from "./nodemap_worker.js";
import { renderStructBody } from "./struct_view.js";
import { NodeMapTourPlayer } from "./tour_mode.js";
import { applyCanvasVersion, diffStructLayouts, renderStructDiffBadge, retargetNodeVersions, revertCanvasVersion } from "./version_diff.js";
import { VIRTUAL_THRESHOLD, estimateNaturalSourceSize, mountVirtualDiff, mountVirtualSource, visibleCanvasRect } from "./virtual_source.js";
import { NodeMapWorkerPool } from "./worker_pool.js";

export { EMBED_TAB_TYPES };

const PASTE_STEP = 24;
const UNDO_LIMIT = 80;
const cssEsc = (s) => (globalThis.CSS?.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&"));
/** Rows an edge end can anchor to (`fromField` / `toField`): struct members, mail messages, history rows. */
const ANCHOR_ROWS = [".nm-sf", ".nm-mail-row", ".nm-hist-row"];
export const ANCHOR_ROW_SEL = ANCHOR_ROWS.map((c) => `${c}[data-key]`).join(", ");
const anchorRowSel = (key) => ANCHOR_ROWS.map((c) => `${c}[data-key="${cssEsc(key)}"]`).join(", ");
/** The rectangle a row anchors at: its header line when it has one (an open mail message), else the row. */
const anchorRectOf = (row) => (row.querySelector?.(".nm-anchor-line") || row).getBoundingClientRect?.();

export class NodeMapController {
  constructor() {
    this.canvas = null;
    this.rootCanvas = null;
    this.subMapPath = [];
    this.selected = new Set();
    this.selectedEdge = null;
    this.active = null;
    this.tool = "pan";
    this.content = new Map();
    this.mounts = new Map();
    this.gesture = null;
    this.saveTimer = null;
    this.unsubs = [];
    this.containerEl = null;
    this.evalCache = new Map();
    this.kconfigEpoch = 0;
    this.fitQueue = new Set();
    this.pasteStamp = "";
    this.pasteCount = 0;
    this.history = new M.UndoStack(UNDO_LIMIT);
    this.connectedPorts = new Set();
    this.mailBodies = new Map();
    this.embedEngines = new Map();
    this.followChannel = null;
    this.followKey = "";
    this.focusHops = null;
    this.focusSeedIds = new Set();
    this.versionDiffTarget = "";
    this.versionDiffCache = new Map();
    this.versionRevertMap = null;
    this.wireEls = new Map();
    this.edgeEnds = new Map();
    this.pendingWireEl = null;
    this.guideEl = null;
    this.dirtyWireNodes = null;
    this.portOffsets = new Map();
    this.workerStructModels = new Map();
    this.highlightTokens = new Map();
    this.workerPool = new NodeMapWorkerPool();
    this.minimap = new NodeMapMinimap(this);
    this.searchBar = new NodeMapSearch(this);
    this.tourPlayer = new NodeMapTourPlayer(this);
    this.activeTourId = "";
    this.canvasMeta = {};
    this.userDb = localDB;
    this._disposeGen = 0;
    this._seededAtMount = false;
    this._incomingAtMount = [];
    this._userEdited = false;
    this.channelFactory = (name) => (typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(name) : null);
    this.kconfigSource = {
      list: () => (typeof state.kconfigEngineList === "function" ? state.kconfigEngineList() : []),
      context: (id) => (typeof state.definitionContext === "function" ? state.definitionContext(id) : null),
    };
  }

  // ------------------------------------------------------------------ lifecycle & persistence delegation

  render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tab = tabData;
    this.currentVersion = tabData?.version || state.currentVersion;
    this.repo = tabData?.repo || state.activeRepo();
    this.storageKey = `${M.STORAGE_PREFIX}${tabData?.id || "default"}`;
    // The header's Apply moves the selected nodes instead of the tab while some are selected
    this.unregisterApplier?.();
    this.unregisterApplier = tabData?.id ? registerSelectionVersionApplier(tabData.id, (ref) => this.applyVersionToSelection(ref)) : null;

    const firstInit = !this.canvas;
    if (firstInit) {
      this.rootCanvas = M.loadCanvas(globalThis.localStorage, this.storageKey) || M.emptyCanvas();
      this.canvas = this.rootCanvas;
      this.history.reset(M.structuralSnapshot(this.canvas));
    }
    if (this.rootEl) {
      containerEl.innerHTML = "";
      containerEl.appendChild(this.rootEl);
      this.subscribe();
      this.consumeIncoming(tabData);
      this.applyTransform();
      this.renderBreadcrumbs();
      this.minimap?.render();
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
    this.breadcrumbsEl = q(".nm-breadcrumbs");

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
    this.minimap.mount(this.wrapEl);
    this.searchBar.mount(this.wrapEl);
    this.tourPlayer.mount(this.wrapEl);
    this.subscribe();
    this._seededAtMount = Boolean(Array.isArray(tabData?.initialNodes) || tabData?.initialCanvas);
    this._incomingAtMount = this._seededAtMount ? [] : this.consumeIncoming(tabData);
    if (this._seededAtMount) this.consumeIncoming(tabData);
    this.applyTransform();
    this.renderBreadcrumbs();
    this.commit({ save: false, record: true });
    if (firstInit && !this._seededAtMount && this._hasIndexedDbBackend()) this.loadFromIndexedDB();
  }

  _hasIndexedDbBackend() { return hasIndexedDbBackend(this); }
  loadFromIndexedDB() { return loadFromIndexedDB(this); }
  scheduleSave() { return scheduleSave(this); }
  flushSave() { return flushSave(this); }
  embedKey(sessionId) { return embedKey(this, sessionId); }
  syncFollowChannel() { return syncFollowChannel(this); }
  closeFollowChannel() { return closeFollowChannel(this); }
  onEmbedMessage(msg) { return onEmbedMessage(this, msg); }
  copyTargets() { return copyTargets(this); }
  copyNodeToSession(id, sessionId) { return copyNodeToSession(this, id, sessionId); }
  acceptsTabDrag(e) { return acceptsTabDrag(e); }
  dropTab(payload, clientX, clientY, opts) { return dropTab(this, payload, clientX, clientY, opts); }
  attachTabDrop() { return attachTabDrop(this); }
  pruneOrphanedCanvases(opts) { return pruneOrphanedCanvases(this, opts); }
  copySelection() { return copySelection(this); }
  readClipboard() { return readClipboard(); }
  pasteClipboard() { return pasteClipboard(this); }
  download(name, blob) { return download(name, blob); }
  exportJSON() { return exportJSON(this); }
  exportSVG(opts) { return exportSVG(this, opts); }
  exportInteractiveHTML(opts) { return exportInteractiveHTML(this, opts); }
  exportMermaid(opts) { return exportMermaid(this, opts); }
  exportDot(opts) { return exportDot(this, opts); }
  exportPNG(opts) { return exportPNG(this, opts); }
  importJSON(file) { return importJSON(this, file); }

  dispose() {
    this._disposeGen += 1;
    this.unregisterApplier?.();
    this.unregisterApplier = null;
    this.flushSave();
    clearTimeout(this.kconfigTimer);
    this.kconfigTimer = null;
    if (this.wireFrame && typeof cancelAnimationFrame === "function") cancelAnimationFrame(this.wireFrame);
    this.wireFrame = null;
    for (const u of this.unsubs) u();
    this.unsubs = [];
    this.minimap?.dispose?.();
    this.searchBar?.dispose?.();
    this.tourPlayer?.dispose?.();
    this.workerPool?.dispose?.();
    clearAlignmentGuides(this);
    this.guideEl?.remove?.();
    this.guideEl = null;
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
    this.wireEls.clear();
    this.edgeEnds.clear();
    this.edgeGeoms?.clear?.();
    this.pendingWireEl = null;
    this.portOffsets.clear();
    this.workerStructModels.clear();
    this.highlightTokens.clear();
    this.content.clear();
    this.evalCache.clear();
    this.mailBodies.clear();
    this.versionDiffCache.clear();
    this.fitQueue.clear();
    this._incomingAtMount = [];
    this.gesture = null;
    this.renderer = null;
    this.rootEl = null;
    this.containerEl = null;
    this.wrapEl = null;
    this.layerEl = null;
    this.wireSvg = null;
    this.marqueeEl = null;
    this.inspectorEl = null;
    this.zoomLabel = null;
    this.hintEl = null;
    this.breadcrumbsEl = null;
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
    this.unsubs.push(typeof offPending === "function" ? offPending : () => { });
    const offScale = onUiScaleChanged(() => {
      this.portOffsets.clear();
      this.renderWires();
    });
    this.unsubs.push(typeof offScale === "function" ? offScale : () => { });
    const onTheme = () => this.renderWires();
    window.addEventListener("kip:theme-changed", onTheme);
    this.unsubs.push(() => window.removeEventListener("kip:theme-changed", onTheme));
    for (const ev of ["kconfig:engine:changed", "kconfig:assignments:updated"]) {
      const off = state.subscribe(ev, () => this.onKconfigChanged());
      this.unsubs.push(typeof off === "function" ? off : () => { });
    }
    this.syncFollowChannel();
  }

  onKconfigChanged() {
    this.kconfigEpoch += 1;
    this.evalCache.clear();
    this.workerStructModels.clear();
    for (const n of this.canvas?.nodes || []) if (n.autoFit && (n.type === "struct" || n.type === "kconfig")) this.fitQueue.add(n.id);
    clearTimeout(this.kconfigTimer);
    this.kconfigTimer = setTimeout(async () => {
      if (!this.containerEl?.isConnected) return;
      if (this.workerPool?.workerSupported && this.canvas?.settings.kconfigCollapse && this.canvas?.nodes?.some((n) => n.type === "struct")) {
        const byEngine = new Map();
        for (const n of this.canvas.nodes) {
          if (n.type !== "struct" || n.data?.disableKconfigEngine) continue;
          const entry = this.content.get(this.fetchKeyOf(n));
          const eng = this.kconfigEngineFor(n);
          const ctx = eng ? this.engineContext(eng.tabId) : null;
          if (entry?.status === "ok" && ctx?.assignments) {
            if (!byEngine.has(eng.tabId)) {
              byEngine.set(eng.tabId, { assignments: ctx.assignments, arch: ctx.arch || "x86_64", strict: Boolean(ctx.strict), structs: [] });
            }
            byEngine.get(eng.tabId).structs.push({ nodeId: n.id, layout: entry.value, data: n.data, editMode: Boolean(this.canvas?.settings.editMode) });
          }
        }
        const batches = [...byEngine.values()];
        const total = batches.reduce((sum, b) => sum + b.structs.length, 0);
        if (total > 1) {
          try {
            const epoch = this.kconfigEpoch;
            const settled = await Promise.all(batches.map((b, i) => this.workerPool.evaluateKconfigBatch(b, { key: `kconfig:${i}` })));
            if (epoch === this.kconfigEpoch && this.containerEl?.isConnected) {
              for (const res of settled) {
                for (const item of res?.results || []) {
                  const n = this.byId(item.nodeId);
                  if (n) this.workerStructModels.set(n.id, { key: this.bodyKey(n), data: n.data, model: item.model });
                }
              }
            }
          } catch {
            // Fall through cleanly to normal commit
          }
        }
      }
      if (this.containerEl?.isConnected) this.commit({ save: false });
    }, 120);
  }

  // ------------------------------------------------------------------ Kconfig engine

  kconfigEngineFor(node) {
    const st = this.canvas?.settings;
    if (!st?.kconfigCollapse || node?.type !== "struct" || node.data.disableKconfigEngine) return null;
    return this.pickEngine(node);
  }

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

  engineContext(tabId) {
    const e = this.embedEngines.get(tabId);
    if (e) return { engineTabId: tabId, assignments: e.assignments, arch: e.arch, strict: true };
    return this.kconfigSource.context(tabId);
  }

  pickEngine(node) {
    const st = this.canvas?.settings;
    const list = this.followedEngines();
    if (st?.kconfigEngine) return list.find((e) => e.tabId === st.kconfigEngine) || null;
    const hit = list.find((e) => e.version && e.version === node.version);
    if (hit) return hit;
    return list.length === 1 && list[0].isEmbedded ? list[0] : null;
  }

  kconfigValuesFor(node) {
    const eng = this.pickEngine(node);
    if (!eng) return null;
    try {
      return this.engineContext(eng.tabId)?.assignments || null;
    } catch {
      return null;
    }
  }

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

  structOpts(node) {
    const vdiff = this.versionDiffFor(node);
    const pahole = node.data?.style === "layout" ? this.paholeFor(node) : null;
    const wm = this.workerStructModels.get(node.id);
    const model = wm && wm.key === this.bodyKey(node) && wm.data === node.data ? wm.model : undefined;
    if (wm && !model) this.workerStructModels.delete(node.id);
    return {
      editMode: Boolean(this.canvas?.settings.editMode),
      evaluator: this.evaluatorFor(node),
      model,
      pahole,
      diffHtml: vdiff?.badgeHtml || "",
      versionDiff: vdiff?.diff || null,
    };
  }

  paholeKeyOf(node) { return JSON.stringify(["pahole", node.repo, node.version, node.data?.name]); }

  paholeFor(node) {
    if (!node?.data?.name) return null;
    const key = this.paholeKeyOf(node);
    let entry = this.content.get(key);
    if (!entry) {
      entry = { status: "loading" };
      this.content.set(key, entry);
      const client = this.clientFor(node);
      if (typeof client.getPaholeLayout === "function") {
        client.getPaholeLayout(node.version || this.currentVersion, node.data.name).then((value) => {
          entry.status = "ok";
          entry.value = value;
        }, (err) => {
          entry.status = "ok";
          entry.value = { unavailable: true, error: err?.message || "pahole layout unavailable" };
        }).finally(() => {
          for (const n of this.canvas?.nodes || []) if (n.type === "struct" && this.paholeKeyOf(n) === key) this.renderer?.invalidate(n.id);
          if (this.containerEl?.isConnected) this.commit({ save: false, inspector: false });
        });
      } else {
        entry.status = "ok";
        entry.value = { unavailable: true, error: "pahole endpoint unavailable" };
      }
    }
    return entry.status === "ok" ? entry.value : null;
  }

  versionDiffFor(node) {
    const target = this.versionDiffTarget;
    if (!target || node?.type !== "struct" || !node.data?.name || node.version === target) return null;
    const baseEntry = this.content.get(this.fetchKeyOf(node));
    if (baseEntry?.status !== "ok") return null;
    const key = JSON.stringify(["vdiff", node.repo, node.version, target, node.data.name, node.data.kind, node.data.path]);
    let entry = this.versionDiffCache.get(key);
    if (!entry) {
      entry = { status: "loading" };
      this.versionDiffCache.set(key, entry);
      const client = this.clientFor(node);
      if (typeof client.getNodemapStruct === "function") {
        client.getNodemapStruct(target, node.data.name, { path: node.data.path, kind: node.data.kind }).then((val) => {
          const diff = diffStructLayouts(baseEntry.value, val);
          entry.status = "ok";
          entry.value = { diff, badgeHtml: renderStructDiffBadge(diff, node.version, target) };
        }, (err) => {
          entry.status = "error";
          entry.error = err;
        }).finally(() => {
          for (const n of this.canvas?.nodes || []) if (n.type === "struct" && n.data?.name === node.data.name) this.renderer?.invalidate(n.id);
          if (this.containerEl?.isConnected) this.commit({ save: false, inspector: false });
        });
      }
    }
    return entry.status === "ok" ? entry.value : null;
  }

  bodyKey(node) {
    if (node.type === "struct") {
      return JSON.stringify([
        Boolean(this.canvas.settings.editMode),
        this.kconfigEngineFor(node)?.tabId || "",
        this.kconfigEpoch,
        this.versionDiffTarget || "",
        node.data?.style === "layout" ? Boolean(this.content.get(this.paholeKeyOf(node))?.value) : false,
      ]);
    }
    if (node.type === "note") return this.canvas.settings.editMode ? "edit" : "";
    if (node.type === "kconfig") {
      const values = this.kconfigValuesFor(node);
      const names = this.canvas.nodes.filter((n) => n.type === "kconfig").map((n) => n.data.symbol).sort().join(",");
      return JSON.stringify([values ? kconfigValue(values, node.data.symbol) : null, names, this.kconfigEpoch]);
    }
    if (node.type === "mail") return String(this.mailEpoch || 0);
    if (node.type === "tab") return this.embedKey(M.embedSessionOf(this.canvas, node).id);
    return "";
  }

  // ------------------------------------------------------------------ node creation

  consumeIncoming(tab) {
    if (!tab) return [];
    const added = [];
    let seeded = false;
    if (tab.initialCanvas && typeof tab.initialCanvas === "object") {
      const imported = M.normalizeCanvas(tab.initialCanvas);
      if (imported) {
        this.rootCanvas = imported;
        this.subMapPath = [];
        this.canvas = imported;
        added.push(...this.canvas.nodes);
        seeded = true;
      }
    } else if (Array.isArray(tab.initialNodes)) {
      seeded = true;
      this.canvas = M.emptyCanvas();
      this.rootCanvas = this.canvas;
      this.subMapPath = [];
      const idMap = new Map();
      for (const spec of tab.initialNodes) {
        const node = this.nodeFromSpec(spec, { x: spec.x ?? 120, y: spec.y ?? 120 });
        if (!node) continue;
        if (spec.id) idMap.set(spec.id, node.id);
        this.canvas.nodes.push(node);
        added.push(node);
      }
      for (const node of this.canvas.nodes) {
        if (node.groupId && idMap.has(node.groupId)) node.groupId = idMap.get(node.groupId);
      }
      for (const e of tab.initialEdges || []) {
        const edge = M.createEdge({ ...e, from: idMap.get(e.from) || e.from, to: idMap.get(e.to) || e.to });
        if (edge && this.byId(edge.from) && this.byId(edge.to)) this.canvas.edges.push(edge);
      }
      if (Array.isArray(tab.initialTours)) {
        const tmp = M.normalizeCanvas({ ...this.canvas, tours: tab.initialTours });
        if (tmp) this.canvas.tours = tmp.tours;
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
    const keys = ["initialCanvas", "initialNodes", "initialEdges", "initialTours", "initialNode", "pendingNodes"];
    if (keys.some((k) => k in tab)) {
      state.updateTabState(tab.id, { initialCanvas: null, initialNodes: null, initialEdges: null, initialTours: null, initialNode: null, pendingNodes: null });
      for (const k of keys) delete tab[k];
    }
    if (added.length) {
      this.selected = new Set(added.map((n) => n.id));
      this.selectedEdge = null;
      if (seeded || added.length > 1) this.fitView(false);
      this.scheduleSave();
    }
    return added;
  }

  nodeFromSpec(spec, pos = null) {
    if (!spec || !M.NODE_TYPES.includes(spec.type)) return null;
    const data = spec.type === "tab" && !spec.data?.embedSessionId
      ? { ...(spec.data || {}), embedSessionId: this.canvas?.settings.embedSessions?.[0]?.id || "" }
      : spec.data;
    return M.createNode(spec.type, {
      x: pos?.x ?? 0, y: pos?.y ?? 0, w: spec.w, h: spec.h, baseW: spec.baseW, baseH: spec.baseH,
      title: spec.title, customTitle: spec.customTitle, color: spec.color, bgColor: spec.bgColor,
      groupId: spec.groupId, layerId: spec.layerId, locks: spec.locks, hideHeader: spec.hideHeader,
      autoFit: spec.autoFit, scale: spec.scale, version: spec.version || this.currentVersion,
      repo: spec.repo || this.repo, data,
    });
  }

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

  addGroup() {
    const members = this.selectedNodes().filter((n) => n.type !== "group");
    const node = M.createNode("group", { version: this.currentVersion, repo: this.repo });
    if (members.length) {
      const b = M.boundsOf(members);
      Object.assign(node, { x: b.x - 24, y: b.y - 48, w: Math.max(M.SIZE_LIMITS.group.minW, b.w + 48), h: Math.max(M.SIZE_LIMITS.group.minH, b.h + 72) });
      this.canvas.nodes.push(node);
      M.reassignGroups(this.canvas.nodes, members);
    } else this.placeNew(node);
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

  // ------------------------------------------------------------------ chrome & dialogs delegation

  chromeHtml() {
    return `
      <div class="nodemap-container" tabindex="0" aria-label="NodeMap canvas">
        <div class="nodemap-toolbar" role="toolbar" aria-label="NodeMap tools">
          <button type="button" class="nodemap-btn" data-tb="add" aria-haspopup="menu" title="Add a kernel node, note or group">+ Add Node</button>
          <button type="button" class="nodemap-btn" data-tb="tool" aria-pressed="${this.tool === "select"}" title="Select tool: drag on the canvas draws a selection box (Shift/Ctrl+drag does it with either tool)">▭ Select</button>
          <button type="button" class="nodemap-btn" data-tb="snap" aria-pressed="${this.canvas.settings.snap}" title="Snap positions and sizes to the grid">Snap</button>
          <div class="nodemap-sep"></div>
          <button type="button" class="nodemap-btn" data-tb="undo" title="Undo (Ctrl+Z)" aria-label="Undo" disabled>↶</button>
          <button type="button" class="nodemap-btn" data-tb="redo" title="Redo (Ctrl+Y / Ctrl+Shift+Z)" aria-label="Redo" disabled>↷</button>
          <div class="nodemap-sep"></div>
          <button type="button" class="nodemap-btn" data-tb="zoom-out" title="Zoom out">−</button>
          <span class="nm-zoom-label" title="Zoom">100%</span>
          <button type="button" class="nodemap-btn" data-tb="zoom-in" title="Zoom in">+</button>
          <button type="button" class="nodemap-btn" data-tb="fit" title="Fit everything into view (Ctrl+1)">Fit</button>
          <button type="button" class="nodemap-btn" data-tb="reset" title="Reset pan and zoom (Ctrl+0)">1:1</button>
          <div class="nodemap-sep"></div>
          <button type="button" class="nodemap-btn" data-tb="layout" title="Arrange nodes by their connections">Auto-Layout</button>
          <button type="button" class="nodemap-btn" data-tb="focus" aria-pressed="${this.focusHops != null}" title="Focus / Trace Mode: highlight N-hop connected subgraph">Focus</button>
          <button type="button" class="nodemap-btn" data-tb="search" title="Find node (Ctrl+F)">Find</button>
          <button type="button" class="nodemap-btn" data-tb="minimap" aria-pressed="${Boolean(this.minimap?.visible)}" title="Toggle corner minimap">Map</button>
          <div class="nodemap-sep"></div>
          <button type="button" class="nodemap-btn" data-tb="library" title="Named Canvas Library &amp; Kernel Starter Templates">Library</button>
          <button type="button" class="nodemap-btn" data-tb="export" aria-haspopup="menu">Export ▾</button>
          <button type="button" class="nodemap-btn" data-tb="import" title="Replace this canvas with an exported JSON file">Import</button>
          <button type="button" class="nodemap-btn" data-tb="shortcuts" title="Keyboard &amp; gesture shortcuts (?)" aria-label="Shortcuts">?</button>
          <input type="file" class="nm-file-input" accept=".json,application/json" hidden />
        </div>
        <div class="nodemap-canvas-wrapper">
          <div class="nodemap-overlay-layer">
            <svg class="nm-wire-layer" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"></svg>
            <div class="nm-marquee" hidden></div>
          </div>
          <div class="nm-breadcrumbs" hidden></div>
          <div class="nm-empty-hint" hidden>
            <b>Empty canvas</b>
            <span>+ Add Node, double-click to write a note, or use “Send to NodeMap” in source, man page and tab menus.</span>
            <div class="nm-empty-actions">
              <button type="button" class="code-btn" data-empty="template">Use Kernel Template…</button>
              <button type="button" class="code-btn" data-empty="library">Open Library…</button>
              <button type="button" class="code-btn" data-empty="impact">Build Impact Map…</button>
            </div>
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
      else if (act === "focus") this.cycleFocusHops();
      else if (act === "search") this.searchBar?.open();
      else if (act === "minimap") {
        this.minimap?.toggle();
        btn.setAttribute("aria-pressed", String(Boolean(this.minimap?.visible)));
      } else if (act === "library") this.openLibraryDialog("library");
      else if (act === "export") this.showExportMenu(r.left, r.bottom + 4);
      else if (act === "import") fileInput.click();
      else if (act === "shortcuts") this.openShortcutsDialog();
    });
    if (this.hintEl) {
      this.hintEl.addEventListener("pointerdown", (e) => { if (e.target.closest?.("[data-empty]")) e.stopPropagation(); });
      this.hintEl.addEventListener("click", (e) => {
        const btn = e.target.closest?.("[data-empty]");
        if (!btn) return;
        e.stopPropagation();
        if (btn.dataset.empty === "template") this.openLibraryDialog("templates");
        else if (btn.dataset.empty === "library") this.openLibraryDialog("library");
        else if (btn.dataset.empty === "impact") this.openImpactMapDialog();
      });
    }
    fileInput.onchange = (e) => {
      this.importJSON(e.target.files?.[0]);
      fileInput.value = "";
    };
    this.wrapEl.classList.toggle("nm-tool-select", this.tool === "select");
  }

  showAddMenu(x, y) { return showAddMenu(this, x, y); }
  showExportMenu(x, y) { return showExportMenu(this, x, y); }
  openNodeDialog(type, existing = null) { return openNodeDialog(this, type, existing); }
  specFromForm(type, f) { return specFromForm(this, type, f); }
  openImageExport(kind) { return openImageExport(this, kind); }
  openFormatExportModal(format) { return openFormatExportModal(this, format); }
  promptCallPath(fromId) { return promptCallPath(this, fromId); }
  promptAttachNode(fromId, fieldKey = null) { return promptAttachNode(this, fromId, fieldKey); }
  promptFieldLock(node, fieldKey) { return promptFieldLock(this, node, fieldKey); }
  openLibraryDialog(initialTab = "library") { return openLibraryDialog(this, initialTab); }
  openImpactMapDialog() { return openImpactMapDialog(this); }
  openShortcutsDialog() { return openShortcutsDialog(this); }

  // ------------------------------------------------------------------ bodies

  fetchKeyOf(node) {
    const d = node.data;
    if (node.type === "struct") return JSON.stringify(["struct", node.repo, node.version, d.name, d.kind, d.path, d.hint, d.line]);
    if (node.type === "source") return JSON.stringify(["source", node.repo, node.version, d.path, d.mode, d.start, d.end, d.symbol, d.symbolKind, d.context]);
    if (node.type === "diff") return JSON.stringify(["diff", node.repo, node.version, d.mode, d.versionA, d.versionB, d.path, d.oldPath, d.commitHash]);
    if (node.type === "mail") return JSON.stringify(["mail", d.tid, d.mid, d.single]);
    if (node.type === "kconfig") return JSON.stringify(["kconfig", node.repo, node.version, d.symbol, d.arch]);
    if (node.type === "maintainer") return JSON.stringify(["maintainer", node.repo, node.version, d.secId, d.query, d.path]);
    if (node.type === "cve") return JSON.stringify(["cve", node.repo, node.version, d.cveId, d.release, d.filePath]);
    if (node.type === "kapi") return JSON.stringify(["kapi", node.repo, node.version, d.catalog, d.query, d.symbol, d.arch]);
    if (node.type === "doc") return JSON.stringify(["doc", node.repo, node.version, d.path, d.mode, d.functions]);
    if (node.type === "history") return JSON.stringify(["history", node.repo, node.version, d.mode, d.path, d.line, d.name, d.astId]);
    return null;
  }

  rawContentOf(node) {
    const key = this.fetchKeyOf(node);
    if (!key) return null;
    const entry = this.content.get(key);
    return entry?.status === "ok" ? entry.value : null;
  }

  isNodeVisibleInLayer(node) { return M.isLayerVisible(this.canvas?.settings, node?.layerId); }

  modelOf(node) {
    const entry = this.content.get(this.fetchKeyOf(node));
    if (entry?.status !== "ok") return null;
    const v = entry.value;
    if (node.type === "mail") return mailThreadModel(v, node.data, this.mailBodies);
    if (node.type === "kconfig") return kconfigModel(v?.detail, v?.graph, node.data.symbol);
    if (node.type === "maintainer") return maintainerModel(v);
    if (node.type === "cve") return cveModel(v, node.data);
    if (node.type === "kapi") return kapiModel(v, node.data);
    if (node.type === "doc") return docModel(v, node.data);
    if (node.type === "history") return historyModel(v, node.data);
    return v;
  }

  ensureContent(node, load) {
    const key = this.fetchKeyOf(node);
    let entry = this.content.get(key);
    if (!entry) {
      entry = { status: "loading" };
      this.content.set(key, entry);
      load().then((value) => { entry.status = "ok"; entry.value = value; }, (err) => { entry.status = "error"; entry.error = err; })
        .finally(() => {
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
        const nodeEl = this.renderer?.element?.(node.id);
        if (!node.autoFit && !nodeEl?.classList?.contains?.("nm-culled")) this.refreshPortOffsets(node.id);
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
      } else {
        const r = entry.value;
        const hl = r.symbol && (r.symbol.line_s > r.start_line || r.symbol.line_e < r.end_line)
          ? new Set(Array.from({ length: r.symbol.line_e - r.symbol.line_s + 1 }, (_, i) => r.symbol.line_s + i)) : null;
        const lines = Array.isArray(r.lines) ? r.lines : [];
        if (lines.length > VIRTUAL_THRESHOLD) {
          body.innerHTML = '<div class="nm-src nm-scroll nm-virtual"></div>';
          const src = body.querySelector(".nm-src");
          if (src) {
            if (prevScroll) src.scrollTop = prevScroll;
            const mount = mountVirtualSource(src, { lines, startLine: r.start_line, path: r.path, highlight: hl });
            this.mounts.set(node.id, { dispose: () => mount.dispose() });
            if (this.workerPool?.workerSupported) {
              this.workerPool.tokenizeSource({ lines, path: r.path }, { key: `hl:${node.id}` }).then((res) => {
                if (res?.htmlRows && src.isConnected) mount.setHtmlRows(res.htmlRows);
              }).catch(() => { });
            }
          }
        } else {
          body.innerHTML = `<div class="nm-src nm-scroll">${renderSourceRows(lines, r.start_line, r.path, hl)}</div>`;
          const src = body.querySelector(".nm-src");
          if (src) src.scrollTop = prevScroll;
        }
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
    } else if (["diff", "mail", "kconfig", "maintainer", "cve", "kapi", "doc", "history"].includes(node.type)) {
      const prevScroll = body.querySelector(".nm-scroll")?.scrollTop || 0;
      const loaders = {
        diff: () => this.loadDiff(node), mail: () => this.loadMail(node), kconfig: () => this.loadKconfig(node),
        maintainer: () => this.loadMaintainer(node), cve: () => this.loadCve(node), kapi: () => this.loadKapi(node),
        doc: () => this.loadDoc(node), history: () => this.loadHistory(node),
      };
      const entry = this.ensureContent(node, loaders[node.type]);
      if (entry.status === "loading") body.innerHTML = status(`Loading ${M.displayTitle(node)}…`);
      else if (entry.status === "error") body.innerHTML = status(entry.error?.message || "Could not load it", true);
      else {
        const model = this.modelOf(node);
        if (node.type === "diff") {
          const rows = diffRows(model);
          if (rows.length > VIRTUAL_THRESHOLD) {
            body.innerHTML = '<div class="nm-diff nm-scroll nm-virtual"></div>';
            const sc = body.querySelector(".nm-diff");
            if (sc) {
              if (prevScroll) sc.scrollTop = prevScroll;
              const mount = mountVirtualDiff(sc, { rows });
              this.mounts.set(node.id, { dispose: () => mount.dispose() });
            }
          } else body.innerHTML = renderDiffBody(model);
        } else if (node.type === "mail") body.innerHTML = renderMailBody(model);
        else if (node.type === "maintainer") body.innerHTML = renderMaintainerBody(model);
        else if (node.type === "cve") body.innerHTML = renderCveBody(model);
        else if (node.type === "kapi") body.innerHTML = renderKapiBody(model);
        else if (node.type === "doc") body.innerHTML = renderDocBody(model);
        else if (node.type === "history") body.innerHTML = renderHistoryBody(model);
        else {
          const values = this.kconfigValuesFor(node);
          const onCanvas = new Set(this.canvas.nodes.filter((n) => n.type === "kconfig" && n.id !== node.id).map((n) => n.data.symbol));
          body.innerHTML = renderKconfigBody(model, values ? kconfigValue(values, model.name) : null, onCanvas);
        }
        const sc = body.querySelector(".nm-scroll");
        if (sc && !sc.classList?.contains?.("nm-virtual")) sc.scrollTop = prevScroll;
        // mail messages and history rows anchor edges like struct members
        if (node.type === "mail" || node.type === "history") {
          const nodeEl = this.renderer?.element?.(node.id);
          if (!node.autoFit && !nodeEl?.classList?.contains?.("nm-culled")) this.refreshPortOffsets(node.id);
          else this.portOffsets.delete(node.id);
        }
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
    this.portOffsets.delete(id);
    this.highlightTokens.delete(id);
    this.workerPool?.cancel?.(`hl:${id}`);
  }

  async loadStruct(node) {
    const d = node.data;
    const client = this.clientFor(node);
    const opts = { path: d.path, kind: d.kind, line: d.line, fresh: this.takeFresh(node.id) };
    try {
      return await client.getNodemapStruct(node.version, d.name, opts);
    } catch (err) {
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
    const res = d.single ? await client.getMailMessage(d.mid) : await client.getMailThread(d.tid || 0, d.mid || "");
    if (d.single && res?.body && d.mid) {
      this.mailBodies.set(d.mid, String(res.body));
      // the whole message is saved with the canvas: it shows (and exports) before a refetch
      const full = M.normalizeData("mail", { ...d, fullBody: fullMailBody(res.body) }).fullBody;
      const mid = d.mid;
      if (full !== d.fullBody) {
        setTimeout(() => {
          if (node.data.mid !== mid || !node.data.single || node.data.fullBody === full) return;
          node.data.fullBody = full;
          if (this.containerEl?.isConnected) this.commit({ absorb: true });
        }, 0);
      }
    }
    if (res && res.available === false) throw new Error(res.error || "Not in the mailing-list archive");
    const tid = res?.tid || res?.messages?.[0]?.tid || null;
    const subject = res?.subject || res?.messages?.[0]?.subject || "";
    if ((tid && !d.tid) || (subject && !d.subject)) {
      setTimeout(() => {
        if (tid && !node.data.tid) node.data.tid = tid;
        if (subject && !node.data.subject) node.data.subject = subject;
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

  async loadCve(node) {
    const d = node.data;
    const client = this.clientFor(node);
    if (d.filePath && !d.cveId) return client.getFileCves(node.version || this.currentVersion, d.filePath);
    if (!d.cveId) throw new Error("Set a CVE ID or file path in the inspector");
    return client.getCveDetail(d.cveId, d.release || "");
  }

  async loadKapi(node) {
    const d = node.data;
    const client = this.clientFor(node);
    const v = node.version || this.currentVersion;
    const cat = d.catalog || "summary";
    if (cat === "summary") return client.getKapi(v, "summary");
    if (cat === "syscalls") return client.getKapi(v, `syscalls?arch=${encodeURIComponent(d.arch || "x86_64")}`);
    if (cat === "exports") {
      return d.symbol
        ? client.request(`/api/kapi/${encodeURIComponent(v)}/exports/${encodeURIComponent(d.symbol)}?arch=${encodeURIComponent(d.arch || "x86_64")}`)
        : client.getKapi(v, "exports");
    }
    if (cat === "params") return client.getKapi(v, `params?arch=${encodeURIComponent(d.arch || "x86_64")}`);
    if (cat === "tracepoints") return client.getKapi(v, "tracepoints");
    if (cat === "modules") return client.getKapi(v, `modules?arch=${encodeURIComponent(d.arch || "x86_64")}`);
    if (cat === "devices") return client.getKapi(v, `devices?arch=${encodeURIComponent(d.arch || "x86_64")}${d.query ? `&q=${encodeURIComponent(d.query)}` : ""}`);
    return client.getKapi(v, "summary");
  }

  async loadDoc(node) {
    const d = node.data;
    const client = this.clientFor(node);
    const v = node.version || this.currentVersion;
    if (!d.path) throw new Error("Set a documentation or source file path in the inspector");
    if (d.mode === "rst" || (d.mode !== "kernel-doc" && isDocumentationPath(d.path))) {
      try {
        const fc = await client.getFileContent(v, d.path);
        if (fc && typeof fc.content === "string") return { path: d.path, content: fc.content };
      } catch {
        // Fallback to kernel-doc extraction
      }
    }
    return client.getKernelDoc(v, d.path, { functions: d.functions || "" });
  }

  async loadHistory(node) {
    const d = node.data;
    const client = this.clientFor(node);
    const v = node.version || this.currentVersion;
    if (d.mode === "blame") {
      if (!d.path) throw new Error("Set a file path in the inspector");
      return client.getFileBlame(v, d.path);
    }
    if (!d.path) throw new Error("Set a file path in the inspector");
    return client.getSymbolHistory(v, d.path, d.line || 1, d.astId || null, { name: d.name || "" });
  }

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
        Object.assign(node.data, { repo: r.repo, version: r.version, path: r.path, name: r.name || d.name, section: r.section || d.section });
        this.commit({ absorb: true });
      } catch (err) {
        if (this.mounts.get(node.id) === token) body.innerHTML = `<div class="nm-status nm-error">${escapeHtml(err.message)}</div>`;
      }
      return;
    }
    body.innerHTML = '<div class="nm-man nm-scroll"></div>';
    const local = { manMode: d.mode || undefined };
    await mountManDocument(body.firstElementChild, {
      repo: d.repo, version: d.version, path: d.path, tab: local, canGoBack: false,
      onNavigate: (path) => { Object.assign(node.data, { path, name: "", section: "" }); this.commit(); },
      onShowSource: () => state.openTab({ type: "code", title: d.path.split("/").pop(), path: d.path, repo: d.repo, version: d.version }),
    }).catch((err) => {
      if (this.mounts.get(node.id) === token) body.innerHTML = `<div class="nm-status nm-error">${escapeHtml(err.message)}</div>`;
    });
    body.querySelector(".man-body")?.classList.add("nm-scroll");
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
    if (node.type === "struct") s += b("expand-types", "⇶", "Expand Types: add or connect every non-standard type the members use");
    if (["struct", "source", "man", "diff", "mail", "kconfig", "maintainer", "cve", "kapi", "doc", "history"].includes(node.type)) s += b("refresh", "⟳", "Load again");
    if (M.TAB_OPENABLE_TYPES.includes(node.type)) s += b("open-tab", "↗", "Open as a tab");
    return s;
  }

  headMeta(node) {
    if (!["struct", "source", "diff", "mail", "kconfig", "maintainer", "cve", "kapi", "doc", "history"].includes(node.type)) return null;
    return headMetaOf(node.type, this.modelOf(node));
  }

  // ------------------------------------------------------------------ view / commit / history

  byId(id) { return this.canvas.nodes.find((n) => n.id === id) || null; }
  selectedNodes() { return this.canvas.nodes.filter((n) => this.selected.has(n.id)); }

  commit({ save = true, inspector = true, record = save, coalesce = null, coalesceEnd = false, absorb = false } = {}) {
    if (!this.renderer) return;
    if (!this.rootCanvas) this.rootCanvas = this.canvas;
    const ids = new Set(this.canvas.nodes.map((n) => n.id));
    for (const id of [...this.selected]) if (!ids.has(id)) this.selected.delete(id);
    if (this.selectedEdge && !this.canvas.edges.some((e) => e.id === this.selectedEdge)) this.selectedEdge = null;
    if (this.active && !ids.has(this.active)) this.active = null;
    this.rootEl?.classList.toggle("nm-edit", Boolean(this.canvas.settings.editMode));
    this.renderer.sync(this.canvas.nodes, this.selected, this.active, this.canvas.settings);
    this.updateViewportCulling();
    const fitted = this.autoFitPass();
    this.applyBackground();
    this.renderWires(null);
    this.applyFocusStyling();
    this.tourPlayer?.applyHighlights?.();
    this.minimap?.render();
    this.renderBreadcrumbs();
    if (this.hintEl) this.hintEl.hidden = this.canvas.nodes.length > 0;
    this.trackHistory({ record, coalesce, coalesceEnd, absorb: absorb || (fitted && !record) });
    if (inspector) this.renderInspector();
    if (save || fitted) {
      if (save) this._userEdited = true;
      this.scheduleSave();
    }
  }

  trackHistory({ record, coalesce, coalesceEnd = false, absorb }) {
    const h = this.history;
    if (!h || !this.canvas) return;
    const snap = () => M.structuralSnapshot(this.canvas, h.base);
    if (h.base === null) h.absorb(snap());
    else if (absorb) {
      if (!this.gesture) h.absorb(snap());
    } else if (record) h.record(snap(), coalesce, coalesceEnd);
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

  undo() {
    if (this.gesture) return false;
    const snap = this.history.undo(M.structuralSnapshot(this.canvas, this.history.base));
    if (!snap) return false;
    this.restoreSnapshot(snap);
    return true;
  }

  redo() {
    if (this.gesture) return false;
    const snap = this.history.redo(M.structuralSnapshot(this.canvas, this.history.base));
    if (!snap) return false;
    this.restoreSnapshot(snap);
    return true;
  }

  restoreSnapshot(snap) {
    const doc = M.materializeSnapshot(snap);
    const st = this.canvas.settings || {};
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
    if (Array.isArray(doc.tours)) this.canvas.tours = doc.tours;
    this.canvas.settings = { ...doc.settings, editMode: st.editMode, snap: st.snap };
    for (const n of this.canvas.nodes) if (n.autoFit) this.fitQueue.add(n.id);
    this.syncFollowChannel();
    this.applyTransform();
    this.commit({ absorb: true });
  }

  applyBackground() {
    const style = this.wrapEl?.style;
    if (!style) return;
    const c = this.canvas.settings.bgColor;
    if (c) style.setProperty("--nm-canvas-bg", M.bgVar(c, "var(--bg-canvas)"));
    else style.removeProperty?.("--nm-canvas-bg");
  }

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
        this.portOffsets.delete(id);
        const el = this.renderer.element(id);
        if (el && this.renderer.place) this.renderer.place(el, node);
      }
    }
    return changed;
  }

  measureNatural(node) {
    if (node.type === "source") {
      const entry = this.content.get(this.fetchKeyOf(node));
      if (entry?.status === "ok" && (entry.value?.lines?.length || 0) > VIRTUAL_THRESHOLD) {
        return estimateNaturalSourceSize(entry.value.lines, entry.value.start_line || 1);
      }
    } else if (node.type === "diff") {
      const entry = this.content.get(this.fetchKeyOf(node));
      if (entry?.status === "ok") {
        const rows = diffRows(entry.value);
        if (rows.length > VIRTUAL_THRESHOLD) return estimateNaturalSourceSize(rows.map((r) => r.text || ""), 1);
      }
    }
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
    return { w: wraps ? node.baseW : Math.ceil(w) + 2, h: Math.ceil(h) + 2 };
  }

  updateView() { this.commit({ save: false }); }

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
    this.updateViewportCulling();
    this.minimap?.render();
  }

  updateViewportCulling() {
    if (!this.renderer?.updateViewportCulling || !this.wrapEl || !this.canvas) return;
    const r = this.wrapEl.getBoundingClientRect?.();
    if (!r?.width || !r?.height) return;
    const wasCulled = new Set();
    for (const n of this.canvas.nodes) {
      const el = this.renderer.element?.(n.id);
      if (el?.classList?.contains?.("nm-culled")) wasCulled.add(n.id);
    }
    const visRect = visibleCanvasRect(this.canvas.viewport, r.width, r.height);
    this.renderer.updateViewportCulling(this.canvas.nodes, visRect, this.zoom, this.active);
    for (const id of wasCulled) {
      const el = this.renderer.element?.(id);
      if (el && !el.classList?.contains?.("nm-culled")) this.portOffsets.delete(id);
    }
  }

  get zoom() { return this.canvas?.viewport.zoom || 1; }

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

  // ------------------------------------------------------------------ wires & cached field ports

  refreshPortOffsets(nodeId) {
    const el = this.renderer?.element?.(nodeId);
    const node = this.byId(nodeId);
    if (!el?.querySelectorAll || !node) return;
    const z = this.zoom || 1;
    const nr = el.getBoundingClientRect?.();
    if (!nr || !nr.height) return;
    const sc = el.querySelector(".nm-scroll");
    const rows = sc?.getBoundingClientRect?.() || nr;
    const rowsBottom = rows.bottom ?? ((rows.top || 0) + (rows.height || 0));
    const map = new Map();
    for (const row of el.querySelectorAll(ANCHOR_ROW_SEL)) {
      const key = row.dataset?.key;
      if (!key) continue;
      const rr = anchorRectOf(row);
      if (!rr) continue;
      const cy = Math.min(Math.max(rr.top + rr.height / 2, rows.top + 4), rowsBottom - 4);
      map.set(key, (cy - nr.top) / z);
    }
    this.portOffsets.set(nodeId, map);
  }

  fieldPoint(node, key, side) {
    const cached = this.portOffsets.get(node.id)?.get(key);
    if (cached !== undefined) return { x: side === "left" ? node.x : node.x + node.w, y: node.y + cached };
    const el = this.renderer?.element(node.id);
    const row = el?.querySelector(anchorRowSel(key));
    if (!row) return null;
    const z = this.zoom || 1;
    const nr = el.getBoundingClientRect();
    // a culled or not yet laid out node has no geometry to anchor to: measure it again once it shows
    if (!nr?.height || el.classList?.contains?.("nm-culled")) return null;
    const rr = anchorRectOf(row);
    const rows = row.closest(".nm-scroll")?.getBoundingClientRect() || nr;
    const rowsBottom = rows.bottom ?? ((rows.top || 0) + (rows.height || 0));
    const cy = Math.min(Math.max(rr.top + rr.height / 2, rows.top + 4), rowsBottom - 4);
    const relY = (cy - nr.top) / z;
    if (!this.portOffsets.has(node.id)) this.portOffsets.set(node.id, new Map());
    this.portOffsets.get(node.id).set(key, relY);
    return { x: side === "left" ? node.x : node.x + node.w, y: node.y + relY };
  }

  endOf(node, side, field) {
    const point = field ? this.fieldPoint(node, field, side === "left" ? "left" : "right") : null;
    return { rect: M.rectOf(node), side: point ? (side === "left" ? "left" : "right") : side, point };
  }

  _edgeInnerSvg(e, g) {
    const dash = dashArray(e.style);
    const sel = e.id === this.selectedEdge;
    const heads = g.heads.map((h) => `<polygon class="nm-edge-head" points="${h}"/>`).join("");
    const hasLockInLabel = e.lockKind && String(e.label || "").toLowerCase().includes(String(e.lockKind).toLowerCase());
    const edgeText = [e.label, e.lockKind && !hasLockInLabel ? `(🔒 ${e.lockKind})` : ""].filter(Boolean).join(" ");
    const label = edgeText ? `<text class="nm-edge-label" x="${g.mid.x}" y="${g.mid.y - 6}" text-anchor="middle">${escapeHtml(edgeText)}</text>` : "";
    const wps = sel && Array.isArray(e.waypoints) && e.waypoints.length
      ? e.waypoints.map((wp, idx) => `<circle class="nm-edge-wp" data-edge-id="${escapeHtml(e.id)}" data-wp-idx="${idx}" cx="${wp.x}" cy="${wp.y}" r="5"/>`).join("")
      : "";
    return `<path class="nm-edge-hit" d="${g.d}"/><path class="nm-edge-line" d="${g.d}"${dash ? ` stroke-dasharray="${dash}"` : ""}/>${heads}${label}${wps}`;
  }

  renderWires(dirtyNodeIds = null) {
    if (!this.wireSvg || !this.canvas) return;
    const byId = new Map(this.canvas.nodes.map((n) => [n.id, n]));
    if (!this.edgeGeoms) this.edgeGeoms = new Map();
    if (!this.edgeEnds) this.edgeEnds = new Map();

    const p = this.gesture?.kind === "wire" ? this.gesture : null;
    const b = M.boundsOf(this.canvas.nodes) || { x: 0, y: 0, w: 0, h: 0 };
    const pts = p ? [p.cur] : [];
    const x1 = Math.min(b.x, ...pts.map((q) => q.x)) - 400;
    const y1 = Math.min(b.y, ...pts.map((q) => q.y)) - 400;
    const x2 = Math.max(b.x + b.w, ...pts.map((q) => q.x)) + 400;
    const y2 = Math.max(b.y + b.h, ...pts.map((q) => q.y)) + 400;
    Object.assign(this.wireSvg.style, { left: `${x1}px`, top: `${y1}px`, width: `${x2 - x1}px`, height: `${y2 - y1}px` });
    this.wireSvg.setAttribute("viewBox", `${x1} ${y1} ${x2 - x1} ${y2 - y1}`);

    const canPatchDom = typeof this.wireSvg.appendChild === "function"
      && typeof document !== "undefined"
      && typeof document.createElementNS === "function";

    if (canPatchDom) {
      const liveIds = new Set();
      for (const e of this.canvas.edges) {
        const a = byId.get(e.from);
        const target = byId.get(e.to);
        if (!a || !target) continue;
        liveIds.add(e.id);
        let gEl = this.wireEls.get(e.id);
        const edgeSig = `${e.label || ""}|${e.lockKind || ""}|${e.style}|${e.arrow}|${e.curve}|${JSON.stringify(e.waypoints || [])}`;
        const needsGeom = !dirtyNodeIds || !gEl || dirtyNodeIds.has(e.from) || dirtyNodeIds.has(e.to) || gEl._edgeSig !== edgeSig;
        if (!gEl) {
          gEl = document.createElementNS("http://www.w3.org/2000/svg", "g");
          gEl.dataset.edgeId = e.id;
          this.wireEls.set(e.id, gEl);
          this.wireSvg.appendChild(gEl);
        } else if (!gEl.isConnected) {
          this.wireSvg.appendChild(gEl);
        }
        const sel = e.id === this.selectedEdge;
        const layerHidden = !M.isLayerVisible(this.canvas.settings, e.layerId);
        const focusDim = Boolean(this.focusHops != null && this._focusEdges && !this._focusEdges.has(e.id));
        const cls = `nm-edge${sel ? " selected" : ""}${layerHidden ? " nm-layer-hidden" : ""}${focusDim ? " nm-focus-dim" : ""}`;
        if (gEl.getAttribute("class") !== cls) gEl.setAttribute("class", cls);
        const edgeColor = M.accentVar(e.color, "var(--accent-blue)");
        if (gEl.style?.getPropertyValue?.("--nm-edge") !== edgeColor) gEl.style?.setProperty?.("--nm-edge", edgeColor);

        if (needsGeom || sel !== Boolean(gEl._wasSel)) {
          gEl._wasSel = sel;
          gEl._edgeSig = edgeSig;
          const fromEnd = this.endOf(a, e.fromSide, e.fromField);
          const toEnd = this.endOf(target, e.toSide, e.toField);
          const g = computeEdgeGeometry(fromEnd, toEnd, { curve: e.curve, arrow: e.arrow, waypoints: e.waypoints });
          this.edgeGeoms.set(e.id, g);
          this.edgeEnds.set(e.id, [[a.id, fromEnd.point ? e.fromField : null, g.fromSide], [target.id, toEnd.point ? e.toField : null, g.toSide]]);
          gEl.innerHTML = this._edgeInnerSvg(e, g);
        }
      }
      for (const [id, gEl] of this.wireEls) {
        if (!liveIds.has(id)) {
          gEl.remove?.();
          this.wireEls.delete(id);
          this.edgeGeoms.delete(id);
          this.edgeEnds.delete(id);
        }
      }
      if (p) {
        const from = byId.get(p.from);
        if (from) {
          if (!this.pendingWireEl) {
            this.pendingWireEl = document.createElementNS("http://www.w3.org/2000/svg", "g");
            this.pendingWireEl.setAttribute("class", "nm-edge nm-edge-pending");
            this.wireSvg.appendChild(this.pendingWireEl);
          }
          const end = { rect: { x: p.cur.x, y: p.cur.y, w: 0, h: 0 }, side: "auto", point: p.cur };
          const g = computeEdgeGeometry(this.endOf(from, p.side, p.field), end, { curve: "bezier", arrow: "forward" });
          this.pendingWireEl.innerHTML = `<path class="nm-edge-line" d="${g.d}" stroke-dasharray="5 4"/>`;
        }
      } else if (this.pendingWireEl) {
        this.pendingWireEl.remove?.();
        this.pendingWireEl = null;
      }
      const ends = [];
      for (const pair of this.edgeEnds.values()) ends.push(...pair);
      this.tagConnectedPorts(ends);
      return;
    }

    let s = "";
    this.edgeGeoms = new Map();
    const ends = [];
    for (const e of this.canvas.edges) {
      const a = byId.get(e.from);
      const target = byId.get(e.to);
      if (!a || !target) continue;
      const fromEnd = this.endOf(a, e.fromSide, e.fromField);
      const toEnd = this.endOf(target, e.toSide, e.toField);
      const g = computeEdgeGeometry(fromEnd, toEnd, { curve: e.curve, arrow: e.arrow, waypoints: e.waypoints });
      this.edgeGeoms.set(e.id, g);
      ends.push([a.id, fromEnd.point ? e.fromField : null, g.fromSide], [target.id, toEnd.point ? e.toField : null, g.toSide]);
      const sel = e.id === this.selectedEdge ? " selected" : "";
      const layerHidden = !M.isLayerVisible(this.canvas.settings, e.layerId) ? " nm-layer-hidden" : "";
      s += `<g class="nm-edge${sel}${layerHidden}" data-edge-id="${escapeHtml(e.id)}" style="--nm-edge:${M.accentVar(e.color, "var(--accent-blue)")}">${this._edgeInnerSvg(e, g)}</g>`;
    }
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

  scheduleWires(dirtyNodeIds = null) {
    if (!dirtyNodeIds) this.dirtyWireNodes = null;
    else if (this.dirtyWireNodes instanceof Set) {
      for (const id of dirtyNodeIds) this.dirtyWireNodes.add(id);
    } else if (this.dirtyWireNodes !== null || !this.wireFrame) {
      this.dirtyWireNodes = new Set(dirtyNodeIds);
    }
    if (this.wireFrame) return;
    this.wireFrame = requestAnimationFrame(() => {
      this.wireFrame = null;
      const dirty = this.dirtyWireNodes;
      this.dirtyWireNodes = null;
      this.renderWires(dirty);
    });
  }

  // ------------------------------------------------------------------ gestures, inline editing & graph actions (delegated)

  attachCanvasEvents() { return attachCanvasEvents(this); }
  onPointerDown(e) { return onPointerDown(this, e); }
  clearTextSelection() { return clearTextSelection(this); }
  selectOnly(id, e = null) { return selectOnly(this, id, e); }
  selectEdge(id) { return selectEdge(this, id); }
  onPointerMove(e) { return onPointerMove(this, e); }
  onPointerUp(e) { return onPointerUp(this, e); }
  finishWire(g, e) { return finishWire(this, g, e); }
  onDblClick(e) { return onDblClick(this, e); }
  onWheel(e) { return onWheel(this, e); }
  onKeyDown(e) { return onKeyDown(this, e); }
  onContextMenu(e) { return onContextMenu(this, e); }
  onClick(e) { return onClick(this, e); }
  _spawnLinkedNode(from, spec, label = "") { return spawnLinkedNode(this, from, spec, label); }
  _spawnFromCveDiff(node, key) { return spawnFromCveDiff(this, node, key); }
  toggleMailMessage(id, mid, mode = "open") { return toggleMailMessage(this, id, mid, mode); }
  mailNodeOf(mid) { return mailNodeOf(this, mid); }
  spawnMailMessages(id, mids, opts) { return spawnMailMessages(this, id, mids, opts); }
  expandThreadToNodes(id) { return expandThreadToNodes(this, id); }
  expandKconfigRelations(id, only = "") { return expandKconfigRelations(this, id, only); }
  toggleExpandNode(id) { return toggleExpandNode(this, id); }
  onStructAction(node, act, key) { return onStructAction(this, node, act, key); }
  inlineInput(host, value, opts) { return inlineInput(this, host, value, opts); }
  beginTitleEdit(id) { return beginTitleEdit(this, id); }
  setTitle(id, value) { return setTitle(this, id, value); }
  beginNoteEdit(id) { return beginNoteEdit(this, id); }
  beginEdgeLabelEdit(id) { return beginEdgeLabelEdit(this, id); }
  followableTypeFor(nodeId, fieldKey) { return followableTypeFor(this, nodeId, fieldKey); }
  followStructType(nodeId, fieldKey) { return followStructType(this, nodeId, fieldKey); }
  expandableTypesOf(id) { return expandableTypesOf(this, id); }
  expandStructTypes(id) { return expandStructTypes(this, id); }
  expandCallgraph(id, direction = "both") { return expandCallgraph(this, id, direction); }
  expandCallPath(fromId, targetFn, opts) { return expandCallPath(this, fromId, targetFn, opts); }

  // ------------------------------------------------------------------ node & selection operations

  setEditMode(on) {
    this.canvas.settings.editMode = Boolean(on);
    for (const n of this.canvas.nodes) if (n.autoFit && n.hideHeader) this.fitQueue.add(n.id);
    this.commit({ absorb: true });
  }

  setHeaderHidden(id, hidden) {
    const node = this.byId(id);
    if (!node) return;
    node.hideHeader = Boolean(hidden);
    if (node.autoFit) this.fitQueue.add(id);
    this.commit();
  }

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

  removeNode(id) { M.removeNodes(this.canvas, [id]); this.selected.delete(id); this.commit(); }
  removeEdge(id) { this.canvas.edges = this.canvas.edges.filter((e) => e.id !== id); this.commit(); }

  clearAll() {
    this.canvas.nodes = [];
    this.canvas.edges = [];
    this.selected.clear();
    this.selectedEdge = null;
    this.commit();
  }

  setColor(color, { inspector = true, coalesce = null, coalesceEnd = false } = {}) {
    const c = M.normalizeColor(color);
    if (this.selectedEdge) {
      const e = this.canvas.edges.find((x) => x.id === this.selectedEdge);
      if (e) e.color = c;
    } else for (const n of this.selectedNodes()) n.color = c;
    this.commit({ inspector, coalesce, coalesceEnd });
  }

  setBackground(color, { inspector = true, coalesce = null, coalesceEnd = false } = {}) {
    const c = M.normalizeColor(color);
    for (const n of this.selectedNodes()) n.bgColor = c;
    this.commit({ inspector, coalesce, coalesceEnd });
  }

  setCanvasBackground(color, { inspector = true, coalesce = null, coalesceEnd = false } = {}) {
    this.canvas.settings.bgColor = M.normalizeColor(color);
    this.commit({ inspector, coalesce, coalesceEnd });
  }

  insertClones({ nodes, edges }) {
    if (!nodes.length) return [];
    const ids = new Set(nodes.map((n) => n.id));
    this.canvas.nodes.push(...nodes);
    this.canvas.edges.push(...edges);
    M.reassignGroups(this.canvas.nodes, nodes.filter((n) => !(n.groupId && ids.has(n.groupId))));
    this.selected = new Set(nodes.filter((n) => !(n.groupId && ids.has(n.groupId))).map((n) => n.id));
    this.selectedEdge = null;
    this.commit();
    return nodes;
  }

  duplicateSelection(ids = [...this.selected]) {
    if (!ids.length) return [];
    return this.insertClones(M.cloneSubgraph(this.canvas, ids, { x: PASTE_STEP, y: PASTE_STEP }));
  }

  duplicateNode(id) {
    if (!this.selected.has(id)) this.selected = new Set([id]);
    return this.duplicateSelection();
  }

  align(mode) { M.alignNodes(this.selectedNodes(), mode); this.commit(); }
  distribute(axis) { M.distributeNodes(this.selectedNodes(), axis); this.commit(); }

  refreshNode(id) {
    const node = this.byId(id);
    if (!node) return;
    const key = this.fetchKeyOf(node);
    if (key) this.content.delete(key);
    this.fresh = new Set([...(this.fresh || []), id]);
    this.renderer.invalidate(id);
    this.commit({ save: false });
  }

  tabOfNode(node) {
    const entry = this.content.get(this.fetchKeyOf(node));
    return tabForNode(node, entry?.status === "ok" ? entry.value : null, { repo: this.repo, version: this.currentVersion });
  }

  openNodeAsTab(id) {
    const node = this.byId(id);
    if (!node) return;
    if (node.type === "tab" && node.data.hash) return urlSync.applyAddress(node.data.hash);
    const tab = this.tabOfNode(node);
    if (tab) state.openTab(tab);
  }

  // ------------------------------------------------------------------ waypoints, focus, sub-maps & layout

  addEdgeWaypointAt(edgeId, pt) {
    const edge = this.canvas.edges.find((e) => e.id === edgeId);
    const g = this.edgeGeoms?.get(edgeId);
    if (!edge || !pt) return false;
    const p = { x: Math.round(pt.x), y: Math.round(pt.y) };
    const wps = Array.isArray(edge.waypoints) ? [...edge.waypoints] : [];
    const pts = g?.p1 && g?.p2 ? [g.p1, ...wps, g.p2] : (g?.points || []);
    const idx = pts.length >= 2 ? nearestWaypointSegment(pts, p) : wps.length;
    wps.splice(Math.min(idx, wps.length), 0, p);
    edge.waypoints = wps;
    this.selectedEdge = edge.id;
    this.commit();
    return true;
  }

  removeEdgeWaypoint(edgeId, idx) {
    const edge = this.canvas.edges.find((e) => e.id === edgeId);
    if (!edge || !Array.isArray(edge.waypoints) || idx < 0 || idx >= edge.waypoints.length) return false;
    edge.waypoints.splice(idx, 1);
    this.commit();
    return true;
  }

  openCallSiteSource(edge) {
    const cs = edge?.callSite;
    if (!cs?.path) return false;
    const src = this.byId(edge.from);
    state.openTab({
      type: "code", title: cs.path.split("/").pop(), path: cs.path, cursorLine: cs.line || 1,
      version: src?.version || this.currentVersion, repo: src?.repo || this.repo,
    });
    return true;
  }

  setFocusHops(hops) {
    this.focusHops = hops == null ? null : hops;
    if (this.focusHops != null && this.selected.size) this.focusSeedIds = new Set(this.selected);
    if (this.focusHops == null) this.focusSeedIds.clear();
    const tb = this.rootEl?.querySelector?.("[data-tb='focus']");
    tb?.setAttribute?.("aria-pressed", String(this.focusHops != null));
    this.commit({ save: false });
  }

  cycleFocusHops() {
    const order = [null, 1, 2, Infinity];
    this.setFocusHops(order[(order.indexOf(this.focusHops) + 1) % order.length]);
  }

  applyFocusStyling() {
    this._focusNodes = null;
    this._focusEdges = null;
    const seeds = this.selected.size ? [...this.selected] : [...this.focusSeedIds];
    if (this.focusHops != null && seeds.length) {
      const sub = M.connectedSubgraph(this.canvas, seeds, this.focusHops);
      this._focusNodes = sub.nodeIds;
      this._focusEdges = sub.edgeIds;
    }
    const toggleCls = (cl, name, on) => {
      if (!cl) return;
      if (typeof cl.toggle === "function") cl.toggle(name, on);
      else if (on) cl.add?.(name);
      else cl.remove?.(name);
    };
    for (const n of this.canvas?.nodes || []) {
      const el = this.renderer?.element?.(n.id);
      if (el?.classList) toggleCls(el.classList, "nm-focus-dim", Boolean(this._focusNodes && !this._focusNodes.has(n.id)));
    }
    for (const [eid, gEl] of this.wireEls || []) {
      if (gEl?.classList) toggleCls(gEl.classList, "nm-focus-dim", Boolean(this._focusEdges && !this._focusEdges.has(eid)));
    }
  }

  onTourStateChanged() { this.renderInspector(); }

  _resetViewForCanvasSwitch() {
    this.selected.clear();
    this.selectedEdge = null;
    this.active = null;
    this.focusHops = null;
    this.focusSeedIds.clear();
    this.tourPlayer?.stop?.();
    this.rootEl?.querySelector?.("[data-tb='focus']")?.setAttribute?.("aria-pressed", "false");
    clearAlignmentGuides(this);
    this.wireEls.clear();
    this.edgeEnds.clear();
    this.edgeGeoms?.clear?.();
    this.pendingWireEl = null;
    this.portOffsets.clear();
    if (this.wireSvg && "innerHTML" in this.wireSvg) this.wireSvg.innerHTML = "";
  }

  enterSubMap(groupId) {
    const grp = this.byId(groupId);
    if (!grp || grp.type !== "group") return false;
    const had = Boolean(grp.data?.subCanvas);
    M.convertGroupToSubMap(grp);
    if (!this.rootCanvas) this.rootCanvas = this.canvas;
    this.subMapPath.push(grp.id);
    this.canvas = grp.data.subCanvas;
    this._resetViewForCanvasSwitch();
    this.history.reset(M.structuralSnapshot(this.canvas));
    this.syncFollowChannel();
    this.applyTransform();
    this.commit({ save: !had, absorb: had });
    return true;
  }

  exitSubMapTo(depth = 0) {
    if (!this.rootCanvas) return false;
    const d = Math.max(0, Math.min(this.subMapPath.length, Number(depth) || 0));
    this.subMapPath = this.subMapPath.slice(0, d);
    const resolved = M.resolveSubMapAtPath(this.rootCanvas, this.subMapPath);
    this.canvas = resolved.canvas;
    this._resetViewForCanvasSwitch();
    this.history.reset(M.structuralSnapshot(this.canvas));
    this.syncFollowChannel();
    this.applyTransform();
    this.commit({ save: false, absorb: true });
    return true;
  }

  navigateToSubMapPath(pathIds = []) {
    if (!this.rootCanvas) this.rootCanvas = this.canvas;
    const targetPath = Array.isArray(pathIds) ? pathIds.map(String) : [];
    const resolved = M.resolveSubMapAtPath(this.rootCanvas, targetPath);
    this.subMapPath = resolved.trail.map((t) => t.id);
    this.canvas = resolved.canvas;
    this._resetViewForCanvasSwitch();
    this.history.reset(M.structuralSnapshot(this.canvas));
    this.syncFollowChannel();
    this.applyTransform();
    this.commit({ save: false, absorb: true });
    return this.subMapPath;
  }

  renderBreadcrumbs() {
    if (!this.breadcrumbsEl || !this.rootCanvas) return;
    if (!this.subMapPath.length) {
      this.breadcrumbsEl.hidden = true;
      this.breadcrumbsEl.innerHTML = "";
      return;
    }
    const { trail } = M.resolveSubMapAtPath(this.rootCanvas, this.subMapPath);
    const parts = ['<button type="button" class="nm-bc-btn" data-bc-depth="0">Root</button>'];
    trail.forEach((t, i) => parts.push(`<span class="nm-bc-sep">/</span><button type="button" class="nm-bc-btn" data-bc-depth="${i + 1}">${escapeHtml(t.title)}</button>`));
    this.breadcrumbsEl.innerHTML = parts.join("");
    this.breadcrumbsEl.hidden = false;
  }

  compareVersion(targetVersion) {
    this.versionDiffTarget = String(targetVersion || "").trim();
    this.versionDiffShown = this.versionDiffTarget;
    this.versionDiffCache.clear();
    for (const n of this.canvas?.nodes || []) if (n.type === "struct") this.renderer?.invalidate(n.id);
    this.commit({ save: false });
  }

  applyTargetVersion(targetVersion, groupId = null) {
    const res = applyCanvasVersion(this.canvas.nodes, targetVersion, { groupId });
    if (res.changedIds.length) {
      this.versionRevertMap = res.previousVersions;
      this.versionDiffCache.clear();
      for (const id of res.changedIds) this.renderer?.invalidate(id);
      this.commit();
    }
    return res.changedIds;
  }

  /**
   * Move the selected nodes to revision `ref` as one undo step (header Apply, multi-selection
   * Revision field). Null when nothing is selected (the caller then switches the tab's default
   * revision); else `{ changedIds, level, message }` for a toast. Nodes without a revision are skipped.
   */
  applyVersionToSelection(ref, { toastResult = false } = {}) {
    const target = String(ref || "").trim();
    const nodes = this.selectedNodes();
    if (!target || !nodes.length) return null;
    // The header names refs of the tab's repository (app.js moves a tab across projects otherwise)
    const res = retargetNodeVersions(nodes, target, { repo: this.repo || state.repoOf(this.tab) });
    const label = shortRef(target);
    const plural = (n) => `${n} selected node${n === 1 ? "" : "s"}`;
    let level = "success";
    let message;
    if (res.changedIds.length) {
      this.versionDiffCache.clear();
      for (const id of res.changedIds) {
        this.renderer?.invalidate(id);
        this.portOffsets.delete(id);
      }
      this.commit();
      message = `Updated ${plural(res.changedIds.length)} to ${label}`;
      if (res.unchanged) message += ` (${res.unchanged} already on it)`;
      if (res.skipped) message += ` · ${res.skipped} without a revision skipped`;
      if (res.otherRepo) message += ` · ${res.otherRepo} of another repository skipped`;
    } else if (res.otherRepo) {
      level = "info";
      message = `The ${plural(res.otherRepo)} ${res.otherRepo === 1 ? "belongs" : "belong"} to another repository than ${label}`;
    } else if (res.unchanged) {
      level = "info";
      message = `The ${plural(res.unchanged)} ${res.unchanged === 1 ? "is" : "are"} already on ${label}`;
    } else {
      level = "info";
      message = `The ${plural(res.skipped)} ${res.skipped === 1 ? "has" : "have"} no revision: clear the selection to switch this NodeMap's default revision`;
    }
    const out = { changedIds: res.changedIds, unchanged: res.unchanged, skipped: res.skipped, otherRepo: res.otherRepo, level, message };
    if (toastResult) toast[level]?.(message);
    return out;
  }

  revertTargetVersion() {
    if (!this.versionRevertMap) return [];
    const reverted = revertCanvasVersion(this.canvas.nodes, this.versionRevertMap);
    this.versionRevertMap = null;
    this.versionDiffCache.clear();
    for (const id of reverted) this.renderer?.invalidate(id);
    if (reverted.length) this.commit();
    return reverted;
  }

  autoLayoutDAG(mode = "dag") {
    const active = this.canvas.nodes.filter((n) => n.type !== "group");
    if (!active.length) return;
    const applyPositions = (positions) => {
      const byPos = new Map((positions || []).map((p) => [p.id, p]));
      for (const n of active) {
        const p = byPos.get(n.id);
        if (p) { n.x = p.x; n.y = p.y; }
      }
      M.reassignGroups(this.canvas.nodes, active);
      this.commit();
      this.fitView();
      toast.success(`Arranged ${active.length} nodes (${mode === "force" ? "force-directed" : "layered DAG"})`);
    };
    if (!this.workerPool?.workerSupported) {
      applyPositions(layoutGraphJob({ nodes: this.canvas.nodes, edges: this.canvas.edges, mode }).positions);
      return;
    }
    return this.workerPool.computeLayout({ nodes: this.canvas.nodes, edges: this.canvas.edges, mode }, { key: "layout" })
      .then((res) => { if (this.containerEl?.isConnected && res?.positions) applyPositions(res.positions); })
      .catch(() => { });
  }

  // ------------------------------------------------------------------ inspector & SVG options

  pickerValue(color, fallback = "var(--accent-blue)") { return pickerValue(color, fallback); }
  renderInspector() { return renderInspector(this); }
  onInspectorInput(e, final) { return onInspectorInput(this, e, final); }
  onInspectorClick(e) { return onInspectorClick(this, e); }

  loadCanvasDocument(doc, { record = true, fit = true } = {}) {
    const canvas = M.normalizeCanvas(doc);
    if (!canvas) return false;
    if (record && this.subMapPath?.length && this.rootCanvas) {
      this.history.reset(M.structuralSnapshot(this.rootCanvas));
    }
    this.rootCanvas = canvas;
    this.subMapPath = [];
    this.canvas = canvas;
    this._resetViewForCanvasSwitch();
    this.syncFollowChannel();
    this.applyTransform();
    this.rootEl?.querySelector?.("[data-tb='snap']")?.setAttribute?.("aria-pressed", String(canvas.settings.snap));
    this.commit({ save: true, record });
    if (fit && canvas.nodes.length) this.fitView(false);
    return true;
  }

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

  structRowHeights(node) {
    if (node.type !== "struct") return null;
    const rows = this.renderer?.element?.(node.id)?.querySelector?.(".nm-struct-rows");
    if (!rows?.children) return null;
    const hs = [...rows.children].filter((el) => el.classList?.contains("nm-sf")).map((el) => el.offsetHeight || 0);
    return hs.length && hs.every((h) => h > 0) ? hs : null;
  }

  canvasSvg(opts = {}) {
    return canvasToSvg(this.canvas, (c) => resolveColor(c), {
      ...this.svgOptions({ foreign: opts.foreign !== false }),
      transparent: Boolean(opts.transparent),
      pixelScale: opts.pixelScale || 1,
    });
  }
}
