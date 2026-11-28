/**
 * tour_export.js - Client-side Tour Frame Export (PNG Sequence) for NodeMap.
 *
 * Drives stepped deterministic frame rendering over a single reused working `<canvas>`,
 * writes individual unzipped PNGs via File System Access API (`showDirectoryPicker`) or
 * a staggered `<a download>` fallback, composites optional HUD overlays while always excluding
 * the export progress/cancel UI, and restores all live controller/tour state in `finally`.
 */
import { escapeHtml, openModal } from "../../components/modal.js";
import { toast } from "../../components/toast.js";
import { visibleCanvasSlice } from "./canvas_model.js";
import { captureElementForeignObject } from "./nodemap_export.js";
import { LIVE_NODE_TYPES, browserPngEnv, collectPaintOps, liveNodeRasters } from "./nodemap_png.js";
import {
  DEFAULT_TOUR_TRANSITION_SEC,
  FALLBACK_CONFIRM_ABOVE,
  FPS_PRESETS,
  RESOLUTION_PRESETS,
  defaultTourExportOptions,
  filenamePreview,
  formatFrameFilename,
  normalizeViewportSize,
  planTourFrames,
} from "./tour_export_plan.js";
import { tourStepHighlights } from "./tour_mode.js";

const STAGGER_DOWNLOAD_MS = 120;
const DOWNLOAD_REVOKE_MS = 1500;
const SVG_NS = "http://www.w3.org/2000/svg";

export function createTourExportState() {
  return {
    ...defaultTourExportOptions(),
    running: false,
    progressFrame: 0,
    progressTotal: 0,
    progressLabel: "",
    statusText: "",
    errorBanner: "",
  };
}

/** Detect File System Access directory picker support and explain why if unavailable. */
export function detectDirectoryPickerSupport(win = globalThis.window) {
  if (typeof win?.showDirectoryPicker === "function") {
    return { supported: true, reason: "supported", notice: "" };
  }
  if (win && win.isSecureContext === false) {
    return {
      supported: false,
      reason: "insecure-context",
      notice: "Folder picker is unavailable in a non-HTTPS context (isSecureContext is false); exporting will use individual browser downloads.",
    };
  }
  return {
    supported: false,
    reason: "unsupported-browser",
    notice: "Folder picker is not supported in this browser; exporting will use individual browser downloads.",
  };
}

/** Read current NodeMap canvas wrapper dimensions (with safe fallback). */
export function controllerViewportSize(ctrl) {
  const r = ctrl?.wrapEl?.getBoundingClientRect?.();
  return normalizeViewportSize({ width: r?.width, height: r?.height });
}

function getActiveTour(ctrl) {
  const tours = Array.isArray(ctrl?.canvas?.tours) ? ctrl.canvas.tours : [];
  return tours.find((t) => t?.id === ctrl?.activeTourId) || tours[0] || null;
}

function formatDuration(sec) {
  if (!(sec > 0)) return "0.0s";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = (sec % 60).toFixed(1);
  return `${m}m ${s}s`;
}

/** Build summary + preview metadata for the current state and tour. */
export function computeTourExportPreview(tour, state, viewportSize) {
  const steps = Array.isArray(tour?.steps) ? tour.steps : [];
  const clamped = {
    ...state,
    fromTarget: Math.max(1, Math.min(Number(state?.fromTarget) || 1, Math.max(1, steps.length))),
    toTarget: Math.max(1, Math.min(Number(state?.toTarget) || Math.max(1, steps.length), Math.max(1, steps.length))),
  };
  if (state?.rangeMode !== "custom") clamped.toTarget = Math.max(1, steps.length);
  const plan = planTourFrames(tour, clamped, viewportSize);
  const preview = filenamePreview(Math.max(1, plan.totalFrames), {
    prefix: clamped.prefix,
    suffix: clamped.suffix,
  });
  return { plan, preview, clamped };
}

/** Render the collapsible Video / Frame Export panel inside the Tours Inspector. */
export function tourExportPanelHtml(tour, rawState, viewportSize, pickerSupport = detectDirectoryPickerSupport()) {
  const state = rawState || createTourExportState();
  const isOpen = Boolean(state.open);
  const toggleBtn = `<div class="nm-tour-export-bar" data-tour-export-ui="1">
    <button type="button" class="nodemap-btn nm-tour-export-toggle" data-insp="tour-export-toggle" aria-pressed="${isOpen}" aria-expanded="${isOpen}">
      ${isOpen ? "▾" : "▸"} Video / Frame Export
    </button>
  </div>`;
  if (!isOpen) return toggleBtn;

  const steps = Array.isArray(tour?.steps) ? tour.steps : [];
  const { plan, preview, clamped } = computeTourExportPreview(tour, state, viewportSize);
  const norm = plan.normalized;
  const running = Boolean(state.running);
  const lockAttr = running ? " disabled" : "";

  const targetOptions = (selectedIdx1) =>
    steps.map((s, idx) => {
      const n = idx + 1;
      const label = `${n}. ${s.title || `Target ${n}`}`;
      return `<option value="${n}"${n === selectedIdx1 ? " selected" : ""}>${escapeHtml(label)}</option>`;
    }).join("");

  const fpsOptions = [
    ...FPS_PRESETS.map((f) => `<option value="${f}"${String(clamped.fpsPreset) === String(f) ? " selected" : ""}>${f} fps</option>`),
    `<option value="custom"${clamped.fpsPreset === "custom" ? " selected" : ""}>Custom…</option>`,
  ].join("");

  const resOptions = RESOLUTION_PRESETS.map((p) => {
    const label = p.id === "viewport"
      ? `Viewport (${viewportSize.width} × ${viewportSize.height})`
      : p.label;
    return `<option value="${p.id}"${clamped.resolutionPreset === p.id ? " selected" : ""}>${escapeHtml(label)}</option>`;
  }).join("");

  const tourDur = Number(tour?.transitionDurationSec) > 0 ? Number(tour.transitionDurationSec) : DEFAULT_TOUR_TRANSITION_SEC;
  const canTrans = plan.canTransition;
  const timingActive = norm.includeTransitions || norm.holdSeconds > 0;
  const pct = state.progressTotal > 0 ? Math.min(100, Math.round((state.progressFrame / state.progressTotal) * 100)) : 0;
  const summaryText = steps.length
    ? `${plan.totalFrames} frame${plan.totalFrames === 1 ? "" : "s"}${norm.fpsApplied ? ` · ~${formatDuration(plan.approxDurationSec)} @ ${norm.fps} fps` : " (target stops only)"}`
    : "0 frames (no targets)";
  const previewText = preview.single ? preview.first : `${preview.first} → ${preview.last}`;

  return `${toggleBtn}
    <div class="nm-tour-export-panel nm-tour-export-ui" data-tour-export-ui="1" aria-label="Tour frame export">
      <div class="nm-tour-export-section">
        <span class="inspector-label">Capture scope</span>
        <label class="inspector-field nm-insp-check"${canTrans ? "" : ' title="Select a range with at least 2 targets to enable transitions"'}>
          <input type="checkbox" data-insp="tour-export-transitions" ${clamped.includeTransitions && canTrans ? "checked" : ""}${!canTrans || running ? " disabled" : ""}/>
          <span>Include transitions between targets</span>
        </label>
        ${!canTrans ? '<span class="inspector-val nm-hint nm-tour-export-hint">Transitions disabled: range needs at least 2 targets.</span>' : ""}
        <label class="inspector-field">
          <span class="inspector-label">Target range</span>
          <select data-insp="tour-export-range"${lockAttr}>
            <option value="all"${clamped.rangeMode !== "custom" ? " selected" : ""}>Entire tour (${steps.length} target${steps.length === 1 ? "" : "s"})</option>
            <option value="custom"${clamped.rangeMode === "custom" ? " selected" : ""}${steps.length ? "" : " disabled"}>Custom range (From → To)</option>
          </select>
        </label>
        ${clamped.rangeMode === "custom" && steps.length ? `
          <div class="nm-insp-pair">
            <label class="inspector-field"><span class="inspector-label">From Target</span><select data-insp="tour-export-from"${lockAttr}>${targetOptions(clamped.fromTarget)}</select></label>
            <label class="inspector-field"><span class="inspector-label">To Target</span><select data-insp="tour-export-to"${lockAttr}>${targetOptions(clamped.toTarget)}</select></label>
          </div>` : ""}
      </div>

      <div class="nm-tour-export-section">
        <span class="inspector-label">Transition &amp; timing</span>
        <div class="nm-insp-pair">
          <label class="inspector-field">
            <span class="inspector-label">Transition FPS</span>
            <select data-insp="tour-export-fps-preset"${!timingActive || running ? " disabled" : ""}>${fpsOptions}</select>
          </label>
          ${clamped.fpsPreset === "custom" ? `
            <label class="inspector-field">
              <span class="inspector-label">Custom FPS (1–240)</span>
              <input type="number" min="1" max="240" step="1" data-insp="tour-export-fps-custom" value="${escapeHtml(clamped.customFps)}"${!timingActive || running ? " disabled" : ""}/>
            </label>` : `
            <label class="inspector-field">
              <span class="inspector-label">Hold / stop (s)</span>
              <input type="number" min="0" step="0.1" data-insp="tour-export-hold" value="${escapeHtml(clamped.holdSeconds)}"${lockAttr}/>
            </label>`}
        </div>
        ${clamped.fpsPreset === "custom" ? `
          <label class="inspector-field">
            <span class="inspector-label">Hold per target stop (s)</span>
            <input type="number" min="0" step="0.1" data-insp="tour-export-hold" value="${escapeHtml(clamped.holdSeconds)}"${lockAttr}/>
          </label>` : ""}
        <label class="inspector-field">
          <span class="inspector-label">Transition duration override (s)</span>
          <input type="number" min="0.01" step="0.05" placeholder="${tourDur}s (tour default)" data-insp="tour-export-duration" value="${escapeHtml(clamped.durationOverride)}"${!norm.includeTransitions || running ? " disabled" : ""}/>
        </label>
      </div>

      <div class="nm-tour-export-section">
        <span class="inspector-label">File naming</span>
        <div class="nm-insp-pair">
          <label class="inspector-field"><span class="inspector-label">Prefix</span><input data-insp="tour-export-prefix" value="${escapeHtml(clamped.prefix)}" spellcheck="false"${lockAttr}/></label>
          <label class="inspector-field"><span class="inspector-label">Suffix</span><input data-insp="tour-export-suffix" value="${escapeHtml(clamped.suffix)}" spellcheck="false"${lockAttr}/></label>
        </div>
        <div class="inspector-val nm-tour-export-preview" aria-live="polite">${escapeHtml(previewText)}</div>
      </div>

      <div class="nm-tour-export-section">
        <span class="inspector-label">Visual &amp; resolution</span>
        <label class="inspector-field nm-insp-check">
          <input type="checkbox" data-insp="tour-export-transparent" ${clamped.transparent ? "checked" : ""}${lockAttr}/>
          <span>Transparent background (RGBA)</span>
        </label>
        <label class="inspector-field nm-insp-check">
          <input type="checkbox" data-insp="tour-export-hide-hud" ${clamped.hideHud ? "checked" : ""}${lockAttr}/>
          <span>Hide HUD / toolbars / controls</span>
        </label>
        <label class="inspector-field">
          <span class="inspector-label">Output resolution</span>
          <select data-insp="tour-export-res"${lockAttr}>${resOptions}</select>
        </label>
        ${clamped.resolutionPreset === "custom" ? `
          <div class="nm-insp-pair">
            <label class="inspector-field"><span class="inspector-label">Width (px)</span><input type="number" min="16" max="8192" step="1" data-insp="tour-export-width" value="${escapeHtml(clamped.customWidth)}"${lockAttr}/></label>
            <label class="inspector-field"><span class="inspector-label">Height (px)</span><input type="number" min="16" max="8192" step="1" data-insp="tour-export-height" value="${escapeHtml(clamped.customHeight)}"${lockAttr}/></label>
          </div>` : ""}
      </div>

      ${!pickerSupport.supported ? `<div class="nm-tour-export-notice">${escapeHtml(pickerSupport.notice)}</div>` : ""}
      <div class="nm-tour-export-errors" role="alert"${plan.errors.length ? "" : " hidden"}>${plan.errors.map((e) => `<div class="nm-tour-export-error">${escapeHtml(e)}</div>`).join("")}</div>
      <div class="nm-tour-export-warnings"${plan.warnings.length ? "" : " hidden"}>${plan.warnings.map((w) => `<div class="nm-tour-export-warn">${escapeHtml(w)}</div>`).join("")}</div>
      ${state.errorBanner ? `<div class="nm-tour-export-error">${escapeHtml(state.errorBanner)}</div>` : ""}

      <div class="nm-tour-export-summary" aria-live="polite">${escapeHtml(summaryText)}</div>

      <div class="nm-insp-actions">
        <button type="button" class="nodemap-btn nm-tour-export-start" data-insp="tour-export-start"${running || !plan.valid || !steps.length ? " disabled" : ""}>
          ${running ? "Exporting…" : "Export PNG Frames"}
        </button>
        ${running ? '<button type="button" class="nodemap-btn nm-danger nm-tour-export-cancel" data-insp="tour-export-cancel">Cancel</button>' : ""}
      </div>

      <div class="nm-tour-export-progress" data-tour-export-ui="1"${running || state.statusText ? "" : " hidden"} aria-live="polite">
        ${running ? `<div class="nm-tour-export-bar-track"><div class="nm-tour-export-bar-fill" style="width:${pct}%"></div></div>` : ""}
        <div class="inspector-val nm-tour-export-status">${escapeHtml(
          running
            ? `Frame ${state.progressFrame} / ${state.progressTotal}${state.progressLabel ? ` · ${state.progressLabel}` : ""}`
            : state.statusText
        )}</div>
      </div>
    </div>`;
}

/** Update dynamic export panel indicators in-place without destroying focused input cursors. */
export function refreshTourExportPanel(ctrl) {
  const root = ctrl?.inspectorEl?.querySelector?.(".nm-tour-export-panel");
  if (!root) return false;
  const tour = getActiveTour(ctrl);
  const state = ctrl.tourExport || createTourExportState();
  const vpSize = controllerViewportSize(ctrl);
  const { plan, preview } = computeTourExportPreview(tour, state, vpSize);
  const norm = plan.normalized;
  const running = Boolean(state.running);

  const q = (sel) => root.querySelector(sel);
  const previewEl = q(".nm-tour-export-preview");
  if (previewEl) previewEl.textContent = preview.single ? preview.first : `${preview.first} → ${preview.last}`;

  const summaryEl = q(".nm-tour-export-summary");
  if (summaryEl) {
    const steps = Array.isArray(tour?.steps) ? tour.steps : [];
    summaryEl.textContent = steps.length
      ? `${plan.totalFrames} frame${plan.totalFrames === 1 ? "" : "s"}${norm.fpsApplied ? ` · ~${formatDuration(plan.approxDurationSec)} @ ${norm.fps} fps` : " (target stops only)"}`
      : "0 frames (no targets)";
  }

  const errBox = q(".nm-tour-export-errors");
  if (errBox) {
    errBox.innerHTML = plan.errors.map((e) => `<div class="nm-tour-export-error">${escapeHtml(e)}</div>`).join("");
    errBox.hidden = !plan.errors.length;
  }

  const warnBox = q(".nm-tour-export-warnings");
  if (warnBox) {
    warnBox.innerHTML = plan.warnings.map((w) => `<div class="nm-tour-export-warn">${escapeHtml(w)}</div>`).join("");
    warnBox.hidden = !plan.warnings.length;
  }

  const transEl = q("[data-insp='tour-export-transitions']");
  if (transEl) {
    transEl.disabled = !plan.canTransition || running;
    if (!plan.canTransition) transEl.checked = false;
  }

  const timingActive = norm.includeTransitions || norm.holdSeconds > 0;
  const fpsPresetEl = q("[data-insp='tour-export-fps-preset']");
  if (fpsPresetEl) fpsPresetEl.disabled = !timingActive || running;
  const fpsCustomEl = q("[data-insp='tour-export-fps-custom']");
  if (fpsCustomEl) fpsCustomEl.disabled = !timingActive || running;
  const durEl = q("[data-insp='tour-export-duration']");
  if (durEl) durEl.disabled = !norm.includeTransitions || running;

  const startBtn = q("[data-insp='tour-export-start']");
  if (startBtn) startBtn.disabled = running || !plan.valid || !(tour?.steps?.length);

  const progBox = q(".nm-tour-export-progress");
  if (progBox) {
    progBox.hidden = !(running || state.statusText);
    const fill = progBox.querySelector(".nm-tour-export-bar-fill");
    if (fill) {
      const pct = state.progressTotal > 0 ? Math.min(100, Math.round((state.progressFrame / state.progressTotal) * 100)) : 0;
      fill.style.width = `${pct}%`;
    }
    const statusEl = progBox.querySelector(".nm-tour-export-status");
    if (statusEl) {
      statusEl.textContent = running
        ? `Frame ${state.progressFrame} / ${state.progressTotal}${state.progressLabel ? ` · ${state.progressLabel}` : ""}`
        : state.statusText;
    }
  }
  return true;
}

/** Handle Inspector input/change events for `data-insp="tour-export-*"`. */
export function handleTourExportInput(ctrl, k, el, final) {
  if (!ctrl.tourExport) ctrl.tourExport = createTourExportState();
  const st = ctrl.tourExport;
  if (st.running) return;
  st.errorBanner = "";
  const val = el.type === "checkbox" ? Boolean(el.checked) : String(el.value ?? "");
  let rerender = false;

  if (k === "tour-export-transitions") {
    st.includeTransitions = Boolean(val);
    rerender = true;
  } else if (k === "tour-export-range") {
    st.rangeMode = val === "custom" ? "custom" : "all";
    const steps = getActiveTour(ctrl)?.steps || [];
    if (st.rangeMode === "custom" && steps.length) {
      st.fromTarget = Math.max(1, Math.min(Number(st.fromTarget) || 1, steps.length));
      st.toTarget = Math.max(st.fromTarget, Math.min(Number(st.toTarget) || steps.length, steps.length));
    }
    rerender = true;
  } else if (k === "tour-export-from") {
    st.fromTarget = Math.max(1, parseInt(val, 10) || 1);
    if (Number(st.toTarget) < st.fromTarget) st.toTarget = st.fromTarget;
    rerender = true;
  } else if (k === "tour-export-to") {
    st.toTarget = Math.max(1, parseInt(val, 10) || 1);
    if (Number(st.fromTarget) > st.toTarget) st.fromTarget = st.toTarget;
    rerender = true;
  } else if (k === "tour-export-fps-preset") {
    st.fpsPreset = val;
    if (val !== "custom") st.customFps = val;
    rerender = true;
  } else if (k === "tour-export-fps-custom") {
    st.customFps = val;
  } else if (k === "tour-export-duration") {
    st.durationOverride = val;
  } else if (k === "tour-export-hold") {
    st.holdSeconds = val;
  } else if (k === "tour-export-prefix") {
    st.prefix = val;
  } else if (k === "tour-export-suffix") {
    st.suffix = val;
  } else if (k === "tour-export-transparent") {
    st.transparent = Boolean(val);
  } else if (k === "tour-export-hide-hud") {
    st.hideHud = Boolean(val);
  } else if (k === "tour-export-res") {
    st.resolutionPreset = val;
    rerender = true;
  } else if (k === "tour-export-width") {
    st.customWidth = val;
  } else if (k === "tour-export-height") {
    st.customHeight = val;
  } else {
    return;
  }

  if (rerender || final && (el.tagName === "SELECT" || el.type === "checkbox")) {
    ctrl.renderInspector?.();
  } else if (!refreshTourExportPanel(ctrl)) {
    ctrl.renderInspector?.();
  }
}

/** Handle Inspector click events for `data-insp="tour-export-*"`. */
export function handleTourExportClick(ctrl, k) {
  if (!ctrl.tourExport) ctrl.tourExport = createTourExportState();
  const st = ctrl.tourExport;
  if (k === "tour-export-toggle") {
    if (st.running) return;
    st.open = !st.open;
    st.statusText = "";
    st.errorBanner = "";
    ctrl.renderInspector?.();
    return;
  }
  if (k === "tour-export-cancel") {
    cancelTourFrameExport(ctrl);
    return;
  }
  if (k === "tour-export-start") {
    startTourFrameExport(ctrl);
  }
}

/** Cancel any active tour frame export run. */
export function cancelTourFrameExport(ctrl) {
  const run = ctrl?._tourExportRun;
  if (!run) return false;
  run.abortController.abort(new DOMException("Export cancelled", "AbortError"));
  return true;
}

// ------------------------------------------------------------------ Rendering & Compositing Helpers

function defaultCreateCanvas(w, h) {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  return c;
}

function defaultCanvasToBlob(canvas) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("Canvas failed to encode PNG blob"));
    }, "image/png");
  });
}

function defaultLoadSvgImage(svg, { signal } = {}) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Export cancelled", "AbortError"));
      return;
    }
    const img = new Image();
    let url = "";
    const hasObjectUrl = typeof URL?.createObjectURL === "function" && typeof Blob !== "undefined";
    if (hasObjectUrl) {
      url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }));
    }
    const release = () => {
      if (url && typeof URL?.revokeObjectURL === "function") {
        URL.revokeObjectURL(url);
        url = "";
      }
    };
    const onAbort = () => {
      release();
      reject(new DOMException("Export cancelled", "AbortError"));
    };
    signal?.addEventListener?.("abort", onAbort, { once: true });
    img.onload = () => {
      signal?.removeEventListener?.("abort", onAbort);
      release();
      resolve(img);
    };
    img.onerror = () => {
      signal?.removeEventListener?.("abort", onAbort);
      release();
      reject(new Error("Failed to decode SVG frame"));
    };
    img.decoding = "sync";
    img.src = url || `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  });
}

async function drawSvgFrameOnCanvas(ctx, svg, outW, outH, deps, signal) {
  const img = await deps.loadSvgImage(svg, { signal, width: outW, height: outH });
  try {
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(img, 0, 0, outW, outH);
  } finally {
    if (typeof img?.close === "function") {
      try {
        img.close();
      } catch {
        // already closed
      }
    }
  }
}

function abortableDelay(ms, signal) {
  if (ms <= 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Export cancelled", "AbortError"));
      return;
    }
    const t = setTimeout(() => {
      signal?.removeEventListener?.("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(t);
      reject(new DOMException("Export cancelled", "AbortError"));
    };
    signal?.addEventListener?.("abort", onAbort, { once: true });
  });
}

function syncTourBarForFrame(ctrl, tour, stepIndex) {
  const tp = ctrl?.tourPlayer;
  if (!tp || !tour) return;
  const steps = Array.isArray(tour.steps) ? tour.steps : [];
  const total = steps.length;
  const idx = Math.max(0, Math.min(stepIndex, Math.max(0, total - 1)));
  const step = steps[idx] || null;
  if (tp.el) tp.el.hidden = false;
  if (tp.counterEl) tp.counterEl.textContent = total ? `${idx + 1} / ${total}` : "0 / 0";
  if (tp.titleEl) tp.titleEl.textContent = step ? step.title : (tour.title || "");
  if (tp.captionEl) {
    const cap = step?.caption || "";
    tp.captionEl.textContent = cap;
    tp.captionEl.hidden = !cap;
  }
  if (tp.prevBtn) tp.prevBtn.disabled = !total || idx <= 0;
  if (tp.nextBtn) tp.nextBtn.disabled = !total || idx >= total - 1;
}

/** Collect visible HUD overlay elements (always excluding the export UI itself). */
export function visibleHudOverlays(ctrl) {
  const root = ctrl?.rootEl;
  const wrap = ctrl?.wrapEl;
  if (!root && !wrap) return [];
  const candidates = [];
  const collectChildren = (parent) => {
    for (const child of parent?.children || []) {
      if (!child) continue;
      const cls = child.classList;
      if (cls?.contains?.("nodemap-overlay-layer")) continue;
      if (cls?.contains?.("nodemap-canvas-wrapper")) {
        collectChildren(child);
        continue;
      }
      if (cls?.contains?.("nodemap-inspector")) continue;
      if (cls?.contains?.("nm-guides")) continue;
      if (cls?.contains?.("nm-tour-export-ui") || child.dataset?.tourExportUi || child.getAttribute?.("data-tour-export-ui")) continue;
      if (child.tagName === "INPUT" && child.type === "file") continue;
      if (child.hidden) continue;
      try {
        const cs = globalThis.getComputedStyle?.(child);
        if (cs?.getPropertyValue?.("display") === "none" || cs?.getPropertyValue?.("visibility") === "hidden") continue;
      } catch {
        // test stub without getComputedStyle
      }
      candidates.push(child);
    }
  };
  collectChildren(root || wrap);
  if (root && wrap && wrap.parentNode !== root) collectChildren(wrap);
  return candidates;
}

async function compositeHudOverlays(ctx, ctrl, { outW, outH, refW, refH }, deps, signal) {
  const baseEl = ctrl?.wrapEl || ctrl?.rootEl;
  const baseRect = baseEl?.getBoundingClientRect?.() || { left: 0, top: 0, width: refW, height: refH };
  const hudW = baseRect.width > 0 ? baseRect.width : refW;
  const hudH = baseRect.height > 0 ? baseRect.height : refH;
  const hudScale = Math.min(outW / hudW, outH / hudH);

  for (const el of visibleHudOverlays(ctrl)) {
    if (signal?.aborted) throw new DOMException("Export cancelled", "AbortError");
    const r = el.getBoundingClientRect?.();
    if (!r || !(r.width > 0 && r.height > 0)) continue;
    const dw = r.width * hudScale;
    const dh = r.height * hudScale;
    const relLeft = r.left - baseRect.left;
    const relTop = r.top - baseRect.top;
    const relRight = baseRect.left + hudW - r.right;
    const relBottom = baseRect.top + hudH - r.bottom;
    const centerX = relLeft + r.width / 2;
    const centerY = relTop + r.height / 2;

    let dx;
    if (Math.abs(centerX - hudW / 2) < hudW * 0.12 && el.classList?.contains?.("nm-tour-bar")) {
      dx = (outW - dw) / 2;
    } else if (centerX > hudW * 0.6) {
      dx = outW - relRight * hudScale - dw;
    } else {
      dx = relLeft * hudScale;
    }
    const dy = centerY > hudH * 0.6 ? outH - relBottom * hudScale - dh : relTop * hudScale;

    if (typeof deps.captureHudElement === "function") {
      await deps.captureHudElement({ ctx, el, dx, dy, dw, dh, hudScale, signal });
      continue;
    }

    const fo = captureElementForeignObject(el);
    if (fo?.html) {
      const svg = `<svg xmlns="${SVG_NS}" width="${Math.max(1, Math.round(dw))}" height="${Math.max(1, Math.round(dh))}" viewBox="0 0 ${fo.w} ${fo.h}"><foreignObject x="0" y="0" width="${fo.w}" height="${fo.h}"><div xmlns="http://www.w3.org/1999/xhtml" style="width:${fo.w}px;height:${fo.h}px;overflow:hidden">${fo.html}</div></foreignObject></svg>`;
      try {
        const img = await deps.loadSvgImage(svg, { signal });
        ctx.drawImage(img, dx, dy, dw, dh);
        img.close?.();
        continue;
      } catch (err) {
        if (err?.name === "AbortError") throw err;
      }
    }

    const win = el.ownerDocument?.defaultView || globalThis;
    const paint = collectPaintOps({ body: el, root: el, win, frame: null });
    if (paint?.ops?.length) {
      ctx.save();
      ctx.translate(dx, dy);
      ctx.scale(hudScale, hudScale);
      for (const op of paint.ops) {
        ctx.globalAlpha = op.alpha ?? 1;
        if (op.k === "rect") {
          ctx.fillStyle = op.fill;
          ctx.fillRect(op.x, op.y, op.w, op.h);
        } else if (op.k === "text") {
          ctx.font = op.font;
          ctx.fillStyle = op.fill;
          ctx.textBaseline = "middle";
          ctx.fillText(op.text, op.x, op.y + op.h / 2);
        }
      }
      ctx.restore();
    }
  }
}

async function writeFrameToDirectory(dirHandle, filename, blob, signal) {
  if (signal?.aborted) throw new DOMException("Export cancelled", "AbortError");
  const fileHandle = await dirHandle.getFileHandle(filename, { create: true });
  const writable = await fileHandle.createWritable();
  let closed = false;
  try {
    if (signal?.aborted) throw new DOMException("Export cancelled", "AbortError");
    await writable.write(blob);
    if (signal?.aborted) throw new DOMException("Export cancelled", "AbortError");
    await writable.close();
    closed = true;
  } finally {
    if (!closed) {
      try {
        await writable.abort?.();
      } catch {
        try {
          await writable.close?.();
        } catch {
          // ignore cleanup error
        }
      }
    }
  }
}

function defaultDownloadFrame(filename, blob) {
  const a = document.createElement("a");
  const url = URL.createObjectURL(blob);
  a.href = url;
  a.download = filename;
  document.body.appendChild?.(a);
  a.click();
  a.remove?.();
  setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_REVOKE_MS);
}

function defaultConfirmFallback(totalFrames, notice) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      resolve(ok);
    };
    const modal = openModal({
      title: `Export ${totalFrames} PNG Frames?`,
      subtitle: "Individual browser downloads fallback",
      width: "440px",
      footerHtml: '<button type="button" class="code-btn primary" data-act="confirm">Start Downloads</button>',
      onClose: () => finish(false),
    });
    modal.body.innerHTML = `<div class="nm-form">
      <p class="nm-tour-export-notice">${escapeHtml(notice)}</p>
      <p>This will trigger <strong>${totalFrames}</strong> individual PNG file downloads. Continue?</p>
    </div>`;
    const btn = modal.footer?.querySelector?.("[data-act='confirm']");
    if (btn) {
      btn.onclick = () => {
        finish(true);
        modal.close();
      };
    }
  });
}

// ------------------------------------------------------------------ Export Lifecycle

/**
 * Trigger Tour Frame Export from the Export click handler.
 * Synchronously invokes `window.showDirectoryPicker` before any `await` so user activation is preserved.
 */
export function startTourFrameExport(ctrl, deps = {}) {
  if (!ctrl || ctrl._tourExportRun) return null;
  if (!ctrl.tourExport) ctrl.tourExport = createTourExportState();
  const tour = getActiveTour(ctrl);
  const vpSize = controllerViewportSize(ctrl);
  const { plan } = computeTourExportPreview(tour, ctrl.tourExport, vpSize);
  if (!plan.valid || !plan.totalFrames) {
    refreshTourExportPanel(ctrl);
    return null;
  }

  const win = deps.win || globalThis.window;
  const pickerSupport = detectDirectoryPickerSupport(win);
  let pickerPromise = null;
  if (pickerSupport.supported && typeof deps.showDirectoryPicker !== "function") {
    try {
      pickerPromise = win.showDirectoryPicker({ mode: "readwrite" });
    } catch (err) {
      if (err?.name === "AbortError") return Promise.resolve({ cancelled: true, reason: "picker-aborted", written: 0 });
      ctrl.tourExport.errorBanner = err?.message || "Could not open folder picker.";
      ctrl.renderInspector?.();
      return Promise.resolve({ cancelled: true, reason: "picker-error", written: 0, error: err });
    }
  }

  return runTourFrameExport(ctrl, ctrl.tourExport, { ...deps, pickerPromise, pickerSupport });
}

/**
 * Execute deterministic stepped Tour Frame Export and restore all live Nodemap state in `finally`.
 */
export async function runTourFrameExport(ctrl, rawOpts = null, deps = {}) {
  if (!ctrl) throw new Error("Controller is required");
  if (ctrl._tourExportRun) return { cancelled: true, reason: "already-running", written: 0 };

  if (!ctrl.tourExport) ctrl.tourExport = createTourExportState();
  const opts = rawOpts || ctrl.tourExport;
  const tour = deps.tour || getActiveTour(ctrl);
  const vpSize = deps.viewportSize || controllerViewportSize(ctrl);
  const plan = planTourFrames(tour, opts, vpSize);
  if (!plan.valid || !plan.totalFrames) {
    return { cancelled: true, reason: "invalid-plan", errors: plan.errors, written: 0 };
  }

  const win = deps.win || globalThis.window;
  const doc = deps.doc || globalThis.document;
  const pickerSupport = deps.pickerSupport || detectDirectoryPickerSupport(win);
  const createCanvas = deps.createCanvas || defaultCreateCanvas;
  const canvasToBlob = deps.canvasToBlob || defaultCanvasToBlob;
  const loadSvgImage = deps.loadSvgImage || defaultLoadSvgImage;
  const downloadFile = deps.downloadFile || defaultDownloadFrame;
  const confirmFallback = deps.confirmFallback || defaultConfirmFallback;
  const yieldFrame = deps.yieldFrame || (() => new Promise((r) => setTimeout(r, 0)));
  const fontsReady = deps.fontsReady || (() => doc?.fonts?.ready || Promise.resolve());
  const staggerMs = deps.staggerMs ?? STAGGER_DOWNLOAD_MS;

  let dirHandle = deps.dirHandle || null;
  if (!dirHandle && pickerSupport.supported) {
    try {
      const p = deps.pickerPromise || (typeof deps.showDirectoryPicker === "function"
        ? deps.showDirectoryPicker({ mode: "readwrite" })
        : win.showDirectoryPicker({ mode: "readwrite" }));
      dirHandle = await p;
    } catch (err) {
      if (err?.name === "AbortError") {
        return { cancelled: true, reason: "picker-aborted", written: 0 };
      }
      ctrl.tourExport.errorBanner = err?.message || "Folder picker failed.";
      ctrl.renderInspector?.();
      return { cancelled: true, reason: "picker-error", written: 0, error: err };
    }
  }

  if (!dirHandle && plan.totalFrames > FALLBACK_CONFIRM_ABOVE) {
    const ok = await confirmFallback(plan.totalFrames, pickerSupport.notice);
    if (!ok) return { cancelled: true, reason: "fallback-declined", written: 0 };
  }

  const runSeq = (ctrl._tourExportSeq || 0) + 1;
  ctrl._tourExportSeq = runSeq;
  const abortController = new AbortController();
  const { signal } = abortController;
  const run = { id: runSeq, abortController };
  ctrl._tourExportRun = run;

  // Snapshot live state for guaranteed restoration in finally
  const tp = ctrl.tourPlayer || null;
  const prevState = {
    viewport: ctrl.canvas?.viewport ? { ...ctrl.canvas.viewport } : null,
    selected: ctrl.selected ? new Set(ctrl.selected) : new Set(),
    selectedEdge: ctrl.selectedEdge ?? null,
    active: ctrl.active ?? null,
    tourId: tp?.tourId ?? null,
    stepIndex: tp?.stepIndex ?? 0,
    tourElHidden: tp?.el ? Boolean(tp.el.hidden) : true,
    counterText: tp?.counterEl?.textContent ?? "",
    titleText: tp?.titleEl?.textContent ?? "",
    captionText: tp?.captionEl?.textContent ?? "",
    captionHidden: tp?.captionEl ? Boolean(tp.captionEl.hidden) : true,
    prevDisabled: tp?.prevBtn ? Boolean(tp.prevBtn.disabled) : false,
    nextDisabled: tp?.nextBtn ? Boolean(tp.nextBtn.disabled) : false,
    hadTourActiveClass: Boolean(ctrl.rootEl?.classList?.contains?.("nm-tour-active")),
  };

  if (tp?._animTimer) {
    clearTimeout(tp._animTimer);
    tp._animTimer = null;
  }
  ctrl.layerEl?.classList?.remove?.("nm-tour-anim");
  ctrl.wrapEl?.classList?.remove?.("nm-moving");
  ctrl.rootEl?.classList?.add?.("nm-tour-export-noanim", "nm-tour-export-locked");

  ctrl.tourExport.running = true;
  ctrl.tourExport.progressFrame = 0;
  ctrl.tourExport.progressTotal = plan.totalFrames;
  ctrl.tourExport.progressLabel = "Preparing fonts & canvas…";
  ctrl.tourExport.statusText = "";
  ctrl.tourExport.errorBanner = "";
  ctrl.renderInspector?.();

  let written = 0;
  let rasters = null;
  const { width: outW, height: outH } = plan.normalized.resolution;
  const { refWidth, refHeight, transparent, hideHud, prefix, suffix } = plan.normalized;

  try {
    await fontsReady();
    if (signal.aborted) throw new DOMException("Export cancelled", "AbortError");
    // high-quality nested pictures rendered once here; every frame embeds the same cached snapshot
    if (ctrl.hasHighQualityNested?.()) await Promise.resolve(ctrl.prepareNestedSnapshots?.()).catch(() => 0);
    if (signal.aborted) throw new DOMException("Export cancelled", "AbortError");

    // Pre-rasterize live DOM nodes (tab, man, doc) once at the maximum frame scale
    const visSlice = ctrl.canvas ? visibleCanvasSlice(ctrl.canvas) : null;
    const hasLive = Boolean(visSlice?.nodes?.some((n) => LIVE_NODE_TYPES.includes(n.type)) && ctrl.renderer);
    if (hasLive) {
      const maxScale = plan.frames.reduce((m, f) => Math.max(m, f.viewBox.scale || 1), 1);
      const rasterScale = Math.max(1, Math.min(4, maxScale));
      ctrl._exportingFrames = true;
      let prep = null;
      try {
        prep = typeof ctrl.prepareExportFrames === "function" ? await ctrl.prepareExportFrames() : null;
        if (!signal.aborted) {
          ({ rasters } = await liveNodeRasters(ctrl, rasterScale, deps.env || browserPngEnv()));
        }
      } finally {
        prep?.restore?.();
        ctrl._exportingFrames = false;
      }
    }

    const rastersOf = rasters && [...rasters.values()].some(Boolean) ? (n) => rasters.get(n.id) || null : null;
    const workCanvas = createCanvas(outW, outH);
    const ctx = workCanvas?.getContext?.("2d");
    if (!ctx) throw new Error(`Could not allocate ${outW} × ${outH} export canvas.`);

    for (const frame of plan.frames) {
      if (signal.aborted) throw new DOMException("Export cancelled", "AbortError");

      ctrl.tourExport.progressFrame = frame.frameNumber;
      ctrl.tourExport.progressLabel = frame.label;
      refreshTourExportPanel(ctrl);

      const discreteStep = tour.steps[frame.discreteStepIndex] || null;
      const tourHighlights = tourStepHighlights(discreteStep);

      ctx.clearRect(0, 0, outW, outH);

      const svg = ctrl.canvasSvg({
        transparent,
        foreign: false,
        viewportBox: frame.viewBox,
        tourHighlights,
        rasters: rastersOf,
      });

      await drawSvgFrameOnCanvas(ctx, svg, outW, outH, { loadSvgImage }, signal);

      if (!hideHud) {
        syncTourBarForFrame(ctrl, tour, frame.discreteStepIndex);
        await compositeHudOverlays(
          ctx,
          ctrl,
          { outW, outH, refW: refWidth, refH: refHeight },
          { loadSvgImage, captureHudElement: deps.captureHudElement },
          signal
        );
      }

      if (signal.aborted) throw new DOMException("Export cancelled", "AbortError");
      const blob = await canvasToBlob(workCanvas);
      const filename = formatFrameFilename(frame.frameNumber, plan.totalFrames, { prefix, suffix });

      if (dirHandle) {
        await writeFrameToDirectory(dirHandle, filename, blob, signal);
      } else {
        downloadFile(filename, blob);
        if (frame.frameNumber < plan.totalFrames) {
          await abortableDelay(staggerMs, signal);
        }
      }

      written += 1;
      await yieldFrame();
    }

    ctrl.tourExport.statusText = `Exported ${written} PNG frame${written === 1 ? "" : "s"}.`;
    toast.success(`Exported ${written} tour PNG frame${written === 1 ? "" : "s"}`);
    return { cancelled: false, written, totalFrames: plan.totalFrames, width: outW, height: outH };
  } catch (err) {
    if (err?.name === "AbortError" || signal.aborted) {
      ctrl.tourExport.statusText = `Cancelled after ${written} of ${plan.totalFrames} frame${plan.totalFrames === 1 ? "" : "s"}.`;
      toast.info(`Tour export cancelled (${written}/${plan.totalFrames} frames written)`);
      return { cancelled: true, reason: "cancelled", written, totalFrames: plan.totalFrames };
    }
    ctrl.tourExport.errorBanner = err?.message || "Tour frame export failed.";
    ctrl.tourExport.statusText = `Failed after ${written} frame${written === 1 ? "" : "s"}.`;
    toast.error(`Tour export failed: ${ctrl.tourExport.errorBanner}`);
    return { cancelled: false, error: err, written, totalFrames: plan.totalFrames };
  } finally {
    if (ctrl._tourExportRun === run) ctrl._tourExportRun = null;
    ctrl.tourExport.running = false;
    ctrl._exportingFrames = false;

    ctrl.rootEl?.classList?.remove?.("nm-tour-export-noanim", "nm-tour-export-locked");

    if (ctrl.canvas && prevState.viewport) {
      ctrl.canvas.viewport = { ...prevState.viewport };
    }
    ctrl.selected = prevState.selected;
    ctrl.selectedEdge = prevState.selectedEdge;
    ctrl.active = prevState.active;

    if (tp) {
      tp.tourId = prevState.tourId;
      tp.stepIndex = prevState.stepIndex;
      if (tp.el) tp.el.hidden = prevState.tourElHidden;
      if (tp.counterEl) tp.counterEl.textContent = prevState.counterText;
      if (tp.titleEl) tp.titleEl.textContent = prevState.titleText;
      if (tp.captionEl) {
        tp.captionEl.textContent = prevState.captionText;
        tp.captionEl.hidden = prevState.captionHidden;
      }
      if (tp.prevBtn) tp.prevBtn.disabled = prevState.prevDisabled;
      if (tp.nextBtn) tp.nextBtn.disabled = prevState.nextDisabled;
      if (prevState.tourId) tp.applyHighlights?.();
      else {
        tp.applyHighlights?.();
        ctrl.rootEl?.classList?.toggle?.("nm-tour-active", prevState.hadTourActiveClass);
      }
    }

    ctrl.applyTransform?.();
    ctrl.renderWires?.();
    ctrl.renderInspector?.();
  }
}
