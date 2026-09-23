/**
 * node_renderer.js - DOM & Two.js Node Renderer.
 * Mounts interactive schematic nodes with drag handles, port anchors, and expand buttons.
 */

export class NodeRenderer {
  constructor(overlayContainerEl, callbacks) {
    this.container = overlayContainerEl;
    this.callbacks = callbacks; // { onDragNode, onSelectNode, onToggleExpand, onStartWire, onRecolor }
  }

  renderNodes(nodes, selectedNodeId) {
    this.container.innerHTML = "";

    nodes.forEach((node) => {
      const nodeEl = document.createElement("div");
      nodeEl.className = `schematic-node ${node.id === selectedNodeId ? "selected" : ""}`;
      nodeEl.dataset.nodeId = node.id;
      nodeEl.style.left = `${node.x}px`;
      nodeEl.style.top = `${node.y}px`;
      nodeEl.style.width = `${node.width}px`;

      // 1. Header
      const header = document.createElement("div");
      header.className = "node-header";
      header.style.borderTop = `3px solid ${node.color}`;

      const titleGroup = document.createElement("div");
      titleGroup.className = "node-title-group";

      const badge = document.createElement("span");
      badge.className = `node-badge ${node.type}`;
      badge.textContent = node.type;

      const titleText = document.createElement("span");
      titleText.textContent = node.title;

      titleGroup.appendChild(badge);
      titleGroup.appendChild(titleText);

      // Header tools
      const headerTools = document.createElement("div");
      headerTools.style.display = "flex";
      headerTools.style.alignItems = "center";
      headerTools.style.gap = "4px";

      // Recolor button
      const colorInput = document.createElement("input");
      colorInput.type = "color";
      colorInput.value = node.color;
      colorInput.style.width = "16px";
      colorInput.style.height = "16px";
      colorInput.style.padding = "0";
      colorInput.style.border = "none";
      colorInput.style.cursor = "pointer";
      colorInput.onchange = (e) => {
        if (this.callbacks.onRecolor) this.callbacks.onRecolor(node.id, e.target.value);
      };

      // Expand / Collapse button
      const expandBtn = document.createElement("button");
      expandBtn.style.fontSize = "11px";
      expandBtn.style.color = "var(--text-muted)";
      expandBtn.textContent = node.expanded ? "−" : "+";
      expandBtn.title = node.expanded ? "Collapse constituents" : "Expand constituents";
      expandBtn.onclick = (e) => {
        e.stopPropagation();
        if (this.callbacks.onToggleExpand) this.callbacks.onToggleExpand(node.id);
      };

      headerTools.appendChild(colorInput);
      headerTools.appendChild(expandBtn);

      header.appendChild(titleGroup);
      header.appendChild(headerTools);
      nodeEl.appendChild(header);

      // 2. Body
      const body = document.createElement("div");
      body.className = "node-body";

      // Input / Output ports
      const portsRow = document.createElement("div");
      portsRow.className = "node-ports";

      const inPortAnchor = document.createElement("div");
      inPortAnchor.className = "port-anchor in-port";
      inPortAnchor.title = "Input Port";
      inPortAnchor.dataset.nodeId = node.id;
      inPortAnchor.dataset.portId = "in";

      const outPortAnchor = document.createElement("div");
      outPortAnchor.className = "port-anchor out-port";
      outPortAnchor.title = "Output Port";
      outPortAnchor.dataset.nodeId = node.id;
      outPortAnchor.dataset.portId = "out";

      portsRow.appendChild(inPortAnchor);
      portsRow.appendChild(outPortAnchor);
      body.appendChild(portsRow);

      // Constituents if expanded
      if (node.expanded && Array.isArray(node.constituents) && node.constituents.length > 0) {
        const list = document.createElement("div");
        list.className = "node-constituents-list";

        node.constituents.forEach((c, idx) => {
          const item = document.createElement("div");
          item.className = "node-constituent-item";
          item.dataset.nodeId = node.id;
          item.dataset.constituentIndex = idx;
          const memberName = c.name || c.type || `member_${idx}`;
          item.dataset.constituentName = memberName;

          const leftPort = document.createElement("div");
          leftPort.className = "port-anchor in-port constituent-port";
          leftPort.title = `Input: ${memberName}`;
          leftPort.dataset.nodeId = node.id;
          leftPort.dataset.portId = `c_in_${idx}`;

          const nameSpan = document.createElement("span");
          nameSpan.className = "node-constituent-name";
          nameSpan.textContent = memberName;
          nameSpan.title = memberName;

          const rightPort = document.createElement("div");
          rightPort.className = "port-anchor out-port constituent-port";
          rightPort.title = `Connect ${memberName} to another node`;
          rightPort.dataset.nodeId = node.id;
          rightPort.dataset.portId = `c_out_${idx}`;

          item.appendChild(leftPort);
          item.appendChild(nameSpan);
          item.appendChild(rightPort);

          item.addEventListener("contextmenu", (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (this.callbacks.onConstituentContextMenu) {
              this.callbacks.onConstituentContextMenu(e, node, c, idx);
            }
          });

          list.appendChild(item);
        });
        body.appendChild(list);
      }

      nodeEl.appendChild(body);

      // Wire interactive ports
      nodeEl.querySelectorAll(".port-anchor").forEach((portEl) => {
        portEl.addEventListener("mousedown", (e) => {
          e.stopPropagation();
          if (this.callbacks.onPortMouseDown) {
            this.callbacks.onPortMouseDown(node.id, portEl.dataset.portId, portEl, e);
          }
        });
      });

      // Mouse drag handlers on header
      this.attachDrag(header, node);

      // Select node on click
      nodeEl.onclick = (e) => {
        e.stopPropagation();
        if (this.callbacks.onSelectNode) this.callbacks.onSelectNode(node.id);
      };

      this.container.appendChild(nodeEl);
    });
  }

  attachDrag(dragHandle, node) {
    let isDragging = false;
    let startX = 0;
    let startY = 0;
    let initialNodeX = 0;
    let initialNodeY = 0;
    const nodeEl = dragHandle.closest(".schematic-node");

    dragHandle.addEventListener("mousedown", (e) => {
      e.stopPropagation();
      isDragging = true;
      startX = e.clientX;
      startY = e.clientY;
      initialNodeX = node.x;
      initialNodeY = node.y;

      const onMouseMove = (moveEvent) => {
        if (!isDragging) return;
        const zoom = (this.callbacks.getZoom ? this.callbacks.getZoom() : 1.0) || 1.0;
        const dx = (moveEvent.clientX - startX) / zoom;
        const dy = (moveEvent.clientY - startY) / zoom;

        node.x = initialNodeX + dx;
        node.y = initialNodeY + dy;

        if (nodeEl) {
          nodeEl.style.left = `${node.x}px`;
          nodeEl.style.top = `${node.y}px`;
        }

        if (this.callbacks.onDragNode) {
          this.callbacks.onDragNode(node.id, node.x, node.y);
        }
      };

      const onMouseUp = () => {
        isDragging = false;
        window.removeEventListener("mousemove", onMouseMove);
        window.removeEventListener("mouseup", onMouseUp);
      };

      window.addEventListener("mousemove", onMouseMove);
      window.addEventListener("mouseup", onMouseUp);
    });
  }

  updateSelection(selectedNodeId) {
    this.container.querySelectorAll(".schematic-node").forEach((el) => {
      el.classList.toggle("selected", el.dataset.nodeId === selectedNodeId);
    });
  }

  updateNodeColor(nodeId, color) {
    const nodeEl = this.container.querySelector(`.schematic-node[data-node-id="${nodeId}"]`);
    if (nodeEl) {
      const header = nodeEl.querySelector(".node-header");
      if (header) header.style.borderTop = `3px solid ${color}`;
    }
  }

  updateNodePosition(nodeId, x, y) {
    const nodeEl = this.container.querySelector(`.schematic-node[data-node-id="${nodeId}"]`);
    if (nodeEl) {
      nodeEl.style.left = `${x}px`;
      nodeEl.style.top = `${y}px`;
    }
  }
}
