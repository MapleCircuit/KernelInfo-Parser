/**
 * nodemap_controller.js - Main Two.js Schematic Canvas Controller.
 * Manages pan/zoom canvas, interactive nodes, bezier edge routing,
 * lossless SVG/PNG/JSON export, and hierarchical constituent expansion.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { contextMenu } from "../../components/context_menu.js";
import { NodeModel, EdgeModel } from "./node_model.js";
import { EdgeRouter } from "./edge_router.js";
import { NodeRenderer } from "./node_renderer.js";

export class NodeMapController {
  constructor() {
    this.nodes = [];
    this.edges = [];
    this.selectedNodeId = null;

    // Viewport transform
    this.panX = 0;
    this.panY = 0;
    this.zoom = 1.0;

    this.two = null;
    this.containerEl = null;
    this.canvasWrapperEl = null;
    this.overlayLayerEl = null;
    this.wireLayerSvg = null;
    this.renderer = null;

    this.isPanning = false;
    this.panStartX = 0;
    this.panStartY = 0;
    this.pendingWire = null;
  }

  render(containerEl, tabData) {
    this.containerEl = containerEl;

    containerEl.innerHTML = `
      <div class="nodemap-container">
        <!-- Floating Toolbar -->
        <div class="nodemap-toolbar">
          <button class="nodemap-btn" id="nm-btn-add-node">+ Add Node</button>
          <button class="nodemap-btn" id="nm-btn-auto-layout" title="Hierarchical DAG Auto-Layout">Auto-Layout</button>
          <button class="nodemap-btn" id="nm-btn-zoom-in">Zoom In</button>
          <button class="nodemap-btn" id="nm-btn-zoom-out">Zoom Out</button>
          <button class="nodemap-btn" id="nm-btn-zoom-reset">Reset View</button>
          <div class="nodemap-sep"></div>
          <button class="nodemap-btn" id="nm-btn-export-svg">Export SVG</button>
          <button class="nodemap-btn" id="nm-btn-export-png">Export PNG</button>
          <button class="nodemap-btn" id="nm-btn-export-json">Export JSON</button>
          <button class="nodemap-btn" id="nm-btn-import-json">Import JSON</button>
          <input type="file" id="nm-file-input" style="display:none;" accept=".json" />
        </div>

        <!-- Canvas Area -->
        <div class="nodemap-canvas-wrapper" id="nm-canvas-wrapper">
          <svg id="nm-wire-layer" style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:10;"></svg>
          <div class="nodemap-overlay-layer" id="nm-overlay-layer"></div>
        </div>

        <!-- Inspector Drawer -->
        <div class="nodemap-inspector" id="nm-inspector">
          <div class="inspector-header">Node Inspector</div>
          <div id="nm-inspector-content" style="color:var(--text-muted);font-size:11px;">
            Click on any schematic node to inspect its attributes and constituent members.
          </div>
        </div>
      </div>
    `;

    this.canvasWrapperEl = containerEl.querySelector("#nm-canvas-wrapper");
    this.overlayLayerEl = containerEl.querySelector("#nm-overlay-layer");
    this.wireLayerSvg = containerEl.querySelector("#nm-wire-layer");

    // Initialize Two.js backend if loaded
    if (window.Two) {
      this.two = new window.Two({
        type: window.Two.Types.svg,
        fullscreen: false,
        autostart: true
      }).appendTo(this.canvasWrapperEl);
    }

    // Node Renderer
    this.renderer = new NodeRenderer(this.overlayLayerEl, {
      getZoom: () => this.zoom,
      onDragNode: (id, x, y) => this.handleNodeMoved(id, x, y),
      onSelectNode: (id) => this.selectNode(id),
      onToggleExpand: (id) => this.toggleExpandNode(id),
      onRecolor: (id, color) => this.recolorNode(id, color),
      onPortMouseDown: (nodeId, portId, portEl, e) => this.handlePortMouseDown(nodeId, portId, portEl, e),
      onConstituentContextMenu: (e, node, c, idx) => {
        contextMenu.showNodeMapMenu(e.clientX, e.clientY, {
          node: {
            id: node.id,
            name: node.title,
            label: node.title,
            type: node.type,
            astId: node.astId,
            expanded: node.expanded,
            constituents: node.constituents
          },
          constituent: c,
          constituentIndex: idx,
          controller: this
        });
      }
    });

    // Toolbar buttons
    containerEl.querySelector("#nm-btn-add-node").onclick = () => this.addNewNode();
    containerEl.querySelector("#nm-btn-auto-layout").onclick = () => this.autoLayoutDAG();
    containerEl.querySelector("#nm-btn-zoom-in").onclick = () => this.adjustZoom(1.15);
    containerEl.querySelector("#nm-btn-zoom-out").onclick = () => this.adjustZoom(0.85);
    containerEl.querySelector("#nm-btn-zoom-reset").onclick = () => this.resetTransform();

    containerEl.querySelector("#nm-btn-export-svg").onclick = () => this.exportSVG();
    containerEl.querySelector("#nm-btn-export-png").onclick = () => this.exportPNG();
    containerEl.querySelector("#nm-btn-export-json").onclick = () => this.exportJSON();

    const fileInput = containerEl.querySelector("#nm-file-input");
    containerEl.querySelector("#nm-btn-import-json").onclick = () => fileInput.click();
    fileInput.onchange = (e) => this.importJSON(e.target.files[0]);

    // Canvas panning & zooming
    this.attachCanvasControls();

    // If initial nodes or diagram requested (e.g. from Callgraph, Pahole, or Context Menu)
    if (tabData.initialNodes && Array.isArray(tabData.initialNodes)) {
      this.importDiagram(tabData.initialNodes, tabData.initialEdges || []);
    } else if (tabData.initialNode) {
      this.addSymbolNode(tabData.initialNode);
    } else if (this.nodes.length === 0) {
      // Default sample nodes
      this.loadSampleDiagram();
    } else {
      this.updateView();
    }
  }

  attachCanvasControls() {
    this.canvasWrapperEl.addEventListener("mousedown", (e) => {
      if (e.target === this.canvasWrapperEl || e.target === this.wireLayerSvg) {
        this.isPanning = true;
        this.panStartX = e.clientX - this.panX;
        this.panStartY = e.clientY - this.panY;
      }
    });

    window.addEventListener("mousemove", (e) => {
      if (this.isPanning) {
        this.panX = e.clientX - this.panStartX;
        this.panY = e.clientY - this.panStartY;
        this.applyTransform();
      }
    });

    window.addEventListener("mouseup", () => {
      this.isPanning = false;
    });

    this.canvasWrapperEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      const zoomFactor = e.deltaY < 0 ? 1.1 : 0.9;
      this.adjustZoom(zoomFactor, e.clientX, e.clientY);
    });

    this.canvasWrapperEl.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();

      const nodeEl = e.target.closest(".schematic-node");
      if (nodeEl && nodeEl.dataset.nodeId) {
        const node = this.nodes.find((n) => n.id === nodeEl.dataset.nodeId);
        if (node) {
          const constituentEl = e.target.closest(".node-constituent-item");
          let constituent = null;
          let constituentIndex = -1;
          if (constituentEl && constituentEl.dataset.constituentIndex !== undefined) {
            constituentIndex = parseInt(constituentEl.dataset.constituentIndex, 10);
            constituent = node.constituents?.[constituentIndex] || null;
          }

          contextMenu.showNodeMapMenu(e.clientX, e.clientY, {
            node: {
              id: node.id,
              name: node.title,
              label: node.title,
              type: node.type,
              astId: node.astId,
              expanded: node.expanded,
              constituents: node.constituents
            },
            constituent,
            constituentIndex,
            controller: this
          });
          return;
        }
      }

      contextMenu.showNodeMapMenu(e.clientX, e.clientY, {
        isCanvas: true,
        controller: this
      });
    });
  }

  adjustZoom(factor, clientX = null, clientY = null) {
    const newZoom = Math.max(0.2, Math.min(3.0, this.zoom * factor));
    if (clientX !== null && clientY !== null) {
      const rect = this.canvasWrapperEl.getBoundingClientRect();
      const mouseX = clientX - rect.left;
      const mouseY = clientY - rect.top;
      this.panX = mouseX - (mouseX - this.panX) * (newZoom / this.zoom);
      this.panY = mouseY - (mouseY - this.panY) * (newZoom / this.zoom);
    }
    this.zoom = newZoom;
    this.applyTransform();
  }

  resetTransform() {
    this.panX = 0;
    this.panY = 0;
    this.zoom = 1.0;
    this.applyTransform();
  }

  removeNode(id) {
    this.nodes = this.nodes.filter((n) => n.id !== id);
    this.edges = this.edges.filter((e) => e.fromNodeId !== id && e.toNodeId !== id);
    if (this.selectedNodeId === id) this.selectedNodeId = null;
    this.updateView();
  }

  clearAll() {
    this.nodes = [];
    this.edges = [];
    this.selectedNodeId = null;
    this.updateView();
  }

  centerOnNode(id) {
    const node = this.nodes.find((n) => n.id === id);
    if (!node || !this.canvasWrapperEl) return;
    const w = this.canvasWrapperEl.clientWidth || 800;
    const h = this.canvasWrapperEl.clientHeight || 600;
    this.panX = Math.round(w / 2 - (node.x + (node.width || 180) / 2) * this.zoom);
    this.panY = Math.round(h / 2 - (node.y + 40) * this.zoom);
    this.applyTransform();
  }

  applyTransform() {
    const transformStr = `translate(${this.panX}px, ${this.panY}px) scale(${this.zoom})`;
    this.overlayLayerEl.style.transform = transformStr;
    this.renderWires();
  }

  loadSampleDiagram() {
    const n1 = new NodeModel({
      id: "node-sched",
      title: "schedule()",
      type: "func",
      x: 120,
      y: 100,
      color: "#bc8cff",
      constituents: [{ name: "__schedule()" }, { name: "context_switch()" }]
    });

    const n2 = new NodeModel({
      id: "node-task",
      title: "struct task_struct",
      type: "struct",
      x: 440,
      y: 100,
      color: "#58a6ff",
      constituents: [{ name: "state" }, { name: "stack" }, { name: "flags" }, { name: "prio" }]
    });

    this.nodes = [n1, n2];
    this.edges = [new EdgeModel({ fromNodeId: n1.id, toNodeId: n2.id })];
    this.updateView();
  }

  addNewNode() {
    const name = prompt("Enter Node Title:", "NewSymbol");
    if (!name) return;

    const node = new NodeModel({
      title: name,
      type: "symbol",
      x: Math.round((200 - this.panX) / this.zoom),
      y: Math.round((150 - this.panY) / this.zoom)
    });

    this.nodes.push(node);
    this.updateView();
    toast.success(`Created node: ${name}`);
  }

  addSymbolNode(data) {
    const node = new NodeModel({
      title: data.name,
      type: data.type === 5 || data.type === 6 || data.type === "func" ? "func" : "struct",
      astId: data.astId,
      x: Math.round((200 - this.panX) / this.zoom),
      y: Math.round((150 - this.panY) / this.zoom),
      color: data.type === 5 || data.type === 6 || data.type === "func" ? "#bc8cff" : "#58a6ff",
      constituents: data.constituents || []
    });

    this.nodes.push(node);
    this.updateView();
    this.selectNode(node.id);
  }

  importDiagram(nodesData, edgesData) {
    this.nodes = nodesData.map((d) => new NodeModel(d));
    this.edges = (edgesData || []).map((e) => new EdgeModel(e));
    this.autoLayoutDAG();
  }

  autoLayoutDAG() {
    if (this.nodes.length === 0) return;

    // Build adjacency and compute in-degree
    const inDegree = new Map();
    const adj = new Map();
    this.nodes.forEach((n) => {
      inDegree.set(n.id, 0);
      adj.set(n.id, []);
    });

    this.edges.forEach((e) => {
      if (adj.has(e.fromNodeId) && inDegree.has(e.toNodeId)) {
        adj.get(e.fromNodeId).push(e.toNodeId);
        inDegree.set(e.toNodeId, inDegree.get(e.toNodeId) + 1);
      }
    });

    // Layer assignment (longest-path DAG leveling)
    const layers = new Map();
    this.nodes.forEach((n) => {
      if (inDegree.get(n.id) === 0) {
        layers.set(n.id, 0);
      }
    });

    // Iterative layer relaxation with cycle prevention
    for (let iter = 0; iter < 10; iter++) {
      let changed = false;
      this.edges.forEach((e) => {
        const fromLayer = layers.get(e.fromNodeId) ?? 0;
        const toLayer = layers.get(e.toNodeId) ?? 0;
        if (toLayer <= fromLayer) {
          layers.set(e.toNodeId, fromLayer + 1);
          changed = true;
        }
      });
      if (!changed) break;
    }

    // Default layer for disconnected or unvisited nodes
    this.nodes.forEach((n) => {
      if (!layers.has(n.id)) layers.set(n.id, 0);
    });

    // Group nodes by layer
    const layerBuckets = new Map();
    this.nodes.forEach((n) => {
      const l = layers.get(n.id);
      if (!layerBuckets.has(l)) layerBuckets.set(l, []);
      layerBuckets.get(l).push(n);
    });

    const colWidth = 320;
    const rowHeight = 170;
    const startX = 80;
    const startY = 80;

    layerBuckets.forEach((bucketNodes, l) => {
      bucketNodes.forEach((node, idx) => {
        node.x = startX + l * colWidth;
        node.y = startY + idx * rowHeight;
      });
    });

    this.updateView();
    toast.success(`Arranged ${this.nodes.length} nodes in hierarchical layout`);
  }

  handleNodeMoved(nodeId, x, y) {
    const node = this.nodes.find((n) => n.id === nodeId);
    if (node) {
      node.x = x;
      node.y = y;
      this.renderWires();
      if (this.selectedNodeId === nodeId) {
        const coordEl = this.containerEl.querySelector("#nm-coords-val");
        if (coordEl) coordEl.textContent = `X: ${Math.round(node.x)}, Y: ${Math.round(node.y)}`;
      }
    }
  }

  selectNode(nodeId) {
    this.selectedNodeId = nodeId;
    this.renderer.updateSelection(this.selectedNodeId);

    const node = this.nodes.find((n) => n.id === nodeId);
    const inspectorEl = this.containerEl.querySelector("#nm-inspector-content");
    if (node && inspectorEl) {
      inspectorEl.innerHTML = `
        <div class="inspector-field">
          <span class="inspector-label">Title</span>
          <span class="inspector-val">${node.title}</span>
        </div>
        <div class="inspector-field">
          <span class="inspector-label">Type</span>
          <span class="inspector-val">${node.type}</span>
        </div>
        <div class="inspector-field">
          <span class="inspector-label">Coordinates</span>
          <span class="inspector-val" id="nm-coords-val">X: ${Math.round(node.x)}, Y: ${Math.round(node.y)}</span>
        </div>
        <div class="inspector-field">
          <span class="inspector-label">Constituents (${node.constituents?.length || 0})</span>
          <div style="display:flex;flex-direction:column;gap:4px;margin-top:4px;">
            ${
              (node.constituents || []).map((c) => `
                <div style="font-family:var(--font-mono);font-size:11px;color:var(--accent-blue);">
                  • ${c.name || c.type}
                </div>
              `).join("") || `<span style="color:var(--text-muted);">None loaded</span>`
            }
          </div>
        </div>
      `;
    }
  }

  async toggleExpandNode(nodeId) {
    const node = this.nodes.find((n) => n.id === nodeId);
    if (!node) return;

    node.expanded = !node.expanded;
    if (node.expanded && (!node.constituents || node.constituents.length === 0)) {
      try {
        if (node.astId) {
          const res = await api.getAstTree(state.currentVersion, node.astId);
          const children = res.node?.children || res.children || [];
          node.constituents = children.map((c) => ({ name: c.spelling || c.name || c.type_name }));
        } else if (node.title) {
          const cleanName = node.title.replace(/^struct\s+/, "").replace(/\(\)$/, "").trim();
          if (node.type === "struct" || node.title.startsWith("struct")) {
            const res = await api.getPaholeLayout(state.currentVersion, cleanName);
            if (res && Array.isArray(res.members) && res.members.length > 0) {
              node.constituents = res.members.map((m) => ({ name: `${m.type} ${m.name}` }));
            }
          }
          if (!node.constituents || node.constituents.length === 0) {
            const detail = await api.getSymbolDetail(state.currentVersion, cleanName);
            if (detail && Array.isArray(detail.members) && detail.members.length > 0) {
              node.constituents = detail.members.map((m) => ({ name: m.name || m.spelling }));
            }
          }
        }
      } catch (e) {
        console.warn("Failed to fetch node constituents:", e);
      }
    }

    this.updateView();
  }

  promptAttachNode(fromNodeId, fromPortId = "out") {
    const fromNode = this.nodes.find((n) => n.id === fromNodeId);
    if (!fromNode) return;

    const name = prompt(`Enter Symbol / Node Title to attach to "${fromNode.title}":`, "NewNode");
    if (!name) return;

    const newNode = new NodeModel({
      title: name,
      type: name.startsWith("struct ") ? "struct" : "symbol",
      x: fromNode.x + (fromNode.width || 200) + 80,
      y: fromNode.y,
      color: name.startsWith("struct ") ? "#58a6ff" : "#bc8cff"
    });

    this.nodes.push(newNode);
    this.edges.push(new EdgeModel({
      fromNodeId: fromNode.id,
      fromPortId: fromPortId,
      toNodeId: newNode.id,
      toPortId: "in"
    }));

    this.updateView();
    toast.success(`Attached "${name}" to "${fromNode.title}"`);
  }

  attachNodeToConstituent(fromNodeId, constituentName, constituentIndex) {
    const fromNode = this.nodes.find((n) => n.id === fromNodeId);
    if (!fromNode) return;

    const cleanTitle = (constituentName || "MemberNode").replace(/^[*&]/, "").trim();
    const title = prompt(`Enter Node Title to attach for constituent "${constituentName}":`, cleanTitle);
    if (!title) return;

    const newNode = new NodeModel({
      title,
      type: title.startsWith("struct ") ? "struct" : "symbol",
      x: fromNode.x + (fromNode.width || 200) + 80,
      y: fromNode.y + constituentIndex * 24,
      color: title.startsWith("struct ") ? "#58a6ff" : "#bc8cff"
    });

    this.nodes.push(newNode);
    this.edges.push(new EdgeModel({
      fromNodeId: fromNode.id,
      fromPortId: `c_out_${constituentIndex}`,
      toNodeId: newNode.id,
      toPortId: "in",
      label: constituentName
    }));

    this.updateView();
    toast.success(`Attached "${title}" to constituent "${constituentName}"`);
  }

  handlePortMouseDown(nodeId, portId, portEl, e) {
    const wrapRect = this.canvasWrapperEl ? this.canvasWrapperEl.getBoundingClientRect() : { left: 0, top: 0 };
    const portRect = portEl.getBoundingClientRect();
    const x1 = portRect.left + portRect.width / 2 - wrapRect.left;
    const y1 = portRect.top + portRect.height / 2 - wrapRect.top;

    this.pendingWire = {
      fromNodeId: nodeId,
      fromPortId: portId,
      x1,
      y1,
      currentX: x1,
      currentY: y1
    };

    const onMouseMove = (moveEvt) => {
      if (!this.pendingWire) return;
      this.pendingWire.currentX = moveEvt.clientX - wrapRect.left;
      this.pendingWire.currentY = moveEvt.clientY - wrapRect.top;
      this.renderWires();
    };

    const onMouseUp = (upEvt) => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);

      if (!this.pendingWire) return;

      const targetEl = document.elementFromPoint(upEvt.clientX, upEvt.clientY);
      const targetPort = targetEl?.closest(".port-anchor");
      const targetNode = targetEl?.closest(".schematic-node");

      if (targetPort && targetPort.dataset.nodeId && targetPort.dataset.nodeId !== this.pendingWire.fromNodeId) {
        this.edges.push(new EdgeModel({
          fromNodeId: this.pendingWire.fromNodeId,
          fromPortId: this.pendingWire.fromPortId,
          toNodeId: targetPort.dataset.nodeId,
          toPortId: targetPort.dataset.portId || "in"
        }));
        toast.success("Connected nodes");
      } else if (targetNode && targetNode.dataset.nodeId && targetNode.dataset.nodeId !== this.pendingWire.fromNodeId) {
        this.edges.push(new EdgeModel({
          fromNodeId: this.pendingWire.fromNodeId,
          fromPortId: this.pendingWire.fromPortId,
          toNodeId: targetNode.dataset.nodeId,
          toPortId: "in"
        }));
        toast.success("Connected nodes");
      }

      this.pendingWire = null;
      this.renderWires();
    };

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
  }

  recolorNode(nodeId, color) {
    const node = this.nodes.find((n) => n.id === nodeId);
    if (node) {
      node.color = color;
      this.renderer.updateNodeColor(nodeId, color);
    }
  }

  updateView() {
    this.renderer.renderNodes(this.nodes, this.selectedNodeId);
    this.renderWires();
  }

  renderWires() {
    if (!this.wireLayerSvg) return;

    let svgPaths = "";
    const wrapRect = this.canvasWrapperEl ? this.canvasWrapperEl.getBoundingClientRect() : null;

    this.edges.forEach((edge) => {
      const fromNode = this.nodes.find((n) => n.id === edge.fromNodeId);
      const toNode = this.nodes.find((n) => n.id === edge.toNodeId);

      if (fromNode && toNode) {
        let x1 = (fromNode.x + fromNode.width) * this.zoom + this.panX;
        let y1 = (fromNode.y + 40) * this.zoom + this.panY;
        let x2 = toNode.x * this.zoom + this.panX;
        let y2 = (toNode.y + 40) * this.zoom + this.panY;

        if (wrapRect && edge.fromPortId) {
          const pEl = this.overlayLayerEl?.querySelector(`.port-anchor[data-node-id="${edge.fromNodeId}"][data-port-id="${edge.fromPortId}"]`);
          if (pEl) {
            const r = pEl.getBoundingClientRect();
            x1 = r.left + r.width / 2 - wrapRect.left;
            y1 = r.top + r.height / 2 - wrapRect.top;
          }
        }
        if (wrapRect && edge.toPortId) {
          const pEl = this.overlayLayerEl?.querySelector(`.port-anchor[data-node-id="${edge.toNodeId}"][data-port-id="${edge.toPortId}"]`);
          if (pEl) {
            const r = pEl.getBoundingClientRect();
            x2 = r.left + r.width / 2 - wrapRect.left;
            y2 = r.top + r.height / 2 - wrapRect.top;
          }
        }

        const pathData = EdgeRouter.computeBezierPath(x1, y1, x2, y2);
        svgPaths += `
          <path d="${pathData.svgPath}" stroke="var(--accent-blue)" stroke-width="2" fill="none" opacity="0.8"/>
          <circle cx="${x1}" cy="${y1}" r="4" fill="var(--accent-blue)"/>
          <circle cx="${x2}" cy="${y2}" r="4" fill="var(--accent-blue)"/>
        `;
      }
    });

    if (this.pendingWire) {
      const pathData = EdgeRouter.computeBezierPath(
        this.pendingWire.x1,
        this.pendingWire.y1,
        this.pendingWire.currentX,
        this.pendingWire.currentY
      );
      svgPaths += `
        <path d="${pathData.svgPath}" stroke="var(--accent-yellow)" stroke-dasharray="4,4" stroke-width="2" fill="none"/>
        <circle cx="${this.pendingWire.x1}" cy="${this.pendingWire.y1}" r="4" fill="var(--accent-yellow)"/>
        <circle cx="${this.pendingWire.currentX}" cy="${this.pendingWire.currentY}" r="4" fill="var(--accent-yellow)"/>
      `;
    }

    this.wireLayerSvg.innerHTML = svgPaths;
  }

  exportSVG() {
    // Lossless SVG export
    let svgContent = `<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1200" viewBox="0 0 1600 1200" style="background:#0a0d12;">\n`;

    // Edges
    this.edges.forEach((edge) => {
      const fromNode = this.nodes.find((n) => n.id === edge.fromNodeId);
      const toNode = this.nodes.find((n) => n.id === edge.toNodeId);
      if (fromNode && toNode) {
        const p = EdgeRouter.computeBezierPath(fromNode.x + fromNode.width, fromNode.y + 40, toNode.x, toNode.y + 40);
        svgContent += `  <path d="${p.svgPath}" stroke="#58a6ff" stroke-width="2" fill="none"/>\n`;
      }
    });

    // Nodes
    this.nodes.forEach((node) => {
      svgContent += `
        <g transform="translate(${node.x}, ${node.y})">
          <rect width="${node.width}" height="100" rx="6" fill="#161b22" stroke="${node.color}" stroke-width="2"/>
          <text x="12" y="24" fill="#ffffff" font-family="sans-serif" font-weight="bold" font-size="12">${node.title}</text>
          <text x="12" y="44" fill="#8b949e" font-family="monospace" font-size="10">${node.type}</text>
        </g>
      `;
    });

    svgContent += `</svg>`;

    const blob = new Blob([svgContent], { type: "image/svg+xml" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "nodemap_schematic.svg";
    a.click();
    toast.success("Exported lossless SVG schematic");
  }

  exportPNG() {
    const canvas = document.createElement("canvas");
    canvas.width = 1600;
    canvas.height = 1200;
    const ctx = canvas.getContext("2d");

    ctx.fillStyle = "#0a0d12";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    // Draw nodes
    this.nodes.forEach((node) => {
      ctx.fillStyle = "#161b22";
      ctx.strokeStyle = node.color;
      ctx.lineWidth = 2;
      ctx.fillRect(node.x, node.y, node.width, 100);
      ctx.strokeRect(node.x, node.y, node.width, 100);

      ctx.fillStyle = "#ffffff";
      ctx.font = "bold 12px sans-serif";
      ctx.fillText(node.title, node.x + 12, node.y + 24);

      ctx.fillStyle = "#8b949e";
      ctx.font = "10px monospace";
      ctx.fillText(node.type, node.x + 12, node.y + 44);
    });

    const a = document.createElement("a");
    a.href = canvas.toDataURL("image/png");
    a.download = "nodemap_schematic.png";
    a.click();
    toast.success("Exported lossless PNG image");
  }

  exportJSON() {
    const data = {
      version: state.currentVersion,
      nodes: this.nodes.map((n) => n.toJSON()),
      edges: this.edges.map((e) => e.toJSON())
    };

    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "nodemap_project.json";
    a.click();
    toast.success("Exported project JSON");
  }

  importJSON(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        const parsed = JSON.parse(e.target.result);
        if (Array.isArray(parsed.nodes)) {
          this.nodes = parsed.nodes.map((n) => new NodeModel(n));
          this.edges = Array.isArray(parsed.edges) ? parsed.edges.map((ed) => new EdgeModel(ed)) : [];
          this.updateView();
          toast.success("Imported NodeMap diagram");
        }
      } catch (err) {
        toast.error(`Import failed: ${err.message}`);
      }
    };
    reader.readAsText(file);
  }
}
