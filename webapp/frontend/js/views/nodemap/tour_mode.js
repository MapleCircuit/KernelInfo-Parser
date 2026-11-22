/**
 * tour_mode.js - Guided canvas walkthroughs (tours) for NodeMap.
 *
 * Pure tour document helpers (`createTour`, `captureTourStep`, `reorderTourStep`, `removeTourStep`,
 * `tourStepHighlights`) are DOM-free and unit tested; `NodeMapTourPlayer` drives playback over
 * `controller.wrapEl` with smooth viewport transitions, focus/dim highlighting, and Inspector UI HTML.
 */
import { displayTitle, newId } from "./canvas_model.js";
import { esc } from "./struct_view.js";

/** Create an empty tour record with a normalized title. */
export function createTour(title = "Walkthrough") {
  return {
    id: newId("tour"),
    title: String(title || "Walkthrough").trim().slice(0, 128) || "Walkthrough",
    steps: [],
  };
}

/** Capture the current selection and viewport into a tour step object. */
export function captureTourStep({
  title = "",
  caption = "",
  selectedNodes = [],
  selectedEdges = [],
  viewport = { x: 0, y: 0, zoom: 1 },
} = {}) {
  const z = Number(viewport?.zoom);
  return {
    id: newId("step"),
    title: String(title || "Step").trim().slice(0, 256) || "Step",
    caption: String(caption || "").slice(0, 2000),
    nodeIds: [...(selectedNodes || [])].map(String),
    edgeIds: [...(selectedEdges || [])].map(String),
    viewport: {
      x: Math.round(Number(viewport?.x) || 0),
      y: Math.round(Number(viewport?.y) || 0),
      zoom: Number.isFinite(z) && z > 0 ? z : 1,
    },
  };
}

/** Move step `stepId` by `delta` (-1 or +1) inside `tour.steps`; returns true when moved. */
export function reorderTourStep(tour, stepId, delta) {
  if (!tour || !Array.isArray(tour.steps)) return false;
  const d = Number(delta);
  if (!Number.isInteger(d) || d === 0) return false;
  const idx = tour.steps.findIndex((s) => s?.id === stepId);
  if (idx < 0) return false;
  const target = idx + d;
  if (target < 0 || target >= tour.steps.length) return false;
  const [step] = tour.steps.splice(idx, 1);
  tour.steps.splice(target, 0, step);
  return true;
}

/** Remove step `stepId` from `tour.steps`; returns true when removed. */
export function removeTourStep(tour, stepId) {
  if (!tour || !Array.isArray(tour.steps)) return false;
  const idx = tour.steps.findIndex((s) => s?.id === stepId);
  if (idx < 0) return false;
  tour.steps.splice(idx, 1);
  return true;
}

/** Highlight sets and whether focusing/dimming is active for `step`. */
export function tourStepHighlights(step) {
  const nodeIds = new Set(Array.isArray(step?.nodeIds) ? step.nodeIds : []);
  const edgeIds = new Set(Array.isArray(step?.edgeIds) ? step.edgeIds : []);
  return {
    nodeIds,
    edgeIds,
    active: Boolean(step && (nodeIds.size > 0 || edgeIds.size > 0)),
  };
}

/**
 * Interactive tour player overlay and Inspector builder for NodeMap guided walkthroughs.
 */
export class NodeMapTourPlayer {
  constructor(controller) {
    this.controller = controller;
    this.tourId = null;
    this.stepIndex = 0;
    this.el = null;
    this.wrapEl = null;
    this.counterEl = null;
    this.titleEl = null;
    this.captionEl = null;
    this.prevBtn = null;
    this.nextBtn = null;
    this.exitBtn = null;
    this._animTimer = null;
    this._unsubs = [];
  }

  mount(wrapEl) {
    this.dispose();
    if (!wrapEl) return null;
    this.wrapEl = wrapEl;

    const el = document.createElement("div");
    el.className = "nm-tour-bar";
    el.hidden = true;
    el.setAttribute?.("role", "region");
    el.setAttribute?.("aria-label", "Canvas tour player");

    const head = document.createElement("div");
    head.className = "nm-tour-head";

    const counterEl = document.createElement("span");
    counterEl.className = "nm-tour-counter";
    counterEl.textContent = "0 / 0";

    const titleEl = document.createElement("span");
    titleEl.className = "nm-tour-title";
    titleEl.textContent = "";

    const controls = document.createElement("div");
    controls.className = "nm-tour-controls";

    const prevBtn = document.createElement("button");
    prevBtn.type = "button";
    prevBtn.className = "nodemap-btn";
    prevBtn.dataset.tour = "prev";
    prevBtn.setAttribute?.("data-tour", "prev");
    prevBtn.title = "Previous step (Left Arrow)";
    prevBtn.textContent = "← Prev";

    const nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "nodemap-btn";
    nextBtn.dataset.tour = "next";
    nextBtn.setAttribute?.("data-tour", "next");
    nextBtn.title = "Next step (Right Arrow)";
    nextBtn.textContent = "Next →";

    const exitBtn = document.createElement("button");
    exitBtn.type = "button";
    exitBtn.className = "nodemap-btn nm-danger";
    exitBtn.dataset.tour = "exit";
    exitBtn.setAttribute?.("data-tour", "exit");
    exitBtn.title = "Exit tour (Escape)";
    exitBtn.textContent = "Exit";

    const captionEl = document.createElement("div");
    captionEl.className = "nm-tour-caption";
    captionEl.hidden = true;

    controls.appendChild(prevBtn);
    controls.appendChild(nextBtn);
    controls.appendChild(exitBtn);
    head.appendChild(counterEl);
    head.appendChild(titleEl);
    head.appendChild(controls);
    el.appendChild(head);
    el.appendChild(captionEl);
    wrapEl.appendChild?.(el);

    this.el = el;
    this.counterEl = counterEl;
    this.titleEl = titleEl;
    this.captionEl = captionEl;
    this.prevBtn = prevBtn;
    this.nextBtn = nextBtn;
    this.exitBtn = exitBtn;

    const listen = (target, type, fn) => {
      if (!target?.addEventListener) return;
      target.addEventListener(type, fn);
      this._unsubs.push(() => target.removeEventListener?.(type, fn));
    };

    const runAction = (act) => {
      if (act === "prev") this.step(-1);
      else if (act === "next") this.step(1);
      else if (act === "exit") this.stop();
    };

    listen(el, "pointerdown", (e) => e.stopPropagation?.());
    listen(el, "dblclick", (e) => e.stopPropagation?.());
    const guardRun = (e, act) => {
      if (!act || e?._tourHandled) return;
      if (e && typeof e === "object") e._tourHandled = true;
      e?.stopPropagation?.();
      runAction(act);
    };
    listen(el, "click", (e) => {
      e?.stopPropagation?.();
      const act = e?.target?.closest?.("[data-tour]")?.dataset?.tour;
      guardRun(e, act);
    });

    listen(prevBtn, "click", (e) => guardRun(e, "prev"));
    listen(nextBtn, "click", (e) => guardRun(e, "next"));
    listen(exitBtn, "click", (e) => guardRun(e, "exit"));

    return el;
  }

  get active() {
    return Boolean(this.tourId);
  }

  currentTour() {
    if (!this.tourId) return null;
    const tours = this.controller?.canvas?.tours;
    if (!Array.isArray(tours)) return null;
    return tours.find((t) => t?.id === this.tourId) || null;
  }

  currentStep() {
    const tour = this.currentTour();
    if (!tour || !Array.isArray(tour.steps) || !tour.steps.length) return null;
    return tour.steps[this.stepIndex] || null;
  }

  start(tourId, startIndex = 0) {
    const tour = (this.controller?.canvas?.tours || []).find((t) => t?.id === tourId) || null;
    if (!tour) return false;
    const steps = Array.isArray(tour.steps) ? tour.steps : [];
    const maxIdx = Math.max(0, steps.length - 1);
    const idx = Math.max(0, Math.min(Number(startIndex) || 0, maxIdx));
    this.tourId = tour.id;
    this.stepIndex = idx;
    if (this.el) this.el.hidden = false;
    this.applyStep();
    this.controller?.onTourStateChanged?.();
    return true;
  }

  stop() {
    if (this._animTimer) {
      clearTimeout(this._animTimer);
      this._animTimer = null;
    }
    this.controller?.layerEl?.classList?.remove?.("nm-tour-anim");
    this.tourId = null;
    this.stepIndex = 0;
    if (this.el) this.el.hidden = true;
    this.applyHighlights();
    this.controller?.onTourStateChanged?.();
  }

  step(delta) {
    const tour = this.currentTour();
    if (!tour || !Array.isArray(tour.steps) || !tour.steps.length) return false;
    const d = Number(delta) || 0;
    const next = Math.max(0, Math.min(this.stepIndex + d, tour.steps.length - 1));
    this.stepIndex = next;
    this.applyStep();
    return true;
  }

  applyStep() {
    const tour = this.currentTour();
    const step = this.currentStep();
    const total = Array.isArray(tour?.steps) ? tour.steps.length : 0;

    if (step?.viewport && this.controller?.canvas) {
      const z = Number(step.viewport.zoom);
      this.controller.canvas.viewport = {
        ...step.viewport,
        x: Number(step.viewport.x) || 0,
        y: Number(step.viewport.y) || 0,
        zoom: Number.isFinite(z) && z > 0 ? z : 1,
      };
      const layerEl = this.controller.layerEl;
      if (layerEl?.classList) {
        layerEl.classList.add("nm-tour-anim");
        if (this._animTimer) clearTimeout(this._animTimer);
        this._animTimer = setTimeout(() => {
          this._animTimer = null;
          layerEl.classList?.remove?.("nm-tour-anim");
        }, 320);
        this._animTimer?.unref?.();
      }
      this.controller.applyTransform?.();
      this.controller.renderWires?.();
    }

    if (this.counterEl) this.counterEl.textContent = total ? `${this.stepIndex + 1} / ${total}` : "0 / 0";
    if (this.titleEl) this.titleEl.textContent = step ? step.title : (tour?.title || "");
    if (this.captionEl) {
      const cap = step?.caption || "";
      this.captionEl.textContent = cap;
      this.captionEl.hidden = !cap;
    }
    if (this.prevBtn) this.prevBtn.disabled = !total || this.stepIndex <= 0;
    if (this.nextBtn) this.nextBtn.disabled = !total || this.stepIndex >= total - 1;

    this.applyHighlights();
  }

  applyHighlights() {
    const active = this.active;
    const step = active ? this.currentStep() : null;
    const hl = tourStepHighlights(step);
    const focusActive = active && hl.active;

    this.controller?.rootEl?.classList?.toggle?.("nm-tour-active", active);

    const els = this.controller?.renderer?.els;
    const nodeEntries = els && typeof els.entries === "function"
      ? [...els.entries()]
      : els && typeof els === "object"
        ? Object.entries(els)
        : (this.controller?.canvas?.nodes || [])
            .map((n) => [n.id, this.controller?.renderer?.element?.(n.id)])
            .filter(([, el]) => Boolean(el));

    for (const [id, nodeEl] of nodeEntries) {
      const focused = focusActive && hl.nodeIds.has(id);
      const dimmed = focusActive && !focused;
      nodeEl?.classList?.toggle?.("nm-tour-focus", focused);
      nodeEl?.classList?.toggle?.("nm-tour-dim", dimmed);
    }

    const wireSvg = this.controller?.wireSvg;
    let edgeEls = [];
    if (wireSvg?.querySelectorAll) {
      edgeEls = [...(wireSvg.querySelectorAll(".nm-edge[data-edge-id]") || [])];
      if (!edgeEls.length) edgeEls = [...(wireSvg.querySelectorAll(".nm-edge") || [])];
    }
    if (!edgeEls.length && Array.isArray(wireSvg?.children)) {
      edgeEls = wireSvg.children.filter((c) => c?.dataset?.edgeId || c?.getAttribute?.("data-edge-id"));
    }
    for (const edgeEl of edgeEls) {
      const eid = edgeEl?.dataset?.edgeId ?? edgeEl?.getAttribute?.("data-edge-id");
      if (!eid) continue;
      const focused = focusActive && hl.edgeIds.has(eid);
      const dimmed = focusActive && !focused;
      edgeEl?.classList?.toggle?.("nm-tour-focus", focused);
      edgeEl?.classList?.toggle?.("nm-tour-dim", dimmed);
    }
  }

  handleKeyDown(e) {
    if (!this.active || !e) return false;
    const tag = String(e.target?.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target?.isContentEditable) {
      return false;
    }
    if (e.key === "ArrowLeft") {
      e.preventDefault?.();
      this.step(-1);
      return true;
    }
    if (e.key === "ArrowRight") {
      e.preventDefault?.();
      this.step(1);
      return true;
    }
    if (e.key === "Escape") {
      e.preventDefault?.();
      this.stop();
      return true;
    }
    return false;
  }

  /** Suggest a default step title from the currently selected node(s) or step index. */
  defaultStepTitle(index = 0) {
    const selected = this.controller?.selected ? [...this.controller.selected] : [];
    if (selected.length === 1) {
      const node = (this.controller?.canvas?.nodes || []).find((n) => n.id === selected[0]);
      if (node) return displayTitle(node);
    }
    return `Step ${index + 1}`;
  }

  inspectorHtml(canvas = this.controller?.canvas, activeTourId = this.tourId || "") {
    return NodeMapTourPlayer.inspectorHtml(canvas, activeTourId);
  }

  /** Render the Inspector Tours section HTML using `data-insp="tour-*"` delegation attributes. */
  static inspectorHtml(canvas, activeTourId = "") {
    const tours = Array.isArray(canvas?.tours) ? canvas.tours : [];
    const activeTour = tours.find((t) => t?.id === activeTourId) || tours[0] || null;
    const header = `<div class="inspector-field nm-tour-insp-head"><span class="inspector-label">Tours</span><div class="nm-insp-inline"><button type="button" class="nodemap-btn" data-insp="tour-add">+ New Tour</button></div></div>`;

    if (!activeTour) {
      return `${header}<div class="inspector-field"><span class="inspector-val nm-hint">No tours yet</span></div>`;
    }

    const optionsHtml = tours.map((t) => {
      const count = Array.isArray(t.steps) ? t.steps.length : 0;
      return `<option value="${esc(t.id)}"${t.id === activeTour.id ? " selected" : ""}>${esc(t.title)} (${count})</option>`;
    }).join("");

    const steps = Array.isArray(activeTour.steps) ? activeTour.steps : [];
    const stepsHtml = steps.length
      ? steps.map((s, i) => {
          const nCount = Array.isArray(s.nodeIds) ? s.nodeIds.length : 0;
          const eCount = Array.isArray(s.edgeIds) ? s.edgeIds.length : 0;
          const zoomPct = Math.round((Number(s.viewport?.zoom) > 0 ? Number(s.viewport.zoom) : 1) * 100);
          return `<div class="nm-tour-step-card" data-step-id="${esc(s.id)}">
            <div class="nm-es-row">
              <span class="nm-es-count">${i + 1}.</span>
              <input data-insp="tour-step-title" data-id="${esc(activeTour.id)}" data-step-id="${esc(s.id)}" value="${esc(s.title)}" aria-label="Step title" spellcheck="false" />
              <button type="button" class="nodemap-btn" data-insp="tour-step-go" data-id="${esc(activeTour.id)}" data-step-id="${esc(s.id)}" data-index="${i}" title="Jump to this step">Go</button>
              <button type="button" class="nodemap-btn" data-insp="tour-step-up" data-id="${esc(activeTour.id)}" data-step-id="${esc(s.id)}" title="Move step up"${i === 0 ? " disabled" : ""}>▲</button>
              <button type="button" class="nodemap-btn" data-insp="tour-step-down" data-id="${esc(activeTour.id)}" data-step-id="${esc(s.id)}" title="Move step down"${i === steps.length - 1 ? " disabled" : ""}>▼</button>
              <button type="button" class="nodemap-btn nm-danger" data-insp="tour-step-delete" data-id="${esc(activeTour.id)}" data-step-id="${esc(s.id)}" title="Remove step">✕</button>
            </div>
            <textarea class="nm-tour-step-caption" data-insp="tour-step-caption" data-id="${esc(activeTour.id)}" data-step-id="${esc(s.id)}" rows="2" placeholder="Caption…" aria-label="Step caption">${esc(s.caption || "")}</textarea>
            <div class="inspector-val nm-hint">${nCount} node${nCount === 1 ? "" : "s"} · ${eCount} edge${eCount === 1 ? "" : "s"} · ${zoomPct}%</div>
          </div>`;
        }).join("")
      : '<div class="inspector-val nm-hint">No steps yet: select nodes, frame the view, and click + Capture Current Step</div>';

    return `${header}
      <div class="inspector-field">
        <select data-insp="tour-select" aria-label="Select tour">${optionsHtml}</select>
      </div>
      <div class="inspector-field">
        <div class="nm-es-row">
          <input data-insp="tour-title" data-id="${esc(activeTour.id)}" value="${esc(activeTour.title)}" aria-label="Tour title" spellcheck="false" />
          <button type="button" class="nodemap-btn nm-danger" data-insp="tour-delete" data-id="${esc(activeTour.id)}" title="Delete tour">×</button>
        </div>
      </div>
      <div class="nm-insp-actions">
        <button type="button" class="nodemap-btn" data-insp="tour-capture" data-id="${esc(activeTour.id)}">+ Capture Current Step</button>
        <button type="button" class="nodemap-btn" data-insp="tour-play" data-id="${esc(activeTour.id)}"${steps.length ? "" : " disabled"}>▶ Present Tour</button>
      </div>
      <div class="nm-es-list nm-tour-step-list">${stepsHtml}</div>`;
  }

  dispose() {
    if (this._animTimer) {
      clearTimeout(this._animTimer);
      this._animTimer = null;
    }
    this.controller?.layerEl?.classList?.remove?.("nm-tour-anim");
    this.tourId = null;
    this.stepIndex = 0;
    this.applyHighlights();
    for (const u of this._unsubs) u();
    this._unsubs = [];
    this.el?.remove?.();
    if (Array.isArray(this.wrapEl?.children)) {
      const idx = this.wrapEl.children.indexOf(this.el);
      if (idx >= 0) this.wrapEl.children.splice(idx, 1);
    }
    this.el = null;
    this.counterEl = null;
    this.titleEl = null;
    this.captionEl = null;
    this.prevBtn = null;
    this.nextBtn = null;
    this.exitBtn = null;
    this.wrapEl = null;
  }
}
