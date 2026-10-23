/**
 * treemap_view.js - Squarified treemap of the source tree (bytes or file counts).
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { shortRef } from "../../components/ref_picker.js";
import { escapeHtml } from "../../components/modal.js";

const PALETTE = ["#388bfd", "#3fb950", "#d29922", "#bc8cff", "#f0883e", "#39c5bb", "#f85149", "#79c0ff", "#56d364", "#e3b341", "#d2a8ff", "#ffa657"];
const MIN_LABEL_W = 46;
const MIN_LABEL_H = 18;

function formatBytes(n) {
  if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GiB`;
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MiB`;
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(1)} KiB`;
  return `${n} B`;
}

/** Squarified treemap layout (Bruls, Huizing & van Wijk). items: [{value, ...}] sorted descending. */
function squarify(items, x, y, w, h) {
  const out = [];
  const total = items.reduce((s, it) => s + it.value, 0);
  if (total <= 0 || w <= 0 || h <= 0) return out;
  const scale = (w * h) / total;
  const queue = items.map((it) => ({ ...it, area: it.value * scale }));
  let rect = { x, y, w, h };

  const worst = (row, side) => {
    const sum = row.reduce((s, r) => s + r.area, 0);
    const max = Math.max(...row.map((r) => r.area));
    const min = Math.min(...row.map((r) => r.area));
    return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min));
  };

  const layoutRow = (row) => {
    const sum = row.reduce((s, r) => s + r.area, 0);
    if (rect.w >= rect.h) {
      const colW = sum / rect.h;
      let cy = rect.y;
      for (const r of row) {
        const rh = r.area / colW;
        out.push({ ...r, x: rect.x, y: cy, w: colW, h: rh });
        cy += rh;
      }
      rect = { x: rect.x + colW, y: rect.y, w: rect.w - colW, h: rect.h };
    } else {
      const rowH = sum / rect.w;
      let cx = rect.x;
      for (const r of row) {
        const rw = r.area / rowH;
        out.push({ ...r, x: cx, y: rect.y, w: rw, h: rowH });
        cx += rw;
      }
      rect = { x: rect.x, y: rect.y + rowH, w: rect.w, h: rect.h - rowH };
    }
  };

  let row = [];
  while (queue.length) {
    const side = Math.min(rect.w, rect.h);
    const next = queue[0];
    if (row.length === 0 || worst([...row, next], side) <= worst(row, side)) {
      row.push(queue.shift());
    } else {
      layoutRow(row);
      row = [];
    }
  }
  if (row.length) layoutRow(row);
  return out;
}

export class TreemapView {
  constructor() {
    this.tab = null;
    this.data = null;
    this.metric = "size";
    this._resizeObserver = null;
  }

  async render(containerEl, tabData) {
    this.tab = tabData;
    this.containerEl = containerEl;
    this.version = tabData.version || state.currentVersion;
    this.path = tabData.path || "";
    this.metric = tabData.metric || "size";

    containerEl.innerHTML = `
      <div class="treemap-view">
        <div class="treemap-toolbar">
          <div class="treemap-crumbs"></div>
          <div class="treemap-actions">
            <span class="treemap-summary"></span>
            <button class="code-btn ${this.metric === "size" ? "active" : ""}" data-metric="size" type="button">Bytes</button>
            <button class="code-btn ${this.metric === "file_count" ? "active" : ""}" data-metric="file_count" type="button">Files</button>
          </div>
        </div>
        <div class="treemap-canvas"><div class="treemap-loading">Loading treemap…</div></div>
        <div class="treemap-tooltip" hidden></div>
      </div>`;

    containerEl.querySelectorAll("[data-metric]").forEach((btn) => {
      btn.onclick = () => {
        this.metric = btn.dataset.metric;
        this.tab.metric = this.metric;
        state.saveState();
        containerEl.querySelectorAll("[data-metric]").forEach((b) => b.classList.toggle("active", b === btn));
        this.draw();
      };
    });

    this._resizeObserver?.disconnect();
    this._resizeObserver = new ResizeObserver(() => this.draw());
    this._resizeObserver.observe(containerEl.querySelector(".treemap-canvas"));
    await this.load();
  }

  async load() {
    const canvas = this.containerEl.querySelector(".treemap-canvas");
    try {
      this.data = await api.getTreemap(this.version, 2, this.path);
    } catch (err) {
      canvas.innerHTML = `<div class="treemap-loading error">Failed: ${escapeHtml(err.message)}</div>`;
      return;
    }
    this.renderCrumbs();
    this.draw();
  }

  navigate(path) {
    this.path = path;
    this.tab.path = path;
    this.tab.title = `Treemap: ${path ? path.split("/").pop() : "root"}`;
    state.saveState();
    const titleEl = document.querySelector(`.tab[data-tab-id="${this.tab.id}"] .tab-title`);
    if (titleEl) titleEl.textContent = this.tab.title;
    this.load();
  }

  renderCrumbs() {
    const crumbs = this.containerEl.querySelector(".treemap-crumbs");
    const parts = this.path ? this.path.split("/") : [];
    let acc = "";
    const links = [`<a href="#" data-path="">${escapeHtml(shortRef(this.version))}</a>`];
    parts.forEach((p) => {
      acc = acc ? `${acc}/${p}` : p;
      links.push(`<a href="#" data-path="${escapeHtml(acc)}">${escapeHtml(p)}</a>`);
    });
    crumbs.innerHTML = links.join(`<span class="treemap-sep">/</span>`);
    crumbs.querySelectorAll("a").forEach((a) => {
      a.onclick = (e) => {
        e.preventDefault();
        this.navigate(a.dataset.path);
      };
    });
    const d = this.data;
    this.containerEl.querySelector(".treemap-summary").textContent = `${formatBytes(d.size)} · ${d.file_count.toLocaleString()} files`;
  }

  draw() {
    const canvas = this.containerEl?.querySelector(".treemap-canvas");
    if (!canvas || !this.data) return;
    const W = canvas.clientWidth;
    const H = canvas.clientHeight;
    if (W < 20 || H < 20) return;
    const metric = this.metric;
    const items = (this.data.children || [])
      .map((c, i) => ({ node: c, value: c[metric] || 0, color: PALETTE[i % PALETTE.length] }))
      .filter((it) => it.value > 0)
      .sort((a, b) => b.value - a.value);

    let html = "";
    for (const r of squarify(items, 0, 0, W, H)) {
      const n = r.node;
      const isDir = n.type === "dir";
      const showLabel = r.w >= MIN_LABEL_W && r.h >= MIN_LABEL_H;
      html += `<div class="treemap-cell ${isDir ? "dir" : "file"}" style="left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;--tm-color:${r.color};" data-path="${escapeHtml(n.path)}" data-type="${n.type}">`;
      if (showLabel) html += `<div class="treemap-label">${escapeHtml(n.name)}${isDir ? "/" : ""}</div>`;
      // Second level, inset under the label
      if (isDir && n.children?.length && r.w > 60 && r.h > 40) {
        const inner = n.children
          .map((c) => ({ node: c, value: c[metric] || 0 }))
          .filter((it) => it.value > 0)
          .sort((a, b) => b.value - a.value);
        for (const c of squarify(inner, 2, 18, r.w - 4, r.h - 20)) {
          const cn = c.node;
          html += `<div class="treemap-sub ${cn.type}" style="left:${c.x}px;top:${c.y}px;width:${c.w}px;height:${c.h}px;" data-path="${escapeHtml(cn.path)}" data-type="${cn.type}">${c.w >= MIN_LABEL_W && c.h >= 14 ? `<span>${escapeHtml(cn.name)}</span>` : ""}</div>`;
        }
      }
      html += `</div>`;
    }
    canvas.innerHTML = html || `<div class="treemap-loading">Empty directory.</div>`;

    const lookup = new Map();
    const index = (node) => {
      lookup.set(node.path, node);
      (node.children || []).forEach(index);
    };
    (this.data.children || []).forEach(index);

    const tooltip = this.containerEl.querySelector(".treemap-tooltip");
    canvas.onmousemove = (e) => {
      const el = e.target.closest("[data-path]");
      const node = el && lookup.get(el.dataset.path);
      if (!node) {
        tooltip.hidden = true;
        return;
      }
      tooltip.hidden = false;
      tooltip.innerHTML = `<strong>${escapeHtml(node.path)}</strong><br>${formatBytes(node.size)} · ${node.file_count.toLocaleString()} file${node.file_count === 1 ? "" : "s"}<br><span>${node.type === "dir" ? "Click to zoom in" : "Click to open"} · middle-click: new tab</span>`;
      const rect = this.containerEl.querySelector(".treemap-view").getBoundingClientRect();
      tooltip.style.left = `${Math.min(e.clientX - rect.left + 14, rect.width - 260)}px`;
      tooltip.style.top = `${e.clientY - rect.top + 14}px`;
    };
    canvas.onmouseleave = () => {
      tooltip.hidden = true;
    };
    const open = (el, forceNew) => {
      if (!el) return;
      if (el.dataset.type === "dir" && !forceNew) this.navigate(el.dataset.path);
      else state.openTab({ type: "code", title: el.dataset.path.split("/").pop(), path: el.dataset.path, version: this.version, forceNew });
    };
    canvas.onclick = (e) => open(e.target.closest("[data-path]"), false);
    canvas.onauxclick = (e) => {
      if (e.button === 1) {
        e.preventDefault();
        open(e.target.closest("[data-path]"), true);
      }
    };
  }
}
