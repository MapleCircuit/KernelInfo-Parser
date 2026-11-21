/**
 * nodemap_controller.js - NodeMap spatial canvas (`webapp/API_FRONTEND.md` §5.3).
 *
 * One controller per NodeMap tab. It owns the tab's canvas document (canvas_model.js), persisted in
 * localStorage under `kip-nodemap-canvas:<tab id>` and exported/imported as JSON, and drives:
 *   - pan (drag the background, middle button, Space+drag), zoom (wheel, Ctrl+wheel over node content),
 *     marquee selection (Select tool, or Shift/Ctrl+drag), Shift/Ctrl-click multi-select;
 *   - node drag (header; dragging a group moves its members, dropping a node in or out of a group frame
 *     changes its membership), resize from 8 handles with per-type limits, optional snap-to-grid;
 *   - inline titles (double-click), notes, groups, accent colors, align / distribute;
 *   - edges from side ports or struct field ports: anchor sides (or auto), label, arrows, line style, curve;
 *   - content nodes: struct layouts with type toggles and `#if` guards (`/api/nodemap/{v}/struct/{name}`),
 *     source files / line ranges / symbol slices (`/api/nodemap/{v}/source`), man pages
 *     (`mountManDocument`), and live KernelInfo tabs in an isolated `?embed=1` iframe.
 * Element lookups stay inside the tab's own container (no document ids), so several NodeMap tabs and
 * panes never collide.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { urlSync } from "../../url_sync.js";
import { toast } from "../../components/toast.js";
import { contextMenu } from "../../components/context_menu.js";
import { escapeHtml, openModal } from "../../components/modal.js";
import { resolveColor } from "../../utils/theme.js";
import { onUiScaleChanged } from "../../utils/ui_scale.js";
import { isTabFeatureEnabled } from "../../utils/projects.js";
import { isAlwaysVersionlessTab } from "../../tab_routes.js";
import { mountManDocument } from "../man/man_view.js";
import * as M from "./canvas_model.js";
import { computeEdgeGeometry, dashArray } from "./edge_router.js";
import { NodeRenderer } from "./node_renderer.js";
import { findItem, renderStructBody } from "./struct_view.js";
import { renderNoteMarkdown, renderSourceRows } from "./node_content.js";
import { canvasToSvg } from "./nodemap_export.js";

/** Tab types a KernelInfo tab node offers (feature-gated per project). */
export const EMBED_TAB_TYPES = [
  ["explorer", "Repository Explorer"], ["code", "Source Editor"], ["kconfig", "Kconfig"], ["commits", "Commits"],
  ["maintainers", "Maintainers"], ["mail", "Mailing List"], ["pahole", "Struct Layout"], ["callgraph", "Callgraph"],
  ["diff", "Compare"], ["treemap", "Treemap"], ["search", "Search"], ["api", "Kernel API"], ["insights", "Insights"],
  ["cve", "CVE Records"], ["man", "Man Pages"], ["watch", "Watch List"], ["bookmarks", "Bookmarks"], ["status", "Status"],
];

const ADD_MENU = [
  ["struct", "Struct / Union…", "🧱"], ["source", "Source File / Slice…", "📄"], ["man", "Man Page…", "📘"],
  ["tab", "KernelInfo Tab…", "🗔"], ["symbol", "Symbol…", "🔣"], ["note", "Note Card", "📝"], ["group", "Group", "▭"],
];

const SAVE_DELAY_MS = 300;
const DRAG_THRESHOLD = 3;

const isTyping = (el) => Boolean(el && (el.closest?.("input, textarea, select, [contenteditable='true']")));

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
    }
    // Re-activation of the tab: the canvas DOM built before moves into the new container as it is (loaded
    // struct, source and man bodies, scroll positions and listeners kept; browsers reload moved iframes)
    if (this.rootEl) {
      containerEl.innerHTML = "";
      containerEl.appendChild(this.rootEl);
      this.subscribe();
      this.consumeIncoming(tabData);
      this.applyTransform();
      this.commit({ save: false });
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
    });

    this.attachToolbar();
    this.attachCanvasEvents();
    this.subscribe();
    this.consumeIncoming(tabData);
    this.applyTransform();
    this.commit({ save: false });
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
    return M.createNode(spec.type, {
      x: pos?.x ?? 0,
      y: pos?.y ?? 0,
      w: spec.w,
      h: spec.h,
      title: spec.title,
      customTitle: spec.customTitle,
      color: spec.color,
      version: spec.version || this.currentVersion,
      repo: spec.repo || this.repo,
      data: spec.data,
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
        { label: "SVG image", icon: "🖼", onClick: () => this.exportSVG() },
        { label: "PNG image", icon: "🖼", onClick: () => this.exportPNG() },
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
    form.querySelector("input[name='name'], input[name='path']")?.focus();
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
    return null;
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
      body.innerHTML = `<div class="nm-note nm-scroll">${node.data.markdown ? renderNoteMarkdown(node.data.text) : `<pre class="nm-note-plain">${escapeHtml(node.data.text || "Double-click to write a note")}</pre>`}</div>`;
    } else if (node.type === "struct") {
      const prevScroll = body.querySelector(".nm-struct-rows")?.scrollTop || 0;
      const entry = this.ensureContent(node, () => this.loadStruct(node));
      if (entry.status === "loading") body.innerHTML = status(`Loading ${node.data.kind} ${node.data.name}…`);
      else if (entry.status === "error") {
        body.innerHTML = `${status(entry.error?.message || "Not found", true)}<div class="nm-status">Set the defining file in the inspector to read it from the source (unparsed revisions).</div>`;
      } else {
        body.innerHTML = renderStructBody(entry.value, node.data);
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
        body.innerHTML = `<div class="nm-src-meta">${escapeHtml(r.path)} · ${r.start_line}–${r.end_line} of ${r.total_lines}${r.truncated ? " · truncated" : ""}${r.symbol ? ` · ${escapeHtml(r.symbol.kind)} ${escapeHtml(r.symbol.name)}` : ""}</div>
          <div class="nm-src nm-scroll">${renderSourceRows(lines, r.start_line, r.path, hl)}</div>`;
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
      const src = M.embedSrc(node.data.hash, typeof location !== "undefined" ? location.pathname : "/");
      body.innerHTML = `<div class="nm-tabframe">
          <iframe class="nm-iframe" src="${escapeHtml(src)}" title="${escapeHtml(M.displayTitle(node))}" loading="lazy" referrerpolicy="same-origin"></iframe>
          <button type="button" class="nm-shield" data-act="interact" title="Click to interact with this tab (Esc or a click on the canvas leaves it)">Click to interact</button>
        </div>`;
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
        // Filling in where the page lives changes the content key: the body mounts again with it
        Object.assign(node.data, { repo: r.repo, version: r.version, path: r.path, name: r.name || d.name, section: r.section || d.section });
        this.commit();
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
  }

  headTools(node, active) {
    const b = (act, label, title) => `<button type="button" class="nm-head-btn" data-act="${act}" title="${escapeHtml(title)}">${label}</button>`;
    let s = "";
    if (node.type === "tab") s += active ? b("interact", "✓", "Done interacting (Esc)") : b("interact", "⤢", "Interact with the embedded tab");
    if (node.type === "symbol") s += b("expand", node.data.expanded ? "−" : "+", node.data.expanded ? "Collapse members" : "Expand members");
    if (["struct", "source", "man"].includes(node.type)) s += b("refresh", "⟳", "Load again");
    if (["struct", "source", "man", "tab", "symbol"].includes(node.type)) s += b("open-tab", "↗", "Open as a tab");
    return s;
  }

  // ------------------------------------------------------------------ view / commit

  byId(id) {
    return this.canvas.nodes.find((n) => n.id === id) || null;
  }

  selectedNodes() {
    return this.canvas.nodes.filter((n) => this.selected.has(n.id));
  }

  /** Re-render nodes, wires and (unless asked not to) the inspector; persist unless `save` is false. */
  commit({ save = true, inspector = true } = {}) {
    if (!this.renderer) return;
    const ids = new Set(this.canvas.nodes.map((n) => n.id));
    for (const id of [...this.selected]) if (!ids.has(id)) this.selected.delete(id);
    if (this.selectedEdge && !this.canvas.edges.some((e) => e.id === this.selectedEdge)) this.selectedEdge = null;
    if (this.active && !ids.has(this.active)) this.active = null;
    this.renderer.sync(this.canvas.nodes, this.selected, this.active);
    this.renderWires();
    if (this.hintEl) this.hintEl.hidden = this.canvas.nodes.length > 0;
    if (inspector) this.renderInspector();
    if (save) this.scheduleSave();
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
    const esc = (s) => (globalThis.CSS?.escape ? CSS.escape(s) : String(s).replace(/"/g, '\\"'));
    const row = el?.querySelector(`.nm-sf[data-key="${esc(key)}"]`);
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
    for (const e of this.canvas.edges) {
      const a = byId.get(e.from);
      const b = byId.get(e.to);
      if (!a || !b) continue;
      const g = computeEdgeGeometry(this.endOf(a, e.fromSide, e.fromField), this.endOf(b, e.toSide, e.toField), { curve: e.curve, arrow: e.arrow });
      this.edgeGeoms.set(e.id, g);
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
      Object.assign(node, M.resizeRect(g.rect, g.handle, dxs / z, dys / z, M.SIZE_LIMITS[node.type], snap));
      const el = this.renderer.element(node.id);
      if (el) Object.assign(el.style, { left: `${node.x}px`, top: `${node.y}px`, width: `${node.w}px`, height: `${node.h}px` });
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
      this.finishWire(g, e);
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
    if (act === "interact") {
      this.active = this.active === node.id ? null : node.id;
      this.selected = new Set([node.id]);
      this.commit({ save: false });
    } else if (act === "open-tab") this.openNodeAsTab(node.id);
    else if (act === "refresh") this.refreshNode(node.id);
    else if (act === "expand") this.toggleExpandNode(node.id);
    else if (node.type === "struct") this.onStructAction(node, act, btn.dataset.key);
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
    } else return;
    this.commit();
  }

  onDblClick(e) {
    if (isTyping(e.target)) return;
    const titleEl = e.target.closest(".nm-title");
    const nodeEl = e.target.closest(".nm-node");
    const edgeEl = e.target.closest(".nm-edge[data-edge-id]");
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
    if (e.key === " ") {
      this.spaceDown = true;
      e.preventDefault();
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      this.deleteSelection();
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
      this.commit();
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

  setColor(color) {
    if (this.selectedEdge) {
      const e = this.canvas.edges.find((x) => x.id === this.selectedEdge);
      if (e) e.color = color;
    } else for (const n of this.selectedNodes()) n.color = color;
    this.commit();
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

  openNodeAsTab(id) {
    const node = this.byId(id);
    if (!node) return;
    const d = node.data;
    const base = { repo: node.repo || this.repo, version: node.version || this.currentVersion };
    if (node.type === "source") {
      const line = d.mode === "range" ? d.start : this.content.get(this.fetchKeyOf(node))?.value?.symbol?.line_s;
      state.openTab({ type: "code", title: d.path.split("/").pop(), path: d.path, cursorLine: line || 1, ...(d.mode === "range" && d.end > d.start ? { lineEnd: d.end } : {}), ...base });
    } else if (node.type === "struct") {
      const layout = this.content.get(this.fetchKeyOf(node))?.value;
      if (layout?.file_path) state.openTab({ type: "code", title: layout.file_path.split("/").pop(), path: layout.file_path, cursorLine: layout.line_s, ...base });
      else state.openTab({ type: "pahole", title: `struct ${d.name}`, structName: d.name, ...base });
    } else if (node.type === "man" && d.path) {
      state.openTab({ type: "man", repo: d.repo, version: d.version, manPath: d.path, manPage: d.name, manSec: d.section, title: M.displayTitle(node) });
    } else if (node.type === "tab" && d.hash) {
      urlSync.applyAddress(d.hash);
    } else if (node.type === "symbol") {
      if (d.kind === "func") state.openTab({ type: "callgraph", title: `Callgraph: ${d.name}`, functionName: d.name, ...base });
      else state.openTab({ type: "search", title: `Search: ${d.name}`, query: d.name, word: true, ...base });
    }
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

  renderInspector() {
    if (!this.inspectorEl) return;
    const swatches = (current) => `<div class="nm-swatches" role="group" aria-label="Accent">${M.ACCENTS.map((a) =>
      `<button type="button" class="nm-swatch${a.id === current ? " on" : ""}" data-insp="color" data-value="${a.id}" title="${a.label}" style="--nm-sw:${M.accentVar(a.id, "var(--bg-tertiary)")}"></button>`).join("")}</div>`;
    const sel = (name, value, options) => `<select data-insp="${name}">${options.map((o) => `<option value="${o}"${o === value ? " selected" : ""}>${o || "any"}</option>`).join("")}</select>`;
    const row = (label, inner) => `<label class="inspector-field"><span class="inspector-label">${escapeHtml(label)}</span>${inner}</label>`;
    const input = (name, value, extra = "") => `<input data-insp="${name}" value="${escapeHtml(value ?? "")}" ${extra}/>`;
    let html = "";
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
      html += row("Color", swatches(edge.color));
      html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="edge-reverse">⇄ Reverse</button><button type="button" class="nodemap-btn nm-danger" data-insp="delete">Delete</button></div>`;
    } else if (nodes.length > 1) {
      html += `<div class="inspector-field"><span class="inspector-label">Selection</span><span class="inspector-val">${nodes.length} nodes</span></div>`;
      html += `<div class="inspector-field"><span class="inspector-label">Align</span><div class="nm-insp-grid">${M.ALIGN_MODES.map((m) => `<button type="button" class="nodemap-btn" data-insp="align" data-value="${m}">${m}</button>`).join("")}</div></div>`;
      html += `<div class="inspector-field"><span class="inspector-label">Distribute</span><div class="nm-insp-grid"><button type="button" class="nodemap-btn" data-insp="distribute" data-value="horizontal" ${nodes.length < 3 ? "disabled" : ""}>horizontally</button><button type="button" class="nodemap-btn" data-insp="distribute" data-value="vertical" ${nodes.length < 3 ? "disabled" : ""}>vertically</button></div></div>`;
      html += row("Color", swatches(""));
      html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="group">▭ Group</button><button type="button" class="nodemap-btn" data-insp="fit">Fit</button><button type="button" class="nodemap-btn nm-danger" data-insp="delete">Delete</button></div>`;
    } else if (nodes.length === 1) {
      const n = nodes[0];
      const d = n.data;
      html += row("Title", `<div class="nm-insp-inline">${input("title", M.displayTitle(n))}${n.customTitle ? '<button type="button" class="nodemap-btn" data-insp="reset-title" title="Back to the default title">Reset</button>' : ""}</div>`);
      html += `<div class="nm-insp-pair">${row("Width", input("w", n.w, 'type="number"'))}${row("Height", input("h", n.h, 'type="number"'))}</div>`;
      html += row("Color", swatches(n.color));
      if (["struct", "source", "symbol"].includes(n.type)) html += row("Revision", input("version", n.version, 'spellcheck="false"'));
      if (n.type === "struct") {
        html += row("Name", input("d-name", d.name, 'spellcheck="false"'));
        html += row("Kind", sel("d-kind", d.kind, ["struct", "union"]));
        html += row("Defining file", input("d-path", d.path, `spellcheck="false" placeholder="${escapeHtml(d.hint || "from the index")}"`));
        html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="struct-types" aria-pressed="${d.showTypes}">Types: ${d.showTypes ? "shown" : "hidden"}</button><button type="button" class="nodemap-btn" data-insp="struct-guards" aria-pressed="${d.showGuards}">#ifdef: ${d.showGuards ? "shown" : "hidden"}</button></div>`;
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
        html += `<div class="inspector-field"><span class="inspector-label">Runs isolated: its own state, no session, nothing saved. Click it to interact; Esc or a click on the canvas gives the pointer back.</span></div>`;
      } else if (n.type === "note") {
        html += `<label class="inspector-field nm-insp-check"><input type="checkbox" data-insp="d-markdown" ${d.markdown ? "checked" : ""}/> <span>Render Markdown</span></label>`;
      } else if (n.type === "group") {
        const members = M.groupDescendants(this.canvas.nodes, n.id).length;
        html += `<div class="inspector-field"><span class="inspector-label">Members</span><span class="inspector-val">${members} (drag nodes in or out of the frame)</span></div>`;
      }
      html += `<div class="nm-insp-actions">${["struct", "source", "man", "tab", "symbol"].includes(n.type) ? '<button type="button" class="nodemap-btn" data-insp="open-tab">↗ Open as tab</button>' : ""}<button type="button" class="nodemap-btn nm-danger" data-insp="delete">Delete</button></div>`;
    } else {
      html += `<div class="inspector-field"><span class="inspector-label">Canvas</span><span class="inspector-val">${this.canvas.nodes.length} nodes · ${this.canvas.edges.length} connections</span></div>`;
      html += `<div class="nm-insp-pair">${row("Grid", input("grid", this.canvas.settings.grid, 'type="number" min="4" max="200"'))}<label class="inspector-field nm-insp-check"><input type="checkbox" data-insp="snap" ${this.canvas.settings.snap ? "checked" : ""}/> <span>Snap</span></label></div>`;
      html += `<ul class="nm-help">
        <li>Drag the background to pan; wheel to zoom (Ctrl+wheel over node content)</li>
        <li>Shift/Ctrl+drag or the Select tool draws a selection box; Shift/Ctrl+click adds to it</li>
        <li>Drag a header to move, a corner or edge to resize; double-click a title to rename</li>
        <li>Drag from a side dot (or a struct field dot) onto another node to connect</li>
        <li>Double-click the background for a note; Delete removes, arrows nudge</li>
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
    if (k === "grid" && final) {
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
      if (v) node[k] = k === "w" ? Math.max(lim.minW, Math.min(lim.maxW, v)) : Math.max(lim.minH, Math.min(lim.maxH, v));
    } else if (node && k === "version" && final) {
      node.version = String(val).trim();
    } else if (node && k.startsWith("d-")) {
      if (!final && el.tagName !== "SELECT") return;
      const f = k.slice(2);
      const raw = ["start", "end", "context"].includes(f) ? int(val) : val;
      node.data = M.normalizeData(node.type, { ...node.data, [f]: typeof raw === "string" ? raw.trim() : raw });
      if (!node.customTitle) node.title = M.defaultTitle(node.type, node.data);
    } else return;
    // Typing in a field keeps the inspector (and the caret) as it is
    this.commit({ inspector: final && (el.tagName === "SELECT" || el.type === "checkbox" || k.startsWith("d-")) });
  }

  onInspectorClick(e) {
    const btn = e.target.closest("button[data-insp]");
    if (!btn) return;
    const k = btn.dataset.insp;
    const node = this.selected.size === 1 ? this.byId([...this.selected][0]) : null;
    if (k === "color") this.setColor(btn.dataset.value || "");
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

  exportSVG() {
    this.download("nodemap_canvas.svg", new Blob([canvasToSvg(this.canvas, (c) => resolveColor(c))], { type: "image/svg+xml" }));
    toast.success("Exported SVG image");
  }

  exportPNG() {
    const svg = canvasToSvg(this.canvas, (c) => resolveColor(c));
    const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
    const img = new Image();
    img.onload = () => {
      const c = document.createElement("canvas");
      c.width = Math.min(8000, img.width * 2);
      c.height = Math.min(8000, img.height * 2);
      const ctx = c.getContext("2d");
      ctx.scale(c.width / img.width, c.height / img.height);
      ctx.drawImage(img, 0, 0);
      URL.revokeObjectURL(url);
      c.toBlob((blob) => {
        if (blob) this.download("nodemap_canvas.png", blob);
      }, "image/png");
      toast.success("Exported PNG image");
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      toast.error("PNG export failed");
    };
    img.src = url;
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
      this.applyTransform();
      this.rootEl.querySelector("[data-tb='snap']")?.setAttribute("aria-pressed", String(canvas.settings.snap));
      this.commit();
      toast.success(`Imported ${canvas.nodes.length} nodes and ${canvas.edges.length} connections`);
    };
    reader.readAsText(file);
  }
}
