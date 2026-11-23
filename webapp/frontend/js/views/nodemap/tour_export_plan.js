/**
 * tour_export_plan.js - Pure, DOM-free timeline planner, camera interpolation, filename sanitizer,
 * resolution validator, and aspect-ratio-safe framing math for NodeMap Tour Frame Export.
 */
import { ZOOM_MAX, ZOOM_MIN } from "./canvas_model.js";

export const DEFAULT_TOUR_TRANSITION_SEC = 0.28;
export const FPS_PRESETS = [24, 30, 60];
export const MIN_FPS = 1;
export const MAX_FPS = 240;

export const RESOLUTION_PRESETS = [
  { id: "viewport", label: "Viewport (current view)" },
  { id: "1080p", label: "1920 × 1080 (Full HD)", width: 1920, height: 1080 },
  { id: "4k", label: "3840 × 2160 (4K UHD)", width: 3840, height: 2160 },
  { id: "custom", label: "Custom…" },
];

export const MIN_EXPORT_DIM = 16;
export const MAX_EXPORT_DIM = 8192;
export const MAX_EXPORT_AREA = 33554432; // 8192 × 4096 / ~8K UHD safe browser canvas ceiling
export const WARN_EXPORT_AREA = 3840 * 2160; // above 4K UHD
export const WARN_FRAME_COUNT = 300;

export const DEFAULT_FILENAME_PREFIX = "frame_";
export const DEFAULT_FILENAME_SUFFIX = "";
export const MIN_FRAME_DIGITS = 5;
export const FALLBACK_CONFIRM_ABOVE = 20;

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/** Cubic ease-out curve matching NodeMap's tour transition (`1 - (1 - t)^3`). */
export function tourEaseOut(t) {
  const u = clamp(Number(t) || 0, 0, 1);
  return 1 - Math.pow(1 - u, 3);
}

/** Normalize reference viewport dimensions with positive fallbacks. */
export function normalizeViewportSize(viewportSize) {
  const rw = Number(viewportSize?.width);
  const rh = Number(viewportSize?.height);
  return {
    width: Number.isFinite(rw) && rw >= 1 ? Math.round(rw) : 1280,
    height: Number.isFinite(rh) && rh >= 1 ? Math.round(rh) : 720,
  };
}

/** Convert a NodeMap `{ x, y, zoom }` viewport into world-space center camera `{ cx, cy, zoom }`. */
export function viewportToCamera(viewport, refWidth, refHeight) {
  const { width, height } = normalizeViewportSize({ width: refWidth, height: refHeight });
  const rawZ = Number(viewport?.zoom);
  const zoom = clamp(Number.isFinite(rawZ) && rawZ > 0 ? rawZ : 1, ZOOM_MIN, ZOOM_MAX);
  const vx = Number.isFinite(Number(viewport?.x)) ? Number(viewport.x) : 0;
  const vy = Number.isFinite(Number(viewport?.y)) ? Number(viewport.y) : 0;
  return {
    cx: (width / 2 - vx) / zoom,
    cy: (height / 2 - vy) / zoom,
    zoom,
  };
}

/** Convert world-space center camera `{ cx, cy, zoom }` back into NodeMap `{ x, y, zoom }` viewport. */
export function cameraToViewport(camera, refWidth, refHeight) {
  const { width, height } = normalizeViewportSize({ width: refWidth, height: refHeight });
  const rawZ = Number(camera?.zoom);
  const zoom = clamp(Number.isFinite(rawZ) && rawZ > 0 ? rawZ : 1, ZOOM_MIN, ZOOM_MAX);
  const cx = Number.isFinite(Number(camera?.cx)) ? Number(camera.cx) : width / (2 * zoom);
  const cy = Number.isFinite(Number(camera?.cy)) ? Number(camera.cy) : height / (2 * zoom);
  return {
    x: width / 2 - cx * zoom,
    y: height / 2 - cy * zoom,
    zoom,
  };
}

/**
 * Interpolate camera smoothly in world space using linear focal center + log-scale zoom,
 * matching NodeMap's tour camera curve and remaining NaN-free for identical targets.
 */
export function interpolateCamera(camA, camB, easedT) {
  const t = clamp(Number(easedT) || 0, 0, 1);
  const zA = clamp(Number(camA?.zoom) > 0 ? Number(camA.zoom) : 1, ZOOM_MIN, ZOOM_MAX);
  const zB = clamp(Number(camB?.zoom) > 0 ? Number(camB.zoom) : 1, ZOOM_MIN, ZOOM_MAX);
  const cxA = Number.isFinite(Number(camA?.cx)) ? Number(camA.cx) : 0;
  const cyA = Number.isFinite(Number(camA?.cy)) ? Number(camA.cy) : 0;
  const cxB = Number.isFinite(Number(camB?.cx)) ? Number(camB.cx) : cxA;
  const cyB = Number.isFinite(Number(camB?.cy)) ? Number(camB.cy) : cyA;
  const cx = cxA + (cxB - cxA) * t;
  const cy = cyA + (cyB - cyA) * t;
  const zoom = zA === zB
    ? zA
    : clamp(Math.exp(Math.log(zA) + (Math.log(zB) - Math.log(zA)) * t), ZOOM_MIN, ZOOM_MAX);
  return { cx, cy, zoom };
}

/**
 * Compute uniform-scaled SVG `viewBox` centered on `camera` for exact `outWidth × outHeight` pixels.
 * Preserves aspect ratio (zero stretching/skew) when `outWidth/outHeight` differs from `refWidth/refHeight`.
 */
export function computeExportViewBox(camera, outWidth, outHeight, refWidth, refHeight) {
  const ref = normalizeViewportSize({ width: refWidth, height: refHeight });
  const ow = Math.max(1, Math.round(Number(outWidth) || ref.width));
  const oh = Math.max(1, Math.round(Number(outHeight) || ref.height));
  const zoom = clamp(Number(camera?.zoom) > 0 ? Number(camera.zoom) : 1, ZOOM_MIN, ZOOM_MAX);
  const fitRatio = Math.min(ow / ref.width, oh / ref.height);
  const effectiveZoom = zoom * fitRatio;
  const vw = ow / effectiveZoom;
  const vh = oh / effectiveZoom;
  const cx = Number.isFinite(Number(camera?.cx)) ? Number(camera.cx) : 0;
  const cy = Number.isFinite(Number(camera?.cy)) ? Number(camera.cy) : 0;
  return {
    x: cx - vw / 2,
    y: cy - vh / 2,
    w: vw,
    h: vh,
    width: ow,
    height: oh,
    scale: effectiveZoom,
  };
}

// ------------------------------------------------------------------ Filename sanitization & formatting

// Strip /, \, control chars (0x00-0x1f, 0x7f), and OS-illegal characters <>:"|?*
// eslint-disable-next-line no-control-regex
const ILLEGAL_FILENAME_RE = /[/\\\x00-\x1f\x7f<>:"|?*]/g;

/**
 * Sanitize an editable filename prefix or suffix for safe cross-platform saving.
 * Strips path separators, null bytes, control chars, and OS-illegal characters; rejects `.` / `..`.
 */
export function sanitizeFilenameAffix(raw, { isPrefix = false } = {}) {
  let s = String(raw ?? "").replace(ILLEGAL_FILENAME_RE, "");
  if (s === "." || s === ".." || /^\.+$/.test(s)) return "";
  if (isPrefix) s = s.replace(/^\.+/, "");
  if (s === "." || s === "..") return "";
  return s;
}

/** Validate raw prefix/suffix inputs before saving and produce sanitized versions. */
export function validateFilenameConfig(prefix = DEFAULT_FILENAME_PREFIX, suffix = DEFAULT_FILENAME_SUFFIX) {
  const rawP = String(prefix ?? "");
  const rawS = String(suffix ?? "");
  const cleanP = sanitizeFilenameAffix(rawP, { isPrefix: true });
  const cleanS = sanitizeFilenameAffix(rawS, { isPrefix: false });
  const errors = [];
  const trimmedP = rawP.trim();
  const trimmedS = rawS.trim();
  if (trimmedP === "." || trimmedP === ".." || trimmedS === "." || trimmedS === "..") {
    errors.push("Prefix and suffix cannot be '.' or '..'.");
  }
  const stemSample = `${cleanP}00001${cleanS}`;
  if (!stemSample || stemSample === "." || stemSample === "..") {
    errors.push("Filename stem cannot be empty, '.', or '..'.");
  }
  return {
    valid: errors.length === 0,
    errors,
    prefix: cleanP,
    suffix: cleanS,
    strippedIllegal: cleanP !== rawP || cleanS !== rawS,
  };
}

/** Format a 1-based frame index as `{prefix}{paddedIndex}{suffix}.png` (min 5 digits, auto-widening). */
export function formatFrameFilename(frameNumber1Based, totalFrames = 1, { prefix = DEFAULT_FILENAME_PREFIX, suffix = DEFAULT_FILENAME_SUFFIX } = {}) {
  const n = Math.max(1, Math.floor(Number(frameNumber1Based) || 1));
  const total = Math.max(n, Math.floor(Number(totalFrames) || 1));
  const digits = Math.max(MIN_FRAME_DIGITS, String(total).length);
  const indexStr = String(n).padStart(digits, "0");
  let cleanP = sanitizeFilenameAffix(prefix, { isPrefix: true });
  const cleanS = sanitizeFilenameAffix(suffix, { isPrefix: false });
  let stem = `${cleanP}${indexStr}${cleanS}`;
  if (!stem || stem === "." || stem === ".." || /^\.+$/.test(stem)) {
    cleanP = DEFAULT_FILENAME_PREFIX;
    stem = `${cleanP}${indexStr}`;
  }
  return `${stem}.png`;
}

/** Build live first/last filename preview strings for the UI. */
export function filenamePreview(totalFrames, { prefix = DEFAULT_FILENAME_PREFIX, suffix = DEFAULT_FILENAME_SUFFIX } = {}) {
  const count = Math.max(1, Math.floor(Number(totalFrames) || 1));
  const first = formatFrameFilename(1, count, { prefix, suffix });
  const last = formatFrameFilename(count, count, { prefix, suffix });
  return { first, last, single: count <= 1 };
}

// ------------------------------------------------------------------ Resolution & option validation

export function defaultTourExportOptions() {
  return {
    open: false,
    includeTransitions: false,
    rangeMode: "all",
    fromTarget: 1,
    toTarget: 1,
    fpsPreset: "30",
    customFps: "30",
    durationOverride: "",
    holdSeconds: "0",
    prefix: DEFAULT_FILENAME_PREFIX,
    suffix: DEFAULT_FILENAME_SUFFIX,
    transparent: true,
    hideHud: true,
    resolutionPreset: "viewport",
    customWidth: "1920",
    customHeight: "1080",
  };
}

/** Resolve and validate output resolution in physical pixels (decoupled from devicePixelRatio). */
export function resolveExportDimensions(opts, viewportSize) {
  const vp = normalizeViewportSize(viewportSize);
  const preset = String(opts?.resolutionPreset || "viewport");
  let width = vp.width;
  let height = vp.height;
  const errors = [];
  const warnings = [];

  if (preset === "1080p") {
    width = 1920;
    height = 1080;
  } else if (preset === "4k") {
    width = 3840;
    height = 2160;
  } else if (preset === "custom") {
    const rawW = String(opts?.customWidth ?? "").trim();
    const rawH = String(opts?.customHeight ?? "").trim();
    const parsedW = /^[1-9]\d*$/.test(rawW) ? Number(rawW) : Number.NaN;
    const parsedH = /^[1-9]\d*$/.test(rawH) ? Number(rawH) : Number.NaN;
    if (!Number.isInteger(parsedW) || parsedW < MIN_EXPORT_DIM || parsedW > MAX_EXPORT_DIM) {
      errors.push(`Width must be an integer between ${MIN_EXPORT_DIM} and ${MAX_EXPORT_DIM} px.`);
    }
    if (!Number.isInteger(parsedH) || parsedH < MIN_EXPORT_DIM || parsedH > MAX_EXPORT_DIM) {
      errors.push(`Height must be an integer between ${MIN_EXPORT_DIM} and ${MAX_EXPORT_DIM} px.`);
    }
    width = Number.isInteger(parsedW) ? parsedW : 0;
    height = Number.isInteger(parsedH) ? parsedH : 0;
  } else {
    width = clamp(vp.width, MIN_EXPORT_DIM, MAX_EXPORT_DIM);
    height = clamp(vp.height, MIN_EXPORT_DIM, MAX_EXPORT_DIM);
  }

  if (!errors.length && width * height > MAX_EXPORT_AREA) {
    errors.push(`Total pixel area (${(width * height).toLocaleString()} px) exceeds safe browser canvas limit (${MAX_EXPORT_AREA.toLocaleString()} px).`);
  }
  if (!errors.length && width * height > WARN_EXPORT_AREA) {
    warnings.push(`High output resolution (${width} × ${height} px) may be slow or memory-intensive.`);
  }

  return {
    preset,
    width,
    height,
    valid: errors.length === 0,
    errors,
    warnings,
  };
}

/** Validate all tour export options and normalize numeric fields. */
export function validateTourExportOptions(tour, opts = {}, viewportSize = null) {
  const steps = Array.isArray(tour?.steps) ? tour.steps : [];
  const stepCount = steps.length;
  const errors = [];
  const warnings = [];

  if (!tour || stepCount === 0) {
    errors.push("Add at least 1 tour target before exporting frames.");
  }

  const rangeMode = opts.rangeMode === "custom" ? "custom" : "all";
  let fromIndex = 0;
  let toIndex = Math.max(0, stepCount - 1);

  if (stepCount > 0 && rangeMode === "custom") {
    const rawFrom = Number(opts.fromTarget);
    const rawTo = Number(opts.toTarget);
    if (!Number.isInteger(rawFrom) || rawFrom < 1 || rawFrom > stepCount) {
      errors.push(`From Target must be between 1 and ${stepCount}.`);
    }
    if (!Number.isInteger(rawTo) || rawTo < 1 || rawTo > stepCount) {
      errors.push(`To Target must be between 1 and ${stepCount}.`);
    }
    if (Number.isInteger(rawFrom) && Number.isInteger(rawTo)) {
      if (rawFrom > rawTo) errors.push("From Target must be less than or equal to To Target.");
      fromIndex = clamp(rawFrom - 1, 0, stepCount - 1);
      toIndex = clamp(rawTo - 1, 0, stepCount - 1);
    }
  }

  const selectedCount = stepCount > 0 && fromIndex <= toIndex ? toIndex - fromIndex + 1 : 0;
  const canTransition = selectedCount >= 2;
  const includeTransitions = Boolean(opts.includeTransitions) && canTransition;

  const fpsRaw = opts.fpsPreset === "custom" ? opts.customFps : (opts.fpsPreset ?? opts.fps ?? "30");
  const fpsNum = Number(String(fpsRaw ?? "").trim());
  const fpsValid = Number.isFinite(fpsNum) && fpsNum >= MIN_FPS && fpsNum <= MAX_FPS;
  const fps = fpsValid ? fpsNum : 30;
  if (!fpsValid) errors.push(`FPS must be a number between ${MIN_FPS} and ${MAX_FPS}.`);

  const rawDur = String(opts.durationOverride ?? "").trim();
  const tourDefaultDur = Number(tour?.transitionDurationSec) > 0 ? Number(tour.transitionDurationSec) : DEFAULT_TOUR_TRANSITION_SEC;
  let durationSec = tourDefaultDur;
  if (rawDur !== "") {
    const dNum = Number(rawDur);
    if (!Number.isFinite(dNum) || dNum <= 0) {
      errors.push("Transition duration override must be a positive number of seconds.");
    } else {
      durationSec = dNum;
    }
  }

  const rawHold = String(opts.holdSeconds ?? "0").trim();
  const holdNum = rawHold === "" ? 0 : Number(rawHold);
  let holdSeconds = 0;
  if (!Number.isFinite(holdNum) || holdNum < 0) {
    errors.push("Hold per target stop must be 0 or a positive number of seconds.");
  } else {
    holdSeconds = holdNum;
  }

  const fileCheck = validateFilenameConfig(opts.prefix, opts.suffix);
  errors.push(...fileCheck.errors);

  const res = resolveExportDimensions(opts, viewportSize);
  errors.push(...res.errors);
  warnings.push(...res.warnings);

  const ref = normalizeViewportSize(viewportSize);
  return {
    valid: errors.length === 0,
    errors,
    warnings,
    normalized: {
      stepCount,
      rangeMode,
      fromIndex,
      toIndex,
      selectedCount,
      canTransition,
      includeTransitions,
      fps,
      fpsApplied: includeTransitions || holdSeconds > 0,
      durationSec,
      holdSeconds,
      prefix: fileCheck.prefix,
      suffix: fileCheck.suffix,
      transparent: opts.transparent !== false,
      hideHud: opts.hideHud !== false,
      resolution: res,
      refWidth: ref.width,
      refHeight: ref.height,
    },
  };
}

// ------------------------------------------------------------------ Deterministic stepped frame schedule

/**
 * Plan the deterministic frame schedule for a tour export:
 * - Target stops emit `holdSeconds > 0 ? Math.max(1, Math.round(holdSeconds * fps)) : 1` frames.
 * - Transitions between consecutive targets emit `intervals - 1` strictly interior frames at
 *   `t_k = k / intervals` mapped through `tourEaseOut`, guaranteeing zero duplicate endpoint frames.
 */
export function planTourFrames(tour, opts = {}, viewportSize = null) {
  const check = validateTourExportOptions(tour, opts, viewportSize);
  const norm = check.normalized;
  const steps = Array.isArray(tour?.steps) ? tour.steps : [];

  if (!steps.length || norm.selectedCount <= 0 || norm.fromIndex > norm.toIndex) {
    return {
      valid: false,
      errors: check.errors,
      warnings: check.warnings,
      normalized: norm,
      totalFrames: 0,
      approxDurationSec: 0,
      stopCount: 0,
      transitionCount: 0,
      canTransition: false,
      frames: [],
    };
  }

  const {
    fromIndex, toIndex, includeTransitions, fps, fpsApplied,
    durationSec, holdSeconds, resolution, refWidth, refHeight,
  } = norm;
  const outW = resolution.width || refWidth;
  const outH = resolution.height || refHeight;

  const stopFrames = holdSeconds > 0 ? Math.max(1, Math.round(holdSeconds * fps)) : 1;
  const intervals = Math.max(1, Math.round(durationSec * fps));
  const easeFn = typeof opts.easeFn === "function" ? opts.easeFn : tourEaseOut;

  const frames = [];
  let stopCount = 0;
  let transitionCount = 0;

  for (let i = fromIndex; i <= toIndex; i++) {
    const step = steps[i];
    const camStop = viewportToCamera(step?.viewport, refWidth, refHeight);
    const vpStop = cameraToViewport(camStop, refWidth, refHeight);
    const vbStop = computeExportViewBox(camStop, outW, outH, refWidth, refHeight);
    const stepLabel = step?.title ? `${i + 1}. ${step.title}` : `Target ${i + 1}`;
    stopCount += 1;

    for (let f = 0; f < stopFrames; f++) {
      frames.push({
        frameIndex: frames.length,
        frameNumber: frames.length + 1,
        kind: "stop",
        stepIndex: i,
        fromIndex: i,
        toIndex: i,
        discreteStepIndex: i,
        stopFrameIndex: f,
        stopFrameCount: stopFrames,
        rawT: 0,
        easedT: 0,
        camera: camStop,
        viewport: vpStop,
        viewBox: vbStop,
        label: stopFrames > 1 ? `${stepLabel} (${f + 1}/${stopFrames})` : stepLabel,
      });
    }

    if (includeTransitions && i < toIndex) {
      const nextStep = steps[i + 1];
      const camNext = viewportToCamera(nextStep?.viewport, refWidth, refHeight);
      for (let k = 1; k < intervals; k++) {
        const rawT = k / intervals;
        const easedT = easeFn(rawT);
        const cam = interpolateCamera(camStop, camNext, easedT);
        const vp = cameraToViewport(cam, refWidth, refHeight);
        const vb = computeExportViewBox(cam, outW, outH, refWidth, refHeight);
        const discreteStepIndex = rawT < 0.5 ? i : i + 1;
        transitionCount += 1;
        frames.push({
          frameIndex: frames.length,
          frameNumber: frames.length + 1,
          kind: "transition",
          stepIndex: discreteStepIndex,
          fromIndex: i,
          toIndex: i + 1,
          discreteStepIndex,
          transitionFrameIndex: k - 1,
          transitionFrameCount: intervals - 1,
          rawT,
          easedT,
          camera: cam,
          viewport: vp,
          viewBox: vb,
          label: `Transition ${i + 1} → ${i + 2} (${Math.round(rawT * 100)}%)`,
        });
      }
    }
  }

  const totalFrames = frames.length;
  const warnings = [...check.warnings];
  if (totalFrames > WARN_FRAME_COUNT) {
    warnings.push(`Large export (${totalFrames} frames) will take longer and write ${totalFrames} PNG files.`);
  }
  const approxDurationSec = fpsApplied ? totalFrames / fps : 0;

  return {
    valid: check.valid && totalFrames > 0,
    errors: check.errors,
    warnings,
    normalized: norm,
    totalFrames,
    approxDurationSec,
    stopCount,
    transitionCount,
    canTransition: norm.canTransition,
    frames,
  };
}
