/**
 * node_model.js - Data Structures for NodeMap Schematic Canvas.
 */

export class NodePort {
  constructor(id, label, direction = "in") {
    this.id = id;
    this.label = label;
    this.direction = direction; // 'in' (left) | 'out' (right)
  }
}

export class NodeModel {
  constructor(options = {}) {
    this.id = options.id || "node-" + Math.random().toString(36).substring(2, 9);
    this.title = options.title || "Symbol";
    this.type = options.type || "struct"; // 'struct' | 'func' | 'file' | 'kconfig'
    this.x = options.x || 100;
    this.y = options.y || 100;
    this.width = options.width || 200;
    this.height = options.height || 120;
    this.color = options.color || "#58a6ff";
    this.expanded = options.expanded || false;
    this.astId = options.astId || null;
    this.constituents = options.constituents || [];
    this.ports = {
      in: options.ports?.in || [new NodePort("in-1", "Input", "in")],
      out: options.ports?.out || [new NodePort("out-1", "Output", "out")]
    };
  }

  toJSON() {
    return {
      id: this.id,
      title: this.title,
      type: this.type,
      x: this.x,
      y: this.y,
      width: this.width,
      height: this.height,
      color: this.color,
      expanded: this.expanded,
      astId: this.astId,
      constituents: this.constituents,
      ports: this.ports
    };
  }
}

export class EdgeModel {
  constructor(options = {}) {
    this.id = options.id || "edge-" + Math.random().toString(36).substring(2, 9);
    this.fromNodeId = options.fromNodeId;
    this.fromPortId = options.fromPortId;
    this.toNodeId = options.toNodeId;
    this.toPortId = options.toPortId;
    this.label = options.label || "";
  }

  toJSON() {
    return {
      id: this.id,
      fromNodeId: this.fromNodeId,
      fromPortId: this.fromPortId,
      toNodeId: this.toNodeId,
      toPortId: this.toPortId,
      label: this.label
    };
  }
}
